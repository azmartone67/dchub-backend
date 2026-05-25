"""
brain_ecosystem_watch.py — Phase r46 (2026-05-25).

Watches the MCP ecosystem for competitive pressure. The L23 lifecycle
curator audits OUR moat-health; this layer watches THEIR moves. When
a new MCP server appears in our category (data-center / energy /
infrastructure) or when our position changes on a directory's
leaderboard, we want to know.

Approach: lightweight HEAD/GET probes of public registry index pages.
We don't scrape full catalogs — we look for the small set of signals
that indicate competition or position change. Findings persist in
brain_ecosystem_watch table for L23 to consume via a new audit dim.

Endpoints:
  POST /api/v1/brain/ecosystem/watch        admin: kick a watch cycle
  GET  /api/v1/brain/ecosystem/findings     latest findings + summary

Schema is auto-created on import. Cron (daily) fires the watch cycle.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.request
import urllib.error
from typing import Any

from flask import Blueprint, jsonify, request


brain_ecosystem_watch_bp = Blueprint("brain_ecosystem_watch", __name__)


ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "")


# ── Watch targets ──────────────────────────────────────────────────
# We probe with a real-browser UA because most registries 403 bare curl.
# audit_signal is a case-insensitive substring; presence → we're listed.
# competition_signal is a substring → competitor entry exists.

_WATCH_TARGETS = [
    {
        "key": "smithery",
        "name": "Smithery",
        "url": "https://smithery.ai/category/data",
        "self_signal": "dchub",
        "competition_signal": "data-center",  # any data-center server
    },
    {
        "key": "glama",
        "name": "Glama",
        "url": "https://glama.ai/mcp/servers",
        "self_signal": "dchub",
        "competition_signal": "data-center",
    },
    {
        "key": "mcp_so",
        "name": "mcp.so",
        "url": "https://mcp.so/servers",
        "self_signal": "dc-hub",
        "competition_signal": "data center",
    },
]


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS brain_ecosystem_watch (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_key TEXT NOT NULL,
    target_name TEXT NOT NULL,
    we_present BOOLEAN,
    competition_seen BOOLEAN,
    http_status INTEGER,
    page_bytes INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_brain_ecosystem_at
    ON brain_ecosystem_watch (at DESC);
CREATE INDEX IF NOT EXISTS ix_brain_ecosystem_target
    ON brain_ecosystem_watch (target_key, at DESC);
"""


def _conn():
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL"))
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


_ensure_schema()


def _admin_ok() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    return bool(ADMIN_KEY) and provided == ADMIN_KEY


def _probe(target: dict) -> dict:
    """Single target probe — returns presence + competition findings."""
    req = urllib.request.Request(
        target["url"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(200_000)  # cap at 200KB so we don't OOM
            status = r.status
    except urllib.error.HTTPError as e:
        return {
            "target_key": target["key"], "target_name": target["name"],
            "we_present": None, "competition_seen": None,
            "http_status": e.code, "page_bytes": 0,
            "detail": f"http_{e.code}",
        }
    except Exception as e:
        return {
            "target_key": target["key"], "target_name": target["name"],
            "we_present": None, "competition_seen": None,
            "http_status": 0, "page_bytes": 0,
            "detail": f"{type(e).__name__}: {str(e)[:80]}",
        }

    body_lower = body.lower()
    we_present = (target["self_signal"].lower().encode("utf-8")
                  in body_lower)
    competition_seen = (target["competition_signal"].lower().encode("utf-8")
                        in body_lower)

    return {
        "target_key":       target["key"],
        "target_name":      target["name"],
        "we_present":       we_present,
        "competition_seen": competition_seen,
        "http_status":      status,
        "page_bytes":       len(body),
        "detail":           "probed ok",
    }


def _record(finding: dict) -> None:
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_ecosystem_watch
                  (target_key, target_name, we_present, competition_seen,
                   http_status, page_bytes, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                finding["target_key"], finding["target_name"],
                finding.get("we_present"), finding.get("competition_seen"),
                finding.get("http_status"), finding.get("page_bytes"),
                finding.get("detail"),
            ))
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


@brain_ecosystem_watch_bp.route(
    "/api/v1/brain/ecosystem/watch", methods=["POST"])
def ecosystem_watch():
    """Run a watch cycle against all targets. Admin-keyed."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    _ensure_schema()
    findings = []
    for t in _WATCH_TARGETS:
        f = _probe(t)
        _record(f)
        findings.append(f)
    return jsonify({
        "ok": True,
        "findings": findings,
        "targets_count": len(_WATCH_TARGETS),
        "we_present_count": sum(1 for f in findings if f.get("we_present")),
        "competition_seen_count": sum(1 for f in findings if f.get("competition_seen")),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }), 200


@brain_ecosystem_watch_bp.route(
    "/api/v1/brain/ecosystem/findings", methods=["GET"])
def ecosystem_findings():
    """Latest finding per target — what's the brain's current view of
    the ecosystem? Consumed by the L23 ecosystem_position audit dim."""
    _ensure_schema()
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db_unreachable",
                        "by_target": {}}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (target_key)
                  target_key, target_name, at, we_present,
                  competition_seen, http_status, detail
                FROM brain_ecosystem_watch
                ORDER BY target_key, at DESC
            """)
            rows = cur.fetchall()
        by_target = {}
        for r in rows:
            by_target[r[0]] = {
                "target_name":     r[1],
                "at":              r[2].isoformat() if r[2] else None,
                "we_present":      r[3],
                "competition_seen": r[4],
                "http_status":     r[5],
                "detail":          r[6],
            }
        # Summary
        present_count = sum(1 for v in by_target.values() if v.get("we_present"))
        return jsonify({
            "ok": True,
            "by_target": by_target,
            "summary": {
                "targets_known":   len(by_target),
                "we_present_in":   present_count,
                "expected_total":  len(_WATCH_TARGETS),
            },
            "purpose": (
                "MCP ecosystem competitive watch. POST /watch to refresh; "
                "GET /findings to read latest. L23 audit reads this for "
                "the ecosystem_position dim — flags weak if we're absent "
                "from more than 1 of the watched targets."
            ),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass
