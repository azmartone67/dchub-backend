"""tests/test_no_fabricated_facility_fields.py — absent data must be NULL (2026-08-11).

Three fields were being ASSERTED rather than sourced, across every discovery
writer:

  country  -> 'US'           a Slovak facility published country='US' while its
                             own coordinates in the same payload said Bratislava
                             (48.1676, 17.0723), and one row carried the wrong
                             country in its page <title>, i.e. the SERP snippet
  status   -> 'Operational'  stamped on 100% of discovered rows — 5,641/5,641
                             OSM, 1,049/1,049 PeeringDB, 5,366/5,366 peeringdb,
                             including 758 named only "OSM DC <numeric id>"
  power_mw -> 0              then restated by score_facility as "0.0 MW capacity
                             (below market avg of 2 MW)" — an absence reported
                             as a measurement, with a comparative verdict

A score can carry an error bar. A bare fact cannot: the country is either
sourced or it is not. These are the fields an external analyst spot-checks
first, so the rule is now absolute — if the upstream did not say it, we publish
NULL and report it as not measured.

House rules: no DB, no network, never import main.py.

Run:  python3 -m pytest tests/test_no_fabricated_facility_fields.py -v
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

WRITERS = ["routes/discovery_routes.py", "routes/osm_crawler.py"]


def _src(rel):
    p = ROOT / rel
    assert p.exists(), f"{rel} is missing — did a file move?"
    return p.read_text(encoding="utf-8", errors="replace")


def _code_only(rel):
    """Source with comment-only lines removed.

    The fix itself documents WHY 'Operational' was wrong, so those explanatory
    comments legitimately contain the retired literals. Asserting against raw
    text would false-fail on our own comments — and, worse, would tempt someone
    to delete the explanation to make the test pass.
    """
    out = []
    for line in _src(rel).splitlines():
        st = line.strip()
        if st.startswith("#") or st.startswith("--"):
            continue
        out.append(line)
    return "\n".join(out)


# ─── the writers no longer fabricate ─────────────────────────────────────

@pytest.mark.parametrize("rel", WRITERS)
def test_no_us_country_fallback(rel):
    code = _code_only(rel)
    for pat in (r"or\s+'US'", r",\s*'US'\)", r"else\s+'US'", r'or\s+"US"',
                r"DEFAULT\s+'US'"):
        m = re.search(pat, code)
        assert not m, f"{rel} still falls back to 'US': {m.group(0)!r}"


@pytest.mark.parametrize("rel", WRITERS)
def test_no_operational_literal_in_code(rel):
    code = _code_only(rel)
    assert "'Operational'" not in code, (
        f"{rel} still hard-codes 'Operational'. A status DC Hub did not source "
        f"must be NULL — OSM tells us a facility exists, not that it is running.")
    assert "default='Operational'" not in code, (
        f"{rel} still passes default='Operational' to canon_status — the "
        f"fabrication was the DEFAULT ARGUMENT, not just the inline literal.")


@pytest.mark.parametrize("rel", WRITERS)
def test_no_zero_power_fallback(rel):
    code = _code_only(rel)
    for pat in (r"power_mw\s*=\s*0\b", r"'power_mw',\s*0\)",
                r"power_mw'\)\s*\)\s*or\s+0\b"):
        m = re.search(pat, code)
        assert not m, f"{rel} still defaults power_mw to 0: {m.group(0)!r}"


def test_stage_facility_signature_defaults_are_none():
    """The keyword defaults are a writer too — a caller that omits the argument
    gets whatever the signature says."""
    import routes.discovery_routes as dr
    sig = inspect.signature(dr._stage_facility)
    for field in ("country", "power_mw", "status"):
        assert sig.parameters[field].default is None, (
            f"_stage_facility({field}=...) still defaults to "
            f"{sig.parameters[field].default!r}")


# ─── canon_status must drop absent, and KEEP sourced ─────────────────────

def test_canon_status_returns_none_when_nothing_was_sourced():
    from util.facility_status import canon_status
    assert canon_status(None, default=None) is None
    assert canon_status("", default=None) is None


def test_canon_status_still_preserves_a_real_status():
    """Over-correcting is also a regression: a status the upstream DID state
    must survive. This guard is why the fix changes the DEFAULT rather than
    stripping the call."""
    from util.facility_status import canon_status
    assert canon_status("active", default=None) == "Operational"
    assert canon_status("Under Construction", default=None) == "Under Construction"


# ─── the DDL, and the part that actually reaches production ──────────────

def test_create_table_declares_no_fabricating_defaults():
    ddl = _src("routes/discovery_routes.py")
    i = ddl.find("CREATE TABLE IF NOT EXISTS discovered_facilities")
    assert i > 0
    block = ddl[i:ddl.find(")", ddl.find("UNIQUE(source, source_id)"))]
    block = "\n".join(l for l in block.splitlines()
                      if not l.strip().startswith("--"))
    assert "country TEXT DEFAULT" not in block
    assert "power_mw REAL DEFAULT" not in block
    assert "status TEXT DEFAULT" not in block


def test_live_table_defaults_are_dropped_by_a_migration():
    """★ THE LOAD-BEARING TEST.

    The CREATE TABLE is `IF NOT EXISTS`, so editing its column defaults changes
    NOTHING on a database where the table already exists — production. Without
    an explicit ALTER ... DROP DEFAULT, this entire fix would look shipped, pass
    every other test in this file, and behave identically in production: any
    INSERT omitting the column would still be handed 'US' / 0 / 'Operational'
    by the server-side default.

    This is the same class of trap as editing a repo file that nothing serves.
    """
    src = _src("routes/discovery_routes.py")
    assert "DROP DEFAULT" in src, (
        "no ALTER ... DROP DEFAULT anywhere — the column defaults still stand "
        "on the live table and the fix is a no-op in production")
    m = re.search(r"for col in \(([^)]*)\):\s*\n\s*try:\s*\n\s*c\.execute\(",
                  src)
    assert m, "expected the DROP DEFAULT migration to follow the ALTER-loop idiom"
    cols = m.group(1)
    for col in ("country", "power_mw", "status"):
        assert f"'{col}'" in cols, f"{col} missing from the DROP DEFAULT loop"


# ─── the OSM inserts pass NULL, not literals ─────────────────────────────

def test_osm_inserts_bind_null_for_power_and_status():
    code = _code_only("routes/osm_crawler.py")
    # Both canonical INSERTs (primary + post-rollback retry) and the
    # discovered_facilities stage must bind NULL where they bound 0/'Operational'.
    assert code.count("NULL, NULL, %s,") >= 2, (
        "the two canonical INSERTs should bind NULL for power_mw and status")
    assert re.search(r"%s,\s*NULL,\s*\n\s*NULL,\s*%s,", code), (
        "the discovered_facilities stage should bind NULL for power_mw and status")


def test_osm_passes_null_country_not_empty_string():
    """An empty string is not absence: it defeats "absent means NULL" and it
    hides the row from the coordinate-based country repair, which looks for
    rows to fill."""
    code = _code_only("routes/osm_crawler.py")
    assert 'r.get("country", "")' not in code, (
        "osm_crawler still passes '' for country; use r.get('country') or None")
    assert code.count('r.get("country") or None') >= 3
