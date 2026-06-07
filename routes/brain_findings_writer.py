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


def upsert_brain_finding(cur, issue: str, url: str = "", count: int = 1,
                         detail: str = "", detector: str = None,
                         status: str = "open") -> str:
    """Constraint-agnostic upsert into brain_findings.

    Returns "updated" | "inserted" | "skipped". Never raises — every DB
    op is savepoint-wrapped so the caller's transaction survives. Trust
    this return value (it reflects the real DB outcome), not an external
    counter.

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
            cur.execute(
                f"UPDATE brain_findings SET {', '.join(set_parts)} "
                f"WHERE issue = %s AND url = %s", params)
            rc = cur.rowcount
            _release_sp(cur, "bfw_upd")
            if rc and rc > 0:
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
