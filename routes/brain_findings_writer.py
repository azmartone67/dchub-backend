"""Canonical brain_findings writer — single source of truth.

Built 2026-06-06 after schema drift broke 4+ writers silently. Every
writer hand-rolled `INSERT ... seen_count ... ON CONFLICT (issue,url)`,
but the LIVE table (verified via information_schema) is:

  id, issue, url, count, detail, detector,
  first_seen, last_seen, created_at, resolved_at, status

— NO seen_count, and NO confirmed UNIQUE(issue,url) constraint. The
repo DDL (brain_consistency_radar) claims both; it's drifted/stale.
So those INSERTs failed silently inside bare `except` blocks, the
brain_findings table went stale, and the recurrence-tracking ("seen
×N" on the dashboard) + the learning loop that references prior
findings both quietly broke for weeks.

This module is the ONE place that writes brain_findings. It:

  1. Lazily INTROSPECTS the live columns (once per process) instead of
     assuming — the schema-drift trap can't bite a writer that asks
     the DB what columns exist.
  2. Idempotently ADDs seen_count if missing — restores recurrence
     tracking (ADD COLUMN IF NOT EXISTS DEFAULT 1 is safe + fast in
     PG 11+; no table rewrite).
  3. Upserts CONSTRAINT-AGNOSTICALLY (UPDATE-then-INSERT) so it works
     whether or not UNIQUE(issue,url) exists.
  4. Writes ONLY columns confirmed present (detector/status filled
     when available).
  5. Wraps every DB op in a SAVEPOINT so a failure rolls back just
     itself — never poisons the caller's transaction (the cascade
     that bit the first enact attempt).

All brain_findings writers should call upsert_brain_finding(cur, ...)
instead of hand-rolling an INSERT. New writers: import this, done.
"""
import logging

logger = logging.getLogger(__name__)

# Process-level schema cache. Re-introspected only if a write hits a
# missing-column error (defensive against an ALTER landing mid-run).
_schema = {"ensured": False, "cols": set(), "has_seen_count": False}


def _savepoint(cur, name: str):
    try:
        cur.execute(f"SAVEPOINT {name}")
        return True
    except Exception:
        return False


def _rollback_sp(cur, name: str):
    try:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:
        pass


def _release_sp(cur, name: str):
    try:
        cur.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        pass


def _ensure_schema(cur, force: bool = False) -> None:
    """Introspect live columns once; add seen_count if missing."""
    if _schema["ensured"] and not force:
        return
    cols = set()
    if _savepoint(cur, "bfw_introspect"):
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'brain_findings'")
            cols = {r[0] for r in cur.fetchall()}
            _release_sp(cur, "bfw_introspect")
        except Exception:
            _rollback_sp(cur, "bfw_introspect")
    # Restore recurrence tracking: add seen_count if the table exists
    # but lacks it. Idempotent + non-destructive.
    if cols and "seen_count" not in cols:
        if _savepoint(cur, "bfw_alter"):
            try:
                cur.execute(
                    "ALTER TABLE brain_findings "
                    "ADD COLUMN IF NOT EXISTS seen_count INTEGER "
                    "NOT NULL DEFAULT 1")
                cols.add("seen_count")
                _release_sp(cur, "bfw_alter")
                logger.info("brain_findings_writer: added missing "
                            "seen_count column")
            except Exception as e:
                _rollback_sp(cur, "bfw_alter")
                logger.warning("brain_findings_writer: could not add "
                               "seen_count: %s", e)
    _schema["cols"] = cols
    _schema["has_seen_count"] = "seen_count" in cols
    _schema["ensured"] = True


# ── Runaway-finding guard (phase r70+obsolete-prune, 2026-06-07) ──────
# Any (issue, url) tuple that crosses RUNAWAY_THRESHOLD sightings without
# transitioning to status='resolved' or 'wont_fix' is suppressed from
# future scans by registering it in brain_pattern_quarantine. This
# prevents the recurring whack-a-mole where a single stuck finding
# floods the worklist for weeks (founding_customer_not_welcomed was
# rate-limited 50× without ever clearing; operator_directive synthesised
# a fake 10,200 seen_count from a priority boost on an obsolete entry).
#
# The guard is fail-soft + audited: a savepoint wraps every probe, the
# quarantine row carries a clear reason, and the standard 24h auto-release
# in brain_autopilot._quarantined_patterns() still applies — so a runaway
# pattern that becomes real again will retry on its own.
RUNAWAY_SEEN_THRESHOLD = 200
RUNAWAY_QUARANTINE_PREFIX = "runaway_finding:"


def _maybe_quarantine_runaway(cur, issue: str, url: str,
                              seen_count_after: int,
                              status: str = "open") -> bool:
    """If (issue,url) crossed RUNAWAY_SEEN_THRESHOLD without resolving,
    register it in brain_pattern_quarantine so the autopilot bench list
    suppresses it. Returns True if it ADDED a new quarantine row this
    call. Idempotent — repeated calls are no-ops once registered.

    A finding marked resolved/wont_fix is exempt — only runaway 'open'
    findings get suppressed. The autopilot's existing 24h auto-release
    naturally retries patterns whose root cause may have changed."""
    if not issue or seen_count_after < RUNAWAY_SEEN_THRESHOLD:
        return False
    if status in ("resolved", "wont_fix", "dismissed"):
        return False
    pattern_name = (f"{RUNAWAY_QUARANTINE_PREFIX}{issue}"[:240]
                    + (f"|{url[:60]}" if url else ""))[:240]
    if not _savepoint(cur, "bfw_runaway"):
        return False
    try:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS brain_pattern_quarantine ("
            "  pattern_name TEXT PRIMARY KEY,"
            "  fail_count INT NOT NULL DEFAULT 0,"
            "  quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "  released_at TIMESTAMPTZ,"
            "  last_reason TEXT)")
        cur.execute(
            "INSERT INTO brain_pattern_quarantine "
            "(pattern_name, fail_count, last_reason) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (pattern_name) DO NOTHING",
            (pattern_name, int(seen_count_after),
             f"runaway: {seen_count_after} sightings without resolution "
             f"(threshold={RUNAWAY_SEEN_THRESHOLD}); auto-release in 24h "
             f"via brain_autopilot._quarantined_patterns. issue={issue} "
             f"url={url[:120]}"),
        )
        added = bool(cur.rowcount)
        _release_sp(cur, "bfw_runaway")
        if added:
            logger.warning(
                "brain_findings_writer: quarantined runaway finding "
                "issue=%s url=%s seen_count=%s",
                issue[:80], url[:120], seen_count_after)
        return added
    except Exception as e:
        _rollback_sp(cur, "bfw_runaway")
        logger.warning("brain_findings_writer: runaway quarantine probe "
                       "failed: %s", e)
        return False


def upsert_brain_finding(cur, issue: str, url: str = "", count: int = 1,
                         detail: str = "", detector: str = None,
                         status: str = "open") -> str:
    """Constraint-agnostic upsert into brain_findings.

    Returns "updated" | "inserted" | "skipped". Never raises — every DB
    op is savepoint-wrapped so the caller's transaction survives. Trust
    this return value (it reflects the real DB outcome), not an external
    counter.

    Side effect (phase r70+obsolete-prune): when a recurrence crosses
    RUNAWAY_SEEN_THRESHOLD without status transitioning to resolved /
    wont_fix, the (issue,url) gets auto-registered in
    brain_pattern_quarantine so the autopilot bench-list suppresses it
    for the standard 24h auto-release window. Prevents the recurring
    "finding flagged 10,200×" pathology.

    Usage in any writer:
        from routes.brain_findings_writer import upsert_brain_finding
        for f in findings:
            upsert_brain_finding(cur, issue=f["issue"], url=f["url"],
                                 count=f.get("count", 1),
                                 detail=f.get("detail", ""),
                                 detector="my_scanner")
        conn.commit()
    """
    _ensure_schema(cur)
    cols = _schema["cols"]
    if "issue" not in cols:
        return "skipped"  # table shape we can't write to

    issue = (issue or "")[:200]
    url = (url or "")[:500]
    detail = (detail or "")[:2000]
    has_sc = _schema["has_seen_count"]

    # ── 1. UPDATE existing row (recurrence) ──
    if _savepoint(cur, "bfw_upd"):
        set_parts = ["count = %s", "detail = %s"]
        params = [count, detail]
        if "last_seen" in cols:
            set_parts.append("last_seen = NOW()")
        if has_sc:
            set_parts.append("seen_count = COALESCE(seen_count, 1) + 1")
        if "status" in cols:
            set_parts.append("status = %s")
            params.append(status)
        params += [issue, url]
        try:
            # RETURNING lets the runaway guard see the post-update
            # seen_count + live status without a second round trip.
            ret_cols = []
            if has_sc:
                ret_cols.append("seen_count")
            if "status" in cols:
                ret_cols.append("status")
            ret_clause = (" RETURNING " + ", ".join(ret_cols)) if ret_cols else ""
            cur.execute(
                f"UPDATE brain_findings SET {', '.join(set_parts)} "
                f"WHERE issue = %s AND url = %s{ret_clause}", params)
            rc = cur.rowcount
            row = cur.fetchone() if ret_cols and rc else None
            _release_sp(cur, "bfw_upd")
            if rc and rc > 0:
                if row and ret_cols:
                    seen_after = int(row[0]) if has_sc else 0
                    status_after = (row[1] if "status" in cols and len(row) > 1
                                    else status)
                    try:
                        _maybe_quarantine_runaway(
                            cur, issue, url, seen_after, status_after or status)
                    except Exception:
                        pass
                return "updated"
        except Exception:
            _rollback_sp(cur, "bfw_upd")

    # ── 2. INSERT new row — only columns that exist ──
    if _savepoint(cur, "bfw_ins"):
        vals = {"issue": issue, "url": url, "count": count, "detail": detail}
        if "detector" in cols and detector is not None:
            vals["detector"] = detector
        if "status" in cols:
            vals["status"] = status
        if has_sc:
            vals["seen_count"] = 1
        use = {c: v for c, v in vals.items() if c in cols}
        icols = list(use)
        now_cols = [c for c in ("first_seen", "last_seen") if c in cols]
        collist = ", ".join(icols + now_cols)
        ph = ", ".join(["%s"] * len(icols) + ["NOW()"] * len(now_cols))
        try:
            cur.execute(
                f"INSERT INTO brain_findings ({collist}) VALUES ({ph}) ON CONFLICT DO NOTHING",
                [use[c] for c in icols])
            _release_sp(cur, "bfw_ins")
            return "inserted"
        except Exception as e:
            _rollback_sp(cur, "bfw_ins")
            logger.warning("brain_findings_writer: insert failed: %s", e)

    return "skipped"


def live_columns() -> list:
    """Diagnostic: what columns did the last introspection see?"""
    return sorted(_schema["cols"])
