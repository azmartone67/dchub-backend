"""
routes/ingestion_integrity_master_shell.py — can the producers still run?
=============================================================================

2026-08-10. Three failures were found live on the same afternoon, and the
/whats-new copy explained NONE of them — it described flat layers in terms of
upstream cadence while the actual causes sat one layer down, in the plumbing
that runs the loaders:

  1. AUTH DRIFT.  The GitHub secret DCHUB_INTERNAL_KEY was last set 2026-05-04
     and no longer matched the 64-char value on Railway. Step 1 of
     SECURITY_KEY_ROTATION.md (2026-06-07) — `gh secret set
     DCHUB_INTERNAL_KEY` — was never executed, and INTERNAL_AUTH_LEGACY_OK=0
     had since closed the legacy bypass. Every workflow sending X-Internal-Key
     got 401. data-sync failed 23 of its last 40 runs on the subsea step alone.

  2. A WORKFLOW THAT LEFT.  daily-infra-sync.yml was not on the default branch,
     so GitHub carried it as state=deleted and its 04:08 cron silently stopped
     firing on 2026-07-25. Sixteen days of nothing, no alarm anywhere: a
     workflow that does not exist cannot fail, and the dead-man board only
     watches feeds that BEAT it.

  3. A PRODUCER RETURNING NOTHING.  overpass-api.de began answering HTTP 406 to
     the crawler's User-Agent. 406 was not in osm_crawler's handled-status set
     (429/502/503/504), so it fell through to a generic "error", every bbox
     returned zero, and the run reported "swept 0 POIs". Twelve consecutive
     zero-row runs rode inside GREEN runs before the 08-08 change made the
     zero-fetch branch exit 1.

The common shape: THE JOB DID NOT RUN, AND NOTHING SAID SO. Freshness boards
answer "was the table written" (/admin/ingestion-freshness) and the liveness
board answers "did it GROW" (/admin/data-liveness). Neither can see a producer
that never got to the point of writing. This shell watches the layer above
both — auth, existence, and output — and each lane exists because a real
failure walked through it today.

HONESTY RULES (shared with every master shell here)
---------------------------------------------------
- A lane that cannot MEASURE renders "?", never PASS. _lane_verdict enforces
  it; a lane whose checks are all indeterminate can never read green.
- A lane that CRASHES renders "?" and does not take the tick down (_safe_lane).
- This shell CANNOT read GitHub secret VALUES — nothing can, they are
  write-only. Lane `cron_auth` therefore convicts on the OBSERVED RUN OUTCOME
  of the workflows that send the key, which is what actually broke. That limit
  is published in _population() rather than papered over.
- The published population is built from the executed lists, never hand-typed.
"""
from __future__ import annotations

import os
import re as _re

from flask import Blueprint, Response, jsonify

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _lane_verdict, _safe_lane)
from routes.audit_closure_master_shell import _jget, _local

ingestion_integrity_master_shell_bp = Blueprint(
    "ingestion_integrity_master_shell", __name__)

_REPO = os.environ.get("GITHUB_REPOSITORY", "azmartone67/dchub-backend")

# A workflow carrying state=deleted is only a FINDING if it was recently alive.
# Something retired last quarter is deleted on purpose; something that ran
# inside this window and then vanished stopped without anyone deciding to.
_DELETED_RECENT_DAYS = 60

# Consecutive zero-row runs before a "successful" producer is called dead. The
# dead-man board already computes the streak; this is the line at which the
# streak becomes a lane failure rather than a note.
_ZERO_ROW_STREAK = 3


def _disabled() -> bool:
    return os.environ.get("INGESTION_INTEGRITY_SHELL_DISABLE", "") == "1"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gh(path: str, timeout: int = 10):
    """GitHub API GET → (json, None) or (None, reason). NEVER raises.

    Fail-soft on a missing token: an unauthenticated read of a PRIVATE repo is
    a 404, which must not be reported as "no workflows are deleted".
    """
    tok = (os.environ.get("GITHUB_TOKEN")
           or os.environ.get("PR_SUBMIT_TOKEN") or "").strip()
    if not tok:
        return None, "no GITHUB_TOKEN on this host"
    return _jget("https://api.github.com" + path, timeout=timeout,
                 headers={"Authorization": "Bearer " + tok,
                          "Accept": "application/vnd.github+json",
                          "User-Agent": "dchub-ingestion-integrity/1.0"})


def _age_days(iso: str):
    """Days since an ISO-8601 stamp, or None if unparseable."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        return None


# ── which workflows hand the internal key to the origin ──────────────────────
# Read from the checkout rather than the API: the question is "which workflows
# SEND this credential", and that is a property of the file body. Returns
# (files, reason) — an empty list with a reason means UNKNOWN, not "none".

_KEY_MARK = _re.compile(r"X-Internal-Key|DCHUB_INTERNAL_KEY")


def _uncommented(body: str) -> str:
    """Drop YAML comment lines before matching.

    Caught on this module's own first live tick: daily-infra-sync.yml was
    rewritten to send X-Admin-Key and EXPLAINS the old X-Internal-Key bug in a
    header comment. Matching raw text listed it as a key caller, so a workflow
    would have been probed — and could have been convicted — for a credential
    it no longer sends. Only lines that survive to the shell count.
    """
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith("#"))


def _internal_key_workflows() -> tuple[list[str], str | None]:
    wf_dir = os.path.join(_repo_root(), ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return [], ("no .github/workflows in the deployed tree — the workflow "
                    "bodies are not shipped to this host")
    hits = []
    try:
        for fn in sorted(os.listdir(wf_dir)):
            if not fn.endswith((".yml", ".yaml")):
                continue
            try:
                with open(os.path.join(wf_dir, fn), "r", encoding="utf-8",
                          errors="replace") as fh:
                    if _KEY_MARK.search(_uncommented(fh.read())):
                        hits.append(fn)
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        return [], f"could not scan workflows: {type(e).__name__}"
    if not hits:
        return [], "scanned the workflow dir and found no internal-key caller"
    return hits, None


# ── lane 1 · can CI still authenticate ───────────────────────────────────────
def _lane_cron_auth() -> list[dict]:
    out = []

    key = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    out.append(_check(
        "key_present", "DCHUB_INTERNAL_KEY is set on this host",
        bool(key),
        (f"present, {len(key)} chars" if key else
         "MISSING — every X-Internal-Key caller will 401 against this host"),
        critical=True))

    files, why = _internal_key_workflows()
    if why:
        out.append(_check("callers", "workflows that send the internal key",
                          None, why, critical=True))
        return out

    out.append(_check("callers", "workflows that send the internal key", True,
                      f"{len(files)} caller(s): " + ", ".join(files)))

    failing, unknown = [], []
    for fn in files:
        d, err = _gh(f"/repos/{_REPO}/actions/workflows/{fn}/runs"
                     "?per_page=1&status=completed")
        if d is None:
            unknown.append(f"{fn} ({err})")
            continue
        runs = d.get("workflow_runs") or []
        if not runs:
            unknown.append(f"{fn} (no completed runs)")
            continue
        r = runs[0]
        if r.get("conclusion") not in ("success", "skipped"):
            failing.append("%s → %s on %s" % (fn, r.get("conclusion"),
                                              (r.get("created_at") or "?")[:10]))

    if failing:
        out.append(_check(
            "latest_run", "latest run of every internal-key caller is green",
            False,
            "RED: " + "; ".join(failing) +
            " — if these are 401s, re-sync the GitHub secret to the Railway "
            "value (SECURITY_KEY_ROTATION.md step 1)",
            critical=True))
    elif len(unknown) == len(files):
        out.append(_check(
            "latest_run", "latest run of every internal-key caller is green",
            None, "could not read any run: " + "; ".join(unknown),
            critical=True))
    else:
        detail = f"{len(files) - len(unknown)}/{len(files)} callers green"
        if unknown:
            detail += " · unread: " + "; ".join(unknown)
        out.append(_check("latest_run",
                          "latest run of every internal-key caller is green",
                          True, detail))
    return out


# GitHub caps this listing at 100 per page. The repo has 159 workflows, so the
# first shipped version of this lane read 100 and reported "?" — honest, but
# permanently indeterminate, and blind to exactly the thing it watches for: on
# the first live tick the unread remainder was 59 workflows wide, and
# daily-infra-sync could have been sitting in it. Walk every page.
_WF_PAGE = 100
_WF_MAX_PAGES = 20          # 2,000 workflows; a runaway-loop backstop, not a cap


def _all_workflows() -> tuple[list | None, int | None, str | None]:
    """Every workflow across all pages → (workflows, total_count, reason).

    workflows is None only when the FIRST page fails — nothing was learned. A
    later page failing returns what was read plus a reason, so the caller can
    say "read N of M" instead of silently sweeping a partial inventory.
    """
    got: list = []
    total = None
    for page in range(1, _WF_MAX_PAGES + 1):
        d, err = _gh(f"/repos/{_REPO}/actions/workflows"
                     f"?per_page={_WF_PAGE}&page={page}")
        if d is None:
            return (None, None, err) if page == 1 else (got, total, err)
        if total is None:
            total = d.get("total_count")
        batch = d.get("workflows") or []
        got.extend(batch)
        if len(batch) < _WF_PAGE:
            break
        if total is not None and len(got) >= total:
            break
    else:
        return got, total, (f"stopped at the {_WF_MAX_PAGES}-page backstop")
    return got, total, None


# ── lane 2 · does every scheduled producer still exist ───────────────────────
def _lane_workflow_present() -> list[dict]:
    """GitHub reports state=deleted for a workflow absent from the default
    branch. Its cron stops firing SILENTLY — there is no run to fail, no beat
    to miss, and every board stays green. daily-infra-sync sat like that for
    16 days."""
    out = []
    wfs, total, err = _all_workflows()
    if wfs is None:
        return [_check("list", "workflow inventory readable", None,
                       f"GitHub API unreadable: {err}", critical=True)]

    out.append(_check("list", "workflow inventory readable", True,
                      f"{len(wfs)} of {total} workflows read"))
    if total and len(wfs) < total:
        # Still possible if a page read fails mid-walk. Indeterminate, never a
        # confident PASS — the whole point of this lane is the workflow you
        # cannot see.
        out.append(_check(
            "list_complete", "inventory is complete", None,
            f"only {len(wfs)} of {total} were read ({err or 'page walk cut '
            'short'}) — a deleted workflow could sit in the unread remainder",
            critical=True))

    ghosts, unread = [], []
    for w in wfs:
        if (w.get("state") or "") != "deleted":
            continue
        # Deleted-and-long-quiet is a retirement. Deleted-but-recently-running
        # is a producer that stopped without anyone deciding it should.
        path = (w.get("path") or "").split("/")[-1]
        r, rerr = _gh(f"/repos/{_REPO}/actions/workflows/{w.get('id')}/runs"
                      "?per_page=1")
        if r is None:
            unread.append(f"{path} ({rerr})")
            continue
        runs = r.get("workflow_runs") or []
        if not runs:
            continue
        age = _age_days(runs[0].get("created_at") or "")
        if age is not None and age <= _DELETED_RECENT_DAYS:
            ghosts.append("%s (last ran %.0fd ago)" % (path, age))

    if ghosts:
        out.append(_check(
            "no_ghosts", "no recently-active workflow has left the default "
            "branch", False,
            "GHOST: " + "; ".join(ghosts) +
            " — state=deleted means the file is not on the default branch, so "
            "the cron no longer fires and nothing goes red",
            critical=True))
    elif unread:
        out.append(_check(
            "no_ghosts", "no recently-active workflow has left the default "
            "branch", None,
            "could not date some deleted workflows: " + "; ".join(unread),
            critical=True))
    else:
        out.append(_check(
            "no_ghosts", "no recently-active workflow has left the default "
            "branch", True,
            f"no workflow with state=deleted has run in the last "
            f"{_DELETED_RECENT_DAYS}d"))
    return out


# ── lane 3 · are the producers actually producing ────────────────────────────
_STREAK = _re.compile(r"(\d+)\s+consecutive zero-row runs")


def _lane_producer_liveness() -> list[dict]:
    out = []
    d, err = _jget(_local("/api/v1/ops/deadman"), timeout=10)
    if d is None:
        return [_check("board", "dead-man board readable", None,
                       f"board unreadable: {err}", critical=True)]

    feeds = d.get("feeds") or []
    out.append(_check("board", "dead-man board readable", True,
                      f"{len(feeds)} feeds on the board"))

    errored, starved = [], []
    for f in feeds:
        name = f.get("feed") or "?"
        if (f.get("status") or "") == "error":
            errored.append(f"{name} (status=error)")
        for reason in (f.get("reasons") or []):
            m = _STREAK.search(reason)
            if m and int(m.group(1)) >= _ZERO_ROW_STREAK:
                starved.append(f"{name} ({m.group(1)} zero-row runs)")
                break

    out.append(_check(
        "no_errors", "no producer is reporting status=error",
        not errored,
        ("RED: " + "; ".join(errored)) if errored
        else "every feed's last beat carried a non-error status",
        critical=True))

    out.append(_check(
        "no_starved", f"no producer has >= {_ZERO_ROW_STREAK} consecutive "
        f"zero-row runs", not starved,
        ("RED: " + "; ".join(starved) +
         " — a producer returning zero rows inside a green run is the exact "
         "shape that hid osm-crawl for 12 runs") if starved
        else f"no feed is at or past a {_ZERO_ROW_STREAK}-run zero streak"))
    return out


_LANES = (
    ("cron_auth", "can CI still authenticate to the job endpoints",
     _lane_cron_auth),
    ("workflow_present", "does every scheduled producer still exist",
     _lane_workflow_present),
    ("producer_liveness", "are the producers actually producing",
     _lane_producer_liveness),
)


def _population() -> dict:
    """Built from the executed lane list, never hand-typed (#2253)."""
    files, why = _internal_key_workflows()
    return {
        "question": ("not 'was the table written' (/admin/ingestion-freshness) "
                     "and not 'did it grow' (/admin/data-liveness), but 'COULD "
                     "THE LOADER RUN AT ALL'"),
        "lanes": [lid for lid, _, _ in _LANES],
        "repo": _REPO,
        "internal_key_callers": files or None,
        "internal_key_callers_unknown": why,
        "cannot_measure": (
            "GitHub secret VALUES are write-only — no service can read them, "
            "so this shell cannot diff the CI-side key against the Railway "
            "one. Lane cron_auth convicts on the OBSERVED RUN OUTCOME of the "
            "workflows that send it, which is the signal that actually moved "
            "when the key drifted."),
        "ghost_rule": (
            f"state=deleted AND a run within {_DELETED_RECENT_DAYS}d = a "
            f"producer that stopped without a decision. Deleted-and-quiet is "
            f"a retirement and is not flagged."),
        "starved_rule": (
            f">= {_ZERO_ROW_STREAK} consecutive zero-row runs, as computed by "
            f"the dead-man board itself"),
    }


def _tick() -> dict:
    lanes = []
    for lid, name, fn in _LANES:
        checks = _safe_lane(fn)
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    return {
        "shell": "ingestion-integrity",
        "note": ("Freshness says the table was written; liveness says it grew. "
                 "This says the loader could run — auth, existence, output. "
                 "All three lanes exist because a real failure walked through "
                 "them on 2026-08-10."),
        "population": _population(),
        "lanes": lanes,
        "lanes_total": len(lanes),
        "lanes_pass": sum(1 for x in lanes if x["verdict"] == "PASS"),
        "summary": " ".join(f"{x['id']}={x['verdict']}" for x in lanes),
    }


@ingestion_integrity_master_shell_bp.route(
    "/api/v1/admin/ingestion-integrity/master-tick", methods=["GET"])
def ingestion_integrity_master_tick():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_tick())


@ingestion_integrity_master_shell_bp.route(
    "/admin/ingestion-integrity", methods=["GET"])
def ingestion_integrity_board():
    if _disabled():
        return Response("shell disabled", mimetype="text/plain")
    if not _admin_ok():
        return Response("unauthorized", status=401, mimetype="text/plain")
    t = _tick()
    rows = []
    for lane in t["lanes"]:
        rows.append(f"\n{lane['verdict']:<5} {lane['id']} — {lane['name']}")
        for c in lane["checks"]:
            mark = {True: "OK ", False: "RED", None: " ? "}[c["pass"]]
            rows.append(f"   [{mark}] {c['name']}\n        {c['detail']}")
    return Response(t["summary"] + "\n" + t["note"] + "\n" + "\n".join(rows),
                    mimetype="text/plain")


def register_ingestion_integrity_master_shell(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(ingestion_integrity_master_shell_bp)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "ingestion_integrity_master_shell wiring failed: %s", e)
