"""White-Glove Loop Master Shell (#45) — 2026-07-30.

Shell #43 asks whether a paying CUSTOMER was looked after. This one covers the
rest of the loop the owner named: new MCP PARTNERS, AI AGENTS, PRODUCT
INFORMATION reaching its surfaces, and BRAIN EXPANSION.

★WHY EACH LANE EXISTS — every one is a failure that already happened and stayed
invisible:

  1 PARTNERS  — partner keys are issued, but nothing checked whether a partner
                ever CALLED after being handed one. An issued key that is never
                used is indistinguishable from a won partner.
  2 AGENTS    — inflow is fine and retention is the leak. Measured repeatedly:
                ~150+ new external IPs/wk against ~30 returning. The dashboards
                lead with the flattering number.
  3 PRODUCT   — /whats-new read `d.platform_updates`; the API emits the cards
                flat under `platform`. The key never existed, so the page told
                every visitor "Platform updates are unavailable — the feed did
                not return them" against a healthy 5-card feed, for weeks. The
                brain's approved product updates reached nobody. NOTHING checked
                that the producer's key and the consumer's key still match.
  4 BRAIN     — the six mechanical transform classes are EXHAUSTED (every
                instance found and fixed; last autofix PR 2026-07-19). Measured
                2026-07-30 against the real classifier: only EIGHT proposals are
                blocked solely by the class gate, so widening the allowlist is
                not the lever. Autonomy is capped by DETECTOR SUPPLY.

★READ-ONLY. Lanes are red when the underlying thing is broken, not when the
shell is unhappy. Several are red on purpose and name the undone work.

GET /admin/white-glove-loop · /api/v1/admin/white-glove-loop/master-tick
Kill: WHITE_GLOVE_LOOP_SHELL_DISABLE=1
"""
from __future__ import annotations

import os
import logging

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
white_glove_loop_master_shell_bp = Blueprint("white_glove_loop_master_shell",
                                             __name__)

_DAYS = int(os.environ.get("WHITE_GLOVE_LOOP_DAYS", "30"))
# Below this share of returning agents, inflow is being wasted.
_RETENTION_FLOOR = float(os.environ.get("WHITE_GLOVE_RETENTION_FLOOR", "0.25"))
# The keys the /whats-new consumer actually reads. If the API stops emitting
# one of these, the page silently degrades — exactly the #1092 failure.
_PRODUCT_CONTRACT_KEYS = ("platform", "platform_unavailable_reason")

_SYNTH = ("dchub-internal", "dchub-selfheal", "value-harness",
          "fixwave-probe", "reviewer-sim")


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _disabled() -> bool:
    return (os.environ.get("WHITE_GLOVE_LOOP_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[wg-loop] db connect failed: %s", str(e)[:120])
        return None


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:430], "critical": critical}


def _verdict(checks):
    d = [c for c in checks if c["pass"] is not None]
    return all(c["pass"] for c in d) if d else None


def _one(cur, sql, args=None):
    try:
        cur.execute(sql, args or ())
        r = cur.fetchone()
        return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[wg-loop] query failed: %s", str(e)[:140])
        return None


# ── lane 1 · new MCP partners ─────────────────────────────────────────
def _lane_partners(cur) -> list:
    out = []
    issued = _one(cur, "SELECT COUNT(*) FROM partner_keys_issued")
    recent = _one(cur, """SELECT COUNT(*) FROM partner_keys_issued
                           WHERE issued_at >= NOW() - make_interval(days => %s)""",
                  (_DAYS,))
    if issued is None:
        out.append(_check("partner_keys", "partner keys are being issued", None,
                          "partner_keys_issued unreadable — UNMEASURED, not zero"))
    else:
        out.append(_check(
            "partner_keys", "partner keys are being issued",
            int(recent or 0) > 0,
            f"{int(recent or 0)} issued in {_DAYS}d ({int(issued)} lifetime)"))
    # ★An ISSUED key that never gets used is not a won partner. Nothing checked
    # this before: the loop counted handoffs, never arrivals.
    # ★JOIN SHAPE MATTERS — two traps in one query, both hit while writing it:
    #  1. partner_keys_issued has `key_prefix`, NOT `api_key`; and it is a REAL
    #     24-25 char prefix (e.g. dchub_developer_Fk6EJsdh), so a LIKE join is
    #     both possible and specific. This is the OPPOSITE of api_keys.key_prefix
    #     ("dchub_dev_"), which is generic and unjoinable — do not generalise
    #     from one to the other.
    #  2. the caller key lives on `mcp_call_log`, NOT `mcp_tool_calls`
    #     (mcp_tool_calls has no api_key column at all).
    # The >=12 length guard keeps a short/blank prefix from LIKE-matching every
    # key on the platform, which would report 100% activation — a flattering
    # false positive, the mirror of the false zero.
    # ★★LITERAL % MUST BE DOUBLED. _one() calls cur.execute(sql, args or ()),
    # and psycopg2 still runs %-interpolation on an EMPTY tuple — so a single
    # '%' in the LIKE raises IndexError, _one swallows it, and the check reports
    # UNMEASURED against a query that would have worked. Cost me one round-trip
    # here after the standalone probe (which used %%) succeeded.
    joinable = _one(cur, """SELECT COUNT(*) FROM partner_keys_issued
                             WHERE key_prefix IS NOT NULL
                               AND length(key_prefix) >= 12""")
    used = _one(cur, """SELECT COUNT(DISTINCT p.key_prefix)
                          FROM partner_keys_issued p
                         WHERE p.key_prefix IS NOT NULL
                           AND length(p.key_prefix) >= 12
                           AND EXISTS (SELECT 1 FROM mcp_call_log t
                                        WHERE t.api_key LIKE p.key_prefix || '%%')""")
    if used is None:
        out.append(_check(
            "partner_activated", "issued partner keys are actually CALLED", None,
            "cannot join partner_keys_issued to mcp_tool_calls — UNMEASURED, "
            "never reported as zero. (A false zero here would read as 'no "
            "partner ever used us', which is the most damaging possible "
            "misreport.)", critical=True))
    else:
        tot = int(joinable or 0)
        u = int(used)
        rate = (u / tot) if tot else 0.0
        out.append(_check(
            "partner_activated", "issued partner keys are actually CALLED",
            rate >= 0.25,
            f"{u} of {tot} issued partner key(s) have EVER made a call "
            f"({rate*100:.1f}%). An issued key that is never used is "
            f"indistinguishable from a won partner unless this is measured — "
            f"and nothing measured it before. Handing out a key is the START of "
            f"the relationship, not the win.", critical=True))
    return out


# ── lane 2 · AI agents: inflow vs retention ───────────────────────────
def _lane_agents(cur) -> list:
    synth = "', '".join(_SYNTH)
    new7 = _one(cur, f"""SELECT COUNT(DISTINCT ip_address) FROM mcp_tool_calls
                          WHERE created_at >= NOW() - INTERVAL '7 days'
                            AND COALESCE(platform,'') NOT IN ('{synth}')""")
    ret7 = _one(cur, f"""SELECT COUNT(DISTINCT ip_address) FROM mcp_tool_calls
                          WHERE created_at >= NOW() - INTERVAL '7 days'
                            AND COALESCE(platform,'') NOT IN ('{synth}')
                            AND ip_address IN (
                                SELECT ip_address FROM mcp_tool_calls
                                 WHERE created_at < NOW() - INTERVAL '7 days')""")
    if new7 is None or ret7 is None:
        return [_check("agent_retention", "returning agents", None,
                       "mcp_tool_calls unreadable — UNMEASURED", critical=True)]
    n, r = int(new7), int(ret7)
    share = (r / n) if n else 0.0
    return [
        _check("agent_inflow", "real external agents are arriving", n > 0,
               f"{n} distinct real external agent(s) in 7d "
               f"(synthetic/probe platforms excluded)"),
        # ★RETENTION IS THE LEAK, and it is the number the dashboards bury.
        # Inflow has never been the constraint.
        _check("agent_retention", "arriving agents COME BACK",
               share >= _RETENTION_FLOOR,
               f"{r}/{n} returning ({share*100:.1f}%, floor "
               f"{_RETENTION_FLOOR*100:.0f}%). Inflow is not the constraint and "
               f"never has been — a headline agent count that rises while this "
               f"stays flat is churn wearing growth's clothes.",
               critical=True),
    ]


# ── lane 3 · product information actually REACHES its surfaces ────────
def _lane_product() -> list:
    """★THE CONTRACT CHECK THAT WOULD HAVE CAUGHT #1092.

    /whats-new read `d.platform_updates`; the API emits the cards flat under
    `platform`. The key never existed, so the page told every visitor the feed
    was unavailable while the feed served 5 healthy cards — for weeks. Both
    halves were individually "fine"; only the JOIN was broken, and nothing
    tested the join.

    So this asserts the CONSUMER'S keys are present in the PRODUCER'S payload.
    Publishing a number is not enough — it has to be readable by the thing that
    renders it.
    """
    try:
        import requests
        r = requests.get("https://dchub.cloud/api/v1/whats-new",
                         headers={"Accept": "application/json"}, timeout=25)
        r.raise_for_status()
        d = r.json() or {}
    except Exception as e:  # noqa: BLE001
        return [_check("product_feed", "the product-update feed answers", None,
                       f"feed unreachable ({str(e)[:90]}) — UNMEASURED",
                       critical=True)]
    missing = [k for k in _PRODUCT_CONTRACT_KEYS if k not in d]
    cards = d.get("platform")
    n = len(cards) if isinstance(cards, list) else None
    return [
        _check("product_contract",
               "the feed emits every key the /whats-new page reads",
               not missing,
               f"consumer reads {list(_PRODUCT_CONTRACT_KEYS)}; "
               + ("all present" if not missing else
                  f"MISSING {missing} — the page will silently render its "
                  f"'unavailable' state against a healthy feed, which is "
                  f"exactly how #1092 hid brain-approved updates for weeks"),
               critical=True),
        _check("product_cards", "approved product updates are being published",
               (n or 0) > 0,
               f"{n} approved card(s) in the feed" if n is not None else
               "`platform` is not a list — the consumer renders nothing"),
    ]


# ── lane 4 · brain expansion: is autonomy actually growing? ───────────
def _lane_brain(cur) -> list:
    out = []
    props = _one(cur, """SELECT COUNT(*) FROM brain_proposed_code_fixes
                          WHERE proposed_at >= NOW() - INTERVAL '7 days'""")
    finds = _one(cur, """SELECT COUNT(*) FROM brain_findings
                          WHERE created_at >= NOW() - INTERVAL '7 days'""")
    out.append(_check(
        "brain_thinking", "the brain is still finding and proposing",
        (int(finds or 0) > 0 and int(props or 0) > 0),
        f"{int(finds or 0)} finding(s) + {int(props or 0)} proposal(s) in 7d — "
        f"the brain IS thinking; this has never been the problem"))
    # ★The measured ceiling. Do not re-litigate this without re-running
    # GET /api/v1/brain/proposals/mechanical — the REAL blocked_by, not a proxy.
    merged7 = _one(cur, """SELECT COUNT(*) FROM brain_automerge_log
                            WHERE kind='merge'
                              AND merged_at >= NOW() - INTERVAL '7 days'""")
    out.append(_check(
        "brain_landing", "the brain LANDS fixes without a human", False,
        f"{int(merged7 or 0)} auto-merge(s) in 7d. The six mechanical transform "
        f"classes are EXHAUSTED — every instance found and fixed, last autofix "
        f"PR 2026-07-19. Measured 2026-07-30 against the REAL classifier: only "
        f"EIGHT proposals are blocked SOLELY by the class gate, so widening the "
        f"allowlist is NOT the lever (my own earlier proxy said 71 — it missed "
        f"the confidence and call-name gates). Autonomy is capped by DETECTOR "
        f"SUPPLY: new narrow, known-shape, high-frequency detectors of the kind "
        f"that gave now_text_cast 12 merged PRs. Adding a detector is additive "
        f"and safe; loosening the merge gate is neither.",
        critical=True))
    return out


# ── lane 5 · DC Hub Media: is it an ANALYST or a metronome? ───────────
# ★MEASURED 2026-07-30, and the headline finding is NOT repetition.
# 70 posts in 30d. Repetition is real but modest: 6 opening lines reused across
# 13 posts, 11 of 70 >70% similar to a nearby post (~16-19%).
# The number that matters: 2,761 total impressions across 70 posts —
# a MEDIAN OF 16 IMPRESSIONS PER POST, 36 total engagements in a month.
# Rewriting copy that reaches 16 people changes nothing. Distribution is the
# constraint, and "stop repeating yourself" is an answer to the wrong question.
# ★Also: 46 of 70 posts are post_type='manual'. The BRAIN drives a minority of
# output (auto_dcpi 14, auto_share 4, auto_mcp_adoption 2, auto_market_intel 1,
# auto_news 1), so "the brain should direct media" is not yet true by volume.
_MEDIA_MIN_MEDIAN_IMPRESSIONS = int(
    os.environ.get("WHITE_GLOVE_MEDIA_MIN_IMPRESSIONS", "100"))
_MEDIA_MAX_REPEAT_SHARE = float(
    os.environ.get("WHITE_GLOVE_MEDIA_MAX_REPEAT", "0.15"))


def _lane_media(cur) -> list:
    out = []
    rows = None
    try:
        cur.execute("""SELECT COALESCE(post_type,'?'), COALESCE(content,''),
                              COALESCE(impressions,0)
                         FROM linkedin_posts
                        WHERE COALESCE(status,'') = 'success'
                          AND posted_at >= NOW() - make_interval(days => %s)""",
                    (_DAYS,))
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("[wg-loop] media query failed: %s", str(e)[:140])
    if rows is None:
        return [_check("media_reach", "media output is measurable", None,
                       "linkedin_posts unreadable — UNMEASURED, not zero",
                       critical=False)]
    n = len(rows)
    if not n:
        return [_check("media_posting", "DC Hub Media is publishing", False,
                       f"0 successful posts in {_DAYS}d. ★status is 'success', "
                       f"NOT 'posted' — a wrong status literal reports a healthy "
                       f"channel as dead (hit this while writing the lane).",
                       critical=True)]
    imps = sorted(int(r[2] or 0) for r in rows)
    median = imps[len(imps) // 2]
    # repetition: how many posts share an opening line with another post
    firsts = {}
    for _, ct, _ in rows:
        k = (ct or "").strip().split("\n")[0][:55]
        firsts[k] = firsts.get(k, 0) + 1
    repeated = sum(v for v in firsts.values() if v > 1)
    rep_share = repeated / n
    brain_driven = sum(1 for r in rows if str(r[0] or "").startswith("auto_"))
    out.append(_check(
        "media_posting", "DC Hub Media is publishing", True,
        f"{n} successful post(s) in {_DAYS}d"))
    # ★REACH FIRST. This is deliberately ordered ahead of the repetition check:
    # if reach is this low, novelty is not the binding constraint and tuning
    # copy is motion without progress.
    out.append(_check(
        "media_reach", "posts actually REACH people",
        median >= _MEDIA_MIN_MEDIAN_IMPRESSIONS,
        f"median {median} impression(s)/post over {n} posts "
        f"(floor {_MEDIA_MIN_MEDIAN_IMPRESSIONS}). DISTRIBUTION is the "
        f"constraint — rewriting copy that reaches this few people changes "
        f"nothing, so fix reach BEFORE novelty.",
        critical=True))
    out.append(_check(
        "media_novelty", "posts are not repeating themselves",
        rep_share <= _MEDIA_MAX_REPEAT_SHARE,
        f"{repeated}/{n} post(s) share an opening line with another "
        f"({rep_share*100:.0f}%, ceiling {_MEDIA_MAX_REPEAT_SHARE*100:.0f}%)"))
    out.append(_check(
        "media_brain_driven", "the BRAIN drives media, not hand-written posts",
        brain_driven > (n / 2),
        f"{brain_driven}/{n} posts are brain-generated (post_type auto_*); "
        f"the rest are manual. 'The brain should direct media' is not yet true "
        f"by volume.", critical=True))
    return out


def _run_tick() -> dict:
    out = {"shell": "white-glove-loop", "n": 45, "window_days": _DAYS,
           "lanes": [], "note": (
               "Read-only. Covers the loop beyond the paying customer (that is "
               "shell #43): partners, agents, product information reaching its "
               "surfaces, and brain expansion. Red lanes name undone work.")}
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            lanes = [
                ("1 · new MCP partners — issued AND arrived",
                 _lane_partners(cur)),
                ("2 · AI agents — inflow vs retention", _lane_agents(cur)),
                ("3 · product information reaches its surfaces",
                 _lane_product()),
                ("4 · brain expansion — thinking vs LANDING",
                 _lane_brain(cur)),
                ("5 · DC Hub Media — analyst or metronome?",
                 _lane_media(cur)),
            ]
            for label, checks in lanes:
                out["lanes"].append({"lane": label, "checks": checks,
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


@white_glove_loop_master_shell_bp.route(
    "/api/v1/admin/white-glove-loop/master-tick", methods=["GET"])
def wg_loop_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_run_tick())


@white_glove_loop_master_shell_bp.route("/admin/white-glove-loop",
                                        methods=["GET"])
def wg_loop_dashboard():
    if _disabled():
        return Response("white-glove loop shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _run_tick()

    def chip(v):
        if v is True:
            return '<span style="color:#22c55e">PASS</span>'
        if v is False:
            return '<span style="color:#ef4444">FAIL</span>'
        return '<span style="color:#eab308">n/a</span>'

    rows = []
    for ln in p.get("lanes", []):
        rows.append(f'<h3>{ln["lane"]} — {chip(ln["pass"])}</h3><ul>')
        for ch in ln["checks"]:
            star = " ★" if ch.get("critical") else ""
            rows.append(f'<li>{chip(ch["pass"])}{star} <b>{ch["name"]}</b><br>'
                        f'<small>{ch["detail"]}</small></li>')
        rows.append("</ul>")
    return Response(
        "<html><body style='font-family:system-ui;background:#0b0b12;"
        "color:#e6e6f0;padding:24px;max-width:920px'>"
        "<h1>White-Glove Loop — Shell #45</h1>"
        f"<p><small>{p.get('note','')}</small></p>"
        f"<p>lanes passing {p.get('lanes_pass','?')}/{p.get('lanes_total','?')}"
        f" · window {p.get('window_days')}d</p>" + "".join(rows)
        + "</body></html>", mimetype="text/html")
