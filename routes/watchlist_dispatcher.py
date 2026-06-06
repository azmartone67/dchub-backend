"""
watchlist_dispatcher.py — Watchlist alert fan-out (2026-06-06).

Sweep new DCPI verdict shifts → fan out alerts to every watcher.

Two cadences (PRO+ ships now; FREE queues for weekly):

  • PRO+ tier watchers → email + browser push NOW (twice-daily cron
    via _run_watchlist_realtime in crawler_scheduler.py).
  • FREE tier watchers → queued (status='pending') and shipped Monday
    14:00 UTC via _run_watchlist_weekly_digest.

The dispatcher REUSES the existing verdict-shift detector in
routes.market_verdict_shifts._detect_shifts so we share one source of
truth for "what shifted today" + dedupe via watchlist_alerts_sent
UNIQUE on (watchlist_id, shift_to, DATE(sent_at)).

Defensive everywhere — a missing Resend key, missing VAPID key, dead
push endpoint, malformed row, or DB hiccup never raises; we log and
move on so a single bad watcher never starves the others.

Public entry points
-------------------
dispatch_watchlist_alerts(mode='realtime') → realtime fan-out (PRO+)
dispatch_watchlist_alerts(mode='digest')   → weekly digest fan-out
                                             (FREE; only fires Mondays
                                             via the wrapper)

Both return a dict summary the crawler scheduler logs.
"""
from __future__ import annotations

import os
import sys
import json
import logging
import hashlib
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────
MAX_REALTIME_ALERTS_PER_RUN = 500   # belt-and-suspenders
MAX_DIGEST_ALERTS_PER_RUN   = 5000
REALTIME_TIERS = {"PRO", "FOUNDING", "ENTERPRISE", "RESEARCH_SEED", "ADMIN"}

FROM_EMAIL = os.environ.get("WATCHLIST_FROM_EMAIL",
                            "DC Hub Watchlist <alerts@dchub.cloud>")


# ── Plumbing (mirrors routes/watchlist.py) ────────────────────────────
def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[watchlist-dispatch] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _resend_key() -> str:
    return (os.environ.get("DCHUB_RESEND_API_KEY")
            or os.environ.get("RESEND_API_KEY") or "").strip()


def _is_monday(now: datetime | None = None) -> bool:
    n = now or datetime.now(timezone.utc)
    return n.weekday() == 0  # Mon == 0


# ── Detect new shifts (reuses the canonical detector) ─────────────────
def _detect_new_shifts(cur) -> list[dict]:
    """Defensive thin wrapper around market_verdict_shifts._detect_shifts.
    Returns [] on any failure rather than raising — the dispatcher must
    never crash the scheduler thread."""
    try:
        from routes.market_verdict_shifts import _detect_shifts as _ds
        return _ds(cur) or []
    except Exception as e:
        _log(f"detect_shifts_failed: {e}")
        return []


def _watchers_for_slug(cur, slug: str) -> list[dict]:
    """Active watchers for a market_slug. We re-resolve the watcher's
    CURRENT tier (their stored tier_at_signup is just metadata — the
    user might have upgraded since)."""
    out: list[dict] = []
    try:
        cur.execute("""
            SELECT id, owner_email_hash, owner_email_masked, channel,
                   tier_at_signup
              FROM user_watchlists
             WHERE market_slug      = %s
               AND unsubscribed_at IS NULL
        """, (slug,))
        for r in cur.fetchall() or []:
            out.append({
                "id":               int(r[0]),
                "email_hash":       r[1],
                "masked_email":     r[2] or "",
                "channel":          r[3] or "email",
                "tier_at_signup":   (r[4] or "FREE").upper(),
            })
    except Exception as e:
        _log(f"watchers_for_slug_failed: {e}")
    return out


def _resolve_email_from_hash(cur, email_hash: str) -> str | None:
    """We hash emails before storage, so to actually SEND we have to
    re-hash known emails from mcp_dev_keys and match. Best-effort —
    returns None if we can't recover the plaintext.

    A watcher who isn't in mcp_dev_keys is unreachable for now (next
    iteration: a separate `watchlist_unverified_emails` table that the
    /add endpoint writes to with verification tokens)."""
    if not email_hash:
        return None
    try:
        # Pull a small candidate pool (the DISTINCT emails table) and
        # match by hash. Bounded — mcp_dev_keys is small.
        cur.execute(
            "SELECT DISTINCT LOWER(email) FROM mcp_dev_keys "
            "WHERE email IS NOT NULL AND email <> ''"
        )
        salt = (os.environ.get("DCHUB_HASH_SALT") or "dchub-2026-salt-v1")
        for row in cur.fetchall() or []:
            e = (row[0] or "").strip().lower()
            if not e:
                continue
            h = hashlib.sha256((e + "|" + salt).encode("utf-8")).hexdigest()
            if h == email_hash:
                return e
    except Exception as e:
        _log(f"resolve_email_failed: {e}")
    return None


def _push_subs_for_email_hash(cur, email_hash: str) -> list[dict]:
    if not email_hash:
        return []
    try:
        cur.execute("""
            SELECT id, endpoint, p256dh, auth FROM browser_push_subscriptions
             WHERE subscriber_email_hash = %s
               AND revoked_at IS NULL
        """, (email_hash,))
        return [
            {"id": int(r[0]), "endpoint": r[1], "p256dh": r[2], "auth": r[3]}
            for r in cur.fetchall() or []
        ]
    except Exception as e:
        _log(f"push_subs_failed: {e}")
        return []


def _already_alerted(cur, watchlist_id: int, shift_to: str) -> bool:
    """One alert per (watchlist_id, shift_to) per day, regardless of
    channel — re-running the dispatcher must not double-send."""
    try:
        cur.execute("""
            SELECT 1 FROM watchlist_alerts_sent
             WHERE watchlist_id = %s AND shift_to = %s
               AND sent_at::date = CURRENT_DATE
             LIMIT 1
        """, (watchlist_id, (shift_to or "").upper()))
        return bool(cur.fetchone())
    except Exception:
        return False


def _record_alert(cur, watchlist_id: int, shift: dict, channel: str,
                  status: str = "sent") -> None:
    try:
        cur.execute("""
            INSERT INTO watchlist_alerts_sent
                   (watchlist_id, market_slug, shift_from, shift_to,
                    channel, status, sent_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (
            watchlist_id, shift.get("market_slug"),
            (shift.get("shift_from") or "").upper(),
            (shift.get("shift_to")   or "").upper(),
            channel, status,
        ))
    except Exception as e:
        _log(f"record_alert_failed: {e}")


# ── Email templates ───────────────────────────────────────────────────
def _verdict_pill(verdict: str) -> str:
    v = (verdict or "").upper()
    color = {"BUILD": "#10b981", "CAUTION": "#f59e0b",
             "AVOID": "#ef4444"}.get(v, "#94a3b8")
    return (f'<span style="background:{color};color:#fff;padding:3px 10px;'
            f'border-radius:6px;font-weight:600;font-size:13px;'
            f'letter-spacing:.04em">{v or "—"}</span>')


def _watchlist_realtime_email(shift: dict) -> tuple[str, str]:
    """Returns (subject, html) for a single-shift realtime email."""
    name = shift.get("market_name") or shift.get("market_slug") or "Your market"
    to_v = (shift.get("shift_to") or "").upper()
    fr_v = (shift.get("shift_from") or "").upper()
    slug = shift.get("market_slug") or ""
    brief_url = f"https://dchub.cloud/markets/{slug}/brief"
    dcpi_url  = f"https://dchub.cloud/dcpi/{slug}"
    unsub_url = (f"https://dchub.cloud/watchlist#unsub-slug={slug}")

    subject = f"🚨 {name} DCPI verdict shifted to {to_v} — your watchlist alert"
    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#0a0e1a">
<div style="max-width:560px;margin:0 auto;padding:32px 24px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            color:#e6ecf5">
  <div style="font-size:11px;color:#94a3b8;letter-spacing:.08em;
              text-transform:uppercase;margin-bottom:10px">
    DC Hub · Watchlist alert
  </div>
  <h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;color:#fff">
    {name} just shifted to {to_v}
  </h1>
  <div style="background:#111726;border:1px solid #1f2940;border-radius:10px;
              padding:18px 20px;margin:18px 0">
    <div style="font-size:13px;color:#94a3b8;margin-bottom:8px">
      DCPI verdict change
    </div>
    <div style="font-size:16px;line-height:1.5">
      {_verdict_pill(fr_v)} &nbsp;→&nbsp; {_verdict_pill(to_v)}
    </div>
  </div>
  <p style="color:#cbd5e1;font-size:15px;line-height:1.55;margin:18px 0">
    The full DCPI breakdown — constraint score, excess-power score, and
    what's driving the move — is in the Market Brief.
  </p>
  <p style="margin:24px 0">
    <a href="{brief_url}"
       style="background:#3da9fc;color:#fff;padding:12px 22px;
              border-radius:8px;text-decoration:none;font-weight:600;
              display:inline-block;font-size:14px">
      Read the {name} Market Brief →
    </a>
  </p>
  <p style="margin:14px 0">
    <a href="{dcpi_url}" style="color:#3da9fc;font-size:13px">
      Or see the raw DCPI scorecard →
    </a>
  </p>
  <hr style="border:0;border-top:1px solid #1f2940;margin:28px 0">
  <p style="font-size:12px;color:#94a3b8;line-height:1.5">
    You're on the DC Hub watchlist for <strong>{name}</strong>. This is
    a real-time alert because you're on a PRO+ plan.<br>
    <a href="{unsub_url}" style="color:#94a3b8">
      Stop watching this market
    </a>
    &nbsp;·&nbsp;
    <a href="https://dchub.cloud/watchlist" style="color:#94a3b8">
      Manage all alerts
    </a>
  </p>
</div></body></html>"""
    return subject, html


def _watchlist_weekly_digest_email(shifts: list[dict]) -> tuple[str, str]:
    """Returns (subject, html) for the weekly free-tier digest. `shifts`
    is the (possibly empty) batch of verdict shifts in the user's watched
    markets that occurred since the LAST digest."""
    rows = ""
    for s in shifts:
        name = s.get("market_name") or s.get("market_slug") or "Unknown"
        slug = s.get("market_slug") or ""
        rows += f"""
        <li style="margin:14px 0;padding:14px;background:#111726;
                   border:1px solid #1f2940;border-radius:8px;list-style:none">
          <div style="font-weight:600;font-size:15px;color:#fff;margin-bottom:6px">
            {name}
          </div>
          <div style="font-size:14px;line-height:1.4">
            {_verdict_pill(s.get('shift_from'))} &nbsp;→&nbsp; {_verdict_pill(s.get('shift_to'))}
          </div>
          <div style="margin-top:10px">
            <a href="https://dchub.cloud/markets/{slug}/brief"
               style="color:#3da9fc;font-size:13px;text-decoration:none">
              Read Market Brief →
            </a>
          </div>
        </li>"""
    if not rows:
        rows = ("<li style=\"color:#94a3b8;font-size:14px;padding:14px\">"
                "No verdict shifts in your watched markets this week — "
                "all clear.</li>")
    n = len(shifts)
    subject = "Your weekly DC Hub digest"
    if n:
        subject = (f"Your weekly DC Hub digest — {n} verdict "
                   f"shift{'s' if n != 1 else ''} in your markets")
    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#0a0e1a">
<div style="max-width:560px;margin:0 auto;padding:32px 24px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            color:#e6ecf5">
  <div style="font-size:11px;color:#94a3b8;letter-spacing:.08em;
              text-transform:uppercase;margin-bottom:10px">
    DC Hub · weekly digest
  </div>
  <h1 style="margin:0 0 12px;font-size:24px;color:#fff;line-height:1.3">
    Your watchlist this week
  </h1>
  <p style="color:#cbd5e1;font-size:14px;margin:0 0 22px">
    {n} verdict shift{'s' if n != 1 else ''} across the markets you watch.
    Upgrade to PRO for real-time alerts on every move.
  </p>
  <ul style="padding:0;margin:0 0 24px">{rows}</ul>
  <div style="margin:22px 0 30px">
    <a href="https://dchub.cloud/pricing"
       style="background:#3da9fc;color:#fff;padding:12px 22px;
              border-radius:8px;text-decoration:none;font-weight:600;
              display:inline-block;font-size:14px">
      Upgrade to PRO for real-time alerts →
    </a>
  </div>
  <hr style="border:0;border-top:1px solid #1f2940;margin:24px 0">
  <p style="font-size:12px;color:#94a3b8;line-height:1.5">
    You're on the DC Hub free-tier weekly digest.
    <a href="https://dchub.cloud/watchlist" style="color:#94a3b8">
      Manage your watchlist
    </a>
  </p>
</div></body></html>"""
    return subject, html


# ── Senders ───────────────────────────────────────────────────────────
def _send_email(to: str, subject: str, html: str) -> bool:
    """Send via Resend. Returns False (not raises) on any failure."""
    key = _resend_key()
    if not key:
        _log("send_email skipped: no DCHUB_RESEND_API_KEY")
        return False
    if not to:
        return False
    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [to], "subject": subject,
                  "html": html},
            timeout=12)
        return resp.status_code in (200, 201, 202)
    except Exception as e:
        _log(f"send_email_failed: {e}")
        return False


def _send_push(sub: dict, payload: dict) -> bool:
    """Send via pywebpush + VAPID. Returns False if VAPID isn't
    configured or pywebpush isn't installed — never raises."""
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not priv:
        return False
    try:
        from pywebpush import webpush, WebPushException  # type: ignore
    except Exception as e:
        _log(f"pywebpush import failed: {e}")
        return False
    claims = {"sub": os.environ.get("VAPID_SUBJECT", "mailto:alerts@dchub.cloud")}
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=priv,
            vapid_claims=claims,
            timeout=10,
        )
        return True
    except Exception as e:
        # Includes WebPushException — dead endpoints get logged + we'll
        # revoke them in a follow-up sweep.
        _log(f"send_push_failed: {e}")
        return False


# ── The fan-out ───────────────────────────────────────────────────────
def dispatch_watchlist_alerts(mode: str = "realtime") -> dict:
    """Main entry point. mode='realtime' fires PRO+ alerts now;
    mode='digest' fires the Monday FREE digest (consumer-pulls the
    pending queue + ships).

    Never raises. Returns a summary dict suitable for crawler logging."""
    started = datetime.now(timezone.utc)
    summary: dict = {
        "mode":              mode,
        "started_at":        started.isoformat(),
        "shifts_seen":       0,
        "watchers_total":    0,
        "alerts_sent":       0,
        "alerts_queued":     0,
        "alerts_skipped":    0,
        "push_sent":         0,
        "push_failed":       0,
        "errors":            [],
    }

    # Ensure tables exist (boot-init may not have run on this worker yet).
    try:
        from routes.watchlist import init_watchlist_tables
        init_watchlist_tables()
    except Exception as e:
        summary["errors"].append(f"init_tables: {e}")

    conn = _db_conn()
    if conn is None:
        summary["errors"].append("db_unavailable")
        return summary

    try:
        with conn.cursor() as cur:
            # ─── 1. Detect today's verdict shifts ─────────────────
            shifts = _detect_new_shifts(cur)
            summary["shifts_seen"] = len(shifts)
            if not shifts and mode == "realtime":
                # Realtime: no shifts → nothing to do.
                return summary

            # ─── 2. For each shift, fan out to watchers ────────────
            for shift in shifts[:MAX_REALTIME_ALERTS_PER_RUN]:
                slug = shift.get("market_slug")
                if not slug:
                    continue
                watchers = _watchers_for_slug(cur, slug)
                summary["watchers_total"] += len(watchers)
                for w in watchers:
                    try:
                        # Resolve current tier (might have upgraded)
                        from routes.watchlist import _lookup_tier
                        plaintext_email = _resolve_email_from_hash(
                            cur, w["email_hash"])
                        current_tier = (
                            _lookup_tier(plaintext_email) if plaintext_email
                            else w["tier_at_signup"])
                        is_realtime = current_tier in REALTIME_TIERS

                        if mode == "realtime" and not is_realtime:
                            # FREE tier → queue for weekly digest.
                            if not _already_alerted(cur, w["id"],
                                                    shift.get("shift_to")):
                                _record_alert(cur, w["id"], shift,
                                              w["channel"], "pending")
                                summary["alerts_queued"] += 1
                            else:
                                summary["alerts_skipped"] += 1
                            continue

                        if mode == "digest" and is_realtime:
                            # PRO+ shouldn't get the weekly digest
                            # (they already got the realtime). Skip.
                            summary["alerts_skipped"] += 1
                            continue

                        if _already_alerted(cur, w["id"],
                                            shift.get("shift_to")):
                            summary["alerts_skipped"] += 1
                            continue

                        # ── SEND ──
                        sent_any = False
                        if w["channel"] in ("email", "push") and plaintext_email:
                            if w["channel"] == "email":
                                subj, html = _watchlist_realtime_email(shift)
                                if _send_email(plaintext_email, subj, html):
                                    sent_any = True
                                    _record_alert(cur, w["id"], shift,
                                                  "email", "sent")
                            else:  # push
                                subs = _push_subs_for_email_hash(
                                    cur, w["email_hash"])
                                payload = {
                                    "title": (f"{shift.get('market_name') or slug} "
                                              f"shifted to {(shift.get('shift_to') or '').upper()}"),
                                    "body": f"DCPI verdict moved. Tap for the brief.",
                                    "url": f"https://dchub.cloud/markets/{slug}/brief",
                                }
                                for s in subs:
                                    if _send_push(s, payload):
                                        summary["push_sent"] += 1
                                        sent_any = True
                                    else:
                                        summary["push_failed"] += 1
                                if sent_any:
                                    _record_alert(cur, w["id"], shift,
                                                  "push", "sent")
                            if sent_any:
                                summary["alerts_sent"] += 1
                            else:
                                summary["alerts_skipped"] += 1
                                _record_alert(cur, w["id"], shift,
                                              w["channel"], "failed")
                        else:
                            # Unreachable (no plaintext email or unsupported
                            # channel like 'sms' not yet wired) — record
                            # status='skipped' so we know.
                            _record_alert(cur, w["id"], shift,
                                          w["channel"], "skipped")
                            summary["alerts_skipped"] += 1
                    except Exception as e:
                        summary["errors"].append(f"watcher_{w.get('id')}: {e}")

            # ─── 3. If digest mode: ALSO ship the pending queue ────
            if mode == "digest":
                _ship_pending_digest(cur, summary)

            try: conn.commit()
            except Exception: pass

    except Exception as e:
        summary["errors"].append(f"top_level: {e}")
    finally:
        try: conn.close()
        except Exception: pass

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def _ship_pending_digest(cur, summary: dict) -> None:
    """Monday only: bundle every PENDING alert per watcher into one
    digest email, send via Resend, mark status='sent'."""
    try:
        cur.execute("""
            SELECT was.id, was.watchlist_id, was.market_slug,
                   was.shift_from, was.shift_to,
                   uw.owner_email_hash, uw.channel
              FROM watchlist_alerts_sent was
              JOIN user_watchlists uw ON uw.id = was.watchlist_id
             WHERE was.status = 'pending'
               AND was.sent_at > NOW() - INTERVAL '8 days'
               AND uw.unsubscribed_at IS NULL
             ORDER BY uw.owner_email_hash
             LIMIT %s
        """, (MAX_DIGEST_ALERTS_PER_RUN,))
        rows = cur.fetchall() or []
        if not rows:
            return

        # Bucket by email hash.
        buckets: dict[str, list[dict]] = {}
        bucket_ids: dict[str, list[int]] = {}
        for r in rows:
            was_id, wl_id, slug, fr, to, eh, ch = r
            buckets.setdefault(eh, []).append({
                "market_slug": slug,
                "market_name": slug.replace("-", " ").title() if slug else "Market",
                "shift_from":  fr,
                "shift_to":    to,
            })
            bucket_ids.setdefault(eh, []).append(int(was_id))

        for email_hash, shifts in buckets.items():
            plaintext = _resolve_email_from_hash(cur, email_hash)
            ids = bucket_ids.get(email_hash, [])
            if not plaintext:
                # Mark as skipped (unreachable) so we don't keep retrying.
                try:
                    cur.execute(
                        "UPDATE watchlist_alerts_sent "
                        "SET status = 'skipped' WHERE id = ANY(%s)",
                        (ids,))
                except Exception:
                    pass
                summary["alerts_skipped"] += len(ids)
                continue
            subj, html = _watchlist_weekly_digest_email(shifts)
            if _send_email(plaintext, subj, html):
                try:
                    cur.execute(
                        "UPDATE watchlist_alerts_sent "
                        "SET status = 'sent', sent_at = NOW() "
                        "WHERE id = ANY(%s)",
                        (ids,))
                except Exception:
                    pass
                summary["alerts_sent"] += len(ids)
            else:
                # Leave status='pending' so we retry next week.
                summary["alerts_skipped"] += len(ids)
    except Exception as e:
        summary["errors"].append(f"digest_ship: {e}")
