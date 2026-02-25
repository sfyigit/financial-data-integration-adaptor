"""
Unit Tests for adapter.core.validator.Validator

Tests validation rules:
  - Required field presence (both old-style and new-style field names)
  - Duplicate ID detection
  - Amount range constraints
  - Interest rate format validation
  - Date format validation
  - Status recognition (including short codes A, K)
  - Payment field validation
  - Cross-file integrity (orphan payment detection)
  - All-or-nothing batch semantics
"""

import pytest
from adapter.core.validator import Validator, ValidationResult, ValidationError


# =============================================================================
# Loan Validation — Field-Level (old-style)
# =============================================================================

class TestValidateLoans:
    """Tests for Validator.validate_loans()"""

    def test_all_valid_loans(self, valid_loan_records):
        """All valid records should produce a passing result."""
        result = Validator.validate_loans(valid_loan_records)
        assert result.is_valid is True
        assert result.valid_count == 3
        assert result.invalid_count == 0
        assert result.total_errors == 0

    def test_missing_required_field(self):
        """A loan missing a required field should be flagged."""
        records = [
            {
                # Missing loan_id
                "amount": "50000",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        assert result.invalid_count == 1
        error_fields = [e.field_name for e in result.errors]
        assert "loan_id" in error_fields

    def test_multiple_missing_fields(self):
        """A loan missing multiple fields should produce multiple errors."""
        records = [{"loan_id": "L_EMPTY"}]  # Missing amount, rate, date, status
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        error_fields = {e.field_name for e in result.errors}
        assert "amount" in error_fields
        assert "interest_rate" in error_fields
        assert "start_date" in error_fields
        assert "status" in error_fields

    def test_duplicate_loan_id(self):
        """Duplicate loan IDs in the same batch should be flagged."""
        records = [
            {
                "loan_id": "L_DUP",
                "amount": "50000",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
            {
                "loan_id": "L_DUP",  # Duplicate
                "amount": "60000",
                "interest_rate": "12%",
                "start_date": "2024-02-15",
                "status": "Active",
            },
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        dup_errors = [e for e in result.errors if e.error_type == "duplicate"]
        assert len(dup_errors) == 1
        assert dup_errors[0].field_name == "loan_id"

    def test_negative_amount(self):
        """Negative loan amount should fail range validation."""
        records = [
            {
                "loan_id": "L_NEG",
                "amount": "-5000",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        range_errors = [e for e in result.errors if e.error_type == "range_violation"]
        assert any(e.field_name == "amount" for e in range_errors)

    def test_zero_amount(self):
        """Zero loan amount should fail range validation (must be positive)."""
        records = [
            {
                "loan_id": "L_ZERO",
                "amount": "0",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False

    def test_exceeds_max_amount(self):
        """Amount exceeding MAX_LOAN_AMOUNT should fail."""
        records = [
            {
                "loan_id": "L_MAX",
                "amount": "999999999999",  # > 100 billion
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        range_errors = [e for e in result.errors if e.error_type == "range_violation"]
        assert any(e.field_name == "amount" for e in range_errors)

    def test_non_numeric_amount(self):
        """Non-numeric amount should fail type validation."""
        records = [
            {
                "loan_id": "L_TEXT",
                "amount": "not_a_number",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        type_errors = [e for e in result.errors if e.error_type == "invalid_type"]
        assert any(e.field_name == "amount" for e in type_errors)

    def test_unparseable_interest_rate(self):
        """Unparseable interest rate should produce an error."""
        records = [
            {
                "loan_id": "L_RATE",
                "amount": "50000",
                "interest_rate": "not_a_rate",
                "start_date": "2024-01-15",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        format_errors = [e for e in result.errors if e.error_type == "invalid_format"]
        assert any(e.field_name == "interest_rate" for e in format_errors)

    def test_unparseable_date(self):
        """Unparseable date should produce an error."""
        records = [
            {
                "loan_id": "L_DATE",
                "amount": "50000",
                "interest_rate": "18.5%",
                "start_date": "not-a-date",
                "status": "Active",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        format_errors = [e for e in result.errors if e.error_type == "invalid_format"]
        assert any(e.field_name == "start_date" for e in format_errors)

    def test_unrecognized_status(self):
        """Unrecognized status label should produce an error."""
        records = [
            {
                "loan_id": "L_STATUS",
                "amount": "50000",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "UNKNOWN_STATUS",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        value_errors = [e for e in result.errors if e.error_type == "invalid_value"]
        assert any(e.field_name == "status" for e in value_errors)

    def test_mixed_valid_and_invalid(self):
        """A batch with both valid and invalid records should be invalid overall."""
        records = [
            {
                "loan_id": "L_OK",
                "amount": "50000",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
            {
                "loan_id": "L_BAD",
                "amount": "-5000",
                "interest_rate": "18.5%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        assert result.valid_count == 1
        assert result.invalid_count == 1

    def test_empty_batch(self):
        """Empty batch should be valid with zero counts."""
        result = Validator.validate_loans([])
        assert result.is_valid is True
        assert result.valid_count == 0
        assert result.invalid_count == 0


# =============================================================================
# Loan Validation — NEW-STYLE (mock_data field names)
# =============================================================================

class TestValidateLoansNewStyle:
    """Tests for Validator.validate_loans() with mock_data field names."""

    def test_valid_retail_loans(self, valid_retail_loan_records):
        """Retail loans with new-style field names should pass validation."""
        result = Validator.validate_loans(valid_retail_loan_records)
        assert result.is_valid is True
        assert result.valid_count == 2

    def test_valid_commercial_loans(self, valid_commercial_loan_records):
        """Commercial loans with new-style field names should pass validation."""
        result = Validator.validate_loans(valid_commercial_loan_records)
        assert result.is_valid is True
        assert result.valid_count == 1

    def test_missing_loan_account_number(self):
        """Missing loan_account_number should be flagged."""
        records = [
            {
                "original_loan_amount": "50000",
                "nominal_interest_rate": "55.47",
                "loan_start_date": "20250302",
                "loan_status_code": "A",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        error_fields = [e.field_name for e in result.errors]
        assert "loan_id" in error_fields

    def test_duplicate_loan_account_number(self):
        """Duplicate loan_account_numbers should be flagged."""
        records = [
            {
                "loan_account_number": "LOAN_DUP",
                "original_loan_amount": "50000",
                "nominal_interest_rate": "55.47",
                "loan_start_date": "20250302",
                "loan_status_code": "A",
            },
            {
                "loan_account_number": "LOAN_DUP",
                "original_loan_amount": "60000",
                "nominal_interest_rate": "45.00",
                "loan_start_date": "20250401",
                "loan_status_code": "A",
            },
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False
        dup_errors = [e for e in result.errors if e.error_type == "duplicate"]
        assert len(dup_errors) == 1

    def test_status_code_a_is_valid(self):
        """Short status code 'A' should be recognized as valid."""
        records = [
            {
                "loan_account_number": "LOAN_A",
                "original_loan_amount": "50000",
                "nominal_interest_rate": "55.47",
                "loan_start_date": "20250302",
                "loan_status_code": "A",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is True

    def test_status_code_k_is_valid(self):
        """Short status code 'K' should be recognized as valid."""
        records = [
            {
                "loan_account_number": "LOAN_K",
                "original_loan_amount": "50000",
                "nominal_interest_rate": "55.47",
                "loan_start_date": "20250302",
                "loan_status_code": "K",
            }
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is True


# =============================================================================
# Payment Validation — Field-Level (old-style)
# =============================================================================

class TestValidatePayments:
    """Tests for Validator.validate_payments()"""

    def test_all_valid_payments(self, valid_payment_records):
        """All valid payments should produce a passing result."""
        result = Validator.validate_payments(valid_payment_records)
        assert result.is_valid is True
        assert result.valid_count == 2
        assert result.invalid_count == 0

    def test_missing_required_payment_fields(self):
        """Missing required payment fields should be flagged."""
        records = [
            {
                # Missing payment_id, loan_id, payment_amount, payment_date
            }
        ]
        result = Validator.validate_payments(records)
        assert result.is_valid is False
        error_fields = {e.field_name for e in result.errors}
        assert "payment_id" in error_fields
        assert "loan_id" in error_fields
        assert "payment_amount" in error_fields
        assert "payment_date" in error_fields

    def test_duplicate_payment_id(self):
        """Duplicate payment IDs should be flagged."""
        records = [
            {
                "payment_id": "P_DUP",
                "loan_id": "L001",
                "payment_amount": "5000",
                "payment_date": "2024-01-15",
            },
            {
                "payment_id": "P_DUP",  # Duplicate
                "loan_id": "L002",
                "payment_amount": "6000",
                "payment_date": "2024-02-15",
            },
        ]
        result = Validator.validate_payments(records)
        assert result.is_valid is False
        dup_errors = [e for e in result.errors if e.error_type == "duplicate"]
        assert len(dup_errors) == 1

    def test_negative_payment_amount(self):
        """Negative payment amount should fail."""
        records = [
            {
                "payment_id": "P_NEG",
                "loan_id": "L001",
                "payment_amount": "-500",
                "payment_date": "2024-01-15",
            }
        ]
        result = Validator.validate_payments(records)
        assert result.is_valid is False

    def test_non_numeric_payment_amount(self):
        """Non-numeric payment amount should fail."""
        records = [
            {
                "payment_id": "P_TEXT",
                "loan_id": "L001",
                "payment_amount": "abc",
                "payment_date": "2024-01-15",
            }
        ]
        result = Validator.validate_payments(records)
        assert result.is_valid is False

    def test_unparseable_payment_date(self):
        """Unparseable payment date should produce an error."""
        records = [
            {
                "payment_id": "P_DATE",
                "loan_id": "L001",
                "payment_amount": "5000",
                "payment_date": "garbage-date",
            }
        ]
        result = Validator.validate_payments(records)
        assert result.is_valid is False


# =============================================================================
# Payment Validation — NEW-STYLE (mock_data field names)
# =============================================================================

class TestValidatePaymentsNewStyle:
    """Tests for Validator.validate_payments() with mock_data field names."""

    def test_valid_retail_payments(self, valid_retail_payment_records):
        """Retail payments with new-style field names should pass."""
        result = Validator.validate_payments(valid_retail_payment_records)
        assert result.is_valid is True
        assert result.valid_count == 3

    def test_duplicate_derived_payment_id(self):
        """Duplicate derived payment IDs (same loan + installment) should be flagged."""
        records = [
            {
                "loan_account_number": "LOAN_001",
                "installment_number": "1",
                "installment_amount": "5000",
                "scheduled_payment_date": "2025-01-15",
            },
            {
                "loan_account_number": "LOAN_001",
                "installment_number": "1",  # Same -> duplicate payment_id
                "installment_amount": "6000",
                "scheduled_payment_date": "2025-02-15",
            },
        ]
        result = Validator.validate_payments(records)
        assert result.is_valid is False
        dup_errors = [e for e in result.errors if e.error_type == "duplicate"]
        assert len(dup_errors) == 1


# =============================================================================
# Cross-File Integrity
# =============================================================================

class TestCrossFileIntegrity:
    """Tests for Validator.validate_cross_file_integrity()"""

    def test_all_payments_reference_valid_loans(self):
        """When all payments reference existing loans, validation passes."""
        loans = [
            {"loan_id": "L001"},
            {"loan_id": "L002"},
        ]
        payments = [
            {"loan_id": "L001", "payment_id": "P001"},
            {"loan_id": "L002", "payment_id": "P002"},
        ]
        result = Validator.validate_cross_file_integrity(loans, payments)
        assert result.is_valid is True
        assert result.valid_count == 2
        assert result.invalid_count == 0

    def test_orphan_payment_detected(self):
        """Payments referencing non-existent loans should be flagged."""
        loans = [{"loan_id": "L001"}]
        payments = [
            {"loan_id": "L001", "payment_id": "P001"},  # Valid
            {"loan_id": "L999", "payment_id": "P002"},  # Orphan
        ]
        result = Validator.validate_cross_file_integrity(loans, payments)
        assert result.is_valid is False
        assert result.invalid_count == 1
        assert result.valid_count == 1
        orphan_errors = [e for e in result.errors if e.error_type == "orphan_reference"]
        assert len(orphan_errors) == 1
        assert "L999" in orphan_errors[0].error_message

    def test_all_orphan_payments(self):
        """When all payments are orphans, all should be flagged."""
        loans = [{"loan_id": "L001"}]
        payments = [
            {"loan_id": "LXXX", "payment_id": "P001"},
            {"loan_id": "LYYY", "payment_id": "P002"},
        ]
        result = Validator.validate_cross_file_integrity(loans, payments)
        assert result.is_valid is False
        assert result.invalid_count == 2

    def test_no_payments(self):
        """Empty payment list should pass (nothing to validate)."""
        loans = [{"loan_id": "L001"}]
        result = Validator.validate_cross_file_integrity(loans, [])
        assert result.is_valid is True

    def test_no_loans(self):
        """All payments become orphans if loan list is empty."""
        payments = [{"loan_id": "L001", "payment_id": "P001"}]
        result = Validator.validate_cross_file_integrity([], payments)
        assert result.is_valid is False
        assert result.invalid_count == 1

    def test_cross_file_new_style_field_names(self):
        """Cross-file check should work with loan_account_number field name."""
        loans = [
            {"loan_account_number": "LOAN_000001"},
            {"loan_account_number": "LOAN_000002"},
        ]
        payments = [
            {"loan_account_number": "LOAN_000001", "installment_number": "1"},
            {"loan_account_number": "LOAN_000002", "installment_number": "1"},
            {"loan_account_number": "LOAN_999999", "installment_number": "1"},  # Orphan
        ]
        result = Validator.validate_cross_file_integrity(loans, payments)
        assert result.is_valid is False
        assert result.valid_count == 2
        assert result.invalid_count == 1


# =============================================================================
# ValidationResult Data Class
# =============================================================================

class TestValidationResult:
    """Tests for the ValidationResult data class."""

    def test_initial_state(self):
        """Fresh result should be valid with zero counts."""
        result = ValidationResult()
        assert result.is_valid is True
        assert result.valid_count == 0
        assert result.invalid_count == 0
        assert result.total_errors == 0
        assert result.errors == []

    def test_add_error_marks_invalid(self):
        """Adding an error should set is_valid to False."""
        result = ValidationResult()
        result.add_error(ValidationError(
            row_number=1,
            field_name="test",
            error_type="test_error",
            error_message="test message",
        ))
        assert result.is_valid is False
        assert result.total_errors == 1
