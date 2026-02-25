"""
Integration Tests for Tenant API Endpoints.

Covers:
  - TenantViewSet CRUD operations
  - SyncLogViewSet read operations
  - Tenant-scoped queryset filtering
  - Dashboard data endpoint
"""

import pytest
from rest_framework import status

from tenants.models import Tenant


# =============================================================================
# TenantViewSet (CRUD)
# =============================================================================

class TestTenantList:
    """Tests for GET /api/tenants/"""

    def test_superuser_sees_all_tenants(
        self, api_client_superuser, tenant_bank1, tenant_bank2
    ):
        """Superuser should see every active tenant."""
        response = api_client_superuser.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = [t["tenant_id"] for t in data["results"]]
        assert "BANK001" in tenant_ids
        assert "BANK002" in tenant_ids

    def test_bank1_user_sees_only_bank1(
        self, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 user should only see BANK001."""
        response = api_client_bank1.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = [t["tenant_id"] for t in data["results"]]
        assert "BANK001" in tenant_ids
        assert "BANK002" not in tenant_ids

    def test_bank2_user_sees_only_bank2(
        self, api_client_bank2, tenant_bank1, tenant_bank2
    ):
        """BANK002 user should only see BANK002."""
        response = api_client_bank2.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = [t["tenant_id"] for t in data["results"]]
        assert "BANK002" in tenant_ids
        assert "BANK001" not in tenant_ids

    def test_api_key_user_sees_own_tenant(
        self, api_client_apikey_bank1, tenant_bank1, tenant_bank2
    ):
        """API key user should only see their tenant."""
        response = api_client_apikey_bank1.get("/api/tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = [t["tenant_id"] for t in data["results"]]
        assert "BANK001" in tenant_ids
        assert len(tenant_ids) == 1


class TestTenantDetail:
    """Tests for GET /api/tenants/{id}/"""

    def test_retrieve_own_tenant(self, api_client_bank1, tenant_bank1):
        """User should be able to retrieve their own tenant."""
        response = api_client_bank1.get(f"/api/tenants/{tenant_bank1.pk}/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["tenant_id"] == "BANK001"

    def test_cannot_retrieve_other_tenant(
        self, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """User should NOT be able to retrieve another tenant."""
        response = api_client_bank1.get(f"/api/tenants/{tenant_bank2.pk}/")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestTenantCreate:
    """Tests for POST /api/tenants/"""

    def test_superuser_can_create_tenant(self, api_client_superuser, db):
        """Superuser should be able to create a new tenant."""
        response = api_client_superuser.post("/api/tenants/", {
            "tenant_id": "BANK_NEW",
            "name": "New Test Bank",
            "is_active": True,
        }, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert Tenant.objects.filter(tenant_id="BANK_NEW").exists()

    def test_regular_user_cannot_create_tenant(
        self, api_client_bank1, tenant_bank1
    ):
        """Non-superuser should not be able to create a tenant."""
        response = api_client_bank1.post("/api/tenants/", {
            "tenant_id": "BANK_FORBIDDEN",
            "name": "Forbidden Bank",
        }, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN


# =============================================================================
# SyncLogViewSet
# =============================================================================

class TestSyncLogList:
    """Tests for GET /api/sync-logs/"""

    def test_superuser_sees_all_logs(
        self, api_client_superuser, sync_log_bank1, sync_log_bank2
    ):
        """Superuser should see all sync logs."""
        response = api_client_superuser.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] >= 2

    def test_bank1_user_sees_only_own_logs(
        self, api_client_bank1, sync_log_bank1, sync_log_bank2
    ):
        """BANK001 user should only see their own sync logs."""
        response = api_client_bank1.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = {r["tenant_id"] for r in data["results"]}
        assert tenant_ids == {"BANK001"}

    def test_bank2_user_sees_only_own_logs(
        self, api_client_bank2, sync_log_bank1, sync_log_bank2
    ):
        """BANK002 user should only see their own sync logs."""
        response = api_client_bank2.get("/api/sync-logs/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = {r["tenant_id"] for r in data["results"]}
        assert tenant_ids == {"BANK002"}


# =============================================================================
# Dashboard Data API
# =============================================================================

class TestDashboardData:
    """Tests for GET /api/dashboard-data/"""

    def test_superuser_dashboard(
        self, api_client_superuser, tenant_bank1, tenant_bank2
    ):
        """Superuser dashboard should include all active tenants."""
        response = api_client_superuser.get("/api/dashboard-data/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_tenants"] >= 2

    def test_bank1_dashboard_filtered(
        self, api_client_bank1, tenant_bank1, tenant_bank2
    ):
        """BANK001 user dashboard should only show BANK001 data."""
        response = api_client_bank1.get("/api/dashboard-data/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_tenants"] == 1
        assert data["tenants"][0]["tenant_id"] == "BANK001"

    def test_no_tenant_user_dashboard(self, api_client_no_tenant):
        """User with no tenant should see zero tenants."""
        response = api_client_no_tenant.get("/api/dashboard-data/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_tenants"] == 0
        assert data["tenants"] == []
