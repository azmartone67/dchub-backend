import os, sys, subprocess, logging, tempfile, json
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
    # Neon defaults a statement_timeout on the role; the COPY of a big table
    # (agent_requests) blows past it → "canceling statement due to statement
    # timeout" (the REAL failure the OOM was masking). Disable the timeout for
    # the dump session via PGOPTIONS, and connect to the DIRECT (non-pooled)
    # endpoint — pg_dump's long single-session COPYs don't play well with the
    # PgBouncer pooler, and Neon recommends the direct endpoint for dumps.
    direct_url = database_url.replace("-pooler.", ".")
    env = {**os.environ, "PGOPTIONS": "-c statement_timeout=0 -c idle_in_transaction_session_timeout=0"}
    log.info("Starting pg_dump (direct endpoint, statement_timeout=0; streaming -> gzip -> %s)...", path)
    with open(path, "wb") as out:
        p1 = subprocess.Popen(
            ["pg_dump", direct_url, "--no-owner", "--no-acl", "--clean",
             "--if-exists", "--format=plain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
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
def parse_manifest_rows(psql_output):
    """`psql -At -F'|'` rows -> {table: estimated_rows}. Pure, no I/O.

    Split on the LAST separator: the row count is always the final field, so a
    name containing the delimiter cannot shift the number. A row whose count is
    not an integer is dropped rather than guessed — an inventory is only useful
    if every entry in it is true.
    """
    tables = {}
    for line in (psql_output or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name, _, est = line.rpartition("|")
        if not name:
            continue
        try:
            tables[name] = int(est)
        except ValueError:
            continue
    return tables


def capture_manifest(database_url):
    """The public-table inventory AS OF the moment this dump starts.

    ★2026-08-31 — WHY THIS EXISTS. restore_verify.py compared the restored dump
    against LIVE prod, which is a different point in time. The 08-31 run failed
    on `gsc_daily_performance`: all 55,071 of its rows landed at 10:15-10:18 UTC,
    THIRTY-FOUR MINUTES AFTER the 09:41:50 dump began. The dump was correct and
    the backup was fine — the comparison was not. A table created between the
    dump and the verify is not data loss, but the gate called it loss and went
    red, and a DR gate that cries wolf is one nobody reads.

    So the backup now records what existed when it ran, and the verifier compares
    against THAT instead of against a prod that has moved on.

    Uses psql (already installed for pg_dump) rather than psycopg2, which this
    job does not install. Best-effort by design: a manifest that cannot be
    captured must never fail a backup that is otherwise good — the verifier
    falls back to the live compare and says so.
    """
    sql = ("SELECT c.relname, c.reltuples::bigint FROM pg_class c "
           "JOIN pg_namespace n ON n.oid = c.relnamespace "
           "WHERE n.nspname = 'public' AND c.relkind = 'r'")
    try:
        out = subprocess.run(
            ["psql", database_url, "-At", "-F", "|", "-c", sql],
            capture_output=True, text=True, timeout=120, check=True).stdout
    except Exception as e:
        log.warning("manifest capture failed (%s) — the restore verifier will "
                    "fall back to comparing against live prod", str(e)[:160])
        return None
    tables = parse_manifest_rows(out)
    if not tables:
        log.warning("manifest capture returned 0 tables — not uploading a manifest "
                    "that would claim prod is empty")
        return None
    log.info("manifest captured: %d public tables as of dump start", len(tables))
    return tables


def upload_manifest_to_r2(s3, manifest, key):
    """Store the inventory next to its dump, keyed by the same timestamp."""
    body = json.dumps(manifest, sort_keys=True).encode("utf-8")
    s3.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=body,
                  ContentType="application/json")
    log.info("Manifest uploaded: %s (%d bytes)", key, len(body))


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
    # Capture BEFORE the dump: pg_dump's snapshot opens at its start, so an
    # inventory taken just before it is the closest honest answer to "what
    # existed when this backup was taken".
    manifest_tables = capture_manifest(DATABASE_URL)
    dump_path, size = dump_and_compress_to_file(DATABASE_URL)
    # LC2 DR-proof: a pg_dump can "succeed" but produce a near-empty/corrupt dump
    # (empty DB, schema-only, permission fault). A tiny "backup" is worse than none —
    # it gives false DR confidence. Reject below a floor so the workflow fails loud.
    min_bytes = int(os.environ.get("MIN_BACKUP_BYTES", "1000000"))  # 1 MB floor (env-tunable)
    if size < min_bytes:
        try: os.remove(dump_path)
        except Exception: pass
        return {"status": "error",
                "error": "backup too small: %d bytes < %d floor — dump likely incomplete/empty" % (size, min_bytes)}
    timestamp = start.strftime("%Y%m%d_%H%M%S")
    key = BACKUP_PREFIX + timestamp + ".sql.gz"
    try:
        s3 = upload_file_to_r2(dump_path, key)
        if manifest_tables:
            try:
                upload_manifest_to_r2(s3, {
                    "taken_at": start.isoformat(),
                    "dump_key": key,
                    "tables": manifest_tables,
                }, BACKUP_PREFIX + timestamp + ".manifest.json")
            except Exception as e:
                # Never fail a good backup over its sidecar.
                log.warning("manifest upload failed: %s", str(e)[:160])
        prune_old_backups(s3)
    finally:
        try: os.remove(dump_path)
        except Exception: pass
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("Backup complete in %.1fs", elapsed)
    return {"status": "success", "key": key, "size_mb": round(size / (1024*1024), 2), "elapsed_seconds": round(elapsed, 1)}
if __name__ == "__main__":
    import sys as _sys
    _result = run_backup()
    print(_result)
    # LC2 DR-proof: a failed/incomplete backup must FAIL the workflow (non-zero exit),
    # not print-an-error-dict-and-exit-0. The dead-man watcher tracks this workflow's
    # last SUCCESS as the backup-neon-r2 feed; a green run on a bad backup = silent DR loss.
    if not isinstance(_result, dict) or _result.get("status") != "success":
        _sys.exit(1)
