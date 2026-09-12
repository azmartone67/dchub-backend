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
from routes.brain_source_map import resolve_finding_to_sources


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
