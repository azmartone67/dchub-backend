"""Guard: the ingest order must make progress MONOTONIC under repeated kills.

WHAT THIS PINS
──────────────
_ingest_order() used to be `sorted(SOURCES, key=load-first)` with declaration
order as the stable tie-break. That made the TAIL of the load group unreachable
in practice, not merely last. Measured live 2026-07-30:

  · comed_ev_load and nvenergy_lhc had been configured, correct and live in
    SOURCES for days with ZERO rows in hosting_capacity_feeders.
  · They sit 11th and 12th of 12 load sources, behind ~240k rows of
    Dominion / SCE / SDG&E / PSE&G.
  · A FORCED run (POST /api/v1/grid/hosting-capacity/ingest?force=1) reached
    exactly ONE source — aep_load, position 1 — wrote 20,000 rows at 08:05:23,
    and died.
  · It died because this ingest is an in-process daemon THREAD ON THE WEB
    SERVICE, and the web service redeployed at 08:06:44. Railway showed SIX
    deploys in ELEVEN MINUTES that morning. The budget is 300s.

Fixed declaration order + a process that rarely survives a full pass = every run
restarts at position 1, re-does the same head, and the tail is never reached.
That is a STRUCTURAL starvation, so raising HOSTING_CAPACITY_INGEST_BUDGET_S is
not the fix — it only widens the window a deploy has to hit.

Staleness-first ordering makes the frontier advance no matter how often the
process dies: whatever a killed run did not refresh is what the next one starts
with. A never-ingested source sorts FIRST, which is exactly the ComEd /
NV Energy / SDG&E case.

THE CONTRACT
────────────
  O1. A never-ingested source is ordered BEFORE any source that has rows.
  O2. Within the same capacity_type, staler comes first.
  O3. LOAD still outranks gen/bus_headroom — the draw-side answer is the one
      that converts, and that priority is unchanged.
  O4. Fail-soft: with no freshness data (DB down) the order must still be a
      complete permutation of SOURCES and must not raise.
  O5. Every source appears EXACTLY once. An ordering that drops or duplicates a
      source silently changes what gets ingested.
  O6. Naive/aware datetime mixing must not raise — the column is timestamptz but
      a naive value must still sort.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
Measured by extracting origin/main with `git archive`, dropping this file into
that tree, and running it there.

UNPATCHED (origin/main @ 4815f45f):  2 failed, 4 passed, 1 xfailed
    O1 and O2 fail — the old key is `0 if load else 1` only, so freshness is
    ignored entirely and a never-ingested source keeps its declaration slot.
    The unpatched O2 failure prints the whole thing plainly:
        got:  [aep_load, ameren_il_load, ..., comed_ev_load, nvenergy_lhc]
        want: [nvenergy_lhc, comed_ev_load, ..., ameren_il_load, aep_load]
    O3/O4/O5 pass in BOTH states — they pin properties the old order also had,
    and exist so a rewrite cannot quietly lose them.
    ★ O6 also passes unpatched, and that is correct rather than a weak
    assertion: the old key never touches a timestamp, so it cannot raise on
    mixed naive/aware values. O6 guards the NEW comparison, and it would have
    caught the obvious first draft of it (`seen or epoch` with no tzinfo
    normalisation raises TypeError against a naive column value).
PATCHED (this branch):               0 failed, 6 passed, 1 xfailed

`1 xfailed` on BOTH runs — the strict-xfail control is collected in each, so a
conftest-level abort (rc 0, 0 tests) cannot read as green.

House rules: no import of main, nothing at module scope, and the function is
AST-extracted (asserting the parse produced a Module with a non-empty body, and
that every free variable resolves) because the module imports flask at import
time.

Run:  python3 -m pytest tests/test_hosting_capacity_ingest_order.py -v
"""
from __future__ import annotations

import ast
import builtins
import datetime
import os
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INGEST = _ROOT / "routes" / "hosting_capacity_ingest.py"

_FN = "_ingest_order"
# The three that were configured-but-empty when this guard was written.
_WERE_STARVED = ("comed_ev_load", "nvenergy_lhc", "sdge_ica_load")


def _tree():
    text = _INGEST.read_text(encoding="utf-8")
    assert text.strip(), "ingest module is empty — nothing to guard"
    tree = ast.parse(text)
    assert isinstance(tree, ast.Module), "ast.parse did not yield a Module"
    assert tree.body, "ast.parse yielded an EMPTY module — extractor is broken"
    return tree


def _free_vars(fn_node):
    assigned, loaded = set(), set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Name):
            (assigned if isinstance(n.ctx, ast.Store) else loaded).add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                assigned.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.Lambda)):
            args = n.args
            assigned.update(a.arg for a in (list(args.posonlyargs)
                                            + list(args.args)
                                            + list(args.kwonlyargs)))
            for v in (args.vararg, args.kwarg):
                if v:
                    assigned.add(v.arg)
            if isinstance(n, ast.FunctionDef) and n is not fn_node:
                assigned.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
    return loaded - assigned - set(dir(builtins))


def _sources():
    """SOURCES only. Entries call int(os.environ.get(...)), so `os` is supplied
    and any OTHER free name raises NameError here rather than going untested."""
    node = next((s for s in _tree().body
                 if isinstance(s, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "SOURCES"
                         for t in s.targets)), None)
    assert node is not None, "SOURCES assignment not found"
    ns = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<src>", "exec"), ns)
    srcs = ns["SOURCES"]
    assert isinstance(srcs, list) and len(srcs) >= 18, (
        "SOURCES extracted %r — the extraction is broken, not the config"
        % (len(srcs) if isinstance(srcs, list) else type(srcs)))
    return srcs


def _order_fn(last_ingested: dict):
    """The REAL _ingest_order(), with its freshness lookup stubbed.

    Free variables are DERIVED from the function, so a new dependency is
    supplied automatically and a genuinely unresolvable one fails loudly.
    """
    tree = _tree()
    fn_node = next((s for s in tree.body
                    if isinstance(s, ast.FunctionDef) and s.name == _FN), None)
    assert fn_node is not None, "%s() not found in the ingest module" % _FN
    assert fn_node.body, "%s() parsed with an EMPTY body" % _FN

    free = _free_vars(fn_node)
    ns = {
        "SOURCES": _sources(),
        "datetime": datetime,
        "os": os,
        # The freshness read is the seam: stub it so no DB is needed.
        "_source_last_ingested": lambda: dict(last_ingested),
    }
    exec(compile(ast.Module(body=[fn_node], type_ignores=[]), "<order>", "exec"), ns)
    unresolved = free - set(ns)
    assert not unresolved, (
        "unresolved free vars in %s: %s — the extract would NameError or, worse, "
        "silently not exercise a branch" % (_FN, sorted(unresolved)))
    return ns[_FN]


def _keys(ordered):
    return [s["key"] for s in ordered]


def _by_key():
    return {s["key"]: s for s in _sources()}


# ── O5 + O4: properties the OLD order also had (pass in both states) ─────────
def test_order_is_a_complete_permutation_of_sources():
    srcs = _sources()
    for last in ({}, {s["utility"]: datetime.datetime(2026, 1, 1) for s in srcs}):
        ordered = _order_fn(last)()
        assert len(ordered) == len(srcs), (
            "order returned %d of %d sources — an ordering that drops one "
            "silently changes what gets ingested" % (len(ordered), len(srcs)))
        assert sorted(_keys(ordered)) == sorted(s["key"] for s in srcs), \
            "order is not a permutation of SOURCES"


def test_no_freshness_data_degrades_instead_of_raising():
    """DB down → every source sorts equal, i.e. the previous behaviour."""
    ordered = _order_fn({})()
    assert _keys(ordered), "empty order with no freshness data"
    loads = [s for s in ordered if s.get("capacity_type") == "load"]
    assert loads, "no load sources survived the fail-soft path"


def test_load_outranks_gen_only_as_a_tie_break():
    """O3 — REVERSED 2026-07-30, deliberately, on measured evidence.

    This test used to assert the opposite: "the type rank must dominate
    staleness". That invariant is what starved the whole gen group. Measured
    live a few hours after the staleness-inside-group fix shipped:

        every `load` source   last ingested 2026-07-30 09:51   (today)
        every `gen`  source   last ingested 2026-07-27 07:37   (3 days old)
        14 gen utilities, not one refreshed in three days
        two brand-new Ausgrid GEN sources never ran at all, while the two
        Ausgrid LOAD sources landed on the first pass

    A hard type priority plus a budget smaller than the priority group does not
    make the second group LAST, it makes it UNREACHABLE — the same shape as the
    declaration-order bug this file was originally written for, one level up.

    So staleness is primary and the type rank is the TIE-BREAK. Load priority
    still applies where it actually matters: on a cold start every source is at
    epoch, so the tie-break puts the draw-side answer first.
    """
    srcs = _sources()
    fresh = datetime.datetime(2026, 7, 30, tzinfo=datetime.timezone.utc)
    stale = datetime.datetime(2026, 7, 27, tzinfo=datetime.timezone.utc)
    last = {s["utility"]: (fresh if s.get("capacity_type") == "load" else stale)
            for s in srcs}
    ordered = _order_fn(last)()
    types = [s.get("capacity_type") for s in ordered]
    first_load = next((i for i, t in enumerate(types) if t == "load"), len(types))
    assert "load" not in types[:first_load], "internal: bad index"
    assert first_load > 0, (
        "a FRESH load source still ran before a 3-day-stale gen source — that "
        "is the starvation this change removes: %s" % types)

    # ...and the tie-break still favours load when staleness is equal, which is
    # every cold start.
    epoch_all = {}
    ordered2 = _order_fn(epoch_all)()
    types2 = [s.get("capacity_type") for s in ordered2]
    first_non_load = next((i for i, t in enumerate(types2) if t != "load"),
                          len(types2))
    assert "load" not in types2[first_non_load:], (
        "with all sources equally stale the tie-break must still put load "
        "first: %s" % types2)


def test_no_capacity_type_group_can_be_starved_by_another_growing():
    """The generalisation: adding load sources must not push gen out of reach.

    Simulates the live situation — a big load group all refreshed today, a gen
    group three days old — and asserts the stalest work is scheduled first
    regardless of type. Pins the property, not the current source list.
    """
    srcs = _sources()
    fresh = datetime.datetime(2026, 7, 30, 9, 51, tzinfo=datetime.timezone.utc)
    stale = datetime.datetime(2026, 7, 27, 7, 37, tzinfo=datetime.timezone.utc)
    last = {}
    for s in srcs:
        last[s["utility"]] = fresh if s.get("capacity_type") == "load" else stale
    ordered = _order_fn(last)()
    n_gen = sum(1 for s in srcs if s.get("capacity_type") != "load")
    head = ordered[:n_gen]
    assert all(s.get("capacity_type") != "load" for s in head), (
        "the stale non-load group is not scheduled first: %s"
        % [(s["key"], s.get("capacity_type")) for s in head])
    # and a never-ingested source outranks everything, whatever its type
    last2 = dict(last)
    victim = next(s for s in srcs if s.get("capacity_type") == "gen")
    last2.pop(victim["utility"], None)
    assert _order_fn(last2)()[0]["key"] == victim["key"], (
        "a never-ingested gen source did not sort first")


# ── O1: the actual fix ───────────────────────────────────────────────────────
def test_never_ingested_sources_are_ordered_first():
    srcs = _sources()
    # everything has rows EXCEPT the three that were starved live
    by_key = _by_key()
    now = datetime.datetime(2026, 7, 30, 8, 5, tzinfo=datetime.timezone.utc)
    last = {s["utility"]: now for s in srcs
            if s["key"] not in _WERE_STARVED}
    ordered = _keys(_order_fn(last)())
    starved_present = [k for k in _WERE_STARVED if k in by_key]
    assert starved_present, "none of the starved keys are configured any more"
    positions = {k: ordered.index(k) for k in starved_present}
    ingested_load = [k for k in ordered
                     if by_key[k].get("capacity_type") == "load"
                     and k not in starved_present]
    assert ingested_load, "harness supplied no already-ingested load source"
    first_ingested = min(ordered.index(k) for k in ingested_load)
    for k, pos in positions.items():
        assert pos < first_ingested, (
            "%s has NEVER been ingested but is ordered at %d, behind an "
            "already-ingested load source at %d. That is the structural "
            "starvation: a run that dies mid-pass (six web deploys in eleven "
            "minutes, 300s budget) restarts at the head and never reaches it."
            % (k, pos, first_ingested))


# ── O2 ───────────────────────────────────────────────────────────────────────
def test_staler_sources_come_first_within_a_type():
    srcs = _sources()
    loads = [s for s in srcs if s.get("capacity_type") == "load"]
    assert len(loads) >= 3, "need >=3 load sources to test staleness ordering"
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    # give each load source a DISTINCT age, deliberately the REVERSE of
    # declaration order, so declaration order cannot satisfy this by accident
    last = {}
    for i, s in enumerate(loads):
        last[s["utility"]] = base + datetime.timedelta(days=len(loads) - i)
    ordered = [s for s in _order_fn(last)()
               if s.get("capacity_type") == "load"]
    got = _keys(ordered)
    want = _keys(sorted(loads, key=lambda s: last[s["utility"]]))
    assert got == want, (
        "load sources are not stalest-first.\n  got:  %s\n  want: %s" % (got, want))


# ── O6 ───────────────────────────────────────────────────────────────────────
def test_naive_timestamps_do_not_raise():
    """ingested_at is timestamptz, but a naive value must still sort.

    Mixing naive and aware datetimes raises TypeError on comparison, which in
    _ingest_order() would take the whole ingest down rather than misorder it.
    """
    srcs = _sources()
    last = {}
    for i, s in enumerate(srcs):
        # alternate naive / aware
        t = datetime.datetime(2026, 7, 1 + (i % 20))
        last[s["utility"]] = t if i % 2 else t.replace(
            tzinfo=datetime.timezone.utc)
    ordered = _order_fn(last)()          # must not raise
    assert len(ordered) == len(srcs)


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
