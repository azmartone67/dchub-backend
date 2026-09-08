"""Guards for routes/blocked_demand_profile.py (2026-09-08).

The endpoint exists to answer a question prospecting asked ("give me the 172
repeat wall-hitters so I can email them") with the two facts that make the
answer no: a caller bucket is not a company, and almost none of them are
mailable. Both of those are only useful if they are TRUE OF THE RESPONSE, so
these guards run the assembly against a stubbed cursor rather than reading
the source for reassuring words.

WHAT THEY PIN
  · no caller identity ever reaches the payload — asserted over the whole
    returned structure, not over a list of keys someone remembered to redact;
  · every depth bucket is published even when the window put nothing in it
    (an omitted bucket reads as "nobody is that deep");
  · the bucket SQL is derived from DEPTH_BUCKETS, so labels and boundaries
    cannot drift apart;
  · DEEP_AT is one constant reaching every "deep" count;
  · an unreadable database reports None, never 0;
  · no query string carries a literal % (the defect that took the live
    handoff-funnel endpoint down inside one deploy).
"""
import ast
import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routes import blocked_demand_profile as bdp  # noqa: E402

_HERE = os.path.dirname(__file__)
_ROOT = os.path.join(_HERE, "..")

# Values a caller bucket could carry. If any of these reaches the payload the
# endpoint has stopped being aggregates-only.
_SECRET_CALLER = "anon:deadbeefdeadbeefdeadbe"
_SECRET_EMAIL = "someone@example.com"
_SECRET_IP = "203.0.113.77"
_SECRET_SESSION = "8c8e1d0dcafef00d"


class _FakeCursor:
    """Returns canned rows in the order run_profile issues its queries."""

    def __init__(self, results):
        self._results = list(results)
        self._last = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = self._results.pop(0) if self._results else []

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def _rows():
    """One canned result set per query, in issue order.

    Deliberately carries a caller id, an email, an IP and a session id in the
    rows the DB hands back — the payload must contain none of them.
    """
    return [
        # totals
        [(1234, 172, 400, 9)],
        # depth: only two buckets came back; the other three must still print.
        # ★ SUM(hits) is position 2 and it is a Decimal, NOT an int —
        # psycopg2 maps PostgreSQL numeric (what SUM(bigint) returns) to
        # decimal.Decimal. This fixture handed back ints for the first three
        # days of this endpoint's life, which is why the whole depth suite
        # was green while the live response published `signals: 0` in every
        # bucket. COUNT(*) positions stay int — that is what the driver does.
        [("3-9", 266, Decimal("900"), 5), ("10-19", 59, Decimal("334"), 1)],
        # by_tool basic
        [("get_interconnection_queue", 922, 219, 300),
         ("get_renewable_energy", 441, 279, 288)],
        # by_tool deep
        [("get_interconnection_queue", 40), ("get_renewable_energy", 3)],
        # contactability
        [(831, 10, 1, 172, 1)],
    ]


def _run(monkeypatch, rows=None, conn=True):
    cur = _FakeCursor(rows if rows is not None else _rows())
    monkeypatch.setattr(bdp, "_conn", (lambda: _FakeConn(cur)) if conn else (lambda: None))
    return bdp.run_profile(30), cur


# ── 1. no caller identity in the payload ───────────────────────────────────

def test_payload_carries_no_caller_identity(monkeypatch):
    """Identity columns must not survive into the response.

    ★ THE ONE STRING THIS ENDPOINT DOES PASS THROUGH IS `tool_requested`, and
    that is the point of it — a tool name is the subject, not the caller.
    It is written server-side by signalPaywall() from the tool being invoked,
    not supplied as free text by the caller, so it is not an identity carrier
    and this guard does not pretend otherwise. Smuggling an email into the
    tool column would test a threat that does not exist and would have to be
    "fixed" by an allowlist that breaks the endpoint the day a tool is added.
    Everything drawn from caller_id / user_email / ip_address / session_id is
    aggregated before it is published, and that is what is asserted here.
    """
    rows = _rows()
    # smuggle identity into every row position the assembly reads from an
    # identity column
    rows[1] = [("3-9", 266, 900, 5), (_SECRET_CALLER, 59, 334, 1)]
    rows[4] = [(_SECRET_IP, _SECRET_EMAIL, 1, 172, 1)]
    out, _ = _run(monkeypatch, rows)
    blob = json.dumps(out)
    for secret in (_SECRET_CALLER, _SECRET_EMAIL, _SECRET_IP, _SECRET_SESSION):
        assert secret not in blob, (
            "a caller identity reached the payload: %r. This endpoint is "
            "aggregates-only by construction, not by redaction." % secret)


def test_unknown_bucket_from_the_db_cannot_invent_a_row(monkeypatch):
    """A label the code does not know must not become a published bucket."""
    rows = _rows()
    rows[1] = [("wat", 999, 999, 999)]
    out, _ = _run(monkeypatch, rows)
    assert [b["bucket"] for b in out["caller_depth"]] == [
        lbl for lbl, _lo, _hi in bdp.DEPTH_BUCKETS]


# ── 2. every bucket is published, including the empty ones ─────────────────

def test_all_depth_buckets_are_published_even_when_empty(monkeypatch):
    out, _ = _run(monkeypatch)
    got = [b["bucket"] for b in out["caller_depth"]]
    assert got == [lbl for lbl, _lo, _hi in bdp.DEPTH_BUCKETS], got
    empty = [b for b in out["caller_depth"] if b["bucket"] == "50+"]
    assert empty and empty[0]["callers"] == 0 and empty[0]["signals"] == 0, (
        "a bucket the window left empty was dropped — that reads as 'nobody "
        "is that deep' rather than 'no rows landed here'")
    deep = [b for b in out["caller_depth"] if b["bucket"] == "10-19"][0]
    assert deep["callers"] == 59 and deep["signals"] == 334
    assert deep["signal_share_pct"] == pytest.approx(27.1, abs=0.2)


def test_sum_positions_survive_the_driver_type(monkeypatch):
    """A Decimal in a SUM position must publish as a number, not as 0.

    ★ THE REGRESSION THIS PINS shipped live: `_int` whitelisted (int, float),
    psycopg2 returns decimal.Decimal for numeric, so SUM(hits) coerced to
    None and `or 0` published a hard 0 in every depth bucket — with
    signal_share_pct null on top of it, because the denominator was the sum
    of those zeros. `callers` was right throughout (COUNT(*) is an int), so
    the response looked structurally perfect and was silently wrong in one
    column. Asserting on _int directly is not enough: the defect was the
    combination of the coercion and the `or 0` that consumes it, so this
    drives the assembled payload.
    """
    assert bdp._int(Decimal("334")) == 334, (
        "_int dropped a Decimal — psycopg2 hands back Decimal for every "
        "numeric column, so this republishes the live zeros"
    )
    out, _ = _run(monkeypatch)
    depth = {b["bucket"]: b for b in out["caller_depth"]}
    assert depth["10-19"]["signals"] == 334
    assert depth["3-9"]["signals"] == 900
    assert sum(b["signals"] for b in out["caller_depth"]) == 1234, (
        "the depth signal total collapsed — a Decimal is being dropped "
        "somewhere between the cursor and the payload"
    )
    assert all(b["signal_share_pct"] is not None
               for b in out["caller_depth"] if b["signals"]), (
        "a non-empty bucket published a null share, which means the "
        "denominator summed to zero"
    )


def test_share_is_none_not_zero_on_an_empty_window(monkeypatch):
    rows = _rows()
    rows[1] = []
    out, _ = _run(monkeypatch, rows)
    assert all(b["signal_share_pct"] is None for b in out["caller_depth"]), (
        "0% off an empty denominator is a fabricated statistic")


# ── 3. the SQL is derived from the constants ───────────────────────────────

def test_bucket_case_is_derived_from_the_tuple():
    case = bdp._bucket_case("hits")
    for label, lo, _hi in bdp.DEPTH_BUCKETS:
        assert "'%s'" % label in case, "label %r missing from the CASE" % label
    for _label, lo, _hi in bdp.DEPTH_BUCKETS[1:]:
        assert ">= %d" % lo in case, "boundary %d missing from the CASE" % lo
    assert "%s" not in case and "%d" not in case


def test_deep_at_is_one_constant_reaching_every_deep_count(monkeypatch):
    out, cur = _run(monkeypatch)
    assert out["deep_at"] == bdp.DEEP_AT
    deep_params = [p for sql, p in cur.executed
                   if p and bdp.DEEP_AT in (p if isinstance(p, tuple) else ())]
    assert len(deep_params) >= 2, (
        "DEEP_AT reaches fewer than two queries — by_tool.deep_callers and "
        "contactability.deep_callers would be free to disagree")


# ── 4. failure reports as unmeasured, never as zero ────────────────────────

def test_no_database_reports_none_not_zero(monkeypatch):
    out, _ = _run(monkeypatch, conn=False)
    assert out["ok"] is False
    for k in ("totals", "caller_depth", "by_tool", "contactability"):
        assert out[k] is None, (
            "%s reported %r on an unreadable database; zeros here read as an "
            "absence of demand, the one wrong conclusion this endpoint exists "
            "to prevent" % (k, out[k]))


# ── 5. no literal % in any query ───────────────────────────────────────────

def test_no_query_carries_a_literal_percent(monkeypatch):
    _out, cur = _run(monkeypatch)
    assert cur.executed, "no queries ran"
    for sql, _params in cur.executed:
        assert sql.count("%") == sql.count("%s"), (
            "literal %% in a query — it cannot survive a `sql %% args` call "
            "site:\n  " + sql[:200])


# ── 6. the module says what it does not measure, and is wired up ───────────

def test_response_declares_the_platform_gap_and_the_id_space_trap(monkeypatch):
    out, _ = _run(monkeypatch)
    assert "mailable" in out["contactability"]
    assert out["contactability"]["mailable"] == 1
    for field in ("basis", "not_measured_here", "compare_carefully"):
        assert out.get(field), "response ships without %s" % field
    assert "top_signal_tools_30d" in out["compare_carefully"], (
        "the response must warn that by_tool.callers and the funnel's "
        "same-named field are different id spaces")


def test_endpoint_is_registered_in_main():
    with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "register_blocked_demand_profile" in called, (
        "the blueprint is never registered — the route does not exist in "
        "production no matter what the module defines")
