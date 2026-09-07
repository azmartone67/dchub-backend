"""
brain_merge_reconciler.py — 2026-07-09. CREDIT half of the brain-PR loop.

The brain OPENS PRs (L5 mechanical autofixes on brain/autofix-*, spec/doc
proposals on brain-spec/*) and a human merges them on GitHub — but nothing
ever tells the brain's own ledger. /api/v1/brain/effectiveness read
"2026-07: total 6, merged 0, reviewed 0" the same day an operator merged
SEVEN brain-spec PRs (#1511–#1519). The fix-success signal the auto-merge
arm gate reads therefore under-counts every human-merged brain PR.

This reconciler closes the loop — read-only against GitHub, telemetry-only
writes against our own tables:

  1. LIST merged PRs (GitHub REST, state=closed+merged_at) whose head branch
     starts with brain-spec/ or brain/autofix- — the two prefixes the brain's
     writers construct (brain_pr_opener.open_spec_pr, brain_draft_pr_writer).
     A human-authored branch can never match.
  2. MATCH each to a brain_proposed_code_fixes row, strongest evidence first:
       a. autofix branch embeds the proposal id  (brain/autofix-<klass>-<id>-<hash>)
       b. stored pr_url equals the merged PR's URL
       c. CONSERVATIVE token match: the finding label parsed from the PR
          title ("Brain finding: <label>") appears in exactly ONE candidate
          row's rationale/recipe_key — ambiguity (0 or 2+) = no match.
     Matched rows get status='merged' + reviewed_at + pr_url + an evidence
     note. Unmatched brain PRs are BACKFILLED as an auditable row
     (loop_name='merge_reconciler_backfill') so spec PRs — which live only
     on GitHub — finally count in code_proposals_by_month.
  3. RECORD the operator's merge as a review decision (decision='approve',
     reviewer='github-merge') — a human merging IS a human review.
  4. VERIFY the outcome HONESTLY (brain_issue_janitor discipline — cite
     evidence, never fabricate resolution): parse the finding label from the
     PR title and read brain_issue_persistence:
       · finding re-seen AFTER merge            → still_broken=TRUE
       · live shortly before merge (≤RECENT_DAYS), not re-seen ≥GRACE_HOURS
         after merge                            → still_broken=FALSE
       · merge younger than GRACE_HOURS         → pending (retried next run)
       · label never tracked / already stale long before merge → NO outcome
         row — absence of the finding is not evidence the merge fixed it.
     Outcomes are written through brain_learning.record_proposal_outcome
     (the canonical brain_fix_outcomes writer) so the effectiveness and
     self-model surfaces pick them up with zero schema drift.

SAFETY INVARIANTS:
  · Only branches with the exact brain-spec/ or brain/autofix- prefixes are
    ever considered (defense-in-depth re-check per PR, like the PR janitor).
  · NEVER writes to GitHub — list-only. No merge, no close, no comment.
  · Fail-closed: GitHub list error or empty DB ⇒ zero writes that run.
  · Idempotent: a per-PR ledger row (brain_merge_reconciliation) is written
    once; later runs skip reconciled PRs (pending-grace rows are re-tried).
  · Per-run cap (BRAIN_MERGE_RECONCILER_MAX_PER_RUN, default 20) + kill
    switch BRAIN_MERGE_RECONCILER_DISABLE=1 (no deploy needed). Default ON.
  · Heartbeat-safe: self-throttles to one effective run per
    BRAIN_MERGE_RECONCILER_MIN_INTERVAL_MIN (default 120) unless ?force=1.

Endpoints:
  GET  /api/v1/brain/merge-reconciler/status — dry preview, never writes
  POST /api/v1/brain/merge-reconciler/run    — acts (admin-gated); ?dry=1 previews

Cron: routes/cron_heartbeat.py _DISPATCH 'brain_merge_reconciler' — 2x daily.
"""

import os
import re
import logging
import datetime as _dt

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_merge_reconciler_bp = Blueprint("brain_merge_reconciler", __name__)

_GITHUB_REPO = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()

# The ONLY branch prefixes the brain's PR writers construct. A PR whose head
# ref doesn't start with one of these is never touched (invariant).
_SPEC_PREFIX = "brain-spec/"
_AUTOFIX_PREFIX = "brain/autofix-"

# NOTE the revert exclusion: brain/autofix-revert-<orig_pr>-<ts> embeds a PR
# number (not a proposal id) where this regex expects the id, and a merged
# REVERT is the opposite of fix credit — reverts are skipped entirely.
_AUTOFIX_BRANCH_RE = re.compile(r"^brain/autofix-(?!revert-).+-(\d+)-[0-9a-f]{4,}$")
_REVERT_MARKER = "/autofix-revert-"
_SPEC_BRANCH_RE = re.compile(r"^brain-spec/([a-z0-9_]+)-(\d+)-")
# brain_pr_opener spec titles: "[brain-spec] agenda #76: [reliability] Brain
# finding: mcp_tool_zero_conversion @ /admin/per-too…" — the label token is
# what brain_issue_persistence keys on.
_FINDING_LABEL_RE = re.compile(r"Brain finding:\s*([A-Za-z0-9_.:\-]+)")

_BACKFILL_LOOP = "merge_reconciler_backfill"

# ★ 2026-09-02 — a merged DOC does not act on a QA RED. `media::item-links`
#   was RED for 142h while #3444 and #3494 (both [brain-spec] docs drafted from
#   QA-derived investigations #100399 / #100405) were each credited as its fix
#   through record_review_decision. The investigation an `inv #N` spec PR was
#   drafted from is looked up; when its question came off the QA super-user
#   board (derive_question's "observed from the <seat> seat on <surface>"
#   shape, or a dchub://qa-superuser/ finding url) the PR is recorded with
#   acted=False, NO review credit, and the fix verdict is left to the `qa:`
#   claim the intake minted (claim_ledger, brain_qa_superuser_intake).
_INV_REF_RE = re.compile(r"\binv\s*#\s*(\d+)")
_QA_ORIGIN_RE = re.compile(
    r"dchub://qa-superuser/|qa-superuser|QA super-user|"
    r"observed from the \S+ seat on ", re.I)
_QA_ORIGIN_STATE = "spec_doc_qa_red_ungraded"


def investigation_ref(title: str):
    """The `inv #N` an innovation-drafted spec PR names, or None."""
    m = _INV_REF_RE.search(title or "")
    return int(m.group(1)) if m else None


def text_derives_from_qa_red(*texts) -> bool:
    """Pure: does any of these texts carry a QA super-user origin marker?"""
    return any(_QA_ORIGIN_RE.search(str(t or "")) for t in texts)


def pr_derives_from_qa_red(cur, pr: dict) -> bool:
    """Does this PR's source investigation come off the QA board? Reads the
    brain_investigations row the title names; the title itself counts too.
    Fail-soft: an unreadable row is NOT a QA origin (no credit is withheld on
    a guess)."""
    if text_derives_from_qa_red(pr.get("title"), pr.get("branch")):
        return True
    inv = investigation_ref(pr.get("title"))
    if inv is None or cur is None:
        return False
    try:
        cur.execute("SELECT question, result_json::text FROM brain_investigations "
                    " WHERE id = %s", (inv,))
        row = cur.fetchone()
    except Exception:
        return False
    if not row:
        return False
    return text_derives_from_qa_red(row[0], (row[1] or "")[:20000])


def _token() -> str:
    """GH_TOKEN / PR_SUBMIT_TOKEN exist on Railway (task note); GITHUB_TOKEN
    is what brain_issue_janitor uses. Accept any, prefer the PR-writer's."""
    return (os.environ.get("PR_SUBMIT_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN") or "").strip()


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _disabled() -> bool:
    return os.environ.get("BRAIN_MERGE_RECONCILER_DISABLE", "") in (
        "1", "true", "True", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _lookback_days() -> int:
    return max(1, _env_int("BRAIN_MERGE_RECONCILER_LOOKBACK_DAYS", 30))


def _max_per_run() -> int:
    return max(0, _env_int("BRAIN_MERGE_RECONCILER_MAX_PER_RUN", 20))


def _grace_hours() -> int:
    """How long after merge before a not-re-seen finding may be called fixed
    (recurrence needs time to show — same idea as the janitor grace window)."""
    return max(1, _env_int("BRAIN_MERGE_RECONCILER_GRACE_HOURS", 24))


def _recent_days() -> int:
    """The finding must have been LIVE within this window before the merge for
    its disappearance to be creditable to the merge at all."""
    return max(1, _env_int("BRAIN_MERGE_RECONCILER_RECENT_DAYS", 14))


def _min_interval_min() -> int:
    return max(0, _env_int("BRAIN_MERGE_RECONCILER_MIN_INTERVAL_MIN", 120))


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(s):
    """GitHub ISO timestamp → aware datetime, or None."""
    if not s:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
#  GitHub primitive (module-level so tests monkeypatch it AND so the
#  disabled path can be asserted to make ZERO calls) — LIST-ONLY.
# ════════════════════════════════════════════════════════════════════
def list_merged_brain_prs(lookback_days: int) -> dict:
    """GET closed PRs into main; keep merged ones on brain-spec/ or
    brain/autofix- head branches within the lookback window.
    Returns {ok, prs:[{number, branch, title, html_url, merged_at,
    created_at, author}]}. Read-only. Never raises."""
    token = _token()
    if not token:
        return {"ok": False, "error": "no_token", "prs": []}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}", "prs": []}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    cutoff = _now() - _dt.timedelta(days=lookback_days)
    out, pages = [], 0
    try:
        while pages < 3:  # 300 recently-updated closed PRs ≫ any brain burst
            pages += 1
            r = requests.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/pulls",
                headers=headers,
                params={"state": "closed", "base": "main", "sort": "updated",
                        "direction": "desc", "per_page": 100, "page": pages},
                timeout=20)
            if r.status_code != 200:
                return {"ok": False,
                        "error": f"{r.status_code}:{r.text[:160]}", "prs": []}
            batch = r.json() or []
            if not batch:
                break
            stop = False
            for pr in batch:
                upd = _parse_ts(pr.get("updated_at"))
                if upd and upd < cutoff:
                    stop = True  # sorted by updated desc — rest is older
                    break
                merged_at = _parse_ts(pr.get("merged_at"))
                if not merged_at or merged_at < cutoff:
                    continue
                ref = ((pr.get("head") or {}).get("ref") or "")
                if not (ref.startswith(_SPEC_PREFIX)
                        or ref.startswith(_AUTOFIX_PREFIX)):
                    continue  # INVARIANT: brain branches only
                if _REVERT_MARKER in ref:
                    continue  # a merged revert is NOT fix credit
                out.append({
                    "number": pr.get("number"),
                    "branch": ref,
                    "title": pr.get("title") or "",
                    "html_url": pr.get("html_url") or "",
                    "merged_at": merged_at,
                    "created_at": _parse_ts(pr.get("created_at")),
                    "author": ((pr.get("user") or {}).get("login") or ""),
                })
            if stop or len(batch) < 100:
                break
        return {"ok": True, "prs": out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:160]}",
                "prs": []}


# ════════════════════════════════════════════════════════════════════
#  DB — brain_learning conventions: short-lived autocommit connection,
#  simple statements (no SAVEPOINTs), every step fail-soft.
# ════════════════════════════════════════════════════════════════════
def _conn():
    import psycopg2
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    c = psycopg2.connect(url, connect_timeout=8)
    c.autocommit = True
    return c


def _ensure_schema(cur) -> None:
    """Per-PR reconciliation ledger — the idempotency backbone."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brain_merge_reconciliation (
            pr_number            BIGINT PRIMARY KEY,
            branch               TEXT,
            merged_at            TIMESTAMPTZ,
            matched_proposal_id  BIGINT,
            match_method         TEXT,
            issue_label          TEXT,
            outcome_state        TEXT,
            still_broken         BOOLEAN,
            evidence             TEXT,
            reconciled_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")


def parse_finding_label(title: str):
    """Spec titles are capped at 70 chars by the opener, so a label can be
    cut mid-token ('page_content_drift:' in the live data) — strip trailing
    separators; a truncated label simply won't exact-match persistence and
    the outcome path stays honest (no_evidence)."""
    m = _FINDING_LABEL_RE.search(title or "")
    if not m:
        return None
    return m.group(1).rstrip(":.-") or None


def match_proposal(cur, pr: dict):
    """Return (proposal_id, method, detail) or (None, 'unmatched', why).
    Strongest evidence first; the token path REQUIRES a unique hit."""
    branch, number = pr["branch"], pr["number"]

    # a. autofix branch embeds the proposal id (brain_draft_pr_writer:624).
    m = _AUTOFIX_BRANCH_RE.match(branch)
    if m:
        pid = int(m.group(1))
        cur.execute("SELECT id FROM brain_proposed_code_fixes WHERE id = %s",
                    (pid,))
        if cur.fetchone():
            return pid, "autofix_branch_id", f"id {pid} embedded in branch"

    # b. the writer stored this PR's URL on the row.
    cur.execute("""
        SELECT id FROM brain_proposed_code_fixes
         WHERE pr_url = %s OR pr_url ~ %s
         ORDER BY id LIMIT 2""",
        (pr["html_url"], f"/pull/{number}$"))
    rows = cur.fetchall()
    if len(rows) == 1:
        return int(rows[0][0]), "pr_url", "stored pr_url matches"

    # c. conservative token match on the finding label — unique hit only.
    label = parse_finding_label(pr["title"])
    if label and len(label) >= 6:  # short tokens match everything — refuse
        cur.execute("""
            SELECT id FROM brain_proposed_code_fixes
             WHERE proposed_at > NOW() - INTERVAL '60 days'
               AND status NOT IN ('merged', 'rejected', 'reverted', 'dismissed')
               AND (rationale ILIKE %s OR recipe_key ILIKE %s)
             ORDER BY id LIMIT 3""",
            (f"%{label}%", f"%{label}%"))
        cands = cur.fetchall()
        if len(cands) == 1:
            return (int(cands[0][0]), "finding_token",
                    f"label '{label}' unique in 1 candidate row")
        if len(cands) > 1:
            return None, "unmatched", f"label '{label}' ambiguous ({len(cands)} rows)"
    return None, "unmatched", "no row derivable from branch/url/label"


def decide_outcome(merged_at, last_seen_at, now, grace_hours, recent_days,
                   noun="merge"):
    """Honest outcome verdict for a finding vs an applied fix. Pure —
    unit-tested. `noun` names the applied event in the evidence text
    ("merge" for PR merges — the default; brain_learning.probe_outcomes
    passes "action" so autopilot actions are verified with the SAME
    grace/dormancy discipline instead of a second hand-rolled check).
    Returns (state, still_broken, evidence):
      state ∈ pending_grace | outcome (still_broken set) | no_evidence"""
    if merged_at is None:
        return "no_evidence", None, f"{noun} timestamp unavailable"
    age_h = (now - merged_at).total_seconds() / 3600.0
    if age_h < grace_hours:
        return ("pending_grace", None,
                f"{noun} {age_h:.1f}h ago < {grace_hours}h grace — too early to judge")
    if last_seen_at is None:
        return ("no_evidence", None,
                "finding label never tracked in brain_issue_persistence — "
                "cannot verify, refusing to fabricate")
    if last_seen_at > merged_at:
        return ("outcome", True,
                f"finding re-seen {last_seen_at.isoformat()} AFTER {noun} "
                f"{merged_at.isoformat()} — fix did not hold")
    dormant_d = (merged_at - last_seen_at).total_seconds() / 86400.0
    if dormant_d > recent_days:
        return ("no_evidence", None,
                f"finding already dormant {dormant_d:.1f}d before {noun} "
                f"(>{recent_days}d) — its absence is not creditable to this {noun}")
    return ("outcome", False,
            f"finding live {dormant_d:.1f}d before {noun}, not re-seen "
            f"≥{grace_hours}h after {noun} {merged_at.isoformat()}")


def _last_seen(cur, label):
    cur.execute("""
        SELECT MAX(last_seen_at) FROM brain_issue_persistence
         WHERE issue_label = %s""", (label,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None


# ── write primitives (module-level for test sentinels) ───────────────
def mark_proposal_merged(cur, pid, pr) -> bool:
    cur.execute("""
        UPDATE brain_proposed_code_fixes
           SET status = 'merged',
               reviewed_at = COALESCE(reviewed_at, %s),
               pr_url = COALESCE(NULLIF(pr_url, ''), %s),
               reviewer_note = COALESCE(reviewer_note, '')
                   || %s
         WHERE id = %s AND status IS DISTINCT FROM 'merged'""",
        (pr["merged_at"], pr["html_url"],
         f" [merge-reconciler: PR #{pr['number']} merged "
         f"{pr['merged_at'].isoformat()}]", pid))
    return cur.rowcount > 0


def backfill_proposal_row(cur, pr):
    """A merged brain-spec PR that lived only on GitHub — record it as an
    auditable proposal row so it finally counts. loop_name marks provenance;
    reversible with one DELETE on that loop_name."""
    cur.execute("""
        INSERT INTO brain_proposed_code_fixes
            (loop_name, file_path, search_text, replace_text, rationale,
             model, status, proposed_at, reviewed_at, pr_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (loop_name, file_path, search_text) DO NOTHING
        RETURNING id""",
        (_BACKFILL_LOOP, f"github:{pr['branch']}", pr["branch"], "",
         (f"Backfilled from merged GitHub PR #{pr['number']}: "
          f"{pr['title']}")[:500],
         "github-merge-reconciler", "merged",
         pr["created_at"] or pr["merged_at"], pr["merged_at"],
         pr["html_url"]))
    row = cur.fetchone()
    return int(row[0]) if row else None


def record_review_decision(pid, label, pr) -> bool:
    """The operator's GitHub merge IS a human approval — record it through
    the canonical brain_learning tables so human_reviews_30d sees it."""
    try:
        from routes.brain_learning import issue_hash, _conn as _blconn
        with _blconn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_review_decisions
                    (proposal_kind, proposal_id, issue_hash, issue_label,
                     decision, reviewer, reviewer_note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING""",
                ("code", pid, issue_hash(label or pr["branch"]),
                 (label or "")[:200], "approve",
                 # list API exposes the AUTHOR, not who merged — stay neutral
                 "github-merge",
                 (f"PR #{pr['number']} (author {pr['author'] or '?'}) merged "
                  f"on GitHub — {pr['html_url']}")[:500]))
        return True
    except Exception as e:
        logger.warning("[merge-reconciler] review-decision write failed: %s", e)
        return False


def record_outcome(pid, still_broken, evidence, pr) -> bool:
    """Route through brain_learning.record_proposal_outcome — the canonical
    brain_fix_outcomes writer (wrong-column INSERTs fail silent; never
    hand-roll a second writer)."""
    try:
        from routes.brain_learning import record_proposal_outcome
        return bool(record_proposal_outcome(
            pid, "code", still_broken,
            evidence_url=pr["html_url"], evidence_note=evidence))
    except Exception as e:
        logger.warning("[merge-reconciler] outcome write failed: %s", e)
        return False


def _upsert_ledger(cur, pr, pid, method, label, state, still_broken, evidence):
    cur.execute("""
        INSERT INTO brain_merge_reconciliation
            (pr_number, branch, merged_at, matched_proposal_id, match_method,
             issue_label, outcome_state, still_broken, evidence, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
        ON CONFLICT (pr_number) DO UPDATE SET
            matched_proposal_id = EXCLUDED.matched_proposal_id,
            match_method = EXCLUDED.match_method,
            issue_label = EXCLUDED.issue_label,
            outcome_state = EXCLUDED.outcome_state,
            still_broken = EXCLUDED.still_broken,
            evidence = EXCLUDED.evidence,
            updated_at = NOW()""",
        (pr["number"], pr["branch"], pr["merged_at"], pid, method,
         label, state, still_broken, (evidence or "")[:500]))


# ════════════════════════════════════════════════════════════════════
#  Core pass
# ════════════════════════════════════════════════════════════════════
def run_reconciliation(dry: bool = False) -> dict:
    """One reconciliation pass. dry=True: read-only preview (no DDL, no
    writes, no ledger). Never raises."""
    report = {"ok": True, "dry": dry, "repo": _GITHUB_REPO,
              "reconciled": [], "pending": [], "skipped": [], "errors": []}
    if _disabled():
        report.update(ok=False, disabled=True,
                      error="BRAIN_MERGE_RECONCILER_DISABLE=1")
        return report

    listing = list_merged_brain_prs(_lookback_days())
    if not listing.get("ok"):
        # FAIL CLOSED — a GitHub error must never be read as "nothing merged".
        report.update(ok=False, error=f"github_list: {listing.get('error')}")
        return report
    prs = listing["prs"]
    report["merged_brain_prs_in_window"] = len(prs)
    if not prs:
        return report

    c = None
    try:
        c = _conn()
        if c is None:
            report.update(ok=False, error="db_unavailable")
            return report
        cur = c.cursor()
        if not dry:
            _ensure_schema(cur)

        # Which PRs still need work? (done = ledger row in a terminal state)
        done, pending_retry = set(), set()
        try:
            cur.execute("""
                SELECT pr_number, outcome_state
                  FROM brain_merge_reconciliation""")
            for n, st in cur.fetchall():
                (pending_retry if st == "pending_grace" else done).add(int(n))
        except Exception:
            c.rollback()  # table absent (first dry run) — treat all as new

        now, cap = _now(), _max_per_run()
        acted = 0
        for pr in sorted(prs, key=lambda p: p["merged_at"]):
            ref = pr["branch"]
            if (not (ref.startswith(_SPEC_PREFIX)
                     or ref.startswith(_AUTOFIX_PREFIX))
                    or _REVERT_MARKER in ref):
                continue  # defense-in-depth re-check
            if pr["number"] in done:
                report["skipped"].append(
                    {"pr": pr["number"], "why": "already reconciled"})
                continue
            if acted >= cap:
                report["skipped"].append(
                    {"pr": pr["number"], "why": f"per-run cap {cap}"})
                continue
            acted += 1
            retry = pr["number"] in pending_retry

            entry = {"pr": pr["number"], "branch": ref,
                     "merged_at": pr["merged_at"].isoformat(),
                     "retry": retry}
            try:
                pid, method, detail = match_proposal(cur, pr)
                entry.update(match=method, match_detail=detail)
                label = parse_finding_label(pr["title"])
                entry["issue_label"] = label

                is_spec = ref.startswith(_SPEC_PREFIX)
                qa_origin = is_spec and pr_derives_from_qa_red(cur, pr)
                # A doc never acts; a doc drafted from a QA RED must not even
                # be credited — the RED's own `qa:` claim is its verdict.
                entry["acted"] = False if is_spec else None
                entry["qa_red_origin"] = qa_origin

                marked = backfilled = False
                if not dry and not retry:
                    if pid is not None:
                        marked = mark_proposal_merged(cur, pid, pr)
                    else:
                        pid = backfill_proposal_row(cur, pr)
                        backfilled = pid is not None
                        if backfilled:
                            method = "backfill_insert"
                            entry.update(match=method,
                                         proposal_id=pid)
                    if pid is not None and not qa_origin:
                        record_review_decision(pid, label, pr)
                entry.update(proposal_id=pid, marked_merged=marked,
                             backfilled=backfilled)

                # Outcome — only where a matching finding exists.
                state, still_broken, evidence = "no_evidence", None, \
                    "no finding label in PR title"
                if label:
                    state, still_broken, evidence = decide_outcome(
                        pr["merged_at"], _last_seen(cur, label), now,
                        _grace_hours(), _recent_days())
                if state == "outcome" and ref.startswith(_SPEC_PREFIX):
                    # HONESTY (2026-07-11): a brain-spec PR adds a DOC only —
                    # zero code execution (brain_pr_opener.open_spec_pr). Its
                    # merge is CREDIT (review decision, effectiveness counts)
                    # but grading a standing detector against a document
                    # fabricates a fix verdict in BOTH directions: the
                    # detector re-firing says nothing about a doc, and its
                    # going quiet is not the doc's doing. Measured 07-10:
                    # 8 of the 9 code-kind brain_fix_outcomes rows were spec
                    # PRs auto-failed this way. Label it; write NO outcome.
                    state, still_broken = "spec_doc_ungraded", None
                    evidence = ("doc-only spec PR — merge credited, not "
                                "graded as a fix outcome; " + evidence)[:500]
                if qa_origin:
                    state, still_broken = _QA_ORIGIN_STATE, None
                    evidence = ("doc-only spec PR drafted from a QA super-user "
                                "RED — acted=False, no review credit; the RED's "
                                "own qa: fix claim is the verdict; " + evidence)[:500]
                entry.update(outcome_state=state, still_broken=still_broken,
                             evidence=evidence)
                if not dry:
                    if state == "outcome" and pid is not None:
                        if record_outcome(pid, still_broken, evidence, pr):
                            state = "outcome_recorded"
                            entry["outcome_state"] = state
                    _upsert_ledger(cur, pr, pid, method, label, state,
                                   still_broken, evidence)
                (report["pending"] if state == "pending_grace"
                 else report["reconciled"]).append(entry)
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                report["errors"].append(
                    {"pr": pr["number"], "error": f"{type(e).__name__}: {e}"[:200]})
        report["acted"] = acted
        report["qa_red_origin_uncredited"] = sum(
            1 for e in report["reconciled"] + report["pending"]
            if e.get("qa_red_origin"))
    except Exception as e:
        report.update(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
    return report


# ════════════════════════════════════════════════════════════════════
#  Endpoints
# ════════════════════════════════════════════════════════════════════
_LAST_RUN_TS = 0.0  # in-process throttle for the ~hourly heartbeat dispatch


@brain_merge_reconciler_bp.route(
    "/api/v1/brain/merge-reconciler/status", methods=["GET"])
def merge_reconciler_status():
    """Dry preview — lists merged brain PRs in window + what a run would do.
    Never writes."""
    rep = run_reconciliation(dry=True)
    rep["config"] = {
        "lookback_days": _lookback_days(), "max_per_run": _max_per_run(),
        "grace_hours": _grace_hours(), "recent_days": _recent_days(),
        "disabled": _disabled(), "token_present": bool(_token()),
    }
    return jsonify(rep), 200


@brain_merge_reconciler_bp.route(
    "/api/v1/brain/merge-reconciler/run", methods=["POST"])
def merge_reconciler_run():
    """Reconcile (admin-gated). ?dry=1 previews; ?force=1 skips the
    min-interval throttle (heartbeat fires ~hourly — dedup here)."""
    global _LAST_RUN_TS
    if _admin_key():
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _admin_key():
            return jsonify(ok=False, error="unauthorized"), 401
    dry = (request.args.get("dry") or "") in ("1", "true", "yes")
    force = (request.args.get("force") or "") in ("1", "true", "yes")
    import time as _t
    if not dry and not force and _min_interval_min() > 0:
        since_min = (_t.time() - _LAST_RUN_TS) / 60.0
        if _LAST_RUN_TS and since_min < _min_interval_min():
            return jsonify(ok=True, throttled=True,
                           minutes_since_last=round(since_min, 1),
                           min_interval_min=_min_interval_min()), 200
    rep = run_reconciliation(dry=dry)
    if not dry and rep.get("ok"):
        _LAST_RUN_TS = _t.time()
    return jsonify(rep), 200
