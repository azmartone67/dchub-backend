"""Fixed-window weekly series — GET /api/v1/reports/weekly-series.

Public, machine-readable, no auth. Built 2026-08-05.

★ THE DEFECT THIS FILE EXISTS TO RETIRE ★
Every agent-count surface DC Hub publishes is a ROLLING 7-day window ending
NOW, recomputed on every request. Measured 2026-08-05, three polls ~15 minutes
apart: real_external_agents_prior_7d read 81 -> 81 -> 82 and
real_external_calls_prior_7d read 3503 -> 3503 -> 3505. Four hours later the
same two fields read 83 and 3665. The BASELINE of a published week-over-week
percentage is therefore not a fixed historical fact — it drifts under the
reader while the delta is being quoted. That is why seven AI partners were
told, in writing, not to quote our WoW delta. This endpoint removes that
caveat by publishing NON-OVERLAPPING ISO calendar weeks whose boundaries are
closed: once a week is complete its numbers can only change if the underlying
rows change, never because the clock moved.

WHY IT IS NOT A COSMETIC DIFFERENCE. On the rolling metric the agent count
read 95 (07-31) -> 84 (08-02) -> 49 (08-04) -> 48 (08-05) against a prior
window of 81, which renders as a -40% cliff and an 8.6% retention rate. On
fixed weeks the same underlying traffic reads 43 -> 81 -> 62 -> 85. The
rolling series and the fixed series disagree about the SIGN of the trend.
Only one of them is answering the question "is agent use expanding?".

★ FIVE HONESTY RULES, EACH ONE A DEFECT WE SHIPPED THIS WEEK ★

1. The in-progress week is NEVER in the series. It is excluded by the QUERY's
   own upper bound (`created_at < date_trunc('week', now())`), not by a
   post-filter a later edit could drop. /api/v1/ai/reach/trend does the
   opposite: measured 2026-08-05 (a Wednesday) it returned week 2026-08-03
   with 6 agents inside weeks[], immediately after a complete week of 85. A
   two-day week charted beside seven-day weeks reads as a 93% collapse. The
   live partial week is still published here — it is real and useful — but as
   a SEPARATE top-level key carrying partial=true, and no delta may touch it.

2. The observation population is declared inline and BUILT FROM the executed
   filters (the _p50_filters/_p50_population pattern from PR #2253). The
   published lists are the same Python lists the query joins into its SQL, so
   an edit to one is an edit to both. A hand-written prose description of a
   filter is a second source of truth, and second sources drift.

3. Self-traffic is excluded by COMPOSING external_platform_predicate() and
   real_ua_predicate() imported from mcp_calls_deloop — never by copying an
   exclusion list. PR #2252 exists because a published p50 had no externality
   filter and ~80% of its population was DC Hub probing itself, including the
   refresh job that built the very table being measured.

4. A week we did not observe renders NULL, never 0. /api/v1/ai/reach/trend
   publishes week 2026-06-15 as distinct_external_ips=0 AND
   new_external_ips=17 in the same row — a week with zero distinct IPs cannot
   have seventeen first-ever IPs, which proves that 0 was never measured. It
   is a missing observation wearing the costume of a finding. Here the two
   cases are separated and neither is guessed: a week whose underlying table
   held rows we could observe can honestly report 0 real external agents
   (status="measured", we looked and found none); a week with no observable
   rows at all reports null (status="no_observation").

5. Every week carries its ISO week start date, its exclusive end date and its
   ISO year-week label, so a reader can verify the boundary rather than trust
   it.

Reads only the canonical identity basis — mcp_calls_identity, is_public_ip AND
is_real_external, identity = agent_id — so this endpoint and the rolling
funnel figures differ ONLY in their window, never in their basis. The rolling
7d canonical count is published alongside, labelled as a different window, per
the parity rule in mcp_calls_deloop.CANONICAL_AGENTS_BASIS.
"""
from __future__ import annotations

import datetime as _dt

from flask import Blueprint, jsonify, request

from mcp_calls_deloop import (
    CANONICAL_AGENTS_BASIS,
    canonical_external_activity_sql,
    external_platform_predicate,
    real_ua_predicate,
)
from routes.brain_ascension_master_shell import _conn

weekly_series_bp = Blueprint("weekly_series", __name__)

_TABLE = "mcp_calls_identity"

# Single-sourced in mcp_calls_deloop so this series, the funnel card
# and agent-retention lane 5 cannot disagree about whether the same
# share is a problem. Fallback keeps the series alive if the import
# ever fails — losing the whole endpoint to an annotation constant
# would be far worse than a stale threshold.
try:
    from mcp_calls_deloop import CONCENTRATION_PCT as _CONCENTRATION_PCT
except Exception:  # pragma: no cover - import-shape guard
    _CONCENTRATION_PCT = 25.0

# Week counts. The floor is 2 because a series of one week supports no delta
# at all; the ceiling bounds the GROUP BY scan over the identity view.
_DEFAULT_WEEKS = 8
_MIN_WEEKS = 2
_MAX_WEEKS = 26

_STATEMENT_TIMEOUT_MS = 20_000


# ── population definition changes ────────────────────────────────────────────
# ★ THE SIXTH HONESTY RULE, AND THE ONE THIS FILE SHIPPED WITHOUT ★
#
# Rules 1-5 all protect the reader from a WINDOW that moves. None of them
# protects the reader from the POPULATION moving. A fixed week is only
# comparable to the week before it if "one real external agent call" meant the
# same thing in both — and on 2026-08-18 06:31Z it stopped meaning the same
# thing, mid-week, with nothing in this payload saying so.
#
# dchub-mcp-server #202 lifted the CI self-tag out of the `clientInfo` branch
# and onto a per-request header, so DC Hub's own GitHub Actions smoke suites
# finally classify as internal and drop out of is_real_external. That is a
# CORRECTION — the numbers were wrong before and are right now — but it is a
# correction that lands as a CLIFF, because the suites were the majority of the
# population it removed:
#
#     GH Actions share of real traffic, 7d to 2026-08-18
#         calls   1,700 / 2,114 = 80.4%
#         agents     49 /    68 = 72.1%
#
# Measured after the deploy, on mcp_calls_identity: 31 CI-shaped bursts
# (>=40 calls by one agent_id inside 300s) totalling 1,710 calls in the 193h
# BEFORE it, and ZERO in the 23h after. The exclusion is working as designed.
#
# So 2026-W34 will publish a large drop for a reason that has nothing to do
# with demand, and every consumer of this endpoint — the dashboard, the press
# headline in mcp_funnel, the partner readouts — would have rendered it as one.
# This endpoint exists precisely so that a number cannot be quoted without the
# thing that makes it comparable, and a definition change is the largest
# comparability hazard there is.
#
# ★ The marker is DATA, not prose in a note field: a consumer can branch on
# `definition_changes` being non-empty. Prose gets skimmed; a non-empty list
# gets handled.
#
# ★ It is never removed once added. A change that is "old news" to the person
# maintaining this file is still news to anyone charting 26 weeks — the series
# ceiling is _MAX_WEEKS, so a marker stays load-bearing for half a year.
_DEFINITION_CHANGES = [
    {
        "effective_at": "2026-08-18T06:31:00+00:00",
        "change": (
            "the CI self-tag moved from the `clientInfo` handshake to a "
            "per-request header, so DC Hub's own GitHub Actions smoke suites "
            "are now classified internal and leave is_real_external"
        ),
        "direction": "REDUCES agents and calls",
        "is_correction": True,
        "measured_effect": (
            "GH Actions were 80.4% of real calls (1,700/2,114) and 72.1% of "
            "real agents (49/68) in the 7d to 2026-08-18. After the deploy: 31 "
            "CI-shaped bursts / 1,710 calls in the preceding 193h, ZERO in the "
            "following 23h"
        ),
        "means": (
            "weeks on opposite sides of this timestamp count DIFFERENT "
            "populations. The drop is a measurement correction, not a demand "
            "change, and a week-over-week percentage across it is not a trend"
        ),
        "ref": "dchub-mcp-server#202",
    },
]


def _parse_effective(ts: str) -> _dt.datetime | None:
    """A malformed marker must not take the endpoint down.

    A definition marker is metadata about honesty; if it is itself broken the
    right failure is to lose the marker, not the series. Callers treat None as
    "does not fall in this week", which is the same behaviour as no marker at
    all — the pre-existing (wrong) state, not a new one.
    """
    try:
        return _dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _changes_in(start: _dt.date, end_exclusive: _dt.date) -> list[dict]:
    """Definition changes taking effect inside [start, end_exclusive).

    Half-open on the same boundary the ISO week uses, so a change landing at
    exactly Monday 00:00:00Z belongs to the week it opens and never to both.
    """
    lo = _dt.datetime.combine(start, _dt.time.min, tzinfo=_dt.timezone.utc)
    hi = _dt.datetime.combine(end_exclusive, _dt.time.min,
                              tzinfo=_dt.timezone.utc)
    out = []
    for ch in _DEFINITION_CHANGES:
        at = _parse_effective(ch.get("effective_at"))
        if at is not None and lo <= at < hi:
            out.append(ch)
    return out


def _spans_of(week_starts: list[str]) -> list[tuple]:
    """ISO week starts -> half-open [start, end) UTC datetime spans.

    Unparseable entries are dropped, not defaulted: a week we cannot place on
    the calendar must not be silently placed at the epoch.
    """
    out = []
    for ws in week_starts:
        try:
            d = _dt.date.fromisoformat(ws)
        except (TypeError, ValueError):
            continue
        out.append((
            _dt.datetime.combine(d, _dt.time.min, tzinfo=_dt.timezone.utc),
            _dt.datetime.combine(d + _dt.timedelta(weeks=1), _dt.time.min,
                                 tzinfo=_dt.timezone.utc)))
    return out


def comparability_for_spans(spans: list[tuple]) -> dict:
    """The two hazards over ARBITRARY half-open [start, end) UTC windows.

    ★ 2026-08-20 — an ISO week is only one shape of window. The funnel payload
    publishes seven bare `*_wow_pct` keys over ROLLING 7d windows, which carry
    exactly the same two hazards and have no ISO week to name. Before this
    existed the only comparability check was week-shaped, so those keys shipped
    with no marker at all — and the funnel dashboard rendered
    real_external_agents_complete_wk_wow_pct as "the trend number" while
    real_external_agents_wow_pct was the "crash" beside it, both across the
    #202 correction.

    Public (no underscore) because flask_mcp_endpoints is the caller: the
    alternative was a second implementation over there, which is the twin drift
    this file already pays for elsewhere.
    """
    hits = []
    for lo, hi in spans:
        for ch in _DEFINITION_CHANGES:
            at = _parse_effective(ch.get("effective_at"))
            if at is not None and lo <= at < hi:
                hits.append(ch)
    seen, uniq = set(), []
    for ch in hits:
        if ch["effective_at"] not in seen:
            seen.add(ch["effective_at"])
            uniq.append(ch)

    sup = []
    for ch in _DEFINITION_CHANGES:
        if not ch.get("is_correction"):
            continue
        at = _parse_effective(ch.get("effective_at"))
        if at is None:
            continue
        # `spans and` matters: an empty window list must not report
        # supersession off a vacuous all([]) == True.
        if spans and all(hi <= at for _lo, hi in spans):
            sup.append(ch)
    return _verdict(uniq, sup)


def _superseded_by(week_starts: list[str]) -> list[dict]:
    """Corrections that took effect AFTER every week in the delta.

    ★ THE GAP `crosses_definition_change` CANNOT SEE (2026-08-20).
    _changes_in asks whether a change lands INSIDE a week the delta touches. A
    correction landing after ALL of them lands inside NONE of them, so the
    delta is declared comparable — and the sentence published beside it,
    "every week in this delta counts the same population", is TRUE and reads
    as an all-clear. Both weeks do count the same population. That population
    is the one the correction removed.

    Measured live 2026-08-19: the 2026-08-03 -> 2026-08-10 delta published
    agents_pct=+89.5 (38 -> 72) with crosses_definition_change=False, and the
    funnel dashboard rendered it as "the trend number" on the same screen as a
    -28.8% rolling-7d "crash". Both weeks END before #202 (2026-08-18 06:31Z),
    so ~72% of the agents in each were DC Hub's own GitHub Actions runners
    minting a fresh agent_id per rotated IP. The delta is arithmetically
    correct, internally consistent, and describes our CI cadence.

    A week is superseded when its whole span precedes the correction: the
    half-open end (Monday 00:00Z of the following week) is <= effective_at, the
    same boundary _changes_in uses, so a week can never be both superseded by
    and straddling one change.

    ★ Only `is_correction` changes supersede. A change that redefines the
    population going FORWARD without declaring the old count wrong leaves
    earlier weeks perfectly quotable among themselves; conflating the two would
    withhold deltas that are fine.
    """
    out = []
    for ch in _DEFINITION_CHANGES:
        if not ch.get("is_correction"):
            continue
        at = _parse_effective(ch.get("effective_at"))
        if at is None:
            continue
        spans = []
        for ws in week_starts:
            try:
                d = _dt.date.fromisoformat(ws)
            except (TypeError, ValueError):
                continue
            end = _dt.datetime.combine(d + _dt.timedelta(weeks=1),
                                       _dt.time.min, tzinfo=_dt.timezone.utc)
            spans.append(end <= at)
        # `spans and` matters: an all-unparseable week list must not report
        # supersession off a vacuous all([]) == True.
        if spans and all(spans):
            out.append(ch)
    return out


def _comparability(week_starts: list[str]) -> dict:
    """Is a delta over these weeks quotable as a trend?

    Takes the week_start strings a delta actually divided, so it cannot drift
    from the arithmetic it describes. Two distinct hazards, because they fail
    in opposite directions and a reader must be able to tell them apart:

      crosses_definition_change — a change lands INSIDE a week it touches, so
        the two sides of the division count different populations.
      superseded_by_correction  — every week predates a correction, so the two
        sides agree with each other and both count a population we have since
        declared wrong.

    A delta is quotable only when NEITHER holds. Consumers should branch on
    `quotable_as_trend`; the two booleans stay published so the REASON for a
    refusal is legible rather than a bare false.
    """
    hits = []
    for ws in week_starts:
        try:
            d = _dt.date.fromisoformat(ws)
        except (TypeError, ValueError):
            continue
        hits.extend(_changes_in(d, d + _dt.timedelta(weeks=1)))
    # dedupe on effective_at, preserving order
    seen, uniq = set(), []
    for ch in hits:
        if ch["effective_at"] not in seen:
            seen.add(ch["effective_at"])
            uniq.append(ch)
    return _verdict(uniq, _superseded_by(week_starts))


def _verdict(uniq: list[dict], sup: list[dict]) -> dict:
    """The published comparability dict, built in exactly ONE place.

    Both the week-shaped and the span-shaped entry points land here, so a
    consumer branching on `quotable_as_trend` gets the same contract whichever
    asked — and the two can never drift into disagreeing about what a refusal
    means.
    """
    if uniq:
        _means = (
            "at least one week in this delta counts a DIFFERENT population "
            "from the others — see changes[]. The percentage is arithmetically "
            "correct and is NOT a trend. Do not quote it as one."
        )
    elif sup:
        _means = (
            "every week in this delta counts the same population — and it is "
            "the SUPERSEDED one: a correction in superseded_by[] took effect "
            "after ALL of them, so both sides agree with each other about a "
            "count we have since declared wrong. The percentage is "
            "arithmetically correct and is NOT a trend. Do not quote it, and "
            "do not compare either week to one measured after the correction."
        )
    else:
        _means = "every week in this delta counts the same population"
    return {
        "crosses_definition_change": bool(uniq),
        "changes": uniq,
        "superseded_by_correction": bool(sup),
        "superseded_by": sup,
        "quotable_as_trend": not (uniq or sup),
        "means": _means,
    }


# ── the executed filters, published verbatim ─────────────────────────────────
# Two lists, because they land in two different places in the SQL and a reader
# is entitled to know which is which:
#
#   _window_filters()      -> the WHERE clause. Defines which rows are LOOKED
#                             AT, and is what makes the series fixed-window.
#   _population_filters()  -> the FILTER (WHERE ...) clause on the aggregates.
#                             Defines which looked-at rows COUNT as a real
#                             external agent call.
#
# Splitting them is what makes rule 4 implementable: rows that pass the window
# but fail the population are still proof that the week was OBSERVED, so a
# genuine zero can be distinguished from a missing observation. If both sets
# lived in one WHERE, an unobserved week and a probe-only week would be
# indistinguishable and we would be back to guessing which zero we were
# looking at.
#
# ★ Both functions are called EXACTLY ONCE each per request: their return
# value is joined into the SQL and published in the payload. There is no
# second copy to drift.


def _window_filters(weeks: int) -> list[str]:
    """The observation window — WHERE clauses, in order.

    The upper bound is the honesty guarantee of this whole endpoint: rows in
    the current, still-accumulating ISO week are never fetched, so a partial
    week cannot reach the series even if every later line of this file were
    deleted. `date_trunc('week', ...)` is Postgres' Monday-start ISO week and
    matches the reach_weekly rollup's own _monday() boundary; the session is
    pinned to UTC before these run so the boundary does not move with the
    server's locale.

    weeks is int()-coerced because these fragments are inlined, never bound —
    see the note on _run().
    """
    n = int(weeks)
    return [
        "created_at >= date_trunc('week', now()) - interval '%d weeks'" % n,
        "created_at <  date_trunc('week', now())",
    ]


def _population_filters() -> list[str]:
    """What counts as one real external agent call — FILTER clauses, in order.

    The first two are the canonical identity basis (the same two the funnel's
    real_external_agents_7d runs on, so this series and that headline differ
    only by window). The last two are the canonical externality verdict,
    IMPORTED from mcp_calls_deloop rather than restated: is_real_external
    already renders real_calls_predicate() into the view, and composing the
    predicates again here is deliberate belt-and-braces — it means this
    endpoint keeps excluding DC Hub's own traffic even if the view were ever
    rebuilt from a stale DDL. Two independent renderings of ONE definition
    cannot drift; two hand-maintained lists always do.

    external_platform_predicate contains a literal % (LIKE). Safe only while
    the execute() that consumes this passes no bound params — see _run().
    """
    return [
        "is_public_ip",
        "is_real_external",
        external_platform_predicate("platform"),
        real_ua_predicate("user_agent"),
    ]


def _population(weeks: int) -> dict:
    """What is counted, in prose and in the exact SQL that counts it."""
    return {
        "statistic": "COUNT(DISTINCT agent_id) and COUNT(*) per ISO week",
        "observations": "MCP tool calls",
        "window": (
            f"the last {int(weeks)} COMPLETE ISO calendar weeks "
            "(Monday 00:00:00 UTC inclusive to the next Monday 00:00:00 UTC "
            "exclusive). Non-overlapping and closed: the in-progress week is "
            "excluded by the query's upper bound, not by a later filter"
        ),
        "source": _TABLE,
        "timezone": "UTC",
        "identity": (
            "agent_id = md5(first public X-Forwarded-For token), NULL for "
            "Cloudflare POP ranges so an edge proxy is never counted as an "
            "agent. An IP-derived PROXY for agents: several agents behind one "
            "NAT count once, one agent on rotating egress counts many times"
        ),
        "includes": "external callers only, keyed and keyless alike",
        "excludes": (
            "DC Hub's own platforms (dchub-*, probes, QA harnesses, registry "
            "crawlers) and scripted/internal user-agents; private, CGNAT and "
            "Cloudflare-POP source addresses"
        ),
        "why_it_matters": (
            "every other agent-count surface is a rolling 7d window ending "
            "now, recomputed per request: on 2026-08-05 the published "
            "prior-7d baseline moved 81 -> 82 -> 83 across four hours of "
            "polling. A percentage whose baseline moves is not a "
            "week-over-week delta. These weeks are fixed, so the same query "
            "run tomorrow returns the same history"
        ),
        "sql_where_filters": _window_filters(weeks),
        "sql_population_filters": _population_filters(),
    }


# ── pure assembly, testable without a database ───────────────────────────────

def _week_starts(current_week_start: _dt.date, weeks: int) -> list[_dt.date]:
    """The complete-week starts the series must account for, ascending.

    Derived from the SAME upper bound the SQL uses, so the expected set and
    the fetched set cannot disagree about which weeks are complete. The
    current week start is NOT in the result — the last entry is the Monday
    seven days before it.
    """
    n = int(weeks)
    return [current_week_start - _dt.timedelta(weeks=k)
            for k in range(n, 0, -1)]


def _iso_label(d: _dt.date) -> str:
    """ISO year-week label, e.g. 2026-W31 — the boundary, stated."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _assemble(rows: dict, week_starts: list[_dt.date],
              net: dict | None = None,
              harness: dict | None = None) -> list[dict]:
    """One row per expected week; nulls where nothing was observed.

    rows maps week_start -> (agents, calls, rows_observed). A week absent from
    the mapping was never observed at all.

    net maps week_start -> (calls, top_calls, net_calls, agents, net_agents,
    top_caller_client) and is OPTIONAL — it defaults to None so the two-arg
    callers that predate it keep working, and so a failure of its query costs
    a reader the concentration fields and nothing else.

    ★ The zero rule, stated once and applied here: `rows_observed` counts rows
    in the underlying table for that week BEFORE the population filters. It is
    the evidence that we were recording. With it we can say "we looked and
    found no real external agents" (a finding) instead of publishing the same
    0 we would publish for "we have no idea" (a guess). Without it, both
    render 0 and a reader cannot tell a quiet week from a data gap — which is
    precisely how /api/v1/ai/reach/trend came to publish a week with 0
    distinct IPs and 17 first-ever IPs in the same row.
    """
    out = []
    for ws in week_starts:
        rec = rows.get(ws)
        end = ws + _dt.timedelta(weeks=1)
        changes = _changes_in(ws, end)
        base = {
            "week_start": ws.isoformat(),
            "week_end_exclusive": end.isoformat(),
            "iso_week": _iso_label(ws),
            "days": 7,
            "partial": False,
            # Always present, empty when clean — a machine reader should be
            # able to branch on this key without first proving it exists.
            "definition_changes": changes,
        }
        if changes:
            base["comparability_warning"] = (
                "the counting definition changed DURING this week — it is not "
                "directly comparable to weeks either side of it. See "
                "definition_changes[]"
            )
        if rec is None or rec[2] <= 0:
            base.update({
                "agents": None,
                "calls": None,
                "status": "no_observation",
                "note": ("no rows for this week in the source table — the "
                         "count is UNKNOWN, deliberately not 0"),
            })
        else:
            base.update({
                "agents": int(rec[0] or 0),
                "calls": int(rec[1] or 0),
                "status": "measured",
                "rows_observed": int(rec[2]),
            })
            # Concentration, per week, from the same rows. Attached only when
            # the week actually has calls — a share of zero is not 0%, it is
            # undefined, and publishing 0.0 there would read as "no
            # concentration" on a week with no data.
            nrec = (net or {}).get(ws)
            if nrec and int(nrec[0] or 0) > 0:
                n_calls, n_top = int(nrec[0] or 0), int(nrec[1] or 0)
                pct = round(100.0 * n_top / n_calls, 1)
                base.update({
                    "top_caller_calls": n_top,
                    "top_caller_client": nrec[5],
                    "top_caller_pct": pct,
                    "calls_net_of_top": int(nrec[2] or 0),
                    "agents_net_of_top": int(nrec[4] or 0),
                    "concentration_flag": pct >= _CONCENTRATION_PCT,
                })
            # ★ QA sweep 2026-09-02 (F7): the harness companion, beside
            # *_net_of_top and from the same rows. harness maps week_start ->
            # (net_calls, net_agents, harness_calls). Reported, never
            # deny-listed: the names ride along so a reader can see WHO was
            # subtracted (mcp_calls_deloop.HARNESS_CLIENT_NAMES).
            hrec = (harness or {}).get(ws)
            if hrec is not None:
                from mcp_calls_deloop import HARNESS_CLIENT_NAMES as _hn
                base.update({
                    "calls_net_of_harnesses": int(hrec[0] or 0),
                    "agents_net_of_harnesses": int(hrec[1] or 0),
                    "harness_calls": int(hrec[2] or 0),
                    "harness_names": list(_hn),
                })
        out.append(base)
    return out


def _wow(weeks: list[dict]) -> dict:
    """Week-over-week between the two most recent COMPLETE weeks.

    Refuses in every ambiguous case rather than publishing a number that
    cannot be checked:
      · fewer than two weeks in the series
      · either week unmeasured (null must not be arithmetic'd into a delta)
      · a zero baseline (a percentage change from 0 is undefined, not +inf,
        and definitely not 0)

    The two week_starts used are published so a reader can recompute the
    division from the series above and land on the same number. Partial weeks
    are structurally unreachable here: _assemble only ever emits complete
    weeks, and the live partial week is carried on a different key entirely.
    """
    out = {"agents_pct": None, "calls_pct": None,
           "current_week_start": None, "baseline_week_start": None,
           "baseline_is_fixed": True, "reason": None}
    complete = [w for w in weeks if not w.get("partial")]
    if len(complete) < 2:
        out["reason"] = "fewer than two complete weeks in the series"
        return out
    prev, last = complete[-2], complete[-1]
    out["current_week_start"] = last["week_start"]
    out["baseline_week_start"] = prev["week_start"]
    if last["status"] != "measured" or prev["status"] != "measured":
        out["reason"] = (
            f"a week in the pair was not observed "
            f"({prev['week_start']}={prev['status']}, "
            f"{last['week_start']}={last['status']}) — a delta against an "
            "unknown is not a delta"
        )
        return out

    def pct(now_v, base_v):
        if not base_v:
            return None
        return round((now_v - base_v) * 100.0 / base_v, 1)

    out["agents_pct"] = pct(last["agents"], prev["agents"])
    out["calls_pct"] = pct(last["calls"], prev["calls"])
    out["comparability"] = _comparability(
        [prev["week_start"], last["week_start"]])
    if out["agents_pct"] is None and out["calls_pct"] is None:
        out["reason"] = (
            f"baseline week {prev['week_start']} measured zero — percentage "
            "change from a zero baseline is undefined, so it is withheld"
        )
    return out


# ── robust baseline (2026-08-11) ─────────────────────────────────────────────
# THE DEFECT ONE LAYER UP FROM THE ONE THIS FILE ALREADY FIXED.
#
# Fixing the window to complete ISO weeks removed a baseline that MOVED under
# the reader. It did not remove a baseline that is a SINGLE OBSERVATION. A
# week-over-week percentage inherits all of its baseline week's volatility, so
# when that one week is an outlier the honest series still produces a dishonest
# headline.
#
# Live, the day this was written:
#     2026-07-06   43 agents   3,514 calls
#     2026-07-13   81 agents   2,701 calls
#     2026-07-20   62 agents   1,971 calls
#     2026-07-27   85 agents   8,334 calls   <- baseline, ~3x its neighbours
#     2026-08-03   38 agents   2,381 calls   <- current
#
# Published delta: calls -71.4%. But 2,381 sits inside the established
# 1,971-3,514 band. Calls did not fall by 71%; they returned to trend from a
# one-week spike. Against the trailing median the same week reads about -23%.
#
# The agents number survives the correction (-47% against the median vs -55%
# against the spike) and THAT is the point: a robust baseline is not a way to
# make bad weeks look better. It made the calls panic go away and left the
# agent decline standing, which is the only reason to trust it.
#
# BOTH are published. The single-week delta keeps its key and its meaning for
# every existing consumer; this is the one to quote.
#
# ★ HONESTY ABOUT THE STATISTICS. This is a median over a handful of weeks, not
# an inferential test, and it is labelled as one. `baseline_is_outlier` is a
# DECLARED RATIO THRESHOLD (>=2x or <=0.5x the median), not significance — with
# n this small, anything dressed up as a hypothesis test would be theatre. The
# threshold, the window and the n all ride in the payload so a reader can
# disagree with the rule and recompute.
_ROBUST_BASELINE_WEEKS = 4
_OUTLIER_HIGH = 2.0
_OUTLIER_LOW = 0.5


def _median(values: list) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def _robust_wow(weeks: list[dict], window: int = _ROBUST_BASELINE_WEEKS) -> dict:
    """Current complete week against the MEDIAN of the preceding `window`
    complete measured weeks.

    Refuses on the same terms as _wow — an unmeasured week is never
    arithmetic'd, a zero baseline yields None rather than a fabricated
    percentage, and too short a history refuses outright rather than quietly
    shrinking the window.
    """
    out = {"agents_pct": None, "calls_pct": None,
           "baseline_kind": "trailing_median",
           "baseline_window_weeks": window,
           "baseline_weeks_used": [], "baseline_n": 0,
           "baseline_agents": None, "baseline_calls": None,
           "current_week_start": None,
           "why": ("A week-over-week percentage inherits its baseline week's "
                   "volatility. Against a single outlier week the delta "
                   "describes the outlier, not the trend. Quote this one."),
           "reason": None}

    measured = [w for w in weeks
                if not w.get("partial") and w.get("status") == "measured"]
    if len(measured) < window + 1:
        out["reason"] = (
            f"need {window + 1} measured complete weeks for a "
            f"{window}-week trailing median; have {len(measured)}"
        )
        return out

    last = measured[-1]
    base = measured[-(window + 1):-1]
    out["current_week_start"] = last["week_start"]
    out["baseline_weeks_used"] = [w["week_start"] for w in base]
    out["baseline_n"] = len(base)
    out["baseline_agents"] = _median([w["agents"] for w in base])
    out["baseline_calls"] = _median([w["calls"] for w in base])

    def pct(now_v, base_v):
        if not base_v:
            return None
        return round((now_v - base_v) * 100.0 / base_v, 1)

    out["agents_pct"] = pct(last["agents"], out["baseline_agents"])
    out["calls_pct"] = pct(last["calls"], out["baseline_calls"])
    # ★ A trailing median does NOT survive a definition change. It is robust to
    # an outlier WEEK, which is a sampling problem; a definition change is a
    # population problem, and averaging over four weeks of the old population
    # makes the break WORSE by hiding it behind a smooth-looking baseline.
    # This is the one delta the payload tells readers to quote, so it is the
    # one that most needs the warning attached.
    out["comparability"] = _comparability(
        out["baseline_weeks_used"] + [last["week_start"]])
    if out["agents_pct"] is None and out["calls_pct"] is None:
        out["reason"] = ("trailing median measured zero — percentage change "
                         "from a zero baseline is undefined, so it is withheld")
    return out


def _baseline_outlier_flag(weeks: list[dict],
                           window: int = _ROBUST_BASELINE_WEEKS) -> dict:
    """Does the SINGLE week the published `wow` divides by look anomalous?

    This is what makes the two deltas legible side by side: without it a reader
    sees -71.4% and -23% and has no way to tell which to believe.

    ★2026-08-21 (Claim Loop step 3): extended to EVERY metric, ADDITIVELY. The
    top-level keys (`metric='calls'`, `ratio_to_median`, `is_outlier`, `means`,
    …) keep their exact prior meaning and values so every existing consumer and
    test is untouched — `calls` was the panic metric and stays the headline
    flag. The new `per_metric` block runs the SAME ratio-threshold rule over
    `calls` AND `agents` (and any other numeric week key present), so the
    claim-breaker gate can refuse a WoW headlined off an outlier baseline for a
    metric OTHER than calls — the agents series was the one that legitimately
    declined, and a spike there would be just as misleading. `any_metric_outlier`
    is the convenience roll-up the gate reads.
    """
    out = {"checked": False, "is_outlier": None, "metric": None,
           "baseline_week_start": None, "ratio_to_median": None,
           "rule": (f"flagged when the baseline week is >={_OUTLIER_HIGH}x or "
                    f"<={_OUTLIER_LOW}x the median of the {window} weeks before "
                    "it. A declared threshold, NOT a significance test — the "
                    "sample is far too small for one, and pretending otherwise "
                    "would be theatre."),
           "means": None,
           # additive: per-metric flags (calls keeps the top-level meaning).
           "per_metric": {}, "any_metric_outlier": None}

    measured = [w for w in weeks
                if not w.get("partial") and w.get("status") == "measured"]
    if len(measured) < window + 2:
        return out
    baseline_week = measured[-2]
    prior = measured[-(window + 2):-2]

    # ── additive per-metric pass ─────────────────────────────────────────
    # Every numeric key that appears on the baseline week AND is present across
    # the prior window (calls + agents today; robust to new metric keys).
    def _numeric_keys(w: dict) -> set:
        return {k for k, v in w.items()
                if k not in ("week_start", "week_end_exclusive", "iso_week",
                             "days", "rows_observed")
                and isinstance(v, (int, float)) and not isinstance(v, bool)}

    metric_keys = _numeric_keys(baseline_week)
    for w in prior:
        metric_keys &= _numeric_keys(w)
    per_metric: dict = {}
    for mk in sorted(metric_keys):
        m_med = _median([w.get(mk) for w in prior])
        base_v = baseline_week.get(mk)
        if not m_med or base_v is None:
            per_metric[mk] = {"checked": False, "is_outlier": None,
                              "ratio_to_median": None,
                              "baseline_week_start": baseline_week["week_start"]}
            continue
        m_ratio = round(base_v / m_med, 2)
        per_metric[mk] = {
            "checked": True,
            "is_outlier": bool(m_ratio >= _OUTLIER_HIGH or m_ratio <= _OUTLIER_LOW),
            "ratio_to_median": m_ratio,
            "baseline_week_start": baseline_week["week_start"],
        }
    out["per_metric"] = per_metric
    out["any_metric_outlier"] = bool(
        any(pm["is_outlier"] for pm in per_metric.values() if pm["checked"]))

    # ── existing calls-based headline flag (unchanged) ───────────────────
    med = _median([w["calls"] for w in prior])
    if not med or baseline_week.get("calls") is None:
        return out

    ratio = round(baseline_week["calls"] / med, 2)
    out.update(checked=True, metric="calls",
               baseline_week_start=baseline_week["week_start"],
               ratio_to_median=ratio,
               is_outlier=bool(ratio >= _OUTLIER_HIGH or ratio <= _OUTLIER_LOW))
    out["means"] = (
        f"the baseline week is {ratio}x the median of the {len(prior)} weeks "
        "before it, so the published `wow` percentage largely describes that "
        "one week — read `robust_wow` instead"
        if out["is_outlier"] else
        f"the baseline week is {ratio}x the median of the {len(prior)} weeks "
        "before it — in line, so `wow` and `robust_wow` should broadly agree"
    )
    return out


def _partial_week(week_start: _dt.date, agents, calls, now: _dt.datetime) -> dict:
    """The live, still-accumulating week — labelled so it cannot be misread.

    Published because it is real and a reader watching adoption wants it, and
    kept OFF the series key because a two-day week rendered beside seven-day
    weeks is the exact chart that reads as a collapse. hours_elapsed lets a
    reader scale it themselves; we deliberately do not scale it for them —
    an extrapolated week is a forecast, and this endpoint publishes
    measurements.
    """
    elapsed = (now - _dt.datetime.combine(
        week_start, _dt.time.min, tzinfo=_dt.timezone.utc))
    hours = max(0.0, round(elapsed.total_seconds() / 3600.0, 1))
    end = week_start + _dt.timedelta(weeks=1)
    changes = _changes_in(week_start, end)
    out = {
        "week_start": week_start.isoformat(),
        "week_end_exclusive": end.isoformat(),
        "iso_week": _iso_label(week_start),
        "partial": True,
        "excluded_from_series": True,
        "excluded_from_delta": True,
        "hours_elapsed_of_168": hours,
        "agents": None if agents is None else int(agents),
        "calls": None if calls is None else int(calls),
        "definition_changes": changes,
        "warning": (
            "IN PROGRESS — not comparable to any complete week above. This "
            "week has had "
            f"{hours:.0f} of 168 hours to accumulate. Charting it beside "
            "complete weeks renders growth as a collapse; that is why it is "
            "not in weeks[]"
        ),
    }
    if changes:
        # ★ The live week is where a fresh change is ALWAYS caught first, and
        # it is the week a reader is most likely to be staring at while
        # wondering what happened. Say it here, not only once the week closes.
        out["comparability_warning"] = (
            "the counting definition ALSO changed during this week — part of "
            "it was measured on the old population and part on the new, so "
            "even the elapsed-hours rate is not comparable to the weeks above. "
            "See definition_changes[]"
        )
    return out


# ── the request ──────────────────────────────────────────────────────────────

def _clamp_weeks(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WEEKS
    return max(_MIN_WEEKS, min(_MAX_WEEKS, n))


def _run(weeks: int) -> dict:
    """Execute the series, the live partial week and the rolling-7d parity read.

    ★ NO BOUND PARAMS anywhere in this function. _population_filters() carries
    external_platform_predicate's literal % (LIKE); psycopg2 only interprets %
    when a parameter sequence is supplied, so every value here is int()- or
    date-coerced and inlined instead. Adding a params tuple to any execute()
    below breaks the whole function with a confusing IndexError — see the
    empty-tuple-percent trap. This is the same constraint canonical_benchmarks
    documents on its own p50 query.
    """
    out = {
        "weeks": [], "current_week_partial": None, "wow": None,
        "robust_wow": None, "wow_baseline_check": None,
        "parity_rolling_7d": None, "degraded": False, "reason": None,
    }
    c = _conn()
    if c is None:
        # No 0s, no empty series presented as "no traffic". Unreachable
        # database means UNKNOWN, and it says so in the payload.
        out["degraded"] = True
        out["reason"] = "db unavailable — the series is unknown, not empty"
        return out

    where = " AND ".join(_window_filters(weeks))
    pop = " AND ".join(_population_filters())
    try:
        with c.cursor() as cur:
            # Pin the session to UTC so date_trunc('week', ...) lands on the
            # same Monday the payload claims, whatever the server locale is.
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("SET statement_timeout = '%d'" % _STATEMENT_TIMEOUT_MS)

            cur.execute("SELECT date_trunc('week', now())::date, now()")
            cur_week, now_ts = cur.fetchone()

            cur.execute(
                "SELECT date_trunc('week', created_at)::date AS week_start,"
                "       COUNT(DISTINCT agent_id) FILTER (WHERE " + pop + ")"
                "         AS agents,"
                "       COUNT(*) FILTER (WHERE " + pop + ") AS calls,"
                "       COUNT(*) AS rows_observed"
                "  FROM " + _TABLE +
                " WHERE " + where +
                " GROUP BY 1 ORDER BY 1"
            )
            fetched = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

            # ★★★ r-net-of-top (2026-08-24). THE SERIES COULD NOT ANSWER THE
            # QUESTION IT EXISTS FOR.
            #
            # Asked "is real demand declining?", this endpoint could not say.
            # Every week before 2026-08-18 counts our own GitHub Actions (see
            # definition_changes) and EVERY week, before and after, is
            # dominated by one hosted registry gateway — 92.3% of calls in the
            # week of 08-17, from a single IP. A series where one caller is
            # nine tenths of every point is that caller's cadence wearing a
            # trend's clothes.
            #
            # So publish the remainder PER WEEK, from the same rows, with the
            # dominant caller NAMED so a reader can see whether it is the same
            # one across weeks. If it is, the net column IS "demand without
            # that gateway" and the names are the proof; if it changes, the
            # column says so instead of quietly comparing two different
            # subtractions.
            #
            # ★ Reuses the `where` and `pop` STRINGS already built above —
            # not a second call to the filter functions — so this query and
            # the series above cannot drift onto different populations. `pop`
            # moves into the WHERE here (the main query needs it as a FILTER
            # to keep rows_observed; this one only wants the population).
            #
            # ★ The subtrahend excludes the NULL-agent_id (Cloudflare POP)
            # bucket while the total keeps it, exactly as
            # canonical_top_caller_sql does — so calls_net_of_top includes
            # CF-POP rows by design and top + net == calls holds.
            net_rows = {}
            try:
                cur.execute(
                    "WITH per AS ("
                    "  SELECT date_trunc('week', created_at)::date AS wk,"
                    "         agent_id, COUNT(*) AS n,"
                    "         MODE() WITHIN GROUP (ORDER BY COALESCE("
                    "           NULLIF(client_name, ''), platform, 'unknown'))"
                    "           AS nm"
                    "    FROM " + _TABLE +
                    "   WHERE " + where + " AND " + pop +
                    "   GROUP BY 1, 2),"
                    " agg AS ("
                    "  SELECT wk, COALESCE(SUM(n), 0) AS calls,"
                    "         COALESCE(MAX(n) FILTER"
                    "           (WHERE agent_id IS NOT NULL), 0) AS top_calls,"
                    "         COUNT(*) FILTER"
                    "           (WHERE agent_id IS NOT NULL) AS agents"
                    "    FROM per GROUP BY wk),"
                    " top AS ("
                    "  SELECT DISTINCT ON (wk) wk, nm FROM per"
                    "   WHERE agent_id IS NOT NULL ORDER BY wk, n DESC)"
                    " SELECT a.wk, a.calls, a.top_calls,"
                    "        a.calls - a.top_calls AS net_calls, a.agents,"
                    "        GREATEST(a.agents - 1, 0) AS net_agents, t.nm"
                    "   FROM agg a LEFT JOIN top t ON t.wk = a.wk"
                    "  ORDER BY a.wk"
                )
                net_rows = {r[0]: r[1:] for r in (cur.fetchall() or [])}
            except Exception:
                # Additive: a failure here must not cost the caller the series
                # itself. Weeks simply carry no concentration fields, which
                # _assemble renders as absent rather than as zero.
                net_rows = {}

            # ★ QA sweep 2026-09-02 (F7): net of the named verification
            # HARNESSES, per week, same `where` + `pop` strings as above so it
            # cannot drift onto a different population. Additive and
            # self-isolated like the net-of-top query: a failure costs the
            # reader these three keys and nothing else. The predicate is
            # exact-name IN (...) — no LIKE, no %, so it is safe inlined.
            harness_rows = {}
            try:
                from mcp_calls_deloop import harness_predicate as _hp_fn
                _hp = _hp_fn()
                cur.execute(
                    "SELECT date_trunc('week', created_at)::date AS wk,"
                    "       COUNT(*) FILTER (WHERE NOT (" + _hp + ")) AS net_calls,"
                    "       COUNT(DISTINCT agent_id) FILTER (WHERE NOT (" + _hp + "))"
                    "         AS net_agents,"
                    "       COUNT(*) FILTER (WHERE " + _hp + ") AS harness_calls"
                    "  FROM " + _TABLE +
                    " WHERE " + where + " AND " + pop +
                    " GROUP BY 1 ORDER BY 1"
                )
                harness_rows = {r[0]: r[1:] for r in (cur.fetchall() or [])}
            except Exception:
                harness_rows = {}

            # The live week, on the same population, from its own query.
            cur.execute(
                "SELECT COUNT(DISTINCT agent_id), COUNT(*)"
                "  FROM " + _TABLE +
                " WHERE created_at >= date_trunc('week', now())"
                "   AND " + pop
            )
            prow = cur.fetchone() or (None, None)

            # Parity: the canonical ROLLING 7d, run from the shared helper so
            # this endpoint cannot quietly disagree with the funnel headline.
            cur.execute(canonical_external_activity_sql(7))
            rrow = cur.fetchone() or (None, None)
    except Exception as exc:  # pragma: no cover - defensive
        out["degraded"] = True
        out["reason"] = f"query failed: {type(exc).__name__}"
        return out
    finally:
        # _conn() hands back a RAW connection, not a context manager.
        try:
            c.close()
        except Exception:
            pass

    out["weeks"] = _assemble(fetched, _week_starts(cur_week, weeks), net_rows,
                             harness=harness_rows)
    out["current_week_partial"] = _partial_week(
        cur_week, prow[0], prow[1], now_ts)
    out["wow"] = _wow(out["weeks"])
    # Both published. `wow` keeps its key and meaning for existing consumers;
    # `robust_wow` is the one to quote, and `wow_baseline_check` is what makes
    # the two legible side by side.
    out["robust_wow"] = _robust_wow(out["weeks"])
    out["wow_baseline_check"] = _baseline_outlier_flag(out["weeks"])
    out["parity_rolling_7d"] = {
        "agents": None if rrow[0] is None else int(rrow[0]),
        "calls": None if rrow[1] is None else int(rrow[1]),
        "window": "rolling 7 days ending now — NOT an ISO week",
        "note": (
            "the same basis as the series above on a DIFFERENT window, "
            "published for parity per mcp_calls_deloop.CANONICAL_AGENTS_BASIS. "
            "It overlaps the in-progress week, so it moves between requests "
            "and must not be used as a week-over-week baseline — that is the "
            "defect this endpoint exists to fix"
        ),
    }
    return out


@weekly_series_bp.route("/api/v1/reports/weekly-series", methods=["GET"])
def weekly_series():
    weeks = _clamp_weeks(request.args.get("weeks"))
    payload = _run(weeks)
    payload["basis"] = CANONICAL_AGENTS_BASIS
    payload["population"] = _population(weeks)
    payload["weeks_requested"] = weeks
    # Every marker, not only the ones inside the requested window: a reader
    # widening `weeks` must be able to see what is about to enter the series,
    # and a reader NARROWING it must not be able to hide a break by asking for
    # fewer weeks.
    payload["definition_changes_all"] = _DEFINITION_CHANGES
    payload["how_to_read"] = (
        "weeks[] holds only COMPLETE ISO weeks and is the only key a "
        "week-over-week claim may be computed from. agents=null means the "
        "week was not observed — it does NOT mean zero. The live week is on "
        "current_week_partial with partial=true and is excluded from wow by "
        "construction. parity_rolling_7d is a different window and is not "
        "comparable to any single week. ★ Before quoting any delta, check "
        "`comparability.crosses_definition_change` on it: a week-over-week "
        "percentage across a change in what is COUNTED is arithmetic, not a "
        "trend. ★★ Each measured week also carries top_caller_pct / "
        "top_caller_client / calls_net_of_top / agents_net_of_top: the "
        "dominant single caller in THAT week and the remainder after it, from "
        "the same rows (top_caller_calls + calls_net_of_top == calls). The "
        "subtracted caller is the largest in each week INDEPENDENTLY and can "
        "differ between weeks — compare top_caller_client across the series "
        "before reading calls_net_of_top as one continuous quantity. It is an "
        "arithmetic companion, not a separate population: exactly one caller "
        "is removed, and registry gateways are kept in the population on "
        "purpose (mcp_calls_deloop._AMBIGUOUS_NOT_EXCLUDED) because a false "
        "exclusion deletes a real customer. Weeks with no calls carry none of "
        "these keys — a share of zero is undefined, not 0%."
    )
    # Fail-soft 200: a degraded read publishes nulls and says why, which is
    # more useful to a partner than a 500 with no population attached.
    return jsonify(payload), 200
