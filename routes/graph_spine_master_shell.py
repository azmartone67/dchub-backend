"""
routes/graph_spine_master_shell.py — Graph-Spine Master Shell (#36, 2026-07-26).

Born from a question: "can we replace our RAG with graph engineering?" The
claim going around is that Microsoft, Stanford and Anthropic all independently
dropped RAG for knowledge graphs, at +18% accuracy and -85% cost. Most of that
is wrong (Anthropic ships Contextual Retrieval, not a graph; the -85% is
measured against stuffing whole documents into context, not against RAG; DSPy
is a prompt optimiser and has no knowledge graph in it). But one honest thing
survives: a graph beats vector search on GLOBAL questions, the kind where no
single chunk contains the answer.

This shell exists because the interesting question is not "graph or RAG" — it
is "where is our retrieval actually losing?" and the answer turned out to be
neither the index nor the schema, but the WIRING between them. We already run a
graph: facilities, markets, ISOs, plants, fiber, carriers and deals are typed
nodes with typed edges in Postgres, and execute_plan already walks a
deterministic tool graph over them.

    2,811 of 4,348 embedded deal chunks (64.7%) pointed at rows /api/deals
    deliberately refuses to serve — and `deals` is in PUBLIC_CORPORA, so every
    one was retrievable, with a citation stamp, on the UNAUTHENTICATED
    /api/v1/rag/search: 2,766 known duplicates, 34 unit-garbage NER fragments
    ("gap After → Orbion and", "Musk quietly → mobile gas") and 11 example.com
    seed placeholders. The `deals` table was de-duped on 2026-07-17 by flagging
    rows with data_flag; the served queries learned, the RAG registry never did.

That is not an architecture problem. Rebuilding it as triples in Neo4j would
faithfully reproduce every one of those rows as a node.

★ THIS LANE SET SHIPPED WRONG ONCE, AND THE CORRECTION IS THE LESSON. The first
cut counted `deals` with a bare COUNT(*) and reported "2,078 of 4,477 rows
(46.4%) are duplicates" as a live defect. But dedup on this table is done by
data_flag, not by DELETE — 2,868 of those rows were already quarantined and
already unserved. The true live figure is 40 of 1,611 (2.5%). Any count over
`deals` that omits the data_flag filter measures history, not what we serve.
Lanes 3 and 4 now carry that filter, and lane 3c states the trap on the page.

Six lanes, each one a place where an ID has more than one meaning:

  1. CARRIER↔FACILITY — carrier_facility_presence.dchub_facility_id holds TWO
     id-spaces at once (int-as-text discovered_facilities.id + hex16
     facilities.id). The union join reaches 13,870 facilities; the naive
     f.id-only join reaches 109. This cliff has produced three wrong fixes,
     every one of which returned HTTP 200.
  2. PEERINGDB — a THIRD id-space. pdb_*.fac_id matches facilities.id in 0 of
     59,728 rows. The bridge is facilities.source_id WHERE source='PeeringDB'.
  3. DEALS — is the 07-17 de-dup holding, measured over SERVED rows only, plus
     whether any writer still mints the old ingest-dated AUTO-<yyyymmdd>- key
     that made ON CONFLICT (id) unable to fire.
  4. RAG WIRING — does the index hold chunks we refuse to serve, does the
     served corpus duplicate itself, and do distinct deals RENDER identically
     (the template emits buyer/seller/type/market/notes, so a buyer-only row
     collapses to "Google →  (, )" — 11 distinct deals, one vector).
  5. ENTITY SPINE — news_discovered_entities resolves 86 of 470 entities to a
     facility (18.3%). This is the PRECONDITION gate for any GraphRAG layer:
     community summaries built over unresolved entities summarise noise.
  6. VERDICT — one honest field: is the graph-replacement precondition met?

★ THE DECOY CONTROL (lane 1c) is the load-bearing idea here. Match COUNT alone
proves nothing — integer id-spaces collide trivially, and a deliberately-wrong
`df.id + 1` join still matches 13,815 of 13,870 rows (99.6%). It is only when
you compare SEMANTICS that the two separate: the real join's matched pairs sit
0.0000° apart, the decoy's sit 60.6° apart on average. Any future "simplify
this join" change that keeps the count but loses the meaning trips 1c and
nothing else. A count-based version of this lane would have passed happily
through all three historical breakages.

★ PURE-DB shell. No outbound requests, no writes to business tables. Every
lane names an actuator and fires NOTHING. The only write is one row into
graph_spine_snapshots for trending.

★ COST: a full tick runs ~10-12s of sequential Neon reads (several are
whole-table md5/regex scans that no index can serve). Admin-only, cached 180s.
Do NOT wire this into a health check or a page a user waits on.

Endpoints:
  GET/POST /api/v1/admin/graph-spine/master-tick   JSON scoreboard (6 lanes)
  GET      /admin/graph-spine                       HTML dashboard
  GET      /api/v1/admin/graph-spine                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as integrity_master_shell.
Kill: GRAPH_SPINE_SHELL_DISABLE=1
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

from util.deals import DEALS_OK, deals_ok, deals_quarantined

logger = logging.getLogger(__name__)

graph_spine_master_shell_bp = Blueprint("graph_spine_master_shell", __name__)


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("GRAPH_SPINE_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers ────────────────────────────────────────────────────────

def _connect(url: str | None):
    """Raw psycopg2 connection, deliberately OUTSIDE the app pool — this tick
    holds a connection for ~10s and must never occupy a shared pool slot."""
    if not url:
        return None
    try:
        import psycopg2 as _pg
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[graph-spine] db connect failed: %s", e)
        return None


def _conn():
    """READ connection. Prefers NEON_REPLICA_URL: every query in this module
    is read-only and several are whole-table md5/regex scans, so they belong
    on the replica rather than the primary the write path shares."""
    return _connect(os.environ.get("NEON_REPLICA_URL")
                    or os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _write_conn():
    """WRITE connection for the snapshot row ONLY, and never the replica.

    ★ The replica rejects writes ("cannot execute CREATE TABLE in a read-only
    transaction"). Reusing the read connection for the snapshot INSERT means
    the insert raises, gets swallowed by the fail-soft handler, and the
    snapshot table stays empty forever while every tick reports success — a
    write that silently does nothing is exactly the class of bug this shell
    audits, so it does not get to live inside it.
    """
    return _connect(os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _row(c, sql: str):
    """Fail-soft single row. None on error — NOT a zero-filled tuple. A probe
    must be able to tell "the query broke" from "the count is zero"
    (reference_dchub_zero_row_feed_semantics).

    Literal SQL only, NO params tuple: a literal % anywhere in the statement
    makes psycopg2 attempt %-substitution against an empty tuple and 500
    (reference_psycopg2_empty_tuple_percent_trap). Every LIKE in this module
    is written as a POSIX regex for exactly that reason.
    """
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[graph-spine] row failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _scalar(c, sql: str):
    r = _row(c, sql)
    return r[0] if r else None


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct(part, whole):
    p, w = _num(part), _num(whole)
    if p is None or not w:
        return None
    return round(p * 100.0 / w, 1)


# ── check / verdict primitives (mirrors integrity_master_shell) ───────

def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate or pure gauge, "?").

    critical=True means this check is the REASON the lane exists. If a
    critical check comes back undetermined the lane must NOT render green —
    see _lane_verdict. A lane that reads PASS while admitting it could not
    reach the thing it audits is the exact failure this house pattern exists
    to prevent (reference_dchub_integrity_master_shell).
    """
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:300], "critical": critical}


def _lane_verdict(checks: list[dict]):
    if any(ch["pass"] is None and ch.get("critical") for ch in checks):
        return None
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


# ── lane 1 · carrier ↔ facility, two id-spaces ────────────────────────

def _lane_carrier_idspaces(c, ctx) -> list[dict]:
    out = []

    # 1a — GAUGE. The shape of the column: how much of it is each id-space.
    # Reported, never asserted: the mix shifts every time the hex backfill
    # runs, and a shifting mix is not a defect.
    r = _row(c, "SELECT "
                " count(*) FILTER (WHERE dchub_facility_id ~ '^[0-9]+$'),"
                " count(DISTINCT dchub_facility_id) FILTER (WHERE dchub_facility_id ~ '^[0-9]+$'),"
                " count(*) FILTER (WHERE dchub_facility_id ~ '^[0-9a-f]{16}$'),"
                " count(DISTINCT dchub_facility_id) FILTER (WHERE dchub_facility_id ~ '^[0-9a-f]{16}$'),"
                " count(*) FILTER (WHERE dchub_facility_id !~ '^[0-9]+$'"
                "                    AND dchub_facility_id !~ '^[0-9a-f]{16}$')"
                " FROM carrier_facility_presence")
    if not r:
        out.append(_check("cf_split", "id-space split of dchub_facility_id",
                          None, "query failed"))
    else:
        int_rows, int_ids, hex_rows, hex_ids, other = (int(x or 0) for x in r)
        ctx["cf_int_ids"], ctx["cf_hex_ids"] = int_ids, hex_ids
        out.append(_check(
            "cf_split", "id-space split of dchub_facility_id", None,
            f"int-as-text {int_rows:,} rows / {int_ids:,} ids · "
            f"hex16 {hex_rows:,} rows / {hex_ids:,} ids · neither {other:,}"))

    # 1b — the union join must still reach the fleet. This is the number the
    # map and every connectivity surface depend on.
    union_reach = _scalar(
        c, "SELECT count(DISTINCT df.id) FROM discovered_facilities df"
           " WHERE EXISTS (SELECT 1 FROM carrier_facility_presence cfp"
           "   WHERE cfp.dchub_facility_id IN (df.id::text, df.merged_facility_id))")
    naive_reach = _scalar(
        c, "SELECT count(DISTINCT df.id) FROM discovered_facilities df"
           " WHERE EXISTS (SELECT 1 FROM carrier_facility_presence cfp"
           "   WHERE cfp.dchub_facility_id = df.merged_facility_id)")
    ctx["cf_union_reach"] = union_reach
    if union_reach is None:
        out.append(_check("cf_union_reach", "two-space union join reaches the fleet",
                          None, "query failed"))
    else:
        lost = _pct((union_reach or 0) - (naive_reach or 0), union_reach)
        out.append(_check(
            "cf_union_reach", "two-space union join reaches the fleet (>=13,000)",
            int(union_reach) >= 13000,
            f"union join {int(union_reach):,} facilities · naive "
            f"merged_facility_id-only join {int(naive_reach or 0):,} "
            f"({lost}% would be silently lost)"))

    # 1c — ★THE DECOY CONTROL. The whole lane rests on this.
    #
    # A count-based check cannot tell a right join from a wrong one here: the
    # deliberately-broken `df.id + 1` join still matches 99.6% as many rows,
    # because consecutive integer ids both exist in the same dense space. Only
    # SEMANTICS separate them — matched pairs must describe the same building.
    # Real join: 0.0000° apart. Decoy: ~60° apart (a different continent).
    #
    # LIMIT 20000 inside each subquery bounds the scan; both samples are drawn
    # the same way so the comparison stays fair.
    real = _row(c, "SELECT round(max(d)::numeric,4), count(*) FROM ("
                   " SELECT abs(df.latitude - cfp.facility_lat)"
                   "      + abs(df.longitude - cfp.facility_lng) AS d"
                   " FROM discovered_facilities df"
                   " JOIN carrier_facility_presence cfp"
                   "   ON cfp.dchub_facility_id = df.id::text"
                   " WHERE df.latitude IS NOT NULL AND cfp.facility_lat IS NOT NULL"
                   " LIMIT 20000) x")
    decoy = _row(c, "SELECT round(avg(d)::numeric,4), count(*) FROM ("
                    " SELECT abs(df.latitude - cfp.facility_lat)"
                    "      + abs(df.longitude - cfp.facility_lng) AS d"
                    " FROM discovered_facilities df"
                    " JOIN carrier_facility_presence cfp"
                    "   ON cfp.dchub_facility_id = (df.id + 1)::text"
                    " WHERE df.latitude IS NOT NULL AND cfp.facility_lat IS NOT NULL"
                    " LIMIT 20000) x")
    real_max = _num(real[0]) if real else None
    real_n = int(real[1]) if real and real[1] is not None else 0
    decoy_avg = _num(decoy[0]) if decoy else None
    decoy_n = int(decoy[1]) if decoy and decoy[1] is not None else 0

    if real_max is None or decoy_avg is None or real_n < 1000 or decoy_n < 1000:
        # Not a failure — we simply learned nothing. Critical, so the lane
        # renders "?" rather than green.
        out.append(_check(
            "cf_decoy", "decoy control: real join is semantically right", None,
            f"insufficient coordinate sample (real n={real_n}, decoy n={decoy_n}) "
            "— cannot distinguish a correct join from a colliding one",
            critical=True))
    else:
        out.append(_check(
            "cf_decoy", "decoy control: real join is semantically right",
            real_max <= 0.01 and decoy_avg >= 5.0,
            f"real join max deviation {real_max}° over {real_n:,} pairs · "
            f"decoy (df.id+1) avg {decoy_avg}° over {decoy_n:,} pairs — "
            "counts alone cannot tell these apart (decoy matches 99.6% as many)",
            critical=True))
    return out


# ── lane 2 · PeeringDB, the third id-space ────────────────────────────

def _lane_pdb_bridge(c, ctx) -> list[dict]:
    out = []

    # 2a — the disjointness fact the resolver exists for. If this ever stops
    # being 0, someone has started writing dchub ids into a pdb column (or
    # vice versa) and _resolve_facility_ids is now lying in both directions.
    # lint: legacy-facilities-ok — this lane MEASURES the legacy `facilities`
    # id-space on purpose: it proves pdb fac_id never collides with
    # facilities.id, which is the whole reason _resolve_facility_ids exists.
    # Switching to discovered_facilities would test a different id-space and
    # silently stop guarding the one that actually drifts.
    r = _row(c, "SELECT (SELECT count(*) FROM pdb_network_facilities),"
                " (SELECT count(*) FROM pdb_network_facilities p"
                "   JOIN facilities f ON f.id = p.fac_id::text)")
    if not r:
        out.append(_check("pdb_disjoint", "pdb fac_id and facilities.id stay disjoint",
                          None, "query failed", critical=True))
    else:
        total, collide = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "pdb_disjoint", "pdb fac_id and facilities.id stay disjoint",
            collide == 0,
            f"{collide} direct matches out of {total:,} pdb_network_facilities "
            "rows — must be 0; the bridge is facilities.source_id, never a "
            "direct id comparison", critical=True))

    # 2b — bridge coverage. A gauge with a floor: below it, connectivity
    # answers stop being available for most of the fleet.
    r = _row(c, "SELECT (SELECT count(*) FROM facilities),"
                " (SELECT count(*) FROM facilities"
                "   WHERE source = 'PeeringDB' AND source_id IS NOT NULL)")
    if not r:
        out.append(_check("pdb_bridge", "PeeringDB bridge coverage", None, "query failed"))
    else:
        total, bridged = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "pdb_bridge", "PeeringDB bridge coverage (>=5,000 facilities)",
            bridged >= 5000,
            f"{bridged:,} of {total:,} facilities carry a PeeringDB source_id "
            f"({_pct(bridged, total)}%) — these are the only ones "
            "/api/v1/connectivity can answer for"))

    # 2c — GAUGE, and a standing warning. carrier_facility_presence.
    # facility_pdb_id is NOT a PeeringDB id despite the name (values look like
    # 'nearby-6-11530'). Reported as a gauge rather than asserted: a bare
    # integer here is a naming hazard, not proof of a break, and we cannot
    # cheaply prove those integers are or are not real pdb ids.
    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE facility_pdb_id ~ '^[0-9]+$')"
                " FROM carrier_facility_presence WHERE facility_pdb_id IS NOT NULL")
    if r:
        tot, digits = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "pdb_redherring", "facility_pdb_id is NOT a PeeringDB id", None,
            f"{digits:,} of {tot:,} values ({_pct(digits, tot)}%) are bare "
            "integers and will LOOK like pdb ids to the next reader — never "
            "use this column as a pdb bridge"))
    return out


# ── lane 3 · deals identity ───────────────────────────────────────────

_DEAL_BUSINESS_COLS = (
    "date, year, buyer, seller, value, mw, type, region, market, source_url,"
    " verified, status, notes, assets, deal_date, deal_category, data_flag,"
    " extraction_confidence, extracted_via")

# ★ THE QUARANTINE PREDICATE. `deals` is not deduped by DELETE — the 07-17
# integrity wave flagged 2,868 redundant/garbage rows with data_flag and taught
# the served queries to skip them. ANY count over this table that ignores
# data_flag is measuring history, not what we serve. The first cut of this lane
# did exactly that and reported 2,078 duplicate rows (46.4%) as a live defect;
# the true live figure is 40 of 1,611 (2.5%).
# Was a module-local copy of the predicate; now imported as DEALS_OK. The
# alias is gone on purpose: util/deals.py owns the LEFT(...,11)-not-LIKE
# reasoning (a literal % alongside a params tuple 500s the route) and the
# `cumulative_capex` carve-out, and an alias hides the guard from the census
# in tests/test_deals_guard.py.


def _lane_deals_identity(c, ctx) -> list[dict]:
    out = []

    # 3a — EXACT duplicates among the rows we actually SERVE: identical on
    # every one of the 19 business columns, differing only by id and the
    # ingest timestamps. Deliberately not a fuzzy match — a fuzzy key
    # collapses blank columns together and inflates the number (an early cut
    # read 3,276 because `assets` is blank in 100% of rows).
    r = _row(c, "WITH b AS (SELECT " + _DEAL_BUSINESS_COLS + " FROM deals"
                " WHERE " + DEALS_OK + "),"
                " g AS (SELECT count(*) AS n FROM b GROUP BY " + _DEAL_BUSINESS_COLS + ")"
                " SELECT (SELECT count(*) FROM deals WHERE " + DEALS_OK + "),"
                "        (SELECT count(*) FROM g),"
                "        (SELECT coalesce(sum(n) - count(*), 0) FROM g WHERE n > 1),"
                "        (SELECT count(*) FROM deals)")
    if not r:
        out.append(_check("deal_dupes", "served deals hold no exact duplicates",
                          None, "query failed", critical=True))
    else:
        live, distinct, excess, raw = (int(x or 0) for x in r)
        share = _pct(excess, live)
        ctx["deal_live"], ctx["deal_distinct"] = live, distinct
        ctx["deal_raw"] = raw
        out.append(_check(
            "deal_dupes", "served deals hold no exact duplicates (<5%)",
            (share is not None and share < 5.0),
            f"{excess:,} of {live:,} SERVED rows ({share}%) are byte-identical "
            f"to another on all 19 business columns · {distinct:,} distinct · "
            f"{raw - live:,} further rows are quarantined and never served",
            critical=True))

    # 3b — did the 07-17 writer fix HOLD? The old key embedded the ingest date
    # (AUTO-<yyyymmdd>-…) so one announcement re-ingested daily; deal_scraper
    # now mints AUTO-<hash6> so ON CONFLICT (id) collapses a re-scrape.
    #
    # ★ Keyed on RECENCY, not on presence. 389 served rows still carry a dated
    # id, but every one predates the fix (newest embedded date: 20260717, the
    # day of it). Flagging those would be flagging history forever — a
    # permanent red that says nothing. What matters is whether a dated id has
    # been minted SINCE, which means a writer regressed or an unknown fourth
    # writer exists (the 07-17 wave already found a hidden third one).
    r = _row(c, "SELECT count(*) FILTER (WHERE substring(id from 6 for 8) > '20260717'),"
                " count(*), coalesce(max(substring(id from 6 for 8)),'none')"
                " FROM deals WHERE id ~ '^AUTO-[0-9]{8}-'")
    if not r:
        out.append(_check("deal_dedup_key", "no writer mints ingest-dated ids any more",
                          None, "query failed"))
    else:
        after, dated_total, newest = int(r[0] or 0), int(r[1] or 0), str(r[2])
        out.append(_check(
            "deal_dedup_key", "no writer mints ingest-dated ids any more",
            after == 0,
            f"{after:,} dated AUTO- ids minted since the 2026-07-17 writer fix "
            f"(newest embedded date {newest}) · {dated_total:,} legacy dated "
            "ids remain, which is history, not a regression"))

    # 3c — gauge: how much of the table the quarantine is holding back. Makes
    # the raw-vs-served gap explicit so nobody re-derives 46% from COUNT(*).
    r = _row(c, "SELECT coalesce(data_flag,'(served)'), count(*) FROM deals"
                " GROUP BY 1 ORDER BY 2 DESC LIMIT 4")
    if r:
        out.append(_check(
            "deal_quarantine", "quarantine is doing the de-dup work", None,
            f"largest class: {r[0]} = {int(r[1] or 0):,} rows · dedup here is "
            "by data_flag, never by DELETE — count deals WITHOUT that filter "
            "and you measure history"))
    return out


# ── lane 4 · what the duplicates do to the vector index ───────────────
#
# The lane that answers "should we replace RAG with a graph". If retrieval is
# losing to duplicate text, a knowledge graph would inherit the duplicates as
# nodes and lose in exactly the same way.

def _lane_rag_redundancy(c, ctx) -> list[dict]:
    out = []

    # 4a — ★THE REAL DEFECT, and not the one this lane first reported.
    #
    # The first cut measured raw duplicate TEXT and blamed a broken dedup in
    # `deals`. Wrong diagnosis: dedup there works, by data_flag. What does not
    # work is that this registry never learned about it — brain_rag.CORPORA's
    # deals `where` had no quarantine filter, so 2,811 of 4,348 embedded deal
    # chunks (64.7%) pointed at rows /api/deals deliberately refuses to serve:
    # 2,766 duplicates, 34 unit-garbage NER fragments ("gap After → Orbion
    # and") and 11 example.com placeholders. `deals` is in PUBLIC_CORPORA, so
    # every one was retrievable with a citation stamp on the UNAUTHENTICATED
    # /api/v1/rag/search — the press_releases publish-gate class, one corpus
    # over. Fixed 2026-07-27 (r-rag-deals-quarantine); _sweep_orphans drains
    # the strays at 1,000/corpus/run, so this goes green over ~3 reindex ticks.
    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE d.id IS NULL"
                "   OR " + deals_quarantined("d") + ")"
                " FROM brain_corpus_embeddings e"
                " LEFT JOIN deals d ON d.id = e.source_id"
                " WHERE e.source_table = 'deals'")
    if not r:
        out.append(_check("rag_serves_excluded", "index holds no chunk we refuse to serve",
                          None, "query failed", critical=True))
    else:
        chunks, excluded = int(r[0] or 0), int(r[1] or 0)
        ctx["rag_excluded"] = excluded
        out.append(_check(
            "rag_serves_excluded", "index holds no chunk we refuse to serve",
            excluded == 0,
            f"{excluded:,} of {chunks:,} embedded deal chunks "
            f"({_pct(excluded, chunks)}%) point at a quarantined or deleted "
            "row — retrievable on the public /api/v1/rag/search while "
            "/api/deals refuses them", critical=True))

    # 4b — duplicate text among the chunks we DO intend to serve. Scoped to
    # live rows so the quarantine backlog above cannot flatter or inflate it.
    r = _row(c, "SELECT count(*), count(DISTINCT md5(e.text))"
                " FROM brain_corpus_embeddings e JOIN deals d ON d.id = e.source_id"
                " WHERE e.source_table = 'deals'"
                "   AND " + deals_ok("d"))
    if not r or not r[0]:
        out.append(_check("rag_live_dupes", "served corpus is not mostly duplicate text",
                          None, "query failed"))
    else:
        chunks, distinct = int(r[0]), int(r[1] or 0)
        share = _pct(chunks - distinct, chunks)
        ctx["rag_worst_share"] = share
        ctx["rag_worst_corpus"] = "deals"
        out.append(_check(
            "rag_live_dupes", "served corpus is not mostly duplicate text (<10%)",
            (share is not None and share < 10.0),
            f"{chunks - distinct:,} of {chunks:,} served deal chunks ({share}%) "
            "are byte-identical to another"))

    # 4c — distinct deals that RENDER identically. Not duplicate rows: the
    # text template is buyer/seller/type/market/notes, so a row carrying only
    # a buyer collapses to "Google →  (, )" — 11 different deals reduced to
    # one indistinguishable vector. This is a template defect, not a dedup one.
    r = _row(c, "SELECT max(n) FROM (SELECT count(*) AS n"
                " FROM brain_corpus_embeddings e JOIN deals d ON d.id = e.source_id"
                " WHERE e.source_table = 'deals'"
                "   AND " + deals_ok("d") +
                " GROUP BY md5(e.text)) x")
    if not r or r[0] is None:
        out.append(_check("rag_template_collapse", "no served chunk renders more than 5x",
                          None, "query failed"))
    else:
        mult = int(r[0])
        out.append(_check(
            "rag_template_collapse", "no served chunk renders more than 5x",
            mult <= 5,
            f"{mult:,} DISTINCT served deals render to byte-identical text — "
            "the corpus template emits buyer/seller/type/market/notes, so a "
            "buyer-only row becomes 'Google →  (, )'. At k=3 they are "
            "interchangeable; enrich the template, this is not a dedup bug"))

    # 4c — the 07-03 seq-scan regression guard. Without the HNSW index the
    # index is still CORRECT and merely 18x slower, which is why it went
    # unnoticed for a day.
    n = _scalar(c, "SELECT count(*) FROM pg_indexes"
                   " WHERE tablename = 'brain_corpus_embeddings'"
                   "   AND indexdef ~ 'hnsw'")
    out.append(_check(
        "rag_hnsw", "HNSW index present on the embedding column",
        (int(n) > 0) if n is not None else None,
        f"{int(n) if n is not None else '?'} hnsw index(es) — absence is a "
        "silent 18x slowdown, never an error"))

    # 4d — whole-index gauge, so the worst-corpus number has a denominator.
    r = _row(c, "SELECT count(*), count(DISTINCT md5(text)) FROM brain_corpus_embeddings")
    if r:
        tot, dis = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "rag_index_size", "index-wide redundancy", None,
            f"{tot:,} chunks · {dis:,} distinct texts · "
            f"{_pct(tot - dis, tot)}% redundant overall"))
    return out


# ── lane 5 · entity spine — the GraphRAG precondition ─────────────────

def _lane_entity_spine(c, ctx) -> list[dict]:
    """★ THIS LANE'S FIRST FLOOR WAS UNREACHABLE BY CONSTRUCTION.

    v1 asserted ">=60% of extracted entities resolve to a facility" and sat
    permanently RED at 18.3%. But `in_facilities` does not mean "resolves to
    the graph" — it means "is a data-centre operator we track", and the
    unresolved set is Nokia, Ericsson, Samsung, Broadcom, Qualcomm, Cisco,
    Intel, Chevron, DARPA. Those are real companies that legitimately do not
    operate a facility in our table. Normalising names gains exactly 0 exact
    matches. No amount of work moves that ratio to 60, so the check could only
    ever be a red light that says nothing — the same defect as lane 3b flagging
    history, one lane over.

    v2 asserts what is actually actionable and actually reachable: that the
    RESOLVER has no known blind spot, and that the entity-quality filter never
    rejects something that resolves. The raw ratio stays, as a gauge, labelled.
    """
    out = []

    # 5a — resolver blind spot. Entities that fail the resolver but WOULD be
    # found by a token-boundary prefix match against facility names. This is a
    # gap in our code, not in the world, so it is fixable and must be 0.
    # (Was 16 — Azure, Crown Castle, Alibaba, Facebook, Stargate … — because
    # facility names carry the site: "Azure Korea Central (Seoul)".)
    # ★ The lane MUST mirror the resolver's guards exactly, or it measures a
    # target the resolver is not allowed to hit. The first cut omitted
    # _GENERIC_PREFIX_STOP and counted 17 where the resolver can only ever
    # resolve 16 — "Power" prefix-matches "power generator for
    # government-linked data center …" and is stoplisted ON PURPOSE, so the
    # check would have sat permanently RED at 1. The stoplist is IMPORTED
    # rather than restated so the two can never drift apart.
    # ★ 2026-08-23: the query moved to news_entity_extraction, next to that
    # stoplist, for exactly the reason above — it was restated HERE and in
    # brain-autonomy, and both copies timed out for months. Importing the
    # measurement keeps the same no-drift property the stoplist import bought.
    try:
        from routes.news_entity_extraction import entity_blindspot_count
    except Exception as e:
        logger.debug("[graph-spine] blindspot import failed: %s", e)
        out.append(_check(
            "es_blindspot", "resolver has no known blind spot", None,
            "could not import the resolver's blind-spot measurement — "
            "refusing to measure against guards that may not match the code",
            critical=True))
        return out

    missed = entity_blindspot_count(c)
    if missed is None:
        out.append(_check("es_blindspot", "resolver has no known blind spot",
                          None, "query failed", critical=True))
    else:
        ctx["es_blindspot"] = missed
        out.append(_check(
            "es_blindspot", "resolver has no known blind spot",
            missed == 0,
            f"{missed:,} entities are unresolved but DO prefix-match a facility "
            "name at a token boundary — our matcher cannot see them, the graph "
            "already holds them", critical=True))

    # 5b — the landmine guard. /api/v1/admin/news-ner/purge-noise applies
    # _is_real_entity DESTRUCTIVELY (status='rejected') across every row. That
    # predicate rejected every single-token mixed-case name until 07-27, i.e.
    # Google, Apple, Amazon Web Services, Huawei, Vodafone, CoreWeave — 48 of
    # which resolve. Nothing may ever be both resolved and rejected.
    r = _row(c, "SELECT count(*) FROM news_discovered_entities"
                " WHERE in_facilities = TRUE AND coalesce(status,'') = 'rejected'")
    if r is None or r[0] is None:
        out.append(_check("es_no_reject_real", "no resolved entity is marked rejected",
                          None, "query failed", critical=True))
    else:
        bad = int(r[0])
        out.append(_check(
            "es_no_reject_real", "no resolved entity is marked rejected",
            bad == 0,
            f"{bad:,} entities resolve to a facility AND carry "
            "status='rejected' — purge-noise would be deleting real operators",
            critical=True))

    # 5c — the raw ratio, as a GAUGE with its ceiling stated. Never a target.
    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE in_facilities)"
                " FROM news_discovered_entities")
    if r:
        total, resolved = int(r[0] or 0), int(r[1] or 0)
        share = _pct(resolved, total)
        ctx["es_share"] = share
        ctx["es_total"], ctx["es_resolved"] = total, resolved
        out.append(_check(
            "es_resolution", "entity → facility resolution (gauge, NOT a target)",
            None,
            f"{resolved:,} of {total:,} ({share}%) resolve. The remainder is "
            "mostly Nokia/Ericsson/Qualcomm/Intel-class firms that legitimately "
            "operate no facility we track — normalising names gains 0 exact "
            "matches, so a 60% floor here would be unreachable by construction"))

    # 5d — is extraction keeping up with the corpus it reads from?
    r = _row(c, "SELECT (SELECT count(*) FROM news_articles),"
                " (SELECT count(*) FROM news_discovered_entities)")
    if r:
        arts, ents = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "es_yield", "entity yield per article", None,
            f"{ents:,} distinct entities extracted from {arts:,} news articles "
            f"({round(ents / arts, 3) if arts else '?'} per article)"))
    return out


# ── lane 6 · the verdict ──────────────────────────────────────────────

def _lane_graph_verdict(c, ctx) -> list[dict]:
    """Reads the metrics the earlier lanes stashed in ctx. Deliberately does
    NOT re-query: a verdict that disagrees with the lanes above it because it
    sampled the database a second time is worse than no verdict."""
    out = []

    share = ctx.get("es_share")
    worst = ctx.get("rag_worst_share")
    excluded = ctx.get("rag_excluded")
    blind = ctx.get("es_blindspot")

    if worst is None or excluded is None or blind is None:
        out.append(_check(
            "gv_precondition", "precondition for a graph retrieval layer", None,
            "lane 4 or lane 5 did not resolve — no verdict", critical=True))
        ctx["verdict"] = "UNKNOWN"
        return out

    # ★ The precondition is about the SPINE→INDEX WIRING being clean, not about
    # the entity ratio. v1 gated on "entity resolution >= 60%", which lane 5
    # now shows is unreachable by construction — a verdict that can never flip
    # is not a verdict. What IS in our control: the index must not hold rows we
    # refuse to serve, the served corpus must not be mostly duplicate text, and
    # the resolver must have no known blind spot.
    ready = excluded == 0 and worst < 10.0 and blind == 0
    ctx["verdict"] = "WIRING_CLEAN" if ready else "NOT_READY"
    out.append(_check(
        "gv_precondition", "spine → index wiring is clean",
        ready,
        f"chunks we refuse to serve {excluded} (need 0) · served-corpus "
        f"redundancy {worst}% (need <10) · resolver blind spot {blind} "
        "(need 0)", critical=True))

    # ★ WIRING_CLEAN IS NOT "BUILD THE GRAPH". It only means the measured
    # objections are gone. The remaining question is one this shell cannot
    # answer from the database: whether callers actually ask GLOBAL questions
    # ("what themes run across these 10,000 docs") that vector search cannot
    # serve. That is the ONLY thing GraphRAG genuinely beats RAG at, and it is
    # a demand question — measure it in mcp_call_log before building anything.
    out.append(_check(
        "gv_next", "what would actually justify a graph layer", None,
        "unanswered here: do callers ask GLOBAL/thematic questions? A graph "
        "wins on those and only those. We already run a typed graph in "
        "Postgres and a deterministic tool graph in execute_plan, so the "
        "build is a retrieval layer, not a migration"))

    # The honest read of the original question, stated in the surface itself
    # so nobody has to re-derive it from six lanes of numbers.
    out.append(_check(
        "gv_reading", "where the retrieval defect actually lives", None,
        f"in the WIRING between the spine and the index, not in the retrieval "
        f"architecture: {ctx.get('rag_excluded', '?')} embedded deal chunks "
        f"point at rows we refuse to serve, and entity resolution is {share}%. "
        f"Served deals are {ctx.get('deal_live','?')} of "
        f"{ctx.get('deal_raw','?')} raw rows — the rest are quarantined, and "
        "counting them is how this lane first over-reported the defect. A "
        "graph layer inherits every one of these; it does not fix them"))
    return out


# ── tick orchestration ────────────────────────────────────────────────
# (key, label, fn, actuator) — actuator is NAMED but never fired.

_LANES = [
    ("carrier_idspaces", "1 · Carrier↔facility (two id-spaces)",
     _lane_carrier_idspaces,
     "none needed while green — the union join in main.py/map + "
     "routes/connectivity_score.py is correct; this lane exists to catch the "
     "next 'simplification' of it"),
    ("pdb_bridge", "2 · PeeringDB (third id-space)", _lane_pdb_bridge,
     "raise bridge coverage by setting PEERINGDB_API_KEY on Railway (both "
     "services) so the weekly peeringdb_network_sync stops running anonymous "
     "at 20 req/min"),
    ("deals_identity", "3 · Deals identity (dedup)", _lane_deals_identity,
     "none needed while green — dedup is by data_flag (07-17 wave) and the "
     "AUTO-<hash6> writer fix holds; physical DELETE of the quarantined rows "
     "is an optional space reclaim, not a correctness fix"),
    ("rag_redundancy", "4 · RAG wiring (index vs what we serve)",
     _lane_rag_redundancy,
     "4a is FIXED at the registry (brain_rag.CORPORA deals `where` now carries "
     "the quarantine filter) and drains via _sweep_orphans at 1,000/run; 4c "
     "needs the corpus text template enriched so buyer-only deals stop "
     "collapsing to one vector"),
    ("entity_spine", "5 · Entity spine (GraphRAG precondition)",
     _lane_entity_spine,
     "extend news_entity_extraction.py resolution beyond exact name match "
     "(alias table + normalisation) — this is the 'deduplicate and normalise' "
     "step, the only part of the graph pipeline we genuinely lack"),
    ("graph_verdict", "6 · Verdict: replace RAG with a graph?",
     _lane_graph_verdict,
     "no actuator — this lane exists to keep the answer honest and dated"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 180.0  # a full tick is ~10-12s of Neon reads; do not tighten


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS graph_spine_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, verdict TEXT, payload JSONB)")
    except Exception as e:
        logger.debug("[graph-spine] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    ctx: dict = {}
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c, ctx)
        except Exception as e:  # a lane must never sink the tick
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        ms = int((time.time() - t0) * 1000)
        decided = [ch for ch in checks if ch["pass"] is not None]
        lanes.append({
            "lane": key, "label": label, "pass": _lane_verdict(checks),
            "actuator": actuator, "checks": checks, "ms": ms,
            "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(decided)}"
            if decided else "0/0"})

    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": ctx.get("verdict", "UNKNOWN"),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC; names an actuator per lane and fires "
                "nothing. Pure-DB, replica-preferred, no outbound requests. "
                "See routes/graph_spine_master_shell.py",
    }
    if c is not None:
        try:
            c.close()
        except Exception:
            pass

    # Snapshot goes to the PRIMARY on its own short-lived connection — the
    # read connection above is (correctly) the replica and cannot accept it.
    # persisted=False in the payload so a reader can tell "trend unavailable"
    # from "trend flat".
    payload["persisted"] = False
    w = _write_conn()
    if w is not None:
        try:
            _ensure_snapshots(w)
            with w.cursor() as cur:
                cur.execute(
                    "INSERT INTO graph_spine_snapshots"
                    " (lanes_pass, lanes_total, verdict, payload)"
                    " VALUES (%s, %s, %s, %s)",
                    (payload["lanes_pass"], payload["lanes_total"],
                     payload["verdict"], json.dumps(payload)))
            payload["persisted"] = True
        except Exception as e:
            logger.warning("[graph-spine] snapshot insert failed: %s", e)
        try:
            w.close()
        except Exception:
            pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

@graph_spine_master_shell_bp.route(
    "/api/v1/admin/graph-spine/master-tick", methods=["GET", "POST"])
def graph_spine_master_tick():
    if _disabled():
        # 404, never 5xx: the CF worker's proxyWithRetry reads ANY 5xx from
        # Railway as a dead origin and fails over to the stale Render backend.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@graph_spine_master_shell_bp.route("/admin/graph-spine", methods=["GET"])
@graph_spine_master_shell_bp.route("/api/v1/admin/graph-spine", methods=["GET"])
def graph_spine_dashboard():
    if _disabled():
        return Response("graph-spine shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;vertical-align:top'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px;vertical-align:top'>{_esc(ch['name'])}"
            f"{' <b style=color:#f59e0b>*</b>' if ch.get('critical') else ''}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else (
            "#ef4444" if lane["pass"] is False else "#334155")
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    verdict = p.get("verdict", "UNKNOWN")
    vcolor = {"READY": "#22c55e", "NOT_READY": "#f59e0b"}.get(verdict, "#eab308")
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='300'>"
        "<title>Graph-Spine Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:920px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Graph-Spine Master Shell "
        f"<span style='color:{'#22c55e' if p['lanes_pass'] == p['lanes_total'] else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>#36 · 07-26 · read-only "
        f"DIAGNOSTIC (names an actuator per lane, fires nothing) · pure-DB, "
        f"replica-preferred · {int(_TICK_TTL)}s tick cache · generated "
        f"{_esc(p['generated_at'])} · JSON: "
        f"/api/v1/admin/graph-spine/master-tick</div>"
        f"<div style='background:#0f172a;border:1px solid {vcolor};border-radius:12px;"
        f"padding:14px;margin:14px 0'><b style='color:{vcolor}'>"
        f"Replace RAG with a graph? {_esc(verdict)}</b>"
        f"<div style='color:#94a3b8;font-size:13px;margin-top:6px'>"
        "We already run a typed graph in Postgres and a deterministic tool "
        "graph in execute_plan. The measured retrieval defect is duplicate "
        "text from a missing identity spine — a defect a knowledge graph "
        "would inherit, not fix. <b style='color:#f59e0b'>*</b> marks the "
        "check each lane exists for; an undetermined one renders the lane "
        "'?', never green.</div></div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
