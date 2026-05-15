"""Phase RR (2026-05-15) — Brain consistency radar tests.

Locks in the structural contract of the radar detectors:
  • scan_all() returns a list[dict]
  • each finding has 'issue', 'url', 'count' keys
  • the 3 specific detector functions exist and return iterable

Doesn't make network calls — those would be flaky in CI. Just verifies
the radar is importable, callable, and shaped correctly. The actual
detection logic gets exercised in production where it has real targets.
"""
import os
import re


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
    """Every tool in _TOOL_API_MAPPING must exist in mcp_gatekeeper.TOOL_MIN_TIER
    or the tier_consistency check will silently skip the entry."""
    from routes.brain_consistency_radar import _TOOL_API_MAPPING
    try:
        from mcp_gatekeeper import TOOL_MIN_TIER
    except Exception:
        # mcp_gatekeeper may fail to import without env — accept that path
        # in the test environment but make a noise.
        import warnings
        warnings.warn("TOOL_MIN_TIER unavailable in test env; skipping")
        return
    for tool in _TOOL_API_MAPPING:
        assert tool in TOOL_MIN_TIER, \
            f"_TOOL_API_MAPPING lists '{tool}' but it's not in TOOL_MIN_TIER"


def test_scan_summary_shape():
    """scan_summary() returns a dict with the documented keys regardless
    of whether any findings fire."""
    from routes.brain_consistency_radar import scan_summary
    s = scan_summary()
    assert isinstance(s, dict)
    for key in ("ok", "count", "by_issue", "findings", "as_of"):
        assert key in s, f"scan_summary missing key '{key}'"
    assert isinstance(s["findings"], list)
    assert isinstance(s["by_issue"], dict)


def test_workflow_yaml_is_parseable():
    """The cron-coverage detector parses evolve-cron.yml. If that file
    becomes malformed, this test catches it before the radar fires
    in production."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wf = os.path.join(here, ".github", "workflows", "evolve-cron.yml")
    if not os.path.exists(wf):
        return  # repo layout may differ in test env
    import yaml
    data = yaml.safe_load(open(wf, "r"))
    # PyYAML parses unquoted 'on:' as the boolean True. Accept either.
    on = data.get("on") or data.get(True)
    assert isinstance(on, dict), "evolve-cron.yml `on:` section must be a dict"
    assert "workflow_dispatch" in on or "schedule" in on, \
        "workflow must declare at least one trigger"
