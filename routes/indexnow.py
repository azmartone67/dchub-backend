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
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "dchub-indexnow/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            code = getattr(r, "status", 200)
        out = {"ok": code in (200, 202), "status": code, "submitted": len(urls)}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        out = {"ok": False, "status": e.code, "submitted": len(urls), "error": body}
    except Exception as e:
        out = {"ok": False, "error": str(e)[:160], "submitted": 0}
    _LAST.update(at=datetime.datetime.utcnow().isoformat() + "Z",
                 submitted=out.get("submitted", 0), status=out.get("status"))
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
    main.py's sitemap (provider-slug + name-slug + md5(id)[:8]) so each URL is a
    strict subset of the canonical sitemap. Read-only, fail-soft → []."""
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
                hash_source = str(fac_id) if fac_id else f"{provider or ''}{name or ''}"
                short_hash = hashlib.md5(hash_source.encode()).hexdigest()[:8]
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


def _wants(name):
    return bool(request.args.get(name)
                or (request.get_json(silent=True) or {}).get(name))


@indexnow_bp.route("/api/v1/admin/indexnow", methods=["GET", "POST"])
def indexnow_endpoint():
    _submit_modes = ("recent", "facilities", "new_facilities")
    # Public read: config + last status (no submit, no mode).
    if request.method == "GET" and not any(request.args.get(m) for m in _submit_modes):
        return jsonify(ok=True, host=HOST, key_location=KEY_LOCATION,
                       configured=bool(_ADMIN_KEY), last=_LAST)
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
