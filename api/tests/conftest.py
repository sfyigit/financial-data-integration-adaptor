"""
Shared pytest fixtures for the FSec test suite.

Provides pre-configured users, tenants, memberships, and API clients
so that individual test modules can focus on assertions rather than setup.

Sample data supports BOTH old-style field names (loan_id, amount, etc.)
and new-style field names (loan_account_number, original_loan_amount, etc.)
from the mock_data CSV files.
"""

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from tenants.models import Tenant, TenantMembership, SyncState, SyncLog


# =============================================================================
# Users
# =============================================================================

@pytest.fixture
def superuser(db):
    """Django superuser (can see all tenants)."""
    return User.objects.create_superuser(
        username="superadmin",
        email="super@fsec.io",
        password="SuperPass123!",
    )


@pytest.fixture
def user_bank1(db):
    """Regular user belonging to BANK001."""
    return User.objects.create_user(
        username="bank1_user",
        email="bank1@fsec.io",
        password="Bank1Pass123!",
    )


@pytest.fixture
def user_bank2(db):
    """Regular user belonging to BANK002."""
    return User.objects.create_user(
        username="bank2_user",
        email="bank2@fsec.io",
        password="Bank2Pass123!",
    )


@pytest.fixture
def user_no_tenant(db):
    """Authenticated user with no tenant membership."""
    return User.objects.create_user(
        username="orphan_user",
        email="orphan@fsec.io",
        password="OrphanPass123!",
    )


# =============================================================================
# Tenants
# =============================================================================

@pytest.fixture
def tenant_bank1(db):
    """Active tenant BANK001."""
    return Tenant.objects.create(
        tenant_id="BANK001",
        name="First National Bank",
        is_active=True,
        api_key="bank001-api-key-test-1234567890",
    )


@pytest.fixture
def tenant_bank2(db):
    """Active tenant BANK002."""
    return Tenant.objects.create(
        tenant_id="BANK002",
        name="Second National Bank",
        is_active=True,
        api_key="bank002-api-key-test-0987654321",
    )


@pytest.fixture
def tenant_inactive(db):
    """Inactive tenant BANK_INACTIVE."""
    return Tenant.objects.create(
        tenant_id="BANK_INACTIVE",
        name="Inactive Bank",
        is_active=False,
        api_key="inactive-api-key-test-1111111111",
    )


# =============================================================================
# Tenant Memberships
# =============================================================================

@pytest.fixture
def membership_bank1(db, user_bank1, tenant_bank1):
    """Links user_bank1 -> BANK001 as admin."""
    return TenantMembership.objects.create(
        user=user_bank1,
        tenant=tenant_bank1,
        role="admin",
        is_default=True,
    )


@pytest.fixture
def membership_bank2(db, user_bank2, tenant_bank2):
    """Links user_bank2 -> BANK002 as analyst."""
    return TenantMembership.objects.create(
        user=user_bank2,
        tenant=tenant_bank2,
        role="analyst",
        is_default=True,
    )


# =============================================================================
# API Clients
# =============================================================================

@pytest.fixture
def api_client():
    """Unauthenticated DRF API client."""
    return APIClient()


@pytest.fixture
def api_client_superuser(superuser):
    """API client authenticated as superuser via JWT."""
    client = APIClient()
    refresh = RefreshToken.for_user(superuser)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


@pytest.fixture
def api_client_bank1(user_bank1, membership_bank1):
    """API client authenticated as BANK001 user via JWT."""
    client = APIClient()
    refresh = RefreshToken.for_user(user_bank1)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


@pytest.fixture
def api_client_bank2(user_bank2, membership_bank2):
    """API client authenticated as BANK002 user via JWT."""
    client = APIClient()
    refresh = RefreshToken.for_user(user_bank2)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


@pytest.fixture
def api_client_apikey_bank1(tenant_bank1):
    """API client authenticated with BANK001's API key."""
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=tenant_bank1.api_key)
    return client


@pytest.fixture
def api_client_apikey_bank2(tenant_bank2):
    """API client authenticated with BANK002's API key."""
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=tenant_bank2.api_key)
    return client


@pytest.fixture
def api_client_no_tenant(user_no_tenant):
    """API client for a user with no tenant membership."""
    client = APIClient()
    refresh = RefreshToken.for_user(user_no_tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


# =============================================================================
# Sync Data Helpers
# =============================================================================

@pytest.fixture
def sync_state_bank1(db, tenant_bank1):
    """Pre-existing sync state for BANK001 loans/RETAIL."""
    return SyncState.objects.create(
        tenant=tenant_bank1,
        file_type="loans",
        loan_type="RETAIL",
        last_version=3,
    )


@pytest.fixture
def sync_log_bank1(db, tenant_bank1):
    """A completed sync log for BANK001."""
    return SyncLog.objects.create(
        tenant=tenant_bank1,
        file_type="loans",
        loan_type="RETAIL",
        status="success",
        records_fetched=100,
        records_valid=100,
        records_invalid=0,
        version_before=2,
        version_after=3,
    )


@pytest.fixture
def sync_log_bank2(db, tenant_bank2):
    """A completed sync log for BANK002."""
    return SyncLog.objects.create(
        tenant=tenant_bank2,
        file_type="loans",
        loan_type="COMMERCIAL",
        status="success",
        records_fetched=50,
        records_valid=50,
        records_invalid=0,
        version_before=1,
        version_after=2,
    )


# =============================================================================
# Sample Data Helpers — OLD-STYLE (backward compat)
# =============================================================================

@pytest.fixture
def valid_loan_records():
    """A list of valid, raw loan records for testing (old-style field names)."""
    return [
        {
            "loan_id": "L001",
            "loan_type": "RETAIL",
            "amount": "50000",
            "interest_rate": "18.5%",
            "start_date": "15/01/2024",
            "status": "Active",
        },
        {
            "loan_id": "L002",
            "loan_type": "RETAIL",
            "amount": "120000",
            "interest_rate": "0.125",
            "start_date": "2024-03-20",
            "status": "Closed",
        },
        {
            "loan_id": "L003",
            "loan_type": "RETAIL",
            "amount": "75000",
            "interest_rate": "2100 bps",
            "start_date": "20240515",
            "status": "Performing",
        },
    ]


@pytest.fixture
def valid_payment_records():
    """A list of valid, raw payment records for testing (old-style field names)."""
    return [
        {
            "payment_id": "P001",
            "loan_id": "L001",
            "loan_type": "RETAIL",
            "payment_amount": "5000",
            "payment_date": "15/02/2024",
        },
        {
            "payment_id": "P002",
            "loan_id": "L002",
            "loan_type": "RETAIL",
            "payment_amount": "12000",
            "payment_date": "2024-04-20",
        },
    ]


@pytest.fixture
def invalid_loan_records():
    """A list of loan records containing various validation errors."""
    return [
        {
            # Missing loan_id
            "loan_type": "RETAIL",
            "amount": "50000",
            "interest_rate": "18.5%",
            "start_date": "15/01/2024",
            "status": "Active",
        },
        {
            "loan_id": "L_BAD_AMOUNT",
            "loan_type": "RETAIL",
            "amount": "-5000",  # Negative amount
            "interest_rate": "18.5%",
            "start_date": "15/01/2024",
            "status": "Active",
        },
        {
            "loan_id": "L_BAD_RATE",
            "loan_type": "RETAIL",
            "amount": "50000",
            "interest_rate": "not_a_rate",  # Unparseable rate
            "start_date": "15/01/2024",
            "status": "Active",
        },
        {
            "loan_id": "L_BAD_DATE",
            "loan_type": "RETAIL",
            "amount": "50000",
            "interest_rate": "18.5%",
            "start_date": "not-a-date",  # Unparseable date
            "status": "Active",
        },
        {
            "loan_id": "L_BAD_STATUS",
            "loan_type": "RETAIL",
            "amount": "50000",
            "interest_rate": "18.5%",
            "start_date": "15/01/2024",
            "status": "UNKNOWN_STATUS",  # Unrecognized status
        },
    ]


# =============================================================================
# Sample Data Helpers — NEW-STYLE (mock_data format)
# =============================================================================

@pytest.fixture
def valid_retail_loan_records():
    """Valid retail loan records using mock_data field names."""
    return [
        {
            "customer_id": "CUST_00001",
            "customer_type": "I",
            "loan_account_number": "LOAN_000001",
            "loan_status_code": "A",
            "days_past_due": "0",
            "final_maturity_date": "20260302",
            "total_installment_count": "10",
            "outstanding_installment_count": "7",
            "paid_installment_count": "3",
            "first_payment_date": "20250402",
            "original_loan_amount": "98940",
            "outstanding_principal_balance": "88600",
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
        },
        {
            "customer_id": "CUST_00002",
            "customer_type": "I",
            "loan_account_number": "LOAN_000002",
            "loan_status_code": "A",
            "days_past_due": "0",
            "final_maturity_date": "20251217",
            "total_installment_count": "5",
            "outstanding_installment_count": "5",
            "paid_installment_count": "0",
            "first_payment_date": "20250720",
            "original_loan_amount": "8570",
            "outstanding_principal_balance": "8530",
            "nominal_interest_rate": "52.83",
            "total_interest_amount": "214.61",
            "kkdf_rate": "15.78",
            "kkdf_amount": "32.48",
            "bsmv_rate": "13.98",
            "bsmv_amount": "30.79",
            "grace_period_months": "0",
            "installment_frequency": "1",
            "loan_start_date": "20250619",
            "loan_closing_date": "",
            "insurance_included": "H",
            "customer_district_code": "DISTRICT_B",
            "customer_province_code": "PROVINCE_2",
            "internal_rating": "4",
            "external_rating": "965",
        },
    ]


@pytest.fixture
def valid_retail_payment_records():
    """Valid retail payment records using mock_data field names."""
    return [
        {
            "loan_account_number": "LOAN_000001",
            "installment_number": "1",
            "actual_payment_date": "20250208",
            "scheduled_payment_date": "2025-02-08",
            "installment_amount": "17790",
            "principal_component": "13640",
            "interest_component": "4281.23",
            "kkdf_component": "727.56",
            "bsmv_component": "651.22",
            "installment_status": "K",
            "remaining_principal": "0",
            "remaining_interest": "0",
            "remaining_kkdf": "0",
            "remaining_bsmv": "0",
        },
        {
            "loan_account_number": "LOAN_000001",
            "installment_number": "2",
            "actual_payment_date": "20250311",
            "scheduled_payment_date": "2025-03-10",
            "installment_amount": "21940",
            "principal_component": "17120",
            "interest_component": "3742.86",
            "kkdf_component": "481.68",
            "bsmv_component": "638.95",
            "installment_status": "K",
            "remaining_principal": "0",
            "remaining_interest": "0",
            "remaining_kkdf": "0",
            "remaining_bsmv": "0",
        },
        {
            "loan_account_number": "LOAN_000002",
            "installment_number": "1",
            "actual_payment_date": "",
            "scheduled_payment_date": "2025-07-25",
            "installment_amount": "53490",
            "principal_component": "40870",
            "interest_component": "5185.48",
            "kkdf_component": "0",
            "bsmv_component": "227.36",
            "installment_status": "A",
            "remaining_principal": "39350",
            "remaining_interest": "4992.03",
            "remaining_kkdf": "0",
            "remaining_bsmv": "267.84",
        },
    ]


@pytest.fixture
def valid_commercial_loan_records():
    """Valid commercial loan records using mock_data field names."""
    return [
        {
            "loan_account_number": "LOAN_COM_001",
            "customer_type": "T",
            "customer_id": "CUST_COM_001",
            "loan_product_type": "4",
            "loan_status_code": "A",
            "loan_status_flag": "A",
            "days_past_due": "0",
            "final_maturity_date": "20250901",
            "total_installment_count": "1",
            "outstanding_installment_count": "1",
            "paid_installment_count": "0",
            "first_payment_date": "20250901",
            "original_loan_amount": "28370",
            "outstanding_principal_balance": "26410",
            "nominal_interest_rate": "0",
            "total_interest_amount": "0",
            "kkdf_rate": "0",
            "kkdf_amount": "0",
            "bsmv_rate": "5.14",
            "bsmv_amount": "0",
            "grace_period_months": "0",
            "installment_frequency": "1",
            "loan_start_date": "20250618",
            "loan_closing_date": "",
            "customer_region_code": "REGION_1",
            "sector_code": "4",
            "internal_credit_rating": "3",
            "default_probability": "0.0217",
            "risk_class": "1",
            "customer_segment": "1",
            "internal_rating": "8",
            "external_rating": "934",
        },
    ]
