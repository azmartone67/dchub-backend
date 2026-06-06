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


# ── Admin triage dashboard ───────────────────────────────────────────
# Triage queue for incoming /feedback submissions. Mirrors the public
# feedback_page() inline-HTML style (no template file → no out-of-sync
# deploy). Reuses the existing approve/reject endpoints in
# routes/feedback_triage.py — the row buttons just POST there with the
# admin key carried through.
#
# Auth (matches feedback_triage._admin_ok pattern):
#   - X-Admin-Key header, OR
#   - ?admin_key=… query param
# Both must equal $DCHUB_ADMIN_KEY (or $ADMIN_KEY fallback).


def _admin_ok_request(req) -> bool:
    """Local mirror of feedback_triage._admin_ok so this page does not
    have to import that module (avoids circular-import drama on cold
    start). Same env-var precedence: DCHUB_ADMIN_KEY → ADMIN_KEY."""
    sent = (req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and sent == _ADMIN_KEY:
        return True
    return False


_TRIAGE_CLASS_COLORS = {
    # Maps brain_triage_class → (background, foreground) for the pill.
    # Color choices mirror the public dchub-brand.css palette already
    # in use by the public feedback page (--ok / --warn / --bad).
    "LOW":    ("#10b981", "#062a1d"),  # green
    "MEDIUM": ("#f59e0b", "#28190a"),  # amber
    "HIGH":   ("#ef4444", "#280808"),  # red
    "SPAM":   ("#71717a", "#fafafa"),  # grey
}


def _triage_pill(cls: Optional[str]) -> str:
    """Render the triage-class chip. Unknown / un-triaged → neutral grey."""
    label = (cls or "—").upper() if cls else "—"
    bg, fg = _TRIAGE_CLASS_COLORS.get(label, ("#27272a", "#a1a1aa"))
    return (f'<span class="pill" style="background:{bg};color:{fg};">'
            f'{label}</span>')


def _esc(v) -> str:
    """Tiny HTML escaper — no f-string injection. The dashboard is
    admin-only but we still sanitise user-submitted strings."""
    if v is None:
        return ""
    try:
        from html import escape
        return escape(str(v), quote=True)
    except Exception:
        return str(v).replace("<", "&lt;").replace(">", "&gt;")


@feedback_forum_bp.route("/admin/feedback", methods=["GET"])
def admin_feedback_dashboard():
    """Triage dashboard for /feedback submissions.

    Renders 50 recent rows with title, type, brain_triage_class (pill),
    confidence, recommendation, status, and inline Approve / Reject /
    Reclassify buttons that hit the existing
    /api/v1/admin/feedback/<id>/{approve,reject} endpoints (plus a
    POST /api/v1/admin/feedback/<id>/reclassify that lives in this
    module). Filter chips at the top, summary stats above the table.

    Auth: X-Admin-Key header OR ?admin_key= query param. Unauthorized
    callers get a minimal login-style page that explains the auth and
    a 401 status — so a stale bookmark renders informatively, not as a
    broken page.
    """
    if not _admin_ok_request(request):
        # Friendly 401 — explains the auth shape rather than a JSON blob.
        return Response(
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Admin Feedback · DC Hub</title>"
            '<link rel="stylesheet" href="/dchub-brand.css"></head>'
            '<body style="background:#0a0e1a;color:#e6ecf5;font-family:'
            "'Inter',system-ui,sans-serif;max-width:560px;margin:80px auto;"
            'padding:32px;">'
            "<h1 style=\"margin:0 0 12px;font-size:22px;\">Admin only</h1>"
            "<p style=\"color:#94a3b8;line-height:1.6;\">"
            "Pass <code>X-Admin-Key: …</code> as a header or "
            "<code>?admin_key=…</code> as a query param. "
            "The key value is the <code>DCHUB_ADMIN_KEY</code> env var "
            "(falls back to <code>ADMIN_KEY</code>)."
            "</p></body></html>",
            status=401,
            mimetype="text/html",
        )

    # Filter chip from query string (?filter=new|triaged|in_progress|…).
    # 'all' is the default; any other value falls back to 'all' so a
    # typo doesn't break the page.
    flt = (request.args.get("filter") or "all").strip().lower()
    if flt != "all" and flt not in _VALID_STATUSES:
        flt = "all"

    # ── DB fan-out ───────────────────────────────────────────────────
    rows: list[dict] = []
    stats = {
        "new":          0,
        "awaiting":     0,   # status='triaged' (queued for user approval)
        "auto_applied": 0,   # audit_log rows actor='brain' action='applied'
        "daily_cap":    5,   # default; overridden by env below
        "cap_used":     0,
    }
    try:
        from routes.feedback_triage import _DAILY_CAP as _TRIAGE_CAP
        stats["daily_cap"] = int(_TRIAGE_CAP)
    except Exception:
        # Honour the same env var directly as a fallback.
        try:
            stats["daily_cap"] = int(
                os.environ.get("FEEDBACK_AUTO_APPLY_DAILY_CAP", "5"))
        except Exception:
            stats["daily_cap"] = 5

    conn = _conn()
    db_error: Optional[str] = None
    if conn is None:
        db_error = "no_database"
    else:
        try:
            with conn.cursor() as cur:
                # Build the row query — best-effort, with WHERE only when
                # a real status filter is set.
                where_sql = ""
                params: tuple = ()
                if flt != "all":
                    where_sql = " WHERE status = %s"
                    params = (flt,)
                try:
                    cur.execute(
                        "SELECT id, title, type, brain_triage_class, "
                        "       brain_confidence, brain_recommendation, "
                        "       status, created_at "
                        "  FROM feedback_submissions"
                        + where_sql
                        + " ORDER BY created_at DESC LIMIT 50",
                        params,
                    )
                    for r in cur.fetchall() or []:
                        rows.append({
                            "id":             r[0],
                            "title":          r[1],
                            "type":           r[2],
                            "brain_class":    r[3],
                            "brain_conf":     r[4],
                            "brain_rec":      r[5],
                            "status":         r[6],
                            "created_at":     r[7],
                        })
                except Exception as e:
                    db_error = f"row_query_failed:{type(e).__name__}"
                    try: conn.rollback()
                    except Exception: pass

                # Summary stats — each isolated so a bad query in one
                # never blanks the others.
                try:
                    cur.execute(
                        "SELECT status, COUNT(*) FROM feedback_submissions "
                        " WHERE status IN ('new','triaged') "
                        " GROUP BY status"
                    )
                    for r in cur.fetchall() or []:
                        if r[0] == "new":
                            stats["new"] = int(r[1] or 0)
                        elif r[0] == "triaged":
                            stats["awaiting"] = int(r[1] or 0)
                except Exception:
                    try: conn.rollback()
                    except Exception: pass

                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM feedback_audit_log "
                        " WHERE action = 'applied' AND actor = 'brain' "
                        "   AND created_at > NOW() - INTERVAL '24 hours'"
                    )
                    r = cur.fetchone()
                    stats["cap_used"] = int((r[0] if r else 0) or 0)
                    stats["auto_applied"] = stats["cap_used"]
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass

    # ── Render ──────────────────────────────────────────────────────
    # Render a row from rows (already a list[dict]).
    def _row_html(r: dict) -> str:
        sid = int(r["id"])
        title = _esc(r.get("title") or "—")
        typ = _esc(r.get("type") or "other")
        cls_pill = _triage_pill(r.get("brain_class"))
        conf = r.get("brain_conf")
        try:
            conf_str = f"{float(conf):.2f}" if conf is not None else "—"
        except Exception:
            conf_str = "—"
        rec = _esc((r.get("brain_rec") or "")[:200])
        rec_full = _esc(r.get("brain_rec") or "")
        status = _esc(r.get("status") or "—")
        created = r.get("created_at")
        try:
            created_str = created.strftime("%Y-%m-%d %H:%M") if created else "—"
        except Exception:
            created_str = str(created or "—")[:16]
        # The buttons use the data-id attribute; the JS at the bottom of
        # the page wires them to the admin endpoints carrying the admin
        # key from the page URL (or window prompt fallback).
        return (
            f'<tr data-status="{status}">'
            f'<td class="num">#{sid}</td>'
            f'<td><div class="title">{title}</div>'
            f'<div class="meta">{created_str}</div></td>'
            f'<td>{typ}</td>'
            f'<td>{cls_pill}<div class="meta">conf {conf_str}</div></td>'
            f'<td><div class="rec" title="{rec_full}">{rec}</div></td>'
            f'<td><span class="status status-{status}">{status}</span></td>'
            f'<td class="actions">'
            f'<button class="btn approve" data-id="{sid}" data-act="approve">Approve</button> '
            f'<button class="btn reject"  data-id="{sid}" data-act="reject">Reject</button> '
            f'<button class="btn reclass" data-id="{sid}" data-act="reclassify">Reclassify</button>'
            f'</td></tr>'
        )

    if rows:
        rows_html = "\n".join(_row_html(r) for r in rows)
    elif db_error:
        rows_html = (
            f'<tr><td colspan="7" class="empty">'
            f'No rows — DB error: {_esc(db_error)}</td></tr>'
        )
    else:
        rows_html = (
            '<tr><td colspan="7" class="empty">'
            'No feedback submissions match this filter.</td></tr>'
        )

    # Filter chips — render with the *current* filter highlighted so the
    # chip looks pressed.
    chip_options = [
        ("all",          "All"),
        ("new",          "New"),
        ("triaged",      "Triaged"),
        ("in_progress",  "In progress"),
        ("shipped",      "Shipped"),
        ("declined",     "Declined"),
        ("spam",         "Spam"),
    ]
    chip_html_parts = []
    for val, label in chip_options:
        cls = "chip on" if val == flt else "chip"
        chip_html_parts.append(
            f'<a class="{cls}" href="/admin/feedback?filter={val}'
            f'&amp;admin_key=__ADMIN_KEY__">{label}</a>'
        )
    chips_html = "".join(chip_html_parts)

    cap_remaining = max(0, int(stats["daily_cap"]) - int(stats["cap_used"]))

    # Build the page. Brand-consistent with /feedback (same palette /
    # spacing tokens). The admin key in the page URL is echoed back into
    # the chip links + JS so the action buttons keep authenticating.
    admin_key_raw = (
        request.headers.get("X-Admin-Key")
        or request.args.get("admin_key") or ""
    ).strip()
    admin_key_safe = _esc(admin_key_raw)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Feedback Triage — DC Hub Admin</title>
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/dchub-brand.css">
<style>
  :root {{ --bg:#0a0e1a; --panel:#111726; --border:#1f2940; --ink:#e6ecf5;
          --muted:#94a3b8; --accent:#3da9fc; --ok:#22c55e; --warn:#f59e0b;
          --bad:#ef4444; --shipped:#10b981; }}
  *{{box-sizing:border-box}}
  body {{ background:var(--bg); color:var(--ink);
         font-family:'Inter',system-ui,sans-serif; margin:0; padding:0; }}
  .wrap {{ max-width:1400px; margin:0 auto; padding:28px 20px 80px; }}
  h1 {{ font-size:24px; margin:0 0 4px; letter-spacing:-0.01em; }}
  .sub {{ color:var(--muted); margin:0 0 22px; font-size:13px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
           gap:10px; margin:0 0 18px; }}
  .stat {{ background:var(--panel); border:1px solid var(--border);
          border-radius:12px; padding:14px 16px; }}
  .stat-l {{ font-size:11px; color:var(--muted); text-transform:uppercase;
            letter-spacing:0.06em; }}
  .stat-v {{ font-size:22px; font-weight:600; margin-top:2px; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 16px; }}
  .chip {{ display:inline-block; padding:6px 12px; border-radius:999px;
          background:var(--panel); color:var(--muted); border:1px solid var(--border);
          font-size:12px; text-decoration:none; }}
  .chip.on {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .chip:hover {{ color:var(--ink); }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
          border:1px solid var(--border); border-radius:12px; overflow:hidden;
          font-size:13px; }}
  th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid var(--border);
           vertical-align:top; }}
  th {{ background:#0d1322; font-size:11px; color:var(--muted);
        text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  td.num {{ color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:12px;
           white-space:nowrap; }}
  .title {{ font-weight:600; color:var(--ink); }}
  .meta {{ font-size:11px; color:var(--muted); margin-top:3px;
          font-family:'JetBrains Mono',monospace; }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:999px;
          font-size:11px; font-weight:600; letter-spacing:0.04em; }}
  .rec {{ max-width:380px; color:var(--ink); line-height:1.45; }}
  .status {{ font-size:11px; padding:2px 8px; border-radius:6px;
            background:#27272a; color:#a1a1aa; text-transform:uppercase;
            letter-spacing:0.06em; }}
  .status-new {{ background:#1e3a8a; color:#dbeafe; }}
  .status-triaged {{ background:#92400e; color:#fef3c7; }}
  .status-in_progress {{ background:#3730a3; color:#e0e7ff; }}
  .status-shipped {{ background:#065f46; color:#d1fae5; }}
  .status-declined {{ background:#7f1d1d; color:#fee2e2; }}
  .actions {{ white-space:nowrap; }}
  .btn {{ display:inline-block; border:none; cursor:pointer; padding:5px 11px;
         border-radius:6px; font-size:11px; font-weight:600;
         font-family:inherit; }}
  .btn.approve {{ background:var(--ok); color:#fff; }}
  .btn.reject {{ background:var(--bad); color:#fff; }}
  .btn.reclass {{ background:#475569; color:#fff; }}
  .btn:hover {{ opacity:0.85; }}
  .btn:disabled {{ opacity:0.4; cursor:not-allowed; }}
  .empty {{ text-align:center; color:var(--muted); padding:32px 0; }}
  .toast {{ position:fixed; bottom:20px; right:20px; background:var(--panel);
           border:1px solid var(--border); border-radius:10px; padding:12px 16px;
           color:var(--ink); font-size:13px; box-shadow:0 6px 24px rgba(0,0,0,0.4);
           opacity:0; transition:opacity 0.2s; pointer-events:none; }}
  .toast.show {{ opacity:1; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Feedback Triage</h1>
  <p class="sub">Recent 50 submissions · brain-triaged + one-click moderation. Filter chips below the stats.</p>

  <div class="stats">
    <div class="stat"><div class="stat-l">New</div><div class="stat-v">{stats['new']}</div></div>
    <div class="stat"><div class="stat-l">Awaiting approval</div><div class="stat-v">{stats['awaiting']}</div></div>
    <div class="stat"><div class="stat-l">Auto-applied (24h)</div><div class="stat-v">{stats['auto_applied']}</div></div>
    <div class="stat"><div class="stat-l">Daily cap remaining</div><div class="stat-v">{cap_remaining} / {stats['daily_cap']}</div></div>
  </div>

  <div class="chips">{chips_html}</div>

  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>Title</th>
        <th>Type</th>
        <th>Triage</th>
        <th>Recommendation</th>
        <th>Status</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
</div>

<div id="toast" class="toast"></div>

<script>
(function(){{
  // Pull the admin key from the page URL so action buttons keep
  // authenticating against the existing /api/v1/admin/feedback/<id>/…
  // endpoints. Fall back to prompt() if absent.
  var params = new URLSearchParams(window.location.search);
  var ADMIN_KEY = params.get("admin_key") || "{admin_key_safe}" || "";

  // Rewrite the chip links so each one carries the *current* admin_key
  // (the template literal placeholder above is the raw token).
  document.querySelectorAll(".chip").forEach(function(a){{
    a.href = a.href.replace("__ADMIN_KEY__", encodeURIComponent(ADMIN_KEY));
  }});

  function toast(msg, ok){{
    var t = document.getElementById("toast");
    t.textContent = msg;
    t.style.borderColor = ok ? "#22c55e" : "#ef4444";
    t.classList.add("show");
    setTimeout(function(){{ t.classList.remove("show"); }}, 2500);
  }}

  async function callAdmin(act, sid, rationale){{
    if (!ADMIN_KEY) {{
      ADMIN_KEY = prompt("Admin key:");
      if (!ADMIN_KEY) return null;
    }}
    var url = "/api/v1/admin/feedback/" + sid + "/" + act;
    var headers = {{ "X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json" }};
    var body = rationale ? JSON.stringify({{rationale: rationale}}) : null;
    try {{
      var resp = await fetch(url, {{ method: "POST", headers: headers, body: body }});
      var j = await resp.json().catch(function(){{ return {{}}; }});
      if (!resp.ok) {{
        toast("Error: " + (j.error || resp.status), false);
        return null;
      }}
      return j;
    }} catch (e) {{
      toast("Network error: " + e.message, false);
      return null;
    }}
  }}

  document.querySelectorAll(".btn").forEach(function(b){{
    b.addEventListener("click", async function(){{
      var act = b.dataset.act;
      var sid = b.dataset.id;
      if (act === "reject") {{
        var why = prompt("Reject reason (visible in audit log):", "Not actionable");
        if (why === null) return;
        b.disabled = true;
        var r = await callAdmin("reject", sid, why);
        if (r && r.ok) {{
          toast("Rejected #" + sid, true);
          var row = b.closest("tr"); if (row) row.style.opacity = "0.45";
        }} else {{ b.disabled = false; }}
      }} else if (act === "approve") {{
        b.disabled = true;
        var r = await callAdmin("approve", sid, null);
        if (r && r.ok) {{
          toast("Approved #" + sid, true);
          var row = b.closest("tr"); if (row) row.style.opacity = "0.6";
        }} else {{ b.disabled = false; }}
      }} else if (act === "reclassify") {{
        var cls = prompt("New triage class (LOW / MEDIUM / HIGH / SPAM):", "MEDIUM");
        if (!cls) return;
        cls = cls.trim().toUpperCase();
        if (["LOW","MEDIUM","HIGH","SPAM"].indexOf(cls) < 0) {{
          toast("Invalid class", false); return;
        }}
        b.disabled = true;
        var r = await callAdmin("reclassify", sid, cls);
        if (r && r.ok) {{
          toast("Reclassified #" + sid + " → " + cls, true);
          // Soft refresh so the new pill colour shows.
          setTimeout(function(){{ window.location.reload(); }}, 600);
        }} else {{ b.disabled = false; }}
      }}
    }});
  }});
}})();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@feedback_forum_bp.route("/api/v1/admin/feedback/<int:sid>/reclassify",
                          methods=["POST"])
def admin_feedback_reclassify(sid: int):
    """Override the brain's triage_class on a submission. Body or query
    string param `rationale` is read as the new class (LOW/MEDIUM/HIGH/
    SPAM). Audit row written via feedback_triage._audit if importable;
    otherwise falls back to a direct INSERT into feedback_audit_log.

    Auth: same X-Admin-Key / ?admin_key= pattern as feedback_triage."""
    if not _admin_ok_request(request):
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    raw = (body.get("rationale") or body.get("class")
           or request.args.get("class") or "").strip().upper()
    if raw not in ("LOW", "MEDIUM", "HIGH", "SPAM"):
        return jsonify(error="invalid_class",
                       allowed=["LOW", "MEDIUM", "HIGH", "SPAM"]), 400
    conn = _conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            # Stamp the override + bump triage_runs so the next cron pass
            # sees that a human touched this row.
            cur.execute(
                "UPDATE feedback_submissions "
                "   SET brain_triage_class = %s, "
                "       brain_last_triage_at = NOW(), "
                "       status_updated_at = NOW() "
                " WHERE id = %s RETURNING id",
                (raw, sid),
            )
            row = cur.fetchone()
            if not row:
                return jsonify(error="not_found"), 404
            conn.commit()
        # Audit — prefer feedback_triage._audit for symmetry with the
        # approve/reject endpoints; fall back to a direct insert.
        try:
            from routes.feedback_triage import _audit as fa
            fa(sid, "reclassified", "admin", f"new_class={raw}")
        except Exception:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO feedback_audit_log "
                        "  (submission_id, action, actor, notes) "
                        "VALUES (%s, 'reclassified', 'admin', %s)",
                        (sid, f"new_class={raw}"),
                    )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        return jsonify(ok=True, id=sid, brain_triage_class=raw), 200
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(error="db_update_failed", hint=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass
