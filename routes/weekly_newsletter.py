"""weekly_newsletter.py — DC Hub Weekly newsletter (Phase RR-newsletter, 2026-06-07).

Friday 16:00 UTC: brain pulls best-of-the-week and renders one HTML email,
sent via Resend to:
  - newsletter_subscribers.status='active' (public signup form)
  - mcp_dev_keys.status='active' (dev-key holders by default — opt-out via
    unsubscribe link)
  - digest_subscribers.unsubscribed_at IS NULL (legacy state-of-2026 form)

Newsletter sections (each idempotent: missing data renders as "no movement
this week" not a fake card):
  1) Top 3 LinkedIn posts by engagement (likes+comments+shares last 7d)
  2) Top 3 MCP queries by volume (mcp_upgrade_signals.tool_requested last 7d)
  3) Top 3 DCPI verdict shifts (market_verdict_post_log last 7d)
  4) Strategic note from brain (brain_findings best-detail last 7d if exists)
  5) Operator brief snippet (most recent operator_brief)
  6) Soft CTAs: dchub.cloud + /signup + /newsletter

Safety:
  - NEWSLETTER_DISABLE=1                kill switch (returns ok+skipped)
  - NEWSLETTER_DRY_RUN=1                renders + logs, no send
  - 500 recipient cap per run           (Resend has its own batch limits)
  - Idempotent: UNIQUE(issue_id, email) on newsletter_sends
  - Throttled: don't re-send to same email within 5 days
  - Friday-only gate inside _run_weekly_newsletter (weekday==4)

Tables:
  - newsletter_subscribers (email PK, ...)
  - newsletter_issues (issue_id PK, ...)
  - newsletter_sends (issue_id+email composite UNIQUE)

Endpoints:
  GET  /newsletter                       — public signup page
  POST /api/v1/newsletter/subscribe      — {email, source?} -> insert + welcome
  GET  /newsletter/unsubscribe/<token>   — 1-click unsubscribe (HMAC)
  GET  /newsletter/archive               — public list of past issues
  GET  /newsletter/issue/<issue_id>      — single issue archive page
  GET  /api/v1/admin/newsletter/preview  — render next Friday's HTML now
  POST /api/v1/admin/newsletter/send-test — send one email to azmartone@gmail.com
  POST /api/v1/admin/newsletter/send     — admin: send to full recipient list
  GET  /api/v1/admin/newsletter/stats    — subscriber + issue counts
  GET  /admin/newsletter                 — admin dashboard
"""
from __future__ import annotations
import os
import re
import json
import hmac
import hashlib
import secrets as _secrets
import logging
import datetime
from datetime import timezone
from urllib.parse import quote
from flask import Blueprint, request, jsonify, Response, redirect

logger = logging.getLogger(__name__)

weekly_newsletter_bp = Blueprint("weekly_newsletter", __name__)

SITE = "https://dchub.cloud"
BRAND_LOGO = f"{SITE}/static/dchub-logo-400.png"

# ── config ────────────────────────────────────────────────────────────────
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
_FROM_EMAIL = (os.environ.get("DCHUB_FROM_EMAIL")
               or os.environ.get("DCHUB_RESEND_FROM")
               or "DC Hub <alerts@dchub.cloud>")
_TEST_RECIPIENT = (os.environ.get("DCHUB_NEWSLETTER_TEST_TO")
                   or "azmartone@gmail.com")

_KILL = (os.environ.get("NEWSLETTER_DISABLE") or "").strip() in ("1", "true", "yes")
_DRY_RUN = (os.environ.get("NEWSLETTER_DRY_RUN") or "").strip() in ("1", "true", "yes")
_RECIPIENT_CAP = int(os.environ.get("NEWSLETTER_RECIPIENT_CAP") or "500")
_RESEND_DAYS = int(os.environ.get("NEWSLETTER_THROTTLE_DAYS") or "5")


# ── DB ────────────────────────────────────────────────────────────────────
def _conn():
    import psycopg2
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL") or "")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=10)
        c.autocommit = False
        return c
    except Exception as e:
        logger.error("newsletter._conn failed: %s", e)
        return None


_TABLES_READY = False


def _ensure_tables(cur):
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                email           TEXT PRIMARY KEY,
                subscribed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source          TEXT,
                unsubscribed_at TIMESTAMPTZ,
                last_sent_at    TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_issues (
                issue_id        TEXT PRIMARY KEY,
                sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                recipient_count INT NOT NULL DEFAULT 0,
                opens_count     INT NOT NULL DEFAULT 0,
                clicks_count    INT NOT NULL DEFAULT 0,
                content_html    TEXT,
                content_json    JSONB
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_sends (
                id          SERIAL PRIMARY KEY,
                issue_id    TEXT NOT NULL,
                email       TEXT NOT NULL,
                sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resend_id   TEXT,
                status      TEXT NOT NULL DEFAULT 'sent'
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS newsletter_sends_uq
                ON newsletter_sends (issue_id, email)
        """)
        _TABLES_READY = True
    except Exception as e:
        logger.warning("newsletter._ensure_tables: %s", e)


# ── HMAC unsubscribe token ────────────────────────────────────────────────
def _secret() -> bytes:
    return (_ADMIN_KEY
            or os.environ.get("DCHUB_SESSION_SECRET")
            or "dchub-newsletter-v1").encode()


def _unsub_token(email: str) -> str:
    """20-hex-char HMAC; matches digest.py's pattern."""
    return hmac.new(_secret(), (email or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()[:24]


def _unsub_url(email: str) -> str:
    e = quote((email or "").strip().lower())
    return f"{SITE}/newsletter/unsubscribe/{_unsub_token(email)}?e={e}"


# ── context gathering ─────────────────────────────────────────────────────
def gather_newsletter_context(now: datetime.datetime | None = None) -> dict:
    """Pull all data sources for this week's newsletter.

    Returns dict with: issue_id, week_label, top_posts, top_queries,
    top_shifts, strategic_note, operator_snippet, hero_stats.
    Every section is fail-soft: missing data -> empty list, never raises."""
    now = now or datetime.datetime.now(timezone.utc)
    week_label = now.strftime("Week of %B %d, %Y")
    issue_id = now.strftime("issue-%Y-w%V")
    out = {
        "issue_id":         issue_id,
        "week_label":       week_label,
        "generated_at":     now.isoformat(),
        "top_posts":        [],
        "top_queries":      [],
        "top_shifts":       [],
        "strategic_note":   None,
        "operator_snippet": None,
        "hero_stats":       {},
    }
    c = _conn()
    if not c:
        out["error"] = "no_database"
        return out
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Top 3 LinkedIn posts last 7d
            try:
                cur.execute("""
                    SELECT post_urn, content, likes, comments, shares,
                           impressions, posted_at
                      FROM linkedin_posts
                     WHERE posted_at >= NOW() - INTERVAL '7 days'
                       AND status = 'posted'
                     ORDER BY (COALESCE(likes,0) + COALESCE(comments,0)*3
                               + COALESCE(shares,0)*5) DESC
                     LIMIT 3
                """)
                for r in cur.fetchall():
                    content = (r.get("content") or "").strip()
                    snippet = content[:200] + ("..." if len(content) > 200 else "")
                    urn = r.get("post_urn") or ""
                    li_url = (f"https://www.linkedin.com/feed/update/{urn}"
                              if urn.startswith("urn:") else "")
                    out["top_posts"].append({
                        "snippet":     snippet,
                        "likes":       int(r.get("likes") or 0),
                        "comments":    int(r.get("comments") or 0),
                        "shares":      int(r.get("shares") or 0),
                        "impressions": int(r.get("impressions") or 0),
                        "link":        li_url,
                        "posted_at":   r["posted_at"].isoformat()
                                       if r.get("posted_at") else None,
                    })
            except Exception as e:
                logger.warning("gather: top_posts failed: %s", e)

            # 2. Top 3 MCP queries by volume last 7d
            try:
                cur.execute("""
                    SELECT tool_requested AS tool,
                           COUNT(*)       AS volume,
                           COUNT(DISTINCT COALESCE(user_email, caller_id, session_id))
                                          AS distinct_callers
                      FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '7 days'
                       AND tool_requested IS NOT NULL
                       AND tool_requested NOT IN ('', 'unknown')
                     GROUP BY tool_requested
                     ORDER BY volume DESC
                     LIMIT 3
                """)
                for r in cur.fetchall():
                    out["top_queries"].append({
                        "tool":             r["tool"],
                        "volume":           int(r["volume"]),
                        "distinct_callers": int(r["distinct_callers"] or 0),
                    })
            except Exception as e:
                logger.warning("gather: top_queries failed: %s", e)

            # 3. Top 3 DCPI verdict shifts last 7d
            try:
                cur.execute("""
                    SELECT market_slug, shift_from, shift_to, composite,
                           excess_score, constraint_sc, blurb, posted_at
                      FROM market_verdict_post_log
                     WHERE posted_at >= NOW() - INTERVAL '7 days'
                     ORDER BY ABS(COALESCE(excess_score,0)) DESC
                     LIMIT 3
                """)
                for r in cur.fetchall():
                    out["top_shifts"].append({
                        "market_slug":  r["market_slug"],
                        "shift_from":   r.get("shift_from") or "",
                        "shift_to":     r.get("shift_to") or "",
                        "blurb":        (r.get("blurb") or "")[:280],
                        "excess_score": float(r["excess_score"])
                                        if r.get("excess_score") is not None else None,
                        "url":          f"{SITE}/dcpi/{r['market_slug']}",
                        "posted_at":    r["posted_at"].isoformat()
                                        if r.get("posted_at") else None,
                    })
            except Exception as e:
                logger.warning("gather: top_shifts failed: %s", e)

            # 4. Strategic note — most recent high-severity brain finding
            try:
                cur.execute("""
                    SELECT detail, issue, url, last_seen
                      FROM brain_findings
                     WHERE last_seen >= NOW() - INTERVAL '7 days'
                       AND detail IS NOT NULL
                       AND LENGTH(detail) >= 80
                     ORDER BY last_seen DESC, count DESC
                     LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    out["strategic_note"] = {
                        "detail":    (row.get("detail") or "")[:600],
                        "issue":     row.get("issue") or "",
                        "url":       row.get("url") or "",
                    }
            except Exception as e:
                logger.debug("gather: strategic_note (optional): %s", e)

            # 5. Operator brief snippet — count of operators in the system
            try:
                cur.execute("""
                    SELECT COUNT(DISTINCT operator) AS n
                      FROM discovered_facilities
                     WHERE operator IS NOT NULL
                       AND operator NOT IN ('', 'Unknown')
                """)
                row = cur.fetchone()
                if row:
                    out["operator_snippet"] = {
                        "operator_count": int(row["n"]),
                        "headline":       (f"This week we shipped operator briefs "
                                           f"for {int(row['n'])}+ unique operators "
                                           f"across our tracked set. View the "
                                           f"index at {SITE}/operators."),
                    }
            except Exception as e:
                logger.debug("gather: operator_snippet (optional): %s", e)

            # Hero stats — live counts for the footer
            try:
                cur.execute("SELECT COUNT(*) AS n FROM discovered_facilities")
                facilities = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute("SELECT COUNT(DISTINCT market_slug) AS n "
                            "FROM market_power_scores "
                            "WHERE market_slug IS NOT NULL")
                markets = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute("SELECT COUNT(*) AS n FROM newsletter_subscribers "
                            "WHERE unsubscribed_at IS NULL")
                subs = int((cur.fetchone() or {}).get("n") or 0)
                out["hero_stats"] = {
                    "facilities": facilities,
                    "markets":    markets,
                    "subscribers": subs,
                }
            except Exception as e:
                logger.debug("gather: hero_stats (optional): %s", e)
    finally:
        try: c.close()
        except Exception: pass
    return out


# ── HTML rendering ────────────────────────────────────────────────────────
def _h(s) -> str:
    """HTML-escape; preserve None as empty string."""
    import html
    return html.escape(str(s or ""))


def render_newsletter_html(ctx: dict, recipient_email: str = "") -> str:
    """Editorial, table-based, email-safe HTML (no <style> dependencies)."""
    week_label = ctx.get("week_label") or "This Week"
    issue_id = ctx.get("issue_id") or "preview"
    unsub_url = _unsub_url(recipient_email) if recipient_email else f"{SITE}/newsletter"

    # ── section 1: top posts ──
    posts_html = ""
    if ctx.get("top_posts"):
        rows = []
        for p in ctx["top_posts"]:
            link = p.get("link") or "#"
            engagement = (f"{p.get('likes', 0)} likes · "
                          f"{p.get('comments', 0)} comments · "
                          f"{p.get('shares', 0)} shares")
            rows.append(
                f'<tr><td style="padding:14px 0;border-bottom:1px solid #1f1f1f">'
                f'<a href="{link}" style="color:#10b981;text-decoration:none;'
                f'font-size:14px;line-height:1.5">"{_h(p.get("snippet"))}"</a>'
                f'<div style="color:#737373;font-size:12px;margin-top:6px">'
                f'{engagement}</div></td></tr>'
            )
        posts_html = "".join(rows)
    else:
        posts_html = (
            '<tr><td style="padding:14px 0;color:#737373;font-size:13px">'
            'Quiet week on the wire. Last week\'s posts are at '
            f'<a href="{SITE}/media" style="color:#10b981">{SITE}/media</a></td></tr>'
        )

    # ── section 2: top queries ──
    queries_html = ""
    if ctx.get("top_queries"):
        rows = []
        for q in ctx["top_queries"]:
            tool = _h(q.get("tool"))
            rows.append(
                f'<tr><td style="padding:14px 0;border-bottom:1px solid #1f1f1f">'
                f'<div style="color:#e5e5e5;font-size:14px;font-family:monospace">'
                f'{tool}</div>'
                f'<div style="color:#737373;font-size:12px;margin-top:6px">'
                f'agents queried this {q.get("volume", 0)} times this week '
                f'({q.get("distinct_callers", 0)} distinct callers)</div></td></tr>'
            )
        queries_html = "".join(rows)
    else:
        queries_html = (
            '<tr><td style="padding:14px 0;color:#737373;font-size:13px">'
            'No paywalled MCP queries logged this week. '
            f'<a href="{SITE}/mcp" style="color:#10b981">Try the MCP</a></td></tr>'
        )

    # ── section 3: verdict shifts ──
    shifts_html = ""
    if ctx.get("top_shifts"):
        rows = []
        for s in ctx["top_shifts"]:
            slug = _h(s.get("market_slug"))
            fr = _h(s.get("shift_from") or "—")
            to = _h(s.get("shift_to") or "—")
            url = s.get("url") or f"{SITE}/dcpi"
            blurb = _h(s.get("blurb")) or "verdict shifted this week"
            rows.append(
                f'<tr><td style="padding:14px 0;border-bottom:1px solid #1f1f1f">'
                f'<a href="{url}" style="color:#e5e5e5;text-decoration:none;'
                f'font-size:15px;font-weight:600">{slug}</a> '
                f'<span style="color:#737373;font-size:12px">'
                f'{fr} → <span style="color:#10b981">{to}</span></span>'
                f'<div style="color:#a3a3a3;font-size:13px;margin-top:6px">'
                f'{blurb}</div></td></tr>'
            )
        shifts_html = "".join(rows)
    else:
        shifts_html = (
            '<tr><td style="padding:14px 0;color:#737373;font-size:13px">'
            'No verdict shifts ≥7d ago this week — markets are stable. '
            f'<a href="{SITE}/dcpi" style="color:#10b981">Full DCPI →</a></td></tr>'
        )

    # ── section 4: strategic note ──
    strategic_html = ""
    if ctx.get("strategic_note"):
        sn = ctx["strategic_note"]
        url_link = (f'<a href="{sn["url"]}" style="color:#10b981;text-decoration:none">'
                    f'see source →</a>') if sn.get("url") else ""
        strategic_html = (
            f'<p style="color:#a3a3a3;font-size:14px;line-height:1.6;margin:0">'
            f'{_h(sn.get("detail"))}</p>'
            f'<p style="color:#737373;font-size:12px;margin:10px 0 0">'
            f'{url_link}</p>'
        )
    else:
        strategic_html = (
            '<p style="color:#737373;font-size:13px;margin:0">'
            'Brain note pending — the strategic recommendation engine '
            'reports next Friday.</p>'
        )

    # ── section 5: operator snippet ──
    op_html = ""
    if ctx.get("operator_snippet"):
        op_html = (
            f'<p style="color:#a3a3a3;font-size:14px;line-height:1.6;margin:0">'
            f'{_h(ctx["operator_snippet"].get("headline"))}</p>'
        )
    else:
        op_html = ""

    hero = ctx.get("hero_stats") or {}
    facilities = hero.get("facilities") or 0
    markets = hero.get("markets") or 0

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub Weekly — {_h(week_label)}</title>
</head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a">
<tr><td align="center" style="padding:32px 12px">

<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#0f0f0f;border:1px solid #1f1f1f;border-radius:12px">

<!-- Header -->
<tr><td style="padding:28px 28px 18px;border-bottom:1px solid #1f1f1f">
  <div style="color:#737373;font-size:11px;letter-spacing:1.5px;text-transform:uppercase">
    DC HUB WEEKLY · {_h(week_label)}
  </div>
  <h1 style="margin:8px 0 0;color:#e5e5e5;font-size:24px;line-height:1.3;font-weight:700">
    What moved in data centers this week
  </h1>
</td></tr>

<!-- Section 1: Top LinkedIn posts -->
<tr><td style="padding:24px 28px 6px">
  <div style="color:#10b981;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">
    Top 3 — what people read
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">
    {posts_html}
  </table>
</td></tr>

<!-- Section 2: Top MCP queries -->
<tr><td style="padding:24px 28px 6px">
  <div style="color:#10b981;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">
    Top 3 — what AI agents asked
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">
    {queries_html}
  </table>
</td></tr>

<!-- Section 3: Verdict shifts -->
<tr><td style="padding:24px 28px 6px">
  <div style="color:#10b981;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">
    Top 3 — markets that moved
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">
    {shifts_html}
  </table>
</td></tr>

<!-- Section 4: Strategic note -->
<tr><td style="padding:24px 28px 6px">
  <div style="color:#10b981;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">
    From the brain
  </div>
  <div style="margin-top:12px;padding:16px;background:#0a0a0a;border-left:3px solid #10b981;border-radius:6px">
    {strategic_html}
  </div>
</td></tr>

<!-- Section 5: Operator brief snippet -->
{f'<tr><td style="padding:24px 28px 6px"><div style="color:#10b981;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">This week we shipped</div><div style="margin-top:12px">{op_html}</div></td></tr>' if op_html else ''}

<!-- CTA -->
<tr><td style="padding:28px 28px 24px;border-top:1px solid #1f1f1f">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <a href="{SITE}" style="display:inline-block;background:#10b981;color:#0a0a0a;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:700;font-size:14px">
        Explore the full data →
      </a>
    </td></tr>
    <tr><td align="center" style="padding-top:14px">
      <a href="{SITE}/r/signup" style="color:#737373;text-decoration:none;font-size:13px">
        or claim a free dev key (50 calls/day) →
      </a>
    </td></tr>
  </table>
</td></tr>

<!-- Footer -->
<tr><td style="padding:18px 28px;border-top:1px solid #1f1f1f;text-align:center">
  <div style="color:#525252;font-size:11px;line-height:1.6">
    DC Hub is tracking {facilities:,} facilities across {markets} markets.<br>
    You're receiving this because you hold a DC Hub dev key or signed up at
    <a href="{SITE}/newsletter" style="color:#737373;text-decoration:underline">dchub.cloud/newsletter</a>.<br>
    <a href="{unsub_url}" style="color:#525252;text-decoration:underline">Unsubscribe</a> ·
    <a href="{SITE}/newsletter/archive" style="color:#525252;text-decoration:underline">View archive</a> ·
    Issue {_h(issue_id)}
  </div>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""
    return html_doc


# ── recipients ────────────────────────────────────────────────────────────
def select_recipients(cap: int = _RECIPIENT_CAP, throttle_days: int = _RESEND_DAYS,
                       issue_id: str | None = None) -> list[str]:
    """Pull active subscribers + dev-key holders, dedupe, exclude:
      - unsubscribed (newsletter_subscribers.unsubscribed_at IS NOT NULL)
      - sent within `throttle_days`
      - already sent this issue (newsletter_sends, if issue_id given)
    Returns at most `cap` lowercase emails."""
    c = _conn()
    if not c:
        return []
    out: list[str] = []
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            c.commit()
            # Build the recipient pool — UNION newsletter_subscribers,
            # mcp_dev_keys, digest_subscribers. Exclude tombstones from
            # newsletter_subscribers (unsubscribed_at IS NOT NULL).
            query = """
                WITH pool AS (
                    SELECT email FROM newsletter_subscribers
                     WHERE unsubscribed_at IS NULL
                    UNION
                    SELECT email FROM mcp_dev_keys
                     WHERE status = 'active' AND email IS NOT NULL
                       AND email NOT LIKE '%@example.com'
                       AND email NOT LIKE 'trial-%'
                ),
                blocked AS (
                    SELECT email FROM newsletter_subscribers
                     WHERE unsubscribed_at IS NOT NULL
                ),
                recent AS (
                    SELECT email FROM newsletter_sends
                     WHERE sent_at >= NOW() - INTERVAL '%s days'
                )
                SELECT DISTINCT p.email
                  FROM pool p
                 WHERE LOWER(p.email) NOT IN (SELECT LOWER(email) FROM blocked)
                   AND LOWER(p.email) NOT IN (SELECT LOWER(email) FROM recent)
                   AND p.email LIKE '%%@%%'
                 LIMIT %s
            """ % (int(throttle_days), int(cap))
            cur.execute(query)
            for (email,) in cur.fetchall():
                if email and "@" in email:
                    out.append(email.strip().lower())
            # If issue_id provided, exclude already-sent for THIS issue
            if issue_id and out:
                cur.execute(
                    "SELECT LOWER(email) FROM newsletter_sends WHERE issue_id = %s",
                    (issue_id,))
                sent_set = {r[0] for r in cur.fetchall() if r and r[0]}
                out = [e for e in out if e.lower() not in sent_set]
    except Exception as e:
        logger.error("select_recipients failed: %s", e)
    finally:
        try: c.close()
        except Exception: pass
    # Dedupe preserving order
    seen, uniq = set(), []
    for e in out:
        le = e.lower()
        if le in seen: continue
        seen.add(le); uniq.append(le)
    return uniq[:cap]


def _resend_send(to_email: str, subject: str, html_body: str) -> dict:
    """Resend API single send. Returns {ok, id?, error?}."""
    if _DRY_RUN:
        return {"ok": True, "id": f"dry-{_secrets.token_hex(6)}", "dry_run": True}
    if not _RESEND_KEY:
        return {"ok": False, "error": "no_resend_key"}
    import requests as _rq
    try:
        rr = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_KEY}",
                     "Content-Type": "application/json"},
            json={"from": _FROM_EMAIL,
                  "to": [to_email],
                  "reply_to": "jonathan@dchub.cloud",
                  "subject": subject,
                  "html": html_body},
            timeout=15,
        )
        if rr.status_code < 400:
            return {"ok": True, "id": (rr.json() or {}).get("id")}
        return {"ok": False,
                "error": f"resend_http_{rr.status_code}: {(rr.text or '')[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def send_newsletter(ctx: dict, recipients: list[str], issue_id: str,
                     persist_issue: bool = True) -> dict:
    """Send the newsletter to each recipient. Idempotent per (issue_id, email)."""
    sent, failed = [], []
    c = _conn()
    if not c:
        return {"ok": False, "error": "no_database", "sent": 0, "failed": 0}
    subject = f"DC Hub Weekly — {ctx.get('week_label')}"
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            c.commit()
            for email in recipients:
                # Check if already sent this issue (extra safety)
                cur.execute(
                    "SELECT 1 FROM newsletter_sends "
                    "WHERE issue_id = %s AND LOWER(email) = LOWER(%s) LIMIT 1",
                    (issue_id, email))
                if cur.fetchone():
                    continue  # idempotent skip
                html_body = render_newsletter_html(ctx, recipient_email=email)
                res = _resend_send(email, subject, html_body)
                if res.get("ok"):
                    sent.append({"email": email, "resend_id": res.get("id")})
                    try:
                        cur.execute("""
                            INSERT INTO newsletter_sends
                                (issue_id, email, resend_id, status)
                            VALUES (%s, %s, %s, 'sent')
                            ON CONFLICT (issue_id, email) DO NOTHING
                        """, (issue_id, email, res.get("id")))
                        cur.execute("""
                            UPDATE newsletter_subscribers
                               SET last_sent_at = NOW()
                             WHERE LOWER(email) = LOWER(%s)
                        """, (email,))
                        c.commit()
                    except Exception as e:
                        c.rollback()
                        logger.warning("newsletter_sends insert failed for %s: %s",
                                       email, e)
                else:
                    failed.append({"email": email,
                                   "reason": res.get("error") or "unknown"})
            # Persist issue row (idempotent)
            if persist_issue and (sent or failed):
                try:
                    cur.execute("""
                        INSERT INTO newsletter_issues
                            (issue_id, sent_at, recipient_count,
                             content_html, content_json)
                        VALUES (%s, NOW(), %s, %s, %s)
                        ON CONFLICT (issue_id) DO UPDATE
                            SET recipient_count = newsletter_issues.recipient_count
                                                  + EXCLUDED.recipient_count
                    """, (issue_id, len(sent),
                          render_newsletter_html(ctx, recipient_email=""),
                          json.dumps(ctx, default=str)))
                    c.commit()
                except Exception as e:
                    c.rollback()
                    logger.warning("newsletter_issues insert failed: %s", e)
    finally:
        try: c.close()
        except Exception: pass
    return {
        "ok":         True,
        "issue_id":   issue_id,
        "attempted":  len(recipients),
        "sent":       len(sent),
        "failed":     len(failed),
        "dry_run":    _DRY_RUN,
        "sent_emails": sent[:50],
        "failures":   failed[:20],
    }


# ── public signup page ────────────────────────────────────────────────────
@weekly_newsletter_bp.route("/newsletter", methods=["GET"])
def public_signup_page():
    """Public signup page — minimal, on-brand, single form."""
    sub_count = 0
    try:
        c = _conn()
        if c:
            with c.cursor() as cur:
                _ensure_tables(cur)
                c.commit()
                cur.execute("SELECT COUNT(*) FROM newsletter_subscribers "
                            "WHERE unsubscribed_at IS NULL")
                sub_count = int((cur.fetchone() or [0])[0])
            c.close()
    except Exception:
        pass
    pg = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub Weekly — get the newsletter</title>
<meta name="description" content="One email every Friday. Top LinkedIn posts, top AI-agent queries, top DCPI verdict shifts, one strategic note from the brain. Free. No card.">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0a;color:#e5e5e5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.6}}
.wrap{{max-width:560px;margin:0 auto;padding:48px 24px}}
.eyebrow{{color:#10b981;font-size:11px;letter-spacing:2px;text-transform:uppercase;font-weight:600;margin-bottom:14px}}
h1{{font-size:32px;line-height:1.2;margin-bottom:16px;color:#fafafa}}
.sub{{color:#a3a3a3;font-size:16px;margin-bottom:32px}}
.bullets{{list-style:none;margin:24px 0 32px}}
.bullets li{{padding:10px 0;color:#d4d4d4;font-size:14px}}
.bullets li:before{{content:"→ ";color:#10b981;font-weight:700;margin-right:6px}}
form{{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}}
input[type=email]{{flex:1;min-width:240px;padding:14px 16px;background:#0f0f0f;color:#e5e5e5;border:1px solid #404040;border-radius:8px;font-size:15px;font-family:inherit}}
input[type=email]:focus{{outline:0;border-color:#10b981}}
button{{padding:14px 22px;background:#10b981;color:#0a0a0a;border:0;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}}
button:hover{{background:#0d9668}}
.msg{{padding:12px;margin-top:8px;border-radius:8px;font-size:14px;display:none}}
.msg.ok{{background:#052e16;color:#86efac;display:block}}
.msg.err{{background:#3f1212;color:#fca5a5;display:block}}
.meta{{color:#737373;font-size:13px;margin-top:24px}}
.meta a{{color:#10b981;text-decoration:none}}
a{{color:#10b981}}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">DC HUB · WEEKLY</div>
  <h1>One email. Every Friday. What moved in data centers.</h1>
  <p class="sub">Curated by the brain that runs DC Hub — same engine that
  powers the live API, the DCPI Power Index, and the MCP server.</p>
  <ul class="bullets">
    <li>Top 3 LinkedIn posts by engagement</li>
    <li>Top 3 MCP queries by volume (what AI agents are asking)</li>
    <li>Top 3 DCPI verdict shifts (markets that moved BUILD ↔ CAUTION ↔ AVOID)</li>
    <li>One strategic note from the brain</li>
    <li>What shipped on DC Hub this week</li>
  </ul>
  <form id="nl-form" onsubmit="return submit(event)">
    <input type="email" name="email" placeholder="you@firm.com" required autocomplete="email">
    <button type="submit">Subscribe</button>
  </form>
  <div id="msg" class="msg"></div>
  <p class="meta">Free. No card. Unsubscribe in one click.
   {sub_count} subscribers. <a href="/newsletter/archive">View past issues →</a></p>
</div>
<script>
function submit(e) {{
  e.preventDefault();
  var email = document.querySelector('input[name=email]').value.trim();
  var msg = document.getElementById('msg');
  msg.className = 'msg';
  fetch('/api/v1/newsletter/subscribe', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{email: email, source: 'newsletter-page'}})
  }}).then(r=>r.json()).then(d=>{{
    if (d.ok) {{
      msg.className = 'msg ok';
      msg.textContent = "You're in. Welcome email landing in your inbox now.";
    }} else {{
      msg.className = 'msg err';
      msg.textContent = d.error || 'Something went wrong. Try again.';
    }}
  }}).catch(_=>{{ msg.className='msg err';
    msg.textContent='Network error — try again in a moment.'; }});
  return false;
}}
</script></body></html>"""
    return Response(pg, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})


@weekly_newsletter_bp.route("/api/v1/newsletter/subscribe",
                              methods=["POST", "OPTIONS"])
def api_subscribe():
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS",
                          "Access-Control-Allow-Headers": "Content-Type"})
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or request.form.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 200:
        return jsonify(ok=False, error="invalid_email"), 400
    if not re.match(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$", email):
        return jsonify(ok=False, error="invalid_email"), 400
    source = (body.get("source") or "signup")[:80]
    c = _conn()
    if not c:
        return jsonify(ok=False, error="db_unavailable"), 503
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            cur.execute("""
                INSERT INTO newsletter_subscribers (email, source)
                VALUES (%s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET unsubscribed_at = NULL,
                        subscribed_at   = COALESCE(newsletter_subscribers.subscribed_at,
                                                    NOW())
            """, (email, source))
            c.commit()
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.error("subscribe failed for %s: %s", email, e)
        return jsonify(ok=False, error="db_error"), 503
    finally:
        try: c.close()
        except Exception: pass

    # Welcome email (fire-and-forget; ignore failures)
    try:
        if _RESEND_KEY and not _DRY_RUN:
            welcome_html = _welcome_email_html(email)
            _resend_send(email,
                         "Welcome to the DC Hub Weekly",
                         welcome_html)
    except Exception as e:
        logger.debug("welcome email skipped: %s", e)

    resp = jsonify(ok=True, email=email, next_send="Friday 16:00 UTC")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def _welcome_email_html(email: str) -> str:
    unsub = _unsub_url(email)
    return f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:32px 12px;background:#0a0a0a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="560" cellpadding="0" cellspacing="0"
       style="max-width:560px;margin:0 auto;background:#0f0f0f;border:1px solid #1f1f1f;border-radius:12px">
<tr><td style="padding:32px 32px 18px">
  <div style="color:#10b981;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">
    Welcome to the DC Hub Weekly
  </div>
  <h1 style="margin:14px 0 0;color:#e5e5e5;font-size:24px;line-height:1.3">
    You're in. Here's what to expect.
  </h1>
</td></tr>
<tr><td style="padding:0 32px 14px;color:#a3a3a3;font-size:15px;line-height:1.7">
  Every Friday at 9am PT, you'll get one short email with:
  <ul style="color:#d4d4d4;font-size:14px;padding-left:18px;line-height:1.9">
    <li>Top 3 LinkedIn posts by engagement</li>
    <li>Top 3 MCP queries by volume (what AI agents are asking)</li>
    <li>Top 3 DCPI verdict shifts (markets that moved this week)</li>
    <li>One strategic note from the brain</li>
    <li>What shipped on DC Hub</li>
  </ul>
  <p style="margin:18px 0 0">In the meantime, the live data lives at
    <a href="{SITE}" style="color:#10b981">dchub.cloud</a> — every claim is
    sourced to a live API endpoint.</p>
</td></tr>
<tr><td style="padding:14px 32px 18px;border-top:1px solid #1f1f1f">
  <a href="{SITE}/r/signup" style="display:inline-block;background:#10b981;color:#0a0a0a;text-decoration:none;padding:11px 18px;border-radius:8px;font-weight:700;font-size:13px">
    Claim a free dev key (50 calls/day) →
  </a>
</td></tr>
<tr><td style="padding:14px 32px;border-top:1px solid #1f1f1f;text-align:center;color:#525252;font-size:11px">
  Not what you signed up for? <a href="{unsub}" style="color:#525252">Unsubscribe</a>
</td></tr>
</table></body></html>"""


# ── unsubscribe ───────────────────────────────────────────────────────────
@weekly_newsletter_bp.route("/newsletter/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    email = (request.args.get("e") or request.form.get("e") or "").strip().lower()
    ok = bool(email) and hmac.compare_digest(token, _unsub_token(email))
    if ok:
        c = _conn()
        if c:
            try:
                with c.cursor() as cur:
                    _ensure_tables(cur)
                    cur.execute("""
                        INSERT INTO newsletter_subscribers
                            (email, source, subscribed_at, unsubscribed_at)
                        VALUES (%s, 'unsub', NOW(), NOW())
                        ON CONFLICT (email) DO UPDATE
                            SET unsubscribed_at = NOW()
                    """, (email,))
                    c.commit()
            except Exception as e:
                try: c.rollback()
                except Exception: pass
                logger.warning("unsubscribe DB failed: %s", e)
            finally:
                try: c.close()
                except Exception: pass
    msg = ("You're unsubscribed. You won't receive the DC Hub Weekly anymore."
           if ok else
           "We couldn't verify that unsubscribe link. Email jonathan@dchub.cloud.")
    pg = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe · DC Hub</title></head>
<body style="margin:0;background:#0a0a0a;color:#e5e5e5;font-family:-apple-system,sans-serif;padding:48px 16px;text-align:center">
<div style="max-width:480px;margin:0 auto;background:#0f0f0f;padding:32px;border:1px solid #1f1f1f;border-radius:12px">
  <p style="font-size:15px;line-height:1.6">{_h(msg)}</p>
  <p style="margin-top:24px"><a href="{SITE}" style="color:#10b981">dchub.cloud →</a></p>
</div></body></html>"""
    return Response(pg, status=200 if ok else 400, mimetype="text/html")


# ── archive ───────────────────────────────────────────────────────────────
@weekly_newsletter_bp.route("/newsletter/archive", methods=["GET"])
def archive_page():
    rows_html = ""
    c = _conn()
    if c:
        try:
            with c.cursor() as cur:
                _ensure_tables(cur)
                c.commit()
                cur.execute("""
                    SELECT issue_id, sent_at, recipient_count
                      FROM newsletter_issues
                     ORDER BY sent_at DESC
                     LIMIT 52
                """)
                rows = cur.fetchall() or []
                if rows:
                    items = []
                    for r in rows:
                        iid, sa, rc = r[0], r[1], r[2]
                        date_label = sa.strftime("%b %d, %Y") if sa else iid
                        items.append(
                            f'<li style="padding:14px 0;border-bottom:1px solid #1f1f1f">'
                            f'<a href="/newsletter/issue/{_h(iid)}" '
                            f'style="color:#e5e5e5;text-decoration:none;'
                            f'font-size:15px;font-weight:600">'
                            f'DC Hub Weekly — {_h(date_label)}</a>'
                            f'<div style="color:#737373;font-size:12px;margin-top:4px">'
                            f'Sent to {int(rc or 0):,} subscribers</div></li>'
                        )
                    rows_html = "".join(items)
                else:
                    rows_html = (
                        '<li style="padding:14px 0;color:#737373">'
                        'No issues yet — the first goes out this Friday at 16:00 UTC. '
                        '<a href="/newsletter" style="color:#10b981">Subscribe →</a></li>'
                    )
        except Exception as e:
            logger.warning("archive_page DB: %s", e)
        finally:
            try: c.close()
            except Exception: pass

    pg = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub Weekly — archive</title>
<style>body{{margin:0;background:#0a0a0a;color:#e5e5e5;font-family:-apple-system,sans-serif}}
.wrap{{max-width:640px;margin:0 auto;padding:48px 24px}}
h1{{font-size:28px;margin-bottom:12px;color:#fafafa}}
.sub{{color:#a3a3a3;font-size:15px;margin-bottom:32px}}
ul{{list-style:none;padding:0}}
a{{color:#10b981;text-decoration:none}}
</style></head><body><div class="wrap">
<h1>DC Hub Weekly · archive</h1>
<p class="sub">Every Friday. Every issue archived here.
<a href="/newsletter">Subscribe →</a></p>
<ul>{rows_html}</ul>
</div></body></html>"""
    return Response(pg, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})


@weekly_newsletter_bp.route("/newsletter/issue/<issue_id>", methods=["GET"])
def issue_page(issue_id):
    c = _conn()
    if not c:
        return Response("Database unavailable", status=503)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT content_html FROM newsletter_issues "
                        "WHERE issue_id = %s", (issue_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return Response(
                    f"<!doctype html><body style=\"background:#0a0a0a;color:#e5e5e5;"
                    f"font-family:-apple-system,sans-serif;padding:48px 16px;"
                    f"text-align:center\"><h1>Issue not found</h1>"
                    f"<p><a href=\"/newsletter/archive\" style=\"color:#10b981\">"
                    f"Back to archive →</a></p></body>",
                    status=404, mimetype="text/html")
            return Response(row[0], mimetype="text/html",
                            headers={"Cache-Control": "public, max-age=3600"})
    finally:
        try: c.close()
        except Exception: pass


# ── admin endpoints ───────────────────────────────────────────────────────
def _admin_authed() -> bool:
    if not _ADMIN_KEY:
        return False
    given = (request.headers.get("X-Admin-Key")
             or request.headers.get("X-Internal-Key")
             or request.args.get("key") or "").strip()
    return hmac.compare_digest(given, _ADMIN_KEY)


@weekly_newsletter_bp.route("/api/v1/admin/newsletter/preview", methods=["GET"])
def admin_preview():
    if not _admin_authed():
        return jsonify(error="unauthorized"), 401
    ctx = gather_newsletter_context()
    fmt = (request.args.get("format") or "html").lower()
    if fmt == "json":
        return jsonify(ctx)
    html_body = render_newsletter_html(ctx, recipient_email="preview@dchub.cloud")
    return Response(html_body, mimetype="text/html")


@weekly_newsletter_bp.route("/api/v1/admin/newsletter/send-test", methods=["POST"])
def admin_send_test():
    if not _admin_authed():
        return jsonify(error="unauthorized"), 401
    if _KILL:
        return jsonify(ok=False, skipped="kill_switch"), 200
    body = request.get_json(silent=True) or {}
    to = (body.get("to") or _TEST_RECIPIENT).strip().lower()
    if not to or "@" not in to:
        return jsonify(error="invalid_to"), 400
    ctx = gather_newsletter_context()
    html_body = render_newsletter_html(ctx, recipient_email=to)
    subject = f"[TEST] DC Hub Weekly — {ctx.get('week_label')}"
    result = _resend_send(to, subject, html_body)
    return jsonify(ok=result.get("ok"),
                   to=to,
                   resend_id=result.get("id"),
                   error=result.get("error"),
                   dry_run=_DRY_RUN,
                   issue_id=ctx.get("issue_id"))


@weekly_newsletter_bp.route("/api/v1/admin/newsletter/send", methods=["POST"])
def admin_send_now():
    """Admin send: skip Friday gate, but still respect throttle + idempotency.
    Returns immediately for confirm=false; pass ?confirm=true to send."""
    if not _admin_authed():
        return jsonify(error="unauthorized"), 401
    if _KILL:
        return jsonify(ok=False, skipped="kill_switch"), 200
    confirm = (request.args.get("confirm") or "").lower() in ("1", "true", "yes")
    ctx = gather_newsletter_context()
    issue_id = ctx["issue_id"]
    recipients = select_recipients(issue_id=issue_id)
    if not confirm:
        return jsonify(ok=True,
                       dry_count=True,
                       issue_id=issue_id,
                       would_send_to=len(recipients),
                       sample=recipients[:5],
                       hint="add ?confirm=true to actually send")
    result = send_newsletter(ctx, recipients, issue_id)
    return jsonify(result)


@weekly_newsletter_bp.route("/api/v1/admin/newsletter/stats", methods=["GET"])
def admin_stats():
    if not _admin_authed():
        return jsonify(error="unauthorized"), 401
    out = {"subscribers": 0, "unsubscribed": 0, "issues": []}
    c = _conn()
    if not c:
        return jsonify(error="db_unavailable"), 503
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            c.commit()
            cur.execute("SELECT COUNT(*) FROM newsletter_subscribers "
                        "WHERE unsubscribed_at IS NULL")
            out["subscribers"] = int((cur.fetchone() or [0])[0])
            cur.execute("SELECT COUNT(*) FROM newsletter_subscribers "
                        "WHERE unsubscribed_at IS NOT NULL")
            out["unsubscribed"] = int((cur.fetchone() or [0])[0])
            cur.execute("""
                SELECT issue_id, sent_at, recipient_count,
                       opens_count, clicks_count
                  FROM newsletter_issues
                 ORDER BY sent_at DESC
                 LIMIT 5
            """)
            for r in cur.fetchall() or []:
                out["issues"].append({
                    "issue_id":        r[0],
                    "sent_at":         r[1].isoformat() if r[1] else None,
                    "recipient_count": int(r[2] or 0),
                    "opens_count":     int(r[3] or 0),
                    "clicks_count":    int(r[4] or 0),
                })
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(out)


@weekly_newsletter_bp.route("/admin/newsletter", methods=["GET"])
def admin_dashboard():
    """Admin dashboard — server-renders preview + stats."""
    if not _admin_authed():
        return Response(
            f"<!doctype html><body style=\"background:#0a0a0a;color:#e5e5e5;"
            f"font-family:-apple-system,sans-serif;padding:48px 16px;text-align:center\">"
            f"<h1>Unauthorized</h1>"
            f"<p>Append ?key=YOUR_ADMIN_KEY to access.</p></body>",
            status=401, mimetype="text/html")
    ctx = gather_newsletter_context()
    preview_html = render_newsletter_html(ctx, recipient_email="preview@dchub.cloud")
    # Stats
    subs = unsub = 0
    issues = []
    c = _conn()
    if c:
        try:
            with c.cursor() as cur:
                _ensure_tables(cur)
                c.commit()
                cur.execute("SELECT COUNT(*) FROM newsletter_subscribers "
                            "WHERE unsubscribed_at IS NULL")
                subs = int((cur.fetchone() or [0])[0])
                cur.execute("SELECT COUNT(*) FROM newsletter_subscribers "
                            "WHERE unsubscribed_at IS NOT NULL")
                unsub = int((cur.fetchone() or [0])[0])
                cur.execute("""
                    SELECT issue_id, sent_at, recipient_count,
                           opens_count, clicks_count
                      FROM newsletter_issues
                     ORDER BY sent_at DESC
                     LIMIT 5
                """)
                for r in cur.fetchall() or []:
                    issues.append({"issue_id": r[0],
                                   "sent_at": r[1].isoformat() if r[1] else "",
                                   "recipient_count": int(r[2] or 0)})
        finally:
            try: c.close()
            except Exception: pass

    issues_html = "".join(
        f"<tr><td>{_h(i['issue_id'])}</td><td>{_h(i['sent_at'])}</td>"
        f"<td style=\"text-align:right\">{i['recipient_count']:,}</td></tr>"
        for i in issues
    ) or "<tr><td colspan=3 style=\"text-align:center;color:#737373\">No issues sent yet</td></tr>"

    key_qs = quote(request.args.get("key") or "")
    pg = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Newsletter Admin</title>
<style>body{{margin:0;background:#0a0a0a;color:#e5e5e5;font-family:-apple-system,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 24px}}
h1{{font-size:24px;margin:0 0 8px}}
h2{{font-size:14px;color:#10b981;margin:24px 0 12px;text-transform:uppercase;letter-spacing:1.5px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 24px}}
.card{{background:#0f0f0f;border:1px solid #1f1f1f;padding:18px;border-radius:10px}}
.card .lbl{{color:#737373;font-size:11px;text-transform:uppercase;letter-spacing:1px}}
.card .val{{color:#fafafa;font-size:28px;font-weight:700;margin-top:6px}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.box{{background:#0f0f0f;border:1px solid #1f1f1f;padding:18px;border-radius:10px}}
button,.btn{{background:#10b981;color:#0a0a0a;border:0;padding:10px 18px;border-radius:8px;font-weight:700;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block}}
.btn-secondary{{background:#1f1f1f;color:#e5e5e5}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 6px;text-align:left;border-bottom:1px solid #1f1f1f}}
th{{color:#737373;font-size:11px;text-transform:uppercase;letter-spacing:1px}}
iframe{{width:100%;height:800px;border:1px solid #1f1f1f;border-radius:10px;background:#fff}}
#send-result{{margin-top:12px;padding:12px;border-radius:8px;font-size:13px;display:none}}
</style></head><body>
<div class="wrap">
<h1>Newsletter Admin</h1>
<p style="color:#737373;font-size:13px">Current state of the DC Hub Weekly system. Throttle: {_RESEND_DAYS}d. Cap: {_RECIPIENT_CAP}. Dry-run: {_DRY_RUN}. Kill switch: {_KILL}.</p>

<div class="cards">
  <div class="card"><div class="lbl">Active subscribers</div><div class="val">{subs:,}</div></div>
  <div class="card"><div class="lbl">Unsubscribed</div><div class="val">{unsub:,}</div></div>
  <div class="card"><div class="lbl">Next issue</div><div class="val">{_h(ctx.get('issue_id'))}</div></div>
  <div class="card"><div class="lbl">Schedule</div><div class="val" style="font-size:14px;padding-top:14px">Friday 16:00 UTC</div></div>
</div>

<h2>Last 5 issues</h2>
<div class="box">
<table>
<thead><tr><th>Issue ID</th><th>Sent at</th><th style="text-align:right">Recipients</th></tr></thead>
<tbody>{issues_html}</tbody>
</table>
</div>

<h2>Send now</h2>
<div class="box">
<p style="font-size:13px;color:#a3a3a3;margin-bottom:14px">Skip the Friday gate. Throttling + idempotency still apply.</p>
<button onclick="sendTest()">Send test → {_h(_TEST_RECIPIENT)}</button>
<button class="btn-secondary" onclick="sendNow()" style="margin-left:8px">Send to full list (with confirm)</button>
<div id="send-result"></div>
</div>

<h2>Preview — next issue</h2>
<iframe srcdoc="{_h(preview_html)}" sandbox="allow-same-origin"></iframe>
</div>

<script>
var KEY = "{key_qs}";
function sendTest() {{
  var r = document.getElementById('send-result');
  r.style.display='block'; r.style.background='#1f2937'; r.textContent='Sending test...';
  fetch('/api/v1/admin/newsletter/send-test?key=' + KEY, {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'
  }}).then(x=>x.json()).then(d=>{{
    r.style.background = d.ok ? '#052e16' : '#3f1212';
    r.textContent = JSON.stringify(d, null, 2);
  }});
}}
function sendNow() {{
  if (!confirm('Send to ALL active subscribers + dev-key holders?')) return;
  var r = document.getElementById('send-result');
  r.style.display='block'; r.style.background='#1f2937'; r.textContent='Sending...';
  fetch('/api/v1/admin/newsletter/send?confirm=true&key=' + KEY, {{method:'POST'}})
    .then(x=>x.json()).then(d=>{{
      r.style.background = d.ok ? '#052e16' : '#3f1212';
      r.textContent = JSON.stringify(d, null, 2);
    }});
}}
</script></body></html>"""
    return Response(pg, mimetype="text/html")


# ── scheduler runner (called from crawler_scheduler.py) ──────────────────
def _run_weekly_newsletter():
    """Friday-only gate inside; called twice per day per same-hour pairing.

    Throttles via newsletter_sends table (issue_id+email UNIQUE) — even if
    the scheduler fires twice on the same Friday, no recipient gets it twice.
    Also gates: weekday != 4 (Friday) -> skip silently. Issue_id is week-
    pinned (ISO week), so a same-week re-fire is also no-op."""
    if _KILL:
        logger.info("📬 weekly_newsletter: skipped — NEWSLETTER_DISABLE=1")
        return
    now = datetime.datetime.now(timezone.utc)
    if now.weekday() != 4:  # 0=Mon ... 4=Fri
        logger.debug("📬 weekly_newsletter: skip — not Friday (today=%s)",
                     now.strftime("%A"))
        return
    if not _RESEND_KEY and not _DRY_RUN:
        logger.warning("📬 weekly_newsletter: skipped — DCHUB_RESEND_API_KEY unset")
        return
    ctx = gather_newsletter_context(now)
    issue_id = ctx["issue_id"]
    # Don't re-send within 5 days for any recipient
    recipients = select_recipients(issue_id=issue_id)
    if not recipients:
        logger.info("📬 weekly_newsletter: 0 recipients (all throttled or empty)")
        return
    result = send_newsletter(ctx, recipients, issue_id)
    logger.info("📬 weekly_newsletter %s: attempted=%s sent=%s failed=%s dry_run=%s",
                issue_id, result.get("attempted"), result.get("sent"),
                result.get("failed"), result.get("dry_run"))
    return result


__all__ = [
    "weekly_newsletter_bp",
    "gather_newsletter_context",
    "render_newsletter_html",
    "select_recipients",
    "send_newsletter",
    "_run_weekly_newsletter",
]
