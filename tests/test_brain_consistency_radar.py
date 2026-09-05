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
