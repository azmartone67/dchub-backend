"""PM Brief — the 24x7 project-manager chase loop (2026-08-14).

The boards already exist and already carry the work orders (born-red lanes are
work orders BY DESIGN). This module is the thing that was missing: it READS
every board daily, PERSISTS one snapshot per day, DIFFS against yesterday,
CHASES what has been red too long, and puts the top three actions in front of
the owner.

  GET  /api/v1/admin/pm-brief          latest brief (markdown; ?date=YYYY-MM-DD,
                                       ?list=1 for history, ?format=json)
  POST /api/v1/admin/pm-brief/run      run a collection now (admin, idempotent
                                       per day, leader-gated advisory lock)
  POST /api/v1/admin/pm-brief/dismiss  owner records a dismissal {item, reason}
  kill: PM_BRIEF_DISABLE=1  (renders 404, never 5xx — a disabled diagnostic
                             must not trip the CF failover to stale Render)

★ THE CONSTRAINT THAT DEFINES THE TOOL: THE PM NEVER MARKS ITS OWN ITEMS DONE.
It has NO write access to any board. Enforced two ways, both mutation-tested
in tests/test_pm_brief.py:
  1. Every DB statement in this module goes through _guarded_execute(), which
     parses the statement and raises PMBriefWriteViolation on any write whose
     target table is not in _ALLOWED_WRITE_TABLES (pm_brief_* only).
  2. The only HTTP verb this module can emit toward any BOARD is GET
     (_read_json). The single deliberate exception is _beat_ledger(): a POST
     to the module's OWN liveness feed on /api/v1/admin/ingest-runs/beat
     (Lens A1 — a dead collector must alarm the deadman board). The guard
     test statically pins the exception: exactly one raw cursor.execute
     (inside _guarded_execute), no requests.post/put/delete/patch, no writes
     naming a non-pm_brief table, and the only POST-capable code lives inside
     _beat_ledger and can only hit the ingest-runs beat URL.
An item leaves the brief ONLY because the underlying board measured it green,
or because the OWNER dismissed it via POST /dismiss — and dismissed items
still render, collapsed, with the reason and date.

Three-valued always (mirrors the master shells): a board that cannot be read
renders UNMEASURED with the reason — never skipped silently, never guessed,
never counted as red or green.

Delivery: the owner reads GET /api/v1/admin/pm-brief. Email/Slack are
deliberately NOT wired — channel choice is an owner decision. Hook point: the
markdown returned by _build_brief() / stored in pm_brief_snapshots.brief_md.
NOTE (CF edge): /api/v1/admin/* GETs can be cached by CF Rule #3
(override_origin ignores no-store) — read with a cache-buster (?_=ts), and the
daily cron POSTs Railway-direct to dodge the 15s edge timeout on admin routes.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.request as _urlreq

from flask import Blueprint, Response, jsonify, request

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import _admin_ok, _conn  # noqa: F401

pm_brief_bp = Blueprint("pm_brief", __name__)

_UA = "dchub-pm-brief/1.0 (+https://dchub.cloud)"
_TIMEOUT = 45  # freshness shell fans out to external reads; be patient
# ★ 814202602, NOT ...01 (2026-08-14, live L3 finding): the first deploy used
# a SESSION advisory lock (pg_try_advisory_lock). Behind the pooled Postgres
# endpoint with autocommit, lock and unlock can land on DIFFERENT backend
# sessions, so the lock leaked and stayed held after both callers returned —
# the very next collection "succeeded" with skipped=another_writer_holds_lock
# and wrote nothing (vacuous green). The fix is a TRANSACTION-scoped lock
# (pg_try_advisory_xact_lock) inside one explicit transaction — pgbouncer
# pins a transaction to one backend, and the lock cannot outlive the commit.
# The id is bumped because xact and session locks share a lockspace and the
# old id may sit leaked in a pooled backend until it recycles.
_LOCK_ID = 814202602
# Lens B2: the brief is a COMPLETE red inventory, however terse — the cap
# exists to bound pathology (a reader melting down into hundreds of lanes),
# not to trim the inventory. Raised 60 -> 100 when STANDING REDS landed; the
# truncation line still names ?format=json as the untruncated source.
_MAX_LINES = 100

FAIL, PASS, UNKNOWN = "FAIL", "PASS", "?"


def _disabled() -> bool:
    return (os.environ.get("PM_BRIEF_DISABLE") or "").strip() == "1"


def _base() -> str:
    """Self-call base. Localhost-direct by default: no CF cache, no CF 15s
    admin timeout, no UA blocking (urllib/curl prefixes are 1010'd pre-worker).
    """
    return (os.environ.get("PM_BRIEF_BASE")
            or os.environ.get("DCHUB_INTERNAL_API")
            or "http://localhost:8080").rstrip("/")


def _admin_key() -> str:
    return ((os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())


# ── the ONLY http reader — GET, never anything else ─────────────────────────
def _read_json(url: str, admin: bool = False):
    """Returns (payload, None) or (None, reason). NEVER raises, NEVER writes.

    This is deliberately the module's single HTTP entry point and it only
    issues GET. The no-write guard test asserts no other verb exists here.
    Status is checked BEFORE parsing: our API answers errors with well-formed
    JSON, and parsing an error body would render a content failure on a board
    we merely could not read (freshness shell, same rule).
    """
    try:
        import requests as _rq
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        if admin:
            key = _admin_key()
            if not key:
                return None, "no admin key in env"
            headers["X-Admin-Key"] = key
        sep = "&" if "?" in url else "?"
        bust = f"{sep}_={int(time.time())}"
        r = _rq.get(url + bust, headers=headers, timeout=_TIMEOUT)
        if not 200 <= r.status_code < 300:
            return None, f"HTTP {r.status_code}"
        return json.loads(r.content.decode("utf-8", "replace")), None
    except Exception as e:  # noqa: BLE001 — any failure is UNMEASURED
        return None, f"{type(e).__name__}"


# ── db: guarded writes only ─────────────────────────────────────────────────
class PMBriefWriteViolation(Exception):
    """Raised when this module attempts a DB write outside its own tables."""


_ALLOWED_WRITE_TABLES = {"pm_brief_snapshots", "pm_brief_dismissals"}
_WRITE_RE = re.compile(
    r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+"
    r"EXISTS)?|ALTER\s+TABLE|TRUNCATE(?:\s+TABLE)?|DROP\s+TABLE(?:\s+IF\s+"
    r"EXISTS)?)\s+([a-zA-Z0-9_\.\"]+)", re.IGNORECASE)


def guard_check_sql(sql: str) -> None:
    """Raises PMBriefWriteViolation on any write statement whose target table
    is not a pm_brief_* table. Pure function so the mutation test can hit it
    directly on the real run path."""
    m = _WRITE_RE.match(sql or "")
    if m:
        table = m.group(2).strip().strip('"').split(".")[-1].lower()
        if table not in _ALLOWED_WRITE_TABLES:
            raise PMBriefWriteViolation(
                f"pm_brief attempted a write to '{table}' — the PM has no "
                f"write access to any board (allowed: "
                f"{sorted(_ALLOWED_WRITE_TABLES)})")


def _guarded_execute(cur, sql: str, params=None):
    """★ THE no-write guard. Every statement this module runs goes through
    here — the static half of the guard test asserts the raw cursor.execute
    below is the ONLY one in this file. Any write outside pm_brief_* raises
    BEFORE the cursor sees the statement.

    Mutation-tested: point any call site at a board table and
    tests/test_pm_brief.py goes red, and the runtime raises first.
    """
    guard_check_sql(sql)
    cur.execute(sql, params) if params is not None else cur.execute(sql)  # noqa: E501 — sole raw execute (guard-scanned)
    return cur


def _ensure_tables(cur) -> None:
    _guarded_execute(cur, """
        CREATE TABLE IF NOT EXISTS pm_brief_snapshots (
            snapshot_date DATE PRIMARY KEY,
            captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            boards        JSONB NOT NULL,
            brief_md      TEXT
        )""")
    _guarded_execute(cur, """
        CREATE TABLE IF NOT EXISTS pm_brief_dismissals (
            item         TEXT PRIMARY KEY,
            reason       TEXT NOT NULL,
            dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")


# ── board readers ───────────────────────────────────────────────────────────
def _shell_reader(path: str, shell_name: str):
    """Generic master-shell reader: lanes -> {id: verdict}. Verdict tokens are
    the shells' own (FAIL / ? / PASS) — never re-invented literals."""
    def read():
        payload, reason = _read_json(_base() + path, admin=True)
        if payload is None:
            return {"status": "UNMEASURED", "reason": reason,
                    "lanes": {}, "headlines": {}}
        lanes_raw = payload.get("lanes")
        if not isinstance(lanes_raw, list):
            return {"status": "UNMEASURED",
                    "reason": "no lanes[] in response",
                    "lanes": {}, "headlines": {}}
        lanes = {}
        lane_meta = {}
        for ln in lanes_raw:
            lid = str(ln.get("id") or "?")
            v = ln.get("verdict")
            lanes[lid] = v if v in (FAIL, PASS, UNKNOWN) else UNKNOWN
            # Lens C1 severity: a failing CRITICAL check outranks non-critical.
            checks = ln.get("checks")
            failing = ([k for k in checks
                        if isinstance(k, dict) and k.get("pass") is False]
                       if isinstance(checks, list) else [])
            lane_meta[lid] = {
                "failing_checks": len(failing),
                "critical_fail": any(k.get("critical") for k in failing)}
        # Lens C1: the board's OWN red_by_design markings — structural work
        # orders that investigation cannot turn green. Read, never re-derived.
        rbd = payload.get("red_by_design")
        n_fail = sum(1 for v in lanes.values() if v == FAIL)
        return {"status": "MEASURED", "reason": None, "lanes": lanes,
                "lane_meta": lane_meta,
                "red_by_design": ([str(x) for x in rbd]
                                  if isinstance(rbd, list) else []),
                "headlines": {"lanes_red": {
                    "value": n_fail, "window": "now",
                    "basis": f"{shell_name} lane verdicts, this tick"}}}
    return read


def _read_pay_events():
    payload, reason = _read_json(
        _base() + "/api/v1/admin/agent-pay-events?days=30", admin=True)
    if payload is None:
        return {"status": "UNMEASURED", "reason": reason,
                "lanes": {}, "headlines": {}}
    totals = payload.get("totals") or {}
    paid = totals.get("paid")
    if paid is None:
        return {"status": "UNMEASURED", "reason": "no totals.paid in response",
                "lanes": {}, "headlines": {}}
    # Born-red BY DESIGN until the first real settlement lands: the rail is
    # live-test-only with 0 real settlements ever (known standing item).
    lanes = {"first_real_settlement": PASS if int(paid) > 0 else FAIL}
    return {"status": "MEASURED", "reason": None, "lanes": lanes,
            "headlines": {
                "agent_pay_settled": {
                    "value": int(paid), "window": "30d",
                    "basis": "mcp_call_log mpp_paid/x402_paid (settled $)"},
                "agent_pay_quotes_issued": {
                    "value": int(totals.get("challenges") or 0),
                    "window": "30d",
                    "basis": "mpp_challenge = quote ISSUED, not pay intent"}}}


_STALL_STATUSES = {"stuck", "stalled", "overdue"}


def _read_whats_new():
    payload, reason = _read_json(_base() + "/api/v1/whats-new")
    if payload is None:
        return {"status": "UNMEASURED", "reason": reason,
                "lanes": {}, "headlines": {}}
    items = payload.get("items")
    if not isinstance(items, list):
        return {"status": "UNMEASURED", "reason": "no items[] in response",
                "lanes": {}, "headlines": {}}
    stalled = [i.get("category") for i in items
               if (i.get("status") or "").lower() in _STALL_STATUSES]
    growing = sum(1 for i in items
                  if (i.get("status") or "").lower() == "growing")
    lanes = {"layer_stalls": FAIL if stalled else PASS}
    # platform announcements: null = UNMEASURED semantics, not "none shipped"
    if payload.get("platform") is None and "platform" in payload:
        lanes["platform_announcements"] = UNKNOWN
    return {"status": "MEASURED", "reason": None, "lanes": lanes,
            "headlines": {
                "layers_growing": {
                    "value": growing, "window": "7d",
                    "basis": f"/whats-new status per layer, {len(items)} layers"},
                "rows_added": {
                    "value": payload.get("total_added"), "window": "7d",
                    "basis": "sum of per-layer added counts (7d subsets)"}},
            "detail": {"stalled_layers": stalled[:5]}}


def _read_fe_contract_guard():
    """Latest run conclusion of the frontend contract guard CI — read from the
    GitHub API (public repo), NOT by re-running the scan daily."""
    payload, reason = _read_json(
        "https://api.github.com/repos/azmartone67/dchub-frontend/actions/runs"
        "?per_page=40&status=completed")
    if payload is None:
        return {"status": "UNMEASURED", "reason": reason,
                "lanes": {}, "headlines": {}}
    runs = payload.get("workflow_runs") or []
    hit = next((r for r in runs
                if "API contract guard" in (r.get("name") or "")), None)
    if hit is None:
        return {"status": "UNMEASURED",
                "reason": "no completed 'QA — API contract guard' run in the "
                          "last 40 workflow runs",
                "lanes": {}, "headlines": {}}
    concl = (hit.get("conclusion") or "").lower()
    lanes = {"contract_guard_ci": PASS if concl == "success"
             else (FAIL if concl in ("failure", "timed_out") else UNKNOWN)}
    return {"status": "MEASURED", "reason": None, "lanes": lanes,
            "headlines": {"contract_guard_last_run": {
                "value": f"{concl} @ {hit.get('created_at')}",
                "window": "latest completed run",
                "basis": "gh actions API, dchub-frontend, read-only"}}}


_BOARDS = [
    ("freshness", _shell_reader("/api/v1/admin/freshness", "freshness")),
    ("adoption", _shell_reader("/api/v1/admin/adoption", "adoption")),
    # NOTE: the GET board path without /master-tick 404s — lanes live on
    # master-tick only (known unfixed gap, do not "fix" by guessing).
    ("registry-distribution",
     _shell_reader("/api/v1/admin/registry-distribution/master-tick",
                   "registry-distribution")),
    ("agent-pay", _read_pay_events),
    ("whats-new", _read_whats_new),
    ("fe-contract-guard", _read_fe_contract_guard),
]


# ── known standing items (seed state, so day one is not all "new") ──────────
# key: "board/lane" where a lane exists; free-floating items get "standing/*".
# Lens C2: each item carries known_since (YYYY-MM-DD, the date the finding was
# first recorded — audit/report date, NOT the snapshot series start). Ages
# render from known_since, so a standing item never re-stamps "(day 1)" just
# because the snapshot series is young.
_KNOWN_STANDING = {
    "agent-pay/first_real_settlement": (
        "machine-pay rail is live-test-only: 0 real settlements ever",
        "agent-doable",
        "GET /api/v1/admin/agent-pay-events?days=30 — watch first_paid_at",
        "2026-08-07"),
    "registry-distribution/listing_accuracy": (
        "Glama listed-but-wrong (their bug, ticket filed; escalate day 3+)",
        "owner",
        "chase the Glama ticket; staleness clock is running",
        "2026-08-08"),
    "registry-distribution/drafted_but_unwired": (
        "7 drafted registries not wired into the shell",
        "agent-doable",
        "wire drafts into registry_distribution_master_shell _LANES",
        "2026-08-08"),
    "standing/hive-submission": (
        "Hive: submission sent, listing never landed — chase the submission",
        "owner", "re-check Hive listing, resubmit if dropped",
        "2026-08-08"),
    "standing/docker-catalog": (
        "Docker MCP catalog: absent; ~18-line server.yaml, owner action",
        "owner", "submit server.yaml to docker/mcp-registry",
        "2026-08-08"),
    "standing/bulk-backfill": (
        "fiber/substation identity fixes shipped; safe BULK BACKFILL not run",
        "owner", "run the backfill job against prod (owner gate)",
        "2026-08-07"),
    "standing/durable-key-retention": (
        "durable-key retention 2.4%; OAuth one client wide "
        "(challenge scope, server.mjs ~13650)", "agent-doable",
        "widen OAuth challenge scope in dchub-mcp-server server.mjs",
        "2026-08-07"),
    "standing/mcp-version-string": (
        "/mcp version string stale (2.5.0 vs real 2.12.0) in worker.js — "
        "dashboard paste only", "agent-doable",
        "bump the version literal in frontend worker.js",
        "2026-08-07"),
}


def _known_age(key: str, today_str: str) -> str:
    """Lens C2: bare 'known since D, day N' for a seeded standing item — the
    age is real elapsed time since the finding was RECORDED, never the
    snapshot series' age (a series younger than the finding must not
    re-stamp day 1). Empty string for non-seeded keys; callers wrap/compose
    ('(known since …)' alone, '(red day R; known since …)' when the item is
    also a measured-red lane)."""
    entry = _KNOWN_STANDING.get(key)
    if not entry or len(entry) < 4:
        return ""
    since = entry[3]
    try:
        days = (datetime.date.fromisoformat(today_str)
                - datetime.date.fromisoformat(since)).days + 1
    except ValueError:
        return f"known since {since}"
    return f"known since {since}, day {days}"


# ── snapshot persistence ────────────────────────────────────────────────────
def _load_dismissals(cur) -> dict:
    _guarded_execute(
        cur, "SELECT item, reason, dismissed_at::date::text "
             "FROM pm_brief_dismissals ORDER BY dismissed_at DESC")
    return {r[0]: {"reason": r[1], "date": r[2]}
            for r in (cur.fetchall() or [])}


def _lane_map(boards: dict) -> dict:
    """{'board/lane': verdict} for one snapshot's boards blob."""
    out = {}
    for bid, b in (boards or {}).items():
        for lid, v in (b.get("lanes") or {}).items():
            out[f"{bid}/{lid}"] = v
    return out


def _red_streak(key: str, today_lanes: dict, history: list[dict],
                today_str: str) -> tuple[int, int]:
    """(measured_red_days, gap_days) for `key`, walking CALENDAR days back
    from today.

    ★ DESIGN DECISION (2026-08-14, Lens A3 + B4 — made deliberately, here):
    a MISSING snapshot day and an UNMEASURED lane day both HOLD the streak —
    they neither extend it (a day nobody measured is not a day measured red;
    the old code counted 08-12 as red day 3 when 08-12 was never collected)
    nor reset it (the old code reset on unmeasured, which let a flaky reader
    permanently prevent escalation — kill the collector every third day and
    nothing ever escalates). Only a MEASURED PASS resets; only a MEASURED
    FAIL extends. The brief must then SAY the series has gaps
    ("RED 4 measured days (1 gap)") — a gap-free claim over a gapped series
    is the lie this function used to tell.

    Gap days are counted only INTERIOR to the streak (between measured reds);
    trailing unmeasured days older than the oldest measured red are not part
    of the streak's span."""
    if today_lanes.get(key) != FAIL:
        return 0, 0
    by_date = {row["date"]: _lane_map(row["boards"]) for row in history}
    n, gaps = 1, 0
    if not by_date:
        return n, gaps
    try:
        day = datetime.date.fromisoformat(today_str)
    except ValueError:
        return n, gaps
    oldest = min(by_date)
    pending = 0  # unmeasured/missing days seen since the last measured red
    day -= datetime.timedelta(days=1)
    while day.isoformat() >= oldest:
        v = by_date.get(day.isoformat(), {}).get(key)
        if v == FAIL:
            n += 1
            gaps += pending  # the held days are interior — commit them
            pending = 0
        elif v == PASS:
            break  # a measured green is the ONLY thing that resets the clock
        else:
            pending += 1  # missing snapshot or unmeasured lane: HOLD
        day -= datetime.timedelta(days=1)
    return n, gaps


def _gap_note(gaps: int) -> str:
    return f" ({gaps} gap{'s' if gaps > 1 else ''})" if gaps else ""


# ── the brief ───────────────────────────────────────────────────────────────
def _action_for(key: str) -> tuple[str, str, str]:
    if key in _KNOWN_STANDING:
        return _KNOWN_STANDING[key][:3]
    bid = key.split("/", 1)[0]
    return (f"investigate {key}", "agent-doable",
            f"GET {_base()}/api/v1/admin/{bid} (X-Admin-Key) and read the "
            f"failing checks' detail strings")


def _red_by_design_keys(boards: dict) -> set:
    """Lens C1: 'board/lane' keys the board ITSELF marks red_by_design —
    structural work orders investigation cannot turn green. Read from the
    boards' own markings, never re-derived here."""
    out = set()
    for bid, b in (boards or {}).items():
        for lid in (b.get("red_by_design") or []):
            out.add(f"{bid}/{lid}")
    return out


def _critical_fail(boards: dict, key: str) -> bool:
    bid, _, lid = key.partition("/")
    meta = (boards.get(bid, {}).get("lane_meta") or {}).get(lid) or {}
    return bool(meta.get("critical_fail"))


def _capped(L: list, items: list, cap: int, render) -> None:
    """Lens B1: every capped section that truncates ENDS by saying so —
    items past a cap must be countable, never invisible."""
    for it in items[:cap]:
        L.append(render(it))
    if len(items) > cap:
        L.append(f"- +{len(items) - cap} more (see ?format=json)")


def _build_brief(today: dict, history: list[dict], dismissals: dict,
                 today_str: str) -> str:
    """history = prior snapshots, newest first, NOT including today."""
    boards = today
    lanes_today = _lane_map(boards)
    first_run = not history
    prev_lanes = _lane_map(history[0]["boards"]) if history else {}

    dismissed_keys = set(dismissals)
    live = {k: v for k, v in lanes_today.items() if k not in dismissed_keys}

    reds = {k for k, v in live.items() if v == FAIL}
    streaks = {k: _red_streak(k, lanes_today, history, today_str)
               for k in reds}
    rbd = _red_by_design_keys(boards)
    escal = sorted((k for k in reds if streaks[k][0] >= 3),
                   key=lambda k: (-streaks[k][0], k))
    new_reds = sorted(k for k in reds
                      if not first_run and prev_lanes.get(k) not in (FAIL,))
    went_green = sorted(k for k, v in live.items()
                        if v == PASS and prev_lanes.get(k) == FAIL)
    unmeasured = [(bid, b.get("reason") or "unreadable")
                  for bid, b in boards.items()
                  if b.get("status") == "UNMEASURED"]

    def _age(k):
        # Lens C2 composition: a seeded item that is ALSO a measured-red lane
        # carries BOTH clocks — the measured streak (gap-honest) and the real
        # known-since age — so a young snapshot series can never re-stamp a
        # week-old finding as "(day 1)".
        known = _known_age(k, today_str)
        if k in streaks:
            n, g = streaks[k]
            return (f" (red day {n}{_gap_note(g)}"
                    + (f"; {known}" if known else "") + ")")
        return f" ({known})" if known else ""

    L = []
    L.append(f"# PM Brief — {today_str}"
             + (" — FIRST RUN (no baseline: nothing here is 'new', "
                "born-red lanes are standing work orders)" if first_run
                else f" (diff vs {history[0]['date']})"))

    L.append("")
    # Lens A3: "measured days" is literal — gaps HOLD the clock and are
    # declared per line; they never extend a streak and never reset it.
    L.append("## ESCALATIONS (red >= 3 measured days; unmeasured/missing "
             "days hold the clock, never reset it)")
    if escal:
        def _esc_line(k):
            n, g = streaks[k]
            note = _KNOWN_STANDING.get(k, ("",))[0]
            label = (f"RED {n} measured days{_gap_note(g)}" if g
                     else f"RED day {n}")
            known = _known_age(k, today_str)
            return (f"- {k} — {label}"
                    + (f" — {note}" if note else "")
                    + (f" ({known})" if known else ""))
        _capped(L, escal, 6, _esc_line)
    else:
        L.append("- none" + (" (first run: clock starts today)"
                             if first_run else ""))

    L.append("")
    L.append("## NEW REDS since previous snapshot")
    if first_run:
        L.append("- FIRST RUN — no diff baseline; today's reds: "
                 + (", ".join(sorted(f"{k}{_age(k)}" for k in reds))
                    or "none"))
    elif new_reds:
        _capped(L, new_reds, 8,
                lambda k: f"- {k}"
                + (" (known standing item)" if k in _KNOWN_STANDING else ""))
    else:
        L.append("- none")

    L.append("")
    # ★ Lens C1 — TOP THREE is VALUE-ranked, not alphabetical. The rule,
    # stated once and implemented right here:
    #   (1) escalated diagnosable reds first, by streak age (oldest first);
    #   (2) then the other diagnosable reds by streak age, then by board
    #       severity (a critical failing check outranks non-critical);
    #   (3) red_by_design lanes are NOT actions — the board itself says
    #       investigation cannot turn them green; they go to the WORK ORDERS
    #       note below (and stay in STANDING REDS);
    #   (4) never two slots with the identical start command — candidates
    #       sharing a command collapse into ONE slot naming every lane;
    #   (5) remaining slots go to seeded standing/* items (owner work),
    #       aged from known_since.
    L.append("## TOP THREE ACTIONS (value-ranked: escalated > diagnosable "
             "by age/severity; red_by_design excluded)")
    diagnosable = sorted(
        (k for k in reds if k not in rbd),
        key=lambda k: (streaks[k][0] < 3,        # escalated first
                       -streaks[k][0],           # then by streak age
                       not _critical_fail(boards, k),  # then severity
                       k))
    pool = diagnosable + [k for k in _KNOWN_STANDING
                          if k.startswith("standing/")
                          and k not in dismissed_keys]
    slots, seen, overflow = [], set(), 0
    for k in pool:
        if k in seen:
            continue
        seen.add(k)
        what, who, start = _action_for(k)
        same = next((s for s in slots if s["start"] == start), None)
        if same is not None:
            same["also"].append(k)      # identical start command: one slot
            continue
        if len(slots) == 3:
            overflow += 1
            continue
        slots.append({"k": k, "what": what, "who": who, "start": start,
                      "also": []})
    for rank, s in enumerate(slots, 1):
        also = (f" (also covers: {', '.join(s['also'])})" if s["also"]
                else "")
        L.append(f"{rank}. [{s['who']}] {s['what']}{_age(s['k'])}{also} — "
                 f"start: {s['start']}")
    if overflow:
        L.append(f"- +{overflow} more candidate actions (see STANDING REDS "
                 f"and ?format=json)")
    if not slots:
        L.append("1. nothing red and nothing standing — verify the boards "
                 "themselves ran (UNMEASURED list below)")

    rbd_reds = sorted(k for k in reds if k in rbd)
    if rbd_reds:
        L.append("")
        L.append("## WORK ORDERS (red_by_design — the board itself says "
                 "investigation cannot turn these green)")
        for k in rbd_reds:
            n, g = streaks[k]
            L.append(f"- {k} — red day {n}{_gap_note(g)} — structural; "
                     f"see the board's work_order")

    L.append("")
    # Lens B2: the complete red inventory — EVERY currently-red lane, one
    # line each, whatever the caps above did. Nothing red is ever invisible.
    L.append("## STANDING REDS (complete inventory: every red lane, today)")
    if reds:
        for k in sorted(reds):
            n, g = streaks[k]
            tags = "".join(
                [" [red_by_design]" if k in rbd else "",
                 " [escalated]" if streaks[k][0] >= 3 else "",
                 " [new]" if k in new_reds else ""])
            known = _known_age(k, today_str)
            L.append(f"- {k} — red day {n}{_gap_note(g)}"
                     + (f"; {known}" if known else "") + tags)
    else:
        L.append("- none — no red lanes today")

    L.append("")
    L.append("## WENT GREEN")
    if went_green:
        _capped(L, went_green, 6,
                lambda k: f"- {k} — was red, board now measures PASS")
    else:
        L.append("- none")

    L.append("")
    L.append("## UNMEASURED boards")
    if unmeasured:
        for bid, why in unmeasured:
            L.append(f"- {bid} — {why} (not red, not green: unread)")
    else:
        L.append("- none — every board was read this run")

    L.append("")
    L.append("## HEADLINE NUMBERS (value / window / basis)")
    for bid, b in boards.items():
        for hname, h in (b.get("headlines") or {}).items():
            L.append(f"- {bid}.{hname}: {h.get('value')} "
                     f"[{h.get('window')}] — {h.get('basis')}")

    if dismissals:
        L.append("")
        # Lens B3: "collapsed but never vanish" must stay true past 5 — the
        # section counts what it truncates, and ?format=json always carries
        # ALL dismissals (see pm_brief_latest).
        L.append("## DISMISSED (collapsed — owner-recorded, never deleted)")
        _capped(L, list(dismissals.items()), 5,
                lambda it: f"- {it[0]} — {it[1]['reason']} ({it[1]['date']})")

    if len(L) > _MAX_LINES:
        L = L[:_MAX_LINES - 1] + [
            f"... truncated at {_MAX_LINES} lines (cap; full data in "
            f"?format=json)"]
    return "\n".join(L)


# ── staleness (Lens A2) ─────────────────────────────────────────────────────
def _staleness_banner(snapshot_date_str, today=None):
    """(age_days, banner). A week-old snapshot must never read as 'latest':
    when the newest snapshot is > 1 day old the markdown opens with a STALE
    banner and ?format=json carries stale/snapshot_age_days. Age is calendar
    days between the snapshot date and today."""
    try:
        d = datetime.date.fromisoformat((snapshot_date_str or "").strip())
    except ValueError:
        return None, ("**STALE — snapshot date unreadable; the collector "
                      "may be dead.**\n\n")
    age = ((today or datetime.date.today()) - d).days
    if age > 1:
        return age, (
            f"**STALE — newest snapshot is {age} days old; the collector "
            f"may be dead. Check pm-brief-daily.yml (GH 60-day auto-disable,"
            f" secrets) and the deadman board before trusting anything "
            f"below.**\n\n")
    return age, ""


# ── liveness beat (Lens A1) ─────────────────────────────────────────────────
def _beat_ledger(note: str) -> None:
    """Post the pm-brief-collection liveness beat to the deadman ledger.

    ★ THE one deliberate non-GET in this module, and it is NOT a board write:
    it feeds the module's OWN row on /api/v1/ops/deadman so a dead collector
    alarms instead of serving week-old briefs as 'latest' forever (Lens A1).
    The static guard test pins this: POST capability exists ONLY inside this
    function and ONLY toward the ingest-runs beat URL. Fail-open — a beat
    error must never fail a real collection. Feed name is pm-brief-collection
    (the PRODUCER's beat); deadman-watch separately tracks the workflow
    pm-brief-daily.yml by GH Actions last-success, so the cron dying and the
    collection silently no-oping alarm independently, and the producer beat
    is never clobbered by a conclusion writer (one-direction masking,
    SH52-002 B2)."""
    try:
        body = json.dumps({
            "feed": "pm-brief-collection",
            "status": "success",
            "rows_inserted": 1,  # liveness sentinel — health lives in note
            "cadence_hours": 30,  # daily cron; generous like other dailies
            "last_run": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        req = _urlreq.Request(
            "http://127.0.0.1:" + str(port)
            + "/api/v1/admin/ingest-runs/beat",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": _UA, "X-Admin-Key": _admin_key()})
        _urlreq.urlopen(req, timeout=5).read()
    except Exception:  # noqa: BLE001 — beat failure must never fail the run
        pass


# ── collection ──────────────────────────────────────────────────────────────
def collect_boards() -> dict:
    """Read every board. Three-valued BY CONSTRUCTION: a reader that crashes
    renders UNMEASURED with the reason — never skipped silently, never a
    verdict."""
    boards = {}
    for bid, reader in _BOARDS:
        try:
            boards[bid] = reader()
        except Exception as e:  # noqa: BLE001 — a reader crash is UNMEASURED
            boards[bid] = {"status": "UNMEASURED",
                           "reason": f"reader crashed: {type(e).__name__}",
                           "lanes": {}, "headlines": {}}
    return boards


def run_collection() -> dict:
    """Read every board (three-valued), persist today's snapshot (idempotent
    per day, advisory-lock leader-gated), build + store the brief."""
    boards = collect_boards()
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db",
                "boards_read": {b: boards[b]["status"] for b in boards}}
    today_str = datetime.date.today().isoformat()
    try:
        # One explicit transaction: the xact lock is taken and released
        # atomically with the upsert, and pgbouncer keeps the whole
        # transaction on one backend. autocommit off ONLY for this block.
        c.autocommit = False
        with c.cursor() as cur:
            _ensure_tables(cur)
            _guarded_execute(
                cur, "SELECT pg_try_advisory_xact_lock(%d)" % _LOCK_ID)
            got = bool((cur.fetchone() or [False])[0])
            if not got:
                c.rollback()
                # NOT a success: for a once-daily collection a concurrent
                # writer is itself an anomaly, and reporting ok:true here is
                # how the leaked-lock bug stayed green. The cron fails on it.
                return {"ok": False, "skipped": "another_writer_holds_lock",
                        "error": "collection skipped: lock busy"}
            _guarded_execute(cur, """
                SELECT snapshot_date::text, boards, brief_md
                  FROM pm_brief_snapshots
                 WHERE snapshot_date < CURRENT_DATE
                 ORDER BY snapshot_date DESC LIMIT 14""")
            history = []
            for r in (cur.fetchall() or []):
                b = r[1]
                if isinstance(b, str):
                    b = json.loads(b)
                history.append({"date": r[0], "boards": b or {},
                                "brief_md": r[2]})
            dismissals = _load_dismissals(cur)
            brief = _build_brief(boards, history, dismissals, today_str)
            # ON CONFLICT upsert => idempotent per day (re-runs refresh).
            _guarded_execute(cur, """
                INSERT INTO pm_brief_snapshots
                    (snapshot_date, boards, brief_md)
                VALUES (CURRENT_DATE, %s, %s)
                ON CONFLICT (snapshot_date) DO UPDATE
                   SET boards = EXCLUDED.boards,
                       brief_md = EXCLUDED.brief_md,
                       captured_at = NOW()""",
                (json.dumps(boards), brief))
        c.commit()  # releases the xact lock with the write, same backend
    finally:
        try:
            c.close()
        except Exception:
            pass
    # Lens A1: beat ONLY after the commit landed — a beat before the write
    # would report liveness for a collection that never persisted.
    unm = [b for b in boards if boards[b]["status"] == "UNMEASURED"]
    _beat_ledger(f"snapshot {today_str}; boards={len(boards)}; "
                 f"unmeasured={','.join(unm) or 'none'}")
    return {"ok": True, "snapshot_date": today_str,
            "boards_read": {b: boards[b]["status"] for b in boards},
            "unmeasured": [b for b in boards
                           if boards[b]["status"] == "UNMEASURED"],
            "brief_md": brief}


# ── routes ──────────────────────────────────────────────────────────────────
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@pm_brief_bp.route("/api/v1/admin/pm-brief", methods=["GET"])
def pm_brief_latest():
    if _disabled():
        return jsonify(ok=False, disabled=True,
                       reason="PM_BRIEF_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    c = _conn()
    if c is None:
        # 424, never 5xx: any 5xx from Railway makes the CF worker fail the
        # site over to the stale Render origin. A broken diagnostic must not.
        return _no_store(jsonify(ok=False, error="no_db")), 424
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            if request.args.get("list"):
                _guarded_execute(cur, """
                    SELECT snapshot_date::text FROM pm_brief_snapshots
                     ORDER BY snapshot_date DESC LIMIT 60""")
                return _no_store(jsonify(
                    ok=True, dates=[r[0] for r in (cur.fetchall() or [])]))
            want = (request.args.get("date") or "").strip()
            if want and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", want):
                return jsonify(ok=False, error="bad date"), 400
            if want:
                _guarded_execute(cur, """
                    SELECT snapshot_date::text, boards, brief_md
                      FROM pm_brief_snapshots
                     WHERE snapshot_date = %s""", (want,))
            else:
                _guarded_execute(cur, """
                    SELECT snapshot_date::text, boards, brief_md
                      FROM pm_brief_snapshots
                     ORDER BY snapshot_date DESC LIMIT 1""")
            row = cur.fetchone()
            if not row:
                return _no_store(jsonify(
                    ok=True, brief=None,
                    note="no snapshots yet — POST /api/v1/admin/pm-brief/run"))
            # Lens A2: "latest" must not mean "newest row, however old".
            # When serving the newest snapshot, an age check runs and a
            # >1-day-old brief opens with a STALE banner (same fields in
            # ?format=json). An explicit ?date= is a historical read — the
            # caller asked for that day, so no banner.
            age, banner = (None, "") if want else _staleness_banner(row[0])
            if request.args.get("format") == "json":
                b = row[1]
                if isinstance(b, str):
                    b = json.loads(b)
                # Lens B3: ALL dismissals ride in the JSON, always — the
                # markdown may cap at 5 with a "+N more" line, but nothing
                # is only reachable by having been under the cap.
                return _no_store(jsonify(
                    ok=True, snapshot_date=row[0], boards=b, brief_md=row[2],
                    stale=bool(banner), snapshot_age_days=age,
                    dismissals=_load_dismissals(cur)))
            return _no_store(Response(banner + (row[2] or "(brief missing)"),
                                      mimetype="text/markdown"))
    finally:
        try:
            c.close()
        except Exception:
            pass


@pm_brief_bp.route("/api/v1/admin/pm-brief/run", methods=["POST"])
def pm_brief_run():
    if _disabled():
        return jsonify(ok=False, disabled=True), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    try:
        out = run_collection()
    except Exception as e:  # noqa: BLE001 — a failed run must FAIL the cron
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    # 424 on failure, never 5xx (CF failover trap) — curl --fail-with-body
    # still exits non-zero on 424, so a green cron means a real collection.
    return _no_store(jsonify(out)), (200 if out.get("ok") else 424)


@pm_brief_bp.route("/api/v1/admin/pm-brief/dismiss", methods=["POST"])
def pm_brief_dismiss():
    """OWNER-only door out of the brief. The PM itself never calls this —
    there is no code path from run_collection() to here. Dismissed items
    still render, collapsed, with reason and date."""
    if _disabled():
        return jsonify(ok=False, disabled=True), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    body = request.get_json(silent=True) or {}
    item = (body.get("item") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not item or not reason:
        return jsonify(ok=False,
                       error="both item and reason are required"), 400
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 424
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            _guarded_execute(cur, """
                INSERT INTO pm_brief_dismissals (item, reason)
                VALUES (%s, %s)
                ON CONFLICT (item) DO UPDATE
                   SET reason = EXCLUDED.reason, dismissed_at = NOW()""",
                (item, reason))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return _no_store(jsonify(ok=True, item=item, dismissed=True,
                             note="collapsed in the brief, never deleted"))
