"""
Spec→code PROMOTION (2026-07-01) — the second rung of the spec-PR actuator.

The approve→PR path (brain_innovation_dashboard → brain_guardrails.
draft_and_open_pr) calls the Layer-5 drafter with NO target file, so on
anything non-trivial Claude correctly refuses and the directive lands as a
doc-only [brain-spec] draft PR (brain_pr_opener.open_spec_pr). A human then
reads prose, finds the file, writes the edit, merges. This module closes
that gap for the subset of specs that ARE a concrete single-file edit:

  1. list open [brain-spec] draft PRs (GitHub REST, read-only)
  2. pull each spec's markdown body from its branch
  3. extract candidate repo file paths from the spec text and verify each
     exists on live main (ground truth — never guess)
  4. re-run draft_and_open_pr WITH the grounded target_path + file excerpt
     — the missing context that forced the original refusal
  5. on success: a real code draft PR opens (human still merges — this
     changes WHAT the human reviews, not WHO decides), the spec PR gets a
     cross-link comment, and a brain_meta marker prevents re-promotion
  6. on refusal: marker with 'refused' — the spec stays for a human, and we
     never burn another LLM call on it

SAFETY: inherits EVERY rail of the drafter path — BRAIN_AUTONOMY_DISABLED
kill switch, BRAIN_DAILY_PR_CAP budget, single-file/exactly-once/24-char
validators, forbidden-path refusal rules, draft-only, never merges. Dark by
default: real runs need BRAIN_SPEC_PROMOTE_ENABLED=1; otherwise every call
is a dry-run preview. Per-run cap keeps it polite (default 2).
"""

import os
import re
import json
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_spec_promoter_bp = Blueprint("brain_spec_promoter", __name__)

try:
    from routes import brain_v2_store as _store
except Exception:            # pragma: no cover
    _store = None

_SPEC_PREFIX = "[brain-spec]"
_MARKER_PREFIX = "spec_promoted:"          # brain_meta key per spec-PR number
_MAX_PER_RUN_DEFAULT = 2

# Repo-relative code paths a spec plausibly names. Grounded against live
# main before use — a regex hit that doesn't exist on main is discarded.
_PATH_RE = re.compile(
    r"\b((?:routes|scripts|workflows|tests|static|templates|mcp-server)"
    r"/[A-Za-z0-9_.\-/]+\.(?:py|mjs|js|html|yml|yaml|md|txt)"
    r"|[A-Za-z0-9_\-]+\.py)\b")


def _flag_on() -> bool:
    return (os.environ.get("BRAIN_SPEC_PROMOTE_ENABLED") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _admin_ok() -> bool:
    key = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(key) and got == key


def _marker_get(pr_number: int) -> str | None:
    if _store is None:
        return None
    try:
        row = _store.get_meta(f"{_MARKER_PREFIX}{pr_number}")
        return (row or {}).get("value")
    except Exception:
        return None


def _marker_set(pr_number: int, value: str) -> None:
    if _store is None:
        return
    try:
        _store.set_meta(f"{_MARKER_PREFIX}{pr_number}", value[:200])
    except Exception:
        pass


_MODULE_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# snake_case tokens that are prose/domain vocabulary, not module names —
# don't burn GitHub lookups probing routes/<these>.py.
_TOKEN_STOPWORDS = {
    "single_file", "draft_pr", "auto_merge", "human_checklist", "follow_up",
    "high_level", "e_g", "end_to_end", "kill_switch", "rate_limit",
}


def _extract_candidate_paths(spec_text: str, cap: int = 5) -> list[str]:
    """Ordered unique repo-relative path candidates named in the spec.

    Two tiers: (1) explicit paths (routes/x.py); (2) bare snake_case module
    names — specs usually say "the mcp_funnel handler", not
    "routes/mcp_funnel.py" — mapped to routes/<token>.py. Both tiers are
    grounded against live main by the caller, so a wrong guess is discarded,
    never edited."""
    seen, out = set(), []
    for m in _PATH_RE.finditer(spec_text or ""):
        p = m.group(1).lstrip("/")
        if ".." in p or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= cap:
            return out
    for m in _MODULE_TOKEN_RE.finditer(spec_text or ""):
        tok = m.group(1)
        if tok in _TOKEN_STOPWORDS or len(tok) < 6:
            continue
        p = f"routes/{tok}.py"
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= cap:
            break
    return out


def _spec_body_for_pr(pr: dict) -> tuple[str, str | None]:
    """Fetch the spec markdown this PR adds (docs/brain-proposals/*.md on its
    head branch). Falls back to the PR body. Returns (text, doc_path)."""
    from routes.brain_pr_opener import _gh, _get_file
    num = pr.get("number")
    head_ref = ((pr.get("head") or {}).get("ref")) or ""
    try:
        r = _gh("GET", f"/repos/{os.environ.get('GITHUB_REPO', 'azmartone67/dchub-backend')}"
                       f"/pulls/{num}/files?per_page=20")
        if r.status_code == 200:
            for f in (r.json() or []):
                fn = f.get("filename") or ""
                if fn.startswith("docs/brain-proposals/") and fn.endswith(".md"):
                    content, _sha = _get_file(fn, ref=head_ref or "main")
                    if content:
                        return content, fn
    except Exception:
        pass
    return (pr.get("body") or ""), None


def promote_spec_prs(dry_run: bool = True,
                     cap: int = _MAX_PER_RUN_DEFAULT) -> dict:
    """One promotion pass. NEVER raises. Read-only unless dry_run=False AND
    BRAIN_SPEC_PROMOTE_ENABLED=1 (checked by the endpoint) — and even then
    every write goes through draft_and_open_pr's own gates."""
    report = {"ok": True, "dry_run": dry_run, "examined": 0, "promoted": [],
              "refused": [], "skipped": []}
    try:
        from routes.brain_pr_opener import _gh
        repo = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend")
        r = _gh("GET", f"/repos/{repo}/pulls?state=open&per_page=100")
        if r.status_code != 200:
            return {"ok": False, "error": f"list_prs {r.status_code}"}
        specs = [p for p in (r.json() or [])
                 if (p.get("title") or "").startswith(_SPEC_PREFIX)
                 and p.get("draft")]
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    from routes.brain_pr_opener import _get_file
    acted = 0
    for pr in specs:
        num = pr.get("number")
        if acted >= cap:
            report["skipped"].append({"pr": num, "reason": "run_cap"})
            continue
        report["examined"] += 1

        mark = _marker_get(num)
        if mark:
            report["skipped"].append({"pr": num, "reason": f"marker:{mark[:40]}"})
            continue

        spec_text, doc_path = _spec_body_for_pr(pr)
        if not (spec_text or "").strip():
            report["skipped"].append({"pr": num, "reason": "empty_spec"})
            continue

        # Ground the candidates: only paths that EXIST on live main qualify.
        target = None
        for cand in _extract_candidate_paths(spec_text):
            try:
                content, _sha = _get_file(cand)
            except Exception:
                content = None
            if content:
                target = cand
                break
        if not target:
            # Not promotable (no concrete file named) — leave for the human,
            # and don't re-examine every run.
            if not dry_run:
                _marker_set(num, "no_target_file")
            report["skipped"].append({"pr": num, "reason": "no_target_file"})
            continue

        if dry_run:
            report["promoted"].append({"pr": num, "target": target,
                                       "doc": doc_path, "preview_only": True})
            acted += 1
            continue

        try:
            from routes.brain_guardrails import draft_and_open_pr
            res = draft_and_open_pr(
                directive_text=spec_text[:4000],
                target_path=target,
                label=f"spec-promote PR#{num}",
                dry_run=False,
            )
        except Exception as e:
            report["skipped"].append({"pr": num,
                                      "reason": f"drafter_error:{str(e)[:100]}"})
            continue

        if res.get("refused"):
            _marker_set(num, "refused")
            report["refused"].append({"pr": num, "target": target,
                                      "rationale": (res.get("rationale") or "")[:200]})
            continue
        if not (res.get("ok") and res.get("acted")):
            # Transient (gate closed / claude error / PR-open failure):
            # NO marker — retry next run.
            report["skipped"].append({"pr": num, "target": target,
                                      "reason": str(res.get("error")
                                                    or res.get("reason")
                                                    or "not_acted")[:150]})
            continue

        acted += 1
        pr_out = res.get("pr") or {}
        code_pr_url = (pr_out.get("pr_url") or pr_out.get("url")
                       or json.dumps(pr_out)[:120])
        _marker_set(num, f"promoted:{code_pr_url}")
        report["promoted"].append({"pr": num, "target": target,
                                   "code_pr": code_pr_url})
        # Cross-link on the spec PR so the human sees the promotion.
        try:
            from routes.brain_pr_opener import _gh as _gh2
            _gh2("POST", f"/repos/{repo}/issues/{num}/comments",
                 {"body": (f"🤖 **Spec promoted to code**: the brain re-ran the "
                           f"Layer-5 drafter with grounded file context "
                           f"(`{target}`) and opened a concrete draft PR: "
                           f"{code_pr_url}\n\nReview the CODE PR; this spec "
                           f"stays as the rationale record.")})
        except Exception:
            pass

    return report


@brain_spec_promoter_bp.post("/api/v1/admin/brain/spec-promote/run")
def _run_spec_promote():
    """Admin/cron entrypoint. Dry-run unless BRAIN_SPEC_PROMOTE_ENABLED=1
    AND ?dry=0 — dark-by-default, preview-first (the pattern that caught the
    674-candidate utcnow sweep before it fired)."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    want_real = (request.args.get("dry") or "1").strip() == "0"
    dry = not (want_real and _flag_on())
    try:
        cap = int(request.args.get("cap") or _MAX_PER_RUN_DEFAULT)
    except Exception:
        cap = _MAX_PER_RUN_DEFAULT
    out = promote_spec_prs(dry_run=dry, cap=max(0, min(cap, 5)))
    out["flag_on"] = _flag_on()
    return jsonify(out), (200 if out.get("ok") else 500)
