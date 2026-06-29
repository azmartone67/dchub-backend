"""brain_reasoning_lane.py — FEATURE #6: the LLM-REASONING LANE.

WHY THIS EXISTS (2026-06-28): brain_action_queue.py ranks open findings, and
brain_work_selector.leverage_score scores them, but NOTHING consumes that
ranked work and turns a finding into a CONCRETE, TYPED next action. This is
that first consumer.

WHAT IT DOES (per tick, when armed):
  1. DRAIN the top-K open brain_findings, ranked by
     brain_work_selector.leverage_score (highest leverage first).
  2. Skip findings that already have a fresh reasoning candidate (dedup via
     UNIQUE(finding_issue, finding_url)) so a tick doesn't re-reason the same
     thing and burn API budget.
  3. For ONE drained finding, call the EXISTING investigator LLM helper
     (brain_investigator._call_model / _parse_json) with a STRUCTURED prompt
     that demands a SINGLE concrete action + a TYPE:
        endpoint  → a one-click thing the human can hit (goes to digest)
        code      → a code change → route to an L22 DRAFT-PR (status pr_drafted)
        not_actionable / human → require a human
  4. Write a TYPED candidate row to brain_reasoning_candidates with the
     routing decision. PROPOSE-ONLY: it NEVER opens a PR, never executes,
     never merges. The route is RECORDED, not taken.

SAFETY / INVARIANTS:
  · DARK BY DEFAULT behind BRAIN_REASONING_LANE_ENABLED (default OFF). With the
    flag off, drain_reasoning_lane() is a no-op that returns immediately.
  · FAIL-SAFE: every path is wrapped; ANY exception degrades to "did nothing"
    and never raises into a tick.
  · No auto-merge, no auto-exec, no L8 shell. Code candidates are marked
    'pr_drafted_pending' — they record the INTENT to draft a PR; the actual
    draft-PR is left to the existing L22 draft path / a human, not fired here.
  · Below a confidence floor (BRAIN_REASONING_CONF_FLOOR, default 0.45) the
    candidate is typed 'require_human' regardless of the model's suggestion.

The READ-side endpoint + the candidate table are additive and safe to ship
live; only drain_reasoning_lane() touches the model, and only when armed.

Endpoints (admin-gated):
  GET  /api/v1/brain/reasoning-lane/candidates?limit=40   (read typed candidates)
  POST /api/v1/brain/reasoning-lane/drain?k=5             (manual one-shot drain)
"""
import datetime
import json
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_reasoning_lane_bp = Blueprint("brain_reasoning_lane", __name__)


# ── env / flags ──────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """DARK by default. The lane spends API budget + writes routing decisions,
    so it stays off until an operator flips BRAIN_REASONING_LANE_ENABLED."""
    return _truthy(os.environ.get("BRAIN_REASONING_LANE_ENABLED"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except Exception:
        return default


# How many findings to score+consider per drain; how many to actually reason
# over (LLM calls cost budget — keep this small).
DRAIN_TOPK = _env_int("BRAIN_REASONING_LANE_TOPK", 5)
REASON_PER_TICK = _env_int("BRAIN_REASONING_LANE_PER_TICK", 1)
CONF_FLOOR = _env_float("BRAIN_REASONING_CONF_FLOOR", 0.45)


# ── Admin gate (reuse the investigator's, single source of truth) ────
def _admin_ok() -> bool:
    try:
        from routes.brain_investigator import _admin_ok as _inv_admin_ok
        return bool(_inv_admin_ok())
    except Exception:
        keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(_n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys


# ── DB (direct psycopg2; mirrors brain_investigator._conn) ───────────
def _conn():
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_reasoning_lane: _conn failed: %s", e)
    return None


def init_reasoning_lane_schema() -> None:
    """Bootstrap brain_reasoning_candidates via DIRECT psycopg2 (safe_db SKIPs
    DDL under SKIP_DDL=1). Idempotent; never raises.

    candidate_type: one of endpoint | code | require_human | not_actionable
    routing:        human_digest | pr_drafted_pending | require_human
    status:         proposed (always — propose-only; nothing is executed here)
    UNIQUE(finding_issue, finding_url) so a finding is reasoned over ONCE
    (a re-run UPSERTs the freshest reasoning rather than piling dup rows)."""
    conn = _conn()
    if conn is None:
        logger.warning("brain_reasoning_lane: no DB; skipping schema init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_reasoning_candidates (
                        id             BIGSERIAL PRIMARY KEY,
                        finding_issue  TEXT NOT NULL,
                        finding_url    TEXT NOT NULL DEFAULT '',
                        leverage       DOUBLE PRECISION,
                        candidate_type TEXT NOT NULL,
                        routing        TEXT NOT NULL,
                        action         TEXT,
                        realizes       TEXT,
                        confidence     DOUBLE PRECISION,
                        rationale      TEXT,
                        result_json    JSONB,
                        status         TEXT NOT NULL DEFAULT 'proposed',
                        model          TEXT,
                        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (finding_issue, finding_url)
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_brain_reasoning_candidates_created "
                    "ON brain_reasoning_candidates (created_at DESC)")
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        logger.info("brain_reasoning_lane: schema ready")
    finally:
        try: conn.close()
        except Exception: pass


# ── Structured reasoning prompt ──────────────────────────────────────
_LANE_SYSTEM = (
    "You are the DC Hub Brain Reasoning Lane. You are given ONE open finding "
    "from the brain's worklist (an issue the platform has detected). Your job "
    "is to decide the SINGLE most concrete next action and HOW it is realized. "
    "You PROPOSE only — you never act, never merge, never run anything.\n\n"
    "Output STRICTLY a JSON object, no prose outside it:\n"
    "  {\n"
    '    "type": "endpoint" | "code" | "not_actionable",\n'
    '    "action": "<the single concrete action, one sentence>",\n'
    '    "realizes": "<if type=endpoint: the exact HTTP method+path that '
    'realizes it, e.g. POST /api/v1/brain/x; if type=code: the file/function '
    "that would change and a one-line description of the diff; if "
    'not_actionable: why>",\n'
    '    "confidence": <0.0-1.0: how sure you are this action is correct AND '
    "concrete>,\n"
    '    "rationale": "<2-3 sentences: why this is the right next action>"\n'
    "  }\n\n"
    "RULES:\n"
    "  - Exactly ONE action. If several are needed, pick the highest-leverage "
    "first step.\n"
    "  - type=endpoint ONLY when an existing or obvious API call resolves it "
    "(a human can one-click it). type=code when a source change is required. "
    "type=not_actionable when it is noise, a duplicate, or needs a human "
    "decision before anything can be done.\n"
    "  - NEVER invent metrics. If you are unsure, lower confidence; do not "
    "fabricate certainty."
)


def _structured_action(finding: dict) -> dict:
    """Call the EXISTING investigator model helper with the structured lane
    prompt for ONE finding. Returns a normalized dict. NEVER raises."""
    out = {
        "type": None, "action": None, "realizes": None,
        "confidence": 0.0, "rationale": None, "model": None, "error": None,
    }
    try:
        from routes.brain_investigator import _call_model, _parse_json
    except Exception as e:
        out["error"] = f"investigator_import_failed:{type(e).__name__}"
        return out
    issue = (finding.get("issue") or "").strip()
    url = (finding.get("url") or "").strip()
    detail = (finding.get("detail") or "").strip()
    detector = (finding.get("detector") or finding.get("source") or "").strip()
    seen = finding.get("seen_count")
    prompt = (
        f"FINDING\n"
        f"  issue:    {issue}\n"
        f"  url:      {url or '(none)'}\n"
        f"  detail:   {detail or '(none)'}\n"
        f"  detector: {detector or '(unknown)'}\n"
        f"  seen_count: {seen if seen is not None else '(unknown)'}\n\n"
        f"Decide the single concrete next action and how it is realized."
    )
    try:
        text, err, model = _call_model(
            _LANE_SYSTEM, prompt, tier="reasoning", max_tokens=700)
    except Exception as e:
        out["error"] = f"call_fail:{type(e).__name__}"
        return out
    out["model"] = model
    if err:
        out["error"] = err
        return out
    parsed = None
    try:
        parsed = _parse_json(text)
    except Exception:
        parsed = None
    if not parsed:
        out["error"] = "unparsed_model_output"
        return out
    t = str(parsed.get("type") or "").strip().lower()
    if t not in ("endpoint", "code", "not_actionable"):
        t = "not_actionable"
    out["type"] = t
    out["action"] = (str(parsed.get("action") or "").strip() or None)
    out["realizes"] = (str(parsed.get("realizes") or "").strip() or None)
    out["rationale"] = (str(parsed.get("rationale") or "").strip() or None)
    try:
        c = float(parsed.get("confidence"))
    except Exception:
        c = 0.0
    out["confidence"] = max(0.0, min(1.0, c))
    return out


def _route_for(candidate_type: str, confidence: float) -> tuple[str, str]:
    """Map (type, confidence) -> (final_candidate_type, routing). Below the
    confidence floor, EVERYTHING degrades to require_human. PROPOSE-ONLY:
    'code' records intent to draft a PR (pr_drafted_pending); it does NOT
    open one here. No auto-merge, no execution."""
    if confidence < CONF_FLOOR:
        return "require_human", "require_human"
    if candidate_type == "endpoint":
        return "endpoint", "human_digest"
    if candidate_type == "code":
        return "code", "pr_drafted_pending"
    # not_actionable (or anything unexpected)
    return "not_actionable", "require_human"


def _already_reasoned(conn, issue: str, url: str) -> bool:
    """True if there's already a fresh (<7d) candidate for this finding, so a
    drain doesn't re-spend budget on the same issue. Fail-open to False."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM brain_reasoning_candidates "
                "WHERE finding_issue = %s AND finding_url = %s "
                "AND created_at > NOW() - INTERVAL '7 days' LIMIT 1",
                (issue, url or ""))
            return cur.fetchone() is not None
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return False


def _write_candidate(conn, finding: dict, sa: dict,
                     final_type: str, routing: str) -> bool:
    """UPSERT a typed candidate. status is ALWAYS 'proposed' — propose-only.
    Never raises; returns True on a committed write."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_reasoning_candidates "
                "(finding_issue, finding_url, leverage, candidate_type, routing, "
                " action, realizes, confidence, rationale, result_json, status, "
                " model, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed',%s,NOW(),NOW()) "
                "ON CONFLICT (finding_issue, finding_url) DO UPDATE SET "
                "  leverage=EXCLUDED.leverage, candidate_type=EXCLUDED.candidate_type, "
                "  routing=EXCLUDED.routing, action=EXCLUDED.action, "
                "  realizes=EXCLUDED.realizes, confidence=EXCLUDED.confidence, "
                "  rationale=EXCLUDED.rationale, result_json=EXCLUDED.result_json, "
                "  model=EXCLUDED.model, updated_at=NOW()",
                (
                    (finding.get("issue") or "").strip(),
                    (finding.get("url") or "").strip(),
                    finding.get("_leverage"),
                    final_type,
                    routing,
                    sa.get("action"),
                    sa.get("realizes"),
                    sa.get("confidence"),
                    sa.get("rationale"),
                    json.dumps({"structured_action": sa,
                                "finding_detail": finding.get("detail")})[:60000],
                    sa.get("model"),
                ))
            conn.commit()
        return True
    except Exception as e:
        logger.warning("brain_reasoning_lane: write_candidate failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return False


def _rank_open_findings(conn, topk: int) -> list[dict]:
    """Pull recent open findings and rank them by leverage_score (highest
    first). Fail-soft: any error returns []."""
    rows: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT issue, url, detail, seen_count, first_seen, last_seen "
                "FROM brain_findings WHERE status = 'open' "
                "ORDER BY last_seen DESC NULLS LAST LIMIT 400")
            for r in cur.fetchall():
                rows.append({
                    "issue": r[0], "url": r[1], "detail": r[2],
                    "seen_count": r[3], "first_seen": r[4], "last_seen": r[5],
                    "occurrence": r[3],
                })
    except Exception as e:
        logger.warning("brain_reasoning_lane: rank query failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return []
    # Rank by leverage (rank_work annotates _leverage; never drops, never raises)
    try:
        from routes.brain_work_selector import rank_work
        ranked = rank_work(rows)
    except Exception:
        ranked = rows
    return ranked[:max(0, topk)]


# ── The drainer (master-tick entry point) ────────────────────────────
def drain_reasoning_lane(*, topk: int | None = None,
                         per_tick: int | None = None) -> dict:
    """Drain top-K leverage-ranked open findings, reason over up to `per_tick`
    of them, and write TYPED candidates. PROPOSE-ONLY. DARK behind the flag.
    NEVER raises — any failure returns {ok:False,...} so a tick is unharmed."""
    summary = {"ok": True, "enabled": _enabled(), "considered": 0,
               "reasoned": 0, "written": 0, "skipped_existing": 0,
               "by_type": {}, "errors": []}
    if not _enabled():
        summary["ok"] = True
        summary["note"] = "dark (BRAIN_REASONING_LANE_ENABLED off)"
        return summary
    k = topk if topk is not None else DRAIN_TOPK
    n = per_tick if per_tick is not None else REASON_PER_TICK
    conn = _conn()
    if conn is None:
        summary["ok"] = False
        summary["errors"].append("no_db")
        return summary
    try:
        try:
            init_reasoning_lane_schema()
        except Exception:
            pass
        candidates = _rank_open_findings(conn, k)
        summary["considered"] = len(candidates)
        done = 0
        for f in candidates:
            if done >= max(0, n):
                break
            issue = (f.get("issue") or "").strip()
            url = (f.get("url") or "").strip()
            if not issue:
                continue
            try:
                if _already_reasoned(conn, issue, url):
                    summary["skipped_existing"] += 1
                    continue
                sa = _structured_action(f)
                summary["reasoned"] += 1
                done += 1
                if sa.get("error") and not sa.get("type"):
                    summary["errors"].append(sa["error"])
                    continue
                final_type, routing = _route_for(
                    sa.get("type") or "not_actionable",
                    float(sa.get("confidence") or 0.0))
                if _write_candidate(conn, f, sa, final_type, routing):
                    summary["written"] += 1
                    summary["by_type"][final_type] = \
                        summary["by_type"].get(final_type, 0) + 1
            except Exception as e:
                summary["errors"].append(f"{type(e).__name__}:{str(e)[:80]}")
                continue
    except Exception as e:
        summary["ok"] = False
        summary["errors"].append(f"drain_fail:{str(e)[:120]}")
    finally:
        try: conn.close()
        except Exception: pass
    return summary


# ── Endpoints ────────────────────────────────────────────────────────
@brain_reasoning_lane_bp.route(
    "/api/v1/brain/reasoning-lane/candidates", methods=["GET"])
def list_candidates():
    """Read the typed candidates (newest first). Admin-gated (proposals can
    name internal endpoints/files)."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        limit = max(1, min(int(request.args.get("limit", 40)), 200))
    except Exception:
        limit = 40
    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no db"}), 500
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, finding_issue, finding_url, leverage, candidate_type, "
                "       routing, action, realizes, confidence, rationale, status, "
                "       model, created_at "
                "FROM brain_reasoning_candidates "
                "ORDER BY created_at DESC LIMIT %s", (limit,))
            for r in cur.fetchall():
                rows.append({
                    "id": r[0], "finding_issue": r[1], "finding_url": r[2],
                    "leverage": r[3], "candidate_type": r[4], "routing": r[5],
                    "action": r[6], "realizes": r[7], "confidence": r[8],
                    "rationale": r[9], "status": r[10], "model": r[11],
                    "created_at": (r[12].isoformat() if isinstance(
                        r[12], datetime.datetime) else str(r[12])),
                })
    except Exception as e:
        logger.warning("brain_reasoning_lane: list failed: %s", e)
        return jsonify({"ok": False, "error": str(e)[:160]}), 500
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify({
        "ok": True, "count": len(rows), "candidates": rows,
        "note": ("Typed, propose-only candidates. routing=human_digest "
                 "(endpoint, one-click), pr_drafted_pending (code → L22 "
                 "draft-PR intent, NOT opened here), require_human (below "
                 f"confidence floor {CONF_FLOOR} or not actionable)."),
    }), 200, {"Cache-Control": "no-store, max-age=0",
              "X-DC-Hub-Surface": "brain-reasoning-lane"}


@brain_reasoning_lane_bp.route(
    "/api/v1/brain/reasoning-lane/drain", methods=["POST"])
def manual_drain():
    """Manually trigger ONE drain (still DARK behind the flag). Admin-gated."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        k = int(request.args.get("k", DRAIN_TOPK))
    except Exception:
        k = DRAIN_TOPK
    try:
        n = int(request.args.get("per_tick", REASON_PER_TICK))
    except Exception:
        n = REASON_PER_TICK
    result = drain_reasoning_lane(topk=k, per_tick=n)
    return jsonify(result), 200


def register_brain_reasoning_lane(app) -> None:
    """Idempotent registration helper for main.py. Best-effort schema init."""
    try:
        init_reasoning_lane_schema()
    except Exception as e:
        logger.warning("brain_reasoning_lane: schema init skipped: %s", e)
    try:
        app.register_blueprint(brain_reasoning_lane_bp)
    except Exception as e:
        logger.warning("brain_reasoning_lane already registered: %s", e)
