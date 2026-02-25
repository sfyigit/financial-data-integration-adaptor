"""
Data Validation Module
======================
Implements field-level and cross-file integrity validation for financial data.

Validation Rules:
- Field-Level: data type checks, mandatory field presence, range constraints
- Cross-File Integrity: every Payment must reference a valid Loan ID
- All-or-Nothing: if any critical validation fails, the entire batch is rejected

Supports both old-style field names (loan_id, amount, etc.) and new-style
field names from mock_data (loan_account_number, original_loan_amount, etc.).
"""

import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("adapter.validator")


@dataclass
class ValidationError:
    """Represents a single validation error."""
    row_number: int
    field_name: str
    error_type: str
    error_message: str
    raw_value: str = ""


@dataclass
class ValidationResult:
    """Aggregated validation result for a batch of records."""
    is_valid: bool = True
    errors: list = field(default_factory=list)
    valid_count: int = 0
    invalid_count: int = 0

    def add_error(self, error: ValidationError):
        """Add a validation error and update counters."""
        self.errors.append(error)
        self.is_valid = False

    @property
    def total_errors(self) -> int:
        return len(self.errors)


def _resolve_loan_field(record: dict, *keys) -> str:
    """Resolve a field value from multiple possible keys (new-style first)."""
    for key in keys:
        val = record.get(key, "")
        if val and str(val).strip():
            return str(val).strip()
    return ""


class Validator:
    """Handles validation of financial data records."""

    # --- Mandatory Fields ---
    # Supports both naming conventions; the resolver will check both
    REQUIRED_LOAN_FIELDS = [
        ("loan_id", "loan_account_number"),       # At least one must exist
        ("amount", "original_loan_amount"),
        ("interest_rate", "nominal_interest_rate"),
        ("start_date", "loan_start_date"),
        ("status", "loan_status_code"),
    ]
    REQUIRED_PAYMENT_FIELDS = [
        ("payment_id", "loan_account_number"),     # payment_id or derivable from loan_account_number
        ("loan_id", "loan_account_number"),
        ("payment_amount", "installment_amount"),
        ("payment_date", "actual_payment_date", "scheduled_payment_date"),
    ]

    # --- Range Constraints ---
    MIN_LOAN_AMOUNT = 0
    MAX_LOAN_AMOUNT = 100_000_000_000  # 100 billion
    MIN_INTEREST_RATE = 0.0
    MAX_INTEREST_RATE = 1.0  # 100% as decimal
    MIN_PAYMENT_AMOUNT = 0

    @classmethod
    def validate_loans(cls, records: list[dict]) -> ValidationResult:
        """
        Validate a batch of loan records.

        Performs field-level checks on every record:
        - Mandatory field presence
        - Data type verification
        - Range constraint checks

        Args:
            records: List of raw loan record dictionaries.

        Returns:
            ValidationResult with errors (if any).
        """
        result = ValidationResult()
        seen_loan_ids = set()

        for idx, record in enumerate(records, start=1):
            row_errors = cls._validate_loan_record(idx, record, seen_loan_ids)
            if row_errors:
                for err in row_errors:
                    result.add_error(err)
                result.invalid_count += 1
            else:
                result.valid_count += 1

        if result.total_errors > 0:
            logger.warning(
                f"Loan validation completed: {result.valid_count} valid, "
                f"{result.invalid_count} invalid, {result.total_errors} errors"
            )
        else:
            logger.info(f"Loan validation passed: {result.valid_count} records OK")

        return result

    @classmethod
    def validate_payments(cls, records: list[dict]) -> ValidationResult:
        """
        Validate a batch of payment records.

        Args:
            records: List of raw payment record dictionaries.

        Returns:
            ValidationResult with errors (if any).
        """
        result = ValidationResult()
        seen_payment_ids = set()

        for idx, record in enumerate(records, start=1):
            row_errors = cls._validate_payment_record(idx, record, seen_payment_ids)
            if row_errors:
                for err in row_errors:
                    result.add_error(err)
                result.invalid_count += 1
            else:
                result.valid_count += 1

        if result.total_errors > 0:
            logger.warning(
                f"Payment validation completed: {result.valid_count} valid, "
                f"{result.invalid_count} invalid, {result.total_errors} errors"
            )
        else:
            logger.info(f"Payment validation passed: {result.valid_count} records OK")

        return result

    @classmethod
    def validate_cross_file_integrity(
        cls,
        loan_records: list[dict],
        payment_records: list[dict],
    ) -> ValidationResult:
        """
        Cross-file integrity check: every payment must reference a valid loan ID.

        This is a critical validation - if orphan payments exist, the data integrity
        is compromised and the sync should be rejected.

        Args:
            loan_records: List of loan record dictionaries.
            payment_records: List of payment record dictionaries.

        Returns:
            ValidationResult with orphan payment errors (if any).
        """
        result = ValidationResult()

        # Build set of valid loan IDs (support both naming conventions)
        valid_loan_ids = set()
        for r in loan_records:
            lid = _resolve_loan_field(r, "loan_account_number", "loan_id")
            if lid:
                valid_loan_ids.add(lid)

        # Check each payment references a valid loan
        for idx, payment in enumerate(payment_records, start=1):
            payment_loan_id = _resolve_loan_field(
                payment, "loan_account_number", "loan_id"
            )
            if payment_loan_id not in valid_loan_ids:
                result.add_error(ValidationError(
                    row_number=idx,
                    field_name="loan_id",
                    error_type="orphan_reference",
                    error_message=(
                        f"Payment references non-existent loan ID '{payment_loan_id}'. "
                        f"No matching loan found in the loan dataset."
                    ),
                    raw_value=payment_loan_id,
                ))
                result.invalid_count += 1
            else:
                result.valid_count += 1

        if result.total_errors > 0:
            logger.warning(
                f"Cross-file integrity check: {result.invalid_count} orphan payments found"
            )
        else:
            logger.info(
                f"Cross-file integrity check passed: all {result.valid_count} payments "
                f"reference valid loans"
            )

        return result

    # =========================================================================
    # Private Methods - Field-Level Validation
    # =========================================================================

    @classmethod
    def _validate_loan_record(
        cls, row_num: int, record: dict, seen_ids: set
    ) -> list[ValidationError]:
        """Validate a single loan record. Returns list of errors."""
        errors = []

        # Check mandatory fields (support both naming conventions)
        for field_keys in cls.REQUIRED_LOAN_FIELDS:
            value = _resolve_loan_field(record, *field_keys)
            if not value:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name=field_keys[0],  # Use primary name for error
                    error_type="missing_required",
                    error_message=f"Required field '{field_keys[0]}' is missing or empty.",
                    raw_value="",
                ))

        # Validate loan_id uniqueness
        loan_id = _resolve_loan_field(record, "loan_account_number", "loan_id")
        if loan_id:
            if loan_id in seen_ids:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="loan_id",
                    error_type="duplicate",
                    error_message=f"Duplicate loan ID '{loan_id}'.",
                    raw_value=loan_id,
                ))
            seen_ids.add(loan_id)

        # Validate amount (must be numeric and within range)
        amount_str = _resolve_loan_field(record, "original_loan_amount", "amount")
        if amount_str:
            try:
                amount = float(amount_str)
                if amount <= cls.MIN_LOAN_AMOUNT:
                    errors.append(ValidationError(
                        row_number=row_num,
                        field_name="amount",
                        error_type="range_violation",
                        error_message=f"Loan amount must be positive, got {amount}.",
                        raw_value=str(amount_str),
                    ))
                elif amount > cls.MAX_LOAN_AMOUNT:
                    errors.append(ValidationError(
                        row_number=row_num,
                        field_name="amount",
                        error_type="range_violation",
                        error_message=f"Loan amount exceeds maximum ({cls.MAX_LOAN_AMOUNT}).",
                        raw_value=str(amount_str),
                    ))
            except (ValueError, TypeError):
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="amount",
                    error_type="invalid_type",
                    error_message=f"Amount must be numeric, got '{amount_str}'.",
                    raw_value=str(amount_str),
                ))

        # Validate interest rate is parseable
        rate_str = _resolve_loan_field(record, "nominal_interest_rate", "interest_rate")
        if rate_str:
            from adapter.core.normalizer import Normalizer
            normalized_rate = Normalizer.normalize_interest_rate(rate_str)
            if normalized_rate is None:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="interest_rate",
                    error_type="invalid_format",
                    error_message=f"Cannot parse interest rate '{rate_str}'.",
                    raw_value=str(rate_str),
                ))
            elif normalized_rate < cls.MIN_INTEREST_RATE or normalized_rate > cls.MAX_INTEREST_RATE:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="interest_rate",
                    error_type="range_violation",
                    error_message=(
                        f"Interest rate {normalized_rate} is outside valid range "
                        f"[{cls.MIN_INTEREST_RATE}, {cls.MAX_INTEREST_RATE}]."
                    ),
                    raw_value=str(rate_str),
                ))

        # Validate date is parseable
        date_str = _resolve_loan_field(record, "loan_start_date", "start_date")
        if date_str:
            from adapter.core.normalizer import Normalizer
            normalized_date = Normalizer.normalize_date(date_str)
            if normalized_date is None:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="start_date",
                    error_type="invalid_format",
                    error_message=f"Cannot parse date '{date_str}'.",
                    raw_value=str(date_str),
                ))

        # Validate status is recognizable
        status_str = _resolve_loan_field(record, "loan_status_code", "status")
        if status_str:
            from adapter.core.normalizer import Normalizer
            normalized_status = Normalizer.normalize_status(status_str)
            if normalized_status is None:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="status",
                    error_type="invalid_value",
                    error_message=f"Unrecognized status label '{status_str}'.",
                    raw_value=str(status_str),
                ))

        return errors

    @classmethod
    def _validate_payment_record(
        cls, row_num: int, record: dict, seen_ids: set
    ) -> list[ValidationError]:
        """Validate a single payment record. Returns list of errors."""
        errors = []

        # Check mandatory fields (support both naming conventions)
        for field_keys in cls.REQUIRED_PAYMENT_FIELDS:
            value = _resolve_loan_field(record, *field_keys)
            if not value:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name=field_keys[0],
                    error_type="missing_required",
                    error_message=f"Required field '{field_keys[0]}' is missing or empty.",
                    raw_value="",
                ))

        # Validate payment_id uniqueness
        # Derive payment_id if not present
        payment_id = record.get("payment_id", "").strip()
        if not payment_id:
            loan_acct = record.get("loan_account_number", "").strip()
            inst_num = record.get("installment_number", "").strip()
            if loan_acct and inst_num:
                payment_id = f"{loan_acct}_{inst_num}"

        if payment_id:
            if payment_id in seen_ids:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="payment_id",
                    error_type="duplicate",
                    error_message=f"Duplicate payment ID '{payment_id}'.",
                    raw_value=payment_id,
                ))
            seen_ids.add(payment_id)

        # Validate payment_amount (must be numeric and positive)
        amount_str = _resolve_loan_field(record, "installment_amount", "payment_amount")
        if amount_str:
            try:
                amount = float(amount_str)
                if amount <= cls.MIN_PAYMENT_AMOUNT:
                    errors.append(ValidationError(
                        row_number=row_num,
                        field_name="payment_amount",
                        error_type="range_violation",
                        error_message=f"Payment amount must be positive, got {amount}.",
                        raw_value=str(amount_str),
                    ))
            except (ValueError, TypeError):
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="payment_amount",
                    error_type="invalid_type",
                    error_message=f"Payment amount must be numeric, got '{amount_str}'.",
                    raw_value=str(amount_str),
                ))

        # Validate payment_date is parseable
        date_str = _resolve_loan_field(
            record, "actual_payment_date", "scheduled_payment_date", "payment_date"
        )
        if date_str:
            from adapter.core.normalizer import Normalizer
            normalized_date = Normalizer.normalize_date(date_str)
            if normalized_date is None:
                errors.append(ValidationError(
                    row_number=row_num,
                    field_name="payment_date",
                    error_type="invalid_format",
                    error_message=f"Cannot parse payment date '{date_str}'.",
                    raw_value=str(date_str),
                ))

        return errors
