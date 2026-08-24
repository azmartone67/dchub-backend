"""Silent-actuator detectors — the class the radar could not see (2026-08-24).

A QA sweep on 2026-08-24 found three live defects. All three had been running
for weeks. The radar's 128 detectors saw none of them, and its four open
actionable findings that day were all `cron_schedule_collision`. The gap was
never actuation — it was that no detector can see a lane that STOPS, as opposed
to a lane that errors.

What was found, and which detector here would have caught it:

  1. mcp_pair_codes minted nothing after 2026-07-07 while mcp_upgrade_signals
     kept firing 1,000-2,500/week. The gate fired; the artifact was never
     written. Nothing errored — the write simply stopped happening.
        -> check_actuator_lane_silent

  2. social_media_posts accumulated `approved` rows while `posted_at` stayed
     NULL: publishing ran 4-7/day through 08-20, then 0/day 08-21..08-24 with
     201 rejects/30d still flowing. A queue that fills while its drain reads
     zero.
        -> check_approved_backlog_unpublished

  3. social_audience.followers for platform='linkedin' has been NULL on all 29
     rows in the last 30 days, reason='http_400', and has NEVER once been
     non-null. fetch_linkedin_followers is fail-soft and its docstring predicts
     a 403 scope gap, so http_400 was read as that expected gap for weeks. The
     media growth loop's declared weakest lever (follower_momentum) was steering
     on that null. The real value was 350 against a goal of 250.
        -> check_failsoft_metric_null_streak

The shared shape of all three: a number that is ZERO or NULL for a reason that
is not an error. `try/except` cannot see them and neither can an HTTP health
check. They are only visible as an ABSENCE measured against the same lane's
own history.

Every detector here FAILS CLOSED — returns [] on any error — so that
resolve-on-absence cannot auto-close a real finding when the DB is unreachable.
That is the same contract check_gated_endpoint_coaching_missing and
check_ai_platform_crawl_drop carry.
"""

# ── knobs, all in one place so a tuning change is one diff ───────────────

# (table, timestamp_column, max_quiet_days, label, why_it_matters)
# Only lanes that are SUPPOSED to write on a sub-weekly cadence belong here.
# A lane that is legitimately event-driven does not — that is what made the
# press_releases SLA a false-breach generator before it was moved to 168h.
_ACTUATOR_LANES = [
    ("relay_opens", "ts", 14,
     "agent->human paywall handoff opens",
     "the /upgrade/h/<payload>.<sig> link is the only path from a paywalled "
     "agent to a paying human; zero opens means the relay is not reaching "
     "anyone, which looks identical to nobody wanting it"),
    ("mcp_upgrade_signals", "created_at", 3,
     "paywall gate firings",
     "if the gate stops firing while calls continue, tier resolution broke "
     "open and paid tools are being served free"),
    ("white_glove_runs", "created_at", 3,
     "white-glove registry propagation",
     "the lane missed 40% of days undetected once already (fixed 2026-08-18); "
     "this is the backstop that notices if it regresses"),
    ("audience_snapshots", "computed_at", 3,
     "agent/audience telemetry snapshot",
     "every growth number the media and growth loops steer on is read from "
     "this table"),
]

# Publish-queue lanes: (table, status_column, approved_value, posted_column,
#                       created_column, min_backlog, quiet_hours, label)
_PUBLISH_QUEUES = [
    ("social_media_posts", "status", "approved", "posted_at", "created_at",
     3, 12, "social publishing"),
]

# Fail-soft metric lanes: (table, date_column, metric_column, partition_column,
#                          reason_column, min_null_days, label)
_FAILSOFT_METRICS = [
    ("social_audience", "snap_date", "followers", "platform", "reason",
     7, "social follower counts"),
]


def _db():
    """Autocommit helper. Returns None when DATABASE_URL is unset or the
    connect fails — every caller treats None as "cannot tell" and emits
    nothing, never "healthy"."""
    try:
        import os as _os
        import psycopg2 as _pg2
    except Exception:
        return None
    try:
        db = _os.environ.get("DATABASE_URL")
    except Exception:
        return None
    if not db:
        return None
    try:
        c = _pg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _close(c):
    try:
        c.close()
    except Exception:
        pass


def check_actuator_lane_silent() -> list[dict]:
    """A write-lane that HAS written before and has now gone quiet past its
    cadence.

    The COUNT(*) guard is what keeps this from screaming on a table that is
    simply new or empty by design: a lane with zero rows EVER is not a
    regression, it is an unbuilt feature. Only a lane with history AND a gap
    fires.
    """
    findings: list[dict] = []
    c = _db()
    if c is None:
        return findings
    try:
        for table, ts_col, max_quiet, label, why in _ACTUATOR_LANES:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        f"SELECT COUNT(*) AS ever, "
                        f"       MAX({ts_col}) AS last_write, "
                        f"       EXTRACT(EPOCH FROM (NOW() - MAX({ts_col})))/86400.0 AS quiet_days "
                        f"  FROM {table}"
                    )
                    row = cur.fetchone()
                if not row:
                    continue
                ever, last_write, quiet_days = row[0], row[1], row[2]
                if not ever or ever == 0:
                    # never written — unbuilt, not broken
                    continue
                if quiet_days is None:
                    continue
                quiet_days = float(quiet_days)
                if quiet_days <= max_quiet:
                    continue
                findings.append({
                    "issue": "actuator_lane_silent",
                    "url": f"table:{table}",
                    "count": int(quiet_days),
                    "detail": (
                        f"{label}: `{table}` has written NOTHING for "
                        f"{quiet_days:.1f} days (last {ts_col} "
                        f"{last_write}), against a {max_quiet}-day cadence — "
                        f"but it holds {ever:,} rows, so this lane used to "
                        f"work. Nothing errored; the write simply stopped. "
                        f"Why it matters: {why}. This is the shape that hid "
                        f"the pair-code mint going quiet on 2026-07-07 for "
                        f"seven weeks while its dashboard reported 0 and "
                        f"read as poor conversion."
                    ),
                })
            except Exception:
                # a missing table/column is not a finding — fail closed per-lane
                continue
    except Exception:
        return []
    finally:
        _close(c)
    return findings


def check_approved_backlog_unpublished() -> list[dict]:
    """Items are being APPROVED while the publish side reads zero.

    Deliberately not "the queue is non-empty" — a queue with items in it is a
    working queue. It fires only when items are approved AND nothing has
    actually shipped for `quiet_hours`, which is the difference between a
    backlog and a stall.
    """
    findings: list[dict] = []
    c = _db()
    if c is None:
        return findings
    try:
        for (table, status_col, approved_val, posted_col, created_col,
             min_backlog, quiet_hours, label) in _PUBLISH_QUEUES:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        f"SELECT "
                        f"  COUNT(*) FILTER (WHERE {status_col} = %s) AS waiting, "
                        f"  MAX({posted_col}) AS last_published, "
                        f"  EXTRACT(EPOCH FROM (NOW() - MAX({posted_col})))/3600.0 AS quiet_h "
                        f"  FROM {table}",
                        (approved_val,)
                    )
                    row = cur.fetchone()
                if not row:
                    continue
                waiting, last_pub, quiet_h = row[0], row[1], row[2]
                if not waiting or waiting < min_backlog:
                    continue
                if quiet_h is None or float(quiet_h) <= quiet_hours:
                    continue
                findings.append({
                    "issue": "approved_backlog_unpublished",
                    "url": f"table:{table}",
                    "count": int(waiting),
                    "detail": (
                        f"{label}: {waiting} row(s) sit at "
                        f"{status_col}='{approved_val}' while nothing has "
                        f"published for {float(quiet_h):.1f}h (last "
                        f"{posted_col} {last_pub}). Approval is running and "
                        f"the drain is not. Check the publisher's own SELECT "
                        f"against these rows before assuming the queue is at "
                        f"fault — on 2026-08-24 the predicate matched fine "
                        f"and the real cause was upstream copy that the "
                        f"dedup gate kept rejecting."
                    ),
                })
            except Exception:
                continue
    except Exception:
        return []
    finally:
        _close(c)
    return findings


def check_failsoft_metric_null_streak() -> list[dict]:
    """A fail-soft metric that has returned NULL every day for a streak.

    This is the one that would have caught the LinkedIn follower bug on day
    one. The pattern it looks for is specifically a metric whose reader keeps
    RUNNING and keeps recording a row — so no freshness check fires, no cron
    alert fires, the deadman stays green — while the value itself is null
    every single time.

    A partition that has NEVER been non-null is escalated separately from one
    that used to work: never-worked means the reader has been broken since it
    shipped, which is a strictly worse finding than a recent regression and
    the one most likely to be dismissed as "not wired up yet".
    """
    findings: list[dict] = []
    c = _db()
    if c is None:
        return findings
    try:
        for (table, date_col, metric_col, part_col, reason_col,
             min_days, label) in _FAILSOFT_METRICS:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        f"SELECT {part_col} AS part, "
                        f"  COUNT(*) AS rows_written, "
                        f"  COUNT({metric_col}) AS non_null, "
                        f"  MAX({date_col}) FILTER (WHERE {metric_col} IS NOT NULL) AS last_real, "
                        f"  MODE() WITHIN GROUP (ORDER BY {reason_col}) AS top_reason "
                        f"  FROM {table} "
                        f" WHERE {date_col} > CURRENT_DATE - %s "
                        f" GROUP BY 1",
                        (max(min_days * 3, 30),)
                    )
                    rows = cur.fetchall() or []
                for part, written, non_null, last_real, top_reason in rows:
                    if not written or written < min_days:
                        continue
                    if non_null:
                        continue  # it works at least sometimes
                    # never-non-null inside the window
                    with c.cursor() as cur:
                        cur.execute(
                            f"SELECT COUNT({metric_col}) FROM {table} WHERE {part_col} = %s",
                            (part,)
                        )
                        ever_row = cur.fetchone()
                    ever = int((ever_row or [0])[0] or 0)
                    never = (ever == 0)
                    findings.append({
                        "issue": "failsoft_metric_null_streak",
                        "url": f"table:{table}:{part}",
                        "count": int(written),
                        "detail": (
                            f"{label}/{part}: the reader RAN and wrote "
                            f"{written} row(s), and `{metric_col}` was NULL "
                            f"in every one"
                            + (" — and has never once been non-null in the "
                               "table's whole history"
                               if never else
                               f" (last real value {last_real})")
                            + f". Recorded reason: {top_reason!r}. Because "
                            f"the row keeps getting written, no freshness "
                            f"check, cron alert or deadman entry fires — the "
                            f"lane looks alive and reports nothing. Read the "
                            f"reason literally before believing a docstring "
                            f"that predicts a different failure: on "
                            f"2026-08-24 an http_400 here was assumed for "
                            f"weeks to be the 403 scope gap the code comment "
                            f"anticipated, and was actually an un-encoded URN "
                            f"in the request path."
                        ),
                    })
            except Exception:
                continue
    except Exception:
        return []
    finally:
        _close(c)
    return findings


# Registered by routes/brain_consistency_radar.scan_all via the same
# append-with-try/except pattern as brain_honesty_reconciliation and
# brain_security_detectors, so an import failure here degrades the sweep
# rather than breaking it.
ACTUATOR_DETECTORS = (
    check_actuator_lane_silent,
    check_approved_backlog_unpublished,
    check_failsoft_metric_null_streak,
)
