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
    SLUG_OWNER_ORDER_SQL, SLUG_OWNER_ORDER_TMPL, _dup_cols, _dry_run_flag,
    _REMINTABLE_TABLES, build_canonical_slug, build_disambiguated_slug,
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


def test_the_ranking_formats_the_shared_template():
    """★ The previous form of this guard was VACUOUS: disjunct 1 was already
    False (every CTE line-wraps its copy, so the one-line constant was never a
    substring) and disjunct 2 compared the constant to its own prefix. Mutating
    the ranking to `ORDER BY id DESC` — which hands rn=1 to the OPPOSITE row
    from the one the page serves — left it green, while FIVE hand-written
    copies accumulated underneath it."""
    cte = _ranked_cte(_Cur(), "discovered_facilities")
    expected = SLUG_OWNER_ORDER_TMPL.format(isdup="is_duplicate",
                                            power="power_mw")
    assert expected in cte, (
        "the ranking does not use the shared ordering; rn=1 can name a "
        f"different row than _fetch_facility_by_slug serves.\nwant: {expected}")
    assert expected == SLUG_OWNER_ORDER_SQL, \
        "the page constant and the ranking template have drifted apart"


def test_the_freeze_module_never_retypes_the_ordering():
    code = "\n".join(l for l in FREEZE_SRC.splitlines()
                     if not l.strip().startswith("#"))
    n = code.count("COALESCE({isdup}, 0) ASC")
    assert n == 1, (f"the ordering is written out {n} times; define it once "
                    "and format it")
    assert "COALESCE(is_duplicate, 0) ASC" not in code, \
        "a hand-typed copy of the owner ordering is back in this module"


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
        self._args = args
        if "UPDATE" in sql:
            self.rowcount = 1

    def mogrify(self, template, args):
        return ("(" + ",".join(repr(a) for a in args) + ")").encode()

    def fetchone(self):
        last = self._last or ""
        if "to_regclass" in last:
            return ("discovered_facilities",)
        if "information_schema.columns" in last:
            probed = (getattr(self, "_args", None) or (None, None))[-1]
            if probed in getattr(self, "missing_cols", ()):
                return None                  # this table lacks that column
            return (1,)                      # every other column exists
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


ROWS = [(101, "Amazon Web Services", "Amazon Web Services", "Dublin", None,
         "IE", "marked_no_pointer", None),
        (202, "Amazon Web Services", "Amazon Web Services", "Boise", "ID",
         "US", "unmarked", None)]

# a points_elsewhere row: it must ADOPT keeper_slug, never mint a new one.
ADOPT_ROWS = [(303, "Equinix", "Equinix SV3", "San Jose", "CA", "US",
               "points_elsewhere", "equinix-sv3-keeper-abcd1234")]


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


def test_blocked_rows_are_stepped_OVER_not_re_read_forever():
    """A row whose new slug is already taken stays selected forever.

    ★ r-slugblockers (2026-09-19): this used to assert the `wrote == 0` break.
    That break was the DEFECT — it ended the run on the first fully blocked
    batch and left every later fixable row untouched. #4830 fixed the COUNT
    feeding it; the break still read that count as progress. The contract is
    now the id cursor: blocked rows are walked PAST.

    ★ The old fake returned the same rows on EVERY read regardless of what it
    was asked — a query the real SQL cannot issue. This one honours the cursor.
    """
    class _Cursoring(_Cur):
        def __init__(self, rows):
            super().__init__(rows)
            self.all_rows = list(rows)
            self.selected = []

        def fetchall(self):
            last = self._last or ""
            if "UPDATE" in last:
                return []                     # the guard rejected every write
            assert "ranked" in last
            # the id cursor is the LAST bound arg: the bucket list is
            # bound ahead of it.
            after = (self._args or ('',))[-1]
            out = [r for r in self.all_rows if str(r[0]) > str(after)]
            self.selected.append([r[0] for r in out])
            return out

    cur = _Cursoring(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=False, batch=len(ROWS), max_batches=50)
    seen = [i for b in cur.selected for i in b]
    assert len(seen) == len(set(seen)), f"re-read the same ids: {cur.selected}"
    assert cur.selected[0] == [101, 202], cur.selected
    assert len(_updates(cur)) <= 2, \
        f"looped {len(_updates(cur))} times over {len(ROWS)} rows"


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
BIG = [(i, "Amazon Web Services", "Amazon Web Services", "Dublin", None,
        "IE", "marked_no_pointer", None)
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


# ── r-slugblockers (2026-09-19): blocking findings from the #4818 review ───

def test_a_table_without_duplicate_columns_gets_TYPED_nulls():
    """★ A bare NULL in a CTE output column is typed `text` by Postgres, so the
    outer COALESCE(_isdup, 0) in _INDEPENDENT raised "COALESCE types text and
    integer cannot be matched" (reproduced on PG 18.6). That killed BOTH stats
    and the re-mint for `facilities` at the safe dry-run default, and the
    swallowed error left collisions: null on the status route permanently."""
    cur = _Cur()
    cur.missing_cols = ("is_duplicate", "duplicate_of_id")
    isdup, dupof, power = _dup_cols(cur, "facilities")
    assert isdup == "NULL::int", f"untyped substitute for is_duplicate: {isdup}"
    assert dupof == "NULL::text", f"untyped substitute: {dupof}"
    assert power == "power_mw"
    assert "NULL::int AS _isdup" in _ranked_cte(cur, "facilities"), \
        "the CTE still emits an untyped NULL; COALESCE(_isdup, 0) will raise"


def test_facilities_is_refused_for_a_real_remint():
    """★ `facilities` rows resolve ONLY via hash8(provider|name), while
    build_disambiguated_slug tails on md5(provider|name|id) — so a re-minted
    slug there is a hard 404 fed into the sitemap, with no alias and so no
    recovery hop. Casting the NULL above without this turns a 500 into that."""
    assert "facilities" not in _REMINTABLE_TABLES
    assert "discovered_facilities" in _REMINTABLE_TABLES
    cur = _Cur(ROWS)
    try:
        disambiguate_slug_collisions(_Conn(cur), "facilities", dry_run=False)
    except ValueError as e:
        assert "measure-only" in str(e)
    else:
        raise AssertionError("a real re-mint of facilities was allowed")
    assert not _updates(cur), "it wrote before refusing"


def test_measuring_facilities_is_still_allowed():
    """The refusal is on the WRITE only — the status route still needs the
    number that sizes the problem."""
    cur = _Cur(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "facilities", dry_run=True)
    assert not _updates(cur)


def test_the_batch_select_carries_an_id_cursor():
    cur = _Cur(ROWS)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=True)
    sel = [x for x in cur.seen if "ranked" in x and "SELECT id" in x][0]
    assert "id::text > %s" in sel, \
        "no id cursor; a blocked row stalls the loop and ends the run early"


def test_dry_run_fails_safe_on_null_and_zero():
    """★ {"dry_run": null} — what a client that serialises unset fields emits —
    reached a bare bool(None) and armed a full rewrite of set-once slugs. So
    did 0 and []. Note the asymmetry this removes: "off" was already safe."""
    for armed in (False, "false", "0", "no", "off", "FALSE"):
        assert _dry_run_flag({'dry_run': armed}) is False, armed
    for safe in (None, 0, [], {}, 1, "true", "yes", object()):
        assert _dry_run_flag({'dry_run': safe}) is True, safe
    assert _dry_run_flag({}) is True


# ─────────────────────────────────────────────────────────────────────────
# 2026-09-19 buckets: WHY a row shares, decides WHAT it gets.
# Measured live after be#4818: of 7,653 non-owner rows,
#   points_at_owner 5,525 · marked_no_pointer 1,468 · points_elsewhere 660
#   (all 660 keepers already have a slug) · unmarked 0
# ─────────────────────────────────────────────────────────────────────────
from routes.facility_slug_freeze import (          # noqa: E402
    _BUCKET_CASE, _REWRITE_BUCKETS, slug_collision_groups,
)


def test_points_at_owner_is_never_rewritten():
    """5,525 rows are twins of the row the page serves. Sharing its URL is
    correct; minting them pages would undo what be#4808 collapses for."""
    assert 'points_at_owner' not in _REWRITE_BUCKETS


def test_every_other_bucket_is_rewritten():
    for b in ('points_elsewhere', 'marked_no_pointer', 'unmarked'):
        assert b in _REWRITE_BUCKETS, f"{b} left colliding"


def test_the_buckets_have_exactly_one_definition():
    """The breakdown publishes a population; the rewrite acts on one. Two
    CASE expressions would let them diverge — be#4818 reported success on a
    defect it never touched, which is that shape."""
    n = FREEZE_SRC.count("WHEN _dupof IS NOT NULL AND _dupof::text = owner_id::text")
    assert n == 1, f"the bucket CASE is written {n} times; it must be one"
    for fn in ("slug_collision_breakdown", "slug_collision_groups",
               "disambiguate_slug_collisions"):
        seg = FREEZE_SRC[FREEZE_SRC.index("def " + fn):]
        seg = seg[:seg.index("\ndef ") if "\ndef " in seg else len(seg)]
        assert "_BUCKET_CASE" in seg, f"{fn} does not read the shared buckets"


def test_the_owner_comes_from_the_same_window_that_ranks_rn():
    cte = _ranked_cte(_Cur(), "discovered_facilities")
    assert "FIRST_VALUE(id) OVER" in cte and "owner_id" in cte
    # both windows must carry the same ORDER BY, or owner_id names a row rn=1
    # does not protect
    assert cte.count("ORDER BY " + SLUG_OWNER_ORDER_TMPL.format(
        isdup="is_duplicate", power="power_mw")) == 2


def _run(rows, **kw):
    cur = _Cur(rows)
    disambiguate_slug_collisions(_Conn(cur), "discovered_facilities",
                                 dry_run=False, **kw)
    return cur


def test_a_points_elsewhere_row_adopts_its_keepers_slug():
    """THE point of this bucket: the marker belongs on the keeper's page.
    Minting a fresh slug would publish a page for a suppressed row."""
    cur = _run(ADOPT_ROWS)
    sql = "\n".join(_updates(cur))
    assert "equinix-sv3-keeper-abcd1234" in sql, \
        "the row did not adopt its keeper's slug"
    assert "pointsan-jose" not in sql and "equinix-sv3-san-jose" not in sql, \
        "a fresh slug was minted for a row that already had a destination"


def test_the_adopt_path_does_not_carry_the_not_taken_guard():
    """Adopting REQUIRES the slug to be taken — by the keeper. Reusing the
    mint guard here would reject every adopt and write nothing."""
    cur = _run(ADOPT_ROWS)
    adopt_sql = [q for q in _updates(cur) if "equinix-sv3-keeper" in q][0]
    assert "NOT EXISTS" not in adopt_sql, \
        "the adopt path refuses the very slug it is trying to adopt"
    assert "EXISTS (SELECT 1" in adopt_sql, \
        "adopt must require the target slug to already exist"


def test_the_mint_path_still_refuses_a_taken_slug():
    cur = _run(ROWS)
    mint_sql = [q for q in _updates(cur) if "NOT EXISTS" in q]
    assert mint_sql, "the mint path lost its not-taken guard"


def test_a_points_elsewhere_row_with_no_keeper_slug_is_left_alone():
    """keeper_has_no_slug was 0 live, but a NULL here must not write ''."""
    rows = [(303, "Equinix", "Equinix SV3", "San Jose", "CA", "US",
             "points_elsewhere", None)]
    cur = _run(rows)
    assert not _updates(cur), "wrote a row whose keeper has no slug"


def test_remaining_counts_the_buckets_the_loop_selects():
    src = FREEZE_SRC[FREEZE_SRC.index("def disambiguate_slug_collisions"):]
    src = src.split("return rewritten")[0]
    # ★ strip comments first. The comment above that return EXPLAINS why
    # independent_non_owner_rows is wrong here, so a raw substring test
    # matched its own rationale and failed on correct code.
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    assert "_REWRITE_BUCKETS" in code, \
        "remaining must be summed over the buckets the loop acts on"
    assert "independent_non_owner_rows" not in code, \
        "remaining still reads the old single-bucket number"


def test_the_group_report_returns_bucket_counts_per_slug():
    class _G(_Cur):
        def fetchall(self):
            if "GROUP BY canonical_slug" in (self._last or ""):
                return [("amazon-web-services-amazon-web-services-7e958426",
                         62, 61, 0, 0, 0)]
            return super().fetchall()
    got = slug_collision_groups(_Conn(_G()), "discovered_facilities", limit=5)
    assert got == [{'slug': "amazon-web-services-amazon-web-services-7e958426",
                    'rows': 62, 'points_at_owner': 61, 'points_elsewhere': 0,
                    'marked_no_pointer': 0, 'unmarked': 0}]


def test_the_group_report_never_reads_coordinates():
    """Span is _twin_redirect_target's question. These counts are keyed by
    slug so the distance is measured outside this module."""
    seg = FREEZE_SRC[FREEZE_SRC.index("def slug_collision_groups"):
                     FREEZE_SRC.index("def disambiguate_slug_collisions")]
    code = "\n".join(l for l in seg.splitlines()
                     if not l.strip().startswith("#"))
    for banned in ("latitude", "longitude", "ST_Distance", "haversine",
                   "_SAME_SITE_METRES", "_same_physical_site"):
        assert banned not in code, f"the group report re-derives span ({banned})"




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
