"""Every module that names a Glama listing must name the SAME, CORRECT one.

★ WHAT WENT WRONG. Four modules carried a Glama URL and they disagreed:

    mcp_presence_crawler   /connectors/cloud.dchub/dc-hub-data-center-…  the DUPLICATE
    agent_broadcast_loop   /mcp/servers/dchub                            301 -> a SEARCH page
    mcp_registry_outreach  /connectors/cloud.dchub/mcp-server            correct
    mcp_registry_watch     /servers/azmartone67/dchub-mcp-server         correct (repo half)

The presence-crawler seed had been inverted since 2026-07-09 by a note claiming
`cloud.dchub/mcp-server` was "a Glama-flagged deprecated DUPLICATE". Measured
2026-09-05, the opposite is true: mcp-server is the Healthy, ownership-verified
connector MIRRORED FROM THE OFFICIAL REGISTRY, and dc-hub-data-center-… is the
Glama-native card that was Unhealthy since 09-01 and has now been deprecated.

★ Why it mattered: white_glove_propagation reads that seed. So the one Glama
surface white-glove has ever kept honest was the duplicate, while the card
installers actually land on went unwatched for two months. A wrong URL does not
fail — it succeeds against the wrong thing, which is why nothing caught it.

★ The distinguishing test is the OFFICIAL REGISTRY, not the health badge on the
day you looked: `?search=cloud.dchub` returns exactly one name, and a
Glama-native card can be green while not being the listing aggregators mirror.

Hermetic: reads committed source only. No network — a probe would tell you both
URLs return 200, which is exactly what made this invisible.
"""
import re

import pytest

KEEPER = "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server"

# The Glama-native card the owner deprecated 2026-09-05. Naming it is the point:
# an omission cannot be asserted against.
DUPLICATE_SLUG = "dc-hub-data-center-intelligence-mcp-server"

# 301s to /mcp/servers?query=author%3Adchub — a search page, not a listing.
DEAD_URL = "https://glama.ai/mcp/servers/dchub"

# The repo half of the keeper entity. Legitimate: it and the connector link to
# each other; it is simply a different surface, not a different product.
REPO_LISTING = "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server"

MODULES = [
    "routes/mcp_presence_crawler.py",
    "routes/agent_broadcast_loop.py",
    "routes/mcp_registry_outreach.py",
    "routes/mcp_registry_watch.py",
    "routes/mcp_standing.py",
]

_URL_RE = re.compile(r"https://glama\.ai/mcp/(?:connectors|servers)/[A-Za-z0-9._/-]+")


def _urls(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except FileNotFoundError:
        pytest.skip(f"{path} not present")
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line.lstrip()
        # Comments RECORD history on purpose — the corrected seed note names the
        # duplicate precisely so the next reader can tell them apart. Only live
        # string values are the contract.
        if stripped.startswith("#"):
            continue
        out.extend(f"{path}:{i}:{u}" for u in _URL_RE.findall(line))
    return out


def test_the_scanner_actually_finds_urls():
    """A scan that matches nothing passes vacuously."""
    found = [u for m in MODULES for u in _urls(m)]
    assert len(found) >= 4, found


@pytest.mark.parametrize("module", MODULES)
def test_no_module_points_at_the_deprecated_duplicate(module):
    offenders = [u for u in _urls(module) if DUPLICATE_SLUG in u]
    assert offenders == [], (
        "points at the Glama-native duplicate, deprecated 2026-09-05. The "
        "keeper is the official-registry mirror at " + KEEPER)


@pytest.mark.parametrize("module", MODULES)
def test_no_module_points_at_the_url_that_redirects_to_a_search(module):
    offenders = [u for u in _urls(module) if u.rstrip("/").endswith("/mcp/servers/dchub")]
    assert offenders == [], (
        f"{DEAD_URL} 301s to a search page; a probe against it reads a generic "
        f"result set as though it were our listing")


def test_every_glama_url_is_one_of_the_two_real_surfaces():
    """Connector keeper or its repo half — nothing else is ours."""
    allowed = {KEEPER, REPO_LISTING}
    stray = []
    for module in MODULES:
        for entry in _urls(module):
            url = entry.split(":", 2)[2].rstrip('",/')
            # Submit/new endpoints are actions, not listings.
            if url.endswith(("/new", "/submit")):
                continue
            if url not in allowed:
                stray.append(entry)
    assert stray == [], stray
