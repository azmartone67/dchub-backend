"""
weekly_digest.py — Phase TT Increments 3b + 3c: the weekly market digest
and the escalating payment ask.

The nurture loop, second touch. Increments 1 & 2 capture the email at the
value moment; Increment 3a (send_identify_welcome) confirms the unlock and
*promises* "a weekly digest of the data-center markets your assistant
queries." This module makes good on that promise.

Once a week, for every identified key with real activity in the last 7
days, we:
  - pull that key's mcp_call_log rows for the window
  - extract the markets/states it actually queried (from tool params)
  - enrich the top markets with a fresh stat (current retail energy rate)
  - email the human a short, genuinely-useful recap

Increment 3c — the escalating payment ask — rides INSIDE that same email
rather than as a separate send. The digest already lands weekly; 3c just
makes its call-to-action escalate by engagement signal instead of being a
static button:
  - hot   : hit the daily cap 3+ times this week -> strong, specific ask
  - warm  : heavy volume, or 1-2 cap hits        -> medium, volume-anchored
  - soft  : everyone else                        -> the gentle nudge
No extra emails, no separate cadence to cooldown-gate — the ask just
sharpens as the buy signal does, and the tier is stamped on the key so we
can measure escalation over time.

Endpoint: POST /api/v1/digest/weekly/run   (admin-gated)
  ?send=true   actually deliver (default: dry-run preview, like the
               cap-exceeded outreach engine)
  ?force=1     bypass the once-per-week per-key dedup
  ?limit=N     cap the cohort size (default 200)

Dedup: one digest per key per ~week, tracked on mcp_dev_keys.metadata
.digest_sent_at — same pattern as welcome_sent_at. Stamped only on a
confirmed send, so a transient failure is retried next run rather than
lost. Best-effort throughout: a DB blip or send failure for one key never
breaks the rest of the cohort, and the whole thing no-ops quietly when
DCHUB_RESEND_API_KEY isn't configured.
"""

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request

weekly_digest_bp = Blueprint("weekly_digest", __name__)

ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
RESEND_KEY = os.environ.get("DCHUB_RESEND_API_KEY", "").strip()
FROM_EMAIL = os.environ.get("DCHUB_RESEND_FROM", "DC Hub <noreply@dchub.cloud>")
PRICING_URL = "https://dchub.cloud/pricing"
# Increment 3c — the upgrade link. Prefer a direct Stripe checkout link
# (same env var the cap-exceeded outreach engine uses) so the "hot" ask
# goes straight to payment; fall back to the pricing page.
UPGRADE_URL = (os.environ.get("DCHUB_STRIPE_DEVELOPER_LINK")
               or os.environ.get("DCHUB_STRIPE_PRO_LINK")
               or PRICING_URL)

# Increment 3c escalation thresholds (env-overridable).
#   hot  : hit the daily cap this many times in the window
#   warm : this many total calls in the window (heavy use, not yet capping)
HOT_CAP_HITS = int(os.environ.get("DCHUB_DIGEST_HOT_CAP_HITS", "3"))
WARM_CALLS = int(os.environ.get("DCHUB_DIGEST_WARM_CALLS", "40"))
# The identified daily limit, for value-anchored copy ("up from 100/day").
IDENT_LIMIT = int(os.environ.get("MCP_IDENTIFIED_DAILY_LIMIT", "100"))

# Don't email ourselves / test rows.
_INTERNAL_RE = [re.compile(p, re.I) for p in (
    r"@dchub\.cloud$", r"@anthropic\.com$", r"^test", r"^admin", r"^noreply",
)]

# A key needs at least this many calls in the window to be worth a digest —
# a 1-call week isn't a digest, it's spam.
MIN_CALLS = int(os.environ.get("DCHUB_DIGEST_MIN_CALLS", "3"))
# Dedup window: skip a key digested within this many days (unless ?force=1).
DEDUP_DAYS = int(os.environ.get("DCHUB_DIGEST_DEDUP_DAYS", "6"))

# jsonb param keys that name a market/place an agent queried.
_TOPIC_KEYS = ("market", "state", "region", "city", "metro", "slug",
               "location", "area")

# US state name <-> USPS abbreviation, for energy-rate enrichment. Params
# come in as full names ("Georgia"), abbreviations ("GA"), or city/market
# names ("Phoenix") — only the first two enrich; cities pass through as
# plain topic labels, which is fine.
_STATE_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_ABBR_SET = set(_STATE_TO_ABBR.values())


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key")
                    or request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return w


def _is_internal(email: str) -> bool:
    return any(p.search(email or "") for p in _INTERNAL_RE)


def _topics_from_params(params):
    """Pull market/place names out of a single call's jsonb params."""
    if params is None:
        return []
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            return []
    if not isinstance(params, dict):
        return []
    out = []
    for k in _TOPIC_KEYS:
        v = params.get(k)
        if isinstance(v, str):
            v = v.strip()
            # slugs come in kebab-case ("los-angeles") — humanize them.
            if k == "slug":
                v = v.replace("-", " ").replace("_", " ").strip()
            if 1 < len(v) < 60:
                out.append(v)
    return out


def _abbr_for(topic: str):
    """Map a topic label to a USPS state abbr, or None if it isn't a state."""
    t = (topic or "").strip()
    if t.upper() in _ABBR_SET:
        return t.upper()
    return _STATE_TO_ABBR.get(t.lower())


def _energy_rates_for(abbrs):
    """Latest industrial retail ¢/kWh for a set of state abbreviations.

    Best-effort — returns {} on any DB hiccup so the digest still sends,
    just without the energy enrichment line.
    """
    abbrs = [a for a in {a for a in abbrs if a}]
    if not abbrs:
        return {}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ON (UPPER(state))
                          UPPER(state), rate_cents_kwh
                     FROM eia_retail_rates
                    WHERE UPPER(state) = ANY(%s)
                      AND sector = 'industrial'
                    ORDER BY UPPER(state), period DESC""",
                (abbrs,),
            )
            return {r[0]: float(r[1]) for r in cur.fetchall()
                    if r[1] is not None}
    except Exception:
        return {}


def _gather_cohort(limit):
    """Identified, active keys with >= MIN_CALLS calls in the last 7 days,
    plus the per-call (tool, params) rows needed to build each digest."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT k.api_key, k.email, k.tier, k.metadata,
                      COUNT(*) AS calls, COUNT(DISTINCT l.tool) AS tools_used,
                      COUNT(*) FILTER (
                          WHERE l.status = 'blocked_daily_cap') AS cap_hits
                 FROM mcp_dev_keys k
                 JOIN mcp_call_log l ON l.api_key = k.api_key
                WHERE k.email IS NOT NULL
                  AND k.status = 'active'
                  AND l.timestamp >= NOW() - INTERVAL '7 days'
                GROUP BY k.api_key, k.email, k.tier, k.metadata
               HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
                LIMIT %s""",
            (MIN_CALLS, limit),
        )
        cohort = {}
        for (api_key, email, tier, meta, calls,
             tools_used, cap_hits) in cur.fetchall():
            cohort[api_key] = {
                "api_key": api_key, "email": email, "tier": tier,
                "metadata": meta or {}, "calls": int(calls),
                "tools_used": int(tools_used), "cap_hits": int(cap_hits or 0),
                "topics": Counter(), "tools": Counter(),
            }
        if not cohort:
            return {}

        cur.execute(
            """SELECT api_key, tool, params
                 FROM mcp_call_log
                WHERE api_key = ANY(%s)
                  AND timestamp >= NOW() - INTERVAL '7 days'""",
            (list(cohort.keys()),),
        )
        for api_key, tool, params in cur.fetchall():
            row = cohort.get(api_key)
            if not row:
                continue
            if tool:
                row["tools"][str(tool)] += 1
            for t in _topics_from_params(params):
                row["topics"][t] += 1
    return cohort


def _recently_digested(meta) -> bool:
    """True if metadata.digest_sent_at is within the dedup window."""
    if not isinstance(meta, dict):
        return False
    raw = meta.get("digest_sent_at")
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        return age_days < DEDUP_DAYS
    except Exception:
        return False


def _upgrade_tier(row):
    """Increment 3c — classify a key's buy signal from the window's activity.

    Returns (tier, reason) where tier is 'hot' | 'warm' | 'soft'. The
    reason is a short dict the response surfaces for measurement.
    """
    cap_hits = row.get("cap_hits", 0)
    calls = row.get("calls", 0)
    if cap_hits >= HOT_CAP_HITS:
        return "hot", {"signal": "cap_hits", "cap_hits": cap_hits}
    if calls >= WARM_CALLS or cap_hits >= 1:
        return "warm", {"signal": "volume" if calls >= WARM_CALLS else "cap_hits",
                        "calls": calls, "cap_hits": cap_hits}
    return "soft", {"signal": "baseline", "calls": calls}


def _cta_block(tier, row, top_markets):
    """Render the Increment 3c call-to-action, escalated to the tier.

    Value-anchored: every variant references the key's OWN usage (cap
    hits, weekly volume, or top market) so the ask reads as a natural
    consequence of how hard they're already leaning on DC Hub.
    """
    btn = ('display:inline-block;background:#1976d2;color:#fff;padding:11px 22px;'
           'border-radius:6px;text-decoration:none;font-weight:600')
    top_mkt = top_markets[0][0] if top_markets else None

    if tier == "hot":
        cap_hits = row.get("cap_hits", 0)
        lead = (f"Your assistant hit the {IDENT_LIMIT}/day limit "
                f"<strong>{cap_hits} time{'s' if cap_hits != 1 else ''}</strong> "
                f"this week — every capped call is a question that went "
                f"unanswered.")
        pitch = (f"The Developer plan is <strong>1,000 calls/day</strong> "
                 f"plus full DCPI history and market-movement alerts"
                 + (f" on the markets you track, like {top_mkt}." if top_mkt
                    else "."))
        label = "Lift the limit — Developer, $49/mo &rarr;"
    elif tier == "warm":
        calls = row.get("calls", 0)
        lead = (f"You ran <strong>{calls} queries</strong> this week — "
                f"you're using DC Hub like a paying desk already.")
        pitch = (f"Developer is <strong>1,000 calls/day</strong>, full data "
                 f"depth, and alerts when "
                 + (f"{top_mkt} moves." if top_mkt else "your markets move."))
        label = "See the Developer plan &rarr;"
    else:  # soft
        lead = ""
        pitch = ("Need more headroom? The Developer plan is "
                 "<strong>1,000 calls/day</strong> plus full data and "
                 "market-movement alerts.")
        label = "See Developer &rarr;"

    lead_html = (f'<p style="color:#555;font-size:15px;line-height:1.55;'
                 f'margin:22px 0 6px">{lead}</p>') if lead else ""
    return f"""{lead_html}
<p style="color:#555;font-size:15px;line-height:1.55;margin:6px 0 14px">{pitch}</p>
<p style="margin:0 0 4px"><a href="{UPGRADE_URL}" style="{btn}">{label}</a></p>"""


def _build_digest(row, rates):
    """Turn one cohort row into the rendered digest payload."""
    top_markets = row["topics"].most_common(5)
    top_tools = row["tools"].most_common(5)

    market_lines = []
    for name, n in top_markets:
        abbr = _abbr_for(name)
        rate = rates.get(abbr) if abbr else None
        if rate is not None:
            market_lines.append(
                f"<li><strong>{name}</strong> — {n} "
                f"quer{'y' if n == 1 else 'ies'} "
                f"&middot; latest industrial power: {rate:.1f}&cent;/kWh</li>")
        else:
            market_lines.append(
                f"<li><strong>{name}</strong> — {n} "
                f"quer{'y' if n == 1 else 'ies'}</li>")

    tool_lines = [f"<li>{t} &times;{n}</li>" for t, n in top_tools]

    markets_block = (
        f"<p style=\"color:#555;font-size:15px;margin:18px 0 6px\">"
        f"<strong>Markets your assistant looked at</strong></p>"
        f"<ul style=\"color:#555;font-size:15px;line-height:1.7\">"
        f"{''.join(market_lines)}</ul>"
    ) if market_lines else ""

    tools_block = (
        f"<p style=\"color:#555;font-size:15px;margin:18px 0 6px\">"
        f"<strong>Most-used tools</strong></p>"
        f"<ul style=\"color:#555;font-size:15px;line-height:1.7\">"
        f"{''.join(tool_lines)}</ul>"
    ) if tool_lines else ""

    headline_markets = ", ".join(n for n, _ in top_markets[:3])
    subject = (
        f"Your DC Hub week: {row['calls']} queries"
        + (f" across {headline_markets}" if headline_markets else "")
    )

    # Increment 3c — escalate the CTA to the key's buy signal.
    tier, tier_reason = _upgrade_tier(row)
    cta_block = _cta_block(tier, row, top_markets)

    html = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:28px;color:#1a1a1a">
<div style="font-size:11px;color:#888;letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">DC Hub &middot; weekly market digest</div>
<h2 style="margin:0 0 12px;font-size:22px">Your AI assistant ran {row['calls']} DC Hub queries this week</h2>
<p style="color:#555;font-size:15px;line-height:1.55">Across {row['tools_used']} different tool{'s' if row['tools_used'] != 1 else ''}. Here's what it was researching &mdash; with a fresh data point on the markets that have one.</p>
{markets_block}
{tools_block}
<hr style="border:0;border-top:1px solid #eee;margin:24px 0 4px">
{cta_block}
<hr style="border:0;border-top:1px solid #eee;margin:24px 0 14px">
<p style="font-size:12px;color:#888">You're getting this weekly digest because your AI assistant identified its DC Hub key with this email. <a href="https://dchub.cloud" style="color:#888">dchub.cloud</a></p>
</body></html>"""

    return {
        "subject": subject,
        "html": html,
        "top_markets": [{"name": n, "queries": c} for n, c in top_markets],
        "top_tools": [{"tool": t, "calls": c} for t, c in top_tools],
        "calls": row["calls"],
        "cap_hits": row.get("cap_hits", 0),
        "upgrade_tier": tier,
        "upgrade_reason": tier_reason,
    }


def _send_email(to_email, subject, html) -> bool:
    """Deliver one digest via Resend. Returns True only on a confirmed send."""
    if not RESEND_KEY:
        return False
    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}",
                     "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [to_email],
                  "subject": subject, "html": html},
            timeout=12,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def _stamp_sent(api_key, tier="soft"):
    """Mark a key digested — only called on a confirmed send. Also stamps
    the Increment 3c upgrade tier the key was shown, so escalation over
    time is measurable straight off mcp_dev_keys.metadata."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """UPDATE mcp_dev_keys
                      SET metadata = COALESCE(metadata, '{}'::jsonb)
                                     || jsonb_build_object(
                                            'digest_sent_at', %s::text,
                                            'last_digest_tier', %s::text)
                    WHERE api_key = %s""",
                (datetime.now(timezone.utc).isoformat(), tier, api_key),
            )
            c.commit()
    except Exception:
        pass


@weekly_digest_bp.route("/api/v1/digest/weekly/run", methods=["POST"])
@_require_admin
def run_weekly_digest():
    """Build + (optionally) send the weekly market digest to every
    identified, active key with real activity in the last 7 days.

    Dry-run by default — pass ?send=true to actually deliver.
    """
    send = (request.args.get("send") or "").lower() in ("1", "true", "yes")
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    try:
        limit = max(1, min(int(request.args.get("limit", "200")), 1000))
    except ValueError:
        limit = 200

    try:
        cohort = _gather_cohort(limit)
    except Exception as e:
        return jsonify(mode="error", error=str(e)[:300]), 200

    eligible_total = len(cohort)
    skipped_internal = 0
    skipped_dedup = 0
    skipped_no_signal = 0
    sent = 0
    send_failed = 0
    # Increment 3c — how the cohort splits across upgrade-ask tiers.
    tier_counts = Counter()

    # One batched energy lookup for every state that shows up anywhere in
    # the cohort — cheaper than per-key round-trips.
    all_abbrs = set()
    for row in cohort.values():
        for topic, _ in row["topics"].most_common(5):
            a = _abbr_for(topic)
            if a:
                all_abbrs.add(a)
    rates = _energy_rates_for(all_abbrs)

    preview = []
    for api_key, row in cohort.items():
        if _is_internal(row["email"]):
            skipped_internal += 1
            continue
        if not force and _recently_digested(row["metadata"]):
            skipped_dedup += 1
            continue
        # A digest with neither markets nor tools is empty — shouldn't
        # happen given MIN_CALLS, but guard anyway.
        if not row["topics"] and not row["tools"]:
            skipped_no_signal += 1
            continue

        digest = _build_digest(row, rates)
        tier_counts[digest["upgrade_tier"]] += 1

        if send:
            ok = _send_email(row["email"], digest["subject"], digest["html"])
            if ok:
                _stamp_sent(api_key, digest["upgrade_tier"])
                sent += 1
            else:
                send_failed += 1
        else:
            if len(preview) < 25:
                preview.append({
                    "email": row["email"],
                    "tier": row["tier"],
                    "subject": digest["subject"],
                    "calls": digest["calls"],
                    "cap_hits": digest["cap_hits"],
                    "upgrade_tier": digest["upgrade_tier"],
                    "upgrade_reason": digest["upgrade_reason"],
                    "top_markets": digest["top_markets"],
                    "top_tools": digest["top_tools"],
                })

    if send:
        mode = "sent" if RESEND_KEY else "no_provider"
    else:
        mode = "dry_run"

    return jsonify(
        mode=mode,
        eligible_total=eligible_total,
        sent=sent,
        send_failed=send_failed,
        skipped_dedup=skipped_dedup,
        skipped_internal=skipped_internal,
        skipped_no_signal=skipped_no_signal,
        min_calls=MIN_CALLS,
        dedup_days=DEDUP_DAYS,
        # Increment 3c — upgrade-ask tier split across the processed cohort.
        upgrade_tiers={"hot": tier_counts.get("hot", 0),
                       "warm": tier_counts.get("warm", 0),
                       "soft": tier_counts.get("soft", 0)},
        upgrade_url_is_stripe=UPGRADE_URL != PRICING_URL,
        provider_configured=bool(RESEND_KEY),
        dry_run_preview=preview,
    ), 200


@weekly_digest_bp.route("/api/v1/digest/weekly/health", methods=["GET"])
def weekly_digest_health():
    """Lightweight visibility: cohort size + how many were digested recently."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM mcp_dev_keys k
                    WHERE k.email IS NOT NULL AND k.status = 'active'
                      AND EXISTS (
                          SELECT 1 FROM mcp_call_log l
                           WHERE l.api_key = k.api_key
                             AND l.timestamp >= NOW() - INTERVAL '7 days')""")
            active_identified = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                """SELECT COUNT(*) FROM mcp_dev_keys
                    WHERE (metadata->>'digest_sent_at') IS NOT NULL
                      AND (metadata->>'digest_sent_at')::timestamptz
                          > NOW() - INTERVAL '7 days'""")
            digested_7d = int((cur.fetchone() or [0])[0] or 0)
            # Increment 3c — what upgrade-ask tier recently-digested keys
            # were last shown.
            cur.execute(
                """SELECT COALESCE(metadata->>'last_digest_tier', 'unknown'),
                          COUNT(*)
                     FROM mcp_dev_keys
                    WHERE (metadata->>'digest_sent_at') IS NOT NULL
                      AND (metadata->>'digest_sent_at')::timestamptz
                          > NOW() - INTERVAL '7 days'
                    GROUP BY 1""")
            tier_split = {r[0]: int(r[1]) for r in cur.fetchall()}
        return jsonify(status="ok",
                       active_identified_with_recent_activity=active_identified,
                       digested_last_7d=digested_7d,
                       digested_tier_split=tier_split,
                       provider_configured=bool(RESEND_KEY)), 200
    except Exception as e:
        return jsonify(status="error", error=str(e)[:200]), 200
