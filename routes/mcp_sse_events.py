"""
mcp_sse_events.py — SSE event stream for MCP agents to subscribe.

Phase ZZZZZ-round38 (2026-05-25). MCP spec advertises "resources" +
"prompts" + "subscribe" capabilities but DC Hub's MCP server only ships
"tools/listChanged". Agents that want to be notified of "new M&A deal",
"DCPI verdict change", or "ISO price spike" can't subscribe — they have
to poll, which is wasteful.

This is a lightweight precursor to full MCP resources/subscribe: a plain
SSE feed that any HTTP client (not just MCP) can connect to and receive
typed events. Once MCP resources are wired in dchub-mcp-server, this
becomes the underlying event source.

Endpoint:
  GET /api/v1/mcp/events.sse           — full event stream (heartbeat + events)
  GET /api/v1/mcp/events.sse?topic=X   — filter by topic
  GET /api/v1/mcp/events/recent        — JSON snapshot of last 50 events (no SSE)

Topics: see `_TOPICS_SUPPORTED` below, which is the ONLY list. Do not
re-type it here — a hand-copied topic list is how this endpoint came to
advertise four topics while emitting one.
"""
import os
import time
import json
import datetime
import threading
from collections import deque
from contextlib import contextmanager
from flask import Blueprint, Response, request, jsonify

# The published verdict rule, as SQL, generated from VERDICT_BANDS — the
# same import routes/agent_broadcast.py makes, for the same reason: this
# feed has to tell a market MOVING apart from a market being RELABELLED,
# and a fifth hand-typed band table is the one thing this codebase must
# not grow again (tests/test_dcpi_verdict_bands.py). Bound at module
# scope on purpose: if it ever fails, main.py's registration try/except
# reports "mcp_sse_bp register failed" loudly, where a lazy import would
# raise inside the fetch's `except` and be swallowed into an empty list —
# the exact silent-zero failure this module is being fixed for.
from util.dcpi_method import verdict_case_sql as _verdict_case_sql
from routes.url_registry import build_public_url

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

mcp_sse_bp = Blueprint("mcp_sse", __name__,
                       url_prefix="/api/v1/mcp")


# ─────────────────────────────────────────────────────────────────────────
# Topics — one list, derived, never hand-copied
# ─────────────────────────────────────────────────────────────────────────
# ★ `/events/recent` advertises `_TOPICS_SUPPORTED` verbatim rather than a
#   literal of its own. Before this change the advertisement was a separate
#   hand-typed list, and it drifted the only way such a list can: it named
#   `iso_price_spike` and `new_facility`, which NOTHING in this module has
#   ever pushed, alongside `dcpi_verdict_shift`, whose fetch read three
#   tables that do not exist (see `_fetch_dcpi_verdict_shifts`). An agent
#   that subscribed to any of the three got a well-formed 200 and silence
#   forever.
#
#   Both surfaces now read this tuple, and
#   tests/test_mcp_sse_dcpi_source.py EXECUTES `refresh()` and asserts the
#   set of topics it actually pushes equals `_PUSH_TOPICS` — so a topic
#   cannot be advertised again without a producer, and a producer cannot be
#   added without being advertised. A presence check on the string would
#   have passed on the broken version; only running it catches this.
_PUSH_TOPICS = ("dcpi_verdict_shift", "dcpi_restatement", "hyperscaler_deal")
_STREAM_TOPICS = ("heartbeat", "reconnect")
_TOPICS_SUPPORTED = _PUSH_TOPICS + _STREAM_TOPICS

#: Where an agent goes to read what a restatement means. Same URL
#: routes/agent_broadcast.py points at, for the same reason.
_METHODOLOGY_URL = "https://dchub.cloud/api/v1/dcpi/methodology"

#: Most shift events one refresh may append. The ring holds 200 and a
#: mass-relabel day can produce ~200 candidates; without a cap one bad day
#: would evict every other topic. Never silent — `refresh()` reports
#: `capped` so a truncation is visible rather than read as "that's all
#: there was".
_MAX_SHIFT_EVENTS = 20


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# In-memory event ring — last 200 events, FIFO
_EVENTS = deque(maxlen=200)
_LOCK = threading.Lock()


def _push_event(topic, data, key=None):
    """Append one event to the ring. Returns True if it was appended.

    ★ `key` de-duplicates. `/events/refresh` is dispatched from
    routes/cron_heartbeat.py on EVERY heartbeat invocation — two crons
    (every 5 min and every 15 min) drive that list, so this runs roughly
    every 3-4 minutes, ~300 times a day. Every source it polls is a
    *standing* query, not a queue: the same row keeps matching until it
    ages out of the window. Without a key each match is re-appended on
    every tick, and since the ring holds 200, a single matching row fills
    it with ~300 copies of itself per day and evicts every other topic.
    That is not hypothetical — measured on production 2026-08-08, one
    news row was being re-pushed as `hyperscaler_deal` on every tick.

    De-duplication is against the RING, not a process-wide "already sent"
    set, and that distinction is load-bearing twice over. The ring is
    per-gunicorn-worker: cron reaches one worker, an SSE client may be on
    another, so a global flag on the cron's worker would starve every
    other worker's ring permanently. And because the ring is bounded, an
    event that ages out becomes pushable again — which is what makes this
    a de-duplicator rather than a one-shot latch.
    """
    with _LOCK:
        if key is not None:
            for e in _EVENTS:
                if e["topic"] == topic and e.get("key") == key:
                    return False
        _EVENTS.append({
            "id": str(int(time.time() * 1000)),
            "topic": topic,
            "key": key,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "data": data,
        })
        return True


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _fetch_dcpi_verdict_shifts():
    """DCPI markets whose PUBLISHED verdict moved at the latest snapshot.

    Returns `(shifts, restated, meta)`.

    ★ WHAT WAS WRONG (measured read-only on live Neon, 2026-08-08).
    This function used to probe three tables in order — `dcpi_scores_history`,
    `dcpi_history`, `dcpi_v2_scores` — inside a per-table
    `except Exception: continue`, returning [] when none worked. `to_regclass`
    is NULL for all three. None has ever existed. So it always returned an
    empty list, `/events/refresh` has never once pushed a
    `dcpi_verdict_shift`, and `/events/recent` went on advertising the topic:
    a 200 with an empty ring is indistinguishable from a quiet market.

    Two independent defects, either of which alone was fatal:

      1. No such table. Not drift — no `dcpi_*_history` table by any of those
         names is in the schema.
      2. The fallback chain could not fall back. psycopg2 opens with
         `autocommit = False`, so the first `UndefinedTable` aborts the
         transaction and every later statement on that connection raises
         `InFailedSqlTransaction` — the loop never rolls back between probes.
         Verified against the live DB: probe 1 raises `UndefinedTable`, probe
         2 against a table that DOES exist raises `InFailedSqlTransaction`.
         Even had `dcpi_history` existed, candidates 2 and 3 were unreachable.
         This is the [[psycopg2 with-conn autocommit trap]] in its read shape:
         one failed query zeroes every later read on the connection, silently.

    ★ THE REAL SOURCE is `dcpi_daily_snapshots` (snapshot_date, market_slug,
    verdict, method_version, both scores) — the same table
    routes/agent_broadcast.py::_fetch_dcpi_verdict_shifts reads, and the only
    per-day DCPI series that exists. `market_power_scores` cannot serve as
    the prior side: it has carried UNIQUE(market_slug) since 2026-05-11, so
    every writer is UPDATE-in-place and no non-latest row survives.

    ★ WHY NOT KEY OFF `market_power_scores.computed_at`, the other candidate.
    It is the live recompute clock and it does tick often enough for a
    minutes-window (measured: one sweep rewrote all 323 rows between 17:49
    and 17:53 UTC). It is still the wrong side to read, because it is
    PRE-publication state. The snapshot writer applies the publish gate; the
    live row is whatever the last of six writers left behind, mid-sweep
    included. Measured at 18:51 UTC on 2026-08-08, 226 of 330 live rows
    carried a verdict the published bands do not produce from that row's own
    scores, against 115 of 315 in that morning's snapshot. Publishing live
    deltas as verdict-shift events would stream exactly the writer flap
    #2429/#2436 fixed, straight to subscribed agents. So the window is not
    minutes at all — the DCPI verdict series is daily, and this asks "what
    moved at the last snapshot", de-duplicated by `_push_event` so the
    ~300 cron ticks between snapshots push each shift once.

    `prior` is the newest snapshot strictly before `max(snapshot_date)` —
    the table's own maximum, NOT `CURRENT_DATE`. Coverage is gapless today
    (71 dates, 2026-05-30..2026-08-08), but keying on CURRENT_DATE would
    make one skipped 06:00 cron produce an empty result, which is the same
    silent zero being fixed. `p.prior_date < l.snapshot_date` drops the
    degenerate case where a market stopped being snapshotted and resolves to
    the same row on both sides.

    ★ RESTATEMENTS ARE EXCLUDED, by the same two markers and the same
    generated band SQL as agent_broadcast (#2442) — imported, not re-derived,
    because two surfaces disagreeing about what counts as a verdict shift is
    worse than either being wrong alone:

      VINTAGE   the pair's `method_version` differs — a weight / ceiling /
                input-source change moved the scores under a fixed rule.
      OFF-BAND  either row's stored verdict is not what VERDICT_BANDS
                produce from that row's OWN stored scores, so it was written
                by a rule no longer in force and the two sides are not
                comparable.

    Neither subsumes the other; #2442's docstring carries the full
    measurement. Both markers were live on the day this was written: today's
    snapshot stamped `2.0.1` while `market_power_scores` had moved to
    `2.2.1`, and 115 of 315 snapshot rows were off-band. All 104 day-over-day
    candidates classify as restatements and 0 as shifts — correctly. A market
    was not rebuilt overnight; the label was recomputed.

    The suppressed rows are not discarded silently: `refresh()` publishes one
    aggregate `dcpi_restatement` event so an agent holding yesterday's
    verdict still learns the label changed, without being told the market
    moved.

    The band expressions are spliced with `.replace()`, never an f-string and
    never `%` — same rule, and same reason, as agent_broadcast and
    util/dcpi_score_row.py.
    """
    shifts, restated = [], []
    meta = {"source": "dcpi_daily_snapshots", "snapshot_date": None,
            "candidates": 0, "reachable": False}
    if not (_pg and _dsn()):
        return shifts, restated, meta
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                WITH bounds AS (
                    SELECT max(snapshot_date) AS newest
                      FROM dcpi_daily_snapshots
                ),
                latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, verdict, method_version,
                           excess_power_score, constraint_score,
                           snapshot_date, captured_at
                      FROM dcpi_daily_snapshots
                     ORDER BY market_slug, snapshot_date DESC
                ),
                prior AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug,
                           verdict            AS prior_verdict,
                           method_version     AS prior_method,
                           excess_power_score AS prior_excess,
                           constraint_score   AS prior_constraint,
                           snapshot_date      AS prior_date
                      FROM dcpi_daily_snapshots, bounds
                     WHERE snapshot_date < bounds.newest
                     ORDER BY market_slug, snapshot_date DESC
                )
                SELECT l.market_slug, l.market_name, m.iso,
                       p.prior_verdict, l.verdict,
                       l.excess_power_score, l.captured_at,
                       l.snapshot_date, p.prior_date,
                       (l.method_version IS DISTINCT FROM p.prior_method)
                           AS vintage_differs,
                       (   {canon_latest} IS DISTINCT FROM l.verdict
                        OR {canon_prior}  IS DISTINCT FROM p.prior_verdict
                       ) AS off_band
                  FROM latest l
                  JOIN prior p USING (market_slug)
                  LEFT JOIN LATERAL (
                       SELECT iso
                         FROM market_power_scores s
                        WHERE s.market_slug = l.market_slug
                        ORDER BY s.computed_at DESC
                        LIMIT 1
                  ) m ON TRUE
                 WHERE p.prior_verdict IS DISTINCT FROM l.verdict
                   AND p.prior_date < l.snapshot_date
                 ORDER BY (l.verdict       IN ('BUILD', 'AVOID')
                        OR p.prior_verdict IN ('BUILD', 'AVOID')) DESC,
                          ABS(COALESCE(l.excess_power_score, 0)
                              - COALESCE(p.prior_excess, 0)) DESC,
                          l.market_slug
            """.replace(
                "{canon_latest}",
                _verdict_case_sql("l.excess_power_score", "l.constraint_score"),
            ).replace(
                "{canon_prior}",
                _verdict_case_sql("p.prior_excess", "p.prior_constraint"),
            ))
            meta["reachable"] = True
            for r in cur.fetchall() or []:
                (slug, name, iso, was, now_, ex, ts, snap_date, since,
                 vintage_differs, off_band) = r
                meta["candidates"] += 1
                meta["snapshot_date"] = _iso(snap_date)
                if vintage_differs or off_band:
                    restated.append({
                        "market": slug, "was": was, "now": now_,
                        "since": _iso(since),
                        "vintage_differs": bool(vintage_differs),
                        "off_band": bool(off_band),
                    })
                    continue
                shifts.append({
                    "market": slug,
                    "market_name": name,
                    "iso": iso,
                    "was": was,
                    "now": now_,
                    "since": _iso(since),
                    "snapshot_date": _iso(snap_date),
                    "excess_power_score": ex,
                    "at": _iso(ts),
                    "url": build_public_url("dcpi", slug),
                })
    except Exception:
        pass
    return shifts, restated, meta


def _restatement_payload(restated, snapshot_date):
    """One aggregate event for the shifts that were suppressed.

    Deliberately NOT one event per market — same call agent_broadcast makes.
    A restatement is a single event (the rule changed, or the inputs were
    re-sourced), and 200 of them would evict every genuine shift from a
    200-slot ring, which is the failure this exists to prevent.
    """
    return {
        "snapshot_date": snapshot_date,
        "restated": len(restated),
        "by_method_version": sum(1 for r in restated if r["vintage_differs"]),
        "by_off_band": sum(1 for r in restated if r["off_band"]),
        "since": min((r["since"] for r in restated if r["since"]), default=None),
        "examples": [f"{r['market']} {r['was']}→{r['now']}"
                     for r in sorted(restated, key=lambda r: r["market"])[:3]],
        "note": ("These markets changed DCPI verdict for a reason other than "
                 "the market moving — a different method_version, or a stored "
                 "verdict the published bands do not produce from that row's "
                 "own scores. They are EXCLUDED from dcpi_verdict_shift. A "
                 "restatement relabels a market; it does not report a change "
                 "in the world."),
        "methodology": _METHODOLOGY_URL,
    }


def _fetch_recent_hyperscaler_deals(since_minutes=60):
    out = []
    if not (_pg and _dsn()): return out
    try:
        with _conn() as c, c.cursor() as cur:
            for keyword_sql in [
                "LOWER(title) LIKE '%stargate%' OR LOWER(title) LIKE '%openai%' OR LOWER(title) LIKE '%coreweave%'",
                "LOWER(title) LIKE '%hyperscaler%' OR LOWER(title) LIKE '%gpu cluster%'",
            ]:
                try:
                    cur.execute(f"""
                        SELECT id, title, source, url, published_date
                        FROM news
                        WHERE ({keyword_sql})
                          AND published_date > CURRENT_DATE - INTERVAL '1 day'
                        ORDER BY published_date DESC LIMIT 10
                    """)
                    rows = cur.fetchall()
                    for r in rows:
                        out.append({"id": r[0], "title": r[1], "source": r[2],
                                    "url": r[3], "published": r[4].isoformat() if r[4] else None})
                    if rows: break
                except Exception: continue
    except Exception: pass
    return out


@mcp_sse_bp.route("/events.sse", methods=["GET"])
def sse_stream():
    """SSE stream — connect with `curl -N` or EventSource client."""
    topic_filter = (request.args.get("topic") or "").strip()

    def generate():
        # Replay recent events first
        with _LOCK:
            recent = list(_EVENTS)
        for evt in recent[-10:]:
            if not topic_filter or evt["topic"] == topic_filter:
                yield f"event: {evt['topic']}\ndata: {json.dumps(evt)}\n\n"

        last_id = recent[-1]["id"] if recent else "0"
        deadline = time.time() + 90  # 90s connection then ask client to reconnect

        while time.time() < deadline:
            # Heartbeat every 30s
            yield f"event: heartbeat\ndata: {json.dumps({'at': datetime.datetime.utcnow().isoformat() + 'Z'})}\n\n"

            # Look for new events
            with _LOCK:
                new_events = [e for e in _EVENTS if e["id"] > last_id]
            for evt in new_events:
                if not topic_filter or evt["topic"] == topic_filter:
                    yield f"id: {evt['id']}\nevent: {evt['topic']}\ndata: {json.dumps(evt)}\n\n"
                last_id = evt["id"]

            time.sleep(30)

        # Tell client to reconnect
        yield "event: reconnect\ndata: {\"reason\": \"90s_lifecycle\"}\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # nginx — don't buffer SSE
        "Access-Control-Allow-Origin": "*",
    })


@mcp_sse_bp.route("/events/recent", methods=["GET"])
def recent():
    with _LOCK:
        events = list(_EVENTS)
    topic = (request.args.get("topic") or "").strip()
    if topic:
        events = [e for e in events if e["topic"] == topic]
    return jsonify({
        "count": len(events),
        "events": events[-50:],
        # Derived from `_TOPICS_SUPPORTED`, never a literal — see the note
        # on that tuple for what a hand-copied list did here.
        "topics_supported": list(_TOPICS_SUPPORTED),
    }), 200


@mcp_sse_bp.route("/events/refresh", methods=["GET", "POST"])
def refresh():
    """Cron-callable. Polls DB for new events + pushes to ring.

    Reports `pushed` (newly appended) separately from `deduped` (already in
    the ring) and `capped` (dropped by `_MAX_SHIFT_EVENTS`), and reports the
    DCPI pass's own `candidates` / `restatements` / `reachable`. A blind
    source and a quiet market both used to render as `"pushed": 0`; they no
    longer do — `reachable: false` or `candidates: 0` says which.
    """
    started = datetime.datetime.utcnow()
    pushed = {t: 0 for t in _PUSH_TOPICS}
    deduped = {t: 0 for t in _PUSH_TOPICS}

    def _emit(topic, data, key):
        if _push_event(topic, data, key=key):
            pushed[topic] += 1
        else:
            deduped[topic] += 1

    shifts, restated, dcpi_meta = _fetch_dcpi_verdict_shifts()
    for shift in shifts[:_MAX_SHIFT_EVENTS]:
        _emit("dcpi_verdict_shift", shift,
              f"{shift['market']}:{shift['snapshot_date']}")
    if restated:
        snap = dcpi_meta.get("snapshot_date")
        _emit("dcpi_restatement",
              _restatement_payload(restated, snap), f"restatement:{snap}")

    for deal in _fetch_recent_hyperscaler_deals(since_minutes=60):
        _emit("hyperscaler_deal", deal, f"news:{deal.get('id')}")

    return jsonify({
        "at": started.isoformat() + "Z",
        "pushed": pushed,
        "deduped": deduped,
        "dcpi": dict(dcpi_meta,
                     shifts=len(shifts),
                     restatements=len(restated),
                     capped=max(0, len(shifts) - _MAX_SHIFT_EVENTS)),
        "ring_size": len(_EVENTS),
    }), 200


@mcp_sse_bp.route("/events/health", methods=["GET"])
def health():
    with _LOCK:
        events = list(_EVENTS)
    topics = {}
    for e in events:
        topics[e["topic"]] = topics.get(e["topic"], 0) + 1
    return jsonify({
        "ring_size": len(events),
        "topics": topics,
        # Same tuple `/events/recent` advertises, so the two can't disagree.
        "topics_supported": list(_TOPICS_SUPPORTED),
        "blueprint": "mcp_sse_bp",
    }), 200
