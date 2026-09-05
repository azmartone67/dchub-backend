"""Phase RR (2026-05-15) — Brain consistency radar tests.

Locks in the structural contract of the radar detectors:
  • scan_all() returns a list[dict]
  • each finding has 'issue', 'url', 'count' keys
  • the 3 specific detector functions exist and return iterable
  • the scan's advertised wall-clock budget actually bounds wall clock

★ This file's header used to read "Doesn't make network calls — those would
  be flaky in CI." That was false from the day test_scan_summary_shape was
  added: it runs the real radar, all 140 detectors, HTTP self-calls and DB
  queries included, and takes 30-60s. Saying otherwise made the file look
  cheaper than it is and hid where its runtime goes.

★ WHY THIS FILE OWNS A DELIBERATE REPO WALK. tests/_scan_floors.py pins each
  test file's principal scan so a guard whose glob goes stale fails loudly
  instead of passing on nothing. Until 2026-09-05 this file's pinned `walk`
  was a SIDE EFFECT: the only scan it performed came from the two
  repo-walking detectors inside that scan_summary() call, so the floor
  measured whichever detectors happened to finish inside the 25s budget —
  a set that varies run to run. test_the_repo_walking_detectors_reach_the_repo
  below performs that walk deliberately, so the floor measures a scan this
  file OWNS and holds no matter what the scan budget abandons.
"""
import os
import re
import threading
import time


def test_radar_module_is_importable():
    """The radar module is loaded by main.py on startup. If it fails
    to import, the brain loses its consistency-finding stream silently.
    Catch import-time errors here."""
    from routes import brain_consistency_radar
    assert hasattr(brain_consistency_radar, "scan_all")
    assert hasattr(brain_consistency_radar, "scan_summary")
    assert hasattr(brain_consistency_radar, "brain_consistency_radar_bp")


def test_three_detector_functions_exist():
    from routes.brain_consistency_radar import (
        check_worker_version_drift,
        check_tier_consistency,
        check_cron_coverage,
    )
    assert callable(check_worker_version_drift)
    assert callable(check_tier_consistency)
    assert callable(check_cron_coverage)


def test_intentional_dispatch_allowlist_includes_safety_phases():
    """The allowlist suppresses cron-coverage findings for phases
    that are SUPPOSED to be manual. Guard the allowlist."""
    from routes.brain_consistency_radar import _INTENTIONAL_DISPATCH_ONLY
    for phase in ("all", "energy_verify", "marketing_rescue",
                   "hot_leads_preview", "hot_leads_send_top_5"):
        assert phase in _INTENTIONAL_DISPATCH_ONLY, \
            f"safety phase '{phase}' should be allowlisted to avoid false-positive findings"


def test_tool_api_mapping_only_lists_known_mcp_tools():
    """Every tool in _TOOL_API_MAPPING must exist in mcp_gatekeeper.TOOL_TIER
    or the tier_consistency check will silently skip the entry."""
    from routes.brain_consistency_radar import _TOOL_API_MAPPING
    try:
        from mcp_gatekeeper import TOOL_TIER
    except Exception:
        # mcp_gatekeeper may fail to import without env — accept that path
        # in the test environment but make a noise.
        import warnings
        warnings.warn("TOOL_TIER unavailable in test env; skipping")
        return
    for tool in _TOOL_API_MAPPING:
        assert tool in TOOL_TIER, \
            f"_TOOL_API_MAPPING lists '{tool}' but it's not in TOOL_TIER"


def _drain_brain_scan_threads(timeout=40.0):
    """Join the pool threads _run_detectors deliberately no longer waits for.

    PRODUCTION must not wait — not waiting is the whole fix. The TEST must,
    for two reasons, and both are about this process rather than about the
    radar:

      • tests/_scan_floors.py keys every scan it records to the file pytest
        is CURRENTLY running. A detector still walking the repo after its
        test returned would have that walk booked against whatever file runs
        next, inflating a floor that file never earned.
      • _run_one writes _DETECTOR_TIMINGS from the pool thread, which races
        any test that restores that dict.
    """
    deadline = time.time() + timeout
    for t in list(threading.enumerate()):
        if t.name.startswith("brain-scan") and t.is_alive():
            t.join(max(0.0, deadline - time.time()))


def test_scan_summary_shape():
    """scan_summary() returns a dict with the documented keys regardless
    of whether any findings fire.

    Runs the REAL radar — every detector, including the ones that make HTTP
    and DB calls. That is deliberate (it is the only end-to-end exercise the
    radar gets before production) and it is why this file takes ~40s.
    """
    from routes.brain_consistency_radar import scan_summary
    try:
        s = scan_summary()
    finally:
        _drain_brain_scan_threads()
    assert isinstance(s, dict)
    for key in ("ok", "count", "by_issue", "findings", "as_of"):
        assert key in s, f"scan_summary missing key '{key}'"
    assert isinstance(s["findings"], list)
    assert isinstance(s["by_issue"], dict)


def test_workflow_yaml_is_parseable():
    """The cron-coverage detector parses evolve-cron.yml. If that file
    becomes malformed, this test catches it before the radar fires
    in production.

    Phase FF+23-followup (2026-05-20): make pyyaml optional. The
    pre-merge-gauntlet CI installs ONLY pytest (not requirements.txt)
    so `import yaml` was raising ModuleNotFoundError on every PR. The
    gauntlet has been silently failing on every commit since at least
    FF+15. Skip cleanly when yaml isn't installed; the radar's own
    import test covers the prod path."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wf = os.path.join(here, ".github", "workflows", "evolve-cron.yml")
    if not os.path.exists(wf):
        return  # repo layout may differ in test env
    try:
        import yaml
    except ImportError:
        import pytest
        pytest.skip("pyyaml not installed in this env; production radar "
                     "exercises the parser at module-load time")
    data = yaml.safe_load(open(wf, "r"))
    # PyYAML parses unquoted 'on:' as the boolean True. Accept either.
    on = data.get("on") or data.get(True)
    assert isinstance(on, dict), "evolve-cron.yml `on:` section must be a dict"
    assert "workflow_dispatch" in on or "schedule" in on, \
        "workflow must declare at least one trigger"


# ════════════════════════════════════════════════════════════════════
# check_inline_script_truncated — the detector that would have caught
# the blank innovation dashboard (2026-09-04)
# ════════════════════════════════════════════════════════════════════

_GOOD_PAGE = """<!DOCTYPE html><html><body>
<script src="/x.js"></script>
<script>
(function(){
  // writing a closing tag here is only safe escaped: <\\/script>
  var re = /<script[^>]*>([\\s\\S]*?)<\\/script>/g;
  console.log(re);
})();
</script>
</body></html>"""

# The real defect: a comment quoting BOTH tags. The `<script src=…>` in it is
# why counting tags was vacuous — it balances the books. Only a parser-faithful
# walk sees that the close at that line cut the block short.
# (Mirrors the real defect: the comment carries BOTH tags, and the regex line
# — which would otherwise re-open script data and absorb the orphan — is gone.)
_TRUNCATED_PAGE = _GOOD_PAGE.replace(
    "  // writing a closing tag here is only safe escaped: <\\/script>\n"
    "  var re = /<script[^>]*>([\\s\\S]*?)<\\/script>/g;\n"
    "  console.log(re);\n",
    "  // e.g. \"add `<script src=x></script>` before </body>\"\n")


def _radar():
    from routes import brain_consistency_radar
    return brain_consistency_radar


def test_orphaned_close_is_found_only_in_the_truncated_page():
    """RED/GREEN on the signal itself."""
    r = _radar()
    assert r._orphaned_script_closes(_GOOD_PAGE) == []
    orphans = r._orphaned_script_closes(_TRUNCATED_PAGE)
    assert len(orphans) == 1
    opened, cut_at, orphan_at = orphans[0]
    assert (opened, cut_at) == (3, 5), (opened, cut_at)
    assert orphan_at == 7, orphan_at


def test_counting_script_tags_would_have_missed_it():
    """★ WHY THE WALK. The first cut of this detector compared open-tag and
    close-tag COUNTS. The comment carries one of each, so the counts balance
    and the detector was vacuous on the very bug it shipped for."""
    assert _TRUNCATED_PAGE.count("<script") == _TRUNCATED_PAGE.count("</script")


def test_detector_fires_on_a_loaded_page_constant(monkeypatch):
    """End to end through the registered detector, on a module the scan
    actually walks — not on the helper."""
    r = _radar()
    monkeypatch.setattr(r, "_served_page_constants",
                        lambda: iter([("routes.fake_page", "_PAGE_HTML",
                                       _TRUNCATED_PAGE)]))
    out = r.check_inline_script_truncated()
    assert len(out) == 1
    assert out[0]["issue"] == "inline_script_truncated"
    assert "line 5" in out[0]["detail"] and "routes.fake_page" in out[0]["detail"]

    monkeypatch.setattr(r, "_served_page_constants",
                        lambda: iter([("routes.fake_page", "_PAGE_HTML",
                                       _GOOD_PAGE)]))
    assert r.check_inline_script_truncated() == []


def test_the_pages_this_app_serves_are_clean():
    """The live assertion: no loaded page constant serves a truncated script."""
    r = _radar()
    findings = r.check_inline_script_truncated()
    assert findings == [], [f["detail"] for f in findings]


# ════════════════════════════════════════════════════════════════════
# The scan budget, and the repo walk this file owns
# ════════════════════════════════════════════════════════════════════

def _blocking_detectors(n, release, started, hard_stop=6.0):
    """n distinct detector callables that each block until `release` is set.

    `started` is a list the detectors append to as they BEGIN, which is how
    the test tells "the scan stopped waiting" from "the scan stopped working".
    """
    lock = threading.Lock()

    def _make(i):
        def _d():
            with lock:
                started.append(i)
            release.wait(hard_stop)
            return []
        _d.__name__ = f"check_fake_blocking_{i}"
        return _d
    return [_make(i) for i in range(n)]


def test_the_scan_budget_bounds_wall_time_and_not_only_the_result():
    """★ RED/GREEN on the defect. `with ThreadPoolExecutor(...) as ex` exits
    through shutdown(wait=True, cancel_futures=False): every detector still
    QUEUED at the deadline ran to completion anyway, and only its RESULT was
    thrown away. The budget bounded what the scan READ, never what it DID.

    40 detectors that each block, 8 workers, a 1s budget. Bounded: ~1s.
    Unbounded (shutdown joining the queue): 5 batches x 6s = ~30s — which is
    what the caller, a gunicorn worker holding a request open, actually paid.
    Measured against the real 25s budget before this fix: 36.0, 40.1, 44.1,
    54.5, 59.8, 62.8s.
    """
    r = _radar()
    release = threading.Event()
    started: list = []
    dets = _blocking_detectors(40, release, started)

    timings = dict(r._DETECTOR_TIMINGS)
    sweep = dict(r._LAST_SWEEP)
    try:
        t0 = time.time()
        out = r._run_detectors(dets, budget_s=1.0)
        elapsed = time.time() - t0
    finally:
        release.set()
        _drain_brain_scan_threads()
        r._DETECTOR_TIMINGS.clear(); r._DETECTOR_TIMINGS.update(timings)
        r._LAST_SWEEP.clear(); r._LAST_SWEEP.update(sweep)

    assert elapsed < 4.0, (
        f"_run_detectors returned after {elapsed:.1f}s on a 1.0s budget. "
        f"The deadline is discarding results while the work runs on.")

    # ★ Second half of the fix, and it needs its own assertion: wait=False
    #   alone would return fast while all 40 detectors still ran to completion
    #   in the background, burning the pool and the DB pool on a scan nobody
    #   will read. cancel_futures=True drops the QUEUE, so only the ≤8 already
    #   occupying a worker ever start.
    assert len(started) <= 8, (
        f"{len(started)} of 40 detectors started on a 1.0s budget with 8 "
        f"workers. The queue is not being cancelled — the scan returned "
        f"early but the work carried on.")

    partial = [f for f in out if f["issue"] == "consistency_radar_scan_partial"]
    assert len(partial) == 1, out
    assert partial[0]["count"] == 40, partial[0]
    assert "1s budget" in partial[0]["detail"], partial[0]["detail"]


def test_scan_all_runs_its_detectors_through_the_budgeted_path(monkeypatch):
    """The guard above tests _run_detectors. Pin that scan_all still GOES
    through it — otherwise a refactor could leave the budget test green while
    the real scan runs unbudgeted somewhere else."""
    r = _radar()
    seen = {}

    def _fake(detectors, budget_s=None):
        seen["n"] = len(detectors)
        seen["budget_s"] = budget_s
        return []

    monkeypatch.setattr(r, "_run_detectors", _fake)
    assert r.scan_all() == []
    assert seen.get("n", 0) > 100, (
        f"scan_all collected {seen.get('n')} detectors; it registers ~140. "
        f"A collapse here silently shrinks the radar.")
    assert seen["budget_s"] is None, "scan_all must run on the default budget"


def test_the_repo_walking_detectors_reach_the_repo():
    """★ THE FAIL-OPEN THIS CLOSES. check_orphaned_scheduler_functions and
    check_cron_endpoint_unscheduled both derive the tree they scan from THIS
    MODULE'S OWN PATH — `dirname(dirname(abspath(__file__)))`. Move the module
    one directory deeper (routes/ -> routes/brain/, the exact refactor that
    silently emptied test_brain_loggers_defined.py's glob in 2026-08) and that
    root becomes routes/: no main.py, no dchub-scheduler.py. Both detectors
    then return [] forever and report GREEN, because "walked the wrong tree
    and found nothing" is byte-identical to "walked everything, all clean".

    Deriving `here` the way the module does is the point — a root computed
    from this test file's location would keep passing through that move.

    This is also the scan tests/_scan_floors.py measures for this file. It
    walks the same tree with the same prune set as the detectors, so the
    pinned floor is a property of the repo, not of which detectors happened
    to survive the scan budget.
    """
    r = _radar()
    here = os.path.dirname(os.path.dirname(os.path.abspath(r.__file__)))

    seen = set()
    for root, dirs, files in os.walk(here):
        dirs[:] = [d for d in dirs if d not in r._SCHEDULER_SKIP_DIRS]
        rel = os.path.relpath(root, here)
        for fname in files:
            if fname.endswith(".py"):
                seen.add(os.path.normpath(os.path.join(rel, fname)))

    for landmark in ("main.py",
                     "dchub-scheduler.py",
                     os.path.join("routes", "brain_consistency_radar.py")):
        assert landmark in seen, (
            f"the repo-walking detectors derive their scan root from "
            f"{r.__name__}.__file__, and that root ({here}) does not contain "
            f"{landmark}. Both of them now scan a tree with none of the code "
            f"they exist to read, and both report clean.")
