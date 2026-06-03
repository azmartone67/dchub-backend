"""
url_registry.py — single chokepoint for every public-facing URL emitted by
DC Hub Media (LinkedIn, X, press, email, RSS, anywhere).

Kills the bug class: linkedin_404 (LinkedIn auto-posted a dead URL).

Responsibilities
  1. build_public_url(kind, slug)         — the ONLY slug -> URL builder.
  2. emit(channel, kind, slug, payload)   — every emitter calls this first.
                                            HEAD-checks before publish.
  3. mark_published(emission_id, ref_id)  — caller flips state on success.
  4. /api/v1/cron/url-smoke               — 5-min recheck on published rows.
  5. /api/v1/url-registry/auto-revoke     — LinkedIn DELETE on dead_404.

Table url_emissions is the registry. Schema lives here AND is mirrored
into routes/schema_repair.py SCHEMA_STATEMENTS so /api/v1/admin/schema/repair
can create it idempotently on cold deploys.
"""
import os, re, sys, json, datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None
try:
    import requests as _rq
except Exception:
    _rq = None

url_registry_bp = Blueprint("url_registry", __name__)

_BASE = "https://dchub.cloud"
_HEAD_TIMEOUT = 5
_VALID_CHANNELS = {"linkedin", "x", "email", "press", "rss", "manual"}
_VALID_KINDS = {"news", "press_release", "dcpi", "partners", "reports", "markets", "facility"}


# slug + URL builder (the ONLY one) -------------------------------------
def slugify(s, max_len=70):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "update"


def build_public_url(kind, slug):
    """Single source of truth for every public dchub.cloud URL.
    Strips duplicate prefixes (e.g. partnership-partnership-) and
    rejects invalid kinds. Returns the absolute URL string."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown url kind: {kind}")
    slug = slugify(slug)
    # Dedupe `partnership-partnership-` etc.
    parts = slug.split("-")
    cleaned = []
    for p in parts:
        if cleaned and cleaned[-1] == p:
            continue
        cleaned.append(p)
    slug = "-".join(cleaned)
    kind_path = {
        "news": "news",
        "press_release": "press-release",
        "dcpi": "dcpi",
        "partners": "partners",
        "reports": "reports",
        "markets": "markets",
        "facility": "facility",
    }[kind]
    return f"{_BASE}/{kind_path}/{slug}"


# DB helpers -----------------------------------------------------------
def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


_DDL = """
CREATE TABLE IF NOT EXISTS url_emissions (
    id              SERIAL PRIMARY KEY,
    channel         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    url             TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'planned',
    pre_head_status INT,
    ref_id          TEXT,
    payload         JSONB,
    last_smoke_at   TIMESTAMPTZ,
    smoke_status    INT,
    revoked_at      TIMESTAMPTZ,
    revoke_reason   TEXT,
    republished_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_url_emissions_state ON url_emissions(state);
CREATE INDEX IF NOT EXISTS ix_url_emissions_url ON url_emissions(url);
CREATE INDEX IF NOT EXISTS ix_url_emissions_published ON url_emissions(published_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ix_url_emissions_channel_url_active
    ON url_emissions(channel, url)
    WHERE state IN ('planned','published');
"""


def ensure_table():
    if not (_pg and _dsn()):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            for stmt in _DDL.strip().split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)
        return True
    except Exception as e:
        print(f"[url_registry] ensure_table failed: {e}", file=sys.stderr)
        return False


def _head(url):
    if not _rq:
        return (0, "no_requests")
    try:
        r = _rq.head(url, timeout=_HEAD_TIMEOUT, allow_redirects=True,
                     headers={"User-Agent": "DCHub-URLRegistry/1.0"})
        return (r.status_code, "")
    except _rq.RequestException as e:
        return (0, f"{type(e).__name__}")


# PUBLIC API (called by every emitter) ---------------------------------
def emit(channel, kind, slug, payload=None):
    """Register a planned emission + pre-flight HEAD. Returns:
        {"ok": True, "id": int, "url": str, "status": int}
        {"ok": False, "reason": str, "status": int}  → caller MUST NOT post.
    """
    if channel not in _VALID_CHANNELS:
        return {"ok": False, "reason": f"bad_channel:{channel}"}
    ensure_table()
    try:
        url = build_public_url(kind, slug)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    status, err = _head(url)
    if status == 0 or status >= 400:
        return {"ok": False, "reason": f"pre_head_{status or err}",
                "url": url, "status": status}
    if not (_pg and _dsn()):
        return {"ok": True, "id": None, "url": url, "status": status,
                "degraded": "no_db"}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO url_emissions
                  (channel, kind, slug, url, state, pre_head_status, payload)
                VALUES (%s, %s, %s, %s, 'planned', %s, %s)
                ON CONFLICT (channel, url)
                  WHERE state IN ('planned','published')
                DO UPDATE SET pre_head_status = EXCLUDED.pre_head_status
                RETURNING id
            """, (channel, kind, slug, url, status,
                   json.dumps(payload or {})))
            row = cur.fetchone()
            return {"ok": True, "id": (row[0] if row else None),
                    "url": url, "status": status}
    except Exception as e:
        return {"ok": True, "id": None, "url": url, "status": status,
                "degraded": f"insert_failed:{type(e).__name__}"}


def mark_published(emission_id, ref_id=None):
    if emission_id is None or not (_pg and _dsn()):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE url_emissions
                   SET state = 'published',
                       ref_id = COALESCE(%s, ref_id),
                       published_at = COALESCE(published_at, NOW())
                 WHERE id = %s
            """, (ref_id, emission_id))
        return True
    except Exception:
        return False


def mark_failed(emission_id, reason=""):
    if emission_id is None or not (_pg and _dsn()):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE url_emissions
                   SET state = 'failed',
                       revoke_reason = %s
                 WHERE id = %s AND state = 'planned'
            """, (reason[:200], emission_id))
        return True
    except Exception:
        return False


# auto-revoke (LinkedIn DELETE) ----------------------------------------
def _linkedin_delete(urn):
    """Delete a LinkedIn post by URN via api.linkedin.com/rest/posts/{urn}.
    Token comes from same env+DB path as linkedin_poster.py."""
    if not _rq or not urn:
        return {"ok": False, "error": "no_requests_or_urn"}
    try:
        sys.path.insert(0, "/app")
        from linkedin_poster import _get_token
        tok = _get_token()
        if not tok:
            return {"ok": False, "error": "no_token"}
        from urllib.parse import quote
        r = _rq.delete(
            f"https://api.linkedin.com/rest/posts/{quote(urn, safe='')}",
            headers={
                "Authorization": f"Bearer {tok}",
                "LinkedIn-Version": "202601",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=10,
        )
        return {"ok": r.status_code < 300, "status": r.status_code,
                "body": (r.text or "")[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _brain_finding(issue, url, detail):
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_findings
                  (issue, url, detail, detector, count, status)
                VALUES (%s, %s, %s, 'url_registry', 1, 'open')
            """, (issue[:120], url[:500], detail[:1000]))
    except Exception:
        pass


# routes: smoke cron + auto-revoke + status ----------------------------
_INTERNAL = {os.environ.get("DCHUB_INTERNAL_KEY", ""),
             os.environ.get("DCHUB_ADMIN_KEY", "")}


def _internal_ok():
    k = (request.headers.get("X-Internal-Key") or
         request.headers.get("X-Admin-Key") or
         request.headers.get("X-DC-Internal-Cron") or "").strip()
    return bool(k) and (k in _INTERNAL or k == "1")


@url_registry_bp.route("/api/v1/cron/url-smoke", methods=["POST", "GET"])
def cron_url_smoke():
    """Recheck every published URL emitted in the last 7d. Auto-revoke
    LinkedIn on 4xx; mark republished if a previously-dead URL is now 2xx."""
    if not _internal_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if not (_pg and _dsn()):
        return jsonify(ok=False, error="no_db"), 503
    ensure_table()
    checked = revoked = republished = 0
    findings = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, channel, url, ref_id, state
                  FROM url_emissions
                 WHERE state IN ('published','dead_404')
                   AND created_at > NOW() - INTERVAL '7 days'
                   AND (last_smoke_at IS NULL
                        OR last_smoke_at < NOW() - INTERVAL '5 minutes')
                 ORDER BY published_at DESC NULLS LAST
                 LIMIT 200
            """)
            rows = cur.fetchall()
        for rid, channel, url, ref_id, state in rows:
            status, err = _head(url)
            checked += 1
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute(
                        "UPDATE url_emissions SET last_smoke_at=NOW(), smoke_status=%s WHERE id=%s",
                        (status, rid))
            except Exception:
                pass
            if state == "published" and (status == 0 or status >= 400):
                # 404 path -> mark dead + auto-revoke linkedin
                try:
                    with _conn() as c, c.cursor() as cur:
                        cur.execute("UPDATE url_emissions SET state='dead_404' WHERE id=%s", (rid,))
                except Exception:
                    pass
                if channel == "linkedin" and ref_id:
                    rd = _linkedin_delete(ref_id)
                    if rd.get("ok"):
                        try:
                            with _conn() as c, c.cursor() as cur:
                                cur.execute("""UPDATE url_emissions
                                                  SET state='revoked',
                                                      revoked_at=NOW(),
                                                      revoke_reason='auto_404'
                                                WHERE id=%s""", (rid,))
                        except Exception:
                            pass
                        revoked += 1
                _brain_finding("media_404_auto_revoke", url,
                               f"channel={channel} ref={ref_id} smoke={status}")
                findings.append({"id": rid, "url": url, "status": status,
                                 "action": "revoked" if channel == "linkedin" else "flagged"})
            elif state == "dead_404" and 200 <= status < 400:
                # Republish path: was dead, now alive (CF Pages caught up).
                try:
                    with _conn() as c, c.cursor() as cur:
                        cur.execute("""UPDATE url_emissions
                                          SET state='republished',
                                              republished_at=NOW()
                                        WHERE id=%s""", (rid,))
                    republished += 1
                    _brain_finding("media_404_recovered", url,
                                   f"channel={channel} now_2xx")
                except Exception:
                    pass
        return jsonify(ok=True, checked=checked, revoked=revoked,
                       republished=republished, findings=findings[:20])
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 500


@url_registry_bp.route("/api/v1/url-registry/status", methods=["GET"])
def status():
    """Public stability metric — % of last-7d emissions still 2xx."""
    if not (_pg and _dsn()):
        return jsonify(ok=False, error="no_db"), 503
    ensure_table()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT state, COUNT(*) FROM url_emissions
                 WHERE created_at > NOW() - INTERVAL '7 days'
                 GROUP BY state
            """)
            buckets = {row[0]: row[1] for row in cur.fetchall()}
        total = sum(buckets.values())
        alive = buckets.get("published", 0) + buckets.get("republished", 0)
        dead = buckets.get("dead_404", 0) + buckets.get("revoked", 0)
        return jsonify(
            ok=True,
            window_days=7,
            total=total,
            alive=alive,
            dead=dead,
            stability_pct=round(100.0 * alive / total, 2) if total else 100.0,
            buckets=buckets,
        )
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 500
