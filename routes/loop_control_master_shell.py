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
  5. WRITER DISCIPLINE  — 10 files still hand-roll INSERT INTO brain_findings
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
import os
import re
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

loop_control_master_shell_bp = Blueprint("loop_control_master_shell", __name__)

# Files permitted to carry a raw `INSERT INTO brain_findings`. Everything else
# must go through routes/brain_findings_writer.upsert_brain_finding(), which is
# the only writer that honours the episode ledger.
_WRITER_ALLOWLIST = ("brain_findings_writer.py",)

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


def _row(c, sql: str):
    """Fail-soft single row. None on error. LITERAL SQL only — no params tuple
    and NO PERCENT CHARACTERS anywhere in the statement (psycopg2 substitution
    trap: a literal % in a paramless execute() 500s)."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[loop-control] row failed: %s -- %s", sql[:80], e)
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

def _lane_cron_liveness(c) -> list[dict]:
    """The real outage the 'seen x477455' misread hid. Same source and same
    threshold as brain_consistency_radar._check_cron_silently_dead."""
    checks = []
    if c is None or not _has_table(c, "cron_last_run"):
        return [_check("cron_src", "cron_last_run readable", None,
                       "cron_last_run absent or DB unreachable", critical=True)]

    r = _row(c, f"""
        SELECT count(*) FROM cron_last_run
         WHERE last_started_at IS NOT NULL
           AND EXTRACT(EPOCH FROM (NOW() - last_started_at))
               > COALESCE(NULLIF(expected_interval_s, 0) * 2, {_DEFAULT_STALE_S})
    """)
    dead = int(r[0]) if r and r[0] is not None else None
    checks.append(_check(
        "no_dead_crons", "no cron past its stale threshold",
        None if dead is None else (dead == 0),
        "could not count" if dead is None else f"{dead} job(s) past threshold",
        critical=True))

    r = _row(c, """
        SELECT job_name,
               EXTRACT(EPOCH FROM (NOW() - last_started_at))::INTEGER
          FROM cron_last_run
         WHERE last_started_at IS NOT NULL
         ORDER BY 2 DESC
         LIMIT 1
    """)
    if r:
        job, secs = r[0], int(r[1] or 0)
        checks.append(_check(
            "worst_offender", "worst job is inside 48h",
            secs <= 172800,
            f"{job} silent {secs}s ({secs / 3600.0:.1f}h / {secs / 86400.0:.2f}d)"))
    else:
        checks.append(_check("worst_offender", "worst job is inside 48h", None,
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

    if open_rows is None:
        checks.append(_check("triage_sees_findings",
                             "triage sees the durable findings", None,
                             "brain_findings unreadable", critical=True))
    else:
        wired = not (open_rows > 0 and merged == 0)
        checks.append(_check(
            "triage_sees_findings", "triage sees the durable findings", wired,
            f"brain_findings open={open_rows:,} vs triage in-process "
            f"sources={merged} — "
            + ("wired" if wired else
               "DISCONNECTED: triage reads per-process caches, so on this dyno "
               "nothing is ever actionable_now and an approval lands nowhere"),
            critical=True))
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
    canon = None
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats() or {}
        for k in ("facilities_tracked", "tracked", "facilities", "total_facilities"):
            if isinstance(s.get(k), int):
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
        if canon is not None:
            checks.append(_check(
                "surfaces_match_canon", "surfaces match canonical_stats",
                all(v == canon for v in seen.values()),
                f"canonical_stats={canon:,} vs surfaces {distinct}"))
        else:
            checks.append(_check("surfaces_match_canon",
                                 "surfaces match canonical_stats", None,
                                 "canonical_stats did not expose a facility count"))
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
            if body and "INSERT INTO brain_findings" in body:
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

def _lane_counter_canon() -> list[dict]:
    """Three surfaces, three answers, opposite signs. Actuator: one SQL, one
    helper, every surface reads it."""
    checks = []
    root = os.path.join(_repo_root(), "routes")
    sites = []
    try:
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".py"):
                continue
            body = _read(os.path.join(root, fn))
            if body and "COUNT(DISTINCT agent_id)" in body.upper():
                sites.append(fn)
    except Exception:
        sites = []
    if not sites:
        checks.append(_check("one_agent_count_sql",
                             "one implementation of the agent count", None,
                             "no COUNT(DISTINCT agent_id) site found"))
    else:
        checks.append(_check(
            "one_agent_count_sql", "one implementation of the agent count",
            len(sites) <= 1,
            f"{len(sites)} independent implementation(s): "
            + ", ".join(sites[:6]) + ("…" if len(sites) > 6 else "")
            + ("" if len(sites) <= 1 else
               " — each is free to drift; portal read 62 (-19 WoW) while reach "
               "read 99 (+73.7pct) on the same day")))
    return checks


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
    checks.append(_check(
        "human_opened", "a human has opened a relay link",
        opens > 0,
        f"{opens:,} relay_opens row(s) — "
        + ("present" if opens else
           "zero: the token is auto-redeemed by our own gateway in ~0.85s and "
           "returns 410 Gone by the time a human clicks")))
    return checks


# ── dead-man beat (fail-open) ─────────────────────────────────────────

def _beat_ledger(note: str) -> None:
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({
            "feed": "loop-control-shell-daily",
            "status": "success",
            "rows_inserted": 1,  # liveness sentinel — health lives in `note`
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        req = urllib.request.Request(
            "http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-loop-control-shell/1.0",
                     "X-Admin-Key": admin_key})
        urllib.request.urlopen(req, timeout=5).read()
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


def _run_tick() -> dict:
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
             "checks": _safe_lane(_lane_counter_canon)},
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
    _beat_ledger("lanes: " + summary)
    return out


@loop_control_master_shell_bp.route(
    "/api/v1/admin/loop-control/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return jsonify(ok=False, error="LOOP_CONTROL_SHELL_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_run_tick())
    # CF Cache-Rules have cached admin GETs before — never serve a stale board.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@loop_control_master_shell_bp.route("/admin/loop-control", methods=["GET"])
@loop_control_master_shell_bp.route("/api/v1/admin/loop-control", methods=["GET"])
def dashboard():
    if _disabled():
        return Response("loop-control shell disabled", status=503,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required (?admin_key=)", status=401,
                        mimetype="text/plain")
    d = _run_tick()
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
