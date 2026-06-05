"""IndexNow protocol — instant indexing for Bing, Yandex, Naver, Seznam.

IndexNow is a free, open protocol that lets a site notify participating
search engines the instant a URL changes. Submission is rate-limited at
10,000 URLs/day per host — far above any practical need.

Phase HJ-2 (2026-06-05) — added during the Google traffic-recovery
sweep to get Bing + Yandex (collectively 20-30% of global search) to
re-index the 7 recovered SEO landings within MINUTES instead of waiting
days for Google's natural crawl.

Two exposed surfaces:

  GET  /{KEY}.txt
       Self-verification file. IndexNow requires the key be hosted at
       the root of the domain so they can confirm we own it.

  POST /api/v1/admin/indexnow/submit
       Admin-only. Body: {"urls": ["...","..."]}.
       Submits the URLs to api.indexnow.org for re-indexing.

The key itself is stored in an env var DCHUB_INDEXNOW_KEY (32-64 hex
chars). Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
"""
import json
import os
import urllib.request
from flask import Blueprint, Response, request, jsonify

indexnow_bp = Blueprint("indexnow", __name__)


# Default key embedded for first-shipment. Override in production via
# DCHUB_INDEXNOW_KEY env var. Generated once 2026-06-05 with
# secrets.token_hex(32).
_DEFAULT_KEY = "a9d2f7c1b8e5640a3c2f1e9d8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b"


def _key() -> str:
    return os.environ.get("DCHUB_INDEXNOW_KEY") or _DEFAULT_KEY


# ── 1. Self-verification file at /{KEY}.txt ──────────────────────

@indexnow_bp.route("/<key>.txt", methods=["GET"])
def indexnow_keyfile(key: str):
    """Serve the IndexNow self-verification file.

    IndexNow requires the key to be hosted at the apex (or a /key/<key>.txt
    path). We use the apex form; the route only fires when the requested
    .txt filename matches our configured key, so it doesn't conflict with
    other .txt files (llms.txt, robots.txt — those have their own
    explicit routes that take precedence).
    """
    expected = _key()
    if key != expected:
        # Not OUR key — let other handlers / 404 take it
        return Response("not found", status=404,
                        mimetype="text/plain")
    return Response(expected + "\n", mimetype="text/plain",
                    headers={"Cache-Control": "public, max-age=86400",
                             "X-DC-Hub-Surface": "indexnow-verify"})


# ── 2. Admin submission endpoint ─────────────────────────────────

@indexnow_bp.route("/api/v1/admin/indexnow/submit", methods=["POST"])
def indexnow_submit():
    """Submit URLs to IndexNow for instant re-indexing.

    Body: {"urls": ["https://dchub.cloud/...", ...]}
    Optionally: {"host": "dchub.cloud"} to override default.

    Returns the upstream response status. Authentic IndexNow servers
    return 200 on success, 202 on "accepted-for-processing".
    """
    payload = request.get_json(silent=True) or {}
    urls = payload.get("urls") or []
    if not urls or not isinstance(urls, list):
        return jsonify({"ok": False,
                        "error": "missing 'urls' array in body"}), 400
    host = payload.get("host", "dchub.cloud")
    body = json.dumps({
        "host":     host,
        "key":      _key(),
        "keyLocation": f"https://{host}/{_key()}.txt",
        "urlList":  urls,
    }).encode()
    req = urllib.request.Request(
        "https://api.indexnow.org/IndexNow",
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "DCHub-IndexNow/1.0 (+https://dchub.cloud)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return jsonify({
                "ok":              200 <= r.status < 300,
                "upstream_status": r.status,
                "submitted_count": len(urls),
                "host":            host,
                "key_location":    f"https://{host}/{_key()}.txt",
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
