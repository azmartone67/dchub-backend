"""DC Hub — PUBLISHED TRUTH master shell (#54, 2026-08-20).

★ WHY THIS SHELL EXISTS

dchub-mcp-server#202 (2026-08-18 06:31Z) removed DC Hub's own GitHub Actions
from `is_real_external`. They had been 80.4% of "real external" calls and 72.1%
of agents. Four PRs (#2970/#2978/#2980/#2982) then stopped every week-over-week
DELTA being quoted across that correction.

Withholding the deltas was necessary and not sufficient. Eight further defects
survived it, and they share ONE shape: **a surface publishes a number, or a
sentence, that the data underneath no longer supports.** A delta guard cannot
see any of them, because none of them is a delta.

Each lane pins ONE of those eight as an INVARIANT, never as a value
(see reference: contract healer #44 — "invariants≠values"). A lane that pins
today's number would go green the moment the number drifts for an unrelated
reason, which is the failure mode this whole family of shells exists to retire.

★ THIS SHELL IS BORN RED. That is correct and expected (cf. #45 BORN RED).
Every lane below was measured FAILING at 2026-08-20; a green lane on day one
would mean the invariant was written to fit the defect.

★ REPORT-ONLY. It heals nothing. Several of these are product decisions
(lane C) or ops actions (lane B) that must not be auto-actioned.

Lanes
  A press_level        a published LEVEL must not quote a superseded population
  B backup_health      a backup feed must not be overdue or last-run-failed
  C wall_reachability  enforcement ON must imply the wall can actually deny
  D conversion_honesty the headline count must use the honest paid filter
  E identity_label     'identified' must mean identified, not defaulted
  F retention_population every cohort must declare its population filter
  G gateway_disclosure  a gateway must not be summed into demand undisclosed
  H prose_vs_data       published prose must not contradict its own payload

Routes (registered via published_truth_master_shell_bp in main.py):
  GET /api/v1/admin/published-truth-shell/master-tick  -> JSON verdicts
  GET /api/v1/admin/published-truth-shell              -> HTML board
  GET /admin/published-truth-shell                     -> HTML board
Kill: PUBLISHED_TRUTH_SHELL_DISABLE=1
"""
from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
published_truth_master_shell_bp = Blueprint("published_truth_master_shell", __name__)

_PUBLIC = "https://dchub.cloud"
_TIMEOUT = 8            # per-read; the whole tick must clear CF's 15s admin ceiling
_CORRECTION = "2026-08-18T06:31:00+00:00"   # dchub-mcp-server#202


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("PUBLISHED_TRUTH_SHELL_DISABLE") or "").strip() == "1"


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": detail,
            "critical": critical}


def _lane_verdict(checks: list) -> str:
    """FAIL on any false; `?` when nothing was actually verified.

    A lane whose reads all failed must never render green. "I could not measure
    it" and "it is fine" are different states and this shell exists precisely
    because surfaces blurred them.
    """
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is None for k in crits):
        return "?"
    if any(k["pass"] is None for k in checks) and not any(k["pass"] is True for k in checks):
        return "?"
    return "PASS"


def _get_json(path: str):
    """GET our own JSON, fail-soft. None on ANY failure — never {} or 0.

    ★ `requests`, never `urllib.request`: regression_lint hard-blocks urllib in
    changed lines, and the CF edge 1010s a bare urllib User-Agent before it
    reaches the origin.
    ★ Cache-busted: /api/v1/* is CF-cached under Rule #3 with
    mode=override_origin, which ignores no-store — a stale payload would let
    this shell certify a state that no longer exists.
    """
    try:
        import requests
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{_PUBLIC}{path}{sep}_cb=shell54",
                         headers={"User-Agent": "dchub-published-truth-shell/1.0"},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.warning("[shell54] read failed %s: %s", path, str(e)[:120])
        return None


def _repo_text(rel: str):
    """Read a repo file for the SHAPE checks. None if unreadable.

    Lanes E and H assert something about how a number or a sentence is
    PRODUCED, which no payload can answer about itself.
    """
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, rel), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ── LANE A — a published LEVEL must not quote a superseded population ───────
def _lane_press_level(ctx: dict) -> list:
    """★ The delta guards left the LEVEL behind.

    #2978/#2982 withhold every *_wow_pct across the correction. But
    press_headline_metric still leads with the LEVEL of a superseded week —
    live 2026-08-20 it read "DC Hub served 2,100 external AI-agent tool calls
    in the week of 2026-08-10", a week measured at ~80% DC Hub's own GitHub
    Actions. Withholding the delta and publishing the level says the quiet part
    louder: the sentence is press-ready and designed to be repeated verbatim.

    INVARIANT (not a value): IF the complete-week comparability says the week is
    superseded, THEN any headline quoting that week's count must disclose it.
    Passes when the level stops being published, when the week rolls past the
    correction, or when the sentence names the superseded share.
    """
    f = ctx.get("funnel")
    if not isinstance(f, dict):
        return [_check("a_read", "funnel readable", None,
                       "could not read /api/v1/mcp/funnel", critical=True)]
    comp = f.get("real_external_complete_wk_comparability") or {}
    superseded = bool(comp.get("superseded_by_correction"))
    headline = str(f.get("press_headline_metric") or "")
    lvl = _num(f.get("real_external_calls_complete_wk"))

    out = [_check("a_comparability_published", "complete-week comparability is published",
                  isinstance(comp, dict) and "superseded_by_correction" in comp,
                  "superseded_by_correction=%s" % comp.get("superseded_by_correction"),
                  critical=True)]
    if not superseded:
        out.append(_check("a_level_ok", "headline week is not superseded", True,
                          "the quoted week postdates the correction — level is quotable"))
        return out

    # The level appears in the sentence (formatted with a thousands separator).
    quoted = bool(lvl is not None and f"{int(lvl):,}" in headline)
    discloses = bool(re.search(r"superseded|withdrawn|own CI|GitHub Actions|"
                               r"measurement correction", headline, re.I)
                     and re.search(r"call", headline, re.I))
    # Disclosure must attach to the LEVEL, not only to the withheld delta. The
    # shipped sentence already explains why the WoW is missing; that is not the
    # same as telling a reader the 2,100 itself is mostly ours.
    delta_only = bool(re.search(r"WoW withheld", headline, re.I))
    out.append(_check(
        "a_level_not_quoted_bare",
        "a superseded week's LEVEL is not published as a bare external count",
        (not quoted) or (discloses and not delta_only),
        ("press_headline_metric quotes %s calls for a SUPERSEDED week and "
         "discloses only the withheld DELTA — a reader takes the level itself "
         "as an external-demand claim, but ~80%% of that week was DC Hub's own "
         "GitHub Actions. headline=%r"
         % (f"{int(lvl):,}" if lvl is not None else "?", headline[:150]))
        if quoted and (delta_only or not discloses)
        else "level not quoted bare",
        critical=True))
    return out


# ── LANE B — a backup feed must not be overdue or last-run-failed ───────────
def _lane_backup_health(ctx: dict) -> list:
    """A backup is not a metric. `/ops/deadman` already tracks it; nothing was
    reading the backup rows specifically, so a cancelled backup sat at 45h
    against a 30h cadence while every dashboard stayed green.

    INVARIANT: no feed whose name marks it as backup/restore may be overdue or
    report a failed/cancelled last run.
    """
    d = ctx.get("deadman")
    if not isinstance(d, dict):
        return [_check("b_read", "deadman readable", None,
                       "could not read /api/v1/ops/deadman", critical=True)]
    feeds = d.get("feeds") or []
    if not feeds:
        return [_check("b_feeds", "deadman lists feeds", None,
                       "feeds[] empty — an empty list is UNMEASURED, not healthy",
                       critical=True)]
    pat = re.compile(r"backup|restore|snapshot-r2|neon-r2", re.I)
    guarded = [f for f in feeds if pat.search(str(f.get("feed") or ""))]
    out = [_check("b_backup_feeds_found", "backup feeds are tracked at all",
                  bool(guarded),
                  "%d backup-class feeds in deadman: %s"
                  % (len(guarded), ", ".join(str(f.get("feed")) for f in guarded) or "NONE"),
                  critical=True)]
    for f in guarded:
        name = str(f.get("feed"))
        status = str(f.get("status") or "")
        bad = bool(f.get("overdue")) or bool(
            re.search(r"fail|cancel|error", status, re.I))
        out.append(_check(
            "b_%s" % re.sub(r"[^a-z0-9]+", "_", name.lower()),
            "%s is healthy" % name,
            not bad,
            "status=%s age=%sh cadence=%sh overdue=%s reasons=%s"
            % (status or "?", f.get("age_hours"), f.get("cadence_hours"),
               f.get("overdue"), f.get("reasons")),
            critical=True))
    return out


# ── LANE C — enforcement ON must imply the wall can actually deny ───────────
def _lane_wall_reachability(ctx: dict) -> list:
    """★ THE BIGGEST ONE. `enforce ON` + `0 wall hits` reads as "nobody is
    hitting limits". It actually means the mechanism has never run.

    Measured 2026-08-20: enforcement on since 2026-08-08, ZERO allowed=false
    decisions over its entire lifetime, while ~650 upgrade signals/week are
    served. The wall is skipped entirely for keyless callers, and the keys it
    can evaluate average ~3.8 lifetime calls against a 300-call free floor.

    INVARIANT: it is contradictory to serve upgrade SIGNALS while the mechanism
    that converts them has never produced a decision. Either the wall is
    reachable (some decisions exist), or the signals are advisory-only and the
    payload must not present them as upgrade pressure.
    """
    f = ctx.get("funnel")
    if not isinstance(f, dict):
        return [_check("c_read", "funnel readable", None,
                       "could not read /api/v1/mcp/funnel", critical=True)]
    qw = f.get("quota_wall") or {}
    enforce = qw.get("enforce")
    if isinstance(enforce, str):
        enforce = enforce.strip().lower() in ("1", "on", "true", "yes")
    hits = _num(qw.get("hits_month"))
    keys_at = _num(qw.get("keys_month"))
    # ★ table_exists is the LIFETIME signal and the strongest one available.
    # mcp_quota_wall_hits is created lazily on the FIRST wall hit, so
    # table_exists=false means no key has EVER reached its monthly quota — not
    # this month, ever. The payload's own `interpretation` says so.
    ever = qw.get("table_exists")
    signals = _num(f.get("real_external_signals_7d")) or _num(f.get("upgrade_signals_7d"))

    out = [_check("c_wall_published", "quota_wall block is published",
                  bool(qw), "quota_wall=%s" % ("present" if qw else "ABSENT"),
                  critical=True)]
    if not qw:
        return out
    out.append(_check("c_enforce_known", "enforcement state is stated",
                      enforce is not None, "enforce=%s" % enforce, critical=True))
    out.append(_check("c_lifetime_signal_present",
                      "the lifetime reachability signal is readable",
                      ever is not None or hits is not None,
                      "table_exists=%s hits_month=%s" % (ever, hits),
                      critical=True))
    # Never fired in its LIFETIME, while signals are being served, under
    # enforcement. Any one of these alone is fine; together they are a
    # mechanism that does not exist.
    never_fired = (ever is False) or (hits == 0 and ever is None)
    contradiction = bool(enforce and never_fired and (signals or 0) > 0)
    out.append(_check(
        "c_signals_have_a_mechanism",
        "upgrade signals are backed by a wall that can actually deny",
        not contradiction,
        ("enforce=ON and %s upgrade signals were served in 7d, but the wall has "
         "NEVER produced a decision in its lifetime (table_exists=%s, "
         "hits_month=%s, keys_at_quota=%s). The hits table is created on the "
         "first hit, so its absence is lifetime evidence. The wall is also "
         "skipped entirely for keyless callers, so the population generating "
         "the volume is never evaluated. Zero here is NOT 'nobody hit a limit' "
         "— it is 'the mechanism has not run'."
         % (signals, ever, hits, keys_at))
        if contradiction else
        "enforce=%s table_exists=%s hits=%s signals=%s" % (enforce, ever, hits, signals),
        critical=True))
    return out


# ── LANE D — the headline count must use the honest paid filter ────────────
def _lane_conversion_honesty(ctx: dict) -> list:
    """`conversions_30d` is a bare COUNT(*) over mcp_conversions with only
    `refunded_at IS NULL`. The payload's OWN definition of honest paid, in
    paid_signal_attribution_30d.definition, is stricter: "stripe_customer_id
    NOT NULL, seed/comp/NLR excluded". Two numbers, one label.

    INVARIANT: the published headline conversion count must not exceed the
    payload's own honest paid count, and attribution must not be asserted at
    100% when the bridge count is lower.
    """
    f = ctx.get("funnel")
    if not isinstance(f, dict):
        return [_check("d_read", "funnel readable", None,
                       "could not read /api/v1/mcp/funnel", critical=True)]
    conv = _num(f.get("conversions_30d"))
    attr = _num(f.get("conversions_attributed_30d"))
    psa = f.get("paid_signal_attribution_30d") or {}
    # `paid_total` IS the honest count — psa.definition states its filter
    # (stripe_customer_id NOT NULL, seed/comp/NLR excluded). conversions_30d is
    # a bare COUNT(*) with only refunded_at IS NULL, so the two are different
    # populations wearing one label.
    honest = _num(psa.get("paid_total"))
    if honest is None:
        honest = _num(psa.get("honest_paid_30d")) or _num(psa.get("paid_30d"))
    bridged = _num(psa.get("bridged_to_signal"))

    out = [_check("d_honest_definition_published",
                  "the honest paid definition is published beside the count",
                  bool(psa.get("definition")),
                  "definition=%s" % str(psa.get("definition"))[:110],
                  critical=True)]
    if conv is not None and honest is not None:
        out.append(_check(
            "d_headline_not_above_honest",
            "headline conversions_30d does not exceed the honest paid count",
            conv <= honest,
            "conversions_30d=%s vs honest paid=%s — the headline uses a looser "
            "filter (refunded_at IS NULL only) than the payload's own stated "
            "definition (stripe_customer_id NOT NULL, seed/comp/NLR excluded)"
            % (conv, honest),
            critical=True))
    else:
        out.append(_check("d_honest_comparable", "honest paid count is readable",
                          None, "psa keys=%s" % sorted(psa)[:8], critical=True))
    if conv is not None and attr is not None and bridged is not None:
        out.append(_check(
            "d_attribution_not_overstated",
            "attributed count is not asserted above what actually bridges",
            attr <= max(bridged, 0) or attr <= 0,
            "conversions_attributed_30d=%s while only %s row(s) bridge to an "
            "upstream signal via attribution_signal_id or a shared caller_id"
            % (attr, bridged)))
    return out


# ── LANE E — 'identified' must mean identified ─────────────────────────────
def _lane_identity_label(ctx: dict) -> list:
    """keys_by_tier IS computed (GROUP BY tier), so the count is real — but the
    LABEL is a default. Key creation stamps
    `tier=("paid" if _claimed_paid else "identified")`, so a key becomes
    'identified' by not being paid, with no email required. A dashboard reading
    "identified 434" states an identity claim the data never made.

    INVARIANT: if an 'identified' tier count is published, an email-backed
    sibling must be published beside it so a reader can see the gap. This shell
    does not demand a particular number — only that the identity claim is
    checkable.
    """
    f = ctx.get("funnel")
    if not isinstance(f, dict):
        return [_check("e_read", "funnel readable", None,
                       "could not read /api/v1/mcp/funnel", critical=True)]
    tiers = f.get("keys_by_tier") or {}
    if isinstance(tiers, list):
        tiers = {str(r.get("tier")): r.get("n") for r in tiers if isinstance(r, dict)}
    ident = _num(tiers.get("identified"))
    out = [_check("e_tier_published", "keys_by_tier is published",
                  bool(tiers), "tiers=%s" % sorted(tiers)[:8], critical=True)]
    if ident is None:
        out.append(_check("e_no_identified_tier", "no 'identified' tier claim published",
                          True, "nothing to corroborate"))
        return out

    email_backed = None
    for k in ("distinct_emails_30d", "emails_captured_30d", "identified_emails",
              "email_bound_keys", "captured_emails_30d"):
        email_backed = _num(f.get(k))
        if email_backed is not None:
            break
    out.append(_check(
        "e_identity_claim_is_corroborated",
        "an email-backed count is published beside the 'identified' tier",
        email_backed is not None,
        ("keys_by_tier.identified=%s is published with no email-backed sibling. "
         "The tier is assigned as the NON-PAID DEFAULT at key creation "
         "(tier = 'paid' if claimed_paid else 'identified'), so this number "
         "counts unpaid keys, not identified users." % ident),
        critical=True))
    src = _repo_text("flask_mcp_endpoints.py")
    if src is None:
        out.append(_check("e_source", "key-creation source readable", None,
                          "could not read flask_mcp_endpoints.py"))
    else:
        defaulted = bool(re.search(
            r"""tier\s*=\s*\(\s*["']paid["']\s+if\s+\w+\s+else\s+["']identified["']""",
            src))
        out.append(_check(
            "e_default_tier_documented",
            "the defaulting behaviour is still the shape this lane describes",
            True,
            "key creation stamps 'identified' as the non-paid default: %s"
            % ("CONFIRMED in source" if defaulted
               else "NOT FOUND — re-point this lane, the mechanism moved")))
    return out


# ── LANE F — every retention cohort must declare its population ────────────
def _lane_retention_population(ctx: dict) -> list:
    """`is_real_external` appears exactly ONCE in the retention route — on
    agent_cohort. Every key-based cohort (the PRIMARY published rate) runs with
    no externality filter at all, so the denominator is not the population the
    agent numbers describe. Two grains, one page, no label.

    INVARIANT: each published cohort must state its population filter. A rate
    whose denominator is undeclared cannot be compared to anything.
    """
    r = ctx.get("retention")
    if not isinstance(r, dict):
        return [_check("f_read", "retention readable", None,
                       "could not read /api/v1/mcp/retention", critical=True)]
    primary = str(r.get("primary_metric") or "")
    ident = r.get("identity_breakdown") or {}
    out = [_check("f_primary_named", "the primary metric names itself",
                  bool(primary), "primary_metric=%r" % primary[:110],
                  critical=True)]
    if not primary:
        return out

    # ★ THE SPECIFIC GAP. `is_real_external` appears exactly ONCE in the
    # retention route — on agent_cohort. The PRIMARY published rate is
    # key-based (durable api_key, 8-30d mature cohort) and runs with no
    # externality filter at all. agent_cohort_note declares ITS population
    # carefully; the key side declares nothing, and both sit on one page.
    #
    # Scoped deliberately to the KEY side. An earlier draft searched every note
    # on the payload for the word "population" and PASSED — agent_cohort_note
    # contains it, so the lane went green on the strength of the one cohort
    # that was never the problem.
    key_side = bool(re.search(r"api_key|key|oauth|email", primary, re.I))
    key_blob = " ".join(str(v) for v in ident.values()) + " " + str(ident.get("note") or "")
    key_declares = bool(re.search(
        r"is_real_external|externality|no external(ity)? filter|not externally filtered",
        key_blob, re.I))
    out.append(_check(
        "f_key_cohort_declares_population",
        "the key-based cohorts declare their externality filter",
        (not key_side) or key_declares,
        ("primary_metric=%r is key-based, but identity_breakdown (%s) never "
         "states whether its denominators are externality-filtered. They are "
         "NOT: is_real_external is applied to agent_cohort only. So the "
         "headline retention rate is computed over a population that is not "
         "the agent population the rest of the page reports."
         % (primary[:80], ", ".join(sorted(k for k in ident if k != "note"))))
        if key_side and not key_declares else
        "key_side=%s declares=%s" % (key_side, key_declares),
        critical=True))

    agent_declares = bool(re.search(r"is_real_external|CANONICAL GRAIN|population",
                                    str(r.get("agent_cohort_note") or ""), re.I))
    out.append(_check(
        "f_agent_cohort_still_declares",
        "the agent-grain cohort still declares its population (control)",
        agent_declares,
        "agent_cohort_note declares its grain: %s — this check is the CONTROL. "
        "If it ever fails, the lane above is not discriminating between the "
        "two sides, it is just reporting that all notes vanished."
        % agent_declares))
    return out


# ── LANE G — a gateway must not be summed into demand undisclosed ──────────
def _lane_gateway_disclosure(ctx: dict) -> list:
    """One Smithery gateway egress IP is >50% of post-correction call volume and
    has produced 0 conversions across its whole lifetime. It is legitimately in
    the canonical population (top_caller_note says gateways are deliberately not
    excluded) — but a single gateway supplying the majority of a number labelled
    as agent demand has to be visible where the number is.

    INVARIANT: when one caller exceeds a declared share of external calls, the
    payload must publish that share AND a gateway-excluded variant, so a reader
    can see demand without it. Not a cap on the gateway — a disclosure rule.
    """
    f = ctx.get("funnel")
    if not isinstance(f, dict):
        return [_check("g_read", "funnel readable", None,
                       "could not read /api/v1/mcp/funnel", critical=True)]
    pct = _num(f.get("top_caller_pct_7d"))
    client = str(f.get("top_caller_client") or f.get("top_caller_platform") or "")
    out = [_check("g_share_published", "the top caller's share is published",
                  pct is not None, "top_caller=%s pct=%s" % (client or "?", pct),
                  critical=True)]
    if pct is None:
        return out
    out.append(_check("g_note_published",
                      "the payload states whether gateways are excluded",
                      bool(f.get("top_caller_note")),
                      "top_caller_note=%s" % str(f.get("top_caller_note"))[:100]))
    dominant = pct >= 25.0
    excl = None
    for k in ("real_external_calls_7d_excl_top_caller",
              "real_external_calls_7d_excl_gateways",
              "real_external_agents_7d_excl_top_caller"):
        excl = _num(f.get(k))
        if excl is not None:
            break
    out.append(_check(
        "g_excluded_variant_published",
        "a gateway-excluded variant is published when one caller dominates",
        (not dominant) or (excl is not None),
        ("%s is %.1f%% of external calls and no *_excl_top_caller variant is "
         "published, so the headline cannot be read without it. The gateway is "
         "legitimately in the population; the point is that a reader must be "
         "able to see the number with it removed." % (client or "top caller", pct))
        if dominant and excl is None else
        "pct=%s excluded_variant=%s" % (pct, excl),
        critical=True))
    return out


# ── LANE H — published prose must not contradict its own payload ───────────
def _lane_prose_vs_data(ctx: dict) -> list:
    """The funnel dashboard asserts "Inflow is fine; retention is the leak."
    Inflow was 72.1% DC Hub's own GitHub Actions. The sentence was true of the
    inflated population and became false the moment #202 landed — prose does not
    recompute.

    INVARIANT: a static claim about a metric must not survive a correction to
    that metric. Fires when the page asserts inflow is healthy while the payload
    declares the agent population superseded.
    """
    html = _repo_text("static/mcp-dashboard.html")
    f = ctx.get("funnel")
    if html is None:
        return [_check("h_read", "dashboard source readable", None,
                       "could not read static/mcp-dashboard.html", critical=True)]
    claims_inflow_fine = bool(re.search(r"inflow\s+is\s+fine", html, re.I))
    comp = (f or {}).get("real_external_complete_wk_comparability") or {}
    superseded = bool(comp.get("superseded_by_correction"))
    out = [_check("h_payload_readable", "funnel readable for cross-check",
                  isinstance(f, dict), "funnel=%s" % ("ok" if f else "None"),
                  critical=True)]
    out.append(_check(
        "h_no_stale_inflow_claim",
        "the page does not assert inflow is healthy over a superseded population",
        not (claims_inflow_fine and superseded),
        ("static/mcp-dashboard.html asserts \"Inflow is fine; retention is the "
         "leak\" while the payload reports the agent population superseded by a "
         "correction that removed 72.1% of agents as our own CI. The inflow "
         "that sentence describes was mostly ours.")
        if (claims_inflow_fine and superseded) else
        "inflow_claim=%s superseded=%s" % (claims_inflow_fine, superseded),
        critical=True))
    return out


_LANES = [
    ("press_level", "A published LEVEL vs a superseded population", _lane_press_level),
    ("backup_health", "Backup feeds are actually healthy", _lane_backup_health),
    ("wall_reachability", "Upgrade signals have a mechanism that can deny", _lane_wall_reachability),
    ("conversion_honesty", "Headline conversions use the honest filter", _lane_conversion_honesty),
    ("identity_label", "'identified' means identified", _lane_identity_label),
    ("retention_population", "Every cohort declares its denominator", _lane_retention_population),
    ("gateway_disclosure", "A dominant gateway is disclosed", _lane_gateway_disclosure),
    ("prose_vs_data", "Published prose matches its payload", _lane_prose_vs_data),
]


def _run() -> dict:
    """One fetch round shared by all lanes.

    ★ BUDGET. Admin routes through Cloudflare die at 15s (ROUTE_TIMEOUTS
    DEFAULT). Per-lane fetching would multiply one endpoint across several
    lanes; three reads at an 8s cap keeps the whole tick inside the ceiling.
    """
    ctx = {
        "funnel": _get_json("/api/v1/mcp/funnel"),
        "deadman": _get_json("/api/v1/ops/deadman"),
        "retention": _get_json("/api/v1/mcp/retention"),
    }
    lanes = []
    for lid, name, fn in _LANES:
        try:
            checks = fn(ctx)
        except Exception as e:              # a lane must never 500 the shell
            checks = [_check("%s_crash" % lid, name, None,
                             "%s: %s" % (type(e).__name__, str(e)[:120]),
                             critical=True)]
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    return {
        "shell": 54,
        "title": "Published truth",
        "correction_ref": "dchub-mcp-server#202",
        "correction_effective_at": _CORRECTION,
        "lanes": lanes,
        "summary": " ".join("%s=%s" % (l["id"], l["verdict"]) for l in lanes),
        "any_fail": any(l["verdict"] == "FAIL" for l in lanes),
        "any_unmeasured": any(l["verdict"] == "?" for l in lanes),
        "note": ("`?` means a lane verified NOTHING — it is not a pass. This "
                 "shell is REPORT-ONLY and was BORN RED on 2026-08-20: every "
                 "lane pins a defect that was measured failing that day. Lanes "
                 "assert INVARIANTS, not values, so a lane goes green only when "
                 "the surface changes — never because a number drifted."),
    }


@published_truth_master_shell_bp.route(
    "/api/v1/admin/published-truth-shell/master-tick", methods=["GET"])
def master_tick():
    if _disabled():
        return jsonify({"ok": False, "disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_run()), 200


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@published_truth_master_shell_bp.route("/admin/published-truth-shell", methods=["GET"])
@published_truth_master_shell_bp.route("/api/v1/admin/published-truth-shell",
                                       methods=["GET"])
def board():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    d = _run()
    color = {"PASS": "#16a34a", "FAIL": "#dc2626", "?": "#eab308"}
    rows = []
    for ln in d["lanes"]:
        items = "".join(
            "<li><b>%s</b> — %s <i>%s</i></li>"
            % ({True: "PASS", False: "FAIL", None: "?"}[c["pass"]],
               _esc(c["name"]), _esc(c["detail"]))
            for c in ln["checks"])
        rows.append(
            "<section style='margin:14px 0;padding:10px;border-left:4px solid %s'>"
            "<h3 style='margin:0 0 6px'>%s — <span style='color:%s'>%s</span></h3>"
            "<ul style='margin:0'>%s</ul></section>"
            % (color.get(ln["verdict"], "#666"), _esc(ln["name"]),
               color.get(ln["verdict"], "#666"), ln["verdict"], items))
    return (
        "<html><head><meta charset='utf-8'><title>Shell 54 — Published truth</title>"
        "</head><body style='font:14px/1.5 system-ui;max-width:900px;margin:24px auto'>"
        "<h1>Shell #54 — Published truth</h1>"
        "<p style='color:#666'>%s</p><p><code>%s</code></p>%s</body></html>"
        % (_esc(d["note"]), _esc(d["summary"]), "".join(rows))), 200, \
        {"Content-Type": "text/html; charset=utf-8"}
