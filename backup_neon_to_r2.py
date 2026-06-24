import os, sys, subprocess, logging, tempfile
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
def dump_and_compress_to_file(database_url):
    """Stream pg_dump -> gzip -> a temp file on disk. NEVER buffers the full dump
    in memory. The old code did capture_output=True THEN gzip.compress(data),
    holding the entire dump RAW *and* COMPRESSED in RAM at once — on a large DB
    that OOM-kills the ~7GB hosted runner, which is exactly the nightly
    'runner received a shutdown signal' failure (6/6 days). Here memory stays
    flat (pipe pg_dump | gzip straight to disk); only the runner's ~14GB disk grows."""
    fd, path = tempfile.mkstemp(suffix=".sql.gz", prefix="neon_dump_")
    os.close(fd)
    log.info("Starting pg_dump (streaming -> gzip -> %s)...", path)
    with open(path, "wb") as out:
        p1 = subprocess.Popen(
            ["pg_dump", database_url, "--no-owner", "--no-acl", "--clean",
             "--if-exists", "--format=plain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(["gzip", "-6"], stdin=p1.stdout, stdout=out)
        p1.stdout.close()              # so gzip gets EOF when pg_dump exits
        _, err = p1.communicate()
        p2.communicate()
    if p1.returncode != 0:
        try: os.remove(path)
        except Exception: pass
        raise RuntimeError("pg_dump failed: " + (err.decode(errors="replace")[:300]))
    if p2.returncode != 0:
        try: os.remove(path)
        except Exception: pass
        raise RuntimeError("gzip failed rc=%s" % p2.returncode)
    size = os.path.getsize(path)
    log.info("pg_dump+gzip complete: %.1f MB compressed", size / (1024*1024))
    return path, size
def upload_file_to_r2(path, key):
    import boto3
    log.info("Uploading to R2: %s/%s", R2_BUCKET_NAME, key)
    s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT_URL, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name="auto")
    # upload_file streams from disk (multipart for large files) — no in-memory body.
    s3.upload_file(path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "application/gzip"})
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
    dump_path, size = dump_and_compress_to_file(DATABASE_URL)
    timestamp = start.strftime("%Y%m%d_%H%M%S")
    key = BACKUP_PREFIX + timestamp + ".sql.gz"
    try:
        s3 = upload_file_to_r2(dump_path, key)
        prune_old_backups(s3)
    finally:
        try: os.remove(dump_path)
        except Exception: pass
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("Backup complete in %.1fs", elapsed)
    return {"status": "success", "key": key, "size_mb": round(size / (1024*1024), 2), "elapsed_seconds": round(elapsed, 1)}
if __name__ == "__main__":
    print(run_backup())
