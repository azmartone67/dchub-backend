"""retention_claim_checks.py — the ONE source for the claim-carry and
claim-activation retention checks (2026-07-31).

Why this module exists — two defects in one:

1. TWIN DRIFT. routes/flywheel_master_shell.py and
   routes/backfunnel_master_shell.py each carried their own copy of the same
   two checks ("mirrors backfunnel" said the comment — mirrors drift; this
   repo has paid for transcribed twins three times in July). Both shells now
   RENDER from here; neither owns SQL.

2. REBASELINE ARTIFACT. Both checks gated a 30-day window that BLENDS claims
   minted before and after the activation-wiring fixes shipped
   (backend /track session→key resolver DCHUB_CLAIM_SESSION_BIND 2026-07-21 +
   gateway early session-tier-bind 2026-07-22). Pre-fix claims are
   permanently un-fixable history — the wiring they needed did not exist when
   their sessions ran — so the blended gate stays red for weeks AFTER the fix
   lands, reading as a live defect. That is the same artifact class the
   07-23 flywheel wave documented (decline = rebaseline, not behaviour).

   The honest gate: measure the POST-FIX cohort at the SAME thresholds, show
   the blended and pre-fix numbers alongside, and refuse to gate while the
   post-fix cohort is too small to mean anything. As the window rolls, the
   pre-fix cohort ages out and the split converges to the original check.

Queries run WITHOUT bound params (literal SQL, trusted module constants
only) — the LIKE 'dch_live_%' literals are safe exactly because psycopg2
performs no substitution here. Both shells' fail-soft conventions are
preserved: any DB error yields (None, "query failed") and never raises.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("retention_claim_checks")

# Both halves of the activation wiring were live from this date: the backend
# /track resolver shipped 07-21, the gateway's early all-tool session-tier
# bind 07-22. Claims minted earlier ran against a gateway that could not
# carry them — they are history, not signal.
CLAIM_CARRY_FIX_DATE = "2026-07-22"

# Below this many post-fix observations the gate abstains (None → "?"), with
# the blended figure still shown. A rate over a handful of sessions is an
# anecdote wearing a percent sign.
MIN_POST_FIX_COHORT = 10

CARRY_PASS_PCT = 70.0
ACTIVATION_PASS_PCT = 40.0

_SQL_CARRY = f"""
WITH claims AS (
 SELECT api_key, created_at, metadata->>'session_id' AS sid,
        (created_at >= DATE '{CLAIM_CARRY_FIX_DATE}') AS post_fix
   FROM mcp_dev_keys
  WHERE api_key LIKE 'dch_live_%' AND metadata->>'source'='claim_api'
    AND metadata->>'session_id' IS NOT NULL
    AND metadata->>'session_id' NOT IN ('None','')
    AND created_at >= now() - interval '30 days')
SELECT
 COUNT(*) FILTER (WHERE NOT post_fix AND EXISTS(
   SELECT 1 FROM mcp_call_log l WHERE l.session_id=c.sid AND l.timestamp>c.created_at)),
 COUNT(*) FILTER (WHERE NOT post_fix AND EXISTS(
   SELECT 1 FROM mcp_call_log l WHERE l.session_id=c.sid AND l.timestamp>c.created_at AND l.api_key=c.api_key)),
 COUNT(*) FILTER (WHERE post_fix AND EXISTS(
   SELECT 1 FROM mcp_call_log l WHERE l.session_id=c.sid AND l.timestamp>c.created_at)),
 COUNT(*) FILTER (WHERE post_fix AND EXISTS(
   SELECT 1 FROM mcp_call_log l WHERE l.session_id=c.sid AND l.timestamp>c.created_at AND l.api_key=c.api_key))
  FROM claims c
"""

_SQL_ACTIVATION = f"""
WITH claimed AS (
 SELECT api_key, created_at,
        (created_at >= DATE '{CLAIM_CARRY_FIX_DATE}') AS post_fix
   FROM mcp_dev_keys
  WHERE api_key LIKE 'dch_live_%' AND metadata->>'source'='claim_api'
    AND created_at >= now() - interval '30 days'
    AND created_at <  now() - interval '7 days')
SELECT
 COUNT(*) FILTER (WHERE NOT post_fix),
 COUNT(*) FILTER (WHERE NOT post_fix AND EXISTS(
   SELECT 1 FROM mcp_call_log l WHERE l.api_key=c.api_key AND l.timestamp>c.created_at+interval '1 minute')),
 COUNT(*) FILTER (WHERE post_fix),
 COUNT(*) FILTER (WHERE post_fix AND EXISTS(
   SELECT 1 FROM mcp_call_log l WHERE l.api_key=c.api_key AND l.timestamp>c.created_at+interval '1 minute'))
  FROM claimed c
"""


def _row(conn, sql):
    """Fail-soft single-row fetch, rollback on error — the shells' shared
    convention, owned here so both callers degrade identically."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return cur.fetchone()
        finally:
            cur.close()
    except Exception as e:
        logger.debug("[retention-claim] query failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _pct(num, den):
    return round(100.0 * num / den, 1) if den else None


def _split_verdict(pre_den, pre_num, post_den, post_num,
                   pass_pct, noun, pre_noun_verb):
    """(passed, detail) shared shape for both checks. Gates ONLY the post-fix
    cohort; abstains (None) while it is under MIN_POST_FIX_COHORT; always
    shows pre-fix and blended so nothing is hidden by the split."""
    blended_den = pre_den + post_den
    blended_num = pre_num + post_num
    if blended_den == 0:
        return None, f"no {noun} yet"
    blended = _pct(blended_num, blended_den)
    pre = _pct(pre_num, pre_den)
    post = _pct(post_num, post_den)
    ctx = (f"pre-fix {pre_num}/{pre_den}"
           + (f" = {pre}%" if pre is not None else "")
           + f" ({pre_noun_verb}) · blended {blended_num}/{blended_den} = {blended}%")
    if post_den < MIN_POST_FIX_COHORT:
        return None, (f"post-fix cohort still maturing: {post_den} of "
                      f">={MIN_POST_FIX_COHORT} {noun} since "
                      f"{CLAIM_CARRY_FIX_DATE} · {ctx}")
    return (post >= pass_pct), (f"post-fix (since {CLAIM_CARRY_FIX_DATE}): "
                                f"{post_num}/{post_den} = {post}% · {ctx}")


def claim_carry_verdict(conn):
    """post-claim sessions carrying the claimed key — gated on the post-fix
    cohort at the original {CARRY_PASS_PCT}% threshold."""
    r = _row(conn, _SQL_CARRY)
    if r is None:
        return None, "query failed"
    pre_kept, pre_carried, post_kept, post_carried = (int(x or 0) for x in r)
    return _split_verdict(
        pre_kept, pre_carried, post_kept, post_carried, CARRY_PASS_PCT,
        "post-claim sessions",
        "claims minted before the wiring existed — un-fixable history")


def claim_activation_verdict(conn):
    """mature claimed keys that made any call after mint — gated on the
    post-fix MATURE cohort at the original {ACTIVATION_PASS_PCT}% threshold."""
    r = _row(conn, _SQL_ACTIVATION)
    if r is None:
        return None, "query failed"
    pre_total, pre_used, post_total, post_used = (int(x or 0) for x in r)
    return _split_verdict(
        pre_total, pre_used, post_total, post_used, ACTIVATION_PASS_PCT,
        "mature post-fix claims",
        "minted before the wiring existed — un-fixable history")
