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
import re as _re
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
#
# ★★2026-07-27 data QA — every number here used to be a hardcoded LITERAL wired
# to nothing, and each had drifted: "178 countries" (live 180), "48 tools" (live
# 80), "Cited by 15+ AI platforms" (live 10 distinct), and "2,000+ tracked
# transactions" — an OVER-CLAIM (deduped reality ~1,5xx) that the
# ai_surface_canon sentinel failed to catch because its stale_markers denylist
# matches the literal strings "2,000+ tracked deals"/"2,000+ M&A"/"2,000+ deals"
# and this copy said "transactions". A denylist that matches strings instead of
# claims cannot hold this surface.
#
# So: NO bare numerals in hero copy. Use the {placeholders} below and they are
# resolved from canon at serve time by _fill() — a value can no longer go stale
# without canon itself going stale.
#   {facilities} {facilities_full} {countries} {markets} {deals}
# There is intentionally NO {tools} placeholder: the live tool count is only
# available via ai_surface_canon.resolve_canon(), which does HTTP, and nothing
# network-bound belongs on the homepage hero path (see _phrases()). Hero copy
# therefore does not cite a tool count at all.
# ★{facilities} resolves to the RAW tracked floor ("22,000+"), which counts
# SOURCE RECORDS, not distinct facilities — 22,775 rows represent ~14,686
# distinct facilities by canonical_slug. Copy therefore says "facility records",
# never "facilities", so the claim is true on the raw basis. Do NOT relabel it
# "facilities" without switching to a distinct-count phrase: that is a ~55%
# over-claim. The distinct number is pending the dedup keeper-election repair
# (9,318 of 14,686 distinct facilities currently have NO keeper row, so no
# is_duplicate-based count can be cited yet).
_SEED_MESSAGES = [
    (
        "The neutral data layer<br>[GRAD]for data center infrastructure.[/GRAD]",
        "{facilities} facility records. {countries} countries. Power, fiber, water, M&A, tax incentives — one MCP endpoint or REST API. The research backend AI assistants and operators both quote.",
        "switzerland",
    ),
    (
        "Cited by ChatGPT.<br>[GRAD]Quoted by Claude.[/GRAD]",
        "When AI assistants research data centers, they reach DC Hub before they reach ERCOT. Real-time facility, grid, fiber and M&A intelligence — one MCP endpoint.",
        "ai-citations",
    ),
    (
        "{facilities} facility records.<br>[GRAD]One source of truth.[/GRAD]",
        "Operators, investors and AI agents all query the same neutral layer. {countries} countries. 7 ISOs. 4x/day refresh. The map the industry can finally agree on.",
        "single-source",
    ),
    (
        "Real-time power.<br>[GRAD]Live grid pulse.[/GRAD]",
        "Substations, transmission lines, gas pipelines, fiber routes, water risk — all in one query. The infrastructure stack hyperscalers actually price against.",
        "power-stack",
    ),
    (
        "Off-market pocket listings.<br>[GRAD]Live deal flow.[/GRAD]",
        "Sub-MW capacity, brownfield campuses, {deals} tracked transactions, M&A pipeline tagged by market tier and DCPI score. The deal book operators don't post publicly.",
        "deal-flow",
    ),
    (
        "Built for AI agents.<br>[GRAD]Loved by humans.[/GRAD]",
        "MCP-native from day one. Sub-300ms median latency. Cited by AI assistants across every major platform. Designed so your agent can answer 'where should I build' in one call.",
        "agent-first",
    ),
]


# ── canon substitution ───────────────────────────────────────────────
_PHRASE_TTL_S = 300
_phrase_cache: dict | None = None
_phrase_ts: float = 0.0


def _phrases() -> dict:
    """Canon phrases for hero copy, cached and NETWORK-FREE.

    ★Deliberately does NOT call ai_surface_canon.resolve_canon(): that performs
    HTTP fetches, and this runs on the homepage hero path — a slow or hanging
    upstream would stall the hero for every visitor. canonical_stats reads Neon
    directly with its own 10-minute cache and a 6s connect timeout, so the worst
    case here is a stale-but-valid phrase, never a hang. Nothing that needs the
    network belongs in this function.
    """
    global _phrase_cache, _phrase_ts
    now = time.time()
    if _phrase_cache is not None and (now - _phrase_ts) < _PHRASE_TTL_S:
        return _phrase_cache
    vals: dict = {}
    try:
        import canonical_stats as _cs
        vals = {
            # ★2026-08-17: was facilities_phrase() = COUNT(*) rows. Hero copy
            # renders {facilities} as "N facilities", so the homepage published
            # the raw discovery pile as a building count — 26,000+ against a
            # public canon of 18,300+. {facilities_full} still carries the
            # explicitly-labelled "tracked · verified" pair.
            "facilities":      _cs.facilities_verified_phrase(),
            "facilities_full": _cs.facilities_phrase_full(),
            "countries":       _cs.countries_phrase(),
            "markets":         _cs.markets_phrase(),
            "deals":           _cs.deals_phrase(),
        }
    except Exception as e:
        logger.warning(f"[hero] canon resolve failed: {e}")
    if vals:
        _phrase_cache, _phrase_ts = vals, now
    return vals or (_phrase_cache or {})


def _fill(text: str) -> str:
    """Resolve {placeholders} in hero copy from canon at serve time.

    ★2026-07-27: exists so hero numbers cannot drift. Every canon helper floors
    DOWN (citation-safe, never above reality), so a canon hiccup under-claims
    rather than over-claims. A placeholder we cannot resolve is stripped rather
    than shown to a visitor as a literal "{brace}".
    """
    if not text or "{" not in text:
        return text
    for k, v in (_phrases() or {}).items():
        if v:
            text = text.replace("{" + k + "}", str(v))
    return _re.sub(r"\s*\{[a-z_]+\}", "", text)


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
                {"h1_html": _fill(h1), "sub_text": _fill(s), "tag": t}
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
    # Resolve canon placeholders AFTER the pick so the numbers are current even
    # when the copy itself came from the hero_messages table (or the brain).
    h1, sub = _fill(h1), _fill(sub)
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
        # pipeline_mw intentionally absent — see the audit note on _probes below.
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
        # ★★2026-07-27 pipeline-GW audit — `pipeline_mw` is REMOVED from this
        # public ticker, not restated. `SUM(capacity_mw) FROM capacity_pipeline`
        # returned 2,580,500 MW (published as 2,514 GW), and the table is
        # contaminated:
        #   - 45 rows >=10,000 MW carry 1,021.7 GW (39.6% of the total). Google
        #     Nevada 150,000 MW with status 'operational'; NextEra/Dominion
        #     130,000 MW status 'acquisition'; AEP 63,000; Dominion 48,000;
        #     PPL 25,200 — the last three are UTILITY data-centre load-request
        #     queues, summed as if each were one building.
        #   - 285 duplicate groups double-count 761.0 GW (29.5%).
        #   - ~339 GW sits under operational/acquisition/cancelled/lease.
        #   - 428 rows have Unknown/blank operator = 546.0 GW (21.2%).
        # Strict cleanup lands at 486.4 GW (5.2x below what we published), and
        # the sibling figure from `facilities` cleans to 251.4 GW — still 1.9x
        # apart. No pipeline number is publishable from either source until the
        # dchub_pipeline extractor stops writing aggregates and unparsed
        # None/Unknown rows, and the aggregates are quarantined behind a
        # data_flag the way `deals` does it.
        # The homepage has no pipeline pill, so nothing renders differently —
        # but this endpoint is public, so leaving the field served meant any
        # agent reading it consumed the 2,514 GW claim. Do NOT re-add without
        # the quarantine work.
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
    safe = _public_safe(brief.get("summary"))
    if safe:
        out["voice_line"] = safe
        out["voice_source"] = "inspector"
    else:
        out["voice_line"] = _voice(out)
        out["voice_source"] = "rules"
    # PUBLIC endpoint: never expose the raw internal Inspector narrative
    # (ops metrics, customer emails, escalation language, possibly-stale
    # counts like the legacy 12,907). Keep only safe display fields.
    if out.get("inspector_brief"):
        out["inspector_brief"] = {
            "age_human":    out["inspector_brief"].get("age_human"),
            "generated_at": out["inspector_brief"].get("generated_at"),
        }

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


_PUBLIC_BANNED = (
    "@", "escalation", "starving", "autopilot", "pipeline is", "conversion",
    "detector", "consistency", "country tag", "no country", "founding",
    "customer", "paid_", "facilities_added", "facilities tracked",
    "unknown entit", "_7d", "_24h", "_30d", "per ", "quarter of", "degrad",
    "stuck", "churn", "leak", "email", "escalat",
)


def _public_safe(text):
    """Return the Inspector line ONLY if safe for the PUBLIC homepage —
    no internal metrics, customer identities, escalation language, or raw
    (possibly-stale) counts. Otherwise None, so the caller falls back to the
    safe rule-based _voice(). This is what keeps internal ops off the public
    surface — added after the stale-count + "elevated escalations" homepage leak (2026-06-04)."""
    if not text or not isinstance(text, str):
        return None
    low = text.lower()
    if any(tok in low for tok in _PUBLIC_BANNED):
        return None
    import re as _re
    if _re.search(r"\d{3,}", text):   # reject raw integer counts in the public voice
        return None
    return text.strip()


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
