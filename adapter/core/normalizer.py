"""
Data Normalization Module
=========================
Converts raw financial data from various formats into standardized forms.

Normalization Rules:
- Dates: DD/MM/YYYY, YYYYMMDD, MM/DD/YYYY, YYYY-MM-DD -> ISO YYYY-MM-DD
- Interest Rates: 18.5%, 0.185, 1850 bps -> decimal 0.185
- Status: "Paid", "Closed", "Kapali", "A", "K" -> unified system codes

Column Mapping (mock_data -> internal):
  Loans:
    loan_account_number -> loan_id
    original_loan_amount -> amount
    nominal_interest_rate -> interest_rate
    loan_start_date -> start_date
    loan_status_code -> status

  Payments:
    loan_account_number + installment_number -> payment_id
    loan_account_number -> loan_id
    installment_amount -> payment_amount
    actual_payment_date / scheduled_payment_date -> payment_date
"""

import re
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("adapter.normalizer")


class Normalizer:
    """Handles normalization of financial data fields."""

    # --- Status Mapping ---
    # Maps various status labels to unified system codes
    STATUS_MAP = {
        # Active states
        "active": "ACTIVE",
        "open": "ACTIVE",
        "current": "ACTIVE",
        "performing": "ACTIVE",
        "acik": "ACTIVE",           # Turkish: "Acik"
        "aktif": "ACTIVE",          # Turkish: "Aktif"
        "a": "ACTIVE",              # Short code used in bank data
        # Closed/Paid states
        "paid": "CLOSED",
        "closed": "CLOSED",
        "settled": "CLOSED",
        "completed": "CLOSED",
        "kapali": "CLOSED",         # Turkish: "Kapali"
        "odendi": "CLOSED",         # Turkish: "Odendi"
        "kapatildi": "CLOSED",      # Turkish: "Kapatildi"
        "k": "CLOSED",              # Short code used in bank data
        # Default/Delinquent states
        "default": "DEFAULT",
        "delinquent": "DEFAULT",
        "non-performing": "DEFAULT",
        "npl": "DEFAULT",
        "gecikme": "DEFAULT",       # Turkish: "Gecikme"
        "takip": "DEFAULT",         # Turkish: "Takip"
        "d": "DEFAULT",             # Short code
        "t": "DEFAULT",             # Short code (Takip)
        # Restructured states
        "restructured": "RESTRUCTURED",
        "modified": "RESTRUCTURED",
        "yapilandirilmis": "RESTRUCTURED",  # Turkish
        "r": "RESTRUCTURED",        # Short code
        # Frozen / Hold states
        "frozen": "FROZEN",
        "hold": "FROZEN",
        "dondurulmus": "FROZEN",     # Turkish
        "f": "FROZEN",              # Short code
    }

    # --- Date Format Patterns ---
    DATE_FORMATS = [
        "%Y-%m-%d",       # ISO: 2024-01-15
        "%d/%m/%Y",       # European: 15/01/2024
        "%m/%d/%Y",       # US: 01/15/2024
        "%Y%m%d",         # Compact: 20240115
        "%d-%m-%Y",       # European dash: 15-01-2024
        "%d.%m.%Y",       # European dot: 15.01.2024
        "%Y/%m/%d",       # Asian: 2024/01/15
    ]

    @classmethod
    def normalize_date(cls, raw_date: str) -> Optional[str]:
        """
        Convert various date formats to ISO YYYY-MM-DD.

        Args:
            raw_date: Date string in any supported format.

        Returns:
            ISO formatted date string, or None if parsing fails.
        """
        if not raw_date or not str(raw_date).strip():
            return None

        raw_date = str(raw_date).strip()

        for fmt in cls.DATE_FORMATS:
            try:
                parsed = datetime.strptime(raw_date, fmt)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

        logger.warning(f"Could not parse date: '{raw_date}'")
        return None

    @classmethod
    def normalize_interest_rate(cls, raw_rate: str) -> Optional[float]:
        """
        Convert various interest rate formats to a standard decimal.

        Supported formats:
            - "18.5%" or "18.5 %" -> 0.185
            - "0.185" -> 0.185
            - "1850 bps" or "1850bps" -> 0.185
            - "18.5" (if > 1, treated as percentage) -> 0.185

        Args:
            raw_rate: Interest rate string in any supported format.

        Returns:
            Normalized decimal rate, or None if parsing fails.
        """
        if not raw_rate or not str(raw_rate).strip():
            return None

        raw_rate = str(raw_rate).strip()

        # Case 1: Basis points (e.g., "1850 bps", "1850bps")
        bps_match = re.match(r"^([\d.]+)\s*bps$", raw_rate, re.IGNORECASE)
        if bps_match:
            bps_value = float(bps_match.group(1))
            return round(bps_value / 10000, 6)

        # Case 2: Percentage (e.g., "18.5%", "18.5 %")
        pct_match = re.match(r"^([\d.]+)\s*%$", raw_rate)
        if pct_match:
            pct_value = float(pct_match.group(1))
            return round(pct_value / 100, 6)

        # Case 3: Plain number
        try:
            value = float(raw_rate)
            # If value > 1, assume it's a percentage
            if value > 1:
                return round(value / 100, 6)
            # Otherwise, it's already a decimal
            return round(value, 6)
        except ValueError:
            logger.warning(f"Could not parse interest rate: '{raw_rate}'")
            return None

    @classmethod
    def normalize_status(cls, raw_status: str) -> Optional[str]:
        """
        Standardize loan status labels into unified system codes.

        Args:
            raw_status: Status string in any language/format.

        Returns:
            Normalized status code (ACTIVE, CLOSED, DEFAULT, RESTRUCTURED, FROZEN),
            or None if mapping not found.
        """
        if not raw_status or not raw_status.strip():
            return None

        # Remove diacritics and normalize for Turkish characters
        cleaned = raw_status.strip().lower()
        cleaned = (
            cleaned
            .replace("\u0131", "i")   # dotless i -> i
            .replace("\u00e7", "c")   # c-cedilla -> c
            .replace("\u015f", "s")   # s-cedilla -> s
            .replace("\u00f6", "o")   # o-umlaut -> o
            .replace("\u00fc", "u")   # u-umlaut -> u
            .replace("\u011f", "g")   # g-breve -> g
        )

        normalized = cls.STATUS_MAP.get(cleaned)
        if normalized is None:
            logger.warning(f"Unknown status label: '{raw_status}' (cleaned: '{cleaned}')")
        return normalized

    # =========================================================================
    # Full Record Normalization
    # =========================================================================

    @classmethod
    def normalize_loan_record(cls, record: dict) -> dict:
        """
        Normalize a loan record.

        Maps external bank field names to internal names and normalizes values.
        Supports both old-style (loan_id, amount) and new-style
        (loan_account_number, original_loan_amount) field names.

        Preserves original values alongside normalized ones.
        """
        # Support both old and new field names
        loan_id = (
            record.get("loan_account_number", "")
            or record.get("loan_id", "")
        ).strip()

        amount_raw = (
            record.get("original_loan_amount", "")
            or record.get("amount", "")
        )

        rate_raw = (
            record.get("nominal_interest_rate", "")
            or record.get("interest_rate", "")
        )

        date_raw = (
            record.get("loan_start_date", "")
            or record.get("start_date", "")
        )

        status_raw = (
            record.get("loan_status_code", "")
            or record.get("status", "")
        )

        loan_type = record.get("loan_type", "").strip().upper()

        result = {
            # ── Core mapped fields ──
            "loan_id": loan_id,
            "loan_type": loan_type,
            "amount": _safe_float(amount_raw, 0.0),
            "interest_rate": cls.normalize_interest_rate(rate_raw),
            "start_date": cls.normalize_date(date_raw),
            "status": cls.normalize_status(status_raw),
            # Preserve originals for audit
            "original_interest_rate": str(rate_raw),
            "original_start_date": str(date_raw),
            "original_status": str(status_raw),

            # ── Extended fields (pass-through) ──
            "customer_id": record.get("customer_id", ""),
            "customer_type": record.get("customer_type", ""),
            "days_past_due": _safe_int(record.get("days_past_due", "0"), 0),
            "final_maturity_date": cls.normalize_date(
                record.get("final_maturity_date", "")
            ),
            "first_payment_date": cls.normalize_date(
                record.get("first_payment_date", "")
            ),
            "loan_closing_date": cls.normalize_date(
                record.get("loan_closing_date", "")
            ),

            "total_installment_count": _safe_int(record.get("total_installment_count")),
            "outstanding_installment_count": _safe_int(
                record.get("outstanding_installment_count")
            ),
            "paid_installment_count": _safe_int(record.get("paid_installment_count")),
            "installment_frequency": _safe_int(record.get("installment_frequency")),
            "grace_period_months": _safe_int(record.get("grace_period_months")),

            "outstanding_principal_balance": _safe_float(
                record.get("outstanding_principal_balance")
            ),
            "total_interest_amount": _safe_float(
                record.get("total_interest_amount")
            ),

            "kkdf_rate": _safe_float(record.get("kkdf_rate")),
            "kkdf_amount": _safe_float(record.get("kkdf_amount")),
            "bsmv_rate": _safe_float(record.get("bsmv_rate")),
            "bsmv_amount": _safe_float(record.get("bsmv_amount")),

            "insurance_included": record.get("insurance_included", ""),

            # Location
            "customer_district_code": record.get("customer_district_code", ""),
            "customer_province_code": record.get("customer_province_code", ""),
            "customer_region_code": record.get("customer_region_code", ""),

            # Ratings
            "internal_rating": record.get("internal_rating", ""),
            "external_rating": record.get("external_rating", ""),

            # Commercial extras
            "loan_product_type": record.get("loan_product_type", ""),
            "loan_status_flag": record.get("loan_status_flag", ""),
            "sector_code": record.get("sector_code", ""),
            "internal_credit_rating": record.get("internal_credit_rating", ""),
            "default_probability": _safe_float(record.get("default_probability")),
            "risk_class": record.get("risk_class", ""),
            "customer_segment": record.get("customer_segment", ""),
        }

        return result

    @classmethod
    def normalize_payment_record(cls, record: dict) -> dict:
        """
        Normalize a payment record.

        Maps external bank field names to internal names and normalizes values.
        Supports both old-style (payment_id, loan_id, payment_amount, payment_date)
        and new-style (loan_account_number, installment_number, installment_amount, etc.)

        Derives payment_id from loan_account_number + installment_number if not present.
        """
        # Support both old and new field names
        loan_id = (
            record.get("loan_account_number", "")
            or record.get("loan_id", "")
        ).strip()

        # Payment ID: use existing or derive from loan + installment
        payment_id = record.get("payment_id", "").strip()
        inst_num = record.get("installment_number", "")
        if not payment_id and loan_id and inst_num:
            payment_id = f"{loan_id}_{inst_num}"

        # Payment amount
        amount_raw = (
            record.get("installment_amount", "")
            or record.get("payment_amount", "")
        )

        # Payment date: prefer actual, fallback to scheduled, then old field
        actual_date = record.get("actual_payment_date", "")
        scheduled_date = record.get("scheduled_payment_date", "")
        old_date = record.get("payment_date", "")
        payment_date_raw = actual_date or scheduled_date or old_date

        loan_type = record.get("loan_type", "").strip().upper()

        # Installment status
        inst_status_raw = record.get("installment_status", "")

        result = {
            # ── Core mapped fields ──
            "payment_id": payment_id,
            "loan_id": loan_id,
            "loan_type": loan_type,
            "payment_amount": _safe_float(amount_raw, 0.0),
            "payment_date": cls.normalize_date(payment_date_raw),
            # Preserve original for audit
            "original_payment_date": str(payment_date_raw),

            # ── Extended fields ──
            "installment_number": _safe_int(inst_num),
            "actual_payment_date": cls.normalize_date(actual_date),
            "scheduled_payment_date": cls.normalize_date(scheduled_date),

            "principal_component": _safe_float(record.get("principal_component")),
            "interest_component": _safe_float(record.get("interest_component")),
            "kkdf_component": _safe_float(record.get("kkdf_component")),
            "bsmv_component": _safe_float(record.get("bsmv_component")),

            "installment_status": cls.normalize_status(inst_status_raw) if inst_status_raw else "",

            "remaining_principal": _safe_float(record.get("remaining_principal")),
            "remaining_interest": _safe_float(record.get("remaining_interest")),
            "remaining_kkdf": _safe_float(record.get("remaining_kkdf")),
            "remaining_bsmv": _safe_float(record.get("remaining_bsmv")),
        }

        return result


# =========================================================================
# Module-level helper functions
# =========================================================================

def _safe_float(value, default=0.0):
    """Safely convert a value to float."""
    if not value or not str(value).strip():
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _safe_int(value, default=None):
    """Safely convert a value to int."""
    if not value or not str(value).strip():
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default
