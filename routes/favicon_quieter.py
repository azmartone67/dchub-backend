"""
favicon_quieter.py — silence /favicon.ico 522 noise.

Phase ZZZZZ-round38.3 (2026-05-25). Every browser auto-requests
/favicon.ico on page load. api.dchub.cloud has no favicon → worker
proxies to Flask → 404 OR falls through to 522. Noisy in DevTools.

Fix: return 204 No Content with long-cache headers so browsers stop
asking, OR redirect to the canonical favicon on dchub.cloud.
"""
from flask import Blueprint, redirect

favicon_bp = Blueprint("favicon_quieter", __name__)


# AUTO-REPAIR: duplicate route '/favicon.ico' also in main.py:15390 — review and remove one
@favicon_bp.route("/favicon.ico")
def favicon():
    # 301 redirect to dchub.cloud's canonical favicon — browsers cache the
    # redirect itself, single round-trip becomes ~0 ongoing requests.
    return redirect("https://dchub.cloud/favicon.ico", code=301)


@favicon_bp.route("/apple-touch-icon.png")
@favicon_bp.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return redirect("https://dchub.cloud/apple-touch-icon.png", code=301)
