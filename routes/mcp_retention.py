"""DC Hub — retention endpoint (r86-reach, 2026-06-14).

STANDALONE drop-in so it survives the parallel session's churn of mcp_funnel.py.
Built + verified live this session (returned 7.4% reuse / 0.62 calls/key / 29 new-1 returning),
then reverted by a concurrent perf-refactor session. To restore durably:

  1. Save this file as routes/mcp_retention.py
  2. In main.py, near the other blueprint registrations, add:
        try:
            from routes.mcp_retention import mcp_retention_bp
            app.register_blueprint(mcp_retention_bp)
        except Exception as _e:
            logging.getLogger(__name__).warning('mcp_retention wiring failed: %s', _e)
  3. railway up  (coordinate so it doesn't race the other session)
  4. Verify: curl .../api/v1/mcp/retention?weeks=8

This file touches NO contested files, so the other session won't revert it.
"""
from __future__ import annotations
import os
from flask import Blueprint, jsonify, request
import psycopg2, psycopg2.extras

mcp_retention_bp = Blueprint("mcp_retention_r86", __name__)
_INTERNAL = r"(loop|dchub-|selfheal|probe|health|scanner|regression|mcp-test|sweep|clawith|anthropicapi)"


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


@mcp_retention_bp.route("/api/v1/mcp/retention", methods=["GET"])
def mcp_retention():
    try:
        weeks = max(1, min(26, int(request.args.get("weeks") or 11)))
    except ValueError:
        weeks = 11
    c = _conn()
    if c is None:
        return jsonify(error="no_db"), 503
    out = {"weeks": weeks, "ip_cohort": [], "key_reuse": [], "summary": {},
           "primary_metric": "summary.pct_returned_next_week_mature (durable api_key, mature 8-30d cohort)",
           "note": ("Retention is the lever, not reach. ⚠️ ip_cohort is NOT the retention truth — it counts "
                    "mcp_tool_calls.ip_address, which is UNRELIABLE IN BOTH DIRECTIONS: TODAY it is the client's "
                    "ROTATING egress IP, so the same agent reads as NEW every week (UNDERCOUNTS returns); "
                    "PRE-2026-06-14 it was the CF/Node PROXY IP, which folded many distinct agents onto one IP "
                    "that read as a single 'returning' caller (OVERCOUNTS — that is the fake Apr '25-32 returning/"
                    "wk'). The honest, identity-based signal is key_reuse on the durable api_key: returned_next_week "
                    "= a key first used in a LATER ISO week than minted = a true cross-session return. Read the "
                    "mature RATE (pct_returned_next_week_mature — computed over keys minted 8-30d ago so each has had "
                    "a full week to return), NOT the right-censored latest week. CAVEAT: it is a FLOOR, not exact — "
                    "keyed on (client-IP-hash, UA) reuse, it can slightly OVER-count distinct agents sharing one NAT "
                    "egress + identical UA onto one key, and still MISSES rotating-IP web hosts (their durable path is "
                    "email-bind, not key reuse). Mint VOLUME is scan/anon-inflated.")}
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH ext AS (
                  SELECT ip_address, date_trunc('week', created_at) AS wk,
                         MIN(date_trunc('week', created_at)) OVER (PARTITION BY ip_address) AS first_wk
                  FROM mcp_tool_calls
                  WHERE created_at >= now() - (%s || ' weeks')::interval
                    AND ip_address IS NOT NULL AND ip_address <> ''
                    AND COALESCE(client_name,'') !~* %s AND COALESCE(platform,'') !~* %s )
                SELECT wk::date AS week, COUNT(DISTINCT ip_address) AS distinct_ips,
                       COUNT(DISTINCT ip_address) FILTER (WHERE wk = first_wk) AS new_ips,
                       COUNT(DISTINCT ip_address) FILTER (WHERE wk > first_wk) AS returning_ips
                FROM ext GROUP BY wk ORDER BY wk
            """, (weeks, _INTERNAL, _INTERNAL))
            out["ip_cohort"] = [dict(r) for r in cur.fetchall()]
            # ── canonical cohort (2026-08-03) ────────────────────────────
            # ★THE 246-vs-76 GAP. ip_cohort above counts raw ip_address with
            # only an internal-name/platform filter, so it counts Cloudflare
            # POP first-hops, non-public IPs and registry crawlers as new
            # agents. On 2026-08-03 it reported 246 new external IPs for
            # wk 2026-07-27 while the headline canonical count for the rolling
            # 7d was 76 — the SAME dashboard, two populations differing ~3x.
            #
            # That is not a cosmetic split: "inflow is fine, retention is the
            # leak" is computed as returning/new, and a denominator inflated by
            # crawlers makes retention look broken whether or not it is. Nobody
            # can tell from ip_cohort alone.
            #
            # So compute the SAME cohort at the canonical grain
            # (mcp_calls_identity.agent_id, the view's own POP/internal/crawler
            # exclusions) and return it BESIDE the legacy series rather than in
            # place of it — the two together are the evidence; either alone is
            # an assertion. Fail-soft: a missing view leaves agent_cohort empty
            # and ip_cohort untouched, never breaking the page.
            try:
                cur.execute("""
                    WITH ext AS (
                      SELECT agent_id, date_trunc('week', created_at) AS wk,
                             MIN(date_trunc('week', created_at))
                               OVER (PARTITION BY agent_id) AS first_wk
                      FROM mcp_calls_identity
                      WHERE created_at >= now() - (%s || ' weeks')::interval
                        AND agent_id IS NOT NULL AND agent_id <> ''
                        AND is_public_ip AND is_real_external )
                    SELECT wk::date AS week,
                           COUNT(DISTINCT agent_id) AS distinct_agents,
                           COUNT(DISTINCT agent_id)
                             FILTER (WHERE wk = first_wk) AS new_agents,
                           COUNT(DISTINCT agent_id)
                             FILTER (WHERE wk > first_wk) AS returning_agents
                    FROM ext GROUP BY wk ORDER BY wk
                """, (weeks,))
                out["agent_cohort"] = [dict(r) for r in cur.fetchall()]
                out["agent_cohort_note"] = (
                    "CANONICAL GRAIN (mcp_calls_identity.agent_id) — the same "
                    "population as the headline agent count and /ai/reach. "
                    "ip_cohort beside it counts raw ip_address and includes "
                    "Cloudflare POPs and crawlers; where the two disagree, "
                    "THIS one is the population you sell to. Retention read "
                    "off ip_cohort is computed on an inflated denominator.")
            except Exception as _e:
                out["agent_cohort"] = []
                out["agent_cohort_note"] = (
                    f"UNMEASURED ({str(_e)[:80]}) — mcp_calls_identity "
                    f"unavailable. NOT zero: read ip_cohort with its "
                    f"inflation caveat rather than treating this as 'no "
                    f"returning agents'.")
                try:
                    c.rollback()
                except Exception:
                    pass
            cur.execute("""
                SELECT date_trunc('week', minted_at)::date AS week, COUNT(*) AS minted,
                       COUNT(*) FILTER (WHERE call_count > 1) AS reused_2plus,
                       COUNT(*) FILTER (WHERE last_used_at IS NOT NULL
                                AND last_used_at > minted_at + interval '1 hour') AS returned_later,
                       COUNT(*) FILTER (WHERE last_used_at IS NOT NULL
                                AND date_trunc('week', last_used_at) > date_trunc('week', minted_at)) AS returned_next_week,
                       COUNT(DISTINCT request_ip_hash) AS distinct_ips
                FROM auto_trial_keys WHERE minted_at >= now() - (%s || ' weeks')::interval
                GROUP BY week ORDER BY week
            """, (weeks,))
            out["key_reuse"] = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT COUNT(*) AS minted_30d,
                       ROUND(100.0*COUNT(*) FILTER (WHERE call_count > 1)/NULLIF(COUNT(*),0),1) AS pct_reused_30d,
                       ROUND(AVG(call_count),2) AS avg_calls_per_key_30d,
                       -- r-retention fix (2026-06-19): the cross-session-return RATE is computed ONLY
                       -- over a MATURE cohort (keys minted 8-30d ago) so every key has had a full
                       -- subsequent ISO week in which to return. Including the last ~week would
                       -- right-censor it downward into a fake decline (the 'partial period = cliff'
                       -- trap r86b already fixed for ip_cohort). Volume (minted_30d) stays full-30d.
                       COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days') AS mature_cohort_30d,
                       COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days' AND last_used_at IS NOT NULL
                                AND date_trunc('week', last_used_at) > date_trunc('week', minted_at)) AS returned_next_week_mature,
                       ROUND(100.0*COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days' AND last_used_at IS NOT NULL
                                AND date_trunc('week', last_used_at) > date_trunc('week', minted_at))
                             /NULLIF(COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days'),0),1) AS pct_returned_next_week_mature
                FROM auto_trial_keys WHERE minted_at >= now() - interval '30 days'
            """)
            row = cur.fetchone()
            out["summary"] = dict(row) if row else {}
            # r86b (2026-06-14): NEVER let the in-progress current week read as a
            # "decline". date_trunc('week', now()) = Monday of the current ISO week;
            # any cohort/reuse row with week >= that is a PARTIAL week (often just a
            # handful of UTC-edge calls → 0 new / ~0 returning) and was making the
            # headline KPI + the last table row look like a cliff. Split it out:
            # the trend arrays + latest_* headline use only COMPLETE weeks; the
            # partial week is surfaced separately under current_partial_* so nothing
            # is hidden, just not mistaken for a finished data point.
            cur.execute("SELECT date_trunc('week', now())::date AS cur_wk")
            cur_wk = cur.fetchone()["cur_wk"]
            partial_ip = [r for r in out["ip_cohort"] if r["week"] >= cur_wk]
            out["ip_cohort"] = [r for r in out["ip_cohort"] if r["week"] < cur_wk]
            out["key_reuse"] = [r for r in out["key_reuse"] if r["week"] < cur_wk]
            # ★★★ 2026-08-18: r86b above has split the partial week out of
            # ip_cohort and key_reuse since 06-14 — but agent_cohort was added
            # LATER (08-15) and never got it, so the series the note calls
            # "CANONICAL GRAIN … the population you sell to" was the ONE series
            # still ending on a right-censored week. Measured live this morning:
            # agent_cohort ended 2026-08-17 with 4 returning / 8 distinct while
            # the last COMPLETE week (08-10) held 8 returning / 72 distinct. Any
            # consumer reading the last row got a 50% "decline" that is just
            # Monday. Exactly the cliff this block's own comment describes, left
            # open on the metric everything else is told to trust — and the same
            # class as the reach-series partial week fixed in #2834.
            partial_agent = [r for r in out["agent_cohort"] if r["week"] >= cur_wk]
            out["agent_cohort"] = [r for r in out["agent_cohort"]
                                   if r["week"] < cur_wk]
            if out["agent_cohort"]:
                la = out["agent_cohort"][-1]
                # The honest headline. Dashboards rendered latest_returning_ips
                # (92 for wk 08-10) labelled "Returning IPs"; the same week at
                # canonical grain is 8. Publish the agent figure so a tile does
                # not have to reach into ip_cohort to find a number to show.
                out["summary"].update(
                    latest_complete_week_agents=str(la["week"]),
                    latest_distinct_agents=la["distinct_agents"],
                    latest_new_agents=la["new_agents"],
                    latest_returning_agents=la["returning_agents"])
            if partial_agent:
                pa = partial_agent[-1]
                out["summary"].update(
                    current_partial_week_agents=str(pa["week"]),
                    current_partial_new_agents=pa["new_agents"],
                    current_partial_returning_agents=pa["returning_agents"])
            if out["ip_cohort"]:
                last = out["ip_cohort"][-1]
                out["summary"].update(latest_week=str(last["week"]),
                                      latest_new_ips=last["new_ips"],
                                      latest_returning_ips=last["returning_ips"],
                                      latest_complete_week=str(last["week"]))
            if partial_ip:
                pw = partial_ip[-1]
                out["summary"].update(current_partial_week=str(pw["week"]),
                                      current_partial_new_ips=pw["new_ips"],
                                      current_partial_returning_ips=pw["returning_ips"])
            else:
                out["summary"]["current_partial_week"] = str(cur_wk)

            # ── identity_breakdown (2026-06-22): WHO returns, by identity DURABILITY ──
            # The return loop's diagnostic split. email_bound + oauth_durable survive
            # across sessions; key_only returns ONLY if the client resends the X-API-Key
            # (header-less hosts — Claude.ai web / ChatGPT — drop it → structurally CANNOT
            # return until they bind email or OAuth). Additive + each query self-isolated:
            # a failure here can never break the headline metric above.
            ib = {"note": ("Returns split by identity durability. email_bound + oauth_durable are "
                           "cross-session durable; key_only depends on the client resending the key. "
                           "Validates whether the durable-identity levers (email-bind, WorkOS OAuth) "
                           "actually lift the 0.6% return rate.")}
            try:
                # email-bound vs key-only return rate, same mature 8-30d cohort as summary.
                cur.execute("""
                    SELECT CASE WHEN operator_email IS NOT NULL AND operator_email <> ''
                                THEN 'email_bound' ELSE 'key_only' END AS cohort,
                           COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days') AS mature,
                           COUNT(*) FILTER (WHERE minted_at < now() - interval '7 days'
                                    AND last_used_at IS NOT NULL
                                    AND date_trunc('week', last_used_at) > date_trunc('week', minted_at)
                                   ) AS returned_mature
                    FROM auto_trial_keys
                    WHERE minted_at >= now() - interval '30 days'
                    GROUP BY 1
                """)
                for r in cur.fetchall():
                    m = int(r["mature"] or 0); rt = int(r["returned_mature"] or 0)
                    ib[r["cohort"]] = {"mature_cohort": m, "returned_next_week_mature": rt,
                                       "pct_returned": round(100.0 * rt / m, 1) if m else None}
            except Exception:
                ib["trial_breakdown_error"] = True
            try:
                # OAuth-durable cohort — now MEASURABLE (2026-06-22): last_used_at is stamped on
                # /oauth/identity resolve + /keys/validate, so created_at (first connect) vs
                # last_used_at (latest use) gives a true cross-session return. dch_oauth_ keys live
                # in mcp_dev_keys (NOT auto_trial_keys). No params arg ⇒ the literal % in LIKE is safe.
                cur.execute("""
                    SELECT COUNT(*) AS identities,
                           COUNT(*) FILTER (WHERE email IS NOT NULL AND email <> '') AS with_email,
                           COUNT(*) FILTER (WHERE created_at < now() - interval '7 days') AS mature,
                           COUNT(*) FILTER (WHERE created_at < now() - interval '7 days'
                                    AND last_used_at IS NOT NULL
                                    AND date_trunc('week', last_used_at) > date_trunc('week', created_at)
                                   ) AS returned_mature
                    FROM mcp_dev_keys WHERE api_key LIKE 'dch_oauth_%'
                """)
                o = cur.fetchone() or {}
                m = int(o.get("mature") or 0); rt = int(o.get("returned_mature") or 0)
                ib["oauth_durable"] = {
                    "identities": int(o.get("identities") or 0),
                    "with_email": int(o.get("with_email") or 0),
                    "mature_cohort": m, "returned_next_week_mature": rt,
                    "pct_returned": round(100.0 * rt / m, 1) if m else None,
                    "measurable": True,
                    "note": ("MEASURABLE via created_at vs last_used_at (stamped on /oauth/identity + "
                             "/keys/validate). pct_returned is null until the cohort matures 8d+ — "
                             "watch it as OAuth adoption grows; this is the durable-identity payoff signal."),
                }
            except Exception:
                ib["oauth_error"] = True
            try:
                # free_key_durable cohort (2026-07-03): the dch_live_ keys claim_free_key
                # mints into mcp_dev_keys (metadata.source='claim_api') were counted
                # NOWHERE — the headline summary reads auto_trial_keys only, and
                # oauth_durable filters dch_oauth_. So reuse of the exact key we tell
                # agents to SAVE was invisible to every retention number.
                # ★ VERIFIED LIVE 2026-07-03: adding it does NOT inflate the rate (the
                # earlier "artifact hides a higher number" hypothesis is DISPROVEN) —
                # dch_live_ shows ~34% intra-window reuse (used_again_any) but ~0%
                # CROSS-week return, so the honest combined number (below) is ~the same
                # (1.2% vs the trial-only 1.3%). The value is HONEST VISIBILITY: the
                # intra-vs-cross-week split is the sharpest evidence that the break is
                # cross-SESSION durable identity (header-less web clients), which the
                # WorkOS OAuth lever targets — not intra-session usability.
                cur.execute("""
                    SELECT COUNT(*) AS identities,
                           COUNT(*) FILTER (WHERE last_used_at IS NOT NULL) AS used_again_any,
                           COUNT(*) FILTER (WHERE created_at < now() - interval '7 days') AS mature,
                           COUNT(*) FILTER (WHERE created_at < now() - interval '7 days'
                                    AND last_used_at IS NOT NULL
                                    AND date_trunc('week', last_used_at) > date_trunc('week', created_at)
                                   ) AS returned_mature
                    FROM mcp_dev_keys
                    WHERE api_key LIKE 'dch_live_%' AND created_at >= now() - interval '30 days'
                """)
                f = cur.fetchone() or {}
                m = int(f.get("mature") or 0); rt = int(f.get("returned_mature") or 0)
                idn = int(f.get("identities") or 0); ua = int(f.get("used_again_any") or 0)
                ib["free_key_durable"] = {
                    "identities": idn,
                    "used_again_any_time": ua,
                    "pct_used_again_any_time": round(100.0 * ua / idn, 1) if idn else None,
                    "mature_cohort": m, "returned_next_week_mature": rt,
                    "pct_returned": round(100.0 * rt / m, 1) if m else None,
                    "note": ("dch_live_ free keys from claim_free_key (mcp_dev_keys). "
                             "used_again_any_time = reused at all (often same-week / same "
                             "session); pct_returned = CROSS-week return — the true "
                             "durable-identity signal. A high used-again but ~0 cross-week "
                             "return = agents DO get value but can't persist identity across "
                             "sessions (header-less hosts): the WorkOS-OAuth lever's target."),
                }
            except Exception:
                ib["free_key_error"] = True
            try:
                # Honest COMBINED cross-session return across ALL durable free-tier keys
                # (auto_trial_keys dch_trial_ + mcp_dev_keys dch_live_). The existing
                # pct_returned_next_week_mature is trial-only; this is the whole-cohort
                # truth. Preserved as an ADDITIVE field so the tracked trial-only number
                # keeps its meaning. anchor = mint/first-connect; same 8-30d mature window.
                cur.execute("""
                    WITH durable AS (
                        SELECT minted_at AS anchor, last_used_at
                          FROM auto_trial_keys WHERE minted_at >= now() - interval '30 days'
                        UNION ALL
                        SELECT created_at AS anchor, last_used_at
                          FROM mcp_dev_keys
                         WHERE api_key LIKE 'dch_live_%' AND created_at >= now() - interval '30 days'
                    )
                    SELECT COUNT(*) FILTER (WHERE anchor < now() - interval '7 days') AS mature,
                           COUNT(*) FILTER (WHERE anchor < now() - interval '7 days'
                                    AND last_used_at IS NOT NULL
                                    AND date_trunc('week', last_used_at) > date_trunc('week', anchor)
                                   ) AS returned
                    FROM durable
                """)
                d = cur.fetchone() or {}
                dm = int(d.get("mature") or 0); dr = int(d.get("returned") or 0)
                out["summary"]["mature_cohort_all_durable_30d"] = dm
                out["summary"]["returned_next_week_all_durable"] = dr
                out["summary"]["pct_returned_next_week_all_durable"] = (
                    round(100.0 * dr / dm, 1) if dm else None)
            except Exception:
                pass

            # ── r-oauth-funnel (2026-07-16): the CHALLENGE side of the funnel ──
            # The engines can see WHO returns but not how many agents were ever
            # ASKED to sign in. The MCP gateway tallies 401 challenges in-process
            # and flushes additive deltas every 60s; this publishes them.
            #
            # ★ These are EVENTS, not people. An unconverted caller is
            # re-challenged on every initialize and every tools/call, so this can
            # NEVER be divided by identities to get a conversion rate — numerator
            # (deduped identities) and denominator (recurring events) are
            # different populations. The ratio ships as an INDEX named
            # ..._challenges_per_new_identity_30d (lower = better) so it cannot
            # be quoted as a percentage. NO key here ends in _pct, deliberately.
            #
            # Self-isolated: additive only, its own try/except, so a failure here
            # can never touch the headline retention numbers the engines score.
            # NO SAVEPOINT — this conn is autocommit=True (see the connect call
            # above), which already isolates statement failures; SAVEPOINT would
            # raise on every call and yield zeros forever.
            try:
                cur.execute("""
                    SELECT
                      COALESCE(SUM(n) FILTER (WHERE kind = 'claude_connector'
                                                AND method = 'initialize'), 0) AS connector_init,
                      COALESCE(SUM(n) FILTER (WHERE kind = 'claude_connector'
                                                AND method = 'tools/call'), 0) AS connector_call,
                      COALESCE(SUM(n) FILTER (WHERE kind = 'claude_connector'), 0) AS connector_all,
                      COALESCE(SUM(n) FILTER (WHERE kind = 'invalid_bearer'), 0)   AS bearer_all,
                      -- QA sweep F2 (2026-09-02): the MIDDLE of the on-ramp. The
                      -- gateway emits oauth_authorize_started when a challenged
                      -- client actually reaches /oauth/authorize, so the
                      -- 1,111:1 challenge->identity loss can be placed.
                      COALESCE(SUM(n) FILTER (WHERE kind = 'oauth_authorize_started'), 0) AS authorize_started,
                      -- r-claude-passive-arrivals (2026-08-28): ARRIVALS, not challenges.
                      -- *_seen kinds fire on THEIR arrival and issue no 401; the
                      -- claude_connector rows above fire on OURS. Never divide one
                      -- into the other -- different populations, see the note below.
                      COALESCE(SUM(n) FILTER (WHERE kind = 'claude_connector_seen'
                                                AND method = 'initialize'), 0) AS cl_seen_init,
                      COALESCE(SUM(n) FILTER (WHERE kind = 'claude_connector_seen'), 0) AS cl_seen_all,
                      COALESCE(SUM(n) FILTER (WHERE kind = 'chatgpt_connector_seen'), 0) AS cg_seen_all,
                      COALESCE(SUM(n) FILTER (WHERE kind = '_beat'), 0)            AS beats,
                      MAX(last_at) FILTER (WHERE kind = '_beat')                   AS last_flush_at
                    FROM mcp_oauth_challenges
                    WHERE day >= ((NOW() AT TIME ZONE 'UTC')::date - 30)
                """)
                ch = cur.fetchone() or {}
                ci = int(ch.get("connector_init") or 0)
                beats = int(ch.get("beats") or 0)
                # Numerator queried HERE, in THIS try — never read back out of
                # ib["oauth_durable"], whose own except can leave it absent and
                # would silently fabricate a 0.
                # No params arg => no %-substitution => the literal % is safe.
                cur.execute("""
                    SELECT COUNT(*) AS new_ids FROM mcp_dev_keys
                    WHERE api_key LIKE 'dch_oauth_%'
                      AND created_at >= NOW() - interval '30 days'
                """)
                new_ids = int((cur.fetchone() or {}).get("new_ids") or 0)

                cc = int(ch.get("connector_call") or 0)
                ca = int(ch.get("connector_all") or 0)
                out["summary"]["oauth_challenges_connector_init_30d"] = ci
                # ★r-challenge-after-value (2026-08-15) — THE METHOD MOVED. The
                # gateway now challenges the Claude connector on tools/call and
                # NEVER on initialize (server.mjs _claudeChallengeEligible), so
                # connector_init decays to 0 over 30d BY DESIGN. It is kept as a
                # published key because three engines read `summary` and a
                # disappearing key reads as a broken feed — but on its own it is
                # now a RETIRED series, and the index built on it below will go
                # None. Read connector_call, or the all-method index.
                out["summary"]["oauth_challenges_connector_call_30d"] = cc
                out["summary"]["oauth_challenges_all_30d"] = ca + int(ch.get("bearer_all") or 0)
                out["summary"]["oauth_new_identities_30d"] = new_ids
                # Lower = better. None when there is nothing to divide, or when
                # the gateway has never checked in (beats == 0 => DORMANT, which
                # is NOT the same as "zero challenges").
                out["summary"]["oauth_connector_init_challenges_per_new_identity_30d"] = (
                    round(float(ci) / new_ids, 1) if (new_ids and ci and beats) else None)
                # ★The index that SURVIVES the method switch: every challenge the
                # Claude connector was issued, whatever method carried it. This is
                # the one to trend from 2026-08-15 onward — the init-only index
                # above cannot be compared across the change, and comparing it
                # anyway would read the fix as a 100% improvement when all that
                # happened is the counter moved to a different row.
                out["summary"]["oauth_connector_challenges_per_new_identity_30d"] = (
                    round(float(ca) / new_ids, 1) if (new_ids and ca and beats) else None)
                out["summary"]["oauth_funnel_gateway_reporting"] = bool(beats)
                _lf = ch.get("last_flush_at")
                out["summary"]["oauth_funnel_last_flush_at"] = (
                    _lf.isoformat() if hasattr(_lf, "isoformat") else None)
                ib["challenge_side"] = {
                    "connector_init_30d": ci,
                    "connector_call_30d": cc,
                    "connector_all_30d": ca,
                    "invalid_bearer_30d": int(ch.get("bearer_all") or 0),
                    "new_identities_30d": new_ids,
                    # ★QA sweep F2 (2026-09-02): challenge_issued -> authorize_
                    # started -> identity_created. Before this only the two ENDS
                    # were measured (connector_* and new_identities_30d); the
                    # middle names WHERE the loss is: a low authorize_started
                    # against a high connector_call means the 401 never turns
                    # into a browser hop; a high one against low new_identities
                    # means the WorkOS page loses them.
                    "authorize_started_30d": int(ch.get("authorize_started") or 0),
                    "authorize_started_instrumented_since": "2026-09-02",
                    "authorize_started_note": (
                        "★READ authorize_started_instrumented_since BEFORE trending. The "
                        "counter did not exist before 2026-09-02 (backend whitelist + "
                        "gateway emit shipped together), so every earlier day reads 0 "
                        "because there was NO INSTRUMENT. Events, not people: one client "
                        "can start authorize several times. Same population rule as the "
                        "rest of this block: never divide it into arrivals_*."
                    ),
                    "gateway_reporting": bool(beats),
                    "method_switched_at": "2026-08-15",
                    # ★ARRIVAL side (r-claude-passive-arrivals, 2026-08-28). Passive:
                    # counts connectors that SHOWED UP, whether or not we challenged
                    # them. This is the series that answers "did Claude traffic
                    # change?" -- the challenge counters above cannot, because they
                    # move when WE change the trigger.
                    "arrivals_claude_30d": int(ch.get("cl_seen_all") or 0),
                    "arrivals_claude_init_30d": int(ch.get("cl_seen_init") or 0),
                    "arrivals_chatgpt_30d": int(ch.get("cg_seen_all") or 0),
                    "arrivals_instrumented_since": "2026-08-28",
                    "arrivals_note": (
                        "★READ arrivals_instrumented_since BEFORE trending these. The Claude "
                        "arrival counter did not exist before 2026-08-28, so every earlier day "
                        "reads 0 because there was NO INSTRUMENT -- not because nobody arrived. "
                        "Trending across that boundary reproduces exactly the error this counter "
                        "was built to end: on 2026-08-15 the challenge trigger narrowed, "
                        "claude_connector fell ~159/day to ~0 BY DESIGN, and three separate "
                        "passes read our own restraint as the cohort dying. "
                        "★arrivals_* and connector_* are DIFFERENT POPULATIONS and must never be "
                        "divided into each other: arrivals count THEIR handshakes, connector_* "
                        "counts 401s WE issued. "
                        "arrivals_chatgpt_30d is the older sibling instrument (live 2026-07-17) "
                        "and is legitimately sparse -- 41 events in its first 42 days -- because "
                        "ChatGPT connector traffic here is rare, NOT because it is broken."
                    ),
                    "note": (
                        "Challenge EVENTS issued, not distinct people: an unconverted caller is "
                        "re-challenged on every eligible call. NOT divisible by identities to get "
                        "a conversion rate (events vs deduped subs = different populations). "
                        "★2026-08-15 the Claude-connector challenge MOVED from initialize to "
                        "tools/call (r-challenge-after-value): initialize is the first message of "
                        "the MCP handshake, so 401-ing it meant the connector never reached the "
                        "tools/list exemption and died at the handshake — 4,356 init challenges "
                        "bought 3 identities in 30d. connector_init therefore decays to 0 by "
                        "DESIGN and is a RETIRED series; do NOT read that decay as the fix "
                        "working. Trend connector_all across the boundary, connector_call after "
                        "it. invalid_bearer is scanner-poisonable. Only the TREND is "
                        "decision-grade. gateway_reporting=false means DORMANT, not zero."
                    ),
                }
            except Exception:
                # Table absent until the gateway first flushes => dormant, not broken.
                ib["challenge_side_error"] = True
                out["summary"]["oauth_funnel_gateway_reporting"] = False

            out["identity_breakdown"] = ib
    except Exception as e:
        return jsonify(error="query_failed", detail=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify(out), 200
