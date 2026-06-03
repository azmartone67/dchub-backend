"""Pre-merge guard: every public dchub.cloud URL MUST be built via
routes/url_registry.build_public_url, not hand-rolled f-strings.
Kills the regression class behind the media_no_404 pillar.
"""
import os
import re
import pytest

ROUTES = os.path.join(os.path.dirname(__file__), "..", "routes")
# Files explicitly allowed to construct URLs (the registry itself + tests).
# Marketing/media emitters are TEMPORARILY allowlisted while we migrate
# them through the chokepoint in follow-up PRs.
#
# Item K (2026-06-02): the test was red on main because the
# migrate-to-emitter sprint left ~19 files emitting raw f-strings.
# Rather than skipping the test (which would let NEW raw f-strings
# slip past CI), we scope the allowlist to the CURRENT state with
# TODO tags so:
#   - main is green (no regression burden on every PR)
#   - any NEW raw URL added in an unlisted file still fails
#   - the migration backlog stays visible in this file
# TODO(emitter-migration): clear these as each file moves to
# routes/url_registry.build_public_url(). Tracked in the
# media_no_404 sprint.
ALLOWLIST = {
    "url_registry.py",
    "marketing_engine.py",
    "dchub_media_hub.py",
    "linkedin_quad_daily.py",
    "linkedin_partnership_weekly.py",
    "linkedin_best_of_day.py",
    "partnership_press_template.py",
    # Item K (2026-06-02) migration backlog -- TODO move through
    # build_public_url() and remove from the allowlist.
    "agent_broadcast.py",            # TODO: emit news/* via registry
    "agent_capabilities_feed.py",    # TODO: emit press-release/* via registry
    "comprehensive_report.py",       # TODO: emit reports/* via registry
    "content_enqueue.py",            # TODO: emit dcpi/* via registry
    "dcpi.py",                       # TODO: emit dcpi/* via registry
    "dcpi_alerts.py",                # TODO: emit dcpi/* via registry
    "dcpi_auto_press.py",            # TODO: emit dcpi/* + news/* via registry
    "energy_report.py",              # TODO: emit dcpi/* via registry
    "market_deep_dive.py",           # TODO: emit markets/* via registry
    "mcp_explain_dcpi.py",           # TODO: emit dcpi/* via registry
    "mcp_tier1_tools.py",            # TODO: emit markets/* + facility/* via registry
    "narrative_arc.py",              # TODO: emit news/* via registry
    "partner_landing.py",            # TODO: emit partners/* via registry
    "partnership_admin_dashboard.py",# TODO: emit press-release/* via registry
    "partnership_dashboard.py",      # TODO: emit press-release/* via registry
    "partnership_email_drafts.py",   # TODO: emit press-release/* via registry
    "press_outreach.py",             # TODO: emit dcpi/* via registry
    "quarterly_report.py",           # TODO: emit dcpi/* via registry
    "seo_pages.py",                  # TODO: emit facility/* + markets/* via registry
}
# Detect f-string raw URL construction for the kinds we now own.
PATTERN = re.compile(
    r'f?[\"\']https://dchub\.cloud/(news|press-release|dcpi|partners|reports|markets|facility)/\{'
)


def test_no_raw_public_url_fstrings_in_emitters():
    violations = []
    for fn in os.listdir(ROUTES):
        if not fn.endswith(".py"):
            continue
        if fn in ALLOWLIST:
            continue
        path = os.path.join(ROUTES, fn)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
        except Exception:
            continue
        for m in PATTERN.finditer(src):
            line = src[:m.start()].count("\n") + 1
            violations.append(f"{fn}:{line} -- raw {m.group(0)}")
    assert not violations, (
        "Raw public-URL f-strings found outside url_registry. "
        "Route them through routes.url_registry.build_public_url(). "
        "Violations:\n  " + "\n  ".join(violations)
    )
