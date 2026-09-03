"""
routes/loop_control_master_shell.py — Loop Control Master Shell (#48, 2026-08-02).

Born from the 2026-08-02 health sweep. The sweep's finding was not a broken
subsystem — it was a MISREAD one, and the misread steered three approved agenda
items at a problem that does not exist while the real outage ran unattended:

  ★ "cron_silently_dead @ /api/jobs/site-baseline (seen x477455)" is not
    477,455 occurrences. It is 477,455 SECONDS — 5.5 days since that cron last
    ran. brain_consistency_radar.py:7419 writes `"count": int(seconds_since)`.
    Live /api/v1/brain/findings/db-status: total_rows=3012 for the WHOLE table,
    and the cron rows read count=139436 / seen_count=1. Dedup was never broken.

  ★ The damage was not cosmetic. brain_work_selector.impact_weight() reads
    `count` as an occurrence signal, so a 6-figure duration hit the occurrence
    cap and RE-WON the agenda every tick — which is precisely the failure mode
    the 2026-06-30 r-brain-loop comment predicted for frontend_endpoint_slow and
    dedup_backlog_large. The guard (VALUE_NOT_COUNT_ISSUES) already existed;
    cron_silently_dead was simply never added to it.

So this shell watches the CONTROL LOOP itself: does the brain read its own
numbers correctly, does an approval reach an actuator, and do the surfaces and
counters tell the operator one story. One lane per item of the sweep's ranked
list (1-8).

  1. CRON LIVENESS      — the real outage the misread hid. Any job past 2x its
     declared interval (30h default) is dead. site-baseline was 5.5d silent.
  2. COUNT SEMANTICS    — `count` is per-detector free-form; only seen_count is
     a recurrence tally. Watches cron_silently_dead stay in the value-not-count
     allowlist so it can never buy agenda leverage with a duration again.
  3. TRIAGE WIRED       — /api/v1/brain/findings/triage merges the IN-PROCESS
     dchub_self_heal caches, not brain_findings. On the web dyno those caches
     are empty, so triage reported source_findings=0 against 3,012 durable
     rows: nothing is ever actionable_now, so an approval lands nowhere.
  4. SURFACE CANON      — five different facility counts served simultaneously
     (mcp.json/server-card 15,000+, one MCP instruction set 12,650+, another
     15,300+, /ai 15,792+, canonical_stats 22,045 tracked). Brain proposal
     #100067 (conf 0.66, survived refutation) is the render-from-canon fix.
  5. WRITER DISCIPLINE  — 10 files still hand-roll a raw findings INSERT
     instead of upsert_brain_finding(); tools/dedup_brain_findings_unique.py
     (which adds UNIQUE(issue,url)) is referenced by nothing and never ran.
  6. AGENT IDENTITY     — ~75 of 99 "real external agents" call essentially
     every tool ~50x (median 47, p90 62): an enumeration signature, not demand.
     Plus two bulk harvesters (datacolo 2 IPs/2,560 calls; smithery connect
     1 IP/1,851). The north star must split identified-platform from generic.
  7. COUNTER CANON      — three surfaces, three answers, opposite signs:
     agent portal 62 (-19 WoW), reach 99 (+73.7), funnel 99 (+208.4); and
     real_external_7d=2637 vs real_external_calls_7d=8641 in ONE payload.
  8. RELAY TWO-ARTIFACT — relay minted 404 -> human acted 0. A single-use token
     auto-redeemed by our own gateway in ~0.85s returns 410 Gone to the human.
     One token cannot serve both an agent and a human.

★ HONESTY RULE (inherited from Integrity #25 / Brain Ascension #28): a lane must
never read PASS when it could not check. An indeterminate check renders "?" and
the lane is not green. Lanes 6-8 introspect their columns at runtime and degrade
to "?" rather than guess a schema — a confident green on an unrun check is the
exact failure this shell exists to end.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.

Endpoints:
  GET/POST /api/v1/admin/loop-control/master-tick   JSON scoreboard (8 lanes)
  GET      /admin/loop-control                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/loop-control                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as the other master shells.
Kill: LOOP_CONTROL_SHELL_DISABLE=1
"""
from __future__ import annotations

import datetime
import logging
import ast
import os
import re
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

loop_control_master_shell_bp = Blueprint("loop_control_master_shell", __name__)

# Files permitted to carry a raw findings INSERT. Everything else must go
# through routes/brain_findings_writer.upsert_brain_finding(), which is the only
# writer that honours the episode ledger.
_WRITER_ALLOWLIST = ("brain_findings_writer.py",)

# The needle lane 5 greps for, assembled at import so this shell's OWN source
# cannot trip the insert-no-on-conflict regression lint (it greps for the
# literal) and so test_shell_is_read_only can assert the file contains no write
# verbs at all. This shell never writes — it only looks for writers.
_FINDINGS_INSERT_NEEDLE = "INSERT" + " INTO brain_findings"

# 30h — a daily-ish job silent past this is presumed dead. Mirrors
# brain_consistency_radar._DEFAULT_STALE_S so the two agree by construction.
_DEFAULT_STALE_S = 30 * 3600


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("LOOP_CONTROL_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers (mirror brain_ascension_master_shell) ──────────────────

def _conn():
    """Raw psycopg2 connection. None on failure. Deliberately OUTSIDE the app
    pool — one short-lived connection per tick."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[loop-control] db connect failed: %s", e)
        return None


def _row(c, sql: str, params=None):
    """Fail-soft single row. None on error.

    LITERAL SQL by default — and when `params` is omitted the old contract
    stands in full: NO PERCENT CHARACTERS anywhere in the statement, because a
    literal % in a paramless execute() is read as a substitution marker and
    500s.

    Pass `params` ONLY together with real placeholders. Once params is present
    psycopg2 does the substitution, so %s is a placeholder rather than a trap —
    but a literal % in that same statement must then be doubled to %%. Keep the
    two modes apart; do not add a placeholder to an existing literal query
    without re-reading it for stray percent signs."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[loop-control] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _rows(c, sql: str, params=None):
    """Fail-soft fetchall. None on error. Same two modes (and the same percent
    trap) as _row — read its docstring before adding a placeholder."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[loop-control] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _has_table(c, name: str) -> bool:
    r = _row(c, f"SELECT to_regclass('public.{name}')")
    return bool(r and r[0])


def _columns(c, table: str) -> set:
    """Live column set for a table/view. Empty set when unknown — callers must
    treat empty as INDETERMINATE, never as 'column absent'."""
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{table}'")
            return {r[0] for r in cur.fetchall()}
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return set()


def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, shown as '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """green only when something was actually decided and nothing failed."""
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in checks if k.get("critical")):
        return "?"
    if not [k for k in checks if k["pass"] is not None]:
        return "?"
    return "PASS"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


# ── lane 1: cron liveness ─────────────────────────────────────────────

def _retired_crons() -> set[str]:
    """Jobs deliberately retired, from the ONE list that already records them.

    ★ 2026-08-31: this lane's docstring has always claimed "same source and
    same threshold as brain_consistency_radar._check_cron_silently_dead", and
    on threshold it is right — but the radar also skips
    `_INTENTIONAL_STALE_CRONS` and this lane never did. So five jobs that were
    correctly retired, with reasons, kept this lane red for weeks:

        content-publish, global-intelligence, ai-outreach, ai-ecosystem
            retired 2026-08-07 — their only driver was heroic-reprieve's frozen
            dchub-scheduler-v4 zombie, whose every call 401'd after the 07-31
            key rotation
        energy-discovery
            retired 2026-08-21 — its five HIFLD ArcGIS sources are dead

    The decision was recorded and executed; only this reader disagreed. Import
    the set rather than re-declaring it — a second copy is how the two would
    drift apart again, and a duplicated allowlist is worse than none because
    both look authoritative.

    Fail-CLOSED on import error: an empty set means nothing is excluded, so the
    lane over-reports rather than silently certifying a genuinely dead cron."""
    try:
        from routes.brain_consistency_radar import _INTENTIONAL_STALE_CRONS
        return set(_INTENTIONAL_STALE_CRONS)
    except Exception:  # noqa: BLE001
        return set()


def _declared_intervals() -> dict:
    """routes/jobs_routes._JOB_INTERVALS — the interval a job DECLARES, read
    when its cron_last_run row has no expected_interval_s yet.

    ★2026-09-02 (D11): the weekly Railway arms (gas-refresh, site-baseline —
    dchub-jobs.yml `30 6 * * 0`) were judged against the 30h default six
    days a week because nothing had stamped the column. Same fallback as
    brain_consistency_radar._declared_interval_s so the two agree. Fail-
    closed: {} means no fallback, so the lane over-reports rather than
    certifying a genuinely dead cron."""
    try:
        from routes.jobs_routes import _JOB_INTERVALS
        return {k: int(v) for k, v in _JOB_INTERVALS.items() if v}
    except Exception:  # noqa: BLE001
        return {}


def _stale_threshold_s(expected_s, declared_s) -> int:
    """2x the stamped interval, else 2x the declared one, else the 30h default
    — the same three-step rule as brain_consistency_radar.check_cron_freshness."""
    exp = expected_s or declared_s
    return int(exp) * 2 if exp and exp > 0 else _DEFAULT_STALE_S


def _lane_cron_liveness(c) -> list[dict]:
    """The real outage the 'seen x477455' misread hid. Same source, same
    threshold (stamped interval, else the DECLARED interval, else 30h) AND
    the same retirement allowlist as
    brain_consistency_radar._check_cron_silently_dead."""
    checks = []
    if c is None or not _has_table(c, "cron_last_run"):
        return [_check("cron_src", "cron_last_run readable", None,
                       "cron_last_run absent or DB unreachable", critical=True)]

    _retired = _retired_crons()
    _declared = _declared_intervals()
    rows = _rows(c, """
        SELECT job_name, expected_interval_s,
               EXTRACT(EPOCH FROM (NOW() - last_started_at))::INTEGER
          FROM cron_last_run
         WHERE last_started_at IS NOT NULL
           AND NOT (job_name = ANY(%(retired)s))
    """, {"retired": sorted(_retired)})
    if rows is None:
        dead = None
    else:
        dead = []
        for job, exp, secs in rows:
            if secs is None:
                continue
            thr = _stale_threshold_s(exp, _declared.get(job))
            if int(secs) > thr:
                dead.append(f"{job} silent {int(secs) / 3600.0:.1f}h > {thr / 3600.0:.0f}h")
    checks.append(_check(
        "no_dead_crons", "no cron past its stale threshold",
        None if dead is None else (len(dead) == 0),
        "could not count" if dead is None else
        (f"{len(dead)} job(s) past threshold"
         + (": " + "; ".join(dead[:4]) if dead else "")),
        critical=True))

    r = _row(c, """
        SELECT job_name, expected_interval_s,
               EXTRACT(EPOCH FROM (NOW() - last_started_at))::INTEGER
          FROM cron_last_run
         WHERE last_started_at IS NOT NULL
           AND NOT (job_name = ANY(%(retired)s))
         ORDER BY 3 DESC
         LIMIT 1
    """, {"retired": sorted(_retired)})
    if r:
        job, exp, secs = r[0], r[1], int(r[2] or 0)
        # 48h, or the job's own stale threshold when it declares a longer
        # cadence — a weekly arm is not "the worst offender" at 65h.
        limit = max(172800, _stale_threshold_s(exp, _declared.get(job)))
        checks.append(_check(
            "worst_offender", "worst job is inside 48h (or its own threshold)",
            secs <= limit,
            f"{job} silent {secs}s ({secs / 3600.0:.1f}h / {secs / 86400.0:.2f}d), "
            f"limit {limit / 3600.0:.0f}h"))
    else:
        checks.append(_check("worst_offender",
                             "worst job is inside 48h (or its own threshold)", None,
                             "no cron rows readable"))
    return checks


# ── lane 2: count semantics ───────────────────────────────────────────

def _lane_count_semantics(c) -> list[dict]:
    """`count` is per-detector free-form. Only seen_count counts episodes.

    Actuator: routes/brain_work_selector.VALUE_NOT_COUNT_ISSUES — membership
    stops impact_weight() reading a duration as occurrences AND stops
    brain_enhancer rendering it as 'seen xN'."""
    checks = []
    try:
        from routes.brain_work_selector import VALUE_NOT_COUNT_ISSUES, is_value_not_count
        listed = "cron_silently_dead" in VALUE_NOT_COUNT_ISSUES
        checks.append(_check(
            "cron_in_allowlist", "cron_silently_dead is value-not-count",
            listed,
            ("listed — a duration can no longer buy agenda leverage"
             if listed else
             "MISSING from VALUE_NOT_COUNT_ISSUES: impact_weight() reads "
             "seconds-since-last-run as an occurrence count, so the finding "
             "re-wins the agenda every tick (this steered 3 approved items)"),
            critical=True))
        probe = is_value_not_count("cron_silently_dead")
        checks.append(_check(
            "classifier_agrees", "classifier resolves the issue string",
            bool(probe) is bool(listed),
            f"is_value_not_count('cron_silently_dead') -> {probe}"))
    except Exception as e:
        checks.append(_check("cron_in_allowlist",
                             "cron_silently_dead is value-not-count", None,
                             f"selector unimportable: {type(e).__name__}",
                             critical=True))

    if c is not None and _has_table(c, "brain_findings"):
        r = _row(c, """
            SELECT COALESCE(MAX(count), 0), COALESCE(MAX(seen_count), 0)
              FROM brain_findings
             WHERE issue = 'cron_silently_dead'
        """)
        if r:
            mx_count, mx_seen = int(r[0] or 0), int(r[1] or 0)
            # If `count` really were a tally it would track seen_count. A
            # 6-figure count beside a single-digit seen_count IS the proof
            # that count carries seconds.
            checks.append(_check(
                "count_is_duration", "count/seen_count divergence understood",
                True,
                f"max(count)={mx_count:,} vs max(seen_count)={mx_seen} — "
                f"count is a duration ({mx_count / 3600.0:.1f}h), seen_count "
                f"is the real episode tally"))
        r = _row(c, "SELECT count(*) FROM brain_findings")
        if r:
            total = int(r[0] or 0)
            checks.append(_check(
                "dedup_intact", "findings table is deduplicated",
                total < 50000,
                f"{total:,} rows total — the episode ledger is working; the "
                f"'477k duplicates' premise was the misread, not a defect"))
    return checks


# ── lane 3: triage wired to durable findings ──────────────────────────

def _lane_triage_wired(c) -> list[dict]:
    """An approval that reaches no actuator is journaling. triage merges the
    in-process dchub_self_heal caches; brain_findings is durable and shared."""
    checks = []
    open_rows = None
    if c is not None and _has_table(c, "brain_findings"):
        r = _row(c, """
            SELECT count(*) FROM brain_findings
             WHERE COALESCE(status, 'open') NOT IN ('resolved', 'wont_fix', 'dismissed')
        """)
        open_rows = int(r[0]) if r and r[0] is not None else None

    merged = 0
    reachable = False
    try:
        import dchub_self_heal as h
        reachable = True
        for fn_name in ("get_last_backend_findings", "get_last_funnel_findings",
                        "get_last_radar_findings", "get_last_html_findings",
                        "get_last_qa_findings", "get_last_asset_findings",
                        "get_last_api_contract_findings"):
            fn = getattr(h, fn_name, None)
            if not callable(fn):
                continue
            try:
                raw = fn() or {}
                merged += sum(len(v) for v in raw.values() if isinstance(v, dict))
            except Exception:
                continue
    except Exception as e:
        checks.append(_check("selfheal_import", "self-heal module importable",
                             None, f"{type(e).__name__}", critical=True))

    # ★ STRUCTURAL, not a count comparison. Comparing a shared-DB count to
    # THIS process's in-memory caches is red-by-construction on any
    # multi-dyno deploy: the web dyno never runs the scans, so merged==0
    # forever and the lane could never go green no matter what anyone fixed.
    # A permanently-red lane is noise, not a signal. What actually matters is
    # whether the triage endpoint has ANY durable source wired at all — that
    # is fixable, so the lane is greenable.
    src = _read(os.path.join(_repo_root(), "routes", "brain_v2_layer4.py"))
    if not src or "brain_findings_triage" not in src:
        checks.append(_check("triage_has_durable_source",
                             "triage reads the durable findings table", None,
                             "triage handler not found — cannot tell",
                             critical=True))
    else:
        i = src.index("brain_findings_triage")
        body = src[i:i + 2500]
        durable = "brain_findings" in body
        checks.append(_check(
            "triage_has_durable_source",
            "triage reads the durable findings table", durable,
            ("reads brain_findings" if durable else
             "reads ONLY the in-process dchub_self_heal caches, never "
             "brain_findings — on a dyno that ran no scan the work-queue is "
             "empty, so nothing is ever actionable_now and an approval "
             "reaches no actuator"),
            critical=True))
    if open_rows is not None:
        checks.append(_check(
            "findings_backlog", "durable backlog is observable", True,
            f"brain_findings open={open_rows:,}; triage in-process "
            f"sources={merged} (context, not a verdict — a web dyno is "
            f"expected to hold 0 scan results)"))
    if reachable:
        checks.append(_check("selfheal_import", "self-heal module importable",
                             True, "imported"))
    return checks


# ── lane 4: surface canon ─────────────────────────────────────────────

_FACILITY_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+)\s*\+?\s*(?:physical\s+)?"
                          r"(?:data[- ]?cent(?:er|re)\s+)?facilit", re.I)


def _lane_surface_canon(c) -> list[dict]:
    """One canon, many surfaces. Actuator: brain proposal #100067 — render every
    AI surface from canonical_stats at build time with a CI diff gate."""
    checks = []
    # ★★ CITABLE FIELD ONLY. /api/v1/stats/canonical states it outright:
    # "facilities_distinct = COUNT(DISTINCT canonical_slug) — distinct
    # BUILDINGS, and the field to cite. facilities_records =
    # facilities_tracked = COUNT(*) — raw source records, ~1.5x the
    # buildings". The first version of this lane compared surfaces against
    # facilities_tracked (~23.7k) and rendered FAIL — a HARMFUL RED: acting
    # on it would have published the raw discovery pile as the public
    # facility count, the exact over-claim canonical_stats exists to
    # prevent. Never compare public copy against a raw-record key.
    _CITABLE = ("facilities_distinct", "facilities_verified")
    _RAW_NEVER = ("facilities_tracked", "facilities_records", "facilities",
                  "total_facilities", "tracked")
    canon = None
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats() or {}
        for k in _CITABLE:
            if isinstance(s.get(k), int) and s[k] > 0:
                canon = s[k]
                break
    except Exception as e:
        logger.debug("[loop-control] canonical_stats unavailable: %s", e)

    root = _repo_root()
    candidates = [
        os.path.join(root, "static", "llms.txt"),
        os.path.join(root, "llms.txt"),
        os.path.join(root, "static", "llms-full.txt"),
        os.path.join(root, "dchub-frontend", "llms.txt"),
        os.path.join(root, "static", "mcp.json"),
        os.path.join(root, "mcp.json"),
    ]
    seen: dict[str, int] = {}
    for p in candidates:
        body = _read(p)
        if not body:
            continue
        m = _FACILITY_RE.search(body)
        if m:
            try:
                seen[os.path.relpath(p, root)] = int(m.group(1).replace(",", ""))
            except Exception:
                pass

    if not seen:
        checks.append(_check("surfaces_found", "AI surfaces readable on disk",
                             None,
                             "no surface file with a facility count found — "
                             "surfaces may be served from the frontend repo",
                             critical=True))
    else:
        distinct = sorted(set(seen.values()))
        checks.append(_check(
            "surfaces_agree", "all AI surfaces quote ONE facility count",
            len(distinct) == 1,
            "; ".join(f"{k}={v:,}" for k, v in sorted(seen.items()))
            + (f" — {len(distinct)} distinct values" if len(distinct) > 1 else "")))
        if canon is None:
            checks.append(_check(
                "surfaces_match_canon", "surfaces track the CITABLE canon",
                None,
                f"canonical_stats exposed none of {list(_CITABLE)} — refusing "
                f"to compare public copy against a raw-record key "
                f"({list(_RAW_NEVER)})", critical=True))
            return checks
        # Public figures are FLOORS that round DOWN, so a surface BELOW canon
        # is legal (just possibly stale) and a surface ABOVE canon is an
        # over-claim. Score those two failure modes separately.
        over = {k: v for k, v in seen.items() if v > canon}
        checks.append(_check(
            "no_overclaim", "no surface claims MORE than the citable canon",
            not over,
            f"citable canon (distinct buildings) = {canon:,}; "
            + ("no surface exceeds it" if not over else
               f"OVER-CLAIMING: {over} — these publish a facility count the "
               f"data does not support"),
            critical=True))
        # ★ 2026-08-31 — MEASURE AGAINST PINNED, NOT LIVE CANON.
        #
        # This compared every hand-maintained surface against the LIVE citable
        # count and called anything >500 behind "stale". Live canon moves
        # continuously (19,935 today) while these files carry ai_surface_canon
        # PINNED (18,500+), which is bumped periodically on purpose — so the
        # moment live drifts 500 past PINNED this lane goes red and STAYS red
        # until someone edits six files by hand. That is not a surface defect,
        # it is this lane asking hardcoded files to track a moving number.
        #
        # Worse, it contradicted its sibling: surface_truth_master_shell's
        # _acceptable_floor ACCEPTS PINNED (and anything up to PINNED x 1.10),
        # so the same files were simultaneously correct there and stale here.
        # Two guards disagreeing about what a file should say is how one of
        # them gets ignored.
        #
        # The relationship is two separate, separately-checkable claims:
        #   surfaces track PINNED   <- the files' job, checked per file
        #   PINNED tracks live      <- canon's job, ONE finding, not six
        _pinned = None
        try:
            from ai_surface_canon import PINNED as _P
            _pinned = int(str((_P.get("public") or {}).get("facilities") or "")
                          .replace(",", "").rstrip("+") or 0) or None
        except Exception as _pe:
            logger.debug("[loop-control] PINNED unavailable: %s", _pe)

        if _pinned is None:
            checks.append(_check(
                "floors_current", "surfaces track PINNED canon", None,
                "ai_surface_canon.PINNED unreadable — refusing to judge",
                critical=True))
        else:
            # Same tolerance as the sibling guard: PINNED itself, or anything
            # up to 10% above it (a live-healed surface is not stale).
            _lo, _hi = _pinned, int(_pinned * 1.10)
            stale = {k: v for k, v in seen.items() if v < _lo or v > _hi}
            checks.append(_check(
                "floors_current", "every surface floor is within the PINNED band",
                not stale,
                f"PINNED {_pinned:,} (band {_lo:,}-{_hi:,}); "
                + ("all floors in band" if not stale else
                   f"out of band: {stale}")))
            # The OTHER half, reported once rather than per file: PINNED itself
            # falling behind live is a real, actionable finding — bump canon.
            _drift = canon - _pinned
            checks.append(_check(
                "pinned_tracks_live", "PINNED is not far behind the live count",
                _drift <= max(1500, int(_pinned * 0.10)),
                f"live {canon:,} vs PINNED {_pinned:,} (drift {_drift:,}) — "
                + ("within tolerance" if _drift <= max(1500, int(_pinned * 0.10))
                   else "bump ai_surface_canon.PINNED; the surfaces are correct, "
                        "the pin is behind")))
    return checks


# ── lane 5: writer discipline ─────────────────────────────────────────

def _lane_writer_discipline(c) -> list[dict]:
    """One writer, one index. Actuators: upsert_brain_finding() and
    tools/dedup_brain_findings_unique.py (written 2026-07-18, wired to nothing)."""
    checks = []
    root = _repo_root()
    offenders = []
    for sub in ("routes", "."):
        base = os.path.join(root, sub)
        try:
            names = sorted(os.listdir(base))
        except Exception:
            continue
        for fn in names:
            if not fn.endswith(".py") or fn in _WRITER_ALLOWLIST:
                continue
            p = os.path.join(base, fn)
            if not os.path.isfile(p):
                continue
            body = _read(p)
            if body and _FINDINGS_INSERT_NEEDLE in body:
                offenders.append(os.path.relpath(p, root))
    offenders = sorted(set(offenders))
    checks.append(_check(
        "single_writer", "only the canonical writer INSERTs findings",
        len(offenders) == 0,
        "clean" if not offenders else
        f"{len(offenders)} hand-rolled writer(s) bypass the episode ledger: "
        + ", ".join(offenders[:6]) + ("…" if len(offenders) > 6 else "")))

    if c is not None:
        r = _row(c, """
            SELECT count(*) FROM pg_indexes
             WHERE tablename = 'brain_findings'
               AND indexname = 'brain_findings_issue_url_uniq'
        """)
        if r is not None:
            have = int(r[0] or 0) > 0
            checks.append(_check(
                "unique_index", "UNIQUE(issue,url) exists on brain_findings",
                have,
                "present" if have else
                "absent — tools/dedup_brain_findings_unique.py adds it and is "
                "referenced by nothing; raw INSERTs can still race in dupes"))
    return checks


# ── lane 6: agent identity split ──────────────────────────────────────

def _lane_agent_identity(c) -> list[dict]:
    """~75 of 99 'real external agents' swept every tool ~50x. Enumeration is
    not demand. Actuator: split the north star before reporting growth."""
    checks = []
    if c is None or not _has_table(c, "mcp_calls_identity"):
        return [_check("identity_view", "mcp_calls_identity readable", None,
                       "identity view absent or DB unreachable", critical=True)]
    cols = _columns(c, "mcp_calls_identity")
    if not cols:
        return [_check("identity_view", "mcp_calls_identity introspectable",
                       None, "column list unavailable — refusing to guess",
                       critical=True)]
    tcol = next((x for x in ("created_at", "ts", "called_at", "timestamp",
                             "request_time") if x in cols), None)
    pcol = next((x for x in ("platform", "client", "client_name") if x in cols), None)
    if not tcol or not pcol:
        return [_check("identity_view", "identity view has time+platform",
                       None,
                       f"need a time and platform column; have {sorted(cols)[:12]}",
                       critical=True)]

    r = _row(c, f"""
        SELECT count(*) FROM mcp_calls_identity
         WHERE is_real_external AND {tcol} > NOW() - INTERVAL '7 days'
    """)
    total = int(r[0]) if r and r[0] is not None else None
    r = _row(c, f"""
        SELECT {pcol}, count(*) FROM mcp_calls_identity
         WHERE is_real_external AND {tcol} > NOW() - INTERVAL '7 days'
         GROUP BY 1 ORDER BY 2 DESC LIMIT 1
    """)
    if total and r:
        top_p, top_n = str(r[0]), int(r[1] or 0)
        share = (top_n * 100.0 / total) if total else 0.0
        checks.append(_check(
            "no_single_caller_dominates", "no one caller is >40pct of real calls",
            share <= 40.0,
            f"top platform '{top_p}' = {top_n:,}/{total:,} ({share:.1f}pct) — "
            + ("broad-based" if share <= 40.0 else
               "the north star is carried by one caller; growth read off this "
               "is a scan wave, not demand")))
    else:
        checks.append(_check("no_single_caller_dominates",
                             "no one caller is >40pct of real calls", None,
                             "7d window unreadable"))
    return checks


# ── lane 7: counter canon ─────────────────────────────────────────────

def _lane_counter_canon(c=None) -> list[dict]:
    """Do the agent counters AGREE? Measured on values, not on file counts.

    ★ REWRITTEN 2026-09-03. Two previous versions both counted TEXT. The
    first grepped for a fixed string, matched itself, and inflated the count
    forever. The second widened to a regex over the repo and reported a
    CANDIDATE list — honest about its own weakness, and its note said so
    outright: "a grep hit is not proof two counters DISAGREE". That is an
    accurate disclaimer on a check that therefore could never pass, and a
    lane that cannot pass is a lane everyone learns to scroll past.

    The lane's name promises canon over VALUES, so it now measures values:
    it runs THE canonical query and, in the same scan, the two retired bases
    that caused the 2026-07-31 incident (three surfaces, three answers on one
    day: badge 64, widget 95, funnel 129). The spread between them is
    published — it is the size of the error a surface still on an old basis
    would print. Then it asserts, by AST rather than by substring, that the
    public emitters actually CALL the canonical helper."""
    checks = []
    close_after = False
    if c is None:
        c = _conn()
        close_after = True
    try:
        # ── the canonical value ───────────────────────────────────────
        if c is None or not _has_table(c, "mcp_calls_identity"):
            checks.append(_check(
                "canon_value", "the canonical agent count is readable", None,
                "mcp_calls_identity unreadable — a failed read is UNKNOWN, "
                "never a zero and never a pass", critical=True))
            return checks
        cols = _columns(c, "mcp_calls_identity")
        tcol = next((x for x in ("created_at", "ts", "called_at", "timestamp",
                                 "request_time") if x in cols), None)
        if not tcol:
            checks.append(_check(
                "canon_value", "identity view has a time column", None,
                f"no usable time column; have {sorted(cols)[:10]}",
                critical=True))
            return checks

        alts, alt_names = [], []
        for cand, label in (("ip_address", "raw ip_address"),
                            ("session_id", "session_id")):
            if cand in cols:
                alts.append(f"COUNT(DISTINCT {cand})")
                alt_names.append(label)
        sel = ", ".join(["COUNT(DISTINCT agent_id)"] + alts)
        r = _row(c, f"""
            SELECT {sel} FROM mcp_calls_identity
             WHERE is_public_ip AND is_real_external
               AND {tcol} > NOW() - INTERVAL '7 days'
        """)
        if not r:
            checks.append(_check(
                "canon_value", "the canonical agent count is readable", None,
                "the canonical query did not return — UNKNOWN", critical=True))
            return checks

        canon = int(r[0] or 0)
        others = [int(x or 0) for x in r[1:]]
        checks.append(_check(
            "canon_value", "the canonical agent count is readable", True,
            f"{canon} distinct agents (7d) on the canonical basis "
            f"(mcp_calls_identity, is_public_ip AND is_real_external)"))

        # ── how wrong an old basis would print, right now ─────────────
        if others:
            spread = ", ".join(f"{n}={v}" for n, v in zip(alt_names, others))
            worst = max(abs(v - canon) for v in others)
            checks.append(_check(
                "canon_spread", "retired counting bases are published for "
                "comparison, not scored", None,
                f"canonical={canon} vs {spread} — a surface still on a "
                f"retired basis would print up to {worst} agents off today. "
                f"Report-only: these SHOULD differ; that is why the canonical "
                f"basis exists (2026-07-31: badge 64 / widget 95 / funnel 129 "
                f"on one day)"))

        # ── centralisation, by AST call-site not by substring ─────────
        emitters = ("flask_mcp_endpoints.py", "routes/ai_reach.py",
                    "routes/weekly_series.py")
        missing, unreadable = [], []
        for rel in emitters:
            body = _read(os.path.join(_repo_root(), rel))
            if not body:
                unreadable.append(rel)
                continue
            try:
                tree = ast.parse(body)
            except SyntaxError:
                unreadable.append(rel)
                continue
            # ★ Resolve ALIASES. flask_mcp_endpoints imports the helper as
            # `_canonical_activity_sql`; matching the bare name would have
            # reported the repo's most important emitter as non-compliant.
            # Caught by this lane's own guard on the first run.
            names = {"canonical_external_activity_sql"}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom):
                    for a in n.names:
                        if a.name == "canonical_external_activity_sql" and a.asname:
                            names.add(a.asname)
            calls = any(
                isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", None)) in names
                for n in ast.walk(tree))
            if not calls:
                missing.append(rel)
        if unreadable:
            checks.append(_check(
                "canon_emitters", "public agent-count emitters call the one "
                "helper", None,
                f"could not parse {', '.join(unreadable)} — UNKNOWN, not a pass",
                critical=True))
        else:
            checks.append(_check(
                "canon_emitters", "public agent-count emitters call the one "
                "helper", not missing,
                (f"{', '.join(missing)} publish an agent count without calling "
                 f"canonical_external_activity_sql()"
                 if missing else
                 f"all {len(emitters)} emitters call "
                 f"canonical_external_activity_sql() (AST call-site, not a "
                 f"substring — a mention in a comment does not count)")))
        return checks
    finally:
        if close_after and c is not None:
            try:
                c.close()
            except Exception:
                pass


# ── lane 8: relay two-artifact ────────────────────────────────────────

def _lane_relay_two_artifact(c) -> list[dict]:
    """One single-use token cannot serve both an agent and a human. Actuator:
    mint TWO artifacts — an agent-redeemable key and a durable human link."""
    checks = []
    if c is None or not _has_table(c, "relay_opens"):
        return [_check("relay_table", "relay_opens readable", None,
                       "relay_opens absent or DB unreachable", critical=True)]
    r = _row(c, "SELECT count(*) FROM relay_opens")
    if r is None:
        return [_check("relay_table", "relay_opens readable", None,
                       "count failed", critical=True)]
    opens = int(r[0] or 0)

    # ★ A bare count is a FALSE GREEN. The 2026-08-02 sweep's premise is that
    # relay_opens holds ONLY our own probe traffic (human-simulated /
    # dchub-ops-verify), so "2 rows" read PASS while zero humans had ever
    # opened a link — the exact flattering-zero this shell exists to catch.
    # Separate real opens from probes, and render "?" rather than PASS when
    # the table carries no column that can tell them apart.
    cols = _columns(c, "relay_opens")
    marker = next((x for x in ("source", "user_agent", "ua", "note", "kind",
                               "opened_by", "channel", "referer") if x in cols), None)
    if not marker:
        checks.append(_check(
            "human_opened", "a REAL human has opened a relay link", None,
            f"{opens:,} row(s), but relay_opens carries no column that "
            f"separates a human open from a probe (have {sorted(cols)[:10]}) "
            f"— refusing to score probe rows as humans", critical=True))
        return checks

    # position(... in ...) = 0 keeps this free of percent literals (LIKE would
    # need '%', which 500s a paramless psycopg2 execute).
    r = _row(c, f"""
        SELECT count(*) FROM relay_opens
         WHERE position('dchub-ops-verify' in lower(coalesce({marker}::text, ''))) = 0
           AND position('human-simulated' in lower(coalesce({marker}::text, ''))) = 0
           AND position('probe' in lower(coalesce({marker}::text, ''))) = 0
           AND position('ops-verify' in lower(coalesce({marker}::text, ''))) = 0
    """)
    if r is None:
        checks.append(_check("human_opened", "a REAL human has opened a relay link",
                             None, f"{opens:,} row(s); probe filter on "
                                   f"{marker!r} failed", critical=True))
        return checks
    real = int(r[0] or 0)
    checks.append(_check(
        "human_opened", "a REAL human has opened a relay link",
        real > 0,
        f"{real:,} non-probe of {opens:,} total relay_opens (probe filter on "
        f"{marker!r}) — "
        + ("present" if real else
           "every row is our own probe traffic. ★CORRECTED 2026-08-03: the "
           "old text here blamed a single-use token auto-redeemed in ~0.85s. "
           "That describes the upgrade CLAIM (2,155 minted / 2,146 consumed "
           "by agents / 0 humans) — NOT this relay. routes/human_relay.py is "
           "STATELESS at mint, HMAC-validated on open, and renders a useful "
           "page even for a bad token, so it cannot return 410. It was built "
           "in July as the FIX for the claim problem. Both envelope causes "
           "were then fixed too (MCP f9c965d, 0aab503, live 2026-07-28). Per "
           "check_relay_opens.py, a still-zero reading is the experiment "
           "returning its OTHER answer: envelope shape is ruled out and the "
           "constraint is agent behaviour — STOP tuning the envelope and work "
           "the human-present channel. Run check_relay_opens.py for the "
           "verdict; do not re-fix the token.")))
    return checks


# ── dead-man beat (fail-open) ─────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    """Best-effort beat into the SHIPPED ingest_runs ledger. NEVER raises."""
    try:
        import json as _json
        body = _json.dumps({
            "feed": "loop-control-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint urllib-request-on-railway)
        _rq.post("http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-loop-control-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[loop-control] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn, *a) -> list[dict]:
    """A lane that CRASHES must render '?' (indeterminate), never 500 the tick."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       f"lane crashed: {type(e).__name__}: {str(e)[:120]}",
                       critical=True)]


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    c = _conn()
    try:
        lanes = [
            {"id": "cron_liveness", "name": "1 · cron liveness",
             "checks": _safe_lane(_lane_cron_liveness, c)},
            {"id": "count_semantics", "name": "2 · count semantics (value ≠ tally)",
             "checks": _safe_lane(_lane_count_semantics, c)},
            {"id": "triage_wired", "name": "3 · triage wired to findings",
             "checks": _safe_lane(_lane_triage_wired, c)},
            {"id": "surface_canon", "name": "4 · surface canon (one number)",
             "checks": _safe_lane(_lane_surface_canon, c)},
            {"id": "writer_discipline", "name": "5 · findings writer discipline",
             "checks": _safe_lane(_lane_writer_discipline, c)},
            {"id": "agent_identity", "name": "6 · agent identity split",
             "checks": _safe_lane(_lane_agent_identity, c)},
            {"id": "counter_canon", "name": "7 · counter canon (one SQL)",
             "checks": _safe_lane(_lane_counter_canon, c)},
            {"id": "relay_two_artifact", "name": "8 · relay two-artifact",
             "checks": _safe_lane(_lane_relay_two_artifact, c)},
        ]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join(f"{ln['id']}={ln['verdict']}" for ln in lanes)
    out = {
        "ok": True,
        "shell": "loop-control-48",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


@loop_control_master_shell_bp.route(
    "/api/v1/admin/loop-control/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="LOOP_CONTROL_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_run_tick(beat=(request.method == "POST")))
    # CF Cache-Rules have cached admin GETs before — never serve a stale board.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@loop_control_master_shell_bp.route("/admin/loop-control", methods=["GET"])
@loop_control_master_shell_bp.route("/api/v1/admin/loop-control", methods=["GET"])
def dashboard():
    if _disabled():
        return Response("loop-control shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required (?admin_key=)", status=401,
                        mimetype="text/plain")
    d = _run_tick(beat=False)
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in d["lanes"]:
        rows.append(
            f"<tr><td class='lane'>{_esc(ln['name'])}</td>"
            f"<td style='color:{color.get(ln['verdict'], '#eab308')}'>"
            f"<b>{_esc(ln['verdict'])}</b></td><td>"
            + "<br>".join(
                ("&#9989; " if k["pass"] is True else
                 ("&#10060; " if k["pass"] is False else "&#10068; "))
                + _esc(k["name"]) + " — <span class='d'>" + _esc(k["detail"])
                + "</span>" for k in ln["checks"])
            + "</td></tr>")
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Loop Control Shell #48</title>"
        "<style>body{background:#0b1020;color:#e2e8f0;font:14px/1.5 "
        "-apple-system,Segoe UI,sans-serif;margin:2rem}table{border-collapse:"
        "collapse;width:100%;max-width:1100px}td{border-bottom:1px solid "
        "#1e293b;padding:.6rem .8rem;vertical-align:top}.lane{white-space:"
        "nowrap;font-weight:600}.d{color:#94a3b8}h1{font-size:1.2rem}"
        "small{color:#64748b}</style>"
        "<h1>Loop Control Master Shell #48</h1>"
        "<small>generated " + _esc(d["generated_at"]) + " · read-only · "
        "refreshes 60s · born from the 2026-08-02 health sweep "
        "(count=SECONDS misread steered 3 approved agenda items) · "
        "kill LOOP_CONTROL_SHELL_DISABLE=1</small>"
        "<table>" + "".join(rows) + "</table>")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def register_loop_control_master_shell(app):
    app.register_blueprint(loop_control_master_shell_bp)
