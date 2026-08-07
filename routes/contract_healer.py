"""
routes/contract_healer.py — the CONTRACT healer (Shell #44, 2026-08-06).

★ WHY A SECOND HEALER EXISTS, WHEN WE ALREADY HAVE ONE.

dchub_self_heal.py is a VALUE healer. Every detector in it asks one question:
"does this number/page/asset match what canon says it should be?" That question
is answerable only when both sides are present and comparable, and it is the
wrong question for the ten defects found in the week of 2026-08-01. Every one of
those returned HTTP 200 with a structurally well-formed body, and every one would
have passed a value check:

  1. published p50 had NO externality filter — ~80% of its population was our own
     probes; the real median was 786ms slower                        (PR #2252)
  2. retention divided by the CURRENT window under a name that means the PRIOR
     cohort — 14.6% published, 8.4% true                        (task_1cc0f777)
  3. /api/v1/mcp/funnel published THREE "real external weekly" call counts,
     6,997 / 1,567 / 7,159, two near-identically named             (PR #2261)
  4. 30 of 31 /js assets shipped `immutable, max-age=31536000` behind a `?v=`
     token that never moved — every frontend fix reached NEW visitors only
  5. ai.html reads data.recent_activity; /api/ai/tracking stopped returning that
     key, so the live feed renders empty on every load        (task_01b0a3f1)
  6. MCP `initialize` advertised 15,300+ facilities against canon 16,500+ — two
     snapshot files, one refreshed                          (mcp-server #132)
  7. Glama's API returns `tools: []` for us — an empty capability list
  8. registry_truth called our HEALTHIEST listing drifted and an empty-tools
     listing fine — the verdicts were inverted
  9. a shell check returned pass=True at 78.5% concentration under the name
     "no single platform carries reach"
 10. `(s.get("status") or "executed")` — an absent status defaulted to the
     flattering literal, flipping coverage partial -> complete

None of these is a wrong VALUE. They are wrong POPULATIONS (1, 2), wrong
DENOMINATORS (2, 3), absent FIELDS (5, 7), undelivered CONTENT (4), and verdicts
that contradict their own prose (8, 9, 10). So this module asserts INVARIANTS
ACROSS SURFACES, never values against canon, and every failure names THE TWO
THINGS THAT DISAGREE — a finding a reader cannot act on is a finding that gets
muted.

★ REPORT-ONLY, ON PURPOSE. Nothing here writes a fix. When two surfaces publish
different numbers for one quantity, WHICH ONE IS CANONICAL IS NOT MECHANICALLY
DECIDABLE — defect #3's three counts were all correctly computed over three
different populations. An auto-fix would have to pick, and picking wrong
launders a bug into canon. The one class with a mechanically derivable answer
(delivery: a content hash) auto-repairs, and it lives in the frontend repo where
the content is — dchub-frontend/scripts/check-immutable-asset-versions.mjs.

★ THREE-VALUED, WITHOUT EXCEPTION. True / False / None. A fetch that fails
renders '?', never a pass and never a failure. This is not a style preference:
it is the semantic that kept registry drift correctly FALSE when 11 of 16
listings were unreadable, and a shell built THIS WEEK violated it twice before
shipping. UNREADABLE IS NOT DRIFT. Unknown is never rendered as 0.

★ WHAT THIS MODULE DELIBERATELY DOES NOT DO — read this before "improving" it.
The obvious implementation of lane A is: grep dchub-frontend for `data.<field>`
reads, map each to the endpoint that file fetches, assert presence. I built that
and measured it against the live site before writing a line of this module. Four
successively narrower formulations, all of them wolf-criers:

    all snake_case reads, by endpoint-referencing file ....  100+ flags
    reads in a render-GATE position only ................... 45 flags
    gate reads, union of every endpoint the file names ...... 15 flags
    gate reads, single-endpoint files only .................. 4 flags

and the 4-flag version — the only one with a tolerable rate — could not see
defect #5 at all, because ai.html names five endpoints. The 15-flag version's
findings were checked by hand and were false: analytics.html BUILDS an object
named `d` with a `stats_by_region` key client-side (line 504) and then reads it,
and ai-inventory.html's `m.facility_count` reads an array ELEMENT, not a payload.
Static JS binding analysis cannot recover which fetch a variable holds in this
codebase — `const data = await r.json()` sits inside a doubly-nested loop over
two candidate paths — and this repo has already rejected two over-broad scans.

So lane A asserts the contract from BOTH LIVE SIDES instead: the field must be
present in the payload the server returns AND still be read by the page the
server serves. No JS parsing, no repo checkout, no guessing. Its registry is
hand-maintained, which is a real weakness — mitigated by the second direction:
an entry whose page stopped reading the field FAILS as a stale entry, so the
registry cannot quietly rot into fiction the way a one-directional watch list
does. Add an entry when an incident teaches you a surface depends on a field.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

contract_healer_bp = Blueprint("contract_healer", __name__)

SHELL_ID = 44
SHELL_NAME = "Contract Healer"

BASE = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud").rstrip("/")

# Our own probe identity. The de-loop predicates exclude this UA from every
# "external" population by design — which is the point of defect #1. Naming
# ourselves honestly here is what makes that exclusion work.
UA = ("Mozilla/5.0 (compatible; DCHubContractHealer/1.0; "
      "+https://dchub.cloud/.well-known/ai-agents.json)")

_FETCH_TIMEOUT = int(os.environ.get("CONTRACT_HEALER_TIMEOUT") or 25)


def _disabled() -> bool:
    return (os.environ.get("CONTRACT_HEALER_DISABLE") or "0") == "1"


def _admin_ok() -> bool:
    want = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


# ── check records: three-valued, no exceptions ───────────────────────────────
def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    """passed is True / False / None. None means NOT MEASURED — it is never a
    pass and never a failure, and it renders '?'. Same shape the master shells
    use (id/name/pass/detail/critical), so any consumer of those reads these."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:400], "critical": bool(critical)}


def _lane_verdict(checks: list) -> str:
    """INDETERMINATE dominates. A lane with an unmeasured check has not been
    measured, and calling it PASSED is the exact lie this module exists to stop."""
    if not checks:
        return "INDETERMINATE"
    if any(c["pass"] is None for c in checks):
        return "INDETERMINATE"
    if any(c["pass"] is False and c["critical"] for c in checks):
        return "FAILED"
    if any(c["pass"] is False for c in checks):
        return "DEGRADED"
    return "PASSED"


# ── fetch: every failure is None, never a value ──────────────────────────────
def _fetch(url: str, accept: str = "application/json"):
    """Returns (payload_or_text, None) on success, (None, reason) on failure.

    A non-2xx, a timeout, a connection reset and unparseable JSON are ALL
    'reason' — the caller must render '?'. Nothing here ever substitutes a
    default value for a failed read.

    `requests`, not urllib, per the urllib-request-on-railway lint rule.
    fix_api_contract_scan works around that rule with an import alias on the
    grounds that a self-probe is not a production data fetch; that reasoning is
    sound but the workaround is a lint dodge, and this module has no reason to
    need one — requests is already a direct dependency and gives us connection
    pooling across the ~8 fetches a run makes.
    """
    import requests
    full = url if url.startswith("http") else BASE + url
    try:
        r = requests.get(full, headers={"User-Agent": UA, "Accept": accept},
                         timeout=_FETCH_TIMEOUT)
    except Exception as e:
        return None, f"{type(e).__name__}"
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    if accept == "application/json":
        try:
            return r.json(), None
        except Exception:
            return None, "non-JSON body"
    return r.text, None


def _keys_deep(obj, acc: set) -> set:
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            _keys_deep(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _keys_deep(v, acc)
    return acc


def _collect(obj, key: str, path: str = "", out=None) -> list:
    """Every (json_path, value) where `key` appears at ANY depth.

    Depth-blind on purpose. Pinning a path like /sections[1]/metrics/... makes
    the check break when a report is restructured, and a check that breaks on a
    refactor gets deleted rather than fixed. It also means the SAME payload
    publishing one quantity twice is caught — which is literally defect #3.
    """
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            if k == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((p, v))
            _collect(v, key, p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect(v, key, f"{path}[{i}]", out)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# LANE A — FIELD EXISTENCE  (catches #5: a read of a key the payload lacks)
# ═════════════════════════════════════════════════════════════════════════════
# Both sides are fetched LIVE. `page` is the URL a browser actually loads, not a
# repo path: what is SERVED is what the reader gets, and the two differ whenever
# a deploy is mid-flight or an edge cache is stale (see class D).
#
# Seeded from the incident. Add an entry when an incident proves a surface
# depends on a field — not speculatively.
FIELD_CONTRACTS = [
    {"page": "/ai.html", "endpoint": "/api/ai/tracking", "field": "recent_activity",
     "why": "task_01b0a3f1 — the live activity feed is gated on this key; absent, "
            "the feed renders empty on every load, forever, with both sides 200"},
    {"page": "/agent-dashboard.html", "endpoint": "/api/ai/tracking",
     "field": "recent_activity",
     "why": "task_01b0a3f1 — same key, second consumer; dashboardData.recentActivity"},
    {"page": "/for-ai.html", "endpoint": "/api/ai/tracking", "field": "recent_activity",
     "why": "task_01b0a3f1 — same key, third consumer; masterData.feed"},
]


def _lane_field_existence() -> list:
    checks = []
    payload_cache: dict = {}
    page_cache: dict = {}
    for con in FIELD_CONTRACTS:
        ep, page, field = con["endpoint"], con["page"], con["field"]
        cid = f"A.{field}@{page.strip('/')}"
        name = f"{page} reads a field {ep} still publishes"

        if ep not in payload_cache:
            payload_cache[ep] = _fetch(ep)
        payload, perr = payload_cache[ep]
        if page not in page_cache:
            page_cache[page] = _fetch(page, accept="text/html")
        html, herr = page_cache[page]

        if perr or herr:
            # UNREADABLE IS NOT DRIFT. One side missing means the invariant was
            # not evaluated — say so, and say which side.
            why = f"{ep} unreadable ({perr})" if perr else f"{page} unreadable ({herr})"
            checks.append(_check(cid, name, None,
                                 f"NOT MEASURED — {why}. No verdict on '{field}'.",
                                 critical=False))
            continue

        in_payload = field in _keys_deep(payload, set())
        # Word-boundary match so `recent_activity` does not match on a longer
        # identifier that merely contains it.
        reads_it = re.search(r"\b" + re.escape(field) + r"\b", html) is not None

        if reads_it and not in_payload:
            checks.append(_check(
                cid, name, False,
                f"DEAD SURFACE. {BASE}{page} reads '{field}'; {BASE}{ep} does not "
                f"publish '{field}' at any depth (it publishes "
                f"{len(_keys_deep(payload, set()))} other keys). Both return 200, "
                f"so nothing else notices. {con['why']}",
                critical=True))
        elif not reads_it and in_payload:
            # The other direction, and the reason this registry cannot rot: an
            # entry that names a consumer which no longer reads the field is
            # stale, and a stale entry is a check that can only ever pass.
            checks.append(_check(
                cid, name, False,
                f"STALE CONTRACT ENTRY. {BASE}{ep} publishes '{field}', but "
                f"{BASE}{page} no longer reads it — prune this entry or repoint "
                f"it at the page that does. A registry entry nobody consumes is "
                f"a check that cannot fail.",
                critical=False))
        elif not reads_it and not in_payload:
            checks.append(_check(
                cid, name, False,
                f"STALE CONTRACT ENTRY. Neither {BASE}{page} nor {BASE}{ep} "
                f"mentions '{field}' any more — the dependency this entry "
                f"records no longer exists. Prune it.",
                critical=False))
        else:
            checks.append(_check(
                cid, name, True,
                f"{BASE}{page} reads '{field}' and {BASE}{ep} publishes it."))
    return checks


# ═════════════════════════════════════════════════════════════════════════════
# LANE B — ONE QUANTITY, ONE NUMBER  (catches #2, #3, #6, #7)
# ═════════════════════════════════════════════════════════════════════════════
# A quantity published on more than one surface must read the same on all of
# them. `canonical` names which surface wins when they disagree — the healer
# does NOT pick for you (see the report-only note at the top), it just tells you
# which one the registry says is authoritative so the argument is short.
#
# ★ TOLERANCE IS RELATIVE, AND THAT IS NOT SLOPPINESS. These windows are
#   "rolling, ending now": the two publishers are fetched seconds apart, and a
#   call arriving between the two fetches moves one and not the other. The first
#   run of this lane duly reported 6011 vs 6012 as a DISAGREEMENT — a guard that
#   fires on the passage of time is a guard that gets muted within a day. A
#   percentage band absorbs fetch skew and still catches the defects that
#   motivated the lane by three orders of magnitude: defect #3 was 4.5x, and
#   defect #2 was 14.6% vs 8.4%, i.e. 74% apart.
#   Set tolerance_pct to 0 only for a quantity that is genuinely frozen between
#   reads (a snapshot, a canon constant), never for a live counter.
QUANTITIES = [
    {"key": "real_external_calls_7d",
     "publishers": ["/api/v1/mcp/funnel", "/api/v1/reports/agent-success"],
     "canonical": "/api/v1/mcp/funnel",
     "tolerance_pct": 1.0,
     "note": "PR #2261 — the funnel once published three near-identically named "
             "weekly external call counts differing 4.5x and pointing in "
             "opposite directions. Both surfaces now read one query; this "
             "asserts they still do."},
    {"key": "real_external_agents_7d",
     "publishers": ["/api/v1/mcp/funnel", "/api/v1/reports/agent-success"],
     "canonical": "/api/v1/mcp/funnel",
     "tolerance_pct": 1.0,
     "note": "the agents twin of the above; calls_per_active_agent divides one "
             "by the other, so a drift here silently rescales that ratio."},
    {"key": "generic_bucket_share_7d",
     "publishers": ["/api/v1/reports/agent-success"],
     "canonical": "/api/v1/reports/agent-success",
     "tolerance_pct": 1.0,
     "note": "published at two depths of ONE payload (the per-platform gate and "
             "the measurement-integrity trend). The expansion tick and this "
             "report disagreed 21.6% vs 25.1% until both were pointed at "
             "measure_generic_bucket_share(); a same-payload split is how that "
             "starts again."},
]


def _lane_one_quantity() -> list:
    checks = []
    cache: dict = {}
    for q in QUANTITIES:
        key, tol_pct = q["key"], q["tolerance_pct"]
        cid = f"B.{key}"
        name = f"one quantity, one number: {key}"
        seen, unreadable = [], []
        for url in q["publishers"]:
            if url not in cache:
                cache[url] = _fetch(url)
            payload, err = cache[url]
            if err:
                unreadable.append(f"{url} ({err})")
                continue
            for path, val in _collect(payload, key):
                seen.append((url, path, val))

        if unreadable:
            # A publisher we could not read is a publisher we did not compare.
            # Agreement among the REMAINING surfaces is not agreement.
            checks.append(_check(
                cid, name, None,
                f"NOT MEASURED — could not read {', '.join(unreadable)}. "
                f"{len(seen)} value(s) readable elsewhere; agreement among a "
                f"subset is not agreement.", critical=False))
            continue
        if len(seen) < 2:
            checks.append(_check(
                cid, name, None,
                f"NOT MEASURED — '{key}' found on {len(seen)} surface(s) "
                f"({', '.join(p for _, p, _ in seen) or 'none'}). A quantity "
                f"published once cannot disagree with itself; if it should "
                f"appear on more surfaces, the publisher renamed it.",
                critical=False))
            continue

        lo = min(v for _, _, v in seen)
        hi = max(v for _, _, v in seen)
        spread = hi - lo
        # Relative to the larger magnitude, so a quantity legitimately at or
        # near zero cannot divide by it and cannot be declared infinitely
        # divergent. A 0-vs-0 pair is agreement; a 0-vs-anything pair is not.
        scale = max(abs(lo), abs(hi))
        drift_pct = (100.0 * spread / scale) if scale else 0.0
        if drift_pct > tol_pct:
            lo_at = next(f"{u}{p}" for u, p, v in seen if v == lo)
            hi_at = next(f"{u}{p}" for u, p, v in seen if v == hi)
            ratio = (hi / lo) if lo else None
            checks.append(_check(
                cid, name, False,
                f"DISAGREEMENT on '{key}': {lo:g} at {BASE}{lo_at} vs {hi:g} at "
                f"{BASE}{hi_at}"
                + (f" ({ratio:.2f}x)" if ratio else "")
                + f" — {drift_pct:.2f}% apart, tolerance {tol_pct:g}%. Registry "
                  f"says canonical is {q['canonical']} — but WHICH IS RIGHT IS A "
                  f"JUDGEMENT CALL, not a repair: {q['note']}",
                critical=True))
        else:
            checks.append(_check(
                cid, name, True,
                f"'{key}' agrees across {len(seen)} publication point(s): "
                f"{lo:g}..{hi:g} ({drift_pct:.2f}% <= {tol_pct:g}%)."))
    return checks


# ═════════════════════════════════════════════════════════════════════════════
# LANE C — POPULATION DECLARED  (catches #1, #10)
# ═════════════════════════════════════════════════════════════════════════════
# routes/canonical_benchmarks.py (PR #2253) established the pattern: the
# published `sql_filters` IS the list joined into the WHERE clause, so the
# declaration cannot disagree with the query — edit a predicate and the payload
# changes in the same edit.
#
# ★ THE PREDICATES ARE IMPORTED AND CALLED, NOT PINNED AS STRINGS. A check in
#   this repo once asserted verdict tokens that existed only as prose in a
#   docstring and reported their absence as a defect. If external_platform_
#   predicate() is edited, this check compares against the NEW text, which is
#   the only way it stays true.
POPULATION_ENDPOINTS = [
    {"url": "/api/v1/reports/canonical-benchmarks",
     "stat": "execute_plan p50",
     "why": "PR #2252 — this statistic published a median over a population "
            "~80% of which was our own probes, under a docstring that claimed "
            "'real external calls'. The filters were the bug, and nothing on "
            "the page let a reader check."},
]

# Statistics that CLAIM externality must say what they excluded, next to the
# number. The funnel publishes `<field>_basis` for exactly this reason.
EXTERNAL_BASIS_FIELDS = [
    {"url": "/api/v1/mcp/funnel", "field": "real_external_agents_7d"},
    {"url": "/api/v1/mcp/funnel", "field": "real_external_calls_7d"},
]

_MIN_BASIS_CHARS = 80


def _external_predicates():
    """The de-loop predicates as SQL text, computed from the canonical module.

    Returns (list_of_sql_strings, None) or (None, reason). Never a literal."""
    try:
        from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    except Exception as e:
        return None, f"mcp_calls_deloop unimportable ({type(e).__name__})"
    try:
        return [external_platform_predicate("platform"),
                real_ua_predicate("user_agent")], None
    except Exception as e:
        return None, f"predicate call raised ({type(e).__name__})"


def _lane_population_declared() -> list:
    checks = []

    # C1 — the statistic carries a population block built from its own filters.
    preds, perr = _external_predicates()
    for spec in POPULATION_ENDPOINTS:
        url, stat = spec["url"], spec["stat"]
        cid = f"C.population@{stat.replace(' ', '_')}"
        name = f"{stat} declares its observation population"
        payload, err = _fetch(url)
        if err:
            checks.append(_check(cid, name, None,
                                 f"NOT MEASURED — {url} unreadable ({err}).",
                                 critical=False))
            continue
        pops = []

        def _find_pop(o, path=""):
            if isinstance(o, dict):
                if "population" in o and isinstance(o["population"], dict):
                    pops.append((path + "/population", o["population"]))
                for k, v in o.items():
                    _find_pop(v, f"{path}/{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    _find_pop(v, f"{path}[{i}]")

        _find_pop(payload)
        if not pops:
            checks.append(_check(
                cid, name, False,
                f"{BASE}{url} publishes '{stat}' with NO population block. "
                f"A reader cannot tell which observations are counted. "
                f"{spec['why']} The pattern to copy is _p50_filters() / "
                f"_p50_population() in routes/canonical_benchmarks.py.",
                critical=True))
            continue
        missing_filters = [p for p, blk in pops
                           if not isinstance(blk.get("sql_filters"), list)
                           or not blk.get("sql_filters")]
        if missing_filters:
            checks.append(_check(
                cid, name, False,
                f"{BASE}{url}: population block(s) at {', '.join(missing_filters)} "
                f"carry no non-empty sql_filters list. A prose description of a "
                f"filter is a SECOND source of truth, and second sources drift — "
                f"that is how the p50 docstring came to claim 'real external "
                f"calls' over an unfiltered population. Publish the list that is "
                f"joined into the WHERE.",
                critical=True))
            continue
        if perr:
            checks.append(_check(
                cid, name, None,
                f"NOT MEASURED (composition) — population block present with "
                f"sql_filters, but the canonical predicates could not be "
                f"computed: {perr}. Presence checked; composition not.",
                critical=False))
            continue
        # Composition: a population claiming externality must be built from the
        # canonical predicates, not from a hand-rolled lookalike.
        bad = []
        for path, blk in pops:
            joined = " AND ".join(str(f) for f in blk.get("sql_filters", []))
            claims_external = "external" in json.dumps(blk).lower()
            if claims_external:
                absent = [p for p in preds if p not in joined]
                if absent:
                    bad.append((path, len(absent)))
        if bad:
            checks.append(_check(
                cid, name, False,
                f"{BASE}{url}: population(s) at "
                f"{', '.join(p for p, _ in bad)} claim EXTERNAL but their "
                f"sql_filters do not contain the canonical predicate text "
                f"returned by mcp_calls_deloop.external_platform_predicate() / "
                f".real_ua_predicate(). A lookalike filter is how ~80% of the "
                f"p50 population turned out to be our own probes.",
                critical=True))
        else:
            checks.append(_check(
                cid, name, True,
                f"{BASE}{url}: {len(pops)} population block(s), each with "
                f"sql_filters; externality claims compose the canonical "
                f"de-loop predicates."))

    # C2 — a number claiming externality states what it excluded, beside itself.
    basis_cache: dict = {}
    for spec in EXTERNAL_BASIS_FIELDS:
        url, field = spec["url"], spec["field"]
        cid = f"C.basis@{field}"
        name = f"{field} publishes its exclusion basis"
        if url not in basis_cache:
            basis_cache[url] = _fetch(url)
        payload, err = basis_cache[url]
        if err:
            checks.append(_check(cid, name, None,
                                 f"NOT MEASURED — {url} unreadable ({err}).",
                                 critical=False))
            continue
        if not _collect(payload, field):
            checks.append(_check(
                cid, name, None,
                f"NOT MEASURED — {BASE}{url} no longer publishes '{field}'; "
                f"nothing to demand a basis for. If it was renamed, repoint "
                f"this entry rather than deleting it.", critical=False))
            continue
        basis_key = f"{field.rsplit('_7d', 1)[0]}_basis"
        found = None
        for k in _keys_deep(payload, set()):
            if k == basis_key:
                found = k
                break

        def _val(o, key):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == key:
                        return v
                    r = _val(v, key)
                    if r is not None:
                        return r
            elif isinstance(o, list):
                for v in o:
                    r = _val(v, key)
                    if r is not None:
                        return r
            return None

        text = _val(payload, basis_key) if found else None
        if isinstance(text, str) and len(text.strip()) >= _MIN_BASIS_CHARS:
            checks.append(_check(
                cid, name, True,
                f"'{field}' carries '{basis_key}' ({len(text)} chars) naming its "
                f"table, window and exclusions."))
            continue
        # One basis may legitimately cover a family. The funnel's
        # real_external_agents_basis documents the CALLS denominator inside
        # itself ("calls counts every real-external call, including CF-POP rows
        # that carry no agent identity") — demanding a separately-named key for
        # that would be pedantry, and pedantry is how a guard earns a mute. What
        # is NOT acceptable is a number with no declaration anywhere.
        sibling = None
        for k in sorted(_keys_deep(payload, set())):
            if not k.endswith("_basis"):
                continue
            v = _val(payload, k)
            if isinstance(v, str) and len(v.strip()) >= _MIN_BASIS_CHARS \
                    and field in v:
                sibling = k
                break
        if sibling:
            checks.append(_check(
                cid, name, True,
                f"'{field}' has no '{basis_key}', but sibling '{sibling}' names "
                f"'{field}' explicitly and declares its population. One basis "
                f"covering a family is fine; no basis is not."))
            continue
        checks.append(_check(
            cid, name, False,
            f"{BASE}{url} publishes '{field}' with NO declared population: no "
            f"'{basis_key}', and no sibling *_basis mentions '{field}'. A number "
            f"that claims 'real external' without naming its table, window and "
            f"exclusions is the shape of defect #3 — three such numbers "
            f"coexisted, differing 4.5x, and each was individually correct over "
            f"its own undeclared population.",
            critical=True))

    # C3 — an absent status must not default to the flattering literal.
    checks.append(_flattering_default_check())
    return checks


# The exact shape of defect #10: `(s.get("status") or "executed")`. An absent
# status became the SUCCESS value, so partial coverage published as complete.
# Neutral defaults ("", "open", "unknown") are correct and are NOT flagged —
# the defect is specifically defaulting to a value that reads as success.
#
# ★ EXECUTION VERDICTS ONLY. The first version of this list also carried
#   "active", "ok", "healthy" and "green" and produced 16 hits, of which 13 were
#   noise: `attrs.get("STATUS") or "active"` in the gas-plant ingester defaults a
#   FACILITY's operational state, and "active" there is a domain value, not a
#   claim that anything ran. Same for the interconnection-queue and
#   facility-page defaults. A guard at 3-real-in-16 is one someone mutes; the
#   distinction that survives is whether the literal asserts SOMETHING HAPPENED.
_FLATTERING = ("executed", "complete", "completed", "done",
               "passed", "success", "succeeded")
_STATUSY = ("status", "state", "verdict", "outcome", "result")
_FLATTERING_RE = re.compile(
    r"""\.get\(\s*(['"])(?P<f>""" + "|".join(_STATUSY) + r""")\1\s*(?:,[^)]*)?\)\s*"""
    r"""or\s*(['"])(?P<d>""" + "|".join(_FLATTERING) + r""")\3""",
    re.IGNORECASE)


def scan_flattering_defaults(root: str) -> list:
    """Every `(x.get("status") or "executed")`-shaped default under `root`.

    Pure and path-taking so the must-fail control can point it at a fixture.
    Returns [(relpath, lineno, text)]."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "node_modules",
                                    ".venv", "venv", "tests")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            # This module QUOTES the defect three times (module docstring, the
            # comment above _FLATTERING, scan_flattering_defaults' own
            # docstring). A guard that reports its own description as a defect
            # is noise of the most corrosive kind — it teaches the reader that
            # the guard is wrong.
            if os.path.abspath(full) == os.path.abspath(__file__):
                continue
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        # Comments and prose cite the bad pattern to WARN about
                        # it — canonical_benchmarks.py:108 documents its own fix
                        # in exactly these words. Only executable code counts.
                        if line.lstrip().startswith("#"):
                            continue
                        m = _FLATTERING_RE.search(line)
                        if m:
                            hits.append((os.path.relpath(full, root), i,
                                         line.strip()[:160]))
            except OSError:
                continue
    return hits


def _flattering_default_check() -> dict:
    cid = "C.flattering_default"
    name = "an absent status never defaults to a success literal"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        hits = scan_flattering_defaults(root)
    except Exception as e:
        return _check(cid, name, None,
                      f"NOT MEASURED — scan raised {type(e).__name__}.",
                      critical=False)
    if hits:
        first = hits[0]
        return _check(
            cid, name, False,
            f"{len(hits)} flattering default(s); first at {first[0]}:{first[1]} "
            f"— `{first[2]}`. An absent status silently became the SUCCESS "
            f"value, which flipped coverage partial -> complete with no error "
            f"anywhere. Default to a NEUTRAL literal ('', 'unknown') or to None "
            f"and let the caller render '?'.",
            critical=True)
    return _check(cid, name, True,
                  f"no `.get('status') or '<success literal>'` defaults under "
                  f"{os.path.basename(root)}/ (neutral defaults are fine and "
                  f"are not flagged).")


# ═════════════════════════════════════════════════════════════════════════════
# LANE E — VERDICT MATCHES PROSE  (catches #8, #9)
# ═════════════════════════════════════════════════════════════════════════════
# Defect #9: a check named "no single platform carries reach" returned pass=True
# at 78.5% against a bar of 90, while its own detail read "Above 25% a WoW built
# on one platform's burst is concentration, not growth." The verdict, the name
# and the prose disagreed, and green was the only part a skimming reader saw.
#
# ★ MINIMUM VIABLE AND DELIBERATELY UNAMBITIOUS. It fires ONLY when all four
#   hold: the name negates, the verdict is True, the detail states a threshold,
#   and the detail states an observed value ABOVE it. A green whose own prose
#   names the bar it just cleared is the whole signal. It does not attempt to
#   parse claims, and it never flags a False or a None — a red check that reads
#   oddly is not a lie, and this repo deletes guards that cry wolf.
_NEGATIONS = ("no single", "no ", "never", "not ", "n't", "zero ", "without ")

# "Above 25%" / "target >0.5%" / "below 25%" / "at most 80%" / "<= 25%".
_THRESHOLD_RE = re.compile(
    r"(?:above|below|under|over|exceed(?:s|ing)?|target|max(?:imum)?|"
    r"at most|no more than|>=|<=|>|<)\s*"
    r"(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
_OBSERVED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def verdict_contradicts_prose(name: str, detail: str, passed):
    """True / False / None for ONE check. Pure — no I/O, no DB, no clock.

    None means the heuristic does not apply (not measurable), which is NOT a
    pass. Returns True only when a passing check's own detail states a bar and
    an observed value above it.
    """
    if passed is not True:
        return None                      # only flattering greens are in scope
    low_name = (name or "").lower()
    if not any(t in low_name for t in _NEGATIONS):
        return None
    d = detail or ""
    thresholds = [float(x) for x in _THRESHOLD_RE.findall(d)]
    if not thresholds:
        return None
    bar = min(thresholds)
    observed = [float(x) for x in _OBSERVED_RE.findall(d)]
    # Values that ARE the threshold restatement are not observations.
    observed = [v for v in observed if v not in thresholds]
    if not observed:
        return None
    return max(observed) > bar


# Shells persist their full payload as JSON. Reading the newest snapshot is
# three SELECTs, versus re-running the shells (minutes, and it would double
# every shell's DB load on a 5-minute cycle).
SHELL_SNAPSHOT_TABLES = ("actuation_shell_snapshots",
                         "inventory_shell_snapshots",
                         "payload_shell_snapshots")


def _ro_conn():
    try:
        import psycopg2
    except Exception:
        return None
    dsn = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not dsn:
        return None
    try:
        c = psycopg2.connect(dsn, connect_timeout=8)
        c.set_session(readonly=True, autocommit=True)
        return c
    except Exception:
        return None


def _iter_checks(payload):
    """Yield every {name, detail, pass}-shaped record in a shell payload."""
    if isinstance(payload, dict):
        if "name" in payload and "detail" in payload and "pass" in payload:
            yield payload
        for v in payload.values():
            yield from _iter_checks(v)
    elif isinstance(payload, list):
        for v in payload:
            yield from _iter_checks(v)


def _lane_verdict_vs_prose(own_checks: list) -> list:
    checks = []

    # E1 — this module audits its own output first. A verdict guard that
    # exempts itself is the defect it is looking for.
    self_bad = [c for c in own_checks
                if verdict_contradicts_prose(c["name"], c["detail"], c["pass"]) is True]
    checks.append(_check(
        "E.self", "this shell's own greens do not contradict their prose",
        len(self_bad) == 0,
        "clean" if not self_bad else
        f"{len(self_bad)} of this shell's own passing checks state a bar and a "
        f"value above it: {[c['id'] for c in self_bad]}", critical=False))

    # E2 — the persisted master-shell snapshots.
    c = _ro_conn()
    if c is None:
        checks.append(_check(
            "E.shells", "master-shell greens do not contradict their prose", None,
            "NOT MEASURED — no read-only database connection; shell snapshots "
            "were not read. Unknown, not clean.", critical=False))
        return checks
    scanned, bad, unreadable = 0, [], []
    try:
        for tbl in SHELL_SNAPSHOT_TABLES:
            try:
                with c.cursor() as cur:
                    # Table name is from a module-level literal tuple, never
                    # from a request — no interpolation of caller input.
                    cur.execute(
                        f"SELECT payload FROM {tbl} ORDER BY id DESC LIMIT 1")
                    row = cur.fetchone()
            except Exception as e:
                unreadable.append(f"{tbl} ({type(e).__name__})")
                continue
            if not row or not row[0]:
                unreadable.append(f"{tbl} (no rows)")
                continue
            payload = row[0]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    unreadable.append(f"{tbl} (unparseable payload)")
                    continue
            for ch in _iter_checks(payload):
                scanned += 1
                if verdict_contradicts_prose(ch.get("name"), ch.get("detail"),
                                             ch.get("pass")) is True:
                    bad.append((tbl, ch.get("name"), (ch.get("detail") or "")[:150]))
    finally:
        try:
            c.close()
        except Exception:
            pass

    if unreadable and not scanned:
        checks.append(_check(
            "E.shells", "master-shell greens do not contradict their prose", None,
            f"NOT MEASURED — no snapshot readable: {', '.join(unreadable)}.",
            critical=False))
        return checks
    if bad:
        t, n, d = bad[0]
        checks.append(_check(
            "E.shells", "master-shell greens do not contradict their prose", False,
            f"{len(bad)} of {scanned} persisted check(s) return pass=True under a "
            f"NEGATING name while their own detail states a bar and a value above "
            f"it. First: {t} — \"{n}\" :: \"{d}\". Either the threshold is wrong "
            f"(defect #9: a 90% bar under a name that means 25%) or the name is. "
            + (f"Not scanned: {', '.join(unreadable)}." if unreadable else ""),
            critical=True))
    else:
        checks.append(_check(
            "E.shells", "master-shell greens do not contradict their prose", True,
            f"{scanned} persisted check(s) scanned; no passing check states a bar "
            f"and a value above it."
            + (f" Not scanned: {', '.join(unreadable)}." if unreadable else "")))
    return checks


# ═════════════════════════════════════════════════════════════════════════════
# runner
# ═════════════════════════════════════════════════════════════════════════════
LANES = [
    ("A", "field existence — the client reads what the server publishes",
     _lane_field_existence),
    ("B", "one quantity, one number", _lane_one_quantity),
    ("C", "population declared", _lane_population_declared),
]


def run_contract_healer() -> dict:
    if _disabled():
        return {"shell": SHELL_NAME, "id": SHELL_ID, "status": "DISABLED"}
    out, verdicts, flat = [], [], []
    for key, title, fn in LANES:
        try:
            cs = fn()
        except Exception as e:
            cs = [_check(f"{key}.err", "lane executed", None,
                         f"NOT MEASURED — lane raised {type(e).__name__}: "
                         f"{str(e)[:140]}", critical=True)]
        flat.extend(cs)
        v = _lane_verdict(cs)
        verdicts.append(v)
        out.append({"lane": key, "title": title, "verdict": v, "checks": cs})

    # Lane E runs last: it audits this shell's own checks alongside the shells'.
    try:
        cs = _lane_verdict_vs_prose(flat)
    except Exception as e:
        cs = [_check("E.err", "lane executed", None,
                     f"NOT MEASURED — lane raised {type(e).__name__}", critical=True)]
    verdicts.append(_lane_verdict(cs))
    out.append({"lane": "E", "title": "verdict matches prose",
                "verdict": _lane_verdict(cs), "checks": cs})

    overall = ("INDETERMINATE" if "INDETERMINATE" in verdicts
               else "FAILED" if "FAILED" in verdicts
               else "DEGRADED" if "DEGRADED" in verdicts else "PASSED")
    all_checks = flat + cs
    return {
        "shell": SHELL_NAME, "id": SHELL_ID, "overall": overall,
        "report_only": True,
        "counts": {
            "pass": sum(1 for c in all_checks if c["pass"] is True),
            "fail": sum(1 for c in all_checks if c["pass"] is False),
            # Never rendered as 0-of-anything. Unmeasured is its own count.
            "not_measured": sum(1 for c in all_checks if c["pass"] is None),
        },
        "lanes": out,
        "note": ("Report-only by design. Which of two disagreeing numbers is "
                 "canonical is a judgement call, not a repair. The one class "
                 "with a derivable answer (delivery) auto-repairs in "
                 "dchub-frontend/scripts/check-immutable-asset-versions.mjs."),
    }


# ── /heal/findings bridge ────────────────────────────────────────────────────
# Same {url, issue, count, detail} shape brain_consistency_radar.scan_all()
# emits, so main.py merges these into actionable_backend_issues with the block
# it already has for that module. Labels start with `contract_` so no FIX_MAP
# key matches them — these are never HTML body substitutions.
_CACHE_TTL = int(os.environ.get("CONTRACT_HEALER_TTL") or 1800)
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "findings": None}
_refreshing = False


def _compute_findings() -> list:
    """Run the lanes and reduce to findings. Blocking; callers choose when."""
    rep = run_contract_healer()
    findings = []
    for lane in rep.get("lanes", []):
        for ch in lane.get("checks", []):
            if ch["pass"] is not False:
                continue
            findings.append({
                "url": f"dchub://contract/{lane['lane']}/{ch['id']}",
                "issue": f"contract_{'critical' if ch['critical'] else 'warn'}: "
                         f"{ch['name']}",
                "count": 1,
                "detail": ch["detail"],
            })
    return findings


def _refresh_async() -> None:
    """Recompute in a daemon thread. At most one in flight."""
    global _refreshing
    with _cache_lock:
        if _refreshing:
            return
        _refreshing = True

    def _run():
        global _refreshing
        try:
            f = _compute_findings()
            with _cache_lock:
                _cache["at"] = time.time()
                _cache["findings"] = f
        except Exception as e:
            logger.warning("[contract-healer] async scan failed: %s", e)
        finally:
            with _cache_lock:
                _refreshing = False

    threading.Thread(target=_run, name="contract-healer-refresh",
                     daemon=True).start()


def scan_all(force: bool = False) -> list:
    """Findings for /heal/findings. NEVER computes on the caller's thread.

    ★ /heal/findings' own docstring is explicit: "NEVER computes synchronously."
      It earned that rule the hard way — the detectors HTTP-crawl this same
      backend, a single hit locked a worker for 42-204s, health checks failed,
      the gunicorn watchdog fired and prod went into a restart loop. These lanes
      make ~8 live fetches, so computing inline would have re-created exactly
      that, in the one handler that already documents the wound.

      Cold cache therefore returns [] and warms in the background. [] here means
      NOT YET COMPUTED, and the distinction is safe in this direction only
      because a finding is a positive assertion of a defect: publishing none
      understates, and the next call publishes them. It must never be read as
      "the contract healer found nothing" — for that, read
      /api/v1/admin/contract-healer, which answers three-valued.

    ★ Only FALSE checks become findings. A None must never enter this list:
      downstream, a finding is a defect, and an unread surface is not a defect.
    """
    now = time.time()
    with _cache_lock:
        have = _cache["findings"]
        stale = have is None or (now - _cache["at"]) >= _CACHE_TTL
    if stale or force:
        _refresh_async()
    return list(have) if have is not None else []


@contract_healer_bp.route("/api/v1/admin/contract-healer", methods=["GET"])
def contract_healer_endpoint():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(run_contract_healer())


def register_contract_healer(app) -> None:
    try:
        app.register_blueprint(contract_healer_bp)
    except Exception as e:
        logger.warning("[contract-healer] blueprint registration skipped: %s", e)
