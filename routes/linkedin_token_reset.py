"""
linkedin_token_reset.py — force DB token to match env var.

Phase ZZZZZ-round44 (2026-05-25). linkedin_poster._get_valid_token()
reads from DB first; env var is fallback only. User refreshed env var
twice but stale REVOKED token in linkedin_tokens table kept being used.
This endpoint UPDATEs the DB row to match the env var so subsequent
posts pick up the fresh token.
"""
import os, datetime, urllib.request, urllib.error, json, logging
from contextlib import contextmanager
from flask import Blueprint, jsonify, request
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

logger = logging.getLogger("linkedin_token")

linkedin_token_reset_bp = Blueprint("linkedin_token_reset", __name__,
                                     url_prefix="/api/v1/linkedin/token")

# pg session/xact advisory-lock id so overlapping cron fires (or the two
# backend replicas) can never double-rotate a refresh_token (LinkedIn rotates
# it on every refresh — a second concurrent call would present an already-spent
# token and fail). Any stable int; unique to this job.
_REFRESH_LOCK_ID = 8730414

# Proactive-refresh windows (days). Refresh once the token is within
# _REFRESH_WITHIN_DAYS of expiry; if it is within _ALERT_WITHIN_DAYS AND there
# is no usable refresh_token (cron cannot save it), alert LOUDLY instead.
_REFRESH_WITHIN_DAYS = int(os.environ.get("LINKEDIN_TOKEN_REFRESH_WITHIN_DAYS", "10"))
_ALERT_WITHIN_DAYS   = int(os.environ.get("LINKEDIN_TOKEN_ALERT_WITHIN_DAYS", "7"))


def _usable_refresh(val):
    """A refresh_token is usable only if it's a non-empty, non-whitespace
    string. The reset/env-seed paths historically stored '' or NULL, both of
    which mean 'no refresh token' — treat them identically."""
    return bool(val and str(val).strip())


def _resolve_refresh_write(existing, supplied):
    """Decide what to STORE for refresh_token on a reset-from-env write.

    Rule (the fix for the silent-clobber bug): only set refresh_token when a
    real new value is supplied; otherwise preserve the existing one. NEVER
    overwrite a real refresh_token with '' or NULL.

      supplied non-empty            -> supplied (a genuinely new token)
      supplied empty, existing real -> existing (PRESERVE — the bug was ''
                                        clobbering this)
      neither                       -> None (store NULL, never '')

    This is the Python mirror of the SQL the endpoint runs; unit-tested.
    """
    s = (supplied or "").strip()
    if s:
        return s
    e = (existing or "").strip()
    return e or None


def refresh_decision(now, expires_at, has_refresh, has_creds,
                     refresh_within_days=_REFRESH_WITHIN_DAYS,
                     alert_within_days=_ALERT_WITHIN_DAYS):
    """Pure decision gate for the proactive refresh cron. No I/O — unit-tested.

    Returns (action, reason) where action is one of:
      'refresh' — token near expiry AND a usable refresh_token + client creds
                  exist -> call LinkedIn's refresh endpoint.
      'alert'   — token near expiry AND no usable refresh_token -> the cron
                  CANNOT save it; the owner must re-seed via OAuth. Loud alert.
      'skip'    — nothing to do this tick.
    """
    if expires_at is None:
        return ("skip", "no_expiry_recorded")
    days = (expires_at - now).total_seconds() / 86400.0
    if has_refresh and has_creds and days <= refresh_within_days:
        return ("refresh", f"{days:.1f}d_to_expiry")
    if (not has_refresh) and days <= alert_within_days:
        return ("alert", f"{days:.1f}d_to_expiry_no_usable_refresh_token")
    return ("skip", f"{days:.1f}d_to_expiry")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()

def _probe(token):
    try:
        req = urllib.request.Request(
            "https://api.linkedin.com/v2/me",
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": "DCHub-TokenReset/1.0",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        return False, None, f"{e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"

@linkedin_token_reset_bp.route("/reset-from-env", methods=["POST", "GET"])
def reset_from_env():
    out = {"at": datetime.datetime.utcnow().isoformat() + "Z"}
    env_token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip().split()[0] if os.environ.get("LINKEDIN_ACCESS_TOKEN") else ""
    if not env_token:
        out["error"] = "LINKEDIN_ACCESS_TOKEN env var not set"
        return jsonify(out), 400

    ok, info, err = _probe(env_token)
    out["env_token_probe_ok"] = ok
    if err: out["env_token_error"] = err
    if ok and info:
        out["env_token_identity"] = {
            "id": info.get("id"),
            "name": (info.get("localizedFirstName", "") + " " + info.get("localizedLastName", "")).strip(),
            "member_urn": (f"urn:li:person:{info.get('id')}" if info.get("id") else None),
        }
    if not ok:
        out["verdict"] = "env_token_itself_broken"
        return jsonify(out), 200

    if not (_pg and _dsn()):
        return jsonify(out), 200

    member_urn = (out.get("env_token_identity") or {}).get("member_urn")
    _co_id = os.environ.get("LINKEDIN_COMPANY_ID", "").strip()
    company_urn = (f"urn:li:organization:{_co_id}" if _co_id else None)
    expires = datetime.datetime.utcnow() + datetime.timedelta(days=60)

    # Optional: caller may supply a genuinely new refresh_token to seed
    # alongside the access token. We NEVER fabricate one — a real refresh_token
    # is minted only by the OAuth callback (/api/linkedin/auth).
    try:
        supplied_refresh = (request.args.get("refresh_token")
                            or (request.get_json(silent=True) or {}).get("refresh_token")
                            or "")
    except Exception:
        supplied_refresh = ""

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT refresh_token FROM linkedin_tokens ORDER BY id DESC LIMIT 1")
            _row = cur.fetchone()
            existing_refresh = _row[0] if _row else None
            # PRESERVE an existing real refresh_token; only write a new one when
            # actually supplied. Never clobber a real token with '' / NULL
            # (the silent-clobber bug that made auto-refresh un-armable).
            resolved_refresh = _resolve_refresh_write(existing_refresh, supplied_refresh)
            if _row is None:
                cur.execute("""INSERT INTO linkedin_tokens (access_token, refresh_token, expires_at, member_urn, company_urn) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                            (env_token, resolved_refresh, expires, member_urn, company_urn))
            else:
                cur.execute("""UPDATE linkedin_tokens SET access_token=%s, refresh_token=%s, expires_at=%s, updated_at=NOW() WHERE id=(SELECT MAX(id) FROM linkedin_tokens)""",
                            (env_token, resolved_refresh, expires))
            c.commit()
        out["db_status"] = "updated"
        out["refresh_token_preserved"] = bool(
            _usable_refresh(existing_refresh) and not (supplied_refresh or "").strip())
    except Exception as e:
        out["db_status"] = f"failed: {type(e).__name__}: {str(e)[:140]}"

    return jsonify(out), 200

# AUTO-REPAIR: duplicate route '/status' also in enhanced_promotion.py:824 — review and remove one
@linkedin_token_reset_bp.route("/status", methods=["GET"])
def status():
    out = {"env_var_set": bool(os.environ.get("LINKEDIN_ACCESS_TOKEN")),
           "company_id_set": bool(os.environ.get("LINKEDIN_COMPANY_ID")),
           # Liveness fields — PRESENCE only, never the secret values.
           "client_creds_set": bool(os.environ.get("LINKEDIN_CLIENT_ID")
                                     and os.environ.get("LINKEDIN_CLIENT_SECRET")),
           "refresh_cron_disabled": os.environ.get("LINKEDIN_TOKEN_REFRESH_CRON_DISABLE") == "1",
           "has_refresh_token": None,
           "days_to_expiry": None}
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, member_urn, company_urn, expires_at, updated_at FROM linkedin_tokens ORDER BY id DESC LIMIT 3")
                rows = cur.fetchall()
                for r in rows:
                    for k, v in list(r.items()):
                        if isinstance(v, datetime.datetime): r[k] = v.isoformat()
                out["db_rows"] = rows
                # Derive liveness from the canonical row WITHOUT emitting any
                # token value: booleans + an integer day count only.
                cur.execute("SELECT refresh_token, expires_at FROM linkedin_tokens ORDER BY id DESC LIMIT 1")
                cr = cur.fetchone()
                if cr:
                    out["has_refresh_token"] = _usable_refresh(cr.get("refresh_token"))
                    exp = cr.get("expires_at")
                    if isinstance(exp, datetime.datetime):
                        now = datetime.datetime.now(exp.tzinfo) if exp.tzinfo else datetime.datetime.utcnow()
                        out["days_to_expiry"] = int((exp - now).total_seconds() // 86400)
                    # self-sustaining = a usable refresh_token AND client creds:
                    # the cron can then keep the token alive with no human action.
                    out["self_sustaining"] = bool(out["has_refresh_token"] and out["client_creds_set"])
                else:
                    out["self_sustaining"] = False
        except Exception as e:
            out["db_error"] = str(e)[:120]
    return jsonify(out), 200


def _loud_expiry_alert(days_to_expiry, out):
    """LOUD alert when the token is near expiry and the cron CANNOT rescue it
    (no usable refresh_token). Reuses the repo's DB-independent email path
    (email_fallback.send_email_resilient — the same helper health_alerter uses)
    and logs at ERROR. NEVER invents a new channel. Fail-soft."""
    subject = (f"🚨 DC Hub: LinkedIn token expires in ~{days_to_expiry}d and CANNOT auto-refresh")
    body = (
        f"<h2>LinkedIn posting token is about to lapse — manual re-auth required</h2>"
        f"<p>The <code>linkedin_tokens</code> row has <b>no usable refresh_token</b>, "
        f"so the daily refresh cron cannot save it. In ~{days_to_expiry} day(s) "
        f"<code>_get_valid_token()</code> will return None and the ENTIRE LinkedIn "
        f"feed goes dark.</p>"
        f"<p><b>Fix (one-time):</b> visit "
        f"<a href=\"https://dchub.cloud/api/linkedin/auth\">https://dchub.cloud/api/linkedin/auth</a> "
        f"and re-authorize. The OAuth callback stores a real refresh_token, after "
        f"which the cron self-sustains (rotates the token indefinitely).</p>"
        f"<p>Liveness: <code>/api/v1/linkedin/token/status</code>.</p>")
    logger.error("[LinkedIn] TOKEN NEAR EXPIRY (~%sd) with NO usable refresh_token — "
                 "owner must re-auth at /api/linkedin/auth", days_to_expiry)
    try:
        from email_fallback import send_email_resilient
        to = (os.environ.get("ADMIN_ALERT_EMAIL")
              or os.environ.get("DCHUB_ADMIN_EMAIL") or "azmartone@gmail.com").strip()
        sent = send_email_resilient(to, subject, html_content=body,
                                    from_name="DC Hub LinkedIn Token Watch")
        out["alert_emailed"] = bool(sent)
    except Exception as e:
        out["alert_email_error"] = f"{type(e).__name__}: {str(e)[:120]}"


@linkedin_token_reset_bp.route("/refresh-cron", methods=["POST", "GET"])
def refresh_cron():
    """Proactive daily refresh of the LinkedIn access token.

    Wired into routes/cron_heartbeat.py _DISPATCH. When the token is within
    ~10 days of expiry AND a usable refresh_token + client creds exist, call
    LinkedIn's refresh endpoint (via linkedin_poster.refresh_access_token — the
    single shared HTTP path) and persist the new access + rotated refresh_token.
    When the token is within ~7 days of expiry with NO usable refresh_token
    (the cron cannot save it), emit a LOUD alert so the owner re-seeds via OAuth
    before the feed goes dark.

    Leader-gated by a pg advisory xact-lock so overlapping heartbeat fires / the
    two backend replicas can never double-rotate. Fail-soft + idempotent (once
    refreshed, expiry jumps ~60d out so subsequent fires decide 'skip').
    Kill switch: LINKEDIN_TOKEN_REFRESH_CRON_DISABLE=1.
    """
    out = {"at": datetime.datetime.utcnow().isoformat() + "Z"}
    if os.environ.get("LINKEDIN_TOKEN_REFRESH_CRON_DISABLE") == "1":
        out["disabled"] = True
        return jsonify(out), 200
    if not (_pg and _dsn()):
        out["error"] = "db_unavailable"
        return jsonify(out), 200

    has_creds = bool(os.environ.get("LINKEDIN_CLIENT_ID")
                     and os.environ.get("LINKEDIN_CLIENT_SECRET"))
    now = datetime.datetime.now(datetime.timezone.utc)
    refresh_token = None
    expires_at = None
    try:
        with _conn() as c, c.cursor() as cur:
            # Advisory xact-lock: auto-released at txn end; a second concurrent
            # runner gets False and no-ops (so it can't spend a rotating token).
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_REFRESH_LOCK_ID,))
            if not cur.fetchone()[0]:
                out["skipped"] = "lock_held"
                return jsonify(out), 200
            cur.execute("SELECT refresh_token, expires_at FROM linkedin_tokens ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                refresh_token, expires_at = row[0], row[1]
            # keep the advisory lock held (open txn) across the decision + any
            # refresh; the write itself happens on a separate connection.
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)

            has_refresh = _usable_refresh(refresh_token)
            action, reason = refresh_decision(now, expires_at, has_refresh, has_creds)
            out.update({"action": action, "reason": reason,
                        "has_refresh_token": has_refresh,
                        "client_creds_set": has_creds})
            if expires_at is not None:
                out["days_to_expiry"] = int((expires_at - now).total_seconds() // 86400)

            if action == "refresh":
                try:
                    from linkedin_poster import refresh_access_token
                    result = refresh_access_token(refresh_token)
                    out["refresh_result"] = {k: v for k, v in result.items()
                                             if k != "access_token"}
                    if not result.get("ok"):
                        # Refresh endpoint rejected us (e.g. refresh_token also
                        # expired) — if we're now inside the alert window, escalate.
                        if out.get("days_to_expiry", 999) <= _ALERT_WITHIN_DAYS:
                            _loud_expiry_alert(out.get("days_to_expiry"), out)
                except Exception as e:
                    out["refresh_error"] = f"{type(e).__name__}: {str(e)[:140]}"
            elif action == "alert":
                _loud_expiry_alert(out.get("days_to_expiry"), out)
            # release lock by ending txn
            c.commit()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"

    return jsonify(out), 200
