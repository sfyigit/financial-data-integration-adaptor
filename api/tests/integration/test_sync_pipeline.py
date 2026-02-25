"""
Integration Tests for the Sync Pipeline (Consistency & Resilience).

Covers:
  - Consistency: uploading 1000 loans then 2000 loans results in exactly 2000
    (replacement semantics, not append)
  - Resilience: failed sync (validation errors, orphan payments) must
    preserve existing data (all-or-nothing policy)

Supports both old-style and new-style (mock_data) field names.

NOTE: These tests mock the External Bank API and ClickHouse calls to run
without external dependencies. They exercise the SyncService orchestration
logic, Validator, and Normalizer integration end-to-end.
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from adapter.sync_service import SyncService
from adapter.core.validator import Validator
from adapter.core.normalizer import Normalizer
from tenants.models import Tenant, SyncState, SyncLog, ValidationErrorLog


# =============================================================================
# Helper: generate N valid loan records (old-style)
# =============================================================================

def _make_loans(n, loan_type="RETAIL"):
    """Generate n valid loan records (old-style field names)."""
    return [
        {
            "loan_id": f"L{str(i).zfill(6)}",
            "loan_type": loan_type,
            "amount": str(50000 + i),
            "interest_rate": "12.5%",
            "start_date": "2024-01-15",
            "status": "Active",
        }
        for i in range(1, n + 1)
    ]


def _make_payments(n, loan_ids, loan_type="RETAIL"):
    """Generate n valid payment records referencing given loan IDs (old-style)."""
    return [
        {
            "payment_id": f"P{str(i).zfill(6)}",
            "loan_id": loan_ids[i % len(loan_ids)],
            "loan_type": loan_type,
            "payment_amount": str(1000 + i),
            "payment_date": "2024-02-15",
        }
        for i in range(1, n + 1)
    ]


# =============================================================================
# Helper: generate N valid loan records (new-style / mock_data format)
# =============================================================================

def _make_loans_new_style(n, loan_type="RETAIL"):
    """Generate n valid loan records using mock_data field names."""
    return [
        {
            "loan_account_number": f"LOAN_{str(i).zfill(6)}",
            "loan_type": loan_type,
            "customer_id": f"CUST_{str(i).zfill(5)}",
            "customer_type": "I",
            "loan_status_code": "A",
            "days_past_due": "0",
            "final_maturity_date": "20260302",
            "total_installment_count": "10",
            "outstanding_installment_count": "7",
            "paid_installment_count": "3",
            "first_payment_date": "20250402",
            "original_loan_amount": str(50000 + i),
            "outstanding_principal_balance": str(40000 + i),
            "nominal_interest_rate": "55.47",
            "total_interest_amount": "785.08",
            "kkdf_rate": "15.14",
            "kkdf_amount": "113.73",
            "bsmv_rate": "15.27",
            "bsmv_amount": "112.31",
            "grace_period_months": "0",
            "installment_frequency": "1",
            "loan_start_date": "20250302",
            "loan_closing_date": "",
            "insurance_included": "H",
            "customer_district_code": "DISTRICT_B",
            "customer_province_code": "PROVINCE_1",
            "internal_rating": "2",
            "external_rating": "1366",
        }
        for i in range(1, n + 1)
    ]


def _make_payments_new_style(n, loan_ids, loan_type="RETAIL"):
    """Generate n valid payment records using mock_data field names."""
    return [
        {
            "loan_account_number": loan_ids[i % len(loan_ids)],
            "loan_type": loan_type,
            "installment_number": str((i % 10) + 1),
            "actual_payment_date": "20250208",
            "scheduled_payment_date": "2025-02-08",
            "installment_amount": str(1000 + i),
            "principal_component": str(800 + i),
            "interest_component": str(200),
            "kkdf_component": "50",
            "bsmv_component": "30",
            "installment_status": "K",
            "remaining_principal": "0",
            "remaining_interest": "0",
            "remaining_kkdf": "0",
            "remaining_bsmv": "0",
        }
        for i in range(1, n + 1)
    ]


# =============================================================================
# Consistency Test: Full Replacement Semantics
# =============================================================================

class TestConsistencyReplacement:
    """
    Verify that the sync pipeline uses REPLACEMENT (not APPEND) semantics.
    If we first sync 1000 loans, then sync 2000 loans, the data warehouse
    should contain exactly 2000 records — not 3000.
    """

    def test_validator_accepts_both_batches(self):
        """
        Simulated scenario: validate 1000 records, then 2000 records.
        Both should pass validation independently.
        """
        batch_1000 = _make_loans(1000)
        batch_2000 = _make_loans(2000)

        result_1000 = Validator.validate_loans(batch_1000)
        result_2000 = Validator.validate_loans(batch_2000)

        assert result_1000.is_valid is True
        assert result_1000.valid_count == 1000

        assert result_2000.is_valid is True
        assert result_2000.valid_count == 2000

    def test_validator_accepts_new_style_batch(self):
        """New-style (mock_data) loan records should pass validation."""
        batch = _make_loans_new_style(500)
        result = Validator.validate_loans(batch)
        assert result.is_valid is True
        assert result.valid_count == 500

    def test_normalizer_handles_large_batch(self):
        """Normalizer should handle a large batch without error."""
        batch = _make_loans(2000)
        normalized = [Normalizer.normalize_loan_record(r) for r in batch]
        assert len(normalized) == 2000
        # Verify first and last records
        assert normalized[0]["loan_id"] == "L000001"
        assert normalized[0]["interest_rate"] == pytest.approx(0.125)
        assert normalized[-1]["loan_id"] == "L002000"

    def test_normalizer_handles_new_style_batch(self):
        """Normalizer should handle new-style field names."""
        batch = _make_loans_new_style(100)
        normalized = [Normalizer.normalize_loan_record(r) for r in batch]
        assert len(normalized) == 100
        assert normalized[0]["loan_id"] == "LOAN_000001"
        assert normalized[0]["interest_rate"] == pytest.approx(0.5547)
        assert normalized[0]["status"] == "ACTIVE"
        assert normalized[0]["customer_id"] == "CUST_00001"
        assert normalized[0]["kkdf_rate"] == 15.14

    @patch("adapter.sync_service.ClickHouseClient")
    @patch("adapter.sync_service.requests.get")
    def test_sync_replaces_data_not_appends(
        self, mock_get, MockCHClient, db, tenant_bank1
    ):
        """
        SyncService._sync_single should call swap_staging_to_production,
        which atomically replaces existing data. We verify:
          1. The staging table is created
          2. Data is loaded
          3. Atomic swap is called (delete old + insert new)
        """
        # Mock ClickHouse client
        ch_instance = MockCHClient.return_value
        ch_instance.initialize.return_value = None
        ch_instance.create_staging_table.return_value = None
        ch_instance.load_loans_to_staging.return_value = None
        ch_instance.swap_staging_to_production.return_value = None
        ch_instance.compute_loan_profile.return_value = {}
        ch_instance.drop_staging_table.return_value = None

        # Mock External Bank API — return 2000 records
        batch_2000 = _make_loans(2000)
        mock_data_response = MagicMock()
        mock_data_response.status_code = 200
        mock_data_response.json.return_value = {
            "data": batch_2000,
            "total_records": 2000,
        }
        mock_data_response.raise_for_status.return_value = None

        mock_version_response = MagicMock()
        mock_version_response.status_code = 200
        mock_version_response.json.return_value = {
            "versions": [
                {"file_type": "loans", "loan_type": "RETAIL", "version": 2}
            ]
        }
        mock_version_response.raise_for_status.return_value = None

        # Route different URLs to different responses
        def side_effect(url, **kwargs):
            if "/version" in url:
                return mock_version_response
            return mock_data_response
        mock_get.side_effect = side_effect

        service = SyncService()
        result = service.sync_tenant("BANK001", file_type="loans", loan_type="RETAIL")

        # Verify the atomic swap was called (replacement, not append)
        ch_instance.swap_staging_to_production.assert_called_once_with("loans", "BANK001")

        # Verify 2000 records were loaded
        ch_instance.load_loans_to_staging.assert_called_once()
        loaded_records = ch_instance.load_loans_to_staging.call_args[0][1]
        assert len(loaded_records) == 2000

        # Verify sync log records success
        assert result["status"] == "completed"
        sync_result = result["results"][0]
        assert sync_result["status"] == "success"
        assert sync_result["records_synced"] == 2000


# =============================================================================
# Resilience Test: Invalid Data Preserves Existing
# =============================================================================

class TestResiliencePreservation:
    """
    Verify that when validation fails, existing data in the warehouse
    is NOT modified — the all-or-nothing policy is enforced.
    """

    def test_invalid_loan_batch_entirely_rejected(self):
        """
        A batch with some invalid records should mark the overall
        result as invalid (all-or-nothing).
        """
        records = [
            # Valid record
            {
                "loan_id": "L001",
                "amount": "50000",
                "interest_rate": "12%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
            # Invalid record (negative amount)
            {
                "loan_id": "L002",
                "amount": "-5000",
                "interest_rate": "12%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
        ]
        result = Validator.validate_loans(records)
        assert result.is_valid is False  # Entire batch is invalid
        assert result.valid_count == 1
        assert result.invalid_count == 1

    def test_orphan_payments_rejected(self):
        """
        Payments referencing non-existent loans should be caught by
        cross-file integrity check and reject the batch.
        """
        loans = [{"loan_id": "L001"}, {"loan_id": "L002"}]
        payments = [
            {"loan_id": "L001", "payment_id": "P001"},
            {"loan_id": "L999", "payment_id": "P002"},  # Orphan
        ]
        result = Validator.validate_cross_file_integrity(loans, payments)
        assert result.is_valid is False
        assert result.invalid_count == 1

    @patch("adapter.sync_service.ClickHouseClient")
    @patch("adapter.sync_service.requests.get")
    def test_failed_validation_preserves_existing_data(
        self, mock_get, MockCHClient, db, tenant_bank1
    ):
        """
        When the External Bank returns invalid data, SyncService should:
        1. NOT load any data into ClickHouse staging
        2. NOT perform an atomic swap
        3. Log the failure with validation errors
        4. Preserve the existing version number in SyncState
        """
        ch_instance = MockCHClient.return_value
        ch_instance.initialize.return_value = None

        # Create an existing sync state (data already exists)
        sync_state = SyncState.objects.create(
            tenant=tenant_bank1,
            file_type="loans",
            loan_type="RETAIL",
            last_version=5,
        )

        # Mock External Bank API — return invalid records
        invalid_records = [
            {
                "loan_id": "L001",
                "amount": "-5000",  # Invalid: negative amount
                "interest_rate": "12%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
        ]

        mock_data_response = MagicMock()
        mock_data_response.status_code = 200
        mock_data_response.json.return_value = {
            "data": invalid_records,
            "total_records": 1,
        }
        mock_data_response.raise_for_status.return_value = None

        mock_version_response = MagicMock()
        mock_version_response.status_code = 200
        mock_version_response.json.return_value = {
            "versions": [
                {"file_type": "loans", "loan_type": "RETAIL", "version": 6}
            ]
        }
        mock_version_response.raise_for_status.return_value = None

        def side_effect(url, **kwargs):
            if "/version" in url:
                return mock_version_response
            return mock_data_response
        mock_get.side_effect = side_effect

        service = SyncService()
        result = service.sync_tenant("BANK001", file_type="loans", loan_type="RETAIL")

        # Verify NO data was loaded to staging
        ch_instance.load_loans_to_staging.assert_not_called()

        # Verify NO atomic swap happened
        ch_instance.swap_staging_to_production.assert_not_called()

        # Verify version was NOT updated
        sync_state.refresh_from_db()
        assert sync_state.last_version == 5  # Still the old version

        # Verify sync log shows failure
        sync_result = result["results"][0]
        assert sync_result["status"] == "validation_failed"
        assert sync_result["errors"] > 0

        # Verify validation error was logged in the DB
        last_log = SyncLog.objects.filter(
            tenant=tenant_bank1,
            file_type="loans",
            loan_type="RETAIL",
        ).first()
        assert last_log.status == "failed"
        assert "all-or-nothing" in last_log.error_message.lower() or \
               "validation" in last_log.error_message.lower()
        assert ValidationErrorLog.objects.filter(sync_log=last_log).exists()

    @patch("adapter.sync_service.ClickHouseClient")
    @patch("adapter.sync_service.requests.get")
    def test_orphan_payments_preserve_existing_data(
        self, mock_get, MockCHClient, db, tenant_bank1
    ):
        """
        When payments reference non-existent loans, the sync should fail
        and existing data should be preserved.
        """
        ch_instance = MockCHClient.return_value
        ch_instance.initialize.return_value = None

        SyncState.objects.create(
            tenant=tenant_bank1,
            file_type="payments",
            loan_type="RETAIL",
            last_version=3,
        )

        # Payments with orphan references
        payment_records = [
            {
                "payment_id": "P001",
                "loan_id": "L999",  # No such loan in the loan dataset
                "payment_amount": "5000",
                "payment_date": "2024-02-15",
            },
        ]

        # Loan records exist but do NOT contain L999 -> orphan detected
        loan_records = [
            {
                "loan_id": "L001",
                "loan_type": "RETAIL",
                "amount": "50000",
                "interest_rate": "12%",
                "start_date": "2024-01-15",
                "status": "Active",
            },
        ]

        mock_version_response = MagicMock()
        mock_version_response.status_code = 200
        mock_version_response.json.return_value = {
            "versions": [
                {"file_type": "payments", "loan_type": "RETAIL", "version": 4}
            ]
        }
        mock_version_response.raise_for_status.return_value = None

        call_count = [0]

        def side_effect(url, **kwargs):
            if "/version" in url:
                return mock_version_response
            # First data call is for payments, second is for loans (cross-check)
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            if call_count[0] == 1:
                # Payment records
                resp.json.return_value = {
                    "data": payment_records,
                    "total_records": 1,
                }
            else:
                # Loan records for cross-file check (exist but no L999)
                resp.json.return_value = {
                    "data": loan_records,
                    "total_records": 1,
                }
            return resp

        mock_get.side_effect = side_effect

        service = SyncService()
        result = service.sync_tenant("BANK001", file_type="payments", loan_type="RETAIL")

        # Verify staging was NOT loaded
        ch_instance.load_payments_to_staging.assert_not_called()

        # Verify swap was NOT called
        ch_instance.swap_staging_to_production.assert_not_called()

        # Verify version preserved
        state = SyncState.objects.get(
            tenant=tenant_bank1,
            file_type="payments",
            loan_type="RETAIL",
        )
        assert state.last_version == 3  # Unchanged

    @patch("adapter.sync_service.ClickHouseClient")
    @patch("adapter.sync_service.requests.get")
    def test_staging_cleanup_on_load_failure(
        self, mock_get, MockCHClient, db, tenant_bank1
    ):
        """
        If loading data to staging fails, the staging table should be
        cleaned up and the error propagated.
        """
        ch_instance = MockCHClient.return_value
        ch_instance.initialize.return_value = None
        ch_instance.create_staging_table.return_value = None
        ch_instance.load_loans_to_staging.side_effect = Exception("ClickHouse down")
        ch_instance.drop_staging_table.return_value = None

        valid_records = _make_loans(10)

        mock_version_response = MagicMock()
        mock_version_response.status_code = 200
        mock_version_response.json.return_value = {
            "versions": [
                {"file_type": "loans", "loan_type": "RETAIL", "version": 2}
            ]
        }
        mock_version_response.raise_for_status.return_value = None

        mock_data_response = MagicMock()
        mock_data_response.status_code = 200
        mock_data_response.json.return_value = {
            "data": valid_records,
            "total_records": 10,
        }
        mock_data_response.raise_for_status.return_value = None

        def side_effect(url, **kwargs):
            if "/version" in url:
                return mock_version_response
            return mock_data_response
        mock_get.side_effect = side_effect

        service = SyncService()
        result = service.sync_tenant("BANK001", file_type="loans", loan_type="RETAIL")

        # Staging should be dropped on failure
        ch_instance.drop_staging_table.assert_called_once_with("loans")

        # Swap should NOT have been called
        ch_instance.swap_staging_to_production.assert_not_called()

        # Result should indicate failure
        sync_result = result["results"][0]
        assert sync_result["status"] == "failed"
