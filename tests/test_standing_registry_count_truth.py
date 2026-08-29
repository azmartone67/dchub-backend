"""'Listed on N MCP registries' must not count DC Hub's own source repo.

★2026-08-28. CONFIRMED_REGISTRIES carries a "Source (GitHub)" row pointing at
github.com/azmartone67/dchub-mcp-server — our own repo. registries_count and
the summary string were both len() over that list, so the API published
"Listed on 9 MCP registries" where one ninth of the social proof was us
publishing ourselves. A repo you publish is not a directory that listed you.

It stayed invisible because our two surfaces were wrong in OPPOSITE directions,
so neither looked wrong beside the other:

    /api/v1/mcp/standing    "Listed on 9 MCP registries"   +1 (this row)
    dchub-frontend/ai.html  "7 registries live"            -1 (omits GitHub
                                                              MCP Registry)

The honest count is EIGHT. github.com/mcp?query=dchub does return our entry
(10 matches; a control query returns none), so the frontend's 7 is an
undercount, not a stricter standard.

★The row is KEPT, not deleted: test_mcp_standing_page pins its name because an
owner asked for the link. The ask was that it be PUBLISHED, not that it be
counted as social proof — so is_registry=False excludes it from the count while
the link stays. registries_count is therefore deliberately != len(registries).

★The URL check is EXACT (host + path), never a substring: `azmartone67/
dchub-mcp-server` also appears inside Glama's listing URL, so a substring guard
flags Glama and passes for the wrong reason. That false positive happened while
writing this file.
"""
from __future__ import annotations

import sys
import unittest.mock as m
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import routes.mcp_standing as ms                      # noqa: E402
from routes.mcp_standing import CONFIRMED_REGISTRIES  # noqa: E402

OWN_REPO = "https://github.com/azmartone67/dchub-mcp-server"
_OWN = urlparse(OWN_REPO)


def _is_own_repo(url: str) -> bool:
    u = urlparse(url)
    return (u.netloc.lower() == _OWN.netloc.lower()
            and u.path.rstrip("/").lower() == _OWN.path.rstrip("/").lower())


def _standing():
    with m.patch.object(ms, "_verified_map", lambda: {}), \
         m.patch.object(ms, "_tools_count", lambda: 82):
        return ms._standing()


def test_our_own_repo_is_not_counted_as_a_registry():
    """THE guard: no COUNTED row may be our own repo."""
    offenders = [r["registry"] for r in CONFIRMED_REGISTRIES
                 if ms._is_registry(r) and _is_own_repo(r["url"])]
    assert not offenders, (
        f"{offenders} point at DC Hub's own source repo and are counted in "
        f"'Listed on N MCP registries' — publishing a repo is not being listed")


def test_the_count_is_eight_and_the_summary_agrees():
    d = _standing()
    assert d["registries_count"] == 8
    assert "Listed on 8 MCP registries" in d["summary"], d["summary"]


def test_the_count_is_deliberately_not_the_row_count():
    """registries_count != len(registries) is the POINT, not a bug: the source
    link is published as a row and excluded from the count."""
    d = _standing()
    assert len(d["registries"]) == 9
    assert d["registries_count"] == 8


def test_the_owner_requested_source_link_is_still_published():
    """The fix must not satisfy the count by deleting what an owner asked for
    (tests/test_mcp_standing_page.py pins this name)."""
    assert any(r["registry"] == "Source (GitHub)" for r in CONFIRMED_REGISTRIES)
    assert any(r["registry"] == "Source (GitHub)"
               for r in _standing()["registries"])


def test_the_exact_matcher_does_not_flag_glama():
    """CONTROL for the matcher. Glama's URL CONTAINS the repo path; a substring
    guard flags it and then 'passes' for the wrong reason."""
    glama = "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server"
    assert "azmartone67/dchub-mcp-server" in glama, "premise: substring present"
    assert not _is_own_repo(glama)
    assert _is_own_repo(OWN_REPO)


def test_must_fail_control_counting_the_source_row_is_caught():
    """CONTROL: flip the row back to counted (the shipped behaviour) and assert
    both the guard and the count notice. Without this, the guard could pass
    because the matcher never matches anything."""
    patched = [dict(r, is_registry=True) if r["registry"] == "Source (GitHub)"
               else r for r in CONFIRMED_REGISTRIES]
    offenders = [r["registry"] for r in patched
                 if ms._is_registry(r) and _is_own_repo(r["url"])]
    assert offenders == ["Source (GitHub)"], \
        f"guard cannot see the defect it exists for; saw {offenders}"
    assert sum(1 for r in patched if ms._is_registry(r)) == 9
