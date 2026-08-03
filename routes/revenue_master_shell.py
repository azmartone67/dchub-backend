"""Revenue Master Shell (#50) — 2026-08-03.

The other shells watch the machine. This one watches the three questions that
decide whether any of it earns money, and it was built the day those three
finally had live numbers instead of beliefs.

★WHY EACH LANE EXISTS — every one is a measurement taken on 2026-08-03 that
contradicted what we thought:

  1 WHO IS CALLING — the artifact returned assistant 176 (1.2%), tooling
                     2,518, unknown 12,452 (82.2%) across 14 unclassified
                     user-agents, and "No attributable meta-ai traffic in the
                     last 30 days". Naming them moved most of that mass into
                     `mcp` — a generic client, 9,220 calls / 207 agents, that
                     never says who it is. The share barely changed: ~61% of
                     traffic is still unidentifiable, so no platform claim is
                     verifiable in EITHER direction.
                     ★AND THE FIRST LIVE RUN OF THIS LANE WENT GREEN ANYWAY,
                     because the check counted only `unknown` and the rename
                     had moved 9,220 calls out of its numerator. A rename is
                     not a fix, and a check a rename can satisfy is not a
                     check. It now counts unknown + unattributed, always.

  2 WHAT THINKING COSTS — 17 model calls, ~50k tokens, in a WEEK. Token spend
                     is a rounding error and optimising it is wasted effort.
                     ★THIS LANE EXISTS TO STOP WORK, NOT START IT. But the same
                     table says WHY it is cheap: 3 of 20 instrumented layers
                     fired at all, and L14 ran 3 times in 7 days at a 48s p50.
                     The causal-edge ranking shipped in #49 lane 2 consumes L14
                     chains — at 3 runs/week it has almost nothing to rank.
                     Cheap and dormant are the same finding read twice.

  3 THE HUMAN HOP — check_relay_opens.py returned its OTHER answer on
                     2026-08-03: real=0 of 2 total, write path PROVEN, so
                     envelope shape is RULED OUT and the pre-registered stop
                     rule says stop tuning MCP fields. The lever it names is
                     the bind-time receipt, which is fully built, idempotent,
                     suppression-honouring, transactional-consent-clean — and
                     DISARMED. Unarmed it logs every intended recipient to
                     bind_receipt_log, so the blast radius of arming it is a
                     number we already have rather than a risk we guess at.

★HONESTY RULE (inherited from Integrity #25 / Loop Control #48): a lane must
never read PASS when it could not check. Every lane here degrades to "?" rather
than guess, and a green lane 2 means "stop working on this", which is only
honest if the number behind it is real.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing. In
particular this shell NEVER sends an email and never arms the receipt.

Endpoints:
  GET/POST /api/v1/admin/revenue/master-tick   JSON scoreboard (3 lanes)
  GET      /admin/revenue                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/revenue                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY).
Kill: REVENUE_SHELL_DISABLE=1
"""
from __future__ import annotations

import logging
import os
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
revenue_master_shell_bp = Blueprint("revenue_master_shell", __name__)

# Measured 2026-08-03 — the readings this shell was built from. They are
# BASELINES, not thresholds: a lane compares today against them so drift is
# visible, and a number that has not moved is itself the finding.
BASELINE = {
    "date": "2026-08-03",
    # 12,452 of 15,146 calls, 30d — measured BEFORE the platforms were named.
    # Post-naming the same mass is mostly `mcp` (unattributed), so the honest
    # comparison is against unnamed_share below, not this one.
    "unknown_share": 0.822,
    "unnamed_share": 0.61,       # unknown + unattributed, the real blind spot
    "assistant_calls_30d": 176,
    "unknown_platforms": 14,
    "week_tokens": 50_787,       # 35,349 in + 15,438 out, 7d
    "week_model_calls": 17,
    "relay_real_opens": 0,
}

# Above this share of unclassified traffic, no platform claim is verifiable.
_UNKNOWN_SHARE_CEILING = float(
    os.environ.get("REVENUE_UNKNOWN_SHARE_CEILING", "0.20"))
# A week's token spend below this is not worth an engineer's afternoon.
_TOKENS_WEEK_TRIVIAL = int(
    os.environ.get("REVENUE_TOKENS_WEEK_TRIVIAL", "2000000"))
# L14 runs per week below which the causal graph is starved of input.
_L14_RUNS_WEEK_FLOOR = int(os.environ.get("REVENUE_L14_RUNS_FLOOR", "14"))


def _disabled() -> bool:
    return (os.environ.get("REVENUE_SHELL_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _conn():
    try:
        import psycopg2
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=10)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[revenue] connect failed: %s", str(e)[:120])
        return None


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:900], "critical": critical}


def _verdict(checks):
    d = [c for c in checks if c["pass"] is not None]
    return all(c["pass"] for c in d) if d else None


def _one(cur, sql, args=None):
    try:
        cur.execute(sql, args or ())
        r = cur.fetchone()
        return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[revenue] query failed: %s", str(e)[:140])
        return None


def _has_table(cur, table: str):
    return _one(cur, "SELECT COUNT(*) FROM information_schema.tables "
                     "WHERE table_name = %s", (table,))


# ── lane 1 · who is actually calling us ───────────────────────────────
def _lane_platforms(cur) -> list:
    out = []
    try:
        from routes.platform_attribution import platform_rows, classify_platform
        data = platform_rows(cur, days=30)
    except Exception as e:  # noqa: BLE001
        return [_check("platform_canon", "platform attribution is computable",
                       None, f"routes.platform_attribution unusable "
                             f"({str(e)[:90]}) — UNMEASURED", critical=True)]
    if not data.get("ok"):
        return [_check("platform_canon", "platform attribution is computable",
                       None, f"{data.get('error')} — UNMEASURED, not zero",
                       critical=True)]

    by_kind = data.get("by_kind") or {}
    total = sum(k.get("calls", 0) for k in by_kind.values())
    # ★UNNAMED = unknown + unattributed. Both, always.
    #
    # The first live run of this lane PASSED at 0.2% while 61% of traffic was a
    # generic `mcp` client that never said who it was. Cause: `unattributed`
    # was given its own kind so the blind spot would stay VISIBLE, and then
    # this check counted only `unknown` — so reclassifying moved 9,220 calls
    # out of the numerator and the lane went green without one thing about the
    # traffic changing. A rename is not a fix, and a check that a rename can
    # satisfy is not a check.
    _UNNAMED_KINDS = ("unknown", "unattributed")
    unk_calls = sum(int((by_kind.get(k) or {}).get("calls", 0))
                    for k in _UNNAMED_KINDS)
    unk_share = (unk_calls / total) if total else 0.0
    unk_names = sorted(p["platform"] for p in data.get("platforms", [])
                       if classify_platform(p["platform"]) in _UNNAMED_KINDS)

    out.append(_check(
        "platform_canon", "platform attribution is computable", True,
        f"{total:,} attributable call(s) in 30d at the canonical grain across "
        f"{len(data.get('platforms', []))} platform tag(s)."))

    # ★THE LANE. Until this is green no platform claim is verifiable — in
    # EITHER direction. "meta-ai returned zero" is not evidence Meta is absent
    # while four fifths of traffic is unnamed.
    out.append(_check(
        "traffic_named", "most traffic comes from a NAMED platform",
        unk_share <= _UNKNOWN_SHARE_CEILING,
        f"{unk_calls:,} of {total:,} calls ({unk_share*100:.1f}%, ceiling "
        f"{_UNKNOWN_SHARE_CEILING*100:.0f}%) are from user-agents nobody has "
        f"classified, across {len(unk_names)} tag(s): "
        f"{', '.join(unk_names[:14]) or 'none'}. "
        f"★A licence conversation cannot start here: a counterparty's first "
        f"question is 'which of these is us', and for {unk_calls:,} of our "
        f"own calls we cannot answer it. "
        f"Actuator: add each name to ASSISTANT_PLATFORMS or TOOLING_PLATFORMS "
        f"in routes/platform_attribution.py — or, if the view emits it as "
        f"'untagged', extend the user-agent CASE in mcp_calls_deloop and "
        f"re-render the identity views.",
        critical=True))

    asst = int((by_kind.get("assistant") or {}).get("calls", 0))
    out.append(_check(
        "assistant_share", "AI assistants are a material share of traffic",
        asst > (total * 0.10) if total else None,
        f"{asst:,} assistant call(s) in 30d "
        f"({(asst/total*100) if total else 0:.1f}% of attributable traffic; "
        f"baseline {BASELINE['assistant_calls_30d']} on {BASELINE['date']}). "
        f"★This reframes the gating question: we are not giving too much away "
        f"to AI platforms while AI platforms are a rounding error. Tightening "
        f"gates cannot grow a channel that is not here yet."))
    return out


# ── lane 2 · what thinking costs, and why it is cheap ─────────────────
def _lane_spend(cur) -> list:
    out = []
    if not _has_table(cur, "brain_llm_spend"):
        return [_check("spend_ledger", "model spend is recorded", None,
                       "brain_llm_spend absent — UNMEASURED, not zero. "
                       "(A missing ledger reading as 'free' is exactly the "
                       "flattering zero this shell exists to refuse.)",
                       critical=True)]
    row = None
    try:
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COUNT(DISTINCT layer)
              FROM brain_llm_spend
             WHERE created_at >= NOW() - INTERVAL '7 days'""")
        row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.debug("[revenue] spend query failed: %s", str(e)[:140])
    if not row:
        return [_check("spend_ledger", "model spend is recorded", None,
                       "ledger unreadable — UNMEASURED", critical=True)]
    calls, tin, tout, layers = (int(x or 0) for x in row)
    tokens = tin + tout

    # ★A PASS HERE MEANS "STOP WORKING ON THIS". That is only honest if the
    # number is real, which is why the missing-ledger branch above refuses to
    # score rather than reading absence as zero.
    out.append(_check(
        "spend_trivial", "token spend is too small to be worth optimising",
        tokens <= _TOKENS_WEEK_TRIVIAL,
        f"{tokens:,} token(s) across {calls:,} model call(s) in 7d "
        f"({tin:,} in / {tout:,} out, {layers} layer(s) firing; baseline "
        f"{BASELINE['week_tokens']:,} on {BASELINE['date']}). ★GREEN MEANS "
        f"STOP: below {_TOKENS_WEEK_TRIVIAL:,}/week this is not worth an "
        f"engineer's afternoon. Token efficiency work is CLOSED until this "
        f"lane goes red."))

    l14 = _one(cur, """SELECT COUNT(*) FROM brain_llm_spend
                        WHERE layer = 'brain_layer14_causal'
                          AND created_at >= NOW() - INTERVAL '7 days'""")
    if l14 is None:
        out.append(_check("brain_throughput", "the brain thinks often enough "
                          "to feed the causal graph", None,
                          "per-layer query failed — UNMEASURED"))
    else:
        out.append(_check(
            "brain_throughput",
            "the brain thinks often enough to feed the causal graph",
            int(l14) >= _L14_RUNS_WEEK_FLOOR,
            f"L14 ran {int(l14)} time(s) in 7d (floor "
            f"{_L14_RUNS_WEEK_FLOOR}). ★CHEAP AND DORMANT ARE THE SAME FINDING "
            f"READ TWICE: spend is trivial BECAUSE the brain barely runs. The "
            f"root-cause ranking shipped in #49 lane 2 consumes L14 chains, so "
            f"at this rate it has almost nothing to collapse — the ranking is "
            f"correct and starved. Actuator: BRAIN_CAUSAL_ENABLED and the "
            f"BRAIN_CAUSAL_DAILY_CAP (default 4/day) are the throttle; raising "
            f"them costs ~{max(1, tokens // max(calls, 1)):,} tokens per call, "
            f"which the lane above says is affordable.",
            critical=True))
    return out


# ── lane 3 · the human hop — the only conversion lever left ───────────
def _lane_human_hop(cur) -> list:
    out = []
    # The relay experiment, re-read rather than remembered.
    if not _has_table(cur, "relay_opens"):
        out.append(_check("relay_verdict", "the relay experiment has a verdict",
                          None, "relay_opens absent — UNMEASURED", critical=True))
    else:
        total = _one(cur, "SELECT COUNT(*) FROM relay_opens")
        # ★INTROSPECT THE MARKER COLUMN, do not assume `source`. The first live
        # run returned "relay_opens unreadable — UNMEASURED" because this
        # hardcoded a column the table does not have. loop_control lane 8
        # already solved it by trying a list of candidates; assuming one was a
        # self-inflicted blind spot in the lane whose whole job is reading a
        # verdict someone else already computed.
        marker = None
        for cand in ("source", "user_agent", "ua", "note", "kind",
                     "opened_by", "channel", "referer"):
            if _one(cur, "SELECT COUNT(*) FROM information_schema.columns "
                         "WHERE table_name = 'relay_opens' "
                         "AND column_name = %s", (cand,)):
                marker = cand
                break
        real = None
        if marker:
            real = _one(cur, f"""
                SELECT COUNT(*) FROM relay_opens
                 WHERE position('dchub-ops-verify' in lower(coalesce({marker}::text,''))) = 0
                   AND position('human-simulated' in lower(coalesce({marker}::text,''))) = 0
                   AND position('ops-verify' in lower(coalesce({marker}::text,''))) = 0
                   AND position('probe' in lower(coalesce({marker}::text,''))) = 0""")
        if total is None or real is None:
            out.append(_check("relay_verdict",
                              "the relay experiment has a verdict", None,
                              f"relay_opens unreadable (marker column "
                              f"{marker!r}) — UNMEASURED, not clean. Probe "
                              f"rows must never be scored as humans.",
                              critical=True))
        else:
            # ★NOT A FAILURE. The experiment pre-registered both answers; this
            # is the one that RULES OUT envelope shape and redirects the work.
            out.append(_check(
                "relay_verdict", "envelope tuning is CLOSED", True,
                f"{int(real)} real human open(s) of {int(total)} row(s) "
                f"(baseline {BASELINE['relay_real_opens']} on "
                f"{BASELINE['date']}, write path PROVEN). Per "
                f"check_relay_opens.py's pre-registered stop rule, envelope "
                f"shape is RULED OUT — stop tuning MCP fields. This is the "
                f"experiment's other answer, not a failure to fix anything."))

    # The lever the stop rule names.
    armed = (os.environ.get("DCHUB_BIND_RECEIPT_ARM") or "").strip() == "1"
    if not _has_table(cur, "bind_receipt_log"):
        out.append(_check(
            "bind_receipt", "the bind-time receipt is reaching humans", None,
            f"bind_receipt_log absent — UNMEASURED. The receipt is built "
            f"(routes/auto_trial._send_bind_receipt) and "
            f"DCHUB_BIND_RECEIPT_ARM is "
            f"{'SET' if armed else 'unset'}; the table appears on first bind.",
            critical=True))
        return out
    would = _one(cur, "SELECT COUNT(*) FROM bind_receipt_log WHERE armed = false")
    sent = _one(cur, "SELECT COUNT(*) FROM bind_receipt_log WHERE armed = true")
    delivered = _one(cur, "SELECT COUNT(*) FROM bind_receipt_log "
                          "WHERE delivered = true")
    if would is None:
        out.append(_check("bind_receipt",
                          "the bind-time receipt is reaching humans", None,
                          "bind_receipt_log unreadable — UNMEASURED",
                          critical=True))
        return out
    out.append(_check(
        "bind_receipt", "the bind-time receipt is reaching humans",
        armed and int(sent or 0) > 0,
        f"DCHUB_BIND_RECEIPT_ARM is {'SET' if armed else 'UNSET'}. "
        f"{int(would)} recipient(s) logged by the DRY RUN (would have been "
        f"emailed), {int(sent or 0)} sent armed, {int(delivered or 0)} "
        f"confirmed delivered. "
        + ("Receipts are going out." if armed else
           f"★THE BLAST RADIUS IS A NUMBER, NOT A GUESS: arming would have "
           f"mailed {int(would)} address(es) to date — one per key, ever, "
           f"suppression honoured, transactional consent basis, and the "
           f"upgrade link is key-scoped so the agent keeps working with "
           f"nothing to reconfigure. This is the lever the relay stop rule "
           f"names. Actuator: DCHUB_BIND_RECEIPT_ARM=1 on Railway — an "
           f"OUTWARD-FACING send, so this shell will never set it."),
        critical=True))
    return out


def _run_tick() -> dict:
    out = {"shell": "revenue", "n": 50, "lanes": [], "baseline": BASELINE,
           "note": ("Read-only. The three questions that decide whether the "
                    "machine earns money, built the day they first had live "
                    "numbers. Lane 2 going GREEN means STOP working on token "
                    "efficiency — a pass here is permission to leave it "
                    "alone, not a compliment.")}
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            for lid, label, checks in (
                ("platforms", "1 · who is actually calling us",
                 _lane_platforms(cur)),
                ("spend", "2 · what thinking costs — and why it is cheap",
                 _lane_spend(cur)),
                ("human_hop", "3 · the human hop — the only lever left",
                 _lane_human_hop(cur)),
            ):
                out["lanes"].append({"id": lid, "lane": label,
                                     "checks": checks,
                                     "pass": _verdict(checks)})
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass
    decided = [ln["pass"] for ln in out["lanes"] if ln["pass"] is not None]
    out["lanes_pass"] = sum(1 for p in decided if p)
    out["lanes_total"] = len(out["lanes"])
    out["ok"] = True
    return out


@revenue_master_shell_bp.route("/api/v1/admin/revenue/master-tick",
                               methods=["GET", "POST"])
def revenue_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_run_tick())


@revenue_master_shell_bp.route("/api/v1/admin/revenue", methods=["GET"])
def revenue_tick_alias():
    """CF zone-worker bypass alias — /admin/* is edge-cached in places."""
    return revenue_tick()


@revenue_master_shell_bp.route("/admin/revenue", methods=["GET"])
def revenue_dashboard():
    if _disabled():
        return Response("revenue shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _run_tick()

    def chip(v):
        if v is True:
            return '<span style="color:#22c55e">PASS</span>'
        if v is False:
            return '<span style="color:#ef4444">FAIL</span>'
        return '<span style="color:#eab308">?</span>'

    rows = []
    for ln in p.get("lanes", []):
        rows.append(f'<h3>{_esc(ln["lane"])} — {chip(ln["pass"])}</h3><ul>')
        for ch in ln["checks"]:
            star = " ★" if ch.get("critical") else ""
            rows.append(f'<li>{chip(ch["pass"])}{star} <b>{_esc(ch["name"])}</b>'
                        f'<br><small>{_esc(ch["detail"])}</small></li>')
        rows.append("</ul>")
    err = p.get("error")
    return Response(
        "<html><head><meta http-equiv='refresh' content='60'>"
        "<title>Revenue — Shell #50</title></head>"
        "<body style='font-family:system-ui;background:#0b0b12;color:#e6e6f0;"
        "padding:24px;max-width:940px'>"
        "<h1>Revenue — Shell #50</h1>"
        f"<p><small>{_esc(p.get('note',''))}</small></p>"
        + (f"<p style='color:#ef4444'>error: {_esc(str(err))}</p>" if err else "")
        + f"<p>lanes passing {p.get('lanes_pass','?')}/"
          f"{p.get('lanes_total','?')} · baseline {BASELINE['date']}</p>"
        + "".join(rows) + "</body></html>", mimetype="text/html")
