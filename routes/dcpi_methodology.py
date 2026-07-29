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
No DB, no network, no imports beyond util.dcpi_method (pure constants). If even
that fails, a PINNED minimal block is served with source="PINNED (fallback)" —
same shape as routes/canon_phrases.py. A consumer never gets nothing, and never
gets a silent zero.

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


@dcpi_methodology_bp.route("/api/v1/dcpi/methodology", methods=["GET"])
def dcpi_methodology():
    body = None
    try:
        from util.dcpi_method import method_block, DCPI_METHOD_VERSION
        block = method_block()
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
