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
       Admin/internal only (X-Internal-Key, X-Admin-Key or ?admin_key).
       Body: {"urls": ["...","..."]}.
       Submits the URLs to api.indexnow.org for re-indexing.

The key itself is stored in an env var DCHUB_INDEXNOW_KEY (32-64 hex
chars). Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
"""
import json
import urllib.request
from flask import Blueprint, Response, request, jsonify

from internal_auth import require_internal_or_admin
# ONE IndexNow key for the whole app. routes/indexnow.py owns it: it is the
# module that actually submits, and its KEY_LOCATION is the URL the search
# engines fetch to verify ownership.
#
# Until 2026-08-09 this file carried its OWN hardcoded default, so whenever
# DCHUB_INDEXNOW_KEY was unset (it is unset in production) the two blueprints
# disagreed about the key: routes/indexnow.py submitted under 97b69fe3… while
# this module served /a9d2f7….txt and would have submitted under a9d2f7….
from routes.indexnow import KEY as _CANONICAL_KEY

indexnow_bp = Blueprint("indexnow", __name__)


def _key() -> str:
    return _CANONICAL_KEY


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

    Admin/internal only, via the shared fail-closed gate — X-Internal-Key,
    X-Admin-Key or ?admin_key (internal_auth.require_internal_or_admin), the
    same credential slots the sibling POST /api/v1/admin/indexnow accepts.

    Until 2026-08-09 this docstring said "Admin-only" and NOTHING enforced it.
    An anonymous POST reached request.get_json() directly (verified live: no
    X-Admin-Key returned 400 "missing 'urls' array", a response from inside the
    handler, not a 401). Any caller could burn our 10,000 URL/day per-host
    IndexNow quota and submit junk/404 dchub.cloud URLs under our published
    key, degrading crawl trust — and the "host" override below let them aim the
    submission at an arbitrary host.

    Body: {"urls": ["https://dchub.cloud/...", ...]}
    Optionally: {"host": "dchub.cloud"} to override default.

    Returns the upstream response status. Authentic IndexNow servers
    return 200 on success, 202 on "accepted-for-processing".
    """
    if not require_internal_or_admin(request):
        return jsonify({"ok": False, "error": "admin key required"}), 401
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
    # 2026-06-14: api.indexnow.org (shared aggregator) 403s us ("key not valid" — its
    # validator gets challenged at our CF edge); the per-engine endpoints accept the
    # IDENTICAL key+payload (Bing→200, Yandex→202). Submitting to one IndexNow engine
    # shares the URLs with all participants, so try engine endpoints first, aggregator
    # last, and return on the first 2xx.
    endpoints = ["https://www.bing.com/indexnow",
                 "https://yandex.com/indexnow",
                 "https://api.indexnow.org/IndexNow"]
    last = {"ok": False, "error": "no endpoint reached"}
    for ep in endpoints:
        req = urllib.request.Request(
            ep, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "DCHub-IndexNow/1.0 (+https://dchub.cloud)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                ok = 200 <= r.status < 300
                last = {
                    "ok":              ok,
                    "upstream_status": r.status,
                    "submitted_count": len(urls),
                    "host":            host,
                    "endpoint":        ep,
                    "key_location":    f"https://{host}/{_key()}.txt",
                }
                if ok:
                    return jsonify(last)
        except Exception as e:
            last = {"ok": False, "error": str(e)[:200], "endpoint": ep}
    return jsonify(last), 502
