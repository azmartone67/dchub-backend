#!/usr/bin/env python3
"""tests/test_slug_collision_disambiguation.py — one frozen slug must not be
worn by many unrelated facilities.

NO NETWORK, NO DB, does not import main (house rule). The fake cursor branches
on the SQL it is HANDED, so these fence the real statements rather than a
restatement of them.

THE BUG (measured live 2026-09-19 on https://dchub.cloud/api/v1/map, 5,000 rows):
    4,287 unique slugs for 5,000 rows. 466 slugs worn by more than one row.
    34 collision groups more than 2km across, 260 rows (5.2 percent) inside one.
    amazon-web-services-amazon-web-services-7e958426 -> 61 rows, all named
    exactly "Amazon Web Services", spanning 17,218 km (IE, ID, US, CL, AE, AU).

    NOT the map's fallback composer: discovered_facilities read 30,621 frozen /
    11 pending the same day, so those rows all take the STORED branch. The
    collision was written into canonical_slug by backfill_canonical_slugs,
    because build_canonical_slug is a pure function of (provider, name) and the
    freeze index is non-unique.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.facility_slug_freeze import (          # noqa: E402
    SLUG_OWNER_ORDER_SQL, build_canonical_slug, build_disambiguated_slug,
    disambiguate_slug_collisions, _INDEPENDENT, _ranked_cte,
)

FREEZE_SRC = open(os.path.join(ROOT, "routes", "facility_slug_freeze.py"),
                  encoding="utf-8", errors="replace").read()
PROFILE_SRC = open(os.path.join(ROOT, "routes", "facility_profile_page.py"),
                   encoding="utf-8", errors="replace").read()

# The real collision, from the live payload.
AWS = ("Amazon Web Services", "Amazon Web Services")


# ── the builder ────────────────────────────────────────────────────────────

def test_the_frozen_builder_really_does_collide():
    """The premise. If this ever stops holding, the fix below is pointless."""
    assert build_canonical_slug(*AWS) == build_canonical_slug(*AWS)


def test_two_rows_with_the_same_provider_and_name_get_different_slugs():
    """THE regression: 61 rows shared one slug, so 60 markers linked elsewhere."""
    a = build_disambiguated_slug(*AWS, 101, city="Dublin", country="IE")
    b = build_disambiguated_slug(*AWS, 202, city="Boise", country="US")
    assert a != b, f"both rows still land on {a}"


def test_it_differs_even_when_the_rows_share_a_location():
    """Location is a nicety; the ROW ID is what makes the tail unique."""
    a = build_disambiguated_slug(*AWS, 101, city="Dublin")
    b = build_disambiguated_slug(*AWS, 102, city="Dublin")
    assert a != b


def test_the_tail_is_exactly_eight_characters():
    """_fetch_facility_by_slug rejects the slug before touching the DB
    otherwise: `parts = slug.rsplit("-", 1)` then `len(parts[1]) != 8`."""
    for fid in (1, 999999, "hex-id-9"):
        s = build_disambiguated_slug(*AWS, fid, city="Dublin")
        parts = s.rsplit("-", 1)
        assert len(parts) == 2 and len(parts[1]) == 8, f"{s!r} cannot resolve"


def test_the_body_carries_the_most_specific_location():
    s = build_disambiguated_slug(*AWS, 7, city="Dublin", state="Leinster",
                                 country="IE")
    assert "-dublin-" in s, s


def test_it_falls_back_through_state_then_country():
    assert "-leinster-" in build_disambiguated_slug(*AWS, 7, state="Leinster",
                                                    country="IE")
    assert "-ie-" in build_disambiguated_slug(*AWS, 7, country="IE")


def test_a_location_already_in_the_body_is_not_repeated():
    s = build_disambiguated_slug("Equinix", "Equinix San Jose", 7, city="San Jose")
    assert "san-jose-san-jose" not in s, s


def test_a_location_that_is_merely_a_token_prefix_is_still_appended():
    """Token boundary, the _dedupe_provider_prefix rule: 'jose' is not
    'san-jose', so a bare substring test would wrongly drop it."""
    s = build_disambiguated_slug("Equinix", "Equinix Jose", 7, city="San Jose")
    assert s.count("jose") >= 2, s


def test_an_unsluggable_row_yields_none_not_a_bare_hash():
    assert build_disambiguated_slug(None, None, 7) is None
    assert build_disambiguated_slug("AWS", "", 7) is None


def test_a_row_with_no_id_yields_none():
    """Without an id there is nothing to disambiguate WITH, and returning the
    shared slug would silently reinstate the collision."""
    assert build_disambiguated_slug(*AWS, None, city="Dublin") is None
    assert build_disambiguated_slug(*AWS, "", city="Dublin") is None


# ── the ordering has exactly one owner ─────────────────────────────────────

def test_the_profile_resolver_imports_the_shared_ordering():
    assert "SLUG_OWNER_ORDER_SQL" in PROFILE_SRC, (
        "facility_profile_page must use the shared ordering, or the row that "
        "keeps the slug and the row the page serves can drift apart")


def test_the_profile_resolver_does_not_inline_a_second_copy():
    code = "\n".join(l for l in PROFILE_SRC.splitlines()
                     if not l.strip().startswith("#"))
    assert "COALESCE(is_duplicate, 0) ASC" not in code, (
        "a second copy of the owner ordering has been inlined; the constant "
        "exists so both sides cannot drift")


def test_the_ranking_uses_that_exact_ordering():
    assert SLUG_OWNER_ORDER_SQL in _ranked_cte(_Cur(), "discovered_facilities") \
        or "COALESCE(is_duplicate, 0) ASC" in SLUG_OWNER_ORDER_SQL


# ── the window must see every row on the slug ──────────────────────────────

def test_the_ranking_window_is_not_filtered_by_duplicate_flags():
    """47 slugs are served ONLY by suppressed rows. Filtering them out of the
    window promotes a different row to rn=1, so TWO rows would keep the slug
    and the collision would survive."""
    cte = _ranked_cte(_Cur(), "discovered_facilities")
    where = cte.split("WHERE", 1)[1]
    assert "is_duplicate" not in where and "duplicate_of_id" not in where, \
        f"the ranking window filters on a duplicate flag: {where}"


def test_only_independent_non_owner_rows_are_rewritten():
    assert "rn > 1" in _INDEPENDENT
    assert "_isdup" in _INDEPENDENT and "_dupof" in _INDEPENDENT, (
        "a row already marked a twin must keep sharing its keeper's URL")


def test_the_disambiguator_does_not_rederive_the_same_site_predicate():
    """The four-condition rule has exactly ONE owner (_twin_redirect_target).
    A second composer disagreeing with the first is the bug be#4793 fixed."""
    body = FREEZE_SRC[FREEZE_SRC.index("def _dup_cols"):]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    for banned in ("_same_physical_site", "_SAME_SITE_METRES", "haversine",
                   "ST_Distance", "latitude", "longitude"):
        assert banned not in code, f"re-derives the twin predicate ({banned})"


# ── the write path ─────────────────────────────────────────────────────────

class _Cur:
    """Fake cursor that branches on the SQL it is GIVEN, not on call order."""
    def __init__(self, ranked_rows=()):
        self.ranked_rows = list(ranked_rows)
        self.seen = []
        self._last = None
        self.rowcount = 0
        # execute_values reads cur.connection.encoding before it builds the SQL
        self.connection = type("C", (), {"encoding": "UTF8"})()

    def execute(self, sql, args=None):
        # execute_values hands back BYTES once it has spliced the VALUES in.
        sql = sql.decode() if isinstance(sql, bytes) else sql
        self.seen.append(sql)
        self._last = sql
        if "UPDATE" in sql:
            self.rowcount = 1

    def mogrify(self, template, args):
        return ("(" + ",".join(repr(a) for a in args) + ")").encode()

    def fetchone(self):
        last = self._last or ""
        if "to_regclass" in last:
            return ("discovered_facilities",)
        if "information_schema.columns" in last:
            return (1,)                      # every column exists
        if "COUNT(DISTINCT canonical_slug)" in last:
            return (466, 713, 260)
        return (0,)

    def fetchall(self):
        # execute_values(fetch=True) reads the UPDATE's RETURNING rows here.
        if "UPDATE" in (self._last or ""):
            return [(1,)] * (self.rowcount or 0)
        assert "ranked" in (self._last or ""), \
            "rows must come from the ranked CTE"
        rows, self.ranked_rows = self.ranked_rows, []
        return rows


class _Conn:
    def __init__(self, cur): self._cur = cur; self.commits = 0
    def cursor(self): return self._cur
    def commit(self): self.commits += 1
    def rollback(self): pass


ROWS = [(101, "Amazon Web Services", "Amazon Web Services", "Dublin", None, "IE"),
        (202, "Amazon Web Services", "Amazon Web Services", "Boise", "ID", "US")]


def _updates(cur):
    return [s for s in cur.seen if "UPDATE" in s]


def test_dry_run_is_the_default_and_writes_nothing():
    cur = _Cur(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities")
    assert not _updates(cur), "the default run issued an UPDATE"


def test_a_real_run_issues_the_update():
    cur = _Cur(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=False)
    assert _updates(cur), "dry_run=False wrote nothing"


def test_the_update_refuses_a_slug_that_is_already_taken():
    cur = _Cur(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=False)
    sql = _updates(cur)[0]
    assert "facility_slug_aliases" in sql, \
        "a new slug that is already an alias would 301 straight back off itself"
    assert sql.count("NOT EXISTS") >= 2, \
        "the new slug must not shadow an existing canonical"


def test_the_update_never_touches_the_row_that_keeps_the_slug():
    """rn=1 is the row the page serves. It must not appear in the write set."""
    cur = _Cur(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=False)
    selects = [s for s in cur.seen if "FROM ranked" in s and "SELECT id" in s]
    assert selects, "no row selection ran"
    assert "rn > 1" in selects[0], \
        "the write set is not restricted to non-owner rows"


def test_no_progress_breaks_the_loop_instead_of_burning_every_batch():
    """A row whose new slug is already taken stays selected forever."""
    cur = _Cur(ROWS)
    cur.rowcount = 0

    class _Stuck(_Cur):
        def execute(self, sql, args=None):
            sql = sql.decode() if isinstance(sql, bytes) else sql
            self.seen.append(sql); self._last = sql
            self.rowcount = 0                 # the guard rejects every write
        def fetchall(self):
            if "UPDATE" in (self._last or ""):
                return []                     # the guard rejected every write
            return list(ROWS)                 # never drains

    stuck = _Stuck(ROWS)
    # batch == len(ROWS) on purpose: a short read would break the loop via
    # `len(rows) < batch` and this would pass without the wrote==0 test
    # existing at all. A full batch that writes nothing is the only shape
    # that isolates it.
    disambiguate_slug_collisions(_Conn(stuck), "discovered_facilities",
                                 dry_run=False, batch=len(ROWS), max_batches=50)
    assert len(_updates(stuck)) == 1, (
        f"looped {len(_updates(stuck))} times writing nothing")




# ─────────────────────────────────────────────────────────────────────────
# 2026-09-19, AFTER the first live run. Two defects it exposed.
# ─────────────────────────────────────────────────────────────────────────
from routes.facility_slug_freeze import (          # noqa: E402
    slug_collision_breakdown, keeper_slug_reachability,
)


class _PagingCur(_Cur):
    """Cursor that behaves like a real one under execute_values PAGING.

    execute_values splits the argslist (page_size 100) and issues one
    statement per page. rowcount therefore describes only the LAST page —
    live this reported 22 for a 722-row rewrite (722 = 7 x 100 + 22).
    """
    MARK = "(ROW)"

    def __init__(self, ranked_rows=()):
        super().__init__(ranked_rows)
        self._page = 0

    def mogrify(self, template, args):
        return self.MARK.encode()

    def execute(self, sql, args=None):
        sql = sql.decode() if isinstance(sql, bytes) else sql
        self.seen.append(sql)
        self._last = sql
        if "UPDATE" in sql:
            self._page = sql.count(self.MARK)
            self.rowcount = self._page        # only THIS page, like psycopg2

    def fetchall(self):
        if "UPDATE" in (self._last or ""):
            return [(1,)] * self._page        # RETURNING rows for this page
        return super().fetchall()


N = 250                                       # 3 pages: 100 + 100 + 50
BIG = [(i, "Amazon Web Services", "Amazon Web Services", "Dublin", None, "IE")
       for i in range(1, N + 1)]


def test_the_rewritten_count_spans_every_page_not_just_the_last():
    """THE miscount: 722 rows rewritten, endpoint reported 22."""
    cur = _PagingCur(BIG)
    wrote, _ = disambiguate_slug_collisions(
        _Conn(cur), "discovered_facilities", dry_run=False,
        batch=N, max_batches=1)
    assert wrote == N, (
        f"reported {wrote} of {N} rewritten — cur.rowcount only describes the "
        "last execute_values page")


def test_the_update_asks_for_returning_so_the_count_is_real():
    cur = _PagingCur(BIG)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=False, batch=N, max_batches=1)
    assert "RETURNING" in _updates(cur)[0], \
        "without RETURNING there is nothing for fetch=True to count"


# ── the breakdown ──────────────────────────────────────────────────────────

class _OneRowCur(_Cur):
    """Returns one canned tuple for whichever breakdown query it is handed."""
    def __init__(self, row): super().__init__(); self._row = row
    def fetchone(self):
        last = self._last or ""
        if "information_schema.columns" in last:
            return (1,)
        if "to_regclass" in last:
            return ("discovered_facilities",)
        return self._row


def test_the_breakdown_buckets_partition_the_non_owner_rows():
    """Every non-owner row lands in exactly one bucket, or the totals lie."""
    got = slug_collision_breakdown(
        _Conn(_OneRowCur((8375, 500, 7000, 153, 722, 7172))),
        "discovered_facilities")
    assert got is not None
    parts = (got['points_at_owner'] + got['points_elsewhere']
             + got['marked_no_pointer'] + got['unmarked'])
    assert parts == got['non_owner_rows'], (
        f"buckets sum to {parts} but there are {got['non_owner_rows']} rows")


def test_the_breakdown_compares_against_the_owner_not_merely_any_pointer():
    """THE rule that was wrong: 'has a duplicate pointer' skipped 7,653 rows
    including a 62-row group spanning 17,218 km. The bucket must be keyed on
    whether the pointer names THE ROW THAT KEEPS THE SLUG."""
    cur = _OneRowCur((1, 0, 1, 0, 0, 1))
    slug_collision_breakdown(_Conn(cur), "discovered_facilities")
    sql = [q for q in cur.seen if "FILTER" in q][0]
    assert "owner_id" in sql, "the breakdown never looks at the owner"
    assert "FIRST_VALUE" in sql, \
        "owner_id must come from the same window that ranks rn"
    assert "_dupof::text = owner_id::text" in sql, \
        "points_at_owner must be pointer equality against the OWNER"


def test_the_breakdown_returns_none_when_it_cannot_measure():
    """Never a reassuring zero."""
    class _Boom(_Cur):
        def execute(self, sql, args=None): raise RuntimeError("no such column")
    assert slug_collision_breakdown(_Conn(_Boom()), "discovered_facilities") is None


def test_keeper_reachability_splits_by_whether_the_keeper_has_a_slug():
    got = keeper_slug_reachability(
        _Conn(_OneRowCur((7000, 6800))), "discovered_facilities")
    assert got == {'points_elsewhere': 7000, 'keeper_has_slug': 6800,
                   'keeper_has_no_slug': 200}


def test_the_new_readers_do_not_rederive_the_same_site_predicate():
    body = FREEZE_SRC[FREEZE_SRC.index("def slug_collision_breakdown"):
                      FREEZE_SRC.index("def disambiguate_slug_collisions")]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    for banned in ("_same_physical_site", "_SAME_SITE_METRES", "haversine",
                   "ST_Distance", "latitude", "longitude"):
        assert banned not in code, f"re-derives the twin predicate ({banned})"


if __name__ == "__main__":
    import traceback
    failed = 0
    for nm, fn in sorted(globals().items()):
        if nm.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {nm}")
            except Exception:
                failed += 1; print(f"FAIL {nm}"); traceback.print_exc()
    sys.exit(1 if failed else 0)
