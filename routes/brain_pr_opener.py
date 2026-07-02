"""
Phase ZZZZ-brain-pr-opener (2026-05-18) — Brain L1: detect → propose PR.

Closes the gap the user called out: "brain detects but doesn't auto-fix."
This endpoint takes a brain finding (issue type + url + diagnostic detail)
and opens a PR via the GitHub REST API with a proposed fix.

For now we support 3 fix types — the patterns brain has caught multiple
times this month and can be safely templated. Each fix template produces
a tiny, reviewable diff:

  • blueprint_registered_but_not_serving — move late-line registration
    into the safe zone at ~line 1180 of main.py
  • cron_endpoint_unscheduled — add a JOBS entry to dchub-scheduler.py
  • shadowed_route — comment out the second registration with a TODO

Each PR opens against a fresh branch `brain/fix-{issue}-{ts}` and is
ASSIGNED + LABELED so the operator notices.
"""

import os
import re
import json
import time
import base64
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_pr_opener_bp = Blueprint("brain_pr_opener", __name__)

_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
_GITHUB_REPO = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _gh(method: str, path: str, body=None):
    """Minimal GitHub REST client."""
    import requests
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dchub-brain-pr-opener/1.0",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    r = requests.request(method, url, headers=headers,
                         json=body, timeout=20)
    return r


def open_pr_exists(title: str) -> bool:
    """True if an OPEN PR with this exact title already exists. The brain's
    L5/L6 drafters re-propose the same ideas every cycle, piling up a 'draft
    graveyard' (28 stale drafts as of 2026-06-28). Call this before opening a
    draft so the same idea isn't re-drafted — the same dedupe fix that worked
    for the L23 capability proposals. Fail-open (returns False) on any error so
    a transient GitHub blip never silently blocks legitimate drafts."""
    t = (title or "").strip()
    if not t:
        return False
    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code == 200:
            return any((p.get("title") or "").strip() == t for p in r.json())
    except Exception:
        pass
    return False


_TITLE_STOPWORDS = frozenset((
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with",
    "via", "into", "from", "by", "at", "as", "is", "are", "be", "that",
))


def _title_tokens(title: str) -> set:
    """Lowercased alphanumeric tokens of a PR title, bracket prefix and
    stopwords stripped — the comparison basis for fuzzy title dedup."""
    t = re.sub(r"^\[[^\]]*\]\s*", "", (title or "").lower())
    return set(re.findall(r"[a-z0-9]{3,}", t)) - _TITLE_STOPWORDS


def titles_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """True when two PR titles share >= threshold of their tokens (measured
    against the shorter title). The L6 planner paraphrases the same theme
    slightly differently every run ("Self-serve checkout for MCP unlock" vs
    "MCP unlock self-serve checkout flow"), so exact-match dedup missed
    essentially every duplicate — token overlap catches the paraphrases."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


def open_similar_pr_exists(title: str, prefix: str = "",
                           threshold: float = 0.6) -> bool:
    """Fuzzy sibling of open_pr_exists: True if an OPEN PR whose title starts
    with `prefix` overlaps `title` by >= threshold. Fail-open like
    open_pr_exists — a GitHub blip never blocks a legitimate draft; the
    supersede pass in expire_stale_draft_prs catches any dup that slips
    through."""
    t = (title or "").strip()
    if not t:
        return False
    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code == 200:
            for p in r.json():
                pt = (p.get("title") or "").strip()
                if prefix and not pt.startswith(prefix):
                    continue
                if titles_overlap(t, pt, threshold):
                    return True
    except Exception:
        pass
    return False


def expire_stale_draft_prs(days: int = 5,
                           prefixes=("[brain-l5 draft]", "[brain-l6 strategic-draft]",
                                     "[brain-spec]")) -> dict:
    """Auto-close brain DRAFT PRs whose title starts with one of the brain
    prefixes — the drain half of the draft-graveyard fix. Two passes:

    1. SUPERSEDE (2026-07-02): when two open drafts under the same prefix
       have overlapping titles (token overlap >= 60%), close the OLDER one
       as superseded. The L6 planner re-paraphrased the same themes across
       runs (two 5-PR bursts on 07-02 alone), and nothing drained the
       overlap until the 7-day expiry.
    2. EXPIRE: close survivors older than `days` (default dropped 14 → 5 on
       2026-07-02 — a strategic draft nobody touched in 5 days is stale, and
       the brain re-proposes anything still worth doing).

    Only touches drafts (never a human-readied PR). Best-effort; returns a
    summary. Safe to cron daily."""
    import datetime as _dt
    closed, superseded, scanned = [], [], 0

    def _close(number: int, comment: str) -> bool:
        try:
            _gh("POST",
                f"/repos/{_GITHUB_REPO}/issues/{number}/comments",
                {"body": comment})
        except Exception:
            pass
        cr = _gh("PATCH", f"/repos/{_GITHUB_REPO}/pulls/{number}",
                 {"state": "closed"})
        return cr.status_code == 200

    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code != 200:
            return {"ok": False, "error": f"list {r.status_code}"}
        drafts = []
        for p in r.json():
            scanned += 1
            title = (p.get("title") or "")
            if not p.get("draft"):
                continue
            prefix = next((pre for pre in prefixes
                           if title.startswith(pre)), None)
            if prefix is None:
                continue
            try:
                created = _dt.datetime.fromisoformat(
                    (p.get("created_at") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            drafts.append((created, p["number"], title, prefix))

        # Pass 1 — supersede: newest-first so the freshest phrasing of each
        # theme survives and every older overlapping draft closes against it.
        kept = []   # (created, number, title, prefix)
        for created, number, title, prefix in sorted(drafts, reverse=True):
            winner = next((k for k in kept
                           if k[3] == prefix and titles_overlap(title, k[2])),
                          None)
            if winner is None:
                kept.append((created, number, title, prefix))
                continue
            if _close(number, (f"🤖 Auto-closed by the brain draft-PR janitor: "
                               f"superseded by the newer overlapping draft "
                               f"#{winner[1]} ({winner[2]!r}).")):
                superseded.append(number)
            else:
                kept.append((created, number, title, prefix))

        # Pass 2 — expire survivors older than the TTL.
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
        for created, number, title, prefix in kept:
            if created < cutoff:
                if _close(number, (f"🤖 Auto-closed by the brain draft-PR expiry: "
                                   f"draft sat unactioned for {days}+ days. The "
                                   f"brain re-proposes anything still worth doing.")):
                    closed.append(number)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"ok": True, "scanned": scanned, "closed": closed,
            "closed_count": len(closed), "superseded": superseded,
            "superseded_count": len(superseded), "older_than_days": days}


def _get_default_branch_sha() -> str | None:
    r = _gh("GET", f"/repos/{_GITHUB_REPO}/git/refs/heads/main")
    if r.status_code != 200: return None
    return ((r.json() or {}).get("object") or {}).get("sha")


def _get_file(path: str, ref: str = "main") -> tuple[str | None, str | None]:
    """Returns (content_decoded, sha). None if file doesn't exist."""
    r = _gh("GET", f"/repos/{_GITHUB_REPO}/contents/{path}?ref={ref}")
    if r.status_code != 200: return None, None
    j = r.json() or {}
    content_b64 = (j.get("content") or "").replace("\n", "")
    try:
        return base64.b64decode(content_b64).decode("utf-8"), j.get("sha")
    except Exception:
        return None, None


def _create_branch(branch_name: str, from_sha: str) -> bool:
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/git/refs",
            {"ref": f"refs/heads/{branch_name}", "sha": from_sha})
    return r.status_code in (200, 201)


def _commit_file(path: str, content: str, message: str,
                 branch: str, sha: str | None) -> bool:
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha: body["sha"] = sha
    r = _gh("PUT", f"/repos/{_GITHUB_REPO}/contents/{path}", body)
    return r.status_code in (200, 201)


def _open_pr(title: str, head: str, body: str) -> dict | None:
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/pulls",
            {"title": title, "head": head, "base": "main", "body": body})
    if r.status_code not in (200, 201): return None
    return r.json()


def open_spec_pr(directive: str, heading: str = "", kind: str = "item",
                 item_id=0, label: str = "") -> dict:
    """r-brain-loop (2026-06-30): the actuator FALLBACK that turns an approval
    into a shippable artifact. When draft_and_open_pr REFUSES a directive (it's a
    'build X / instrument Y / gather Z' PLAN, not a single-file find/replace), this
    captures the APPROVED directive as a DRAFT spec PR — a markdown file under
    docs/brain-proposals/ with the recommendation + a human checklist. It adds a
    DOC only (zero code execution), opens as a draft, and a human merges — so the
    approval becomes a visible, trackable, human-implementable PR instead of
    'recorded only'. Inherits can_open_pr() (kill switch + daily cap), dedups by
    title. NEVER raises — always returns a dict."""
    directive = (directive or "").strip()
    if not directive:
        return {"ok": False, "acted": False, "error": "empty directive"}
    try:
        from routes.brain_guardrails import can_open_pr
        ok_gate, why = can_open_pr()
        if not ok_gate:
            return {"ok": False, "acted": False, "error": "autonomy_gate_closed", "reason": why}
    except Exception:
        pass
    import re as _re, datetime as _dt
    _slug = (_re.sub(r"[^a-z0-9]+", "-", (heading or directive)[:48].lower())
             .strip("-") or "proposal")
    title = f"[brain-spec] {kind} #{item_id}: {(heading or directive)[:70]}"
    try:
        if open_pr_exists(title):
            return {"ok": True, "acted": False, "note": "spec PR already open (dedup)"}
    except Exception:
        pass
    base = _get_default_branch_sha()
    if not base:
        return {"ok": False, "acted": False, "error": "no base sha"}
    branch = f"brain-spec/{kind}-{item_id}-{_slug}"[:90]
    path = f"docs/brain-proposals/{kind}-{item_id}-{_slug}.md"
    content = (
        f"# Brain proposal — {(heading or directive[:80])}\n\n"
        f"> Auto-captured from an **approved** brain {kind} item (#{item_id}). The brain's\n"
        f"> recommendation couldn't be expressed as a single-file edit, so it's filed here\n"
        f"> as a spec for a human to implement (or close). **Draft PR — a human merges.**\n\n"
        f"_Filed {_dt.datetime.utcnow().isoformat()}Z · {label}_\n\n"
        f"## The approved recommendation\n\n{directive}\n\n"
        f"## Human checklist\n\n"
        f"- [ ] Confirm this is still worth doing\n"
        f"- [ ] Scope it to a concrete change (file(s) + approach)\n"
        f"- [ ] Implement + verify\n"
        f"- [ ] Or close this PR if superseded / not worth it\n"
    )
    if not _create_branch(branch, base):
        return {"ok": False, "acted": False, "error": "create_branch failed"}
    if not _commit_file(path, content, f"brain-spec: {(heading or directive)[:60]}",
                        branch, None):
        return {"ok": False, "acted": False, "error": "commit_file failed"}
    # Open as a DRAFT PR (draft=true so it can't be merged without a human flip).
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/pulls",
            {"title": title, "head": branch, "base": "main", "draft": True,
             "body": content[:4000]})
    if r.status_code not in (200, 201):
        return {"ok": False, "acted": False, "error": f"open_pr failed ({r.status_code})"}
    pr = r.json() or {}
    return {"ok": True, "acted": True, "spec_pr": True,
            "pr": {"number": pr.get("number"), "url": pr.get("html_url")}}


# ─── Fix templates ──────────────────────────────────────────────────────

def _fix_blueprint_silent_failure(finding: dict) -> tuple[str | None, str | None, str]:
    """For blueprint_registered_but_not_serving — relocate the import +
    register_blueprint pair into the safe zone after weekly_digest_bp.

    Returns (file_path, proposed_new_content, diff_summary). Returns None
    content if it can't safely automate the fix.
    """
    # Extract blueprint var name from finding.url like "main.py: register_blueprint(foo_bp)"
    m = re.search(r"register_blueprint\((\w+)\)", finding.get("url", ""))
    if not m: return None, None, "Could not parse blueprint name from finding"
    bp_var = m.group(1)

    main_py, _sha = _get_file("main.py")
    if not main_py: return None, None, "Could not read main.py"

    # Find any existing try/except wrap that registers this bp
    pattern = re.compile(
        r"try:\s*\n\s*from routes\.(\w+) import " + re.escape(bp_var)
        + r"\s*\n\s*app\.register_blueprint\(" + re.escape(bp_var)
        + r"\)\s*\n(?:\s*except[^\n]*\n(?:\s*[^\n]*\n)*?)?",
        re.MULTILINE)
    found = pattern.search(main_py)
    if not found:
        return None, None, f"Could not locate try/except for {bp_var} in main.py"
    module = found.group(1)

    # Find the safe-zone anchor (the weekly_digest_bp block)
    safe_anchor = ("    try:\n"
                   "        from routes.weekly_digest import weekly_digest_bp\n"
                   "        app.register_blueprint(weekly_digest_bp)")
    if safe_anchor not in main_py:
        return None, None, "Could not find safe-zone anchor"

    # Build the replacement
    new_block = (
        f"\n    # Phase brain-auto-relocate (2026-05-18): moved here by\n"
        f"    # brain_pr_opener after blueprint_registered_but_not_serving fired.\n"
        f"    try:\n"
        f"        from routes.{module} import {bp_var}\n"
        f"        app.register_blueprint({bp_var})\n"
        f"    except Exception as _bp_relo:\n"
        f"        import logging\n"
        f"        logging.getLogger(__name__).warning('{bp_var} wiring failed: %s', _bp_relo)\n"
    )
    new_main = main_py.replace(safe_anchor, safe_anchor + new_block, 1)
    # Remove the old (broken) registration block
    new_main = new_main.replace(found.group(0), "", 1)

    summary = (f"Moved `from routes.{module} import {bp_var}` + "
               f"`app.register_blueprint({bp_var})` from line ~unknown "
               f"(late-line zone) to ~line 1180 (safe zone next to "
               f"weekly_digest_bp).")
    return "main.py", new_main, summary


def _fix_generic_find_replace(finding: dict) -> tuple[str | None, str | None, str]:
    """Generic, deterministic single-file edit: apply an exact string
    find→replace to a named file. This is the same primitive Layer 4's
    healer already produces (brain_proposed_fixes.find/replace), now able to
    open a review PR for ANY file — not just main.py.

    Finding fields:
      file     — repo-relative path (required)
      find     — exact substring to replace (required, must be present + unique)
      replace  — replacement text (required; may be empty for deletion)

    Safety: refuses if `find` is missing, absent from the file, or appears
    more than once (ambiguous). Path is constrained to the repo (no '..',
    no leading '/'). PR is review-gated — humans merge.
    """
    path = (finding.get("file") or "").strip().lstrip("/")
    find = finding.get("find")
    replace = finding.get("replace", "")
    if not path or find is None:
        return None, None, "generic_find_replace needs 'file' and 'find'"
    if ".." in path or path.startswith("/"):
        return None, None, f"unsafe path: {path}"
    if not find:
        return None, None, "'find' must be a non-empty string"
    content, _sha = _get_file(path)
    if content is None:
        return None, None, f"could not read {path}"
    n = content.count(find)
    if n == 0:
        return None, None, f"'find' string not present in {path}"
    if n > 1:
        return None, None, f"'find' appears {n}× in {path} — ambiguous, refused"
    new_content = content.replace(find, replace, 1)
    if new_content == content:
        return None, None, "no-op edit (find == replace)"
    summary = (f"Applied a single exact find→replace in `{path}` "
               f"({len(find)}→{len(replace)} chars).")
    return path, new_content, summary


_FIX_HANDLERS = {
    "blueprint_registered_but_not_serving": _fix_blueprint_silent_failure,
    "generic_find_replace": _fix_generic_find_replace,
}


# ─── Endpoint ───────────────────────────────────────────────────────────

@brain_pr_opener_bp.route("/api/v1/brain/open-pr-for-finding", methods=["POST"])
def open_pr_for_finding():
    """Take a brain finding, open a PR with the proposed fix.

    POST body:
      { "issue": "blueprint_registered_but_not_serving",
        "url":   "main.py: register_blueprint(industry_pulse_bp)",
        "detail": "..." }

    Admin-gated. Returns the PR URL on success.
    """
    # Admin gate
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    if not _GITHUB_TOKEN:
        return jsonify(ok=False, error="GITHUB_TOKEN env var not set; cannot open PR"), 503

    # Stage-5 guardrails: kill switch + daily change budget. Every autonomous
    # PR passes through here. Manual callers can override with ?force=1.
    if request.args.get("force") not in ("1", "true", "yes"):
        try:
            from routes.brain_guardrails import can_open_pr
            _ok, _why = can_open_pr()
            if not _ok:
                return jsonify(ok=False, error="autonomy_gate_closed",
                               reason=_why), 429
        except Exception as _ge:
            return jsonify(ok=False, error=f"guardrail check failed: {_ge}"), 503

    finding = request.get_json(silent=True) or {}
    issue = finding.get("issue", "")
    fix_handler = _FIX_HANDLERS.get(issue)
    if not fix_handler:
        return jsonify(ok=False,
                       error=f"No fix template for issue type '{issue}'",
                       supported=list(_FIX_HANDLERS.keys())), 400

    file_path, new_content, summary = fix_handler(finding)
    if not new_content or not file_path:
        return jsonify(ok=False, error=f"Fix could not be templated: {summary}"), 422

    # Open the PR
    sha = _get_default_branch_sha()
    if not sha:
        return jsonify(ok=False, error="Could not read main branch SHA"), 503

    ts = int(time.time())
    issue_short = issue[:30]
    branch_name = f"brain/fix-{issue_short}-{ts}"
    if not _create_branch(branch_name, sha):
        return jsonify(ok=False, error=f"Could not create branch {branch_name}"), 503

    # Get current sha for the target file
    _cur, file_sha = _get_file(file_path, ref="main")
    if not file_sha:
        return jsonify(ok=False, error=f"Could not get sha for {file_path}"), 503

    if not _commit_file(file_path, new_content,
                         f"fix(brain): {issue} in {file_path}", branch_name, file_sha):
        return jsonify(ok=False, error=f"Could not commit fix to {file_path}"), 503

    pr_body = (
        f"## Brain auto-fix\n\n"
        f"**Issue:** `{issue}`\n"
        f"**Finding URL:** `{finding.get('url','?')}`\n\n"
        f"### What this PR does\n\n"
        f"{summary}\n\n"
        f"### Original finding\n\n"
        f"> {finding.get('detail', '(no detail)')[:500]}\n\n"
        f"---\n"
        f"_Auto-generated by `routes/brain_pr_opener.py`. Review carefully before merging — "
        f"this is L1 brain auto-remediation; humans hold the merge button._"
    )
    pr = _open_pr(
        title=f"[brain auto-fix] {issue}",
        head=branch_name,
        body=pr_body,
    )
    if not pr:
        return jsonify(ok=False,
                        error="PR creation failed (branch + commit succeeded)",
                        branch=branch_name), 503

    # Count this PR against today's budget (after success only).
    try:
        from routes.brain_guardrails import record_pr_opened
        _budget_used = record_pr_opened()
    except Exception:
        _budget_used = None

    return jsonify(
        ok=True,
        pr_url=pr.get("html_url"),
        pr_number=pr.get("number"),
        branch=branch_name,
        summary=summary,
        auto_prs_today=_budget_used,
    ), 200


@brain_pr_opener_bp.route("/api/v1/brain/pr-opener/health", methods=["GET"])
def health():
    return jsonify(
        ok=True,
        github_token_set=bool(_GITHUB_TOKEN),
        github_repo=_GITHUB_REPO,
        supported_fixes=list(_FIX_HANDLERS.keys()),
        note=("POST a finding dict to /api/v1/brain/open-pr-for-finding "
              "with X-Admin-Key header to auto-generate a fix PR."),
    ), 200
