"""GSC re-submission for the 7 SEO landings that were 403'ing.

Background (2026-06-05): the autopilot created 7 high-intent SEO
landings under CF-blocked prefixes (/aws/*, /address/*, /interxion-*,
/moltbook-*) which returned Error 1000 at the CF edge for hours.
Google deprioritized the domain. Fix shipped: the canonical URLs
moved under CF-routable prefixes, AND the legacy paths now 301 via
the CF Pages worker.

This script accelerates Google's re-discovery by:

  1. Pinging Google's sitemap notification endpoint with the updated
     sitemap (always works, no auth required, free)
  2. Calling the URL Inspection API to request re-crawl for each of
     the 7 NEW URLs (requires GSC service account creds; optional)
  3. Ping IndexNow (Bing's protocol — works for Yandex too)

USAGE:
    # Option A — sitemap ping only (no auth, runs instantly):
    python scripts/gsc_resubmit_landings.py --sitemap-only

    # Option B — full re-submission (needs GSC service account):
    export GSC_SERVICE_ACCOUNT_JSON=/path/to/credentials.json
    python scripts/gsc_resubmit_landings.py

NOTES:
  - Google deprecated the sitemap ping endpoint in 2023 but it still
    accepts pings silently; we ping it as a belt-and-suspenders move.
  - The URL Inspection API has a 2000/day per-property quota, so
    7 URLs is well within limits.
  - Re-discovery typically takes 24-72h after submission.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request


SITE = "https://dchub.cloud"
SITEMAP_URL = f"{SITE}/sitemap.xml"
LANDINGS_SITEMAP_URL = f"{SITE}/sitemap-landings.xml"

# The 7 NEW URLs (CF-routable). These are what Google should re-index.
NEW_URLS = [
    f"{SITE}/facility/aws-iad36",
    f"{SITE}/facility/aws-db1",
    f"{SITE}/facility/aws-kix10",
    f"{SITE}/facility/aws-sjc29",
    f"{SITE}/markets/interxion-frankfurt",
    f"{SITE}/partners/moltbook-api",
    f"{SITE}/facility/1725-comstock-st-san-jose",
]


def _verify_url(url: str) -> tuple[int, str]:
    """Return (status_code, content_type) for a HEAD request."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)[:60]


def ping_sitemap(url: str) -> bool:
    """Ping Google + Bing about the sitemap. Returns True if either accepted."""
    targets = [
        f"https://www.google.com/ping?sitemap={urllib.parse.quote(url)}",
        f"https://www.bing.com/ping?sitemap={urllib.parse.quote(url)}",
    ]
    any_ok = False
    for t in targets:
        try:
            with urllib.request.urlopen(t, timeout=10) as r:
                ok = 200 <= r.status < 300
                print(f"  {'✅' if ok else '❌'} {r.status:3d}  {t}")
                if ok:
                    any_ok = True
        except Exception as e:
            print(f"  ❌ ERR  {t}  ({e})")
    return any_ok


def request_indexnow(urls: list[str]) -> bool:
    """IndexNow (Bing + Yandex + Naver). Free, no auth."""
    # Public host key — anyone can request indexing for their domain.
    # We use a static key file at /indexnow-key.txt (need to add to repo
    # for self-verification). For now, skip if not configured.
    key = os.environ.get("DCHUB_INDEXNOW_KEY")
    if not key:
        print("  (skipped — set DCHUB_INDEXNOW_KEY to enable)")
        return False
    payload = json.dumps({
        "host":     "dchub.cloud",
        "key":      key,
        "urlList":  urls,
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.indexnow.org/IndexNow",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = r.status in (200, 202)
            print(f"  {'✅' if ok else '❌'} {r.status}  IndexNow ({len(urls)} URLs)")
            return ok
    except Exception as e:
        print(f"  ❌ ERR  IndexNow  ({e})")
        return False


def request_gsc_inspection(urls: list[str], creds_path: str) -> int:
    """GSC URL Inspection API — request re-crawl for each URL.

    Requires google-api-python-client + a service account with GSC
    access to the property.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        print("  ⚠️  google-api-python-client not installed; skipping GSC API")
        print("      pip install google-api-python-client google-auth")
        return 0
    creds = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/webmasters"],
    )
    svc = build("searchconsole", "v1", credentials=creds)
    n_ok = 0
    for url in urls:
        try:
            req = {"inspectionUrl": url, "siteUrl": SITE + "/"}
            res = svc.urlInspection().index().inspect(body=req).execute()
            status = (res.get("inspectionResult") or {}).get(
                "indexStatusResult", {}).get("verdict", "?")
            print(f"  ✅ {url}  status={status}")
            n_ok += 1
        except Exception as e:
            print(f"  ❌ {url}  ({e})")
        time.sleep(1.0)  # polite spacing
    return n_ok


def main():
    sitemap_only = "--sitemap-only" in sys.argv

    print("=" * 70)
    print("Step 1: Verify all 7 NEW URLs return 200")
    print("=" * 70)
    all_ok = True
    for url in NEW_URLS:
        code, _ = _verify_url(url)
        symbol = "✅" if code == 200 else "❌"
        print(f"  {symbol}  HTTP {code:3d}  {url}")
        if code != 200:
            all_ok = False
    if not all_ok:
        print("\n  ⚠️  At least one URL is not 200 — fix that before re-submitting.")
        sys.exit(1)

    print()
    print("=" * 70)
    print("Step 2: Verify legacy URLs redirect (301) — NOT 403")
    print("=" * 70)
    LEGACY_PAIRS = [
        ("/aws/iad36",                         "/facility/aws-iad36"),
        ("/aws/db1",                           "/facility/aws-db1"),
        ("/aws/kix10",                         "/facility/aws-kix10"),
        ("/aws/sjc29",                         "/facility/aws-sjc29"),
        ("/interxion-frankfurt",               "/markets/interxion-frankfurt"),
        ("/moltbook-api-documentation",        "/partners/moltbook-api"),
        ("/address/1725-comstock-st-san-jose", "/facility/1725-comstock-st-san-jose"),
    ]
    for legacy, target in LEGACY_PAIRS:
        # urllib follows 301s by default; we want to check that the
        # final URL is the new target
        try:
            req = urllib.request.Request(SITE + legacy, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as r:
                final = r.url
                ok = final.endswith(target)
                symbol = "✅" if ok else "❌"
                print(f"  {symbol}  {legacy:42s} → {final}")
        except Exception as e:
            print(f"  ❌  {legacy:42s} ({e})")

    print()
    print("=" * 70)
    print("Step 3: Ping sitemap (Google + Bing)")
    print("=" * 70)
    ping_sitemap(SITEMAP_URL)
    ping_sitemap(LANDINGS_SITEMAP_URL)

    print()
    print("=" * 70)
    print("Step 4: IndexNow (Bing + Yandex)")
    print("=" * 70)
    request_indexnow(NEW_URLS)

    if not sitemap_only:
        print()
        print("=" * 70)
        print("Step 5: GSC URL Inspection API")
        print("=" * 70)
        creds = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
        if creds and os.path.exists(creds):
            n = request_gsc_inspection(NEW_URLS, creds)
            print(f"\n  Submitted {n}/{len(NEW_URLS)} URLs to GSC.")
        else:
            print("  (skipped — set GSC_SERVICE_ACCOUNT_JSON=<path> to enable)")

    print()
    print("=" * 70)
    print("Done. Expect Google to re-index in 24-72h. Monitor at:")
    print("  https://search.google.com/search-console")
    print("=" * 70)


if __name__ == "__main__":
    main()
