"""Guard: HIFLD endpoints resolve themselves, validate by COUNT AND FIELDS, and
the sync is wired to a dispatcher that actually runs.

WHAT THIS PINS
──────────────
Both HIFLD feeds were pinned to one hardcoded opendata.arcgis.com dataset URL.
Those URLs returned HTTP 500 ("Item does not exist or is inaccessible") and
stayed dead for four months. Every run recorded fetched=0, errors=1 in
land_power_sync_log; nothing read it, and /api/land-power/status returned a
hardcoded "healthy".

A single pinned URL is a single point of silent failure for a feed nobody
watches, and these layers get republished under new org/service ids routinely.
So the crawler now resolves its endpoint at RUN TIME from an ordered candidate
list and records which one it chose.

★★ THE DECOY. Measured 2026-07-31 while finding replacements: one substation
candidate answers 200 with a perfectly valid FeatureServer exposing every field
the parser reads — and holds 128 rows, against a national layer of 75,328.
"It responded" is not "it is the right layer". Validation is therefore a row
FLOOR *and* a field check, never HTTP 200. That candidate is kept in the list
on purpose, second, as a live regression case: if the floor is ever dropped it
silently wins on any day the real one is down, and the result is a 99.8% data
loss that looks like a successful sync.

★ Same trap on transmission: the layer exists at 89,744 features on one org and
52,244 on another. The smaller is a different population (close to
transmission_lines_eia's 56,108, not the 94,626 maintained transmission_lines).
Picking whichever responds first would have quietly swapped the population — the
wrong-table class, arriving through a URL instead of a table name.

SCHEDULING. dchub-scheduler.py declares land_power_sync_incremental at 04:30 in
its JOBS dict, and that entire 34-job dict is dead code: nothing invokes the
file, railway.json runs start_web.sh, and 3 days of HTTP logs showed exactly one
POST to /api/land-power/sync (a manual one). The sync is moved onto
.github/workflows/dchub-jobs.yml, which demonstrably runs hourly and succeeds.
Deliberately NOT by reviving the 34 dormant jobs — several send email and post
publicly, so that is a decision to take on purpose, not a side effect.

THE CONTRACT
────────────
  H1. Neither crawler reads a hardcoded opendata.arcgis.com download URL.
  H2. Every layer declares BOTH a row floor and required fields.
  H3. The resolver rejects a candidate below the floor even when it is a valid
      endpoint exposing every field (the 128-row decoy).
  H4. The resolver rejects a candidate missing a required field even when it
      clears the floor.
  H5. When nothing qualifies it raises, and the message names EVERY candidate's
      verdict — so the sync log records why the layer is unavailable.
  H6. The chosen endpoint is recorded in the sync log on success, and
      source_note is bound at function scope (the #1994 reporter rule).
  H7. The sync is reachable from the dispatcher that runs: a /api/jobs/ route
      exists, and dchub-jobs.yml has a cron plus a schedule entry for it.
  H8. The job endpoint does not report its own spawn as success — it returns the
      PREVIOUS run's outcome, because the crawl outlives the 300s dispatcher
      budget and a 200 meaning "thread created" is what let this feed die.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ c5155e21):   9 failed, 0 passed, 1 xfailed
PATCHED (this branch):                0 failed, 9 passed, 1 xfailed

`1 xfailed` in both runs — strict-xfail must-fail control.

★ NO NETWORK. The resolver is exercised against a stubbed probe, so this file
cannot go red because ArcGIS is having a bad morning. The live verification
(75,328 substations / 89,744 transmission, both fetched and parsed) is recorded
in the PR, not re-run in CI.

Tests never import main.py; nothing runs at module scope.

Run:  python3 -m pytest tests/test_hifld_self_healing_and_scheduling.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "land_power_crawler.py")
WF = os.path.join(ROOT, ".github", "workflows", "dchub-jobs.yml")

# Live, measured 2026-07-31.
SUBSTATIONS_LIVE = 75328
TRANSMISSION_LIVE = 89744
DECOY_ROWS = 128
WRONG_POPULATION_TRANSMISSION = 52244


def _tree():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t, src


def _const(name):
    t, _ = _tree()
    node = next((n for n in t.body if isinstance(n, ast.Assign)
                 and any(getattr(x, "id", None) == name for x in n.targets)), None)
    assert node is not None, f"{name} not found at module scope in {MOD}"
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), MOD, "exec"), ns)
    v = ns[name]
    assert v, f"{name} evaluated EMPTY — an empty literal passes every check"
    return v


def _func(name):
    t, _ = _tree()
    fn = next((n for n in t.body if isinstance(n, ast.FunctionDef)
               and n.name == name), None)
    assert fn is not None, f"{name} not found at module scope"
    assert fn.body, f"{name} parsed with an EMPTY body"
    return fn


def _resolver(probe_results):
    """Run the SHIPPED _resolve_arcgis_layer against a stubbed probe.

    probe_results maps url -> (count, fields, error).
    """
    fn = _func("_resolve_arcgis_layer")
    ns = {
        "_ARCGIS_LAYERS": _const("_ARCGIS_LAYERS"),
        "_arcgis_probe": lambda url: probe_results[url],
        "logger": type("L", (), {"info": staticmethod(lambda *a, **k: None)})(),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), MOD, "exec"), ns)
    return ns["_resolve_arcgis_layer"]


# ── H1 ────────────────────────────────────────────────────────────────────────
def test_no_crawler_reads_a_hardcoded_dead_download_url():
    """Checked on STRING LITERALS via the AST, not on source text.

    ★ Comments do not appear in the AST, which is the point: the fix's own
    explanatory comment names opendata.arcgis.com, and a text search flagged it.
    That is the third time in this session a guard matched its own prose — the
    rule is to assert on what the interpreter sees, not on what the file says.
    """
    for fname in ("crawl_substations", "crawl_transmission_lines"):
        fn = _func(fname)
        literals = [n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        dead = [x for x in literals if "opendata.arcgis.com" in x]
        assert not dead, (
            f"{fname} still holds an opendata.arcgis.com URL as a string "
            f"literal — those have returned HTTP 500 since at least "
            f"2026-03-29: {dead}")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_resolve_arcgis_layer"]
        assert calls, f"{fname} does not resolve its endpoint at run time"


# ── H2 ────────────────────────────────────────────────────────────────────────
def test_every_layer_declares_a_row_floor_and_required_fields():
    layers = _const("_ARCGIS_LAYERS")
    assert set(layers) >= {"hifld-substations", "hifld-transmission"}
    for key, spec in layers.items():
        assert spec.get("min_rows", 0) > 0, f"{key} has no row floor"
        assert spec.get("required_fields"), f"{key} declares no required fields"
        assert spec.get("candidates"), f"{key} has no candidates"
    # the floors must actually exclude the known-wrong populations
    assert layers["hifld-substations"]["min_rows"] > DECOY_ROWS, (
        f"the substation floor does not exclude the {DECOY_ROWS}-row decoy")
    assert layers["hifld-transmission"]["min_rows"] > WRONG_POPULATION_TRANSMISSION, (
        f"the transmission floor does not exclude the "
        f"{WRONG_POPULATION_TRANSMISSION}-feature layer, which is a different "
        f"population from the {TRANSMISSION_LIVE}-feature national one")


# ── H3 ────────────────────────────────────────────────────────────────────────
def test_a_valid_endpoint_below_the_floor_is_rejected():
    """The decoy: 200 OK, every field present, 128 rows."""
    layers = _const("_ARCGIS_LAYERS")
    cands = list(layers["hifld-substations"]["candidates"])
    fields = set(layers["hifld-substations"]["required_fields"])
    assert len(cands) >= 2, (
        "the decoy candidate has been removed from the list — it is kept "
        "deliberately as a live regression case for the row floor")
    resolve = _resolver({
        cands[0]: (None, None, "simulated outage"),
        cands[1]: (DECOY_ROWS, fields, None),        # valid, complete, tiny
    })
    with pytest.raises(RuntimeError) as ei:
        resolve("hifld-substations")
    msg = str(ei.value)
    assert str(DECOY_ROWS) in msg, f"the rejection does not cite the row count: {msg}"
    assert "floor" in msg.lower(), f"the rejection does not name the floor: {msg}"


# ── H4 ────────────────────────────────────────────────────────────────────────
def test_a_big_endpoint_missing_a_required_field_is_rejected():
    layers = _const("_ARCGIS_LAYERS")
    cands = list(layers["hifld-substations"]["candidates"])
    fields = set(layers["hifld-substations"]["required_fields"])
    short = fields - {"MAX_VOLT"}
    resolve = _resolver({
        cands[0]: (SUBSTATIONS_LIVE, short, None),   # huge, but incomplete
        cands[1]: (DECOY_ROWS, fields, None),
    })
    with pytest.raises(RuntimeError) as ei:
        resolve("hifld-substations")
    assert "MAX_VOLT" in str(ei.value), \
        f"the rejection does not name the missing field: {ei.value}"


# ── H5 ────────────────────────────────────────────────────────────────────────
def test_total_failure_names_every_candidate_verdict():
    layers = _const("_ARCGIS_LAYERS")
    cands = list(layers["hifld-substations"]["candidates"])
    resolve = _resolver({c: (None, None, f"HTTP 500 on {i}")
                         for i, c in enumerate(cands)})
    with pytest.raises(RuntimeError) as ei:
        resolve("hifld-substations")
    msg = str(ei.value)
    for i in range(len(cands)):
        assert f"HTTP 500 on {i}" in msg, (
            f"candidate {i}'s verdict is missing — the sync log would record "
            f"that the layer is unavailable without recording why: {msg}")


def test_a_good_candidate_resolves_and_reports_what_it_picked():
    layers = _const("_ARCGIS_LAYERS")
    cands = list(layers["hifld-transmission"]["candidates"])
    fields = set(layers["hifld-transmission"]["required_fields"])
    resolve = _resolver({cands[0]: (TRANSMISSION_LIVE, fields, None)})
    url, count, note = resolve("hifld-transmission")
    assert url == cands[0]
    assert count == TRANSMISSION_LIVE
    assert str(TRANSMISSION_LIVE) in note and "required fields" in note, \
        f"the success note does not record what was chosen and why: {note}"


# ── H6 ────────────────────────────────────────────────────────────────────────
def test_chosen_endpoint_is_logged_and_bound_at_function_scope():
    t, src = _tree()
    for fname in ("crawl_substations", "crawl_transmission_lines"):
        fn = _func(fname)
        tries = [n for n in fn.body if isinstance(n, ast.Try)]
        assert tries, f"{fname} has no try block"
        # source_note must be bound before the try — the #1994 reporter rule
        bound_before = {n.id for n in ast.walk(fn)
                        if isinstance(n, ast.Name)
                        and isinstance(n.ctx, ast.Store)
                        and n.lineno < tries[0].lineno}
        assert "source_note" in bound_before, (
            f"{fname}: source_note is bound inside the try but read by the "
            f"_log_sync after it — that is the NameError-while-reporting "
            f"regression from #1994")
        body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
        assert "else source_note" in body, \
            f"{fname} does not record the resolved endpoint in the sync log"


# ── H7 ────────────────────────────────────────────────────────────────────────
def test_the_sync_is_wired_to_the_dispatcher_that_actually_runs():
    _, src = _tree()
    assert "/api/jobs/land-power-sync" in src, (
        "no /api/jobs/ route — dchub-jobs.yml dispatches to /api/jobs/<name>, "
        "and dchub-scheduler.py's JOBS dict is dead code that fires nothing")
    wf = open(WF).read()
    assert "'30 4 * * *'" in wf or '"30 4 * * *"' in wf, \
        "dchub-jobs.yml has no 04:30 cron for the land-power sync"
    assert "land-power-sync" in wf, \
        "dchub-jobs.yml never dispatches land-power-sync"
    # and the :30 slot must be matched explicitly — the HOUR:00 case map cannot
    assert 'MINUTE}" = "04:30"' in wf or '"${HOUR}:${MINUTE}"' in wf, (
        "the schedule step only compares ${HOUR}:00, so a :30 job can never be "
        "selected — the cron would fire and dispatch nothing")


# ── H8 ────────────────────────────────────────────────────────────────────────
def test_the_job_endpoint_does_not_report_its_own_spawn_as_success():
    _, src = _tree()
    # Anchor on the ROUTE DECORATOR (the module docstring names the path too),
    # and slice to the NEXT decorator rather than a fixed character count.
    # ★ The fixed 4000-char window broke the moment the handler grew: the
    # per-source branch pushed the spawn response past it and this test failed
    # on a change that did not touch what it pins. A magic-number slice is a
    # guard with an expiry date.
    i = src.index("@app.route('/api/jobs/land-power-sync'")
    j = src.index("@app.route", i + 10)
    body = src[i:j]
    assert "previous_last_success" in body, (
        "the job endpoint reports only that it spawned. The crawl outlives the "
        "dispatcher's 300s budget, so a 200 meaning 'thread created' is exactly "
        "the flattering-green that let this feed die for four months")
    assert "202" in body, "the spawn should not return a bare 200"
    assert "not that it worked" in body or "STARTED, not" in body, \
        "the response does not tell the caller what the status code means"


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
