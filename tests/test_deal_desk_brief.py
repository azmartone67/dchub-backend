"""The Deal Desk Brief must PRINT what the answer does not cover.

The brief exists because an `execute_plan` envelope dies in an agent's context
window and nobody can forward it. Turning it into a shareable PDF is only worth
doing if the PDF is as honest as the envelope — a brief that keeps the verdicts
and drops the tier-gated fields, the unapplied arguments and the steps that
never ran launders a free-tier preview into something that reads like a finished
analysis. These tests pin that, plus the two mechanical traps this renderer hit:

  * `constraint_coverage` ships in four incompatible shapes under one name, so
    the renderer must BRANCH on the derived shape, never iterate blind.
  * step results arrive truncated mid-value, so the salvage step must recover
    complete pairs and never invent the pair that was cut.

The fixture is a REAL `plan_execution` envelope captured live on 2026-09-10
(intent "rank markets for a 200 MW AI campus in Texas"), trimmed to two steps
and otherwise verbatim. A paraphrased fixture only tests the paraphrase.
"""
import json
import os
import re

import pytest

from routes.deal_desk import (
    brief_model, coverage_lines, limits_from_envelope, render_brief_html,
    salvage_truncated_json, _pro_price_usd,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "deal_desk_plan_execution.json")


@pytest.fixture(scope="module")
def env():
    with open(FIXTURE) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def model(env):
    return brief_model(env, prepared_for="Steelton Capital", prepared_by="DC Hub")


@pytest.fixture(scope="module")
def html(model):
    return render_brief_html(model)


# ── salvage: recover what arrived, invent nothing ───────────────────────────

def test_salvage_recovers_complete_pairs_from_a_truncated_preview(env):
    """execute_plan cuts each step result at ~1.2KB; the tail is a JSON PREFIX.

    Without salvage the findings page is empty for every step, because
    json.loads rejects the whole string over one unterminated value at the end.
    """
    preview = env["executed"][1]["result"]["preview"]
    assert preview.startswith("{") and not preview.rstrip().endswith("}"), (
        "fixture no longer carries a truncated preview — this test would pass "
        "vacuously against a complete object")
    got = salvage_truncated_json(preview)
    assert got["avg_kwh_cents"] == "10.312"
    assert got["constraint_score"] == 22.8
    assert got["data_basis"] == "mixed"
    # The pair that was cut mid-key must NOT appear. Salvage recovers; it never
    # reconstructs.
    assert "_time_to_power_months" not in got
    assert preview.endswith("_time_to_pow"), (
        "the fixture's cut point moved — re-derive the negative assertion above")


@pytest.mark.parametrize("bad", ["", "not json", "[1,2,3]", "{", '{"a"', '{"a":'])
def test_salvage_returns_empty_rather_than_guessing(bad):
    assert salvage_truncated_json(bad) == {}


def test_the_brief_actually_uses_the_salvage(model, html):
    """The wiring, not the unit. Testing `salvage_truncated_json` in isolation
    leaves the renderer free to stop calling it — mutation-proved: replacing the
    call with `{}` left all salvage unit tests green while the findings page
    silently lost every number that lived in the truncated tail.

    `avg_kwh_cents` and `constraint_score` exist ONLY inside step 2's cut-off
    preview string, never as top-level keys, so their presence on the page is
    proof the salvage ran end to end.
    """
    findings = dict(model["steps"][1]["findings"])
    assert findings["avg_kwh_cents"] == "10.312"
    assert findings["constraint_score"] == "22.8"
    assert "avg_kwh_cents" in html and "10.312" in html


def test_salvage_only_values_are_absent_from_the_raw_result(env):
    """Pins the premise of the test above: if these ever become real top-level
    fields, that test stops proving anything and must be re-derived."""
    result = env["executed"][1]["result"]
    assert "avg_kwh_cents" not in result
    assert "constraint_score" not in result
    assert result["preview"].count('"avg_kwh_cents"') == 1


def test_salvage_is_lossless_on_a_complete_object():
    src = {"a": 1, "b": {"c": [1, 2, 3]}, "d": "x"}
    assert salvage_truncated_json(json.dumps(src)) == src


# ── constraint_coverage: four shapes, one name ──────────────────────────────

def test_coverage_lines_branch_on_each_published_shape():
    """One blind `for x in coverage` yields prose from the first shape and dict
    KEYS from the other three — a wrong-type read that raises nothing."""
    assert coverage_lines(["generation is not deliverable load"]) == [
        "generation is not deliverable load"]
    assert coverage_lines({"power_score": "unavailable"}) == ["power_score: unavailable"]
    assert coverage_lines({"headroom_mw": {"status": "unavailable", "reason": "no feeder data"}}) == [
        "headroom_mw: unavailable — no feeder data"]
    assert coverage_lines(None) == []
    assert coverage_lines({}) == []


def test_an_unapplied_argument_is_reported_as_unapplied():
    """`applied: false` means the schema accepted an argument and nothing used
    it. Reporting the key alone loses the only fact that matters."""
    lines = coverage_lines({"capacity_mw": {
        "applied": False, "reason": "rows carry an index, not megawatts",
        "instead": "read excess_power_score"}})
    assert len(lines) == 1
    assert "NOT applied" in lines[0]
    assert "rows carry an index, not megawatts" in lines[0]
    assert "read excess_power_score" in lines[0]


def test_all_arguments_applied_still_says_so():
    assert coverage_lines({"region": {"applied": True}}) == [
        "every argument you sent was applied"]


def test_an_unnameable_shape_is_printed_not_dropped():
    lines = coverage_lines({"weird": 42})
    assert lines and "weird" in lines[0]


# ── the honesty spine reaches the page ──────────────────────────────────────

def test_the_real_runs_unapplied_argument_reaches_the_rendered_brief(model, html):
    """End-to-end on the live fixture: the run declared capacity_mw, the tool did
    not apply it, and the reader must be told — in the model AND on the page."""
    coverage = [lm for lm in model["limits"] if lm["kind"] == "coverage"]
    assert len(coverage) == 1, [lm["text"] for lm in coverage]
    assert coverage[0]["source"] == "Step 1 · site_selection_canvas"
    assert "capacity_mw" in coverage[0]["text"] and "NOT applied" in coverage[0]["text"]
    assert "The shortlist is NOT sized to this target" in html
    assert "NOT applied" in html


def test_tier_withheld_fields_are_named_on_the_page(model, html):
    withheld = [lm for lm in model["limits"] if lm["kind"] == "withheld"]
    assert len(withheld) >= 3
    assert any("time_to_power_months" in lm["text"] for lm in withheld)
    assert any("3 of 12 shown" in lm["text"] for lm in withheld)
    assert "3 of 12 shown — the rest is tier-gated" in html


def test_transport_truncation_is_disclosed_not_hidden(model):
    """Salvage makes a truncated step useful; it does not make it complete."""
    transport = [lm for lm in model["limits"] if lm["kind"] == "transport"]
    assert len(transport) == 2
    assert all("truncated in transport" in lm["text"] for lm in transport)


def test_every_limit_the_model_collects_is_rendered(model, html):
    """No silent trimming: a limits list longer than one sheet paginates, it does
    not get cut to fit."""
    assert model["limits"], "fixture must publish limits or this is vacuous"
    for lm in model["limits"]:
        head = lm["text"][:60].replace("&", "&amp;").replace("<", "&lt;")
        assert head in html, f"limit dropped from the page: {lm['kind']} {lm['text'][:80]}"


@pytest.mark.parametrize("status,needle", [
    ("gated_preview", "WORKING partial answer"),
    ("not_run", "step budget"),
    ("timed_out", "NOT a finding of 'no data'"),
    ("failed", "contributed nothing"),
])
def test_a_step_that_did_not_fully_run_says_which_kind_of_not(status, needle):
    """`timed_out` is not `failed` and neither is an absence of data. Flattening
    them into one word tells the reader something untrue."""
    limits = limits_from_envelope({"executed": [
        {"step": 1, "tool": "get_grid_intelligence", "status": status, "result": {}}]})
    texts = [lm["text"] for lm in limits if lm["kind"] == "status"]
    assert len(texts) == 1 and needle in texts[0]


def test_an_executed_step_contributes_no_status_limit():
    limits = limits_from_envelope({"executed": [
        {"step": 1, "tool": "get_facility", "status": "executed", "result": {}}]})
    assert [lm for lm in limits if lm["kind"] == "status"] == []


def test_nothing_withheld_renders_as_nothing_withheld_not_as_silence():
    """An empty coverage block means nothing was withheld — never 'unknown'. A
    brief that just omits the section leaves the reader unable to tell which."""
    clean = brief_model({"_entity": "plan_execution", "intent": "a clean run",
                         "executed": [{"step": 1, "tool": "get_facility",
                                       "status": "executed", "result": {"name": "X"}}]})
    assert clean["limits"] == []
    page = render_brief_html(clean)
    assert "Nothing was withheld on this run" in page
    assert "it does not mean the limits are unknown" in page


def test_the_limits_section_is_always_emitted(model, html):
    assert "What this brief does <i>not</i> cover" in html


# ── rendering contracts ─────────────────────────────────────────────────────

_FOOTERS = re.compile(r'<div class="(?:cover-)?foot">(.*?)</div>', re.S)


def test_the_footer_never_claims_a_page_count(html):
    """The renderer chunks content into sheet-sized groups, but Chromium decides
    how many physical sheets a group becomes. An authored count printed on every
    page was wrong by two sheets on this very fixture.

    Scoped to the FOOTERS, not the whole document: a rejected-path reason in
    this fixture says "30/60/90 days", and a guard that reads customer prose as
    a page number is a guard that fights the content it is protecting.
    """
    feet = _FOOTERS.findall(html)
    assert len(feet) >= 5, f"footers not found — the guard is scanning nothing ({len(feet)})"
    for foot in feet:
        assert re.search(r"\d+\s*/\s*\d+", foot) is None, (
            f"a page-count claim reappeared in a footer: {foot}")


def test_a_section_that_spans_sheets_says_so_in_its_title(model, html):
    """10 limits at 9 per sheet is two sheets. The reader must be able to tell a
    continued section from a truncated one."""
    assert len(model["limits"]) == 10, "fixture changed — re-derive the split below"
    assert "What this brief does <i>not</i> cover (1 of 2)" in html
    assert "What this brief does <i>not</i> cover (2 of 2)" in html
    assert "What each step returned</h2>" in html, (
        "a section that fits on one sheet must NOT carry an (n of m) suffix")


def test_hostile_text_in_the_intent_is_escaped():
    page = render_brief_html(brief_model({
        "intent": '<script>alert(1)</script>', "executed": [],
        "provenance": {"as_of_basis": "<img src=x onerror=alert(2)>"}}))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "onerror=alert(2)" not in page or "&lt;img" in page


def test_the_brief_carries_the_question_verbatim(model, html):
    assert model["intent"] == "rank markets for a 200 MW AI campus in Texas"
    assert "rank markets for a 200 MW AI campus in Texas" in html


def test_the_verdict_headline_survives_a_decimal_score(model):
    """`[^.]+` cut "CAUTION (50.5)" at the decimal point and printed half a score."""
    assert model["headline"] == "BUILD (83) · CAUTION (50.5)"


def test_an_unknown_verdict_is_never_coloured_green():
    B = brief_model({"intent": "x", "executed": [
        {"step": 1, "tool": "t", "status": "executed", "result": {"verdict": "SOMETHING_NEW"}}]})
    assert B["cards"][0]["tone"] != "grn"


def test_rejected_paths_are_published(model, html):
    assert model["rejected"], "fixture must carry rejected alternatives"
    assert "Paths considered and rejected" in html
    assert model["rejected"][0]["tool"] in html


# ── the price in the gate is derived, not typed ─────────────────────────────

def test_the_pro_price_is_derived_from_the_tier_registry():
    from tier_registry import price
    assert _pro_price_usd() == int(price("pro"))


def test_no_dollar_price_literal_in_the_module():
    """The market-brief PDF gate still tells callers Pro is "$499/mo". A price
    typed into a gate message goes stale where nobody reads it."""
    with open(os.path.join(os.path.dirname(HERE), "routes", "deal_desk.py")) as fh:
        src = fh.read()
    assert re.search(r"\$\d", src) is None, "a dollar literal appeared in deal_desk.py"


# ── routing + gate ──────────────────────────────────────────────────────────

def _app():
    from flask import Flask
    from routes.deal_desk import register_deal_desk_routes
    app = Flask(__name__)
    assert register_deal_desk_routes(app) is True
    assert register_deal_desk_routes(app) is False, "registration must be idempotent"
    return app


def test_the_pdf_suffix_dispatches_to_the_pdf_handler():
    """`<token>` and `<token>.pdf` are two rules over the same prefix. If the
    bare rule wins, the reader gets an HTML page under a .pdf filename and the
    token silently carries the suffix."""
    adapter = _app().url_map.bind("dchub.cloud")
    endpoint, args = adapter.match("/reports/deal-desk/dd-abc123.pdf")
    assert endpoint == "deal_desk.download_deal_desk_brief"
    assert args == {"token": "dd-abc123"}
    endpoint, args = adapter.match("/reports/deal-desk/dd-abc123")
    assert endpoint == "deal_desk.view_deal_desk_brief"
    assert args == {"token": "dd-abc123"}


def test_an_unpaid_caller_is_refused_before_anything_is_stored(env):
    r = _app().test_client().post("/api/v1/deal-desk", json={"plan": env})
    assert r.status_code == 402
    body = r.get_json()
    assert body["error"] == "deal_desk_requires_pro"
    assert body["upgrade_url"].endswith("/pricing")


def test_a_body_that_is_not_a_plan_execution_is_refused(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    r = _app().test_client().post("/api/v1/deal-desk", json={"nope": 1},
                                  headers={"X-Admin-Key": "k"})
    assert r.status_code == 400 and r.get_json()["error"] == "missing_plan_execution"


# ── the write is checked, not assumed ───────────────────────────────────────

class _FakeCursor:
    """Only `execute` + `fetchone` — exactly what a psycopg2 cursor gives here.
    A fake with more capability than the real object turns a can't-work path
    green."""

    def __init__(self, returns):
        self._returns = list(returns)
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self._returns.pop(0)


def test_the_insert_declares_on_conflict_and_returns_the_id():
    from routes.deal_desk import _INSERT_SQL
    up = " ".join(_INSERT_SQL.split()).upper()
    assert "ON CONFLICT (BRIEF_TOKEN) DO NOTHING" in up
    assert up.rstrip().endswith("RETURNING ID"), (
        "without RETURNING, a conflict is indistinguishable from a write")


def test_a_conflicting_token_reports_that_nothing_was_stored():
    """ON CONFLICT DO NOTHING is how a write silently becomes a no-op. The
    caller would hand its human a link to a brief that does not exist."""
    from routes.deal_desk import store_brief
    row = dict.fromkeys(["api_key_hash", "intent", "intent_class", "prepared_for",
                         "prepared_by", "payload", "source", "expires_at"], "x")
    assert store_brief(_FakeCursor([(1,)]), "dd-fresh", row) is True
    assert store_brief(_FakeCursor([None]), "dd-taken", row) is False


def test_the_mint_response_never_claims_a_page_count():
    """Found by verifying the real thing: the mint said "Branded 4-page …" while
    the live PDF rendered 10 sheets. The footer count was cut for exactly this
    reason and the same literal survived one layer up, in the field an agent
    reads back to its human."""
    import routes.deal_desk as dd
    src = open(dd.__file__).read()
    assert re.search(r"\d+\s*-?\s*page\b", src, re.I) is None, (
        "an authored page count reappeared in deal_desk.py")
