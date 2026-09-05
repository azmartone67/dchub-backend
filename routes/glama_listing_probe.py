"""routes/glama_listing_probe.py — Glama listing health, MEASURED not assumed.

Glama is the *inspected* registry: it runs its own health probe against
https://dchub.cloud/mcp, renders our tool schemas, and publishes a public
Healthy/Unhealthy badge. That badge is part of our public product — an
installer choosing between two DC Hub cards will not pick the red one.

We had two connector cards and only ever looked at one of them:

    /mcp/connectors/cloud.dchub/mcp-server
        mirrored from the official registry (cloud.dchub/mcp-server).
        Healthy, ownership verified, 83 tools.  KEEPER.
    /mcp/servers/azmartone67/dchub-mcp-server
        the repo half of the same entity; each links to the other.  KEEPER.
    /mcp/connectors/cloud.dchub/dc-hub-data-center-intelligence-mcp-server
        Glama-NATIVE — not in the official registry (?search=cloud.dchub
        returns ONE name), mcpServerId null, capabilities null.  DUPLICATE.

Measured 2026-09-05: the duplicate had been red since 2026-09-01 and still
advertised "33 tools" under the Energy category, while the keeper was tested
Healthy at 14:41 the same day with 83. Nothing in this codebase could see
that. `check_mcp_presence_stale` watches the presence *crawler*, which reads
a generic registry index for the substring "dchub" — it cannot see a health
badge, a tool count on a specific card, or a second card existing at all.

★ WHY THE DUPLICATE EXISTS, and why deleting it is not the fix on its own:
`/.well-known/glama.json` on our origin published the deprecated EMAIL form
(`maintainers[{email}]`), and that verifies *any* connector submitted against
the origin. So a second, hand-submitted connector on the same URL
auto-verified as ours. Glama's schema now marks that form deprecated in
favour of an opaque account-bound `claim: glama_claim_<32>` token. Until the
origin serves a claim token instead, a deleted duplicate can come straight
back. The origin half ships in dchub-frontend (_worker.js).

Parsing notes — these matter more than they look:
  * Glama's class names are hashed CSS-in-JS (`czikZZ`, `jrPWok`) and rotate
    on every deploy. NEVER anchor on them. We anchor on the `<dt>Status</dt>`
    label and take the first `<span>` inside the following `<dd>`.
  * ONLY connector pages carry a Status/Last Tested pair. The `/mcp/servers/…`
    repo listing has no health badge at all, so a missing Status there is the
    page working normally — `has_health_badge` says which is which, or the
    probe reports a layout break on a page that never had the block.
  * The tool count is read from the `Available Tools` badge, NOT from any
    "N tools" text on the page. Glama renders that badge with HTML comments
    inside it (`83<!-- --> tool<!-- -->s`), and the page ALSO carries their
    own AI review — "With 82 tools, this server is extremely heavy compared
    to typical MCP servers (3-15 tools)" — so a loose scan reads 15, 82 and
    83 off a page whose real answer is 83.
  * That badge exists only where Glama INTROSPECTED successfully. The
    unhealthy duplicate has no badge; its "33 tools" comes from a Glama-side
    cached description written long ago.
  * `/tools` returns 0 bytes to curl (SPA-only) and `/schema` carries no
    mirror sha. The overview/connector pages are the ones that render both.
  * Glama's JSON API now requires an API key (401 since ~2026-09), so HTML is
    the only keyless read.

FAIL-SAFE on transport, FAIL-CLOSED on parse: a page we could not fetch
yields NO finding (never a false alarm from Glama being down or from us being
rate-limited), but a page we DID fetch and could not parse yields a finding
that says so. "Could not run" is not "ran and passed".
"""
from __future__ import annotations

import json
import re

import requests

# Glama 403s a bare urllib UA the way Smithery does.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

KEEP = "keep"
DEPRECATE = "deprecate"

# Our own ownership document. Reading it closes the loop: the duplicate exists
# because this file verifies ANY connector on the origin, so a deprecated
# duplicate with the email form still published is a fix that can be undone by
# someone else's submission tomorrow.
OWNERSHIP_URL = "https://dchub.cloud/.well-known/glama.json"
CLAIM_RE = re.compile(r"^glama_claim_[A-Za-z0-9_-]{32}$")

# (key, url, disposition, has_health_badge)
GLAMA_LISTINGS: tuple[tuple[str, str, str, bool], ...] = (
    ("connector_registry_mirror",
     "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server", KEEP, True),
    ("server_repo_listing",
     "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server", KEEP, False),
    ("connector_glama_native_duplicate",
     "https://glama.ai/mcp/connectors/cloud.dchub/"
     "dc-hub-data-center-intelligence-mcp-server", DEPRECATE, True),
)

# Counts that were true once and can never be true again. Same shape as the
# surface-truth shell's _STALE_FLOOR: naming the retired literal is what lets a
# test assert against it — an omission cannot be asserted against.
#
# ★ OBSERVATIONAL ONLY — these never fire a finding of their own. On Glama the
# stale numbers live in GLAMA-AUTHORED copy: the cached "What can you do with
# this server?" summary and their AI quality review. Neither is reachable from
# this repo or from the Glama admin panel, so a finding on them would be a red
# that no commit can clear — which is how a monitor teaches people to ignore
# it. They ride in `detail` on findings a human CAN close.
RETIRED_COUNT_PHRASES: tuple[str, ...] = (
    "33 tools", "38 tools", "53 tools", "58 tools", "73 tools", "80 tools",
    "81 tools", "82 tools",
    "19,700+", "20,100+", "21,000+", "22,000+", "12,650+", "15,000+",
    "232 global DC markets", "232 markets", "232 US power markets",
)

_STATUS_RE = re.compile(
    r">Status</dt>.{0,400}?<span>\s*(Healthy|Unhealthy|Unknown)\s*</span>",
    re.I | re.S)
_TESTED_RE = re.compile(
    r">Last Tested</dt>.{0,400}?dateTime=\"([0-9T:.+\-]+)", re.I | re.S)
# Anchored on the badge, and tolerant of the HTML comments React emits inside
# it. Bounded lookahead so a missing badge fails to match rather than scanning
# to the far end of a 2 MB document.
# ★ Measured against the real page 2026-09-05 17:14Z, after the duplicate was
# deprecated — not guessed. Glama renders a plain <strong> banner with the
# owner's reason beneath it; there are no hashed class names in the marker
# itself, which makes it the sturdiest anchor on the page.
#     <strong>This connector has been deprecated</strong><div…><p>duplicate</p>
# The timestamp only appears in the page's data payload, where the value sits
# immediately after its key (before deprecation the key had no value at all).
_DEPRECATED_RE = re.compile(r"This connector has been deprecated", re.I)
_DEPRECATED_AT_RE = re.compile(
    r'\\?"deprecatedAt\\?"\s*,\s*\\?"([0-9TZ:.+\-]{10,40})\\?"')

_TOOLS_BADGE_RE = re.compile(
    r"Available\s+Tools</h\d>.{0,200}?>\s*(\d{1,4})\s*(?:<!--.*?-->)?\s*tool",
    re.I | re.S)


def parse_listing(html: str) -> dict:
    """Pull the published facts off one Glama listing page.

    Returns {status, last_tested, tools_badge, retired_phrases, status_found}.
    `status_found` is False when the page came back with no Status block. On a
    connector page that is a real signal (Glama redesigned, or the listing went
    away and we are reading a shell); on the repo listing it is normal, which
    is why the caller consults `has_health_badge` before reacting.
    `tools_badge` is None when Glama never introspected the server — the
    unhealthy duplicate renders no badge at all.
    """
    html = html or ""
    m = _STATUS_RE.search(html)
    t = _TESTED_RE.search(html)
    b = _TOOLS_BADGE_RE.search(html)
    dep = _DEPRECATED_RE.search(html)
    dep_at = _DEPRECATED_AT_RE.search(html)
    return {
        "status": (m.group(1).capitalize() if m else None),
        "last_tested": (t.group(1) if t else None),
        "tools_badge": (int(b.group(1)) if b else None),
        "deprecated": bool(dep),
        "deprecated_at": (dep_at.group(1) if dep_at else None),
        "retired_phrases": [p for p in RETIRED_COUNT_PHRASES if p in html],
        "status_found": bool(m),
    }


def _fetch(url: str, timeout: float = 10.0) -> str | None:
    """The page body, or None on ANY transport failure. Never raises.

    A non-200 counts as unreachable, not as a listing to parse: an error page
    has no Status block, so parsing it would raise the fail-closed
    `glama_listing_unparseable` finding on what is really a transport problem.
    """
    try:
        resp = requests.get(
            url, headers={"User-Agent": _UA, "Accept": "text/html"},
            timeout=timeout)
        return resp.text if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 — a probe must never take the caller down
        return None


def probe(fetch=_fetch) -> list[dict]:
    """Read every listing. `fetch` is injectable so tests never touch network."""
    out = []
    for key, url, disposition, has_badge in GLAMA_LISTINGS:
        body = fetch(url)
        row = {"key": key, "url": url, "disposition": disposition,
               "has_health_badge": has_badge, "reachable": body is not None}
        if body is not None:
            row.update(parse_listing(body))
        out.append(row)
    return out


def ownership_finding(fetch=_fetch) -> list[dict]:
    """Fire while the origin still publishes the deprecated email form.

    This is the half that makes a deprecation stick. `maintainers[{email}]`
    verifies ANY connector submitted against this origin — it is how the
    duplicate auto-verified as ours in the first place — and it publishes an
    address in cleartext on a public path, which Glama's own guidance says not
    to do. The replacement is an opaque account-bound claim token.

    FAIL-SAFE: our own endpoint being unreachable yields no finding.
    """
    body = fetch(OWNERSHIP_URL)
    if body is None:
        return []
    try:
        doc = json.loads(body)
    except Exception:  # noqa: BLE001
        return [{
            "issue": "glama_ownership_doc_unparseable",
            "url": OWNERSHIP_URL,
            "count": 1,
            "detail": ("the Glama ownership document did not parse as JSON. "
                       "Glama reads this to verify we own the origin; a broken "
                       "one costs verified ownership after a 7-day grace."),
        }]
    claim = doc.get("claim")
    if isinstance(claim, str) and CLAIM_RE.match(claim):
        return []
    return [{
        "issue": "glama_origin_publishes_email_ownership",
        "url": OWNERSHIP_URL,
        "count": 1,
        "detail": (
            "the origin still serves the DEPRECATED maintainers[{email}] "
            "ownership form" + (" (and a `claim` that does not match Glama's "
            "pattern — a typo'd claim is not a claim)" if claim else "") + ". "
            "That form verifies ANY connector submitted against this origin, "
            "which is how a duplicate DC Hub connector auto-verified as ours; "
            "deprecating that card does not stop another appearing. It also "
            "publishes an email address in cleartext on a public well-known "
            "path. Fix: copy the token from the Glama claim panel into "
            "GLAMA_CLAIM_TOKEN in dchub-frontend/_worker.js and deploy — that "
            "switches to claim-only in the same deploy. Never serve both."),
    }]


def findings(rows: list[dict], canon_tools: int | None = None) -> list[dict]:
    """Radar findings. Only conditions we can ACT on fire their own finding.

    Deliberately NOT a finding: the retired counts observed in Glama-authored
    copy (their cached "What can you do with this server?" summary and their AI
    quality review, which says "With 82 tools, this server is extremely heavy").
    That text is regenerated on Glama's cycle and cannot be reached from this
    repo or from the Glama admin panel — a daily red no commit can clear is how
    a monitor teaches people to ignore it. It rides in `detail` instead, where
    it informs a finding a human can actually close.
    """
    out: list[dict] = []

    for row in rows:
        if not row.get("reachable"):
            continue  # transport failure — fail SAFE, say nothing

        stale = ", ".join(row.get("retired_phrases") or []) or "none"

        # A missing Status block only means something on a page that HAS one.
        if row.get("has_health_badge") and not row.get("status_found"):
            out.append({
                "issue": "glama_listing_unparseable",
                "url": row["url"],
                "count": 1,
                "detail": (
                    f"Glama connector listing {row['key']} returned a page but no "
                    f"Status block. Their markup uses hashed CSS-in-JS class names "
                    f"that rotate on deploy, so this is most likely a layout change "
                    f"(re-anchor routes/glama_listing_probe.py:_STATUS_RE) — but it "
                    f"can also mean the listing was removed and we are reading a "
                    f"shell. Read the page before assuming which. This finding "
                    f"exists because a probe that cannot parse must not report the "
                    f"listing healthy: could-not-run is not ran-and-passed."),
            })
            continue

        if row["disposition"] == DEPRECATE:
            # ★ Deprecation is the END STATE, not a step toward one. Glama's
            # owner UI offers no delete for a claimed connector, and a
            # deprecated connector is banner-marked and sorted last in search.
            # There is nothing further a human can do here, so continuing to
            # fire would be a red that no action can clear — the thing this
            # module refuses to do with their cached copy, and it would be no
            # better here. Detection is KEPT rather than dropped so that an
            # un-deprecation fires again.
            if row.get("deprecated"):
                continue
            out.append({
                "issue": "glama_duplicate_connector_listed",
                "url": row["url"],
                "count": 1,
                "detail": (
                    f"A SECOND DC Hub connector is still listed on Glama "
                    f"(status {row['status']}, last tested {row['last_tested']}, "
                    f"Available-Tools badge {row['tools_badge']}, retired counts in "
                    f"its copy: {stale}). It is Glama-native — not in the official "
                    f"registry — so it carries its own category, description and "
                    f"health clock, and an installer comparing two DC Hub cards can "
                    f"land on the worse one. It is NOT marked deprecated. Two "
                    f"actions, and the first alone does not hold: "
                    f"(1) DEPRECATE this connector in the Glama owner UI "
                    f"— deprecation, not deletion, is what the UI offers for a "
                    f"claimed connector, and Glama models it as first-class state "
                    f"(`deprecatedAt` + `deprecationComment` on the record); "
                    f"(2) serve a `claim: glama_claim_<32>` token from "
                    f"/.well-known/glama.json instead of the deprecated "
                    f"maintainers[{{email}}] form — the email form verifies ANY "
                    f"connector submitted against this origin, which is how this "
                    f"one auto-verified as ours, and leaving it in place lets a "
                    f"deleted duplicate come straight back."),
            })
            continue

        if row["status"] == "Unhealthy":
            out.append({
                "issue": "glama_listing_unhealthy",
                "url": row["url"],
                "count": 1,
                "detail": (
                    f"Glama's own probe reports DC Hub {row['key']} UNHEALTHY "
                    f"(last tested {row['last_tested']}). This badge is public and "
                    f"sits next to the install button. Check the live endpoint "
                    f"first — an anonymous POST to https://dchub.cloud/mcp should "
                    f"return 200 with an mcp-session-id — then, if we are up, "
                    f"re-run Sync Server THEN Deploy in the Glama admin (deploy "
                    f"alone rebuilds the mirror's frozen SHA forever)."),
            })

        badge = row.get("tools_badge")
        if canon_tools and badge is not None and badge != canon_tools:
            out.append({
                "issue": "glama_listing_tool_count_drift",
                "url": row["url"],
                "count": 1,
                "detail": (
                    f"Glama listing {row['key']} advertises {badge} tools; canon is "
                    f"{canon_tools}. Glama publishes tools from a RELEASE cut from a "
                    f"build of its repo MIRROR, so the fix is Sync Server THEN "
                    f"Deploy — in that order, because deploy alone rebuilds the "
                    f"frozen SHA — and the rendered schema then lags the green build "
                    f"by tens of minutes. Allow that grace window before treating "
                    f"this as stuck. Retired counts also on the page: {stale}."),
            })

    return out
