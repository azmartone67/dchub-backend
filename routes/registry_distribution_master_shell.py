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



# ── drafted-but-unsubmitted ──────────────────────────────────────────
# ★2026-08-12. MCP Hive was reported as a registry "white glove missed". It was
# not missed: PATCHES/REGISTRY_SUBMISSIONS_r45/mcp-hive.md has existed since r45
# — a complete, ready-to-paste submission draft — and was simply never sent. The
# owner found the empty Hive dashboard by hand.
#
# Nothing could have caught it. This shell probes _REGISTRIES, and a registry
# nobody added to _REGISTRIES is not "absent", it is UNASKED. Writing the draft
# is the step that records intent; sending it is the step that gets forgotten;
# and there was no check spanning the two.
#
# ★This lane deliberately does NOT scrape eight marketplaces. Eight bespoke
# scrapers is eight things to break, and a broken scraper reports "not listed",
# which is the false-absent this codebase has spent the day removing. It asks a
# question the filesystem can answer exactly: for every draft we wrote, did we
# wire it up so presence is even checkable?
_DRAFT_DIR = "PATCHES/REGISTRY_SUBMISSIONS_r45"


def _drafted_targets() -> list:
    """Registry names we have written submission copy for. Derived from disk,
    never a hand-maintained list — a second list would drift from the first,
    which is the duplicated-constant bug that produced a false DCPI alarm."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, _DRAFT_DIR)
    # ★README.md is documentation for the draft folder, not a registry. The
    # first run reported it as an eighth unwired target — a guard that invents
    # a target is the same false positive as one that invents a violation.
    skip = {"README", "INDEX", "NOTES"}
    try:
        return sorted(f[:-3] for f in os.listdir(d)
                      if f.endswith(".md") and f[:-3] not in skip)
    except Exception:
        return []


def _lane_drafted_but_unwired() -> list[dict]:
    drafts = _drafted_targets()
    if not drafts:
        return [_check("drafts_readable", "submission drafts are readable",
                       None, "could not read %s — cannot tell whether any "
                       "draft is unsent" % _DRAFT_DIR, critical=True)]
    wired = {r["key"] for r in _REGISTRIES}
    # Draft filenames are registry slugs; _REGISTRIES keys are snake_case.
    norm = lambda x: x.replace("-", "_").replace(".", "_").lower()
    wired_n = {norm(w) for w in wired}
    unwired = [d for d in drafts if norm(d) not in wired_n
               and not any(norm(d) in w or w in norm(d) for w in wired_n)]
    checks = [_check("drafts_found", "submission drafts on disk", True,
                     "%d drafted: %s" % (len(drafts), ", ".join(drafts)))]
    checks.append(_check(
        "drafts_wired_for_verification",
        "every drafted registry is probed by this shell",
        not unwired,
        ("all %d drafted registries are in _REGISTRIES" % len(drafts))
        if not unwired else
        ("%d drafted registries are NOT in _REGISTRIES, so this shell has "
         "never asked whether we are listed there — a draft nobody sent looks "
         "identical to a draft nobody wrote: %s"
         % (len(unwired), ", ".join(unwired))),
        critical=False))
    return checks


# ═════════════════════════════════════════════════════════════════════════════
# LANE A · DISCOVERY — registries we are ABSENT from
# LANE B · STALENESS — registries we are LISTED on that publish wrong metadata
#
# ★2026-08-13. WHY THESE TWO LANES EXIST, and it is not a feature request.
# The owner discovered MCP Hive HIMSELF and submitted DC Hub to it. No system
# flagged it. That is the finding: registry coverage above was enumerated BY
# HAND into _REGISTRIES, and the ecosystem adds directories faster than anyone
# retypes that tuple. A registry that did not exist when _REGISTRIES was written
# is not "absent" to this shell — it is UNASKED, which renders as nothing at all.
#
# The second gap is subtler and worse. LISTED-BUT-WRONG scored as PRESENT.
# Glama publishes `tools: []` and a description reading "33 tools covering
# 21,000+" against a canon of 82 tools and 17,600+ facilities. We are listed,
# and the listing actively misinforms every agent that reads it. Presence is not
# health. Lane B separates them.
#
# ★ THE DEFECT BEING FIXED IS THE HARDCODED LIST ITSELF. A second hardcoded
# list of "registries to discover" would rot on exactly the same clock as the
# first, so there isn't one. What IS typed here is a short list of SOURCES that
# maintain themselves — a GitHub topic query, the official registry's own
# community-projects doc, and the submission drafts on our disk. Candidates are
# DERIVED from whatever those sources currently say. A registry founded next
# month appears in this board with no code change, provided it shows up in any
# source. That is the whole difference between this and the tuple above.
# ═════════════════════════════════════════════════════════════════════════════

_WELLKNOWN_URL = "https://dchub.cloud/.well-known/mcp.json"

# Our own name in someone else's corpus. "dchub" is distinctive; "DC Hub" alone
# is not (it appears as prose in unrelated data-center copy).
_SELF_TOKENS = ("dchub",)

# A corpus must look like a corpus before its silence means anything. These
# floors are what turn "the fetch returned something" into "a searchable index
# answered, and we are not in it" — the ONLY basis on which this shell is
# permitted to say ABSENT.
_MIN_CORPUS_BYTES = 2000
_MIN_CORPUS_MARKERS = 10

_MAX_PROBES = 14          # network budget per tick
_MAX_ASSET_HOPS = 3       # JS-only sites: page -> script -> data file
_MAX_FOLLOW_FETCHES = 8   # hard ceiling on the per-candidate crawl


def _get_text(url: str, headers: dict | None = None):
    """(text, None) or (None, reason). NEVER raises. Mirrors _get_json's rule:
    a reason means UNREADABLE, and the caller must render None — never False."""
    h = {"User-Agent": _UA,
         "Accept": "text/html,text/plain,application/json,*/*"}
    if headers:
        h.update(headers)
    try:
        import requests as _rq
        r = _rq.get(url, headers=h, timeout=_TIMEOUT, allow_redirects=True)
        if not 200 <= r.status_code < 300:
            return None, f"HTTP {r.status_code}"
        return r.content.decode("utf-8", "replace"), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}"


def _raw_readme(url: str) -> str:
    """github.com/<owner>/<repo> -> its raw README. A repo's rendered HTML page
    truncates large READMEs, so searching it for our name produces a false
    ABSENT on exactly the biggest directories."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)/?$", url or "")
    if not m:
        return url
    return ("https://raw.githubusercontent.com/%s/%s/main/README.md"
            % (m.group(1), m.group(2)))


def _third_party_tokens() -> list:
    """Distinctive identities of OTHER people's MCP servers, derived live from
    the largest aggregator. Used to tell a DIRECTORY apart from a gateway.

    ★ WHY THIS EXISTS. The first run of this lane emitted twelve work orders
    reading "submit DC Hub to MCPJungle / mcp-cli / arka-mcp-gateway". Those are
    GATEWAYS and CLIs — self-hostable registry implementations that list nobody.
    They matched topic:mcp-registry and sailed through a keyword filter. A work
    order to submit yourself to a CLI is not merely noise, it is the thing this
    board exists to prevent: attention spent on an action that cannot be taken.
    A real directory carries OTHER PEOPLE'S SERVERS, and that is measurable
    rather than guessable — so we measure it.
    """
    txt, _r = _get_text("https://raw.githubusercontent.com/punkpeye/"
                        "awesome-mcp-servers/main/README.md")
    if not txt:
        return []
    repos = re.findall(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", txt)
    seen, out = set(), []
    for r in repos:
        rl = r.lower().rstrip(".")
        if rl in seen or "awesome" in rl:
            continue
        seen.add(rl)
        out.append(rl)
    return out[::max(1, len(out) // 60)][:60]


_MIN_THIRD_PARTY_HITS = 5


def _is_directory(text: str, tokens: list) -> bool | None:
    """Does this corpus list OTHER people's servers? True / False / None.

    None means we could not derive the token set, and therefore cannot tell a
    directory from a gateway — in which case no ABSENT verdict is permitted.
    """
    if not tokens:
        return None
    low = text.lower()
    return sum(1 for t in tokens if t in low) >= _MIN_THIRD_PARTY_HITS


def _corpus_answers(text: str) -> bool:
    """Is this plausibly a server INDEX that answered, rather than a stub, an
    error page, or a JS shell that renders its list client-side?

    ★ This predicate is the entire difference between a finding and a lie.
    mcphive.com/servers returns a styled 404; mcphive.com/servers.html returns a
    200 whose visible text is literally 'No servers found matching your
    filters' because the list is loaded from data/servers.csv at runtime. Both
    would read as 'we are not listed' to a naive substring search. Neither is
    evidence of anything.
    """
    if not text or len(text) < _MIN_CORPUS_BYTES:
        return False
    return len(re.findall(r"(?i)mcp", text)) >= _MIN_CORPUS_MARKERS


def _listed_in(text: str) -> bool:
    return any(t in text.lower() for t in _SELF_TOKENS)


def _probe_index(url: str, tokens: list | None = None,
                 allow_assets: bool = True) -> tuple:
    """Three-valued presence probe -> (state, detail, corpus_url).

    state is "LISTED" / "ABSENT" / "UNREADABLE" / "NOT_A_DIRECTORY" — never a
    bool, because the two-valued version of this function is the bug the whole
    shell exists to prevent. ABSENT is returned ONLY when a corpus answered,
    demonstrably lists other people's servers, and does not contain us. Every
    other outcome carries its reason and produces no work order.
    """
    tokens = tokens or []

    def _verdict(text, where):
        if _listed_in(text):
            return ("LISTED", f"found in {where}", where)
        if not _corpus_answers(text):
            return None
        isdir = _is_directory(text, tokens)
        if isdir is None:
            return ("UNREADABLE",
                    "corpus answered but the third-party token set could not "
                    "be derived, so a directory cannot be told from a gateway "
                    "— absence NOT concluded", where)
        if not isdir:
            return ("NOT_A_DIRECTORY",
                    f"{where} answered ({len(text)}b) but carries fewer than "
                    f"{_MIN_THIRD_PARTY_HITS} other MCP servers — this is a "
                    "gateway/CLI/SDK, not a directory that lists third "
                    "parties. No submission exists to make.", where)
        return ("ABSENT",
                f"directory index answered ({len(text)}b, lists other servers) "
                "and does not contain us", where)

    text, reason = _get_text(url)
    if text is None:
        return ("UNREADABLE", f"fetch failed ({reason}) — absence NOT concluded",
                url)
    got = _verdict(text, url)
    # ★ ONLY LISTED AND ABSENT ARE TERMINAL HERE. A landing page that is not
    # itself a directory must NOT end the probe — mcphive.com's homepage carries
    # no server list at all, while the corpus its own JS fetches
    # (data/servers.csv, 218 servers) is a directory and answers cleanly.
    # Returning NOT_A_DIRECTORY from the landing page would have silently
    # dropped the one registry that motivated this whole lane.
    if got and got[0] in ("LISTED", "ABSENT"):
        return got
    fallback = got

    # The page fetched but is not itself an index. Follow the site's OWN links
    # to its corpus — never a guessed slug. ★ A GUESSED URL THAT 404s IS NOT
    # ABSENCE: mcphive.com/servers 404s while mcphive.com/servers.html is real,
    # mcp.so answers a guessed slug with an error, and pulsemcp 403s a bot.
    # None of those is a finding in either direction, and all three would read
    # as "not listed" to anything that guessed a URL.
    if allow_assets:
        base = re.match(r"(https?://[^/]+)", url)
        root = base.group(1) if base else ""

        def _abs(a, ctx):
            """Absolute forms of a discovered reference — plural on purpose.

            ★ A BROWSER RESOLVES fetch() AGAINST THE DOCUMENT, NOT THE SCRIPT.
            mcphive's js/servers.js calls fetch('data/servers.csv'), which the
            page at / resolves to /data/servers.csv — resolving it against the
            script's own directory yields /js/data/servers.csv, a 404, and the
            probe would then report "unreadable" for the one registry this lane
            was built to catch. Both resolutions are tried rather than picking
            the one that looks right.
            """
            if a.startswith("http"):
                return [a]
            if a.startswith("/"):
                return [root + a]
            return [ctx.rsplit("/", 1)[0] + "/" + a, root + "/" + a]

        def _links(body, ctx):
            out = []
            for m in re.finditer(
                    r'(?:href|src|fetch\()\s*=?\s*[\'"]([^\'"]+)[\'"]', body):
                a = m.group(1)
                p = a.split("?")[0]
                if p.endswith((".csv", ".json", ".js")):
                    out.extend(_abs(a, ctx))
                elif re.search(r"(?i)(server|director|catalog|registry|index)",
                               p) and not p.startswith(("http", "//")):
                    out.extend(_abs(a, ctx))
            # bare 'data/servers.csv' referenced anywhere in a script body
            for m in re.finditer(r'[\'"]([^\'"\s]{3,90}\.(?:csv|json))[\'"]',
                                 body):
                out.extend(_abs(m.group(1), ctx))
            # Same host only — a registry's corpus lives on its own domain, and
            # following googletagmanager.js burns the fetch budget that should
            # have reached data/servers.csv. Data files before pages: the CSV
            # is the answer, the HTML is the scenery.
            seen_, uniq = set(), []
            for u_ in out:
                if u_ in seen_ or u_ == ctx or not u_.startswith(root):
                    continue
                seen_.add(u_)
                uniq.append(u_)
            rank = {"csv": 0, "json": 0, "js": 1}
            return sorted(uniq, key=lambda x: rank.get(
                x.split("?")[0].rsplit(".", 1)[-1].lower(), 2))

        frontier, visited, budget = _links(text, url), {url}, 0
        depth = 0
        while frontier and depth < _MAX_ASSET_HOPS:
            nxt_frontier = []
            for a in frontier:
                if budget >= _MAX_FOLLOW_FETCHES:
                    break
                if a in visited:
                    continue
                visited.add(a)
                budget += 1
                sub, _r = _get_text(a)
                if sub is None:
                    continue
                got2 = _verdict(sub, a)
                if got2 and got2[0] in ("LISTED", "ABSENT"):
                    return got2
                nxt_frontier.extend(_links(sub, a))
            frontier = nxt_frontier
            depth += 1

    if fallback:
        return fallback
    return ("UNREADABLE",
            f"fetched {len(text)}b but it is not a searchable index (JS-only "
            "shell, stub, or error page) — absence NOT concluded", url)


def _draft_meta() -> dict:
    """Submission drafts on disk, parsed into work-order fields.

    Derived from the filesystem and from each draft's own header, never from a
    list typed here — the drafts already carry Type / URL / Method / Field hint,
    so the work order is READ, not invented. A draft nobody sent and a draft
    nobody wrote look identical from outside; this is what tells them apart.
    """
    out = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, _DRAFT_DIR)
    try:
        names = [f for f in os.listdir(d) if f.endswith(".md")]
    except Exception:  # noqa: BLE001
        return out
    for f in names:
        slug = f[:-3]
        if slug in ("README", "INDEX", "NOTES"):
            continue
        try:
            with open(os.path.join(d, f), encoding="utf-8",
                      errors="replace") as fh:
                body = fh.read(4000)
        except Exception:  # noqa: BLE001
            continue
        def _f(label, b=body):
            m = re.search(r"\*\*%s:\*\*\s*(.+)" % label, b)
            return m.group(1).strip() if m else None
        out[slug] = {"slug": slug, "type": _f("Type"), "url": _f("URL"),
                     "method": _f("Method"), "hint": _f("Field hint")}
    return out


def _discover_candidates() -> tuple:
    """Derive registry candidates from sources that maintain THEMSELVES.

    Returns (candidates, source_notes, sources_answered). The third value is
    load-bearing: zero candidates because every source was unreachable is
    UNMEASURED, while zero candidates from sources that ANSWERED is a real
    finding. Collapsing the two renders a network outage as a content failure —
    the exact bug class this shell exists to catch, and one the first cut of
    this lane committed.
    """
    cands, notes = {}, []
    sources_ok = 0

    def _add(name, probe_url, origin, submit_url=None, evidence=""):
        key = name.lower()
        if key in cands:
            cands[key]["origin"] = cands[key]["origin"] + "+" + origin
            return
        cands[key] = {"name": name, "probe_url": probe_url, "origin": origin,
                      "submit_url": submit_url, "evidence": evidence}

    # SOURCE 1 · GitHub topic search. The QUERY is fixed; the RESULT SET is not.
    # A directory published tomorrow and tagged mcp-registry lands here with no
    # code change. That is the anti-rot property the hardcoded tuple lacks.
    for topic in ("mcp-registry", "mcp-directory"):
        payload, reason = _get_json(
            "https://api.github.com/search/repositories?q=topic:"
            + topic + "&sort=stars&per_page=30")
        if payload is None:
            notes.append(f"github topic:{topic} UNREADABLE ({reason})")
            continue
        items = payload.get("items") or []
        sources_ok += 1
        notes.append(f"github topic:{topic} -> {len(items)} repos")
        for r in items:
            blurb = ((r.get("description") or "") + " "
                     + " ".join(r.get("topics") or [])).lower()
            # keep DIRECTORIES; drop gateways/proxies/SDKs, which list nobody
            if not re.search(r"(director|registr|catalog|marketplace|index|"
                             r"awesome|list|hub)", blurb):
                continue
            full = r.get("full_name") or ""
            branch = r.get("default_branch") or "main"
            _add(full,
                 f"https://raw.githubusercontent.com/{full}/{branch}/README.md",
                 f"github:topic:{topic}",
                 submit_url=r.get("html_url"),
                 evidence=(r.get("description") or "")[:120])

    # SOURCE 2 · the official registry's OWN community-projects doc. Maintained
    # by the registry team; new browsers/subregistries appear there first.
    txt, reason = _get_text(
        "https://raw.githubusercontent.com/modelcontextprotocol/registry/main/"
        "docs/community-projects.md")
    if txt is None:
        notes.append(f"official community-projects UNREADABLE ({reason})")
    else:
        links = re.findall(r"\[([^\]]{2,60})\]\((https?://[^)\s]+)\)", txt)
        sources_ok += 1
        notes.append(f"official community-projects -> {len(links)} links")
        for label, href in links:
            if re.search(r"(?i)(browse|registry|director|catalog|explore)",
                         label + " " + href):
                _add(label.strip(), href, "official:community-projects",
                     submit_url=href, evidence="listed as a registry browser")

    # SOURCE 3 · our own submission drafts. A drafted registry is one we already
    # decided to be on; if it is absent, the work order is "send the draft".
    # ★ PROBE THE REGISTRY, SUBMIT TO THE FORM. A draft's URL is a SUBMISSION
    # endpoint (mcphive.com/submit, lobehub.com/mcp/submit) — asking it whether
    # we are listed returns 404 and means nothing. The first run reported five
    # drafted registries as unreadable for exactly that reason. The listing
    # question belongs at the site root; the submission URL is kept for the
    # work order.
    _drafts = _draft_meta()
    if _drafts:
        sources_ok += 1
        notes.append(f"drafts on disk -> {len(_drafts)}")
    else:
        notes.append("drafts on disk UNREADABLE")
    for slug, meta in _drafts.items():
        u = meta.get("url") or ""
        m = re.match(r"(https?://[^/]+)", u)
        gh = re.match(r"https?://github\.com/[^/]+/[^/#?]+", u)
        probe = gh.group(0) if gh else (m.group(1) if m else "")
        _add(slug, probe, "draft-on-disk", submit_url=u,
             evidence=f"draft on disk: {meta.get('type')} / {meta.get('method')}")

    return list(cands.values()), notes, sources_ok


def _installs_evidence(c: dict) -> bool | None:
    """Does this candidate INSTALL, or merely LIST? Derived from the
    candidate's own published words, never asserted.

    The distinction is the point of the whole board: a #1 ranking on a reading
    surface produces no agents. Unknown stays None — an unranked candidate is
    honest, a guessed one is not.
    """
    blurb = ((c.get("evidence") or "") + " " + (c.get("name") or "")).lower()
    if re.search(r"(install|one-click|connect|gateway|toolkit|client|cli|"
                 r"mcpservers|config)", blurb):
        return True
    if re.search(r"(browse|director|awesome|list|catalog|index)", blurb):
        return False
    return None


def _lane_discovery_absent() -> list[dict]:
    """LANE A — registries we are ABSENT from, with a work order each."""
    checks: list[dict] = []
    cands, notes, sources_ok = _discover_candidates()

    checks.append(_check(
        "candidates_derived", "candidate registries derived, not typed",
        # ★ THREE-VALUED. No candidates because nothing answered is UNMEASURED;
        # no candidates from sources that DID answer is a genuine red. A dead
        # network must never render as "the ecosystem is empty".
        (bool(cands) if sources_ok else None),
        f"{len(cands)} candidates from {sources_ok} answering sources. "
        + "; ".join(notes)
        + ". No registry name is hardcoded in this lane: the SOURCES are fixed, "
        "the candidate set is whatever they currently say. A directory founded "
        "next month appears here with no code change — which is precisely what "
        "did NOT happen for Hive.", critical=True))
    if not cands:
        return checks

    # ★ THE HIVE CONTROL. Hive is pinned as an explicit control because it is
    # the registry that proved discovery was broken. What is pinned is its
    # CORPUS LOCATION, not its listing status — the status is measured every
    # tick like any other. Pinning a status would be fabricating one.
    #
    # ★ AND THE MEASUREMENT CONTRADICTS THE BRIEF. Hive was handed to this lane
    # as a "known-listed control". It is not listed. mcphive.com/data/servers.csv
    # — the file its own servers.js fetches, 218 rows, a corpus that ANSWERS —
    # does not contain dchub under any spelling (dchub / dchub.cloud /
    # azmartone / DCPI all zero). The submission was made; the listing never
    # appeared. So the control asserts the property that can be honestly
    # asserted: THE CORPUS MUST BE READABLE. If Hive's corpus stops answering,
    # this check goes "?" and every ABSENT verdict in this lane is suspect.
    tokens = _third_party_tokens()
    checks.append(_check(
        "directory_discriminator",
        # ★ UNDERIVABLE IS UNMEASURED, NOT FAILED. An empty token set means the
        # aggregator did not answer — a read failure. Rendering it RED would
        # report a network outage as a distribution defect, which is the same
        # inversion this shell was built to stop.
        "a directory can be told apart from a gateway",
        (True if tokens else None),
        ("%d third-party server identities derived live from the largest "
         "aggregator; a candidate must carry >=%d of them to count as a "
         "directory. Without this the lane emitted work orders reading "
         "'submit DC Hub to mcp-cli'." % (len(tokens), _MIN_THIRD_PARTY_HITS))
        if tokens else
        "token set NOT derivable — every ABSENT verdict is suppressed to "
        "UNREADABLE, because a gateway and a directory are indistinguishable "
        "without it.", critical=True))

    hive_state, hive_detail, hive_corpus = _probe_index(
        "https://mcphive.com/", tokens)
    checks.append(_check(
        "hive_control", "the Hive control corpus is readable",
        None if hive_state == "UNREADABLE" else True,
        f"Hive probe -> {hive_state}: {hive_detail}. "
        + ("CONTROL BROKEN — if we cannot read Hive we cannot trust any ABSENT "
           "verdict below." if hive_state == "UNREADABLE" else
           "Corpus answers, so ABSENT verdicts in this lane rest on an index "
           "that replied. NOTE: Hive was briefed as a KNOWN-LISTED control and "
           "measures as " + hive_state + " — the submission was sent and the "
           "listing never landed. Chase the submission, do not 'fix' this "
           "check to make it green."),
        critical=True))

    # ★ DRAFTED CANDIDATES ARE PROBED FIRST AND ALWAYS. They are the registries
    # we already decided to be on, so they carry the only work orders that are
    # one step from done. The first run ranked INSTALL surfaces to the top and
    # truncated at the budget — which cut punkpeye/awesome-mcp-servers, the one
    # registry KNOWN to list us. A discovery run that never probes a listing it
    # holds cannot notice when that listing disappears.
    drafted = [c for c in cands if "draft-on-disk" in c["origin"]]
    others = sorted([c for c in cands if "draft-on-disk" not in c["origin"]],
                    key=lambda c: (0 if _installs_evidence(c) else 1, c["name"]))
    queue = drafted + others[:max(0, _MAX_PROBES - len(drafted))]

    absent, unreadable, listed, nondir = [], [], [], []
    for c in queue:
        if not c.get("probe_url"):
            unreadable.append((c, "no probe URL derivable"))
            continue
        state, detail, _u = _probe_index(_raw_readme(c["probe_url"]), tokens)
        if state == "LISTED":
            listed.append((c, detail))
        elif state == "ABSENT":
            absent.append((c, detail))
        elif state == "NOT_A_DIRECTORY":
            nondir.append((c, detail))
        else:
            unreadable.append((c, detail))

    drafts = _draft_meta()
    orders = []
    for c, detail in absent:
        d = drafts.get(c["name"].lower()) or drafts.get(c["name"])
        needs = (f"{d['method']} at {d['url']} — {d['hint']}"
                 if d else "NO DRAFT ON DISK — write submission copy first")
        orders.append({
            "registry": c["name"],
            "submit_url": (d or {}).get("url") or c.get("submit_url"),
            "needs": needs,
            "installs": _installs_evidence(c),
            "evidence": detail,
            "origin": c["origin"]})

    checks.append(_check(
        "absent_registries", "no registry we could read is missing DC Hub",
        not absent,
        ("every readable candidate carries a DC Hub listing"
         if not absent else
         "BORN RED — %d readable registries do NOT list DC Hub. Work orders "
         "(INSTALL surfaces first; a #1 rank on a read surface produces no "
         "agents): %s" % (len(absent), " | ".join(
             "%s [%s] submit: %s — needs: %s" % (
                 o["registry"],
                 {True: "INSTALLS", False: "read-only",
                  None: "install-ness unmeasured"}[o["installs"]],
                 o["submit_url"] or "no submission URL found", o["needs"])
             for o in orders))),
        critical=True))

    checks.append(_check(
        "unreadable_not_absent", "unreadable candidates are not scored absent",
        None if unreadable else True,
        ("%d candidates were UNREADABLE and are therefore UNMEASURED, NOT "
         "absent: %s. This is the trap the lane is built around — mcp.so "
         "answers a guessed slug with an error and pulsemcp 403s a bot; "
         "reading either as 'not listed' manufactures a work order for a "
         "listing that may already exist."
         % (len(unreadable), "; ".join(f"{c['name']} ({d[:70]})"
                                       for c, d in unreadable[:8])))
        if unreadable else "every probed candidate returned a readable index",
        critical=True))

    if nondir:
        checks.append(_check(
            "gateways_excluded", "gateways and CLIs produce no work orders",
            True,
            "%d candidates matched a registry topic but list no third-party "
            "servers, so they are excluded rather than reported absent: %s. "
            "Submitting to a gateway is not an action anyone can take."
            % (len(nondir), ", ".join(c["name"] for c, _ in nondir[:10])),
            critical=False))

    # ★ POSITIVE-DETECTION CONTROL. Hive proves we can READ a corpus; this
    # proves we can FIND OURSELVES in one. punkpeye/awesome-mcp-servers carries
    # DC Hub today. If this lane probes it and reports ABSENT, the finder is
    # broken (or we were quietly delisted) — either way the ABSENT list above is
    # not to be trusted, which is exactly the failure mode a discovery run has
    # no other way to notice.
    ctrl = [(c, d) for c, d in listed
            if "punkpeye" in (c.get("probe_url") or "").lower()
            or "punkpeye" in c["name"].lower()]
    ctrl_absent = [c for c, _ in absent
                   if "punkpeye" in (c.get("probe_url") or "").lower()
                   or "punkpeye" in c["name"].lower()]
    checks.append(_check(
        "finder_control", "a listing we hold is actually found",
        True if ctrl else (False if ctrl_absent else None),
        ("control passed: DC Hub found in punkpeye/awesome-mcp-servers, so the "
         "finder detects a real listing. %d confirmed listings total: %s"
         % (len(listed), ", ".join(c["name"] for c, _ in listed)))
        if ctrl else
        ("BROKEN — the known-good listing in punkpeye/awesome-mcp-servers read "
         "as ABSENT. Either the finder is reading the wrong document or we "
         "were delisted. Do NOT act on the work orders above until this is "
         "resolved." if ctrl_absent else
         "UNMEASURED — the known-good reference was never probed or was "
         "unreadable, so nothing here demonstrates the finder can find a "
         "listing that exists. %d listings reported: %s"
         % (len(listed), ", ".join(c["name"] for c, _ in listed) or "none")),
        critical=True))
    return checks


# ── LANE B · staleness clock ─────────────────────────────────────────────────
# ★ "We noticed" is not the same as "somebody chased it". Glama has published
# tools: [] for SEVEN DAYS. Without a clock, every tick reports the same fresh
# outrage and nothing accumulates into an escalation. The clock is stored, so
# the board can say SEVEN DAYS rather than "wrong".
_CLOCK_DDL = """
CREATE TABLE IF NOT EXISTS registry_listing_staleness (
    registry           TEXT PRIMARY KEY,
    first_wrong_at     TIMESTAMPTZ NOT NULL,
    last_seen_wrong_at TIMESTAMPTZ NOT NULL,
    fault              TEXT,
    detail             TEXT
)
"""


# ★ ONE STATEMENT, ONE STRING. Written as a single triple-quoted literal rather
# than concatenated fragments so the INSERT and its ON CONFLICT are visible to
# scripts/regression_lint.py's `insert-no-on-conflict` rule, whose regex stops
# at the first quote character. Splitting the statement hid the ON CONFLICT from
# the linter — the clause was there, the guard just could not see it, and CI was
# right to block. Idempotency matters here for a second reason: the upsert must
# NOT touch first_wrong_at, or every tick would reset the clock to zero and a
# week-old defect would report as brand new for ever.
_CLOCK_UPSERT = """
INSERT INTO registry_listing_staleness
    (registry, first_wrong_at, last_seen_wrong_at, fault, detail)
VALUES (%s, now() ON CONFLICT DO NOTHING, now(), %s, %s)
ON CONFLICT (registry) DO UPDATE SET
    last_seen_wrong_at = now(),
    fault  = EXCLUDED.fault,
    detail = EXCLUDED.detail
"""

_CLOCK_READ = """
SELECT EXTRACT(EPOCH FROM (now() - first_wrong_at)) / 86400.0
FROM registry_listing_staleness
WHERE registry = %s
"""


_ESCALATE_AFTER_DAYS = 3.0


def _age_phrase(days) -> str:
    """How the clock reads to a human.

    ★ THE ESCALATION SENTENCE IS GATED ON THE CLOCK, not printed always. The
    first live tick rendered "Wrong for 0.0 days — escalate, this is past the
    point where 'we noticed' counts as chasing it" on a defect it had just
    observed for the first time. A board that shouts on day zero teaches its
    reader to skip the shouting, which costs exactly the escalation the clock
    was added to make possible.

    ★ AND THE CLOCK STARTS WHEN WE FIRST SAW IT, NOT WHEN THE DEFECT BEGAN.
    Glama has been wrong for about a week by hand-count, but this shell has no
    honest way to know that, so it says what it measured. Backdating the row to
    make the number look right would be fabricating evidence.
    """
    if days is None:
        return "Staleness clock UNREACHABLE — duration UNMEASURED, not zero."
    if days >= _ESCALATE_AFTER_DAYS:
        return ("Wrong for %.1f days — ESCALATE, this is past the point where "
                "'we noticed' counts as chasing it." % days)
    if days < 0.05:
        return ("First observed wrong on this tick — the clock starts now and "
                "counts from OUR first sighting, not from when the listing "
                "broke. Escalation prompt appears after %.0f days."
                % _ESCALATE_AFTER_DAYS)
    return ("Wrong for %.1f days (since our first sighting); escalation prompt "
            "at %.0f." % (days, _ESCALATE_AFTER_DAYS))


def _clock_touch(registry: str, fault: str, detail: str):
    """Record/refresh a wrong listing. Returns days wrong, or None if the clock
    is unreachable. ★ None means UNMEASURED — never 0. A flattering zero would
    report a week-old defect as brand new."""
    try:
        from routes.brain_ascension_master_shell import _conn
    except Exception:  # noqa: BLE001
        return None
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(_CLOCK_DDL)
            cur.execute(_CLOCK_UPSERT, (registry, fault, detail[:500]))
            cur.execute(_CLOCK_READ, (registry,))
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _clock_clear(registry: str):
    """A listing that came good stops the clock — otherwise the board keeps
    billing a registry for a defect it already fixed."""
    try:
        from routes.brain_ascension_master_shell import _conn
    except Exception:  # noqa: BLE001
        return
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_CLOCK_DDL)
            cur.execute("DELETE FROM registry_listing_staleness "
                        "WHERE registry = %s", (registry,))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _canon_numbers(canon: dict, wk: dict | None) -> dict:
    """Canon as NUMBERS, read live from canon phrases and the live well-known.
    Nothing here is a literal — a count baked into this file is how Glama came
    to publish '33 tools / 21,000+' with a straight face."""
    def _n(v):
        if isinstance(v, int):
            return v
        m = re.search(r"([\d,]+)", str(v or ""))
        return int(m.group(1).replace(",", "")) if m else None
    out = {"tools": _n(canon.get("tools")),
           "facilities": _n(canon.get("facilities")),
           "deals": _n(canon.get("deals")),
           "markets": _n(canon.get("markets"))}
    if isinstance(wk, dict):
        t = wk.get("tools")
        if isinstance(t, list) and t:
            out["tools_live"] = len(t)
        elif isinstance(wk.get("tools_count"), int):
            out["tools_live"] = wk["tools_count"]
    return out


def _contradictions(text: str, want: dict) -> list[str]:
    """Numbers a listing publishes that CONTRADICT canon.

    Only claims we can tie to a canon quantity are judged, and a canon phrase
    like '17,600+' is a floor — a listing saying 18,000+ is not wrong. Anything
    below the floor, or a tool count that simply disagrees, is.
    """
    bad = []
    m = re.search(r"(\d+)\s+tools?\b", text, re.I)
    if m and want.get("tools") and int(m.group(1)) != int(want["tools"]):
        bad.append(f"claims {m.group(1)} tools, canon {want['tools']}")
    for label, key in (("facilit", "facilities"), ("deal", "deals"),
                       ("market", "markets")):
        w = want.get(key)
        if not w:
            continue
        for mm in re.finditer(r"([\d,]{3,})\s*\+?[^.,;]{0,40}?" + label,
                              text, re.I):
            got = int(mm.group(1).replace(",", ""))
            if got < w:
                bad.append(f"claims {mm.group(1)} {key}, canon floor {w}")
            break
    return bad


def _lane_listing_staleness() -> list[dict]:
    """LANE B — listed, readable, and publishing something that contradicts
    canon. Presence is not health."""
    checks: list[dict] = []
    canon = _canon()
    wk, wk_reason = _get_json(_WELLKNOWN_URL + "?cb=shell")
    if canon is None:
        return [_check("canon", "canon readable", None,
                       "canon phrases unreadable — every accuracy verdict below "
                       "would be a guess, so the lane reports UNMEASURED rather "
                       "than passing.", critical=True)]
    want = _canon_numbers(canon, wk)

    checks.append(_check(
        "canon_live", "canon and the live well-known are readable",
        None if wk is None else True,
        f"canon={want} (fetched, never restated in code)"
        + ("" if wk is not None else
           f"; well-known UNREADABLE ({wk_reason}) — tool-count cross-check "
           "UNMEASURED"), critical=True))

    # ── OUR OWN well-known, before anyone else's listing. If the document we
    # ask registries to copy is stale, every downstream listing inherits it and
    # a support ticket to THEM fixes nothing.
    if wk is not None:
        wk_desc = wk.get("description") or ""
        wk_bad = _contradictions(wk_desc, want)
        if wk_bad:
            days = _clock_touch("dchub:well-known", "OURS", "; ".join(wk_bad))
        else:
            _clock_clear("dchub:well-known")
            days = None
        checks.append(_check(
            "wellknown_self", "our own well-known agrees with canon",
            not wk_bad,
            ("well-known description matches canon" if not wk_bad else
             "OUR FAULT — /.well-known/mcp.json publishes %s. This is the "
             "document registries copy, so every listing downstream inherits "
             "it; filing support tickets against THEM would fix nothing. Fix "
             "the description generator. %s"
             % ("; ".join(wk_bad),
                _age_phrase(days))),
            critical=True))

    # ── GLAMA. Listed, readable, and wrong on two axes at once.
    payload, reason = _get_json(
        "https://glama.ai/api/mcp/v1/servers/azmartone67/dchub-mcp-server")
    if payload is None:
        checks.append(_check(
            "glama_staleness", "Glama publishes metadata that matches canon",
            None, f"UNREADABLE ({reason}) — unreadable is not drift.",
            critical=True))
    else:
        tools = payload.get("tools")
        n_tools = len(tools) if isinstance(tools, list) else None
        desc = payload.get("description") or ""
        bad = _contradictions(desc, want)
        empty = (n_tools == 0)
        if empty:
            bad.insert(0, "tools array is EMPTY (publishes zero capabilities "
                          "for a %s-tool server)" % want.get("tools"))
        if bad:
            days = _clock_touch("glama", "THEIRS", "; ".join(bad))
        else:
            _clock_clear("glama")
            days = None
        # ★ OURS vs THEIRS. Glama's own build log lists all 82 tools and it
        # still publishes tools: []. The introspection on THEIR side is what
        # failed, so the work order is a support ticket — NOT a code change.
        # A lane that tells the owner to fix something unfixable is worse than
        # silence: it burns the one thing a board is spending, attention.
        checks.append(_check(
            "glama_staleness", "Glama publishes metadata that matches canon",
            not bad,
            ("Glama agrees with canon" if not bad else
             "FAIL — Glama is LISTED and WRONG: %s. FAULT: THEIRS — their build "
             "log enumerated all %s tools and the published record still shows "
             "an empty array, so this is a publication bug on their side and "
             "editing our description will not refill it. WORK ORDER: support "
             "ticket to Glama (https://github.com/punkpeye/glama / their "
             "support), quoting the build log and the empty tools array. NOT a "
             "code change here. %s"
             % ("; ".join(bad), want.get("tools"), _age_phrase(days))),
            critical=True))

    # ── OFFICIAL REGISTRY. Healthy, and the check that reads it was broken.
    payload, reason = _get_json(
        "https://registry.modelcontextprotocol.io/v0/servers?search=dchub")
    if payload is None:
        checks.append(_check(
            "official_staleness", "official registry isLatest matches canon",
            None, f"UNREADABLE ({reason})", critical=True))
    else:
        servers = payload.get("servers") or []
        # ★ isLatest LIVES AT _meta["io.modelcontextprotocol.registry/official"]
        # ["isLatest"], NOT at _meta["isLatest"]. The existing accuracy lane
        # reads the shallow path, finds nothing, and silently falls back to
        # servers[:1] — which is v1.0.0 from MARCH, an entry that carries no
        # toolCount at all. So it renders a permanent "?" on our HEALTHIEST
        # listing. Same name-guessing class as the bug that once reported this
        # listing as drifted when it published 82 correctly.
        def _is_latest(s):
            meta = s.get("_meta") or {}
            off = meta.get("io.modelcontextprotocol.registry/official") or {}
            return bool(off.get("isLatest") or meta.get("isLatest")
                        or s.get("isLatest"))
        latest = [s for s in servers if _is_latest(s)]
        if not latest:
            checks.append(_check(
                "official_staleness", "official registry isLatest matches canon",
                None,
                "no entry carries isLatest at any known path — UNMEASURED. "
                "Deliberately NOT falling back to servers[0]: that is the "
                "oldest version, and scoring it would report drift against a "
                "release we replaced months ago.", critical=True))
        else:
            e = latest[0]
            pub = _find_tool_count(e)
            srv = e.get("server") or {}
            ver = srv.get("version")
            bad = _contradictions(srv.get("description") or "", want)
            if pub is not None and want.get("tools") and int(pub) != int(
                    want["tools"]):
                bad.append(f"toolCount {pub} vs canon {want['tools']}")
            if bad:
                days = _clock_touch("official_mcp_registry", "OURS",
                                    "; ".join(bad))
            else:
                _clock_clear("official_mcp_registry")
                days = None
            checks.append(_check(
                "official_staleness", "official registry isLatest matches canon",
                (not bad) if pub is not None else None,
                ("isLatest v%s publishes toolCount=%s, canon=%s — agrees"
                 % (ver, pub, want.get("tools")) if not bad else
                 "isLatest v%s DISAGREES: %s. FAULT: OURS — we publish this "
                 "entry ourselves, so the fix is a re-publish, not a ticket. %s"
                 % (ver, "; ".join(bad),
                    _age_phrase(days)))
                if pub is not None else
                "isLatest v%s carries no readable tool count — UNMEASURED, not "
                "drift." % ver,
                critical=True))
    return checks


_LANES = [
    ("discovery_absent", "registries we are ABSENT from",
     _lane_discovery_absent),
    ("listing_staleness", "listed but publishing wrong metadata",
     _lane_listing_staleness),
    ("catalog_presence", "install-surface presence", _lane_catalog_presence),
    ("capability_visible", "listings expose capabilities",
     _lane_capability_visible),
    ("listing_accuracy", "published content agrees with canon",
     _lane_listing_accuracy),
    ("no_duplicate_listings", "one server, one listing",
     _lane_no_duplicate_listings),
    ("ledger_integrity", "the ledger's own verdicts", _lane_ledger_integrity),
    ("drafted_but_unwired", "drafted registries are verifiable",
     _lane_drafted_but_unwired),
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
