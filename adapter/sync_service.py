"""
Sync Service - Main Orchestrator
=================================
Coordinates the full data synchronization pipeline between
the External Bank API and the ClickHouse Data Warehouse.

Pipeline:
1. Fetch data from External Bank API (with chunked reading for large files).
2. Validate all records (field-level + cross-file integrity).
3. If validation fails -> log errors, abort, preserve existing data.
4. If validation passes -> normalize all records.
5. Load normalized data into ClickHouse staging table.
6. Perform atomic swap (staging -> production).
7. Update sync state and compute data profiling stats.
"""

import io
import csv
import logging
from datetime import datetime
from typing import Optional

import requests
from django.conf import settings
from django.utils import timezone

from adapter.core.normalizer import Normalizer
from adapter.core.validator import Validator, ValidationResult
from adapter.warehouse.clickhouse_client import ClickHouseClient

logger = logging.getLogger("adapter.sync_service")

# Maximum number of records to fetch per API call (for chunked processing)
CHUNK_SIZE = 10_000


class SyncService:
    """
    Orchestrates data synchronization from External Bank to ClickHouse.
    Implements all-or-nothing validation with atomic data replacement.
    """

    def __init__(self):
        self.external_bank_url = getattr(settings, "EXTERNAL_BANK_URL", "http://external_bank:8000")
        self.ch_client = ClickHouseClient()
        # Ensure ClickHouse tables exist
        try:
            self.ch_client.initialize()
        except Exception as e:
            logger.warning(f"ClickHouse initialization warning: {e}")

    def sync_tenant(
        self,
        tenant_id: str,
        file_type: str = None,
        loan_type: str = None,
    ) -> dict:
        """
        Run the full sync pipeline for a tenant.

        Args:
            tenant_id: Tenant identifier (e.g., BANK001).
            file_type: Optional - sync only 'loans' or 'payments'.
            loan_type: Optional - sync only 'RETAIL' or 'COMMERCIAL'.

        Returns:
            Dictionary with sync results.
        """
        from tenants.models import Tenant, SyncState, SyncLog

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            logger.error(f"Tenant '{tenant_id}' not found")
            return {"status": "error", "message": f"Tenant '{tenant_id}' not found"}

        # Determine which file_type/loan_type combinations to sync
        sync_targets = self._get_sync_targets(tenant_id, file_type, loan_type)

        if not sync_targets:
            logger.info(f"[{tenant_id}] No sync targets found")
            return {"status": "no_data", "tenant_id": tenant_id}

        results = []

        for target in sync_targets:
            target_file_type = target["file_type"]
            target_loan_type = target["loan_type"]
            remote_version = target["version"]

            # Create sync log entry
            sync_log = SyncLog.objects.create(
                tenant=tenant,
                file_type=target_file_type,
                loan_type=target_loan_type,
                status="running",
            )

            try:
                result = self._sync_single(
                    tenant=tenant,
                    file_type=target_file_type,
                    loan_type=target_loan_type,
                    remote_version=remote_version,
                    sync_log=sync_log,
                )
                results.append(result)

            except Exception as e:
                sync_log.status = "failed"
                sync_log.error_message = str(e)
                sync_log.completed_at = timezone.now()
                sync_log.save()
                logger.error(
                    f"[{tenant_id}] Sync failed for {target_file_type}/{target_loan_type}: {e}"
                )
                results.append({
                    "file_type": target_file_type,
                    "loan_type": target_loan_type,
                    "status": "failed",
                    "error": str(e),
                })

        # Update tenant's last_sync_at
        tenant.last_sync_at = timezone.now()
        tenant.save()

        return {
            "status": "completed",
            "tenant_id": tenant_id,
            "results": results,
        }

    def _sync_single(
        self,
        tenant,
        file_type: str,
        loan_type: str,
        remote_version: int,
        sync_log,
    ) -> dict:
        """
        Sync a single file_type/loan_type combination.
        Implements the all-or-nothing validation pattern.
        """
        from tenants.models import SyncState, ValidationErrorLog

        tenant_id = tenant.tenant_id

        logger.info(f"[{tenant_id}] Syncing {file_type}/{loan_type} (remote v{remote_version})")

        # Get current local version
        local_state, _ = SyncState.objects.get_or_create(
            tenant=tenant,
            file_type=file_type,
            loan_type=loan_type,
            defaults={"last_version": 0},
        )
        sync_log.version_before = local_state.last_version
        sync_log.save()

        # =====================================================================
        # Step 1: Fetch data from External Bank API
        # =====================================================================
        records = self._fetch_data(tenant_id, file_type, loan_type)
        sync_log.records_fetched = len(records)
        sync_log.save()

        if not records:
            sync_log.status = "success"
            sync_log.completed_at = timezone.now()
            sync_log.save()
            logger.info(f"[{tenant_id}] No records to sync for {file_type}/{loan_type}")
            return {"file_type": file_type, "loan_type": loan_type, "status": "no_data"}

        # =====================================================================
        # Step 2: Validate records
        # =====================================================================
        # Add loan_type to each record for processing
        for r in records:
            r["loan_type"] = loan_type

        if file_type == "loans":
            validation_result = Validator.validate_loans(records)
        else:
            validation_result = Validator.validate_payments(records)

        sync_log.records_valid = validation_result.valid_count
        sync_log.records_invalid = validation_result.invalid_count
        sync_log.save()

        # Cross-file integrity check (if syncing payments, verify loan IDs exist)
        if file_type == "payments":
            loan_records = self._fetch_data(tenant_id, "loans", loan_type)
            if loan_records:
                cross_result = Validator.validate_cross_file_integrity(loan_records, records)
                if not cross_result.is_valid:
                    # Add cross-file errors to the validation result
                    for err in cross_result.errors:
                        validation_result.add_error(err)
                    validation_result.invalid_count += cross_result.invalid_count

        # =====================================================================
        # Step 3: Handle validation failures (All-or-Nothing)
        # =====================================================================
        if not validation_result.is_valid:
            # Log all validation errors
            for err in validation_result.errors:
                ValidationErrorLog.objects.create(
                    sync_log=sync_log,
                    row_number=err.row_number,
                    field_name=err.field_name,
                    error_type=err.error_type,
                    error_message=err.error_message,
                    raw_value=err.raw_value,
                )

            sync_log.status = "failed"
            sync_log.error_message = (
                f"Validation failed: {validation_result.total_errors} errors found. "
                f"Existing data preserved (all-or-nothing policy)."
            )
            sync_log.completed_at = timezone.now()
            sync_log.save()

            logger.warning(
                f"[{tenant_id}] Sync aborted for {file_type}/{loan_type}: "
                f"{validation_result.total_errors} validation errors. "
                f"Existing data preserved."
            )

            return {
                "file_type": file_type,
                "loan_type": loan_type,
                "status": "validation_failed",
                "errors": validation_result.total_errors,
                "records_fetched": len(records),
            }

        # =====================================================================
        # Step 4: Normalize records
        # =====================================================================
        if file_type == "loans":
            normalized = [Normalizer.normalize_loan_record(r) for r in records]
        else:
            normalized = [Normalizer.normalize_payment_record(r) for r in records]

        logger.info(f"[{tenant_id}] Normalized {len(normalized)} {file_type} records")

        # =====================================================================
        # Step 5: Load into ClickHouse staging table
        # =====================================================================
        table_name = file_type  # 'loans' or 'payments'

        try:
            self.ch_client.create_staging_table(table_name)

            if file_type == "loans":
                self.ch_client.load_loans_to_staging(tenant_id, normalized)
            else:
                self.ch_client.load_payments_to_staging(tenant_id, normalized)

        except Exception as e:
            # Clean up staging on failure
            self.ch_client.drop_staging_table(table_name)
            raise RuntimeError(f"Failed to load data into staging: {e}")

        # =====================================================================
        # Step 6: Atomic swap (staging -> production)
        # =====================================================================
        try:
            self.ch_client.swap_staging_to_production(table_name, tenant_id)
        except Exception as e:
            raise RuntimeError(f"Atomic swap failed: {e}")

        # =====================================================================
        # Step 7: Update sync state and compute profiling
        # =====================================================================
        local_state.last_version = remote_version
        local_state.last_synced_at = timezone.now()
        local_state.save()

        sync_log.status = "success"
        sync_log.version_after = remote_version
        sync_log.completed_at = timezone.now()
        sync_log.save()

        # Compute data profiling stats (non-blocking)
        try:
            if file_type == "loans":
                profile = self.ch_client.compute_loan_profile(tenant_id)
            else:
                profile = self.ch_client.compute_payment_profile(tenant_id)
            logger.info(f"[{tenant_id}] Profiling completed for {file_type}")
        except Exception as e:
            logger.warning(f"[{tenant_id}] Profiling failed (non-critical): {e}")

        logger.info(
            f"[{tenant_id}] Sync completed for {file_type}/{loan_type}: "
            f"{len(normalized)} records, v{remote_version}"
        )

        return {
            "file_type": file_type,
            "loan_type": loan_type,
            "status": "success",
            "records_synced": len(normalized),
            "version": remote_version,
        }

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _get_sync_targets(
        self,
        tenant_id: str,
        file_type: str = None,
        loan_type: str = None,
    ) -> list[dict]:
        """
        Determine which file_type/loan_type combinations need syncing.
        Queries the External Bank API version endpoint and compares
        with local sync state.
        """
        from tenants.models import Tenant, SyncState

        url = f"{self.external_bank_url}/version?tenant_id={tenant_id}"

        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"[{tenant_id}] Failed to fetch version info: {e}")
            return []

        targets = []
        tenant = Tenant.objects.get(tenant_id=tenant_id)

        for rv in data.get("versions", []):
            remote_ft = rv["file_type"]
            remote_lt = rv["loan_type"]
            remote_ver = rv["version"]

            # Filter if specific file_type/loan_type requested
            if file_type and remote_ft != file_type:
                continue
            if loan_type and remote_lt != loan_type:
                continue

            # Check if we need to sync
            local_state, _ = SyncState.objects.get_or_create(
                tenant=tenant,
                file_type=remote_ft,
                loan_type=remote_lt,
                defaults={"last_version": 0},
            )

            if remote_ver > local_state.last_version:
                targets.append({
                    "file_type": remote_ft,
                    "loan_type": remote_lt,
                    "version": remote_ver,
                })

        return targets

    def _fetch_data(
        self,
        tenant_id: str,
        file_type: str,
        loan_type: str,
    ) -> list[dict]:
        """
        Fetch data from the External Bank API.
        Uses pagination/chunking for large datasets to avoid memory overflow.

        Args:
            tenant_id: Tenant identifier.
            file_type: 'loans' or 'payments'.
            loan_type: 'RETAIL' or 'COMMERCIAL'.

        Returns:
            List of record dictionaries.
        """
        all_records = []
        offset = 0

        while True:
            url = (
                f"{self.external_bank_url}/data"
                f"?tenant_id={tenant_id}"
                f"&file_type={file_type}"
                f"&loan_type={loan_type}"
                f"&limit={CHUNK_SIZE}"
                f"&offset={offset}"
            )

            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as e:
                logger.error(f"[{tenant_id}] Failed to fetch data chunk: {e}")
                raise RuntimeError(f"Data fetch failed: {e}")

            records = data.get("data", [])
            total = data.get("total_records", 0)

            all_records.extend(records)

            logger.debug(
                f"[{tenant_id}] Fetched {len(records)} records "
                f"(offset={offset}, total={total})"
            )

            # Check if we have all records
            if len(all_records) >= total or not records:
                break

            offset += CHUNK_SIZE

        logger.info(
            f"[{tenant_id}] Fetched total {len(all_records)} {file_type}/{loan_type} records"
        )
        return all_records
