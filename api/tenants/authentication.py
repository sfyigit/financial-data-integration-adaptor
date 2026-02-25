"""
Custom DRF Authentication Backends for FSec.

Supports two authentication methods:
  1. JWT (Bearer token) - For user-facing API access
  2. API Key (X-API-Key header) - For machine-to-machine / tenant-level access
"""

from rest_framework import authentication, exceptions
from django.contrib.auth.models import AnonymousUser
from .models import Tenant


class APIKeyUser:
    """
    A lightweight user-like object representing an API-key-authenticated tenant.
    Implements the minimal interface expected by DRF and Django.
    """

    def __init__(self, tenant):
        self.tenant = tenant
        self.is_authenticated = True
        self.is_active = True
        self.is_staff = False
        self.is_superuser = False
        self.username = f"apikey:{tenant.tenant_id}"
        self.pk = None
        self.id = None

    def __str__(self):
        return self.username


class APIKeyAuthentication(authentication.BaseAuthentication):
    """
    Authenticates requests using the X-API-Key header.
    The API key must match an active tenant's api_key field.

    Usage:
        curl -H "X-API-Key: <tenant-api-key>" http://host/api/...
    """

    keyword = "X-API-Key"

    def authenticate(self, request):
        api_key = request.META.get("HTTP_X_API_KEY")
        if not api_key:
            return None  # Let other authenticators try

        try:
            tenant = Tenant.objects.get(api_key=api_key, is_active=True)
        except Tenant.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid or inactive API key.")

        user = APIKeyUser(tenant)
        return (user, tenant)

    def authenticate_header(self, request):
        return self.keyword
