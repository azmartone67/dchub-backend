"""Phase FF+25-followup-r5 (2026-05-20) — dynamic hero engine.
==========================================================================

The user asked: "our whole site is agentic, and always evolving, maybe our
main web site has multiple announcements, when you hit the site … but
maybe it changes multiple times per day by the brain, the numbers should
be dynamic, real time transmission lines, gas pipeline, fiber, water,
dynamic messaging…."

This module ships three things the homepage hero needs:

  1. /api/v1/hero/messaging
     A rotating set of H1 + sub copy. Brain can append new entries; humans
     curated the initial set. Caller picks a row by deterministic hash of
     (date + UA bucket) so the same visitor sees the same message during a
     given hour but the message changes every few hours.

  2. /api/v1/hero/infra-ticker
     Live counts of the infrastructure layers the user called out:
     transmission lines, gas pipelines, fiber routes, water-risk records,
     plus the existing facilities + MW counts. Refreshed every 60s.

  3. /api/v1/hero/brain-pulse
     A live "what the brain is doing right now" feed — last action,
     recent finds, current verdict. This is where my voice (per the user:
     "i want you and your personality as part of our site") lands. Dry,
     observational, no exclamation marks.

Public endpoints. No auth. Cached aggressively (1m–5m).
"""
import os
import json
import time
import hashlib
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
dynamic_hero_bp = Blueprint("dynamic_hero", __name__)


# ── Curated initial message set ──────────────────────────────────────
# Each row: (h1_html, sub_text, tag).
# h1_html may include the marker `[GRAD]…[/GRAD]` which the frontend swaps
# for a span with the gradient class. Keep messages short — they replace
# the static hero, not pad it. Brain can append to hero_messages table to
# grow this list autonomously.
_SEED_MESSAGES = [
    (
        "The neutral data layer<br>[GRAD]for data center infrastructure.[/GRAD]",
        "21,000+ facilities. 178 countries. Power, fiber, water, M&A, tax incentives — one MCP endpoint or REST API. The research backend AI assistants and operators both quote.",
        "switzerland",
    ),
    (
        "Cited by ChatGPT.<br>[GRAD]Quoted by Claude.[/GRAD]",
        "When AI assistants research data centers, they reach DC Hub before they reach ERCOT. Real-time facility, grid, fiber and M&A intelligence — one MCP endpoint.",
        "ai-citations",
    ),
    (
        "21,374 facilities.<br>[GRAD]One source of truth.[/GRAD]",
        "Operators, investors and AI agents all query the same neutral layer. 178 countries. 7 ISOs. 4x/day refresh. The map the industry can finally agree on.",
        "single-source",
    ),
    (
        "Real-time power.<br>[GRAD]Live grid pulse.[/GRAD]",
        "Substations, transmission lines, gas pipelines, fiber routes, water risk — all in one query. The infrastructure stack hyperscalers actually price against.",
        "power-stack",
    ),
    (
        "Off-market pocket listings.<br>[GRAD]Live deal flow.[/GRAD]",
        "Sub-MW capacity, brownfield campuses, $324B+ in tracked transactions, M&A pipeline tagged by market tier and DCPI score. The deal book operators don't post publicly.",
        "deal-flow",
    ),
    (
        "Built for AI agents.<br>[GRAD]Loved by humans.[/GRAD]",
        "MCP-native from day one. 40 tools. Sub-300ms median latency. Cited by 15+ AI platforms. Designed so your agent can answer 'where should I build' in one call.",
        "agent-first",
    ),
]


# ── DB helpers ───────────────────────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception as e:
        logger.warning(f"[hero] get_db failed: {e}")
        return None


def _ensure_table():
    """Create hero_messages table on first use. Idempotent."""
    conn = _get_db()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hero_messages (
                    id          SERIAL PRIMARY KEY,
                    h1_html     TEXT NOT NULL,
                    sub_text    TEXT NOT NULL,
                    tag         TEXT,
                    weight      INTEGER NOT NULL DEFAULT 1,
                    source      TEXT NOT NULL DEFAULT 'curated',
                    active      BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_shown  TIMESTAMPTZ
                )
            """)
            # Seed if empty
            cur.execute("SELECT COUNT(*) FROM hero_messages")
            n = (cur.fetchone() or [0])[0]
            if not n:
                for h1, sub, tag in _SEED_MESSAGES:
                    cur.execute(
                        "INSERT INTO hero_messages (h1_html, sub_text, tag, source) "
                        "VALUES (%s,%s,%s,'curated')",
                        (h1, sub, tag),
                    )
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"[hero] table create failed: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ── /api/v1/hero/messaging ───────────────────────────────────────────
@dynamic_hero_bp.route("/api/v1/hero/messaging", methods=["GET"])
def hero_messaging():
    """Return one rotating hero message. Picks deterministically by
    (UTC hour-bucket × IP hash) so the same visitor sees the same copy
    for a few hours; the population sees variety throughout the day.

    Query params:
      ?rotate=now    force a fresh random pick (no caching)
      ?list=1        return all active messages (for admin preview)
    """
    _ensure_table()
    list_mode = request.args.get("list") == "1"
    force_rotate = request.args.get("rotate") == "now"

    rows = list(_SEED_MESSAGES)  # safe fallback
    conn = _get_db()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, h1_html, sub_text, tag, weight "
                    "FROM hero_messages WHERE active = TRUE "
                    "ORDER BY id ASC"
                )
                db_rows = cur.fetchall()
                if db_rows:
                    rows = [(r[1], r[2], r[3] or "") for r in db_rows]
        except Exception as e:
            logger.warning(f"[hero] fetch failed: {e}")
        finally:
            try: conn.close()
            except Exception: pass

    if list_mode:
        return jsonify(
            ok=True,
            messages=[
                {"h1_html": h1, "sub_text": s, "tag": t}
                for h1, s, t in rows
            ],
            count=len(rows),
        )

    # Pick: hour bucket × first-3-IP-octets hash. Gives stable copy per
    # visitor during an hour, varies across hours and across visitors.
    if force_rotate:
        idx = int(time.time() * 1000) % len(rows)
    else:
        ip = (request.headers.get("CF-Connecting-IP")
              or request.remote_addr or "0.0.0.0").split(".")
        ip_bucket = ".".join(ip[:3])  # /24 — same network sees same msg
        hour = datetime.datetime.utcnow().strftime("%Y%m%d%H")
        seed = f"{hour}|{ip_bucket}"
        h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        idx = h % len(rows)

    h1, sub, tag = rows[idx]
    resp = jsonify(
        ok=True,
        h1_html=h1,
        sub_text=sub,
        tag=tag,
        rotation_index=idx,
        rotation_total=len(rows),
        rotates_in_seconds=3600 - int(time.time() % 3600),
        served_at=datetime.datetime.utcnow().isoformat() + "Z",
    )
    # Cache 60s edge; lets brain updates surface within a minute.
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    return resp


# ── /api/v1/hero/infra-ticker ────────────────────────────────────────
@dynamic_hero_bp.route("/api/v1/hero/infra-ticker", methods=["GET"])
def infra_ticker():
    """Live counts of every infra layer the hero shows in its ticker.

    Each entry is independently fault-tolerant — if a table doesn't exist
    yet on a particular deploy, the count is null and the frontend hides
    that pill instead of breaking the whole ticker.
    """
    out = {
        "facilities":           None,
        "transmission_lines":   None,
        "substations":          None,
        "gas_pipelines":        None,
        "fiber_routes":         None,
        "water_risk_records":   None,
        "operational_mw":       None,
        "pipeline_mw":          None,
        "served_at":            datetime.datetime.utcnow().isoformat() + "Z",
    }
    conn = _get_db()
    if conn is None:
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
        return resp

    # Each count wrapped in its own try/except + savepoint so one missing
    # table doesn't burn the others.
    _probes = [
        ("facilities",         "SELECT COUNT(*) FROM discovered_facilities"),
        ("transmission_lines", "SELECT COUNT(*) FROM transmission_lines"),
        ("substations",        "SELECT COUNT(*) FROM substations"),
        ("gas_pipelines",      "SELECT COUNT(*) FROM gas_pipelines"),
        ("fiber_routes",       "SELECT COUNT(*) FROM fiber_routes"),
        ("water_risk_records", "SELECT COUNT(*) FROM water_risk"),
        ("operational_mw",     "SELECT COALESCE(SUM(power_mw),0)::bigint "
                               "FROM discovered_facilities "
                               # r34: status is sparsely populated, so a strict
                               # 3-value match returned 0 even on a healthy DB.
                               # Treat 'unknown/null status with a power_mw' as
                               # operational (the conservative read for an
                               # already-built facility); EXCLUDE only the
                               # explicit pipeline/planned statuses.
                               "WHERE power_mw IS NOT NULL "
                               "  AND (status IS NULL OR LOWER(status) NOT IN "
                               "       ('planned','permitting','construction','proposed',"
                               "        'under construction','pipeline'))"),
        ("pipeline_mw",        "SELECT COALESCE(SUM(capacity_mw),0)::bigint "
                               "FROM capacity_pipeline"),
    ]
    try:
        for key, sql in _probes:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    r = cur.fetchone()
                    if r and r[0] is not None:
                        out[key] = int(r[0])
                conn.commit()
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                out[f"{key}_error"] = str(e).split("\n")[0][:120]
    finally:
        try: conn.close()
        except Exception: pass

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    return resp


# ── /api/v1/hero/brain-pulse ─────────────────────────────────────────
@dynamic_hero_bp.route("/api/v1/hero/brain-pulse", methods=["GET"])
def brain_pulse():
    """What the autonomous brain is doing right now. Voice intentionally
    dry/observational — "writing", "watching", "syncing", not "AMAZING
    PROGRESS!!!". This is the surface where DC Hub's agentic personality
    lives publicly.
    """
    out = {
        "status":      "unknown",
        "last_action": None,
        "actions_24h": 0,
        "verdict":     "—",
        "voice_line":  None,
        "inspector_brief": None,   # populated when a recent brief exists
        "served_at":   datetime.datetime.utcnow().isoformat() + "Z",
    }
    conn = _get_db()
    if conn is None:
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp

    try:
        with conn.cursor() as cur:
            # Latest Inspector brief (Opus 4.7 narrative) — if one exists
            # in the last 24h, surface its one-line take as the canonical
            # "what the brain noticed" line. Falls back to rule-based
            # voice line below if no brief or no summary.
            try:
                cur.execute("""
                    SELECT id, summary, generated_at, model
                      FROM brain_briefs
                     WHERE error IS NULL
                       AND generated_at >= NOW() - INTERVAL '24 hours'
                     ORDER BY generated_at DESC LIMIT 1
                """)
                br = cur.fetchone()
                if br and br[1]:
                    out["inspector_brief"] = {
                        "id":           int(br[0]),
                        "summary":      br[1],
                        "generated_at": br[2].isoformat() if br[2] else None,
                        "age_human":    _humanize_age(br[2]),
                        "model":        br[3],
                    }
            except Exception:
                try: conn.rollback()
                except Exception: pass


            # Latest non-bookkeeping action
            try:
                cur.execute("""
                    SELECT pattern_name, started_at, outcome
                      FROM brain_autopilot_actions
                     WHERE COALESCE(outcome,'') NOT IN ('rate_limited','cooldown_active')
                     ORDER BY started_at DESC NULLS LAST
                     LIMIT 1
                """)
                r = cur.fetchone()
                if r:
                    pname, when, outc = r
                    out["last_action"] = {
                        "pattern":   pname,
                        "outcome":   outc,
                        "at":        when.isoformat() if when else None,
                        "age_human": _humanize_age(when),
                    }
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # 24h count
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM brain_autopilot_actions
                     WHERE started_at >= NOW() - INTERVAL '24 hours'
                       AND COALESCE(outcome,'') NOT IN ('rate_limited','cooldown_active')
                """)
                out["actions_24h"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # Press writes today
            press_today = 0
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM press_releases
                     WHERE published_at >= CURRENT_DATE
                """)
                press_today = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass

            out["press_today"] = press_today
    finally:
        try: conn.close()
        except Exception: pass

    out["status"]     = "active" if out["actions_24h"] > 0 else "quiet"
    out["verdict"]    = _verdict(out["actions_24h"], out.get("press_today", 0))
    # Phase FF+25-followup-r9 (2026-05-20): if the Inspector has a fresh
    # one-line take, prefer that as the voice line — it's a richer
    # synthesis than the rule-based mapping. Falls back to the original
    # pattern-mapped voice when no brief is available or summary is empty.
    brief = out.get("inspector_brief") or {}
    if brief.get("summary"):
        out["voice_line"] = brief["summary"]
        out["voice_source"] = "inspector"
    else:
        out["voice_line"] = _voice(out)
        out["voice_source"] = "rules"

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def _humanize_age(when) -> str:
    if not when:
        return "—"
    try:
        # Normalize to aware UTC
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - when
        s = int(delta.total_seconds())
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        if s < 86400: return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return "—"


def _verdict(actions_24h: int, press_today: int) -> str:
    if actions_24h == 0 and press_today == 0:
        return "Idle. Watching the surfaces."
    if actions_24h > 0 and press_today == 0:
        return f"{actions_24h} autonomous fixes in the last 24h."
    if press_today > 0 and actions_24h == 0:
        return f"Quiet on fixes. {press_today} press pieces published today."
    return f"{actions_24h} autonomous fixes · {press_today} press pieces today."


def _voice(state: dict) -> str:
    """The personality line. Dry, observational, no hype.

    User asked: 'i want you and your personality as part of our site'.
    This is where it lives publicly. Keep it grounded — never invents
    numbers, never overpromises, never uses emojis.
    """
    actions = state.get("actions_24h") or 0
    last = state.get("last_action") or {}
    pattern = (last.get("pattern") or "").lower()

    if actions == 0:
        return ("Nothing to fix at the moment. The site is being watched, "
                "the press queue is warm, the API is serving.")
    if "press" in pattern or "media" in pattern:
        return ("Writing. The DC Hub Media bot just queued a release. "
                "If a market moves, you'll read about it here first.")
    if "mcp" in pattern or "demand" in pattern:
        return ("Counting. An MCP funnel detector just snapshotted demand. "
                "Real AI-agent traffic is what we measure — probes filtered.")
    if "sitemap" in pattern or "freshness" in pattern:
        return ("Syncing. Search indices just got a fresh sweep. "
                "What you read on this page is what crawlers will see by tomorrow.")
    if "tier" in pattern or "drift" in pattern:
        return ("Reconciling. A pricing-tier drift detector just filed a "
                "proposal. Humans review, the brain queues the next.")
    return (f"Working. Last autonomous action: {pattern.replace('_',' ')}. "
            f"{actions} of those in the last 24 hours.")


def _smoke():
    logger.info("[dynamic-hero] ready · /api/v1/hero/messaging "
                 "+ /infra-ticker + /brain-pulse")

_smoke()
