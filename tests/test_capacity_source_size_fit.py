"""Capacity Source size search means "does this fit my requirement" (2026-09-16).

★ WHAT WAS WRONG

A listing's headline capacity hid two facts that decide whether it fits a
buyer at all, and the size filter read only the headline:

  * an enterprise colo provider may have 2 MW available but only 500 kW
    CONTIGUOUS — it came back for a 1 MW search it cannot serve;
  * a 40 MW provider may be willing to cut it up and contract as little as
    1 MW — a 500 kW search got it anyway, and a 2 MW search could not tell
    that it would deal at all.

So `detail.contiguous_kw` (the largest single contiguous block) and
`detail.min_contract_kw` (the smallest chunk the provider will contract) are
reserved detail keys, in kW like min_kw so nothing converts, carried on the
teaser, and read by _size_sql as the ceiling and the floor a requested size
has to sit between.

WHAT THIS PINS

  F1  the two scenarios above, through the real feed;
  F2  a listing declaring NEITHER key keeps the original rule exactly — the
      same slugs the total-only predicate returned;
  F3  the four combinations of declared/undeclared, at their boundaries;
  F4  min_mw is min_kw in the bigger unit, so the rule reaches it too;
  F5  _db_count_matching agrees with the feed for the same requirement — one
      matcher, not two;
  F6  the predicate is one parenthesised expression holding no " AND " and no
      SQL comment, so a WHERE clause joined with " AND " still splits into its
      predicates and whitespace normalisation cannot swallow it.

The stand-in, the row shape and the WHERE-splitting cursor are
test_capacity_source_search.py's own, driven here over listings that declare
the new keys; what the predicate means to Postgres is checked against a real
database in test_capacity_source_search_sql.py (F7..F9 there).

Run: python3 -m pytest tests/test_capacity_source_size_fit.py -rEf
"""
import sys
import types

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

import routes.exclusive_listings as el  # noqa: E402

from tests.test_capacity_source_search import _Store, _listing  # noqa: E402

# The owner's two cases, plus one listing per remaining combination and a
# control that declares neither. Every listing is live and in one market, so a
# bare size filter is the only thing that separates them.
FIT_LISTINGS = [
    # 2 MW of colocation space, but the biggest single block is 500 kW.
    _listing("colo-2mw-500-contiguous", capacity_mw=2.0,
             detail={"delivery_type": "colocation",
                     "colocation": {"kw_available": 2000},
                     "contiguous_kw": 500}),
    # 40 MW, and the provider will cut it up down to 1 MW.
    _listing("shell-40mw-from-1mw", capacity_mw=40.0,
             detail={"delivery_type": "powered_shell", "min_contract_kw": 1000}),
    # Both declared: it deals between 250 kW and 5 MW.
    _listing("turnkey-band-250-to-5000", capacity_mw=30.0,
             detail={"delivery_type": "turnkey",
                     "contiguous_kw": 5000, "min_contract_kw": 250}),
    # Neither declared: the original rule, total = capacity_mw * 1000.
    _listing("land-10mw-plain", capacity_mw=10.0, detail={"delivery_type": "land"}),
    # Neither declared and no total recorded at all.
    _listing("unsized-plain", capacity_mw=None, detail={"delivery_type": "land"}),
]

ALL_SLUGS = sorted(row["slug"] for row in FIT_LISTINGS)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = _Store()
    store.rows = [dict(row, id=i + 1) for i, row in enumerate(FIT_LISTINGS)]
    monkeypatch.setattr(el, "_fetch", store.fetch)
    monkeypatch.setattr(el, "_conn", lambda: pytest.fail("search reaches storage only through _fetch"))
    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    store.client = app.test_client()
    return store


def _feed(env, query):
    r = env.client.get("/api/v1/listings?" + query)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True, body
    return sorted(item["slug"] for item in body["items"])


# ── F1: the two cases the owner named ─────────────────────────────────────

def test_f1_two_mw_with_500_kw_contiguous_is_not_a_one_mw_answer(env):
    """It has 2 MW, but not 1 MW of it in one piece."""
    assert "colo-2mw-500-contiguous" not in _feed(env, "min_kw=1000")
    # and it is exactly the right answer for a 400 kW requirement.
    assert "colo-2mw-500-contiguous" in _feed(env, "min_kw=400")


def test_f1_forty_mw_contracting_from_one_mw_answers_two_mw_not_500_kw(env):
    """It will cut 40 MW up, but not below 1 MW."""
    assert "shell-40mw-from-1mw" in _feed(env, "min_kw=2000")
    assert "shell-40mw-from-1mw" not in _feed(env, "min_kw=500")


# ── F2: a listing declaring neither key is untouched ──────────────────────

@pytest.mark.parametrize("min_kw,matches", [
    (1, True), (9999, True), (10000, True), (10001, False), (40000, False),
])
def test_f2_a_listing_declaring_neither_key_keeps_the_original_total_rule(env, min_kw, matches):
    """10 MW, nothing declared: total >= min_kw, the rule that always applied,
    boundary included."""
    assert ("land-10mw-plain" in _feed(env, f"min_kw={min_kw}")) is matches


def test_f2_a_listing_of_unrecorded_size_is_still_left_out_of_the_feed(env):
    """Unknown size has never matched the feed, and still does not — while it
    stays a possible match for a standing requirement."""
    assert "unsized-plain" not in _feed(env, "min_kw=1")
    assert _feed(env, "") == ALL_SLUGS
    assert el._db_count_matching({"capacity_kw": 1_000_000}) >= 1


# ── F3: the four combinations, at their boundaries ────────────────────────

@pytest.mark.parametrize("slug,min_kw,matches", [
    # contiguous only: ceiling is the contiguous block, not the 2 MW total.
    ("colo-2mw-500-contiguous", 499, True),
    ("colo-2mw-500-contiguous", 500, True),
    ("colo-2mw-500-contiguous", 501, False),
    ("colo-2mw-500-contiguous", 2000, False),
    # min_contract only: floor is the chunk, ceiling is the total.
    ("shell-40mw-from-1mw", 999, False),
    ("shell-40mw-from-1mw", 1000, True),
    ("shell-40mw-from-1mw", 40000, True),
    ("shell-40mw-from-1mw", 40001, False),
    # both: the requirement has to sit in the band.
    ("turnkey-band-250-to-5000", 249, False),
    ("turnkey-band-250-to-5000", 250, True),
    ("turnkey-band-250-to-5000", 5000, True),
    ("turnkey-band-250-to-5000", 5001, False),
])
def test_f3_the_fit_rule_at_each_boundary(env, slug, min_kw, matches):
    assert (slug in _feed(env, f"min_kw={min_kw}")) is matches


# ── F4: min_mw is the same rule in the bigger unit ────────────────────────

@pytest.mark.parametrize("mw,kw", [(0.5, 500), (1, 1000), (2, 2000), (5, 5000), (40, 40000)])
def test_f4_min_mw_matches_what_min_kw_matches_at_a_thousand_times(env, mw, kw):
    assert _feed(env, f"min_mw={mw}") == _feed(env, f"min_kw={kw}")


def test_f4_min_mw_reads_the_contiguous_block_too(env):
    """The 2 MW / 500 kW listing is the one min_mw used to get wrong."""
    assert "colo-2mw-500-contiguous" not in _feed(env, "min_mw=1")


# ── F5: one matcher — the count agrees with the feed ──────────────────────

@pytest.mark.parametrize("kw", [1, 250, 400, 500, 501, 1000, 2000, 5000, 5001, 40000, 40001])
def test_f5_the_requirement_count_agrees_with_the_feed(env, kw):
    """_db_count_matching uses the SAME _size_sql, so it counts the listings
    the feed shows — plus the ones whose size is not recorded, which it keeps
    as possible matches and the feed leaves out."""
    shown = _feed(env, f"min_kw={kw}")
    unsized = [row["slug"] for row in FIT_LISTINGS
               if row["capacity_mw"] is None and not row["detail"].get("contiguous_kw")]
    assert el._db_count_matching({"capacity_kw": kw}) == len(shown) + len(unsized)


def test_f5_capacity_mw_in_a_requirement_goes_through_the_same_matcher(env):
    assert (el._db_count_matching({"capacity_mw": 2})
            == el._db_count_matching({"capacity_kw": 2000}))


# ── F6: the predicate's shape ─────────────────────────────────────────────

def test_f6_the_size_predicate_is_one_splittable_comment_free_expression():
    for unknown_matches in (False, True):
        sql, params = el._size_sql(1000, unknown_matches=unknown_matches)
        assert sql.startswith("(") and sql.endswith(")")
        # A WHERE clause is joined with " AND " and split back on it.
        assert " AND " not in sql, sql
        # Callers normalise whitespace, so a `--` would eat the rest of the
        # statement once the newlines are gone.
        assert "--" not in sql, sql
        # One requested size, bound to every branch that compares against it.
        assert params == [1000, 1000, 1000, 1000]
        assert sql.count("%s") == len(params)
    # Both new keys are read, and every JSON number is cast only behind a
    # jsonb_typeof guard, so text in the field raises nothing.
    sql = el._size_sql(1)[0]
    for key in ("min_contract_kw", "contiguous_kw", "colocation'->'kw_available"):
        assert f"jsonb_typeof(detail->'{key}') = 'number'" in sql, key


def test_f6_a_size_filter_binds_its_parameters_inside_the_where_clause(env):
    _feed(env, "min_kw=1234")
    # The feed also asks for the live count; the size predicate belongs to the
    # statement that selects the rows.
    sized = [(sql, params) for sql, params in env.statements if el._size_sql(1234)[0] in sql]
    assert len(sized) == 1, [sql for sql, _ in env.statements]
    statement, params = sized[0]
    assert params.count(1234) == 4, params
    # Nothing is interpolated: the requested size appears only as a parameter.
    assert "1234" not in statement
