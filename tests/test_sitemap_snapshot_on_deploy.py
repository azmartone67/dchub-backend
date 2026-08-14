#!/usr/bin/env python3
"""tests/test_sitemap_snapshot_on_deploy.py — a deployed sitemap change must
reach the served artifact.

NO NETWORK.

2026-08-14. PR #2655 changed which facility URLs the sitemap emits. It merged,
CI went green, Railway deployed the new code — and /sitemap-facilities-1.xml
served the OLD 10,000-URL shard for 40 minutes afterwards.

Nothing was broken. /sitemap*.xml reads from the `sitemap_snapshot` table, and
the snapshot rebuilt only on a 4-hourly cron. Every check a person would run
said SHIPPED: PR merged, checks green, origin 200. The only tell was
`x-sitemap-source: snapshot` on the response.

★ THE GAP IS BETWEEN "THE CODE IS LIVE" AND "WHAT THE CODE PRODUCES IS LIVE."
A deploy is not a rebuild. So a push to main that touches the sitemap builder
now rebuilds the snapshot, after the same Railway settle-wait post-deploy-smoke
already uses. The cron stays for data drift — facilities appear without deploys.

★ AND A 200 IS NOT A GOOD SNAPSHOT. _rebuild_sitemap_snapshot DELETEs every row
and re-INSERTs in one transaction, so a build that succeeds and produces almost
nothing REPLACES the good snapshot and serves it. The old step asserted only
`code == 200`, which a 12-URL sitemap passes. main.py carries an internal
collapse floor for the facilities section; this is the floor for the PUBLISHED
artifact, and it is the one a human sees fail.

Run standalone:   python3 tests/test_sitemap_snapshot_on_deploy.py
Run under pytest: pytest tests/test_sitemap_snapshot_on_deploy.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "sitemap-snapshot.yml")


def _wf():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _on():
    d = _wf()
    return d[True] if True in d else d["on"]


def _steps():
    return _wf()["jobs"]["rebuild"]["steps"]


def _rebuild_run():
    """The rebuild step's shell, minus comments — the comments narrate the
    failure on purpose, and several guards in this repo have passed by matching
    prose that described behaviour the code no longer had."""
    for s in _steps():
        if "rebuild-snapshot" in (s.get("run") or ""):
            return "\n".join(l for l in s["run"].splitlines()
                             if not l.lstrip().startswith("#"))
    raise AssertionError("no step calls the rebuild endpoint")


def test_a_builder_change_rebuilds_the_snapshot():
    """★ The motivating failure. Without a push trigger the served sitemap can
    lag a deploy by up to four hours while every signal reads green."""
    on = _on()
    assert "push" in on, (
        "a push to main must rebuild the snapshot — otherwise a merged, "
        "deployed sitemap change does not reach the artifact until the cron"
    )
    assert on["push"].get("branches") == ["main"]
    paths = on["push"].get("paths") or []
    assert "main.py" in paths, (
        "main.py builds the sitemap XML; a change there is exactly the case "
        "that needs a rebuild"
    )


def test_the_cron_survives_as_the_drift_net():
    """Facilities appear without a deploy. Replacing the cron with the push
    trigger would leave the sitemap frozen between commits."""
    on = _on()
    assert "schedule" in on, "the periodic rebuild must remain"
    assert any("*/4" in c.get("cron", "") for c in on["schedule"]), (
        "the 4-hourly data-drift rebuild is gone"
    )


def test_it_waits_for_railway_before_rebuilding():
    """★ Rebuilding before the replicas swap regenerates the snapshot from the
    OLD code and reports success — the very failure this job exists to prevent,
    arriving through the fix for it."""
    steps = _steps()
    waits = [s for s in steps
             if "sleep" in (s.get("run") or "") and "push" in str(s.get("if", ""))]
    assert waits, (
        "a push-triggered rebuild must wait for the Railway deploy to settle"
    )
    idx_wait = steps.index(waits[0])
    idx_rebuild = next(i for i, s in enumerate(steps)
                       if "rebuild-snapshot" in (s.get("run") or ""))
    assert idx_wait < idx_rebuild, "the wait must come BEFORE the rebuild"
    secs = int(re.search(r"sleep\s+(\d+)", waits[0]["run"]).group(1))
    assert secs >= 90, f"sleep {secs}s is under the 90s post-deploy-smoke uses"


def test_a_collapsed_rebuild_fails_the_job():
    """★ The snapshot is replaced in one transaction, so a tiny build is
    published before anyone sees it. `code == 200` does not catch that."""
    run = _rebuild_run()
    assert "MIN_URLS" in run, "no floor on the rebuilt URL count"
    assert "total_urls" in run, (
        "the floor must be checked against the reported URL count, not just "
        "the HTTP status"
    )
    env = next((s.get("env") or {}) for s in _steps()
               if "rebuild-snapshot" in (s.get("run") or ""))
    assert "MIN_URLS" in env, "MIN_URLS is not set in the step env"
    floor = int(env["MIN_URLS"])
    # The capacity gate took the published sitemap to 9,214 URLs on 2026-08-14.
    assert 500 < floor < 9214, f"floor {floor} is not between zero and the live count"


def test_ok_false_is_not_treated_as_success():
    run = _rebuild_run()
    assert re.search(r"if not d\.get\('ok'\)", run), (
        "the endpoint returns ok:false on a build error; that must fail the job"
    )


def test_the_call_is_not_swallowed():
    run = _rebuild_run()
    assert "|| true" not in run and "|| echo" not in run, (
        "the rebuild must be allowed to fail the job"
    )
    assert 'test "$code" = "200"' in run, "the HTTP status check is gone"


def test_it_verifies_the_published_artifact_not_just_the_endpoint():
    """The endpoint reports on its own write. Reading dchub.cloud afterwards is
    what proves the edge serves it — the distinction this whole PR is about."""
    runs = "\n".join((s.get("run") or "") for s in _steps())
    assert "https://dchub.cloud/sitemap.xml" in runs, (
        "the job must read the published sitemap back"
    )
    assert re.search(r"date \+%s", runs), (
        "the read-back must be cache-busted or it can pass on a cached copy"
    )


def test_has_a_kill_switch():
    run = _rebuild_run()
    assert re.search(r'if \[ "\$\{GATE_DISABLE:-0\}" = "1" \]', run), (
        "needs a kill switch, like the other scheduled jobs here"
    )


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
