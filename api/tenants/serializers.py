"""
Django REST Framework Serializers for tenant management and sync operations.
"""

from rest_framework import serializers
from .models import Tenant, SyncState, SyncLog, ValidationErrorLog


class TenantSerializer(serializers.ModelSerializer):
    """Serializer for Tenant model."""

    class Meta:
        model = Tenant
        fields = [
            "id", "tenant_id", "name", "api_key", "is_active",
            "loan_types", "last_sync_at", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "api_key", "created_at", "updated_at"]


class TenantListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for tenant listing."""
    sync_states_count = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = ["id", "tenant_id", "name", "is_active", "last_sync_at", "sync_states_count"]

    def get_sync_states_count(self, obj):
        return obj.sync_states.count()


class SyncStateSerializer(serializers.ModelSerializer):
    """Serializer for SyncState model."""
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)

    class Meta:
        model = SyncState
        fields = [
            "id", "tenant_id", "file_type", "loan_type",
            "last_version", "last_checksum", "last_synced_at",
        ]


class ValidationErrorLogSerializer(serializers.ModelSerializer):
    """Serializer for ValidationErrorLog model."""

    class Meta:
        model = ValidationErrorLog
        fields = [
            "id", "row_number", "field_name", "error_type",
            "error_message", "raw_value",
        ]


class SyncLogSerializer(serializers.ModelSerializer):
    """Serializer for SyncLog model."""
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)
    validation_errors = ValidationErrorLogSerializer(many=True, read_only=True)

    class Meta:
        model = SyncLog
        fields = [
            "id", "tenant_id", "file_type", "loan_type", "status",
            "records_fetched", "records_valid", "records_invalid",
            "version_before", "version_after", "error_message",
            "started_at", "completed_at", "validation_errors",
        ]


class SyncLogListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for sync log listing (without nested validation errors)."""
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)
    error_count = serializers.SerializerMethodField()

    class Meta:
        model = SyncLog
        fields = [
            "id", "tenant_id", "file_type", "loan_type", "status",
            "records_fetched", "records_valid", "records_invalid",
            "started_at", "completed_at", "error_count",
        ]

    def get_error_count(self, obj):
        return obj.validation_errors.count()


class TriggerSyncSerializer(serializers.Serializer):
    """Serializer for manually triggering a sync."""
    tenant_id = serializers.CharField(help_text="Tenant identifier (e.g., BANK001)")
    file_type = serializers.ChoiceField(
        choices=["loans", "payments"],
        required=False,
        help_text="Specific file type to sync (optional, syncs all if omitted)",
    )
    loan_type = serializers.ChoiceField(
        choices=["RETAIL", "COMMERCIAL"],
        required=False,
        help_text="Specific loan type to sync (optional, syncs all if omitted)",
    )
