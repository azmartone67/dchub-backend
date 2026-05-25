"""
linkedin_token_reset.py — force DB token to match env var.

Phase ZZZZZ-round44 (2026-05-25). linkedin_poster._get_valid_token()
reads from DB first; env var is fallback only. User refreshed env var
twice but stale REVOKED token in linkedin_tokens table kept being used.
This endpoint UPDATEs the DB row to match the env var so subsequent
posts pick up the fresh token.
"""
import os, datetime, urllib.request, urllib.error, json
from contextlib import contextmanager
from flask import Blueprint, jsonify
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

linkedin_token_reset_bp = Blueprint("linkedin_token_reset", __name__,
                                     url_prefix="/api/v1/linkedin/token")

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

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM linkedin_tokens")
            n = cur.fetchone()[0]
            if n == 0:
                cur.execute("""INSERT INTO linkedin_tokens (access_token, refresh_token, expires_at, member_urn, company_urn) VALUES (%s, %s, %s, %s, %s)""",
                            (env_token, "", expires, member_urn, company_urn))
            else:
                cur.execute("""UPDATE linkedin_tokens SET access_token=%s, expires_at=%s, updated_at=NOW() WHERE id=(SELECT MAX(id) FROM linkedin_tokens)""",
                            (env_token, expires))
            c.commit()
        out["db_status"] = "updated"
    except Exception as e:
        out["db_status"] = f"failed: {type(e).__name__}: {str(e)[:140]}"

    return jsonify(out), 200

@linkedin_token_reset_bp.route("/status", methods=["GET"])
def status():
    out = {"env_var_set": bool(os.environ.get("LINKEDIN_ACCESS_TOKEN")),
           "company_id_set": bool(os.environ.get("LINKEDIN_COMPANY_ID"))}
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, member_urn, company_urn, expires_at, updated_at FROM linkedin_tokens ORDER BY id DESC LIMIT 3")
                rows = cur.fetchall()
                for r in rows:
                    for k, v in list(r.items()):
                        if isinstance(v, datetime.datetime): r[k] = v.isoformat()
                out["db_rows"] = rows
        except Exception as e:
            out["db_error"] = str(e)[:120]
    return jsonify(out), 200
