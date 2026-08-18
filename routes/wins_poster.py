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
    helpers (citation-safe floors); agent-traction is COUNT(DISTINCT agent_id)
    on the canonical mcp_calls_identity view (is_public_ip AND is_real_external —
    never session_id, never raw ip_address); citations must be fresh,
    organic, and non-disclaiming. Every composed post is run through a fence
    self-check before queueing, and (when MEDIA_FACT_CHECK_GUARD_ENABLED) the
    one-call gate_media_text corroboration gate before any row is inserted.
  • DEDUP + RATE CAP — a win posts at most once (wins_posted_ledger UNIQUE +
    social_media_posts ON CONFLICT(win_key, platform)) and at most 1 win/day.

Reuses ALL existing transport (content_publisher / marketing_engine) — no new
poster code. Endpoints are admin-gated and dry-by-default.
"""
from __future__ import annotations

from utils.anthropic_helper import cached_system
import os
import re
import datetime
import logging

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)
wins_poster_bp = Blueprint("wins_poster", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
# 2026-06-20: 1 -> 2. At cap=1 a single ship win starved genuine
# milestone/traction/citation wins (the "1M requests" milestone never posted).
# 2/day lets a real win ride alongside without flooding.
_DAILY_CAP = int(os.environ.get("WINS_POST_DAILY_CAP", "2"))
# default OFF — the job only drafts; a human approves. This flag is the ONLY way
# the job itself writes status='approved'.
_AUTOPILOT = str(os.environ.get("WINS_POSTER_AUTOPILOT_ENABLED", "")).lower() in ("1", "true", "yes")

# Belt-and-suspenders fence: a composed post may NEVER carry a retired over-claim.
# Mirrors tests/test_honest_numbers.py. canonical_stats phrases are already safe;
# this guards against a template/data regression slipping a banned string through.
_BANNED = [
    # $324B M&A aggregate — catch $324B, 324B+, "$324 billion", "324 billion dollars".
    re.compile(r"\$?\s?324\s?B\b", re.I),
    re.compile(r"\b324[\s-]*billion", re.I),
    # 50,000 facilities — catch space/hyphen/no-sep variants ("50,000+ data-center",
    # "50,000-facilities", "50000 sites").
    re.compile(r"\b50[,.]?000\+?[\s-]*(?:data[\s-]?center|facilit|site)", re.I),
    # market over-claims — 280/285/286/289 AND 340+ (canon is ~232-300); cover hyphen.
    re.compile(r"\b(?:280|285|286|289|3[4-9]\d)\+?[\s-]*markets?", re.I),
    re.compile(r"\b12[,.]?907\b"),
    re.compile(r"DC Hub Nexus", re.I),
]


def _fence_safe(text: str) -> bool:
    """True if the composed post carries no retired/banned over-claim."""
    return not any(p.search(text or "") for p in _BANNED)


def _year_led(text: str) -> bool:
    """True if the post opens with a bare year (e.g. "2026 was the year …").
    A year is never an honest analyst lead — ships must hook on the capability,
    not the calendar. Used to reject year-led ship drafts even though ships are
    otherwise exempt from the strict number-lead rule."""
    head = (text or "").strip()
    return bool(re.match(r"^(?:19|20)\d{2}\b", head))


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


# ── composition-time honesty gate (r-media-canon-gate, 2026-07-02) ────────────
def _media_gate_denies(text: str, platform: str = "linkedin") -> tuple[bool, list]:
    """Run the one-call pre-queue gate (routes.media_fact_check_guard.
    gate_media_text) BEFORE a win row is inserted — the corroboration pass the
    session-inflated agent-count post never went through. Activation follows
    the guard module's own kill-switch (MEDIA_FACT_CHECK_GUARD_ENABLED, default
    OFF) so rollout is a Railway env flip, not a deploy.

    Returns (denied, reasons). With the flag ON, a gate crash fails CLOSED.
    With the guard module unimportable it fails OPEN only because the
    content_publisher drain gate still re-checks at publish time."""
    try:
        from routes.media_fact_check_guard import _enabled, gate_media_text
    except Exception as e:
        log.warning(f"[wins] fact-check guard unavailable: {str(e)[:120]}")
        return False, []
    try:
        if not _enabled():
            return False, []
    except Exception:
        return False, []
    # Own AUTOCOMMIT connection for the guard's dedup/quality SELECTs — never
    # the shared queue_wins transaction, so a failed guard query can't abort
    # the pending ledger/queue INSERTs (the shared-tx poison trap).
    conn = _conn()
    cur = None
    if conn is not None:
        try:
            conn.autocommit = True
            cur = conn.cursor()
        except Exception:
            cur = None
    try:
        res = gate_media_text(cur, text or "", platform)
        return (not res.get("allow", False)), list(res.get("reasons") or [])
    except Exception as e:
        return True, [f"gate raised — failing closed ({str(e)[:100]})"]
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


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
    """A new weekly high of DISTINCT EXTERNAL AI agents, read from the CANONICAL
    identity view mcp_calls_identity (agent = md5 of first public XFF token,
    is_real_external filters probe/self/scripted-UA traffic). NEVER a raw
    COUNT(*) (~25x selfheal-inflated), never sessions (they rotate per
    connection), never a locally-filtered raw-IP distinct count (the old
    regex denylist read 30+ where the canonical count was ~14)."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT agent_id)
                  FROM mcp_calls_identity
                 WHERE created_at >= NOW() - INTERVAL '7 days'
                   AND is_public_ip AND is_real_external
            """)
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


# ── ship-win detection (real shipped capabilities, not git noise) ────────────
# A SHIP is a feature/capability we actually built and published. The honest,
# runtime-available source is the press_releases table — the canonical changelog
# the public /changelog feed reads (NOT git history; Railway has no git binary).
# We only narrate a ship that carries a CONCRETE, quantified capability; vague
# marketing copy is rejected so the analyst never over-claims.

# Words that signal a real, specific shipped capability (concrete > promo).
_SHIP_CAPABILITY_RE = re.compile(
    r"\b(added|launched|shipped|wired|integrated|now\s+(?:live|tracks?|covers?|surfaces?)|"
    r"new\s+(?:tool|endpoint|api|layer|dataset|feed|detector|score|index)|"
    r"real[- ]time|live\s+(?:data|grid|power|fiber|gas)|per[- ]facility|"
    r"\d+\s*(?:tools?|markets?|isos?|regions?|facilit|endpoints?|layers?|datasets?|metrics?))",
    re.I)

# Promo/vague language that, if it's the ONLY hook, means the ship is not
# newsworthy as an honest analyst post. (Distinct from _BANNED over-claims —
# these just aren't specific enough to narrate.)
_SHIP_VAGUE_RE = re.compile(
    r"\b(revolutioni[sz]|game[- ]chang|unlock(?:ed|s)?\s+ai|supercharg|"
    r"transform(?:ed|s)?\s+(?:the\s+)?(?:industry|everything)|next[- ]gen|"
    r"reimagin|disrupt)", re.I)


def _extract_ship_metric(title: str, body: str) -> str | None:
    """Return a short, concrete capability snippet from a ship's title+body, or
    None if the ship has no honest, specific hook. Never invents a number — it
    only surfaces text the ship actually shipped. A vague/promo-only ship → None
    so we stay silent rather than over-claim."""
    title = (title or "").strip()
    blob = f"{title}\n{(body or '')[:600]}".strip()
    if not blob:
        return None
    # If the only hook is hype with no concrete capability, do not post.
    if _SHIP_VAGUE_RE.search(blob) and not _SHIP_CAPABILITY_RE.search(blob):
        return None
    if not _SHIP_CAPABILITY_RE.search(blob):
        return None
    # Prefer the title as the snippet (changelog titles are already capability-led);
    # fall back to the first concrete sentence of the body.
    if _SHIP_CAPABILITY_RE.search(title) and len(title) >= 12:
        return title[:160]
    for sent in re.split(r"(?<=[.!?])\s+", (body or "")):
        if _SHIP_CAPABILITY_RE.search(sent):
            return sent.strip()[:160]
    return title[:160] if len(title) >= 12 else None


def _draft_ship_post_llm(title: str, metric: str) -> str | None:
    """Draft an analyst-voice ship post via the brain LLM (value/benefit framing
    for data-center / AI-infra buyers). Honest by construction: the model is told
    to use ONLY the provided capability and to invent NO numbers. Returns the
    drafted text, or None if the LLM is unreachable (caller falls back to a
    deterministic template). The caller still fences/number-checks the result."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from utils.anthropic_helper import anthropic_messages_url
        from routes.media_editorial import ANALYST_VOICE
        from routes.brain_models import brain_model_for
        from urllib.request import Request, urlopen
        import json as _json
    except Exception:
        return None
    prompt = (
        "DC Hub just SHIPPED a new capability. Write ONE LinkedIn post (an analyst "
        "reporting DC Hub's own shipment) that frames the VALUE for data-center and "
        "AI-infrastructure buyers — site-selection leads, capacity planners, developers. "
        "Frame the benefit (faster siting, better capex/interconnection decisions), not "
        "the engineering. Lead the first sentence with a concrete count or capability — "
        "NEVER a bare year. Use ONLY the capability below; invent NO numbers, markets, "
        "MW, or companies. If the capability has no number, lead with the capability itself.\n\n"
        f"SHIPPED CAPABILITY: {metric}\n"
        f"CHANGELOG TITLE: {title}\n\n"
        "Return ONLY the post text (700-1500 chars), ending with this exact source line "
        "on its own line: Source: DC Hub, the live infrastructure data layer for AI agents "
        "· dchub.cloud"
    )
    body = _json.dumps({
        "model": brain_model_for("voice"),
        "max_tokens": 900,
        "system": cached_system(ANALYST_VOICE),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    try:
        req = Request(anthropic_messages_url(), data=body, headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "User-Agent": "dchub-brain/1.0",
            "Anthropic-Version": "2023-06-01",
        })
        with urlopen(req, timeout=30) as r:
            data = _json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        return text or None
    except Exception as e:
        log.warning(f"[wins] ship LLM draft failed: {str(e)[:100]}")
        return None


def detect_ship_win() -> list[dict]:
    """Detect recent SHIPS (real shipped capabilities) from the canonical
    press_releases changelog feed — the source the backend actually has at
    runtime. Only narrates a ship with a concrete, honest capability hook; the
    LLM draft (analyst voice, value framing) is carried on the lead so the
    composer/fence run on the SAME text the LLM produced. Stays silent (returns
    []) when there is no genuine, fence-safe ship to report."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, slug, title, body
                  FROM press_releases
                 WHERE COALESCE(published, false) = true
                   AND COALESCE(published_at, created_at) >= NOW() - INTERVAL '7 days'
                 ORDER BY COALESCE(published_at, created_at) DESC
                 LIMIT 12
            """)
            rows = cur.fetchall()
    except Exception as e:
        log.warning(f"[wins] ship detect query failed: {e}")
        try: conn.close()
        except Exception: pass
        return []
    try: conn.close()
    except Exception: pass

    leads: list[dict] = []
    seen: set[str] = set()
    for ship_id, slug, title, body in rows:
        slug = slug or f"id{ship_id}"
        # 2026-06-20: DCPI market-stat releases are ALREADY the formulaic feed
        # (linkedin_quad dcpi_mover slot). Excluding them here stops them from
        # being re-narrated as "ship wins" and crowding out genuine
        # citation/milestone/traction updates in the wins feed.
        _haystack = f"{slug} {title or ''}".lower()
        if any(k in _haystack for k in ("dcpi", "power index", "market mover", "dcpi mover")):
            continue
        metric = _extract_ship_metric(title, body)
        if not metric:
            continue  # no concrete capability → not an honest, postable ship
        if slug in seen:
            continue
        seen.add(slug)
        # Draft analyst-voice text NOW so compose_win_post fences the real draft.
        draft = _draft_ship_post_llm(title or metric, metric)
        leads.append({
            "kind": "ship",
            "dedup_key": f"ship:{ship_id}:{slug}",
            "win_key": f"ship:{ship_id}:{slug}",
            "cooldown_days": 14,   # don't flood ship posts; ~2wk between ships
            "score": 68,           # between milestone (60) and citation (90)
            "ship_id": ship_id,
            "ship_title": title or metric,
            "ship_slug": slug,
            "_metric_snippet": metric,
            "_llm_draft": draft,   # may be None → composer uses a safe template
        })
    return leads


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
            # ★2026-08-17: led with facilities_phrase() = COUNT(*) ROWS. The
            # composer re-voices this lead and drops trailing qualifiers, so on
            # 08-17 it published "26,000 data-center facilities ... up from the
            # 18,000+" — the raw pile presented as buildings, and the ~1.4x
            # dedup ratio presented as GROWTH over the number that is actually
            # correct. Lead with distinct buildings, exactly as
            # ai_surface_canon does (see its facilities_verified_phrase note):
            # rows are not facilities, and the qualifier cannot be relied on to
            # survive re-voicing.
            f"{cs.facilities_verified_phrase()} data-center facilities across {cs.countries_phrase()} "
            f"countries — distinct buildings, deduped from {cs.facilities_phrase()} source records — now tracked "
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
        # 2026-06-20: replaced the sweeping over-claim ("AI models now treat DC
        # Hub as THE source of truth") — unsupported (one engine, one prompt) —
        # with the specific, defensible fact: THIS engine cited us for THIS
        # question, and what agents actually get when they query.
        text = (
            f"{lead.get('engine')} cited DC Hub to answer “{lead.get('prompt')}” — "
            f"pulling live grid intelligence for a siting question that used to take weeks of "
            f"consulting.\n\n"
            f"When an agent queries DC Hub it gets cited, machine-readable infrastructure data "
            f"— live power, grid, fiber and tenants — instead of a static PDF.\n\n"
            f"Source: DC Hub · dchub.cloud\n#AI #DataCenter #DCPI")
    elif kind == "ship":
        # Prefer the LLM-drafted analyst-voice post (value framing) when it's
        # present AND passes the same number-led + fence guards below. Otherwise
        # fall back to a deterministic, honest template built ONLY from the real
        # capability snippet the ship shipped (never an invented number).
        draft = (lead.get("_llm_draft") or "").strip()
        metric = (lead.get("_metric_snippet") or "").strip()
        # Use the LLM draft only if it's NOT year-led and is fence-safe. A ship's
        # honest hook is the capability, so a numeric lead isn't required — but a
        # bare-year lead or a banned figure means we fall back to the template.
        if draft and not _year_led(draft) and _fence_safe(draft):
            text = draft
        elif metric:
            text = (
                f"{metric} — now live in DC Hub.\n\n"
                f"For teams siting AI infrastructure, that's one more decision answered "
                f"from live data instead of a stale PDF or a six-figure consulting cycle: "
                f"buyers can query it the moment they're weighing a site.\n\n"
                f"Each shipped capability compounds — the agents and developers already "
                f"querying DC Hub inherit it automatically, with no re-integration.\n\n"
                f"Source: DC Hub, the live infrastructure data layer for AI agents · "
                f"dchub.cloud\n#DataCenter #AI #Infrastructure")
    if not text:
        return None
    # number-first (analyst voice) + fence-safe guards. Citation AND ship posts
    # are exempt from the number-lead rule — their hook is an EVENT ("{engine}
    # cited DC Hub…", "X is now live"), a stronger analyst lead than a bare count.
    # The fence (no fabricated/over-claimed figures) is the honesty guard that
    # matters for ships; a year-led ship draft is still rejected below.
    if kind not in ("citation", "ship") and not _number_led(text):
        log.warning(f"[wins] composed post not number-led, skipping: {lead.get('win_key')}")
        return None
    if not _fence_safe(text):
        log.warning(f"[wins] composed post hit a banned over-claim, skipping: {lead.get('win_key')}")
        return None
    # r-media-canon-gate (2026-07-02): the one-call corroboration gate
    # (gate_media_text), run on the ANALYST BODY — before the CTA append below,
    # same placement as the fence/number-lead guards, because the canonical CTA
    # is a constant, known fence-safe line whose "$10 = 1,000 calls" figure
    # would otherwise trip the dollar-aggregate fail-closed check on every
    # post. On denial: ONE log line + drop the post (never a stripped version).
    _denied, _greasons = _media_gate_denies(text, platform)
    if _denied:
        log.warning(f"[wins] media gate denied {lead.get('win_key')}: "
                    f"{'; '.join(_greasons)[:400]}")
        return None
    # Item 8 (2026-06-30): append the one canonical reach CTA so every win post
    # ends with a connect line ("free, call claim_free_key · $10 = 1,000 calls").
    # Appended AFTER the fence/number-lead guards so those still validate the
    # analyst body; the CTA itself is known fence-safe. Fail-soft: if the shared
    # module can't import, the post still ships without the CTA.
    try:
        from media_cta import append_reach_cta
        text = append_reach_cta(text, short=(platform in ("twitter", "x", "bluesky")))
    except Exception:
        pass
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
        for det in (detect_citation_win, detect_ship_win, detect_milestone_win, detect_agent_traction_win):
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
            # novelty check vs recent LinkedIn posts (belt-and-suspenders).
            # 2026-06-20: EXEMPT milestone/citation/agent_traction. Their metric
            # is a bare recurring word ("requests", "facilities", "agents") that
            # routinely appears in other posts, so this check silently killed
            # every milestone (the "1M requests" win never posted). Those kinds
            # are already deduped by their own dedup_key + cooldown above; only
            # ship wins (topic-repeat risk) still get the bare-metric guard.
            try:
                from routes.media_editorial import _recently_posted_keys
                if (c["kind"] not in ("milestone", "citation", "agent_traction")
                        and c.get("metric")
                        and c["metric"] in _recently_posted_keys(days=9)):
                    out["skipped"].append({"win_key": c["win_key"], "reason": "recently_posted"})
                    continue
            except Exception:
                pass
            # r-media-canon-gate (2026-07-02): compose_win_post now also runs
            # the one-call gate_media_text corroboration gate on the analyst
            # body (pre-CTA) and returns None on denial, so no unproven copy
            # ever reaches the INSERTs below.
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
                                       VALUES (%s,%s,'win',%s,%s,NOW())
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
