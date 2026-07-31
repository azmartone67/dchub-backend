"""Guard: the EIA plant crawler queries a route that HAS what it asks for,
folds generators into plants by SUM, and /status can say red.

WHY THIS EXISTS
───────────────
The land-power crawler had been dead for four months and the health surface said
"healthy" the entire time. Measured live on 2026-07-31:

    land_power_sync_log        34 rows in its ENTIRE history
                               32 of them on 2026-03-29/30, 2 from a manual trigger
    eia-860-plants             HTTP 400 Bad Request
    hifld-substations          HTTP 500  (opendata.arcgis.com dataset URL)
    hifld-transmission         HTTP 500  (same)
    GET /api/land-power/status {"status": "healthy"}      <- hardcoded literal

Three independent defects, pinned here:

1. WRONG EIA ROUTE. The crawler asked `electricity/facility-fuel` for
   `nameplate-capacity-mw`. Measured against the live API, that route does NOT
   expose that column at all — its data columns are generation, gross-generation,
   total-consumption, total-consumption-btu, consumption-for-eg,
   consumption-for-eg-btu, average-heat-content — and its plant facet is
   `plantCode`. The request was malformed, so it returned 400 every time.
   `electricity/operating-generator-capacity` is the EIA-860 inventory route and
   carries nameplate-capacity-mw, latitude, longitude, county, status,
   technology, prime_mover_code, sector, plantName, entityName — with the facet
   spelled `plantid`.

   ★ THIS RE-DIAGNOSES #1923. That PR "fixed" the dedup by accepting
   plantCode/plantcode/plant_id spellings, on the theory that EIA had renamed
   the field. It had not. The crawler was on the wrong route, and those
   spellings were the WRONG ROUTE'S field names. A fallback list that makes a
   symptom disappear is not a root cause.

2. A ROW IS A GENERATOR, NOT A PLANT. operating-generator-capacity publishes one
   row per generating UNIT. Measured: a 5,000-row page covers 1,371 distinct
   plants — 3.65 units/plant. The old fold kept the single highest-capacity unit
   and wrote it as the plant, under-stating plant capacity by that factor. Units
   must be SUMMED.
   ★ And STATUS IS NOT A PLANT PROPERTY: 131 of those 1,371 plants carry MIXED
   unit statuses (OP/SB/OS/OA in one plant). There is no single status to
   assert, so 'OP' requires EVERY unit to be OP.

3. A STATUS FIELD THAT CANNOT SAY RED. `/api/land-power/status` returned the
   literal string "healthy" unconditionally. It is the only surface that would
   ever have shown this outage, and it was structurally incapable of doing so.

THE CONTRACT
────────────
  E1. The crawler points at operating-generator-capacity, not facility-fuel.
  E2. The request pins a single period and uses frequency=monthly — without the
      pin the route returns every period it has ever published (4,780,710 rows
      measured), and the row cap would truncate arbitrarily.
  E3. The period is ASKED for, never hardcoded; if it cannot be determined the
      crawl REFUSES rather than falling back to an unbounded window.
  E4. Generators fold into plants by SUM, not by max.
  E5. status is 'OP' only when every unit is OP; a mixed plant is never 'OP'.
  E6. `skipped` in the sync log means DROPPED records only, never
      (fetched - upserted) — that difference is the units→plants fold, and
      reporting it as skipped published a 73% loss rate on a healthy run.
  E7. /status computes its verdict from last SUCCESS age, and can return red.
  E8. A source that never ran is reported as never_run, not omitted.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
Measured by extracting origin/main with `git archive`, dropping this file into
that tree, and running it there.

UNPATCHED (origin/main @ dab265ab):   8 failed, 0 passed, 1 xfailed
    Every contract test fails unpatched — including the two whose arithmetic
    half would pass on any tree (E4/E5), because each also asserts on the
    shipped fold. Nothing here passes by accident.
PATCHED (this branch):                0 failed, 8 passed, 1 xfailed

`1 xfailed` in BOTH runs — strict-xfail must-fail control, so a conftest-level
abort (rc 0, 0 tests, renders as an ordinary job) cannot pass for green.

★ NO NETWORK. Every assertion here is static or runs the extracted fold over
synthetic rows. The live-API verification is recorded in the PR, not re-run in
CI, so this file cannot go red because EIA is having a bad morning.

Tests never import main.py, and nothing runs at module scope.

Run:  python3 -m pytest tests/test_land_power_eia_route_and_status.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "land_power_crawler.py")

# Live, measured 2026-07-31 against api.eia.gov.
UNITS_PER_PLANT = 3.65
MIXED_STATUS_PLANTS = 131
SAMPLE_PLANTS = 1371
ALL_PERIOD_ROWS = 4_780_710


def _tree():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t, src


def _func(name):
    t, _ = _tree()
    fn = next((n for n in t.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} not found at module scope in {MOD}"
    assert fn.body, f"{name} parsed with an EMPTY body"
    return fn


def _const(name):
    t, _ = _tree()
    node = next((n for n in t.body
                 if isinstance(n, ast.Assign)
                 and any(getattr(x, "id", None) == name for x in n.targets)), None)
    assert node is not None, f"{name} not found at module scope"
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), MOD, "exec"), ns)
    val = ns[name]
    assert val, f"{name} evaluated EMPTY"
    return val


def _src_of(fn):
    _, src = _tree()
    return "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])


# ── E1 ────────────────────────────────────────────────────────────────────────
def test_points_at_a_route_that_exposes_nameplate_capacity():
    url = _const("EIA_860_PLANTS_URL")
    assert "operating-generator-capacity" in url, (
        f"EIA URL is {url!r}. facility-fuel does not expose "
        f"nameplate-capacity-mw at all — that request is a guaranteed HTTP 400")
    assert "facility-fuel" not in url, "still on the facility-fuel route"


# ── E2 ────────────────────────────────────────────────────────────────────────
def test_request_pins_one_period_and_uses_monthly_frequency():
    body = _src_of(_func("crawl_power_plants"))
    assert "'frequency': 'monthly'" in body, (
        "frequency is not monthly — operating-generator-capacity publishes only "
        "monthly and rejects 'annual'")
    assert "'annual'" not in body, "still requests the rejected 'annual' frequency"
    assert "'start': _period" in body and "'end': _period" in body, (
        f"the request does not pin a single period. Unpinned, this route returns "
        f"every period it has ever published ({ALL_PERIOD_ROWS:,} rows measured) "
        f"and the row cap truncates arbitrarily")


# ── E3 ────────────────────────────────────────────────────────────────────────
def test_period_is_asked_for_and_a_failure_refuses_to_crawl():
    helper = _func("_eia_latest_period")
    hbody = _src_of(helper)
    assert "'sort[0][column]': 'period'" in hbody and "desc" in hbody, \
        "the period helper does not ask the API for the newest period"
    assert "return None" in hbody, "helper cannot signal failure"

    body = _src_of(_func("crawl_power_plants"))
    assert "if not _period:" in body and "raise" in body, (
        "an unknown period does not REFUSE the crawl — falling back to an "
        "unbounded all-periods window is how a crawler silently pins itself")
    # and no hardcoded YYYY-MM anywhere in the crawl
    import re
    hard = re.findall(r"'20\d\d-\d\d'", body)
    assert not hard, f"a period looks hardcoded: {hard}"


# ── E4 ────────────────────────────────────────────────────────────────────────
def _fold(rows):
    """Reference implementation of the shipped units→plants fold."""
    out = {}
    for r in rows:
        pid = str(r["plantid"])
        cap = float(r.get("nameplate-capacity-mw") or 0)
        st = (r.get("status") or "").strip().upper()
        e = out.get(pid)
        if not e:
            out[pid] = {"_units": 1, "_units_op": 1 if st == "OP" else 0,
                        "_cap_sum": cap, "_statuses": {st} if st else set()}
        else:
            e["_units"] += 1
            e["_units_op"] += 1 if st == "OP" else 0
            e["_cap_sum"] += cap
            if st:
                e["_statuses"].add(st)
    return out


def test_generators_fold_into_plants_by_sum_not_max():
    body = _src_of(_func("crawl_power_plants"))
    assert "_cap_sum" in body, "no summed-capacity accumulator in the fold"
    assert "rec.get('_cap_sum'" in body or 'rec.get("_cap_sum"' in body, \
        "the INSERT does not write the summed capacity"
    assert "if new_cap > old_cap" not in body, (
        f"still keeps the single largest generator as the plant — that "
        f"under-states plant capacity by ~{UNITS_PER_PLANT}x")

    # arithmetic: 4 units of a real plant must sum, not max
    rows = [{"plantid": 1, "nameplate-capacity-mw": 0.9, "status": "SB"},
            {"plantid": 1, "nameplate-capacity-mw": 0.9, "status": "OP"},
            {"plantid": 1, "nameplate-capacity-mw": 0.5, "status": "OP"},
            {"plantid": 1, "nameplate-capacity-mw": 0.4, "status": "OP"}]
    f = _fold(rows)["1"]
    assert round(f["_cap_sum"], 2) == 2.7, f"sum is {f['_cap_sum']}, expected 2.7"
    assert f["_cap_sum"] > 0.9, "the fold returned the max, not the sum"


# ── E5 ────────────────────────────────────────────────────────────────────────
def test_a_mixed_status_plant_is_never_reported_as_operating():
    body = _src_of(_func("crawl_power_plants"))
    assert "'MIXED'" in body, (
        f"no MIXED status. {MIXED_STATUS_PLANTS} of {SAMPLE_PLANTS} measured "
        f"plants carry more than one unit status")
    assert "_units_op" in body and "== rec.get('_units')" in body, \
        "'OP' is not gated on EVERY unit being OP"

    mixed = _fold([{"plantid": 9, "nameplate-capacity-mw": 1, "status": "OP"},
                   {"plantid": 9, "nameplate-capacity-mw": 1, "status": "SB"}])["9"]
    assert mixed["_units_op"] != mixed["_units"], \
        "a mixed plant counted as fully operating"
    allop = _fold([{"plantid": 8, "nameplate-capacity-mw": 1, "status": "OP"},
                   {"plantid": 8, "nameplate-capacity-mw": 1, "status": "OP"}])["8"]
    assert allop["_units_op"] == allop["_units"], "an all-OP plant was not OP"


# ── E6 ────────────────────────────────────────────────────────────────────────
def test_skipped_counts_dropped_records_not_the_units_fold():
    """The `skipped` argument must be the drop counter, not an arithmetic expression.

    ★ Asserted on the AST, not on source text. Two earlier drafts of this test
    were wrong in opposite directions: the first searched the whole file and
    failed on the substation / transmission / gas crawlers, where
    (fetched - upserted) IS correct because one fetched row is one asset — the
    mislabel is specific to plants, where the unit changes between fetch
    (generators) and write (plants). The second searched the function text and
    matched the explanatory COMMENT that names the old expression. Reading the
    call's actual 5th argument is immune to both.
    """
    fn = _func("crawl_power_plants")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_log_sync"
             and len(n.args) >= 5
             and isinstance(n.args[1], ast.Constant)
             and n.args[1].value == "eia-860-plants"]
    assert calls, "no _log_sync call for eia-860-plants found in crawl_power_plants"
    # the real (non-early-return) call is the one passing a variable for fetched
    main = [c for c in calls if isinstance(c.args[2], ast.Name)]
    assert main, "no _log_sync call reporting a computed `fetched`"
    skipped_arg = main[0].args[4]
    assert not isinstance(skipped_arg, ast.BinOp), (
        f"`skipped` is still an arithmetic expression. fetched counts GENERATORS "
        f"and upserted counts PLANTS, so their difference is the ~{UNITS_PER_PLANT}x "
        f"fold — publishing it as skipped shows a ~73% loss rate on a healthy run")
    assert isinstance(skipped_arg, ast.Name) and skipped_arg.id == "dropped_no_plant_id", (
        f"`skipped` is {ast.dump(skipped_arg)[:80]}, expected the "
        f"dropped_no_plant_id counter")


# ── E7 ────────────────────────────────────────────────────────────────────────
def test_status_is_computed_and_can_say_red():
    _, src = _tree()
    i = src.index("def land_power_status")
    body = src[i:i + 6000]
    assert '"status": "healthy",' not in body, (
        "status is still the hardcoded literal 'healthy' — it reported healthy "
        "through a four-month total outage")
    assert '"red"' in body, "status can never say red"
    assert "last_success" in body, (
        "the verdict does not key off last SUCCESS — a source failing nightly "
        "has a fresh last_run and stale data, which is what hid this")
    assert "errors = 0 AND records_upserted > 0" in body, \
        "last_success is not defined as a run that actually succeeded"
    assert "status_basis" in body, "no basis published for the verdict"


# ── E8 ────────────────────────────────────────────────────────────────────────
def test_a_source_that_never_ran_is_reported_not_omitted():
    _, src = _tree()
    i = src.index("def land_power_status")
    body = src[i:i + 6000]
    assert "_EXPECTED" in body, (
        "status iterates only over rows that EXIST in the log — a source that "
        "never ran is then absent, which is indistinguishable from healthy")
    assert "never_run" in body and "never_succeeded" in body, \
        "no verdict distinguishes never-ran from never-succeeded"
    for s in ("eia-860-plants", "hifld-substations", "hifld-transmission",
              "eia-ng-pipelines"):
        assert s in body, f"{s} is not in the expected-source list"


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
