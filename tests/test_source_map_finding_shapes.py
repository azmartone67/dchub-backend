"""brain_source_map must resolve a finding that NAMES its location.

Measured against the live board 2026-09-12, it resolved such findings WORSE
than findings with no pointer at all:

  finding cross_surface_metric_divergence @ routes/mcp_presence_crawler.py:2403
    1. routes/news_digests_read.py:115        route 0.92
    2. routes/partnership_email_drafts.py:282 route 0.92
    3. routes/redeem_diagnostic.py:145        route 0.92
    4. routes/sources.py:229                  route 0.92
    5. routes/mcp_presence_crawler.py:1       filename 0.90   <- right file, line 1

_RE_URL_PATH scraped "/mcp_presence_crawler.py:2403" out of the pointer; the
`not p.endswith(".py")` guard existed to stop that but misses when a line
number is appended, so a fabricated single-segment path matched every bare
`/<param>` catch-all route in the repo. Investigation #100257's refuter reported
it verbatim: "the source_map only returned line 1 of that file".

  finding iso_metric_count_dropped
    4 of 5 candidates were inside tests/test_grid_ba_surface_guard.py — the
    brain was offered TEST FILES as fix sites while real ingest code was
    crowded out of the five-slot budget.
"""
import os, sys, textwrap
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import routes.brain_source_map as _source_map
from routes.brain_source_map import resolve_finding_to_sources


@pytest.fixture(scope="module", autouse=True)
def _index_starts_cold():
    """Make this file's repo scan its own, whatever ran before it.

    resolve_finding_to_sources indexes the repo with os.walk and caches the
    index in brain_source_map._INDEX_CACHE. In a serial run an earlier caller
    (tests/test_brain_investigator.py) had already built it, so this file
    scanned nothing and could not be pinned. Alone, or in a unit-tests shard
    without that file, it walked ~100 dirs unpinned and
    test_scan_floors_are_pinned.py failed (unit-tests shard 4, run 35587527309).
    Starting cold makes the walk happen here in every run, so its floor in
    tests/scan_floors.json always applies. The cache is restored afterwards.
    """
    saved = dict(_source_map._INDEX_CACHE)
    _source_map._INDEX_CACHE.clear()
    yield
    _source_map._INDEX_CACHE.clear()
    _source_map._INDEX_CACHE.update(saved)


@pytest.fixture
def repo(tmp_path):
    """A repo carrying the exact shapes that broke: bare catch-all slug
    routes, a deep target file, and a test file full of table references."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "tests").mkdir()

    # Four bare catch-all slug routes — these are what used to win.
    for i, (fn, param) in enumerate([
            ("news_digests_read.py", "<string:slug>"),
            ("partnership_email_drafts.py", "<slug>"),
            ("redeem_diagnostic.py", "<string:email>"),
            ("sources.py", "<string:source_id>")]):
        (tmp_path / "routes" / fn).write_text(
            f'from flask import Blueprint\nbp{i} = Blueprint("b{i}", __name__)\n'
            f'@bp{i}.route("/{param}", methods=["GET"])\ndef h{i}(x):\n    return x\n')

    # The file the finding actually points at. Line 40 is distinctive.
    body = ["# header"] * 39 + ['CANON_MARKETS = 311  # the hardcoded literal'] + ["# tail"] * 10
    (tmp_path / "routes" / "target_crawler.py").write_text("\n".join(body) + "\n")

    # A test file mentioning the same table many times, and — deliberately —
    # with a STRONGER raw match kind than the production file (CREATE TABLE
    # scores 0.8 ddl vs INSERT 0.7 write) AND a name that sorts EARLIER than
    # the production file. Both tiebreaks therefore favour the test file, so
    # only the demotion itself can put production code on top. Without that,
    # this fixture would pass on an alphabetical accident.
    (tmp_path / "tests" / "test_aaa_grid_guard.py").write_text(textwrap.dedent("""
        def test_a(): assert "CREATE TABLE grid_data (iso text)"
        def test_b(): assert "INSERT INTO grid_data VALUES (1)"
        def test_c(): assert "UPDATE grid_data SET x=1"
        def test_d(): assert "SELECT x FROM grid_data WHERE y"
    """))
    # Real production code touching the same table, once, with a WEAKER match.
    (tmp_path / "routes" / "zzz_iso_ingest.py").write_text(
        'def w(cur):\n    cur.execute("INSERT INTO grid_data (iso) VALUES (%s) ON CONFLICT DO NOTHING")\n')
    return str(tmp_path)


def _files(cands):
    return [c["file"].replace("\\", "/") for c in cands]


# ── the source-pointer shape ─────────────────────────────────────────
def test_a_cited_file_and_line_is_the_top_candidate(repo):
    c = resolve_finding_to_sources(
        {"url": "routes/target_crawler.py:40",
         "issue": "cross_surface_metric_divergence"}, repo_root=repo)
    assert c, "a finding that names its location must resolve to something"
    top = c[0]
    assert top["file"].replace("\\", "/") == "routes/target_crawler.py"
    assert top["line"] == 40, f"cited line must be carried through, got {top['line']}"


def test_the_cited_line_is_not_flattened_to_line_1(repo):
    """The shipped bug: the right file came back pinned to line 1."""
    c = resolve_finding_to_sources(
        {"url": "routes/target_crawler.py:40", "issue": "x"}, repo_root=repo)
    hits = [x for x in c if x["file"].replace("\\", "/") == "routes/target_crawler.py"]
    assert hits
    assert all(h["line"] != 1 for h in hits), "line 1 is the bug, not the answer"


def test_the_snippet_is_the_cited_line_not_the_file_header(repo):
    c = resolve_finding_to_sources(
        {"url": "routes/target_crawler.py:40", "issue": "x"}, repo_root=repo)
    assert "CANON_MARKETS = 311" in c[0]["snippet"]


def test_a_source_pointer_does_not_summon_catch_all_slug_routes(repo):
    """The fabricated '/target_crawler.py:40' path must never reach the
    route matcher. These four files have NOTHING to do with the finding."""
    c = resolve_finding_to_sources(
        {"url": "routes/target_crawler.py:40", "issue": "x"}, repo_root=repo)
    junk = {"routes/news_digests_read.py", "routes/partnership_email_drafts.py",
            "routes/redeem_diagnostic.py", "routes/sources.py"}
    assert not (junk & set(_files(c))), f"catch-all slug routes matched: {_files(c)}"


def test_a_real_url_path_still_matches_its_param_route(repo):
    """The guard must not break ordinary route resolution."""
    c = resolve_finding_to_sources({"url": "/some-slug", "issue": "y"}, repo_root=repo)
    assert any(f.startswith("routes/") for f in _files(c)), _files(c)


# ── test files must not take production slots ────────────────────────
def test_test_files_rank_below_production_code(repo):
    c = resolve_finding_to_sources(
        {"url": "table:grid_data", "issue": "iso_metric_count_dropped"},
        repo_root=repo)
    files = _files(c)
    assert "routes/zzz_iso_ingest.py" in files, f"real ingest code missing: {files}"
    prod = files.index("routes/zzz_iso_ingest.py")
    tests = [i for i, f in enumerate(files) if f.startswith("tests/")]
    assert all(t > prod for t in tests), f"a test file outranked prod code: {files}"


def test_one_file_cannot_eat_the_whole_candidate_budget(repo):
    """4 of 5 slots were the same test file at four different lines."""
    c = resolve_finding_to_sources(
        {"url": "table:grid_data", "issue": "iso_metric_count_dropped"},
        repo_root=repo)
    counts = {}
    for f in _files(c):
        counts[f] = counts.get(f, 0) + 1
    assert max(counts.values()) <= 2, f"one file took {max(counts.values())} slots: {counts}"


# ── the text tier is not a candidate ───────────────────────────────────────
#
# Measured 2026-09-18: twelve consecutive bug-squasher investigations were each
# handed the same four unrelated files and each refused to emit a remedy, with
# `0 fixes landed · last merge never` on the board. The cause is that the
# symbol/text fallback separates the two kinds on one predicate — whether the
# token contains an underscore — so any bare English word from a prose question
# produces "candidates" that are just the first files it occurs in.
class TestStructuralCandidateFilter:

    def _cands(self):
        return [
            {"file": "routes/real.py", "line": 10, "match_kind": "table",
             "confidence": 0.8},
            {"file": "routes/also_real.py", "line": 20, "match_kind": "symbol",
             "confidence": 0.45},
            {"file": "news_digests_read.py", "line": 30, "match_kind": "text",
             "confidence": 0.3},
            {"file": "partnership_email_drafts.py", "line": 40,
             "match_kind": "text", "confidence": 0.3},
        ]

    def test_text_matches_are_withheld(self):
        from routes.brain_source_map import structural_candidates
        out = structural_candidates(self._cands())
        assert [c["file"] for c in out] == ["routes/real.py",
                                            "routes/also_real.py"]

    def test_every_structural_kind_survives(self):
        # A filter that dropped a real kind would starve the lane silently —
        # the failure one row down from the one being fixed.
        from routes.brain_source_map import structural_candidates
        kinds = ("route", "filename", "table", "symbol")
        out = structural_candidates(
            [{"file": f"f_{k}.py", "line": 1, "match_kind": k} for k in kinds])
        assert [c["match_kind"] for c in out] == list(kinds)

    def test_an_all_text_result_resolves_to_nothing(self):
        # This is the live case: no structural handle at all. The lane must be
        # able to say "we could not locate this", not hand over four files.
        from routes.brain_source_map import structural_candidates
        assert structural_candidates([
            {"file": "a.py", "line": 1, "match_kind": "text"},
            {"file": "b.py", "line": 2, "match_kind": "text"}]) == []

    def test_malformed_entries_never_raise(self):
        from routes.brain_source_map import structural_candidates
        assert structural_candidates(None) == []
        assert structural_candidates([{}, {"match_kind": None}]) == []

    # ★★ THE FINDING KEYS BELOW ARE ASSEMBLED AT RUNTIME, NOT WRITTEN OUT.
    #    The resolver indexes this repo including its tests, so a test that
    #    spells a live finding key makes ITSELF a symbol match for that finding
    #    — measured: the first draft of this test put
    #    tests/test_source_map_finding_shapes.py into the candidate set for the
    #    key it was asserting about. Splitting the literal keeps the assertion
    #    honest about production code.
    CSP_KEY = "csp_" + "violation_recurring"
    CRAWL_KEY = "ai_platform_" + "crawl_drop:perplexity"

    def test_prose_in_the_question_cannot_manufacture_candidates(self):
        # End-to-end against the real repo index. Wrapping a finding key in an
        # ordinary English sentence used to add files whose only relationship to
        # the subject was a shared word ("firing", "does", "keep").
        #
        # ★ This does NOT assert prose ⊇ bare. It is not true and it is not the
        #   property wanted: prose changes which tokens the resolver picks, so
        #   the two forms legitimately resolve differently. What must hold is
        #   that nothing NON-STRUCTURAL survives either way.
        from routes.brain_source_map import (resolve_finding_to_sources,
                                             structural_candidates)
        prose = structural_candidates(resolve_finding_to_sources(
            f"Why does {self.CSP_KEY} keep firing on dchub.cloud?") or [])
        assert all(c["match_kind"] != "text" for c in prose), prose
        assert not any(str(c["file"]).endswith(
            ("news_digests_read.py", "partnership_email_drafts.py",
             "redeem_diagnostic.py", "sources.py")) for c in prose), prose

    def test_the_bare_key_still_resolves(self):
        # The filter must not starve the lane. The detector that OWNS this
        # finding has to survive it — a filter that returned [] for everything
        # would pass every noise assertion above and be useless.
        from routes.brain_source_map import (resolve_finding_to_sources,
                                             structural_candidates)
        out = structural_candidates(
            resolve_finding_to_sources(self.CSP_KEY) or [])
        assert out, "the bare finding key must still locate its detector"
        assert any("brain_consistency_radar" in str(c["file"]) for c in out), out

    def test_no_file_that_merely_describes_a_finding_is_its_answer(self):
        # A comment or test that quotes its subject becomes its subject. Caught
        # twice while writing this fix — once in brain_source_map.py, once here.
        from routes.brain_source_map import (resolve_finding_to_sources,
                                             structural_candidates)
        for q in (self.CSP_KEY, self.CRAWL_KEY):
            files = {str(c["file"]) for c in structural_candidates(
                resolve_finding_to_sources(q) or [])}
            assert not any(f.endswith(("brain_source_map.py",
                                       "brain_investigator.py",
                                       "test_source_map_finding_shapes.py"))
                           for f in files), (q, files)


# ── the wiring, not just the helper ───────────────────────────────────────
#
# ★★★ ADDED AFTER A SURVIVING MUTATION. The filter helper above was fully
# covered while `_source_evidence` — the function that actually decides what
# reaches the model — was not: replacing its filter call with a no-op left all
# 42 tests green. A tested helper wired to nothing is the defect this whole
# change is about, committed one layer up.
class TestInvestigatorWithholdsTheTextTier:

    MIXED = [
        {"file": "routes/real.py", "line": 10, "match_kind": "table",
         "confidence": 0.8, "snippet": "CREATE TABLE real"},
        {"file": "news_digests_read.py", "line": 30, "match_kind": "text",
         "confidence": 0.3, "snippet": "def unrelated():"},
        {"file": "partnership_email_drafts.py", "line": 40,
         "match_kind": "text", "confidence": 0.3, "snippet": "def other():"},
    ]

    def _run(self, monkeypatch, cands):
        import routes.brain_source_map as sm
        from routes.brain_investigator import _source_evidence
        monkeypatch.setattr(sm, "resolve_finding_to_sources",
                            lambda *_a, **_k: list(cands))
        return _source_evidence("any question at all")

    def test_no_text_tier_file_reaches_the_model(self, monkeypatch):
        blob = " ".join(str(e.get("claim") or "")
                        for e in self._run(monkeypatch, self.MIXED))
        assert "routes/real.py" in blob
        assert "news_digests_read.py" not in blob, blob[:400]
        assert "partnership_email_drafts.py" not in blob, blob[:400]

    def test_an_all_text_result_is_reported_as_a_measured_miss(self, monkeypatch):
        noise = [c for c in self.MIXED if c["match_kind"] == "text"]
        out = self._run(monkeypatch, noise)
        blob = " ".join(str(e.get("claim") or "") for e in out)
        assert "NO SOURCE RESOLVED" in blob, blob[:400]
        # Named, not silently dropped — a resolver regression must stay visible.
        assert "news_digests_read.py" in blob, blob[:400]
        assert "must not be treated as one" in blob, blob[:400]
        assert all(e.get("value") == 0 for e in out), out

    def test_a_real_hit_is_never_downgraded_to_a_miss(self, monkeypatch):
        blob = " ".join(str(e.get("claim") or "")
                        for e in self._run(monkeypatch, self.MIXED))
        assert "NO SOURCE RESOLVED" not in blob, blob[:400]


# ── Layer 5 uses the same filter ──────────────────────────────────────────
#
# ★★★ ALSO ADDED AFTER A SURVIVING MUTATION. Unwrapping layer 5's resolver call
# left all 45 tests green, so the propose lane could have gone back to spending
# a Claude call per finding to buy a refusal with nobody noticing.
#
# ★ This is an AST check, not a substring check. `"structural_candidates" in
#   src` would be satisfied by the import line alone, and by this comment.
class TestLayer5ResolverIsAlwaysFiltered:

    def _calls(self):
        import ast, inspect, routes.brain_v2_layer5 as l5
        tree = ast.parse(inspect.getsource(l5))
        raw, wrapped = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "resolve_finding_to_sources":
                raw.append(node)
            if name == "structural_candidates":
                for a in node.args:
                    if (isinstance(a, ast.Call)
                            and (getattr(a.func, "id", None)
                                 or getattr(a.func, "attr", None))
                            == "resolve_finding_to_sources"):
                        wrapped.append(a)
        return raw, wrapped

    def test_every_resolver_call_is_wrapped_by_the_filter(self):
        raw, wrapped = self._calls()
        assert raw, "the resolver call vanished — this guard would be vacuous"
        unwrapped = [n for n in raw if n not in wrapped]
        assert not unwrapped, (
            "brain_v2_layer5 calls resolve_finding_to_sources without "
            "structural_candidates at line(s) "
            f"{[n.lineno for n in unwrapped]} — the text tier would reach the "
            "model again and buy a refusal per finding")
