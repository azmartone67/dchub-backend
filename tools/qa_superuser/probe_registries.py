#!/usr/bin/env python3
"""Surface 7 — THIRD-PARTY LISTINGS, the copy we cannot see and cannot fix.

Every canon guard in this platform protects surfaces we own: llms.txt, the
manifest, the pricing page, the tool descriptions. A registry listing is
structurally invisible to all of them — it lives on someone else's server, is
populated from a submission we made once, and never re-reads anything.

Measured 2026-08-08, Glama (`glama.ai/api/mcp/v1/servers/azmartone67/
dchub-mcp-server`), live and returning HTTP 200:

    "33 tools covering 21,000+ data-center facilities … 2,000+ tracked M&A deals"
    "tools": []

Live canon at the same moment: **82 tools, 17,096 facilities, 1,745 deals**. So
an agent browsing that directory sees a server with **40% of the real tool
count**, an over-claim on facilities and deals, and — worse — an EMPTY tool
list, which in a tool-search directory is indistinguishable from having nothing
to offer. The listing had drifted for months with nothing watching it, because
nothing could: the repo's `glama.json` carries only `maintainers` (that is the
entire published schema), so the description is not ours to set from code.

## What this probe claims, and what it refuses to claim

* **Under-claiming our own advertised numbers is RED.** The bar is not invented:
  it is the platform's OWN canon, read live in the same run. A listing that
  says 33 tools when tools/list says 82 is wrong by the platform's own account.
* **OVER-claiming is RED at higher severity.** "21,000+ facilities" against a
  deduped 17,096 is not a stale cache, it is a false public claim — the class
  the honest-numbers fence exists to stop, on a surface the fence cannot reach.
* **An empty tool list on a remote-capable listing is RED.** The directory says
  it hosts a remote server and shows zero tools; that is the listing failing at
  its only job.
* **A listing we cannot fetch or parse is BLIND.** A registry being down, rate
  limiting us, or changing its JSON shape is not evidence our listing is wrong.
  Rule 1, and it matters more here than anywhere: these are other people's
  servers and they will break on their own schedule.
* **No opinion about listings we never submitted.** Absence from a directory is
  a business decision, not a defect. This probe only judges listings that
  EXIST and carry our name.

## Why it lives in the QA super-user and not in a shell

It must run from OUTSIDE, on GitHub Actions, against the public internet — a
check that runs inside the platform cannot see what a third party publishes
about us. Same reason the whole harness exists.
"""
from __future__ import annotations

import re
import time

from .finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED,
                      SEAT_NONE, Finding, blind, stable_key)
from .http import Unreachable, fetch, get_json

SURFACE = "registry"

# Listings we have submitted and can read back programmatically. A registry
# with no machine-readable record cannot be watched here — it needs a human,
# and saying so is better than pretending coverage.
LISTINGS = (
    {
        "name": "glama",
        "url": ("https://glama.ai/api/mcp/v1/servers/"
                "azmartone67/dchub-mcp-server"),
        "text_fields": ("description",),
        "page": "https://glama.ai/mcp/servers/qa3uoznre7",
        # ★★★ The tool inventory is counted from the RENDERED schema page, NOT
        #   from the API record's `tools` field. That field is an empty list for
        #   EVERY server in the registry — verified 2026-08-16 against
        #   microsoft/playwright-mcp, github/github-mcp-server and
        #   getsentry/sentry-mcp, all official and all unquestionably built and
        #   distributed. Reading it produced a RED that no server on earth could
        #   have passed, and that vacuous finding stood for five days claiming
        #   Glama's build was broken while the schema page listed all 82 tools
        #   correctly. Do not reintroduce a check on `.tools` from this endpoint.
        "schema_page": ("https://glama.ai/mcp/servers/"
                        "azmartone67/dchub-mcp-server/schema"),
        # Each tool is a link to its own detail page; distinct hrefs = inventory.
        "tool_link": re.compile(
            r"/mcp/servers/azmartone67/dchub-mcp-server/tools/([A-Za-z0-9_-]+)"),
    },
)

# Canon read LIVE in the same run — never a constant in this file, or the guard
# becomes the next thing that drifts.
_CANON_TOOLS_URL = "https://dchub.cloud/.well-known/mcp.json"
_CANON_STATS_URL = "https://dchub.cloud/api/ai/query?type=stats"


def _int(s: str) -> int:
    return int(re.sub(r"[^0-9]", "", s or "0") or 0)


def read_canon() -> dict | None:
    """{tools, facilities, deals} from the platform's own live surfaces.

    ★ The stats leg gets ONE retry. From the CI seat the stats call
    intermittently fails while tools succeeds, and a canon of {tools} alone
    silently demotes the two most severe registry findings — the
    facilities/deals OVER-claims — to a bland "no canon to check" gauge
    (2026-08-09: board read 3 red/0 critical in CI vs 4 red/2 critical from a
    local seat, same probe, same registries). A retry is cheap; giving up
    without one is how the worst findings became invisible exactly where the
    harness runs."""
    out = {}
    code, man = get_json(_CANON_TOOLS_URL)
    if code == 200 and isinstance(man, dict):
        tools = man.get("tools")
        if isinstance(tools, list) and tools:
            out["tools"] = len(tools)
        elif isinstance(man.get("tool_count"), int):
            out["tools"] = man["tool_count"]
    for attempt in (0, 1):
        code, stats = get_json(_CANON_STATS_URL)
        if code == 200 and isinstance(stats, dict):
            txt = str(stats.get("suggested_response") or "")
            m = re.search(r"([\d,]+)\s+data center facilities", txt)
            if m:
                out["facilities"] = _int(m.group(1))
            m = re.search(r"([\d,]+)\s+M&A transactions", txt)
            if m:
                out["deals"] = _int(m.group(1))
        if "facilities" in out or "deals" in out:
            break
        if attempt == 0:
            time.sleep(2.5)
    return out or None


# What a listing's prose claims, keyed to the canon field it should match.
_CLAIM_PATTERNS = (
    ("tools", re.compile(r"(\d[\d,]*)\s*\+?\s*tools?\b", re.I)),
    ("facilities", re.compile(r"(\d[\d,]*)\s*\+?\s*(?:data[- ]center\s+)?"
                              r"facilities\b", re.I)),
    # ★ Listing prose arrives ESCAPED — Glama returns "M\\&A", not "M&A". A
    #   regex assuming a bare ampersand silently missed the deals OVER-claim
    #   (2,000+ vs canon 1,745) on the first live run, which is precisely the
    #   class this probe exists to catch. Tolerate an optional backslash.
    ("deals", re.compile(r"(\d[\d,]*)\s*\+?\s*tracked\s+M\\?&?A|"
                         r"(\d[\d,]*)\s*\+?\s*M\\?&?A\s+(?:deals|transactions)",
                         re.I)),
)


def tools_rendered(spec: dict) -> int | None:
    """How many distinct tools the registry RENDERS for us, or None if blind.

    This is the only observable that reflects whether the registry's Docker
    build and MCP introspection actually SUCCEEDED. Glama withholds distribution
    for a server whose build fails — the profile page survives but the schema
    page carries no tools — so a zero here is the real "our listing is dead"
    signal that `.tools` from the JSON API only pretended to be.
    """
    try:
        status, _, body = fetch(spec["schema_page"])
    except Unreachable:
        return None
    if status != 200:
        return None
    return len(set(spec["tool_link"].findall(body)))


def claims_in(text: str) -> dict:
    """Numbers a listing asserts about us, by canon field."""
    found = {}
    for field, pat in _CLAIM_PATTERNS:
        m = pat.search(text or "")
        if m:
            raw = next((g for g in m.groups() if g), None)
            if raw:
                found[field] = _int(raw)
    return found


def compare(claimed: int, canon: int, plus: bool) -> str:
    """'ok' | 'under' | 'over'.

    ★ A trailing '+' makes a number a FLOOR, not an assertion — "17,000+" is
      true at 17,096 and this probe must not call it drift. But a floor ABOVE
      canon is still a false claim: "21,000+" says at-least-21,000 and there
      are 17,096.
    """
    if canon <= 0:
        return "ok"
    if claimed > canon:
        return "over"
    if plus:
        # A floor at or below canon is satisfied.
        return "ok" if claimed <= canon else "over"
    # An exact claim is allowed a little slack for a rounded canon figure.
    return "ok" if claimed >= canon * 0.95 else "under"


def probe(out: list[Finding] | None = None) -> list[Finding]:
    """Append this surface's findings to ``out`` — the harness-wide convention.

    ★ Takes the shared list and APPENDS, like every other probe. Shipped as a
      no-arg `probe()` returning a fresh list, which `run.collect()` called as
      `mod.probe(findings)`: TypeError on every run, caught by the runner's
      blanket except, filed as BLIND, never escalated. Two days, zero verdicts.
      The default of None keeps the direct `probe()` call usable from a REPL,
      but the runner always passes the list.
    """
    out = [] if out is None else out
    canon = read_canon()
    if not canon:
        out.append(blind(stable_key("registry", "canon"), SURFACE, SEAT_NONE,
                         "Listing drift unmeasurable — canon unreadable",
                         "could not read tool count / stats from the platform's "
                         "own live surfaces, so there is nothing to compare a "
                         "listing against",
                         basis=f"GET {_CANON_TOOLS_URL} and {_CANON_STATS_URL}"))
        return out

    for spec in LISTINGS:
        name = spec["name"]
        code, rec = get_json(spec["url"])
        if code != 200 or not isinstance(rec, dict):
            out.append(blind(
                stable_key("registry", name, "fetch"), SURFACE, SEAT_NONE,
                f"{name} listing unreadable",
                f"HTTP {code} or non-JSON body — a registry being down is not "
                f"evidence our listing is wrong",
                basis=f"GET {spec['url']}"))
            continue

        text = " ".join(str(rec.get(f) or "") for f in spec["text_fields"])
        claimed = claims_in(text)

        # 1 · did the registry's build + introspection actually deliver tools?
        n = tools_rendered(spec)
        canon_tools = canon.get("tools")
        if n is None:
            out.append(blind(
                stable_key("registry", name, "tools-listed"), SURFACE, SEAT_NONE,
                f"{name} tool inventory unreadable",
                "could not fetch or parse the rendered schema page — a page we "
                "cannot read is not evidence the listing is empty",
                basis=f"GET {spec['schema_page']}"))
        else:
            matches = canon_tools is not None and n == canon_tools
            out.append(Finding(
                key=stable_key("registry", name, "tools-listed"),
                surface=SURFACE, seat=SEAT_NONE,
                title=(f"{name} renders {n} tool(s) for a remote-capable "
                       f"server (canon {canon_tools})"),
                verdict=PASS if matches else RED,
                severity=INFO if matches else MAJOR if n else CRITICAL,
                evidence=(f"{name} schema page links {n} distinct tool detail "
                          f"pages; canon tools/list = {canon_tools}. "
                          f"Page: {spec['page']}"),
                basis=(f"GET {spec['schema_page']} -> distinct "
                       f"/tools/<name> links; canon from {_CANON_TOOLS_URL} "
                       f"in the SAME run"),
                red_when=("the registry renders a DIFFERENT tool count than we "
                          "serve. ZERO means its Docker build or MCP "
                          "introspection failed and distribution is withheld — "
                          "the listing survives but is not offered to anyone. A "
                          "non-zero mismatch means it is serving a STALE build, "
                          "so callers are shown tools we no longer have or are "
                          "denied ones we do"),
                value=n,
                remedy=("ZERO means the registry's own build failed, not that "
                        "our server is wrong: verify first with "
                        "`node server.mjs --stdio` fed initialize -> "
                        "notifications/initialized -> tools/list, which must "
                        "return canon. If that passes, it is theirs — mail "
                        "support@glama.ai for the build log, since the API "
                        "record exposes no build status field. A NON-ZERO "
                        "mismatch is usually a stale build: push a commit to "
                        "retrigger, then recheck."),
            ))

        # 2 · do the numbers in the prose match canon?
        for field, canon_val in canon.items():
            if field not in claimed:
                continue
            said = claimed[field]
            plus = bool(re.search(rf"{said:,}\s*\+|{said}\s*\+", text))
            verdict_kind = compare(said, canon_val, plus)
            is_over = verdict_kind == "over"
            out.append(Finding(
                key=stable_key("registry", name, f"claim-{field}"),
                surface=SURFACE, seat=SEAT_NONE,
                title=(f"{name} advertises {said:,} {field} "
                       f"(canon {canon_val:,})"),
                verdict=PASS if verdict_kind == "ok" else RED,
                severity=(INFO if verdict_kind == "ok"
                          else CRITICAL if is_over else MAJOR),
                evidence=(f"listing prose says {said:,}{'+' if plus else ''} "
                          f"{field}; the platform's own live canon says "
                          f"{canon_val:,} ({verdict_kind})"),
                basis=(f"GET {spec['url']} -> "
                       f".{'/'.join(spec['text_fields'])}; canon from "
                       f"{_CANON_STATS_URL} / {_CANON_TOOLS_URL} in the SAME run"),
                red_when=("the listing's number contradicts the platform's own "
                          "live canon — UNDER-claiming sells the server short, "
                          "OVER-claiming is a false public statement, which is "
                          "worse and is why it carries higher severity. A "
                          "trailing '+' is read as a floor, not an assertion"),
                value=said,
                remedy=(f"correct the copy on {spec['page']} — a MAINTAINER "
                        f"EDIT, not a code change: glama.json's entire "
                        f"published schema is `maintainers`, and the prose is "
                        f"a one-time submission blob the repo never updates. "
                        f"(Distinct from the empty-tools finding, which is a "
                        f"build failure.)"),
            ))

        # 3 · report claims we could not evaluate, rather than silently passing
        unmatched = [f for f in claimed if f not in canon]
        if unmatched:
            # ★ Two different facts share this branch, and only one is a gauge.
            #   A claim canon HAS no field for (e.g. "servers indexed") is a
            #   gauge — unwatchable by design. A claim canon DOES own that we
            #   failed to READ this run (facilities/deals missing after the
            #   retry) is a PARTIAL CANON — our instrument, not their claim —
            #   and filing it as a gauge is how the two OVER-claim CRITICALs
            #   stayed invisible from the CI seat. A partial read is closer to
            #   BLIND than to a measurement, and it is addressed to US.
            canon_owned = [f for f in unmatched if f in ("facilities", "deals")]
            if canon_owned:
                out.append(Finding(
                    key=stable_key("registry", name, "canon-partial"),
                    surface=SURFACE, seat=SEAT_NONE,
                    title=f"{name}: canon read PARTIAL — "
                          f"{'/'.join(canon_owned)} over-claim check unmeasured",
                    verdict=BLIND, severity=INFO, instrument_fault=True,
                    evidence=(f"canon returned {sorted(canon)} only; claims "
                              f"present but unjudgeable: {', '.join(canon_owned)}"),
                    basis=(f"GET {spec['url']} -> prose regex; canon from "
                           f"read_canon() (stats leg failed after 1 retry)"),
                    red_when=("n/a — BLIND, addressed to the harness: the "
                              "stats canon leg failed from this seat, so the "
                              "most severe registry checks did not run"),
                ))
            rest = [f for f in unmatched if f not in canon_owned]
            if rest:
                out.append(Finding(
                    key=stable_key("registry", name, "unmatched"),
                    surface=SURFACE, seat=SEAT_NONE,
                    title=f"{name}: {len(rest)} claim(s) with no canon to check",
                    verdict=GAUGE, severity=INFO,
                    evidence=f"claims found with no canonical counterpart: "
                             f"{', '.join(rest)}",
                    basis=f"GET {spec['url']} -> prose regex vs canon keys",
                    red_when=("n/a — a GAUGE. Reported so an unwatched claim is "
                              "visible rather than counted as verified"),
                ))
    return out
