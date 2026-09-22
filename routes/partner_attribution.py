"""Pay links served to a declared partner's keyless callers carry a ref recorded
to that partner, so a checkout that starts there can be attributed to it.

WHY (2026-09-21). A hosted catalogue (partner_egress.py) relays our walls to its
customers' agents. Nothing tied a checkout to the partner: its keyless callers
got the same links as any anonymous caller, so a conversion that came through
the partner could not be told apart from any other.

WHAT. The source is derived server-side, from the limiter's own classification
of the request (rate_limiter.partner_of_request: keyless, from a declared
egress), never from anything the client sends. A ref served to such a request
is written to partner_offer_refs (ref -> partner). The ref is one the funnel
already understands, so no webhook or reader changes meaning:

  pair_code  the DCM- code the paywall builder already mints per anonymous
             caller and puts on its checkout links as client_reference_id
             (be#4872). The /pricing links get it as ?ref=, which /pricing
             forwards to /go/p and Stripe as ref_<code>__tool_<t>__ts_<n>.

A payment is the partner's when its client_reference_id is a recorded ref, or
/pricing's ref_<ref>__ wrapper around one (read_attribution).

SCOPE. Walls built per request only. A ref inside a payload that is cached and
shared (the worker's KV tier holds some keyless 200s) would reach callers who
are not the partner and credit them to it.

FAIL-OPEN. Records are buffered and flushed by the usage tracker's thread
(routes/api_usage_tracker._flush_loop). A database error loses a record, never
a response.
"""
from __future__ import annotations

import datetime as _dt
import re
import threading

PRICING_URL = "https://dchub.cloud/pricing"

# The charset /go/c and /go/p accept for a ref (checkout_click_tracker._REF_OK).
_REF_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_offer_refs (
    ref              TEXT        PRIMARY KEY,
    partner          TEXT        NOT NULL,
    kind             TEXT        NOT NULL,
    first_path       TEXT,
    first_served_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_served_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    served           BIGINT      NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_partner_offer_refs_partner
    ON partner_offer_refs (partner, last_served_at DESC);
"""

_UPSERT = """
INSERT INTO partner_offer_refs
       (ref, partner, kind, first_path, first_served_at, last_served_at, served)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ref) DO UPDATE
   SET last_served_at = GREATEST(partner_offer_refs.last_served_at,
                                 EXCLUDED.last_served_at),
       served         = partner_offer_refs.served + EXCLUDED.served
"""

_BUF_LOCK = threading.Lock()
_BUF: dict = {}          # ref -> [partner, kind, path, served, first_ts, last_ts]
_BUF_MAX = 5000
_DROPPED = [0]


def partner_of_request():
    """The partner prefix when this request is keyless from a declared egress."""
    try:
        from rate_limiter import partner_of_request as _p
        return _p()
    except Exception:  # noqa: BLE001
        return None


def note_offer_ref(ref, partner, kind, path) -> bool:
    """Buffer one serving of `ref` to `partner`. False when nothing was buffered."""
    if not (ref and partner and _REF_OK.match(ref)):
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    with _BUF_LOCK:
        row = _BUF.get(ref)
        if row is None:
            if len(_BUF) >= _BUF_MAX:
                _DROPPED[0] += 1
                return False
            _BUF[ref] = [partner, kind, (path or "")[:200], 1, now, now]
        else:
            row[3] += 1
            row[5] = now
    return True


def _drain() -> dict:
    global _BUF
    with _BUF_LOCK:
        out, _BUF = _BUF, {}
    return out


def _requeue(rows: dict) -> None:
    with _BUF_LOCK:
        for ref, row in rows.items():
            cur = _BUF.get(ref)
            if cur is None:
                if len(_BUF) < _BUF_MAX:
                    _BUF[ref] = row
            else:
                cur[3] += row[3]
                cur[4] = min(cur[4], row[4])
                cur[5] = max(cur[5], row[5])


def flush(conn) -> dict:
    """Write the buffer through `conn` (a psycopg2 connection, or None). On any
    failure the rows go back into the buffer for the next tick."""
    rows = _drain()
    if not rows:
        return {"refs": 0}
    if conn is None:
        _requeue(rows)
        return {"refs": 0, "skipped": "no_db"}
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            for ref, (partner, kind, path, served, first, last) in rows.items():
                cur.execute(_UPSERT, (ref, partner, kind, path, first, last, served))
        conn.commit()
        return {"refs": len(rows)}
    except Exception as ex:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _requeue(rows)
        return {"refs": 0, "error": str(ex)[:200]}


def _with_ref(url, ref):
    sep = "&" if "?" in url else "?"
    return url + sep + "ref=" + ref


def attribute_wall(body, pair_code, path=""):
    """For a keyless caller from a declared partner egress: record the wall's
    pair code to the partner and put it on the /pricing links as ?ref=.

    Every other caller's body is returned untouched. Never raises.
    """
    try:
        if not isinstance(body, dict) or not pair_code or not _REF_OK.match(pair_code):
            return body
        partner = partner_of_request()
        if not partner:
            return body
        if not note_offer_ref(pair_code, partner, "pair_code", path):
            return body
        old = body.get("upgrade_url")
        if isinstance(old, str) and old.startswith(PRICING_URL) and "ref=" not in old:
            new = _with_ref(old, pair_code)
            body["upgrade_url"] = new
            msg = body.get("human_message")
            if isinstance(msg, str):
                # the prose links /pricing with the same attribution params
                body["human_message"] = msg.replace(old, new)
        body["pricing_url"] = _with_ref(PRICING_URL, pair_code)
        return body
    except Exception:  # noqa: BLE001
        return body


# ── the read side (GET /api/v1/admin/usage-tracker/partner-traffic) ─────────

def _table_exists(cur, name) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    row = cur.fetchone()
    return bool(row and row[0])


# A payment or /pricing click belongs to a recorded ref when it IS the ref, or
# is /pricing's wrapper around it: ref_<ref>__tool_<t>__ts_<n>. starts_with,
# not LIKE: '_' is a LIKE wildcard.
_OWNS = "({col} = r.ref OR starts_with({col}, 'ref_' || r.ref || '__'))"


def read_attribution(cur, days: int) -> dict:
    """Refs served, clicks and payments per partner over the last `days` days.
    A missing table reads as absent, never as zero."""
    out = {"window_days": days}
    if not _table_exists(cur, "partner_offer_refs"):
        out["refs"] = "absent: no partner ref has been flushed yet"
        return out
    cur.execute(
        "SELECT partner, kind, COUNT(*), COALESCE(SUM(served), 0) "
        "  FROM partner_offer_refs "
        " WHERE last_served_at > NOW() - make_interval(days => %s) "
        " GROUP BY 1, 2 ORDER BY 1, 2", (days,))
    out["refs"] = [{"partner": p, "kind": k, "refs": int(n), "served": int(s)}
                   for p, k, n, s in cur.fetchall()]
    lanes = (
        # a /pricing button press (/go/p) that carried a partner ref
        ("pricing_clicks", "pricing_checkout_clicks", "c.ref", "c.clicked_at",
         "c.known_plan IS TRUE"),
        # a paid checkout: the pair code itself (the wall's checkout links) or
        # /pricing's wrapper around it. Test-mode payments are not counted.
        ("payments", "mcp_checkout_payments", "c.client_reference_id", "c.paid_at",
         "c.livemode IS NOT FALSE"),
    )
    for label, table, col, ts, extra in lanes:
        if not _table_exists(cur, table):
            out[label] = "absent: " + table + " does not exist"
            continue
        cur.execute(
            "SELECT r.partner, COUNT(*) FROM " + table + " c "
            "  JOIN partner_offer_refs r ON " + _OWNS.format(col=col) +
            " WHERE " + ts + " > NOW() - make_interval(days => %s) AND " + extra +
            " GROUP BY 1 ORDER BY 1", (days,))
        out[label] = {p: int(n) for p, n in cur.fetchall()}
    return out


def pending() -> int:
    """Refs buffered and not yet flushed in this process."""
    with _BUF_LOCK:
        return len(_BUF)
