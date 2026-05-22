"""
Phase FF+selfreg Stage 5 (2026-05-22) — autonomy guardrails for the brain.

Before the brain gets "hands" (auto-opening PRs from its worklist/directives),
it needs brakes. This module is the single chokepoint every autonomous write
action MUST pass through:

  • KILL SWITCH   — env BRAIN_AUTONOMY_DISABLED=1 halts ALL autonomous actions
                    instantly, no deploy needed.
  • CHANGE BUDGET — at most N auto-PRs/day (env BRAIN_DAILY_PR_CAP, default 8),
                    counted in brain_meta so it survives restarts.
  • NEVER auto-merge — this module only ever gates PR *creation*. Humans hold
                    the merge button. Nothing here can merge or deploy.

Fail-safe: if the store/DB is unavailable we DENY (return False) rather than
fail open — an autonomous system that can't check its own budget shouldn't act.
"""

import os
import datetime as _dt

try:
    from routes import brain_v2_store as _store
except Exception:  # pragma: no cover
    _store = None

DEFAULT_DAILY_PR_CAP = 8


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def autonomy_enabled() -> bool:
    """False if the kill switch is set. Env-driven so it flips with no deploy."""
    return not _truthy(os.environ.get("BRAIN_AUTONOMY_DISABLED"))


def daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("BRAIN_DAILY_PR_CAP", DEFAULT_DAILY_PR_CAP)))
    except Exception:
        return DEFAULT_DAILY_PR_CAP


def _today_key() -> str:
    d = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    return f"auto_pr_count:{d}"


def prs_today() -> int:
    if _store is None:
        return 0
    row = _store.get_meta(_today_key())
    try:
        return int((row or {}).get("value") or 0)
    except Exception:
        return 0


def can_open_pr() -> tuple[bool, str]:
    """The gate. Returns (allowed, reason). DENIES if the store is unavailable
    (can't verify budget) — never fail open into an unbounded PR loop."""
    if not autonomy_enabled():
        return False, "kill_switch: BRAIN_AUTONOMY_DISABLED is set"
    if _store is None:
        return False, "store_unavailable: cannot verify change budget — denying"
    cap = daily_cap()
    if cap <= 0:
        return False, "daily_cap is 0 — autonomy paused"
    used = prs_today()
    if used >= cap:
        return False, f"daily_budget_exhausted ({used}/{cap} auto-PRs today)"
    return True, f"ok ({used}/{cap} used today)"


def record_pr_opened() -> int:
    """Increment today's counter AFTER a PR is successfully opened. Returns the
    new count. Best-effort — a miss just means a slightly loose budget."""
    if _store is None:
        return 0
    new = prs_today() + 1
    try:
        _store.set_meta(_today_key(), str(new))
    except Exception:
        pass
    return new


def status() -> dict:
    """Snapshot for dashboards/admin probes — no secrets."""
    cap = daily_cap()
    used = prs_today()
    ok, reason = can_open_pr()
    return {
        "autonomy_enabled": autonomy_enabled(),
        "daily_cap": cap,
        "prs_today": used,
        "remaining_today": max(0, cap - used),
        "can_open_pr_now": ok,
        "gate_reason": reason,
        "auto_merge": False,  # invariant — always human-gated
    }


# ---------------------------------------------------------------------------
# Blueprint: status (public) + auto-remediate consumer (admin-gated)
# ---------------------------------------------------------------------------
import hmac as _hmac
from flask import Blueprint, jsonify, request

brain_guardrails_bp = Blueprint("brain_guardrails", __name__)


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("BRAIN_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    provided = request.headers.get("X-Admin-Key", "")
    return bool(expected) and bool(provided) and _hmac.compare_digest(provided, expected)


@brain_guardrails_bp.get("/api/v1/brain/guardrails")
def _guardrails_status():
    return jsonify(ok=True, **status()), 200


# Findings whose `issue` maps to a PR-opener fix handler today. The consumer
# only attempts auto-PRs for these; everything else is left for a human (or a
# future fix-generator). Keep in lockstep with brain_pr_opener._FIX_HANDLERS.
_AUTO_PR_SUPPORTED = {"blueprint_registered_but_not_serving", "generic_find_replace"}


@brain_guardrails_bp.post("/api/v1/brain/auto-remediate")
def _auto_remediate():
    """Stage 2+4 consumer: take the single highest-priority actionable item
    (operator directive first, then findings the PR-opener can template) and
    open ONE review-PR for it — through the guardrail gate. Admin-gated.
    Idempotent-ish: respects the daily budget; humans still merge.

    This is intentionally ONE item per call: a cron fires it on a cadence, so
    the daily-budget cap (not a loop) bounds how much the brain proposes."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin auth required"), 401

    allowed, why = can_open_pr()
    if allowed is False and request.args.get("dry_run") not in ("1", "true"):
        return jsonify(ok=False, error="autonomy_gate_closed", reason=why), 429

    # 1) Prefer an open operator directive that targets a file (Stage 4).
    candidate = None
    try:
        if _store is not None:
            for d in _store.list_directives(status="open", limit=5):
                # Only auto-PR directives that are a concrete find/replace
                # (carry file+find+replace). Free-form "build X" directives
                # need codegen — left for a human / future Layer-5 wiring.
                if d.get("target") and d.get("find") and d.get("replace") is not None:
                    candidate = {"issue": "generic_find_replace",
                                 "file": d["target"], "find": d["find"],
                                 "replace": d["replace"],
                                 "detail": d.get("directive", ""),
                                 "_directive_id": d.get("id")}
                    break
    except Exception:
        pass

    if candidate is None:
        return jsonify(ok=True, acted=False,
                       reason="no auto-PR-able item in queue",
                       note=("Directives need file+find+replace to auto-PR; "
                             "free-form directives await Layer-5 codegen."),
                       gate=status()), 200

    if request.args.get("dry_run") in ("1", "true"):
        return jsonify(ok=True, acted=False, dry_run=True,
                       would_open_pr_for=candidate, gate=status()), 200

    # 2) Hand off to the PR-opener (which re-checks the gate + records budget).
    try:
        import urllib.request, json as _json
        base = os.environ.get("INTERNAL_BASE_URL",
                              "https://dchub-backend-production.up.railway.app")
        body = _json.dumps(candidate).encode()
        req = urllib.request.Request(
            f"{base}/api/v1/brain/open-pr-for-finding",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Admin-Key": (os.environ.get("DCHUB_ADMIN_KEY")
                                     or os.environ.get("BRAIN_ADMIN_KEY") or "")})
        with urllib.request.urlopen(req, timeout=40) as r:
            out = _json.loads(r.read().decode("utf-8", "ignore"))
        # Stage 3: a directive whose PR opened moves open → in_progress, so
        # the verifier knows to watch it (and we don't re-PR it next cycle).
        if out.get("ok") and candidate.get("_directive_id") and _store is not None:
            try:
                _store.set_directive_status(
                    candidate["_directive_id"], "in_progress",
                    notes=f"auto-PR opened: {out.get('pr_url','?')}")
            except Exception:
                pass
        return jsonify(ok=True, acted=bool(out.get("ok")),
                       pr=out, gate=status()), 200
    except Exception as e:
        return jsonify(ok=False, error=f"open-pr handoff failed: {e}"), 502


@brain_guardrails_bp.post("/api/v1/brain/draft-from-directive")
def _draft_from_directive():
    """Stage 2/4 capstone: take a free-form open directive and have Claude
    draft a concrete (file, find, replace) proposal that goes through the
    same generic_find_replace review-PR pipeline. Admin-gated. Guarded by
    can_open_pr() (kill switch + daily change budget). Validates the model's
    output against the live file BEFORE handing off — file must exist, `find`
    must be present and unique. Humans still merge every PR.

    Body: {"directive_id": <int>}  (or "id"). Optionally {"dry_run": true}.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin auth required"), 401
    body = request.get_json(silent=True) or {}
    did = body.get("directive_id") or body.get("id")
    if not did:
        return jsonify(ok=False, error="directive_id required"), 400

    ok_gate, why = can_open_pr()
    if not ok_gate and not body.get("dry_run"):
        return jsonify(ok=False, error="autonomy_gate_closed", reason=why), 429

    if _store is None:
        return jsonify(ok=False, error="store unavailable"), 503

    # Fetch the directive — must be open + free-form (no find/replace yet).
    target_directive = None
    for d in _store.list_directives(status="open", limit=200):
        if int(d.get("id") or 0) == int(did):
            target_directive = d
            break
    if not target_directive:
        return jsonify(ok=False, error=f"open directive {did} not found"), 404
    if target_directive.get("find") and target_directive.get("replace") is not None:
        return jsonify(ok=False,
                       error="directive already has find/replace — use auto-remediate"), 400

    directive_text = (target_directive.get("directive") or "").strip()
    target_path = (target_directive.get("target") or "").strip().lstrip("/")
    if not directive_text:
        return jsonify(ok=False, error="directive text is empty"), 400

    # Optional: include the target file content as Claude context.
    file_excerpt = ""
    if target_path:
        try:
            from routes.brain_pr_opener import _get_file
            body_text, _sha = _get_file(target_path)
            if body_text:
                file_excerpt = body_text[:6000]  # cap so we don't blow the context
        except Exception:
            pass

    # Strict-JSON prompt. The validators downstream catch any ambiguity, but
    # we prime the model heavily because a clear refusal is far better than a
    # wrong guess on autonomous code generation.
    system = (
        "You are DC Hub's Layer-5 code drafter. Convert an operator directive "
        "into ONE deterministic single-file find→replace edit that opens as a "
        "review PR (a human merges). NEVER produce free-form code unless you "
        "can express it as an EXACT, UNAMBIGUOUS single-string substitution.\n\n"
        "Output STRICTLY a JSON object with exactly these keys:\n"
        "  {\"file\": \"...\", \"find\": \"...\", \"replace\": \"...\", "
        "\"rationale\": \"...\"}\n"
        "OR, if you cannot safely produce such an edit:\n"
        "  {\"refuse\": true, \"rationale\": \"...\"}\n\n"
        "Rules — REFUSE if any are violated:\n"
        "  • `find` MUST be present LITERALLY in the target file and appear "
        "    EXACTLY ONCE (we will verify; ambiguous fails closed).\n"
        "  • `find` MUST be at least 24 characters (no tiny matches).\n"
        "  • Path MUST be repo-relative (no '..', no leading '/').\n"
        "  • Do NOT propose changes to secrets, .env, CI workflows, or "
        "    anything touching auth, billing, or payment flows.\n"
        "  • If the directive is research or scaffolding (no clear single-edit "
        "    expression), REFUSE — a human will handle it."
    )
    excerpt_block = (f"\n\nTARGET FILE `{target_path}` (first 6KB):\n```\n"
                     f"{file_excerpt}\n```" if file_excerpt else "")
    prompt = (f"DIRECTIVE #{did}: {directive_text}{excerpt_block}\n\n"
              f"Produce the JSON object now. No commentary outside the JSON.")

    try:
        from routes.brain_v2_layer4 import _call_claude
    except Exception as e:
        return jsonify(ok=False, error=f"cannot import _call_claude: {e}"), 503
    text, err = _call_claude(prompt, system)
    if err or not text:
        return jsonify(ok=False, error=f"claude call failed: {err}"), 502

    # Parse JSON — accept either the proposal or an explicit refusal.
    import json as _json, re as _re
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not m:
        return jsonify(ok=False, error="no JSON in model output",
                       model_text=text[:400]), 422
    try:
        prop = _json.loads(m.group(0))
    except Exception as e:
        return jsonify(ok=False, error=f"JSON parse failed: {e}",
                       model_text=text[:400]), 422
    if prop.get("refuse"):
        return jsonify(ok=True, acted=False, refused=True,
                       rationale=prop.get("rationale", "")), 200

    # Hand the proposal to the PR-opener's generic_find_replace (it re-runs
    # the same validators: file exists, find present + unique, etc., and
    # checks the autonomy gate again before committing).
    if body.get("dry_run"):
        return jsonify(ok=True, dry_run=True, proposal=prop, gate=status()), 200

    payload = {
        "issue":  "generic_find_replace",
        "file":   prop.get("file") or target_path,
        "find":   prop.get("find"),
        "replace": prop.get("replace", ""),
        "detail": f"Layer-5 codegen for directive #{did}: {directive_text[:200]}",
    }
    try:
        import urllib.request
        base = os.environ.get("INTERNAL_BASE_URL",
                              "https://dchub-backend-production.up.railway.app")
        req = urllib.request.Request(
            f"{base}/api/v1/brain/open-pr-for-finding",
            data=_json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Admin-Key": (os.environ.get("DCHUB_ADMIN_KEY")
                                     or os.environ.get("BRAIN_ADMIN_KEY") or "")})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = _json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return jsonify(ok=False, error=f"open-pr handoff failed: {e}"), 502

    # On success, mark directive in_progress so the verifier loop watches it.
    if out.get("ok"):
        try:
            _store.set_directive_status(
                int(did), "in_progress",
                notes=f"Layer-5 drafted PR {out.get('pr_url','?')}")
        except Exception:
            pass

    return jsonify(ok=True, acted=bool(out.get("ok")),
                   proposal=prop, pr=out, gate=status()), 200


@brain_guardrails_bp.post("/api/v1/brain/verify-directives")
def _verify_directives():
    """Stage 3 verification loop. For each in_progress directive carrying a
    find/replace, fetch the target file from `main` and check the `find` string
    is GONE (i.e. the PR merged + fix applied). If so → mark done (verified).
    If still present, leave it (PR not merged yet / regressed). Admin-gated.

    This is what lets the brain TRUST its own fixes: an action isn't "done"
    until the live file proves it, not when the PR was merely opened."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin auth required"), 401
    if _store is None:
        return jsonify(ok=False, error="store unavailable"), 503
    try:
        from routes.brain_pr_opener import _get_file
    except Exception as e:
        return jsonify(ok=False, error=f"cannot import _get_file: {e}"), 503

    verified, still_open, errors = [], [], []
    for d in _store.list_directives(status="in_progress", limit=50):
        find = d.get("find")
        path = d.get("target")
        if not (find and path):
            continue
        try:
            body, _sha = _get_file(path, ref="main")
            if body is None:
                errors.append({"id": d.get("id"), "path": path, "err": "unreadable"})
                continue
            if find not in body:
                _store.set_directive_status(
                    d["id"], "done",
                    notes="verified: find-string absent from main (fix merged)")
                verified.append(d.get("id"))
            else:
                still_open.append(d.get("id"))
        except Exception as e:
            errors.append({"id": d.get("id"), "err": str(e)[:120]})

    return jsonify(ok=True, verified=verified, verified_count=len(verified),
                   still_in_progress=still_open, errors=errors), 200
