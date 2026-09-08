"""Suppression must narrow the detector, not blind it.

2026-09-08. The detector's first run reported 12 dead probes, of which 2 were
real. A sensor that fires on healthy input is not a sensor — the same failure
as the corpora_missing sensor that fired on ~100% of healthy traffic. But the
fix for over-reporting is the one most likely to turn a working detector into
a silent one, so every rule here is pinned from both sides: it must fire on
the case it was written for, and it must NOT fire on anything else.

After these rules, against the live tree: 9 dead -> 3 reported, 6 suppressed,
and the 3 are exactly the three that hand-triage called real (request_log,
request_log_404, signup_events).
"""
import pathlib
import textwrap

import pytest

from routes import brain_null_signal_detector as nsd


def _tree(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return tmp_path


def test_plain_dead_probe_is_still_reported(tmp_path):
    """The load-bearing negative. If this ever passes a probe through, the
    detector has been turned off rather than tuned."""
    root = _tree(tmp_path, {"routes/live.py": """
        import other
        def go(cur):
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "other.py": "import live\n"})
    assert nsd.classify(root, "public.ghost", ["routes/live.py:4"]) is None


def test_lazy_create_is_suppressed(tmp_path):
    root = _tree(tmp_path, {"routes/lazy.py": """
        import other
        DDL = "CREATE TABLE IF NOT EXISTS wall_hits (id int)"
        def go(cur):
            cur.execute("SELECT to_regclass('public.wall_hits')")
    """, "other.py": "import lazy\n"})
    got = nsd.classify(root, "public.wall_hits", ["routes/lazy.py:5"])
    assert got and got["rules"] == ["lazy_create"]


def test_unreachable_module_is_suppressed(tmp_path):
    root = _tree(tmp_path, {"orphan.py": """
        def go(cur):
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "other.py": "print('this file names no dead module')\n"})
    got = nsd.classify(root, "public.ghost", ["orphan.py:3"])
    assert got and got["rules"] == ["unreachable_module"]


def test_a_single_mention_anywhere_defeats_unreachable(tmp_path):
    """Blunt on purpose: a module reached by a mechanism this scan does not
    understand must never be suppressed by mistake."""
    root = _tree(tmp_path, {"orphan.py": """
        def go(cur):
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "loader.py": "MODULES = ['orphan']  # dynamic import\n"})
    assert nsd.classify(root, "public.ghost", ["orphan.py:3"]) is None


def test_annotation_is_suppressed_and_carries_its_reason(tmp_path):
    root = _tree(tmp_path, {"routes/a.py": """
        import other
        def go(cur):
            # null-signal: feature not built yet
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "other.py": "import a\n"})
    got = nsd.classify(root, "public.ghost", ["routes/a.py:5"])
    assert got and got["rules"] == ["annotated"]
    assert got["sites"][0]["why"] == "feature not built yet"


def test_annotation_does_not_reach_beyond_its_lookback(tmp_path):
    root = _tree(tmp_path, {"routes/a.py": """
        import other
        # null-signal: stale note far above
        def go(cur):
            x = 1
            y = 2
            z = 3
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "other.py": "import a\n"})
    assert nsd.classify(root, "public.ghost", ["routes/a.py:8"]) is None


def test_one_live_site_defeats_suppression_of_the_others(tmp_path):
    """A probe is only by-design if EVERY call site is. One live site reading
    a table that does not exist is still a real defect."""
    root = _tree(tmp_path, {"routes/lazy.py": """
        import other
        DDL = "CREATE TABLE IF NOT EXISTS ghost (id int)"
        def go(cur):
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "routes/live.py": """
        import other
        def go(cur):
            cur.execute("SELECT to_regclass('public.ghost')")
    """, "other.py": "import lazy\nimport live\n"})
    assert nsd.classify(
        root, "public.ghost",
        ["routes/lazy.py:5", "routes/live.py:4"]) is None


def test_suppressed_findings_are_published_not_swallowed():
    """A suppression nobody can see is a way to hide findings."""
    src = pathlib.Path(nsd.__file__).read_text()
    assert '"suppressed"' in src and '"suppressed_count"' in src, (
        "the route must publish what it suppressed, with the rule that did it")
    assert '"dead_probe_count_unsuppressed"' in src, (
        "the pre-suppression count must stay visible, or a rule that starts "
        "eating everything looks identical to a healthy tree")


def test_split_dead_partitions_without_losing_any(tmp_path):
    root = _tree(tmp_path, {"routes/lazy.py": """
        import other
        DDL = "CREATE TABLE IF NOT EXISTS a (id int)"
        def go(cur):
            cur.execute("SELECT to_regclass('public.a')")
    """, "routes/live.py": """
        import other
        def go(cur):
            cur.execute("SELECT to_regclass('public.b')")
    """, "other.py": "import lazy\nimport live\n"})
    dead = [{"probed": "public.a", "sites": ["routes/lazy.py:5"], "site_count": 1},
            {"probed": "public.b", "sites": ["routes/live.py:4"], "site_count": 1}]
    out = nsd.split_dead(dead, root=root)
    assert len(out["reported"]) + len(out["suppressed"]) == len(dead)
    assert [d["probed"] for d in out["reported"]] == ["public.b"]
