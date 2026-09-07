"""Tests for the Cloudflare cache-ruleset drift guard.

These exercise the REAL functions from `scripts/check_cf_cache_ruleset.py`
against mutated copies of the REAL pinned canon — not a hand-built fixture and
not a reimplementation of the differ. A guard tested against a mirror of itself
is green by construction.

Each drift class below corresponds to an edit somebody can make in the
Cloudflare dashboard in about four seconds, with no review and no audit trail
in this repo.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_cf_cache_ruleset.py"
_CANON = Path(__file__).resolve().parents[1] / "scripts" / "cf_cache_ruleset_canon.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_cf_cache_ruleset", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_module()


@pytest.fixture
def canon() -> dict:
    return json.loads(_CANON.read_text())


@pytest.fixture
def live(canon) -> list[dict]:
    """A live ruleset identical to canon — the baseline that must be green."""
    return copy.deepcopy(canon["rules"])


# ---------------------------------------------------------------- baseline
def test_identical_ruleset_reports_no_drift(canon, live):
    assert guard.diff_ruleset(canon, live) == []


def test_the_pinned_canon_passes_its_own_floor(canon):
    assert guard.check_canon_floor(canon) is None


# ---------------------------------------------------------------- drift classes
def test_added_rule_is_drift(canon, live):
    live.append(
        {
            "position": len(live) + 1,
            "id": "0" * 32,
            "description": "Cache everything, what could go wrong",
            "expression": 'starts_with(http.request.uri.path, "/api/v1/")',
            "action": "set_cache_settings",
            "action_parameters": {"cache": True},
            "enabled": True,
        }
    )
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("ADDED") for f in findings), findings


def test_removed_rule_is_drift(canon, live):
    dropped = live.pop()
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("REMOVED") and dropped["id"][:8] in f for f in findings), findings


def test_changed_expression_is_drift(canon, live):
    live[0]["expression"] = 'http.request.uri.path eq "/nope"'
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("CHANGED expression") for f in findings), findings


def test_flipping_a_bypass_to_cacheable_is_drift(canon, live):
    """The exact 2026-09-06 failure: a bypass rule turned into a caching rule."""
    target = next(
        r for r in live if isinstance(r["action_parameters"], dict)
        and r["action_parameters"].get("cache") is False
    )
    target["action_parameters"] = {"cache": True, "edge_ttl": {"mode": "override_origin"}}
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("CHANGED action_parameters") for f in findings), findings


def test_disabling_a_rule_is_drift(canon, live):
    live[-1]["enabled"] = False
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("CHANGED enabled") for f in findings), findings


def test_rewriting_a_description_is_drift(canon, live):
    """Descriptions carry the WHY of each bypass; a silent rewrite loses it."""
    live[-1]["description"] = "misc"
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("CHANGED description") for f in findings), findings


def test_pure_reorder_is_drift(canon, live):
    """★ The one a per-rule comparison misses.

    Cache Rules are LAST-MATCH-WINS. Swapping two rules changes what the edge
    serves while every individual rule still compares byte-identical — which is
    precisely how rule 2 ('Cache Public API') beat rule 1 ('No cache auth') and
    served a keyed CSV to an anonymous caller.
    """
    live[0], live[1] = live[1], live[0]
    findings = guard.diff_ruleset(canon, live)
    assert any(f.startswith("REORDERED") for f in findings), findings
    # and it must NOT be reported as a content change, which would mislead
    assert not any(f.startswith("CHANGED") for f in findings), findings


# ---------------------------------------------------------------- floors
def test_emptied_canon_is_refused_not_matched(canon):
    canon["rules"] = []
    assert guard.check_canon_floor(canon) is not None


def test_truncated_canon_is_refused(canon):
    canon["rules"] = canon["rules"][: guard.MIN_CANON_RULES - 1]
    assert guard.check_canon_floor(canon) is not None


def test_canon_without_ruleset_id_is_refused(canon):
    canon["ruleset_id"] = ""
    assert guard.check_canon_floor(canon) is not None


def test_duplicate_rule_ids_are_refused(canon):
    canon["rules"] = canon["rules"] + [copy.deepcopy(canon["rules"][0])]
    assert guard.check_canon_floor(canon) is not None


# ---------------------------------------------------------------- probe verdicts
def _obs(cache_status, worker_stamp="10ms", age=None):
    return {"status": 200, "cf_cache_status": cache_status, "age": age, "worker_stamp": worker_stamp}


def test_probe_fails_when_a_bypass_path_is_served_from_cache():
    entry = {"path": "/x", "expect": "bypass", "rule": "r", "why": "w"}
    verdict, message = guard.evaluate_probe(entry, [_obs("HIT", "108ms", "1148"), _obs("HIT", "108ms", "1149")])
    assert verdict == "fail"
    assert "edge cache" in message


def test_probe_passes_when_a_bypass_path_is_dynamic():
    entry = {"path": "/x", "expect": "bypass", "rule": "r", "why": "w"}
    verdict, _ = guard.evaluate_probe(entry, [_obs("DYNAMIC"), _obs("DYNAMIC")])
    assert verdict == "ok"


def test_missing_cache_header_is_unchecked_not_ok():
    """★ An unreadable edge verdict is not the same as 'uncached'."""
    entry = {"path": "/x", "expect": "bypass", "rule": "r", "why": "w"}
    verdict, _ = guard.evaluate_probe(entry, [_obs(None), _obs(None)])
    assert verdict == "unchecked"


def test_transport_error_is_unchecked_not_ok():
    entry = {"path": "/x", "expect": "bypass", "rule": "r", "why": "w"}
    verdict, _ = guard.evaluate_probe(entry, [{"error": "ConnectionError: boom"}, _obs("DYNAMIC")])
    assert verdict == "unchecked"


def test_control_that_never_hits_is_flagged_as_weak_evidence():
    """If the control cannot observe a HIT, 'no HIT observed' proves little."""
    entry = {"path": "/api/v1/stats", "expect": "cached", "rule": "r", "why": "control"}
    verdict, message = guard.evaluate_probe(entry, [_obs("DYNAMIC"), _obs("MISS")])
    assert verdict == "control_weak"
    assert "weaker evidence" in message


def test_control_hit_confirms_the_probe_discriminates():
    entry = {"path": "/api/v1/stats", "expect": "cached", "rule": "r", "why": "control"}
    verdict, _ = guard.evaluate_probe(entry, [_obs("MISS"), _obs("HIT", "108ms", "3")])
    assert verdict == "ok"


# ---------------------------------------------------------------- the invariant
def test_mcp_tool_routes_bypass_and_outrank_the_public_api_cache_rule(canon):
    """The 2026-09-06 fix, pinned as an invariant rather than a snapshot.

    `/api/v1/mcp/tools/*` must be bypassed, AND its rule must sit AFTER the
    'Cache Public API' rule that matches the same paths — otherwise
    last-match-wins hands the request back to the caching rule.
    """
    rules = canon["rules"]
    public_api = next(
        r for r in rules
        if "/api/v1/" in r["expression"] and r["action_parameters"].get("cache") is True
    )
    mcp_tools = [
        r for r in rules
        if "/api/v1/mcp/tools/" in r["expression"] and r["action_parameters"].get("cache") is False
    ]
    assert mcp_tools, "no bypass rule covers /api/v1/mcp/tools/ — the CSV leak path is uncovered"
    assert max(r["position"] for r in mcp_tools) > public_api["position"], (
        "the /api/v1/mcp/tools/ bypass sits BEFORE the public-API caching rule; "
        "last-match-wins means the caching rule wins and the leak is re-opened"
    )


# ---------------------------------------------------------------- credentials
def test_token_sources_are_tried_in_order():
    env = {"CF_CACHE_RULES_TOKEN": "a", "CLOUDFLARE_API_TOKEN": "b", "CF_TOKEN": "c"}
    assert guard.resolve_token(env) == ("a", "CF_CACHE_RULES_TOKEN")
    del env["CF_CACHE_RULES_TOKEN"]
    assert guard.resolve_token(env) == ("b", "CLOUDFLARE_API_TOKEN")
    del env["CLOUDFLARE_API_TOKEN"]
    assert guard.resolve_token(env) == ("c", "CF_TOKEN")


def test_an_empty_secret_is_not_a_credential():
    """★ `${{ secrets.MISSING }}` expands to the EMPTY STRING, not to unset.

    So in CI the variable is always *present*; only its emptiness distinguishes
    a missing secret. Treating "" as a credential would send a blank bearer
    token and report the resulting 400 as a permission problem.
    """
    env = {"CF_CACHE_RULES_TOKEN": "", "CLOUDFLARE_API_TOKEN": "   ", "CF_TOKEN": "real"}
    assert guard.resolve_token(env) == ("real", "CF_TOKEN")


def test_no_credential_at_all_returns_none():
    assert guard.resolve_token({}) == (None, None)


def test_workflow_exports_every_token_source_the_script_knows():
    """★ The bug this file exists to prevent a second time.

    v1 exported only CF_CACHE_RULES_TOKEN while the script documented a
    CLOUDFLARE_API_TOKEN fallback, so the fallback could never fire in CI and
    the run blamed a missing secret. A documented fallback the caller does not
    wire up is not a fallback.
    """
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github" / "workflows" / "cf-cache-ruleset-drift.yml"
    ).read_text()
    step_env = workflow.split("env:", 1)[1].split("run:", 1)[0]
    missing = [name for name in guard.TOKEN_ENV_VARS if f"{name}:" not in step_env]
    assert not missing, f"script reads {missing} but the workflow never exports them"


@pytest.mark.parametrize(
    "status,expected_prefix",
    [(401, "CREDENTIAL REFUSED"), (403, "CREDENTIAL REFUSED"), (500, "Cloudflare API returned HTTP 500")],
)
def test_refused_credential_is_reported_separately_from_a_server_error(
    monkeypatch, status, expected_prefix
):
    """Absent, refused and broken are three different fixes, not one message."""
    import requests

    class FakeResponse:
        status_code = status
        text = '{"errors":[{"code":9109}]}'

        def json(self):
            raise ValueError("not json")

    monkeypatch.setenv("CF_CACHE_RULES_TOKEN", "pretend-token")
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
    result, error = guard.fetch_live_ruleset("zone", "ruleset")
    assert result is None
    assert error.startswith(expected_prefix), error
    if status in (401, 403):
        assert "CF_CACHE_RULES_TOKEN" in error  # names WHICH credential was refused


def test_absent_credential_names_every_variable_it_looked_for(monkeypatch):
    for name in guard.TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    result, error = guard.fetch_live_ruleset("zone", "ruleset")
    assert result is None
    assert error.startswith("NO CREDENTIAL PRESENT")
    for name in guard.TOKEN_ENV_VARS:
        assert name in error


# ---------------------------------------------------------------- escalation
def test_the_drift_alarm_reaches_ci_triage_by_its_EXACT_name():
    """★ A guard whose alarm reaches nobody is the alarm not existing.

    `workflow_run` matches by the workflow's declared `name:` STRING, so the
    ci-triage allowlist and the workflow name are coupled by text with nothing
    enforcing it. Rename the workflow and the entry silently stops matching:
    the job still runs, still goes red, and simply stops being routed —
    no error, anywhere.

    ci-triage.yml's own header records this class already ("THE GATING
    WORKFLOWS WERE NOT ON THIS LIST", 2026-08-30, found cold by hand).
    """
    import yaml

    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    declared_name = yaml.safe_load((root / "cf-cache-ruleset-drift.yml").read_text())["name"]
    triage = yaml.safe_load((root / "ci-triage.yml").read_text())
    # PyYAML parses the bare `on:` key as the boolean True.
    watched = triage[True]["workflow_run"]["workflows"]

    assert declared_name in watched, (
        f"cf-cache-ruleset-drift.yml declares name {declared_name!r}, which is "
        f"NOT in ci-triage's allowlist {watched!r}. Its failures reach nobody."
    )


def test_refused_message_does_not_assert_an_unverified_permission_group(monkeypatch):
    """★ Regression guard on advice, not on code.

    The refused message used to name "Zone > Config Rules: Read" with full
    confidence. Nothing verified it — the token cannot enumerate permission
    groups (403 on /user/tokens/permission_groups) and the Cloudflare docs do
    not state it — and on 2026-09-07 a token minted from that advice was
    refused, costing a round trip.

    Remediation text a reader cannot check is worse than none: it turns "I do
    not know" into a confident wrong turn. The message must carry the measured
    evidence and a test command instead.
    """
    import requests

    class FakeResponse:
        status_code = 403
        text = '{"errors":[{"message":"request is not authorized"}]}'

        def json(self):
            raise ValueError("not json")

    monkeypatch.setenv("CF_CACHE_RULES_TOKEN", "pretend-token")
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
    _, error = guard.fetch_live_ruleset("zone", "ruleset")

    assert "Config Rules" not in error, (
        "the refused message names a permission group that was never verified "
        "and is not the one that works"
    )
    # it must hand the reader a way to check for themselves
    assert "check_cf_cache_ruleset.py" in error
    assert "403" in error and "200" in error  # the measured evidence table
