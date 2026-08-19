"""
brain_review_lane.py — the HUMAN-REVIEW actuation lane (2026-08-19).

WHY THIS EXISTS
───────────────
Measured on 2026-08-19: the autonomy tick ran armed every 30 minutes and
opened ZERO draft PRs, every tick, for weeks. `/api/v1/brain/squasher.json`
self-reported `AMBER — "No fix landed in 7d (last: never)"`. The cause was not
a broken loop — the loop is healthy. It is that `classify_mechanical` requires
a match against **6 named SQL/datetime transform classes** (bool_is_active,
immutable_index, interval_literal, now_text_cast, sqlite_datetime_on_pg,
tz_naive_utcnow), and the proposals the brain actually generates are Python
logic fixes. The two populations do not intersect, so:

    draft_prs.opened = []  →  automerge merged = []  →  canary evaluated = []

The automerge loop was never starved by a bug; the stage upstream of it opened
nothing. Widening the automerge allowlist would "fix" the zero by letting
unreviewed Python logic changes merge themselves — the opposite of what the
PROPOSE-ONLY safety line in reference_dchub_autonomy_core exists to protect.

WHAT THIS DOES INSTEAD
──────────────────────
It opens a DRAFT PR for the proposals whose ONLY remaining blocker is the
absence of a *named* class — and for nothing else.

★ THE CENTRAL CLAIM: `blocked_by == ["no allowlist transform class matched"]`
means every OTHER mechanical gate PASSED:

  · single file, single contiguous hunk        (rule 1a)
  · replace differs from search, not a no-op   (rule 1b)
  · <= MECH_MAX_LINES changed lines            (rule 1c)
  · search_text non-trivial AND occurs exactly once in the live file  (rule 2)
  · NO new control-flow keyword / import / call name introduced       (rule 3)
  · file path not in the forbidden set         (rule 4)
  · confidence >= MECH_MIN_CONF                (rule 6)

The gap is a LABELLING gap, not a safety gap. Every substantive property the
mechanical lane relies on still holds; what is missing is a human-audited name
for the transform. So a human names it — by reading the PR.

SAFETY — why this can never auto-merge
──────────────────────────────────────
★ Branches here are `brain/review-*`. `brain_automerge` is hard-scoped to
`AUTOFIX_BRANCH_PREFIX = "brain/autofix-"`: it only ever LISTS autofix branches
(brain_automerge.py ~118), carries an explicit `continue` INVARIANT on any
other ref (~144), and skips `not_autofix_branch` in the merge pass (~764).
Even if a review branch reached that pass, it re-runs `classify_mechanical`
(~784), which returns is_mechanical=False for exactly these rows. Doubly
ineligible, by construction rather than by convention.

★ This module deliberately does NOT call `log_proposal_for_automerge()`. The
mechanical lane registers each branch in `brain_automerge_proposals` so the
merge pass can re-verify it; a review-lane branch is never registered, so the
merge pass cannot resolve metadata for it even by accident.

★ BACKPRESSURE IS ON OPEN PRs, NOT ON TIME. The tick runs every 30 minutes —
a per-run cap would still mint ~96 draft PRs/day and reproduce the L6 PR-flood
(reference_dchub_brain_l6_pr_flood). Instead the lane refuses to open anything
while BRAIN_REVIEW_LANE_MAX_OPEN (default 3) review PRs are already open. The
queue is therefore self-limiting: it cannot grow past the human's review
bandwidth, and it resumes the moment one is merged or closed.

Kill switch: BRAIN_REVIEW_LANE_ENABLED=0.
Real opening additionally requires DCHUB_L22_REAL_PR=1 AND a token, exactly
like the mechanical lane.
"""
from __future__ import annotations

import os


# ── The single blocker that defines this lane ────────────────────────
# Must match brain_mechanical_classifier.classify_mechanical rule 5 verbatim.
UNCLASSIFIED_BLOCKER = "no allowlist transform class matched"

REVIEW_BRANCH_PREFIX = "brain/review-"

# The klass recorded for dedup / provenance. NOT an allowlist class — it is
# deliberately absent from brain_mechanical_classifier's table so it can never
# satisfy the mechanical gate.
REVIEW_KLASS = "unclassified"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _enabled() -> bool:
    return os.environ.get("BRAIN_REVIEW_LANE_ENABLED", "1") not in (
        "0", "false", "False", "off")


def _classify(proposal: dict) -> dict:
    """Single seam onto the mechanical classifier.

    ★ Both entry points route through THIS function rather than importing
    classify_mechanical directly, so there is exactly one thing to patch. The
    direct-import version was patchable only via the string target
    "routes.brain_mechanical_classifier.classify_mechanical", which silently
    stops working once any earlier test in the suite stubs that module in
    sys.modules — monkeypatch then sets the attribute on a DIFFERENT module
    object than the one this code imports. Two tests here passed in isolation
    and failed in the full suite for exactly that reason."""
    from routes.brain_mechanical_classifier import classify_mechanical
    return classify_mechanical(proposal)


MAX_OPEN_REVIEW_PRS = lambda: _env_int("BRAIN_REVIEW_LANE_MAX_OPEN", 3)  # noqa: E731


# ── The gate ─────────────────────────────────────────────────────────
def is_unclassified_safe(verdict: dict) -> bool:
    """True when the ONLY thing between this proposal and the mechanical lane
    is the absence of a named transform class.

    ★ Deliberately an EXACT list comparison, not a substring test and not
    `blocked_by[0] ==`. A proposal blocked by the missing class AND anything
    else (too many lines, new control flow, ambiguous search, low confidence,
    forbidden path, sqlite-data guard) is NOT in this lane. Widening this
    predicate is how the lane would stop being safe, so it is written to fail
    closed on any additional blocker."""
    if not isinstance(verdict, dict):
        return False
    if verdict.get("is_mechanical"):
        return False          # already handled by the mechanical lane
    blocked = verdict.get("blocked_by")
    if not isinstance(blocked, list):
        return False
    return blocked == [UNCLASSIFIED_BLOCKER]


# ── Backpressure: how many review PRs are already open ───────────────
def count_open_review_prs() -> int:
    """Number of OPEN PRs whose head branch starts with brain/review-.

    Returns -1 when it cannot be determined (no token, API error). ★ The
    caller treats -1 as FAIL-CLOSED and opens nothing: an unknown queue depth
    must never be read as an empty one — that is how a flood starts."""
    try:
        from routes.brain_draft_pr_writer import _gh_config
        import requests
    except Exception:
        return -1
    cfg = _gh_config()
    token = cfg.get("token")
    if not token:
        return -1
    try:
        r = requests.get(
            f"https://api.github.com/repos/{cfg['upstream']}/pulls",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            params={"state": "open", "per_page": 100}, timeout=15,
        )
        if r.status_code != 200:
            return -1
        n = 0
        for pr in (r.json() or []):
            ref = ((pr or {}).get("head") or {}).get("ref") or ""
            if ref.startswith(REVIEW_BRANCH_PREFIX):
                n += 1
        return n
    except Exception:
        return -1


# ── Open ONE review draft PR ─────────────────────────────────────────
def open_review_draft_pr(proposal: dict, dry_run: bool = True) -> dict:
    """Apply ONE unclassified-but-structurally-safe proposal as a DRAFT PR.

    Mirrors open_mechanical_draft_pr's gate order deliberately, so the two
    lanes cannot drift apart: idempotency → re-classify → quarantine → cross
    proposal dedup → re-verify exactly-once on LIVE main → compute → open.
    The ONLY difference is which verdict is accepted, the branch prefix, and
    that nothing is registered for automerge."""
    from routes.brain_draft_pr_writer import (
        _already_opened, _diff_preview, _gh_config, _mark_dup_skipped,
        _mark_pr_opened, _short_hash, find_open_pr_for_file_class,
        get_file_on_main, open_draft_pr_with_content,
    )
    from routes.brain_mechanical_classifier import _extract_single_change

    if not _enabled():
        return {"ok": True, "skipped": True,
                "reason": "BRAIN_REVIEW_LANE_ENABLED=0",
                "id": proposal.get("id")}

    # ── Gate 0: idempotency. ─────────────────────────────────────────
    if _already_opened(proposal):
        return {"ok": True, "skipped": True, "reason": "already_pr_opened",
                "id": proposal.get("id"),
                "pr_url": (proposal.get("pr_url") or None)}

    # ── Gate 1: re-classify at apply time and REQUIRE the exact shape. ─
    verdict = _classify(proposal)
    if verdict.get("is_mechanical"):
        # Belongs to the mechanical lane; never double-open.
        return {"ok": True, "skipped": True, "reason": "is_mechanical",
                "id": proposal.get("id")}
    if not is_unclassified_safe(verdict):
        return {"ok": False, "aborted": True, "reason": "not_review_eligible",
                "blocked_by": verdict.get("blocked_by", [])[:6],
                "id": proposal.get("id")}

    file_path, search_text, replace_text, _multi = _extract_single_change(proposal)
    file_path = (file_path or "").strip()
    search_text = search_text or ""
    replace_text = replace_text or ""
    if not file_path or not search_text:
        return {"ok": False, "aborted": True, "reason": "missing_file_or_search",
                "id": proposal.get("id")}

    # ── Gate 1a: consume the janitor's quarantine, same as the mech lane. ─
    try:
        from routes.brain_fix_outcome_verify import is_recipe_quarantined
        if is_recipe_quarantined(file_path, REVIEW_KLASS):
            return {"ok": True, "skipped": True, "reason": "quarantined",
                    "id": proposal.get("id"), "file_path": file_path}
    except Exception:
        pass

    # ── Gate 1b: cross-proposal dedup on (file, unclassified). ────────
    try:
        existing = find_open_pr_for_file_class(
            file_path=file_path, klass=REVIEW_KLASS,
            exclude_id=proposal.get("id"))
    except Exception:
        existing = None
    if existing:
        try:
            _mark_dup_skipped(proposal.get("id"),
                              dup_of_pr_url=existing.get("pr_url") or "",
                              dup_of_id=existing.get("id"))
        except Exception:
            pass
        return {"ok": True, "skipped": True,
                "reason": "dup_open_pr_same_file_class",
                "id": proposal.get("id"), "file_path": file_path,
                "dup_of_id": existing.get("id"),
                "dup_of_pr_url": existing.get("pr_url")}

    # ── Gate 2: re-verify search occurs EXACTLY ONCE on LIVE main. ────
    fetched = get_file_on_main(file_path)
    if not fetched.get("ok"):
        return {"ok": False, "aborted": True,
                "reason": "fetch_main_failed:" + str(fetched.get("error")),
                "id": proposal.get("id")}
    content = fetched.get("content") or ""
    occ = content.count(search_text)
    if occ == 0:
        return {"ok": False, "aborted": True,
                "reason": "search_text_absent_in_main",
                "id": proposal.get("id")}
    if occ > 1:
        return {"ok": False, "aborted": True,
                "reason": f"search_text_ambiguous_in_main ({occ}x)",
                "id": proposal.get("id")}

    new_content = content.replace(search_text, replace_text, 1)
    if new_content == content:
        return {"ok": False, "aborted": True, "reason": "noop_replace",
                "id": proposal.get("id")}

    head_sha = fetched.get("head_sha")
    branch = (f"{REVIEW_BRANCH_PREFIX}{proposal.get('id')}-"
              f"{_short_hash(search_text + replace_text)}")
    preview = _diff_preview(content, search_text, replace_text)

    if dry_run:
        return {"ok": True, "would_open": True, "id": proposal.get("id"),
                "klass": REVIEW_KLASS, "branch": branch,
                "file_path": file_path, "head_sha": head_sha,
                "diff_preview": preview,
                "passed_gates": verdict.get("reasons", [])}

    # ── Gate 3: BACKPRESSURE — refuse while the review queue is full. ─
    # Checked here (not in dry_run) so the shadow surface still previews.
    open_now = count_open_review_prs()
    cap = MAX_OPEN_REVIEW_PRS()
    if open_now < 0:
        return {"ok": True, "skipped": True,
                "reason": "open_pr_count_unknown (fail-closed)",
                "id": proposal.get("id"), "branch": branch}
    if open_now >= cap:
        return {"ok": True, "skipped": True,
                "reason": f"review_queue_full ({open_now}/{cap} open)",
                "id": proposal.get("id"), "branch": branch}

    real_pr = os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"
    token = _gh_config()["token"]
    if not real_pr or not token:
        return {"ok": True, "skipped": True,
                "reason": ("DCHUB_L22_REAL_PR != 1" if not real_pr
                           else "no_token"),
                "id": proposal.get("id"), "branch": branch,
                "would_open": True, "diff_preview": preview}

    rationale = (proposal.get("rationale") or "").strip()
    gates = verdict.get("reasons", []) or []
    title = (f"[brain-review] {file_path} (proposal "
             f"#{proposal.get('id')}) — needs a human")
    commit_msg = (
        f"[brain-review] {file_path}\n\n"
        f"Single-hunk search->replace from brain proposal "
        f"#{proposal.get('id')} (loop {proposal.get('loop_name') or '?'}).\n\n"
        f"Rationale: {rationale or '(none provided)'}\n\n"
        f"NOT a recognised mechanical transform class - DRAFT, human review "
        f"required, never auto-merged.\n"
        f"Co-Authored-By: Brain review lane <l22-bot@dchub.cloud>"
    )
    body = (
        f"## What\n\n"
        f"Applies a single search→replace to `{file_path}`.\n\n"
        f"## Why this is a DRAFT and not an autofix\n\n"
        f"This proposal passed **every** mechanical safety gate except one: it "
        f"does not match any of the six audited transform classes, so no "
        f"machine can name what kind of change it is. That is a labelling gap, "
        f"not a safety gap — but naming it is a human's job, which is why this "
        f"PR exists instead of a merge.\n\n"
        f"Gates it PASSED at apply time:\n\n"
        + "".join(f"- {g}\n" for g in gates)
        + f"\nOnly blocker: `{UNCLASSIFIED_BLOCKER}`.\n\n"
        f"## Why\n\n{rationale or '(no rationale recorded on the proposal)'}\n\n"
        f"## Provenance\n\n"
        f"Auto-drafted from brain proposal **#{proposal.get('id')}** "
        f"(loop `{proposal.get('loop_name') or '?'}`, confidence "
        f"`{proposal.get('confidence')}`). Re-verified to occur exactly once "
        f"on `main` (HEAD `{head_sha}`).\n\n"
        f"## Diff preview\n\n```diff\n" + "\n".join(preview) + "\n```\n\n"
        f"## How to revert\n\n`git revert <merge-commit>` — single-hunk, "
        f"isolated.\n\n"
        f"---\n"
        f"> [!IMPORTANT]\n"
        f"> 🤖 brain **review lane** — DRAFT, human review required. The head "
        f"branch is `{REVIEW_BRANCH_PREFIX}*`, which the automerge pass is "
        f"hard-scoped to never touch (it only lists `brain/autofix-*`), and "
        f"this branch is deliberately NOT registered in "
        f"`brain_automerge_proposals`. It cannot merge itself.\n"
    )

    try:
        res = open_draft_pr_with_content(
            file_path=file_path, new_content=new_content, branch=branch,
            title=title, body=body, commit_msg=commit_msg,
        )
    except Exception as e:
        return {"ok": False, "aborted": True,
                "reason": f"pr_open_exception: {type(e).__name__}: {str(e)[:160]}",
                "id": proposal.get("id"), "branch": branch}
    if not res.get("ok"):
        return {"ok": False, "aborted": True,
                "reason": "pr_open_failed:" + str(res.get("stage") or res.get("error")),
                "id": proposal.get("id"), "branch": branch}

    pr_url = res.get("pr_url")
    try:
        db_ok = _mark_pr_opened(proposal.get("id"), pr_url or "")
    except Exception:
        db_ok = False

    # ★ NO log_proposal_for_automerge() here — see the module docstring.
    return {"ok": True, "pr_url": pr_url, "branch": res.get("branch") or branch,
            "id": proposal.get("id"), "klass": REVIEW_KLASS,
            "status_updated": db_ok}


# ── Batch driver ─────────────────────────────────────────────────────
def open_review_draft_prs(rows, *, apply: bool) -> dict:
    """Run the review lane over `rows`. Mirrors open_mechanical_draft_prs'
    contract: {enabled, dry_run, max_open, open_now, opened, previewed,
    skipped}. One bad proposal never breaks the others."""
    from routes.brain_draft_pr_writer import _already_opened, _mark_dup_skipped

    if not _enabled():
        return {"enabled": False, "dry_run": not apply, "opened": [],
                "previewed": [], "skipped": []}

    cap = MAX_OPEN_REVIEW_PRS()
    open_now = count_open_review_prs() if apply else None
    opened, previewed, skipped = [], [], []
    seen_files: dict = {}

    for row in (rows or []):
        try:
            verdict = _classify(row)
        except Exception as e:
            skipped.append({"id": row.get("id"),
                            "reason": f"classify_error: {str(e)[:120]}"})
            continue
        if not is_unclassified_safe(verdict):
            continue          # not this lane's business — stay quiet
        if _already_opened(row):
            skipped.append({"id": row.get("id"), "reason": "already_pr_opened"})
            continue
        fp = (row.get("file_path") or "").strip()
        if fp and fp in seen_files:
            try:
                _mark_dup_skipped(row.get("id"), dup_of_id=seen_files[fp])
            except Exception:
                pass
            skipped.append({"id": row.get("id"),
                            "reason": "dup_open_pr_same_file_class",
                            "dup_of_id": seen_files[fp]})
            continue
        # Budget is the number of OPEN review PRs, refreshed as we open.
        if apply and (open_now is None or open_now < 0 or open_now >= cap):
            skipped.append({
                "id": row.get("id"),
                "reason": (f"review_queue_full ({open_now}/{cap} open)"
                           if isinstance(open_now, int) and open_now >= 0
                           else "open_pr_count_unknown (fail-closed)")})
            continue
        try:
            res = open_review_draft_pr(row, dry_run=(not apply))
        except Exception as e:
            skipped.append({"id": row.get("id"),
                            "reason": f"writer_exception: {str(e)[:120]}"})
            continue
        if not res.get("ok"):
            skipped.append({"id": row.get("id"),
                            "reason": res.get("reason", "aborted")})
        elif res.get("skipped"):
            skipped.append({"id": row.get("id"),
                            "reason": res.get("reason", "skipped")})
        elif apply and res.get("pr_url"):
            if fp:
                seen_files.setdefault(fp, row.get("id"))
            if isinstance(open_now, int) and open_now >= 0:
                open_now += 1
            opened.append({"id": row.get("id"), "pr_url": res.get("pr_url"),
                           "branch": res.get("branch")})
        else:
            if fp:
                seen_files.setdefault(fp, row.get("id"))
            previewed.append({"id": row.get("id"), "branch": res.get("branch"),
                              "file": res.get("file_path"),
                              "diff_preview": res.get("diff_preview", []),
                              "passed_gates": res.get("passed_gates", [])})

    return {"enabled": True, "dry_run": not apply, "max_open": cap,
            "open_now": open_now, "opened": opened,
            "previewed": previewed, "skipped": skipped}
