"""
External Bank Simulation API
=============================
FastAPI service simulating an external banking system.
Uses PostgreSQL (multi-tenant) for data storage.

Architecture:
    - CSV uploads are streamed and directly inserted into PostgreSQL.
    - Parsed records are stored in PostgreSQL for querying.
    - Sync service fetches data via JSON cursor pagination.

Endpoints:
    POST /upload          - Upload a CSV file (loans/payments) → PostgreSQL
    GET  /data            - Return stored data as JSON (with cursor pagination)
    GET  /version         - Data version info (for sync checks)
    GET  /tenants         - List available tenants
    GET  /health          - Health check
"""

import csv
import io
import hashlib
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from psycopg2.extras import execute_values

from sqlalchemy import text
from models import Loan, Payment, DataVersion, FileUpload
from auth import verify_api_key, AuthenticatedService
from schemas import (
    UploadResponse, DataResponse, VersionResponse,
    TenantListResponse, HealthResponse, ErrorResponse,
    LoanRecord, PaymentRecord, DataVersionInfo,
)
from db import get_session, get_engine, init_db, list_tenants

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("external_bank")

# --- FastAPI Application ---
app = FastAPI(
    title="External Bank Simulation API",
    description=(
        "API simulating an external banking system. "
        "Accepts credit/payment data via CSV. "
        "Stores records directly in PostgreSQL."
    ),
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Prometheus Metrics ---
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics"],
).instrument(
    app,
    latency_lowr_buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 30.0, 60.0, 120.0),
).expose(app, endpoint="/metrics")


# --- Helper Functions ---

def _safe_float(value, default=0.0):
    """Safely convert a value to float, returning default on failure."""
    if not value or not str(value).strip():
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _safe_int(value, default=None):
    """Safely convert a value to int, returning default on failure."""
    if not value or not str(value).strip():
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def update_version(
    session,
    tenant_id: str,
    file_type: str,
    loan_type: str,
    record_count: int,
    checksum: str,
) -> int:
    """Updates or creates a DataVersion entry. Returns the new version number."""
    version_entry = (
        session.query(DataVersion)
        .filter_by(tenant_id=tenant_id, file_type=file_type, loan_type=loan_type)
        .first()
    )

    if version_entry:
        version_entry.version += 1
        version_entry.record_count = record_count
        version_entry.last_updated = datetime.utcnow()
        version_entry.checksum = checksum
        new_version = version_entry.version
    else:
        version_entry = DataVersion(
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
            version=1,
            record_count=record_count,
            last_updated=datetime.utcnow(),
            checksum=checksum,
        )
        session.add(version_entry)
        new_version = 1

    return new_version


# --- Endpoints ---

@app.post("/upload", response_model=UploadResponse, responses={400: {"model": ErrorResponse}})
async def upload_csv(
    file: UploadFile = File(..., description="CSV file (loans or payments)"),
    tenant_id: str = Query(..., description="Bank/Tenant identifier (e.g., BANK001)"),
    file_type: str = Query(..., description="File type: 'loans' or 'payments'"),
    loan_type: str = Query(..., description="Loan type: 'RETAIL' or 'COMMERCIAL'"),
    authenticated_service: AuthenticatedService = Depends(verify_api_key),
):
    """
    Upload a CSV file to store bank data.

    Flow:
    1. Stream CSV file and parse in batches.
    2. Bulk insert into PostgreSQL using execute_values.
    3. Update version metadata.

    Performs full replacement of existing data for the given tenant_id/loan_type.
    """
    # -- Validation --
    tenant_id = tenant_id.strip().upper()
    file_type = file_type.strip().lower()
    loan_type = loan_type.strip().upper()

    if file_type not in ("loans", "payments"):
        raise HTTPException(status_code=400, detail="file_type must be 'loans' or 'payments'.")

    if loan_type not in ("RETAIL", "COMMERCIAL"):
        raise HTTPException(status_code=400, detail="loan_type must be 'RETAIL' or 'COMMERCIAL'.")

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    # Basic content type and size checks (defense in depth)
    if file.content_type not in ("text/csv", "application/vnd.ms-excel", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Invalid content type for CSV upload.")

    # -- Stream CSV and process --
    session = get_session()
    engine = get_engine()
    conn = engine.raw_connection()
    cursor = conn.cursor()

    try:
        BATCH_SIZE = 5000
        total_inserted = 0
        batch_num = 0
        checksum_hash = hashlib.sha256()
        total_size = 0
        delimiter = None
        first_batch = True

        # Read file content
        content = await file.read()
        total_size = len(content)
        size_mb = total_size / (1024 * 1024)
        logger.info(
            f"[{tenant_id}] File received: {size_mb:.1f} MB "
            f"({file_type}/{loan_type})"
        )

        # Detect delimiter from first line
        first_line = content.split(b'\n')[0].decode('utf-8', errors='replace')
        delimiter = ";" if ";" in first_line else ","

        # Parse CSV from memory
        text_stream = io.StringIO(content.decode('utf-8', errors='replace'))
        reader = csv.DictReader(text_stream, delimiter=delimiter)

        # Delete existing data for this tenant/loan_type
        if file_type == "loans":
            session.query(Loan).filter_by(tenant_id=tenant_id, loan_type=loan_type).delete()
        else:
            session.query(Payment).filter_by(tenant_id=tenant_id, loan_type=loan_type).delete()
        session.commit()

        # Process CSV in batches
        batch = []
        for record in reader:
            # Clean and prepare record
            cleaned_record = {
                k.strip().lower(): (v.strip() if v else "")
                for k, v in record.items()
                if k is not None
            }
            
            if not any(v and v.strip() for v in cleaned_record.values()):
                continue  # Skip empty rows

            batch.append(cleaned_record)
            checksum_hash.update(str(sorted(cleaned_record.items())).encode())

            if len(batch) >= BATCH_SIZE:
                batch_num += 1
                if file_type == "loans":
                    inserted = _insert_loan_batch_execute_values(cursor, batch, tenant_id, loan_type)
                else:
                    inserted = _insert_payment_batch_execute_values(cursor, batch, tenant_id, loan_type)
                total_inserted += inserted
                logger.info(
                    f"  [{tenant_id}] Batch {batch_num}: {total_inserted} records inserted"
                )
                batch = []

        # Insert remaining records
        if batch:
            batch_num += 1
            if file_type == "loans":
                inserted = _insert_loan_batch_execute_values(cursor, batch, tenant_id, loan_type)
            else:
                inserted = _insert_payment_batch_execute_values(cursor, batch, tenant_id, loan_type)
            total_inserted += inserted

        conn.commit()

        # Update version and record file upload
        checksum = checksum_hash.hexdigest()[:16]
        new_version = update_version(
            session, tenant_id, file_type, loan_type,
            total_inserted, checksum,
        )

        # Record the file upload history
        upload_record = FileUpload(
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
            version=new_version,
            filename=file.filename,
            file_size=total_size,
            record_count=total_inserted,
            checksum=checksum,
        )
        session.add(upload_record)
        session.commit()

        logger.info(
            f"[{tenant_id}] {file_type}/{loan_type} upload completed - "
            f"{total_inserted} records, version: {new_version}"
        )

        return UploadResponse(
            status="success",
            message=f"{total_inserted} records uploaded successfully.",
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
            records_processed=total_inserted,
            version=new_version,
        )

    except Exception as e:
        conn.rollback()
        session.rollback()
        logger.error(f"[{tenant_id}] Upload error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cursor.close()
        conn.close()
        session.close()


def _insert_loan_batch_execute_values(cursor, records: list[dict], tenant_id: str, loan_type: str) -> int:
    """Insert a batch of loan records using execute_values for better performance."""
    if not records:
        return 0

    rows = []
    for r in records:
        rows.append((
            tenant_id,
            r.get("loan_account_number", ""),
            loan_type,
            r.get("customer_id", ""),
            r.get("customer_type", ""),
            r.get("loan_status_code", ""),
            r.get("loan_status_flag", ""),
            _safe_int(r.get("days_past_due", "0"), 0),
            r.get("loan_start_date", ""),
            r.get("final_maturity_date", ""),
            r.get("first_payment_date", ""),
            r.get("loan_closing_date", ""),
            _safe_int(r.get("total_installment_count")),
            _safe_int(r.get("outstanding_installment_count")),
            _safe_int(r.get("paid_installment_count")),
            _safe_int(r.get("installment_frequency")),
            _safe_int(r.get("grace_period_months")),
            _safe_float(r.get("original_loan_amount")),
            _safe_float(r.get("outstanding_principal_balance")),
            r.get("nominal_interest_rate", ""),
            _safe_float(r.get("total_interest_amount")),
            _safe_float(r.get("kkdf_rate")),
            _safe_float(r.get("kkdf_amount")),
            _safe_float(r.get("bsmv_rate")),
            _safe_float(r.get("bsmv_amount")),
            r.get("insurance_included", ""),
            r.get("customer_district_code", ""),
            r.get("customer_province_code", ""),
            r.get("customer_region_code", ""),
            r.get("internal_rating", ""),
            r.get("external_rating", ""),
            r.get("loan_product_type", ""),
            r.get("sector_code", ""),
            r.get("internal_credit_rating", ""),
            _safe_float(r.get("default_probability")),
            r.get("risk_class", ""),
            r.get("customer_segment", ""),
        ))

    insert_query = """
        INSERT INTO ext_bank.loans (
            tenant_id, loan_account_number, loan_type, customer_id, customer_type,
            loan_status_code, loan_status_flag, days_past_due,
            loan_start_date, final_maturity_date, first_payment_date, loan_closing_date,
            total_installment_count, outstanding_installment_count, paid_installment_count,
            installment_frequency, grace_period_months,
            original_loan_amount, outstanding_principal_balance,
            nominal_interest_rate, total_interest_amount, kkdf_rate, kkdf_amount, bsmv_rate, bsmv_amount,
            insurance_included, customer_district_code, customer_province_code, customer_region_code,
            internal_rating, external_rating, loan_product_type, sector_code,
            internal_credit_rating, default_probability, risk_class, customer_segment
        ) VALUES %s
    """

    execute_values(
        cursor,
        insert_query,
        rows,
        template=None,
        page_size=len(rows),
    )

    return len(rows)


def _insert_payment_batch_execute_values(cursor, records: list[dict], tenant_id: str, loan_type: str) -> int:
    """Insert a batch of payment records using execute_values for better performance."""
    if not records:
        return 0

    rows = []
    for r in records:
        rows.append((
            tenant_id,
            f"{r.get('loan_account_number', '')}_{r.get('installment_number', '0')}",
            r.get("loan_account_number", ""),
            loan_type,
            _safe_int(r.get("installment_number", "0"), 0),
            r.get("actual_payment_date", ""),
            r.get("scheduled_payment_date", ""),
            _safe_float(r.get("installment_amount")),
            _safe_float(r.get("principal_component")),
            _safe_float(r.get("interest_component")),
            _safe_float(r.get("kkdf_component")),
            _safe_float(r.get("bsmv_component")),
            r.get("installment_status", ""),
            _safe_float(r.get("remaining_principal")),
            _safe_float(r.get("remaining_interest")),
            _safe_float(r.get("remaining_kkdf")),
            _safe_float(r.get("remaining_bsmv")),
        ))

    insert_query = """
        INSERT INTO ext_bank.payments (
            tenant_id, payment_id, loan_account_number, loan_type, installment_number,
            actual_payment_date, scheduled_payment_date,
            installment_amount, principal_component, interest_component,
            kkdf_component, bsmv_component, installment_status,
            remaining_principal, remaining_interest, remaining_kkdf, remaining_bsmv
        ) VALUES %s
    """

    execute_values(
        cursor,
        insert_query,
        rows,
        template=None,
        page_size=len(rows),
    )

    return len(rows)


@app.get("/data", response_model=DataResponse, responses={404: {"model": ErrorResponse}})
async def get_data(
    tenant_id: str = Query(..., description="Bank/Tenant identifier (e.g., BANK001)"),
    file_type: str = Query(..., description="File type: 'loans' or 'payments'"),
    loan_type: str = Query(..., description="Loan type: 'RETAIL' or 'COMMERCIAL'"),
    limit: Optional[int] = Query(None, ge=1, description="Maximum number of records to return"),
    offset: Optional[int] = Query(None, ge=0, description="Number of records to skip (pagination)"),
    after_id: Optional[int] = Query(None, ge=0, description="Cursor: return records with id > after_id"),
    authenticated_service: AuthenticatedService = Depends(verify_api_key),
):
    """
    Returns the stored data for a specific tenant as JSON.
    Supports cursor-based pagination (after_id) for efficient large dataset access.
    """
    tenant_id = tenant_id.strip().upper()
    file_type = file_type.strip().lower()
    loan_type = loan_type.strip().upper()

    if file_type not in ("loans", "payments"):
        raise HTTPException(status_code=400, detail="file_type must be 'loans' or 'payments'.")

    if loan_type not in ("RETAIL", "COMMERCIAL"):
        raise HTTPException(status_code=400, detail="loan_type must be 'RETAIL' or 'COMMERCIAL'.")

    session = get_session()
    try:
        # Get version info
        version_entry = (
            session.query(DataVersion)
            .filter_by(tenant_id=tenant_id, file_type=file_type, loan_type=loan_type)
            .first()
        )
        current_version = version_entry.version if version_entry else None

        Model = Loan if file_type == "loans" else Payment
        RecordSchema = LoanRecord if file_type == "loans" else PaymentRecord

        # Total count
        base_query = session.query(Model).filter_by(tenant_id=tenant_id, loan_type=loan_type)
        total = base_query.count()

        # Build paginated query
        query = base_query.order_by(Model.id)

        if after_id is not None:
            query = query.filter(Model.id > after_id)
        elif offset:
            query = query.offset(offset)

        if limit:
            query = query.limit(limit)

        results = query.all()
        data = [RecordSchema.model_validate(r).model_dump(mode="json") for r in results]

        return DataResponse(
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
            total_records=total,
            version=current_version,
            data=data,
        )

    except Exception as e:
        logger.error(f"[{tenant_id}] Data query error: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        session.close()


@app.get("/version", response_model=VersionResponse)
async def get_version(
    tenant_id: str = Query(..., description="Bank/Tenant identifier (e.g., BANK001)"),
    authenticated_service: AuthenticatedService = Depends(verify_api_key),
):
    """
    Returns all data versions for a tenant.
    """
    tenant_id = tenant_id.strip().upper()
    session = get_session()

    try:
        versions = session.query(DataVersion).filter_by(tenant_id=tenant_id).all()
        version_list = [DataVersionInfo.model_validate(v) for v in versions]

        return VersionResponse(
            tenant_id=tenant_id,
            versions=version_list,
        )
    except Exception as e:
        logger.error(f"[{tenant_id}] Version query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/tenants", response_model=TenantListResponse)
async def get_tenants(authenticated_service: AuthenticatedService = Depends(verify_api_key)):
    """Lists all registered tenants (banks) in the system."""
    tenants = list_tenants()
    return TenantListResponse(tenants=tenants, count=len(tenants))


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Service health check endpoint."""
    tenants = list_tenants()
    return HealthResponse(
        status="healthy",
        service="External Bank Simulation API (PostgreSQL)",
        active_tenants=len(tenants),
    )


# --- Application Startup ---

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("External Bank Simulation API started (v4.0.0)")
    logger.info("Backend: PostgreSQL")
    logger.info("=" * 60)

    # Initialize PostgreSQL tables
    try:
        init_db()
        logger.info("PostgreSQL database initialized")
    except Exception as e:
        logger.error(f"PostgreSQL initialization failed: {e}")
