"""routes/checkout_integrity_master_shell.py — Checkout Integrity Master Shell (#47, 2026-08-01).

WHY THIS EXISTS
===============
PR #2106 repointed the founding CTAs at canon and left four findings on the
table that no fence watches. Each is a different way a checkout button can be
wrong, and NONE of them makes the button look broken:

  1 · a link that charges the WRONG AMOUNT — eVq5kE4oOfs13mleGuaZi0h, the
      pre-r-reprice $199 Pro link, is still served as 'pro_monthly' by
      /api/v2/stripe/config while canon Pro is $299. It renders perfectly. It
      just bills $100/mo less than the page beside it advertises.
  2 · a link that DOES NOT EXIST — 7sY5kE8F4fs13mI0PEaZi0c in worker.js is the
      Developer link with a capital I where canon has a lowercase l. The same
      class shipped three live 403 "Get Developer" buttons on
      /developers/signup as buy.stripe.com/dchub-developer (fixed in
      dchub-frontend#1110). A typo'd Stripe URL is indistinguishable from a
      real one until someone clicks it.
  3 · a link that sells the WRONG PLAN — developers.html labels its CTA
      "Upgrade to Pro →" and land-power-map.html "Upgrade to Pro — $99/mo",
      both over the $99 FOUNDING link. Same mislabelling PR #2102 fixed in the
      lifecycle emails: the button sells a different plan than its label.
  4 · a link with NOTHING LEFT TO SELL — founding is a capped program
      (/api/founding-members). When the last licence goes, every founding CTA
      becomes a dead end and nothing takes them down.

The 2026-08-01 founding drift is the proof that a static fence is not enough:
the retired link and canon charged the SAME $99 for the SAME product, so no
price check, no lint and no human review would have flagged it. Only asking
Stripe what a link actually charges, and asking the live page what it actually
claims, can tell those apart.

So this shell verifies the money path end to end — canon, Stripe, and the
served bytes — rather than any one artifact's self-consistency.

LANES
  1 · links resolve       every link on a served surface really loads at Stripe
  2 · charge agreement    Stripe's own amount == tier_registry.price(tier)
  3 · label vs plan       a CTA naming a tier links THAT tier's link
  4 · founding capacity   a capped program still has stock to sell

HOUSE RULES
  · A lane never reads PASS when it could not check. No Stripe key, an
    unreachable page, a 5xx — all render '?', never green-by-silence. The whole
    point is that a wrong link looks exactly like a right one.
  · Read-only. It reads canon, fetches public pages, and asks Stripe to
    DESCRIBE links. It creates nothing, charges nothing, changes no price, and
    flips no flag (L8: never auto-execute).
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s the tick.

Surface:  GET /admin/checkout-integrity            (HTML)
          GET /api/v1/admin/checkout-integrity     (HTML)
          GET|POST /api/v1/admin/checkout-integrity/master-tick   (JSON)
Beat:     checkout-integrity-shell-daily
Kill:     CHECKOUT_INTEGRITY_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

checkout_integrity_master_shell_bp = Blueprint(
    "checkout_integrity_master_shell", __name__)

# The EDGE, not loopback — a buyer reaches these pages through Cloudflare, so
# that is the path that must be verified.
ORIGIN = (os.environ.get("CHECKOUT_INTEGRITY_ORIGIN")
          or "https://dchub.cloud").rstrip("/")

# ★ urllib without a UA gets CF-403'd on this zone. Always send one.
_UA = "dchub-checkout-integrity/1.0 (+https://dchub.cloud; internal-audit)"

# Public surfaces that carry a checkout CTA. Paths, not files — the repo copy
# is not what a buyer sees (dchub-backend/dchub-frontend/ is a non-deploying
# mirror; the live frontend is the standalone repo).
_SURFACES = (
    "/pricing",
    "/developers",
    "/developers/signup/",
    "/land-power-map",
    "/app/",
    "/upgrade/",
    "/landing/",
)

# The JSON surface that publishes payment links to the frontend and to agents,
# unauthenticated. This is the one that served the retired founding link.
_CONFIG_SURFACE = "/api/v2/stripe/config"

_FOUNDING_COUNTER = "/api/founding-members"

# A payment-link id is opaque alphanumeric. Hyphens are captured DELIBERATELY:
# stopping the match at the hyphen is exactly how buy.stripe.com/dchub-developer
# read as the innocuous id "dchub" and shipped three live 403s.
_LINK_RE = re.compile(r"buy\.stripe\.com/([A-Za-z0-9-]+)")

# An anchor wrapping a checkout href — the label is captured for lane 3.
# (Deliberately no example URL here: tests/test_stripe_link_canonical.py scans
# the repo for checkout links and a placeholder one reads as a real stray.)
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*href=[\"']https://buy\.stripe\.com/([A-Za-z0-9-]+)[\"'][^>]*>(.*?)</a>",
    re.I | re.S)

# Tier words that may appear in a CTA label, mapped to the canonical key whose
# link that label promises. Order matters: check longer/more specific first so
# "founding member" does not also match a bare "member".
_LABEL_TIERS = (
    ("founding", "founding"),
    ("starter", "starter"),
    ("developer", "developer"),
    ("enterprise", "enterprise"),
    ("team", "team"),
    ("pro", "pro"),
)

_MONEY_RE = re.compile(r"\$\s?(\d[\d,]*)\s*/\s*(?:mo|month)\b", re.I)


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("CHECKOUT_INTEGRITY_SHELL_DISABLE") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, renders '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """PASS only when every critical check affirmatively passed. An
    indeterminate critical check yields '?' — never green-by-silence."""
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in crits):
        return "?"
    return "PASS"


def _canon():
    """{tier: url} from THE source of truth. None if unreadable — with no canon
    nothing below can be judged, and saying so beats rendering a green board."""
    try:
        from routes._stripe_links import STRIPE_LINKS
        return dict(STRIPE_LINKS)
    except Exception as e:  # noqa: BLE001
        logger.warning("[checkout-integrity] canon import failed: %s", e)
        return None


def _canon_by_id(canon: dict) -> dict:
    return {url.rsplit("/", 1)[-1]: tier for tier, url in canon.items()}


def _fetch(path: str):
    """GET a live surface. Returns (body, error). Never raises.

    requests, not urllib (regression_lint urllib-request-on-railway).
    A non-2xx is an ERROR, not a body: a 403/404 means we could NOT check,
    which must render '?' — never a PASS on an error page's contents.
    """
    try:
        import requests as _rq
        r = _rq.get(ORIGIN + path, headers={"User-Agent": _UA}, timeout=12)
        if r.status_code >= 400:
            return None, "HTTP %d" % r.status_code
        return r.text, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


def _link_status(link_id: str):
    """HEAD/GET a Stripe payment link. Returns (status_code, error).

    A retired or typo'd link 404s/403s here while looking identical in source —
    which is the only way to tell 7sY5kE8F4fs13mI0PEaZi0c from
    7sY5kE8F4fs13ml0PEaZi0c without reading it character by character.
    """
    try:
        import requests as _rq
        r = _rq.get("https://buy.stripe.com/" + link_id,
                    headers={"User-Agent": _UA}, timeout=12,
                    allow_redirects=True)
        return r.status_code, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


def _stripe_amounts():
    """{link_id: (amount_dollars, interval, nickname)} straight from Stripe.

    Returns (mapping, error). READ-ONLY: lists payment links and their line
    items. The URL short code is NOT the plink_ id, so links are matched on
    their `url` field.
    """
    key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        return None, "STRIPE_SECRET_KEY not set"
    try:
        import stripe as _stripe
    except Exception as e:  # noqa: BLE001
        return None, "stripe lib unavailable: %s" % type(e).__name__
    try:
        _stripe.api_key = key
        out = {}
        links = _stripe.PaymentLink.list(limit=100)
        for pl in getattr(links, "auto_paging_iter", lambda: links.data)():
            url = getattr(pl, "url", "") or ""
            if "buy.stripe.com/" not in url:
                continue
            link_id = url.rsplit("/", 1)[-1]
            try:
                items = _stripe.PaymentLink.list_line_items(pl.id, limit=5)
                data = getattr(items, "data", []) or []
                if not data:
                    continue
                price = getattr(data[0], "price", None) or {}
                amount = price.get("unit_amount")
                recurring = price.get("recurring") or {}
                interval = recurring.get("interval") or "one_time"
                nickname = price.get("nickname") or price.get("lookup_key") or ""
                if amount is not None:
                    out[link_id] = (amount / 100.0, interval, nickname)
            except Exception as e:  # noqa: BLE001 — one bad link must not kill the lane
                logger.debug("[checkout-integrity] line items for %s: %s", pl.id, e)
        return out, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


def _served_links():
    """{link_id: [surface, ...]} across every live surface. Plus the surfaces
    that could not be read, so a lane can refuse to pass on partial data."""
    found, unreachable = {}, []
    for path in _SURFACES:
        body, err = _fetch(path)
        if err:
            unreachable.append("%s (%s)" % (path, err))
            continue
        for link_id in set(_LINK_RE.findall(body)):
            found.setdefault(link_id, []).append(path)

    body, err = _fetch(_CONFIG_SURFACE)
    if err:
        unreachable.append("%s (%s)" % (_CONFIG_SURFACE, err))
    else:
        for link_id in set(_LINK_RE.findall(body)):
            found.setdefault(link_id, []).append(_CONFIG_SURFACE)
    return found, unreachable


# ── lanes ─────────────────────────────────────────────────────────────

def _lane_links_resolve(canon: dict) -> list[dict]:
    """FINDING 2 — a link that does not exist. Ask Stripe to load each one."""
    checks = []
    served, unreachable = _served_links()

    if unreachable:
        # Any unreadable surface means the sweep below is partial, so the lane
        # cannot claim PASS — it did not see everything it is asked to judge.
        checks.append(_check(
            "surfaces_read", "every checkout surface was readable", None,
            "could not read: " + "; ".join(unreachable[:6]),
            critical=True))
    else:
        checks.append(_check("surfaces_read", "every checkout surface was readable",
                             True, "%d surfaces read" % (len(_SURFACES) + 1)))

    # Canonical links first — these are what everything is supposed to use.
    for tier, url in sorted(canon.items()):
        link_id = url.rsplit("/", 1)[-1]
        code, err = _link_status(link_id)
        if err or code is None:
            checks.append(_check("canon_%s" % tier, "canon '%s' link loads" % tier,
                                 None, "could not reach Stripe: %s" % err,
                                 critical=True))
        else:
            checks.append(_check(
                "canon_%s" % tier, "canon '%s' link loads" % tier,
                code < 400, "HTTP %d — %s" % (code, link_id), critical=True))

    # Then anything SERVED that canon does not know: an unmanaged link nobody
    # can price, and the most likely place for a typo to hide.
    by_id = _canon_by_id(canon)
    for link_id, paths in sorted(served.items()):
        if link_id in by_id:
            continue
        code, err = _link_status(link_id)
        where = ", ".join(paths[:3])
        if err or code is None:
            checks.append(_check("served_%s" % link_id,
                                 "unmanaged link %s loads" % link_id, None,
                                 "on %s — could not reach Stripe: %s" % (where, err),
                                 critical=True))
        else:
            checks.append(_check(
                "served_%s" % link_id, "unmanaged link %s loads" % link_id,
                code < 400,
                "HTTP %d — served on %s; not in canon, so nobody can price it"
                % (code, where), critical=True))
    return checks


def _lane_charge_agreement(canon: dict) -> list[dict]:
    """FINDING 1 — a link that charges the wrong amount.

    The 2026-08-01 incident is why this lane asks STRIPE and not the repo: the
    retired founding link and canon charged the same $99 for the same product,
    so every repo-side check agreed with itself while the webhook could not
    attribute the sale.
    """
    checks = []
    amounts, err = _stripe_amounts()
    if amounts is None:
        return [_check("stripe_readable", "Stripe describes its payment links",
                       None, "cannot ask Stripe: %s" % err, critical=True)]
    checks.append(_check("stripe_readable", "Stripe describes its payment links",
                         True, "%d links described" % len(amounts)))

    try:
        import tier_registry
    except Exception as e:  # noqa: BLE001
        return checks + [_check("registry", "tier_registry importable", None,
                                "%s" % type(e).__name__, critical=True)]

    for tier, url in sorted(canon.items()):
        link_id = url.rsplit("/", 1)[-1]
        got = amounts.get(link_id)
        expected = tier_registry.price(tier)
        if got is None:
            checks.append(_check(
                "amount_%s" % tier, "Stripe knows canon '%s'" % tier, None,
                "link %s not in this account's payment links" % link_id,
                critical=True))
            continue
        charged, interval, nickname = got
        # price() returns 0 for tiers with no published monthly figure
        # (enterprise/custom, annual SKUs) — those are not judged here.
        if not expected:
            checks.append(_check(
                "amount_%s" % tier, "canon '%s' has a published price" % tier,
                True, "charges $%s/%s%s — registry publishes no monthly figure, "
                      "nothing to compare" % (charged, interval,
                                              " (%s)" % nickname if nickname else "")))
            continue
        if interval != "month":
            checks.append(_check(
                "amount_%s" % tier, "canon '%s' bills monthly" % tier, True,
                "charges $%s per %s — not a monthly SKU, amount not compared"
                % (charged, interval)))
            continue
        ok = abs(charged - float(expected)) < 0.01
        checks.append(_check(
            "amount_%s" % tier, "canon '%s' charges its published price" % tier,
            ok, "Stripe charges $%s/mo · tier_registry publishes $%s/mo%s"
                % (charged, expected, "" if ok else "  ← MISMATCH"),
            critical=True))
    return checks


def _lane_label_vs_plan(canon: dict) -> list[dict]:
    """FINDING 3 — a CTA that sells a different plan than its label."""
    checks = []
    by_id = _canon_by_id(canon)
    examined = 0
    unreadable = []

    for path in _SURFACES:
        body, err = _fetch(path)
        if err:
            unreadable.append("%s (%s)" % (path, err))
            continue
        for link_id, raw_label in _ANCHOR_RE.findall(body):
            label = re.sub(r"<[^>]+>", " ", raw_label)
            label = re.sub(r"\s+", " ", label).strip()
            if not label:
                continue
            examined += 1
            sells = by_id.get(link_id)
            low = label.lower()
            named = next((t for word, t in _LABEL_TIERS if word in low), None)

            if named and not sells:
                # The label promises a tier but the href is a link canon cannot
                # identify, so we CANNOT say which plan it sells. Never PASS
                # here: this is the exact shape of the live 08-01 drift —
                # "Upgrade to Pro" over the retired founding link — and reading
                # it green because the link is unrecognised would be the
                # green-by-silence this shell exists to end. Lane 1 fails the
                # link separately; this lane records that the CLAIM is unverifiable.
                checks.append(_check(
                    "unknown_%s_%s" % (path.strip("/").replace("/", "_") or "root",
                                       link_id),
                    "CTA on %s links a plan canon can name" % path, None,
                    "%r promises '%s' but links %s, which is not in canon"
                    % (label[:60], named, link_id), critical=True))
                continue

            if named and sells and named != sells:
                # 'founding' is pro-equivalent for ACCESS, but it is a distinct
                # SKU at a distinct price, so a Pro label over it still sells
                # something the buyer did not choose.
                checks.append(_check(
                    "label_%s_%s" % (path.strip("/").replace("/", "_") or "root", link_id),
                    "CTA on %s sells what it says" % path, False,
                    "%r links the '%s' plan, not '%s'" % (label[:60], sells, named),
                    critical=True))
                continue

            money = _MONEY_RE.search(label)
            if money and sells:
                try:
                    import tier_registry
                    quoted = float(money.group(1).replace(",", ""))
                    real = float(tier_registry.price(sells) or 0)
                    if real and abs(quoted - real) >= 0.01:
                        checks.append(_check(
                            "price_%s_%s" % (path.strip("/").replace("/", "_") or "root",
                                             link_id),
                            "CTA on %s quotes its plan's price" % path, False,
                            "%r quotes $%s but the '%s' plan is $%s"
                            % (label[:60], quoted, sells, real), critical=True))
                        continue
                except Exception:  # noqa: BLE001
                    pass

    if unreadable:
        checks.append(_check("labels_readable", "every surface was readable", None,
                             "could not read: " + "; ".join(unreadable[:6]),
                             critical=True))
    if examined == 0:
        checks.append(_check("labels_found", "found CTAs to examine", None,
                             "no anchored checkout CTA parsed — this lane would "
                             "prove nothing", critical=True))
    elif not any(c["pass"] is not True for c in checks):
        # Only claim a clean sweep when NOTHING was unresolved. The first draft
        # tested `is False`, so a lane full of indeterminate checks still
        # appended this green summary — a green line on top of "we could not
        # tell" is precisely the failure mode being fenced.
        checks.append(_check("labels_agree", "every labelled CTA sells its plan",
                             True, "%d CTA(s) examined, all consistent" % examined))
    return checks


def _lane_founding_capacity(canon: dict) -> list[dict]:
    """FINDING 4 — a CTA with nothing left to sell."""
    body, err = _fetch(_FOUNDING_COUNTER)
    if err:
        return [_check("counter", "founding counter readable", None,
                       "%s unreachable: %s" % (_FOUNDING_COUNTER, err),
                       critical=True)]
    try:
        data = json.loads(body)
    except Exception as e:  # noqa: BLE001
        return [_check("counter", "founding counter is JSON", None,
                       "unparseable: %s" % type(e).__name__, critical=True)]

    active = bool(data.get("program_active"))
    remaining = data.get("remaining")
    checks = [_check("counter", "founding counter readable", True,
                     "active=%s remaining=%s claimed=%s price=%s"
                     % (active, remaining, data.get("claimed"), data.get("price")))]

    founding_url = canon.get("founding") or ""
    founding_id = founding_url.rsplit("/", 1)[-1] if founding_url else None
    if not founding_id:
        return checks + [_check("canon_founding", "canon has a founding link",
                                None, "no 'founding' key in canon", critical=True)]

    served, unreachable = _served_links()
    offering = served.get(founding_id, [])
    open_for_sale = active and (remaining is None or remaining > 0)

    if open_for_sale:
        checks.append(_check(
            "stock", "founding CTAs have stock to sell", True,
            "program open (remaining=%s) · offered on %d surface(s)"
            % (remaining, len(offering))))
        if isinstance(remaining, int) and remaining <= 1:
            # Not a failure — a heads-up that the next sale closes the program
            # and turns every one of these CTAs into a dead end.
            # critical=True so the lane reads '?' rather than plain green: the
            # program is healthy RIGHT NOW and one sale from not being.
            checks.append(_check(
                "stock_warning", "founding stock is not about to run out",
                None, "only %s licence(s) left — the next sale makes all %d "
                      "founding CTA(s) dead ends and nothing takes them down"
                      % (remaining, len(offering)), critical=True))
    elif offering:
        checks.append(_check(
            "stock", "no founding CTA outlives the program", False,
            "program CLOSED (active=%s remaining=%s) but the founding link is "
            "still offered on: %s" % (active, remaining, ", ".join(offering)),
            critical=True))
    else:
        checks.append(_check("stock", "no founding CTA outlives the program",
                             True, "program closed and no surface offers it"))

    if unreachable:
        checks.append(_check("capacity_coverage", "every surface was readable",
                             None, "could not read: " + "; ".join(unreachable[:6]),
                             critical=True))
    return checks


# ── ledger ────────────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    """Best-effort beat into the SHIPPED ingest_runs ledger. NEVER raises."""
    try:
        body = json.dumps({
            "feed": "checkout-integrity-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint urllib-request-on-railway)
        _rq.post("http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-checkout-integrity-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[checkout-integrity] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn, *a) -> list[dict]:
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s" % (type(e).__name__, str(e)[:120]),
                       critical=True)]


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    canon = _canon()
    if not canon:
        lanes = [{"id": "canon", "name": "0 · canon available",
                  "checks": [_check("canon_missing",
                                    "routes/_stripe_links.STRIPE_LINKS", None,
                                    "canon unavailable — no lane can be judged",
                                    critical=True)]}]
    else:
        lanes = [
            {"id": "links_resolve", "name": "1 · links resolve (does it exist?)",
             "checks": _safe_lane(_lane_links_resolve, canon)},
            {"id": "charge_agreement",
             "name": "2 · charge agreement (does it bill the published price?)",
             "checks": _safe_lane(_lane_charge_agreement, canon)},
            {"id": "label_vs_plan",
             "name": "3 · label vs plan (does it sell what it says?)",
             "checks": _safe_lane(_lane_label_vs_plan, canon)},
            {"id": "founding_capacity",
             "name": "4 · founding capacity (is there stock to sell?)",
             "checks": _safe_lane(_lane_founding_capacity, canon)},
        ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "checkout-integrity-47",
        "origin": ORIGIN,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


# Admin GETs are cached at the edge on this zone — a board that renders a stale
# tick is worse than no board, because it reports a money surface.
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0",
             "Surrogate-Control": "no-store", "Pragma": "no-cache"}


@checkout_integrity_master_shell_bp.route(
    "/api/v1/admin/checkout-integrity/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="CHECKOUT_INTEGRITY_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_run_tick(beat=(request.method == "POST"))), 200, _NO_STORE


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@checkout_integrity_master_shell_bp.route("/admin/checkout-integrity", methods=["GET"])
@checkout_integrity_master_shell_bp.route("/api/v1/admin/checkout-integrity",
                                          methods=["GET"])
def dashboard():
    if _disabled():
        return ("<h1>Checkout Integrity</h1>"
                "<p>CHECKOUT_INTEGRITY_SHELL_DISABLE=1</p>"), 404
    if not _admin_ok():
        return "<h1>401</h1><p>admin key required</p>", 401
    t = _run_tick(beat=False)
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in t["lanes"]:
        rows.append("<tr><td><b>%s</b></td><td style='color:%s'><b>%s</b></td>"
                    "<td>%s</td></tr>"
                    % (_esc(ln["name"]), color.get(ln["verdict"], "#eab308"),
                       _esc(ln["verdict"]),
                       "<br>".join(
                           "%s <i>%s</i> — %s"
                           % ({True: "✓", False: "✗"}.get(k["pass"], "?"),
                              _esc(k["name"]), _esc(k["detail"]))
                           for k in ln["checks"])))
    return ("<html><head><title>Checkout Integrity #47</title>"
            "<meta http-equiv='refresh' content='120'></head>"
            "<body style='font-family:system-ui;max-width:1100px;margin:24px auto'>"
            "<h1>Checkout Integrity <small>#47</small></h1>"
            "<p>origin <code>%s</code> · %s</p>"
            "<p><small>Four ways a checkout button is wrong while looking right: "
            "it does not exist, it bills the wrong amount, it sells a different "
            "plan than its label, or it has nothing left to sell. Read-only — "
            "it asks Stripe to DESCRIBE links, never to create or charge. "
            "A lane never reads PASS when it could not check.</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
            "<th align='left'>checks</th></tr>%s</table>"
            "<p><small>refreshes 120s · kill CHECKOUT_INTEGRITY_SHELL_DISABLE=1"
            "</small></p></body></html>"
            % (_esc(t["origin"]), _esc(t["generated_at"]), "".join(rows))
            ), 200, _NO_STORE
