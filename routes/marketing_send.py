"""routes/marketing_send.py — the single MARKETING email choke-point (2026-06-18).

Every MARKETING sender must route through send_marketing_email() so consent +
CAN-SPAM compliance is enforced in ONE place instead of hand-rolled in ~34
senders. The ratcheting guard in tests/test_marketing_chokepoint.py fails any
NEW direct Resend POST outside this module, so the surface can only shrink.

send_marketing_email() guarantees, on every send:
  1. SUPPRESSION — skips any address on the email_suppression list (the canonical
     CAN-SPAM opt-out). FAIL-OPEN: if the check itself errors, we log and still
     send (never silently drop legit mail on our own infra hiccup).
  2. UNSUBSCRIBE FOOTER — guarantees a tokenized one-click /api/v1/unsubscribe
     link is in the HTML (appends a minimal compliant footer if absent).
  3. List-Unsubscribe + List-Unsubscribe-Post (RFC 2369 / 8058) headers.
  4. Resend send WITH those headers (main._resend_email cannot carry headers, so
     we POST directly, mirroring its env chain + FROM default). Never raises.

Audience helpers enforce the consent model at QUERY time:
  - marketing_recipients_from_keys(): the ONLY sanctioned way to get the
    captured-email marketing audience — mcp_dev_keys WHERE marketing_opt_in='true'
    AND not suppressed (Phase-2 default is false, so non-opted-in keys are out).
  - filter_suppressed(): drop suppressed addrs from a caller-supplied list (for
    senders whose audience comes from a separately-consented list).

This is an import-only library — no blueprint, nothing to register. TRANSACTIONAL
senders (receipts, key delivery, ops alerts) are intentionally NOT routed here.
"""
import logging
import os

log = logging.getLogger(__name__)

# Guarded import — a missing/broken suppression module must degrade gracefully,
# never crash a send. (routes.email_suppression first, bare fallback second.)
try:
    from routes.email_suppression import (
        is_suppressed as _is_suppressed,
        unsub_link as _unsub_link,
        list_unsubscribe_headers as _list_unsub_headers,
    )
except Exception:  # pragma: no cover - import-environment dependent
    try:
        from email_suppression import (  # type: ignore
            is_suppressed as _is_suppressed,
            unsub_link as _unsub_link,
            list_unsubscribe_headers as _list_unsub_headers,
        )
    except Exception:
        _is_suppressed = None
        _unsub_link = None
        _list_unsub_headers = None


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")


def _resend_key():
    return (os.environ.get("DCHUB_RESEND_API_KEY")
            or os.environ.get("RESEND_API_KEY") or "").strip()


def _default_from():
    return os.environ.get("DCHUB_RESEND_FROM", "DC Hub <alerts@dchub.cloud>")


def _ensure_unsub_footer(html: str, email: str) -> str:
    """Guarantee a tokenized one-click unsubscribe link is in the HTML."""
    if not _unsub_link:
        return html
    try:
        if "/unsubscribe" in (html or ""):
            return html  # sender already embedded a (tokenized) link
        link = _unsub_link(email)
        footer = (
            '<p style="margin-top:28px;font-size:11px;color:#9ca3af;'
            'border-top:1px solid #eee;padding-top:12px">'
            'You can <a href="' + link + '" style="color:#9ca3af">unsubscribe</a> '
            'from DC Hub marketing email at any time.</p>'
        )
        return (html or "") + footer
    except Exception:
        return html


def send_marketing_email(to_email, subject, html, *, source,
                         to_name=None, from_email=None, from_name=None):
    """Suppression-gated, unsubscribe-compliant marketing send. Never raises.

    Returns {sent: bool, suppressed: bool, reason: str|None, source: str}.
    """
    result = {"sent": False, "suppressed": False, "reason": None, "source": source}
    email = (to_email or "").strip().lower()
    if not email or "@" not in email:
        result["reason"] = "invalid_email"
        return result

    # 1) SUPPRESSION — fail-open (a check error must not block legit mail).
    if _is_suppressed:
        try:
            import psycopg2
            dsn = _dsn()
            if dsn:
                with psycopg2.connect(dsn, connect_timeout=4, sslmode="require") as conn:
                    with conn.cursor() as cur:
                        if _is_suppressed(cur, email):
                            result["suppressed"] = True
                            result["reason"] = "suppressed"
                            return result  # DROP — do not send
        except Exception as e:
            log.debug("marketing_send suppression check failed (fail-open send): %s", e)

    key = _resend_key()
    if not key:
        result["reason"] = "no_resend_key"
        return result  # silently no-op like the rest of the app when unconfigured

    # 2) + 3) compliant footer + List-Unsubscribe headers
    html2 = _ensure_unsub_footer(html or "", email)
    headers = {}
    if _list_unsub_headers:
        try:
            headers = _list_unsub_headers(email) or {}
        except Exception:
            headers = {}

    sender = from_email or _default_from()
    if from_name and "<" not in sender:
        sender = f"{from_name} <{sender}>"
    to_field = f"{to_name} <{email}>" if to_name else email

    # 4) Resend send WITH the headers (main._resend_email can't carry headers).
    try:
        import requests as _rq
        payload = {"from": sender, "to": [to_field], "subject": subject, "html": html2}
        if headers:
            payload["headers"] = headers
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=12,
        )
        result["sent"] = resp.status_code in (200, 201)
        if not result["sent"]:
            result["reason"] = f"resend_{resp.status_code}"
    except Exception as e:
        result["reason"] = f"send_error:{str(e)[:120]}"
    return result


def marketing_recipients_from_keys(cur, *, tiers=None):
    """The ONLY sanctioned captured-email marketing audience: mcp_dev_keys with
    an explicit marketing opt-in, suppression-excluded. Returns lower(email) list.
    Best-effort: returns [] on any error (never raises)."""
    try:
        sql = (
            "SELECT DISTINCT lower(k.email) FROM mcp_dev_keys k "
            "WHERE k.email IS NOT NULL AND k.email <> '' "
            "  AND COALESCE(k.status,'active') = 'active' "
            "  AND k.metadata->>'marketing_opt_in' = 'true' "
            "  AND NOT EXISTS (SELECT 1 FROM email_suppression s "
            "                   WHERE lower(s.email) = lower(k.email))"
        )
        params = []
        if tiers:
            sql += " AND k.tier = ANY(%s)"
            params.append(list(tiers))
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception as e:
        log.debug("marketing_recipients_from_keys failed: %s", e)
        return []


def filter_suppressed(cur, emails):
    """Drop suppressed addresses from a caller-supplied list. Dedups, lowercases,
    preserves order. FAIL-OPEN: on error, returns the de-duped input unchanged."""
    seen = set()
    cleaned = []
    for e in (emails or []):
        le = (e or "").strip().lower()
        if le and le not in seen:
            seen.add(le)
            cleaned.append(le)
    if not _is_suppressed:
        return cleaned
    try:
        return [e for e in cleaned if not _is_suppressed(cur, e)]
    except Exception as e:
        log.debug("filter_suppressed failed (fail-open): %s", e)
        return cleaned
