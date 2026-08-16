"""Guards for the QA smoke/attribution UA exclusion (2026-08-15).

The /ai platform cards and MCP counts reported copilot/gemini/grok as live
platform channels — every row was ONE QA smoke agent (agent_id 62a465b0…,
UAs 'Copilot-QA/1.0 (dchub approval smoke test)', 'GeminiCLI/1.0
(attribution-test)', 'Cursor/1.0 (attribution-test)', 'Grok-DC-Hub/1.0'),
plus a second QA agent (840d969f…, 'qa-judge-probe-*', 'acme-siting-agent/1.0',
'reviewer-sim') inflating the claude channel. All passed is_real_external
because the exclusions were platform-TAG and UA-FAMILY based and none of
these UAs matched an existing family.

Two-sided on purpose, same as test_registry_crawler_exclusion.py: wrongly
excluding a REAL platform silently deletes a customer to make a metric
prettier, so the keep-side matters more than the exclude-side. In particular
the exclusion is by UA SIGNATURE, never by platform tag — a real agent
arriving on the copilot/gemini/grok/mistral/cursor channels must still count.

Pure functions: no DB, no network, never imports main.
"""
import re

import pytest

dl = pytest.importorskip("mcp_calls_deloop")

# ★ DERIVED from the predicate, never transcribed — a hardcoded copy of the
# regex drifts the moment someone edits a family. real_ua_predicate() renders
# "COALESCE(user_agent,'') !~* '(<alternation>)'"; we lift the alternation and
# run it exactly as Postgres would (case-insensitive, unanchored search).
# Lazy (inside the helper, not module scope) so a shape change in the
# predicate surfaces as a test FAILURE, never a collection error.
def _ua_excluded(ua: str) -> bool:
    m = re.search(r"!~\*\s+'\((.+)\)'", dl.real_ua_predicate())
    assert m, "real_ua_predicate() no longer renders a !~* '(...)' regex"
    return bool(re.search(m.group(1), ua or "", re.I))


QA_FLEET_UAS = [
    # agent 62a465b0… — the platform-tag smoke agent
    "Copilot-QA/1.0 (dchub approval smoke test)",
    "Copilot-QA/1.0",  # the bare form — 8 of the 12 rows carried NO suffix
    "GeminiCLI/1.0 (attribution-test)",
    "Cursor/1.0 (attribution-test)",
    "Grok-DC-Hub/1.0",
    # agent 840d969f… — the judge-probe personas
    "qa-judge-probe-20260814-a",
    "acme-siting-agent/1.0",
    "acme-siting-agent",  # bare form, also observed live
    "reviewer-sim",
    # reviewer-sim's actual wire UA: the two-segment Chrome malformation
    # (real Chrome is MAJOR.0.0.0). 120.0 dodged the old 126.0-only literal.
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
]

# 2026-08-16 — the "cursor" one-off verification scripts: 11 calls, all
# agent ff29c4ac… (one operator Comcast IP, 2026-07-28 only, same session as
# the dchub-attrib-probe fleet). Exact fingerprints with the trailing slash —
# see the two-sided rule on REAL_UAS below for why not verify/audit families.
STARTERPACK_VERIFY_UAS = [
    "starterpack-verify/1.0",
    "starterpack-audit/1.0",
    "order-verify/1.0",
    # same agent/session, surfaced by sweeping its remaining real rows:
    # no dchub- prefix, and 'starterpack-audit/' does not match it
    "keyless-audit-probe/1.0",
]

# 2026-08-16 (r2) — the same leftover-rows sweep against the OTHER operator
# agent, 840d969f… (72.208.88.69, the qa-judge machine). Every UA here is
# operator-IP-only across the whole table.
QA_JUDGE_LEFTOVER_UAS = [
    "Mozilla/5.0 (adversarial-verify)",   # skeptic fleet, 12 calls 08-13
    "Mozilla/5.0 (verify2)",
    # the Chrome disguise MUTATED: no dot at all / UA ends at bare version —
    # both dodge the \.0-requiring pattern the 08-15 block generalized to
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120",
    "cursor-mcp/1.0",   # 07-05 attribution-era fabrications, version-pinned
    "cline-mcp/1.0",
    "healthcheck/1.0",
    "qa-judge-keyed-1785834895",  # dodged the old 'qa-judge-probe' form
]

# 2026-08-16 (r3) — smoke agent 62a465b0 + the prefix-wide close-out: the
# operator LAN is an IPv6 /64 with privacy rotation, so these appeared under
# three different agent_ids. All operator-prefix-only across the table.
SMOKE_AGENT_LEFTOVER_UAS = [
    "mistral-org-agent/1.0",              # hand-built Mistral connector relay
    "mistral-org-agent-relay/1.0 (dchub_1 connector)",
    "mistral-org-agent-relay/1.0 (dchub_1 connector; executed for ag_019f7477)",
    "audit/1.0",                          # bare one-off; matched ANCHORED only
]

# Real client UAs that MUST keep counting. GeminiCLI/Cursor/Grok WITHOUT the
# QA decoration are exactly what a genuine arrival on those channels looks
# like — if any of these trips the guard, the exclusion stopped being a
# signature match and started deleting the channel.
REAL_UAS = [
    "GeminiCLI/1.0",
    "Cursor/1.0",
    "Grok/1.2",
    "claude-ai/1.0",
    "Claude-User/1.0 (+claude.ai)",
    "ChatGPT-User/1.0 (+https://openai.com/bot)",
    "PerplexityBot/1.0",
    "node",  # mcp-remote's bare transport UA — documented as deliberately kept
    "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",  # REAL Chrome (MAJOR.0.0.0, not 126.0)
    # Live third-party verifier/auditor clients (observed in mcp_tool_calls,
    # 2026-08-16). These are why the starterpack exclusions are EXACT
    # fingerprints: a broad 'verify'/'audit' UA family would delete them.
    "LinerMCPVerifier/1.0",
    "TrimtabVerifier/0.1 (+https://trimtabist.com/verifier; mrigank86@gmail.com)",
    "SaSame-MCP-Audit/0.1",
    # Mistral's REAL MCP client (Azure egress, 2026-07-18) — must survive the
    # 'mistral-org-agent' relay exclusion.
    "MistralAI-MCPClient/1.0",
    # pins the ^ anchor on 'audit/1.0': a named third-party auditor at
    # version 1.0 must not be swallowed by the bare-'audit/1.0' fingerprint
    "AcmeCorp-Audit/1.0",
]


@pytest.mark.parametrize("ua", QA_FLEET_UAS)
def test_qa_fleet_uas_are_excluded(ua):
    assert _ua_excluded(ua), f"QA UA still counts as real external: {ua!r}"


@pytest.mark.parametrize("ua", STARTERPACK_VERIFY_UAS)
def test_starterpack_verify_uas_are_excluded(ua):
    assert _ua_excluded(ua), (
        f"internal verification UA still counts as real external: {ua!r}")


@pytest.mark.parametrize("ua", QA_JUDGE_LEFTOVER_UAS)
def test_qa_judge_leftover_uas_are_excluded(ua):
    assert _ua_excluded(ua), (
        f"qa-judge leftover UA still counts as real external: {ua!r}")


@pytest.mark.parametrize("ua", SMOKE_AGENT_LEFTOVER_UAS)
def test_smoke_agent_leftover_uas_are_excluded(ua):
    assert _ua_excluded(ua), (
        f"smoke-agent leftover UA still counts as real external: {ua!r}")


def test_versioned_fingerprints_stay_version_pinned():
    """cursor-mcp/cline-mcp/healthcheck are excluded at the OBSERVED version
    only, deliberately: a future real client shipping one of these names at a
    new version must count until evidence says otherwise."""
    for ua in ("cursor-mcp/2.0", "cline-mcp/2.1", "healthcheck/2.0"):
        assert not _ua_excluded(ua), f"version pin leaked to: {ua!r}"


@pytest.mark.parametrize("ua", REAL_UAS)
def test_real_uas_are_never_excluded(ua):
    """The expensive direction. A false exclusion deletes a real customer."""
    assert not _ua_excluded(ua), f"real UA wrongly excluded: {ua!r}"


def test_platform_tags_stay_countable():
    """The fix is UA-signature-scoped BY DESIGN: the bare platform tags the QA
    agent wrote must remain valid channels for a future real agent. The
    write-time platform predicate must keep them."""
    fams = (dl.INTERNAL_TAG_FAMILIES + "|" + dl.REGISTRY_CRAWLER_FAMILIES)
    fam_re = re.compile(fams, re.I)
    for tag in ("copilot", "gemini", "grok", "mistral", "cursor"):
        assert tag not in {v.lower() for v in dl.INTERNAL_PLATFORM_VALUES}, (
            f"platform tag {tag!r} landed in INTERNAL_PLATFORM_VALUES — that "
            "deletes the channel, not the QA agent")
        assert not fam_re.search(tag), (
            f"platform tag {tag!r} matches an exclusion family")


def test_qa_persona_platform_tags_are_excluded():
    """reviewer-sim arrived wearing a Chrome disguise, so its UA is not a
    stable fingerprint — the exact WRITE-TIME tags kill the personas
    regardless of costume. These are the fleet's own invented names, never a
    real channel, so exact-tag exclusion cannot delete a customer."""
    vals = {v.lower() for v in dl.INTERNAL_PLATFORM_VALUES}
    for tag in ("reviewer-sim", "acme-siting-agent"):
        assert tag in vals, f"QA persona tag {tag!r} not excluded by tag"


def test_loopback_first_hop_is_not_external():
    """824 rows/30d (measured 2026-08-15) reached the funnel's per-platform
    breakdown from 127.0.0.1 — our own box, wearing grok/mistral/claude as
    client_name with the kept-by-design 'node' UA. A loopback FIRST hop can
    never be an external caller; NULL/empty is KEPT (no IP is not evidence
    of self-traffic)."""
    pred = dl.loopback_self_predicate()
    assert "COALESCE" in pred, "NULL ip_address must be kept, not dropped"
    for lit in ("'127.0.0.1'", "'::1'", "'localhost'"):
        assert lit in pred, f"loopback literal {lit} missing"
    # Simulate the SQL: first XFF token, trimmed, against the deny set.
    def excluded(ip):
        return (ip or "").split(",")[0].strip() in (
            "127.0.0.1", "::1", "localhost")
    assert excluded("127.0.0.1")
    assert excluded(" 127.0.0.1 , 8.8.8.8")  # loopback FIRST hop
    assert excluded("::1")
    assert not excluded("8.8.8.8")
    assert not excluded("8.8.8.8, 127.0.0.1")  # loopback later in chain is fine
    assert not excluded("")     # NULL/empty kept
    assert not excluded(None)


def test_loopback_guard_is_composed_into_real_calls():
    """The guard only matters if the canonical predicate carries it — the
    07-30 twin-drift showed a family can exist in source and be absent where
    it matters."""
    assert dl.loopback_self_predicate() in dl.real_calls_predicate(), (
        "loopback_self_predicate() is not part of real_calls_predicate()")


def test_view_migration_carries_the_families():
    """The live verdict is the RENDERED view, not the module — the 07-30 drift
    (LIKE form updated, regex twin stale) is the proof that a family can exist
    in source and be absent where it matters. The newest identity-view
    migration must contain the QA families."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    mig = sorted(repo.glob("migrations/*mcp_calls_identity*.sql"))[-1]
    sql = mig.read_text()
    for fam in ("smoke test", "attribution-test", "qa-judge-",
                "acme-siting-agent", "reviewer-sim", "dc-hub",
                # 2026-08-16 — the "cursor" verification one-offs + the
                # same session's keyless-access audit probe
                "starterpack-verify/", "starterpack-audit/", "order-verify/",
                "keyless-audit-probe/",
                # 2026-08-16 r2 — the qa-judge leftover sweep
                "adversarial-verify", "cursor-mcp/1", "cline-mcp/1",
                "healthcheck/1",
                # 2026-08-16 r3 — smoke agent + prefix-wide close-out
                "mistral-org-agent", "^audit/1"):
        assert fam in sql, (
            f"family {fam!r} missing from {mig.name} — re-render with "
            "scripts/render_identity_views.py, never hand-edit")


# ── /ai "Latest AI Requests" feed: scanner probe paths ────────────────────

def _like(pattern: str, s: str) -> bool:
    """Simulate Postgres LIKE (case-sensitive, % wildcard only)."""
    return bool(re.fullmatch(
        ".*".join(re.escape(p) for p in pattern.split("%")), s, re.S))


def test_feed_drops_scanner_probe_paths():
    """A secrets scanner spoofing ChatGPT's UA probes /api/v1/env,
    /api/v1/config, /api/v1/settings (all 404, verified 2026-08-14); each row
    lands as platform='chatgpt' and rendered as live ChatGPT activity. The
    feed's probe-path patterns must catch the exact paths and querystring
    variants."""
    at = pytest.importorskip("ai_tracking")
    pats = [p + "%" for p in at.FEED_SCANNER_PROBE_PATHS]
    for path in ("/api/v1/env", "/api/v1/config", "/api/v1/settings",
                 "/api/v1/env?probe=1", "/api/v1/config.json"):
        assert any(_like(p, path) for p in pats), f"probe path kept: {path}"


def test_feed_probe_patterns_are_anchored_and_keep_content():
    """Anchored at the path START (no leading %) so real content endpoints
    can never be swept in by a substring."""
    at = pytest.importorskip("ai_tracking")
    for p in at.FEED_SCANNER_PROBE_PATHS:
        assert p.startswith("/"), f"probe path not anchored: {p!r}"
    pats = [p + "%" for p in at.FEED_SCANNER_PROBE_PATHS]
    for path in ("/facilities/ashburn-va", "/api/v1/markets/dallas",
                 "/api/v1/facilities/search?q=env",
                 "/transactions", "/api/v1/grid/status"):
        assert not any(_like(p, path) for p in pats), (
            f"real content path wrongly dropped from the feed: {path}")


def test_discovered_roster_filter_catches_qa_fleet():
    """The /ai platform cards extend with auto-discovered platforms; the
    read-time junk filter in mcp_auto_register must drop the QA fleet's
    identities while keeping legit registries/clients. Patterns are DERIVED
    from the function's SQL (comment lines stripped), not restated."""
    import inspect
    mar = pytest.importorskip("mcp_auto_register")
    src = inspect.getsource(mar.get_discovered_platforms_as_cards)
    sql_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("--")]
    pats = re.findall(r"'(%[^']+%)'", "\n".join(sql_lines))
    assert pats, "could not extract LIKE patterns from the roster filter"

    def dropped(identified_as, ua):
        hay = (identified_as + " " + ua).lower()
        return any(_like(p, hay) for p in pats)

    for name, ua in [
        ("Copilot-QA", "Copilot-QA/1.0 (dchub approval smoke test)"),
        ("GeminiCLI", "GeminiCLI/1.0 (attribution-test)"),
        ("Cursor", "Cursor/1.0 (attribution-test)"),
        ("Grok-DC-Hub", "Grok-DC-Hub/1.0"),
        ("acme-siting-agent", "acme-siting-agent/1.0"),
        ("reviewer-sim", "reviewer-sim"),
        ("qa-judge", "qa-judge-probe-20260814-a"),
    ]:
        assert dropped(name, ua), f"QA identity survives the roster: {name}"

    for name, ua in [
        ("Glama", "Glama/1.0"),
        ("Poe", "Poe/1.0 (+poe.com)"),
        ("Cursor", "Cursor/1.0"),
        ("Smithery", "Smithery/2.1"),
        ("Claude", "claude-ai/1.0"),
    ]:
        assert not dropped(name, ua), f"legit platform wrongly dropped: {name}"
