"""tests/test_outbound_claims_are_substantiated.py — we only publish what we can
prove (2026-08-12).

The registry submission drafts carried four claims that cannot be substantiated
from any surface this repo serves:

    "Active AI platforms: 96+"        /api/v1/mcp/platforms → connected_count: 0
    "MCP calls per month: 100,000+"   no readable surface reports it
    "369 GW of construction pipeline" not reproducible from /api/v1/pipeline
    "Used by leading AI platforms (Claude, ChatGPT, Gemini, Perplexity,
     Copilot, Meta AI)"               get_agent_registry masks aggregates and
                                      returned 1 of 11 platforms

★THE PUBLIC PLATFORMS ENDPOINT MAY ITSELF BE MASKED (hidden_noise_count: 76), so
this is NOT an assertion that nobody uses the server. It is the weaker and more
important claim: WE CANNOT PROVE IT, and an unprovable usage number pasted into
a third-party marketplace is the kind of thing that gets a listing pulled — and
it would undo the canon-accuracy work in #2609 on the very same files.

Everything left in the drafts is measured: 82 tools (ai_surface_canon
tools_advertised), 17,000+ facilities (live 17,425), 300+ markets (live 320),
1,600+ M&A deals, 170+ countries.

Guards:
  (1) RESURRECTION — an unverifiable claim returns to outbound copy.
  (2) DRAFTED-BUT-UNWIRED — the registry shell stops noticing that we wrote
      submission copy for a registry it never probes. That gap is why MCP Hive
      sat unsubmitted with a complete draft on disk and nothing said so.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_outbound_claims_are_substantiated.py -v
"""
from __future__ import annotations

import glob
import importlib
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Each pattern is a claim we removed, with the reason it could not be proven.
_UNVERIFIABLE = [
    (re.compile(r"Active AI platforms:\s*\d+\+", re.I),
     "/api/v1/mcp/platforms reports connected_count: 0"),
    (re.compile(r"MCP calls per month:\s*[\d,]+\+", re.I),
     "no readable surface reports a monthly call volume"),
    (re.compile(r"\d+\s*GW (of )?(construction )?pipeline", re.I),
     "not reproducible from /api/v1/pipeline"),
    (re.compile(r"Used by leading AI platforms", re.I),
     "get_agent_registry masks aggregates; the claim is unprovable"),
    (re.compile(r"Powering leading AI platforms", re.I),
     "same unprovable usage claim, short form"),
]


def _outbound() -> list:
    files = (sorted(glob.glob(str(_ROOT / "PATCHES/REGISTRY_SUBMISSIONS_r45/*.md")))
             + sorted(glob.glob(str(_ROOT / "PATCHES/MCP_DIRECTORIES/*.json")))
             + [str(_ROOT / p) for p in
                ("PATCHES/ANTHROPIC_CONNECTOR_SUBMISSION.md",
                 "REGISTRY_SUBMISSIONS.md")])
    return [pathlib.Path(f) for f in files if pathlib.Path(f).exists()]


def test_there_is_outbound_copy_to_check():
    assert len(_outbound()) >= 8


@pytest.mark.parametrize("path", _outbound(), ids=lambda p: p.name)
def test_no_unverifiable_claim_in_outbound_copy(path):
    """★(1) These are what a marketplace would publish on our behalf, and we
    cannot take them back once ingested."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    hits = [(m.group(0), why) for pat, why in _UNVERIFIABLE
            for m in pat.finditer(text)]
    assert not hits, (
        "%s carries an unverifiable claim:\n  %s"
        % (path.name, "\n  ".join("%r — %s" % (h, w) for h, w in hits)))


def _shell():
    return importlib.import_module("routes.registry_distribution_master_shell")


def test_readme_is_not_counted_as_a_registry():
    """★A guard that invents a target is the same false positive as one that
    invents a violation. The first run reported README.md as an unwired
    registry."""
    assert "README" not in _shell()._drafted_targets()


def test_every_draft_on_disk_is_discovered():
    """Derived from the filesystem, never a hand-maintained list — a second list
    drifts from the first, which is the duplicated-constant bug that produced a
    false DCPI green-on-stale alarm the same day."""
    on_disk = {pathlib.Path(f).stem
               for f in glob.glob(str(_ROOT / "PATCHES/REGISTRY_SUBMISSIONS_r45/*.md"))
               if pathlib.Path(f).stem not in ("README", "INDEX", "NOTES")}
    assert set(_shell()._drafted_targets()) == on_disk


def test_the_lane_reports_drafted_registries_it_cannot_probe():
    """★(2) THE MCP HIVE FAILURE. A complete draft existed since r45 and was
    never sent; the shell probes only _REGISTRIES, so it never asked. The lane
    must name the gap rather than render a clean board."""
    m = _shell()
    checks = m._lane_drafted_but_unwired()
    ids = {c["id"] for c in checks}
    assert "drafts_wired_for_verification" in ids
    wired = next(c for c in checks if c["id"] == "drafts_wired_for_verification")
    if wired["pass"] is False:
        assert "mcp-hive" in wired["detail"] or "NOT in _REGISTRIES" in wired["detail"]


def test_the_lane_is_registered_so_it_actually_runs():
    """A lane nobody runs is the orphan-blueprint failure in another costume."""
    m = _shell()
    assert any(lid == "drafted_but_unwired" for lid, _n, _f in m._LANES), \
        "the drafted-but-unwired lane exists but is not in _LANES"
