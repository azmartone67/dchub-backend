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

EPISODE SEMANTICS (stateful-detector layer, 2026-07-17): detectors are
stateless probes that re-emit the same finding every scan cycle, which
used to bump seen_count per cycle (max observed: 1463 on a single
month-open finding). A finding row is now an EPISODE ledger:

  open ──(re-observation)──> still-observed ──(absence/resolve)──> resolved
    └────────────(re-detect after resolved = NEW episode)◄────────────┘

  · still-observed (row open, resolved_at NULL): bump last_seen +
    episode_seen_count ONLY. seen_count does NOT move.
  · reopen (row resolved, re-detected): episode_count += 1,
    seen_count += 1, episode_seen_count resets to 1,
    episode_started_at restamps. seen_count therefore counts
    EPISODES ("recurred ×N incidents"), not scan cycles.
  · explicit resolve (status='resolved'/'wont_fix'/'dismissed'):
    transition only — stamps resolved_at if unset, no count bumps
    (a resolve is not a sighting).

The runaway guard keys on episode_seen_count (sightings within the
current episode without resolution) — its original documented meaning.
"""
import logging
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)

# Process-level schema cache. Re-introspected only if a write hits a
# missing-column error (defensive against an ALTER landing mid-run).
_schema = {"ensured": False, "cols": set(), "has_seen_count": False,
           "has_episodes": False}

# Episode-ledger columns (stateful-detector layer). Added idempotently by
# _ensure_schema, same self-heal pattern as the seen_count restore.
_EPISODE_COL_DDL = (
    ("episode_count",      "INTEGER NOT NULL DEFAULT 1"),
    ("episode_seen_count", "INTEGER NOT NULL DEFAULT 1"),
    ("episode_started_at", "TIMESTAMPTZ"),
)
# Episode UPDATE logic reads these pre-existing columns too; without any
# of them we fall back to the legacy bump-per-sighting behavior.
_EPISODE_REQUIRED = {"episode_count", "episode_seen_count",
                     "episode_started_at", "status", "resolved_at",
                     "first_seen", "seen_count"}

# Shell #49 lane 3 (2026-08-02): `count` is per-detector FREE-FORM. Some
# detectors put a tally in it, some a duration, some a backlog size — and
# brain_work_selector.impact_weight() reads it as an occurrence signal, so
# brain_consistency_radar's `int(seconds_since)` made 5.5 days of cron
# silence read as 477,455 sightings and re-win the agenda every tick.
#
# The producer KNOWS the answer at write time. This column carries it, so
# the consumer can stop inferring it from a hand-maintained list of issue
# strings that has now been edited three times, once per recurrence of the
# same class, each time AFTER the misread had already cost a cycle.
# Same idempotent self-heal as seen_count / the episode ledger.
_COUNT_KIND_DDL = (("count_kind", "TEXT"),)
# The one value that means "this integer is a tally of sightings". Anything
# else is a magnitude and must not buy agenda leverage.
OCCURRENCE_KIND = "occurrence"


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
    # count_kind (#49 lane 3): nullable TEXT, no default, no backfill —
    # a NULL means "this detector has not declared its type yet", which is
    # exactly what the consumer needs to know so it can fall back to the
    # conservative untyped path instead of assuming a tally.
    ck_missing = [c for c, _ in _COUNT_KIND_DDL if c not in cols] if cols else []
    if ck_missing and _savepoint(cur, "bfw_alter_count_kind"):
        try:
            for cname, ctype in _COUNT_KIND_DDL:
                cur.execute(f"ALTER TABLE brain_findings "
                            f"ADD COLUMN IF NOT EXISTS {cname} {ctype}")
            cols.update(c for c, _ in _COUNT_KIND_DDL)
            _release_sp(cur, "bfw_alter_count_kind")
            logger.info("brain_findings_writer: added count_kind column")
        except Exception as e:
            _rollback_sp(cur, "bfw_alter_count_kind")
            logger.warning("brain_findings_writer: could not add "
                           "count_kind: %s", e)
    # Episode-ledger columns: add any that are missing (idempotent, no
    # table rewrite — constant defaults are metadata-only in PG 11+).
    ep_missing = [c for c, _ in _EPISODE_COL_DDL if c not in cols] if cols else []
    if ep_missing:
        if _savepoint(cur, "bfw_alter_episode"):
            try:
                for cname, ctype in _EPISODE_COL_DDL:
                    cur.execute(
                        f"ALTER TABLE brain_findings "
                        f"ADD COLUMN IF NOT EXISTS {cname} {ctype}")
                cols.update(c for c, _ in _EPISODE_COL_DDL)
                _release_sp(cur, "bfw_alter_episode")
                logger.info("brain_findings_writer: added episode columns %s",
                            ep_missing)
            except Exception as e:
                _rollback_sp(cur, "bfw_alter_episode")
                logger.warning("brain_findings_writer: could not add episode "
                               "columns: %s", e)
        # Backfill: pre-existing rows date their current episode from
        # first_seen (their episode_seen_count starts at the DEFAULT 1 and
        # climbs honestly from here — the legacy inflated seen_count is
        # left frozen as history). Own savepoint so a backfill failure
        # can't undo the column ADDs above.
        if "episode_started_at" in cols and _savepoint(cur, "bfw_ep_backfill"):
            try:
                cur.execute(
                    "UPDATE brain_findings SET episode_started_at = "
                    "COALESCE(first_seen, NOW()) "
                    "WHERE episode_started_at IS NULL")
                _release_sp(cur, "bfw_ep_backfill")
            except Exception:
                _rollback_sp(cur, "bfw_ep_backfill")
    _schema["cols"] = cols
    _schema["has_seen_count"] = "seen_count" in cols
    _schema["has_episodes"] = _EPISODE_REQUIRED <= cols
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
# The guard is fail-soft + audited: a savepoint wraps every probe and the
# quarantine row carries a clear reason.
#
# ★HONEST AUTO-RELEASE (2026-08-14). The original text here claimed "the
# standard 24h auto-release in brain_autopilot._quarantined_patterns()
# still applies". It did NOT: every row this guard writes is born with
# fail_count = seen_count >= RUNAWAY_SEEN_THRESHOLD (200), which is also
# brain_autopilot._QUARANTINE_RUNAWAY_FAIL (200) — so the item-16 runaway
# immunize clause kept the ENTIRE class benched forever, and nothing ever
# stamped released_at. Nine cron_silently_dead self-alarms sat silenced
# 9-15 days on a 24h promise (owner release 2026-08-14). Now:
#   * brain_autopilot's verify tick stamps released_at on any
#     runaway_finding: row older than BRAIN_RUNAWAY_RELEASE_HOURS
#     (default 24) — the promise is real and VISIBLE in the table.
#   * A finding that is STILL runaway re-benches here for another window,
#     but only when a NEW episode re-crosses the threshold (exact
#     crossing, seen == threshold), never on the trailing sightings of
#     the already-benched episode — so a human/owner release is not
#     instantly clobbered by the next scan.
RUNAWAY_SEEN_THRESHOLD = 200
RUNAWAY_QUARANTINE_PREFIX = "runaway_finding:"


def _maybe_quarantine_runaway(cur, issue: str, url: str,
                              seen_count_after: int,
                              status: str = "open") -> bool:
    """If (issue,url) crossed RUNAWAY_SEEN_THRESHOLD without resolving,
    register it in brain_pattern_quarantine so the autopilot bench list
    suppresses it. Returns True if it ADDED or RE-ARMED a quarantine row
    this call. Repeated calls inside the same episode are no-ops.

    seen_count_after is sightings-without-resolution: on episode-aware
    schemas the caller passes episode_seen_count (resets when an episode
    resolves, so a recurring-but-resolving finding never trips this);
    on legacy schemas it's the all-time seen_count.

    A finding marked resolved/wont_fix is exempt — only runaway 'open'
    findings get suppressed. The autopilot verify tick stamps
    released_at after BRAIN_RUNAWAY_RELEASE_HOURS (default 24h) — see
    the block comment above — and a released row is only RE-armed here
    on an exact new threshold crossing (seen == threshold), so release
    decisions stick until the finding genuinely re-runs away."""
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
        # Exact-crossing flag: TRUE only on the sighting that takes the
        # episode from threshold-1 to threshold. Trailing sightings of an
        # already-runaway episode (seen 201, 202, …) must never re-arm a
        # row a human or the auto-release has released — otherwise every
        # release is clobbered by the very next scan and the bench becomes
        # a permanent gag again (the exact defect this fixes).
        crossed_now = int(seen_count_after) == RUNAWAY_SEEN_THRESHOLD
        cur.execute(
            "INSERT INTO brain_pattern_quarantine "
            "(pattern_name, fail_count, last_reason) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (pattern_name) DO UPDATE "
            "   SET fail_count = EXCLUDED.fail_count, "
            "       quarantined_at = NOW(), "
            "       released_at = NULL, "
            "       last_reason = EXCLUDED.last_reason "
            " WHERE brain_pattern_quarantine.released_at IS NOT NULL "
            "   AND %s",
            (pattern_name, int(seen_count_after),
             f"runaway: {seen_count_after} sightings without resolution "
             f"(threshold={RUNAWAY_SEEN_THRESHOLD}); auto-release after "
             f"BRAIN_RUNAWAY_RELEASE_HOURS (default 24h) — released_at is "
             f"stamped by the autopilot verify tick. issue={issue} "
             f"url={url[:120]}",
             crossed_now),
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
                         status: str = "open", count_kind: str = "",
                         detector_fn: str = "") -> str:
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
    count_kind = (count_kind or "")[:40]
    has_sc = _schema["has_seen_count"]
    # ★Only write count_kind when the caller DECLARED one. An undeclared
    # write must never overwrite a type a detector previously declared —
    # that would silently re-open the exact hole this column closes.
    write_kind = bool(count_kind) and "count_kind" in cols
    # ★ PROVENANCE (2026-09-05). `detector` names the MODULE that wrote the
    #   row; `detector_fn` names the specific check function inside it. The
    #   radar's resolve-on-absence sweep needs the finer grain: it may only
    #   close a row whose producing FUNCTION actually ran this sweep. Written
    #   only when the caller declares one AND the column exists, so every
    #   other writer and every older schema is untouched.
    detector_fn = (detector_fn or "")[:120]
    write_fn = bool(detector_fn) and "detector_fn" in cols

    # ── 1. UPDATE existing row (recurrence) ──
    if _savepoint(cur, "bfw_upd"):
        has_ep = _schema["has_episodes"]
        resolved_like = status in ("resolved", "wont_fix", "dismissed")
        set_parts = ["count = %s", "detail = %s"]
        params = [count, detail]
        if write_kind:
            set_parts.append("count_kind = %s")
            params.append(count_kind)
        if write_fn:
            set_parts.append("detector_fn = %s")
            params.append(detector_fn)
        if "last_seen" in cols:
            set_parts.append("last_seen = NOW()")
        if has_ep and not resolved_like:
            # EPISODE semantics (see module docstring). The CASE reads the
            # row's PRE-update values (PG evaluates SET right-hand sides
            # against the old row): an open, unresolved row is the same
            # ongoing episode — absorb the re-observation (last_seen +
            # episode_seen_count only, seen_count frozen). Anything else
            # (auto-resolved by the radar's absence sweep, explicitly
            # resolved, or a stale open+resolved_at inconsistency) means
            # the incident went away and came back: a NEW episode —
            # seen_count/episode_count move exactly once per episode.
            still = "status = 'open' AND resolved_at IS NULL"
            set_parts += [
                f"seen_count = CASE WHEN {still} THEN COALESCE(seen_count, 1)"
                f" ELSE COALESCE(seen_count, 1) + 1 END",
                f"episode_count = CASE WHEN {still}"
                f" THEN COALESCE(episode_count, 1)"
                f" ELSE COALESCE(episode_count, 1) + 1 END",
                f"episode_seen_count = CASE WHEN {still}"
                f" THEN COALESCE(episode_seen_count, 1) + 1 ELSE 1 END",
                f"episode_started_at = CASE WHEN {still}"
                f" THEN COALESCE(episode_started_at, first_seen)"
                f" ELSE NOW() END",
                "status = %s",
                # Reopen handling (durable-findings r-incentives): clear the
                # stamp so the row never reads both open and resolved.
                "resolved_at = NULL",
            ]
            params.append(status)
        elif has_ep and resolved_like:
            # Explicit resolve/wont_fix/dismiss = a state TRANSITION, not a
            # sighting: no count bumps. Stamp resolved_at if the caller is
            # the first to close this episode (fast_qa-style resolvers
            # never stamped it before, starving the open→resolved
            # trajectory metric).
            set_parts += ["status = %s",
                          "resolved_at = COALESCE(resolved_at, NOW())"]
            params.append(status)
        else:
            # Legacy path (episode columns unavailable): original
            # bump-per-sighting behavior, unchanged.
            if has_sc:
                set_parts.append("seen_count = COALESCE(seen_count, 1) + 1")
            if "status" in cols:
                set_parts.append("status = %s")
                params.append(status)
            # Reopen handling (durable-findings r-incentives): a finding that
            # re-detects after having been auto-resolved must clear its
            # resolved_at stamp, otherwise it reads as both open (status) and
            # resolved (resolved_at non-NULL) → the open/resolved trajectory
            # double-counts it. Only clear on a re-detect that is itself
            # "open" (the normal scan path); an explicit resolve/wont_fix
            # upsert keeps any prior resolved_at. Degrades safely if the
            # column is absent (older schema) — the clause is just skipped.
            if "resolved_at" in cols and not resolved_like:
                set_parts.append("resolved_at = NULL")
        params += [issue, url]
        try:
            # RETURNING lets the runaway guard see the post-update counts +
            # live status without a second round trip.
            ret_cols = []
            if has_sc:
                ret_cols.append("seen_count")
            if "status" in cols:
                ret_cols.append("status")
            if has_ep:
                ret_cols.append("episode_seen_count")
            ret_clause = (" RETURNING " + ", ".join(ret_cols)) if ret_cols else ""
            cur.execute(
                f"UPDATE brain_findings SET {', '.join(set_parts)} "
                f"WHERE issue = %s AND url = %s{ret_clause}", params)
            rc = cur.rowcount
            row = cur.fetchone() if ret_cols and rc else None
            _release_sp(cur, "bfw_upd")
            if rc and rc > 0:
                if row and ret_cols:
                    retvals = dict(zip(ret_cols, row))
                    status_after = retvals.get("status") or status
                    # Guard input: sightings within the CURRENT episode
                    # (episode_seen_count) — the guard's documented "N
                    # sightings without resolution". Falls back to the
                    # legacy all-time seen_count on old schemas.
                    guard_seen = retvals.get("episode_seen_count")
                    if guard_seen is None:
                        guard_seen = retvals.get("seen_count") or 0
                    try:
                        _maybe_quarantine_runaway(
                            cur, issue, url, int(guard_seen),
                            status_after or status)
                    except Exception:
                        pass
                return "updated"
        except Exception:
            note_swallowed_write("brain_findings", where="brain_findings_writer.upsert_brain_finding")
            _rollback_sp(cur, "bfw_upd")

    # ── 2. INSERT new row — only columns that exist ──
    if _savepoint(cur, "bfw_ins"):
        vals = {"issue": issue, "url": url, "count": count, "detail": detail}
        if write_kind:
            vals["count_kind"] = count_kind
        if write_fn:
            vals["detector_fn"] = detector_fn
        if "detector" in cols and detector is not None:
            vals["detector"] = detector
        if "status" in cols:
            vals["status"] = status
        if has_sc:
            vals["seen_count"] = 1
        # Episode ledger: a brand-new finding opens episode #1. (The
        # `use` filter below drops these on pre-episode schemas.)
        vals["episode_count"] = 1
        vals["episode_seen_count"] = 1
        use = {c: v for c, v in vals.items() if c in cols}
        icols = list(use)
        now_cols = [c for c in ("first_seen", "last_seen",
                                "episode_started_at") if c in cols]
        collist = ", ".join(icols + now_cols)
        ph = ", ".join(["%s"] * len(icols) + ["NOW()"] * len(now_cols))
        try:
            cur.execute(
                f"INSERT INTO brain_findings ({collist}) VALUES ({ph})",
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
