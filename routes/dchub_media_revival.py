"""Phase RRRR (2026-05-16) — DC Hub Media revival.

User question: "is DC Hub Media telling everyone?"
Honest answer: NO. /api/v1/press-releases/list returned 0 items.
Source-of-truth score is 10/100 — "invisible." The infrastructure
exists (newsroom-auto cron 3h, marketing_auto_press, agent_vendor_
digest weekly) but the OUTPUT side is broken.

This module:
  1. New brain detector `dchub_media_press_silent` — fires if no
     press release in last 7 days
  2. New brain detector `source_of_truth_critical` — fires when
     score < 20 (current is 10)
  3. New endpoint /api/v1/media/winback-pitches — per-AI-platform
     email templates based on dormant agent UAs, ready to copy/paste
  4. Both detectors map to autopilot patterns that AUTO-TRIGGER
     /api/v1/marketing/auto-generate (not escalation-only — this
     is the "wake up DC Hub Media" move the user asked for)
"""

from __future__ import annotations

import os
import re
import datetime
from flask import Blueprint, jsonify, request


dchub_media_revival_bp = Blueprint("dchub_media_revival", __name__)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# Fix (2026-07-04): platform classification now uses the CANONICAL
# MCP_PLATFORM_MAP (main.py) — the same map every other reach/attribution metric
# uses — instead of a private ad-hoc regex list that classified ALL 50 dormant
# agents as "Unidentified" (winback channel stuck at 20/100 = platform_count 1).
# The real signal for MCP agents lives in the recovered `platform` column +
# clientInfo name, NOT the raw UA (which is usually '@modelcontextprotocol/sdk'
# or 'node'), so we resolve from the identity view's platform/client_name first
# and only fall back to the UA string.

# Fallback copy of the canonical map — used ONLY if `from main import
# MCP_PLATFORM_MAP` fails (circular-import guard / test harness). Keep in sync
# with main.py:MCP_PLATFORM_MAP.
_MCP_PLATFORM_MAP_FALLBACK = {
    'claude': 'Claude', 'claude-desktop': 'Claude', 'anthropic': 'Claude',
    'chatgpt': 'ChatGPT', 'openai': 'ChatGPT',
    'grok': 'Grok', 'xai': 'Grok',
    'gemini': 'Gemini', 'google': 'Gemini',
    'perplexity': 'Perplexity',
    'cursor': 'Cursor', 'copilot': 'Copilot',
    'windsurf': 'Windsurf', 'cline': 'Cline',
    'groq': 'Groq', 'deepseek': 'DeepSeek',
    'mistral': 'Mistral', 'le chat': 'Mistral', 'lechat': 'Mistral',
    'cohere': 'Cohere', 'poe': 'Poe', 'youcom': 'You.com',
    'huggingface': 'Hugging Face', 'hf': 'Hugging Face', 'hugging face': 'Hugging Face',
    'base44': 'base44',
}


def _mcp_platform_map() -> dict:
    try:
        from main import MCP_PLATFORM_MAP
        if MCP_PLATFORM_MAP:
            return MCP_PLATFORM_MAP
    except Exception:
        pass
    return _MCP_PLATFORM_MAP_FALLBACK


# Canonical display-platform → (contact_url, pitch_angle) for the winback email.
_PLATFORM_CONTACT = {
    "Claude":       ("https://www.anthropic.com/contact-sales",
                     "Add DC Hub MCP to the Claude registry / connector directory so direct connectors discover us"),
    "ChatGPT":      ("https://openai.com/contact-sales",
                     "List DC Hub in the GPT Store + wire it as a ChatGPT connector for data-center queries"),
    "Grok":         ("https://x.ai/",
                     "Formalize DC Hub as a Grok data source for infrastructure questions"),
    "Gemini":       ("https://cloud.google.com/contact",
                     "Submit DC Hub to Vertex AI agent garden + Gemini extensions"),
    "Perplexity":   ("https://www.perplexity.ai/hub/blog",
                     "Apply for Perplexity's source-citation program — the call volume proves data quality"),
    "Cursor":       ("https://cursor.sh/contact",
                     "List DC Hub in Cursor's MCP server directory for data-center workflows"),
    "Copilot":      ("https://github.com/features/copilot",
                     "List DC Hub as a Copilot extension / MCP server for infra tooling"),
    "Windsurf":     ("https://codeium.com/contact",
                     "Add DC Hub to the Windsurf MCP directory"),
    "Cline":        ("https://cline.bot/",
                     "List DC Hub in the Cline MCP marketplace"),
    "Groq":         ("https://groq.com/contact/",
                     "Wire DC Hub as a Groq tool/function data source"),
    "DeepSeek":     ("https://www.deepseek.com/",
                     "Formalize DC Hub as a DeepSeek data connector"),
    "Mistral":      ("https://mistral.ai/contact/",
                     "Add DC Hub to Le Chat's connector catalog"),
    "Cohere":       ("https://cohere.com/contact-sales",
                     "Register DC Hub as a Cohere tool-use data source"),
    "Poe":          ("https://poe.com/",
                     "Publish a DC Hub bot / server on Poe"),
    "You.com":      ("https://you.com/",
                     "Add DC Hub as a You.com source / plugin"),
    "Hugging Face": ("https://huggingface.co/support",
                     "List DC Hub in the HF MCP / Spaces directory"),
    "base44":       ("https://base44.com/",
                     "Formalize the base44 → DC Hub integration"),
}
_DEFAULT_CONTACT = ("n/a",
                    "Unidentified — add a platform/clientInfo/UA mapping to MCP_PLATFORM_MAP (main.py)")


def _canon_platform(*signals: str) -> str:
    """Resolve a canonical display-platform from the given signals (recovered
    platform column, MCP clientInfo name, raw UA), tried in order. Each is
    matched exact-then-substring against the canonical MCP_PLATFORM_MAP keys.
    Returns 'Unidentified AI platform' when nothing matches."""
    pmap = _mcp_platform_map()
    _JUNK = {"", "mcp", "unknown", "anonymous", "internal-dchub",
             "n/a", "none", "null"}
    for sig in signals:
        s = (sig or "").strip().lower()
        if not s or s in _JUNK:
            continue
        if s in pmap:
            return pmap[s]
        for key, disp in pmap.items():
            if key and key in s:
                return disp
    return "Unidentified AI platform"


def _classify_ua(ua: str) -> tuple[str, str, str]:
    """UA-only classification (kept for backward compat — imported by
    routes/winback_outreach.py). Delegates to the canonical map now."""
    platform = _canon_platform(ua)
    contact, angle = _PLATFORM_CONTACT.get(platform, _DEFAULT_CONTACT)
    return (platform, contact, angle)


def _dormant_by_platform(min_prior_calls: int = 30, idle_days: int = 14,
                         look_back_days: int = 90) -> list:
    """Dormant REAL-external agents grouped from the canonical identity view
    (mcp_calls_identity), carrying the recovered platform + clientInfo name so
    classification matches every other reach metric. One row per agent_id
    (md5 of the first public XFF token). is_real_external already excludes
    python/curl/node/internal probe traffic, so this is the genuine winback
    cohort. Returns [] on any error — the caller falls back to the UA-only path."""
    c = _conn()
    if c is None:
        return []
    rows = []
    try:
        with c.cursor() as cur:
            cur.execute(f"""
                WITH agg AS (
                  SELECT agent_id,
                         COUNT(*)                  AS prior_calls,
                         MAX(created_at)           AS last_call,
                         MIN(created_at)           AS first_call,
                         COUNT(DISTINCT tool_name) AS distinct_tools,
                         MAX(user_agent)           AS sample_ua,
                         mode() WITHIN GROUP (ORDER BY LOWER(COALESCE(platform,'')))    AS modal_platform,
                         mode() WITHIN GROUP (ORDER BY LOWER(COALESCE(client_name,''))) AS modal_client
                    FROM mcp_calls_identity
                   WHERE created_at >= NOW() - INTERVAL '{int(look_back_days)} days'
                     AND is_public_ip
                     AND is_real_external
                     AND agent_id IS NOT NULL  -- CF-POP rows carry NULL agent_id (grain guard 2026-07-19)
                   GROUP BY agent_id
                )
                SELECT agent_id, prior_calls, last_call, first_call,
                       distinct_tools, sample_ua, modal_platform, modal_client
                  FROM agg
                 WHERE prior_calls >= %s
                   AND last_call < NOW() - INTERVAL '{int(idle_days)} days'
                 ORDER BY prior_calls DESC
                 LIMIT 200
            """, (int(min_prior_calls),))
            rows = cur.fetchall() or []
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass

    now = datetime.datetime.now(datetime.timezone.utc)
    out: list = []
    for r in rows:
        (agent_id, prior_calls, last_call, first_call,
         distinct_tools, sample_ua, modal_platform, modal_client) = r
        days_idle = None
        if last_call:
            lc = last_call
            if lc.tzinfo is None:
                lc = lc.replace(tzinfo=datetime.timezone.utc)
            days_idle = round((now - lc).total_seconds() / 86400.0, 1)
        platform = _canon_platform(modal_platform, modal_client, sample_ua)
        out.append({
            "ip_hash":        (agent_id or "?")[:12],
            "ua_fingerprint": (sample_ua or "")[:80],
            "platform":       platform,
            "prior_calls":    int(prior_calls or 0),
            "distinct_tools": int(distinct_tools or 0),
            "days_idle":      days_idle,
            "last_call_at":   last_call.isoformat() if last_call else None,
        })
    return out


def _last_press_age_days() -> tuple[float | None, int]:
    """Returns (days_since_last_press, count_30d). Tolerates missing
    table by returning (None, 0). Tries auto_press_releases first
    (the autonomous writer), falls back to press_releases."""
    c = _conn()
    if c is None: return None, 0
    try:
        with c.cursor() as cur:
            for table, date_col in (("auto_press_releases", "generated_for"),
                                     ("auto_press_releases", "created_at"),
                                     ("press_releases", "published_at"),
                                     ("press_releases", "published_date"),
                                     ("press_releases", "created_at")):
                try:
                    cur.execute(f"SELECT to_regclass('public.{table}')")
                    if not (cur.fetchone() or [None])[0]: continue
                    cur.execute(f"""
                        SELECT EXTRACT(EPOCH FROM (NOW() - MAX({date_col})))/86400.0,
                               COUNT(*) FILTER (WHERE {date_col} >= NOW() - INTERVAL '30 days')
                          FROM {table}
                    """)
                    r = cur.fetchone()
                    if r and r[0] is not None:
                        return float(r[0]), int(r[1] or 0)
                except Exception:
                    continue
    finally:
        try: c.close()
        except Exception: pass
    return None, 0


@dchub_media_revival_bp.route("/api/v1/media/press-health", methods=["GET"])
def press_health():
    """Public — DC Hub Media output health. Used by /transparency."""
    age, count_30d = _last_press_age_days()
    sot = None
    try:
        from routes.media_pulse import _compute_source_of_truth  # if exposed
        sot = _compute_source_of_truth().get("score")
    except Exception:
        try:
            import requests
            r = requests.get("http://localhost:8080/api/v1/media/source-of-truth",
                              timeout=2)
            if r.status_code == 200:
                sot = r.json().get("score")
        except Exception: pass
    resp = jsonify({
        "days_since_last_press":  round(age, 1) if age is not None else None,
        "press_releases_30d":     count_30d,
        "source_of_truth_score":  sot,
        "verdict": (
            "silent"  if age is None or age > 7 else
            "weak"    if count_30d < 4 else
            "healthy"
        ),
        "generated_at":           datetime.datetime.utcnow().isoformat() + "Z",
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@dchub_media_revival_bp.route("/api/v1/media/winback-pitches", methods=["GET"])
def winback_pitches():
    """Public worklist. For each dormant agent (>=30 prior calls,
    idle 14+ days), classify by UA and emit a per-platform pitch
    template. DC Hub Media can copy/paste these for outbound."""
    # Fix (2026-07-04): prefer the identity-view sourced dormant list (carries
    # the recovered platform + clientInfo) so agents classify into REAL
    # platforms; fall back to the UA-only bot_outreach list only if that query
    # yields nothing.
    dormant = _dormant_by_platform(min_prior_calls=30, idle_days=14)
    _source = "mcp_calls_identity"
    if not dormant:
        try:
            from routes.bot_outreach import _compute_dormant
            dormant = _compute_dormant(min_prior_calls=30, idle_days=14) or []
        except Exception:
            dormant = []
        _source = "bot_outreach_ua_only"

    # Group by platform
    by_platform: dict = {}
    for a in dormant:
        ua = (a.get("ua_fingerprint") or "")[:200]
        platform = a.get("platform")
        if platform:
            contact, angle = _PLATFORM_CONTACT.get(platform, _DEFAULT_CONTACT)
        else:
            platform, contact, angle = _classify_ua(ua)
        b = by_platform.setdefault(platform, {
            "platform":         platform,
            "contact":          contact,
            "pitch_angle":      angle,
            "dormant_count":    0,
            "total_prior_calls": 0,
            "sample_uas":       [],
            "max_prior_calls":  0,
        })
        b["dormant_count"]    += 1
        b["total_prior_calls"] += int(a.get("prior_calls") or 0)
        b["max_prior_calls"]   = max(b["max_prior_calls"], int(a.get("prior_calls") or 0))
        if len(b["sample_uas"]) < 3:
            b["sample_uas"].append(ua[:60])

    pitches = sorted(by_platform.values(),
                     key=lambda p: -p["total_prior_calls"])

    # Generate a templated email pitch per platform
    for p in pitches:
        p["email_subject"] = (
            f"DC Hub: {p['total_prior_calls']:,} calls from "
            f"{p['platform']} — let's formalize the integration"
        )
        p["email_body"] = (
            f"Hi {p['platform']} team,\n\n"
            f"DC Hub (https://dchub.cloud) is the live, MCP-native "
            f"data-center intelligence platform. We've observed "
            f"{p['total_prior_calls']:,} MCP calls from agents matching "
            f"{p['platform']} signatures over the last 90 days — that's "
            f"strong organic adoption, but the agents went dormant "
            f"14+ days ago.\n\n"
            f"{p['pitch_angle']}.\n\n"
            f"What we'd like to discuss:\n"
            f"- Formal listing in your registry / directory\n"
            f"- Co-marketing case study on data-center site selection\n"
            f"- Joint webinar for hyperscale / colo decision-makers\n\n"
            f"Try our MCP server: https://dchub.cloud/mcp\n"
            f"Brand positioning: https://dchub.cloud/vs\n"
            f"Live ops dashboard: https://dchub.cloud/transparency\n\n"
            f"— DC Hub team\napi@dchub.cloud"
        )

    resp = jsonify(
        pitches=pitches,
        source=_source,
        platform_count=len(pitches),
        total_dormant_agents=sum(p["dormant_count"] for p in pitches),
        total_dormant_calls=sum(p["total_prior_calls"] for p in pitches),
        note=("Copy-paste-ready outbound pitches for the dormant-agent "
              "winback campaign. Maps UA fingerprints from "
              "/api/v1/bots/dormant to the originating AI platform + "
              "suggested contact URL + email template. Send via the "
              "platform's contact form or whatever channel your DC Hub "
              "Media playbook uses."),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    )

    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ── r30 (2026-05-24): consolidated media pulse ────────────────────
#
# One endpoint that rolls press cadence + LinkedIn velocity + winback
# pitch count into a single health verdict. Consumed by the
# /api/v1/sentinel/sweep rollup and the /transparency UI so the
# operator stops needing to mentally compose 3 separate endpoints.

# Thresholds for the LinkedIn publisher verdict. Env-tunable so the operator
# can retune without a deploy — but note that RAISING these hides an outage,
# which is exactly how this surface came to report healthy through one.
def _envint(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)) or default))
    except Exception:
        return default


def linkedin_publisher_verdict(quad: dict,
                               press_crosspost_24h: int = 0,
                               press_crosspost_7d: int = 0) -> dict:
    """PURE. Roll a linkedin_quad_posts snapshot into a component verdict.

    `quad` keys (all optional; a failed read is an empty dict):
        hours_since_success, published_24h, published_7d,
        attempts_7d, with_image_7d, gate_blocked_3d,
        abandoned_claims_7d, suppressed_7d

    Verdicts, worst first — each maps to a way this publisher has ACTUALLY
    broken in production, not to a generic cadence band:
        silent    nothing published recently (or ever)
        degraded  posts ship but carry no card, OR the gate keeps refusing
                  whatever the desk keeps electing (the selector deadlock)
        weak      publishing, below the cadence floor
        healthy   publishing at cadence, with cards

    An empty `quad` reads as SILENT, deliberately: "I could not measure the
    publisher" must never present as "the publisher is fine". That inversion is
    the specific bug this function replaces."""
    hrs      = quad.get("hours_since_success")
    pub7     = int(quad.get("published_7d") or 0)
    pub24    = int(quad.get("published_24h") or 0)
    img7     = int(quad.get("with_image_7d") or 0)
    blocked  = int(quad.get("gate_blocked_3d") or 0)
    aband7   = int(quad.get("abandoned_claims_7d") or 0)
    supp7    = int(quad.get("suppressed_7d") or 0)
    # 2026-08-16: was 7, against a 4-slots/day = 28/wk cadence. A floor of 7
    # lets the publisher lose 75% of its slots and still read "healthy" — and
    # it did exactly that, reading healthy through a window with 8 stranded
    # claims. 21 = 75% of cadence: tolerant of a few missed slots, not of a
    # quarter-strength feed.
    floor7   = _envint("MEDIA_LINKEDIN_WEEKLY_FLOOR", 21)
    # 4 slots/day, so 36h of silence is six missed slots — past any bad slot.
    stale    = hrs is None or hrs > _envint("MEDIA_LINKEDIN_STALE_HOURS", 36)
    # Cards dead: real posts went out and NOT ONE carried an image.
    cardless = pub7 >= 3 and img7 == 0
    # Deadlock: repeated gate refusals with nothing getting through behind them.
    jammed   = blocked >= _envint("MEDIA_LINKEDIN_JAM_BLOCKS", 4) and pub24 == 0
    # ★ Slots that claimed and then vanished. Distinct from every band above:
    # nothing was refused and nothing was published — the run died mid-flight,
    # which reads as "in progress" forever rather than as a failure.
    stranded = aband7 >= _envint("MEDIA_LINKEDIN_STRANDED_CLAIMS", 3)
    reasons = []
    if stale:
        reasons.append(f"no successful post in {hrs:.0f}h" if hrs is not None
                       else "no successful post on record")
    if cardless:
        reasons.append(f"{pub7} posts in 7d and none carried a card "
                       "— the image upload is failing")
    if jammed:
        reasons.append(f"{blocked} publish-gate refusals in 3d with nothing "
                       "published in 24h — the desk is re-electing a lead the "
                       "gate keeps refusing")
    if stranded:
        reasons.append(f"{aband7} slots claimed then abandoned in 7d — the run "
                       "died between claim and publish, so the row still reads "
                       "'claimed_in_flight' and the slot produced nothing")
    if not stale and pub7 < floor7:
        _floor_reason = f"{pub7} posts in 7d against a {floor7}/wk floor"
        # Suppression is the desk doing its job (event-driven cadence), so it
        # never degrades on its own — but when the floor IS missed, name how
        # much of the gap was chosen silence: that redirects the operator to
        # lead supply instead of a publisher hunt.
        if supp7:
            _floor_reason += (f" — {supp7} of the lost slots were deliberate "
                              "editorial suppressions (no novel event): a "
                              "lead-supply gap, not a publisher crash")
        reasons.append(_floor_reason)
    return {
        "hours_since_last_publish": hrs,
        "published_24h":       pub24,
        "published_7d":        pub7,
        "attempts_7d":         int(quad.get("attempts_7d") or 0),
        "with_image_7d":       img7,
        "gate_blocked_3d":     blocked,
        "abandoned_claims_7d": aband7,
        "suppressed_7d":       supp7,
        "press_crosspost_24h": press_crosspost_24h,
        "press_crosspost_7d":  press_crosspost_7d,
        "reasons": reasons,
        "verdict": (
            "silent"   if stale                          else
            "degraded" if cardless or jammed or stranded else
            "weak"     if pub7 < floor7                  else
            "healthy"
        ),
    }


# Worst-first. ★ 2026-08-17: 'silent' and 'degraded' used to SHARE rank 3 —
# see rollup_verdict for what that cost.
_SEVERITY_RANK = {"silent": 4, "degraded": 3, "weak": 2, "quiet": 1, "healthy": 0}

# The engagement sync runs daily (14:40 UTC), so 48h tolerates one missed run
# before the readback counts as stale. The 6h grace stops a post from being
# called "unmeasured" before the platform has had time to report on it at all.
_READBACK_STALE_H = _envint("MEDIA_READBACK_STALE_HOURS", 48)
_READBACK_GRACE_H = _envint("MEDIA_READBACK_GRACE_HOURS", 6)


def engagement_readback_verdict(age_hours, unmeasured: int,
                                measurable: int) -> dict:
    """PURE. Can we still READ BACK what we published?

    ★★★ 2026-08-18 — THIS PULSE COULD NOT SEE TWO WEEKS OF BLINDNESS.
    Every component here is supply-side (published_7d, attempts_7d,
    with_image_7d, hours_since_last_publish), so the endpoint answered "weak —
    19 posts against a 21/wk floor" while the last successful engagement fetch
    was 2026-08-05 and 53 posts carried NULL impressions/likes/comments. The
    publisher was fine; the FEEDBACK LOOP was dead, and the surface whose whole
    job is media health had no field for it. Publishing you cannot measure is
    not healthy publishing — the media engine learns from engagement, so a dead
    readback silently freezes what it learns from.

    ★ CAPPED AT 'degraded' ON PURPOSE — never 'silent'. `silent` is documented
      as "nothing published recently (or ever)", and rollup_verdict carries the
      worst component's verdict STRING up to the aggregate. A measurement
      failure emitting 'silent' would tell media_organism, brain_qa,
      brain_ownership_loop, brain_self_perception and the morning briefing that
      the publisher had stopped — which is exactly the false-outage the
      2026-08-17 rank collision manufactured. 'degraded' is the honest word:
      shipping, impaired.

    ★ A window with nothing to measure is 'quiet', not a failure: no posts
      means no readback is OWED. Do not let an empty window read as an outage
      (and do not let it read as proof of health either).

    age_hours  — since the last successful engagement fetch; None = never.
    unmeasured — posts with a URN, past the grace period, still lacking
                 impressions: the ones we published and cannot score.
    measurable — posts with a URN in the window at all.
    """
    if measurable <= 0:
        return {"verdict": "quiet", "measurable": 0, "unmeasured": 0,
                "age_hours": age_hours,
                "reasons": ["no posts in the window — no readback owed"]}
    stale = age_hours is None or age_hours > _READBACK_STALE_H
    if stale and unmeasured > 0:
        age_txt = ("never" if age_hours is None
                   else f"{age_hours / 24.0:.1f}d ago")
        return {
            "verdict": "degraded",
            "measurable": measurable, "unmeasured": unmeasured,
            "age_hours": age_hours,
            # ★ Name the OBSERVABLE, not a cause this function never saw. The
            #   squasher burned 82 findings writing a guessed cause into a
            #   permanent verdict; the fix there was to report what was
            #   measured and let the reader go look.
            "reasons": [f"engagement last read back {age_txt}; {unmeasured} of "
                        f"{measurable} posts carry no impressions — published "
                        f"but unmeasurable. Check the linkedin-engagement-sync "
                        f"job for what the platform actually returned."],
        }
    return {"verdict": "healthy", "measurable": measurable,
            "unmeasured": unmeasured, "age_hours": age_hours, "reasons": []}


def rollup_verdict(components: dict) -> tuple:
    """PURE. Roll component verdicts into (aggregate_verdict, ok).

    ★ 2026-08-17 — THE AGGREGATE RENAMED EVERY 'degraded' FEED 'silent'.
    `silent` and `degraded` both ranked 3, and the rollup recovered the NAME by
    reverse-lookup — `next(v for v, rank in severity_rank.items()
    if rank == worst)` — which returns the FIRST key at that rank in dict order.
    That key is 'silent'. So a publisher shipping 18 posts in 7d with 3 stranded
    claims (component verdict 'degraded') published `"verdict": "silent"`, whose
    documented meaning is "nothing published recently (or ever)".

    Every consumer — media_organism, brain_qa, brain_ownership_loop,
    brain_self_perception and the morning briefing — was told the feed was dead
    while it ran at 86% of cadence. The inverse of the 08-15 bug this endpoint
    was rewritten to fix: that one under-reported an outage, this one
    manufactured one, and both make the surface unusable for deciding anything.

    Two independent corrections, either of which alone fixes it:
      1. ranks are DISTINCT, so no reverse-lookup is ambiguous, and 'silent'
         (nothing at all) correctly outranks 'degraded' (shipping, impaired);
      2. the winning verdict STRING is carried directly, never re-derived from
         its rank — so a future rank collision cannot rename a verdict again.

    An unknown verdict string ranks 0: it must not silently outrank a real
    'silent'. Components with no verdict (e.g. the `error` string) are skipped.
    """
    verdicts = [c["verdict"] for c in (components or {}).values()
                if isinstance(c, dict) and c.get("verdict")]
    worst = max((_SEVERITY_RANK.get(v, 0) for v in verdicts), default=0)
    winner = max(verdicts, key=lambda v: _SEVERITY_RANK.get(v, 0),
                 default="healthy")
    # Unchanged band: weak(2) and worse is not-ok; quiet(1)/healthy(0) are ok.
    return winner, worst < 2


@dchub_media_revival_bp.route("/api/v1/media/pulse", methods=["GET"])
def media_pulse():
    """Consolidated DC Hub Media health rollup.

    Replaces the operator's mental sum of:
      - /api/v1/media/press-health  (press cadence)
      - /api/v1/media/winback-pitches (outbound queue)
      + LinkedIn publish velocity

    Returns one dict + a single verdict (healthy / weak / quiet /
    silent) so /transparency can render a single tile and the
    surveillance sweep can roll it into the master severity.
    """
    out: dict = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "components": {},
    }
    c = _conn()
    press_age, press_30d, press_7d, li_24h, li_7d = None, 0, 0, 0, 0
    quad: dict = {}   # publisher-table rollup; stays {} if the read fails
    if c is not None:
        try:
            with c.cursor() as cur:
                for table, date_col in (
                    ("auto_press_releases", "generated_at"),
                    ("auto_press_releases", "generated_for"),
                ):
                    try:
                        cur.execute(f"SELECT to_regclass('public.{table}')")
                        if not (cur.fetchone() or [None])[0]:
                            continue
                        cur.execute(f"""
                            SELECT
                              EXTRACT(EPOCH FROM (NOW() - MAX({date_col})))/86400.0,
                              COUNT(*) FILTER (WHERE {date_col} >= NOW() - INTERVAL '30 days'),
                              COUNT(*) FILTER (WHERE {date_col} >= NOW() - INTERVAL '7 days')
                            FROM {table}
                        """)
                        r = cur.fetchone()
                        if r and r[0] is not None:
                            press_age = float(r[0])
                            press_30d = int(r[1] or 0)
                            press_7d = int(r[2] or 0)
                            break
                    except Exception:
                        continue
                try:
                    cur.execute("""
                        SELECT
                          COUNT(*) FILTER (WHERE linkedin_sent_at >= NOW() - INTERVAL '24 hours'),
                          COUNT(*) FILTER (WHERE linkedin_sent_at >= NOW() - INTERVAL '7 days')
                        FROM auto_press_releases
                        WHERE linkedin_sent_at IS NOT NULL
                    """)
                    r = cur.fetchone()
                    if r:
                        li_24h = int(r[0] or 0)
                        li_7d = int(r[1] or 0)
                except Exception:
                    pass
                # ★ 2026-08-15 — THE PUBLISHER'S OWN TABLE. See the linkedin
                # component below for why the counter above cannot see an outage.
                try:
                    cur.execute("""
                        SELECT
                          EXTRACT(EPOCH FROM (NOW() - MAX(posted_at)
                                   FILTER (WHERE success)))/3600.0,
                          COUNT(*) FILTER (WHERE success
                                   AND posted_at >= NOW() - INTERVAL '24 hours'),
                          COUNT(*) FILTER (WHERE success
                                   AND posted_at >= NOW() - INTERVAL '7 days'),
                          COUNT(*) FILTER (WHERE posted_at >= NOW() - INTERVAL '7 days'),
                          COUNT(*) FILTER (WHERE success
                                   AND posted_at >= NOW() - INTERVAL '7 days'
                                   AND COALESCE(image_attached, FALSE)),
                          COUNT(*) FILTER (WHERE NOT success
                                   AND posted_at >= NOW() - INTERVAL '3 days'
                                   AND COALESCE(error_msg,'') LIKE 'gate:%'),
                          -- ★ 2026-08-16 ABANDONED CLAIMS. _claim_slot pre-inserts
                          -- success=FALSE, error_msg='claimed_in_flight' and sets
                          -- ONLY claimed_at; _record fills posted_at later. If the
                          -- process dies in between the row is stranded forever —
                          -- and because EVERY counter above filters on posted_at,
                          -- which a stranded row never gets, it was invisible even
                          -- to attempts_7d. 10 of 30 slots in one 08-08..08-15
                          -- window ended this way with no success row for the same
                          -- (slot_date, slot_hour): they produced nothing, and the
                          -- publisher still read "healthy".
                          -- Keyed on claimed_at, and only past the claim TTL, so a
                          -- publish genuinely in flight right now is not counted.
                          -- Since the suppress paths started stamping their exits
                          -- (_stamp_claim_outcome), a row that ENDS in
                          -- 'claimed_in_flight' means the attempt got no outcome
                          -- at all — a genuine mid-flight death, not desk silence.
                          -- ★★2026-08-17: `posted_at IS NULL` ADDED — without it
                          -- this counter contradicted its own premise above.
                          -- The premise is "_record fills posted_at later ...
                          -- because EVERY counter filters on posted_at, which a
                          -- stranded row NEVER GETS". A row that HAS posted_at
                          -- therefore reached _record and recorded an outcome;
                          -- it cannot be a death between claim and publish.
                          -- Such rows exist because _claim_slot's re-claim path
                          -- resets error_msg back to 'claimed_in_flight' on a
                          -- later tick, overwriting the outcome marker on a row
                          -- that already has its posted_at.
                          -- All three rows counted on 2026-08-17 were of exactly
                          -- this shape — claimed_at LATER than posted_at:
                          --   08-13 16:00  posted 16:01:11  re-claimed 19:52:21
                          --   08-13 08:00  posted 08:00:15  re-claimed 11:51:50
                          --   08-12 08:00  posted 08:03:12  re-claimed 19:54:25
                          -- so /api/v1/media/pulse reported "3 slots claimed then
                          -- abandoned — the run died between claim and publish"
                          -- about three slots that had all reached _record hours
                          -- earlier. A counter that fires on the wrong mechanism
                          -- sends the operator hunting a crash that never
                          -- happened — the same class of wrong as the healthy-
                          -- through-an-outage bug this block was added to fix,
                          -- pointed the other way.
                          COUNT(*) FILTER (WHERE success IS NOT TRUE
                                   AND COALESCE(error_msg,'') = 'claimed_in_flight'
                                   AND posted_at IS NULL
                                   AND claimed_at >= NOW() - INTERVAL '7 days'
                                   AND claimed_at <  NOW() - INTERVAL '1 hour'),
                          -- The abandoned counter's benign twin: slots that exited
                          -- between claim and record ON PURPOSE (editorial found no
                          -- novel event / composer judged nothing new) and stamped
                          -- that. Counted apart so a floor miss can name
                          -- lead-supply instead of a publisher crash.
                          COUNT(*) FILTER (WHERE success IS NOT TRUE
                                   AND COALESCE(error_msg,'') LIKE 'suppressed:%'
                                   AND claimed_at >= NOW() - INTERVAL '7 days')
                        FROM linkedin_quad_posts
                    """)   # single % is correct: execute() gets NO args tuple,
                           # so psycopg2 does no interpolation on this string.
                    r = cur.fetchone()
                    if r:
                        quad = {
                            "hours_since_success": (round(float(r[0]), 1)
                                                    if r[0] is not None else None),
                            "published_24h":   int(r[1] or 0),
                            "published_7d":    int(r[2] or 0),
                            "attempts_7d":     int(r[3] or 0),
                            "with_image_7d":   int(r[4] or 0),
                            "gate_blocked_3d": int(r[5] or 0),
                            "abandoned_claims_7d": int(r[6] or 0),
                            "suppressed_7d":   int(r[7] or 0),
                        }
                except Exception:
                    pass
        except Exception as _e:
            out["components"]["error"] = f"{type(_e).__name__}: {str(_e)[:80]}"

    out["components"]["press"] = {
        "days_since_last": round(press_age, 1) if press_age is not None else None,
        "count_30d": press_30d,
        "count_7d":  press_7d,
        "verdict": (
            "silent"  if press_age is None or press_age > 7 else
            "weak"    if press_30d < 4 else
            "healthy"
        ),
    }
    # ── LinkedIn ─────────────────────────────────────────────────────
    # ★ 2026-08-15 — WHY THIS COMPONENT WAS REWRITTEN. IT REPORTED HEALTHY
    # THROUGH A THREE-DAY TOTAL OUTAGE. Two independent reasons, both fatal:
    #
    #   1. WRONG TABLE. It counted auto_press_releases.linkedin_sent_at — the
    #      PRESS cross-post path. The quad publisher writes linkedin_quad_posts
    #      and was never read here, so the surface that died was not measured at
    #      all. Nothing about the outage could move this number.
    #   2. A BAR OF ZERO. `"healthy" if li_7d > 0` — one cross-post in seven days
    #      against a 4-posts-a-DAY cadence scored a clean bill of health.
    #
    # Live at the time of writing: last successful quad post 2026-08-12T18:40Z,
    # 8 consecutive slots refused since, every card text-only for 30 posts —
    # and this endpoint returned {"linkedin":{"verdict":"healthy"},"ok":true},
    # which the organism, brain_qa, brain_ownership_loop, brain_self_perception
    # and the morning briefing all consume. Every one of them was told fine.
    #
    # So it now watches the publisher's own table and fails LOUD on the three
    # ways this has actually broken: nothing published, published-but-no-cards,
    # and the same lead refused over and over (the selector/gate deadlock).
    # The press cross-post counter is kept as context, never as the verdict.
    out["components"]["linkedin"] = linkedin_publisher_verdict(
        quad, press_crosspost_24h=li_24h, press_crosspost_7d=li_7d)

    # ── can we still READ BACK what we published? (2026-08-18) ──────────
    # Publishing is only half the loop. See engagement_readback_verdict.
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute(f"""
                    SELECT
                      EXTRACT(EPOCH FROM (NOW() - MAX(engagement_fetched_at)))
                        / 3600.0,
                      COUNT(*) FILTER (
                        WHERE post_urn IS NOT NULL
                          AND posted_at >= NOW() - INTERVAL '30 days'
                          AND posted_at <= NOW() - INTERVAL '{_READBACK_GRACE_H} hours'),
                      COUNT(*) FILTER (
                        WHERE post_urn IS NOT NULL
                          AND posted_at >= NOW() - INTERVAL '30 days'
                          AND posted_at <= NOW() - INTERVAL '{_READBACK_GRACE_H} hours'
                          AND impressions IS NULL)
                      FROM linkedin_posts""")
                _r = cur.fetchone() or (None, 0, 0)
                out["components"]["measurement"] = engagement_readback_verdict(
                    None if _r[0] is None else float(_r[0]),
                    int(_r[2] or 0), int(_r[1] or 0))
        except Exception as _e:  # noqa: BLE001
            # ★ BLIND != healthy AND BLIND != RED. rollup_verdict skips
            #   components carrying no `verdict`, so an unreadable table
            #   neither certifies the loop nor manufactures an outage — it
            #   says so and stays out of the aggregate.
            out["components"]["measurement"] = {
                "measured": False, "error": str(_e)[:160],
                "reasons": ["engagement readback UNMEASURED — could not read "
                            "linkedin_posts; this is not evidence of health"]}

    pitches_count = 0
    try:
        from flask import current_app
        with current_app.test_client() as _tc:
            _r = _tc.get("/api/v1/media/winback-pitches")
            if _r.status_code == 200:
                _data = _r.get_json() or {}
                pitches_count = int(_data.get("platform_count") or 0)
    except Exception:
        pass
    out["components"]["winback"] = {
        "platforms_targetable": pitches_count,
        "verdict": "healthy" if pitches_count > 0 else "quiet",
    }

    # ★Assigned as two explicit subscripts, NOT tuple-unpacked: the API
    # response-contract guard reads these statically and cannot see keys set
    # via `out["a"], out["b"] = f()` — it reported both as REMOVED.
    _agg_verdict, _agg_ok = rollup_verdict(out["components"])
    out["verdict"] = _agg_verdict
    out["ok"] = _agg_ok

    if c is not None:
        try: c.close()
        except Exception: pass

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
