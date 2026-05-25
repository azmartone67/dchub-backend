"""
linkedin_whoami_proxy.py — expose LinkedIn token health at canonical path.

Phase ZZZZZ-round41 (2026-05-25). marketing_engine.py has a working
linkedin_whoami() function at line 1700 that returns the LinkedIn API's
view of the current token (org URN, validity, etc) — but the route was
registered at a different path that 404s. This module aliases the
canonical /api/v1/linkedin/whoami URL.

If the underlying linkedin_whoami function is gated by admin or fails,
returns a clean error explaining what to check.
"""
import os
import urllib.request
import urllib.error
import json

from flask import Blueprint, jsonify

linkedin_whoami_bp = Blueprint("linkedin_whoami_proxy", __name__,
                                url_prefix="/api/v1/linkedin")


@linkedin_whoami_bp.route("/whoami", methods=["GET"])
def whoami():
    """Probe LinkedIn API with the current LINKEDIN_ACCESS_TOKEN env var."""
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
    if not token:
        return jsonify({
            "ok":      False,
            "error":   "LINKEDIN_ACCESS_TOKEN env var not set on Railway",
            "fix":     "1. Visit https://www.linkedin.com/developers/apps → your app → Auth tab → Generate OAuth token (scopes: w_organization_social r_organization_social w_member_social). 2. railway variables --set LINKEDIN_ACCESS_TOKEN=<paste>",
        }), 500
    # Split on whitespace to defend against contaminated env vars
    token = token.split()[0]
    try:
        req = urllib.request.Request(
            "https://api.linkedin.com/v2/userinfo",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent":    "DCHub-LinkedInWhoami/1.0",
                "Accept":        "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return jsonify({
                "ok":      True,
                "status":  resp.status,
                "profile": body,
                "scopes_hint": "If 'name' / 'email' missing, token lacks userinfo scope. For post-as-organization, ensure w_organization_social.",
            }), 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        return jsonify({
            "ok":           False,
            "status":       e.code,
            "linkedin_response": body,
            "interpretation": _interpret_linkedin_error(e.code, body),
        }), 200
    except Exception as e:
        return jsonify({
            "ok":     False,
            "error":  f"{type(e).__name__}: {str(e)[:200]}",
        }), 200


def _interpret_linkedin_error(code, body):
    if code == 401:
        if "REVOKED" in body.upper():
            return "Token REVOKED. Re-authorize at linkedin.com/developers/apps."
        if "EXPIRED" in body.upper():
            return "Token EXPIRED (60-day lifetime). Re-generate at linkedin.com/developers/apps."
        return "401 Unauthorized — token rejected. Re-auth at linkedin.com/developers/apps."
    if code == 403:
        return "403 Forbidden — token valid but lacks scope (need w_organization_social or w_member_social)."
    if code == 429:
        return "429 Rate limited — LinkedIn enforces per-app caps. Retry in 1h."
    return None
