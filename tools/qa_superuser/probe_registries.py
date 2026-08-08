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

from .finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED,
                      SEAT_NONE, Finding, blind, stable_key)
from .http import get_json

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
        "tools_field": "tools",
        "page": "https://glama.ai/mcp/servers/qa3uoznre7",
    },
)

# Canon read LIVE in the same run — never a constant in this file, or the guard
# becomes the next thing that drifts.
_CANON_TOOLS_URL = "https://dchub.cloud/.well-known/mcp.json"
_CANON_STATS_URL = "https://dchub.cloud/api/ai/query?type=stats"


def _int(s: str) -> int:
    return int(re.sub(r"[^0-9]", "", s or "0") or 0)


def read_canon() -> dict | None:
    """{tools, facilities, deals} from the platform's own live surfaces."""
    out = {}
    code, man = get_json(_CANON_TOOLS_URL)
    if code == 200 and isinstance(man, dict):
        tools = man.get("tools")
        if isinstance(tools, list) and tools:
            out["tools"] = len(tools)
        elif isinstance(man.get("tool_count"), int):
            out["tools"] = man["tool_count"]
    code, stats = get_json(_CANON_STATS_URL)
    if code == 200 and isinstance(stats, dict):
        txt = str(stats.get("suggested_response") or "")
        m = re.search(r"([\d,]+)\s+data center facilities", txt)
        if m:
            out["facilities"] = _int(m.group(1))
        m = re.search(r"([\d,]+)\s+M&A transactions", txt)
        if m:
            out["deals"] = _int(m.group(1))
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


def probe() -> list[Finding]:
    out: list[Finding] = []
    canon = read_canon()
    if not canon:
        return [blind(stable_key("registry", "canon"), SURFACE, SEAT_NONE,
                      "Listing drift unmeasurable — canon unreadable",
                      "could not read tool count / stats from the platform's "
                      "own live surfaces, so there is nothing to compare a "
                      "listing against",
                      basis=f"GET {_CANON_TOOLS_URL} and {_CANON_STATS_URL}")]

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

        # 1 · does the listing show any tools at all?
        tools = rec.get(spec["tools_field"])
        if isinstance(tools, list):
            n = len(tools)
            out.append(Finding(
                key=stable_key("registry", name, "tools-listed"),
                surface=SURFACE, seat=SEAT_NONE,
                title=f"{name} lists {n} tool(s) for a remote-capable server",
                verdict=PASS if n > 0 else RED,
                severity=INFO if n > 0 else MAJOR,
                evidence=(f"{name} record `{spec['tools_field']}` has {n} "
                          f"entries; canon tools/list = {canon.get('tools')}. "
                          f"Page: {spec['page']}"),
                basis=f"GET {spec['url']} -> .{spec['tools_field']} (length)",
                red_when=("a listing that advertises a remote-capable server "
                          "shows ZERO tools — in a tool-search directory that "
                          "is indistinguishable from having nothing to offer"),
                value=n,
                remedy=("ZERO tools usually means the registry's own "
                        "INTROSPECTION failed, not that our server is wrong. "
                        "Glama Docker-builds the repo and runs "
                        "`mcp-proxy -- node server.mjs --stdio` (supported "
                        "since r-glama 2026-06-08) — observed 2026-08-08 "
                        "failing on THEIR infra pulling debian:trixie-slim "
                        "('context deadline exceeded'), before git clone or "
                        "npm ci. Retry the build from "
                        f"{spec['page']} (or push a commit to retrigger); "
                        "only investigate our side if a build that REACHES "
                        "npm ci still yields no tools."),
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
            out.append(Finding(
                key=stable_key("registry", name, "unmatched"),
                surface=SURFACE, seat=SEAT_NONE,
                title=f"{name}: {len(unmatched)} claim(s) with no canon to check",
                verdict=GAUGE, severity=INFO,
                evidence=f"claims found with no canonical counterpart: "
                         f"{', '.join(unmatched)}",
                basis=f"GET {spec['url']} -> prose regex vs canon keys",
                red_when=("n/a — a GAUGE. Reported so an unwatched claim is "
                          "visible rather than counted as verified"),
            ))
    return out
