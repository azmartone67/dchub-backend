"""Rule 2 'Cache Public API' must not match the three force-public stats paths.

NO NETWORK. Evaluates the real pinned canon (scripts/cf_cache_ruleset_canon.json)
through scripts/cf_expression.py, last match wins.

★ WHY (2026-09-24). Zone Cache Rules govern the zone worker's Cache API put()
as well as fetch(). Measured on api.dchub.cloud, worker 4.9.78:

  * override_origin stretches the worker's entry. /api/v1/site/stats and
    /api/v1/discovery/last-7d were HIT with x-dc-edge-cache-age 3550, against
    the 300s the worker stores them for: rule 2's 3600s, not the worker's.
  * a BYPASS rule makes put() resolve and store nothing. /api/v1/stats (rule 19)
    answered `x-dc-edge-store: put` then `readback-miss`, and never HIT;
    /api/v1/ops/claims (rule 21) the same. /api/v1/freshness (rule 2) and
    /api/v1/news stored and HIT normally.

So bypassing these paths (the first idea) would switch the edge cache off for
the homepage hero counts entirely. With NO rule, the worker's public-key cache
keeps its own TTL, and it already refuses a no-store body
(x-dc-edge-store: skip:origin-cache-control), which is the boot-degraded
/api/v1/stats case rule 19 was added for.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "scripts" / "cf_cache_ruleset_canon.json").read_text())["rules"]
RULE2_ID = "ef1b5109ef354d28b31d8b977daae0a1"
RAILWAY = "dchub-backend-production.up.railway.app"
STATS_PATHS = ("/api/v1/stats", "/api/v1/site/stats", "/api/v1/discovery/last-7d")


def _load():
    spec = importlib.util.spec_from_file_location("_cfexpr_rule2_stats",
                                                  ROOT / "scripts" / "cf_expression.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


X = _load()
RULE2 = next(r for r in RULES if r["id"] == RULE2_ID)


def _rule2_matches(path, host="api.dchub.cloud"):
    return X.evaluate(X.parse(RULE2["expression"]), X.Request(path, host=host))


def _verdict(path, **kw):
    verdict, winner, err = X.disposition(RULES, X.Request(path, **kw))
    assert err is None, err
    return verdict, (winner or {}).get("id")


@pytest.mark.parametrize("path", STATS_PATHS)
@pytest.mark.parametrize("host", ["api.dchub.cloud", "dchub.cloud"])
def test_rule2_does_not_match_the_stats_paths(path, host):
    assert _rule2_matches(path, host) is False


@pytest.mark.parametrize("path", STATS_PATHS)
def test_stats_paths_match_no_rule_anonymously(path):
    # Not 'bypass': a bypass rule would make the worker's put() store nothing.
    # /api/v1/stats joined once rule 19 dropped it (the follow-up to #5460).
    assert _verdict(path, host="api.dchub.cloud") == ("no-rule", None)


# Exact paths only. A prefix would carry the exclusion to routes nobody measured.
@pytest.mark.parametrize("path", ["/api/v1/stats/canonical", "/api/v1/site/stats/x",
                                  "/api/v1/discovery/last-30d", "/api/v1/facilities",
                                  "/api/v1/freshness", "/api/v1/statsx"])
def test_neighbouring_paths_are_still_cached_by_rule2(path):
    assert _rule2_matches(path) is True
    assert _verdict(path, host="api.dchub.cloud") == ("cached", RULE2_ID)


@pytest.mark.parametrize("path", STATS_PATHS)
def test_credentialed_requests_still_bypass(path):
    verdict, _ = _verdict(path, host="api.dchub.cloud", headers={"x-api-key"})
    assert verdict == "bypass"


def test_rule2_keeps_its_railway_host_exclusion_and_ttl_policy():
    assert 'not ends_with(http.host, ".up.railway.app")' in RULE2["expression"]
    assert _rule2_matches("/api/v1/facilities", host=RAILWAY) is False
    assert RULE2["action_parameters"]["cache"] is True
    assert RULE2["action_parameters"]["edge_ttl"]["mode"] == "override_origin"
    assert RULE2["action_parameters"]["edge_ttl"]["default"] == 3600
