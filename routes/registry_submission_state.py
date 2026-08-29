"""routes/registry_submission_state.py — where each registry submission STANDS.

WHY (2026-08-29)
================
registry_truth answers "is our listing correct?" and registry_acquisition
answers "which directories don't list us?". Neither answers the question
that actually wasted the most effort:

    have we ALREADY submitted here, and what happened to it?

Nothing recorded that, so it got re-derived from scratch every time — and
re-derived WRONG. On 2026-08-29 an audit checked presence by grepping each
repo's root README.md and concluded "9 of 10 GitHub MCP lists are missing
DC Hub". Two duplicate pull requests were filed off that conclusion before
it was caught. The truth:

  · TensorBlock had MERGED our entry on 2026-07-12 — into docs/, not the
    README, so a README grep could never see it.
  · YuzeHao2023 (#378) and rohitg00 (#301) already had OPEN pull requests
    from us, which no content probe of any kind can see.
  · appcypher was ARCHIVED on 2026-08-01 — read-only for everyone.
  · wong2 has pull requests and issues DISABLED; its web form is the only
    door, exactly as registry_acquisition had recorded.

So the acquisition problem was never "we don't submit". It is that
submissions sit in queues (YuzeHao 232 open PRs, rohitg00 63, the Docker
MCP registry ~869) and nothing remembers they were made.

★★★ THE TWO PROBES — AND A CONTENT GREP IS NEITHER
--------------------------------------------------
A registry's state is only decidable from BOTH of:

  1. REPO CAPABILITY — archived? pull requests disabled? (`/pulls` 404)
  2. OUR SUBMISSION HISTORY — `search/issues?q=repo:X+is:pr+author:US`

Content probes are advisory only, and this module will not conclude
`absent` from one. Reasons, all observed:
  · entries may live outside README.md (TensorBlock: docs/*.md)
  · GitHub CODE SEARCH is unreliable here — it returned 0 for TensorBlock
    while the entry was live at docs/data-analysis--business-intelligence.md
  · an open PR is invisible to every content probe by definition

STATES
------
  listed    our entry is in the registry (merged PR, or identity confirmed)
  pending   we submitted; it is open and unmerged  -> carries days_waiting
  absent    capability OK, no submission on record, and we could read it
  no_door   archived / PRs disabled / no public submission route
  unknown   we could NOT determine it  (never collapsed into any of the above)

Surfaces
--------
  GET|POST /api/v1/admin/registry-submission-state/scan   (probes + persists)
  GET      /api/v1/admin/registry-submission-state        (pure DB read)
Kill: REGISTRY_SUBMISSION_STATE_DISABLE=1

The scan does the HTTP; the read is pure DB, so the white-glove agent's
acquisition lane can consume it without a self-request (the invariant behind
the 2026-07-06 flywheel outage).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

registry_submission_state_bp = Blueprint("registry_submission_state", __name__)

KILL_SWITCH_ENV = "REGISTRY_SUBMISSION_STATE_DISABLE"
HTTP_TIMEOUT = 12
_OUR_LOGIN = (os.environ.get("DCHUB_GITHUB_LOGIN") or "azmartone67").strip()

STATE_LISTED = "listed"
STATE_PENDING = "pending"
STATE_ABSENT = "absent"
STATE_NO_DOOR = "no_door"
STATE_UNKNOWN = "unknown"

# GitHub-hosted registries. `path_hint` is advisory only — it is NEVER the
# basis for an `absent` verdict (that is the bug this module exists for).
GITHUB_REGISTRIES = [
    {"registry": "punkpeye_awesome_mcp",  "repo": "punkpeye/awesome-mcp-servers"},
    {"registry": "appcypher_awesome_mcp", "repo": "appcypher/awesome-mcp-servers"},
    {"registry": "wong2_awesome_mcp",     "repo": "wong2/awesome-mcp-servers",
     "fallback_submit": "https://mcpservers.org/submit"},
    {"registry": "chatmcp_mcpso",         "repo": "chatmcp/mcpso",
     "note": "repo is the site's code, not the listing; mcp.so listings are DB-driven"},
    {"registry": "yuzehao_awesome_mcp",   "repo": "YuzeHao2023/Awesome-MCP-Servers"},
    {"registry": "rohitg00_devops_mcp",   "repo": "rohitg00/awesome-devops-mcp-servers"},
    {"registry": "tensorblock_awesome_mcp", "repo": "TensorBlock/awesome-mcp-servers"},
    {"registry": "pipedream_awesome_mcp", "repo": "PipedreamHQ/awesome-mcp-servers"},
    {"registry": "toolsdk_mcp_registry",  "repo": "toolsdk-ai/toolsdk-mcp-registry"},
    {"registry": "ever_works_awesome_mcp", "repo": "ever-works/awesome-mcp-servers"},
    {"registry": "docker_mcp_registry",   "repo": "docker/mcp-registry"},
]

_DDL = """
CREATE TABLE IF NOT EXISTS registry_submission_state (
    registry      TEXT PRIMARY KEY,
    repo          TEXT,
    kind          TEXT,
    state         TEXT,
    pr_number     INTEGER,
    pr_url        TEXT,
    pr_state      TEXT,
    submitted_at  TIMESTAMPTZ,
    days_waiting  INTEGER,
    open_pr_backlog INTEGER,
    evidence      TEXT,
    checked_at    TIMESTAMPTZ
)
"""


def _db():
    try:
        from routes.mcp_presence_crawler import _db_conn as _c
        return _c()
    except Exception:
        return None


def _gh_token() -> str:
    return (os.environ.get("PR_SUBMIT_TOKEN")
            or os.environ.get("GITHUB_TOKEN") or "").strip()


def _gh(path: str, token: str):
    """GET api.github.com/<path>. Returns (status, json_or_None).
    Never raises — a transport failure is reported as status 0 so the
    caller records `unknown` rather than inventing a verdict."""
    try:
        r = requests.get(
            f"https://api.github.com/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=HTTP_TIMEOUT)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None
    except Exception as e:
        logger.debug("[submission-state] GET %s failed: %s", path, e)
        return 0, None


def classify(repo_meta, repo_status, our_prs, backlog=None, now=None):
    """Pure decision function — unit-tested, no I/O.

    repo_meta   : dict from GET /repos/{repo}, or None
    repo_status : HTTP status of GET /repos/{repo}/pulls (404 => PRs off)
    our_prs     : list of {number,url,state,merged,created_at} authored by us,
                  or None meaning THE SEARCH DID NOT RUN (never "no PRs").
    """
    now = now or datetime.now(timezone.utc)
    if repo_meta is None:
        return {"state": STATE_UNKNOWN, "kind": "unknown",
                "evidence": "repository metadata unreadable"}
    if repo_meta.get("archived"):
        return {"state": STATE_NO_DOOR, "kind": "archived",
                "evidence": "repository is archived (read-only for everyone)"}
    if repo_status == 404:
        return {"state": STATE_NO_DOOR, "kind": "prs_disabled",
                "evidence": "pull requests are disabled on this repository"}

    # ★ None means we could not look. It must NEVER read as "no submission".
    if our_prs is None:
        return {"state": STATE_UNKNOWN, "kind": "github_pr",
                "evidence": "our submission history could not be searched"}

    merged = [p for p in our_prs if p.get("merged")]
    if merged:
        p = merged[0]
        return {"state": STATE_LISTED, "kind": "github_pr",
                "pr_number": p.get("number"), "pr_url": p.get("url"),
                "pr_state": "merged",
                "evidence": f"our PR #{p.get('number')} was merged"}
    openp = [p for p in our_prs if (p.get("state") or "").lower() == "open"]
    if openp:
        p = sorted(openp, key=lambda x: x.get("created_at") or "")[0]
        days = None
        if p.get("created_at"):
            try:
                c = datetime.fromisoformat(
                    p["created_at"].replace("Z", "+00:00"))
                days = int((now - c).total_seconds() // 86400)
            except Exception:
                days = None
        ev = f"our PR #{p.get('number')} is open"
        if days is not None:
            ev += f", waiting {days}d"
        if backlog:
            ev += f" behind {backlog} open PRs on the repo"
        return {"state": STATE_PENDING, "kind": "github_pr",
                "pr_number": p.get("number"), "pr_url": p.get("url"),
                "pr_state": "open", "submitted_at": p.get("created_at"),
                "days_waiting": days, "open_pr_backlog": backlog,
                "evidence": ev}
    return {"state": STATE_ABSENT, "kind": "github_pr",
            "evidence": "repository accepts PRs and we have none on record"}


def run_scan() -> dict:
    """Probe every GitHub-hosted registry and persist its state. Never raises."""
    out = {"ok": True, "scanned": 0, "rows": [], "counts": {}}
    if os.environ.get(KILL_SWITCH_ENV) == "1":
        return {"ok": False, "disabled": True, "reason": f"{KILL_SWITCH_ENV}=1"}
    token = _gh_token()
    if not token:
        # No token => the submission search cannot run => every verdict would
        # be `unknown`. Say so rather than writing a table full of guesses.
        return {"ok": False, "error": "no_github_token",
                "note": "submission history is unsearchable without a token; "
                        "no state was written"}
    conn = _db()
    if conn is None:
        return {"ok": False, "error": "db_unavailable"}
    now = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            for entry in GITHUB_REGISTRIES:
                repo = entry["repo"]
                st_meta, meta = _gh(f"repos/{repo}", token)
                if st_meta != 200:
                    meta = None
                st_pulls, _ = _gh(f"repos/{repo}/pulls?state=all&per_page=1", token)

                our_prs = None
                backlog = None
                if meta is not None and st_pulls != 404:
                    q = f"repo:{repo}+is:pr+author:{_OUR_LOGIN}"
                    st_s, res = _gh(f"search/issues?q={q}&per_page=20", token)
                    if st_s == 200 and isinstance(res, dict):
                        our_prs = [
                            {"number": i.get("number"),
                             "url": i.get("html_url"),
                             "state": i.get("state"),
                             "merged": bool((i.get("pull_request") or {})
                                            .get("merged_at")),
                             "created_at": i.get("created_at")}
                            for i in (res.get("items") or [])]
                    st_b, bres = _gh(
                        f"search/issues?q=repo:{repo}+is:pr+is:open&per_page=1",
                        token)
                    if st_b == 200 and isinstance(bres, dict):
                        backlog = bres.get("total_count")

                v = classify(meta, st_pulls, our_prs, backlog, now)
                if entry.get("fallback_submit") and v["state"] == STATE_NO_DOOR:
                    v["evidence"] += f" — submit via {entry['fallback_submit']}"
                if entry.get("note"):
                    v["evidence"] += f" ({entry['note']})"

                cur.execute(
                    "INSERT INTO registry_submission_state "
                    "(registry, repo, kind, state, pr_number, pr_url, pr_state, "
                    " submitted_at, days_waiting, open_pr_backlog, evidence, checked_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (registry) DO UPDATE SET "
                    " repo=EXCLUDED.repo, kind=EXCLUDED.kind, state=EXCLUDED.state, "
                    " pr_number=EXCLUDED.pr_number, pr_url=EXCLUDED.pr_url, "
                    " pr_state=EXCLUDED.pr_state, submitted_at=EXCLUDED.submitted_at, "
                    " days_waiting=EXCLUDED.days_waiting, "
                    " open_pr_backlog=EXCLUDED.open_pr_backlog, "
                    " evidence=EXCLUDED.evidence, checked_at=EXCLUDED.checked_at",
                    (entry["registry"], repo, v.get("kind"), v["state"],
                     v.get("pr_number"), v.get("pr_url"), v.get("pr_state"),
                     v.get("submitted_at"), v.get("days_waiting"),
                     v.get("open_pr_backlog"), v.get("evidence", "")[:900], now))
                out["rows"].append({"registry": entry["registry"], **v})
                out["scanned"] += 1
        conn.commit()
        for r in out["rows"]:
            out["counts"][r["state"]] = out["counts"].get(r["state"], 0) + 1
    except Exception as e:
        logger.error("[submission-state] scan failed: %s", e, exc_info=True)
        out.update(ok=False, error=str(e)[:200])
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _authorized() -> bool:
    try:
        from routes.mcp_presence_crawler import _admin_or_cron_authorized
        return _admin_or_cron_authorized()
    except Exception:
        provided = (request.headers.get("X-Admin-Key")
                    or request.args.get("admin_key") or "").strip()
        expected = (os.environ.get("DCHUB_ADMIN_KEY")
                    or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
        return bool(expected) and provided == expected


@registry_submission_state_bp.route(
    "/api/v1/admin/registry-submission-state/scan", methods=["GET", "POST"])
def rss_scan():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(run_scan())


@registry_submission_state_bp.route(
    "/api/v1/admin/registry-submission-state", methods=["GET"])
def rss_read():
    """Pure DB read — the white-glove agent consumes this table directly."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    conn = _db()
    if conn is None:
        return jsonify({"ok": False, "error": "db_unavailable"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            cur.execute(
                "SELECT registry, repo, kind, state, pr_url, days_waiting, "
                "       open_pr_backlog, evidence, checked_at "
                "  FROM registry_submission_state ORDER BY state, registry")
            rows = [dict(zip(("registry", "repo", "kind", "state", "pr_url",
                              "days_waiting", "open_pr_backlog", "evidence",
                              "checked_at"),
                             (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                              r[8].isoformat() if r[8] else None)))
                    for r in cur.fetchall()]
        conn.commit()
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        return jsonify({
            "ok": True, "counts": counts, "registries": rows,
            "note": "state is decided from repo capability + OUR PR history. "
                    "A content grep is never the basis for `absent` — entries "
                    "can live outside README.md and an open PR is invisible to "
                    "any content probe."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
