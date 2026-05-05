"""Cloudflare R2 (S3-compatible) upload helper.

Uses boto3 against R2's S3 endpoint. R2 egress is free, so public-read
bucket + custom domain + Cloudflare cache = global CDN for our PNGs.

Env:
    R2_ACCOUNT_ID
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET          e.g. dchub-daily
    R2_PUBLIC_BASE     e.g. https://daily.dchub.cloud   (Cloudflare custom domain)
"""
from __future__ import annotations

import io
import os

import boto3
from botocore.config import Config

ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]    if os.environ.get("R2_ACCOUNT_ID") else None
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
BUCKET = os.environ.get("R2_BUCKET", "dchub-daily")
PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", f"https://{BUCKET}.r2.dev")


def _client():
    if not ACCOUNT_ID:
        raise RuntimeError("R2_ACCOUNT_ID not set")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload(key: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload bytes, return public URL."""
    _client().put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=86400, immutable",
    )
    return f"{PUBLIC_BASE}/{key}"


def upload_image(key: str, pil_image) -> str:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return upload(key, buf.getvalue())
