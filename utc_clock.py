"""One UTC clock, one wire shape.

★ WHY THIS EXISTS. `datetime.utcnow()` is naive and deprecated (removal
scheduled), but the reflex fix is wrong for a SERIALIZED timestamp:

    naive  utcnow().isoformat() + "Z"   -> 2026-09-05T04:24:58.917155Z
    aware  now(utc).isoformat() + "Z"   -> ...917282+00:00Z      <- malformed

Measured across this repo on 2026-09-05, by AST: 347 call sites carry that
exact `+ "Z"` shape and 368 more emit a bare `.isoformat()` that would silently
GAIN a `+00:00` offset. A find-and-replace corrupts those serialized timestamps
into API responses and DB rows — a far larger blast radius than the ~90
naive/aware COMPARISON sites the deprecation notice draws attention to.

So the clock is aware and the wire shape is pinned back to `Z`. Output is
byte-identical to what `utcnow().isoformat() + "Z"` produced.

★ `Z` IS CANONICAL BY DECISION, not by accident: it is the majority shape here.
routes/ops_activation.py and routes/ops_claims.py already serve `+00:00` from
their own aware helpers — measured live at /api/v1/ops/install-stats,
"2026-09-01T03:39:34.221213+00:00". That is drift to reconcile in a later
batch, not the standard to copy.

★ WHY A SHARED MODULE rather than a helper per file. The call sites use three
different datetime import styles — `import datetime`, `import datetime as _dt`,
and `from datetime import datetime` — so a local fix has to be written three
ways and reviewed three ways. One import normalises all of them, and the wire
shape then has exactly one place to be wrong.
"""
from datetime import datetime, timezone

__all__ = ["utc_iso_z", "utc_now"]


def utc_now() -> datetime:
    """Timezone-aware UTC now. Compare this only against other AWARE datetimes."""
    return datetime.now(timezone.utc)


def utc_iso_z() -> str:
    """UTC now as ISO-8601 ending in `Z` — the canonical wire shape."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
