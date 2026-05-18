"""
Phase ZZZZ-redirects (2026-05-18) — index-page redirects so the 3
remaining 404s become 301s to working content:

  /vs          → /vs/dchawk
  /industry    → /industry/pulse
  /competitive → /vs/dchawk

These are exact-match handlers; sub-paths still go through their
own routes.
"""

from flask import Blueprint, redirect

quick_redirects_bp = Blueprint("quick_redirects", __name__)


@quick_redirects_bp.route("/vs", methods=["GET"], strict_slashes=False)
def vs_index_redirect():
    return redirect("/vs/dchawk", code=301)


@quick_redirects_bp.route("/industry", methods=["GET"], strict_slashes=False)
def industry_index_redirect():
    return redirect("/industry/pulse", code=301)


@quick_redirects_bp.route("/competitive", methods=["GET"], strict_slashes=False)
def competitive_redirect():
    return redirect("/vs/dchawk", code=301)
