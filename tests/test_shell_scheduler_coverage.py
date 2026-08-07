"""The registered≠scheduled class, closed for good (audit 2026-08-07, SH52-001).

Four separate times a module shipped a declared dead-man beat with nothing
driving it: the #30-33-era shells, agent-pay #34 (whose only 'scheduler' was a
substring inside DISABLED_JOBS in a file nothing executes), loop-control #48
(red 132h from its ship day), and the #50/#51 boards (tick-on-demand only).
Each time the fix was one _DISPATCH line and a per-shell test. This test is
the CLASS fix: it walks every routes/ module that declares `_beat_ledger` and
demands a live scheduler for its master-tick route. A new shell cannot merge
unscheduled.

The scan itself lives in routes/audit_closure_master_shell.py (shell #52 runs
it live every tick as lane J) — imported here, never re-declared, so the CI
gate and the live lane can never drift.

MUST-FAIL proof is built in: test_scanner_catches_an_unscheduled_beat runs the
scanner against a synthetic tree containing an unscheduled beat and asserts it
reports the gap. If the scanner is ever gutted, that test — not production —
goes red (the 'empty parse PASSES ALL' / silent-green class).

CI-SAFETY: source scanning only; no DB, no network.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def scanner():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ["AUDIT_CLOSURE_SHELL_PROBE"] = "0"   # no network in CI
    from routes import audit_closure_master_shell as m
    return m


def test_every_declared_beat_has_a_scheduler(scanner):
    gaps = scanner.scan_beat_scheduler_gaps()
    assert gaps == [], (
        "modules declaring _beat_ledger with no scheduler:\n  "
        + "\n  ".join(gaps)
        + "\nAdd a cron_heartbeat._DISPATCH entry (or a cron'd workflow) for "
          "the module's master-tick route — registration is not scheduling.")


def test_exemptions_carry_written_reasons(scanner):
    """An allowlist entry without a reason is how exemptions rot into holes
    (the discovery-family lesson: 5 exemptions, each with a written why)."""
    for rel, reason in scanner.BEAT_SCHEDULER_EXEMPT.items():
        assert os.path.exists(os.path.join(ROOT, rel)), (
            "%s is exempted but no longer exists — remove the entry" % rel)
        assert isinstance(reason, str) and len(reason) >= 40, (
            "%s: exemption reason too thin to audit" % rel)


def test_scanner_catches_an_unscheduled_beat(scanner, tmp_path):
    """MUST-FAIL: a scanner that cannot flag a real gap is a silent green.

    Build a minimal tree with one module declaring _beat_ledger + a tick
    route, and a cron_heartbeat.py that does NOT drive it: the scan must
    report exactly that gap. Then add the dispatch line: the gap must clear.
    """
    (tmp_path / "routes").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    shell = tmp_path / "routes" / "ghost_master_shell.py"
    shell.write_text(
        'def _beat_ledger(note):\n    pass\n\n'
        'class _BP:\n'
        '    def route(self, *a, **k):\n'
        '        def deco(f):\n            return f\n        return deco\n'
        'bp = _BP()\n'
        '@bp.route("/api/v1/admin/ghost/master-tick", methods=["POST"])\n'
        'def tick():\n    pass\n')
    hb = tmp_path / "routes" / "cron_heartbeat.py"
    hb.write_text("_DISPATCH = []\n")

    gaps = scanner.scan_beat_scheduler_gaps(root=str(tmp_path))
    assert len(gaps) == 1 and "ghost_master_shell" in gaps[0], gaps

    hb.write_text('_DISPATCH = [("ghost_daily", '
                  'BASE + "/api/v1/admin/ghost/master-tick", "POST", None)]\n')
    assert scanner.scan_beat_scheduler_gaps(root=str(tmp_path)) == []


def test_scanner_rejects_a_beat_with_no_tick_route(scanner, tmp_path):
    """A module that beats but exposes nothing schedulable needs a WRITTEN
    exemption, not silence."""
    (tmp_path / "routes").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "routes" / "cron_heartbeat.py").write_text("_DISPATCH = []\n")
    (tmp_path / "routes" / "mute_crawler.py").write_text(
        "def _beat_ledger(note):\n    pass\n")
    gaps = scanner.scan_beat_scheduler_gaps(root=str(tmp_path))
    assert len(gaps) == 1 and "no master-tick route" in gaps[0], gaps


def test_a_commented_out_dispatch_entry_is_not_a_scheduler(scanner, tmp_path):
    """Review #11/#19/#32/#42: substring matching over raw file text graded a
    COMMENTED-OUT _DISPATCH entry — the standard way a job gets disabled —
    as 'scheduled'. The scanner must strip comments before matching."""
    (tmp_path / "routes").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "routes" / "ghost_master_shell.py").write_text(
        'def _beat_ledger(note):\n    pass\n\n'
        'class _BP:\n'
        '    def route(self, *a, **k):\n'
        '        def deco(f):\n            return f\n        return deco\n'
        'bp = _BP()\n'
        '@bp.route("/api/v1/admin/ghost/master-tick", methods=["POST"])\n'
        'def tick():\n    pass\n')
    (tmp_path / "routes" / "cron_heartbeat.py").write_text(
        '_DISPATCH = [\n'
        '    # ("ghost_daily",\n'
        '    #  BASE + "/api/v1/admin/ghost/master-tick",\n'
        '    #  "POST", None),\n'
        ']\n')
    gaps = scanner.scan_beat_scheduler_gaps(root=str(tmp_path))
    assert len(gaps) == 1 and "ghost_master_shell" in gaps[0], (
        "a commented-out dispatch entry was accepted as a scheduler", gaps)


def test_dispatch_only_workflow_is_not_a_scheduler(scanner, tmp_path):
    """#2027's lesson verbatim: a workflow_dispatch-only workflow schedules
    nothing. The scanner must not accept it as evidence."""
    (tmp_path / "routes").mkdir()
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True)
    (tmp_path / "routes" / "cron_heartbeat.py").write_text("_DISPATCH = []\n")
    (tmp_path / "routes" / "ghost_master_shell.py").write_text(
        'def _beat_ledger(note):\n    pass\n\n'
        'class _BP:\n'
        '    def route(self, *a, **k):\n'
        '        def deco(f):\n            return f\n        return deco\n'
        'bp = _BP()\n'
        '@bp.route("/api/v1/admin/ghost/master-tick")\n'
        'def tick():\n    pass\n')
    (wfdir / "ghost-tick.yml").write_text(
        "on:\n  workflow_dispatch: {}\njobs:\n  t:\n    steps:\n"
        "      - run: curl /api/v1/admin/ghost/master-tick\n")
    gaps = scanner.scan_beat_scheduler_gaps(root=str(tmp_path))
    assert len(gaps) == 1, gaps
