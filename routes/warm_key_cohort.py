"""warm_key_cohort.py — who gave us an email and never bought (2026-09-17).

WHY THIS EXISTS
===============
Measured 2026-09-17, `mcp_dev_keys WHERE status='active'`:

    identified 540 · free 113 · paid 47 · enterprise 6

Five conversions in the trailing 30 days, and by platform they were web-direct
3, organic-direct 1, value-harness 1 — **none from the agent channel**, whose
reach is 66 agents / ~462 real external calls per week. The whole conversion
effort that week went into instrumenting that channel.

Meanwhile ~493 addresses have been bound to a key and have never paid. That is
the largest self-selected, infrastructure-buying audience the product holds,
and nothing reads it. `audience_export` looks like it covers this and does
NOT: it reads the `users` table, the WEB signup population. The keys are in
`mcp_dev_keys`, a different table and a different set of people.

WHAT THIS ANSWERS, before anyone drafts an email
================================================
The cohort is only worth working if it is people rather than abandoned
machine keys, so the aggregate leads and the rows follow:

  mailable                how many survive email + internal + suppression +
                          already-paid filters. The only number an outreach
                          plan may size itself on.
  domain_kind             corporate vs consumer vs ours. A key bound to a
                          company domain is a buyer; one bound to gmail may
                          still be, one bound to @dchub.cloud is us.
  engaged_after_bind      made >=1 call AFTER the key was minted. Bound-and-
                          vanished is a different prospect from bound-and-used,
                          and the split is the difference between "warm list"
                          and "540 abandoned minting attempts".
  already_paid            the SAME address also holds a paid/enterprise key.
                          Pitching an existing customer is worse than silence.
  top_walls               which tool's wall they hit (mcp_upgrade_signals).
                          An outreach line naming the tool beats a generic one.
  age_buckets             days since bind. A key bound 6 months ago is cold.

★ EVERY FIGURE IS A COUNT OF ROWS THAT EXIST, and each filter is published
separately, so a reader can see what each one removed rather than being handed
one number. `mailable` is deliberately the LAST and smallest.

★ NEVER RETURNS api_key. The cohort is addresses, not credentials.

  GET /api/v1/admin/audience/warm-keys            -> aggregate (+ sample rows)
  GET /api/v1/admin/audience/warm-keys?rows=all   -> aggregate + every row
  GET /api/v1/admin/audience/warm-keys.csv        -> the outreach list

Admin-gated exactly like audience_export (X-Admin-Key / X-Internal-Key), and it
reuses that module's gate rather than restating it — two copies of an auth
check is how one of them ends up weaker.

Read-only. Writes nothing. Never raises: a failed sub-read is reported as its
own error key and the rest of the aggregate still answers.
"""
from __future__ import annotations

import csv
import io
import logging
import os

from flask import Blueprint, Response, jsonify, request

# Re-exported under the old names, NOT renamed: audience_keys_export borrows
# `_is_internal` from this module by object identity and pins that it does.
from routes._audience_identity import (  # noqa: F401
    INTERNAL_MARKERS as _INTERNAL_MARKERS, is_internal_email as _is_internal,
    is_harness_persona_email as _is_harness_persona)

logger = logging.getLogger(__name__)
warm_key_cohort_bp = Blueprint("warm_key_cohort", __name__)

# ★★ 2026-09-17 — THIS WAS AN ALLOWLIST INVERSION BUG, MEASURED IN PRODUCTION.
#
# The first version read the paid set from `tier_registry.paid_plans()`, on the
# principle "read the canon, never type it". The canon returned
#     {enterprise, pro, research_seed, developer, founding, team, starter}
# — the PLAN vocabulary. `mcp_dev_keys.tier` stores a COARSER one:
#     free · identified · paid · enterprise
# The literal string 'paid' is not a plan name, so every `tier='paid'` key fell
# straight through the exclusion. Live read: 26 PAYING CUSTOMERS appeared in
# `mailable`, and `already_paid` reported false for each of them because it was
# built from the same list.
#
# The rule "read the canon" was right; the mistake was reading the WRONG
# producer. The authority on what values this column holds is the column, not a
# registry that happens to use the word tier for something else.
#
# ★ SO THE FILTER IS INVERTED. We no longer enumerate what is paid; we
# enumerate the only tiers known NOT to be, and everything else — including a
# tier nobody has added yet — is excluded. The failure modes are not symmetric:
# under-mailing costs a lead, over-mailing pitches an upgrade to someone who
# already bought. `excluded_unknown_tier` publishes what this removed, so a new
# tier is visible rather than silently dropped.
#
# Measured vocabulary of mcp_dev_keys.tier on 2026-09-17 (keys_by_tier, active):
#   free 113 · identified 540 · paid 47 · enterprise 6
NON_PAID_TIERS = frozenset({
    "free", "identified", "anonymous", "anon", "", "none",
})

# Kept only so a reader can see what the registry calls paid; it is NOT the
# filter, because it does not speak this column's vocabulary.
_REGISTRY_PLAN_NAMES_FOR_REFERENCE = (
    "starter", "developer", "pro", "founding", "team", "enterprise",
    "research_seed")

# Consumer mailbox providers. Not a disqualifier — a founder on gmail is still a
# founder — but the split changes what the list is worth, so it is published.
_CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "aol.com", "gmx.com", "mail.com",
    "yandex.com", "zoho.com", "fastmail.com", "hey.com", "duck.com",
}

# Ours. An address here is the operator, a probe, or a reviewer comp — never a
# prospect, and counting one as a lead is the failure this file's own history
# is full of. The rule — `_INTERNAL_MARKERS` and `_is_internal` — lives in
# routes/_audience_identity and is imported at the top of this file, so the
# free-users export applies the same list instead of a copy (or none: before
# 2026-09-21 it applied none, and 21 of its 149 rows were ours).


def is_non_paid_tier(tier) -> bool:
    """True ONLY for a tier positively known not to pay.

    An unknown value answers False and is excluded. That is the safe direction:
    a tier we have never seen might be paid, and the cost of guessing wrong is
    an upgrade pitch to a customer.
    """
    return str(tier or "").strip().lower() in NON_PAID_TIERS


def _admin_ok() -> bool:
    """The SAME gate audience_export applies, called not copied."""
    try:
        from routes.audience_export import _admin_ok as _gate
        return bool(_gate())
    except Exception:  # noqa: BLE001
        # Fail CLOSED. An import error must not open an endpoint that returns
        # every address we hold.
        logger.warning("[warm_keys] admin gate unavailable — refusing")
        return False


def _conn():
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception as e:  # noqa: BLE001
        logger.warning("[warm_keys] no DB: %s", e)
        return None


def _release(c, error=False):
    try:
        from main import return_pg_connection
        return_pg_connection(c, error=error)
    except Exception:  # noqa: BLE001
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _domain(email: str) -> str:
    e = (email or "").strip().lower()
    return e.rsplit("@", 1)[1] if "@" in e else ""


def _domain_kind(email: str) -> str:
    if _is_internal(email):
        return "ours"
    d = _domain(email)
    if not d:
        return "unknown"
    return "consumer" if d in _CONSUMER_DOMAINS else "corporate"


def _bucket_days(n) -> str:
    if n is None:
        return "unknown"
    n = int(n)
    if n <= 7:
        return "0-7d"
    if n <= 30:
        return "8-30d"
    if n <= 90:
        return "31-90d"
    if n <= 180:
        return "91-180d"
    return "180d+"


# ── the reads ────────────────────────────────────────────────────────────
# One statement per question, each in its own try, so a missing column in one
# does not blank the whole aggregate. A sub-read that fails names itself in
# `errors` — never reports 0.

def _gather() -> tuple:
    c = _conn()
    if c is None:
        return [], {"error": "no_db"}
    non_paid = sorted(NON_PAID_TIERS)
    rows, errors = [], {}
    suppressed, paid_emails, calls, walls = set(), set(), {}, {}
    consented, last_walls = set(), {}
    excluded_tiers = None
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT lower(email) FROM email_suppression "
                            "WHERE email IS NOT NULL")
                suppressed = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:  # noqa: BLE001
                errors["suppression"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # ★ r-optin-consents (2026-09-24): CONFIRMED consent held by the
            # ADDRESS. routes/marketing_opt_in._set_opted_in writes the key
            # flag only on keys bound to the address AT confirm time, and
            # always writes opt_in_consents.confirmed_at. A key bound after the
            # click, or an address that confirmed before it held any key, had
            # consent this export could not see. Still only the double-opt-in
            # click: a request row with no confirmed_at is NOT consent.
            try:
                cur.execute("SELECT DISTINCT lower(trim(email)) FROM opt_in_consents "
                            "WHERE confirmed_at IS NOT NULL AND email IS NOT NULL")
                consented = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:  # noqa: BLE001
                errors["opt_in_consents"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # Addresses that ALREADY hold a paid key. Same table, so this is
            # the authoritative "is a customer", not an inference from tier.
            try:
                # Addresses holding a key on ANY tier not known to be free.
                # Inverted for the same reason the cohort filter is: an
                # unrecognised tier must read as "may be a customer".
                cur.execute(
                    "SELECT DISTINCT lower(trim(email)) FROM mcp_dev_keys "
                    "WHERE email IS NOT NULL AND email <> '' "
                    "AND lower(COALESCE(tier,'')) <> ALL(%s)", (non_paid,))
                paid_emails = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:  # noqa: BLE001
                errors["paid_emails"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # What the inversion removed, BY TIER. A tier appearing here that
            # should have been mailable is the signal that NON_PAID_TIERS needs
            # a new member — published rather than silently dropped.
            try:
                cur.execute(
                    """SELECT lower(COALESCE(tier,'')) AS tier, COUNT(*) AS n
                         FROM mcp_dev_keys
                        WHERE status = 'active'
                          AND email IS NOT NULL AND email <> ''
                          AND position('@' in email) > 1
                          AND lower(COALESCE(tier,'')) <> ALL(%s)
                        GROUP BY 1 ORDER BY 2 DESC""", (non_paid,))
                excluded_tiers = {r[0] or "(none)": int(r[1] or 0)
                                  for r in (cur.fetchall() or [])}
            except Exception as e:  # noqa: BLE001
                errors["excluded_tiers"] = f"{type(e).__name__}: {str(e)[:90]}"
                excluded_tiers = None
                c.rollback()

            # THE COHORT: an active key, a real address, not on a paid tier.
            # Includes `free`-with-email as well as `identified` — both gave us
            # an address and neither has bought. `tier` is carried through so
            # the 540 stays visible inside the total.
            try:
                cur.execute(
                    """SELECT lower(trim(email)) AS email,
                              lower(tier)        AS tier,
                              MIN(created_at)    AS bound_at,
                              COUNT(*)           AS keys_held,
                              -- r-consent (2026-09-17): CONSENT IS NOT A
                              -- DERIVED FIELD. mcp_dev_keys.metadata->>
                              -- 'marketing_opt_in' is set ONLY by the
                              -- tokenized double-opt-in confirm click
                              -- (main.py:39722); the bind path defaults it
                              -- false and the paywall CTA explicitly never
                              -- sets it. TRUE here for ANY of the address's
                              -- keys, because consent attaches to the person.
                              bool_or(metadata->>'marketing_opt_in' = 'true')
                                AS marketing_opt_in,
                              MAX(metadata->>'name')  AS name,
                              -- r-harness-personas (2026-09-25): an agent
                              -- test harness bound invented addresses under
                              -- many made-up client names. Distinct names on
                              -- claim_api keys, and whether ANY key for the
                              -- address was verified (OAuth or a confirm
                              -- click), are what separate it from a person.
                              COUNT(DISTINCT metadata->>'client_name')
                                FILTER (WHERE metadata->>'source' = 'claim_api')
                                AS claim_client_names,
                              bool_or(metadata->>'email_verified_at' IS NOT NULL
                                      OR metadata->>'source' = 'workos_oauth')
                                AS email_verified
                         FROM mcp_dev_keys
                        WHERE status = 'active'
                          AND email IS NOT NULL AND email <> ''
                          AND position('@' in email) > 1
                          AND lower(COALESCE(tier,'')) = ANY(%s)
                        GROUP BY 1, 2
                        ORDER BY 3 DESC NULLS LAST""", (non_paid,))
                raw = cur.fetchall() or []
            except Exception as e:  # noqa: BLE001
                _release(c, error=True)
                return [], {"error": f"cohort read failed: "
                                     f"{type(e).__name__}: {str(e)[:120]}"}

            # Did they ever call AFTER binding? mcp_call_log's time column is
            # `timestamp`, not created_at — the install_artifact basis records
            # that trap. Keyed on the key, then rolled up to the address.
            try:
                cur.execute(
                    """SELECT lower(trim(k.email)) AS email,
                              COUNT(l.api_key)     AS calls_after,
                              MAX(l.timestamp)     AS last_call
                         FROM mcp_dev_keys k
                         LEFT JOIN mcp_call_log l
                                ON l.api_key = k.api_key
                               AND l.timestamp > k.created_at
                        WHERE k.status = 'active'
                          AND k.email IS NOT NULL AND k.email <> ''
                        GROUP BY 1""")
                calls = {r[0]: (int(r[1] or 0), r[2]) for r in cur.fetchall()
                         if r and r[0]}
            except Exception as e:  # noqa: BLE001
                errors["calls_after_bind"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # Which wall did they hit? The tool is the one thing that makes an
            # outreach line specific instead of generic.
            try:
                cur.execute(
                    """SELECT lower(trim(s.user_email)) AS email,
                              s.tool_requested,
                              COUNT(*) AS n
                         FROM mcp_upgrade_signals s
                        WHERE s.user_email IS NOT NULL AND s.user_email <> ''
                          AND s.tool_requested IS NOT NULL
                        GROUP BY 1, 2
                        ORDER BY 3 DESC""")
                for email, tool, n in (cur.fetchall() or []):
                    if email and email not in walls:
                        walls[email] = (tool, int(n or 0))
            except Exception as e:  # noqa: BLE001
                errors["walls"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()

            # The MOST RECENT wall, beside the most-hit one: a soft offer
            # names what they tried last, which is not always what they tried
            # most (2026-09-24, item 6 of the convert-P0 ship list).
            try:
                cur.execute(
                    """SELECT DISTINCT ON (lower(trim(s.user_email)))
                              lower(trim(s.user_email)), s.tool_requested
                         FROM mcp_upgrade_signals s
                        WHERE s.user_email IS NOT NULL AND s.user_email <> ''
                          AND s.tool_requested IS NOT NULL
                        ORDER BY lower(trim(s.user_email)), s.created_at DESC""")
                last_walls = {r[0]: r[1] for r in (cur.fetchall() or [])
                              if r and r[0]}
            except Exception as e:  # noqa: BLE001
                errors["last_walls"] = f"{type(e).__name__}: {str(e)[:90]}"
                c.rollback()
    except Exception as e:  # noqa: BLE001
        _release(c, error=True)
        return [], {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    _release(c)

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    seen = set()
    for (email, tier, bound_at, keys_held, opt_in, name,
         claim_names, verified) in raw:
        if not email or email in seen:
            continue
        seen.add(email)
        days = None
        if bound_at is not None:
            try:
                b = bound_at if bound_at.tzinfo else bound_at.replace(
                    tzinfo=_dt.timezone.utc)
                days = max(0, (now - b).days)
            except Exception:  # noqa: BLE001
                days = None
        calls_after, last_call = calls.get(email, (0, None))
        tool, wall_hits = walls.get(email, ("", 0))
        rows.append({
            "email": email,
            "tier": tier or "",
            "domain": _domain(email),
            "domain_kind": _domain_kind(email),
            "days_since_bind": days,
            "age_bucket": _bucket_days(days),
            "keys_held": int(keys_held or 0),
            "calls_after_bind": calls_after,
            "last_call": (last_call.isoformat() if hasattr(last_call, "isoformat")
                          else ""),
            "top_tool_wall": tool or "",
            "last_tool_wall": last_walls.get(email) or "",
            "wall_hits": wall_hits,
            "already_paid": email in paid_emails,
            "suppressed": email in suppressed,
            "marketing_opt_in": bool(opt_in) or email in consented,
            "name": (name or "").strip(),
            "claim_client_names": int(claim_names or 0),
            "email_verified": bool(verified),
            "harness_persona": _harness_persona(email, claim_names, verified),
        })
    meta = {"excluded_by_tier": excluded_tiers}
    if errors:
        meta["errors"] = errors
    return rows, meta


# An unverified address bound under this many DISTINCT claim_api client names
# is treated as a harness persona. Measured 2026-09-25: the known personas had
# 2-5; every real prospect in the cohort had 1 (client names like "Claude.ai",
# "claude-code-brain", a Cowork workspace). A real
# person running two agents on one unverified typed address is excluded too —
# accepted, because the other error mails a stranger's inbox. Every excluded
# address is listed in `harness_personas` for review, never dropped silently.
HARNESS_MIN_CLIENT_NAMES = 2


def _harness_persona(email, claim_client_names, email_verified) -> bool:
    if _is_harness_persona(email):
        return True
    return (not email_verified
            and int(claim_client_names or 0) >= HARNESS_MIN_CLIENT_NAMES)


def _mailable(r: dict) -> bool:
    """REACHABLE: a real, non-customer, non-suppressed address we hold.

    This is NOT permission to email. See _sendable — the two are deliberately
    separate numbers, because conflating "we have an address" with "we may use
    it" is how a warm-list plan becomes a compliance incident.
    """
    return (bool(r["email"]) and r["domain_kind"] != "ours"
            and not r.get("harness_persona")
            and not r["already_paid"] and not r["suppressed"])


def _sendable(r: dict) -> bool:
    """★ MAY BE EMAILED. Mailable AND explicit marketing consent.

    Transactional bind is NOT marketing opt-in. The consent flag is written
    only by the tokenized double-opt-in confirm click; the bind endpoint
    defaults it false and the paywall opt-in CTA explicitly never sets it. The
    outreach engine's own segment hard-gates on the same flag, and
    mcp_gatekeeper records that ~0 addresses carry it — so a sendable count of
    0 beside a mailable count of many is the CORRECT reading, and it names the
    real blocker: consent, not the size of the list.
    """
    return _mailable(r) and bool(r.get("marketing_opt_in"))


def summarize(rows: list) -> dict:
    """Aggregate, with every filter's effect published separately.

    Handed one number, a reader cannot tell a warm list from a pile of
    abandoned key mints. Each count below is rows that EXIST after exactly one
    more filter than the line above it, and `mailable` is last.
    """
    def n(pred):
        return sum(1 for r in rows if pred(r))
    by = {}
    for r in rows:
        by[r["tier"] or "(none)"] = by.get(r["tier"] or "(none)", 0) + 1
    ages, kinds, tools = {}, {}, {}
    for r in rows:
        if not _mailable(r):
            continue
        ages[r["age_bucket"]] = ages.get(r["age_bucket"], 0) + 1
        kinds[r["domain_kind"]] = kinds.get(r["domain_kind"], 0) + 1
        if r["top_tool_wall"]:
            tools[r["top_tool_wall"]] = tools.get(r["top_tool_wall"], 0) + 1
    return {
        "cohort_total": len(rows),
        "by_tier": dict(sorted(by.items(), key=lambda kv: -kv[1])),
        "removed_ours": n(lambda r: r["domain_kind"] == "ours"),
        "removed_harness_personas": n(lambda r: r.get("harness_persona")
                                      and r["domain_kind"] != "ours"),
        "removed_already_paid": n(lambda r: r["already_paid"]
                                  and r["domain_kind"] != "ours"
                                  and not r.get("harness_persona")),
        "removed_suppressed": n(lambda r: r["suppressed"]
                                and r["domain_kind"] != "ours"
                                and not r.get("harness_persona")
                                and not r["already_paid"]),
        # The addresses removed as personas, for a human to check. A real
        # person here is a false positive to fix, not a lead to mail blind.
        "harness_personas": sorted(r["email"] for r in rows
                                   if r.get("harness_persona")
                                   and r["domain_kind"] != "ours"),
        "mailable": n(_mailable),
        # ★ THE NUMBER AN OUTREACH SEND MAY SIZE ITSELF ON. `mailable` is
        # reachability; this is permission. They are different, and the gap
        # between them is the consent gap, not a data gap.
        "sendable_with_consent": n(_sendable),
        "mailable_without_consent": n(lambda r: _mailable(r)
                                      and not r.get("marketing_opt_in")),
        "mailable_corporate": n(lambda r: _mailable(r)
                                and r["domain_kind"] == "corporate"),
        "mailable_engaged_after_bind": n(lambda r: _mailable(r)
                                         and r["calls_after_bind"] > 0),
        "mailable_bound_and_vanished": n(lambda r: _mailable(r)
                                         and r["calls_after_bind"] == 0),
        "mailable_hit_a_wall": n(lambda r: _mailable(r) and r["wall_hits"] > 0),
        "mailable_by_age": dict(sorted(ages.items())),
        "mailable_by_domain_kind": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "mailable_top_walls": dict(sorted(tools.items(),
                                          key=lambda kv: -kv[1])[:12]),
        "basis": (
            "Cohort = mcp_dev_keys WHERE status='active' AND a parseable email "
            "AND tier IS ONE OF the tiers positively known not to pay "
            "(NON_PAID_TIERS: free, identified, anonymous, anon, blank), "
            "deduped by lower(email). ★ INVERTED ON PURPOSE, after the first "
            "live read put 26 PAYING customers in `mailable`: the previous "
            "version excluded tier_registry.paid_plans(), which is the PLAN "
            "vocabulary (pro/starter/developer/...), while this column stores a "
            "coarser one (free/identified/paid/enterprise) — so the literal "
            "'paid' matched nothing. An unrecognised tier is now EXCLUDED, "
            "because under-mailing costs a lead and over-mailing pitches an "
            "upgrade to someone who already bought; excluded_unknown_or_paid_"
            "tier publishes what that removed. This is the AGENT-KEY "
            "population; routes/audience_export reads the `users` table, which "
            "is the WEB signup population — different table, different people. "
            "removed_* are applied in the order listed and each counts only "
            "rows the lines above it did not already remove, so they sum into "
            "cohort_total with `mailable` and nothing is double-counted. "
            "engaged_after_bind = >=1 mcp_call_log row on that key later than "
            "the key's created_at (that table's time column is `timestamp`). "
            "★ `mailable` is REACHABILITY, never permission: "
            "`sendable_with_consent` is the subset carrying an explicit "
            "mcp_dev_keys.metadata->>'marketing_opt_in' = 'true', which is "
            "written ONLY by the tokenized double-opt-in confirm click. The "
            "bind endpoint defaults it false and the paywall opt-in CTA never "
            "sets it, so a sendable count of 0 beside a large mailable count "
            "is the correct reading and names consent as the blocker. "
            "removed_ours covers _INTERNAL_MARKERS AND the named operator "
            "addresses in routes/_audience_identity, because a consumer "
            "mailbox carries no marker to match. "
            "removed_harness_personas = addresses an agent test harness "
            "invented: the named set in routes/_audience_identity, plus any "
            "address with no verified key (no workos_oauth source, no "
            "email_verified_at) bound under >= HARNESS_MIN_CLIENT_NAMES "
            "distinct claim_api client names. The claim IP is NOT used: MCP "
            "claims arrive via our own Railway-hosted MCP server, so real "
            "users carry Railway IPs too. harness_personas lists them. "
            "top_tool_wall is the most frequent mcp_upgrade_signals."
            "tool_requested for the address, joined on user_email, which only "
            "exists where a bind wrote it back — a blank means unknown, never "
            "'hit no wall'. api_key is never returned."),
    }


_CSV_FIELDS = ["email", "name", "tier", "domain", "domain_kind",
               "days_since_bind", "age_bucket", "calls_after_bind", "last_call",
               "top_tool_wall", "last_tool_wall", "wall_hits", "keys_held",
               "marketing_opt_in"]


@warm_key_cohort_bp.route("/api/v1/admin/audience/warm-keys", methods=["GET"])
def warm_keys_json():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    rows, meta = _gather()
    if meta.get("error"):
        return jsonify(ok=False, error=meta["error"]), 200
    out = {"ok": True, "metric": "warm_key_cohort", **summarize(rows)}
    out["excluded_unknown_or_paid_tier"] = meta.get("excluded_by_tier")
    out["non_paid_tiers"] = sorted(NON_PAID_TIERS)
    if meta.get("errors"):
        out["partial_reads"] = meta["errors"]
        out["partial_note"] = ("a sub-read failed and is named above; the "
                               "field it feeds is 0 for UNKNOWN reasons, not "
                               "because the rows are absent")
    want = (request.args.get("rows") or "").strip().lower()
    consent_any = (request.args.get("consent") or "").strip().lower() == "any"
    keep = _mailable if consent_any else _sendable
    picked = [r for r in rows if keep(r)]
    out["rows_consent_gated"] = not consent_any
    out["rows"] = picked if want == "all" else picked[:25]
    out["rows_returned"] = len(out["rows"])
    out["rows_note"] = (
        ("rows carrying explicit marketing consent only (the set that may be "
         "emailed), newest bind first. Pass consent=any for the REACHABLE set "
         "instead — that is for analysis, not for sending."
         if not consent_any else
         "REACHABLE rows, consent NOT required — analysis only. Every row "
         "carries its marketing_opt_in flag; do not send to a false one.")
        + " Pass rows=all for every row, or use the .csv route.")
    return jsonify(out), 200


@warm_key_cohort_bp.route("/api/v1/admin/audience/warm-keys.csv", methods=["GET"])
def warm_keys_csv():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    rows, meta = _gather()
    if meta.get("error"):
        return jsonify(ok=False, error=meta["error"]), 200
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    # ★ CONSENT-GATED BY DEFAULT. This file is the thing someone pastes into a
    # sending tool, so the default must be the set that may lawfully receive
    # mail. `?consent=any` returns the reachable set for analysis and labels
    # every row with its flag — it is not a send list.
    consent_any = (request.args.get("consent") or "").strip().lower() == "any"
    keep = _mailable if consent_any else _sendable
    for r in rows:
        if keep(r):
            w.writerow(r)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="dchub-warm-keys.csv"',
                 "Cache-Control": "no-store"})


def register_warm_key_cohort(app) -> None:
    app.register_blueprint(warm_key_cohort_bp)
