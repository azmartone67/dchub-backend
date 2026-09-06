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

# ── lane 7 · registry onboarding ────────────────────────────────────────────
# The registry loop lives in dchub-mcp-server: registry-discover.mjs crawls for
# curated MCP lists every Monday and refreshes ONE tracking issue; a scaffold PR
# is the bridge from "found" to "onboarded".
_REGISTRY_REPO = os.environ.get("WHITE_GLOVE_REGISTRY_REPO",
                                "azmartone67/dchub-mcp-server")
_REGISTRY_ISSUE_TITLE = "New MCP registry candidates"
_SCAFFOLD_BRANCH_PREFIX = "discover/add-target-"
# Discovery runs weekly; allow a missed Monday before calling the crawl dead.
_DISCOVERY_STALE_DAYS = int(os.environ.get("WHITE_GLOVE_DISCOVERY_STALE_DAYS", "10"))
# How long a discovered candidate may sit undisposed before it is a dropped lead.
_ONBOARD_SLA_DAYS = int(os.environ.get("WHITE_GLOVE_ONBOARD_SLA_DAYS", "21"))


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
        # ★NAME THE DORMANT ONES. A bare percentage is a fact nobody can act
        # on: "24% activated" tells an operator to feel bad, not who to call.
        # The whole point of this lane is that handing over a key is the START
        # of a relationship, so the actionable unit is the SPECIFIC partner who
        # was handed one and never arrived — with how long they have been
        # silent, because a key issued yesterday is not yet a failure.
        # Capped at 5 so the check detail stays inside _check's 430-char clamp;
        # the count says how many more there are.
        dormant = None
        try:
            cur.execute("""
                SELECT p.partner_slug, p.user_email, p.key_prefix,
                       EXTRACT(DAY FROM (NOW() - p.issued_at))::int AS days
                  FROM partner_keys_issued p
                 WHERE p.key_prefix IS NOT NULL
                   AND length(p.key_prefix) >= 12
                   AND NOT EXISTS (SELECT 1 FROM mcp_call_log t
                                    WHERE t.api_key LIKE p.key_prefix || '%%')
                 ORDER BY p.issued_at ASC
            """)
            dormant = cur.fetchall() or []
        except Exception as e:  # noqa: BLE001
            logger.debug("[wg-loop] dormant partner query failed: %s", str(e)[:140])
        if dormant:
            named = "; ".join(
                f"{(d[0] or '?')}/{(d[1] or '?')} ({int(d[3] or 0)}d)"
                for d in dormant[:5])
            more = f" +{len(dormant) - 5} more" if len(dormant) > 5 else ""
            who = f" DORMANT: {named}{more}."
        elif dormant is None:
            who = " (dormant list UNAVAILABLE — query failed, not 'none dormant')"
        else:
            who = ""
        out.append(_check(
            "partner_activated", "issued partner keys are actually CALLED",
            rate >= 0.25,
            f"{u} of {tot} issued partner key(s) have EVER made a call "
            f"({rate*100:.1f}%). Handing out a key is the START of the "
            f"relationship, not the win.{who}", critical=True))
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
    out = [
        _check("agent_inflow", "real external agents are arriving", n > 0,
               f"{n} distinct real external agent(s) in 7d "
               f"(synthetic/probe platforms excluded)"),
        # ★RETENTION IS THE LEAK, and it is the number the dashboards bury.
        # Inflow has never been the constraint.
        _check("agent_retention", "arriving agents COME BACK [IP basis]",
               share >= _RETENTION_FLOOR,
               f"{r}/{n} returning ({share*100:.1f}%, floor "
               f"{_RETENTION_FLOOR*100:.0f}%) by DISTINCT ip_address over 7d. "
               f"Inflow is not the constraint and never has been — a headline "
               f"agent count that rises while this stays flat is churn wearing "
               f"growth's clothes. ★This basis UNDERCOUNTS — see "
               f"agent_retention_keyed.",
               critical=True),
    ]

    # ★A SECOND BASIS, REPORTED SEPARATELY — NEVER BLENDED WITH THE FIRST.
    # The IP basis above is the only one mcp_tool_calls can support: that table
    # has NO api_key column at all (the same fact lane 1 documents for its own
    # join). But agents run from rotating cloud IPs — one Claude or ChatGPT
    # caller can present a different address every session — so DISTINCT
    # ip_address counts one returning agent as several new ones. The bias has a
    # KNOWN direction: it can only understate retention, never overstate it.
    #
    # mcp_call_log DOES carry api_key, so for identified callers we can ask the
    # real question: did this KEY come back? Better identity, smaller population
    # (keyless callers have no api_key at all).
    #
    # ★NOT AVERAGED, NOT SUBSTITUTED. House rule: every figure carries its
    # window and exclusion basis; never blend bases. Swapping the IP number for
    # the keyed one would make this lane greener without anything improving,
    # which is defeating the instrument. Both are shown. The keyed check is NOT
    # critical, because it speaks for a subset of the traffic.
    keyed_new = _one(cur, """SELECT COUNT(DISTINCT api_key) FROM mcp_call_log
                              WHERE timestamp >= NOW() - INTERVAL '7 days'
                                AND api_key IS NOT NULL AND api_key <> ''""")
    keyed_ret = _one(cur, """SELECT COUNT(DISTINCT api_key) FROM mcp_call_log
                              WHERE timestamp >= NOW() - INTERVAL '7 days'
                                AND api_key IS NOT NULL AND api_key <> ''
                                AND api_key IN (
                                    SELECT api_key FROM mcp_call_log
                                     WHERE timestamp < NOW() - INTERVAL '7 days'
                                       AND api_key IS NOT NULL AND api_key <> '')""")
    if keyed_new is None or keyed_ret is None:
        out.append(_check(
            "agent_retention_keyed", "returning agents [api_key basis]", None,
            "mcp_call_log unreadable — UNMEASURED, not zero. The IP figure above "
            "stands on its own; it is not promoted by this being missing."))
    elif int(keyed_new) == 0:
        out.append(_check(
            "agent_retention_keyed", "returning agents [api_key basis]", None,
            "no keyed callers in 7d — UNMEASURED on this basis. Reading 0% "
            "retention off a population of zero would be a false red."))
    else:
        kn, kr = int(keyed_new), int(keyed_ret)
        kshare = kr / kn
        out.append(_check(
            "agent_retention_keyed", "returning agents [api_key basis]",
            kshare >= _RETENTION_FLOOR,
            f"{kr}/{kn} keyed caller(s) returning ({kshare*100:.1f}%, floor "
            f"{_RETENTION_FLOOR*100:.0f}%) by DISTINCT api_key over 7d, from "
            f"mcp_call_log. IDENTIFIED CALLERS ONLY — keyless traffic carries no "
            f"api_key and is absent here, so this is a DIFFERENT POPULATION from "
            f"the IP figure above, not a correction to it. Where the two "
            f"disagree, the gap is the measurement error in the IP basis."))
    return out


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
    # ★2026-08-06 — this pass value was the LITERAL `False`, not a comparison.
    # merged7 was computed and then used ONLY in the message string, so the
    # check could never pass: had the brain auto-merged fifty fixes this week,
    # the lane would still have reported FAIL while its own detail text read
    # "50 auto-merge(s) in 7d". Being critical=True, it also pinned the entire
    # shell red permanently — a ratchet with no release.
    #
    # That was defensible as a standing indictment while the ceiling was
    # believed immovable. It stops being defensible the moment something is
    # actually done about detector supply (Phase 0 of the detector-supply
    # pipeline, #2245), because an instrument that cannot register success
    # cannot tell you whether the fix worked. The claim in the detail below is
    # still true; it is now a claim the DATA can retire.
    #
    # `merged7 is None` (missing table / failed query) stays FAIL rather than
    # becoming UNMEASURED: brain_automerge_log not being readable is itself a
    # reason not to believe the brain is landing anything.
    out.append(_check(
        "brain_landing", "the brain LANDS fixes without a human",
        (merged7 is not None and int(merged7) > 0),
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
                              COALESCE(impressions,0),
                              (engagement_fetched_at IS NOT NULL)
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
    # ★IS THE REACH NUMBER EVEN FRESH? Ask BEFORE judging reach on it.
    # linkedin_posts.impressions is not written at publish time — it is filled
    # in later by fetch_linkedin_impressions() (linkedin_poster.py:1099), which
    # calls LinkedIn's organizationalEntityShareStatistics and stamps
    # engagement_fetched_at. That fetch returns 401/403 and gives up when the
    # token is missing the r_organization_social scope, and its daily workflow
    # (linkedin-engagement-sync.yml) pipes the response straight to `head`
    # without checking the status — so a permanently no-opping sync stays green
    # and every post keeps whatever impressions it was seeded with.
    #
    # That makes "median 16 impressions" ambiguous: it is either a real
    # distribution problem or a stale-metric artifact, and those call for
    # opposite actions. Judging reach on unfetched numbers is the same
    # unmeasured-as-zero mistake this file keeps paying for — so if engagement
    # was never fetched for these posts, this reports UNMEASURED instead of a
    # confident red.
    fetched = None
    try:
        cur.execute("""SELECT COUNT(*) FROM linkedin_posts
                        WHERE COALESCE(status,'') = 'success'
                          AND posted_at >= NOW() - make_interval(days => %s)
                          AND engagement_fetched_at IS NOT NULL""", (_DAYS,))
        row = cur.fetchone()
        fetched = int(row[0]) if row else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[wg-loop] engagement freshness query failed: %s", str(e)[:140])
    # ★MEDIAN OVER FETCHED POSTS ONLY (2026-08-17). The old median mixed the
    # two populations: 41 of 89 posts had never had engagement fetched (the
    # sync has been a scope_or_auth_403 no-op since ~08-05) and sat at a
    # seeded 0, dragging the published median to 1 while the fetched half's
    # real median was 11.5. A reach number computed over unfetched rows
    # measures the SYNC, not the audience — split them or publish neither.
    imps = sorted(int(r[2] or 0) for r in rows if r[3])
    median = imps[len(imps) // 2] if imps else 0
    # repetition: how many posts share an opening line with another post
    firsts = {}
    for _, ct, _imp, _f in rows:
        k = (ct or "").strip().split("\n")[0][:55]
        firsts[k] = firsts.get(k, 0) + 1
    repeated = sum(v for v in firsts.values() if v > 1)
    rep_share = repeated / n
    brain_driven = sum(1 for r in rows if str(r[0] or "").startswith("auto_"))
    out.append(_check(
        "media_posting", "DC Hub Media is publishing", True,
        f"{n} successful post(s) in {_DAYS}d"))
    # ★FRESHNESS GATES REACH. If LinkedIn engagement was never fetched for any
    # of these posts, `impressions` is whatever it was seeded with and the
    # median measures the sync, not the audience. Report that instead of a
    # confident red — "distribution is broken" and "the stats fetch is broken"
    # demand opposite work, and only one of them is knowable from a stale
    # column. `fetched is None` (query failed / column absent) is also not
    # evidence of freshness.
    if fetched is None:
        out.append(_check(
            "media_reach", "posts actually REACH people", None,
            f"UNMEASURED — cannot read engagement_fetched_at, so it is unknown "
            f"whether the {n} post(s) ever had impressions fetched. The median "
            f"of {median} may be real reach or an unfetched column; those call "
            f"for opposite work.", critical=False))
    elif fetched == 0:
        out.append(_check(
            "media_reach", "posts actually REACH people", None,
            f"UNMEASURED — 0 of {n} post(s) in {_DAYS}d have engagement_fetched_at "
            f"set, so the median of {median} reflects an UNFETCHED column, not "
            f"the audience. fetch_linkedin_impressions() gives up on 401/403 "
            f"when the token lacks r_organization_social, and "
            f"linkedin-engagement-sync.yml pipes its response to `head` without "
            f"checking status — so a permanently no-opping sync stays green. "
            f"FIX THE SYNC BEFORE JUDGING REACH.", critical=True))
    else:
        # ★REACH FIRST. Deliberately ordered ahead of the repetition check: if
        # reach is this low, novelty is not the binding constraint and tuning
        # copy is motion without progress.
        unfetched = n - fetched
        stale_note = ""
        if unfetched > max(2, n // 4):
            # A PARTIAL fetch is its own defect: on 2026-08-16 every post
            # since 08-05 was unfetched because the LinkedIn token lost the
            # r_organization_social scope — the sync endpoint answered
            # ok:true / updated:0 / scope_or_auth_403 and the daily workflow
            # piped it to `head` and stayed green. Surface it beside the
            # median so "fix the sync" and "fix distribution" stay two
            # separate, correctly-ordered pieces of work.
            stale_note = (
                f" ★{unfetched} of {n} post(s) — the newest — have NO fetch: "
                f"verify POST /api/linkedin/engagement-sync (updated:0 with "
                f"scope_or_auth_403 = token lacks r_organization_social, "
                f"owner re-auth).")
        out.append(_check(
            "media_reach", "posts actually REACH people",
            median >= _MEDIA_MIN_MEDIAN_IMPRESSIONS,
            f"median {median} impression(s)/post over the {fetched} FETCHED "
            f"post(s) of {n} (floor {_MEDIA_MIN_MEDIAN_IMPRESSIONS}; "
            f"unfetched posts excluded — they measure the sync, not the "
            f"audience). DISTRIBUTION is the constraint — rewriting copy that "
            f"reaches this few people changes nothing, so fix reach BEFORE "
            f"novelty.{stale_note}",
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


# ── lane 6 · AI surface — do our published numbers agree? ─────────────
# ★WHY THIS LANE EXISTS. Measured 2026-08-06 against the LIVE MCP server: a
# single `why_dchub` response claimed 15,700+ facilities (edges), 15,000+
# (pitch) and 21,900+ (provenance_note), and both "82+ tools" and "40+ tools"
# — three facility numbers and two tool counts in ONE payload. Across surfaces
# it is wider: llms.txt (static, last touched 2026-06-25) says 15,000+, and
# REGISTRY_SUBMISSIONS.md — the copy handed to MCP registries — says 21,000+
# facilities, 29 tools and 4,000+ deals, the last of which ai_surface_canon
# itself documents as a debunked row-count over-claim corrected to 1,600+.
#
# ★AND BOTH FIXES WERE ALREADY BUILT AND NEVER RUN. ai_surface_sentinel audits
# every agent-facing surface against canon; white_glove_propagation pushes canon
# out to the MCP registries and its own docstring calls itself "the daily
# propagation job". Neither was scheduled — no workflow, no scheduler entry.
# The capability existed; the cadence never did. That is what this lane watches:
# not whether the code exists, but whether it RAN.
#
# ★NULL IS NOT ZERO. _one() returns None on a missing table or a failed query,
# which is indistinguishable from a real 0 if you coalesce. A lane that reads
# "never audited" as "0 drifts" reports perfect health for a surface nobody has
# ever checked — the precise failure this whole lane exists to catch. So None
# is carried through and named as "never ran".
_AI_SURFACE_MAX_AGE_H = int(
    os.environ.get("WHITE_GLOVE_AI_SURFACE_MAX_AGE_H", "48"))


# ── lane 7 · registry onboarding: found is not onboarded ──────────────
def _lane_registry_onboarding() -> list:
    """★THE 48-DAY LEAD NOBODY CALLED.

    Measured 2026-09-06. `registry-discover.mjs` had been running every Monday,
    exiting GREEN, and refreshing tracking issue #73 with the SAME five curated
    MCP lists since 2026-07-20 — 48 days. Zero were onboarded. The owner found
    out by asking, and the answer had to be re-derived by hand from a repo the
    other lanes never look at.

    Two distinct failures, and the shell saw neither:

      1. THE BRIDGE WAS WEDGED. The discover -> onboard step logged
         `contents PUT failed 409 — skip` and exited 0, every Monday. Root cause
         (sending main's blob sha against a diverged branch) was fixed 2026-09-01
         — the day AFTER the last scheduled run — so the fix sat unexecuted and
         unproven until a manual dispatch on 09-06 opened PR #359, the first
         scaffold in seven weeks.

      2. NOBODY DISPOSED THE PROPOSALS. Even with the bridge working, discovery
         only PROPOSES. A candidate sitting undisposed is a lead nobody called,
         which is exactly what lane 1 already asserts for partner keys — an
         issued key nobody used is indistinguishable from a won partner. Same
         shape, different noun.

    ★WHY THIS LANE READS ANOTHER REPO. The registry loop has its own cron, its
    own tracking issue and its own green check, in dchub-mcp-server. That is
    precisely why it could stall for seven weeks: nothing that the owner LOOKS
    AT was asking whether it had. A lane that only reads our own database would
    reproduce the blind spot it exists to close.

    Read-only, unauthenticated (the repo is public), fail-open to UNMEASURED —
    a lane that cannot check must never read as PASS.
    """
    import datetime as _dt

    def _age_days(iso: str):
        try:
            d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (_dt.datetime.now(_dt.timezone.utc) - d).days
        except Exception:  # noqa: BLE001
            return None

    try:
        import requests
        q = (f'repo:{_REGISTRY_REPO} in:title "{_REGISTRY_ISSUE_TITLE}" '
             f'state:open')
        r = requests.get("https://api.github.com/search/issues",
                         params={"q": q}, timeout=25,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
        pr = requests.get(
            f"https://api.github.com/repos/{_REGISTRY_REPO}/pulls",
            params={"state": "all", "per_page": 100}, timeout=25,
            headers={"Accept": "application/vnd.github+json"})
        pr.raise_for_status()
        pulls = pr.json() or []
    except Exception as e:  # noqa: BLE001
        return [_check("registry_loop", "the registry onboarding loop is visible",
                       None,
                       f"GitHub unreachable ({str(e)[:80]}) — UNMEASURED",
                       critical=True)]

    if not items:
        return [_check("registry_candidates",
                       "discovery has an open candidate ledger", True,
                       "no open candidate issue — nothing is waiting to be "
                       "onboarded, or discovery has not run since the last "
                       "triage")]

    issue = items[0]
    open_days = _age_days(issue.get("created_at") or "")
    since_refresh = _age_days(issue.get("updated_at") or "")
    scaffolds = [p for p in pulls
                 if str((p.get("head") or {}).get("ref") or "")
                 .startswith(_SCAFFOLD_BRANCH_PREFIX)]
    newest = max((_age_days(p.get("created_at") or "") for p in scaffolds
                  if _age_days(p.get("created_at") or "") is not None),
                 default=None)
    unmerged = [p for p in scaffolds if p.get("state") == "open"]

    return [
        _check("discovery_running",
               "the weekly crawl is still refreshing its ledger",
               (since_refresh is not None and since_refresh <= _DISCOVERY_STALE_DAYS),
               f"candidate issue #{issue.get('number')} last refreshed "
               f"{since_refresh}d ago (weekly cron; stale over "
               f"{_DISCOVERY_STALE_DAYS}d)"
               if since_refresh is not None else "refresh time unreadable",
               critical=True),
        _check("candidates_disposed",
               "no discovered registry has been left sitting past the SLA",
               (open_days is not None and open_days <= _ONBOARD_SLA_DAYS),
               f"candidate ledger #{issue.get('number')} open {open_days}d "
               f"(SLA {_ONBOARD_SLA_DAYS}d). Discovery PROPOSES; onboarding is "
               f"the undone half — this is the check that was missing while it "
               f"sat 48d with five candidates and zero onboarded"
               if open_days is not None else "issue age unreadable",
               critical=True),
        _check("onboarding_bridge",
               "discovery has actually produced an onboarding PR",
               (newest is not None and open_days is not None
                and newest <= open_days),
               (f"newest scaffold PR is {newest}d old; "
                f"{len(unmerged)} still open, {len(scaffolds)} total"
                if newest is not None else
                "NO scaffold PR has ever been opened — candidates are "
                "accumulating with no bridge to onboarding, the shape the "
                "409 wedge produced for seven green Mondays"),
               critical=True),
    ]


def _lane_ai_surface(cur) -> list:
    out = []

    # — the internal leg: do OUR OWN surfaces agree with canon? —
    audit_age = _one(cur, """SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))/3600
                               FROM ai_surface_audits""")
    if audit_age is None:
        out.append(_check(
            "ai_surface_audited", "our agent-facing surfaces are checked on a cadence",
            False,
            "NEVER — no ai_surface_audits row exists. ai_surface_sentinel is "
            "registered (main.py) and audits every agent-facing surface against "
            "ai_surface_canon, but nothing scheduled it and until 2026-08-06 it "
            "persisted nothing, so no run could be proven either way. This is "
            "'never ran', NOT 'ran and found nothing'.",
            critical=True))
    else:
        fresh = float(audit_age) <= _AI_SURFACE_MAX_AGE_H
        out.append(_check(
            "ai_surface_audited", "our agent-facing surfaces are checked on a cadence",
            fresh,
            f"last audit {float(audit_age):.1f}h ago "
            f"(ceiling {_AI_SURFACE_MAX_AGE_H}h)"))
        major = _one(cur, """SELECT major_drift FROM ai_surface_audits
                              ORDER BY created_at DESC LIMIT 1""")
        drifts = _one(cur, """SELECT total_drifts FROM ai_surface_audits
                               ORDER BY created_at DESC LIMIT 1""")
        out.append(_check(
            "ai_surface_agrees", "every surface matches canon",
            (major is not None and int(major) == 0),
            f"{int(major or 0)} surface(s) in MAJOR drift, {int(drifts or 0)} "
            f"drifted field(s) at the last audit. Contradictory self-reported "
            f"numbers are worse for an agent than smaller consistent ones — "
            f"citability is the entire pitch.",
            critical=True))

    # — the outward leg: do our MCP/AI PARTNERS have the current numbers? —
    # ★DRY RUNS DO NOT COUNT, and the first version of this lane let them.
    # It read MAX(created_at) across ALL of white_glove_runs, and a dry run
    # writes a row too. Anything probing propagation on a schedule — the
    # ai-surface-partner-sync workflow did exactly this daily — would stamp a
    # fresh row every day and hold `partners_told` green while the REAL
    # propagation had not run in a week. That is a false green in the precise
    # check built to catch a propagation outage, and #2283 measured that real
    # runs miss roughly four days in seven, so the mask had something to hide.
    #
    # `dry_run IS NOT TRUE` (not `= false`) so a NULL dry_run — rows written
    # before the column was populated — still counts as a real run rather than
    # vanishing from the freshness window.
    #
    # This deliberately matches white_glove_propagation.ran_today(), which asks
    # the same question for the scheduler's catch-up guard. The two must agree:
    # if they disagree the shell can call a day covered that the runner is about
    # to re-cover, or vice versa.
    prop_age = _one(cur, """SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))/3600
                              FROM white_glove_runs
                             WHERE dry_run IS NOT TRUE""")
    if prop_age is None:
        out.append(_check(
            "partners_told", "MCP/AI partner listings get the canonical numbers",
            False,
            "NEVER — no non-dry white_glove_runs row exists. "
            "routes/white_glove_propagation.py probes every registry in "
            "mcp_presence_crawler.SEED_REGISTRIES for stale copy and auto-resubmits "
            "where a path exists. A dry run does NOT count here: it probes and "
            "reports but submits nothing, so it moves no listing.",
            critical=True))
    else:
        out.append(_check(
            "partners_told", "MCP/AI partner listings get the canonical numbers",
            float(prop_age) <= _AI_SURFACE_MAX_AGE_H,
            f"last REAL (non-dry) propagation run {float(prop_age):.1f}h ago "
            f"(ceiling {_AI_SURFACE_MAX_AGE_H}h). crawler_scheduler slot 20 runs "
            f"this daily with a 23:00 catch-up (#2283); a gap here means both "
            f"slots missed, not that the job is unscheduled."))
        drifted = _one(cur, """SELECT drifted FROM white_glove_runs
                                WHERE dry_run IS NOT TRUE
                                ORDER BY created_at DESC LIMIT 1""")
        checked = _one(cur, """SELECT checked FROM white_glove_runs
                                WHERE dry_run IS NOT TRUE
                                ORDER BY created_at DESC LIMIT 1""")
        out.append(_check(
            "partner_listings_clean", "no registry is advertising stale numbers",
            (drifted is not None and int(drifted) == 0),
            f"{int(drifted or 0)} of {int(checked or 0)} probed listing(s) carry "
            f"numbers that disagree with canon. Under-selling is as costly as "
            f"over-claiming: the registry packet's 29 tools against a real 82 "
            f"hides two-thirds of the catalog from every agent reading it.",
            critical=True))
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
                ("6 · AI surface — do our published numbers agree, "
                 "and do partners know?", _lane_ai_surface(cur)),
                ("7 · registry onboarding — found is not onboarded",
                 _lane_registry_onboarding()),
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
