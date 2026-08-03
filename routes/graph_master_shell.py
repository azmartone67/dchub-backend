"""Graph Master Shell (#49) — 2026-08-02.

Every other shell asks whether a NODE is healthy. This one asks whether the
EDGES exist at all — because in every case below the pieces are individually
fine and only the join is missing, which is the exact failure shape that has
cost this codebase the most and stayed invisible the longest.

★WHY EACH LANE EXISTS — every one is a failure that already happened, or a
missing edge that made one unfindable:

  1 LOOP EDGES   — system_loops.py surveys 7 loops and each reports
                   alive|stale|dead against its OWN cadence. There are ZERO
                   edges. Nothing records that a downstream loop consumes an
                   upstream one, so when the upstream dies the downstream still
                   fires on schedule, still writes rows, and still reports
                   ALIVE — a green board over stale input. This is also what
                   makes the #48 sweep's dead-cron finding unrankable: a cron
                   with no downstream consumer and one that feeds DCPI are the
                   same severity today, because nothing knows the difference.

  2 CAUSAL EDGES — brain_layer14_causal ALREADY computes root-cause chains, and
                   persists NONE of them: the result is a Claude narrative
                   cached for an hour. So brain_work_selector ranks a FLAT
                   candidate list and can spend its whole MAX_DRAFT_PRS_PER_RUN
                   budget on five symptoms of one cause. The graph is computed
                   and then thrown away every tick.

  3 TYPED NODES  — the #48 misread in one sentence: `count` is per-detector
                   free-form, brain_consistency_radar.py:7419 writes SECONDS
                   into it, and impact_weight() read 477,455 seconds as 477,455
                   occurrences. The fix that shipped was to add one string to a
                   hand-maintained 3-tuple (VALUE_NOT_COUNT_ISSUES). That is a
                   list someone must remember to edit — it has now been edited
                   three times, once per recurrence of the SAME class. The
                   radar knows the answer at write time (two sites already
                   annotate `count_kind`) and the type is dropped at the write
                   boundary. A typed node makes the allowlist DERIVED.

  4 IDENTITY     — partner -> key -> agent -> call -> outcome is an entity
                   graph that exists only as separate counts, which is why one
                   payload carries real_external_7d=2637 next to
                   real_external_calls_7d=8641, why three surfaces answer 62 /
                   99 / 99, and why ~75 of 99 "real external agents" are an
                   enumeration signature nobody can split from demand.

  5 ORCHESTRATOR — brain_master_orchestrator chains 8-10 serial self-HTTP calls
                   at a 90s budget each, tier order hardcoded. No node declares
                   what it depends on, so nothing can run independent tiers in
                   parallel and a failure at step 8 re-runs steps 1-7.

★HONESTY RULE (inherited from Integrity #25 / Loop Control #48): a lane must
never read PASS when it could not check. Schema-dependent lanes introspect
their columns at runtime and degrade to "?" rather than guess. Known-but-unfixed
work renders FAIL, never green-by-silence — several lanes here are red on
purpose and name the undone work.

★This module is also the CANONICAL HOME of the declared graph: LOOP_EDGES,
FINDING_EDGE_KINDS and ORCHESTRATOR_NODE_COUNT are importable constants so the
wiring work each lane names has one place to read from instead of re-deriving.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.

Endpoints:
  GET/POST /api/v1/admin/graph/master-tick   JSON scoreboard (5 lanes)
  GET      /admin/graph                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/graph                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as the other master shells.
Kill: GRAPH_SHELL_DISABLE=1
"""
from __future__ import annotations

import logging
import os
import re
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
graph_master_shell_bp = Blueprint("graph_master_shell", __name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Synthetic callers excluded from every identity measure (same list the
# white-glove shell uses — kept in sync deliberately, not imported, so a
# refactor there cannot silently change what this shell counts).
_SYNTH = ("dchub-internal", "dchub-selfheal", "value-harness",
          "fixwave-probe", "reviewer-sim")

# ── THE DECLARED LOOP GRAPH ───────────────────────────────────────────
# ★This is the artifact, not just the board. system_loops.py has no edge set
# at all; these are the first four, each carrying the BASIS it is asserted on.
#
# `evidence` is the honest part:
#   "code"     — the dependency is visible in the source and _edge_evidenced()
#                re-checks it on every tick, so a refactor that removes it
#                turns the edge red instead of leaving a lie in a tuple.
#   "declared" — asserted from the subsystem's documented behaviour and NOT yet
#                proven by a code read. A declared edge is a hypothesis. Lane 1
#                is red while any edge is still merely declared, because an
#                edge you cannot verify is worse than no edge: it will be
#                trusted exactly as much as a real one.
LOOP_EDGES = (
    {
        "producer": "mcp_traffic",
        "consumer": "brain_learn",
        "kind": "probe",
        "evidence": "code",
        # _probe_brain_learn falls back to counting mcp_tool_calls rows to
        # decide whether a silent brain hour is "idle" (backend alive, nothing
        # novel) or "dead". So brain_learn's STATUS is derived from
        # mcp_traffic: if traffic stops, brain_learn is misclassified.
        # ★Anchored to the function BODY: `(?:(?!\ndef ).)*?` stops at the next
        # top-level def, so the marker cannot quietly match an mcp_tool_calls
        # read in some later probe and report the edge as proven after the real
        # dependency has been refactored out.
        "marker": r"_probe_brain_learn(?:(?!\ndef ).)*?mcp_tool_calls",
        "basis": "routes/system_loops.py::_probe_brain_learn reads mcp_tool_calls "
                 "as its backend-alive proxy, so brain_learn's own status is a "
                 "function of mcp_traffic being alive.",
    },
    {
        "producer": "iso_extract",
        "consumer": "dcpi_recompute",
        "kind": "data",
        "evidence": "declared",
        "marker": None,
        "basis": "DCPI market scores incorporate ISO/grid inputs refreshed by "
                 "iso_extract. NOT yet proven by a code read — name the table "
                 "dcpi_recompute reads and promote this to evidence=code.",
    },
    {
        "producer": "dcpi_recompute",
        "consumer": "auto_press_daily",
        "kind": "data",
        "evidence": "declared",
        "marker": None,
        "basis": "auto_dcpi is a live post_type (14 posts/30d measured 07-30), "
                 "so press output is generated off DCPI scores. NOT yet proven "
                 "by a code read.",
    },
    {
        "producer": "mcp_traffic",
        "consumer": "engagement_track",
        "kind": "data",
        "evidence": "declared",
        "marker": None,
        "basis": "Engagement pixels fire against surfaces that agent traffic "
                 "drives. WEAKEST edge in the set — if it cannot be evidenced "
                 "it should be DELETED, not left as decoration.",
    },
)

# ── THE FINDING EDGE VOCABULARY ───────────────────────────────────────
# What brain_layer14_causal already reasons about in prose. Persisting these as
# rows in brain_finding_edges(cause_id, effect_id, kind, confidence) is what
# lane 2 asks for; naming the kinds here keeps L14's writer and the selector's
# reader from inventing two different vocabularies.
FINDING_EDGE_KINDS = ("causes", "duplicates", "blocks", "symptom_of")

# Worst-case serial budget of the master-tick, from _call()'s default timeout.
_ORCH_STEP_TIMEOUT_S = 90
# Above this the tick cannot finish inside a cron window without overlapping
# its own next fire.
_ORCH_WALLCLOCK_CEILING_S = 600


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _disabled() -> bool:
    return (os.environ.get("GRAPH_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[graph] db connect failed: %s", str(e)[:120])
        return None


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:600], "critical": critical}


def _verdict(checks):
    d = [c for c in checks if c["pass"] is not None]
    return all(c["pass"] for c in d) if d else None


def _one(cur, sql, args=None):
    """Scalar or None. ★Any literal % in `sql` must be DOUBLED: psycopg2 runs
    %-interpolation even against an empty tuple, and a swallowed IndexError
    reports UNMEASURED against a query that would have worked."""
    try:
        cur.execute(sql, args or ())
        r = cur.fetchone()
        return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[graph] query failed: %s", str(e)[:140])
        return None


def _src(*parts):
    """Source of a shipped file, or None. Lanes that cannot read their source
    render '?' rather than assuming the code says what they hope."""
    try:
        with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()
    except Exception as e:  # noqa: BLE001
        logger.debug("[graph] source read failed %s: %s", parts, str(e)[:120])
        return None


def _has_column(cur, table: str, column: str):
    """True/False, or None when information_schema itself is unreadable.
    ★None is NOT False — 'the column is missing' and 'I could not look' are
    different answers and only one of them is a finding."""
    return _one(cur, """SELECT COUNT(*) FROM information_schema.columns
                         WHERE table_name = %s AND column_name = %s""",
                (table, column))


def _has_table(cur, table: str):
    return _one(cur, """SELECT COUNT(*) FROM information_schema.tables
                         WHERE table_name = %s""", (table,))


def _call_column(cur):
    """Which column on mcp_tool_calls names the tool. Measured, never guessed:
    the tree uses `tool` in ~163 places and `tool_name` in one, so a hardcoded
    guess has a real chance of being wrong on the deployed schema."""
    for cand in ("tool", "tool_name"):
        n = _has_column(cur, "mcp_tool_calls", cand)
        if n is None:
            return None
        if int(n) > 0:
            return cand
    return ""


# ── lane 1 · loop edges — who feeds whom ──────────────────────────────
def _edge_evidenced(edge, sys_src) -> bool:
    """Re-prove a code-evidenced edge on every tick. An edge asserted once and
    never re-checked decays into folklore the first time someone refactors."""
    if edge.get("evidence") != "code" or not edge.get("marker"):
        return False
    if not sys_src:
        return False
    return bool(re.search(edge["marker"], sys_src, re.S))


def _lane_loop_edges(cur) -> list:
    out = []
    sys_src = _src("routes", "system_loops.py")

    out.append(_check(
        "edges_declared", "the loop graph is declared at all",
        len(LOOP_EDGES) > 0,
        f"{len(LOOP_EDGES)} edge(s) declared in graph_master_shell.LOOP_EDGES. "
        f"Before this module the count was ZERO: system_loops.py surveys 7 "
        f"loops with no notion that any of them feeds another."))

    # Drift guard: an edge naming a loop that no probe emits is dead weight and
    # will silently stop being checked.
    if sys_src is None:
        out.append(_check("edges_resolve", "every edge names a real loop", None,
                          "routes/system_loops.py unreadable — UNMEASURED"))
    else:
        known = set(re.findall(r'"name":\s*"([a-z0-9_]+)"', sys_src))
        named = {e["producer"] for e in LOOP_EDGES} | {e["consumer"] for e in LOOP_EDGES}
        unknown = sorted(named - known)
        out.append(_check(
            "edges_resolve", "every edge names a loop system_loops actually probes",
            not unknown,
            f"{len(named)} endpoint name(s) referenced; "
            + ("all resolve to a live probe" if not unknown else
               f"UNRESOLVED {unknown} — the edge is pointing at a loop that no "
               f"longer exists, so it is checking nothing")))

    ev = [e for e in LOOP_EDGES if _edge_evidenced(e, sys_src)]
    declared_only = [f'{e["producer"]}->{e["consumer"]}'
                     for e in LOOP_EDGES if e not in ev]
    out.append(_check(
        "edges_evidenced", "every declared edge is PROVEN by a code read",
        not declared_only,
        f"{len(ev)}/{len(LOOP_EDGES)} edge(s) re-proven against source this "
        f"tick. Still declared-only: {declared_only or 'none'}. ★An edge you "
        f"cannot verify is worse than no edge — it gets trusted exactly as much "
        f"as a real one. Actuator: name the table the consumer reads, add it as "
        f"`marker`, promote to evidence='code' — or DELETE the edge.",
        critical=True))

    # The payoff check: a consumer reading ALIVE on top of a dead producer.
    statuses = _loop_statuses(cur)
    if statuses is None:
        out.append(_check(
            "no_stale_input", "no loop is alive on STALE INPUT", None,
            "could not run system_loops probes — UNMEASURED, not clean. "
            "(A false green here is the whole failure this lane exists to "
            "end, so it must never be reported as a pass.)", critical=True))
    else:
        bad = []
        for e in LOOP_EDGES:
            p = statuses.get(e["producer"])
            c = statuses.get(e["consumer"])
            if p in ("stale", "dead") and c in ("alive", "idle"):
                bad.append(f'{e["consumer"]} reads ALIVE on {e["producer"]}={p}')
        out.append(_check(
            "no_stale_input", "no loop is alive on STALE INPUT", not bad,
            (f"{len(statuses)} loop(s) probed; no consumer is running on a "
             f"dead producer" if not bad else
             "; ".join(bad) + " — the downstream loop is firing on schedule "
             "and writing rows off input nobody refreshed. It reports GREEN "
             "today because no edge exists to make it report anything else."),
            critical=True))

    if sys_src is None:
        out.append(_check("board_consumes_edges",
                          "the public loop board renders the edge status", None,
                          "routes/system_loops.py unreadable — UNMEASURED"))
    else:
        wired = ("graph_master_shell" in sys_src
                 or "alive_on_stale_input" in sys_src)
        out.append(_check(
            "board_consumes_edges",
            "the public loop board renders alive_on_stale_input", wired,
            "system_loops.py imports the edge set and emits the derived status"
            if wired else
            "RED BY DESIGN. system_loops.py does not import LOOP_EDGES, so "
            "/api/v1/system/loops still serves a per-loop verdict with no "
            "upstream context — this shell can see the stale-input case and "
            "the board every dashboard polls cannot. Actuator: import "
            "graph_master_shell.LOOP_EDGES in system_loops._survey and add "
            "'alive_on_stale_input' as a fourth status.",
            critical=True))
    return out


def _loop_statuses(cur):
    """{loop_name: status} by RE-USING the shipped probes rather than
    re-deriving liveness. Returns None if the probes cannot be run — the
    honesty rule: an unrun probe is '?', never 'alive'."""
    # ★CANARY FIRST. The probes are written to tolerate a broken cursor: with
    # no rows they classify every loop as "dead", and an all-dead board makes
    # "no consumer is alive on a dead producer" trivially TRUE. That is a PASS
    # manufactured out of a query that never ran — the exact false green this
    # lane exists to end, one level up. Caught by
    # test_every_lane_degrades_to_unmeasured_without_a_db, not by review.
    if _one(cur, "SELECT 1") is None:
        return None
    try:
        from routes import system_loops as _sl
    except Exception as e:  # noqa: BLE001
        logger.debug("[graph] system_loops import failed: %s", str(e)[:120])
        return None
    probes = [getattr(_sl, n) for n in dir(_sl) if n.startswith("_probe_")]
    if not probes:
        return None
    out = {}
    for p in probes:
        try:
            r = p(cur) or {}
            name, status = r.get("name"), r.get("status")
            if name and status:
                out[name] = status
        except Exception as e:  # noqa: BLE001
            logger.debug("[graph] probe %s failed: %s",
                         getattr(p, "__name__", "?"), str(e)[:120])
    return out or None


# ── lane 2 · causal edges — computed, then thrown away ────────────────
def _lane_causal_edges(cur) -> list:
    out = []
    tbl = _has_table(cur, "brain_finding_edges")
    if tbl is None:
        out.append(_check("edge_store", "causal edges have somewhere to live",
                          None, "information_schema unreadable — UNMEASURED",
                          critical=True))
    else:
        out.append(_check(
            "edge_store", "causal edges have somewhere to live", int(tbl) > 0,
            "brain_finding_edges exists" if int(tbl) > 0 else
            "no brain_finding_edges table. Actuator: "
            "brain_finding_edges(cause_id, effect_id, kind, confidence) with "
            f"kind in {list(FINDING_EDGE_KINDS)} — the vocabulary is declared "
            f"in graph_master_shell.FINDING_EDGE_KINDS so the L14 writer and "
            f"the selector's reader cannot invent two different ones.",
            critical=True))

    l14 = _src("routes", "brain_layer14_causal.py")
    if l14 is None:
        out.append(_check("l14_persists", "L14 PERSISTS the chains it computes",
                          None, "brain_layer14_causal.py unreadable — UNMEASURED"))
    else:
        persists = "brain_finding_edges" in l14
        out.append(_check(
            "l14_persists", "L14 PERSISTS the chains it computes", persists,
            "L14 writes its edges" if persists else
            "RED BY DESIGN. L14 joins related findings, calls Claude for the "
            "ROOT CAUSE, and stores the answer as a NARRATIVE cached for an "
            "hour. The graph is computed every run and discarded every run — "
            "the most expensive step in the pipeline is also the one whose "
            "output nothing downstream can read. Actuator: write one row per "
            "chain link into brain_finding_edges alongside the narrative.",
            critical=True))

    sel = _src("routes", "brain_work_selector.py")
    if sel is None:
        out.append(_check("selector_reads_edges",
                          "the selector ranks by ROOT, not by symptom", None,
                          "brain_work_selector.py unreadable — UNMEASURED"))
    else:
        reads = "brain_finding_edges" in sel or "root_cause" in sel
        out.append(_check(
            "selector_reads_edges",
            "the selector ranks by ROOT, not by symptom", reads,
            "the selector reads the edge set" if reads else
            "rank_work() sees a FLAT list: leverage_score() has no way to know "
            "that five candidates are one cause, so a run can spend its whole "
            "MAX_DRAFT_PRS_PER_RUN budget on symptoms and land nothing. "
            "Actuator: collapse each connected component to its root BEFORE "
            "the existing rate cap slices the list — rank-only, still never "
            "drops a candidate, so the safety property already documented in "
            "brain_work_selector holds unchanged.",
            critical=True))

    # Scale: how much unstructured backlog this is being asked to organise.
    open_f = _one(cur, """SELECT COUNT(*) FROM brain_findings
                           WHERE COALESCE(status,'open') NOT IN
                                 ('resolved','closed','dismissed')""")
    edges_n = None
    if tbl is not None and int(tbl or 0) > 0:
        edges_n = _one(cur, "SELECT COUNT(*) FROM brain_finding_edges")
    if open_f is None:
        out.append(_check("edge_coverage", "open findings carry causal edges",
                          None, "brain_findings unreadable — UNMEASURED"))
    else:
        n = int(open_f)
        e = int(edges_n or 0)
        out.append(_check(
            "edge_coverage", "open findings carry causal edges", e > 0,
            f"{e} edge(s) across {n} open finding(s) "
            f"({(e / n * 100) if n else 0:.1f} per 100). Every one of those "
            f"findings is currently ranked as if it were independent."))
    return out


# ── lane 3 · typed finding nodes — the #48 class, structurally ────────
def _lane_typed_nodes(cur) -> list:
    out = []
    col = _has_column(cur, "brain_findings", "count_kind")
    if col is None:
        out.append(_check("count_kind_column",
                          "a finding's numeric TYPE survives the write", None,
                          "information_schema unreadable — UNMEASURED",
                          critical=True))
    else:
        out.append(_check(
            "count_kind_column", "a finding's numeric TYPE survives the write",
            int(col) > 0,
            "brain_findings.count_kind exists" if int(col) > 0 else
            "brain_findings has no count_kind column, so the two detectors "
            "that DO annotate their count type (backlog_size, latency_ms) lose "
            "it at the write boundary. The producer knows the answer and the "
            "consumer cannot ask. Actuator: add the column, write it from "
            "upsert_brain_finding(), read it in impact_weight().",
            critical=True))

    radar = _src("routes", "brain_consistency_radar.py")
    if radar is None:
        out.append(_check("count_kind_annotated",
                          "detectors declare what their count MEANS", None,
                          "brain_consistency_radar.py unreadable — UNMEASURED"))
    else:
        writes = len(re.findall(r'"count":', radar))
        kinds = len(re.findall(r'"count_kind":', radar))
        out.append(_check(
            "count_kind_annotated", "detectors declare what their count MEANS",
            kinds >= writes and writes > 0,
            f"{kinds} of {writes} count-writing site(s) in the radar declare a "
            f"count_kind. The other {max(writes - kinds, 0)} are free-form: "
            f"whether that integer is a tally, a duration or a byte count is "
            f"knowable only by reading the detector."))

    sel = _src("routes", "brain_work_selector.py")
    if sel is None:
        out.append(_check("allowlist_derived",
                          "the value-not-count guard is DERIVED", None,
                          "brain_work_selector.py unreadable — UNMEASURED"))
    else:
        m = re.search(r"VALUE_NOT_COUNT_ISSUES\s*=\s*\((.*?)\)", sel, re.S)
        listed = len(re.findall(r'"[a-z0-9_]+"', m.group(1))) if m else 0
        derived = bool(re.search(r"count_kind", sel))
        ceiling = bool(re.search(r"_untyped_ceiling|UNTYPED_OCCURRENCE_CEILING",
                                 sel))
        out.append(_check(
            "allowlist_derived",
            "the value-not-count guard is DERIVED, not hand-maintained",
            derived,
            (f"the selector reads count_kind and treats anything but "
             f"'occurrence' as a magnitude; VALUE_NOT_COUNT_ISSUES survives as "
             f"the fallback for the {listed} legacy classes and the ~193 radar "
             f"sites that have not declared a type yet"
             + (". An UNDECLARED count above the plausibility ceiling is also "
                "distrusted, so a new detector is covered before anyone edits "
                "a list." if ceiling else
                ". ★NO CEILING: an undeclared magnitude from a detector nobody "
                "has annotated can still buy agenda leverage."))
            if derived else
            f"VALUE_NOT_COUNT_ISSUES is a hardcoded {listed}-entry tuple and "
            f"the selector never reads count_kind. ★It has been edited three "
            f"times, once per recurrence of the SAME class "
            f"(frontend_endpoint_slow, dedup_backlog_large, "
            f"cron_silently_dead) — each time AFTER the misread had already "
            f"cost an agenda cycle. A fourth detector that writes a magnitude "
            f"into `count` is undetectable until it wins the agenda and "
            f"someone notices. Actuator: membership becomes "
            f"count_kind != 'occurrence', so a new detector is covered the "
            f"moment it declares its type instead of the moment someone "
            f"remembers this tuple.",
            critical=True))
    return out


# ── lane 4 · identity graph — one agent, one node ─────────────────────
_IDENTITY_TABLES = ("agent_identity", "agent_identities", "mcp_agent_identity")


def _lane_identity(cur) -> list:
    out = []
    found = []
    unknown = False
    for t in _IDENTITY_TABLES:
        n = _has_table(cur, t)
        if n is None:
            unknown = True
            break
        if int(n) > 0:
            found.append(t)
    if unknown:
        out.append(_check("identity_store", "resolved identities have a home",
                          None, "information_schema unreadable — UNMEASURED",
                          critical=True))
    else:
        out.append(_check(
            "identity_store", "resolved identities have a home", bool(found),
            f"found {found}" if found else
            f"none of {list(_IDENTITY_TABLES)} exists. Identity is re-derived "
            f"per surface from raw call rows, which is why the same question "
            f"gets a different answer depending on who is asking. Actuator: "
            f"one node per resolved agent (ip + api_key + user-agent + "
            f"platform), every surface a PROJECTION of it — the same "
            f"render-from-canon fix loop-control lane 4 names for facility "
            f"counts.",
            critical=True))

    # The divergence, stated in one line: three keys, three cardinalities,
    # three defensible answers to "how many agents".
    synth = "', '".join(_SYNTH)
    where = (f"created_at >= NOW() - INTERVAL '7 days' "
             f"AND COALESCE(platform,'') NOT IN ('{synth}')")
    by_ip = _one(cur, f"SELECT COUNT(DISTINCT ip_address) FROM mcp_tool_calls WHERE {where}")
    by_key = _one(cur, f"SELECT COUNT(DISTINCT api_key) FROM mcp_tool_calls WHERE {where}")
    by_plat = _one(cur, f"SELECT COUNT(DISTINCT platform) FROM mcp_tool_calls WHERE {where}")
    if by_ip is None or by_key is None or by_plat is None:
        out.append(_check("identity_agrees",
                          "the three identity keys agree on the count", None,
                          "mcp_tool_calls unreadable — UNMEASURED", critical=True))
    else:
        i, k, p = int(by_ip), int(by_key), int(by_plat)
        spread = (max(i, k) / min(i, k)) if min(i, k) else 0
        out.append(_check(
            "identity_agrees", "the three identity keys agree on the count",
            bool(min(i, k)) and spread <= 1.25,
            f"7d distinct: ip_address={i}, api_key={k}, platform={p}. These "
            f"are three different answers to 'how many agents' and every "
            f"surface picks one without saying which — the mechanism behind "
            f"agent portal 62 / reach 99 / funnel 99, and behind "
            f"real_external_7d=2637 next to real_external_calls_7d=8641 in ONE "
            f"payload. Nothing is wrong with any single number; there is no "
            f"node to make them the same number.",
            critical=True))

    tool_col = _call_column(cur)
    if not tool_col:
        out.append(_check("enumeration_split",
                          "enumeration is split from real demand", None,
                          "could not resolve the tool column on mcp_tool_calls "
                          "(tried tool, tool_name) — UNMEASURED. ★Introspected "
                          "rather than guessed: the tree uses `tool` in ~163 "
                          "places and `tool_name` in one."))
    else:
        # An enumeration signature: broad tool coverage, ~one call each. A real
        # user calls a few tools repeatedly; a crawler calls everything once.
        enum_n = _one(cur, f"""
            SELECT COUNT(*) FROM (
              SELECT ip_address
                FROM mcp_tool_calls
               WHERE created_at >= NOW() - INTERVAL '30 days'
                 AND COALESCE(platform,'') NOT IN ('{synth}')
               GROUP BY ip_address
              HAVING COUNT(DISTINCT {tool_col}) >= 30
                 AND COUNT(*) < COUNT(DISTINCT {tool_col}) * 2
            ) s""")
        tot_n = _one(cur, f"""
            SELECT COUNT(DISTINCT ip_address) FROM mcp_tool_calls
             WHERE created_at >= NOW() - INTERVAL '30 days'
               AND COALESCE(platform,'') NOT IN ('{synth}')""")
        if enum_n is None or tot_n is None:
            out.append(_check("enumeration_split",
                              "enumeration is split from real demand", None,
                              "enumeration query failed — UNMEASURED"))
        else:
            e, t = int(enum_n), int(tot_n)
            share = (e / t) if t else 0.0
            out.append(_check(
                "enumeration_split", "enumeration is split from real demand",
                share <= 0.25,
                f"{e}/{t} distinct caller(s) in 30d match an enumeration "
                f"signature ({share*100:.0f}%): >=30 distinct tools at under "
                f"2 calls each — everything once, nothing twice. Consistent "
                f"with the ~75-of-99 finding in loop-control lane 6. These sit "
                f"in the headline agent count as demand. Until identity is a "
                f"node with a resolved TYPE, the north-star number cannot "
                f"separate a platform integration from a crawler.",
                critical=True))
    return out


# ── lane 5 · orchestrator — a chain pretending to be a graph ──────────
def _lane_orchestrator() -> list:
    src = _src("routes", "brain_master_orchestrator.py")
    if src is None:
        return [_check("orchestrator_declared", "the tick declares its nodes",
                       None, "brain_master_orchestrator.py unreadable — "
                       "UNMEASURED")]
    inline = len(re.findall(r"_call\(\"(?:GET|POST)\"", src))
    declared = len(re.findall(r'"depends_on":', src))
    steps = inline + declared
    worst = steps * _ORCH_STEP_TIMEOUT_S
    resumable = bool(re.search(r"resume_from|_retry_step|skip_completed", src))
    # ★The runner EXISTING is not the same as the fan-out RUNNING. Reading the
    # flag is the difference between "we built it" and "it is saving time",
    # and only the second one is worth a green.
    has_runner = "_run_nodes" in src
    fanned_out = has_runner and (
        (os.environ.get("BRAIN_MASTER_PARALLEL") or "").strip() == "1")
    return [
        _check(
            "orchestrator_declared",
            "the tick declares its nodes and their dependencies",
            declared > 0 and inline == 0,
            f"{declared} declared node(s) with depends_on, {inline} still "
            f"inline as hardcoded _call() lines. "
            + ("every step is declared" if inline == 0 else
               f"The remaining {inline} keep their prerequisites encoded as "
               f"the order the lines happen to be written in. Actuator: lift "
               f"them into the node table too, keeping the tier gates as node "
               f"attributes."),
            critical=True),
        _check(
            "orchestrator_parallel",
            "independent nodes ACTUALLY run concurrently", fanned_out,
            (f"BRAIN_MASTER_PARALLEL=1, width "
             f"{os.environ.get('BRAIN_MASTER_PARALLEL_MAX') or '3'} — declared "
             f"nodes at the same dependency level share wall-clock." if fanned_out
             else
             (f"the runner exists but the fan-out is DORMANT "
              f"(BRAIN_MASTER_PARALLEL unset). ★Deliberate: the 2026-07-03 "
              f"outage was web thread-pool starvation from this tick, every "
              f"step is a self-HTTP call served by that same pool, and the "
              f"dyno's thread count is not knowable from in here. Per-node "
              f"timings ship either way, so the flip can be judged on data."
              if has_runner else
              f"no runner: every step is awaited in source order. Worst case "
              f"{steps} x {_ORCH_STEP_TIMEOUT_S}s = {worst}s of serial "
              f"wall-clock (ceiling {_ORCH_WALLCLOCK_CEILING_S}s before the "
              f"tick risks overlapping its own next fire)."))
            + ("" if worst <= _ORCH_WALLCLOCK_CEILING_S else
               " ★SERIAL WORST CASE IS OVER THE CEILING.")),
        _check(
            "orchestrator_resumable", "a failed node re-runs alone", resumable,
            "the tick has per-node resume" if resumable else
            f"a failure at step {steps} re-runs steps 1-{max(steps - 1, 1)}. "
            f"Every retry pays the full serial cost, which is why the practical "
            f"response to a flaky step is to wait for the next cron rather than "
            f"re-fire. NOT ADDRESSED by the node table: declaring dependencies "
            f"lets the runner SKIP a node whose prerequisite failed, which is "
            f"not the same as re-running one in isolation."),
    ]


def _run_tick() -> dict:
    out = {"shell": "graph", "n": 49, "lanes": [], "note": (
        "Read-only. Every other shell asks whether a NODE is healthy; this one "
        "asks whether the EDGES exist. In each lane the pieces are "
        "individually fine and only the join is missing. Red lanes name undone "
        "work — several are red by design.")}
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            lanes = [
                ("loop_edges", "1 · loop edges — who feeds whom",
                 _lane_loop_edges(cur)),
                ("causal_edges", "2 · causal edges — computed, then discarded",
                 _lane_causal_edges(cur)),
                ("typed_nodes", "3 · typed finding nodes — the #48 class, structurally",
                 _lane_typed_nodes(cur)),
                ("identity", "4 · identity graph — one agent, one node",
                 _lane_identity(cur)),
                ("orchestrator", "5 · orchestrator — a chain pretending to be a graph",
                 _lane_orchestrator()),
            ]
            for lid, label, checks in lanes:
                out["lanes"].append({"id": lid, "lane": label, "checks": checks,
                                     "pass": _verdict(checks)})
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass
    decided = [ln["pass"] for ln in out["lanes"] if ln["pass"] is not None]
    out["lanes_pass"] = sum(1 for p in decided if p)
    out["lanes_total"] = len(out["lanes"])
    out["edges_declared"] = len(LOOP_EDGES)
    out["ok"] = True
    return out


@graph_master_shell_bp.route("/api/v1/admin/graph/master-tick",
                             methods=["GET", "POST"])
def graph_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_run_tick())


@graph_master_shell_bp.route("/api/v1/admin/graph", methods=["GET"])
def graph_tick_alias():
    """CF zone-worker bypass alias — /admin/* is edge-cached in places and an
    admin board served 30 minutes stale reads as a failed deploy (loop-flywheel
    lane 2 exists because that already happened)."""
    return graph_tick()


@graph_master_shell_bp.route("/admin/graph", methods=["GET"])
def graph_dashboard():
    if _disabled():
        return Response("graph shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _run_tick()

    def chip(v):
        if v is True:
            return '<span style="color:#22c55e">PASS</span>'
        if v is False:
            return '<span style="color:#ef4444">FAIL</span>'
        return '<span style="color:#eab308">?</span>'

    rows = []
    for ln in p.get("lanes", []):
        rows.append(f'<h3>{_esc(ln["lane"])} — {chip(ln["pass"])}</h3><ul>')
        for ch in ln["checks"]:
            star = " ★" if ch.get("critical") else ""
            rows.append(f'<li>{chip(ch["pass"])}{star} <b>{_esc(ch["name"])}</b>'
                        f'<br><small>{_esc(ch["detail"])}</small></li>')
        rows.append("</ul>")
    edges = "".join(
        f'<li><code>{_esc(e["producer"])} → {_esc(e["consumer"])}</code> '
        f'[{_esc(e["kind"])}/{_esc(e["evidence"])}] '
        f'<small>{_esc(e["basis"])}</small></li>' for e in LOOP_EDGES)
    err = p.get("error")
    return Response(
        "<html><head><meta http-equiv='refresh' content='60'>"
        "<title>Graph — Shell #49</title></head>"
        "<body style='font-family:system-ui;background:#0b0b12;color:#e6e6f0;"
        "padding:24px;max-width:920px'>"
        "<h1>Graph — Shell #49</h1>"
        f"<p><small>{_esc(p.get('note',''))}</small></p>"
        + (f"<p style='color:#ef4444'>error: {_esc(str(err))}</p>" if err else "")
        + f"<p>lanes passing {p.get('lanes_pass','?')}/"
          f"{p.get('lanes_total','?')} · {len(LOOP_EDGES)} loop edge(s) "
          f"declared</p>"
        + "".join(rows)
        + f"<h3>declared loop graph</h3><ul>{edges}</ul>"
        + "</body></html>", mimetype="text/html")
