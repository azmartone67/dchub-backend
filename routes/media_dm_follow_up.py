"""
media_dm_follow_up.py — DC Hub Media (2026-06-07).

LinkedIn commenter DM follow-up loop (compounds the F comment-engagement
loop in media_comment_engagement.py).

Comments are public engagement. DMs are 1:1 sales conversations. The
combination of:
  - A high-engagement comment from a real person at a real company
  - Followed by a personalized DM with a specific data point relevant
    to their work

…converts to leads at a much higher rate than cold outreach. State of
2026 launch Monday will generate dozens of these moments.

The mechanic:
  1. Cron polls media_comment_engagement_log for REPLIED comments in the
     last 7 days where we have enriched the commenter's LinkedIn profile.
  2. For each qualified commenter (500+ followers OR title contains one
     of the qualified keywords like "CFO" / "Director" / "VP" /
     "Founder" / "PE" / "Real Estate" / "Energy" / etc.), pick the most
     relevant DC Hub asset:
       - PE / Energy        → /reports/state-of-power
       - Real Estate        → /markets/<their-city>/brief
       - DC operator        → /operators/<their-company>/brief
       - General            → /state-of-2026
  3. Claude generates a 3-sentence DM that:
       - references their public comment in sentence 1
       - adds ONE specific data point in sentence 2
       - soft ask in sentence 3 ("happy to chat — just reply")
     Tone filter rejects banned phrases (circle back, synergy, leverage,
     best of breed, value-add). Regenerates ONCE on lazy hit.
  4. Daily cap = 5 DMs/day (LinkedIn allows ~100/week for org pages;
     5/day = 35/week, very safe).
  5. 24h cooldown per recipient (don't DM same person twice in 24h).
  6. Send via LinkedIn DM API as the org URN.
  7. Log to media_dm_log + best-effort track downstream attribution
     (visit / signup) via the same /li/<short> click-attribution proxy
     used by media_topic_tuner.

SAFETY (DRY-RUN ON by default for first deploy):
  - MEDIA_DM_DISABLE=1         → master kill switch
  - MEDIA_DM_DRY_RUN=1         → default ON; draft but DO NOT send
  - MEDIA_DM_DAILY_CAP=5       → hard cap, max 5 per UTC day
  - MEDIA_DM_MIN_FOLLOWERS=500 → quality floor
  - MEDIA_DM_BLOCKLIST=...     → comma-separated URNs/names to skip
  - 24h cooldown per recipient (enforced via media_dm_log lookup)
  - Tone reject list — auto-regenerates ONCE; on 2nd miss the recipient
    is skipped with decision_reason='skipped_tone'.

Endpoints (all admin-keyed):
  GET  /api/v1/admin/media/dm-followup/preview?days=7
       — what we WOULD send right now. Generates drafts, NO sending,
         even if MEDIA_DM_DRY_RUN=0.
  POST /api/v1/admin/media/dm-followup/send
       — full cycle. Honors MEDIA_DM_DRY_RUN. Cron calls this.
  GET  /api/v1/admin/media/dm-followup/log?days=30
       — last 30d of log rows for the dashboard.
  POST /api/v1/admin/media/dm-followup/approve/<log_id>
       — admin override: send a specific dry-run draft now.
  POST /api/v1/admin/media/dm-followup/regenerate/<log_id>
       — re-roll the Claude call for a single draft row (no send).

Schema: media_dm_log (see init_dm_follow_up_tables).

Cron entries (crawler_scheduler.py):
  (11, 23, "media_dm_follow_up", "_run_media_dm_follow_up")
"""
from __future__ import annotations

import os
import re
import sys
import json
import datetime
from typing import Any

from flask import Blueprint, jsonify, request

media_dm_follow_up_bp = Blueprint("media_dm_follow_up", __name__)


# ── Tunables (env-driven) ────────────────────────────────────────────────
LOOKBACK_DAYS_COMMENTS = 7
DAILY_CAP_DEFAULT      = 5      # MEDIA_DM_DAILY_CAP (LinkedIn safe: 35/wk)
MIN_FOLLOWERS_DEFAULT  = 500    # MEDIA_DM_MIN_FOLLOWERS
COOLDOWN_HOURS_DEFAULT = 24     # 24h per-recipient cooldown
DM_SUBJECT_MAX_CHARS   = 70
DM_BODY_MAX_CHARS      = 500    # ~3 sentences, well under LinkedIn limit
MAX_DRAFTS_PER_RUN     = 5      # same as daily cap; matches LinkedIn safe
MODEL_NAME             = "claude-haiku-4-5-20251001"
MAX_TOKENS_PER_GEN     = 700


# Qualified title keywords (case-insensitive). Anyone with a title
# matching ONE of these is allowed through even below the follower floor.
QUALIFIED_TITLE_KEYWORDS = {
    # Senior decision-makers
    "ceo", "cfo", "coo", "cto", "cio", "cmo",
    "founder", "co-founder", "cofounder",
    "president", "vp", "vice president",
    "head of", "director", "managing director",
    "partner", "managing partner", "general partner",
    "principal",
    # Industry-relevant roles
    "real estate", "data center", "datacenter", "data-center",
    "energy", "power", "utility", "utilities",
    "infrastructure", "interconnection",
    "private equity", "pe ", " pe,", " pe ",
    "investment", "investor", "asset manager",
    "site selection", "development", "developer",
    "construction", "design build",
    "broker", "brokerage", "tenant rep",
    "reit", "fund manager",
}


# Lazy LLM-speak — reject + regenerate once. Mirrors the comment
# engagement filter PLUS the DM-specific bans from the spec.
LAZY_REJECT_TOKENS = {
    "circle back",
    "synergy",
    "synergies",
    "leverage",
    "leveraging",
    "best of breed",
    "best-of-breed",
    "value-add",
    "value add",
    "amazing",
    "game-changer",
    "game changer",
    "revolutionary",
    "delve",
    "ecosystem",
    "tapestry",
    "unleash",
    "groundbreaking",
    "paradigm shift",
    "elevate your",
    "unlock the power",
    "low-hanging fruit",
    "move the needle",
    "let's connect",
    "lets connect",
    "let's chat",  # too generic; we want the SPECIFIC ask
    "touch base",
    "hop on a call",
    "quick sync",
    "thoughtful comment",  # backslapping
    "great point",  # backslapping
}


# ── Plumbing ─────────────────────────────────────────────────────────────
def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[dm-followup] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def _kill_switched() -> bool:
    return os.environ.get("MEDIA_DM_DISABLE", "0") == "1"


def _dry_run() -> bool:
    # DEFAULT ON — drafts DMs but does NOT send. Operator flips
    # MEDIA_DM_DRY_RUN=0 after reviewing ~5 drafts.
    return os.environ.get("MEDIA_DM_DRY_RUN", "1") == "1"


def _blocklist() -> set[str]:
    raw = os.environ.get("MEDIA_DM_BLOCKLIST", "") or ""
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _company_urn() -> str:
    company = (os.environ.get("LINKEDIN_COMPANY_ID") or "").strip()
    return f"urn:li:organization:{company}" if company else ""


def _li_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": "202601",
        "X-Restli-Protocol-Version": "2.0.0",
    }


# ── Schema ───────────────────────────────────────────────────────────────
def init_dm_follow_up_tables() -> bool:
    """Idempotent schema bootstrap. Creates media_dm_log."""
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_dm_log (
                  id                           BIGSERIAL PRIMARY KEY,
                  source_comment_urn           TEXT UNIQUE NOT NULL,
                  source_post_urn              TEXT,
                  recipient_urn                TEXT NOT NULL,
                  recipient_name               TEXT,
                  recipient_title              TEXT,
                  recipient_company            TEXT,
                  recipient_headline           TEXT,
                  recipient_follower_count     INTEGER,
                  qualified_by                 TEXT,
                  dm_subject                   TEXT,
                  dm_body                      TEXT,
                  dm_link                      TEXT,
                  dm_sent_at                   TIMESTAMPTZ,
                  dm_thread_urn                TEXT,
                  decision                     TEXT,
                  decision_reason              TEXT,
                  detected_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  response_received_at         TIMESTAMPTZ,
                  response_text                TEXT,
                  attributed_visit             BOOLEAN,
                  attributed_signup            BOOLEAN
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_mdl_detected
                    ON media_dm_log(detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_mdl_recipient_recent
                    ON media_dm_log(recipient_urn, detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_mdl_decision
                    ON media_dm_log(decision)
            """)
        _log("schema bootstrap OK")
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


# Lazy init at import-time.
try:
    _SCHEMA_OK = init_dm_follow_up_tables()
except Exception:
    _SCHEMA_OK = False


# ── LinkedIn profile enrichment ──────────────────────────────────────────
def enrich_commenter(comment: dict, token: str) -> dict:
    """Pull LinkedIn profile data via the same auth scope as F (the
    LINKEDIN_ACCESS_TOKEN). Returns:
      {name, headline, company, title, follower_count, urn}

    Best-effort, NEVER raises. If the API rejects (scope missing, rate
    limit, member doesn't exist) we return whatever we have (often just
    the URN tail).

    The standard LinkedIn person endpoint requires r_liteprofile +
    organizationConnections to read company/headline. Most marketing
    apps don't have full profile scope, so we fall back to the comment
    actor URN + the headline LinkedIn embeds in the comment payload
    when available (LinkedIn includes commenterFirstName/LastName +
    headlineV2 inline on some API versions)."""
    out = {
        "name":           "",
        "headline":       "",
        "company":        "",
        "title":          "",
        "follower_count": 0,
        "urn":            comment.get("author_urn") or "",
    }
    urn = (comment.get("author_urn") or "").strip()
    if not urn:
        return out

    # First — check if the comment payload already includes inline
    # commenter metadata (some LinkedIn API versions add this).
    inline = comment.get("commenter_profile") or comment.get("actor_data") or {}
    if isinstance(inline, dict):
        nm = (inline.get("name") or inline.get("displayName")
              or inline.get("fullName") or "")
        if nm:
            out["name"] = str(nm)[:80]
        hd = (inline.get("headline") or inline.get("headlineV2") or "")
        if hd:
            out["headline"] = str(hd)[:200]
        co = (inline.get("company") or inline.get("currentCompany") or "")
        if co:
            out["company"] = str(co)[:80]
        ti = (inline.get("title") or inline.get("currentTitle") or "")
        if ti:
            out["title"] = str(ti)[:80]
        fc = inline.get("follower_count") or inline.get("followerCount") or 0
        try:
            out["follower_count"] = int(fc)
        except Exception:
            pass

    # If we already have everything, short-circuit.
    if out["name"] and out["headline"] and out["follower_count"]:
        return out

    # Otherwise try the People API. We're conservative with timeouts
    # because Railway is single-replica (see backend_flapping memory).
    if not token:
        return out
    try:
        import urllib.parse as _up
        import requests as _rq
        # urn:li:person:abc123 → "abc123"
        member_id = urn.split(":", 2)[-1] if ":" in urn else urn
        if not member_id:
            return out
        # /rest/people/(id:abc123)?projection=...
        url = (f"https://api.linkedin.com/rest/people/"
               f"(id:{_up.quote(member_id, safe='')})")
        resp = _rq.get(url, headers=_li_headers(token), timeout=8)
        if resp.status_code == 200:
            d = resp.json() or {}
            first = ((d.get("firstName") or {}).get("localized") or {})
            last  = ((d.get("lastName")  or {}).get("localized") or {})
            # Pick the first locale value
            f_val = next(iter(first.values())) if first else ""
            l_val = next(iter(last.values()))  if last  else ""
            if (f_val or l_val) and not out["name"]:
                out["name"] = f"{f_val} {l_val}".strip()[:80]
            hd = d.get("headline") or {}
            if isinstance(hd, dict):
                hd = next(iter((hd.get("localized") or {}).values()), "")
            if hd and not out["headline"]:
                out["headline"] = str(hd)[:200]
        else:
            _log(f"profile_fetch_status={resp.status_code} urn={urn}")
    except Exception as e:
        _log(f"profile_fetch_err urn={urn} err={e}")

    # Parse company + title from headline ("CTO at Acme Corp" or
    # "Founder & CEO @ Acme | helping data centers...") — best effort.
    if out["headline"] and (not out["company"] or not out["title"]):
        hd = out["headline"]
        m = re.match(
            r"^([^|@,]+?)\s+(?:at|@)\s+([^|,]+?)(?:\s*[|,]|$)",
            hd, re.IGNORECASE)
        if m:
            if not out["title"]:
                out["title"] = m.group(1).strip()[:80]
            if not out["company"]:
                out["company"] = m.group(2).strip()[:80]

    return out


# ── Qualification ────────────────────────────────────────────────────────
def is_qualified(profile: dict, min_followers: int) -> tuple[bool, str]:
    """Return (qualified, reason). reason ∈ {'followers', 'title:<kw>',
    'rejected_low_quality'}."""
    fc = int(profile.get("follower_count") or 0)
    if fc >= int(min_followers):
        return True, f"followers:{fc}"
    title = (profile.get("title") or "").lower()
    headline = (profile.get("headline") or "").lower()
    haystack = f"{title} {headline}"
    for kw in QUALIFIED_TITLE_KEYWORDS:
        # word-boundary on simple keywords; substring for multi-word
        if " " in kw or "-" in kw:
            if kw in haystack:
                return True, f"title:{kw.strip()}"
        else:
            if re.search(rf"\b{re.escape(kw)}\b", haystack):
                return True, f"title:{kw}"
    return False, "rejected_low_quality"


# ── Brief selection ──────────────────────────────────────────────────────
# US data-center metro slugs — must match the metro form used by
# /markets/<slug>/brief (see reference_dchub_market_slugs.md).
_KNOWN_METRO_SLUGS = {
    "northern-virginia", "ashburn", "atlanta", "dallas", "chicago",
    "phoenix", "silicon-valley", "seattle", "portland", "los-angeles",
    "new-york", "northern-new-jersey", "boston", "denver", "salt-lake-city",
    "columbus", "des-moines", "minneapolis", "miami", "tampa",
    "houston", "austin", "san-antonio", "nashville", "raleigh",
    "charlotte", "memphis", "pittsburgh", "philadelphia",
    "kansas-city", "omaha", "cheyenne", "reno", "las-vegas",
}

# Known DC-operator slugs — keep this conservative; better to fall back
# to the generic /operators index than 404 a brief URL.
_KNOWN_OPERATOR_SLUGS = {
    "equinix", "digital-realty", "qts", "coresite", "cyrusone",
    "vantage", "compass-datacenters", "stack-infrastructure",
    "aligned", "edgeconnex", "edgeconnex", "iron-mountain",
    "switch", "data4", "global-switch", "ntt-data", "ntt",
    "airtrunk", "next-dc", "interxion", "telecitygroup",
    "cologix", "365-data-centers", "databank", "evoque", "h5",
    "lambda-labs", "lambda", "applied-digital", "core-scientific",
    "crusoe", "iren", "iris-energy",
}


def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def select_brief(profile: dict) -> dict:
    """Pick the most relevant DC Hub asset for this commenter.

    Returns: {url, asset_kind, reason}

    Routing:
      PE / Energy       → /reports/state-of-power
      Real Estate       → /markets/<their-city>/brief  (if slug known)
      DC operator       → /operators/<their-company>/brief  (if slug known)
      General           → /state-of-2026 (always-safe headline)
    """
    title    = (profile.get("title") or "").lower()
    headline = (profile.get("headline") or "").lower()
    company  = profile.get("company") or ""
    haystack = f"{title} {headline}"

    # 1. DC operator — most specific, route to operator brief if slug known
    company_slug = _slugify(company)
    if company_slug and company_slug in _KNOWN_OPERATOR_SLUGS:
        return {
            "url":        f"dchub.cloud/operators/{company_slug}/brief",
            "asset_kind": "operator_brief",
            "reason":     f"operator:{company_slug}",
        }

    # 2. PE / Energy → State of Power
    pe_signals = ("private equity", "investment", "investor",
                  "asset manager", "fund manager", "pe ", "reit",
                  "energy", "power", "utility", "utilities",
                  "interconnection", "renewable")
    if any(sig in haystack for sig in pe_signals):
        return {
            "url":        "dchub.cloud/reports/state-of-power",
            "asset_kind": "state_of_power",
            "reason":     "pe_or_energy",
        }

    # 3. Real Estate → market brief if we can spot a known metro slug
    re_signals = ("real estate", "broker", "brokerage", "tenant rep",
                  "site selection", "land", "cbre", "jll", "cushman",
                  "newmark", "colliers")
    if any(sig in haystack for sig in re_signals):
        # Try to spot a known metro mention in headline/company
        for slug in sorted(_KNOWN_METRO_SLUGS, key=len, reverse=True):
            # slug like "northern-virginia" → check both forms
            nice = slug.replace("-", " ")
            if nice in haystack or slug in haystack:
                return {
                    "url":        f"dchub.cloud/markets/{slug}/brief",
                    "asset_kind": "market_brief",
                    "reason":     f"real_estate:{slug}",
                }
        # Real estate but no metro hint → fall back to state-of-2026
        return {
            "url":        "dchub.cloud/state-of-2026",
            "asset_kind": "state_of_2026",
            "reason":     "real_estate_no_market",
        }

    # 4. Default: the State of 2026 headline (always safe)
    return {
        "url":        "dchub.cloud/state-of-2026",
        "asset_kind": "state_of_2026",
        "reason":     "general",
    }


# ── Live DC Hub data context for the DM ──────────────────────────────────
def _live_context_for_dm(asset_kind: str) -> dict:
    """Pull one or two specific numbers that match the chosen asset.
    No outbound HTTP (single-replica Railway, see backend_flapping)."""
    ctx: dict = {"primary_stat": "", "fallback_stat": ""}
    conn = _db_conn()
    if conn is None:
        # Fall back to canonical numbers from HEALTH_BASELINE.md.
        ctx["primary_stat"]  = "2,000+ tracked deals across 178 countries"
        ctx["fallback_stat"] = "232 DCPI markets, 5,700+ discovered facilities"
        return ctx
    try:
        with conn, conn.cursor() as cur:
            if asset_kind == "state_of_power":
                # ISO queue depth + recent renewable add
                try:
                    cur.execute("""
                        SELECT iso_region, COUNT(*)
                          FROM interconnection_queue_projects
                         WHERE updated_at > NOW() - INTERVAL '30 days'
                         GROUP BY iso_region
                         ORDER BY 2 DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    if row:
                        ctx["primary_stat"] = (
                            f"{int(row[1] or 0):,} projects queued in {row[0]} "
                            f"alone in the last 30 days")
                except Exception:
                    pass
            elif asset_kind == "market_brief":
                # Top DCPI BUILD verdict
                try:
                    cur.execute("""
                        SELECT market_slug, composite_score
                          FROM dcpi_v3_master
                         WHERE COALESCE(verdict,'') ILIKE 'BUILD%'
                         ORDER BY composite_score DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    if row:
                        ctx["primary_stat"] = (
                            f"top BUILD market is {row[0]} with a DCPI of "
                            f"{float(row[1] or 0):.1f}")
                except Exception:
                    pass
            elif asset_kind == "operator_brief":
                # Recent deal count for the operator's market
                try:
                    cur.execute("""
                        SELECT COUNT(*)
                          FROM deals
                         WHERE announced_at > NOW() - INTERVAL '90 days'
                    """)
                    row = cur.fetchone()
                    if row:
                        ctx["primary_stat"] = (
                            f"{int(row[0] or 0):,} data-center deals "
                            f"announced in the last 90 days")
                except Exception:
                    pass

            # Always populate fallback from canonical numbers
            ctx["fallback_stat"] = (
                "2,000+ tracked deals across 178 countries; "
                "232 DCPI markets; 5,700+ discovered facilities")
        # If primary still empty, lift fallback into primary
        if not ctx["primary_stat"]:
            ctx["primary_stat"] = ctx["fallback_stat"]
        return ctx
    except Exception:
        ctx["primary_stat"] = "2,000+ tracked deals across 178 countries"
        return ctx
    finally:
        try: conn.close()
        except Exception: pass


# ── DM generation via Claude ─────────────────────────────────────────────
_DM_PROMPT_SYSTEM = (
    "You are the founder of DC Hub, a data center intelligence platform.\n"
    "You are writing a LinkedIn DM follow-up to someone who left a thoughtful "
    "comment on one of DC Hub's public LinkedIn posts.\n"
    "\n"
    "Voice: casual founder, data-driven, no fluff, no emojis, no hashtags, "
    "no salesy language. Write like one professional reaching out to another.\n"
    "\n"
    "Hard rules:\n"
    "  - SUBJECT: 1 line, MAXIMUM 70 characters, references their comment.\n"
    "  - BODY: EXACTLY 3 sentences.\n"
    "      Sentence 1 — references their public comment specifically.\n"
    "      Sentence 2 — drops EXACTLY ONE specific DC Hub number.\n"
    "      Sentence 3 — soft ask: 'happy to chat if you want a quick "
    "                    walkthrough — just reply'.\n"
    "  - Include EXACTLY ONE link (no scheme, no www).\n"
    "  - 500 character maximum on the BODY.\n"
    "  - No banned phrases: circle back, synergy, leverage, best of breed, "
    "    value-add, let's connect, touch base, hop on a call, amazing, "
    "    revolutionary, game-changer, delve, ecosystem.\n"
    "  - Do NOT compliment the comment ('thoughtful comment', 'great point'). "
    "    Reference WHAT they said, not how they said it.\n"
    "  - Do NOT @-mention anyone. Do NOT use exclamation marks.\n"
    "\n"
    "Return STRICT JSON: "
    "{\"subject\":\"...\",\"body\":\"...\",\"link_used\":\"...\","
    "\"data_used\":\"...\"}\n"
    "Nothing outside the JSON object."
)


def _call_claude(messages: list[dict], system: str) -> str | None:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        _log("claude: no ANTHROPIC_API_KEY")
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model":      MODEL_NAME,
                "max_tokens": MAX_TOKENS_PER_GEN,
                "system":     system,
                "messages":   messages,
            }).encode("utf-8"),
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         key,
                "anthropic-version": "2023-06-01",
                "User-Agent":        "dchub-dm-followup/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return text.strip() or None
    except Exception as e:
        _log(f"claude_call_err: {e}")
        return None


def _parse_dm_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        chunk = m.group(0) if m else raw
        d = json.loads(chunk)
        if not isinstance(d, dict):
            return None
        if not isinstance(d.get("subject"), str):
            return None
        if not isinstance(d.get("body"), str):
            return None
        d.setdefault("link_used", "")
        d.setdefault("data_used", "")
        return d
    except Exception:
        return None


def _dm_tone_ok(subject: str, body: str) -> tuple[bool, str | None]:
    if not subject or not body:
        return False, "empty"
    if len(subject) > DM_SUBJECT_MAX_CHARS:
        return False, f"subject_too_long:{len(subject)}"
    if len(body) > DM_BODY_MAX_CHARS:
        return False, f"body_too_long:{len(body)}"
    s = (subject + " " + body).lower()
    for bad in LAZY_REJECT_TOKENS:
        if bad in s:
            return False, f"lazy_token:{bad}"
    # exclamation guard (spec: soft ask)
    if "!" in body:
        return False, "exclamation"
    return True, None


def generate_dm(commenter: dict, brief: dict,
                comment_text: str = "",
                max_retries: int = 1) -> dict | None:
    """Returns {subject, body, link_used, data_used} or None on fail.

    Retries ONCE on tone-filter rejection. On 2nd miss caller skips
    with decision_reason='skipped_tone'."""
    ctx = _live_context_for_dm(brief.get("asset_kind", ""))
    primary_stat = ctx.get("primary_stat") or ctx.get("fallback_stat") or ""

    nm   = (commenter.get("name") or "").strip()
    ti   = (commenter.get("title") or "").strip()
    co   = (commenter.get("company") or "").strip()
    hd   = (commenter.get("headline") or "").strip()
    link = brief.get("url", "dchub.cloud/state-of-2026")

    who_line = nm or "the commenter"
    if ti and co:
        who_line += f" ({ti} at {co})"
    elif ti:
        who_line += f" ({ti})"
    elif hd:
        who_line += f" ({hd[:60]})"

    user_msg = (
        f"Recipient: {who_line}\n"
        f"They commented on a DC Hub LinkedIn post. Their comment "
        f"(verbatim):\n\"\"\"{comment_text[:600]}\"\"\"\n\n"
        f"You're sending them a 1:1 LinkedIn DM as a follow-up.\n\n"
        f"USE EXACTLY ONE link (no scheme, no www): {link}\n"
        f"USE EXACTLY ONE specific number from this context: {primary_stat}\n\n"
        "Write the DM. Strict JSON: "
        "{\"subject\":\"...\",\"body\":\"...\",\"link_used\":\"...\","
        "\"data_used\":\"...\"}."
    )

    last_reason = None
    for attempt in range(int(max_retries) + 1):
        raw = _call_claude(
            messages=[{"role": "user", "content": user_msg}],
            system=_DM_PROMPT_SYSTEM,
        )
        if not raw:
            last_reason = "claude_no_response"
            continue
        parsed = _parse_dm_json(raw)
        if not parsed:
            last_reason = "claude_unparseable"
            continue
        ok, why = _dm_tone_ok(parsed["subject"], parsed["body"])
        if not ok:
            last_reason = why
            continue
        return parsed
    _log(f"generate_dm_failed reason={last_reason}")
    return None


# ── DM send via LinkedIn API ─────────────────────────────────────────────
def send_dm(recipient_urn: str, subject: str, body: str) -> tuple[bool, dict]:
    """POST a 1:1 DM to recipient_urn as the org URN. Fail-soft, never raises.

    The LinkedIn Messaging API (preview) uses
    POST /rest/messages with {recipients, subject, body, sender: orgUrn}.
    Most org pages don't have the r_messaging scope until applied for;
    when we don't have it the API returns 403/401 and we surface that
    cleanly to the operator (with decision_reason='post_failed:scope').
    """
    try:
        import requests as _rq
        token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
        actor = _company_urn()
        if not token:
            return False, {"error": "no_linkedin_token"}
        if not actor:
            return False, {"error": "no_company_id"}
        if not recipient_urn or not recipient_urn.startswith("urn:li:"):
            return False, {"error": "bad_recipient_urn"}

        url = "https://api.linkedin.com/rest/messages"
        headers = _li_headers(token)
        headers["Content-Type"] = "application/json"
        payload = {
            "sender":     actor,
            "recipients": [recipient_urn],
            "subject":    subject[:DM_SUBJECT_MAX_CHARS],
            "body":       body[:DM_BODY_MAX_CHARS],
        }
        r = _rq.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201, 202):
            try:
                d = r.json() or {}
            except Exception:
                d = {}
            return True, {
                "status":     r.status_code,
                "thread_urn": str(d.get("$URN") or d.get("threadUrn") or ""),
            }
        return False, {"error": f"http_{r.status_code}",
                       "body": r.text[:200]}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── Qualified-commenter discovery ────────────────────────────────────────
def gather_qualified_commenters(days: int = LOOKBACK_DAYS_COMMENTS,
                                limit: int = 50,
                                token: str = "") -> list[dict]:
    """Query media_comment_engagement_log for replied / queued / dry_run
    comments in the last `days` days, enrich each commenter's LinkedIn
    profile, qualify against follower/title bar, and return the survivors.

    Returns a list of dicts:
      {
        comment_urn, source_post_urn, comment_text,
        recipient_urn, name, title, company, headline, follower_count,
        qualified_by
      }
    """
    out: list[dict] = []
    conn = _db_conn()
    if conn is None:
        return out
    min_followers = _env_int("MEDIA_DM_MIN_FOLLOWERS", MIN_FOLLOWERS_DEFAULT)
    bl = _blocklist()
    cooldown_h = _env_int("MEDIA_DM_COOLDOWN_HOURS", COOLDOWN_HOURS_DEFAULT)
    try:
        with conn, conn.cursor() as cur:
            # Eligible: comments we ENGAGED with (replied / queued /
            # dry_run) within the window, where we have a non-self author
            # URN, and we have NOT already DM'd this comment_urn.
            cur.execute(f"""
                SELECT mcel.comment_urn,
                       mcel.source_post_urn,
                       mcel.comment_text,
                       mcel.comment_author_urn,
                       mcel.comment_author_name
                  FROM media_comment_engagement_log mcel
                 WHERE mcel.comment_detected_at > NOW() - INTERVAL '{int(days)} days'
                   AND mcel.decision IN ('replied','queued','dry_run')
                   AND COALESCE(mcel.comment_author_urn,'') <> ''
                   AND NOT EXISTS (
                       SELECT 1 FROM media_dm_log mdl
                        WHERE mdl.source_comment_urn = mcel.comment_urn)
                 ORDER BY mcel.comment_detected_at DESC
                 LIMIT %s
            """, (int(limit),))
            rows = cur.fetchall() or []

            org_urn = _company_urn().lower()

            for r in rows:
                c_urn, p_urn, c_text, a_urn, a_name = r
                if not a_urn:
                    continue
                # Self-comment guard (already guarded upstream, belt+susp)
                if org_urn and str(a_urn).lower() == org_urn:
                    continue
                # Blocklist (URN or trimmed name)
                bl_haystack = [
                    str(a_urn).lower(),
                    str(a_name or "").lower(),
                    (str(a_urn).split(":", 2)[-1].lower()
                     if ":" in str(a_urn) else str(a_urn).lower()),
                ]
                if bl and any(x in bl for x in bl_haystack):
                    continue
                # 24h cooldown per recipient
                try:
                    cur.execute("""
                        SELECT 1 FROM media_dm_log
                         WHERE recipient_urn = %s
                           AND detected_at > NOW() - make_interval(hours => %s)
                         LIMIT 1
                    """, (str(a_urn), int(cooldown_h)))
                    if cur.fetchone() is not None:
                        continue
                except Exception:
                    pass

                # Enrich profile (best-effort)
                comment_blob = {
                    "author_urn": str(a_urn),
                    "author_name": str(a_name or ""),
                    "message": str(c_text or ""),
                }
                profile = enrich_commenter(comment_blob, token)
                # Display-name fallback: use logged author_name if API
                # didn't yield one.
                if not profile.get("name") and a_name:
                    profile["name"] = str(a_name)[:80]

                qualified, why = is_qualified(profile, min_followers)
                if not qualified:
                    continue

                out.append({
                    "comment_urn":     str(c_urn),
                    "source_post_urn": str(p_urn or ""),
                    "comment_text":    str(c_text or ""),
                    "recipient_urn":   str(a_urn),
                    "name":            profile.get("name", ""),
                    "title":           profile.get("title", ""),
                    "company":         profile.get("company", ""),
                    "headline":        profile.get("headline", ""),
                    "follower_count":  int(profile.get("follower_count") or 0),
                    "qualified_by":    why,
                })
        return out
    except Exception as e:
        _log(f"gather_qualified_commenters_failed: {e}")
        return out
    finally:
        try: conn.close()
        except Exception: pass


# ── Logging ──────────────────────────────────────────────────────────────
def _log_decision(
    candidate: dict,
    decision: str,
    reason: str,
    dm: dict | None = None,
    brief: dict | None = None,
    sent_at: datetime.datetime | None = None,
    thread_urn: str | None = None,
) -> int | None:
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO media_dm_log (
                        source_comment_urn, source_post_urn,
                        recipient_urn, recipient_name, recipient_title,
                        recipient_company, recipient_headline,
                        recipient_follower_count, qualified_by,
                        dm_subject, dm_body, dm_link,
                        dm_sent_at, dm_thread_urn,
                        decision, decision_reason
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_comment_urn) DO UPDATE SET
                        decision        = EXCLUDED.decision,
                        decision_reason = EXCLUDED.decision_reason,
                        dm_subject      = COALESCE(EXCLUDED.dm_subject,
                                                   media_dm_log.dm_subject),
                        dm_body         = COALESCE(EXCLUDED.dm_body,
                                                   media_dm_log.dm_body),
                        dm_link         = COALESCE(EXCLUDED.dm_link,
                                                   media_dm_log.dm_link),
                        dm_sent_at      = COALESCE(EXCLUDED.dm_sent_at,
                                                   media_dm_log.dm_sent_at),
                        dm_thread_urn   = COALESCE(EXCLUDED.dm_thread_urn,
                                                   media_dm_log.dm_thread_urn)
                    RETURNING id
                """, (
                    candidate.get("comment_urn") or "",
                    candidate.get("source_post_urn") or "",
                    candidate.get("recipient_urn") or "",
                    (candidate.get("name") or "")[:120],
                    (candidate.get("title") or "")[:120],
                    (candidate.get("company") or "")[:120],
                    (candidate.get("headline") or "")[:240],
                    int(candidate.get("follower_count") or 0),
                    (candidate.get("qualified_by") or "")[:60],
                    ((dm or {}).get("subject") or "")[:DM_SUBJECT_MAX_CHARS]
                        if dm else None,
                    ((dm or {}).get("body") or "")[:DM_BODY_MAX_CHARS]
                        if dm else None,
                    ((brief or {}).get("url") or "")[:240]
                        if brief else None,
                    sent_at,
                    thread_urn,
                    decision,
                    reason,
                ))
                row = cur.fetchone()
                return int(row[0]) if row else None
            except Exception as e:
                _log(f"log_insert_failed: {e}")
                return None
    except Exception as e:
        _log(f"log_decision_failed: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


# ── Daily cap helper ─────────────────────────────────────────────────────
def _dms_today(cur) -> int:
    """Count of DMs sent or drafted today UTC."""
    cur.execute("""
        SELECT COUNT(*)
          FROM media_dm_log
         WHERE decision IN ('sent','dry_run','queued')
           AND detected_at >= date_trunc('day', NOW())
    """)
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


# ── Main scan ────────────────────────────────────────────────────────────
def run_dm_follow_up_scan(force_send: bool = False,
                          force_dry: bool = False) -> dict:
    """Main entrypoint. Find qualified commenters, generate DMs, send
    (or draft in DRY_RUN). Never raises.

    In DRY_RUN mode (default), drafts are written with decision='dry_run'.
    Operator reviews on /admin/media-mix → "DM Follow-up" and either
    flips MEDIA_DM_DRY_RUN=0 OR uses the per-row "approve & send" button.

    Args:
      force_send: bypass DRY_RUN and send immediately (admin button).
      force_dry:  force DRY_RUN regardless of env (preview endpoint).

    Returns: {detected, qualified, fired, skipped, errors}.
    """
    result: dict = {
        "ok":           False,
        "detected":     0,
        "qualified":    0,
        "fired":        0,
        "skipped":      {},
        "dry_run":      _dry_run() or force_dry,
        "kill_switched": _kill_switched(),
        "daily_cap":    _env_int("MEDIA_DM_DAILY_CAP", DAILY_CAP_DEFAULT),
        "min_followers": _env_int("MEDIA_DM_MIN_FOLLOWERS", MIN_FOLLOWERS_DEFAULT),
        "dms_today":    0,
        "rows":         [],
        "errors":       [],
    }
    if force_send:
        result["dry_run"] = False
    if result["kill_switched"]:
        result["ok"] = True
        result["errors"].append("kill_switched")
        return result

    token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    if not token:
        result["errors"].append("no_linkedin_token")
        # we can still draft in DRY_RUN without a token (skip enrichment)

    conn = _db_conn()
    if conn is None:
        result["errors"].append("no_db")
        return result
    try:
        with conn, conn.cursor() as cur:
            result["dms_today"] = _dms_today(cur)
            budget = max(0, int(result["daily_cap"]) - result["dms_today"])

        candidates = gather_qualified_commenters(
            days=LOOKBACK_DAYS_COMMENTS,
            limit=50,
            token=token,
        )
        result["detected"]  = len(candidates)
        result["qualified"] = len(candidates)

        # Cap by per-run + per-day budget
        max_to_fire = min(MAX_DRAFTS_PER_RUN, budget)
        for cand in candidates[:max_to_fire]:
            if result["fired"] >= max_to_fire:
                break

            brief = select_brief(cand)
            dm = generate_dm(
                commenter=cand,
                brief=brief,
                comment_text=cand.get("comment_text", ""),
            )
            if not dm:
                result["skipped"]["skipped_tone"] = (
                    result["skipped"].get("skipped_tone", 0) + 1)
                _log_decision(cand,
                              decision="skipped_tone",
                              reason="tone_filter_or_gen_failed",
                              brief=brief)
                continue

            if result["dry_run"]:
                log_id = _log_decision(cand,
                                       decision="dry_run",
                                       reason=brief.get("reason", ""),
                                       dm=dm,
                                       brief=brief)
                result["fired"] += 1
                result["rows"].append({
                    "log_id":         log_id,
                    "recipient":      cand.get("name") or cand.get("recipient_urn"),
                    "title":          cand.get("title"),
                    "company":        cand.get("company"),
                    "followers":      cand.get("follower_count"),
                    "qualified_by":   cand.get("qualified_by"),
                    "subject":        dm["subject"],
                    "body":           dm["body"],
                    "link":           brief.get("url"),
                    "asset_kind":     brief.get("asset_kind"),
                    "status":         "dry_run",
                })
                continue

            # LIVE path
            ok_s, info = send_dm(cand.get("recipient_urn", ""),
                                 dm["subject"], dm["body"])
            now = datetime.datetime.now(datetime.timezone.utc)
            if ok_s:
                log_id = _log_decision(cand,
                                       decision="sent",
                                       reason=brief.get("reason", ""),
                                       dm=dm,
                                       brief=brief,
                                       sent_at=now,
                                       thread_urn=info.get("thread_urn"))
                result["fired"] += 1
                result["rows"].append({
                    "log_id":      log_id,
                    "recipient":   cand.get("name") or cand.get("recipient_urn"),
                    "title":       cand.get("title"),
                    "company":     cand.get("company"),
                    "subject":     dm["subject"],
                    "body":        dm["body"],
                    "link":        brief.get("url"),
                    "thread_urn":  info.get("thread_urn"),
                    "status":      "sent",
                })
            else:
                _log_decision(cand,
                              decision="post_failed",
                              reason=(json.dumps(info)[:300]
                                      if isinstance(info, dict) else str(info)[:300]),
                              dm=dm,
                              brief=brief)
                result["errors"].append({
                    "recipient": cand.get("recipient_urn"),
                    "error":     info,
                })

        result["ok"] = True
        return result
    except Exception as e:
        result["errors"].append(
            f"scan_err:{type(e).__name__}:{str(e)[:120]}")
        result["ok"] = False
        return result
    finally:
        try: conn.close()
        except Exception: pass


# ── Log reads (dashboard) ────────────────────────────────────────────────
def recent_log_rows(days: int = 30, limit: int = 100) -> list[dict]:
    conn = _db_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, source_comment_urn, source_post_urn,
                       recipient_urn, recipient_name, recipient_title,
                       recipient_company, recipient_headline,
                       recipient_follower_count, qualified_by,
                       dm_subject, dm_body, dm_link,
                       dm_sent_at, dm_thread_urn,
                       decision, decision_reason,
                       detected_at,
                       response_received_at, response_text,
                       attributed_visit, attributed_signup
                  FROM media_dm_log
                 WHERE detected_at > NOW() - make_interval(days => %s)
                 ORDER BY detected_at DESC
                 LIMIT %s
            """, (int(days), int(limit)))
            out: list[dict] = []
            for row in cur.fetchall() or []:
                out.append({
                    "id":                       int(row[0]),
                    "source_comment_urn":       str(row[1] or ""),
                    "source_post_urn":          str(row[2] or ""),
                    "recipient_urn":            str(row[3] or ""),
                    "recipient_name":           str(row[4] or ""),
                    "recipient_title":          str(row[5] or ""),
                    "recipient_company":        str(row[6] or ""),
                    "recipient_headline":       str(row[7] or ""),
                    "recipient_follower_count": int(row[8] or 0),
                    "qualified_by":             str(row[9] or ""),
                    "dm_subject":               str(row[10] or ""),
                    "dm_body":                  str(row[11] or ""),
                    "dm_link":                  str(row[12] or ""),
                    "dm_sent_at":               row[13].isoformat() if row[13] else None,
                    "dm_thread_urn":            str(row[14] or ""),
                    "decision":                 str(row[15] or ""),
                    "decision_reason":          str(row[16] or ""),
                    "detected_at":              row[17].isoformat() if row[17] else None,
                    "response_received_at":     row[18].isoformat() if row[18] else None,
                    "response_text":            str(row[19] or "") if row[19] else None,
                    "attributed_visit":         bool(row[20]) if row[20] is not None else None,
                    "attributed_signup":        bool(row[21]) if row[21] is not None else None,
                })
            return out
    except Exception as e:
        _log(f"recent_log_rows_failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


def recent_counters(days: int = 30) -> dict:
    conn = _db_conn()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT decision, COUNT(*)
                  FROM media_dm_log
                 WHERE detected_at > NOW() - make_interval(days => %s)
                 GROUP BY decision
            """, (int(days),))
            out: dict = {"total": 0}
            for row in cur.fetchall() or []:
                key = str(row[0] or "unknown")
                cnt = int(row[1] or 0)
                out[key] = cnt
                out["total"] += cnt
            try:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE attributed_visit IS TRUE),
                           COUNT(*) FILTER (WHERE attributed_signup IS TRUE),
                           COUNT(*) FILTER (WHERE response_received_at IS NOT NULL)
                      FROM media_dm_log
                     WHERE detected_at > NOW() - make_interval(days => %s)
                """, (int(days),))
                r = cur.fetchone()
                if r:
                    out["attributed_visits"]  = int(r[0] or 0)
                    out["attributed_signups"] = int(r[1] or 0)
                    out["responses"]          = int(r[2] or 0)
            except Exception:
                pass
            return out
    except Exception:
        return {}
    finally:
        try: conn.close()
        except Exception: pass


# ── HTTP endpoints ───────────────────────────────────────────────────────
@media_dm_follow_up_bp.route(
    "/api/v1/admin/media/dm-followup/preview", methods=["GET"])
def http_preview():
    """What WOULD we send right now? Always DRY-RUN. Safe to spam."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    result = run_dm_follow_up_scan(force_dry=True)
    return jsonify(result)


@media_dm_follow_up_bp.route(
    "/api/v1/admin/media/dm-followup/send", methods=["POST"])
def http_send():
    """Full cycle. Honors MEDIA_DM_DRY_RUN env. Cron calls this with the
    cron secret. Operator can POST with ?force_send=1 to bypass DRY-RUN
    (rare — use the per-row approve button instead)."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    force = (request.args.get("force_send") or "").lower() in ("1", "true", "yes")
    result = run_dm_follow_up_scan(force_send=force)
    return jsonify(result)


@media_dm_follow_up_bp.route(
    "/api/v1/admin/media/dm-followup/log", methods=["GET"])
def http_log():
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    days = int(request.args.get("days", "30"))
    return jsonify({
        "ok":       True,
        "counters": recent_counters(days=days),
        "rows":     recent_log_rows(days=days),
        "dry_run":  _dry_run(),
        "kill":     _kill_switched(),
        "daily_cap": _env_int("MEDIA_DM_DAILY_CAP", DAILY_CAP_DEFAULT),
        "min_followers": _env_int("MEDIA_DM_MIN_FOLLOWERS",
                                  MIN_FOLLOWERS_DEFAULT),
    })


@media_dm_follow_up_bp.route(
    "/api/v1/admin/media/dm-followup/approve/<int:log_id>",
    methods=["POST"])
def http_approve(log_id: int):
    """Admin override: send a specific dry-run draft NOW (single-row).
    The dashboard's per-draft "approve & send" button hits this."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    conn = _db_conn()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT recipient_urn, dm_subject, dm_body, decision
                  FROM media_dm_log
                 WHERE id = %s
                 LIMIT 1
            """, (int(log_id),))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        recipient_urn, subject, body, decision = row
        if not subject or not body:
            return jsonify(ok=False, error="no_draft_on_row"), 400
        if decision == "sent":
            return jsonify(ok=False, error="already_sent"), 409
        if _kill_switched():
            return jsonify(ok=False, error="kill_switched"), 423

        ok_s, info = send_dm(str(recipient_urn), str(subject), str(body))
        now = datetime.datetime.now(datetime.timezone.utc)
        with conn, conn.cursor() as cur2:
            if ok_s:
                cur2.execute("""
                    UPDATE media_dm_log
                       SET decision        = 'sent',
                           decision_reason = 'approved_by_admin',
                           dm_sent_at      = %s,
                           dm_thread_urn   = %s
                     WHERE id = %s
                """, (now, info.get("thread_urn"), int(log_id)))
                return jsonify(ok=True, log_id=log_id, info=info)
            cur2.execute("""
                UPDATE media_dm_log
                   SET decision        = 'post_failed',
                       decision_reason = %s
                 WHERE id = %s
            """, (json.dumps(info)[:300], int(log_id)))
            return jsonify(ok=False, log_id=log_id, info=info)
    except Exception as e:
        return jsonify(ok=False,
                       error=f"{type(e).__name__}: {str(e)[:200]}"), 500
    finally:
        try: conn.close()
        except Exception: pass


@media_dm_follow_up_bp.route(
    "/api/v1/admin/media/dm-followup/regenerate/<int:log_id>",
    methods=["POST"])
def http_regenerate(log_id: int):
    """Re-roll the Claude call for a specific dry-run row. No send."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    conn = _db_conn()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT recipient_urn, recipient_name, recipient_title,
                       recipient_company, recipient_headline,
                       recipient_follower_count, qualified_by,
                       dm_link, source_comment_urn,
                       (SELECT mcel.comment_text FROM media_comment_engagement_log mcel
                         WHERE mcel.comment_urn = media_dm_log.source_comment_urn LIMIT 1)
                  FROM media_dm_log
                 WHERE id = %s LIMIT 1
            """, (int(log_id),))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        cand = {
            "recipient_urn":  str(row[0] or ""),
            "name":           str(row[1] or ""),
            "title":          str(row[2] or ""),
            "company":        str(row[3] or ""),
            "headline":       str(row[4] or ""),
            "follower_count": int(row[5] or 0),
            "qualified_by":   str(row[6] or ""),
            "comment_urn":    str(row[8] or ""),
            "comment_text":   str(row[9] or ""),
        }
        brief = select_brief(cand)
        # If the previous link was stored, keep that asset_kind stable
        if row[7]:
            brief["url"] = str(row[7])
        dm = generate_dm(cand, brief, comment_text=cand["comment_text"])
        if not dm:
            return jsonify(ok=False, error="gen_failed_or_tone_reject")
        with conn, conn.cursor() as cur2:
            cur2.execute("""
                UPDATE media_dm_log
                   SET dm_subject = %s,
                       dm_body    = %s,
                       dm_link    = %s,
                       decision_reason = CONCAT(COALESCE(decision_reason,''),
                                                ' | regenerated@', NOW()::text)
                 WHERE id = %s
            """, (dm["subject"][:DM_SUBJECT_MAX_CHARS],
                  dm["body"][:DM_BODY_MAX_CHARS],
                  brief["url"][:240], int(log_id)))
        return jsonify(ok=True, log_id=int(log_id),
                       subject=dm["subject"], body=dm["body"],
                       link=brief["url"])
    except Exception as e:
        return jsonify(ok=False,
                       error=f"{type(e).__name__}: {str(e)[:200]}"), 500
    finally:
        try: conn.close()
        except Exception: pass
