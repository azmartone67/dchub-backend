"""routes/agent_pay_master_shell.py — Agent-Pay Master Shell (#34, 2026-07-25).

WHY THIS EXISTS
===============
On 2026-07-25 the agent-pay watcher had been reporting a leading indicator —
`first_real_challenge_at = 2026-07-07` — for two and a half weeks. It was not
real. The MCP gateway collapses every harness/QA clientInfo into the single
platform tag `dchub-internal`, and the master-tick's synthetic filter did not
exclude it, so our own probes scored as customer demand. With the filter fixed
the milestone went NULL: **all-time, there has never been one real agent
pay-intent event.**

Two lessons are wired into this shell:

  1. The metric that tells you whether the business is working is exactly the
     metric most likely to flatter you. Lane 5 pins the synthetic filter itself
     so the day someone drops `%dchub%` it reads RED, not "record demand".
  2. "Nobody paid" had been read as a plumbing failure for weeks. It was not —
     the rail mints valid challenges in <1s. Lane 4 proves plumbing
     independently of demand so the two can never again be confused; lane 1
     then reads demand knowing the rail is sound.

Lanes 2 and 3 hold the two open decisions the fix surfaced: the pay offer only
appears AFTER an agent exhausts a generous auto-trial (so most agents never see
it), and MPP charges $0.50 for flagship tools the price list puts at $0.10,
because Stripe's fiat SPT floor is $0.50.

HOUSE RULES
  · A lane never reads PASS when it could not check — unreachable is '?'.
  · Read-only. One bounded live MCP probe per tick (kill: AGENT_PAY_SHELL_PROBE=0).
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s.
  · The synthetic-traffic predicate is IMPORTED from funnel_health, never
    re-declared — a copy would drift and re-open the exact bug this shell exists
    to catch.

Surface:  GET /admin/agent-pay-shell                             (HTML)
          GET /api/v1/admin/agent-pay-shell                      (HTML)
          GET|POST /api/v1/admin/agent-pay-shell/master-tick     (JSON)

          NOT /api/v1/admin/agent-pay/master-tick — that route belongs to the
          raw watcher in funnel_health.py, which the dchub-agent-pay-watcher
          scheduled task calls. Registering it twice would shadow the watcher.
Beat:     agent-pay-shell-daily
Kill:     AGENT_PAY_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

agent_pay_master_shell_bp = Blueprint("agent_pay_master_shell", __name__)

ORIGIN = (os.environ.get("SURFACE_TRUTH_ORIGIN") or "https://dchub.cloud").rstrip("/")
_UA = "dchub-agent-pay-shell/1.0 (+https://dchub.cloud; internal-audit)"

# Tools the live fiat rail covers (mirrors MPP_PRICE in the gateway's
# mpp-hook.mjs). The three flagships are where the paywall volume actually
# lands; the rest are the $0.50 deep tier.
_FLAGSHIP = ("get_grid_intelligence", "get_fiber_intel", "get_market_intel")
_MPP_TOOLS = _FLAGSHIP + ("analyze_site", "compare_sites",
                          "get_site_capacity_report", "get_developer_brief",
                          "site_selection_canvas")

# Stripe fiat Shared-Payment-Token minimum. A challenge minted below this is
# unsettleable — the agent would do everything right and still be rejected.
_SPT_FLOOR = 0.50
# What the price list charges for a flagship call on the metered rail.
_FLAGSHIP_LIST_USD = 0.10

# Gateway statuses meaning "unpaid caller was gated" — i.e. the pay offer was
# reachable on this call. (Sourced from server.mjs `status = '…'` sites.)
_GATED_ST = ('depth_teased', 'blocked_paid_only', 'anon_daily_cap',
             'credits_depleted')
# Statuses meaning the caller got data WITHOUT paying — the offer never showed.
_GRANTED_ST = ('trial_taste_inline', 'trial_taste_bounded', 'trial_used',
               'ok', 'credits_full', 'return_reward_full')


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("AGENT_PAY_SHELL_DISABLE") or "").strip() == "1"


def _probe_enabled() -> bool:
    return (os.environ.get("AGENT_PAY_SHELL_PROBE") or "1").strip() != "0"


# ── helpers (contract shared with shells #30-#33) ─────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """Stricter than the shared #30-33 helper, deliberately.

    That version returns PASS when every unknown check is non-critical — so a
    lane whose probe was disabled and which therefore verified NOTHING renders
    green. This shell exists because a metric flattered us for two and a half
    weeks; it must not ship a lane that reads PASS without checking anything.
    """
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is None for k in crits):
        return "?"
    if (any(k["pass"] is None for k in checks)
            and not any(k["pass"] is True for k in checks)):
        return "?"   # nothing was actually verified
    return "PASS"


def _db():
    """Reuse funnel_health's connector so this shell reads exactly the
    connection the watcher reads."""
    try:
        from routes.funnel_health import _conn
        return _conn()
    except Exception as e:  # noqa: BLE001
        logger.debug("[agent-pay] db unavailable: %s", e)
        return None


def _synth_predicates():
    """IMPORT the live synthetic/real predicates — never re-declare them.
    Returns (real_sql, synth_sql, chal_st, paid_st, fail_st) or None."""
    try:
        from routes.funnel_health import (_REAL_PLATFORM_SQL,
                                          _SYNTH_PLATFORM_SQL, _CHAL_ST,
                                          _PAID_ST, _FAIL_ST)
        return (_REAL_PLATFORM_SQL, _SYNTH_PLATFORM_SQL, _CHAL_ST,
                _PAID_ST, _FAIL_ST)
    except Exception as e:  # noqa: BLE001
        logger.debug("[agent-pay] predicate import failed: %s", e)
        return None


def _q(cur, sql, params):
    """Isolated probe — returns a row or None, never raises."""
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.debug("[agent-pay] query failed: %s", e)
        return None


def _n(row, i, default=0):
    try:
        v = row[i]
        return default if v is None else int(v)
    except Exception:
        return default


def _mcp_probe(tool: str, args: dict, _memo={}):
    """ONE bounded anon MCP call against the public gateway, to read what an
    unpaid agent actually receives. Returns (structuredContent, error).

    The clientInfo name carries 'probe' on purpose: the gateway collapses it to
    platform `dchub-internal`, which the synthetic filter excludes — so this
    shell can never inflate its own demand lane. That is also a live end-to-end
    assertion of lane 5.

    ★ Memoized per tick (_run_tick clears it). Lane 2 and lane 3 both want the
    deep-tier offer, and lane 3 falls back to it after the flagship — without
    the memo one tick fired THREE handshakes at prod. Timeouts are deliberately
    tight: this handler answers a WEB request on a single-replica service, so a
    slow gateway must never hold the connection open (see the 502/starve
    traps). Worst case is now ~2 probes x ~28s, typical <8s total.
    """
    key = (tool, json.dumps(args, sort_keys=True))
    if key in _memo:
        return _memo[key]
    # Hard budget for ALL probing in one tick. Per-call timeouts bound a single
    # hop; only this bounds the tick. Without it a wedged gateway could hold a
    # WEB connection for the sum of every hop in every lane.
    left = _PROBE_BUDGET_S - (_now_s() - _tick_started_at[0])
    if _tick_started_at[0] and left <= 2:
        res = (None, "probe budget exhausted (%.0fs) — skipped to protect the "
                     "web replica" % _PROBE_BUDGET_S)
    else:
        res = _mcp_probe_uncached(tool, args)
    _memo[key] = res
    return res


_PROBE_BUDGET_S = 30.0
_tick_started_at = [0.0]


def _now_s() -> float:
    import time
    return time.monotonic()


def _probe_memo_clear():
    """Reset per-tick probe state: memo + the budget clock."""
    try:
        _mcp_probe.__defaults__[0].clear()
    except Exception:  # noqa: BLE001
        pass
    _tick_started_at[0] = _now_s()


def _mcp_probe_uncached(tool: str, args: dict):
    try:
        import requests as _rq   # not urllib (regression_lint)
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "User-Agent": _UA}
        s = _rq.Session()
        r = s.post(ORIGIN + "/mcp", timeout=8, headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "dchub-agent-pay-shell-probe",
                                      "version": "1.0"}}})
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if not sid:
            return None, "no mcp-session-id on initialize (HTTP %d)" % r.status_code
        h2 = dict(h, **{"mcp-session-id": sid})
        s.post(ORIGIN + "/mcp", timeout=5, headers=h2,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r2 = s.post(ORIGIN + "/mcp", timeout=15, headers=h2, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        # ★ Two traps, both silent:
        #   1. the SSE reply carries no charset, so requests falls back to
        #      ISO-8859-1 and .text returns mojibake — decode UTF-8 explicitly.
        #   2. str.splitlines() also breaks on U+0085 / U+2028 / U+2029, which
        #      occur inside DC Hub's own JSON payloads — that severs the data:
        #      line mid-object and the parse fails. Split on "\n" only (the
        #      house parser in shells #30-33 does the same).
        raw = r2.content.decode("utf-8", "replace")
        body = None
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    body = json.loads(line)
                    break
                except Exception:
                    continue
        if body is None:
            return None, "unparseable MCP response"
        return (body.get("result", {}) or {}).get("structuredContent", {}) or {}, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


# ── lane 1 · demand (is anyone REALLY paying?) ────────────────────────

def _lane_demand() -> list[dict]:
    preds = _synth_predicates()
    c = _db()
    if preds is None or c is None:
        return [_check("dm_db", "pay ledger readable", None,
                       "no DB connection" if c is None
                       else "could not import funnel_health predicates",
                       critical=True)]
    real_sql, _synth, chal, paid_st, fail_st = preds
    out: list[dict] = []
    try:
        with c.cursor() as cur:
            r = _q(cur, "SELECT MIN(timestamp) FROM mcp_call_log "
                        " WHERE status = %s AND " + real_sql, (chal,))
            first_chal = r[0] if r else None
            out.append(_check(
                "dm_first_intent", "a REAL agent has ever asked to pay",
                bool(first_chal),
                "first real challenge %s" % first_chal if first_chal else
                "NEVER — zero real pay-intent all-time (the 2026-07-07 "
                "milestone was internal traffic, reclassified 07-25)",
                critical=True))

            r = _q(cur, "SELECT MIN(timestamp), COUNT(*) FROM mcp_call_log "
                        " WHERE status IN %s AND " + real_sql,
                   (tuple(paid_st),))
            first_paid, n_paid = (r[0] if r else None), _n(r, 1)
            out.append(_check(
                "dm_settled", "a REAL agent has ever settled",
                bool(first_paid),
                "first settle %s · %d total" % (first_paid, n_paid)
                if first_paid else
                "no autonomous agent has ever completed a payment",
                critical=True))

            r = _q(cur, "SELECT COUNT(*) FROM mcp_call_log "
                        " WHERE status = %s AND " + real_sql +
                        "   AND timestamp > NOW() - INTERVAL '30 days'",
                   (chal,))
            out.append(_check(
                "dm_30d", "real pay-intent in the last 30d", _n(r, 0) > 0,
                "%d real challenges/30d" % _n(r, 0), critical=False))

            r = _q(cur, "SELECT COUNT(DISTINCT platform) FROM mcp_call_log "
                        " WHERE tool IN %s AND " + real_sql +
                        "   AND timestamp > NOW() - INTERVAL '30 days'",
                   (tuple(_MPP_TOOLS),))
            out.append(_check(
                "dm_audience", "real agents are reaching payable tools at all",
                _n(r, 0) > 0,
                "%d distinct real platforms called a payable tool/30d"
                % _n(r, 0), critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("dm_db", "pay ledger readable", None,
                          "query failed: %s" % str(e)[:110], critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 2 · reachability (does an unpaid agent ever SEE the offer?) ──

def _lane_reachability() -> list[dict]:
    out: list[dict] = []
    # (a) live: does a gated anon call actually carry a ready-to-pay challenge?
    if _probe_enabled():
        sc, err = _mcp_probe("analyze_site", {"lat": 31.99, "lon": -102.08})
        if err:
            out.append(_check("rc_live", "gated preview carries a live "
                              "challenge", None, "probe failed: %s" % err,
                              critical=True))
        else:
            ap = (sc or {}).get("agent_payment") or {}
            chals = ap.get("challenges") or []
            out.append(_check(
                "rc_live", "gated preview carries a ready-to-pay challenge",
                bool(chals),
                "inline challenge present ($%s, one-step)" % ap.get("price_usd")
                if chals else
                ("agent_payment present but NO inline challenge — the sidecar "
                 "is down or the breaker is open; agents fall back to the "
                 "two-step _meta.mpp_pay flag" if ap else
                 "NO agent_payment on a gated preview — the rail is invisible"),
                critical=True))
            out.append(_check(
                "rc_onestep", "offer needs no magic flag to act on",
                bool(ap.get("pay_now")),
                "pay_now recipe present" if ap.get("pay_now") else
                "no machine-readable pay_now — agents must parse prose",
                critical=False))
    else:
        out.append(_check("rc_live", "gated preview carries a live challenge",
                          None, "probe disabled (AGENT_PAY_SHELL_PROBE=0)",
                          critical=True))

    # (b) structural: what share of payable-tool calls are ever gated? The pay
    #     offer sits behind !gate.allowed, so a generous auto-trial means most
    #     agents never reach it. This quantifies that gap.
    preds = _synth_predicates()
    c = _db()
    if preds is None or c is None:
        out.append(_check("rc_share", "offer-eligible share measurable", None,
                          "no DB connection", critical=False))
        return out
    real_sql = preds[0]
    try:
        with c.cursor() as cur:
            r = _q(cur, "SELECT "
                        "  COUNT(*) FILTER (WHERE status IN %s), "
                        "  COUNT(*) FILTER (WHERE status IN %s) "
                        " FROM mcp_call_log WHERE tool IN %s AND " + real_sql +
                        "   AND timestamp > NOW() - INTERVAL '30 days'",
                   (tuple(_GATED_ST), tuple(_GRANTED_ST), tuple(_MPP_TOOLS)))
            gated, granted = _n(r, 0), _n(r, 1)
            tot = gated + granted
            pct = (100.0 * gated / tot) if tot else 0.0
            out.append(_check(
                "rc_share", "unpaid agents actually reach the wall",
                (tot > 0 and pct >= 10.0) if tot else None,
                "%d/%d (%.1f%%) payable-tool calls were gated — the rest were "
                "granted free, so the pay offer never rendered" % (gated, tot, pct)
                if tot else "no real payable-tool traffic in 30d to measure",
                critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("rc_share", "offer-eligible share measurable", None,
                          "query failed: %s" % str(e)[:110], critical=False))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 3 · pricing coherence ────────────────────────────────────────

def _lane_pricing() -> list[dict]:
    """The fiat rail cannot price below Stripe's $0.50 SPT floor, but the
    flagship list price is $0.10 — so an agent paying per-call for a flagship
    pays 5x. That is an open commercial decision, not a bug; acknowledge it
    with MPP_FLAGSHIP_PREMIUM_ACK=1 to close the check deliberately."""
    out: list[dict] = []
    if not _probe_enabled():
        return [_check("pr_floor", "advertised price is settleable", None,
                       "probe disabled (AGENT_PAY_SHELL_PROBE=0)",
                       critical=False)]
    # Prefer a FLAGSHIP observation (that is the tool whose list price the
    # premium is measured against). A flagship call is usually trial-granted and
    # carries no offer, so fall back to the deep tier for the floor check rather
    # than rendering the whole lane unknown.
    sc, err = _mcp_probe("get_grid_intelligence", {"region": "PJM"})
    ap = (sc or {}).get("agent_payment") or {} if not err else {}
    observed_on_flagship = bool(ap)
    if not ap:
        sc2, err2 = _mcp_probe("analyze_site", {"lat": 31.99, "lon": -102.08})
        if err2:
            return [_check("pr_floor", "advertised price is settleable", None,
                           "probe failed: %s" % (err or err2), critical=False)]
        ap = (sc2 or {}).get("agent_payment") or {}
    if not ap:
        # Saying PASS here would be reading success from an unchecked path.
        return [_check("pr_floor", "advertised price is settleable", None,
                       "no offer observable on any payable tool this tick",
                       critical=False)]
    try:
        price = float(ap.get("price_usd"))
    except Exception:
        price = None
    out.append(_check(
        "pr_floor", "advertised price clears the $%.2f Stripe SPT floor"
        % _SPT_FLOOR,
        (price is not None and price >= _SPT_FLOOR),
        "advertised $%s" % ap.get("price_usd") if price is not None
        else "unparseable price %r" % ap.get("price_usd"), critical=True))
    if price is not None:
        ack = (os.environ.get("MPP_FLAGSHIP_PREMIUM_ACK") or "").strip() == "1"
        prem = price / _FLAGSHIP_LIST_USD if _FLAGSHIP_LIST_USD else 0
        if not observed_on_flagship and not ack:
            # The rail prices every tool at the same flat fiat minimum, but this
            # tick did not actually SEE a flagship offer — do not assert a
            # flagship number we did not observe.
            out.append(_check(
                "pr_flagship", "flagship per-call price matches the list price",
                None,
                "flagship offer not observable this tick (trial-granted); "
                "deep-tier advertised $%.2f vs $%.2f flagship list price"
                % (price, _FLAGSHIP_LIST_USD), critical=False))
            return out
        out.append(_check(
            "pr_flagship", "flagship per-call price matches the list price",
            True if ack else (price <= _FLAGSHIP_LIST_USD),
            ("acknowledged: $%.2f vs $%.2f list (%.0fx) is a deliberate "
             "choice — MPP_FLAGSHIP_PREMIUM_ACK=1" % (price, _FLAGSHIP_LIST_USD, prem))
            if ack else
            ("$%.2f charged vs $%.2f list price = %.0fx premium. Stripe's fiat "
             "SPT floor forbids charging less, so the options are: accept it, "
             "bundle (one $%.2f buys N flagship calls), or keep per-call pay "
             "for deep tools only. Set MPP_FLAGSHIP_PREMIUM_ACK=1 to close."
             % (price, _FLAGSHIP_LIST_USD, prem, price)),
            critical=False))
    return out


# ── lane 4 · rail health (plumbing, independent of demand) ────────────

def _lane_rail_health() -> list[dict]:
    """Keeps 'nobody paid' from being misread as 'the rail is broken' — the
    error that cost two and a half weeks. Settle FAILURES are a bug signal;
    abandonment is a demand signal. They must never share a number."""
    preds = _synth_predicates()
    c = _db()
    if preds is None or c is None:
        return [_check("rh_db", "pay ledger readable", None,
                       "no DB connection", critical=True)]
    real_sql, _s, chal, paid_st, fail_st = preds
    out: list[dict] = []
    try:
        with c.cursor() as cur:
            r = _q(cur, "SELECT COUNT(*) FROM mcp_call_log "
                        " WHERE status IN %s AND " + real_sql +
                        "   AND timestamp > NOW() - INTERVAL '30 days'",
                   (tuple(fail_st),))
            fails = _n(r, 0)
            out.append(_check(
                "rh_settle_errors", "no settle FAILURES (a bug signal)",
                fails == 0,
                "clean — 0 verify failures/30d" if fails == 0 else
                "%d settle failures/30d — agents TRIED and the rail rejected "
                "them; this is a defect, not a demand gap" % fails,
                critical=True))

            r = _q(cur, "SELECT COUNT(*) FILTER (WHERE status = %s), "
                        "       COUNT(*) FILTER (WHERE status IN %s) "
                        " FROM mcp_call_log WHERE " + real_sql +
                        "   AND timestamp > NOW() - INTERVAL '30 days'",
                   (chal, tuple(paid_st)))
            ch, pd = _n(r, 0), _n(r, 1)
            out.append(_check(
                "rh_abandon", "challenge→settle conversion (demand signal)",
                None if ch == 0 else (pd > 0),
                "no real challenges in 30d — nothing to convert (demand, see "
                "lane 1)" if ch == 0 else
                "%d/%d settled (%.0f%%)" % (pd, ch, 100.0 * pd / ch),
                critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("rh_db", "pay ledger readable", None,
                          "query failed: %s" % str(e)[:110], critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 5 · metric integrity (the filter that lied) ──────────────────

def _lane_metric_integrity() -> list[dict]:
    """Pins the synthetic-traffic filter itself. This is the regression guard
    for the 2026-07-25 defect: the gateway funnels ALL internal/QA clientInfo
    into platform `dchub-internal`, on the stated assumption that every backend
    read predicate excludes %dchub% — and this one did not, so our own probes
    read as customer demand for weeks."""
    out: list[dict] = []
    preds = _synth_predicates()
    if preds is None:
        return [_check("mi_import", "synthetic filter importable", None,
                       "could not import funnel_health predicates",
                       critical=True)]
    real_sql, synth_sql = preds[0], preds[1]
    low = synth_sql.lower()
    for token, why in (("dchub", "the tag EVERY internal caller collapses to"),
                       ("test", "harness self-IDs"),
                       ("probe", "diagnostic probes"),
                       ("verify", "verification runs"),
                       ("harness", "the value harness")):
        out.append(_check(
            "mi_%s" % token, "filter excludes %%%s%%" % token,
            token in low,
            "present" if token in low else
            "MISSING — %s now count as REAL agent demand" % why,
            critical=(token == "dchub")))

    # Live: no internal platform may appear inside the "real" set.
    c = _db()
    if c is None:
        out.append(_check("mi_live", "no internal platform scores as real",
                          None, "no DB connection", critical=True))
        return out
    try:
        with c.cursor() as cur:
            # ★ params MUST be non-empty: the imported predicate carries
            # doubled %% and psycopg2 only collapses it when it performs
            # substitution. A None/() params here would leave literal '%%' in
            # every LIKE and silently match nothing.
            r = _q(cur, "SELECT COUNT(*) FROM mcp_call_log "
                        " WHERE " + real_sql +
                        "   AND LOWER(COALESCE(platform,'')) LIKE %s "
                        "   AND timestamp > NOW() - INTERVAL '30 days'",
                   ('%dchub%',))
            leaked = _n(r, 0) if r is not None else None
            out.append(_check(
                "mi_live", "no internal platform scores as real",
                None if r is None else (leaked == 0),
                "clean — 0 dchub-tagged rows inside the real set"
                if leaked == 0 else
                "%d internal rows counted as REAL — the filter has regressed"
                % leaked, critical=True))
    except Exception as e:  # noqa: BLE001
        out.append(_check("mi_live", "no internal platform scores as real",
                          None, "query failed: %s" % str(e)[:110],
                          critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str) -> None:
    try:
        body = json.dumps({
            "feed": "agent-pay-shell-daily",
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
        import requests as _rq   # not urllib (regression_lint)
        _rq.post("http://127.0.0.1:" + str(port)
                 + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-agent-pay-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[agent-pay] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn) -> list[dict]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s"
                       % (type(e).__name__, str(e)[:120]), critical=True)]


def _run_tick() -> dict:
    _probe_memo_clear()   # each tick gets fresh live evidence
    lanes = [
        {"id": "demand", "name": "1 · real agent demand",
         "checks": _safe_lane(_lane_demand)},
        {"id": "reachability", "name": "2 · offer reachability",
         "checks": _safe_lane(_lane_reachability)},
        {"id": "pricing", "name": "3 · pricing coherence",
         "checks": _safe_lane(_lane_pricing)},
        {"id": "rail_health", "name": "4 · rail health (plumbing)",
         "checks": _safe_lane(_lane_rail_health)},
        {"id": "metric_integrity", "name": "5 · metric integrity",
         "checks": _safe_lane(_lane_metric_integrity)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "agent-pay-34",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
        "note": "Lane 4 proves the rail works; lane 1 proves whether anyone "
                "uses it. Never read lane 1 as a plumbing failure without "
                "lane 4, and never trust either without lane 5.",
    }
    _beat_ledger("lanes: " + summary)
    return out


def _no_store(resp):
    # ★CF caches admin GETs (30-min stale-board trap) — always no-store.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@agent_pay_master_shell_bp.route("/api/v1/admin/agent-pay-shell/master-tick",
                                 methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return _no_store(jsonify(
            ok=False, error="AGENT_PAY_SHELL_DISABLE=1")), 503
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(_run_tick()))


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@agent_pay_master_shell_bp.route("/admin/agent-pay-shell", methods=["GET"])
@agent_pay_master_shell_bp.route("/api/v1/admin/agent-pay-shell", methods=["GET"])
def dashboard():
    from flask import make_response
    if _disabled():
        return _no_store(make_response(
            "<h1>Agent Pay</h1><p>AGENT_PAY_SHELL_DISABLE=1</p>", 503))
    if not _admin_ok():
        return _no_store(make_response(
            "<h1>401</h1><p>admin key required</p>", 401))
    t = _run_tick()
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
    html = ("<html><head><title>Agent Pay #34</title>"
            "<meta http-equiv='refresh' content='120'></head>"
            "<body style='font-family:system-ui;max-width:1150px;margin:24px auto'>"
            "<h1>Agent Pay <small>#34</small></h1>"
            "<p>%s</p>"
            "<p><small>%s</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
            "<th align='left'>checks</th></tr>%s</table>"
            "<p><small>refreshes 120s · one bounded live MCP probe per tick "
            "(AGENT_PAY_SHELL_PROBE=0 to disable) · kill "
            "AGENT_PAY_SHELL_DISABLE=1</small></p></body></html>"
            % (_esc(t["generated_at"]), _esc(t["note"]), "".join(rows)))
    return _no_store(make_response(html, 200))
