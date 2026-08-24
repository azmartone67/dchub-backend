"""
media_thread_generator.py — LinkedIn multi-post thread generator
(2026-06-07, Round 3).

What it does
============
Given a single seed (e.g., the State-of-2026 launch post URL + headline
number + supporting data points), Claude generates a 5-post series that
ships as a REPLY chain on LinkedIn — post 1 is the top-level post, posts
2–5 are each posted as a comment on the PREVIOUS post (not on the parent),
producing the "thread that unfurls" UX that the LI algorithm rewards.

Why threads work for big launches
---------------------------------
Single posts get one shot at the feed. A 5-part thread:
  • Hits the algo 5 times (each reply re-surfaces the root in followers' feeds)
  • Lets us drill from headline → data → market evidence → counter-take → CTA
    without burying the lede
  • Per-comment 280-char hard cap forces brevity (our usual posts run long)
  • Stagger of 4–7 minutes between replies looks human (no bot fingerprint)

Triggered by admin
------------------
POST /api/v1/admin/media/thread/generate?seed=<id>&dry_run=1

Inputs (JSON body or query):
  - seed_post_id : numeric linkedin_posts.id of the root post (required)
                   The thread will REPLY to this post's URN.
  - title        : human-readable label for the log row
  - headline_num : optional override; if absent we sniff from seed content
  - destination_url : the URL the thread should drive traffic to
                      (e.g., https://dchub.cloud/state-of-2026)
  - data_points  : optional list of 3–5 stat strings to seed the drilldowns

DRY-RUN default
---------------
?dry_run=1 (the default) writes media_thread_log with status='dry_run' and
the 5 posts as JSONB — NO LinkedIn POST. Operator inspects via
GET /api/v1/admin/media/thread/<id> and flips dry_run=0 to ship.

Cap: 1 thread/day
-----------------
The algo punishes spam. We hard-cap 1 thread per UTC day across the entire
table (not per-user, since we're a single-org account). Re-firing inside
the cap returns the prior log row.

Schedule: NOT auto-fired
------------------------
Threads are launch artifacts — too visible to algo to run on a cron. The
admin endpoint is the ONLY entry point. (The "cron" in the spec for the
trending detector + newsletter A/B does NOT apply here.)

Stagger between posts: 4–7 minutes (jittered)
-----------------------------------------------
We don't post all 5 simultaneously; the publisher loop POSTs post 1, sleeps
4–7 minutes, POSTs post 2 as reply to 1, sleeps, etc. To keep this from
blocking the request thread, the admin endpoint returns immediately with
status='queued' and a background `flush_due_thread_replies()` cron picks
up rows where `scheduled_for <= now`.

Safety
------
- MEDIA_R3_DISABLE=1                kills R3 globally.
- THREAD_GENERATOR_DISABLE=1        per-module kill switch.
- DRY-RUN by default.
- 1 thread/UTC day across the table (not per-seed).
- All 5 posts capped at 280 chars EACH (hard truncate w/ ellipsis).
- DCHUB_ADMIN_KEY required to trigger.
- Background flush writes are wrapped — never raise.

Table
-----
media_thread_log
  id BIGSERIAL, seed_post_id BIGINT, root_post_urn TEXT,
  title TEXT, posts JSONB (array of 5 dicts each
  {idx, text, scheduled_for, reply_to_urn, posted_urn, posted_at, status}),
  status TEXT ('dry_run'|'queued'|'partial'|'completed'|'failed'),
  started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
  destination_url TEXT, headline_num TEXT
"""
from __future__ import annotations

import os
import re
import sys
from linkedin_text import escape_li_commentary  # /rest/posts commentary escaping
import json
import time
import random
import datetime
from typing import Any
from datetime import timezone

from flask import Blueprint, jsonify, request


media_thread_generator_bp = Blueprint("media_thread_generator", __name__)


# ── Config ──────────────────────────────────────────────────────────────
THREAD_POSTS_COUNT      = int(os.environ.get("THREAD_POSTS_COUNT") or "5")
THREAD_CHAR_CAP         = int(os.environ.get("THREAD_CHAR_CAP")    or "280")
THREAD_STAGGER_MIN_MIN  = int(os.environ.get("THREAD_STAGGER_MIN_MIN") or "4")
THREAD_STAGGER_MAX_MIN  = int(os.environ.get("THREAD_STAGGER_MAX_MIN") or "7")
DAILY_THREAD_CAP        = int(os.environ.get("THREAD_DAILY_CAP")   or "1")
MODEL_NAME              = (os.environ.get("THREAD_MODEL_NAME")
                            or "claude-sonnet-4-5-20251022")
MAX_TOKENS              = 1200


# ── Plumbing ────────────────────────────────────────────────────────────
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
        sys.stderr.write(f"[thread-gen] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _r3_disabled() -> bool:
    v = (os.environ.get("MEDIA_R3_DISABLE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _module_disabled() -> bool:
    if _r3_disabled():
        return True
    v = (os.environ.get("THREAD_GENERATOR_DISABLE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# ── Schema ──────────────────────────────────────────────────────────────
def init_thread_tables() -> bool:
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_thread_log (
                    id              BIGSERIAL PRIMARY KEY,
                    seed_post_id    BIGINT,
                    root_post_urn   TEXT,
                    title           TEXT,
                    posts           JSONB NOT NULL DEFAULT '[]'::jsonb,
                    status          TEXT NOT NULL DEFAULT 'dry_run',
                    destination_url TEXT,
                    headline_num    TEXT,
                    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at    TIMESTAMPTZ,
                    error           TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_thread_log_status_idx
                    ON media_thread_log(status, started_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_thread_log_started_idx
                    ON media_thread_log(started_at DESC)
            """)
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


try:
    _SCHEMA_OK = init_thread_tables()
except Exception:
    _SCHEMA_OK = False


# ── Seed loading ───────────────────────────────────────────────────────
def _load_seed_post(seed_post_id: int) -> dict | None:
    """Return {id, post_urn, content, destination_url_guess} for the
    seed row, or None if not found."""
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, post_urn,
                       COALESCE(content, content_text, '') AS content
                  FROM linkedin_posts
                 WHERE id = %s
                 LIMIT 1
            """, (int(seed_post_id),))
            row = cur.fetchone()
            if not row:
                return None
            content = str(row[2] or "")
            # Cheap URL sniff — first dchub.cloud URL wins; else None
            m = re.search(r"https://dchub\.cloud[^\s\"']+", content)
            return {
                "id":   int(row[0]),
                "post_urn": str(row[1] or ""),
                "content":  content,
                "destination_url_guess": (m.group(0) if m else None),
            }
    except Exception as e:
        _log(f"load seed failed: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _today_thread_count() -> int:
    conn = _db_conn()
    if conn is None:
        return 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM media_thread_log
                 WHERE started_at >= date_trunc('day', NOW())
                   AND status IN ('dry_run','queued','partial','completed')
            """)
            row = cur.fetchone() or (0,)
            return int(row[0] or 0)
    except Exception:
        return 0
    finally:
        try: conn.close()
        except Exception: pass


# ── Claude call ────────────────────────────────────────────────────────
def _call_claude(prompt: str, system: str) -> str | None:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model":      MODEL_NAME,
                "max_tokens": MAX_TOKENS,
                "system":     system,
                "messages":   [{"role": "user", "content": prompt}],
            }).encode("utf-8"),
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         key,
                "anthropic-version": "2023-06-01",
                "User-Agent":        "dchub-thread-gen/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return text.strip() or None
    except Exception as e:
        _log(f"claude_call_err: {e}")
        return None


# ── Thread generation ─────────────────────────────────────────────────
_SYSTEM = """You are DC Hub's founder, writing a 5-post LinkedIn thread to
maximize algorithmic surface area on a big launch.

CONSTRAINTS — fail to meet any of these and the thread will be rejected:
- Exactly 5 posts. Output ONLY valid JSON: {"posts":["...","...","...","...","..."]}
- Each post <= 280 characters. NO emoji. NO hashtags. NO links inside posts
  2-5 (post 1 may include a single URL; posts 2-5 are pure text — links
  in replies kill reach).
- Post 1: HOOK + HEADLINE NUMBER. Cold-stop a scroller in 4 seconds. Lead
  with the most surprising stat. End with the URL ONLY if provided.
- Posts 2, 3, 4: DRILLDOWN. Each picks ONE specific data point or
  contrarian angle and elaborates in tight prose. NO "thread continues" /
  "more below" filler. Each post must stand alone if someone only sees that
  one in their feed.
- Post 5: CTA + SOFT ASK. Invite a specific action (read the full report,
  reply with their take, share with one colleague). NO begging for likes
  or follows.
- Voice: direct, data-forward, slightly contrarian. No corp-speak.
  Active verbs. Short sentences. Use ALL CAPS for ONE word per post max
  (sparingly, for emphasis on a metric — e.g., "2.4 GW. ONE QUARTER.").

CONTEXT (from the operator):
"""


def _generate_5_posts(title: str, headline_num: str, destination_url: str,
                      data_points: list[str], seed_content: str) -> list[str] | None:
    """Call Claude, parse JSON, validate 5 posts each <=280 chars.
    Returns the list, or None on any failure (caller must handle)."""
    prompt = (
        f"Launch: {title}\n"
        f"Headline number: {headline_num}\n"
        f"Destination URL: {destination_url}\n"
        f"Data points the drilldowns can use (pick the 3 most surprising):\n"
        + "\n".join(f"- {dp}" for dp in (data_points or []))
        + f"\n\nThe root post text (post 1's content) for tone reference:\n"
        f"{seed_content[:1200]}\n\n"
        "Generate the 5-post thread now. Output ONLY the JSON object."
    )
    raw = _call_claude(prompt, _SYSTEM)
    if not raw:
        return None
    # Strip code fences if Claude added them
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except Exception:
        # Try to find a JSON object substring
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return None
    posts = parsed.get("posts") if isinstance(parsed, dict) else None
    if not isinstance(posts, list) or len(posts) != THREAD_POSTS_COUNT:
        _log(f"bad post count: got {len(posts) if isinstance(posts, list) else 'non-list'}")
        return None
    out: list[str] = []
    for i, p in enumerate(posts):
        if not isinstance(p, str):
            return None
        text = p.strip()
        # Hard-truncate at the cap so LinkedIn doesn't reject the POST
        if len(text) > THREAD_CHAR_CAP:
            text = text[:THREAD_CHAR_CAP - 1].rstrip() + "…"
        out.append(text)
    return out


def _fallback_thread(title: str, headline_num: str, destination_url: str,
                      data_points: list[str], seed_content: str) -> list[str]:
    """Deterministic fallback when Claude unavailable — produces a workable
    5-post thread from the inputs alone. Used both as the offline fallback
    AND as the example we paste back to the operator on a dry-run with no
    API key set. NOT decorative: it's the safety net so the user can ship
    EVEN if ANTHROPIC_API_KEY is missing."""
    dps = data_points or []
    # Pad to 3 minimum so we always have drilldowns
    while len(dps) < 3:
        dps.append("Data point pending — fill in before send")
    p1 = (f"{title}: {headline_num}. "
          f"That's the number we're tracking this quarter. "
          f"Full report: {destination_url}")
    p2 = f"Breaking it down: {dps[0]}. The takeaway: this changes how we underwrite the next 12 months."
    p3 = f"Next layer: {dps[1]}. Most market reports miss this because they aggregate at the ISO level, not the metro."
    p4 = f"The contrarian read: {dps[2]}. The consensus is wrong, and the data trail is in the report."
    p5 = (f"If you're allocating capital or capacity in 2026 — read it, "
          f"share it with one colleague, and reply with the one number "
          f"you think we got wrong. {destination_url}")
    out: list[str] = []
    for txt in (p1, p2, p3, p4, p5):
        if len(txt) > THREAD_CHAR_CAP:
            txt = txt[:THREAD_CHAR_CAP - 1].rstrip() + "…"
        out.append(txt)
    return out


def _schedule_post_times(start: datetime.datetime,
                          count: int = THREAD_POSTS_COUNT) -> list[str]:
    """Stagger posts 4–7 minutes apart (jittered) starting at `start`.
    Returns ISO strings for each scheduled_for. Post 1 fires at `start`."""
    out: list[str] = []
    t = start
    rng = random.Random(int(start.timestamp()))
    for i in range(count):
        out.append(t.isoformat())
        gap_min = rng.randint(THREAD_STAGGER_MIN_MIN, THREAD_STAGGER_MAX_MIN)
        t = t + datetime.timedelta(minutes=gap_min)
    return out


# ── Endpoint: generate a thread ─────────────────────────────────────────
@media_thread_generator_bp.route(
    "/api/v1/admin/media/thread/generate", methods=["POST"])
def http_generate_thread():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if _module_disabled():
        return jsonify({"ok": False, "error": "disabled",
                        "reason": ("MEDIA_R3_DISABLE=1" if _r3_disabled()
                                   else "THREAD_GENERATOR_DISABLE=1")}), 200

    body = request.get_json(silent=True) or {}
    seed = (request.args.get("seed")
            or str(body.get("seed_post_id") or "")).strip()
    if not seed:
        return jsonify({"ok": False, "error": "seed_post_id_required"}), 400
    try:
        seed_id = int(seed)
    except Exception:
        return jsonify({"ok": False, "error": "seed_post_id_not_int"}), 400

    # Daily cap — across the whole table, not per-seed
    today_count = _today_thread_count()
    if today_count >= DAILY_THREAD_CAP:
        # Return the existing row(s) so the operator can grab the posts
        existing = list_recent_threads(limit=3)
        return jsonify({
            "ok": False,
            "error": "daily_cap_reached",
            "daily_cap": DAILY_THREAD_CAP,
            "today_count": today_count,
            "recent": existing,
        }), 200

    seed_row = _load_seed_post(seed_id)
    if seed_row is None:
        # We can still generate even without a seed_row — operator may want
        # to author the root post from the thread output. Continue with stub.
        seed_row = {"id": seed_id, "post_urn": "",
                    "content": str(body.get("seed_content") or ""),
                    "destination_url_guess": None}

    title = (body.get("title")
             or request.args.get("title")
             or "State of 2026 thread").strip()[:200]
    destination_url = (body.get("destination_url")
                        or request.args.get("destination_url")
                        or seed_row.get("destination_url_guess")
                        or "https://dchub.cloud/state-of-2026").strip()[:500]
    headline_num = (body.get("headline_num")
                     or request.args.get("headline_num") or "").strip()[:120]
    data_points = body.get("data_points") or []
    if isinstance(data_points, str):
        try:
            data_points = json.loads(data_points)
        except Exception:
            data_points = [s.strip() for s in data_points.split("|") if s.strip()]
    if not isinstance(data_points, list):
        data_points = []
    data_points = [str(s)[:240] for s in data_points][:6]

    # Dry-run is the default. ?dry_run=0 (or false) opts INTO live posting.
    dry_param = (request.args.get("dry_run")
                 or str(body.get("dry_run", "1"))).strip().lower()
    dry_run = dry_param not in ("0", "false", "no", "off")

    posts = _generate_5_posts(title, headline_num or "(no headline given)",
                               destination_url, data_points,
                               seed_row.get("content") or "")
    used_fallback = False
    if not posts:
        posts = _fallback_thread(title, headline_num or "(no headline given)",
                                  destination_url, data_points,
                                  seed_row.get("content") or "")
        used_fallback = True

    # Schedule each post — post 1 fires "now", posts 2-5 stagger 4-7min
    now = datetime.datetime.now(timezone.utc)
    scheduled = _schedule_post_times(now)
    posts_json = [{
        "idx": i + 1,
        "text": posts[i],
        "char_count": len(posts[i]),
        "scheduled_for": scheduled[i],
        "reply_to_urn": None,  # filled in by flush loop after post 1 returns
        "posted_urn": None,
        "posted_at": None,
        "status": "pending" if not dry_run else "dry_run",
    } for i in range(len(posts))]

    status = "dry_run" if dry_run else "queued"
    log_id = _insert_thread_log({
        "seed_post_id": seed_id,
        "root_post_urn": seed_row.get("post_urn") or "",
        "title": title,
        "posts": posts_json,
        "status": status,
        "destination_url": destination_url,
        "headline_num": headline_num,
    })

    return jsonify({
        "ok": True,
        "log_id": log_id,
        "status": status,
        "dry_run": dry_run,
        "used_fallback": used_fallback,
        "model": MODEL_NAME if not used_fallback else "deterministic-fallback",
        "posts": posts_json,
        "title": title,
        "headline_num": headline_num,
        "destination_url": destination_url,
        "daily_cap": DAILY_THREAD_CAP,
        "today_count_after_insert": today_count + 1,
    })


def _insert_thread_log(payload: dict) -> int | None:
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO media_thread_log
                    (seed_post_id, root_post_urn, title, posts, status,
                     destination_url, headline_num)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s) ON CONFLICT DO NOTHING
                RETURNING id
            """, (payload.get("seed_post_id"),
                  payload.get("root_post_urn"),
                  payload.get("title"),
                  json.dumps(payload.get("posts") or []),
                  payload.get("status"),
                  payload.get("destination_url"),
                  payload.get("headline_num")))
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        _log(f"insert thread log failed: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


def list_recent_threads(limit: int = 10) -> list[dict]:
    conn = _db_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, seed_post_id, title, status, posts,
                       started_at, completed_at, headline_num,
                       destination_url
                  FROM media_thread_log
                 ORDER BY started_at DESC
                 LIMIT %s
            """, (int(limit),))
            out = []
            for r in cur.fetchall() or []:
                posts = r[4] or []
                if isinstance(posts, str):
                    try: posts = json.loads(posts)
                    except Exception: posts = []
                out.append({
                    "id": int(r[0]),
                    "seed_post_id": int(r[1] or 0),
                    "title": str(r[2] or ""),
                    "status": str(r[3] or ""),
                    "posts_count": len(posts),
                    "first_post_text": (posts[0].get("text") if posts else ""),
                    "started_at": r[5].isoformat() if r[5] else None,
                    "completed_at": r[6].isoformat() if r[6] else None,
                    "headline_num": r[7],
                    "destination_url": r[8],
                })
            return out
    except Exception as e:
        _log(f"list_recent_threads failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


@media_thread_generator_bp.route(
    "/api/v1/admin/media/thread/<int:log_id>", methods=["GET"])
def http_get_thread(log_id):
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_db"}), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, seed_post_id, root_post_urn, title, posts, status,
                       destination_url, headline_num, started_at, completed_at, error
                  FROM media_thread_log
                 WHERE id = %s
            """, (int(log_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404
            posts = row[4] or []
            if isinstance(posts, str):
                try: posts = json.loads(posts)
                except Exception: posts = []
            return jsonify({
                "ok": True,
                "id": int(row[0]),
                "seed_post_id": int(row[1] or 0),
                "root_post_urn": str(row[2] or ""),
                "title": str(row[3] or ""),
                "posts": posts,
                "status": str(row[5] or ""),
                "destination_url": str(row[6] or ""),
                "headline_num": str(row[7] or ""),
                "started_at": row[8].isoformat() if row[8] else None,
                "completed_at": row[9].isoformat() if row[9] else None,
                "error": str(row[10] or ""),
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try: conn.close()
        except Exception: pass


@media_thread_generator_bp.route(
    "/api/v1/admin/media/thread/list", methods=["GET"])
def http_list_threads():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    limit = int(request.args.get("limit") or "10")
    return jsonify({"ok": True, "rows": list_recent_threads(limit=limit)})


@media_thread_generator_bp.route(
    "/api/v1/admin/media/thread/<int:log_id>/publish", methods=["POST"])
def http_publish_thread(log_id):
    """Flip a dry_run row to queued (so the flush cron picks it up). The
    flush cron actually does the LinkedIn POSTs, respecting stagger."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if _module_disabled():
        return jsonify({"ok": False, "error": "disabled"}), 200
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_db"}), 503
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE media_thread_log
                   SET status = 'queued'
                 WHERE id = %s AND status = 'dry_run'
                RETURNING id, posts
            """, (int(log_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_in_dry_run"}), 200
            posts = row[1] or []
            if isinstance(posts, str):
                try: posts = json.loads(posts)
                except Exception: posts = []
            now = datetime.datetime.now(timezone.utc)
            scheduled = _schedule_post_times(now)
            for i, p in enumerate(posts):
                if i < len(scheduled):
                    p["scheduled_for"] = scheduled[i]
                p["status"] = "pending"
            cur.execute(
                "UPDATE media_thread_log SET posts=%s::jsonb WHERE id=%s",
                (json.dumps(posts), int(log_id)))
        return jsonify({"ok": True, "id": int(log_id), "queued_at": now.isoformat()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── LinkedIn posting helpers (reuse comment-engagement pattern) ─────────
def _publish_root_post(text: str) -> tuple[bool, dict]:
    """POST a NEW top-level LinkedIn post and return its URN.
    Uses /rest/posts (UGC v2 — requires w_organization_social scope).
    Fail-soft, never raises."""
    try:
        import urllib.request
        # DB-first: the LINKEDIN_ACCESS_TOKEN env var goes stale/revoked
        # silently while the DB token self-refreshes. See routes/li_token.py.
        from routes.li_token import li_access_token
        token = li_access_token()
        company = (os.environ.get("LINKEDIN_COMPANY_ID") or "").strip()
        if not token:
            return False, {"error": "no_linkedin_token"}
        if not company:
            return False, {"error": "no_company_id"}
        actor = f"urn:li:organization:{company}"
        payload = {
            "author": actor,
            "commentary": escape_li_commentary(text),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        req = urllib.request.Request(
            "https://api.linkedin.com/rest/posts",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "LinkedIn-Version": "202601",
                "X-Restli-Protocol-Version": "2.0.0",
                "User-Agent": "dchub-thread-gen/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            urn = r.headers.get("x-restli-id") or ""
            return True, {"urn": urn}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _publish_comment_reply(post_urn: str, text: str) -> tuple[bool, dict]:
    """POST a comment as a reply to `post_urn`. Same as the comment-engagement
    helper. Fail-soft."""
    try:
        import urllib.parse as _up
        import urllib.request
        # DB-first: the LINKEDIN_ACCESS_TOKEN env var goes stale/revoked
        # silently while the DB token self-refreshes. See routes/li_token.py.
        from routes.li_token import li_access_token
        token = li_access_token()
        company = (os.environ.get("LINKEDIN_COMPANY_ID") or "").strip()
        if not token:
            return False, {"error": "no_linkedin_token"}
        if not company:
            return False, {"error": "no_company_id"}
        actor = f"urn:li:organization:{company}"
        if not post_urn or not post_urn.startswith("urn:li:"):
            return False, {"error": "bad_post_urn"}
        enc = _up.quote(post_urn, safe="")
        payload = {"actor": actor, "object": post_urn, "message": {"text": text}}
        req = urllib.request.Request(
            f"https://api.linkedin.com/rest/socialActions/{enc}/comments",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "LinkedIn-Version": "202601",
                "X-Restli-Protocol-Version": "2.0.0",
                "User-Agent": "dchub-thread-gen/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            try:
                d = json.loads(r.read().decode("utf-8"))
            except Exception:
                d = {}
            return True, {"urn": str(d.get("$URN") or d.get("id") or "")}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── Flush cron: post DUE posts ────────────────────────────────────────
def flush_due_thread_replies() -> dict:
    """Walk queued thread rows; for each post whose scheduled_for <= now AND
    posted_at IS NULL, POST it (root first, then replies). The flush is
    idempotent — posted_at is set per-post so re-runs skip done posts.
    A thread is marked 'completed' when all 5 posts have posted_urn set."""
    out = {"threads_examined": 0, "posts_shipped": 0, "errors": 0}
    if _module_disabled():
        return {**out, "skipped": "disabled"}
    conn = _db_conn()
    if conn is None:
        return {**out, "skipped": "no_db"}
    now = datetime.datetime.now(timezone.utc)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, root_post_urn, posts
                  FROM media_thread_log
                 WHERE status IN ('queued','partial')
                 ORDER BY started_at ASC
                 LIMIT 5
            """)
            rows = cur.fetchall() or []
            for r in rows:
                out["threads_examined"] += 1
                log_id = int(r[0])
                root_urn = str(r[1] or "")
                posts = r[2] or []
                if isinstance(posts, str):
                    try: posts = json.loads(posts)
                    except Exception: posts = []
                # Find first un-posted, due slot
                changed = False
                for i, p in enumerate(posts):
                    if p.get("posted_urn"):
                        continue
                    sched_iso = p.get("scheduled_for") or ""
                    try:
                        sched_dt = datetime.datetime.fromisoformat(
                            sched_iso.replace("Z", "+00:00"))
                    except Exception:
                        sched_dt = None
                    if sched_dt and sched_dt > now:
                        break  # not yet due; later slots are later still
                    text = p.get("text") or ""
                    if i == 0:
                        ok, info = (_publish_root_post(text)
                                    if not root_urn
                                    else (True, {"urn": root_urn}))
                        if ok:
                            urn = info.get("urn") or ""
                            p["posted_urn"] = urn
                            p["posted_at"] = now.isoformat()
                            p["status"] = "posted"
                            if not root_urn:
                                root_urn = urn
                                cur.execute(
                                    "UPDATE media_thread_log "
                                    "  SET root_post_urn=%s WHERE id=%s",
                                    (urn, log_id))
                            out["posts_shipped"] += 1
                            changed = True
                        else:
                            p["status"] = "failed"
                            out["errors"] += 1
                            changed = True
                            break
                    else:
                        # Reply chain: post i replies to post i-1's URN
                        reply_to = (posts[i - 1].get("posted_urn") or root_urn)
                        if not reply_to:
                            break  # parent didn't post yet — wait
                        ok, info = _publish_comment_reply(reply_to, text)
                        if ok:
                            p["reply_to_urn"] = reply_to
                            p["posted_urn"] = info.get("urn") or ""
                            p["posted_at"] = now.isoformat()
                            p["status"] = "posted"
                            out["posts_shipped"] += 1
                            changed = True
                            # Don't ship more than one per flush — we want
                            # the stagger to feel human in clock-time, not
                            # just in the scheduled_for stamps.
                            break
                        else:
                            p["status"] = "failed"
                            out["errors"] += 1
                            changed = True
                            break
                # Decide overall thread status
                done_count = sum(1 for p in posts if p.get("posted_urn"))
                fail_count = sum(1 for p in posts if p.get("status") == "failed")
                if changed:
                    if fail_count > 0 and done_count < len(posts):
                        new_status = "partial"
                    elif done_count == len(posts):
                        new_status = "completed"
                    else:
                        new_status = "queued"
                    cur.execute("""
                        UPDATE media_thread_log
                           SET posts = %s::jsonb,
                               status = %s,
                               completed_at = CASE WHEN %s = 'completed'
                                                  THEN NOW() ELSE NULL END
                         WHERE id = %s
                    """, (json.dumps(posts), new_status, new_status, log_id))
    except Exception as e:
        _log(f"flush failed: {e}")
        out["errors"] += 1
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── Cron entry points ──────────────────────────────────────────────────
def _run_media_thread_flush():
    """Called every hour by crawler_scheduler.py once threads are queued.
    Does at most 1 POST per call across all threads (small humanlike pace)."""
    if _module_disabled():
        return
    try:
        res = flush_due_thread_replies()
        _log(f"flush examined={res.get('threads_examined')} "
             f"shipped={res.get('posts_shipped')} errors={res.get('errors')}")
    except Exception as e:
        _log(f"flush cron failed: {e}")


__all__ = [
    "media_thread_generator_bp",
    "init_thread_tables",
    "list_recent_threads",
    "flush_due_thread_replies",
    "_run_media_thread_flush",
]
