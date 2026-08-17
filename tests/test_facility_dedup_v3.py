"""facility_dedup_v3 — anonymous-provider variants (2026-07-28).

The planner is PURE, so these are real unit tests on the shipped function,
extracted with ast (importing routes/* pulls in main.py).

The case that matters most is the one it must REFUSE: 581 of the 1,205
same-(name,city) groups are distinct buildings sharing a generic name
(Amazon IAD85 / IAD75 / IAD96 @ Manassas). A false merge hides a real site.
"""
import ast
import functools
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "facility_dedup_v3.py"
WANT = {"is_anonymous", "plan_group"}


@functools.lru_cache(maxsize=1)
def _mod():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") in ("_ANON", "_COORD_EPS"))]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == WANT, "missing {}".format(WANT - found)
    g = {}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


def _row(i, provider, slug="s", lat=1.0, lon=1.0, mw=None):
    return {"id": i, "provider": provider, "canonical_slug": slug,
            "latitude": lat, "longitude": lon, "power_mw": mw}


# ── the refusal that protects real sites ───────────────────────────────
def test_distinct_real_providers_are_never_merged():
    """Amazon IAD85 / IAD75 / IAD96 @ Manassas -- three real buildings."""
    p = _mod()["plan_group"]([
        _row(1, "Amazon IAD85", lat=38.779, lon=-77.542),
        _row(2, "Amazon IAD75", lat=38.779, lon=-77.543),
        _row(3, "Amazon IAD96", lat=38.780, lon=-77.540),
    ])
    assert p["skip"] == "multiple_real_providers"
    assert p["duplicates"] == []


def test_one_real_provider_plus_a_rival_is_still_refused():
    p = _mod()["plan_group"]([
        _row(1, "Equinix"), _row(2, "Digital Realty"), _row(3, ""),
    ])
    assert p["skip"] == "multiple_real_providers"


# ── the merge it should make ───────────────────────────────────────────
def test_anonymous_variants_fold_into_the_named_row():
    """BrainServe SA + blank + Unknown, all within ~200m."""
    p = _mod()["plan_group"]([
        _row(1, "BrainServe SA", slug="brainserve-sa-x", lat=46.545, lon=6.574),
        _row(2, "", slug="brainserve-y", lat=46.547, lon=6.573),
        _row(3, "Unknown", slug="unknown-brainserve-z", lat=46.547, lon=6.573),
    ])
    assert p["skip"] is None
    assert p["canonical"] == 1
    assert sorted(p["duplicates"]) == [2, 3]


def test_unknown_spellings_all_count_as_anonymous():
    a = _mod()["is_anonymous"]
    for v in ("", "  ", "Unknown", "unknown", "N/A", "none", "-", None):
        assert a(v), repr(v)
    for v in ("Equinix", "AWS", "NTT"):
        assert not a(v)


# ── coordinates VETO, never justify ────────────────────────────────────
def test_far_apart_rows_are_skipped_even_with_the_right_provider_shape():
    p = _mod()["plan_group"]([
        _row(1, "Acme", lat=40.0, lon=-100.0),
        _row(2, "", lat=41.0, lon=-100.0),
    ])
    assert p["skip"] == "coords_far_apart"


def test_missing_coordinates_do_not_block_a_merge():
    """A missing coordinate is not evidence of distance."""
    p = _mod()["plan_group"]([
        _row(1, "Acme", lat=None, lon=None),
        _row(2, "", lat=None, lon=None),
    ])
    assert p["skip"] is None and p["duplicates"] == [2]


# ── canonical selection ────────────────────────────────────────────────
def test_canonical_must_have_a_real_slug():
    """A canonical with no slug cannot receive the consolidated signal."""
    p = _mod()["plan_group"]([_row(1, "Acme", slug=""), _row(2, "")])
    assert p["skip"] == "named_row_has_no_slug"


def test_richest_row_wins_then_lowest_id_for_determinism():
    p = _mod()["plan_group"]([
        _row(5, "Acme", mw=None), _row(3, "Acme", mw=12.0), _row(9, ""),
    ])
    assert p["canonical"] == 3, "the row carrying power_mw should win"
    p2 = _mod()["plan_group"]([_row(7, "Acme"), _row(2, "Acme"), _row(9, "")])
    assert p2["canonical"] == 2, "ties break on lowest id, so plans are stable"


def test_a_group_of_only_anonymous_rows_is_left_alone():
    p = _mod()["plan_group"]([_row(1, ""), _row(2, "Unknown")])
    assert p["skip"] == "no_named_row"


def test_a_group_with_no_anonymous_row_is_left_alone():
    p = _mod()["plan_group"]([_row(1, "Acme"), _row(2, "Acme")])
    assert p["skip"] == "no_anonymous_row"


def test_single_row_is_not_a_group():
    assert _mod()["plan_group"]([_row(1, "Acme")])["skip"] == "single_row"


# ── the write path's own guarantees ────────────────────────────────────
def _code():
    return "\n".join(ln for ln in SRC.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


def test_apply_reasserts_preconditions_at_write_time():
    seg = _code().split("def apply", 1)[1].split("def undo", 1)[0]
    assert "COALESCE(is_duplicate, 0) = 0" in seg, (
        "a row suppressed between analyze and apply must be left alone")
    assert "duplicate_of_id IS NULL" in seg, (
        "an existing POINTER is another pass's actual verdict and must never "
        "be overwritten")
    assert "id <> %s" in seg, "a row can never be its own duplicate"


def test_apply_is_pointer_only_and_never_suppresses():
    """is_duplicate=1 left 57 of 58 slugs with no keeper. Never again.

    is_duplicate is a VISIBILITY flag: setting it drops the row from every
    filtered count and from the sitemap. Consolidation needs only the pointer —
    the page then emits rel=canonical at the twin while staying live and 200.
    """
    seg = _code().split("def apply", 1)[1].split("def undo", 1)[0]
    assert "is_duplicate = 1" not in seg, (
        "apply must never suppress a row; write the pointer only")
    assert "SET duplicate_of_id = %s" in seg


def test_undo_clears_only_what_apply_wrote():
    seg = _code().split("def undo", 1)[1]
    assert "is_duplicate = 0" not in seg, (
        "undo must not write is_duplicate — apply never set it, so restoring "
        "it would be changing a value this module did not touch")
    assert "duplicate_of_id = NULL" in seg and "dedup_method = NULL" in seg


def test_undo_only_clears_this_modules_marks():
    seg = _code().split("def undo", 1)[1]
    assert "WHERE dedup_method = %s" in seg, (
        "a v3 undo must never roll back a v2 decision")


def test_apply_requires_explicit_confirmation():
    seg = _code().split("def apply", 1)[1].split("def undo", 1)[0]
    assert 'confirm' in seg and '"1"' in seg


def test_writes_take_a_primary_connection_not_a_hand_rolled_dsn():
    """apply/undo must not roll their own psycopg2.connect(DATABASE_URL).

    ★ The first apply died with "cannot execute UPDATE in a read-only
    transaction" while analyze read fine: a hand-rolled connection landed on a
    read-only endpoint in the web process. The app's pooled get_db() is the
    blessed writable path.
    """
    code = _code()
    for fn in ("def apply", "def undo"):
        seg = code.split(fn, 1)[1].split("\n@", 1)[0]
        assert "_conn(write=True)" in seg, (
            "{} must request a WRITE connection".format(fn))


def test_analyze_surfaces_the_write_connection_state():
    """A read-only surprise must appear in the DRY RUN, not as a 500 halfway
    through the apply."""
    code = _code()
    seg = code.split("def analyze", 1)[1].split("\n@", 1)[0]
    assert "_write_diag()" in seg
    diag = code.split("def _conn_diag", 1)[1].split("\ndef ", 1)[0]
    assert "transaction_read_only" in diag and "pg_is_in_recovery" in diag


# ── ★★ the scan must not re-report its own output (2026-08-16) ─────────
# This lane's ONLY output is `duplicate_of_id`. Its prefilter gates on
# `is_duplicate`, which apply deliberately never writes (pointer-only), so
# nothing this lane does can move a row out of its own selection set. Measured
# live 2026-08-16, before the fix: /analyze reported groups_planned=671 and
# urls_removed=675 where apply would mark 58 — 617 phantom rows, a 12x
# over-report, and 614 of the 671 groups could only issue an UPDATE matching
# nothing. Same class as the fiber-provider scan (#2757), but louder: that one
# still reported `fixable: 0` truthfully.

_COLLECT = {"_collect", "plan_group", "is_anonymous", "_GROUP_SQL"}


@functools.lru_cache(maxsize=1)
def _collect_mod():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _COLLECT)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") in
                ("_ANON", "_COORD_EPS", "_GROUP_SQL"))]
    g = {}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


class _FakeCur:
    """Serves _GROUP_SQL's column order and counts the queries issued."""

    def __init__(self, rows):
        self._rows, self.queries = rows, []

    def execute(self, sql, args=None):
        self.queries.append((sql, args))

    def fetchall(self):
        return self._rows


def _grow(nm, ci, i, provider, pointed=None, slug="s", lat=1.0, lon=1.0, mw=None):
    """One _GROUP_SQL row: (nm, ci, id, provider, slug, lat, lon, mw, dup_of)."""
    return (nm, ci, i, provider, slug, lat, lon, mw, pointed)


def test_a_row_this_lane_already_pointed_is_not_counted_as_work_again():
    """★The defect. apply's UPDATE carries `AND duplicate_of_id IS NULL`, so an
    already-pointed row can only produce rowcount 0 — counting it as an
    outstanding URL makes /analyze describe work that cannot happen."""
    m = _collect_mod()
    cur = _FakeCur([
        _grow("acme dc1", "ashburn", 1, "Acme Corp"),      # named canonical
        _grow("acme dc1", "ashburn", 2, "unknown", pointed=1),   # ALREADY marked
        _grow("acme dc1", "ashburn", 3, ""),               # still to mark
    ])
    plans, stats = m["_collect"](cur)
    assert len(plans) == 1
    assert plans[0]["canonical"] == 1
    assert plans[0]["duplicates"] == [3], (
        "id 2 already carries a pointer — apply would skip it, so analyze must "
        "not report it as a URL it will remove")
    assert stats.get("planned") == 1


def test_a_group_with_nothing_left_to_mark_stops_being_planned():
    """614 of 671 live groups are exactly this shape. Before the fix each one
    produced a plan AND an UPDATE that could only match zero rows."""
    m = _collect_mod()
    cur = _FakeCur([
        _grow("acme dc1", "ashburn", 1, "Acme Corp"),
        _grow("acme dc1", "ashburn", 2, "unknown", pointed=1),
    ])
    plans, stats = m["_collect"](cur)
    assert plans == [], "a fully-marked group is finished work, not a plan"
    assert stats.get("already_pointed") == 1, (
        "...and it must stay COUNTABLE — silently vanishing is the other bug")
    assert "planned" not in stats


def test_the_pointed_row_still_takes_part_in_its_group():
    """★THE GUARD ON THE FIX ITSELF. The subtraction is arithmetic, applied
    AFTER plan_group. Filtering `duplicate_of_id IS NULL` in _GROUP_SQL instead
    would drop the row out of its GROUP — and here that would silently turn a
    correctly-REFUSED merge into an accepted one, because the second real
    provider is the row that got dropped."""
    m = _collect_mod()
    cur = _FakeCur([
        _grow("dc1", "manassas", 1, "Amazon"),
        _grow("dc1", "manassas", 2, "Microsoft", pointed=9),   # rival, marked
        _grow("dc1", "manassas", 3, "unknown"),
    ])
    plans, stats = m["_collect"](cur)
    assert plans == [], "two real providers must still veto, pointed or not"
    assert stats.get("multiple_real_providers") == 1


def test_the_prefilter_reads_the_pointer_but_does_not_filter_on_it():
    """The deliberate half. Selecting the column is what makes the subtraction
    possible; putting it in the WHERE would change grouping, which is a
    different decision with its own before/after."""
    sql = _collect_mod()["_GROUP_SQL"]
    assert "duplicate_of_id" in sql.split("WHERE")[0], (
        "_GROUP_SQL must SELECT duplicate_of_id — without it the scan cannot "
        "tell its own output from outstanding work")
    assert "duplicate_of_id" not in sql.split("WHERE")[1], (
        "filtering it here would drop already-pointed rows out of their "
        "groups and change canonical election; that is not this change")


def test_the_subtraction_costs_no_extra_query():
    """The pointer comes off the group's own rows. A per-group lookup would
    trade a wrong number for 671 round trips."""
    m = _collect_mod()
    cur = _FakeCur([
        _grow("acme dc1", "ashburn", 1, "Acme Corp"),
        _grow("acme dc1", "ashburn", 2, "unknown", pointed=1),
        _grow("acme dc1", "ashburn", 3, ""),
        _grow("beta dc", "dallas", 4, "Beta Inc"),
        _grow("beta dc", "dallas", 5, "n/a"),
    ])
    m["_collect"](cur)
    assert len(cur.queries) == 1, cur.queries


def test_limit_counts_real_plans_not_finished_ones():
    """?limit=1 must return one thing to DO, not one thing already done."""
    m = _collect_mod()
    cur = _FakeCur([
        _grow("done", "x", 1, "Acme Corp"),
        _grow("done", "x", 2, "unknown", pointed=1),
        _grow("todo", "y", 3, "Beta Inc"),
        _grow("todo", "y", 4, "unknown"),
    ])
    plans, _ = m["_collect"](cur, None, 1)
    assert len(plans) == 1 and plans[0]["duplicates"] == [4]
