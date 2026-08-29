"""r-white-glove BUILD 1 (2026-07-18) — drift-detector unit tests.

Pure-function tests for routes.white_glove_propagation.detect_number_drift
plus static wiring guards (SCHEDULE entry, kill switch, canonical
brain-findings writer).

ZERO-FALSE-POSITIVE TOLERANCE: the detector's contract is that canonical
copy NEVER flags. Several tests below render the real canonical
description builders and assert an empty drift list — if any of them
fail, the propagation job would file drift issues against our own
correct copy, which is worse than doing nothing.

Run:  python3 -m pytest tests/test_white_glove_drift_detector.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai_surface_canon import PINNED as _PINNED  # the SHIPPED canon

from routes.white_glove_propagation import (
    AUTO_PATH_REGISTRIES,
    WITHDRAWN_NEAR_CHARS,
    detect_number_drift,
    load_canon,
    _parse_floor,
    _WITHDRAWN_RE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# A frozen canon fixture — tests must not depend on network or on the
# PINNED values moving (74 → 75 next tool launch).
CANON = {
    "tools": 74,
    "tools_live": 74,
    "deals_floor": 1400,
    "facilities_floor": 21000,
    "markets_floor": 300,
    "version": "2.4.4",
    "endpoint": "https://dchub.cloud/mcp",
    "stale_markers": [
        "10,706", "50,000+", "100 calls/day", "3,000+ M&A",
        "2,000+ tracked deals", "4,000+ M&A", "4,000+ tracked deals",
        "24 tools", "48 tools", "58 tools", "72 tools",
        "2.1.22", "2.3.3", "2.4.3",
    ],
    "stale_markers_regex": [
        {"re": r"\bDCGI\b(?![^.]*[Ww]ithdrawn)",
         "label": "DCGI advertised as a live score (withdrawn 2026-08-08)"},
    ],
}


def wrap(copy: str) -> str:
    """Embed copy in listing-page chrome with a DC Hub mention so the
    dchub-window scoping keeps it in scope."""
    return f"<html><body><h1>DC Hub MCP Server</h1><p>{copy}</p></body></html>"


# ── ZERO FALSE POSITIVES: canonical copy never flags ─────────────────

def test_canon_tool_count_present_no_drift():
    assert detect_number_drift(wrap("74 tools for data-center infra"), CANON) == []


def test_canon_full_sentence_no_drift():
    copy = ("DC Hub is the data layer for data-center infrastructure: "
            "74 live MCP tools covering 21,000 discovered facilities, "
            "300 DCPI markets, 1,400+ tracked deals, ISO-grid headroom. "
            "Free tier exposes ~10 tools; paid tiers unlock the full 74.")
    assert detect_number_drift(wrap(copy), CANON) == []


def test_canon_public_block_no_drift():
    copy = ("21,000+ facilities · 300+ markets · 1,400+ tracked deals · "
            "170+ countries · 74 tools · https://dchub.cloud/mcp")
    assert detect_number_drift(wrap(copy), CANON) == []


def test_live_overlay_count_no_drift():
    canon = dict(CANON, tools=74, tools_live=75)
    assert detect_number_drift(wrap("75 tools, live and cited"), canon) == []


def test_free_tier_ten_tools_no_drift():
    assert detect_number_drift(
        wrap("Free tier exposes ~10 tools at 10 calls per day"), CANON) == []


def test_tool_calls_stat_no_drift():
    # Smithery-style lifetime metric — "tool calls" is not a tool count.
    assert detect_number_drift(
        wrap("979 lifetime tool calls · 99.6% uptime"), CANON) == []


def test_tools_json_url_no_drift():
    assert detect_number_drift(
        wrap("fetch /api/v1/mcp/tools.json for the list of 74 tools"),
        CANON) == []


def test_live_facilities_above_floor_no_drift():
    assert detect_number_drift(
        wrap("21,433 discovered facilities worldwide"), CANON) == []


def test_live_deals_above_floor_no_drift():
    assert detect_number_drift(wrap("1,420 tracked deals"), CANON) == []


def test_editorial_small_numbers_no_drift():
    assert detect_number_drift(
        wrap("3 deals this week across 12 facilities in Ashburn"), CANON) == []


def test_version_fragment_not_matched_as_count():
    # "2.4.4" must never contribute a bare "4" to any number match.
    assert detect_number_drift(wrap("version 2.4.4 · 74 tools"), CANON) == []


def test_markets_and_tools_phrase_no_drift():
    # Regression guard: a generic modifier-word regex matched
    # "300 markets and tools" as a 300-tool claim.
    assert detect_number_drift(
        wrap("300+ markets and tools for the grid"), CANON) == []


def test_other_servers_counts_out_of_scope():
    # Directory homepage: other servers' tool counts are NOT our drift.
    page = ("<div>WeatherMCP — 58 tools</div><div>FinanceMCP — 30 tools</div>"
            + "x" * 3000 +
            "<div>DC Hub — 74 tools</div>")
    assert detect_number_drift(page, CANON) == []


def test_page_without_dchub_mention_no_drift():
    assert detect_number_drift(
        "<html>Some other server: 58 tools, 3,000+ deals</html>", CANON) == []


def test_empty_and_none_pages():
    assert detect_number_drift("", CANON) == []
    assert detect_number_drift(None, CANON) == []


def test_canonical_description_builder_is_drift_free(monkeypatch):
    """END-TO-END zero-false-positive: the paste-ready description the
    propagation job publishes (mcp_presence_crawler's builder, now fed
    by routes/mcp_honest_numbers) must not flag against the REAL canon.
    Live-resolver stubbed out — no network in unit tests."""
    from routes.mcp_presence_crawler import _build_canonical_description
    monkeypatch.setattr(
        "routes.mcp_presence_crawler._our_actual_tool_count",
        lambda: None, raising=True)
    canon = load_canon()          # pure import; live overlay stubbed
    canon["tools_live"] = canon.get("tools_live") or canon.get("tools")
    for registry in ("mcphive", "cursor_directory", "smithery",
                     "lobehub", "glama", "_default"):
        desc = _build_canonical_description(registry)
        drifts = detect_number_drift(wrap(desc), canon)
        assert drifts == [], (
            f"{registry}: canonical description flags its own copy: "
            f"{drifts} — description was: {desc!r}")


def test_honest_numbers_bridge_overrides_stale_fallback():
    """The crawler's frozen fallback said tools=33; the bridge module
    must override it with the pinned canon."""
    from routes.mcp_presence_crawler import _canonical_numbers
    from ai_surface_canon import PINNED
    n = _canonical_numbers()
    assert n["tools"] == PINNED["tools_advertised"], (
        "mcp_honest_numbers bridge not applied — auto-submitted "
        f"descriptions would advertise {n['tools']} tools")


# ── TRUE POSITIVES: the numbers from the mission brief all flag ──────

@pytest.mark.parametrize("copy,kind,found", [
    ("58 tools for data centers", "tools", 58),
    ("30 tools", "tools", 30),
    ("71 tools available", "tools", 71),
    ("24 tools", "tools", 24),
    ("48 tools", "tools", 48),
    ("53 live tools", "tools", 53),
    ('"tools": 33', "tools", 33),
    ("tools: 49", "tools", 49),
])
def test_stale_tool_counts_flag(copy, kind, found):
    drifts = detect_number_drift(wrap(copy), CANON)
    assert any(d["kind"] == kind and d["found"] == found for d in drifts), \
        f"expected {kind}={found} drift in {copy!r}, got {drifts}"


@pytest.mark.parametrize("copy,found", [
    ("3,000+ deals tracked", 3000),
    ("4,000+ tracked M&A deals", 4000),
    ("2,000+ tracked deals", 2000),
    ("11,500 transactions", 11500),
])
def test_stale_deal_counts_flag(copy, found):
    drifts = detect_number_drift(wrap(copy), CANON)
    assert any(d["kind"] in ("deals", "stale_marker") for d in drifts), \
        f"expected deals drift in {copy!r}, got {drifts}"


@pytest.mark.parametrize("copy", [
    "10,706 facilities",
    "50,000+ facilities worldwide",
    "5,000 data-center facilities",
])
def test_stale_facility_counts_flag(copy):
    drifts = detect_number_drift(wrap(copy), CANON)
    assert any(d["kind"] in ("facilities", "stale_marker") for d in drifts), \
        f"expected facilities drift in {copy!r}, got {drifts}"


def test_lettered_stale_marker_flags():
    drifts = detect_number_drift(wrap("free tier: 100 calls/day"), CANON)
    assert any(d["kind"] == "stale_marker" and d["found"] == "100 calls/day"
               for d in drifts)


def test_bare_number_stale_markers_skipped_on_third_party_html():
    # "2.1.22" etc. collide with unrelated JS-lib versions on arbitrary
    # HTML — the LISTING detector must skip letterless markers.
    assert detect_number_drift(
        wrap("react@2.1.22 loaded · 74 tools"), CANON) == []


def test_drift_findings_carry_expected_and_context():
    drifts = detect_number_drift(wrap("58 tools"), CANON)
    assert drifts and drifts[0]["expected"] == "74"
    assert "58" in drifts[0]["context"]


def test_dedupe_same_number_once():
    drifts = detect_number_drift(
        wrap("58 tools here and 58 tools there"), CANON)
    assert len([d for d in drifts if d["kind"] == "tools"]) == 1


# ── helpers ──────────────────────────────────────────────────────────

def test_parse_floor():
    assert _parse_floor("1,400+") == 1400
    assert _parse_floor("21,000+") == 21000
    assert _parse_floor("300+") == 300
    assert _parse_floor(None) is None
    assert _parse_floor("") is None


def test_load_canon_offline_shape(monkeypatch):
    """load_canon works without network: pinned floors present."""
    import routes.white_glove_propagation as wgp
    monkeypatch.setattr(
        "routes.mcp_presence_crawler._our_actual_tool_count",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        raising=True)
    canon = wgp.load_canon()
    assert isinstance(canon["tools"], int) and canon["tools"] > 0
    assert canon["deals_floor"] and canon["facilities_floor"]
    assert canon["endpoint"].startswith("https://")


# ── static wiring guards (test_canon_constraint_guard style) ─────────

def test_schedule_wired_and_kill_switch_present():
    src = (REPO_ROOT / "crawler_scheduler.py").read_text(
        encoding="utf-8", errors="ignore")
    assert re.search(
        r'\(20,\s*20,\s*"white_glove_propagate",\s*"_run_white_glove_propagate"\)',
        src), "SCHEDULE entry for white_glove_propagate missing"
    assert '"white_glove_propagate": _run_white_glove_propagate' in src, \
        "_RUNNERS map entry missing — cron name would silently no-op"
    assert "WHITE_GLOVE_PROPAGATE_DISABLE" in src, \
        "kill switch not checked in the scheduler runner"


def test_uses_canonical_brain_findings_writer():
    src = (REPO_ROOT / "routes" / "white_glove_propagation.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "from routes.brain_findings_writer import upsert_brain_finding" in src
    assert "INSERT INTO brain_findings" not in src, \
        "hand-rolled brain_findings INSERT — must go through the canonical writer"


def test_auto_vs_human_partition_is_complete():
    """Every seeded registry classifies deterministically (auto path or
    human-gated); AUTO set only names real seeded registries."""
    from routes.mcp_presence_crawler import SEED_REGISTRIES
    seeded = {r["registry_name"] for r in SEED_REGISTRIES}
    unknown_auto = AUTO_PATH_REGISTRIES - seeded
    assert not unknown_auto, f"AUTO_PATH names unseeded registries: {unknown_auto}"
    human = seeded - AUTO_PATH_REGISTRIES
    assert "mcp_so" in human and "smithery" in AUTO_PATH_REGISTRIES


# ── r-registry-latest-only (2026-07-26) ──────────────────────────────

from routes.white_glove_propagation import _official_registry_latest_only


def test_official_registry_scans_latest_version_only():
    """The registry search payload carries EVERY historical version; old
    descriptions legitimately hold retired counts. Drift must be judged on
    the isLatest entry only — the 2026-07-26 false-permadrift regression."""
    import json as _json
    body = _json.dumps({"servers": [
        {"server": {"version": "2.1.22",
                    "description": "stale era: 33 MCP tools, 21,000+ facilities"},
         "_meta": {"io.modelcontextprotocol.registry/official":
                   {"isLatest": False}}},
        {"server": {"version": "2.5.1",
                    "description": "clean latest — query and cite."},
         "_meta": {"io.modelcontextprotocol.registry/official":
                   {"isLatest": True}}},
    ]})
    out = _official_registry_latest_only(body)
    assert "2.5.1" in out and "33 MCP tools" not in out


def test_official_registry_filter_failsoft_on_non_json():
    raw = "<html>33 tools somewhere</html>"
    assert _official_registry_latest_only(raw) == raw


# ── WITHDRAWN-CAPABILITY DRIFT (regex markers) ───────────────────────
# The class the number/literal markers cannot see: a retired capability still
# advertised as live. DCGI was withdrawn 2026-08-08; listings kept selling it.

def test_withdrawn_capability_advertised_as_live_is_flagged():
    copy = "the DCGI (Data Center Gas Index): per-state natural-gas suitability for siting."
    out = detect_number_drift(wrap(copy), CANON)
    kinds = [f["kind"] for f in out]
    assert "stale_capability" in kinds, f"a live DCGI claim must flag: {out}"
    hit = next(f for f in out if f["kind"] == "stale_capability")
    assert hit["found"].upper() == "DCGI"


def test_corrected_copy_that_says_withdrawn_never_flags():
    # Our own corrected copy pairs DCGI with 'withdrawn' in the same sentence —
    # the zero-false-positive guarantee must hold for it.
    copy = ("per-state gas pipeline/operator presence with live Henry Hub "
            "(the DCGI composite was withdrawn 2026-08-08 — inputs, not a score).")
    assert detect_number_drift(wrap(copy), CANON) == [], \
        "the corrected 'DCGI ... withdrawn' copy must NOT flag"


def test_no_dcgi_mention_never_flags():
    copy = "markets scored by the DCPI power index; grid & fiber intel."
    assert detect_number_drift(wrap(copy), CANON) == []


def test_a_malformed_regex_marker_is_skipped_not_raised():
    canon = dict(CANON, stale_markers_regex=[{"re": r"(unterminated", "label": "x"}])
    # must not raise; returns whatever the other (none here) markers find
    assert detect_number_drift(wrap("all tools live and cited"), canon) == []


def test_the_regex_marker_is_case_insensitive_and_word_bounded():
    # lower-case dcgi as a live claim still flags...
    assert any(f["kind"] == "stale_capability"
               for f in detect_number_drift(wrap("our dcgi gas score is live."), CANON))
    # ...but a substring inside another word must not (word boundary)
    assert detect_number_drift(wrap("the XDCGIY token is unrelated."), CANON) == []


# ── GUARD THE SHIPPED PINNED PATTERN (not just the test fixture) ──────
# The tests above use their own CANON regex. This one uses the REAL
# ai_surface_canon.PINNED["stale_markers_regex"] so a future edit that breaks
# the shipped DCGI pattern (e.g. drops the "(?!...withdrawn)" lookahead) is
# caught — the corrected copy would start self-flagging.

def _canon_from_pinned():
    return {
        "tools": None, "tools_live": None,
        "deals_floor": None, "facilities_floor": None, "markets_floor": None,
        "stale_markers": [],
        "stale_markers_regex": list(_PINNED.get("stale_markers_regex") or []),
    }


def test_shipped_pinned_has_a_dcgi_withdrawn_marker():
    pats = [ (e.get("re") if isinstance(e, dict) else e)
             for e in (_PINNED.get("stale_markers_regex") or []) ]
    assert any("DCGI" in (p or "") for p in pats), \
        "PINNED lost its DCGI withdrawn-capability marker"


def test_shipped_pattern_flags_a_live_dcgi_claim():
    copy = "the DCGI (Data Center Gas Index): per-state natural-gas suitability for siting."
    out = detect_number_drift(wrap(copy), _canon_from_pinned())
    assert any(f["kind"] == "stale_capability" for f in out), \
        f"the SHIPPED pattern must flag a live DCGI claim: {out}"


def test_shipped_pattern_never_flags_the_corrected_withdrawn_copy():
    # This is the assertion that dies if the lookahead is ever removed from
    # PINNED — the corrected copy pairs DCGI with 'withdrawn' in-sentence.
    copy = ("per-state gas pipeline/operator presence with live Henry Hub "
            "(the DCGI composite was withdrawn 2026-08-08 — inputs, not a score).")
    assert detect_number_drift(wrap(copy), _canon_from_pinned()) == [], \
        "SHIPPED pattern false-flagged our own corrected 'DCGI ... withdrawn' copy"


# ── ★★★ THE DISCLAIMER MAY SIT IN AN ADJACENT SENTENCE, OR BEHIND THE TERM ──
# 2026-08-25. The withdrawn-capability negation used to be a lookahead inside
# each PINNED pattern: `(?![^.]*withdrawn)` — sentence-scoped AND forward-only.
# Both limits produced daily FALSE POSITIVES on the live smithery.ai listing,
# which kept our highest-volume registry permanently "drifted" and burned the
# auto_path on a non-problem. The strings below are COPIED FROM PRODUCTION —
# they are the exact shapes that were flagging. Keep them verbatim: a fixture
# paraphrase would not have caught this.

_REAL_GAS_INDEX_DESC = (
    "Data Center Gas Index (DCGI) — the per-US-state natural-gas suitability "
    "score. ★ WITHDRAWN 2026-08-08: this tool no longer returns a score. "
    "The backend returns an `unavailable_reason` naming the defects."
)

# Trimmed from the live get_gas_intelligence description. What matters is
# preserved: DCGI is named ~700 chars BEFORE its withdrawal note, and named
# again twice AFTER it.
_REAL_GAS_INTEL_DESC = (
    "The GAS analogue of get_grid_intelligence: fuses the DC Hub Gas Index "
    "(DCGI), live Henry Hub, gas-to-grid $/MWh across heat-rate scenarios, "
    "pipeline-operator presence, and the live grid gas share into one "
    "per-STATE brief. " + ("Params: region (US state code or name). " * 12) +
    "★ WITHDRAWN 2026-08-08: dcgi_score, dcgi_verdict and the "
    "behind-the-meter-vs-grid delta are NO LONGER RETURNED — two of the "
    "DCGI's three terms were measurably wrong. DO NOT quote a cached DCGI "
    "score. Use get_gas_index for the DCGI score alone."
)


def test_real_gas_index_description_does_not_flag():
    """Disclaimer is only 4 chars away — but past a period. The old
    sentence-scoped lookahead could not see it."""
    assert detect_number_drift(wrap(_REAL_GAS_INDEX_DESC), _canon_from_pinned()) == [], \
        "the shipped get_gas_index description must not self-flag"


def test_real_gas_intelligence_description_does_not_flag():
    """Four DCGI mentions: one ~700 chars AHEAD of the withdrawal note, and
    two BEHIND it. No lookahead can reach backwards."""
    assert detect_number_drift(wrap(_REAL_GAS_INTEL_DESC), _canon_from_pinned()) == [], \
        "the shipped get_gas_intelligence description must not self-flag"


def test_disclaimer_behind_the_term_does_not_flag():
    copy = ("★ WITHDRAWN 2026-08-08: the gas index no longer returns a "
            "score. Do not quote a cached DCGI score.")
    assert detect_number_drift(wrap(copy), _canon_from_pinned()) == [], \
        "a term whose withdrawal note precedes it must not flag"


def test_disclaimer_in_the_next_sentence_does_not_flag():
    copy = "per-state natural-gas suitability score. WITHDRAWN 2026-08-08: retired."
    assert detect_number_drift(wrap(copy), _canon_from_pinned()) == [], \
        "a disclaimer one sentence later must not flag"


def test_a_live_claim_far_from_any_withdrawal_note_still_flags():
    """The widened window must not become a blanket amnesty: a genuine live
    claim with the word 'withdrawn' only far away still has to fire."""
    copy = "the DCGI: per-state gas suitability, updated daily." + ("filler. " * 200) + "withdrawn"
    out = detect_number_drift(wrap(copy), _canon_from_pinned())
    assert any(f["kind"] == "stale_capability" for f in out), \
        f"a live DCGI claim outside the disclaimer window must still flag: {out}"


def test_pinned_markers_are_plain_terms_not_lookaheads():
    """The proximity test belongs in detect_number_drift (bidirectional), not
    in the pattern. A re-added `(?!...)` silently stops covering the
    disclaimer-behind case — the bug this replaced."""
    for e in (_PINNED.get("stale_markers_regex") or []):
        pat = e.get("re") if isinstance(e, dict) else e
        assert "(?!" not in (pat or ""), (
            f"stale_markers_regex entry re-added an inline lookahead: {pat!r} — "
            "the disclaimer window is detect_number_drift's job")


def test_withdrawn_proximity_is_case_insensitive_by_itself():
    """Production writes '★ WITHDRAWN' in caps. The guard must hold even
    if a caller forgets re.IGNORECASE."""
    assert _WITHDRAWN_RE.search("★ WITHDRAWN 2026-08-08")
    assert _WITHDRAWN_RE.search("was withdrawn")


# ── OVER-claims, not just stale numbers ──────────────────────────────
#
# ★2026-08-28. The facilities band was MULTIPLICATIVE — floor..2×floor —
# so it grew with the fleet and stopped bounding anything. At the live
# floor of 19,300 it accepted anything up to 38,600. Measured the same
# day, both of these were live and unflagged:
#
#   glama.ai/mcp/connectors/cloud.dchub/…   "21,000+ facilities"  x6
#   punkpeye/awesome-mcp-servers README     "21,000+ data-center facilities"
#
# against /api/v1/stats facilities = 19,366. That is ~1,600 facilities we
# do not have, advertised on two partner surfaces.
#
# An over-claim is the worse half of this detector's job: a stale listing
# undersells a real fleet, an over-claim is unbacked.

# The live canon as of 2026-08-28 (floor = live rounded DOWN to 100).
LIVE_FLOOR_CANON = dict(CANON, facilities_floor=19300)
# The DEGRADED path: resolve_canon down, hand-maintained PINNED stands in
# at 18,500 while live is 19,366 — an 866-wide lag the band must absorb.
PINNED_LAG_CANON = dict(CANON, facilities_floor=18500)


def test_partner_overclaim_flags_against_the_live_floor():
    """The real Glama / awesome-mcp-servers copy, verbatim."""
    drifts = detect_number_drift(
        wrap("21,000+ data-center facilities (170+ countries)"),
        LIVE_FLOOR_CANON)
    assert any(d["kind"] == "facilities" and d["found"] == 21000
               for d in drifts), \
        f"21,000+ over-claims a 19,366 fleet and must flag, got {drifts}"


def test_partner_overclaim_flags_on_the_degraded_pinned_path_too():
    """A resolver outage must not become a licence to over-claim."""
    drifts = detect_number_drift(
        wrap("21,000+ data-center facilities"), PINNED_LAG_CANON)
    assert any(d["kind"] == "facilities" for d in drifts), \
        f"expected over-claim flag under the pinned floor, got {drifts}"


def test_the_real_live_count_never_flags_under_the_pinned_lag():
    """ZERO-FALSE-POSITIVE: an honest listing quoting the LIVE count while
    the pinned floor lags 866 behind must stay clean. This is the case the
    width exists for — set it below ~900 and this goes red."""
    assert detect_number_drift(
        wrap("19,366 discovered facilities"), PINNED_LAG_CANON) == []


def test_floor_plus_exact_live_never_flags():
    assert detect_number_drift(
        wrap("19,366 discovered facilities"), LIVE_FLOOR_CANON) == []


def test_must_fail_control_the_old_multiplicative_band_accepted_it():
    """CONTROL: pin down WHY this was invisible. Under floor..2×floor the
    over-claim sat comfortably inside the accepted range, so the detector
    was not wrong-by-accident — it was configured to allow it."""
    floor = 19300
    assert floor <= 21000 <= floor * 2, \
        "if this fails the old band did not actually accept 21,000 and " \
        "the premise of this change is wrong"
    # And the new band does not.
    from routes.white_glove_propagation import FACILITIES_BAND_WIDTH
    assert not (floor <= 21000 < floor + FACILITIES_BAND_WIDTH)
# ── markets: the fact the issue advertised but never checked ─────────
#
# ★2026-08-28. load_canon() has always parsed markets_floor, and
# _issue_body() has always PRINTED it on the "Canonical numbers" line:
#
#   **Canonical numbers (ai_surface_canon):** `82 tools` ·
#   `1,900+ tracked deals` · `18,900+ facilities` · `300+ markets`
#
# detect_number_drift had no markets branch. The lane published a
# canonical fact it was not checking, and punkpeye/awesome-mcp-servers has
# carried "232 US power markets" against 300+ since its listing was
# written, never once flagged.
#
# ★Whatever the issue CLAIMS is canonical, the detector must actually
# check — otherwise the header is a promise the lane cannot keep.


def test_the_real_awesome_mcp_markets_undercount_flags():
    """The live README copy, verbatim."""
    drifts = detect_number_drift(
        wrap("232 US power markets scored by the DC Hub Power Index (DCPI)"),
        CANON)
    assert any(d["kind"] == "markets" and d["found"] == 232
               for d in drifts), \
        f"232 markets against a 300+ canon must flag, got {drifts}"


def test_canonical_markets_phrase_never_flags():
    """ZERO-FALSE-POSITIVE: our own copy, both rendered shapes."""
    assert detect_number_drift(wrap("300+ DCPI markets"), CANON) == []
    assert detect_number_drift(wrap("300+ markets"), CANON) == []


def test_live_markets_above_floor_never_flags():
    """The floor rounds DOWN — live 320 against a 300+ floor is honest."""
    assert detect_number_drift(wrap("320 markets"), CANON) == []


def test_markets_and_tools_phrase_still_clean_with_the_new_branch():
    """The regression this file already guards, re-asserted against the
    branch most likely to reintroduce it. A generic \\w+ modifier here is
    exactly what once matched '300 markets and tools' as a tool count."""
    assert detect_number_drift(
        wrap("300+ markets and tools for the grid"), CANON) == []


def test_editorial_market_counts_are_not_our_claim():
    assert detect_number_drift(
        wrap("3 markets in Texas and 12 markets in the Southeast"),
        CANON) == []


def test_markets_overclaim_flags_too():
    """Symmetric with facilities: an over-claim is unbacked, not merely
    stale."""
    drifts = detect_number_drift(wrap("900 DCPI markets"), CANON)
    assert any(d["kind"] == "markets" and d["found"] == 900 for d in drifts)


def test_the_issue_header_promise_is_now_backed_by_a_check():
    """Static guard tying the two together: if _issue_body renders
    markets_floor, detect_number_drift must emit a markets kind. This is
    the invariant that was broken, so it is asserted directly rather than
    left to the behavioural tests above."""
    import inspect
    from routes import white_glove_propagation as wgp
    body_src = inspect.getsource(wgp._issue_body)
    det_src = inspect.getsource(wgp.detect_number_drift)
    if "markets_floor" in body_src:
        assert '_add("markets"' in det_src, (
            "_issue_body advertises markets_floor as canonical; "
            "detect_number_drift must check it")