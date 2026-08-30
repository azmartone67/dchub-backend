"""
routes/growthfix_master_shell.py — Growth-Loop Fix Master Shell (#26, 2026-07-24).

Born from the 2026-07-24 white-glove loop audit + fix-wave. Five findings, one
shell that keeps them fixed:

  1. MEDIA CARD — the reserved 16:00 "What We Shipped" capability slot claimed
     then editorially SUPPRESSED every day (the novelty gates judged evergreen
     cards "not novel"), so the card machine never fired. Fixed in
     media_editorial (reserved_slot_bypass). This lane watches that the card
     actually POSTS on its rotation, and that the desk would post one today.
  2. COMPETITOR GAP — the crawler read the SAME first 500 sitemap URLs every
     run: 15 consecutive zero-row runs while "green". Fixed with a rotating
     window + affirmative no_new_data beats. This lane watches the ledger row
     and the newest coverage_gaps insert.
  3. INGEST BOARD — eia/osm/competitor burned red on the >=3-zero-row rule
     while healthy-idle. Fixed with the no_new_data status. This lane mirrors
     the /ops/deadman rules in-DB and expects ZERO overdue feeds.
  4. LINKEDIN TOKEN — the whole media feed goes dark if the token lapses
     (~60d expiry). The refresh cron self-sustains only with a real
     refresh_token. This lane watches days-to-expiry + refresh-token presence.
  5. GOVERNANCE — the audit found send/merge arm-flags had drifted from what
     the operator believed (ACTIVATION_NUDGE_ARM newly set, automerge ARMED,
     CUSTOMER_WHITE_GLOVE_ACT absent). This lane is REPORT-ONLY: it renders
     the live arm posture so drift is visible, and flips NOTHING (L8).

★ HONESTY RULE (inherited from Integrity #25): a lane must never read PASS
when it couldn't check — an indeterminate critical check renders "?" and the
lane is not green.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.

Endpoints:
  GET/POST /api/v1/admin/growthfix/master-tick   JSON scoreboard (5 lanes)
  GET      /admin/growthfix                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/growthfix                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as the other master shells.
Cron: cron_heartbeat _DISPATCH `growthfix_shell_daily` (05:xx UTC) POSTs the
tick; on completion the tick beats the dead-man ledger (feed
`growthfix-shell-daily`, rows_inserted=1 liveness sentinel) so a silent stall
of the shell itself is detectable.
Kill: GROWTHFIX_SHELL_DISABLE=1
"""
from __future__ import annotations

import datetime
import logging
import os
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

growthfix_master_shell_bp = Blueprint("growthfix_master_shell", __name__)

# Statuses that mean "the loop is fine" on the ingest board — MUST mirror
# routes/ingest_runs._OK_STATUS (kept literal here so this shell never
# imports the route module).
_OK_STATUS = {"success", "ok", "idle", "no-op", "noop", "skipped", "",
              "no_new_data", "no-new-data"}
_NO_NEW_DATA = {"no_new_data", "no-new-data"}
_DEFAULT_CADENCE_H = 48.0

# The card must have posted within the LONGEST registry rotation (14d
# error_envelope) — one honest window, not per-key bookkeeping.
_CARD_MAX_QUIET_DAYS = 14


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("GROWTHFIX_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers (mirror integrity_master_shell) ────────────────────────

def _conn():
    """Raw psycopg2 connection. None on failure. Deliberately OUTSIDE the
    app pool — one short-lived connection per tick."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[growthfix] db connect failed: %s", e)
        return None


def _row(c, sql: str):
    """Fail-soft single row. None on error. LITERAL SQL only — no params
    tuple and no percent characters anywhere in the statement (psycopg2
    percent-substitution trap)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[growthfix] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _rows(c, sql: str):
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[growthfix] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, shown as '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """green only when something was actually decided and nothing failed.

    2026-07-25 (adversarial review): the critical-only escalation let a lane
    whose checks were ALL indeterminate-and-non-critical render a confident
    PASS — green-by-silence, the exact thing the honesty rule forbids.
    Mirrors routes/integrity_master_shell.py:161 (#25), the reference impl."""
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in checks if k.get("critical")):
        return "?"
    if not [k for k in checks if k["pass"] is not None]:
        return "?"
    return "PASS"


def _as_dt(ts):
    """Coerce a DB timestamp into an aware datetime. Some house tables store
    timestamps as TEXT (coverage_gaps.created_at — it 500'd this shell's very
    first live tick), so strings are parsed, not assumed. None on failure."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00").strip())
        except Exception:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _age_days(ts) -> float | None:
    ts = _as_dt(ts)
    if ts is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - ts).total_seconds() / 86400.0


# ── lane 1: media capability card ─────────────────────────────────────

def _lane_media_card(c) -> list[dict]:
    checks = []
    last = _row(c, "SELECT posted_at FROM linkedin_quad_posts "
                   "WHERE topic = 'capability' AND success IS TRUE "
                   "ORDER BY posted_at DESC LIMIT 1") if c else None
    age = _age_days(last[0]) if last else None
    if last is None and c is not None:
        checks.append(_check("card_posted", "capability card posted recently",
                             None, "no successful capability post recorded yet "
                             "(first fires at the next 16:00 UTC slot)"))
    else:
        ok = (age is not None and age <= _CARD_MAX_QUIET_DAYS)
        checks.append(_check(
            "card_posted", "capability card posted recently",
            ok if last else None,
            (f"last success {age:.1f}d ago" if age is not None
             else "never posted / db unreadable")))

    # Would the desk post a card RIGHT NOW? (exercises the
    # reserved_slot_bypass fix end-to-end, read-only — nothing publishes.)
    would = None
    detail = "editorial_decision unavailable"
    try:
        from routes.media_editorial import editorial_decision
        ed = editorial_decision("capability") or {}
        would = bool(ed.get("post"))
        detail = ((ed.get("lead") or {}).get("kind") or "no lead") + \
            (" [reserved_slot_bypass]" if ed.get("reserved_slot_bypass") else "")
        if not would:
            detail = "SUPPRESSED: " + str(ed.get("reason"))[:160]
    except Exception as e:  # noqa: BLE001
        detail = f"decision probe failed: {type(e).__name__}"
    # CRITICAL disjunction: healthy = posted recently OR would post today.
    posted_ok = (age is not None and age <= _CARD_MAX_QUIET_DAYS)
    combined = True if (posted_ok or would) else (None if would is None else False)
    checks.append(_check("card_would_post", "desk would post a card today",
                         combined, detail, critical=True))
    return checks


# ── lane 2: competitor gap crawl ──────────────────────────────────────

def _lane_competitor_gap(c) -> list[dict]:
    checks = []
    led = _row(c, "SELECT last_run, last_status, rows_inserted, note "
                  "FROM ingest_runs WHERE feed = 'competitor-gap-crawl'") if c else None
    if led is None:
        checks.append(_check("gap_ledger", "crawl beat on the ledger", None,
                             "ledger row unreadable", critical=True))
    else:
        lr, st, ri, note = led
        aged = _age_days(lr)
        ok = (st or "").lower() in _OK_STATUS and aged is not None and aged <= 2.0
        checks.append(_check(
            "gap_ledger", "crawl beat on the ledger", ok,
            f"status={st} rows={ri} age={aged:.1f}d note={str(note)[:100]}",
            critical=True))
    newest = _row(c, "SELECT MAX(created_at) FROM coverage_gaps") if c else None
    gage = _age_days(newest[0]) if newest and newest[0] else None
    # Gauge, not critical: with the rotating window a full sitemap sweep takes
    # days; a long-quiet gap board is a smell, not proof of breakage.
    checks.append(_check(
        "gap_newest", "coverage_gaps still accrues finds",
        (True if (gage is not None and gage <= 21) else None),
        (f"newest gap {gage:.1f}d ago" if gage is not None else "no rows / unreadable")))
    return checks


# ── lane 3: ingest board (in-DB deadman mirror) ───────────────────────

def _lane_ingest_board(c) -> list[dict]:
    rows = _rows(c, "SELECT feed, last_run, last_status, cadence_hours, "
                    "consecutive_zero, max_content_date FROM ingest_runs") if c else None
    if rows is None:
        return [_check("board", "all board feeds within cadence", None,
                       "ingest_runs unreadable", critical=True)]
    now = datetime.datetime.now(datetime.timezone.utc)
    reds = []
    for feed, lr, st, cad, cz, mcd in rows:
        cad_h = float(cad) if cad is not None else _DEFAULT_CADENCE_H
        stl = (st or "").lower()
        why = []
        lrz = _as_dt(lr)
        if lrz is None:
            why.append("never ran")
        elif (now - lrz).total_seconds() / 3600.0 > 2 * cad_h:
            why.append("stale")
        if stl not in _OK_STATUS:
            why.append("status=" + stl)
        if cz and cz >= 3 and stl not in _NO_NEW_DATA:
            why.append(str(cz) + " zero-row runs")
        mz = _as_dt(mcd)
        if mz is not None and mz > now + datetime.timedelta(hours=6):
            why.append("future content date")
        if why:
            reds.append(feed + "(" + ",".join(why) + ")")
    return [_check("board", "all board feeds within cadence",
                   len(reds) == 0,
                   (f"{len(rows)} feeds, 0 overdue" if not reds
                    else f"{len(reds)} overdue: " + "; ".join(reds[:6])),
                   critical=True)]


# ── lane 4: linkedin token ────────────────────────────────────────────

def _lane_linkedin_token(c) -> list[dict]:
    checks = []
    row = _row(c, "SELECT expires_at, COALESCE(refresh_token, '') <> '' "
                  "FROM linkedin_tokens ORDER BY id DESC LIMIT 1") if c else None
    if row is None:
        return [_check("li_expiry", "token not near expiry", None,
                       "linkedin_tokens unreadable", critical=True)]
    exp, has_refresh = row
    _a = _age_days(exp)
    days = (-_a) if _a is not None else None
    checks.append(_check(
        "li_expiry", "token not near expiry",
        (days is not None and days > 5.0) if exp is not None else None,
        (f"{days:.1f}d to expiry" if days is not None else "no expiry recorded"),
        critical=True))
    checks.append(_check(
        "li_refresh", "refresh_token present (cron can self-sustain)",
        bool(has_refresh),
        "present" if has_refresh else
        "ABSENT — the refresh cron can only ALERT; owner must re-seed via "
        "/api/linkedin/auth before expiry"))
    return checks


# ── lane 5: governance (report-only, flips nothing) ───────────────────

def _flag(name: str) -> str:
    v = os.environ.get(name)
    return "unset" if v is None else str(v)


def _lane_governance() -> list[dict]:
    """Arm-posture visibility. This lane is informational BY DESIGN: it can
    only PASS (the env read cannot fail) and never drives the shell red —
    its job is to stop the operator's mental model drifting from prod."""
    send = (f"ACTIVATION_NUDGE_ARM={_flag('ACTIVATION_NUDGE_ARM')} · "
            f"CUSTOMER_WHITE_GLOVE_ACT={_flag('CUSTOMER_WHITE_GLOVE_ACT')}")
    merge = (f"BRAIN_AUTOMERGE_ENABLED={_flag('BRAIN_AUTOMERGE_ENABLED')} · "
             f"BRAIN_AUTOMERGE_DRY_RUN={_flag('BRAIN_AUTOMERGE_DRY_RUN')} · "
             f"L22_AUTO_MERGE_ENABLE={_flag('L22_AUTO_MERGE_ENABLE')}")
    media = (f"TWITTER_PUBLISHER_ENABLED={_flag('TWITTER_PUBLISHER_ENABLED')} · "
             f"AUTOPOST_ENABLED={_flag('AUTOPOST_ENABLED')}")
    return [
        _check("gov_send", "customer-send arm posture", True, send),
        _check("gov_merge", "brain automerge posture", True, merge),
        _check("gov_media", "media publisher posture", True, media),
    ]


# ── dead-man beat (fail-open, mirrors competitor_gap_crawler) ─────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({
            "feed": "growthfix-shell-daily",
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
        req = urllib.request.Request(
            "http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-growthfix-shell/1.0",
                     "X-Admin-Key": admin_key})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[growthfix] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn, *a) -> list[dict]:
    """A lane that CRASHES must render '?' (indeterminate), never 500 the
    tick — the very first live tick died on coverage_gaps.created_at being
    TEXT, taking all five lanes down with it."""
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
            {"id": "media_card", "name": "1 · media capability card",
             "checks": _safe_lane(_lane_media_card, c)},
            {"id": "competitor_gap", "name": "2 · competitor gap crawl",
             "checks": _safe_lane(_lane_competitor_gap, c)},
            {"id": "ingest_board", "name": "3 · ingest dead-man board",
             "checks": _safe_lane(_lane_ingest_board, c)},
            {"id": "linkedin_token", "name": "4 · linkedin token durability",
             "checks": _safe_lane(_lane_linkedin_token, c)},
            {"id": "governance", "name": "5 · governance / arm posture",
             "checks": _safe_lane(_lane_governance)},
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
        "shell": "growthfix-26",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


@growthfix_master_shell_bp.route("/api/v1/admin/growthfix/master-tick",
                                 methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="GROWTHFIX_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_run_tick())


@growthfix_master_shell_bp.route("/admin/growthfix", methods=["GET"])
@growthfix_master_shell_bp.route("/api/v1/admin/growthfix", methods=["GET"])
def dashboard():
    if _disabled():
        return Response("growthfix shell disabled", status=404, mimetype="text/plain")
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
        "<title>Growth-Loop Fix Shell #26</title>"
        "<style>body{background:#0b1020;color:#e2e8f0;font:14px/1.5 "
        "-apple-system,Segoe UI,sans-serif;margin:2rem}table{border-collapse:"
        "collapse;width:100%;max-width:1100px}td{border-bottom:1px solid "
        "#1e293b;padding:.6rem .8rem;vertical-align:top}.lane{white-space:"
        "nowrap;font-weight:600}.d{color:#94a3b8}h1{font-size:1.2rem}"
        "small{color:#64748b}</style>"
        "<h1>Growth-Loop Fix Master Shell #26</h1>"
        "<small>generated " + _esc(d["generated_at"]) + " · read-only · "
        "refreshes 60s · kill GROWTHFIX_SHELL_DISABLE=1</small>"
        "<table>" + "".join(rows) + "</table>")
    return Response(html, mimetype="text/html")


def register_growthfix_master_shell(app):
    app.register_blueprint(growthfix_master_shell_bp)
