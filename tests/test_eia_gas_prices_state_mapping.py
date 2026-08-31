"""EIA `area-name` → postal mapping must be case-INSENSITIVE.

2026-08-30. `_state_abbr` ended in `STATE_MAP.get(name)` — an exact lookup
against Title-Case keys. EIA returns most states as "USA-XX" but nine as the
BARE UPPERCASE full name ("TEXAS", "OHIO", "NEW YORK", …), so those nine
resolved to None and `fetch_and_upsert` dropped them with a bare `continue`.

The blast radius was not the loader. Those nine are exactly the nine states
routes/dcgi.py then scored on its `cost = 50.0` fallback — 40% of the DCGI
composite — and Texas was published at DCGI 68.0 / rank #3 / GAS-ADVANTAGED off
that constant. Defect 3 of the 2026-08-08 withdrawal audit.

Two things are fenced here, because fixing only the first lets the same class
recur the next time EIA changes a key:
  1. the mapping accepts every casing EIA actually emits;
  2. an unmapped, non-aggregate area is COUNTED, not silently skipped.
"""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "eia_gas_prices_loader.py")
_spec = importlib.util.spec_from_file_location("eia_gas_prices_loader", _PATH)
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)


# The nine, verbatim as the live EIA v2 response emits them (observed
# 2026-08-30 on natural-gas/pri/sum/data, facets[process][]=PEU and PIN).
EIA_UPPERCASE_AREAS = {
    "CALIFORNIA": "CA", "COLORADO": "CO", "FLORIDA": "FL",
    "MASSACHUSETTS": "MA", "MINNESOTA": "MN", "NEW YORK": "NY",
    "OHIO": "OH", "TEXAS": "TX", "WASHINGTON": "WA",
}


@pytest.mark.parametrize("area,expected", sorted(EIA_UPPERCASE_AREAS.items()))
def test_bare_uppercase_area_names_map(area, expected):
    """The exact nine that were being dropped. This is the regression."""
    assert loader._state_abbr(area) == expected, (
        f"EIA emits {area!r} for {expected}; a case-sensitive STATE_MAP lookup "
        f"returns None here and the state vanishes from eia_gas_prices, which "
        f"puts routes/dcgi.py on its `cost = 50.0` constant for that state.")


def test_every_state_maps_in_every_casing_eia_uses():
    """50 states + DC, across all three shapes seen in one EIA response."""
    for full, postal in loader.STATE_MAP.items():
        for variant in (full, full.upper(), full.lower(), postal, f"USA-{postal}"):
            assert loader._state_abbr(variant) == postal, \
                f"{variant!r} should map to {postal}"


def test_aggregates_and_unknowns_still_return_none():
    """Case-folding must not start swallowing non-states."""
    for junk in ("U.S.", "USA", "", None, "ZZ", "USA-ZZ", "Gulf of Mexico",
                 "MIDWEST", "Lower 48"):
        assert loader._state_abbr(junk) is None, f"{junk!r} must not map"


def test_state_map_has_no_case_folded_collisions():
    """The case-insensitive index is only safe while this holds."""
    assert len({k.upper() for k in loader.STATE_MAP}) == len(loader.STATE_MAP)
    assert len(loader._STATE_MAP_CI) == len(loader.STATE_MAP)


def test_state_map_covers_all_50_states_plus_dc():
    assert len(set(loader.STATE_MAP.values())) == 51


# ── The silent-drop fence ───────────────────────────────────────────────────

class _FakeCursor:
    """★ Emulates the BINDING step FIRST, before any logic.

    A stub more forgiving than psycopg2 certifies SQL the driver would refuse:
    a literal percent-sign beside %s params raises IndexError client-side,
    before the statement is ever sent. See the psycopg2 literal-percent trap.
    """
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        if params is not None:
            sql % tuple(params)          # must not raise
            self.sink.append(tuple(params))

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.written = []

    def cursor(self):
        return _FakeCursor(self.written)

    def commit(self):
        pass

    def rollback(self):
        pass


def _run_with_areas(monkeypatch, areas):
    """Drive fetch_and_upsert over a synthetic EIA page of the given areas."""
    def fake_get(endpoint, params=None):
        if params.get("offset"):
            return {"response": {"data": []}}
        return {"response": {"data": [
            {"area-name": a, "period": "2026-05", "value": "5.00"} for a in areas
        ]}}
    monkeypatch.setattr(loader, "eia_get", fake_get)
    monkeypatch.setattr(loader.time, "sleep", lambda *_: None)
    conn = _FakeConn()
    total, errors, by_sector, unmapped = loader.fetch_and_upsert(conn)
    return conn, total, unmapped


def test_uppercase_state_is_written_not_dropped(monkeypatch):
    conn, total, unmapped = _run_with_areas(monkeypatch, ["TEXAS", "USA-AK"])
    states = {row[0] for row in conn.written}
    assert "TX" in states, "TEXAS must reach the INSERT, not the floor"
    assert "AK" in states
    assert unmapped == {}, f"nothing should be unmapped here, got {unmapped}"


def test_unmapped_area_is_counted_not_silently_skipped(monkeypatch):
    """The observability half. A bare `continue` is what hid this for months."""
    conn, total, unmapped = _run_with_areas(
        monkeypatch, ["USA-AK", "ATLANTIS", "ATLANTIS", "U.S."])
    assert "ATLANTIS" in unmapped, "an unknown area must be REPORTED"
    assert unmapped["ATLANTIS"] == 2 * len(loader.GAS_PROCESSES), \
        "the count must be real, not a boolean"
    assert "U.S." not in unmapped, "known aggregates stay quiet"
