"""Cache Rule 'Cache Public API' must not pin the WORKERS' Railway fetches.

NO NETWORK. Evaluates the real pinned canon (scripts/cf_cache_ruleset_canon.json)
through scripts/cf_expression.py, which now models `http.host`.

★ WHY (2026-09-24). Zone Cache Rules are evaluated on Worker SUBREQUESTS too, on
the subrequest's own host. The rule matched on path alone, so both workers'
fetches to https://dchub-backend-production.up.railway.app/api/v1/* were cached
override_origin 3600s — over the TTLs the workers set themselves (120-300s by
tier). Measured: /api/v1/stats HIT age 435 and 2661 while the zone worker asked
for 300; a boot-degraded copy sat at the edge for 17 min, and single-file AND
prefix purges of the Railway key returned success without evicting it — only
purge_everything did. The rule now carries `not ends_with(http.host,
".up.railway.app")`: the workers' own cacheTtl governs their Railway fetches,
and every request to a dchub.cloud hostname is judged exactly as before.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANON = json.loads((ROOT / "scripts" / "cf_cache_ruleset_canon.json").read_text())
RULES = CANON["rules"]
RULE1_ID = "ef1b5109ef354d28b31d8b977daae0a1"
RAILWAY = "dchub-backend-production.up.railway.app"


def _load():
    spec = importlib.util.spec_from_file_location("_cfexpr_rule1",
                                                  ROOT / "scripts" / "cf_expression.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


X = _load()


def _verdict(path, host="", **channels):
    verdict, winner, err = X.disposition(RULES, X.Request(path, host=host, **channels))
    assert err is None, err
    return verdict, (winner or {}).get("id")


def test_rule1_is_still_the_cache_public_api_rule():
    r = [r for r in RULES if r["id"] == RULE1_ID]
    assert len(r) == 1 and r[0]["action_parameters"]["cache"] is True
    assert r[0]["action_parameters"]["edge_ttl"] == {
        "default": 3600, "mode": "override_origin",
        "status_code_ttl": [{"status_code_range": {"from": 400, "to": 599}, "value": -1}]}, (
        "this change is the host condition only; the TTL policy on zone hosts is unchanged")


# Paths rule 1 caches on the zone host (the evaluator picks them, not a list
# written from memory: /api/v1/stats is BYPASSED by rule 19 since #5405).
@pytest.mark.parametrize("path", ["/api/v1/facilities", "/api/v1/infrastructure",
                                  "/api/rankings/markets", "/api/news/latest"])
def test_worker_fetch_to_railway_is_not_pinned_by_rule1(path):
    verdict, winner = _verdict(path, host=RAILWAY)
    assert winner != RULE1_ID, f"rule 1 still pins the Railway subrequest for {path}"
    assert verdict == "no-rule", (verdict, winner)  # the worker's own cf.cacheTtl decides


@pytest.mark.parametrize("host", ["", "api.dchub.cloud", "dchub.cloud"])
def test_zone_hostnames_are_judged_exactly_as_before(host):
    """"" is the evaluator's historical contract (a zone request, host unspecified)."""
    assert _verdict("/api/v1/facilities", host=host) == ("cached", RULE1_ID)


@pytest.mark.parametrize("path", ["/api/v1/deals", "/api/v1/seo/x"])
def test_path_bypass_rules_still_win_on_the_railway_host(path):
    """Excluding Railway from rule 1 must not turn a bypass into anything else
    (rule 19 bypasses /api/v1/seo/ on every host. It named /api/v1/stats from
    #5405 until the follow-up to #5460, which left stats to the worker)."""
    assert _verdict(path, host=RAILWAY)[0] == "bypass"
    assert _verdict(path, host="api.dchub.cloud")[0] == "bypass"


def test_credentialed_railway_fetch_still_bypasses():
    verdict, _ = _verdict("/api/v1/facilities", host=RAILWAY, headers=("x-api-key",))
    assert verdict == "bypass"


def test_evaluator_models_http_host():
    node = X.parse('ends_with(http.host, ".up.railway.app")')
    assert X.evaluate(node, X.Request("/", host=RAILWAY)) is True
    assert X.evaluate(node, X.Request("/", host="api.dchub.cloud")) is False
    assert X.Request("/").host == "", "default must keep every existing caller's verdicts"
