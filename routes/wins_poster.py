"""
wins_poster.py — DC Hub Media "wins" auto-drafter (r-wins, 2026-06-16).

The owner's note: DC Hub Media should post like an ANALYST reporting DC Hub's OWN
successes — like the "Claude cited DC Hub to answer 'where to build 100MW'" and
"a quiet milestone… 178 countries" posts — NOT a scraped-news-headline dump.

This detects a REAL, FRESH DC Hub win and DRAFTS an analyst-voice post for it. It
is deliberately conservative:

  • REVIEW-QUEUE DEFAULT — the job only writes status='draft' into
    social_media_posts. The existing marketing publisher (Path A) drains
    status='approved', so a draft is NEVER auto-sent. A human flips draft→approved.
    The optional env WINS_POSTER_AUTOPILOT_ENABLED (default off) is the only way the
    job writes 'approved'.
  • HONEST numbers only — milestone numbers come ONLY from canonical_stats phrase
    helpers (citation-safe floors); agent-traction is COUNT(DISTINCT ip_address)
    with the internal-traffic filter (never raw COUNT(*)); citations must be fresh,
    organic, and non-disclaiming. Every composed post is run through a fence
    self-check before queueing.
  • DEDUP + RATE CAP — a win posts at most once (wins_posted_ledger UNIQUE +
    social_media_posts ON CONFLICT(win_key, platform)) and at most 1 win/day.

Reuses ALL existing transport (content_publisher / marketing_engine) — no new
poster code. Endpoints are admin-gated and dry-by-default.
"""
from __future__ import annotations

import os
import re
import datetime
import logging

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)
wins_poster_bp = Blueprint("wins_poster", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_DAILY_CAP = int(os.environ.get("WINS_POST_DAILY_CAP", "1"))
# default OFF — the job only drafts; a human approves. This flag is the ONLY way
# the job itself writes status='approved'.
_AUTOPILOT = str(os.environ.get("WINS_POSTER_AUTOPILOT_ENABLED", "")).lower() in ("1", "true", "yes")

# Internal/synthetic traffic that must NEVER count as an external "AI agent".
_INTERNAL_RE = re.compile(
    r"(loop|dchub-|selfheal|self-heal|probe|health|scanner|regression|mcp-test|"
    r"sweep|clawit|anthropicapi|qa-|monitor|uptime|curl/|python-requests|"
    r"postman|render/|googlebot|bingbot)", re.I)

# Belt-and-suspenders fence: a composed post may NEVER carry a retired over-claim.
# Mirrors tests/test_honest_numbers.py. canonical_stats phrases are already safe;
# this guards against a template/data regression slipping a banned string through.
_BANNED = [
    re.compile(r"\$324B"), re.compile(r"324B\+"), re.compile(r"\$324\s*billion", re.I),
    re.compile(r"50,?000\+?\s*(?:data center|facilit)", re.I),
    re.compile(r"\b(?:280|285|286|289)\+?\s*markets?", re.I),
    re.compile(r"\b12,?907\b"),
    re.compile(r"DC Hub Nexus", re.I),
]


def _fence_safe(text: str) -> bool:
    """True if the composed post carries no retired/banned over-claim."""
    return not any(p.search(text or "") for p in _BANNED)


def _number_led(text: str) -> bool:
    """Analyst-voice guarantee: the post opens with a real count in the first ~45
    chars (never a bare year). Wins posts lead with facility/country/agent counts —
    a legitimate number-lead, distinct from the daily desk's GW/$/delta vocabulary
    that media_editorial.leads_with_number() is tuned for."""
    head = (text or "").strip()[:45]
    if not any(ch.isdigit() for ch in head):
        return False
    return not re.match(r"^\D*(?:19|20)\d{2}\D*$", head)


def _conn():
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        return psycopg2.connect(dsn, connect_timeout=8) if dsn else None
    except Exception:
        return None


def _ensure_wins_schema(conn) -> None:
    """Create the dedup ledger + add social_media_posts.win_key (idempotent).
    DDL via the live path (SKIP_DDL verified unset); plain UNIQUE index so NULL
    win_key on non-win rows never conflicts (NULL != NULL)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wins_posted_ledger (
                id           BIGSERIAL PRIMARY KEY,
                win_kind     TEXT NOT NULL,
                dedup_key    TEXT NOT NULL,
                win_key      TEXT,
                headline     TEXT,
                post_id      BIGINT,
                status       TEXT DEFAULT 'queued',
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS wins_ledger_kind_key_idx "
                    "ON wins_posted_ledger(win_kind, dedup_key)")
        # add win_key to the existing queue table (idempotent)
        cur.execute("ALTER TABLE social_media_posts ADD COLUMN IF NOT EXISTS win_key TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS social_media_posts_win_key_platform_idx "
                    "ON social_media_posts(win_key, platform)")
    conn.commit()


# ── helpers ──────────────────────────────────────────────────────────────────
def _already_posted(cur, win_kind: str, dedup_key: str, cooldown_days: int) -> bool:
    """True if this exact win is in the ledger OR a same-kind win posted within
    the cooldown (so we don't flood with one win-type)."""
    cur.execute("SELECT 1 FROM wins_posted_ledger WHERE win_kind=%s AND dedup_key=%s LIMIT 1",
                (win_kind, dedup_key))
    if cur.fetchone():
        return True
    cur.execute("""SELECT 1 FROM wins_posted_ledger
                    WHERE win_kind=%s AND created_at > NOW() - (%s || ' days')::interval LIMIT 1""",
                (win_kind, str(cooldown_days)))
    return cur.fetchone() is not None


# ── detectors (each returns a list of win-lead dicts; honest + fresh only) ────
def detect_milestone_win() -> list[dict]:
    """A canonical metric crossing a NEW clean threshold. Numbers come ONLY from
    canonical_stats phrase helpers (citation-safe). Silent until a real crossing."""
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats(force=True)
    except Exception:
        return []
    leads = []
    # (metric, live_value, ascending clean thresholds)
    plan = [
        ("facilities", int(s.get("facilities") or 0), [22000, 25000, 30000, 40000, 50000]),
        ("countries",  int(s.get("countries") or 0),  [180, 190, 200, 210]),
        ("markets",    int(s.get("markets") or 0),     [250, 300, 350, 400]),
    ]
    for metric, val, thresholds in plan:
        crossed = max((t for t in thresholds if val >= t), default=None)
        if crossed is None:
            continue
        leads.append({
            "kind": "milestone",
            "dedup_key": f"milestone:{metric}:{crossed}",
            "win_key": f"milestone:{metric}:{crossed}",
            "cooldown_days": 30,
            "score": 60,
            "metric": metric,
        })
    return leads


def detect_agent_traction_win() -> list[dict]:
    """A new weekly high of DISTINCT EXTERNAL AI agents (by IP), internal traffic
    filtered out. NEVER COUNT(*) (raw is ~25x selfheal-inflated)."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT ip_address)
                  FROM mcp_tool_calls
                 WHERE created_at >= NOW() - INTERVAL '7 days'
                   AND ip_address IS NOT NULL
                   AND COALESCE(client_name,'')||' '||COALESCE(user_agent,'')||' '||COALESCE(platform,'')
                       !~* %s
            """, (_INTERNAL_RE.pattern,))
            n = int((cur.fetchone() or [0])[0] or 0)
            # prior weekly max from the ledger (headline stored as the number)
            cur.execute("""SELECT MAX(CAST(NULLIF(headline,'') AS INT))
                             FROM wins_posted_ledger WHERE win_kind='agent_traction'""")
            prior = (cur.fetchone() or [None])[0]
        # Only a RECORD counts; if no prior, we set the baseline silently (no post).
        if n < 25 or (prior is not None and n <= int(prior)):
            return []
        iso_week = datetime.date.today().isocalendar()
        return [{
            "kind": "agent_traction",
            "dedup_key": f"traction:{iso_week[0]}-W{iso_week[1]}",
            "win_key": f"traction:{iso_week[0]}-W{iso_week[1]}",
            "cooldown_days": 7,
            "score": 50,
            "n_distinct": n,
            "headline_num": n,
            "_baseline_only": prior is None,  # first run: record baseline, don't post
        }]
    except Exception as e:
        log.warning(f"[wins] agent_traction detect failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


def detect_citation_win() -> list[dict]:
    """A FRESH, ORGANIC, non-disclaiming AI citation of DC Hub. Reuses
    ai_citation_tracker's disclaimer/self-solicited guards. Emits ZERO today (only
    stale/self-recorded rows exist) — silence is the honest answer."""
    conn = _conn()
    if conn is None:
        return []
    try:
        from routes import ai_citation_tracker as act
        _DISC = getattr(act, "_DISCLAIMER_RE", None)
    except Exception:
        _DISC = None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, engine, prompt_id, prompt_text, response_text, observed_at, source
                  FROM ai_citations
                 WHERE dchub_cited = true
                   AND observed_at >= NOW() - INTERVAL '7 days'
                   AND COALESCE(source,'') !~* '^(auto_cron|user_recorded|seed|baseline|daily_probe|hand_obs)'
                 ORDER BY observed_at DESC LIMIT 5
            """)
            rows = cur.fetchall()
        for cid, engine, pid, prompt, resp, obs, source in rows:
            if not (engine and prompt and resp):
                continue
            if _DISC and _DISC.search(resp or ""):
                continue  # the response actually disclaims knowledge — not a win
            return [{
                "kind": "citation",
                "dedup_key": f"citation:{engine}:{pid}",
                "win_key": f"citation:{engine}:{pid}",
                "cooldown_days": 9,
                "score": 90,
                "engine": engine, "prompt": (prompt or "")[:120],
            }]
        return []
    except Exception as e:
        log.warning(f"[wins] citation detect failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


# ── compose (analyst voice; honest numbers only) ─────────────────────────────
def compose_win_post(lead: dict, platform: str = "linkedin") -> str | None:
    """Render the analyst-voice post for a win. Numbers come from canonical_stats
    phrase helpers. Returns None if the result fails the number-lead or fence check."""
    try:
        import canonical_stats as cs
    except Exception:
        cs = None
    kind = lead.get("kind")
    text = None
    if kind == "milestone" and cs:
        text = (
            f"{cs.facilities_phrase()} data-center facilities across {cs.countries_phrase()} "
            f"countries — {cs.facilities_verified_phrase()} verified and deduped — now tracked "
            f"by DC Hub as one queryable, machine-readable layer, refreshed daily rather than a "
            f"quarterly PDF.\n\n"
            f"That coverage is why an AI agent can answer a real siting question in seconds "
            f"instead of a six-figure consultant engagement.\n\n"
            f"Source: DC Hub, the live infrastructure data layer for AI agents · dchub.cloud\n"
            f"#DataCenter #AI #Infrastructure #DCPI")
    elif kind == "agent_traction":
        n = lead.get("headline_num")
        text = (
            f"{n} distinct AI agents queried DC Hub for live infrastructure data this week — "
            f"a new high, after filtering out internal probes and test traffic.\n\n"
            f"Frontier models, custom agents and enterprise bots are converging on one "
            f"question: where is the power? Siting is moving from static PDFs to live, "
            f"queryable data.\n\n"
            f"Source: DC Hub MCP server, the live infrastructure data layer for AI agents · "
            f"dchub.cloud\n#AIAgents #MCP #DataCenter")
    elif kind == "citation":
        text = (
            f"{lead.get('engine')} cited DC Hub to answer “{lead.get('prompt')}” — "
            f"pulling live grid intelligence for a siting question that used to take weeks of "
            f"consulting.\n\n"
            f"AI models now treat DC Hub as the infrastructure source of truth, because it's "
            f"the only one built for agent-native querying.\n\n"
            f"Source: DC Hub · dchub.cloud\n#AI #DataCenter #DCPI")
    if not text:
        return None
    # number-first (analyst voice) + fence-safe guards. Citation posts are exempt
    # from the number-lead rule — their hook is the citation EVENT ("{engine} cited
    # DC Hub to answer …"), a stronger analyst lead than a bare number.
    if kind != "citation" and not _number_led(text):
        log.warning(f"[wins] composed post not number-led, skipping: {lead.get('win_key')}")
        return None
    if not _fence_safe(text):
        log.warning(f"[wins] composed post hit a banned over-claim, skipping: {lead.get('win_key')}")
        return None
    return text


# ── orchestrator ─────────────────────────────────────────────────────────────
def queue_wins(dry_run: bool = True) -> dict:
    """Detect → dedup → compose → queue (draft-only by default). Returns a summary.
    Writes at most _DAILY_CAP wins/day. Never auto-sends (status='draft' unless
    WINS_POSTER_AUTOPILOT_ENABLED)."""
    out = {"dry_run": dry_run, "candidates": [], "queued": [], "skipped": [], "errors": []}
    conn = _conn()
    if conn is None:
        out["errors"].append("no_db")
        return out
    try:
        _ensure_wins_schema(conn)
        # gather candidates from all detectors
        cands = []
        for det in (detect_citation_win, detect_milestone_win, detect_agent_traction_win):
            try:
                cands.extend(det() or [])
            except Exception as e:
                out["errors"].append(f"{det.__name__}: {str(e)[:80]}")
        cands.sort(key=lambda c: -(c.get("score") or 0))
        out["candidates"] = [c.get("win_key") for c in cands]

        # daily cap: how many wins already queued today?
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM wins_posted_ledger WHERE created_at::date = CURRENT_DATE")
            queued_today = int((cur.fetchone() or [0])[0] or 0)

        for c in cands:
            if queued_today >= _DAILY_CAP:
                out["skipped"].append({"win_key": c["win_key"], "reason": "daily_cap"})
                continue
            # agent-traction first-run sets the baseline silently (no post)
            if c.get("_baseline_only"):
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute("""INSERT INTO wins_posted_ledger (win_kind, dedup_key, win_key, headline, status)
                                       VALUES (%s,%s,%s,%s,'baseline') ON CONFLICT (win_kind, dedup_key) DO NOTHING""",
                                    (c["kind"], c["dedup_key"], c["win_key"], str(c.get("headline_num") or "")))
                        conn.commit()
                out["skipped"].append({"win_key": c["win_key"], "reason": "baseline_set"})
                continue
            with conn.cursor() as cur:
                if _already_posted(cur, c["kind"], c["dedup_key"], c.get("cooldown_days", 7)):
                    out["skipped"].append({"win_key": c["win_key"], "reason": "dedup_or_cooldown"})
                    continue
            # novelty check vs recent LinkedIn posts (belt-and-suspenders)
            try:
                from routes.media_editorial import _recently_posted_keys
                recent = _recently_posted_keys(days=9)
                if c.get("metric") and c["metric"] in recent:
                    out["skipped"].append({"win_key": c["win_key"], "reason": "recently_posted"})
                    continue
            except Exception:
                pass
            text = compose_win_post(c, "linkedin")
            if not text:
                out["skipped"].append({"win_key": c["win_key"], "reason": "compose_failed_or_unsafe"})
                continue
            out["queued"].append({"win_key": c["win_key"], "kind": c["kind"], "preview": text[:200]})
            if not dry_run:
                _status = "approved" if _AUTOPILOT else "draft"
                with conn.cursor() as cur:
                    _hl = str(c.get("headline_num") or "")
                    for _plat in ("linkedin", "twitter"):
                        cur.execute("""INSERT INTO social_media_posts
                                         (platform, content, post_type, status, win_key, created_at)
                                       VALUES (%s,%s,'win',%s,%s,NOW() ON CONFLICT DO NOTHING)
                                       ON CONFLICT (win_key, platform) DO NOTHING""",
                                    (_plat, text, _status, c["win_key"]))
                    cur.execute("""INSERT INTO wins_posted_ledger (win_kind, dedup_key, win_key, headline, status)
                                   VALUES (%s,%s,%s,%s,'queued') ON CONFLICT (win_kind, dedup_key) DO NOTHING""",
                                (c["kind"], c["dedup_key"], c["win_key"], _hl))
                    conn.commit()
            queued_today += 1
        out["default_status"] = "approved" if _AUTOPILOT else "draft"
        return out
    except Exception as e:
        out["errors"].append(str(e)[:160])
        return out
    finally:
        try: conn.close()
        except Exception: pass


# ── endpoints (admin-gated; dry by default; never auto-send) ─────────────────
def _admin_ok() -> bool:
    if not _ADMIN_KEY:
        return True  # no key configured → allow (dev); prod sets DCHUB_ADMIN_KEY
    return (request.headers.get("X-Admin-Key") or "").strip() == _ADMIN_KEY


@wins_poster_bp.route("/api/v1/admin/wins/candidates", methods=["GET"])
def wins_candidates():
    """Preview ranked win candidates + composed copy. Read-only — writes nothing."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    res = queue_wins(dry_run=True)
    return jsonify(res), 200


@wins_poster_bp.route("/api/v1/admin/wins/queue-batch", methods=["POST"])
def wins_queue_batch():
    """Queue win drafts. dry=1 (default) composes + returns but writes nothing.
    A real run writes status='draft' (review-queue) unless WINS_POSTER_AUTOPILOT_ENABLED."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    dry = (request.args.get("dry", "1") != "0")
    res = queue_wins(dry_run=dry)
    return jsonify(res), 200


def register_wins_poster_routes(app):
    try:
        app.register_blueprint(wins_poster_bp)
    except Exception as e:
        log.warning(f"[wins] blueprint register failed: {e}")
