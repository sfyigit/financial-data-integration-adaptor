"""
External Bank Simulation API
=============================
FastAPI service simulating an external banking system.
Uses PostgreSQL (multi-tenant) + MinIO (S3 object storage).

Architecture:
    - CSV uploads are stored in MinIO for durable, streaming access.
    - Parsed records are stored in PostgreSQL for querying.
    - Sync service downloads CSVs from MinIO via presigned URLs.

Endpoints:
    POST /upload          - Upload a CSV file (loans/payments) → MinIO + PostgreSQL
    GET  /data            - Return stored data as JSON (with cursor pagination)
    GET  /version         - Data version info (for sync checks)
    GET  /files/download  - Get presigned MinIO URL for a specific version
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
from models import Loan, Payment, DataVersion, FileUpload
from schemas import (
    UploadResponse, DataResponse, VersionResponse,
    TenantListResponse, HealthResponse, ErrorResponse,
    LoanRecord, PaymentRecord, DataVersionInfo,
    FileDownloadResponse,
)
from db import get_session, init_db, list_tenants
import minio_client as mc

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
        "Stores files in MinIO, records in PostgreSQL."
    ),
    version="3.0.0",
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
    minio_object_key: str = None,
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
        version_entry.minio_object_key = minio_object_key
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
            minio_object_key=minio_object_key,
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

    Flow:
    1. Stream upload to temp file on disk.
    2. Upload the temp file to MinIO (S3).
    3. Parse CSV and bulk-insert into PostgreSQL.
    4. Update version metadata.

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

    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    # -- Stream upload to temp file --
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
        logger.info(
            f"[{tenant_id}] File received: {size_mb:.1f} MB "
            f"({file_type}/{loan_type}) - saved to temp file"
        )
    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        logger.error(f"[{tenant_id}] File read error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {str(e)}")

    # -- Process: MinIO upload + PostgreSQL insert --
    try:
        # Detect delimiter from first line
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline()
        delimiter = ";" if ";" in first_line else ","

        # Count total records
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            total_lines = sum(1 for _ in f) - 1  # subtract header

        if total_lines <= 0:
            raise HTTPException(status_code=400, detail="CSV file is empty or has an invalid format.")

        logger.info(f"[{tenant_id}] {file_type}/{loan_type} upload started - {total_lines} records")

        # -- Step 1: Upload CSV to MinIO --
        # Get the next version number (peek)
        session = get_session()
        try:
            version_entry = (
                session.query(DataVersion)
                .filter_by(tenant_id=tenant_id, file_type=file_type, loan_type=loan_type)
                .first()
            )
            next_version = (version_entry.version + 1) if version_entry else 1
        finally:
            session.close()

        object_key = mc.build_object_key(
            tenant_id, file_type, loan_type, next_version, file.filename,
        )
        minio_result = mc.upload_file(object_key, tmp_path)
        logger.info(f"[{tenant_id}] CSV uploaded to MinIO: {object_key}")

        # -- Step 2: Bulk insert into PostgreSQL --
        session = get_session()
        try:
            BATCH_SIZE = 50_000

            # Delete existing data for this tenant/loan_type
            if file_type == "loans":
                session.query(Loan).filter_by(tenant_id=tenant_id, loan_type=loan_type).delete()
            else:
                session.query(Payment).filter_by(tenant_id=tenant_id, loan_type=loan_type).delete()
            session.commit()

            # Stream CSV file and batch-insert
            total_inserted = 0
            batch_num = 0
            checksum_hash = hashlib.sha256()

            with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                batch = []

                for record in reader:
                    batch.append(record)
                    checksum_hash.update(str(sorted(record.items())).encode())

                    if len(batch) >= BATCH_SIZE:
                        batch_num += 1
                        if file_type == "loans":
                            _insert_loan_batch(session, batch, tenant_id, loan_type)
                        else:
                            _insert_payment_batch(session, batch, tenant_id, loan_type)
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
                        _insert_loan_batch(session, batch, tenant_id, loan_type)
                    else:
                        _insert_payment_batch(session, batch, tenant_id, loan_type)
                    total_inserted += len(batch)

            # Update version and record file upload
            checksum = checksum_hash.hexdigest()[:16]
            new_version = update_version(
                session, tenant_id, file_type, loan_type,
                total_inserted, checksum, object_key,
            )

            # Record the file upload history
            upload_record = FileUpload(
                tenant_id=tenant_id,
                file_type=file_type,
                loan_type=loan_type,
                version=new_version,
                filename=file.filename,
                minio_object_key=object_key,
                file_size=total_size,
                record_count=total_inserted,
                checksum=checksum,
            )
            session.add(upload_record)
            session.commit()

            logger.info(
                f"[{tenant_id}] {file_type}/{loan_type} upload completed - "
                f"{total_inserted} records, version: {new_version}, MinIO: {object_key}"
            )

            return UploadResponse(
                status="success",
                message=f"{total_inserted} records uploaded successfully.",
                tenant_id=tenant_id,
                file_type=file_type,
                loan_type=loan_type,
                records_processed=total_inserted,
                version=new_version,
                minio_object_key=object_key,
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


def _insert_loan_batch(session, records: list[dict], tenant_id: str, loan_type: str):
    """Insert a batch of loan records using bulk_insert_mappings."""
    mappings = [
        {
            "tenant_id": tenant_id,
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


def _insert_payment_batch(session, records: list[dict], tenant_id: str, loan_type: str):
    """Insert a batch of payment records using bulk_insert_mappings."""
    mappings = [
        {
            "tenant_id": tenant_id,
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
    after_id: Optional[int] = Query(None, ge=0, description="Cursor: return records with id > after_id"),
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
):
    """
    Returns all data versions for a tenant.
    Includes MinIO object keys for the sync service.
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


@app.get("/files/download", response_model=FileDownloadResponse)
async def get_file_download_url(
    tenant_id: str = Query(..., description="Bank/Tenant identifier"),
    file_type: str = Query(..., description="File type: 'loans' or 'payments'"),
    loan_type: str = Query(..., description="Loan type: 'RETAIL' or 'COMMERCIAL'"),
    version: Optional[int] = Query(None, description="Specific version (default: latest)"),
):
    """
    Get a presigned MinIO URL to download the CSV file for a specific version.
    If version is not specified, returns the latest version.

    Used by the sync service to download files directly from MinIO
    instead of paginating through the /data endpoint.
    """
    tenant_id = tenant_id.strip().upper()
    file_type = file_type.strip().lower()
    loan_type = loan_type.strip().upper()

    session = get_session()
    try:
        if version:
            upload = (
                session.query(FileUpload)
                .filter_by(
                    tenant_id=tenant_id,
                    file_type=file_type,
                    loan_type=loan_type,
                    version=version,
                )
                .first()
            )
        else:
            upload = (
                session.query(FileUpload)
                .filter_by(
                    tenant_id=tenant_id,
                    file_type=file_type,
                    loan_type=loan_type,
                )
                .order_by(FileUpload.version.desc())
                .first()
            )

        if not upload:
            raise HTTPException(
                status_code=404,
                detail=f"No file found for {tenant_id}/{file_type}/{loan_type}"
                + (f" v{version}" if version else ""),
            )

        # Generate presigned URL (valid for 1 hour)
        presigned_url = mc.get_presigned_url(upload.minio_object_key)

        return FileDownloadResponse(
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
            version=upload.version,
            filename=upload.filename,
            minio_object_key=upload.minio_object_key,
            presigned_url=presigned_url,
            file_size=upload.file_size,
            record_count=upload.record_count,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{tenant_id}] File download URL error: {e}")
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
        service="External Bank Simulation API (PostgreSQL + MinIO)",
        active_tenants=len(tenants),
    )


# --- Application Startup ---

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("External Bank Simulation API started (v3.0.0)")
    logger.info("Backend: PostgreSQL + MinIO")
    logger.info("=" * 60)

    # Initialize PostgreSQL tables
    try:
        init_db()
        logger.info("PostgreSQL database initialized")
    except Exception as e:
        logger.error(f"PostgreSQL initialization failed: {e}")

    # Initialize MinIO bucket
    try:
        mc.ensure_bucket()
        logger.info(f"MinIO bucket ready: {mc.MINIO_BUCKET}")
    except Exception as e:
        logger.error(f"MinIO initialization failed: {e}")
