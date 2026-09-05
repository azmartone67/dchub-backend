"""Onboarding: qualify a candidate MCP directory before anyone submits to it.

Companion to routes/white_glove_propagation.py, which keeps EXISTING listings
honest. This module answers the question one step earlier: for a directory we
are not on yet, is it real, are we already on it, what stands between us and a
listing, and which source tag must exist BEFORE the listing is created.

★ WHAT THIS DELIBERATELY DOES NOT DO: submit. Measured 2026-09-05, every wall
that blocks the update loop also blocks submission — bot challenge (403/429 on
cursor.directory, pulsemcp), sign-in (cursor.directory, mcp.so, smith.land,
Claude Directory), payment ($39 on mcp.so), submissions paused site-wide
(pulsemcp), dead endpoint (404 on mcphive). Of 16 hosts in SEED_REGISTRIES
exactly TWO have a write path we have observed working. Writing more submitters
would build against walls that are there on purpose — the mistake #3944 just
corrected by demoting five registries that were trusted on an assumption.

★ AND IT DOES NOT AUTO-DISCOVER, because the obvious source does not work.
Measured 2026-09-05 against punkpeye/awesome-mcp-servers (1.4 MB, 338 distinct
hosts): it is a catalogue of SERVERS, not directories. Filtering by
directory-shaped names returns individual servers (mcpqueen.com, open-mcp.org,
mcp.travel.art — one occurrence each). Ranking by frequency does not rescue it
either: glama.ai tops the list at 4,236 purely because listings carry a Glama
BADGE, and the next non-code host appears 5 times. One known directory and
noise. So CANDIDATES below is hand-maintained on purpose — a short honest list
beats a long automated one built on a signal that is not there.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Reuse the two helpers that already carry mutation-tested semantics rather
# than re-deriving them: an unreadable page is UNVERIFIABLE, never "absent".
# Only classify_reachability is needed here, and it is already on main (#3896).
# Deliberately NOT importing probe_submit_endpoint: it lands with #3944, and a
# module that imports an unmerged symbol fails at import time for everyone.
from routes.white_glove_propagation import classify_reachability   # noqa: E402

MCP_ENDPOINT_BASE = "https://dchub.cloud"

# Hand-maintained. Each entry is a directory a human has actually seen.
# `kind` matters more than it looks: a CLIENT directory puts us in front of
# someone already choosing tools, an AGGREGATOR mostly puts us in a list.
CANDIDATES = [
    {"host": "mcp.so",          "kind": "aggregator",
     "listing_url": "https://mcp.so/search?q=dchub"},
    {"host": "code.visualstudio.com", "kind": "client",
     "listing_url": "https://code.visualstudio.com/mcp"},
    {"host": "claude.ai",       "kind": "client",
     "listing_url": "https://claude.ai/directory"},
]

# A known-listed server, used as a POSITIVE CONTROL. Without it, "we are not on
# this page" is indistinguishable from "this page renders nothing to a script",
# which is exactly how the white-glove lane reported five unreadable registries
# as clean for months.
CONTROL_TERMS = ("playwright", "github", "notion", "figma", "stripe")
# * MATCH A LINK, NOT THE WORD. First cut matched "dchub" anywhere in the body
#   and reported mcp.so as ALREADY LISTED - because the listing_url is
#   ?q=dchub and the page echoes the query back. Searching for ourselves always
#   "found" ourselves, which would have dropped the one aggregator we are
#   genuinely absent from straight off the worklist. A real listing is a LINK
#   to a server page; an echoed query is not.
_US_LINK = re.compile(r'(?:href|src)\s*=\s*["\'][^"\']*dchub', re.I)
# Explicit absence, which several directories state outright.
_NO_RESULTS = re.compile(r"no servers? match|no results|0 results|nothing found", re.I)


def _slug_for(host: str) -> str:
    """A source-path slug from a host: mcp.so -> mcpso."""
    base = host.lower().split("//")[-1].split("/")[0]
    base = re.sub(r"^www\.", "", base)
    base = re.sub(r"\.(com|ai|so|io|dev|org|net|tools|directory)$", "", base)
    return re.sub(r"[^a-z0-9]", "", base)


def tag_availability(host: str, fetch=None) -> dict:
    """Is /mcp/<slug> free, or already serving?

    ★ THE TAG MUST EXIST BEFORE THE LISTING DOES. MCP carries no Referer, so a
    listing created with a bare https://dchub.cloud/mcp is unattributable for
    its entire life. PulseMCP is the standing example: 624 estimated visitors
    and no way to attribute one of them. Availability is checked against
    PRODUCTION rather than a local map, because the map that matters is the one
    the deployed server actually routes."""
    slug = _slug_for(host)
    url = f"{MCP_ENDPOINT_BASE}/mcp/{slug}"
    body, status = (fetch or _default_post)(url)
    if status == 200:
        return {"slug": slug, "url": url, "state": "already_serving",
                "note": "tag exists — reuse it, do not mint a second"}
    if status in (404, 405):
        return {"slug": slug, "url": url, "state": "available",
                "note": "must be added to MCP_SOURCE_PATHS and DEPLOYED "
                        "before the listing is created"}
    return {"slug": slug, "url": url, "state": "unknown",
            "note": f"probe inconclusive (status {status})"}


def _default_post(url: str):
    import requests
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "dchub-onboarding", "version": "1"}}}
    try:
        r = requests.post(url, json=payload, timeout=15, allow_redirects=False,
                          headers={"Accept": "application/json, text/event-stream",
                                   "User-Agent": "dchub-onboarding-probe/1.0"})
        return r.text, r.status_code
    except Exception:
        return None, None


def qualify(candidate: dict, fetch=None) -> dict:
    """Reachable? a real directory? already listing us? what is the barrier?

    ★ EVERY 'no' IS GATED ON THE CONTROL. If a known-listed server does not
    appear either, the page did not render for us and the answer is UNKNOWN —
    reporting 'absent' from a page we could not read is the precise failure the
    reachability gate (#3896) exists to prevent."""
    host = candidate["host"]
    out = {"host": host, "kind": candidate.get("kind"),
           "listing_url": candidate.get("listing_url")}
    body, status = (fetch or _default_get)(candidate["listing_url"])

    unreachable = classify_reachability(body, status)
    if unreachable:
        out.update(directory="unknown", lists_us="unknown",
                   barrier=f"unreadable: {unreachable}",
                   note="needs a real browser; do NOT record as absent")
        return out

    low = (body or "").lower()
    controls = [t for t in CONTROL_TERMS if t in low]
    out["control_hits"] = controls
    if not controls:
        out.update(directory="unknown", lists_us="unknown",
                   barrier="control failed — page renders no known server",
                   note="script cannot judge this page; verify in a browser")
        return out

    out["directory"] = "yes"
    if _NO_RESULTS.search(body or ""):
        out["lists_us"] = "no"          # the page says so itself
    elif _US_LINK.search(body or ""):
        out["lists_us"] = "yes"
    else:
        out["lists_us"] = "no"
    out["barrier"] = "already listed" if out["lists_us"] == "yes" else "submission required"
    return out


def _default_get(url: str):
    import requests
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent":
                                  "Mozilla/5.0 (compatible; dchub-onboarding/1.0)"})
        return r.text, r.status_code
    except Exception:
        return None, None


def build_worklist(candidates=None, fetch_get=None, fetch_post=None) -> list[dict]:
    """One row per candidate, client directories first.

    Client directories outrank aggregators deliberately: measured 2026-09-05 we
    sit #11,607 of ~21,970 on PulseMCP while identifiable MCP clients are 3.2%
    of /mcp traffic — another aggregator row is not what moves adoption."""
    rows = []
    for c in (candidates if candidates is not None else CANDIDATES):
        row = qualify(c, fetch=fetch_get)
        row["tag"] = tag_availability(c["host"], fetch=fetch_post)
        rows.append(row)
    rows.sort(key=lambda r: (r.get("kind") != "client", r.get("lists_us") == "yes"))
    return rows
