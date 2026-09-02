#!/usr/bin/env python3
"""
tests/test_marketing_chokepoint.py — RATCHETING marketing-email choke-point guard.

WHY THIS EXISTS
===============
The live Flask backend has ~45 email senders that each hand-roll their own
Resend POST (literal `https://api.resend.com/emails`), audience SQL, footer,
and unsubscribe. There is NO central governance, so CAN-SPAM / consent
compliance cannot be proven for MARKETING mail. The durable fix is a single
choke-point — `routes/marketing_send.py::send_marketing_email(...)` — that
EVERY marketing sender routes through. It enforces:
  - consent (mcp_dev_keys metadata->>'marketing_opt_in' == 'true', default false)
  - suppression (routes/email_suppression.is_suppressed)
  - List-Unsubscribe headers + unsub footer (routes/email_suppression)
  - one auditable Resend call (delegating to main._resend_email)

THE RATCHET
===========
We cannot migrate ~45 senders in one PR. So this guard is a RATCHET, not a
big-bang gate. `_KNOWN_DIRECT_SENDERS` below is the baseline allow-list of
every file that hits api.resend.com/emails DIRECTLY *today*. The test FAILS
only if a NEW file (not in the baseline, and not the choke-point itself)
introduces a direct Resend POST — i.e. a fresh bypass. As senders are
migrated, delete them from the baseline; the ratchet then prevents
back-sliding (you can never re-add a direct call to a migrated file without
the maintainer consciously editing this allow-list).

Each baseline entry is tagged:
  MARKETING     — promo / nurture / upgrade / digest / outreach mail. SHOULD
                  migrate to send_marketing_email. Counts toward the backlog.
  TRANSACTIONAL — receipts, key/redeem delivery, enterprise inquiries,
                  ops/build alerts, operator's personal report, feedback
                  routing, click-tracking. NOT marketing; allowed to stay
                  direct permanently. Does NOT count toward the backlog.

This is a PURE-STATIC guard: it reads files off disk and greps for the
literal Resend endpoint. NO DB, NO network — GitHub Actions has no
DATABASE_URL, so it must never touch one.

Run standalone:   python3 tests/test_marketing_chokepoint.py
Run under pytest: pytest tests/test_marketing_chokepoint.py
"""

import os
import re
import sys

# ---------------------------------------------------------------------------
# Repo geometry. This file lives at <repo>/tests/, so the repo root is one up.
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
ROUTES_DIR = os.path.join(REPO_ROOT, "routes")
MAIN_PY = os.path.join(REPO_ROOT, "main.py")

# The literal that identifies a hand-rolled Resend POST. Tolerate http/https
# and an optional trailing slash so a sender can't sneak past on formatting.
_RESEND_RE = re.compile(r"https?://api\.resend\.com/emails/?", re.IGNORECASE)

# The one file allowed (in fact REQUIRED) to call Resend directly: the
# marketing choke-point itself. It does not exist yet — it is the migration
# target — but exempt it now so it never trips this guard once built.
CHOKEPOINT_BASENAME = "marketing_send.py"

# ---------------------------------------------------------------------------
# RATCHET BASELINE — every file that contains a direct api.resend.com/emails
# call as of 2026-06-18. Snapshot via:
#     grep -rl "api.resend.com/emails" routes/ main.py | xargs -n1 basename | sort
#
# Classification (MARKETING vs TRANSACTIONAL) is judgment based on each file's
# docstring/purpose. MARKETING entries are the migration backlog; TRANSACTIONAL
# entries are allowed to keep sending directly forever.
#
# When you migrate a MARKETING sender to send_marketing_email(), DELETE its
# entry here. The backlog count (asserted below) drops, and the ratchet
# prevents the direct call from ever coming back silently.
# ---------------------------------------------------------------------------

# --- MARKETING senders: SHOULD migrate to routes/marketing_send.py ----------
_MARKETING_SENDERS = frozenset({
    "ai_lab_outreach.py",            # campaign outreach to AI labs / GPU clouds
    "ai_platform_onboarder.py",      # AI-agent expansion onboarding emails
    "auto_interconnect.py",          # NOTIFY stage pings agents/partners (promo)
    "brain_layer13_upgrade_nudge.py",# free->paid upgrade nudge to MCP users
    "brain_weekly_digest.py",        # weekly strategic-brain digest blast
    "broadcast.py",                  # free-tier broadcast + Media announcements
    "campaign_halfprice_annual.py",  # half-price Pro Annual upgrade campaign
    "campaign_outcome_poller.py",    # campaign follow-up / outcome mail
    "dchub_media_hub.py",            # media-relations / agent-encouragement blasts
    "dcpi_digest.py",                # DCPI weekly digest
    "digest.py",                     # daily DC market digest (Bloomberg-style)
    "enterprise_leads_sweep.py",     # outbound to high-intent free-tier leads
    "founding_customers.py",         # founding-customer outreach/follow-up
    "free_upgrade_nudge.py",         # automated nudge to active free-key users
    "lost_conversion_outreach.py",   # win-the-conversion nurture outreach
    "lp_alerts_cron.py",             # L+P saved-site alert emails (marketing-ish)
    "market_alerts.py",              # market-movement alert emails
    "marketing_engine.py",           # autonomous marketing engine
    "mcp_conversion_plays.py",       # MCP conversion-play outreach
    "media_outreach.py",             # journalist/media outreach pipeline
    "monthly_outreach.py",           # monthly trend outreach
    "outreach_cron.py",              # follow-up to abandoned-checkout leads (NOTE:
                                     # sends via SMTP smtp.resend.com, not the
                                     # HTTPS api.resend.com/emails endpoint, so it
                                     # does NOT trip the HTTPS regex / backlog count
                                     # — but it IS a marketing sender that bypasses
                                     # the choke-point and still needs migrating)
    "pockets.py",                    # Pockets-of-Power personalized recs email
    "press_outreach.py",            # journalist outreach pitches
    "press_queue.py",                # auto-drafted press-release mail
    "renewal_nudge.py",              # day-330 renewal nudge to annual buyers
    "sales_outreach_automator.py",   # founder-led sales outreach
    "upgrade_nudger.py",             # tier-upgrade nudger
    "upgrade_pool_outreach.py",      # MCP upgrade-pool outreach engine
    "watchlist_dispatcher.py",       # watchlist verdict-shift alert fan-out
    "weekly_digest.py",              # weekly market digest + payment ask
    "weekly_movement_digest.py",     # weekly DCPI movement digest
    "weekly_newsletter.py",          # DC Hub Weekly newsletter
    "winback_outreach.py",           # winback outreach delivery loop
})

# --- TRANSACTIONAL senders: NOT marketing; allowed to stay direct -----------
_TRANSACTIONAL_SENDERS = frozenset({
    "main.py",                       # shared _resend_email + transactional/ops mail
    "paid_intent_ledger.py",         # weekly grid/fiber intent digest to the OPERATOR
                                     # (ops report, recipients=owner) — mirrors
                                     # brain_weekly_digest/morning_briefing, not customer marketing
    "brain_innovation_email.py",     # operator innovation digest — transactional, not marketing
    "enterprise_inquiry.py",         # enterprise data-licensing inquiry receipts
    "feedback_triage.py",            # routes /feedback to operator (ops, not promo)
    "agent_adoption_master_shell.py",  # operator agent-adoption digest — admin-gated
                                     # POST /api/v1/admin/agent-adoption/digest, recipients
                                     # are DCHUB_BRIEFING_EMAIL / BRAIN_DIGEST_EMAIL and
                                     # default to the OWNER inbox. Same shape as
                                     # growth_ops_digest/paid_intent_ledger: an ops report
                                     # to the operator, never customer marketing.
    "growth_ops_digest.py",          # operator growth-ops digest (wins/watch/red-lane
                                     # report to the OWNER inbox, admin-gated
                                     # /admin/growth-digest/send) — mirrors
                                     # paid_intent_ledger/morning_briefing: an ops
                                     # report to the operator, not customer marketing
    "shortlists.py",                 # user-configured shortlist DRIFT alert: fires only
                                     # when the caller's OWN saved sites cross the
                                     # threshold they set (opt-in notify_email, event-
                                     # triggered) — a transactional alert, not promo
    "health_alerter.py",             # ops alert on backend restart-loop/health (to operator)
    "morning_briefing.py",           # operator's PERSONAL CEO daily report (self)
    "outreach_cap_exceeded.py",      # reactive limit-hit -> transactional notice
    "redeem_diagnostic.py",          # diagnostic test-send for the redeem flow
    "redeem_routes.py",              # delivers the redeemed key (transactional)
    "redeem_tracking.py",            # redeem-link delivery/tracking (transactional)
    "render_build_monitor.py",       # ops alert on failed Render build (to operator)
    "site_sentinel.py",              # page-health/error ops alerts (to operator)
    "state_visitor_claim.py",        # delivers the trial key the visitor claimed
    "activation_emails.py",          # paid-customer ACTIVATION (day-1 first query
                                     # with their connector URL, day-3 no-usage
                                     # nudge): onboarding of a customer who has
                                     # already paid, one send per step per
                                     # customer by ledger, founder reply-to. Same
                                     # class as the welcome/key delivery. DARK
                                     # until ACTIVATION_EMAILS_ENABLED == "1".
    "upgrade_outreach.py",           # draft-and-approve outreach engine: its
                                     # TRANSACTIONAL path (keyless_payer +
                                     # at_risk_paid) sends via
                                     # email_fallback.send_email_resilient — it
                                     # contains NO direct api.resend.com/emails
                                     # POST. Its MARKETING path (comped_paid +
                                     # high_usage_free) routes through
                                     # routes/marketing_send.send_marketing_email,
                                     # so it is NOT a new direct-Resend bypass.
})

# The full ratchet baseline: a file in EITHER bucket may call Resend directly.
_KNOWN_DIRECT_SENDERS = _MARKETING_SENDERS | _TRANSACTIONAL_SENDERS

# Informational: how many MARKETING senders still hit the HTTPS Resend endpoint
# directly today and thus need migrating. This pins the backlog so a NEW
# marketing sender added to the baseline is a conscious, reviewable bump — not a
# silent ratchet of the debt upward.
#
# NOTE: this is 33, not len(_MARKETING_SENDERS) (34), because outreach_cron.py is
# classified MARKETING but currently sends via SMTP (smtp.resend.com), so it does
# not match the HTTPS api.resend.com/emails regex the live backlog counts. It
# still bypasses the choke-point and should migrate; it just isn't counted here.
_MIGRATION_BACKLOG_BASELINE = 33


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _candidate_files():
    """Every routes/*.py plus main.py — the surface we police."""
    files = []
    if os.path.isdir(ROUTES_DIR):
        for name in os.listdir(ROUTES_DIR):
            if name.endswith(".py"):
                files.append(os.path.join(ROUTES_DIR, name))
    if os.path.isfile(MAIN_PY):
        files.append(MAIN_PY)
    return sorted(files)


def _has_direct_resend_call(path):
    """True if the file contains a literal api.resend.com/emails reference."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return bool(_RESEND_RE.search(fh.read()))
    except OSError:
        return False


def _find_new_bypasses():
    """
    Return basenames of files that call Resend directly but are NOT in the
    ratchet baseline and are NOT the choke-point. A non-empty result == a NEW
    bypass that must be caught.
    """
    offenders = []
    for path in _candidate_files():
        base = os.path.basename(path)
        if base == CHOKEPOINT_BASENAME:
            continue
        if base in _KNOWN_DIRECT_SENDERS:
            continue
        if _has_direct_resend_call(path):
            offenders.append(base)
    return sorted(offenders)


def _current_marketing_backlog():
    """
    MARKETING-classified files that STILL call Resend directly (i.e. not yet
    migrated). This is the live migration backlog.
    """
    backlog = []
    for path in _candidate_files():
        base = os.path.basename(path)
        if base in _MARKETING_SENDERS and _has_direct_resend_call(path):
            backlog.append(base)
    return sorted(backlog)


# ---------------------------------------------------------------------------
# pytest-compatible tests
# ---------------------------------------------------------------------------
def test_no_new_resend_bypass():
    """
    RATCHET: no NEW file may hand-roll a Resend POST. Existing baseline senders
    are grandfathered; the choke-point is exempt. Any other file with a direct
    api.resend.com/emails call is a fresh bypass and fails the build.
    """
    offenders = _find_new_bypasses()
    assert not offenders, (
        "NEW direct Resend bypass(es) detected — route marketing mail through "
        "routes/marketing_send.py::send_marketing_email() instead of calling "
        "https://api.resend.com/emails directly. Offending file(s): "
        + ", ".join(offenders)
        + ". (If this is genuinely TRANSACTIONAL — a receipt, key delivery, "
        "ops alert, etc. — add it to _TRANSACTIONAL_SENDERS in this test with "
        "a justifying comment.)"
    )


def test_marketing_migration_backlog_is_tracked():
    """
    INFORMATIONAL ratchet: the count of un-migrated MARKETING senders must not
    exceed the recorded baseline. Migrating a sender (and deleting its entry)
    lowers the live backlog below baseline — fine. Adding a brand-new marketing
    sender to the baseline must be a conscious bump of _MIGRATION_BACKLOG_BASELINE.
    """
    backlog = _current_marketing_backlog()
    assert len(backlog) <= _MIGRATION_BACKLOG_BASELINE, (
        "Marketing migration backlog GREW (%d > baseline %d). New marketing "
        "senders must go through the choke-point, not the direct-Resend "
        "allow-list. Un-migrated: %s"
        % (len(backlog), _MIGRATION_BACKLOG_BASELINE, ", ".join(backlog))
    )


def test_baseline_buckets_are_disjoint():
    """Sanity: a file can't be both MARKETING and TRANSACTIONAL."""
    overlap = _MARKETING_SENDERS & _TRANSACTIONAL_SENDERS
    assert not overlap, "Baseline buckets overlap: %s" % ", ".join(sorted(overlap))


def test_chokepoint_exists_and_defines_send():
    """The choke-point the ratchet EXEMPTS must actually exist and define
    send_marketing_email — otherwise the exemption is hollow and the file can
    silently vanish while the suite stays green (which is exactly how a fabricated
    'I wrote it' build report slipped through once)."""
    path = os.path.join(REPO_ROOT, "routes", CHOKEPOINT_BASENAME)
    assert os.path.isfile(path), (
        "Choke-point routes/%s is MISSING — the send_marketing_email() library "
        "must exist for the ratchet exemption to mean anything." % CHOKEPOINT_BASENAME
    )
    with open(path, "r", encoding="utf-8") as _fh:
        src = _fh.read()
    assert "def send_marketing_email" in src, (
        "routes/%s exists but does not define send_marketing_email()." % CHOKEPOINT_BASENAME
    )


# ---------------------------------------------------------------------------
# Standalone runner — prints a report + sets exit code.
# ---------------------------------------------------------------------------
def _main():
    print("=" * 72)
    print("MARKETING EMAIL CHOKE-POINT GUARD (ratcheting static check)")
    print("=" * 72)
    print("repo root          : %s" % REPO_ROOT)
    print("choke-point exempt : routes/%s" % CHOKEPOINT_BASENAME)
    print(
        "baseline senders   : %d (%d marketing, %d transactional)"
        % (
            len(_KNOWN_DIRECT_SENDERS),
            len(_MARKETING_SENDERS),
            len(_TRANSACTIONAL_SENDERS),
        )
    )

    backlog = _current_marketing_backlog()
    print(
        "migration backlog  : %d un-migrated MARKETING senders "
        "(baseline cap %d)" % (len(backlog), _MIGRATION_BACKLOG_BASELINE)
    )

    ok = True

    # Check 0: the exempted choke-point must actually exist + define the entrypoint.
    _cp = os.path.join(REPO_ROOT, "routes", CHOKEPOINT_BASENAME)
    if not os.path.isfile(_cp) or "def send_marketing_email" not in open(_cp, encoding="utf-8").read():
        ok = False
        print("\n[FAIL] choke-point routes/%s missing or lacks send_marketing_email()" % CHOKEPOINT_BASENAME)
    else:
        print("\n[ok]   choke-point routes/%s present + defines send_marketing_email" % CHOKEPOINT_BASENAME)

    # Check 1: disjoint buckets.
    overlap = _MARKETING_SENDERS & _TRANSACTIONAL_SENDERS
    if overlap:
        ok = False
        print("\n[FAIL] baseline buckets overlap: %s" % ", ".join(sorted(overlap)))

    # Check 2: no new bypass.
    offenders = _find_new_bypasses()
    if offenders:
        ok = False
        print("\n[FAIL] NEW direct Resend bypass(es) — must use the choke-point:")
        for base in offenders:
            print("         - %s" % base)
    else:
        print("\n[ok]   no new direct-Resend bypass outside the baseline")

    # Check 3: backlog not above baseline.
    if len(backlog) > _MIGRATION_BACKLOG_BASELINE:
        ok = False
        print(
            "\n[FAIL] marketing backlog grew (%d > %d):"
            % (len(backlog), _MIGRATION_BACKLOG_BASELINE)
        )
        for base in backlog:
            print("         - %s" % base)
    else:
        print(
            "[ok]   marketing backlog within baseline "
            "(%d <= %d)" % (len(backlog), _MIGRATION_BACKLOG_BASELINE)
        )

    print("\n" + "=" * 72)
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
