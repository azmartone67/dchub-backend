#!/usr/bin/env python3
"""Surface 6 — RETRIEVAL and LATENCY, the two things nothing watches from outside.

The board is green on 26 checks and has never once asked the two questions a
paying agent actually cares about: *did the search find the thing that exists?*
and *did it answer before the caller gave up?*

Both are dangerous to check badly, and this file is written around that.

## Retrieval: ground truth from the platform's OWN index, never from a judge

"Is this answer relevant?" needs a judge, and a judge invents a target — the
thing rule 3 forbids. So this probe never grades relevance. It asks a strictly
weaker, decidable question:

    Take an entity the platform's own DETERMINISTIC lookup returns. Search for
    it by name through the semantic door. Does the corpus admit it exists?

That is not a quality bar someone made up: the ground truth is the platform's
own `search_facilities` result, from the same seat, in the same run. If a
deterministic lookup finds "Equinix DC12" and the semantic door returns zero
results for that exact name, retrieval is broken by the platform's own account —
no relevance judgment required. If the semantic door returns SOMETHING but not
that entity, this is a GAUGE, not a RED: ranking is a judgment call and we
promised not to make it.

## Citations: a contract the platform publishes about itself

The MCP server's own instructions advertise "live, **cited** ground truth", and
the tools return a `citation` field. So "a retrieval answer carrying zero
citations anywhere in its envelope" is measurable against a claim the platform
makes, not a bar this probe invented. Partial citation coverage is a GAUGE.

## Latency: the only hard threshold here belongs to the edge, not to me

No opinion about what "fast" means. But the Cloudflare zone applies a **15s
route timeout** to this platform's own paths, and beyond it the caller gets a
503 or a failover to a stale origin — so exceeding the platform's own edge
budget is a defect by construction, and everything else is reported as a number
with no claim attached.

## The seat is not a detail — it decides whether the question is answerable

Retrieval is judged from the **paid** seat. Measured live 2026-08-08: an anon
`semantic_search` returns ONE row plus `_results_total_in_pro=8` — the platform
openly withholding the rest. Recall cannot be judged from a set someone else
truncated, so running this lane on anon would publish a permanent BLIND that
reads like a broken probe, or worse, a permanent "not found" that reads like a
broken index. With no paid credential the honest output is "we could not sit in
that seat", and no call is spent.

## Three ways this probe refuses to lie

* **A trimmed result set is BLIND, not RED.** Either an explicit preview flag
  or a withheld-remainder count means what came back measures the paywall, not
  the index.
* **An unreadable row shape is BLIND, not a miss.** The first draft read
  top-level `name` only, got `[]` from rows that carry the name in `cite.name`,
  and published "the entity was not among the returned names" — a claim about
  the platform that was really a fact about the probe (shell #49, verbatim).
* **A slow FIRST call is not a slow endpoint.** Cold caches and warmers make
  single measurements noise, so latency is the MEDIAN of three, and the spread
  is published in the evidence so a reader can see when the median is hiding
  something.
"""
from __future__ import annotations

import statistics
import time

from . import config as C
from .finding import (BLIND, GAUGE, INFO, MAJOR, PASS, RED, SEAT_NONE,
                      SEAT_PAID, Finding, blind, stable_key)
from .http import MCPSession, Unreachable, envelope_all, fetch

# ── retrieval doors ─────────────────────────────────────────────────────────
# The deterministic door that supplies ground truth, and the semantic doors it
# is used to judge. Both are the platform's own tools; nothing external.
GROUND_TRUTH_TOOL = "search_facilities"
GROUND_TRUTH_ARGS = {"query": "ashburn", "limit": 5}
SEMANTIC_TOOLS = ["semantic_search", "search_intelligence"]

# Field names the platform uses for a retrieved entity's display name. Checked
# in this order; the first present wins.
#
# ★ These are checked on the row AND inside its nested containers. Measured
# live 2026-08-08: a `semantic_search` row is
# {cosine, kind, source_id, source_table, text, cite:{name, provider, market,
# location, url}} — the name is in `cite.name` and in the `text` blob, and
# NOWHERE at top level. The first draft of this probe read top-level only, got
# [], and published "the entity was not among the returned names" — a claim
# about the platform that was really a fact about the probe. That is shell
# #49's error verbatim (an absence proven by reading the wrong field), so
# unreadable names are now BLIND, never a miss.
NAME_FIELDS = ("name", "facility_name", "title", "site_name", "slug")
NAME_CONTAINERS = ("cite", "citation", "metadata", "meta", "entity", "_entity")
# Free-text blobs a name may be embedded in when no structured field carries it.
TEXT_FIELDS = ("text", "snippet", "content", "chunk")

# ★ The edge's OWN budget, not an invented one: the CF worker applies a 15s
# route timeout (ROUTE_TIMEOUTS DEFAULT), past which a caller gets a 503 or a
# failover to the stale origin. A public GET slower than this is broken by the
# platform's own configuration.
EDGE_TIMEOUT_S = 15.0

# Public read paths worth timing. Deliberately GET-only and public: an admin POST
# has a different budget and a different failure mode.
LATENCY_PATHS = [
    "/api/v1/stats",
    "/api/v1/heal/findings",
    "/api/v1/data-freshness",
]
LATENCY_SAMPLES = 3

_PREVIEW_MARKERS = ("preview_is_partial", "trial_preview", "_gated",
                    "paid_only", "upgrade_required")

# ★ Fields that announce a WITHHELD remainder ("N more in Pro"). Measured live
# 2026-08-08: an anon `semantic_search` returns ONE row alongside
# `_results_total_in_pro`, i.e. the platform is telling the caller there are
# more results it is not showing. A "miss" inside a set trimmed to one row
# measures the paywall, not recall — and a check that can only ever say "not
# found" is the permanent-false-positive shape that trains readers to skip the
# line, including the half that is real. So a trimmed result set is BLIND for
# every recall question.
_WITHHELD_MARKERS = ("_results_total_in_pro", "_corpus_total_in_pro",
                     "_recent_facilities_total_in_pro")


# ── helpers ─────────────────────────────────────────────────────────────────

def _rows(env: dict) -> list:
    """Best-effort list of result rows from a tool envelope.

    Looks in structuredContent first (where the data lives) and accepts the
    common container names. Returns [] when the shape is unrecognised — the
    caller must treat that as *unobserved*, never as "no results".
    """
    sc = env.get("structuredContent")
    if not isinstance(sc, dict):
        return []
    for key in ("results", "facilities", "matches", "items", "hits",
                "records", "data"):
        v = sc.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
    return []


def _names(rows: list) -> list[str]:
    """Display names, read from the row, its nested containers, then nothing.

    Deliberately does NOT fall back to the free-text blob — see `_mentions`.
    An empty return means "no structured name was readable", which the caller
    must treat as unobserved.
    """
    out = []
    for r in rows:
        found = None
        for f in NAME_FIELDS:
            v = r.get(f)
            if isinstance(v, str) and v.strip():
                found = v.strip()
                break
        if found is None:
            for c in NAME_CONTAINERS:
                sub = r.get(c)
                if not isinstance(sub, dict):
                    continue
                for f in NAME_FIELDS:
                    v = sub.get(f)
                    if isinstance(v, str) and v.strip():
                        found = v.strip()
                        break
                if found:
                    break
        if found:
            out.append(found)
    return out


def _mentions(rows: list, target: str) -> bool:
    """True when any row's free text names the target.

    Kept separate from `_names` on purpose: a hit here proves the corpus
    returned the entity even when the row exposes no structured name field, so
    it can rescue a TRUE positive — but it is too loose to prove a MISS, which
    is why the miss path requires readable names instead.
    """
    t = target.lower()
    for r in rows:
        for f in TEXT_FIELDS:
            v = r.get(f)
            if isinstance(v, str) and t in v.lower():
                return True
    return False


def _looks_previewed(env: dict) -> tuple[bool, str]:
    """(trimmed?, which field said so).

    Two ways the platform admits it withheld results: an explicit preview flag,
    or a sales field counting the remainder. Either one makes every recall
    question unanswerable from this seat.
    """
    sc = env.get("structuredContent")
    if isinstance(sc, dict):
        for m in _PREVIEW_MARKERS:
            if sc.get(m):
                return True, m
        for m in _WITHHELD_MARKERS:
            v = sc.get(m)
            if isinstance(v, (int, float)) and v > 0:
                return True, f"{m}={v}"
            if v:
                return True, m
    return False, ""


def _cited(rows: list, env: dict) -> tuple[int, int]:
    """(rows carrying a citation/source, rows examined).

    A citation counts when it is on the row OR when the envelope carries one
    that covers the answer — the platform does both, and demanding per-row
    citations would invent a stricter contract than the one it publishes.
    """
    if not rows:
        return 0, 0
    per_row = sum(1 for r in rows
                  if any(r.get(k) for k in ("citation", "source", "source_url",
                                            "sources", "cite", "url")))
    if per_row:
        return per_row, len(rows)
    sc = env.get("structuredContent")
    if isinstance(sc, dict) and any(
            sc.get(k) for k in ("citation", "sources", "citations")):
        return len(rows), len(rows)
    return 0, len(rows)


def _median_ms(samples: list[float]) -> int:
    return int(statistics.median(samples) * 1000)


# ── the checks ──────────────────────────────────────────────────────────────

def _check_retrieval(out: list) -> None:
    # ★ SEAT CHOICE IS THE WHOLE CHECK. Measured live 2026-08-08: the anon seat
    # receives ONE row plus `_results_total_in_pro=8` — the platform withholding
    # the rest. Recall is therefore UNANSWERABLE from anon by construction, so
    # running this lane there would publish a permanent BLIND that looks like a
    # broken probe. Retrieval quality is a PAYING caller's question; without a
    # paid credential the honest verdict is "we could not sit in that seat".
    seat = SEAT_PAID
    if not C.seat_available(SEAT_PAID):
        out.append(blind(
            key=stable_key("retrieval", "ground-truth"), surface="retrieval",
            seat=SEAT_PAID,
            title="Retrieval unobserved — no paid credential to judge recall from",
            why=("the anon seat is served a trimmed result set (a withheld "
                 "remainder is announced in the envelope), so recall cannot be "
                 "judged from it; set DCHUB_REVIEWER_KEY to observe this lane"),
            basis="config.seat_available(paid) — DCHUB_REVIEWER_KEY unset"))
        return
    try:
        sess = MCPSession(C.MCP_URL, api_key=C.PAID_KEY, timeout=C.HTTP_TIMEOUT)
        truth_env = sess.call(GROUND_TRUTH_TOOL, GROUND_TRUTH_ARGS)
    except Unreachable as e:
        out.append(blind(
            key=stable_key("retrieval", "ground-truth"), surface="retrieval",
            seat=seat, title="Retrieval unobserved — no MCP session",
            why=str(e), basis=f"paid MCP tools/call {GROUND_TRUTH_TOOL}"))
        return

    truth_rows = _rows(truth_env)
    truth_names = _names(truth_rows)
    if not truth_names:
        # No ground truth means no judgement is possible. Say so; do not guess.
        out.append(blind(
            key=stable_key("retrieval", "ground-truth"), surface="retrieval",
            seat=seat,
            title="Retrieval unobserved — the deterministic door returned no "
                  "named entity to search for",
            why=(f"{GROUND_TRUTH_TOOL}{GROUND_TRUTH_ARGS} yielded "
                 f"{len(truth_rows)} row(s) and 0 usable name field(s) "
                 f"(looked for {', '.join(NAME_FIELDS)}); without a known-present "
                 "entity there is nothing to hold the semantic door to"),
            basis=(f"{seat} MCP {GROUND_TRUTH_TOOL}, structuredContent rows, "
                   f"name fields {NAME_FIELDS}")))
        return

    target = truth_names[0]
    for tool in SEMANTIC_TOOLS:
        key = stable_key("retrieval", "finds-known-entity", tool)
        try:
            env = sess.call(tool, {"query": target})
        except Unreachable as e:
            out.append(blind(
                key=key, surface="retrieval", seat=seat,
                title=f"{tool} unobserved", why=str(e),
                basis=f"{seat} MCP tools/call {tool}"))
            continue

        if env.get("_jsonrpc_error"):
            out.append(Finding(
                key=key, surface="retrieval", seat=seat,
                title=f"{tool} errors on a query naming an entity the platform "
                      f"itself returned",
                verdict=RED, severity=MAJOR,
                evidence=(f"query={target!r} (from {GROUND_TRUTH_TOOL}) -> "
                          f"{str(env['_jsonrpc_error'])[:200]}"),
                basis=(f"{seat} MCP tools/call {tool} with a name taken from this "
                       f"same run's {GROUND_TRUTH_TOOL} result"),
                red_when="the semantic door returns a protocol error for a query "
                         "the platform's own deterministic door just answered",
                remedy="An agent that cannot search by name falls back to "
                       "guessing; fix the tool or stop advertising it."))
            continue

        trimmed, marker = _looks_previewed(env)
        if trimmed:
            out.append(blind(
                key=key, surface="retrieval", seat=seat,
                title=f"{tool} retrieval quality unobserved — the result set "
                      f"was trimmed for this seat",
                why=(f"the envelope announces a withheld remainder ({marker}), "
                     "so what came back measures the paywall rather than the "
                     "index — recall cannot be judged from a truncated set, "
                     "and a check that can only ever say 'not found' would be "
                     "a permanent false positive"),
                basis=(f"{seat} MCP {tool}; structuredContent preview markers "
                       f"{_PREVIEW_MARKERS} and withheld-remainder markers "
                       f"{_WITHHELD_MARKERS}")))
            continue

        rows = _rows(env)
        if not rows:
            out.append(Finding(
                key=key, surface="retrieval", seat=seat,
                title=f"{tool} returns NOTHING for an entity the platform's own "
                      f"lookup just returned",
                verdict=RED, severity=MAJOR,
                evidence=(f"query={target!r} -> 0 result row(s); "
                          f"{GROUND_TRUTH_TOOL} returned it in this same run "
                          f"({len(truth_rows)} row(s))"),
                basis=(f"{seat} MCP tools/call {tool}, structuredContent result "
                       f"containers; ground truth = this run's "
                       f"{GROUND_TRUTH_TOOL}{GROUND_TRUTH_ARGS}"),
                red_when="the semantic door returns zero results for the exact "
                         "name of an entity the deterministic door returned in "
                         "the same run — the corpus denies something it holds",
                remedy="Check the embedding/index build for this entity class: "
                       "an empty answer here means the chunk was never indexed, "
                       "or the query never reached the index."))
            continue

        names = _names(rows)
        hit = (any(target.lower() in n.lower() or n.lower() in target.lower()
                   for n in names)
               or _mentions(rows, target))
        if not hit and not names:
            # Rows came back but nothing readable to compare. Saying "not
            # found" here would be an absence proven by reading the wrong
            # field — shell #49, which this harness exists to refuse.
            out.append(blind(
                key=key, surface="retrieval", seat=seat,
                title=f"{tool} answered, but no readable entity name to "
                      f"compare against",
                why=(f"{len(rows)} row(s) returned and 0 exposed a name in "
                     f"{NAME_FIELDS} at row level or inside "
                     f"{NAME_CONTAINERS}, and none mentioned {target!r} in "
                     f"{TEXT_FIELDS}; the shape changed or the fields moved"),
                basis=(f"{seat} MCP tools/call {tool}, structuredContent rows; "
                       f"name fields {NAME_FIELDS}, containers "
                       f"{NAME_CONTAINERS}, text fields {TEXT_FIELDS}")))
        elif hit:
            out.append(Finding(
                key=key, surface="retrieval", seat=seat,
                title=f"{tool} finds a known-present entity by name",
                verdict=PASS, severity=INFO,
                evidence=(f"query={target!r} -> {len(rows)} row(s), target "
                          f"present in results"),
                basis=(f"{seat} MCP tools/call {tool}; ground truth = this run's "
                       f"{GROUND_TRUTH_TOOL} result"),
                red_when="the semantic door returns zero results for an entity "
                         "the deterministic door returned in the same run",
                remedy=""))
        else:
            # Ranking is a judgement. We promised not to make one.
            out.append(Finding(
                key=key, surface="retrieval", seat=seat,
                title=(f"{tool} answered, but the known-present entity was not "
                       f"among the returned names"),
                verdict=GAUGE, severity=INFO,
                evidence=(f"query={target!r} -> {len(rows)} row(s); returned "
                          f"{names[:5]}"),
                basis=(f"{seat} MCP tools/call {tool}; ground truth = this run's "
                       f"{GROUND_TRUTH_TOOL} result. Names read from "
                       f"{NAME_FIELDS} at row level then inside "
                       f"{NAME_CONTAINERS}; free text {TEXT_FIELDS} also "
                       "searched for the target before calling it a miss"),
                red_when="n/a — GAUGE by design. The platform declares no "
                         "ranking or recall target, and inventing one would "
                         "make this board argue with a judge instead of an "
                         "observation. Only the EMPTY answer is decidable, and "
                         "that check is separate",
                remedy=""))

        got, examined = _cited(rows, env)
        ckey = stable_key("retrieval", "citations", tool)
        if examined and got == 0:
            out.append(Finding(
                key=ckey, surface="retrieval", seat=seat,
                title=f"{tool} returns {examined} result(s) with NO citation "
                      f"anywhere in the envelope",
                verdict=RED, severity=MAJOR,
                evidence=(f"query={target!r}; {examined} row(s), 0 carrying "
                          "citation/source, and no envelope-level citation"),
                basis=(f"{seat} MCP tools/call {tool}; per-row citation/source/"
                       "source_url/sources/cite/url, then structuredContent "
                       "citation/sources/citations"),
                red_when="a retrieval tool returns rows and neither the rows nor "
                         "the envelope carry any citation — the platform's own "
                         "instructions advertise CITED ground truth",
                remedy="Attach the source each row came from. An uncited answer "
                       "is indistinguishable from a hallucination to the agent "
                       "reading it."))
        elif examined:
            out.append(Finding(
                key=ckey, surface="retrieval", seat=seat,
                title=f"{tool} citation coverage: {got}/{examined} row(s)",
                verdict=PASS if got == examined else GAUGE,
                severity=INFO,
                evidence=f"query={target!r}; {got} of {examined} row(s) cited",
                basis=(f"{seat} MCP tools/call {tool}; per-row citation fields, "
                       "then the envelope-level citation"),
                red_when=("no row and no envelope field carries a citation"
                          if got == examined else
                          "n/a — GAUGE. Partial coverage has no declared floor: "
                          "the platform advertises cited answers, not a "
                          "cited-row percentage. Zero citations anywhere IS "
                          "decidable and reds separately"),
                remedy=""))


def _check_latency(out: list) -> None:
    for path in LATENCY_PATHS:
        key = stable_key("perf", "latency", path)
        samples, statuses = [], []
        for i in range(LATENCY_SAMPLES):
            url = f"{C.EDGE}{path}?_={int(time.time() * 1000)}{i}"
            t0 = time.monotonic()
            try:
                status, _h, _b = fetch(url, timeout=int(EDGE_TIMEOUT_S) + 15,
                                       retries=0)
            except Unreachable as e:
                out.append(blind(
                    key=key, surface="perf", seat=SEAT_NONE,
                    title=f"{path} latency unobserved", why=str(e),
                    basis=f"GET {C.EDGE}{path} cache-busted, {LATENCY_SAMPLES} "
                          "samples"))
                samples = []
                break
            samples.append(time.monotonic() - t0)
            statuses.append(status)
        if not samples:
            continue

        med = _median_ms(samples)
        spread = "/".join(str(int(s * 1000)) for s in samples)
        over = med > EDGE_TIMEOUT_S * 1000
        out.append(Finding(
            key=key, surface="perf", seat=SEAT_NONE,
            title=(f"{path} median {med}ms exceeds the edge's own {int(EDGE_TIMEOUT_S)}s "
                   f"route budget" if over else
                   f"{path} median {med}ms"),
            verdict=RED if over else GAUGE,
            severity=MAJOR if over else INFO,
            evidence=(f"{LATENCY_SAMPLES} cache-busted GETs: {spread}ms "
                      f"(status {statuses}); median {med}ms"),
            basis=(f"GET {C.EDGE}{path} with ?_=<ms> per sample so no response "
                   f"is edge-cached; MEDIAN of {LATENCY_SAMPLES} because a cold "
                   "first call is not a slow endpoint"),
            red_when=(f"the median exceeds {int(EDGE_TIMEOUT_S)}s — the CF zone's "
                      "own route timeout, past which this path returns 503 or "
                      "fails over to the stale origin. Everything faster is a "
                      "GAUGE: the platform declares no other latency target"),
            remedy=("Move the slow work off-request. Past the edge budget the "
                    "caller gets the worker's error envelope while the origin "
                    "keeps running." if over else "")))


def probe(out: list) -> None:
    """Entry point — signature matches every other probe module."""
    _check_retrieval(out)
    _check_latency(out)
