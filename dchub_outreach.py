"""
DC Hub Outreach Engine — autonomous customer email pipeline.

Pulls from:
  • /api/v1/admin/churn-risk
  • /api/v1/admin/welcome-sequence
  • /api/v1/mcp/power-users

Personalizes templates per customer scenario.
Sends via Resend (RESEND_API_KEY required).
Tracks every send in email_outreach_log table.
Marks mcp_upgrade_signals.outreach_sent = true.

Safety:
  • dry_run defaults to True
  • max_emails per dispatch is capped
  • re-running is idempotent (skips already-emailed customers)
"""
import os
# ============================================================================
# Phase 267: INTERNAL EMAIL EXCLUSIONS — never email these
# ============================================================================

INTERNAL_EMAIL_PATTERNS = [
    # Domains
    "@dchub.cloud",
    "@arcadianinfra.com",
    "@martoneadvisors.com",
    # Specific people (you + family + test accounts)
    "azmartone@",
    "nicomartone@",
    "jonathan.martone@",
    "jonathanmartone@",
    # Stripe / generic test patterns
    "+stripe",
    "+test",
    "+dev",
    "noreply@",
    "no-reply@",
    "test@",
    "demo@",
]


def is_internal_email(email):
    """Returns True if email matches any internal/test exclusion pattern."""
    if not email: return True
    e = email.lower().strip()
    return any(pat.lower() in e for pat in INTERNAL_EMAIL_PATTERNS)


def get_db_excludes():
    """Pull additional excludes from email_exclude_list table (if exists)."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS email_exclude_list (
                    email TEXT PRIMARY KEY,
                    reason TEXT,
                    added_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            c.commit()
            cur.execute("SELECT LOWER(email) FROM email_exclude_list;")
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


from datetime import datetime
from typing import Optional


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)


def ensure_outreach_log_table():
    """Idempotent: creates email_outreach_log if not present."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS email_outreach_log (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    subject TEXT,
                    template_key TEXT,
                    sent_at TIMESTAMPTZ DEFAULT NOW(),
                    success BOOLEAN,
                    resend_id TEXT,
                    response_body TEXT,
                    dry_run BOOLEAN DEFAULT false
                );
                CREATE INDEX IF NOT EXISTS email_outreach_log_email_idx
                    ON email_outreach_log (email, sent_at DESC);
            """)
            c.commit()
    except Exception:
        pass


def already_outreached_recently(email, days=14):
    """Returns True if we've emailed this address in the last N days."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM email_outreach_log
                WHERE LOWER(email) = LOWER(%s)
                  AND sent_at > NOW() - (%s || ' days')::interval
                  AND success = true
                  AND dry_run = false
            """, (email, days))
            n = cur.fetchone()[0]
            return n > 0
    except Exception:
        return False


def send_via_resend(to_email, subject, html_body, text_body=None, reply_to="jonathan@dchub.cloud"):
    """Send email via Resend API. Returns (ok, response_text, resend_id)."""
    # P268_INTERNAL_HARD_BLOCK — last line of defense
    if is_internal_email(to_email):
        print(f"  [BLOCKED] internal/test address — refusing to send to {to_email}")
        return {"blocked": True, "reason": "internal_email_pattern", "email": to_email}
    import requests
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return False, "RESEND_API_KEY not set", None
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Jonathan @ DC Hub <jonathan@dchub.cloud>",
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body or "",
                "reply_to": reply_to,
            },
            timeout=10,
        )
        if r.ok:
            body = r.json()
            return True, "ok", body.get("id")
        return False, r.text[:300], None
    except Exception as e:
        return False, str(e)[:200], None


def log_send(email, subject, template_key, success, resend_id, response_body, dry_run):
    """Audit log every send (including failures)."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO email_outreach_log
                (email, subject, template_key, success, resend_id, response_body, dry_run)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING;
            """, (email, subject[:300], template_key, success, resend_id, (response_body or "")[:1000], dry_run))
            c.commit()
    except Exception:
        pass


# ============================================================================
# TEMPLATES — one per scenario
# ============================================================================

def template_churn_risk_pro(customer):
    """For Pro-tier customers with zero usage."""
    name = (customer.get("name") or "there").split()[0] or "there"
    company = customer.get("company") or ""
    company_clause = f" at {company}" if company else ""
    days_since = customer.get("days_since_signup", "?")

    subject = f"{name} — your DC Hub Pro account (quick question)"

    text = f"""Hi {name},

I'm Jonathan, founder of DC Hub. I noticed your Pro account{company_clause} has been active since signup but shows zero API calls. Before your next renewal, I want to make sure DC Hub is actually delivering value or refund you cleanly.

Three quick questions:

1. What did you originally want to do with DC Hub? Data center research, M&A intelligence, site selection, AI agent integration via MCP, grid/infrastructure data — whatever it is, I want to point you at the right path.

2. Was there a specific blocker? Setup confusion, missing data, slow responses, MCP errors, documentation gaps — I'd rather hear "your API rejected my request because of X" than have you cancel quietly.

3. Want a refund instead? If DC Hub isn't a fit, I'll refund your last 30 days clean. Reply "refund" and I'll process today.

If you want to give it another try, I can extend your Pro tier or grant Enterprise access for 90 days at no extra cost. Just tell me what you need.

15 minutes works: https://cal.com/jonathanmartone

Thanks for backing this.

Best,
Jonathan Martone
Founder, DC Hub
jonathan@dchub.cloud
https://dchub.cloud"""

    html = text.replace("\n\n", "</p><p>").replace("\n", "<br>")
    html = f"<p>{html}</p>"
    return subject, html, text


def template_churn_risk_developer(customer):
    """For Developer-tier customers with zero usage (Firas pattern)."""
    name = (customer.get("name") or "there").split()[0] or "there"
    key_name = customer.get("key_name", "")
    key_clue = f" (your API key was named \"{key_name}\")" if key_name and key_name != "Pro API Key" else ""

    subject = f"{name} — your DC Hub Developer key (quick question)"

    text = f"""Hi {name},

I'm Jonathan, founder of DC Hub. I noticed your Developer plan ($49/mo) has been active but your API key{key_clue} shows zero successful calls. Before your next charge, I want to make sure you're getting value or refund cleanly.

Three quick questions:

1. What were you trying to do? Data center facility listings, M&A deal data, grid intelligence, MCP integration for AI agents — whatever the use case, I want to make sure you can get there.

2. What blocked you? Confusing docs, missing endpoints, rate limit hits, MCP setup — I'd rather hear it than have you cancel quietly.

3. Want a refund? If DC Hub isn't a fit, I'll refund 30 days. Reply "refund" and I'll process today.

If you want another try, I'll grant Pro tier free for 90 days. Tell me what you need.

15 min: https://cal.com/jonathanmartone

Best,
Jonathan Martone
Founder, DC Hub
jonathan@dchub.cloud"""

    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return subject, html, text


def template_welcome_new_paid(customer):
    """For brand-new paid customers (last N days) with zero calls yet."""
    name = (customer.get("name") or "there").split()[0] or "there"
    plan = (customer.get("plan") or "your plan").title()

    subject = f"Welcome to DC Hub, {name} — let me help you get your first successful API call"

    text = f"""Hi {name},

Jonathan here — founder of DC Hub. I noticed you just signed up for {plan} (thank you!) and your API key hasn't been used yet. Most customers who don't make a call in their first 3 days end up never coming back, so I want to head that off.

What are you trying to do? Send a single sentence reply ("I want to pull data center listings for X market" / "I need M&A comparables for Y deal" / "I'm building an AI agent that needs grid intel") and I'll send back a working curl command + the exact endpoint to call.

Or grab 15 minutes for a screen share: https://cal.com/jonathanmartone

I'd much rather help you succeed in week 1 than watch you churn in month 2.

Best,
Jonathan Martone
Founder, DC Hub
jonathan@dchub.cloud"""

    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return subject, html, text


def template_power_user_free(customer):
    """For free users with 5+ paywall signals — addressable upgrade pool."""
    name = (customer.get("name") or "there").split()[0] or "there"
    signal_count = customer.get("signal_count", 0)
    tools = customer.get("tools_blocked", [])[:3]
    tools_str = ", ".join(tools) if tools else "paid tools"

    subject = f"{name}, you hit our paid tools {signal_count} times last week"

    text = f"""Hi {name},

I'm Jonathan, founder of DC Hub. I noticed you've been hitting our paid MCP tools — specifically {tools_str} — about {signal_count} times in the last 30 days. That's high-intent usage from a free key.

Pro tier ($99/month billed annually, $1,188/yr) unlocks:
  • Full results (not previews)
  • 10,000 calls/day
  • MCP + REST + Webhooks
  • Priority support

Founding member rate is $99/mo locked for life — offer closes May 31.

Direct checkout (utm-tracked so we know it's you): https://dchub.cloud/pricing?utm_source=mcp&utm_email={customer.get('email','')}#pro-annual

Or 15 min call: https://cal.com/jonathanmartone

If there's a specific use case driving the heavy usage, reply and tell me — I'll customize Pro to make sure it covers what you need.

Best,
Jonathan Martone
Founder, DC Hub
jonathan@dchub.cloud"""

    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return subject, html, text


TEMPLATES = {
    "churn_risk_pro": template_churn_risk_pro,
    "churn_risk_developer": template_churn_risk_developer,
    "welcome_new_paid": template_welcome_new_paid,
    "power_user_free": template_power_user_free,
}


# ============================================================================
# DISPATCH — pulls candidates, applies templates, sends or previews
# ============================================================================

def build_queue(max_total=50):
    """Walks all sources and builds the outreach queue."""
    queue = []

    try:
        with _conn() as c:
            # 1. Churn risk — Pro and Developer tiers, zero usage
            with c.cursor() as cur:
                cur.execute("""
                    SELECT u.email, u.name, u.company, u.plan,
                           u.created_at, k.calls_total, k.last_used_at,
                           k.name AS key_name
                    FROM users u
                    LEFT JOIN api_keys k ON k.user_id::text = u.id::text
                    WHERE u.plan IN ('developer', 'pro', 'paid', 'enterprise')
                      AND COALESCE(k.is_active_bool, true) = true
                      AND COALESCE(k.calls_total, 0) = 0
                      AND u.email IS NOT NULL
                    ORDER BY u.created_at DESC
                    LIMIT %s
                """, (max_total,))
                for r in cur.fetchall():
                    email, name, company, plan, created, calls, last_used, key_name = r
                    if not email: continue
                    template_key = ("churn_risk_pro" if plan in ("pro","paid","enterprise")
                                    else "churn_risk_developer")
                    queue.append({
                        "email": email,
                        "name": name,
                        "company": company,
                        "plan": plan,
                        "key_name": key_name,
                        "template_key": template_key,
                        "priority": 1 if plan in ("pro","paid","enterprise") else 2,
                    })

            # 2. Power users — free users with 5+ paywall signals
            with c.cursor() as cur:
                cur.execute("""
                    SELECT user_email, COUNT(*),
                           array_agg(DISTINCT tool_requested ORDER BY tool_requested),
                           MAX(created_at)
                    FROM mcp_upgrade_signals
                    WHERE created_at > NOW() - INTERVAL '30 days'
                      AND user_email IS NOT NULL AND user_email != ''
                      AND COALESCE(converted, false) = false
                      AND COALESCE(outreach_sent, false) = false
                    GROUP BY user_email
                    HAVING COUNT(*) >= 5
                    ORDER BY COUNT(*) DESC LIMIT %s
                """, (max_total,))
                for r in cur.fetchall():
                    email, signal_count, tools, last = r
                    if not email: continue
                    if email in {q["email"] for q in queue}: continue
                    queue.append({
                        "email": email,
                        "name": email.split("@")[0],
                        "signal_count": signal_count,
                        "tools_blocked": list(tools) if tools else [],
                        "template_key": "power_user_free",
                        "priority": 3,
                    })
    except Exception as e:
        return [], str(e)

    # Sort by priority, then de-dupe by email
    queue.sort(key=lambda x: x.get("priority", 99))
    seen = set()
    deduped = []
    for q in queue:
        if q["email"] in seen: continue
        seen.add(q["email"])
        deduped.append(q)

    # Phase 267: filter out internal/test emails first
    db_excludes = get_db_excludes()
    filtered_internal = [
        q for q in deduped
        if not is_internal_email(q["email"]) and q["email"].lower() not in db_excludes
    ]
    deduped = filtered_internal

    # Filter out anyone already emailed in last 14 days
    ensure_outreach_log_table()
    filtered = [q for q in deduped if not already_outreached_recently(q["email"], 14)]
    # P268_BUILD_QUEUE_FILTER — strip internal/test/db-excluded emails before returning
    try:
        _db_excl = get_db_excludes()
    except Exception:
        _db_excl = set()
    filtered = [
        _q for _q in filtered
        if not is_internal_email(_q.get("email", ""))
        and (_q.get("email", "").lower() not in _db_excl)
    ]

    return filtered[:max_total], None


def dispatch(dry_run=True, limit=10):
    """Run the full pipeline: queue → personalize → send/preview → log → mark."""
    queue, err = build_queue(max_total=limit * 3)
    if err:
        return {"ok": False, "error": err, "queue": [], "sent": [], "previews": []}

    results = {"ok": True, "dry_run": dry_run, "queue_size": len(queue),
               "limit": limit, "sent": [], "previews": [], "errors": []}

    for customer in queue[:limit]:
        tk = customer["template_key"]
        fn = TEMPLATES.get(tk)
        if not fn:
            results["errors"].append({"email": customer["email"], "reason": f"no template: {tk}"})
            continue

        subject, html, text = fn(customer)

        preview = {
            "email": customer["email"],
            "name": customer.get("name"),
            "company": customer.get("company"),
            "template_key": tk,
            "subject": subject,
            "body_preview": text[:300],
        }

        if dry_run:
            results["previews"].append(preview)
            continue

        ok, resp, resend_id = send_via_resend(customer["email"], subject, html, text)
        log_send(customer["email"], subject, tk, ok, resend_id, resp, dry_run=False)

        if ok:
            # Mark outreach_sent on related signals
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""
                        UPDATE mcp_upgrade_signals
                        SET outreach_sent = true, outreach_sent_at = NOW()
                        WHERE LOWER(user_email) = LOWER(%s)
                          AND COALESCE(outreach_sent, false) = false;
                    """, (customer["email"],))
                    c.commit()
            except Exception:
                pass

            results["sent"].append({**preview, "resend_id": resend_id})
        else:
            results["errors"].append({"email": customer["email"], "reason": resp[:200]})

    return results
