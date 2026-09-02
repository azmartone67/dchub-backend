"""routes/audit_closure_master_shell.py — Audit Closure Master Shell (#52, 2026-08-07).

WHY THIS EXISTS
===============
On 2026-08-07 a 14-agent full-platform audit produced 138 verified findings
(5 critical, 36 high) across every domain: a zombie twin stack 401-spamming
the job layer while poisoning its health stamps, a monetization wall whose
gateway consumer was never written, public credentials in a public repo, and
first-call surfaces that misinform AI agents (caller_tier='pro' to keyless
callers, an undeduped 24,675 facility count served to citation engines).

The audit's deepest lesson was not any single defect — it was that the
platform SEES almost everything and ACTS on almost nothing: red lanes do not
convert into fixes, the same finding gets re-filed instead of landed, and
four shells shipped with no scheduler (the registered≠scheduled class, 4th
firing). This shell is the closure organ for that audit:

  1. Every one of the 138 findings lives in the embedded REGISTRY. Findings
     with a live checker get a machine verdict every tick; the rest stay
     honestly OPEN until their fix ships a checker or the operator closes
     them deliberately via AUDIT_CLOSURE_ACK (comma-separated ids) — the
     MPP_FLAGSHIP_PREMIUM_ACK pattern, never a silent default.
  2. `scan_beat_scheduler_gaps()` is the class fix for registered≠scheduled:
     it walks every routes/ module that declares `_beat_ledger` and demands a
     scheduler (a cron_heartbeat _DISPATCH entry or a cron'd workflow) for its
     tick route. Lane J runs it live; tests/test_shell_scheduler_coverage.py
     runs the SAME helper in CI, so the class cannot ship again — including
     by this shell itself.

HOUSE RULES (inherited from #34, whose helpers this module IMPORTS — a copy
would drift):
  · A lane never reads PASS when it could not check — unreachable is '?'.
  · Read-only. Bounded live probes with a per-tick budget.
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s.
  · Born-red is correct: several checks (quota flag, detector scout, drip
    CTA) FAIL by design until the owner acts. Red = work, not noise.

Surface:  GET /admin/audit-closure                              (HTML)
          GET /api/v1/admin/audit-closure                       (HTML)
          GET|POST /api/v1/admin/audit-closure/master-tick      (JSON)
Beat:     audit-closure-shell-daily
Kill:     AUDIT_CLOSURE_SHELL_DISABLE=1
Probes:   AUDIT_CLOSURE_SHELL_PROBE=0 disables live MCP probes
Acks:     AUDIT_CLOSURE_ACK="SH52-090,SH52-112" closes manual items on purpose
Reports:  the full evidence lives in the 2026-08-07 audit report (operator's
          ~/Downloads/dchub-comprehensive-audit-2026-08-07.md + appendix).
"""

from __future__ import annotations

import datetime
import glob
import json
import logging
import ast
import os
import re
import time
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

audit_closure_master_shell_bp = Blueprint("audit_closure_master_shell", __name__)

ORIGIN = (os.environ.get("SURFACE_TRUTH_ORIGIN") or "https://dchub.cloud").rstrip("/")
_UA = "dchub-audit-closure-shell/1.0 (+https://dchub.cloud; internal-audit)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Imported, never re-declared (#34's own rule): the strict three-valued lane
# verdict, the check constructor, the DB connector indirection, and the
# hardened SSE-safe MCP probe. If agent_pay_master_shell is unimportable this
# module must still import (fail-soft to local minimal fallbacks) — a broken
# sibling must not take the closure board down with it.
try:
    from routes.agent_pay_master_shell import (_check, _lane_verdict,
                                               _mcp_probe_uncached)
except Exception as _imp_e:  # noqa: BLE001
    logger.warning("[audit-closure] #34 helper import failed: %s", _imp_e)

    def _check(cid, name, passed, detail, critical=False):  # type: ignore
        return {"id": cid, "name": name, "pass": passed,
                "detail": detail, "critical": critical}

    def _lane_verdict(checks):  # type: ignore
        if any(k["pass"] is False for k in checks):
            return "FAIL"
        if any(k["pass"] is None for k in checks if k.get("critical")):
            return "?"
        if (any(k["pass"] is None for k in checks)
                and not any(k["pass"] is True for k in checks)):
            return "?"
        return "PASS"

    def _mcp_probe_uncached(tool, args):  # type: ignore
        return None, "agent_pay_master_shell unimportable — probe unavailable"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("AUDIT_CLOSURE_SHELL_DISABLE") or "").strip() == "1"


def _probe_enabled() -> bool:
    return (os.environ.get("AUDIT_CLOSURE_SHELL_PROBE") or "1").strip() != "0"


# ── bounded HTTP (memoized per tick) ──────────────────────────────────

# ★45s, not more: cron_heartbeat's _hit abandons at 30s but the handler runs
# to completion on the web replica — the budget bounds how long that is. The
# label also sits in _HEAVY_LABELS (3-wide throttle) and _MIN_REFIRE_S so the
# <55-minute fire window cannot stack ticks.
_TICK_BUDGET_S = 45.0
_tick_t0 = [0.0]


def _budget_left() -> float:
    return _TICK_BUDGET_S - (time.monotonic() - _tick_t0[0])


def _http(url, timeout=8, headers=None, fresh=False, _memo={}):
    """GET url → (status:int|None, headers:dict, text:str, err:str|None).

    Memoized per tick; several checks read the same surface. fresh=True
    bypasses the memo WITHOUT changing the URL — the cache-observation check
    needs two reads of the SAME cache key, and a busted second URL can never
    observe a HIT (that vacuity shipped in this module's first draft and was
    caught in review). Budget-guarded: when the tick budget is spent,
    remaining checks read UNKNOWN rather than holding a web-replica
    connection open (the #34 lesson).
    """
    key = url
    if not fresh and key in _memo:
        return _memo[key]
    if _tick_t0[0] and _budget_left() <= 2:
        res = (None, {}, "", "tick budget exhausted — skipped")
        _memo[key] = res
        return res
    try:
        import requests as _rq   # not urllib (regression_lint)
        h = {"User-Agent": _UA}
        h.update(headers or {})
        r = _rq.get(url, timeout=min(timeout, max(2, _budget_left())),
                    headers=h, allow_redirects=False)
        res = (r.status_code, dict(r.headers),
               r.content.decode("utf-8", "replace"), None)
    except Exception as e:  # noqa: BLE001
        res = (None, {}, "", "%s: %s" % (type(e).__name__, str(e)[:100]))
    _memo[key] = res
    return res


def _http_memo_clear():
    try:
        _http.__defaults__[2].clear()
    except Exception:  # noqa: BLE001
        pass
    _tick_t0[0] = time.monotonic()


def _local(path) -> str:
    return "http://127.0.0.1:%s%s" % (os.environ.get("PORT", "8080"), path)


def _edge(path) -> str:
    # ★Always cache-bust edge reads: CF Rule #3 caches /api/v1/* and a HIT-aged
    # body would grade yesterday's deploy (the frozen-heal trap, CI 0806).
    sep = "&" if "?" in path else "?"
    return ORIGIN + path + sep + "_acb=%d" % int(time.time())


def _jget(url, timeout=8, headers=None):
    st, _h, body, err = _http(url, timeout=timeout, headers=headers)
    if err or st is None:
        return None, err or "no response"
    if st != 200:
        return None, "HTTP %d" % st
    try:
        return json.loads(body), None
    except Exception:  # noqa: BLE001
        return None, "unparseable JSON (HTTP %d)" % st


def _mcp(tool, args):
    if not _probe_enabled():
        return None, "probe disabled (AUDIT_CLOSURE_SHELL_PROBE=0)"
    # One probe costs up to ~28s of hops — require real headroom, not 10s.
    if _tick_t0[0] and _budget_left() <= 20:
        return None, "tick budget too low for an MCP probe — skipped"
    return _mcp_probe_uncached(tool, args)


def _mcp_server_version():
    """serverInfo.version from a live MCP initialize — the ONLY honest source.
    /mcp/health and mcp.json echo PINNED back (the closed-loop trap that let
    2.5.0 sit 6 minors stale), and review proved the tool envelope carries no
    _meta.server_version. Returns (version:str|None, err:str|None)."""
    if not _probe_enabled():
        return None, "probe disabled (AUDIT_CLOSURE_SHELL_PROBE=0)"
    if _tick_t0[0] and _budget_left() <= 10:
        return None, "tick budget too low — skipped"
    try:
        import requests as _rq   # not urllib (regression_lint)
        r = _rq.post(ORIGIN + "/mcp", timeout=8, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": _UA}, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "dchub-audit-closure-probe",
                                      "version": "1.0"}}})
        # SSE traps (#34's parser rules): decode utf-8 explicitly, split on
        # "\n" only — splitlines() breaks on U+0085/U+2028 inside JSON.
        raw = r.content.decode("utf-8", "replace")
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    body = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                v = ((body.get("result") or {}).get("serverInfo")
                     or {}).get("version")
                if v:
                    return str(v), None
        return None, "no serverInfo.version in initialize reply (HTTP %d)" \
            % r.status_code
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:100])


# Envelope keys are IMPORTED from the QA superuser's classifier — the exact
# miscount (quota/_entity graded as "data") inverted a paid-vs-anon check on
# 2026-08-04 and then re-shipped in this module's first draft. Fallback keeps
# the module importable if the tools package moves; leading-underscore keys
# are envelope by rule either way.
try:
    from tools.qa_superuser.probe_mcp import ENVELOPE_KEYS as _ENVELOPE_KEYS
except Exception:  # noqa: BLE001
    _ENVELOPE_KEYS = {"quota", "upgrade", "next_session", "tool", "tease",
                      "trial_preview", "preview_is_partial", "platform",
                      "success", "resume", "agent_payment", "note", "ok",
                      "query", "count", "starter_pack", "for_your_human",
                      "retry_instructions", "persist_command"}


def _data_keys(sc: dict) -> list:
    """structuredContent keys that are DATA: not envelope, not _-prefixed,
    and not the tease scaffolding itself."""
    scaffold = {"tease", "tool", "upgrade", "next_session", "ok", "note",
                "query", "count"}
    return [k for k in (sc or {})
            if not k.startswith("_")
            and k not in _ENVELOPE_KEYS and k not in scaffold]


def _feed_bad(row) -> bool:
    """Any fault at all — LATE or RED. ★2026-09-02 (D2): the board split
    `overdue` (late) from `red` (ran, reported a fault); checks that ask
    "is this feed healthy" read the union, checks that ask "did it run on
    schedule" read `overdue`. Falls back to `overdue` for a board that
    predates the split."""
    if not row:
        return False
    return bool(row.get("unhealthy", row.get("overdue")))


def _deadman_feed(name):
    """Row for one feed on the deadman board, or (None, why)."""
    d, err = _jget(_local("/api/v1/ops/deadman"), timeout=10)
    if d is None:
        return None, err
    for row in d.get("feeds") or []:
        if row.get("feed") == name:
            return row, None
    return None, "feed '%s' not on the board" % name


def _src(relpath):
    """Own source file → (state, text): ("ok", str) | ("absent", None) |
    ("error", None). Absent and unreadable are DIFFERENT answers — collapsing
    them let the first draft of the secrets check read PASS off an IOError
    (three-valued-truth violation, caught in review)."""
    path = os.path.join(ROOT, relpath)
    if not os.path.exists(path):
        return "absent", None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "ok", f.read()
    except Exception:  # noqa: BLE001
        return "error", None


def _strip_comments(text: str) -> str:
    """Drop #-to-EOL comments (python AND yaml) before matching scheduler
    evidence. A commented-out _DISPATCH entry is the standard way a job gets
    disabled — substring matching over raw text graded exactly that as
    'scheduled' (the grep-test-passed-on-a-COMMENT class, re-proven in this
    module's review by mutation)."""
    return "\n".join(ln.split("#", 1)[0] for ln in text.split("\n"))


# ── the registered≠scheduled class fix (shared with CI) ───────────────

# Modules whose _beat_ledger legitimately has no tick route to schedule.
# An entry here requires a WRITTEN reason (the discovery-exemption pattern).
BEAT_SCHEDULER_EXEMPT = {
    "routes/competitor_gap_crawler.py":
        "beat fires inside run_competitor_gap(), which runs as part of the "
        "infrastructure-discovery pipeline, not from a tick route; the feed "
        "was green at audit time (parsed=1533, staged=200 on 2026-08-07).",
    "routes/pm_brief.py":
        "beat (feed pm-brief-collection) fires inside run_collection(), "
        "driven by the cron'd pm-brief-daily.yml workflow via POST "
        "/api/v1/admin/pm-brief/run — there is no master-tick route to "
        "schedule. The loop is double-watched (2026-08-14, Lens A1): "
        "deadman-watch tracks pm-brief-daily.yml last-success by the GH "
        "Actions API, and the producer beat covers the ledger fold, so this "
        "exemption cannot hide a dead loop.",
}


def scan_beat_scheduler_gaps(root=None):
    """Every routes/ module declaring `def _beat_ledger` must have a scheduler
    for its master-tick route: a cron_heartbeat _DISPATCH entry or a workflow
    with a real cron. Returns a list of human-readable gap strings — empty
    means the class is closed.

    This is the 4x-recurring class (shells #30-33 era, #34, #48, and the
    #50/#51 boards): a beat declared but never driven goes red on its ship day
    and stays red until a human notices. tests/test_shell_scheduler_coverage.py
    asserts this scan returns [] so the class cannot ship again; lane J runs
    the same scan live so drift between deploys is visible on the board too.
    """
    root = root or ROOT
    gaps = []
    try:
        heartbeat = _strip_comments(
            open(os.path.join(root, "routes/cron_heartbeat.py"),
                 encoding="utf-8").read())
    except Exception as e:  # noqa: BLE001
        return ["routes/cron_heartbeat.py unreadable: %s" % e]
    wf_scheduled = ""
    for wf in glob.glob(os.path.join(root, ".github/workflows/*.yml")):
        try:
            body = _strip_comments(open(wf, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            continue
        # dispatch-only is not a scheduler (#2027's lesson) — require a cron.
        if "schedule:" in body and "cron:" in body:
            wf_scheduled += body
    for path in sorted(glob.glob(os.path.join(root, "routes/*.py"))):
        rel = os.path.relpath(path, root)
        try:
            src = open(path, encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            continue
        if "def _beat_ledger" not in src:
            continue
        if rel in BEAT_SCHEDULER_EXEMPT:
            continue
        ticks = re.findall(r'\.route\(\s*"([^"]*master-tick[^"]*)"', src)
        if not ticks:
            gaps.append("%s declares _beat_ledger but has no master-tick "
                        "route and no written exemption" % rel)
            continue
        if not any(t in heartbeat or t in wf_scheduled for t in ticks):
            gaps.append("%s: no scheduler drives %s (not in cron_heartbeat "
                        "_DISPATCH, no cron'd workflow)" % (rel, ticks[0]))
    return gaps


# ── lanes ─────────────────────────────────────────────────────────────

def _lane_p0_incidents() -> list[dict]:
    """Zombie stack fallout: job-health stamp integrity + leader election."""
    out = []

    # ★2026-08-09 who-watches-the-watcher: assert deadman-watch (the GitHub-
    # Actions board writer) is itself alive. It beats "deadman-watch" every
    # run; this shell runs on the Railway APScheduler — a DIFFERENT scheduler —
    # so if the GH-Actions watcher goes dark, its self-beat goes stale and this
    # RED fires. Combined with deadman-watch already watching this shell's
    # audit-closure beat, the two independent schedulers watch each other and
    # neither can die unseen (the anti-drift keystone).
    row, why = _deadman_feed("deadman-watch")
    out.append(_check(
        "a_watcher", "the deadman watcher is itself alive (who-watches-the-"
        "watcher)",
        None if row is None else (not row.get("overdue")),
        "no deadman-watch self-beat yet (%s)" % why if row is None else
        ("alive — age %.1fh" % row.get("age_hours", -1)
         if not row.get("overdue") else
         "STALE %.1fh — the board writer may be dark; the whole board could be "
         "frozen-but-green" % row.get("age_hours", -1)),
        critical=True))

    row, why = _deadman_feed("loop-control-shell-daily")
    # ★2026-09-02 (D2): "beats on schedule" is a CADENCE question. Since
    # #3365 that shell beats lanes_failing whenever one of its own lanes is
    # red, and the board used to count that as overdue — so this check failed
    # on "overdue 0.0h: status=lanes_failing", a feed that had run six minutes
    # earlier. LATE fails here; RED is reported in the detail, and is that
    # shell's own board to read.
    _red = bool(row.get("red")) if row else False
    out.append(_check(
        "a_loopctl", "loop-control shell beats on schedule (SH52-001)",
        None if row is None else (not row.get("overdue")),
        why if row is None else
        (("beating — age %.1fh" % row.get("age_hours", -1)
          + (" — but RED (%s): its own lanes, not a cadence fault"
             % ("; ".join(row.get("reasons") or [])[:100]) if _red else ""))
         if not row.get("overdue") else
         "overdue %.1fh: %s" % (row.get("age_hours", -1),
                                "; ".join(row.get("reasons") or [])[:120])),
        critical=True))

    # health_signal is data-liveness lane 4: whether cron_last_run.last_status
    # can be trusted at all. It reads FAIL while the zombie's 401s (or any
    # unauthenticated caller) stamp job health. ★Imported and run in-process —
    # the first draft nested a full #51 tick over loopback HTTP, which review
    # showed keeps running server-side after the 25s client timeout (cascade
    # on the web replica). This closes SH52-115 (stamp integrity) ONLY; the
    # zombie-stack findings themselves (SH52-049/078/113) need the
    # decommission verified, not a proxy — they close by ack.
    try:
        from routes.data_liveness_master_shell import _lane_health_signal
        hs = _lane_health_signal()
        v = _lane_verdict(hs)
        detail = "; ".join(str(k.get("detail"))[:80] for k in hs[:2])
        out.append(_check(
            "a_stamps", "cron health stamps trustworthy (SH52-115)",
            None if v == "?" else (v == "PASS"),
            "health_signal=%s — %s" % (v, detail), critical=True))
    except Exception as e:  # noqa: BLE001
        out.append(_check("a_stamps", "cron health stamps trustworthy "
                          "(SH52-115)", None,
                          "health_signal lane unrunnable: %s: %s"
                          % (type(e).__name__, str(e)[:90]), critical=True))

    # Leader election: someone must hold the lock, and the board must be able
    # to say WHO (the 08-07 incident was undiagnosable because the holder was
    # anonymous — surface identity every tick).
    try:
        from routes.funnel_health import _conn
        c = _conn()
    except Exception:  # noqa: BLE001
        c = None
    if c is None:
        out.append(_check("a_leader", "singleton leader lock held (SH52-079)",
                          None, "no DB connection", critical=True))
    else:
        try:
            with c.cursor() as cur:
                # ★Session-level SET, not SET LOCAL: funnel_health._conn is
                # autocommit=True, where SET LOCAL is a warned no-op and the
                # query would run unbounded (review-proven on PG18). The
                # connection closes right after, so session scope is fine.
                cur.execute("SET statement_timeout = 8000")
                cur.execute(
                    "SELECT l.pid, COALESCE(a.application_name,''), "
                    "       COALESCE(a.client_addr::text,''), "
                    "       COALESCE(a.backend_start::text,'') "
                    "  FROM pg_locks l "
                    "  LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
                    " WHERE l.locktype = 'advisory' AND l.granted "
                    "   AND l.objid = 911714323")
                rows = cur.fetchall()
            # ★Deliberately UNMAPPED from SH52-079: that finding is the lock
            # held by the WRONG service, and pg_stat_activity cannot name the
            # service (the incident was undiagnosable for exactly that
            # reason). A holder-exists check closing a wrong-holder finding
            # would have read CLOSED during the incident itself (review
            # finding #27). SH52-079 closes when the attributable lease-row
            # rewire lands with its own checker.
            out.append(_check(
                "a_leader", "singleton leader lock held (holder surfaced; "
                "SH52-079 needs the attributable lease)",
                len(rows) >= 1,
                "holder pid=%s app='%s' addr=%s since %s" % (
                    rows[0][0], rows[0][1][:40], rows[0][2], rows[0][3][:19])
                if rows else
                "NO session holds lock 911714323 — election broken, brain "
                "and publishers are idling", critical=True))
        except Exception as e:  # noqa: BLE001
            out.append(_check("a_leader", "singleton leader lock held "
                              "(SH52-079)", None,
                              "pg_locks query failed: %s" % str(e)[:100],
                              critical=True))
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def _lane_secrets() -> list[dict]:
    out = []
    state, snap = _src("CONFIG_SNAPSHOT.md")
    # ★Deliberately UNMAPPED from SH52-122: this reads the DEPLOYED copy, but
    # the finding is about the PUBLIC REPO, where the values stay reachable
    # via the old SHA even after redaction — and rotation is not machine-
    # checkable from here. The check can prove DIRTY (red), never fully
    # clean; SH52-122 closes by ack after rotate+redact (review #30/#41).
    if state != "ok":
        out.append(_check("b_snapshot", "no credential values in the "
                          "deployed CONFIG_SNAPSHOT.md", None,
                          "file %s — cannot prove anything about the public "
                          "repo from here" % state, critical=True))
    else:
        # The recurrence grep from the 07-24 incident memory, run every tick.
        hits = re.findall(  # secretscan:allow — this IS the scanner's regex
            r"redis://[^:\s]+:[^@\s]{6,}@|api\.render\.com/deploy/[^?\s]+\?key="  # secretscan:allow
            r"|postgres(?:ql)?://[^:\s]+:[^@\s]{6,}@", snap)  # secretscan:allow
        out.append(_check(
            "b_snapshot", "no credential values in the deployed "
            "CONFIG_SNAPSHOT.md",
            False if hits else True,
            "%d credential-shaped value(s) in the PUBLIC repo doc — rotate "
            "then redact (SH52-122 stays open until acked post-rotation)"
            % len(hits) if hits else
            "deployed copy clean — ack SH52-122 once rotation is done",
            critical=True))
    d, err = _jget(_local("/api/land-power/status"), timeout=10)
    if d is None:
        out.append(_check("b_landpower", "no keys in served error strings "
                          "(SH52-050)", None, err, critical=True))
    else:
        blob = json.dumps(d)
        leaked = re.findall(r"api_key=[A-Za-z0-9]{8,}", blob)
        out.append(_check(
            "b_landpower", "no keys in served error strings (SH52-050)",
            len(leaked) == 0,
            "clean" if not leaked else
            "%d api_key value(s) served on the public status endpoint — "
            "redact persisted last_error and rotate" % len(leaked),
            critical=True))
    return out


def _lane_revenue_wall() -> list[dict]:
    out = []
    # ★UNMAPPED from SH52-102/129: the env flag on THIS web replica proves
    # neither that the gateway consumer exists nor that the worker/mcp
    # services share the flag — flag set ≠ enforcement live (review #29; the
    # audit itself proved the consumer was never written). The findings close
    # when an end-to-end wall checker exists (keyless burn past quota →
    # wall observed) or by ack once the quota task's fix is live-verified.
    flag = (os.environ.get("MONTHLY_QUOTA_ENFORCE") or "").strip()
    out.append(_check(
        "c_quota", "monthly quota flag armed on this service (posture only)",
        flag == "1",
        "MONTHLY_QUOTA_ENFORCE=1" if flag == "1" else
        "MONTHLY_QUOTA_ENFORCE=%r — the platform has no wall; wire the "
        "gateway consumer, then flip (audit P0-3)" % (flag or None),
        critical=True))
    # The $199 legacy Pro link on its 6 surfaces (SH52-103): source-inspect
    # the four backend carriers AND live-probe the two frontend pages the
    # audit named — closing a 6-surface finding on 4 surfaces was review #14.
    carriers = ("mcp_gatekeeper.py", "api_tier_gating.py",
                "routes/email_capture.py", "main.py")
    dirty = []
    unknown = []
    for rel in carriers:
        state, src = _src(rel)
        if state != "ok":
            unknown.append(rel + ":" + state)
        elif "eVq5kE4oOfs13mleGuaZi0h" in src:
            dirty.append(rel)
    for path, label in (("/app/", "fe:/app/"), ("/platform", "fe:/platform")):
        st, _h, body, err = _http(_edge(path), timeout=8)
        if err or st is None or st >= 400:
            unknown.append(label + ":" + (err or "HTTP %s" % st))
        elif "eVq5kE4oOfs13mleGuaZi0h" in body:
            dirty.append(label)
    out.append(_check(
        "c_legacy199", "legacy $199 Pro link retired from all six surfaces "
        "(SH52-103)",
        False if dirty else (None if unknown else True),
        ("still sold by: " + ", ".join(dirty)) if dirty else
        (("unreadable: " + ", ".join(unknown)) if unknown else
         "clean — 4 backend carriers + both live FE pages canon"),
        critical=False))
    try:
        import welcome_emails
        tier = getattr(welcome_emails, "WELCOME_CTA_TIER", None)
        out.append(_check(
            "c_drip", "drip CTA sells Founding while licenses remain "
            "(SH52-109)", tier == "founding",
            "WELCOME_CTA_TIER=%r%s" % (
                tier, "" if tier == "founding" else
                " — the only motion that has ever produced sales is Founding "
                "$99 (11 of 25 left at audit time)"), critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("c_drip", "drip CTA sells Founding while licenses "
                          "remain (SH52-109)", None,
                          "welcome_emails unimportable: %s" % str(e)[:80],
                          critical=False))
    return out


def _lane_first_call() -> list[dict]:
    """The three surfaces that misinform an agent on first contact."""
    out = []
    sc, err = _mcp("get_energy_prices", {"state": "VA"})
    _PAID_TIERS = ("pro", "paid", "developer", "starter", "founding",
                   "enterprise")
    if sc is None:
        out.append(_check("d_tier", "keyless caller never told it is paid "
                          "(SH52-019/039/124)", None,
                          "probe failed: %s" % err, critical=True))
    elif "caller_tier" not in sc:
        # ★Absent/renamed field is UNKNOWN, not honesty — a rename would
        # otherwise read PASS forever (review #35).
        out.append(_check("d_tier", "keyless caller never told it is paid "
                          "(SH52-019/039/124)", None,
                          "caller_tier field absent from the envelope — "
                          "cannot verify (renamed?)", critical=True))
    else:
        tier = str(sc.get("caller_tier") or "").lower()
        out.append(_check(
            "d_tier", "keyless caller never told it is paid "
            "(SH52-019/039/124)",
            tier not in _PAID_TIERS,
            "caller_tier=%r" % (sc.get("caller_tier"),) +
            ("" if tier not in _PAID_TIERS
             else " on a keyless call — the upgrade prompt never renders"),
            critical=True))
    sc, err = _mcp("get_iso_context", {"iso": "ERCOT"})
    if sc is None:
        out.append(_check("d_tease", "iso depth-tease carries actual data "
                          "(SH52-020)", None, "probe failed: %s" % err,
                          critical=False))
    else:
        # ★_data_keys excludes envelope keys (_entity, quota, ...) via the QA
        # superuser's classifier — counting those as data made this check
        # structurally unfailable in the first draft (review #2: the FAIL
        # branch was unreachable because _entity is stamped on every reply).
        data_keys = _data_keys(sc)
        if sc.get("tease"):
            out.append(_check(
                "d_tease", "iso depth-tease carries actual data (SH52-020)",
                bool(data_keys),
                "tease carries data %s" % (data_keys[:4],) if data_keys else
                "tease delivered ZERO data while advertising 'headline + "
                "top 3' — first-call impression is an empty upsell",
                critical=False))
        else:
            out.append(_check(
                "d_tease", "iso depth-tease carries actual data (SH52-020)",
                bool(data_keys) or None,
                "no tease — %d data key(s) delivered (%s)"
                % (len(data_keys), data_keys[:4]) if data_keys else
                "no tease but no data keys either — envelope-only reply, "
                "cannot grade the tease path this tick", critical=False))
    stats, err = _jget(_local("/api/ai/query?type=stats"), timeout=8)
    canon, cerr = _jget(_local("/api/v1/stats/canonical"), timeout=8)
    served = ((stats or {}).get("data") or {}).get("facilities")
    # ★Counts nest under "stats" (routes/facilities_by_dims.py) — the first
    # draft read the top level and graded nothing, forever, while a 45.6%
    # drift was live (review #3). Top-level fallback kept for shape changes.
    cstats = (canon or {}).get("stats") or canon or {}
    truth = cstats.get("facilities_distinct") if isinstance(
        cstats.get("facilities_distinct"), int) else None
    if served is None or truth is None:
        out.append(_check(
            "d_aiquery", "/api/ai/query stats match canon (SH52-123)", None,
            "unreadable: stats=%s canon=%s" % (err or "ok", cerr or "ok"),
            critical=True))
    else:
        drift = abs(served - truth) / float(truth) if truth else 1.0
        out.append(_check(
            "d_aiquery", "/api/ai/query stats match canon (SH52-123)",
            drift <= 0.10,
            "served %s vs canon %s (%.0f%% drift)%s" % (
                served, truth, drift * 100,
                "" if drift <= 0.10 else
                " — citation engines are quoting an undeduped count"),
            critical=True))
    return out


def _lane_surfaces() -> list[dict]:
    """One floor per file, one version, no retired claims (SH52-027..032)."""
    out = []

    def _floors(text, noun_re):
        return sorted(set(re.findall(
            r"([\d][\d,]*\+)\s+(?:[a-z-]+\s+){0,3}" + noun_re, text, re.I)))

    st, _h, llms, err = _http(_edge("/llms.txt"), timeout=10)
    fac_floor = deal_floor = None
    if err or st != 200:
        out.append(_check("e_llms", "llms.txt carries ONE facility floor and "
                          "ONE deal floor (SH52-027/125)", None,
                          err or "HTTP %s" % st, critical=True))
    else:
        fac = _floors(llms, r"facilit")
        deals = _floors(llms, r"(?:M&A\s+)?(?:transactions|deals)")
        fac_floor = fac[0] if len(fac) == 1 else None
        deal_floor = deals[0] if len(deals) == 1 else None
        out.append(_check(
            "e_llms", "llms.txt carries ONE facility floor and ONE deal "
            "floor (SH52-027/125)",
            len(fac) == 1 and len(deals) == 1,
            "facilities=%s deals=%s" % (fac, deals) +
            ("" if len(fac) == 1 and len(deals) == 1 else
             " — the file contradicts itself; heal regexes are noun-blind"),
            critical=True))
    st, _h, full, err = _http(_edge("/llms-full.txt"), timeout=10)
    if err or st != 200:
        out.append(_check("e_full", "llms-full.txt floors match llms.txt "
                          "(SH52-028)", None, err or "HTTP %s" % st,
                          critical=False))
    else:
        # ★Both nouns: SH52-028 names facilities AND deals; the first draft
        # compared facilities only, so a deals-only staleness would have
        # closed the finding (review #34).
        ffac = _floors(full, r"facilit")
        fdeals = _floors(full, r"(?:M&A\s+)?(?:transactions|deals)")
        if fac_floor is None or deal_floor is None:
            out.append(_check(
                "e_full", "llms-full.txt floors match llms.txt (SH52-028)",
                None, "llms.txt floors unresolved — nothing to compare "
                "against (facilities=%s deals=%s)" % (fac_floor, deal_floor),
                critical=False))
        else:
            ok = (ffac == [fac_floor] and fdeals == [deal_floor])
            out.append(_check(
                "e_full", "llms-full.txt floors match llms.txt (SH52-028)",
                ok, "llms-full facilities=%s deals=%s vs llms.txt %s/%s"
                % (ffac, fdeals, fac_floor, deal_floor), critical=False))
    card, err = _jget(_edge("/.well-known/mcp.json"), timeout=10)
    if card is None:
        out.append(_check("e_version", "advertised version == live server "
                          "version (SH52-031)", None, err, critical=False))
        out.append(_check("e_369gw", "retired 369 GW claim gone from the "
                          "card (SH52-032)", None, err, critical=False))
    else:
        adv = str(card.get("version") or "")
        blob = json.dumps(card)
        # The trap (mcp-health-topology): /mcp/health and mcp.json echo
        # PINNED back. The only honest comparison is a live initialize —
        # review proved the tool envelope carries NO _meta.server_version, so
        # this reads serverInfo.version from the handshake itself.
        live, sc_err = _mcp_server_version()
        if live:
            out.append(_check(
                "e_version", "advertised version == live server version "
                "(SH52-031)", adv == str(live),
                "card=%s live=%s" % (adv, live), critical=False))
        else:
            out.append(_check(
                "e_version", "advertised version == live server version "
                "(SH52-031)", None,
                "live version unreadable (%s); card=%s" % (sc_err, adv),
                critical=False))
        gw_clean = "369 GW" not in blob and "369GW" not in blob
        out.append(_check(
            "e_369gw", "retired 369 GW claim gone from the card (SH52-032)",
            gw_clean,
            "clean" if gw_clean else
            "'369 GW' still served in tool descriptions — the 07-27 "
            "take-down order missed the card", critical=False))
    phrases, perr = _jget(_local("/api/v1/canon/phrases"), timeout=8)
    fac_phrase = ((phrases or {}).get("facilities")
                  or (phrases or {}).get("facilities_phrase"))
    st, _h, agent_page, err = _http(_edge("/agent"), timeout=10)
    if err or st != 200 or not isinstance(fac_phrase, str):
        out.append(_check("e_agent", "/agent serves the canon facility floor "
                          "(SH52-029)", None,
                          err or perr or "HTTP %s / phrase %r" % (st, fac_phrase),
                          critical=False))
    else:
        out.append(_check(
            "e_agent", "/agent serves the canon facility floor (SH52-029)",
            fac_phrase in agent_page,
            "canon '%s' %s" % (fac_phrase,
                               "present" if fac_phrase in agent_page else
                               "ABSENT — PINNED is lagging a canon roll"),
            critical=False))
    return out


def _lane_frontend_seo() -> list[dict]:
    out = []
    st, _h, body, err = _http(_edge("/markets"), timeout=10)
    if err or st is None:
        out.append(_check("f_markets", "/markets serves the live 59-market "
                          "hub (SH52-072)", None, err, critical=False))
    else:
        good = ('href="https://dchub.cloud/markets/"' in body
                or "Data Center Markets" in body[:4000])
        out.append(_check(
            "f_markets", "/markets serves the live 59-market hub (SH52-072)",
            good,
            "static hub markers present" if good else
            "stale Railway 'Market Intelligence' page with a self-de-indexing "
            "canonical is still serving (worker ASSETS rewrite never fires)",
            critical=False))
    st, _h, robots, err = _http(_edge("/robots.txt"), timeout=8)
    if err or st != 200:
        out.append(_check("f_robots", "hygiene Disallows repeated in named "
                          "crawler groups (SH52-098)", None,
                          err or "HTTP %s" % st, critical=False))
    else:
        n = robots.count("Disallow: /*?")
        out.append(_check(
            "f_robots", "hygiene Disallows repeated in named crawler groups "
            "(SH52-098)", n >= 2,
            "%d group(s) carry 'Disallow: /*?' — RFC 9309 voids the '*' "
            "group's rules for named bots%s" % (
                n, "" if n >= 2 else "; Googlebot may crawl parameterized "
                "duplicates"), critical=False))
    # reveal partner feed must never be edge-cached (SH52-084): two reads of
    # the SAME URL — the second observes whether the first primed the edge.
    # ★The first draft cache-busted BOTH reads (never-seen keys can never be
    # a HIT), so the check would have closed the finding while the leak was
    # live — review proved MISS→HIT on a same-key re-read that same day. The
    # fresh=True flag bypasses the per-URL memo without changing the key.
    u = _edge("/api/v1/reveal-validation-feed")
    st1, h1, _b, e1 = _http(u, timeout=8, fresh=True)
    st2, h2, _b2, e2 = _http(u, timeout=8, fresh=True)
    if e1 or e2 or st1 is None or st2 is None:
        out.append(_check("f_reveal", "reveal partner feeds never edge-cached "
                          "(SH52-084)", None, e1 or e2, critical=False))
    else:
        cs = (h2.get("cf-cache-status") or "")
        out.append(_check(
            "f_reveal", "reveal partner feeds never edge-cached (SH52-084)",
            cs.upper() not in ("HIT",),
            "second same-key read cf-cache-status=%s" % (cs or "absent"),
            critical=False))
    return out


def _lane_media() -> list[dict]:
    out = []
    st, _h, rss, err = _http(_local("/api/v1/media/rss"), timeout=10)
    if err or st != 200:
        out.append(_check("g_press", "RSS feed carries press releases "
                          "(SH52-062)", None, err or "HTTP %s" % st,
                          critical=False))
    else:
        n = rss.count("press_release")
        out.append(_check(
            "g_press", "RSS feed carries press releases (SH52-062)", n > 0,
            "%d press_release item(s)" % n if n else
            "0 of 143 published releases reach the feed — news readers and "
            "crawlers see only template one-liners", critical=False))
    st, _h, neso, err = _http(
        _local("/api/press-releases/"
               "2026-07-17-neso-interconnection-queue-609-gw"), timeout=8)
    if st is None:
        out.append(_check("g_neso", "false NESO 'US queued capacity' release "
                          "corrected (SH52-061)", None, err, critical=False))
    elif st == 200:
        bad = bool(re.search(r"US Queued|all US queued", neso, re.I))
        out.append(_check(
            "g_neso", "false NESO 'US queued capacity' release corrected "
            "(SH52-061)", not bad,
            "still published with the US claim (NESO is the GB operator)"
            if bad else "published without the US claim", critical=False))
    elif st in (404, 410):
        out.append(_check("g_neso", "false NESO 'US queued capacity' release "
                          "corrected (SH52-061)", True, "release gone "
                          "(HTTP %d)" % st, critical=False))
    else:
        # ★A 500/403 is not evidence of correction — the first draft graded
        # every non-200 as 'release gone' (review #20).
        out.append(_check("g_neso", "false NESO 'US queued capacity' release "
                          "corrected (SH52-061)", None,
                          "HTTP %d — cannot tell corrected from broken" % st,
                          critical=False))
    mode = (os.environ.get("MEDIA_CLAIM_VERIFY") or "warn").strip()
    out.append(_check(
        "g_verify", "claim verification blocks, not warns (SH52-063)",
        mode == "block",
        "MEDIA_CLAIM_VERIFY=%r%s" % (
            mode, "" if mode == "block" else
            " — composed over-claims log a warning and SHIP"),
        critical=False))
    return out


def _lane_inventory() -> list[dict]:
    out = []
    row, why = _deadman_feed("osm-crawl")
    zero = row is not None and any("zero-row" in str(r)
                                   for r in row.get("reasons") or [])
    out.append(_check(
        "h_osm", "OSM crawl lands rows (SH52-002)",
        None if row is None else (not _feed_bad(row) and not zero),
        why if row is None else
        ("healthy — age %.1fh" % row.get("age_hours", -1)
         if not _feed_bad(row) and not zero else
         "; ".join(row.get("reasons") or ["overdue"])[:140]),
        critical=False))
    row, why = _deadman_feed("generator-inventory-ingest")
    out.append(_check(
        "h_geninv", "generator inventory ingest green (SH52-003/055)",
        None if row is None else (not _feed_bad(row)),
        why if row is None else
        ("green — age %.1fh" % row.get("age_hours", -1)
         if not _feed_bad(row) else
         "%s %.1fh (%s)" % ("overdue" if row.get("overdue") else "red",
                            row.get("age_hours", -1),
                            row.get("status"))), critical=False))
    ed, e1 = _jget(_local("/api/energy-discovery/status"), timeout=10)
    lp, e2 = _jget(_local("/api/land-power/status"), timeout=10)
    # ★energy-discovery nests under "data" (routes/energy_discovery_routes) —
    # the first draft read the top level and graded nothing (review #5).
    a = ((ed or {}).get("data") or {}).get("total_power_plants")
    if a is None:
        a = (ed or {}).get("total_power_plants")
    b = (((lp or {}).get("tables") or {}).get("power_plants")
         if isinstance((lp or {}).get("tables"), dict) else None)
    if not isinstance(a, int) or not isinstance(b, int):
        out.append(_check("h_plants", "power-plant twins agree (SH52-052)",
                          None, "unreadable: %s / %s" % (e1, e2),
                          critical=False))
    else:
        drift = abs(a - b) / float(max(a, b))
        out.append(_check(
            "h_plants", "power-plant twins agree (SH52-052)", drift <= 0.05,
            "energy-discovery=%d land-power=%d (%.1f%% apart)%s" % (
                a, b, drift * 100, "" if drift <= 0.05 else
                " — two tables, two loaders, one truth needed"),
            critical=False))
    return out


def _lane_brain() -> list[dict]:
    out = []
    d, err = _jget(_local("/api/v1/brain/mirror/report"), timeout=10)
    if d is None:
        out.append(_check("i_proposals", "brain converts findings into "
                          "proposals (SH52-040)", None, err, critical=False))
    else:
        # ★Both fields nest under _brain_status_snapshot (brain_mirror
        # _run_cycle) — the first draft read the top level (review #6).
        snap = d.get("_brain_status_snapshot") or d
        backlog = snap.get("actionable_findings_count")
        proposed = snap.get("proposed_fixes_count")
        if backlog is None or proposed is None:
            out.append(_check("i_proposals", "brain converts findings into "
                              "proposals (SH52-040)", None,
                              "mirror fields absent", critical=False))
        else:
            jammed = backlog > 10 and proposed == 0
            out.append(_check(
                "i_proposals", "brain converts findings into proposals "
                "(SH52-040)", not jammed,
                "backlog=%s proposed=%s%s" % (
                    backlog, proposed, " — the propose stage is jammed; "
                    "green-with-zero-output" if jammed else ""),
                critical=False))
    scout = (os.environ.get("DETECTOR_SCOUT_ENABLED") or "0").strip()
    out.append(_check(
        "i_scout", "detector scout is on and accruing (SH52-042)",
        scout == "1",
        "DETECTOR_SCOUT_ENABLED=%s%s" % (
            scout, "" if scout == "1" else
            " — the stated attack on the autonomy ceiling is dark; its "
            "2-week exit criterion can never accrue"), critical=False))
    return out


def _lane_class_guard() -> list[dict]:
    gaps = scan_beat_scheduler_gaps()
    out = [_check(
        "j_beats", "every declared beat has a scheduler (the SH52-001 class)",
        len(gaps) == 0,
        "all _beat_ledger modules scheduled" if not gaps else
        " | ".join(gaps)[:220], critical=True)]
    # Consumption posture for the pull-only boards (informational, UNMAPPED):
    # #51's health_signal is consumed in-process by lane A every tick;
    # loop-control is dispatched; registry-freshness was already dispatched
    # on main (hour 17 — the audit's 'undriven' claim was wrong for it, and
    # this branch briefly shipped a DUPLICATE label before review caught it).
    # ingestion-freshness (#50) still has no consumer — SH52-007/117 stay
    # OPEN until the rewire routes board verdicts into the deadman path;
    # ticking a pure-read endpoint on a cron changes nothing (review #39).
    state, hb_raw = _src("routes/cron_heartbeat.py")
    if state != "ok":
        out.append(_check("j_shells", "board consumption posture", None,
                          "cron_heartbeat.py %s" % state, critical=False))
    else:
        hb = _strip_comments(hb_raw)
        lc = "loop-control/master-tick" in hb
        rf = "registry-freshness/master-tick" in hb
        me = "audit-closure/master-tick" in hb
        out.append(_check(
            "j_shells", "board consumption posture",
            lc and rf and me,
            "loop-control %s · registry-freshness %s · audit-closure %s · "
            "#51 health_signal consumed in-process by lane A · #50 still "
            "unconsumed (SH52-007/117 open by design)" % (
                "dispatched" if lc else "UNDRIVEN",
                "dispatched" if rf else "UNDRIVEN",
                "dispatched" if me else "UNDRIVEN"), critical=False))
    return out


# ── registry ──────────────────────────────────────────────────────────
# All 138 findings from the 2026-08-07 audit. (id, domain, severity C/H/M/L,
# effort S/M/L, title). Machine verdicts attach via _CHECK_CLOSES; everything
# else stays OPEN until acked (AUDIT_CLOSURE_ACK) or a checker ships.

REGISTRY = [
    ("SH52-001", "heal", "H", "S",
     "loop-control shell (#48) beat has NO scheduler — 4th firing of the declared-beat-no-cron class; red 132h"),
    ("SH52-002", "heal", "H", "M",
     "OSM crawl fetched ZERO POIs for 9 consecutive runs; the 08-01-diagnosed masking (no exit(1) + deadman-watch overwri..."),
    ("SH52-003", "heal", "M", "M",
     "generator-inventory-ingest red 274h: synchronous 180s curl against a >3-minute admin ingest, weekly cron means one ..."),
    ("SH52-004", "heal", "M", "S",
     "qa-guards.yml: if:!cancelled() applied ONLY to Guard 7 — Guards 1/2/2b/4/5/6/6b/tier-parity still skip silently beh..."),
    ("SH52-005", "heal", "M", "S",
     "Contract healer has no liveness proof: zero public findings is indistinguishable from a wedged/all-None scan"),
    ("SH52-006", "heal", "M", "S",
     "Deep failover drill still unarmed: FAILOVER_DEEP_DRILL_ENFORCE='0' despite >1 week of clean runs"),
    ("SH52-007", "heal", "M", "M",
     "Growth/liveness shells (#50 ingestion-freshness, #51 data-liveness, registry-freshness) are pull-only: no tick, no ..."),
    ("SH52-008", "heal", "M", "L",
     "Red-feed remediation is fully manual and slow: detection→fix loop unclosed (LC5 gate never resolved)"),
    ("SH52-009", "heal", "L", "S",
     "Three separate armed auto-merge levers with scattered kill switches, plus a decoy DRY_RUN env var that kills none o..."),
    ("SH52-010", "heal", "L", "S",
     "White-glove auto-fan still human-gated while its downstream nudge is armed — stranded paying customers wait on the ..."),
    ("SH52-011", "agents", "H", "L",
     "Front-door adoption is ZERO: 0/242 episodes and 0/32 agents opened with execute_plan; only 3 lifecycle runs in 7d"),
    ("SH52-012", "agents", "H", "L",
     "Fleet contraction with ~93% churn: agents 97→34 rolling 7d; current fixed week tracking 23 vs 85 last week; 74% of ..."),
    ("SH52-013", "agents", "M", "S",
     "Quota ladder contradicts itself inside a single bind_email response and across the funnel: 5/10/25/50/100 for the s..."),
    ("SH52-014", "agents", "M", "M",
     "Agent-facing discovery + paywall surfaces serve 4 vintages of stale canon (15,000+/15,700+/21,900+/4,000+ deals vs ..."),
    ("SH52-015", "agents", "M", "S",
     "A2A agent-card advertises a 404 OAuth authorization-server metadata URL — the discovery path for header-less hosts ..."),
    ("SH52-016", "agents", "M", "M",
     "Gemini envelope partnership and the 07-11 partner-key program are dormant: 12+ comp pro keys at 0 calls, all human-..."),
    ("SH52-017", "agents", "M", "S",
     "Flagship anchor demo answers 'AVOID everything': the published intent 'rank markets for a 200 MW AI campus' returns..."),
    ("SH52-018", "agents", "L", "S",
     "Shell #49/#45 boards are tick-on-demand only (no cron, no deadman) — door/lane state cannot be confirmed and born-r..."),
    ("SH52-019", "funnel", "H", "S",
     "get_energy_prices tells keyless callers caller_tier='pro' — Aug-5 fix (mcp#136) misses every preview path"),
    ("SH52-020", "funnel", "H", "S",
     "get_iso_context depth-tease delivers ZERO data while claiming 'showing the headline + top 3'"),
    ("SH52-021", "funnel", "H", "L",
     "Wall-to-paid is 0 across 30d on BOTH branches; upsell clicks and MPP rail produce no settles"),
    ("SH52-022", "funnel", "M", "M",
     "Key re-mint worsened: ~18.9x redemption events per distinct agent (was 15x on 07-27); 35.2% of keys never make a call"),
    ("SH52-023", "funnel", "M", "S",
     "search_facilities anon note self-contradicts: 'showing 5 of 36' while the array is trimmed to 1 row"),
    ("SH52-024", "funnel", "L", "M",
     "Trial-cap accounting inconsistent within one session: preview served while full_answers_remaining_today=2"),
    ("SH52-025", "funnel", "L", "S",
     "Depth-tease upgrade_url loses tool attribution: 'tool=unknown'"),
    ("SH52-026", "funnel", "L", "M",
     "execute_plan market_comparison ran depth step for only ONE of the two compared markets"),
    ("SH52-027", "surfaces", "H", "S",
     "llms.txt self-contradicts on facilities and deals — re-diverged after merged fix #1115 because heal regexes are nou..."),
    ("SH52-028", "surfaces", "H", "S",
     "llms-full.txt is three canon generations stale (15,000+ facilities / 1,500+ deals) — never a heal target"),
    ("SH52-029", "surfaces", "H", "M",
     "Backend PINNED lags canon → /agent, /AGENTS.md and the worker card all serve 15,700+/1,600+"),
    ("SH52-030", "surfaces", "M", "M",
     "CF worker static manifest card stale: mcp.json description, server-card.json, agent.json, ai-plugin.json say 15,700..."),
    ("SH52-031", "surfaces", "M", "S",
     "Public manifest advertises version 2.5.0 while the live server is 2.11.1"),
    ("SH52-032", "surfaces", "M", "M",
     "Retired '369 GW / 540+ projects' pipeline claim still served by tools/list, smithery.yaml, and backend discovery ro..."),
    ("SH52-033", "surfaces", "M", "S",
     "/mcp-standing publishes '40 tools' for the Official MCP Registry beside a green verified badge"),
    ("SH52-034", "surfaces", "M", "M",
     "Smithery listing verification stuck for 10 days (verified 07-28, 73 tools vs live 82)"),
    ("SH52-035", "surfaces", "H", "L",
     "Systemic: 124 frontend files still carry the retired '15,000+' floor, 34 carry '1,500+' deals — heal covers ~30 files"),
    ("SH52-036", "surfaces", "M", "M",
     "Red fence lanes do not convert into fixes — surface-truth served_text has been failing while status reads 'success'"),
    ("SH52-037", "surfaces", "L", "S",
     "llms.txt replay block stamp contradicts the bake it claims to mirror (07-30 vs 08-03)"),
    ("SH52-038", "surfaces", "L", "S",
     "Local working-tree branches hold already-merged orphan twins — pushing them would regress live surfaces"),
    ("SH52-039", "brain", "H", "S",
     "Anonymous MCP callers are told they are on a paid tier (caller_tier='pro') — live now, red since 08-05, six merged ..."),
    ("SH52-040", "brain", "H", "M",
     "Detect→propose is the loop's stall point: 55 actionable findings, 0 pending code proposals, 0 open PRs, 0 brain-lan..."),
    ("SH52-041", "brain", "M", "S",
     "Duplicate spec-PR treadmill: same unfixed finding re-filed and merged 6x; 151 docs/brain-proposals files and 33 spe..."),
    ("SH52-042", "brain", "M", "S",
     "Detector scout (Phase 0 of the detector-supply pipeline — the stated attack on the real autonomy ceiling) is dark A..."),
    ("SH52-043", "brain", "M", "M",
     "L15↔janitor close/refile carousel still burning cycles on the same two unresolved themes (funnel contamination + at..."),
    ("SH52-044", "brain", "M", "M",
     "Brain actuation is backend-repo-only: defects living in dchub-mcp-server (like the live tier-envelope bug) can neve..."),
    ("SH52-045", "brain", "L", "S",
     "Stale issue hygiene: 8 slo-gate rollback-drill issues open ~10 days despite a supersede-closer, plus aging needs-hu..."),
    ("SH52-046", "brain", "M", "M",
     "Deadman standing red for 18 days: failover-canary last success ~92 days ago, and 8/20 loops unreadable by the watch..."),
    ("SH52-047", "brain", "M", "S",
     "Competitor-gap crawler: the Cloudscene sweep-row verification (whether the 8.1% coverage engine is actually sweepin..."),
    ("SH52-048", "brain", "L", "L",
     "Optimization engines remain diagnostic-only shells — 'arming' flags still change one JSON boolean and nothing else"),
    ("SH52-049", "inventory", "H", "M",
     "Phantom fleet identified: heroic-reprieve project runs frozen scheduler-v4 against stale-keyed twin backend — six j..."),
    ("SH52-050", "inventory", "H", "S",
     "EIA API key leaked in public error string on /api/land-power/status; eia-ng-pipelines feed dead 130 days on a malfo..."),
    ("SH52-051", "inventory", "H", "S",
     "data-sync 'Energy discovery per market' step failing ~40% of runs since Aug 4 — own anon gate 402s the workflow's k..."),
    ("SH52-052", "inventory", "H", "M",
     "Power-plants twin divergence live: /api/energy-discovery/status publishes 13,446 from abandoned power_plants_eia wh..."),
    ("SH52-053", "inventory", "H", "M",
     "Water pillar has no owned ingestion: usgs_water_stress is a zero-writer table frozen 2026-03-18 (water levels dated..."),
    ("SH52-054", "inventory", "M", "M",
     "Fiber identity: the UNIQUE(name,provider) cap is FIXED (#2544/#2622, routes now keyed on the upstream asset id) — s..."),
    ("SH52-055", "inventory", "M", "S",
     "generator-inventory-ingest failed its last scheduled run (curl_fail) — weekly with no retry, and the deadman board ..."),
    ("SH52-056", "inventory", "M", "L",
     "Substations upstream refresh permanently blocked pending identity strategy — HIFLD vintage pinned at 2026-03-17"),
    ("SH52-057", "inventory", "M", "S",
     "dchub-scheduler.py in dchub-backend is dead code that keeps misleading: never launched here, while a diverged v4 tw..."),
    ("SH52-058", "inventory", "M", "M",
     "Built acquisition sources shipped dark: air-permit discovery has no driver; parcel rollout stalled at 1 of 13 markets"),
    ("SH52-059", "inventory", "L", "S",
     "Growth instrumentation blind spots: infra_growth_snapshot has no subsea layer, and DCM env flag still reads armed w..."),
    ("SH52-060", "inventory", "L", "M",
     "Asset-layer counts have no canonical owner: MCP instructions hardcode drifting literals (13k plants, 94k transmissi..."),
    ("SH52-061", "media", "H", "S",
     "Known-false press release still live: 'NESO 609 GW = 35% of all US queued capacity' (NESO is the GB operator)"),
    ("SH52-062", "media", "H", "M",
     "RSS + JSON distribution feeds carry ZERO of the 143 press releases — only DCPI one-liners and testimonial stamps"),
    ("SH52-063", "media", "H", "S",
     "Canon claim-verification is warn-only on the social path and LinkedIn-only — X/Bluesky posts never get number verif..."),
    ("SH52-064", "media", "M", "M",
     "DCPI history slug split: documented '<city>-<st>' slugs serve a series frozen at 2026-07-28 while bare-city slugs c..."),
    ("SH52-065", "media", "M", "S",
     "feed-v3 alert rail publishes AVOID verdicts on named markets, contra the 07-02 positive-only directive"),
    ("SH52-066", "media", "M", "S",
     "Solicited AI responses published as 'testimonials' attributed to Claude/Perplexity, with approval defaulting open"),
    ("SH52-067", "media", "M", "M",
     "Scheduler job /api/jobs/content-publish silently dead (~7.3 days) — brain finding open and dup-suppressed; press ca..."),
    ("SH52-068", "media", "M", "M",
     "Press composer still fabricates metrics — gates are the only line of defense (0728 class recurring)"),
    ("SH52-069", "media", "L", "S",
     "Same-vendor contradictory usage stamps on the public feed (fingerprint fragmentation)"),
    ("SH52-070", "media", "L", "S",
     "k=1 usage disclosure: single-caller tool patterns published as testimonials"),
    ("SH52-071", "media", "L", "M",
     "DCPI mover lane still tuned for the flat-index era even though DCPI now moves daily"),
    ("SH52-072", "frontend", "H", "S",
     "/markets and /markets/ serve the stale Railway 'Market Intelligence' page with a self-de-indexing canonical instead..."),
    ("SH52-073", "frontend", "M", "S",
     "llms.txt live self-contradicts on the facility count: 16,900+ and 15,700+ in the same file; the fix branch has no PR"),
    ("SH52-074", "frontend", "M", "S",
     "Auto-heal misses high-visibility surfaces: /map <title> says '15,000+ Facilities Worldwide', /about shows '15,000+'..."),
    ("SH52-075", "frontend", "L", "M",
     "Admin/ops shells are publicly reachable unauthenticated: /admin, /admin-qa (internal bug inventory), /admin-outreac..."),
    ("SH52-076", "frontend", "M", "S",
     "The /markets worker ASSETS-rewrite failure mode is silent — no guard catches a Pages-static page being shadowed by ..."),
    ("SH52-077", "frontend", "L", "M",
     "brand.css wildcard !important rules remain a recurring foot-gun (4 confirmed incidents) with no naming lint"),
    ("SH52-078", "backend", "C", "S",
     "Stale admin key on heroic-reprieve scheduler: every scheduled job 401s since the 07-31 key rotation"),
    ("SH52-079", "backend", "C", "M",
     "Leader lock held outside the designated dchub-worker — brain autonomous cycles and all publishers idle for 5+ hours"),
    ("SH52-080", "backend", "H", "M",
     "Web service memory regression: avg 2.7GB, peak 4.9GB vs the 1.9GB GC design threshold"),
    ("SH52-081", "backend", "H", "M",
     "Recurring 500s on GET /api/v1/transactions and /api/v1/deals (plus marketing/auto-generate 500, brain/propose-detec..."),
    ("SH52-082", "backend", "H", "S",
     "Chronic primary-pool saturation: 95% bursts driven by crawler traffic on primary-pool reads"),
    ("SH52-083", "backend", "M", "S",
     "CF ROUTE_TIMEOUTS still has zero /api/v1/admin/ entries — any admin POST >15s through the edge silently 503s"),
    ("SH52-084", "backend", "M", "S",
     "/api/v1/reveal-* partner feeds still Rule-#3 edge-cached (known-open bypass never applied)"),
    ("SH52-085", "backend", "M", "M",
     "Forgeable Referer map bypass still live 2+ months past its removal date, actively used by plain curl"),
    ("SH52-086", "backend", "M", "S",
     "News feed carries future-dated rows → trust surface reports platform 'degraded'"),
    ("SH52-087", "backend", "M", "M",
     "heroic-reprieve twin stack drifts: worker service 25 days stale, different admin key, participates in singleton ele..."),
    ("SH52-088", "backend", "L", "S",
     "502 blips on MCP-called backend POSTs (track/auto-mint/signal-paywall) at 18:56Z"),
    ("SH52-089", "backend", "L", "S",
     "Neon post-migration loose ends: token rotation + read-URL repoint unverified, hardcoded Arizona hosts in repo scripts"),
    ("SH52-090", "backend", "L", "S",
     "R2 backup RPO widened from 6h to 24h (deliberate, but now the only off-Neon recovery layer)"),
    ("SH52-091", "seo", "H", "S",
     "/api/v1/ai/reach/trend still publishes impossible numbers — unfixed since the 0805 diagnosis"),
    ("SH52-092", "seo", "H", "M",
     "561 /markets + /pockets pages are internal-link dead ends with ~450 words — the money-query page class has no crawl..."),
    ("SH52-093", "seo", "H", "S",
     "Bing decision window is NOW and the channel is blind: Bingbot 7 requests/7d in our tracking, robots /api/ revert du..."),
    ("SH52-094", "seo", "M", "M",
     "Google money queries: 0 of 5 probed SERPs show dchub.cloud — query-win pages not ranking yet, competitors are"),
    ("SH52-095", "seo", "M", "M",
     "The claude 'AI crawler' counter is ~93% self-instructed metadata — feed was de-polluted, counters were not"),
    ("SH52-096", "seo", "M", "L",
     "~10k international facility pages remain thin (99-107 unique words) — the unrescued half of the long tail"),
    ("SH52-097", "seo", "M", "S",
     "Public /reach surface still leads with rolling-window WoW (-64.9%) — the exact artifact class 0805 retired for press"),
    ("SH52-098", "seo", "L", "S",
     "robots.txt named-crawler groups do not repeat 'Disallow: /*?' or '/admin/' — void for Googlebot/Bingbot per RFC 930..."),
    ("SH52-099", "seo", "L", "S",
     "/us-data-center-map understates its own count (4,700+) vs live 5,203 US facilities, and the heal-dodge guarantees i..."),
    ("SH52-100", "seo", "L", "M",
     "/answers content hub frozen since 07-03 — a GEO surface advertising weekly freshness that never changes"),
    ("SH52-101", "seo", "L", "S",
     "Facility pages link to robots-blocked /sites/<slug> URLs sitewide"),
    ("SH52-102", "revenue", "C", "M",
     "Paid/free caps still unenforced end-to-end on /mcp — Phase 2 enforcement shipped dark with ZERO consumers on the pu..."),
    ("SH52-103", "revenue", "H", "S",
     "Legacy $199 Pro link (eVq5kE4oOfs13mleGuaZi0h) still sold on 6 live surfaces — two Pro prices coexist, purchases bo..."),
    ("SH52-104", "revenue", "H", "M",
     "Agent-discovery /.well-known/mcp.json serves a typo'd Developer checkout link + main.py advertises two links canon ..."),
    ("SH52-105", "revenue", "H", "M",
     "Agent rail has never produced a sale: funnel dies at re-mint (19x) and first-call (34.9% drop), then 26 checkout li..."),
    ("SH52-106", "revenue", "H", "M",
     "Pay-offer reachability for real agents is still ~0 — the prewall offer surface is consumed almost entirely by our o..."),
    ("SH52-107", "revenue", "M", "M",
     "No enforced differentiation inside the paid ladder: $9 starter ≈ $49 developer, $99 founding == $299 pro"),
    ("SH52-108", "revenue", "M", "S",
     "/developers CTA still sells the founding plan under an 'Upgrade to Pro →' label"),
    ("SH52-109", "revenue", "M", "S",
     "Drip CTA sells $299 Pro while 11 Founding licenses at $99 — the only proven converter — sit unsold"),
    ("SH52-110", "revenue", "M", "M",
     "1,556-row paid-intent lead ledger with ISO-tagged site queries is captured but unworked"),
    ("SH52-111", "revenue", "L", "S",
     "Upgrade-offer A/B rig sits killed with arm B starved at 1 impression"),
    ("SH52-112", "revenue", "M", "S",
     "NLR Year-2 ($10K clause) re-engagement still pending — the largest single license on the books is stranded"),
    ("SH52-113", "sched", "C", "M",
     "Zombie scheduler service fires ~34 jobs/day with rotated-out admin key — six jobs dead since 07-31, cron health sta..."),
    ("SH52-114", "sched", "H", "S",
     "dchub-jobs.yml 'outreach' and 'promotion' arms dispatch to nonexistent endpoints — 404 inside green runs"),
    ("SH52-115", "sched", "M", "S",
     "Unauthenticated /api/jobs/* requests stamp cron_last_run completion — health signal is spoofable and staleness dete..."),
    ("SH52-116", "sched", "M", "M",
     "Twin backend service dchub-api (heroic-reprieve) auto-deploys current main with stale env against the production es..."),
    ("SH52-117", "sched", "M", "S",
     "Newest liveness shells' master-ticks are driven by nothing — their FAIL verdicts reach nobody"),
    ("SH52-118", "sched", "M", "M",
     "Land-power crawl still has two drivers and no durable single-flight"),
    ("SH52-119", "sched", "M", "M",
     "/api/land-power/status is permanently 'degraded' — one source deliberately write-blocked, another superseded but st..."),
    ("SH52-120", "sched", "L", "S",
     "gas-pipeline-ingest.yml turns a missing admin key into a green no-op"),
    ("SH52-121", "sched", "L", "S",
     "dchub-scheduler.py remains a 34-job decoy registry on main, with admin keys embedded in URL query params that get l..."),
    ("SH52-122", "security", "C", "M",
     "CONFIG_SNAPSHOT.md in PUBLIC repo still exposes a live Redis password and Render deploy-hook key"),
    ("SH52-123", "security", "H", "S",
     "/api/ai/query?type=stats hands AI agents a raw undeduped 24,675-facility count in a citation suggested_response"),
    ("SH52-124", "security", "M", "M",
     "get_energy_prices tells an anonymous, un-keyed MCP caller it is on the 'pro' tier"),
    ("SH52-125", "security", "M", "S",
     "llms.txt contradicts its own facility and deal counts (15,700+ vs 16,900+; 1,600+ vs 1,700+)"),
    ("SH52-126", "security", "M", "M",
     "Rate limiter is fully bypassed by a spoofable Origin/Referer substring and by any 'dchub-' User-Agent"),
    ("SH52-127", "security", "L", "S",
     "Legacy internal-key literal dchub-internal-sync-2026 re-documented in current public-repo docstrings"),
    ("SH52-128", "security", "L", "S",
     "Old Azure Neon connection strings (host + neondb_owner) embedded in many tracked ingestion scripts"),
    ("SH52-129", "product", "H", "S",
     "Monetization enforcement dark for 4+ weeks: quota + metered billing built but OFF, metered billing still a scaffold"),
    ("SH52-130", "product", "M", "S",
     "/api/v1/iso/zones counts Brazil and Korea zones as US — the global-coverage claim surface contradicts the live feeds"),
    ("SH52-131", "product", "H", "M",
     "DC-load interconnection queue still null for all US ISOs except ERCOT — grid-moat gap #3 half-open"),
    ("SH52-132", "product", "M", "M",
     "DCPI local-granularity clips saturate in dense metros — intra-metro clones are back, and the promised provenance ne..."),
    ("SH52-133", "product", "M", "M",
     "Taxonomy over-claims two in_scope classes: no PPA-benchmark dataset and no colo pricing/vacancy data behind them"),
    ("SH52-134", "product", "M", "M",
     "Rollout velocity turned inward: 202 fix vs 68 feat in 14d, ~1 new agent-facing capability, and all remaining distri..."),
    ("SH52-135", "product", "M", "M",
     "Intl grid expansion stalled since 07-11: India, Mexico, Singapore-official all researched-not-built while LandGate/..."),
    ("SH52-136", "product", "L", "S",
     "RFC 8414 authorization-server metadata 404s live on both hosts despite the module advertising it"),
    ("SH52-137", "product", "L", "S",
     "L15 calibration harness still guards exactly one tool, two months after being built as a generic registry"),
    ("SH52-138", "product", "L", "S",
     "DCPI 3/12/24-month forecast projects constraint AND excess both at 100 with implied AVOID for a mid-tier market — l..."),
]

# check-id → finding ids it closes when it reads PASS. ★Mapping discipline
# (review #7/#9/#27/#29/#30/#33): a check may close ONLY what it actually
# measures. Proxy signals (env flags, health-stamp integrity, holder-exists)
# do NOT close incident/e2e findings — those close by ack after their fix is
# verified, or when a real end-to-end checker ships. That is why a_leader,
# b_snapshot and c_quota appear in no entry here despite guarding lanes.
# ── closeout lane: checkers for findings resolved in the 2026-08-08 grind ──
# These verify the FIX still holds (so a regression re-opens the finding) —
# the VERIFY half of the loop, written by the auditor, never by the thing
# being scored. Each is three-valued: unreachable is '?', never a false close.

def _lane_closeout() -> list[dict]:
    out = []

    # SH52-130: /api/v1/iso/zones no longer miscounts Brazil/Korea as US.
    d, err = _jget(_local("/api/v1/iso/zones"), timeout=10)
    c = (d or {}).get("countries") or {}
    out.append(_check(
        "z_isozones", "iso/zones counts BR + KR as their own countries "
        "(SH52-130)",
        None if d is None else bool(c.get("BR") and c.get("KR")),
        err if d is None else "BR=%s KR=%s" % (c.get("BR"), c.get("KR")),
        critical=False))

    # SH52-060: stats/canonical carries per-layer asset counts (no more
    # hardcoded drifting literals).
    d, err = _jget(_local("/api/v1/stats/canonical"), timeout=8)
    s = (d or {}).get("stats") or d or {}
    out.append(_check(
        "z_assetcanon", "stats/canonical exposes per-layer asset counts "
        "(SH52-060)",
        None if d is None else ("power_plants" in s or "transmission_lines" in s),
        err if d is None else "keys: %s" % [k for k in s
                                            if "plant" in k or "transmission" in k][:3],
        critical=False))

    # SH52-138: DCPI forecast no longer prints excess=100 AND constraint=100
    # with an AVOID verdict (the clamp artifact).
    d, err = _jget(_local("/api/v1/dcpi/scores/allen"), timeout=8)
    proj = (((d or {}).get("forecast") or {}).get("projection") or {})
    bad = any(isinstance(p, dict) and p.get("excess_power_score") == 100
              and p.get("constraint_score") == 100 for p in proj.values()
              if isinstance(proj, dict)) if proj else None
    out.append(_check(
        "z_dcpiforecast", "DCPI forecast is not clamped to 100/100 AVOID "
        "(SH52-138)",
        None if d is None else (bad is False or bad is None) if proj
        else None,
        err if d is None else ("no 100/100 clamp" if not bad
                               else "still clamps 100/100"),
        critical=False))

    # SH52-033: /mcp-standing no longer publishes an implausible tool count
    # beside a verified badge.
    d, err = _jget(_local("/api/v1/mcp/standing"), timeout=10)
    if d is None:
        out.append(_check("z_standing", "no implausible tool counts on "
                          "/mcp-standing (SH52-033)", None, err, critical=False))
    else:
        rows = d.get("registries") or d.get("rows") or []
        bad = [r for r in rows if isinstance(r, dict)
               and isinstance(r.get("tools"), int)
               and not (0.5 * 82 <= r["tools"] <= 1.5 * 82)]
        out.append(_check(
            "z_standing", "no implausible tool counts on /mcp-standing "
            "(SH52-033)", len(bad) == 0,
            "clean" if not bad else "implausible: %s"
            % [(r.get("registry"), r.get("tools")) for r in bad][:3],
            critical=False))

    # SH52-015: the A2A card's OAuth authorization-server metadata URL resolves
    # (was a 404 dead-end).
    card, err = _jget(_edge("/.well-known/agent-card.json"), timeout=10)
    if card is None:
        out.append(_check("z_a2a_oauth", "A2A card OAuth metadata URL is not a "
                          "404 (SH52-015)", None, err, critical=False))
    else:
        asm = (((card.get("auth") or {}).get("oauth2") or {})
               .get("authorization_server_metadata") or "")
        # a resolvable AS metadata (WorkOS authkit or a served proxy), not the
        # old api.dchub.cloud/.well-known/oauth-authorization-server 404.
        ok = bool(asm) and "authkit" in asm.lower() or (
            asm and not asm.endswith("oauth-authorization-server"))
        st, _h, _b, e2 = _http(asm + ("?_a=1" if asm else ""), timeout=8) \
            if asm else (None, {}, "", "no url")
        out.append(_check(
            "z_a2a_oauth", "A2A card OAuth metadata URL resolves (SH52-015)",
            None if st is None else (st == 200),
            "url=%s -> %s" % (asm[:60], st if st else e2), critical=False))

    # SH52-126: the rate-limiter same-origin bypass keys on HOST, not a
    # spoofable substring.
    state, txt = _src("rate_limiter.py")
    if state != "ok":
        out.append(_check("z_ratelimit", "rate-limiter bypass is host-exact "
                          "(SH52-126)", None, "rate_limiter.py %s" % state,
                          critical=False))
    else:
        # ★2026-09-02 — THIS CHECK USED TO GREP, AND PROSE IS NOT CODE. It read
        #     substr = "'dchub.cloud' in origin" in txt
        # against the whole file, so the two COMMENTS that explain the original
        # vulnerability (rate_limiter.py's header block, and the note left where
        # the second call site was deleted) both matched it. The check could not
        # go green no matter what the code did — and symmetrically it would have
        # gone green on a live `if "dchub.cloud" in origin_header:` that merely
        # spelled the variable differently. Parse the module instead and look for
        # a real `'<something with dchub.cloud>' in <name>` comparison NODE.
        host_gate = "_origin_host_is_trusted" in txt
        try:
            tree = ast.parse(txt)
        except SyntaxError as e:  # unparseable is NOT clean — three-valued truth
            out.append(_check(
                "z_ratelimit", "rate-limiter bypass is host-exact (SH52-126)",
                None, "rate_limiter.py did not parse: %s" % str(e)[:80],
                critical=False))
            tree = None
        if tree is not None:
            substr_nodes = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Compare)
                and any(isinstance(o, ast.In) for o in n.ops)
                and isinstance(n.left, ast.Constant)
                and isinstance(n.left.value, str)
                and "dchub.cloud" in n.left.value
            ]
            substr = bool(substr_nodes)
            where = ", ".join("line %d" % n.lineno for n in substr_nodes[:4])
            out.append(_check(
                "z_ratelimit", "rate-limiter bypass is host-exact (SH52-126)",
                host_gate and not substr,
                "host-allowlist present; no substring-origin comparison in the AST"
                if host_gate and not substr
                else "host_gate=%s substring_compare=%s%s" % (
                    host_gate, substr, (" at %s" % where) if where else ""),
                critical=False))

    # SH52-127/128: legacy internal-key literal and pre-migration Azure Neon
    # strings scrubbed from tracked source.
    leaked = []
    for rel in ("flask_mcp_endpoints.py", "routes/admin_ai_deals.py",
                "routes/stripe_metered.py"):
        st, t = _src(rel)
        if st == "ok" and "dchub-internal-sync-2026" in t:
            leaked.append(rel)
    out.append(_check(
        "z_intkey", "legacy internal-key literal scrubbed from live docstrings "
        "(SH52-127)", len(leaked) == 0,
        "clean" if not leaked else "still present in: " + ", ".join(leaked),
        critical=False))
    azure = []
    for rel in ("expand_countries.py", "load_hifld_transmission.py",
                "quarterly_refresh.py"):
        st, t = _src(rel)
        if st == "ok" and "ep-old-waterfall" in t:
            azure.append(rel)
    out.append(_check(
        "z_azure", "pre-migration Azure Neon strings replaced with placeholders "
        "(SH52-128)", len(azure) == 0,
        "clean" if not azure else "still present in: " + ", ".join(azure),
        critical=False))

    # SH52-057: dchub-scheduler.py carries a NEVER-LAUNCHED banner (dead-code
    # trap that burned audit sessions).
    st, t = _src("dchub-scheduler.py")
    out.append(_check(
        "z_deadsched", "dchub-scheduler.py flagged NEVER-LAUNCHED (SH52-057)",
        None if st != "ok" else ("NEVER" in t[:2000].upper()
                                 and "LAUNCH" in t[:2000].upper()),
        "banner present" if st == "ok" and "NEVER" in t[:2000].upper()
        else "dchub-scheduler.py %s" % st, critical=False))

    # SH52-074/099: high-visibility frontend counts healed off the retired
    # 15,000+/4,700+ floors (live probe — the backend can't read FE source).
    st, _h, mp, err = _http(_edge("/map"), timeout=10)
    out.append(_check(
        "z_map_floor", "/map title off the retired 15,000+ floor (SH52-074)",
        None if err or st != 200 else ("15,000+" not in mp[:3000]),
        err or "HTTP %s" % st if st != 200 else
        ("healed" if "15,000+" not in mp[:3000] else "still 15,000+"),
        critical=False))
    st, _h, ud, err = _http(_edge("/us-data-center-map"), timeout=10)
    out.append(_check(
        "z_usmap_floor", "/us-data-center-map off the 4,700+ floor (SH52-099)",
        None if err or st != 200 else ("4,700+" not in ud[:4000]),
        err or "HTTP %s" % st if st != 200 else
        ("bumped" if "4,700+" not in ud[:4000] else "still 4,700+"),
        critical=False))

    return out


# Findings resolved by a MERGED PR that either cannot regress (a one-time
# cleanup) or lives in a repo/surface this backend shell cannot probe
# (frontend workflow config, a deleted branch, closed issues, decommissioned
# infra). Closed with recorded, version-controlled evidence — NOT a hidden
# env ack, and never over a checker that says otherwise.
_EVIDENCE_ACKED = {
    "SH52-004": "FE #1142 — if:!cancelled() on every qa-guards.yml static guard",
    "SH52-083": "FE _worker.js ROUTE_TIMEOUTS '/api/v1/admin/':120000 (frontend repo)",
    "SH52-038": "merged-orphan fix/llms-canon-floors branch deleted local+remote",
    "SH52-045": "7 stale slo-gate drill issues closed via gh (#1860/61/70/71/79/81/84)",
    "SH52-059": "DCM_CRAWL_ENABLED disabled_reason breadcrumb added",
    "SH52-090": "R2 RPO<=24h widening recorded in RESTORE_RUNBOOK.md",
    "SH52-101": "rel=nofollow on the /sites/<slug> facility-page links",
    "SH52-108": "FE /developers CTA relabelled to the canonical Founding checkout",
    "SH52-114": "#2461 dchub-jobs.yml outreach/promotion arms repointed + 4xx fatal",
    "SH52-120": "#2461 gas-pipeline-ingest.yml missing-key exit 1 (not green no-op)",
    "SH52-023": "mcp #159 search_facilities anon note rewritten to match trimmed rows",
    "SH52-049": "heroic-reprieve decommissioned (Railway project gone)",
    "SH52-078": "heroic-reprieve zombie scheduler decommissioned",
    "SH52-087": "heroic-reprieve twin stack decommissioned",
    "SH52-113": "heroic-reprieve Railway project absent from the workspace",
    "SH52-116": "heroic-reprieve/dchub-api twin decommissioned",
    "SH52-044": "#2350 detector scout armed + cross-repo routing landed",
    "SH52-008": "#2350 deadman per-feed triage router (red→work-item, auto-close)",
    "SH52-041": "#2350 landed-spec fingerprint dedup (kills the 6x treadmill)",
    "SH52-051": "#2411 data-sync per-market 402 fixed (X-Internal-Key on the curl)",
    "SH52-056": "identity strategy shipped (hifld_id partial-unique) + owner-gated "
                "2026-08-14 supervised HIFLD backfill ran: held rows keyed and "
                "refreshed in place, 2026-03-17 vintage pin discharged",
    "SH52-079": "leader lock back on resourceful-essence/dchub-worker (post-decommission)",
    "SH52-075": "FE #1145 noindex + robots Disallow on the admin/ops shells",
    "SH52-122": "CONFIG_SNAPSHOT.md redacted (0 credential lines on origin/main) + creds rotated",
}

# The 80 findings the grind routed to a human owner — builds, commercial/BD
# calls, diagnosis, or one env-flip decisions. Tagged (owner, reason) so the
# board reads them as OWNED-DEFERRED, not broken-open. A checker still WINS:
# a deferred finding a checker marks OPEN-RED is broken regardless.
DEFERRED = {"SH52-005": ("build", "The fix ('give the contract healer a liveness beat') is a small feature, not a canon/con"), "SH52-006": ("commercial", "Owner/infra action outside the PR surface: arming the deep failover drill is a Railway e"), "SH52-007": ("build", "~75% resolved and the remaining piece is an architectural judgement the shell author del"), "SH52-009": ("commercial", "Consolidation + judgement, not mechanical: three separately-armed auto-merge levers with"), "SH52-010": ("commercial", "Product/BD judgement: the white-glove auto-fan is human-gated while its downstream nudge"), "SH52-011": ("commercial", "Front-door (execute_plan) adoption is 0/242 episodes, 0/32 agents. The tool itself is ve"), "SH52-012": ("diagnose", "Fleet contraction ~93% churn (agents 97→34 rolling 7d; 74% of calls from 2 platforms). R"), "SH52-013": ("commercial", "The free/identified quota ladder self-contradicts (5/10/25/50/100 across bind_email mess"), "SH52-014": ("build", "Agent-facing surfaces (/.well-known/agent.json v2.5.0 15,700+, agent-card.json 15,000+ x"), "SH52-016": ("commercial", "Gemini envelope partnership + the 07-11 partner-key program are dormant (12+ comp pro ke"), "SH52-017": ("owner-flag", "The flagship anchor intent 'rank markets for a 200 MW AI campus' returns AVOID-only mark"), "SH52-018": ("judgment", "Shell #49/#45 (agent retention/expansion) boards are tick-on-demand only — no cron, no d"), "SH52-021": ("commercial", "Wall-to-paid = 0 on both agent and human branches (26 checkout links → 0 paid). Not a co"), "SH52-022": ("judgment", "Key re-mint 18.9x / 35.2% of keys never call. Fixing it changes claim_free_key mint cont"), "SH52-024": ("diagnose", "Trial-cap accounting inconsistent (preview served while full_answers_remaining_today=2)."), "SH52-025": ("build", "VERIFIED still live (get_iso_context tease upgrade_url = …/pricing/upgrade?tool=unknown&"), "SH52-026": ("diagnose", "execute_plan comparison ran the depth step for only one of two markets. The fix changes "), "SH52-034": ("diagnose", "The finding's substance is a crawler STALL — the 20:20 UTC registry-truth scan stopped r"), "SH52-035": ("build", "[HIGH/gap/L] Systemic: 124 frontend files (+ dozens of backend files) carry the retired "), "SH52-036": ("build", "Red fence lanes (surface-truth served_text=FAIL, loop-control surface_canon=FAIL) do not"), "SH52-037": ("judgment", "llms.txt's replay-block stamp still contradicts the backend bake (live llms.txt L292 'ca"), "SH52-043": ("judgment", "L15↔janitor close/refile carousel keeps burning cycles on two unresolved themes (funnel "), "SH52-046": ("build", "Deadman standing red 18 days: failover-canary last success ~92 days ago (real DR risk on"), "SH52-047": ("judgment", "Whether the competitor-gap crawler's Cloudscene sweep actually runs (64% of inventory gr"), "SH52-048": ("build", "Optimization engines remain diagnostic-only shells (armed=False, executes=False, executi"), "SH52-052": ("build", "NOT safely mechanical despite the guidance hint. Live still publishes total_power_plants"), "SH52-053": ("build", "BUILD (data ingestion). Confirmed on origin/main: zero WRI_AQUEDUCT/aqueduct references "), "SH52-054": ("build", "Cap fixed (#2544/#2622). Still owned: frozen duplicate twins published un-pruned, and mos"), "SH52-058": ("build", "BUILD + BD/judgment. Confirmed on origin/main: no air-permit reference in .github/workfl"), "SH52-064": ("judgment", "DCPI history slug split still live: cheyenne-wy frozen at 2026-07-28 (49 pts, 1 distinct"), "SH52-065": ("build", "feed-v3 alert rail still leaks AVOID (21 AVOID items live) + testimonials, contra the 07"), "SH52-066": ("judgment", "Solicited AI answers published as 'testimonials' with approval defaulting open. dchub_me"), "SH52-067": ("owner-flag", "/api/jobs/content-publish silently dead ~7.3 days; its only driver was the rotated-out z"), "SH52-068": ("build", "Press composer still fabricates metrics (gate-dropped a release 2026-08-07); eliminating"), "SH52-069": ("build", "Same-vendor contradictory usage stamps on the public feed (two 'Claude cited DC Hub' cou"), "SH52-070": ("build", "k=1 usage disclosure: _ingest_mcp_derived HAVING (COUNT(*) >= 20) has no distinct-caller"), "SH52-071": ("build", "dcpi_mover lead kind is tuned for the flat-index era (requires abs(delta) >= 5 WoW, de-w"), "SH52-072": ("owner-flag", "The audit's _routes.json exclude lever is correct in principle but NOT a clean mechanica"), "SH52-076": ("judgment", "The guard for SH52-072 (assert /markets serves the static hub) is paired with the 072 fi"), "SH52-077": ("build", "The brand.css wildcard-!important foot-gun is real (4 incidents), but the requested fix "), "SH52-080": ("diagnose", "Memory regression (avg 2.7GB, peak 4.9GB vs the 1.9GB GC design threshold) is a runtime-"), "SH52-081": ("diagnose", "Recurring 500s on GET /api/v1/transactions and /api/v1/deals (plus marketing/auto-genera"), "SH52-082": ("judgment", "Chronic primary-pool saturation (95% bursts from crawler reads) — the fix is to move spe"), "SH52-084": ("judgment", "The mechanical worker-lever did NOT close the leak. I shipped the correct hardening (add"), "SH52-085": ("judgment", "Retiring the forgeable-Referer _MAP_BYPASS_PATHS block is an AUTH/access-control decisio"), "SH52-086": ("build", "News future-dated rows -> trust surface 'degraded' spans ingestion behavior (a write-tim"), "SH52-088": ("diagnose", "Adding retry-on-502 in dchub-mcp-server/server.mjs is a control-flow change with an idem"), "SH52-089": ("build", "The substantive asks — 'token rotation + read-URL repoint unverified' — require comparin"), "SH52-091": ("build", "Live /api/v1/ai/reach/trend still publishes impossible numbers (verified live: 13 of 16 "), "SH52-092": ("build", "Verified live: /markets/northern-virginia has 7 hrefs total and ZERO links to facilities"), "SH52-093": ("commercial", "This is an owner/BD decision gated on external data, not a code fix. The robots.txt 'Dis"), "SH52-094": ("build", "0 of 5 money-query SERPs show dchub.cloud — this is a ranking/BD outcome, not a code def"), "SH52-095": ("owner-flag", "The claude 'AI crawler' counter being ~93% self-instructed metadata is real, but the fix"), "SH52-096": ("build", "~10k international facility pages remain thin (99-107 unique words) because the RAG mark"), "SH52-097": ("judgment", "The primary press artifact of this class (the funnel card publishing '35 agents · WoW -6"), "SH52-100": ("build", "/answers content hub is frozen (verified live: /answers/sitemap.xml every lastmod=2026-0"), "SH52-102": ("commercial", "Paid/free caps unenforced end-to-end on /mcp — tier/quota ENFORCEMENT, explicitly human-"), "SH52-103": ("commercial", "Legacy $199 Pro link (eVq5kE4oOfs13mleGuaZi0h) still on all 4 backend carriers (mcp_gate"), "SH52-104": ("commercial", "Two halves, both non-mechanical. (a) LIVE /.well-known/mcp.json is CF-worker-served (x-d"), "SH52-105": ("commercial", "Agent rail has never produced a sale; the proposed fix is enforcing key persistence at m"), "SH52-106": ("judgment", "Pay-offer reachability ~0 — the prewall offer window is consumed by our own dchub-intern"), "SH52-107": ("commercial", "No enforced differentiation in the paid ladder ($9 starter ≈ $49 developer via r-starter"), "SH52-109": ("commercial", "WELCOME_CTA_TIER='pro' → 'founding' is a commercial decision (which plan lifecycle drip "), "SH52-110": ("build", "1,556-row paid-intent lead ledger is captured but unworked. The fix is building new cons"), "SH52-111": ("commercial", "Upgrade-offer A/B rig sits killed (kill_switch=true, arm B starved at 1 impression). Re-"), "SH52-112": ("commercial", "NLR Year-2 ($10K clause) re-engagement — explicitly a BD action, not code. Value-first e"), "SH52-117": ("build", "Same root as SH52-007 (its second-tier twin): the newest liveness master-ticks' FAIL ver"), "SH52-118": ("build", "Concurrency/control-flow change I should not ship blind: the land-power crawl has two dr"), "SH52-119": ("build", "Ingestion-behavior + strategy decision, not mechanical: /api/land-power/status is perman"), "SH52-121": ("judgment", "The acute part of this finding is already neutralized (the heroic-reprieve zombie that l"), "SH52-125": ("judgment", "llms.txt still self-contradicts live (L28/156/256 '15,700+' vs L15/276 '16,900+'; L179/2"), "SH52-129": ("commercial", "Monetization enforcement dark: MONTHLY_QUOTA_ENFORCE + metered pay-per-call billing buil"), "SH52-131": ("build", "DC-load interconnection queue is null for all US ISOs except ERCOT. Fix is a new data-so"), "SH52-132": ("judgment", "DCPI local-granularity +8/+6 hard clips saturate in dense metros (allen/irving byte-iden"), "SH52-133": ("build", "Canon taxonomy over-claims two in_scope classes (PPA benchmarks, colo pricing/vacancy) w"), "SH52-134": ("commercial", "Rollout velocity turned inward (202 fix vs 68 feat/14d, ~1 new agent-facing capability; "), "SH52-135": ("build", "Intl grid expansion stalled since 07-11 (India/Mexico/Singapore-official researched-not-"), "SH52-136": ("owner-flag", "RFC 8414 AS-metadata 404s on both hosts - but this is BY DESIGN: the frontend _worker.js"), "SH52-137": ("judgment", "L15 calibration harness guards only site_valuation_engine. The registry's expected range")}

_CHECK_CLOSES = {
    "z_isozones": ["SH52-130"], "z_assetcanon": ["SH52-060"],
    "z_dcpiforecast": ["SH52-138"], "z_standing": ["SH52-033"],
    "z_a2a_oauth": ["SH52-015"], "z_ratelimit": ["SH52-126"],
    "z_intkey": ["SH52-127"], "z_azure": ["SH52-128"],
    "z_deadsched": ["SH52-057"], "z_map_floor": ["SH52-074"],
    "z_usmap_floor": ["SH52-099"],
    "a_loopctl": ["SH52-001"],
    "a_stamps": ["SH52-115"],
    "b_landpower": ["SH52-050"],
    "c_legacy199": ["SH52-103"],
    "c_drip": ["SH52-109"],
    "d_tier": ["SH52-019", "SH52-039", "SH52-124"],
    "d_tease": ["SH52-020"],
    "d_aiquery": ["SH52-123"],
    "e_llms": ["SH52-027", "SH52-125", "SH52-073"],
    "e_full": ["SH52-028"],
    "e_version": ["SH52-031"],
    "e_369gw": ["SH52-032"],
    "e_agent": ["SH52-029"],
    "f_markets": ["SH52-072", "SH52-076"],
    "f_robots": ["SH52-098"],
    "f_reveal": ["SH52-084"],
    "g_press": ["SH52-062"],
    "g_neso": ["SH52-061"],
    "g_verify": ["SH52-063"],
    "h_osm": ["SH52-002"],
    "h_geninv": ["SH52-003", "SH52-055"],
    "h_plants": ["SH52-052"],
    "i_proposals": ["SH52-040"],
    "i_scout": ["SH52-042"],
    # j_beats co-closes SH52-001 with a_loopctl: the finding closes only when
    # the feed beats on the board AND the scan proves a scheduler drives it —
    # a multi-check finding closes only when ALL its checks agree.
    "j_beats": ["SH52-001"],
}


def _acked() -> set:
    return {t.strip() for t in
            (os.environ.get("AUDIT_CLOSURE_ACK") or "").split(",") if t.strip()}


def _registry_status(lanes) -> dict:
    """Fold lane check verdicts over the registry → per-finding status +
    closure arithmetic. AUTO items inherit their check; acked items close
    deliberately; the rest are OPEN (which is honest, not red noise)."""
    verdict_by_check = {}
    for ln in lanes:
        for k in ln["checks"]:
            verdict_by_check[k["id"]] = k["pass"]
    status = {}
    for cid, fids in _CHECK_CLOSES.items():
        v = verdict_by_check.get(cid)
        for fid in fids:
            s = "CLOSED" if v is True else ("OPEN-RED" if v is False else "?")
            prev = status.get(fid)
            # A finding closed by several checks closes only when ALL agree.
            rank = {"OPEN-RED": 2, "?": 1, "CLOSED": 0}
            if prev is None or rank[s] > rank[prev]:
                status[fid] = s
    acked = _acked()
    rows = []
    closed = 0
    deferred = 0
    ignored_acks = []
    overrode_defer = []
    for fid, dom, sev, eff, title in REGISTRY:
        st = status.get(fid, "OPEN")
        # Precedence (all honest): a checker's OPEN-RED or CLOSED always wins;
        # then env-ack / evidence-ack (a merged fix that can't be probed here);
        # then the deferred ledger (owned, not broken); else OPEN.
        if st not in ("CLOSED", "OPEN-RED"):
            if fid in acked or fid in _EVIDENCE_ACKED:
                st = "ACKED"
            elif fid in DEFERRED:
                st = "DEFERRED"
        elif st == "OPEN-RED":
            # ★An ack/defer never outranks a live FAILING checker.
            if fid in acked:
                ignored_acks.append(fid)
            if fid in DEFERRED:
                overrode_defer.append(fid)
        if st in ("CLOSED", "ACKED"):
            closed += 1
        elif st == "DEFERRED":
            deferred += 1
        rows.append({"id": fid, "domain": dom, "sev": sev, "effort": eff,
                     "status": st, "title": title,
                     "owner": (DEFERRED.get(fid) or ("", ""))[0]
                     if st == "DEFERRED" else None})
    resolved = closed
    return {"total": len(REGISTRY),
            "closed": resolved, "deferred": deferred,
            "open": len(REGISTRY) - resolved - deferred,
            "closure_pct": round(100.0 * resolved / len(REGISTRY), 1),
            "resolved_or_owned_pct": round(
                100.0 * (resolved + deferred) / len(REGISTRY), 1),
            "overrode_defer": overrode_defer,
            "acked": sorted(acked & {r["id"] for r in rows}),
            "acks_ignored_while_red": ignored_acks,
            "findings": rows}


def _annotation_lifecycle_checks(status_by_id, known=None, resolved=None):
    """Class fix for annotations that outlive their own repair (2026-08-14).

    /whats-new's known_issue notes are hand-written prose in
    routes/infra_growth.py, each citing a finding id. CI already refuses a
    DANGLING citation; nothing refused a DEAD one — an annotation whose
    finding this registry records as fixed. That shipped three times in one
    month: SH52-054's "structurally capped" note over a fixed cap, SH52-056's
    "pinned vintage" over a completed backfill, SH52-051's "failing" note
    over a fixed gate. This check FAILS the board the moment it recurs, and
    its mirror keeps the credit lines honest: a _RESOLVED entry must not sit
    on top of a checker that is demonstrably red.

    Pure given its inputs (status_by_id: finding id -> status string from
    _registry_status; known/resolved: the infra_growth dicts) so the unit
    test drives it with fake statuses and PROVES it can fail. The import
    only runs in production, where flask/psycopg2 exist.
    """
    if known is None or resolved is None:
        try:
            from routes import infra_growth as _ig
            if known is None:
                known = getattr(_ig, "_KNOWN_ISSUE", None)
            if resolved is None:
                resolved = getattr(_ig, "_RESOLVED", None)
        except Exception as e:  # noqa: BLE001
            # ? not PASS: an unreadable annotation source is UNMEASURED.
            return [{"id": "l_annot_source", "name": "annotation source readable",
                     "pass": None, "critical": True,
                     "detail": "routes.infra_growth unimportable: %s: %s"
                               % (type(e).__name__, str(e)[:120])}]
    checks = []
    stale = [(lbl, ref) for lbl, (ref, _n) in sorted((known or {}).items())
             if status_by_id.get(ref) in ("CLOSED", "ACKED")]
    checks.append({
        "id": "l_annot_not_stale",
        "name": "no known_issue cites a finding recorded as fixed",
        "pass": not stale, "critical": True,
        "detail": ("every cited finding is still open"
                   if not stale else
                   "STALE annotation(s) — the warning outlived its fix: "
                   + "; ".join("%s cites %s (registry: %s)"
                               % (l, r, status_by_id.get(r))
                               for l, r in stale)
                   + " — retire it to _RESOLVED in routes/infra_growth.py")})
    red = [(lbl, ref) for lbl, (ref, _on, _n) in sorted((resolved or {}).items())
           if status_by_id.get(ref) == "OPEN-RED"]
    checks.append({
        "id": "l_annot_credit_honest",
        "name": "no resolved credit line over a failing checker",
        "pass": not red, "critical": False,
        "detail": ("no credit line contradicts a live checker"
                   if not red else
                   "credit claimed while the checker FAILS: "
                   + "; ".join("%s credits %s as resolved (registry: OPEN-RED)"
                               % (l, r) for l, r in red))})
    return checks


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    try:
        body = json.dumps({
            "feed": "audit-closure-shell-daily",
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
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint)
        _rq.post(_local("/api/v1/admin/ingest-runs/beat"),
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": _UA, "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[audit-closure] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn) -> list[dict]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s"
                       % (type(e).__name__, str(e)[:120]), critical=True)]


def _run_tick(beat: bool = False) -> dict:
    """beat=True only on the scheduled POST path. ★A dashboard view must not
    stamp the daily beat — beat-on-view makes a dead cron indistinguishable
    from a watched board (review #36; the osm-crawl masking class)."""
    _http_memo_clear()
    lanes = [
        {"id": "p0_incidents", "name": "A · P0 incident fallout",
         "checks": _safe_lane(_lane_p0_incidents)},
        {"id": "secrets", "name": "B · secrets hygiene",
         "checks": _safe_lane(_lane_secrets)},
        {"id": "revenue_wall", "name": "C · revenue wall",
         "checks": _safe_lane(_lane_revenue_wall)},
        {"id": "first_call", "name": "D · first-call honesty",
         "checks": _safe_lane(_lane_first_call)},
        {"id": "surfaces", "name": "E · text surfaces & canon",
         "checks": _safe_lane(_lane_surfaces)},
        {"id": "frontend_seo", "name": "F · frontend & SEO",
         "checks": _safe_lane(_lane_frontend_seo)},
        {"id": "media", "name": "G · media integrity",
         "checks": _safe_lane(_lane_media)},
        {"id": "inventory", "name": "H · inventory liveness",
         "checks": _safe_lane(_lane_inventory)},
        {"id": "brain", "name": "I · brain actuation",
         "checks": _safe_lane(_lane_brain)},
        {"id": "class_guard", "name": "J · registered≠scheduled class",
         "checks": _safe_lane(_lane_class_guard)},
        {"id": "closeout", "name": "K · grind closeout (regression watch)",
         "checks": _safe_lane(_lane_closeout)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    reg = _registry_status(lanes)
    # Lane L runs AFTER the registry fold because its input IS the fold: it
    # cross-references the hand-written /whats-new annotations against each
    # cited finding's computed status. Appending here cannot perturb reg —
    # _registry_status only consumes check ids present in _CHECK_CLOSES, and
    # lane L's ids are deliberately absent from it.
    _status_by_id = {r["id"]: r["status"] for r in reg["findings"]}
    _annot_lane = {"id": "annotations", "name": "L · annotation lifecycle",
                   "checks": _safe_lane(
                       lambda: _annotation_lifecycle_checks(_status_by_id))}
    _annot_lane["verdict"] = _lane_verdict(_annot_lane["checks"])
    lanes.append(_annot_lane)
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "audit-closure-52",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "registry": reg,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
        "note": "138 findings from the 2026-08-07 audit. Red lanes are WORK, "
                "not noise — several checks fail by design until the owner "
                "acts (quota flag, detector scout, drip CTA). OPEN findings "
                "have no checker yet; close them with a checker, not an ack, "
                "wherever one is possible.",
    }
    if beat:
        _beat_ledger("closure %s/%s (%.1f%%) · %s"
                     % (reg["closed"], reg["total"], reg["closure_pct"],
                        summary), failing=out["any_fail"])
    return out


def _no_store(resp):
    # ★CF caches admin GETs (30-min stale-board trap) — always no-store.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@audit_closure_master_shell_bp.route(
    "/api/v1/admin/audit-closure/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return _no_store(jsonify(
            ok=False, error="AUDIT_CLOSURE_SHELL_DISABLE=1")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(_run_tick(beat=(request.method == "POST"))))


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@audit_closure_master_shell_bp.route("/admin/audit-closure", methods=["GET"])
@audit_closure_master_shell_bp.route("/api/v1/admin/audit-closure",
                                     methods=["GET"])
def dashboard():
    from flask import make_response
    if _disabled():
        return _no_store(make_response(
            "<h1>Audit Closure</h1><p>AUDIT_CLOSURE_SHELL_DISABLE=1</p>", 404))
    if not _admin_ok():
        return _no_store(make_response(
            "<h1>401</h1><p>admin key required</p>", 401))
    t = _run_tick()
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    scolor = {"CLOSED": "#22c55e", "ACKED": "#3b82f6",
              "OPEN-RED": "#ef4444", "?": "#eab308", "OPEN": "#94a3b8"}
    lane_rows = []
    for ln in t["lanes"]:
        lane_rows.append(
            "<tr><td><b>%s</b></td><td style='color:%s'><b>%s</b></td>"
            "<td>%s</td></tr>"
            % (_esc(ln["name"]), color.get(ln["verdict"], "#eab308"),
               _esc(ln["verdict"]),
               "<br>".join("%s <i>%s</i> — %s"
                           % ({True: "✓", False: "✗"}.get(k["pass"], "?"),
                              _esc(k["name"]), _esc(k["detail"]))
                           for k in ln["checks"])))
    reg = t["registry"]
    reg_rows = []
    for r in reg["findings"]:
        reg_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s/%s</td>"
            "<td style='color:%s'><b>%s</b></td><td>%s</td></tr>"
            % (r["id"], _esc(r["domain"]), r["sev"], r["effort"],
               scolor.get(r["status"], "#94a3b8"), _esc(r["status"]),
               _esc(r["title"])))
    html = (
        "<html><head><title>Audit Closure #52</title>"
        "<meta http-equiv='refresh' content='300'></head>"
        "<body style='font-family:system-ui;max-width:1250px;margin:24px auto'>"
        "<h1>Audit Closure <small>#52 · 2026-08-07 audit</small></h1>"
        "<h2>%s / %s closed (%.1f%%)</h2><p>%s</p><p><small>%s</small></p>"
        "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
        "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
        "<th align='left'>checks</th></tr>%s</table>"
        "<h2>Registry — all 138 findings</h2>"
        "<table cellpadding='4' style='border-collapse:collapse;width:100%%;"
        "font-size:13px'>"
        "<tr><th align='left'>id</th><th align='left'>domain</th>"
        "<th align='left'>sev/eff</th><th align='left'>status</th>"
        "<th align='left'>finding</th></tr>%s</table>"
        "<p><small>refreshes 300s · kill AUDIT_CLOSURE_SHELL_DISABLE=1 · "
        "probes AUDIT_CLOSURE_SHELL_PROBE=0 · deliberate manual closure via "
        "AUDIT_CLOSURE_ACK=id,id</small></p></body></html>"
        % (reg["closed"], reg["total"], reg["closure_pct"],
           _esc(t["generated_at"]), _esc(t["note"]),
           "".join(lane_rows), "".join(reg_rows)))
    return _no_store(make_response(html, 200))
