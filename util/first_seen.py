"""First-seen registry: the "new" count for energy layers whose loader
restamps every row.

WHY THIS EXISTS (measured 2026-09-22)
  interconnect_queue   5,559 rows, re-upserted daily; loaded_at is restamped
                       on every row the feed carries, so it says "the loader
                       ran", never "this project is new".
  planned_generators   2,341 rows, monthly DELETE + INSERT; ingested_at is one
                       instant shared by every row.
  generator_inventory  27,700 rows, weekly DELETE + INSERT; same shape.

  A COUNT(*) delta cannot stand in for "new" either. On a delete-and-reinsert
  table the delta is NET churn (new minus removed): 40 new planned units and
  40 that went operational read as 0, and a table reloaded from nothing reads
  as +everything. So each layer keeps ONE row per stable upstream key here,
  written with INSERT ... ON CONFLICT DO NOTHING. A key's first_seen_at is set
  the first time an ingest carried it and never moves after that. "New in the
  last 7 days" is a COUNT over this table, not a difference of two totals.

  Keys, all checked in production 2026-09-22 (non-null, unique):
    interconnect_queue   (iso, queue_id)            7 ISOs, 5,559 of 5,559 distinct
    planned_generators   plant_id:generator_id      2,341 of 2,341 distinct
    generator_inventory  plant_id:generator_id      27,700 of 27,700 distinct

A BASELINE IS NEVER NEWS
  Every key present when the registry starts is written baseline=TRUE and is
  never counted. Otherwise the first run would publish "+5,559 new
  interconnection requests", which is a backfill presented as growth. The
  same rule covers:
    seed                 the first run for a layer: every key is baseline.
    first_run_for_scope  the first run for one ISO under a layer that already
                         has rows (the queue is recorded one ISO at a time, and
                         a later ISO may be added).
    rekey_suspected      one run brings more unknown keys than
                         REKEY_FRACTION of the scope already holds (and at
                         least REKEY_MIN of them). That is what a parser
                         changing its id format looks like, or a table
                         rebuilt under new ids. The measured real rate is
                         about 115 new queue projects in 55 days over all 7
                         ISOs, so a genuine burst is nowhere near this bar.
                         These keys are written as baseline, and the reason
                         is returned to the ingest's caller so the log shows
                         it.

CONTRACT
  * ensure(conn) runs the DDL once per process through util.ddl_once on a
    DIRECT psycopg2 connection. DDL sent through a db_utils.get_db() cursor is
    silently skipped (SKIP_DDL=1).
  * record() never raises and never breaks the caller's transaction. It runs
    inside a SAVEPOINT and rolls back to it on any failure. It writes nothing
    but this table.
  * added_counts() returns None when the registry is absent or holds nothing
    for the layer. That means UNMEASURED, and a reader must not show it as 0.
  * plan() is pure, so tests import it directly: no DB, no psycopg2, no flask.
"""
from __future__ import annotations

import math

TABLE = "energy_first_seen"

DDL = (
    """CREATE TABLE IF NOT EXISTS energy_first_seen (
        layer          TEXT        NOT NULL,
        scope          TEXT        NOT NULL DEFAULT '',
        item_key       TEXT        NOT NULL,
        first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        baseline       BOOLEAN     NOT NULL,
        reason         TEXT        NOT NULL,
        PRIMARY KEY (layer, scope, item_key)
    )""",
)

SEED = "seed"
FIRST_SCOPE = "first_run_for_scope"
REKEY = "rekey_suspected"
NEW = "new"

REKEY_FRACTION = 0.5
REKEY_MIN = 50


def ensure(conn) -> bool:
    """Create the registry once per process on a DIRECT connection."""
    try:
        from util.ddl_once import already_done, ensure_once
    except Exception:
        return False
    ensure_once(TABLE, conn, DDL)
    return already_done(TABLE)


def _clean(keys):
    out = set()
    for k in keys or ():
        s = str(k).strip() if k is not None else ""
        if s:
            out.add(s)
    return out


def plan(known_in_scope, layer_has_rows, incoming, seed_keys=()):
    """PURE. Decide how one run for one (layer, scope) is written.

    known_in_scope  keys the registry already holds for this (layer, scope)
    layer_has_rows  whether the registry holds ANY row for this layer
    incoming        keys this run's feed carried
    seed_keys       keys already in the source table. They are written as
                    baseline on a first run only, so a project that has
                    dropped out of the feed cannot come back later and be
                    counted as new.

    Returns (baseline, reason, keys_to_insert). keys_to_insert is sorted.
    """
    inc = _clean(incoming)
    if not layer_has_rows:
        return True, SEED, sorted(inc | _clean(seed_keys))
    known = _clean(known_in_scope)
    if not known:
        return True, FIRST_SCOPE, sorted(inc | _clean(seed_keys))
    fresh = inc - known
    if len(fresh) >= REKEY_MIN and len(fresh) > REKEY_FRACTION * len(known):
        return True, REKEY, sorted(fresh)
    return False, NEW, sorted(fresh)


def record(cur, layer, scope, incoming, seed_keys=None):
    """Write one ingest run into the registry. Never raises.

    `seed_keys` may be a zero-argument callable. It is called only on a first
    run (seed / first_run_for_scope), so the daily path does not re-read the
    source table. The caller owns the transaction. This runs inside a
    SAVEPOINT, so a failure here rolls back only its own writes and the
    caller's ingest continues.
    """
    scope = scope or ""
    try:
        cur.execute("SAVEPOINT energy_first_seen")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "layer": layer, "scope": scope,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    try:
        cur.execute("SELECT EXISTS (SELECT 1 FROM energy_first_seen WHERE layer = %s)",
                    (layer,))
        layer_has_rows = bool((cur.fetchone() or [False])[0])
        cur.execute("SELECT item_key FROM energy_first_seen "
                    "WHERE layer = %s AND scope = %s", (layer, scope))
        known = {r[0] for r in cur.fetchall()}
        first_run = (not layer_has_rows) or (not known)
        seed = ()
        if first_run and seed_keys is not None:
            seed = seed_keys() if callable(seed_keys) else seed_keys
        baseline, reason, keys = plan(known, layer_has_rows, incoming, seed)
        inserted = 0
        if keys:
            # One statement, keys as a single array parameter. RETURNING
            # counts only rows actually inserted, so a key another run
            # registered first is not counted twice.
            cur.execute(
                """INSERT INTO energy_first_seen (layer, scope, item_key, baseline, reason)
                   SELECT %s, %s, k, %s, %s FROM unnest(%s::text[]) AS k
                   ON CONFLICT DO NOTHING RETURNING 1""",
                (layer, scope, baseline, reason, list(keys)))
            inserted = len(cur.fetchall() or [])
        cur.execute("RELEASE SAVEPOINT energy_first_seen")
        return {"ok": True, "layer": layer, "scope": scope, "reason": reason,
                "new": 0 if baseline else inserted,
                "baselined": inserted if baseline else 0,
                "known_before": len(known)}
    except Exception as e:  # noqa: BLE001 — the ingest must never fail on this
        try:
            cur.execute("ROLLBACK TO SAVEPOINT energy_first_seen")
        except Exception:
            pass
        return {"ok": False, "layer": layer, "scope": scope,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


def added_counts(cur, layer, window_days=7):
    """Keys first seen for `layer` in the last `window_days` / 1 day.

    Returns None when the registry table is absent or holds no row for the
    layer, meaning UNMEASURED. window_days is capped at the registry's own
    age, rounded up to whole days, so a registry started two days ago never
    publishes "in the last 7d" for a window it was not recording.
    """
    cur.execute("SELECT to_regclass(%s)", (TABLE,))
    if not (cur.fetchone() or [None])[0]:
        return None
    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE NOT baseline AND first_seen_at >= "
        "         NOW() - make_interval(days => %s)), "
        "       COUNT(*) FILTER (WHERE NOT baseline AND first_seen_at >= "
        "         NOW() - INTERVAL '1 day'), "
        "       COUNT(*) FILTER (WHERE baseline), "
        "       MIN(first_seen_at), "
        "       EXTRACT(EPOCH FROM (NOW() - MIN(first_seen_at))) / 86400.0 "
        "  FROM energy_first_seen WHERE layer = %s",
        (int(window_days), layer))
    row = cur.fetchone()
    if not row or row[3] is None:
        return None
    age_days = float(row[4] or 0.0)
    return {
        "added_window": int(row[0] or 0),
        "window_days": min(int(window_days), max(1, math.ceil(age_days))),
        "added_1d": int(row[1] or 0),
        "baseline": int(row[2] or 0),
        "since": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
    }
