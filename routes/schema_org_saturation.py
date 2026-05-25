"""Phase GGGGG (2026-05-16) — schema.org saturation audit + injection.

Source-of-truth score is 10/100. Citation hunter (TTTT) confirmed
Claude doesn't mention DC Hub in answers. One of the cheapest fixes
is making sure EVERY page has rich schema.org markup so AI agents
have something concrete to cite when they crawl us.

This module:
  1. Audits every Sentinel-monitored HTML page for schema.org blocks
  2. Surfaces pages missing structured data as brain findings
  3. Provides a small reusable schema-generator for new pages

  GET /api/v1/schema-org/audit         coverage report
  GET /api/v1/schema-org/missing       pages without schema (worklist)

For pages we OWN that lack schema, the operator can patch them.
For pages already shipped with schema, this validates they're
still serving it correctly.
"""

from __future__ import annotations

import os
import re
import datetime
import requests
from flask import Blueprint, jsonify, request


schema_org_saturation_bp = Blueprint("schema_org_saturation", __name__)


_SITE_BASE = os.environ.get("DCHUB_SITE_BASE_URL", "https://dchub.cloud").rstrip("/")


# Pages we KNOW should have schema markup + the expected type.
# When the audit runs, missing markup OR wrong @type fires a finding.
_REQUIRED_SCHEMA = [
    ("/",                                "Organization|WebSite"),
    ("/intelligence",                    "WebApplication"),
    ("/transparency",                    "WebApplication"),
    ("/vs",                              "WebPage|ItemList"),
    ("/sentinel",                        "WebApplication"),
    ("/dcpi/totals",                     "Dataset"),
    ("/transactions",                    "Dataset"),
    ("/operators",                       "ItemList"),
    ("/spare-capacity",                  "WebApplication"),
    ("/pocket-listings",                 "WebPage"),
    ("/markets/northern-virginia/deep-dive", "Article"),
    ("/reports/quarterly",               "Report"),
    ("/events",                          "ItemList"),
]


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_org_audit (
    path           TEXT PRIMARY KEY,
    expected_types TEXT NOT NULL,
    has_jsonld     BOOLEAN,
    found_types    TEXT,
    last_checked   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


_JSONLD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        re.IGNORECASE | re.DOTALL)
_TYPE_RE   = re.compile(r'"@type"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _audit_page(path: str) -> dict:
    """Fetch + scan for schema.org JSON-LD blocks. Returns {has_jsonld,
    found_types[], status, bytes}."""
    out = {"path": path, "has_jsonld": False, "found_types": [],
           "status": 0, "bytes": 0}
    try:
        r = requests.get(f"{_SITE_BASE}{path}", timeout=12, headers={
            "User-Agent": "DCHub-Schema-Audit/1.0",
        })
        out["status"] = r.status_code
        body = r.text[:200_000]  # cap
        out["bytes"] = len(body)
        if r.status_code != 200:
            return out
        blocks = _JSONLD_RE.findall(body)
        out["has_jsonld"] = bool(blocks)
        types = []
        for b in blocks:
            for m in _TYPE_RE.finditer(b):
                types.append(m.group(1))
        out["found_types"] = list(set(types))
    except Exception as e:
        out["error"] = f"{type(e).__name__}:{str(e)[:80]}"
    return out


def run_audit() -> dict:
    """Audit every required page; persist + return summary.

    r41-schema-parallel (2026-05-25): the per-page HTTP fetch loop is
    now parallelized via ThreadPoolExecutor. Pre-fix each page fetched
    serially with a 12s timeout, so ~20 pages × ~1.2s avg = ~24s wall
    time (4th-slowest brain detector). Now ~3-5s. The DB INSERT is
    moved out of the loop and batched after all fetches return.
    """
    summary = {"total": len(_REQUIRED_SCHEMA), "ok": 0, "missing": 0,
                "wrong_type": 0, "unreachable": 0,
                "ran_at": datetime.datetime.utcnow().isoformat() + "Z",
                "details": []}
    c = _conn()
    if c is not None:
        _ensure_schema(c)

    import concurrent.futures as _cf

    def _audit_with_expected(item):
        path, expected = item
        return path, expected, _audit_page(path)

    with _cf.ThreadPoolExecutor(max_workers=8,
                                 thread_name_prefix="schema-audit") as ex:
        results = list(ex.map(_audit_with_expected, _REQUIRED_SCHEMA))

    rows_to_persist = []
    for path, expected, a in results:
        exp_types = [t.strip() for t in expected.split("|")]
        status_ok = a.get("status") == 200
        types_ok  = bool(a.get("found_types")) and any(
            t in exp_types for t in (a.get("found_types") or [])
        )
        if not status_ok:
            verdict = "unreachable"
            summary["unreachable"] += 1
        elif not a.get("has_jsonld"):
            verdict = "missing"
            summary["missing"] += 1
        elif not types_ok:
            verdict = "wrong_type"
            summary["wrong_type"] += 1
        else:
            verdict = "ok"
            summary["ok"] += 1
        summary["details"].append({
            **a, "expected_types": expected, "verdict": verdict,
        })
        rows_to_persist.append(
            (path, expected, a.get("has_jsonld"),
             ",".join(a.get("found_types") or []))
        )

    # Persist all rows after fetches complete (single connection held briefly)
    if c is not None and rows_to_persist:
        try:
            with c.cursor() as cur:
                from psycopg2.extras import execute_values
                execute_values(cur, """
                    INSERT INTO schema_org_audit
                      (path, expected_types, has_jsonld, found_types, last_checked)
                    VALUES %s
                    ON CONFLICT (path) DO UPDATE
                      SET expected_types = EXCLUDED.expected_types,
                          has_jsonld     = EXCLUDED.has_jsonld,
                          found_types    = EXCLUDED.found_types,
                          last_checked   = NOW()
                """, [(p, e, h, f, datetime.datetime.utcnow())
                       for (p, e, h, f) in rows_to_persist])
            c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass
    if c is not None:
        try: c.close()
        except Exception: pass
    summary["coverage_pct"] = round(100.0 * summary["ok"] / max(1, summary["total"]), 1)
    return summary


@schema_org_saturation_bp.route("/api/v1/schema-org/audit", methods=["GET"])
def audit_endpoint():
    """Public coverage report. Cron can also POST without body to refresh."""
    out = run_audit()
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@schema_org_saturation_bp.route("/api/v1/schema-org/missing", methods=["GET"])
def missing_endpoint():
    """Just the worklist — pages that need schema added."""
    out = run_audit()
    missing = [d for d in (out.get("details") or [])
               if d.get("verdict") in ("missing", "wrong_type")]
    return jsonify({"missing": missing, "count": len(missing),
                    "coverage_pct": out.get("coverage_pct")}), 200
