"""dcpi_methodology.py — GET /api/v1/dcpi/methodology (r-ws3-methodology, 2026-07-29).

The machine-readable DCPI methodology, EMITTED FROM the scorer's own constants
(util/dcpi_method.py) rather than retyped. That is the entire point: the
methodology DC Hub published until today was hand-written prose describing a
formula that does not exist in this codebase, and 67% of the live index carried
a verdict its published bands could not produce.

WHY /api/v1/dcpi/methodology AND NOT /dcpi/methodology
------------------------------------------------------
Cloudflare Pages intercepts /dcpi/* before the request ever reaches Flask — a
backend route at /dcpi/methodology is dead code, which is exactly how the
static page went four months without anything in this repo validating it.
/api/v1/dcpi/* is proven reachable (that is where the scores are served).

FAIL-SOFT
---------
util.dcpi_method is pure constants — no DB, no network. If even that import
fails, a PINNED minimal block is served with source="PINNED (fallback)" — same
shape as routes/canon_phrases.py. A consumer never gets nothing, and never gets
a silent zero.

r-repro (2026-08-08) adds ONE optional counting query, in _live_counts, wrapped
so that any failure degrades the DOCUMENT rather than the response. It exists
because three published figures had drifted while sitting in the source as
literals: the index size, the underlying row count, and the queue-wait ceiling
(which several markets exceeded). A number that describes the live index
cannot be a constant in a module that never reads the live index, so those
figures are measured here and injected into method_block(). When the query
fails, method_block() emits explicit "unmeasured" wording — never a stale
literal, and never a fabricated zero.

The rule this preserves: DB access is confined to _live_counts, which returns
a dict on every path and raises on none, and the request handler contains no
DB code at all. The document endpoint still cannot 500 on a DB outage.

Registered in main.py's SAFE ZONE (~line 1820) with the market_deep_dive
try/except recipe. Late-line registration silently 404s in prod.
"""

import logging
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
dcpi_methodology_bp = Blueprint("dcpi_methodology", __name__)

# Served when util.dcpi_method cannot be imported at all. Deliberately does NOT
# restate any weight: a fallback that retypes the formula would recreate the
# hand-copy bug this endpoint exists to kill. It says "unavailable", which is
# true, instead of a number that might be wrong.
_PINNED = {
    "ok": True,
    "source": "PINNED (fallback)",
    "method_version": None,
    "note": ("The DCPI method constants could not be loaded in this process. "
             "No weights are restated here on purpose — a hand-copied "
             "fallback is how the previous published methodology came to "
             "describe a formula that did not exist. Retry, or read the "
             "scorer directly."),
    "scores_endpoint": "/api/v1/dcpi/scores",
    "history_endpoint": "/api/v1/dcpi/history",
}


def _live_counts():
    """Figures that describe the LIVE index, measured rather than retyped.

    Returns a dict on success, or {"available": False, "reason": ...} on any
    failure. Never raises: the caller must be able to publish the document
    without it, and method_block() has honest wording for every value absent.

    One query, five aggregates, no row payload. QUEUE_WAIT_PROXY_PATH_CEILING
    is passed as a bound parameter rather than inlined so the SQL cannot drift
    from the constant it is counting breaches of.
    """
    try:
        import os
        import psycopg2
        from util.dcpi_method import QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS
        url = os.environ.get("DATABASE_URL")
        if not url:
            return {"available": False, "reason": "no DATABASE_URL"}
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = 5000")
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(DISTINCT market_slug)
                               FILTER (WHERE COALESCE(published, true)),
                           MAX(queue_wait_months)
                               FILTER (WHERE COALESCE(published, true)),
                           COUNT(*) FILTER (WHERE COALESCE(published, true)
                                              AND queue_wait_months > %s)
                      FROM market_power_scores
                """, (QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS,))
                r = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not r:
            return {"available": False, "reason": "empty result"}
        table_rows, index_size, qw_max, qw_over = r
        # A zero index would make every generated sentence read as a
        # measurement of nothing. Treat it as unmeasured, not as news.
        if not index_size:
            return {"available": False, "reason": "index_size came back 0"}
        return {
            "available": True,
            "index_size": int(index_size),
            "table_rows": int(table_rows or 0),
            "queue_wait_max": (round(float(qw_max), 1)
                               if qw_max is not None else None),
            "queue_wait_over_proxy_ceiling": int(qw_over or 0),
            "source": "market_power_scores",
        }
    except Exception as e:
        logger.warning("dcpi_methodology: live counts failed: %s", str(e)[:200])
        return {"available": False, "reason": f"{type(e).__name__}"}


@dcpi_methodology_bp.route("/api/v1/dcpi/methodology", methods=["GET"])
def dcpi_methodology():
    body = None
    counts = _live_counts()
    try:
        from util.dcpi_method import method_block, DCPI_METHOD_VERSION
        block = method_block(counts if counts.get("available") else None)
        if block and block.get("method_version"):
            body = {
                "ok": True,
                "source": "util/dcpi_method.py (emitted from the scorer constants)",
                "generated_from": ("the same Python constants routes/dcpi.py "
                                   "imports to score — not a transcription"),
                **block,
            }
            # Cheap self-check so the endpoint can never publish a partial doc
            # that looks complete. UNMEASURED emits null, never 0.
            body["completeness"] = {
                "inputs_documented": len(block.get("inputs") or []) or None,
                "fallbacks_documented": len(block.get("fallbacks") or []) or None,
                "revisions_documented": len(block.get("revisions") or []) or None,
                "limitations_documented": (
                    len(block.get("known_limitations") or []) or None),
            }
            # Say plainly whether the live-measured figures are present, so a
            # reader can tell "unmeasured in this process" from "measured".
            body["live_counts"] = counts
            _ = DCPI_METHOD_VERSION  # imported so a rename fails loudly here
    except Exception as e:
        logger.warning("dcpi_methodology: method_block failed: %s", str(e)[:200])

    if body is None:
        body = dict(_PINNED)

    # provenance-v1: DCPI is DC Hub's own model output, so 'inferred' is the
    # honest baseline for anything derived. Fail-soft — never 500 for a stamp.
    try:
        from routes.provenance import attach_provenance
        attach_provenance(
            body,
            source="DC Hub Power Index (DCPI) — scoring method",
            method=("emitted from util/dcpi_method.py, the constants "
                    "routes/dcpi.py scores with; method_version="
                    + str(body.get("method_version") or "unavailable")),
            as_of=body.get("scoring_unchanged_since"),
            # 'published' (not 'inferred'): this document is DC Hub's own
            # stated method, not a derived measurement.
            default_v="published",
        )
    except Exception:
        pass

    resp = jsonify(body)
    # Public and static — this is a document, not a measurement. 1h matches
    # canon_phrases and keeps a crawler from turning one page into N calls.
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
