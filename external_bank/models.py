"""
SQLAlchemy ORM Models - External Bank Simulation
Stored in isolated SQLite databases per tenant (bank).

Expanded to support full Turkish banking credit portfolio fields:
  - Retail & Commercial credit records (27-32 columns)
  - Payment plan / installment records (14 columns)
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, DateTime, Text,
    Index, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Loan(Base):
    """
    Loan (credit) records table.

    Stores ALL fields from the source CSV (retail & commercial).
    Fields unique to commercial credits are nullable.
    """
    __tablename__ = "loans"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Core identifiers ──
    loan_account_number = Column(String(50), nullable=False, index=True)
    loan_type = Column(String(20), nullable=False)    # RETAIL / COMMERCIAL
    customer_id = Column(String(50), nullable=True)
    customer_type = Column(String(10), nullable=True)  # I (Individual) / T (Tüzel) / V

    # ── Loan status ──
    loan_status_code = Column(String(10), nullable=True)   # A, K, etc.
    loan_status_flag = Column(String(10), nullable=True)   # Commercial only
    days_past_due = Column(Integer, nullable=True, default=0)

    # ── Dates ──
    loan_start_date = Column(String(50), nullable=True)
    final_maturity_date = Column(String(50), nullable=True)
    first_payment_date = Column(String(50), nullable=True)
    loan_closing_date = Column(String(50), nullable=True)

    # ── Installment info ──
    total_installment_count = Column(Integer, nullable=True)
    outstanding_installment_count = Column(Integer, nullable=True)
    paid_installment_count = Column(Integer, nullable=True)
    installment_frequency = Column(Integer, nullable=True)
    grace_period_months = Column(Integer, nullable=True)

    # ── Amounts ──
    original_loan_amount = Column(Float, nullable=True)
    outstanding_principal_balance = Column(Float, nullable=True)

    # ── Rates ──
    nominal_interest_rate = Column(String(50), nullable=True)  # Stored raw
    total_interest_amount = Column(Float, nullable=True)
    kkdf_rate = Column(Float, nullable=True)
    kkdf_amount = Column(Float, nullable=True)
    bsmv_rate = Column(Float, nullable=True)
    bsmv_amount = Column(Float, nullable=True)

    # ── Insurance ──
    insurance_included = Column(String(10), nullable=True)  # Retail only (H/E)

    # ── Customer location ──
    customer_district_code = Column(String(50), nullable=True)  # Retail
    customer_province_code = Column(String(50), nullable=True)  # Retail
    customer_region_code = Column(String(50), nullable=True)    # Commercial

    # ── Rating & risk ──
    internal_rating = Column(String(20), nullable=True)
    external_rating = Column(String(20), nullable=True)

    # ── Commercial-only fields ──
    loan_product_type = Column(String(20), nullable=True)
    sector_code = Column(String(20), nullable=True)
    internal_credit_rating = Column(String(20), nullable=True)
    default_probability = Column(Float, nullable=True)
    risk_class = Column(String(20), nullable=True)
    customer_segment = Column(String(20), nullable=True)

    # ── Metadata ──
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("loan_account_number", "loan_type", name="uq_loan_id_type"),
    )


class Payment(Base):
    """
    Payment plan / installment records table.

    Stores ALL 14 fields from the source CSV.
    payment_id is derived: loan_account_number + '_' + installment_number
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Core identifiers ──
    payment_id = Column(String(70), nullable=False, index=True)  # derived
    loan_account_number = Column(String(50), nullable=False, index=True)
    loan_type = Column(String(20), nullable=False)
    installment_number = Column(Integer, nullable=True)

    # ── Dates ──
    actual_payment_date = Column(String(50), nullable=True)
    scheduled_payment_date = Column(String(50), nullable=True)

    # ── Amounts ──
    installment_amount = Column(Float, nullable=True)
    principal_component = Column(Float, nullable=True)
    interest_component = Column(Float, nullable=True)
    kkdf_component = Column(Float, nullable=True)
    bsmv_component = Column(Float, nullable=True)

    # ── Status ──
    installment_status = Column(String(10), nullable=True)  # A, K, etc.

    # ── Remaining balances ──
    remaining_principal = Column(Float, nullable=True)
    remaining_interest = Column(Float, nullable=True)
    remaining_kkdf = Column(Float, nullable=True)
    remaining_bsmv = Column(Float, nullable=True)

    # ── Metadata ──
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("payment_id", "loan_type", name="uq_payment_id_type"),
    )


class DataVersion(Base):
    """
    Data versioning table.
    Used by the Adapter service for sync checks.
    Each upload increments the version number.
    """
    __tablename__ = "data_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_type = Column(String(20), nullable=False)   # loans / payments
    loan_type = Column(String(20), nullable=False)    # RETAIL / COMMERCIAL
    version = Column(Integer, nullable=False, default=1)
    record_count = Column(Integer, nullable=False, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    checksum = Column(String(64), nullable=True)      # For data integrity checks

    __table_args__ = (
        UniqueConstraint("file_type", "loan_type", name="uq_file_loan_type"),
    )
