"""
Django Models for Tenant Management and Sync Tracking.

Stored in PostgreSQL for metadata and operational state management.
"""

import uuid
from django.conf import settings
from django.db import models


class Tenant(models.Model):
    """
    Represents a bank/financial institution tenant.
    Each tenant has isolated data access via a unique API key.
    """

    tenant_id = models.CharField(
        max_length=50, unique=True, db_index=True,
        help_text="Unique tenant identifier (e.g., BANK001)",
    )
    name = models.CharField(max_length=200, help_text="Human-readable bank name")
    api_key = models.CharField(
        max_length=128, unique=True, db_index=True,
        default=uuid.uuid4,
        help_text="API key for authentication and tenant isolation",
    )
    is_active = models.BooleanField(default=True, help_text="Whether this tenant is active")
    loan_types = models.JSONField(
        default=list,
        help_text="List of loan types this tenant has (e.g., ['RETAIL', 'COMMERCIAL'])",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True, help_text="Last successful sync timestamp")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant_id"]

    def __str__(self):
        return f"{self.tenant_id} - {self.name}"


class TenantMembership(models.Model):
    """
    Links a Django user to a tenant, defining the user's role within that tenant.
    Supports multi-tenant access: a user can belong to multiple tenants.
    """

    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("analyst", "Analyst"),
        ("viewer", "Viewer"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="viewer")
    is_default = models.BooleanField(
        default=False,
        help_text="If true, this is the user's default tenant on login.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "tenant"]
        ordering = ["user", "tenant"]

    def __str__(self):
        return f"{self.user.username} -> {self.tenant.tenant_id} ({self.role})"


class SyncState(models.Model):
    """
    Tracks the last known data version for each tenant's file_type/loan_type combination.
    Used to detect whether the External Bank has new data to sync.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sync_states")
    file_type = models.CharField(max_length=20, help_text="Data file type: loans or payments")
    loan_type = models.CharField(max_length=20, help_text="Loan type: RETAIL or COMMERCIAL")
    last_version = models.IntegerField(default=0, help_text="Last synced version number")
    last_checksum = models.CharField(max_length=64, blank=True, help_text="Last synced data checksum")
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ["tenant", "file_type", "loan_type"]
        ordering = ["tenant", "file_type", "loan_type"]

    def __str__(self):
        return f"{self.tenant.tenant_id}/{self.file_type}/{self.loan_type} v{self.last_version}"


class SyncLog(models.Model):
    """
    Audit log for each sync operation.
    Tracks success/failure, record counts, and error details.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("success", "Success"),
        ("failed", "Failed"),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sync_logs")
    file_type = models.CharField(max_length=20)
    loan_type = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    records_fetched = models.IntegerField(default=0, help_text="Number of records fetched from bank")
    records_valid = models.IntegerField(default=0, help_text="Number of records passing validation")
    records_invalid = models.IntegerField(default=0, help_text="Number of records failing validation")
    version_before = models.IntegerField(null=True, blank=True, help_text="Version before sync")
    version_after = models.IntegerField(null=True, blank=True, help_text="Version after sync")
    error_message = models.TextField(blank=True, help_text="Error details if sync failed")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.tenant.tenant_id}/{self.file_type}/{self.loan_type} - {self.status}"


class ValidationErrorLog(models.Model):
    """
    Detailed log of validation errors encountered during sync.
    Provides row-level error tracking for debugging.
    """

    sync_log = models.ForeignKey(SyncLog, on_delete=models.CASCADE, related_name="validation_errors")
    row_number = models.IntegerField(null=True, help_text="Row number in the source data")
    field_name = models.CharField(max_length=100, help_text="Name of the field that failed validation")
    error_type = models.CharField(max_length=50, help_text="Type of validation error")
    error_message = models.CharField(max_length=500, help_text="Human-readable error description")
    raw_value = models.CharField(max_length=500, blank=True, help_text="Original value that failed validation")

    class Meta:
        ordering = ["row_number", "field_name"]

    def __str__(self):
        return f"Row {self.row_number}: {self.field_name} - {self.error_type}"
