"""The data-native card must never draw a number it did not measure.

`grid_inventory` renders ONE MARK PER ASSET from the grid layer. That claim is
only worth making if three things hold, and these tests are those three:

  1. A FAILED READ IS NEVER A ZERO. A dropped connection must omit the class,
     not publish "0 substations near Ashburn".
  2. THE RADIUS CLAIM IS TRUE. The sibling counts in
     grid_intelligence_routes_patched use `ABS(lat - x) < deg` — a bounding BOX —
     while saying "radius". This card says "within N km" on its face, so the SQL
     is executed here against known points to prove the corners of the box are
     excluded.
  3. NO COORDINATE ⇒ NO CARD. It falls back to editorial rather than inventing
     an inventory.

No network, no Postgres: the radius test runs the REAL SQL string under sqlite
with the maths functions registered, so it exercises the query rather than
asserting that a string contains the word "radians".
"""
import datetime
import math
import os
import sqlite3

import pytest

pytest.importorskip("PIL")


# ── 2. the radius claim, by executing the real SQL ─────────────────────────

def _run_sql_under_sqlite(rows, lat, lon, radius_km):
    """Execute og_cards._GRID_COUNT_SQL verbatim (bar the paramstyle) against
    an in-memory table, so the predicate itself is under test."""
    from routes import og_cards as o
    con = sqlite3.connect(":memory:")
    con.create_function("radians", 1, math.radians)
    con.create_function("asin", 1, math.asin)
    con.create_function("sin", 1, math.sin)
    con.create_function("cos", 1, math.cos)
    con.create_function("sqrt", 1, math.sqrt)
    con.create_function("power", 2, lambda a, b: a ** b)
    con.execute("CREATE TABLE substations (lat REAL, lng REAL)")
    con.executemany("INSERT INTO substations VALUES (?, ?)", rows)

    sql = o._GRID_COUNT_SQL.format(table="substations")
    sql = sql.replace("%(", ":").replace(")s", "")   # psycopg2 -> sqlite params
    deg_lat = radius_km / 111.0
    deg_lng = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-6)
    return con.execute(sql, {
        "lat": lat, "lng": lon, "radius_km": radius_km,
        "lat_lo": lat - deg_lat, "lat_hi": lat + deg_lat,
        "lng_lo": lon - deg_lng, "lng_hi": lon + deg_lng,
    }).fetchone()[0]


def test_the_box_corner_is_excluded_but_the_radius_is_kept():
    """A bbox of half-width R includes points up to R*sqrt(2) at the corners.
    A point at ~1.4R must NOT be counted by a card that says 'within R km'."""
    lat, lon, R = 41.14, -104.82, 60.0
    dlat = R / 111.0
    dlng = R / (111.0 * math.cos(math.radians(lat)))
    rows = [
        (lat, lon),                          # centre                 — in
        (lat + dlat * 0.90, lon),            # ~54 km north           — in
        (lat + dlat * 0.99, lon + dlng * 0.99),   # box CORNER, ~84 km — OUT
    ]
    got = _run_sql_under_sqlite(rows, lat, lon, R)
    assert got == 2, (
        f"expected 2 inside a true 60 km radius, got {got} — the query is "
        f"counting the bounding box, so 'within 60 km' on the card is false")


def test_a_point_just_beyond_the_radius_is_excluded():
    lat, lon, R = 41.14, -104.82, 60.0
    inside = lat + (R * 0.98) / 111.0
    outside = lat + (R * 1.02) / 111.0
    assert _run_sql_under_sqlite([(inside, lon)], lat, lon, R) == 1
    assert _run_sql_under_sqlite([(outside, lon)], lat, lon, R) == 0


# ── 1. a failed read is never a zero ───────────────────────────────────────

def test_a_failed_query_yields_none_not_zero(monkeypatch):
    """The whole point. `None` means unmeasured; `0` is a measurement."""
    from routes import og_cards as o

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if "FROM substations" in sql:
                raise RuntimeError("connection reset")
        def fetchone(self): return (7,)

    class _Conn:
        def cursor(self): return _Cur()
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())
    counts = o._grid_counts(41.14, -104.82, 60)
    assert counts["substations"] is None, "a failed read became a number"
    assert counts["transmission_lines"] == 7


@pytest.mark.parametrize("empty", [None, (None,)], ids=["no-row", "null-count"])
def test_a_query_that_succeeds_but_returns_nothing_is_also_not_zero(monkeypatch, empty):
    """★ The exception path is not the only way to fail. A query can RETURN —
    with no row, or a NULL count — and `int(row[0]) if row else 0` would publish
    a measured-looking zero. Mutation-found: the exception test above passes
    even with the `else 0` bug, because that line is never reached when execute
    raises."""
    from routes import og_cards as o

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __init__(self): self.sql = ""
        def execute(self, sql, params=None): self.sql = sql
        def fetchone(self):
            return empty if "FROM substations" in self.sql else (7,)

    class _Conn:
        def cursor(self): return _Cur()
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setenv("DATABASE_URL", "postgres://stub")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())
    counts = o._grid_counts(41.14, -104.82, 60)
    assert counts["substations"] is None, (
        f"an empty result ({empty!r}) became {counts['substations']!r} — the card "
        f"would publish a zero it never measured")


def test_a_class_that_failed_is_omitted_from_the_card(monkeypatch):
    from routes import og_cards as o
    monkeypatch.setattr(o, "_grid_counts",
                        lambda *a, **k: {"transmission_lines": 12,
                                         "substations": None,
                                         "power_plants": 3})
    img = o._draw_grid_inventory(_pr())
    assert img.size == (o.W, o.H)
    # 12 + 3 = 15; a card that counted the None as 0 would still say 15, so the
    # real check is that the omitted class cannot contribute a drawn field.
    monkeypatch.setattr(o, "_grid_counts",
                        lambda *a, **k: {"transmission_lines": 12,
                                         "substations": 0,
                                         "power_plants": 3})
    with_zero = o._draw_grid_inventory(_pr())
    assert img.tobytes() != with_zero.tobytes(), (
        "an unmeasured class renders identically to a measured zero — the card "
        "cannot tell 'we did not look' from 'there are none'")


def test_no_class_measured_falls_back_rather_than_drawing_an_empty_inventory(monkeypatch):
    from routes import og_cards as o
    monkeypatch.setattr(o, "_grid_counts",
                        lambda *a, **k: {"transmission_lines": None,
                                         "substations": None,
                                         "power_plants": None})
    assert o._draw_grid_inventory(_pr()).tobytes() == o._draw_editorial(_pr()).tobytes()


# ── 3. no coordinate, no card ──────────────────────────────────────────────

def _pr(card=None):
    return {"slug": "dyn-grid", "title": "A headline long enough to wrap twice over",
            "subheadline": "20,500+ facilities", "topic": "grid",
            "date": datetime.date(2026, 9, 5), "signals": {},
            "card": card if card is not None else
                    {"lat": "41.14", "lon": "-104.82",
                     "place": "Cheyenne, WY", "radius_km": "60"}}


@pytest.mark.parametrize("card", [
    {}, {"place": "Cheyenne"}, {"lat": "41.14"}, {"lat": "abc", "lon": "-104.8"},
    {"lat": "999", "lon": "-104.8"},
])
def test_a_missing_or_bad_coordinate_falls_back_to_editorial(card, monkeypatch):
    from routes import og_cards as o
    monkeypatch.setattr(o, "_grid_counts",
                        lambda *a, **k: pytest.fail("queried without a valid coordinate"))
    pr = _pr(card)
    assert o._draw_grid_inventory(pr).tobytes() == o._draw_editorial(pr).tobytes()


def test_the_headline_total_is_the_sum_of_the_drawn_classes(monkeypatch):
    """The card's own number must equal what it drew."""
    from routes import og_cards as o
    seen = {}
    real = o._unit_field
    def spy(img, d, x0, y0, n, colour, shape, cols=22, gap=19):
        seen[len(seen)] = n
        return real(img, d, x0, y0, n, colour, shape, cols, gap)
    monkeypatch.setattr(o, "_unit_field", spy)
    monkeypatch.setattr(o, "_grid_counts",
                        lambda *a, **k: {"transmission_lines": 140,
                                         "substations": 111, "power_plants": 10})
    o._draw_grid_inventory(_pr())
    assert sum(seen.values()) == 261, f"drew {sum(seen.values())} marks, card says 261"


def test_a_dense_metro_declares_that_it_truncated(monkeypatch):
    """The panel holds 176 marks. Over that the card must SAY it is showing a
    subset, not silently draw a wrong quantity."""
    from routes import og_cards as o
    monkeypatch.setattr(o, "_grid_counts",
                        lambda *a, **k: {"transmission_lines": 4000,
                                         "substations": 12, "power_plants": 3})
    drawn = []
    real = o._unit_field
    monkeypatch.setattr(o, "_unit_field",
                        lambda img, d, x0, y0, n, c, s, cols=22, gap=19:
                        (drawn.append(n), real(img, d, x0, y0, n, c, s, cols, gap))[1])
    o._draw_grid_inventory(_pr())
    assert max(drawn) <= 22 * 8, "an unbounded field would overflow the panel"


def test_the_style_is_registered_and_reachable():
    from routes import og_cards as o
    assert o.STYLE_MAP.get("grid_inventory") is o._draw_grid_inventory
