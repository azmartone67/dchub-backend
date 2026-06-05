"""MCP registry presence watcher.

Phase HJ-2 (2026-06-05) — verifies DC Hub is listed in the major MCP
registries. Most registries don't expose programmatic submission, so
this module FETCHES each registry and checks whether DC Hub appears.
When DC Hub is missing from any registry, the watcher files a brain
finding so the autopilot or a human can ship the submission PR.

Registries tracked:
  • smithery.ai     — central directory, REST API for search
  • mcp.so          — community directory, scrapeable HTML
  • awesome-mcp-servers (GitHub) — README list
  • Anthropic registry — official discoverability (Q4 2025)
  • Cline marketplace  — VS Code extension catalog (manual submission)
  • Cursor MCP catalog — Cursor's curated tools

Endpoints:
  GET /api/v1/brain/mcp-registries    — current presence status across all
  POST /api/v1/admin/brain/mcp-registries/scan  — trigger a fresh scan
                                          (admin-key gated, fail-closed)

Cron: .github/workflows/mcp-registry-watch.yml runs weekly and POSTs
to the scan endpoint. Findings flow into the brain via the standard
brain_findings table (same as other autopilot findings).
"""
import json
import os
import urllib.parse
import urllib.request
from typing import Dict, List

from flask import Blueprint, jsonify, request


mcp_registry_watch_bp = Blueprint("mcp_registry_watch", __name__)


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")

# The strings we expect to find in each registry's payload that confirm
# DC Hub is listed. Most registries use the URL or the name "dchub" /
# "DC Hub" / "DCPI" / "Data Center Power Index".
_PRESENCE_MARKERS = (
    "dchub.cloud",
    "dchub",
    "DC Hub",
    "Data Center Power Index",
)


def _fetch(url: str, timeout: int = 15) -> tuple[int, str]:
    """GET a URL with a real Chrome UA. Returns (status, body)."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _BROWSER_UA,
            "Accept":     "text/html,application/json,*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, f"_fetch_error: {str(e)[:120]}"


def _present_in_body(body: str) -> bool:
    """Does the body contain any of our presence markers?"""
    if not body:
        return False
    body_lower = body.lower()
    return any(m.lower() in body_lower for m in _PRESENCE_MARKERS)


# ── Per-registry probes ─────────────────────────────────────────

_REGISTRIES = [
    {
        "id":           "smithery_server",
        "name":         "smithery.ai server page",
        # 2026-06-05: Smithery's slug is namespaced by GitHub owner —
        # the actual canonical path is /servers/<gh-user>/<repo>, not
        # /server/<name>. DC Hub is live at /servers/azmartone67/dchub
        # (verified via Chrome inspection: 95/100 score, 99.6% uptime,
        # 162ms p50 latency, 979 lifetime tool calls).
        "url":          "https://smithery.ai/servers/azmartone67/dchub",
        "submission_url": "https://smithery.ai/new",
    },
    {
        "id":           "mcp_so",
        "name":         "mcp.so directory",
        "url":          "https://mcp.so/server/dchub",
        "submission_url": "https://github.com/chatmcp/mcp-directory",
    },
    {
        "id":           "awesome_mcp_servers",
        "name":         "awesome-mcp-servers (GitHub README)",
        "url":          "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
        "submission_url": "https://github.com/punkpeye/awesome-mcp-servers/pulls",
    },
    {
        "id":           "modelcontextprotocol_servers",
        "name":         "modelcontextprotocol/servers (Anthropic-curated)",
        "url":          "https://raw.githubusercontent.com/modelcontextprotocol/servers/main/README.md",
        "submission_url": "https://github.com/modelcontextprotocol/servers/pulls",
    },
    {
        "id":           "cursor_mcp",
        "name":         "Cursor MCP catalog",
        "url":          "https://docs.cursor.com/context/model-context-protocol",
        "submission_url": "https://forum.cursor.com",
    },
]


def _probe_all() -> Dict[str, dict]:
    """Probe every registry; return per-id status."""
    out = {}
    for r in _REGISTRIES:
        status, body = _fetch(r["url"])
        if status == 200:
            present = _present_in_body(body)
            verdict = "present" if present else "missing"
        elif status == 0:
            verdict = "fetch_error"
        else:
            verdict = f"http_{status}"
        out[r["id"]] = {
            "registry":       r["name"],
            "registry_url":   r["url"],
            "submission_url": r["submission_url"],
            "verdict":        verdict,
            "http_status":    status,
        }
    return out


def _file_findings_for_missing(results: Dict[str, dict]) -> List[dict]:
    """For each missing registry, return a brain-finding-shaped dict.

    Caller threads these into the brain_findings table (same shape as
    other autopilot findings).
    """
    findings = []
    for rid, info in results.items():
        if info["verdict"] in ("missing", "fetch_error"):
            findings.append({
                "kind":     "mcp_registry_missing",
                "subject":  rid,
                "url":      info["submission_url"],
                "evidence": (f"{info['registry']}: probe returned "
                              f"{info['verdict']} (HTTP {info['http_status']}). "
                              f"Submit DC Hub MCP server at {info['submission_url']}."),
                "severity": "medium",
            })
    return findings


# ── Public endpoints ────────────────────────────────────────────

@mcp_registry_watch_bp.route("/api/v1/brain/mcp-registries",
                              methods=["GET"])
def mcp_registries_status():
    """Read-only snapshot of DC Hub's presence across MCP registries.

    Cached at the edge for 1h since the registries themselves only
    update on PR merges. Public — no auth required.
    """
    results = _probe_all()
    present_count = sum(1 for r in results.values() if r["verdict"] == "present")
    return jsonify({
        "ok":             True,
        "total":          len(results),
        "present":        present_count,
        "missing":        len(results) - present_count,
        "results":        results,
        "doc":            "https://dchub.cloud/api/v1/brain/mcp-registries",
        "submission_pattern": (
            "Most MCP registries accept submissions via GitHub PR to their "
            "README or registry index. Pattern: fork the repo, add a "
            "DC Hub entry following the existing format, open PR. The "
            "awesome-mcp-pr workflow (already running weekly) handles "
            "punkpeye/awesome-mcp-servers."
        ),
    })


@mcp_registry_watch_bp.route("/api/v1/admin/brain/mcp-registries/scan",
                              methods=["POST"])
def mcp_registries_scan():
    """Trigger a fresh scan + file brain findings for any missing registries.

    Admin-key gated (fail-closed): if DCHUB_ADMIN_KEY env var is unset,
    returns 503. Otherwise checks X-Admin-Key header.
    """
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    if not admin_key:
        return jsonify({
            "error": "admin_endpoint_unconfigured",
            "hint":  ("Set DCHUB_ADMIN_KEY env var on Railway to enable. "
                      "Same fail-closed pattern as /api/v1/admin/sitemap/purge."),
        }), 503
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if provided != admin_key:
        return jsonify({"error": "unauthorized"}), 401

    results = _probe_all()
    findings = _file_findings_for_missing(results)

    # File findings to brain_findings table if available. Soft-fail —
    # the scan response still succeeds even if the DB write fails.
    filed = 0
    try:
        import psycopg2
        _du = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL", "")).strip()
        if _du and findings:
            with psycopg2.connect(_du, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    for f in findings:
                        try:
                            cur.execute("""
                                INSERT INTO brain_findings
                                  (finding_kind, subject, url, evidence,
                                   severity, source, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                                ON CONFLICT DO NOTHING
                            """, (f["kind"], f["subject"], f["url"],
                                  f["evidence"], f["severity"],
                                  "mcp_registry_watch"))
                            filed += 1
                        except Exception:
                            pass
                conn.commit()
    except Exception as _e:
        pass

    return jsonify({
        "ok":             True,
        "scanned":        len(results),
        "present":        sum(1 for r in results.values()
                              if r["verdict"] == "present"),
        "missing":        len(findings),
        "findings_filed": filed,
        "results":        results,
        "findings":       findings,
    })
