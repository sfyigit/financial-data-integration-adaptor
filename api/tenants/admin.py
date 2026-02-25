"""
Django Admin configuration for tenant management models.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import Tenant, TenantMembership, SyncState, SyncLog, ValidationErrorLog


# Re-register User admin to add search_fields (required for autocomplete)
admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    search_fields = ["username", "email", "first_name", "last_name"]


class TenantMembershipInline(admin.TabularInline):
    model = TenantMembership
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["tenant_id", "name", "is_active", "last_sync_at", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["tenant_id", "name"]
    readonly_fields = ["api_key", "created_at", "updated_at"]
    inlines = [TenantMembershipInline]


@admin.register(TenantMembership)
class TenantMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "tenant", "role", "is_default", "created_at"]
    list_filter = ["role", "is_default"]
    search_fields = ["user__username", "tenant__tenant_id"]
    autocomplete_fields = ["user", "tenant"]


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ["tenant", "file_type", "loan_type", "last_version", "last_synced_at"]
    list_filter = ["file_type", "loan_type"]


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = [
        "tenant", "file_type", "loan_type", "status",
        "records_fetched", "records_valid", "records_invalid",
        "started_at", "completed_at",
    ]
    list_filter = ["status", "file_type", "loan_type"]
    readonly_fields = ["started_at"]


@admin.register(ValidationErrorLog)
class ValidationErrorLogAdmin(admin.ModelAdmin):
    list_display = ["sync_log", "row_number", "field_name", "error_type", "error_message"]
    list_filter = ["error_type", "field_name"]
