"""Guard: the four defects that kept the physical-infra layers static.

FENCES the 2026-08-07 unfreeze. Each test drives the real shipped artefact —
the loader registry, the scheduler's window function extracted with ast, the
liveness shell's scope helper, and the workflow YAML itself.

──────────────────────────────────────────────────────────────────────────
1. GAS CAP BELOW THE SOURCE (test_gas_pipeline_cap_exceeds_the_live_source).
   tools/infra_fetch.py ends fetch_gas_pipelines with `return rows[:cap]`, and
   gas-pipelines carried default_cap 30000 while the EIA service publishes
   32,892 features (returnCountOnly, measured 2026-08-07). Result: the table
   sat at EXACTLY 30,000 rows with a fresh created_at and ZERO net growth for
   54 days — green on every freshness check, 2,892 segments dropped silently.
   Every sibling feed carries headroom over its source; this one alone did not.

2. day_of_week IGNORED (test_is_job_in_window_honours_day_of_week).
   dchub-scheduler.py's is_job_in_window matched only hours+minute. Thirteen
   job specs declare a day_of_week (2 active, 11 disabled); each fired EVERY
   DAY, up to 7x its documented rate. subsea_sync was moved out of
   DISABLED_JOBS on 2026-07-29 expressly to run weekly.

3. ★★★ STAMPABILITY IS THE DECORATOR, NOT THE PATH
   (test_only_blueprint_registered_jobs_are_judged). cron_last_run is written
   by @jobs_bp.after_request. The liveness shell's never_ran lane originally
   scoped on the URL path "/api/jobs/", which is NOT the same set — and so
   published SIX FALSE ACCUSATIONS: subsea_sync, fiber_sync, permit_scraper,
   sec_parser, smoke_test and daily_image_render are all registered with
   @app.route (or another blueprint) and can NEVER be stamped. Proven the hard
   way: /api/jobs/subsea-sync was triggered manually on 2026-08-07, ran clean
   (691 -> 699 cables, 1,908 -> 1,927 landing points, first write since
   2026-03-27) and STILL had no cron_last_run row.

4. SUBSEA HAD NO DRIVER (test_subsea_sync_step_is_authenticated_and_loud).
   Nothing in this repo launches dchub-scheduler.py — Procfile and
   railway.json both run start_web.sh. The workflow is the driver that works.
   The step must send the INTERNAL key (fiber_integration uses
   @require_internal_key, not the admin gate), must accept 202 (the call is
   worker-delegated), and must FAIL on anything else rather than echo a
   reassuring line — the #2318 lesson that cost 147 hours of green.

No DB and no network. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_unfreeze_the_organism.py -v
"""
from __future__ import annotations

import ast
import os
import re
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Measured live 2026-08-07 via returnCountOnly on the EIA
# Natural_Gas_Interstate_and_Intrastate_Pipelines_1 FeatureServer.
LIVE_EIA_GAS_FEATURES = 32892

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "data-sync.yml")
SCHEDULER = os.path.join(ROOT, "dchub-scheduler.py")

# Registered with @app.route / another blueprint, so structurally un-stampable.
UNSTAMPABLE_JOBS = ("subsea-sync", "fiber-sync", "permit-scraper",
                    "sec-parser", "smoke-test", "render-daily-fanout")


# ── 1. the gas cap ──────────────────────────────────────────────────────────
def _feeds():
    """Pull the loader registry out of tools/infra_fetch.py with ast — the
    module opens network handles, so it must not be imported."""
    src = open(os.path.join(ROOT, "tools", "infra_fetch.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if getattr(k, "value", None) == "gas-pipelines":
                    out = {}
                    for kk, vv in zip(v.keys, v.values):
                        if isinstance(vv, ast.Constant):
                            out[getattr(kk, "value", "")] = vv.value
                    if "default_cap" in out:
                        return out
    return {}


def test_gas_pipeline_cap_exceeds_the_live_source():
    """A cap below the source truncates forever and reads as healthy."""
    cap = _feeds().get("default_cap")
    assert cap is not None, "gas-pipelines default_cap not found"
    assert cap > LIVE_EIA_GAS_FEATURES, (
        f"cap {cap} <= live EIA feature count {LIVE_EIA_GAS_FEATURES}: the "
        f"loader would silently drop {LIVE_EIA_GAS_FEATURES - cap} segments "
        f"and the table would net zero growth forever")


def test_gas_workflow_cap_exceeds_the_live_source():
    """★ THE DEFAULT IS NOT THE LIVE PATH. gas-pipeline-ingest.yml passes CAP
    as an explicit argv (`infra_fetch.py gas-pipelines "${CAP}"`), so its
    hardcoded value WINS over default_cap. Raising the default alone was a
    no-op for every scheduled run — caught only by reading the workflow after
    the first fix had already merged."""
    src = open(os.path.join(ROOT, ".github", "workflows",
                            "gas-pipeline-ingest.yml"), encoding="utf-8").read()
    m = re.search(r"CAP:\s*\$\{\{\s*github\.event\.inputs\.cap\s*\|\|\s*'(\d+)'",
                  src)
    assert m, "could not find the CAP default in gas-pipeline-ingest.yml"
    cap = int(m.group(1))
    assert cap > LIVE_EIA_GAS_FEATURES, (
        f"workflow CAP {cap} <= live EIA count {LIVE_EIA_GAS_FEATURES}; the "
        f"scheduled run would still truncate regardless of default_cap")

    # ★ AND the workflow_dispatch input default, which is a SEPARATE cap. On a
    # manual dispatch github.event.inputs.cap is POPULATED from it, so the
    # `|| ...` fallback above never fires. Two runs were dispatched after the
    # fallback was fixed and both still fetched exactly 30,000 because this
    # one was missed. Asserting only the fallback is how that slipped through.
    d = yaml.safe_load(src)
    on = d.get(True, d.get("on"))          # PyYAML parses bare `on:` as True
    inp = on["workflow_dispatch"]["inputs"]["cap"].get("default")
    assert int(inp) > LIVE_EIA_GAS_FEATURES, (
        f"workflow_dispatch input default {inp} <= live EIA count "
        f"{LIVE_EIA_GAS_FEATURES}; every manual run would still truncate")


def test_gas_workflow_actually_passes_cap_to_the_loader():
    """Fences WHY the test above matters: if the workflow ever stops passing
    CAP, the default becomes the live value and the assertion moves."""
    src = open(os.path.join(ROOT, ".github", "workflows",
                            "gas-pipeline-ingest.yml"), encoding="utf-8").read()
    assert 'infra_fetch.py gas-pipelines "${CAP}"' in src


def test_gas_fetch_still_truncates_at_the_cap():
    """The cap is load-bearing, not decorative — proving WHY it must exceed
    the source. If this ever stops truncating, the test above is moot."""
    src = open(os.path.join(ROOT, "tools", "infra_fetch.py"),
               encoding="utf-8").read()
    body = src.split("def fetch_gas_pipelines", 1)[1].split("\ndef ", 1)[0]
    assert "rows[:cap]" in body, (
        "fetch_gas_pipelines no longer truncates at cap — re-derive the "
        "relationship between default_cap and the upstream count")


# ── 2. day_of_week ──────────────────────────────────────────────────────────
def _is_job_in_window():
    """Extract the real function from dchub-scheduler.py and bind it against
    stubs. The module is a CLI with import-time env reads; never import it."""
    src = open(SCHEDULER, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "is_job_in_window")
    import datetime as _dt
    ns = {"datetime": _dt.datetime, "timezone": _dt.timezone}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SCHEDULER, "exec"), ns)
    return ns["is_job_in_window"]


def _wed_0330():
    import datetime as _dt
    # 2026-08-05 is a Wednesday (weekday() == 2).
    return _dt.datetime(2026, 8, 5, 3, 30, tzinfo=_dt.timezone.utc)


def _thu_0330():
    import datetime as _dt
    return _dt.datetime(2026, 8, 6, 3, 30, tzinfo=_dt.timezone.utc)


def test_is_job_in_window_honours_day_of_week():
    """subsea_sync's real spec: Wednesday 03:30. It must NOT fire Thursday."""
    f = _is_job_in_window()
    job = {"hours": [3], "minute": 30, "day_of_week": 2}
    assert f(job, _wed_0330()) is True, "must fire on its declared weekday"
    assert f(job, _thu_0330()) is False, "must NOT fire on other days"


def test_jobs_without_day_of_week_still_fire_daily():
    """MUST-FAIL CONTROL. Most jobs declare no weekday and must be unaffected —
    a day filter that suppressed them would break the whole fleet."""
    f = _is_job_in_window()
    job = {"hours": [3], "minute": 30}
    assert f(job, _wed_0330()) is True
    assert f(job, _thu_0330()) is True


def test_day_of_week_uses_monday_zero_like_the_inline_comments():
    """Every declaration documents itself ('day_of_week': 2 == '# Wednesday'),
    which is datetime.weekday(), not isoweekday(). An off-by-one here silently
    runs every weekly job on the wrong day."""
    f = _is_job_in_window()
    import datetime as _dt
    sunday = _dt.datetime(2026, 8, 9, 2, 0, tzinfo=_dt.timezone.utc)
    assert sunday.weekday() == 6
    # permit_scraper's real spec: 'day_of_week': 6  # Sunday only
    assert f({"hours": [2], "minute": 0, "day_of_week": 6}, sunday) is True


def test_declared_weekly_jobs_are_actually_weekly_now():
    """Drives the REAL specs out of the scheduler, so a new weekly job added
    without weekday support is caught."""
    f = _is_job_in_window()
    src = open(SCHEDULER, encoding="utf-8").read()
    tree = ast.parse(src)
    specs = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", "") == "JOBS":
            for k, v in zip(node.value.keys, node.value.values):
                d = {getattr(kk, "value", ""): vv
                     for kk, vv in zip(v.keys, v.values)}
                if "day_of_week" in d:
                    specs.append((k.value, {
                        "hours": [e.value for e in d["hours"].elts],
                        "minute": d["minute"].value,
                        "day_of_week": d["day_of_week"].value}))
    assert specs, "no weekly jobs found — did JOBS move?"
    import datetime as _dt
    for name, spec in specs:
        fires = 0
        for day in range(7):          # a full week at the declared time
            now = _dt.datetime(2026, 8, 3 + day, spec["hours"][0],
                               spec["minute"], tzinfo=_dt.timezone.utc)
            if f(spec, now):
                fires += 1
        assert fires == 1, f"{name} fires {fires}x/week, expected exactly 1"


# ── 3. stampability is the decorator, not the path ──────────────────────────
def test_blueprint_job_routes_reads_the_real_module():
    from routes.data_liveness_master_shell import _blueprint_job_routes
    names, err = _blueprint_job_routes()
    assert err is None, err
    assert "discovery" in names and "news-refresh" in names
    for j in UNSTAMPABLE_JOBS:
        assert j not in names, (
            f"{j} is NOT registered on jobs_bp — including it would restore "
            f"the false-accusation bug")


def test_only_blueprint_registered_jobs_are_judged(monkeypatch):
    """★ REGRESSION. A job registered with @app.route under /api/jobs/ can
    never be stamped, so its absence from cron_last_run is not evidence."""
    import routes.data_liveness_master_shell as m

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): pass
        def fetchall(self): return [("discovery",)]
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(m, "_declared_jobs", lambda: ({
        "discovery": "/api/jobs/discovery",         # jobs_bp, HAS run
        "subsea_sync": "/api/jobs/subsea-sync",     # @app.route — unstampable
        "fiber_sync": "/api/jobs/fiber-sync",       # @app.route — unstampable
    }, None))
    monkeypatch.setattr(m, "_conn", lambda: _Conn())
    c = {x["id"]: x for x in m._lane_never_ran()}["declared_jobs_have_run"]

    assert c["pass"] is True, (
        "subsea_sync/fiber_sync are un-stampable; accusing them of never "
        "running is the false-accusation bug")
    assert "OUT OF SCOPE" in c["detail"]
    for j in ("subsea_sync", "fiber_sync"):
        assert j in c["detail"].split("OUT OF SCOPE")[1]


def test_a_genuinely_missing_blueprint_job_still_convicts(monkeypatch):
    """MUST-FAIL CONTROL. Narrowing the scope must not disarm the lane: a job
    that IS on jobs_bp and has no row is still a real finding."""
    import routes.data_liveness_master_shell as m

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): pass
        def fetchall(self): return []          # nothing has ever run
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(m, "_declared_jobs",
                        lambda: ({"discovery": "/api/jobs/discovery"}, None))
    monkeypatch.setattr(m, "_conn", lambda: _Conn())
    c = {x["id"]: x for x in m._lane_never_ran()}["declared_jobs_have_run"]
    assert c["pass"] is False
    assert "discovery" in c["detail"]


def test_unreadable_blueprint_is_unmeasurable_not_a_finding(monkeypatch):
    import routes.data_liveness_master_shell as m
    monkeypatch.setattr(m, "_blueprint_job_routes",
                        lambda: (set(), "blueprint renamed"))
    monkeypatch.setattr(m, "_declared_jobs",
                        lambda: ({"discovery": "/api/jobs/discovery"}, None))
    checks = m._lane_never_ran()
    assert checks[0]["pass"] is None
    assert "UNMEASURABLE" in checks[0]["detail"]


# ── 4. the subsea driver ────────────────────────────────────────────────────
def _subsea_step():
    d = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    for job in d["jobs"].values():
        for step in job.get("steps", []):
            if "subsea" in str(step.get("name", "")).lower():
                return step
    return None


def test_subsea_sync_step_is_authenticated_and_loud():
    step = _subsea_step()
    assert step is not None, "no subsea sync step in data-sync.yml"
    run = step.get("run", "")
    # INTERNAL key, not admin — fiber_integration uses @require_internal_key.
    assert "X-Internal-Key" in run
    assert "DCHUB_INTERNAL_KEY" in str(step.get("env", {}))
    # 202 is success: the call is worker-delegated.
    assert "202" in run
    # ...and a failure must FAIL the step, not echo a reassuring line (#2318).
    # NOTE `|| echo 000` is NOT a swallow here — it is the opposite. It
    # captures a transport failure into the code variable so the case below
    # can convict it; it is the same idiom the fixed news-refresh step uses.
    # The property that actually matters is the shape of the SUCCESS arm:
    # only 200 and 202 may pass, and everything else must reach exit 1.
    assert "exit 1" in run
    arm = re.search(r"^\s*([0-9|]+)\)", run, re.M)
    assert arm, "no case arm found — cannot tell which codes are treated as ok"
    ok_codes = set(arm.group(1).split("|"))
    assert ok_codes <= {"200", "202"}, (
        f"success arm accepts {sorted(ok_codes)} — a 4xx/5xx treated as "
        f"success is how a dead job stays green")
    tail = run.split(arm.group(0), 1)[1]
    assert "exit 1" in tail, "the catch-all arm must exit non-zero"


def test_subsea_step_calls_the_right_host_and_path():
    run = _subsea_step().get("run", "")
    assert "/api/jobs/subsea-sync" in run
    # dchub-api-production has a DIFFERENT admin key and 401s — see the r80
    # comment in this workflow. The backend host is the only correct target.
    assert "dchub-api-production" not in run


def test_workflow_still_parses_and_keeps_its_other_steps():
    """A YAML edit that silently drops sibling steps would be worse than the
    bug being fixed."""
    d = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    names = [s.get("name", "") for j in d["jobs"].values()
             for s in j.get("steps", [])]
    for required in ("Keep alive and News refresh", "Warm caches",
                     "Health check", "Evolution and auto-approve"):
        assert required in names, f"lost step: {required}"
