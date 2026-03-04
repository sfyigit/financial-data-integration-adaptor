"""
Sync Service - Main Orchestrator
=================================
Coordinates the full data synchronization pipeline between
the External Bank API and the ClickHouse Data Warehouse.

Architecture (v3 — PostgreSQL + MinIO):
    1. Check External Bank /version for new data.
    2. If new version detected, get presigned URL for CSV from /files/download.
    3. Download CSV from MinIO via presigned URL (streaming).
    4. Validate all records (field-level + cross-file integrity).
    5. If validation fails → log errors, abort, preserve existing data.
    6. If validation passes → normalize all records (in batches).
    7. Load normalized data into ClickHouse staging table.
    8. Perform atomic swap (staging → production) with tenant+loan_type isolation.
    9. Update sync state and compute data profiling stats.

Key improvements over v2:
    - Downloads CSV directly from MinIO (no cursor pagination needed).
    - loan_type-aware atomic swap (fixes data isolation bug).
    - Streaming CSV processing (constant memory usage for any file size).
"""

import csv
import io
import os
import tempfile
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

# Timeout for downloading CSV files from MinIO (seconds)
DOWNLOAD_TIMEOUT = 300

# Batch size for normalize + ClickHouse load (limits peak memory)
NORMALIZE_BATCH_SIZE = 100_000

# Fallback: max records per /data API call (if MinIO download fails)
CHUNK_SIZE = 50_000
FETCH_TIMEOUT = 120


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
            minio_object_key = target.get("minio_object_key")

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
                    minio_object_key=minio_object_key,
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
        minio_object_key: str = None,
    ) -> dict:
        """
        Sync a single file_type/loan_type combination.
        Implements the all-or-nothing validation pattern.

        Data isolation: only replaces data for the specific tenant+loan_type,
        preserving other loan_types' data for the same tenant.
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
        # Step 1: Fetch data — try MinIO first, fall back to /data API
        # =====================================================================
        records = None
        if minio_object_key:
            try:
                records = self._fetch_data_from_minio(tenant_id, minio_object_key)
                logger.info(
                    f"[{tenant_id}] Downloaded {len(records)} records from MinIO: {minio_object_key}"
                )
            except Exception as e:
                logger.warning(
                    f"[{tenant_id}] MinIO download failed, falling back to /data API: {e}"
                )
                records = None

        if records is None:
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
                    for err in cross_result.errors:
                        validation_result.add_error(err)
                    validation_result.invalid_count += cross_result.invalid_count
            del loan_records

        # =====================================================================
        # Step 3: Handle validation failures (All-or-Nothing)
        # =====================================================================
        if not validation_result.is_valid:
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
        # Steps 4+5: Normalize records in batches + Load into ClickHouse
        # =====================================================================
        table_name = file_type
        total_normalized = 0

        try:
            self.ch_client.create_staging_table(table_name)

            for batch_start in range(0, len(records), NORMALIZE_BATCH_SIZE):
                batch_end = min(batch_start + NORMALIZE_BATCH_SIZE, len(records))
                batch = records[batch_start:batch_end]
                batch_num = batch_start // NORMALIZE_BATCH_SIZE + 1

                if file_type == "loans":
                    normalized_batch = [Normalizer.normalize_loan_record(r) for r in batch]
                else:
                    normalized_batch = [Normalizer.normalize_payment_record(r) for r in batch]

                if file_type == "loans":
                    self.ch_client.load_loans_to_staging(tenant_id, normalized_batch)
                else:
                    self.ch_client.load_payments_to_staging(tenant_id, normalized_batch)

                total_normalized += len(normalized_batch)
                del normalized_batch

                logger.info(
                    f"[{tenant_id}] Normalize+Load batch {batch_num}: "
                    f"{len(batch)} records ({total_normalized}/{len(records)} total)"
                )

        except Exception as e:
            self.ch_client.drop_staging_table(table_name)
            raise RuntimeError(f"Failed to load data into staging: {e}")

        logger.info(f"[{tenant_id}] Normalized+loaded {total_normalized} {file_type} records")

        num_records = len(records)
        del records

        # =====================================================================
        # Step 6: Atomic swap (staging → production) with loan_type isolation
        # =====================================================================
        try:
            # CRITICAL: Pass loan_type to ensure only this loan_type's data is replaced.
            # This preserves other loan_types' data for the same tenant.
            self.ch_client.swap_staging_to_production(table_name, tenant_id, loan_type=loan_type)
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
            f"{total_normalized} records, v{remote_version}"
        )

        return {
            "file_type": file_type,
            "loan_type": loan_type,
            "status": "success",
            "records_synced": total_normalized,
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
        Now includes minio_object_key from the version response.
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
            minio_key = rv.get("minio_object_key")

            if file_type and remote_ft != file_type:
                continue
            if loan_type and remote_lt != loan_type:
                continue

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
                    "minio_object_key": minio_key,
                })

        return targets

    def _fetch_data_from_minio(
        self,
        tenant_id: str,
        minio_object_key: str,
    ) -> list[dict]:
        """
        Download a CSV file from MinIO via the External Bank's presigned URL endpoint,
        then parse it into a list of record dictionaries.

        This is much faster than paginating through /data for large datasets.
        """
        # Get presigned URL from External Bank
        # We'll extract file_type/loan_type from the minio_object_key
        parts = minio_object_key.split("/")
        if len(parts) >= 3:
            eb_file_type = parts[1]
            eb_loan_type = parts[2]
        else:
            raise ValueError(f"Cannot parse minio_object_key: {minio_object_key}")

        url = (
            f"{self.external_bank_url}/files/download"
            f"?tenant_id={tenant_id}"
            f"&file_type={eb_file_type}"
            f"&loan_type={eb_loan_type}"
        )

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            download_info = response.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to get presigned URL: {e}")

        presigned_url = download_info.get("presigned_url")
        if not presigned_url:
            raise RuntimeError("No presigned_url in response")

        # Download the CSV from MinIO
        logger.info(f"[{tenant_id}] Downloading CSV from MinIO ({minio_object_key})...")

        try:
            csv_response = requests.get(presigned_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            csv_response.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to download CSV from MinIO: {e}")

        # Stream CSV to a temp file to avoid holding everything in memory
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="minio_download_")
        try:
            total_bytes = 0
            with os.fdopen(tmp_fd, "wb") as tmp_f:
                for chunk in csv_response.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        tmp_f.write(chunk)
                        total_bytes += len(chunk)

            logger.info(
                f"[{tenant_id}] Downloaded {total_bytes / (1024*1024):.1f} MB from MinIO"
            )

            # Parse CSV
            records = []
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                # Detect delimiter
                first_line = f.readline()
                f.seek(0)
                delimiter = ";" if ";" in first_line else ","

                reader = csv.DictReader(f, delimiter=delimiter)
                for row in reader:
                    if any(v and v.strip() for v in row.values()):
                        cleaned = {
                            k.strip().lower(): (v.strip() if v else "")
                            for k, v in row.items()
                            if k is not None
                        }
                        records.append(cleaned)

            return records

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _fetch_data(
        self,
        tenant_id: str,
        file_type: str,
        loan_type: str,
    ) -> list[dict]:
        """
        Fetch data from the External Bank API via cursor-based pagination.
        This is the fallback method when MinIO download is not available.
        """
        all_records = []
        after_id = 0
        chunk_num = 0

        while True:
            chunk_num += 1
            url = (
                f"{self.external_bank_url}/data"
                f"?tenant_id={tenant_id}"
                f"&file_type={file_type}"
                f"&loan_type={loan_type}"
                f"&limit={CHUNK_SIZE}"
                f"&after_id={after_id}"
            )

            try:
                response = requests.get(url, timeout=FETCH_TIMEOUT)
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as e:
                logger.error(
                    f"[{tenant_id}] Failed to fetch data chunk {chunk_num} "
                    f"(after_id={after_id}): {e}"
                )
                raise RuntimeError(f"Data fetch failed at chunk {chunk_num}: {e}")

            records = data.get("data", [])
            total = data.get("total_records", 0)

            if not records:
                break

            all_records.extend(records)

            last_record = records[-1]
            last_id = last_record.get("id")

            if last_id is not None:
                after_id = last_id
            else:
                logger.warning(
                    f"[{tenant_id}] No 'id' field in response — "
                    f"cursor pagination not supported, fetched {len(all_records)} records"
                )
                break

            logger.info(
                f"[{tenant_id}] Chunk {chunk_num}: fetched {len(records)} records "
                f"(total so far: {len(all_records)}/{total}, last_id={after_id})"
            )

            if len(all_records) >= total:
                break

        logger.info(
            f"[{tenant_id}] Fetched total {len(all_records)} {file_type}/{loan_type} records "
            f"in {chunk_num} chunks"
        )
        return all_records
