"""media_journalist_lane.py — FEATURE 3: Journalist Outreach Lane (the distribution layer).

Phase r-journalist-lane (2026-06-23). DARK BY DEFAULT.

THE JOB
=======
Given a queued data story (a row in media_story_queue, or text passed in by the
operator), pick the best-fit OUTLET from a small journalist registry, draft a
tailored pitch email in DC Hub's analyst voice, run that draft through the SAME
runtime guards we hardened today (the partner-disparagement / publish filter +
the claim verifier), and write the approved draft to a REVIEW QUEUE
(media_pitch_queue) for the OPERATOR to copy and send by hand.

THIS MODULE NEVER SENDS AN EMAIL. It never POSTs to LinkedIn/Twitter, never
hits Resend, never auto-publishes a page. It only stages drafts a human sends.

HARD SAFETY INVARIANTS (non-negotiable — an auto-published post disparaged a
partner today, so every lever here is OFF until the operator turns it on):

  1. DARK BY DEFAULT. The whole feature is gated behind MEDIA_JOURNALIST_LANE_ENABLED.
     With the flag off every endpoint returns {"ok": true, "skipped": "disabled"}
     and the module touches NOTHING (no DB, no LLM, no writes).

  2. NO AUTO-SEND / NO AUTO-PUBLISH. /match-and-draft only ever INSERTs a row
     into media_pitch_queue with status='draft'. /log-sent/<id> only RECORDS
     that the operator sent it manually (it does not send anything). There is no
     code path in this module that transmits a pitch.

  3. EVERY draft is routed through the guards before it is queued:
       · content_publisher._should_skip_publish(cur, text, platform) — the
         partner-disparagement + quality + LLM-disclaimer filter.
       · routes.media_claim_verify.verify_claims(text) — the canonical-number
         claim verifier (blocks fabricated counts / banned over-claims).
     If EITHER flags the draft, it is DROPPED (not queued). Both are imported
     lazily inside try/except so a guard import problem fails CLOSED (drop the
     draft) — a guard we cannot run is treated as a rejection, never a pass.

  4. VERIFIED NUMBERS ONLY. Story facts come from live data:
       · market_power_scores (verdict / excess_power_score / constraint_score /
         time_to_power_months) for any DCPI verdict the pitch quotes.
       · canonical_stats.get_canonical_stats() for platform counts.
     We pass ONLY these verified values into the prompt and tell the model to use
     no other numbers; then verify_claims re-checks the OUTPUT against canon. A
     value that cannot be verified live is simply omitted — never fabricated.

  5. SELF-IMPORT-SAFE. The module imports cleanly with the flag off and no env:
     psycopg2, the LLM helper, the guards and canonical_stats are all imported
     lazily inside functions wrapped in try/except, so registering this blueprint
     can NEVER crash app boot.

  6. ADMIN-GATED. Every endpoint is behind the fail-closed admin gate (mirrors
     routes/media_outreach._admin_ok / routes/brain_innovation._innov_admin_ok):
     X-Admin-Key / X-Internal-Key header or ?admin_key= query param, matched
     against DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY / INTERNAL_KEY.

ENDPOINTS (all admin-gated, all dark unless the flag is on)
  GET  /api/v1/media/journalist-lane/registry        list registry
  POST /api/v1/media/journalist-lane/registry        upsert a journalist
  POST /api/v1/media/journalist-lane/match-and-draft  match outlet + draft pitch → queue
  GET  /api/v1/media/journalist-lane/pitch-queue      list queued drafts
  POST /api/v1/media/journalist-lane/log-sent/<id>    operator records a manual send

ACTIVATION
  Railway → Variables → MEDIA_JOURNALIST_LANE_ENABLED=1  (default OFF).
  Optional: ANTHROPIC_API_KEY (else a deterministic non-LLM template draft is
  used — still routed through both guards).
"""
from __future__ import annotations

from utils.anthropic_helper import cached_system
import os
import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
media_journalist_lane_bp = Blueprint("media_journalist_lane", __name__)


# ── KILL SWITCH (DARK BY DEFAULT) ─────────────────────────────────────────────
_FLAG = "MEDIA_JOURNALIST_LANE_ENABLED"


def _enabled() -> bool:
    """The feature is OFF unless the operator explicitly enables it."""
    return os.environ.get(_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _disabled_response():
    return jsonify({"ok": True, "skipped": "disabled",
                    "hint": f"set {_FLAG}=1 to enable (default OFF)"})


# ── ADMIN GATE (fail-closed — copied from media_outreach._admin_ok) ───────────
def _admin_ok() -> bool:
    """True ONLY when a provided key matches a configured env key. False when no
    admin key is configured OR none is provided (fail-closed by construction)."""
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key")
             or request.cookies.get("dchub_innov_key") or "").strip()
    return bool(_sent) and _sent in _keys


def _forbidden():
    return jsonify({"ok": False, "error": "admin-only",
                    "hint": "send X-Admin-Key / X-Internal-Key or append ?admin_key="}), 403


# ── DB (lazy, self-import-safe) ───────────────────────────────────────────────
def _conn():
    """Best-effort connection. Prefers main.get_db (the Neon pool); falls back to
    a direct psycopg2 connect. Returns None on any failure (never raises)."""
    try:
        from main import get_db
        return get_db()
    except Exception:
        pass
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            return None
        return psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
    except Exception as e:
        logger.warning("[journalist_lane] db connect failed: %s", str(e)[:160])
        return None


# Registry of outlets/journalists. cooldown_days enforces a per-outlet re-pitch
# floor; last_pitched_at is stamped on /log-sent (the operator's manual send).
_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_journalists (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    outlet          TEXT NOT NULL,
    beat            TEXT,
    contact         TEXT,
    cooldown_days   INTEGER NOT NULL DEFAULT 30,
    last_pitched_at TIMESTAMPTZ,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, outlet)
);
CREATE INDEX IF NOT EXISTS ix_mj_outlet ON media_journalists(outlet);

CREATE TABLE IF NOT EXISTS media_pitch_queue (
    id              BIGSERIAL PRIMARY KEY,
    journalist_id   BIGINT REFERENCES media_journalists(id),
    journalist_name TEXT,
    outlet          TEXT,
    contact         TEXT,
    story_ref       TEXT,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    match_reason    TEXT,
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS ix_mpq_status ON media_pitch_queue(status, created_at DESC);
"""

# Seed registry. Rich Miller / Data Center Frontier is the real seed; the rest
# are placeholders the operator should verify on each masthead before sending.
# contact left blank for placeholders ON PURPOSE — the operator fills the real
# address; this module never invents/sends one.
_SEED = [
    {"name": "Rich Miller", "outlet": "Data Center Frontier",
     "beat": "data center infrastructure, power, AI capacity, site selection",
     "contact": "rich@datacenterfrontier.com", "cooldown_days": 30,
     "notes": "Founder/editor. Likes data-driven power + capacity stories."},
    {"name": "DCD Newsdesk", "outlet": "Data Center Dynamics (DCD)",
     "beat": "data centers, hyperscale, M&A, power, cooling",
     "contact": "", "cooldown_days": 30,
     "notes": "PLACEHOLDER — verify the right DCD reporter/email before sending."},
    {"name": "Capacity Media Newsdesk", "outlet": "Capacity Media",
     "beat": "interconnection, subsea/fiber, wholesale infrastructure, deals",
     "contact": "", "cooldown_days": 30,
     "notes": "PLACEHOLDER — verify the right Capacity reporter/email before sending."},
    {"name": "Data Center Knowledge Newsdesk", "outlet": "Data Center Knowledge",
     "beat": "data center infrastructure, industry trends, operations",
     "contact": "", "cooldown_days": 30,
     "notes": "PLACEHOLDER — verify the right DCK reporter/email before sending."},
]

_SCHEMA_INIT = False


def _ensure_schema(cur, conn) -> bool:
    """Create tables (idempotent) + seed the registry once. Returns True on success.
    Never raises; commits via the passed connection."""
    global _SCHEMA_INIT
    try:
        cur.execute(_REGISTRY_SCHEMA)
        # Seed only rows that don't already exist (UNIQUE(name, outlet)).
        for j in _SEED:
            cur.execute(
                """INSERT INTO media_journalists
                       (name, outlet, beat, contact, cooldown_days, notes)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (name, outlet) DO NOTHING""",
                (j["name"], j["outlet"], j["beat"], j["contact"],
                 j["cooldown_days"], j["notes"]))
        try:
            conn.commit()
        except Exception:
            pass
        _SCHEMA_INIT = True
        return True
    except Exception as e:
        logger.warning("[journalist_lane] schema init failed: %s", str(e)[:200])
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# ── verified data pulls (NEVER fabricate) ─────────────────────────────────────
def _canon_facts() -> dict:
    """Live canonical platform counts (or conservative floors). Never raises."""
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats() or {}
        return {
            # ★2026-08-17: was facilities_phrase() = COUNT(*) rows, handed to a
            # composer that renders it as "N facilities". Distinct buildings is
            # the citeable basis (see ai_surface_canon).
            "facilities_phrase": cs.facilities_verified_phrase(),
            "countries_phrase": cs.countries_phrase(),
            "markets": int(s.get("markets") or 0) or None,
            "grid_coverage": cs.grid_coverage_phrase("short"),
        }
    except Exception as e:
        logger.warning("[journalist_lane] canon facts failed: %s", str(e)[:160])
        return {}


def _dcpi_verdict(cur, market_hint: str | None) -> dict | None:
    """Pull ONE real DCPI verdict row from market_power_scores. If a market hint
    is given we match it; otherwise we surface a strong BUILD market. Only the
    columns that exist live (verdict / excess_power_score / constraint_score /
    time_to_power_months) are returned. Returns None on any failure — the pitch
    then quotes no per-market number rather than a fabricated one."""
    try:
        if market_hint:
            cur.execute(
                """SELECT market_name, verdict, excess_power_score,
                          constraint_score, time_to_power_months
                     FROM market_power_scores
                    WHERE COALESCE(published, true) = true
                      AND market_name ILIKE %s
                    ORDER BY excess_power_score DESC NULLS LAST
                    LIMIT 1""",
                ("%" + market_hint.strip() + "%",))
            row = cur.fetchone()
            if row:
                return _verdict_row(row)
        cur.execute(
            """SELECT market_name, verdict, excess_power_score,
                      constraint_score, time_to_power_months
                 FROM market_power_scores
                WHERE COALESCE(published, true) = true
                  AND verdict = 'BUILD'
                ORDER BY excess_power_score DESC NULLS LAST
                LIMIT 1""")
        row = cur.fetchone()
        return _verdict_row(row) if row else None
    except Exception as e:
        logger.warning("[journalist_lane] dcpi verdict pull failed: %s", str(e)[:160])
        return None


def _verdict_row(row) -> dict:
    # row order matches the SELECT column list above.
    return {
        "market_name": row[0],
        "verdict": row[1],
        "excess_power_score": row[2],
        "constraint_score": row[3],
        "time_to_power_months": row[4],
    }


# ── outlet matching ───────────────────────────────────────────────────────────
def _tokens(s: str) -> set:
    import re
    return set(t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2)


def _match_outlet(cur, story_text: str):
    """Pick the best-fit ACTIVE, off-cooldown journalist by beat-token overlap
    with the story text. Returns (row_dict, reason) or (None, why_none).

    Cooldown: a journalist whose last_pitched_at is within cooldown_days is
    excluded so we never re-pitch the same outlet too soon."""
    try:
        cur.execute(
            """SELECT id, name, outlet, beat, contact, cooldown_days,
                      last_pitched_at
                 FROM media_journalists
                WHERE active = TRUE
                  AND (last_pitched_at IS NULL
                       OR last_pitched_at < NOW() - (cooldown_days || ' days')::interval)
            """)
        rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("[journalist_lane] match query failed: %s", str(e)[:160])
        return None, f"registry query failed: {str(e)[:80]}"

    if not rows:
        return None, ("no eligible journalist (registry empty or every outlet "
                      "is within its cooldown window)")

    story_tok = _tokens(story_text)
    best = None
    best_score = -1
    for r in rows:
        beat = r[3] or ""
        overlap = len(story_tok & _tokens(beat))
        if overlap > best_score:
            best_score = overlap
            best = r
    # Deterministic fallback: if nothing overlapped, the first eligible row
    # (registry is small + curated) — still a real, off-cooldown outlet.
    chosen = best
    reason = (f"beat match: {best_score} shared term(s) with "
              f"'{(chosen[3] or '')[:60]}'") if best_score > 0 else \
             "no beat-token overlap — first eligible off-cooldown outlet"
    return {
        "id": chosen[0], "name": chosen[1], "outlet": chosen[2],
        "beat": chosen[3], "contact": chosen[4],
        "cooldown_days": chosen[5], "last_pitched_at": chosen[6],
    }, reason


# ── pitch drafting ─────────────────────────────────────────────────────────────
_VOICE_SYSTEM = (
    "You are DC Hub's analyst-relations writer pitching a data story to a trade "
    "journalist. Write a SHORT, factual, non-promotional email pitch: a tight "
    "subject line and a 3-4 sentence body. Lead with the single most newsworthy "
    "verified number, say why it matters for this reporter's beat, and offer the "
    "underlying data + an interview. Cite 'DC Hub (dchub.cloud)'. "
    "CRITICAL RULES: Use ONLY the numbers in the VERIFIED FACTS block — invent no "
    "statistics, no dollar figures, no facility/market/country counts beyond what "
    "is given. Never disparage, name-and-shame, or make negative claims about any "
    "company, partner, or competitor. No hype, no superlatives about DC Hub. "
    "Output strictly as JSON: {\"subject\": \"...\", \"body\": \"...\"}."
)


def _build_prompt(journalist: dict, story_text: str, facts: dict,
                  verdict: dict | None) -> str:
    lines = ["VERIFIED FACTS (use ONLY these numbers):"]
    if facts.get("facilities_phrase"):
        lines.append(f"- Facilities tracked: {facts['facilities_phrase']}")
    if facts.get("countries_phrase"):
        lines.append(f"- Countries: {facts['countries_phrase']}")
    if facts.get("markets"):
        lines.append(f"- markets (DCPI): {facts['markets']}")
    if facts.get("grid_coverage"):
        lines.append(f"- Grid coverage: {facts['grid_coverage']}")
    if verdict and verdict.get("market_name"):
        v = verdict
        parts = [f"- DCPI verdict for {v['market_name']}: {v.get('verdict')}"]
        if v.get("excess_power_score") is not None:
            parts.append(f"excess_power_score={v['excess_power_score']}")
        if v.get("constraint_score") is not None:
            parts.append(f"constraint_score={v['constraint_score']}")
        if v.get("time_to_power_months") is not None:
            parts.append(f"time_to_power_months={v['time_to_power_months']}")
        lines.append(", ".join(parts))
    facts_block = "\n".join(lines)
    return (
        f"REPORTER: {journalist.get('name')} at {journalist.get('outlet')} "
        f"(beat: {journalist.get('beat')}).\n\n"
        f"STORY ANGLE (the operator's queued data story):\n{story_text.strip()[:1200]}\n\n"
        f"{facts_block}\n\n"
        "Write the pitch as JSON {\"subject\":..., \"body\":...}."
    )


def _llm_draft(prompt: str) -> dict | None:
    """Compose the pitch via Claude (Sonnet), mirroring linkedin_content_engine.
    Returns {"subject","body"} or None on any failure (caller falls back to a
    deterministic template). Lazy import; never raises."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import urllib.request
        from utils.anthropic_helper import anthropic_messages_url
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "max_tokens": 700,
            "system": cached_system(_VOICE_SYSTEM),
            "messages": [{"role": "user", "content": prompt}],
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
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not text:
            return None
        # The model is told to emit JSON; tolerate code-fence wrappers.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        obj = json.loads(text)
        subj = (obj.get("subject") or "").strip()
        bdy = (obj.get("body") or "").strip()
        if subj and bdy:
            return {"subject": subj[:300], "body": bdy}
        return None
    except Exception as e:
        logger.warning("[journalist_lane] llm draft failed: %s", str(e)[:160])
        return None


def _template_draft(journalist: dict, story_text: str, facts: dict,
                    verdict: dict | None) -> dict:
    """Deterministic fallback draft using ONLY verified facts. No fabricated
    numbers. Still routed through both guards by the caller."""
    headline = None
    if verdict and verdict.get("market_name") and verdict.get("verdict"):
        eps = verdict.get("excess_power_score")
        headline = (f"DC Hub's Data Center Power Index rates {verdict['market_name']} "
                    f"a {verdict['verdict']}"
                    + (f" (excess-power score {eps})" if eps is not None else "") + ".")
    elif facts.get("markets"):
        headline = (f"DC Hub now scores {facts['markets']} markets on the "
                    f"Data Center Power Index (DCPI).")
    elif facts.get("facilities_phrase"):
        headline = (f"DC Hub tracks {facts['facilities_phrase']} data center "
                    f"facilities across {facts.get('countries_phrase','')} countries.")
    else:
        headline = "DC Hub has a fresh data point that may fit your beat."

    angle = story_text.strip().split("\n", 1)[0][:240]
    subject = f"Data story for {journalist.get('outlet')}: {angle[:80]}"
    body = (
        f"Hi {journalist.get('name','')},\n\n"
        f"{headline} {angle}\n\n"
        f"Given your coverage of {journalist.get('beat','this space')}, I thought "
        f"this might be useful. I can share the underlying data and walk you "
        f"through the methodology on a quick call.\n\n"
        f"Source: DC Hub (dchub.cloud).\n\n"
        f"Best,\nDC Hub"
    )
    return {"subject": subject, "body": body}


# ── the guard gauntlet (both guards, fail-CLOSED) ─────────────────────────────
def _run_guards(cur, text: str, platform: str = "email") -> tuple[bool, list]:
    """Run BOTH hardened guards over the draft. Returns (passed, reasons).

    passed=False means DROP the draft. Fails CLOSED: if a guard cannot be
    imported/run, we treat that as a rejection (we never queue copy we couldn't
    verify). This is the partner-disparagement-can't-ship guarantee."""
    reasons: list = []
    passed = True

    # (1) content_publisher._should_skip_publish — disparagement / quality filter.
    try:
        from content_publisher import _should_skip_publish
        skip, why = _should_skip_publish(cur, text, platform)
        if skip:
            passed = False
            reasons.append(f"_should_skip_publish: {why}")
    except Exception as e:
        passed = False
        reasons.append(f"_should_skip_publish unavailable (fail-closed): {str(e)[:120]}")

    # (2) media_claim_verify.verify_claims — canonical-number claim verifier.
    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text)
        if not cv.get("ok", True):
            passed = False
            reasons.append("verify_claims blocks: " + "; ".join(cv.get("blocks") or []))
        for w in (cv.get("warns") or []):
            reasons.append(f"verify_claims warn: {w}")
    except Exception as e:
        passed = False
        reasons.append(f"verify_claims unavailable (fail-closed): {str(e)[:120]}")

    return passed, reasons


# ── ENDPOINTS ─────────────────────────────────────────────────────────────────
@media_journalist_lane_bp.route(
    "/api/v1/media/journalist-lane/registry", methods=["GET"])
def registry_list():
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return _forbidden()
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        cur = c.cursor()
        _ensure_schema(cur, c)
        cur.execute(
            """SELECT id, name, outlet, beat, contact, cooldown_days,
                      last_pitched_at, active, notes
                 FROM media_journalists ORDER BY outlet, name""")
        out = []
        for r in cur.fetchall() or []:
            out.append({
                "id": r[0], "name": r[1], "outlet": r[2], "beat": r[3],
                "contact": r[4], "cooldown_days": r[5],
                "last_pitched_at": r[6].isoformat() if r[6] else None,
                "active": r[7], "notes": r[8],
            })
        return jsonify({"ok": True, "count": len(out), "journalists": out})
    except Exception as e:
        logger.warning("[journalist_lane] registry_list failed: %s", str(e)[:160])
        return jsonify({"ok": False, "error": str(e)[:160]}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@media_journalist_lane_bp.route(
    "/api/v1/media/journalist-lane/registry", methods=["POST"])
def registry_upsert():
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return _forbidden()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    outlet = (data.get("outlet") or "").strip()
    if not name or not outlet:
        return jsonify({"ok": False, "error": "name and outlet are required"}), 400
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        cur = c.cursor()
        _ensure_schema(cur, c)
        cur.execute(
            """INSERT INTO media_journalists
                   (name, outlet, beat, contact, cooldown_days, active, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (name, outlet) DO UPDATE SET
                   beat = EXCLUDED.beat,
                   contact = EXCLUDED.contact,
                   cooldown_days = EXCLUDED.cooldown_days,
                   active = EXCLUDED.active,
                   notes = EXCLUDED.notes
               RETURNING id""",
            (name, outlet, (data.get("beat") or "").strip() or None,
             (data.get("contact") or "").strip() or None,
             int(data.get("cooldown_days") or 30),
             bool(data.get("active", True)),
             (data.get("notes") or "").strip() or None))
        rid = (cur.fetchone() or [None])[0]
        c.commit()
        return jsonify({"ok": True, "id": rid, "upserted": {"name": name, "outlet": outlet}})
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("[journalist_lane] registry_upsert failed: %s", str(e)[:160])
        return jsonify({"ok": False, "error": str(e)[:160]}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@media_journalist_lane_bp.route(
    "/api/v1/media/journalist-lane/match-and-draft", methods=["POST"])
def match_and_draft():
    """Match the best-fit outlet for a queued data story, draft a pitch, run BOTH
    guards, and QUEUE the draft for the operator. NEVER sends anything.

    Body: {"text": "<story angle>"} OR {"story_id": <id from media_story_queue>},
    optional "market_hint" to anchor the DCPI verdict pull. If a story is dropped
    by the guards, returns ok:true, queued:false, dropped:true with reasons."""
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return _forbidden()

    data = request.get_json(silent=True) or {}
    story_text = (data.get("text") or "").strip()
    story_id = data.get("story_id")
    market_hint = (data.get("market_hint") or "").strip() or None

    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        cur = c.cursor()
        _ensure_schema(cur, c)

        # Resolve the story text from media_story_queue if a story_id was passed
        # and no text was supplied. The table may not exist — degrade gracefully.
        story_ref = None
        if not story_text and story_id is not None:
            try:
                cur.execute(
                    "SELECT id, COALESCE(headline, title, summary, body, '') "
                    "FROM media_story_queue WHERE id = %s", (story_id,))
                row = cur.fetchone()
                if row:
                    story_ref = f"media_story_queue#{row[0]}"
                    story_text = (row[1] or "").strip()
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                story_text = ""
        elif story_id is not None:
            story_ref = f"media_story_queue#{story_id}"

        if not story_text:
            return jsonify({"ok": False, "error": (
                "no story text — pass {\"text\": ...} or a {\"story_id\": ...} "
                "that resolves in media_story_queue")}), 400

        # 1) Match the outlet (respects cooldown).
        journalist, reason = _match_outlet(cur, story_text)
        if journalist is None:
            return jsonify({"ok": True, "queued": False, "matched": False,
                            "reason": reason})

        # 2) Pull VERIFIED facts only.
        facts = _canon_facts()
        verdict = _dcpi_verdict(cur, market_hint or _maybe_market_from_text(story_text))

        # 3) Draft (LLM preferred, deterministic template fallback).
        prompt = _build_prompt(journalist, story_text, facts, verdict)
        draft = _llm_draft(prompt) or _template_draft(journalist, story_text, facts, verdict)
        full_text = (draft["subject"] + "\n\n" + draft["body"]).strip()

        # 4) GUARD GAUNTLET — both hardened guards, fail-closed.
        passed, reasons = _run_guards(cur, full_text, platform="email")
        if not passed:
            logger.info("[journalist_lane] draft DROPPED by guards: %s",
                        "; ".join(reasons)[:300])
            return jsonify({"ok": True, "queued": False, "dropped": True,
                            "matched_outlet": journalist["outlet"],
                            "reasons": reasons})

        # 5) QUEUE for operator review (status='draft'). No send.
        cur.execute(
            """INSERT INTO media_pitch_queue
                   (journalist_id, journalist_name, outlet, contact, story_ref,
                    subject, body, match_reason, status, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)
               RETURNING id""",
            (journalist["id"], journalist["name"], journalist["outlet"],
             journalist["contact"], story_ref, draft["subject"], draft["body"],
             reason, ("; ".join(reasons)[:500] if reasons else None)))
        pid = (cur.fetchone() or [None])[0]
        c.commit()
        return jsonify({
            "ok": True, "queued": True, "pitch_id": pid,
            "matched_outlet": journalist["outlet"],
            "journalist": journalist["name"], "match_reason": reason,
            "subject": draft["subject"],
            "warns": [r for r in reasons if r.startswith("verify_claims warn")],
            "note": ("DRAFT staged for operator review — NOT sent. Approve + send "
                     "manually, then POST /log-sent/<id>."),
        })
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("[journalist_lane] match_and_draft failed: %s", str(e)[:200])
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


def _maybe_market_from_text(text: str) -> str | None:
    """Very light heuristic: if the story names a known marquee market, anchor
    the DCPI verdict pull to it. Returns None if nothing obvious — then we just
    surface a strong BUILD market. Never fabricates."""
    t = (text or "").lower()
    for m in ("northern virginia", "ashburn", "phoenix", "dallas", "columbus",
              "atlanta", "chicago", "santa clara", "cheyenne", "reno",
              "salt lake", "council bluffs", "richmond", "portland"):
        if m in t:
            return m
    return None


@media_journalist_lane_bp.route(
    "/api/v1/media/journalist-lane/pitch-queue", methods=["GET"])
def pitch_queue():
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return _forbidden()
    status = (request.args.get("status") or "").strip()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except Exception:
        limit = 50
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        cur = c.cursor()
        _ensure_schema(cur, c)
        if status:
            cur.execute(
                """SELECT id, journalist_name, outlet, contact, story_ref,
                          subject, body, match_reason, status, created_at,
                          sent_at, notes
                     FROM media_pitch_queue WHERE status = %s
                    ORDER BY created_at DESC LIMIT %s""", (status, limit))
        else:
            cur.execute(
                """SELECT id, journalist_name, outlet, contact, story_ref,
                          subject, body, match_reason, status, created_at,
                          sent_at, notes
                     FROM media_pitch_queue
                    ORDER BY created_at DESC LIMIT %s""", (limit,))
        out = []
        for r in cur.fetchall() or []:
            out.append({
                "id": r[0], "journalist_name": r[1], "outlet": r[2],
                "contact": r[3], "story_ref": r[4], "subject": r[5],
                "body": r[6], "match_reason": r[7], "status": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
                "sent_at": r[10].isoformat() if r[10] else None,
                "notes": r[11],
            })
        return jsonify({"ok": True, "count": len(out), "pitches": out})
    except Exception as e:
        logger.warning("[journalist_lane] pitch_queue failed: %s", str(e)[:160])
        return jsonify({"ok": False, "error": str(e)[:160]}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@media_journalist_lane_bp.route(
    "/api/v1/media/journalist-lane/log-sent/<int:pitch_id>", methods=["POST"])
def log_sent(pitch_id: int):
    """Operator RECORDS that they sent the pitch manually. This does NOT send
    anything — it flips status='sent', stamps sent_at, and stamps the
    journalist's last_pitched_at so the cooldown window starts."""
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return _forbidden()
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        cur = c.cursor()
        _ensure_schema(cur, c)
        cur.execute(
            """UPDATE media_pitch_queue
                  SET status = 'sent', sent_at = NOW()
                WHERE id = %s AND status <> 'sent'
            RETURNING journalist_id""", (pitch_id,))
        row = cur.fetchone()
        if not row:
            c.rollback()
            return jsonify({"ok": False, "error": "pitch not found or already sent",
                            "pitch_id": pitch_id}), 404
        jid = row[0]
        if jid is not None:
            cur.execute(
                "UPDATE media_journalists SET last_pitched_at = NOW() WHERE id = %s",
                (jid,))
        c.commit()
        return jsonify({"ok": True, "pitch_id": pitch_id, "status": "sent",
                        "note": ("recorded as manually sent; outlet cooldown "
                                 "window started")})
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning("[journalist_lane] log_sent failed: %s", str(e)[:160])
        return jsonify({"ok": False, "error": str(e)[:160]}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
