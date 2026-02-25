"""
Authentication Views for FSec.

Provides:
  - Session login/logout (for dashboard web UI)
  - User registration with tenant creation
  - JWT token obtain/refresh (for API consumers)
  - Current user profile & tenant info
"""

import logging
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import render, redirect
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Tenant, TenantMembership
from .permissions import get_tenant_for_user, get_user_tenants
from .authentication import APIKeyUser
from .throttling import AuthRateThrottle

logger = logging.getLogger("adapter")


# =============================================================================
# Session-Based Auth (Dashboard Web UI)
# =============================================================================

def session_login_view(request):
    """
    Handles POST login from the dashboard login form.
    On success, redirects to the dashboard.
    On failure, re-renders the login page with an error message.
    """
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            next_url = request.GET.get("next", "/")
            logger.info(f"User '{username}' logged in successfully.")
            return redirect(next_url)
        else:
            from django.shortcuts import render
            logger.warning(f"Failed login attempt for username '{username}'.")
            return render(request, "login.html", {
                "error": "Invalid username or password.",
                "username": username,
            })

    # GET request — show login page
    if request.user.is_authenticated:
        return redirect("/")

    from django.shortcuts import render
    return render(request, "login.html")


def session_logout_view(request):
    """Logs out the current session user and redirects to login."""
    logout(request)
    return redirect("/login/")


# =============================================================================
# User Registration (creates User + Tenant)
# =============================================================================

def register_view(request):
    """
    Handles user registration with automatic tenant creation.

    On POST:
      - Creates a new Django User
      - Creates a new Tenant with the provided tenant_id and tenant_name
      - Does NOT create a TenantMembership (super admin handles that)
      - Logs the user in and redirects to dashboard

    The newly registered user can upload CSV to the external bank for
    any existing tenant (tenant existence is validated at upload time).
    Viewing tenant data in the dashboard requires the super admin to
    associate the user with the tenant via TenantMembership.
    """
    if request.user.is_authenticated:
        return redirect("/")

    context = {}

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        password_confirm = request.POST.get("password_confirm", "")
        tenant_id = request.POST.get("tenant_id", "").strip().upper()
        tenant_name = request.POST.get("tenant_name", "").strip()

        # Preserve form values on error
        context.update({
            "username": username,
            "email": email,
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
        })

        # --- Validation ---
        errors = []

        if not username:
            errors.append("Username is required.")
        elif User.objects.filter(username=username).exists():
            errors.append("This username is already taken.")

        if not email:
            errors.append("Email is required.")
        elif User.objects.filter(email=email).exists():
            errors.append("This email is already registered.")

        if not password:
            errors.append("Password is required.")
        elif password != password_confirm:
            errors.append("Passwords do not match.")
        else:
            try:
                validate_password(password)
            except DjangoValidationError as e:
                errors.extend(e.messages)

        if not tenant_id:
            errors.append("Tenant ID is required.")
        elif Tenant.objects.filter(tenant_id=tenant_id).exists():
            errors.append(f"Tenant ID '{tenant_id}' already exists. Choose a different one.")

        if not tenant_name:
            errors.append("Tenant name is required.")

        if errors:
            context["errors"] = errors
            return render(request, "register.html", context)

        # --- Create User + Tenant ---
        try:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
            )

            tenant = Tenant.objects.create(
                tenant_id=tenant_id,
                name=tenant_name,
                is_active=True,
            )

            logger.info(
                f"New user '{username}' registered with tenant '{tenant_id}' ({tenant_name})."
            )

            # Log user in immediately after registration
            login(request, user)
            return redirect("/")

        except Exception as e:
            logger.error(f"Registration error: {e}", exc_info=True)
            context["errors"] = [f"An unexpected error occurred: {str(e)}"]
            return render(request, "register.html", context)

    return render(request, "register.html", context)


# =============================================================================
# JWT Token Auth (API Consumers)
# =============================================================================

@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthRateThrottle])
def token_obtain_view(request):
    """
    Obtain JWT access + refresh tokens.

    Request body:
        { "username": "...", "password": "..." }

    Response:
        {
            "access": "<jwt-access-token>",
            "refresh": "<jwt-refresh-token>",
            "user": { "id": ..., "username": "...", "email": "..." },
            "tenants": [{ "tenant_id": "...", "name": "...", "role": "..." }]
        }
    """
    username = request.data.get("username", "").strip()
    password = request.data.get("password", "")

    if not username or not password:
        return Response(
            {"error": "Both username and password are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = authenticate(username=username, password=password)
    if user is None:
        return Response(
            {"error": "Invalid credentials."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.is_active:
        return Response(
            {"error": "User account is disabled."},
            status=status.HTTP_403_FORBIDDEN,
        )

    refresh = RefreshToken.for_user(user)

    # Include tenant memberships in token response
    memberships = TenantMembership.objects.filter(user=user).select_related("tenant")
    tenant_list = [
        {
            "tenant_id": m.tenant.tenant_id,
            "name": m.tenant.name,
            "role": m.role,
            "is_default": m.is_default,
        }
        for m in memberships
    ]

    logger.info(f"JWT token issued for user '{username}' with {len(tenant_list)} tenant(s).")

    return Response({
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_staff": user.is_staff,
        },
        "tenants": tenant_list,
    })


@api_view(["POST"])
@permission_classes([AllowAny])
def token_refresh_view(request):
    """
    Refresh an expired access token using a valid refresh token.

    Request body:
        { "refresh": "<jwt-refresh-token>" }

    Response:
        { "access": "<new-jwt-access-token>" }
    """
    refresh_token = request.data.get("refresh")
    if not refresh_token:
        return Response(
            {"error": "Refresh token is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        refresh = RefreshToken(refresh_token)
        return Response({
            "access": str(refresh.access_token),
        })
    except Exception:
        return Response(
            {"error": "Invalid or expired refresh token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )


# =============================================================================
# User Profile & Tenant Info
# =============================================================================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me_view(request):
    """
    Returns the current authenticated user's profile and tenant access.

    Works for both JWT and API Key authenticated requests.
    """
    user = request.user

    if isinstance(user, APIKeyUser):
        return Response({
            "auth_method": "api_key",
            "tenant": {
                "tenant_id": user.tenant.tenant_id,
                "name": user.tenant.name,
            },
        })

    memberships = TenantMembership.objects.filter(user=user).select_related("tenant")
    tenant_list = [
        {
            "tenant_id": m.tenant.tenant_id,
            "name": m.tenant.name,
            "role": m.role,
            "is_default": m.is_default,
        }
        for m in memberships
    ]

    default_tenant = get_tenant_for_user(user)

    return Response({
        "auth_method": "jwt" if "HTTP_AUTHORIZATION" in request.META else "session",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        },
        "default_tenant": {
            "tenant_id": default_tenant.tenant_id,
            "name": default_tenant.name,
        } if default_tenant else None,
        "tenants": tenant_list,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_tenants_view(request):
    """
    Returns a list of tenants the authenticated user has access to.
    For API key users, returns only the key's tenant.
    """
    tenants = get_user_tenants(request.user)
    data = [
        {
            "tenant_id": t.tenant_id,
            "name": t.name,
            "is_active": t.is_active,
            "last_sync_at": t.last_sync_at,
        }
        for t in tenants
    ]
    return Response(data)
