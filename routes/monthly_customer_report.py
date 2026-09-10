"""monthly_customer_report.py — per-customer monthly activity email (2026-09-09).

WHY
---
We bill monthly but tell a customer nothing about what they got. Asked whether we
could "sample what clients are doing on a monthly basis" and email each of them a
recap, the answer measured live on 2026-09-09 was: the DATA is there, but a single
recap template would render BLANK for most of the roster.

    Aug-2026, external paying accounts — 6 made ANY MCP call:
      seydi.sokhona 2,563 · eren@globeholder.ai 1,661 · mykemiller 161
      ian.christie@nlr.gov 10 · gabriel.zuckerman 2 · galen.maclaurin 2
    ~30 made zero — including payers on invoice #5, #6 and #8 with 0 calls EVER.

    tj@karklins.com ($49 developer, renewed 2026-09-08, invoice #2): key issued
    2026-08-08, mcp_dev_keys.last_used_at NULL, 0 rows in mcp_call_log, no row in
    mcp_monthly_usage — three stores agree he has never called once. But
    users.last_login = 2026-08-27, so he IS using the website. He is paying for an
    API he has never touched.

So this ships TWO lanes, not one:

  recap       calls > 0 in the month — what they ran, which tools, what they hit a
              paywall on, and one question: what should we build next?
  activation  calls == 0, still paying — their key, one worked example, and the
              only question worth asking: what were you hoping to do?

WHERE THE NUMBERS COME FROM
---------------------------
  mcp_call_log(timestamp, tool, api_key, status)  per-call telemetry
    JOIN mcp_dev_keys(api_key, email)             key -> customer
  users(plan, invoices_paid_count, last_login, engagement_stage)

  api_key attribution coverage on mcp_call_log is 82.8% (Sep) - 97.7% (May),
  measured 2026-09-09. It is not complete, so `calls` is a FLOOR, and the
  activation lane says "we have no record of" rather than "you made zero calls".

★ DO NOT read users.api_calls_total as usage. It is 0 for 57 of 58 paying rows —
  the column is never incremented (see api_usage_tracker.py, which fixed the
  api_keys counters and not this one). site_analysis_log is likewise empty
  despite carrying user_email. The only trustworthy web-side per-customer signal
  is users.last_login, which is why the activation lane cites it.

★ The live `api_keys` table has NO api_key and NO email column — the shipped
  migration_001_api_keys.sql DDL does not match production. Customer identity
  comes from mcp_dev_keys.

SAFETY (this reaches real paying customers)
-------------------------------------------
  * DRY-RUN BY DEFAULT. A send needs BOTH an explicit confirm AND the arm flag
    MONTHLY_CUSTOMER_REPORT_ARM=1. Un-armed runs return exactly who WOULD be
    mailed, with the rendered HTML, and send nothing.
  * Idempotent per month: email_drip_log has UNIQUE(user_email, email_key) and
    the key carries the month — 'monthly_report_2026_08'. Re-running is a no-op
    for anyone already mailed.
  * The drip row is written AFTER a successful send, not claimed before it, so a
    transient Resend failure does not permanently burn that customer's month.
    Trade-off: two CONCURRENT armed runs could double-send. This is an operator-
    triggered monthly job; do not run two at once.
  * Honours email_suppression, email_exclude_list and dchub_outreach's internal
    patterns. kevin.d.serfass@gmail.com is on the suppression list (unsubscribed
    2026-06-22) and must never be mailed by this.
  * BD seed rows (partnerships@<ai-lab>, press@deepmind.com — plan='developer',
    0 invoices, 0 keys) are excluded by requiring a real payment OR real usage.

RUNNING IT
----------
  Preferred — CLI, so the send is not subject to the edge's 15s admin-POST timeout:
    railway run python3 -m routes.monthly_customer_report --month 2026-08
    railway run python3 -m routes.monthly_customer_report --month 2026-08 --email tj@karklins.com --html
    railway run python3 -m routes.monthly_customer_report --month 2026-08 --send --confirm

  Endpoints (admin-keyed), for the operator UI:
    GET  /api/v1/admin/monthly-customer-report/preview?month=2026-08[&email=]
    POST /api/v1/admin/monthly-customer-report/send?month=2026-08&confirm=1[&limit=25]
"""
from __future__ import annotations

import calendar
import datetime
import logging
import os
from html import escape as _esc

from flask import Blueprint, jsonify, request

logger = logging.getLogger("monthly_customer_report")
monthly_customer_report_bp = Blueprint("monthly_customer_report", __name__)

PAID_PLANS = ("starter", "developer", "pro", "enterprise", "founding")
FROM_EMAIL = "jonathan@dchub.cloud"
FROM_NAME = "Jonathan Martone"
DEFAULT_SEND_LIMIT = 25

# Tools we never suggest as "worth a look" — infra/meta, not customer value.
_NON_PRODUCT_TOOLS = {
    "discover_tools", "why_dchub", "claim_free_key", "recover_my_key",
    "bind_email", "unlock_more_data", "fetch", "search",
}


def _conn():
    import psycopg2
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL / NEON_DATABASE_URL not set")
    c = psycopg2.connect(dsn)
    c.autocommit = True
    return c


def _admin_ok() -> bool:
    from internal_auth import accepted_internal_keys
    keys = set(accepted_internal_keys())
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return bool(sent) and sent in keys


def parse_month(spec: str | None) -> tuple[int, int]:
    """'2026-08' -> (2026, 8). None -> the last COMPLETE month.

    Defaulting to last-complete (not current) matters: run on the 1st, a
    current-month report is one day of data and reads as a usage collapse.
    """
    if spec:
        y, m = spec.strip().split("-")[:2]
        y, m = int(y), int(m)
        if not (1 <= m <= 12):
            raise ValueError(f"bad month: {spec}")
        return y, m
    today = datetime.date.today()
    first = today.replace(day=1)
    prev = first - datetime.timedelta(days=1)
    return prev.year, prev.month


def month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, month, 1)
    end = start + datetime.timedelta(days=calendar.monthrange(year, month)[1])
    return start, end


def month_label(year: int, month: int) -> str:
    return datetime.date(year, month, 1).strftime("%B %Y")


def email_key_for(year: int, month: int) -> str:
    return f"monthly_report_{year}_{month:02d}"


# ── measurement ──────────────────────────────────────────────────────────

def collect_month(year: int, month: int, conn=None) -> list[dict]:
    """One row per real paying/using customer, with that month's activity.

    Roster rule: a real payment (invoices_paid_count > 0) OR real usage in the
    month. Either alone is wrong — invoices-only drops the nlr.gov enterprise
    seats and seydi.sokhona (billed outside Stripe, 2,563 calls in Aug);
    usage-only drops every stranded payer, who are the whole point of the
    activation lane. Together they also exclude the BD seed rows, which have
    neither.
    """
    own = conn is None
    c = conn or _conn()
    start, end = month_bounds(year, month)
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH roster AS (
                  SELECT lower(u.email) AS email,
                         COALESCE(NULLIF(btrim(u.name), ''), '') AS name,
                         u.plan,
                         COALESCE(u.invoices_paid_count, 0) AS invoices,
                         u.engagement_stage,
                         u.last_login,
                         u.subscription_status,
                         u.created_at
                    FROM users u
                   WHERE u.plan IN %(plans)s
                     AND u.email IS NOT NULL AND btrim(u.email) <> ''
                ),
                keyed AS (
                  SELECT lower(d.email) AS email, d.api_key
                    FROM mcp_dev_keys d
                   WHERE d.email IS NOT NULL AND btrim(d.email) <> ''
                ),
                calls AS (
                  SELECT k.email,
                         COUNT(*) FILTER (WHERE ml.status = 'ok' OR ml.status IS NULL) AS ok_calls,
                         COUNT(*) AS all_calls,
                         COUNT(DISTINCT ml.timestamp::date) AS active_days,
                         COUNT(DISTINCT ml.tool) AS distinct_tools,
                         MIN(ml.timestamp) AS first_call,
                         MAX(ml.timestamp) AS last_call
                    FROM mcp_call_log ml
                    JOIN keyed k ON k.api_key = ml.api_key
                   WHERE ml.timestamp >= %(start)s AND ml.timestamp < %(end)s
                   GROUP BY 1
                ),
                ever AS (
                  SELECT k.email, COUNT(*) AS calls_ever, MAX(ml.timestamp) AS last_ever
                    FROM mcp_call_log ml
                    JOIN keyed k ON k.api_key = ml.api_key
                   GROUP BY 1
                )
                -- Every column MUST carry an explicit alias: psycopg2 names a
                -- COALESCE column 'coalesce', so three of these would collide
                -- into one key and the rest would vanish from the dict.
                SELECT r.email AS email, r.name AS name, r.plan AS plan,
                       r.invoices AS invoices,
                       r.engagement_stage AS engagement_stage,
                       r.last_login AS last_login,
                       r.subscription_status AS subscription_status,
                       r.created_at AS created_at,
                       COALESCE(cl.ok_calls, 0)       AS ok_calls,
                       COALESCE(cl.all_calls, 0)      AS all_calls,
                       COALESCE(cl.active_days, 0)    AS active_days,
                       COALESCE(cl.distinct_tools, 0) AS distinct_tools,
                       cl.first_call AS first_call, cl.last_call AS last_call,
                       COALESCE(ev.calls_ever, 0) AS calls_ever,
                       ev.last_ever AS last_ever,
                       (SELECT COUNT(*) FROM keyed k2 WHERE k2.email = r.email) AS n_keys
                  FROM roster r
                  LEFT JOIN calls cl ON cl.email = r.email
                  LEFT JOIN ever  ev ON ev.email = r.email
                 -- Explicitly-dead subscriptions are excluded: the activation
                 -- copy asserts the plan is billing, and 5 of this lane were
                 -- canceled/payment_failed on 2026-09-09 (dave@stonegoff,
                 -- owenlamontt, faisalbis, m18563991063, motifs-buckles0j).
                 -- Winback is send_winback_outreach.py's job, not this one.
                 -- NULL is NOT dead — it is 25 of 58 paying rows, including the
                 -- nlr.gov seats and seydi.sokhona, who are billed outside
                 -- Stripe and were the month's heaviest users.
                 WHERE COALESCE(r.subscription_status, '') NOT IN ('canceled', 'payment_failed')
                   AND (r.invoices > 0 OR COALESCE(cl.all_calls, 0) > 0)
                 ORDER BY COALESCE(cl.ok_calls, 0) DESC, r.plan, r.email
            """, {"plans": PAID_PLANS, "start": start, "end": end})
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        emails = [r["email"] for r in rows]
        tools_by_email: dict[str, list[tuple[str, int]]] = {}
        blocked_by_email: dict[str, list[tuple[str, int]]] = {}
        if emails:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT lower(d.email), ml.tool,
                           COUNT(*) FILTER (WHERE ml.status = 'ok' OR ml.status IS NULL) AS ok_n,
                           COUNT(*) FILTER (WHERE ml.status IN ('blocked_paid_only', 'error')) AS blocked_n
                      FROM mcp_call_log ml
                      JOIN mcp_dev_keys d ON d.api_key = ml.api_key
                     WHERE ml.timestamp >= %s AND ml.timestamp < %s
                       AND lower(d.email) = ANY(%s)
                       AND ml.tool IS NOT NULL AND ml.tool <> ''
                     GROUP BY 1, 2
                """, (start, end, emails))
                for em, tool, ok_n, blocked_n in cur.fetchall():
                    if ok_n:
                        tools_by_email.setdefault(em, []).append((tool, ok_n))
                    if blocked_n:
                        blocked_by_email.setdefault(em, []).append((tool, blocked_n))

        popular = _popular_tools(c, start, end)

        out = []
        for r in rows:
            em = r["email"]
            # Nobody gets a report for a month they were not yet a customer.
            # lbthrall@gmail.com signed up 2026-09-08 and would otherwise have
            # been told his August was empty. users.created_at is TEXT with
            # inconsistent formats and is NULL for the nlr.gov seats, so this
            # fails OPEN: only an unambiguous post-month signup is dropped.
            if _signed_up_after(r.get("created_at"), end):
                continue
            tools = sorted(tools_by_email.get(em, []), key=lambda t: -t[1])
            used = {t for t, _ in tools}
            r["top_tools"] = [{"tool": t, "calls": n} for t, n in tools[:8]]
            r["blocked_tools"] = [
                {"tool": t, "hits": n}
                for t, n in sorted(blocked_by_email.get(em, []), key=lambda t: -t[1])[:5]
            ]
            r["suggested_tools"] = [t for t in popular if t not in used][:4]
            r["lane"] = "recap" if r["ok_calls"] > 0 else "activation"
            out.append(r)
        return out
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def _signed_up_after(created_at, month_end: datetime.date) -> bool:
    """True only when we can PROVE the account was created on/after month_end.

    Fails open by design — users.created_at is TEXT, written in at least two
    formats ('2026-08-08T21:07:43.420312' and '2026-07-26 07:42:40.586695+00')
    and NULL for seats provisioned outside signup. An unparseable date must not
    silently drop a real customer from their own report.
    """
    if not created_at:
        return False
    try:
        s = str(created_at).strip().replace("Z", "+00:00")
        d = datetime.datetime.fromisoformat(s)
        return d.date() >= month_end
    except Exception:
        return False


MIN_CALLERS_TO_RECOMMEND = 2


def _popular_tools(c, start, end, limit: int = 20) -> list[str]:
    """Tools that OTHER, EXTERNAL customers actually used that month.

    Derived from the call log rather than a hardcoded catalog — a pinned list
    goes stale the moment a tool ships and then recommends tools that no longer
    exist.

    ★ Two filters that are not optional:

    1. SELF-TRAFFIC. Unfiltered, August 2026 reads search_facilities 38,782 ·
       get_news 20,228 · get_agent_registry 10,170 — but 171,236 of that month's
       calls are our OWN (azmartone@gmail.com), so the list was mostly a picture
       of our load tests, and it surfaced internal surfaces like
       get_agent_registry and market_brief_all as customer recommendations.
       External-only, the same month reads search_facilities 2,392 ·
       get_global_power 361 · get_facility 318 — a different list entirely.

    2. A CALLER FLOOR. The copy says other customers use these. With
       MIN_CALLERS_TO_RECOMMEND = 2 that sentence is literally true; at 1 it
       would be one person, and for the recap lane that one person is sometimes
       the recipient's own account.
    """
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT ml.tool, COUNT(*) AS n,
                       COUNT(DISTINCT ml.api_key) AS callers
                  FROM mcp_call_log ml
                  JOIN mcp_dev_keys d ON d.api_key = ml.api_key
                 WHERE ml.timestamp >= %s AND ml.timestamp < %s
                   AND ml.tool IS NOT NULL AND ml.tool <> ''
                   AND (ml.status = 'ok' OR ml.status IS NULL)
                   AND d.email IS NOT NULL
                   AND lower(d.email) NOT LIKE %s
                   AND lower(d.email) NOT LIKE %s
                   AND lower(d.email) NOT LIKE %s
                   AND lower(d.email) NOT LIKE %s
                 GROUP BY 1
                HAVING COUNT(DISTINCT ml.api_key) >= %s
                 ORDER BY n DESC LIMIT %s
            """, (start, end, "%dchub.cloud", "%martone%", "%selftest%",
                  "%qa-canary%", MIN_CALLERS_TO_RECOMMEND, limit * 2))
            return [t for (t, _n, _c) in cur.fetchall()
                    if t not in _NON_PRODUCT_TOOLS][:limit]
    except Exception as e:
        logger.warning("popular tools unavailable: %s", str(e)[:140])
        return []


# ── recipient filtering ──────────────────────────────────────────────────

def _suppressed(c) -> set[str]:
    out: set[str] = set()
    for table in ("email_suppression", "email_exclude_list"):
        try:
            with c.cursor() as cur:
                cur.execute(f"SELECT lower(email) FROM {table} WHERE email IS NOT NULL")
                out.update(e for (e,) in cur.fetchall() if e)
        except Exception as e:
            # Fail CLOSED on the suppression read: an unreadable list must not
            # silently become an empty one, or an unsubscriber gets mailed.
            raise RuntimeError(f"{table} unreadable, refusing to send: {e}") from e
    return out


def _already_sent(c, key: str) -> set[str]:
    try:
        with c.cursor() as cur:
            cur.execute("SELECT lower(user_email) FROM email_drip_log WHERE email_key = %s", (key,))
            return {e for (e,) in cur.fetchall() if e}
    except Exception as e:
        raise RuntimeError(f"email_drip_log unreadable, refusing to send: {e}") from e


def _is_internal(email: str) -> bool:
    try:
        from dchub_outreach import is_internal_email
        return bool(is_internal_email(email))
    except Exception:
        e = (email or "").lower()
        return (not e) or "@dchub.cloud" in e or "selftest" in e or "qa-canary" in e


def _log_sent(c, email: str, key: str, status: str = "sent") -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO email_drip_log (user_email, email_key, status) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_email, email_key) DO NOTHING",
                (email, key, status))
    except Exception as e:
        logger.warning("could not log send for %s/%s: %s", email, key, str(e)[:140])


# ── rendering ────────────────────────────────────────────────────────────

_INK, _MUTED, _ACCENT, _LINE = "#0a2540", "#5a6b85", "#1976d2", "#e9eef5"
_BODY = (f"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
         f"color:{_INK};line-height:1.6;font-size:16px;")


# What a customer is actually CALLED, where it differs from what they
# registered as. Not derivable from any column — users.name holds the legal/
# signup name, so tj@karklins.com renders as "Theodore" unless it is stated
# here. Hand-curated on purpose; a wrong guess at a nickname is worse than the
# formal name. Overridable per-deploy with DCHUB_PREFERRED_NAMES, formatted
# "email:Name,email:Name", so adding one does not need a deploy.
PREFERRED_FIRST_NAMES = {
    "tj@karklins.com": "TJ",
}


def _preferred_names() -> dict[str, str]:
    out = dict(PREFERRED_FIRST_NAMES)
    raw = os.environ.get("DCHUB_PREFERRED_NAMES", "")
    for pair in raw.split(","):
        if ":" in pair:
            em, nm = pair.split(":", 1)
            em, nm = em.strip().lower(), nm.strip()
            if em and nm:
                out[em] = nm
    return out


def _first_name(name: str, email: str) -> str:
    preferred = _preferred_names().get((email or "").strip().lower())
    if preferred:
        return preferred
    n = (name or "").strip()
    if n:
        return n.split()[0]
    local = (email or "").split("@")[0]
    local = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return local.split()[0].title() if local else "there"


def _pretty(tool: str) -> str:
    return tool.replace("_", " ")


def subject_for(r: dict, year: int, month: int) -> str:
    label = month_label(year, month)
    if r["lane"] == "recap":
        return f"Your DC Hub {label}: {r['ok_calls']:,} calls"
    return f"A question about your DC Hub {label}"


def render_email(r: dict, year: int, month: int) -> str:
    return (_render_recap(r, year, month) if r["lane"] == "recap"
            else _render_activation(r, year, month))


def _render_recap(r: dict, year: int, month: int) -> str:
    first = _esc(_first_name(r.get("name") or "", r["email"]))
    label = month_label(year, month)
    rows = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid {_LINE};">{_esc(_pretty(t["tool"]))}</td>'
        f'<td style="padding:7px 0;border-bottom:1px solid {_LINE};text-align:right;'
        f'font-variant-numeric:tabular-nums;">{t["calls"]:,}</td></tr>'
        for t in r["top_tools"])

    days = r["active_days"]
    day_word = "day" if days == 1 else "days"
    lede = (f'You ran <b>{r["ok_calls"]:,} queries</b> across '
            f'<b>{r["distinct_tools"]}</b> tools on {days} {day_word} in {label}.')

    blocked = ""
    if r["blocked_tools"]:
        items = ", ".join(f'{_esc(_pretty(b["tool"]))} ({b["hits"]}×)'
                          for b in r["blocked_tools"])
        blocked = (f'<p>You also hit a wall on {items}. If those are things you '
                   f'actually need, tell me — that is exactly the kind of thing '
                   f'I will open up.</p>')

    suggested = ""
    if r["suggested_tools"]:
        items = ", ".join(_esc(_pretty(t)) for t in r["suggested_tools"][:3])
        suggested = (f'<p>Tools other DC Hub customers used last month that you '
                     f'have not tried yet: {items}.</p>')

    return f"""<!DOCTYPE html>
<html><body style="{_BODY}">
<p>Hi {first},</p>
<p>{lede}</p>
<table style="border-collapse:collapse;width:100%;max-width:420px;margin:18px 0;font-size:15px;">
<tr><th style="text-align:left;padding:0 0 6px;color:{_MUTED};font-weight:600;font-size:13px;
text-transform:uppercase;letter-spacing:.03em;">Tool</th>
<th style="text-align:right;padding:0 0 6px;color:{_MUTED};font-weight:600;font-size:13px;
text-transform:uppercase;letter-spacing:.03em;">Calls</th></tr>
{rows}
</table>
{blocked}
{suggested}
<p><b>The reason I am writing:</b> I would rather build the next thing for you than
guess. What did you go looking for this month and not find? Reply with one line —
I read every one of these myself.</p>
<p>&mdash; Jonathan Martone<br>DC Hub &middot; Martone Advisors<br>
<a href="mailto:jonathan@dchub.cloud" style="color:{_ACCENT};">jonathan@dchub.cloud</a></p>
</body></html>"""


def _render_activation(r: dict, year: int, month: int) -> str:
    first = _esc(_first_name(r.get("name") or "", r["email"]))
    label = month_label(year, month)
    plan = (r.get("plan") or "").replace("_", " ").title() or "DC Hub"

    # Cite last_login when we have it: for several of these customers it is the
    # one true signal that they ARE engaged, just not through the API. Saying
    # "you have not used it" to someone who logged in last week reads as wrong
    # and burns the reply.
    seen_web = ""
    ll = r.get("last_login")
    if ll:
        try:
            d = ll if isinstance(ll, datetime.date) else datetime.datetime.fromisoformat(
                str(ll).replace("Z", "+00:00"))
            seen_web = (f'I can see you signing in to the site (last on '
                        f'{d.strftime("%B %-d")}), so this is not a nudge about '
                        f'whether you are getting value — it is about the half you '
                        f'are paying for and not using.')
        except Exception:
            seen_web = ""

    suggested = ""
    if r["suggested_tools"]:
        items = "".join(f"<li>{_esc(_pretty(t))}</li>" for t in r["suggested_tools"][:3])
        suggested = (f'<p>What other customers reached for most last month:</p>'
                     f'<ul style="margin:6px 0 14px;padding-left:20px;">{items}</ul>')

    # Say something VERIFIABLE about the billing rather than "renewed this
    # month" — the report is sent after the month it covers, so "this month" is
    # ambiguous, and we hold no reliable renewal DATE (stripe_webhook_events
    # carries only event_id/event_type/processed_at, no customer). invoices_
    # paid_count is a real counter, so use that.
    n = r.get("invoices") or 0
    billing = (f"Your {plan} plan is active and billing — that is "
               f"{n} invoices now" if n >= 2 else
               f"Your {plan} plan is active and billing")

    return f"""<!DOCTYPE html>
<html><body style="{_BODY}">
<p>Hi {first},</p>
<p>{billing}, so I went to pull your {label} usage summary and found we have no
record of any API calls on your key. {seen_web}</p>
<p>That is on me, not on you — if the API has not earned its place in your workflow
yet, the setup is the most likely reason.</p>
{suggested}
<p>Two ways I can help, whichever is less work for you:</p>
<ul style="margin:6px 0 14px;padding-left:20px;">
<li>Reply &ldquo;setup&rdquo; and I will wire your key into Claude, Cursor or your own
agent — takes about two minutes, happy to screen-share.</li>
<li>Or just tell me what you were hoping to do with it. If we do not do that yet,
I would genuinely rather know.</li>
</ul>
<p>And if it turns out you do not need the API tier, say so and I will move you to
something that fits rather than let it quietly renew.</p>
<p>&mdash; Jonathan Martone<br>DC Hub &middot; Martone Advisors<br>
<a href="mailto:jonathan@dchub.cloud" style="color:{_ACCENT};">jonathan@dchub.cloud</a></p>
</body></html>"""


# ── the run ──────────────────────────────────────────────────────────────

def run_monthly_report(year: int, month: int, armed: bool = False,
                       limit: int = DEFAULT_SEND_LIMIT,
                       only_email: str | None = None,
                       include_html: bool = False) -> dict:
    """Measure, filter, and (only when fully armed) send. Dry-run by default."""
    key = email_key_for(year, month)
    env_armed = os.environ.get("MONTHLY_CUSTOMER_REPORT_ARM") == "1"
    really_armed = bool(armed) and env_armed

    c = _conn()
    try:
        roster = collect_month(year, month, conn=c)
        suppressed = _suppressed(c)
        sent_already = _already_sent(c, key)

        eligible, skipped = [], []
        for r in roster:
            em = r["email"]
            if only_email and em != only_email.strip().lower():
                continue
            reason = None
            if _is_internal(em):
                reason = "internal"
            elif em in suppressed:
                reason = "suppressed"
            elif em in sent_already:
                reason = "already_sent_this_month"
            if reason:
                skipped.append({"email": em, "reason": reason, "lane": r["lane"]})
            else:
                eligible.append(r)

        result = {
            "ok": True,
            "month": f"{year}-{month:02d}",
            "month_label": month_label(year, month),
            "email_key": key,
            "armed": really_armed,
            "arm_flag_set": env_armed,
            "confirm_passed": bool(armed),
            "roster": len(roster),
            "eligible": len(eligible),
            "lanes": {
                "recap": sum(1 for r in eligible if r["lane"] == "recap"),
                "activation": sum(1 for r in eligible if r["lane"] == "activation"),
            },
            "skipped": skipped,
            "sent": 0,
            "errors": 0,
        }

        def _summarise(r):
            d = {
                "email": r["email"], "name": r.get("name") or "", "plan": r["plan"],
                "lane": r["lane"], "stage": r.get("engagement_stage"),
                "invoices": r["invoices"], "calls": r["ok_calls"],
                "calls_ever": r["calls_ever"], "active_days": r["active_days"],
                "distinct_tools": r["distinct_tools"],
                "top_tools": r["top_tools"], "blocked_tools": r["blocked_tools"],
                "subject": subject_for(r, year, month),
            }
            if include_html:
                d["html"] = render_email(r, year, month)
            return d

        result["recipients"] = [_summarise(r) for r in eligible[:limit]]

        if not really_armed:
            why = []
            if not armed:
                why.append("confirm not passed")
            if not env_armed:
                why.append("MONTHLY_CUSTOMER_REPORT_ARM != 1")
            result["note"] = ("DRY-RUN — nothing sent (" + "; ".join(why) + "). "
                              "These are the customers who WOULD be mailed.")
            return result

        from email_fallback import send_email_resilient
        for r in eligible[:limit]:
            try:
                ok = send_email_resilient(
                    r["email"], subject_for(r, year, month),
                    html_content=render_email(r, year, month),
                    from_email=FROM_EMAIL, from_name=FROM_NAME)
            except Exception as e:
                logger.warning("send failed for %s: %s", r["email"], str(e)[:160])
                ok = False
            if ok:
                _log_sent(c, r["email"], key)
                result["sent"] += 1
            else:
                result["errors"] += 1
        # Alarm on the OUTCOME, not inside an except — send_email_resilient
        # swallows its own failures and returns False, so an except-only alert
        # would never fire on the failure mode that actually happens.
        if result["errors"]:
            logger.error("monthly_customer_report %s: %d of %d sends FAILED",
                         key, result["errors"], result["sent"] + result["errors"])
        return result
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── endpoints ────────────────────────────────────────────────────────────

@monthly_customer_report_bp.route("/api/v1/admin/monthly-customer-report/preview",
                                  methods=["GET"])
def _preview():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    try:
        year, month = parse_month(request.args.get("month"))
    except Exception as e:
        return jsonify(ok=False, error=f"bad month: {e}"), 400
    only = request.args.get("email")
    try:
        return jsonify(run_monthly_report(
            year, month, armed=False,
            limit=int(request.args.get("limit", 100)),
            only_email=only,
            include_html=bool(only) or request.args.get("html") == "1"))
    except Exception as e:
        logger.exception("preview failed")
        return jsonify(ok=False, error=str(e)[:300]), 500


@monthly_customer_report_bp.route("/api/v1/admin/monthly-customer-report/send",
                                  methods=["POST"])
def _send():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    try:
        year, month = parse_month(request.args.get("month"))
    except Exception as e:
        return jsonify(ok=False, error=f"bad month: {e}"), 400
    # Keep the batch small: admin POSTs through the CF edge time out at 15s and
    # each Resend round-trip costs a few hundred ms. The run is idempotent per
    # month, so calling this repeatedly finishes the roster safely.
    limit = min(int(request.args.get("limit", DEFAULT_SEND_LIMIT)), DEFAULT_SEND_LIMIT)
    try:
        return jsonify(run_monthly_report(
            year, month,
            armed=request.args.get("confirm") == "1",
            limit=limit,
            only_email=request.args.get("email")))
    except Exception as e:
        logger.exception("send failed")
        return jsonify(ok=False, error=str(e)[:300]), 500


def setup_monthly_customer_report_routes(app):
    app.register_blueprint(monthly_customer_report_bp)
    return True


# ── CLI ──────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    import json
    p = argparse.ArgumentParser(description="Per-customer monthly activity report")
    p.add_argument("--month", help="YYYY-MM (default: last complete month)")
    p.add_argument("--email", help="restrict to one customer")
    p.add_argument("--html", action="store_true", help="include rendered HTML")
    p.add_argument("--send", action="store_true", help="attempt a real send")
    p.add_argument("--confirm", action="store_true", help="required alongside --send")
    p.add_argument("--limit", type=int, default=100)
    a = p.parse_args()

    year, month = parse_month(a.month)
    armed = a.send and a.confirm
    res = run_monthly_report(year, month, armed=armed, limit=a.limit,
                             only_email=a.email, include_html=a.html)

    if a.html and a.email:
        for r in res.get("recipients", []):
            print(f"=== {r['email']} [{r['lane']}] ===")
            print(f"Subject: {r['subject']}\n")
            print(r.get("html", "(no html)"))
        return

    slim = {k: v for k, v in res.items() if k != "recipients"}
    print(json.dumps(slim, indent=2, default=str))
    print("\nRECIPIENTS")
    for r in res.get("recipients", []):
        tools = ", ".join(f"{t['tool']}:{t['calls']}" for t in r["top_tools"][:3]) or "-"
        print(f"  {r['lane']:<10} {r['email']:<34} {r['plan']:<10} "
              f"inv={r['invoices']:<2} calls={r['calls']:<7} {tools}")


if __name__ == "__main__":
    _cli()
