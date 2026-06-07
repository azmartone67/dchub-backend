"""
media_spike_responder.py — DC Hub Media ROUND 2 (2026-06-07).

Engagement-spike → auto-generated response thread.

When a LinkedIn post (last 24h) crosses 2x the user's 30d baseline
impressions in under 6 hours of being published, this module:

  1. picks the post + builds a live data context (DCPI movers, recent
     deal velocity, grid headroom from the cached snapshots already on
     the box — no synchronous outbound calls);
  2. asks Claude to draft 3 short follow-up comments that:
       - reference the trending data point from the original post
       - add NEW supporting data (live numbers from DC Hub)
       - end with a soft CTA ("want the data? dchub.cloud")
       - stay in the founder's voice (casual, data-driven, no fluff)
       - reject lazy LLM-speak via a 12-word reject list
                    (auto-regenerate up to 2x);
  3. stamps a `linkedin_posts.autoresponse_triggered_at` so the same
     post can never re-fire (once-per-post rate limit);
  4. caps to 3 auto-amplifications per ROLLING 7-day window
     (MEDIA_AUTORESPONSE_WEEKLY_CAP env, default 3);
  5. logs to media_autoresponse_log with the comment texts and
     scheduled publish offsets (30 / 90 / 180 min staggered).

Publishing path:
  - In DRY-RUN mode (MEDIA_AUTORESPONSE_DRY_RUN=1), the comments are
    written to media_autoresponse_log with status='dry_run' and
    NOT posted.
  - In live mode, the LinkedIn /rest/socialActions/{post_urn}/comments
    endpoint is called with a staggered ETA per comment.
    Round-1 ships in DRY-RUN — the operator reads back the drafts on
    /admin/media-mix → "Auto-Response Activity", then flips the flag.

Safety rails (all OFF by default = SAFE):
  - MEDIA_AUTORESPONSE_DISABLE=1 → master kill switch.
  - MEDIA_AUTORESPONSE_DRY_RUN=1 → generate but don't post.
  - MEDIA_AUTORESPONSE_WEEKLY_CAP=3 → max amplifications per 7-day
    rolling window; defaults to 3.
  - Per-post once-only via autoresponse_triggered_at column on
    linkedin_posts.
  - Tone-check: reject any comment containing lazy clickbait words
    (auto-regenerate). Reject list curated below — keep tight.
  - Stagger: 30 / 90 / 180 minutes between comment posts — looks
    human, well inside LinkedIn's rate-limit floor.

Endpoints (all admin-keyed):
  GET  /api/v1/admin/media/spikes/preview        — what would fire NOW?
  POST /api/v1/admin/media/spikes/detect         — run a real detect+gen
  GET  /api/v1/admin/media/autoresponse/recent   — last 30d log
  POST /api/v1/admin/media/autoresponse/publish/<log_id>  — flush a dry-run draft (admin override)

Cron entry: twice daily at 10/22 UTC via crawler_scheduler:
  ( 10, 22, "media_spike_responder", "_run_media_spike_responder")
"""
from __future__ import annotations

import os
import re
import sys
import json
import datetime
from typing import Any

from flask import Blueprint, jsonify, request


media_spike_responder_bp = Blueprint("media_spike_responder", __name__)


# ── Tunables (env-driven) ────────────────────────────────────────────────
SPIKE_RATIO          = 2.0    # impressions / baseline ≥ this fires
SPIKE_WINDOW_HOURS   = 6.0    # ONLY posts <6h old qualify
BASELINE_LOOKBACK_D  = 30     # 30-day rolling baseline
WEEKLY_CAP_DEFAULT   = 3      # MEDIA_AUTORESPONSE_WEEKLY_CAP
CTA_URL              = "dchub.cloud"
MODEL_NAME           = "claude-haiku-4-5-20251001"   # cheap + fast — 3 short comments
MAX_TOKENS_PER_GEN   = 800
COMMENT_MAX_CHARS    = 280

# Stagger offsets (minutes) — looks human, stays well under LinkedIn rate caps
STAGGER_OFFSETS_MIN = [30, 90, 180]


# Lazy clickbait words — any one of these in a generated comment auto-rejects it.
# Keep tight: real phrasing like "huge" should slip through; only
# obvious LLM-speak / hype gets caught.
LAZY_REJECT_TOKENS = {
    "amazing",
    "game-changer",
    "game changer",
    "revolutionary",
    "you won't believe",
    "you wont believe",
    "literally",
    "leverage",
    "synergy",
    "synergies",
    "delve",
    "tapestry",
    "groundbreaking",
    "paradigm shift",
    "in this fast-paced",
    "in today's fast-paced",
    "elevate your",
    "unlock the power",
    "unleash",
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
        sys.stderr.write(f"[spike-responder] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def _kill_switched() -> bool:
    return os.environ.get("MEDIA_AUTORESPONSE_DISABLE", "0") == "1"


def _dry_run() -> bool:
    # Default DRY-RUN = ON, so the very first deploy observes spikes and
    # generates drafts without posting. Operator flips the flag once
    # comfortable.
    return os.environ.get("MEDIA_AUTORESPONSE_DRY_RUN", "1") == "1"


# ── Schema ───────────────────────────────────────────────────────────────
def init_spike_responder_tables() -> bool:
    """Idempotent schema bootstrap. Wired into content_publisher.init_content_tables.
    Adds the autoresponse_triggered_at column to linkedin_posts and creates
    the media_autoresponse_log table."""
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            # Per-post once-only marker — prevents the same post from
            # being amplified twice. Idempotent ALTER.
            try:
                cur.execute("""
                    ALTER TABLE linkedin_posts
                      ADD COLUMN IF NOT EXISTS autoresponse_triggered_at TIMESTAMPTZ
                """)
            except Exception as e:
                _log(f"linkedin_posts.autoresponse_triggered_at add skipped: {e}")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_autoresponse_log (
                    id                          BIGSERIAL PRIMARY KEY,
                    source_post_id              BIGINT,
                    source_post_urn             TEXT,
                    source_platform             TEXT NOT NULL DEFAULT 'linkedin',
                    spike_detected_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    impressions_before          BIGINT,
                    impressions_after           BIGINT,
                    baseline_impressions        NUMERIC,
                    viral_ratio                 NUMERIC,
                    comments                    JSONB NOT NULL DEFAULT '[]'::jsonb,
                    profile_visits_attributed   INTEGER,
                    traffic_attributed          INTEGER,
                    status                      TEXT NOT NULL DEFAULT 'pending',
                    error                       TEXT,
                    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_post_urn)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_autoresponse_log_recent_idx
                    ON media_autoresponse_log(spike_detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_autoresponse_log_status_idx
                    ON media_autoresponse_log(status)
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
    _SCHEMA_OK = init_spike_responder_tables()
except Exception:
    _SCHEMA_OK = False


# ── Detection ────────────────────────────────────────────────────────────
def _compute_baseline(cur) -> float | None:
    """30-day average impressions across non-zero linkedin_posts."""
    try:
        cur.execute(f"""
            SELECT AVG(impressions)::float
              FROM linkedin_posts
             WHERE posted_at > NOW() - INTERVAL '{BASELINE_LOOKBACK_D} days'
               AND impressions IS NOT NULL
               AND impressions > 0
        """)
        row = cur.fetchone()
        if not row:
            return None
        return float(row[0]) if row[0] is not None else None
    except Exception as e:
        _log(f"baseline_compute_failed: {e}")
        return None


def _weekly_used(cur) -> int:
    """Count of fired auto-responses in the past 7 days
    (status in ('queued','published','dry_run'))."""
    try:
        cur.execute("""
            SELECT COUNT(*)
              FROM media_autoresponse_log
             WHERE spike_detected_at > NOW() - INTERVAL '7 days'
               AND status IN ('queued','published','dry_run')
        """)
        row = cur.fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def detect_spikes(limit: int = 5) -> list[dict]:
    """Find LinkedIn posts in last 24h that crossed the spike threshold
    and haven't been auto-responded to yet. Returns up to `limit` dicts
    sorted by viral_ratio DESC.

    Schema-defensive: silently returns [] if linkedin_posts.impressions
    or autoresponse_triggered_at column is missing."""
    conn = _db_conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn, conn.cursor() as cur:
            baseline = _compute_baseline(cur)
            if not baseline or baseline <= 0:
                return []
            try:
                cur.execute(f"""
                    SELECT id,
                           COALESCE(post_urn, '')                       AS post_urn,
                           COALESCE(content, content_text, '')          AS content,
                           posted_at,
                           impressions,
                           EXTRACT(EPOCH FROM (NOW() - posted_at))/3600.0 AS age_h
                      FROM linkedin_posts
                     WHERE posted_at > NOW() - INTERVAL '24 hours'
                       AND posted_at < NOW() - INTERVAL '30 minutes'
                       AND impressions IS NOT NULL
                       AND impressions >= %s
                       AND autoresponse_triggered_at IS NULL
                     ORDER BY (impressions / NULLIF(%s, 0)) DESC
                     LIMIT %s
                """, (int(SPIKE_RATIO * baseline), float(baseline), int(limit) * 4))
                for row in cur.fetchall() or []:
                    pid, urn, content, posted_at, impressions, age_h = row
                    if age_h is None or float(age_h) > SPIKE_WINDOW_HOURS:
                        continue
                    ratio = float(impressions) / float(baseline) if baseline else 0.0
                    if ratio < SPIKE_RATIO:
                        continue
                    out.append({
                        "post_id":     int(pid),
                        "post_urn":    str(urn or ""),
                        "content":     str(content or ""),
                        "posted_at":   posted_at,
                        "impressions": int(impressions),
                        "age_hours":   float(age_h),
                        "viral_ratio": round(ratio, 2),
                        "baseline":    round(baseline, 1),
                    })
                    if len(out) >= int(limit):
                        break
            except Exception as e:
                _log(f"spike_query_failed: {e}")
                return []
        return out
    except Exception as e:
        _log(f"detect_spikes_failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


# ── Live data context ────────────────────────────────────────────────────
def _live_context_for_claude() -> dict:
    """Pull a small, FAST, DB-local data context for the comment generator.
    Avoid synchronous outbound HTTP — Railway is one replica and the brain
    hammers slow endpoints (see backend_flapping memory).

    Returns: {dcpi_top: [...], recent_deals: [...], grid_alerts: [...]}.
    Each field is best-effort; missing fields just shorten the prompt."""
    ctx: dict = {"dcpi_top": [], "recent_deals": [], "grid_alerts": []}
    conn = _db_conn()
    if conn is None:
        return ctx
    try:
        with conn, conn.cursor() as cur:
            # Top 3 DCPI BUILD-verdict movers.
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
                # Try the simpler verdict view; if neither table is named,
                # ctx just stays empty — Claude prompt falls back gracefully.
                try:
                    cur.execute("""
                        SELECT market_slug, score
                          FROM dcpi_market_scores
                         ORDER BY score DESC
                         LIMIT 3
                    """)
                    for r in cur.fetchall() or []:
                        ctx["dcpi_top"].append({
                            "market": str(r[0] or ""),
                            "score":  round(float(r[1] or 0), 1),
                            "verdict": "BUILD",
                        })
                except Exception:
                    pass

            # 3 most recent verified deals (used as "12% growth" style stats).
            try:
                cur.execute("""
                    SELECT title, market, value_usd, announced_at
                      FROM deals
                     WHERE announced_at IS NOT NULL
                     ORDER BY announced_at DESC
                     LIMIT 3
                """)
                for r in cur.fetchall() or []:
                    ctx["recent_deals"].append({
                        "title":     str(r[0] or "")[:120],
                        "market":    str(r[1] or ""),
                        "value_usd": float(r[2] or 0) if r[2] else None,
                    })
            except Exception:
                pass

            # Latest grid_alerts / interconnection_queue snapshot.
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
                    ctx["grid_alerts"].append({
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


# ── Comment generation ──────────────────────────────────────────────────
_PROMPT_SYSTEM = (
    "You are the founder of DC Hub, a data center intelligence platform.\n"
    "Voice: casual, sharp, data-driven, no fluff, no clickbait, no emojis.\n"
    "Each comment must:\n"
    "  - reference the trending data point from the original post,\n"
    "  - add ONE new supporting number from the live DC Hub data context\n"
    "    (DCPI movers / recent deal velocity / grid headroom),\n"
    "  - end with a soft CTA pointing readers to dchub.cloud.\n"
    "Hard constraints:\n"
    "  - 280 character maximum per comment.\n"
    "  - No hype words: amazing, revolutionary, game-changer, you won't believe.\n"
    "  - No corporate jargon: leverage, synergy, delve, paradigm shift.\n"
    "  - Three (3) comments, distinct angles, no repetition.\n"
    "Return STRICT JSON: [{\"comment\":\"...\"},{\"comment\":\"...\"},{\"comment\":\"...\"}]\n"
    "Nothing outside the JSON array."
)


def _comment_tone_ok(text: str) -> tuple[bool, str | None]:
    """Returns (ok, reason_if_bad). Lowercases + token-matches the lazy
    LLM-speak reject list. Fast — no regex pyrotechnics."""
    if not text:
        return False, "empty"
    s = text.lower()
    for bad in LAZY_REJECT_TOKENS:
        if bad in s:
            return False, f"lazy_token:{bad}"
    if len(text) > COMMENT_MAX_CHARS:
        return False, f"too_long:{len(text)}"
    return True, None


def _call_claude(messages: list[dict], system: str) -> str | None:
    """Anthropic Messages API — fail-soft. Returns the text content of
    the first text block, or None on any error."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        _log("claude: no ANTHROPIC_API_KEY")
        return None
    try:
        import urllib.request, urllib.error
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
                "User-Agent":        "dchub-spike-responder/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return text.strip() or None
    except Exception as e:
        _log(f"claude_call_err: {e}")
        return None


def _parse_comments_json(raw: str) -> list[str]:
    """Pull the JSON array out of the LLM response. The system prompt tells
    Claude to return strict JSON, but we still handle stray prose around it."""
    if not raw:
        return []
    try:
        m = re.search(r"\[\s*\{.*\}\s*\]", raw, flags=re.DOTALL)
        chunk = m.group(0) if m else raw
        arr = json.loads(chunk)
        out: list[str] = []
        for item in arr if isinstance(arr, list) else []:
            if isinstance(item, dict) and isinstance(item.get("comment"), str):
                out.append(item["comment"].strip())
            elif isinstance(item, str):
                out.append(item.strip())
        return out[:3]
    except Exception as e:
        _log(f"parse_comments_err: {e}")
        return []


def generate_response_thread(post: dict, max_retries: int = 2) -> list[dict]:
    """Build the 3-comment thread for a spike post. Returns a list of:
        [{"comment": str, "schedule_at": iso8601}, ...]
    Empty list if generation fails or all retries get rejected by tone.

    Retries up to `max_retries` times if a generated comment fails the
    tone check (lazy LLM-speak)."""
    ctx = _live_context_for_claude()
    # Compact the live data into a readable few-liner the LLM can quote.
    ctx_lines: list[str] = []
    if ctx["dcpi_top"]:
        ctx_lines.append("Top BUILD markets right now: " +
                         ", ".join(f"{m['market']} ({m['score']})"
                                   for m in ctx["dcpi_top"]))
    if ctx["recent_deals"]:
        ctx_lines.append("Last 3 verified deals: " +
                         "; ".join(d["title"] for d in ctx["recent_deals"]
                                   if d.get("title")))
    if ctx["grid_alerts"]:
        ctx_lines.append("30d queue activity: " +
                         ", ".join(f"{a['iso']} {a['count']}"
                                   for a in ctx["grid_alerts"]))
    ctx_block = "\n".join(ctx_lines) if ctx_lines else (
        "DC Hub tracks 2,000+ deals across 232 markets — cite the platform.")

    post_text = (post.get("content") or "").strip()[:1200]
    impressions = int(post.get("impressions") or 0)
    ratio = float(post.get("viral_ratio") or 0)

    user_msg = (
        f"Original LinkedIn post (just hit {impressions:,} impressions, "
        f"{ratio:.1f}x our baseline):\n\n"
        f"\"\"\"{post_text}\"\"\"\n\n"
        f"Live DC Hub data you can cite:\n{ctx_block}\n\n"
        "Generate 3 short LinkedIn comments to continue this thread. "
        "Each comment is its own message — they will be posted "
        "30 / 90 / 180 minutes apart. Each MUST end with the soft CTA "
        f"pointing to {CTA_URL}. JSON array of 3 objects, "
        "each with a `comment` field. Strict JSON only."
    )

    comments: list[str] = []
    for attempt in range(int(max_retries) + 1):
        raw = _call_claude(
            messages=[{"role": "user", "content": user_msg}],
            system=_PROMPT_SYSTEM,
        )
        if not raw:
            break
        parsed = _parse_comments_json(raw)
        if len(parsed) < 3:
            _log(f"parse_too_few attempt={attempt} got={len(parsed)}")
            continue
        # Tone-check each comment; if any fails, retry the whole batch.
        rejected = []
        for c in parsed:
            ok, why = _comment_tone_ok(c)
            if not ok:
                rejected.append((c[:60], why))
        if rejected:
            _log(f"tone_reject attempt={attempt} rejected={rejected[:2]}")
            continue
        comments = parsed
        break

    if not comments:
        return []

    now = datetime.datetime.utcnow().replace(microsecond=0)
    out: list[dict] = []
    for c, off_min in zip(comments, STAGGER_OFFSETS_MIN):
        sched = now + datetime.timedelta(minutes=int(off_min))
        out.append({
            "comment":     c[:COMMENT_MAX_CHARS],
            "schedule_at": sched.isoformat() + "Z",
            "offset_min":  int(off_min),
        })
    return out


# ── LinkedIn comment posting ─────────────────────────────────────────────
def _publish_linkedin_comment(post_urn: str, text: str) -> tuple[bool, dict]:
    """POST a comment on a LinkedIn post.
    Uses /rest/socialActions/{post_urn}/comments — the modern surface.
    Author URN = the company org (LINKEDIN_COMPANY_ID) so the comment
    posts as DC Hub, not a personal account.

    Fail-soft: returns (False, {error: ...}) on any error so caller can
    log + fall back to dry_run status. Never raises."""
    try:
        import urllib.parse as _up
        import requests as _rq
        token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
        company = (os.environ.get("LINKEDIN_COMPANY_ID") or "").strip()
        if not token:
            return False, {"error": "no_linkedin_token"}
        if not company:
            return False, {"error": "no_company_id"}
        if not post_urn or not post_urn.startswith("urn:li:"):
            return False, {"error": "bad_post_urn"}
        actor = f"urn:li:organization:{company}"
        enc = _up.quote(post_urn, safe="")
        url = f"https://api.linkedin.com/rest/socialActions/{enc}/comments"
        headers = {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": "202601",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        payload = {"actor": actor, "object": post_urn, "message": {"text": text}}
        r = _rq.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            return True, {"status": r.status_code}
        return False, {"error": f"http_{r.status_code}", "body": r.text[:200]}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def publish_response_thread(log_row: dict) -> dict:
    """Publish a comment thread that was previously generated + logged.
    Posts immediately (no scheduler) — the stagger is handled by the
    fact that this fn is called via cron at 30/90/180min ticks per
    comment (kept simple to avoid an APScheduler dependency).

    In Round 1 ship we DRY-RUN by default; this function is here so the
    operator can manually POST /api/v1/admin/media/autoresponse/publish/<id>
    once they're confident.

    Returns: {posted: int, errors: [...]}.
    """
    out = {"posted": 0, "errors": []}
    comments = log_row.get("comments") or []
    if isinstance(comments, str):
        try:
            comments = json.loads(comments)
        except Exception:
            comments = []
    post_urn = log_row.get("source_post_urn") or ""
    if not post_urn:
        out["errors"].append("no_source_post_urn_on_log_row")
        return out
    for c in comments:
        text = (c.get("comment") if isinstance(c, dict) else "") or ""
        if not text:
            continue
        ok, info = _publish_linkedin_comment(post_urn, text)
        if ok:
            out["posted"] += 1
        else:
            out["errors"].append(info)
    return out


# ── Logging ──────────────────────────────────────────────────────────────
def _log_autoresponse(post: dict, thread: list[dict], status: str,
                      error: str | None = None) -> int | None:
    """INSERT a row into media_autoresponse_log. Stamps autoresponse_triggered_at
    on the source linkedin_posts row in the same transaction. Returns log_id.

    `status` is one of 'pending' | 'dry_run' | 'queued' | 'published' | 'rejected'.
    Idempotent via UNIQUE(source_post_urn): re-detecting the same post is a no-op."""
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            # Stamp the rate-limit marker FIRST so a concurrent run can't
            # double-fire on the same post.
            try:
                cur.execute("""
                    UPDATE linkedin_posts
                       SET autoresponse_triggered_at = NOW()
                     WHERE id = %s
                       AND autoresponse_triggered_at IS NULL
                """, (post.get("post_id"),))
            except Exception:
                pass
            try:
                cur.execute("""
                    INSERT INTO media_autoresponse_log (
                        source_post_id, source_post_urn, source_platform,
                        impressions_before, baseline_impressions, viral_ratio,
                        comments, status, error
                    )
                    VALUES (%s, %s, 'linkedin', %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (source_post_urn) DO UPDATE SET
                        comments = EXCLUDED.comments,
                        status   = EXCLUDED.status,
                        error    = EXCLUDED.error
                    RETURNING id
                """, (
                    post.get("post_id"),
                    post.get("post_urn") or f"li-id-{post.get('post_id')}",
                    int(post.get("impressions") or 0),
                    float(post.get("baseline") or 0),
                    float(post.get("viral_ratio") or 0),
                    json.dumps(thread or []),
                    status,
                    error,
                ))
                row = cur.fetchone()
                return int(row[0]) if row else None
            except Exception as e:
                _log(f"log_insert_failed: {e}")
                return None
    except Exception as e:
        _log(f"log_autoresponse_failed: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


def recent_log_rows(days: int = 30, limit: int = 50) -> list[dict]:
    conn = _db_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, source_post_id, source_post_urn, spike_detected_at,
                       impressions_before, baseline_impressions, viral_ratio,
                       comments, status, error
                  FROM media_autoresponse_log
                 WHERE spike_detected_at > NOW() - make_interval(days => %s)
                 ORDER BY spike_detected_at DESC
                 LIMIT %s
            """, (int(days), int(limit)))
            out = []
            for row in cur.fetchall() or []:
                out.append({
                    "id":                   int(row[0]),
                    "source_post_id":       int(row[1]) if row[1] else None,
                    "source_post_urn":      str(row[2] or ""),
                    "spike_detected_at":    row[3].isoformat() if row[3] else None,
                    "impressions_before":   int(row[4]) if row[4] else 0,
                    "baseline_impressions": float(row[5]) if row[5] else 0,
                    "viral_ratio":          float(row[6]) if row[6] else 0,
                    "comments":             row[7] if isinstance(row[7], list) else (
                                              json.loads(row[7]) if row[7] else []),
                    "status":               str(row[8] or ""),
                    "error":                str(row[9] or "") if row[9] else None,
                })
            return out
    except Exception as e:
        _log(f"recent_log_rows_failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


# ── Top-level scan ───────────────────────────────────────────────────────
def run_spike_responder_scan() -> dict:
    """Main entrypoint. Detect spikes, generate threads, write log rows.
    Honors the kill switch + weekly cap + dry-run flag.

    Returns: {detected, fired, dry_run, errors}.
    Never raises."""
    result = {
        "ok": False,
        "detected":         0,
        "fired":            0,
        "dry_run":          _dry_run(),
        "kill_switched":    _kill_switched(),
        "weekly_cap":       _env_int("MEDIA_AUTORESPONSE_WEEKLY_CAP", WEEKLY_CAP_DEFAULT),
        "weekly_used":      0,
        "spikes":           [],
        "errors":           [],
    }
    if result["kill_switched"]:
        result["ok"] = True
        result["errors"].append("kill_switched")
        return result

    spikes = detect_spikes(limit=5)
    result["detected"] = len(spikes)
    if not spikes:
        result["ok"] = True
        return result

    conn = _db_conn()
    used = 0
    if conn is not None:
        try:
            with conn, conn.cursor() as cur:
                used = _weekly_used(cur)
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    result["weekly_used"] = used
    budget = max(0, int(result["weekly_cap"]) - used)
    if budget <= 0:
        result["ok"] = True
        result["errors"].append(f"weekly_cap_hit:{used}/{result['weekly_cap']}")
        return result

    for spike in spikes:
        if result["fired"] >= budget:
            break
        try:
            thread = generate_response_thread(spike)
            if not thread:
                _log_autoresponse(spike, [], "rejected",
                                  error="gen_failed_or_tone_reject")
                result["errors"].append({
                    "post_id":  spike.get("post_id"),
                    "error":    "gen_failed_or_tone_reject",
                })
                continue
            status = "dry_run" if result["dry_run"] else "queued"
            log_id = _log_autoresponse(spike, thread, status)
            result["fired"] += 1
            result["spikes"].append({
                "post_id":     spike.get("post_id"),
                "post_urn":    spike.get("post_urn"),
                "impressions": spike.get("impressions"),
                "viral_ratio": spike.get("viral_ratio"),
                "log_id":      log_id,
                "status":      status,
                "comments":    thread,
            })
        except Exception as e:
            result["errors"].append({
                "post_id": spike.get("post_id"),
                "error":   f"{type(e).__name__}: {str(e)[:120]}",
            })
    result["ok"] = True
    return result


# ── HTTP endpoints ───────────────────────────────────────────────────────
@media_spike_responder_bp.route(
    "/api/v1/admin/media/spikes/preview", methods=["GET"])
def http_spikes_preview():
    """Read-only: what posts WOULD fire right now? Doesn't write anything,
    doesn't call Claude — just runs the detector + returns a peek."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    spikes = detect_spikes(limit=int(request.args.get("limit", "5")))
    return jsonify({
        "ok":            True,
        "kill_switched": _kill_switched(),
        "dry_run":       _dry_run(),
        "spike_ratio":   SPIKE_RATIO,
        "window_hours":  SPIKE_WINDOW_HOURS,
        "weekly_cap":    _env_int("MEDIA_AUTORESPONSE_WEEKLY_CAP", WEEKLY_CAP_DEFAULT),
        "count":         len(spikes),
        "spikes":        [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in s.items()}
            for s in spikes
        ],
    })


@media_spike_responder_bp.route(
    "/api/v1/admin/media/spikes/detect", methods=["POST"])
def http_spikes_detect():
    """Run a real detect+generate+log cycle. Respects DRY_RUN flag —
    will NOT post comments unless MEDIA_AUTORESPONSE_DRY_RUN!=1."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    result = run_spike_responder_scan()
    return jsonify(result)


@media_spike_responder_bp.route(
    "/api/v1/admin/media/autoresponse/recent", methods=["GET"])
def http_autoresponse_recent():
    """Last 30d log rows. Pretty-printable JSON for /admin/media-mix to
    render the Auto-Response Activity table."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    days = int(request.args.get("days", "30"))
    return jsonify({
        "ok":   True,
        "rows": recent_log_rows(days=days),
    })


@media_spike_responder_bp.route(
    "/api/v1/admin/media/autoresponse/publish/<int:log_id>", methods=["POST"])
def http_autoresponse_publish(log_id: int):
    """Admin override: flip a 'dry_run' or 'queued' row to 'published'
    by actually POSTing the comments to LinkedIn. Use this ONLY when
    you've reviewed the generated comments on the dashboard."""
    if not _admin_or_cron_authorized():
        return jsonify(error="unauthorized"), 401
    rows = recent_log_rows(days=60)
    row = next((r for r in rows if r["id"] == int(log_id)), None)
    if row is None:
        return jsonify(ok=False, error="not_found"), 404
    result = publish_response_thread(row)
    new_status = "published" if result.get("posted") else "publish_failed"
    conn = _db_conn()
    if conn is not None:
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    UPDATE media_autoresponse_log
                       SET status = %s,
                           error  = %s,
                           impressions_after = NULL
                     WHERE id = %s
                """, (new_status,
                      json.dumps(result.get("errors") or []),
                      int(log_id)))
        except Exception as e:
            _log(f"publish_status_update_failed: {e}")
        finally:
            try: conn.close()
            except Exception: pass
    return jsonify(ok=True, log_id=int(log_id), result=result,
                   status=new_status)
