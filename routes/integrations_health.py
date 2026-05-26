"""
integrations_health.py — Phase r54 (2026-05-25).

Single endpoint that pings every external integration and reports
which tokens are working vs broken. User runs after each rotation
to confirm the fix landed.

Integrations checked:
  - LinkedIn   (GET /v2/userinfo with LINKEDIN_ACCESS_TOKEN)
  - Twitter/X  (GET /2/users/me with TWITTER_BEARER_TOKEN)
  - Bluesky    (POST /xrpc/com.atproto.server.createSession with HANDLE+APP_PASSWORD)
  - ERCOT      (lightweight grid endpoint with ERCOT_TOKEN)
  - heroic-reprieve REFRESH_SECRET (POST /refresh — 401 if mismatched)
  - Anthropic  (presence check on ANTHROPIC_API_KEY)
  - GitHub     (gh API ping with GITHUB_TOKEN / PR_SUBMIT_TOKEN)

Output:
  GET /api/v1/admin/integrations/health
  {
    "ok": false,
    "summary": {"healthy": 4, "broken": 2, "missing_env": 1},
    "checks": {
      "linkedin": {"ok": true, "status": 200, "last_checked": "..."},
      "twitter":  {"ok": false, "status": 401, "error": "Unauthorized"},
      ...
    }
  }

Admin-keyed.
"""
from __future__ import annotations

import datetime
import os
import urllib.request
import urllib.error
import json
import base64

from flask import Blueprint, jsonify, request


integrations_health_bp = Blueprint("integrations_health", __name__)


def _admin_authorized() -> bool:
    """Same chain as other admin endpoints."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


def _http_get(url: str, headers: dict | None = None, timeout: int = 10) -> dict:
    """Plain HTTP GET. Returns {ok, status, body_preview, error}."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(800).decode("utf-8", errors="replace")
            return {"ok": True, "status": r.status, "body_preview": body[:200]}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read(400).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return {"ok": False, "status": e.code, "error": e.reason,
                "body_preview": err_body[:200]}
    except Exception as e:
        return {"ok": False, "status": None,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _http_post(url: str, headers: dict | None = None,
                body: bytes | None = None, timeout: int = 10) -> dict:
    try:
        req = urllib.request.Request(url, data=body or b"", headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(800).decode("utf-8", errors="replace")
            return {"ok": True, "status": r.status, "body_preview": body[:200]}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read(400).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return {"ok": False, "status": e.code, "error": e.reason,
                "body_preview": err_body[:200]}
    except Exception as e:
        return {"ok": False, "status": None,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ── Per-integration check functions ─────────────────────────────────

def _check_linkedin() -> dict:
    tok = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    if not tok:
        return {"status": "missing_env", "ok": False,
                "hint": "Set LINKEDIN_ACCESS_TOKEN in resourceful-essence Railway env."}
    out = _http_get("https://api.linkedin.com/v2/userinfo",
                     headers={"Authorization": f"Bearer {tok}"})
    out["env_var"] = "LINKEDIN_ACCESS_TOKEN"
    if out.get("status") == 401:
        out["fix"] = ("Token expired. Visit https://dchub.cloud/api/linkedin/auth "
                       "to re-authorize, then update LINKEDIN_ACCESS_TOKEN in Railway.")
    return out


def _check_twitter() -> dict:
    tok = (os.environ.get("TWITTER_BEARER_TOKEN") or "").strip()
    if not tok:
        return {"status": "missing_env", "ok": False,
                "hint": "Set TWITTER_BEARER_TOKEN in resourceful-essence Railway env."}
    out = _http_get("https://api.twitter.com/2/users/me",
                     headers={"Authorization": f"Bearer {tok}"})
    out["env_var"] = "TWITTER_BEARER_TOKEN"
    if out.get("status") == 401:
        out["fix"] = ("Token expired or revoked. Generate a new bearer token at "
                       "https://developer.twitter.com/en/portal/dashboard, then "
                       "update TWITTER_BEARER_TOKEN in Railway.")
    return out


def _check_bluesky() -> dict:
    handle = (os.environ.get("BLUESKY_HANDLE") or "").strip()
    app_pw = (os.environ.get("BLUESKY_APP_PASSWORD") or "").strip()
    if not handle or not app_pw:
        return {"status": "missing_env", "ok": False,
                "env_var": "BLUESKY_HANDLE + BLUESKY_APP_PASSWORD",
                "hint": ("Bluesky distribution DARK. Set BLUESKY_HANDLE (e.g. "
                          "dchub.bsky.social) and BLUESKY_APP_PASSWORD (generate "
                          "at bsky.app/settings/app-passwords) in Railway.")}
    out = _http_post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"identifier": handle, "password": app_pw}).encode(),
    )
    out["env_var"] = "BLUESKY_HANDLE + BLUESKY_APP_PASSWORD"
    if out.get("status") == 401:
        out["fix"] = ("App password invalid. Regenerate at "
                       "https://bsky.app/settings/app-passwords and update Railway env.")
    return out


def _check_ercot() -> dict:
    tok = (os.environ.get("ERCOT_API_KEY") or
           os.environ.get("ERCOT_BEARER_TOKEN") or
           os.environ.get("ERCOT_TOKEN") or "").strip()
    if not tok:
        return {"status": "missing_env", "ok": False,
                "env_var": "ERCOT_API_KEY",
                "hint": "Set ERCOT_API_KEY in Railway."}
    out = _http_get(
        "https://api.ercot.com/api/public-reports/np6-905-cd/spp_node_zone_hub?size=1",
        headers={"Authorization": f"Bearer {tok}",
                  "Ocp-Apim-Subscription-Key": tok},
    )
    out["env_var"] = "ERCOT_API_KEY"
    if out.get("status") in (401, 403):
        out["fix"] = ("ERCOT credential expired/wrong. Refresh subscription "
                       "key at https://apiexplorer.ercot.com, update Railway env.")
    return out


def _check_refresh_secret() -> dict:
    """Heroic-reprieve /refresh — verifies REFRESH_SECRET matches."""
    secret = (os.environ.get("REFRESH_SECRET") or
               os.environ.get("DAILY_REFRESH_SECRET") or "").strip()
    base = (os.environ.get("DAILY_BASE")
            or "https://dchub-backend-production-f7dd.up.railway.app").rstrip("/")
    out = _http_post(
        f"{base}/refresh",
        headers={"X-Refresh-Secret": secret} if secret else {},
    )
    out["env_var"] = "REFRESH_SECRET"
    out["target"] = base
    if out.get("status") == 401:
        out["fix"] = ("REFRESH_SECRET in resourceful-essence does NOT match "
                       "the one in heroic-reprieve. Rotate to the same value "
                       "in BOTH Railway services + GH Actions secret.")
    return out


def _check_anthropic() -> dict:
    tok = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not tok:
        return {"status": "missing_env", "ok": False,
                "env_var": "ANTHROPIC_API_KEY",
                "hint": "Brain L23 Opus proposals will be skipped without this."}
    # Don't burn a real token call; presence check + format sanity.
    if not tok.startswith("sk-ant-"):
        return {"status": "malformed", "ok": False,
                "env_var": "ANTHROPIC_API_KEY",
                "hint": "Token doesn't start with sk-ant- (likely wrong value)."}
    return {"ok": True, "status": "present",
            "env_var": "ANTHROPIC_API_KEY",
            "note": "Format sanity only (no API call to avoid spend)."}


def _check_github() -> dict:
    tok = (os.environ.get("GITHUB_TOKEN") or
           os.environ.get("PR_SUBMIT_TOKEN") or "").strip()
    if not tok:
        return {"status": "missing_env", "ok": False,
                "env_var": "GITHUB_TOKEN / PR_SUBMIT_TOKEN",
                "hint": ("Set PR_SUBMIT_TOKEN as a GH Actions secret + "
                          "GITHUB_TOKEN in Railway for L22 auto-code + "
                          "awesome-mcp-pr workflow.")}
    out = _http_get("https://api.github.com/user",
                     headers={"Authorization": f"Bearer {tok}",
                              "User-Agent": "DCHub-IntegrationsHealth/1.0"})
    out["env_var"] = "GITHUB_TOKEN"
    if out.get("status") == 401:
        out["fix"] = ("PAT expired. Generate new fine-grained PAT at "
                       "https://github.com/settings/tokens?type=beta with "
                       "contents:write + pull_requests:write.")
    return out


_CHECKS = {
    "linkedin":         _check_linkedin,
    "twitter":          _check_twitter,
    "bluesky":          _check_bluesky,
    "ercot":            _check_ercot,
    "refresh_secret":   _check_refresh_secret,
    "anthropic":        _check_anthropic,
    "github":           _check_github,
}


@integrations_health_bp.route(
    "/api/v1/admin/integrations/health", methods=["GET"]
)
def integrations_health():
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    only = (request.args.get("only") or "").strip()
    checks_to_run = [only] if only and only in _CHECKS else list(_CHECKS)

    results = {}
    healthy = 0
    broken = 0
    missing = 0
    for name in checks_to_run:
        try:
            r = _CHECKS[name]()
        except Exception as e:
            r = {"ok": False, "error": f"check_exception: {type(e).__name__}: {str(e)[:140]}"}
        r["last_checked"] = datetime.datetime.utcnow().isoformat() + "Z"
        results[name] = r
        if r.get("status") == "missing_env":
            missing += 1
        elif r.get("ok"):
            healthy += 1
        else:
            broken += 1

    return jsonify({
        "ok":          broken == 0 and missing == 0,
        "summary":     {"healthy": healthy, "broken": broken,
                         "missing_env": missing,
                         "total": len(results)},
        "checks":      results,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "purpose":      ("Verifies every external integration's token "
                          "after rotation. Hit this after updating Railway "
                          "env to confirm the fix worked."),
    }), 200
