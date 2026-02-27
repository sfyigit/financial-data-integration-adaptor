"""
Celery Tasks for Periodic Data Synchronization
================================================
Background tasks that:
1. Periodically check the External Bank API for new data (Celery Beat).
2. Run full sync pipelines when new data is detected.
"""

import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("adapter.tasks")


@shared_task(name="adapter.tasks.sync_tasks.check_for_updates")
def check_for_updates():
    """
    Periodic task (triggered by Celery Beat) that checks all active tenants
    for new data in the External Bank API.

    For each tenant, it compares the remote version with the locally stored version.
    If a newer version is detected, it triggers a sync task.

    Skips tenants that already have a running sync to prevent duplicate tasks
    from consuming excessive memory.
    """
    from tenants.models import Tenant, SyncLog
    logger.info("=== Starting periodic update check ===")

    active_tenants = Tenant.objects.filter(is_active=True)

    if not active_tenants.exists():
        logger.info("No active tenants found. Skipping update check.")
        return {"status": "no_tenants"}

    triggered_count = 0
    skipped_count = 0

    for tenant in active_tenants:
        try:
            # ── Guard: skip if a sync is already running for this tenant ──
            running_syncs = SyncLog.objects.filter(
                tenant=tenant,
                status="running",
            ).count()
            if running_syncs > 0:
                logger.info(
                    f"[{tenant.tenant_id}] Skipping — {running_syncs} sync(s) "
                    f"already running"
                )
                skipped_count += 1
                continue

            has_updates = _check_tenant_updates(tenant)
            if has_updates:
                # Trigger async sync
                run_sync_for_tenant.delay(tenant_id=tenant.tenant_id)
                triggered_count += 1
                logger.info(f"[{tenant.tenant_id}] New data detected - sync triggered")
            else:
                logger.debug(f"[{tenant.tenant_id}] No new data")
        except Exception as e:
            logger.error(f"[{tenant.tenant_id}] Update check failed: {e}")

    logger.info(
        f"=== Update check completed: {triggered_count} syncs triggered, "
        f"{skipped_count} skipped (already running) ==="
    )
    return {
        "status": "completed",
        "syncs_triggered": triggered_count,
        "skipped": skipped_count,
    }


@shared_task(
    name="adapter.tasks.sync_tasks.run_sync_for_tenant",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_sync_for_tenant(self, tenant_id: str, file_type: str = None, loan_type: str = None):
    """
    Run the full sync pipeline for a specific tenant.

    This task:
    1. Fetches data from the External Bank API.
    2. Validates the data (field-level + cross-file integrity).
    3. Normalizes valid records (in memory-efficient batches).
    4. Loads into ClickHouse staging tables.
    5. Performs atomic swap if all validations pass.
    6. Computes data profiling statistics.

    Includes a distributed lock: only one sync per tenant at a time.

    Args:
        tenant_id: Tenant identifier (e.g., BANK001).
        file_type: Optional specific file type to sync (loans/payments).
        loan_type: Optional specific loan type to sync (RETAIL/COMMERCIAL).
    """
    from adapter.sync_service import SyncService
    from tenants.models import SyncLog

    logger.info(
        f"[{tenant_id}] Starting sync task "
        f"(file_type={file_type}, loan_type={loan_type}, "
        f"attempt={self.request.retries + 1}/{self.max_retries + 1})"
    )

    # ── Guard: prevent concurrent syncs for the same tenant ──
    # Only check on first attempt (retries should be allowed)
    if self.request.retries == 0:
        running_count = SyncLog.objects.filter(
            tenant__tenant_id=tenant_id,
            status="running",
        ).count()
        if running_count > 0:
            logger.warning(
                f"[{tenant_id}] Another sync is already running "
                f"({running_count} active). Skipping this task."
            )
            return {
                "status": "skipped",
                "tenant_id": tenant_id,
                "reason": "concurrent_sync_running",
            }

    try:
        service = SyncService()
        result = service.sync_tenant(
            tenant_id=tenant_id,
            file_type=file_type,
            loan_type=loan_type,
        )
        logger.info(f"[{tenant_id}] Sync task completed: {result}")
        return result

    except Exception as e:
        logger.error(f"[{tenant_id}] Sync task failed (attempt {self.request.retries + 1}): {e}")

        # If we've exhausted all retries, mark any stuck sync_logs as failed
        if self.request.retries >= self.max_retries:
            logger.error(f"[{tenant_id}] All retries exhausted. Marking stuck sync logs as failed.")
            _mark_stuck_synclogs_failed(tenant_id, str(e))
            return {
                "status": "failed",
                "tenant_id": tenant_id,
                "error": str(e),
                "retries_exhausted": True,
            }

        # Retry on transient failures
        raise self.retry(exc=e)


def _mark_stuck_synclogs_failed(tenant_id: str, error_message: str):
    """Mark all 'running' sync logs for a tenant as 'failed' — cleanup for stuck tasks."""
    try:
        from tenants.models import SyncLog
        stuck_logs = SyncLog.objects.filter(
            tenant__tenant_id=tenant_id,
            status="running",
        )
        count = stuck_logs.update(
            status="failed",
            error_message=f"Task failed after max retries: {error_message}",
            completed_at=timezone.now(),
        )
        if count:
            logger.info(f"[{tenant_id}] Marked {count} stuck sync logs as failed")
    except Exception as cleanup_err:
        logger.error(f"[{tenant_id}] Failed to cleanup stuck sync logs: {cleanup_err}")


def _check_tenant_updates(tenant) -> bool:
    """
    Check if a tenant has new data available in the External Bank API.

    Compares remote version numbers with locally stored versions.

    Args:
        tenant: Tenant model instance.

    Returns:
        True if new data is available, False otherwise.
    """
    import requests
    from django.conf import settings
    from tenants.models import SyncState

    external_bank_url = settings.EXTERNAL_BANK_URL
    url = f"{external_bank_url}/version?tenant_id={tenant.tenant_id}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        logger.warning(f"[{tenant.tenant_id}] Could not reach External Bank API: {e}")
        return False

    remote_versions = data.get("versions", [])

    for rv in remote_versions:
        remote_file_type = rv["file_type"]
        remote_loan_type = rv["loan_type"]
        remote_version = rv["version"]
        remote_checksum = rv.get("checksum", "")

        # Get or create local sync state
        local_state, _ = SyncState.objects.get_or_create(
            tenant=tenant,
            file_type=remote_file_type,
            loan_type=remote_loan_type,
            defaults={"last_version": 0, "last_checksum": ""},
        )

        # Compare versions
        if remote_version > local_state.last_version:
            logger.info(
                f"[{tenant.tenant_id}] New data: {remote_file_type}/{remote_loan_type} "
                f"v{local_state.last_version} -> v{remote_version}"
            )
            return True

        # Also check checksum for same-version changes
        if remote_checksum and remote_checksum != local_state.last_checksum:
            logger.info(
                f"[{tenant.tenant_id}] Checksum mismatch for "
                f"{remote_file_type}/{remote_loan_type} v{remote_version}"
            )
            return True

    return False
