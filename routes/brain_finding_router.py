"""routes/brain_finding_router.py — finding ROUTING for the tag-team loop.

WHY (docs/BRAIN_SUPERUSER_TAGTEAM.md, escalation ladder step 1)
===============================================================
The propose recorder proved the learn worklist is ~54 findings of which ~39
are already triaged NON-code (terminal_acknowledged / refused / permafail /
config_not_code) — yet every consumer still counts all 54 as "actionable
backlog":

  - brain_v2_layer4._cached_actionable_count() feeds the mirror the RAW
    detector count, so the self-grade reads a permanent 54/0 "jam" while the
    propose stage is in fact FLOWING and triaging;
  - nothing routes a triaged finding to its real OWNER. The mcp-server
    findings live in a repo the brain cannot PR (caller_tier='pro' to anon,
    the frozen quota meter — both current QA-superuser reds), and the
    config/env findings are operator decisions. Both sat in the same bucket
    as code the brain can fix.

WHAT
====
Pure classification over /api/v1/heal/findings items, keyed by the SAME
(issue[:200], url) persistence key the Layer-5 learn loop uses:

  active           — no terminal outcome recorded → the honest backlog
  operator_config  — config_not_code / not_code_availability → operator
                     worklist (surfaced here + in propose-stage status)
  mcp_server       — refused / no_source_map whose text names the mcp-server
                     surface → ONE deduped GitHub issue on the mcp-server
                     repo (the brain cannot PR it; an issue is the handoff)
  terminal         — acknowledged / refused / permafail → triaged, not
                     backlog

Routing is a VIEW plus one escalation write (the deduped cross-repo issue,
daily-guarded via brain_state, fail-soft). A finding is never dropped: it
stays in /api/v1/heal/findings until its detector stops emitting it, so a
misroute can only misLABEL, never lose evidence.

Kill switch: FINDING_ROUTER_DISABLE=1 (classification still works; the
GitHub write is skipped).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

brain_finding_router_bp = Blueprint("brain_finding_router", __name__)

# Outcomes that mean "triaged, not brain-code backlog". Must stay a superset
# of layer5's _TERMINAL_OC plus the ack outcome itself; skipped_permafail is
# matched by prefix because it is recorded as "skipped_permafail:<reason>".
TERMINAL_OUTCOMES = (
    "terminal_acknowledged",
    "config_not_code",
    "not_code_availability",
    "no_source_map",
    "refused",
)
OPERATOR_OUTCOMES = ("config_not_code", "not_code_availability")
_PERMAFAIL_PREFIX = "skipped_permafail"

# A finding is mcp-server-owned only when its own text names that surface.
# Conservative on purpose: a miss stays in `terminal` (safe); a false match
# would file an issue in the wrong repo.
MCP_SERVER_HINTS = (
    "server.mjs",
    "mcp-server",
    "dchub-mcp-server",
    "caller_tier",
    "tools/list",
    "remaining_full_today",
    "full_answers_remaining",
    "trial_preview",
)

_MCP_REPO = os.environ.get(
    "FINDING_ROUTER_MCP_REPO", "azmartone67/dchub-mcp-server")
_ISSUE_TITLE = "[brain-route] Findings owned by the mcp-server repo"
_BODY_MARKER = "<!-- brain-finding-router -->"
_SYNC_STATE_KEY = "finding_router_mcp_issue_synced"


def _disabled() -> bool:
    return os.environ.get("FINDING_ROUTER_DISABLE", "0") == "1"


# ── classification (pure — outcomes injectable for tests) ────────────────

def _load_outcomes() -> dict:
    try:
        from routes.brain_v2_store import last_outcomes_map
        return last_outcomes_map() or {}
    except Exception:
        return {}


DEFERRED_OUTCOME = "deferred_rate_cap"
DEFERRED_ALARM_H = 7 * 24.0


def _load_outcome_ages() -> dict:
    """{(issue_label, url): first_seen_at} for rows parked at deferred_rate_cap.
    ★2026-09-02 (D10): 27 of 29 active findings sat at deferred_rate_cap and
    nothing said for how long — the propose stage read "green with zero
    output". The age is since the row was FIRST seen (brain_issue_persistence
    keeps no per-outcome timestamp); a finding cannot have been deferred for
    longer than it has existed, so this is a ceiling stated as such."""
    try:
        from routes.brain_v2_store import _conn
        c = _conn()
        if c is None:
            return {}
        try:
            with c, c.cursor() as cur:
                cur.execute(
                    "SELECT issue_label, url, first_seen_at "
                    "  FROM brain_issue_persistence "
                    " WHERE last_outcome = %s ORDER BY last_seen_at DESC LIMIT 500",
                    (DEFERRED_OUTCOME,))
                return {(r[0], r[1] or ""): r[2] for r in cur.fetchall()}
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return {}


def deferred_age_h(item: dict, ages: dict, now=None) -> float | None:
    """Hours a finding has been parked at deferred_rate_cap (ceiling: since
    first seen). None when the row is unknown — unknown is not zero."""
    issue = item.get("issue") or ""
    url = item.get("url") or ""
    seen = ages.get((issue[:200], url)) or ages.get((issue, url))
    if seen is None:
        return None
    now = now or datetime.now(timezone.utc)
    if getattr(seen, "tzinfo", None) is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - seen).total_seconds() / 3600.0), 1)


def _outcome_for(item: dict, outcomes: dict) -> str:
    issue = item.get("issue") or ""
    url = item.get("url") or ""
    return (outcomes.get((issue[:200], url))
            or outcomes.get((issue, url)) or "")


def _is_terminal(outcome: str) -> bool:
    return bool(outcome) and (outcome in TERMINAL_OUTCOMES
                              or outcome.startswith(_PERMAFAIL_PREFIX))


def _names_mcp_surface(item: dict) -> bool:
    text = " ".join(str(item.get(k) or "")
                    for k in ("issue", "url", "detail")).lower()
    return any(h in text for h in MCP_SERVER_HINTS)


def classify_items(items: list, outcomes: dict | None = None,
                   ages: dict | None = None, now=None) -> dict:
    """Split findings into active / operator_config / mcp_server / terminal.

    Bucket precedence: operator outcomes are unambiguous and win first;
    refused/no_source_map findings that name the mcp-server surface route to
    mcp_server; every other terminal outcome is `terminal`; anything without
    a recorded terminal outcome is the honest `active` backlog.

    Active findings parked at deferred_rate_cap carry `deferred_age_h`, and
    `deferred_over_7d` counts the ones older than DEFERRED_ALARM_H — the
    number the radar's check_findings_deferred_over_7d alarms on."""
    if outcomes is None:
        outcomes = _load_outcomes()
    if ages is None:
        ages = _load_outcome_ages() if any(
            v == DEFERRED_OUTCOME for v in outcomes.values()) else {}
    buckets = {"active": [], "operator_config": [],
               "mcp_server": [], "terminal": []}
    over = 0
    for item in items or []:
        oc = _outcome_for(item, outcomes)
        entry = dict(item)
        entry["last_outcome"] = oc or None
        if oc == DEFERRED_OUTCOME:
            age = deferred_age_h(item, ages, now)
            entry["deferred_age_h"] = age
            if age is not None and age >= DEFERRED_ALARM_H:
                over += 1
        if not _is_terminal(oc):
            buckets["active"].append(entry)
        elif oc in OPERATOR_OUTCOMES:
            buckets["operator_config"].append(entry)
        elif oc in ("refused", "no_source_map") and _names_mcp_surface(item):
            buckets["mcp_server"].append(entry)
        else:
            buckets["terminal"].append(entry)
    buckets["counts"] = {k: len(v) for k, v in buckets.items()
                         if isinstance(v, list)}
    buckets["deferred_over_7d"] = over
    return buckets


def triaged_out_count(items: list, outcomes: dict | None = None) -> int:
    """How many of `items` are already triaged out of the brain backlog."""
    if outcomes is None:
        outcomes = _load_outcomes()
    return sum(1 for it in items or []
               if _is_terminal(_outcome_for(it, outcomes)))


def classify_live() -> dict:
    """Classify the live /api/v1/heal/findings lists (cache-served, cheap)."""
    from flask import current_app
    with current_app.test_client() as c:
        r = c.get("/api/v1/heal/findings")
        d = (r.get_json() or {}) if r.status_code == 200 else {}
    items = list(d.get("actionable_backend_issues") or []) \
        + list(d.get("actionable_frontend_issues") or [])
    out = classify_items(items)
    out["as_of"] = datetime.now(timezone.utc).isoformat()
    out["source_warming_up"] = bool(d.get("_warming_up"))
    return out


# ── brain_state helpers (daily guard for the cross-repo write) ───────────

def _db_url() -> str | None:
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def _state_get(key: str):
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute(
                "SELECT state_value FROM brain_state WHERE state_key=%s",
                (key,))
            row = cur.fetchone()
            val = row[0] if row else None
            if isinstance(val, str):
                val = json.loads(val or "null")
            return val
    except Exception:
        return None


def _state_set(key: str, value) -> bool:
    url = _db_url()
    if not url:
        return False
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_state (state_key, state_value, updated_at)
                   VALUES (%s, %s::jsonb, NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT (state_key)
                   DO UPDATE SET state_value = EXCLUDED.state_value,
                                 updated_at = NOW()""",
                (key, json.dumps(value)))
            conn.commit()
        return True
    except Exception:
        return False


# ── the one write: a deduped issue on the mcp-server repo ────────────────

def _issue_body(mcp_findings: list) -> str:
    lines = [
        _BODY_MARKER,
        "Auto-routed by the brain finding-router "
        "(docs/BRAIN_SUPERUSER_TAGTEAM.md, escalation ladder step 1).",
        "",
        "These findings were detected on the backend/QA side but are owned "
        "by THIS repo — the brain cannot open PRs here, so this issue is "
        "the handoff. The list is refreshed in place (never duplicated); "
        "a finding disappears when its detector stops emitting it.",
        "",
        "| finding | url | last_outcome | seen |",
        "|---|---|---|---|",
    ]
    for f in mcp_findings[:40]:
        lines.append("| %s | `%s` | %s | %s |" % (
            (f.get("issue") or "")[:160].replace("|", "\\|"),
            (f.get("url") or "")[:80],
            f.get("last_outcome") or "?",
            f.get("count") or 1))
    if len(mcp_findings) > 40:
        lines.append("")
        lines.append("_…and %d more (see /api/v1/brain/finding-routes)._"
                     % (len(mcp_findings) - 40))
    lines += ["", "_Updated %sZ_" % datetime.now(timezone.utc)
              .replace(tzinfo=None).isoformat(timespec="seconds")]
    return "\n".join(lines)


def sync_mcp_issue(classification: dict | None = None, force: bool = False,
                   session=None) -> dict:
    """Upsert ONE deduped issue on the mcp-server repo listing the
    mcp_server bucket. Daily-guarded (brain_state) unless force=True.
    Fail-soft: any error is returned, never raised."""
    if _disabled():
        return {"ok": True, "skipped": "FINDING_ROUTER_DISABLE=1"}
    today = datetime.now(timezone.utc).date().isoformat()
    if not force and _state_get(_SYNC_STATE_KEY) == today:
        return {"ok": True, "skipped": "already_synced_today"}
    try:
        cl = classification if classification is not None else classify_live()
        mcp = cl.get("mcp_server") or []
        if not mcp:
            return {"ok": True, "skipped": "no_mcp_findings"}
        token = (os.environ.get("GITHUB_TOKEN")
                 or os.environ.get("PR_SUBMIT_TOKEN"))
        if not token:
            return {"ok": False, "error": "no_github_token"}
        if session is None:
            import requests as session  # noqa: N813 — module as session
        headers = {"Authorization": "Bearer %s" % token,
                   "Accept": "application/vnd.github+json"}
        base = "https://api.github.com/repos/%s/issues" % _MCP_REPO
        r = session.get(base, headers=headers,
                        params={"state": "open", "per_page": 50}, timeout=10)
        if r.status_code != 200:
            return {"ok": False, "error": "list_issues_%s" % r.status_code}
        existing = next((i for i in (r.json() or [])
                         if "pull_request" not in i
                         and (i.get("title") or "").startswith(
                             "[brain-route]")), None)
        body = _issue_body(mcp)
        if existing:
            r2 = session.patch("%s/%d" % (base, existing["number"]),
                               headers=headers,
                               json={"body": body}, timeout=10)
            action, number = "updated", existing["number"]
        else:
            r2 = session.post(base, headers=headers,
                              json={"title": _ISSUE_TITLE, "body": body},
                              timeout=10)
            action, number = "created", (r2.json() or {}).get("number") \
                if r2.status_code < 300 else None
        if r2.status_code >= 300:
            return {"ok": False, "error": "%s_%s" % (action, r2.status_code)}
        _state_set(_SYNC_STATE_KEY, today)
        return {"ok": True, "action": action, "number": number,
                "routed": len(mcp)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


# ── endpoints (admin; /api/v1/brain/* already has a CF bypass rule) ──────

def _admin_ok_local() -> bool:
    try:
        from routes.brain_mechanical_classifier import _admin_ok
        return bool(_admin_ok())
    except Exception:
        key = os.environ.get("DCHUB_ADMIN_KEY", "")
        sent = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
        return bool(key) and sent == key


@brain_finding_router_bp.get("/api/v1/brain/finding-routes")
def finding_routes():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    out = {"ok": True}
    try:
        out.update(classify_live())
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=str(e)[:160])
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_finding_router_bp.post("/api/v1/brain/finding-routes/sync-mcp-issue")
def finding_routes_sync():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    force = request.args.get("force") == "1"
    result = sync_mcp_issue(force=force)
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp
