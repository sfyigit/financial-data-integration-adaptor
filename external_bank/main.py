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
import os
import tempfile
import hashlib
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from sqlalchemy import text
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
# Custom bucket resolution: default (0.1, 0.5, 1.0) is too coarse for upload operations
# that can take seconds to minutes. We add fine-grained buckets for per-handler latency.
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics"],
).instrument(
    app,
    latency_lowr_buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 30.0, 60.0, 120.0),
).expose(app, endpoint="/metrics")


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
    Uses temp-file + streaming for memory-efficient processing of large files.
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

    # -- Stream upload to temp file (avoids holding entire CSV in memory) --
    CHUNK_SIZE = 8 * 1024 * 1024  # 8MB chunks
    tmp_path = None
    try:
        total_size = 0
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="upload_")
        with os.fdopen(tmp_fd, "wb") as tmp_f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                tmp_f.write(chunk)
                total_size += len(chunk)

        size_mb = total_size / (1024 * 1024)
        if total_size > 200 * 1024 * 1024:
            logger.info(
                f"[{tenant_id}] Large file received: {size_mb:.1f} MB "
                f"({file_type}/{loan_type}) - saved to temp file"
            )

    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        logger.error(f"[{tenant_id}] File read error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {str(e)}")

    # -- Detect delimiter and stream-process CSV from temp file --
    try:
        # Detect delimiter from first line
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline()
        delimiter = ";" if ";" in first_line else ","

        # Count total records for logging (header-aware)
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            total_lines = sum(1 for _ in f) - 1  # subtract header

        if total_lines <= 0:
            raise HTTPException(status_code=400, detail="CSV file is empty or has an invalid format.")

        logger.info(f"[{tenant_id}] {file_type}/{loan_type} upload started - {total_lines} records")

        # -- Write to Database (stream from file, batch inserts) --
        session = get_session(tenant_id)
        try:
            BATCH_SIZE = 50_000

            # Delete existing data
            if file_type == "loans":
                session.query(Loan).filter_by(loan_type=loan_type).delete()
            else:
                session.query(Payment).filter_by(loan_type=loan_type).delete()
            session.commit()

            # Enable SQLite optimizations for bulk inserts
            session.execute(text("PRAGMA synchronous=OFF"))
            session.execute(text("PRAGMA cache_size=-64000"))
            session.execute(text("PRAGMA temp_store=MEMORY"))

            # Stream CSV file and batch-insert
            total_inserted = 0
            batch_num = 0
            checksum_hash = hashlib.sha256()

            with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                batch = []

                for record in reader:
                    batch.append(record)
                    # Update checksum incrementally
                    checksum_hash.update(str(sorted(record.items())).encode())

                    if len(batch) >= BATCH_SIZE:
                        batch_num += 1
                        if file_type == "loans":
                            _insert_loan_batch(session, batch, loan_type)
                        else:
                            _insert_payment_batch(session, batch, loan_type)
                        total_inserted += len(batch)
                        logger.info(
                            f"  [{tenant_id}] Batch {batch_num}: {total_inserted}/{total_lines} "
                            f"records inserted ({total_inserted * 100 // total_lines}%)"
                        )
                        batch = []

                # Insert remaining records
                if batch:
                    batch_num += 1
                    if file_type == "loans":
                        _insert_loan_batch(session, batch, loan_type)
                    else:
                        _insert_payment_batch(session, batch, loan_type)
                    total_inserted += len(batch)

            # Restore PRAGMA and update version
            session.execute(text("PRAGMA synchronous=FULL"))
            checksum = checksum_hash.hexdigest()[:16]
            new_version = update_version(session, file_type, loan_type, total_inserted, checksum)
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
            session.rollback()
            logger.error(f"[{tenant_id}] Upload error: {e}")
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        finally:
            session.close()

    finally:
        # Always clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _insert_loan_batch(session, records: list[dict], loan_type: str):
    """Insert a batch of loan records using bulk_insert_mappings (memory-efficient)."""
    mappings = [
        {
            "loan_account_number": r.get("loan_account_number", ""),
            "loan_type": loan_type,
            "customer_id": r.get("customer_id", ""),
            "customer_type": r.get("customer_type", ""),
            "loan_status_code": r.get("loan_status_code", ""),
            "loan_status_flag": r.get("loan_status_flag", ""),
            "days_past_due": _safe_int(r.get("days_past_due", "0"), 0),
            "loan_start_date": r.get("loan_start_date", ""),
            "final_maturity_date": r.get("final_maturity_date", ""),
            "first_payment_date": r.get("first_payment_date", ""),
            "loan_closing_date": r.get("loan_closing_date", ""),
            "total_installment_count": _safe_int(r.get("total_installment_count")),
            "outstanding_installment_count": _safe_int(r.get("outstanding_installment_count")),
            "paid_installment_count": _safe_int(r.get("paid_installment_count")),
            "installment_frequency": _safe_int(r.get("installment_frequency")),
            "grace_period_months": _safe_int(r.get("grace_period_months")),
            "original_loan_amount": _safe_float(r.get("original_loan_amount")),
            "outstanding_principal_balance": _safe_float(r.get("outstanding_principal_balance")),
            "nominal_interest_rate": r.get("nominal_interest_rate", ""),
            "total_interest_amount": _safe_float(r.get("total_interest_amount")),
            "kkdf_rate": _safe_float(r.get("kkdf_rate")),
            "kkdf_amount": _safe_float(r.get("kkdf_amount")),
            "bsmv_rate": _safe_float(r.get("bsmv_rate")),
            "bsmv_amount": _safe_float(r.get("bsmv_amount")),
            "insurance_included": r.get("insurance_included", ""),
            "customer_district_code": r.get("customer_district_code", ""),
            "customer_province_code": r.get("customer_province_code", ""),
            "customer_region_code": r.get("customer_region_code", ""),
            "internal_rating": r.get("internal_rating", ""),
            "external_rating": r.get("external_rating", ""),
            "loan_product_type": r.get("loan_product_type", ""),
            "sector_code": r.get("sector_code", ""),
            "internal_credit_rating": r.get("internal_credit_rating", ""),
            "default_probability": _safe_float(r.get("default_probability")),
            "risk_class": r.get("risk_class", ""),
            "customer_segment": r.get("customer_segment", ""),
        }
        for r in records
    ]
    session.bulk_insert_mappings(Loan, mappings)
    session.commit()


def _insert_payment_batch(session, records: list[dict], loan_type: str):
    """Insert a batch of payment records using bulk_insert_mappings (memory-efficient)."""
    mappings = [
        {
            "payment_id": f"{r.get('loan_account_number', '')}_{r.get('installment_number', '0')}",
            "loan_account_number": r.get("loan_account_number", ""),
            "loan_type": loan_type,
            "installment_number": _safe_int(r.get("installment_number", "0"), 0),
            "actual_payment_date": r.get("actual_payment_date", ""),
            "scheduled_payment_date": r.get("scheduled_payment_date", ""),
            "installment_amount": _safe_float(r.get("installment_amount")),
            "principal_component": _safe_float(r.get("principal_component")),
            "interest_component": _safe_float(r.get("interest_component")),
            "kkdf_component": _safe_float(r.get("kkdf_component")),
            "bsmv_component": _safe_float(r.get("bsmv_component")),
            "installment_status": r.get("installment_status", ""),
            "remaining_principal": _safe_float(r.get("remaining_principal")),
            "remaining_interest": _safe_float(r.get("remaining_interest")),
            "remaining_kkdf": _safe_float(r.get("remaining_kkdf")),
            "remaining_bsmv": _safe_float(r.get("remaining_bsmv")),
        }
        for r in records
    ]
    session.bulk_insert_mappings(Payment, mappings)
    session.commit()


@app.get("/data", response_model=DataResponse, responses={404: {"model": ErrorResponse}})
async def get_data(
    tenant_id: str = Query(..., description="Bank/Tenant identifier (e.g., BANK001)"),
    file_type: str = Query(..., description="File type: 'loans' or 'payments'"),
    loan_type: str = Query(..., description="Loan type: 'RETAIL' or 'COMMERCIAL'"),
    limit: Optional[int] = Query(None, ge=1, description="Maximum number of records to return"),
    offset: Optional[int] = Query(None, ge=0, description="Number of records to skip (pagination)"),
    after_id: Optional[int] = Query(None, ge=0, description="Cursor: return records with id > after_id (efficient for large datasets)"),
):
    """
    Returns the stored data for a specific tenant as JSON.

    The Adapter service uses this endpoint to fetch data for processing.
    Supports two pagination modes:
      - **offset-based** (legacy): Uses SQL OFFSET — slow for large datasets.
      - **cursor-based** (recommended): Uses `after_id` parameter — O(1) via primary key index.
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

        Model = Loan if file_type == "loans" else Payment
        RecordSchema = LoanRecord if file_type == "loans" else PaymentRecord

        # Total count (cached per query)
        base_query = session.query(Model).filter_by(loan_type=loan_type)
        total = base_query.count()

        # Build paginated query
        query = base_query.order_by(Model.id)

        if after_id is not None:
            # Cursor-based pagination: WHERE id > after_id ORDER BY id (uses PK index)
            query = query.filter(Model.id > after_id)
        elif offset:
            # Legacy offset-based pagination (slow for large offsets)
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
