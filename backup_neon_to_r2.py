import os, sys, gzip, subprocess, logging
from datetime import datetime, timedelta, timezone
logging.basicConfig(level=logging.INFO, format="%(asctime)s [BACKUP] %(levelname)s %(message)s")
log = logging.getLogger("dchub_backup")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "dchub-backups")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", "")
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
BACKUP_PREFIX = "neon_backup_"
def check_env():
    missing = []
    if not DATABASE_URL: missing.append("DATABASE_URL")
    if not R2_ACCESS_KEY_ID: missing.append("R2_ACCESS_KEY_ID")
    if not R2_SECRET_ACCESS_KEY: missing.append("R2_SECRET_ACCESS_KEY")
    if not R2_ENDPOINT_URL: missing.append("R2_ENDPOINT_URL")
    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        return False
    return True
def pg_dump(database_url):
    log.info("Starting pg_dump...")
    result = subprocess.run(["pg_dump", database_url, "--no-owner", "--no-acl", "--clean", "--if-exists", "--format=plain"], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("pg_dump failed: " + result.stderr.decode(errors="replace")[:200])
    log.info("pg_dump complete: %.1f MB", len(result.stdout) / (1024*1024))
    return result.stdout
def compress(data):
    compressed = gzip.compress(data, compresslevel=6)
    log.info("Compressed: %.1f MB", len(compressed) / (1024*1024))
    return compressed
def upload_to_r2(compressed, key):
    import boto3
    log.info("Uploading to R2: %s/%s", R2_BUCKET_NAME, key)
    s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT_URL, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name="auto")
    s3.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=compressed, ContentType="application/gzip")
    log.info("Upload complete: %s", key)
    return s3
def prune_old_backups(s3_client):
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=BACKUP_PREFIX):
        for obj in page.get("Contents", []):
            if obj["LastModified"].replace(tzinfo=timezone.utc) < cutoff:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=obj["Key"])
                deleted += 1
    log.info("Pruned %d old backup(s)", deleted)
def run_backup():
    start = datetime.now(timezone.utc)
    log.info("DC Hub Neon Backup starting")
    if not check_env():
        return {"status": "error", "error": "Missing required environment variables"}
    raw_sql = pg_dump(DATABASE_URL)
    compressed = compress(raw_sql)
    timestamp = start.strftime("%Y%m%d_%H%M%S")
    key = BACKUP_PREFIX + timestamp + ".sql.gz"
    s3 = upload_to_r2(compressed, key)
    prune_old_backups(s3)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("Backup complete in %.1fs", elapsed)
    return {"status": "success", "key": key, "size_mb": round(len(compressed) / (1024*1024), 2), "elapsed_seconds": round(elapsed, 1)}
if __name__ == "__main__":
    print(run_backup())
