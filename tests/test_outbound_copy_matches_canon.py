"""tests/test_outbound_copy_matches_canon.py — what we publish must match canon
(2026-08-12).

OUTBOUND COPY is the set of files whose contents get pasted into third-party
listings: the registry submission drafts, the directory manifests, glama.json
(which Glama AUTO-DISCOVERS) and server.json. They are public claims with a
longer half-life than any page we serve, because once a marketplace ingests a
number we cannot edit it.

Every one of them had drifted:

  facilities   21,000+ / 22,000+ / 20,000+   live: 17,425
  tools        38 / 23+ / 25 / 53            canon: 82
  M&A          2,000+                        canon: 1,600+

ai_surface_canon:139 already recorded WHY the facility number is dangerous —
"22,000+ ... floored RAW discovered_facilities ROWS", and after dedup landed it
"silently became a ~1.7x over-claim ... and every downstream consumer (registry
submitters, description builders, white-glove)". This is that consumer. The
canon was corrected on 2026-07-24; the submission drafts never were, because
surface_truth fences only /llms.txt, /llms-full.txt, /agent and /ai.

★DIRECTION MATTERS AND THE TWO SIDES ARE NOT SYMMETRIC.
An inflated facility count is a false claim about the product. A deflated tool
count is not dishonest — it just sells 82 tools as 23. Both are failures, so
both are asserted, but only the first is an integrity problem.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_outbound_copy_matches_canon.py -v
"""
from __future__ import annotations

import glob
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _outbound() -> list:
    files = (sorted(glob.glob(str(_ROOT / "PATCHES/REGISTRY_SUBMISSIONS_r45/*.md")))
             + sorted(glob.glob(str(_ROOT / "PATCHES/MCP_DIRECTORIES/*.json")))
             + [str(_ROOT / p) for p in
                ("PATCHES/ANTHROPIC_CONNECTOR_SUBMISSION.md",
                 "PATCHES/mcp-proxy-add-manifest.md",
                 "REGISTRY_SUBMISSIONS.md", "glama.json", "server.json")])
    return [pathlib.Path(f) for f in files if pathlib.Path(f).exists()]


def _canon_facility_floor() -> int:
    """The published floor, as an int, from the single source of truth."""
    import importlib
    c = importlib.import_module("ai_surface_canon")
    raw = (c.PINNED.get("public") or {}).get("facilities") or ""
    m = re.search(r"([\d,]+)", str(raw))
    assert m, "ai_surface_canon PINNED public.facilities is unreadable"
    return int(m.group(1).replace(",", ""))


def _canon_tools() -> int:
    import importlib
    return int(importlib.import_module("ai_surface_canon").PINNED["tools_advertised"])


def test_there_is_outbound_copy_to_check():
    """★A sweep that silently matches nothing is the vacuous guard this repo
    keeps rediscovering."""
    assert len(_outbound()) >= 10, \
        "expected the outbound-copy set, found %d files" % len(_outbound())


@pytest.mark.parametrize("path", _outbound(), ids=lambda p: p.name)
def test_no_facility_claim_above_canon(path):
    """★THE INTEGRITY ONE. A number larger than canon is a false public claim,
    and a marketplace that ingests it will keep serving it after we correct."""
    floor = _canon_facility_floor()
    text = path.read_text(encoding="utf-8", errors="ignore")
    bad = []
    # ★(?<![\d,]) is load-bearing. Without it this matched "45,000+" INSIDE
    # "445,000+ MCP tool calls logged since April" and reported a call count as
    # a facility over-claim. A guard that invents a violation gets switched off.
    for m in re.finditer(r"(?<![\d,])(\d{2},\d{3})\+", text):
        n = int(m.group(1).replace(",", ""))
        if n <= floor:
            continue
        # ★And the number must actually be about FACILITIES. This repo publishes
        # much larger honest counts for other entities — 126k substations, 94k
        # transmission lines, 55k fiber routes, 30k gas segments — and flagging
        # those as facility over-claims would be the same false positive with a
        # different subject.
        ctx = text[max(0, m.start() - 90): m.end() + 90].lower()
        if any(w in ctx for w in ("facilit", "data cent", "data-cent", "site")):
            bad.append(m.group(0))
    assert not bad, (
        "%s claims %s facilities; canon floor is %s+. ai_surface_canon:139 "
        "records the 22,000+ figure as a ~1.7x over-claim after dedup — do not "
        "reintroduce it." % (path.name, ", ".join(sorted(set(bad))),
                             f"{floor:,}"))


@pytest.mark.parametrize("path", _outbound(), ids=lambda p: p.name)
def test_no_tool_count_below_canon(path):
    """Not an integrity failure — a sales one. Every draft understated the tool
    count by 35-72%, which is the opposite of the partner-growth goal these
    files exist to serve."""
    tools = _canon_tools()
    low = []
    # ★Line-scoped with explicit exclusions, each one a real false positive this
    # guard produced on its first run:
    #   · "detector flags '3,000+ M&A' and '58 tools'-era strings as stale
    #     markers" — prose ABOUT stale strings, not a claim
    #   · "free: '5 calls/day, truncated results, 20 tools'" — a TIER ALLOWANCE;
    #     the next line correctly advertises all 82
    # A guard that cannot tell a claim from a quota reads as noise and gets
    # muted, which is worse than not having it.
    _SKIP = ("stale", "-era", "free:", "developer:", "pro:", "starter:",
             "truncated", "calls/day", "per day", "quota", "tier")
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        low_line = line.lower()
        if any(w in low_line for w in _SKIP):
            continue
        for pat in (r"\b(\d{2,3}) tools\b", r"\b(\d{2,3}) MCP tools\b",
                    r"Tools:\s*(\d{2,3})\+?", r'"tools":\s*(\d{2,3})\b'):
            for m in re.finditer(pat, line):
                if int(m.group(1)) < tools:
                    low.append(m.group(0))
    assert not low, (
        "%s advertises %s but canon advertises %d tools — regenerate the copy"
        % (path.name, ", ".join(sorted(set(low))), tools))


def test_glama_json_is_fenced_because_glama_auto_discovers_it():
    """★glama.json is not a draft. Glama reads .well-known/glama.json directly,
    so a stale number there is LIVE on a third-party listing without anyone
    submitting anything. It carried 22,000+ — the exact retired over-claim."""
    p = _ROOT / "glama.json"
    assert p.exists()
    assert p in _outbound(), "glama.json dropped out of the fenced set"
    assert "22,000+" not in p.read_text(encoding="utf-8")
