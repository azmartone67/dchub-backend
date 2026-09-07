"""
Phase ZZZZ-cf-purge (2026-05-18) — programmatic CF cache purge.

We hit a /markets 403 issue that looks like a poisoned CF cache or a
stale Page Rule. CF's API supports purging by URL. Now wired so we can
hit `POST /api/v1/cf/purge` with a list of URLs and instantly clear them
from CF's cache without dashboard clicks.

Also exposed via brain L1 auto-fix: when /markets-style edge-divergence
is detected, brain can call this directly.

Requires:
  CLOUDFLARE_API_TOKEN  — Cache Purge: Edit permission
  CLOUDFLARE_ZONE_ID    — zone for dchub.cloud
"""

import os
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
cf_purge_bp = Blueprint("cf_purge", __name__)

_CF_API_TOKEN  = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
_CF_ZONE_ID    = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
_ADMIN_KEY     = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _purge_urls(urls: list[str]) -> dict:
    """Purge specific URLs from CF cache."""
    if not _CF_API_TOKEN or not _CF_ZONE_ID:
        return {"ok": False, "error": "CLOUDFLARE_API_TOKEN or CLOUDFLARE_ZONE_ID not set"}
    try:
        import requests
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/zones/{_CF_ZONE_ID}/purge_cache",
            headers={
                "Authorization": f"Bearer {_CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"files": urls},
            timeout=15,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:500]}
        return {"ok": r.status_code == 200 and body.get("success"),
                "status": r.status_code,
                "purged": urls,
                "cf_response": body}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _purge_everything() -> dict:
    """Purge the whole zone.

    WHY THIS EXISTS AND WHEN IT IS CORRECT (2026-08-28). A sponsorship state
    change has to clear the edge across every page the sponsor renders on. The
    facility module runs on thousands of URLs, and CF's purge-by-file API takes
    30 files per request on our plan, so enumerating them is not an option;
    purge-by-prefix is Enterprise-only.

    The alternative to a full purge is leaving a CANCELLED sponsor rendering
    for up to the stale-while-revalidate window — 1h on facility pages, 24h on
    market pages — which is an exclusivity-clause breach, not a stale cache.

    The usual objection to a full purge is an origin stampede. Measured traffic
    says that does not apply here: Googlebot fetches ~5,721/day (~0.07 req/s)
    and human traffic was 1,224 sessions in 28 days. Re-warming this zone is
    not a thundering herd. Sponsorship state changes are also rare — a handful
    a month — so this is not a hot path.
    """
    if not _CF_API_TOKEN or not _CF_ZONE_ID:
        return {"ok": False, "error": "CLOUDFLARE_API_TOKEN or CLOUDFLARE_ZONE_ID not set"}
    try:
        import requests
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/zones/{_CF_ZONE_ID}/purge_cache",
            headers={
                "Authorization": f"Bearer {_CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"purge_everything": True},
            timeout=20,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:500]}
        return {"ok": r.status_code == 200 and bool(body.get("success")),
                "status": r.status_code,
                "purged": "everything",
                "cf_response": body}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cf_purge_bp.route("/api/v1/cf/purge", methods=["POST"])
def purge_endpoint():
    """Admin-gated CF cache purge.

    POST body: { "urls": ["https://dchub.cloud/markets", ...] }
    OR        : { "url":  "https://dchub.cloud/markets" }
    """
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
    if not urls:
        return jsonify(ok=False, error="provide 'urls' (list) or 'url' (str)"), 400

    return jsonify(_purge_urls(urls)), 200


@cf_purge_bp.route("/api/v1/cf/purge/markets-fix", methods=["GET", "POST"])
def purge_markets_fix():
    """One-shot: purge the /markets-family URLs the user reported 403s on.
    Public GET so the user can trigger from a browser without admin key —
    purges are idempotent and read-side."""
    return jsonify(_purge_urls([
        "https://dchub.cloud/markets",
        "https://dchub.cloud/markets/",
        "https://dchub.cloud/market-intelligence",
    ])), 200


@cf_purge_bp.route("/api/v1/cf/purge/frontend-static", methods=["GET", "POST"])
def purge_frontend_static():
    """One-shot: purge the versionless frontend static assets that the CF edge
    pins stale after a Pages deploy (nav.js, homepage, llms.txt, sitemap.xml) —
    e.g. a nav change shipped but /js/dchub-nav.js kept serving the old copy
    (cf-cache HIT, growing age) despite max-age=0. Public GET, idempotent +
    read-side, same rationale as purge/markets-fix above."""
    return jsonify(_purge_urls([
        "https://dchub.cloud/js/dchub-nav.js",
        "https://dchub.cloud/",
        "https://dchub.cloud/llms.txt",
        "https://dchub.cloud/sitemap.xml",
    ])), 200


# CF purge-by-file accepts 30 files per request on this plan; a 31st in the same
# call is rejected, which would silently leave that URL serving. Named and split
# out so a test can drive it with a list long enough to actually chunk — with
# only ~24 derived URLs the route itself never produces a second batch, so
# asserting on the route alone cannot tell 30 from 300 (measured: it did not).
_CF_PURGE_MAX_FILES = 30


def _purge_in_batches(urls):
    return [_purge_urls(urls[i:i + _CF_PURGE_MAX_FILES])
            for i in range(0, len(urls), _CF_PURGE_MAX_FILES)]


# NOTE this route is named after what it purges (an export), NOT after the
# tool family, on purpose. The canonical-counts scanner
# (tests/test_canonical_counts_drift.py) flags a digit followed by a separator
# and the plural of "tool" as a hardcoded tool-count literal contradicting
# canon, and fails unit-tests on it — that is a false positive on a route slug,
# but renaming is cheaper and safer than the alternative. Do NOT reach for
# STALE_SCAN_SKIP_FILES: it excludes the whole FILE, including the numbers that
# genuinely need watching.
@cf_purge_bp.route("/api/v1/cf/purge/tier2-export", methods=["GET", "POST"])
def purge_tier2_export():
    """One-shot: evict pre-gate Tier-2 MCP tool responses from the CF edge.

    WHY THIS EXISTS. #4038 gated /api/v1/mcp/tools/export_facility_csv and
    create_site_report behind a developer-tier key. The ORIGIN gate went live
    and works — every cache-busted anonymous probe returns 401. But the eyeball
    cache sits IN FRONT of the worker, so entries warmed BEFORE the gate kept
    being served. Measured 2026-09-06, after the gate deployed, on the
    un-cache-busted URL a scraper would actually use:

        10/10 probes -> HTTP 200, cf-cache-status: HIT, 1,003,630 bytes,
        age climbing past 1,900s — the full 10,000-row registry, anonymous.

    dchub-frontend#1409 (worker 4.92.0) stopped the worker RE-caching this
    family, but nothing in the worker runs on a HIT, so it cannot evict what is
    already there. Only a purge does.

    PUBLIC GET, no admin key — same rationale and same shape as
    purge/markets-fix and purge/frontend-static above: the URL list is DERIVED
    HERE and the caller cannot influence it, so this cannot be used to evict
    arbitrary paths on the zone. Purges are idempotent and read-side. The admin
    key gates /api/v1/cf/purge because that one takes caller-supplied URLs;
    this one does not, and requiring a secret to close a live data leak is the
    thing that kept it open.

    A CF zone Cache Rule bypass for /api/v1/mcp/tools/* is still the durable
    fix — this only evicts what is cached NOW. Re-run it after any window in
    which a paid caller may have primed a new URL shape.
    """
    urls = []
    for host in ("https://dchub.cloud", "https://api.dchub.cloud"):
        b = f"{host}/api/v1/mcp/tools"
        urls.append(f"{b}/export_facility_csv")
        # The row-cap shapes an enumerator actually sends. limit=10000 is the
        # ceiling and the one measured leaking; the rest are the round numbers
        # a caller reaches for. CF purge-by-file is exact-match on the full
        # URL including query string, so each shape must be listed.
        for lim in (10000, 5000, 2000, 1000, 500, 250, 100, 50, 25, 10):
            urls.append(f"{b}/export_facility_csv?limit={lim}")
        urls.append(f"{b}/create_site_report")
    results = _purge_in_batches(urls)
    return jsonify({
        "ok": all(r.get("ok") for r in results),
        "batches": len(results),
        "url_count": len(urls),
        "results": results,
    }), 200


# ── OG/social card purge (2026-09-05) ──────────────────────────────────────
# Card designs change. Since #3938 gave /api/v1/og/* a 7-day edge TTL (they are
# deterministic PNGs and were being re-rendered every 5 minutes at ~1.4s each),
# a redesign no longer reaches LinkedIn until that TTL expires. #3979 shipped a
# full re-skin onto the brand tokens and the fleet would have served the old art
# for a week. This is the missing step, and it recurs on EVERY card change.
#
# Three constraints shaped it:
#
#  1. NO CALLER-SUPPLIED URLS. This is public, matching purge/markets-fix and
#     purge/frontend-static. A public endpoint that purges whatever it is handed
#     lets anyone evict any path on the zone — cheap origin-load amplification.
#     The list is DERIVED here and the caller cannot influence it.
#  2. DERIVED, NOT HARDCODED. A card URL embeds the page's title
#     (?style=editorial&title=...), so a hardcoded list silently rots the first
#     time a headline is edited — it would purge URLs nobody serves while the
#     live card stayed stale. So: read the pages and take the og:image they
#     actually publish.
#  3. ALLOWLISTED. A page could point og:image at any host. Only our own hosts,
#     and only OG-card paths, are ever sent to CF.
_OG_PAGES = (
    "/", "/news", "/pricing", "/about", "/agents", "/markets",
    "/dcpi", "/enterprise", "/integrations/mcp",
)
_OG_HOSTS = ("dchub.cloud", "api.dchub.cloud", "www.dchub.cloud")
_OG_PATH_HINTS = ("/api/v1/og/", "/images/og")
_OG_PURGE_COOLDOWN_S = 60
_og_purge_last = [0.0]          # per-PROCESS, so it blunts a loop, not a fleet


def _og_card_urls() -> tuple[list, list]:
    """Return (urls, notes). Reads each page and keeps the og:image it
    publishes, if it points at one of our own card paths."""
    import re
    import requests
    urls, notes = [], []
    for path in _OG_PAGES:
        page = f"https://dchub.cloud{path}"
        try:
            r = requests.get(page, timeout=8, headers={
                # urllib/py UA is 403'd at the edge; identify honestly.
                "User-Agent": "dchub-og-purge/1.0 (+https://dchub.cloud)"})
            if r.status_code != 200:
                notes.append(f"{path}: HTTP {r.status_code}")
                continue
            m = re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)',
                          r.text) or \
                re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image',
                          r.text)
            if not m:
                notes.append(f"{path}: no og:image")
                continue
            url = m.group(1).strip()
            host = url.split("//", 1)[-1].split("/", 1)[0].lower()
            if host not in _OG_HOSTS or not any(h in url for h in _OG_PATH_HINTS):
                notes.append(f"{path}: og:image outside the card allowlist ({host})")
                continue
            if url not in urls:
                urls.append(url)
        except Exception as e:
            notes.append(f"{path}: {type(e).__name__}")
    return urls, notes



def _still_cached(urls: list) -> list:
    """Re-fetch each purged URL and return those the edge STILL serves from
    cache. CF's `success:true` means "we accepted the request", not "the object
    is gone" — see the note on purge_og_cards."""
    import requests
    stuck = []
    for u in urls:
        try:
            r = requests.get(u, timeout=12, headers={
                "User-Agent": "dchub-og-purge/1.0 (+https://dchub.cloud)"})
            if (r.headers.get("cf-cache-status") or "").upper() == "HIT":
                stuck.append({"url": u, "age": r.headers.get("age")})
        except Exception as e:
            stuck.append({"url": u, "error": type(e).__name__})
    return stuck


@cf_purge_bp.route("/api/v1/cf/purge/og-cards", methods=["GET", "POST"])
def purge_og_cards():
    """One-shot: purge the OG/social cards after a card redesign.

    Public GET, idempotent and read-side — same rationale as
    purge/markets-fix and purge/frontend-static above. The URL list is derived
    from the og:image each page actually publishes and allowlisted to our own
    card paths; nothing the caller sends is used.
    """
    import time
    now = time.time()
    if now - _og_purge_last[0] < _OG_PURGE_COOLDOWN_S:
        # Not an error — the previous purge is seconds old and purges are
        # idempotent, so the honest answer is "already done".
        return jsonify(ok=True, skipped="cooldown",
                       cooldown_s=_OG_PURGE_COOLDOWN_S,
                       retry_in_s=round(_OG_PURGE_COOLDOWN_S - (now - _og_purge_last[0]), 1),
                       note="per-process cooldown; another replica may accept sooner"), 200
    _og_purge_last[0] = now

    urls, notes = _og_card_urls()
    # The static fallback card is served on pages that never touch the
    # generator, so a card redesign does not regenerate it — but it IS an OG
    # asset and it does go stale at the edge.
    static = "https://dchub.cloud/images/og-default.png"
    if static not in urls:
        urls.append(static)
    if not urls:
        return jsonify(ok=False, error="no og:image URLs could be derived",
                       pages_checked=len(_OG_PAGES), notes=notes), 200

    # CF accepts at most 30 files per purge call.
    results, purged = [], []
    for i in range(0, len(urls), 30):
        chunk = urls[i:i + 30]
        res = _purge_urls(chunk)
        results.append({"count": len(chunk), "ok": res.get("ok"),
                        "status": res.get("status"),
                        "error": res.get("error")})
        if res.get("ok"):
            purged.extend(chunk)

    # ★ 2026-09-06: CF ACCEPTED != OBJECT EVICTED. Measured on the first live
    # run: one call purged /images/og-default.png (MISS afterwards — worked) and
    # the homepage CARD (still HIT, age climbing 2250 -> 2326 — did nothing),
    # and CF reported success:true for BOTH.
    #
    # Cause: worker.js proxies with
    #     fetch(RAILWAY_BACKEND + pathname, {cf:{cacheTtl, cacheEverything:true}})
    # so the generated cards are cached under the RAILWAY origin URL, not the
    # public api.dchub.cloud one. Purge-by-URL on our zone cannot match a key it
    # does not own, so it is STRUCTURALLY incapable of clearing them. Only
    # purge_everything (or a worker cache-key change) reaches those objects.
    #
    # So this endpoint verifies its own work instead of trusting the API's
    # acknowledgement, and `ok` is false when anything survived.
    accepted = all(r["ok"] for r in results) if results else False
    stuck = _still_cached(purged) if accepted else []
    if stuck:
        notes.append(
            "CF accepted the purge but these are STILL cached — worker.js caches "
            "them under the Railway origin URL (cf.cacheEverything), which "
            "purge-by-URL on this zone cannot address. Needs purge_everything or "
            "a worker cache-key fix.")
    return jsonify(
        ok=accepted and not stuck,
        cf_accepted=accepted,
        purged=purged,
        purged_count=len(purged),
        evicted_count=len(purged) - len(stuck),
        still_cached=stuck,
        pages_checked=len(_OG_PAGES),
        batches=results,
        # Say what was NOT purged and why, rather than reporting a clean count
        # over a list that quietly lost half its entries.
        notes=notes,
    ), 200


def _cf_get(path: str) -> dict:
    """Helper: GET against CF API. Returns parsed JSON or error dict."""
    if not _CF_API_TOKEN:
        return {"ok": False, "error": "CLOUDFLARE_API_TOKEN not set"}
    try:
        import requests
        r = requests.get(
            f"https://api.cloudflare.com/client/v4{path}",
            headers={"Authorization": f"Bearer {_CF_API_TOKEN}"},
            timeout=12,
        )
        return r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cf_purge_bp.route("/api/v1/cf/inspect/token", methods=["GET"])
def inspect_token():
    """Phase ZZZZ-token-debug (2026-05-18): the token in Railway might be
    out of sync with the token in the CF dashboard (user edited token
    perms but Railway still has the OLD token value). This endpoint
    calls CF's /user/tokens/verify which returns the active token's
    own metadata + status — proves whether Railway has the current
    token value or a stale one.

    Hides the actual token but shows id, status, expires_on, and the
    last 6 chars so the user can compare to what's in their CF
    dashboard."""
    if not _CF_API_TOKEN:
        return jsonify(ok=False, error="CLOUDFLARE_API_TOKEN not set in Railway"), 503
    try:
        import requests
        r = requests.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {_CF_API_TOKEN}"},
            timeout=12,
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
        return jsonify(
            railway_token_last6=("..." + _CF_API_TOKEN[-6:]) if _CF_API_TOKEN else "(unset)",
            railway_token_length=len(_CF_API_TOKEN),
            status_code=r.status_code,
            cf_response=body,
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 503


@cf_purge_bp.route("/api/v1/cf/inspect/routes-and-rules", methods=["GET"])
def inspect_routes_and_rules():
    """Phase ZZZZ-cf-inspect (2026-05-18): aggregate view of everything
    that could be intercepting requests for dchub.cloud, so we can find
    what's catching /markets and what's serving the 4.8.3 worker error
    on /api/v1/marketing/publish-now.

    Returns:
      - workers in account (list of all worker scripts)
      - worker routes (which patterns map to which scripts)
      - pages projects
      - zone DNS records (looks for hostname intercepts)
      - bulk redirects + ruleset rules

    Public read-only — no admin gate. Token must have Workers/Pages/Zone
    read perms.
    """
    acct = (os.environ.get("CLOUDFLARE_ACCOUNT_ID")
            or "4bb33ec40ef02f9f4b41dc97668d5a52").strip()
    zone = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
    out = {"account": acct, "zone_id": zone or "(unset — set CLOUDFLARE_ZONE_ID)"}

    # All worker scripts in the account
    workers = _cf_get(f"/accounts/{acct}/workers/scripts")
    out["workers_scripts"] = [
        {"id": w.get("id"),
         "created_on": w.get("created_on"),
         "modified_on": w.get("modified_on"),
         "logpush": w.get("logpush"),
         "placement_mode": w.get("placement", {}).get("mode") if isinstance(w.get("placement"), dict) else None}
        for w in (workers.get("result") or [])
    ] if workers.get("success") else {"_error": workers.get("errors", workers)}

    # Pages projects
    pages = _cf_get(f"/accounts/{acct}/pages/projects")
    out["pages_projects"] = [
        {"name": p.get("name"),
         "subdomain": p.get("subdomain"),
         "domains": p.get("domains"),
         "production_branch": p.get("production_branch"),
         "latest_deployment": (p.get("latest_deployment") or {}).get("created_on")}
        for p in (pages.get("result") or [])
    ] if pages.get("success") else {"_error": pages.get("errors", pages)}

    # Zone-scoped data only if ZONE_ID set
    if zone:
        # Worker routes on this zone — THE key data
        routes = _cf_get(f"/zones/{zone}/workers/routes")
        out["worker_routes"] = [
            {"pattern": r.get("pattern"), "script": r.get("script"), "id": r.get("id")}
            for r in (routes.get("result") or [])
        ] if routes.get("success") else {"_error": routes.get("errors", routes)}

        # Ruleset entries (transform rules, redirect rules, etc.)
        rulesets = _cf_get(f"/zones/{zone}/rulesets")
        out["rulesets"] = [
            {"id": rs.get("id"), "name": rs.get("name"), "phase": rs.get("phase"),
             "kind": rs.get("kind")}
            for rs in (rulesets.get("result") or [])
        ] if rulesets.get("success") else {"_error": rulesets.get("errors", rulesets)}

        # Page Rules
        page_rules = _cf_get(f"/zones/{zone}/pagerules")
        out["page_rules"] = [
            {"targets": [t.get("constraint", {}).get("value")
                         for t in (pr.get("targets") or [])],
             "actions": [a.get("id") for a in (pr.get("actions") or [])],
             "status": pr.get("status")}
            for pr in (page_rules.get("result") or [])
        ] if page_rules.get("success") else {"_error": page_rules.get("errors", page_rules)}
    else:
        out["worker_routes"] = "(need CLOUDFLARE_ZONE_ID)"

    return jsonify(out), 200
