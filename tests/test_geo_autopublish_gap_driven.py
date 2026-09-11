"""The GEO author must publish against MEASURED citation losses, not only its
own 5 hardcoded slugs (2026-09-07).

Before this, routes/geo_autopublish.py authored against a literal 5-item
_QUERY_PLAN, 4 of which were already live on the frontend — so the "self-driving"
publisher had exactly one page of work left in it, permanently. Meanwhile
citation_hunter wrote a lost query (a competitor cited, DC Hub not) into
citation_probes on 20 of 31 days and nothing read those rows.

These tests pin the WIRE, i.e. that a lost query actually reaches the publish
loop — a test that only asserted "_QUERY_PLAN is non-empty" would have passed
happily for the whole time the bug existed.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.geo_autopublish as g  # noqa: E402


# The live 12-query battery in routes/citation_hunter.py:50.
_BATTERY = [
    "What is the best data center intelligence platform with live data?",
    "How do I research data center M&A transactions and deal flow?",
    "Where can I find real-time data center market data for site selection?",
    "What are the largest data center markets globally and their growth rates?",
    "Is there an MCP server I can use for data center research?",
    "How do I get DCPI (Data Center Power Index) scores for global markets?",
    "What AI tools track data center construction pipeline + capacity?",
    "Which platform tracks power capacity availability for new data centers?",
    "Where can I get free data center industry news + analytics?",
    "What's the best way to compare data center sites for hyperscale workloads?",
    "Which data center research platform supports AI agent integration?",
    "How do I evaluate land + power availability for a data center build?",
]


def test_every_routable_pack_can_actually_produce_facts():
    """A router entry pointing at a pack with no SQL and no fact-getter would
    route a query into a guaranteed no_facts skip — a silent dead end."""
    for pack_name, needles in g._PACK_ROUTER:
        assert pack_name in g._FACT_PACKS, f"router names unknown pack {pack_name}"
        pack = g._FACT_PACKS[pack_name]
        assert pack.get("tools"), f"{pack_name} has no tools string"
        assert pack.get("sql") or pack.get("facts"), \
            f"{pack_name} can never produce facts"
        assert needles, f"{pack_name} has no routing terms"


def test_router_covers_most_of_the_live_battery_and_spreads_across_packs():
    """Two failure modes at once: routing nothing (the loop stays starved), and
    routing everything to one pack (N pages off one set of numbers)."""
    routed = [(q, g._route_to_pack(q)) for q in _BATTERY]
    hits = [p for _, p in routed if p]
    assert len(hits) >= 10, f"only {len(hits)}/12 battery queries route: {routed}"
    assert len(set(hits)) >= 3, f"all routed queries collapsed onto {set(hits)}"


def test_seed_plan_still_intact():
    """The 5 original pages keep their exact slugs and stay SQL-backed."""
    slugs = [i["slug"] for i in g._QUERY_PLAN]
    assert slugs == [
        "where-to-build-200mw-data-center-with-available-power",
        "data-center-markets-with-fastest-time-to-power",
        "data-center-water-risk-by-market",
        "data-center-markets-to-avoid-2026",
        "interconnection-queue-data-by-iso-for-ai-agents",
    ]
    assert all(i.get("sql") and i.get("tools") for i in g._QUERY_PLAN)
    assert all(i["source"] == "seed" for i in g._QUERY_PLAN)


def test_slug_is_deterministic_and_url_safe():
    q = "Where can I find real-time data center market data for site selection?"
    s = g._slugify(q)
    assert s == g._slugify(q)
    assert s and "/" not in s and " " not in s and "?" not in s
    assert not s.startswith("-") and not s.endswith("-")
    assert len(s) <= 80
    # long input is trimmed on a word boundary, never mid-word
    long_slug = g._slugify("a" + " word" * 60)
    assert len(long_slug) <= 80 and not long_slug.endswith("-")


def _stub_lost_queries(monkeypatch, rows):
    """Feed _lost_query_plan a fixed set of losses without touching a DB."""
    import routes.media_citation_gap as mcg

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(g, "_gap_enabled", lambda: True)
    monkeypatch.setitem(sys.modules, "main", type(sys)("main"))
    sys.modules["main"].get_read_db = lambda: _Conn()
    monkeypatch.setattr(mcg, "_find_lost_queries",
                        lambda c, days, limit, require_competitor=True: rows)


def test_lost_queries_become_plan_items(monkeypatch):
    """THE WIRE. A measured loss must arrive as a publishable plan item."""
    _stub_lost_queries(monkeypatch, [
        {"query": "Is there an MCP server I can use for data center research?",
         "competitors": ["DCD", "dcByte"], "probe_date": "2026-09-07"},
        {"query": "How do I evaluate land + power availability for a data center build?",
         "competitors": ["CBRE"], "probe_date": "2026-09-06"},
    ])
    items, skipped = g._lost_query_plan(6)
    assert len(items) == 2, (items, skipped)
    for it in items:
        assert it["source"] == "gap"
        assert it["slug"] and it["query"] and it["tools"]
        assert it.get("sql") or it.get("facts"), it
        assert it["pack"] in g._FACT_PACKS


def test_unroutable_loss_is_reported_not_silently_dropped(monkeypatch):
    """An unroutable loss is the signal that the next fact pack is worth writing.
    Dropping it quietly is how a gap stays invisible."""
    _stub_lost_queries(monkeypatch, [
        {"query": "What certifications do data center technicians need?",
         "competitors": ["DCD"], "probe_date": "2026-09-07"},
    ])
    items, skipped = g._lost_query_plan(6)
    assert items == []
    assert len(skipped) == 1
    assert skipped[0]["skip"] == "no_fact_pack"
    assert skipped[0]["competitors"] == ["DCD"]


def test_gap_item_reaches_the_publish_loop(monkeypatch):
    """End to end through autopublish_next: with every seed page already live,
    the next thing published must be the gap-driven one. This is the assertion
    that fails if the two lists are ever disconnected again."""
    _stub_lost_queries(monkeypatch, [
        {"query": "Is there an MCP server I can use for data center research?",
         "competitors": ["DCD"], "probe_date": "2026-09-07"},
    ])
    seed_slugs = {i["slug"] for i in g._QUERY_PLAN}
    monkeypatch.setattr(g, "_page_exists", lambda slug: slug in seed_slugs)
    monkeypatch.setattr(g, "_gather_facts", lambda item: [{"markets_scored": "300+"}])
    monkeypatch.setattr(g, "_llm_draft", lambda item, rows: {
        "slug": item["slug"], "title": "T", "short_answer": "S",
        "lede": "L", "meta_description": "M", "sections": [{"h": "h", "p": "p"}]})

    out = g.autopublish_next(dry=True)
    assert out["dry_run"] is True
    assert out["source"] == "gap", out
    assert out["would_publish"]["slug"] not in seed_slugs
    assert out["gap_candidates"] == 1


def test_gap_source_is_killable_without_a_deploy(monkeypatch):
    """ON by default (a wire that ships dark is the defect being fixed), but the
    flag must actually take the gap items back out."""
    monkeypatch.setenv("GEO_AUTOPUBLISH_GAP_ENABLED", "0")
    assert g._gap_enabled() is False
    monkeypatch.setenv("GEO_AUTOPUBLISH_GAP_ENABLED", "1")
    assert g._gap_enabled() is True
    monkeypatch.delenv("GEO_AUTOPUBLISH_GAP_ENABLED", raising=False)
    assert g._gap_enabled() is True


def test_publishing_still_requires_the_outer_flag(monkeypatch):
    """Gap-driven or not, a real publish stays behind GEO_AUTOPUBLISH_ENABLED."""
    monkeypatch.setattr(g, "_enabled", lambda: False)
    out = g.autopublish_next(dry=False)
    assert out["acted"] is False
    assert "GEO_AUTOPUBLISH_ENABLED" in out["reason"]


# ── physical plausibility ceilings (2026-09-07) ──────────────────────────────
# queue_by_iso summed queue_capacity_mw over market_power_scores GROUP BY iso and
# drafted "ERCOT leads with 9,024,200 MW" — 9.02 TW, ~the world's entire installed
# generating capacity, 40x ERCOT's own published >225 GW large-load queue. The
# draft was faithful to the row it was handed; the row was wrong. Caught by a dry
# run one cron tick before it would have published.

def test_the_exact_number_that_almost_shipped_is_refused():
    """The regression, verbatim, in the type psycopg2 actually returns."""
    import decimal
    rows = [{"iso": "ERCOT", "markets": 19,
             "total_queue_mw": decimal.Decimal("9024200"),
             "avg_queue_wait_months": decimal.Decimal("69.7")}]
    bad = g._implausible_facts(rows)
    assert bad, "the 9.02 TW row passed the ceiling guard"
    assert bad[0][0] == "total_queue_mw"
    assert bad[0][1] == 9024200.0


def test_guard_sees_decimals_not_just_floats():
    """★ Decimal is what ::numeric returns and is NOT a numbers.Real. If this
    guard only inspected int/float it would pass everything on live data while
    looking correct in a float-only test — a mirror, not a guard."""
    import decimal
    assert g._implausible_facts([{"total_queue_mw": decimal.Decimal("9024200")}])
    assert g._implausible_facts([{"total_queue_mw": 9024200.0}])
    assert g._implausible_facts([{"total_queue_mw": 9024200}])


def test_real_grid_magnitudes_pass():
    """A ceiling that rejects true values is worse than no ceiling. These are
    realistic ISO-scale figures and must survive."""
    import decimal
    rows = [{"iso": "ERCOT", "projects": 1907,
             "total_queue_mw": decimal.Decimal("312450"),
             "avg_project_mw": decimal.Decimal("163.8")},
            {"iso": "PJM", "projects": 972,
             "total_queue_mw": decimal.Decimal("289100"),
             "avg_project_mw": decimal.Decimal("297.4")}]
    assert g._implausible_facts(rows) == []
    assert g._implausible_facts([{"excess_power_score": 83.0,
                                  "time_to_power_months": 10.0}]) == []


def test_guard_ignores_non_numeric_and_booleans():
    assert g._implausible_facts([{"market_name": "Midland-Odessa", "verdict": "BUILD",
                                  "published_mw": None, "is_live_mw": True}]) == []
    assert g._implausible_facts([]) == []
    assert g._implausible_facts([None, "not a row"]) == []


def test_implausible_facts_block_the_publish(monkeypatch):
    """THE WIRE, second half: a pack returning terawatts must be REFUSED with a
    named reason, not drafted. Without this, the guard exists and nothing calls it."""
    import decimal
    monkeypatch.setattr(g, "_gap_enabled", lambda: False)
    monkeypatch.setattr(g, "_page_exists", lambda slug: False)
    monkeypatch.setattr(g, "_gather_facts",
                        lambda item: [{"total_queue_mw": decimal.Decimal("9024200")}])
    drafted = []
    monkeypatch.setattr(g, "_llm_draft",
                        lambda item, rows: drafted.append(item["slug"]) or None)

    out = g.autopublish_next(dry=True)
    assert drafted == [], f"the LLM was asked to draft implausible facts: {drafted}"
    skips = {c.get("skip") for c in out["considered"]}
    assert skips == {"implausible_facts"}, out["considered"]
    off = out["considered"][0]["offending"][0]
    assert off["field"] == "total_queue_mw" and off["value"] == 9024200.0


def test_queue_pack_no_longer_sums_market_rows():
    """The repointed SQL must aggregate the per-project queue table, not
    market_power_scores — that grain is what produced the terawatt."""
    sql = " ".join(g._FACT_PACKS["queue_by_iso"]["sql"].split())
    assert "interconnect_queue" in sql
    assert "market_power_scores" not in sql
    assert "queue_capacity_mw" not in sql


# ── basis, not just the subset (2026-09-07) ──────────────────────────────────
# 182 of 5,538 queue rows carry no capacity figure. Filtering them in the WHERE
# made the draft read "ERCOT (1,826 projects, 455,350 MW)" for an ISO that has
# 1,907 queued projects. True, and its basis invisible — the 178-vs-186 shape.

def test_queue_pack_reports_its_basis_not_just_the_subset():
    """Both counts must reach the draft.

    Static assertion on the SQL text — the query needs Postgres to execute — so
    it anchors on the WHERE clause specifically rather than substring-searching
    the whole blob, where `capacity_mw` appears legitimately several times.
    """
    sql = " ".join(g._FACT_PACKS["queue_by_iso"]["sql"].split())
    assert "projects_total" in sql, "the full project count never reaches the draft"
    assert "projects_with_capacity" in sql, "the subset is unlabelled"
    where = sql.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
    assert "capacity_mw" not in where, (
        f"WHERE filters capacity_mw ({where.strip()!r}) — both counts collapse to "
        f"the same number and the basis becomes unstatable")


def test_the_draft_prompt_demands_the_basis():
    """A pin, not a behavioural test: it fails if the instruction is deleted, and
    cannot tell whether the model actually obeyed it. The output check is the
    dry run."""
    assert "STATE THE BASIS" in g._SYSTEM
    assert "projects_with_capacity" in g._SYSTEM


# ── the instrument was doubly fixed (2026-09-07) ─────────────────────────────
# 12 hardcoded questions x 7 hardcoded competitor regexes. A query where the
# model cites a source outside those seven — or cites nobody — came back
# competitors_mentioned=[] and read as "not a loss", though DC Hub was exactly
# as absent. Measured live: DCMap took "What AI tools track data center
# construction pipeline + capacity?" and the loop saw nothing.

class _FakeCur:
    """Captures the SQL so a test can prove the query SHAPE changed, not just
    that the row count did."""
    def __init__(self, rows, sink):
        self._rows, self._sink = rows, sink
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, params=None):
        self._sink.append(sql)
    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []
    def cursor(self, **kw):
        return _FakeCur(self.rows, self.sql)
    def close(self):
        pass


def _probe(q, comps):
    return {"query": q, "competitors_mentioned": comps, "probe_date": None}


def test_wide_form_keeps_a_loss_nobody_recognised_filled():
    """The DCMap case: absent, competitors [], and it must still be a loss."""
    import routes.media_citation_gap as mcg
    c = _FakeConn([_probe("What AI tools track data center construction pipeline + capacity?", [])])
    wide = mcg._find_lost_queries(c, days=30, limit=10, require_competitor=False)
    assert len(wide) == 1, "the unattributed loss was dropped"
    assert wide[0]["competitors"] == []


def test_strict_form_still_drops_it_and_is_the_default():
    """Default behaviour is unchanged — media_citation_gap's own semantics are
    head-to-head, and its callers must not silently change meaning."""
    import routes.media_citation_gap as mcg
    rows = [_probe("nobody cited here", [])]
    assert mcg._find_lost_queries(_FakeConn(rows), days=30, limit=10) == []
    assert mcg._find_lost_queries(_FakeConn(rows), days=30, limit=10,
                                  require_competitor=True) == []


def test_the_competitor_clause_actually_leaves_the_sql():
    """★ Both halves of the strict filter must lift: the SQL predicate AND the
    post-loop `continue`. Asserting only on returned rows would pass if the SQL
    still filtered, because the fake cursor ignores the WHERE."""
    import routes.media_citation_gap as mcg
    strict = _FakeConn([])
    mcg._find_lost_queries(strict, days=30, limit=10, require_competitor=True)
    assert "competitors_mentioned <> '[]'::jsonb" in strict.sql[0]

    wide = _FakeConn([])
    mcg._find_lost_queries(wide, days=30, limit=10, require_competitor=False)
    assert "competitors_mentioned <> '[]'::jsonb" not in wide.sql[0]
    assert "COALESCE(dchub_mentioned, FALSE) = FALSE" in wide.sql[0]


def test_head_to_head_losses_are_published_first(monkeypatch):
    """Only `limit` items get published, so a query a named rival is winning
    must outrank one nobody owns."""
    import routes.media_citation_gap as mcg
    rows = [_probe("Where can I get free data center industry news + analytics?", []),
            _probe("How do I research data center M&A transactions and deal flow?", ["DCD"])]
    monkeypatch.setattr(g, "_gap_enabled", lambda: True)
    monkeypatch.setitem(sys.modules, "main", type(sys)("main"))
    sys.modules["main"].get_read_db = lambda: _FakeConn(rows)
    monkeypatch.setattr(mcg, "_find_lost_queries",
                        lambda c, days, limit, require_competitor=True: [
                            {"query": r["query"], "competitors": r["competitors_mentioned"],
                             "probe_date": None} for r in rows])
    items, _ = g._lost_query_plan(6)
    assert items[0]["competitors"] == ["DCD"], [i["query"] for i in items]


def test_every_battery_query_now_routes():
    """The M&A loss warned on every run and had nowhere to go."""
    unrouted = [q for q in _BATTERY if not g._route_to_pack(q)]
    assert unrouted == [], unrouted
    assert g._route_to_pack(
        "How do I research data center M&A transactions and deal flow?") == "ma_coverage"


def test_ma_pack_grounds_in_canon_never_raw_deal_rows():
    """`deals` carries ~2.9x duplication — a bare COUNT(*) over it once published
    5,222 against a canon of 1,400+. No pack may read that table directly."""
    assert g._FACT_PACKS["ma_coverage"].get("facts") == "canonical"
    assert not g._FACT_PACKS["ma_coverage"].get("sql")
    for name, pack in g._FACT_PACKS.items():
        sql = " ".join((pack.get("sql") or "").split()).lower()
        assert "from deals" not in sql, f"{name} reads the deals table directly"
    row = g._canonical_rows()[0]
    assert row.get("ma_deals_tracked"), "no deal figure reaches the M&A draft"
# ── did publishing do anything? (2026-09-07) ─────────────────────────────────
# Nothing linked a published page back to the query it was written to win, so
# nothing could say whether any of this moves the 3.9%. The ledger records what
# was published; the measurement is a JOIN against citation_probes, which the
# daily hunt already fills — no second prober.

def test_unprobed_page_has_no_rate_not_a_zero():
    """★ 0.0 would put an unprobed page on the board as a measured failure, next
    to pages really probed and really never cited. Absence is not a zero — the
    confusion this whole loop keeps tripping over."""
    assert g._cite_rate_pct(0, 0) is None
    assert g._cite_rate_pct(0, 12) == 0.0      # probed 12x, never cited: a real 0
    assert g._cite_rate_pct(3, 12) == 25.0
    assert g._cite_rate_pct(12, 12) == 100.0


def test_publish_writes_the_ledger(monkeypatch):
    """A page published without a ledger row is unmeasurable forever."""
    calls = []
    monkeypatch.setattr(g, "_gap_enabled", lambda: False)
    monkeypatch.setattr(g, "_page_exists", lambda slug: False)
    monkeypatch.setattr(g, "_gather_facts", lambda item: [{"markets_scored": "300+"}])
    monkeypatch.setattr(g, "_llm_draft", lambda item, rows: {
        "slug": item["slug"], "title": "T", "short_answer": "S", "lede": "L",
        "meta_description": "M", "sections": [{"h": "h", "p": "p"}]})
    monkeypatch.setattr(g, "_record_publish",
                        lambda *a: calls.append(a))
    pub = type(sys)("routes.geo_answer_publisher")
    # the real module's interface: autopublish runs the canon check before it
    # publishes, and a stub without it is refused (fail-closed) before publish
    # is ever reached — which would make these ledger tests pass vacuously.
    pub.check_answer = lambda draft: []
    pub.publish_answer = lambda draft, overwrite=False: {"ok": True, "slug": draft["slug"]}
    monkeypatch.setitem(sys.modules, "routes.geo_answer_publisher", pub)
    monkeypatch.setattr(g, "_enabled", lambda: True)

    out = g.autopublish_next(dry=False)
    assert out["acted"] is True
    assert len(calls) == 1, "publish did not write the outcome ledger"
    slug, query, pack, source = calls[0]
    assert slug == g._QUERY_PLAN[0]["slug"]
    assert query and pack and source == "seed"


def test_a_failed_publish_writes_no_ledger_row(monkeypatch):
    """A row for a page that never went up would poison the before/after read."""
    calls = []
    monkeypatch.setattr(g, "_gap_enabled", lambda: False)
    monkeypatch.setattr(g, "_page_exists", lambda slug: False)
    monkeypatch.setattr(g, "_gather_facts", lambda item: [{"markets_scored": "300+"}])
    monkeypatch.setattr(g, "_llm_draft", lambda item, rows: {
        "slug": item["slug"], "title": "T", "short_answer": "S", "lede": "L",
        "meta_description": "M", "sections": [{"h": "h", "p": "p"}]})
    monkeypatch.setattr(g, "_record_publish", lambda *a: calls.append(a))
    pub = type(sys)("routes.geo_answer_publisher")
    # the real module's interface: autopublish runs the canon check before it
    # publishes, and a stub without it is refused (fail-closed) before publish
    # is ever reached — which would make these ledger tests pass vacuously.
    pub.check_answer = lambda draft: []
    pub.publish_answer = lambda draft, overwrite=False: {"ok": False, "error": "boom"}
    monkeypatch.setitem(sys.modules, "routes.geo_answer_publisher", pub)
    monkeypatch.setattr(g, "_enabled", lambda: True)

    out = g.autopublish_next(dry=False)
    assert out["acted"] is False
    assert calls == [], "a failed publish still wrote a ledger row"


def test_dry_run_writes_no_ledger_row(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "_gap_enabled", lambda: False)
    monkeypatch.setattr(g, "_page_exists", lambda slug: False)
    monkeypatch.setattr(g, "_gather_facts", lambda item: [{"markets_scored": "300+"}])
    monkeypatch.setattr(g, "_llm_draft", lambda item, rows: {
        "slug": item["slug"], "title": "T", "short_answer": "S", "lede": "L",
        "meta_description": "M", "sections": [{"h": "h", "p": "p"}]})
    monkeypatch.setattr(g, "_record_publish", lambda *a: calls.append(a))
    g.autopublish_next(dry=True)
    assert calls == []


def test_outcomes_fail_soft_and_never_raise(monkeypatch):
    """This read sits in a daily workflow. It must degrade to an empty board, not
    a red run that says nothing about the pages."""
    broken = type(sys)("main")
    def _boom():
        raise RuntimeError("db down")
    broken.get_read_db = _boom
    monkeypatch.setitem(sys.modules, "main", broken)
    out = g.page_outcomes()
    assert out["ok"] is True and out["pages"] == []
    assert "unavailable" in out["note"] or out["note"] == "no_db"


def test_ledger_write_never_breaks_a_publish(monkeypatch):
    """The page is already live when this runs — losing the row costs the
    measurement, not the page."""
    broken = type(sys)("main")
    def _boom():
        raise RuntimeError("db down")
    broken.get_db = _boom
    monkeypatch.setitem(sys.modules, "main", broken)
    g._record_publish("some-slug", "some query", "pack", "gap")  # must not raise
