#!/usr/bin/env python3
"""
remove_non_dc_facility_rows.py — remove facility rows that are not data centers
==============================================================================
Dry run by default; --apply writes; --rollback FILE restores.

THE ROWS (2026-09-26)
---------------------
"Golds Gym Ashburn"  (discovered_facilities id 10669, provider = name,
slug golds-gym-ashburn-golds-gym-ashburn-0167420b, hash8 0167420b)
  * A fitness club, not a data center. Served live as v=verified in
    /api/v1/facilities?query=Ashburn, and it was the result a ChatGPT
    directory reviewer saw for "Ashburn data center power" (mcp#564 hid it
    from the connector's search; this removes it from the data).
  * PeeringDB has no such facility (api/fac?name__contains=Gold&city=Ashburn:
    none). Its 38 "on-site" carriers are the networks of the buildings next
    to its 2-dp point 39.02, -77.48 — DataBank Ashburn (IAD1) is PeeringDB fac
    1223 at 39.0160, -77.4816 with 36 networks, and that building already has
    its own row. Nothing real is lost with it.

WHAT IS REMOVED, PER TARGET
---------------------------
 * discovered_facilities rows with the target's hash8(provider|name), its
   frozen canonical_slug, or its id — AND whose name still matches the
   target's name pattern (a renamed row is left alone and reported).
 * facilities (legacy) rows with the same hash8, or whose id is a matched
   row's merged_facility_id — with the same name check.
 * carrier_facility_presence rows linked to any removed id (both id spaces).
 * facility_slug_aliases rows that point at the removed slug.

SAFETY
------
 * Dry run prints every row it would remove.
 * More than MAX_ROWS facility rows for one target aborts before any write.
 * --apply and --rollback refuse when DATABASE_URL is the read replica.
 * --apply writes the rollback JSON (every removed row, every column) BEFORE
   the first DELETE and refuses to overwrite an existing file.
 * ONE transaction with lock/statement timeouts; each DELETE is by primary key
   and must hit exactly the planned rows, else everything rolls back.
 * After COMMIT a fresh connection confirms the rows are gone.

USAGE (DATABASE_URL must point at the PRIMARY)
    python3 scripts/remove_non_dc_facility_rows.py                  # dry run
    python3 scripts/remove_non_dc_facility_rows.py --apply [--rollback-out FILE]
    python3 scripts/remove_non_dc_facility_rows.py --rollback FILE

Exit: 0 ok · 1 write/read-back failure · 2 refused · 3 row cap exceeded ·
      4 nothing matched (already removed, or the wrong database)

AFTER --apply: purge the edge for the slug (POST /api/v1/cf/purge, header
X-Admin-Key) — /facilities/<slug>, /facilities/<slug>.json and the three
/api/v1/facilit{y,ies}[/slug]/<slug> URLs — then rebuild the sitemap snapshot
(POST /api/v1/admin/sitemap/rebuild-snapshot). discovery_engine_v3's
is_valid_datacenter now refuses these names, so re-ingestion cannot re-add them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

FORMAT = "remove_non_dc_facility_rows/v1"
MAX_ROWS = 4
REPLICA_ENV_VARS = ("NEON_REPLICA_URL", "DATABASE_READ_URL")
HASH8_SQL = "LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8)"
TX_SETTINGS = ("SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '60s'")

TARGETS = (
    {"key": "golds-gym-ashburn",
     "id": 10669,
     "hash8": "0167420b",
     "slug": "golds-gym-ashburn-golds-gym-ashburn-0167420b",
     "name_re": r"(?i)\bgold'?s\s+gym\b",
     "reason": "A fitness club, not a data center; not in PeeringDB; its carriers "
               "belong to the adjacent DataBank Ashburn (IAD1), which has its own row."},
)

DF, LEGACY, CARRIER, ALIASES = ("discovered_facilities", "facilities",
                                "carrier_facility_presence", "facility_slug_aliases")
# (table, primary key) in the order rows are restored; deleted in reverse.
RESTORE_ORDER = ((LEGACY, "id"), (DF, "id"), (CARRIER, "id"), (ALIASES, "old_slug"))


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _jsonable(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return v


def _same_db(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = urlparse(a), urlparse(b)
    return ((pa.hostname, pa.port or 5432, pa.path) == (pb.hostname, pb.port or 5432, pb.path))


def refuse_replica(dsn, env=os.environ):
    for var in REPLICA_ENV_VARS:
        if _same_db(dsn, (env.get(var) or "").strip()):
            raise Refused(2, f"DATABASE_URL is the read replica ({var}); point it at the primary")


def _table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return bool(cur.fetchone()[0])


def _columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND table_schema = current_schema()", (table,))
    return {r[0] for r in cur.fetchall()}


def _rows(cur, sql, params):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def plan_target(cur, t):
    """Every row to remove for one target, plus the rows left alone and why."""
    name_ok = re.compile(t["name_re"])
    plan = {DF: [], LEGACY: [], CARRIER: [], ALIASES: []}
    kept = []
    df_cols = _columns(cur, DF)
    where = [HASH8_SQL + " = %s", "id = %s"]
    params = [t["hash8"], t["id"]]
    if "canonical_slug" in df_cols:
        where.append("canonical_slug = %s")
        params.append(t["slug"])
    for r in _rows(cur, f"SELECT * FROM {DF} WHERE " + " OR ".join(where) + " ORDER BY id", params):
        (plan[DF] if name_ok.search(r.get("name") or "") else kept).append(r)
    merged = sorted({str(r["merged_facility_id"]) for r in plan[DF] if r.get("merged_facility_id")})
    if _table_exists(cur, LEGACY):
        for r in _rows(cur, f"SELECT * FROM {LEGACY} WHERE {HASH8_SQL} = %s OR id::text = ANY(%s::text[]) "
                            "ORDER BY id", (t["hash8"], merged)):
            (plan[LEGACY] if name_ok.search(r.get("name") or "") else kept).append(r)
    ids = sorted({str(r["id"]) for r in plan[DF] + plan[LEGACY]})
    if ids and _table_exists(cur, CARRIER):
        plan[CARRIER] = _rows(cur, f"SELECT * FROM {CARRIER} WHERE dchub_facility_id = ANY(%s::text[]) "
                                   "ORDER BY id", (ids,))
    if _table_exists(cur, ALIASES):
        plan[ALIASES] = _rows(cur, f"SELECT * FROM {ALIASES} WHERE canonical_slug = %s "
                                   "ORDER BY old_slug", (t["slug"],))
    n_fac = len(plan[DF]) + len(plan[LEGACY])
    if n_fac > MAX_ROWS:
        raise Refused(3, f"{t['key']}: {n_fac} facility rows match, cap is {MAX_ROWS}")
    return plan, kept


def build_plan(cur, targets=TARGETS):
    return {t["key"]: plan_target(cur, t) for t in targets}


def _print_plan(plans):
    total = 0
    for key, (plan, kept) in plans.items():
        print(f"== {key}")
        for table, _pk in RESTORE_ORDER:
            for r in plan[table]:
                total += 1
                show = {k: r.get(k) for k in ("id", "old_slug", "name", "provider", "city",
                                             "source", "dchub_facility_id", "carrier_name") if k in r}
                print(f"   remove {table}: {show}")
        for r in kept:
            print(f"   KEEP (name no longer matches): id={r.get('id')} name={r.get('name')!r}")
    return total


def _delete(cur, table, pk, rows):
    if not rows:
        return
    keys = [r[pk] for r in rows]
    cur.execute(f"DELETE FROM {table} WHERE {pk} = ANY(%s)", (keys,))
    if cur.rowcount != len(keys):
        raise RuntimeError(f"{table}: deleted {cur.rowcount}, planned {len(keys)}")


def apply(conn, plans, rollback_out):
    if os.path.exists(rollback_out):
        raise Refused(2, f"{rollback_out} exists; refusing to overwrite a rollback file")
    doc = {"format": FORMAT, "written_at": datetime.now(timezone.utc).isoformat(),
           "targets": {k: {t: [{c: _jsonable(v) for c, v in r.items()} for r in rows]
                           for t, rows in plan.items()} for k, (plan, _kept) in plans.items()}}
    with open(rollback_out, "x") as f:
        json.dump(doc, f, indent=1, default=str)
    print(f"rollback file written: {rollback_out}")
    cur = conn.cursor()
    try:
        for s in TX_SETTINGS:
            cur.execute(s)
        for _k, (plan, _kept) in plans.items():
            for table, pk in reversed(RESTORE_ORDER):
                _delete(cur, table, pk, plan[table])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def read_back_gone(conn, plans):
    cur = conn.cursor()
    left = []
    for _k, (plan, _kept) in plans.items():
        for table, pk in RESTORE_ORDER:
            keys = [r[pk] for r in plan[table]]
            if keys:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {pk} = ANY(%s)", (keys,))
                n = cur.fetchone()[0]
                if n:
                    left.append((table, n))
    return left


def rollback(conn, path):
    from psycopg2.extras import Json
    with open(path) as f:
        doc = json.load(f)
    if doc.get("format") != FORMAT:
        raise Refused(2, f"{path} is not a {FORMAT} file")
    cur = conn.cursor()
    n = 0
    try:
        for s in TX_SETTINGS:
            cur.execute(s)
        for _k, tables in doc["targets"].items():
            for table, pk in RESTORE_ORDER:
                for r in tables.get(table, []):
                    cols = list(r)
                    vals = [Json(v) if isinstance(v, dict) else v for v in r.values()]
                    cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                                f"({', '.join(['%s'] * len(cols))}) ON CONFLICT ({pk}) DO NOTHING", vals)
                    n += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    ap.add_argument("--rollback-out", metavar="FILE")
    a = ap.parse_args(argv)
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    import psycopg2
    try:
        if a.apply or a.rollback:
            refuse_replica(dsn)
        conn = psycopg2.connect(dsn)
        if a.rollback:
            print(f"restored {rollback(conn, a.rollback)} row(s) from {a.rollback}")
            return 0
        plans = build_plan(conn.cursor())
        conn.rollback()
        total = _print_plan(plans)
        if not total:
            print("nothing matched: already removed, or this is not the production database")
            return 4
        if not a.apply:
            print(f"DRY RUN: {total} row(s) would be removed. Re-run with --apply.")
            return 0
        out = a.rollback_out or f"remove_non_dc_rollback_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        apply(conn, plans, out)
        left = read_back_gone(psycopg2.connect(dsn), plans)
        if left:
            print(f"READ-BACK FAILED, rows still present: {left}", file=sys.stderr)
            return 1
        print(f"removed {total} row(s); read-back on a fresh connection: gone. Rollback: {out}")
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return e.code
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
