"""
routes/feedback_forum.py — Customer Feedback Forum v1 (2026-06-06).

A /feedback page where customers submit bugs/features/data issues.
The brain auto-triages (see routes/feedback_triage.py): trivially-safe
fixes apply themselves, medium-risk fixes queue for one-click approval,
anything touching pricing/auth/tiers emails the user for a manual call.

Endpoints (this file):
  POST /api/v1/feedback/submit         public, IP-rate-limited (3/day)
  GET  /api/v1/feedback/list           public, anonymized, paginated
  POST /api/v1/feedback/<id>/vote      IP-hashed upvote, 1 per IP
  GET  /feedback                       HTML page (form + list)

Schema bootstrap is in init_feedback_tables() and is wired into
content_publisher.init_content_tables() so it runs on boot.

Anti-spam:
  - Email shape validation when present (RFC 5322 lite).
  - IP rate limit: 3 submissions / day / IP hash.
  - Hidden honeypot field (must be empty).
  - CF Turnstile token check IF CLOUDFLARE_TURNSTILE_SITEKEY env is set.
  - Brain prompt-injection sniff in feedback_triage.py.

Anonymization:
  - Raw email + raw IP NEVER stored; we store sha256(raw + DCHUB_IP_SALT).
  - Public list shows masked email ("j****@gmail.com") + state from IP geo.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, Response, jsonify, render_template_string, request

logger = logging.getLogger(__name__)

feedback_forum_bp = Blueprint("feedback_forum", __name__)


# ── env ───────────────────────────────────────────────────────────────

_IP_SALT = (os.environ.get("DCHUB_IP_SALT")
            or os.environ.get("DCHUB_SESSION_SECRET")
            or "dchub-feedback-default-salt")

_TURNSTILE_SITEKEY = (os.environ.get("CLOUDFLARE_TURNSTILE_SITEKEY") or "").strip()
_TURNSTILE_SECRET = (os.environ.get("CLOUDFLARE_TURNSTILE_SECRET") or "").strip()

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("ADMIN_KEY") or "").strip()

_VALID_TYPES = {"bug", "feature", "enhancement", "data_issue",
                "wrong_number", "other"}

_VALID_STATUSES = {"new", "triaged", "in_progress", "shipped", "declined"}

# RFC 5322 lite — shape only, not a delivery probe.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# ── DB helpers ────────────────────────────────────────────────────────


def _conn():
    """Open a RAW psycopg2 connection. Callers use `conn.cursor()` directly
    and `conn.close()` in finally, so _conn() must return a raw connection —
    NOT a context manager.

    r-fix (2026-06-06): the old primary path returned routes._iso_common.conn()
    which is a @contextmanager (a _GeneratorContextManager), so every forum
    endpoint AND init_feedback_tables 500'd with
    "'_GeneratorContextManager' object has no attribute 'cursor'" — the whole
    forum was dead and the table was never created. Use raw psycopg2.connect."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn)
    except Exception:
        pass
    return None


def init_feedback_tables():
    """Idempotent CREATE TABLE IF NOT EXISTS + ALTER ADD COLUMN IF NOT EXISTS.
    Called from content_publisher.init_content_tables() at boot.

    Defensive: never raises; logs and continues so a partial failure
    doesn't take down content_publisher (and the whole app)."""
    conn = _conn()
    if conn is None:
        logger.warning("feedback_forum: no DB; skipping table init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_submissions (
                        id                    BIGSERIAL PRIMARY KEY,
                        title                 TEXT NOT NULL,
                        description           TEXT NOT NULL,
                        type                  TEXT NOT NULL DEFAULT 'other',
                        url_referenced        TEXT,
                        submitter_email_masked TEXT,
                        submitter_email_hash  TEXT,
                        submitter_ip_hash     TEXT,
                        submitter_state       TEXT,
                        votes_up              INT NOT NULL DEFAULT 0,
                        brain_triage_class    TEXT,
                        brain_recommendation  TEXT,
                        brain_confidence      NUMERIC(3,2),
                        brain_proposed_diff_hash TEXT,
                        brain_triage_runs     INT NOT NULL DEFAULT 0,
                        brain_last_triage_at  TIMESTAMPTZ,
                        status                TEXT NOT NULL DEFAULT 'new',
                        decline_rationale     TEXT,
                        fix_pr_url            TEXT,
                        fix_commit_sha        TEXT,
                        approve_token         TEXT,
                        approve_token_expires TIMESTAMPTZ,
                        created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        status_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception as e:
                logger.warning("feedback_submissions CREATE skipped: %s", e)
                try: conn.rollback()
                except Exception: pass
            # Add missing columns idempotently (in case the table existed
            # from a prior partial deploy).
            for col_def in [
                "brain_triage_class TEXT",
                "brain_recommendation TEXT",
                "brain_confidence NUMERIC(3,2)",
                "brain_proposed_diff_hash TEXT",
                "brain_triage_runs INT NOT NULL DEFAULT 0",
                "brain_last_triage_at TIMESTAMPTZ",
                "decline_rationale TEXT",
                "fix_pr_url TEXT",
                "fix_commit_sha TEXT",
                "approve_token TEXT",
                "approve_token_expires TIMESTAMPTZ",
                "votes_up INT NOT NULL DEFAULT 0",
                "submitter_email_masked TEXT",
                "submitter_email_hash TEXT",
                "submitter_ip_hash TEXT",
                "submitter_state TEXT",
                "url_referenced TEXT",
                "status_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE feedback_submissions "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            # Indexes.
            for ix in [
                "CREATE INDEX IF NOT EXISTS ix_feedback_status_created "
                "ON feedback_submissions (status, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_feedback_type "
                "ON feedback_submissions (type)",
                "CREATE INDEX IF NOT EXISTS ix_feedback_brain_class "
                "ON feedback_submissions (brain_triage_class)",
                "CREATE INDEX IF NOT EXISTS ix_feedback_ip_hash_created "
                "ON feedback_submissions (submitter_ip_hash, created_at)",
            ]:
                try:
                    cur.execute(ix)
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            # Votes.
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_votes (
                        id            BIGSERIAL PRIMARY KEY,
                        submission_id BIGINT NOT NULL,
                        voter_ip_hash TEXT NOT NULL,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception as e:
                logger.warning("feedback_votes CREATE skipped: %s", e)
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_feedback_votes_sub_ip "
                    "ON feedback_votes (submission_id, voter_ip_hash)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            # Spam quarantine.
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_spam_quarantine (
                        id                BIGSERIAL PRIMARY KEY,
                        raw_payload       JSONB,
                        classifier_reason TEXT,
                        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception as e:
                logger.warning("feedback_spam_quarantine CREATE skipped: %s", e)
                try: conn.rollback()
                except Exception: pass
            # Audit log.
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_audit_log (
                        id            BIGSERIAL PRIMARY KEY,
                        submission_id BIGINT,
                        action        TEXT NOT NULL,
                        actor         TEXT NOT NULL,
                        notes         TEXT,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception as e:
                logger.warning("feedback_audit_log CREATE skipped: %s", e)
                try: conn.rollback()
                except Exception: pass
        logger.info("feedback_forum: tables initialized")
    finally:
        try: conn.close()
        except Exception: pass


def _audit(submission_id: Optional[int], action: str, actor: str,
           notes: str = "") -> None:
    """Append an audit-log row. Never raises."""
    try:
        conn = _conn()
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feedback_audit_log "
                    "(submission_id, action, actor, notes) "
                    "VALUES (%s, %s, %s, %s)",
                    (submission_id, action[:60], actor[:60], notes[:600])
                )
                conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning("feedback audit failed: %s", e)


# ── Hashing + anonymization ───────────────────────────────────────────


def _client_ip() -> str:
    """Caller IP, honoring CF-Connecting-IP / X-Forwarded-For first."""
    for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        v = request.headers.get(h, "").strip()
        if v:
            return v.split(",")[0].strip()
    return (request.remote_addr or "0.0.0.0").strip()


def _hash_ip(ip: str) -> str:
    return hashlib.sha256((ip + "|" + _IP_SALT).encode("utf-8")).hexdigest()


def _hash_email(email: str) -> str:
    return hashlib.sha256(
        (email.lower().strip() + "|" + _IP_SALT).encode("utf-8")
    ).hexdigest()


def _mask_email(email: str) -> str:
    """j****@gmail.com style masking. Never crashes on weird input."""
    e = (email or "").strip()
    if "@" not in e:
        return ""
    local, _, domain = e.partition("@")
    if not local:
        return "*@" + domain
    if len(local) == 1:
        return local + "*@" + domain
    return local[0] + "*" * max(3, min(8, len(local) - 1)) + "@" + domain


def _state_from_ip(ip: str) -> Optional[str]:
    """Cheap state derivation from CF headers (we set this at insert time
    once; we don't probe geo APIs synchronously). Returns 2-letter code or None."""
    # Cloudflare's CF-Region-Code is "VA" etc. when it's a US 2-letter region.
    region = (request.headers.get("CF-Region-Code", "")
              or request.headers.get("CF-IPRegionCode", "")).strip().upper()
    if region and len(region) == 2 and region.isalpha():
        return region
    # Fallback: use country code so the UI never says "User from None"
    country = (request.headers.get("CF-IPCountry", "")
               or request.headers.get("X-Country", "")).strip().upper()
    if country and len(country) == 2 and country.isalpha():
        return country
    return None


# ── Anti-spam ─────────────────────────────────────────────────────────


def _verify_turnstile(token: str, ip: str) -> tuple[bool, str]:
    """Verify a CF Turnstile token. No-ops + returns True if Turnstile
    isn't configured (so dev/local + early launch work)."""
    if not _TURNSTILE_SECRET:
        return True, "turnstile_not_configured"
    if not token:
        return False, "missing_token"
    try:
        import requests as _rq
        r = _rq.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": _TURNSTILE_SECRET, "response": token,
                  "remoteip": ip},
            timeout=8,
        )
        d = r.json() or {}
        if d.get("success") is True:
            return True, "ok"
        return False, "siteverify_failed:" + ",".join(d.get("error-codes", []))[:80]
    except Exception as e:
        # Fail-OPEN on a Turnstile API hiccup — better to accept a few
        # bot rows than silently 503 every customer submission.
        logger.warning("turnstile verify error (fail-open): %s", e)
        return True, "verify_error_fail_open"


def _rate_limit_ok(ip_hash: str) -> bool:
    """3 submissions per IP-hash per 24h. Conservative — counts pre-spam-
    quarantine rows as part of the budget so bots can't burn through
    quarantine to flood real submissions."""
    conn = _conn()
    if conn is None:
        return True  # fail-open if DB is down
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM feedback_submissions "
                "WHERE submitter_ip_hash = %s "
                "  AND created_at > NOW() - INTERVAL '24 hours'",
                (ip_hash,)
            )
            row = cur.fetchone()
            n = int((row[0] if not hasattr(row, "get") else row.get("count")) or 0)
            return n < 3
    except Exception:
        return True
    finally:
        try: conn.close()
        except Exception: pass


def _quick_spam_class(title: str, description: str) -> Optional[str]:
    """Fast deterministic spam check BEFORE we touch the brain (which
    costs a Claude API call). Returns a reason if spam, else None.

    Catches the obvious stuff (prompt injection, 'test test test',
    asdf-flood) so we never burn an Anthropic call on them."""
    blob = (title + " " + description).lower()
    if len(blob.strip()) < 10:
        return "too_short"
    inject_patterns = [
        "ignore previous instructions",
        "ignore the above",
        "ignore all prior",
        "you are now",
        "act as",
        "system prompt",
        "as an ai",
        "as a language model",
        "system:",
        "</system>",
        "</instructions>",
        "disregard",
    ]
    for p in inject_patterns:
        if p in blob:
            return f"prompt_injection:{p}"
    # 'test test test' / asdfasdf
    if re.fullmatch(r"(test\s*){2,}", blob.strip()):
        return "test_test_test"
    if re.fullmatch(r"(asdf|qwer|zxcv){2,}", blob.strip()):
        return "keyboard_mash"
    # All-caps shouting > 60% of total length.
    if len(blob) > 40:
        caps = sum(1 for c in blob if c.isupper())
        if caps / max(1, len(blob)) > 0.6:
            return "all_caps"
    return None


def _quarantine(payload: dict, reason: str) -> None:
    try:
        conn = _conn()
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feedback_spam_quarantine "
                    "(raw_payload, classifier_reason) VALUES (%s, %s)",
                    (json.dumps(payload)[:8000], reason[:200])
                )
                conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning("quarantine insert failed: %s", e)


# ── Endpoints ─────────────────────────────────────────────────────────


@feedback_forum_bp.route("/api/v1/feedback/submit", methods=["POST"])
def submit_feedback():
    """Insert a new feedback submission. Anti-spam first, then triage
    runs out-of-band via the cron (see routes/feedback_triage.py).

    Returns {id, status} on success, or {error, hint} on rejection.
    Always 200 for spam quarantine (we don't tell bots they were flagged)."""
    _ensure_ff_schema()
    data = request.get_json(silent=True) or request.form.to_dict() or {}

    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    type_ = (data.get("type") or "other").strip().lower()
    url_ref = (data.get("url_referenced") or data.get("url") or "").strip()
    email = (data.get("email") or "").strip()
    honeypot = (data.get("website") or data.get("hp_url") or "").strip()
    turnstile_token = (data.get("cf-turnstile-response")
                       or data.get("turnstile_token") or "").strip()

    # Honeypot — anyone who fills this is a bot.
    if honeypot:
        _quarantine({"raw": dict(data), "trap": "honeypot"}, "honeypot_filled")
        return jsonify(ok=True, id=None, status="received"), 200

    # Required field validation.
    if not (10 <= len(title) <= 120):
        return jsonify(error="invalid_title",
                       hint="title must be 10–120 chars"), 400
    if not (20 <= len(description) <= 2000):
        return jsonify(error="invalid_description",
                       hint="description must be 20–2000 chars"), 400
    if type_ not in _VALID_TYPES:
        return jsonify(error="invalid_type",
                       hint=f"type must be one of: {sorted(_VALID_TYPES)}"), 400
    if email and not _EMAIL_RE.match(email):
        return jsonify(error="invalid_email",
                       hint="email shape must be valid (we won't probe delivery)"), 400
    if url_ref and len(url_ref) > 500:
        return jsonify(error="invalid_url"), 400

    ip = _client_ip()
    ip_hash = _hash_ip(ip)

    # Turnstile (no-op if not configured).
    ok, reason = _verify_turnstile(turnstile_token, ip)
    if not ok:
        _quarantine({"raw": dict(data), "ip_hash": ip_hash},
                    f"turnstile_fail:{reason}")
        return jsonify(ok=True, id=None, status="received"), 200

    # Rate limit.
    if not _rate_limit_ok(ip_hash):
        return jsonify(error="rate_limited",
                       hint="3 submissions per day per IP. Try again tomorrow."), 429

    # Quick deterministic spam check.
    spam_reason = _quick_spam_class(title, description)
    if spam_reason:
        _quarantine({"title": title, "description": description,
                     "ip_hash": ip_hash, "email_hash": _hash_email(email) if email else None},
                    spam_reason)
        # 200 to bots — never tell them they were flagged.
        return jsonify(ok=True, id=None, status="received"), 200

    email_masked = _mask_email(email) if email else ""
    email_hash = _hash_email(email) if email else ""
    state = _state_from_ip(ip)

    conn = _conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feedback_submissions
                (title, description, type, url_referenced,
                 submitter_email_masked, submitter_email_hash,
                 submitter_ip_hash, submitter_state, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'new')
                RETURNING id
            """, (title[:120], description[:2000], type_, url_ref[:500],
                  email_masked[:120], email_hash, ip_hash, state))
            row = cur.fetchone()
            sid = int((row[0] if not hasattr(row, "get") else row.get("id")) or 0)
            conn.commit()
        _audit(sid, "submitted", "user",
               f"type={type_} state={state or '?'} email={'y' if email else 'n'}")
        return jsonify(ok=True, id=sid, status="new"), 200
    except Exception as e:
        logger.error("feedback submit insert failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return jsonify(error="db_insert_failed",
                       hint=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


_FF_SCHEMA_READY = False


def _ensure_ff_schema():
    """r-fix (2026-06-06): /api/v1/feedback/list 500'd because the SELECT hit
    feedback_submissions before content_publisher.init_content_tables() had
    guaranteed the table + all columns existed (boot-order / schema-drift).
    init_feedback_tables() is idempotent (CREATE + ALTER IF NOT EXISTS), so
    run it once per process at the read/write entrypoints to self-heal."""
    global _FF_SCHEMA_READY
    if _FF_SCHEMA_READY:
        return
    try:
        init_feedback_tables()
    except Exception:
        pass
    _FF_SCHEMA_READY = True


@feedback_forum_bp.route("/api/v1/feedback/list", methods=["GET"])
def list_feedback():
    """Public anonymized list. Query params: status, type, limit (≤50),
    offset, sort (recent|votes|shipped)."""
    _ensure_ff_schema()
    status = (request.args.get("status") or "").strip().lower()
    type_ = (request.args.get("type") or "").strip().lower()
    sort = (request.args.get("sort") or "recent").strip().lower()
    try:
        limit = min(50, max(1, int(request.args.get("limit", 25))))
    except Exception:
        limit = 25
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        offset = 0

    where = ["1=1"]
    params: list = []
    if status and status in _VALID_STATUSES:
        where.append("status = %s")
        params.append(status)
    if type_ and type_ in _VALID_TYPES:
        where.append("type = %s")
        params.append(type_)

    order = "created_at DESC"
    if sort == "votes":
        order = "votes_up DESC, created_at DESC"
    elif sort == "shipped":
        where.append("status = 'shipped'")
        order = "status_updated_at DESC"

    conn = _conn()
    if conn is None:
        return jsonify(items=[], total=0, error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, type, url_referenced, "
                "       submitter_state, votes_up, brain_triage_class, "
                "       status, fix_pr_url, fix_commit_sha, "
                "       decline_rationale, created_at, status_updated_at "
                "FROM feedback_submissions "
                "WHERE " + " AND ".join(where) +
                f" ORDER BY {order} LIMIT %s OFFSET %s",
                params + [limit, offset]
            )
            rows = cur.fetchall() or []
            items = []
            for r in rows:
                if hasattr(r, "get"):
                    d = dict(r)
                else:
                    d = {
                        "id": r[0], "title": r[1], "description": r[2],
                        "type": r[3], "url_referenced": r[4],
                        "submitter_state": r[5], "votes_up": r[6],
                        "brain_triage_class": r[7], "status": r[8],
                        "fix_pr_url": r[9], "fix_commit_sha": r[10],
                        "decline_rationale": r[11],
                        "created_at": r[12], "status_updated_at": r[13],
                    }
                # Defang internal-only fields.
                d.pop("submitter_email_hash", None)
                d.pop("submitter_email_masked", None)
                d.pop("submitter_ip_hash", None)
                d.pop("approve_token", None)
                d.pop("brain_proposed_diff_hash", None)
                if d.get("created_at"):
                    d["created_at"] = str(d["created_at"])
                if d.get("status_updated_at"):
                    d["status_updated_at"] = str(d["status_updated_at"])
                d["display_author"] = ("User from " + d["submitter_state"]
                                       if d.get("submitter_state") else "Anonymous user")
                items.append(d)
            cur.execute(
                "SELECT COUNT(*) FROM feedback_submissions "
                "WHERE " + " AND ".join(where), params
            )
            row = cur.fetchone()
            total = int((row[0] if not hasattr(row, "get") else row.get("count")) or 0)
    except Exception as e:
        logger.error("feedback list error: %s", e)
        return jsonify(items=[], total=0, error="query_failed",
                       hint=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass

    return jsonify(items=items, total=total, limit=limit, offset=offset,
                   filters={"status": status or None, "type": type_ or None,
                            "sort": sort}), 200


@feedback_forum_bp.route("/api/v1/feedback/<int:sid>/vote", methods=["POST"])
def vote_feedback(sid: int):
    """Upvote a submission. Idempotent per IP-hash (UNIQUE index)."""
    ip = _client_ip()
    ip_hash = _hash_ip(ip)
    conn = _conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            # Confirm submission exists.
            cur.execute("SELECT id FROM feedback_submissions WHERE id = %s",
                        (sid,))
            row = cur.fetchone()
            if not row:
                return jsonify(error="not_found"), 404
            try:
                cur.execute(
                    "INSERT INTO feedback_votes (submission_id, voter_ip_hash) "
                    "VALUES (%s, %s)",
                    (sid, ip_hash)
                )
                conn.commit()
            except Exception:
                # Unique violation = already voted. Idempotent OK.
                try: conn.rollback()
                except Exception: pass
                cur.execute(
                    "SELECT votes_up FROM feedback_submissions WHERE id = %s",
                    (sid,))
                vr = cur.fetchone()
                return jsonify(ok=True, already_voted=True,
                               votes_up=int((vr[0] if not hasattr(vr, "get") else vr.get("votes_up")) or 0)), 200
            cur.execute(
                "UPDATE feedback_submissions SET votes_up = votes_up + 1 "
                "WHERE id = %s RETURNING votes_up",
                (sid,)
            )
            vr = cur.fetchone()
            conn.commit()
            new_votes = int((vr[0] if not hasattr(vr, "get") else vr.get("votes_up")) or 0)
        return jsonify(ok=True, votes_up=new_votes), 200
    except Exception as e:
        logger.error("vote error: %s", e)
        try: conn.rollback()
        except Exception: pass
        return jsonify(error="vote_failed", hint=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── HTML page ────────────────────────────────────────────────────────


_FEEDBACK_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Feedback — DC Hub</title>
<meta name="description" content="Tell DC Hub what's broken, what to add, or what number looks wrong. The brain triages — trivial fixes ship same-day.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/dchub-brand.css">
<style>
  :root { --bg:#0a0e1a; --panel:#111726; --border:#1f2940; --ink:#e6ecf5;
          --muted:#94a3b8; --accent:#3da9fc; --ok:#22c55e; --warn:#f59e0b;
          --bad:#ef4444; --shipped:#10b981; }
  body { background:var(--bg); color:var(--ink); font-family:'Inter',system-ui,sans-serif;
         margin:0; padding:0; }
  .wrap { max-width:1200px; margin:0 auto; padding:32px 20px; }
  h1 { font-size:32px; margin:0 0 8px; letter-spacing:-0.02em; }
  .sub { color:var(--muted); margin:0 0 28px; }
  .grid { display:grid; grid-template-columns:1fr 1.4fr; gap:28px; }
  @media (max-width:880px) { .grid { grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:22px; }
  label { display:block; margin-top:14px; font-size:13px; color:var(--muted); }
  input, textarea, select {
    width:100%; box-sizing:border-box; background:#0d1322; color:var(--ink);
    border:1px solid var(--border); border-radius:8px; padding:10px 12px;
    font-family:inherit; font-size:14px; margin-top:6px;
  }
  textarea { min-height:120px; resize:vertical; }
  button.submit { margin-top:18px; padding:11px 20px; background:var(--accent);
                  color:#fff; border:none; border-radius:8px; font-weight:600;
                  cursor:pointer; font-size:14px; }
  button.submit:disabled { opacity:0.5; cursor:not-allowed; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 18px; }
  .chip { padding:5px 11px; border-radius:99px; border:1px solid var(--border);
          background:#0d1322; color:var(--ink); font-size:12px; cursor:pointer;
          user-select:none; }
  .chip.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  .item { border-bottom:1px solid var(--border); padding:14px 0; }
  .item:last-child { border-bottom:none; }
  .item-h { display:flex; justify-content:space-between; align-items:start; gap:12px; }
  .item-title { font-weight:600; font-size:15px; margin:0 0 4px; }
  .item-meta { color:var(--muted); font-size:12px; }
  .item-desc { color:#cbd5e1; font-size:13px; margin:6px 0 0; line-height:1.45; }
  .badge { padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600;
           text-transform:uppercase; letter-spacing:0.04em; }
  .badge.new      { background:#1e293b; color:#94a3b8; }
  .badge.triaged  { background:#1e3a5f; color:#7dd3fc; }
  .badge.in_progress { background:#3b2c0f; color:#fbbf24; }
  .badge.shipped  { background:#0f3a25; color:#34d399; }
  .badge.declined { background:#3a0f0f; color:#f87171; }
  .vote-btn { background:transparent; border:1px solid var(--border); color:var(--ink);
              border-radius:8px; padding:6px 10px; font-size:12px; cursor:pointer;
              white-space:nowrap; }
  .vote-btn:hover { background:#0d1322; }
  .shipped-strip { background:#0f1a14; border:1px solid #1f3a2c; border-radius:12px;
                   padding:14px 18px; margin-top:24px; }
  .shipped-strip h3 { margin:0 0 8px; font-size:14px; color:var(--shipped);
                      text-transform:uppercase; letter-spacing:0.06em; }
  .shipped-row { display:flex; gap:14px; flex-wrap:wrap; font-size:13px;
                 color:#cbd5e1; }
  .shipped-row a { color:var(--accent); text-decoration:none; }
  .msg { padding:12px 14px; border-radius:8px; margin-top:14px; font-size:13px; }
  .msg.ok { background:#0f3a25; color:#86efac; border:1px solid #1f5a3c; }
  .msg.err { background:#3a0f0f; color:#fca5a5; border:1px solid #5a1f1f; }
  .hp { position:absolute; left:-9999px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Tell us what's broken (or missing)</h1>
  <p class="sub">Bug, feature, wrong number, broken link — drop it here. The brain triages:
    trivial fixes ship same-day, judgment calls queue for review.</p>

  <div class="grid">
    <!-- LEFT: form -->
    <div class="card">
      <h2 style="margin:0 0 6px;font-size:18px;">Submit feedback</h2>
      <p style="color:var(--muted);font-size:13px;margin:0 0 12px;">
        Email is optional — only fill it if you want a follow-up.
      </p>
      <form id="fbform" autocomplete="off">
        <label>Title <span style="color:var(--muted)">(10–120 chars)</span>
          <input name="title" maxlength="120" minlength="10" required>
        </label>
        <label>Description <span style="color:var(--muted)">(20–2000 chars)</span>
          <textarea name="description" maxlength="2000" minlength="20" required></textarea>
        </label>
        <label>Type
          <select name="type" required>
            <option value="bug">Bug</option>
            <option value="feature">Feature request</option>
            <option value="enhancement">Enhancement</option>
            <option value="data_issue">Data issue</option>
            <option value="wrong_number">Wrong number</option>
            <option value="other">Other</option>
          </select>
        </label>
        <label>URL / page (optional)
          <input name="url_referenced" placeholder="https://dchub.cloud/...">
        </label>
        <label>Email (optional — only if you want a follow-up)
          <input name="email" type="email" placeholder="you@company.com">
        </label>
        <!-- honeypot -->
        <input class="hp" name="website" tabindex="-1" autocomplete="off">
        __TURNSTILE_WIDGET__
        <button type="submit" class="submit" id="submitbtn">Submit feedback</button>
        <div id="msg"></div>
      </form>
    </div>

    <!-- RIGHT: public list -->
    <div class="card">
      <h2 style="margin:0 0 10px;font-size:18px;">Recent feedback</h2>
      <div class="chips" id="typechips">
        <span class="chip active" data-type="">all</span>
        <span class="chip" data-type="bug">bug</span>
        <span class="chip" data-type="feature">feature</span>
        <span class="chip" data-type="enhancement">enhancement</span>
        <span class="chip" data-type="data_issue">data issue</span>
        <span class="chip" data-type="wrong_number">wrong number</span>
        <span class="chip" data-type="other">other</span>
      </div>
      <div id="list">Loading…</div>
    </div>
  </div>

  <div class="shipped-strip">
    <h3>Recently fixed from feedback</h3>
    <div class="shipped-row" id="shipped">Loading…</div>
  </div>
</div>

<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<script>
const fmt = (d) => { try { return new Date(d).toLocaleDateString(); } catch(e) { return d; } };
const esc = (s) => String(s||'').replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

let activeType = '';

async function loadList() {
  const params = new URLSearchParams();
  if (activeType) params.set('type', activeType);
  params.set('limit', 25);
  try {
    const r = await fetch('/api/v1/feedback/list?' + params.toString());
    const d = await r.json();
    const items = d.items || [];
    if (!items.length) { document.getElementById('list').innerHTML = '<div style="color:var(--muted);padding:20px 0;">No feedback yet — be the first.</div>'; return; }
    document.getElementById('list').innerHTML = items.map(it => `
      <div class="item">
        <div class="item-h">
          <div style="flex:1;">
            <div class="item-title">${esc(it.title)}</div>
            <div class="item-meta">${esc(it.display_author)} · ${fmt(it.created_at)} · <span class="badge ${esc(it.status)}">${esc(it.status)}</span> · ${esc(it.type)}</div>
            <div class="item-desc">${esc((it.description||'').slice(0,260))}${(it.description||'').length>260?'…':''}</div>
            ${it.fix_pr_url ? `<div class="item-meta" style="margin-top:4px;"><a href="${esc(it.fix_pr_url)}" style="color:var(--accent);">View fix</a></div>` : ''}
            ${it.decline_rationale ? `<div class="item-meta" style="margin-top:4px;color:var(--bad);">Declined: ${esc(it.decline_rationale)}</div>` : ''}
          </div>
          <button class="vote-btn" data-id="${it.id}">▲ ${it.votes_up||0}</button>
        </div>
      </div>`).join('');
    document.querySelectorAll('.vote-btn').forEach(b => {
      b.addEventListener('click', async () => {
        const id = b.dataset.id;
        const r = await fetch(`/api/v1/feedback/${id}/vote`, { method: 'POST' });
        const d = await r.json();
        if (d.ok) b.textContent = '▲ ' + (d.votes_up || 0);
      });
    });
  } catch(e) {
    document.getElementById('list').innerHTML = '<div style="color:var(--bad);">Failed to load.</div>';
  }
}

async function loadShipped() {
  try {
    const r = await fetch('/api/v1/feedback/list?status=shipped&limit=8&sort=shipped');
    const d = await r.json();
    const items = d.items || [];
    if (!items.length) { document.getElementById('shipped').innerHTML = '<span style="color:var(--muted)">Nothing shipped from feedback yet.</span>'; return; }
    document.getElementById('shipped').innerHTML = items.map(it => `
      <span>${esc(it.title)} ${it.fix_pr_url?`<a href="${esc(it.fix_pr_url)}">↗</a>`:''}</span>`).join(' · ');
  } catch(e) { document.getElementById('shipped').innerHTML = ''; }
}

document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    activeType = c.dataset.type || '';
    loadList();
  });
});

document.getElementById('fbform').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submitbtn');
  const msg = document.getElementById('msg');
  btn.disabled = true;
  msg.innerHTML = '';
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  // pick up Turnstile token if present
  const ts = document.querySelector('[name="cf-turnstile-response"]');
  if (ts && ts.value) payload['cf-turnstile-response'] = ts.value;
  try {
    const r = await fetch('/api/v1/feedback/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (r.ok && d.ok !== false) {
      msg.innerHTML = '<div class="msg ok">Thanks — submission #' + (d.id||'received') + ' is in. The brain will triage shortly.</div>';
      e.target.reset();
      loadList();
    } else {
      msg.innerHTML = '<div class="msg err">' + esc(d.error || 'submit_failed') + (d.hint ? ' — ' + esc(d.hint) : '') + '</div>';
    }
  } catch(err) {
    msg.innerHTML = '<div class="msg err">Network error — please retry.</div>';
  } finally {
    btn.disabled = false;
  }
});

loadList();
loadShipped();
</script>
</body>
</html>
"""


@feedback_forum_bp.route("/feedback", methods=["GET"])
@feedback_forum_bp.route("/partners/feedback", methods=["GET"])
@feedback_forum_bp.route("/community", methods=["GET"])
def feedback_page():
    # r74 (2026-06-06): dual-route alias. The single-segment /feedback path
    # hits the CF zone-worker allowlist trap (same Error 1000 pattern as
    # /aws/*, /research/*, /docs/* per session memory). /partners/* IS in
    # the CF zone allowlist, so /partners/feedback reaches CF Pages → the
    # backend cleanly. We keep BOTH alive so once the user fixes the CF
    # dashboard rule, the canonical /feedback URL works too without a
    # second deploy.
    """HTML page with form + public list. Renders inline (no template
    file) so a deploy can't get out of sync with a missing template."""
    if _TURNSTILE_SITEKEY:
        widget = (
            f'<div class="cf-turnstile" '
            f'data-sitekey="{_TURNSTILE_SITEKEY}" '
            f'data-theme="dark" style="margin-top:14px;"></div>'
        )
    else:
        widget = ""
    html = _FEEDBACK_PAGE.replace("__TURNSTILE_WIDGET__", widget)
    return Response(html, mimetype="text/html")
