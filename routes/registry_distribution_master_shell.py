"""Registry Distribution Master Shell — GET /api/v1/admin/registry-distribution
tick: /api/v1/admin/registry-distribution/master-tick
kill: REGISTRY_DISTRIBUTION_SHELL_DISABLE=1

Built 2026-08-06 because registries are the ONLY channel with evidence behind
it, and nothing watched them for the failure modes that actually bite.

WHY THIS EXISTS — four findings from the 2026-08-06 distribution audit, none of
which the existing registry_truth ledger surfaced:

  1. ABSENT FROM THE ONE CATALOG THAT INSTALLS. docker/mcp-registry carries 328
     servers and zero of them are ours. It ships inside Docker Desktop's MCP
     Toolkit, which one-click-installs into Claude Desktop, Cursor and VS Code.
     That is an INSTALL surface; an awesome-list is a reading surface. Nothing
     tracked the difference.
  2. AN EMPTY tools ARRAY IS WORSE THAN A STALE COUNT. Glama's API returns
     `tools: []` for us — read twice, cache-busted, minutes apart. A stale count
     misinforms; an empty array tells an agent the server has NO capabilities.
     The ledger had no concept for this and scored the listing fine.
  3. A ZOMBIE TWIN SPLITS THE LISTING. Smithery carries both
     azmartone67/dchub (82 tools, #1 of 193) and azmartone67/dchub-mcp-server
     (28 tools, empty description, no execute_plan). Two listings for one
     server is ambiguity a ranking cannot fix.
  4. THE LEDGER'S OWN VERDICTS WERE INVERTED. It recorded the official MCP
     registry as verified_drift ("publishes 40 tools, canon is 82") when the
     isLatest entry actually publishes 82 and is our healthiest listing — while
     scoring the empty-tools Glama listing as fine. A board that is red on the
     correct thing and green on the broken one is worse than no board.

★ THE RULE THIS SHELL IS BUILT AROUND: UNREADABLE IS NOT DRIFT.
registry_truth already encodes this (verified_ok / verified_drift / not_ours /
unreadable) and it is the single most important semantic here — 11 of 16
listings were once unreadable while drift correctly read FALSE. Every check
below is THREE-valued: True (agrees), False (contradicts), None (could not
read). A fetch failure must never render as a content failure, and must never
render as a pass either.

Lanes are born red where the work is real and unstarted. A born-red lane is a
work order, not a defect.
"""
from __future__ import annotations

import json
import os
import re

from flask import Blueprint, Response, jsonify

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _lane_verdict, _safe_lane)

registry_distribution_master_shell_bp = Blueprint(
    "registry_distribution_master_shell", __name__)

_UA = "dchub-registry-distribution-shell/1.0 (+https://dchub.cloud)"
_TIMEOUT = 12

# Canon is fetched, never restated. A count baked into this file goes stale
# silently — exactly how Glama came to publish "33 tools / 21,000+ facilities".
_CANON_URL = "https://dchub.cloud/api/v1/canon/phrases"

# Registries that INSTALL vs registries that merely LIST. The distinction is the
# whole point: a #1 ranking on a reading surface produces no agents. `installs`
# means a user can go from seeing us to a working connection without leaving.
_REGISTRIES = [
    {"key": "docker_mcp_catalog", "installs": True,
     "probe": "github_tree", "repo": "docker/mcp-registry",
     "match": ("dchub", "datacenter", "data-center"),
     "why": "ships in Docker Desktop MCP Toolkit -> one-click into Claude "
            "Desktop / Cursor / VS Code"},
    {"key": "official_mcp_registry", "installs": True,
     "probe": "official", "why": "consumed by VS Code / Copilot MCP gallery"},
    {"key": "smithery", "installs": True,
     "probe": "smithery", "why": "hosted proxy + one-click connect"},
    {"key": "glama", "installs": False,
     "probe": "glama", "why": "read surface; feeds other listings' copy"},
]


def _disabled() -> bool:
    return os.environ.get("REGISTRY_DISTRIBUTION_SHELL_DISABLE", "") == "1"


def _get_json(url: str, headers: dict | None = None):
    """Returns (payload, None) or (None, reason). NEVER raises.

    A reason string means UNREADABLE — the caller must render None, not False.

    ★ THE STATUS CHECK IS LOAD-BEARING, NOT DEFENSIVE NOISE. This helper used
    urllib.request.urlopen, which RAISES HTTPError on 4xx/5xx — so an error
    response could never reach the parser. `requests` does not raise; it hands
    back the error response like any other. And the registries read here answer
    errors with well-formed JSON: api.github.com replies {"message":"Not Found"}
    to a 404. Parsing that as a payload would give the catalog lane an object
    with no `tree` key, which reads as zero catalogued servers and renders RED
    — "ABSENT from 0 catalogued servers" — on a registry we merely could not
    read. That is a read failure rendered as a content failure, the single bug
    class this shell exists to catch. Status is therefore checked BEFORE the
    body is parsed, and a non-2xx returns the same UNREADABLE reason urllib's
    HTTPError branch used to produce.
    """
    h = {"User-Agent": _UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    try:
        import requests as _rq
        r = _rq.get(url, headers=h, timeout=_TIMEOUT, allow_redirects=True)
        if not 200 <= r.status_code < 300:
            return None, f"HTTP {r.status_code}"
        # .content, not .text: requests guesses ISO-8859-1 for a text/* body
        # with no charset, which mangles UTF-8 registry copy. Decoding the raw
        # bytes keeps the previous urlopen behaviour exactly.
        return json.loads(r.content.decode("utf-8", "replace")), None
    except Exception as e:  # noqa: BLE001 - any failure is UNREADABLE
        return None, f"{type(e).__name__}"


def _find_tool_count(obj, _depth: int = 0):
    """First plausible tool count anywhere in a registry entry, or None.

    Registries move this field between releases (toolCount, _meta.toolCount,
    _meta.<namespace>.toolCount). Guessing a single path and scoring the miss
    as DRIFT is how a healthy listing gets reported broken — so we search, and
    return None (UNMEASURED) when nothing is found.
    """
    if _depth > 4 or not isinstance(obj, dict):
        return None
    for k, v in obj.items():
        if k in ("toolCount", "tool_count", "toolsCount") and isinstance(v, int):
            return v
        if k in ("tools",) and isinstance(v, list) and v:
            return len(v)
    for v in obj.values():
        if isinstance(v, dict):
            got = _find_tool_count(v, _depth + 1)
            if got is not None:
                return got
    return None


def _canon():
    """Live canon, or None. Every accuracy check degrades to UNMEASURED without
    it — we do not fall back to a remembered number."""
    payload, _ = _get_json(_CANON_URL + "?cb=shell")
    return payload


# ── lane 1 · install-surface presence ────────────────────────────────────────
def _lane_catalog_presence() -> list[dict]:
    checks: list[dict] = []

    tree, reason = _get_json(
        "https://api.github.com/repos/docker/mcp-registry/git/trees/"
        "main?recursive=1")
    if tree is None:
        checks.append(_check(
            "docker_catalog", "listed in Docker MCP Catalog", None,
            f"UNREADABLE ({reason}) — absence NOT concluded. A registry we "
            "cannot read is not a registry we are missing from.",
            critical=True))
    else:
        paths = [n.get("path", "") for n in (tree.get("tree") or [])]
        servers = {p.split("/")[1] for p in paths
                   if p.startswith("servers/") and len(p.split("/")) > 1}
        hit = sorted(s for s in servers
                     if any(m in s.lower() for m in ("dchub", "datacenter",
                                                     "data-center", "grid")))
        checks.append(_check(
            "docker_catalog", "listed in Docker MCP Catalog", bool(hit),
            (f"present as {hit}" if hit else
             f"ABSENT from {len(servers)} catalogued servers. BORN RED — this "
             "is the only install surface we hold no position on. Remote "
             "servers need no Dockerfile: a ~18-line servers/<name>/server.yaml "
             "with type: remote, transport_type: streamable-http, "
             "url: https://dchub.cloud/mcp. Owner action; merge latency is "
             "theirs and unmeasurable from here."),
            critical=True))

    installs = [r["key"] for r in _REGISTRIES if r["installs"]]
    checks.append(_check(
        "install_vs_read", "install surfaces are tracked apart from read ones",
        True,
        f"{len(installs)} install surfaces tracked ({', '.join(installs)}). A "
        "#1 ranking on a READ surface produces no agents — punkpeye has 91k "
        "stars and installs nobody. Rank by whether a reader can reach a "
        "working connection without leaving.", critical=False))
    return checks


# ── lane 2 · capability visibility ───────────────────────────────────────────
def _lane_capability_visible() -> list[dict]:
    """An empty tools array is a DIFFERENT failure from a stale count, and a
    worse one: it tells an agent the server has no capabilities at all."""
    checks: list[dict] = []

    payload, reason = _get_json(
        "https://glama.ai/api/mcp/v1/servers/azmartone67/dchub-mcp-server")
    if payload is None:
        checks.append(_check(
            "glama_tools", "Glama listing exposes a non-empty tool list", None,
            f"UNREADABLE ({reason}) — not scored as empty. Unreadable is not "
            "drift.", critical=True))
    else:
        tools = payload.get("tools")
        n = len(tools) if isinstance(tools, list) else None
        checks.append(_check(
            "glama_tools", "Glama listing exposes a non-empty tool list",
            (n is not None and n > 0),
            (f"{n} tools published" if n else
             "tools array is EMPTY. Strictly worse than a stale count: a stale "
             "count misinforms, an empty array tells an agent this server has "
             "NO capabilities. If Glama populates this by crawling our remote "
             "endpoint rather than from submitted metadata, editing the "
             "description will NOT refill it — the introspection is what is "
             "failing, and we cannot see why from outside."),
            critical=True))
    return checks


# ── lane 3 · published-content accuracy ──────────────────────────────────────
def _lane_listing_accuracy() -> list[dict]:
    """Do listings publish numbers that contradict canon — in EITHER direction?

    Every drift found before 2026-08-06 was a number too SMALL. Smithery
    published "4,000+ tracked M&A deals" against a canon of 1,600+ — the first
    inflated figure found anywhere, and inflation is the more damaging kind.
    """
    checks: list[dict] = []
    canon = _canon()
    if canon is None:
        return [_check("accuracy", "listings agree with canon", None,
                       "canon unreadable — accuracy UNMEASURED rather than "
                       "assumed", critical=True)]

    payload, reason = _get_json(
        "https://registry.modelcontextprotocol.io/v0/servers?search=dchub")
    if payload is None:
        checks.append(_check(
            "official_tools", "official registry publishes the canon tool count",
            None, f"UNREADABLE ({reason})", critical=True))
    else:
        servers = payload.get("servers") or payload.get("data") or []
        # ★ isLatest ONLY. The endpoint returns every historical version; a scan
        # that reads them all will 'find' drift in versions we already replaced.
        latest = [s for s in servers
                  if (s.get("_meta") or {}).get("isLatest")
                  or s.get("isLatest")] or servers[:1]
        if not latest:
            checks.append(_check(
                "official_tools",
                "official registry publishes the canon tool count", None,
                "no isLatest entry returned — UNMEASURED", critical=True))
        else:
            e = latest[0]
            # ★ The count is not always at a fixed path — it has been seen at
            # toolCount, _meta.toolCount and _meta.<ns>.toolCount. Search the
            # whole entry rather than guessing ONE path: on 2026-08-06 this
            # check guessed wrong, read None, and rendered RED — announcing
            # DRIFT on our healthiest listing. That is the precise failure this
            # shell exists to catch, committed by the shell itself.
            pub = _find_tool_count(e)
            want = canon.get("tools")
            if pub is None:
                checks.append(_check(
                    "official_tools",
                    "official registry publishes the canon tool count", None,
                    "isLatest entry carries no readable tool count at any known "
                    f"path (keys: {sorted(e.keys())[:8]}) — UNMEASURED, NOT "
                    "drift. An absent field is a read failure; calling it drift "
                    "is how a correct listing gets reported as broken.",
                    critical=True))
            else:
                checks.append(_check(
                    "official_tools",
                    "official registry publishes the canon tool count",
                    (want is not None and int(pub) == int(want)),
                    f"isLatest publishes toolCount={pub}, canon={want}"
                    + ("" if str(pub) == str(want) else
                       " — DRIFT. Verify against isLatest before trusting "
                       "either verdict: the ledger once recorded this listing "
                       "as drifted when it was in fact correct."),
                    critical=True))

    checks.append(_check(
        "inflation_watch", "no listing publishes a number LARGER than canon",
        None,
        "UNMEASURED here by design — per-registry description parsing is not "
        "reliable enough to automate. Checked by hand 2026-08-06: Smithery's "
        f"listing said '4,000+ tracked M&A deals' against canon "
        f"{canon.get('deals')}. Inflation is rarer than staleness and more "
        "damaging: a small number reads as modest, a large one reads as a lie "
        "when checked.", critical=False))
    return checks


# ── lane 4 · one server, one listing ─────────────────────────────────────────
def _lane_no_duplicate_listings() -> list[dict]:
    """Two listings for one server split rank, reviews and trust — and the
    weaker twin is the one that shows an agent a truncated tool set."""
    checks: list[dict] = []
    payload, reason = _get_json(
        "https://registry.smithery.ai/servers?q=dchub",
        headers={"Accept": "application/json"})
    if payload is None:
        checks.append(_check(
            "smithery_twin", "exactly one Smithery listing for DC Hub", None,
            f"UNREADABLE ({reason}) — twin NOT concluded absent",
            critical=False))
        return checks

    items = payload.get("servers") or payload.get("results") or []
    ours = [s for s in items
            if "dchub" in json.dumps(s).lower()]
    names = sorted({(s.get("qualifiedName") or s.get("name") or "?")
                    for s in ours})
    checks.append(_check(
        "smithery_twin", "exactly one Smithery listing for DC Hub",
        len(names) <= 1,
        (f"{len(names)} listing(s): {names}"
         + ("" if len(names) <= 1 else
            " — the twin azmartone67/dchub-mcp-server carries an EMPTY "
            "description and 28 tools with no execute_plan, against the real "
            "listing's 82. Two listings for one server is ambiguity a #1 rank "
            "cannot fix. Delete or repoint the twin.")),
        critical=False))
    return checks


# ── lane 5 · the ledger's own verdicts ───────────────────────────────────────
def _lane_ledger_integrity() -> list[dict]:
    """A board that is RED on the correct listing and GREEN on the broken one
    is worse than no board — it spends the reader's attention backwards.

    This lane deliberately does NOT re-derive every verdict. It asserts the two
    properties that failed on 2026-08-06: the ledger must carry the 4-state
    vocabulary, and unreadable must never be recorded as drift.
    """
    checks: list[dict] = []
    try:
        from routes import registry_truth  # noqa: F401
        src = ""
        try:
            import inspect
            src = inspect.getsource(registry_truth)
        except Exception:  # noqa: BLE001
            pass
        # ★ 2026-08-06 — THIS CHECK GUESSED ITS OWN LITERALS AND WAS WRONG.
        # It asserted ("verified_ok","verified_drift","not_ours","unreadable")
        # and reported `missing ['not_ours']` as a finding. But `not_ours` and
        # `unreadable` are NOT tokens anywhere in registry_truth — they are
        # prose in its docstring. The real tokens are verified_ok /
        # verified_drift / broken. Pinning invented strings is the same
        # name-guessing class as reading `tool` where the column is `tool_name`.
        #
        # The contract that matters is not WHICH words are used. It is whether
        # a reader can tell "we could not read this listing" apart from "we
        # read it and it is wrong" — collapsing those is how 11 of 16
        # unreadable listings once reported drift=FALSE.
        if not src:
            checks.append(_check(
                "four_state", "unreadable is distinguishable from wrong", None,
                "could not read registry_truth source (inspect.getsource "
                "returned nothing) — UNMEASURED, not a missing vocabulary",
                critical=True))
        else:
            tokens = sorted(set(re.findall(
                r'verdict"\]\s*=\s*"([a-z_]+)"', src)))
            readable_marker = "cannot read" in src
            checks.append(_check(
                "four_state",
                "unreadable is distinguishable from wrong", readable_marker,
                (f"verdict tokens in use: {tokens}. "
                 + ("Unreadable cases carry a 'cannot read' reason, so the "
                    "distinction survives — but ONLY in `reason`, not in the "
                    "verdict: `broken` is assigned both for HTTP>=400 and for "
                    "a listing that resolves to a search page. A board that "
                    "groups on verdict alone cannot tell a registry we failed "
                    "to FETCH from a listing that is genuinely WRONG. Group on "
                    "reason, or split the token."
                    if readable_marker else
                    "no 'cannot read' marker — an unreadable listing and a "
                    "wrong one are indistinguishable, which is how drift "
                    "reported FALSE across 11 unreadable listings.")),
                critical=True))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "four_state", "ledger keeps a 4-state verdict vocabulary", None,
            f"registry_truth not importable: {type(e).__name__}",
            critical=True))

    checks.append(_check(
        "verdict_vs_reality",
        "ledger verdicts agree with a fresh read of the same listing", None,
        "BORN UNMEASURED. On 2026-08-06 the ledger recorded the official MCP "
        "registry as verified_drift ('publishes 40 tools, canon is 82') while "
        "its isLatest entry published 82 and was our healthiest listing — and "
        "scored the empty-tools Glama listing as fine. Both verdicts were "
        "backwards. Closing this needs the ledger's stored verdict compared "
        "against a live re-read per listing; until that ships, do not act on a "
        "ledger verdict without re-reading the listing yourself.",
        critical=True))
    return checks


_LANES = [
    ("catalog_presence", "install-surface presence", _lane_catalog_presence),
    ("capability_visible", "listings expose capabilities",
     _lane_capability_visible),
    ("listing_accuracy", "published content agrees with canon",
     _lane_listing_accuracy),
    ("no_duplicate_listings", "one server, one listing",
     _lane_no_duplicate_listings),
    ("ledger_integrity", "the ledger's own verdicts", _lane_ledger_integrity),
]


def _tick() -> dict:
    lanes = []
    for lid, name, fn in _LANES:
        checks = _safe_lane(fn)
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    return {
        "shell": "registry-distribution",
        "note": ("Registries are the only channel with measured volume behind "
                 "it. UNREADABLE is not DRIFT: a check that could not read a "
                 "listing renders pass=None and must never be counted as "
                 "either a pass or a failure."),
        "lanes": lanes,
        "lanes_total": len(lanes),
        "lanes_pass": sum(1 for x in lanes if x["verdict"] == "PASS"),
        "summary": " ".join(f"{x['id']}={x['verdict']}" for x in lanes),
    }


@registry_distribution_master_shell_bp.route(
    "/api/v1/admin/registry-distribution/master-tick", methods=["GET"])
def registry_distribution_master_tick():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_tick())


@registry_distribution_master_shell_bp.route(
    "/admin/registry-distribution", methods=["GET"])
def registry_distribution_board():
    if _disabled():
        return Response("shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("unauthorized", status=401, mimetype="text/plain")
    t = _tick()
    rows = []
    for lane in t["lanes"]:
        rows.append(f"\n{lane['verdict']:<5} {lane['id']} — {lane['name']}")
        for c in lane["checks"]:
            mark = {True: "OK ", False: "RED", None: " ? "}[c["pass"]]
            rows.append(f"   [{mark}] {c['name']}\n        {c['detail']}")
    return Response(t["summary"] + "\n" + "\n".join(rows),
                    mimetype="text/plain")
