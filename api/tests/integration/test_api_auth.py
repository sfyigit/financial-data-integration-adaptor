"""
Integration Tests for Authentication API Endpoints.

Covers:
  - JWT token obtain / refresh
  - Session login / logout
  - /api/auth/me/ profile endpoint
  - /api/auth/my-tenants/ endpoint
  - Unauthenticated access denial
"""

import pytest
from django.test import TestCase
from rest_framework import status


# =============================================================================
# JWT Token Endpoints
# =============================================================================

class TestTokenObtain:
    """Tests for POST /api/auth/token/"""

    def test_obtain_token_success(self, api_client, user_bank1, membership_bank1):
        """Valid credentials should return access + refresh tokens."""
        response = api_client.post("/api/auth/token/", {
            "username": "bank1_user",
            "password": "Bank1Pass123!",
        }, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "access" in data
        assert "refresh" in data
        assert data["user"]["username"] == "bank1_user"
        assert isinstance(data["tenants"], list)

    def test_obtain_token_includes_tenant_info(
        self, api_client, user_bank1, tenant_bank1, membership_bank1
    ):
        """Token response should include the user's tenant memberships."""
        response = api_client.post("/api/auth/token/", {
            "username": "bank1_user",
            "password": "Bank1Pass123!",
        }, format="json")

        data = response.json()
        assert len(data["tenants"]) == 1
        assert data["tenants"][0]["tenant_id"] == "BANK001"
        assert data["tenants"][0]["role"] == "admin"

    def test_obtain_token_invalid_credentials(self, api_client, user_bank1):
        """Invalid password should return 401."""
        response = api_client.post("/api/auth/token/", {
            "username": "bank1_user",
            "password": "WrongPassword",
        }, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_obtain_token_missing_fields(self, api_client, db):
        """Missing fields should return 400."""
        response = api_client.post("/api/auth/token/", {}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_obtain_token_nonexistent_user(self, api_client, db):
        """Non-existent user should return 401."""
        response = api_client.post("/api/auth/token/", {
            "username": "ghost",
            "password": "GhostPass123!",
        }, format="json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestTokenRefresh:
    """Tests for POST /api/auth/token/refresh/"""

    def test_refresh_token_success(self, api_client, user_bank1, membership_bank1):
        """Valid refresh token should return a new access token."""
        # First, obtain tokens
        token_resp = api_client.post("/api/auth/token/", {
            "username": "bank1_user",
            "password": "Bank1Pass123!",
        }, format="json")
        refresh = token_resp.json()["refresh"]

        # Then, refresh
        response = api_client.post("/api/auth/token/refresh/", {
            "refresh": refresh,
        }, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert "access" in response.json()

    def test_refresh_token_invalid(self, api_client, db):
        """Invalid refresh token should return 401."""
        response = api_client.post("/api/auth/token/refresh/", {
            "refresh": "invalid-token",
        }, format="json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_refresh_token_missing(self, api_client, db):
        """Missing refresh token should return 400."""
        response = api_client.post("/api/auth/token/refresh/", {}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# =============================================================================
# /api/auth/me/ Endpoint
# =============================================================================

class TestMeEndpoint:
    """Tests for GET /api/auth/me/"""

    def test_me_jwt_user(self, api_client_bank1, tenant_bank1):
        """JWT authenticated user should see their profile and tenants."""
        response = api_client_bank1.get("/api/auth/me/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["user"]["username"] == "bank1_user"
        assert data["default_tenant"]["tenant_id"] == "BANK001"
        assert len(data["tenants"]) >= 1

    def test_me_api_key_user(self, api_client_apikey_bank1, tenant_bank1):
        """API key authenticated user should see their tenant info."""
        response = api_client_apikey_bank1.get("/api/auth/me/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["auth_method"] == "api_key"
        assert data["tenant"]["tenant_id"] == "BANK001"

    def test_me_unauthenticated(self, api_client):
        """Unauthenticated request should be denied."""
        response = api_client.get("/api/auth/me/")
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_me_superuser(self, api_client_superuser):
        """Superuser should see their profile with is_superuser=True."""
        response = api_client_superuser.get("/api/auth/me/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["user"]["is_superuser"] is True


# =============================================================================
# /api/auth/my-tenants/ Endpoint
# =============================================================================

class TestMyTenantsEndpoint:
    """Tests for GET /api/auth/my-tenants/"""

    def test_my_tenants_returns_memberships(
        self, api_client_bank1, tenant_bank1
    ):
        """Should return the tenants the user belongs to."""
        response = api_client_bank1.get("/api/auth/my-tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        tenant_ids = [t["tenant_id"] for t in data]
        assert "BANK001" in tenant_ids

    def test_my_tenants_api_key(self, api_client_apikey_bank1, tenant_bank1):
        """API key user should see only their tenant."""
        response = api_client_apikey_bank1.get("/api/auth/my-tenants/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["tenant_id"] == "BANK001"

    def test_my_tenants_no_membership(self, api_client_no_tenant):
        """User with no membership should get an empty list."""
        response = api_client_no_tenant.get("/api/auth/my-tenants/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_my_tenants_unauthenticated(self, api_client):
        """Unauthenticated request should be denied."""
        response = api_client.get("/api/auth/my-tenants/")
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )


# =============================================================================
# Session Auth (Login / Logout)
# =============================================================================

class TestSessionAuth:
    """Tests for session-based login/logout."""

    def test_login_page_renders(self, api_client, db):
        """GET /login/ should return 200 with login form."""
        response = api_client.get("/login/")
        assert response.status_code == status.HTTP_200_OK

    def test_login_success_redirects(self, api_client, user_bank1, membership_bank1):
        """Successful login should redirect to dashboard."""
        response = api_client.post("/login/", {
            "username": "bank1_user",
            "password": "Bank1Pass123!",
        })
        # Django session login redirects (302)
        assert response.status_code in (status.HTTP_302_FOUND, status.HTTP_200_OK)

    def test_login_failure_shows_error(self, api_client, user_bank1):
        """Failed login should re-render with error."""
        response = api_client.post("/login/", {
            "username": "bank1_user",
            "password": "wrong",
        })
        assert response.status_code == status.HTTP_200_OK
        # Should contain error message in rendered HTML
        content = response.content.decode()
        assert "Invalid" in content or "invalid" in content

    def test_logout_redirects(self, api_client, user_bank1, membership_bank1):
        """Logout should redirect to login page."""
        # Login first
        api_client.login(username="bank1_user", password="Bank1Pass123!")
        response = api_client.get("/logout/")
        assert response.status_code == status.HTTP_302_FOUND


# =============================================================================
# Unauthenticated Access to Protected API Endpoints
# =============================================================================

class TestUnauthenticatedAccess:
    """Verify that protected endpoints reject unauthenticated requests."""

    @pytest.mark.parametrize("endpoint", [
        "/api/tenants/",
        "/api/sync-logs/",
        "/api/dashboard-data/",
        "/api/auth/me/",
        "/api/auth/my-tenants/",
    ])
    def test_get_requires_auth(self, api_client, db, endpoint):
        """GET on protected endpoints should return 401 or 403."""
        response = api_client.get(endpoint)
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_trigger_sync_requires_auth(self, api_client, db):
        """POST /api/trigger-sync/ without auth should be denied."""
        response = api_client.post("/api/trigger-sync/", {
            "tenant_id": "BANK001",
        }, format="json")
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )
