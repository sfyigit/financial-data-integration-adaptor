"""
Integration Tests for Tenant Isolation (CRITICAL).

These tests verify the single most important security property:
  *** BANK001 must NEVER see BANK002 data ***

Tests cover:
  - JWT user can only list their own tenant
  - JWT user cannot retrieve another tenant's detail
  - API Key user can only list their own tenant
  - API Key user cannot access another tenant's API
  - Sync logs are scoped per tenant
  - Dashboard data is scoped per tenant
  - Trigger sync is scoped per tenant
  - Superuser can see all (bypass check)
  - User with no membership sees nothing
"""

import pytest
from unittest.mock import patch
from rest_framework import status

from tenants.models import Tenant, TenantMembership, SyncLog


# =============================================================================
# Tenant List Isolation
# =============================================================================

class TestTenantListIsolation:
    """Ensure that tenant listing never leaks cross-tenant data."""

    def test_bank1_jwt_never_sees_bank2(
        self, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 JWT user MUST NOT see BANK002 in /api/tenants/."""
        response = api_client_bank1.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        visible_ids = {t["tenant_id"] for t in data["results"]}
        assert "BANK002" not in visible_ids
        assert "BANK001" in visible_ids

    def test_bank2_jwt_never_sees_bank1(
        self, api_client_bank2, tenant_bank1, tenant_bank2
    ):
        """BANK002 JWT user MUST NOT see BANK001 in /api/tenants/."""
        response = api_client_bank2.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        visible_ids = {t["tenant_id"] for t in data["results"]}
        assert "BANK001" not in visible_ids
        assert "BANK002" in visible_ids

    def test_bank1_apikey_never_sees_bank2(
        self, api_client_apikey_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 API key MUST NOT see BANK002 in /api/tenants/."""
        response = api_client_apikey_bank1.get("/api/tenants/")
        data = response.json()
        visible_ids = {t["tenant_id"] for t in data["results"]}
        assert "BANK002" not in visible_ids

    def test_bank2_apikey_never_sees_bank1(
        self, api_client_apikey_bank2, tenant_bank1, tenant_bank2
    ):
        """BANK002 API key MUST NOT see BANK001 in /api/tenants/."""
        response = api_client_apikey_bank2.get("/api/tenants/")
        data = response.json()
        visible_ids = {t["tenant_id"] for t in data["results"]}
        assert "BANK001" not in visible_ids


# =============================================================================
# Tenant Detail Isolation
# =============================================================================

class TestTenantDetailIsolation:
    """Ensure that tenant detail retrieval is isolated."""

    def test_bank1_cannot_get_bank2_detail(
        self, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 user requesting BANK002 detail should get 404."""
        response = api_client_bank1.get(f"/api/tenants/{tenant_bank2.pk}/")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_bank2_cannot_get_bank1_detail(
        self, api_client_bank2, tenant_bank1, tenant_bank2
    ):
        """BANK002 user requesting BANK001 detail should get 404."""
        response = api_client_bank2.get(f"/api/tenants/{tenant_bank1.pk}/")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_apikey_bank1_cannot_get_bank2_detail(
        self, api_client_apikey_bank1, tenant_bank1, tenant_bank2
    ):
        """API key BANK001 requesting BANK002 detail should get 404."""
        response = api_client_apikey_bank1.get(f"/api/tenants/{tenant_bank2.pk}/")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_superuser_can_get_any_detail(
        self, api_client_superuser, tenant_bank1, tenant_bank2
    ):
        """Superuser should be able to retrieve any tenant."""
        resp1 = api_client_superuser.get(f"/api/tenants/{tenant_bank1.pk}/")
        resp2 = api_client_superuser.get(f"/api/tenants/{tenant_bank2.pk}/")
        assert resp1.status_code == status.HTTP_200_OK
        assert resp2.status_code == status.HTTP_200_OK


# =============================================================================
# Sync Log Isolation
# =============================================================================

class TestSyncLogIsolation:
    """Ensure sync logs are strictly scoped per tenant."""

    def test_bank1_cannot_see_bank2_logs(
        self, api_client_bank1, sync_log_bank1, sync_log_bank2
    ):
        """BANK001 user should only see BANK001 sync logs."""
        response = api_client_bank1.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        for log in data["results"]:
            assert log["tenant_id"] == "BANK001", \
                f"BANK001 user leaked to tenant {log['tenant_id']}"

    def test_bank2_cannot_see_bank1_logs(
        self, api_client_bank2, sync_log_bank1, sync_log_bank2
    ):
        """BANK002 user should only see BANK002 sync logs."""
        response = api_client_bank2.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        for log in data["results"]:
            assert log["tenant_id"] == "BANK002", \
                f"BANK002 user leaked to tenant {log['tenant_id']}"

    def test_superuser_sees_all_logs(
        self, api_client_superuser, sync_log_bank1, sync_log_bank2
    ):
        """Superuser should see logs from all tenants."""
        response = api_client_superuser.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = {log["tenant_id"] for log in data["results"]}
        assert "BANK001" in tenant_ids
        assert "BANK002" in tenant_ids


# =============================================================================
# Dashboard Data Isolation
# =============================================================================

class TestDashboardDataIsolation:
    """Ensure dashboard data is scoped per tenant."""

    def test_bank1_dashboard_only_bank1(
        self, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 user dashboard should only contain BANK001."""
        response = api_client_bank1.get("/api/dashboard-data/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        for t in data["tenants"]:
            assert t["tenant_id"] == "BANK001"

    def test_bank2_dashboard_only_bank2(
        self, api_client_bank2, tenant_bank1, tenant_bank2
    ):
        """BANK002 user dashboard should only contain BANK002."""
        response = api_client_bank2.get("/api/dashboard-data/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        for t in data["tenants"]:
            assert t["tenant_id"] == "BANK002"


# =============================================================================
# Trigger Sync Isolation
# =============================================================================

class TestTriggerSyncIsolation:
    """Ensure sync triggering respects tenant boundaries."""

    @patch("adapter.tasks.sync_tasks.run_sync_for_tenant")
    def test_bank1_cannot_trigger_sync_for_bank2(
        self, mock_sync, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 user should be denied when trying to sync BANK002."""
        response = api_client_bank1.post("/api/trigger-sync/", {
            "tenant_id": "BANK002",
        }, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_sync.delay.assert_not_called()

    @patch("adapter.tasks.sync_tasks.run_sync_for_tenant")
    def test_bank1_can_trigger_own_sync(
        self, mock_sync, api_client_bank1, tenant_bank1
    ):
        """BANK001 user should be allowed to sync their own tenant."""
        mock_sync.delay.return_value.id = "fake-task-id"
        response = api_client_bank1.post("/api/trigger-sync/", {
            "tenant_id": "BANK001",
        }, format="json")
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_sync.delay.assert_called_once()

    @patch("adapter.tasks.sync_tasks.run_sync_for_tenant")
    def test_apikey_bank1_cannot_trigger_sync_for_bank2(
        self, mock_sync, api_client_apikey_bank1, tenant_bank1, tenant_bank2
    ):
        """API key for BANK001 should not be able to sync BANK002."""
        response = api_client_apikey_bank1.post("/api/trigger-sync/", {
            "tenant_id": "BANK002",
        }, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_sync.delay.assert_not_called()


# =============================================================================
# No Membership = No Data
# =============================================================================

class TestNoMembershipUser:
    """User with no tenant membership should see nothing."""

    def test_no_tenants_visible(
        self, api_client_no_tenant, tenant_bank1, tenant_bank2
    ):
        """User with no membership should get an empty tenant list."""
        response = api_client_no_tenant.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 0

    def test_no_sync_logs_visible(
        self, api_client_no_tenant, sync_log_bank1, sync_log_bank2
    ):
        """User with no membership should see no sync logs."""
        response = api_client_no_tenant.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 0

    def test_empty_dashboard(
        self, api_client_no_tenant, tenant_bank1, tenant_bank2
    ):
        """User with no membership should see an empty dashboard."""
        response = api_client_no_tenant.get("/api/dashboard-data/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_tenants"] == 0
