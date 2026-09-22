"""
checkout_click_tracker.py — make relayed Stripe checkout clicks measurable.

2026-08-07. `unlock_more_data` hands the human a DIRECT buy.stripe.com URL
with no dchub hop, so a click on it is structurally unobservable. The admin
waterfall therefore reads:

    219 trial keys -> 142 called -> 26 saw an offer -> ??? -> 0 paid

and the `???` is not measured anywhere. `funnel.stripe_clicked_30d` looks
like it covers this but does NOT: it reads `mcp_pair_codes.stripe_clicked_at`
(the /connect landing flow, which minted 1 code in 30d), a different and
nearly-dead path. So "26 -> 0" cannot currently distinguish

    (a) the agent never relayed the link to a human, from
    (b) a human opened it and declined.

Those imply completely different fixes, which is why the gap matters more
than the zero does.

This adds the missing hop, same shape as the /connect stripe-click proxy
(routes/mcp_connect.py connect_click_proxy) and the /r/<token> attribution
proxy: stamp FIRST, then 302. In-page JS beacons lose the race against a
cross-origin navigation; a server-side redirect cannot.

    GET /go/c/<token>   ->  stamp mcp_checkout_clicks, 302 to Stripe

`/go/*` is already reachable: it is in the frontend `_routes.json` include
AND in the _worker.js backend-proxy prefix list (added 2026-07-11 for
/go/partners). No edge change is needed — the fourth repeat of that class
(/claim/, /r/, /go/, /relay/) is already paid for.

TOKEN FORMAT — mirrors server.mjs buildHumanRelay exactly:

    <base64url(plan|ref)>.<hmac_sha256(DCHUB_INTERNAL_KEY, payload)[:32]>
    <base64url(plan|ref|sid)>.<hmac_sha256(DCHUB_INTERNAL_KEY, payload)[:32]>

`plan` is a KEY of routes._stripe_links.STRIPE_LINKS, never a URL. The
destination is therefore an allowlist lookup and this endpoint cannot be
turned into an open redirect no matter what the token says. `ref` is the
client_reference_id the MCP server already binds (`pk-<sha256>` durable-key
pack, `k-<sha256>` durable-key subscription, `a-<hex>` anonymous offer id,
or a bare mcp_session_id) and is passed through untouched so Fix-E /
r-durable-key attribution is unaffected: the same value reaches Stripe
either way.

`sid` (2026-09-13) is an optional THIRD field: the caller's Mcp-Session-Id,
minted only when a session exists and it is not the ref itself, i.e. beside
a `pk-`/`k-` key ref. It never reaches Stripe. It is stored in
mcp_checkout_clicks.session_id because the operator self-traffic exclusion
keys on session ids and a key-hash ref gives it nothing to test
(routes/handoff_definition.RELAYED_CHECKOUT_SESSION_ID reads it). Two-field
tokens already in the wild verify exactly as before, with sid "".

FAIL-OPEN EVERYWHERE. A human mid-click must never see an error page for a
telemetry blip:
  * DB down            -> log nothing, still 302 to Stripe
  * bad/absent sig     -> stamp ok=false, 302 to /pricing (we cannot know
                          the plan, so /pricing is the honest landing)
  * other field count  -> stamp ok=false, 302 to /pricing (nothing we mint
                          has that shape, so no reading of it is trusted)
  * unknown plan       -> stamp ok=false, 302 to /pricing
And on the MCP side, an unset DCHUB_INTERNAL_KEY makes _goUrl() emit the
DIRECT Stripe link, i.e. exactly today's behaviour — this can degrade to
un-measured, never to un-payable.
"""
from __future__ import annotations

import os
import re
import hmac
import base64
import hashlib
import logging
from contextlib import contextmanager

from flask import Blueprint, request, redirect

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

from routes._stripe_links import STRIPE_LINKS
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)

checkout_click_bp = Blueprint("checkout_click", __name__)

_PRICING_URL = "https://dchub.cloud/pricing"

# Every ref the MCP server mints is `pk-<sha256 hex>`, `k-<sha256 hex>` or a
# bare session id. Anything outside this charset cannot have come from us, so
# it is dropped rather than concatenated into the redirect Location — a signed
# token should not be the ONLY thing standing between a ref and the URL we
# emit. (Belt and braces: the signature already gates this path.)
_REF_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")

# ★ 2026-09-13. True once mcp_checkout_clicks.session_id is CONFIRMED on this
# database by reading the catalog back — never inferred from an ALTER having
# returned, which reports success whether or not it did anything. _log_click
# picks its INSERT from it, so a database the ALTER has not reached keeps
# stamping clicks (without the session) instead of failing every INSERT on an
# unknown column.
_SCHEMA_READY = [False]

_SESSION_ID_COLUMN_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_attribute"
    " WHERE attrelid = to_regclass('mcp_checkout_clicks')"
    " AND attname = 'session_id' AND NOT attisdropped)")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


def _session_id_column(cur) -> bool:
    cur.execute(_SESSION_ID_COLUMN_SQL)
    row = cur.fetchone()
    return bool(row and row[0])


def _ensure_table() -> bool:
    """Create the table and add session_id. True once the column is confirmed.

    ★ 2026-09-13. CREATE TABLE IF NOT EXISTS is a no-op on the live table, so
    session_id needs its own ALTER (routes/human_relay._log_open's token_hash
    is the precedent). Measured on Postgres 18 before writing this: ADD COLUMN
    IF NOT EXISTS waits for ACCESS EXCLUSIVE even when the column already
    exists. So the catalog is asked first, and the ALTER runs only when the
    column is absent, under a short lock_timeout inside a real transaction
    (SET LOCAL does nothing under autocommit). A /go/c INSERT queued behind a
    stalled ALTER is a human waiting on a redirect. Never raises.
    """
    if not (_pg and _dsn()):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mcp_checkout_clicks (
                    id           SERIAL PRIMARY KEY,
                    clicked_at   TIMESTAMPTZ DEFAULT NOW(),
                    plan         TEXT,
                    ref          TEXT,
                    ref_kind     TEXT,
                    sig_ok       BOOLEAN DEFAULT TRUE,
                    ip           TEXT,
                    user_agent   TEXT,
                    referrer     TEXT,
                    session_id   TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_mcc_ts   ON mcp_checkout_clicks(clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_mcc_ref  ON mcp_checkout_clicks(ref, clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_mcc_plan ON mcp_checkout_clicks(plan, clicked_at DESC);
            """)
            if not _session_id_column(cur):
                c.autocommit = False
                try:
                    cur.execute("SET LOCAL lock_timeout = '2s'")
                    cur.execute("ALTER TABLE mcp_checkout_clicks"
                                " ADD COLUMN IF NOT EXISTS session_id TEXT")
                    c.commit()
                except Exception:
                    c.rollback()
                    raise
                finally:
                    c.autocommit = True
            cur.execute("CREATE INDEX IF NOT EXISTS ix_mcc_session"
                        " ON mcp_checkout_clicks(session_id, clicked_at DESC)")
            _SCHEMA_READY[0] = _session_id_column(cur)
    except Exception:
        pass
    return _SCHEMA_READY[0]


_ensure_table()


def ensure_schema() -> bool:
    """True once session_id is confirmed present. Never raises.

    For a reader about to query the column: the handoff funnel calls it once
    per process before counting the relayed-checkout lane. Re-runs the DDL
    only while the column is unconfirmed.
    """
    if not _SCHEMA_READY[0]:
        _ensure_table()
    return _SCHEMA_READY[0]


def _ref_kind(ref: str) -> str:
    """Which identity the checkout is bound to (mirrors the MCP prefixes).

    pk-  = durable key, $10 pack (r-durable-key)   k- = durable key, subscription
    a-   = ephemeral anon attribution id (r-anon-attrib 2026-08-26) — the
           no-key/no-session cohort, i.e. every Smithery/listed-connector caller.
           It identifies the OFFER OCCURRENCE, not a person and not a session:
           enough to join this click to the payment that follows it, and
           deliberately not enough to enter any cohort numerator. Counted on its
           own line (see checkout_clicks_anon) rather than folded into `session`,
           because calling it a session would overstate what we know.
    sid  = bare Mcp-Session-Id (Fix E, keyless callers)
    """
    if ref.startswith("pk-"):
        return "pack_key"
    if ref.startswith("k-"):
        return "sub_key"
    if ref.startswith("a-"):
        return "anon"
    return "session" if ref else "none"


def _log_click(plan: str, ref: str, sid: str, sig_ok: bool) -> None:
    try:
        with _conn() as c, c.cursor() as cur:
            ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
            ua = (request.headers.get("User-Agent", "") or "")[:300]
            rf = (request.headers.get("Referer", "") or "")[:300]
            row = (plan[:40], ref[:200], _ref_kind(ref), bool(sig_ok), ip[:80], ua, rf)
            # ★ 2026-09-13: the column list comes from a CONFIRMED column.
            # Unconfirmed in this process, the catalog is asked again on this
            # connection (a read; DDL never runs on a human's click). Still
            # absent, the click is stamped without its session, not dropped.
            if not _SCHEMA_READY[0]:
                try:
                    _SCHEMA_READY[0] = _session_id_column(cur)
                except Exception:
                    pass
            if _SCHEMA_READY[0]:
                cur.execute(
                    # ON CONFLICT DO NOTHING satisfies the insert-no-on-conflict
                    # lint and is a genuine no-op here: this is an append-only
                    # event log keyed by SERIAL, with no unique constraint for a
                    # row to collide with. It must STAY that way — a human who
                    # clicks the same link twice is two clicks, and deduping them
                    # would re-introduce the undercount this table exists to fix.
                    """INSERT INTO mcp_checkout_clicks
                         (plan, ref, ref_kind, sig_ok, ip, user_agent, referrer,
                          session_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    row + ((sid or "")[:200] or None,),
                )
            else:
                cur.execute(
                    """INSERT INTO mcp_checkout_clicks
                         (plan, ref, ref_kind, sig_ok, ip, user_agent, referrer)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    row,
                )
    except Exception:
        note_swallowed_write("mcp_checkout_clicks",
                             where="checkout_click_tracker._log_click")


def _verify(token: str):
    """(plan, ref, sid, sig_ok). plan is '' unless the signature verified.

    A token whose signature does not check out is NEVER trusted for the
    destination — that is what keeps the allowlist meaningful.

    The payload is `plan|ref` (sid "") or, since 2026-09-13, `plan|ref|sid`.
    Any other field count is unverified even under a good signature.
    """
    secret = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    if not secret or not token or "." not in token:
        return "", "", "", False
    payload, _, sig = token.rpartition(".")
    try:
        expect = hmac.new(secret.encode(), payload.encode(),
                          hashlib.sha256).hexdigest()[:32]
    except Exception:
        return "", "", "", False
    if not hmac.compare_digest(expect, sig):
        return "", "", "", False
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except Exception:
        return "", "", "", False
    fields = raw.split("|")
    if len(fields) not in (2, 3):
        return "", "", "", False
    plan, ref = fields[0], fields[1].strip()
    sid = fields[2].strip() if len(fields) == 3 else ""
    if ref and not _REF_OK.match(ref):
        ref = ""
    # Same charset as ref, for the same reason: it is written to the database,
    # so it must be a shape we mint, never whatever a payload happens to carry.
    if sid and not _REF_OK.match(sid):
        sid = ""
    return plan.strip(), ref, sid, True


def mint_checkout_token(plan: str, ref: str, sid: str = "") -> str | None:
    """A /go/c/ token built the way the MCP server builds one, or None.

    Payload `plan|ref`, or `plan|ref|sid` when a session is present and is not
    the ref itself. None when DCHUB_INTERNAL_KEY is unset, when `plan` is not a
    checkout this endpoint resolves, or when `ref` is outside the charset
    _verify keeps: a link that verifies to something other than what the
    caller asked for is worse than none, because a caller given None falls
    back to a link it knows works. A `sid` outside that charset is dropped,
    exactly as _verify would drop it.
    """
    secret = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    ref = (ref or "").strip()
    sid = (sid or "").strip()
    if not secret or plan not in STRIPE_LINKS or (ref and not _REF_OK.match(ref)):
        return None
    if sid and not _REF_OK.match(sid):
        sid = ""
    raw = plan + "|" + ref + ("|" + sid if sid and sid != ref else "")
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig


_GO_BASE = "https://dchub.cloud/go/c/"


def checkout_url(plan: str, ref: str = "", sid: str = "") -> str:
    """The signed /go/c URL for `plan`, or the pricing page when none can be
    minted (no signing secret, unknown plan). With no ref and no sid the URL is
    caller-independent, so it is safe inside a payload shared by a cache, and
    it is still measured: /go/c stamps mcp_checkout_clicks with the plan."""
    tok = mint_checkout_token(plan, ref, sid)
    return (_GO_BASE + tok) if tok else _PRICING_URL


def _pack_led_ladder(ref: str = "") -> dict:
    """The $10 pack leads (its measured /go/c checkout is upgrade_url), then
    Developer. Both open the REST list they are shown on."""
    out = {"upgrade_url": checkout_url("metered", ref)}
    opts = []
    try:
        from routes.mcp_conversion_plays import PACK10_PRICE_CENTS, PACK10_CREDITS
        if PACK10_PRICE_CENTS and PACK10_CREDITS:
            opts.append({"plan": "pack", "opens": "rest",
                         "label": "$%d one-time = %s API credits; each full answer here uses one" % (
                             int(PACK10_PRICE_CENTS) // 100, format(int(PACK10_CREDITS), ",")),
                         "url": out["upgrade_url"]})
    except Exception:  # noqa: BLE001
        pass
    try:
        import tier_registry as _tr
        price = _tr.price_display("developer")
        if price:
            opts.append({"plan": "developer", "opens": "rest",
                         "label": "%s %s opens this endpoint" % (_tr.label("developer"), price),
                         "url": checkout_url("developer", ref)})
    except Exception:  # noqa: BLE001
        pass
    if opts:
        out["upgrade_options"] = opts
    return out


def rest_wall_ladder(opens_on_rest: str = "pro", mcp_tool: str = "", ref: str = "") -> dict:
    """What a REST free-tier wall hands its caller: only what actually opens it.

    `opens_on_rest` is the plan the endpoint's own require_plan() admits (every
    wall that calls this sits in front of require_plan('pro') today), and
    `upgrade_url` is that plan's measured /go/c checkout. The $10 pack and
    Developer follow in `upgrade_options` marked `opens: "mcp"`, naming the MCP
    tool that returns the full result for them.

    ★ 2026-09-21. The first version (be#5072) put the $10 pack in upgrade_url
    and pack → Developer → Pro in the options. Measured after it shipped: over
    REST neither the pack nor Developer opens these lists, and a Developer key
    is REFUSED by require_plan('pro'). A caller who bought what the wall offered
    got a worse answer than before paying. Offering a rung that cannot open the
    thing it is shown on is the false promise the MCP walls already refuse
    (r62b-conv).

    opens_on_rest="pack" is for a REST list the pack itself opens: a key below
    Developer gets the full answer for one pack credit (util/rest_pack_access.py).
    There the $10 pack leads and Developer follows, both `opens: "rest"`.

    Caller-independent by construction, so it can ride a cached, shared payload,
    unless `ref` is passed. `ref` is for a wall built for ONE request (a 403):
    every /go/c link then carries it as client_reference_id, as the MCP
    server's links do (partner_attribution.offer_ref_for_request). Never pass
    one into a payload that is cached.
    """
    if opens_on_rest == "pack":
        return _pack_led_ladder(ref)
    out = {"upgrade_url": checkout_url(opens_on_rest, ref)}
    opts = []
    try:
        import tier_registry as _tr
        price = _tr.price_display(opens_on_rest)
        if price:
            opts.append({"plan": opens_on_rest, "opens": "rest",
                         "label": "%s %s opens this endpoint" % (_tr.label(opens_on_rest), price),
                         "url": out["upgrade_url"]})
    except Exception:  # noqa: BLE001
        pass
    if mcp_tool and opens_on_rest == "pro":
        via = ("opens the full result through the MCP tool `%s` (https://dchub.cloud/mcp), "
               "not this REST endpoint" % mcp_tool)
        try:
            from routes.mcp_conversion_plays import PACK10_PRICE_CENTS, PACK10_CREDITS
            if PACK10_PRICE_CENTS and PACK10_CREDITS:
                opts.append({"plan": "pack", "opens": "mcp", "mcp_tool": mcp_tool,
                             "label": "$%d one-time = %s API credits; %s" % (
                                 int(PACK10_PRICE_CENTS) // 100,
                                 format(int(PACK10_CREDITS), ","), via),
                             "url": checkout_url("metered", ref)})
        except Exception:  # noqa: BLE001
            pass
        try:
            import tier_registry as _tr
            price = _tr.price_display("developer")
            if price:
                opts.append({"plan": "developer", "opens": "mcp", "mcp_tool": mcp_tool,
                             "label": "%s %s; %s" % (_tr.label("developer"), price, via),
                             "url": checkout_url("developer", ref)})
        except Exception:  # noqa: BLE001
            pass
    if opts:
        out["upgrade_options"] = opts
    return out


@checkout_click_bp.route("/go/c/<token>", methods=["GET"])
def checkout_click(token):
    """Stamp the click, then 302 to the canonical Stripe Payment Link."""
    plan, ref, sid, sig_ok = _verify(token or "")

    target = STRIPE_LINKS.get(plan) if (sig_ok and plan) else None
    if not target:
        # Unverifiable or unknown plan: we cannot name a destination, so land
        # the human on pricing rather than guessing a checkout. Still stamped
        # (sig_ok=False) so a broken/mismatched secret shows up as a spike
        # instead of as silence.
        _log_click(plan or "unknown", ref, sid, False)
        return redirect(_PRICING_URL, code=302)

    # The session never joins the Location: Stripe gets client_reference_id
    # and nothing else, exactly as for a two-field token.
    if ref:
        sep = "&" if "?" in target else "?"
        target = target + sep + "client_reference_id=" + ref

    _log_click(plan, ref, sid, True)
    # ── r-go-click-identify (2026-09-18) ──────────────────────────────────
    # `go_click` has been in relay_identify.SOURCES since that module shipped
    # and nothing ever called it. A keyed caller's ref is pk-/k-<sha256(its
    # api key)>, and that key often already carries an address bound by
    # bind_email / claim_free_key — an identity `identified` structurally
    # cannot see, because it lives on mcp_dev_keys with no session link. The
    # token's HMAC already proved we minted this link for THIS session and
    # THIS key, so the join is a fact about one caller.
    #
    # This is the ONLY path on which a click identifies with no typing and no
    # payment. Measured 30d to 2026-09-18: 26 keyed clicks, 10 with a session,
    # 7 on keys with an address, 2 sessions it would newly stamp.
    #
    # AFTER the click is logged and BEFORE nothing: the redirect below is the
    # human's, and a capture must never stand between them and checkout.
    # capture() never raises; this try is for the import itself.
    if sid:
        try:
            from routes.relay_identify import capture_from_key_ref
            capture_from_key_ref(ref, sid, tool="")
        except Exception as _ie:  # noqa: BLE001
            logger.debug("go-click identify swallowed: %s", str(_ie)[:120])
    return redirect(target, code=302)
