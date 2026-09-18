#!/usr/bin/env python3
"""NULL the fabricated `power_mw` values on OpenStreetMap-sourced facility rows.

    python3 scripts/quarantine_fabricated_osm_mw.py            # dry run (default)
    python3 scripts/quarantine_fabricated_osm_mw.py --apply
    python3 scripts/quarantine_fabricated_osm_mw.py --rollback <file.json>

★ WHY THESE VALUES ARE NOT DATA ★
Measured live 2026-09-18 against discovered_facilities:

  * 688 rows carry source='openstreetmap' AND power_mw > 0.
  * ALL 688 have raw_data IS NULL and sqft IS NULL. There is no source record
    behind a single one of them — nothing to cite, nothing to re-derive.
  * The values cluster on four constants: 5.0 (x263), 50.0 (x169), 14.0 (x78),
    18.0 (x63) — 573 of 688 on four numbers.
  * Those constants span unrelated operators. atNorth, Quality Technology
    Services, Verizon, Pulsant and Universite Claude Bernard - Lyon 1 all read
    exactly 5.0 MW. A university campus room and a QTS hall do not both
    measure 5.0.
  * Every one was written in a single closed window, 2026-01-17 .. 2026-03-02.
    Every OSM row discovered since reads NULL.

routes/osm_crawler.py already states the rule these rows violate, in the
comment above its INSERT: "OSM tells us a data centre EXISTS; it does not tell
us its capacity or whether it is running." The writer was fixed (2026-08-11,
2026-08-31). The rows it had already written were not.

★ WHY REMOVING DATA IMPROVES COVERAGE ★
It lowers reported MW and that is the point. Every fleet aggregate does
COALESCE(power_mw, 0) and SUMs it, so a fabricated 5.0 is indistinguishable in
the output from a published 5.0 — it silently becomes part of a market total,
an operator portfolio and a hyperscaler fleet. A NULL is honest: the
denominator published alongside these sums (#4710, and this branch for the
other four surfaces) then reports the row as not-reporting, which is true.

A fabricated MW is worse than a denominator saying "4 of 91".

★ SCOPE ★
Touches ONLY rows matching source='openstreetmap' (case-insensitive) AND
power_mw > 0. Writes NULL, never 0 — 0 is a reading, NULL is the absence of
one, and the surfaces distinguish them. Sets nothing else.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

SRC = "openstreetmap"


def _url() -> str:
    for var in ("DATABASE_URL", "NEON_DATABASE_URL"):
        if os.environ.get(var):
            return os.environ[var]
    sys.exit("no DATABASE_URL / NEON_DATABASE_URL in the environment")


def _connect():
    import psycopg2
    return psycopg2.connect(_url(), connect_timeout=20)


def _targets(cur, table: str) -> list[tuple]:
    cur.execute(
        f"SELECT id, name, provider, power_mw FROM {table} "
        f" WHERE LOWER(COALESCE(source,'')) = %s AND power_mw > 0 ORDER BY id",
        (SRC,))
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    args = ap.parse_args()

    conn = _connect()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '120s'")

    if args.rollback:
        payload = json.load(open(args.rollback))
        n = 0
        for table, rows in payload["rows"].items():
            for r in rows:
                cur.execute(
                    f"UPDATE {table} SET power_mw = %s WHERE id = %s",
                    (r["power_mw"], r["id"]))
                n += cur.rowcount
        conn.commit()
        print(f"restored {n} rows from {args.rollback}")
        return 0

    snapshot: dict[str, list] = {}
    total = 0
    for table in ("discovered_facilities", "facilities"):
        rows = _targets(cur, table)
        snapshot[table] = [
            {"id": r[0], "name": r[1], "provider": r[2], "power_mw": float(r[3])}
            for r in rows]
        total += len(rows)
        vals: dict[float, int] = {}
        for r in rows:
            vals[float(r[3])] = vals.get(float(r[3]), 0) + 1
        top = sorted(vals.items(), key=lambda kv: -kv[1])[:4]
        print(f"{table}: {len(rows)} rows, {sum(float(r[3]) for r in rows):,.0f} MW "
              f"| top values: {', '.join(f'{v}x{n}' for v, n in top)}")

    if not args.apply:
        print(f"\nDRY RUN — {total} rows would be set to NULL. Re-run with --apply.")
        conn.rollback()
        return 0

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = f"rollback_osm_mw_{stamp}.json"
    with open(path, "w") as fh:
        json.dump({"generated_at": stamp, "rows": snapshot}, fh, indent=1)
    print(f"rollback written to {path}")

    done = 0
    for table in ("discovered_facilities", "facilities"):
        cur.execute(
            f"UPDATE {table} SET power_mw = NULL "
            f" WHERE LOWER(COALESCE(source,'')) = %s AND power_mw > 0", (SRC,))
        done += cur.rowcount
        print(f"{table}: {cur.rowcount} rows set to NULL")
    conn.commit()
    print(f"\napplied — {done} fabricated MW values removed. "
          f"Undo: --rollback {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
