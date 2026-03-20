"""
DC Hub Neon Database Backup System
Exports critical tables from Neon PostgreSQL to compressed local JSON files.
Keeps last 7 daily backups + last 4 weekly backups locally.
Can also push to Cloudflare R2 if credentials are configured.

Usage:
  python db_backup.py                    # Run backup now
  python db_backup.py --list             # List existing backups
  python db_backup.py --restore <file>   # Show restore instructions

Called from /api/jobs/db-backup endpoint (admin key required).
"""

import os
import sys
import json
import gzip
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BACKUP] %(levelname)s %(message)s")
log = logging.getLogger("dchub_backup")

BACKUP_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "backups"
DAILY_RETENTION = 7
WEEKLY_RETENTION = 4

CRITICAL_TABLES = [
    "facilities",
    "users",
    "deals",
    "news_articles",
    "capacity_pipeline",
    "ecosystem_companies",
    "api_keys",
    "leads",
    "user_alerts",
    "partner_inquiries",
]

SECONDARY_TABLES = [
    "ai_testimonials",
    "mcp_tool_calls",
    "mcp_connections",
    "ambassador_broadcasts",
    "fiber_providers",
    "pending_facilities",
    "alert_subscriptions",
    "simple_alerts",
]


def get_neon_connection():
    import psycopg2
    db_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("No database URL configured (NEON_DATABASE_URL or DATABASE_URL)")
    conn = psycopg2.connect(db_url, connect_timeout=30)
    conn.set_session(readonly=True)
    return conn


def export_table(conn, table_name):
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cur.fetchone()[0]
    except Exception as e:
        log.warning("Table %s not found or empty: %s", table_name, str(e)[:80])
        conn.rollback()
        return None, 0

    if count == 0:
        return [], 0

    cur.execute(f"SELECT * FROM {table_name}")
    columns = [desc[0] for desc in cur.description]
    rows = cur.fetchall()

    data = []
    for row in rows:
        record = {}
        for i, col in enumerate(columns):
            val = row[i]
            if isinstance(val, datetime):
                val = val.isoformat()
            elif isinstance(val, (bytes, bytearray)):
                val = val.hex()
            elif hasattr(val, '__str__') and not isinstance(val, (str, int, float, bool, type(None))):
                val = str(val)
            record[col] = val
        data.append(record)

    return data, count


def run_backup(include_secondary=True):
    start = time.time()
    log.info("DC Hub Neon Backup starting...")

    BACKUP_DIR.mkdir(exist_ok=True)

    conn = get_neon_connection()

    backup_data = {
        "backup_version": "2.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": os.environ.get("NEON_DATABASE_URL", "")[:40] + "...",
        "tables": {},
        "row_counts": {},
    }

    tables_to_export = list(CRITICAL_TABLES)
    if include_secondary:
        tables_to_export.extend(SECONDARY_TABLES)

    total_rows = 0
    exported = 0
    errors = []

    for table in tables_to_export:
        try:
            data, count = export_table(conn, table)
            if data is not None:
                backup_data["tables"][table] = data
                backup_data["row_counts"][table] = count
                total_rows += count
                exported += 1
                log.info("  %s: %d rows", table, count)
            else:
                errors.append(table)
        except Exception as e:
            log.error("  %s: FAILED - %s", table, str(e)[:100])
            errors.append(table)
            try:
                conn.rollback()
            except:
                pass

    conn.close()

    backup_data["summary"] = {
        "tables_exported": exported,
        "tables_failed": len(errors),
        "total_rows": total_rows,
        "failed_tables": errors,
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    weekday = datetime.now(timezone.utc).strftime("%A").lower()
    is_weekly = weekday == "sunday"

    filename = f"dchub_backup_{timestamp}.json.gz"
    filepath = BACKUP_DIR / filename

    raw_json = json.dumps(backup_data, default=str, ensure_ascii=False)
    compressed = gzip.compress(raw_json.encode("utf-8"), compresslevel=6)

    with open(filepath, "wb") as f:
        f.write(compressed)

    raw_mb = len(raw_json) / (1024 * 1024)
    compressed_mb = len(compressed) / (1024 * 1024)
    elapsed = time.time() - start

    log.info("Backup saved: %s (%.1f MB raw -> %.1f MB compressed)", filename, raw_mb, compressed_mb)

    pruned = prune_old_backups()

    r2_result = None
    if os.environ.get("R2_ACCESS_KEY_ID") and os.environ.get("R2_ENDPOINT_URL"):
        try:
            r2_result = upload_to_r2(compressed, filename)
            log.info("R2 upload: %s", r2_result)
        except Exception as e:
            log.warning("R2 upload failed (local backup still saved): %s", str(e)[:100])
            r2_result = f"failed: {str(e)[:100]}"

    result = {
        "status": "success",
        "filename": filename,
        "path": str(filepath),
        "tables_exported": exported,
        "tables_failed": len(errors),
        "total_rows": total_rows,
        "raw_size_mb": round(raw_mb, 2),
        "compressed_size_mb": round(compressed_mb, 2),
        "elapsed_seconds": round(elapsed, 1),
        "old_backups_pruned": pruned,
        "is_weekly": is_weekly,
        "r2_upload": r2_result,
        "failed_tables": errors if errors else None,
    }

    log.info("Backup complete in %.1fs: %d tables, %d rows, %.1f MB", elapsed, exported, total_rows, compressed_mb)
    return result


def upload_to_r2(compressed_data, key):
    import boto3
    r2_endpoint = os.environ.get("R2_ENDPOINT_URL", "")
    r2_access = os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    r2_bucket = os.environ.get("R2_BUCKET_NAME", "dchub-backups")

    s3 = boto3.client("s3",
                       endpoint_url=r2_endpoint,
                       aws_access_key_id=r2_access,
                       aws_secret_access_key=r2_secret,
                       region_name="auto")
    s3.put_object(Bucket=r2_bucket, Key=key, Body=compressed_data, ContentType="application/gzip")
    return f"uploaded to {r2_bucket}/{key}"


def prune_old_backups():
    if not BACKUP_DIR.exists():
        return 0

    now = datetime.now(timezone.utc)
    daily_cutoff = now - timedelta(days=DAILY_RETENTION)
    weekly_cutoff = now - timedelta(weeks=WEEKLY_RETENTION)

    backups = sorted(BACKUP_DIR.glob("dchub_backup_*.json.gz"))
    pruned = 0

    for bp in backups:
        try:
            ts_str = bp.stem.replace("dchub_backup_", "").replace(".json", "")
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        age = now - ts

        if age.days > WEEKLY_RETENTION * 7:
            bp.unlink()
            pruned += 1
            log.info("Pruned old backup: %s (%.0f days old)", bp.name, age.days)
        elif age.days > DAILY_RETENTION:
            file_weekday = ts.strftime("%A").lower()
            if file_weekday != "sunday":
                bp.unlink()
                pruned += 1
                log.info("Pruned non-weekly backup: %s (%.0f days old)", bp.name, age.days)

    return pruned


def list_backups():
    if not BACKUP_DIR.exists():
        return []

    backups = []
    for bp in sorted(BACKUP_DIR.glob("dchub_backup_*.json.gz"), reverse=True):
        stat = bp.stat()
        backups.append({
            "filename": bp.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    return backups


def verify_backup(filepath):
    with open(filepath, "rb") as f:
        raw = gzip.decompress(f.read())
    data = json.loads(raw)
    result = {
        "filename": os.path.basename(filepath),
        "backup_version": data.get("backup_version"),
        "created_at": data.get("created_at"),
        "tables": {},
        "total_rows": 0,
    }
    for table, rows in data.get("tables", {}).items():
        count = len(rows)
        result["tables"][table] = count
        result["total_rows"] += count
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--list":
            backups = list_backups()
            if not backups:
                print("No backups found")
            else:
                for b in backups:
                    print(f"  {b['filename']}  ({b['size_mb']} MB)  {b['created']}")
            sys.exit(0)
        elif sys.argv[1] == "--verify" and len(sys.argv) > 2:
            result = verify_backup(sys.argv[2])
            print(json.dumps(result, indent=2))
            sys.exit(0)
        elif sys.argv[1] == "--restore":
            print("To restore from a backup:")
            print("  1. Decompress: gunzip -k backups/dchub_backup_XXXXXX.json.gz")
            print("  2. The JSON contains all table data keyed by table name")
            print("  3. Use psql or a script to INSERT the records back")
            print("  4. Critical tables: facilities, users, deals, api_keys")
            sys.exit(0)

    result = run_backup()
    print(json.dumps(result, indent=2))
