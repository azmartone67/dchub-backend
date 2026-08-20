"""Guard: a repo-wide scan must see the same repo wherever the checkout lives.

What happened (2026-08-19):

Three guards computed their skip-list against each file's ABSOLUTE path::

    SKIP = (".claude/", "worktrees/", "node_modules/", "/.git/", "/tests/")
    if any(d in str(f) for d in SKIP):
        continue

The intent is right — skip nested worktrees and vendored copies found INSIDE a
checkout. But an absolute path also carries the checkout's own ANCESTORS, and
Claude Code puts its worktrees at ``~/dchub-backend/.claude/worktrees/<name>/``.
Run the suite from one and every file in the repo matches ".claude/": 3,474
found, 3,474 skipped, 0 survivors.

The two halves of the damage, which is why this file guards BOTH:

  1. LOUD — test_sql_literal_percent and test_stripe_route_auth have non-vacuity
     floors, so they failed: 17 red on a PRISTINE main. A clean tree that looks
     broken is not a harmless annoyance; it is what makes "these failures are
     pre-existing" the reasonable-sounding wrong conclusion.

  2. SILENT — test_honest_numbers, the drift-fence that owns the canonical
     counts, had NO floor. Every one of its assertions is "no forbidden pattern
     was found", and nothing was found because nothing was read. It scanned 0
     files instead of 2,027 and reported green while guarding nothing.

So the property is not "the scan passes" — a scan of nothing always passes. It
is that the scan still SEES the repo, and still skips what it meant to skip,
from a path built to trip it.
"""
import importlib.util
import pathlib
import textwrap

TESTS = pathlib.Path(__file__).resolve().parent

# A parameterized query whose SQL COMMENT carries a lone percent — the exact
# shape that blanked the customer board. psycopg2 scans comments too.
_BAD_SQL = textwrap.dedent("""\
    def measure(cur, plans):
        cur.execute("SELECT u.email FROM users WHERE plan = %s "
                    "-- only 'sent%' is a delivery", (plans,))
""")

_BAD_ROUTE = textwrap.dedent("""\
    from flask import Flask
    app = Flask(__name__)

    @app.route("/api/v1/stripe/webhook-planted", methods=["POST"])
    def planted():
        return {"ok": True}
""")

_BAD_NUMBER = 'HEADLINE = "the $324B in tracked M&A"\n'


def _load(name):
    """Import a sibling guard by path. Kept inside a function: nothing under
    tests/ may run at module scope."""
    path = TESTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_pathdep_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_checkout(tmp_path):
    """A miniature checkout sitting exactly where Claude Code puts worktrees.

    It plants each violation TWICE: once in real source, and once inside a
    nested .claude/worktrees/ copy. A fix that merely stopped skipping would
    find both — and re-introduce the double-reporting the skip-list exists to
    prevent. The nested copy must stay invisible.
    """
    root = tmp_path / ".claude" / "worktrees" / "session-x"
    (root / "routes").mkdir(parents=True)
    (root / "tests").mkdir()
    nested = root / ".claude" / "worktrees" / "inner" / "routes"
    nested.mkdir(parents=True)

    for target in (root / "routes", nested):
        (target / "billing.py").write_text(_BAD_SQL + _BAD_ROUTE, encoding="utf-8")
        (target / "copy.py").write_text(_BAD_NUMBER, encoding="utf-8")

    # tests/ is skipped by every one of these scans; a violation here is a
    # fixture, not a finding.
    (root / "tests" / "test_fixture.py").write_text(_BAD_SQL, encoding="utf-8")
    return root


def test_percent_scan_sees_a_repo_under_a_claude_worktree_path(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path)
    mod = _load("test_sql_literal_percent")
    monkeypatch.setattr(mod, "ROOT", root)

    hits = {f"{f}" for f, _l, sql in mod._param_queries() if mod._BARE_PCT.search(sql)}
    assert hits == {"routes/billing.py"}, (
        "the literal-percent scan did not see a checkout whose own path contains "
        f"'.claude/worktrees/' — found {sorted(hits)}. The skip-list must be "
        "matched against the path RELATIVE to ROOT."
    )


def test_stripe_route_walk_sees_a_repo_under_a_claude_worktree_path(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path)
    mod = _load("test_stripe_route_auth")
    monkeypatch.setattr(mod, "ROOT", root)

    found = mod._stripe_routes()
    files = {str(f) for _p, f, _l, _s in found}
    assert [p for p, *_ in found] == ["/api/v1/stripe/webhook-planted"], (
        f"the Stripe route walk found {[p for p, *_ in found]} from a checkout "
        "under '.claude/worktrees/' — expected exactly the planted route"
    )
    assert files == {"routes/billing.py"}, (
        f"nested worktree copy leaked into the walk: {sorted(files)}"
    )


def test_honest_numbers_scan_sees_a_repo_under_a_claude_worktree_path(tmp_path, monkeypatch):
    import re

    root = _fake_checkout(tmp_path)
    mod = _load("test_honest_numbers")
    monkeypatch.setattr(mod, "ROOT", str(root))

    hits = mod._scan([re.compile(r"\$324B")])
    assert [f for f, _n, _l in hits] == ["routes/copy.py"], (
        "the honest-numbers drift-fence found "
        f"{[f for f, _n, _l in hits]} from a checkout under '.claude/worktrees/'. "
        "Zero here is the dangerous answer: every assertion in that file is a "
        "'nothing was found' shape, so a scan of nothing reports green."
    )


def test_the_floors_that_make_this_loud_are_still_present():
    """★ The fix above is one line in each file and nothing re-checks it at
    runtime. What keeps a future regression from being SILENT is the non-vacuity
    floor in each guard — so assert those still exist and still have a real
    threshold. This is the amplifier half; removing a floor is how a scan goes
    back to passing for the wrong reason."""
    import ast

    expected = {
        "test_sql_literal_percent": "test_the_scan_finds_queries",
        "test_stripe_route_auth": "test_the_walk_finds_the_routes",
        "test_honest_numbers": "test_the_scan_is_not_vacuous",
    }
    missing = []
    for module, floor in expected.items():
        tree = ast.parse((TESTS / f"{module}.py").read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == floor), None)
        if fn is None:
            missing.append(f"{module}.py::{floor} is gone")
            continue
        # The floor must compare against a positive literal, not `>= 0`.
        bounds = [c.comparators[0].value for c in ast.walk(fn)
                  if isinstance(c, ast.Compare)
                  and isinstance(c.comparators[0], ast.Constant)
                  and isinstance(c.comparators[0].value, int)]
        if not any(b > 0 for b in bounds):
            missing.append(f"{module}.py::{floor} no longer asserts a positive floor")
    assert not missing, (
        "a repo-wide scan lost its non-vacuity floor — it can now scan zero "
        "files and report green: " + "; ".join(missing)
    )
