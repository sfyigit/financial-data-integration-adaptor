"""
Views for the FSec SaaS Platform.
Handles both API endpoints and server-side rendered dashboard pages.

All API endpoints enforce tenant isolation:
  - JWT/Session users can only see tenants they are members of.
  - API Key users can only see the tenant the key belongs to.
  - Superusers can see all tenants.
"""

import logging
import requests as http_requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes, action, throttle_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from .models import Tenant, SyncState, SyncLog, ValidationErrorLog, TenantMembership
from .serializers import (
    TenantSerializer, TenantListSerializer,
    SyncStateSerializer, SyncLogSerializer, SyncLogListSerializer,
    TriggerSyncSerializer,
)
from .permissions import (
    IsTenantMember, IsTenantAdmin, IsAdminOrReadOnly,
    get_tenant_for_user, get_user_tenants,
)
from .authentication import APIKeyUser
from .throttling import BurstRateThrottle

logger = logging.getLogger("adapter")


# =============================================================================
# API ViewSets
# =============================================================================

class TenantViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for tenants.
    Enforces tenant isolation: users only see tenants they belong to.
    Superusers can see and manage all tenants.
    """

    permission_classes = [IsAuthenticated, IsTenantMember]

    def get_queryset(self):
        user = self.request.user

        # Superusers see all tenants
        if hasattr(user, "is_superuser") and user.is_superuser:
            return Tenant.objects.all()

        # API Key users only see their own tenant
        if isinstance(user, APIKeyUser):
            return Tenant.objects.filter(pk=user.tenant.pk)

        # JWT/Session users see tenants they are members of
        accessible = get_user_tenants(user)
        return accessible

    def get_serializer_class(self):
        if self.action == "list":
            return TenantListSerializer
        return TenantSerializer

    def get_permissions(self):
        if self.action in ["create", "destroy"]:
            return [IsAuthenticated(), IsAdminOrReadOnly()]
        return super().get_permissions()

    @action(detail=True, methods=["get"])
    def sync_states(self, request, pk=None):
        """Returns all sync states for a specific tenant."""
        tenant = self.get_object()
        states = SyncState.objects.filter(tenant=tenant)
        serializer = SyncStateSerializer(states, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def sync_logs(self, request, pk=None):
        """Returns sync history for a specific tenant."""
        tenant = self.get_object()
        logs = SyncLog.objects.filter(tenant=tenant)[:50]
        serializer = SyncLogListSerializer(logs, many=True)
        return Response(serializer.data)


class SyncLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only viewset for sync logs.
    Enforces tenant isolation via queryset filtering.
    """

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        if hasattr(user, "is_superuser") and user.is_superuser:
            return SyncLog.objects.all()

        if isinstance(user, APIKeyUser):
            return SyncLog.objects.filter(tenant=user.tenant)

        accessible_tenants = get_user_tenants(user)
        return SyncLog.objects.filter(tenant__in=accessible_tenants)

    def get_serializer_class(self):
        if self.action == "list":
            return SyncLogListSerializer
        return SyncLogSerializer


# =============================================================================
# Sync Trigger API
# =============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BurstRateThrottle])
def trigger_sync(request):
    """
    Manually trigger a data sync for a specific tenant.
    Enforces that the user has access to the requested tenant.
    """
    serializer = TriggerSyncSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    tenant_id = serializer.validated_data["tenant_id"]
    file_type = serializer.validated_data.get("file_type")
    loan_type = serializer.validated_data.get("loan_type")

    try:
        tenant = Tenant.objects.get(tenant_id=tenant_id.upper())
    except Tenant.DoesNotExist:
        return Response(
            {"error": f"Tenant '{tenant_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Enforce tenant isolation
    user = request.user
    if not (hasattr(user, "is_superuser") and user.is_superuser):
        if isinstance(user, APIKeyUser):
            if user.tenant.pk != tenant.pk:
                return Response(
                    {"error": "You do not have access to this tenant."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        else:
            if not TenantMembership.objects.filter(user=user, tenant=tenant).exists():
                return Response(
                    {"error": "You do not have access to this tenant."},
                    status=status.HTTP_403_FORBIDDEN,
                )

    if not tenant.is_active:
        return Response(
            {"error": f"Tenant '{tenant_id}' is not active."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Trigger Celery task
    from adapter.tasks.sync_tasks import run_sync_for_tenant

    task = run_sync_for_tenant.delay(
        tenant_id=tenant.tenant_id,
        file_type=file_type,
        loan_type=loan_type,
    )

    return Response({
        "status": "sync_triggered",
        "tenant_id": tenant.tenant_id,
        "task_id": task.id,
        "file_type": file_type or "all",
        "loan_type": loan_type or "all",
    }, status=status.HTTP_202_ACCEPTED)


# =============================================================================
# Dashboard API (for Charts and Data Tables)
# =============================================================================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_data(request):
    """
    Returns aggregated data for the dashboard.
    Scoped to the tenants the authenticated user has access to.
    """
    user = request.user

    if hasattr(user, "is_superuser") and user.is_superuser:
        tenants = Tenant.objects.filter(is_active=True)
    elif isinstance(user, APIKeyUser):
        tenants = Tenant.objects.filter(pk=user.tenant.pk, is_active=True)
    else:
        tenants = get_user_tenants(user).filter(is_active=True)

    recent_syncs = SyncLog.objects.filter(tenant__in=tenants)[:20]

    # Build per-tenant summary
    tenant_summaries = []
    for tenant in tenants:
        states = SyncState.objects.filter(tenant=tenant)
        recent_log = SyncLog.objects.filter(tenant=tenant).first()

        tenant_summaries.append({
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "last_sync_at": tenant.last_sync_at,
            "sync_states": SyncStateSerializer(states, many=True).data,
            "last_sync_status": recent_log.status if recent_log else None,
        })

    # Get profiling stats from ClickHouse (if available)
    profiling_stats = _get_profiling_stats(tenants)

    return Response({
        "tenants": tenant_summaries,
        "recent_syncs": SyncLogListSerializer(recent_syncs, many=True).data,
        "profiling": profiling_stats,
        "total_tenants": tenants.count(),
        "timestamp": timezone.now(),
    })


def _get_profiling_stats(tenants=None):
    """Fetch profiling statistics from ClickHouse for given tenants."""
    try:
        from adapter.warehouse.clickhouse_client import ClickHouseClient
        ch_client = ClickHouseClient()
        # Extract tenant_id strings from QuerySet for ClickHouse client
        tenant_ids = None
        if tenants is not None:
            tenant_ids = list(tenants.values_list("tenant_id", flat=True))
        return ch_client.get_profiling_summary(tenant_ids)
    except Exception as e:
        logger.warning(f"Could not fetch profiling stats from ClickHouse: {e}")
        return {}


# =============================================================================
# Server-Side Rendered Pages (Django Templates)
# =============================================================================

@login_required(login_url="/login/")
def dashboard_page(request):
    """Main dashboard page - requires session authentication."""
    user = request.user

    if user.is_superuser:
        tenants = Tenant.objects.filter(is_active=True)
    else:
        tenants = get_user_tenants(user).filter(is_active=True)

    recent_syncs = SyncLog.objects.filter(tenant__in=tenants)[:10]

    context = {
        "tenants": tenants,
        "recent_syncs": recent_syncs,
        "total_tenants": tenants.count(),
        "user": user,
    }
    return render(request, "dashboard.html", context)


def login_page(request):
    """Login page — delegates POST to session_login_view."""
    from .auth_views import session_login_view
    return session_login_view(request)


# =============================================================================
# CSV Upload to External Bank
# =============================================================================

@login_required(login_url="/login/")
def upload_csv_page(request):
    """
    Page for uploading CSV files to the External Bank service.

    On POST:
      - Validates that the given tenant_id exists in the Django database
      - Forwards the CSV file to the External Bank API (FastAPI)
      - Returns success/error result

    Tenant list is filtered by user role:
      - Superusers see all active tenants
      - Normal users see only tenants they are members of
    """
    user = request.user
    if user.is_superuser:
        tenants = Tenant.objects.filter(is_active=True).order_by("tenant_id")
    else:
        tenants = get_user_tenants(user).filter(is_active=True).order_by("tenant_id")

    context = {
        "tenants": tenants,
    }

    if request.method == "POST":
        tenant_id = request.POST.get("tenant_id", "").strip().upper()
        file_type = request.POST.get("file_type", "").strip().lower()
        loan_type = request.POST.get("loan_type", "").strip().upper()
        csv_file = request.FILES.get("csv_file")

        # Preserve form values on error
        context.update({
            "selected_tenant_id": tenant_id,
            "selected_file_type": file_type,
            "selected_loan_type": loan_type,
        })

        # --- Validation ---
        errors = []

        if not tenant_id:
            errors.append("Tenant ID is required.")
        else:
            try:
                target_tenant = Tenant.objects.get(tenant_id=tenant_id)
                # Normal users can only upload to their own tenants
                if not user.is_superuser:
                    if not TenantMembership.objects.filter(
                        user=user, tenant=target_tenant
                    ).exists():
                        errors.append(
                            f"You do not have access to tenant '{tenant_id}'. "
                            f"Ask an admin to assign you to this tenant."
                        )
            except Tenant.DoesNotExist:
                errors.append(
                    f"Tenant '{tenant_id}' does not exist. "
                    f"Please register a tenant first or choose an existing one."
                )

        if file_type not in ("loans", "payments"):
            errors.append("File type must be 'loans' or 'payments'.")

        if loan_type not in ("RETAIL", "COMMERCIAL"):
            errors.append("Loan type must be 'RETAIL' or 'COMMERCIAL'.")

        if not csv_file:
            errors.append("Please select a CSV file to upload.")
        elif not csv_file.name.endswith(".csv"):
            errors.append("Only CSV files (.csv) are accepted.")

        if errors:
            context["errors"] = errors
            return render(request, "upload_csv.html", context)

        # --- Forward to External Bank API ---
        try:
            external_bank_url = settings.EXTERNAL_BANK_URL
            upload_url = f"{external_bank_url}/upload"

            # Dynamic timeout & streaming for large files (200MB+)
            LARGE_FILE_THRESHOLD = 200 * 1024 * 1024  # 200MB
            file_size = csv_file.size

            if file_size > LARGE_FILE_THRESHOLD:
                # Large file: stream directly, generous timeout (~5s per MB for DB processing, min 600s)
                upload_timeout = max(600, int(file_size / (1024 * 1024)) * 5)
                csv_file.seek(0)
                logger.info(
                    f"Large file upload ({file_size / (1024*1024):.1f} MB) "
                    f"for {tenant_id}/{file_type}/{loan_type} - streaming with {upload_timeout}s timeout"
                )
                response = http_requests.post(
                    upload_url,
                    params={
                        "tenant_id": tenant_id,
                        "file_type": file_type,
                        "loan_type": loan_type,
                    },
                    files={
                        "file": (csv_file.name, csv_file, "text/csv"),
                    },
                    timeout=upload_timeout,
                )
            else:
                # Normal file: read into memory
                file_content = csv_file.read()
                response = http_requests.post(
                    upload_url,
                    params={
                        "tenant_id": tenant_id,
                        "file_type": file_type,
                        "loan_type": loan_type,
                    },
                    files={
                        "file": (csv_file.name, file_content, "text/csv"),
                    },
                    timeout=60,
                )

            if response.status_code == 200:
                result = response.json()
                context["success"] = True
                context["result"] = result
                logger.info(
                    f"CSV upload successful: {tenant_id}/{file_type}/{loan_type} "
                    f"- {result.get('records_processed', 0)} records, "
                    f"version: {result.get('version', '?')}"
                )
            else:
                try:
                    error_detail = response.json().get("detail", response.text)
                except Exception:
                    error_detail = response.text
                context["errors"] = [
                    f"External Bank returned error ({response.status_code}): {error_detail}"
                ]
                logger.warning(
                    f"CSV upload failed for {tenant_id}/{file_type}/{loan_type}: "
                    f"{response.status_code} - {error_detail}"
                )

        except http_requests.exceptions.ConnectionError:
            context["errors"] = [
                "Could not connect to the External Bank service. "
                "Please ensure the service is running."
            ]
            logger.error("External Bank service is unreachable.")
        except http_requests.exceptions.Timeout:
            context["errors"] = ["External Bank service timed out. Please try again."]
            logger.error("External Bank service timed out.")
        except Exception as e:
            context["errors"] = [f"Unexpected error: {str(e)}"]
            logger.error(f"CSV upload unexpected error: {e}", exc_info=True)

    return render(request, "upload_csv.html", context)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_data_tables(request):
    """
    API endpoint for browsing ClickHouse data (loans & payments).

    Query params:
        - tenant_id: required
        - table: 'loans' or 'payments' (default: 'loans')
        - loan_type: optional filter (RETAIL / COMMERCIAL)
        - limit: records per page (default: 50, max: 200)
        - offset: pagination offset (default: 0)
    """
    user = request.user
    tenant_id = request.query_params.get("tenant_id", "").strip().upper()
    table = request.query_params.get("table", "loans").strip().lower()
    loan_type = request.query_params.get("loan_type", "").strip().upper() or None

    try:
        limit = min(int(request.query_params.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except (ValueError, TypeError):
        offset = 0

    if not tenant_id:
        return Response({"error": "tenant_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Check tenant exists
    try:
        tenant = Tenant.objects.get(tenant_id=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": f"Tenant '{tenant_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

    # Enforce tenant isolation
    if not (hasattr(user, "is_superuser") and user.is_superuser):
        if isinstance(user, APIKeyUser):
            if user.tenant.pk != tenant.pk:
                return Response({"error": "Access denied."}, status=status.HTTP_403_FORBIDDEN)
        else:
            if not TenantMembership.objects.filter(user=user, tenant=tenant).exists():
                return Response({"error": "Access denied."}, status=status.HTTP_403_FORBIDDEN)

    try:
        from adapter.warehouse.clickhouse_client import ClickHouseClient
        ch = ClickHouseClient()

        if table == "payments":
            data = ch.get_payments(tenant_id, loan_type=loan_type, limit=limit, offset=offset)
        else:
            data = ch.get_loans(tenant_id, loan_type=loan_type, limit=limit, offset=offset)

        data["tenant_id"] = tenant_id
        data["table"] = table
        return Response(data)

    except Exception as e:
        logger.error(f"Data tables query error: {e}", exc_info=True)
        message = (
            "ClickHouse query failed."
            if not settings.DEBUG
            else f"ClickHouse query failed: {str(e)}"
        )
        return Response(
            {"error": message},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@login_required(login_url="/login/")
def data_tables_page(request):
    """
    Server-side rendered page for browsing ClickHouse data.
    Users can pick a tenant and view loan/payment records with pagination.
    """
    user = request.user
    if user.is_superuser:
        tenants = Tenant.objects.filter(is_active=True).order_by("tenant_id")
    else:
        tenants = get_user_tenants(user).filter(is_active=True).order_by("tenant_id")

    context = {"tenants": tenants}
    return render(request, "data_tables.html", context)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_upload_csv(request):
    """
    API endpoint for uploading CSV to External Bank.

    Validates that the tenant_id exists in Django DB, then forwards
    the CSV file to the External Bank service.

    Request (multipart/form-data):
        - tenant_id: str (e.g. "BANK001")
        - file_type: str ("loans" or "payments")
        - loan_type: str ("RETAIL" or "COMMERCIAL")
        - file: CSV file

    No user-tenant membership check is performed; only tenant existence.
    """
    tenant_id = request.data.get("tenant_id", "").strip().upper()
    file_type = request.data.get("file_type", "").strip().lower()
    loan_type = request.data.get("loan_type", "").strip().upper()
    csv_file = request.FILES.get("file")

    # --- Validation ---
    if not tenant_id:
        return Response({"error": "tenant_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        tenant = Tenant.objects.get(tenant_id=tenant_id)
    except Tenant.DoesNotExist:
        return Response(
            {"error": f"Tenant '{tenant_id}' does not exist."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if file_type not in ("loans", "payments"):
        return Response(
            {"error": "file_type must be 'loans' or 'payments'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if loan_type not in ("RETAIL", "COMMERCIAL"):
        return Response(
            {"error": "loan_type must be 'RETAIL' or 'COMMERCIAL'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not csv_file:
        return Response({"error": "CSV file is required."}, status=status.HTTP_400_BAD_REQUEST)

    if not csv_file.name.lower().endswith(".csv"):
        return Response({"error": "Only CSV files are accepted."}, status=status.HTTP_400_BAD_REQUEST)

    # Basic content type and size checks (defense in depth)
    max_size_bytes = 1024 * 1024 * 1024  # 1GB hard cap
    if csv_file.size > max_size_bytes:
        return Response(
            {"error": "Uploaded file is too large."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Enforce tenant isolation for API uploads as well
    user = request.user
    if not (hasattr(user, "is_superuser") and user.is_superuser):
        if isinstance(user, APIKeyUser):
            if user.tenant.pk != tenant.pk:
                return Response(
                    {"error": "You do not have access to this tenant."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        else:
            if not TenantMembership.objects.filter(user=user, tenant=tenant).exists():
                return Response(
                    {"error": "You do not have access to this tenant."},
                    status=status.HTTP_403_FORBIDDEN,
                )

    # --- Forward to External Bank ---
    try:
        external_bank_url = settings.EXTERNAL_BANK_URL

        # Dynamic timeout & streaming for large files (200MB+)
        LARGE_FILE_THRESHOLD = 200 * 1024 * 1024  # 200MB
        file_size = csv_file.size

        if file_size > LARGE_FILE_THRESHOLD:
            # Large file: stream directly, generous timeout (~5s per MB for DB processing, min 600s)
            upload_timeout = max(600, int(file_size / (1024 * 1024)) * 5)
            csv_file.seek(0)
            logger.info(
                f"Large file API upload ({file_size / (1024*1024):.1f} MB) "
                f"for {tenant_id}/{file_type}/{loan_type} - streaming with {upload_timeout}s timeout"
            )
            response = http_requests.post(
                f"{external_bank_url}/upload",
                params={
                    "tenant_id": tenant_id,
                    "file_type": file_type,
                    "loan_type": loan_type,
                },
                files={"file": (csv_file.name, csv_file, "text/csv")},
                timeout=upload_timeout,
            )
        else:
            file_content = csv_file.read()
            response = http_requests.post(
                f"{external_bank_url}/upload",
                params={
                    "tenant_id": tenant_id,
                    "file_type": file_type,
                    "loan_type": loan_type,
                },
                files={"file": (csv_file.name, file_content, "text/csv")},
                timeout=60,
            )

        if response.status_code == 200:
            return Response(response.json(), status=status.HTTP_200_OK)
        else:
            try:
                error_detail = response.json().get("detail", response.text)
            except Exception:
                error_detail = response.text
            return Response(
                {"error": f"External Bank error: {error_detail}"},
                status=response.status_code,
            )

    except http_requests.exceptions.ConnectionError:
        return Response(
            {"error": "Could not connect to External Bank service."},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except Exception as e:
        logger.error(f"CSV API upload unexpected error: {e}", exc_info=True)
        message = (
            "Unexpected server error during CSV upload."
            if not settings.DEBUG
            else f"Unexpected error: {str(e)}"
        )
        return Response(
            {"error": message},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
