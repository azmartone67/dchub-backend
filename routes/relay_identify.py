"""routes/relay_identify.py — the IDENTIFY rung: an email, bound to the MCP session.

WHAT THIS EXISTS TO MOVE
========================
Measured live 2026-09-17, /api/v1/mcp/handoff-funnel:

    30d  paywall_hit 941 → high_intent 309 → relay_minted 307
         → human_acted 7 → identified 0 → paid_attributed 0
     7d  150 → 74 → 74 → 7 → 0 → 0

`human_acted` moves. `identified` does not, and it is not close: it has been
0 in every window since the stage existed. The stage counts
`mcp_high_intent_sessions.claim_email`, and the only writers of that column
are the /claim page form and the agent-facing `bind_email` endpoint
(flask_mcp_endpoints.identify_key, which side-writes it). NOTHING on the path
a human actually walks — open the relayed link, decide, pay — ever asked for
an email. The rung could not move because nothing was wired to it.

That is also why `paid_attributed` can stay 0 while Stripe shows money: a
purchase is joined back to an MCP session through the /go/c/ click that sold
it, and if the buyer is never identified, the only thing tying the payment to
the agent session is that click. Miss it and the payment is orphaned — real
revenue, invisible to the funnel.

WHAT SHIPS HERE
===============
One capture function, two callers:

  1. POST /upgrade/h/<token>  (routes/human_relay) — the human types an email
     on the relay page before continuing to checkout. The session comes from
     the SIGNED token, never from the form, so a capture cannot stamp a
     session the caller does not hold a mint for.

  2. checkout.session.completed (main.py, beside record_checkout_payment) —
     the buyer's Stripe email, stamped onto the session resolved by the SAME
     join `paid_attributed` uses. This is the one that makes the last two
     rungs consistent: any payment that can reach paid_attributed can now
     also reach identified, so `paid > identified` stops being possible.

WHAT IT WRITES
  * relay_identify_captures — raw facts, one row per capture, append-only.
    Kept because the rung is a SUBSET of what this captures: an email from a
    session with no mcp_high_intent_sessions row is a real lead the funnel
    stage structurally cannot count, and a capture that cannot reach the rung
    must be visible rather than silently dropped. `stamped_high_intent`
    records which it was.
  * mcp_high_intent_sessions.claim_email — the rung itself. Never overwrites
    an email already there, and never INSERTs: fabricating a high-intent row
    would inflate the stage ABOVE this one, which is the denominator.
  * mcp_upgrade_signals.user_email — makes the lead reachable by
    lost_conversion_outreach. The same pair of side-writes
    flask_mcp_endpoints.identify_key already does, for the same reason.

Never raises: both callers are a live payment surface.
Kill switch: DCHUB_RELAY_IDENTIFY_DISABLE=1 (the page still renders and still
sells; only the capture stops).
"""
from __future__ import annotations

import os
import re

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover - the light CI install has it
    _pg = None

from routes._swallowed_writes import note_swallowed_write

TABLE = "relay_identify_captures"

# The sources a capture can carry. A value outside this set is stored as
# 'other' rather than rejected — losing the email to protect a label would be
# the wrong trade.
SOURCES = ("relay_page", "checkout", "go_click")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

_SCHEMA_READY = [False]

_EXISTS_SQL = "SELECT to_regclass('relay_identify_captures') IS NOT NULL"

_DDL = """
CREATE TABLE IF NOT EXISTS relay_identify_captures (
    id                    BIGSERIAL PRIMARY KEY,
    mcp_session_id        TEXT NOT NULL,
    email                 TEXT NOT NULL,
    source                TEXT NOT NULL,
    tool                  TEXT,
    stamped_high_intent   BOOLEAN NOT NULL DEFAULT FALSE,
    stripe_session_id     TEXT,
    captured_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_relay_identify_captures_at
    ON relay_identify_captures (captured_at DESC);
CREATE INDEX IF NOT EXISTS ix_relay_identify_captures_session
    ON relay_identify_captures (mcp_session_id, captured_at DESC);
-- Stripe re-delivers checkout.session.completed, and more than one endpoint
-- receives it, so a checkout capture must be idempotent on the Checkout
-- Session id — the same rule and the same reason as mcp_checkout_payments.
-- PARTIAL on purpose: a relay-page capture carries no Stripe session, and two
-- submits from one human are two real captures, not a conflict to swallow.
CREATE UNIQUE INDEX IF NOT EXISTS ux_relay_identify_captures_stripe
    ON relay_identify_captures (stripe_session_id)
    WHERE stripe_session_id IS NOT NULL;
"""


def _disabled() -> bool:
    return (os.environ.get("DCHUB_RELAY_IDENTIFY_DISABLE") or "").strip() == "1"


def _dsn() -> str:
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "").strip()


def normalize_email(email) -> str:
    """Lowercased and trimmed, or '' when it is not an address.

    Cheap shape check only. The deliverability gate below is a soft filter on
    top of it, exactly as flask_mcp_endpoints.identify_key applies it: a
    rejected address must never cost us the capture of a good one.
    """
    s = str(email or "").strip().lower()
    if not s or len(s) > 254 or not _EMAIL_RE.match(s):
        return ""
    return s


def deliverable(email: str) -> bool:
    """Soft deliverability gate. Absent validator or any throw → True.

    Role accounts / disposable domains / domains with no MX are not worth
    binding to a session, but this check failing CLOSED would silently kill
    the rung it exists to serve.
    """
    try:
        from routes.email_validation import validate_email
        ok, _reason, _norm = validate_email(email)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return True


def ensure_schema() -> bool:
    """True once the table exists. Never raises; retries until it does.

    Asked of the catalog before any DDL runs, and on its OWN autocommit
    connection: CREATE INDEX IF NOT EXISTS takes a lock even when the index
    already exists, and DDL sent through the pooled request cursor does not
    run at all.
    """
    if _SCHEMA_READY[0]:
        return True
    if not (_pg and _dsn()):
        return False
    try:
        c = _pg.connect(_dsn(), connect_timeout=8)
        try:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(_EXISTS_SQL)
                if not cur.fetchone()[0]:
                    cur.execute(_DDL)
                    cur.execute(_EXISTS_SQL)
                    _SCHEMA_READY[0] = bool(cur.fetchone()[0])
                else:
                    _SCHEMA_READY[0] = True
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        note_swallowed_write(TABLE, where="relay_identify.ensure_schema")
    return _SCHEMA_READY[0]


def capture(sid: str, email: str, source: str = "relay_page", *,
            tool: str = "", stripe_session_id: str = "") -> dict:
    """Bind `email` to MCP session `sid`. Never raises.

    Returns {"ok": bool, "skipped": reason} or
    {"ok": True, "stamped_high_intent": bool, "stamped_signal": bool}.

    `stamped_high_intent` is the honest answer to "did this move the rung":
    False means the email was captured but the session has no
    mcp_high_intent_sessions row, so `identified` cannot count it. It is read
    back from rowcount, not assumed from the write succeeding.
    """
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    sid = str(sid or "").strip()
    if not sid or sid == "no-session":
        return {"ok": False, "skipped": "no_session"}
    email = normalize_email(email)
    if not email:
        return {"ok": False, "skipped": "invalid_email"}
    if not deliverable(email):
        return {"ok": False, "skipped": "undeliverable_email"}
    if source not in SOURCES:
        source = "other"
    if not ensure_schema():
        return {"ok": False, "skipped": "no_schema"}

    stamped_hi = False
    stamped_sig = False
    idempotent = False
    try:
        c = _pg.connect(_dsn(), connect_timeout=8)
        try:
            c.autocommit = True
            with c.cursor() as cur:
                # The rung. Never overwrite, never insert — see the module
                # docstring on why fabricating the row would be worse than
                # not counting the capture.
                cur.execute(
                    "UPDATE mcp_high_intent_sessions SET claim_email = %s "
                    "WHERE mcp_session_id = %s "
                    "AND (claim_email IS NULL OR claim_email = '')",
                    (email, sid))
                stamped_hi = (cur.rowcount or 0) > 0
                # Reachability for lost_conversion_outreach. Marketing sends
                # stay gated by that module's explicit marketing_opt_in; this
                # only makes the lead findable.
                cur.execute(
                    "UPDATE mcp_upgrade_signals SET user_email = %s "
                    "WHERE session_id = %s "
                    "AND (user_email IS NULL OR user_email = '')",
                    (email, sid))
                stamped_sig = (cur.rowcount or 0) > 0
                cur.execute(
                    """INSERT INTO relay_identify_captures
                         (mcp_session_id, email, source, tool,
                          stamped_high_intent, stripe_session_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING
                       RETURNING id""",
                    (sid[:200], email[:254], source, (tool or "")[:80] or None,
                     stamped_hi, (stripe_session_id or "")[:255] or None))
                # Only the partial unique index above can fire this, i.e. a
                # re-delivered checkout webhook. Reported rather than
                # swallowed: a caller that cannot tell a first capture from a
                # replay cannot tell a quiet failure from one either.
                idempotent = cur.fetchone() is None
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        note_swallowed_write(TABLE, where="relay_identify.capture")
        return {"ok": False, "skipped": "write_failed"}
    return {"ok": True, "stamped_high_intent": stamped_hi,
            "stamped_signal": stamped_sig, "idempotent": idempotent}


def session_for_checkout_ref(cref: str) -> str:
    """The MCP session a paid checkout belongs to, or ''. Never raises.

    Resolved through routes.handoff_definition — the SAME lookup
    `paid_attributed` joins on — so a payment cannot be attributed to one
    session by the funnel and another by this module.
    """
    cref = str(cref or "").strip()
    if not cref or not (_pg and _dsn()):
        return ""
    try:
        from routes.handoff_definition import relayed_click_session_for_ref_sql
        sql = "SELECT " + relayed_click_session_for_ref_sql() + " AS sid"
        c = _pg.connect(_dsn(), connect_timeout=8)
        try:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(sql, (cref,))
                row = cur.fetchone()
        finally:
            c.close()
        return str((row or [None])[0] or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def capture_from_checkout(session) -> dict:
    """Stamp the buyer's Stripe email onto the session that bought. Never raises.

    `session` is checkout.session.completed's data.object. Paid sessions only
    — the same rule record_checkout_payment applies, and for the same reason:
    a completed session that took no money is not a customer.
    """
    s = session if isinstance(session, dict) else {}
    if str(s.get("payment_status") or "").strip().lower() != "paid":
        return {"ok": False, "skipped": "not_paid"}
    email = ((s.get("customer_details") or {}).get("email")
             or s.get("customer_email") or "")
    if not normalize_email(email):
        return {"ok": False, "skipped": "no_email"}
    cref = str(s.get("client_reference_id") or "").strip()
    if not cref:
        return {"ok": False, "skipped": "no_ref"}
    sid = session_for_checkout_ref(cref)
    if not sid:
        # The payment is real; no signed, real-UA, session-bearing click sold
        # it inside the lookback. paid_attributed cannot count it either, and
        # both stages say so the same way.
        return {"ok": False, "skipped": "no_relayed_click_session"}
    return capture(sid, email, "checkout",
                   stripe_session_id=str(s.get("id") or ""))


# ── read side ────────────────────────────────────────────────────────────
# Published beside the rung so a capture the stage cannot count is visible.
# One string literal with an uppercase FROM on purpose: scripts/
# dataset_inventory.py counts a table as READ only when it can see that shape,
# and a table this module only ever wrote would fail NEW_WRITE_ONLY.
def captures_count_sql(interval_sql: str) -> str:
    """captured / reached_the_rung over the window, one pass."""
    return ("SELECT count(*) AS captured,"
            " count(*) FILTER (WHERE r.stamped_high_intent) AS reached_the_rung,"
            " count(DISTINCT lower(r.email)) AS distinct_emails"
            " FROM relay_identify_captures r"
            " WHERE r.captured_at > now() - interval '" + interval_sql + "'")


CAPTURES_BASIS = (
    "Emails captured on the human path — the relay page's form and the paid "
    "checkout's Stripe email — written by routes/relay_identify. `captured` is "
    "every capture in the window; `reached_the_rung` is the subset whose MCP "
    "session had an mcp_high_intent_sessions row to stamp, which is the only "
    "subset `identified` can count. The two differ when a human opens a "
    "relayed link from a session that never entered the high-intent table: a "
    "real lead the stage structurally cannot show.")
