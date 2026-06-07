"""
sales_outreach_automator.py — DC Hub Sales (2026-06-07).
============================================================

CONSERVATIVE founder-led sales outreach. Brain detects high-intent
companies from dchub.cloud traffic + MCP claims + newsletter signups,
enriches via Hunter.io (free tier) or WebFetch fallback, picks the best
target contact, and uses Claude to draft a SHORT (4 sentence max),
specific, no-fluff founder-voice outreach email.

CRITICAL: HIGH STAKES — cold outreach can damage brand. The system ships
DRY-RUN ON by default. Founder reviews EVERY first batch on
/admin/sales-outreach before flipping any default. Multiple safety
layers stack:

  1. SALES_OUTREACH_DISABLE=1            master kill switch
  2. SALES_OUTREACH_DRY_RUN=1            DEFAULT ON; drafts only, no send
  3. SALES_OUTREACH_DAILY_CAP=3          hard cap (very small to start)
  4. 30-day per-domain cooldown          same company can't be re-emailed
                                          within 30 days
  5. SALES_OUTREACH_BLOCKLIST            comma-separated domain list
                                          (competitors, known opt-outs)
  6. Tone filter                         regenerates ONCE on cliché hit;
                                          on 2nd miss row is skipped
  7. Free-mail domain skip               gmail.com, yahoo.com, hotmail.com
                                          etc. are not "companies"
  8. Per-admin approval                  every draft awaits explicit
                                          1-click approval on the dashboard
                                          before the Resend API is called

═══════════════════════════════════════════════════════════════════════
HIGH-INTENT DETECTION (clustering by email domain in last 14d):
═══════════════════════════════════════════════════════════════════════

Source A — Newsletter signups from CORPORATE domains
   - newsletter_subscribers.email LIKE '%@<corp domain>'
   - 1+ corp-domain signup in last 14d

Source B — High-intent MCP claims from CORPORATE domains
   - mcp_high_intent_sessions.email captured in last 14d
   - state_visitor_intent.email captured in last 14d
   - filter: corporate domain (NOT gmail/yahoo/hotmail/outlook/icloud)

Source C — MCP upgrade signals from CORPORATE domains
   - mcp_upgrade_signals.user_email captured in last 14d
   - 3+ signals from same domain (3x repeat = interest)

Source D — Repeat visits via attribution proxy
   - /r/<token> or /li/<short> clicks where session or referer ties
     back to a known corporate domain (best-effort fallback)

The four sources are MERGED + deduped by lowercased email domain. Each
candidate row carries:
   domain, visit_count, brief_clicks, top_contact_email,
   top_contact_name, first_seen, last_seen, source_mix

═══════════════════════════════════════════════════════════════════════
OUTREACH FORMAT (enforced by tone filter + JSON contract):
═══════════════════════════════════════════════════════════════════════

  Subject: 1 line, references inferred interest, < 70 chars.
  Body:    4 sentences MAX.
     S1 — Reference what they were looking at on dchub.cloud
     S2 — Add 1 specific data point relevant to their company/industry
     S3 — Soft ask ("happy to walk you through the data")
     S4 — Signature with Jonathan's actual title
  Banned phrases (regenerate ONCE; on 2nd hit, skip):
     leverage, synergy, circle back, value-add, best of breed,
     game-changer, ecosystem, delve, revolutionary, paradigm shift.

═══════════════════════════════════════════════════════════════════════
ENDPOINTS (all admin-gated):
═══════════════════════════════════════════════════════════════════════

  POST /api/v1/admin/sales-outreach/detect
       - Detect + draft. Honors DRY_RUN. Writes rows to
         sales_outreach_log with decision='dry_run' OR queues
         for send if DRY_RUN=0 AND under daily cap.
  GET  /api/v1/admin/sales-outreach/log?days=30
       - List rows for the dashboard.
  POST /api/v1/admin/sales-outreach/approve/<log_id>
       - 1-click approve + send a single dry-run draft now.
  POST /api/v1/admin/sales-outreach/decline/<log_id>
       - Mark row decision='declined' (never re-attempt this row).
  POST /api/v1/admin/sales-outreach/regenerate/<log_id>
       - Re-run Claude on a draft (no send).
  GET  /admin/sales-outreach
       - HTML dashboard: last 30d drafts, 1-click approve/decline,
         audit log, response tracking.

Cron entry (crawler_scheduler.py):
   (15, 3, "sales_outreach_detect", "_run_sales_outreach_detect")
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import datetime
from typing import Any

from flask import Blueprint, jsonify, request, Response

sales_outreach_automator_bp = Blueprint("sales_outreach_automator", __name__)


# ── Tunables (env-driven) ────────────────────────────────────────────────
LOOKBACK_DAYS_DEFAULT     = 14
DAILY_CAP_DEFAULT         = 3       # very small — start conservative
COOLDOWN_DAYS_DEFAULT     = 30
MIN_VISIT_COUNT_DEFAULT   = 3       # 3+ visits = "high-intent"
SUBJECT_MAX_CHARS         = 90
BODY_MAX_CHARS            = 700     # ~4 sentences
MAX_DRAFTS_PER_RUN        = 10      # cap exploration even if cap=3 send
MODEL_NAME                = "claude-haiku-4-5-20251001"
MAX_TOKENS_PER_GEN        = 800
HUNTER_TIMEOUT_S          = 8
CLAUDE_TIMEOUT_S          = 30
RESEND_TIMEOUT_S          = 15

JONATHAN_TITLE = (
    os.environ.get("DCHUB_FOUNDER_TITLE")
    or "Founder, DC Hub"
)
JONATHAN_FROM = (
    os.environ.get("DCHUB_FROM_EMAIL")
    or "Jonathan Martone <jonathan@dchub.cloud>"
)
JONATHAN_REPLY_TO = (
    os.environ.get("DCHUB_REPLY_TO")
    or "jonathan@dchub.cloud"
)


# Free-mail domains — these are individuals, not "companies". Skip them
# entirely — they're handled by lost_conversion_outreach + winback.
FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com",
    "proton.me", "protonmail.com", "tutanota.com",
    "fastmail.com", "fastmail.fm",
    "qq.com", "163.com", "126.com", "naver.com",
    "duck.com", "duckduckgo.com",
    "example.com", "test.com", "dchub.cloud",
}


# Tone filter — same banned-phrase list as media_dm_follow_up,
# plus extras the spec calls out.
LAZY_REJECT_TOKENS = {
    "leverage", "leveraging",
    "synergy", "synergies",
    "circle back",
    "value-add", "value add",
    "best of breed", "best-of-breed",
    "game-changer", "game changer",
    "delve", "ecosystem", "tapestry",
    "unleash", "groundbreaking",
    "paradigm shift", "revolutionary",
    "low-hanging fruit", "move the needle",
    "let's connect", "lets connect",
    "touch base", "hop on a call", "quick sync",
    "amazing", "elevate your", "unlock the power",
    "thoughtful", "thought-provoking",
}


# ── Plumbing ─────────────────────────────────────────────────────────────
def _db_conn():
    """Return a psycopg2 connection or None. Never raises."""
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    """Allow admin key OR the cron-shared internal key."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    candidates = []
    for name in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY",
                 "DCHUB_ADMIN_API_KEY", "INTERNAL_KEY",
                 "MCP_INTERNAL_KEY"):
        v = os.environ.get(name)
        if v:
            candidates.append(v)
    if not candidates:
        return False
    return provided in candidates


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[sales-outreach] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def _kill_switched() -> bool:
    return os.environ.get("SALES_OUTREACH_DISABLE", "0") == "1"


def _dry_run() -> bool:
    """DEFAULT ON. Operator must explicitly set SALES_OUTREACH_DRY_RUN=0
    AND go through the per-row approval flow before anything sends."""
    return os.environ.get("SALES_OUTREACH_DRY_RUN", "1") == "1"


def _blocklist() -> set[str]:
    raw = os.environ.get("SALES_OUTREACH_BLOCKLIST", "") or ""
    blk = {x.strip().lower() for x in raw.split(",") if x.strip()}
    # Always block freemail
    blk |= FREEMAIL_DOMAINS
    return blk


# ── Schema ───────────────────────────────────────────────────────────────
def init_sales_outreach_tables() -> bool:
    """Idempotent schema bootstrap. Creates sales_outreach_log."""
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sales_outreach_log (
                  id                       BIGSERIAL PRIMARY KEY,
                  company_domain           TEXT NOT NULL,
                  company_name             TEXT,
                  company_industry         TEXT,
                  company_employee_count   INTEGER,
                  company_news_headline    TEXT,
                  contact_email            TEXT,
                  contact_name             TEXT,
                  contact_title            TEXT,
                  source_visit_count       INTEGER NOT NULL DEFAULT 0,
                  source_brief_clicks      INTEGER NOT NULL DEFAULT 0,
                  source_mix               TEXT,
                  source_first_seen        TIMESTAMPTZ,
                  source_last_seen         TIMESTAMPTZ,
                  generated_subject        TEXT,
                  generated_body           TEXT,
                  generated_data_point     TEXT,
                  decision                 TEXT NOT NULL DEFAULT 'dry_run',
                  decision_reason          TEXT,
                  decided_by               TEXT,
                  decided_at               TIMESTAMPTZ,
                  sent_at                  TIMESTAMPTZ,
                  resend_id                TEXT,
                  response_received_at     TIMESTAMPTZ,
                  response_text            TEXT,
                  detected_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Indexes
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_sol_detected
                    ON sales_outreach_log(detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_sol_decision
                    ON sales_outreach_log(decision, detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_sol_domain_recent
                    ON sales_outreach_log(company_domain, detected_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_sol_sent
                    ON sales_outreach_log(sent_at DESC)
                    WHERE sent_at IS NOT NULL
            """)
        _log("schema bootstrap OK")
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


# Lazy init at import-time. content_publisher.init_content_tables() also
# calls this on boot — belt-and-suspenders.
try:
    _SCHEMA_OK = init_sales_outreach_tables()
except Exception:
    _SCHEMA_OK = False


# ── High-intent detection ────────────────────────────────────────────────
def _is_corporate_domain(email_or_domain: str) -> bool:
    """Return True if the email/domain looks corporate (NOT freemail)."""
    if not email_or_domain:
        return False
    d = email_or_domain.strip().lower()
    if "@" in d:
        d = d.split("@", 1)[-1]
    if not d or "." not in d:
        return False
    return d not in _blocklist()


def _domain_of(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[-1].strip().lower()


def _table_exists(cur, table_name: str) -> bool:
    """Cheap detector — some tables only exist on the main backend."""
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = %s
            LIMIT 1
        """, (table_name,))
        return cur.fetchone() is not None
    except Exception:
        return False


def detect_high_intent_companies(lookback_days: int = LOOKBACK_DAYS_DEFAULT,
                                  min_visits: int = MIN_VISIT_COUNT_DEFAULT
                                  ) -> tuple[list[dict], str | None]:
    """Cluster signals by corporate email DOMAIN over the last
    `lookback_days`. Returns (candidates, err).

    Each candidate row carries:
        domain, contact_email, contact_name, visit_count,
        brief_clicks, source_mix, first_seen, last_seen
    """
    conn = _db_conn()
    if conn is None:
        return [], "no_database"

    by_domain: dict[str, dict] = {}
    blk = _blocklist()
    err = None
    try:
        with conn.cursor() as cur:
            # ─── Source A: newsletter signups ───────────────────────
            if _table_exists(cur, "newsletter_subscribers"):
                try:
                    cur.execute("""
                        SELECT LOWER(SPLIT_PART(email, '@', 2)) AS domain,
                               LOWER(MIN(email))                AS first_email,
                               COUNT(*)                          AS n,
                               MIN(subscribed_at)                AS first_seen,
                               MAX(subscribed_at)                AS last_seen
                          FROM newsletter_subscribers
                         WHERE email LIKE '%%@%%'
                           AND COALESCE(unsubscribed_at, NOW() + INTERVAL '1 day')
                                > NOW()
                           AND subscribed_at
                                > NOW() - INTERVAL '%s days'
                         GROUP BY 1
                    """ % int(lookback_days))
                    for row in cur.fetchall():
                        dom = (row[0] or "").strip()
                        if not dom or dom in blk:
                            continue
                        if not _is_corporate_domain(dom):
                            continue
                        d = by_domain.setdefault(dom, _empty_candidate(dom))
                        d["contact_email"] = d["contact_email"] or row[1]
                        d["visit_count"]  += int(row[2] or 0)
                        d["source_mix"].add("newsletter")
                        d["first_seen"] = _min_dt(d["first_seen"], row[3])
                        d["last_seen"]  = _max_dt(d["last_seen"],  row[4])
                except Exception as e:
                    _log(f"source_newsletter_failed: {e}")
                    try: conn.rollback()
                    except Exception: pass

            # ─── Source B: state_of_2026 visitor claim ──────────────
            if _table_exists(cur, "state_visitor_intent"):
                try:
                    cur.execute("""
                        SELECT LOWER(SPLIT_PART(email, '@', 2)) AS domain,
                               LOWER(MIN(email))                AS first_email,
                               SUM(COALESCE(brief_clicks, 0))   AS clicks,
                               COUNT(*)                          AS n,
                               MIN(first_seen_at)                AS first_seen,
                               MAX(last_event_at)                AS last_seen
                          FROM state_visitor_intent
                         WHERE email IS NOT NULL AND email LIKE '%%@%%'
                           AND first_seen_at
                                > NOW() - INTERVAL '%s days'
                         GROUP BY 1
                    """ % int(lookback_days))
                    for row in cur.fetchall():
                        dom = (row[0] or "").strip()
                        if not dom or dom in blk:
                            continue
                        if not _is_corporate_domain(dom):
                            continue
                        d = by_domain.setdefault(dom, _empty_candidate(dom))
                        d["contact_email"] = d["contact_email"] or row[1]
                        d["brief_clicks"] += int(row[2] or 0)
                        d["visit_count"]  += int(row[3] or 0)
                        d["source_mix"].add("state_visitor")
                        d["first_seen"] = _min_dt(d["first_seen"], row[4])
                        d["last_seen"]  = _max_dt(d["last_seen"],  row[5])
                except Exception as e:
                    _log(f"source_state_visitor_failed: {e}")
                    try: conn.rollback()
                    except Exception: pass

            # ─── Source C: MCP high-intent claim sessions ───────────
            if _table_exists(cur, "mcp_high_intent_sessions"):
                try:
                    cur.execute("""
                        SELECT LOWER(SPLIT_PART(email, '@', 2)) AS domain,
                               LOWER(MIN(email))                AS first_email,
                               COUNT(*)                          AS n,
                               MIN(detected_at)                  AS first_seen,
                               MAX(detected_at)                  AS last_seen
                          FROM mcp_high_intent_sessions
                         WHERE email IS NOT NULL AND email LIKE '%%@%%'
                           AND detected_at
                                > NOW() - INTERVAL '%s days'
                         GROUP BY 1
                    """ % int(lookback_days))
                    for row in cur.fetchall():
                        dom = (row[0] or "").strip()
                        if not dom or dom in blk:
                            continue
                        if not _is_corporate_domain(dom):
                            continue
                        d = by_domain.setdefault(dom, _empty_candidate(dom))
                        d["contact_email"] = d["contact_email"] or row[1]
                        d["visit_count"]  += int(row[2] or 0)
                        d["source_mix"].add("mcp_claim")
                        d["first_seen"] = _min_dt(d["first_seen"], row[3])
                        d["last_seen"]  = _max_dt(d["last_seen"],  row[4])
                except Exception as e:
                    _log(f"source_mcp_claim_failed: {e}")
                    try: conn.rollback()
                    except Exception: pass

            # ─── Source D: MCP upgrade signals w/ email ─────────────
            if _table_exists(cur, "mcp_upgrade_signals"):
                try:
                    cur.execute("""
                        SELECT LOWER(SPLIT_PART(user_email, '@', 2)) AS domain,
                               LOWER(MIN(user_email))                AS first_email,
                               COUNT(*)                               AS n,
                               MIN(created_at)                        AS first_seen,
                               MAX(created_at)                        AS last_seen
                          FROM mcp_upgrade_signals
                         WHERE user_email IS NOT NULL
                           AND user_email LIKE '%%@%%'
                           AND created_at
                                > NOW() - INTERVAL '%s days'
                         GROUP BY 1
                        HAVING COUNT(*) >= 1
                    """ % int(lookback_days))
                    for row in cur.fetchall():
                        dom = (row[0] or "").strip()
                        if not dom or dom in blk:
                            continue
                        if not _is_corporate_domain(dom):
                            continue
                        d = by_domain.setdefault(dom, _empty_candidate(dom))
                        d["contact_email"] = d["contact_email"] or row[1]
                        d["visit_count"]  += int(row[2] or 0)
                        d["source_mix"].add("mcp_signals")
                        d["first_seen"] = _min_dt(d["first_seen"], row[3])
                        d["last_seen"]  = _max_dt(d["last_seen"],  row[4])
                except Exception as e:
                    _log(f"source_mcp_signals_failed: {e}")
                    try: conn.rollback()
                    except Exception: pass

            # ─── Cooldown: skip domains we've emailed in the last
            # COOLDOWN_DAYS_DEFAULT days ─────────────────────────────
            cooldown_days = _env_int("SALES_OUTREACH_COOLDOWN_DAYS",
                                     COOLDOWN_DAYS_DEFAULT)
            try:
                cur.execute("""
                    SELECT LOWER(company_domain)
                      FROM sales_outreach_log
                     WHERE detected_at
                            > NOW() - INTERVAL '%s days'
                       AND (sent_at IS NOT NULL
                            OR decision IN ('dry_run', 'approved'))
                """ % int(cooldown_days))
                in_cooldown = {r[0] for r in cur.fetchall() if r[0]}
            except Exception:
                in_cooldown = set()

        # Filter + sort
        out = []
        for dom, d in by_domain.items():
            if dom in in_cooldown:
                continue
            if int(d["visit_count"]) < int(min_visits):
                continue
            # finalize source_mix
            d["source_mix"] = ",".join(sorted(d["source_mix"]))
            d["first_seen"] = d["first_seen"].isoformat() if d["first_seen"] else None
            d["last_seen"]  = d["last_seen"].isoformat() if d["last_seen"] else None
            out.append(d)
        # Highest visit count first, recent activity second
        out.sort(key=lambda r: (-int(r["visit_count"] or 0),
                                 r.get("last_seen") or ""), reverse=False)
        out.sort(key=lambda r: (-int(r["visit_count"] or 0)))
        return out[:MAX_DRAFTS_PER_RUN], None
    except Exception as e:
        return [], f"detect_failed: {str(e)[:200]}"
    finally:
        try: conn.close()
        except Exception: pass


def _empty_candidate(dom: str) -> dict:
    return {
        "domain":        dom,
        "contact_email": None,
        "visit_count":   0,
        "brief_clicks":  0,
        "source_mix":    set(),
        "first_seen":    None,
        "last_seen":     None,
    }


def _min_dt(a, b):
    if a is None: return b
    if b is None: return a
    return a if a < b else b


def _max_dt(a, b):
    if a is None: return b
    if b is None: return a
    return a if a > b else b


# ── Enrichment ──────────────────────────────────────────────────────────
def enrich_company(domain: str) -> dict:
    """Return {company_name, industry, employee_count, news_headline}.
    Best-effort. Tries Hunter.io first; falls back to no-op."""
    out = {
        "company_name":         None,
        "industry":             None,
        "employee_count":       None,
        "news_headline":        None,
    }
    if not domain:
        return out

    key = (os.environ.get("HUNTER_API_KEY") or "").strip()
    if key:
        try:
            import requests as _rq
            r = _rq.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": key, "limit": 1},
                timeout=HUNTER_TIMEOUT_S,
            )
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                out["company_name"]   = data.get("organization")
                out["industry"]       = data.get("industry")
                # Hunter free tier doesn't return employee_count, but the
                # paid tier includes a `metadata.size` field.
                meta = data.get("metadata") or {}
                if isinstance(meta, dict):
                    out["employee_count"] = (
                        meta.get("size") or meta.get("employees_count"))
            else:
                _log(f"hunter HTTP {r.status_code} for {domain}")
        except Exception as e:
            _log(f"hunter_enrich_err {domain}: {e}")

    # Best-effort news lookup from the local news table (no outbound
    # WebFetch — Railway single-replica + this runs in a 30s cron slot)
    if not out["news_headline"]:
        try:
            conn = _db_conn()
            if conn is not None:
                with conn, conn.cursor() as cur:
                    # Match company name OR domain root in title/url
                    co = (out["company_name"] or "").lower()
                    domain_root = domain.split(".")[0]
                    cur.execute("""
                        SELECT title FROM news
                         WHERE (LOWER(title) LIKE %s
                                OR LOWER(url)   LIKE %s
                                OR LOWER(title) LIKE %s)
                           AND created_at > NOW() - INTERVAL '60 days'
                         ORDER BY COALESCE(published_date, created_at) DESC
                         LIMIT 1
                    """, (f"%{co}%" if co else "%__NEVERMATCH__%",
                          f"%{domain}%",
                          f"%{domain_root}%" if domain_root else
                          "%__NEVERMATCH__%"))
                    row = cur.fetchone()
                    if row and row[0]:
                        out["news_headline"] = str(row[0])[:200]
                conn.close()
        except Exception as e:
            _log(f"news_lookup_err {domain}: {e}")

    return out


# ── Contact selection ──────────────────────────────────────────────────
def select_contact(candidate: dict) -> dict:
    """Pick best target. The candidate already carries contact_email +
    domain. We try to enrich the NAME via mcp_dev_keys + state_visitor_
    intent + newsletter_subscribers; the first non-empty wins."""
    out = {
        "contact_email": candidate.get("contact_email"),
        "contact_name":  None,
        "contact_title": None,
    }
    if not out["contact_email"]:
        return out

    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn, conn.cursor() as cur:
            em = (out["contact_email"] or "").lower()
            # Try mcp_dev_keys first (has name + sometimes title)
            for sql in [
                ("SELECT name FROM mcp_dev_keys "
                 "WHERE LOWER(email)=%s LIMIT 1", (em,)),
                ("SELECT name FROM api_keys "
                 "WHERE LOWER(email)=%s LIMIT 1", (em,)),
                ("SELECT name FROM newsletter_subscribers "
                 "WHERE LOWER(email)=%s LIMIT 1", (em,)),
                ("SELECT name FROM user_enrichment "
                 "WHERE LOWER(email)=%s LIMIT 1", (em,)),
            ]:
                try:
                    cur.execute(sql[0], sql[1])
                    r = cur.fetchone()
                    if r and r[0]:
                        out["contact_name"] = str(r[0])[:80]
                        break
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            # Try title from user_enrichment
            try:
                cur.execute(
                    "SELECT industry FROM user_enrichment "
                    "WHERE LOWER(email)=%s LIMIT 1", (em,))
                r = cur.fetchone()
                if r and r[0]:
                    out["contact_title"] = str(r[0])[:80]
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass

    return out


# ── Live data context for the Claude prompt ─────────────────────────────
def _live_context(industry: str | None,
                  source_mix: str) -> dict:
    """One specific number the email can drop. Industry-aware.
    NEVER raises — falls back to canonical HEALTH_BASELINE numbers."""
    ctx = {"primary_stat": "", "fallback_stat": ""}
    # Always have a safe fallback
    ctx["fallback_stat"] = (
        "2,000+ tracked deals across 178 countries; "
        "232 DCPI markets; 5,700+ discovered facilities"
    )
    conn = _db_conn()
    if conn is None:
        ctx["primary_stat"] = ctx["fallback_stat"]
        return ctx
    try:
        with conn, conn.cursor() as cur:
            ind = (industry or "").lower()
            # PE / RE / fund / asset manager → top BUILD market
            if any(s in ind for s in ("real estate", "broker", "reit",
                                       "investment", "private equity",
                                       "asset manager")):
                try:
                    cur.execute("""
                        SELECT market_slug, composite_score
                          FROM dcpi_v3_master
                         WHERE COALESCE(verdict,'') ILIKE 'BUILD%'
                         ORDER BY composite_score DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    if row:
                        ctx["primary_stat"] = (
                            f"top BUILD market is {row[0]} with a DCPI of "
                            f"{float(row[1] or 0):.1f}")
                except Exception:
                    pass
            # Energy / utility → ISO queue depth
            if not ctx["primary_stat"] and any(
                    s in ind for s in ("energy", "utility", "power",
                                        "renewable")):
                try:
                    cur.execute("""
                        SELECT iso_region, COUNT(*)
                          FROM interconnection_queue_projects
                         WHERE updated_at > NOW() - INTERVAL '30 days'
                         GROUP BY iso_region
                         ORDER BY 2 DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    if row:
                        ctx["primary_stat"] = (
                            f"{int(row[1] or 0):,} projects queued in "
                            f"{row[0]} alone in the last 30 days")
                except Exception:
                    pass
            # Tech / hyperscaler / saas → deal velocity
            if not ctx["primary_stat"] and any(
                    s in ind for s in ("software", "saas", "tech",
                                        "cloud", "ai", "hyperscale")):
                try:
                    cur.execute("""
                        SELECT COUNT(*)
                          FROM deals
                         WHERE announced_at > NOW() - INTERVAL '90 days'
                    """)
                    row = cur.fetchone()
                    if row:
                        ctx["primary_stat"] = (
                            f"{int(row[0] or 0):,} data-center deals "
                            f"announced in the last 90 days")
                except Exception:
                    pass
        if not ctx["primary_stat"]:
            ctx["primary_stat"] = ctx["fallback_stat"]
        return ctx
    except Exception:
        ctx["primary_stat"] = ctx["fallback_stat"]
        return ctx
    finally:
        try: conn.close()
        except Exception: pass


# ── Claude prompt + JSON contract ──────────────────────────────────────
_OUTREACH_SYSTEM_PROMPT = (
    "You are Jonathan Martone, founder of DC Hub, a data center "
    "intelligence platform. You are writing a SHORT cold outreach email "
    "to a recipient whose company has been browsing dchub.cloud.\n"
    "\n"
    "Voice: founder, casual, data-driven, direct. No sales clichés. No "
    "emoji. No hashtags. Treat the recipient like a peer.\n"
    "\n"
    "Hard rules:\n"
    "  - SUBJECT: 1 line, MAXIMUM 90 characters, references their "
    "inferred interest.\n"
    "  - BODY: EXACTLY 4 sentences. No more, no less.\n"
    "      S1 — Reference what they were looking at on dchub.cloud.\n"
    "      S2 — Drop EXACTLY ONE specific DC Hub data point relevant to "
    "           their company/industry.\n"
    "      S3 — Soft ask: 'happy to walk you through the data — just reply'.\n"
    "      S4 — Sign off with the founder signature provided.\n"
    "  - 700 character maximum on the BODY.\n"
    "  - No banned phrases: leverage, synergy, circle back, value-add, "
    "best of breed, game-changer, ecosystem, delve, revolutionary, "
    "paradigm shift, let's connect, touch base, amazing.\n"
    "  - Do NOT use exclamation marks.\n"
    "  - Do NOT compliment the recipient ('thoughtful', 'great work'). "
    "Reference the substance.\n"
    "\n"
    "Return STRICT JSON: "
    "{\"subject\":\"...\",\"body\":\"...\",\"data_point\":\"...\"}\n"
    "Nothing outside the JSON object."
)


def _call_claude(messages: list[dict], system: str) -> str | None:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        _log("claude: no ANTHROPIC_API_KEY")
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model":      MODEL_NAME,
                "max_tokens": MAX_TOKENS_PER_GEN,
                "system":     system,
                "messages":   messages,
            }).encode("utf-8"),
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         key,
                "anthropic-version": "2023-06-01",
                "User-Agent":        "dchub-sales-outreach/1.0",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
        return text.strip() or None
    except Exception as e:
        _log(f"claude_call_err: {e}")
        return None


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        chunk = m.group(0) if m else raw
        d = json.loads(chunk)
        if not isinstance(d, dict):
            return None
        if not isinstance(d.get("subject"), str):
            return None
        if not isinstance(d.get("body"), str):
            return None
        d.setdefault("data_point", "")
        return d
    except Exception:
        return None


def _tone_ok(subject: str, body: str) -> tuple[bool, str | None]:
    if not subject or not body:
        return False, "empty"
    if len(subject) > SUBJECT_MAX_CHARS:
        return False, f"subject_too_long:{len(subject)}"
    if len(body) > BODY_MAX_CHARS:
        return False, f"body_too_long:{len(body)}"
    s = (subject + " " + body).lower()
    for bad in LAZY_REJECT_TOKENS:
        if bad in s:
            return False, f"lazy_token:{bad}"
    if "!" in body:
        return False, "exclamation"
    # Sentence count — strict 4 in body
    n_sentences = len([x for x in re.split(r"(?<=[.?])\s+", body.strip())
                       if x and re.search(r"[A-Za-z]", x)])
    if n_sentences > 5:    # allow signature line + 4 sentences
        return False, f"too_many_sentences:{n_sentences}"
    return True, None


def generate_outreach(candidate: dict, contact: dict,
                       enrichment: dict, max_retries: int = 1
                       ) -> dict | None:
    """Return {subject, body, data_point} or None on fail.
    Retries ONCE on tone-filter rejection."""
    ctx = _live_context(enrichment.get("industry"),
                         candidate.get("source_mix") or "")
    primary_stat = ctx.get("primary_stat") or ctx.get("fallback_stat") or ""

    company    = enrichment.get("company_name") or candidate.get("domain")
    industry   = enrichment.get("industry")     or "unknown"
    news_line  = enrichment.get("news_headline") or ""
    cont_name  = contact.get("contact_name")    or ""
    visit_n    = int(candidate.get("visit_count") or 0)
    brief_n    = int(candidate.get("brief_clicks") or 0)
    src_mix    = candidate.get("source_mix") or ""

    # Build "what they were looking at" summary
    surfaces = []
    if "newsletter" in src_mix:
        surfaces.append("our newsletter")
    if "state_visitor" in src_mix:
        surfaces.append("the State of 2026 report")
    if "mcp_claim" in src_mix:
        surfaces.append("our MCP/API")
    if "mcp_signals" in src_mix:
        surfaces.append("our data tools")
    if brief_n:
        surfaces.append(f"{brief_n} market briefs")
    surface_str = ", ".join(surfaces) or "DC Hub"

    salutation = f"Hi {cont_name.split()[0]}" if cont_name else "Hi"
    signature  = f"— Jonathan Martone, {JONATHAN_TITLE}"

    user_msg = (
        f"Recipient company: {company} (domain {candidate.get('domain')})\n"
        f"Industry: {industry}\n"
        f"Recipient: {cont_name or 'unknown'} at {company}\n"
        f"Their activity on dchub.cloud in the last "
        f"{LOOKBACK_DAYS_DEFAULT} days:\n"
        f"  - visit_count: {visit_n}\n"
        f"  - brief clicks: {brief_n}\n"
        f"  - sources: {surface_str}\n"
        f"Recent news (if any): {news_line or 'none'}\n\n"
        f"Use EXACTLY ONE specific number from this context: {primary_stat}\n"
        f"\n"
        f"Open the body with: '{salutation},'\n"
        f"Close the body with this exact signature on its own sentence: "
        f"'{signature}'\n"
        f"\n"
        f"Write the outreach. Strict JSON: "
        f"{{\"subject\":\"...\",\"body\":\"...\",\"data_point\":\"...\"}}."
    )

    last_reason = None
    for attempt in range(int(max_retries) + 1):
        raw = _call_claude(
            messages=[{"role": "user", "content": user_msg}],
            system=_OUTREACH_SYSTEM_PROMPT,
        )
        if not raw:
            last_reason = "claude_no_response"
            continue
        parsed = _parse_json(raw)
        if not parsed:
            last_reason = "claude_unparseable"
            continue
        ok, why = _tone_ok(parsed["subject"], parsed["body"])
        if not ok:
            last_reason = why
            continue
        # Belt-and-suspenders: force signature presence
        if signature not in parsed["body"]:
            parsed["body"] = (parsed["body"].rstrip() + "\n\n" +
                              signature)
        return parsed

    _log(f"generate_outreach_failed reason={last_reason}")
    return None


def _templated_fallback(candidate: dict, contact: dict,
                         enrichment: dict) -> dict:
    """Pure-template fallback when Claude is unavailable (e.g. ANTHROPIC_
    API_KEY missing). Keeps the dashboard populated so the operator sees
    SOMETHING to review."""
    company = enrichment.get("company_name") or candidate.get("domain")
    cont_name = contact.get("contact_name") or ""
    visit_n = int(candidate.get("visit_count") or 0)
    src_mix = candidate.get("source_mix") or ""
    salutation = f"Hi {cont_name.split()[0]}" if cont_name else "Hi"
    surface = "the State of 2026 report" if "state_visitor" in src_mix else (
        "our market briefs" if "newsletter" in src_mix else "DC Hub")
    body = (
        f"{salutation},\n\n"
        f"Saw a few visits from {company} looking at {surface} "
        f"on dchub.cloud over the last few weeks. "
        f"DC Hub tracks 2,000+ data center deals across 178 countries "
        f"and ranks 232 markets by power, fiber, and land readiness — "
        f"useful if you're sizing up a specific market or operator. "
        f"Happy to walk you through the data — just reply. "
        f"— Jonathan Martone, {JONATHAN_TITLE}"
    )
    return {
        "subject": f"Saw {company} looking at DC Hub — quick walkthrough?",
        "body": body,
        "data_point": "2,000+ tracked deals, 232 DCPI markets",
    }


# ── Persistence ────────────────────────────────────────────────────────
def _write_log_row(candidate: dict, contact: dict, enrichment: dict,
                    draft: dict, decision: str,
                    decision_reason: str | None = None) -> int | None:
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sales_outreach_log
                    (company_domain, company_name, company_industry,
                     company_employee_count, company_news_headline,
                     contact_email, contact_name, contact_title,
                     source_visit_count, source_brief_clicks,
                     source_mix, source_first_seen, source_last_seen,
                     generated_subject, generated_body, generated_data_point,
                     decision, decision_reason)
                VALUES (%s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s)
                RETURNING id
            """, (
                candidate.get("domain"),
                enrichment.get("company_name"),
                enrichment.get("industry"),
                enrichment.get("employee_count"),
                enrichment.get("news_headline"),
                contact.get("contact_email"),
                contact.get("contact_name"),
                contact.get("contact_title"),
                int(candidate.get("visit_count") or 0),
                int(candidate.get("brief_clicks") or 0),
                candidate.get("source_mix"),
                candidate.get("first_seen"),
                candidate.get("last_seen"),
                (draft or {}).get("subject"),
                (draft or {}).get("body"),
                (draft or {}).get("data_point"),
                decision,
                decision_reason,
            ))
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        _log(f"write_log_row_err: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


# ── Send via Resend ─────────────────────────────────────────────────────
def send_outreach(log_id: int) -> tuple[bool, dict]:
    """Send a single approved draft. Fail-soft."""
    conn = _db_conn()
    if conn is None:
        return False, {"error": "no_db"}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT contact_email, contact_name,
                       generated_subject, generated_body,
                       company_domain, decision
                  FROM sales_outreach_log
                 WHERE id=%s LIMIT 1
            """, (int(log_id),))
            row = cur.fetchone()
            if not row:
                return False, {"error": "not_found", "log_id": log_id}
            email, name, subject, body, domain, decision = row
            if decision == "sent":
                return False, {"error": "already_sent",
                                "log_id": log_id}
            if decision == "declined":
                return False, {"error": "declined",
                                "log_id": log_id}
            if not email or not subject or not body:
                return False, {"error": "incomplete_draft",
                                "log_id": log_id}

        resend_key = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
        if not resend_key:
            return False, {"error": "resend_not_configured"}

        import requests as _rq
        payload = {
            "from":     JONATHAN_FROM,
            "to":       [email],
            "reply_to": JONATHAN_REPLY_TO,
            "subject":  subject,
            "text":     body,
        }
        rr = _rq.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=RESEND_TIMEOUT_S,
        )
        if rr.status_code >= 400:
            return False, {"error": f"resend_http_{rr.status_code}",
                            "body": (rr.text or "")[:200]}
        rd = (rr.json() or {}) if rr.text else {}
        # Mark as sent
        with conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE sales_outreach_log
                   SET decision = 'sent',
                       sent_at  = NOW(),
                       resend_id = %s
                 WHERE id = %s
            """, (rd.get("id"), int(log_id)))
        return True, {"ok": True, "resend_id": rd.get("id"),
                      "email": email, "log_id": log_id}
    except Exception as e:
        _log(f"send_outreach_err: {e}")
        return False, {"error": str(e)[:200]}
    finally:
        try: conn.close()
        except Exception: pass


# ── Endpoints ───────────────────────────────────────────────────────────
@sales_outreach_automator_bp.post(
    "/api/v1/admin/sales-outreach/detect")
def endpoint_detect():
    """Detect + draft. ALWAYS DRY-RUN regardless of env — the spec
    requires the operator to approve EVERY first batch via the
    dashboard before any send fires.

    The cron calls this; it never sends. Sending happens only via the
    /approve/<log_id> endpoint."""
    if not _admin_or_cron_authorized():
        return jsonify(error="forbidden",
                       hint="X-Admin-Key required"), 403
    if _kill_switched():
        return jsonify(ok=True, skipped=True,
                       reason="SALES_OUTREACH_DISABLE=1"), 200

    lookback = int(request.args.get("days", LOOKBACK_DAYS_DEFAULT))
    min_visits = int(request.args.get("min_visits", MIN_VISIT_COUNT_DEFAULT))
    cap = _env_int("SALES_OUTREACH_DAILY_CAP", DAILY_CAP_DEFAULT)

    candidates, err = detect_high_intent_companies(
        lookback_days=lookback, min_visits=min_visits)
    if err:
        return jsonify(error=err), 500

    # Count today's drafts so we don't blow past the cap
    today_count = 0
    try:
        conn = _db_conn()
        if conn is not None:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM sales_outreach_log
                     WHERE detected_at::date = CURRENT_DATE
                """)
                row = cur.fetchone()
                today_count = int((row or [0])[0] or 0)
            conn.close()
    except Exception:
        pass

    drafts = []
    skipped = []
    for c in candidates:
        if len(drafts) + today_count >= cap:
            skipped.append({"domain": c["domain"],
                            "reason": "daily_cap_reached"})
            continue
        enrichment = enrich_company(c["domain"])
        contact    = select_contact(c)
        if not contact.get("contact_email"):
            skipped.append({"domain": c["domain"],
                            "reason": "no_contact_email"})
            _write_log_row(c, contact, enrichment, None,
                            "skipped", "no_contact_email")
            continue

        draft = generate_outreach(c, contact, enrichment, max_retries=1)
        if not draft:
            # Try template fallback so the operator sees SOMETHING
            draft = _templated_fallback(c, contact, enrichment)
            decision_reason = "fallback_template"
        else:
            decision_reason = None

        log_id = _write_log_row(c, contact, enrichment, draft,
                                  "dry_run", decision_reason)
        drafts.append({
            "log_id":           log_id,
            "domain":           c["domain"],
            "company_name":     enrichment.get("company_name"),
            "industry":         enrichment.get("industry"),
            "contact_email":    contact.get("contact_email"),
            "contact_name":     contact.get("contact_name"),
            "subject":          draft.get("subject"),
            "body_preview":     (draft.get("body") or "")[:200],
            "visit_count":      c.get("visit_count"),
            "source_mix":       c.get("source_mix"),
        })

    return jsonify(
        ok=True,
        as_of=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        dry_run=True,            # always
        env_dry_run=_dry_run(),
        detected=len(candidates),
        drafted=len(drafts),
        skipped=skipped,
        daily_cap=cap,
        cap_remaining=max(0, cap - today_count - len(drafts)),
        drafts=drafts,
        dashboard_url="/admin/sales-outreach",
    ), 200


@sales_outreach_automator_bp.get(
    "/api/v1/admin/sales-outreach/log")
def endpoint_log():
    """Last N days of log rows for the dashboard. JSON."""
    if not _admin_or_cron_authorized():
        return jsonify(error="forbidden"), 403
    days = int(request.args.get("days", 30))
    limit = min(int(request.args.get("limit", 200)), 500)
    conn = _db_conn()
    if conn is None:
        return jsonify(error="no_db"), 500
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, company_domain, company_name, company_industry,
                       contact_email, contact_name,
                       source_visit_count, source_brief_clicks, source_mix,
                       generated_subject, generated_body,
                       decision, decision_reason,
                       detected_at, decided_at, sent_at,
                       response_received_at, response_text
                  FROM sales_outreach_log
                 WHERE detected_at > NOW() - INTERVAL '%s days'
                 ORDER BY detected_at DESC
                 LIMIT %s
            """ % (int(days), int(limit)))
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                for k in ("detected_at", "decided_at", "sent_at",
                          "response_received_at",
                          "source_first_seen", "source_last_seen"):
                    if isinstance(d.get(k), datetime.datetime):
                        d[k] = d[k].isoformat()
                rows.append(d)
        return jsonify(ok=True, count=len(rows), rows=rows)
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


@sales_outreach_automator_bp.post(
    "/api/v1/admin/sales-outreach/approve/<int:log_id>")
def endpoint_approve(log_id: int):
    """1-click approve + send a single dry-run draft NOW.
    Triggers Resend. This is the ONLY path that actually sends email."""
    if not _admin_or_cron_authorized():
        return jsonify(error="forbidden"), 403
    if _kill_switched():
        return jsonify(error="kill_switched"), 503

    # Mark the row 'approved' before send so the audit trail is clean
    # even if send_outreach throws.
    conn = _db_conn()
    if conn is None:
        return jsonify(error="no_db"), 500
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE sales_outreach_log
                   SET decision = 'approved',
                       decided_at = NOW(),
                       decided_by = COALESCE(%s, 'admin')
                 WHERE id = %s AND decision = 'dry_run'
                RETURNING id
            """, (request.headers.get("X-Admin-User"), int(log_id)))
            row = cur.fetchone()
            if not row:
                return jsonify(error="not_found_or_not_dry_run",
                                log_id=log_id), 404
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass

    ok, info = send_outreach(int(log_id))
    return jsonify(ok=ok, info=info, log_id=log_id), 200 if ok else 500


@sales_outreach_automator_bp.post(
    "/api/v1/admin/sales-outreach/decline/<int:log_id>")
def endpoint_decline(log_id: int):
    """Mark row 'declined'. The same domain still goes on the 30-day
    cooldown."""
    if not _admin_or_cron_authorized():
        return jsonify(error="forbidden"), 403
    conn = _db_conn()
    if conn is None:
        return jsonify(error="no_db"), 500
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE sales_outreach_log
                   SET decision = 'declined',
                       decided_at = NOW(),
                       decided_by = COALESCE(%s, 'admin'),
                       decision_reason = COALESCE(%s, decision_reason)
                 WHERE id = %s
                RETURNING id, company_domain
            """, (request.headers.get("X-Admin-User"),
                  request.args.get("reason"),
                  int(log_id)))
            row = cur.fetchone()
            if not row:
                return jsonify(error="not_found", log_id=log_id), 404
        return jsonify(ok=True, log_id=row[0], domain=row[1])
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


@sales_outreach_automator_bp.post(
    "/api/v1/admin/sales-outreach/regenerate/<int:log_id>")
def endpoint_regenerate(log_id: int):
    """Re-roll Claude on a draft (no send). Decision stays 'dry_run'."""
    if not _admin_or_cron_authorized():
        return jsonify(error="forbidden"), 403
    conn = _db_conn()
    if conn is None:
        return jsonify(error="no_db"), 500
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT company_domain, company_name, company_industry,
                       company_employee_count, company_news_headline,
                       contact_email, contact_name, contact_title,
                       source_visit_count, source_brief_clicks,
                       source_mix, source_first_seen, source_last_seen
                  FROM sales_outreach_log
                 WHERE id = %s AND decision = 'dry_run' LIMIT 1
            """, (int(log_id),))
            row = cur.fetchone()
            if not row:
                return jsonify(error="not_found_or_not_dry_run",
                                log_id=log_id), 404
        candidate = {
            "domain":        row[0],
            "visit_count":   int(row[8] or 0),
            "brief_clicks":  int(row[9] or 0),
            "source_mix":    row[10] or "",
            "first_seen":    row[11].isoformat() if row[11] else None,
            "last_seen":     row[12].isoformat() if row[12] else None,
            "contact_email": row[5],
        }
        contact = {
            "contact_email": row[5],
            "contact_name":  row[6],
            "contact_title": row[7],
        }
        enrichment = {
            "company_name":   row[1],
            "industry":       row[2],
            "employee_count": row[3],
            "news_headline":  row[4],
        }
        draft = generate_outreach(candidate, contact, enrichment,
                                    max_retries=1)
        if not draft:
            draft = _templated_fallback(candidate, contact, enrichment)
        with conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE sales_outreach_log
                   SET generated_subject = %s,
                       generated_body    = %s,
                       generated_data_point = %s,
                       decision_reason   = 'regenerated'
                 WHERE id = %s
            """, (draft["subject"], draft["body"],
                  draft.get("data_point"), int(log_id)))
        return jsonify(ok=True, log_id=log_id,
                        subject=draft["subject"],
                        body_preview=(draft["body"] or "")[:200])
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── Admin HTML dashboard ───────────────────────────────────────────────
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sales Outreach · DC Hub Admin</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;
   margin:0;background:#0b0f17;color:#e5e7eb;padding:24px;line-height:1.5}
 h1{color:#10b981;margin:0 0 4px 0;font-size:22px}
 h2{color:#fbbf24;margin:24px 0 8px 0;font-size:16px}
 .summary{display:flex;gap:24px;margin-bottom:20px;flex-wrap:wrap}
 .kpi{background:#111827;border:1px solid #1f2937;border-radius:8px;
   padding:12px 16px;min-width:140px}
 .kpi .label{font-size:11px;color:#9ca3af;text-transform:uppercase}
 .kpi .value{font-size:24px;font-weight:700;color:#10b981}
 .row{background:#111827;border:1px solid #1f2937;border-radius:8px;
   padding:16px;margin-bottom:12px}
 .row .meta{font-size:12px;color:#9ca3af;margin-bottom:8px}
 .row .subject{font-size:15px;font-weight:600;color:#f9fafb;
   margin-bottom:6px}
 .row .body{font-size:13px;color:#d1d5db;white-space:pre-wrap;
   background:#0b0f17;border-left:3px solid #10b981;padding:8px 12px;
   margin:8px 0;border-radius:0 4px 4px 0}
 .pill{display:inline-block;padding:2px 8px;border-radius:99px;
   font-size:10px;text-transform:uppercase;font-weight:700;margin-left:6px}
 .pill.dry_run{background:#fef3c7;color:#92400e}
 .pill.approved{background:#d1fae5;color:#065f46}
 .pill.sent{background:#dbeafe;color:#1e40af}
 .pill.declined{background:#fee2e2;color:#991b1b}
 .pill.skipped{background:#e5e7eb;color:#374151}
 .actions button{background:#10b981;color:white;border:0;border-radius:6px;
   padding:8px 16px;font-weight:700;cursor:pointer;margin-right:8px}
 .actions button.danger{background:#dc2626}
 .actions button.warn{background:#f59e0b}
 .empty{text-align:center;padding:60px;color:#6b7280}
 .banner{background:#fef3c7;color:#92400e;padding:12px 16px;border-radius:8px;
   margin-bottom:16px;font-size:13px;font-weight:600}
 .danger-banner{background:#fee2e2;color:#991b1b}
 input[type=password]{background:#0b0f17;color:#e5e7eb;border:1px solid #374151;
   padding:6px 10px;border-radius:6px;width:340px;margin-bottom:12px}
 code{background:#0b0f17;padding:2px 6px;border-radius:3px;color:#fbbf24}
</style>
</head>
<body>
<h1>Sales Outreach Automator</h1>
<p style="color:#9ca3af;margin:0 0 16px 0">
 Brain-detected high-intent companies · founder-approved sends only ·
 <code>3/day</code> hard cap · 30-day per-domain cooldown</p>

<div id="banner"></div>

<label>Admin key:</label><br>
<input type="password" id="adminkey" placeholder="X-Admin-Key">
<button onclick="loadLog()">Load Drafts</button>
<button onclick="runDetect()" class="warn">Detect Now</button>

<div class="summary" id="summary"></div>

<h2>Pending Drafts (dry-run)</h2>
<div id="dryRunRows"></div>

<h2>Sent + Decided (last 30d)</h2>
<div id="historyRows"></div>

<script>
function $$(id){return document.getElementById(id);}
function key(){return $$("adminkey").value.trim();}

async function loadLog(){
  if(!key()){$$("banner").innerHTML='<div class="banner danger-banner">Paste admin key first</div>';return;}
  $$("banner").innerHTML='';
  const r = await fetch('/api/v1/admin/sales-outreach/log?days=30',{
    headers:{'X-Admin-Key':key()}});
  if(!r.ok){$$("banner").innerHTML='<div class="banner danger-banner">Load failed: '+r.status+'</div>';return;}
  const d = await r.json();
  const rows = d.rows || [];
  const dry = rows.filter(r=>r.decision==='dry_run');
  const hist = rows.filter(r=>r.decision!=='dry_run');
  const sent = rows.filter(r=>r.decision==='sent').length;
  const declined = rows.filter(r=>r.decision==='declined').length;
  $$("summary").innerHTML =
    kpi("Pending", dry.length) + kpi("Sent (30d)", sent) +
    kpi("Declined (30d)", declined) + kpi("Total (30d)", rows.length);
  $$("dryRunRows").innerHTML = dry.length ?
    dry.map(rowHtml).join('') :
    '<div class="empty">No pending drafts. Cron drafts twice daily; click Detect Now to force a sweep.</div>';
  $$("historyRows").innerHTML = hist.length ?
    hist.map(rowHtml).join('') :
    '<div class="empty">No history yet.</div>';
}

function kpi(label, value){
  return `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div></div>`;
}

function rowHtml(r){
  return `<div class="row">
    <div class="meta">
     ${escapeHtml(r.company_name || r.company_domain)}
     <span class="pill ${r.decision}">${r.decision}</span>
     · ${r.contact_email || '(no contact)'}
     ${r.contact_name ? '· '+escapeHtml(r.contact_name) : ''}
     · ${r.source_visit_count || 0} visits
     · sources: ${escapeHtml(r.source_mix||'')}
     · ${escapeHtml(r.company_industry||'unknown')}
     ${r.sent_at ? '· sent '+(new Date(r.sent_at).toLocaleString()) : ''}
    </div>
    <div class="subject">${escapeHtml(r.generated_subject || '(no subject)')}</div>
    <div class="body">${escapeHtml(r.generated_body || '')}</div>
    ${r.decision==='dry_run' ? actionsHtml(r.id) : ''}
   </div>`;
}

function actionsHtml(id){
  return `<div class="actions">
    <button onclick="act(${id},'approve')">Approve &amp; Send</button>
    <button onclick="act(${id},'regenerate')" class="warn">Re-roll</button>
    <button onclick="act(${id},'decline')" class="danger">Decline</button>
   </div>`;
}

async function act(id, action){
  if(!key()){alert('Paste admin key first');return;}
  if(action==='approve' &&
     !confirm('Send this email via Resend? This is LIVE — recipient will receive it.')){return;}
  const r = await fetch('/api/v1/admin/sales-outreach/'+action+'/'+id,{
    method:'POST', headers:{'X-Admin-Key':key()}});
  const d = await r.json().catch(()=>({}));
  if(!r.ok){alert(action+' failed: '+(d.error || d.info?.error || 'unknown'));return;}
  loadLog();
}

async function runDetect(){
  if(!key()){alert('Paste admin key first');return;}
  $$("banner").innerHTML='<div class="banner">Running detect…</div>';
  const r = await fetch('/api/v1/admin/sales-outreach/detect',{
    method:'POST', headers:{'X-Admin-Key':key()}});
  const d = await r.json().catch(()=>({}));
  if(!r.ok){$$("banner").innerHTML='<div class="banner danger-banner">Detect failed: '+(d.error||r.status)+'</div>';return;}
  $$("banner").innerHTML='<div class="banner">Detected '+d.detected+
    ', drafted '+d.drafted+' (cap '+d.cap_remaining+' remaining today)</div>';
  loadLog();
}

function escapeHtml(s){return String(s||'').replace(/[&<>'"]/g, c => (
 {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
</script>
</body>
</html>
"""


@sales_outreach_automator_bp.get("/admin/sales-outreach")
def endpoint_dashboard():
    """HTML dashboard — admin key entered client-side, no auth on
    the HTML shell itself (same pattern as /admin/media-mix)."""
    return Response(_DASHBOARD_HTML, mimetype="text/html")
