"""
tests/test_scaffold_evidence_reconcile.py — merged-scaffold reconciliation
(2026-08-31).

NO DB, NO network, NO main import. reconcile() is pure by construction —
context and scaffold inventory are arguments — so every rule is pinned here
without touching either.

THE RULE THIS FILE EXISTS TO DEFEND
───────────────────────────────────
"Cited evidence no longer resolves" must NEVER be reported as "the scaffold
went stale". Measured across the 33 scaffolds in the tree, 34 of 92 cited
keys name a root that is not a context source at all (`competitor_signal`
for `competitors`, `customer_asks` for `feedback`, `past_lessons`,
`market_news`). Those never resolved on any day. Collapsing UNRESOLVED into
a staleness verdict would manufacture exactly the kind of false RED that
be#3448 removed from the WorkOS probe — and which produced the three
scaffolds be#3458/be#3459 then had to delete.

MUTATION CONTROLS — each must FAIL if the reporter is weakened:
  A. drop the SOURCE_DOWN check          -> an unreadable source is reported
                                            as MALFORMED, inventing a
                                            verdict from a failed fetch.
  B. absent root -> UNRESOLVED not MALFORMED -> loses the one provable
                                            signal the pass actually has.
  C. UNRESOLVED folded into MALFORMED    -> the headline bug: never-resolved
                                            and might-have-drifted become
                                            one claim.
  D. ALL_MALFORMED fires on >=1 instead of all -> caught only by
                                            test_mixed_malformed_and_
                                            unresolved_is_not_condemned. The
                                            one-good-citation test does NOT
                                            catch it: a resolving citation
                                            returns before that branch. This
                                            mutant survived until the mixed
                                            case was written.
  E. no citations -> ALL_MALFORMED       -> absence of evidence reported as
                                            evidence of absence.
  F. assertion tail not stripped         -> `...verdict=broken` stops
                                            resolving against a live path.
"""
import datetime as _dt

import pytest

r = pytest.importorskip("routes.brain_scaffold_reconcile")


CTX = {
    "funnel": {"now": {"paid_signal_attribution_30d":
                       {"attribution_rate_pct": 41.2}}},
    "page_health": {"pages": {"/mcp#workos-oauth-challenge":
                              {"verdict": "alive", "last_reason": "ok"}}},
    "competitors": {"presence": {"competitor_features": {"klavis_ai": {}}}},
    "self_model": {"weakest_areas": ["a", "b"]},
}


def _sc(keys, slug="s", week="2026-07-13"):
    return {"slug": slug, "file": f"routes/_proposed_{slug}.py",
            "week_of": week, "evidence_keys": keys}


# ════════════════════════════════════════════════════════════════════
#  resolve_citation — the four states
# ════════════════════════════════════════════════════════════════════
def test_live_path_resolves():
    c = r.resolve_citation(
        CTX, "page_health.pages[/mcp#workos-oauth-challenge].verdict")
    assert c["state"] == r.RESOLVES
    assert "alive" in c["value_preview"]


def test_absent_root_is_malformed_not_unresolved():
    """Mutation control B. `competitor_signal` is not a context source —
    the real key is `competitors`. That is provable from the schema, so it
    is MALFORMED, the one thing this pass can assert without a baseline."""
    c = r.resolve_citation(
        CTX, "competitor_signal.presence.competitor_features[klavis_ai]")
    assert c["state"] == r.MALFORMED


def test_real_root_wrong_subpath_is_unresolved_never_malformed():
    """Mutation control C, the headline. `funnel` IS a real source, so a
    path under it that does not walk today is INDETERMINATE — it could be
    drift or a wrong subpath, and nothing here can tell which."""
    c = r.resolve_citation(
        CTX, "funnel.paid_signal_attribution_30d.attribution_rate_pct")
    assert c["state"] == r.UNRESOLVED
    assert c["state"] != r.MALFORMED


def test_failed_source_is_source_down_not_a_verdict():
    """Mutation control A. A source we could not read is not evidence."""
    c = r.resolve_citation(CTX, "feedback.open[x]", failed_sources=["feedback"])
    assert c["state"] == r.SOURCE_DOWN


def test_source_down_wins_over_absent_root():
    """A root missing from ctx BECAUSE its fetch failed must not be called
    malformed — that is inventing a schema claim from an I/O failure."""
    c = r.resolve_citation({}, "backlog.stuck", failed_sources=["backlog"])
    assert c["state"] == r.SOURCE_DOWN


@pytest.mark.parametrize("key", [
    "page_health.pages[/mcp#workos-oauth-challenge].verdict",
    "page_health.pages./mcp#workos-oauth-challenge.verdict",
    "page_health.pages[/mcp#workos-oauth-challenge].verdict=broken",
])
def test_bracket_dot_and_assertion_forms_all_resolve(key):
    """Mutation control F — all three spellings address one value."""
    assert r.resolve_citation(CTX, key)["state"] == r.RESOLVES


def test_list_index_resolves():
    assert r.resolve_citation(CTX, "self_model.weakest_areas[1]")["state"] \
        == r.RESOLVES


@pytest.mark.parametrize("bad", ["", "   ", None, 123, "==="])
def test_unusable_key_is_malformed_and_never_raises(bad):
    assert r.resolve_citation(CTX, bad)["state"] == r.MALFORMED


# ════════════════════════════════════════════════════════════════════
#  reconcile — the scaffold verdicts
# ════════════════════════════════════════════════════════════════════
def test_all_malformed_only_when_every_citation_is():
    """Mutation control D."""
    rep = r.reconcile(CTX, [_sc(["news.abc", "market_news[1]"])])
    assert rep["rows"][0]["verdict"] == r.V_ALL_MALFORMED


def test_one_good_citation_saves_the_scaffold():
    """Mutation control D: a single resolving citation means the scaffold
    is NOT condemned, however many bad ones sit beside it."""
    rep = r.reconcile(CTX, [_sc([
        "news.abc",
        "page_health.pages[/mcp#workos-oauth-challenge].verdict"])])
    assert rep["rows"][0]["verdict"] == r.V_RESOLVE


def test_mixed_malformed_and_unresolved_is_not_condemned():
    """Mutation control D, the case that actually exercises the threshold.

    No citation resolves, so the RESOLVES branch is skipped — this is where
    "all malformed" is decided. One citation IS provably malformed
    (`news` is not a source); the other names a REAL source with a subpath
    that does not walk, which is indeterminate. A scaffold is condemned
    only when EVERY citation is provably malformed, so a single
    indeterminate citation is enough to withhold the verdict.

    The earlier one-good-citation test cannot catch a broken threshold: it
    has a resolving citation and returns before this branch is reached.
    """
    rep = r.reconcile(CTX, [_sc(["news.abc", "funnel.nope.nope"])])
    row = rep["rows"][0]
    assert row["counts"][r.MALFORMED] == 1
    assert row["counts"][r.UNRESOLVED] == 1
    assert row["counts"][r.RESOLVES] == 0
    assert row["verdict"] == r.V_INDETERMINATE, (
        "one indeterminate citation must withhold the condemnation")


def test_unresolved_only_is_indeterminate_never_condemned():
    """Mutation control C at scaffold level: real sources, paths that do
    not walk => we do not know, and we say so."""
    rep = r.reconcile(CTX, [_sc([
        "funnel.paid_signal_attribution_30d.attribution_rate_pct"])])
    assert rep["rows"][0]["verdict"] == r.V_INDETERMINATE
    assert "baseline" in rep["rows"][0]["why"]


def test_no_citations_is_indeterminate_not_condemned():
    """Mutation control E: absence of evidence is not evidence of absence."""
    rep = r.reconcile(CTX, [_sc([])])
    assert rep["rows"][0]["verdict"] == r.V_INDETERMINATE
    assert rep["rows"][0]["why"] == "no evidence cited"


def test_source_down_scaffold_is_never_condemned():
    rep = r.reconcile(CTX, [_sc(["feedback.open[x]"])],
                      failed_sources=["feedback"])
    assert rep["rows"][0]["verdict"] == r.V_INDETERMINATE
    assert rep["sources_unavailable"] == ["feedback"]


def test_the_three_deleted_scaffolds_are_not_called_stale():
    """The OAuth trio, verbatim. Two cite `page_health...` which resolves
    today, one cites `competitor_signal...` which never did. None of that
    licenses a staleness verdict, and the report must not imply one."""
    rep = r.reconcile(CTX, [
        _sc(["competitor_signal.presence.competitor_features[klavis_ai]",
             "page_health.pages[/mcp#workos-oauth-challenge]",
             "funnel.ai_agent_top_platforms_external"], "klavis", "2026-07-13"),
        _sc(["page_health.pages./mcp#workos-oauth-challenge.verdict=broken",
             "funnel.paid_signal_attribution_30d.attribution_rate_pct"],
            "reenable", "2026-08-17"),
        _sc(["page_health.pages[/mcp#workos-oauth-challenge].last_reason",
             "funnel.now.paid_signal_attribution_30d.attribution_rate_pct"],
            "durable", "2026-08-24"),
    ])
    assert "stale" not in str(rep["verdicts"]).lower()
    for row in rep["rows"]:
        assert row["verdict"] in (r.V_RESOLVE, r.V_INDETERMINATE,
                                  r.V_ALL_MALFORMED)
        assert "stale" not in row["why"].lower() or "not stale" in row["why"]


def test_summary_counts_add_up():
    rep = r.reconcile(CTX, [
        _sc(["news.abc"], "a"),
        _sc(["page_health.pages[/mcp#workos-oauth-challenge]"], "b"),
        _sc(["funnel.nope.nope"], "c"),
    ])
    assert rep["scaffolds"] == 3
    assert sum(rep["verdicts"].values()) == 3
    assert rep["citations_total"] == 3
    assert sum(rep["citations"].values()) == 3


def test_condemned_rows_sort_first():
    rep = r.reconcile(CTX, [
        _sc(["page_health.pages[/mcp#workos-oauth-challenge]"], "good"),
        _sc(["news.abc"], "bad"),
    ])
    assert rep["rows"][0]["slug"] == "bad"


def test_age_days_from_week_of():
    rep = r.reconcile(CTX, [_sc(["news.a"], "s", "2026-07-13")],
                      today=_dt.date(2026, 8, 31))
    assert rep["rows"][0]["age_days"] == 49


def test_unparseable_week_gives_no_age_rather_than_a_wrong_one():
    rep = r.reconcile(CTX, [_sc(["news.a"], "s", "not-a-date")])
    assert rep["rows"][0]["age_days"] is None


def test_report_states_its_own_limit():
    """The note is load-bearing: it is what stops a reader treating
    UNRESOLVED as an action item."""
    rep = r.reconcile(CTX, [_sc([])])
    assert "INDETERMINATE, not stale" in rep["note"]


# ════════════════════════════════════════════════════════════════════
#  parse_scaffold — reading a real file
# ════════════════════════════════════════════════════════════════════
def test_parse_scaffold_reads_evidence_and_week(tmp_path):
    f = tmp_path / "_proposed_demo_thing.py"
    f.write_text(
        '"""\ndemo-thing.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, '
        'week 2026-08-17).\n\nSPEC (from brain L6 synthesis):\nblah\n\n'
        'Evidence cited by the brain when proposing this:\n'
        '- `page_health.pages[/x]`\n- `funnel.now.y`\n\n'
        'To unblock: implement the routes.\n"""\n')
    got = r.parse_scaffold(f)
    assert got["slug"] == "demo_thing"
    assert got["week_of"] == "2026-08-17"
    assert got["evidence_keys"] == ["page_health.pages[/x]", "funnel.now.y"]


def test_parse_scaffold_missing_file_reports_rather_than_raises(tmp_path):
    got = r.parse_scaffold(tmp_path / "nope.py")
    assert got["evidence_keys"] == [] and "error" in got


def test_parse_every_scaffold_in_the_tree():
    """Guards the parser against real drift: every file on disk must parse,
    and the corpus must still be citing things (a silent parse regression
    that returned zero keys everywhere would otherwise look like success)."""
    import pathlib
    files = sorted(pathlib.Path("routes").glob("_proposed_*.py"))
    if not files:
        pytest.skip("no scaffolds in tree")
    parsed = [r.parse_scaffold(p) for p in files]
    assert all("error" not in p for p in parsed)
    assert sum(len(p["evidence_keys"]) for p in parsed) > 0
