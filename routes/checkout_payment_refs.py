"""checkout_payment_refs.py — keep the client_reference_id of every paid checkout.

2026-09-14 (r-paid-join). The handoff funnel's last stage, `paid_attributed`,
counted a payment only when the webhook had bound it to an MCP session:
`mcp_session_upgrades` (Fix E, a bare session ref) or
`mcp_topups.mcp_session_id` (the pack grant). A caller holding an API key is
sold through a KEY ref instead, `pk-<sha256>` for the $10 pack and
`k-<sha256>` for a subscription, and neither key branch writes a session: the
pack grant passes mcp_session_id None, and the k- branch stamps
mcp_dev_keys.tier and nothing else. So a keyed caller's purchase from an agent
unlock could never move paid_attributed off 0.

The session is not lost. The link that sold it is a signed
/go/c/<plan|ref|sid> token, and routes/checkout_click_tracker stores that ref
and that session in mcp_checkout_clicks before it redirects to Stripe with the
same ref as client_reference_id. The missing half was the payment side: no
table kept a completed checkout's client_reference_id where a query could join
it (conversion_attribution stores one only when a tool name can be read out of
it).

This module stores it, one row per paid Checkout Session. The row is raw
facts; the join and its rules live in routes/handoff_definition, the one
writer of the funnel's definitions:

    mcp_checkout_payments.client_reference_id = mcp_checkout_clicks.ref

WRITE RULES
  * payment_status 'paid' only. A completed session that took no money (a
    100% coupon, a free trial) or has not settled yet (an async method still
    'unpaid') is not a payment.
  * Idempotent on the Checkout Session id. Stripe re-delivers, and more than
    one webhook endpoint receives checkout.session.completed.
  * Never raises. It runs inside the live payment webhook, beside provisioning
    it must never break.
"""
from __future__ import annotations

import os

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover - the light CI install has it
    _pg = None

from routes.checkout_click_tracker import _REF_OK, _ref_kind
from routes._swallowed_writes import note_swallowed_write

TABLE = "mcp_checkout_payments"

_SCHEMA_READY = [False]

# Asked of the catalog before any DDL runs: CREATE INDEX IF NOT EXISTS takes a
# lock on the table even when the index exists, and this table is written
# inside the payment webhook.
_EXISTS_SQL = "SELECT to_regclass('mcp_checkout_payments') IS NOT NULL"

_DDL = """
CREATE TABLE IF NOT EXISTS mcp_checkout_payments (
    id                  BIGSERIAL PRIMARY KEY,
    stripe_session_id   TEXT NOT NULL UNIQUE,
    client_reference_id TEXT NOT NULL,
    ref_kind            TEXT,
    mode                TEXT,
    amount_subtotal     INTEGER,
    amount_total        INTEGER,
    currency            TEXT,
    livemode            BOOLEAN,
    paid_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mcp_checkout_payments_ref
    ON mcp_checkout_payments (client_reference_id, paid_at DESC);
CREATE INDEX IF NOT EXISTS ix_mcp_checkout_payments_paid
    ON mcp_checkout_payments (paid_at DESC);
"""

# client_reference_id shapes the webhooks already special-case, labelled so a
# reader of the table can tell a key ref from a pair code. The label is
# informational: the join reads the click's identity, never this column.
_PREFIX_KINDS = (
    ("mcp:", "mcp_ref"),
    ("web__", "web"),
    ("ref_", "legacy_ref"),
    ("dcm-", "pair_code"),
    ("tu-", "topup_token"),
)


def _dsn() -> str:
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "").strip()


def payment_ref_kind(cref: str) -> str:
    """pack_key | sub_key | anon | session | mcp_ref | web | legacy_ref |
    pair_code | topup_token | other.

    The /go/c/ shapes are classified by routes/checkout_click_tracker._ref_kind,
    the same function that labels the click, so a payment and the click that
    sold it can never be labelled two different ways.
    """
    s = (cref or "").strip()
    low = s.lower()
    for prefix, kind in _PREFIX_KINDS:
        if low.startswith(prefix):
            return kind
    if s and _REF_OK.match(s):
        return _ref_kind(s)
    return "other"


def ensure_schema() -> bool:
    """True once the table exists. Never raises; retries until it does."""
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
    except Exception:
        note_swallowed_write(TABLE, where="checkout_payment_refs.ensure_schema")
    return _SCHEMA_READY[0]


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _text(v, n: int):
    s = str(v or "").strip()
    return s[:n] or None


def record_checkout_payment(session) -> dict:
    """Store one paid Checkout Session's client_reference_id. Never raises.

    `session` is checkout.session.completed's data.object. Returns
    {"ok": bool, "skipped": reason} or {"ok": True, "idempotent": bool,
    "ref_kind": kind}.
    """
    s = session if isinstance(session, dict) else {}
    stripe_session_id = str(s.get("id") or "").strip()
    cref = str(s.get("client_reference_id") or "").strip()
    if not stripe_session_id or not cref:
        return {"ok": False, "skipped": "no_session_or_ref"}
    if str(s.get("payment_status") or "").strip().lower() != "paid":
        return {"ok": False, "skipped": "not_paid"}
    if not ensure_schema():
        return {"ok": False, "skipped": "no_schema"}
    kind = payment_ref_kind(cref)
    livemode = s.get("livemode")
    try:
        c = _pg.connect(_dsn(), connect_timeout=8)
        try:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO mcp_checkout_payments
                         (stripe_session_id, client_reference_id, ref_kind, mode,
                          amount_subtotal, amount_total, currency, livemode)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (stripe_session_id) DO NOTHING
                       RETURNING id""",
                    (stripe_session_id[:255], cref[:300], kind,
                     _text(s.get("mode"), 20), _int(s.get("amount_subtotal")),
                     _int(s.get("amount_total")), _text(s.get("currency"), 10),
                     livemode if isinstance(livemode, bool) else None))
                inserted = cur.fetchone() is not None
        finally:
            c.close()
        return {"ok": True, "idempotent": not inserted, "ref_kind": kind}
    except Exception:
        note_swallowed_write(TABLE, where="checkout_payment_refs.record_checkout_payment")
        return {"ok": False, "skipped": "write_failed"}
