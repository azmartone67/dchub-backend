"""Single canonical source for the honest, floored public number PHRASES
(2026-07-20).

Everything agent-facing — the /for/*.html pages, llms.txt, the registry
manifests — should DERIVE its headline numbers from here instead of hardcoding,
so a new tool shipping (73->79 in a day) or a recount never leaves a surface
stale. Backed by ai_surface_canon.resolve_canon():
  - tools     : the LIVE tools/list count (moves every time a tool ships)
  - deals     : deals_phrase (DISTINCT deduped M&A, floored "1,400+" — NOT the
                raw ~11.5K deals COUNT(*), which is a ~2.5x dup-inflated over-claim)
  - markets/facilities/countries : conservative citation-safe floors

Public, cached 1h. Fail-soft: PINNED fallback so a consumer never gets nothing.
This is the endpoint the frontend agent-page heal + any registry heal fetch —
ONE source, so the numbers can never disagree across surfaces again.
"""
import logging
import json
import threading
import time

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
canon_phrases_bp = Blueprint("canon_phrases", __name__)

# ── ★2026-09-03: MEMOIZE. This endpoint advertised `max-age=3600` and cached
#    NOTHING server-side, so every edge miss recomputed the whole canon.
#
#    MEASURED at the origin, no cache-buster: 2.1s - 3.2s per request, every
#    request, all of it TTFB. resolve_canon() is an aggregate of ~6 synchronous
#    calls — an HTTP self-call to /api/v1/stats (~0.44s), four DB phrase queries
#    (deals / facilities / markets / countries), and an MCP initialize +
#    tools/list whose response is ~403 KB and has to be parsed to count names.
#    None of that can change between two requests a second apart, and the
#    response header already promises it will not change for an hour.
#
#    WHY IT MATTERS BEYOND LATENCY. This is the ONE source the nightly frontend
#    heal, llms.txt, the registry manifests and (since dchub-mcp-server#313's
#    sibling change) the CF zone worker all read. A 2.5s canon read is how a
#    heal quietly times out and leaves a surface stale — the exact failure this
#    endpoint exists to prevent. The zone worker's derivation used a 1.5s
#    ceiling and was therefore losing the race on EVERY cold isolate, silently
#    falling back to its literal.
#
#    Fail-soft in both directions: a refresh that raises leaves the previous
#    good body in place (stale beats nothing, and the value is an hour-stable
#    floor), and a cold cache computes inline exactly as before.
_CACHE_TTL_S = 300

# ── ★2026-09-09: A PROVISIONAL BODY MUST NOT BE MEMOIZED FOR THE FULL TTL.
#
#    resolve_public_floors_cached() answers immediately with PINNED floors on a
#    cold process and warms in the background — MEASURED at 7.6-15.5s. So the
#    cold window at its source is seconds. This memo then held that cold body
#    for the whole 300s, turning a ~10s condition into a 5-minute one.
#
#    That matters because of what sits downstream. Every edge MISS inside the
#    window re-caches a cold, under-stated body at Cloudflare — observed
#    2026-09-09: cf-cache-status HIT, age 1741, serving facilities "20,700+"
#    while this origin was already warm at "21,200+". A 5-minute origin window
#    is 30x more chances to be snapshotted for an hour than a 10s one.
#
#    ★ THIS IS THE LEVER THAT ACTUALLY MOVES, and the note is here so nobody
#    "fixes" it at the wrong layer. Shortening Cache-Control does NOT work: the
#    zone Cache Rule for /api/v1/ is override_origin, and the response reaching
#    a client carries `private, max-age=0, must-revalidate` rather than the
#    `public, max-age=3600` set below — CF rewrites it and caches anyway. Only
#    the zone rule (dashboard) or a purge can change EDGE behaviour. What the
#    origin controls is how long it keeps ANSWERING cold, which is this.
#
#    Deliberately not zero: a provisional body still costs a full rebuild
#    (resolve_canon's tools probe alone is seconds), so retrying every request
#    would hammer the origin exactly when it is already unhealthy.
_PROVISIONAL_TTL_S = 20

# ── ★2026-09-12: A PROVISIONAL BODY MUST NOT EVICT A GOOD ONE.
#
#    _PROVISIONAL_TTL_S above bounds how long a cold body is HELD. It does not
#    stop a cold body being stored in the first place, and storing it is the
#    part that reaches customers. The block below already refuses to lose a good
#    body to a blip — but only when the builder RAISES. When
#    resolve_public_floors_cached() is merely cold it RETURNS, successfully, a
#    body of pinned floors, and that return overwrote the good one. Same intent,
#    one branch short.
#
#    MEASURED 2026-09-12, from outside, minutes apart:
#        cold=False  facilities 21,600+  "resolve_public_floors (live)"
#        cold=True   facilities 21,500+  "(cold: PINNED floors)"
#    and downstream, in the same hour: /llms.txt, /AGENTS.md, /openapi.json and
#    the MCP server card all serving 21,500+ while this origin answered 21,600+
#    — the server card carrying BOTH numbers in one document. The zone stores
#    /api/v1/* with override_origin (measured: same URL MISS then HIT), so one
#    cold answer is snapshotted and served for the edge TTL.
#
#    ★ This is the lever THIS repo owns. Per the note above, shortening
#    Cache-Control cannot help — CF rewrites it and caches anyway; only the zone
#    rule or a purge changes edge behaviour. What the origin controls is what it
#    puts in the body, and a good body is never worth replacing with a floor.
#
#    BOUNDED ON PURPOSE. Covering forever would turn "the resolver is sick" into
#    "the numbers stopped moving", which is the harder failure to see. The cold
#    window at its source is 7.6-15.5s measured; this is two orders of magnitude
#    more than that, enough to ride out a restart or a brief resolver wobble, and
#    short enough that a real outage surfaces within the quarter-hour.
_GOOD_BODY_GRACE_S = 900

# ── ★2026-09-12 (second pass): THE LAST-GOOD BODY MUST BE SHARED, NOT PER-PROCESS.
#
#    The in-process cover below fixed the case it was written for and did NOT
#    fix the oscillation. MEASURED across the deploy that shipped it, 24 reads
#    over 7 minutes, one URL:
#
#        t=48   cache=miss  cold=false  21,600+
#        t=69   cache=miss  cold=true   21,500+
#        ...    8 cold bodies in all, and X-DC-Canon-Covering never once set
#
#    A single process cannot do that. Storing a warm body at t=48 arms the memo
#    for _CACHE_TTL_S, so t=69 would be a HIT. Two MISSES 21s apart, one warm and
#    one cold, means two PROCESSES with independent memos — Railway replicas —
#    and requests alternating between them. The cover never fired because the
#    cold replica had no good body of its OWN to cover with: it was the
#    "genuinely cold process" case, which the first pass documented as a
#    residual and which turns out to be the dominant one.
#
#    So the last-good body is kept where every replica can see it. A cold
#    replica now serves the newest known-good canon instead of the pinned floor,
#    under the same grace bound. Best-effort in both directions: if the store is
#    unreachable the endpoint behaves exactly as it did before this block.
_SHARED_TABLE = "canon_last_good"
_shared_ready = False
_shared_last_put = None          # avoid rewriting an unchanged body every refresh


def _ensure_shared_table():
    """Create the cross-process store, once per process.

    ★ db_utils.ddl_cursor() and not a pooled cursor. PGCursorWrapper.execute()
    returns early for CREATE TABLE whenever SKIP_DDL is set, and it defaults to
    '1' — no raise, no log, no table. ddl_cursor is the blessed direct
    connection. The read-back is not ceremony: a CREATE that silently did
    nothing is the exact failure being avoided, so the table is confirmed to
    exist before anything is allowed to depend on it.
    """
    global _shared_ready
    if _shared_ready:
        return True
    try:
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS " + _SHARED_TABLE + " ("
                "  id        SMALLINT PRIMARY KEY,"
                "  body      JSONB NOT NULL,"
                "  stored_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                ")")
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + _SHARED_TABLE,))
            row = cur.fetchone()
            _shared_ready = bool(row and row[0])
        if not _shared_ready:
            logger.warning("canon_phrases: %s did not exist after CREATE — "
                           "the cross-process cover is disabled", _SHARED_TABLE)
        return _shared_ready
    except Exception as e:
        logger.warning("canon_phrases: shared store unavailable (%s); "
                       "falling back to per-process cover only", str(e)[:140])
        return False


def _shared_put(body):
    """Publish a good body for the other replicas. Never raises."""
    global _shared_last_put
    if body is None or _is_provisional(body):
        return
    try:
        key = json.dumps(body, sort_keys=True, default=str)
    except Exception:
        return
    if key == _shared_last_put:          # unchanged — nothing to publish
        return
    if not _ensure_shared_table():
        return
    try:
        from db_utils import safe_db_cursor
        with safe_db_cursor() as cur:
            cur.execute(
                "INSERT INTO " + _SHARED_TABLE + " (id, body, stored_at) "
                "VALUES (1, %s::jsonb, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET body = EXCLUDED.body, "
                "stored_at = EXCLUDED.stored_at", (key,))
        _shared_last_put = key
    except Exception as e:
        logger.warning("canon_phrases: could not publish last-good: %s", str(e)[:140])


def _shared_get():
    """(body, age_seconds) of the newest published good body, or (None, None).

    The age is computed by the DATABASE, not this process: replicas do not share
    a clock, and a cover bounded by a grace window must not be bounded by a
    clock that might be minutes out.
    """
    if not _ensure_shared_table():
        return None, None
    try:
        from db_utils import safe_db_cursor
        with safe_db_cursor() as cur:
            cur.execute("SELECT body, EXTRACT(EPOCH FROM (NOW() - stored_at)) "
                        "FROM " + _SHARED_TABLE + " WHERE id = 1")
            row = cur.fetchone()
        if not row or row[0] is None:
            return None, None
        body = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        age = float(row[1] or 0)
        if _is_provisional(body):        # never cover with a floor
            return None, None
        return body, age
    except Exception as e:
        logger.warning("canon_phrases: could not read last-good: %s", str(e)[:140])
        return None, None


_cache_lock = threading.Lock()
# `good_at` is when a NON-provisional body was last stored; `covering` is true
# while a good body is being served in place of a provisional refresh. Both are
# read by the route for the X-DC-Canon-* headers — a number that silently
# differs from what the builder just returned is what costs hours to diagnose.
_cache = {"at": 0.0, "body": None, "ttl": _CACHE_TTL_S, "good_at": 0.0, "covering": False}


def _is_provisional(body):
    """True when `body` carries PINNED floors rather than measurements.

    Same discriminator, and for the same reason, as the frontend heal that
    consumes this endpoint (dchub-frontend#1424): `cold` alone is not enough,
    because the PINNED-fallback path sets cold=False while every floor key is
    pinned, and a DEGRADED body is live for its siblings and pinned for the one
    rejected key.

    Reads ONLY the floor keys. `substations`, `fiber_routes`,
    `transmission_lines` and `assets` are "pinned" on a perfectly warm bundle by
    design — they are not in _PUBLIC_FLOOR_KEYS, so the overlay can never mark
    them live, and treating them as evidence would make EVERY body look
    provisional and pin the TTL to 20s forever."""
    if not isinstance(body, dict):
        return True
    if body.get("cold") or body.get("degraded"):
        return True
    try:
        from ai_surface_canon import _PUBLIC_FLOOR_KEYS as floor_keys
    except Exception:
        floor_keys = ("facilities", "deals", "markets", "countries")
    src = body.get("value_source") or {}
    return not any(src.get(k) == "live" for k in floor_keys)


def _cached_body(builder):
    """Return the memoized canon body, refreshing at most every _CACHE_TTL_S.

    The lock is held across the rebuild deliberately: a stampede of concurrent
    misses recomputing the same ~2.5s aggregate is precisely what this is for,
    and every waiter wants the same answer. Callers arriving during a refresh
    block once, not N times.
    """
    now = time.time()
    with _cache_lock:
        if _cache["body"] is not None and (now - _cache["at"]) < _cache["ttl"]:
            return _cache["body"], True
        try:
            body = builder()
        except Exception as e:                      # never lose a good body to a blip
            logger.warning("canon_phrases: refresh failed: %s", str(e)[:160])
            if _cache["body"] is not None:
                return _cache["body"], True
            raise
        if body is not None:
            now2 = time.time()
            provisional = _is_provisional(body)
            prev = _cache["body"]
            # Cover a provisional refresh with the last good body, for a bounded
            # window. Retry on the provisional TTL, not the full one: the cold
            # window is seconds, so the good body should stop covering quickly.
            if provisional and not (prev is not None and not _is_provisional(prev)
                                    and (now2 - _cache["good_at"]) < _GOOD_BODY_GRACE_S):
                # No good body of our own — the cold-replica case. Adopt the
                # newest one any replica published, keeping its TRUE age so the
                # grace bound still means what it says.
                shared_body, shared_age = _shared_get()
                if shared_body is not None and shared_age is not None and shared_age < _GOOD_BODY_GRACE_S:
                    _cache["at"] = now2
                    _cache["body"] = shared_body
                    _cache["good_at"] = now2 - shared_age
                    _cache["ttl"] = _PROVISIONAL_TTL_S
                    _cache["covering"] = True
                    logger.info("canon_phrases: covering a provisional refresh with the "
                                "SHARED last-good body (%.0fs old, grace %ss)",
                                shared_age, _GOOD_BODY_GRACE_S)
                    return shared_body, False
            if (provisional and prev is not None and not _is_provisional(prev)
                    and (now2 - _cache["good_at"]) < _GOOD_BODY_GRACE_S):
                _cache["at"] = now2
                _cache["ttl"] = _PROVISIONAL_TTL_S
                _cache["covering"] = True
                logger.info(
                    "canon_phrases: covering a provisional refresh with the good body "
                    "(good is %.0fs old, grace %ss)", now2 - _cache["good_at"], _GOOD_BODY_GRACE_S)
                return prev, True
            _cache["at"] = now2
            _cache["body"] = body
            _cache["covering"] = False
            # A provisional body is held only long enough to keep a stampede off
            # a sick origin; a good one keeps the full TTL.
            _cache["ttl"] = _PROVISIONAL_TTL_S if provisional else _CACHE_TTL_S
            if not provisional:
                _cache["good_at"] = now2
                _shared_put(body)          # let the other replicas cover with it
        return body, False


@canon_phrases_bp.route("/api/v1/canon/phrases", methods=["GET"])
def canon_phrases():
    body, cached = _cached_body(_build_canon_body)
    if body is None:
        return jsonify({"ok": False, "error": "canon unavailable"}), 503
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    # Observability the old shape could not give: whether THIS response was
    # computed or served from the process cache. A latency claim about this
    # endpoint is unreadable without it.
    resp.headers["X-DC-Canon-Cache"] = "hit" if cached else "miss"
    # Says so when the body is a good one standing in for a provisional refresh,
    # so "why is this number not moving" is answerable from a response header.
    if _cache.get("covering"):
        resp.headers["X-DC-Canon-Covering"] = str(int(time.time() - _cache.get("good_at", 0)))
    return resp


def _build_canon_body():
    """Build the canon payload, and SAY which origin supplied each number.

    ★2026-09-08 — this called resolve_canon() directly and hardcoded
    "source": "resolve_canon (live)". Both were wrong, and the second hid the
    first.

    resolve_public_floors()'s docstring says it outright: "every consumer must go
    through here rather than calling resolve_canon() directly", because
    resolve_canon DEGRADES rather than raising. Reproduced 2026-09-08 with no
    DATABASE_URL:

        key            PINNED     resolve_canon   resolve_public_floors
        facilities    20,700+          400+              20,700+
        deals          2,100+        1,400+               2,100+

    A 52x UNDER-claim, served with no error marker in the payload and labelled
    "live". This endpoint is the ONE source the nightly frontend heal, llms.txt,
    the registry manifests and the CF zone worker read, so a DB blip during a
    refresh does not just serve a bad number — it WRITES it onto surfaces.

    ★ canon_is_live() DOES NOT CATCH THIS, and I checked rather than assumed: it
    returned True for all five witnesses while facilities was "400+". It answers
    "was this measured", not "is this sane" — 400 is a positive measurement. Only
    the floor comparison catches it, which is exactly what
    resolve_public_floors() does and why the label has to come from ITS verdict
    rather than from a constant string.

    The floors path is also the cached one, so this stops paying resolve_canon's
    measured 7.6-15.5s on a cold memo for the public numbers.
    """
    body = None
    try:
        from ai_surface_canon import (PINNED, resolve_canon,
                                      resolve_public_floors_cached)

        # ★ COPY. resolve_public_floors_cached() returns the cache object itself;
        #   popping the private keys off it in place would corrupt the cache for
        #   every later caller in this process.
        floors = dict(resolve_public_floors_cached() or {})
        src_map = dict(floors.get("_source") or {})
        rejected = list(floors.get("_rejected") or [])
        cold = bool(floors.get("_cold"))
        pub = {k: v for k, v in floors.items() if not k.startswith("_")}

        # tools is not a public FLOOR key, so it still comes from resolve_canon.
        tools = None
        try:
            c = resolve_canon() or {}
            tools = c.get("tools_advertised") or c.get("tools_live")
        except Exception as e:
            logger.warning("canon_phrases: tools resolve failed: %s", str(e)[:120])
        if not tools:
            tools = PINNED.get("tools_advertised")

        if tools and pub.get("deals"):
            live_n = sum(1 for v in src_map.values() if v == "live")
            if cold:
                # A cold process serves PINNED floors for a few seconds by
                # design. Under-stated, never wrong-direction — but say so.
                source = "resolve_public_floors (cold: PINNED floors)"
            elif rejected:
                source = ("resolve_public_floors (DEGRADED: %s)"
                          % ", ".join(rejected))
            elif live_n:
                source = "resolve_public_floors (live)"
            else:
                source = "resolve_public_floors (PINNED floors)"
            body = {
                "ok": True,
                "source": source,
                "tools": tools,
                **pub,
                # ★ Publish the provenance, not just the numbers. A consumer that
                #   must not write a pinned value onto a surface can now tell,
                #   per key, instead of trusting one flat label — the same reason
                #   the MCP tools publish a constraint_coverage block naming what
                #   an answer does NOT cover.
                "value_source": src_map,
                "degraded": rejected,
                "cold": cold,
            }
    except Exception as e:
        logger.warning("canon_phrases: build failed: %s", str(e)[:160])

    if body is None:
        # PINNED fallback — never serve nothing (the frontend heal is fail-closed
        # and will simply not write if a field is missing, but give it the floors)
        try:
            from ai_surface_canon import PINNED
            p = (PINNED.get("public") or {})
            # Same spread as above on purpose: a fallback publishing FEWER keys
            # than the live path is a second shape for consumers to handle.
            body = {
                "ok": True,
                "source": "PINNED (fallback)",
                "tools": PINNED.get("tools_advertised"),
                **{k: v for k, v in p.items()},
                "value_source": {k: "pinned" for k in p},
                "degraded": [],
                "cold": False,
            }
        except Exception as e:
            logger.error("canon_phrases: PINNED fallback failed: %s", str(e)[:120])
            return None

    return body
