"""routes/temporal_capture.py — WS6 (shell #41, 2026-07-29): the append-only
change-capture SPINE.

WHY THIS EXISTS
---------------
Four of the six data layers destroy their own history:

  * interconnect_queue — 04_interconnect_queue.py:179 DELETEs the table and
    run_queue_etl.py:188 DROPs it; the daily loader
    (load_interconnect_queue_live.py:602-619) sets `loaded_at = EXCLUDED.loaded_at`
    on every upsert, so loaded_at is a LAST-TOUCH stamp, never a first-seen.
    Queue-status transitions cannot be recovered in place — only an external
    capture can ever hold them.
  * hosting_capacity_feeders — routes/hosting_capacity_ingest.py:570 sets
    `ingested_at = NOW()` on every conflict. Same last-seen-only shape.
  * capacity_pipeline — global_intelligence_agent.py:140-157 declares
    created_at TEXT with no first_seen at all.
  * discovered_facilities — routes/facilities_delta.py banks AGGREGATE counts
    only (facility_count_snapshots), so we can say +1,085 facilities appeared
    in 30 days but not WHICH.

Every day without capture is history permanently lost, so this module captures
FIRST and leaves the as-of query API for a later pass.

WHAT IT WRITES
--------------
  entity_state         one row per (layer, entity_key): current payload hash,
                       first_seen, first_seen_exact, last_seen, change_count.
                       UPSERTed — this is state, not history.
  entity_changes       append-only. One row per genuine post-baseline event
                       ('appeared') or per changed tracked field
                       ('field_change'). This is the history.
  entity_capture_runs  append-only run ledger. Cron-green is not proof a pass
                       ran or covered the table — this table is the proof, and
                       it carries the resume offset.

HONEST-NUMBERS RULES BAKED IN
-----------------------------
  ★ BASELINE IS NOT AN APPEARANCE. The first pass over a layer writes
    entity_state rows with first_seen_exact = FALSE and emits ZERO
    entity_changes rows. `first_seen` on those rows is when WE STARTED
    WATCHING, not when the entity appeared — exactly the ★backfill-cluster
    trap that makes a bulk-loaded created_at look like history. Only rows with
    first_seen_exact = TRUE carry a real appearance date.
  ★ NO DISAPPEARANCE DETECTION IN v1. A budget-truncated pass sees only part
    of the table, so "missing from this pass" cannot be distinguished from
    "deleted". Emitting `disappeared` would manufacture false events at every
    truncation. Deliberately absent — see run ledger `truncated`.
  ★ UNMEASURED IS NULL, NEVER 0. A layer that was skipped, has no live table,
    or is missing its key columns reports scanned/appeared/changed = None with
    a status and a note. It never reports 0, which would read as "nothing
    changed".

DURABILITY (the twice-shipped failure this is written against)
--------------------------------------------------------------
This runs as a daemon thread inside the WEB worker (piggybacked on
POST /api/v1/competitors/scan, STEP 6). A web-worker daemon thread has been
killed by worker recycle mid-write with ZERO rows persisted, twice. So:
execute_values(page_size=500) and COMMIT PER CHUNK — every 2,000 source rows
are durable before the next chunk starts — plus a persisted resume offset in
entity_capture_runs so the next tick continues instead of restarting.

ADDITIVE ONLY: no existing loader's write semantics are touched.

Endpoints (safe-zone registered in main.py):
    POST /api/v1/temporal/capture     admin; ?force=1 ignores the gate,
                                      ?sync=1 runs inline, ?layer=<name>
    GET  /api/v1/temporal/coverage    what has actually been banked so far
"""

from __future__ import annotations

import os
import json
import time
import decimal
import hashlib
import logging
import datetime

logger = logging.getLogger(__name__)

# Wall-clock budget for one capture pass across ALL layers. Kept well under
# the daily cron's curl --max-time because the caller spawns us detached.
_BUDGET_S = float(os.environ.get("TEMPORAL_CAPTURE_BUDGET_S", "120"))
# Source rows per read+write cycle. One COMMIT per chunk.
_CHUNK = int(os.environ.get("TEMPORAL_CAPTURE_CHUNK", "2000"))
# Don't re-run a layer that completed a full pass this recently. Mirrors
# market_alerts.MIN_SNAPSHOT_GAP_HOURS=20 — a manual/duplicate tick must not
# corrupt the day-over-day baseline.
_GATE_HOURS = float(os.environ.get("TEMPORAL_CAPTURE_GAP_HOURS", "20"))
# Truncation of a stored old/new value in entity_changes.
_VAL_MAX = 500

_KEY_SEP = "\x1f"


# ── Layer registry ───────────────────────────────────────────────────
# `key`   columns forming the stable entity identity. If ANY is missing from
#         the LIVE table the layer is SKIPPED with a status — never guessed.
# `track` columns whose change is a real-world event. Missing ones are dropped
#         and reported in `dropped_columns` (LIVE ≠ repo DDL; the live column
#         set is introspected every run, never assumed).
_LAYERS = [
    {
        "layer": "interconnect_queue",
        "table": "interconnect_queue",
        # id is NOT stable here: 04_interconnect_queue.py recreates the table.
        # (iso, queue_id) is the loader's own UNIQUE index.
        "key": ("iso", "queue_id"),
        "track": ("queue_status", "queue_date", "capacity_mw", "fuel_type",
                  "project_name", "state", "county", "poi_name"),
        "why": ("queue history is destroyed on every reload; loaded_at is a "
                "last-touch stamp"),
    },
    {
        "layer": "capacity_pipeline",
        "table": "capacity_pipeline",
        "key": ("id",),
        "track": ("operator", "market", "region", "capacity_mw", "phase",
                  "status", "announcement_date", "completion_date"),
        "why": "no first_seen column exists anywhere in this table",
    },
    {
        "layer": "discovered_facilities",
        "table": "discovered_facilities",
        "key": ("id",),
        # is_duplicate/merged_at are the VISIBILITY flags — a suppression is a
        # page disappearing, which is exactly the change nothing records today.
        "track": ("name", "provider", "city", "state", "market", "power_mw",
                  "status", "is_duplicate", "merged_at"),
        "why": ("facility_count_snapshots banks aggregate counts only — WHICH "
                "facility appeared is unrecoverable"),
    },
    {
        "layer": "hosting_capacity_feeders",
        "table": "hosting_capacity_feeders",
        "key": ("utility", "feeder_key"),
        "track": ("capacity_mw_max", "capacity_mw_min", "queued_gen_kw",
                  "voltage_kv", "substation", "src_updated", "capacity_type"),
        "why": "ingested_at is re-stamped on every conflict; no capacity history",
    },
]

_LAYER_NAMES = tuple(s["layer"] for s in _LAYERS)

# Statuses that mean "this layer produced no measurement" — counts must be
# None, not 0.
_UNMEASURED = ("skipped_recent", "no_such_table", "missing_key_columns",
               "no_tracked_columns", "no_database", "schema_failed")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_state (
    layer            TEXT NOT NULL,
    entity_key       TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    payload          JSONB,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen_exact BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    change_count     INT NOT NULL DEFAULT 0,
    PRIMARY KEY (layer, entity_key)
);
CREATE INDEX IF NOT EXISTS ix_estate_layer_seen
    ON entity_state (layer, last_seen DESC);
CREATE INDEX IF NOT EXISTS ix_estate_layer_first
    ON entity_state (layer, first_seen DESC) WHERE first_seen_exact;

CREATE TABLE IF NOT EXISTS entity_changes (
    id          BIGSERIAL PRIMARY KEY,
    layer       TEXT NOT NULL,
    entity_key  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT,
    detail      JSONB,
    run_id      TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_echg_layer_at
    ON entity_changes (layer, detected_at DESC);
CREATE INDEX IF NOT EXISTS ix_echg_entity
    ON entity_changes (layer, entity_key, detected_at DESC);

CREATE TABLE IF NOT EXISTS entity_capture_runs (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    layer       TEXT NOT NULL,
    status      TEXT NOT NULL,
    baseline    BOOLEAN NOT NULL DEFAULT FALSE,
    scanned     INT,
    appeared    INT,
    changed     INT,
    unkeyable   INT,
    truncated   BOOLEAN NOT NULL DEFAULT FALSE,
    next_offset INT NOT NULL DEFAULT 0,
    note        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_ecr_layer_at
    ON entity_capture_runs (layer, started_at DESC);
"""

_STATE_UPSERT = """
    INSERT INTO entity_state
        (layer, entity_key, payload_hash, payload, first_seen_exact,
         change_count)
    VALUES %s
    ON CONFLICT (layer, entity_key) DO UPDATE SET
        payload_hash = EXCLUDED.payload_hash,
        payload      = EXCLUDED.payload,
        last_seen    = NOW(),
        change_count = entity_state.change_count + EXCLUDED.change_count
"""

_CHANGE_INSERT = """
    INSERT INTO entity_changes
        (layer, entity_key, kind, field, old_value, new_value, detail, run_id)
    VALUES %s
"""

_RUN_INSERT = """
    INSERT INTO entity_capture_runs
        (run_id, layer, status, baseline, scanned, appeared, changed,
         unkeyable, truncated, next_offset, note)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
"""


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


def _ensure_schema(c) -> bool:
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
        c.commit()
        return True
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("temporal_capture: schema init failed: %s", str(e)[:200])
        return False


# ── Pure helpers (unit-testable without a DB) ────────────────────────

def _norm_value(v):
    """Canonical string form of a column value, so a hash is stable across
    driver/type quirks. NUMERIC(10,2) vs REAL vs float all collapse to a
    fixed 6-decimal form; None stays None (absent is not the empty string)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (datetime.datetime, datetime.date)):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    if isinstance(v, decimal.Decimal):
        try:
            return "{:.6f}".format(float(v))
        except Exception:
            return str(v)
    if isinstance(v, float):
        return "{:.6f}".format(v)
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def _entity_key(rec: dict, key_cols) -> str | None:
    """Stable identity for one source row. Returns None when EVERY key part is
    empty — an unkeyable row is counted, never silently folded onto a shared
    key (which would manufacture change events between unrelated rows)."""
    parts = []
    for k in key_cols:
        v = _norm_value(rec.get(k))
        parts.append("" if v is None else v)
    if not any(parts):
        return None
    return _KEY_SEP.join(parts)[:900]


def _payload_hash(payload: dict) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def _diff_payload(old: dict, new: dict):
    """[(field, old, new)] over the union of tracked fields. A field missing
    from one side reads as None, so a dropped/added tracked column surfaces as
    a real change rather than a silent no-op."""
    out = []
    for f in sorted(set(old or {}) | set(new or {})):
        o = (old or {}).get(f)
        n = (new or {}).get(f)
        if o != n:
            out.append((f, o, n))
    return out


def _live_columns(cur, table: str) -> set:
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,))
    return {r[0] for r in cur.fetchall()}


def _last_run(cur, layer: str):
    cur.execute("SELECT status, truncated, next_offset, finished_at "
                "  FROM entity_capture_runs "
                " WHERE layer = %s ORDER BY started_at DESC LIMIT 1",
                (layer,))
    return cur.fetchone()


def _blank_result(layer: str, status: str, note=None) -> dict:
    """UNMEASURED ⇒ None counts. Never 0 — 0 reads as 'nothing changed'."""
    return {"layer": layer, "status": status, "note": note,
            "scanned": None, "appeared": None, "changed": None,
            "unkeyable": None, "truncated": False, "baseline": False,
            "next_offset": 0}


def _record_run(c, run_id: str, res: dict) -> None:
    """Append the run ledger row. Skipped-by-gate passes are NOT recorded — a
    ledger row for them would overwrite the resume offset of the truncated
    pass that preceded them."""
    if res.get("status") == "skipped_recent":
        return
    try:
        with c.cursor() as cur:
            cur.execute(_RUN_INSERT, (
                run_id, res.get("layer"), res.get("status") or "unknown",
                bool(res.get("baseline")), res.get("scanned"),
                res.get("appeared"), res.get("changed"),
                res.get("unkeyable"), bool(res.get("truncated")),
                int(res.get("next_offset") or 0),
                (res.get("note") or None)))
        c.commit()
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("temporal_capture: run ledger write failed (%s): %s",
                       res.get("layer"), str(e)[:160])


# ── Capture ──────────────────────────────────────────────────────────

def capture_layer(spec: dict, run_id: str, deadline: float,
                  force: bool = False) -> dict:
    """One layer, own short-lived connection, COMMIT PER CHUNK.

    Never raises: every failure degrades to a status + note in the result."""
    layer = spec["layer"]
    table = spec["table"]
    c = _conn()
    if c is None:
        return _blank_result(layer, "no_database")
    try:
        if not _ensure_schema(c):
            return _blank_result(layer, "schema_failed")

        # LIVE column set — the repo DDL is not authoritative here.
        try:
            with c.cursor() as cur:
                live = _live_columns(cur, table)
            c.commit()
        except Exception as e:
            try:
                c.rollback()
            except Exception:
                pass
            return _blank_result(layer, "introspect_failed", str(e)[:160])
        if not live:
            return _blank_result(layer, "no_such_table",
                                 "%s absent from information_schema.columns" % table)
        missing_key = [k for k in spec["key"] if k not in live]
        if missing_key:
            return _blank_result(layer, "missing_key_columns",
                                 "missing: " + ",".join(missing_key))
        key_cols = list(spec["key"])
        track_cols = [t for t in spec["track"] if t in live]
        dropped = [t for t in spec["track"] if t not in live]
        if not track_cols:
            return _blank_result(layer, "no_tracked_columns",
                                 "none of %s exist live" % (spec["track"],))

        # Gate + resume offset.
        try:
            with c.cursor() as cur:
                last = _last_run(cur, layer)
                cur.execute("SELECT COUNT(*) FROM entity_state WHERE layer = %s",
                            (layer,))
                existing = int((cur.fetchone() or [0])[0] or 0)
            c.commit()
        except Exception as e:
            try:
                c.rollback()
            except Exception:
                pass
            return _blank_result(layer, "state_read_failed", str(e)[:160])

        if last and not force:
            _st, _trunc, _noff, _fin = last
            if _st == "ok" and not _trunc and _fin is not None:
                try:
                    age_h = (datetime.datetime.now(datetime.timezone.utc)
                             - _fin).total_seconds() / 3600.0
                except Exception:
                    age_h = _GATE_HOURS
                if age_h < _GATE_HOURS:
                    return _blank_result(
                        layer, "skipped_recent",
                        "last full pass %.1fh ago (gate %.0fh)" % (age_h, _GATE_HOURS))

        offset = int((last[2] or 0)) if (last and last[1]) else 0
        baseline = existing == 0

        out = {"layer": layer, "status": "ok", "note": None,
               "scanned": 0, "appeared": 0, "changed": 0, "unkeyable": 0,
               "truncated": False, "baseline": baseline,
               "next_offset": offset, "start_offset": offset,
               "tracked_columns": track_cols, "dropped_columns": dropped}
        if baseline:
            out["note"] = ("BASELINE pass: state rows written with "
                           "first_seen_exact=false (this is when we started "
                           "watching, NOT when the entity appeared); no "
                           "change events emitted")

        sel_cols = key_cols + [t for t in track_cols if t not in key_cols]
        col_sql = ", ".join('"%s"' % col for col in sel_cols)
        # Order by the PK when the live table has one (cheap + stable under
        # the loaders' upserts); otherwise by the entity key.
        if "id" in live:
            order_sql = 'ORDER BY "id"'
        else:
            order_sql = "ORDER BY " + ", ".join('"%s"' % k for k in key_cols)
        read_sql = ('SELECT %s FROM "%s" %s LIMIT %%s OFFSET %%s'
                    % (col_sql, table, order_sql))

        from psycopg2.extras import Json, execute_values

        while True:
            if time.monotonic() >= deadline:
                out["truncated"] = True
                out["status"] = "truncated"
                out["note"] = ("budget exhausted at offset %d — resumes here "
                               "next tick" % offset)
                break
            try:
                with c.cursor() as cur:
                    cur.execute(read_sql, (_CHUNK, offset))
                    rows = cur.fetchall()
                c.commit()
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                out["status"] = "read_failed"
                out["note"] = str(e)[:160]
                out["truncated"] = True      # resume rather than gate
                break
            if not rows:
                out["next_offset"] = 0
                break

            keyed = []
            for r in rows:
                rec = dict(zip(sel_cols, r))
                ek = _entity_key(rec, key_cols)
                if not ek:
                    out["unkeyable"] += 1
                    continue
                payload = {t: _norm_value(rec.get(t)) for t in track_cols}
                keyed.append((ek, payload, _payload_hash(payload)))
            out["scanned"] += len(rows)

            prior = {}
            if keyed and not baseline:
                try:
                    with c.cursor() as cur:
                        cur.execute(
                            "SELECT entity_key, payload_hash, payload "
                            "  FROM entity_state "
                            " WHERE layer = %s AND entity_key = ANY(%s)",
                            (layer, [k for k, _p, _h in keyed]))
                        prior = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
                    c.commit()
                except Exception as e:
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    out["status"] = "state_read_failed"
                    out["note"] = str(e)[:160]
                    out["truncated"] = True
                    break

            state_rows, change_rows = [], []
            appeared = changed = 0
            for ek, payload, h in keyed:
                p = prior.get(ek)
                if p is None:
                    # Unknown to us. On the baseline pass that means "first
                    # look", NOT "new" — flagged via first_seen_exact=False
                    # and no event row.
                    state_rows.append((layer, ek, h, Json(payload),
                                       not baseline, 0))
                    if not baseline:
                        appeared += 1
                        change_rows.append((layer, ek, "appeared", None, None,
                                            None, Json(payload), run_id))
                    continue
                if p[0] == h:
                    state_rows.append((layer, ek, h, Json(payload), True, 0))
                    continue
                fields = _diff_payload(p[1] or {}, payload)
                for fld, ov, nv in fields:
                    change_rows.append((
                        layer, ek, "field_change", fld,
                        None if ov is None else str(ov)[:_VAL_MAX],
                        None if nv is None else str(nv)[:_VAL_MAX],
                        None, run_id))
                if fields:
                    changed += 1
                state_rows.append((layer, ek, h, Json(payload), True,
                                   1 if fields else 0))

            try:
                with c.cursor() as cur:
                    if state_rows:
                        execute_values(cur, _STATE_UPSERT, state_rows,
                                       page_size=500)
                    if change_rows:
                        execute_values(cur, _CHANGE_INSERT, change_rows,
                                       page_size=500)
                c.commit()          # ★ durable before the next chunk
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                out["status"] = "write_failed"
                out["note"] = str(e)[:160]
                out["truncated"] = True
                break

            out["appeared"] += appeared
            out["changed"] += changed
            offset += len(rows)
            out["next_offset"] = offset
            if len(rows) < _CHUNK:
                out["next_offset"] = 0
                break

        _record_run(c, run_id, out)
        return out
    except Exception as e:
        logger.warning("temporal_capture: layer %s failed: %s",
                       layer, str(e)[:200])
        res = _blank_result(layer, "error", str(e)[:160])
        try:
            _record_run(c, run_id, res)
        except Exception:
            pass
        return res
    finally:
        try:
            c.close()
        except Exception:
            pass


def run_temporal_capture(force: bool = False, layer: str | None = None) -> dict:
    """Capture pass across every registered layer. Never raises."""
    if os.environ.get("TEMPORAL_CAPTURE_DISABLE") == "1":
        return {"status": "disabled", "layers": {}}
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    deadline = time.monotonic() + _BUDGET_S
    out = {"status": "ok", "run_id": run_id, "budget_s": _BUDGET_S,
           "layers": {}}
    specs = [s for s in _LAYERS if (not layer or s["layer"] == layer)]
    if not specs:
        return {"status": "unknown_layer", "run_id": run_id,
                "known_layers": list(_LAYER_NAMES), "layers": {}}
    for spec in specs:
        try:
            res = capture_layer(spec, run_id, deadline, force=force)
        except Exception as e:          # belt-and-braces: never kill the pass
            res = _blank_result(spec["layer"], "error", str(e)[:160])
        res["why_captured"] = spec.get("why")
        out["layers"][spec["layer"]] = res
        if res.get("status") not in ("ok", "skipped_recent", "truncated"):
            out["status"] = "partial"
    banked = [v for v in out["layers"].values()
              if isinstance(v.get("changed"), int)]
    out["events_recorded"] = (
        sum((v.get("appeared") or 0) + (v.get("changed") or 0) for v in banked)
        if banked else None)
    out["note"] = ("Baseline passes bank state only — first_seen_exact=false "
                   "means 'first observed by us', not 'appeared then'. "
                   "Disappearance is NOT detected (a truncated pass cannot "
                   "tell missing from deleted).")
    logger.info("temporal_capture: %s", {k: v.get("status")
                                         for k, v in out["layers"].items()})
    return out


# ── Blueprint (safe-zone registered in main.py) ──────────────────────
from flask import Blueprint, jsonify, request as _rq  # noqa: E402

temporal_capture_bp = Blueprint("temporal_capture", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


@temporal_capture_bp.route("/api/v1/temporal/capture", methods=["POST"])
def temporal_capture_endpoint():
    provided = (_rq.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    force = _rq.args.get("force") == "1"
    layer = (_rq.args.get("layer") or "").strip() or None
    if layer and layer not in _LAYER_NAMES:
        return jsonify(error="unknown_layer",
                       known_layers=list(_LAYER_NAMES)), 400
    if _rq.args.get("sync") == "1":
        return jsonify(run_temporal_capture(force=force, layer=layer)), 200
    import threading
    threading.Thread(
        target=lambda: run_temporal_capture(force=force, layer=layer),
        name="temporal-capture-manual", daemon=True).start()
    return jsonify(status="spawned", force=force, layer=layer), 202


@temporal_capture_bp.route("/api/v1/temporal/coverage", methods=["GET"])
def temporal_coverage_endpoint():
    """What has actually been banked. Read-only, creates nothing — before the
    first capture pass this reports not_initialized, not zeros."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    layers = {}
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.entity_state')")
            if not (cur.fetchone() or [None])[0]:
                return jsonify(status="not_initialized",
                               hint=("no capture pass has run yet — "
                                     "POST /api/v1/temporal/capture"),
                               known_layers=list(_LAYER_NAMES)), 200
            cur.execute(
                "SELECT layer, COUNT(*), "
                "       COUNT(*) FILTER (WHERE first_seen_exact), "
                "       MIN(first_seen), MAX(last_seen) "
                "  FROM entity_state GROUP BY layer")
            for r in cur.fetchall():
                layers[r[0]] = {
                    "entities_tracked": int(r[1] or 0),
                    "with_exact_first_seen": int(r[2] or 0),
                    "watching_since": r[3].isoformat() if r[3] else None,
                    "last_seen": r[4].isoformat() if r[4] else None,
                    "changes_recorded": None,
                    "last_run": None,
                }
            cur.execute("SELECT layer, COUNT(*), MAX(detected_at) "
                        "  FROM entity_changes GROUP BY layer")
            for r in cur.fetchall():
                layers.setdefault(r[0], {"entities_tracked": None})
                layers[r[0]]["changes_recorded"] = int(r[1] or 0)
                layers[r[0]]["last_change_at"] = (
                    r[2].isoformat() if r[2] else None)
            cur.execute("SELECT DISTINCT ON (layer) layer, status, truncated, "
                        "       scanned, appeared, changed, finished_at "
                        "  FROM entity_capture_runs "
                        " ORDER BY layer, started_at DESC")
            for r in cur.fetchall():
                layers.setdefault(r[0], {"entities_tracked": None})
                layers[r[0]]["last_run"] = {
                    "status": r[1], "truncated": bool(r[2]),
                    "scanned": r[3], "appeared": r[4], "changed": r[5],
                    "finished_at": r[6].isoformat() if r[6] else None,
                }
    except Exception as e:
        return jsonify(error="read_failed", detail=str(e)[:160]), 200
    finally:
        try:
            c.close()
        except Exception:
            pass
    for name in _LAYER_NAMES:
        layers.setdefault(name, {"entities_tracked": None,
                                 "status": "never_captured"})
    return jsonify(
        status="ok", layers=layers, known_layers=list(_LAYER_NAMES),
        basis=("entity_state = current per-entity state (upserted); "
               "entity_changes = append-only events; entity_capture_runs = "
               "the run ledger (cron-green is not proof a pass ran)"),
        honest_note=("with_exact_first_seen counts entities whose appearance "
                     "date we actually observed. The remainder were present "
                     "at baseline — their first_seen is when capture started, "
                     "NOT when they appeared. Disappearance is not detected."),
    ), 200
