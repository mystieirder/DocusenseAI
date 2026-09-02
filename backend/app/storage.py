"""
Object storage — one S3 abstraction for MinIO (docker), Supabase Storage, or
Cloudflare R2 (free-tier cloud), or real AWS S3.

- `_client()`         talks to the storage service (internal endpoint in Docker).
- `_public_client()`  signs presigned URLs against a browser-reachable host.
- Managed stores (Supabase/R2) need path-style addressing (S3_ADDRESSING_STYLE)
  and reject AES-256 SSE headers, so S3_USE_SSE defaults off and upload_bytes
  falls back to a plain PUT if SSE is refused.
"""
import logging
import time

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import settings

log = logging.getLogger("docusense.storage")


def _make_client(endpoint: str):
    cfg = Config(
        signature_version="s3v4",
        s3={"addressing_style": settings.S3_ADDRESSING_STYLE},   # "path" for Supabase/R2
        retries={"max_attempts": 3, "mode": "standard"},
    )
    kwargs = dict(
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=cfg,
    )
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _client():
    return _make_client(settings.S3_ENDPOINT_URL)


def _public_client():
    # Presigned URLs must point at a host the browser can reach.
    return _make_client(settings.S3_PUBLIC_ENDPOINT_URL or settings.S3_ENDPOINT_URL)


def ensure_bucket(retries: int = 15, delay: float = 2.0) -> None:
    """
    Make sure the bucket exists. Retries on connection errors so a slightly
    slow MinIO container (docker-compose uses `service_started`, not a
    healthcheck) does not leave the app without storage.
    """
    c = _client()
    last = None
    for attempt in range(1, retries + 1):
        try:
            c.head_bucket(Bucket=settings.S3_BUCKET)
            return
        except ClientError:
            # Bucket missing (404) or head not permitted — try to create it.
            try:
                c.create_bucket(Bucket=settings.S3_BUCKET)
                log.info("Created bucket %s", settings.S3_BUCKET)
                return
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    return
                # Managed stores (Supabase Storage / R2) usually forbid CreateBucket
                # over the S3 protocol — the bucket must be made in their dashboard.
                if code in ("AccessDenied", "NotImplemented", "MethodNotAllowed",
                            "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                    log.warning(
                        "Cannot auto-create bucket %r (%s). Create it once in your "
                        "storage provider's dashboard, then restart the app.",
                        settings.S3_BUCKET, code)
                    return
                last = e
        except Exception as e:                       # endpoint not reachable yet
            last = e
        if attempt < retries:
            time.sleep(delay)
    log.warning("Object storage not ready after %d attempts: %s", retries, last)


def upload_bytes(key: str, data: bytes, content_type: str) -> None:
    c = _client()
    params = dict(Bucket=settings.S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    sse_params = dict(params, ServerSideEncryption="AES256")
    try:
        c.put_object(**(sse_params if settings.S3_USE_SSE else params))
        return
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            ensure_bucket()
            c.put_object(**params)                   # bucket recreated — plain PUT
            return
        if settings.S3_USE_SSE:
            log.warning("SSE upload failed (%s) — retrying without SSE", e)
            c.put_object(**params)
            return
        raise


def download_bytes(key: str) -> bytes:
    c = _client()
    obj = c.get_object(Bucket=settings.S3_BUCKET, Key=key)
    return obj["Body"].read()


def presigned_get_url(key: str, expires: int | None = None) -> str:
    expires = expires or settings.PRESIGN_EXPIRE_SECONDS
    return _public_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expires,
    )


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=settings.S3_BUCKET, Key=key)
    except ClientError as e:
        log.warning("Failed to delete %s: %s", key, e)
