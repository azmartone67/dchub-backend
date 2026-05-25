"""
blog_redirect.py — /blog → 301 to /news.

Phase ZZZZZ-round47.5 (2026-05-25). /blog was a 404 advertised by the
Pages worker OG metadata. Rather than maintain a separate blog surface,
consolidate to the existing /news archive (which already aggregates
press releases + industry news). 301 preserves SEO authority.
"""
from flask import Blueprint, redirect

blog_redirect_bp = Blueprint("blog_redirect", __name__)


@blog_redirect_bp.route("/blog", methods=["GET"], strict_slashes=False)
@blog_redirect_bp.route("/blog/<path:rest>", methods=["GET"])
def blog(rest=None):
    target = "/news" + (f"/{rest}" if rest else "")
    return redirect(target, code=301)
