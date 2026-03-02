"""
DC Hub Nexus - Nightly Neon PostgreSQL Backup to Cloudflare R2
==============================================================
Dumps the Neon database and uploads a compressed backup to
Cloudflare R2 with automatic rotation (keeps last 30 days).

SETUP:
  1. pip install boto3 (R2 uses the S3-compatible API)
  2. Set these env vars on Railway:
       DATABASE_URL          - Neon connection string (you already have this)
       R2_ACCOUNT_ID         - Cloudflare account ID
       R2_ACCESS_KEY_ID      - R2 API token access key
       R2_SECRET_ACCESS_KEY  - R2 API token secret key
       R2_BUCKET_NAME        - e.g. "dchub-backups"
       R2_ENDPOINT_URL       - https://<ACCOUNT_ID>.r2.cloudflarestorage.com
  3. Schedule via Railway cron or add to your existing scheduler:
       python backup_neon_to_r2.py

RESTORE:
  # Download from R2
  aws s3 cp s3://dchub-backups/neon_backup_20260302_030000.sql.gz ./restore.sql.gz \
      --endpoint-url $R2_ENDPOINT_URL

  # Restore to Neon (or any PostgreSQL)
  gunzip -c restore.sql.gz | psql $DATABASE_URL
"""

import os
import sys
import gzip
import subprocess
import tempfile
import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BACKUP] %(levelname)s %(message)s"
)
log = logging.getLogger("dchub_backup")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
DATABASE_URL       = os.environ.get("DATABASE_URL", "")
R2_ACCOUNT_ID      = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID   = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME     = os.environ.get("R2_BUCKET_NAME", "dchub-backups")
R2_ENDPOINT_URL    = os.environ.get(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)

RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
BACKUP_PREFIX  = "neon_backup_"


def check_env():
    """Validate required environment variables."""
    missing = []
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not R2_ACCESS_KEY_ID:
        missing.append("R2_ACCESS_KEY_ID")
    if not R2_SECRET_ACCESS_KEY:
        missing.append("R2_SECRET_ACCESS_KEY")
    if not R2_ENDPOINT_URL:
        missing.append("R2_ENDPOINT_URL (or R2_ACCOUNT_ID)")
    if missing:
        log.error(f"Missing env vars: {', '.join(missing)}")
        sys.exit(1)


def pg_dump(database_url: str) -> bytes:
    """
    Run pg_dump and return the raw SQL as bytes.
    Uses --no-owner --no-acl so the dump restores cleanly
    to any Neon branch or fresh PostgreSQL instance.
    """
    log.info("Starting pg_dump...")
    result = subprocess.run(
        [
            "pg_dump",
            database_url,
            "--no-owner",
            "--no-acl",
            "--clean",
            "--if-exists",
            "--format=plain",
        ],
        capture_output=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        log.error(f"pg_dump failed (exit {result.returncode}): {stderr}")
        sys.exit(1)

    size_mb = len(result.stdout) / (1024 * 1024)
    log.info(f"pg_dump complete: {size_mb:.1f} MB uncompressed")
    return result.stdout


def compress(data: bytes) -> bytes:
    """Gzip compress the dump."""
    log.info("Compressing...")
    compressed = gzip.compress(data, compresslevel=6)
    ratio = len(compressed) / len(data) * 100 if data else 0
    log.info(f"Compressed: {len(compressed) / (1024*1024):.1f} MB ({ratio:.0f}% of original)")
    return compressed


def upload_to_r2(compressed: bytes, key: str):
    """Upload the gzipped dump to Cloudflare R2."""
    try:
        import boto3
    except ImportError:
        log.error("boto3 not installed. Run: pip install boto3")
        sys.exit(1)

    log.info(f"Uploading to R2: {R2_BUCKET_NAME}/{key}")

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )

    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=compressed,
        ContentType="application/gzip",
        Metadata={
            "source": "dchub-neon-backup",
            "created": datetime.now(timezone.utc).isoformat(),
        },
    )
    log.info(f"✅ Upload complete: {key}")
    return s3


def prune_old_backups(s3_client):
    """Delete backups older than RETENTION_DAYS."""
    log.info(f"Pruning backups older than {RETENTION_DAYS} days...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    deleted = 0

    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=BACKUP_PREFIX):
        for obj in page.get("Contents", []):
            if obj["LastModified"].replace(tzinfo=timezone.utc) < cutoff:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=obj["Key"])
                deleted += 1
                log.info(f"  Deleted: {obj['Key']}")

    log.info(f"Pruned {deleted} old backup(s)")


def run_backup():
    """Main backup pipeline."""
    start = datetime.now(timezone.utc)
    log.info("=" * 50)
    log.info("DC Hub Neon Backup — Starting")
    log.info("=" * 50)

    check_env()

    # 1. Dump
    raw_sql = pg_dump(DATABASE_URL)

    # 2. Compress
    compressed = compress(raw_sql)

    # 3. Upload
    timestamp = start.strftime("%Y%m%d_%H%M%S")
    key = f"{BACKUP_PREFIX}{timestamp}.sql.gz"
    s3 = upload_to_r2(compressed, key)

    # 4. Prune
    prune_old_backups(s3)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info(f"✅ Backup complete in {elapsed:.1f}s")

    return {
        "status": "success",
        "key": key,
        "size_mb": round(len(compressed) / (1024 * 1024), 2),
        "elapsed_seconds": round(elapsed, 1),
    }


if __name__ == "__main__":
    result = run_backup()
    print(f"\n{result}")
