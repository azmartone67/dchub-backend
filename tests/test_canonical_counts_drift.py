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

  worker.js (the Cloudflare edge worker serving dchub.cloud/mcp) is likewise NOT
  in SURFACES: its ~290KB body carries a /* */ changelog header that RECORDS past
  counts ("53 -> 72 tools", "3,000+ -> 4,000+") as version history, which a naive
  line-scan would false-positive on. Its one machine-readable agent-facing claim,
  the /mcp server-card MCP_SERVER_INFO.description, is guarded surgically by
  test_worker_mcp_card_is_canonical instead. (That card is the in-repo analog of
  the live /mcp `initialize` instructions blob, which is emitted by the deployed
  MCP server, not from this repo, so it cannot be pinned from here.)

  AGENT_CODE_SURFACES extends the fence to the server-side SOURCE that EMITS
  agent-facing strings (MCP tool catalogs, the /mcp connect page, the A2A card,
  interconnection blurbs). Because those are .py (not served data bodies), they
  get a high-signal scan — tool-count + BANNED_STALE only, NOT the full
  stale_markers denylist, whose bare-number/version markers would collide with
  incidental code. See test_agent_code_surfaces_free_of_stale_counts.

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
    "tools": 81,        # live tools/list length on the public MCP gate (★2026-07-29: 80 -> 81, +get_hosting_capacity — live-probed on both dchub.cloud/mcp and api.dchub.cloud/mcp)
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

# ── AGENT_CODE_SURFACES: server-side SOURCE that EMITS agent-facing strings
#    (MCP tool catalogs, the /mcp connect page, the A2A card, interconnection
#    status blurbs). Unlike SURFACES these are .py, not served data bodies, so
#    they are scanned with the high-signal tool-count + BANNED_STALE patterns
#    ONLY — never the ai_surface_canon stale_markers denylist, whose bare-number
#    / version markers ("232 ", "2.4.3", "2.1.0", "50000") would collide with
#    incidental code. worker.js is deliberately NOT here: its ~290KB body carries
#    a changelog header of past counts, so it gets a surgical guard
#    (test_worker_mcp_card_is_canonical) instead of a whole-file line-scan. ──────
AGENT_CODE_SURFACES = (
    # ★2026-07-25: ai_discovery_routes.py SERVES /llms.txt and /llms-full.txt
    # inline (main.py:20177 — the old file-backed routes were removed). It was
    # absent from this tuple, so the fence scanned four repo-root files that
    # nothing serves and went green while the live surfaces carried 21,000+ in
    # ten places. The served path must be fenced, not just the artifact.
    "ai_discovery_routes.py",
    "chatgpt_mcp_compat.py",
    "ai_interconnection.py",
    "mcp_gatekeeper.py",
    os.path.join("routes", "mcp_connect.py"),
    os.path.join("routes", "mcp_tool_catalog.py"),
    os.path.join("routes", "agent_a2a.py"),
    # ★2026-07-30: /agent (Agent Concierge landing) is served INLINE from this
    # route module — the frontend worker proxies the bare /agent path to Flask,
    # so no frontend heal can reach it. It was absent from this tuple and its
    # title/hero stale-cycled through retired tool counts while the fence stayed
    # green (ChatGPT's citation card was still showing a two-generations-old
    # count). Same class as ai_discovery_routes.py above: fence the SERVED
    # source. Counts there now render from ai_surface_canon.PINNED, so this scan
    # should find no literals — it exists to catch a re-hardcode.
    os.path.join("routes", "agent_concierge.py"),
)

# ── Allow-list: lines that are explicitly historical/retrospective are exempt.
#    Keyword-driven on purpose (a bare date is NOT enough — many current lines
#    carry a "Last Updated" date), so a genuinely NEW stale claim is still
#    caught even if it sits next to a date. ────────────────────────────────────
_HISTORICAL_RE = re.compile(
    r"\bwas\b|\bformerly\b|\bpreviously\b|\bno longer\b|\bdeprecated\b"
    r"|\bhistorical\b|\bchangelog\b|\bused to\b|\brenamed from\b|\bprior to\b"
    r"|\bsuperseded\b|\bretired\b|\bwere live\b",
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
        "facilities_stale_floor",
        re.compile(r"(?<![\d,])(?:19|20|21|22|23),\d{3}\+"),
        "15,000+ facilities",
        "facilit",
        "2026-07-25: the pre-dedup facility floor. ai_surface_canon rebased to "
        "DISTINCT SITES on 07-24 (customer dedup audit) and the old floor became "
        "a ~1.7x over-claim. A RANGE, not one value, on purpose: 20,000+, "
        "21,000+ and 22,000+ were all live simultaneously — llms.txt served "
        "21,000+ from ai_discovery_routes while the repo-root copy the fence "
        "scanned said 22,000+ and dchub-frontend said 20,000+. Pinning a single "
        "retired value would have caught one of the three.",
    ),
    (
        "facilities_retired_12650",
        re.compile(r"(?<![\d,])12,650\+"),
        "15,000+ facilities",
        None,
        "2026-07-30: '12,650+' — canon itself from 07-24 to 07-28 — is now the "
        "RETIRED floor (PINNED rebased to 15,000+, live 15,300+). It sat on the "
        "/ai hero contradicting the SAME page's live-hydrated stat card "
        "(15,367+), and in ~200 files across both repos; swept 07-30. No "
        "`requires` guard on purpose: unlike a bare '232', the token 12,650+ "
        "never occurs incidentally — any hit is the retired claim, whatever "
        "noun follows it (the range entry above misses it entirely, which is "
        "how the LAST retirement went unfenced — widen the guard to catch the "
        "NEXT wrong value, not just the previous one).",
    ),
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


def _iter_surface_lines(paths):
    """Yield (path, line_no, line_text) for every file in `paths`, skipping
    lines flagged as historical/changelog context."""
    for rel in paths:
        p = REPO_ROOT / rel
        assert p.is_file(), (
            f"{rel}: agent-facing surface missing — this drift-fence anchors to "
            f"it ({FIXWAVE}). If the file moved/renamed, update the surface list "
            f"to follow it (do not just drop the surface)."
        )
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if _HISTORICAL_RE.search(line):
                continue  # allow-listed retrospective/changelog mention
            yield rel, i, line


def _surface_lines():
    """Yield lines for the served data SURFACES (llms.txt, README, mcp.json, …)."""
    yield from _iter_surface_lines(SURFACES)


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


def test_worker_mcp_card_is_canonical():
    """The edge worker's served /mcp server card must state the canonical tool
    count and carry no banned deal/market/gas/ISO tokens.

    worker.js is the Cloudflare edge worker for dchub.cloud/mcp. It is NOT a
    line-scanned SURFACE (see the module docstring) because its ~290KB body has a
    changelog header that legitimately records past counts. Instead we guard the
    one machine-readable thing an agent reads from it: MCP_SERVER_INFO.description
    — the server-card description string. This is the in-repo analog of the live
    /mcp `initialize` instructions blob (served by the deployed MCP server, so it
    cannot be pinned from this repo).

    Regression guarded: the card once advertised "73 tools ... 10 ISOs ... 4,000+
    tracked M&A deals" while the worker's OWN comments said "72 tools" and canon
    said 79 — exactly the self-contradicting count sprawl the canon layer exists
    to kill.
    """
    worker = REPO_ROOT / "worker.js"
    assert worker.is_file(), (
        f"worker.js (edge worker serving /mcp) missing — this guard anchors to "
        f"its MCP_SERVER_INFO.description ({FIXWAVE})."
    )
    text = worker.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"MCP_SERVER_INFO\s*=\s*\{.*?\bdescription:\s*'([^']*)'", text, re.S)
    assert m, (
        "could not locate MCP_SERVER_INFO.description in worker.js — if the "
        f"server-card shape changed, update this guard to follow it ({FIXWAVE})."
    )
    desc = m.group(1)

    # (a) every tool count stated in the card must be the canonical count.
    tool_counts = re.findall(r"(?<![\d,])(\d{1,3})\s+(?:live\s+|MCP\s+)?tools\b", desc)
    assert tool_counts, (
        f"worker.js MCP card states no tool count — the guard would pass "
        f"vacuously ({FIXWAVE}): {desc[:120]!r}"
    )
    bad_counts = sorted({int(c) for c in tool_counts if int(c) != CANONICAL["tools"]})
    assert not bad_counts, (
        f"worker.js MCP_SERVER_INFO.description advertises {bad_counts} tools; "
        f"canonical is {CANONICAL['tools']} (live tools/list). Update the literal "
        f"AND the neighbouring NOTE comment ({FIXWAVE})."
    )

    # (b) no BANNED_STALE deal/market/gas/ISO token may appear in the card.
    low = desc.lower()
    banned_hits = []
    for tok_id, pat, canonical_phrase, requires, why in BANNED_STALE:
        if requires and requires.lower() not in low:
            continue
        hit = pat.search(desc)
        if hit:
            banned_hits.append(
                f"  [{tok_id}] {hit.group(0)!r} contradicts canonical "
                f"'{canonical_phrase}'\n        why: {why}"
            )
    assert not banned_hits, (
        "Stale count token(s) in the served /mcp server card "
        f"(worker.js MCP_SERVER_INFO.description) ({FIXWAVE}):\n"
        + "\n".join(banned_hits)
    )


def test_agent_code_surfaces_free_of_stale_counts():
    """Server-side code that EMITS agent-facing strings must not hard-code a
    non-canonical tool count or a banned deal/market/gas/ISO token.

    Covers the MCP tool catalogs (mcp_gatekeeper, routes/mcp_tool_catalog), the
    /mcp connect page (routes/mcp_connect), the A2A card (routes/agent_a2a), the
    ChatGPT MCP-compat surface, and the interconnection status blurbs
    (ai_interconnection) — all of which shipped "73 tools" / "4,000+ deals" /
    "10 ISOs" while canon said 79 / 1,400+ / 7 US ISOs.

    High-signal scan by design: only the tool-count regex and BANNED_STALE
    patterns, reused from the data-SURFACE tests, plus the shared historical
    allow-list. It deliberately does NOT apply the ai_surface_canon stale_markers
    denylist here — those bare-number/version markers ("232 ", "2.4.3", ...)
    would collide with incidental code (line numbers, protocol versions, limits).
    """
    tool_count_re = re.compile(r"(?<![\d,])(\d{1,3})\s+(?:live\s+|MCP\s+)?tools\b")
    failures = []
    canonical_tool_count_seen = False
    for rel, i, line in _iter_surface_lines(AGENT_CODE_SURFACES):
        low = line.lower()
        for m in tool_count_re.finditer(line):
            n = int(m.group(1))
            if n == CANONICAL["tools"]:
                canonical_tool_count_seen = True
            else:
                failures.append(
                    f"  {rel}:{i}: advertises {n} tools (canonical "
                    f"{CANONICAL['tools']}) -> {line.strip()[:90]!r}"
                )
        for tok_id, pat, canonical_phrase, requires, why in BANNED_STALE:
            if requires and requires.lower() not in low:
                continue
            hit = pat.search(line)
            if hit:
                failures.append(
                    f"  [{tok_id}] {rel}:{i}: {hit.group(0)!r} contradicts "
                    f"canonical '{canonical_phrase}' -> {line.strip()[:90]!r}"
                )
    assert not failures, (
        "Stale count(s) hard-coded in agent-facing server code — fix to canon "
        f"({FIXWAVE}):\n" + "\n".join(failures)
    )
    assert canonical_tool_count_seen, (
        f"No AGENT_CODE_SURFACE advertises the canonical '{CANONICAL['tools']} "
        f"tools' — the guard would pass vacuously; a surface should state the "
        f"count so this has something to protect ({FIXWAVE})."
    )
