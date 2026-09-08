"""The detector must not become defect number nine.

Every defect in the 2026-09-07/08 brain sweep failed as SILENCE: a gate that
rejected 45 of 45, a heartbeat key simply absent, a counter pinned at a
confident 0, twelve workflow runs all `skipped` in neutral grey. A detector
that hunts that shape is subject to it -- a scanner that silently scans
nothing reports a clean bill of health forever, and it will be believed
precisely because it is the thing that was supposed to catch this.

So the self-test is the real subject here, not the scan. Its three legs are
each pinned against the failure they exist for:

    scan_floor      a broken regex finds zero probes -> "no dead probes"
    negative canary a comparison that always passes -> misses everything
    positive canary an empty relation list -> flags all 69 and buries the real

and `ok` must be False with findings WITHHELD whenever any leg fails, because
an unverified scanner's output is worse than no output.

Stdlib + pytest; no DB, no network.
"""
import textwrap

import pytest

from routes import brain_null_signal_detector as d


# ── the scan ──────────────────────────────────────────────────────────
def test_scan_finds_probes_in_the_real_tree():
    """Non-vacuous by construction: the repo genuinely contains these."""
    probes = d.scan_table_probes()
    assert len(probes) >= d._MIN_PROBES, (
        f"only {len(probes)} probes found; the scan or the regex is broken")


def test_scan_records_file_and_line(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent("""
        def f(cur):
            cur.execute("SELECT to_regclass('public.widgets')")
    """))
    probes = d.scan_table_probes(tmp_path)
    assert "public.widgets" in probes
    assert probes["public.widgets"][0].endswith("m.py:3")


def test_scan_skips_tests_dir(tmp_path):
    """A test may probe a fixture table that production does not have;
    flagging it would train people to ignore this detector."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("to_regclass('fixture_only')\n")
    assert "fixture_only" not in d.scan_table_probes(tmp_path)


@pytest.mark.parametrize("src,expected", [
    ("to_regclass('public.a')", "public.a"),
    ('to_regclass("b")', "b"),
    ("to_regclass( 'public.c' )", "public.c"),
])
def test_probe_regex_shapes(tmp_path, src, expected):
    (tmp_path / "m.py").write_text(src + "\n")
    assert expected in d.scan_table_probes(tmp_path)


# ── the comparison ────────────────────────────────────────────────────
def test_schema_qualified_and_bare_names_both_resolve():
    """to_regclass resolves through search_path, so `public.x` and `x` are
    the same relation. Treating them differently would report false deaths."""
    live = {"widgets"}
    assert d.dead_probes({"public.widgets": ["a:1"]}, live) == []
    assert d.dead_probes({"widgets": ["a:1"]}, live) == []


def test_a_missing_table_is_reported_with_its_sites():
    out = d.dead_probes({"public.gone": ["r/x.py:9", "r/y.py:2"]}, {"kept"})
    assert len(out) == 1
    assert out[0]["probed"] == "public.gone"
    assert out[0]["site_count"] == 2


# ── the self-test: one leg per failure mode ───────────────────────────
def test_scan_floor_fails_when_the_scan_finds_nothing():
    """THE dangerous green: a broken scan reports no dead probes."""
    st = d._self_test({}, {"anything"})
    assert st["passed"] is False
    assert st["legs"]["scan_floor"]["passed"] is False


def test_positive_canary_fails_on_an_empty_relation_list():
    """A failed/empty table query would mark all 69 probes dead."""
    st = d._self_test({f"p{i}": ["x:1"] for i in range(d._MIN_PROBES)}, set())
    assert st["passed"] is False
    assert st["legs"]["positive_canary"]["passed"] is False


def test_negative_canary_fails_if_nothing_can_be_reported_dead(monkeypatch):
    """A comparison that always passes misses every real defect."""
    monkeypatch.setattr(d, "dead_probes", lambda probes, live: [])
    st = d._self_test({f"p{i}": ["x:1"] for i in range(d._MIN_PROBES)},
                      {d._CANARY_PRESENT})
    assert st["passed"] is False
    assert st["legs"]["negative_canary"]["passed"] is False


def test_all_three_legs_pass_on_a_healthy_run():
    st = d._self_test({f"p{i}": ["x:1"] for i in range(d._MIN_PROBES)},
                      {d._CANARY_PRESENT})
    assert st["passed"] is True
    assert all(l["passed"] for l in st["legs"].values())


def test_the_present_canary_is_a_table_that_really_exists():
    """The positive canary must name a relation the codebase actually uses,
    or the leg is decorative. This one is the table defect #2 should have
    named -- brain_proposed_code_fixes, not brain_proposed_code."""
    assert d._CANARY_PRESENT == "brain_proposed_code_fixes"
    probes = d.scan_table_probes()
    assert any(p.split(".")[-1] == d._CANARY_PRESENT for p in probes), (
        "the positive canary is no longer probed anywhere in the tree")


def test_the_absent_canary_cannot_collide_with_a_real_table():
    assert d._CANARY_MISSING.split(".")[-1].startswith("__")


# ── it must not read its own prose as evidence (2026-09-08) ───────────
# Its FIRST live run reported three findings -- `public.X`, `public.foo`
# and `foo` -- every one from its own docstring and the comment above
# _PROBE_RE, where the idiom is spelled out to explain it. 3 of 12
# findings were noise it manufactured about itself. A scanner that cites
# its own documentation is the same class of defect it exists to hunt.

def test_it_does_not_scan_its_own_module():
    """The three placeholder names from its docstring must not appear."""
    probes = d.scan_table_probes()
    for placeholder in ("foo", "public.foo", "public.X"):
        assert placeholder not in probes, (
            f"{placeholder!r} came from this detector's own prose — it is "
            f"reading its documentation as evidence")


def test_comment_lines_are_not_probes(tmp_path):
    (tmp_path / "m.py").write_text(
        "# to_regclass('public.explained_in_a_comment')\n"
        "cur.execute(\"SELECT to_regclass('public.real_one')\")\n")
    probes = d.scan_table_probes(tmp_path)
    assert "public.real_one" in probes
    assert "public.explained_in_a_comment" not in probes


def test_an_indented_comment_is_also_skipped(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f():\n    # to_regclass('public.indented_comment')\n    pass\n")
    assert "public.indented_comment" not in d.scan_table_probes(tmp_path)


def test_skipping_itself_does_not_collapse_the_scan():
    """Excluding one file must not take the scan under its floor — that
    would trade a noise bug for a silent one."""
    probes = d.scan_table_probes()
    assert len(probes) >= d._MIN_PROBES, (
        f"{len(probes)} probes after self-exclusion, floor {d._MIN_PROBES}")
