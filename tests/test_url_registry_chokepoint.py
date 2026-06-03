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
ALLOWLIST = {
    "url_registry.py",
    "marketing_engine.py",
    "dchub_media_hub.py",
    "linkedin_quad_daily.py",
    "linkedin_partnership_weekly.py",
    "linkedin_best_of_day.py",
    "partnership_press_template.py",
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
