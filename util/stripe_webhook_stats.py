"""util/stripe_webhook_stats.py — PERSISTED Stripe webhook receipt counters.

★ WHY (QA sweep 2026-09-02, finding 5:4e). /api/v1/stripe/webhook-diagnostics
published `stats.received_total: 0, last_received_at: null` at 00:38Z while
the conversions-audit on the same host said Stripe 4 <-> DB 4. The counter
is `_STRIPE_WEBHOOK_STATS`, a module-level dict in main.py that starts at 0
on every deploy; this backend deploys several times a day, so the counter
reads "webhook dead" for most of its life and the endpoint's own
recommendation ("fire a test event, received_total should increment") sends
the reader to chase a fault that does not exist.

The durable record already exists: `stripe_webhook_events` (main.py
_stripe_event_already_processed) inserts one row per VERIFIED event id — it
is the idempotency gate, written before any handler runs, so it survives
deploys and counts exactly what a persisted receipt counter should.

This module reads it through an injected executor (main._pg_execute's shape:
execute(sql, params, fetch=True) -> (rowcount, rows)) so it is testable
without importing main. Nothing here writes.
"""
from __future__ import annotations

TABLE = "stripe_webhook_events"

PERSISTED_SQL = (
    "SELECT COUNT(*), "
    "       COUNT(*) FILTER (WHERE processed_at >= NOW() - INTERVAL '24 hours'), "
    "       COUNT(*) FILTER (WHERE processed_at >= NOW() - INTERVAL '7 days'), "
    "       MAX(processed_at) "
    f"  FROM {TABLE}"
)

NOTE = (
    "received_total / verified_total / last_received_at are IN-MEMORY and "
    "reset to 0 on every deploy — they answer 'since this process started', "
    "not 'is Stripe reaching us'. Read the *_persisted fields for that: they "
    f"count rows of {TABLE}, the idempotency ledger written once per VERIFIED "
    "event id, which survives deploys. persisted_total=null means the read "
    "itself failed (table absent or DB down), never 0."
)


def persisted_stats(execute) -> dict:
    """{persisted_total, persisted_24h, persisted_7d, last_persisted_at,
    persisted_source, persisted_error?}. Fail-soft: every count is None (not
    0) when the read fails, and the error is named."""
    out = {"persisted_source": TABLE, "persisted_basis":
           "one row per verified Stripe event id (the idempotency gate)",
           "persisted_total": None, "persisted_24h": None, "persisted_7d": None,
           "last_persisted_at": None}
    try:
        _, rows = execute(PERSISTED_SQL, (), fetch=True)
        r = (rows or [None])[0]
        if r:
            out["persisted_total"] = int(r[0] or 0)
            out["persisted_24h"] = int(r[1] or 0)
            out["persisted_7d"] = int(r[2] or 0)
            ts = r[3]
            out["last_persisted_at"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
    except Exception as e:  # noqa: BLE001
        out["persisted_error"] = f"{type(e).__name__}:{str(e)[:100]}"
    return out
