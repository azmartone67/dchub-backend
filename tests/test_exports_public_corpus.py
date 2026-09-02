"""r-exports-corpus (2026-09-02) — the public facility export must carry the
already-public fields for every row, and must not be able to carry anything
else.

Pairs with tests/test_exports_stub_guard.py: that file proves a tier preview
never ships; this one proves the thing that replaces it is the corpus and not
a wider disclosure than was agreed.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.r2_exports import (  # noqa: E402
    _FACILITY_LICENCE,
    _PUBLIC_FACILITY_SQL,
    _BUILDERS,
    _gate_evidence,
    _row_count,
)

# Fields already rendered on every public dchub.cloud/facilities/<slug> page —
# main.py BASIC_FIELDS minus the internal row id, plus the derived profile_url.
PUBLIC = {"canonical_slug", "name", "provider", "city", "state", "country"}
# Anything scored, located or licensed. None of these may be selectable.
PAID = {"power_mw", "capacity_mw", "latitude", "longitude", "lat", "lon",
        "tenants", "sqft", "confidence_score", "dcpi", "score", "operator_id",
        "contract", "revenue", "utility", "substation"}


def _selected_columns(sql):
    head = sql.split("FROM")[0]
    head = re.sub(r"SELECT\s+DISTINCT\s+ON\s*\([^)]*\)", "", head,
                  flags=re.I).replace("SELECT", "")
    return {c.strip() for c in head.split(",") if c.strip()}


def test_the_query_selects_exactly_the_public_fields():
    assert _selected_columns(_PUBLIC_FACILITY_SQL) == PUBLIC


def test_no_paid_column_is_selectable():
    """★ The tier preview arrived by accident. A paid field must not be able
    to arrive the same way — the SELECT list is explicit, never SELECT *."""
    assert "*" not in _PUBLIC_FACILITY_SQL
    cols = _selected_columns(_PUBLIC_FACILITY_SQL)
    assert cols & PAID == set(), cols & PAID


def test_the_guard_would_reject_a_paid_column_being_added():
    """Mutation-in-a-test: prove the assertion above can fail."""
    bad = "SELECT DISTINCT ON (canonical_slug) canonical_slug, name, power_mw FROM x"
    assert _selected_columns(bad) & PAID == {"power_mw"}


def test_row_basis_mirrors_the_citeable_count():
    """public_endpoints publishes facilities as COUNT(DISTINCT canonical_slug)
    ... WHERE canonical_slug IS NOT NULL. An export on a different basis would
    disagree with the homepage total."""
    flat = " ".join(_PUBLIC_FACILITY_SQL.split())
    assert "DISTINCT ON (canonical_slug)" in flat
    assert "FROM discovered_facilities" in flat
    assert "WHERE canonical_slug IS NOT NULL" in flat


def test_facilities_is_built_in_process_not_over_http():
    """The HTTP path can only ever see the anonymous tier — that is the whole
    defect. facilities must not be able to fall back to it."""
    assert "facilities" in _BUILDERS


def test_the_licence_is_stamped_and_forbids_relicensing():
    assert _FACILITY_LICENCE["layer"] == "facility_inventory"
    assert "ODbL" in _FACILITY_LICENCE["note"]
    assert "CC-BY" in _FACILITY_LICENCE["note"]
    assert _FACILITY_LICENCE["attribution"]


def _sample_payload(n=3):
    return {
        "dataset": "facilities", "count": n,
        "basis": "...", "fields": sorted(PUBLIC | {"profile_url"}),
        "licence": _FACILITY_LICENCE, "generated_at": "2026-09-02T00:00:00Z",
        "data": [{"slug": f"s{i}", "name": "n"} for i in range(n)],
    }


def test_the_corpus_payload_does_not_trip_the_stub_guard():
    """★ A false positive here would block the real dataset for looking like
    a preview — the two guards must not fight."""
    raw = json.dumps(_sample_payload(20191)).encode()
    assert _gate_evidence(raw) is None
    assert _row_count(json.loads(raw)) == 20191


# ── empty slug != absent slug (2026-09-02, found in the first live build) ───
import types  # noqa: E402


class _FakeCur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *_a, **_k): return None
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCur(self._rows)
    def close(self): return None


def _build_with(rows, monkeypatch):
    """Run the REAL builder against injected rows — no DB, no HTTP."""
    monkeypatch.setitem(
        sys.modules, "db_utils",
        types.SimpleNamespace(get_read_db=lambda: _FakeConn(rows)))
    from routes.r2_exports import _build_public_facilities
    return _build_public_facilities()


_GOOD = ("007-hebergement-paris-a8b78433", "007 Hebergement Paris", None,
         "Paris", None, "FR")
# the live row that shipped a dead link
_EMPTY = ("", "L7", "Digital Telecom IX LLC", "Kyiv", "", "UA")


def test_an_empty_slug_gets_no_profile_url(monkeypatch):
    """★ THE REGRESSION. It rendered 'https://dchub.cloud/facilities/' — a
    bare directory URL asserting a page that does not exist."""
    p = _build_with([_GOOD, _EMPTY], monkeypatch)
    row = [r for r in p["data"] if r["name"] == "L7"][0]
    assert row["profile_url"] is None, row
    assert row["slug"] == ""


def test_the_empty_slug_row_is_still_counted(monkeypatch):
    """★ THE HALF THAT MUST NOT BE 'FIXED'. Dropping the row would make the
    export 20,193 against a published 20,194 and break the reconciliation
    that mirroring public_endpoints' basis exists to guarantee."""
    p = _build_with([_GOOD, _EMPTY], monkeypatch)
    assert p["count"] == 2
    assert len(p["data"]) == 2


def test_a_real_slug_still_gets_its_url(monkeypatch):
    p = _build_with([_GOOD], monkeypatch)
    assert p["data"][0]["profile_url"] == (
        "https://dchub.cloud/facilities/007-hebergement-paris-a8b78433")


def test_whitespace_only_slug_is_treated_as_empty(monkeypatch):
    p = _build_with([("   ", "Ghost", None, None, None, "US")], monkeypatch)
    assert p["data"][0]["profile_url"] is None
    assert p["count"] == 1


def test_a_null_slug_does_not_crash_the_builder(monkeypatch):
    """WHERE canonical_slug IS NOT NULL should prevent this, but the row
    projection must not be the thing that discovers it was wrong."""
    p = _build_with([(None, "NullSlug", None, None, None, "US")], monkeypatch)
    assert p["data"][0]["profile_url"] is None
