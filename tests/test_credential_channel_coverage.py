"""Tests for the credential-channel cache-coverage guard.

The guard's claim is "every credential channel the code reads is bypassed at the
edge". That claim is only worth something if the checker can go red, so these
tests pin the three ways it could quietly stop being able to:

1. the EVALUATOR must reproduce dispositions MEASURED at the live edge on
   2026-09-07 — both the ones that were already bypassed and the 22 that were
   returning `cf-cache-status: HIT age=3044` before rule 24 was extended;
2. a NEGATIVE CONTROL must still be CACHED, so a rule 24 degraded to a bare path
   match cannot make every assertion pass vacuously;
3. the CLASSIFIER must keep telling credentials apart from look-alikes
   (`max_tokens` is a sampling parameter; `response.headers.get(...)` is a header
   we SEND, not one anyone authenticates with).

★ The anonymous contract of cf_expression is pinned here too. That module is
shared with check_anon_tier_cache_coverage.py, whose whole model is "a request
carrying a path and nothing else"; adding credential modelling must not have
moved it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cfx = _load("cf_expression_cc", "scripts/cf_expression.py")
guard = _load("cred_cov", "scripts/check_credential_channel_coverage.py")
RULES = json.loads((ROOT / "scripts" / "cf_cache_ruleset_canon.json").read_text())["rules"]
STATS = "/api/v1/stats"


# ── 1. the evaluator against the live edge ───────────────────────────────────
# Measured 2026-09-07 on https://dchub.cloud/api/v1/stats, two consecutive
# un-cache-busted GETs each. BEFORE rule 24 was extended these all read HIT.
MEASURED_BYPASS = [
    ("header", "x-api-key"), ("header", "authorization"), ("header", "x-admin-key"),
    ("header", "x-internal-key"), ("header", "x-admin-token"), ("header", "x-internal-cron"),
    ("header", "x-cron-auth"), ("header", "x-fire-key"), ("header", "x-dc-internal-token"),
    ("header", "x-dc-internal-cron"), ("header", "admin-key"), ("header", "x-admin-user"),
    ("header", "x-session-id"), ("header", "mcp-session-id"), ("header", "x-mcp-session"),
    ("header", "x-mcp-session-id"),
    ("cookie", "dchub_token"), ("cookie", "dchub_refresh"), ("cookie", "dchub_session"),
    ("cookie", "session_token"), ("cookie", "auth_token"), ("cookie", "token"),
    ("cookie", "dchub_admin_key"), ("cookie", "dchub_innov_key"), ("cookie", "dchub_adopt_key"),
    ("cookie", "dchub_admin"), ("cookie", "dchub_lab_token"), ("cookie", "dch_sid"),
    ("cookie", "api_key"), ("cookie", "dc_li_session"), ("cookie", "session"),
    ("cookie", "session_id"),
    ("arg", "api_key"), ("arg", "admin_key"), ("arg", "key"), ("arg", "token"),
    ("arg", "fire_key"), ("arg", "pro_token"), ("arg", "refresh_token"), ("arg", "key_id"),
    ("arg", "session"), ("arg", "session_id"), ("arg", "sid"), ("arg", "mcp_session"),
]


@pytest.mark.parametrize("kind,name", MEASURED_BYPASS, ids=lambda v: str(v))
def test_measured_credential_channels_are_bypassed(kind, name):
    req = guard._request_for(cfx, kind, name, STATS)
    verdict, rule, err = cfx.disposition(RULES, req)
    assert verdict == "bypass", (
        f"{kind} {name} evaluates {verdict!r} on {STATS} (rule "
        f"{(rule or {}).get('id','?')[:8]}, {err}). Measured DYNAMIC at the live "
        "edge — a credentialed response stored under a URL-only key is served to "
        "the next caller."
    )


# ── 2. anti-vacuity ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind,name", [
    ("header", "x-definitely-not-a-credential"),
    ("header", "user-agent"),
    ("cookie", "_ga"),
    ("arg", "limit"),
])
def test_non_credentials_are_still_cached(kind, name):
    """If these bypass, rule 24 has become a bare path match and every
    assertion above passes for free. Measured: `?limit=7` MISS->HIT, `_ga` HIT."""
    verdict, _, _ = cfx.disposition(RULES, guard._request_for(cfx, kind, name, STATS))
    assert verdict == "cached", (
        f"{kind} {name} is not a credential but evaluates {verdict!r}. A rule that "
        "bypasses everything proves nothing."
    )


def test_anonymous_public_api_still_caches():
    """The positive control. Measured HIT age=480 on 2026-09-07."""
    assert cfx.disposition(RULES, STATS)[0] == "cached"


def test_self_check_passes_on_the_pinned_canon():
    assert guard.self_check(cfx, RULES) == []


def test_self_check_catches_a_rule_that_bypasses_everything():
    """M2 from the mutation run: rule 24 degraded to `starts_with(path,"/api/")`."""
    mutated = [dict(r) for r in RULES]
    for rule in mutated:
        if rule["id"].startswith("b3ce82fb"):
            rule["expression"] = 'starts_with(http.request.uri.path, "/api/")'
    assert guard.self_check(cfx, mutated), "a path-only rule 24 must fail the self-check"


def test_unparseable_rule_is_never_read_as_coverage():
    """'I could not read this rule' must not become 'this rule does not apply'."""
    mutated = [dict(r) for r in RULES]
    for rule in mutated:
        if rule["id"].startswith("b3ce82fb"):
            rule["expression"] = 'starts_with(http.request.uri.path, "/api/") and (((('
    verdict, _, _ = cfx.disposition(mutated, guard._request_for(cfx, "header", "x-api-key", STATS))
    assert verdict == "unknown"
    assert guard.self_check(cfx, mutated), "an unparseable rule must fail the self-check"


# ── 3. the classifier ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("x-api-key", True), ("authorization", True), ("dchub_session", True),
    ("x-internal-cron", True), ("dch_sid", True), ("admin_key", True),
    ("max_tokens", False),      # an LLM sampling parameter; only matches on "token"
    ("user-agent", False), ("referer", False), ("cf-connecting-ip", False),
    ("stripe-signature", False),  # POST-only webhook signature, never GET-cacheable
])
def test_credential_classification(name, expected):
    assert guard._is_credential(name) is expected


def test_response_headers_are_not_request_channels():
    """`response.headers.get("x-trial-key")` reads a header we SEND. Counting it
    would demand a bypass for a channel nobody authenticates to us with."""
    found, files, _ = guard.scan_channels(ROOT, guard._load("_rtc_t", "check_route_table_coherence.py"))
    assert files >= guard.MIN_FILES_SCANNED
    assert ("header", "x-trial-key") not in found


def test_scan_finds_the_credentials_that_are_actually_there():
    found, _, _ = guard.scan_channels(ROOT, guard._load("_rtc_t2", "check_route_table_coherence.py"))
    for channel in [("header", "x-admin-key"), ("cookie", "dchub_session"), ("arg", "admin_key")]:
        assert channel in found, f"{channel} is read in the route files but the scan missed it"


# ── 4. the shared evaluator's ANONYMOUS contract must not have moved ─────────
def test_bare_path_string_still_means_the_anonymous_request():
    node = cfx.parse('any(http.request.headers["x-api-key"][*] != "")')
    assert cfx.evaluate(node, "/api/v1/stats") is False
    assert cfx.evaluate(node, cfx.Request("/api/v1/stats")) is False
    assert cfx.evaluate(node, cfx.Request("/api/v1/stats", headers=["x-api-key"])) is True


def test_parser_retains_the_subscript_name():
    """Discarding it is what made headers["x-api-key"] and headers["x-admin-token"]
    indistinguishable — the bug this guard exists to make impossible."""
    a = cfx.parse('any(http.request.headers["x-api-key"][*] != "")')
    req = cfx.Request("/api/v1/stats", headers=["x-admin-token"])
    assert cfx.evaluate(a, req) is False, "a rule naming x-api-key must not fire for x-admin-token"


def test_cookie_families_match_the_raw_cookie_header():
    """`http.cookie contains "token"` matches auth_token because the field is the
    raw header string. That is real Cloudflare behaviour, modelled not idealised."""
    node = cfx.parse('http.cookie contains "token"')
    assert cfx.evaluate(node, cfx.Request("/x", cookies=["auth_token"])) is True
    assert cfx.evaluate(node, cfx.Request("/x", cookies=["dch_sid"])) is False


def test_header_matching_is_case_insensitive():
    node = cfx.parse('any(http.request.headers["x-api-key"][*] != "")')
    assert cfx.evaluate(node, cfx.Request("/x", headers=["X-API-Key"])) is True


# ── 5. the guard actually runs and passes on this tree ───────────────────────
def test_guard_exits_zero_on_the_current_tree():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_credential_channel_coverage.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, f"guard failed:\n{proc.stdout}\n{proc.stderr}"
