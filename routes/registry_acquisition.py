"""routes/registry_acquisition.py — the ACQUISITION half of registry presence.

WHY (2026-07-27)
================
White-glove + registry-truth MAINTAIN the 16 listings we already have.
Nothing looks for directories we are ABSENT from. That is the growth half,
and it is the one that matters: directories produce ~84% of new agent
arrivals (262 of ~313 first-seen real-external IPs in 30d arrive as generic
`platform=mcp`), while arrivals are flat-to-declining (~12/day).

Maintenance keeps presence from rotting. Only acquisition grows it.

WHAT IT DOES
------------
For every candidate directory, two independent questions, each answered
honestly or not at all:

  1. IS THE DIRECTORY REAL?   fetch its home page. A candidate that does
                              not resolve is `dead_directory` and drops out
                              of the queue — the seed list below is a
                              STARTING GUESS, and the loop is what decides
                              which entries are real. Nothing here asserts
                              a directory exists because it appears in the
                              seed.
  2. ARE WE ON IT?            fetch its search/listing URL and look for a
                              DC Hub identity token.

Verdicts: `present` · `absent` (-> submission queue) · `dead_directory` ·
`unverified` (403/429/timeout — we could NOT check, and that is never
reported as "we're present" nor as "we're absent").

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not submit. The speculative registry-refresh webhooks were deleted
2026-07-17 after every POST 404'd; almost every directory takes a manual
form or a GitHub PR. So this produces a REVIEWED QUEUE — name, why we're
absent, and the exact submit URL — and a human (or a separate, explicitly
authorised job) acts on it. A module that pretends to submit is worse than
one that admits it cannot: that is precisely the fake-push lesson.

Surface:  GET|POST /api/v1/admin/registry-acquisition/scan  (crawl+persist)
          GET      /api/v1/admin/registry-acquisition       (queue, pure DB)
Kill:     REGISTRY_ACQUISITION_DISABLE=1
"""

from __future__ import annotations

import datetime
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

registry_acquisition_bp = Blueprint("registry_acquisition", __name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_IDENTITY_TOKENS = ("dchub.cloud", "dchub", "dc hub")

# ── candidate directories ────────────────────────────────────────────
# A STARTING GUESS, not a claim. Each entry is verified at scan time: a
# candidate whose home page does not resolve becomes `dead_directory` and
# leaves the queue. Add rows freely — a wrong guess costs one fetch and
# self-removes; it can never produce a false "we should submit here".
#
#   home   — proves the directory exists at all
#   probe  — a URL that would show DC Hub IF we were listed (search page
#            preferred; these sites' search pages are the honest test)
#   submit — where a human goes to add us
CANDIDATE_DIRECTORIES = [
    {"name": "mcpservers_org", "home": "https://mcpservers.org/",
     "probe": "https://mcpservers.org/?q=dchub", "submit": "https://mcpservers.org/"},
    {"name": "mcp_run", "home": "https://www.mcp.run/",
     "probe": "https://www.mcp.run/search?q=dchub", "submit": "https://www.mcp.run/"},
    {"name": "opentools", "home": "https://opentools.com/",
     "probe": "https://opentools.com/registry?q=dchub", "submit": "https://opentools.com/"},
    {"name": "mcpmarket", "home": "https://mcpmarket.com/",
     "probe": "https://mcpmarket.com/search?q=dchub", "submit": "https://mcpmarket.com/submit"},
    {"name": "himcp_ai", "home": "https://himcp.ai/",
     "probe": "https://himcp.ai/?s=dchub", "submit": "https://himcp.ai/submit"},
    {"name": "mcp_get", "home": "https://mcp-get.com/",
     "probe": "https://mcp-get.com/packages?q=dchub", "submit": "https://mcp-get.com/"},
    {"name": "composio", "home": "https://composio.dev/",
     "probe": "https://composio.dev/tools?q=dchub", "submit": "https://composio.dev/"},
    {"name": "fleur", "home": "https://www.fleurmcp.com/",
     "probe": "https://www.fleurmcp.com/?q=dchub", "submit": "https://www.fleurmcp.com/"},
    {"name": "toolbase", "home": "https://gettoolbase.ai/",
     "probe": "https://gettoolbase.ai/?q=dchub", "submit": "https://gettoolbase.ai/"},
    {"name": "wong2_awesome_mcp", "home": "https://mcpservers.org/",
     "probe": "https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md",
     "submit": "https://github.com/wong2/awesome-mcp-servers/pulls"},
    {"name": "punkpeye_awesome_mcp",
     "home": "https://github.com/punkpeye/awesome-mcp-servers",
     "probe": "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
     "submit": "https://github.com/punkpeye/awesome-mcp-servers/pulls"},
    {"name": "appcypher_awesome_mcp",
     "home": "https://github.com/appcypher/awesome-mcp-servers",
     "probe": "https://raw.githubusercontent.com/appcypher/awesome-mcp-servers/main/README.md",
     "submit": "https://github.com/appcypher/awesome-mcp-servers/pulls"},
]


def _disabled() -> bool:
    return (os.environ.get("REGISTRY_ACQUISITION_DISABLE") or "").strip() == "1"


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
        logger.debug("[registry-acq] db unavailable: %s", e)
        return None


# ── the classifier (pure — unit-tested without network) ──────────────

def classify_candidate(home_status, probe_status, probe_body) -> dict:
    """Two-question verdict from already-fetched evidence.

    Question 1 gates question 2: if the directory itself does not resolve,
    "are we listed" is meaningless and must not be answered.
    """
    out = {"verdict": "unverified", "reason": ""}

    # Q1 — is the directory real?
    if home_status is None:
        out["reason"] = "directory home unreachable — cannot tell if it exists"
        return out
    if home_status >= 400 and home_status not in (401, 403, 429):
        out["verdict"] = "dead_directory"
        out["reason"] = ("home returns HTTP %s — not a live directory; drop it "
                         "from the candidate list" % home_status)
        return out

    # Q2 — are we on it?
    if probe_status is None or probe_status in (401, 403, 429):
        out["reason"] = ("directory is live but the probe could not be read "
                         "(HTTP %s) — presence UNKNOWN, not absent"
                         % (probe_status if probe_status else "timeout"))
        return out
    if probe_status >= 400:
        out["reason"] = ("probe URL returns HTTP %s — the probe is wrong, not "
                         "necessarily our absence" % probe_status)
        return out

    low = (probe_body or "").lower()
    if not low.strip():
        out["reason"] = "empty probe body — presence UNKNOWN"
        return out
    if any(tok in low for tok in _IDENTITY_TOKENS):
        out["verdict"] = "present"
        out["reason"] = "DC Hub found on the directory"
        return out
    out["verdict"] = "absent"
    out["reason"] = "directory is live and does not list DC Hub — submittable"
    return out


def _fetch(url: str):
    try:
        import requests as _rq
        r = _rq.get(url, headers={"User-Agent": _UA}, timeout=20,
                    allow_redirects=True)
        return r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        logger.debug("[registry-acq] fetch %s: %s", url, e)
        return None, ""


_DDL = """
CREATE TABLE IF NOT EXISTS registry_acquisition_candidates (
    name          TEXT PRIMARY KEY,
    home_url      TEXT,
    probe_url     TEXT,
    submit_url    TEXT,
    verdict       TEXT,
    reason        TEXT,
    home_status   INT,
    probe_status  INT,
    checked_at    TIMESTAMPTZ,
    first_absent_at TIMESTAMPTZ
)
"""


def run_scan() -> dict:
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    results = []
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
        c.commit()
        for cand in CANDIDATE_DIRECTORIES:
            hs, _ = _fetch(cand["home"])
            ps, pb = _fetch(cand["probe"])
            v = classify_candidate(hs, ps, pb)
            v.update({"name": cand["name"], "submit_url": cand["submit"],
                      "home_status": hs, "probe_status": ps})
            results.append(v)
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO registry_acquisition_candidates"
                        " (name, home_url, probe_url, submit_url, verdict,"
                        "  reason, home_status, probe_status, checked_at,"
                        "  first_absent_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),"
                        "         CASE WHEN %s='absent' THEN NOW() END)"
                        " ON CONFLICT (name) DO UPDATE SET"
                        "  verdict=EXCLUDED.verdict, reason=EXCLUDED.reason,"
                        "  home_status=EXCLUDED.home_status,"
                        "  probe_status=EXCLUDED.probe_status,"
                        "  checked_at=NOW(),"
                        "  first_absent_at = CASE"
                        "    WHEN EXCLUDED.verdict='absent'"
                        "     AND registry_acquisition_candidates.first_absent_at IS NOT NULL"
                        "    THEN registry_acquisition_candidates.first_absent_at"
                        "    WHEN EXCLUDED.verdict='absent' THEN NOW() ELSE NULL END",
                        (cand["name"], cand["home"], cand["probe"],
                         cand["submit"], v["verdict"], v["reason"][:400],
                         hs, ps, v["verdict"]))
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
    return {"ok": True, "checked": len(results), "counts": counts,
            "candidates": results,
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z"}


def read_queue() -> dict:
    """Pure-DB read — safe for a shell lane (no outbound HTTP; preserves the
    no-self-request invariant from the 2026-07-06 flywheel outage)."""
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT name, verdict, reason, submit_url, checked_at,"
                "       first_absent_at"
                "  FROM registry_acquisition_candidates"
                " ORDER BY (verdict='absent') DESC, name")
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}
    finally:
        try:
            c.close()
        except Exception:
            pass
    counts, queue, unverified = {}, [], []
    for name, verdict, reason, submit, checked, since in rows:
        counts[verdict or "?"] = counts.get(verdict or "?", 0) + 1
        if verdict == "absent":
            queue.append({"directory": name, "submit_url": submit,
                          "reason": reason,
                          "absent_since": since.isoformat() if since else None})
        elif verdict == "unverified":
            unverified.append(name)
    return {"ok": True, "counts": counts,
            "submission_queue": queue, "unverified": unverified,
            "queue_depth": len(queue),
            "note": ("absent = the directory is live and does not list us; "
                     "submit via submit_url. Nothing here auto-submits — most "
                     "directories take a manual form or a GitHub PR.")}


@registry_acquisition_bp.route(
    "/api/v1/admin/registry-acquisition/scan", methods=["GET", "POST"])
def scan():
    if _disabled():
        return jsonify(ok=False, error="REGISTRY_ACQUISITION_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(run_scan())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@registry_acquisition_bp.route("/api/v1/admin/registry-acquisition",
                               methods=["GET"])
def queue():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(read_queue())
    resp.headers["Cache-Control"] = "no-store"
    return resp
