"""
twitter_diagnostic.py — Phase r62 (2026-05-25).

Verifies X/Twitter credentials WITHOUT going through the daily
publisher loop. User has rotated API keys 3x; each rotation, the
6h publisher cycle generated 403 log noise but no clean signal on
"what's actually wrong."

Endpoints:

  GET /api/v1/admin/twitter/diagnose
       Reads-only check. Pings GET /2/users/me with the configured
       OAuth1 quad. Returns one of:
         200 + {handle, user_id}                  — creds work
         401 + {error: "auth_failed", ...}         — wrong keys
         403 + {error: "app_not_in_project",...}  — Project gotcha
         disabled                                   — env flag off

  POST /api/v1/admin/twitter/test-tweet
       Fires a single test tweet ("DC Hub X publisher verification
       — <utc-iso>") via the existing _post_to_twitter code path.
       Admin keyed. Use AFTER /diagnose returns ok=true.

Both endpoints surface the EXACT error text from X's API so you
can paste it into the dev-portal support form without going
hunting through Railway logs.
"""
from __future__ import annotations

import datetime
import json
import os

from flask import Blueprint, jsonify, request


twitter_diagnostic_bp = Blueprint("twitter_diagnostic", __name__)


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    return bool(expected) and provided == expected


def _env_state() -> dict:
    """Snapshot of which env vars are set (NOT the values)."""
    return {
        "TWITTER_PUBLISHER_ENABLED": (
            os.environ.get("TWITTER_PUBLISHER_ENABLED", "").strip().lower()
        ),
        "TWITTER_API_KEY_set":     bool(os.environ.get("TWITTER_API_KEY", "").strip()),
        "TWITTER_API_SECRET_set":  bool(os.environ.get("TWITTER_API_SECRET", "").strip()),
        "TWITTER_ACCESS_TOKEN_set": bool(os.environ.get("TWITTER_ACCESS_TOKEN", "").strip()),
        "TWITTER_ACCESS_SECRET_set": bool(os.environ.get("TWITTER_ACCESS_SECRET", "").strip()),
        "TWITTER_BEARER_TOKEN_set": bool(os.environ.get("TWITTER_BEARER_TOKEN", "").strip()),
    }


def _interpret_x_error(status: int, body_text: str) -> dict:
    """Map X API error text to actionable diagnosis."""
    body = (body_text or "").lower()
    if "must be attached to a project" in body or "must be associated" in body:
        return {
            "diagnosis":     "app_not_in_project",
            "fix":           ("Your X dev-portal App is a legacy standalone. "
                                "Go to https://developer.x.com/en/portal/projects-and-apps, "
                                "create a Project, attach the existing App to it, "
                                "then REGENERATE Access Token + Access Token Secret "
                                "inside the new Project context. Consumer Key/Secret "
                                "can stay the same — only the access pair needs "
                                "regenerating."),
        }
    if "401" == str(status) or "unauthorized" in body:
        return {
            "diagnosis":     "auth_failed",
            "fix":           ("OAuth signature didn't match. Most common cause: "
                                "newline appended to one of the 4 env vars when "
                                "pasted from the dashboard. Re-paste each value "
                                "via `railway variables set --service dchub-backend "
                                "TWITTER_ACCESS_TOKEN=<paste>` instead of the web "
                                "UI. Second cause: regenerated tokens not propagated."),
        }
    if "403" == str(status) and "tweet.write" in body:
        return {
            "diagnosis":     "missing_tweet_write_scope",
            "fix":           ("App permissions are read-only. In dev-portal → "
                                "your App → User authentication settings → set "
                                "App permissions to 'Read and write'. Then "
                                "regenerate Access Token + Secret (old ones are "
                                "read-only forever)."),
        }
    if "duplicate content" in body or "status is over" in body:
        return {
            "diagnosis":     "duplicate_or_too_long",
            "fix":           "Just a content issue. Auth is fine.",
        }
    if "429" == str(status) or "rate limit" in body:
        return {
            "diagnosis":     "rate_limit",
            "fix":           ("Rate limit (free tier: 17 posts/24h, 1500/month). "
                                "Wait or upgrade to Basic tier ($100/mo, 100/day) "
                                "or Pro ($5000/mo) on developer.x.com."),
        }
    return {
        "diagnosis":  "unknown",
        "fix":        ("Unfamiliar error. Paste the raw response body into the X "
                        "dev-portal support form: https://developer.x.com/en/support."),
    }


# ── Endpoints ───────────────────────────────────────────────────────

@twitter_diagnostic_bp.route(
    "/api/v1/admin/twitter/diagnose", methods=["GET"]
)
def diagnose():
    """Read-only check. Pings GET /2/users/me with the OAuth1 quad.
    Doesn't post anything. Safe to spam."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    state = _env_state()

    # Env presence pre-flight
    needed = ["TWITTER_API_KEY_set", "TWITTER_API_SECRET_set",
              "TWITTER_ACCESS_TOKEN_set", "TWITTER_ACCESS_SECRET_set"]
    missing = [k.replace("_set","") for k in needed if not state.get(k)]
    if missing:
        return jsonify({
            "ok":      False,
            "diagnosis":  "env_vars_missing",
            "missing_env_vars": missing,
            "env_state":  state,
            "fix":     (f"Set these in Railway: {', '.join(missing)}. "
                          "Strip whitespace when pasting (dashboard UIs "
                          "often append newlines)."),
        }), 200

    if state.get("TWITTER_PUBLISHER_ENABLED") not in ("1", "true", "yes"):
        return jsonify({
            "ok":      False,
            "diagnosis":  "publisher_disabled",
            "env_state":  state,
            "fix":     ("Set TWITTER_PUBLISHER_ENABLED=true in Railway. "
                          "OAuth credentials look set; the kill switch is the "
                          "only thing blocking the publisher loop."),
            "note":    ("Diagnose still ran — it bypasses the kill switch — "
                          "but the daily publisher loop will skip until you flip "
                          "this env var."),
        }), 200

    # Real ping — GET /2/users/me requires only tweet.read which every
    # OAuth1 token has, so a success here proves OAuth1 + Project state
    # are both correct. No tweet.write coupling.
    try:
        import requests
        from requests_oauthlib import OAuth1
        auth = OAuth1(
            os.environ.get("TWITTER_API_KEY", "").strip(),
            os.environ.get("TWITTER_API_SECRET", "").strip(),
            os.environ.get("TWITTER_ACCESS_TOKEN", "").strip(),
            os.environ.get("TWITTER_ACCESS_SECRET", "").strip(),
        )
        r = requests.get(
            "https://api.twitter.com/2/users/me",
            auth=auth,
            timeout=10,
        )
        if r.status_code in (200, 201):
            data = (r.json() or {}).get("data") or {}
            return jsonify({
                "ok":          True,
                "diagnosis":   "credentials_valid",
                "handle":      data.get("username"),
                "user_id":     data.get("id"),
                "display":     data.get("name"),
                "env_state":   state,
                "next_step":   ("Run POST /api/v1/admin/twitter/test-tweet to "
                                  "verify tweet.write scope. Or wait for the next "
                                  "6h publisher tick — it'll drain the queue."),
            }), 200
        diag = _interpret_x_error(r.status_code, r.text or "")
        return jsonify({
            "ok":          False,
            "x_status":    r.status_code,
            "x_body":      (r.text or "")[:600],
            "env_state":   state,
            **diag,
        }), 200
    except ImportError:
        return jsonify({
            "ok": False,
            "diagnosis": "missing_requests_oauthlib",
            "fix": "Add requests-oauthlib to requirements.txt + redeploy.",
        }), 200
    except Exception as e:
        return jsonify({
            "ok":     False,
            "diagnosis": "exception",
            "error":  f"{type(e).__name__}: {str(e)[:200]}",
            "env_state": state,
        }), 200


@twitter_diagnostic_bp.route(
    "/api/v1/admin/twitter/test-tweet", methods=["POST"]
)
def test_tweet():
    """Fires a single test tweet via the existing _post_to_twitter path.
    Confirms tweet.write scope works end-to-end. Admin keyed."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    test_text = (request.args.get("text")
                   or f"DC Hub X publisher verification — "
                       f"{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}. "
                       f"Live data center intelligence: dchub.cloud")
    test_text = test_text[:240]

    try:
        from content_publisher import _post_to_twitter
        success, result = _post_to_twitter(test_text)
    except Exception as e:
        return jsonify({
            "ok":     False,
            "error":  f"{type(e).__name__}: {str(e)[:200]}",
            "hint":   "_post_to_twitter raised — see Railway logs",
        }), 200

    if success:
        return jsonify({
            "ok":         True,
            "tweet_id":   result,
            "url":        (f"https://x.com/i/web/status/{result}"
                              if result and result != "posted" else None),
            "text":       test_text,
            "next_step":  ("Tweet posted successfully. The 6h publisher loop "
                            "will now drain the social_media_posts queue at "
                            "every tick. Confirm by polling "
                            "/api/v1/content-engine/status."),
        }), 200

    # Failure path — parse the error to surface root cause
    diag = _interpret_x_error(0, str(result))
    return jsonify({
        "ok":           False,
        "x_response":   str(result)[:600],
        **diag,
    }), 200
