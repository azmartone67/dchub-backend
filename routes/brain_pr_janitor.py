"""
brain_pr_janitor.py — PHASE 3.5 (2026-06-19). SAFE DRAFT-PR PILEUP JANITOR.

The brain opens mechanical autofix DRAFT PRs (brain_draft_pr_writer). The
DORMANT auto-merge engine (brain_automerge) only ever MERGES the GREEN ones —
it is fail-closed and NEVER closes the RED ones. So failing PRs accumulate
forever (the same unbounded-pileup failure mode as the L15 issue flood).

This janitor is the CLEANUP half: it CLOSES the brain's OWN autofix draft PRs
whose REQUIRED CI is RED/FAILING and that are older than a grace window, and
QUARANTINES the underlying (file, klass) recipe so it is not re-proposed in an
endless loop. It NEVER merges anything — merging is the separately-gated
brain_automerge step.

╔══════════════════════════════════════════════════════════════════════╗
║ SUPREME CONSTRAINT — SHIPS INERT.                                      ║
║   BRAIN_PR_JANITOR_ENABLED defaults to "0" (OFF). With it off the      ║
║   sweep runs DRY: it returns the would-close list and makes ZERO       ║
║   GitHub WRITES (no PATCH/close), ZERO quarantine writes, ZERO alerts. ║
║   dry_run=True (the function default) ALSO forces dry behavior even if ║
║   the env flag is later flipped on, so a caller can always preview.    ║
╚══════════════════════════════════════════════════════════════════════╝

SAFETY INVARIANTS (asserted in the mocked tests):
  · Only OPEN PRs whose head branch starts with `brain/autofix-` are ever
    considered (reuses brain_automerge.list_autofix_prs — the SAME filter the
    merge engine uses, so we can never touch a human PR).
  · A PR is closed ONLY when ALL of:
      (a) its REQUIRED CI is RED/FAILING — NOT pending, NOT green. We reuse
          brain_automerge.ci_status_for_sha (the SAME fail-closed CI read the
          merge engine uses) and additionally require an explicit FAILURE
          signal: green=False is NOT enough (a pending/no-signal PR also has
          green=False). _ci_is_red() classifies the reason — only terminal
          failure conclusions count as RED.
      (b) the PR is older than BRAIN_PR_JANITOR_GRACE_MIN (env, default 60).
          A young RED PR is left alone (CI may still be re-running).
  · A GREEN PR, a PENDING PR, a no-CI-signal PR, and a RED-but-young PR are
    ALL untouched.
  · CLOSE = REST PATCH /pulls/{n} state=closed. NEVER merge, NEVER delete the
    branch, NEVER force. We do NOT touch branch protection.
  · On a real close we QUARANTINE the (file_path, klass) in the proposals
    table (brain_proposed_code_fixes) status='quarantined' so the recipe is
    NOT re-proposed (the re-propose query selects only
    COALESCE(status,'proposed')='proposed').
  · Best-effort operator alert (main._resend_email). Never raises.
  · A per-run cap (BRAIN_PR_JANITOR_MAX_PER_RUN, default 10) bounds blast
    radius even in a degenerate pileup.

REUSE (do NOT duplicate the CI / listing logic):
  · routes.brain_automerge.list_autofix_prs  — the OPEN brain/autofix-* listing.
  · routes.brain_automerge.ci_status_for_sha — the required-CI status read.
  · routes.brain_automerge._gh_cfg / _gh_headers — the SAME single token/repo.
  · routes.brain_automerge._alert_operator    — the operator email path.
  · routes.brain_draft_pr_writer.get_logged_proposal_for_branch — to recover
    the (file_path, klass) behind a PR for quarantine.
  · routes.brain_mechanical_classifier._admin_ok — endpoint gate.

Heavy imports (flask, requests, psycopg2) are LAZY so this module imports
cleanly in a no-flask test env where every primitive is monkeypatched.
"""
from __future__ import annotations

import os
import time


# ── env helpers ──────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _enabled() -> bool:
    """Master switch. Defaults OFF ("0"); only the exact string "1" enables.
    Read live (not at import) so an operator flip is honored."""
    return os.environ.get("BRAIN_PR_JANITOR_ENABLED", "0") == "1"


def _grace_min() -> int:
    """Grace window (minutes) a RED PR must exceed before it is closable."""
    return max(0, _env_int("BRAIN_PR_JANITOR_GRACE_MIN", 60))


def _max_per_run() -> int:
    return max(0, _env_int("BRAIN_PR_JANITOR_MAX_PER_RUN", 10))


AUTOFIX_BRANCH_PREFIX = "brain/autofix-"


# ── CI verdict classification (RED vs pending vs green) ──────────────
# ci_status_for_sha (reused, NOT re-implemented) returns:
#   {green:True}                         → GREEN
#   {green:False, reason:"no_ci_signals"}→ no CI yet  (NOT red)
#   {green:False, reason:"...pending"}   → still running (NOT red)
#   {green:False, reason:"combined_status_failure" / "check_run_failure" /
#                        "check_run_error" / "check_run_timed_out" / ...} → RED
# We treat ONLY an explicit terminal-failure reason as RED. Anything we can't
# positively classify as a failure is treated as NOT red (fail-safe: when in
# doubt we leave the PR alone rather than close a maybe-passing PR).
_PENDING_MARKERS = (
    "pending", "no_ci_signals", "no_sha", "no_token", "deps:",
    "http_", "queued", "in_progress", "expected", "waiting", "checks:0",
)
_RED_MARKERS = (
    "failure", "failed", "error", "timed_out", "timeout", "cancelled",
    "canceled", "action_required", "stale", "startup_failure",
)


def _ci_is_red(ci: dict) -> bool:
    """True ONLY when the CI status reflects a terminal FAILURE. A green status,
    a pending/queued/no-signal status, or any status we can't positively
    classify as a failure ⇒ False (do NOT close). Never raises."""
    try:
        if not isinstance(ci, dict):
            return False
        if ci.get("green"):
            return False
        reason = str(ci.get("reason") or "").lower()
        if not reason:
            return False
        # A pending / transport / no-signal reason is NOT a failure.
        if any(m in reason for m in _PENDING_MARKERS):
            return False
        # Require an explicit failure marker — never infer RED from absence.
        return any(m in reason for m in _RED_MARKERS)
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════
#  GitHub: CLOSE a PR (the ONLY write this module makes). Reuses the
#  brain_automerge token/repo/header config so there is ONE GitHub client.
# ════════════════════════════════════════════════════════════════════
def close_pr(pr_number: int, *, reason: str = "") -> dict:
    """PATCH /repos/{repo}/pulls/{n} state=closed. NEVER merges, NEVER deletes
    the branch. Optionally posts a closing comment (best-effort). Returns
    {ok} or {ok:False, error}. Never raises. Tests monkeypatch this."""
    try:
        from routes.brain_automerge import _gh_cfg, _gh_headers
    except Exception as e:
        return {"ok": False, "error": f"cfg_import:{type(e).__name__}:{str(e)[:80]}"}
    try:
        cfg = _gh_cfg()
    except Exception as e:
        return {"ok": False, "error": f"cfg:{type(e).__name__}:{str(e)[:80]}"}
    token = (cfg or {}).get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    repo = cfg.get("upstream") or "azmartone67/dchub-backend"
    h = _gh_headers(token)
    try:
        # Best-effort breadcrumb comment so the closure is auditable. A failure
        # here must NOT block the close itself, so it is fully swallowed.
        if reason:
            try:
                requests.post(
                    f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
                    headers=h, timeout=20,
                    json={"body": ("🧹 **brain PR janitor** auto-closing this "
                                   "stale RED autofix PR.\n\n" + str(reason)[:400])},
                )
            except Exception:
                pass
        r = requests.patch(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            headers=h, timeout=20, json={"state": "closed"},
        )
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"{r.status_code}:{r.text[:160]}"}
        j = r.json() or {}
        if (j.get("state") or "").lower() != "closed":
            return {"ok": False, "error": f"not_closed:{str(j)[:120]}"}
        return {"ok": True, "number": pr_number}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


# ════════════════════════════════════════════════════════════════════
#  Quarantine the (file, klass) recipe so it is NOT re-proposed.
#  Writes brain_proposed_code_fixes status='quarantined' (the re-propose
#  query selects only COALESCE(status,'proposed')='proposed'). Best-effort.
# ════════════════════════════════════════════════════════════════════
def _db_url() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def quarantine_recipe(*, file_path: str, klass: str, note: str = "") -> bool:
    """Mark every still-'proposed' brain_proposed_code_fixes row matching this
    (file_path, finding_class) as status='quarantined' so the brain does NOT
    re-propose the failing recipe. Best-effort; returns committed. Never raises.

    Matches on file_path AND finding_class so a DIFFERENT recipe for the same
    file is untouched. If klass is falsy we match on file_path alone (still
    scoped to that one file). Only flips rows whose status is currently
    'proposed' (or NULL) — never resurrects/clobbers a resolved/merged row."""
    fp = (file_path or "").strip()
    if not fp:
        return False
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            if klass:
                cur.execute(
                    "UPDATE brain_proposed_code_fixes "
                    "   SET status = 'quarantined', "
                    "       reviewed_at = NOW(), "
                    "       reviewer_note = %s "
                    " WHERE file_path = %s "
                    "   AND LOWER(COALESCE(finding_class, '')) = LOWER(%s) "
                    "   AND COALESCE(status, 'proposed') = 'proposed'",
                    (("janitor: " + str(note))[:500], fp, str(klass)),
                )
            else:
                cur.execute(
                    "UPDATE brain_proposed_code_fixes "
                    "   SET status = 'quarantined', "
                    "       reviewed_at = NOW(), "
                    "       reviewer_note = %s "
                    " WHERE file_path = %s "
                    "   AND COALESCE(status, 'proposed') = 'proposed'",
                    (("janitor: " + str(note))[:500], fp),
                )
            conn.commit()
        return True
    except Exception:
        return False


# ── Recover the (file_path, klass) behind a PR (reuses the writer's log) ──
def _recipe_for_pr(pr) -> dict:
    """Recover {file_path, klass} for an autofix PR from the Phase-2 writer's
    proposal log (the SAME source brain_automerge re-verifies against). Falls
    back to parsing the head branch (brain/autofix-<klass>-...) and the PR
    title for the file path. Never raises. Returns {} if nothing recoverable."""
    out = {"file_path": "", "klass": ""}
    ref = (pr or {}).get("head_ref") or ""
    # 1) Authoritative: the logged proposal keyed by branch.
    try:
        from routes.brain_draft_pr_writer import get_logged_proposal_for_branch
        logged = get_logged_proposal_for_branch(ref) or {}
        if isinstance(logged, dict):
            out["file_path"] = (logged.get("file_path") or "").strip()
            out["klass"] = (logged.get("klass") or "").strip()
    except Exception:
        pass
    # 2) Fallback klass from the branch name: brain/autofix-<klass>-<id>-<hash>.
    if not out["klass"] and ref.startswith(AUTOFIX_BRANCH_PREFIX):
        try:
            tail = ref[len(AUTOFIX_BRANCH_PREFIX):]
            out["klass"] = (tail.rsplit("-", 2)[0] if "-" in tail else tail).strip()
        except Exception:
            pass
    return out


# ════════════════════════════════════════════════════════════════════
#  THE SWEEP
# ════════════════════════════════════════════════════════════════════
def janitor_sweep(dry_run: bool = True) -> dict:
    """One janitor pass over the brain's OPEN autofix draft PRs.

    For each OPEN brain/autofix-* PR whose REQUIRED CI is RED/FAILING (not
    pending, not green) AND older than the grace window, CLOSE it (REST PATCH
    state=closed) + QUARANTINE its (file, klass) recipe + best-effort alert.
    GREEN / PENDING / RED-within-grace PRs are left untouched.

    dry_run=True (DEFAULT) OR the env flag BRAIN_PR_JANITOR_ENABLED != "1" ⇒
    DRY: return the would-close list and make ZERO closes / quarantines /
    alerts. Acting requires BOTH dry_run=False AND the env flag on.

    Returns {dry_run, enabled, grace_min, scanned, closed:[...],
    would_close:[...], skipped:[...], cap}. Never raises out."""
    # `acting` is true ONLY when the caller opted in AND the env flag is on.
    acting = (not dry_run) and _enabled()
    grace_min = _grace_min()
    grace_s = grace_min * 60
    cap = _max_per_run()
    now = time.time()

    out = {
        "dry_run": (not acting),
        "enabled": _enabled(),
        "grace_min": grace_min,
        "cap": cap,
        "scanned": 0,
        "closed": [],
        "would_close": [],
        "skipped": [],
    }

    # List the brain's OPEN autofix PRs — REUSE the merge engine's listing so
    # we apply the IDENTICAL brain/autofix-* filter (never a human PR).
    try:
        from routes.brain_automerge import (
            list_autofix_prs, ci_status_for_sha,
        )
    except Exception as e:
        out["error"] = f"import_automerge:{type(e).__name__}:{str(e)[:100]}"
        return out

    try:
        listing = list_autofix_prs()
    except Exception as e:
        out["error"] = f"list_exception:{type(e).__name__}:{str(e)[:100]}"
        return out
    if not isinstance(listing, dict) or not listing.get("ok"):
        out["error"] = "list_failed:" + str((listing or {}).get("error"))
        return out

    acted = 0
    for pr in (listing.get("prs") or []):
        out["scanned"] += 1
        num = pr.get("number")
        ref = pr.get("head_ref") or ""

        # Defense-in-depth: list already filtered, but re-assert the prefix.
        if not ref.startswith(AUTOFIX_BRANCH_PREFIX):
            out["skipped"].append({"pr": num, "reason": "not_autofix_branch"})
            continue

        # ── REQUIRED CI must be RED (not pending, not green). ────────────
        try:
            ci = ci_status_for_sha(pr.get("head_sha"))
        except Exception as e:
            out["skipped"].append({"pr": num,
                                    "reason": f"ci_exception:{str(e)[:80]}"})
            continue
        if (ci or {}).get("green"):
            out["skipped"].append({"pr": num, "reason": "ci_green"})
            continue
        if not _ci_is_red(ci):
            out["skipped"].append(
                {"pr": num,
                 "reason": "ci_not_red:" + str((ci or {}).get("reason"))})
            continue

        # ── Must be older than the grace window. ────────────────────────
        age_s = _pr_age_seconds(pr, now)
        if age_s is None:
            # Can't establish age ⇒ fail-safe, don't close.
            out["skipped"].append({"pr": num, "reason": "no_age (fail-safe skip)"})
            continue
        if age_s < grace_s:
            out["skipped"].append(
                {"pr": num, "reason": "within_grace",
                 "age_min": round(age_s / 60, 1), "grace_min": grace_min})
            continue

        recipe = _recipe_for_pr(pr)
        candidate = {
            "pr": num,
            "head_ref": ref,
            "age_min": round(age_s / 60, 1),
            "ci_reason": str((ci or {}).get("reason")),
            "file_path": recipe.get("file_path") or "",
            "klass": recipe.get("klass") or "",
        }

        # ── DRY: record the would-close and move on (ZERO writes). ──────
        if not acting:
            out["would_close"].append(candidate)
            continue

        # ── Cap blast radius even in a degenerate pileup. ───────────────
        if acted >= cap:
            out["skipped"].append({"pr": num,
                                   "reason": f"rate_cap_reached ({cap})"})
            continue

        # ── ACT: close → quarantine → alert (all best-effort). ──────────
        reason_txt = (f"RED required CI ({candidate['ci_reason']}), "
                      f"age {candidate['age_min']}min > grace {grace_min}min.")
        try:
            closed = close_pr(num, reason=reason_txt)
        except Exception as e:
            closed = {"ok": False, "error": f"close_exception:{str(e)[:80]}"}
        if not closed.get("ok"):
            out["skipped"].append({"pr": num,
                                   "reason": "close_failed:" + str(closed.get("error"))})
            continue

        acted += 1
        q_ok = False
        try:
            q_ok = quarantine_recipe(
                file_path=candidate["file_path"], klass=candidate["klass"],
                note=f"closed RED autofix PR #{num} ({candidate['ci_reason']})",
            )
        except Exception:
            q_ok = False

        try:
            _alert_close(candidate)
        except Exception:
            pass

        result = dict(candidate)
        result["quarantined"] = q_ok
        out["closed"].append(result)

    return out


def _pr_age_seconds(pr, now: float):
    """Best-effort age (seconds) of a PR. Prefers an explicit created_at /
    updated_at timestamp on the PR dict (ISO-8601, GitHub style); falls back
    to a numeric created_epoch if present. Returns None when age can't be
    established so the caller fail-safes (does NOT close). Never raises."""
    if not isinstance(pr, dict):
        return None
    # Numeric epoch fast-path (used by tests + any caller that pre-resolves it).
    for k in ("created_epoch", "age_epoch"):
        v = pr.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return max(0.0, now - float(v))
    # ISO-8601 created_at / updated_at (GitHub's pulls payload shape).
    for k in ("created_at", "updated_at"):
        ts = pr.get(k)
        if not ts:
            continue
        epoch = _iso_to_epoch(ts)
        if epoch is not None:
            return max(0.0, now - epoch)
    return None


def _iso_to_epoch(ts: str):
    """Parse a GitHub ISO-8601 timestamp (e.g. '2026-06-19T12:00:00Z') to a
    UTC epoch. Returns None on any failure. Never raises."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime, timezone
        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _alert_close(candidate: dict) -> bool:
    """Best-effort operator email when a stale RED PR is closed. Reuses
    brain_automerge._alert_operator (main._resend_email). Never raises."""
    try:
        from routes.brain_automerge import _alert_operator
    except Exception:
        return False
    try:
        return bool(_alert_operator(
            f"🧹 Brain PR janitor closed stale RED autofix PR #{candidate.get('pr')}",
            (f"<h2>Janitor closed a stale RED autofix PR</h2>"
             f"<p><b>PR:</b> #{candidate.get('pr')}</p>"
             f"<p><b>Branch:</b> {candidate.get('head_ref')}</p>"
             f"<p><b>File:</b> {candidate.get('file_path') or '(unknown)'}</p>"
             f"<p><b>Recipe class:</b> {candidate.get('klass') or '(unknown)'}</p>"
             f"<p><b>CI:</b> {candidate.get('ci_reason')}</p>"
             f"<p><b>Age:</b> {candidate.get('age_min')} min</p>"
             f"<p>The (file, class) recipe was quarantined so it is not "
             f"re-proposed. No merge was performed.</p>"),
        ))
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════
#  ENDPOINT (admin-gated; honors the OFF gate — defaults to dry-run)
# ════════════════════════════════════════════════════════════════════
def _make_blueprint():
    """Build the Flask blueprint LAZILY so this module imports without flask in
    the test env. main.py calls this inside its guarded try/except."""
    from flask import Blueprint, jsonify, request
    from routes.brain_mechanical_classifier import _admin_ok

    bp = Blueprint("brain_pr_janitor", __name__)

    @bp.post("/api/v1/brain/pr-janitor/sweep")
    def _sweep():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        body = request.get_json(silent=True) or {}
        # Default to dry-run. Acting requires BOTH ?act=1 (or {"act":true})
        # AND the env flag — janitor_sweep re-checks the env flag itself.
        want_act = bool(body.get("act") or request.args.get("act") == "1")
        dry = not (want_act and _enabled())
        result = janitor_sweep(dry_run=dry)
        return jsonify(ok=True, enabled=_enabled(), result=result), 200

    @bp.get("/api/v1/brain/pr-janitor/status")
    def _status():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        # Status is always a DRY preview — never acts.
        preview = janitor_sweep(dry_run=True)
        return jsonify(
            ok=True,
            enabled=_enabled(),
            grace_min=_grace_min(),
            max_per_run=_max_per_run(),
            would_close_count=len(preview.get("would_close", [])),
            preview=preview,
            note=("Dormant unless BRAIN_PR_JANITOR_ENABLED=1. Closes only "
                  "stale RED brain/autofix-* draft PRs; quarantines the recipe; "
                  "NEVER merges."),
        ), 200

    return bp


def register(app) -> bool:
    """Register the blueprint on `app`. Returns True on success. Guarded —
    main.py wraps this in its own try/except too."""
    try:
        app.register_blueprint(_make_blueprint())
        return True
    except Exception:
        return False
