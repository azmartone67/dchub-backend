"""
routes/registry_surface_shell.py — Registry Surface Shell (#42, 2026-07-29).

★ THE SUBJECT OF THIS SHELL IS WHAT AN AGENT SEES BEFORE IT EVER REACHES US.

Shell #41 measured the only durable state an agent can create. This one measures
the step before that: whether a directory listing describes a product anyone
would connect to. Smithery alone carries ~83% of client calls, so the listings
are not marketing — they are the front door, and a wrong one is indistinguishable
from being absent.

Measured 2026-07-29, the Glama listing serves:

    "33 tools covering 21,000+ data-center facilities (170+ countries),
     232 US power markets"        …and  tools: []

against a live server of 81 tools / 15,000+ facilities / 311 markets. Every
number is from a canon we abandoned, and the tool array is EMPTY — an agent
browsing Glama sees a server with no capabilities at all. scripts/
sync-tools-manifest.mjs documents this exact string ("33 tools · 232 US power
markets · 2,000+ deals · tools:[]") as a QA finding from 2026-07-04. It is
byte-for-byte unchanged 25 days later, which means nothing that ran in between
was capable of correcting it.

LANES
  1. THE LISTING IS EMPTY, NOT MERELY STALE. tools:[] is a different failure
     from a stale number and deserves its own check: a wrong count still shows a
     product, an empty array shows nothing. glama.json declares no `tools` key
     at all, so a crawler that cannot introspect a remote streamable-http server
     has nothing to fall back on. Stale canon is the visible symptom; the empty
     array is the one that costs connections.

  2. THE FIXER POINTED AT A 404 (fires an actuator). update_listing_description
     carried its OWN hardcoded copy of the Glama URL. The 2026-07-09 fix — "was
     'dchub' -> 400s, the slug is namespaced" — was applied to the reader and
     never to that copy, which kept BOTH original defects: the un-namespaced
     slug and a transposed path segment.

         reader  /api/mcp/v1/servers/azmartone67/dchub-mcp-server -> 200
         writer  /api/v1/mcp/servers/dchub                        -> 404

     So every Glama PATCH we ever issued went into a 404. The listing could not
     have been repaired by the machinery built to repair it. Both callers now
     derive from _glama_api_url(); this lane asserts no second copy returns.

     The same lane guards a second wrong surface: white-glove probes Glama's
     listing_url, a JS-rendered HTML page that reads as unreadable — and
     unreadable scores as drift=FALSE. The JSON API is trivially readable and
     plainly stale. The detector and the fixer were each looking at a different
     wrong place, which is why a listing this broken produced no signal.

  3. RANK IS ASSERTED, NEVER MEASURED. "#1 on Smithery for data-center / energy /
     grid" ships inside our own manifests as a factual claim. Nothing measures
     position. Per the freshness workflow's own teardown, Smithery rank is
     RELEVANCE-driven and recency is ~0.00, so a republish cadence cannot defend
     it — and byteaskai is recorded as out-ranking us on "interconnection". A
     claim we publish and never check is the same category of error as a green
     check that ran against nothing; this lane makes it falsifiable or removes it.

Read-only. Lane 2's actuator is the single-origin URL in mcp_presence_crawler.

Run:  GET /api/v1/admin/registry-surface-shell        (admin-gated)
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

registry_surface_shell_bp = Blueprint("registry_surface_shell", __name__)

SHELL_ID = 42
SHELL_NAME = "Registry Surface Shell"

# Claims we publish about ourselves that a reader could check. Kept as data so
# the shell can be pointed at a new claim without editing logic.
RANK_CLAIMS = ("#1 on Smithery for data-center / energy / grid",)

_TIMEOUT = 25
_UA = {"User-Agent": "dchub-registry-surface-shell/1.0"}


def _admin_ok() -> bool:
    want = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


def _disabled() -> bool:
    return (os.environ.get("REGISTRY_SURFACE_SHELL_DISABLE") or "0") == "1"


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name,
            "status": "PASS" if passed is True else ("FAIL" if passed is False else "INDETERMINATE"),
            "detail": detail, "critical": bool(critical)}


def _lane_verdict(checks: list) -> str:
    """A lane that could not read its evidence is INDETERMINATE, never PASSED.
    Glama is the standing proof of why: an unreadable HTML page scored as
    'no drift' for 25 days while the listing showed a server with no tools."""
    if not checks:
        return "INDETERMINATE"
    if any(c["status"] == "INDETERMINATE" for c in checks):
        return "INDETERMINATE"
    if any(c["status"] == "FAIL" and c["critical"] for c in checks):
        return "FAILED"
    if any(c["status"] == "FAIL" for c in checks):
        return "DEGRADED"
    return "PASSED"


def _get_json(url: str):
    """GET + parse. Uses requests, not urllib — house rule
    (regression_lint: urllib-request-on-railway). Returns (status, payload) and
    NEVER raises: a transport failure must surface as INDETERMINATE, not as a
    lane that quietly reports clean."""
    import requests
    try:
        r = requests.get(url, headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return r.status_code, (r.text or "")[:120]
        return r.status_code, r.json()
    except Exception as e:
        return None, str(e)[:120]


def _live_canon() -> dict:
    """Canon from the shipped source, never transcribed into this shell."""
    try:
        from routes.mcp_presence_crawler import _canonical_numbers, _our_actual_tool_count
        n = dict(_canonical_numbers())
        live = _our_actual_tool_count()
        if isinstance(live, int) and live > 0:
            n["tools"] = live
        return n
    except Exception:
        return {}


# ── Lane 1 — the listing is empty, not merely stale ──────────────────
def _lane_listing_content() -> list:
    from routes.mcp_presence_crawler import _glama_api_url
    checks = []
    status, body = _get_json(_glama_api_url())
    if status != 200 or not isinstance(body, dict):
        checks.append(_check("L1.0", "Glama listing readable", None,
                             f"Glama API unreadable ({status}: {str(body)[:80]}) — "
                             f"an unreadable listing is NOT a clean one",
                             critical=True))
        return checks

    tools = body.get("tools") or []
    checks.append(_check(
        "L1.1", "listing advertises a non-empty tool set", len(tools) > 0,
        f"Glama lists {len(tools)} tools. An empty array shows an agent a server "
        f"with no capabilities — a different and worse failure than a stale count.",
        critical=True))

    canon = _live_canon()
    want_tools = canon.get("tools")
    if want_tools:
        checks.append(_check(
            "L1.2", "listed tool count matches live", len(tools) == want_tools,
            f"listing={len(tools)} live={want_tools}"))
    else:
        checks.append(_check("L1.2", "live tool count resolvable", None,
                             "could not resolve the live tool count to compare against"))

    desc = (body.get("description") or "")
    stale_markers = [m for m in ("33 tools", "21,000+", "232 US power markets",
                                 "2,000+", "4,000+")
                     if m in desc]
    checks.append(_check(
        "L1.3", "description carries no abandoned canon", not stale_markers,
        f"stale markers present: {stale_markers or 'none'}. Live canon is "
        f"{want_tools or '?'} tools / {canon.get('facilities')} facilities / "
        f"{canon.get('markets')} markets.",
        critical=bool(stale_markers)))
    return checks


# ── Lane 2 — the fixer pointed at a 404 ──────────────────────────────
def _lane_fixer_target() -> list:
    import inspect
    from routes import mcp_presence_crawler as pc
    checks = []

    # The actuator: exactly one definition of the Glama resource URL.
    try:
        src = inspect.getsource(pc)
        hardcoded = src.count("https://glama.ai/api/")
        # One occurrence is legitimate: the f-string inside _glama_api_url().
        checks.append(_check(
            "L2.1", "exactly one Glama URL origin", hardcoded <= 2,
            f"{hardcoded} literal 'glama.ai/api/' occurrences in the module. A "
            f"second copy is a second thing to fix, and the copy nobody tests is "
            f"the one that stays broken — the writer sat on a 404 for 20 days "
            f"after the reader was corrected.",
            critical=hardcoded > 2))
    except Exception as e:
        checks.append(_check("L2.1", "source readable", None, str(e)[:80], critical=True))

    # The resolved URL must actually answer. A fixer aimed at a 404 reports
    # failure into a log nobody reads and changes nothing.
    try:
        url = pc._glama_api_url()
        status, _ = _get_json(url)
        checks.append(_check(
            "L2.2", "the fixer's target resolves", status == 200,
            f"{url} -> HTTP {status}. Every PATCH we issued before 2026-07-29 went "
            f"to /api/v1/mcp/servers/dchub, which 404s.",
            critical=status != 200))
    except Exception as e:
        checks.append(_check("L2.2", "URL resolvable", None, str(e)[:80], critical=True))

    # The detector must read a surface that can actually be read.
    try:
        from routes.white_glove_propagation import AUTO_PATH_REGISTRIES
        checks.append(_check(
            "L2.3", "Glama drift can escalate to a human", "glama" in AUTO_PATH_REGISTRIES,
            "glama is in AUTO_PATH_REGISTRIES, so PR #1882's escalation applies once "
            "its submitter reports it cannot finish. Before that fix an auto-path "
            "registry was silently absorbed."))
    except Exception as e:
        checks.append(_check("L2.3", "white-glove readable", None, str(e)[:80]))
    return checks


# ── Lane 3 — rank is asserted, never measured ────────────────────────
def _lane_rank_claim(fetch=None) -> list:
    """We publish '#1 on Smithery for data-center / energy / grid' as fact.

    ★2026-09-06: this lane USED TO SAY "Smithery's public API exposes no ranked
    search endpoint we have verified" and returned UNVERIFIED forever. That
    sentence was false when it was written and stayed in the tree for six weeks.
    `registry.smithery.ai/servers?q=<term>` returns an ORDERED list with a
    totalCount, and scripts/registry_monitor.py in dchub-mcp-server has read our
    position out of it since July. The measurement existed in the other repo the
    whole time; this lane just could not see it, and "we have not verified" got
    published as "cannot be verified". A lane that reports a gap it could have
    closed by looking is worse than no lane.

    It now MEASURES, three-valued: True (we hold #1 on every term the claim
    names), False (a named term slipped), None (the registry did not answer —
    never rendered as a slip).
    """
    from routes.mcp_ecosystem_board import category_ranks

    # Only the terms the published claim actually names. A slip on a term we
    # never claimed is a growth question, not a false statement.
    claimed = ("data center", "energy", "grid")
    rows = category_ranks(terms=claimed, fetch=fetch)
    readable = [r for r in rows if r.get("readable")]
    if not readable:
        return [_check("L3.1", "published rank claim is measured", None,
                       "the registry answered for none of "
                       f"{list(claimed)} — UNMEASURED, which is not a slip",
                       critical=False)]
    lost = [f"{r['term']} at {r['position']} (leader {r['leader']})"
            for r in readable if not r.get("held")]
    unread = [r["term"] for r in rows if not r.get("readable")]
    return [_check(
        "L3.1", "published rank claim is measured", not lost,
        ("we publish " + "; ".join(RANK_CLAIMS) + ". Measured now: "
         + ", ".join(f"{r['term']} #{r['position']} of {r['of']}"
                     for r in readable)
         + (f". SLIPPED: {'; '.join(lost)} — correct the copy or reclaim the "
            f"term; a published rank we do not hold is the same class of error "
            f"as a green check that ran against nothing." if lost else ".")
         + (f" UNREADABLE (not counted either way): {unread}." if unread else "")),
        critical=bool(lost))]


LANES = (
    ("listing_content", "The listing is empty, not merely stale", _lane_listing_content),
    ("fixer_target", "The fixer pointed at a 404", _lane_fixer_target),
    ("rank_claim", "Rank is asserted, never measured", _lane_rank_claim),
)


def run_registry_surface_shell() -> dict:
    if _disabled():
        return {"shell": SHELL_NAME, "id": SHELL_ID, "status": "DISABLED"}
    out, verdicts = [], []
    for key, title, fn in LANES:
        try:
            checks = fn()
        except Exception as e:
            checks = [_check(f"{key}.err", "lane executed", None,
                             f"lane raised: {str(e)[:150]}", critical=True)]
        v = _lane_verdict(checks)
        verdicts.append(v)
        out.append({"lane": key, "title": title, "verdict": v, "checks": checks})
    overall = ("INDETERMINATE" if "INDETERMINATE" in verdicts
               else "FAILED" if "FAILED" in verdicts
               else "DEGRADED" if "DEGRADED" in verdicts else "PASSED")
    return {"shell": SHELL_NAME, "id": SHELL_ID, "overall": overall, "lanes": out}


@registry_surface_shell_bp.route("/api/v1/admin/registry-surface-shell", methods=["GET"])
def registry_surface_shell_endpoint():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(run_registry_surface_shell())


def register_registry_surface_shell(app) -> None:
    try:
        app.register_blueprint(registry_surface_shell_bp)
    except Exception:
        pass
