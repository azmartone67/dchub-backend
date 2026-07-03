"""IndexNow (2026-06-07) — instant search-engine indexing for Bing/Yandex/etc.

Instead of waiting for a crawl, we PING IndexNow the moment content changes and
Bing (+ Yandex + Seznam, all IndexNow participants) index within minutes. Ideal
for DC Hub's constant new /facilities/* and /news/* pages.

Ownership is proven by a key file hosted at https://dchub.cloud/<KEY>.txt (static,
in the frontend repo). The ping references that keyLocation.

  GET  /api/v1/admin/indexnow            → config + last-submit status (public read)
  POST /api/v1/admin/indexnow            → {"urls":[...]} explicit submit (admin)
  POST /api/v1/admin/indexnow?recent=1   → submit the most-recent sitemap URLs (admin)

submit_to_indexnow(urls) is exported for in-process hooks (e.g. ping on press
publish). Only https://dchub.cloud/* URLs are accepted (IndexNow rejects off-host).
"""
import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

indexnow_bp = Blueprint("indexnow", __name__)

HOST = "dchub.cloud"
KEY = os.environ.get("DCHUB_INDEXNOW_KEY", "97b69fe31b1f8cd2e6069adf9caf1949")
KEY_LOCATION = f"https://{HOST}/{KEY}.txt"
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY")
              or os.environ.get("ADMIN_API_KEY") or "")
_LAST = {"at": None, "submitted": 0, "status": None}


def _db_conn():
    db = (os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or "")
    if not db:
        return None
    try:
        import psycopg2  # lazy
        return psycopg2.connect(db, connect_timeout=8)
    except Exception:
        return None


def _save_last(d):
    """Persist last-submit status. The in-memory _LAST resets on every redeploy, and
    this backend redeploys constantly — so a HEALTHY IndexNow always read last:null on
    the dashboard, which looked permanently broken. Persisting makes the status honest.
    Fail-soft: any DB issue just leaves the in-memory value."""
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS indexnow_last "
                        "(id INT PRIMARY KEY, at TEXT, submitted INT, status INT)")
            cur.execute("INSERT INTO indexnow_last (id, at, submitted, status) "
                        "VALUES (1, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                        "at = EXCLUDED.at, submitted = EXCLUDED.submitted, status = EXCLUDED.status",
                        (d.get("at"), int(d.get("submitted") or 0), d.get("status")))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_last():
    """Read persisted last-submit status; fall back to in-memory on any issue."""
    conn = _db_conn()
    if not conn:
        return dict(_LAST)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT at, submitted, status FROM indexnow_last WHERE id = 1")
            row = cur.fetchone()
        if row:
            return {"at": row[0], "submitted": row[1], "status": row[2]}
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return dict(_LAST)


def _admin_ok() -> bool:
    return bool(_ADMIN_KEY) and request.headers.get("X-Admin-Key", "") == _ADMIN_KEY


def submit_to_indexnow(urls):
    """Submit dchub.cloud URLs to IndexNow. Dedups, host-filters, caps at 10k.
    Returns a small status dict; never raises."""
    urls = [u for u in dict.fromkeys(urls or [])
            if isinstance(u, str) and u.startswith(f"https://{HOST}")][:10000]
    if not urls:
        return {"ok": False, "reason": "no valid dchub.cloud URLs"}
    payload = json.dumps({
        "host": HOST, "key": KEY, "keyLocation": KEY_LOCATION, "urlList": urls,
    }).encode()
    # 2026-06-14: the shared aggregator https://api.indexnow.org/indexnow 403s our
    # submissions ("key not valid" — its key-file validator gets challenged at our
    # Cloudflare edge) while the per-engine endpoints accept the IDENTICAL key +
    # payload (Bing→200, Yandex→202). Per the IndexNow protocol, submitting to ONE
    # participating engine shares the URLs with all others, so try the authoritative
    # engine endpoints first and only fall back to the aggregator. Return on first 2xx.
    endpoints = ["https://www.bing.com/indexnow",
                 "https://yandex.com/indexnow",
                 "https://api.indexnow.org/indexnow"]
    out = {"ok": False, "submitted": len(urls), "error": "no endpoint reached"}
    for ep in endpoints:
        req = urllib.request.Request(
            ep, data=payload,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "dchub-indexnow/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                code = getattr(r, "status", 200)
            out = {"ok": code in (200, 202), "status": code,
                   "submitted": len(urls), "endpoint": ep}
            if out["ok"]:
                break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            out = {"ok": False, "status": e.code, "submitted": len(urls),
                   "error": body, "endpoint": ep}
        except Exception as e:
            out = {"ok": False, "error": str(e)[:160],
                   "submitted": 0, "endpoint": ep}
    _LAST.update(at=datetime.datetime.utcnow().isoformat() + "Z",
                 submitted=out.get("submitted", 0), status=out.get("status"))
    _save_last(_LAST)
    return out


def _fetch_sitemap_xml(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-indexnow/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def _sitemap_recent(n=500):
    """Most-recent URLs from the live sitemap (sorted by <lastmod> desc).
    r-sitemap-shard (2026-07-03): /sitemap.xml is now a sitemapindex — follow
    its child shard files and collect PAGE locs, else this mode would submit
    the ~8 shard-file URLs to IndexNow and nothing else."""
    try:
        xml = _fetch_sitemap_xml(f"https://{HOST}/sitemap.xml")
    except Exception:
        return []
    pairs = []

    def _collect(x):
        for u, lm in re.findall(r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?", x):
            u = u.strip()
            if u.startswith(f"https://{HOST}"):
                pairs.append((u, (lm or "").strip()))

    if "<sitemapindex" in xml:
        shard_urls = [u.strip() for u in re.findall(r"<loc>([^<]+)</loc>", xml)
                      if u.strip().startswith(f"https://{HOST}")]
        for su in shard_urls[:10]:
            try:
                _collect(_fetch_sitemap_xml(su))
            except Exception:
                continue
    else:
        _collect(xml)
    pairs.sort(key=lambda e: e[1], reverse=True)
    return [u for u, _ in pairs[:max(1, n)]]


def _slugify(text):
    """URL-safe slug — identical to main.py serve_sitemap_xml().slugify so the
    facility URLs we ping MATCH the canonical /facilities/<slug> in sitemap.xml."""
    if not text:
        return ""
    s = str(text).lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s)
    return s.strip("-")


def _recent_facility_urls(n=2000):
    """Canonical /facilities/<slug> URLs for the NEWEST facilities.

    The sitemap stamps a uniform lastmod (every URL = today), so 'recent by
    lastmod' can't surface new content. discovered_facilities.id is a serial PK,
    so ORDER BY id DESC = most-recently-discovered. The slug is built EXACTLY like
    main.py's sitemap: provider-slug + name-slug + stable_hash8(provider|name)
    (r-stable-slug 2026-06-16 — NOT md5(id), which churned every re-ingestion);
    each URL is a strict subset of the canonical sitemap. Read-only, fail-soft → []."""
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL") or "")
    if not db:
        return []
    try:
        import psycopg2  # lazy — avoid hard dep at import time
        conn = psycopg2.connect(db, connect_timeout=10)
    except Exception:
        return []
    urls, seen = [], set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, provider, id FROM discovered_facilities
                 WHERE name IS NOT NULL AND name != ''
                 ORDER BY id DESC
                 LIMIT %s
                """, (max(1, min(int(n), 10000)),))
            for name, provider, fac_id in cur.fetchall():
                name_slug = _slugify(name)
                if not name_slug or len(name_slug) < 3:
                    continue
                provider_slug = _slugify(provider)
                # r-stable-slug (2026-06-16): hash on provider|name (stable), NOT id
                # (churns every re-ingestion) — must match the sitemap/lookup.
                from routes.facility_slug import stable_hash8
                short_hash = stable_hash8(provider, name)
                full = (f"{provider_slug}-{name_slug}-{short_hash}"
                        if provider_slug else f"{name_slug}-{short_hash}")
                if full in seen:
                    continue
                seen.add(full)
                urls.append(f"https://{HOST}/facilities/{full}")
    except Exception:
        urls = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return urls


def ping_new_facilities(limit=5000):
    """Delta churn hook (r-indexnow-delta 2026-07-03): submit the canonical
    /facilities/<slug> URLs for discovered_facilities rows NEWER than the last
    successfully pinged id, then advance the cursor. Before this, daily
    ingestion + the competitor-gap crawler grew the table continuously but
    NOTHING ever told IndexNow — Bing only learned about new facilities by
    re-crawling the (monolithic) sitemap on its own throttled schedule.

    Concurrency: the cursor row is taken FOR UPDATE, so overlapping runs from
    multiple processes/replicas serialize on the row lock and the losers see
    the advanced cursor → empty delta → no duplicate submits. (Row locks are
    pooler-safe, unlike the advisory-lock trap fixed 2026-07-02.) The cursor
    only advances on a 2xx submit, so a failed ping retries next run.

    First run initializes the cursor to MAX(id) and submits nothing — the
    backfill path for the existing corpus is the ?facilities=1 admin mode."""
    conn = _db_conn()
    if not conn:
        return {"ok": False, "reason": "no db"}
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS indexnow_cursor "
                        "(id INT PRIMARY KEY, last_fac_id BIGINT, updated_at TEXT)")
            cur.execute("SELECT last_fac_id FROM indexnow_cursor WHERE id = 1 FOR UPDATE")
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT COALESCE(MAX(id), 0) FROM discovered_facilities")
                max_id = int(cur.fetchone()[0] or 0)
                cur.execute("INSERT INTO indexnow_cursor (id, last_fac_id, updated_at) "
                            "VALUES (1, %s, %s)",
                            (max_id, datetime.datetime.utcnow().isoformat() + "Z"))
                conn.commit()
                return {"ok": True, "initialized": True, "cursor": max_id,
                        "submitted": 0}
            last_id = int(row[0] or 0)
            cur.execute("""
                SELECT id, name, provider FROM discovered_facilities
                 WHERE id > %s AND name IS NOT NULL AND name != ''
                   AND COALESCE(is_duplicate, 0) = 0
                 ORDER BY id ASC
                 LIMIT %s
            """, (last_id, max(1, min(int(limit), 10000))))
            rows = cur.fetchall()
            if not rows:
                conn.commit()
                return {"ok": True, "submitted": 0, "cursor": last_id,
                        "new_facilities": 0}
            max_seen = max(int(r[0]) for r in rows)
            urls, seen = [], set()
            from routes.facility_slug import stable_hash8
            for fac_id, name, provider in rows:
                name_slug = _slugify(name)
                if not name_slug or len(name_slug) < 3:
                    continue
                provider_slug = _slugify(provider)
                h8 = stable_hash8(provider, name)
                full = (f"{provider_slug}-{name_slug}-{h8}"
                        if provider_slug else f"{name_slug}-{h8}")
                if full in seen:
                    continue
                seen.add(full)
                urls.append(f"https://{HOST}/facilities/{full}")
            if not urls:
                # nothing slug-worthy in the delta — still advance past it
                cur.execute("UPDATE indexnow_cursor SET last_fac_id = %s, "
                            "updated_at = %s WHERE id = 1",
                            (max_seen, datetime.datetime.utcnow().isoformat() + "Z"))
                conn.commit()
                return {"ok": True, "submitted": 0, "cursor": max_seen,
                        "new_facilities": len(rows)}
            res = submit_to_indexnow(urls)
            if res.get("ok"):
                cur.execute("UPDATE indexnow_cursor SET last_fac_id = %s, "
                            "updated_at = %s WHERE id = 1",
                            (max_seen, datetime.datetime.utcnow().isoformat() + "Z"))
                conn.commit()
            else:
                conn.rollback()  # keep the old cursor → retry next run
            return {**res, "new_facilities": len(rows),
                    "cursor": max_seen if res.get("ok") else last_id}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:200]}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _recent_dcpi_urls(n=400):
    """Canonical /dcpi/<market_slug> URLs for PUBLISHED power markets — a strict
    subset of the sitemap. Lever #3 (2026-06-26): re-engages crawlers (IndexNow
    reaches Bing → which powers Copilot) on the DCPI market pages (the #1 tool
    real agents use) after the recompute. Read-only, fail-soft → []. NOTE:
    IndexNow is an orthogonal PUSH protocol — it does NOT stamp or forge any
    sitemap lastmod (lastmod stays = real content age). Honest reach caveat:
    Gemini & Perplexity expose NO submit/ping API, so this re-engages Copilot
    directly and only nudges Google/Gemini via the existing GSC sitemap resubmit."""
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL") or "")
    if not db:
        return []
    try:
        import psycopg2  # lazy — avoid hard dep at import time
        conn = psycopg2.connect(db, connect_timeout=10)
    except Exception:
        return []
    urls, seen = [], set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (market_slug) market_slug
                  FROM market_power_scores
                 WHERE published = true AND market_slug IS NOT NULL AND market_slug != ''
                 ORDER BY market_slug, computed_at DESC
                 LIMIT %s
                """, (max(1, min(int(n), 1000)),))
            for (slug,) in cur.fetchall():
                if slug in seen:
                    continue
                seen.add(slug)
                urls.append(f"https://{HOST}/dcpi/{slug}")
    except Exception:
        urls = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return urls


def _wants(name):
    return bool(request.args.get(name)
                or (request.get_json(silent=True) or {}).get(name))


@indexnow_bp.route("/api/v1/admin/indexnow", methods=["GET", "POST"])
def indexnow_endpoint():
    _submit_modes = ("recent", "facilities", "new_facilities", "dcpi", "markets",
                     "delta")
    # Public read: config + last status (no submit, no mode).
    if request.method == "GET" and not any(request.args.get(m) for m in _submit_modes):
        return jsonify(ok=True, host=HOST, key_location=KEY_LOCATION,
                       configured=bool(_ADMIN_KEY), last=_load_last())
    dry = bool(_wants("dry_run"))
    is_admin = _admin_ok()
    # A real submit is admin-gated. A dry_run PREVIEW is public but capped — it
    # only returns URLs that are already in the public sitemap, never pings.
    if not is_admin and not dry:
        return jsonify(ok=False, error="admin key required"), 401
    body = request.get_json(silent=True) or {}
    try:
        n = int(request.args.get("n", body.get("n", 0)) or 0)
    except Exception:
        n = 0
    if dry and not is_admin:
        n = min(n or 25, 50)  # cap public preview (DB-backed) to avoid abuse
    # Cursor-advancing facility delta (admin-only, never dry): submits only
    # facilities newer than the last successful ping. The daily churn loop
    # (register_indexnow) calls ping_new_facilities() directly; this mode is
    # the manual/external-cron trigger for the same thing.
    if _wants("delta"):
        if not is_admin:
            return jsonify(ok=False, error="admin key required"), 401
        return jsonify(ping_new_facilities(n or 5000))
    urls = list(body.get("urls") or [])
    # Newest facilities (canonical /facilities/<slug>, by id desc) — the main
    # new-content stream that has no in-process publish hook.
    if _wants("facilities") or _wants("new_facilities"):
        urls += _recent_facility_urls(n or 2000)
    # DCPI market pages (canonical /dcpi/<slug>, published only) — Lever #3:
    # re-engage crawlers on the #1-tool content after the daily recompute.
    if _wants("dcpi") or _wants("markets"):
        urls += _recent_dcpi_urls(n or 400)
    # Recent sitemap URLs (news/press/static) as a belt-and-suspenders net.
    if _wants("recent"):
        urls += _sitemap_recent(n or 500)
    if dry:
        seen = [u for u in dict.fromkeys(urls)
                if isinstance(u, str) and u.startswith(f"https://{HOST}")]
        return jsonify(ok=True, dry_run=True, count=len(seen), sample=seen[:25])
    return jsonify(submit_to_indexnow(urls))


def _start_delta_loop(app):
    """Daily facility-churn ping (r-indexnow-delta 2026-07-03). Runs on the
    bg role only (DCHUB_ROLE=web skips it, same split as the other
    schedulers); duplicate replicas are harmless anyway — ping_new_facilities
    serializes on the cursor row lock, so losers see an empty delta."""
    if (os.environ.get("DCHUB_ROLE", "all").strip().lower() or "all") == "web":
        return
    import random
    import threading
    import time

    def _loop():
        # let boot settle + stagger replicas so they don't all wake together
        time.sleep(900 + random.uniform(0, 600))
        while True:
            try:
                res = ping_new_facilities()
                app.logger.info(f"indexnow facility-delta: {res}")
            except Exception as e:
                app.logger.warning(f"indexnow facility-delta failed: {e}")
            time.sleep(86400)

    threading.Thread(target=_loop, name="indexnow-facility-delta",
                     daemon=True).start()


def register_indexnow(app):
    try:
        app.register_blueprint(indexnow_bp)
        app.logger.info(f"✓ IndexNow: key {KEY[:8]}… → {KEY_LOCATION}")
        _start_delta_loop(app)
    except Exception as e:
        app.logger.warning(f"indexnow registration: {e}")
