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


@indexnow_bp.route("/api/v1/admin/indexnow", methods=["GET", "POST"])
def indexnow_endpoint():
    # Public read: config + last status (no submit).
    if request.method == "GET" and not request.args.get("recent"):
        return jsonify(ok=True, host=HOST, key_location=KEY_LOCATION,
                       configured=bool(_ADMIN_KEY), last=_LAST)
    # Any submit (POST, or GET/POST ?recent) is admin-gated.
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    body = request.get_json(silent=True) or {}
    if request.args.get("recent") or body.get("recent"):
        try:
            n = int(request.args.get("n", body.get("n", 500)))
        except Exception:
            n = 500
        urls = _sitemap_recent(n)
    else:
        urls = body.get("urls") or []
    return jsonify(submit_to_indexnow(urls))


def register_indexnow(app):
    try:
        app.register_blueprint(indexnow_bp)
        app.logger.info(f"✓ IndexNow: key {KEY[:8]}… → {KEY_LOCATION}")
    except Exception as e:
        app.logger.warning(f"indexnow registration: {e}")
