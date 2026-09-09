"""The Cloudflare Worker states tier prices. They must equal tier_registry's.

WHY THIS EXISTS. worker.js is the EDGE, and it is the last surface anyone
thinks of when a price changes — it is JavaScript, deployed on its own path,
and it cannot import tier_registry the way the ten Python modules that read
`tier_registry.price()` can. So it restates prices as literals, and on
2026-09-08 it held SIX of them, all saying Pro is $299:

  worker.js:618   <b>Pro ($299/mo)</b>            HTML tier card
  worker.js:622   >$299 Pro</a>                   upgrade button
  worker.js:1035  "… · $299/mo Pro."              unlock_more_data description
  worker.js:2480  pro: '$299/mo — 2,000 calls/day + Pro tools (…)'
  worker.js:2555  pro: '$299/mo — 2,000 calls/day + Pro tools'
  worker.js:3223  pro: '$299/mo — 2,000 calls/day + Pro tools (…)'

Pro has been $99 since r-price-collapse (owner call, 2026-09-05). The 2480
copy is the one served on /.well-known/mcp.json — the discovery manifest
agents and registries scrape — so the edge was still publishing $299 after
the backend routes, the MCP envelope and every manifest had been corrected.
That is the whole shape of this defect: a surface nobody remembers is a
surface that stays wrong the longest.

Python cannot make worker.js read canon, but it CAN read both and refuse to
let them disagree. That is what this does.

★ COMMENT LINES ARE EXEMPT, deliberately. worker.js carries dated changelog
entries describing the May 2026 $199/$299 incident. Those are history and
history stays true — the same exemption smithery-canon-guard makes.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tier_registry  # noqa: E402

_WORKER = os.path.join(os.path.dirname(__file__), "..", "worker.js")

# ★ FLOOR. This guard finds price statements by pattern. A pattern that stops
# matching — a refactor, a renamed key, a template literal — makes it green on
# zero statements, which is the day it stops protecting the edge. Six is what
# shipped; the floor is five so a legitimate removal does not wedge CI.
_MIN_STATEMENTS = 5

_TIERS = ("starter", "developer", "pro")

_PATTERNS = (
    # pro: '$99/mo — …'   /   developer: `$49/mo — …`
    re.compile(r"\b(starter|developer|pro)\s*:\s*[`'\"]\$(\d+)/mo"),
    # "… · $99/mo Pro."   (the unlock_more_data description)
    re.compile(r"\$(\d+)/mo\s+(Starter|Developer|Pro)\b"),
    # <b>Pro ($99/mo)</b>
    re.compile(r"<b>(Starter|Developer|Pro)\s*\(\$(\d+)/mo\)"),
    # >$99 Pro</a>
    re.compile(r">\$(\d+)\s+(Starter|Developer|Pro)<"),
)


def _statements():
    """(lineno, tier, dollars) for every price statement in CODE, not comments."""
    out = []
    with open(_WORKER, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            stripped = line.lstrip()
            if stripped.startswith(("//", "*", "/*")):
                continue          # history stays true
            for pat in _PATTERNS:
                for m in pat.finditer(line):
                    a, b = m.group(1), m.group(2)
                    tier, amount = (a, b) if a.lower() in _TIERS else (b, a)
                    out.append((i, tier.lower(), int(amount)))
    return out


def test_the_scan_still_finds_the_price_statements():
    """The floor. Without it the next test is green on an empty match set."""
    found = _statements()
    assert len(found) >= _MIN_STATEMENTS, (
        "only %d price statements found in worker.js (floor %d). Either the "
        "shapes changed and this guard now protects nothing, or the edge "
        "stopped stating prices. Both need a human." % (len(found), _MIN_STATEMENTS)
    )


def test_every_worker_price_equals_canon():
    bad = []
    for lineno, tier, dollars in _statements():
        want = tier_registry.price(tier)
        if want != dollars:
            bad.append("worker.js:%d  %s $%d  (tier_registry: $%s)"
                       % (lineno, tier, dollars, want))
    assert not bad, (
        "the Cloudflare Worker states a price that is not canon:\n  "
        + "\n  ".join(bad) +
        "\nworker.js serves /.well-known/mcp.json — the manifest agents and "
        "registries scrape — so a stale price here outlives every corrected "
        "Python surface. Update the literal to tier_registry.price()."
    )
