"""Dead-man ledger + overdue view — the linchpin from the 2026-07-19 loop audit.

Every scheduled feed BEATS {feed,status,rows_inserted,max_content_date} here after
each run; an INDEPENDENT off-worker watcher (.github/workflows/deadman-watch.yml on
GitHub Actions — NOT the single APScheduler thread that drives all ~75 in-worker jobs)
reads /api/v1/ops/deadman and alarms (GH issue) when a loop hasn't SUCCEEDED within 2x
its cadence, inserted 0 rows across recent runs, or emitted a content date in the future.

"Cron green proves nothing": health here = "the loop ran AND inserted sane data",
never "a row exists" or "a timestamp looks recent".
"""
import os
import datetime
import logging

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("ingest_runs")
ingest_runs_bp = Blueprint("ingest_runs", __name__)

# Fallback cadence (hours) when a beat did not carry one. The authoritative registry
# lives in the watcher (tools/deadman/watch.py); this is only a safety net so a beat
# that forgets cadence_hours still gets a sane overdue threshold.
_DEFAULT_CADENCE_H = 48.0
# Statuses that are NOT themselves an alarm (the loop ran and was fine / intentionally idle).
_OK_STATUS = {"success", "ok", "idle", "no-op", "noop", "skipped", "",
              # 2026-07-24 (growthfix wave): an AFFIRMATIVE "the loop ran fine,
              # upstream simply had nothing new" — distinct from a broken loop
              # inserting 0. eia-pricing (monthly EIA data), osm-crawl (no new
              # tagged POIs) and competitor-gap (diff window saturated) burned
              # red for days on the >=3-zero-row rule while perfectly healthy.
              "no_new_data", "no-new-data"}

# Statuses that assert "zero rows is EXPECTED this run" — they reset the
# consecutive_zero counter instead of climbing it (trusting the producer,
# exactly as we already trust `status` itself).
_NO_NEW_DATA = {"no_new_data", "no-new-data"}

# ★2026-09-02 (D2): which `kinds` tokens mean LATE (the loop did not run) and
# which mean RED (it ran and reported a fault). /api/v1/ops/deadman publishes
# them as two counts; the in-DB mirrors (growthfix lane 3, loop-flywheel lane
# 9) split on the same line.
_LATE_KINDS = frozenset({"never_ran", "stale_age"})
_RED_KINDS = frozenset({"run_failed", "zero_rows", "future_content_date"})

# ★2026-09-03: WAITING ON AN UPSTREAM PERIOD THAT DOES NOT EXIST YET.
#
# The board's third word, and the only one that is neither an alarm nor a
# clean bill of health. It exists because `iso-intl` sat red while NINE of ten
# Japanese areas wrote fresh 30-minute fuel mix on the same tick: HEPCO had
# not posted the September monthly file, `degraded` is not in _OK_STATUS, and
# so the feed was indistinguishable from one that had actually broken.
#
# ★ IT IS NOT AN OK STATUS AND MUST NOT BE ADDED TO _OK_STATUS. A feed here is
# published in its own list with its own count and its own line in `basis`;
# the whole point is that a reader SEES the wait. Folding it into green would
# make the board say "fine" about a feed with a hole in its coverage, which is
# the failure this whole ledger was built to stop.
#
# ★ THE TEETH ARE UPSTREAM OF HERE, and they have to be, because this module
# cannot know any feed's publication calendar. The producer may only beat this
# status while the wait is bounded and unexpired — see
# routes/iso_jp_denkiyoho._UPSTREAM_MONTH_GRACE_DAYS. Past the window the
# producer beats the raw failure again and this feed is red by the ordinary
# rule, with no change here. So this is a state a feed can only OCCUPY
# briefly, never settle into. Guarded by tests/test_awaiting_upstream_state.py.
#
# The off-worker watcher (tools/deadman/watch.py) folds `unhealthy`, which is
# still late-or-red — so a wait does not page anyone, and an expired wait does.
_AWAITING_UPSTREAM = {"awaiting_upstream", "awaiting-upstream"}


def _dsn():
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    )


def _admin_ok():
    exp = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or ""
    got = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(exp) and got == exp


def _ensure(cur):
    cur.execute(
        """CREATE TABLE IF NOT EXISTS ingest_runs (
            feed              TEXT PRIMARY KEY,
            last_run          TIMESTAMPTZ,
            last_status       TEXT,
            rows_inserted     BIGINT,
            max_content_date  TIMESTAMPTZ,
            cadence_hours     NUMERIC,
            consecutive_zero  INT DEFAULT 0,
            note              TEXT,
            updated_at        TIMESTAMPTZ DEFAULT NOW()
        )"""
    )


def record_beat(feed, status="success", rows=None, mcd=None, cad=None,
                lr=None, note=None):
    """THE upsert. One row per feed; the consecutive_zero counter is authoritative HERE.

    LC6a: extracted verbatim from the POST /beat handler so the HTTP path and
    in-process callers share ONE implementation. Before this there were eleven
    hand-rolled loopback POSTs across the codebase and no shared code path at all,
    so any change to the zero-row alarm semantics had to be made in one place and
    hoped-for in ten others.

    Takes ALREADY-COERCED values — all HTTP concerns (auth, JSON parsing, type
    coercion, status codes) stay in the handler, which is why the wire behaviour is
    byte-identical. Raises on failure; callers decide whether to fail soft.
    """
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("no DATABASE_URL")
    # Sentinel so ON CONFLICT can tell "rows==0" (bump) from "rows unknown" (leave counter).
    rows_sig = rows if rows is not None else -1
    # no_new_data asserts zero rows is EXPECTED → feed the counter a positive
    # sentinel so it RESETS (clears any red built up before the producer
    # learned to report no_new_data) instead of climbing toward the alarm.
    if str(status).lower() in _NO_NEW_DATA and rows_sig == 0:
        rows_sig = 1
    with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """INSERT INTO ingest_runs
                   (feed, last_run, last_status, rows_inserted, max_content_date,
                    cadence_hours, consecutive_zero, note, updated_at)
               VALUES (%s, COALESCE(%s::timestamptz, NOW() ON CONFLICT DO NOTHING), %s, %s, %s::timestamptz, %s,
                       CASE WHEN %s = 0 THEN 1 ELSE 0 END, %s, NOW())
               ON CONFLICT (feed) DO UPDATE SET
                   last_run         = COALESCE(EXCLUDED.last_run, ingest_runs.last_run),
                   last_status      = EXCLUDED.last_status,
                   rows_inserted    = COALESCE(EXCLUDED.rows_inserted, ingest_runs.rows_inserted),
                   max_content_date = COALESCE(EXCLUDED.max_content_date, ingest_runs.max_content_date),
                   cadence_hours    = COALESCE(EXCLUDED.cadence_hours, ingest_runs.cadence_hours),
                   consecutive_zero = CASE WHEN %s = 0
                                           THEN ingest_runs.consecutive_zero + 1
                                           WHEN %s < 0 THEN ingest_runs.consecutive_zero
                                           ELSE 0 END,
                   note             = COALESCE(EXCLUDED.note, ingest_runs.note),
                   updated_at       = NOW()""",
            (feed, lr, status, rows, mcd, cad, rows_sig, note, rows_sig, rows_sig),
        )
        c.commit()


@ingest_runs_bp.route("/api/v1/admin/ingest-runs/beat", methods=["POST"])
def beat():
    """A scheduler (or the off-worker watcher) records its last SUCCESSFUL run.

    Body: {feed, status?, rows_inserted?, max_content_date?, cadence_hours?, last_run?, note?}
    Upsert — one row per feed, always the latest. consecutive_zero climbs while
    rows_inserted==0 and resets on any positive insert (the "rows=0 for N runs" alarm).
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    j = request.get_json(silent=True) or {}
    feed = str(j.get("feed") or "").strip()[:80]
    if not feed:
        return jsonify(ok=False, error="feed required"), 400
    status = str(j.get("status") or "success").strip()[:40]
    rows = j.get("rows_inserted")
    try:
        rows = int(rows) if rows is not None else None
    except (TypeError, ValueError):
        rows = None
    mcd = (str(j.get("max_content_date")).strip() or None) if j.get("max_content_date") else None
    cad = j.get("cadence_hours")
    try:
        cad = float(cad) if cad is not None else None
    except (TypeError, ValueError):
        cad = None
    lr = (str(j.get("last_run")).strip() or None) if j.get("last_run") else None
    note = (str(j.get("note")).strip()[:280] or None) if j.get("note") else None
    try:
        # LC6a: the upsert (and the consecutive_zero semantics) now live in
        # record_beat(). Everything above is unchanged HTTP concern; the wire
        # contract — status codes, JSON shape, fail-soft on DB error — is identical.
        record_beat(feed, status=status, rows=rows, mcd=mcd, cad=cad, lr=lr, note=note)
    except Exception as e:  # noqa: BLE001 — fail soft, ledger must never 500 a caller into a retry storm
        log.warning("ingest_runs beat failed for %s: %s", feed, e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, feed=feed)


def beat_feed(feed, status="success", rows_inserted=0, max_content_date=None,
              cadence_hours=24, base_url=None):
    """In-process beat helper (LC6). Fail-OPEN — never raises into the caller.

    HTTP-only by design. LC6a proposes a module-level record_beat() so the HTTP
    handler and in-process callers share ONE consecutive_zero code path; that has
    not shipped, and an import of a function that does not exist would raise on
    every call and be swallowed — the monitored loop would look instrumented while
    beating nothing. When LC6a lands, the direct-call fast path belongs HERE, and
    the ten hand-rolled copies of this POST (agent_request_writer.py,
    competitor_gap_crawler.py, tools/infra_fetch.py and the master shells) should
    migrate onto it.

    Loopback is correct for WEB-process callers (routes/*): that container serves
    the port. It is NOT correct from dchub-worker, which is the brain/scheduler and
    does not serve the API — worker-side callers must pass base_url explicitly
    (the internal Railway hostname), or the beat is silently dropped.
    """
    if os.environ.get("DEADMAN_BEAT_DISABLE") == "1":
        return
    import json as _json
    import urllib.request as _urlreq

    base = base_url or ("http://127.0.0.1:%s" % (os.environ.get("PORT") or "8080"))
    # LC6a: prefer the DIRECT call. This is the fast path the original spec wanted —
    # no loopback HTTP, no admin-key round trip, no rate limiter, no CF edge, and it
    # works in a background thread that has no request context. The HTTP fallback
    # below stays for callers with no DB reach (a worker-side beat pointed at
    # base_url, or a process with no DATABASE_URL).
    # Only skipped when the caller explicitly targeted another origin.
    if base_url is None:
        try:
            _mcd = max_content_date
            if _mcd is not None:
                _now = datetime.datetime.now(datetime.timezone.utc)
                if _mcd > _now:
                    _mcd = _now
                _mcd = _mcd.isoformat()
            record_beat(feed, status=status, rows=int(rows_inserted or 0),
                        mcd=_mcd, cad=cadence_hours)
            return
        except Exception as e:  # noqa: BLE001 — fall through to HTTP, never raise
            log.warning("deadman direct beat failed feed=%s (%s) — trying HTTP", feed, e)
    if max_content_date is not None:
        # Clamp: a content date >6h in the future is itself an overdue reason, so a
        # timezone bug upstream must not be able to poison the freshness signal.
        now = datetime.datetime.now(datetime.timezone.utc)
        if max_content_date > now:
            max_content_date = now
        max_content_date = max_content_date.isoformat()

    body = {"feed": feed, "status": status,
            "rows_inserted": int(rows_inserted or 0),
            "cadence_hours": cadence_hours}
    if max_content_date:
        body["max_content_date"] = max_content_date
    try:
        req = _urlreq.Request(
            base.rstrip("/") + "/api/v1/admin/ingest-runs/beat",
            data=_json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Admin-Key": os.environ.get("DCHUB_ADMIN_KEY") or "",
                     "User-Agent": "dchub-deadman-beat/1.0 (+https://dchub.cloud)"})
        _urlreq.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        # A dropped beat is a real gap — log at ERROR so it is greppable — but never
        # propagate: the monitored loop's own work matters more than its heartbeat.
        log.error("deadman beat DROPPED feed=%s status=%s err=%s", feed, status, e)


@ingest_runs_bp.route("/api/v1/admin/ingest-runs/purge", methods=["POST"])
def purge():
    """Remove feed(s) from the ledger — use when retiring a loop from the registry.

    Body: {feed} or {feeds:[...]}. Admin-gated.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    j = request.get_json(silent=True) or {}
    feeds = j.get("feeds") or ([j["feed"]] if j.get("feed") else [])
    feeds = [str(f).strip()[:80] for f in feeds if str(f).strip()]
    if not feeds:
        return jsonify(ok=False, error="feed or feeds[] required"), 400
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            _ensure(cur)
            cur.execute("DELETE FROM ingest_runs WHERE feed = ANY(%s)", (feeds,))
            deleted = cur.rowcount
            c.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("ingest_runs purge failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, deleted=deleted, feeds=feeds)


@ingest_runs_bp.route("/api/v1/ops/deadman", methods=["GET"])
def deadman():
    """PUBLIC read — the board. Two DIFFERENT faults, two different words:

    OVERDUE (late) — the loop did not run when it should have:
      • never ran,
      • last beat older than 2x its cadence (the classic dead-man).
    AWAITING_UPSTREAM (2026-09-03) — ran fine; the PERIOD does not exist yet:
      • the producer beat `awaiting_upstream`, meaning the only thing missing
        is a file the upstream has not published to anybody. Neither red nor
        green: published in its own list, with the producer's note naming the
        period and the grace window after which the same absence is a fault.
        See _AWAITING_UPSTREAM for why this is not an OK status.
    RED (ran, but reported a fault) — the loop fired on time and said so:
      • last_status is not a known-OK status (failure / lanes_failing / anomaly),
      • >=3 consecutive zero-row runs (loop fires but inserts nothing),
      • max_content_date is in the future (>6h ahead — the news-future-date rot class).

    `any_overdue` / `overdue_count` / `overdue[]` mean LATE only. `red_count` /
    `red[]` (feed names) / `any_red` mean ran-on-time-but-red. `unhealthy_count`
    is the union, so nothing that was alarming before is lost — it is just
    named. Per feed: `overdue`, `red`, `unhealthy` booleans + `kinds` tokens.

    ★2026-09-02 (D2): before this split a shell that ran 6 minutes ago and beat
    `lanes_failing` (#3365 made shells beat HEALTH) counted as "overdue", so
    11 on-time shells sat on the overdue list and three OTHER shells' cadence
    lanes failed because the board listed them — a self-referential red
    cascade that buried the one genuinely late feed.
    """
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            _ensure(cur)
            cur.execute(
                """SELECT feed, last_run, last_status, rows_inserted, max_content_date,
                          cadence_hours, consecutive_zero, note
                     FROM ingest_runs"""
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("ingest_runs deadman read failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500

    feeds, overdue, red, awaiting = [], [], [], []
    for feed, lr, st, ri, mcd, cad, cz, note in rows:
        cad_h = float(cad) if cad is not None else _DEFAULT_CADENCE_H
        # ★2026-08-25: `overdue` was ONE boolean carrying FIVE different faults,
        # and two of them mean opposite things operationally. A feed 36h into an
        # 18h cadence is LATE — nothing broke, it just has not run. A feed 7h
        # into a 16h cadence whose last run FAILED is not late at all; it ran
        # and it broke. `kinds` emits the rules as tokens; ★2026-09-02 the
        # boolean itself is split: `overdue` = LATE kinds only, `red` = ran-
        # but-faulted kinds, `unhealthy` = either. The off-worker watcher
        # (tools/deadman/watch.py) folds `unhealthy`, so the alarm is unchanged.
        reasons, kinds = [], []
        if lr is None:
            reasons.append("never ran")
            kinds.append("never_ran")
        else:
            age_h = (now - lr).total_seconds() / 3600.0
            if age_h > 2 * cad_h:
                reasons.append(f"last success {age_h:.0f}h ago (>2x cadence {cad_h:.0f}h)")
                kinds.append("stale_age")
        # ★ Three outcomes, not two. `awaiting_upstream` is NOT in _OK_STATUS
        # — it is carved out here explicitly so the carve-out is visible at
        # the point of judgement rather than hidden in a set literal.
        is_awaiting = bool(st) and st.lower() in _AWAITING_UPSTREAM
        if is_awaiting:
            reasons.append(f"status={st}")
            kinds.append("awaiting_upstream")
        elif st and st.lower() not in _OK_STATUS:
            reasons.append(f"status={st}")
            kinds.append("run_failed")
        # A feed whose LATEST beat affirms no_new_data is healthy even if the
        # counter climbed before the producer learned to report it.
        if cz and cz >= 3 and (st or "").lower() not in _NO_NEW_DATA:
            reasons.append(f"{cz} consecutive zero-row runs")
            kinds.append("zero_rows")
        if mcd and mcd > now + datetime.timedelta(hours=6):
            reasons.append(f"content date in the FUTURE ({mcd.date().isoformat()})")
            kinds.append("future_content_date")
        is_late = any(k in _LATE_KINDS for k in kinds)
        is_red = any(k in _RED_KINDS for k in kinds)
        # A wait never suppresses a red: a feed that is BOTH waiting on one
        # source and faulted on another is red, and says both.
        is_awaiting = is_awaiting and not is_red
        rec = {
            "feed": feed,
            "last_run": lr.isoformat() if lr else None,
            "status": st,
            "rows_inserted": ri,
            "cadence_hours": cad_h,
            "age_hours": round((now - lr).total_seconds() / 3600.0, 1) if lr else None,
            # LATE only (never_ran | stale_age) — see the docstring.
            "overdue": is_late,
            # Ran on time (or not) but the last beat carried a fault
            # (run_failed | zero_rows | future_content_date).
            "red": is_red,
            # Waiting on an upstream period nobody has published yet. Neither
            # an alarm nor a clean bill of health — published, never folded.
            "awaiting_upstream": is_awaiting,
            "unhealthy": is_late or is_red,
            # Machine-readable companion to `reasons`, same rules in the same
            # order, one token each: never_ran | stale_age | run_failed |
            # zero_rows | future_content_date. `stale_age` and `run_failed` are
            # the pair that was previously indistinguishable without prose.
            "kinds": kinds,
            "reasons": reasons,
        }
        if note:
            rec["note"] = note
        # ── triage: can an ENGINEER clear this red, or not? ───────────
        # ★2026-09-02. The board said "10 red" and treated every red alike.
        # Read lane by lane, most were not defects: loop_control/agent_identity
        # asserts "no one caller is >40pct" (chain-hire is 66.8%) and
        # agent_pay/demand asserts "a REAL agent has ever paid" (none has) —
        # both CORRECT, and no PR clears either. A board whose red is mostly
        # unclearable trains everyone to scroll past all of it, which is how
        # failover-canary sat red while the DR mirror drifted 112 commits
        # behind. Purely ADDITIVE: overdue/red/unhealthy/kinds are untouched,
        # so tools/deadman/watch.py and every existing reader are unaffected.
        # Fail-soft — a triage error must never take the board down with it.
        if is_red:
            try:
                from routes.lane_triage import triage_feed
                rec["triage"] = triage_feed(feed, note or "")
            except Exception as e:  # noqa: BLE001
                log.debug("lane triage failed for %s: %s", feed, e)
        feeds.append(rec)
        if is_late:
            overdue.append(rec)
        if is_red:
            red.append(rec)
        if is_awaiting:
            awaiting.append(rec)

    feeds.sort(key=lambda r: (not r["overdue"], not r["red"], r["feed"]))
    unhealthy_count = sum(1 for r in feeds if r["unhealthy"])
    # Roll the per-feed triage up. `red_lanes_unnamed` counts shells that
    # report HOW MANY lanes failed but not WHICH — those cannot be triaged
    # from the board at all, and that gap is published rather than counted
    # as zero.
    tri = [r["triage"] for r in feeds if isinstance(r.get("triage"), dict)]
    red_triage = {
        "code_actionable": sum(t.get("code_actionable_count", 0) for t in tri),
        "not_code": sum(t.get("not_code_count", 0) for t in tri),
        "unclassified": sum(t.get("unclassified_count", 0) for t in tri),
        "red_lanes_unnamed": sum(1 for t in tri
                                 if not t.get("lanes_named")
                                 and not t.get("triage_skipped")),
        "basis": ("failing lanes parsed from each red shell's own beat note "
                  "and classified by routes/lane_triage.LANE_TRIAGE, which "
                  "keys on WHO CLOSES IT. code_actionable = build|instrument "
                  "(an engineer). not_code = commercial|owner-flag|judgment|"
                  "diagnose — correct reds that no PR clears. "
                  "red_lanes_unnamed = shells whose note counts failures "
                  "without naming them; that is a blind spot, NOT a zero."),
    }
    resp = jsonify(
        ok=True,
        generated_at=now.isoformat(),
        tracked=len(feeds),
        any_overdue=bool(overdue),
        overdue_count=len(overdue),
        overdue=overdue,
        any_red=bool(red),
        red_count=len(red),
        red=[r["feed"] for r in red],
        # Published as its own list so it can never be mistaken for green.
        # Each entry carries the producer's own note, which names the period
        # being waited on and the grace window after which it becomes a fault.
        awaiting_upstream_count=len(awaiting),
        awaiting_upstream=[{"feed": r["feed"], "note": r.get("note"),
                            "status": r.get("status")} for r in awaiting],
        unhealthy_count=unhealthy_count,
        red_triage=red_triage,
        basis=("overdue = LATE (never ran, or last beat >2x cadence). red = ran "
               "but the last beat carried a fault (non-OK status, >=3 zero-row "
               "runs, future content date). unhealthy = either. A shell that "
               "beat lanes_failing six minutes ago is red, not overdue. "
               "awaiting_upstream = ran fine and is waiting on a period the "
               "UPSTREAM has not published to anyone yet — not green (coverage "
               "really is short, and the feed is listed with the period it is "
               "waiting on) and not red (nobody can act on it). The producer "
               "may only claim it inside a bounded grace window it declares "
               "itself; past that window it beats the raw failure and the feed "
               "is red here by the ordinary rule."),
        feeds=feeds,
    )
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def register_ingest_runs(app):
    try:
        app.register_blueprint(ingest_runs_bp)
        log.info("ingest_runs (dead-man ledger) registered")
    except Exception as e:  # noqa: BLE001
        log.warning("ingest_runs register failed: %s", e)


# ── GET /api/v1/ops/leader — is a singleton leader alive? ───────────────────
# ★2026-08-22: no surface outside the container could say whether ANY worker
# replica held the leader lock. After the 01:25Z redeploy none did for 48+
# minutes and every singleton loop idled while the board read green. This is
# the DB's own answer (pg_locks + pg_stat_activity), read-only, no secrets:
# the holder's pid/age/idle and whether it is a corpse by the same rule the
# keepalive loop uses to steal (util.leader_election). The replica answering
# this request is the WEB role, which never leads — so `this_replica` is
# reported for honesty, not as the verdict.
@ingest_runs_bp.route("/api/v1/ops/leader", methods=["GET"])
def leader_state():
    out = {"ok": True, "leader_present": None, "holder": None, "zombie_suspected": None,
           "stale_seconds": None, "this_replica_role": os.environ.get("DCHUB_ROLE") or "unset",
           "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
           "basis": "pg_locks JOIN pg_stat_activity on the singleton advisory lock; a live leader "
                    "pings every 30s, so holder idle_s >= stale_seconds means the holder is dead "
                    "and the keepalive loop will terminate it (DCHUB_LEADER_STEAL_STALE=0 disables)."}
    dsn = _dsn()
    if not dsn:
        out["ok"] = False; out["error"] = "no DATABASE_URL"
        return jsonify(out), 503
    try:
        from util.leader_election import lock_holder, STALE_SECONDS_DEFAULT, STALE_SECONDS_FLOOR
        try:
            stale = max(STALE_SECONDS_FLOOR, float(os.environ.get("DCHUB_LEADER_STALE_SECONDS", STALE_SECONDS_DEFAULT)))
        except Exception:
            stale = STALE_SECONDS_DEFAULT
        conn = psycopg2.connect(dsn, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                h = lock_holder(cur)
        finally:
            conn.close()
        out["stale_seconds"] = stale
        out["holder"] = h
        out["leader_present"] = bool(h) and (h.get("idle_s") is None or h["idle_s"] < stale)
        out["zombie_suspected"] = bool(h) and h.get("idle_s") is not None and h["idle_s"] >= stale
        return jsonify(out), 200
    except Exception as e:
        out["ok"] = False; out["error"] = str(e)[:200]
        return jsonify(out), 503
