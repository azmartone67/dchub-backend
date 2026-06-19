"""
brain_fix_outcome_verify.py — PHASE 3.6 (2026-06-19). FIX-OUTCOME VERIFIER.

The brain opens mechanical autofix DRAFT PRs (brain_draft_pr_writer), the
auto-merge engine (brain_automerge) can MERGE the green ones, and the janitor
(brain_pr_janitor) closes the stale RED ones + QUARANTINES the recipe. But ONE
loop was never closed: AFTER a mechanical fix LANDS on main, did it actually
RESOLVE the finding — or was it a no-op that left the anti-pattern in place?

This module answers exactly that, against GROUND TRUTH (the file on main), and
RECORDS the verdict for the calibration feed. It is RECORD-ONLY: verification
NEVER blocks a safe merge. A brain mechanical fix is a single
(file, search_text, replace_text) per
routes/brain_mechanical_classifier._extract_single_change.

  verify_fix_resolved(proposal_or_pr) -> {resolved, reason, ...}

  resolved is True/False/None:
    · True  — the anti-pattern (search_text) is GONE from the file on main AND
              the replacement (replace_text) is PRESENT. The fix landed and
              resolved the finding.
    · False — the anti-pattern is STILL present on main (the fix was a no-op /
              did not resolve, or drifted back). A wrong claim of resolution is
              worse than an honest "unresolved", so this is reported plainly.
    · None  — INDETERMINATE: the file is unreadable / no search_text on the
              proposal / we can't fetch main. NEVER guess — record unverified.

╔══════════════════════════════════════════════════════════════════════╗
║ SUPREME CONSTRAINT — RECORD-ONLY, NEVER BLOCKS A MERGE.                ║
║   BRAIN_FIX_VERIFY defaults to "0" (OFF). With it off, the optional    ║
║   call-site in the brain loop does NOTHING (no read, no record). The   ║
║   verify_fix_resolved() function itself is always safe to call — it is ║
║   read-only and never raises — but the brain only WIRES it when the    ║
║   flag is on, and even then it merely RECORDS verified vs unverified.  ║
╚══════════════════════════════════════════════════════════════════════╝

ALSO closes the QUARANTINE-CONSUME GAP. Today brain_pr_janitor.quarantine_recipe
WRITES status='quarantined' rows but NOTHING reads them, so a quarantined
(file, klass) recipe is NOT actually blocked from re-proposal. We add:

  is_recipe_quarantined(file_path, klass) -> bool
    Reads brain_proposed_code_fixes for a quarantined-status row on that
    (file_path, finding_class). Best-effort; False on any error so a DB hiccup
    never wrongly blocks a legit fix.

and wire ONE guarded check into
routes.brain_draft_pr_writer.open_mechanical_draft_pr so a quarantined
(file, klass) is skipped (reason 'quarantined') BEFORE a PR is opened. (The
wiring lives in the writer; this module owns the helper + the verifier.)

REUSE (single source of truth — do NOT re-derive):
  · routes.brain_mechanical_classifier._extract_single_change — pull the
    (file, search, replace) out of either proposal shape.
  · routes.brain_draft_pr_writer.get_file_on_main — read the file off main.
  · routes.brain_draft_pr_writer.get_logged_proposal_for_branch — recover the
    proposal behind an autofix PR branch (so a {head_ref:...} PR dict works).
  · routes.autopilot_outcomes — the calibration / outcomes feed (if present).

Heavy imports (flask, requests, psycopg2, the classifier's flask chain) are
LAZY so this module imports cleanly in a no-flask test env where every
primitive is monkeypatched.
"""
from __future__ import annotations

import os


# ── env helpers ──────────────────────────────────────────────────────
def _flag_on() -> bool:
    """BRAIN_FIX_VERIFY master switch for the OPTIONAL brain-loop call-site.
    Defaults OFF ("0"); only the exact string "1" enables. Read live (not at
    import) so an operator flip is honored. verify_fix_resolved() itself is
    always safe to call — this gate only governs the wired-in recording."""
    return os.environ.get("BRAIN_FIX_VERIFY", "0") == "1"


def _record_enabled() -> bool:
    """Whether to persist outcomes to the calibration feed. Defaults to the
    SAFE 'record-only' behavior whenever the master flag is on; an operator can
    independently disable persistence via BRAIN_VERIFY_RESOLVE=0 without
    touching the (always-safe) read path."""
    if os.environ.get("BRAIN_VERIFY_RESOLVE", "1") == "0":
        return False
    return _flag_on()


def _db_url() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


# ════════════════════════════════════════════════════════════════════
#  Extract (file, search, replace) from a proposal OR an autofix PR dict.
# ════════════════════════════════════════════════════════════════════
def _coerce_to_proposal(proposal_or_pr: dict) -> dict | None:
    """Accept either a proposal-shaped dict (has file_path/search_text/...) or
    an autofix PR dict (has head_ref / head branch). For a PR, recover the
    proposal behind it via the Phase-2 writer's branch log. Returns a
    proposal-shaped dict or None if nothing recoverable. Never raises."""
    if not isinstance(proposal_or_pr, dict):
        return None
    p = proposal_or_pr
    # Already proposal-shaped? Any of these signals a proposal, not a bare PR.
    if (p.get("search_text") or p.get("replace_text") or p.get("changes_json")
            or p.get("file_path")):
        return p
    # Otherwise treat it as a PR dict: recover the logged proposal by branch.
    ref = (p.get("head_ref") or p.get("branch") or p.get("ref") or "")
    if not ref:
        return None
    try:
        from routes.brain_draft_pr_writer import get_logged_proposal_for_branch
        logged = get_logged_proposal_for_branch(ref)
        return logged if isinstance(logged, dict) else None
    except Exception:
        return None


def _extract(proposal: dict):
    """(file_path, search_text, replace_text) for a proposal, reusing the
    classifier's single-change extractor. Falls back to the legacy columns if
    the classifier import fails. Never raises."""
    try:
        from routes.brain_mechanical_classifier import _extract_single_change
        fp, st, rt, _multi = _extract_single_change(proposal)
        return (fp or ""), (st or ""), (rt or "")
    except Exception:
        return (
            (proposal.get("file_path") or ""),
            (proposal.get("search_text") or ""),
            (proposal.get("replace_text") or ""),
        )


# ════════════════════════════════════════════════════════════════════
#  THE FIX-OUTCOME VERIFIER
# ════════════════════════════════════════════════════════════════════
def verify_fix_resolved(proposal_or_pr: dict) -> dict:
    """Verify, against GROUND TRUTH (the file on main), whether a landed brain
    mechanical fix actually RESOLVED its finding.

    A brain mechanical fix is a single (file, search_text, replace_text). After
    it lands on main the finding is RESOLVED iff the anti-pattern (search_text)
    is GONE from the file AND the replacement (replace_text) is PRESENT.

    Returns {resolved, reason, file_path, klass?} where resolved is:
      · True  — search_text absent on main AND replace_text present → resolved.
      · False — search_text STILL present on main → no-op / unresolved fix.
      · None  — indeterminate (file unreadable / nothing to check) → unverified.

    READ-ONLY. NEVER raises. NEVER blocks anything — it only reports."""
    out = {"resolved": None, "reason": "indeterminate",
           "file_path": "", "klass": None}

    proposal = _coerce_to_proposal(proposal_or_pr)
    if not isinstance(proposal, dict):
        out["reason"] = "no_proposal_recoverable"
        return out

    # Carry a klass through if the proposal/branch log already knows it.
    out["klass"] = proposal.get("klass")

    file_path, search_text, replace_text = _extract(proposal)
    file_path = (file_path or "").strip()
    search_text = search_text or ""
    replace_text = replace_text or ""
    out["file_path"] = file_path

    # Nothing to verify against ⇒ indeterminate (NEVER claim resolved).
    if not file_path or not search_text:
        out["reason"] = "missing_file_or_search_text"
        return out

    # Read the file from MAIN (ground truth). Reuse the writer's primitive.
    try:
        from routes.brain_draft_pr_writer import get_file_on_main
        fetched = get_file_on_main(file_path)
    except Exception as e:
        out["reason"] = f"fetch_error:{type(e).__name__}:{str(e)[:80]}"
        return out
    if not isinstance(fetched, dict) or not fetched.get("ok"):
        # Unreadable file ⇒ INDETERMINATE (resolved=None). Do NOT guess.
        out["reason"] = "file_unreadable:" + str((fetched or {}).get("error"))
        return out

    content = fetched.get("content")
    if content is None:
        out["reason"] = "file_unreadable:no_content"
        return out

    search_gone = (search_text not in content)
    # An EMPTY replace_text is a legitimate fix (e.g. removing a shadowed
    # route); "present" for an empty string is vacuously True.
    replace_present = (replace_text == "") or (replace_text in content)

    if search_gone and replace_present:
        out["resolved"] = True
        out["reason"] = "anti_pattern_gone_and_replacement_present"
    elif not search_gone:
        # The anti-pattern is STILL there → the fix did not resolve the finding.
        out["resolved"] = False
        out["reason"] = "anti_pattern_still_present_on_main"
    else:
        # search gone but the replacement isn't present → the file changed in
        # some OTHER way; we can't positively confirm THIS fix resolved it.
        out["resolved"] = None
        out["reason"] = "search_gone_but_replacement_absent"
    return out


# ════════════════════════════════════════════════════════════════════
#  CALIBRATION FEED — record verified vs unverified (best-effort).
#  Mirrors routes/autopilot_outcomes' autopilot_outcomes table convention so
#  the same calibration surface can read fix-outcome verifications. NEVER
#  raises; a failure here must never affect a merge or a tick.
# ════════════════════════════════════════════════════════════════════
def _ensure_fix_outcomes_table() -> bool:
    """CREATE TABLE IF NOT EXISTS brain_fix_outcomes. Idempotent. Returns False
    on any failure (no DB, no psycopg2). Never raises."""
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_fix_outcomes (
                    id            BIGSERIAL PRIMARY KEY,
                    proposal_id   BIGINT,
                    pr_number     INTEGER,
                    branch        TEXT,
                    file_path     TEXT,
                    klass         TEXT,
                    resolved      BOOLEAN,   -- NULL = unverified/indeterminate
                    reason        TEXT,
                    verified_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS brain_fix_outcomes_recent_idx
                  ON brain_fix_outcomes(verified_at DESC);
            """)
            conn.commit()
        return True
    except Exception:
        return False


def record_fix_outcome(verdict: dict, *, proposal_id=None, pr_number=None,
                       branch: str = "") -> bool:
    """Persist a fix-outcome verdict to the calibration feed. Best-effort:
    returns True on a committed row, False otherwise. Gated by _record_enabled()
    so it is a no-op unless BRAIN_FIX_VERIFY=1 (and BRAIN_VERIFY_RESOLVE!=0).
    NEVER raises.

    Also writes a NEUTRAL outcome into the shared autopilot_outcomes feed (when
    that module is importable) keyed pattern='brain_fix_outcome' so the existing
    /api/v1/brain/autopilot/outcomes surface shows fix-verification calibration
    alongside the autopilot outcomes — succeeded mirrors `resolved` (tri-state:
    None = cannot/indeterminate, mapped to the cannot_verify bucket)."""
    if not _record_enabled():
        return False
    resolved = (verdict or {}).get("resolved")
    reason = str((verdict or {}).get("reason") or "")[:500]
    file_path = (verdict or {}).get("file_path") or ""
    klass = (verdict or {}).get("klass")

    wrote = False
    # 1) Our own dedicated table (always when records are enabled).
    if _ensure_fix_outcomes_table():
        try:
            import psycopg2
            url = _db_url()
            if url:
                with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO brain_fix_outcomes "
                        "(proposal_id, pr_number, branch, file_path, klass, "
                        " resolved, reason) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (proposal_id, pr_number, (branch or None), file_path,
                         klass, resolved, reason),
                    )
                    conn.commit()
                wrote = True
        except Exception:
            wrote = False

    # 2) Best-effort mirror into the shared calibration feed (if it exists).
    #    succeeded is tri-state and mirrors `resolved` exactly (None stays None
    #    → the outcomes endpoint buckets it as cannot_verify, not a failure).
    try:
        from routes import autopilot_outcomes as ao
        c = ao._conn()
        if c is not None:
            try:
                ao._ensure_schema(c)
                import datetime as _dt
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO autopilot_outcomes "
                        "(autopilot_action_id, pattern_name, fired_at, "
                        " succeeded, evidence, failure_reason) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (proposal_id, "brain_fix_outcome",
                         _dt.datetime.utcnow(), resolved,
                         (reason or "")[:500],
                         (None if resolved else reason)[:500] if reason else None),
                    )
            finally:
                try:
                    c.close()
                except Exception:
                    pass
    except Exception:
        pass

    return wrote


def verify_and_record(proposal_or_pr: dict, *, proposal_id=None,
                      pr_number=None, branch: str = "") -> dict:
    """Convenience: run verify_fix_resolved() AND (best-effort, flag-gated)
    record the outcome. Returns the verdict dict (with a `recorded` bool added).
    This is the entrypoint the brain loop wires in. NEVER raises, NEVER blocks."""
    verdict = verify_fix_resolved(proposal_or_pr)
    pid = proposal_id
    if pid is None and isinstance(proposal_or_pr, dict):
        pid = proposal_or_pr.get("id") or proposal_or_pr.get("proposal_id")
    recorded = False
    try:
        recorded = record_fix_outcome(
            verdict, proposal_id=pid, pr_number=pr_number, branch=branch)
    except Exception:
        recorded = False
    verdict = dict(verdict)
    verdict["recorded"] = recorded
    return verdict


# ════════════════════════════════════════════════════════════════════
#  QUARANTINE-CONSUME: is this (file, klass) recipe quarantined?
#  brain_pr_janitor.quarantine_recipe WRITES status='quarantined' rows; this
#  is the READER that finally CONSUMES them so a quarantined recipe is actually
#  blocked from re-proposal. Best-effort; False on any error so a DB hiccup
#  never wrongly blocks a legitimate fix.
# ════════════════════════════════════════════════════════════════════
def is_recipe_quarantined(file_path: str, klass: str = "") -> bool:
    """True iff brain_proposed_code_fixes has a status='quarantined' row for
    this (file_path, finding_class). Matches file_path AND finding_class so a
    DIFFERENT recipe on the same file is unaffected; when klass is falsy we
    match on file_path alone (still scoped to that one file). Best-effort:
    returns False on ANY error (no DB / no psycopg2 / query failure) so a
    transient DB problem can NEVER wrongly block a legitimate fix. Never raises.
    """
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
            if (klass or "").strip():
                cur.execute(
                    "SELECT 1 FROM brain_proposed_code_fixes "
                    " WHERE file_path = %s "
                    "   AND LOWER(COALESCE(finding_class, '')) = LOWER(%s) "
                    "   AND COALESCE(status, '') = 'quarantined' "
                    " LIMIT 1",
                    (fp, str(klass)),
                )
            else:
                cur.execute(
                    "SELECT 1 FROM brain_proposed_code_fixes "
                    " WHERE file_path = %s "
                    "   AND COALESCE(status, '') = 'quarantined' "
                    " LIMIT 1",
                    (fp,),
                )
            row = cur.fetchone()
        return bool(row)
    except Exception:
        return False
