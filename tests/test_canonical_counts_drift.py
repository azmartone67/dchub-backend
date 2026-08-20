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

  main.py is likewise NOT in AGENT_CODE_SURFACES, for the worker.js reason at
  42,000 lines: a whole-file line-scan produces ~30 false positives (Flask
  "Phase 232" section headers, "6 tools in a day" prose, tier comments reading
  "Tier 1 MCP tools"). It gets surgical guards over the two blobs that actually
  reach an agent — test_main_by_the_numbers_tool_count_renders_from_canon and
  test_main_agent_recommend_blurbs_are_canonical. Both fence the SHAPE (is this
  rendered from canon?) rather than a value, because value-only checks cannot
  see a count that has never been wrong before: /by-the-numbers published "33
  MCP tools" against a live 82 for months, and BANNED_STALE has no "33" entry.

  frontend_stat_normalizer.py gets a direct assertion rather than a scan
  (test_frontend_stat_normalizer_matches_canon): it is a REWRITER, so a stale
  number there is written INTO frontend copy, and its find-patterns must quote
  retired values by design — which is exactly what a line-scan would flag.

Run locally:
    python -m pytest tests/test_canonical_counts_drift.py -v
"""
from __future__ import annotations

import ast
import functools
import html
import json
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

# ── The facility floor as the canon states it RIGHT NOW, derived rather than
#    typed. Used for the advice text in BANNED_STALE failures and by
#    test_no_facility_figure_below_the_canon_floor below.
#
#    ★2026-08-18: this used to be a hand-typed "15,000+ facilities" string,
#    which is how the fence ended up RECOMMENDING a floor that was itself three
#    generations stale (15,000 -> 15,700 -> 17,000 -> 18,000 -> 18,300 in about
#    three weeks). A guard that hands out a literal as the fix is a guard that
#    re-seeds the drift it exists to catch. ──────────────────────────────────
CANON_FACILITIES = (PINNED.get("public") or {}).get("facilities") or ""
CANON_FACILITIES_PHRASE = f"{CANON_FACILITIES} facilities"


def _floor_int(phrase: str) -> int:
    """'18,300+' -> 18300. Returns 0 if the canon is unreadable (fail-open)."""
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+)", phrase or "")
    return int(m.group(1).replace(",", "")) if m else 0

# ── CANONICAL: the fence's independent baseline (DESIGN #3). Kept as explicit
#    literals so a *malicious/accidental* edit to the SoT can't quietly move the
#    goalposts — test_fence_baseline_matches_canon_sot cross-checks that the
#    imported SoT still agrees with these. ──────────────────────────────────────
CANONICAL = {
    "tools": 82,        # live tools/list length on the public MCP gate (★2026-07-31: 81 -> 82, +get_power_availability_timeline, gateway v2.10.0 — live-probed on dchub.cloud/mcp; ★2026-07-29: 80 -> 81, +get_hosting_capacity)
    "markets_min": 300,  # DCPI markets floor (live ~311; grows via intl expansion)
    "deals_min": 1400,  # DISTINCT deduped tracked deals floor (rows over-state ~2.9x)
    "gas": 52,          # gas-suitability states (DCGI)
    "isos": 7,          # live US ISOs: ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE
    # ★2026-07-31: the FREE-tier daily quota. Pinned here only because BOTH
    # enforcement lanes agree on it — tier_registry.TIER_LIMITS['free'] carries
    # rate_limit=10 (REST) and mcp_daily=10 (MCP), and the edge worker's
    # hand-copied MCP_TIERS.free.daily_limit is 10 too. That is the whole
    # licence for fencing this number: quotas are normally NOT safe to fence,
    # because one tier legitimately carries different values per lane (dev is
    # rate_limit 1,000 vs mcp_daily 500, which reads like drift and is not).
    # test_fence_baseline_matches_canon_sot re-checks every lane on each run, so
    # if they ever diverge this fence fails loudly instead of picking a winner.
    "free_calls": 10,
    # ★2026-07-31 — the EMAIL-BOUND free quota, the second half of the free
    # funnel and the number the /mcp landing page needed but did not have. It
    # is fenceable on the same licence as free_calls and no other: every lane
    # agrees on 50 (TIER_LIMITS['identified'] rate_limit AND mcp_daily, the edge
    # MCP_TIERS.identified.daily_limit, _canonical_pricing, and the live
    # bind_email_required gate copy). test_fence_baseline_matches_canon_sot
    # re-checks them each run. `developer` is deliberately absent: its lanes
    # DISAGREE (rate_limit 1,000 vs mcp_daily 500), so pinning either would be
    # the "new bug wearing a fence" this baseline exists to refuse.
    "identified_calls": 50,
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
    # ★2026-08-02: the /vs/<slug> comparison pages (and the differentiator
    # facts they render) carried a hardcoded "mcp_tools": 40 while canon said
    # 82, plus the raw-row facility floor — for weeks, because neither module
    # was fenced. Both now render from ai_surface_canon.PINNED; the scan
    # exists to catch a re-hardcode. (Interpolated f-string counts are
    # invisible to TOOL_COUNT_RE by design — the companion rendered-body
    # tests in tests/test_competitive_seo_pages.py cover the output side.)
    os.path.join("routes", "competitive_seo.py"),
    os.path.join("routes", "competitive_intel.py"),
    # ★2026-08-20: routes/agent_capabilities_feed.py — /api/v1/agents/capabilities.json,
    #  which is CC-BY-4.0 and carries `agent_quotable`, a sentence written to be
    #  pasted verbatim into an agent's answer. It was NOT in this tuple, and it
    #  published `{"facilities": 21000, "markets_scored": 232, "deals_tracked": 1972}`
    #  as its DB-down fallback plus a live raw `COUNT(*) FROM discovered_facilities`
    #  (26,334) as its headline — while /api/v1/canon/phrases served 18,500+.
    #
    #  ★FOUND BY MUTATION-TESTING THE NEW BARE-INT GUARD, not by reading. The
    #  guard was written, its unit controls passed, and then re-inserting the
    #  exact literal that shipped did NOT turn it red — because this file was
    #  outside the tuple the guard iterates. A fence's patterns are worthless at
    #  a path it never opens; COVERAGE is the dominant failure mode here, which
    #  is why the surface list is the thing that needed changing, not the regex.
    os.path.join("routes", "agent_capabilities_feed.py"),
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
        CANON_FACILITIES_PHRASE,
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
        CANON_FACILITIES_PHRASE,
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
            r"(?:tracked\s+)?(?:M&(?:amp;)?A\s+|data[\s-]center\s+)?"
            r"(?:deals|transactions|M&(?:amp;)?A)\b",
            re.I,
        ),
        "1,400+ tracked deals",
        None,
        "'4,000+/3,000+/2,200+/2,000+ deals' floored duplicate ROWS; deduped "
        "canonical floor is 1,400+ (deals_phrase). ★2026-07-31: now also "
        "matches the HTML-ESCAPED 'M&amp;A'. The pattern only knew the raw "
        "ampersand, so '4,000+ tracked M&amp;A deals' — the exact string on the "
        "/mcp landing page hero, and the natural form in any served HTML — read "
        "as clean. Found by mutation-testing this fence, not by it firing: "
        "every guard over that blob was green with the stale floor restored.",
    ),
    (
        "gas_47_states",
        re.compile(r"(?<![\d,])47\+?[\s-]+(?:US\s+)?states?\b", re.I),
        "52 gas states",
        None,
        "gas coverage is 52 states (DCGI); '47 states' is the stale count.",
    ),
    (
        "isos_non_canonical",
        re.compile(
            r"(?<![\d,-])(?!7\b)\d{1,3}\s+"
            r"(?:major\s+|live\s+|tracked\s+|US\s+|North[\s-]American\s+)?ISOs\b",
            re.I,
        ),
        "7 US ISOs",
        None,
        "there are 7 live US ISOs (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE), "
        "so ANY other count before 'ISOs' is wrong. ★2026-07-31: generalized from "
        "the old '10 ISOs'-only pattern, which pinned the single wrong value that "
        "had already been fixed and therefore missed every other one — "
        "ai_interconnection.py was serving '6 major ISOs (ERCOT, CAISO, NYISO, "
        "MISO, SPP, ISONE)' (six, PJM omitted) and mcp_gatekeeper.py advertised "
        "'11 tracked ISOs', both on fenced surfaces, both green. Fence the SHAPE, "
        "not the last known bad number. Still does NOT match the separate and "
        "correct '10 North-American grid OPERATORS' claim (7 US ISOs + TVA + BPA "
        "+ IESO) — that says operators, not ISOs. Ranges ('2-4 ISOs') are exempt "
        "via the hyphen in the lookbehind: those describe a call's arity, not "
        "coverage.",
    ),
]

# ── Tool-count phrasings. TOOL_COUNT_RE is the original shape ("82 tools").
#    TOOL_ALT_COUNT_RE covers the two ways the SAME claim gets written WITHOUT
#    the word "tools" directly after the digits — the exact blind spot that let
#    three stale counts sit on fenced surfaces until 2026-07-31:
#        "### Available MCP Tools (33 total — ...)"  ai_interconnection.py
#        "Available MCP tools (73 total; ...)"       ai_discovery_routes.py
#        "Full 60-tool list + JSON schemas"          ai_discovery_routes.py
#    All three are agent-facing headline counts; none matched TOOL_COUNT_RE, and
#    none is a BANNED_STALE token (that list bans specific retired VALUES, so it
#    can only ever catch a count that has already been wrong once). ────────────
TOOL_COUNT_RE = re.compile(r"(?<![\d,])(\d{1,3})\s+(?:live\s+|MCP\s+)?tools\b")
TOOL_ALT_COUNT_RE = re.compile(r"\((\d{1,3})\s+total\b|(?<![\d,])(\d{1,3})-tools?\b")

# ── FREE-tier quota phrasings. Fences the SHAPE — ANY free-tier calls/day
#    figure that is not CANONICAL["free_calls"] — for the same reason
#    isos_non_canonical was generalized away from pinning "10 ISOs": a
#    banned-VALUE list can only ever catch a number that has already shipped
#    wrong once, and the number this was built for ("1k") had not.
#
#    It shipped as `<span class="badge">Free tier 1k calls/day</span>` in the
#    /mcp landing page hero — a 100x over-claim sitting four lines below the
#    SAME page's JSON-LD saying "Free tier: 10 calls/day". Every guard over that
#    blob was green: "1k" is not a tool count, not a BANNED_STALE token, and the
#    literal "10" was present elsewhere in the template, so even a naive
#    presence check passed.
#
#    Deliberately anchored on "free tier" adjacency rather than a bare
#    "N calls/day": the paid quotas on these same surfaces are legitimately
#    1,000 (developer, edge lane) and 2,000 (pro), so an unanchored pattern
#    would flag every correct upgrade blurb on the page. See
#    test_free_tier_quota_shape_is_not_vacuous for both halves.
#
#    ★2026-07-31 — the KNOWN GAP recorded here is now CLOSED, and closing it
#    needed three changes, not the one-word "free tier|free key" widening the
#    original note anticipated. The gap text was:
#
#      For 1k calls/day, add a header ... <a href="/signup">get a free key here</a>
#
#    1. ORDER. The number comes BEFORE its anchor here, and this pattern only
#       reads anchor-then-number. FREE_PATH_QUOTA_RE below matches both.
#    2. MARKUP. 60-odd characters of <code>/<a href> sit between "1k calls/day"
#       and "free key", and the [^.<>] class cannot cross a tag at all — the
#       same evasion that let an HTML-escaped "M&amp;A" slip the deals regex in
#       #2059. Matching now runs on tag-stripped, entity-decoded text.
#    3. VOCABULARY. "free tier" was never the only free-path phrasing. The
#       sibling line on the same page said "Tools work anonymously at 5
#       calls/day" — a free-path quota claim with no "free" in it.
#
#    This pattern is KEPT, unwidened, as the strict layer: a literal "free tier"
#    claim must be exactly CANONICAL["free_calls"]. FREE_PATH_QUOTA_RE is the
#    broad layer and admits the email-bound figure too, because "free key" is
#    honestly either number depending on whether it is bound.
FREE_TIER_QUOTA_RE = re.compile(
    r"free[\s_-]*tier\b[^.<>]{0,40}?(?<![\d,.])(\d[\d,]*\s*[kK]?)\s*calls?\s*/\s*day",
    re.I,
)

# ── The BROAD free-path layer. Any claim tying a calls/day figure to a path the
#    caller does not PAY for — free tier, free key, anonymous, keyless — must
#    state one of the two canonical unpaid quotas: CANONICAL["free_calls"] (10,
#    anonymous or an unbound key) or CANONICAL["identified_calls"] (50, email
#    bound). Anything else is either an over-claim (the shipped "1k") or the
#    wrong lane (the shipped "anonymously at 5", which quoted the anonymous REST
#    rate_limit on an MCP page where mcp_daily governs).
#
#    Two-number tolerance is deliberate and is the honest shape of the funnel:
#    "get a free key" alone is 10, and the same key after bind_email is 50, so a
#    pattern admitting only one of them would false-positive on correct copy.
#    What it still catches is the whole failure class that shipped — any free
#    path advertised at a PAID tier's number.
#
#    Sentence-bounded ([^.]) so it cannot reach across ". " into an adjacent and
#    legitimately paid clause: MCP_RATE_LIMIT_NOTE puts "free tier limit reached
#    (N calls/day)." immediately before "unlock 500 calls/day with a Developer
#    key", and 500 there is correct.
_FREE_PATH = (r"free[\s_-]*(?:tier|key|plan)|anonymous(?:ly)?|keyless"
              r"|no[\s-]*sign[\s-]*up|without[\s_-]*(?:an?[\s_-]*)?key")
_CALLS_PER_DAY = r"(?<![\d,.])(\d[\d,]*\s*[kK]?)\s*calls?\s*/\s*day"
FREE_PATH_QUOTA_RE = re.compile(
    rf"(?:(?:{_FREE_PATH})[^.]{{0,80}}?{_CALLS_PER_DAY})"
    rf"|(?:{_CALLS_PER_DAY}[^.]{{0,80}}?(?:{_FREE_PATH}))",
    re.I,
)

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Tag-stripped, entity-decoded text, so a claim split across markup reads as
    one clause. Without this the /mcp funnel line's `1k calls/day ... <code>…</code>
    … <a href=…>free key</a>` is three fragments to any regex."""
    return html.unescape(_TAG_RE.sub(" ", text))


def _stated_free_tier_quotas(text: str) -> list[int]:
    """Every FREE-tier calls/day figure stated in `text`, normalised to an int.

    "1k" and "1,000" are the same claim written two ways, and the one that
    shipped was the "k" form — so both normalise here rather than the pattern
    knowing only the spelling that happens to be in the repo today.
    """
    out = []
    for m in FREE_TIER_QUOTA_RE.finditer(text):
        raw = m.group(1).replace(",", "").replace(" ", "")
        mult = 1
        if raw[-1:] in ("k", "K"):
            raw, mult = raw[:-1], 1000
        if raw.isdigit():
            out.append(int(raw) * mult)
    return out


def _stated_free_path_quotas(text: str) -> list[int]:
    """Every calls/day figure tied to an UNPAID access path in `text`.

    Runs on _plain(text): the claim this exists for was split across an <a> and
    a <code>, and a tag-blind pattern read it as unrelated fragments.
    """
    out = []
    for m in FREE_PATH_QUOTA_RE.finditer(_plain(text)):
        # One alternation per direction (anchor-first / number-first), so
        # exactly one of the two groups is populated per match.
        raw = (m.group(1) or m.group(2)).replace(",", "").replace(" ", "")
        mult = 1
        if raw[-1:] in ("k", "K"):
            raw, mult = raw[:-1], 1000
        if raw.isdigit():
            out.append(int(raw) * mult)
    return out


def _stated_tool_counts(text: str) -> list[int]:
    """Every tool COUNT stated in `text`, across all fenced phrasings.

    The alternate phrasings are only read on text that is actually talking
    about tools, so an unrelated "(12 total)" is not swept in.
    """
    counts = [int(m.group(1)) for m in TOOL_COUNT_RE.finditer(text)]
    if "tool" in text.lower():
        counts += [int(m.group(1) or m.group(2))
                   for m in TOOL_ALT_COUNT_RE.finditer(text)]
    return counts


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

    # ★2026-07-31 — the free-tier quota, cross-checked against the DISPLAY canon
    # AND both ENFORCEMENT lanes. This is the load-bearing half of fencing a
    # quota at all: a number that differs per lane is not drift, and pinning one
    # lane's value onto a surface quoting the other would be a new bug wearing a
    # fence. `free` is only fenceable because all three agree today; the moment
    # one moves, this fails and the next person decides which lane the surfaces
    # quote instead of inheriting a silently-wrong pin.
    assert PINNED.get("free_tier_calls_per_day") == CANONICAL["free_calls"], (
        f"ai_surface_canon.PINNED['free_tier_calls_per_day']="
        f"{PINNED.get('free_tier_calls_per_day')} but this fence pins "
        f"free_calls={CANONICAL['free_calls']}. Reconcile the canon and this "
        f"test together ({FIXWAVE})."
    )
    from tier_registry import TIER_LIMITS  # SoT: the ENFORCED per-tier caps
    for lane in ("rate_limit", "mcp_daily"):
        assert TIER_LIMITS["free"][lane] == CANONICAL["free_calls"], (
            f"tier_registry.TIER_LIMITS['free']['{lane}']="
            f"{TIER_LIMITS['free'][lane]} != the advertised free tier "
            f"{CANONICAL['free_calls']} calls/day. The two quota lanes have "
            f"diverged for `free`, so there is no longer ONE honest number to "
            f"render on the /mcp landing page — decide which lane the public "
            f"surfaces quote, then update PINNED, this baseline and the badge "
            f"together. Do NOT just relax this assertion ({FIXWAVE})."
        )

    # ★2026-07-31 (follow-on) — same three-way check for the EMAIL-BOUND quota.
    # The landing page now tells agents that binding an email moves a free key
    # to this number, so it carries exactly the weight free_calls does.
    assert PINNED.get("identified_calls_per_day") == CANONICAL["identified_calls"], (
        f"ai_surface_canon.PINNED['identified_calls_per_day']="
        f"{PINNED.get('identified_calls_per_day')} but this fence pins "
        f"identified_calls={CANONICAL['identified_calls']}. Reconcile the canon "
        f"and this test together ({FIXWAVE})."
    )
    for lane in ("rate_limit", "mcp_daily"):
        assert TIER_LIMITS["identified"][lane] == CANONICAL["identified_calls"], (
            f"tier_registry.TIER_LIMITS['identified']['{lane}']="
            f"{TIER_LIMITS['identified'][lane]} != the advertised email-bound "
            f"quota {CANONICAL['identified_calls']} calls/day. The /mcp landing "
            f"page promises this figure as the reward for calling bind_email, "
            f"and the live bind_email_required gate in flask_mcp_endpoints.py "
            f"promises it too — both must move with the lanes. Decide which "
            f"lane the public surfaces quote, then update PINNED, this baseline "
            f"and the page together ({FIXWAVE})."
        )

    # The `developer` split is WHY the two assertions above are written as
    # per-lane loops rather than a single pin. Asserted here so the split is a
    # tested fact, not a comment that can rot: if the lanes ever converge, this
    # fails and a developer figure becomes safe to quote on an MCP surface.
    assert TIER_LIMITS["developer"]["rate_limit"] != TIER_LIMITS["developer"]["mcp_daily"], (
        f"tier_registry.TIER_LIMITS['developer'] lanes have CONVERGED on "
        f"{TIER_LIMITS['developer']['rate_limit']} calls/day (they were "
        f"rate_limit=1000 vs mcp_daily=500). The /mcp landing page deliberately "
        f"links to /pricing instead of quoting a developer number because there "
        f"was no single honest one. If that is now settled, a "
        f"{{canon_developer_calls}} placeholder becomes safe — but reconcile the "
        f"edge worker's MCP_TIERS.developer.daily_limit (a hand-copy, 1000) in "
        f"the same change ({FIXWAVE})."
    )


def test_widened_count_shapes_are_not_vacuous():
    """The 2026-07-31 widenings must actually FIRE on the text they were built
    for — a pattern that matches nothing is the green-but-blind failure this
    fence exists to prevent, and both widenings replaced patterns that had
    exactly that problem (they pinned one already-fixed value each).

    STALE below is real text that was live on a fenced surface while every
    fence passed. CLEAN is correct text that must stay unflagged, including
    the two shapes the ISO pattern deliberately spares: the separate and
    correct 'North-American grid OPERATORS' claim, and a '2-4 ISOs' call
    arity (a range, not a coverage claim).
    """
    for text, expected in (
        ("### Available MCP Tools (33 total — full input schemas)", 33),
        ("Available MCP tools (73 total; flagship set below", 73),
        ("Full 60-tool list + JSON schemas", 60),
    ):
        assert expected in _stated_tool_counts(text), (
            f"TOOL_ALT_COUNT_RE no longer reads {expected} out of {text!r} — "
            f"the '(N total' / 'N-tool' blind spot is back ({FIXWAVE})."
        )
    assert not _stated_tool_counts("we shipped 12 total widgets today"), (
        f"the tools-context guard is gone — '(N total' now fires on any line, "
        f"which will bury real hits in noise ({FIXWAVE})."
    )

    iso_pat = dict((tok_id, pat) for tok_id, pat, *_ in BANNED_STALE)["isos_non_canonical"]
    for stale in ("Live grid data from 6 major ISOs (ERCOT, CAISO, NYISO)",
                  "head-to-head across all 11 ISOs ranked by avg excess-power",
                  "ISO snapshot for any of the 11 tracked ISOs",
                  "grid data from 10 North-American ISOs"):
        assert iso_pat.search(stale), (
            f"isos_non_canonical stopped matching {stale!r} ({FIXWAVE})."
        )
    for clean in ("Real-time grid data from 7 US ISOs (ERCOT, PJM, CAISO)",
                  "10 North-American grid operators w/ live data",
                  "pairwise side-by-side of 2-4 ISO grids",
                  "compare 2-4 ISOs in one call"):
        assert not iso_pat.search(clean), (
            f"isos_non_canonical false-positives on correct text {clean!r} — "
            f"a fence that cries wolf gets disabled ({FIXWAVE})."
        )

    # ★2026-07-31: the HTML-escaped ampersand. main.py's /mcp landing page is
    # served HTML, so its hero says "M&amp;A" — and the deals pattern only knew
    # the raw "&", so the stale floor read as clean on the one surface this
    # wave exists to fence. Both spellings must fire; the raw form must not
    # regress while widening for the escaped one.
    deals_pat = dict((tok_id, pat) for tok_id, pat, *_ in BANNED_STALE)["deals_stale_floor"]
    for stale in ("4,000+ tracked M&amp;A deals, grid intelligence",
                  "4,000+ tracked M&A deals, grid intelligence",
                  "M&amp;A across 4,000+ tracked deals",
                  "2,000+ M&amp;A transactions"):
        assert deals_pat.search(stale), (
            f"deals_stale_floor stopped matching {stale!r} — the HTML-escaped "
            f"'M&amp;A' spelling is how this shipped on the /mcp landing page "
            f"({FIXWAVE})."
        )
    for clean in ("1,500+ tracked M&amp;A deals, grid intelligence",
                  "M&amp;A across 1,500+ tracked deals"):
        assert not deals_pat.search(clean), (
            f"deals_stale_floor false-positives on the canonical floor "
            f"{clean!r} ({FIXWAVE})."
        )


def test_free_tier_quota_shape_is_not_vacuous():
    """FREE_TIER_QUOTA_RE must FIRE on the over-claim that shipped, and stay
    silent on the correct free-tier line and on every PAID quota.

    Both halves are load-bearing and the second one is why this pattern is
    anchored on "free tier" rather than a bare "N calls/day". The paid figures
    on these same surfaces are legitimately 1,000 (developer, edge lane) and
    2,000 (pro) — an unanchored pattern would flag the correct upgrade blurbs
    sitting a few lines from the badge, and a fence that cries wolf gets
    disabled.

    STALE below is the real text that was live at main.py:10149 while every
    other guard over that same blob passed.
    """
    for stale, expected in (
        ('<span class="badge">Free tier 1k calls/day</span>', 1000),
        ("Free tier: 1,000 calls/day, no signup required", 1000),
        ("free tier limit reached (25 calls/day)", 25),
        ("Free tier 100 calls / day", 100),
        ("FREE TIER 5 CALL/DAY", 5),
    ):
        assert expected in _stated_free_tier_quotas(stale), (
            f"FREE_TIER_QUOTA_RE no longer reads {expected} out of {stale!r} — "
            f"the free-tier over-claim blind spot is back ({FIXWAVE})."
        )

    canonical_n = CANONICAL["free_calls"]
    for clean in (
        f'<span class="badge">Free tier {canonical_n} calls/day</span>',
        f"Free tier: {canonical_n} calls/day, no signup required",
        # PAID quotas — correct, and none of them is a free-tier claim.
        "Developer plan ($49/mo) gives you 1,000 calls/day with full data",
        "PRO: 2,000 calls/day + multi-site comparator + alerts",
        "includes: 500 calls/day, full facility data, coordinates",
        "Identify with an email -> 50 calls/day",
        # A free-tier mention with NO quota attached must not be swept in.
        "Free tier: all 82 tools available, truncated results",
    ):
        assert not [n for n in _stated_free_tier_quotas(clean)
                    if n != canonical_n], (
            f"FREE_TIER_QUOTA_RE false-positives on correct text {clean!r} — "
            f"the paid lanes legitimately carry 1,000/2,000/500 calls/day and "
            f"flagging them would bury the real hit ({FIXWAVE})."
        )


def test_free_path_quota_shape_is_not_vacuous():
    """FREE_PATH_QUOTA_RE must FIRE on both claims the "free tier" anchor could
    not structurally reach, and stay silent on every correct line near them.

    The two STALE entries marked ``live`` are verbatim /mcp landing-page text
    that shipped while FREE_TIER_QUOTA_RE, the tool-count guards, BANNED_STALE
    and the placeholder census were all green over that same blob. Each defeated
    the narrow pattern a different way — one by word order plus intervening
    markup, one by never saying "free" at all — which is why the replacement is
    bidirectional AND tag-stripping AND vocabulary-widened rather than a
    one-word alternation.
    """
    for stale, expected, why in (
        ('<strong>Want higher limits?</strong> For 1k calls/day, add a header in '
         'the connector setup: <code>X-API-Key: your-key</code> &mdash; '
         '<a href="/signup?next=/onboarding">get a free key here</a>.',
         1000, "live: number before anchor, ~60 chars of markup between"),
        ("<li>Skip the auth section &mdash; we don't use OAuth. Tools work "
         "anonymously at 5 calls/day.</li>",
         5, "live: free-path claim containing no 'free'"),
        ("get a free key for 200 calls/day", 200, "anchor-first, plain text"),
        ("Keyless callers get 1k calls/day", 1000, "keyless vocabulary"),
        ("100 calls/day, no signup required", 100, "no-signup vocabulary"),
        ('<span class="badge">Free tier 1k calls/day</span>',
         1000, "the #2062 badge — broad layer must catch it too"),
    ):
        assert expected in _stated_free_path_quotas(stale), (
            f"FREE_PATH_QUOTA_RE no longer reads {expected} out of {stale!r} "
            f"({why}) — that free-path blind spot is back ({FIXWAVE})."
        )

    ok = (CANONICAL["free_calls"], CANONICAL["identified_calls"])
    for clean in (
        # The corrected /mcp funnel copy, rendered. BOTH unpaid figures appear
        # in it, which is why this layer admits two values rather than one.
        f'<strong>Want higher limits?</strong> Anonymous callers and unbound '
        f'free keys both get {ok[0]} calls/day &mdash; the key alone is not the '
        f'upgrade, the email bind is. Bind an operator email and that same free '
        f'key moves to {ok[1]} calls/day, still free: call the '
        f'<code>bind_email</code> tool, or <a href="/signup">sign up here</a>. '
        f'Paid plans go higher &mdash; see <a href="/pricing">pricing</a>.',
        f"<li>Tools work anonymously at {ok[0]} calls/day.</li>",
        # PAID figures with no free-path token anywhere near them.
        "Developer plan ($49/mo) gives you 1,000 calls/day with full data",
        "PRO: 2,000 calls/day + multi-site comparator + alerts",
        "includes: 500 calls/day, full facility data, coordinates",
        "Identify with an email -> 50 calls/day",
        "Free tier: all 82 tools available, truncated results",
        # The sentence bound doing its job: 500 is a correct PAID figure sitting
        # one clause after a free-tier claim, in a string that really ships.
        f"⚠️ DC Hub free tier limit reached ({ok[0]} calls/day). The "
        f"user can unlock 500 calls/day with a Developer key at "
        f"dchub.cloud/developers",
    ):
        assert not [n for n in _stated_free_path_quotas(clean) if n not in ok], (
            f"FREE_PATH_QUOTA_RE false-positives on correct text {clean!r} "
            f"(read {_stated_free_path_quotas(clean)}, allowed {list(ok)}). The "
            f"paid lanes legitimately carry 500/1,000/2,000 calls/day, and a "
            f"fence that cries wolf on the correct upgrade blurb gets disabled "
            f"({FIXWAVE})."
        )


def test_surfaces_advertise_canonical_tool_count():
    """Every '<n> tools' mention on a SURFACE must be the canonical 79, and at
    least one surface must actually advertise it (so the fence isn't vacuous).

    Catches the exact regression from this session (48 -> 79) AND any future
    non-canonical tool count (e.g. a stray '80 tools'), not just the enumerated
    stale tokens. Matches '79 tools', '79 live tools', '79 MCP tools', plus the
    alternate phrasings '(79 total' and '79-tool' (see TOOL_ALT_COUNT_RE). Does
    not match the shields.io badge 'tools-79' (number trails the word) or the
    JSON key '"tools":' (no leading number) — neither states a human count.
    """
    failures = []
    canonical_seen = False
    for rel, i, line in _surface_lines():
        for n in _stated_tool_counts(line):
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
    tool_counts = _stated_tool_counts(desc)
    assert tool_counts, (
        f"worker.js MCP card states no tool count — the guard would pass "
        f"vacuously ({FIXWAVE}): {desc[:120]!r}"
    )
    bad_counts = sorted({c for c in tool_counts if c != CANONICAL["tools"]})
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
    failures = []
    canonical_tool_count_seen = False
    for rel, i, line in _iter_surface_lines(AGENT_CODE_SURFACES):
        low = line.lower()
        for n in _stated_tool_counts(line):
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


# ── BARE-INT TWINS (2026-08-20) ──────────────────────────────────────────────
#
# ★Every pattern in BANNED_STALE is written for PROSE. "facilities_stale_floor"
#  is `(?:19|20|21|22|23),\d{3}\+` — it requires the thousands COMMA and the
#  trailing PLUS. So the fence names 21,000+ as banned and the JSON int 21000
#  sails straight past the guard that names it.
#
# That is not hypothetical. routes/agent_capabilities_feed.py shipped
#
#     counts = {"facilities": 21000, "markets_scored": 232, ...}
#
# as the DB-down fallback of a CC-BY feed built to be quoted, and every fence in
# this file was green on it. `markets_232` happened to catch its neighbour only
# because that pattern writes the plus as OPTIONAL (`232\+?`) — an accident of
# one token's authoring, not a property of the fence.
#
# ★Matched over the AST, not the line. A dict entry can be split across lines,
#  and this file has already been burned once by a fence that read adjacency
#  (§ the _TOOL_COUNT = 59 refreeze: the noun and the digits sat on opposite
#  sides of a template seam and every adjacency pattern read clean). Key and
#  value are one AST node pair no matter how they are formatted.
#
# ★Keyed on the DICT KEY, which is what makes this safe to state as bare ints.
#  A free-floating 21000 in code is a buffer size; `"facilities": 21000` is a
#  claim. The key requirement is doing the same work `requires` does in
#  BANNED_STALE — it is the reason this can name numbers as small as 232
#  without colliding with line numbers, ports and limits.
#
# ★MAINTENANCE CONTRACT, identical to BANNED_STALE's: these bands are RETIRED
#  values. When the verified fleet genuinely crosses 19,000, re-base the
#  facilities band upward in the same commit that moves canon — exactly as the
#  prose pattern above it must be re-based. A band left behind turns a true
#  number into a failure; that is the intended cost of naming values.
_CANON_KEY_TOKENS = ("facilit", "market", "deal", "tool", "countr")

_STALE_INT_TWINS = (
    (
        "facilities_bare_int",
        "facilit",
        frozenset(range(19000, 24000)) | {12650, 15000, 15700, 17000, 18000},
        CANON_FACILITIES_PHRASE,
        "raw-row discovery pile / retired floors as a bare int — the prose twin "
        "is BANNED_STALE 'facilities_stale_floor', which needs the comma+plus.",
    ),
    (
        "markets_bare_int",
        "market",
        frozenset({232, 311, 320, 330}),
        "300+ markets",
        "232 is pre-intl-expansion; 311/320/330 count score ROWS, not scored "
        "markets (canonical is COUNT(DISTINCT market_name) minus 3 aggregates).",
    ),
    (
        "deals_bare_int",
        "deal",
        frozenset({4000, 4275, 1972, 2097}),
        "1,400+ tracked deals",
        "row counts over-state deals ~2.9x (the AUTO id embeds the ingest date, "
        "so one deal accrues a row per day).",
    ),
    (
        "tools_bare_int",
        "tool",
        frozenset({29, 40, 48, 53, 59, 60, 73, 80, 81}),
        "82 tools",
        "every previously-advertised catalog size, incl. the _TOOL_COUNT = 59 "
        "refreeze and the 73 a partner AI found on /connect from the outside.",
    ),
)


def _canon_keyed_int_literals(rel, src):
    """Yield (lineno, key, value) for every `"<canon-ish key>": <int literal>`
    dict entry in `src`. AST-based: formatting and line seams cannot hide one.

    Booleans are excluded — `bool` is a subclass of `int` in Python, so a plain
    isinstance check would report `"facilities": True` as the integer 1.
    """
    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError:  # a file that cannot parse is a different test's problem
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            key = k.value.lower()
            if not any(t in key for t in _CANON_KEY_TOKENS):
                continue
            if isinstance(v, ast.Constant) and isinstance(v.value, int) \
                    and not isinstance(v.value, bool):
                yield v.lineno, k.value, v.value


def _scan_bare_int_twins(rel, src):
    """The guard's whole decision, factored out so the must-fire control below
    can drive it with the exact bytes that shipped instead of re-implementing
    the check (a control that re-implements proves nothing about the guard)."""
    out = []
    for lineno, key, val in _canon_keyed_int_literals(rel, src):
        low = key.lower()
        for tok_id, requires, banned, canonical_phrase, why in _STALE_INT_TWINS:
            if requires not in low:
                continue
            if val in banned:
                out.append(
                    f"  [{tok_id}] {rel}:{lineno}: {key!r}: {val} contradicts "
                    f"canonical '{canonical_phrase}' — {why}"
                )
    return out


def test_canon_keyed_int_literals_are_not_retired_values():
    """A retired count must not re-enter agent-facing code as a bare int.

    Closes the evasion described above: BANNED_STALE's numeric patterns all
    require prose punctuation (the comma and the plus), so the JSON/dict int
    forms of the very values they name pass them.
    """
    failures = []
    for rel in AGENT_CODE_SURFACES:
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            failures.extend(_scan_bare_int_twins(rel, fh.read()))
    assert not failures, (
        "Retired count(s) re-frozen as bare ints in agent-facing code — these "
        f"evade the prose patterns in BANNED_STALE ({FIXWAVE}):\n"
        + "\n".join(failures)
        + "\n\nDerive the value instead of seeding it: canonical_stats."
        "get_canonical_stats() is the dict ai_surface_canon.resolve_canon() "
        "reads, so binding to it makes a surface agree with "
        "/api/v1/canon/phrases by construction."
    )


def test_bare_int_scanner_fires_on_the_literal_that_actually_shipped():
    """Must-fail control. A guard that cannot fail is not a guard.

    Drives _scan_bare_int_twins with the exact dict routes/agent_capabilities_feed.py
    served until 2026-08-20, and with the derived form that replaced it.
    """
    shipped = (
        "counts = {\n"
        '    "facilities":       21000,\n'
        '    "markets_scored":   232,\n'
        '    "deals_tracked":    1972,\n'
        "}\n"
    )
    hits = _scan_bare_int_twins("probe.py", shipped)
    ids = {h.split("]")[0].lstrip(" [") for h in hits}
    assert ids == {"facilities_bare_int", "markets_bare_int", "deals_bare_int"}, (
        f"scanner no longer reads all three retired ints out of the dict that "
        f"shipped — got {sorted(ids)} from {len(hits)} hit(s). The guard has "
        "stopped protecting the defect it was written for."
    )

    # ...and the fix must be clean, or the guard is unusable rather than strict.
    derived = (
        "counts = {\n"
        '    "facilities":       int(_cs.get("facilities_verified") or 0),\n'
        '    "markets_scored":   int(_cs.get("markets") or 0),\n'
        '    "deals_tracked":    int(_cs.get("deals") or 0),\n'
        "}\n"
    )
    assert not _scan_bare_int_twins("probe.py", derived), (
        "scanner false-positives on values that DERIVE — it would block the fix."
    )

    # A canon-keyed int that is NOT a retired value must pass: this fence names
    # specific retired numbers, it does not ban seeded fallbacks as a shape.
    assert not _scan_bare_int_twins("probe.py", 'x = {"markets": 300}\n'), (
        "scanner fires on the CURRENT canonical markets floor — it would block "
        "routes/competitive_seo.py's documented, canon-overridden fallback."
    )

    # Key-scoping must actually scope: the same integer under a non-canon key is
    # a buffer size, not a claim.
    assert not _scan_bare_int_twins("probe.py", 'x = {"chunk_bytes": 21000}\n'), (
        "scanner ignores the dict key — it will collide with limits and sizes."
    )

    # bool is a subclass of int; `True` must not be read as the integer 1.
    assert not _scan_bare_int_twins("probe.py", 'x = {"facilities": True}\n'), (
        "scanner treats a bool as an int."
    )


_FACILITY_FIGURE_RE = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+)\+")

# ── KNOWN-STALE, deliberately not fixed in the .py sweep (2026-08-18).
#    These are served DATA bodies / static assets, not canon plumbing: they do
#    not render through canon_text(), so fixing them is an edit to generated
#    artifacts + the CF edge and belongs in its own PR with its own deploy
#    verification. Listed here so the debt is VISIBLE and guarded rather than
#    silently outside the fence.
#
#    test_pending_facility_surfaces_still_need_fixing below asserts each entry
#    STILL violates, so this list cannot quietly rot into a permanent hole: fix
#    the file and that test fails until you delete its line here. ─────────────
_FACILITY_FLOOR_PENDING = {
    "llms.txt",
    "llms-full.txt",
    "README.md",
    os.path.join(".well-known", "mcp.json"),
}


def _facility_floor_violations(paths):
    """(rel, lineno, token, line) for facility figures under the canon floor.

    Comment lines are skipped: the canon sweeps deliberately quote the retired
    numbers in comments to explain what was wrong, and that prose must stay
    legal — the same carve-out tests/test_canon_placeholders_resolved.py makes.
    """
    floor = _floor_int(CANON_FACILITIES)
    for rel, i, line in _iter_surface_lines(paths):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
            continue
        if "facilit" not in line.lower():
            continue
        for m in _FACILITY_FIGURE_RE.finditer(line):
            val = int(m.group(1).replace(",", ""))
            # Only judge figures in the facility-count magnitude; a line may also
            # carry "1,800+ deals" or "9,000+ substations".
            if val < 10_000 or val >= floor:
                continue
            yield rel, i, m.group(0), line


def test_no_facility_figure_below_the_canon_floor():
    """★2026-08-18: a facility floor BELOW the canon is stale, whatever it is.

    Every previous entry in BANNED_STALE names one retired value, which is why
    each floor move needed a new entry and why "15,000+" survived three moves
    (15,000 -> 15,700 -> 17,000 -> 18,000 -> 18,300) in ~160 places: the fence
    only ever knew the value BEFORE last. This derives the rule instead —
    anything under the current canon floor fails, so when the floor next moves
    the value that was correct yesterday becomes banned automatically, with no
    edit here.

    Scoped to lines that actually talk about facilities (`requires`-style), and
    the shared historical allow-list still exempts changelog prose. Over-claims
    are already covered by the facilities_stale_floor range entry above.
    """
    assert _floor_int(CANON_FACILITIES) > 0, (
        "canon facility floor unreadable — this guard would pass vacuously "
        f"({FIXWAVE})"
    )
    scanned = tuple(SURFACES) + tuple(AGENT_CODE_SURFACES)
    failures = [
        f"  {rel}:{i}: {tok!r} is below the canon floor {CANON_FACILITIES} "
        f"-> {line.strip()[:90]!r}"
        for rel, i, tok, line in _facility_floor_violations(scanned)
        if rel not in _FACILITY_FLOOR_PENDING
    ]
    assert not failures, (
        "Facility figure(s) below the canonical floor — derive them from "
        f"ai_surface_canon (canon_text('{{canon_facilities}}')) rather than "
        f"retyping a number ({FIXWAVE}):\n" + "\n".join(failures)
    )


def test_pending_facility_surfaces_still_need_fixing():
    """The exemption list above must not outlive the debt it records.

    Each _FACILITY_FLOOR_PENDING entry is asserted to STILL carry a sub-floor
    facility figure. Fix one and this fails, forcing its removal from the list —
    which is what keeps a temporary carve-out from becoming a permanent blind
    spot the way the single-value BANNED_STALE entries did.
    """
    scanned = tuple(SURFACES) + tuple(AGENT_CODE_SURFACES)
    offending = {rel for rel, _, _, _ in _facility_floor_violations(scanned)}
    fixed = sorted(_FACILITY_FLOOR_PENDING - offending)
    assert not fixed, (
        "These surfaces no longer carry a stale facility floor — delete them "
        f"from _FACILITY_FLOOR_PENDING so the fence guards them for real: {fixed}"
    )


# ── main.py SURGICAL GUARDS (2026-07-31) ─────────────────────────────────────
#
# main.py is 42,000 lines and cannot be line-scanned (see the module docstring),
# so these pin the two blobs in it that an AI agent or a crawler actually reads:
#
#   serve_by_the_numbers   /by-the-numbers — the Railway-origin fallback page.
#                          Its <meta name="description"> and "MCP tools live"
#                          KPI tile published "33 MCP tools" against a live 82.
#                          Not a rare failure path either: canonical_stats emits
#                          no `mcp_tools` key at all, so `_stats.get('mcp_tools')
#                          or 33` ALWAYS took the literal — 33 was the only tool
#                          count this page ever served.
#   api_agents_recommend   /api/agents/recommend — the body of the
#                          get_dchub_recommendation MCP tool, i.e. text agents
#                          quote verbatim. It advertised "80 tools" and "73 MCP
#                          tools" on ADJACENT LINES, plus "4,000+" deals.
#
# Both fence the SHAPE, not a value: the assertion is "renders from canon", so a
# NEW wrong number is caught the same as a previously-retired one. A value-only
# check structurally could not have seen 33 (BANNED_STALE has no such entry).

MAIN_PY = "main.py"


@functools.lru_cache(maxsize=1)
def _main_py_tree():
    """Parse main.py once (~0.3s) and share it across the guards below.

    Tests never IMPORT main.py — it opens DB pools, starts keepalive threads and
    registers ~200 blueprints (see CLAUDE.md) — so the shipped source is read
    with `ast`, the same way the rest of the suite pulls real code out of it.
    """
    src = (REPO_ROOT / MAIN_PY).read_text(encoding="utf-8", errors="ignore")
    return src, ast.parse(src)


def _main_py_func(name: str) -> str:
    """Source text of one top-level function in main.py."""
    src, tree = _main_py_tree()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(
        f"{MAIN_PY}: function {name}() not found — this drift-fence anchors to "
        f"it. If it was renamed or moved, update the guard to follow it (do not "
        f"just drop it) ({FIXWAVE})."
    )


def _main_py_const_node(name: str) -> ast.Assign:
    """The module-level `name = ...` assignment node in main.py.

    _main_py_func cannot reach _MCP_LANDING_HTML_TEMPLATE: it is a module-level
    STRING, not a def — and it is the single largest agent-facing blob in the
    file (~240 lines of HTML served at /mcp to any browser or agent that sends
    Accept: text/html). It advertised "80 tools" in four places and "10 ISOs"
    in its JSON-LD, none of which any fence could see.
    """
    src, tree = _main_py_tree()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return node
    raise AssertionError(
        f"{MAIN_PY}: module-level constant {name} not found — this drift-fence "
        f"anchors to it. If it was renamed or moved, update the guard to follow "
        f"it (do not just drop it) ({FIXWAVE})."
    )


def _main_py_const(name: str) -> str:
    """Source text of one module-level constant assignment in main.py."""
    src, _tree = _main_py_tree()
    return ast.get_source_segment(src, _main_py_const_node(name)) or ""


def _main_py_const_value(name: str):
    """The literal VALUE of a module-level constant in main.py."""
    node = _main_py_const_node(name)
    try:
        return ast.literal_eval(node.value)
    except (ValueError, TypeError, SyntaxError) as exc:  # pragma: no cover
        raise AssertionError(
            f"{MAIN_PY}: {name} is no longer a plain literal ({exc}) — the "
            f"render guard below needs its value ({FIXWAVE})."
        )


@functools.lru_cache(maxsize=1)
def _main_py_canon_render():
    """main.py's own _canon_nums/_canon_text, pulled out with ast and executed.

    Tests never IMPORT main.py (CLAUDE.md), so the two helpers are compiled and
    exec'd standalone against the REAL canon modules — the same technique the
    rest of the suite uses to run shipped code.

    This is what keeps the guards below non-vacuous. A literal-absence check
    passes just as happily on a page that renders "82 tools" as on one whose
    placeholders silently resolve to "" and advertise no count at all — and
    "went count-free by accident" is a real failure mode here, because
    _canon_text is deliberately fail-open. Asserting on the RENDERED output is
    the only form that can tell those two apart.
    """
    src, tree = _main_py_tree()
    ns = {}
    wanted = ("_canon_nums", "_canon_text")
    found = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         f"<{MAIN_PY}:{node.name}>", "exec"), ns)
            found.append(node.name)
    missing = [n for n in wanted if n not in found]
    assert not missing, (
        f"{MAIN_PY}: canon-render helper(s) {missing} not found at module "
        f"scope. Every agent-facing blob in main.py renders its headline "
        f"numbers through these; if they moved, follow them here (do not just "
        f"drop the guard) ({FIXWAVE})."
    )
    return ns


def _assert_blob_canonical(label: str, body: str) -> None:
    """No non-canonical tool count and no BANNED_STALE token in `body`.

    Scanned line-by-line through the same _HISTORICAL_RE allow-list the SURFACE
    tests use, so a comment that RECORDS a retired count ("previously advertised
    80 tools") is not mistaken for a live claim — these blobs sit in code, and
    the comment explaining a fix is right next to it.
    """
    failures = []
    for i, line in enumerate(body.splitlines(), 1):
        if _HISTORICAL_RE.search(line):
            continue
        for n in _stated_tool_counts(line):
            if n != CANONICAL["tools"]:
                failures.append(
                    f"  {label}:{i}: states {n} tools "
                    f"(canonical {CANONICAL['tools']}) -> {line.strip()[:90]!r}")
        for n in _stated_free_tier_quotas(line):
            if n != CANONICAL["free_calls"]:
                failures.append(
                    f"  {label}:{i}: advertises a FREE tier of {n} calls/day "
                    f"(canonical {CANONICAL['free_calls']}; both the REST "
                    f"rate_limit and the MCP mcp_daily lane agree, and the edge "
                    f"worker's MCP_TIERS.free.daily_limit does too) -> "
                    f"{line.strip()[:90]!r}")
        # ★2026-07-31 — the BROAD free-path layer. Catches the two claims the
        # "free tier" anchor above structurally could not see: a quota promised
        # behind "get a free KEY" (number stated before the anchor, and across
        # ~60 chars of markup), and one quoting the anonymous REST rate_limit on
        # an MCP page. Both were live on the /mcp landing page while every other
        # guard over that same blob passed.
        for n in _stated_free_path_quotas(line):
            if n not in (CANONICAL["free_calls"], CANONICAL["identified_calls"]):
                failures.append(
                    f"  {label}:{i}: ties {n} calls/day to an UNPAID access "
                    f"path. The only honest unpaid quotas are "
                    f"{CANONICAL['free_calls']} (anonymous, or a key with no "
                    f"email bound) and {CANONICAL['identified_calls']} (after "
                    f"bind_email). This shipped as \"For 1k calls/day ... get a "
                    f"free key here\" — a free key is tier `free`, i.e. exactly "
                    f"what anonymous already gets. If {n} is a PAID tier's "
                    f"figure, name the plan or link /pricing; do not attach it "
                    f"to a free path -> {line.strip()[:90]!r}")
        low = line.lower()
        for tok_id, pat, canonical_phrase, requires, why in BANNED_STALE:
            if requires and requires.lower() not in low:
                continue
            hit = pat.search(line)
            if hit:
                failures.append(
                    f"  [{tok_id}] {label}:{i}: {hit.group(0)!r} contradicts "
                    f"canonical '{canonical_phrase}' -> {line.strip()[:90]!r}"
                    f"\n        why: {why}")
    assert not failures, (
        f"Stale count(s) in an agent-facing {MAIN_PY} blob — render from "
        f"ai_surface_canon.PINNED instead of hard-coding ({FIXWAVE}):\n"
        + "\n".join(failures)
    )


def test_main_by_the_numbers_tool_count_renders_from_canon():
    """/by-the-numbers must derive its tool count from canon, with NO integer
    literal left in the binding.

    The literal is the whole bug: `mcp_tools = _stats.get('mcp_tools') or 33`
    looks like a defensive fallback but is the only value the page can produce,
    and a count bump has no reason to visit main.py. So this asserts the SHAPE —
    canon reference present, integer constant absent — which no future stale
    number can satisfy.
    """
    body = _main_py_func("serve_by_the_numbers")
    _assert_blob_canonical("serve_by_the_numbers", body)

    tree = ast.parse(body.strip())
    binding = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "mcp_tools"):
            binding = node.value
    assert binding is not None, (
        f"serve_by_the_numbers no longer binds `mcp_tools` — if the KPI tile "
        f"was renamed, follow it here ({FIXWAVE})."
    )
    binding_src = ast.unparse(binding)
    assert "tools_advertised" in binding_src or "tool_manifest" in binding_src, (
        f"serve_by_the_numbers `mcp_tools` no longer reads from "
        f"ai_surface_canon.PINNED -> {binding_src!r}. The /by-the-numbers meta "
        f"description and KPI tile are crawler-facing; render from canon "
        f"(precedent: #2051 ai_interconnection.py) ({FIXWAVE})."
    )
    literals = [n.value for n in ast.walk(binding)
                if isinstance(n, ast.Constant) and isinstance(n.value, int)
                and not isinstance(n.value, bool)]
    assert not literals, (
        f"serve_by_the_numbers `mcp_tools` carries hard-coded integer(s) "
        f"{literals} -> {binding_src!r}. This is the `or 33` shape that shipped "
        f"a count stale by 49: canonical_stats never emits an `mcp_tools` key, "
        f"so a literal here is not a fallback, it is THE published value. Use "
        f"PINNED['tools_advertised'] (falling back to len(tool_manifest), which "
        f"test_fix_closure_shell.py pins equal to it) ({FIXWAVE})."
    )


def test_main_agent_recommend_blurbs_are_canonical():
    """The get_dchub_recommendation blurbs must render every headline number
    from canon.

    This dict is served verbatim to agents. It carried "80 tools" and "73 MCP
    tools" on adjacent lines plus "4,000+" deals — three different wrong answers
    to two questions, in one literal.
    """
    body = _main_py_func("api_agents_recommend")
    _assert_blob_canonical("api_agents_recommend", body)
    assert "tools_advertised" in body or "tool_manifest" in body, (
        f"api_agents_recommend no longer reads the tool count from "
        f"ai_surface_canon.PINNED — a re-hardcoded count would be invisible "
        f"until it went stale, which is how 73 and 80 coexisted ({FIXWAVE})."
    )
    for canon_key in ("facilities", "deals"):
        assert f"'{canon_key}'" in body or f'"{canon_key}"' in body, (
            f"api_agents_recommend no longer reads PINNED['public']"
            f"['{canon_key}'] — that blurb is an MCP tool body ({FIXWAVE})."
        )


# ── main.py, second wave (2026-07-31): the surfaces #2056 left out of scope.
#
#    #2056 fenced /by-the-numbers and the get_dchub_recommendation blurbs and
#    deliberately stopped there, because the rest meant re-rendering served HTML
#    on a hot path. It isn't a hot path: _MCP_LANDING_HTML is a module-level
#    constant, so its substitution happens ONCE at import and the route still
#    returns a plain string.
#
#    What was still stale on these surfaces, all of them agent-facing:
#      _MCP_LANDING_HTML   "80 tools" x4 (meta description, JSON-LD, hero, badge)
#                          + "10 ISOs" in the JSON-LD + "4,000+ tracked M&A deals"
#      _canonical_pricing  "all 33" / "29 of 33 (excludes 4 Pro-only)" / "80 tools"
#                          — three wrong answers to two questions in ONE dict
#      _canonical_mcp_manifest  "4,000+ M&A deals" in the manifest description
#      handle_well_known   "10 ISOs" on the A2A card, "4,000+ transactions"
#      get_ai_platforms_status  "80 tools", hand-typed in TEN separate blurbs
#      mcp_proxy           "Free tier: all 80 tools available" (initialize)
#
#    main.py still does NOT join AGENT_CODE_SURFACES — a whole-file line-scan of
#    42k lines still produces ~19 false positives (Flask "Phase 232" headers,
#    "6 tools in a day" prose, a Glama changelog note, the get_stats 232-floor
#    logic). These are surgical anchors instead, in the same shape as the two
#    #2056 added.
#
#    ★The load-bearing guard is test_main_canon_render_pipeline_is_canonical,
#    which RUNS the renderer. Every other test here checks that literals are
#    absent — and absence is equally satisfied by a page that advertises the
#    canonical count and by one whose placeholders resolved to "" and advertise
#    nothing. _canon_text is deliberately fail-open, so that second state is
#    reachable; only asserting on rendered OUTPUT distinguishes them.

# Module-level string constants in main.py whose bodies reach an agent, with
# the placeholder census each one must keep.
#
# ★2026-07-31 — the census is a COUNT per placeholder, not a presence check, and
# that distinction is the whole guard. The presence form ("does the template
# still contain any {canon_*}?") was GREEN when the free-tier badge was replaced
# by a hard-coded literal of the CORRECT value: the template still had five
# {canon_tools} and a {canon_free_calls} in the JSON-LD, so "some placeholder
# exists" held while the badge had quietly stopped rendering from canon. That is
# the exact shape of the bug this whole wave is about — a number that is right
# today, hard-typed, invisible, and stale the moment canon moves. Found by
# mutation-testing this fence, not by it firing.
#
# If you legitimately reword the page and a mention genuinely goes away, lower
# the number HERE, deliberately — do not delete the entry.
MAIN_CANON_TEMPLATES = (
    # ★2026-07-31 (follow-on): {canon_free_calls} 2 -> 4 and a new
    # {canon_identified_calls}. The two added free_calls mentions are the
    # Claude.ai pane's connector steps, which had hand-typed "5 calls/day" (the
    # anonymous REST rate_limit, quoted on an MCP surface where mcp_daily
    # governs) and "1k calls/day" behind "get a free key" (a free key is 10/day
    # — the same as anonymous). The identified mention is the honest upgrade
    # path those two were pointing at.
    ("_MCP_LANDING_HTML_TEMPLATE", "_MCP_LANDING_HTML",
     {"{canon_tools}": 5, "{canon_facilities}": 2, "{canon_free_calls}": 4,
      "{canon_identified_calls}": 1,
      "{canon_deals}": 1, "{canon_isos}": 1, "{canon_markets}": 1},
     "the /mcp connection landing page, served to any browser or agent that "
     "sends Accept: text/html"),
)

# Top-level functions in main.py that build agent-facing bodies from canon.
MAIN_CANON_RENDERED_FUNCS = (
    ("_canonical_mcp_manifest",
     "/.well-known/mcp.json + /mcp/manifest + /api/v1/mcp/manifest"),
    ("_canonical_pricing",
     "the pricing block embedded in every manifest above"),
    ("handle_well_known",
     "the A2A server card + /.well-known/agent.json"),
    ("serve_tools_manifest",
     "the /tools manifest agents read for endpoint wiring"),
    ("get_ai_platforms_status",
     "/api/v1/ai-platforms/status"),
    ("mcp_proxy",
     "the /mcp initialize `instructions` blob"),
)

_CANON_PLACEHOLDER_RE = re.compile(r"\{canon_[a-z_]+\}")


@pytest.mark.parametrize("const,rendered,census,why", MAIN_CANON_TEMPLATES)
def test_main_canon_templates_are_canonical(const, rendered, census, why):
    """The template carries no stale literal, still CLAIMS every count via a
    placeholder AS OFTEN AS IT DID, and the name the route serves is the
    RENDERED one.

    All three matter. Without the placeholder census, deleting every count would
    pass — and so would swapping any ONE of them back to a hard-coded literal
    (see the census note above). Without the binding check, the template could
    be canon-clean while the route still served a stale sibling constant.
    """
    body = _main_py_const(const)
    _assert_blob_canonical(const, body)

    assert _CANON_PLACEHOLDER_RE.search(body), (
        f"{MAIN_PY}: {const} no longer contains a {{canon_*}} placeholder. It "
        f"is {why} — if the counts were deleted rather than rendered, this "
        f"guard has nothing left to protect and the next stale number will "
        f"land unseen. Render from ai_surface_canon.PINNED ({FIXWAVE})."
    )

    for placeholder, minimum in sorted(census.items()):
        seen = body.count(placeholder)
        assert seen >= minimum, (
            f"{MAIN_PY}: {const} renders {placeholder} {seen}x, was {minimum}x. "
            f"A mention stopped coming from canon. If it was replaced by a "
            f"hard-coded number this is the bug — it is correct today and stale "
            f"the moment ai_surface_canon.PINNED moves, which is how "
            f"'Free tier 1k calls/day' sat on {why} contradicting the same "
            f"page's JSON-LD. If the copy genuinely lost a mention, lower the "
            f"count in MAIN_CANON_TEMPLATES deliberately ({FIXWAVE})."
        )

    binding = _main_py_const(rendered)
    assert "_canon_text(" in binding and const in binding, (
        f"{MAIN_PY}: {rendered} is no longer `_canon_text({const})` -> "
        f"{binding.strip()[:120]!r}. The route serves {rendered}; if that name "
        f"stops being the rendered one, the template's placeholders ship "
        f"verbatim to agents ({FIXWAVE})."
    )


@pytest.mark.parametrize("fn,why", MAIN_CANON_RENDERED_FUNCS)
def test_main_agent_facing_blobs_are_canonical(fn, why):
    """No stale tool count or BANNED_STALE token in an agent-facing main.py
    function body."""
    _assert_blob_canonical(fn, _main_py_func(fn))


@pytest.mark.parametrize("fn,why", MAIN_CANON_RENDERED_FUNCS)
def test_main_agent_facing_blobs_render_from_canon(fn, why):
    """...and each must still READ canon, so the fix cannot be "delete the
    number".

    The shape check is what catches a count that has never been wrong before —
    the failure BANNED_STALE structurally cannot see, because it can only ban a
    value that has already shipped wrong once.
    """
    body = _main_py_func(fn)
    assert "_canon_text(" in body or "_canon_nums(" in body, (
        f"{MAIN_PY}: {fn}() no longer renders through _canon_text/_canon_nums, "
        f"so its headline numbers are hard-coded again. It serves {why} "
        f"({FIXWAVE})."
    )


def test_main_canon_render_helper_reads_pinned():
    """_canon_nums is the one place these numbers enter main.py — it must read
    the canon SoT and carry no integer literal of its own.

    Same shape assertion as #2056's `mcp_tools` binding, for the same reason: a
    literal here would look like a defensive fallback while being the only value
    six surfaces can produce.
    """
    body = _main_py_func("_canon_nums")
    for token in ("tools_advertised", "tool_manifest", "ai_surface_canon"):
        assert token in body, (
            f"{MAIN_PY}: _canon_nums() no longer references {token!r} — the "
            f"tool count must come from ai_surface_canon.PINNED, with "
            f"len(tool_manifest) (which test_fix_closure_shell.py pins equal "
            f"to tools_advertised) as the only fallback ({FIXWAVE})."
        )
    for canon_key in ("facilities", "deals", "markets"):
        assert f"'{canon_key}'" in body or f'"{canon_key}"' in body, (
            f"{MAIN_PY}: _canon_nums() no longer reads PINNED['public']"
            f"['{canon_key}'] ({FIXWAVE})."
        )
    literals = [n.value for n in ast.walk(ast.parse(body))
                if isinstance(n, ast.Constant) and isinstance(n.value, int)
                and not isinstance(n.value, bool)]
    assert not literals, (
        f"{MAIN_PY}: _canon_nums() carries hard-coded integer(s) {literals}. "
        f"Every surface in this file renders through it, so a literal here is "
        f"not a fallback — it is THE published number on all of them "
        f"({FIXWAVE})."
    )


def test_main_canon_render_pipeline_is_canonical():
    """END-TO-END: run main.py's own renderer and assert the OUTPUT is canon.

    The anti-vacuous guard for everything above. Executes the shipped
    _canon_nums/_canon_text against the real canon modules, renders the real
    landing-page template, and checks the result actually advertises the
    canonical numbers — so "the placeholders quietly resolve to empty strings"
    fails here even though every literal-absence check above still passes.
    """
    ns = _main_py_canon_render()
    nums = ns["_canon_nums"]()

    assert nums["{canon_tools}"] == str(CANONICAL["tools"]), (
        f"{MAIN_PY}: _canon_nums() renders tools={nums['{canon_tools}']!r}, "
        f"canonical is {CANONICAL['tools']} ({FIXWAVE})."
    )
    assert nums["{canon_isos}"] == str(CANONICAL["isos"]), (
        f"{MAIN_PY}: _canon_nums() renders isos={nums['{canon_isos}']!r}, "
        f"canonical is {CANONICAL['isos']} US ISOs ({FIXWAVE})."
    )
    # .get(), not [] — a MISSING key and a WRONG value are both real failures
    # here, and a raw KeyError would tell the next person nothing about which
    # of the two happened or where to fix it.
    assert nums.get("{canon_free_calls}") == str(CANONICAL["free_calls"]), (
        f"{MAIN_PY}: _canon_nums() renders free_calls="
        f"{nums.get('{canon_free_calls}')!r}, canonical is "
        f"{CANONICAL['free_calls']} calls/day. The key must read "
        f"ai_surface_canon.PINNED['free_tier_calls_per_day']; if it is absent "
        f"entirely, the /mcp landing page ships a literal "
        f"'{{canon_free_calls}}' to agents ({FIXWAVE})."
    )
    assert nums.get("{canon_identified_calls}") == str(CANONICAL["identified_calls"]), (
        f"{MAIN_PY}: _canon_nums() renders identified_calls="
        f"{nums.get('{canon_identified_calls}')!r}, canonical is "
        f"{CANONICAL['identified_calls']} calls/day. The key must read "
        f"ai_surface_canon.PINNED['identified_calls_per_day']; if it is absent "
        f"entirely, the /mcp landing page ships a literal "
        f"'{{canon_identified_calls}}' to agents in the one sentence that tells "
        f"them binding an email is worth doing ({FIXWAVE})."
    )
    for ph, canon_key in (("{canon_facilities}", "facilities"),
                          ("{canon_deals}", "deals"),
                          ("{canon_markets}", "markets"),
                          ("{canon_countries}", "countries")):
        assert nums[ph] == PINNED["public"][canon_key], (
            f"{MAIN_PY}: _canon_nums() renders {ph}={nums[ph]!r} != "
            f"ai_surface_canon.PINNED['public'][{canon_key!r}]="
            f"{PINNED['public'][canon_key]!r} ({FIXWAVE})."
        )

    for const, _rendered, _census, why in MAIN_CANON_TEMPLATES:
        out = ns["_canon_text"](_main_py_const_value(const))
        leftover = _CANON_PLACEHOLDER_RE.findall(out)
        assert not leftover, (
            f"{MAIN_PY}: rendering {const} left {sorted(set(leftover))} "
            f"unsubstituted — those ship verbatim to agents on {why}. Add the "
            f"key to _canon_nums() ({FIXWAVE})."
        )
        _assert_blob_canonical(f"{const} (RENDERED)", out)
        assert f"{CANONICAL['tools']} tools" in out, (
            f"{MAIN_PY}: the rendered {const} does not advertise "
            f"'{CANONICAL['tools']} tools' anywhere. Either the count vanished "
            f"(fail-open resolved to an empty string) or the page stopped "
            f"stating it — both make every literal-absence guard above vacuous "
            f"({FIXWAVE})."
        )
        # ★2026-07-31 — same anti-vacuous argument, for the free-tier quota.
        # _assert_blob_canonical above only proves no WRONG figure is present,
        # and _canon_text is fail-open: "Free tier {canon_free_calls} calls/day"
        # with a missing canon key renders "Free tier  calls/day", which states
        # nothing and passes every absence check. Only the rendered output can
        # tell "correct" from "silently blank" apart.
        assert f"Free tier {CANONICAL['free_calls']} calls/day" in out, (
            f"{MAIN_PY}: the rendered {const} does not carry the hero badge "
            f"'Free tier {CANONICAL['free_calls']} calls/day'. It shipped as "
            f"'Free tier 1k calls/day' — a 100x over-claim four lines below the "
            f"same page's JSON-LD stating the canonical figure — so this page "
            f"must state the free quota, and state it once, from canon. If the "
            f"badge was reworded, follow it here; if it resolved to an empty "
            f"string, {const} lost its {{canon_free_calls}} key ({FIXWAVE})."
        )
        assert out.count(f"{CANONICAL['free_calls']} calls/day") >= 2, (
            f"{MAIN_PY}: the rendered {const} states the free-tier quota fewer "
            f"than twice — the hero badge AND the JSON-LD SoftwareApplication "
            f"offer both advertise it, and they contradicted each other (1k vs "
            f"10) precisely because they were two hand-typed literals. Both "
            f"must render from {{canon_free_calls}} ({FIXWAVE})."
        )
        # ★2026-07-31 (follow-on) — the funnel sentence, same anti-vacuous
        # argument. _assert_blob_canonical proves no WRONG unpaid figure is
        # present, and a fail-open resolve to "" satisfies that trivially while
        # deleting the reason to bind an email. Only rendered output separates
        # "correct" from "silently blank".
        assert f"{CANONICAL['identified_calls']} calls/day" in out, (
            f"{MAIN_PY}: the rendered {const} never states the email-bound "
            f"quota '{CANONICAL['identified_calls']} calls/day'. That sentence "
            f"replaced a live over-claim ('For 1k calls/day ... get a free "
            f"key') and is the page's ONLY honest upgrade path short of "
            f"payment — a free key on its own is "
            f"{CANONICAL['free_calls']}/day, exactly what anonymous already "
            f"gets. If it resolved to an empty string, {const} lost its "
            f"{{canon_identified_calls}} key ({FIXWAVE})."
        )
        # The page must not re-acquire a paid figure on a free path. This is the
        # EXECUTED twin of the _assert_blob_canonical scan above: the template
        # source states these numbers as placeholders, so only the rendered
        # output can be checked for the values agents actually read.
        stray = [n for n in _stated_free_path_quotas(out)
                 if n not in (CANONICAL["free_calls"],
                              CANONICAL["identified_calls"])]
        assert not stray, (
            f"{MAIN_PY}: the RENDERED {const} ties {sorted(set(stray))} "
            f"calls/day to an unpaid access path. Only "
            f"{CANONICAL['free_calls']} (anonymous / unbound key) and "
            f"{CANONICAL['identified_calls']} (email bound) are honest there; "
            f"paid figures belong behind a named plan or a /pricing link "
            f"({FIXWAVE})."
        )


def test_main_canonical_pricing_tool_totals_are_canonical():
    """EXECUTED guard over _canonical_pricing() — every tier string must state
    the canonical TOTAL, and the gated split must be arithmetic on it.

    This exists because the line-scan structurally CANNOT see these strings.
    Every tool-count pattern in this file needs the word "tools" right after the
    digits, and these are "all 33 (preview)" / "29 of 33 (excludes 4 Pro-only)"
    — a bare total with no noun. Restoring "all 33 incl Pro-only" here passes
    _assert_blob_canonical, passes the render-from-canon shape check (the dict
    has other _canon_text calls, so per-function granularity is too coarse for
    nine independent strings), and is not a BANNED_STALE value. It was found by
    mutation-testing this fence, not by the fence firing.

    Asserting the exact rendered forms is deliberate: it pins BOTH halves of
    "N of M", so a future edit cannot fix the total and leave the subset stale.
    """
    from routes.mcp_tool_catalog import PRO_ONLY_TOOLS  # authoritative gate list

    src, tree = _main_py_tree()
    ns = dict(_main_py_canon_render())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_canonical_pricing":
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         f"<{MAIN_PY}:_canonical_pricing>", "exec"), ns)
    assert "_canonical_pricing" in ns, (
        f"{MAIN_PY}: _canonical_pricing() not found at module scope — it is the "
        f"pricing block of every published MCP manifest ({FIXWAVE})."
    )
    pricing = ns["_canonical_pricing"]()

    total = CANONICAL["tools"]
    n_pro = len(PRO_ONLY_TOOLS)
    expected_split = f"{total - n_pro} of {total} (excludes {n_pro} Pro-only)"

    for tier in ("free", "pro", "enterprise"):
        got = pricing[tier]["tools_unlocked"]
        assert got.startswith(f"all {total}"), (
            f"{MAIN_PY}: _canonical_pricing()['{tier}']['tools_unlocked']="
            f"{got!r} does not lead with the canonical total 'all {total}'. "
            f"This is published in /.well-known/mcp.json ({FIXWAVE})."
        )
    for tier in ("identified", "starter", "developer"):
        got = pricing[tier]["tools_unlocked"]
        assert got == expected_split, (
            f"{MAIN_PY}: _canonical_pricing()['{tier}']['tools_unlocked']="
            f"{got!r} != {expected_split!r}. Both halves are derived — the "
            f"total from ai_surface_canon.PINNED, the gate size from "
            f"routes.mcp_tool_catalog.PRO_ONLY_TOOLS. If the Pro gate changed, "
            f"it changed in PRO_ONLY_TOOLS and this follows automatically; a "
            f"hand-typed split here is the drift ({FIXWAVE})."
        )
    for key, value in pricing["legacy_strings"].items():
        if "tool" not in value.lower():
            continue
        assert str(total) in value, (
            f"{MAIN_PY}: _canonical_pricing()['legacy_strings']['{key}']="
            f"{value!r} states a tool count that is not the canonical "
            f"{total} ({FIXWAVE})."
        )
        _assert_blob_canonical(f"_canonical_pricing legacy_strings[{key}]", value)


def test_main_canonical_pricing_legacy_strings_state_the_correct_price():
    """EXECUTED guard: a tier's legacy_strings blurb must quote the SAME price
    as that tier's own price_usd_month — both live in the same dict, returned
    by the same function call, in the same request.

    2026-08-15 weekly accuracy check found legacy_strings['pro'] hand-typed
    '$199/mo' three lines below price_usd_month=299 in _canonical_pricing()
    itself — served live on /.well-known/mcp.json and the MCP manifest. Two
    numbers for the same tier, in the same dict literal, from one function.
    Mutation-tested: reverting the fix (legacy_strings['pro'] back to
    '$199/mo') fails this test; the fixed source passes it.
    """
    import re

    _, tree = _main_py_tree()
    ns = dict(_main_py_canon_render())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_canonical_pricing":
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         f"<{MAIN_PY}:_canonical_pricing>", "exec"), ns)
    pricing = ns["_canonical_pricing"]()

    for tier, blurb in pricing["legacy_strings"].items():
        tier_info = pricing.get(tier)
        if not isinstance(tier_info, dict) or "price_usd_month" not in tier_info:
            continue
        expected_price = tier_info["price_usd_month"]
        m = re.search(r"\$(\d+)/mo", blurb)
        if not m:
            # Free-tier blurbs legitimately state no price at all.
            continue
        stated_price = int(m.group(1))
        assert stated_price == expected_price, (
            f"{MAIN_PY}: _canonical_pricing()['legacy_strings']['{tier}'] "
            f"quotes ${stated_price}/mo but _canonical_pricing()['{tier}']"
            f"['price_usd_month']={expected_price} — same dict, same call, "
            f"two prices for one tier ({FIXWAVE})."
        )


def test_agents_md_pro_price_matches_canonical_pricing():
    """The live /AGENTS.md page (routes/agents_md_fallback._render_agents_md)
    hand-types its per-tier prices instead of rendering them from
    ai_surface_canon.PINNED — unlike facility/deal/tool counts, which the
    module's own docstring says are canon-derived, pricing is not. 2026-08-15
    weekly accuracy check found it still quoting '$199/mo' for Pro while
    _canonical_pricing()['pro']['price_usd_month'] (the same figure served on
    /.well-known/mcp.json) is 299. Mutation-tested: reverting the AGENTS.md
    fix back to '$199/mo' fails this test.
    """
    import re
    from routes.agents_md_fallback import _render_agents_md

    _, tree = _main_py_tree()
    ns = dict(_main_py_canon_render())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_canonical_pricing":
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         f"<{MAIN_PY}:_canonical_pricing>", "exec"), ns)
    pro_price = ns["_canonical_pricing"]()["pro"]["price_usd_month"]

    body = _render_agents_md()
    m = re.search(r"\*\*Pro \(\$(\d+)/mo\)\*\*", body)
    assert m, (
        "routes/agents_md_fallback.py: /AGENTS.md's '**Pro ($N/mo)**' line "
        f"not found — this guard anchors to it ({FIXWAVE})."
    )
    stated_price = int(m.group(1))
    assert stated_price == pro_price, (
        f"routes/agents_md_fallback.py: /AGENTS.md advertises Pro at "
        f"${stated_price}/mo but _canonical_pricing()['pro']"
        f"['price_usd_month']={pro_price} (served on /.well-known/mcp.json "
        f"for the same tier) ({FIXWAVE})."
    )


def _main_py_exec_funcs(*names):
    """ast-exec the named top-level main.py functions over the canon-render ns.

    Same standalone-exec technique as _main_py_canon_render (tests never import
    main.py — CLAUDE.md); this just carries a dependency chain, since
    _well_known_tool_gate() calls _canonical_pricing() calls _canon_text().
    """
    src, tree = _main_py_tree()
    ns = dict(_main_py_canon_render())
    found = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         f"<{MAIN_PY}:{node.name}>", "exec"), ns)
            found.append(node.name)
    missing = [n for n in names if n not in found]
    assert not missing, (
        f"{MAIN_PY}: {missing} not found at module scope — this drift-fence "
        f"anchors to them. If they moved, follow them (do not just drop the "
        f"guard) ({FIXWAVE})."
    )
    return ns


def test_well_known_pricing_matches_canonical_pricing():
    """EXECUTED guard over _well_known_tool_gate() — /.well-known/mcp.json's
    per-tier tool-unlock numbers must EQUAL _canonical_pricing()'s.

    main.py shipped TWO independent pricing tables. _canonical_pricing() was
    derived in #2059; this one stayed hand-typed and stale in three separate
    places — .pricing.tools_unlocked (13/28/"28 of 33"/28/33/"all 33"),
    .tiers.tools_count (13/28/28/46), and a "13 FREE-tier tools" quick_start
    line — against a canonical 82-tool total.

    The free-tier value was wrong in KIND, not just stale: the enforcing server
    gates free by DEPTH, not by tool count (every tool stays in tools/list and
    returns a trimmed preview), so no small integer is a correct answer there.
    That is why this asserts free == the FULL total.

    EXECUTED rather than line-scanned, for the reason
    test_main_canonical_pricing_tool_totals_are_canonical documents: these are
    bare totals with no noun ("all 33", "28 of 33"), and every tool-count regex
    in this file needs the word "tools" right after the digits, so a line-scan
    structurally cannot see them. Restoring any of the six stale literals
    passes _assert_blob_canonical and is not a BANNED_STALE value.
    """
    from routes.mcp_tool_catalog import PRO_ONLY_TOOLS  # authoritative gate list

    ns = _main_py_exec_funcs("_canonical_pricing", "_well_known_tool_gate")
    canonical = ns["_canonical_pricing"]()
    gate = ns["_well_known_tool_gate"](CANONICAL["tools"])
    pricing = gate["pricing"]

    total = CANONICAL["tools"]
    non_pro = total - len(PRO_ONLY_TOOLS)

    # 1. The published SHAPE — types are the /.well-known contract. int for
    #    free/identified/developer/pro, str for starter/enterprise. Migrating
    #    one silently breaks `jq '.pricing.free.tools_unlocked'` consumers.
    expected_types = {"free": int, "identified": int, "starter": str,
                      "developer": int, "pro": int, "enterprise": str}
    for tier, want_type in expected_types.items():
        got = pricing[tier]["tools_unlocked"]
        assert isinstance(got, want_type) and not isinstance(got, bool), (
            f"{MAIN_PY}: /.well-known pricing['{tier}']['tools_unlocked']="
            f"{got!r} is {type(got).__name__}, not {want_type.__name__}. That "
            f"field's TYPE is the published contract — change it only as a "
            f"deliberate migration, not as a side effect ({FIXWAVE})."
        )

    # 2. The numbers themselves, derived from the same two operands as
    #    _canonical_pricing(). free/pro see everything (free at preview depth);
    #    identified/developer are the Pro-gated subset.
    for tier, want in (("free", total), ("identified", non_pro),
                       ("developer", non_pro), ("pro", total)):
        assert pricing[tier]["tools_unlocked"] == want, (
            f"{MAIN_PY}: /.well-known pricing['{tier}']['tools_unlocked']="
            f"{pricing[tier]['tools_unlocked']!r} != {want}. Derived from "
            f"ai_surface_canon.PINNED ({total} tools) and "
            f"routes.mcp_tool_catalog.PRO_ONLY_TOOLS ({len(PRO_ONLY_TOOLS)} "
            f"Pro-only). If the gate changed it changed THERE and this follows "
            f"automatically; a hand-typed count here is the drift ({FIXWAVE})."
        )
    # 2b. The two STRING tiers. Found by mutation-testing this guard: the loop
    #     above only covers the int tiers, so restoring "28 of 33" / "all 33"
    #     here sailed through every other check — type was still str, and the
    #     note field (which IS pinned) is a different key. A bare total with no
    #     noun after it is invisible to every regex in this file, so it has to
    #     be pinned by VALUE, exactly like the int tiers.
    for tier in ("starter", "enterprise"):
        got = pricing[tier]["tools_unlocked"]
        assert got == canonical[tier]["tools_unlocked"], (
            f"{MAIN_PY}: /.well-known pricing['{tier}']['tools_unlocked']="
            f"{got!r} != _canonical_pricing()'s "
            f"{canonical[tier]['tools_unlocked']!r}. This tier publishes a "
            f"STRING, so it must be the canonical string verbatim — a "
            f"hand-typed one here states a total with no noun after it "
            f"('all 33'), which no count regex in this file can see "
            f"({FIXWAVE})."
        )
        assert str(total) in got, (
            f"{MAIN_PY}: pricing['{tier}']['tools_unlocked']={got!r} does not "
            f"state the canonical total {total} ({FIXWAVE})."
        )
    assert gate["tier_tools_count"] == {
        "FREE": total, "IDENTIFIED": non_pro, "DEVELOPER": non_pro, "PRO": total
    }, (
        f"{MAIN_PY}: /.well-known .tiers tools_count={gate['tier_tools_count']!r} "
        f"disagrees with the .pricing block in the SAME document ({FIXWAVE})."
    )

    # 3. ★ The bug this test exists for: the two tables must not restate each
    #    other differently. Every string form is lifted from _canonical_pricing()
    #    verbatim, so equality here is what "one source of truth" means.
    for tier in expected_types:
        assert pricing[tier]["tools_unlocked_note"] == canonical[tier]["tools_unlocked"], (
            f"{MAIN_PY}: the two pricing tables disagree for '{tier}' — "
            f"/.well-known says {pricing[tier]['tools_unlocked_note']!r}, "
            f"_canonical_pricing() says {canonical[tier]['tools_unlocked']!r}. "
            f"These are published as .pricing in DIFFERENT manifests and were "
            f"hand-typed independently for months ({FIXWAVE})."
        )
        for field in ("price_usd_month", "calls_per_day"):
            assert pricing[tier][field] == canonical[tier][field], (
                f"{MAIN_PY}: pricing['{tier}']['{field}'] differs between the "
                f"two tables ({pricing[tier][field]!r} vs "
                f"{canonical[tier][field]!r}) ({FIXWAVE})."
            )

    # 4. Non-vacuity. _canon_text is deliberately fail-open, so a broken canon
    #    import renders every note to "" and turns all of the above into a
    #    tautology. Assert the rendered strings actually STATE the total.
    for tier in expected_types:
        note = pricing[tier]["tools_unlocked_note"]
        assert note and str(total) in note, (
            f"{MAIN_PY}: pricing['{tier}']['tools_unlocked_note']={note!r} does "
            f"not state the canonical total {total} — either canon fail-open "
            f"resolved it to empty or the card stopped stating a count. Both "
            f"make every check above vacuous ({FIXWAVE})."
        )
    for label in ("quick_start_free_tools", "quick_start_identified_tools"):
        value = gate[label]
        assert value and str(total) in value, (
            f"{MAIN_PY}: _well_known_tool_gate()[{label!r}]={value!r} does not "
            f"state the canonical total {total} ({FIXWAVE})."
        )
        _assert_blob_canonical(f"_well_known_tool_gate {label} (RENDERED)", value)


def test_well_known_tool_gate_numbers_are_not_rehardcoded():
    """SHAPE guard: no tool-gating value inside handle_well_known() may be a
    LITERAL again.

    The value guard above only sees what _well_known_tool_gate() returns. Wiring
    that helper up and then re-typing a number back into the served dict — the
    exact regression that produced two tables in the first place — would sail
    past it. So this walks handle_well_known()'s own AST and requires every
    tools_count / tools_unlocked / tools value to be a computed expression.
    """
    src, tree = _main_py_tree()
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "handle_well_known":
            fn = node
    assert fn is not None, (
        f"{MAIN_PY}: handle_well_known() not found — it serves /.well-known/"
        f"mcp.json, the public MCP discovery manifest ({FIXWAVE})."
    )
    guarded = {"tools_count", "tools_unlocked", "tools_unlocked_note", "tools"}
    literals = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in guarded):
                continue
            if isinstance(value, ast.Constant):
                literals.append(
                    f"  line {getattr(value, 'lineno', '?')}: "
                    f"{key.value!r}: {value.value!r}")
    assert not literals, (
        f"{MAIN_PY}: hand-typed tool-gating literal(s) back inside "
        f"handle_well_known(). Every one of these must come from "
        f"_well_known_tool_gate(), which derives them from "
        f"ai_surface_canon.PINNED + routes.mcp_tool_catalog.PRO_ONLY_TOOLS — "
        f"restating a count here is how this card drifted to 13/28/33 against "
        f"a canonical {CANONICAL['tools']} ({FIXWAVE}):\n" + "\n".join(literals)
    )


def test_no_hand_typed_tool_count_anywhere_in_main_py():
    """SHAPE guard, FILE-WIDE: no `tools_count` / `tool_count` / `server_version`
    dict value anywhere in main.py may be a hard-coded literal.

    The guard above is this same idea scoped to handle_well_known(), and that
    scoping is exactly why it missed one. main.py mounts ~200 blueprints' worth
    of routes, and /api/v1/mcp/platforms sat outside BOTH closure waves — outside
    the _canon_text render pipeline (#2059) and outside the AST walk above, which
    only ever descends into handle_well_known. So that endpoint published
    `"tools_count": 46` against a canonical 82 and `"server_version": "2.2.5"`
    against a live 2.5.0, with every fence over main.py green the whole time.

    main.py deliberately stays OUT of AGENT_CODE_SURFACES — line-scanning 42k
    lines yields ~19 false positives off incidental numbers — so this coverage
    has to come from a surgical AST anchor instead. Keyed on the KEY NAME rather
    than on an enclosing function, so the NEXT endpoint that publishes a tool
    count is fenced the day it is written, not the day someone notices it drifted.

    Deliberately narrow. `tools_count`/`tool_count` and `server_version` each
    have exactly one honest source — len(routes.mcp_tool_catalog.
    flat_tools_for_card()) and ai_surface_canon.PINNED["version"] — and neither
    name occurs incidentally anywhere in main.py. A bare `version` key would NOT
    be safe to add here: main.py carries a dozen unrelated schema/protocol/API
    versions ('2024-11-05', 'v92', '1.27.0', 'ai-agents/v2') that are not the MCP
    server version and must not be pinned to canon.

    `None` is allowed: it is the fail-open sentinel the derivations degrade to,
    and a null can never restate the list. Every other constant is a claim.
    """
    _src, tree = _main_py_tree()
    guarded = {"tools_count", "tool_count", "server_version"}
    literals = []
    derived = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in guarded):
                continue
            if isinstance(value, ast.Constant) and value.value is not None:
                literals.append(
                    f"  line {getattr(value, 'lineno', '?')}: "
                    f"{key.value!r}: {value.value!r}")
            else:
                derived.append(getattr(value, "lineno", "?"))
    assert not literals, (
        f"{MAIN_PY}: hand-typed tool-count / server-version literal(s). These "
        f"must be DERIVED, in a try/except that fails open to None:\n"
        f"    from routes.mcp_tool_catalog import flat_tools_for_card\n"
        f"    \"tools_count\": len(flat_tools_for_card())   # canonical "
        f"{CANONICAL['tools']}\n"
        f"    from ai_surface_canon import PINNED\n"
        f"    \"server_version\": PINNED[\"version\"]\n"
        f"A literal here RESTATES the tool list, and the list is the only thing "
        f"that cannot drift against the live server — that is how "
        f"/api/v1/mcp/platforms came to advertise 46 tools and v2.2.5 against a "
        f"live 82 / 2.5.0 ({FIXWAVE}):\n" + "\n".join(literals)
    )
    # Anti-vacuous carrier. A literal-ABSENCE scan is happiest on a file where
    # the keys no longer exist at all: rename `tools_count`, or restructure these
    # responses out of dict literals, and the loop above finds nothing and passes
    # forever while advertising coverage it has lost. Assert the walk is still
    # REACHING real derived values (9 at time of writing, across
    # handle_well_known, _canonical_mcp_manifest and mcp_platforms_status).
    assert len(derived) >= 7, (
        f"{MAIN_PY}: this guard reached only {len(derived)} derived "
        f"tools_count/server_version value(s) (lines {derived}) — it reached 9 "
        f"when written. The scan above is now near-vacuous. Follow the keys to "
        f"wherever they moved; do not drop the guard ({FIXWAVE})."
    )


def test_tool_catalog_length_matches_canon():
    """len(flat_tools_for_card()) == PINNED['tools_advertised'].

    The guard above forces every main.py tool count to be DERIVED from this
    catalog, which is only worth anything if the catalog itself agrees with
    canon — otherwise "derived" just means the endpoint publishes an unfenced
    number instead of a hand-typed one, and the drift moves one layer down
    rather than closing.

    Nothing else in the suite pinned this link: test_fix_closure_shell asserts
    PINNED['tools_advertised'] == len(PINNED['tool_manifest']), which is
    canon-INTERNAL, and the catalog is a third list that was free to move on its
    own. A tool added to routes/mcp_tool_catalog.py without a canon bump would
    have shipped a count no fence reads.
    """
    from routes.mcp_tool_catalog import flat_tools_for_card

    catalog = flat_tools_for_card()
    assert len(catalog) == PINNED["tools_advertised"], (
        f"routes/mcp_tool_catalog.flat_tools_for_card() serves "
        f"{len(catalog)} tools but ai_surface_canon.PINNED['tools_advertised'] "
        f"is {PINNED['tools_advertised']}. Every discovery surface derives its "
        f"count from one of these two, so they must move together: bump canon "
        f"(PINNED['tools_advertised'] AND PINNED['tool_manifest']) in the same "
        f"change that adds or removes the tool ({FIXWAVE})."
    )


def test_frontend_stat_normalizer_matches_canon():
    """frontend_stat_normalizer.CANONICAL must equal the canon, and none of its
    REPLACEMENT VALUES may carry a stale count.

    This module REWRITES frontend HTML, so a stale number here does not sit
    quietly in a file — it gets written into published copy. It shipped with two
    independently-drifted tables: a CANONICAL dict calling itself "single source
    of truth" that nothing read (mcp_tools '79', deals '4,000+', markets '311',
    facilities_number the retired '12650'), and a REPLACEMENTS table that
    actually ran and rewrote pages to "20 MCP Tools" / "20,000+ facilities" /
    "4,000+ deals". Asserted directly rather than line-scanned: its FIND
    patterns must quote retired values by design.
    """
    import frontend_stat_normalizer as fsn  # no main.py import; stdlib + canon

    canon_tools = str(CANONICAL["tools"])
    assert fsn.CANONICAL["mcp_tools"] == canon_tools, (
        f"frontend_stat_normalizer.CANONICAL['mcp_tools']="
        f"{fsn.CANONICAL['mcp_tools']!r} != canonical {canon_tools!r} "
        f"({FIXWAVE})."
    )
    for norm_key, canon_key in (("facilities", "facilities"),
                                ("countries", "countries"),
                                ("markets", "markets")):
        assert fsn.CANONICAL[norm_key] == PINNED["public"][canon_key], (
            f"frontend_stat_normalizer.CANONICAL[{norm_key!r}]="
            f"{fsn.CANONICAL[norm_key]!r} != ai_surface_canon.PINNED['public']"
            f"[{canon_key!r}]={PINNED['public'][canon_key]!r} ({FIXWAVE})."
        )
    assert fsn.CANONICAL["deals_tracked"].startswith(PINNED["public"]["deals"]), (
        f"frontend_stat_normalizer.CANONICAL['deals_tracked']="
        f"{fsn.CANONICAL['deals_tracked']!r} does not lead with the canonical "
        f"deal floor {PINNED['public']['deals']!r} ({FIXWAVE})."
    )

    # Every value this module WRITES must itself be canon-clean — CANONICAL
    # entries and the replacement half of every rewrite rule.
    written = [(f"CANONICAL[{k!r}]", str(v)) for k, v in fsn.CANONICAL.items()]
    written += [(f"REPLACEMENTS[{desc}]", rep)
                for _pat, rep, desc in fsn.REPLACEMENTS]
    written += [(f"PAGE_SPECIFIC_FIXES[{page}][{desc}]", rep)
                for page, rules in fsn.PAGE_SPECIFIC_FIXES.items()
                for _pat, rep, desc in rules]
    for label, value in written:
        _assert_blob_canonical(f"frontend_stat_normalizer {label}", value)


# ── SHADOW_HTML_SURFACES (r-agent-parity 2026-07-31): backend-served HTML
#    copies of agent-facing pages. static/ai.html is the Railway-origin /ai
#    (main.py routes /ai to it — the CF Pages frontend serves live traffic,
#    but the origin shadow is what failover and direct-origin readers get);
#    ai.html at the repo root is its older sibling, which sat at the RETIRED
#    "20,000+" facilities floor until this wave while every fence stayed
#    green — the exact "fence guards a file nobody scans" class from
#    2026-07-25. static/mcp-dashboard.html is the Upgrade Funnel dashboard,
#    one of the three surfaces that published three different "agents (7d)"
#    numbers. Scanned with BANNED_STALE only (same rationale as
#    AGENT_CODE_SURFACES: the stale_markers denylist's bare-number markers
#    would collide with incidental markup/JS). ────────────────────────────────
#    ★2026-08-19: static/connect.html joins — and it is the sharpest example of
#    the class yet, because a fence for it was built in the WRONG REPO. /connect
#    is the highest-intent page on the site, and dchub-frontend/connect.html is a
#    dead 47KB file that nothing serves: _worker.js forwards /connect to the
#    Railway origin (`x-dc-hub-served-by: railway-primary`), /connect.html
#    308-redirects to /connect, and the two documents do not even share a <title>
#    ("Connect to DC Hub - MCP Server Setup" served vs "…— Data Center
#    Intelligence for AI" in the repo). dchub-frontend#1216 measured "THREE tool
#    counts" and wired heal + fence around the dead artifact; the SERVED page was
#    meanwhile publishing "82 live tools" three times and "Available Tools — 73
#    live" once, in the same document, to every partner the agent-note points at.
#    Same basename, different repo — that is the whole trap. Fence the bytes the
#    origin actually sends.
SHADOW_HTML_SURFACES = (
    "ai.html",
    os.path.join("static", "ai.html"),
    os.path.join("static", "mcp-dashboard.html"),
    os.path.join("static", "connect.html"),
)


def test_shadow_html_surfaces_free_of_stale_counts():
    """Backend-served HTML shadows must not carry a banned stale count.

    These files are static (no heal reaches them, no canon placeholder
    renders them), so a retired floor sits forever unless fenced. The live
    frontend copies are healed daily in the dchub-frontend repo; THESE are the
    origin/failover bytes and drift independently — 2026-07-31 the repo-root
    ai.html still said "20,000+ Facilities" (retired 07-24) while the frontend
    said 15,300+.
    """
    failures = []
    for rel, i, line in _iter_surface_lines(SHADOW_HTML_SURFACES):
        low = line.lower()
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
        "Stale count(s) in backend-served HTML shadow(s) — these bytes serve "
        f"at the Railway origin and in failover ({FIXWAVE}):\n"
        + "\n".join(failures)
    )


# ── SERVED-PAGE SELF-CONSISTENCY (2026-08-19) ────────────────────────────────
#
# BANNED_STALE can only catch a value that has ALREADY shipped wrong once, and
# TOOL_COUNT_RE / TOOL_ALT_COUNT_RE only catch counts written digits-first.
# static/connect.html defeated all three at once. Its four tool-count sites are:
#
#   "Unlock all 82 tools"              TOOL_COUNT_RE      ✓ caught
#   "82 live tools"                    TOOL_COUNT_RE      ✓ caught
#   "Available Tools — 73 live"        noun FIRST         ✗ invisible
#   "The full catalog of 82"           no noun at all     ✗ invisible
#
# So the page served 82 three times and 73 once and every fence in this file was
# green. This is the same lesson TOOL_ALT_COUNT_RE was added for on 07-31 and it
# recurred because that fix enumerated the two phrasings then in evidence rather
# than the invariant underneath them.
#
# The invariant IS the fence here: ONE tool count per served document. It cannot
# tell you 82 is right — resolve_canon does that — but it catches a page arguing
# with itself, which is how every defect in this class has actually presented.
# Deterministic, offline, no canon fetch, and phrasing-agnostic on the side that
# matters: a NEW way of writing the count can only ever ADD a value to the set.
#
# Rendered through canon_text() because that is the serve path (main.connect_page
# reads the file and returns Response(_canon_text(...))). Scanning raw bytes
# would compare "{canon_tools}" against itself and pass vacuously.
SERVED_CANON_PAGES = (os.path.join("static", "connect.html"),)

# Plausibility band: a real advertised catalog size. Excludes step numbers,
# pixel values, ports and "the six most-used tools" style subset counts.
_TOOL_BAND = range(40, 201)

_TOOL_SITE_PATTERNS = (
    # digits first — "82 tools", "82 live tools", "82 MCP tools"
    re.compile(r"(?<![\d,])(\d{1,3})\s+(?:live\s+|MCP\s+)?tools\b", re.I),
    # noun first — "Available Tools — 82 live", "Tools: 82"
    re.compile(r"\btools?\b\s*(?:[—–:-]|\()\s*(\d{1,3})\b", re.I),
    # no noun at all — "The full catalog of 82", "all 82 of them"
    re.compile(r"\bcatalog of\s+(\d{1,3})\b", re.I),
    # "(82 total)", "82-tool"
    re.compile(r"\((\d{1,3})\s+total\b|(?<![\d,])(\d{1,3})-tools?\b", re.I),
)

_FACILITY_FLOOR_RE = re.compile(
    r"(?<![\d,])(\d[\d,]*\+)\s*(?:\w+\s+){0,2}?(?:data[ -]?cent\w*|facilit\w+)", re.I
)


def _served_views(rel):
    """The rendered page, plus a tag-blanked view of it.

    Both views are load-bearing. A hero tile splits the number from its noun
    across sibling elements (`<div>73</div><div>MCP Tools</div>`), so it is
    invisible in the raw view; markup attributes carry incidental digits, so the
    blanked view alone would miss counts written inside a tag. Scan both, union
    the results — the frontend's Guard 5 learned this the same way.
    """
    from ai_surface_canon import canon_text

    p = REPO_ROOT / rel
    assert p.is_file(), (
        f"{rel}: served canon page missing — this consistency fence anchors to "
        f"it ({FIXWAVE}). If the file moved, update SERVED_CANON_PAGES to follow "
        f"it (do not just drop the page)."
    )
    rendered = canon_text(p.read_text(encoding="utf-8", errors="replace"))
    return rendered, re.sub(r"<[^>]+>", " ", rendered)


def test_served_pages_publish_one_tool_count():
    """A canon-rendered page must not publish two different tool counts.

    ★ Not "is the number right" — "is the page arguing with itself". /connect
    told partners 82 three times and 73 once, in one response, for weeks.
    """
    failures, total_matched = [], 0
    for rel in SERVED_CANON_PAGES:
        found = {}                                  # count -> set of phrasings
        for view in _served_views(rel):
            for pat in _TOOL_SITE_PATTERNS:
                for m in pat.finditer(view):
                    raw_val = next((g for g in m.groups() if g), None)
                    if raw_val is None or int(raw_val) not in _TOOL_BAND:
                        continue
                    total_matched += 1
                    found.setdefault(raw_val, set()).add(
                        " ".join(m.group(0).split())[:60])
        if len(found) > 1:
            shown = " vs ".join(
                f"{v!r} ({', '.join(sorted(ph))})" for v, ph in sorted(found.items()))
            failures.append(f"  {rel}: {len(found)} different tool counts -> {shown}")
    assert not failures, (
        "A served page publishes more than one tool count. Bind every site to "
        "{canon_tools} — the page is rendered through canon_text() already, so "
        f"a hardcoded literal is a site someone forgot to convert ({FIXWAVE}):\n"
        + "\n".join(failures)
    )
    # ★ NON-VACUITY. These pages carry a tool count today. Zero matches means the
    # patterns stopped reaching the copy, not that the copy became clean — the
    # failure class this whole file exists to catch.
    assert total_matched, (
        f"HARNESS ERROR: matched zero tool counts across {len(SERVED_CANON_PAGES)} "
        "served page(s). The patterns are broken, not the pages clean."
    )


def test_served_pages_publish_one_facility_floor():
    """Same invariant for the facility floor.

    static/connect.html carried the retired pre-dedup "21,000+" twice — in the
    hero lede and in the search_facilities blurb — while canon had rebased to
    DISTINCT SITES. An over-claim, and floors round DOWN.
    """
    failures, total_matched = [], 0
    for rel in SERVED_CANON_PAGES:
        found = {}
        for view in _served_views(rel):
            for m in _FACILITY_FLOOR_RE.finditer(view):
                total_matched += 1
                found.setdefault(m.group(1).replace(" ", ""), set()).add(
                    " ".join(m.group(0).split())[:60])
        if len(found) > 1:
            shown = " vs ".join(
                f"{v!r} ({', '.join(sorted(ph))})" for v, ph in sorted(found.items()))
            failures.append(f"  {rel}: {len(found)} different facility floors -> {shown}")
    assert not failures, (
        "A served page publishes more than one facility floor. Bind every site "
        f"to {{canon_facilities}} ({FIXWAVE}):\n" + "\n".join(failures)
    )
    assert total_matched, (
        f"HARNESS ERROR: matched zero facility floors across "
        f"{len(SERVED_CANON_PAGES)} served page(s) — patterns broken, not pages clean."
    )


def test_rendered_platform_pages_bind_every_count_to_canon():
    """The nine /connect/<client> pages must render canon, not a module literal.

    ★ Companion to the two tests above, for pages that have no file to scan.
    /connect is a static file run through canon_text(); /connect/<client> is
    built by routes.mcp_connect._render_page() from a template, so
    SERVED_CANON_PAGES cannot reach it and neither can any line-scan of the
    module — which is exactly how it broke.

    `_TOOL_COUNT = 59` sat under a comment naming `_mcp_tool_count()` as the
    canonical source, and all nine pages served "59" five times each: 45 stale
    claims, a 23-tool under-claim, while tools/list served 82. Every fence was
    green because the template said "{canon_tools} tools" (a placeholder, no
    digits) and the constant said "= 59" (digits, no noun) — TOOL_COUNT_RE
    needs them adjacent, and a template boundary separated them.

    So assert against the RENDER, which is the only place the two halves meet.
    """
    from ai_surface_canon import canon_nums
    from routes.mcp_connect import _CLIENTS, _render_page

    assert _CLIENTS, "HARNESS ERROR: _CLIENTS is empty — nothing would be checked"
    canon_tools = canon_nums()["{canon_tools}"]
    failures, total_matched = [], 0

    for key in sorted(_CLIENTS):
        html = _render_page(key, None)

        # 1. no placeholder may survive into a served response
        leftover = re.findall(r"\{(?:TOOLS|canon_[a-z_]+)\}", html)
        if leftover:
            failures.append(
                f"  /connect/{key}: unresolved placeholder(s) {sorted(set(leftover))} "
                f"— worse than a stale number, an agent reads literal braces")

        # 2. one tool count per page, and it must BE canon. Unlike the static
        #    pages this can assert the value too: the render has just resolved
        #    it, so equality here is a wiring check, not a frozen number.
        counts = set()
        for pat in _TOOL_SITE_PATTERNS:
            for m in pat.finditer(html):
                val = next((g for g in m.groups() if g), None)
                if val is None or int(val) not in _TOOL_BAND:
                    continue
                total_matched += 1
                counts.add(val)
        if len(counts) > 1:
            failures.append(
                f"  /connect/{key}: {len(counts)} different tool counts -> {sorted(counts)}")
        elif counts and counts != {str(canon_tools)}:
            failures.append(
                f"  /connect/{key}: renders {counts.pop()!r} tools, canon is "
                f"{canon_tools!r} — the count is not bound to canon")

        # 3. and no banned stale figure may survive the render
        low = html.lower()
        for tok_id, pat, canonical_phrase, requires, _why in BANNED_STALE:
            if requires and requires.lower() not in low:
                continue
            hit = pat.search(html)
            if hit:
                failures.append(
                    f"  /connect/{key}: [{tok_id}] {hit.group(0)!r} contradicts "
                    f"canonical '{canonical_phrase}'")

    assert not failures, (
        "Rendered /connect/<client> page(s) are not canon-bound. These are the "
        "per-platform install pages — the highest-intent surface after /connect "
        f"itself ({FIXWAVE}):\n" + "\n".join(failures)
    )
    assert total_matched, (
        f"HARNESS ERROR: matched zero tool counts across {len(_CLIENTS)} rendered "
        "platform page(s) — the patterns are broken, not the pages clean."
    )


# ── AGENT-COUNT PARITY FENCE (r-agent-parity 2026-07-31) ─────────────────────
#
# Measured 2026-07-31, one day, one reader-facing quantity, three values:
#   /ai header badge          64   (reach_weekly ISO-week rollup, labeled
#                                   "this week", served by /api/v1/ai/reach)
#   /ai tool-use widget       95   (mcp_calls_identity view, rolling 7d —
#                                   main._real_tool_use_7d)
#   /api/v1/mcp/funnel       129   (COUNT(DISTINCT raw ip_address strings),
#                                   live predicate only — XFF chains count
#                                   once per chain form)
#
# The fix (shell #44 lane 3's own prescription): ONE canonical query in
# mcp_calls_deloop.canonical_external_activity_sql(), imported by every
# surface. These tests pin that wiring so a new inline copy — or a surface
# quietly reverting to sessions/raw-IP counting — fails CI, not a reader.

AGENT_COUNT_EMITTERS = (
    "flask_mcp_endpoints.py",
    "main.py",
    os.path.join("routes", "ai_reach.py"),
    os.path.join("routes", "growth_memo.py"),
)


def test_canonical_agent_query_shape():
    """The single-source query must keep the canonical (identity × exclusion)
    tuple: mcp_calls_identity view, is_public_ip AND is_real_external,
    COUNT(DISTINCT agent_id). If the canonical definition itself changes,
    change it THERE and update this pin in the same commit — never fork a
    variant in an emitter."""
    from mcp_calls_deloop import canonical_external_activity_sql
    sql = canonical_external_activity_sql(7)
    for marker in ("mcp_calls_identity",
                   "is_public_ip AND is_real_external",
                   "COUNT(DISTINCT agent_id)"):
        assert marker in sql, (
            f"canonical_external_activity_sql lost {marker!r} — the canonical "
            f"agent-count definition drifted ({FIXWAVE}): {sql}"
        )
    # Window parameter must be integer-coerced (the fragment is literal-only).
    assert "7 days" in sql
    with pytest.raises((ValueError, TypeError)):
        canonical_external_activity_sql("7; DROP TABLE x")  # type: ignore[arg-type]


def test_agent_count_emitters_import_canonical_query():
    """Every emitter that publishes a 'distinct agents' figure must reference
    the ONE canonical query (or, for multi-window variants like the growth
    memo, carry the same view + filter + identity markers inline).

    flask_mcp_endpoints must also actually EMIT real_external_agents_7d (the
    field dashboards bind), and the Upgrade Funnel dashboard must bind it —
    a helper nobody calls is not parity."""
    def _src(rel):
        return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")

    for rel in ("flask_mcp_endpoints.py", "main.py",
                os.path.join("routes", "ai_reach.py")):
        assert "canonical_external_activity_sql" in _src(rel), (
            f"{rel}: no longer references "
            f"canonical_external_activity_sql — an agent-count surface forked "
            f"off the canonical definition ({FIXWAVE})."
        )

    funnel_src = _src("flask_mcp_endpoints.py")
    assert "real_external_agents_7d" in funnel_src, (
        "/api/v1/mcp/funnel no longer emits real_external_agents_7d — "
        f"dashboards fall back to the raw-IP secondary count ({FIXWAVE})."
    )

    memo_src = _src(os.path.join("routes", "growth_memo.py"))
    for marker in ("mcp_calls_identity", "is_public_ip AND is_real_external"):
        assert marker in memo_src, (
            f"routes/growth_memo.py lost {marker!r} — its inline two-window "
            f"variant must keep the canonical view + filter ({FIXWAVE})."
        )

    dash_src = _src(os.path.join("static", "mcp-dashboard.html"))
    assert "real_external_agents_7d" in dash_src, (
        "static/mcp-dashboard.html Agents KPI no longer binds "
        f"real_external_agents_7d ({FIXWAVE})."
    )


def test_agent_count_emitters_never_count_sessions_as_agents():
    """session_id rotates per MCP connection (verified 2026-07-01: 1 of 7,933
    sessions spanned >1 day) — counting it EVER produced the inflated '65 real
    agents' artifact. Sessions may be counted AS sessions; a line that counts
    DISTINCT session_id and names it agents/callers/sources is the banned
    shape. Historical/changelog lines are exempt (the allow-list)."""
    sess_re = re.compile(r"count\s*\(\s*distinct\s+session_id", re.I)
    name_re = re.compile(r"agent|caller|source", re.I)
    hits = []
    for rel, i, line in _iter_surface_lines(AGENT_COUNT_EMITTERS):
        if sess_re.search(line) and name_re.search(line):
            hits.append(f"  {rel}:{i}: {line.strip()[:100]!r}")
    assert not hits, (
        "COUNT(DISTINCT session_id) published under an agent/caller/source "
        f"name — session_id rotates per connection ({FIXWAVE}):\n"
        + "\n".join(hits)
    )


# ── SERVED_CANON_ROUTES (2026-08-20) — guard (g): FENCE THE RENDER ───────────
#
# SERVED_CANON_PAGES above is a FILE list. It works because static/connect.html
# is bytes on disk that main.connect_page() renders through canon_text(). The
# surfaces below have no bytes on disk at all: /llms.txt, /llms-full.txt and the
# MCP server-card are built inline by @app.route handlers in ai_discovery_routes.py,
# and /api/v1/agents/capabilities.json is assembled dict-by-dict in
# routes/agent_capabilities_feed.py.
#
# ★That distinction is exactly how the _TOOL_COUNT = 59 refreeze survived: the
#  template said "{TOOLS} tools" (noun, no digits) and the constant said "= 59"
#  (digits, no noun), so every line-scan of every FILE read clean while the
#  RENDERED page published 59 five times. A surface list cannot fence a surface
#  that only exists as a response.
#
# ★And it is how the four dead frontend heal targets survived on the other side
#  of the repo boundary: llms.txt was healed for months in dchub-frontend while
#  the bytes clients actually receive come from the route below.
#
# Rendered with a bare Flask app carrying ONLY the blueprint under test — the
# house pattern (tests/test_analyst_note.py) — so this never imports main.py.
_SERVED_CANON_ROUTES = (
    "/llms.txt",
    "/llms-full.txt",
    "/.well-known/mcp/server-card.json",
    "/api/v1/agents/capabilities.json",
)


def _render_canon_routes():
    """{path: body} for every inline-rendered canon surface. Skips the module
    if Flask or a route module cannot import, rather than failing a fence on an
    environment problem."""
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    from ai_discovery_routes import register_discovery_routes
    register_discovery_routes(app)
    from routes.agent_capabilities_feed import agent_capabilities_bp
    app.register_blueprint(agent_capabilities_bp)
    client = app.test_client()
    out = {}
    for path in _SERVED_CANON_ROUTES:
        resp = client.get(path)
        assert resp.status_code == 200, (
            f"{path}: rendered {resp.status_code}, not 200 — this fence anchors "
            f"to the response body ({FIXWAVE})."
        )
        out[path] = resp.get_data(as_text=True)
    return out


def test_rendered_canon_routes_publish_one_tool_count():
    """An inline-rendered surface must not publish two different tool counts.

    Same invariant as test_served_pages_publish_one_tool_count, applied to the
    RESPONSE instead of the file — see the block comment above for why a file
    list structurally cannot reach these.

    ★_HISTORICAL_RE is applied PER LINE, and it is load-bearing here rather than
     defensive. /llms.txt deliberately publishes retired counts:

         "call tools/list for the canonical, always-current catalog — '11 tools',
          '53 tools' and '60 tools' are previously advertised, now-retired counts."

     That is a disclosure, not drift, and a fence that failed on it would be
     demanding the removal of an honest line. The allow-list keyword is on the
     same line as the digits by construction, because the sentence has to say
     what the numbers are for a reader too.
    """
    failures = []
    for path, body in _render_canon_routes().items():
        found = {}
        for line in body.splitlines():
            if _HISTORICAL_RE.search(line):
                continue
            for pat in _TOOL_SITE_PATTERNS:
                for m in pat.finditer(line):
                    raw = next((g for g in m.groups() if g), None)
                    if raw is None or int(raw) not in _TOOL_BAND:
                        continue
                    found.setdefault(raw, set()).add(" ".join(m.group(0).split())[:60])
        if len(found) > 1:
            shown = " vs ".join(
                f"{v!r} ({', '.join(sorted(ph))})" for v, ph in sorted(found.items()))
            failures.append(f"  {path}: {len(found)} different tool counts -> {shown}")
    assert not failures, (
        "A rendered canon surface publishes more than one tool count "
        f"({FIXWAVE}):\n" + "\n".join(failures)
    )


def test_capabilities_quotable_never_contradicts_its_own_counts():
    """agent_quotable must not disagree with the counts in the same document.

    ★This field is CC-BY-4.0 and written to be pasted verbatim into an agent's
     answer, so a number in it is a published claim with our licence attached.
     Until 2026-08-20 it read "DC Hub tracks 26,334 data-center facilities" —
     the raw COUNT(*) discovery pile INCLUDING flagged duplicates — while
     /api/v1/canon/phrases served 18,500+ off the deduped basis.

    ★Driven with an INJECTED canon dict, not the live DB. Without DATABASE_URL
     the feed correctly omits these fields, so a fence that just read the live
     render would pass vacuously in CI — green because there was nothing there,
     which is the exact failure this file exists to refuse.
    """
    flask = pytest.importorskip("flask")
    from routes import agent_capabilities_feed as feed

    live_like = {
        "facilities_verified": 18603, "markets": 300,
        "deals": 1892, "countries_verified": 178,
    }
    app = flask.Flask(__name__)
    app.register_blueprint(feed.agent_capabilities_bp)

    orig = feed._canon_stats
    feed._canon_stats = lambda: dict(live_like)
    try:
        feed._CAPS_CACHE.update({"data_version": None, "payload": None, "computed_at": 0.0})
        body = app.test_client().get("/api/v1/agents/capabilities.json").get_data(as_text=True)
    finally:
        feed._canon_stats = orig
        feed._CAPS_CACHE.update({"data_version": None, "payload": None, "computed_at": 0.0})

    doc = json.loads(body)
    counts, quotable = doc.get("counts", {}), doc.get("agent_quotable")

    assert counts.get("facilities") == 18603, (
        f"counts.facilities is {counts.get('facilities')!r}, not the injected "
        "verified count — the feed is not reading facilities_verified. This is "
        "the assertion that would have failed on the raw COUNT(*) basis."
    )
    assert quotable, (
        "agent_quotable absent although every count resolved — the fence below "
        "would pass vacuously."
    )
    for field, value in (("facilities", 18603), ("markets_scored", 300),
                         ("deals_tracked", 1892), ("countries", 178)):
        assert counts.get(field) == value, f"counts.{field} != injected {value}"
        assert f"{value:,}" in quotable or str(value) in quotable, (
            f"agent_quotable omits counts.{field}={value}. The sentence and the "
            f"counts block disagree — one was updated and the other was not:\n"
            f"  {quotable}"
        )
    # The raw pile must not appear anywhere in the document, under any framing.
    assert "26,334" not in quotable and "26334" not in body, (
        "the raw discovered_facilities row count is back in the served document"
    )


def test_capabilities_omits_rather_than_publishing_below_canon_floor():
    """A degraded render must drop the claim, not shrink it.

    canonical_stats._FALLBACK["facilities_verified"] is 400 — a conservative
    cold-start seed from 2026-06-30. Rendered with no DATABASE_URL, this feed
    used to put that straight into the CC-BY sentence: "DC Hub tracks 400
    data-center facilities". A 46x under-claim is not the safe direction of a
    46x over-claim; both are wrong numbers published as fact.
    """
    flask = pytest.importorskip("flask")
    from routes import agent_capabilities_feed as feed

    floor = feed._canon_facilities_floor()
    assert floor and floor > 1000, (
        f"canon facilities floor read as {floor!r} — the bound this test drives "
        "is unreadable, so the guard below cannot mean anything."
    )

    app = flask.Flask(__name__)
    app.register_blueprint(feed.agent_capabilities_bp)
    orig = feed._canon_stats
    feed._canon_stats = lambda: {"facilities_verified": 400, "markets": 300,
                                 "deals": 1400, "countries_verified": 170}
    try:
        feed._CAPS_CACHE.update({"data_version": None, "payload": None, "computed_at": 0.0})
        body = app.test_client().get("/api/v1/agents/capabilities.json").get_data(as_text=True)
    finally:
        feed._canon_stats = orig
        feed._CAPS_CACHE.update({"data_version": None, "payload": None, "computed_at": 0.0})

    doc = json.loads(body)
    assert "facilities" not in doc.get("counts", {}), (
        f"counts.facilities published {doc['counts'].get('facilities')!r}, below "
        f"the {floor:,}+ floor this site already quotes."
    )
    assert not doc.get("agent_quotable"), (
        "agent_quotable was built without a facility count — the CC-BY sentence "
        "must not ship with a hole in it."
    )
    # ...and the neighbours, whose fallbacks ARE the published floors, survive.
    assert doc["counts"].get("markets_scored") == 300, (
        "markets_scored was dropped too — the floor check is over-reaching."
    )


# ── COVERAGE INVERSION (2026-08-20) ───────────────────────────────────────────
#
# AGENT_CODE_SURFACES is an ALLOW-list: a file is fenced only if someone
# remembered to add it. Every entry above carries a ★ note saying, in effect,
# "this shipped a stale count for weeks because it was not in this tuple" —
# ai_discovery_routes.py, routes/agent_concierge.py, routes/competitive_seo.py,
# routes/competitive_intel.py were each added AFTER the miss they caused. That
# is not four unlucky omissions; it is the shape of the list. The dominant
# failure mode here is COVERAGE, not pattern quality.
#
# Measured 2026-08-20 over the whole backend, using these same production
# patterns (tool-count + BANNED_STALE) and nothing new:
#
#   scan unit                          files flagged   not currently fenced
#   raw line scan                          146                 146
#   lines minus comments                   127                 127
#   emitted string literals only (AST)      96                  94
#
# 68 of those files carry a Blueprint AND are wired into main — i.e. they are
# SERVED, not dead patch scripts. The allow-list holds ten.
#
# So this section inverts the default: scan every .py, and require an
# EXCLUSION to be justified rather than an inclusion to be remembered.
#
# Two design choices make that affordable:
#
#  1. The scan unit is the EMITTED STRING LITERAL, not the line. A comment
#     cannot reach an agent, and the false positives that made a whole-file
#     line-scan of main.py unusable (~19: "Phase 232" headers, changelog notes,
#     a 232-floor in get_stats logic) are overwhelmingly comments, docstrings,
#     and bare numeric logic. Dropping to literals removed a third of the
#     flagged files without dropping a single real finding — every one of the
#     handoff-named literals (vertex_integration's tool descriptions,
#     og_cards' image text, content_publisher's f-string) survives the filter.
#
#  2. Existing debt is ENUMERATED, not silently tolerated. KNOWN_STALE_COUNT_DEBT
#     is a ratchet: a (file, token-class) pair already in it is allowed to stay,
#     anything NEW fails. A stale count in a file written tomorrow is caught the
#     day it lands, which is the property the allow-list never had.
#
# ★ The ledger is keyed (path -> {token_id}), deliberately NOT (path, line).
#   Bots commit to this repo all day and line numbers churn; a line-keyed ledger
#   would go red on a pure reformat and get deleted for being noisy. Token-class
#   keying survives movement and still fails on a NEW KIND of stale claim in an
#   already-indebted file.
#
# ★ Fail-CLOSED on parse: a file that will not parse is reported, not skipped.
#   "Scanner could not read it" and "scanner found nothing" must never be the
#   same outcome — that equivalence is what let the deleted-twin and heal-target
#   classes hide.

# Directories that hold no agent-facing served source.
#   tests/          — fixtures deliberately contain retired values (this file
#                     itself carries every banned token as a pattern).
#   migrations/     — historical DDL, never rendered.
#   scripts/        — one-shot operational tooling, not imported by the app.
#   static/, templates/, site/ — non-.py by construction; listed so a stray
#                     .py dropped there does not silently join the scan.
STALE_SCAN_SKIP_DIRS = frozenset({
    ".git", ".github", "__pycache__", "venv", ".venv", "node_modules",
    "tests", "migrations", "scripts", "site", "static", "templates",
    # A vendored 2,426-file frontend snapshot. Not deployed from here; the
    # live copy is fenced in the dchub-frontend repo.
    "frontend_snapshot", "dchub-frontend",
})

# Files excluded BY NAME, each with the reason it is covered elsewhere. An
# exclusion without a live alternative guard is a hole, so each value names the
# test that actually covers the file.
STALE_SCAN_SKIP_FILES = {
    # ★ main.py is NOT excluded, and that is a change of position worth stating.
    #
    # Every prior note says main.py "cannot be line-scanned — 42k lines yields
    # ~19 false positives" (Flask "Phase 232" headers, "6 tools in a day" prose,
    # a Glama changelog note, the get_stats 232-floor logic). That measurement
    # was correct, and it was a measurement of a LINE scan. Re-measured
    # 2026-08-20 against the literal scan this section uses, main.py (now 45,576
    # lines) yields exactly ONE hit — and it is a true positive:
    #
    #   main.py:19240   "facilities": 21000
    #
    # Every one of the ~19 was a comment, a docstring, or a bare number in
    # logic. None is an emitted string literal or a count-keyed int. The
    # exclusion was load-bearing for the old scan unit and is simply obsolete
    # for this one, so main.py joins the walk rather than living on surgical
    # anchors alone. The anchors stay — they assert on RENDERED OUTPUT, which
    # is strictly stronger than any scan, and this does not replace them.
    #
    # These two modules ARE the denylist. ai_surface_canon.PINNED['stale_markers']
    # and the sentinel's marker set enumerate retired values as DATA — that is
    # their job. Scanning them for retired values is self-referential: it would
    # demand the ban list stop naming what it bans.
    "ai_surface_canon.py": "SoT for stale_markers — enumerating a value is not claiming it",
    "ai_surface_sentinel.py": "consumes the same marker set as data",
}

# ── (e) BARE-INT EVASION ──────────────────────────────────────────────────────
#
# BANNED_STALE matches the PROSE form: r"(?<![\d,])(?:19|20|21|22|23),\d{3}\+"
# requires the comma AND the plus. A JSON body carrying {"facilities": 21000}
# states the same retired floor to the same agent and matches none of it.
#
# Measured 2026-08-20 — thirteen live bare-int literals, none of them visible to
# the comma-form fence, including one inside a file the fence ALREADY SCANS:
#
#   ai_discovery_routes.py:588   "facilities_tracked": 21000
#
# ai_discovery_routes.py is AGENT_CODE_SURFACES[0]. It serves /llms.txt and
# /llms-full.txt inline. It was added to the allow-list on 2026-07-25 precisely
# because it "served 21,000+ in ten places" — and it has been serving 21000 as
# an int ever since, with this fence green over the exact file.
#
# Keyed on the DICT KEY, not on a bare number: an int is only a facility claim
# when it is the value of a facility-named key. 21000 in a timeout, a port, or
# a row limit is not a claim and must not fire. Same discipline as the main.py
# AST anchor, which guards tools_count/tool_count and deliberately refuses a
# bare `version` key.
FACILITY_COUNT_KEYS = frozenset({
    "facilities", "facility_count", "facilities_count", "total_facilities",
    "facilities_tracked", "facility_total",
})
TOOL_COUNT_KEYS = frozenset({
    "tools_count", "tool_count", "mcp_tools", "tools_advertised",
    "num_tools", "tool_total",
})
# The pre-dedup facility floor, as ints. Mirrors the comma-form range in
# BANNED_STALE['facilities_stale_floor'] exactly — same retired band, other
# notation. NOT extended below 19,000: canon's own floor lives there and a
# tighter bound would fence the correct answer.
STALE_FACILITY_INT_RANGE = (19_000, 23_999)


def _docstring_line_span(tree):
    """Line numbers occupied by module/class/function docstrings.

    Docstrings are documentation, not emitted claims — and this repo documents
    retired values heavily (every ★ note above names one). Scanning them would
    make the fence fire on its own changelog.
    """
    spans = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc = body[0].value
                spans.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return spans


def _scan_python_source(text):
    """Return {token_id: [(lineno, matched_text, source_line)]} for one module.

    Raises SyntaxError to the caller — a file that cannot be parsed must be
    reported, never counted as clean.
    """
    tree = ast.parse(text)
    raw = text.splitlines()
    doc_lines = _docstring_line_span(tree)
    found = {}

    def src(lineno):
        return raw[lineno - 1] if 0 < lineno <= len(raw) else ""

    def record(tok_id, lineno, matched):
        found.setdefault(tok_id, []).append(
            (lineno, matched, src(lineno).strip()[:100]))

    for node in ast.walk(tree):
        # Emitted string literals. ast.walk descends into JoinedStr, so the
        # constant parts of an f-string are covered — that is how
        # content_publisher.py's f"…4,000+ tracked…" is visible at all.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in doc_lines:
                continue
            line = src(node.lineno)
            if _HISTORICAL_RE.search(line):
                continue
            value, low = node.value, node.value.lower()
            for n in _stated_tool_counts(value):
                if n != CANONICAL["tools"]:
                    record("tool_count_literal", node.lineno, f"{n} tools")
            for tok_id, pat, _phrase, requires, _why in BANNED_STALE:
                if requires and requires.lower() not in low:
                    continue
                hit = pat.search(value)
                if hit:
                    record(tok_id, node.lineno, hit.group(0))

        # (e) bare ints, keyed on the dict key that gives them meaning.
        elif isinstance(node, ast.Dict):
            for key_node, val_node in zip(node.keys, node.values):
                if not (isinstance(key_node, ast.Constant)
                        and isinstance(key_node.value, str)):
                    continue
                if not (isinstance(val_node, ast.Constant)
                        and isinstance(val_node.value, int)
                        # bool is an int subclass; {"facilities": True} is not a count
                        and not isinstance(val_node.value, bool)):
                    continue
                key, ival = key_node.value.strip().lower(), val_node.value
                if _HISTORICAL_RE.search(src(val_node.lineno)):
                    continue
                lo, hi = STALE_FACILITY_INT_RANGE
                if key in FACILITY_COUNT_KEYS and lo <= ival <= hi:
                    record("facilities_bare_int", val_node.lineno, str(ival))
                elif (key in TOOL_COUNT_KEYS
                      and 0 < ival < 1000
                      and ival != CANONICAL["tools"]):
                    record("tool_count_bare_int", val_node.lineno, str(ival))
    return found


def _iter_scannable_python():
    """Yield repo-relative paths of every .py the inverted fence covers."""
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs
                   if d not in STALE_SCAN_SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            if fname in STALE_SCAN_SKIP_FILES:
                continue
            yield os.path.relpath(os.path.join(root, fname), REPO_ROOT)


@functools.lru_cache(maxsize=1)
def _scan_repo_stale_counts():
    """Return ({relpath: {token_id: [hits]}}, [unparseable]) for the whole repo.

    Cached: three tests need this walk and it parses ~1,291 modules, which is
    the difference between a 2s and a 17s run of this file. Callers only read
    the result. Safe because the tree cannot change mid-session — and if that
    ever stops being true, the cache is the least of the problems.
    """
    found, unparseable = {}, []
    for rel in _iter_scannable_python():
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            unparseable.append(f"{rel}: unreadable ({exc})")
            continue
        try:
            hits = _scan_python_source(text)
        except SyntaxError as exc:
            unparseable.append(f"{rel}: unparseable ({exc})")
            continue
        if hits:
            found[rel] = hits
    return found, unparseable


# ── KNOWN_STALE_COUNT_DEBT — the ratchet baseline, measured 2026-08-20 ────────
#
# 100 files, 132 (file, token) pairs. This is a DEBT REGISTER, not an
# allow-list: every entry is a real stale claim in served or shippable code,
# and the only legal direction of travel is smaller.
#
# It is deliberately not being fixed in the same change that adds the fence.
# Roughly two thirds of these are the pre-dedup facility floor (19,000+ …
# 21,800+) or the retired deal floor, and the correct replacement value is the
# open question in the canon basis review — canonical_stats calls the raw count
# authoritative, ai_surface_canon records a 07-24 DEDUP REBASE away from it, and
# repair_thin_twin_canonical counts DISTINCT canonical_slug WHERE NOT
# is_duplicate. Rewriting 100 files against an unsettled basis would produce a
# fourth contradictory answer. Land the fence, settle the basis, then drain the
# ledger against it.
#
# To regenerate after a fix wave, run this module's scanner and diff — the
# failure message prints the corrected dict ready to paste.
KNOWN_STALE_COUNT_DEBT = {
    # DB-DOWN fallback branch: when the stats query raises, this endpoint
    # publishes the pre-dedup floor as fact. Same class as the
    # canonical_stats fallback that sat frozen at 12,650+ for four days.
    'main.py': {'facilities_bare_int'},
    'ai_discovery_routes.py': {'tool_count_literal'},
    'ai_outreach_agent.py': {'tool_count_literal'},
    'canonical_stats.py': {'facilities_bare_int'},
    'content_publisher.py': {'deals_stale_floor'},
    'dc_expert_brain.py': {'deals_stale_floor'},
    'dchub-mcp-v2.1/apply_worker_patch.py': {'deals_stale_floor', 'facilities_stale_floor'},
    'dchub_daily_automation.py': {'facilities_bare_int'},
    'dchub_mcp_server.py': {'facilities_stale_floor', 'tool_count_literal'},
    'dchub_paywall.py': {'deals_stale_floor'},
    'dchub_self_heal.py': {'markets_232'},
    'fix_neon_tables.py': {'tool_count_literal'},
    'google_meta_integration.py': {'facilities_bare_int'},
    'inject_meta_tags.py': {'deals_stale_floor'},
    'integrations/huggingface-space/app.py': {'facilities_stale_floor', 'tool_count_literal'},
    'intelligence_index.py': {'facilities_bare_int'},
    'linkedin_poster.py': {'deals_stale_floor'},
    'marketing_stats_route.py': {'deals_stale_floor'},
    'mcp_bug_fixes_and_new_tools.py': {'deals_stale_floor', 'facilities_stale_floor'},
    'mcp_gateway.py': {'facilities_stale_floor'},
    'mcp_qa_fixes_v7.py': {'tool_count_literal'},
    'mcp_server_patch.py': {'tool_count_literal'},
    'mcp_teaser_fixes.py': {'tool_count_literal'},
    'moltbook_integration.py': {'deals_stale_floor'},
    'qa_mcp_test.py': {'tool_count_literal'},
    'replit-nav-config-endpoint.py': {'facilities_bare_int'},
    'routes/agent_capabilities_feed.py': {'facilities_bare_int'},
    'routes/agent_self_register.py': {'tool_count_literal'},
    'routes/agent_success_report.py': {'tool_count_literal'},
    'routes/ai_capacity_index.py': {'markets_232'},
    'routes/ai_citation_tracker.py': {'isos_non_canonical', 'tool_count_literal'},
    'routes/ai_lab_outreach.py': {'deals_stale_floor', 'facilities_stale_floor'},
    'routes/architecture_landing.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/audit_closure_master_shell.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/brain_autopilot.py': {'tool_count_literal'},
    'routes/brain_capability_ledger.py': {'tool_count_literal'},
    'routes/brain_consistency_radar.py': {'tool_count_literal'},
    'routes/brain_error_classes.py': {'tool_count_literal'},
    'routes/brain_feature_proposer.py': {'tool_count_literal'},
    'routes/brain_investigator.py': {'markets_232'},
    'routes/bs_translator.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/campaign_halfprice_annual.py': {'tool_count_literal'},
    'routes/case_studies_landing.py': {'deals_stale_floor'},
    'routes/competitive_intel.py': {'isos_non_canonical'},
    'routes/competitive_vs.py': {'tool_count_literal'},
    'routes/content_enqueue.py': {'deals_stale_floor', 'facilities_bare_int', 'tool_count_literal'},
    'routes/demo.py': {'isos_non_canonical', 'tool_count_literal'},
    'routes/email_capture.py': {'tool_count_literal'},
    'routes/energy_report.py': {'tool_count_literal'},
    'routes/funnel_health.py': {'tool_count_literal'},
    'routes/funnel_leads.py': {'deals_stale_floor'},
    'routes/handoff_truth_master_shell.py': {'facilities_retired_12650'},
    'routes/industry_pulse.py': {'tool_count_literal'},
    'routes/integrations_landing.py': {'isos_non_canonical', 'tool_count_literal'},
    'routes/linkedin_content_engine.py': {'deals_stale_floor'},
    'routes/linkedin_partnership_weekly.py': {'deals_stale_floor', 'isos_non_canonical'},
    'routes/linkedin_quad_daily.py': {'deals_stale_floor', 'isos_non_canonical'},
    'routes/lost_conversion_outreach.py': {'tool_count_literal'},
    'routes/market_brief.py': {'deals_stale_floor'},
    'routes/market_deep_dive.py': {'facilities_stale_floor'},
    'routes/marketing_engine.py': {'deals_stale_floor', 'facilities_stale_floor'},
    'routes/mcp_funnel_upgrade.py': {'isos_non_canonical'},
    'routes/mcp_outreach_drafts.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/mcp_presence_crawler.py': {'tool_count_literal'},
    'routes/mcp_quality_badge.py': {'tool_count_literal'},
    'routes/mcp_usage_self.py': {'tool_count_literal'},
    'routes/media_claim_verify.py': {'deals_stale_floor', 'markets_232'},
    'routes/media_fact_check_guard.py': {'deals_stale_floor'},
    'routes/media_outreach.py': {'deals_stale_floor', 'isos_non_canonical'},
    'routes/monthly_trend.py': {'markets_232', 'tool_count_literal'},
    'routes/multiplatform_amplifier.py': {'tool_count_literal'},
    'routes/og_cards.py': {'facilities_stale_floor'},
    'routes/og_landings.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/onboard_universal.py': {'tool_count_literal'},
    'routes/onboarding_recover.py': {'deals_stale_floor'},
    'routes/open_data_csv.py': {'isos_non_canonical'},
    'routes/openapi_dynamic.py': {'facilities_bare_int', 'markets_232', 'tool_count_literal'},
    'routes/operator_brief.py': {'deals_stale_floor'},
    'routes/operators.py': {'facilities_stale_floor'},
    'routes/outreach_cron.py': {'tool_count_literal'},
    'routes/partner_landing.py': {'deals_stale_floor', 'facilities_stale_floor', 'isos_non_canonical', 'tool_count_literal'},
    'routes/partnerships_page.py': {'tool_count_literal'},
    'routes/paywall_hint_middleware.py': {'deals_stale_floor'},
    'routes/press_outreach.py': {'deals_stale_floor'},
    'routes/quarterly_report.py': {'deals_stale_floor', 'facilities_bare_int'},
    'routes/quick_redirects.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/registry_distribution_master_shell.py': {'deals_stale_floor', 'tool_count_literal'},
    'routes/registry_surface_shell.py': {'markets_232', 'tool_count_literal'},
    'routes/sample_landing.py': {'tool_count_literal'},
    'routes/seo_pages.py': {'deals_stale_floor'},
    'routes/site_valuation_engine.py': {'deals_stale_floor'},
    'routes/state_of_power.py': {'tool_count_literal'},
    'routes/testimonial_probe.py': {'facilities_stale_floor'},
    'routes/upgrade_outreach.py': {'markets_232'},
    'routes/vertex_integration.py': {'facilities_stale_floor', 'markets_232'},
    'routes/white_glove_loop_master_shell.py': {'tool_count_literal'},
    'seo_meta_tags.py': {'deals_stale_floor', 'facilities_stale_floor'},
    'tax_incentives_routes.py': {'facilities_stale_floor'},
    'tools/email_blast_developer_launch.py': {'facilities_stale_floor'},
    'utils/paywall_response.py': {'tool_count_literal'},
}


def _format_debt_ledger(found):
    """Render a {rel: {tok}} mapping as a paste-ready literal.

    The failure message carries the corrected ledger so draining debt is a
    copy-paste, not a hand-edit of 100 lines. A guard that is annoying to
    satisfy gets deleted; this one hands you the fix.
    """
    lines = ["KNOWN_STALE_COUNT_DEBT = {"]
    for rel in sorted(found):
        toks = ", ".join(repr(t) for t in sorted(found[rel]))
        lines.append(f"    {rel!r}: {{{toks}}},")
    lines.append("}")
    return "\n".join(lines)


def test_stale_count_scanner_actually_detects(tmp_path):
    """Guard the guard: every detector must fire on a planted value.

    A scanner that silently matched nothing would make both tests below pass
    vacuously — and "found nothing" is exactly what a broken regex looks like
    from the outside. Each case is a MUST-FIND; the last two are MUST-NOT-FIND
    controls, because a detector that fires on everything is equally useless.
    """
    must_find = [
        ("bare int under a facility key",
         'CFG = {"facilities": 21000}', "facilities_bare_int"),
        ("bare int under an alias key",
         'CFG = {"total_facilities": 20000}', "facilities_bare_int"),
        ("bare int tool count",
         'CFG = {"tools_count": 46}', "tool_count_bare_int"),
        ("prose facility floor in a literal",
         'BLURB = "over 21,000+ facilities tracked"', "facilities_stale_floor"),
        ("prose floor inside an f-string part",
         'BLURB = f"{name}: 4,000+ tracked deals"', "deals_stale_floor"),
        ("stale tool count in prose",
         'BLURB = "DC Hub exposes 73 MCP tools"', "tool_count_literal"),
    ]
    for name, src, expected in must_find:
        hits = _scan_python_source(src)
        assert expected in hits, (
            f"scanner MISSED {name}: {src!r} should have raised {expected!r}, "
            f"got {sorted(hits) or 'nothing'} — the fence would pass vacuously."
        )

    must_not_find = [
        ("docstring naming a retired value",
         '"""Historic note: we once said 21,000+ facilities."""\nX = 1'),
        ("comment naming a retired value",
         'X = 1  # was 21,000+ facilities before the rebase'),
        ("int of the same magnitude under an unrelated key",
         'CFG = {"timeout_ms": 21000, "max_rows": 20000}'),
        ("canonical tool count",
         f'BLURB = "DC Hub exposes {CANONICAL["tools"]} MCP tools"'),
        ("boolean under a facility key",
         'CFG = {"facilities": True}'),
    ]
    for name, src in must_not_find:
        hits = _scan_python_source(src)
        assert not hits, (
            f"scanner FALSE-POSITIVED on {name}: {src!r} -> {sorted(hits)}. "
            "Over-firing gets a fence deleted as noisy."
        )

    # Fail-closed: a file that will not parse must raise, never read as clean.
    with pytest.raises(SyntaxError):
        _scan_python_source("def broken(:\n    pass\n")


def test_no_new_stale_count_debt():
    """No .py may introduce a stale count token that is not already ledgered.

    This is the inverted fence. AGENT_CODE_SURFACES asks "did someone remember
    to list this file?"; this asks "is this claim new?" — so a module written
    tomorrow is covered on its first commit.

    Failing here means a NEW stale count landed. Fix the number; do not add the
    file to the ledger. The ledger exists to hold PRE-EXISTING debt measured on
    2026-08-20, and it is meant to shrink.
    """
    found, unparseable = _scan_repo_stale_counts()

    assert not unparseable, (
        "stale-count scanner could not read these files — a scanner that "
        "cannot parse must not be mistaken for a clean scan "
        f"({FIXWAVE}):\n  " + "\n  ".join(unparseable)
    )

    new = []
    for rel in sorted(found):
        allowed = KNOWN_STALE_COUNT_DEBT.get(rel, frozenset())
        for tok_id in sorted(found[rel]):
            if tok_id in allowed:
                continue
            for lineno, matched, line in found[rel][tok_id][:3]:
                new.append(
                    f"  [{tok_id}] {rel}:{lineno}: {matched!r} -> {line!r}")

    assert not new, (
        "NEW stale count(s) in agent-facing Python — these contradict canon "
        f"and no ledger entry covers them ({FIXWAVE}):\n"
        + "\n".join(new)
        + "\n\nFix the number to render from canon (ai_surface_canon.PINNED / "
          "canonical_stats), rather than ledgering it. If the file is genuinely "
          "not agent-facing, exclude it BY NAME in STALE_SCAN_SKIP_FILES with "
          "the guard that covers it instead."
    )


def test_stale_count_debt_ledger_has_not_rotted():
    """Every ledger entry must still correspond to a real finding.

    Separate from the ratchet on purpose. A fix wave that drains twenty entries
    should produce ONE clear "update the ledger" failure with the corrected
    dict attached — not twenty red builds on unrelated work, and not a register
    that quietly accumulates entries for code that no longer exists. The
    deleted-.html-twin audit works the same way, for the same reason: an
    allow-list nobody reconciles becomes fiction.
    """
    found, unparseable = _scan_repo_stale_counts()
    assert not unparseable, (
        "stale-count scanner could not read these files "
        f"({FIXWAVE}):\n  " + "\n  ".join(unparseable))

    stale = []
    for rel in sorted(KNOWN_STALE_COUNT_DEBT):
        if rel not in found:
            stale.append(f"  {rel}: ledgered, but now clean — drop the entry")
            continue
        for tok_id in sorted(KNOWN_STALE_COUNT_DEBT[rel]):
            if tok_id not in found[rel]:
                stale.append(
                    f"  {rel}: [{tok_id}] ledgered, but no longer present — "
                    "drop the token")

    assert not stale, (
        "KNOWN_STALE_COUNT_DEBT names debt that no longer exists. This is good "
        "news — the debt was paid — but the ledger must follow reality or it "
        f"stops meaning anything ({FIXWAVE}):\n"
        + "\n".join(stale)
        + "\n\nReplace the ledger with:\n\n"
        + _format_debt_ledger(found)
    )


def test_inverted_fence_covers_more_than_the_allow_list():
    """The inversion must actually widen coverage, not restate it.

    Pins the property the change exists for: the scanned universe is far larger
    than AGENT_CODE_SURFACES, and the debt register proves the extra coverage
    found real claims outside it. If someone narrows the walk (a broad new
    SKIP_DIR, say) until this fence only re-covers the allow-list, that is the
    coverage regression this section was written to prevent, and it fails here
    rather than silently.
    """
    scanned = set(_iter_scannable_python())
    assert len(scanned) > 500, (
        f"stale-count scan covers only {len(scanned)} file(s) — it walked "
        "1,292 when written. A SKIP_DIR has almost certainly swallowed the "
        f"repo ({FIXWAVE})."
    )

    outside = set(KNOWN_STALE_COUNT_DEBT) - set(AGENT_CODE_SURFACES)
    assert len(outside) >= 96, (
        f"only {len(outside)} indebted file(s) sit outside AGENT_CODE_SURFACES "
        "— 98 did when measured. If debt was genuinely drained, lower this "
        f"floor in the same commit that drains it ({FIXWAVE})."
    )

    # The allow-list must remain a strict subset of the scanned universe, minus
    # the by-name exclusions — otherwise a fenced file silently left the walk.
    for rel in AGENT_CODE_SURFACES:
        if os.path.basename(rel) in STALE_SCAN_SKIP_FILES:
            continue
        assert rel in scanned, (
            f"{rel} is in AGENT_CODE_SURFACES but fell OUT of the inverted "
            f"scan — coverage went backwards ({FIXWAVE})."
        )
