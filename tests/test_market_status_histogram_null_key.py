"""r-status-null-key (2026-09-05) — one NULL status 500'd /api/v1/markets/<id>.

THE BUG. get_market_stats built its status breakdown as

    by_status = {r[0]: r[1] for r in by_status_rows}

straight from `GROUP BY status`. discovered_facilities.status is NULLABLE, so
a single NULL row puts a literal None among the dict KEYS. flask.jsonify sorts
keys, and sorting None against a str raises:

    TypeError: '<' not supported between instances of 'NoneType' and 'str'

The whole 200 became a 500, which the Cloudflare worker renders to the caller
as "503 Backend unreachable and no cached data available" — a message that
points at infrastructure and names neither the route nor the column.

MEASURED LIVE 2026-09-05 through the edge, cache-busted, 4 consecutive probes
each:

    /api/v1/markets/northern virginia   503 503 503 503
    /api/v1/markets/london-gb           503 503 503 503
    /api/v1/markets/singapore-sg        503 503 503 503
    /api/v1/markets/ashburn             200 200 200 200
    /api/v1/markets/tokyo-jp            200 200 200 200

Origin logs named it exactly: `get_market_stats('northern virginia') error`
at the jsonify(...) line, TypeError as above. The split is not curated vs
international and not big vs small — it is precisely which markets happen to
contain a NULL-status facility. So this fires as ingest lands one and heals
when the row is backfilled, which is why it can pass every test and every
smoke probe and then take down a headline market at 05:00.

★ EXECUTED THROUGH THE REAL flask.jsonify, not asserted about the source. The
broken line and the fixed line differ by a conditional; nothing in the text of
either tells you whether Flask can sort the result. A guard that greps for
'unknown' would pass on code that still crashes.
"""
import pytest

pytest.importorskip("flask")
from flask import Flask, jsonify  # noqa: E402

from util.status_taxonomy import (  # noqa: E402
    UNKNOWN_STATUS_KEY, status_histogram)


def _serialize(hist):
    """Round-trip through the real Flask serializer the route uses."""
    app = Flask(__name__)
    with app.app_context():
        return jsonify(hist).get_data(as_text=True)


def test_the_raw_dict_comprehension_really_does_break_flask():
    """The bug is reproducible — otherwise every assertion below is theatre.
    This is the code that shipped, and it must still raise."""
    rows = [(None, 3), ("Operational", 812)]
    with pytest.raises(TypeError, match="not supported between instances"):
        _serialize({r[0]: r[1] for r in rows})


def test_a_null_status_serializes_after_the_fix():
    """Same rows, through the helper the route now uses."""
    rows = [(None, 3), ("Operational", 812)]
    body = _serialize(status_histogram(rows))
    assert UNKNOWN_STATUS_KEY in body and "812" in body


def test_no_facility_is_lost_to_the_unknown_bucket():
    """NULL and '' are distinct GROUP BY buckets that both land on the same
    key. Assigning instead of accumulating silently drops one of them, which
    would under-report the market."""
    rows = [(None, 3), ("", 1), ("Operational", 812)]
    hist = status_histogram(rows)
    assert hist[UNKNOWN_STATUS_KEY] == 4, "a status bucket was overwritten"
    assert sum(hist.values()) == sum(r[1] for r in rows)


def test_every_key_is_a_string():
    """The invariant that makes the sort safe, stated directly."""
    rows = [(None, 1), ("", 2), ("Operational", 3), ("Under Construction", 4)]
    assert all(isinstance(k, str) for k in status_histogram(rows))


def test_an_empty_histogram_is_still_serializable():
    """A market with no facilities must not be a second failure mode."""
    assert _serialize(status_histogram([])) is not None
    assert status_histogram(None) == {}
