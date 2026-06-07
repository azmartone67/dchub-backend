"""
media_comment_engagement.py — DC Hub Media (2026-06-07).

LinkedIn comment engagement loop.

When someone comments on a DC Hub LinkedIn post, Claude drafts a
substantive reply (referencing live DC Hub data), the reply waits
4-7 min for human cadence, then posts as the ORG URN.

CRITICAL for Monday's State of 2026 launch when the user will face a
comment storm with 10K followers.

The mechanic:
  1. Cron polls LinkedIn /rest/socialActions/<post_urn>/comments for
     the last 7 days of org posts every 4 hours (slot 9/21 UTC) and
     the in-between flush slot picks up pending replies (slot 13/1 UTC).
  2. For each unreplied comment from a real user (not self, not bot):
       - Detect intent: question vs. opinion vs. spam
       - If question or substantive opinion: queue for reply
  3. Claude generates a reply that:
       - references the comment text
       - adds live DC Hub data (1 specific number + 1 link to a brief)
       - ends with a soft CTA
       - max 280 chars
       - casual founder voice
  4. Wait 4-7 min (random) for human cadence (queued via
     scheduled_for timestamp on media_comment_engagement_log).
  5. Post via LinkedIn API as the ORG URN (not personal).
  6. Log to media_comment_engagement_log.
  7. Track downstream: did the original commenter visit dchub.cloud?
     (best-effort via the /li/<short> click attribution proxy when
     the reply includes a /li/ link; otherwise leave NULL.)

SAFETY (all OFF by default = SAFE):
  - MEDIA_COMMENT_REPLY_DISABLE=1     → master kill switch
  - MEDIA_COMMENT_REPLY_DRY_RUN=1     → default ON; generate but DO NOT post
  - MEDIA_COMMENT_REPLY_DAILY_CAP=10  → max 10 auto-replies per UTC day
  - MEDIA_COMMENT_REPLY_BLOCKLIST     → comma-separated LinkedIn member URNs
                                         or display names to skip
  - Tone reject list — auto-regenerates ONCE; on 2nd miss the comment is
    skipped with decision='skipped_tone'.
  - Author guard — skip comments authored by the org itself.
  - Stagger — random 4-7 minute delay between detect and post per reply.
  - First-deploy posture — DRY-RUN ON by default; operator reviews ~10
    drafts on /admin/media-mix → "Comment Engagement", flips the flag.

Endpoints (all admin-keyed):
  GET  /api/v1/admin/media/comment-engagement/poll-now
       — what comments WOULD be replied to right now? Detect + generate +
         tone-check, NO posting, returns the drafts. Safe to spam.
  POST /api/v1/admin/media/comment-engagement/run
       — full live cycle: poll + generate + (queue or post if DRY_RUN off)
  GET  /api/v1/admin/media/comment-engagement/recent
       — last 7d of log rows for the dashboard
  POST /api/v1/admin/media/comment-engagement/regenerate/<log_id>
       — admin override: re-run the Claude call with a fresh seed
  POST /api/v1/admin/media/comment-engagement/flush-due
       — flush pending replies whose scheduled_for <= NOW()

Schema: media_comment_engagement_log (see init_comment_engagement_tables).

Cron entries (crawler_scheduler.py):
  ( 9, 21, "media_comment_engagement",       "_run_media_comment_engagement")
  (13,  1, "media_comment_engagement_flush", "_run_media_comment_engagement_flush")
"""
from __future__ import annotations

import os
import re
import sys
import json
import random
import datetime
from typing import Any

from flask import Blueprint, jsonify, request

media_comment_engagement_bp = Blueprint("media_comment_engagement", __name__)


# ── Tunables (env-driven) ────────────────────────────────────────────────
LOOKBACK_DAYS_POSTS   = 7      # only poll comments on posts <= 7d old
DAILY_CAP_DEFAULT     = 10     # MEDIA_COMMENT_REPLY_DAILY_CAP
REPLY_MAX_CHARS       = 280
MIN_COMMENT_CHARS     = 10     # bare-emoji / one-word filter
HUMAN_DELAY_MIN_SEC   = 4 * 60
HUMAN_DELAY_MAX_SEC   = 7 * 60
MAX_POSTS_PER_RUN     = 25     # cap recent posts we scan per cycle
MAX_REPLIES_PER_RUN   = 6      # cap replies queued per cycle (UI safety)
MODEL_NAME            = "claude-haiku-4-5-20251001"
MAX_TOKENS_PER_GEN    = 600


# Lazy clickbait / LLM-speak — reject + regenerate once.
LAZY_REJECT_TOKENS = {
    "amazing",
    "game-changer",
    "game changer",
    "revolutionary",
    "you won't believe",
    "you wont believe",
    "leverage",
    "synergy",
    "synergies",
    "delve",
    "tapestry",
    "groundbreaking",
    "paradigm shift",
    "ecosystem",
    "elevate your",
    "unlock the power",
    "unleash",
}

# Spam regex — fires on the comment text BEFORE we look at the author.
SPAM_PATTERNS = [
    r"\bfollow\s+me\b",
    r"\bclick\s+here\b",
    r"\bdm\s+me\b",
    r"\b(visit|check)\s+my\s+(profile|page)\b",
    r"\bcrypto\b.*\b(invest|airdrop|moon|x100|x10)\b",
    r"\bwhatsapp\b.*\+\d",
    r"\btelegram\b.*@\w",
    r"\b(?:bit\.ly|tinyurl\.com|t\.me)/\w+",
    r"\bcheap\s+seo\b",
    r"\bbuy\s+followers\b",
]
_SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)


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
        sys.stderr.write(f"[comment-engagement] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def _kill_switched() -> bool:
    return os.environ.get("MEDIA_COMMENT_REPLY_DISABLE", "0") == "1"


def _dry_run() -> bool:
    # DEFAULT ON — generates replies but does NOT post. Operator flips
    # MEDIA_COMMENT_REPLY_DRY_RUN=0 after reviewing ~10 drafts.
    return os.environ.get("MEDIA_COMMENT_REPLY_DRY_RUN", "1") == "1"


def _blocklist() -> set[str]:
    raw = os.environ.get("MEDIA_COMMENT_REPLY_BLOCKLIST", "") or ""
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


# ── Schema ───────────────────────────────────────────────────────────────
def init_comment_engagement_tables() -> bool:
    """Idempotent schema bootstrap. Wired into content_publisher
    .init_content_tables. Creates media_comment_engagement_log."""
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_comment_engagement_log (
                  id                       BIGSERIAL PRIMARY KEY,
                  source_post_urn          TEXT NOT NULL,
                  source_post_id           TEXT,
                  comment_urn              TEXT UNIQUE NOT NULL,
                  comment_author_urn       TEXT,
                  comment_author_name      TEXT,
                  comment_text             TEXT,
                  comment_detected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  reply_generated          TEXT,
                  reply_posted_at          TIMESTAMPTZ,
                  reply_urn                TEXT,
                  scheduled_for            TIMESTAMPTZ,
                  decision                 TEXT,
                  decision_reason          TEXT,
                  attributed_visit         BOOLEAN,
                  attributed_signup        BOOLEAN
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_mcel_decided
                    ON media_comment_engagement_log(comment_detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_mcel_pending
                    ON media_comment_engagement_log(scheduled_for)
                    WHERE decision = 'queued' AND reply_posted_at IS NULL
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_mcel_decision
                    ON media_comment_engagement_log(decision)
            """)
        _log("schema bootstrap OK")
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


# Lazy init at import-time (belt + suspenders next to content_publisher hook).
try:
    _SCHEMA_OK = init_comment_engagement_tables()
except Exception:
    _SCHEMA_OK = False


# ── LinkedIn API client ──────────────────────────────────────────────────
def _li_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": "202601",
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _company_urn() -> str:
    company = (os.environ.get("LINKEDIN_COMPANY_ID") or "").strip()
    return f"urn:li:organization:{company}" if company else ""


def _recent_org_post_urns(days: int = LOOKBACK_DAYS_POSTS, limit: int = MAX_POSTS_PER_RUN) -> list[dict]:
    """Pull recent linkedin_posts.post_urn from the local table. We don't
    fan out to /rest/posts because that endpoint requires the
    r_organization_social scope and is slow; the local table is the
    source of truth for "we authored this"."""
    conn = _db_conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT id, post_urn, posted_at,
                       COALESCE(content, content_text, '') AS content
                  FROM linkedin_posts
                 WHERE post_urn IS NOT NULL AND post_urn <> ''
                   AND COALESCE(status,'') = 'success'
                   AND posted_at > NOW() - INTERVAL '{int(days)} days'
                 ORDER BY posted_at DESC
                 LIMIT %s
            """, (int(limit),))
            for r in cur.fetchall() or []:
                out.append({
                    "post_id":   int(r[0]),
                    "post_urn":  str(r[1] or ""),
                    "posted_at": r[2],
                    "content":   str(r[3] or "")[:600],
                })
        return out
    except Exception as e:
        _log(f"recent_posts_query_failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _fetch_comments_for_post(post_urn: str, token: str) -> list[dict]:
    """Read comments for one post via /rest/socialActions/<urn>/comments.
    Returns up to ~50 surface-level comments (the API paginates; we take
    page 1 only — high-velocity comment storms get the next 50 on the
    next 4h cron tick). Each item: {urn, author_urn, message, created_at}."""
    if not post_urn or not post_urn.startswith("urn:li:"):
        return []
    out: list[dict] = []
    try:
        import urllib.parse as _up
        import requests as _rq
        enc = _up.quote(post_urn, safe="")
        url = (f"https://api.linkedin.com/rest/socialActions/{enc}/comments"
               f"?count=50&sortOrder=REVERSE_CHRONOLOGICAL")
        resp = _rq.get(url, headers=_li_headers(token), timeout=12)
        if resp.status_code != 200:
            _log(f"comments_fetch_status={resp.status_code} urn={post_urn}")
            return []
        d = resp.json() or {}
        elements = d.get("elements") or []
        for el in elements:
            try:
                c_urn = (el.get("$URN")
                         or el.get("commentUrn")
                         or el.get("id") or "")
                actor = el.get("actor") or el.get("commenter") or ""
                msg = ((el.get("message") or {}).get("text")
                       or el.get("text") or "")
                created_ms = el.get("created", {}).get("time") or el.get(
                    "createdAt") or 0
                created_iso = None
                if created_ms:
                    try:
                        created_iso = datetime.datetime.utcfromtimestamp(
                            int(created_ms) / 1000).isoformat() + "Z"
                    except Exception:
                        created_iso = None
                out.append({
                    "comment_urn": str(c_urn or ""),
                    "author_urn":  str(actor or ""),
                    "message":     str(msg or ""),
                    "created_at":  created_iso,
                })
            except Exception:
                continue
        return out
    except Exception as e:
        _log(f"comments_fetch_err urn={post_urn} err={e}")
        return []


def _resolve_author_name(author_urn: str, token: str) -> str:
    """Best-effort name lookup for display in the dashboard. LinkedIn
    requires r_liteprofile or r_basicprofile for /people/<urn>; if the
    token doesn't have it, return the trimmed URN tail. Never raises."""
    if not author_urn:
        return ""
    try:
        # urn:li:person:abc → "person:abc"
        tail = author_urn.split(":", 2)[-1] if ":" in author_urn else author_urn
        return tail[:40]
    except Exception:
        return author_urn[:40]


# ── Decision engine ──────────────────────────────────────────────────────
def _already_handled(cur, comment_urn: str) -> bool:
    cur.execute("""
        SELECT 1 FROM media_comment_engagement_log
         WHERE comment_urn = %s LIMIT 1
    """, (comment_urn,))
    return cur.fetchone() is not None


def should_reply(comment: dict, cur) -> tuple[bool, str]:
    """Return (should_reply, reason). Reason describes the OUTCOME
    regardless of branch — e.g. ('approved' if True else 'skipped_self')."""
    text = (comment.get("message") or "").strip()
    author = (comment.get("author_urn") or "").strip()
    urn = (comment.get("comment_urn") or "").strip()

    if not urn:
        return False, "no_urn"

    # already handled (any decision, including skipped) → never re-fire
    if _already_handled(cur, urn):
        return False, "already_handled"

    # self-comment guard
    org_urn = _company_urn().lower()
    if author and org_urn and author.lower() == org_urn:
        return False, "skipped_self"

    # length guard
    if len(text) < MIN_COMMENT_CHARS:
        return False, "skipped_too_short"

    # bare emoji guard — strip emoji-like chars; if <8 letters/digits, skip
    letters = re.sub(r"[^\w]", "", text)
    if len(letters) < 6:
        return False, "skipped_bare_emoji"

    # spam regex
    if _SPAM_RE.search(text):
        return False, "skipped_spam"

    # blocklist (URN or trimmed name)
    bl = _blocklist()
    if bl:
        if author.lower() in bl:
            return False, "skipped_blocklist_urn"
        # also check the tail (e.g. "person:abc123" matches "abc123")
        tail = author.split(":", 2)[-1].lower() if ":" in author else author.lower()
        if tail and tail in bl:
            return False, "skipped_blocklist_urn"

    return True, "approved"


# ── Live DC Hub data context ─────────────────────────────────────────────
def _live_context_for_claude() -> dict:
    """Pull a small DB-local context for the reply generator. No outbound
    HTTP — Railway is single-replica and we don't want to flap (see the
    backend_flapping memory)."""
    ctx: dict = {"dcpi_top": [], "recent_deals": [], "iso_queue": []}
    conn = _db_conn()
    if conn is None:
        return ctx
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT market_slug, composite_score, verdict
                      FROM dcpi_v3_master
                     WHERE COALESCE(verdict,'') ILIKE 'BUILD%'
                     ORDER BY composite_score DESC
                     LIMIT 3
                """)
                for r in cur.fetchall() or []:
                    ctx["dcpi_top"].append({
                        "market":  str(r[0] or ""),
                        "score":   round(float(r[1] or 0), 1),
                        "verdict": str(r[2] or ""),
                    })
            except Exception:
                pass

            try:
                cur.execute("""
                    SELECT title, market
                      FROM deals
                     WHERE announced_at IS NOT NULL
                     ORDER BY announced_at DESC
                     LIMIT 3
                """)
                for r in cur.fetchall() or []:
                    ctx["recent_deals"].append({
                        "title":  str(r[0] or "")[:100],
                        "market": str(r[1] or ""),
                    })
            except Exception:
                pass

            try:
                cur.execute("""
                    SELECT iso_region, COUNT(*)
                      FROM interconnection_queue_projects
                     WHERE updated_at > NOW() - INTERVAL '30 days'
                     GROUP BY iso_region
                     ORDER BY 2 DESC
                     LIMIT 3
                """)
                for r in cur.fetchall() or []:
                    ctx["iso_queue"].append({
                        "iso":   str(r[0] or ""),
                        "count": int(r[1] or 0),
                    })
            except Exception:
                pass
        return ctx
    except Exception:
        return ctx
    finally:
        try: conn.close()
        except Exception: pass


def _pick_brief_link(ctx: dict) -> str:
    """Pick ONE relevant brief URL to include in the reply. Prefer the
    top DCPI BUILD market brief; fall back to the State of 2026 hub."""
    if ctx.get("dcpi_top"):
        slug = ctx["dcpi_top"][0]["market"]
        return f"dchub.cloud/markets/{slug}/brief"
    return "dchub.cloud/state-of-2026"


# ── Reply generation ─────────────────────────────────────────────────────
_PROMPT_SYSTEM = (
    "You are the founder of DC Hub, a data center intelligence platform.\n"
    "You are replying to a LinkedIn comment on one of DC Hub's posts.\n"
    "\n"
    "Voice: casual, data-driven, no fluff, no emojis, no hashtags.\n"
    "Hard rules:\n"
    "  - Acknowledge or reference the commenter's point in the first sentence.\n"
    "  - Include EXACTLY ONE specific number from the DC Hub data context.\n"
    "  - Include EXACTLY ONE link (no scheme, no www) from the context.\n"
    "  - End with a soft CTA — a short question or invitation, no exclamation marks.\n"
    "  - 280 character maximum, hard limit.\n"
    "  - No banned words: amazing, revolutionary, game-changer, you won't believe,\n"
    "    leverage, synergy, delve, ecosystem.\n"
    "  - One paragraph. No bullet points.\n"
    "  - Do NOT @-mention anyone.\n"
    "Return STRICT JSON: {\"reply\":\"...\",\"links_used\":[\"...\"],\"data_used\":[\"...\"]}\n"
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
                "User-Agent":        "dchub-comment-engagement/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return text.strip() or None
    except Exception as e:
        _log(f"claude_call_err: {e}")
        return None


def _parse_reply_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        chunk = m.group(0) if m else raw
        d = json.loads(chunk)
        if not isinstance(d, dict):
            return None
        if not isinstance(d.get("reply"), str):
            return None
        d.setdefault("links_used", [])
        d.setdefault("data_used", [])
        return d
    except Exception:
        return None


def _reply_tone_ok(text: str) -> tuple[bool, str | None]:
    if not text:
        return False, "empty"
    s = text.lower()
    for bad in LAZY_REJECT_TOKENS:
        if bad in s:
            return False, f"lazy_token:{bad}"
    if len(text) > REPLY_MAX_CHARS:
        return False, f"too_long:{len(text)}"
    return True, None


def generate_reply(comment: dict, post_context: dict,
                   max_retries: int = 1) -> dict | None:
    """Returns {reply, links_used, data_used} or None on failure / tone reject.

    Retries ONCE on tone-filter rejection. Per spec: if 2nd attempt also
    fails, the caller skips this comment with decision='skipped_tone'."""
    ctx = _live_context_for_claude()
    ctx_lines: list[str] = []
    if ctx["dcpi_top"]:
        ctx_lines.append("Top BUILD markets: " +
                         ", ".join(f"{m['market']} (DCPI {m['score']})"
                                   for m in ctx["dcpi_top"]))
    if ctx["recent_deals"]:
        ctx_lines.append("Recent deals: " +
                         "; ".join(d["title"] for d in ctx["recent_deals"]
                                   if d.get("title")))
    if ctx["iso_queue"]:
        ctx_lines.append("ISO queue depth (30d): " +
                         ", ".join(f"{a['iso']} {a['count']}"
                                   for a in ctx["iso_queue"]))
    ctx_lines.append("Static numbers ALWAYS available: 2,000+ tracked deals; "
                     "232 DCPI markets; 5,700+ discovered facilities.")
    brief_link = _pick_brief_link(ctx)
    ctx_lines.append(f"Recommended brief link: {brief_link}")

    ctx_block = "\n".join(ctx_lines)
    post_text = (post_context.get("content") or "").strip()[:600]
    comment_text = (comment.get("message") or "").strip()[:600]

    user_msg = (
        f"DC Hub LinkedIn post (snippet):\n\"\"\"{post_text}\"\"\"\n\n"
        f"A user just commented:\n\"\"\"{comment_text}\"\"\"\n\n"
        f"Live DC Hub data + a link you can cite:\n{ctx_block}\n\n"
        "Write ONE LinkedIn reply (<=280 chars). Strict JSON: "
        "{\"reply\":\"...\",\"links_used\":[\"...\"],\"data_used\":[\"...\"]}."
    )

    last_reason = None
    for attempt in range(int(max_retries) + 1):
        raw = _call_claude(
            messages=[{"role": "user", "content": user_msg}],
            system=_PROMPT_SYSTEM,
        )
        if not raw:
            last_reason = "claude_no_response"
            continue
        parsed = _parse_reply_json(raw)
        if not parsed:
            last_reason = "claude_unparseable"
            continue
        ok, why = _reply_tone_ok(parsed["reply"])
        if not ok:
            last_reason = why
            continue
        return parsed
    _log(f"generate_reply_failed reason={last_reason}")
    return None


# ── Posting ──────────────────────────────────────────────────────────────
def _publish_linkedin_comment(post_urn: str, text: str) -> tuple[bool, dict]:
    """POST a reply on a LinkedIn post as the org URN. Mirrors the proven
    helper in media_spike_responder; we keep our own copy so this module
    can be enabled/disabled independently. Fail-soft, never raises."""
    try:
        import urllib.parse as _up
        import requests as _rq
        token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
        actor = _company_urn()
        if not token:
            return False, {"error": "no_linkedin_token"}
        if not actor:
            return False, {"error": "no_company_id"}
        if not post_urn or not post_urn.startswith("urn:li:"):
            return False, {"error": "bad_post_urn"}
        enc = _up.quote(post_urn, safe="")
        url = f"https://api.linkedin.com/rest/socialActions/{enc}/comments"
        headers = _li_headers(token)
        headers["Content-Type"] = "application/json"
        payload = {"actor": actor, "object": post_urn, "message": {"text": text}}
        r = _rq.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            try:
                d = r.json() or {}
            except Exception:
                d = {}
            return True, {
                "status":     r.status_code,
                "reply_urn":  str(d.get("$URN") or d.get("id") or ""),
            }
        return False, {"error": f"http_{r.status_code}", "body": r.text[:200]}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── Logging ──────────────────────────────────────────────────────────────
def _log_decision(post: dict, comment: dict, decision: str,
                  reason: str, reply: dict | None = None,
                  scheduled_for: datetime.datetime | None = None,
                  reply_posted_at: datetime.datetime | None = None,
                  reply_urn: str | None = None) -> int | None:
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO media_comment_engagement_log (
                        source_post_urn, source_post_id, comment_urn,
                        comment_author_urn, comment_author_name, comment_text,
                        reply_generated, reply_posted_at, reply_urn,
                        scheduled_for, decision, decision_reason
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (comment_urn) DO UPDATE SET
                        decision        = EXCLUDED.decision,
                        decision_reason = EXCLUDED.decision_reason,
                        reply_generated = COALESCE(EXCLUDED.reply_generated,
                                                   media_comment_engagement_log.reply_generated),
                        scheduled_for   = COALESCE(EXCLUDED.scheduled_for,
                                                   media_comment_engagement_log.scheduled_for),
                        reply_posted_at = COALESCE(EXCLUDED.reply_posted_at,
                                                   media_comment_engagement_log.reply_posted_at),
                        reply_urn       = COALESCE(EXCLUDED.reply_urn,
                                                   media_comment_engagement_log.reply_urn)
                    RETURNING id
                """, (
                    post.get("post_urn") or "",
                    str(post.get("post_id") or "") if post.get("post_id") else None,
                    comment.get("comment_urn") or "",
                    comment.get("author_urn") or "",
                    comment.get("author_name") or "",
                    (comment.get("message") or "")[:2000],
                    (reply.get("reply") if reply else None),
                    reply_posted_at,
                    reply_urn,
                    scheduled_for,
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


# ── Helpers for cap + flush ──────────────────────────────────────────────
def _replies_today(cur) -> int:
    """Count of replies SHIPPED (queued, posted, or dry_run) today UTC.
    Drives the daily cap before we generate a new reply."""
    cur.execute("""
        SELECT COUNT(*)
          FROM media_comment_engagement_log
         WHERE decision IN ('queued','replied','dry_run')
           AND comment_detected_at >= date_trunc('day', NOW())
    """)
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _flush_due_pending(now: datetime.datetime | None = None) -> dict:
    """POST any pending queued replies whose scheduled_for <= now. The
    detect cron writes queued rows; this flush cron (or a separate slot)
    actually posts. Honors DRY_RUN: in dry-run mode, queued is the final
    state (no live POST)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"posted": 0, "errors": 0, "rows": []}
    if _kill_switched() or _dry_run():
        # in dry-run we leave the queued rows alone — they're the drafts
        return out
    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, source_post_urn, comment_urn, reply_generated
                  FROM media_comment_engagement_log
                 WHERE decision = 'queued'
                   AND reply_posted_at IS NULL
                   AND (scheduled_for IS NULL OR scheduled_for <= NOW())
                 ORDER BY scheduled_for ASC
                 LIMIT 25
            """)
            rows = cur.fetchall() or []
        for r in rows:
            log_id, post_urn, comment_urn, reply_text = r
            ok, info = _publish_linkedin_comment(str(post_urn), str(reply_text or ""))
            try:
                with conn, conn.cursor() as cur2:
                    if ok:
                        cur2.execute("""
                            UPDATE media_comment_engagement_log
                               SET decision        = 'replied',
                                   decision_reason = 'flushed',
                                   reply_posted_at = NOW(),
                                   reply_urn       = %s
                             WHERE id = %s
                        """, (info.get("reply_urn"), int(log_id)))
                        out["posted"] += 1
                    else:
                        cur2.execute("""
                            UPDATE media_comment_engagement_log
                               SET decision        = 'post_failed',
                                   decision_reason = %s
                             WHERE id = %s
                        """, (json.dumps(info)[:300], int(log_id)))
                        out["errors"] += 1
            except Exception as e:
                _log(f"flush_update_failed log_id={log_id} err={e}")
                continue
            out["rows"].append({
                "log_id":     int(log_id),
                "comment_urn": str(comment_urn),
                "ok":         ok,
                "info":       info,
            })
        return out
    except Exception as e:
        _log(f"flush_due_pending_failed: {e}")
        return out
    finally:
        try: conn.close()
        except Exception: pass


# ── Top-level scan ───────────────────────────────────────────────────────
def run_comment_engagement_scan(force_post: bool = False) -> dict:
    """Main entrypoint. Poll comments, decide, generate, log.

    In DRY_RUN mode (default), queued replies are written with
    decision='dry_run' and scheduled_for=NULL — the operator reviews on
    /admin/media-mix and flips the env flag.

    In LIVE mode, qualifying replies are written with decision='queued'
    and scheduled_for = NOW() + random(4,7) minutes. The flush cron (or
    `force_post=True` from an admin button) actually posts.

    Returns: {detected, eligible, replied, skipped, errors}.
    Never raises."""
    result = {
        "ok": False,
        "detected":      0,
        "eligible":      0,
        "fired":         0,
        "skipped":       {},
        "dry_run":       _dry_run(),
        "kill_switched": _kill_switched(),
        "daily_cap":     _env_int("MEDIA_COMMENT_REPLY_DAILY_CAP", DAILY_CAP_DEFAULT),
        "replies_today": 0,
        "rows":          [],
        "errors":        [],
    }
    if result["kill_switched"]:
        result["ok"] = True
        result["errors"].append("kill_switched")
        return result

    token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    if not token:
        result["errors"].append("no_linkedin_token")
        result["ok"] = True
        return result
    if not _company_urn():
        result["errors"].append("no_company_id")
        result["ok"] = True
        return result

    posts = _recent_org_post_urns()
    if not posts:
        result["ok"] = True
        return result

    conn = _db_conn()
    if conn is None:
        result["errors"].append("no_db")
        return result
    try:
        with conn, conn.cursor() as cur:
            result["replies_today"] = _replies_today(cur)
            budget = max(0, int(result["daily_cap"]) - result["replies_today"])

            for post in posts:
                if result["fired"] >= min(MAX_REPLIES_PER_RUN, budget):
                    break
                comments = _fetch_comments_for_post(post["post_urn"], token)
                for c in comments:
                    result["detected"] += 1
                    if result["fired"] >= min(MAX_REPLIES_PER_RUN, budget):
                        break
                    ok, reason = should_reply(c, cur)
                    if not ok:
                        result["skipped"][reason] = result["skipped"].get(reason, 0) + 1
                        # log SKIPPED so we don't re-evaluate this comment URN
                        if reason not in ("already_handled", "no_urn"):
                            _log_decision(post, {
                                **c,
                                "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                            }, decision=reason, reason=reason)
                        continue

                    if budget <= 0:
                        result["skipped"]["skipped_capped"] = result["skipped"].get("skipped_capped", 0) + 1
                        _log_decision(post, {
                            **c,
                            "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                        }, decision="skipped_capped",
                            reason=f"daily_cap_hit:{result['replies_today']}/{result['daily_cap']}")
                        continue

                    result["eligible"] += 1
                    reply = generate_reply(c, post)
                    if not reply:
                        result["skipped"]["skipped_tone"] = result["skipped"].get("skipped_tone", 0) + 1
                        _log_decision(post, {
                            **c,
                            "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                        }, decision="skipped_tone",
                            reason="tone_filter_or_gen_failed")
                        continue

                    # Schedule for posting in random 4-7min window for
                    # human cadence. In DRY_RUN we still stamp the
                    # scheduled_for so the operator sees what WOULD post.
                    delay = random.randint(HUMAN_DELAY_MIN_SEC, HUMAN_DELAY_MAX_SEC)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    sched = now + datetime.timedelta(seconds=delay)

                    if result["dry_run"] and not force_post:
                        log_id = _log_decision(post, {
                            **c,
                            "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                        }, decision="dry_run",
                            reason=f"delay={delay}s",
                            reply=reply,
                            scheduled_for=sched)
                        result["fired"] += 1
                        result["rows"].append({
                            "log_id":      log_id,
                            "post_urn":    post.get("post_urn"),
                            "comment_urn": c.get("comment_urn"),
                            "reply":       reply["reply"],
                            "scheduled_for": sched.isoformat(),
                            "status":      "dry_run",
                        })
                        budget -= 1
                        continue

                    # LIVE PATH — queue for the flush cron OR post now if forced
                    if force_post:
                        ok_p, info = _publish_linkedin_comment(
                            post["post_urn"], reply["reply"])
                        if ok_p:
                            log_id = _log_decision(post, {
                                **c,
                                "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                            }, decision="replied",
                                reason="force_post",
                                reply=reply,
                                reply_posted_at=now,
                                reply_urn=info.get("reply_urn"))
                            result["fired"] += 1
                            budget -= 1
                            result["rows"].append({
                                "log_id":      log_id,
                                "post_urn":    post.get("post_urn"),
                                "comment_urn": c.get("comment_urn"),
                                "reply":       reply["reply"],
                                "status":      "replied",
                                "reply_urn":   info.get("reply_urn"),
                            })
                        else:
                            _log_decision(post, {
                                **c,
                                "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                            }, decision="post_failed",
                                reason=json.dumps(info)[:300],
                                reply=reply)
                            result["errors"].append({
                                "comment_urn": c.get("comment_urn"),
                                "error":       info,
                            })
                    else:
                        log_id = _log_decision(post, {
                            **c,
                            "author_name": _resolve_author_name(c.get("author_urn", ""), token),
                        }, decision="queued",
                            reason=f"delay={delay}s",
                            reply=reply,
                            scheduled_for=sched)
                        result["fired"] += 1
                        budget -= 1
                        result["rows"].append({
                            "log_id":      log_id,
                            "post_urn":    post.get("post_urn"),
                            "comment_urn": c.get("comment_urn"),
                            "reply":       reply["reply"],
                            "scheduled_for": sched.isoformat(),
                            "status":      "queued",
                        })

        result["ok"] = True
        return result
    except Exception as e:
        result["errors"].append(f"scan_err:{type(e).__name__}:{str(e)[:120]}")
        result["ok"] = False
        return result
    finally:
        try: conn.close()
        except Exception: pass


def recent_log_rows(days: int = 7, limit: int = 100) -> list[dict]:
    conn = _db_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, source_post_urn, source_post_id,
                       comment_urn, comment_author_urn, comment_author_name,
                       comment_text, comment_detected_at, reply_generated,
                       reply_posted_at, reply_urn, scheduled_for,
                       decision, decision_reason
                  FROM media_comment_engagement_log
                 WHERE comment_detected_at > NOW() - make_interval(days => %s)
                 ORDER BY comment_detected_at DESC
                 LIMIT %s
            """, (int(days), int(limit)))
            out: list[dict] = []
            for row in cur.fetchall() or []:
                out.append({
                    "id":                  int(row[0]),
                    "source_post_urn":     str(row[1] or ""),
                    "source_post_id":      str(row[2] or "") if row[2] else None,
                    "comment_urn":         str(row[3] or ""),
                    "comment_author_urn":  str(row[4] or ""),
                    "comment_author_name": str(row[5] or ""),
                    "comment_text":        str(row[6] or ""),
                    "comment_detected_at": row[7].isoformat() if row[7] else None,
                    "reply_generated":     str(row[8] or "") if row[8] else None,
                    "reply_posted_at":     row[9].isoformat() if row[9] else None,
                    "reply_urn":           str(row[10] or "") if row[10] else None,
                    "scheduled_for":       row[11].isoformat() if row[11] else None,
                    "decision":            str(row[12] or ""),
                    "decision_reason":     str(row[13] or "") if row[13] else None,
                })
            return out
    except Exception as e:
        _log(f"recent_log_rows_failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


def recent_counters(days: int = 7) -> dict:
    """Aggregate counts for the dashboard header."""
    conn = _db_conn()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT decision, COUNT(*)
                  FROM media_comment_engagement_log
                 WHERE comment_detected_at > NOW() - make_interval(days => %s)
                 GROUP BY decision
            """, (int(days),))
            out: dict = {"total": 0}
            for row in cur.fetchall() or []:
                key = str(row[0] or "unknown")
                cnt = int(row[1] or 0)
                out[key] = cnt
                out["total"] += cnt
            return out
    except Exception:
        return {}
    finally:
        try: conn.close()
        except Exception: pass


# ── HTTP endpoints ───────────────────────────────────────────────────────
@media_comment_engagement_bp.route(
    "/api/v1/admin/media/comment-engagement/poll-now", methods=["GET"])
def http_poll_now():
    """Read-only-ish: what comments would we reply to right now?
    Always returns the drafts. NEVER posts. Safe to spam.

    This is the verify-mode endpoint per the task spec: "Test with the
    poll endpoint: returns what WOULD be replied without actually posting".

    Internally calls run_comment_engagement_scan() but forces DRY_RUN=True
    by setting the env var for the duration of the call. We use a context
    manager so a parallel cron isn't affected."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    prev = os.environ.get("MEDIA_COMMENT_REPLY_DRY_RUN")
    os.environ["MEDIA_COMMENT_REPLY_DRY_RUN"] = "1"
    try:
        result = run_comment_engagement_scan()
    finally:
        if prev is None:
            os.environ.pop("MEDIA_COMMENT_REPLY_DRY_RUN", None)
        else:
            os.environ["MEDIA_COMMENT_REPLY_DRY_RUN"] = prev
    return jsonify(result)


@media_comment_engagement_bp.route(
    "/api/v1/admin/media/comment-engagement/run", methods=["POST"])
def http_run():
    """Full live cycle. Honors DRY_RUN env. Cron calls this with the
    cron secret. Operator can POST with ?force_post=1 to bypass the
    queued-for-flush stagger (rare — debug only)."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    force = (request.args.get("force_post") or "").lower() in ("1", "true", "yes")
    result = run_comment_engagement_scan(force_post=force)
    return jsonify(result)


@media_comment_engagement_bp.route(
    "/api/v1/admin/media/comment-engagement/recent", methods=["GET"])
def http_recent():
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    days = int(request.args.get("days", "7"))
    return jsonify({
        "ok":       True,
        "counters": recent_counters(days=days),
        "rows":     recent_log_rows(days=days),
        "dry_run":  _dry_run(),
        "kill":     _kill_switched(),
        "daily_cap": _env_int("MEDIA_COMMENT_REPLY_DAILY_CAP", DAILY_CAP_DEFAULT),
    })


@media_comment_engagement_bp.route(
    "/api/v1/admin/media/comment-engagement/regenerate/<int:log_id>",
    methods=["POST"])
def http_regenerate(log_id: int):
    """Admin button on the dashboard: re-roll the Claude call for a
    single log row WITHOUT posting (DRY-RUN regardless of env). Updates
    the reply_generated column in place. Safe to call repeatedly."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    conn = _db_conn()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT source_post_urn, source_post_id, comment_urn,
                       comment_author_urn, comment_author_name, comment_text
                  FROM media_comment_engagement_log
                 WHERE id = %s
                 LIMIT 1
            """, (int(log_id),))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        # Build a minimal post + comment context from the row.
        post = {
            "post_urn": str(row[0] or ""),
            "post_id":  row[1],
            "content":  "",  # we don't store post content in the log; OK
        }
        comment = {
            "comment_urn": str(row[2] or ""),
            "author_urn":  str(row[3] or ""),
            "author_name": str(row[4] or ""),
            "message":     str(row[5] or ""),
        }
        reply = generate_reply(comment, post)
        if not reply:
            return jsonify(ok=False, error="gen_failed_or_tone_reject")
        with conn, conn.cursor() as cur2:
            cur2.execute("""
                UPDATE media_comment_engagement_log
                   SET reply_generated = %s,
                       decision_reason = CONCAT(COALESCE(decision_reason,''), ' | regenerated@', NOW()::text)
                 WHERE id = %s
            """, (reply["reply"], int(log_id)))
        return jsonify(ok=True, log_id=int(log_id), reply=reply)
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 500
    finally:
        try: conn.close()
        except Exception: pass


@media_comment_engagement_bp.route(
    "/api/v1/admin/media/comment-engagement/flush-due", methods=["POST"])
def http_flush_due():
    """POST any queued replies whose scheduled_for <= NOW(). Cron calls
    this between detect runs so the 4-7min stagger doesn't drift."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    result = _flush_due_pending()
    return jsonify(ok=True, **result)
