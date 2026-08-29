"""MCP registry presence watcher.

Phase HJ-2 (2026-06-05) — verifies DC Hub is listed in the major MCP
registries. Most registries don't expose programmatic submission, so
this module FETCHES each registry and checks whether DC Hub appears.
When DC Hub is missing from any registry, the watcher files a brain
finding so the autopilot or a human can ship the submission PR.

Registries tracked:
  • smithery.ai     — central directory, REST API for search
  • mcp.so          — community directory, scrapeable HTML
  • awesome-mcp-servers (GitHub) — README list
  • Anthropic registry — official discoverability (Q4 2025)
  • Cline marketplace  — VS Code extension catalog (manual submission)
  • Cursor MCP catalog — Cursor's curated tools

Endpoints:
  GET /api/v1/brain/mcp-registries    — current presence status across all
  POST /api/v1/admin/brain/mcp-registries/scan  — trigger a fresh scan
                                          (admin-key gated, fail-closed)

Cron: .github/workflows/mcp-registry-watch.yml runs weekly and POSTs
to the scan endpoint. Findings flow into the brain via the standard
brain_findings table (same as other autopilot findings).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Dict, List

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write


mcp_registry_watch_bp = Blueprint("mcp_registry_watch", __name__)


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")

# The strings we expect to find in each registry's payload that confirm
# DC Hub is listed. Most registries use the URL or the name "dchub" /
# "DC Hub" / "DCPI" / "Data Center Power Index".
_PRESENCE_MARKERS = (
    "dchub.cloud",
    "dchub",
    "DC Hub",
    "Data Center Power Index",
)


def _fetch(url: str, timeout: int = 15) -> tuple[int, str, str]:
    """GET a URL with a real Chrome UA. Returns (status, body, final_url).

    ★ final_url IS THE POINT. urlopen follows redirects SILENTLY, so a probe
    URL that has rotted into a 301 still returns 200 with the body of whatever
    it landed on -- and if that landing page happens to echo our marker, the
    registry scores "present" on a listing that does not exist. That is not
    hypothetical: four probe URLs in this file have rotted that way (mcp.so's
    invented slug, the modelcontextprotocol README, mcphub.io's SPA shell, and
    Glama's /dchub stub). Returning the landing URL lets _probe_all refuse to
    score a body it did not ask for.
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _BROWSER_UA,
            "Accept":     "text/html,application/json,*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return r.status, body, (r.geturl() or url)
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:
        return 0, f"_fetch_error: {str(e)[:120]}", url


def _same_resource(requested: str, final: str) -> bool:
    """True when a redirect did NOT change which resource we read.

    Scheme swaps, www, and trailing slashes are cosmetic. A different HOST or
    PATH means the registry sent us somewhere else, and its body must not be
    scored as an answer about the URL we asked for.
    """
    try:
        a, b = urllib.parse.urlsplit(requested), urllib.parse.urlsplit(final)
    except Exception:
        return True   # unparseable -> do not invent a failure
    norm = lambda p: (p.netloc.lower().removeprefix("www."),
                      p.path.rstrip("/").lower())
    return norm(a) == norm(b)


# The official MCP registry exposes a JSON search API rather than a
# scrapeable page. DC Hub publishes here (mcp-registry-publish.yml); this
# is the canonical replacement for the deprecated servers README list.
_OFFICIAL_REGISTRY_API = (
    "https://registry.modelcontextprotocol.io/v0/servers?search=cloud.dchub")
_OFFICIAL_SERVER_NAME = "cloud.dchub/mcp-server"


def _probe_official_registry(timeout: int = 25) -> tuple[int, str]:
    """Check DC Hub's presence in the official MCP registry via its API.

    Returns (status, body) shaped for the generic _present_in_body path:
    a synthetic body carrying a presence marker when we're listed, empty
    otherwise. The registry can be slow, so we allow a longer timeout.
    """
    status, body, _final = _fetch(_OFFICIAL_REGISTRY_API, timeout=timeout)
    if status != 200:
        return status, body
    try:
        servers = json.loads(body).get("servers", [])
        listed = any(
            (s.get("server") or {}).get("name") == _OFFICIAL_SERVER_NAME
            for s in servers)
        return 200, ("dchub.cloud listed" if listed else "")
    except Exception:
        return 0, ""


def _present_in_body(body: str) -> bool:
    """Does the body contain any of our presence markers?"""
    if not body:
        return False
    body_lower = body.lower()
    return any(m.lower() in body_lower for m in _PRESENCE_MARKERS)


# ── Per-registry probes ─────────────────────────────────────────

_REGISTRIES = [
    {
        "id":           "smithery_server",
        "name":         "smithery.ai server page",
        # 2026-06-05: Smithery's slug is namespaced by GitHub owner —
        # the actual canonical path is /servers/<gh-user>/<repo>, not
        # /server/<name>. DC Hub is live at /servers/azmartone67/dchub
        # (verified via Chrome inspection: 95/100 score, 99.6% uptime,
        # 162ms p50 latency, 979 lifetime tool calls).
        "url":          "https://smithery.ai/servers/azmartone67/dchub",
        "submission_url": "https://smithery.ai/new",
    },
    {
        "id":           "mcp_so",
        "name":         "mcp.so directory",
        # r-mcpso-url (2026-07-18): we probed /server/dchub — a slug that never
        # existed — and reported "missing" for weeks while TWO real listings
        # were live at /servers/dchub-mcp-server (primary, indexed from the
        # mcp-server repo) and /servers/dchub-backend (secondary). Watch the
        # primary; its copy refreshes from the repo README on their re-crawl.
        "url":          "https://mcp.so/servers/dchub-mcp-server",
        "submission_url": "https://github.com/chatmcp/mcp-directory",
    },
    {
        "id":           "awesome_mcp_servers",
        "name":         "awesome-mcp-servers (GitHub README)",
        "url":          "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
        "submission_url": "https://github.com/punkpeye/awesome-mcp-servers/pulls",
    },
    {
        # 2026-06-27: the modelcontextprotocol/servers README now states
        # it houses ONLY the steering-group reference servers — third-party
        # servers were moved to the official REST registry. Probing the
        # README therefore ALWAYS returned "missing" (a false positive),
        # since we'll never be added there. DC Hub publishes to the official
        # registry via mcp-registry-publish.yml and is live (latest v2.3.3),
        # so we probe the registry API instead and stop self-flagging.
        "id":           "modelcontextprotocol_registry",
        "name":         "Official MCP Registry (registry.modelcontextprotocol.io)",
        "url":          _OFFICIAL_REGISTRY_API,
        "submission_url": "https://github.com/modelcontextprotocol/registry",
        "probe":        "official_registry",
    },
    {
        # Cursor has no submittable registry — it's a docs page + a
        # community forum, so there's no PR path to "fix" being absent.
        # actionable=False keeps it visible in the audit but excludes it
        # from the "missing" alarm count, so the weekly watch workflow
        # stops opening an un-actionable issue every Monday forever.
        "id":           "cursor_mcp",
        "name":         "Cursor MCP catalog",
        "url":          "https://docs.cursor.com/context/model-context-protocol",
        "submission_url": "https://forum.cursor.com",
        "actionable":   False,
    },
    # 2026-07-01: added the four community registries DC Hub is verifiably
    # listed on but which were NOT monitored — so "listed on Glama/PulseMCP/
    # LobeHub/mcphub.io" is now a CHECKED fact, not a hardcoded /ai claim.
    # (2026-07-02: the mcphub_io "verified" turned out to be a false
    # positive — see its entry.) Glama uses the canonical /servers/<owner>/
    # <repo> page; PulseMCP uses its JSON API (the HTML page bot-blocks 403).
    {
        "id":           "glama_ai",
        "name":         "Glama.ai MCP directory",
        # r-glamaslug (2026-08-08): this probed /azmartone67/dchub, which 301s
        # and has NO API record (name=None). urlopen follows redirects
        # silently, so the probe scored whatever it landed on and reported
        # Glama "present" for a stub. The real listing -- the one carrying
        # all 82 tools and the A grades -- is /azmartone67/dchub-mcp-server.
        # Fourth slug-rot in this file; see the redirect guard in _probe_all.
        "url":          "https://glama.ai/mcp/servers/azmartone67/dchub-mcp-server",
        "submission_url": "https://glama.ai/mcp/servers",
    },
    {
        "id":           "pulsemcp",
        "name":         "PulseMCP directory",
        # 2026-07-01: the api.pulsemcp.com v0beta query endpoint now 410s
        # (deprecated/rate-limited for everyone) — probe the canonical HTML
        # server page instead (200 + contains "dchub", verified live).
        "url":          "https://www.pulsemcp.com/servers/dchub",
        "submission_url": "https://www.pulsemcp.com/",
    },
    {
        "id":           "lobehub",
        "name":         "LobeHub MCP marketplace",
        # r-lobehubslug (2026-08-15): LobeHub re-slugged the listing to
        # azmartone67-dchub-mcp-server (fifth slug-rot in this file) AND
        # lobehub.com/mcp/<slug> now 302s to market.lobehub.com/s/plugins/
        # <slug> — probing the vanity URL trips the redirect guard and scores
        # "redirected" forever. Probe the final host directly: 200 with full
        # DC Hub identity, a control slug 404s there, so no SPA-shell false
        # positive. Old slug 404s at market (verified 2026-08-15).
        "url":          "https://market.lobehub.com/s/plugins/azmartone67-dchub-mcp-server",
        "submission_url": "https://github.com/lobehub/lobe-chat-agents",
    },
    {
        # r-fix 2026-07-02: the 07-01 "verified listed" was a FALSE
        # POSITIVE — /servers/<slug> is a Next.js SPA shell that 200s
        # for ANY slug with "dchub" echoed in its route params, so the
        # marker check always passed. Real data source is
        # registry.mcphub.io (their client fetches /registry, /search)
        # — DOWN with CF 525 as of 2026-07-02, and the only archived
        # snapshot (2025-03) has no dchub, so we were likely never
        # listed. Probe the API instead; non-actionable because their
        # submit page 404s and the data backend is dead — a "missing"
        # verdict has no submission path to act on.
        "id":           "mcphub_io",
        "name":         "mcphub.io directory",
        "url":          "https://registry.mcphub.io/search?q=dchub",
        "submission_url": "https://mcphub.io/",
        "actionable":   False,
    },
    {
        # 2026-08-08: docker/mcp-registry ships inside Docker Desktop's MCP
        # Toolkit, which one-click-installs into Claude Desktop, Cursor and
        # VS Code. It is an INSTALL surface, not a reading surface -- the
        # only one on this roster. Probing the raw server.yaml means the
        # verdict is the literal presence of our entry in their main branch:
        # 404 (missing) until PR #4644 merges, then green with no code change.
        "id":           "docker_mcp_catalog",
        "name":         "Docker MCP Catalog (Docker Desktop MCP Toolkit)",
        "url":          "https://raw.githubusercontent.com/docker/mcp-registry/main/servers/dchub/server.yaml",
        "submission_url": "https://github.com/docker/mcp-registry/pulls",
    },
]


def _probe_all() -> Dict[str, dict]:
    """Probe every registry; return per-id status."""
    out = {}
    for r in _REGISTRIES:
        redirected_to = None
        if r.get("probe") == "official_registry":
            status, body = _probe_official_registry()
        else:
            status, body, final = _fetch(r["url"])
            if status == 200 and not _same_resource(r["url"], final):
                redirected_to = final
        if redirected_to:
            # A moved listing is UNKNOWN, never "present". Scoring the landing
            # page is how a dead slug reads green for weeks.
            verdict = "redirected"
        elif status == 200:
            present = _present_in_body(body)
            verdict = "present" if present else "missing"
        elif status == 0:
            verdict = "fetch_error"
        else:
            verdict = f"http_{status}"
        out[r["id"]] = {
            "registry":       r["name"],
            "registry_url":   r["url"],
            "submission_url": r["submission_url"],
            "verdict":        verdict,
            "http_status":    status,
            "actionable":     r.get("actionable", True),
            "redirected_to":  redirected_to,
        }
    return out


# r-probecache (2026-07-03): _probe_all() serially fetches ~9 registries,
# so the public GET cost ~5s at origin on every cache-miss variant.
# Registries only change on PR merges — cache the probe result in-process
# for 1h (same pattern as main.py _FIBER_INTEL_CACHE). Per-worker cache;
# the admin scan endpoint forces a fresh probe and re-primes it.
_PROBE_CACHE: dict = {"data": None, "at": 0.0}
_PROBE_TTL_SEC = 3600.0


def _probe_all_cached(force: bool = False) -> Dict[str, dict]:
    now = time.time()
    if (not force and _PROBE_CACHE["data"] is not None
            and (now - _PROBE_CACHE["at"]) < _PROBE_TTL_SEC):
        return _PROBE_CACHE["data"]
    data = _probe_all()
    _PROBE_CACHE["data"] = data
    _PROBE_CACHE["at"] = now
    return data


def _file_findings_for_missing(results: Dict[str, dict]) -> List[dict]:
    """For each missing registry, return a brain-finding-shaped dict.

    Caller threads these into the brain_findings table (same shape as
    other autopilot findings).
    """
    findings = []
    for rid, info in results.items():
        # Skip registries with no submission path — flagging them is noise,
        # not an action item (e.g. Cursor: docs page + forum, no PR).
        if not info.get("actionable", True):
            continue
        if info["verdict"] in ("missing", "fetch_error"):
            findings.append({
                "kind":     "mcp_registry_missing",
                "subject":  rid,
                "url":      info["submission_url"],
                "evidence": (f"{info['registry']}: probe returned "
                              f"{info['verdict']} (HTTP {info['http_status']}). "
                              f"Submit DC Hub MCP server at {info['submission_url']}."),
                "severity": "medium",
            })
    return findings


# ── New-registry discovery ──────────────────────────────────────
#
# The audit above watches a FIXED list. This half hunts for NEW MCP
# registries / directories that have appeared since we last looked, so a
# new partner surface doesn't stay invisible until a human stumbles on
# it. It surfaces CANDIDATES — it does NOT silently enroll them, because
# a junk repo shouldn't land in the audited set — filing one low-severity
# brain finding per new domain for autopilot/human vetting.

# Domains we already track (audited list + outreach targets + known dead
# ends). A candidate resolving to one of these is not "new".
_KNOWN_REGISTRY_DOMAINS = {
    "smithery.ai", "mcp.so", "glama.ai", "mcphub.io", "pulsemcp.com",
    "lobehub.com", "market.lobehub.com", "mcphive.com", "yellowmcp.com",
    "cursor.com", "claude.ai", "registry.modelcontextprotocol.io",
}

# Never treat these as a registry even if a discovery source links them.
_DISCOVERY_DENYLIST = {
    "github.com", "raw.githubusercontent.com", "githubusercontent.com",
    "anthropic.com", "modelcontextprotocol.io", "npmjs.com", "pypi.org",
    "youtube.com", "twitter.com", "x.com", "medium.com", "reddit.com",
    "discord.com", "discord.gg", "linkedin.com", "google.com",
    "apache.org", "opensource.org", "wikipedia.org", "shields.io",
    "img.shields.io", "vercel.app", "netlify.app", "pages.dev",
}

# A candidate must read like a registry, not a random MCP server repo.
_REGISTRY_NAME_HINTS = ("registry", "directory", "catalog", "marketplace",
                        "awesome-mcp", "mcp-hub", "mcp-list", "mcp-index")

_GITHUB_REPO_SEARCH = (
    "https://api.github.com/search/repositories"
    "?q=mcp+registry+OR+directory+OR+marketplace+in:name,description"
    "&sort=updated&order=desc&per_page=50")


def _normalize_domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _discover_candidate_registries(timeout: int = 20) -> List[dict]:
    """Search GitHub for new MCP-registry surfaces we don't already track.

    Best-effort: returns [] on any fetch/parse error. Uses GITHUB_TOKEN if
    present (raises the search rate limit) but works unauthenticated for a
    once-daily cron call. Surfaces at most 15 candidates, ranked by stars.
    """
    token = (os.environ.get("GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": _BROWSER_UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(_GITHUB_REPO_SEARCH, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []

    candidates: Dict[str, dict] = {}
    for repo in data.get("items", []):
        name = (repo.get("name") or "")
        blob = f"{name} {repo.get('description') or ''} {repo.get('full_name') or ''}".lower()
        if "mcp" not in blob or not any(h in blob for h in _REGISTRY_NAME_HINTS):
            continue

        # Prefer the repo's homepage (the live registry surface). Fall back
        # to the repo itself only when its NAME strongly signals a registry,
        # so we catch GitHub-README registries without flagging every repo.
        homepage = (repo.get("homepage") or "").strip()
        dom = _normalize_domain(homepage)
        if dom and dom not in _DISCOVERY_DENYLIST and dom not in _KNOWN_REGISTRY_DOMAINS:
            key, cand_url = dom, homepage
        elif any(h in name.lower() for h in _REGISTRY_NAME_HINTS):
            key = (repo.get("full_name") or "").lower()
            cand_url = repo.get("html_url") or ""
        else:
            continue

        if not key or key in candidates or key in _KNOWN_REGISTRY_DOMAINS:
            continue
        candidates[key] = {
            "key":         key,
            "candidate_url": cand_url,
            "repo":        repo.get("full_name"),
            "repo_url":    repo.get("html_url"),
            "stars":       repo.get("stargazers_count", 0),
            "description": (repo.get("description") or "")[:200],
        }

    return sorted(candidates.values(),
                  key=lambda c: c["stars"], reverse=True)[:15]


# r-mingate (2026-07-17): discovery had NO stars gate — it took the top-15 by
# stars, so a 0-star name-matching repo became a "candidate" and filed a finding,
# burying real registries in junk. Gate on stars so onboarding proposals are
# quality-filtered. Tunable; 0 disables the gate.
_MIN_CANDIDATE_STARS = int(os.environ.get("MCP_REGISTRY_MIN_STARS", "25") or "25")


def _findings_for_candidates(candidates: List[dict]) -> List[dict]:
    out = []
    for c in candidates:
        stars = int(c.get("stars", 0) or 0)
        if stars < _MIN_CANDIDATE_STARS:
            continue  # skip 0-star name-match junk — not worth an onboarding proposal
        out.append({
            "kind":     "mcp_registry_candidate",
            "subject":  c["key"],
            "url":      c.get("candidate_url") or c.get("repo_url"),
            # onboard-READY: the finding now carries the exact next step so a human
            # (or the L22 auto-code Issue path, behind AUTO_CODE_DRY_RUN=0) can act
            # without re-investigating. Auto-PR-to-_REGISTRIES stays owner-gated.
            "evidence": (f"NEW MCP registry candidate: {c['key']} "
                          f"(repo {c.get('repo')}, ⭐{stars}). "
                          f"{c.get('description', '')} "
                          f"ONBOARD: if it accepts submissions, add a _REGISTRIES entry "
                          f"in routes/mcp_registry_watch.py (method=github_pr|form|refresh_only) "
                          f"+ the submit list. Propose-only until vetted."),
            "severity": "medium" if stars >= 100 else "low",
        })
    return out


# ── Public endpoints ────────────────────────────────────────────

@mcp_registry_watch_bp.route("/api/v1/brain/mcp-registries",
                              methods=["GET"])
def mcp_registries_status():
    """Read-only snapshot of DC Hub's presence across MCP registries.

    Cached at the edge for 1h since the registries themselves only
    update on PR merges. Public — no auth required.
    """
    results = _probe_all_cached()
    present_count = sum(1 for r in results.values() if r["verdict"] == "present")
    # "missing" counts only ACTIONABLE absences (a registry we could
    # actually submit to). Non-actionable surfaces (no PR path, e.g.
    # Cursor) are surfaced separately so the weekly watch workflow,
    # which alarms on missing>0, doesn't nag about un-fixable gaps.
    missing_actionable = sum(
        1 for r in results.values()
        if r["verdict"] != "present" and r.get("actionable", True))
    not_actionable = sum(
        1 for r in results.values()
        if r["verdict"] != "present" and not r.get("actionable", True))
    resp = jsonify({
        "ok":             True,
        "total":          len(results),
        "present":        present_count,
        "missing":        missing_actionable,
        "not_actionable": not_actionable,
        "results":        results,
        "doc":            "https://dchub.cloud/api/v1/brain/mcp-registries",
        "submission_pattern": (
            "Most MCP registries accept submissions via GitHub PR to their "
            "README or registry index. Pattern: fork the repo, add a "
            "DC Hub entry following the existing format, open PR. The "
            "awesome-mcp-pr workflow (already running weekly) handles "
            "punkpeye/awesome-mcp-servers."
        ),
    })
    # r-probecache: the docstring always claimed 1h edge caching but no
    # header was ever set — send it so CF can actually honor it.
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=3600"
    return resp


@mcp_registry_watch_bp.route("/api/v1/admin/brain/mcp-registries/scan",
                              methods=["POST"])
def mcp_registries_scan():
    """Trigger a fresh scan + file brain findings for any missing registries.

    Admin-key gated (fail-closed): if DCHUB_ADMIN_KEY env var is unset,
    returns 503. Otherwise checks X-Admin-Key header.
    """
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    if not admin_key:
        return jsonify({
            "error": "admin_endpoint_unconfigured",
            "hint":  ("Set DCHUB_ADMIN_KEY env var on Railway to enable. "
                      "Same fail-closed pattern as /api/v1/admin/sitemap/purge."),
        }), 503
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if provided != admin_key:
        return jsonify({"error": "unauthorized"}), 401

    results = _probe_all_cached(force=True)
    findings = _file_findings_for_missing(results)

    # Also hunt for NEW registries we don't yet track and file them as
    # low-severity candidate findings for vetting. Best-effort — never
    # let discovery failure break the audit scan.
    try:
        candidates = _discover_candidate_registries()
        findings.extend(_findings_for_candidates(candidates))
    except Exception:
        candidates = []

    # File findings to brain_findings table if available. Soft-fail —
    # the scan response still succeeds even if the DB write fails.
    filed = 0
    try:
        import psycopg2
        _du = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL", "")).strip()
        if _du and findings:
            with psycopg2.connect(_du, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    # 2026-06-06 fix: the LIVE brain_findings table
                    # (verified via information_schema) has columns
                    # id, issue, url, count, detail, detector, first_seen,
                    # last_seen, created_at, resolved_at, status — and NO
                    # seen_count, despite the repo DDL claiming one. The
                    # old INSERT used finding_kind/subject/evidence (don't
                    # exist) AND filed++ ran inside a swallowed except, so
                    # findings_filed lied and zero rows ever landed. Use
                    # ONLY the known-good columns that exist on the live
                    # table (issue, url, count, detail); plain INSERT (no
                    # ON CONFLICT — live unique constraint unknown; a dup
                    # just rolls back its savepoint). filed++ only on a
                    # real insert. See memory: brain_findings schema note.
                    for f in findings:
                        issue = f"{f['kind']}:{f['subject']}"
                        url = f.get("url") or f"dchub://mcp-registry/{f['subject']}"
                        detail = f"[{f.get('severity','medium')}] {f.get('evidence','')}"
                        # ★2026-08-29 lane 8: canonical writer. It
                        # savepoint-wraps internally and returns the REAL
                        # DB outcome, so filed++ no longer trusts an
                        # external counter that once "lied" while zero
                        # rows landed.
                        from routes.brain_findings_writer import (
                            upsert_brain_finding as _upsert)
                        if _upsert(cur, issue=issue, url=url, count=1,
                                   detail=detail, detector="mcp_registry_watch",
                                   count_kind="occurrence") in ("inserted", "updated"):
                            filed += 1
                conn.commit()
    except Exception as _e:
        note_swallowed_write("brain_findings", where="mcp_registry_watch.mcp_registries_scan")
        pass

    return jsonify({
        "ok":             True,
        "scanned":        len(results),
        "present":        sum(1 for r in results.values()
                              if r["verdict"] == "present"),
        "missing":        sum(1 for f in findings
                              if f["kind"] == "mcp_registry_missing"),
        "new_candidates": sum(1 for f in findings
                              if f["kind"] == "mcp_registry_candidate"),
        "findings_filed": filed,
        "results":        results,
        "findings":       findings,
    })


@mcp_registry_watch_bp.route("/api/v1/brain/mcp-registries/discover",
                              methods=["GET"])
def mcp_registries_discover():
    """Read-only list of candidate NEW MCP registries to vet.

    Public. These are NOT yet audited — they're surfaces discovery found
    that we don't track, ranked by GitHub stars. Add real ones to the
    audited set in routes/mcp_registry_watch.py:_REGISTRIES.
    """
    candidates = _discover_candidate_registries()
    return jsonify({
        "ok":         True,
        "count":      len(candidates),
        "candidates": candidates,
        "note":       ("Candidate new registries for vetting; not yet "
                       "audited. The weekly scan files these as low-severity "
                       "brain findings (kind=mcp_registry_candidate)."),
    })
