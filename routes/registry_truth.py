"""routes/registry_truth.py — registry listings, verified honestly (2026-07-27).

WHY THIS EXISTS (and why the previous attempts did not stick)
============================================================
Directory/registry discovery produces ~84% of all new agent arrivals. The
white-glove loop has been maintaining those listings nightly for weeks and
reporting healthy. On 2026-07-27 a listing-by-listing check against what a
VISITOR actually sees found:

  · 11 of 16 tracked listings could not be read at all — 403 (pulsemcp),
    429 (cursor.directory), or a 200 that silently redirects to a SEARCH
    page (glama) — and every one of them recorded drift_detected = FALSE.
  · So the board said "3 drifted of 16" when the truth was
    "3 drifted, 11 UNKNOWN, 2 verified".

That is the same failure this codebase keeps re-learning: absence of
evidence recorded as evidence of health. `drift_detected` is a BOOLEAN, and
a boolean cannot express "I could not look." Every prior fix trusted that
flag, so every prior fix inherited the blind spot.

WHAT THIS DOES DIFFERENTLY
--------------------------
A four-state verdict per listing, where "could not check" is its own state
and is never clean:

  verified_ok      page resolves, IS OURS (identity match), content agrees
  verified_drift   page resolves, IS OURS, content contradicts canon
  broken           resolves to something that is NOT our listing —
                   redirect to a search/query page, 404, or a page with no
                   trace of our identity. This is the Glama case and the
                   one that silently costs arrivals.
  unverified       403 / 429 / timeout / unparseable — WE DO NOT KNOW.
                   Never reported as healthy. Ages into a red.

IDENTITY BEFORE CONTENT. A 200 is not proof the page is ours; the body must
carry one of our identity tokens (dchub.cloud/mcp, the endpoint, our server
name). Checking counts on a page that isn't ours is how "0 tools" got
recorded for Glama as if it were a real reading of our listing.

LAG IS NOT BREAKAGE. Registries re-crawl on their own schedule and
Smithery's index is known to lag by design (do NOT chase it — the 2026-06
"dead URL" misdiagnosis). A listing that is OURS and merely behind canon is
verified_drift, which self-heals when they re-crawl; only a listing that is
not ours, or unreadable, escalates.

NO FAKE PUSH. Registries have no working refresh webhook — the speculative
/refresh + /reindex POSTs were deleted 2026-07-17 after every one 404'd.
The only real path is: bump our manifest -> they re-crawl. This module
therefore only ever READS and records.

Surface:  GET|POST /api/v1/admin/registry-truth/scan   (crawls + persists)
          GET      /api/v1/admin/registry-truth        (JSON, pure DB read)
Kill:     REGISTRY_TRUTH_DISABLE=1
"""

from __future__ import annotations

import datetime
import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

registry_truth_bp = Blueprint("registry_truth", __name__)

# A listing we cannot read is tolerable for a little while (registries rate
# limit, and a single 429 means nothing). Past this it is a real red — we
# have been blind to that listing for over a week.
UNVERIFIED_RED_DAYS = 7

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# The body must prove the page is OURS before any count is believed.
_IDENTITY_TOKENS = ("dchub.cloud/mcp", "dchub.cloud", "dc hub", "dchub")

# A URL that lands on a search/query/browse page is NOT a listing, even at
# HTTP 200. Glama redirects /servers/dchub -> /servers?query=author%3Adchub.
_SEARCH_MARKERS = ("?query=", "?search=", "/search", "?q=", "/servers?")


def _disabled() -> bool:
    return (os.environ.get("REGISTRY_TRUTH_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _db():
    try:
        from routes.brain_rag import _db as _rag_db
        return _rag_db()
    except Exception as e:  # noqa: BLE001
        logger.debug("[registry-truth] db unavailable: %s", e)
        return None


# ── the classifier (pure — unit-tested without network) ───────────────

def classify(url: str, status, final_url: str, body: str,
             canon_tools) -> dict:
    """Four-state verdict for one listing fetch.

    Pure function: every input is already-fetched evidence, so the decision
    table is testable without touching the network.
    """
    out = {"verdict": "unverified", "reason": "", "found_tools": None,
           "http_status": status, "final_url": final_url or url}

    if status is None:
        out["reason"] = "fetch failed (timeout or connection error)"
        return out
    if status in (401, 403, 429):
        out["reason"] = "blocked by the registry (HTTP %s) — cannot read" % status
        return out
    if status >= 400:
        out["verdict"] = "broken"
        out["reason"] = "HTTP %s — the listing URL does not serve a page" % status
        return out

    text_head = (body or "").lstrip()[:400]
    is_json = text_head.startswith("{") or text_head.startswith("[")
    # v2: an API QUERY endpoint legitimately carries ?search= — the official
    # MCP registry's tracked URL is /v0/servers?search=cloud.dchub and returns
    # JSON that DOES contain us. Applying the search-page rule to it cried
    # wolf on a healthy listing, which is how earlier versions of this check
    # got ignored. For JSON, identity in the payload is the test.
    fu = (final_url or url or "").lower()
    if (not is_json) and any(m in fu for m in _SEARCH_MARKERS):
        out["verdict"] = "broken"
        out["reason"] = ("resolves to a SEARCH page, not a listing (%s) — the "
                         "tracked URL is wrong or the listing was removed" % fu)
        return out

    text = (body or "")
    low = text.lower()
    if not text.strip():
        out["reason"] = "empty body — cannot read"
        return out

    # IDENTITY BEFORE CONTENT — never read counts off a page that isn't ours.
    if not any(tok in low for tok in _IDENTITY_TOKENS):
        out["verdict"] = "broken"
        out["reason"] = ("page carries no DC Hub identity — it is not our "
                         "listing (wrong URL, or delisted)")
        return out

    # v2: a page can carry SEVERAL different counts at once (Surface Truth
    # #30 found four live simultaneously). Taking the FIRST match made the
    # verdict depend on page order — mcp.so carries both "79 tools" (x6) and
    # "55 tools" (x1). Count every distinct value, prefer the most frequent,
    # and surface disagreement rather than hiding it behind one number.
    counts: dict = {}
    for mm in re.finditer(r"(\d{1,3})\s*tools", low):
        v = int(mm.group(1))
        counts[v] = counts.get(v, 0) + 1
    found = max(counts, key=lambda k: (counts[k], k)) if counts else None
    out["found_tools"] = found
    if len(counts) > 1:
        out["all_counts"] = dict(sorted(counts.items()))
    if found is None:
        out["verdict"] = "verified_ok"
        out["reason"] = "our listing, no tool count published on the page"
        return out
    if canon_tools and found != int(canon_tools):
        out["verdict"] = "verified_drift"
        out["reason"] = ("publishes %d tools, canon is %s — registry re-crawl "
                         "pending or the listing needs an edit"
                         % (found, canon_tools))
        return out
    out["verdict"] = "verified_ok"
    out["reason"] = ("in sync (%s tools)" % found
                     + ("; page also shows %s — mixed numbers live at once"
                        % ", ".join(str(k) for k in out["all_counts"] if k != found)
                        if out.get("all_counts") else ""))
    return out


# ── crawl + persist ───────────────────────────────────────────────────

def _ensure_columns(cur) -> None:
    for ddl in (
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_verdict TEXT",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_reason TEXT",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_http_status INT",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_final_url TEXT",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_checked_at TIMESTAMPTZ",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_ok_at TIMESTAMPTZ",
        # ★ 2026-07-27: the scan MEASURED found_tools and threw it away, so the
        # only tool count on the row was dchub_metric_published_tools from an
        # older crawler run. /mcp-standing then paired TODAY's verification date
        # with a number from weeks ago — a date vouching for a measurement it
        # never took. Store the count beside the verdict that observed it.
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS truth_found_tools INT",
    ):
        try:
            cur.execute(ddl)
        except Exception:  # noqa: BLE001
            pass


def _fetch(url: str):
    try:
        import requests as _rq
        r = _rq.get(url, headers={"User-Agent": _UA}, timeout=20,
                    allow_redirects=True)
        return r.status_code, str(r.url), r.text
    except Exception as e:  # noqa: BLE001
        logger.debug("[registry-truth] fetch %s: %s", url, e)
        return None, url, ""


def run_scan() -> dict:
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    try:
        from routes.white_glove_propagation import load_canon
        canon_tools = (load_canon() or {}).get("tools")
    except Exception:  # noqa: BLE001
        canon_tools = None
    results = []
    try:
        with c.cursor() as cur:
            _ensure_columns(cur)
            cur.execute("SELECT registry_name, listing_url FROM mcp_presence_listings "
                        "WHERE listing_url IS NOT NULL ORDER BY registry_name")
            rows = cur.fetchall()
        for name, url in rows:
            status, final_url, body = _fetch(url)
            v = classify(url, status, final_url, body, canon_tools)
            v["registry"] = name
            v["url"] = url
            results.append(v)
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE mcp_presence_listings SET truth_verdict=%s,"
                        " truth_reason=%s, truth_http_status=%s,"
                        " truth_final_url=%s, truth_found_tools=%s,"
                        " truth_checked_at=NOW(),"
                        " truth_ok_at = CASE WHEN %s = 'verified_ok' THEN NOW()"
                        "                    ELSE truth_ok_at END"
                        " WHERE registry_name=%s",
                        (v["verdict"], v["reason"][:400], v["http_status"],
                         (v["final_url"] or "")[:400], v.get("found_tools"),
                         v["verdict"], name))
                c.commit()
            except Exception:  # noqa: BLE001
                try:
                    c.rollback()
                except Exception:
                    pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    counts: dict = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"ok": True, "canon_tools": canon_tools,
            "checked": len(results), "counts": counts, "listings": results,
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z"}


def read_state() -> dict:
    """Pure-DB read — safe for a shell lane (no outbound HTTP, preserving
    the no-self-request invariant from the 2026-07-06 flywheel outage)."""
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT registry_name, truth_verdict, truth_reason,"
                "       truth_checked_at, truth_ok_at,"
                "       EXTRACT(EPOCH FROM (NOW() - COALESCE(truth_ok_at,"
                "         truth_checked_at, NOW() - interval '99 days')))/86400.0"
                "  FROM mcp_presence_listings ORDER BY registry_name")
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}
    finally:
        try:
            c.close()
        except Exception:
            pass
    listings, counts = [], {}
    broken, stale_unverified = [], []
    for name, verdict, reason, checked, ok_at, days in rows:
        v = verdict or "never_checked"
        counts[v] = counts.get(v, 0) + 1
        d = float(days or 99)
        listings.append({"registry": name, "verdict": v, "reason": reason,
                         "days_since_ok": round(d, 1)})
        if v == "broken":
            broken.append(name)
        elif v in ("unverified", "never_checked") and d > UNVERIFIED_RED_DAYS:
            stale_unverified.append(name)
    return {"ok": True, "counts": counts, "listings": listings,
            "broken": broken, "stale_unverified": stale_unverified,
            "critical": bool(broken or stale_unverified)}


@registry_truth_bp.route("/api/v1/admin/registry-truth/scan",
                         methods=["GET", "POST"])
def scan():
    if _disabled():
        return jsonify(ok=False, error="REGISTRY_TRUTH_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(run_scan())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@registry_truth_bp.route("/api/v1/admin/registry-truth", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(read_state())
    resp.headers["Cache-Control"] = "no-store"
    return resp
