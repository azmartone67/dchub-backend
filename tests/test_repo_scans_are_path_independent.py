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


# ── the same class, found again 2026-09-21 ───────────────────────────────
#
# Two more guards were still matching a skip-list against the ABSOLUTE path,
# and both failed only from a checkout under .claude/worktrees/:
#
#   tests/test_llm_spend_coverage.py::test_scan_excludes_stale_worktree_checkouts
#       asserted `".claude" not in str(f)` over every scanned path, so every
#       file of a checkout that lives there was an "offender".
#   tests/test_route_auth_shell_transitive_reach.py — its reader skipped
#       `"/.claude/" in str(f)`, so it read NOTHING and its source blob came
#       back empty (0 bytes against a 5 MB floor).
#
# Both now judge the path relative to the root, by segment, through a helper
# the tests below can point at a miniature checkout.

_RAW_ANTHROPIC = textwrap.dedent("""\
    import requests
    from util.anthropic_endpoint import anthropic_messages_url

    def ask(body):
        return requests.post(anthropic_messages_url(), json=body)
""")

_SINK_CALLER = textwrap.dedent("""\
    def publish(urls):
        return submit_to_indexnow(urls)
""")

#: A name that exists ONLY in the copies that must stay invisible. If a nested
#: worktree or .git copy leaks into a reader, this string proves it.
_ONLY_IN_STALE_COPIES = "submit_from_a_stale_checkout"


def _mini_checkout(tmp_path, name):
    """A miniature checkout exactly where Claude Code puts worktrees, with a
    nested .claude/worktrees/ copy inside it that must stay invisible."""
    root = (tmp_path / ".claude" / "worktrees" / name).resolve()
    (root / "routes").mkdir(parents=True)
    nested = root / ".claude" / "worktrees" / "inner" / "routes"
    nested.mkdir(parents=True)
    return root, nested


def test_llm_spend_scan_sees_a_repo_under_a_claude_worktree_path(tmp_path):
    """The scanner runs from a path built to trip it, and the guard's own
    stale-dir check must not fire on the checkout's ANCESTORS — while still
    naming a nested copy inside it."""
    root, nested = _mini_checkout(tmp_path, "session-llm")
    (root / "routes" / "brain_llm_spend.py").write_text(
        "# the ledger itself; the scan skips it by name\n", encoding="utf-8")
    (root / "routes" / "media_thread_generator.py").write_text(
        _RAW_ANTHROPIC, encoding="utf-8")
    (root / "main.py").write_text(_RAW_ANTHROPIC, encoding="utf-8")
    (nested / "media_thread_generator.py").write_text(_RAW_ANTHROPIC, encoding="utf-8")

    mod = _load("test_llm_spend_coverage")
    ns = mod._load_scanner(root / "routes" / "brain_llm_spend.py")

    files = ns["_scanned_files"]()
    seen = {str(pathlib.Path(f).relative_to(root)) for f in files}
    assert seen == {"main.py", "routes/brain_llm_spend.py",
                    "routes/media_thread_generator.py"}, (
        f"the scan saw {sorted(seen)} from a checkout under "
        "'.claude/worktrees/' — it must see the checkout's own files and not "
        "the nested copy")

    assert not mod._stale_dirs_reached(files, root), (
        "the stale-checkout guard flagged a PRISTINE checkout's own files "
        "because its path runs through '.claude/worktrees/'. The skip-list "
        "must be matched against the path RELATIVE to the repo root.")

    leaked = nested / "media_thread_generator.py"
    assert mod._stale_dirs_reached([leaked], root), (
        "a nested .claude/worktrees/ copy INSIDE the checkout is no longer "
        "recognised as stale — the fix must not simply stop skipping")

    sites = {m for m, _ln in ns["uninstrumented_call_sites"]()}
    assert sites == {"main", "media_thread_generator"}, (
        f"call-site scan returned {sorted(sites)} — it must find the raw "
        "calls in the checkout and count the nested copy zero times")


def test_sink_name_reader_sees_a_repo_under_a_claude_worktree_path(tmp_path):
    """The blob reader behind the outbound-sink guard. Reading nothing left it
    asserting over an empty string; reading too much would let a dead sink name
    be vouched for by a stale copy of the code that deleted it."""
    root, nested = _mini_checkout(tmp_path, "session-sink")
    (root / ".git").mkdir()
    (root / "routes" / "seo.py").write_text(_SINK_CALLER, encoding="utf-8")
    stale = f"def publish(urls):\n    return {_ONLY_IN_STALE_COPIES}(urls)\n"
    (nested / "seo.py").write_text(stale, encoding="utf-8")
    (root / ".git" / "stale_hook.py").write_text(stale, encoding="utf-8")
    # The shell DEFINES the sink set, so it is excluded by name: a dead name
    # must not be able to vouch for itself out of its own registry.
    (root / "routes" / "route_auth_master_shell.py").write_text(
        stale, encoding="utf-8")

    mod = _load("test_route_auth_shell_transitive_reach")
    got = mod._repo_sources(root)
    seen = {str(rel) for rel, _src in got}
    assert seen == {"routes/seo.py"}, (
        f"the reader returned {sorted(seen)} from a checkout under "
        "'.claude/worktrees/'. Empty means it skipped the whole repo on its "
        "own ancestors; anything extra means a stale copy leaked in.")
    blob = "\n".join(src for _rel, src in got)
    assert "submit_to_indexnow(" in blob, "the real source was not read"
    assert _ONLY_IN_STALE_COPIES not in blob, (
        "a nested worktree / .git / self copy leaked into the source blob")


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
        # Added 2026-09-21 with the two guards below. The reach guard's floor
        # lives inside the test that reads the blob, which is why the entry
        # names that test.
        "test_llm_spend_coverage": "test_scan_excludes_stale_worktree_checkouts",
        "test_route_auth_shell_transitive_reach":
            "test_every_outbound_sink_name_exists_in_the_repo",
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
