#!/usr/bin/env python3
"""One URL, one row (r-one-url-many-rows, 2026-09-12).

MEASURED LIVE 2026-09-12, through the public IndexNow delta preview (be#4454)
and the public facilities listing:

    ids 12901642-12901646  "South Reach Networks Fort Pierce"
      · all five wear ONE canonical_slug, south-reach-networks-fort-pierce-d6d47cf4
      · all five have duplicate_of_id IS NULL — /api/v1/facilities filters on
        exactly that and returned all five
      · all five have is_duplicate 0/NULL — the delta filters on that and
        returned all five
    → the 5-result free search preview was that ONE building, five times.

Globally: 22,573 rows in the delta stream resolve to 20,504 distinct URLs.
~696 of the 2,069 redundant rows collapse INSIDE a single 500-row window —
identical slug, consecutive ids — and they are this defect. (The other ~1,373
are twin→keeper pairs, which is the pointer dedup working.)

WHY NOTHING CAUGHT IT. Every existing lane looks for one facility published at
SEVERAL URLs; this is the inverse. No pointer, so pointer dedup is blind; flag
unset, so flag dedup is blind; repair_dedup_keeper_election fixes groups with NO
keeper and this has five; facility_dedup_v3 wants an anonymous-provider twin and
all five share one real provider; facility_dedup_v4 keys on duplicate published
URLs and this is ONE url; _twin_redirect_target case B needs slug_rows == 1.

THE CAUSE. source_url was the only write-time dedup, and it is narrower than the
identity it protects. One building reachable at five URLs in one sweep passed
diff_gaps' seen_urls five times, and _is_existing reads the database as it was
BEFORE the run's own inserts, so the siblings never saw each other.

Run:  python3 -m pytest tests/test_one_url_one_row.py -v
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routes.facility_slug_freeze import build_canonical_slug  # noqa: E402

SRN = {"provider": "South Reach Networks", "name": "South Reach Networks Fort Pierce"}
SRN_SLUG = build_canonical_slug(SRN["provider"], SRN["name"])


def test_the_fixture_names_a_real_composed_slug():
    """Floor. If the composer returned None for this pair, every assertion
    below would be exercising the `no slug -> do not check` branch."""
    assert SRN_SLUG and SRN_SLUG.startswith("south-reach-networks-fort-pierce-"), SRN_SLUG


# ── a database that behaves like psycopg2, and no better ────────────────────

class _FakeCursor:
    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self._conn.statements.append(text)
        if self._conn.aborted:
            # psycopg2 refuses every statement after an error until rollback.
            # Without this the fail-open test would pass on a guard that never
            # rolled back, which is the whole thing it exists to prove.
            raise RuntimeError("current transaction is aborted")
        if self._conn.raise_on and self._conn.raise_on in text:
            self._conn.aborted = True
            raise RuntimeError("probe exploded")
        self._rows = self._conn.answer(text)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _FakeConn:
    """`existing` is what the identity probe finds; `raise_on` is a substring
    of the statement that should blow up."""

    def __init__(self, existing=None, raise_on=None):
        self.existing, self.raise_on = existing or [], raise_on
        self.statements, self.aborted, self.rollbacks, self.commits = [], False, 0, 0

    def answer(self, text):
        if "INSERT INTO discovered_facilities" in text:
            return [(4242,)]
        if "WHERE source_url" in text:
            return []
        if "canonical_slug = %s" in text:
            return list(self.existing)
        return []

    def cursor(self, *_a, **_kw):
        return _FakeCursor(self)

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    # what the guard is judged on
    def inserted(self):
        return any("INSERT INTO discovered_facilities" in s for s in self.statements)

    def probed(self):
        return any("canonical_slug = %s" in s for s in self.statements)


def _candidate(**over):
    fac = {"name": SRN["name"], "provider": SRN["provider"], "city": "Fort Pierce",
           "state": "FL", "country": "US", "latitude": None, "longitude": None,
           "power_mw": None, "sqft": None, "status": "Announced",
           "source": "competitor_gap:cloudscene",
           "source_url": "https://cloudscene.com/data-center/us/fort-pierce/srn-2",
           "confidence_score": 0.55, "discovered_at": "2026-09-12",
           "notes": "", "investment_usd": None, "acreage": None}
    fac.update(over)
    return fac


def _insert(conn, fac):
    import news_facility_extractor as nfe
    return nfe.insert_discovered_facility(conn, fac)


# ── 1. the write refuses a row that would share a page ──────────────────────

def test_a_second_row_for_the_same_page_is_refused():
    """The live defect: same provider+name at a DIFFERENT source_url. The old
    guard saw a new URL and inserted; five of these became five rows."""
    conn = _FakeConn(existing=[(12901642,)])
    assert _insert(conn, _candidate()) is None
    assert conn.probed(), "the identity probe never ran"
    assert not conn.inserted(), "a second row for one page was written"


def test_a_row_wearing_an_ALREADY_FROZEN_slug_is_refused():
    """The second net. A row inserted moments ago has no stored canonical_slug
    yet, so siblings within a run are only visible through provider+name; a row
    frozen long ago is only visible through the stored column. The probe has to
    ask both, and this proves the stored-column arm is asked."""
    conn = _FakeConn(existing=[(999,)])
    _insert(conn, _candidate())
    probe = [s for s in conn.statements if "canonical_slug = %s" in s]
    assert probe and "LOWER(TRIM(COALESCE(provider" in probe[0], (
        "the identity probe asks only one of the two questions")


def test_a_different_facility_is_still_inserted():
    """Non-vacuity. A guard that refused everything would pass both tests
    above and stop ingestion dead."""
    conn = _FakeConn(existing=[])
    assert _insert(conn, _candidate(name="South Reach Networks Vero Beach")) == 4242
    assert conn.inserted() and conn.commits == 1


def test_the_same_name_under_a_different_provider_is_a_different_page():
    """The slug hashes provider|name, so these are two URLs and two buildings.
    A guard keyed on the name alone would merge real facilities."""
    a = build_canonical_slug("Equinix", "AM4")
    b = build_canonical_slug("Digital Realty", "AM4")
    assert a and b and a != b, (a, b)


# ── 2. it fails OPEN, and the rollback is what makes that true ──────────────

def test_a_probe_that_cannot_run_does_not_stop_ingestion():
    """An older schema with no canonical_slug column must not silently refuse
    every row. The probe raises, the guard rolls the aborted transaction back,
    and the insert proceeds — exactly the behaviour that shipped before."""
    conn = _FakeConn(raise_on="canonical_slug = %s")
    assert _insert(conn, _candidate()) == 4242
    assert conn.rollbacks >= 1, "the aborted transaction was never rolled back"
    assert conn.inserted(), "a failed probe refused a row it could not judge"


def test_without_the_rollback_the_insert_could_not_have_run():
    """Control on the fake: it really does refuse statements after an error,
    so the test above is proving the rollback and not the fake's leniency."""
    conn = _FakeConn(raise_on="canonical_slug = %s")
    cur = conn.cursor()
    with pytest.raises(RuntimeError):
        cur.execute("SELECT 1 WHERE canonical_slug = %s", ("x",))
    with pytest.raises(RuntimeError, match="aborted"):
        cur.execute("INSERT INTO discovered_facilities (name) VALUES (%s)", ("x",))


# ── 3. the crawler stops spending its budget on its own repeats ─────────────

def _gaps(cands):
    from routes.competitor_gap_crawler import diff_gaps

    class _Cur:
        def execute(self, *_a, **_kw):
            self._rows = []

        def fetchone(self):
            return None
    return diff_gaps(cands, _Cur())


def _cand(url, name=SRN["name"], operator=SRN["provider"]):
    return {"name": name, "operator": operator, "city": "Fort Pierce",
            "state": "FL", "country": "US", "source_url": url}


def test_one_building_at_five_urls_is_one_gap():
    """seen_urls de-dups on where a candidate was FOUND. Cloudscene listed this
    building at five URLs, so five candidates passed, and the existence probe
    reads the database as it was before the run's own inserts."""
    res = _gaps([_cand(f"https://cloudscene.com/data-center/us/fort-pierce/srn-{i}")
                 for i in range(5)])
    assert len(res["true_gaps"]) == 1, [g["source_url"] for g in res["true_gaps"]]
    assert res["dropped_same_run_repeat"] == 4
    assert res["dropped_existing"] == 0, (
        "a repeat inside one run is not 'we already had it' — that counter is "
        "what check_gap_coverage.py reads to call a source tapped out")


def test_distinct_buildings_are_still_distinct_gaps():
    """Non-vacuity for the de-dup above."""
    res = _gaps([_cand("https://cloudscene.com/a", name="South Reach Networks Fort Pierce"),
                 _cand("https://cloudscene.com/b", name="South Reach Networks Vero Beach"),
                 _cand("https://cloudscene.com/c", name="South Reach Networks Stuart")])
    assert len(res["true_gaps"]) == 3, [g["name"] for g in res["true_gaps"]]
    assert res["dropped_same_run_repeat"] == 0
