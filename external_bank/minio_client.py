"""
MinIO S3 Client - External Bank Simulation
============================================
Handles all interactions with MinIO object storage.

File organization:
    {bucket}/
        {tenant_id}/
            {file_type}/
                {loan_type}/
                    v{version}_{filename}

Provides:
    - Upload CSV files (streaming)
    - Generate presigned download URLs (for sync service)
    - List/delete objects
"""

import os
import io
import logging
from datetime import timedelta
from minio import Minio
from minio.error import S3Error

logger = logging.getLogger("external_bank.minio")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "fsec_minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "fsec_minio_secret_2026")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "fsec-data")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"

_client = None


def get_minio_client() -> Minio:
    """Get or create MinIO client singleton."""
    global _client
    if _client is None:
        _client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
    return _client


def ensure_bucket():
    """Create the default bucket if it doesn't exist."""
    client = get_minio_client()
    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)
        logger.info(f"Created MinIO bucket: {MINIO_BUCKET}")


def build_object_key(
    tenant_id: str,
    file_type: str,
    loan_type: str,
    version: int,
    filename: str,
) -> str:
    """
    Build a structured object key for MinIO storage.

    Format: {tenant_id}/{file_type}/{loan_type}/v{version}_{filename}
    Example: BANK001/loans/RETAIL/v3_retail_loans.csv
    """
    safe_filename = filename.replace(" ", "_")
    return f"{tenant_id}/{file_type}/{loan_type}/v{version}_{safe_filename}"


def upload_file(
    object_key: str,
    file_path: str,
    content_type: str = "text/csv",
) -> dict:
    """
    Upload a file from disk to MinIO.

    Args:
        object_key: The S3 object key (path within the bucket).
        file_path: Local file path to upload.
        content_type: MIME type of the file.

    Returns:
        Dictionary with upload metadata (bucket, key, etag, size).
    """
    client = get_minio_client()
    file_size = os.path.getsize(file_path)

    result = client.fput_object(
        MINIO_BUCKET,
        object_key,
        file_path,
        content_type=content_type,
    )

    logger.info(
        f"Uploaded to MinIO: {object_key} "
        f"({file_size / (1024*1024):.1f} MB, etag={result.etag})"
    )

    return {
        "bucket": MINIO_BUCKET,
        "object_key": object_key,
        "etag": result.etag,
        "size": file_size,
    }


def upload_bytes(
    object_key: str,
    data: bytes,
    content_type: str = "text/csv",
) -> dict:
    """
    Upload bytes data to MinIO.

    Args:
        object_key: The S3 object key.
        data: Raw bytes to upload.
        content_type: MIME type.

    Returns:
        Dictionary with upload metadata.
    """
    client = get_minio_client()
    data_stream = io.BytesIO(data)
    data_size = len(data)

    result = client.put_object(
        MINIO_BUCKET,
        object_key,
        data_stream,
        length=data_size,
        content_type=content_type,
    )

    logger.info(
        f"Uploaded to MinIO: {object_key} "
        f"({data_size / (1024*1024):.1f} MB, etag={result.etag})"
    )

    return {
        "bucket": MINIO_BUCKET,
        "object_key": object_key,
        "etag": result.etag,
        "size": data_size,
    }


def get_presigned_url(
    object_key: str,
    expires: timedelta = timedelta(hours=1),
) -> str:
    """
    Generate a presigned download URL for an object.

    Args:
        object_key: The S3 object key.
        expires: URL expiration time (default: 1 hour).

    Returns:
        Presigned URL string.
    """
    client = get_minio_client()
    url = client.presigned_get_object(
        MINIO_BUCKET,
        object_key,
        expires=expires,
    )
    return url


def get_internal_url(object_key: str) -> str:
    """
    Get the internal (Docker network) URL for an object.
    Used by services within the same Docker network.

    Returns:
        URL string like http://minio:9000/bucket/key
    """
    protocol = "https" if MINIO_SECURE else "http"
    return f"{protocol}://{MINIO_ENDPOINT}/{MINIO_BUCKET}/{object_key}"


def download_file(object_key: str, file_path: str):
    """
    Download an object from MinIO to a local file.

    Args:
        object_key: The S3 object key.
        file_path: Local destination path.
    """
    client = get_minio_client()
    client.fget_object(MINIO_BUCKET, object_key, file_path)
    logger.info(f"Downloaded from MinIO: {object_key} -> {file_path}")


def get_object_stream(object_key: str):
    """
    Get a streaming response for an object.

    Returns:
        urllib3.HTTPResponse (supports .read(), .stream(), iteration)
    """
    client = get_minio_client()
    return client.get_object(MINIO_BUCKET, object_key)


def delete_object(object_key: str):
    """Delete an object from MinIO."""
    client = get_minio_client()
    client.remove_object(MINIO_BUCKET, object_key)
    logger.info(f"Deleted from MinIO: {object_key}")


def object_exists(object_key: str) -> bool:
    """Check if an object exists in MinIO."""
    client = get_minio_client()
    try:
        client.stat_object(MINIO_BUCKET, object_key)
        return True
    except S3Error:
        return False
