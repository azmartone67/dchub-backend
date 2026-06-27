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


def _sitemap_recent(n=500):
    """Most-recent URLs from the live sitemap (sorted by <lastmod> desc)."""
    try:
        req = urllib.request.Request(f"https://{HOST}/sitemap.xml",
                                     headers={"User-Agent": "dchub-indexnow/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    pairs = re.findall(r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?", xml)
    pairs = [(u.strip(), (lm or "").strip()) for u, lm in pairs
             if u.strip().startswith(f"https://{HOST}")]
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
    _submit_modes = ("recent", "facilities", "new_facilities", "dcpi", "markets")
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


def register_indexnow(app):
    try:
        app.register_blueprint(indexnow_bp)
        app.logger.info(f"✓ IndexNow: key {KEY[:8]}… → {KEY_LOCATION}")
    except Exception as e:
        app.logger.warning(f"indexnow registration: {e}")
