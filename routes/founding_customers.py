"""Phase FF+25-followup-r19 (2026-05-20) — founding customers.
==========================================================================

Kevin Serfass (kevin.d.serfass@gmail.com) is the first new paid
customer to come in via the website front-door (not the MCP funnel).
$9 → $49 within 60 seconds at 2026-05-20 20:03 UTC. Pure top-funnel
conversion driven by Switzerland positioning + the brand polish.

The first dozen paid customers matter disproportionately:
  · They're proof the value-prop lands
  · They become reference customers (with permission)
  · They tell us which use cases the product actually solves
  · They tolerate the rough edges that prevent customer #50 from
    converting

This module gives us a queryable founding-customer cohort + a brain
signal so the Inspector celebrates / tracks these specifically.

ENDPOINTS:
  POST /api/v1/admin/founding-customers/tag      add an email to the
                                                   founding cohort
  POST /api/v1/admin/founding-customers/untag    remove
  GET  /api/v1/admin/founding-customers           list (admin)
  GET  /api/v1/founding-customers/count           public count

Used by:
  · brain_inspector — adds founding_customers count to signal block
  · /status dashboard — surfaces the count as a positive metric
  · Inspector system prompt rule: when founding_customers > 0,
    name them as a positive Healthy item
"""
import os
from internal_auth import accepted_internal_keys
import json
import logging
import datetime
from flask import Blueprint, jsonify, request, Response
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)
founding_customers_bp = Blueprint("founding_customers", __name__)


_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    c = _get_db()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS founding_customers (
                    email           TEXT PRIMARY KEY,
                    tagged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    plan_at_tag     TEXT,
                    first_payment_at TIMESTAMPTZ,
                    stripe_customer_id TEXT,
                    notes           TEXT,
                    contact_status  TEXT DEFAULT 'new',
                    contacted_at    TIMESTAMPTZ,
                    consented_to_cite BOOLEAN DEFAULT FALSE
                )
            """)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning(f"[founding-customers] table create failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/admin/founding-customers/tag",
                              methods=["POST"])
def tag_founding():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    p = request.get_json(silent=True) or {}
    email = (p.get("email") or "").lower().strip()
    if not email:
        return jsonify(ok=False, error="email_required"), 400
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO founding_customers
                  (email, plan_at_tag, first_payment_at,
                   stripe_customer_id, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                  notes = COALESCE(founding_customers.notes, '')
                          || E'\\n' || COALESCE(EXCLUDED.notes, '')
            """, (
                email, p.get("plan"),
                p.get("first_payment_at"),
                p.get("stripe_customer_id"),
                p.get("notes"),
            ))
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, email=email,
                       tagged_at=datetime.datetime.utcnow().isoformat() + "Z")
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/admin/founding-customers",
                              methods=["GET"])
def list_founding():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            # slow_pages quick-win: cap at 200 — the founding-customers
            # roster is small but we still want a hard ceiling so a
            # runaway tag never blows up the admin endpoint.
            cur.execute("""
                SELECT email, tagged_at, plan_at_tag, first_payment_at,
                       stripe_customer_id, contact_status, contacted_at,
                       consented_to_cite, notes
                  FROM founding_customers
                 ORDER BY tagged_at DESC
                 LIMIT 200
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "email": r[0],
                    "tagged_at": str(r[1]) if r[1] else None,
                    "plan_at_tag": r[2],
                    "first_payment_at": str(r[3]) if r[3] else None,
                    "stripe_customer_id": r[4],
                    "contact_status": r[5],
                    "contacted_at": str(r[6]) if r[6] else None,
                    "consented_to_cite": r[7],
                    "notes": r[8],
                })
        return jsonify(ok=True, count=len(rows), founding=rows)
    finally:
        try: c.close()
        except Exception: pass


# ── What the counter COUNTS (owner decision, 2026-09-02) ─────────────
# The public counter is a scarcity meter for the $99 FOUNDING LICENCE, so
# it must count that SKU and nothing else.
#
# Until this change it counted every row in founding_customers, and the
# Stripe webhook auto-tagged EVERY paid plan into that table. Measured at
# the Railway origin 2026-09-02T06:16Z, the 18 it published were:
#   founding 5 · starter 5 · pro 4 (incl. the owner's own $0 comp)
#   · developer 3 · enterprise 1 (the $3,000/yr research seat)
# i.e. 13 of the 18 "founding licences claimed" were not founding licences.
#
# WHICH FIELD: `users.plan` — the plan the Stripe webhook itself writes and
# the field tier_registry treats as canonical ('founding' is its own tier,
# priced at 99 in TIER_PRICE_USD_MONTH). NOT founding_customers.plan_at_tag,
# which is a snapshot taken at FIRST payment: three customers who started on
# starter/developer and later upgraded onto the founding licence still carry
# their old plan_at_tag, so plan_at_tag alone reports 5 where Stripe reports 7.
#
# WHY STILL JOINED TO THE COHORT TABLE: `users.plan='founding'` alone is 10
# live — it includes three Feb/Mar-2026 accounts granted the founding tier by
# hand, before the $99 SKU existed (r-founder99, 2026-06-26) and before this
# cohort table did. A founding_customers row is only ever created from a
# Stripe checkout/subscription event (or a deliberate admin tag), so the join
# is the "actually bought the SKU" half of the predicate. Cohort ∩
# users.plan='founding' = 7, which is exactly the number of Founding Member
# $99 subscriptions Stripe has ever created.
FOUNDING_SKU_PLAN = "founding"

# fc.email is stored lower-cased; LOWER() on both sides anyway so a
# differently-cased users row cannot silently drop a paid licence.
_SKU_COUNT_SQL = (
    "SELECT COUNT(*) FROM founding_customers fc "
    "JOIN users u ON LOWER(u.email) = LOWER(fc.email) "
    "WHERE u.plan = %s"
)
# Degraded path only (no users table / join unavailable). Still SKU-filtered:
# there is deliberately NO code path left that counts the cohort unfiltered.
_SKU_COUNT_FALLBACK_SQL = (
    "SELECT COUNT(*) FROM founding_customers WHERE plan_at_tag = %s"
)


def _count_founding_sku(cur) -> int:
    """How many $99 founding licences have been claimed.

    Raises if BOTH the join and the plan_at_tag fallback fail — callers
    treat that as "unreadable" and report claimed=0 / programme open,
    which is the safe direction on a money surface.
    """
    try:
        cur.execute(_SKU_COUNT_SQL, (FOUNDING_SKU_PLAN,))
        return int((cur.fetchone() or [0])[0] or 0)
    except Exception as e:
        logger.warning(f"[founding-customers] SKU join failed, "
                       f"falling back to plan_at_tag: {e}")
    # Postgres aborts the transaction on a failed statement; the fallback
    # cannot run on the same connection until it is rolled back.
    try:
        cur.connection.rollback()
    except Exception:
        pass
    cur.execute(_SKU_COUNT_FALLBACK_SQL, (FOUNDING_SKU_PLAN,))
    return int((cur.fetchone() or [0])[0] or 0)


def founding_status() -> dict:
    """The ONE source of truth for the public founding counters.

    Read by BOTH public surfaces — /api/v1/founding-customers/count
    (homepage pill) and public_endpoints /api/founding-members (pricing
    seats meter) — so they can never contradict each other. Before
    2026-08-01 the homepage counted this cohort table against cap 25
    while /api/founding-members counted users.plan='founding' against a
    hardcoded total of 10: one more sale would have flipped the pricing
    card to "All founding licenses claimed" and self-disabled the money
    CTA mid-renewal-wave.

    claimed = founding_customers rows whose customer is ON the $99
    founding SKU (_count_founding_sku above) — the same population
    auto_tag_if_under_cap now writes and measures its cap against. It is
    NOT "the first 25 paid customers of any plan"; see the note above
    _SKU_COUNT_SQL for the owner decision and the live numbers.
    cap     = FOUNDING_CUSTOMERS_CAP env (default 25), the owner's
    scarcity knob; setting it at or below claimed closes the program.
    A DB failure reports claimed=0 / program still active — an outage
    must never read as "sold out" on a money surface.
    """
    claimed = 0
    c = _get_db()
    if c is not None:
        try:
            with c.cursor() as cur:
                claimed = _count_founding_sku(cur)
        except Exception as e:
            logger.warning(f"[founding-customers] status count failed: {e}")
        finally:
            try: c.close()
            except Exception: pass
    remaining = max(0, FOUNDING_CAP - claimed)
    return {
        "claimed": claimed,
        "cap": FOUNDING_CAP,
        "remaining": remaining,
        # ★ r-price-collapse (2026-09-05): the founding PROGRAMME is retired.
        #   $99 is simply the Pro list price now, so there is no scarcity to
        #   publish and nothing to be short of. Set HERE, at the one source
        #   both public surfaces read, and not on either endpoint — patching
        #   one of them is how /api/v1/founding-customers/count and
        #   /api/founding-members came to contradict each other before, which
        #   is the whole reason this function exists (see the docstring).
        #   claimed/cap/remaining stay truthfully computed so no consumer
        #   KeyErrors and the cohort stays countable internally.
        "program_active": False,
        "retired": True,
    }


@founding_customers_bp.route("/api/v1/founding-customers/count",
                              methods=["GET"])
def public_count():
    """Public — just the count, no PII. Brain Inspector reads this and
    the Inspector brief celebrates each milestone (1, 5, 10, 25, 50)."""
    _ensure_table()
    st = founding_status()
    n = st["claimed"]
    return jsonify(count=n,
                   cap=st["cap"],
                   remaining=st["remaining"],
                   program_active=st["program_active"],
                   milestone=("first" if n == 1
                               else ("5+" if n >= 5
                                     else f"{n} of 5 to milestone")),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")


# ── Auto-tag hook ────────────────────────────────────────────────────
# Called from the Stripe webhook on checkout.session.completed +
# customer.subscription.created.
#
# 2026-09-02: this used to tag EVERY paid plan, which is how the public
# counter came to publish 18 "founding licences claimed" of which 13 were
# starters, developers, pros, the owner's own comp and a research seat.
# It now tags only customers on the $99 founding SKU, so a starter no
# longer consumes a founding seat — nor receives the "you are founding
# member #N of 25" admin ping and welcome mail, which fire off `tagged`.
#
# Cap is FOUNDING_CUSTOMERS_CAP env var (default 25) and is measured with
# the SAME SKU predicate the public counter reads, so the gate and the
# published number can never disagree about what a founding member is.
FOUNDING_CAP = int(os.environ.get("FOUNDING_CUSTOMERS_CAP", "25"))


def auto_tag_if_under_cap(
    email: str,
    plan: str = "developer",
    stripe_customer_id: str | None = None,
    first_payment_at: str | None = None,
    notes: str | None = None,
) -> dict:
    """Idempotently tag a paid customer into founding_customers if the
    cohort is below FOUNDING_CAP. Safe to call from the Stripe webhook —
    no exception bubbles up if the table is missing or the connection
    fails.

    Only the $99 founding SKU is tagged: any other plan returns
    {tagged: False, reason: "not_founding_sku (<plan>)"} and writes
    nothing.

    Returns {tagged: bool, position: int|None, cap: int}.
    """
    out: dict = {"tagged": False, "position": None,
                 "cap": FOUNDING_CAP, "reason": ""}
    if not email or "@" not in email:
        out["reason"] = "invalid_email"
        return out
    plan_key = (plan or "").strip().lower()
    if plan_key != FOUNDING_SKU_PLAN:
        # A paid customer on any other plan is a customer, not a founding
        # licence holder. Nothing is written and no founding mail fires.
        out["reason"] = f"not_founding_sku ({plan_key or 'unknown'})"
        return out
    email = email.lower().strip()
    try:
        _ensure_table()
        c = _get_db()
        if c is None:
            out["reason"] = "no_db"
            return out
        try:
            with c.cursor() as cur:
                cohort_size = _count_founding_sku(cur)
                if cohort_size >= FOUNDING_CAP:
                    out["reason"] = f"cap_reached ({cohort_size}/{FOUNDING_CAP})"
                    return out
                # Already tagged?
                cur.execute(
                    "SELECT 1 FROM founding_customers WHERE email = %s",
                    (email,),
                )
                if cur.fetchone():
                    out["reason"] = "already_tagged"
                    return out
                cur.execute("""
                    INSERT INTO founding_customers
                      (email, plan_at_tag, first_payment_at,
                       stripe_customer_id, notes, contact_status)
                    VALUES (%s, %s, %s, %s, %s, 'auto-tagged')
                """, (email, plan, first_payment_at,
                       stripe_customer_id, notes))
            try: c.commit()
            except Exception: pass
            out["tagged"] = True
            out["position"] = cohort_size + 1
            return out
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        out["reason"] = f"exception: {str(e)[:160]}"
        return out


def notify_admin_of_founding(email: str, position: int, plan: str,
                              stripe_customer_id: str | None) -> None:
    """Send Jonathan an admin alert email so he knows immediately when
    a new founding customer lands. Best-effort — never raises.
    Phase r23: also surface MILESTONE flags (cohort hit 5, 10, 25)."""
    try:
        from main import send_admin_alert_email
    except Exception:
        return
    cap = FOUNDING_CAP
    # Milestone trigger thresholds — surface as a banner on the alert
    MILESTONES = {5, 10, 25, 50, 100}
    is_milestone = position in MILESTONES

    milestone_html = ""
    if is_milestone:
        milestone_html = (
            f"<div style='background:linear-gradient(135deg,#6366f1,#a855f7);"
            f"color:#fff;padding:18px 24px;border-radius:10px;"
            f"margin-bottom:18px;text-align:center'>"
            f"<div style='font-size:11px;text-transform:uppercase;"
            f"letter-spacing:.12em;opacity:.85;margin-bottom:6px'>"
            f"COHORT MILESTONE</div>"
            f"<div style='font-size:22px;font-weight:700'>"
            f"{position} of {cap} founding customers</div>"
            f"<div style='font-size:13px;opacity:.85;margin-top:6px'>"
            f"This is a moment. Consider an admin alert / LinkedIn post."
            f"</div></div>"
        )

    subj_prefix = "MILESTONE · " if is_milestone else ""
    subj = f"{subj_prefix}Founding customer #{position} of {cap} — {email}"
    body = (
        milestone_html
        + f"<h2>Founding customer #{position} of {cap} just signed up</h2>"
        + f"<p><b>Email:</b> {email}</p>"
        + f"<p><b>Plan:</b> {plan}</p>"
        + f"<p><b>Stripe:</b> {stripe_customer_id or '(none)'}</p>"
        + f"<p>The first {cap} paying customers matter disproportionately. "
        + f"Reach out personally within the next hour — even a 60-second "
        + f"welcome note converts a buyer into a reference customer.</p>"
        + f"<p>"
        + f"<a href='https://dchub.cloud/api/v1/admin/customer-lookup?"
        + f"email={email}'>Customer record</a> · "
        + f"<a href='https://dashboard.stripe.com/customers/"
        + f"{stripe_customer_id or ''}'>Stripe</a> · "
        + f"<a href='https://dchub.cloud/api/v1/admin/founding-customers'>"
        + f"Cohort</a> · "
        + f"<a href='https://dchub.cloud/founders'>Public page</a>"
        + f"</p>"
    )
    try:
        send_admin_alert_email(subj, body)
    except Exception as e:
        logger.warning(f"[founding-customers] admin alert failed: {e}")


# ── Founding-customer welcome email ─────────────────────────────────
# Sent automatically by the Stripe webhook after auto_tag_if_under_cap
# succeeds. Different tone from the standard Pro welcome — acknowledges
# the founding-cohort status, asks for permission to cite (sets the
# consented_to_cite flag for /founders public page), invites a 15-min
# founder call. Sends via Resend (existing infra).

def _greeting_first_name(email: str) -> str:
    """The name this email opens with. Delegates to the ONE canonical
    implementation (founder_note.first_name_for) — never re-derive it here.

    ★2026-08-28: this used to be
        (email.split("@")[0] or "there").split(".")[0].title()
    which reads the address, not the customer. It greeted founding customer
    #18 as "Hi Mgelshteyn," and tj@karklins.com as "Hi Tj,". On #18 the Stripe
    cardholder name is not the same person as the account localpart, so a
    guessed name can address the WRONG PERSON — a worse failure than a bland
    greeting. The canonical helper reads users.name (set from Stripe at
    provisioning), rejects a value that merely echoes the localpart, and
    fail-softs to 'there'.

    Fail-soft here too: a greeting must never be the reason a founding
    welcome fails to send.
    """
    try:
        from founder_note import first_name_for
        return first_name_for(email)
    except Exception:
        return 'there'


def send_founding_welcome_email(email: str, position: int,
                                  plan: str = "developer") -> bool:
    """Send the founding-customer welcome email. Returns True on
    success, False on any failure (never raises)."""
    # ★★★2026-08-28: PER-PLAN DEDUPE, replacing the generic 24h guard.
    #
    # History. 2026-07-29 this sender was made to honour `_welcome_recently_sent`
    # because opting out of it is what sent alexander@ryex.net FOUR welcomes in
    # three seconds (paid:mint, paid, starter, and this one). That fixed the
    # flood but introduced a RACE: the plain `founding` welcome fires ~1.6s
    # earlier in the SAME webhook, and `_welcome_recently_sent` keys on
    # lower(email) over a 24h window WITHOUT filtering by plan — so this email,
    # the only one carrying cohort position, the founder-call invite and the
    # /cited-by consent link, lost the race roughly half the time.
    #
    # Measured 2026-08-28 over all time: 5 sent, 6 skipped_duplicate, across
    # only 3 distinct customers (tj x4, rob x1, sasa-holdings x1). Which
    # customer loses is pure timing jitter — the "cold buyer sends, upgrader
    # skips" hypothesis was tested against the data and REFUTED (cold buyers and
    # pre-existing users appear on BOTH sides). The loser then waited for the
    # next daily sweep: up to ~41h for the founder-call invite.
    #
    # ★The real invariant is "this specific email exactly once, ever" — not "no
    # welcome in 24h". Dedupe on (email, plan='founding:cohort_welcome') instead.
    # That is strictly stronger than the 24h window for THIS email (it never
    # expires) while leaving the plain welcome free to send alongside it, so the
    # 07-29 flood cannot return through this door.
    #
    # Fail-OPEN on any error, matching the previous contract: a DB blip must
    # never suppress a genuine founding welcome.
    try:
        c_dd = _get_db()
        if c_dd is not None:
            try:
                with c_dd.cursor() as cur_dd:
                    # No bare percent may appear in this string: psycopg2 scans
                    # the whole query for format specs, so LIKE 'sent%' has to
                    # be doubled even though the comment above it is prose.
                    cur_dd.execute(
                        "SELECT 1 FROM welcome_email_log "
                        " WHERE lower(email) = lower(%s) "
                        "   AND plan = 'founding:cohort_welcome' "
                        "   AND COALESCE(status, '') LIKE 'sent%%' LIMIT 1",
                        (email,))
                    already = cur_dd.fetchone() is not None
            finally:
                try: c_dd.close()
                except Exception: pass
            if already:
                logger.info("[founding-customers] cohort welcome SKIPPED for %s "
                            "— already sent once (per-plan dedupe)", email)
                try:
                    from main import _log_welcome_email
                    _log_welcome_email(email, 'founding:cohort_welcome',
                                       'skipped_duplicate')
                except Exception:
                    pass
                return False
    except Exception as _dd_err:
        logger.warning("[founding-customers] dedupe check failed for %s (%s) "
                       "— sending anyway", email, str(_dd_err)[:120])
    resend_key = (os.environ.get("DCHUB_RESEND_API_KEY")
                  or "").strip()
    if not resend_key:
        logger.warning("[founding-customers] no Resend key; welcome "
                       "email skipped")
        return False

    cap = FOUNDING_CAP
    first_name = _greeting_first_name(email)
    consent_link = (f"https://dchub.cloud/api/v1/founding-customers/"
                    f"consent?email={email}")

    subject = f"You're #{position} of {cap} — welcome to DC Hub"
    body_text = f"""Hi {first_name},

Jonathan from DC Hub here. You just landed as founding customer #{position}
of {cap} — which means a lot more than the email signature suggests.

The first {cap} paying customers are the ones who proved this thing was
worth building. You showed up before the case studies, before the
reviews, before the analyst coverage. That carries weight.

Three things, none of which require a reply:

1. Your account is live. The plan you signed up for ({plan}) is active
   and your API key is ready in the dashboard at dchub.cloud/dashboard.

2. If you'd like 15 min on a Zoom this week — me, on you, no script —
   I'd love to hear what you're building and what's missing. Reply
   with a time that works and I'll send a link.

3. If we ever quote you on dchub.cloud/cited-by (with attribution and
   only the words you write back to me — no marketing-speak), would
   that be OK with you? Click here to opt in:
   {consent_link}
   Or just reply "yes" / "no" — I won't ask twice.

Real thanks for the bet.

— Jonathan
   dchub.cloud
   reply directly to this email
"""

    try:
        import urllib.request
        payload = json.dumps({
            "from": "DC Hub <jonathan@dchub.cloud>",
            "to": [email],
            "subject": subject,
            "text": body_text,
            "reply_to": "jonathan@dchub.cloud",
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
                # Cloudflare fronts api.resend.com and 403s urllib's DEFAULT
                # User-Agent (error 1010). A normal UA is REQUIRED or every
                # welcome silently fails. r-sec 2026-06-07.
                "User-Agent": "DCHub-Mailer/1.0 (+https://dchub.cloud)",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        logger.info(f"[founding-customers] welcome sent to {email}")
        # r-delivery-truth (2026-07-17): stamp the send + Resend message id
        # into welcome_email_log so the /webhooks/resend event stream can
        # confirm delivery. Distinct plan label keeps it out of the key-email
        # audits and the _welcome_recently_sent guard scans only 24h anyway.
        try:
            _mid = (json.loads(body) or {}).get("id")
        except Exception:
            _mid = None
        try:
            from main import _log_welcome_email
            _log_welcome_email(email, 'founding:cohort_welcome', 'sent',
                               resend_message_id=_mid)
        except Exception:
            pass
        # Mark in DB
        c = None
        try:
            c = _get_db()
            if c is not None:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE founding_customers SET "
                        "contact_status = 'welcomed', contacted_at = NOW() "
                        "WHERE email = %s",
                        (email,),
                    )
                try: c.commit()
                except Exception: pass
        except Exception:
            note_swallowed_write("founding_customers", where="founding_customers.send_founding_welcome_email")
            pass
        finally:
            if c is not None:
                try: c.close()
                except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"[founding-customers] welcome email failed: {e}")
        return False


# ── Admin send-welcome (for backfilling customers tagged before
#    the auto-email path was wired) ───────────────────────────────────
@founding_customers_bp.route(
    "/api/v1/admin/founding-customers/send-welcome",
    methods=["POST"],
)
def admin_send_welcome():
    """Fire send_founding_welcome_email for an already-tagged customer.

    Used for backfilling Kevin (tagged manually before the auto-email
    path went live) and any future case where a customer needs a
    resend. Idempotent at the customer level — they may get the email
    twice if called twice, so use sparingly."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    p = request.get_json(silent=True) or {}
    email = (p.get("email") or request.args.get("email") or "").lower().strip()
    if not email:
        return jsonify(ok=False, error="email_required"), 400
    _ensure_table()
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT email, plan_at_tag, contact_status
                  FROM founding_customers
                 WHERE email = %s
            """, (email,))
            r = cur.fetchone()
            if not r:
                return jsonify(
                    ok=False,
                    error="not_in_cohort",
                    hint=("Customer isn't tagged as founding. Call "
                           "POST /api/v1/admin/founding-customers/tag "
                           "first."),
                ), 404
            # Position is their rank in the cohort by tagged_at ASC
            cur.execute("""
                SELECT COUNT(*) FROM founding_customers
                 WHERE tagged_at <= (
                    SELECT tagged_at FROM founding_customers WHERE email = %s
                 )
            """, (email,))
            position = int((cur.fetchone() or [1])[0] or 1)
    finally:
        try: c.close()
        except Exception: pass

    sent = send_founding_welcome_email(
        email=email,
        position=position,
        plan=(r[1] or "developer"),
    )
    return jsonify(
        ok=sent, email=email, position=position,
        cap=FOUNDING_CAP,
        previous_status=r[2],
        note=("Welcome email " + ("sent" if sent
                                    else "FAILED — check Resend logs")),
    )


# ── Consent endpoint (CC-BY-style opt-in for /founders public page) ──
@founding_customers_bp.route("/api/v1/founding-customers/consent",
                              methods=["GET", "POST"])
def consent():
    """Public — a founding customer can opt in to be listed on the
    /founders public page. Token-less by design (the link is mailed
    directly to them; visiting it = consent). One-click UX."""
    email = (request.args.get("email") or "").lower().strip()
    if not email or "@" not in email:
        return Response(
            "<p>Invalid link. Reply to the welcome email and we'll fix.</p>",
            mimetype="text/html",
        )
    _ensure_table()
    c = _get_db()
    if c is None:
        return Response("<p>System unavailable. Try again shortly.</p>",
                        mimetype="text/html")
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE founding_customers SET "
                "consented_to_cite = TRUE WHERE email = %s "
                "RETURNING email", (email,),
            )
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        if not row:
            return Response(
                "<p>We don't have your email on file. Probably means "
                "you're not in the founding cohort yet — that's OK, just "
                "reply to Jonathan directly.</p>",
                mimetype="text/html",
            )
        return Response(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Thanks — DC Hub</title>"
            "<style>body{font-family:-apple-system,sans-serif;"
            "background:#0a0a0f;color:#f5f5f7;display:flex;"
            "align-items:center;justify-content:center;min-height:100vh;"
            "margin:0;padding:20px}"
            ".card{max-width:480px;text-align:center;"
            "background:#131319;border:1px solid rgba(255,255,255,.06);"
            "border-radius:14px;padding:40px}"
            "h1{font-size:1.5rem;margin:0 0 12px;"
            "background:linear-gradient(135deg,#6366f1,#a855f7);"
            "-webkit-background-clip:text;background-clip:text;"
            "color:transparent}"
            "p{color:#a1a1aa;line-height:1.5}"
            "a{color:#c7d2fe}</style></head><body>"
            "<div class='card'>"
            "<h1>Thanks — consent recorded</h1>"
            "<p>You're now eligible to appear on "
            "<a href='https://dchub.cloud/founders'>dchub.cloud/founders</a> "
            "once enough of the cohort opts in. We'll only quote the words "
            "you write back to us in email — no marketing-speak.</p>"
            "<p style='margin-top:20px;font-size:.85rem'>"
            "Change your mind? Just reply with 'opt out'.</p>"
            "</div></body></html>",
            mimetype="text/html",
        )
    finally:
        try: c.close()
        except Exception: pass


# ── Opt-out endpoint (revoke /founders consent) ─────────────────────
@founding_customers_bp.route("/api/v1/founding-customers/opt-out",
                              methods=["GET", "POST"])
def opt_out():
    """Public — a founding customer can revoke /founders consent at
    any time. Mirrors /consent. Visiting the link = opt out. Token-less
    by design (the link is mailed directly to them)."""
    email = (request.args.get("email") or "").lower().strip()
    if not email or "@" not in email:
        return Response(
            "<p>Invalid link.</p>", mimetype="text/html",
        )
    _ensure_table()
    c = _get_db()
    if c is None:
        return Response("<p>System unavailable. Try again shortly.</p>",
                        mimetype="text/html")
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE founding_customers SET consented_to_cite = FALSE "
                "WHERE email = %s RETURNING email", (email,),
            )
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        if not row:
            return Response(
                "<p>We don't have your email on file. Nothing to opt out of.</p>",
                mimetype="text/html",
            )
        return Response(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Opted out — DC Hub</title>"
            "<style>body{font-family:-apple-system,sans-serif;"
            "background:#0a0a0f;color:#f5f5f7;display:flex;"
            "align-items:center;justify-content:center;min-height:100vh;"
            "margin:0;padding:20px}"
            ".card{max-width:480px;text-align:center;"
            "background:#131319;border:1px solid rgba(255,255,255,.06);"
            "border-radius:14px;padding:40px}"
            "h1{font-size:1.5rem;margin:0 0 12px;color:#f5f5f7}"
            "p{color:#a1a1aa;line-height:1.5}"
            "a{color:#c7d2fe}</style></head><body>"
            "<div class='card'>"
            "<h1>Opted out</h1>"
            "<p>Your name has been removed from "
            "<a href='https://dchub.cloud/founders'>dchub.cloud/founders</a>. "
            "Your subscription and access are unaffected — we just won't "
            "list you publicly.</p>"
            "<p style='margin-top:20px;font-size:.85rem'>"
            "Changed your mind? Reply to Jonathan and we'll re-enable.</p>"
            "</div></body></html>",
            mimetype="text/html",
        )
    finally:
        try: c.close()
        except Exception: pass


# ── Public /founders page ───────────────────────────────────────────
@founding_customers_bp.route("/founders", methods=["GET"])
def founders_html():
    """Public HTML page listing the consented founding customers as
    social proof. Hides email PII for non-consented rows (just shows
    count). Eyeball-card brand."""
    _ensure_table()
    c = _get_db()
    consented: list = []
    total = 0
    cap = FOUNDING_CAP
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT email, plan_at_tag, tagged_at,
                           consented_to_cite, notes
                      FROM founding_customers
                     ORDER BY tagged_at ASC
                     LIMIT 200
                """)
                for r in cur.fetchall():
                    total += 1
                    if r[3]:  # consented_to_cite
                        consented.append({
                            "email": r[0], "plan": r[1],
                            "tagged_at": r[2],
                        })
        finally:
            try: c.close()
            except Exception: pass

    consented_html = ""
    for i, c_row in enumerate(consented[:50], 1):
        em = c_row.get("email") or ""
        # Show first 2 chars + asterisks + domain — light privacy even
        # when consented (operator can swap to full email if customer
        # explicitly OKs)
        masked = (em[:2] + "***@" + em.split("@", 1)[1]) if "@" in em else em
        plan = (c_row.get("plan") or "").title()
        when = str(c_row.get("tagged_at"))[:10] if c_row.get("tagged_at") else ""
        consented_html += (
            f'<div class="founder">'
            f'<div class="founder-num">#{i:02d}</div>'
            f'<div class="founder-info">'
            f'<div class="founder-email">{masked}</div>'
            f'<div class="founder-meta">{plan} · joined {when}</div>'
            f'</div></div>'
        )

    if not consented_html:
        consented_html = (
            '<div style="padding:32px;text-align:center;'
            'color:#71717a;background:#131319;border:1px dashed '
            'rgba(255,255,255,.06);border-radius:14px">'
            'Cohort is building. Once founding customers opt in to be '
            'cited, they appear here.'
            '</div>'
        )

    return Response(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub · Founding customers</title>
<meta name="description" content="The first {cap} paid customers of DC Hub. The people who showed up before the case studies, the reviews, the analyst coverage.">
<meta property="og:title" content="DC Hub · Founding customers">
<meta property="og:description" content="The {cap}-customer cohort that proved DC Hub.">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script defer src="/js/dchub-brand.js"></script>
<script defer src="/js/dchub-nav.js"></script>
<style>
  :root{{--bg:#0a0a0f;--surface:#131319;--border:rgba(255,255,255,.06);
    --border-strong:rgba(255,255,255,.1);--text:#f5f5f7;
    --text-dim:#a1a1aa;--text-faint:#71717a;--indigo:#6366f1;
    --violet:#a855f7;
    --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
    --font:'Instrument Sans',-apple-system,sans-serif;
    --mono:'JetBrains Mono','SF Mono',monospace;}}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:var(--font);background:var(--bg);color:var(--text);
    line-height:1.55;-webkit-font-smoothing:antialiased;min-height:100vh;
    position:relative}}
  body::before{{content:'';position:fixed;top:-30%;left:50%;
    transform:translateX(-50%);width:1400px;height:1400px;z-index:0;
    pointer-events:none;
    background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
                                rgba(168,85,247,.06) 30%,transparent 60%)}}
  .wrap{{position:relative;z-index:1;max-width:780px;margin:0 auto;
    padding:64px 24px 80px}}
  header.top{{display:flex;align-items:center;justify-content:space-between;
    margin-bottom:36px;flex-wrap:wrap;gap:12px}}
  a.brand{{display:inline-flex;align-items:center;gap:10px;
    text-decoration:none;color:var(--text)}}
  .progress{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
    letter-spacing:.1em;color:var(--text-faint);
    padding:6px 14px;border-radius:999px;
    background:var(--grad-soft);
    border:1px solid rgba(168,85,247,.22)}}
  .eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
    letter-spacing:.16em;color:var(--violet);font-weight:600;margin-bottom:14px}}
  h1{{font-size:clamp(2rem,4.2vw,2.8rem);font-weight:700;
    letter-spacing:-.03em;line-height:1.05;margin-bottom:16px}}
  h1 .grad{{background:var(--grad);-webkit-background-clip:text;
    background-clip:text;color:transparent}}
  .lede{{color:var(--text-dim);font-size:1.02rem;line-height:1.55;
    max-width:640px;margin-bottom:36px}}
  .cohort{{display:flex;flex-direction:column;gap:8px;margin-bottom:48px}}
  .founder{{display:flex;align-items:center;gap:18px;padding:16px 22px;
    background:var(--surface);border:1px solid var(--border);
    border-radius:14px;transition:border-color .2s ease}}
  .founder:hover{{border-color:var(--border-strong)}}
  .founder-num{{font-family:var(--mono);font-size:1.05rem;font-weight:700;
    color:var(--violet);min-width:44px}}
  .founder-info{{flex:1;min-width:0}}
  .founder-email{{font-weight:600;font-size:.95rem;color:var(--text)}}
  .founder-meta{{font-family:var(--mono);font-size:10px;
    text-transform:uppercase;letter-spacing:.08em;color:var(--text-faint);
    margin-top:4px}}
  .cta{{background:var(--grad-soft);border:1px solid rgba(168,85,247,.22);
    border-radius:14px;padding:28px;text-align:center;margin-top:32px}}
  .cta h3{{font-size:1.1rem;font-weight:700;letter-spacing:-.02em;
    margin-bottom:8px}}
  .cta p{{color:var(--text-dim);font-size:.92rem;margin-bottom:18px}}
  .btn{{display:inline-flex;align-items:center;padding:11px 22px;
    background:var(--grad);color:#fff;text-decoration:none;
    border-radius:999px;font-weight:600;font-size:14px;
    transition:transform .15s ease,box-shadow .15s ease}}
  .btn:hover{{transform:translateY(-1px);
    box-shadow:0 8px 24px rgba(168,85,247,.32)}}
  .foot{{font-family:var(--mono);font-size:10.5px;color:var(--text-faint);
    text-align:center;margin-top:48px;letter-spacing:.06em}}
  .foot a{{color:var(--text-dim);margin:0 8px;text-decoration:none}}
  .foot a:hover{{color:var(--text)}}
</style>
</head><body>
<div class="wrap">
  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="progress">{total} of {cap} founding seats taken</span>
  </header>

  <div class="eyebrow">Founding customers</div>
  <h1>The first {cap}. <span class="grad">They showed up early.</span></h1>
  <p class="lede">Before the case studies, before the reviews, before the analyst coverage — these are the operators, investors, and AI agents who paid for DC Hub when it was still proving the value-prop. We don't forget that. Listed here with their permission.</p>

  <div class="cohort">
    {consented_html}
  </div>

  <div class="cta">
    <h3>{cap - total} founding seats still open</h3>
    <p>The first {cap} paid customers become the founding cohort — listed here forever (with permission), with founder-touch onboarding and direct access. Once we hit {cap}, the cohort closes.</p>
    <a href="/pricing" class="btn">See plans</a>
  </div>

  <div class="foot">
    <a href="/">dchub.cloud</a> · <a href="/cited-by">cited by</a> · <a href="/reports/monthly">monthly trend</a> · <a href="/pricing">pricing</a>
  </div>
</div>
</body></html>""",
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=300"})


def _smoke():
    logger.info(f"[founding-customers] ready · cap={FOUNDING_CAP} · "
                 f"POST /tag · GET /api/v1/admin/founding-customers · "
                 f"GET /founders (public) · "
                 f"auto_tag_if_under_cap() importable · "
                 f"send_founding_welcome_email() importable")

_smoke()
