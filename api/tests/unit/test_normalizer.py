"""
Unit Tests for adapter.core.normalizer.Normalizer

Tests all normalization rules:
  - Date conversion (7+ source formats -> ISO YYYY-MM-DD)
  - Interest rate conversion (%, bps, decimal, plain number)
  - Status mapping (English, Turkish, short codes -> system codes)
  - Full record normalization for loans and payments
  - Column name mapping (new-style mock_data -> internal)
"""

import pytest
from adapter.core.normalizer import Normalizer


# =============================================================================
# Date Normalization
# =============================================================================

class TestNormalizeDate:
    """Tests for Normalizer.normalize_date()"""

    @pytest.mark.parametrize("raw,expected", [
        # ISO format (YYYY-MM-DD)
        ("2024-01-15", "2024-01-15"),
        # European format (DD/MM/YYYY)
        ("15/01/2024", "2024-01-15"),
        # US format (MM/DD/YYYY)
        ("01/15/2024", "2024-01-15"),
        # Compact format (YYYYMMDD)
        ("20240115", "2024-01-15"),
        # European dash format (DD-MM-YYYY)
        ("15-01-2024", "2024-01-15"),
        # European dot format (DD.MM.YYYY)
        ("15.01.2024", "2024-01-15"),
        # Asian format (YYYY/MM/DD)
        ("2024/01/15", "2024-01-15"),
    ])
    def test_supported_date_formats(self, raw, expected):
        """Each supported date format should normalize to ISO."""
        assert Normalizer.normalize_date(raw) == expected

    def test_date_with_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        assert Normalizer.normalize_date("  2024-01-15  ") == "2024-01-15"

    def test_empty_date_returns_none(self):
        """Empty strings and None-like values should return None."""
        assert Normalizer.normalize_date("") is None
        assert Normalizer.normalize_date("   ") is None

    def test_unparseable_date_returns_none(self):
        """Unparseable date strings should return None."""
        assert Normalizer.normalize_date("not-a-date") is None
        assert Normalizer.normalize_date("2024/13/40") is None

    def test_none_input_returns_none(self):
        """None input should return None."""
        assert Normalizer.normalize_date(None) is None

    def test_mock_data_compact_date(self):
        """Mock data uses YYYYMMDD format (e.g. 20260302)."""
        assert Normalizer.normalize_date("20260302") == "2026-03-02"
        assert Normalizer.normalize_date("20250619") == "2025-06-19"


# =============================================================================
# Interest Rate Normalization
# =============================================================================

class TestNormalizeInterestRate:
    """Tests for Normalizer.normalize_interest_rate()"""

    def test_percentage_with_sign(self):
        """'18.5%' should become 0.185"""
        assert Normalizer.normalize_interest_rate("18.5%") == pytest.approx(0.185)

    def test_percentage_with_space(self):
        """'18.5 %' should become 0.185"""
        assert Normalizer.normalize_interest_rate("18.5 %") == pytest.approx(0.185)

    def test_basis_points(self):
        """'1850 bps' should become 0.185"""
        assert Normalizer.normalize_interest_rate("1850 bps") == pytest.approx(0.185)

    def test_basis_points_no_space(self):
        """'1850bps' should become 0.185"""
        assert Normalizer.normalize_interest_rate("1850bps") == pytest.approx(0.185)

    def test_decimal_already_normalized(self):
        """'0.185' (already < 1) should stay 0.185"""
        assert Normalizer.normalize_interest_rate("0.185") == pytest.approx(0.185)

    def test_plain_number_gt_1_treated_as_percentage(self):
        """'18.5' (> 1) should be treated as 18.5% -> 0.185"""
        assert Normalizer.normalize_interest_rate("18.5") == pytest.approx(0.185)

    def test_zero_rate(self):
        """'0' or '0%' should be 0.0"""
        assert Normalizer.normalize_interest_rate("0") == pytest.approx(0.0)
        assert Normalizer.normalize_interest_rate("0%") == pytest.approx(0.0)

    def test_hundred_percent(self):
        """'100%' should become 1.0"""
        assert Normalizer.normalize_interest_rate("100%") == pytest.approx(1.0)

    def test_empty_returns_none(self):
        """Empty input should return None."""
        assert Normalizer.normalize_interest_rate("") is None
        assert Normalizer.normalize_interest_rate("   ") is None

    def test_none_input_returns_none(self):
        """None input should return None."""
        assert Normalizer.normalize_interest_rate(None) is None

    def test_unparseable_returns_none(self):
        """Completely non-numeric input should return None."""
        assert Normalizer.normalize_interest_rate("abc") is None
        assert Normalizer.normalize_interest_rate("not_a_rate") is None

    def test_mock_data_high_rate(self):
        """Mock data rates like 55.47 should be treated as 55.47% -> 0.5547"""
        assert Normalizer.normalize_interest_rate("55.47") == pytest.approx(0.5547)
        assert Normalizer.normalize_interest_rate("52.83") == pytest.approx(0.5283)


# =============================================================================
# Status Normalization
# =============================================================================

class TestNormalizeStatus:
    """Tests for Normalizer.normalize_status()"""

    # Active states
    @pytest.mark.parametrize("raw", ["Active", "ACTIVE", "open", "current", "performing"])
    def test_active_states_english(self, raw):
        assert Normalizer.normalize_status(raw) == "ACTIVE"

    @pytest.mark.parametrize("raw", ["Acik", "Aktif", "açık", "aktif"])
    def test_active_states_turkish(self, raw):
        assert Normalizer.normalize_status(raw) == "ACTIVE"

    def test_short_code_a_is_active(self):
        """Short code 'A' used in mock data should map to ACTIVE."""
        assert Normalizer.normalize_status("A") == "ACTIVE"
        assert Normalizer.normalize_status("a") == "ACTIVE"

    # Closed states
    @pytest.mark.parametrize("raw", ["Paid", "CLOSED", "settled", "completed"])
    def test_closed_states_english(self, raw):
        assert Normalizer.normalize_status(raw) == "CLOSED"

    @pytest.mark.parametrize("raw", ["Kapali", "Odendi", "Kapatildi", "kapalı", "ödendi"])
    def test_closed_states_turkish(self, raw):
        assert Normalizer.normalize_status(raw) == "CLOSED"

    def test_short_code_k_is_closed(self):
        """Short code 'K' used in mock data should map to CLOSED."""
        assert Normalizer.normalize_status("K") == "CLOSED"
        assert Normalizer.normalize_status("k") == "CLOSED"

    # Default/Delinquent states
    @pytest.mark.parametrize("raw", ["Default", "delinquent", "non-performing", "NPL"])
    def test_default_states_english(self, raw):
        assert Normalizer.normalize_status(raw) == "DEFAULT"

    @pytest.mark.parametrize("raw", ["Gecikme", "Takip", "gecikme"])
    def test_default_states_turkish(self, raw):
        assert Normalizer.normalize_status(raw) == "DEFAULT"

    def test_short_code_d_and_t(self):
        """Short codes 'D' and 'T' should map to DEFAULT."""
        assert Normalizer.normalize_status("D") == "DEFAULT"
        assert Normalizer.normalize_status("T") == "DEFAULT"

    # Restructured states
    @pytest.mark.parametrize("raw", ["Restructured", "modified"])
    def test_restructured_states_english(self, raw):
        assert Normalizer.normalize_status(raw) == "RESTRUCTURED"

    @pytest.mark.parametrize("raw", ["Yapilandirilmis", "yapılandırılmış"])
    def test_restructured_states_turkish(self, raw):
        assert Normalizer.normalize_status(raw) == "RESTRUCTURED"

    def test_unknown_status_returns_none(self):
        """Unrecognized status should return None."""
        assert Normalizer.normalize_status("UNKNOWN_STATUS") is None
        assert Normalizer.normalize_status("something_random") is None

    def test_empty_status_returns_none(self):
        assert Normalizer.normalize_status("") is None
        assert Normalizer.normalize_status("   ") is None

    def test_none_input_returns_none(self):
        assert Normalizer.normalize_status(None) is None


# =============================================================================
# Full Record Normalization — OLD-STYLE
# =============================================================================

class TestNormalizeLoanRecord:
    """Tests for Normalizer.normalize_loan_record() with old-style field names."""

    def test_complete_record(self):
        """A complete loan record should be fully normalized."""
        record = {
            "loan_id": " L001 ",
            "loan_type": " retail ",
            "amount": "50000",
            "interest_rate": "18.5%",
            "start_date": "15/01/2024",
            "status": "Active",
        }
        result = Normalizer.normalize_loan_record(record)

        assert result["loan_id"] == "L001"
        assert result["loan_type"] == "RETAIL"
        assert result["amount"] == 50000.0
        assert result["interest_rate"] == pytest.approx(0.185)
        assert result["start_date"] == "2024-01-15"
        assert result["status"] == "ACTIVE"
        # Originals preserved
        assert result["original_interest_rate"] == "18.5%"
        assert result["original_start_date"] == "15/01/2024"
        assert result["original_status"] == "Active"

    def test_missing_optional_fields_handled(self):
        """Missing fields should not raise errors."""
        record = {}
        result = Normalizer.normalize_loan_record(record)
        assert result["loan_id"] == ""
        assert result["interest_rate"] is None
        assert result["start_date"] is None
        assert result["status"] is None


class TestNormalizePaymentRecord:
    """Tests for Normalizer.normalize_payment_record() with old-style field names."""

    def test_complete_payment(self):
        """A complete payment record should be fully normalized."""
        record = {
            "payment_id": " P001 ",
            "loan_id": " L001 ",
            "loan_type": " retail ",
            "payment_amount": "5000.50",
            "payment_date": "15/02/2024",
        }
        result = Normalizer.normalize_payment_record(record)

        assert result["payment_id"] == "P001"
        assert result["loan_id"] == "L001"
        assert result["loan_type"] == "RETAIL"
        assert result["payment_amount"] == 5000.50
        assert result["payment_date"] == "2024-02-15"
        assert result["original_payment_date"] == "15/02/2024"


# =============================================================================
# Full Record Normalization — NEW-STYLE (mock_data format)
# =============================================================================

class TestNormalizeNewStyleLoanRecord:
    """Tests for Normalizer.normalize_loan_record() with mock_data field names."""

    def test_retail_loan_new_style(self, valid_retail_loan_records):
        """Retail loan with mock_data field names should be mapped correctly."""
        record = valid_retail_loan_records[0]
        record["loan_type"] = "RETAIL"
        result = Normalizer.normalize_loan_record(record)

        # Core mapped fields
        assert result["loan_id"] == "LOAN_000001"
        assert result["loan_type"] == "RETAIL"
        assert result["amount"] == 98940.0
        assert result["interest_rate"] == pytest.approx(0.5547)
        assert result["start_date"] == "2025-03-02"
        assert result["status"] == "ACTIVE"  # "A" -> ACTIVE

        # Extended fields
        assert result["customer_id"] == "CUST_00001"
        assert result["customer_type"] == "I"
        assert result["days_past_due"] == 0
        assert result["final_maturity_date"] == "2026-03-02"
        assert result["outstanding_principal_balance"] == 88600.0
        assert result["total_interest_amount"] == 785.08
        assert result["kkdf_rate"] == 15.14
        assert result["kkdf_amount"] == 113.73
        assert result["bsmv_rate"] == 15.27
        assert result["bsmv_amount"] == 112.31
        assert result["insurance_included"] == "H"
        assert result["customer_district_code"] == "DISTRICT_B"
        assert result["internal_rating"] == "2"
        assert result["external_rating"] == "1366"

    def test_commercial_loan_new_style(self, valid_commercial_loan_records):
        """Commercial loan with mock_data field names should include commercial-only fields."""
        record = valid_commercial_loan_records[0]
        record["loan_type"] = "COMMERCIAL"
        result = Normalizer.normalize_loan_record(record)

        assert result["loan_id"] == "LOAN_COM_001"
        assert result["loan_type"] == "COMMERCIAL"
        assert result["loan_product_type"] == "4"
        assert result["loan_status_flag"] == "A"
        assert result["customer_region_code"] == "REGION_1"
        assert result["sector_code"] == "4"
        assert result["default_probability"] == pytest.approx(0.0217)
        assert result["risk_class"] == "1"
        assert result["customer_segment"] == "1"


class TestNormalizeNewStylePaymentRecord:
    """Tests for Normalizer.normalize_payment_record() with mock_data field names."""

    def test_payment_with_actual_date(self, valid_retail_payment_records):
        """Payment with actual_payment_date should use it as primary date."""
        record = valid_retail_payment_records[0]
        record["loan_type"] = "RETAIL"
        result = Normalizer.normalize_payment_record(record)

        assert result["payment_id"] == "LOAN_000001_1"
        assert result["loan_id"] == "LOAN_000001"
        assert result["loan_type"] == "RETAIL"
        assert result["payment_amount"] == 17790.0
        assert result["payment_date"] == "2025-02-08"  # actual date used

        # Extended fields
        assert result["installment_number"] == 1
        assert result["principal_component"] == 13640.0
        assert result["interest_component"] == 4281.23
        assert result["kkdf_component"] == 727.56
        assert result["bsmv_component"] == 651.22
        assert result["installment_status"] == "CLOSED"  # K -> CLOSED
        assert result["remaining_principal"] == 0.0

    def test_payment_without_actual_date(self, valid_retail_payment_records):
        """Payment without actual_payment_date should fallback to scheduled_payment_date."""
        record = valid_retail_payment_records[2]
        record["loan_type"] = "RETAIL"
        result = Normalizer.normalize_payment_record(record)

        assert result["payment_id"] == "LOAN_000002_1"
        assert result["payment_date"] == "2025-07-25"  # scheduled date used
        assert result["installment_status"] == "ACTIVE"  # A -> ACTIVE
        assert result["remaining_principal"] == 39350.0
