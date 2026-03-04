"""
Pydantic Schemas - API Request/Response models.

Expanded to support full Turkish banking credit portfolio fields
and MinIO file download endpoints.
"""

from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional


# --- Loan Schemas ---

class LoanRecord(BaseModel):
    """Single loan record — covers both retail and commercial credits."""
    id: Optional[int] = None  # DB primary key (used for cursor-based pagination)
    tenant_id: Optional[str] = None
    # Core identifiers
    loan_account_number: str
    loan_type: str
    customer_id: Optional[str] = None
    customer_type: Optional[str] = None

    # Status
    loan_status_code: Optional[str] = None
    loan_status_flag: Optional[str] = None
    days_past_due: Optional[int] = 0

    # Dates
    loan_start_date: Optional[str] = None
    final_maturity_date: Optional[str] = None
    first_payment_date: Optional[str] = None
    loan_closing_date: Optional[str] = None

    # Installment info
    total_installment_count: Optional[int] = None
    outstanding_installment_count: Optional[int] = None
    paid_installment_count: Optional[int] = None
    installment_frequency: Optional[int] = None
    grace_period_months: Optional[int] = None

    # Amounts
    original_loan_amount: Optional[float] = None
    outstanding_principal_balance: Optional[float] = None

    # Rates
    nominal_interest_rate: Optional[str] = None
    total_interest_amount: Optional[float] = None
    kkdf_rate: Optional[float] = None
    kkdf_amount: Optional[float] = None
    bsmv_rate: Optional[float] = None
    bsmv_amount: Optional[float] = None

    # Insurance (retail)
    insurance_included: Optional[str] = None

    # Location
    customer_district_code: Optional[str] = None
    customer_province_code: Optional[str] = None
    customer_region_code: Optional[str] = None

    # Rating & risk
    internal_rating: Optional[str] = None
    external_rating: Optional[str] = None

    # Commercial-only
    loan_product_type: Optional[str] = None
    sector_code: Optional[str] = None
    internal_credit_rating: Optional[str] = None
    default_probability: Optional[float] = None
    risk_class: Optional[str] = None
    customer_segment: Optional[str] = None

    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# --- Payment Schemas ---

class PaymentRecord(BaseModel):
    """Single payment / installment record."""
    id: Optional[int] = None  # DB primary key (used for cursor-based pagination)
    tenant_id: Optional[str] = None
    payment_id: str
    loan_account_number: str
    loan_type: str
    installment_number: Optional[int] = None

    # Dates
    actual_payment_date: Optional[str] = None
    scheduled_payment_date: Optional[str] = None

    # Amounts
    installment_amount: Optional[float] = None
    principal_component: Optional[float] = None
    interest_component: Optional[float] = None
    kkdf_component: Optional[float] = None
    bsmv_component: Optional[float] = None

    # Status
    installment_status: Optional[str] = None

    # Remaining
    remaining_principal: Optional[float] = None
    remaining_interest: Optional[float] = None
    remaining_kkdf: Optional[float] = None
    remaining_bsmv: Optional[float] = None

    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# --- Version Schemas ---

class DataVersionInfo(BaseModel):
    """Data version information."""
    tenant_id: Optional[str] = None
    file_type: str
    loan_type: str
    version: int
    record_count: int
    last_updated: Optional[datetime] = None
    checksum: Optional[str] = None
    minio_object_key: Optional[str] = None

    class Config:
        from_attributes = True


# --- Response Schemas ---

class UploadResponse(BaseModel):
    """CSV upload result."""
    status: str
    message: str
    tenant_id: str
    file_type: str
    loan_type: str
    records_processed: int
    version: int
    minio_object_key: Optional[str] = None


class DataResponse(BaseModel):
    """Data query result."""
    tenant_id: str
    file_type: str
    loan_type: str
    total_records: int
    version: Optional[int] = None
    data: list


class VersionResponse(BaseModel):
    """Tenant version status."""
    tenant_id: str
    versions: list[DataVersionInfo]


class FileDownloadResponse(BaseModel):
    """Presigned URL response for file download."""
    tenant_id: str
    file_type: str
    loan_type: str
    version: int
    filename: str
    minio_object_key: str
    presigned_url: str
    file_size: Optional[int] = None
    record_count: Optional[int] = None


class TenantListResponse(BaseModel):
    """List of available tenants."""
    tenants: list[str]
    count: int


class HealthResponse(BaseModel):
    """System health status."""
    status: str
    service: str
    active_tenants: int


class ErrorResponse(BaseModel):
    """Error response."""
    status: str = "error"
    message: str
    detail: Optional[str] = None
