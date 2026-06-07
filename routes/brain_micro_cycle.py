"""
brain_micro_cycle.py — Brain HOURLY Ops Micro-Decision Loop (2026-06-07).
========================================================================

PURPOSE
-------
The L6 strategic synthesis (brain_strategic_planner.py) runs TWICE A WEEK
(Mon + Thu at 08:00 UTC) on Opus 4.8 — deep, slow, expensive (~$1/run,
~$10/yr). That's perfect for "what should DC Hub ship in the next 4
weeks?" but useless for "the funnel just collapsed in the last hour —
do something."

This module fills the operational gap with a LIGHT, HOURLY cycle on
Haiku 4.5 (~$0.001–$0.005/run, ~$1-2/day, capped at $5/day = $1,800/yr).
Each hour it:

  1. Pulls a tight 7-day funnel-health snapshot + sentinel state + the
     last hour's media activity. Total context ~2 KB.
  2. Asks Haiku for: 1 anomaly to address NOW, 1 micro-action to
     schedule, 1 metric to watch next hour. Output is a single JSON
     object (~200 tokens).
  3. Writes the row to brain_micro_decisions and the anomaly (if any)
     to brain_findings so the L6 strategic synthesis can consider it.
  4. Tracks daily spend in brain_micro_budget_state. Skips the Claude
     call when the cap is hit OR when context is identical to the last
     run (sha256 of the context dict).

SAFETY
------
  · BRAIN_MICRO_CYCLE_DISABLE=1            kill switch
  · BRAIN_MICRO_DAILY_BUDGET_USD=5.00      daily cap (default $5)
  · BRAIN_MICRO_CONTEXT_DEDUP_DISABLE=1    bypass context-hash dedup
  · BRAIN_MICRO_DRY_RUN=1                  build context but skip Claude
  · Idempotent UNIQUE(DATE(ran_at), hour) — same hour can't double-fire.
  · Defensive — never raises (cron caller catches anyway).

SCHEDULE
--------
The crawler_scheduler SCHEDULE harness is hour-based with TWO slots per
entry → can't do 24/day inside that table. We ship a separate background
thread (_start_micro_loop, called from the same boot path init_content_
tables() lives on). It wakes every 5 min and fires when the current UTC
hour hasn't run yet.

That mirrors the precedent of the crawler_scheduler thread itself
(routes/crawler_scheduler._scheduler_loop runs as its own daemon) so
nothing structural changes — we just add one more daemon.

COST
----
Per call (Haiku 4.5):
  · Input: ~2000 tokens × $0.80/M = $0.0016
  · Output: ~300 tokens × $4.0/M = $0.0012
  · Per call: ~$0.003
  · Per day: 24 calls × $0.003 = ~$0.07 expected
  · Daily cap (BRAIN_MICRO_DAILY_BUDGET_USD): $5.00 = ~1,650 calls worst
    case (way more than 24/day; the cap is a runaway guard).
  · Per year: ~$25 expected, $1,800 hard cap.

Dedup is the dominant cost saver: if the funnel snapshot is identical
to the previous hour, we skip the Claude call entirely and just bump the
no-op counter on the prior row.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from typing import Any, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_micro_bp = Blueprint("brain_micro_cycle", __name__)

# ─── Config ─────────────────────────────────────────────────────────

_INTERNAL_BASE = (os.environ.get("INTERNAL_BASE_URL")
                  or "http://localhost:8080").rstrip("/")
_RAILWAY_BASE = "https://dchub-backend-production.up.railway.app"
_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

# Hard input-context cap so a runaway upstream cannot inflate the call.
_CTX_HARD_CAP_CHARS = 8000  # ~2,000 tokens
_DEFAULT_BUDGET_USD = 5.00


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("BRAIN_MICRO_CYCLE_DISABLE"))


def _dry_run() -> bool:
    return _truthy(os.environ.get("BRAIN_MICRO_DRY_RUN"))


def _dedup_disabled() -> bool:
    return _truthy(os.environ.get("BRAIN_MICRO_CONTEXT_DEDUP_DISABLE"))


def _daily_budget_usd() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "BRAIN_MICRO_DAILY_BUDGET_USD", str(_DEFAULT_BUDGET_USD))))
    except Exception:
        return _DEFAULT_BUDGET_USD


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


# ─── DB ─────────────────────────────────────────────────────────────

def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def init_micro_tables() -> None:
    """Idempotent CREATE for brain_micro_decisions + brain_micro_budget_
    state. Called from content_publisher.init_content_tables()."""
    c = _get_db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_micro_decisions (
                    id                  SERIAL PRIMARY KEY,
                    ran_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    hour_utc            INT NOT NULL,
                    date_utc            DATE NOT NULL,
                    model               TEXT,
                    anomaly             TEXT,
                    anomaly_severity    TEXT,
                    micro_action        TEXT,
                    watch_metric        TEXT,
                    confidence          REAL,
                    context_summary     TEXT,
                    context_hash        TEXT,
                    -- claude_cost_cents stored as NUMERIC so fractional
                    -- Haiku costs (~0.3¢/call) don't round to 0.
                    claude_cost_cents   NUMERIC(10, 4) DEFAULT 0,
                    skipped_reason      TEXT,
                    raw_payload         JSONB
                )
            """)
            # If a prior boot landed the INT version, widen it.
            try:
                cur.execute("""
                    ALTER TABLE brain_micro_decisions
                      ALTER COLUMN claude_cost_cents TYPE NUMERIC(10, 4)
                      USING claude_cost_cents::NUMERIC(10, 4)
                """)
            except Exception:
                pass
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                  brain_micro_decisions_dh_uq
                  ON brain_micro_decisions (date_utc, hour_utc)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS brain_micro_decisions_ran_at_idx
                  ON brain_micro_decisions (ran_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_micro_budget_state (
                    day             DATE PRIMARY KEY,
                    spent_cents     NUMERIC(10, 4) NOT NULL DEFAULT 0,
                    runs_count      INT NOT NULL DEFAULT 0,
                    skipped_count   INT NOT NULL DEFAULT 0,
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # If a prior boot landed the INT version, widen it.
            try:
                cur.execute("""
                    ALTER TABLE brain_micro_budget_state
                      ALTER COLUMN spent_cents TYPE NUMERIC(10, 4)
                      USING spent_cents::NUMERIC(10, 4)
                """)
            except Exception:
                pass
            try:
                c.commit()
            except Exception:
                pass
        logger.info("brain_micro_cycle: tables initialized")
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("brain_micro_cycle: init_micro_tables failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _hour_utc() -> int:
    return _dt.datetime.now(_dt.timezone.utc).hour


# ─── Context gather (~2 KB total) ───────────────────────────────────

def _http_get_json(path: str, timeout: int = 6) -> dict:
    """Fetch JSON from a backend route — prefers Railway external so a
    same-worker loopback can't deadlock. Fail-soft → {}."""
    import urllib.request
    headers = {"X-Internal-Probe": "1",
               "User-Agent": "dchub-brain-micro/1.0"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
    for base in (_RAILWAY_BASE, _INTERNAL_BASE):
        try:
            req = urllib.request.Request(f"{base}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, (dict, list)) else {}
        except Exception:
            continue
    return {}


def _slim(obj: Any, keep_keys: Optional[list] = None,
          truncate_lists: int = 5) -> Any:
    """Keep only the keys that matter for hourly ops. The strategic
    synthesis sees the full envelope; this loop wants ~200 chars per
    source."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        if keep_keys is None:
            keep_keys = list(obj.keys())[:6]
        out = {}
        for k in keep_keys:
            if k in obj:
                v = obj[k]
                if isinstance(v, list):
                    v = v[:truncate_lists]
                elif isinstance(v, dict):
                    # one-level recursion
                    v = {kk: vv for kk, vv in list(v.items())[:6]}
                out[k] = v
        return out
    if isinstance(obj, list):
        return obj[:truncate_lists]
    return obj


def gather_micro_context() -> dict:
    """Pull tight 1-hour ops snapshot. Each input is fail-soft.

    Total budget ~6 KB. We slim every input down to top-level keys + a
    handful of recent rows so the prompt stays under the 2,000-token cap.
    """
    # Funnel — the now-snapshot is enough; we want hourly drift not
    # 30-day baselines (that's the strategic loop's job). Keys named to
    # match the LIVE /api/v1/mcp/funnel shape (probed 2026-06-07).
    funnel = _http_get_json("/api/v1/mcp/funnel") or {}
    funnel_slim = _slim(funnel, keep_keys=[
        "ai_agent_requests_total",
        "ai_agent_top_platforms",
        "tool_calls_7d", "tool_calls_7d_real", "tool_calls_30d",
        "conversions_30d", "keys_by_tier",
        "annualized_run_rate_from_7d",
        "press_headline_metric",
        "time_to_convert_90d",
        "real_external_7d",
    ])

    # Sentinel page-integrity — slim to the verdict rollup + a few
    # unhealthy pages. Live shape (probed 2026-06-07) is site_score +
    # verdict_breakdown + per-page list; we don't ship the whole 69-page
    # blob to Claude.
    sentinel = _http_get_json("/api/v1/sentinel/page-integrity") or {}
    sentinel_slim = {
        "site_score":         sentinel.get("site_score"),
        "site_verdict":       sentinel.get("site_verdict"),
        "verdict_breakdown":  sentinel.get("verdict_breakdown"),
        "pages_total":        sentinel.get("pages_total"),
        "generated_at":       sentinel.get("generated_at"),
        # Top 5 worst pages so the loop can call them out by path.
        # Live page row uses 'path', 'verdict', 'score', 'last_status',
        # 'label' — probed 2026-06-07. Sort by score ascending so the
        # worst come first.
        "worst_5_pages":      [
            {"path":     (p or {}).get("path"),
             "label":    (p or {}).get("label"),
             "verdict":  (p or {}).get("verdict"),
             "score":    (p or {}).get("score"),
             "last_status": (p or {}).get("last_status")}
            for p in sorted(
                sentinel.get("pages") or [],
                key=lambda x: (x or {}).get("score") or 100)[:5]
        ],
    }

    # Brain backlog: stuck items count + recent additions. Bound to 5.
    backlog = _http_get_json("/api/v1/admin/brain/backlog") or {}
    backlog_slim = {
        "stuck_total":     backlog.get("stuck_total"),
        "stuck_by_source": backlog.get("stuck_by_source"),
        "proposed_code":   _slim(backlog.get("proposed_code") or {},
                                  keep_keys=[
                                      "total_rows", "high_conf_count",
                                      "pending_pr_match"]),
        "stuck_items":     [
            {"issue_label": (it or {}).get("issue_label"),
             "seen_count":  (it or {}).get("seen_count"),
             "source":      (it or {}).get("source")}
            for it in (backlog.get("stuck_items") or [])[:5]
        ],
    }

    # Last hour's media activity — newest posts + immediate engagement.
    media = _read_recent_media_activity()

    # Last hour's HTTP error sample (5xx burst detector) — small DB read
    last_errs = _read_recent_errors()

    return {
        "as_of_utc":  _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "funnel":     funnel_slim,
        "sentinel":   sentinel_slim,
        "backlog":    backlog_slim,
        "media_1h":   media,
        "errors_1h":  last_errs,
    }


def _read_recent_media_activity() -> dict:
    c = _get_db()
    if c is None:
        return {"_note": "no_db"}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT platform, status,
                       COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour') AS h1,
                       COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS h24
                  FROM social_media_posts
                 WHERE created_at > NOW() - INTERVAL '24 hours'
                 GROUP BY platform, status
                 ORDER BY h24 DESC
                 LIMIT 8
            """)
            rows = cur.fetchall() or []
            return {
                "by_platform_status": [
                    {"platform": r[0], "status": r[1],
                     "last_hour": int(r[2] or 0),
                     "last_24h": int(r[3] or 0)}
                    for r in rows
                ],
                "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
    except Exception as e:
        return {"_note": "read_failed", "error": str(e)[:120]}
    finally:
        try:
            c.close()
        except Exception:
            pass


def _read_recent_errors() -> dict:
    """Cheap 5xx-burst snapshot — checks brain_findings for HTTP error
    issues filed in the last hour. brain_findings is the canonical
    log of detector signals; we don't need to scrape gunicorn logs."""
    c = _get_db()
    if c is None:
        return {"_note": "no_db"}
    try:
        with c.cursor() as cur:
            # Inspect schema first to handle the brain_findings drift
            # (see brain_findings_writer.py — last_seen/created_at vary).
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'brain_findings'
            """)
            cols = {r[0] for r in cur.fetchall() or []}
            ts = ("last_seen" if "last_seen" in cols
                  else "created_at" if "created_at" in cols
                  else None)
            if not ts:
                return {"_note": "no_timestamp_col"}
            cur.execute(
                f"""SELECT issue, url, COALESCE(count, 1)
                       FROM brain_findings
                      WHERE {ts} > NOW() - INTERVAL '1 hour'
                        AND (issue ILIKE '%%5xx%%'
                             OR issue ILIKE '%%http_error%%'
                             OR issue ILIKE '%%500%%')
                      ORDER BY {ts} DESC
                      LIMIT 5""")
            rows = cur.fetchall() or []
            return {
                "recent_5xx": [
                    {"issue": r[0], "url": (r[1] or "")[:120],
                     "count": int(r[2] or 1)}
                    for r in rows
                ],
                "count": len(rows),
            }
    except Exception as e:
        return {"_note": "read_failed", "error": str(e)[:120]}
    finally:
        try:
            c.close()
        except Exception:
            pass


def _context_hash(ctx: dict) -> str:
    """Deterministic SHA256 of the slimmed context (excluding the
    as_of_utc timestamp, which would defeat dedup). Used to skip the
    Claude call when nothing has changed."""
    ctx_copy = {k: v for k, v in (ctx or {}).items() if k != "as_of_utc"}
    s = json.dumps(ctx_copy, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _last_context_hash() -> Optional[str]:
    c = _get_db()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT context_hash FROM brain_micro_decisions
                 WHERE skipped_reason IS NULL
                 ORDER BY ran_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Budget ─────────────────────────────────────────────────────────

def _today_budget_state() -> dict:
    c = _get_db()
    if c is None:
        return {"day": str(_today_utc()), "spent_cents": 0,
                "runs_count": 0, "skipped_count": 0}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT day, spent_cents, runs_count, skipped_count
                  FROM brain_micro_budget_state
                 WHERE day = CURRENT_DATE
            """)
            row = cur.fetchone()
            if row:
                return {
                    "day": str(row[0]),
                    "spent_cents": float(row[1] or 0),
                    "runs_count": int(row[2] or 0),
                    "skipped_count": int(row[3] or 0),
                }
            return {"day": str(_today_utc()), "spent_cents": 0.0,
                    "runs_count": 0, "skipped_count": 0}
    except Exception:
        return {"day": str(_today_utc()), "spent_cents": 0.0,
                "runs_count": 0, "skipped_count": 0}
    finally:
        try:
            c.close()
        except Exception:
            pass


def _record_spend(cents: float, skipped: bool) -> None:
    c = _get_db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_micro_budget_state
                  (day, spent_cents, runs_count, skipped_count, updated_at)
                VALUES (CURRENT_DATE, %s, %s, %s, NOW())
                ON CONFLICT (day) DO UPDATE SET
                  spent_cents  = brain_micro_budget_state.spent_cents  + EXCLUDED.spent_cents,
                  runs_count   = brain_micro_budget_state.runs_count   + EXCLUDED.runs_count,
                  skipped_count = brain_micro_budget_state.skipped_count + EXCLUDED.skipped_count,
                  updated_at   = NOW()
            """, (float(cents), 0 if skipped else 1, 1 if skipped else 0))
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("brain_micro_cycle: _record_spend failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass


def _budget_exhausted() -> bool:
    cap_cents = _daily_budget_usd() * 100.0
    state = _today_budget_state()
    return float(state.get("spent_cents", 0) or 0) >= cap_cents


# ─── Claude call (Haiku, single attempt + fallback chain) ───────────

_SYSTEM_PROMPT = """You are DC Hub's HOURLY operations brain.

Layers 1-6 already handle weekly strategy + tactical PRs. You are the
fast-pulse loop: given a tight 1-hour ops snapshot, surface signal that
the operator needs to know RIGHT NOW.

Output a SINGLE JSON object — no markdown fences, no prose, no commentary
before or after — with EXACTLY this shape:

{
  "anomaly": "<one short sentence: the most concerning hour-over-hour change, or 'none' if all green>",
  "anomaly_severity": "low|medium|high|critical|none",
  "micro_action": "<one concrete action the brain can schedule (e.g. 'boost northern-virginia market brief', 'expand get_grid_intel description', 'kill stuck issue brain_findings#12345'), or 'none'>",
  "watch_metric": "<one metric to keep an eye on next hour (e.g. 'paywall→signal conv rate', 'sentinel unhealthy_count', 'media spike rate')>",
  "confidence": <number between 0.0 and 1.0>,
  "context_summary": "<one short sentence describing the most relevant data point you saw>"
}

RULES
─────
1. anomaly is grounded in the snapshot. If nothing has changed materially
   from the prior hour, output anomaly='none' and severity='none' — the
   loop is allowed to be quiet. Do not invent panic.
2. confidence reflects how strong the signal is in the snapshot.
   confidence<=0.3 means "I'm guessing" — use it when the snapshot is
   thin.
3. micro_action must be CONCRETE: name a market slug, a tool, an issue.
   No "look into the funnel" handwaves.
4. Keep every string under 200 characters. This is operations chatter,
   not strategy.
5. Reply with ONLY the JSON object."""


def _call_claude(prompt: str) -> Optional[dict]:
    """Single Haiku call with fallback chain. Returns parsed JSON dict
    + _model_used + _input_tokens_est + _output_tokens_est, or None on
    total failure."""
    if not _ANTHROPIC_KEY:
        logger.warning("brain_micro_cycle: ANTHROPIC_API_KEY unset")
        return None

    try:
        from routes.brain_models import brain_model_for, resolve_chain
        from utils.anthropic_helper import anthropic_messages_url
    except Exception as e:
        logger.warning("brain_micro_cycle: model registry import failed: %s",
                       e)
        return None

    # Voice tier = Haiku 4.5 = cheapest. Resolve chain still degrades
    # if Haiku is briefly unreachable, so the loop doesn't zero out on
    # transient 5xx upstream.
    chain = resolve_chain(brain_model_for("voice"))
    last_err = None
    for model in chain:
        try:
            import requests
            r = requests.post(
                anthropic_messages_url(),
                headers={
                    "x-api-key":         _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "User-Agent":        "dchub-brain-micro/1.0",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 400,
                    "system":     _SYSTEM_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if r.status_code == 404:
                last_err = f"{model}:404"
                continue
            if r.status_code != 200:
                last_err = f"{model}:{r.status_code}:{r.text[:160]}"
                logger.warning("brain_micro_cycle: %s", last_err)
                continue
            body = r.json() or {}
            text = "".join(
                b.get("text", "")
                for b in (body.get("content") or [])
                if b.get("type") == "text"
            ).strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1] if "```" in text else text
                if text.startswith("json"):
                    text = text[4:].lstrip("\n")
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
            try:
                parsed = json.loads(text)
                parsed["_model_used"] = model
                parsed["_input_tokens_est"] = len(prompt) // 4
                parsed["_output_tokens_est"] = len(text) // 4
                return parsed
            except Exception as je:
                last_err = f"parse:{je}"
                logger.warning(
                    "brain_micro_cycle: JSON parse failed (%s); raw "
                    "first 200 chars: %s", je, text[:200])
                continue
        except Exception as e:
            last_err = f"{model}:exc:{e}"
            logger.warning("brain_micro_cycle: %s exception: %s", model, e)
            continue
    logger.error("brain_micro_cycle: all models failed; last_err=%s",
                 last_err)
    return None


def _estimate_cost_cents(in_tok: int, out_tok: int, model: str) -> float:
    """Rough Anthropic pricing (2026-06) — Haiku 4.5 = $0.80/M in,
    $4.0/M out. Returns FRACTIONAL cents (Haiku per-call is ~0.28¢ which
    rounds to 0 at integer precision; the table stores NUMERIC(10,4) so
    we can sum them honestly)."""
    rates = {
        "claude-haiku-4-5":  (0.80, 4.0),
        "claude-sonnet-4-5": (3.0, 15.0),
        "claude-opus-4-8":   (15.0, 75.0),
        "claude-opus-4-7":   (15.0, 75.0),
        "claude-opus-4-5":   (15.0, 75.0),
    }
    r_in, r_out = rates.get(model, (0.80, 4.0))
    usd = in_tok / 1e6 * r_in + out_tok / 1e6 * r_out
    return max(0.0, round(usd * 100, 4))


# ─── Persistence ────────────────────────────────────────────────────

def _persist_decision(ctx: dict, payload: Optional[dict],
                      hash_: str, cost_cents: float,
                      skipped_reason: Optional[str]) -> Optional[int]:
    c = _get_db()
    if c is None:
        return None
    payload = payload or {}
    anomaly = (payload.get("anomaly") or "")[:500]
    severity = (payload.get("anomaly_severity") or "none")[:32]
    action = (payload.get("micro_action") or "")[:500]
    watch = (payload.get("watch_metric") or "")[:200]
    confidence = payload.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except Exception:
        confidence = None
    summary = (payload.get("context_summary") or "")[:600]
    model = (payload.get("_model_used") or "")[:60]
    raw = json.dumps(payload, default=str)[:6000]
    today = _today_utc()
    hour = _hour_utc()
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_micro_decisions
                  (ran_at, hour_utc, date_utc, model, anomaly,
                   anomaly_severity, micro_action, watch_metric,
                   confidence, context_summary, context_hash,
                   claude_cost_cents, skipped_reason, raw_payload)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb)
                ON CONFLICT (date_utc, hour_utc) DO UPDATE SET
                  anomaly = EXCLUDED.anomaly,
                  anomaly_severity = EXCLUDED.anomaly_severity,
                  micro_action = EXCLUDED.micro_action,
                  watch_metric = EXCLUDED.watch_metric,
                  confidence = EXCLUDED.confidence,
                  context_summary = EXCLUDED.context_summary,
                  context_hash = EXCLUDED.context_hash,
                  claude_cost_cents = brain_micro_decisions.claude_cost_cents
                                      + EXCLUDED.claude_cost_cents,
                  skipped_reason = EXCLUDED.skipped_reason,
                  raw_payload = EXCLUDED.raw_payload,
                  ran_at = NOW()
                RETURNING id
            """, (hour, today, model, anomaly, severity, action, watch,
                  confidence, summary, hash_, float(cost_cents),
                  skipped_reason, raw))
            row = cur.fetchone()
        try:
            c.commit()
        except Exception:
            pass
        return int(row[0]) if row else None
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("brain_micro_cycle: persist failed: %s", e)
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass


def _file_anomaly_finding(payload: dict) -> None:
    """When a real anomaly fires, write it to brain_findings so the
    L6 strategic synthesis sees it. Severity 'none' is skipped so the
    table doesn't fill with no-ops."""
    severity = (payload.get("anomaly_severity") or "").lower()
    if severity in ("none", "", "low"):
        return
    anomaly = (payload.get("anomaly") or "").strip()
    if not anomaly or anomaly.lower() == "none":
        return
    c = _get_db()
    if c is None:
        return
    try:
        from routes.brain_findings_writer import upsert_brain_finding
        with c.cursor() as cur:
            upsert_brain_finding(
                cur,
                issue=f"micro_anomaly_{severity}",
                url="/api/v1/admin/brain/micro-cycle/recent",
                count=1,
                detail=anomaly[:1000],
                detector="brain_micro_cycle",
                status="open",
            )
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        logger.warning(
            "brain_micro_cycle: file_anomaly_finding skipped: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Top-level runner ───────────────────────────────────────────────

def run_micro_cycle(force: bool = False) -> dict:
    """End-to-end hourly run. Returns a summary dict — never raises.

    Order: kill-switch → idempotency (same hour) → budget cap → context
    gather → dedup → Claude call → persist + finding write + budget bump.
    """
    started = _dt.datetime.now(_dt.timezone.utc)
    if _kill_switch_on():
        return {"ok": False, "reason": "kill_switch_on",
                "env": "BRAIN_MICRO_CYCLE_DISABLE=1"}

    # Idempotency: skip if we already wrote a row for this hour. The
    # background-thread caller checks the same flag before firing, but
    # an admin "run-now" can bypass via force=True.
    if not force and _already_ran_this_hour():
        return {"ok": True, "skipped": "already_ran_this_hour"}

    # Budget cap.
    if _budget_exhausted():
        _record_spend(0.0, skipped=True)
        return {"ok": True, "skipped": "daily_budget_exhausted",
                "budget_usd": _daily_budget_usd(),
                "state": _today_budget_state()}

    # Build context.
    ctx = gather_micro_context()
    ctx_str = json.dumps(ctx, default=str)[:_CTX_HARD_CAP_CHARS]
    ctx_hash = _context_hash(ctx)

    # Dedup: if the slimmed context hasn't changed, skip the Claude call.
    if not _dedup_disabled() and not force:
        prev_hash = _last_context_hash()
        if prev_hash and prev_hash == ctx_hash:
            _record_spend(0.0, skipped=True)
            return {"ok": True, "skipped": "context_unchanged",
                    "context_hash": ctx_hash}

    if _dry_run():
        return {"ok": True, "dry_run": True, "context_hash": ctx_hash,
                "context_chars": len(ctx_str), "context": ctx}

    # Claude call.
    prompt = ("HOURLY OPS SNAPSHOT (1h drift focus):\n" + ctx_str +
              "\n\n──── End snapshot. Reply with the JSON only. ────")
    t0 = time.time()
    payload = _call_claude(prompt)
    claude_ms = int((time.time() - t0) * 1000)
    if not payload:
        _persist_decision(ctx, None, ctx_hash, 0,
                          skipped_reason="claude_failed")
        _record_spend(0.0, skipped=True)
        return {"ok": False, "reason": "claude_call_failed",
                "context_hash": ctx_hash, "claude_ms": claude_ms}

    in_tok = int(payload.get("_input_tokens_est") or len(ctx_str) // 4)
    out_tok = int(payload.get("_output_tokens_est") or 0)
    cost_cents = _estimate_cost_cents(in_tok, out_tok,
                                      payload.get("_model_used") or
                                      "claude-haiku-4-5")
    dec_id = _persist_decision(ctx, payload, ctx_hash, cost_cents, None)
    _record_spend(float(cost_cents), skipped=False)
    _file_anomaly_finding(payload)

    finished = _dt.datetime.now(_dt.timezone.utc)
    return {
        "ok": True,
        "decision_id": dec_id,
        "started":   started.isoformat(),
        "finished":  finished.isoformat(),
        "duration_ms": int((finished - started).total_seconds() * 1000),
        "claude_ms": claude_ms,
        "model_used": payload.get("_model_used"),
        "anomaly":    payload.get("anomaly"),
        "anomaly_severity": payload.get("anomaly_severity"),
        "micro_action":     payload.get("micro_action"),
        "watch_metric":     payload.get("watch_metric"),
        "confidence":       payload.get("confidence"),
        "context_summary":  payload.get("context_summary"),
        "context_hash":     ctx_hash,
        "cost_cents":       cost_cents,
        "input_tokens_est": in_tok,
        "output_tokens_est": out_tok,
        "budget_state":     _today_budget_state(),
    }


def _already_ran_this_hour() -> bool:
    c = _get_db()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM brain_micro_decisions
                 WHERE date_utc = CURRENT_DATE
                   AND hour_utc = %s
                 LIMIT 1
            """, (_hour_utc(),))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Background scheduler ──────────────────────────────────────────

_micro_thread: Optional[threading.Thread] = None
_micro_stop = threading.Event()


def _micro_loop() -> None:
    """Wakes every 5 min. If the current UTC hour hasn't fired yet, runs
    one cycle. Belt-and-suspenders against the harness-level idempotency
    check inside run_micro_cycle()."""
    logger.info("brain_micro_cycle: background loop started")
    while not _micro_stop.is_set():
        try:
            if not _kill_switch_on() and not _already_ran_this_hour():
                result = run_micro_cycle(force=False)
                logger.info(
                    "brain_micro_cycle: hourly fire — anomaly=%s "
                    "severity=%s action=%s cost_cents=%s skipped=%s",
                    (result.get("anomaly") or "")[:80],
                    result.get("anomaly_severity"),
                    (result.get("micro_action") or "")[:80],
                    result.get("cost_cents"),
                    result.get("skipped"))
        except Exception as e:
            logger.error("brain_micro_cycle: loop error: %s", e,
                         exc_info=True)
        # 5-min poll window. Worst case missed hour: we fire 5 min late.
        _micro_stop.wait(300)
    logger.info("brain_micro_cycle: background loop stopped")


def start_micro_loop() -> bool:
    """Daemon-thread bootstrap, called from main.py once at startup.
    Single-instance guarded by an fcntl lock on /tmp so the multi-worker
    gunicorn deploy only runs one loop (mirrors crawler_scheduler)."""
    global _micro_thread
    if _kill_switch_on():
        logger.info(
            "brain_micro_cycle: NOT starting (BRAIN_MICRO_CYCLE_DISABLE=1)")
        return False
    if os.environ.get("DISABLE_ALL_CRAWLERS", "").lower() in (
            "1", "true", "yes"):
        logger.info(
            "brain_micro_cycle: NOT starting (DISABLE_ALL_CRAWLERS set)")
        return False

    _lock_path = "/tmp/.brain_micro_cycle.lock"
    try:
        import fcntl
        _lock_fd = open(_lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        # Intentionally keep _lock_fd alive — closing releases the lock.
        # Stash on the function to outlive the call.
        start_micro_loop._lock_fd = _lock_fd  # type: ignore[attr-defined]
    except (IOError, OSError):
        logger.info(
            "brain_micro_cycle: SKIPPED (another worker holds the lock)")
        return False

    if _micro_thread and _micro_thread.is_alive():
        logger.info("brain_micro_cycle: loop already running")
        return True

    _micro_stop.clear()
    _micro_thread = threading.Thread(
        target=_micro_loop, daemon=True, name="brain-micro-cycle")
    _micro_thread.start()
    logger.info("brain_micro_cycle: hourly loop running (cap $%s/day)",
                _daily_budget_usd())
    return True


def stop_micro_loop() -> None:
    _micro_stop.set()
    if _micro_thread:
        _micro_thread.join(timeout=10)


# ─── HTTP routes ────────────────────────────────────────────────────

@brain_micro_bp.route(
    "/api/v1/admin/brain/micro-cycle/run-now", methods=["POST", "GET"])
def micro_run_now():
    """Admin-gated synchronous trigger. Useful for verification + ad-hoc
    debugging. Honors kill switch + budget + dedup unless force=1."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    force = _truthy(request.args.get("force") or request.headers.get(
        "X-Force"))
    out = run_micro_cycle(force=force)
    return jsonify(out), 200


@brain_micro_bp.route(
    "/api/v1/admin/brain/micro-cycle/recent", methods=["GET"])
def micro_recent():
    """Last 24h of decisions + today's budget state. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _get_db()
    rows = []
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, ran_at, hour_utc, date_utc, model, anomaly,
                           anomaly_severity, micro_action, watch_metric,
                           confidence, context_summary, context_hash,
                           claude_cost_cents, skipped_reason
                      FROM brain_micro_decisions
                     WHERE ran_at > NOW() - INTERVAL '24 hours'
                     ORDER BY ran_at DESC
                     LIMIT 48
                """)
                for r in cur.fetchall() or []:
                    rows.append({
                        "id":               r[0],
                        "ran_at":           r[1].isoformat() if r[1] else None,
                        "hour_utc":         r[2],
                        "date_utc":         str(r[3]) if r[3] else None,
                        "model":            r[4],
                        "anomaly":          r[5],
                        "anomaly_severity": r[6],
                        "micro_action":     r[7],
                        "watch_metric":     r[8],
                        "confidence":       (float(r[9])
                                              if r[9] is not None else None),
                        "context_summary":  r[10],
                        "context_hash":     r[11],
                        "cost_cents":       float(r[12] or 0),
                        "skipped_reason":   r[13],
                    })
        except Exception as e:
            logger.warning(
                "brain_micro_cycle: micro_recent read failed: %s", e)
        finally:
            try:
                c.close()
            except Exception:
                pass
    return jsonify(
        ok=True,
        as_of=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        budget_state=_today_budget_state(),
        budget_cap_usd=_daily_budget_usd(),
        kill_switch=_kill_switch_on(),
        dry_run=_dry_run(),
        dedup_disabled=_dedup_disabled(),
        decisions=rows,
    ), 200


@brain_micro_bp.route(
    "/api/v1/admin/brain/micro-cycle/preview-context", methods=["GET"])
def micro_preview_context():
    """Show the slimmed context the loop would send Claude RIGHT NOW.
    No model call. Useful for tuning the prompt budget."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    ctx = gather_micro_context()
    ctx_str = json.dumps(ctx, default=str)[:_CTX_HARD_CAP_CHARS]
    return jsonify(
        ok=True,
        context=ctx,
        context_chars=len(ctx_str),
        context_hash=_context_hash(ctx),
        last_context_hash=_last_context_hash(),
        hard_cap=_CTX_HARD_CAP_CHARS,
    ), 200


@brain_micro_bp.route(
    "/api/v1/admin/brain/micro-cycle/status", methods=["GET"])
def micro_status():
    """Public-ish: confirm the loop is alive + show today's spend.
    Admin-gated to avoid leaking signal."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    alive = bool(_micro_thread and _micro_thread.is_alive())
    return jsonify(
        ok=True,
        loop_alive=alive,
        kill_switch=_kill_switch_on(),
        dry_run=_dry_run(),
        dedup_disabled=_dedup_disabled(),
        budget_cap_usd=_daily_budget_usd(),
        budget_state=_today_budget_state(),
        current_hour_utc=_hour_utc(),
        already_ran_this_hour=_already_ran_this_hour(),
    ), 200
