"""Brain Layer-6 STRATEGIC-OUTCOME LEDGER (feature #8, 2026-06-28).

Closes the accountability loop on the weekly strategic synthesis. The
planner (brain_strategic_planner.py) already proposes 3-7 recs/week and
records whether the *PR* shipped — but it never measured whether the
business METRIC the rec was supposed to move actually moved.

This module adds three additive, fail-safe pieces:

  (a) brain_strategic_outcomes table — one row per *verifiable* rec, with
      a derived target_metric + a baseline snapshot captured at persist
      time. Recs with no derivable metric land as verifiable=FALSE so a
      vague rec ("improve the funnel") is NEVER falsely credited.

  (b) stamp_strategic_outcomes() — a Tier-3 master-tick step. It re-reads
      each baselined metric 14d (and again 30d) after persist and stamps
      the row moved / flat / regressed. Pure detector + ledger UPDATE; no
      live behaviour, safe to run live.

  (c) gather_ledger_feedback() — a per-rec-type success summary the
      planner can splice into its prompt. DARK behind
      BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED (default OFF); when off the
      planner prompt is byte-identical to today.

Every public function is wrapped so any exception degrades to today's
behaviour (empty envelope / no-op) and never breaks a tick.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Config / flags ─────────────────────────────────────────────────

def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def ledger_feedback_enabled() -> bool:
    """DARK flag. When OFF (default) the planner prompt is unchanged."""
    return _truthy(os.environ.get("BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED"))


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# How much a metric must move (fraction of baseline) to count as a real
# change rather than noise. ±5% → "flat".
_FLAT_BAND = 0.05


# ─── Table (idempotent self-create; also registered in schema_repair) ─

_DDL = (
    """CREATE TABLE IF NOT EXISTS brain_strategic_outcomes (
        id              BIGSERIAL PRIMARY KEY,
        rec_id          BIGINT,
        run_id          TEXT,
        week_of         DATE,
        kind            TEXT,
        title           TEXT,
        target_metric   TEXT,
        metric_source   TEXT,
        verifiable      BOOLEAN NOT NULL DEFAULT FALSE,
        baseline_value  DOUBLE PRECISION,
        baseline_at     TIMESTAMPTZ,
        check_14d_value DOUBLE PRECISION,
        check_14d_at    TIMESTAMPTZ,
        check_30d_value DOUBLE PRECISION,
        check_30d_at    TIMESTAMPTZ,
        outcome         TEXT NOT NULL DEFAULT 'pending',
        direction       TEXT,
        delta_pct       DOUBLE PRECISION,
        note            TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_bso_rec_id ON brain_strategic_outcomes(rec_id) WHERE rec_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_bso_outcome ON brain_strategic_outcomes(outcome)",
    "CREATE INDEX IF NOT EXISTS ix_bso_kind ON brain_strategic_outcomes(kind)",
    "CREATE INDEX IF NOT EXISTS ix_bso_week_of ON brain_strategic_outcomes(week_of DESC)",
    "CREATE INDEX IF NOT EXISTS ix_bso_verifiable ON brain_strategic_outcomes(verifiable) WHERE verifiable = TRUE",
)


def ensure_table(cur=None) -> bool:
    """Idempotently create the ledger table. Safe to call repeatedly.

    If a cursor is passed we run inside the caller's tx; otherwise we
    open our own connection. Returns True on success, False on any miss.
    """
    if cur is not None:
        try:
            for stmt in _DDL:
                cur.execute(stmt)
            return True
        except Exception as e:
            logger.warning("L6 ledger: ensure_table (shared cur) failed: %s", e)
            return False
    c = _get_db()
    if c is None:
        return False
    try:
        with c.cursor() as cur2:
            for stmt in _DDL:
                cur2.execute(stmt)
        c.commit()
        return True
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("L6 ledger: ensure_table failed: %s", e)
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Metric resolution ──────────────────────────────────────────────
# A "target_metric" is a stable, machine-readable handle of the form
#   "<source>:<dotted.path>"
# that both the baseline capture (reads from the in-memory synthesis
# ctx) AND the later stamping pass (reads the same endpoint live) can
# resolve to a single float. Only metrics we can actually re-read are
# verifiable; everything else lands verifiable=FALSE.
#
# Sources map to live JSON endpoints (re-read at stamping time):
_METRIC_SOURCES = {
    # source-key            : (endpoint path,                         ctx path into _gather_strategic_context())
    "funnel_now":  ("/api/v1/mcp/funnel",                      ("funnel", "now")),
    "funnel_30d":  ("/api/v1/mcp/conversion-funnel?days=30",   ("funnel", "d30")),
    "self_model":  ("/api/v1/brain/self-model",                ("self_model",)),
}

# Curated whitelist of headline metrics the brain is allowed to target,
# keyed by the metric handle. value is the dotted path WITHIN that
# source's JSON. Keeping this curated means a rec can only be credited
# against a metric we know how to read deterministically.
_KNOWN_METRICS = {
    "funnel_now:ai_agent_requests_total":        ("funnel_now", "ai_agent_requests_total"),
    "funnel_now:annualized_run_rate_from_7d":    ("funnel_now", "annualized_run_rate_from_7d"),
}


def _dig(obj: Any, path) -> Any:
    """Walk a dotted/tuple path into nested dict/list. None on miss."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(key)]
            except Exception:
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_path(leaf) -> tuple:
    """Normalize a leaf descriptor into a path tuple for _dig.

    A dotted string ("a.b") or a bare key ("foo") becomes a tuple; an
    already-tuple/list is returned as-is. Prevents _dig from iterating a
    plain string char-by-char.
    """
    if isinstance(leaf, (tuple, list)):
        return tuple(leaf)
    if isinstance(leaf, str):
        return tuple(leaf.split(".")) if "." in leaf else (leaf,)
    return (leaf,)


def _coerce_float(v) -> Optional[float]:
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.strip():
            return float(v.strip())
    except Exception:
        return None
    return None


def derive_target_metric(item: dict, kind: str) -> Optional[dict]:
    """Derive a verifiable target_metric handle from a rec item.

    Honours an explicit `target_metric` field on the rec if the model
    supplied one AND it is in our known whitelist. Otherwise falls back
    to a kind-based default for funnel_optimization recs (the only kind
    with an unambiguous headline metric). Returns:

        {"metric": handle, "source": source_key, "path": (..)}  (verifiable)

    or None when no deterministic metric can be derived (→ the caller
    will store the rec as verifiable=FALSE so it is never auto-credited).
    """
    try:
        explicit = (item.get("target_metric") or "").strip()
        if explicit and explicit in _KNOWN_METRICS:
            src, path = _KNOWN_METRICS[explicit]
            return {"metric": explicit, "source": src, "path": (path,)}
        # Kind-based default: only funnel_optimization recs get an
        # automatic headline metric. Everything else stays unverifiable
        # unless the model named a known metric explicitly.
        if kind == "funnel_optimization":
            handle = "funnel_now:annualized_run_rate_from_7d"
            src, path = _KNOWN_METRICS[handle]
            return {"metric": handle, "source": src, "path": (path,)}
    except Exception as e:
        logger.debug("L6 ledger: derive_target_metric error: %s", e)
    return None


def _read_metric_from_ctx(ctx: dict, source: str, leaf_path) -> Optional[float]:
    """Resolve a metric value from the in-memory synthesis ctx."""
    try:
        src = _METRIC_SOURCES.get(source)
        if not src:
            return None
        _endpoint, ctx_path = src
        root = _dig(ctx, ctx_path)
        return _coerce_float(_dig(root, _as_path(leaf_path)))
    except Exception:
        return None


def _read_metric_live(source: str, leaf_path) -> Optional[float]:
    """Re-read a metric value live from its endpoint (stamping time)."""
    try:
        src = _METRIC_SOURCES.get(source)
        if not src:
            return None
        endpoint, _ctx_path = src
        # Reuse the planner's hardened HTTP helper so we share timeouts +
        # admin headers + base-URL logic. Import lazily to avoid cycles.
        try:
            from routes.brain_strategic_planner import _http_get_json
        except Exception:
            return None
        body = _http_get_json(endpoint)
        return _coerce_float(_dig(body, _as_path(leaf_path)))
    except Exception:
        return None


# ─── (a) Baseline capture at rec-persist time ───────────────────────

def record_baseline(cur, *, rec_id: Optional[int], run_id: str,
                    week_of, kind: str, title: str, item: dict,
                    ctx: dict) -> None:
    """Insert one ledger row for a rec, capturing its baseline snapshot.

    MUST be called from inside the planner's persist tx (shares `cur` so
    it commits/rolls-back atomically with the rec). Fail-safe: any error
    is swallowed and logged — a ledger miss never blocks rec persistence.

    Verifiable recs get target_metric + baseline_value; vague recs land
    verifiable=FALSE with outcome='unverifiable' so they cannot later be
    credited as a win.
    """
    try:
        ensure_table(cur)
        spec = derive_target_metric(item, kind)
        if spec:
            baseline = _read_metric_from_ctx(ctx, spec["source"], spec["path"][0])
            # A metric we can name but not currently read (None baseline)
            # is still verifiable in principle, but without a baseline we
            # cannot compute a delta → treat as unverifiable this run.
            if baseline is None:
                cur.execute(
                    """INSERT INTO brain_strategic_outcomes(
                        rec_id, run_id, week_of, kind, title,
                        target_metric, metric_source, verifiable,
                        baseline_value, baseline_at, outcome, note
                      ) VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,NULL,NULL,
                                'unverifiable',%s)
                      ON CONFLICT (rec_id) WHERE rec_id IS NOT NULL DO NOTHING""",
                    (rec_id, run_id, week_of, kind, title[:200],
                     spec["metric"], spec["source"],
                     "metric named but no baseline reading available"))
                return
            cur.execute(
                """INSERT INTO brain_strategic_outcomes(
                    rec_id, run_id, week_of, kind, title,
                    target_metric, metric_source, verifiable,
                    baseline_value, baseline_at, outcome
                  ) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s,NOW() ON CONFLICT DO NOTHING,'pending')
                  ON CONFLICT (rec_id) WHERE rec_id IS NOT NULL DO NOTHING""",
                (rec_id, run_id, week_of, kind, title[:200],
                 spec["metric"], spec["source"], baseline))
        else:
            cur.execute(
                """INSERT INTO brain_strategic_outcomes(
                    rec_id, run_id, week_of, kind, title,
                    verifiable, outcome, note
                  ) VALUES (%s,%s,%s,%s,%s,FALSE,'unverifiable',%s)
                  ON CONFLICT (rec_id) WHERE rec_id IS NOT NULL DO NOTHING""",
                (rec_id, run_id, week_of, kind, title[:200],
                 "no deterministic target_metric derivable"))
    except Exception as e:
        # Never let a ledger miss abort the rec persist. We do NOT
        # rollback here — the caller owns the tx.
        logger.warning("L6 ledger: record_baseline failed (rec_id=%s): %s",
                        rec_id, e)


# ─── (b) Tier-3 stamping pass ───────────────────────────────────────

def _classify(baseline: float, current: float) -> tuple:
    """Return (outcome, direction, delta_pct)."""
    if baseline == 0:
        # Avoid div-by-zero; treat any nonzero move as the direction.
        if current == 0:
            return ("flat", "flat", 0.0)
        return (("moved" if current > 0 else "regressed"),
                ("up" if current > 0 else "down"), None)
    delta = (current - baseline) / abs(baseline)
    delta_pct = round(delta * 100.0, 2)
    if abs(delta) < _FLAT_BAND:
        return ("flat", "flat", delta_pct)
    if delta > 0:
        return ("moved", "up", delta_pct)
    return ("regressed", "down", delta_pct)


def stamp_strategic_outcomes(max_rows: int = 200) -> dict:
    """Tier-3 step: re-read baselined metrics 14d/30d later and stamp.

    Pure read + ledger UPDATE. No live behaviour, no PRs, no shell.
    Fail-safe → returns an envelope; never raises.

    14d window: any verifiable+pending row whose baseline is >=14d old
    gets check_14d_value + a preliminary outcome.
    30d window: rows already 14d-stamped and now >=30d old get the
    final check_30d_value and the outcome is re-stamped on the 30d delta.
    """
    out = {"ok": True, "checked_14d": 0, "checked_30d": 0, "errors": 0}
    if not ensure_table():
        out["ok"] = False
        out["reason"] = "no_db_or_table"
        return out
    c = _get_db()
    if c is None:
        out["ok"] = False
        out["reason"] = "no_db"
        return out
    try:
        # ---- 14d preliminary stamp ----
        rows = []
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, target_metric, metric_source, baseline_value
                     FROM brain_strategic_outcomes
                    WHERE verifiable = TRUE
                      AND outcome = 'pending'
                      AND check_14d_at IS NULL
                      AND baseline_at IS NOT NULL
                      AND baseline_at <= NOW() - INTERVAL '14 days'
                    ORDER BY baseline_at ASC
                    LIMIT %s""", (max_rows,))
            rows = cur.fetchall() or []
        for rid, metric, source, baseline in rows:
            try:
                handle = (metric or "")
                leaf = handle.split(":", 1)[1] if ":" in handle else handle
                cur_val = _read_metric_live(source, leaf)
                if cur_val is None or baseline is None:
                    continue
                outcome, direction, delta_pct = _classify(
                    float(baseline), float(cur_val))
                with c.cursor() as cur:
                    cur.execute(
                        """UPDATE brain_strategic_outcomes
                              SET check_14d_value=%s, check_14d_at=NOW(),
                                  outcome=%s, direction=%s, delta_pct=%s,
                                  updated_at=NOW()
                            WHERE id=%s""",
                        (cur_val, outcome, direction, delta_pct, rid))
                c.commit()
                out["checked_14d"] += 1
            except Exception as e:
                out["errors"] += 1
                try:
                    c.rollback()
                except Exception:
                    pass
                logger.warning("L6 ledger: 14d stamp id=%s failed: %s", rid, e)

        # ---- 30d final stamp ----
        rows = []
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, target_metric, metric_source, baseline_value
                     FROM brain_strategic_outcomes
                    WHERE verifiable = TRUE
                      AND check_14d_at IS NOT NULL
                      AND check_30d_at IS NULL
                      AND baseline_at <= NOW() - INTERVAL '30 days'
                    ORDER BY baseline_at ASC
                    LIMIT %s""", (max_rows,))
            rows = cur.fetchall() or []
        for rid, metric, source, baseline in rows:
            try:
                handle = (metric or "")
                leaf = handle.split(":", 1)[1] if ":" in handle else handle
                cur_val = _read_metric_live(source, leaf)
                if cur_val is None or baseline is None:
                    continue
                outcome, direction, delta_pct = _classify(
                    float(baseline), float(cur_val))
                with c.cursor() as cur:
                    cur.execute(
                        """UPDATE brain_strategic_outcomes
                              SET check_30d_value=%s, check_30d_at=NOW(),
                                  outcome=%s, direction=%s, delta_pct=%s,
                                  updated_at=NOW()
                            WHERE id=%s""",
                        (cur_val, outcome, direction, delta_pct, rid))
                c.commit()
                out["checked_30d"] += 1
            except Exception as e:
                out["errors"] += 1
                try:
                    c.rollback()
                except Exception:
                    pass
                logger.warning("L6 ledger: 30d stamp id=%s failed: %s", rid, e)
    except Exception as e:
        out["ok"] = False
        out["reason"] = str(e)[:160]
        logger.warning("L6 ledger: stamp pass failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ─── (c) Per-rec-type feedback for the prompt (DARK) ────────────────

def gather_ledger_feedback(lookback_days: int = 120) -> dict:
    """Summarise stamped outcomes per rec-kind for the synthesis prompt.

    Returns {by_kind: {kind: {moved, flat, regressed, unverifiable,
    verified_total, hit_rate}}, ...}. Fail-soft → empty envelope.

    NOTE: the *planner* only splices this into the prompt when
    BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED is set; this function is
    read-only and safe to call regardless.
    """
    env = {"_note": "ledger_feedback", "lookback_days": lookback_days,
           "by_kind": {}}
    c = _get_db()
    if c is None:
        env["_note"] = "no_db"
        return env
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT kind, outcome, COUNT(*)
                     FROM brain_strategic_outcomes
                    WHERE created_at > NOW() - %s * INTERVAL '1 day'
                    GROUP BY kind, outcome""", (lookback_days,))
            for kind, outcome, n in cur.fetchall() or []:
                k = kind or "unknown"
                d = env["by_kind"].setdefault(
                    k, {"moved": 0, "flat": 0, "regressed": 0,
                         "unverifiable": 0, "pending": 0})
                d[outcome if outcome in d else "pending"] = int(n)
        for k, d in env["by_kind"].items():
            verified = d["moved"] + d["flat"] + d["regressed"]
            d["verified_total"] = verified
            d["hit_rate"] = (round(d["moved"] / verified, 3)
                             if verified else None)
    except Exception as e:
        logger.warning("L6 ledger: gather_feedback failed: %s", e)
        env["_note"] = "read_failed"
        env["error"] = str(e)[:160]
    finally:
        try:
            c.close()
        except Exception:
            pass
    return env


def format_feedback_for_prompt(env: dict, budget: int = 1500) -> str:
    """Render the feedback envelope to a compact prompt block."""
    try:
        by_kind = (env or {}).get("by_kind") or {}
        if not by_kind:
            return "(no stamped strategic outcomes yet — ledger still warming up)"
        lines = []
        for kind, d in sorted(by_kind.items()):
            hr = d.get("hit_rate")
            hr_s = f"{int(hr*100)}%" if hr is not None else "n/a"
            lines.append(
                f"- {kind}: moved={d.get('moved',0)} flat={d.get('flat',0)} "
                f"regressed={d.get('regressed',0)} "
                f"(verified={d.get('verified_total',0)}, hit-rate={hr_s}; "
                f"unverifiable={d.get('unverifiable',0)})")
        text = "\n".join(lines)
        return text[:budget]
    except Exception:
        return "(ledger feedback unavailable)"
