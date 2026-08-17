#!/usr/bin/env python3
"""Quarantine byte-identical SERVED deal rows (Shell #36 lane 3a).

WHY THESE EXIST: extractor_cron.py mints deal ids as
ext_<md5(announcement_id)[:20]> — the hash is over the ANNOUNCEMENT, not the
deal, so the same deal announced in N articles inserts N rows that are
byte-identical on all 19 business columns and differ only by id and ingest
timestamps. ON CONFLICT (id) never fires. 107 of 2,144 served rows (5.0%)
were such copies on 2026-08-16, tripping lane 3a's <5% bar. (Same defect
shape as the 07-17 AUTO-<date> ids, one id scheme later — lane 3b keys on
"AUTO-" and is blind to "ext_".)

Dedup here is by data_flag, NEVER by DELETE — every served query and the RAG
registry already exclude LEFT(data_flag,11)='quarantine_', and
brain_rag._sweep_orphans GCs the victims' embeddings on the next reindex.

Keeper = min(id) per identical group. Content is identical by construction so
any keeper is equivalent; min(id) is stable across re-runs (idempotent).

The group key and the served predicate are IMPORTED from the modules that own
them (graph_spine_master_shell._DEAL_BUSINESS_COLS, util.deals.DEALS_OK) —
mirror the guards of the code under audit exactly, never restate them.

Usage:
    DATABASE_URL=postgres://…  python3 repair_deals_exact_dupes.py            # dry run
    DATABASE_URL=postgres://…  python3 repair_deals_exact_dupes.py --apply
    DATABASE_URL=postgres://…  python3 repair_deals_exact_dupes.py --rollback <file.json>

--apply writes an id→prior-flag rollback JSON to ~/Downloads first, then runs
ONE transaction (execute_values, never row-by-row — the Neon pooler stalls on
per-row UPDATE loops).
"""

import argparse
import datetime
import json
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from routes.graph_spine_master_shell import _DEAL_BUSINESS_COLS  # noqa: E402
from util.deals import DEALS_OK, deals_ok  # noqa: E402

FLAG = "quarantine_duplicate"


def _conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set (must be the PRIMARY, not the replica)")
    return psycopg2.connect(url)


def find_groups(cur):
    cur.execute(
        f"SELECT min(id) AS keep, array_agg(id ORDER BY id) AS ids, count(*) AS n"
        f" FROM deals WHERE {DEALS_OK}"
        f" GROUP BY {_DEAL_BUSINESS_COLS} HAVING count(*) > 1"
        f" ORDER BY count(*) DESC")
    return cur.fetchall()


def lane_excess(cur):
    """The exact number lane 3a publishes: served dupe rows beyond 1/group."""
    cur.execute(
        f"WITH b AS (SELECT {_DEAL_BUSINESS_COLS} FROM deals WHERE {DEALS_OK}),"
        f" g AS (SELECT count(*) AS n FROM b GROUP BY {_DEAL_BUSINESS_COLS})"
        f" SELECT (SELECT count(*) FROM b),"
        f"        (SELECT coalesce(sum(n) - count(*), 0) FROM g WHERE n > 1)")
    served, excess = cur.fetchone()
    return int(served or 0), int(excess or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    args = ap.parse_args()

    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()

    if args.rollback:
        rows = json.load(open(args.rollback))["rows"]
        execute_values(
            cur,
            "UPDATE deals AS d SET data_flag = v.prior"
            " FROM (VALUES %s) AS v(id, prior) WHERE d.id = v.id",
            [(r["id"], r["prior"]) for r in rows],
            template="(%s::text, %s::text)")
        conn.commit()
        print(f"rolled back {len(rows)} rows from {args.rollback}")
        return

    served, excess = lane_excess(cur)
    groups = find_groups(cur)
    victims = [i for keep, ids, _n in groups for i in ids if i != keep]
    print(f"served={served} excess={excess} groups={len(groups)} victims={len(victims)}")
    for keep, ids, n in groups[:6]:
        print(f"  n={n} keep={keep} quarantine={[i for i in ids if i != keep]}")

    if not args.apply:
        print("(dry run — pass --apply to write)")
        return
    if not victims:
        print("nothing to do")
        return

    cur.execute("SELECT id, data_flag FROM deals WHERE id = ANY(%s)", (victims,))
    prior = [{"id": r[0], "prior": r[1]} for r in cur.fetchall()]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rb = os.path.expanduser(f"~/Downloads/deals_exact_dupes_rollback_{ts}.json")
    with open(rb, "w") as f:
        json.dump({"applied_at": ts, "flag": FLAG, "rows": prior}, f, indent=1)
    print(f"rollback -> {rb}")

    # Guarded write: only rows STILL served flip, so a re-run or a race with
    # another repair is a no-op, and rollback state is exactly what we dumped.
    execute_values(
        cur,
        f"UPDATE deals AS d SET data_flag = '{FLAG}'"
        f" FROM (VALUES %s) AS v(id)"
        f" WHERE d.id = v.id AND {deals_ok('d')}",
        [(v,) for v in victims],
        template="(%s::text)", page_size=max(len(victims), 1))
    # ★ rowcount is per-page: at the default page_size=100 a 107-row run
    # reports 7. One page → rowcount is the whole write.
    flipped = cur.rowcount
    served2, excess2 = lane_excess(cur)
    conn.commit()
    print(f"flipped={flipped} · lane 3a now: served={served2} excess={excess2}"
          f" ({(100.0 * excess2 / served2) if served2 else 0:.1f}%)")


if __name__ == "__main__":
    main()
