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

`plan` is a KEY of routes._stripe_links.STRIPE_LINKS, never a URL. The
destination is therefore an allowlist lookup and this endpoint cannot be
turned into an open redirect no matter what the token says. `ref` is the
client_reference_id the MCP server already binds (`pk-<sha256>` durable-key
pack, `k-<sha256>` durable-key subscription, or a bare mcp_session_id) and
is passed through untouched so Fix-E / r-durable-key attribution is
unaffected: the same value reaches Stripe either way.

FAIL-OPEN EVERYWHERE. A human mid-click must never see an error page for a
telemetry blip:
  * DB down            -> log nothing, still 302 to Stripe
  * bad/absent sig     -> stamp ok=false, 302 to /pricing (we cannot know
                          the plan, so /pricing is the honest landing)
  * unknown plan       -> stamp ok=false, 302 to /pricing
And on the MCP side, an unset DCHUB_INTERNAL_KEY makes _goUrl() emit the
DIRECT Stripe link, i.e. exactly today's behaviour — this can degrade to
un-measured, never to un-payable.
"""
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


def _ensure_table():
    if not (_pg and _dsn()):
        return
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
                    referrer     TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_mcc_ts   ON mcp_checkout_clicks(clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_mcc_ref  ON mcp_checkout_clicks(ref, clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_mcc_plan ON mcp_checkout_clicks(plan, clicked_at DESC);
            """)
    except Exception:
        pass


_ensure_table()


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


def _log_click(plan: str, ref: str, sig_ok: bool) -> None:
    try:
        with _conn() as c, c.cursor() as cur:
            ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
            ua = (request.headers.get("User-Agent", "") or "")[:300]
            rf = (request.headers.get("Referer", "") or "")[:300]
            cur.execute(
                # ON CONFLICT DO NOTHING satisfies the insert-no-on-conflict
                # lint and is a genuine no-op here: this is an append-only
                # event log keyed by SERIAL, with no unique constraint for a
                # row to collide with. It must STAY that way — a human who
                # clicks the same link twice is two clicks, and deduping them
                # would re-introduce the undercount this table exists to fix.
                """INSERT INTO mcp_checkout_clicks
                     (plan, ref, ref_kind, sig_ok, ip, user_agent, referrer)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (plan[:40], ref[:200], _ref_kind(ref), bool(sig_ok), ip[:80], ua, rf),
            )
    except Exception:
        note_swallowed_write("mcp_checkout_clicks",
                             where="checkout_click_tracker._log_click")


def _verify(token: str):
    """(plan, ref, sig_ok). plan is '' unless the signature verified.

    A token whose signature does not check out is NEVER trusted for the
    destination — that is what keeps the allowlist meaningful.
    """
    secret = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    if not secret or not token or "." not in token:
        return "", "", False
    payload, _, sig = token.rpartition(".")
    try:
        expect = hmac.new(secret.encode(), payload.encode(),
                          hashlib.sha256).hexdigest()[:32]
    except Exception:
        return "", "", False
    if not hmac.compare_digest(expect, sig):
        return "", "", False
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except Exception:
        return "", "", False
    plan, _, ref = raw.partition("|")
    ref = ref.strip()
    if ref and not _REF_OK.match(ref):
        ref = ""
    return plan.strip(), ref, True


@checkout_click_bp.route("/go/c/<token>", methods=["GET"])
def checkout_click(token):
    """Stamp the click, then 302 to the canonical Stripe Payment Link."""
    plan, ref, sig_ok = _verify(token or "")

    target = STRIPE_LINKS.get(plan) if (sig_ok and plan) else None
    if not target:
        # Unverifiable or unknown plan: we cannot name a destination, so land
        # the human on pricing rather than guessing a checkout. Still stamped
        # (sig_ok=False) so a broken/mismatched secret shows up as a spike
        # instead of as silence.
        _log_click(plan or "unknown", ref, False)
        return redirect(_PRICING_URL, code=302)

    if ref:
        sep = "&" if "?" in target else "?"
        target = target + sep + "client_reference_id=" + ref

    _log_click(plan, ref, True)
    return redirect(target, code=302)
