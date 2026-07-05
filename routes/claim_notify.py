"""
claim_notify.py — out-of-band operator notification on high-intent claim mint (2026-06-25).

WHY THIS EXISTS
---------------
The high-intent claim URL (https://dchub.cloud/claim/<token>) is only ever seen
by the calling AI agent inside the 402 paywall response. Agents rarely surface it
to a human, so minted claims produced ~0 human opens — the "dead conversion
machine" the brain flagged for 18+ days. That diagnosis had two real root causes:

  1. MEASUREMENT: nothing recorded page-opens until 2026-06-24 (commit e85066d2),
     so `page_viewed` read 0 regardless of reality. Fixed.
  2. DELIVERY: even when measured, the link depends on the agent pasting it to a
     human. This module closes #2 with an OUT-OF-BAND channel: the moment a fresh
     claim is minted, fire a fire-and-forget email/Slack to the OPERATOR so a human
     always sees real demand and can follow up — independent of the agent.

This is the working implementation of brain scaffold PRs #1263 / #1250, which
only shipped a 501 stub. Shadow→arm per the house pattern (cf. health_alerter
HEALTH_AUTORESET_ENABLE): a pure no-op unless DCHUB_CLAIM_NOTIFY=1 is set, so
merging changes nothing until the operator deliberately arms it.

SAFETY
------
  * Fully wrapped — must never raise into, or slow, the mint path. should_mint_claim
    is called inline by the mcp-server with the agent waiting, so delivery runs in a
    detached daemon thread and the caller returns immediately.
  * Idempotent — the caller guards on a claim_notified_at column (set once via a
    conditional UPDATE), so each claim notifies exactly once even under a race.
  * DB-independent send — reuses email_fallback.send_email_resilient (Resend-first,
    stdlib HTTP), the same path health_alerter uses during pool exhaustion.
"""
from __future__ import annotations
import os
import html as _html
import logging
import threading

log = logging.getLogger("claim_notify")


def _enabled() -> bool:
    """Master arm for operator notifications. Default OFF (shadow→arm)."""
    return str(os.environ.get("DCHUB_CLAIM_NOTIFY", "")).strip().lower() in ("1", "true", "yes")


def _enabled_on_mint() -> bool:
    """Mint-time operator ping. Default OFF as of 2026-07-04.

    Minting fires the moment a session crosses the high-intent threshold — which
    for a session-rotating anonymous agent (no human, no email) is pure noise: one
    real actor rotating session_ids produces N mints → N operator emails, none of
    them a contactable lead. The REAL signal — a captured email — is delivered by
    notify_operator_of_lead() at claim time instead. Set DCHUB_CLAIM_NOTIFY_ON_MINT=1
    to restore the old per-mint behavior."""
    return str(os.environ.get("DCHUB_CLAIM_NOTIFY_ON_MINT", "")).strip().lower() in ("1", "true", "yes")


def _operator_email() -> str:
    return (os.environ.get("DCHUB_OPERATOR_EMAIL")
            or os.environ.get("OPERATOR_EMAIL")
            or os.environ.get("DCHUB_ADMIN_EMAIL")
            or os.environ.get("ADMIN_ALERT_EMAIL")
            or "azmartone@gmail.com").strip()


def _slack_webhook() -> str:
    return (os.environ.get("DCHUB_CLAIM_SLACK_WEBHOOK")
            or os.environ.get("SLACK_WEBHOOK_URL") or "").strip()


def _send_email(to: str, subject: str, body_html: str) -> bool:
    try:
        from email_fallback import send_email_resilient
        return bool(send_email_resilient(
            to, subject, html_content=body_html,
            from_email=os.environ.get("SENDGRID_FROM_EMAIL", "alerts@dchub.cloud"),
            from_name="DC Hub Claim Notifier"))
    except Exception as e:
        log.warning("claim_notify: email send failed: %s", e)
        return False


def _post_slack(webhook: str, text: str) -> bool:
    try:
        import requests  # repo standardizes on requests (urllib egress is flaky on Railway)
        r = requests.post(webhook, json={"text": text}, timeout=8,
                          headers={"User-Agent": "dchub-claim-notify/1.0"})
        return r.status_code < 300
    except Exception as e:
        log.warning("claim_notify: slack post failed: %s", e)
        return False


def _deliver(sid: str, tool: str, token: str, count: int, variant: str | None) -> None:
    claim_url = "https://dchub.cloud/claim/%s" % token
    pricing_url = "https://dchub.cloud/pricing"
    sid_short = (str(sid or ""))[:12]
    h = _html.escape
    subject = "🛰️ High-intent claim minted — %s" % tool
    body = (
        "<h2>A real prospect just crossed the high-intent threshold</h2>"
        "<p>An AI agent hit a paywalled DC Hub tool enough times in 24h to mint a "
        "trial-key claim link. The link is in the agent's hands — a human may never "
        "see it. Here it is so you can follow up directly.</p>"
        "<table cellpadding='6' style='border-collapse:collapse;font-size:14px'>"
        "<tr><td><b>Tool</b></td><td>%s</td></tr>"
        "<tr><td><b>Paid calls (24h)</b></td><td>%d</td></tr>"
        "<tr><td><b>Platform variant</b></td><td>%s</td></tr>"
        "<tr><td><b>Session</b></td><td><code>%s…</code></td></tr>"
        "</table>"
        "<p><b>Claim page the human would open:</b><br>"
        "<a href='%s'>%s</a></p>"
        "<p><a href='%s'>Pricing / Stripe checkout</a> for direct follow-up.</p>"
        "<hr><p style='color:#888;font-size:12px'>DC Hub claim notifier · out-of-band "
        "delivery (the agent-facing link does not depend on the agent surfacing it). "
        "Set <code>DCHUB_CLAIM_NOTIFY=0</code> to silence.</p>"
        % (h(tool), int(count or 0), h(variant or "generic"), h(sid_short),
           claim_url, claim_url, pricing_url)
    )
    to = _operator_email()
    if to:
        _send_email(to, subject, body)
    wh = _slack_webhook()
    if wh:
        _post_slack(wh, "🛰️ High-intent claim minted — *%s* (calls=%d, variant=%s)\n%s"
                    % (tool, int(count or 0), variant or "generic", claim_url))


def notify_operator_of_claim(sid: str, tool: str, token: str,
                             count: int = 0, variant: str | None = None) -> None:
    """Fire-and-forget. Returns immediately, never raises, no-op unless armed.

    Call this exactly once per FRESH mint (the caller guards on claim_notified_at).
    """
    try:
        if not _enabled_on_mint() or not token:
            return
        threading.Thread(
            target=_deliver, args=(sid, tool, token, count, variant),
            name="claim-notify", daemon=True).start()
    except Exception as e:  # never let a notifier break the mint path
        try:
            log.warning("claim_notify: spawn failed: %s", e)
        except Exception:
            pass


# ── Lead notification — fires on EMAIL CAPTURE (a real, sellable lead) ────────
# The mint-time ping above is anonymous noise (see _enabled_on_mint). This one
# fires only when a human/agent actually binds an email on a claim — i.e. you now
# have someone to follow up with, and you know exactly which tool/market they
# wanted. Gated by the master DCHUB_CLAIM_NOTIFY. Fire-and-forget; never raises.

def _deliver_lead(email: str, tool: str, sid: str, variant: str | None, count: int) -> None:
    pricing_url = "https://dchub.cloud/pricing"
    sid_short = (str(sid or ""))[:12]
    h = _html.escape
    subject = "🟢 New DC Hub lead — %s (%s)" % (email, tool)
    body = (
        "<h2>A prospect just left you an email</h2>"
        "<p>Someone crossed the high-intent paywall on a DC Hub tool AND bound an "
        "email to claim a trial key. Unlike an anonymous mint, this is a "
        "<b>contactable lead</b> — reach out directly.</p>"
        "<table cellpadding='6' style='border-collapse:collapse;font-size:14px'>"
        "<tr><td><b>Email</b></td><td><a href='mailto:%s'>%s</a></td></tr>"
        "<tr><td><b>Wanted (tool)</b></td><td>%s</td></tr>"
        "<tr><td><b>Paid calls (24h)</b></td><td>%d</td></tr>"
        "<tr><td><b>Platform variant</b></td><td>%s</td></tr>"
        "<tr><td><b>Session</b></td><td><code>%s…</code></td></tr>"
        "</table>"
        "<p>They already have a 7-day trial key. Best follow-up: ask what site/market "
        "they're evaluating and point them at <a href='%s'>pricing</a> for full access.</p>"
        "<hr><p style='color:#888;font-size:12px'>DC Hub lead notifier · fires on email "
        "capture only. Set <code>DCHUB_CLAIM_NOTIFY=0</code> to silence.</p>"
        % (h(email), h(email), h(tool), int(count or 0), h(variant or "generic"),
           h(sid_short), pricing_url)
    )
    to = _operator_email()
    if to:
        _send_email(to, subject, body)
    wh = _slack_webhook()
    if wh:
        _post_slack(wh, "🟢 New DC Hub lead — *%s* wanted `%s` (calls=%d, variant=%s)"
                    % (email, tool, int(count or 0), variant or "generic"))


def notify_operator_of_lead(email: str, tool: str, sid: str = "",
                            variant: str | None = None, count: int = 0) -> None:
    """Fire-and-forget. Call once when an email is bound on a claim (the human form
    POST or the agent-redeem path with an email). No-op unless DCHUB_CLAIM_NOTIFY is
    armed AND an email is present. Never raises into the conversion path."""
    try:
        if not _enabled() or not email:
            return
        threading.Thread(
            target=_deliver_lead, args=(email, tool, sid, variant, count),
            name="lead-notify", daemon=True).start()
    except Exception as e:
        try:
            log.warning("claim_notify: lead spawn failed: %s", e)
        except Exception:
            pass
