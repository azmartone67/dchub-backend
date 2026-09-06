"""MCP ecosystem board — the two ranks, and the semantics that keep both honest.

★WHY THIS EXISTS. We publish "#1 on Smithery for data-center / energy / grid"
and it is true. Measured 2026-09-06 we also sit at 3,573 uses, behind at least
161 servers in a 546-server sample of an 11,729-server corpus. A board that
carries only the first number is how a server is #1 on ten terms and nobody's
installed tool.

★THE THREE SEMANTICS UNDER TEST, each one a failure that already happened here:
  · UNREADABLE ≠ ABSENT. A search that did not answer must be None, never
    ">50" — the shape that let five registries read clean for months (#3896).
  · A FLOOR IS NOT A RANK. The registry caps any query at 500 rows and honours
    no sort parameter, so a server outside the sample could sit above us. The
    board may only ever say "no better than #N".
  · SUBMITTED IS NOT LISTED. 43 upstream PRs, 5 merged. A ledger that counts
    submissions reports a loop working 43 times for 5 outcomes.

NO network, NO DB — every test injects a `fetch` and drives the REAL functions.
★EVERY STATEMENT IS INSIDE A FUNCTION — a module-scope exit aborts collection.

Run:  python3 -m pytest tests/test_mcp_ecosystem_board.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import mcp_ecosystem_board as B  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────
def _server(name, uses=0, remote=True, verified=False):
    return {"qualifiedName": name, "useCount": uses, "remote": remote,
            "verified": verified}


def _fake_fetch(by_query, total=11729, dead=()):
    """A registry that answers `by_query` and refuses `dead` terms.

    `by_query[term]` may be a flat list (page 1 only) or {page: [...]}.
    """
    def fetch(url, params):
        if url == B.GITHUB_SEARCH:
            return {"items": []}, None
        q = params.get("q", "")
        if q in dead:
            return None, "HTTP 503"
        entry = by_query.get(q, [])
        if isinstance(entry, dict):
            servers = entry.get(int(params.get("page", 1)), [])
        else:
            servers = entry if int(params.get("page", 1)) == 1 else []
        return ({"servers": servers,
                 "pagination": {"totalCount": total}}, None)
    return fetch


# ── category rank ────────────────────────────────────────────────────
def test_position_one_when_we_lead_the_term():
    rows = B.category_ranks(
        terms=("energy",),
        fetch=_fake_fetch({"energy": [_server(B.OUR_QUALIFIED_NAME),
                                      _server("rival/x")]}, total=153))
    assert rows[0]["position"] == 1
    assert rows[0]["held"] is True
    assert rows[0]["of"] == 153
    assert rows[0]["leader"] == B.OUR_QUALIFIED_NAME


def test_position_counts_from_one_not_zero():
    rows = B.category_ranks(
        terms=("fiber",),
        fetch=_fake_fetch({"fiber": [_server("hum/internet-availability"),
                                     _server("other/y"),
                                     _server(B.OUR_QUALIFIED_NAME)]}))
    assert rows[0]["position"] == 3, "a 0-indexed position would report #2"
    assert rows[0]["held"] is False
    assert rows[0]["leader"] == "hum/internet-availability"


def test_absent_from_the_page_is_a_bound_not_none():
    """Searched and not found is DIFFERENT from not searched."""
    rows = B.category_ranks(
        terms=("hyperscale",),
        fetch=_fake_fetch({"hyperscale": [_server("cloudflare/radar")]}))
    assert rows[0]["position"] == ">50"
    assert rows[0]["readable"] is True


def test_unreadable_term_is_none_and_never_a_slip():
    """★THE ONE THAT MATTERS. A dead fetch must not render as ">50" — that is
    a measurement, and it would put a term on the reclaim worklist that was
    never actually lost."""
    rows = B.category_ranks(terms=("energy",),
                            fetch=_fake_fetch({}, dead=("energy",)))
    assert rows[0]["readable"] is False
    assert rows[0]["position"] is None
    assert rows[0]["position"] != ">50"
    assert rows[0].get("held") is not True


# ── the field ────────────────────────────────────────────────────────
def _field_fetch(rows, ours_uses):
    everything = list(rows) + [_server(B.OUR_QUALIFIED_NAME, ours_uses)]
    return _fake_fetch({"": {1: everything}})


def test_use_rank_floor_counts_only_servers_strictly_above_us():
    f = B.ecosystem_field(
        terms=(),
        fetch=_field_fetch([_server("a", 9000), _server("b", 5000),
                            _server("c", 3573), _server("d", 10)], 3573))
    # a and b are above; c TIES and must not count against us.
    assert f["our_use_rank_floor"] == 3
    assert f["our_use_count"] == 3573


def test_field_is_always_declared_a_sample():
    """A 500-row ceiling over 11,729 servers cannot produce a rank. If this
    ever reports a census, every number below it becomes an over-claim."""
    f = B.ecosystem_field(terms=(), fetch=_field_fetch([_server("a", 1)], 5))
    assert f["field_is_a_sample"] is True
    assert "FLOOR" in f["basis"]
    assert str(f["sample_size"]) in f["basis"]


def test_top_is_ordered_by_use_and_marks_us():
    f = B.ecosystem_field(
        terms=(),
        fetch=_field_fetch([_server("big", 99), _server("small", 1)], 50))
    names = [r["qualified_name"] for r in f["top"]]
    assert names == ["big", B.OUR_QUALIFIED_NAME, "small"]
    assert [r["is_us"] for r in f["top"]] == [False, True, False]


def test_missing_use_count_is_zero_not_a_crash():
    f = B.ecosystem_field(
        terms=(),
        fetch=_fake_fetch({"": {1: [{"qualifiedName": "no-uses"},
                                    _server(B.OUR_QUALIFIED_NAME, 5)]}}))
    assert f["our_use_rank_floor"] == 1
    assert f["top"][-1]["use_count"] == 0


def test_dead_registry_yields_unreadable_field_not_an_empty_top():
    f = B.ecosystem_field(terms=(), fetch=_fake_fetch({}, dead=("",)))
    assert f["readable"] is False
    assert f["our_use_rank_floor"] is None, "no data must not read as rank #1"
    # ★ Found by mutation: the early-return path had its own copy of this flag
    # and no test read it, so a census claim could be introduced on the branch
    # that has the LEAST data behind it.
    assert f["field_is_a_sample"] is True


# ── listing PR ledger ────────────────────────────────────────────────
def _ledger_fetch(items):
    def fetch(url, params):
        if url == B.GITHUB_SEARCH:
            return {"items": items}, None
        return {"servers": [], "pagination": {}}, None
    return fetch


def test_ledger_separates_merged_from_merely_closed():
    led = B.listing_pr_ledger(_ledger_fetch([
        {"state": "closed", "pull_request": {"merged_at": "2026-07-12T00:00:00Z"}},
        {"state": "closed", "pull_request": {"merged_at": None}},
        {"state": "closed", "pull_request": {}},
        {"state": "open", "pull_request": {"merged_at": None}},
    ]))
    assert led["submitted"] == 4
    assert led["merged"] == 1
    assert led["still_open"] == 1
    assert led["closed_unmerged"] == 2
    assert led["land_rate_pct"] == 25.0


def test_unreachable_github_is_unmeasured_never_zero():
    def dead(url, params):
        return None, "HTTP 403"
    led = B.listing_pr_ledger(dead)
    assert led["readable"] is False
    assert "submitted" not in led, "a zero here reads as 'nothing outstanding'"


# ── snapshot + board ─────────────────────────────────────────────────
def test_snapshot_counts_held_terms_over_readable_ones_only():
    snap = B.build_snapshot(fetch=_fake_fetch(
        {"data center": [_server(B.OUR_QUALIFIED_NAME)],
         "energy": [_server(B.OUR_QUALIFIED_NAME)]},
        dead=tuple(t for t in B.DOMAIN_TERMS
                   if t not in ("data center", "energy"))))
    cat = snap["category"]
    assert cat["terms_probed"] == len(B.DOMAIN_TERMS)
    assert cat["terms_readable"] == 2
    assert cat["terms_held_at_1"] == 2
    assert set(cat["held"]) == {"data center", "energy"}


def test_board_with_no_snapshot_claims_nothing():
    out = B.render_board(None)
    assert "NO SNAPSHOT" in out
    assert "#1" not in out


def test_board_prints_both_ranks_side_by_side():
    """The whole point of the page. If the use rank can vanish from the render,
    the board becomes the flattering half we already publish."""
    snap = B.build_snapshot(fetch=_field_fetch([_server("big", 99999)], 3573))
    out = B.render_board(snap)
    assert "category rank" in out
    assert "use rank" in out
    assert "no better than" in out


def test_routes_are_registered_and_the_html_board_is_the_typed_url():
    """A board defined and never wired is the shape #4015 found in lane 7.
    Blueprint rules only materialise on registration, so register for real."""
    from flask import Flask
    app = Flask(__name__)
    B.register_mcp_ecosystem_board(app)
    paths = {str(r) for r in app.url_map.iter_rules()}
    # /admin/* carries the worker's 15s DEFAULT — this one must never fetch.
    assert "/admin/mcp-ecosystem" in paths
    # /api/v1/admin/* carries the 120s ceiling — the probing happens here.
    assert "/api/v1/admin/mcp-ecosystem" in paths
    assert "/api/v1/admin/mcp-ecosystem/refresh" in paths


# ── the upsert, and the column it conflicts on ───────────────────────
def _sql_of(fn) -> str:
    import inspect
    return inspect.getsource(fn)


def test_on_conflict_target_is_actually_declared_unique():
    """★ AN ON CONFLICT ON A COLUMN WITH NO UNIQUE INDEX IS AN EXCEPTION, NOT A
    GUARD. This repo has already paid for it: brain_findings has no
    UNIQUE(issue,url), so every hand-rolled upsert against it failed until one
    canonical writer took over. A substring check for "ON CONFLICT" would pass
    on exactly that bug, so this reads the conflict COLUMN out of the INSERT and
    demands the CREATE TABLE declare that same column UNIQUE.
    """
    import re
    insert_sql = _sql_of(B.store_snapshot)
    create_sql = _sql_of(B._ensure_table)

    m = re.search(r"ON CONFLICT \((\w+)\)", insert_sql)
    assert m, "the INSERT names no conflict target"
    col = m.group(1)

    unique_cols = set(re.findall(r"^\s*(\w+)\s+[A-Z ]+UNIQUE",
                                 create_sql, re.M))
    unique_cols |= set(re.findall(r"UNIQUE\s*\(\s*(\w+)\s*\)", create_sql))
    assert col in unique_cols, (
        f"INSERT conflicts on {col!r} but the table declares UNIQUE on "
        f"{sorted(unique_cols) or 'nothing'} — this upsert would raise at "
        f"runtime, and the write would be lost")


def test_conflict_resolves_by_replacing_the_day_not_by_dropping_the_write():
    """DO NOTHING here would mean the first refresh of a day wins and every
    later one is silently discarded — a board that quietly stops updating after
    its first read of the morning."""
    insert_sql = _sql_of(B.store_snapshot)
    assert "DO UPDATE" in insert_sql
    assert "DO NOTHING" not in insert_sql
    assert "payload = EXCLUDED.payload" in insert_sql
