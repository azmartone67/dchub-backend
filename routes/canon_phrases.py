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
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "body": None}


def _cached_body(builder):
    """Return the memoized canon body, refreshing at most every _CACHE_TTL_S.

    The lock is held across the rebuild deliberately: a stampede of concurrent
    misses recomputing the same ~2.5s aggregate is precisely what this is for,
    and every waiter wants the same answer. Callers arriving during a refresh
    block once, not N times.
    """
    now = time.time()
    with _cache_lock:
        if _cache["body"] is not None and (now - _cache["at"]) < _CACHE_TTL_S:
            return _cache["body"], True
        try:
            body = builder()
        except Exception as e:                      # never lose a good body to a blip
            logger.warning("canon_phrases: refresh failed: %s", str(e)[:160])
            if _cache["body"] is not None:
                return _cache["body"], True
            raise
        if body is not None:
            _cache["at"] = time.time()
            _cache["body"] = body
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
