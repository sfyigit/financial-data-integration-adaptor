"""
Unit Tests for tenants.authentication (APIKeyAuthentication & APIKeyUser)
and tenants.permissions (IsTenantMember, IsTenantAdmin, helpers).

These tests exercise the authentication backends and permission classes
in isolation using Django's test framework.
"""

import pytest
from django.test import RequestFactory
from django.contrib.auth.models import User
from rest_framework.exceptions import AuthenticationFailed

from tenants.models import Tenant, TenantMembership
from tenants.authentication import APIKeyAuthentication, APIKeyUser
from tenants.permissions import (
    IsTenantMember,
    IsTenantAdmin,
    IsAdminOrReadOnly,
    get_tenant_for_user,
    get_user_tenants,
    _user_has_tenant_access,
)


# =============================================================================
# APIKeyUser
# =============================================================================

class TestAPIKeyUser:
    """Tests for the lightweight APIKeyUser object."""

    def test_properties(self, tenant_bank1):
        user = APIKeyUser(tenant_bank1)
        assert user.is_authenticated is True
        assert user.is_active is True
        assert user.is_staff is False
        assert user.is_superuser is False
        assert user.tenant == tenant_bank1
        assert "BANK001" in user.username
        assert user.pk is None

    def test_str_representation(self, tenant_bank1):
        user = APIKeyUser(tenant_bank1)
        assert str(user) == f"apikey:{tenant_bank1.tenant_id}"


# =============================================================================
# APIKeyAuthentication Backend
# =============================================================================

class TestAPIKeyAuthentication:
    """Tests for the X-API-Key authentication backend."""

    def _make_request(self, api_key=None):
        """Helper to build a fake DRF-compatible request."""
        factory = RequestFactory()
        extra = {}
        if api_key is not None:
            extra["HTTP_X_API_KEY"] = api_key
        return factory.get("/api/tenants/", **extra)

    def test_valid_api_key(self, tenant_bank1):
        """A valid, active API key should authenticate successfully."""
        request = self._make_request(api_key=tenant_bank1.api_key)
        backend = APIKeyAuthentication()
        result = backend.authenticate(request)

        assert result is not None
        user, auth = result
        assert isinstance(user, APIKeyUser)
        assert user.tenant.tenant_id == "BANK001"
        assert auth == tenant_bank1  # second element is the tenant

    def test_invalid_api_key(self, tenant_bank1):
        """An invalid API key should raise AuthenticationFailed."""
        request = self._make_request(api_key="invalid-key-does-not-exist")
        backend = APIKeyAuthentication()
        with pytest.raises(AuthenticationFailed):
            backend.authenticate(request)

    def test_inactive_tenant_api_key(self, tenant_inactive):
        """API key for an inactive tenant should raise AuthenticationFailed."""
        request = self._make_request(api_key=tenant_inactive.api_key)
        backend = APIKeyAuthentication()
        with pytest.raises(AuthenticationFailed):
            backend.authenticate(request)

    def test_missing_api_key_returns_none(self, db):
        """When no X-API-Key header is present, backend returns None (skip)."""
        request = self._make_request(api_key=None)
        backend = APIKeyAuthentication()
        result = backend.authenticate(request)
        assert result is None

    def test_authenticate_header(self, db):
        """The authenticate_header should return the keyword."""
        request = self._make_request()
        backend = APIKeyAuthentication()
        assert backend.authenticate_header(request) == "X-API-Key"


# =============================================================================
# Permission Helpers
# =============================================================================

class TestPermissionHelpers:
    """Tests for utility functions in tenants.permissions."""

    def test_get_tenant_for_user_jwt(self, user_bank1, tenant_bank1, membership_bank1):
        """get_tenant_for_user should return the default tenant for a JWT user."""
        tenant = get_tenant_for_user(user_bank1)
        assert tenant is not None
        assert tenant.tenant_id == "BANK001"

    def test_get_tenant_for_user_api_key(self, tenant_bank1):
        """get_tenant_for_user should return the API key's tenant."""
        api_user = APIKeyUser(tenant_bank1)
        tenant = get_tenant_for_user(api_user)
        assert tenant.tenant_id == "BANK001"

    def test_get_tenant_for_user_no_membership(self, user_no_tenant):
        """A user with no memberships should get None."""
        tenant = get_tenant_for_user(user_no_tenant)
        assert tenant is None

    def test_get_user_tenants_jwt(self, user_bank1, tenant_bank1, membership_bank1):
        """get_user_tenants should return all tenants for a JWT user."""
        tenants = get_user_tenants(user_bank1)
        tenant_ids = list(tenants.values_list("tenant_id", flat=True))
        assert "BANK001" in tenant_ids
        assert len(tenant_ids) == 1

    def test_get_user_tenants_api_key(self, tenant_bank1):
        """get_user_tenants for an API key user should return only that tenant."""
        api_user = APIKeyUser(tenant_bank1)
        tenants = get_user_tenants(api_user)
        assert tenants.count() == 1
        assert tenants.first().tenant_id == "BANK001"

    def test_user_has_tenant_access_positive(self, user_bank1, tenant_bank1, membership_bank1):
        """User with membership should have access."""
        assert _user_has_tenant_access(user_bank1, tenant_bank1) is True

    def test_user_has_tenant_access_negative(self, user_bank1, tenant_bank2, membership_bank1):
        """User should NOT have access to a tenant they don't belong to."""
        assert _user_has_tenant_access(user_bank1, tenant_bank2) is False

    def test_user_has_tenant_access_api_key_match(self, tenant_bank1):
        """API key user should have access to their own tenant."""
        api_user = APIKeyUser(tenant_bank1)
        assert _user_has_tenant_access(api_user, tenant_bank1) is True

    def test_user_has_tenant_access_api_key_mismatch(self, tenant_bank1, tenant_bank2):
        """API key user should NOT have access to a different tenant."""
        api_user = APIKeyUser(tenant_bank1)
        assert _user_has_tenant_access(api_user, tenant_bank2) is False


# =============================================================================
# IsTenantMember Permission
# =============================================================================

class TestIsTenantMemberPermission:
    """Tests for the IsTenantMember permission class."""

    def _make_request(self, user):
        factory = RequestFactory()
        request = factory.get("/api/tenants/")
        request.user = user
        return request

    def test_superuser_always_allowed(self, superuser):
        """Superuser should always pass IsTenantMember."""
        request = self._make_request(superuser)
        perm = IsTenantMember()
        assert perm.has_permission(request, None) is True

    def test_authenticated_user_allowed(self, user_bank1, membership_bank1):
        """Authenticated user with membership should pass."""
        request = self._make_request(user_bank1)
        perm = IsTenantMember()
        assert perm.has_permission(request, None) is True

    def test_unauthenticated_denied(self, db):
        """Anonymous/unauthenticated requests should be denied."""
        from django.contrib.auth.models import AnonymousUser
        request = self._make_request(AnonymousUser())
        perm = IsTenantMember()
        assert perm.has_permission(request, None) is False

    def test_object_permission_matching_tenant(
        self, user_bank1, tenant_bank1, membership_bank1
    ):
        """User should have object-level access to their own tenant."""
        request = self._make_request(user_bank1)
        perm = IsTenantMember()
        assert perm.has_object_permission(request, None, tenant_bank1) is True

    def test_object_permission_non_matching_tenant(
        self, user_bank1, tenant_bank2, membership_bank1
    ):
        """User should NOT have object-level access to a different tenant."""
        request = self._make_request(user_bank1)
        perm = IsTenantMember()
        assert perm.has_object_permission(request, None, tenant_bank2) is False

    def test_api_key_user_object_permission_match(self, tenant_bank1):
        """API key user should have object permission on their own tenant."""
        api_user = APIKeyUser(tenant_bank1)
        request = self._make_request(api_user)
        perm = IsTenantMember()
        assert perm.has_object_permission(request, None, tenant_bank1) is True

    def test_api_key_user_object_permission_mismatch(self, tenant_bank1, tenant_bank2):
        """API key user should NOT have object permission on a different tenant."""
        api_user = APIKeyUser(tenant_bank1)
        request = self._make_request(api_user)
        perm = IsTenantMember()
        assert perm.has_object_permission(request, None, tenant_bank2) is False


# =============================================================================
# IsTenantAdmin Permission
# =============================================================================

class TestIsTenantAdminPermission:
    """Tests for the IsTenantAdmin permission class."""

    def _make_request(self, user):
        factory = RequestFactory()
        request = factory.get("/api/tenants/")
        request.user = user
        return request

    def test_admin_role_allowed(self, user_bank1, tenant_bank1, membership_bank1):
        """User with 'admin' role should pass IsTenantAdmin object check."""
        request = self._make_request(user_bank1)
        perm = IsTenantAdmin()
        # membership_bank1 has role='admin'
        assert perm.has_object_permission(request, None, tenant_bank1) is True

    def test_non_admin_role_denied(self, user_bank2, tenant_bank2, membership_bank2):
        """User with 'analyst' role should fail IsTenantAdmin object check."""
        request = self._make_request(user_bank2)
        perm = IsTenantAdmin()
        # membership_bank2 has role='analyst'
        assert perm.has_object_permission(request, None, tenant_bank2) is False

    def test_superuser_always_admin(self, superuser, tenant_bank1):
        """Superuser should always pass IsTenantAdmin."""
        request = self._make_request(superuser)
        perm = IsTenantAdmin()
        assert perm.has_object_permission(request, None, tenant_bank1) is True


# =============================================================================
# IsAdminOrReadOnly Permission
# =============================================================================

class TestIsAdminOrReadOnly:
    """Tests for the IsAdminOrReadOnly permission class."""

    def test_safe_method_for_authenticated(self, user_bank1, membership_bank1):
        """GET requests should be allowed for authenticated users."""
        factory = RequestFactory()
        request = factory.get("/api/tenants/")
        request.user = user_bank1
        perm = IsAdminOrReadOnly()
        assert perm.has_permission(request, None) is True

    def test_unsafe_method_for_non_superuser(self, user_bank1, membership_bank1):
        """POST/PUT/DELETE should be denied for non-superusers."""
        factory = RequestFactory()
        request = factory.post("/api/tenants/")
        request.user = user_bank1
        perm = IsAdminOrReadOnly()
        assert perm.has_permission(request, None) is False

    def test_unsafe_method_for_superuser(self, superuser):
        """POST/PUT/DELETE should be allowed for superusers."""
        factory = RequestFactory()
        request = factory.post("/api/tenants/")
        request.user = superuser
        perm = IsAdminOrReadOnly()
        assert perm.has_permission(request, None) is True
