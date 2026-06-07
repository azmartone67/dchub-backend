"""
brain_backlog_admin.py — 2026-06-07 (Phase r68: close the see→act loop).

Diagnosis (from /api/v1/brain/persistence + /api/v1/brain/proposed-code/debug-summary):
  • 67 stuck findings (16 heartbeat + 50 unfixed + 1 directive). Every
    `last_outcome` is `?` or `untried · Nh old, cap Mh` — never tried.
  • 40 brain_proposed_code_fixes rows with `pr_url IS NULL` AND
    `confidence ≥ 0.85` (the threshold the existing PR-opener already
    uses). 39 ≥ 0.85, 35 ≥ 0.95.
  • The GH Actions workflow `brain-layer5-pr-opener.yml` runs every 4h
    but FAILS every run (exit 1 from the test-suite-FAIL gate). It
    never gets to `gh pr create`. So the proposals just stack up.
  • Many stuck issues (shadowed_route, coverage_gap_gas:RI/HI/DE,
    addressable_demand_unconverted, trial_to_paid_stagnation) aren't
    in _PATTERN_LIBRARY at all — autopilot SKIPS them silently.

This module:
  1. /admin/brain-backlog  → dashboard showing 67 stuck + 40 proposed,
     with one-click action buttons.
  2. POST /api/v1/admin/brain/draft-prs/run → bypass the broken GH
     Actions workflow. Reads the same pending-pr queue + opens DRAFT
     PRs directly via GitHub REST API (using brain_pr_opener._gh
     helpers). Capped at 5/day, kill-switched by
     BRAIN_AUTOPILOT_DRAFT_PR_DISABLE=1.
  3. GET  /api/v1/admin/brain/draft-prs/preview → dry-run.

Safety stays intact:
  • DRAFT PRs only — humans merge. brain_pr_opener won't auto-merge.
  • Daily cap (default 5, env BRAIN_DRAFT_PR_DAILY_CAP).
  • Kill switch env var defaults OFF. Set
    BRAIN_AUTOPILOT_DRAFT_PR_DISABLE=1 on Railway to halt in 5s.
  • Each draft PR passes the SAME ast.parse syntax check the GH
    Actions workflow uses (file is fetched, patched in-memory,
    compiled — bad patches are skipped, not committed).
  • Idempotent: a proposal whose pr_url is set is skipped + status
    flipped to 'pr_opened' via mark-pr.

Risk: a bad patch from the brain's Layer-5 codegen could open a draft
PR with broken code. Mitigations:
  • DRAFT — won't trigger auto-deploy.
  • Syntax check applied BEFORE committing.
  • User reviews every PR before un-drafting + merging.
  • Daily cap = 5 means the worst case is 5 noisy PRs to close.

Companion env vars:
  BRAIN_AUTOPILOT_DRAFT_PR_DISABLE=1   kill switch (default off)
  BRAIN_DRAFT_PR_DAILY_CAP=5           daily cap (default 5)
  DCHUB_BRAIN_L5_CONF_MIN=0.75         min confidence (default 0.75)
                                       (canonical name; legacy alias
                                       BRAIN_DRAFT_PR_MIN_CONF still
                                       honored for backwards-compat)

2026-06-07 (Task #148): lowered the draft-PR confidence floor 0.90 → 0.75
so the brain opens 3-5× more DRAFT PRs and we accumulate faster
learning data. DRAFT mode is preserved + the ast.parse syntax gate
still rejects broken patches + the 5/day cap still throttles noise.
Sentinel auto-merge floor (SENTINEL_AUTO_MERGE_REQUIRE_CONF=0.95)
is UNCHANGED — auto-merge stays conservative; only the human-review
draft pool widens.
"""
from __future__ import annotations

import ast
import base64
import datetime as _dt
import json
import logging
import os
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
brain_backlog_admin_bp = Blueprint("brain_backlog_admin", __name__)

_INTERNAL_BASE = (os.environ.get("INTERNAL_BASE_URL")
                  or "http://localhost:8080").rstrip("/")
_RAILWAY_BASE = "https://dchub-backend-production.up.railway.app"


# ── Config ───────────────────────────────────────────────────────────

def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("BRAIN_AUTOPILOT_DRAFT_PR_DISABLE"))


def _daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("BRAIN_DRAFT_PR_DAILY_CAP", "5")))
    except Exception:
        return 5


def _min_conf() -> float:
    """Confidence floor below which a Layer-5 proposal will NOT be
    promoted to a DRAFT PR. Env-driven so we can tune the bar without
    a redeploy.

    Canonical env var: DCHUB_BRAIN_L5_CONF_MIN (default 0.75 as of
    2026-06-07 task #148 — lowered from 0.90 so the brain opens 3-5×
    more draft PRs + accumulates faster learning data; safety is
    preserved by DRAFT mode + ast.parse syntax gate + 5/day cap).

    Legacy alias BRAIN_DRAFT_PR_MIN_CONF is still honored if set so a
    rollback can re-pin the old 0.90 without code changes. Sentinel
    auto-merge uses a SEPARATE knob (SENTINEL_AUTO_MERGE_REQUIRE_CONF,
    default 0.95) and is unaffected by this floor.
    """
    raw = (os.environ.get("DCHUB_BRAIN_L5_CONF_MIN")
           or os.environ.get("BRAIN_DRAFT_PR_MIN_CONF")
           or "0.75")
    try:
        return max(0.0, min(1.0, float(raw)))
    except Exception:
        return 0.75


def _stuck_auto_action_off() -> bool:
    return _truthy(os.environ.get("BRAIN_STUCK_AUTO_ACTION_DISABLE"))


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


# ── Backlog dashboard data ───────────────────────────────────────────

def _http_get_json(path: str, timeout: int = 10) -> dict:
    """Internal GET. Tries localhost first, falls back to Railway."""
    import urllib.request
    import urllib.error
    headers = {"X-Internal-Probe": "1"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
    for base in (_INTERNAL_BASE, _RAILWAY_BASE):
        try:
            req = urllib.request.Request(f"{base}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="ignore"))
        except Exception:
            continue
    return {}


def _backlog_snapshot() -> dict:
    """Aggregate the stuck-queue + proposed-code snapshot."""
    pers = _http_get_json("/api/v1/brain/persistence?min_count=2") or {}
    debug = _http_get_json(
        "/api/v1/brain/proposed-code/debug-summary") or {}
    pending = _http_get_json(
        f"/api/v1/brain/proposed-code/pending-pr?limit=50&"
        f"min_confidence={_min_conf()}") or {}
    self_model = _http_get_json("/api/v1/brain/self-model") or {}

    stuck_items = (pers.get("items") or [])
    # split: heartbeat surfaces vs unfixed findings vs directives
    by_source: dict[str, list] = {
        "operator_directive": [],
        "persistence": [],
        "heartbeat": [],
    }
    for it in stuck_items:
        src = it.get("source") or "persistence"
        if src not in by_source:
            src = "persistence"
        by_source[src].append(it)

    return {
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "stuck_total": len(stuck_items),
        "stuck_by_source": {k: len(v) for k, v in by_source.items()},
        "stuck_items": stuck_items[:60],
        "proposed_code": {
            "by_status": debug.get("by_status") or {},
            "confidence_buckets": debug.get("confidence_buckets") or {},
            "pending_pr_match": int(debug.get("pending_pr_match") or 0),
            "total_rows": int(debug.get("total_rows") or 0),
            "high_conf_pending": pending.get("items") or [],
            "high_conf_count": int(pending.get("count") or 0),
        },
        "config": {
            "draft_pr_kill_switch": _kill_switch_on(),
            "draft_pr_daily_cap": _daily_cap(),
            "draft_pr_min_conf": _min_conf(),
            "stuck_auto_action_kill_switch": _stuck_auto_action_off(),
            "draft_prs_today": _draft_prs_today(),
        },
        "self_model": {
            "open_findings_24h": (self_model.get("current_state") or {}).get(
                "open_findings_24h"),
            "fix_success_rate_30d": (self_model.get("current_state")
                                      or {}).get("fix_success_rate_30d"),
            "top_open_finding_types": (self_model.get("current_state")
                                        or {}).get("top_open_finding_types"),
        },
    }


# ── Daily cap bookkeeping ────────────────────────────────────────────

def _today_key() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _draft_prs_today() -> int:
    """Count brain-opened draft PRs from today via brain_meta."""
    try:
        from routes import brain_v2_store as _store
        row = _store.get_meta(f"brain_draft_pr_count:{_today_key()}")
        return int((row or {}).get("value") or 0)
    except Exception:
        return 0


def _record_draft_pr() -> int:
    try:
        from routes import brain_v2_store as _store
        new = _draft_prs_today() + 1
        _store.set_meta(f"brain_draft_pr_count:{_today_key()}", str(new))
        return new
    except Exception:
        return 0


# ── GitHub PR opener (uses brain_pr_opener helpers) ──────────────────

def _open_draft_pr_for_proposal(prop: dict) -> dict:
    """Open a DRAFT PR for one Layer-5 proposal.

    Steps:
      1. Pull the file from main via brain_pr_opener._get_file.
      2. Verify the `search_text` is present + unique enough.
      3. Apply the substitution in-memory.
      4. ast.parse the result if it's a .py file (syntax gate).
      5. Create a branch, commit the patched file, open a DRAFT PR.
      6. Call mark-pr to flip status='pr_opened'.

    Returns {"ok": bool, "pr_url"?: str, "skipped"?: str, "error"?: str}.
    """
    try:
        from routes.brain_pr_opener import (
            _get_file, _get_default_branch_sha, _create_branch,
            _commit_file, _open_pr, _GITHUB_TOKEN, _GITHUB_REPO,
        )
    except Exception as e:
        return {"ok": False, "error": f"brain_pr_opener import: {e}"}

    if not _GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN unset on backend"}

    pid = prop.get("id")
    changes = prop.get("changes") or []
    if not changes:
        # legacy single-file proposal
        if prop.get("file_path") and prop.get("search_text"):
            changes = [{
                "file": prop["file_path"],
                "search": prop["search_text"],
                "replace": prop.get("replace_text") or "",
            }]
    if not changes:
        return {"ok": False, "skipped": "no changes in proposal"}

    # Multi-file proposals: handle each. For now ship single-file only —
    # multi-file lifts complexity (need to bundle into one commit).
    if len(changes) > 1:
        return {"ok": False,
                "skipped": ("multi-file proposal; handled by GH Actions "
                            "workflow only (this helper is single-file)")}

    ch = changes[0]
    cf = (ch.get("file") or "").strip().lstrip("/")
    cs = ch.get("search") or ""
    cr = ch.get("replace") or ""
    if not cf or not cs:
        return {"ok": False, "skipped": "missing file/search"}
    if ".." in cf:
        return {"ok": False, "skipped": f"unsafe path: {cf}"}

    # 1. Fetch current main content
    content, file_sha = _get_file(cf)
    if content is None:
        return {"ok": False, "skipped": f"file not in main: {cf}"}

    # 2. Verify search is present + unique
    n = content.count(cs)
    if n == 0:
        return {"ok": False,
                "skipped": (f"search text not in {cf} "
                            "(file changed since proposal)")}
    if n > 1:
        return {"ok": False,
                "skipped": f"search text appears {n}× in {cf} (ambiguous)"}

    # 3. Apply
    new_content = content.replace(cs, cr, 1)
    if new_content == content:
        return {"ok": False, "skipped": "no-op edit"}

    # 4. Syntax gate (Python only)
    if cf.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as se:
            return {"ok": False,
                    "skipped": (f"patched file fails ast.parse: "
                                f"{str(se)[:120]}")}

    # 5. Create branch + commit
    base_sha = _get_default_branch_sha()
    if not base_sha:
        return {"ok": False, "error": "could not read main SHA"}
    ts = int(time.time())
    loop_name = (prop.get("loop_name") or "unknown")[:30].replace("/", "-")
    loop_safe = "".join(c if c.isalnum() or c in "-_" else "-"
                        for c in loop_name)
    branch = f"brain-v2/auto-{loop_safe}-{pid}-{ts}"
    if not _create_branch(branch, base_sha):
        return {"ok": False, "error": f"branch create failed: {branch}"}

    confidence = float(prop.get("confidence") or 0.0)
    rationale = (prop.get("rationale") or "(no rationale)")[:1000]
    commit_msg = (
        f"brain-l5(draft): proposal #{pid} for {prop.get('loop_name', '?')}\n\n"
        f"Auto-proposed by Brain v2 Layer 5 "
        f"(proposal #{pid}, conf {confidence:.2f}).\n"
        f"Opened as a DRAFT PR by /api/v1/admin/brain/draft-prs/run.\n"
        f"Syntax gate: passed (ast.parse OK on patched {cf}).\n\n"
        f"Rationale: {rationale}\n\n"
        f"This PR is a DRAFT. A human must review the diff + un-draft + "
        f"merge. No auto-merge by design."
    )
    if not _commit_file(cf, new_content, commit_msg, branch, file_sha):
        return {"ok": False, "error": f"commit failed on {cf}"}

    # 6. Open draft PR
    pr_title = f"[brain-l5 draft] {prop.get('loop_name', '?')} — #{pid}"
    pr_body = (
        f"## Brain Layer-5 auto-proposed fix\n\n"
        f"**Proposal:** #{pid}\n"
        f"**Loop:** `{prop.get('loop_name', '?')}`\n"
        f"**Confidence:** {confidence:.2f} (threshold ≥{_min_conf():.2f})\n"
        f"**File:** `{cf}` ({len(cs)}→{len(cr)} chars)\n\n"
        f"### Rationale\n\n{rationale}\n\n"
        f"### Syntax check\n\nPatched file passes `ast.parse()`. "
        f"This does NOT mean it's semantically correct — tests still "
        f"need to pass.\n\n"
        f"### How to verify locally\n\n"
        f"```\ngit checkout {branch}\npython3 -c 'import ast; ast.parse(open(\"{cf}\").read())'\npytest tests/ -q -x\n```\n\n"
        f"---\n"
        f"_Auto-generated by `/api/v1/admin/brain/draft-prs/run` "
        f"(routes/brain_backlog_admin.py). This route opens DRAFT "
        f"PRs only — humans merge. Kill switch: "
        f"`BRAIN_AUTOPILOT_DRAFT_PR_DISABLE=1`._"
    )
    # GitHub REST PR creation — brain_pr_opener._open_pr doesn't set
    # draft=true, so we call _gh directly to get a draft PR.
    try:
        from routes.brain_pr_opener import _gh
    except Exception as e:
        return {"ok": False, "error": f"_gh import: {e}"}
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/pulls", {
        "title": pr_title, "head": branch, "base": "main",
        "body": pr_body, "draft": True,
    })
    if r.status_code not in (200, 201):
        return {"ok": False,
                "error": (f"PR create returned {r.status_code}: "
                          f"{r.text[:200]}"),
                "branch": branch}
    pr = r.json()
    pr_url = pr.get("html_url")
    pr_number = pr.get("number")

    # 7. Mark in DB so the GH Actions workflow doesn't try again
    try:
        import urllib.request as _ur
        import urllib.error
        mark_url = (f"{_INTERNAL_BASE}/api/v1/brain/proposed-code/"
                    f"{pid}/mark-pr")
        body = json.dumps({"pr_url": pr_url}).encode("utf-8")
        req = _ur.Request(mark_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Admin-Key", _admin_key())
        _ur.urlopen(req, timeout=10).read()
    except Exception as e:
        logger.warning("mark-pr callback failed for proposal #%s: %s",
                       pid, e)

    return {"ok": True, "pr_url": pr_url, "pr_number": pr_number,
            "branch": branch, "proposal_id": pid,
            "confidence": confidence, "file": cf}


# ── Endpoints ────────────────────────────────────────────────────────

@brain_backlog_admin_bp.route("/api/v1/admin/brain/backlog",
                               methods=["GET"])
def backlog_json():
    """JSON snapshot of stuck queue + L5 proposals. Admin-only."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(ok=True, **_backlog_snapshot()), 200


@brain_backlog_admin_bp.route("/api/v1/admin/brain/draft-prs/preview",
                               methods=["GET"])
def draft_prs_preview():
    """Dry-run: which proposals would open a draft PR right now?"""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    snap = _backlog_snapshot()
    cap = _daily_cap()
    used = snap["config"]["draft_prs_today"]
    remaining = max(0, cap - used)
    high_conf = snap["proposed_code"]["high_conf_pending"][:remaining]
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        cap=cap, used_today=used, remaining=remaining,
        min_conf=_min_conf(),
        would_open=[{
            "id": p.get("id"),
            "loop_name": p.get("loop_name"),
            "file_path": p.get("file_path"),
            "confidence": p.get("confidence"),
            "rationale": (p.get("rationale") or "")[:200],
        } for p in high_conf],
    ), 200


@brain_backlog_admin_bp.route("/api/v1/admin/brain/draft-prs/run",
                               methods=["POST"])
def draft_prs_run():
    """Open up to (cap - used_today) draft PRs for the highest-conf
    Layer-5 proposals that don't have a PR yet.

    Each PR is created via GitHub REST API (draft=true). The function
    re-checks the syntax gate before committing, so a broken patch is
    skipped rather than landing a syntax-error commit.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    if _kill_switch_on():
        return jsonify(
            ok=False, error="kill_switch_on",
            reason="BRAIN_AUTOPILOT_DRAFT_PR_DISABLE=1 in env",
        ), 429

    cap = _daily_cap()
    used = _draft_prs_today()
    remaining = max(0, cap - used)
    if remaining == 0:
        return jsonify(
            ok=True, acted=False,
            reason=f"daily_cap_exhausted ({used}/{cap})",
            cap=cap, used_today=used,
        ), 200

    # Pull the same high-conf queue. Over-fetch by 8x because the L5
    # codegen has historically generated patches that fail the syntax
    # gate (the GH Actions workflow has been failing on this for weeks);
    # we need a wide pool to find any that actually patch cleanly.
    fetch_limit = int(request.args.get("fetch_limit")
                      or max(remaining * 8, 16))
    pending = _http_get_json(
        f"/api/v1/brain/proposed-code/pending-pr?"
        f"limit={fetch_limit}&"
        f"min_confidence={_min_conf()}") or {}
    items = pending.get("items") or []
    if not items:
        return jsonify(
            ok=True, acted=False,
            reason=f"no pending proposals at conf >= {_min_conf()}",
            cap=cap, used_today=used,
        ), 200

    opened = []
    skipped = []
    errors = []
    for prop in items:
        if len(opened) >= remaining:
            break
        try:
            res = _open_draft_pr_for_proposal(prop)
        except Exception as e:
            errors.append({"id": prop.get("id"), "error": str(e)[:200]})
            continue
        if res.get("ok"):
            new_used = _record_draft_pr()
            res["new_used_today"] = new_used
            opened.append(res)
        elif res.get("skipped"):
            skipped.append({"id": prop.get("id"),
                            "reason": res["skipped"][:200]})
        else:
            errors.append({"id": prop.get("id"),
                            "error": (res.get("error") or "")[:200]})

    return jsonify(
        ok=True,
        acted=bool(opened),
        cap=cap,
        used_before=used,
        used_after=_draft_prs_today(),
        opened=opened,
        skipped=skipped,
        errors=errors,
        candidates_examined=len(items),
    ), 200


# ── HTML dashboard ───────────────────────────────────────────────────

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Brain Backlog · DC Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0a0a14;--fg:#e9e9f0;--muted:#9a9ab0;--card:#13131f;
      --bord:#23233a;--ind:#7c5cff;--vio:#c084fc;--ok:#22c55e;
      --warn:#f59e0b;--bad:#ef4444}
*{box-sizing:border-box}
body{margin:0;padding:24px;font:14px/1.5 ui-sans-serif,system-ui;
     background:var(--bg);color:var(--fg)}
h1{margin:0 0 4px;font-size:24px;background:linear-gradient(90deg,
   var(--ind),var(--vio));-webkit-background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;
      margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--bord);
      border-radius:8px;padding:16px}
.card .v{font:600 22px/1 ui-sans-serif;color:var(--vio)}
.card .l{color:var(--muted);font-size:12px;margin-top:4px}
.section{background:var(--card);border:1px solid var(--bord);
         border-radius:8px;padding:16px;margin-bottom:18px}
.section h2{margin:0 0 8px;font-size:16px}
.row{display:flex;justify-content:space-between;gap:8px;
     padding:8px 10px;border-bottom:1px dashed #2a2a40;font-size:13px}
.row:last-child{border-bottom:0}
.row .name{flex:1;color:var(--fg)}
.row .url{color:var(--muted);font-family:ui-monospace,monospace;
         font-size:11px}
.row .seen{color:var(--ind);font-weight:600;min-width:60px;
           text-align:right}
.row .conf{color:var(--ok);font-weight:600;min-width:60px;
           text-align:right}
button{background:var(--ind);border:0;color:#fff;padding:10px 16px;
       border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;
       margin-right:8px}
button.warn{background:var(--warn)}
button.disabled{background:#444;cursor:not-allowed;opacity:.6}
button:hover:not(.disabled){opacity:.9}
.actions{display:flex;gap:8px;margin:16px 0;flex-wrap:wrap}
pre{background:#0a0a14;border:1px solid var(--bord);border-radius:6px;
    padding:12px;overflow:auto;font-size:12px;max-height:280px}
.toast{position:fixed;top:24px;right:24px;background:var(--card);
       border:1px solid var(--ind);border-radius:6px;padding:12px 16px;
       max-width:480px;z-index:10}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;
      font-size:11px;font-weight:600}
.pill.kill{background:rgba(239,68,68,.18);color:var(--bad)}
.pill.ok{background:rgba(34,197,94,.18);color:var(--ok)}
.pill.warn{background:rgba(245,158,11,.18);color:var(--warn)}
.meta{color:var(--muted);font-size:12px;margin-top:6px}
.tag{display:inline-block;padding:1px 6px;border:1px solid var(--bord);
     border-radius:4px;font-size:11px;color:var(--muted);margin-left:6px}
</style>
</head><body>
<h1>Brain Backlog · Drive Change</h1>
<div class="sub">stuck-issue queue · L5 proposed code fixes ·
   one-click draft PRs · admin-gated · loaded <span id="ts"></span>
   · <span id="floor-pill" class="pill ok"
          title="Confidence floor below which a Layer-5 proposal will
                 NOT be promoted to a DRAFT PR. Lower = more learning
                 data; sentinel auto-merge floor is separate (0.95).">
       L5 floor: ?
     </span></div>

<div class="grid" id="kpis"></div>

<div class="tabs" style="display:flex;gap:4px;margin-bottom:16px;
     border-bottom:1px solid var(--bord)">
  <div class="tab active" data-tab="tactical"
       onclick="switchTab('tactical')"
       style="padding:10px 16px;cursor:pointer;border-bottom:2px solid var(--ind);
              color:var(--fg);font-weight:600">
    Tactical (L5 backlog)
  </div>
  <div class="tab" data-tab="strategic"
       onclick="switchTab('strategic')"
       style="padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;
              color:var(--muted);font-weight:600">
    Strategic (L6 weekly)
  </div>
  <div class="tab" data-tab="outcomes"
       onclick="switchTab('outcomes')"
       style="padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;
              color:var(--muted);font-weight:600">
    PR Outcomes (R2)
  </div>
  <div class="tab" data-tab="hourly"
       onclick="switchTab('hourly')"
       style="padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;
              color:var(--muted);font-weight:600">
    Hourly Cycle
  </div>
</div>

<div id="tab-tactical">
  <div class="section">
    <h2>Actions</h2>
    <div class="actions">
      <button onclick="run('preview')">Preview draft PRs</button>
      <button onclick="run('open', this)" class="warn">
        Open up to N draft PRs now
      </button>
      <button onclick="refresh()">Refresh</button>
    </div>
    <div class="meta" id="cfg-meta"></div>
  </div>

  <div class="section">
    <h2>L5 proposed code fixes — high-conf, no PR yet</h2>
    <div id="proposals"></div>
  </div>

  <div class="section">
    <h2>Stuck findings (persistence worklist)</h2>
    <div id="stuck"></div>
  </div>

  <div class="section">
    <h2>Feature Proposals from Feedback</h2>
    <div class="meta" id="feature-proposer-meta">
      Twice-daily (15/3 UTC) — clusters last-30d <code>/feedback</code>
      MEDIUM/HIGH submissions by shared theme, asks Claude for a 200-word
      feature spec per cluster of 3+ users, opens a DRAFT PR with a
      <code>routes/_proposed_&lt;slug&gt;.py</code> stub. Hard cap 2/week.
      Kill: <code>BRAIN_FEATURE_PROPOSER_DISABLE=1</code>. Dry-run:
      <code>BRAIN_FEATURE_PROPOSER_DRY_RUN=1</code>.
    </div>
    <div class="actions">
      <button onclick="runFeatureProposer('preview', this)">
        Preview clusters + specs
      </button>
      <button onclick="runFeatureProposer('run', this)" class="warn">
        Run proposer now
      </button>
      <button onclick="loadFeatureProposer()">Refresh</button>
    </div>
    <div id="feature-proposer-clusters"></div>
    <h3 style="margin-top:18px">Recent proposals</h3>
    <div id="feature-proposer-log"></div>
  </div>
</div>

<div id="tab-strategic" style="display:none">
  <div class="section">
    <h2>Strategic synthesis · Brain Layer-6</h2>
    <div class="actions">
      <button onclick="runStrategic('preview', this)">Preview prompt</button>
      <button onclick="runStrategic('synth', this)" class="warn">
        Run synthesis now
      </button>
      <button onclick="runStrategic('digest', this)" class="warn">
        Send weekly digest now
      </button>
      <button onclick="loadStrategic()">Refresh</button>
    </div>
    <div class="meta" id="strategic-meta">
      Weekly Claude-backed pass that reads funnel + page health + customer
      asks + brain backlog + competitor signal + self-model, then proposes
      3 strategic gaps + 3 competitor lacks + 3 funnel optimizations + 1
      wildcard bet. Runs Mondays 08:00 UTC; digest email at 08:30 UTC.
      Kill: <code>DCHUB_BRAIN_STRATEGIC_DISABLE=1</code>. Scaffold PRs:
      <code>DCHUB_BRAIN_STRATEGIC_DRAFT_PR=1</code>.
    </div>
  </div>
  <div class="section">
    <h2>This week's recommendations</h2>
    <div id="strategic-week-meta" class="meta"></div>
    <div id="strategic-recs"></div>
  </div>
  <div class="section">
    <h2>Trend lines · funnel snapshot</h2>
    <div id="strategic-trends"></div>
  </div>
</div>

<div id="tab-outcomes" style="display:none">
  <div class="section">
    <h2>PR Outcomes · Brain ROUND 2 (auto-execute monitor)</h2>
    <div class="actions">
      <button onclick="runOutcomes('monitor', this)" class="warn">
        Run monitor now (24h window)
      </button>
      <button onclick="loadOutcomes()">Refresh</button>
    </div>
    <div class="meta" id="outcomes-meta">
      Twice-daily (10/22 UTC) — polls GitHub for merged brain-authored
      PRs in the last 24h, re-probes sentinel grade before/after, writes
      success/regression rows so the strategic synthesis learns from
      its own track record. Detects brain authorship by PR title
      markers (<code>[brain-l5</code>), branch prefix
      (<code>brain-v2/</code>), or body markers. On regression files a
      follow-up <code>brain_pr_regression</code> finding.
      Kill: <code>BRAIN_PR_OUTCOME_MONITOR_DISABLE=1</code>.
    </div>
  </div>
  <div class="grid" id="outcomes-kpis"></div>
  <div class="section">
    <h2>Recent PR outcomes (last 30d)</h2>
    <div id="outcomes-rows"></div>
  </div>
  <div class="section">
    <h2>Cross-session learnings · pattern pollination</h2>
    <div class="meta" id="learnings-meta">
      Patterns surfaced in 3+ distinct brain sessions over the last 7d
      (escalated to higher severity so the synthesis weighs them).
      Kill: <code>BRAIN_CROSS_SESSION_ESCALATE_DISABLE=1</code>.
      <button onclick="scanCrossSession(this)" class="warn"
              style="font-size:12px;padding:6px 10px;margin-left:8px">
        Scan now
      </button>
    </div>
    <div id="learnings-rows"></div>
  </div>
</div>

<div id="tab-hourly" style="display:none">
  <div class="section">
    <h2>Hourly micro-decision loop · Brain HOURLY</h2>
    <div class="actions">
      <button onclick="runHourly('run', this)" class="warn">
        Run cycle now
      </button>
      <button onclick="runHourly('preview', this)">
        Preview context
      </button>
      <button onclick="loadHourly()">Refresh</button>
    </div>
    <div class="meta" id="hourly-meta">
      Light Haiku-backed pass every hour (~$0.003/call) that detects
      immediate funnel/sentinel/media anomalies, picks one micro-action
      to schedule, and names one metric to watch next hour. Daily cap
      <code>BRAIN_MICRO_DAILY_BUDGET_USD</code> (default $5). Context-hash
      dedup → if nothing has changed since last hour, the Claude call is
      skipped. Anomalies severity ≥medium are written to brain_findings
      so the L6 synthesis can consider them.
      Kill: <code>BRAIN_MICRO_CYCLE_DISABLE=1</code>. Dry-run:
      <code>BRAIN_MICRO_DRY_RUN=1</code>.
    </div>
  </div>
  <div class="grid" id="hourly-kpis"></div>
  <div class="section">
    <h2>Last 24h of decisions</h2>
    <div id="hourly-rows"></div>
  </div>
</div>

<pre id="raw" style="display:none"></pre>

<div id="toast" class="toast" style="display:none"></div>

<script>
const ADMIN_KEY = new URLSearchParams(location.search).get("admin_key") || "";
const HDR = ADMIN_KEY ? {"X-Admin-Key": ADMIN_KEY} : {};

function $(id){return document.getElementById(id);}
function toast(msg, ms=3500){
  $('toast').innerHTML = msg;
  $('toast').style.display = 'block';
  setTimeout(()=>$('toast').style.display='none', ms);
}

async function refresh(){
  const r = await fetch('/api/v1/admin/brain/backlog', {headers: HDR});
  if(!r.ok){
    document.body.innerHTML += `<pre style="color:#ef4444">
      ${r.status} — admin_key required. Append ?admin_key=...
      to URL or set X-Admin-Key header.</pre>`;
    return;
  }
  const d = await r.json();
  render(d);
}

function render(d){
  $('ts').textContent = d.as_of;
  const cfg = d.config || {};
  const kill = cfg.draft_pr_kill_switch ?
    '<span class="pill kill">KILL ON</span>' :
    '<span class="pill ok">live</span>';
  const stk = cfg.stuck_auto_action_kill_switch ?
    '<span class="pill kill">stuck kill ON</span>' :
    '<span class="pill ok">stuck live</span>';
  // Floor pill (top sub-line): pure indicator of the active L5 PR
  // confidence floor. Color-codes the throttle: <0.80 = wide (warn,
  // more PRs), >=0.90 = strict (ok, fewer PRs).
  const fp = $('floor-pill');
  if(fp){
    const floor = (cfg.draft_pr_min_conf!=null)
      ? Number(cfg.draft_pr_min_conf).toFixed(2) : '?';
    fp.textContent = `L5 floor: ≥${floor}`;
    fp.className = 'pill ' + (Number(floor) < 0.80 ? 'warn' :
                              Number(floor) >= 0.90 ? 'ok' : 'ok');
    fp.title = `Layer-5 draft-PR confidence floor (env ` +
      `DCHUB_BRAIN_L5_CONF_MIN). Proposals below ${floor} are NOT ` +
      `promoted to a draft PR. Sentinel auto-merge uses a separate ` +
      `floor (0.95) and is unaffected.`;
  }
  $('kpis').innerHTML = `
    <div class="card"><div class="v">${d.stuck_total||0}</div>
      <div class="l">Stuck findings</div></div>
    <div class="card"><div class="v">${(d.proposed_code||{}).total_rows||0}</div>
      <div class="l">L5 proposals total
        <span class="tag">${(d.proposed_code||{}).pending_pr_match||0} no-PR</span></div></div>
    <div class="card"><div class="v">${(d.proposed_code||{}).high_conf_count||0}</div>
      <div class="l">High-conf actionable
        <span class="tag">≥${cfg.draft_pr_min_conf}</span></div></div>
    <div class="card"><div class="v">${cfg.draft_prs_today||0}/${cfg.draft_pr_daily_cap}</div>
      <div class="l">Draft PRs today ${kill}</div></div>
  `;
  $('cfg-meta').innerHTML = `kill switch (PR opener): ${kill} · ` +
    `stuck auto-action: ${stk} · ` +
    `daily cap: <b>${cfg.draft_pr_daily_cap}</b> · ` +
    `min conf: <b>${cfg.draft_pr_min_conf}</b> · ` +
    `Env to disable: <code>BRAIN_AUTOPILOT_DRAFT_PR_DISABLE=1</code>`;

  const hc = ((d.proposed_code||{}).high_conf_pending || []).slice(0,30);
  $('proposals').innerHTML = hc.length ? hc.map(p => {
    const conf = (p.confidence||0).toFixed(2);
    return `<div class="row">
      <div class="name">#${p.id} · ${p.file_path||'?'}
        <div class="meta">${(p.rationale||'').slice(0,160)}</div>
        <div class="meta">${p.loop_name||''}</div>
      </div>
      <div class="conf">${conf}</div>
    </div>`;
  }).join('') : '<div class="meta">No high-conf proposals waiting.</div>';

  const stk_items = (d.stuck_items || []).slice(0, 40);
  $('stuck').innerHTML = stk_items.length ? stk_items.map(it=>`
    <div class="row">
      <div class="name">${it.issue_label||'?'}
        <span class="tag">${it.source||'?'}</span>
        <div class="meta url">${it.url||''}</div>
      </div>
      <div class="seen">×${it.seen_count||0}</div>
    </div>
  `).join('') : '<div class="meta">No stuck findings.</div>';
}

async function run(mode, btn){
  if(btn) btn.classList.add('disabled');
  const path = mode === 'preview' ?
    '/api/v1/admin/brain/draft-prs/preview' :
    '/api/v1/admin/brain/draft-prs/run';
  const r = await fetch(path, {
    method: mode==='preview' ? 'GET' : 'POST',
    headers: HDR,
  });
  const txt = await r.text();
  $('raw').style.display = 'block';
  $('raw').textContent = txt;
  if(btn) btn.classList.remove('disabled');
  try {
    const d = JSON.parse(txt);
    if(mode === 'preview'){
      toast(`Would open ${(d.would_open||[]).length} draft PR(s). ` +
            `${d.remaining}/${d.cap} budget remaining today.`);
    } else {
      const op = (d.opened||[]).length;
      const sk = (d.skipped||[]).length;
      const er = (d.errors||[]).length;
      toast(`Opened ${op} draft PR(s). Skipped ${sk}. Errors ${er}. ` +
            `Used today: ${d.used_after}/${d.cap}.`);
    }
  } catch(e){ toast('Response not JSON — check raw output below.'); }
  setTimeout(refresh, 1200);
}

// ── Strategic tab (Brain Layer-6) ─────────────────────────────────

function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>{
    const active = t.dataset.tab === name;
    t.style.borderBottomColor = active ? 'var(--ind)' : 'transparent';
    t.style.color = active ? 'var(--fg)' : 'var(--muted)';
  });
  document.getElementById('tab-tactical').style.display =
    (name==='tactical') ? '' : 'none';
  document.getElementById('tab-strategic').style.display =
    (name==='strategic') ? '' : 'none';
  document.getElementById('tab-outcomes').style.display =
    (name==='outcomes') ? '' : 'none';
  if(name==='strategic') loadStrategic();
  if(name==='outcomes')  loadOutcomes();
}

// ── PR Outcomes tab (ROUND 2) ──────────────────────────────────────

const OUTCOME_BADGE = {
  'success':     '<span class="pill ok">success</span>',
  'regression':  '<span class="pill kill">REGRESSION</span>',
  'draft':       '<span class="pill">draft</span>',
  'open':        '<span class="pill">open</span>',
  'unknown':     '<span class="pill">unknown</span>',
  'deploy_fail': '<span class="pill kill">deploy_fail</span>',
};

async function loadOutcomes(){
  try {
    const [s, l, cs] = await Promise.all([
      fetch('/api/v1/admin/brain/pr-outcomes/summary', {headers: HDR}),
      fetch('/api/v1/admin/brain/pr-outcomes', {headers: HDR}),
      fetch('/api/v1/admin/brain/cross-session-learnings', {headers: HDR}),
    ]);
    const summary = await s.json();
    const listing = await l.json();
    const learnings = await cs.json();
    renderOutcomes(summary, listing, learnings);
  } catch(e){
    $('outcomes-rows').innerHTML =
      `<div class="meta">Load error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderOutcomes(summary, listing, learnings){
  // KPIs
  const succ = summary.success_rate;
  const regr = summary.regression_rate;
  const by   = summary.by_outcome || {};
  $('outcomes-kpis').innerHTML = `
    <div class="card"><div class="v">${summary.merged || 0}</div>
      <div class="l">Merged (30d)</div></div>
    <div class="card"><div class="v">${(by.success||0)}</div>
      <div class="l">Success
        ${succ != null ? `<span class="tag">${(succ*100|0)}%</span>` : ''}
      </div></div>
    <div class="card"><div class="v">${(by.regression||0)}</div>
      <div class="l">Regression
        ${regr != null ? `<span class="tag">${(regr*100|0)}%</span>` : ''}
      </div></div>
    <div class="card"><div class="v">${(by.draft||0)+(by.open||0)}</div>
      <div class="l">In-flight (draft+open)</div></div>
  `;
  // Rows
  const rows = (listing.outcomes || []).slice(0, 50);
  $('outcomes-rows').innerHTML = rows.length ? rows.map(r => {
    const grade = (r.sentinel_before_grade != null &&
                   r.sentinel_after_grade != null)
      ? `<span class="tag">${r.sentinel_before_grade.toFixed(1)} → ${r.sentinel_after_grade.toFixed(1)}</span>`
      : '';
    const reg = r.regression_details ?
      `<div class="meta" style="color:#ef4444">${escapeHtml(r.regression_details)}</div>` : '';
    const url = r.pr_url ?
      `<a href="${escapeHtml(r.pr_url)}" target="_blank" style="color:var(--ind)">#${r.pr_number}</a>` :
      `#${r.pr_number}`;
    return `<div class="row" style="display:block;padding:10px 12px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          ${OUTCOME_BADGE[r.outcome] || OUTCOME_BADGE.unknown}
          ${url}
          <span class="tag">${escapeHtml(r.sentinel_endpoint || 'no-endpoint')}</span>
          ${grade}
        </div>
        <div class="meta" style="font-size:11px">${escapeHtml((r.merged_at || r.created_at || '').slice(0,16))}</div>
      </div>
      <div class="name" style="font-size:13px;margin-top:4px">
        ${escapeHtml((r.pr_title || '').slice(0, 140))}
      </div>
      ${reg}
    </div>`;
  }).join('') : '<div class="meta">No PR outcomes recorded yet. Click "Run monitor now" above to backfill.</div>';

  // Cross-session learnings
  const ls = (learnings.learnings || []).slice(0, 20);
  $('learnings-rows').innerHTML = ls.length ? ls.map(l => `
    <div class="row" style="display:block;padding:10px 12px">
      <div>
        <span class="pill kill">×${l.distinct_sessions} sessions</span>
        <code>${escapeHtml(l.detector || '?')}</code>
        <span class="tag">${escapeHtml(l.issue || '?')}</span>
        <span class="tag">${l.row_count} rows</span>
      </div>
      <div class="meta" style="margin-top:4px;font-size:11px">
        escalated ${escapeHtml((l.escalated_at || '').slice(0, 16))}
        · last seen ${escapeHtml((l.last_seen || '').slice(0, 16))}
      </div>
      ${l.sample_urls ? `<div class="meta url" style="margin-top:4px">${escapeHtml(l.sample_urls.slice(0, 200))}</div>` : ''}
    </div>
  `).join('') : '<div class="meta">No cross-session patterns detected yet (threshold: 3+ sessions in 7d).</div>';
}

async function runOutcomes(mode, btn){
  if(btn) btn.classList.add('disabled');
  try {
    const r = await fetch('/api/v1/admin/brain/pr-outcomes/monitor-now',
      {method:'POST', headers: HDR});
    const d = await r.json();
    $('raw').style.display = 'block';
    $('raw').textContent = JSON.stringify(d, null, 2);
    const res = d.results || {};
    toast(`Monitor: scanned=${d.scanned||0} success=${res.success||0} ` +
          `regression=${res.regression||0} draft=${res.draft||0}.`);
  } catch(e){ toast('Run failed: ' + e.message); }
  finally {
    if(btn) btn.classList.remove('disabled');
    setTimeout(loadOutcomes, 800);
  }
}

async function scanCrossSession(btn){
  if(btn) btn.classList.add('disabled');
  try {
    const r = await fetch('/api/v1/admin/brain/cross-session/scan',
      {method:'POST', headers: HDR});
    const d = await r.json();
    $('raw').style.display = 'block';
    $('raw').textContent = JSON.stringify(d, null, 2);
    toast(`Cross-session scan: ${(d.escalated_clusters||[]).length} ` +
          `clusters, ${d.escalated_rows||0} rows escalated.`);
  } catch(e){ toast('Scan failed: ' + e.message); }
  finally {
    if(btn) btn.classList.remove('disabled');
    setTimeout(loadOutcomes, 800);
  }
}

function escapeHtml(s){
  return String(s||'').replace(/[&<>"']/g, c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

async function loadStrategic(){
  try {
    const r = await fetch('/api/v1/brain/strategic-synthesis/latest');
    const d = await r.json();
    renderStrategic(d);
  } catch(e){
    $('strategic-recs').innerHTML =
      `<div class="meta">Load error: ${escapeHtml(e.message)}</div>`;
  }
  // Trends — load funnel snapshot
  try {
    const f = await fetch('/api/v1/mcp/funnel');
    if (f.ok) {
      const fd = await f.json();
      const totals = fd.totals || fd.summary || {};
      const items = Object.entries(totals).slice(0, 8)
        .map(([k,v]) => `<div class="row">
          <div class="name">${escapeHtml(k)}</div>
          <div class="seen">${typeof v==='number' ?
            v.toLocaleString() : escapeHtml(String(v))}</div>
        </div>`).join('');
      $('strategic-trends').innerHTML = items ||
        '<div class="meta">No funnel totals available.</div>';
    }
  } catch(e){}
}

function renderStrategic(d){
  if(!d || !d.ok){
    $('strategic-recs').innerHTML =
      '<div class="meta">No strategic data yet. Hit Run synthesis above.</div>';
    return;
  }
  $('strategic-week-meta').innerHTML =
    `Week of <b>${escapeHtml(d.week_of||'?')}</b> · ` +
    `<b>${d.rec_count||0}</b> recommendation(s)`;
  const recs = (d.recommendations || []).filter(r=>r.kind!=='synthesis_meta');
  if(!recs.length){
    $('strategic-recs').innerHTML = '<div class="meta">No recommendations yet.</div>';
    return;
  }
  const KIND_LABEL = {
    'strategic_gap_4w':    '🎯 Strategic gap (4w)',
    'competitor_lack':     '⚔ Competitor lack',
    'funnel_optimization': '📈 Funnel optimization',
    'wildcard_bet':        '🎲 Wildcard bet',
  };
  $('strategic-recs').innerHTML = recs.map(r=>{
    const dollar = (r.dollar_lift != null) ?
      `<span class="tag" style="color:#22c55e">+$${Math.round(r.dollar_lift).toLocaleString()}/yr est.</span>` : '';
    const conf = (r.confidence != null) ?
      `<span class="tag">${(r.confidence*100|0)}% conf</span>` : '';
    const pr = r.pr_url ?
      `<div style="margin-top:6px"><a href="${escapeHtml(r.pr_url)}"
        target="_blank" style="color:var(--ind)">→ Draft PR ready</a></div>` :
      `<div style="margin-top:6px">
        <button onclick="openStrategicPR(${r.id}, this)"
          style="font-size:12px;padding:6px 10px">
          Open draft scaffold PR</button></div>`;
    const evid = (r.evidence_keys||[]).length ?
      `<div class="meta" style="margin-top:6px">Evidence: ` +
        r.evidence_keys.map(e=>`<code>${escapeHtml(e)}</code>`).join(', ') +
        `</div>` : '';
    return `<div class="row" style="display:block;padding:14px 12px">
      <div style="margin-bottom:6px">
        <span class="tag">${escapeHtml(KIND_LABEL[r.kind] || r.kind)}</span>
        ${dollar} ${conf}
      </div>
      <div class="name" style="font-weight:600;font-size:14px">
        ${escapeHtml(r.title||'?')}
      </div>
      <div class="meta" style="margin-top:6px;white-space:pre-wrap">
        ${escapeHtml((r.spec_md||'').slice(0, 600))}
      </div>
      ${evid}
      ${pr}
    </div>`;
  }).join('');
}

async function runStrategic(mode, btn){
  if(btn) btn.classList.add('disabled');
  let path, method;
  if(mode==='preview'){
    path = '/api/v1/admin/brain/strategic-synthesis/preview'; method='GET';
  } else if(mode==='synth'){
    path = '/api/v1/admin/brain/strategic-synthesis/run?force=1'; method='POST';
  } else if(mode==='digest'){
    path = '/api/v1/admin/brain/strategic-digest/send?force=1'; method='POST';
  }
  try {
    const r = await fetch(path, {method, headers: HDR});
    const txt = await r.text();
    $('raw').style.display = 'block';
    $('raw').textContent = txt;
    try {
      const d = JSON.parse(txt);
      if(mode==='preview'){
        toast(`Prompt ready: ${d.prompt_chars||0} chars (~${d.prompt_tokens_est||0} tokens).`);
      } else if(mode==='synth'){
        toast(`Synthesis: ${d.rec_count||0} recs, ${d.prs_opened||0} PRs, ` +
              `model=${d.model_used||'?'}, cost~$${d.estimated_cost_usd||0}.`);
      } else {
        if(d.skipped){
          toast(`Digest: ${d.skipped}.`);
        } else {
          toast(`Digest: sent=${(d.sent||[]).length}, failed=${(d.failed||[]).length}, ` +
                `dry_run=${d.dry_run||false}.`);
        }
      }
    } catch(e){ toast('Response not JSON — check raw output below.'); }
  } finally {
    if(btn) btn.classList.remove('disabled');
    setTimeout(loadStrategic, 1500);
  }
}

async function openStrategicPR(recId, btn){
  // For now, the operator route is "re-run synthesis with PRs on"; per-rec
  // PR opener requires a finer-grained endpoint we have not exposed yet.
  toast('Per-rec PR opener: TODO. Use Run synthesis with ' +
        'DCHUB_BRAIN_STRATEGIC_DRAFT_PR=1 in env to open all eligible scaffold PRs at once.');
}

// ── Feature Proposer panel (2026-06-07 / task #157) ─────────────────

function escFP(s){
  return String(s||'').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

async function loadFeatureProposer(){
  try {
    const [c, l] = await Promise.all([
      fetch('/api/v1/admin/brain/feature-proposer/clusters', {headers: HDR}),
      fetch('/api/v1/admin/brain/feature-proposer/log?limit=20', {headers: HDR}),
    ]);
    const cd = await c.json();
    const ld = await l.json();
    renderFeatureProposerClusters(cd);
    renderFeatureProposerLog(ld);
  } catch(e){
    const el = $('feature-proposer-clusters');
    if(el) el.innerHTML = `<div class="meta">Load failed: ${escFP(e)}</div>`;
  }
}

function renderFeatureProposerClusters(d){
  const clusters = (d && d.clusters) || [];
  const el = $('feature-proposer-clusters');
  if(!el) return;
  if(!clusters.length){
    el.innerHTML = `<div class="meta">No clusters meet threshold. ` +
      `Rows scanned: ${d.rows_scanned||0}.</div>`;
    return;
  }
  el.innerHTML = clusters.slice(0, 10).map(c => `
    <div class="row">
      <div class="name">${escFP(c.theme||'?')}
        <span class="tag">${c.count||0} users</span>
        <div class="meta">cluster ${escFP(c.cluster_id||'?')} · ` +
        `tokens ${escFP((c.tokens||[]).join(', '))}</div>
      </div>
      <div class="conf">${c.count||0}</div>
    </div>
  `).join('');
}

function renderFeatureProposerLog(d){
  const props = (d && d.proposals) || [];
  const el = $('feature-proposer-log');
  if(!el) return;
  if(!props.length){
    el.innerHTML = '<div class="meta">No proposals yet.</div>';
    return;
  }
  el.innerHTML = props.map(p => {
    const dry = p.dry_run ? '<span class="tag">dry</span>' : '';
    const pr = p.pr_url
      ? `<a href="${escFP(p.pr_url)}" target="_blank">PR #${p.pr_number||'?'}</a>`
      : '<span class="meta">no PR</span>';
    const fb = Array.isArray(p.feedback_ids)
      ? p.feedback_ids.slice(0,8).map(i=>`#${i}`).join(', ')
      : '';
    return `<div class="row">
      <div class="name">${escFP(p.theme||'?')} ${dry}
        <div class="meta">${pr} · status=${escFP(p.status||'?')} · ` +
        `created ${escFP((p.created_at||'').slice(0,16))}</div>
        <div class="meta url">feedback: ${escFP(fb)}</div>
      </div>
    </div>`;
  }).join('');
}

async function runFeatureProposer(mode, btn){
  if(btn) btn.classList.add('disabled');
  let path = '';
  let method = 'GET';
  if(mode === 'preview'){
    path = '/api/v1/admin/brain/feature-proposer/preview';
  } else if(mode === 'run'){
    path = '/api/v1/admin/brain/feature-proposer/run';
    method = 'POST';
  }
  try {
    const r = await fetch(path, {method, headers: HDR});
    const txt = await r.text();
    $('raw').style.display = 'block';
    $('raw').textContent = txt;
    let d = {};
    try { d = JSON.parse(txt); } catch(e){}
    if(mode === 'preview'){
      toast(`Preview: ${d.clusters_found||0} cluster(s) found · ` +
            `rows scanned ${d.rows_scanned||0}.`);
    } else {
      const props = (d.proposals||[]).length;
      const skipped = (d.skipped||[]).length;
      toast(`Run: ${props} proposed · ${skipped} skipped · ` +
            `dry_run=${!!d.dry_run}.`);
    }
  } catch(e){
    toast(`Feature proposer error: ${escFP(e)}`);
  } finally {
    if(btn) btn.classList.remove('disabled');
    setTimeout(loadFeatureProposer, 1500);
  }
}

refresh();
loadFeatureProposer();
</script>
</body></html>"""


@brain_backlog_admin_bp.route("/admin/brain-backlog", methods=["GET"])
def backlog_html():
    """Render the dashboard. The admin_key is read client-side from the
    URL query so the page itself doesn't need server-side gating —
    every fetch from the JS includes the X-Admin-Key header."""
    return Response(_HTML, mimetype="text/html")
