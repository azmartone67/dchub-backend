"""The zone-Cache-Rule canary must keep asserting on a signal that can fail.

2026-09-07. Three layers keep /api/v1/mcp/tools/* out of the CF edge: the origin
gate (#4038), the worker's ROUTE_CACHE_MAP entry (dchub-frontend#1409), and a
ZONE Cache Rule (#1413). Only the first two are in git. The zone rule lives in
the Cloudflare dashboard, nothing in CI reads the zone ruleset, and it was
silently broken within the hour — reordered from last to first, which in the
cache phase means overridden by the rule after it.

scripts/probe_mcp_tools_cache_bypass.sh is the only thing that would notice, so
these tests fence its DESIGN, not its output:

  * It must ride a CACHEABLE 200 on the guarded prefix. The tempting probe —
    "export_facility_csv returns 401 to anon" — reads cf-cache-status: BYPASS
    whether or not the zone rule exists, because 4xx is never cached. That probe
    was green through the entire hour the rule was inverted.
  * It must keep a CONTROL that still caches. "Nothing on the prefix is cached"
    also passes if someone bypasses the zone globally, which would be a large
    silent performance regression.
  * It must cache-bust, or a leftover entry masks a missing rule from run 2 on.
"""
import os
import re
import stat

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "probe_mcp_tools_cache_bypass.sh")


@pytest.fixture(scope="module")
def src():
    with open(SCRIPT) as fh:
        return fh.read()


def test_canary_exists_and_is_executable():
    assert os.path.exists(SCRIPT), "the zone Cache Rule has no canary again"
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR, f"{SCRIPT} is not executable"


def test_it_rides_a_cacheable_200_not_the_gated_4xx(src):
    """A 4xx is never cached, so probing the gated route proves nothing."""
    m = re.search(r'^GATED_PATH="([^"]+)"', src, re.M)
    assert m, "GATED_PATH is gone"
    path = m.group(1)
    assert path.startswith("/api/v1/mcp/tools/"), (
        f"GATED_PATH {path!r} is not on the prefix the zone rule covers"
    )
    assert "export_facility_csv" not in path, (
        "GATED_PATH points at the gated export, which answers 401 to anon. A "
        "4xx is not cached in the first place, so its cf-cache-status reads "
        "BYPASS with or without the zone rule — that is the exact probe that "
        "stayed green while the rule was inverted on 2026-09-06."
    )


def test_it_keeps_a_control_outside_the_guarded_prefix(src):
    """Without a control, a zone-wide bypass passes as success."""
    m = re.search(r'^CONTROL_PATH="([^"]+)"', src, re.M)
    assert m, "the control probe was removed — a global bypass would now pass"
    ctl = m.group(1)
    assert not ctl.startswith("/api/v1/mcp/tools/"), (
        f"CONTROL_PATH {ctl!r} is INSIDE the bypassed prefix, so it can only "
        f"ever confirm the bypass — it is not a control"
    )


def test_cached_statuses_are_not_treated_as_success(src):
    """MISS/HIT/EXPIRED all mean the response entered the cache."""
    m = re.search(r'^UNCACHED_OK="([^"]+)"', src, re.M)
    assert m, "UNCACHED_OK is gone"
    ok = set(m.group(1).split())
    assert ok, "UNCACHED_OK is empty — every status would pass"
    for bad in ("MISS", "HIT", "EXPIRED", "REVALIDATED", "STALE"):
        assert bad not in ok, (
            f"{bad} counted as 'not cached'; it means the response WAS cached"
        )
    assert ok <= {"DYNAMIC", "BYPASS"}, f"unexpected pass-statuses: {sorted(ok)}"


def test_it_cache_busts_every_run(src):
    """A fixed URL caches on run 1 and masks a missing rule from run 2 on."""
    m = re.search(r'^Z="\$HOST\$GATED_PATH\?([^"]+)"', src, re.M)
    assert m, "the guarded probe URL is no longer cache-busted"
    assert "$(date" in m.group(1) or "$RANDOM" in m.group(1), (
        "the guarded probe URL has no per-run component"
    )


def test_transport_failure_is_not_reported_as_a_rule_verdict(src):
    """DNS/network down must not read as 'the rule is gone' — or as success."""
    assert "exit 2" in src, (
        "no distinct exit for 'could not measure'; a network blip would be "
        "reported as a verdict on the rule"
    )
    assert "COULD NOT MEASURE" in src


def test_the_fix_is_named_in_the_failure_message(src):
    """A canary that fires without saying what to do gets muted."""
    assert "last match wins" in src.lower() or "LAST" in src, (
        "the failure message does not explain that the cache phase is "
        "last-match-wins — the single fact that made this break"
    )
