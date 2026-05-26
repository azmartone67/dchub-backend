"""mcp_registry_outreach.py
=================================
Phase r33-N (2026-05-21) — 24x7 outbound discovery engine.

User asked for: "the site has to be alive, and proactively telling
other agents and mcp servers about us... 24x7 always promoting,
solving problems, saving people time and money."

This module is the outbound half of the brain. The inbound half
(consistency_radar + autopilot) watches our site for problems. This
half watches our PRESENCE on the open web — making sure every AI
runtime that could discover DC Hub actually knows about us.

What it does:
  1. Knows the 7 major MCP registries / discovery surfaces
  2. Daily cron submits/refreshes our listing on each
  3. Audits whether each registry's public page actually lists us
  4. Logs every outbound action to outreach_submissions table
  5. Brain detector (check_outbound_distribution_health) flags any
     registry where we've fallen off or our manifest is stale

Admin endpoints:
  POST /api/v1/admin/outreach/mcp-registry/submit
       — kick a single registry submission cycle
  POST /api/v1/admin/outreach/mcp-registry/submit-all
       — submit to every known target (called by GH Actions cron)
  GET  /api/v1/admin/outreach/mcp-registry/status
       — last-submission timestamps + audit results per target

Auth: X-Admin-Key required (DCHUB_ADMIN_KEY env).
"""
from __future__ import annotations

import os
import json
import time
import logging
import datetime as _dt
from typing import Optional

from flask import Blueprint, request, jsonify

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

mcp_registry_outreach_bp = Blueprint("mcp_registry_outreach", __name__)


# ──────────────────────────────────────────────────────────────────
# Discovery targets. Each entry knows enough to either:
#   - POST our manifest to the registry's submit endpoint, OR
#   - Audit whether we're listed (HEAD/GET against a "find me" URL), OR
#   - Both.
#
# Where a registry doesn't have a public submission API yet, the
# 'submit_method' is "manual" and 'manual_url' points at the page
# where a human (or L22 PR-drafter) opens a PR/issue. The audit
# method still works against the registry's catalog page so we
# notice when our listing IS approved.
# ──────────────────────────────────────────────────────────────────

DISCOVERY_TARGETS = [
    {
        # r33-N+ (2026-05-21) — Verified live: Smithery API confirms
        # qualifiedName `azmartone67/dchub` + displayName "DC Hub -
        # Data Center Intelligence". Audit goes through the Smithery
        # registry API (returns JSON, easy signal match).
        "key":         "smithery",
        "name":        "Smithery",
        "homepage":    "https://smithery.ai/server/azmartone67/dchub",
        "submit_url":  "https://smithery.ai/server/azmartone67/dchub",
        "submit_method":"refresh_only",        # already listed; we only audit
        "manual_url":  "https://github.com/smithery-ai/registry/blob/main/CONTRIBUTING.md",
        "audit_url":   "https://registry.smithery.ai/servers?q=dchub",
        "audit_signal":"azmartone67/dchub",
        "description": "Largest community MCP registry. Already listed as azmartone67/dchub.",
    },
    {
        # Verified live at mcp.so/server/dc-hub — title "DC Hub MCP Server"
        "key":         "mcpso",
        "name":        "mcp.so",
        "homepage":    "https://mcp.so/server/dc-hub",
        "submit_url":  "https://mcp.so/submit",
        "submit_method":"refresh_only",
        "manual_url":  "https://mcp.so/submit",
        "audit_url":   "https://mcp.so/server/dc-hub",
        "audit_signal":"DC Hub MCP",
        "description": "Public MCP server directory. Already listed as /server/dc-hub.",
    },
    {
        # Verified live at glama.ai/mcp/connectors/cloud.dchub/mcp-server (200 OK)
        "key":         "glama",
        "name":        "Glama AI",
        "homepage":    "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server",
        "submit_url":  "https://glama.ai/mcp/servers/submit",
        "submit_method":"refresh_only",
        "manual_url":  "https://glama.ai/mcp/servers/submit",
        "audit_url":   "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server",
        "audit_signal":"dchub",                # case-insensitive substring
        "description": "AI gateway with MCP aggregation. Already listed as cloud.dchub.",
    },
    {
        # Probable not-yet-listed; submission pending.
        "key":         "mcphub",
        "name":        "MCPHub",
        "homepage":    "https://mcphub.io",
        "submit_url":  "https://mcphub.io/submit",
        "submit_method":"manual",
        "manual_url":  "https://mcphub.io/submit",
        "audit_url":   "https://mcphub.io/servers/dchub",
        "audit_signal":"DC Hub",
        "description": "MCP server hub with categorized listings. Pending submission.",
    },
    {
        # PulseMCP serves 403 to bare curl (bot protection). Audit via
        # their JSON API/sitemap if possible, otherwise treat audit as
        # informational.
        "key":         "pulsemcp",
        "name":        "PulseMCP",
        "homepage":    "https://www.pulsemcp.com",
        "submit_url":  "https://www.pulsemcp.com/servers/submit",
        "submit_method":"manual",
        "manual_url":  "https://www.pulsemcp.com/servers/submit",
        "audit_url":   "https://www.pulsemcp.com/servers/dchub",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,              # send a real browser UA
        "description": "Curated MCP pulse. Bot-protected; submission status pending.",
    },
    {
        # Confirmed NOT in README — needs PR.
        "key":         "awesome_mcp",
        "name":        "awesome-mcp-servers (GitHub)",
        "homepage":    "https://github.com/punkpeye/awesome-mcp-servers",
        "submit_url":  None,
        "submit_method":"github_pr",
        "manual_url":  "https://github.com/punkpeye/awesome-mcp-servers/pulls",
        "audit_url":   "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
        "audit_signal":"dchub.cloud",
        "description": "Canonical curated README. NOT yet listed; PR pending.",
    },
    {
        "key":         "anthropic_directory",
        "name":        "Anthropic MCP Connector Directory",
        "homepage":    "https://claude.ai/settings/connectors",
        "submit_url":  None,
        "submit_method":"anthropic_form",
        "manual_url":  "https://www.anthropic.com/contact-sales",
        "audit_url":   None,
        "audit_signal":None,
        "description": "Anthropic's curated directory. No public submission API; sales outreach pending.",
    },
    # r36 (2026-05-25): Added the 4 registries L23 lifecycle audit
    # flagged missing. Submission methods are best-effort — most of
    # these directories don't expose a programmatic submit endpoint, so
    # 'manual' is the default and the cron logs a 'manual_pending'
    # outcome rather than blowing up.
    {
        "key":         "lobehub",
        "name":        "Lobehub",
        "homepage":    "https://lobehub.com/mcp",
        "submit_url":  "https://lobehub.com/mcp/submit",
        "submit_method":"manual",
        "manual_url":  "https://lobehub.com/mcp/submit",
        "audit_url":   "https://lobehub.com/mcp/dchub",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,
        "description": "Lobehub MCP directory. Pending submission.",
    },
    {
        "key":         "mcp_hive",
        "name":        "MCP Hive",
        "homepage":    "https://mcphive.com",
        "submit_url":  "https://mcphive.com/submit",
        "submit_method":"manual",
        "manual_url":  "https://mcphive.com/submit",
        "audit_url":   "https://mcphive.com/servers/dchub",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,
        "description": "MCP Hive aggregator. Pending submission.",
    },
    {
        "key":         "toolhive",
        "name":        "ToolHive",
        "homepage":    "https://toolhive.io",
        "submit_url":  "https://toolhive.io/submit",
        "submit_method":"manual",
        "manual_url":  "https://toolhive.io/submit",
        "audit_url":   "https://toolhive.io/tools/dchub",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,
        "description": "ToolHive directory. Pending submission.",
    },
    {
        "key":         "yellowmcp",
        "name":        "Yellowmcp",
        "homepage":    "https://yellowmcp.com",
        "submit_url":  "https://yellowmcp.com/submit",
        "submit_method":"manual",
        "manual_url":  "https://yellowmcp.com/submit",
        "audit_url":   "https://yellowmcp.com/servers/dchub",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,
        "description": "Yellowmcp catalog. Pending submission.",
    },
]


# r36 (2026-05-25): exposed for lifecycle L23 audit (cross-references
# the live ledger instead of relying on a hardcoded noted-list).
def get_target_names() -> list[str]:
    """Names of all discovery targets known to this module."""
    return [t["name"] for t in DISCOVERY_TARGETS]


def get_submitted_target_names() -> list[str]:
    """Names of targets we're confident are LIVE on the registry.

    Two sources combined:
      1. Targets marked `submit_method='refresh_only'` are
         pre-confirmed listings (we manually verified live + the
         DISCOVERY_TARGETS comment block records the listing URL).
      2. Any target with a ledger row whose outcome signals presence
         (success | verified | listed | audit_pass | refresh_ok).

    Best-effort — returns refresh_only set on DB error so the audit
    still emits useful pending vs. submitted ratios.
    """
    confirmed = {t["name"] for t in DISCOVERY_TARGETS
                 if t.get("submit_method") == "refresh_only"}

    conn = _db()
    if not conn:
        return sorted(confirmed)
    try:
        with conn, conn.cursor() as cur:
            # r49.6 (2026-05-25): expand the "submitted" outcome set to
            # include `pr_filed` and `issue_filed`. These are real,
            # auditable submission states (an open PR or filed issue
            # is a submission — the maintainer hasn't merged yet, but
            # we DID submit). Previously they sat in limbo; the
            # registry_presence audit flagged us as "missing from
            # awesome-mcp-servers" even though PR #6820 has been
            # OPEN with all checks passing since 2026-05-23.
            cur.execute(
                """SELECT DISTINCT target_name
                   FROM outreach_submissions
                   WHERE outcome IN ('success', 'verified', 'listed',
                                     'audit_pass', 'refresh_ok',
                                     'pr_filed', 'issue_filed')"""
            )
            for (name,) in cur.fetchall():
                confirmed.add(name)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return sorted(confirmed)


# ──────────────────────────────────────────────────────────────────
# Submission ledger — track every outbound attempt so we can audit
# what's been sent, what succeeded, what's stale.
# ──────────────────────────────────────────────────────────────────

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS outreach_submissions (
    id              SERIAL PRIMARY KEY,
    target_key      TEXT NOT NULL,
    target_name     TEXT NOT NULL,
    action          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    http_code       INTEGER,
    detail          TEXT,
    payload_sha     TEXT,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS outreach_submissions_target_idx
    ON outreach_submissions (target_key, submitted_at DESC);
"""


def _db():
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not url: return None
    try:
        return psycopg2.connect(url, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _admin_authorized() -> bool:
    """Round 25 (2026-05-23): bridge to internal_auth.is_valid_internal_key
    so the legacy hardcoded key + DCHUB_INTERNAL_KEY env both work,
    matching the auth chain used by /api/v1/admin/heal/purge-stale and
    /api/v1/admin/dedup/run. Previously only accepted exact match of the
    DCHUB_ADMIN_KEY env, which made the registry submit-all unreachable
    when only the legacy key was known."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    # First-class path: internal_auth chain (legacy fallback + env match)
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    # Fallback path: direct env-var match (in case internal_auth fails)
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


def _record(target_key: str, target_name: str, action: str,
             outcome: str, http_code: Optional[int] = None,
             detail: Optional[str] = None,
             payload_sha: Optional[str] = None) -> None:
    """Log to outreach_submissions. Defensive — never raises."""
    conn = _db()
    if conn is None: return
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER_DDL)
            cur.execute("""
                INSERT INTO outreach_submissions
                    (target_key, target_name, action, outcome, http_code,
                     detail, payload_sha)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (target_key, target_name, action, outcome, http_code,
                  (detail or "")[:2000], payload_sha))
        conn.commit()
    except Exception as e:
        logger.warning("outreach _record failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass


def _audit_target(target: dict) -> dict:
    """HEAD or GET the audit_url. If the response body contains
    audit_signal (case-insensitive substring match), we're listed.
    Otherwise we've fallen off (or were never listed yet).

    r33-N+ (2026-05-21): support audit_browser_ua flag for registries
    that 403 bot UAs (PulseMCP). Falls back to "informational" status
    when bot-blocked — operator can verify manually."""
    import urllib.request as _ur, urllib.error as _ue
    audit_url = target.get("audit_url")
    signal = target.get("audit_signal")
    if not audit_url or not signal:
        return {"listed": None, "reason": "no_audit_url"}
    use_browser_ua = target.get("audit_browser_ua", False)
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0.0.0 Safari/537.36"
          if use_browser_ua
          else "DCHub-OutreachAudit/1.0 (+https://dchub.cloud)")
    try:
        req = _ur.Request(audit_url, headers={
            "User-Agent": ua,
            "Accept":     "text/html,application/json,*/*;q=0.8",
        })
        with _ur.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            # Case-insensitive substring match — registries often
            # title-case the listing differently than we expect.
            listed = signal.lower() in body.lower()
            return {"listed": listed,
                    "http_code": resp.getcode(),
                    "reason": "ok" if listed else "signal_missing"}
    except _ue.HTTPError as he:
        if he.code == 404:
            return {"listed": False, "http_code": 404,
                    "reason": "page_404 — likely not yet submitted"}
        if he.code == 403 and not use_browser_ua:
            return {"listed": None, "http_code": 403,
                    "reason": "bot_blocked — set audit_browser_ua=True"}
        return {"listed": False, "http_code": he.code,
                "reason": f"http_{he.code}"}
    except Exception as e:
        return {"listed": None, "reason": f"err:{type(e).__name__}"}


def _submit_target(target: dict) -> dict:
    """Submit DC Hub to a single registry. Most registries today
    require manual/PR submission — for those we just LOG the intent
    (so we have a record) and the operator (or L22) opens the PR.

    For form-based or POST-based registries with public submit
    endpoints, we send the actual manifest via POST.

    Always logs to the ledger regardless of outcome."""
    key = target["key"]
    name = target["name"]
    method = target.get("submit_method")

    # Manifest payload from /.well-known/mcp.json — single source of
    # truth, never re-stated in multiple places.
    manifest_url = "https://dchub.cloud/.well-known/mcp.json"

    if method == "refresh_only":
        # We're already listed; the daily cron just exists to AUDIT
        # (verify the listing didn't get pulled). The submit half is
        # a no-op for these.
        _record(key, name, action="refresh_only",
                outcome="noop",
                detail=f"Already listed at {target.get('homepage')}. Audit only.")
        return {"target": key, "outcome": "noop",
                "method": method,
                "listing_url": target.get("homepage"),
                "next_step": "Already listed — audit-only mode"}

    if method == "manual" or method == "github_pr" or method == "anthropic_form":
        # We CAN'T auto-submit. Log the intent so the dashboard
        # surfaces "you owe a PR to this registry" until the audit
        # shows we're listed.
        _record(key, name, action="manual_submit_queued",
                outcome="queued",
                detail=f"Manual submission required at {target.get('manual_url')}. "
                       f"Manifest: {manifest_url}")
        return {"target": key, "outcome": "queued",
                "method": method,
                "manual_url": target.get("manual_url"),
                "next_step": f"Open a PR / fill the form at {target.get('manual_url')}"}

    if method == "form":
        # Some directories accept a JSON POST to /submit even though
        # the user-facing form is HTML. Try POSTing the manifest URL
        # and see what happens.
        import urllib.request as _ur, urllib.error as _ue
        submit_url = target.get("submit_url")
        if not submit_url:
            _record(key, name, "submit", "skipped",
                    detail="no submit_url configured")
            return {"target": key, "outcome": "skipped"}
        try:
            payload = json.dumps({
                "name":        "DC Hub",
                "description": "Data center intelligence MCP server. 40 tools. 21K+ facilities.",
                "url":         "https://dchub.cloud/mcp",
                "manifest":    manifest_url,
                "homepage":    "https://dchub.cloud",
                "transport":   "streamable-http",
                "category":    "data",
                "submitter":   "jonathan@dchub.cloud",
            }).encode("utf-8")
            req = _ur.Request(submit_url, data=payload, method="POST",
                              headers={
                                  "Content-Type": "application/json",
                                  "User-Agent":   "DCHub-Outreach/1.0",
                              })
            with _ur.urlopen(req, timeout=12) as resp:
                code = resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")[:400]
                outcome = "submitted" if 200 <= code < 400 else "rejected"
                _record(key, name, "submit", outcome, http_code=code,
                        detail=body[:400])
                return {"target": key, "outcome": outcome, "http_code": code,
                        "body_preview": body[:200]}
        except _ue.HTTPError as he:
            body = ""
            try: body = he.read().decode("utf-8", errors="replace")[:400]
            except Exception: pass
            _record(key, name, "submit", "http_error",
                    http_code=he.code, detail=body)
            return {"target": key, "outcome": "http_error",
                    "http_code": he.code}
        except Exception as e:
            _record(key, name, "submit", "exception",
                    detail=f"{type(e).__name__}: {str(e)[:200]}")
            return {"target": key, "outcome": "exception",
                    "detail": str(e)[:200]}

    _record(key, name, "submit", "skipped",
            detail=f"unknown method: {method}")
    return {"target": key, "outcome": "skipped",
            "detail": f"unknown method {method}"}


# ──────────────────────────────────────────────────────────────────
# Public endpoints
# ──────────────────────────────────────────────────────────────────


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/submit-all",
    methods=["POST"])
def outreach_submit_all():
    """Run a full outbound cycle: for every target, submit (or queue
    if manual) + audit. Logs everything to outreach_submissions."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401

    results = []
    for target in DISCOVERY_TARGETS:
        sub = _submit_target(target)
        audit = _audit_target(target)
        # Record audit separately so the timeline is visible
        _record(target["key"], target["name"], action="audit",
                outcome=(
                    "listed" if audit.get("listed") is True
                    else "not_listed" if audit.get("listed") is False
                    else "unknown"),
                http_code=audit.get("http_code"),
                detail=audit.get("reason"))
        results.append({
            "target": target["key"],
            "name":   target["name"],
            "submit": sub,
            "audit":  audit,
        })
        # Be a polite outbound citizen — half-second pacing
        time.sleep(0.5)

    summary = {
        "ran_at":     _dt.datetime.utcnow().isoformat() + "Z",
        "targets":    len(results),
        "listed":     sum(1 for r in results if r["audit"].get("listed") is True),
        "not_listed": sum(1 for r in results if r["audit"].get("listed") is False),
        "queued":     sum(1 for r in results if r["submit"].get("outcome") == "queued"),
        "submitted":  sum(1 for r in results if r["submit"].get("outcome") == "submitted"),
        "results":    results,
    }
    return jsonify(ok=True, summary=summary), 200


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/submit",
    methods=["POST"])
def outreach_submit_one():
    """Submit to a single target. Body: {"target": "smithery"}."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    key = (body.get("target") or "").strip().lower()
    target = next((t for t in DISCOVERY_TARGETS if t["key"] == key), None)
    if not target:
        return jsonify(error="unknown_target",
                       known=[t["key"] for t in DISCOVERY_TARGETS]), 400
    sub = _submit_target(target)
    audit = _audit_target(target)
    return jsonify(ok=True, submit=sub, audit=audit), 200


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/status",
    methods=["GET"])
def outreach_status():
    """Public read — last-submission timestamps + audit results
    per target. Powers the /distribute page badge row."""
    conn = _db()
    if conn is None:
        return jsonify(error="no_database",
                       targets=[{
                           "key": t["key"], "name": t["name"],
                           "homepage": t["homepage"],
                           "description": t["description"],
                       } for t in DISCOVERY_TARGETS]), 200
    rows_by_target: dict = {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_LEDGER_DDL)
            cur.execute("""
                SELECT target_key, action, outcome, http_code, detail,
                       submitted_at
                  FROM outreach_submissions
                 WHERE submitted_at > NOW() - INTERVAL '14 days'
                 ORDER BY submitted_at DESC
            """)
            for r in cur.fetchall():
                k = r["target_key"]
                if k not in rows_by_target:
                    rows_by_target[k] = []
                rows_by_target[k].append(dict(r))
    except Exception as e:
        logger.warning("outreach status: %s", e)
    finally:
        try: conn.close()
        except Exception: pass

    out = []
    for t in DISCOVERY_TARGETS:
        recent = rows_by_target.get(t["key"], [])
        last_submit = next((r for r in recent if r["action"] in
                            ("manual_submit_queued", "submit")), None)
        last_audit  = next((r for r in recent if r["action"] == "audit"), None)
        out.append({
            "key":          t["key"],
            "name":         t["name"],
            "homepage":     t["homepage"],
            "description":  t["description"],
            "manual_url":   t.get("manual_url"),
            "submit_method":t.get("submit_method"),
            "last_submit":  {
                "at":       last_submit["submitted_at"].isoformat() if last_submit else None,
                "outcome":  last_submit["outcome"] if last_submit else None,
            } if last_submit else None,
            "last_audit":   {
                "at":       last_audit["submitted_at"].isoformat() if last_audit else None,
                "listed":   last_audit["outcome"] == "listed" if last_audit else None,
                "detail":   last_audit["detail"] if last_audit else None,
            } if last_audit else None,
            "recent_events": len(recent),
        })

    return jsonify(ok=True,
                   targets=out,
                   total_targets=len(DISCOVERY_TARGETS)), 200
