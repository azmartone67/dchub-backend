"""
DC Hub Nexus - Nightly Neon PostgreSQL Backup to Cloudflare R2
"""
import os, io, gzip, logging
import psycopg2
from datetime import datetime, timedelta, timezone
logging.basicConfig(level=logging.INFO, format="%(asctime)s [BACKUP] %(levelname)s %(message)s")
log = logging.getLogger("dchub_backup")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "dchub-backups")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else "")
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
BACKUP_PREFIX = "neon_backup_"
def check_env():
    missing = []
    if not DATABASE_URL: missing.append("DATABASE_URL")
    if not R2_ACCESS_KEY_ID: missing.append("R2_ACCESS_KEY_ID")
    if not R2_SECRET_ACCESS_KEY: missing.append("R2_SECRET_ACCESS_KEY")
    if not R2_ENDPOINT_URL: missing.append("R2_ENDPOINT_URL (or R2_ACCOUNT_ID)")
    if missing:
        log.error(f"Missing env vars: {', '.join(missing)}")
        return False
    return True
def dump_database(database_url):
    log.info("Starting database dump via psycopg2...")
    conn = psycopg2.connect(database_url)
    conn.set_session(readonly=True)
    cursor = conn.cursor()
    buf = io.StringIO()
    buf.write("-- DC Hub Neon Backup\n")
    buf.write(f"-- Generated: {datetime.now(timezone.utc).isoformat()}\n\n")
    cursor.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    tables = [row[0] for row in cursor.fetchall()]
    log.info(f"Found {len(tables)} tables to backup")
    for table in tables:
        log.info(f"  Dumping: {table}")
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,)
        )
        columns = [row[0] for row in cursor.fetchall()]
        if not columns:
            continue
        cols_quoted = ', '.join(f'"{c}"' for c in columns)
        buf.write(f"\n-- Table: {table}\n")
        buf.write(f'DELETE FROM "{table}";\n')
        cursor.execute(f'SELECT {cols_quoted} FROM "{table}"')
        rows = cursor.fetchall()
        for row in rows:
            values = []
            for val in row:
                if val is None:
                    values.append("NULL")
                elif isinstance(val, bool):
                    values.append("TRUE" if val else "FALSE")
                elif isinstance(val, (int, float)):
                    values.append(str(val))
                elif isinstance(val, datetime):
                    values.append(f"'{val.isoformat()}'")
                else:
                    escaped = str(val).replace("'", "''")
                    values.append(f"'{escaped}'")
            buf.write(f'INSERT INTO "{table}" ({cols_quoted}) VALUES ({", ".join(values)});\n')
        log.info(f"    {len(rows)} rows")
    cursor.close()
    conn.close()
    sql = buf.getvalue()
    log.info(f"Dump complete: {len(sql.encode()) / (1024*1024):.1f} MB")
    return sql.encode("utf-8")
def compress(data):
    log.info("Compressing...")
    compressed = gzip.compress(data, compresslevel=6)
    log.info(f"Compressed: {len(compressed)/(1024*1024):.1f} MB")
    return compressed
def upload_to_r2(compressed, key):
    import boto3
    log.info(f"Uploading to R2: {R2_BUCKET_NAME}/{key}")
    s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT_URL, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name="auto")
    s3.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=compressed, ContentType="application/gzip", Metadata={"source": "dchub-neon-backup", "created": datetime.now(timezone.utc).isoformat()})
    log.info(f"Upload complete: {key}")
    return s3
def prune_old_backups(s3_client):
    log.info(f"Pruning backups older than {RETENTION_DAYS} days...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=BACKUP_PREFIX):
        for obj in page.get("Contents", []):
            if obj["LastModified"].replace(tzinfo=timezone.utc) < cutoff:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=obj["Key"])
                deleted += 1
    log.info(f"Pruned {deleted} old backup(s)")
def run_backup():
    start = datetime.now(timezone.utc)
    log.info("DC Hub Neon Backup — Starting")
    if not check_env():
        return {"status": "error", "error": "Missing required environment variables"}
    raw_sql = dump_database(DATABASE_URL)
    compressed = compress(raw_sql)
    timestamp = start.strftime("%Y%m%d_%H%M%S")
    key = f"{BACKUP_PREFIX}{timestamp}.sql.gz"
    s3 = upload_to_r2(compressed, key)
    prune_old_backups(s3)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info(f"Backup complete in {elapsed:.1f}s")
    return {"status": "success", "key": key, "size_mb": round(len(compressed)/(1024*1024), 2), "elapsed_seconds": round(elapsed, 1)}
if __name__ == "__main__":
    result = run_backup()
    print(result)
