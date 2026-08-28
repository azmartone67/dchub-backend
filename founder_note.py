"""
founder_note.py — automated founder-voice welcome for FOUNDING-tier conversions
(2026-07-17).

Until now the owner hand-sent a personal welcome to every Founding member
(drafted in Gmail, pasted into GoDaddy webmail because Gmail's send-as is
broken). This job automates that exact email — his own words, PLAIN TEXT (no
branded HTML template; it must read personal), from jonathan@dchub.cloud —
and sends it 5-15 MINUTES after payment so it doesn't look machine-fired.

Two triggers, one idempotent sweep:
  * handle_checkout_completed / the subscription-upgrade path schedule a
    threading.Timer at a random 5-15 min delay (the happy path).
  * A cron-heartbeat lane calls the sweep on every tick as the backstop
    (covers deploy restarts that kill in-process timers).

SAFETY:
  * Kill switch: FOUNDER_NOTE_DISABLE=1 stops all sends instantly.
  * Dedupe: one note per email, EVER — a welcome_email_log row with
    plan='founding:founder_note' (any status) marks the email as handled.
    The send path RESERVES that row atomically (INSERT ... WHERE NOT EXISTS)
    before sending, so the timer and the cron lane can never double-send.
    Seed rows with status='seeded_manual_welcome' for members the owner
    already welcomed by hand.
  * Candidates only from the founding TIER (users.plan='founding' or a
    welcome_email_log plan='founding' row), only within a recent window
    (default 5 min .. 72 h after conversion) so an old member can never be
    re-welcomed, and never internal/test addresses.
  * The admin route DRY-RUNS by default; the cron lane passes ?confirm=1.

Register with setup_founder_note_routes(app). Trigger:
  POST /api/v1/admin/founder-note/run           (X-Admin-Key)  -> DRY-RUN preview
  POST /api/v1/admin/founder-note/run?confirm=1 (X-Admin-Key)  -> actually sends
"""
import os
import json
import logging
import urllib.request

logger = logging.getLogger("founder_note")

PLAN_KEY = 'founding:founder_note'
SUBJECT = "Welcome to DC Hub — and thank you"

# The owner's own words (manual sends 2026-07-16). Keep PLAIN TEXT.
NOTE_TEMPLATE = """Hi {first_name},

Your Founding Member sign-up just came through — thank you, and welcome. There are only a limited number of founding licenses, so I wanted to reach out personally rather than let a system email be your first hello.

A few things to get you up and running right away:

1) Your access is live — Founding / Pro tier, which means the full facility + market dataset and higher rate limits. Your API key should already be in your inbox; if it isn't, just reply "key" and I'll get it straight to you.

2) Fastest way to see value with zero setup: the live playground — real-time grid scoreboard, plus the Land & Power map (dynamic power, fiber, gas, and environmental layers for site selection). https://dchub.cloud/playground

3) Want it wired into Claude / Cursor / your own agent? Takes about two minutes and I'm happy to screen-share and set it up with you.

Would 15 minutes this week work? I'll tailor it to whatever you're working on.

Really glad to have you in early.

Jonathan Martone
DC Hub · Martone Advisors
602-214-3714
jonathan@dchub.cloud
"""


def _disabled():
    return os.environ.get('FOUNDER_NOTE_DISABLE') == '1'


def _get_conn():
    """Direct psycopg2 connection to Neon (autocommit). Deliberately NOT
    db_utils.safe_db — its wrapper skips DDL and this module must be able to
    run standalone (cron/tests) without importing main."""
    import psycopg2
    url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not url:
        raise Exception("No NEON_DATABASE_URL or DATABASE_URL set")
    conn = psycopg2.connect(url, connect_timeout=8)
    conn.autocommit = True
    return conn


def _ensure_log_schema(cur):
    """welcome_email_log exists on prod; make this module standalone-safe and
    add the delivery-truth column. _pg_execute-style direct DDL (the safe_db
    wrapper would silently skip these — see db_utils traps)."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS welcome_email_log (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            plan TEXT,
            status TEXT NOT NULL,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE welcome_email_log "
                "ADD COLUMN IF NOT EXISTS resend_message_id TEXT")


def find_candidates(min_delay_minutes=5, lookback_hours=72, limit=10):
    """Founding-TIER conversions in the delay window that have no founder-note
    row yet. Sources BOTH welcome_email_log (plan='founding' rows are written
    by the key-email path on checkout and by the upgrade path) AND users
    (plan='founding' + recent plan_updated_at) so a conversion that somehow
    skipped email logging is still caught. users timestamps are TEXT-ish /
    inconsistent, so that side is filtered in Python (mirrors activation_nudge).
    """
    from datetime import datetime as DT, timedelta, timezone
    now = DT.now(timezone.utc)
    newest_ok = now - timedelta(minutes=min_delay_minutes)   # converted BEFORE this
    oldest_ok = now - timedelta(hours=lookback_hours)        # converted AFTER this

    out, seen = [], set()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        _ensure_log_schema(cur)
        # Source 1: welcome_email_log founding rows (attempted_at is real timestamptz)
        cur.execute("""
            SELECT lower(w.email), MIN(w.attempted_at)
              FROM welcome_email_log w
             WHERE w.plan = 'founding'
               AND w.attempted_at > %s AND w.attempted_at < %s
               AND NOT EXISTS (
                   SELECT 1 FROM welcome_email_log d
                    WHERE lower(d.email) = lower(w.email) AND d.plan = %s)
               -- ★★★2026-08-28: DEFER TO THE COHORT WELCOME.
               -- Both this note and founding:cohort_welcome are personal
               -- notes from Jonathan, and BOTH audiences are the founding
               -- cohort, so every founding customer was getting two. The
               -- cohort welcome is strictly richer (position, the 15-min
               -- founder call, the /cited-by consent link), so it wins and
               -- this note becomes the fallback for anyone the cohort lane
               -- will not reach.
               -- 'new'/'auto-tagged' means a cohort welcome is still QUEUED
               -- for them on the 09/21 UTC sweep, so skipping here is not a
               -- silent drop -- they are about to get the better one.
               AND NOT EXISTS (
                   SELECT 1 FROM founding_customers fc
                    WHERE lower(fc.email) = lower(w.email)
                      AND COALESCE(fc.contact_status, 'new')
                          IN ('new', 'auto-tagged', 'welcomed'))
             GROUP BY lower(w.email)
             ORDER BY MIN(w.attempted_at) ASC
             LIMIT 50
        """, (oldest_ok, newest_ok, PLAN_KEY))
        for email, converted_at in cur.fetchall():
            if email and email not in seen:
                seen.add(email)
                out.append({'email': email, 'source': 'welcome_email_log',
                            'converted_at': converted_at.isoformat()})
        # Source 2: users on the founding plan (plan_updated_at parsed in Python)
        cur.execute("""
            SELECT lower(u.email), u.plan_updated_at::text
              FROM users u
             WHERE u.plan = 'founding'
               AND u.email IS NOT NULL AND u.email <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM welcome_email_log d
                    WHERE lower(d.email) = lower(u.email) AND d.plan = %s)
               -- ★★★2026-08-28: DEFER TO THE COHORT WELCOME.
               -- Both this note and founding:cohort_welcome are personal
               -- notes from Jonathan, and BOTH audiences are the founding
               -- cohort, so every founding customer was getting two. The
               -- cohort welcome is strictly richer (position, the 15-min
               -- founder call, the /cited-by consent link), so it wins and
               -- this note becomes the fallback for anyone the cohort lane
               -- will not reach.
               -- 'new'/'auto-tagged' means a cohort welcome is still QUEUED
               -- for them on the 09/21 UTC sweep, so skipping here is not a
               -- silent drop -- they are about to get the better one.
               AND NOT EXISTS (
                   SELECT 1 FROM founding_customers fc
                    WHERE lower(fc.email) = lower(u.email)
                      AND COALESCE(fc.contact_status, 'new')
                          IN ('new', 'auto-tagged', 'welcomed'))
             LIMIT 200
        """, (PLAN_KEY,))
        for email, updated_raw in cur.fetchall():
            if not email or email in seen:
                continue
            try:
                udt = DT.fromisoformat(str(updated_raw).replace('Z', '').strip())
                if udt.tzinfo is None:
                    udt = udt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if oldest_ok < udt < newest_ok:
                seen.add(email)
                out.append({'email': email, 'source': 'users.plan_updated_at',
                            'converted_at': udt.isoformat()})
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out[:limit]


def _echoes_the_localpart(first, localpart):
    """True when a single-token 'name' is really just the address again.

    Compares verbatim AND with separators/digits stripped, so a localpart of
    `m.gelshteyn` stored as `mgelshteyn` is still caught.
    """
    a, b = first.lower(), localpart.lower()
    if a == b:
        return True
    strip = lambda s: "".join(ch for ch in s if ch not in ".-_+0123456789")
    return strip(a) == strip(b)


def first_name_for(email, fallback='there'):
    """Best-effort first name from the Stripe customer name (stored as
    users.name at provisioning). Fail-soft to `fallback`.

    ★2026-08-28 (follow-up to #3266): `fallback=None` lets a caller drop the
    name from its copy entirely. "Hi there," reads; "Heads up, there — you're
    close to today's limit" does not, so usage_limit_emails needs the None.

    ★2026-08-28: PROMOTED to the canonical greeting helper for every DC Hub
    customer email. routes/founding_customers.py carried its own derivation —
    `email.split("@")[0].split(".")[0].title()` — which greeted founding
    customer #18 as "Hi Mgelshteyn," and tj@karklins.com as "Hi Tj,". Worse
    than robotic: on #18 the Stripe cardholder name and the account email are
    different people, so a name guessed from the localpart can address the
    wrong person entirely. There is now ONE implementation and both founding
    emails call it — a second copy is how these two drift apart again.
    """
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM users WHERE lower(email)=lower(%s) "
                        "AND COALESCE(name,'')<>'' LIMIT 1", (email,))
            row = cur.fetchone()
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if row and row[0]:
            parts = str(row[0]).strip().split()
            first = parts[0] if parts else ''
            # An email-localpart "name" (no real Stripe name) reads robotic —
            # prefer the neutral greeting over "Hi motifs-buckles0j,".
            #
            # ★2026-08-28: the echo check now applies ONLY to a SINGLE-token
            # name. #3266 compared unconditionally, which discarded REAL names
            # whose first name happens to equal the localpart — alexander@,
            # rob@, jim@ are ordinary address shapes. Verified against the live
            # cohort: alexander@ryex.net stores "Alexander Ting" and was being
            # greeted "Hi there". A surname is the proof the value came from a
            # real name field, not from provisioning storing the address
            # (main.py: display_name = customer_name or localpart).
            #
            # A lone initial ("M" from "M Gelshteyn") is not a greeting either.
            if (first and '@' not in first and len(first) > 1
                    and not (len(parts) == 1
                             and _echoes_the_localpart(first, email.split('@')[0]))):
                return first
    except Exception:
        pass
    return fallback



# Historical private name — kept so existing call sites/tests keep working.
_first_name = first_name_for


def _reserve(email):
    """Atomically claim the send for this email. Returns the reserved row id,
    or None if a founder-note row (sent, seeded, or in-flight) already exists.
    Single INSERT..WHERE NOT EXISTS on an autocommit conn = the timer and the
    cron lane can race safely."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        _ensure_log_schema(cur)
        cur.execute("""
            INSERT INTO welcome_email_log (email, plan, status)
            SELECT %s, %s, 'sending'
             WHERE NOT EXISTS (
                   SELECT 1 FROM welcome_email_log
                    WHERE lower(email) = lower(%s) AND plan = %s)
            RETURNING id
        """, (email, PLAN_KEY, email, PLAN_KEY))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _finalize(row_id, status, resend_message_id=None):
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE welcome_email_log SET status = %s, "
                        "resend_message_id = %s WHERE id = %s",
                        (status, resend_message_id, row_id))
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("founder_note: finalize failed for row %s: %s", row_id, e)


def _send_note(email, first_name):
    """Plain-text Resend send from the founder. Returns the Resend message id
    (truthy str) on success, None on failure. Never raises."""
    rk = (os.environ.get('DCHUB_RESEND_API_KEY') or
          os.environ.get('RESEND_API_KEY') or '').strip()
    if not rk:
        logger.warning("founder_note: no Resend key; send skipped")
        return None
    payload = json.dumps({
        "from": "Jonathan Martone <jonathan@dchub.cloud>",
        "to": [email],
        "reply_to": "jonathan@dchub.cloud",
        "subject": SUBJECT,
        "text": NOTE_TEMPLATE.format(first_name=first_name),
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload, method="POST",
            headers={"Authorization": "Bearer " + rk,
                     "Content-Type": "application/json",
                     # CF fronts api.resend.com and 403s urllib's default UA
                     "User-Agent": "DCHub-Mailer/1.0 (+https://dchub.cloud)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            mid = (json.loads(body) or {}).get("id")
        except Exception:
            mid = None
        logger.info("founder_note: sent to %s (resend id=%s)", email, mid)
        return mid or "sent-no-id"
    except Exception as e:
        logger.warning("founder_note: send failed for %s: %s", email, str(e)[:160])
        return None


def run_founder_note(armed=False, limit=10, min_delay_minutes=None,
                     lookback_hours=None):
    """Find founding conversions past the 5-min delay and (if armed) send the
    founder note. Returns a report dict. Never raises."""
    if _disabled():
        return {'ok': True, 'disabled': True,
                'note': 'FOUNDER_NOTE_DISABLE=1 — no scan, no sends.'}
    try:
        min_delay_minutes = int(min_delay_minutes
                                or os.environ.get('FOUNDER_NOTE_MIN_DELAY_MIN', 5))
        lookback_hours = int(lookback_hours
                             or os.environ.get('FOUNDER_NOTE_LOOKBACK_HOURS', 72))
    except Exception:
        min_delay_minutes, lookback_hours = 5, 72

    try:
        from dchub_outreach import is_internal_email
    except Exception:
        def is_internal_email(e):
            return (not e) or '@dchub.cloud' in (e or '').lower()

    try:
        cands = find_candidates(min_delay_minutes, lookback_hours, limit * 2)
    except Exception as e:
        logger.warning("founder_note: candidate query failed: %s", e)
        return {'ok': False, 'error': str(e)[:200]}

    cands = [c for c in cands if not is_internal_email(c.get('email'))][:limit]
    result = {'ok': True, 'armed': bool(armed),
              'window': f'{min_delay_minutes}m..{lookback_hours}h',
              'candidates': len(cands), 'sent': 0, 'skipped_dedupe': 0,
              'errors': 0, 'preview': cands}
    if not armed:
        result['note'] = ('DRY-RUN — no emails sent. These founding conversions '
                          'WOULD get the founder note. Arm with ?confirm=1 '
                          '(the cron lane passes it).')
        return result

    for c in cands:
        email = c['email']
        row_id = None
        try:
            row_id = _reserve(email)
        except Exception as e:
            logger.warning("founder_note: reserve failed for %s: %s", email, e)
            result['errors'] += 1
            continue
        if row_id is None:
            result['skipped_dedupe'] += 1
            continue
        mid = _send_note(email, _first_name(email))
        if mid:
            _finalize(row_id, 'sent', None if mid == 'sent-no-id' else mid)
            result['sent'] += 1
        else:
            # Leave the reservation row as the dedupe marker but record the
            # failure; the owner can resend by deleting the row.
            _finalize(row_id, 'send_failed')
            result['errors'] += 1
    return result


def schedule_founder_note_after_conversion(email):
    """Called from the Stripe webhook when a founding-tier conversion lands.
    Schedules ONE in-process sweep at a random 5-15 min delay so the note
    arrives fast without looking machine-fired. The cron lane is the backstop
    if a deploy restart kills this timer. Never raises."""
    try:
        if _disabled():
            return
        import threading
        import random
        delay = random.uniform(5 * 60 + 30, 15 * 60 - 30)
        t = threading.Timer(delay, lambda: run_founder_note(armed=True))
        t.daemon = True
        t.start()
        logger.info("founder_note: sweep scheduled in %.0fs after founding "
                    "conversion (%s)", delay, email)
    except Exception as e:
        logger.warning("founder_note: timer schedule failed: %s", e)


def setup_founder_note_routes(app):
    """Register the admin/cron trigger route."""
    from flask import request, jsonify

    @app.route('/api/v1/admin/founder-note/run', methods=['POST', 'GET'])
    def _admin_founder_note():
        provided = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
        if provided != os.environ.get('DCHUB_ADMIN_KEY', ''):
            return jsonify({'error': 'unauthorized', 'hint': 'X-Admin-Key required'}), 401
        armed = request.args.get('confirm') == '1'
        try:
            limit = max(1, min(int(request.args.get('limit', 10)), 50))
        except Exception:
            limit = 10
        return jsonify(run_founder_note(armed=armed, limit=limit))
