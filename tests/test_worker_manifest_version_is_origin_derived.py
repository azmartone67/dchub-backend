"""Every manifest surface must answer with the ORIGIN's version, not a literal.

THE DEFECT, measured live 2026-09-06, the day dchub-mcp-server shipped 2.12.8.
`Pragma: no-cache` on every read, because the zone caches these for an hour:

    /.well-known/mcp.json              2.12.8  tools_count=85  desc: —
    /.well-known/mcp/server-card.json  2.12.8  tools_count=85  desc: —
    /.well-known/agent.json            2.12.8                  desc: —
    /mcp/manifest                      2.5.0   tools_count=85  desc: "83 tools"
    /mcp/manifest.json                 2.5.0   tools_count=85  desc: "83 tools"

★ WHY IT SURVIVED. v4.9.45 moved the three .well-known surfaces onto the
origin's canon and its own comment stated the rule: "Fixing only mcp.json would
have created a NEW split-brain — two well-known surfaces on the same zone
disagreeing about the server's own version." /mcp/manifest was missed, so the
split-brain moved one path further out instead of ending. A prose rule that
nothing executes is a rule that holds until the next handler is written.

★ WHY IT LOOKED HEALTHY. `tools_count` had ALREADY been put on the live
tools/list (v4.9.29), so a single JSON body asserted 85 tools in one field, "83
tools" in its description, and version 2.5.0 — seven minor releases behind. The
live half is what makes the dead half credible: nothing in the response reads as
unmaintained, so no consumer has a reason to doubt the number.

WHAT THIS ASSERTS. Not the version — that is the origin's to move, and pinning
it here would just relocate the hardcoding into the test. It asserts the
BINDING: every `version:` in a served body that falls back to
MCP_SERVER_INFO.version must prefer an origin-derived value first, by either

    version: <x>Extras.version || MCP_SERVER_INFO.version   (server-card, agent,
                                                             /mcp/manifest)
    version: MCP_SERVER_INFO.version, ... ...<x>Extras      (/.well-known/mcp.json,
                                                             spread wins, and its
                                                             comment says keep it last)

Both are legitimate. A bare literal with neither is the bug.

mcpOriginVersion() is exempt BY NAME, not by pattern: it is the offline
fallback itself, so `return v || MCP_SERVER_INFO.version` there is the correct
shape and must not be mistaken for a served surface.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "worker.js")

# The whole point of the guard is that this set GROWS. A new manifest handler
# that hardcodes the version reds here without anyone remembering to come back.
_MIN_SERVED_VERSION_SITES = 4


def _src():
    with open(WORKER, encoding="utf-8") as fh:
        return fh.read()


def _fallback_span(src):
    """Line range of mcpOriginVersion — the sanctioned literal fallback."""
    start = src.index("async function mcpOriginVersion")
    # to the end of that function: the next top-level `const ` declaration.
    end = src.index("\nconst MCP_SERVER_INFO", start)
    return (src.count("\n", 0, start), src.count("\n", 0, end))


def _served_version_sites(src):
    """(lineno, text) for each `version:` in a response body, fallback excluded."""
    lo, hi = _fallback_span(src)
    sites = []
    for i, line in enumerate(src.splitlines()):
        if lo <= i <= hi:
            continue
        if re.match(r"\s*version:\s*.*MCP_SERVER_INFO\.version", line):
            sites.append((i + 1, line.strip()))
    return sites


def _spread_follows(src, lineno):
    """Does this object literal end with a `...<x>Extras` merge?

    Bounded to the enclosing JSON.stringify body so a spread in the NEXT
    handler can never vouch for this one.
    """
    lines = src.splitlines()
    for line in lines[lineno:]:
        if re.match(r"\s*\}, null, 2\)", line):
            return False
        if re.match(r"\s*\.\.\.\w*[Ee]xtras,", line):
            return True
    return False


def test_the_scan_actually_found_the_surfaces():
    """A guard that matches nothing reports green. Prove it observed something."""
    sites = _served_version_sites(_src())
    assert len(sites) >= _MIN_SERVED_VERSION_SITES, (
        f"only {len(sites)} served `version:` sites found in worker.js — the "
        f"pattern has drifted and this guard is now inspecting nothing")


def test_every_served_version_prefers_the_origin():
    """THE DEFECT: /mcp/manifest answered 2.5.0 while its siblings said 2.12.8."""
    src = _src()
    bare = [
        (ln, txt) for ln, txt in _served_version_sites(src)
        if "Extras.version ||" not in txt and not _spread_follows(src, ln)
    ]
    assert not bare, (
        "worker.js publishes a HARDCODED MCP_SERVER_INFO.version on "
        + ", ".join(f"line {ln}" for ln, _ in bare)
        + ". Every served manifest must prefer the origin's version — use "
        "`<x>Extras.version || MCP_SERVER_INFO.version`, or end the body with "
        "`...<x>Extras`. Measured 2026-09-06: /mcp/manifest served 2.5.0 next "
        "to a live tools_count of 85.\n"
        + "\n".join(f"  {ln}: {txt}" for ln, txt in bare))


def test_the_manifest_handler_resolves_extras_at_all():
    """The binding above is unreachable if nothing fetches the extras."""
    src = _src()
    handler = src.index("pathname === '/mcp/manifest'")
    body = src[handler:src.index("X-DC-Manifest-Source", handler)]
    assert "resolveManifestExtras(" in body, (
        "/mcp/manifest never calls resolveManifestExtras — `_mExtras.version` "
        "would be undefined and silently fall through to the 2.5.0 literal, "
        "which is exactly the bug, restored")


def test_the_offline_fallback_is_still_reachable():
    """Fail-open by contract: an unreadable origin must not blank the field.

    Non-vacuous — it would break if someone 'fixed' the guard by deleting the
    literal instead of binding the origin in front of it.
    """
    src = _src()
    lo, hi = _fallback_span(src)
    window = "\n".join(src.splitlines()[lo:hi + 1])
    assert "MCP_SERVER_INFO.version" in window, (
        "mcpOriginVersion no longer falls back to the literal — an origin "
        "timeout would now publish an empty version")
