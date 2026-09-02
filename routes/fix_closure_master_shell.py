"""routes/fix_closure_master_shell.py — Fix Closure Master Shell (#33, 2026-07-26).

WHY THIS EXISTS
===============
The 07-25/26 waves closed a set of long-open defects, and each one had the
same biography: broken silently, discovered by measurement, fixed, and — per
the 40% recidivism number — statistically likely to un-fix itself. This shell
pins every fix in the set to a live check so a regression reads RED the day
it happens instead of the month someone rediscovers it:

  · the eia_retail_rates mirror (broken since 2026-05-14, protected only by
    an accidental exception; now a bounded vocabulary-projecting refresh
    with an explicit rollback guard — and 2021-2024 history is irreplaceable)
  · the /mcp envelope tool count (fallback list sat 7 behind the registry)
  · the media snapshot append (8 identical rows/day from the cron window)
  · the LinkedIn follower collector (one-word enum blindness)
  · the paid-key contract (a dead CI key masqueraded as a paywall bug — but
    a PAID key being served a preview is a real revenue defect, so the
    contract itself gets a daily probe with the QA canary)
  · minted-key cohort retention — the growth constraint (0% ≥7d return on
    recent cohorts when this shipped) — measured on a board, daily.

HOUSE RULES
  · A lane never reads PASS when it could not check — unreachable is '?'.
  · Read-only except its own dead-man beat. The canary paywall probe is one
    bounded loopback tool call per tick.
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s.

Surface:  GET /admin/fix-closure                    (HTML)
          GET /api/v1/admin/fix-closure             (HTML)
          GET|POST /api/v1/admin/fix-closure/master-tick   (JSON)
Beat:     fix-closure-shell-daily
Kill:     FIX_CLOSURE_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

fix_closure_master_shell_bp = Blueprint("fix_closure_master_shell", __name__)

ORIGIN = (os.environ.get("SURFACE_TRUTH_ORIGIN") or "https://dchub.cloud").rstrip("/")
_UA = "dchub-fix-closure/1.0 (+https://dchub.cloud; internal-audit)"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("FIX_CLOSURE_SHELL_DISABLE") or "").strip() == "1"


# ── helpers (contract shared with shells #30-#32) ─────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in crits):
        return "?"
    return "PASS"


def _db():
    try:
        from routes.brain_rag import _db as _rag_db
        return _rag_db()
    except Exception as e:  # noqa: BLE001
        logger.debug("[fix-closure] db unavailable: %s", e)
        return None


def _read_repo(rel: str):
    try:
        with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8",
                  errors="ignore") as fh:
            return fh.read()
    except Exception:
        return None


def _fetch(path: str):
    """Edge GET with a cache-buster (zone caches admin/manifest GETs 3600s).
    requests, not urllib (regression_lint)."""
    try:
        import time as _t
        import requests as _rq
        sep = "&" if "?" in path else "?"
        r = _rq.get(ORIGIN + path + sep + "cb=%d" % _t.time(),
                    headers={"User-Agent": _UA}, timeout=12)
        if r.status_code >= 400:
            return None, "HTTP %d" % r.status_code
        return r.text, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


# ── lane 1 · eia mirror ───────────────────────────────────────────────

def _lane_eia_mirror() -> list[dict]:
    out: list[dict] = []
    c = _db()
    if c is None:
        return [_check("em_db", "mirror readable", None, "no DB connection",
                       critical=True)]
    try:
        with c.cursor() as cur:
            yr = str(datetime.datetime.now(datetime.timezone.utc).year)
            cur.execute(
                "SELECT count(*) FILTER (WHERE period = %s), "
                "count(*) FILTER (WHERE period < '2025'), "
                "max(updated_at), "
                "count(*) FILTER (WHERE sector NOT IN "
                "  ('commercial', 'industrial')) "
                "FROM eia_retail_rates", (yr,))
            cur_yr, hist, latest, badvocab = cur.fetchone()
            cur_yr, hist, badvocab = int(cur_yr), int(hist), int(badvocab)
            out.append(_check(
                "em_fresh", "mirror carries the current year", cur_yr > 0,
                "%d rows for %s" % (cur_yr, yr) if cur_yr else
                "NO %s rows — the projecting sync is not landing" % yr,
                critical=True))
            out.append(_check(
                "em_history", "2021-2024 history intact (irreplaceable)",
                hist >= 300,
                "%d pre-2025 rows" % hist if hist >= 300 else
                "ONLY %d pre-2025 rows — history has been damaged; the "
                "source cannot regenerate it" % hist, critical=True))
            out.append(_check(
                "em_vocab", "mirror vocabulary intact (word sectors)",
                badvocab == 0,
                "clean" if badvocab == 0 else
                "%d rows with non-word sectors — a naive copy ran" % badvocab,
                critical=True))
            if latest is not None:
                # ★live: updated_at is timestamp WITHOUT time zone — coerce
                # naive→UTC or the subtraction raises (first tick rendered
                # this check '?' on exactly that TypeError).
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=datetime.timezone.utc)
                age_d = ((datetime.datetime.now(datetime.timezone.utc)
                          - latest).total_seconds() / 86400.0)
                out.append(_check(
                    "em_age", "mirror refreshed within 8d",
                    age_d <= 8, "last refresh %.1fd ago" % age_d,
                    critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("em_db", "mirror readable", None,
                          "query failed: %s" % str(e)[:110], critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 2 · paid-key contract ────────────────────────────────────────

def _lane_paywall_contract() -> list[dict]:
    key = (os.environ.get("PAYWALL_CANARY_KEY") or "").strip()
    if not key:
        return [_check("pc_key", "canary key available", None,
                       "PAYWALL_CANARY_KEY unset", critical=True)]
    try:
        import requests as _rq
        port = os.environ.get("PORT", "8080")
        r = _rq.post(
            "http://127.0.0.1:%s/mcp" % port,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "analyze_site",
                             "arguments": {"lat": 33.45, "lon": -112.07}}},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "X-API-Key": key, "User-Agent": _UA},
            timeout=45)
        body = r.text
        for line in body.split("\n"):
            if line.startswith("data:"):
                body = line[5:]
                break
        d = json.loads(body)
        txt = ((d.get("result") or {}).get("content") or [{}])[0].get("text", "")
        payload = json.loads(txt) if txt.strip().startswith("{") else {}
        locked = bool(payload.get("locked")) or payload.get("preview")
        return [_check(
            "pc_contract", "paid canary key receives full data", not locked,
            "full data" if not locked else
            "PAID key served a preview/lock envelope — the post-pay leak "
            "class is live again (tier=%s preview=%s)"
            % ((payload.get("quota") or {}).get("tier"),
               payload.get("preview")), critical=True)]
    except Exception as e:  # noqa: BLE001
        return [_check("pc_contract", "paid canary key receives full data",
                       None, "probe failed: %s: %s"
                       % (type(e).__name__, str(e)[:100]), critical=True)]


# ── lane 3 · zone envelope ────────────────────────────────────────────

def _lane_zone_envelope() -> list[dict]:
    out: list[dict] = []
    try:
        from ai_surface_canon import PINNED
        want = int(PINNED.get("tools_advertised") or 0)
    except Exception as e:  # noqa: BLE001
        return [_check("ze_canon", "canon available", None,
                       "canon import failed: %s" % str(e)[:90],
                       critical=True)]
    body, err = _fetch("/mcp")
    if body is None:
        out.append(_check("ze_live", "/mcp envelope matches canon", None,
                          "unreachable: %s" % err, critical=True))
    else:
        try:
            env_tools = json.loads(body).get("tools")
        except Exception:
            env_tools = None
        out.append(_check(
            "ze_live", "/mcp envelope matches canon",
            env_tools == want if isinstance(env_tools, int) else None,
            "envelope=%s canon=%d" % (env_tools, want), critical=True))
    src = _read_repo("worker.js") or ""
    m = re.search(r"const MCP_FALLBACK_TOOLS = \[\n(.*?)\n\];", src, re.S)
    n = len(re.findall(r'\{ name: "', m.group(1))) if m else None
    out.append(_check(
        "ze_repo", "repo fallback list matches canon",
        (n == want) if n is not None else None,
        "repo fallback entries=%s canon=%d" % (n, want), critical=True))
    return out


# ── lane 4 · media hygiene ────────────────────────────────────────────

def _lane_media_hygiene() -> list[dict]:
    out: list[dict] = []
    c = _db()
    if c is None:
        return [_check("mh_db", "media stores readable", None,
                       "no DB connection", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM media_growth_snapshots "
                        "WHERE created_at::date = CURRENT_DATE")
            n_today = int(cur.fetchone()[0])
            out.append(_check(
                "mh_dedup", "one snapshot row per day", n_today <= 1,
                "%d row(s) today" % n_today + (
                    "" if n_today <= 1 else
                    " — the cron-window dedup guard is not holding"),
                critical=True))
            cur.execute("""
                SELECT DISTINCT ON (platform) platform, snap_date, followers,
                       reason
                  FROM social_audience WHERE platform IN ('linkedin', 'x')
                 ORDER BY platform, snap_date DESC""")
            rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        today = datetime.datetime.now(datetime.timezone.utc).date()
        li = rows.get("linkedin")
        if li is None:
            out.append(_check("mh_li", "LinkedIn followers landing", None,
                              "no linkedin rows", critical=True))
        else:
            d, f, reason = li
            fresh = (today - d).days <= 2
            out.append(_check(
                "mh_li", "LinkedIn followers landing",
                bool(fresh and f is not None),
                "li=%s (%s)" % (f, d.isoformat()) if f is not None else
                "NULL (%s, reason=%s) — enum fix deployed 07-26; red past "
                "the next media tick means the collector broke differently"
                % (d.isoformat(), reason), critical=True))
    except Exception as e:  # noqa: BLE001
        out.append(_check("mh_db", "media stores readable", None,
                          "query failed: %s" % str(e)[:100], critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 5 · retention instrument ─────────────────────────────────────

def _lane_retention() -> list[dict]:
    c = _db()
    if c is None:
        return [_check("rt_db", "retention computable", None,
                       "no DB connection", critical=True)]
    try:
        with c.cursor() as cur:
            # ★api_keys.created_at/last_used_at are TEXT on live — cast, and
            # judge only cohorts old enough to have had 7 days to return.
            # Scope note: this is the REST api_keys system; trial/partner
            # keys live in a second system — this instrument tracks the
            # durable-key funnel specifically.
            cur.execute("""
                SELECT date_trunc('week',
                           NULLIF(created_at::text, '')::timestamptz)::date AS wk,
                       count(*),
                       count(*) FILTER (
                           WHERE NULLIF(last_used_at::text, '')::timestamptz
                                 >= NULLIF(created_at::text, '')::timestamptz
                                    + interval '7 days')
                  FROM api_keys
                 WHERE NULLIF(created_at::text, '')::timestamptz
                       BETWEEN now() - interval '6 weeks'
                           AND now() - interval '7 days'
                 GROUP BY 1 ORDER BY 1""")
            rows = cur.fetchall() or []
        if not rows:
            return [_check("rt_cohorts", "minted-key ≥7d return rate", None,
                           "no mature cohorts in window", critical=True)]
        parts, tot_m, tot_r = [], 0, 0
        for wk, minted, ret in rows:
            tot_m += int(minted)
            tot_r += int(ret)
            parts.append("%s: %d/%d" % (wk.isoformat(), int(ret),
                                        int(minted)))
        pct = (100.0 * tot_r / tot_m) if tot_m else 0.0
        return [_check(
            "rt_cohorts", "minted-key ≥7d return rate", True,
            "%.0f%% over %d keys (%s) — THE growth constraint; raising "
            "this beats any top-of-funnel push"
            % (pct, tot_m, " · ".join(parts)), critical=True)]
    except Exception as e:  # noqa: BLE001
        return [_check("rt_cohorts", "minted-key ≥7d return rate", None,
                       "query failed: %s" % str(e)[:110], critical=True)]
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── lane 6 · eia sync source ──────────────────────────────────────────

def _lane_sync_source() -> list[dict]:
    """The projecting sync exists in TWO duplicated job blocks in main.py —
    a naive rewrite of either one re-arms the original landmine. Pin both."""
    src = _read_repo("main.py") or ""
    if not src:
        return [_check("ss_src", "main.py readable", None, "unreadable",
                       critical=True)]
    # Contiguous source fragment — the full sentence spans two adjacent
    # string literals (fragment-blindness: grep source for what is actually
    # in the source, not the runtime concatenation).
    guards = src.count("back to preserve the mirror")
    naive = ("SELECT state, sector, price_cents_kwh, period, retrieved_at"
             in src)
    return [
        _check("ss_guards", "both sync blocks carry the rollback guard",
               guards >= 2, "%d guard(s) present" % guards, critical=True),
        _check("ss_naive", "naive vocabulary-blind copy absent", not naive,
               "clean" if not naive else
               "the un-projected INSERT is back — that copy empties every "
               "word-form consumer", critical=True),
    ]


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    try:
        body = json.dumps({
            "feed": "fix-closure-shell-daily",
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
        import requests as _rq   # not urllib (regression_lint)
        _rq.post("http://127.0.0.1:" + str(port)
                 + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-fix-closure-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[fix-closure] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn) -> list[dict]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s"
                       % (type(e).__name__, str(e)[:120]), critical=True)]


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    lanes = [
        {"id": "eia_mirror", "name": "1 · eia retail mirror",
         "checks": _safe_lane(_lane_eia_mirror)},
        {"id": "paywall", "name": "2 · paid-key contract",
         "checks": _safe_lane(_lane_paywall_contract)},
        {"id": "zone_envelope", "name": "3 · zone envelope 79",
         "checks": _safe_lane(_lane_zone_envelope)},
        {"id": "media_hygiene", "name": "4 · media hygiene",
         "checks": _safe_lane(_lane_media_hygiene)},
        {"id": "retention", "name": "5 · key retention (north star)",
         "checks": _safe_lane(_lane_retention)},
        {"id": "sync_source", "name": "6 · sync source integrity",
         "checks": _safe_lane(_lane_sync_source)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "fix-closure-33",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


def _no_store(resp):
    # ★CF caches admin GETs (30-min stale-board trap) — always no-store.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@fix_closure_master_shell_bp.route("/api/v1/admin/fix-closure/master-tick",
                                   methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return _no_store(jsonify(
            ok=False, error="FIX_CLOSURE_SHELL_DISABLE=1")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(_run_tick(beat=(request.method == "POST"))))


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@fix_closure_master_shell_bp.route("/admin/fix-closure", methods=["GET"])
@fix_closure_master_shell_bp.route("/api/v1/admin/fix-closure",
                                   methods=["GET"])
def dashboard():
    from flask import make_response
    if _disabled():
        return _no_store(make_response(
            "<h1>Fix Closure</h1><p>FIX_CLOSURE_SHELL_DISABLE=1</p>", 404))
    if not _admin_ok():
        return _no_store(make_response(
            "<h1>401</h1><p>admin key required</p>", 401))
    t = _run_tick(beat=False)
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in t["lanes"]:
        rows.append("<tr><td><b>%s</b></td><td style='color:%s'><b>%s</b></td>"
                    "<td>%s</td></tr>"
                    % (_esc(ln["name"]), color.get(ln["verdict"], "#eab308"),
                       _esc(ln["verdict"]),
                       "<br>".join(
                           "%s <i>%s</i> — %s"
                           % ({True: "✓", False: "✗"}.get(k["pass"], "?"),
                              _esc(k["name"]), _esc(k["detail"]))
                           for k in ln["checks"])))
    html = ("<html><head><title>Fix Closure #33</title>"
            "<meta http-equiv='refresh' content='120'></head>"
            "<body style='font-family:system-ui;max-width:1150px;margin:24px auto'>"
            "<h1>Fix Closure <small>#33</small></h1>"
            "<p>%s</p>"
            "<p><small>Every fix from the 07-25/26 waves, pinned to a live "
            "check. A lane never reads PASS when it could not check.</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
            "<th align='left'>checks</th></tr>%s</table>"
            "<p><small>refreshes 120s · kill FIX_CLOSURE_SHELL_DISABLE=1"
            "</small></p></body></html>"
            % (_esc(t["generated_at"]), "".join(rows)))
    return _no_store(make_response(html, 200))
