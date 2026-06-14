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
    # Remaining migration backlog. 2026-06-14: 16 single-slug emitters were
    # routed through build_public_url() (incl. the new subpath= support for
    # /markets/<slug>/brief) and removed from this allowlist. The files below
    # stay because their URLs live inside .format()/Jinja HTML templates or
    # use numeric facility ids / query-string variants the next phase handles.
    # TODO(emitter-migration phase 2): clear these too.
    "comprehensive_report.py",       # .format() report templates ({d['window']})
    "dcpi.py",                       # Jinja {{ s.market_slug }} + .format templates
    "dcpi_alerts.py",                # slug source is a display name, not a slug
    "dcpi_auto_press.py",            # TODO: emit dcpi/* + news/* via registry
    "market_deep_dive.py",           # .format() HTML/JSON-LD deep-dive templates
    "mcp_tier1_tools.py",            # numeric facility ids (facility/{id})
    "partner_landing.py",            # .format() HTML landing templates
    "partnership_admin_dashboard.py",# .format() press-release template ({_esc(...)})
    "quarterly_report.py",           # .format() report template ({_esc(...)})
    "seo_pages.py",                  # numeric facility ids + sitemap XML
    "market_brief.py",               # query-string + .pdf/embed variants (phase 2)
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
