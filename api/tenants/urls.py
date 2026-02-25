"""
URL Configuration for tenant management, authentication, and dashboard.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from . import auth_views

router = DefaultRouter()
router.register(r"tenants", views.TenantViewSet, basename="tenant")
router.register(r"sync-logs", views.SyncLogViewSet, basename="sync-log")

urlpatterns = [
    # --- Server-side rendered pages ---
    path("", views.dashboard_page, name="dashboard"),
    path("login/", views.login_page, name="login"),
    path("logout/", auth_views.session_logout_view, name="logout"),
    path("register/", auth_views.register_view, name="register"),
    path("upload/", views.upload_csv_page, name="upload-csv"),
    path("data/", views.data_tables_page, name="data-tables"),

    # --- REST API endpoints ---
    path("api/", include(router.urls)),
    path("api/trigger-sync/", views.trigger_sync, name="trigger-sync"),
    path("api/dashboard-data/", views.dashboard_data, name="dashboard-data"),
    path("api/upload-csv/", views.api_upload_csv, name="api-upload-csv"),
    path("api/data-tables/", views.api_data_tables, name="api-data-tables"),

    # --- JWT Auth endpoints ---
    path("api/auth/token/", auth_views.token_obtain_view, name="token-obtain"),
    path("api/auth/token/refresh/", auth_views.token_refresh_view, name="token-refresh"),
    path("api/auth/me/", auth_views.me_view, name="auth-me"),
    path("api/auth/my-tenants/", auth_views.my_tenants_view, name="auth-my-tenants"),
]
