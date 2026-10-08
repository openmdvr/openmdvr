"""Object storage abstraction layer (business code never uses a vendor SDK
directly). This module is the READ side: jt808-server (Go) uploads the bytes
the device sends (see jt808-server/internal/storage); this API never sees the
file contents, it only signs short-lived read URLs so the frontend can play
the clip.

Current provider: Cloudflare R2 (S3-compatible, no egress cost). boto3 works
against R2 unpatched -- the path documented by Cloudflare itself."""
from __future__ import annotations

import boto3
from botocore.client import Config as BotoConfig

from .config import Settings

# Explicit SigV4 -- boto3 already defaults to v4, but it is pinned on purpose:
# R2 requires SigV4, and a non-AWS endpoint with the wrong signer fails
# silently with a generic 403 instead of a clear error.
_BOTO_CONFIG = BotoConfig(signature_version="s3v4")


class StorageNotConfigured(RuntimeError):
    """The current deployment has no R2 bucket configured (R2_ENDPOINT /
    R2_ACCESS_KEY / R2_SECRET_KEY / R2_BUCKET empty) -- a clear error instead
    of a cryptic boto3 traceback."""


def _client(settings: Settings):
    if not (settings.r2_endpoint and settings.r2_access_key and settings.r2_secret_key and settings.r2_bucket):
        raise StorageNotConfigured("storage: R2 is not configured in this deployment")
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        # R2 has no real regions -- "auto" is the value Cloudflare documents
        # for the AWS SDK.
        region_name="auto",
        config=_BOTO_CONFIG,
    )


def generate_signed_url(settings: Settings, key: str, expires_in: int = 3600) -> str:
    """Presigned read URL (GET) for object `key` -- short-lived (default 1h),
    same policy as the rest of the project (single-use video tickets, etc.):
    anyone holding the URL can use it until it expires, so it is never
    persisted, only generated on demand per request. Raises
    StorageNotConfigured if the deployment has no R2 configured, or
    boto3.ClientError if the object does not exist / was already removed by
    the bucket lifecycle rule -- the caller must translate that into a clean
    404, never let the raw boto3 error reach the client.
    """
    client = _client(settings)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket, "Key": key},
        ExpiresIn=expires_in,
    )
