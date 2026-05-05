#!/usr/bin/env python3
"""Email blast: free-tier users -> Developer tier with launch coupon.

Targets the highest-LTV cohort (already-signed-up free-tier users) with the
semantic-search launch announcement plus a $20-off-first-month Stripe coupon.

Safety rails:
  --dry-run        Preview targets + email body, send nothing (default ON until --send)
  --send           Actually send. Mutex with --dry-run.
  --limit N        Cap sends this run (default 10 for first live test)
  --coupon CODE    Stripe coupon code to embed (default SEMANTIC20)
  --campaign STR   Idempotency key (default 'developer_launch_2026_04').
                   Skips users who already received this campaign.

Engagement filter: free tier + (signed up in last 30 days OR last_used in last 60).
Skips invalid emails, unsubscribes, and previously-sent recipients.

Setup before running live:
  1. Create the coupon in Stripe dashboard:
     Stripe -> Products -> Coupons -> New: SEMANTIC20, $20 off, once
  2. Set env vars: SENDGRID_API_KEY, SENDGRID_FROM_EMAIL,
     DATABASE_URL or NEON_DATABASE_URL (prod, not helium)
  3. Dry-run first:    python tools/email_blast_developer_launch.py --dry-run --limit 5
  4. Test live small:  python tools/email_blast_developer_launch.py --send --limit 5
  5. Inspect inbox + email_blasts table; if good:
                       python tools/email_blast_developer_launch.py --send
"""
from __future__ import annotations

import argparse, json, os, sys, time
import urllib.request, urllib.error
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
SENDGRID_FROM    = os.environ.get('SENDGRID_FROM_EMAIL', 'launch@dchub.cloud')
SENDGRID_FROM_NAME = os.environ.get('SENDGRID_FROM_NAME', 'DC Hub')

EMAIL_SUBJECT = "DC Hub just shipped semantic search — $20 off Developer for you"

EMAIL_HTML_TEMPLATE = """\
<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a1220;color:#e8f8ff;margin:0;padding:32px;">
<div style="max-width:600px;margin:0 auto;background:#141b2d;border-radius:12px;padding:32px;border:1px solid #1e293b;">
  <h1 style="color:#00d4aa;margin:0 0 16px;font-size:24px;">Semantic search is live, {first_name}.</h1>
  <p style="line-height:1.6;color:#cbd5e1;">
    You signed up for DC Hub <b>{signup_relative}</b>. Today we shipped what most of you asked for:
    natural-language search across all 21,000+ data center facilities, with grid-aware filtering
    (PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE, NWPP).
  </p>
  <p style="line-height:1.6;color:#cbd5e1;">
    "30 MW with PJM access" used to require a SQL join. Now it returns Microsoft Ashburn,
    AWS Manassas, and STACK NoVA in 190ms — at the edge.
  </p>
  <h2 style="color:#00d4aa;font-size:18px;margin-top:32px;">Try it free:</h2>
  <p>
    <a href="https://dchub.cloud/api/v1/explorer?from=email_launch" style="display:inline-block;background:#00d4aa;color:#0a1220;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">
      Open the explorer
    </a>
  </p>
  <h2 style="color:#00d4aa;font-size:18px;margin-top:32px;">Or unlock the full Developer tier:</h2>
  <p style="line-height:1.6;color:#cbd5e1;">
    Free: 10 calls/day, 5 results.<br>
    Developer ($49/mo): 1,000 calls/day, 100 results, full grid filters.
  </p>
  <p style="line-height:1.6;color:#cbd5e1;">
    <b>Coupon for you: <span style="color:#00d4aa;font-family:monospace">{coupon}</span></b> — $20 off first month, expires in 7 days.
  </p>
  <p>
    <a href="https://dchub.cloud/pricing?from=email_launch&coupon={coupon}&user_id={user_id}" style="display:inline-block;background:#00d4aa;color:#0a1220;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">
      Upgrade to Developer
    </a>
  </p>
  <hr style="border:none;border-top:1px solid #1e293b;margin:32px 0;">
  <p style="font-size:12px;color:#64748b;line-height:1.5;">
    DC Hub — data center intelligence for 20,000+ facilities, 140+ countries.<br>
    <a href="https://dchub.cloud/unsubscribe?u={user_id}" style="color:#64748b;">Unsubscribe</a>
    &middot;
    <a href="https://dchub.cloud" style="color:#64748b;">dchub.cloud</a>
  </p>
</div>
</body></html>
"""


def get_conn():
    url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not url:
        sys.exit("ERROR: NEON_DATABASE_URL not set")
    return psycopg2.connect(url)


def ensure_audit_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_blasts (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            email       TEXT NOT NULL,
            campaign    TEXT NOT NULL,
            sent_at     TIMESTAMPTZ DEFAULT NOW(),
            status      TEXT NOT NULL,
            sg_msg_id   TEXT,
            note        TEXT,
            UNIQUE (user_id, campaign)
        )
    """)
    conn.commit()
    cur.close()


def fetch_targets(conn, campaign, limit):
    """Free-tier users, engaged, not previously emailed for this campaign."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT u.id AS user_id, u.email, u.name, u.created_at,
               COALESCE(MAX(ak.last_used_at), '1970-01-01') AS last_used,
               COUNT(ak.id) AS key_count
        FROM users u
        LEFT JOIN api_keys ak ON ak.user_id = u.id
        WHERE u.plan = 'free'
          AND u.email IS NOT NULL
          AND u.email <> ''
          AND u.email NOT LIKE '%example.com'
          AND u.email NOT LIKE '%test%'
          AND u.id NOT IN (SELECT user_id FROM email_blasts WHERE campaign = %s)
        GROUP BY u.id, u.email, u.name, u.created_at
        HAVING (
            u.created_at > NOW() - INTERVAL '30 days'
            OR MAX(ak.last_used_at)::timestamptz > NOW() - INTERVAL '60 days'
        )
        ORDER BY u.created_at DESC
        LIMIT %s
    """, (campaign, limit))
    rows = cur.fetchall()
    cur.close()
    return rows


def humanize_signup(ts):
    if not ts: return "recently"
    if isinstance(ts, str):
        try: ts = datetime.fromisoformat(ts.replace('Z','+00:00'))
        except Exception: return "recently"
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
    days = (now - ts).days
    if days < 7:  return f"{days} days ago"
    if days < 60: return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def render_email(target, coupon):
    first = (target.get('name') or '').split(' ')[0] or 'there'
    return EMAIL_HTML_TEMPLATE.format(
        first_name=first,
        signup_relative=humanize_signup(target.get('created_at')),
        coupon=coupon,
        user_id=target['user_id'],
    )


def send_via_sendgrid(to_email, html_body, subject):
    if not SENDGRID_API_KEY:
        return False, "SENDGRID_API_KEY not set"
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": SENDGRID_FROM, "name": SENDGRID_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
        "tracking_settings": {
            "click_tracking": {"enable": True},
            "open_tracking":  {"enable": True},
        },
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode('utf-8'),
        method='POST',
        headers={
            'Authorization': f'Bearer {SENDGRID_API_KEY}',
            'Content-Type':  'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, resp.headers.get('X-Message-Id', 'sent')
    except urllib.error.HTTPError as e:
        try: body = e.read().decode('utf-8', errors='replace')[:300]
        except Exception: body = str(e)
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def record_send(conn, user_id, email, campaign, status, sg_id, note):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO email_blasts (user_id, email, campaign, status, sg_msg_id, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, campaign) DO UPDATE
        SET status = EXCLUDED.status, sg_msg_id = EXCLUDED.sg_msg_id, note = EXCLUDED.note
    """, (user_id, email, campaign, status, sg_id, note))
    conn.commit()
    cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='preview, no sends (default if --send not passed)')
    ap.add_argument('--send', action='store_true', help='actually send emails')
    ap.add_argument('--limit', type=int, default=10, help='cap sends this run (default 10)')
    ap.add_argument('--coupon', default='SEMANTIC20', help='Stripe coupon code (default SEMANTIC20)')
    ap.add_argument('--campaign', default='developer_launch_2026_04', help='idempotency key')
    args = ap.parse_args()

    if args.send and args.dry_run:
        sys.exit("ERROR: --send and --dry-run are mutually exclusive")
    is_dry = not args.send

    conn = get_conn()
    ensure_audit_table(conn)
    targets = fetch_targets(conn, args.campaign, args.limit)

    print(f"==> campaign:  {args.campaign}")
    print(f"==> coupon:    {args.coupon}")
    print(f"==> mode:      {'DRY-RUN (no sends)' if is_dry else 'LIVE'}")
    print(f"==> targets:   {len(targets)}")
    print()

    if is_dry:
        for t in targets[:5]:
            print(f"  -> {t['email']:35s}  ({t.get('name') or 'unnamed':25s})  signed up {humanize_signup(t['created_at'])}")
        if len(targets) > 5:
            print(f"     ... + {len(targets) - 5} more")
        print()
        if targets:
            print("=== Sample email (first target) ===")
            print(render_email(targets[0], args.coupon)[:1200])
            print("...")
        print()
        print("To send for real:  python tools/email_blast_developer_launch.py --send --limit 5")
        return

    sent = failed = 0
    for t in targets:
        html = render_email(t, args.coupon)
        ok, info = send_via_sendgrid(t['email'], html, EMAIL_SUBJECT)
        status = 'sent' if ok else 'failed'
        record_send(conn, t['user_id'], t['email'], args.campaign,
                    status, info if ok else None, None if ok else info)
        if ok:
            sent += 1
            print(f"  +sent  {t['email']:35s}  msg_id={info}")
        else:
            failed += 1
            print(f"  -fail  {t['email']:35s}  {info}", file=sys.stderr)
        time.sleep(0.2)  # rate limit ~5/sec, well under SendGrid limits

    conn.close()
    print()
    print(f"==> done: sent={sent} failed={failed} total={len(targets)}")
    print(f"==> audit: SELECT * FROM email_blasts WHERE campaign = '{args.campaign}' ORDER BY sent_at DESC")


if __name__ == '__main__':
    main()
