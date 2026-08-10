"""Tests for tools/qa_superuser/probe_retrieval.py.

The four construction rules are what make this board readable, so the tests are
written against them rather than against the happy path:

  BLIND ≠ RED        a spent preview / unreachable door must not read as failure
  red_when required  every RED-capable check states its failure condition
  no invented target ranking is a GAUGE; only the empty answer and the edge's
                     own 15s route budget are decidable
  basis required     every finding names the seat and the exact field read
"""

import pytest

from tools.qa_superuser import probe_retrieval as pr
from tools.qa_superuser.finding import BLIND, GAUGE, PASS, RED


class _Sess:
    """MCPSession stand-in. `script` maps tool name -> envelope or Exception."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        v = self.script.get(name)
        if isinstance(v, Exception):
            raise v
        return v if v is not None else {"structuredContent": {}}


def _env(rows, **extra):
    sc = {"results": rows}
    sc.update(extra)
    return {"structuredContent": sc}


def _truth(name="Equinix DC12"):
    return _env([{"name": name, "citation": "src"}])


@pytest.fixture
def patched(monkeypatch):
    """Install a scripted session, grant the paid seat, silence latency."""
    holder = {}

    def _install(script):
        sess = _Sess(script)
        monkeypatch.setattr(pr, "MCPSession", lambda *a, **k: sess)
        holder["sess"] = sess
        return sess

    # Retrieval is judged from the PAID seat (anon is served a trimmed set), so
    # the tests must hold that credential or every one of them is BLIND.
    monkeypatch.setattr(pr.C, "seat_available", lambda seat: True)
    monkeypatch.setattr(pr.C, "PAID_KEY", "test-paid-key", raising=False)
    monkeypatch.setattr(pr, "_check_latency", lambda out: None)
    holder["install"] = _install
    return holder


def _by_title(out, needle):
    return [f for f in out if needle.lower() in f.title.lower()]


# ── the seat this lane must be judged from ──────────────────────────────────

def test_no_paid_credential_is_blind_not_a_verdict(monkeypatch):
    # Without the paid seat the platform withholds results, so recall is
    # unanswerable. Say we could not sit in the seat; never guess from anon.
    monkeypatch.setattr(pr.C, "seat_available", lambda seat: False)
    monkeypatch.setattr(pr, "_check_latency", lambda out: None)
    called = {"n": 0}
    monkeypatch.setattr(pr, "MCPSession",
                        lambda *a, **k: called.update(n=called["n"] + 1))
    out = []
    pr.probe(out)
    assert len(out) == 1 and out[0].verdict == BLIND
    assert out[0].seat == "paid"
    assert called["n"] == 0, "must not spend a call it cannot interpret"


def test_retrieval_calls_are_made_with_the_paid_key(monkeypatch):
    # A lane that silently fell back to anon would publish paid-seat verdicts
    # measured from a trimmed anon envelope.
    seen = {}
    sess = _Sess({pr.GROUND_TRUTH_TOOL: _truth(),
                  "semantic_search": _truth(),
                  "search_intelligence": _truth()})
    monkeypatch.setattr(pr.C, "seat_available", lambda seat: True)
    monkeypatch.setattr(pr.C, "PAID_KEY", "test-paid-key", raising=False)

    def _mk(url, api_key=None, timeout=None):
        seen["api_key"] = api_key
        return sess

    monkeypatch.setattr(pr, "MCPSession", _mk)
    pr._check_retrieval([])
    assert seen.get("api_key") == "test-paid-key"


def test_every_retrieval_finding_declares_the_paid_seat(patched):
    patched["install"]({pr.GROUND_TRUTH_TOOL: _truth(),
                        "semantic_search": _truth(),
                        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert out and all(f.seat == "paid" for f in out)
    assert all("paid MCP" in f.basis for f in out)


# ── retrieval: the decidable failure ────────────────────────────────────────

def test_empty_semantic_answer_for_known_entity_is_red(patched):
    patched["install"]({pr.GROUND_TRUTH_TOOL: _truth(),
                        "semantic_search": _env([]),
                        "search_intelligence": _env([])})
    out = []
    pr.probe(out)
    reds = [f for f in out if f.verdict == RED
            and "returns NOTHING" in f.title]
    assert len(reds) == 2, "both semantic doors must be judged"
    assert all(f.red_when for f in reds), "rule 2: red_when is required"
    assert all("Equinix DC12" in f.evidence for f in reds)


def test_found_entity_passes(patched):
    patched["install"]({pr.GROUND_TRUTH_TOOL: _truth(),
                        "semantic_search": _truth(),
                        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert _by_title(out, "finds a known-present entity")
    assert not [f for f in out if f.verdict == RED]


def test_name_is_read_from_a_nested_container(patched):
    # Measured live: a semantic_search row carries its name in cite.name and
    # nothing at top level. Reading top-level only produced a false "not
    # found" claim — shell #49's error, committed by this probe's first draft.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"cosine": 0.84, "source_table": "x",
                                  "cite": {"name": "Equinix DC12",
                                           "url": "u"}}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert _by_title(out, "finds a known-present entity")
    assert not [f for f in out if f.verdict == RED]


def test_free_text_mention_rescues_a_true_positive(patched):
    # No structured name anywhere, but the blob names the entity: that is the
    # corpus admitting it holds it. Loose enough to confirm a hit, which is
    # why it is NOT allowed to prove a miss.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"text": "Equinix DC12 — Ashburn, VA",
                                  "cosine": 0.9}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert _by_title(out, "finds a known-present entity")


def test_unreadable_row_shape_is_blind_not_a_miss(patched):
    # Rows returned, no name field anywhere, no mention. We cannot tell —
    # so we must not publish a claim about the platform.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"cosine": 0.42, "source_id": "9"}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    blinds = _by_title(out, "no readable entity name")
    assert len(blinds) == 1 and blinds[0].verdict == BLIND
    assert not _by_title(out, "was not among the returned names")


def test_substring_match_counts_as_found(patched):
    # "Equinix DC12" vs a result named "Equinix DC12 (Ashburn)" is the same
    # entity; demanding string equality would manufacture a false RED.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Equinix DC12 (Ashburn)",
                                  "citation": "s"}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert not [f for f in out if f.verdict == RED]


def test_wrong_results_are_a_gauge_not_a_red(patched):
    # Rule 3: ranking has no platform-declared threshold, so a non-empty answer
    # that misses the target reports a number and votes on nothing.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Somewhere Else", "citation": "s"}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    misses = _by_title(out, "was not among the returned names")
    assert misses and all(f.verdict == GAUGE for f in misses)
    assert all(f.severity == "info" for f in misses)


def test_jsonrpc_error_is_red_with_a_reason(patched):
    patched["install"]({pr.GROUND_TRUTH_TOOL: _truth(),
                        "semantic_search": {"_jsonrpc_error": {"code": -32000,
                                                               "message": "boom"}},
                        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    errs = [f for f in out if f.verdict == RED and "errors on a query" in f.title]
    assert len(errs) == 1 and "boom" in errs[0].evidence


# ── BLIND ≠ RED ─────────────────────────────────────────────────────────────

def test_preview_envelope_is_blind_not_red(patched):
    # A spent anon budget returns a trimmed preview. Judging retrieval off it
    # measures the paywall — the 4-day blindness this harness already paid for.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([], preview_is_partial=True),
        "search_intelligence": _env([], trial_preview=True)})
    out = []
    pr.probe(out)
    assert not [f for f in out if f.verdict == RED]
    assert len([f for f in out if f.verdict == BLIND]) == 2


def test_withheld_remainder_makes_recall_unobserved(patched):
    # Measured live: the anon seat gets ONE row plus `_results_total_in_pro`.
    # A miss inside a set trimmed to one row is the paywall, not recall — and
    # a GAUGE that can only ever say "not found" is a permanent false positive.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Some Other Site"}],
                                _results_total_in_pro=97),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert not _by_title(out, "was not among the returned names")
    trimmed = _by_title(out, "trimmed for this seat")
    assert len(trimmed) == 1 and trimmed[0].verdict == BLIND
    assert "_results_total_in_pro=97" in trimmed[0].evidence


def test_zero_remainder_does_not_count_as_trimmed(patched):
    # `_results_total_in_pro: 0` means nothing was withheld — treating it as a
    # trim would blind the probe on every complete answer.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Equinix DC12", "citation": "s"}],
                                _results_total_in_pro=0),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert not _by_title(out, "trimmed for this seat")
    assert _by_title(out, "finds a known-present entity")


def test_unreachable_ground_truth_is_blind(patched, monkeypatch):
    from tools.qa_superuser.http import Unreachable
    patched["install"]({pr.GROUND_TRUTH_TOOL: Unreachable("dns")})
    out = []
    pr.probe(out)
    assert len(out) == 1 and out[0].verdict == BLIND


def test_no_ground_truth_name_is_blind_not_red(patched):
    # Without a known-present entity there is nothing to hold search to.
    patched["install"]({pr.GROUND_TRUTH_TOOL: _env([{"id": 7}])})
    out = []
    pr.probe(out)
    assert len(out) == 1 and out[0].verdict == BLIND
    assert "nothing to hold the semantic door to" in out[0].evidence


def test_unreachable_semantic_door_is_blind(patched):
    from tools.qa_superuser.http import Unreachable
    patched["install"]({pr.GROUND_TRUTH_TOOL: _truth(),
                        "semantic_search": Unreachable("timeout"),
                        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert len([f for f in out if f.verdict == BLIND]) == 1
    assert not [f for f in out if f.verdict == RED]


# ── citations ───────────────────────────────────────────────────────────────

def test_zero_citations_anywhere_is_red(patched):
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Equinix DC12"}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    cites = [f for f in out if "NO citation" in f.title]
    assert len(cites) == 1 and cites[0].verdict == RED and cites[0].red_when


def test_envelope_level_citation_covers_uncited_rows(patched):
    # The platform cites at envelope level too; demanding per-row citations
    # would invent a stricter contract than the one it publishes.
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Equinix DC12"}],
                                citation={"url": "x"}),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert not [f for f in out if "NO citation" in f.title]


def test_partial_citation_coverage_is_a_gauge(patched):
    patched["install"]({
        pr.GROUND_TRUTH_TOOL: _truth(),
        "semantic_search": _env([{"name": "Equinix DC12", "citation": "s"},
                                 {"name": "Other"}]),
        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    cov = _by_title(out, "citation coverage")
    assert cov and any(f.verdict == GAUGE for f in cov)


# ── latency: the only hard threshold is the edge's own ─────────────────────

def _fake_clock(monkeypatch, per_call_s):
    """Make each fetch appear to take `per_call_s` seconds."""
    state = {"t": 0.0}

    def _mono():
        return state["t"]

    def _fetch(url, **kw):
        state["t"] += per_call_s
        return 200, {}, "{}"

    monkeypatch.setattr(pr.time, "monotonic", _mono)
    monkeypatch.setattr(pr, "fetch", _fetch)


def test_fast_endpoint_is_a_gauge_never_a_pass_claim(monkeypatch):
    # Rule 3: the platform declares no "fast" target, so a quick response
    # reports a number and makes no claim.
    _fake_clock(monkeypatch, 0.2)
    out = []
    pr._check_latency(out)
    assert out and all(f.verdict == GAUGE for f in out)
    assert all(f.severity == "info" for f in out)


def test_over_edge_budget_is_red(monkeypatch):
    _fake_clock(monkeypatch, pr.EDGE_TIMEOUT_S + 1)
    out = []
    pr._check_latency(out)
    assert out and all(f.verdict == RED for f in out)
    assert all("route budget" in f.title for f in out)
    assert all(f.red_when and f.remedy for f in out)


def test_latency_uses_the_median_not_the_worst(monkeypatch):
    # One cold sample must not convict an endpoint.
    seq = iter([0.1, 30.0, 0.1] * len(pr.LATENCY_PATHS))
    state = {"t": 0.0}
    monkeypatch.setattr(pr.time, "monotonic", lambda: state["t"])

    def _fetch(url, **kw):
        state["t"] += next(seq)
        return 200, {}, "{}"

    monkeypatch.setattr(pr, "fetch", _fetch)
    out = []
    pr._check_latency(out)
    assert all(f.verdict == GAUGE for f in out), \
        "median of [100, 30000, 100] is 100ms — a single cold call is not a " \
        "slow endpoint"
    assert all("/" in f.evidence for f in out), "the spread must be published"


def test_latency_samples_are_cache_busted(monkeypatch):
    seen = []
    state = {"t": 0.0}
    monkeypatch.setattr(pr.time, "monotonic", lambda: state["t"])

    def _fetch(url, **kw):
        seen.append(url)
        state["t"] += 0.1
        return 200, {}, "{}"

    monkeypatch.setattr(pr, "fetch", _fetch)
    pr._check_latency([])
    assert len(seen) == len(pr.LATENCY_PATHS) * pr.LATENCY_SAMPLES
    assert all("?_=" in u for u in seen), \
        "an edge-cached response measures Cloudflare, not the origin"
    assert len(set(seen)) == len(seen), "each sample needs its own cache key"


def test_unreachable_path_is_blind(monkeypatch):
    from tools.qa_superuser.http import Unreachable

    def _boom(url, **kw):
        raise Unreachable("connection reset")

    monkeypatch.setattr(pr, "fetch", _boom)
    out = []
    pr._check_latency(out)
    assert out and all(f.verdict == BLIND for f in out)


# ── construction discipline across every finding this module can emit ──────

def test_every_red_capable_finding_states_basis_and_red_when(patched):
    patched["install"]({pr.GROUND_TRUTH_TOOL: _truth(),
                        "semantic_search": _env([]),
                        "search_intelligence": _truth()})
    out = []
    pr.probe(out)
    assert out
    for f in out:
        assert f.basis, f"{f.title} has no basis (rule 4)"
        if f.verdict == RED:
            assert f.red_when, f"{f.title} is RED with no red_when (rule 2)"


def test_probe_is_registered_in_the_runner():
    """A probe nobody calls is the built-but-dark class; assert the wiring.

    ★ Asserts against the REGISTRY OBJECT, not `inspect.getsource(collect)`.
      The source grep was satisfied by the *name appearing in the file* — a
      comment would have passed it (#37's "comments satisfy grep"), and it said
      nothing about whether the probe is CALLABLE the way the runner calls it.
      `probe_registries` was registered by that standard and still produced zero
      verdicts on every run for two days, because its signature did not match.
      run.PROBES cannot lie about membership, and the signature-binding test in
      tests/test_qa_superuser.py covers the half this one never could.
    """
    from tools.qa_superuser import probe_retrieval
    from tools.qa_superuser.run import PROBES
    assert ("retrieval", probe_retrieval) in PROBES
