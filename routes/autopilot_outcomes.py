"""Phase FFFFF (2026-05-16) — autopilot resolution outcome verification.

The brain knows which actions it FIRED but not which ones SUCCEEDED.
When dchub_media_press_silent fires marketing/auto-generate, did a
new press row actually land? When winback_pitches_unsent fires the
delivery endpoint, did Resend actually send? Today: invisible.

This module:
  1. Defines per-pattern outcome verifiers (one function per pattern)
  2. Runs verifications 30+ min after each autopilot action fires
  3. Persists outcomes to autopilot_outcomes table
  4. Brain detector autopilot_action_unverified fires for stale ones

  POST /api/v1/brain/autopilot/verify-pending   admin cron entry
  GET  /api/v1/brain/autopilot/outcomes         public outcome history

Cron piggy-backs on heartbeat-auto (every 15min): each tick verifies
actions fired 30+ min ago.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


autopilot_outcomes_bp = Blueprint("autopilot_outcomes", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_outcomes (
    id                BIGSERIAL PRIMARY KEY,
    autopilot_action_id BIGINT,        -- FK reference to brain_autopilot_actions.id
    pattern_name      TEXT NOT NULL,
    fired_at          TIMESTAMPTZ NOT NULL,
    verified_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    succeeded         BOOLEAN,           -- NULL = cannot_verify (no verifier defined for this pattern)
    evidence          TEXT,              -- what we checked and found
    failure_reason    TEXT
);
CREATE INDEX IF NOT EXISTS ix_autopilot_outcomes_pattern
    ON autopilot_outcomes(pattern_name, verified_at DESC);
CREATE INDEX IF NOT EXISTS ix_autopilot_outcomes_action
    ON autopilot_outcomes(autopilot_action_id);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            # r86: the table already exists in prod with succeeded BOOLEAN NOT
            # NULL. Relax it so cannot_verify rows (succeeded=NULL) are legal —
            # without this, the sentinel INSERT below fails the NOT NULL and is
            # silently swallowed by the bare except, leaving the bug in place.
            cur.execute("ALTER TABLE autopilot_outcomes ALTER COLUMN succeeded DROP NOT NULL")
    except Exception:
        try: c.rollback()
        except Exception: pass


# Per-pattern verifier: returns (succeeded, evidence_str).
# Each takes the autopilot_actions row dict and DB cursor.

def _verify_press_silent(action, cur) -> tuple[bool, str]:
    """marketing/auto-generate — did a new press row land in 30 min?"""
    try:
        cur.execute("""
            SELECT COUNT(*) FROM auto_press_releases
             WHERE COALESCE(generated_for, created_at)
                    >= %s
               AND COALESCE(generated_for, created_at)
                    <= %s + INTERVAL '60 minutes'
        """, (action["started_at"], action["started_at"]))
        n = int((cur.fetchone() or [0])[0] or 0)
        return (n > 0, f"{n} new press release(s) in the 60min after action fired")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_winback_delivery(action, cur) -> tuple[bool, str]:
    """winback/deliver — did a row land in winback_outreach_sent?"""
    try:
        cur.execute("""
            SELECT COUNT(*) FROM winback_outreach_sent
             WHERE sent_at >= %s - INTERVAL '5 minutes'
               AND sent_at <= %s + INTERVAL '15 minutes'
        """, (action["started_at"], action["started_at"]))
        n = int((cur.fetchone() or [0])[0] or 0)
        return (n > 0, f"{n} winback delivery row(s) recorded")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_market_deep_dive(action, cur) -> tuple[bool, str]:
    """markets/deep-dive/cron — did any rows update?"""
    try:
        cur.execute("""
            SELECT COUNT(*) FROM market_deep_dives
             WHERE generated_at >= %s - INTERVAL '5 minutes'
               AND generated_at <= %s + INTERVAL '30 minutes'
        """, (action["started_at"], action["started_at"]))
        n = int((cur.fetchone() or [0])[0] or 0)
        return (n > 0, f"{n} market deep-dive(s) generated")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_competitor_response(action, cur) -> tuple[bool, str]:
    """competitor_announcement → marketing/auto-generate — same as press_silent"""
    return _verify_press_silent(action, cur)


# ── 2026-06-14 (incentive re-tune ITEM 2): REAL verifiers for the five
# patterns that actually fire daily. Before this, _VERIFIERS had four
# entries — NONE of which matched the high-volume patterns — so every one
# of those actions wrote a NULL/cannot_verify sentinel and 0 outcomes were
# ever verified_resolved. These close the loop so the brain is rewarded
# for CLOSURES (a registry actually submitted, an email actually sent, a
# draft actually drafted) instead of raw action count.
#
# Every verifier is wrapped so any failure (missing table, bad column,
# parse miss) degrades to (False, "verify_query_failed:…") — it can never
# break the verification pass or fabricate a success. Tables/columns were
# confirmed against prod information_schema on 2026-06-14.

def _verify_outbound_distribution(action, cur) -> tuple[bool, str]:
    """outbound_distribution_health → POST mcp-registry/submit.

    The action carries finding_url='target:<key>' and POSTs the submit
    endpoint, which (via _record) writes an outreach_submissions row with
    action in (submit | manual_submit_queued | audit) for that target_key.
    Success = a fresh ledger row for the target landed in the window after
    the action fired."""
    try:
        url = (action.get("finding_url") or "")
        key = url.split("target:")[-1].strip() if "target:" in url else ""
        if not key:
            return (False, "no_target_key_in_finding_url")
        cur.execute("""
            SELECT action, outcome, submitted_at
              FROM outreach_submissions
             WHERE target_key = %s
               AND submitted_at >= %s - INTERVAL '2 minutes'
               AND submitted_at <= %s + INTERVAL '30 minutes'
             ORDER BY submitted_at DESC LIMIT 5
        """, (key, action["started_at"], action["started_at"]))
        rows = cur.fetchall()
        if not rows:
            return (False, f"no outreach_submissions row for target={key} in the 30min after action")
        # r-incentives FIX (review): a mere ledger row is NOT a closure.
        # action='audit' is just a re-check; 'manual_submit_queued' needs a
        # human. Reward success ONLY when a real submission actually landed —
        # otherwise outbound_distribution_health would score "verified" forever
        # while the registry stays not_listed.
        _CLOSED = {"submitted", "listed", "ok", "success", "accepted", "approved", "live"}
        for r in rows:
            if (r.get("action") or "").lower() == "submit" and (r.get("outcome") or "").lower() in _CLOSED:
                return (True, f"submitted {key} (outcome={r.get('outcome')})")
        detail = ", ".join(f"{(r.get('action') or '?')}:{(r.get('outcome') or '?')}" for r in rows)
        return (False, f"{len(rows)} row(s) for {key} but no completed submit ({detail})")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_monthly_outreach_sent(action, cur) -> tuple[bool, str]:
    """monthly_trend_unsent_3d → POST reports/monthly/send-outreach.

    The send endpoint logs a monthly_outreach_log row keyed (year, month)
    with recipients/succeeded counts. finding_url encodes the period as
    /reports/monthly/<year>-<month>. Success = a log row exists for that
    period with at least one recipient/success (i.e. the campaign actually
    went out, not just the endpoint returning 200)."""
    try:
        import re as _re
        url = (action.get("finding_url") or "")
        m = _re.search(r"/(\d{4})-(\d{1,2})", url)
        if not m:
            return (False, f"could_not_parse_year_month:{url[:60]}")
        yr, mo = int(m.group(1)), int(m.group(2))
        cur.execute("""
            SELECT recipients, succeeded, sent_at, triggered_by
              FROM monthly_outreach_log
             WHERE year = %s AND month = %s
        """, (yr, mo))
        r = cur.fetchone()
        if not r:
            return (False, f"no monthly_outreach_log row for {yr}-{mo:02d} — send did not land")
        recips = int(r["recipients"] or 0)
        succ = int(r["succeeded"] or 0)
        sent = (succ > 0 or recips > 0)
        return (bool(sent),
                f"{yr}-{mo:02d} outreach log: recipients={recips} succeeded={succ} "
                f"triggered_by={r['triggered_by']} sent_at={r['sent_at']}")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_schema_org_recovered(action, cur) -> tuple[bool, str]:
    """schema_org_coverage_low → POST heal/run html_quality_scan.

    The action re-probes rendered HTML/JSON-LD; run_audit() upserts the
    fresh per-page state into schema_org_audit (has_jsonld, last_checked).
    The finding fired because coverage < 80%. Success = the audit was
    re-checked AFTER the action fired AND coverage has recovered to ≥80%
    (i.e. the /schema-org/missing worklist shrank). We compute coverage
    from the persisted table — no HTTP call inside the verifier."""
    try:
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE has_jsonld IS NOT TRUE) AS missing,
                   MAX(last_checked) AS newest
              FROM schema_org_audit
        """)
        r = cur.fetchone()
        total = int(r["total"] or 0)
        missing = int(r["missing"] or 0)
        newest = r["newest"]
        if total == 0:
            return (False, "schema_org_audit empty — cannot judge coverage")
        cov = round(100.0 * (total - missing) / total, 1)
        # Require the scan to have re-run after the action (otherwise we'd
        # be judging stale state). 2-min grace for clock skew.
        import datetime as _dt
        rechecked = bool(newest and newest >= (action["started_at"] - _dt.timedelta(minutes=2)))
        succeeded = bool(rechecked and cov >= 80.0)
        return (succeeded,
                f"schema coverage now {cov}% ({missing}/{total} pages missing JSON-LD); "
                f"rechecked_after_action={rechecked} (last_checked={newest})")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_l22_draft(action, cur) -> tuple[bool, str]:
    """inspector_l22_handoff → POST brain/brief/<id>/draft-prs.

    The handoff hands RECIPE candidates to Layer-22, which (when it acts)
    writes a brain_auto_code_actions row via _record(). Success = a draft
    landed in brain_auto_code_actions near the action time. NOTE: L22 has
    a 3-recipe whitelist + _already_drafted() idempotency + an env gate
    (BRAIN_L22_HANDOFF_ENABLE), so a legitimately-gated/duplicate handoff
    produces NO new draft → that is honestly reported as not-succeeded
    (a non-closure), which is the intent: reward only real drafts. The
    15-min look-back captures an idempotent draft made just before this
    tick's handoff."""
    try:
        cur.execute("""
            SELECT COUNT(*) AS n, MAX(drafted_at) AS last_at
              FROM brain_auto_code_actions
             WHERE drafted_at >= %s - INTERVAL '15 minutes'
               AND drafted_at <= %s + INTERVAL '30 minutes'
        """, (action["started_at"], action["started_at"]))
        r = cur.fetchone()
        n = int(r["n"] or 0)
        if n > 0:
            return (True, f"{n} brain_auto_code_actions draft(s) near the handoff (last={r['last_at']})")
        return (False, "no brain_auto_code_actions draft in window — L22 produced no draft "
                       "(env-gated OFF, recipe not whitelisted, or already drafted)")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


def _verify_founding_welcomed(action, cur) -> tuple[bool, str]:
    """founding_customer_not_welcomed → POST founding-customers/send-welcome.

    On a successful send, send_founding_welcome_email() sets the customer's
    contact_status='welcomed' and contacted_at=NOW(). The action's payload
    carries {'email': …}. Success = that customer is now 'welcomed'."""
    try:
        import re as _re
        pay = action.get("action_payload")
        email = ""
        if isinstance(pay, dict):
            email = (pay.get("email") or "").strip()
        if not email:
            m = _re.search(r"[\w.+-]+@[\w.-]+\.\w+", action.get("detail") or "")
            email = m.group(0) if m else ""
        if not email:
            return (False, "no_email_in_payload_or_detail")
        cur.execute("""
            SELECT contact_status, contacted_at
              FROM founding_customers WHERE email = %s
        """, (email,))
        r = cur.fetchone()
        if not r:
            return (False, f"{email} not found in founding_customers")
        welcomed = ((r["contact_status"] or "").lower() == "welcomed")
        return (bool(welcomed),
                f"{email}: contact_status={r['contact_status']} contacted_at={r['contacted_at']}")
    except Exception as e:
        return (False, f"verify_query_failed:{type(e).__name__}")


# Pattern → verifier mapping. Patterns without a verifier are skipped
# (treated as 'cannot verify' — different from failed).
_VERIFIERS = {
    "dchub_media_press_silent":   _verify_press_silent,
    "winback_pitches_unsent":     _verify_winback_delivery,
    "market_deep_dive_stale":     _verify_market_deep_dive,
    "competitor_announcement":    _verify_competitor_response,
    # 2026-06-14 ITEM 2 — the five high-volume patterns that fire daily.
    "outbound_distribution_health": _verify_outbound_distribution,
    "monthly_trend_unsent_3d":      _verify_monthly_outreach_sent,
    "schema_org_coverage_low":      _verify_schema_org_recovered,
    "inspector_l22_handoff":        _verify_l22_draft,
    "founding_customer_not_welcomed": _verify_founding_welcomed,
}


def verify_pending(window_minutes: int = 30, max_actions: int = 50) -> dict:
    """Find autopilot actions fired in the last `window_minutes` to N
    hours ago that haven't been verified yet, run their verifier,
    persist outcomes."""
    out: dict = {"verified": 0, "succeeded": 0, "failed": 0,
                  "skipped": 0, "resolved": 0, "details": [],
                  "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Find unverified actions in the verification window
            try:
                cur.execute("""
                    SELECT a.id, a.pattern_name, a.started_at, a.outcome,
                           a.finding_issue, a.finding_url,
                           -- 2026-06-14 ITEM 2: the new verifiers read the
                           -- action payload (founding email) and detail
                           -- (fallback email parse). Selecting them here is
                           -- harmless for the pre-existing verifiers.
                           a.action_payload, a.detail
                      FROM brain_autopilot_actions a
                     WHERE a.started_at <= NOW() - %s * INTERVAL '1 minute'
                       -- r86b: 6h→24h to match the autopilot_action_unverified
                       -- detector window (1h-24h). At 6h, executed_ok actions in
                       -- the 6h-24h band were unreachable, so verifier-less
                       -- patterns there never got a cannot_verify sentinel and
                       -- the finding never cleared. max_actions caps the work.
                       AND a.started_at >= NOW() - INTERVAL '24 hours'
                       AND a.outcome = 'executed_ok'
                       AND NOT EXISTS (
                         SELECT 1 FROM autopilot_outcomes o
                          WHERE o.autopilot_action_id = a.id
                       )
                     ORDER BY a.started_at DESC LIMIT %s
                """, (window_minutes, max_actions))
                actions = cur.fetchall()
            except Exception as e:
                out["error"] = f"action_query_failed:{type(e).__name__}"
                return out

            for a in actions:
                pattern = a["pattern_name"] or ""
                verifier = _VERIFIERS.get(pattern)
                if not verifier:
                    # r86: no verifier for this pattern. Write a NEUTRAL
                    # cannot_verify outcome (succeeded=NULL) so the NOT EXISTS
                    # subquery above (and the autopilot_action_unverified
                    # detector, which uses the same NOT EXISTS) stops
                    # re-selecting this action forever. Deliberately does NOT
                    # touch brain_issue_persistence (no mark_resolved /
                    # mark_attempted_failed) — absence of a verifier is neither
                    # success nor failure, and writing back either way would
                    # corrupt the finding lifecycle.
                    try:
                        cur.execute("""
                            INSERT INTO autopilot_outcomes
                              (autopilot_action_id, pattern_name, fired_at,
                               succeeded, evidence, failure_reason)
                            VALUES (%s, %s, %s, NULL, %s, NULL)
                            ON CONFLICT DO NOTHING
                        """, (a["id"], pattern, a["started_at"],
                              "no_verifier_defined"))
                        out["cannot_verify"] = out.get("cannot_verify", 0) + 1
                    except Exception:
                        pass
                    out["skipped"] += 1
                    continue
                succeeded, evidence = verifier(a, cur)
                out["verified"] += 1
                if succeeded: out["succeeded"] += 1
                else: out["failed"] += 1
                try:
                    cur.execute("""
                        INSERT INTO autopilot_outcomes
                          (autopilot_action_id, pattern_name, fired_at,
                           succeeded, evidence, failure_reason)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (a["id"], pattern, a["started_at"], succeeded,
                          evidence, (None if succeeded else evidence)))
                except Exception: pass

                # ── CLOSE THE LOOP (INV1 + INV3, 2026-06-02) ────────────
                # Write the outcome BACK to brain_issue_persistence so a
                # verified-resolved finding stops being re-proposed /
                # re-acted. This is the missing link the brain never had:
                # detection → action → VERIFICATION → resolution.
                #
                #   succeeded=TRUE  WITH evidence → mark_resolved()
                #       (terminal 'verified_resolved'; the learn-worklist
                #        and the action gate both skip it from now on)
                #   succeeded=FALSE              → mark_attempted_failed()
                #       (explicitly NOT resolved — stays in the worklist,
                #        surfaces in /stuck-findings once it piles up)
                #
                # INV3 is enforced at BOTH ends: mark_resolved() itself
                # refuses empty evidence, and we only call it when
                # succeeded is truthy AND evidence is non-blank — never on
                # assumption, never on timeout, never on succeeded=FALSE.
                f_issue = (a.get("finding_issue") or "").strip()
                f_url = a.get("finding_url") or ""
                if f_issue:
                    try:
                        from routes import brain_v2_store as _bs
                        if succeeded and (evidence or "").strip():
                            did = _bs.mark_resolved(f_issue, f_url, evidence)
                            out["resolved"] = out.get("resolved", 0) + (1 if did else 0)
                            # #3: persist verified-resolved MEMORY (deepens the
                            # fix-memory depth metric + arms recall_gate). Own
                            # guard — a memory write must never break verify.
                            try:
                                from routes.brain_memory import record_verified_resolved
                                record_verified_resolved(f_issue, f_url, evidence)
                            except Exception:
                                pass
                        elif not succeeded:
                            _bs.mark_attempted_failed(f_issue, f_url, evidence)
                    except Exception as _we:
                        # Best-effort: a writeback hiccup must never break
                        # the verification pass.
                        out.setdefault("writeback_errors", []).append(
                            f"{type(_we).__name__}")

                out["details"].append({
                    "action_id": int(a["id"]),
                    "pattern":   pattern,
                    "fired_at":  a["started_at"].isoformat() if a["started_at"] else None,
                    "succeeded": succeeded,
                    "evidence":  evidence,
                })
    finally:
        try: c.close()
        except Exception: pass
    return out


@autopilot_outcomes_bp.route("/api/v1/brain/autopilot/verify-pending", methods=["POST"])
def verify_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(verify_pending()), 200


@autopilot_outcomes_bp.route("/api/v1/brain/autopilot/outcomes", methods=["GET"])
def outcomes_endpoint():
    """Public outcome history — what autopilot actions actually worked."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # r86: cannot_verify rows (succeeded IS NULL) are excluded from the
            # success-rate denominator so a verifier-less pattern can't drag the
            # rate to 0%. They're surfaced separately as cannot_verify.
            cur.execute("""
                SELECT pattern_name,
                       COUNT(*) FILTER (WHERE succeeded IS NOT NULL) AS total,
                       COUNT(*) FILTER (WHERE succeeded)             AS succeeded,
                       COUNT(*) FILTER (WHERE succeeded IS NULL)     AS cannot_verify,
                       MAX(verified_at) AS last_verified
                  FROM autopilot_outcomes
                 WHERE verified_at >= NOW() - INTERVAL '30 days'
                 GROUP BY pattern_name
                 ORDER BY COUNT(*) DESC
            """)
            by_pattern = cur.fetchall()
            cur.execute("""
                SELECT pattern_name, fired_at, verified_at,
                       succeeded, evidence
                  FROM autopilot_outcomes
                 ORDER BY verified_at DESC LIMIT 50
            """)
            recent = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    return jsonify({
        "by_pattern": [{
            "pattern":          r["pattern_name"],
            "total_verified":   int(r["total"] or 0),
            "succeeded":        int(r["succeeded"] or 0),
            "cannot_verify":    int(r["cannot_verify"] or 0),
            "success_rate_pct": round(100.0 * (r["succeeded"] or 0) / max(1, r["total"] or 1), 1),
            "last_verified":    r["last_verified"].isoformat() if r["last_verified"] else None,
        } for r in by_pattern],
        "recent": [{
            "pattern":     r["pattern_name"],
            "fired_at":    r["fired_at"].isoformat() if r["fired_at"] else None,
            "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None,
            # r86: tri-state — None when cannot_verify (no verifier), else bool
            "succeeded":   (None if r["succeeded"] is None else bool(r["succeeded"])),
            "evidence":    r["evidence"],
        } for r in recent],
    }), 200
