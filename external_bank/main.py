"""
External Bank Simulation API
=============================
FastAPI service simulating an external banking system.
Uses isolated SQLite databases per tenant (bank).

Supports CSV files with `;` (semicolon) or `,` (comma) delimiters.
Automatically maps mock-data column names to the internal schema.

Endpoints:
    POST /upload          - Upload a CSV file (loans/payments)
    GET  /data            - Return stored data as JSON
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

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from models import Loan, Payment, DataVersion
from schemas import (
    UploadResponse, DataResponse, VersionResponse,
    TenantListResponse, HealthResponse, ErrorResponse,
    LoanRecord, PaymentRecord, DataVersionInfo,
)
from db import get_session, init_tenant_db, list_tenants, DATABASE_DIR

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("external_bank")

# --- FastAPI Application ---
app = FastAPI(
    title="External Bank Simulation API",
    description="API simulating an external banking system. Accepts credit/payment data via CSV.",
    version="2.0.0",
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
).instrument(app).expose(app, endpoint="/metrics")


# --- Helper Functions ---

def compute_checksum(records: list[dict]) -> str:
    """Computes SHA-256 checksum for data integrity verification."""
    raw = str(sorted([str(sorted(r.items())) for r in records]))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _detect_delimiter(content: str) -> str:
    """Auto-detect CSV delimiter by inspecting the header line."""
    first_line = content.split("\n", 1)[0]
    if ";" in first_line:
        return ";"
    return ","


def parse_csv_content(content: str) -> list[dict]:
    """
    Parses CSV content into a list of dictionaries.
    Auto-detects delimiter (`;` or `,`).
    """
    delimiter = _detect_delimiter(content)
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    records = []
    for row in reader:
        # Skip empty rows
        if any(v and v.strip() for v in row.values()):
            # Lowercase all keys and strip whitespace
            cleaned = {
                k.strip().lower(): (v.strip() if v else "")
                for k, v in row.items()
                if k is not None
            }
            records.append(cleaned)
    return records


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


def update_version(session, file_type: str, loan_type: str, record_count: int, checksum: str) -> int:
    """Updates or creates a DataVersion entry. Returns the new version number."""
    version_entry = (
        session.query(DataVersion)
        .filter_by(file_type=file_type, loan_type=loan_type)
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
):
    """
    Upload a CSV file to store bank data.

    - **tenant_id**: Bank identifier (each bank has its own isolated SQLite database)
    - **file_type**: 'loans' (credit records) or 'payments' (payment records)
    - **loan_type**: 'RETAIL' or 'COMMERCIAL'
    - **file**: The CSV file to upload (supports `;` or `,` delimiters)

    Performs full replacement of existing data for the given loan_type.
    """
    # -- Validation --
    tenant_id = tenant_id.strip().upper()
    file_type = file_type.strip().lower()
    loan_type = loan_type.strip().upper()

    if file_type not in ("loans", "payments"):
        raise HTTPException(status_code=400, detail="file_type must be 'loans' or 'payments'.")

    if loan_type not in ("RETAIL", "COMMERCIAL"):
        raise HTTPException(status_code=400, detail="loan_type must be 'RETAIL' or 'COMMERCIAL'.")

    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    # -- Read CSV --
    try:
        raw_content = await file.read()
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback to latin-1 encoding
        content = raw_content.decode("latin-1")

    records = parse_csv_content(content)

    if not records:
        raise HTTPException(status_code=400, detail="CSV file is empty or has an invalid format.")

    logger.info(f"[{tenant_id}] {file_type}/{loan_type} upload started - {len(records)} records")

    # -- Write to Database --
    session = get_session(tenant_id)
    try:
        if file_type == "loans":
            _process_loans(session, records, loan_type)
        else:
            _process_payments(session, records, loan_type)

        # Update version
        checksum = compute_checksum(records)
        new_version = update_version(session, file_type, loan_type, len(records), checksum)

        session.commit()

        logger.info(
            f"[{tenant_id}] {file_type}/{loan_type} upload completed - "
            f"{len(records)} records, version: {new_version}"
        )

        return UploadResponse(
            status="success",
            message=f"{len(records)} records uploaded successfully.",
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
            records_processed=len(records),
            version=new_version,
        )

    except Exception as e:
        session.rollback()
        logger.error(f"[{tenant_id}] Upload error: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        session.close()


def _process_loans(session, records: list[dict], loan_type: str):
    """
    Writes loan records to the database (deletes existing data for the loan_type first).
    Maps CSV column names to model fields.
    """
    # Full replacement - clear existing data
    session.query(Loan).filter_by(loan_type=loan_type).delete()

    for record in records:
        loan = Loan(
            loan_account_number=record.get("loan_account_number", ""),
            loan_type=loan_type,
            customer_id=record.get("customer_id", ""),
            customer_type=record.get("customer_type", ""),

            loan_status_code=record.get("loan_status_code", ""),
            loan_status_flag=record.get("loan_status_flag", ""),
            days_past_due=_safe_int(record.get("days_past_due", "0"), 0),

            loan_start_date=record.get("loan_start_date", ""),
            final_maturity_date=record.get("final_maturity_date", ""),
            first_payment_date=record.get("first_payment_date", ""),
            loan_closing_date=record.get("loan_closing_date", ""),

            total_installment_count=_safe_int(record.get("total_installment_count")),
            outstanding_installment_count=_safe_int(record.get("outstanding_installment_count")),
            paid_installment_count=_safe_int(record.get("paid_installment_count")),
            installment_frequency=_safe_int(record.get("installment_frequency")),
            grace_period_months=_safe_int(record.get("grace_period_months")),

            original_loan_amount=_safe_float(record.get("original_loan_amount")),
            outstanding_principal_balance=_safe_float(record.get("outstanding_principal_balance")),

            nominal_interest_rate=record.get("nominal_interest_rate", ""),
            total_interest_amount=_safe_float(record.get("total_interest_amount")),
            kkdf_rate=_safe_float(record.get("kkdf_rate")),
            kkdf_amount=_safe_float(record.get("kkdf_amount")),
            bsmv_rate=_safe_float(record.get("bsmv_rate")),
            bsmv_amount=_safe_float(record.get("bsmv_amount")),

            insurance_included=record.get("insurance_included", ""),

            customer_district_code=record.get("customer_district_code", ""),
            customer_province_code=record.get("customer_province_code", ""),
            customer_region_code=record.get("customer_region_code", ""),

            internal_rating=record.get("internal_rating", ""),
            external_rating=record.get("external_rating", ""),

            loan_product_type=record.get("loan_product_type", ""),
            sector_code=record.get("sector_code", ""),
            internal_credit_rating=record.get("internal_credit_rating", ""),
            default_probability=_safe_float(record.get("default_probability")),
            risk_class=record.get("risk_class", ""),
            customer_segment=record.get("customer_segment", ""),
        )
        session.add(loan)


def _process_payments(session, records: list[dict], loan_type: str):
    """
    Writes payment records to the database (deletes existing data for the loan_type first).
    Generates payment_id from loan_account_number + installment_number.
    """
    # Full replacement - clear existing data
    session.query(Payment).filter_by(loan_type=loan_type).delete()

    for record in records:
        loan_acct = record.get("loan_account_number", "")
        inst_num = record.get("installment_number", "0")
        # Derive payment_id
        payment_id = f"{loan_acct}_{inst_num}"

        payment = Payment(
            payment_id=payment_id,
            loan_account_number=loan_acct,
            loan_type=loan_type,
            installment_number=_safe_int(inst_num, 0),

            actual_payment_date=record.get("actual_payment_date", ""),
            scheduled_payment_date=record.get("scheduled_payment_date", ""),

            installment_amount=_safe_float(record.get("installment_amount")),
            principal_component=_safe_float(record.get("principal_component")),
            interest_component=_safe_float(record.get("interest_component")),
            kkdf_component=_safe_float(record.get("kkdf_component")),
            bsmv_component=_safe_float(record.get("bsmv_component")),

            installment_status=record.get("installment_status", ""),

            remaining_principal=_safe_float(record.get("remaining_principal")),
            remaining_interest=_safe_float(record.get("remaining_interest")),
            remaining_kkdf=_safe_float(record.get("remaining_kkdf")),
            remaining_bsmv=_safe_float(record.get("remaining_bsmv")),
        )
        session.add(payment)


@app.get("/data", response_model=DataResponse, responses={404: {"model": ErrorResponse}})
async def get_data(
    tenant_id: str = Query(..., description="Bank/Tenant identifier (e.g., BANK001)"),
    file_type: str = Query(..., description="File type: 'loans' or 'payments'"),
    loan_type: str = Query(..., description="Loan type: 'RETAIL' or 'COMMERCIAL'"),
    limit: Optional[int] = Query(None, ge=1, description="Maximum number of records to return"),
    offset: Optional[int] = Query(None, ge=0, description="Number of records to skip (pagination)"),
):
    """
    Returns the stored data for a specific tenant as JSON.

    The Adapter service uses this endpoint to fetch data for processing.
    """
    tenant_id = tenant_id.strip().upper()
    file_type = file_type.strip().lower()
    loan_type = loan_type.strip().upper()

    if file_type not in ("loans", "payments"):
        raise HTTPException(status_code=400, detail="file_type must be 'loans' or 'payments'.")

    if loan_type not in ("RETAIL", "COMMERCIAL"):
        raise HTTPException(status_code=400, detail="loan_type must be 'RETAIL' or 'COMMERCIAL'.")

    session = get_session(tenant_id)
    try:
        # Get version info
        version_entry = (
            session.query(DataVersion)
            .filter_by(file_type=file_type, loan_type=loan_type)
            .first()
        )
        current_version = version_entry.version if version_entry else None

        if file_type == "loans":
            query = session.query(Loan).filter_by(loan_type=loan_type)
            total = query.count()

            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

            results = query.all()
            data = [LoanRecord.model_validate(r).model_dump(mode="json") for r in results]
        else:
            query = session.query(Payment).filter_by(loan_type=loan_type)
            total = query.count()

            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

            results = query.all()
            data = [PaymentRecord.model_validate(r).model_dump(mode="json") for r in results]

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
):
    """
    Returns all data versions for a tenant.

    The Adapter service uses this endpoint to check if new data is available.
    The version number is incremented on each upload.
    """
    tenant_id = tenant_id.strip().upper()
    session = get_session(tenant_id)

    try:
        versions = session.query(DataVersion).all()
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
async def get_tenants():
    """Lists all registered tenants (banks) in the system."""
    tenants = list_tenants()
    return TenantListResponse(tenants=tenants, count=len(tenants))


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Service health check endpoint."""
    tenants = list_tenants()
    return HealthResponse(
        status="healthy",
        service="External Bank Simulation API",
        database_dir=DATABASE_DIR,
        active_tenants=len(tenants),
    )


# --- Application Startup ---

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("External Bank Simulation API started (v2.0.0)")
    logger.info(f"Database directory: {DATABASE_DIR}")
    logger.info(f"Active tenants: {len(list_tenants())}")
    logger.info("=" * 60)
