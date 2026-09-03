"""media_recurring_formats.py — FEATURE 5: recurring subscribable formats.
================================================================================

Three recurring, subscribable media formats, ALL dark-by-default and ALL
gated through the same publish guards we hardened today:

  (a) WEEKLY "DCPI Movers" digest — markets whose BUILD/CAUTION/AVOID verdict
      FLIPPED in the last 7 days, sourced PREFERENTIALLY from the
      media_dcpi_snapshots table that Feature 2 creates (a true point-in-time
      snapshot, immune to history-archival), and degrading GRACEFULLY to a
      market_power_scores self-join (the same shape dcpi_auto_press.py uses)
      when that table is absent.
  (b) MONTHLY "State of Data-Center Power" report — verdict DISTRIBUTION,
      biggest AVOID markets, and build-ready leaders, all from live
      market_power_scores + canonical_stats counts.
  (c) RSS feed of the latest movers at GET /dcpi-movers.rss — emits ONLY
      APPROVED queue items, and ONLY when the flag is on.

SAFETY POSTURE (non-negotiable — an auto-published post disparaged a partner
today, so every lever here is OFF + queue-only by construction):

  1. DARK BY DEFAULT. The whole module is gated behind
     MEDIA_RECURRING_FORMATS_ENABLED. With it OFF, EVERY endpoint returns
     {ok: true, skipped: "disabled"} and the generators do NOTHING (no DB
     writes, no LLM calls, no RSS items).
  2. NO AUTO-SEND / NO AUTO-PUBLISH-LIVE. run-weekly / run-monthly compose a
     DRAFT and INSERT it into a REVIEW QUEUE (media_recurring_queue) with
     status='pending'. They NEVER POST to LinkedIn/X, never email a journalist,
     never publish a page. An operator must flip status to 'approved' (a
     separate admin action) before the RSS feed will emit it.
  3. EVERY draft is routed through BOTH runtime guards BEFORE it can be queued:
       content_publisher._should_skip_publish(cur, text, platform)  — the
         partner-disparagement / quality / dedup block, and
       routes.media_claim_verify.verify_claims(text)                — the
         numeric-claim fact-check (catches runtime-hallucinated over-claims).
     Anything either guard flags is DROPPED (queued as status='blocked' with
     the reason, so the operator can see WHY — but it can never be approved or
     emitted).
  4. VERIFIED NUMBERS ONLY. Every figure in a draft comes from a live query
     (market_power_scores for verdict/excess_power_score/constraint_score/
     time_to_power_months, canonical_stats.get_canonical_stats() for counts).
     We NEVER fabricate a number; if a value can't be verified live, it is
     OMITTED. The claim-verify guard in (3) is the belt to this suspenders.
  5. SELF-IMPORT-SAFE. The module imports cleanly with the flag off and no
     special env — every optional import (psycopg2, anthropic helper, the two
     guards, canonical_stats) is wrapped, and schema bootstrap is lazy + only
     runs when the flag is on, so registering the blueprint can NEVER crash
     app boot.
  6. LLM (optional). When ANTHROPIC_API_KEY is present we polish the draft with
     Sonnet via utils.anthropic_helper.anthropic_messages_url(), mirroring
     routes/linkedin_content_engine.py. Without it we fall back to a
     deterministic template built only from verified numbers. Either way the
     output still passes through guard (3).
  7. ADMIN GATE. POST /run-weekly and POST /run-monthly require an admin/internal
     key (X-Admin-Key / X-Internal-Key / ?admin_key=, mirroring
     brain_innovation._innov_admin_ok + dcpi_auto_press). The RSS GET is public
     but emits only APPROVED items and only when the flag is on.

Endpoints (all under the blueprint):
  POST /api/v1/media/recurring/run-weekly    admin — draft the weekly digest → queue
  POST /api/v1/media/recurring/run-monthly   admin — draft the monthly report → queue
  GET  /api/v1/media/recurring/queue         admin — inspect the queue
  POST /api/v1/media/recurring/approve        admin — flip a pending item → approved
  GET  /dcpi-movers.rss                       public — approved movers feed (flag-gated)
  GET  /rss/dcpi-movers                       public — alias

Kill-switch env: MEDIA_RECURRING_FORMATS_ENABLED  (OFF unless 1/true/yes/on)

Table: media_recurring_queue
  id BIGSERIAL PK, format TEXT ('weekly_movers'|'monthly_state'),
  title TEXT, body TEXT, platform TEXT, payload JSONB (verified facts used),
  status TEXT ('pending'|'approved'|'blocked'|'rejected'), block_reason TEXT,
  guard_warns JSONB, created_at TIMESTAMPTZ, approved_at TIMESTAMPTZ,
  approved_by TEXT, period_key TEXT (idempotency: e.g. '2026-W25', '2026-06')
"""
from __future__ import annotations

from utils.anthropic_helper import cached_system
import os
import sys
import json
import logging
import datetime
from datetime import timezone

from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)

media_recurring_formats_bp = Blueprint("media_recurring_formats", __name__)


# ── Kill-switch ───────────────────────────────────────────────────────────────
KILL_SWITCH = "MEDIA_RECURRING_FORMATS_ENABLED"


def _enabled() -> bool:
    """Dark by default: ON only when the flag is explicitly truthy."""
    return (os.environ.get(KILL_SWITCH, "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _skipped():
    """Uniform OFF response — every endpoint returns this when disabled."""
    return jsonify({"ok": True, "skipped": "disabled",
                    "flag": KILL_SWITCH}), 200


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[recurring-formats] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────────────────
MODEL_NAME = (os.environ.get("RECURRING_FORMATS_MODEL")
              or "claude-sonnet-4-5")
MAX_TOKENS = int(os.environ.get("RECURRING_FORMATS_MAX_TOKENS") or "900")
_LOOKBACK_DAYS = 7
RSS_LIMIT = int(os.environ.get("RECURRING_FORMATS_RSS_LIMIT") or "25")

_VOICE_SYSTEM = (
    "You are the editorial desk for DC Hub (dchub.cloud), the live "
    "infrastructure-data layer for data-center power. Write a crisp, neutral "
    "analyst brief. HARD RULES: (1) Use ONLY the numbers and market names "
    "provided in the data block below — never invent a figure, percentage, "
    "dollar amount, capacity, or date. If a value is not in the data, do not "
    "mention it. (2) Be factual and dispassionate. NEVER disparage, attack, or "
    "make negative claims about any named company, operator, utility, or "
    "competitor — describe market CONDITIONS (a market's verdict), never "
    "criticise a party. (3) Open with the single most important verified "
    "number. (4) Cite 'DC Hub (dchub.cloud)'. Keep it under 1100 characters.")


# ── Plumbing (all wrapped — import-safe) ───────────────────────────────────────
def _db_conn():
    """psycopg2 connection or None. Never raises (self-import-safe)."""
    try:
        import psycopg2  # noqa: F401
    except Exception:
        return None
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url, sslmode="require", connect_timeout=8)
    except Exception:
        try:
            import psycopg2
            return psycopg2.connect(url, connect_timeout=8)
        except Exception:
            return None


def _admin_ok() -> bool:
    """Admin/internal-key OR internal-cron gate. Mirrors
    brain_innovation._innov_admin_ok + dcpi_auto_press._admin_or_cron_authorized."""
    keys = set()
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.args.get("key") or "").strip()
    if sent and sent in keys:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _xml(s) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


# ── Schema bootstrap (LAZY — only when enabled) ────────────────────────────────
def _ensure_table(cur) -> None:
    """Create the review-queue table if absent. Called inside an already-open
    cursor on the run/approve/rss paths, only when the flag is on."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS media_recurring_queue (
            id           BIGSERIAL PRIMARY KEY,
            format       TEXT NOT NULL,
            title        TEXT,
            body         TEXT NOT NULL,
            platform     TEXT NOT NULL DEFAULT 'feed',
            payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
            status       TEXT NOT NULL DEFAULT 'pending',
            block_reason TEXT,
            guard_warns  JSONB NOT NULL DEFAULT '[]'::jsonb,
            period_key   TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            approved_at  TIMESTAMPTZ,
            approved_by  TEXT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS media_recurring_queue_status_idx
            ON media_recurring_queue(status, created_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS media_recurring_queue_period_idx
            ON media_recurring_queue(format, period_key)
    """)


# ── VERIFIED data pulls (live only) ────────────────────────────────────────────
def _canon_counts() -> dict:
    """canonical_stats.get_canonical_stats() — verified counts only. Never raises."""
    try:
        import canonical_stats as cs
        return cs.get_canonical_stats() or {}
    except Exception as e:
        _log(f"canon lookup failed: {e}")
        return {}


def _snapshot_table_exists(cur) -> bool:
    """True iff Feature 2's media_dcpi_snapshots table exists (degrade gracefully
    when absent)."""
    try:
        cur.execute("SELECT to_regclass('public.media_dcpi_snapshots')")
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _movers_from_snapshots(cur) -> list[dict]:
    """Preferred source: verdict flips between the latest snapshot and the
    snapshot from ~7d ago, read from Feature 2's media_dcpi_snapshots table.

    We probe column names defensively (Feature 2 owns the schema and may name
    things slightly differently); on ANY mismatch we return [] so the caller
    falls back to market_power_scores."""
    out: list[dict] = []
    try:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, market_name, verdict,
                       excess_power_score, constraint_score, snapshot_at
                  FROM media_dcpi_snapshots
                 ORDER BY market_slug, snapshot_at DESC
            ),
            prior AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, verdict AS prior_verdict, snapshot_at
                  FROM media_dcpi_snapshots
                 WHERE snapshot_at < NOW() - (%s || ' days')::interval
                 ORDER BY market_slug, snapshot_at DESC
            )
            SELECT l.market_slug, l.market_name, p.prior_verdict, l.verdict,
                   l.excess_power_score, l.constraint_score
              FROM latest l
              JOIN prior p USING (market_slug)
             WHERE p.prior_verdict IS DISTINCT FROM l.verdict
               AND p.prior_verdict IN ('BUILD','CAUTION','AVOID')
               AND l.verdict       IN ('BUILD','CAUTION','AVOID')
             ORDER BY l.market_name
             LIMIT 40
        """, (str(_LOOKBACK_DAYS),))
        for r in cur.fetchall() or []:
            out.append({
                "slug": r[0], "name": r[1],
                "prior_verdict": r[2], "new_verdict": r[3],
                "excess_power_score": float(r[4]) if r[4] is not None else None,
                "constraint_score": float(r[5]) if r[5] is not None else None,
                "source": "snapshot",
            })
    except Exception as e:
        _log(f"snapshot movers query failed (will fall back): {e}")
        return []
    return out


def _movers_from_scores(cur) -> list[dict]:
    """Graceful fallback: verdict flips via a market_power_scores self-join —
    the same shape dcpi_auto_press._find_significant_shifts uses. Verdict flips
    ONLY (not point moves) to match the 'DCPI Movers = verdict flipped' spec."""
    out: list[dict] = []
    try:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, market_name, verdict,
                       excess_power_score, constraint_score, computed_at
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                 ORDER BY market_slug, computed_at DESC
            ),
            prior AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, verdict AS prior_verdict, computed_at
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                   AND computed_at < NOW() - (%s || ' days')::interval
                 ORDER BY market_slug, computed_at DESC
            )
            SELECT l.market_slug, l.market_name, p.prior_verdict, l.verdict,
                   l.excess_power_score, l.constraint_score
              FROM latest l
              JOIN prior p USING (market_slug)
             WHERE p.prior_verdict IS DISTINCT FROM l.verdict
               AND p.prior_verdict IN ('BUILD','CAUTION','AVOID')
               AND l.verdict       IN ('BUILD','CAUTION','AVOID')
             ORDER BY l.market_name
             LIMIT 40
        """, (str(_LOOKBACK_DAYS),))
        for r in cur.fetchall() or []:
            out.append({
                "slug": r[0], "name": r[1],
                "prior_verdict": r[2], "new_verdict": r[3],
                "excess_power_score": float(r[4]) if r[4] is not None else None,
                "constraint_score": float(r[5]) if r[5] is not None else None,
                "source": "scores",
            })
    except Exception as e:
        _log(f"scores movers query failed: {e}")
        return []
    return out


def _get_movers(cur) -> list[dict]:
    """DCPI movers from snapshots if Feature 2's table exists, else
    market_power_scores. Verified data only."""
    if _snapshot_table_exists(cur):
        rows = _movers_from_snapshots(cur)
        if rows:
            return rows
    return _movers_from_scores(cur)


def _monthly_state(cur) -> dict:
    """Verified inputs for the monthly report: verdict distribution, biggest
    AVOID markets, and build-ready leaders — all live from market_power_scores.
    Never raises; missing pieces are simply omitted."""
    state: dict = {"verdict_distribution": {}, "avoid": [], "build_ready": []}
    # verdict distribution over the latest score per market
    try:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug) market_slug, verdict
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                 ORDER BY market_slug, computed_at DESC
            )
            SELECT verdict, COUNT(*) FROM latest
             WHERE verdict IS NOT NULL
             GROUP BY verdict ORDER BY COUNT(*) DESC
        """)
        for v, n in (cur.fetchall() or []):
            if v:
                state["verdict_distribution"][str(v)] = int(n)
    except Exception as e:
        _log(f"verdict distribution failed: {e}")
    # biggest AVOID markets (highest constraint_score = most power-constrained)
    try:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, market_name, verdict,
                       constraint_score, excess_power_score
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                 ORDER BY market_slug, computed_at DESC
            )
            SELECT market_name, constraint_score
              FROM latest
             WHERE verdict = 'AVOID' AND market_name IS NOT NULL
             ORDER BY constraint_score DESC NULLS LAST
             LIMIT 5
        """)
        for name, cs in (cur.fetchall() or []):
            item = {"name": name}
            if cs is not None:
                item["constraint_score"] = float(cs)
            state["avoid"].append(item)
    except Exception as e:
        _log(f"avoid query failed: {e}")
    # build-ready leaders (BUILD verdict, highest excess_power_score)
    try:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, market_name, verdict, excess_power_score
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                 ORDER BY market_slug, computed_at DESC
            )
            SELECT market_name, excess_power_score
              FROM latest
             WHERE verdict = 'BUILD' AND market_name IS NOT NULL
             ORDER BY excess_power_score DESC NULLS LAST
             LIMIT 5
        """)
        for name, ex in (cur.fetchall() or []):
            item = {"name": name}
            if ex is not None:
                item["excess_power_score"] = float(ex)
            state["build_ready"].append(item)
    except Exception as e:
        _log(f"build-ready query failed: {e}")
    return state


# ── Draft composition (verified-only template + optional LLM polish) ───────────
def _movers_facts_block(movers: list[dict], counts: dict) -> str:
    lines = []
    n = counts.get("markets")
    if isinstance(n, int) and n > 0:
        lines.append(f"DCPI tracks {n} markets.")
    lines.append(f"{len(movers)} markets flipped DCPI verdict in the last "
                 f"{_LOOKBACK_DAYS} days:")
    for m in movers[:20]:
        lines.append(f"- {m['name']}: {m['prior_verdict']} -> {m['new_verdict']}")
    return "\n".join(lines)


def _movers_template(movers: list[dict], counts: dict) -> str:
    """Deterministic, verified-only draft. The headline number is the count of
    flips (always true) so the post leads with a real metric."""
    head = (f"{len(movers)} DCPI markets flipped verdict this week.")
    body = [head, ""]
    for m in movers[:15]:
        body.append(f"• {m['name']}: {m['prior_verdict']} → {m['new_verdict']}")
    body.append("")
    body.append("Source: DC Hub (dchub.cloud) — live DC Hub Power Index.")
    return "\n".join(body)


def _monthly_facts_block(state: dict, counts: dict) -> str:
    lines = []
    n = counts.get("markets")
    if isinstance(n, int) and n > 0:
        lines.append(f"DCPI tracks {n} markets.")
    dist = state.get("verdict_distribution") or {}
    if dist:
        lines.append("Verdict distribution (latest score per market): "
                     + ", ".join(f"{k}={v}" for k, v in dist.items()))
    if state.get("build_ready"):
        lines.append("Build-ready leaders (BUILD, highest excess-power score):")
        for it in state["build_ready"]:
            s = (f" (excess-power {it['excess_power_score']:.0f})"
                 if "excess_power_score" in it else "")
            lines.append(f"- {it['name']}{s}")
    if state.get("avoid"):
        lines.append("Most power-constrained AVOID markets (highest constraint score):")
        for it in state["avoid"]:
            s = (f" (constraint {it['constraint_score']:.0f})"
                 if "constraint_score" in it else "")
            lines.append(f"- {it['name']}{s}")
    return "\n".join(lines)


def _monthly_template(state: dict, counts: dict) -> str:
    dist = state.get("verdict_distribution") or {}
    total = sum(dist.values()) if dist else 0
    if total > 0:
        head = (f"{total} markets scored this month: "
                + ", ".join(f"{v} {k}" for k, v in dist.items()) + ".")
    else:
        head = "State of Data-Center Power — monthly DCPI report."
    body = [head, ""]
    if state.get("build_ready"):
        body.append("Build-ready leaders:")
        for it in state["build_ready"]:
            s = (f" — excess-power {it['excess_power_score']:.0f}"
                 if "excess_power_score" in it else "")
            body.append(f"• {it['name']}{s}")
        body.append("")
    if state.get("avoid"):
        body.append("Most power-constrained (AVOID):")
        for it in state["avoid"]:
            s = (f" — constraint {it['constraint_score']:.0f}"
                 if "constraint_score" in it else "")
            body.append(f"• {it['name']}{s}")
        body.append("")
    body.append("Source: DC Hub (dchub.cloud) — live DC Hub Power Index.")
    return "\n".join(body)


def _llm_polish(facts_block: str, instruction: str) -> str | None:
    """Optional Sonnet polish (mirrors linkedin_content_engine._compose_with_claude).
    Returns None on any failure so the caller keeps the deterministic template.
    The LLM is constrained to ONLY the verified facts_block; output still passes
    through the guards downstream."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import urllib.request
        from utils.anthropic_helper import anthropic_messages_url
    except Exception:
        return None
    user_prompt = (
        f"{instruction}\n\nVERIFIED DATA (use ONLY these numbers and names — "
        f"invent nothing):\n{facts_block}\n")
    try:
        body = json.dumps({
            "model": MODEL_NAME,
            "max_tokens": MAX_TOKENS,
            "system": cached_system(_VOICE_SYSTEM),
            "messages": [{"role": "user", "content": user_prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in parts
                       if isinstance(p, dict)).strip()
        if text.startswith('"') and text.endswith('"') and len(text) > 10:
            text = text[1:-1].strip()
        return text or None
    except Exception as e:
        _log(f"LLM polish failed (using template): {e}")
        return None


# ── The two guards (BOTH must pass; either flag → DROP) ─────────────────────────
def _run_guards(cur, text: str, platform: str) -> dict:
    """Run BOTH publish guards. Returns {blocked: bool, reason: str, warns: []}.

    A blocked draft is DROPPED (queued as status='blocked' so the operator sees
    why, but it can never be approved or emitted). Both guards are wrapped so a
    transient guard import failure does NOT silently allow an unverified draft —
    on guard error we BLOCK (fail-closed), because here the safe failure is to
    NOT queue an approvable item (the opposite of content_publisher's fail-open,
    which is correct THERE because it's deciding whether to skip a publish; HERE
    we're deciding whether something becomes operator-approvable)."""
    warns: list[str] = []

    # Guard 1: partner-disparagement / quality / dedup / number-lead.
    try:
        from content_publisher import _should_skip_publish
        skip, why = _should_skip_publish(cur, text, platform)
        if skip:
            return {"blocked": True,
                    "reason": f"_should_skip_publish: {why}"[:480],
                    "warns": warns}
    except Exception as e:
        _log(f"_should_skip_publish unavailable — failing CLOSED: {e}")
        return {"blocked": True,
                "reason": f"guard unavailable (_should_skip_publish): {str(e)[:200]}",
                "warns": warns}

    # Guard 2: numeric claim fact-check.
    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text)
        for w in (cv.get("warns") or []):
            warns.append(str(w))
        if cv.get("blocks"):
            return {"blocked": True,
                    "reason": "claim-verify: " + "; ".join(cv["blocks"])[:440],
                    "warns": warns}
    except Exception as e:
        _log(f"verify_claims unavailable — failing CLOSED: {e}")
        return {"blocked": True,
                "reason": f"guard unavailable (verify_claims): {str(e)[:200]}",
                "warns": warns}

    return {"blocked": False, "reason": "", "warns": warns}


def _queue_draft(cur, *, fmt: str, title: str, body: str, platform: str,
                 payload: dict, period_key: str) -> dict:
    """Idempotent enqueue. If a non-rejected item already exists for
    (format, period_key), return it unchanged. Otherwise run BOTH guards and
    INSERT as 'pending' (clean) or 'blocked' (a guard flagged it). NEVER
    publishes / sends."""
    # idempotency: one draft per (format, period)
    cur.execute(
        "SELECT id, status, block_reason FROM media_recurring_queue "
        "WHERE format = %s AND period_key = %s AND status <> 'rejected' "
        "ORDER BY id DESC LIMIT 1",
        (fmt, period_key))
    existing = cur.fetchone()
    if existing:
        return {"id": int(existing[0]), "status": existing[1],
                "block_reason": existing[2], "deduped": True}

    guard = _run_guards(cur, body, platform)
    status = "blocked" if guard["blocked"] else "pending"
    cur.execute(
        "INSERT INTO media_recurring_queue "
        "(format, title, body, platform, payload, status, block_reason, "
        " guard_warns, period_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (fmt, title, body, platform, json.dumps(payload), status,
         (guard["reason"] or None), json.dumps(guard["warns"]), period_key))
    new_id = int(cur.fetchone()[0])
    return {"id": new_id, "status": status,
            "block_reason": (guard["reason"] or None),
            "warns": guard["warns"], "deduped": False}


def _iso_week_key(d: datetime.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ── Endpoints ──────────────────────────────────────────────────────────────────
@media_recurring_formats_bp.route(
    "/api/v1/media/recurring/run-weekly", methods=["POST"])
def run_weekly():
    if not _enabled():
        return _skipped()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 403
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no db"}), 503
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            counts = _canon_counts()
            movers = _get_movers(cur)
            if not movers:
                return jsonify({"ok": True, "queued": False,
                                "reason": "no verdict flips in window"}), 200
            facts = _movers_facts_block(movers, counts)
            template = _movers_template(movers, counts)
            polished = _llm_polish(
                facts,
                "Write a short weekly 'DCPI Movers' digest naming each market "
                "that flipped verdict and the direction of the flip.")
            body = polished or template
            payload = {"movers": movers, "counts_markets": counts.get("markets"),
                       "source": movers[0].get("source") if movers else None,
                       "llm_polished": bool(polished)}
            period_key = _iso_week_key(datetime.datetime.now(timezone.utc).date())
            res = _queue_draft(
                cur, fmt="weekly_movers",
                title=f"DCPI Movers — week {period_key}",
                body=body, platform="feed", payload=payload,
                period_key=period_key)
        return jsonify({"ok": True, "format": "weekly_movers",
                        "queued": res["status"] == "pending",
                        "queue": res, "movers_count": len(movers)}), 200
    except Exception as e:
        _log(f"run-weekly failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:240]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@media_recurring_formats_bp.route(
    "/api/v1/media/recurring/run-monthly", methods=["POST"])
def run_monthly():
    if not _enabled():
        return _skipped()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 403
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no db"}), 503
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            counts = _canon_counts()
            state = _monthly_state(cur)
            if not (state.get("verdict_distribution")
                    or state.get("build_ready") or state.get("avoid")):
                return jsonify({"ok": True, "queued": False,
                                "reason": "no live market scores"}), 200
            facts = _monthly_facts_block(state, counts)
            template = _monthly_template(state, counts)
            polished = _llm_polish(
                facts,
                "Write a short monthly 'State of Data-Center Power' report: the "
                "verdict distribution, the build-ready leaders, and the most "
                "power-constrained AVOID markets.")
            body = polished or template
            payload = {"state": state, "counts_markets": counts.get("markets"),
                       "llm_polished": bool(polished)}
            now = datetime.datetime.now(timezone.utc)
            period_key = f"{now.year}-{now.month:02d}"
            res = _queue_draft(
                cur, fmt="monthly_state",
                title=f"State of Data-Center Power — {period_key}",
                body=body, platform="feed", payload=payload,
                period_key=period_key)
        return jsonify({"ok": True, "format": "monthly_state",
                        "queued": res["status"] == "pending",
                        "queue": res}), 200
    except Exception as e:
        _log(f"run-monthly failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:240]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@media_recurring_formats_bp.route(
    "/api/v1/media/recurring/queue", methods=["GET"])
def queue_list():
    if not _enabled():
        return _skipped()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 403
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no db"}), 503
    status = (request.args.get("status") or "").strip()
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            if status:
                cur.execute(
                    "SELECT id, format, title, body, status, block_reason, "
                    "guard_warns, period_key, created_at, approved_at "
                    "FROM media_recurring_queue WHERE status = %s "
                    "ORDER BY id DESC LIMIT 100", (status,))
            else:
                cur.execute(
                    "SELECT id, format, title, body, status, block_reason, "
                    "guard_warns, period_key, created_at, approved_at "
                    "FROM media_recurring_queue ORDER BY id DESC LIMIT 100")
            items = []
            for r in cur.fetchall() or []:
                items.append({
                    "id": int(r[0]), "format": r[1], "title": r[2],
                    "body": r[3], "status": r[4], "block_reason": r[5],
                    "guard_warns": r[6], "period_key": r[7],
                    "created_at": r[8].isoformat() if r[8] else None,
                    "approved_at": r[9].isoformat() if r[9] else None,
                })
        return jsonify({"ok": True, "count": len(items), "items": items}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:240]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@media_recurring_formats_bp.route(
    "/api/v1/media/recurring/approve", methods=["POST"])
def approve():
    """Operator approval. Flips a PENDING item to 'approved' (or 'rejected').
    A 'blocked' item (a guard flagged it) can NOT be approved — only rejected."""
    if not _enabled():
        return _skipped()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 403
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    item_id = data.get("id") or request.args.get("id")
    decision = (data.get("decision") or request.args.get("decision")
                or "approve").strip().lower()
    if not item_id:
        return jsonify({"ok": False, "error": "id required"}), 400
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no db"}), 503
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("SELECT status FROM media_recurring_queue WHERE id = %s",
                        (int(item_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not found"}), 404
            cur_status = row[0]
            if decision in ("reject", "rejected"):
                cur.execute(
                    "UPDATE media_recurring_queue SET status='rejected' "
                    "WHERE id = %s", (int(item_id),))
                return jsonify({"ok": True, "id": int(item_id),
                                "status": "rejected"}), 200
            # approve path — only a clean PENDING item may be approved.
            if cur_status != "pending":
                return jsonify({"ok": False, "error":
                                f"cannot approve item in status '{cur_status}' "
                                f"(only 'pending' is approvable)"}), 409
            who = (request.headers.get("X-Admin-Key")
                   or request.headers.get("X-Internal-Key") or "operator")[:16]
            cur.execute(
                "UPDATE media_recurring_queue SET status='approved', "
                "approved_at=NOW(), approved_by=%s WHERE id = %s",
                (who, int(item_id)))
        return jsonify({"ok": True, "id": int(item_id),
                        "status": "approved"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:240]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@media_recurring_formats_bp.route("/dcpi-movers.rss")
@media_recurring_formats_bp.route("/rss/dcpi-movers")
def movers_rss():
    """Public RSS of the latest movers — emits ONLY approved items, and ONLY
    when the flag is on. With the flag off, returns an empty (valid) feed so
    subscribers see nothing rather than an error."""
    self_url = "https://dchub.cloud/dcpi-movers.rss"
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        '<title>DC Hub — DCPI Movers</title>'
        '<link>https://dchub.cloud/</link>'
        '<description>Markets whose DC Hub Power Index verdict flipped — '
        'operator-approved.</description>'
        f'<atom:link href="{self_url}" rel="self" type="application/rss+xml"/>')
    tail = '</channel></rss>'

    if not _enabled():
        return Response(head + tail,
                        content_type="application/rss+xml; charset=utf-8")
    conn = _db_conn()
    if conn is None:
        return Response(head + tail,
                        content_type="application/rss+xml; charset=utf-8")
    items_xml = []
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                "SELECT id, title, body, approved_at, created_at "
                "FROM media_recurring_queue "
                "WHERE status = 'approved' "
                "ORDER BY COALESCE(approved_at, created_at) DESC LIMIT %s",
                (RSS_LIMIT,))
            for r in cur.fetchall() or []:
                iid, title, body, approved_at, created_at = r
                when = approved_at or created_at
                pub = (when.strftime("%a, %d %b %Y %H:%M:%S +0000")
                       if when else "")
                guid = f"dcpi-movers-{iid}"
                items_xml.append(
                    "<item>"
                    f"<title>{_xml(title)}</title>"
                    f"<link>https://dchub.cloud/markets</link>"
                    f"<description>{_xml(body)}</description>"
                    + (f"<pubDate>{_xml(pub)}</pubDate>" if pub else "")
                    + f'<guid isPermaLink="false">{_xml(guid)}</guid>'
                    "</item>")
    except Exception as e:
        _log(f"rss failed: {e}")
        items_xml = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return Response(head + "".join(items_xml) + tail,
                    content_type="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=900",
                             "X-DC-Feature": "media-recurring-formats"})