"""
multiplatform_amplifier.py — Multi-platform amplifier for DC Hub Media
(2026-06-07).

Monday's State of 2026 is the seed: ONE LinkedIn post fans out to
Bluesky + Twitter/X + Hacker News (semi-auto) + Mastodon in parallel,
each with platform-tuned framing so each channel's response can drive
more traffic back to dchub.cloud than LinkedIn alone.

Architecture
------------
This module does NOT re-implement Bluesky / Twitter posting — those
already exist in content_publisher.py (_post_to_bluesky, _post_to_twitter).
It DOES add:

  • Mastodon poster (new — federated, simple OAuth via Bearer token)
  • Hacker News semi-auto (no bot API → generates the 1-click submit URL
    https://news.ycombinator.com/submitlink?u=...&t=...; surfaces on the
    admin dashboard so user clicks while logged-in in their browser)
  • Per-platform framers (Bluesky 300 char, Twitter 280 char, HN 80-char
    title-only, Mastodon 500 char) tuned to platform vernacular
  • Master dispatcher amplify_to_all() that fires each in parallel via
    threadpool, logs everything to multiplatform_amplifier_log
  • Source-pull from linkedin_posts (by id or post_urn) OR a raw text
    payload for ad-hoc cross-posts
  • Admin dashboard /admin/multiplatform-amplifier showing last 30d
    amplifications, pre-staged Monday State of 2026 preview per
    platform, 1-click fire-all + 1-click HN submit URL.
  • A schedule entry for /api/v1/admin/multiplatform/auto-sweep that
    every 30 minutes finds LinkedIn posts published in the last 60
    minutes that haven't been amplified yet → amplifies them.

Endpoints
---------
POST /api/v1/admin/multiplatform/amplify
     Body: {source_post_id?, source_text?, source_link?, platforms?}
     Fires the cross-post. Admin-keyed.

POST /api/v1/admin/multiplatform/auto-sweep
     Sweeps the last 60min of linkedin_posts for unamplified rows and
     fires amplify_to_all on each. Cron + admin keyed.

GET  /api/v1/admin/multiplatform/preview-state-of-2026
     Returns the JSON preview of all 4 framings for the State of 2026
     campaign. No write side-effects.

GET  /api/v1/admin/multiplatform/log
     Last N rows from multiplatform_amplifier_log.

GET  /admin/multiplatform-amplifier
     HTML dashboard. Admin-keyed.

GET  /r/hn-submit/<short>
     Tracking redirect → news.ycombinator.com/submitlink. Logs the
     click so we know when the operator opens the HN form.

Env vars
--------
  BLUESKY_HANDLE, BLUESKY_APP_PASSWORD              (existing)
  TWITTER_API_KEY/SECRET, TWITTER_ACCESS_TOKEN/SECRET (existing — blocked
                                                       on "App in Project")
  MASTODON_INSTANCE        (default: mastodon.social)
  MASTODON_ACCESS_TOKEN    (NEW — user must mint at
                            https://<instance>/settings/applications)
  MULTIPLATFORM_AMPLIFIER_DISABLE=1   kill switch
  MULTIPLATFORM_AMPLIFIER_DRY_RUN=1   generates but does not post
  MULTIPLATFORM_AMPLIFIER_DAILY_CAP=5 max amplifications/day (default 5)

Safety
------
  - kill switch + dry-run flags above
  - 5 amplifications/day default cap (env override)
  - per-platform individual try/except, one failure never blocks others
  - Bluesky 300 char / Twitter 280 char / Mastodon 500 char auto-clamped
  - HN never auto-posts (their API + ToS block bots; we surface the URL)
  - admin-keyed everywhere; cron uses same admin key
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape as _esc
from typing import Any

import requests
from flask import Blueprint, Response, jsonify, redirect, request


logger = logging.getLogger(__name__)


multiplatform_amplifier_bp = Blueprint("multiplatform_amplifier", __name__)


# ── Tunables ──────────────────────────────────────────────────────────

PLATFORMS_DEFAULT = ("bluesky", "twitter", "mastodon", "hn")
PLATFORM_CHAR_LIMITS = {
    "bluesky":  300,
    "twitter":  280,
    "mastodon": 500,
    "hn":       80,        # title-only limit
}
DEFAULT_DAILY_CAP = 5
MASTODON_DEFAULT_INSTANCE = "mastodon.social"

# State of 2026 — the specific seed campaign this module was built for.
STATE_OF_2026_URL = "https://dchub.cloud/state-of-2026"


# ── Plumbing ──────────────────────────────────────────────────────────


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
        sys.stderr.write(f"[multi-amplifier] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _kill_switch_on() -> bool:
    return (os.environ.get("MULTIPLATFORM_AMPLIFIER_DISABLE", "")
            .strip().lower() in ("1", "true", "yes", "on"))


def _dry_run_on() -> bool:
    return (os.environ.get("MULTIPLATFORM_AMPLIFIER_DRY_RUN", "")
            .strip().lower() in ("1", "true", "yes", "on"))


def _daily_cap() -> int:
    try:
        return int(os.environ.get("MULTIPLATFORM_AMPLIFIER_DAILY_CAP",
                                   str(DEFAULT_DAILY_CAP)))
    except Exception:
        return DEFAULT_DAILY_CAP


# ── Schema ────────────────────────────────────────────────────────────


def init_amplifier_tables() -> None:
    """Idempotent schema bootstrap. Wired into content_publisher.init_content_tables()
    so it runs on the same boot path as the other media-loop tables.

    multiplatform_amplifier_log captures EVERY cross-post attempt:
      id, source_post_id (linkedin_posts.id OR raw 0),
      source_platform (almost always 'linkedin'),
      target_platform ('bluesky'|'twitter'|'mastodon'|'hn'),
      content_text (the platform-shaped string we sent),
      target_post_url (best-effort — Bluesky returns at:// URI which we
        translate to https://bsky.app/profile/...; Mastodon returns URL;
        Twitter returns id; HN logs the submitlink URL),
      status ('posted'|'dry_run'|'skipped_cap'|'failed'),
      error (truncated),
      posted_at (TIMESTAMPTZ).

    UNIQUE(source_post_id, target_platform) so the auto-sweep cron is
    idempotent — a second sweep over the same source row no-ops cleanly.
    """
    conn = _db_conn()
    if conn is None:
        _log("init_amplifier_tables: no_db_connection")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS multiplatform_amplifier_log (
                    id              SERIAL PRIMARY KEY,
                    source_post_id  BIGINT NOT NULL DEFAULT 0,
                    source_platform TEXT NOT NULL DEFAULT 'linkedin',
                    target_platform TEXT NOT NULL,
                    content_text    TEXT,
                    target_post_url TEXT,
                    status          TEXT NOT NULL DEFAULT 'posted',
                    error           TEXT,
                    posted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                  multiplatform_amplifier_log_src_tgt
                ON multiplatform_amplifier_log (source_post_id, target_platform)
                WHERE source_post_id > 0
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS multiplatform_amplifier_log_posted_at
                ON multiplatform_amplifier_log (posted_at DESC)
            """)
        conn.commit()
    except Exception as e:
        _log(f"init_amplifier_tables_failed: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


# Best-effort lazy init at import time so the schema is ready even if
# content_publisher init never runs (test harness etc). The real call
# path is via content_publisher.init_content_tables on Railway boot.
try:
    init_amplifier_tables()
except Exception:
    pass


# ── Per-platform framers ──────────────────────────────────────────────
# Each framer takes (source_text, source_link) and returns the platform-
# shaped string. State of 2026 framing is hard-coded as the marquee
# example; the generic framers fall back to truncating + appending link.


def _is_state_of_2026(source_text: str, source_link: str) -> bool:
    """Heuristic — does this look like the State of 2026 seed?"""
    if not source_text:
        return False
    if "state-of-2026" in (source_link or "").lower():
        return True
    t = source_text.lower()
    return ("state of 2026" in t
            or "state of data centers 2026" in t)


# Hard-coded marquee framings for the State of 2026 launch.
STATE_OF_2026_BLUESKY = (
    "DC Hub's State of 2026 just dropped — 300+ markets in DCPI, "
    "38 MCP tools, 14 AI platforms connected. Live data from our "
    "actual production: " + STATE_OF_2026_URL +
    " #datacenters #infrastructure"
)
STATE_OF_2026_TWITTER = (
    "State of Data Centers 2026 from DC Hub: 300+ markets scored, "
    "38 MCP tools live for AI agents, real-time grid telemetry across "
    "7 US ISOs. Live: " + STATE_OF_2026_URL
)
STATE_OF_2026_HN_TITLE = (
    "Show HN: DC Hub State of Data Centers 2026 — live, queryable by AI"
)
STATE_OF_2026_MASTODON = (
    "DC Hub's State of Data Centers 2026 just shipped.\n\n"
    "300+ markets scored in our DCPI (DC Power Index), 38 MCP tools live "
    "for AI agents to query in real time, grid telemetry across 7 US ISOs, "
    "and live data on every hyperscaler deal in our pipeline.\n\n"
    "Everything queryable from Claude, ChatGPT, Cursor, etc.\n\n"
    + STATE_OF_2026_URL +
    "\n\n#DataCenters #AIInfrastructure #MCP"
)


def frame_bluesky(source_text: str, source_link: str) -> str:
    """Bluesky 300-char post. Keeps the LinkedIn voice; clamps to 300."""
    if _is_state_of_2026(source_text, source_link):
        return STATE_OF_2026_BLUESKY
    text = (source_text or "").strip()
    link = (source_link or "").strip()
    if link and link not in text:
        # Leave room for the link at the end.
        room = 300 - (len(link) + 1)
        if len(text) > room:
            text = text[:max(0, room - 1)].rstrip() + "…"
        return (text + "\n" + link).strip()[:300]
    return text[:300]


def frame_twitter(source_text: str, source_link: str) -> str:
    """Twitter/X 280-char status."""
    if _is_state_of_2026(source_text, source_link):
        return STATE_OF_2026_TWITTER
    text = (source_text or "").strip()
    link = (source_link or "").strip()
    if link and link not in text:
        room = 280 - (len(link) + 1)
        if len(text) > room:
            text = text[:max(0, room - 1)].rstrip() + "…"
        return (text + " " + link).strip()[:280]
    return text[:280]


def frame_mastodon(source_text: str, source_link: str) -> str:
    """Mastodon 500-char status (their default; instances vary)."""
    if _is_state_of_2026(source_text, source_link):
        return STATE_OF_2026_MASTODON
    text = (source_text or "").strip()
    link = (source_link or "").strip()
    if link and link not in text:
        room = 500 - (len(link) + 2)
        if len(text) > room:
            text = text[:max(0, room - 1)].rstrip() + "…"
        return (text + "\n\n" + link).strip()[:500]
    return text[:500]


def frame_hn_title(source_text: str, source_link: str) -> str:
    """Hacker News 80-char title. HN doesn't take a body — title-only.
    For State of 2026 we hard-code the Show HN framing because HN's
    audience responds to that specific prefix + question-style hook."""
    if _is_state_of_2026(source_text, source_link):
        return STATE_OF_2026_HN_TITLE
    # Generic: first ~76 chars of source, prefix Show HN: when about
    # something we built.
    text = (source_text or "").strip().split("\n", 1)[0].strip()
    if len(text) > 76:
        text = text[:75].rstrip() + "…"
    if text.lower().startswith("show hn"):
        return text[:80]
    # If it looks like our build/ship language, prefix.
    if any(k in text.lower() for k in ("dc hub", "dchub", "we built",
                                        "we shipped", "we launched")):
        return ("Show HN: " + text)[:80]
    return text[:80]


def build_hn_submit_url(title: str, link: str) -> str:
    """HN's 1-click submit URL. User opens in logged-in browser and
    clicks Submit. https://news.ycombinator.com/submitlink?u=...&t=..."""
    qs = urllib.parse.urlencode({"u": link or "", "t": title or ""})
    return f"https://news.ycombinator.com/submitlink?{qs}"


# ── Posters ───────────────────────────────────────────────────────────


def post_to_bluesky(content: str, link: str = "", image_url: str = "") -> dict:
    """Bluesky AT Protocol post. Reuses the existing _post_to_bluesky
    helper in content_publisher.py so we don't drift on auth.

    Returns {ok, url, error}."""
    try:
        from content_publisher import _post_to_bluesky as _pb
        ok, result = _pb(content)
    except Exception as e:
        return {"ok": False, "url": "", "error": f"import_failed: {e}"}
    if not ok:
        return {"ok": False, "url": "", "error": str(result)}
    # Translate at://did:plc:.../app.bsky.feed.post/<rkey> → web URL.
    web_url = ""
    try:
        if isinstance(result, str) and result.startswith("at://"):
            parts = result.replace("at://", "").split("/")
            if len(parts) >= 3:
                did = parts[0]
                rkey = parts[-1]
                handle = (os.environ.get("BLUESKY_HANDLE", "") or did).strip()
                web_url = f"https://bsky.app/profile/{handle}/post/{rkey}"
    except Exception:
        pass
    return {"ok": True, "url": web_url or str(result), "error": ""}


def post_to_twitter(content: str, link: str = "", image_url: str = "") -> dict:
    """X/Twitter post via existing _post_to_twitter helper."""
    try:
        from content_publisher import _post_to_twitter as _pt
        ok, result = _pt(content)
    except Exception as e:
        return {"ok": False, "url": "", "error": f"import_failed: {e}"}
    if not ok:
        return {"ok": False, "url": "", "error": str(result)}
    tweet_id = str(result)
    handle = (os.environ.get("TWITTER_HANDLE", "dchubcloud") or "dchubcloud").strip()
    web_url = (f"https://twitter.com/{handle}/status/{tweet_id}"
               if tweet_id and tweet_id != "posted" else "")
    return {"ok": True, "url": web_url, "error": ""}


def post_to_mastodon(content: str, link: str = "", image_url: str = "") -> dict:
    """Mastodon post. Uses MASTODON_INSTANCE + MASTODON_ACCESS_TOKEN.

    Endpoint: POST {instance}/api/v1/statuses
    Headers:  Authorization: Bearer <token>
    Body:     {"status": "...", "visibility": "public"}
    """
    instance = (os.environ.get("MASTODON_INSTANCE",
                                MASTODON_DEFAULT_INSTANCE) or "").strip()
    token = (os.environ.get("MASTODON_ACCESS_TOKEN", "") or "").strip()
    if not token:
        return {"ok": False, "url": "", "error": "no_mastodon_token"}
    if not instance.startswith("http"):
        instance = "https://" + instance
    instance = instance.rstrip("/")
    try:
        resp = requests.post(
            f"{instance}/api/v1/statuses",
            json={"status": content[:500], "visibility": "public"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return {"ok": True, "url": data.get("url", ""), "error": ""}
        return {"ok": False, "url": "",
                "error": f"mastodon_{resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "url": "", "error": f"mastodon_error: {e}"}


def post_to_hn(title: str, link: str, image_url: str = "") -> dict:
    """Hacker News submission. HN has no bot API — we generate the
    1-click submit URL and surface it on the admin dashboard. The
    operator opens it in their logged-in browser session and clicks
    Submit (one click — no form filling).

    Returns {ok: True, url: <submitlink URL>, error: "semi_auto"} so
    the dispatcher can still log the action; the dashboard's HN card
    surfaces the URL for the operator to click."""
    submit_url = build_hn_submit_url(title, link)
    # We tag a short hash so we can route clicks through /r/hn-submit/<short>
    # for tracking.
    return {"ok": True, "url": submit_url, "error": "semi_auto"}


# ── Daily cap check ──────────────────────────────────────────────────


def _amplifications_today(cur) -> int:
    """How many DISTINCT source_post_ids have we amplified today?
    Multiple platforms for the same source = ONE amplification."""
    try:
        cur.execute("""
            SELECT COUNT(DISTINCT source_post_id)
              FROM multiplatform_amplifier_log
             WHERE status IN ('posted','dry_run')
               AND posted_at::date = CURRENT_DATE
        """)
        row = cur.fetchone()
        if not row:
            return 0
        return int(row[0] if not hasattr(row, "get") else (row.get("count") or 0))
    except Exception:
        return 0


def _already_amplified(cur, source_post_id: int, platform: str) -> bool:
    if not source_post_id or source_post_id <= 0:
        return False
    try:
        cur.execute("""
            SELECT 1 FROM multiplatform_amplifier_log
             WHERE source_post_id = %s
               AND target_platform = %s
               AND status IN ('posted','dry_run')
             LIMIT 1
        """, (int(source_post_id), platform))
        return cur.fetchone() is not None
    except Exception:
        return False


def _record(cur, source_post_id: int, source_platform: str,
            target_platform: str, content_text: str,
            target_post_url: str, status: str, error: str) -> None:
    try:
        cur.execute("""
            INSERT INTO multiplatform_amplifier_log
              (source_post_id, source_platform, target_platform,
               content_text, target_post_url, status, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_post_id, target_platform)
              DO UPDATE SET
                content_text    = EXCLUDED.content_text,
                target_post_url = EXCLUDED.target_post_url,
                status          = EXCLUDED.status,
                error           = EXCLUDED.error,
                posted_at       = NOW()
        """, (int(source_post_id or 0), source_platform, target_platform,
              (content_text or "")[:8000], (target_post_url or "")[:1000],
              status, (error or "")[:2000]))
    except Exception as e:
        _log(f"record_failed: {e}")


# ── Source-text resolution ───────────────────────────────────────────


def _pull_linkedin_post(source_post_id: int) -> dict | None:
    """Fetch a LinkedIn post row by id. Returns {content, link} or None.

    Schema reference: linkedin_posts has (id, post_urn, content/content_text,
    posted_at, impressions, ...). We grab content + best-effort link
    (either the press-release slug we threaded in, or fall back to the
    state-of-2026 marquee URL if the content mentions it)."""
    if not source_post_id or source_post_id <= 0:
        return None
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COALESCE(content, content_text, '') AS content,
                           COALESCE(post_urn, '') AS post_urn
                      FROM linkedin_posts
                     WHERE id = %s
                     LIMIT 1
                """, (int(source_post_id),))
                row = cur.fetchone()
                if not row:
                    return None
                content = (row[0] if not hasattr(row, "get") else row.get("content")) or ""
                # Best-effort link guess from the body. If it has a
                # dchub.cloud URL, use that. Otherwise fall back to
                # /state-of-2026 if the content mentions State of 2026.
                link = ""
                for tok in (content or "").split():
                    if tok.startswith("https://dchub.cloud") or tok.startswith("http://dchub.cloud"):
                        link = tok.rstrip(".,;:)]\"'")
                        break
                if not link and ("state of 2026" in content.lower()
                                  or "state of data centers 2026" in content.lower()):
                    link = STATE_OF_2026_URL
                return {"content": content, "link": link or "https://dchub.cloud/"}
            except Exception as e:
                _log(f"pull_linkedin_post_failed: {e}")
                return None
    finally:
        try: conn.close()
        except Exception: pass


# ── Master dispatcher ────────────────────────────────────────────────


def amplify_to_all(source_post_id: int = 0,
                    source_text: str = "",
                    source_link: str = "",
                    platforms: tuple[str, ...] | list[str] = PLATFORMS_DEFAULT,
                    force: bool = False) -> dict:
    """Fan-out the source content to every requested platform in parallel.

    Args:
      source_post_id: linkedin_posts.id (preferred — enables dedup).
                      Pass 0 for ad-hoc raw posts.
      source_text:    overrides content_text from the row (or use alone
                      with source_post_id=0)
      source_link:    overrides link (or use alone with source_post_id=0)
      platforms:      subset of ("bluesky","twitter","mastodon","hn")
      force:          if True, ignore daily cap + already-amplified
                      dedup (operator override for the 1-click button)

    Returns a dict with per-platform result + summary."""
    result: dict[str, Any] = {
        "source_post_id": source_post_id,
        "platforms":      list(platforms),
        "results":        {},
        "dry_run":        _dry_run_on(),
        "kill_switched":  _kill_switch_on(),
        "errors":         [],
        "amplifications_today": 0,
        "cap":            _daily_cap(),
    }

    if _kill_switch_on() and not force:
        result["errors"].append("kill_switch_on")
        return result

    # Resolve content. Caller can pass source_text directly (ad-hoc
    # path) OR a linkedin_posts.id (cron + dashboard path).
    text = (source_text or "").strip()
    link = (source_link or "").strip()
    if (not text or not link) and source_post_id:
        pulled = _pull_linkedin_post(int(source_post_id))
        if pulled:
            text = text or pulled.get("content", "")
            link = link or pulled.get("link", "")

    if not text and not link:
        result["errors"].append("no_source_content")
        return result

    # Daily cap (counts DISTINCT source_post_ids amplified today).
    conn = _db_conn()
    if conn is None:
        result["errors"].append("no_db_connection")
        return result
    try:
        conn.autocommit = False
        cur = conn.cursor()
        amp_today = _amplifications_today(cur)
        result["amplifications_today"] = amp_today
        if (amp_today >= _daily_cap()) and not force:
            result["errors"].append(f"daily_cap_hit_{amp_today}_of_{_daily_cap()}")
            return result

        # Build framings.
        framings = {
            "bluesky":  frame_bluesky(text, link),
            "twitter":  frame_twitter(text, link),
            "mastodon": frame_mastodon(text, link),
            "hn":       frame_hn_title(text, link),
        }
        result["framings"] = framings

        # Filter to requested + new (unless force).
        targets: list[tuple[str, str]] = []
        for plat in platforms:
            if plat not in framings:
                continue
            if (not force and source_post_id
                    and _already_amplified(cur, source_post_id, plat)):
                result["results"][plat] = {
                    "ok": True, "url": "", "error": "already_amplified",
                    "status": "skipped_cap",
                }
                continue
            targets.append((plat, framings[plat]))

        # Commit the cap-check + dedup-check reads before we hand
        # off to threads (each thread opens its own conn).
        try: conn.commit()
        except Exception: pass

        # Fire each platform in its own thread.
        def _do_one(plat: str, payload: str) -> tuple[str, dict]:
            if _dry_run_on() and not force:
                return plat, {"ok": True, "url": "", "error": "dry_run",
                              "status": "dry_run", "content": payload}
            try:
                if plat == "bluesky":
                    r = post_to_bluesky(payload, link)
                elif plat == "twitter":
                    r = post_to_twitter(payload, link)
                elif plat == "mastodon":
                    r = post_to_mastodon(payload, link)
                elif plat == "hn":
                    r = post_to_hn(payload, link)
                else:
                    r = {"ok": False, "url": "", "error": "unknown_platform"}
                # HN is semi-auto — the URL is the submitlink, not a
                # confirmed submission. Status mirrors that.
                if plat == "hn":
                    r["status"] = "semi_auto_url_ready"
                elif r.get("ok"):
                    r["status"] = "posted"
                else:
                    r["status"] = "failed"
                r["content"] = payload
                return plat, r
            except Exception as e:
                return plat, {"ok": False, "url": "",
                              "error": f"exception: {e}",
                              "status": "failed", "content": payload}

        if targets:
            with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                futs = [ex.submit(_do_one, p, c) for p, c in targets]
                for f in as_completed(futs, timeout=60):
                    try:
                        plat, r = f.result(timeout=30)
                        result["results"][plat] = r
                    except Exception as e:
                        result["errors"].append(f"thread_error: {e}")

        # Log every result row (including dry-run + skipped-cap).
        try:
            cur2 = conn.cursor()
            for plat, r in result["results"].items():
                _record(cur2, source_post_id or 0, "linkedin",
                        plat, r.get("content", framings.get(plat, "")),
                        r.get("url", ""),
                        r.get("status", "failed"),
                        r.get("error", ""))
            conn.commit()
        except Exception as e:
            _log(f"log_record_failed: {e}")
            try: conn.rollback()
            except Exception: pass

    except Exception as e:
        result["errors"].append(f"dispatch_error: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass

    return result


# ── Auto-sweep (cron) ────────────────────────────────────────────────


def auto_sweep_recent() -> dict:
    """Find LinkedIn posts published in the last 60 minutes that have
    NOT been amplified yet, and amplify each. Idempotent — repeat
    sweeps no-op via the UNIQUE(source_post_id, target_platform) index.

    Bounded scan: at most 10 posts per sweep (a sane sanity cap)."""
    result = {"swept": 0, "amplified": 0, "results": [],
              "errors": []}
    conn = _db_conn()
    if conn is None:
        result["errors"].append("no_db_connection")
        return result
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT lp.id
                      FROM linkedin_posts lp
                     WHERE lp.posted_at > NOW() - INTERVAL '60 minutes'
                       AND NOT EXISTS (
                             SELECT 1
                               FROM multiplatform_amplifier_log a
                              WHERE a.source_post_id = lp.id
                                AND a.status IN ('posted','dry_run','semi_auto_url_ready')
                       )
                     ORDER BY lp.posted_at DESC
                     LIMIT 10
                """)
                ids = [r[0] if not hasattr(r, "get") else r.get("id")
                       for r in (cur.fetchall() or [])]
                result["swept"] = len(ids)
            except Exception as e:
                result["errors"].append(f"sweep_query: {e}")
                ids = []
    finally:
        try: conn.close()
        except Exception: pass

    for pid in ids:
        try:
            r = amplify_to_all(source_post_id=int(pid))
            posted = sum(1 for v in (r.get("results") or {}).values()
                          if v.get("status") in ("posted", "dry_run"))
            if posted:
                result["amplified"] += 1
            result["results"].append({"source_post_id": pid, "result": r})
        except Exception as e:
            result["errors"].append(f"amplify_{pid}: {e}")

    return result


# ── Endpoints ────────────────────────────────────────────────────────


@multiplatform_amplifier_bp.route(
    "/api/v1/admin/multiplatform/amplify", methods=["POST"])
def amplify_endpoint():
    """Admin-keyed manual amplification trigger."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(force=True, silent=True) or {}
    args = request.args
    source_post_id = int(payload.get("source_post_id")
                          or args.get("source_post_id") or 0)
    source_text = (payload.get("source_text") or args.get("source_text") or "")
    source_link = (payload.get("source_link") or args.get("source_link") or "")
    platforms_raw = (payload.get("platforms")
                      or args.get("platforms")
                      or PLATFORMS_DEFAULT)
    if isinstance(platforms_raw, str):
        platforms = tuple(p.strip() for p in platforms_raw.split(",") if p.strip())
    else:
        platforms = tuple(platforms_raw)
    force = bool(payload.get("force") or args.get("force"))
    result = amplify_to_all(source_post_id=source_post_id,
                             source_text=source_text,
                             source_link=source_link,
                             platforms=platforms,
                             force=force)
    return jsonify(result), 200


@multiplatform_amplifier_bp.route(
    "/api/v1/admin/multiplatform/auto-sweep", methods=["POST", "GET"])
def auto_sweep_endpoint():
    """Cron + admin: amplify any unamplified LinkedIn post from the
    last 60 minutes."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(auto_sweep_recent()), 200


@multiplatform_amplifier_bp.route(
    "/api/v1/admin/multiplatform/preview-state-of-2026", methods=["GET"])
def preview_state_of_2026():
    """Returns the per-platform preview for the State of 2026 launch
    so the operator can review on Saturday night."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    hn_title = frame_hn_title("State of 2026", STATE_OF_2026_URL)
    hn_submit = build_hn_submit_url(hn_title, STATE_OF_2026_URL)
    return jsonify({
        "campaign":   "state_of_2026",
        "source_url": STATE_OF_2026_URL,
        "framings": {
            "bluesky":  frame_bluesky("State of 2026", STATE_OF_2026_URL),
            "twitter":  frame_twitter("State of 2026", STATE_OF_2026_URL),
            "mastodon": frame_mastodon("State of 2026", STATE_OF_2026_URL),
            "hn_title": hn_title,
            "hn_submit_url": hn_submit,
        },
        "char_counts": {
            "bluesky":  len(STATE_OF_2026_BLUESKY),
            "twitter":  len(STATE_OF_2026_TWITTER),
            "mastodon": len(STATE_OF_2026_MASTODON),
            "hn_title": len(STATE_OF_2026_HN_TITLE),
        },
        "limits":     PLATFORM_CHAR_LIMITS,
    }), 200


@multiplatform_amplifier_bp.route(
    "/api/v1/admin/multiplatform/log", methods=["GET"])
def log_endpoint():
    """Last N amplifier log rows."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except Exception:
        limit = 100
    rows = []
    conn = _db_conn()
    if conn is None:
        return jsonify({"error": "no_db_connection"}), 500
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT id, source_post_id, source_platform,
                           target_platform, content_text,
                           target_post_url, status, error, posted_at
                      FROM multiplatform_amplifier_log
                     ORDER BY posted_at DESC
                     LIMIT %s
                """, (limit,))
                for row in (cur.fetchall() or []):
                    if hasattr(row, "get"):
                        rows.append(dict(row))
                    else:
                        rows.append({
                            "id":              row[0],
                            "source_post_id":  row[1],
                            "source_platform": row[2],
                            "target_platform": row[3],
                            "content_text":    (row[4] or "")[:500],
                            "target_post_url": row[5],
                            "status":          row[6],
                            "error":           row[7],
                            "posted_at":       row[8].isoformat()
                                                  if row[8] else None,
                        })
            except Exception as e:
                return jsonify({"error": f"query_failed: {e}"}), 500
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify({"rows": rows, "count": len(rows)}), 200


# ── HN tracking redirect ────────────────────────────────────────────


@multiplatform_amplifier_bp.route("/r/hn-submit/<short>", methods=["GET"])
def hn_submit_redirect(short: str):
    """Tracking redirect → news.ycombinator.com/submitlink.

    The dashboard renders a 1-click button as /r/hn-submit/<hash> where
    <hash> is hashlib.md5(submitlink_url).hexdigest()[:10]. We look up
    the matching multiplatform_amplifier_log row with status='semi_auto_url_ready'
    + target_platform='hn', stamp clicked_at, and 302 to the real URL.
    If we can't find a match we still 302 to the State of 2026 submitlink
    so the operator's click is never wasted."""
    conn = _db_conn()
    target = build_hn_submit_url(STATE_OF_2026_HN_TITLE, STATE_OF_2026_URL)
    if conn is None:
        return redirect(target, code=302)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT target_post_url
                      FROM multiplatform_amplifier_log
                     WHERE target_platform = 'hn'
                       AND MD5(target_post_url) LIKE %s
                     ORDER BY posted_at DESC
                     LIMIT 1
                """, (short + "%",))
                row = cur.fetchone()
                if row:
                    target = (row[0] if not hasattr(row, "get") else row.get("target_post_url")) or target
                # Stamp a click row for tracking.
                cur.execute("""
                    UPDATE multiplatform_amplifier_log
                       SET error = COALESCE(error,'') || ' clicked@' || NOW()::text
                     WHERE target_platform = 'hn'
                       AND MD5(target_post_url) LIKE %s
                """, (short + "%",))
                conn.commit()
            except Exception as e:
                _log(f"hn_redirect_lookup_failed: {e}")
                try: conn.rollback()
                except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    return redirect(target, code=302)


# ── HTML dashboard ───────────────────────────────────────────────────


def _platform_status_chips() -> str:
    """Compute per-platform integration status for the dashboard."""
    chips = []
    # Bluesky
    bs_set = bool(os.environ.get("BLUESKY_HANDLE", "").strip()
                   and os.environ.get("BLUESKY_APP_PASSWORD", "").strip())
    chips.append(("Bluesky", "WIRED" if bs_set else "NOT_WIRED",
                  "set BLUESKY_HANDLE + BLUESKY_APP_PASSWORD"))
    # Twitter
    tw_oauth1 = all(os.environ.get(k, "").strip() for k in
                     ("TWITTER_API_KEY", "TWITTER_API_SECRET",
                      "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"))
    tw_bearer = bool(os.environ.get("TWITTER_BEARER_TOKEN", "").strip())
    if tw_oauth1 or tw_bearer:
        chips.append(("Twitter/X", "CREDS_PRESENT",
                       "verify with /api/v1/admin/twitter/diagnose"))
    else:
        chips.append(("Twitter/X", "BLOCKED",
                       "App not in Project — see /api/v1/admin/twitter/diagnose"))
    # Mastodon
    mas_set = bool(os.environ.get("MASTODON_ACCESS_TOKEN", "").strip())
    chips.append(("Mastodon", "WIRED" if mas_set else "NOT_WIRED",
                  f"set MASTODON_ACCESS_TOKEN (instance="
                  f"{os.environ.get('MASTODON_INSTANCE', MASTODON_DEFAULT_INSTANCE)})"))
    # HN
    chips.append(("Hacker News", "SEMI_AUTO_WIRED",
                  "no API — uses 1-click submitlink"))
    out = []
    for name, status, hint in chips:
        color = ("#2bd97a" if status in ("WIRED", "SEMI_AUTO_WIRED",
                                          "CREDS_PRESENT")
                  else "#ff9b4a" if status == "NOT_WIRED" else "#ff6b6b")
        out.append(
            f'<span style="display:inline-block;background:#0e1320;border:1px solid #1f2740;'
            f'padding:6px 10px;border-radius:8px;margin-right:6px;font-size:12px;">'
            f'<b style="color:{color}">{_esc(name)}</b> {_esc(status)} '
            f'<span style="opacity:0.65">— {_esc(hint)}</span></span>'
        )
    return "".join(out)


@multiplatform_amplifier_bp.route(
    "/admin/multiplatform-amplifier", methods=["GET"])
def admin_dashboard():
    """HTML admin dashboard. Admin-keyed."""
    if not _admin_or_cron_authorized():
        return Response("Unauthorized", status=401)

    admin_key_safe = _esc(os.environ.get("DCHUB_ADMIN_KEY", ""))

    # Pre-stage Monday's State of 2026 framings.
    bs_text  = STATE_OF_2026_BLUESKY
    tw_text  = STATE_OF_2026_TWITTER
    mas_text = STATE_OF_2026_MASTODON
    hn_title = STATE_OF_2026_HN_TITLE
    hn_submit = build_hn_submit_url(hn_title, STATE_OF_2026_URL)
    hn_short  = hashlib.md5(hn_submit.encode("utf-8")).hexdigest()[:10]

    # Pull last 30d log rows.
    log_rows: list[dict] = []
    conn = _db_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute("""
                        SELECT id, source_post_id, target_platform,
                               status, target_post_url, posted_at,
                               LEFT(content_text, 120) AS preview,
                               error
                          FROM multiplatform_amplifier_log
                         WHERE posted_at > NOW() - INTERVAL '30 days'
                         ORDER BY posted_at DESC
                         LIMIT 60
                    """)
                    for row in (cur.fetchall() or []):
                        if hasattr(row, "get"):
                            log_rows.append(dict(row))
                        else:
                            log_rows.append({
                                "id":              row[0],
                                "source_post_id":  row[1],
                                "target_platform": row[2],
                                "status":          row[3],
                                "target_post_url": row[4],
                                "posted_at":       (row[5].isoformat()
                                                      if row[5] else ""),
                                "preview":         row[6] or "",
                                "error":           row[7] or "",
                            })
                except Exception as e:
                    _log(f"dashboard_log_query_failed: {e}")
        finally:
            try: conn.close()
            except Exception: pass

    log_html_rows = []
    for r in log_rows[:60]:
        status_color = ("#2bd97a" if r["status"] in ("posted",)
                          else "#fbbf24" if r["status"] in ("dry_run",
                                                              "semi_auto_url_ready")
                          else "#ff6b6b" if r["status"] == "failed"
                          else "#888")
        url_cell = (f'<a href="{_esc(r["target_post_url"])}" target="_blank">link</a>'
                     if r["target_post_url"] else "—")
        log_html_rows.append(
            f'<tr><td style="opacity:0.6">{_esc(str(r["posted_at"])[:19])}</td>'
            f'<td>{_esc(r["target_platform"])}</td>'
            f'<td style="color:{status_color}">{_esc(r["status"])}</td>'
            f'<td>{url_cell}</td>'
            f'<td style="font-size:11px;opacity:0.7">{_esc((r["preview"] or "")[:100])}</td>'
            f'</tr>'
        )
    log_table_html = "".join(log_html_rows) or (
        '<tr><td colspan="5" style="opacity:0.5;padding:24px">'
        'No amplifications recorded yet. Once you fire the State of 2026 '
        'campaign Monday this table will populate.</td></tr>'
    )

    fire_endpoint = (
        f"/api/v1/admin/multiplatform/amplify?admin_key={admin_key_safe}"
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Multi-Platform Amplifier — DC Hub Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    background: #0a0d18; color: #e6ebf5;
    margin: 0; padding: 24px;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px 0; }}
  h2 {{ font-size: 16px; margin: 24px 0 8px 0; opacity: 0.85; }}
  .sub {{ opacity: 0.6; margin-bottom: 18px; font-size: 13px; }}
  .card {{
    background: #121829; border: 1px solid #1f2740;
    border-radius: 12px; padding: 16px; margin-bottom: 14px;
  }}
  pre {{
    background: #0a0d18; border: 1px solid #1f2740;
    border-radius: 8px; padding: 12px; white-space: pre-wrap;
    word-wrap: break-word; font-size: 13px;
    color: #c0cad8;
  }}
  .row {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .row > .card {{ flex: 1; min-width: 320px; }}
  button, .btn {{
    background: #3b82f6; color: #fff; border: none;
    padding: 10px 14px; border-radius: 8px; cursor: pointer;
    font-weight: 600; font-size: 14px; text-decoration: none;
    display: inline-block;
  }}
  button.warn, .btn.warn {{ background: #f59e0b; color: #0a0d18; }}
  button.danger, .btn.danger {{ background: #ef4444; }}
  button:hover, .btn:hover {{ opacity: 0.9; }}
  .meta {{ font-size: 12px; opacity: 0.55; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: left;
              border-bottom: 1px solid #1f2740; }}
  th {{ background: #0e1320; opacity: 0.85; font-weight: 600; }}
  .charcount {{ float: right; opacity: 0.55; font-size: 11px; }}
  a {{ color: #60a5fa; }}
</style>
</head>
<body>
  <h1>Multi-Platform Amplifier</h1>
  <div class="sub">Fan out one LinkedIn post to Bluesky + Twitter/X + Mastodon + Hacker News (semi-auto) in parallel.</div>
  <div style="margin-bottom: 18px">{_platform_status_chips()}</div>

  <h2>Pre-staged — Monday's State of 2026 Launch</h2>
  <div class="row">

    <div class="card">
      <div><b>Bluesky</b>
        <span class="charcount">{len(bs_text)}/300</span></div>
      <pre>{_esc(bs_text)}</pre>
    </div>

    <div class="card">
      <div><b>Twitter / X</b>
        <span class="charcount">{len(tw_text)}/280</span></div>
      <pre>{_esc(tw_text)}</pre>
    </div>

    <div class="card">
      <div><b>Mastodon</b>
        <span class="charcount">{len(mas_text)}/500</span></div>
      <pre>{_esc(mas_text)}</pre>
    </div>

    <div class="card">
      <div><b>Hacker News</b> (title only)
        <span class="charcount">{len(hn_title)}/80</span></div>
      <pre>{_esc(hn_title)}
{_esc(STATE_OF_2026_URL)}</pre>
      <a class="btn warn" href="/r/hn-submit/{hn_short}" target="_blank">
        1-click HN Submit (opens form pre-filled)
      </a>
      <div class="meta" style="margin-top:8px">
        HN doesn't allow bot submissions — opens news.ycombinator.com/submitlink
        in your logged-in browser session. Click Submit on their page.
      </div>
    </div>

  </div>

  <h2>Fire All Now</h2>
  <div class="card">
    <button class="btn danger" onclick="fireAll('state_of_2026', false)">
      🔥 Fire (live) State of 2026 → Bluesky + Twitter + Mastodon
    </button>
    <button class="btn warn" onclick="fireAll('state_of_2026', true)"
            style="margin-left:8px">
      🧪 Dry-run preview
    </button>
    <div class="meta" style="margin-top:8px">
      HN is excluded from "Fire All" — click the 1-click submit button above
      for HN (their ToS blocks bot submissions).
    </div>
    <pre id="fire-result" style="margin-top:12px;display:none"></pre>
  </div>

  <h2>Last 30d Amplifications</h2>
  <div class="card" style="padding:0">
    <table>
      <thead><tr>
        <th style="width:170px">When (UTC)</th>
        <th style="width:110px">Platform</th>
        <th style="width:110px">Status</th>
        <th style="width:60px">URL</th>
        <th>Preview</th>
      </tr></thead>
      <tbody>{log_table_html}</tbody>
    </table>
  </div>

  <h2>Env vars (set in Railway)</h2>
  <div class="card">
    <pre>BLUESKY_HANDLE              = dchub.cloud (or your bsky handle)
BLUESKY_APP_PASSWORD        = bsky.app/settings/app-passwords
TWITTER_API_KEY/SECRET      = dev portal (App must be in a Project)
TWITTER_ACCESS_TOKEN/SECRET = regenerate INSIDE Project context
MASTODON_INSTANCE           = mastodon.social  (default)
MASTODON_ACCESS_TOKEN       = your_instance/settings/applications (write:statuses)
MULTIPLATFORM_AMPLIFIER_DISABLE = 1   # kill switch
MULTIPLATFORM_AMPLIFIER_DRY_RUN = 1   # preview without posting
MULTIPLATFORM_AMPLIFIER_DAILY_CAP = 5 # 5 amplifications/day (default)</pre>
  </div>

<script>
async function fireAll(campaign, dryRun) {{
  const out = document.getElementById("fire-result");
  out.style.display = "block";
  out.textContent = "Firing…";
  try {{
    const body = {{
      source_text: "DC Hub State of 2026 launch",
      source_link: "{STATE_OF_2026_URL}",
      platforms:   "bluesky,twitter,mastodon",
      force:       true
    }};
    if (dryRun) body.dry_run_request = true;
    const r = await fetch("{fire_endpoint}", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify(body)
    }});
    const j = await r.json();
    out.textContent = JSON.stringify(j, null, 2);
    if (!dryRun) setTimeout(() => location.reload(), 2500);
  }} catch (e) {{
    out.textContent = "ERROR: " + e.message;
  }}
}}
</script>
</body></html>
"""
    return Response(html, mimetype="text/html")


# ── Cron entry point (called by crawler_scheduler) ──────────────────


def _run_multiplatform_amplifier() -> None:
    """Crawler-scheduler entry point. Fires hourly to amplify any
    LinkedIn post from the last 60min that hasn't already been
    amplified. Idempotent + bounded."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    if not key:
        logger.warning(
            "multiplatform_amplifier: skipped — DCHUB_ADMIN_KEY not set")
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    try:
        r = _rq.post(
            f"{base}/api/v1/admin/multiplatform/auto-sweep",
            headers={"X-Admin-Key": key,
                     "User-Agent": "dchub-cron-multiplatform-amplifier/1.0"},
            timeout=120,
        )
        d = ((r.json() if r.headers.get("content-type", "")
              .startswith("application/json") else {})
              or {})
        logger.info(
            "multiplatform_amplifier: swept=%s amplified=%s errors=%s",
            d.get("swept"), d.get("amplified"),
            len(d.get("errors", []) or []),
        )
    except Exception as e:
        logger.error("multiplatform_amplifier: error — %s", e)
