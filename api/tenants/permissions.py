"""
Custom DRF Permission Classes for FSec.

Enforces tenant isolation so that BANK001 can never see BANK002 data.
"""

from rest_framework import permissions
from .authentication import APIKeyUser
from .models import TenantMembership, Tenant


class IsTenantMember(permissions.BasePermission):
    """
    Grants access only if the authenticated user (or API key) belongs to the
    requested tenant. Prevents cross-tenant data leakage.

    For JWT-authenticated users:
        Checks TenantMembership for the user + requested tenant.
    For API-key-authenticated requests:
        Verifies the API key's tenant matches the requested tenant.
    """

    message = "You do not have access to this tenant's data."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Superusers bypass tenant checks (admin panel access)
        if hasattr(request.user, "is_superuser") and request.user.is_superuser:
            return True

        return True

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False

        if hasattr(request.user, "is_superuser") and request.user.is_superuser:
            return True

        tenant = _resolve_tenant_from_object(obj)
        if tenant is None:
            return True  # Non-tenant-scoped objects are allowed

        return _user_has_tenant_access(request.user, tenant)


class IsTenantAdmin(permissions.BasePermission):
    """
    Grants access only to users with 'admin' role for the tenant.
    Used for destructive operations (delete tenant, modify config, etc.)
    """

    message = "Only tenant admins can perform this action."

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False

        if hasattr(request.user, "is_superuser") and request.user.is_superuser:
            return True

        tenant = _resolve_tenant_from_object(obj)
        if tenant is None:
            return True

        if isinstance(request.user, APIKeyUser):
            return request.user.tenant.pk == tenant.pk

        return TenantMembership.objects.filter(
            user=request.user, tenant=tenant, role="admin"
        ).exists()


class IsAdminOrReadOnly(permissions.BasePermission):
    """
    Allows read access to any authenticated user, but write access only to
    superusers or tenant admins.
    """

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return (
            request.user
            and request.user.is_authenticated
            and hasattr(request.user, "is_superuser")
            and request.user.is_superuser
        )


# =============================================================================
# Helpers
# =============================================================================

def get_tenant_for_user(user):
    """
    Returns the tenant associated with the current request user.
    For API key users, returns the tenant directly.
    For JWT/session users, returns their default tenant or first membership.
    """
    if isinstance(user, APIKeyUser):
        return user.tenant

    membership = TenantMembership.objects.filter(user=user).order_by("-is_default").first()
    return membership.tenant if membership else None


def get_user_tenants(user):
    """Returns all tenants the user has access to."""
    if isinstance(user, APIKeyUser):
        return Tenant.objects.filter(pk=user.tenant.pk)

    tenant_ids = TenantMembership.objects.filter(user=user).values_list("tenant_id", flat=True)
    return Tenant.objects.filter(id__in=tenant_ids)


def _resolve_tenant_from_object(obj):
    """Extracts the Tenant from a model instance."""
    if isinstance(obj, Tenant):
        return obj
    if hasattr(obj, "tenant"):
        return obj.tenant
    return None


def _user_has_tenant_access(user, tenant):
    """Checks if a user has access to a given tenant."""
    if isinstance(user, APIKeyUser):
        return user.tenant.pk == tenant.pk

    return TenantMembership.objects.filter(user=user, tenant=tenant).exists()
