"""canonical-counts DRIFT-FENCE — static source guard for agent-facing counts.

Build-breaking companion to the *advisory* canon layer (ai_surface_canon.py's
runtime sentinel + the manifest linters). Those audit the LIVE served bodies
after deploy; this fires in CI, at merge time, on the static source in the repo
— so a stale headline count can never SHIP in the first place.

Why it exists: this session shipped count sprawl across the agent-facing
surfaces — "48 tools" (live tools/list is 79), "232 markets" (>=300), "4,000+"
deals (the AUTO id embeds the ingest date, so 4,275 rows collapse to ~1,420
distinct — canonical floor is 1,400+), "47" gas states (52), and "10 ISOs" /
"10 North-American ISOs" (there are 7 US ISOs; "10 North-American grid
OPERATORS" is the separate, correct claim). The fixes landed in
#1689/#1690/#74/#75; this fence keeps them from silently reverting.

Design (all static reads — no DB, no network, sub-second; same source-extraction
style as tests/test_canon_constraint_guard.py):

  CANONICAL   tools=79 · markets>=300 · deals>=1400 · gas=52 states · isos=7
  SURFACES    llms.txt · llms-full.txt · README.md · .well-known/mcp.json
              (a TIGHT, reliable, currently-canon-clean set — see the note on
               AGENTS.md below for why there is no static AGENTS.md surface here)
  BANNED      the specific stale tokens that shipped this session, matched
              boundary-safely (so "2,000+ deals" can't hide inside "22,000+
              facilities" and "10 North-American grid operators" is NOT
              mis-flagged as the "10 ISOs" error), PLUS the reused
              ai_surface_canon.PINNED["stale_markers"] denylist.

Reuse, not duplication: the canonical values and the stale-marker denylist are
IMPORTED from the existing source-of-truth modules (ai_surface_canon.PINNED and
canonical_stats._FALLBACK). test_fence_baseline_matches_canon_sot below pins
those imports so the fence and the canon can never silently disagree — if the
SoT itself drifts, this test says so.

Allow-list: a banned token on a line explicitly marked historical/retrospective
(was / formerly / previously / no longer / deprecated / changelog / …) is NOT a
failure — that is how a legit "(was 4,000+, now 1,400+)" changelog note is
tolerated. Kept keyword-driven (not bare-date) so a NEW stale claim that merely
happens to sit near a date is still caught.

Crude by design: "did a headline count drift" protection, not a behavior test.
If you intentionally change a canonical count, move the protection with it —
update CANONICAL here AND the surfaces AND ai_surface_canon/canonical_stats — do
not just delete the assertion.

  AGENTS.md has no static surface to guard. A stale static /AGENTS.md USED TO
  sit at the repo root ("14 total" tools, fake tool names, "4,000+ deals",
  "3,800+ providers"), but it was never the served surface: routes/
  agents_md_fallback.py renders the LIVE /AGENTS.md from ai_surface_canon.PINNED
  (self-fresh), and the only route that would have read the static file
  (ai_agent_discovery.py's load_file('AGENTS.md')) is an unregistered blueprint
  (dead). Rather than carry a shadowed drift landmine, that static file was
  DELETED (2026-07-20, repo-hygiene). The live surface cannot drift — it renders
  straight from PINNED, whose counts test_fence_baseline_matches_canon_sot
  already pins — so there is deliberately no AGENTS.md entry in SURFACES.

Run locally:
    python -m pytest tests/test_canonical_counts_drift.py -v
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# conftest.py already puts REPO_ROOT on sys.path; belt-and-suspenders so this
# module also imports the SoT cleanly when run/py_compiled standalone.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai_surface_canon import PINNED          # noqa: E402  (SoT: stale_markers + tools_advertised)
from canonical_stats import _FALLBACK        # noqa: E402  (SoT: markets/deals/isos floors)

FIXWAVE = "canonical-counts drift (fixes #1689/#1690/#74/#75, 2026-07-20)"

# ── CANONICAL: the fence's independent baseline (DESIGN #3). Kept as explicit
#    literals so a *malicious/accidental* edit to the SoT can't quietly move the
#    goalposts — test_fence_baseline_matches_canon_sot cross-checks that the
#    imported SoT still agrees with these. ──────────────────────────────────────
CANONICAL = {
    "tools": 79,        # live tools/list length on the public MCP gate
    "markets_min": 300,  # DCPI markets floor (live ~311; grows via intl expansion)
    "deals_min": 1400,  # DISTINCT deduped tracked deals floor (rows over-state ~2.9x)
    "gas": 52,          # gas-suitability states (DCGI)
    "isos": 7,          # live US ISOs: ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE
}

# ── SURFACES: live, agent-facing, confirmed canon-clean on main. Paths relative
#    to the repo root. .well-known/mcp.json carries no stated tool COUNT (its
#    tools[] is a curated subset, by design) so it contributes coverage only on
#    the deal/market/gas/grid tokens — which is exactly where a revert would
#    land it. ──────────────────────────────────────────────────────────────────
SURFACES = (
    "llms.txt",
    "llms-full.txt",
    "README.md",
    os.path.join(".well-known", "mcp.json"),
)

# ── Allow-list: lines that are explicitly historical/retrospective are exempt.
#    Keyword-driven on purpose (a bare date is NOT enough — many current lines
#    carry a "Last Updated" date), so a genuinely NEW stale claim is still
#    caught even if it sits next to a date. ────────────────────────────────────
_HISTORICAL_RE = re.compile(
    r"\bwas\b|\bformerly\b|\bpreviously\b|\bno longer\b|\bdeprecated\b"
    r"|\bhistorical\b|\bchangelog\b|\bused to\b|\brenamed from\b|\bprior to\b"
    r"|\bsuperseded\b|\bretired\b",
    re.I,
)

# ── BANNED_STALE — the specific stale tokens that shipped this session, as
#    boundary-safe regexes. Each: (id, compiled, canonical-phrase, requires,
#    why). `requires` (if set) must also be present on the line (case-insensitive)
#    for a match to count — used to keep a bare "232" from mis-firing outside a
#    markets context. The leading (?<![\d,]) stops a token matching inside a
#    LARGER number (e.g. "2,000+ deals" is NOT a substring hit on "22,000+
#    facilities"). ─────────────────────────────────────────────────────────────
BANNED_STALE = [
    (
        "markets_232",
        re.compile(r"(?<![\d,])232\+?\b"),
        "300+ markets",
        "market",
        "232 was the pre-intl-expansion market count; canonical floor is 300+ (live ~311).",
    ),
    (
        "deals_stale_floor",
        re.compile(
            r"(?<![\d,])(?:2,000|2,200|3,000|4,000)\+\s*"
            r"(?:tracked\s+)?(?:M&A\s+|data[\s-]center\s+)?"
            r"(?:deals|transactions|M&A)\b",
            re.I,
        ),
        "1,400+ tracked deals",
        None,
        "'4,000+/3,000+/2,200+/2,000+ deals' floored duplicate ROWS; deduped "
        "canonical floor is 1,400+ (deals_phrase).",
    ),
    (
        "gas_47_states",
        re.compile(r"(?<![\d,])47\+?[\s-]+(?:US\s+)?states?\b", re.I),
        "52 gas states",
        None,
        "gas coverage is 52 states (DCGI); '47 states' is the stale count.",
    ),
    (
        "grid_10_isos",
        re.compile(r"(?<![\d,])10\s+(?:North[\s-]American\s+)?ISOs?\b", re.I),
        "7 US ISOs",
        None,
        "there are 7 live US ISOs; '10 ISOs'/'10 North-American ISOs' is wrong "
        "(the correct '10' claim is 10 North-American grid OPERATORS, which this "
        "pattern deliberately does NOT match).",
    ),
]


def _surface_lines():
    """Yield (surface_path, line_no, line_text) for every SURFACE, skipping
    lines flagged as historical/changelog context."""
    for rel in SURFACES:
        p = REPO_ROOT / rel
        assert p.is_file(), (
            f"{rel}: agent-facing surface missing — this drift-fence anchors to "
            f"it ({FIXWAVE}). If the file moved/renamed, update SURFACES to "
            f"follow it (do not just drop the surface)."
        )
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if _HISTORICAL_RE.search(line):
                continue  # allow-listed retrospective/changelog mention
            yield rel, i, line


def test_fence_baseline_matches_canon_sot():
    """The fence's CANONICAL baseline must agree with the imported SoT modules.

    Guards the guard: if ai_surface_canon.PINNED or canonical_stats._FALLBACK
    drifts away from what this fence enforces on the surfaces, reconcile them —
    don't let the surfaces and the canon quietly disagree. Floors (markets,
    deals) may GROW, so those are `>=`; tools and ISOs are exact.
    """
    assert PINNED.get("tools_advertised") == CANONICAL["tools"], (
        f"ai_surface_canon.PINNED['tools_advertised']="
        f"{PINNED.get('tools_advertised')} but this fence pins tools="
        f"{CANONICAL['tools']} (the live tools/list length). Reconcile the "
        f"canon and this test together ({FIXWAVE})."
    )
    assert int(_FALLBACK.get("markets", 0)) >= CANONICAL["markets_min"], (
        f"canonical_stats._FALLBACK['markets']={_FALLBACK.get('markets')} dropped "
        f"below the fence floor {CANONICAL['markets_min']}."
    )
    assert int(_FALLBACK.get("deals", 0)) >= CANONICAL["deals_min"], (
        f"canonical_stats._FALLBACK['deals']={_FALLBACK.get('deals')} dropped "
        f"below the fence floor {CANONICAL['deals_min']}."
    )
    assert int(_FALLBACK.get("isos", 0)) == CANONICAL["isos"], (
        f"canonical_stats._FALLBACK['isos']={_FALLBACK.get('isos')} != "
        f"{CANONICAL['isos']} live US ISOs."
    )


def test_surfaces_advertise_canonical_tool_count():
    """Every '<n> tools' mention on a SURFACE must be the canonical 79, and at
    least one surface must actually advertise it (so the fence isn't vacuous).

    Catches the exact regression from this session (48 -> 79) AND any future
    non-canonical tool count (e.g. a stray '80 tools'), not just the enumerated
    stale tokens. Matches '79 tools', '79 live tools', '79 MCP tools'. Does not
    match the shields.io badge 'tools-79' (number trails the word) or the JSON
    key '"tools":' (no leading number) — neither states a human count.
    """
    tool_count_re = re.compile(r"(?<![\d,])(\d{1,3})\s+(?:live\s+|MCP\s+)?tools\b")
    failures = []
    canonical_seen = False
    for rel, i, line in _surface_lines():
        for m in tool_count_re.finditer(line):
            n = int(m.group(1))
            if n == CANONICAL["tools"]:
                canonical_seen = True
            else:
                failures.append(
                    f"  {rel}:{i}: advertises {n} tools (canonical is "
                    f"{CANONICAL['tools']}) -> {line.strip()[:90]!r}"
                )
    assert not failures, (
        "Stale MCP tool count on an agent-facing surface — the live tools/list "
        f"length is {CANONICAL['tools']} ({FIXWAVE}):\n" + "\n".join(failures)
    )
    assert canonical_seen, (
        f"No SURFACE advertises the canonical '{CANONICAL['tools']} tools' — the "
        "fence would pass vacuously. A surface should state the tool count so "
        f"this guard has something to protect ({FIXWAVE})."
    )


def test_surfaces_free_of_banned_stale_counts():
    """No SURFACE may carry a BANNED_STALE count token outside a historical/
    changelog line. Each failure names file:line, the stale token, and the
    canonical value it contradicts.
    """
    failures = []
    for rel, i, line in _surface_lines():
        low = line.lower()
        for tok_id, pat, canonical_phrase, requires, why in BANNED_STALE:
            if requires and requires.lower() not in low:
                continue
            m = pat.search(line)
            if m:
                failures.append(
                    f"  [{tok_id}] {rel}:{i}: {m.group(0)!r} contradicts "
                    f"canonical '{canonical_phrase}' -> {line.strip()[:90]!r}\n"
                    f"        why: {why}"
                )
    assert not failures, (
        "Stale headline count(s) on agent-facing surface(s) — advisory canon "
        f"is not enough, this is the build-breaking gate ({FIXWAVE}):\n"
        + "\n".join(failures)
    )


@pytest.mark.parametrize(
    "marker",
    [m for m in PINNED.get("stale_markers", []) if m and m.strip()],
    ids=lambda m: m.strip().replace(" ", "_"),
)
def test_surfaces_clean_of_ai_surface_canon_stale_markers(marker):
    """Reuse the EXISTING ai_surface_canon.PINNED['stale_markers'] denylist as a
    static gate (the runtime sentinel only checks it against live-served bodies).

    Boundary-safe: numeric markers get a (?<![\\d.,]) prefix so e.g. '2,000+ M&A'
    is not a spurious hit inside '22,000+ M&A' — a hazard the sentinel's plain
    substring `in` check tolerates because it runs on already-canon bodies, but
    which a hard CI gate must not. Historical/changelog lines are exempt.
    """
    if marker[0].isdigit():
        pat = re.compile(r"(?<![\d.,])" + re.escape(marker))
    else:
        pat = re.compile(re.escape(marker))
    hits = []
    for rel, i, line in _surface_lines():
        if pat.search(line):
            hits.append(f"  {rel}:{i}: {marker!r} -> {line.strip()[:90]!r}")
    assert not hits, (
        f"ai_surface_canon stale marker {marker!r} present on an agent-facing "
        f"surface — update it from canon ({FIXWAVE}):\n" + "\n".join(hits)
    )
