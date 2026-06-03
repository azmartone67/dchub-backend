"""mcp_signal_canonical.py — THE only path that writes mcp_upgrade_signals,
AND THE only path that flips signals.converted_at after Stripe fires.

Collapses 4 divergent writers + 2 divergent webhook handlers into one helper
each. Every consumer (funnel diag, visitor intel, lost-conversion outreach,
brain consistency radar) reads from VIEW mcp_funnel_canonical / mcp_funnel_real
/ mcp_funnel_callers — the synthetic-client filter is in the SQL view, NOT
repeated in 5 Python constants.
"""
import os
import hashlib
from contextlib import contextmanager

try:
    import psycopg2
except Exception:
    psycopg2 = None

DSN = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = psycopg2.connect(DSN, connect_timeout=5)
    try:
        yield c
    finally:
        try: c.close()
        except Exception: pass


def _compute_caller_id(*, user_email=None, session_id=None, mcp_client=None,
                       user_agent=None, ip_address=None) -> str:
    """ONE definition of caller identity. Mirrors the SQL UPDATE in schema_repair
    that backfills mcp_upgrade_signals.caller_id. Order of preference:
      1. lowercased email (identified user)
      2. session_id (MCP session — persists across calls)
      3. anon:md5(mcp_client|ua_first_120|ip)  (best-effort anon fingerprint)
    Keep this in lock-step with the SQL backfill or join joins fail.
    """
    if user_email:
        e = user_email.strip().lower()
        if e and '@' in e:
            return e
    if session_id:
        s = session_id.strip()
        if s:
            return s
    seed = (str(mcp_client or '').lower() + '|' +
            (str(user_agent or '')[:120]) + '|' +
            str(ip_address or ''))
    return 'anon:' + hashlib.md5(seed.encode('utf-8')).hexdigest()[:24]


def record_signal(*, signal_type, tool_requested=None, tier_current='free',
                  tier_required='paid', message_shown=None, mcp_client=None,
                  user_agent=None, daily_usage=None, daily_limit=None,
                  session_id=None, user_email=None, ip_address=None,
                  api_key=None):
    """THE only function that writes mcp_upgrade_signals.
    Replaces fire_upgrade_signal (mcp_upgrade_gate.py), log_upgrade_signal
    (mcp_analytics_postgres.py), and the 2 ad-hoc INSERTs in pair_code.py.

    Synthetic-traffic gating happens at the READ layer via the
    mcp_funnel_canonical.is_synthetic view column — we INSERT everything
    so we keep audit trail, but no reader counts synthetic rows.
    """
    if psycopg2 is None or not DSN:
        return None
    # Email resolution from api_key if caller didn't pass one.
    if not user_email and api_key and api_key.startswith('dchub_'):
        try:
            with _conn() as c, c.cursor() as cur:
                kh = hashlib.sha256(api_key.encode()).hexdigest()
                cur.execute(
                    """SELECT u.email FROM api_keys ak
                         JOIN users u ON ak.user_id = u.id
                        WHERE ak.key_hash = %s
                          AND COALESCE(ak.is_active,1) = 1 LIMIT 1""", (kh,))
                r = cur.fetchone()
                if r and r[0]: user_email = r[0]
        except Exception: pass
    caller_id = _compute_caller_id(
        user_email=user_email, session_id=session_id, mcp_client=mcp_client,
        user_agent=user_agent, ip_address=ip_address)
    try:
        with _conn() as c, c.cursor() as cur:
            # Hourly dedup per (caller_id, signal_type) so a thrashing agent
            # doesn't inflate the count — replaces the old session_id-only dedup
            # in mcp_analytics_postgres.log_upgrade_signal that missed anon rows.
            cur.execute("""SELECT 1 FROM mcp_upgrade_signals
                            WHERE caller_id = %s AND signal_type = %s
                              AND created_at > NOW() - INTERVAL '1 hour' LIMIT 1""",
                        (caller_id, signal_type))
            if cur.fetchone():
                c.commit()
                return None
            cur.execute("""INSERT INTO mcp_upgrade_signals
                            (session_id, user_email, ip_address, signal_type, tool_requested,
                             tier_current, tier_required, daily_usage, daily_limit,
                             message_shown, mcp_client, user_agent, caller_id, created_at)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                          RETURNING id""",
                        (session_id, user_email, ip_address, signal_type, tool_requested,
                         tier_current, tier_required, daily_usage, daily_limit,
                         message_shown, mcp_client, user_agent, caller_id))
            sid = cur.fetchone()[0]
            c.commit()
            return sid
    except Exception as e:
        print(f"[mcp_signal_canonical] record_signal failed: {e}")
        return None


def mark_signals_converted(*, email=None, stripe_customer_id=None,
                            session_id=None, mcp_session=None, conv_id=None):
    """THE only path that flips signals.converted_at after Stripe fires.
    Updates by ANY matching key: email OR session_id OR resolved-from-customer.
    Replaces the divergent UPDATE in main.py:29363 (email-only) and the
    missing UPDATE in flask_mcp_endpoints.py:1500 (didn't write back at all).

    Returns dict of rows-affected per match-path so the webhook can log
    which path attributed.
    """
    if psycopg2 is None or not DSN:
        return {'total': 0, 'reason': 'no_db'}
    matched = {'by_email': 0, 'by_session': 0, 'by_stripe_customer': 0, 'by_caller_id': 0}
    e = email.strip().lower() if email else None
    try:
        with _conn() as c, c.cursor() as cur:
            # Path 1: email match (identified signals)
            if e:
                cur.execute("""UPDATE mcp_upgrade_signals
                                  SET converted = TRUE, converted_at = COALESCE(converted_at, NOW())
                                WHERE LOWER(TRIM(user_email)) = %s
                                  AND COALESCE(converted, false) = false""", (e,))
                matched['by_email'] = cur.rowcount or 0
                # Path 4: anon signals whose caller_id == email (typical when
                # they later authenticated via the same caller_id key).
                cur.execute("""UPDATE mcp_upgrade_signals
                                  SET converted = TRUE, converted_at = COALESCE(converted_at, NOW())
                                WHERE caller_id = %s
                                  AND COALESCE(converted, false) = false""", (e,))
                matched['by_caller_id'] = cur.rowcount or 0
            # Path 2: session match (anon MCP session that hit paywall, then later upgraded)
            sid = session_id or mcp_session
            if sid:
                cur.execute("""UPDATE mcp_upgrade_signals
                                  SET converted = TRUE, converted_at = COALESCE(converted_at, NOW()),
                                      user_email = COALESCE(user_email, %s)
                                WHERE session_id = %s
                                  AND COALESCE(converted, false) = false""", (e, sid))
                matched['by_session'] = cur.rowcount or 0
            # Path 3: resolve email via stripe_customer_id → mcp_conversions/users
            if stripe_customer_id and not e:
                cur.execute("""SELECT LOWER(user_email) FROM mcp_conversions
                                WHERE stripe_customer_id = %s LIMIT 1""", (stripe_customer_id,))
                r = cur.fetchone()
                if r and r[0]:
                    cur.execute("""UPDATE mcp_upgrade_signals
                                      SET converted = TRUE, converted_at = COALESCE(converted_at, NOW())
                                    WHERE LOWER(TRIM(user_email)) = %s
                                      AND COALESCE(converted, false) = false""", (r[0],))
                    matched['by_stripe_customer'] = cur.rowcount or 0
            c.commit()
    except Exception as ex:
        return {'total': 0, 'error': str(ex)[:200]}
    matched['total'] = sum(v for k,v in matched.items() if k != 'total')
    return matched
