#!/usr/bin/env python3
"""Guard: the FREE, KEYLESS surface must answer real programmatic clients.

★ 2026-08-22 ROOT CAUSE + CONTRACT CHANGE
-----------------------------------------
The `Python-urllib` 403/1010 is NOT this zone's Browser Integrity Check. It is
the Cloudflare PAGES pipeline's own BIC: `dchub-frontend.pages.dev` refuses
`Python-urllib/*` and `libwww-perl/*` directly, `api.dchub.cloud` (zone worker
→ Railway, not Pages) answers 200, the zone's `browser_check` has been OFF
since 2026-08-13, and every zone-level object that could block (configuration
rules, custom rules, page rules, snippets, UA blocking, AI Crawl Control, both
workers' source) was verified clean. CF's own request log records the 403 as
an ORIGIN response with no security action — on a Pages site the origin is the
Pages platform. No zone setting can reach it; only a zone Worker route in
front of Pages bypasses it (that is why /mcp* and /.well-known/* pass).

Owner decision 2026-08-22: do not reroute production paths through the zone
worker to satisfy a probe whose blocked UAs no real agent or crawler sends
(CCBot, Bytespider, GPTBot, ClaudeBot, PerplexityBot, Scrapy, Wget, curl,
python-requests, Go-http-client, node-fetch all pass). So this script now has
TWO tiers:

  GUARD  (exit 1 on 403)  — the clients third parties actually use:
         python-requests, Go-http-client, node-fetch, curl, Wget.
  GAUGE  (never exits 1)  — urllib's bare default UA. Reported every run with
         the root cause; a `::warning::` while the Pages-layer block stands, a
         `::notice::` the day it lifts. Tracking issue #2564.

Original note (2026-08-10), kept for the record
-----------------------------------------------

Why this exists (2026-08-10)
----------------------------
DC Hub's positioning is "the live infrastructure data layer for AI agents", and
`/api/v1/dcpi/scores/<slug>` is deliberately keyless so third parties can verify
our published numbers. On 2026-08-10 every one of those surfaces returned

    HTTP 403 / body: "error code: 1010"

to any request whose User-Agent *starts with* `Python-urllib` — i.e. to the
obvious three-line verification script an analyst or an agent would write. The
403 is emitted at the Cloudflare edge (no `x-dc-worker-version` header on the
response, `Server-Timing: cfOrigin;dur=0`), so neither `worker.js` nor the Flask
backend can see it, fix it, or notice it regressing. Only an outside probe can.

Measured blast radius on that date: `/api/v1/dcpi/scores/<slug>`,
`/api/v1/dcpi/methodology`, `/api/v1/stats`, `/llms.txt`, `/sitemap-dcpi.xml`
and `/robots.txt` — all 403. The block is prefix-anchored: `Python-urllib/3` is
blocked, `MyTool Python-urllib/3.14` is not.

The remedy is a Cloudflare-side allowance (see
`docs/cf-urllib-block-2026-08-10.md`). This script is the regression check for
it. It fails on 403 so the allowance cannot be silently removed again.

★ THE VACUITY TRAP THIS SCRIPT DEFENDS AGAINST
`main.py` line 1 imports `http_ua_default`, a global shim that forces a real
browser User-Agent onto *every* urllib and requests call in the backend process,
precisely to dodge this same CF 403 on outbound calls. If that shim — or any
well-meaning `headers={'User-Agent': ...}` — ever reaches this script, it would
send a browser UA, get 200 forever, and pass whether or not the public API is
actually reachable. So the script asserts the UA it is about to send really is
`Python-urllib/*` and exits 2 (not 0) if it is not. A guard that cannot fail is
worse than no guard, because it reads as evidence.

Exit codes
    0  every checked path answered every REAL client with a non-403 status
       (the urllib gauge is reported but never fails the run)
    1  at least one path returned 403 to a real client, or was unreachable
    2  the gauge could not run honestly (urllib's default UA was overridden)

Usage
    python3 scripts/check_public_api_programmatic_access.py
    python3 scripts/check_public_api_programmatic_access.py --base https://dchub.cloud
    python3 scripts/check_public_api_programmatic_access.py --base https://example.com --paths /
"""

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

# The free, keyless surfaces we point third parties and AI crawlers at.
# Every one of these was 403 to bare urllib on 2026-08-10.
DEFAULT_PATHS = [
    "/api/v1/dcpi/scores/tokyo",
    "/api/v1/dcpi/methodology",
    "/api/v1/stats",
    "/llms.txt",
    "/sitemap-dcpi.xml",
    "/robots.txt",
]

DEFAULT_BASE = "https://dchub.cloud"

# The clients third parties and AI agents actually send. Measured 2026-08-22:
# all of these pass the Pages-layer check; only Python-urllib and libwww-perl
# do not. This list is the GUARD; urllib's default UA is the GAUGE.
REAL_CLIENT_UAS = [
    "python-requests/2.32.3",
    "Go-http-client/2.0",
    "node-fetch/1.0 (+https://github.com/node-fetch/node-fetch)",
    "curl/8.5.0",
    "Wget/1.21.4",
]

PAGES_BIC_SIGNATURE = b"error code: 1010"


def build_client_opener(ua):
    """An opener that sends exactly `ua` (the guard tier)."""
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-agent", ua)]
    return opener



def build_honest_opener():
    """Return (opener, ua). Exits 2 if the UA is not urllib's real default.

    `build_opener()` seeds `addheaders` with `[('User-agent', 'Python-urllib/X.Y')]`.
    We read the UA off the very opener we are about to fetch with, so what we
    assert is exactly what goes on the wire — not a re-derivation that could
    drift from it.
    """
    opener = urllib.request.build_opener()
    ua = dict(opener.addheaders).get("User-agent", "")
    if not ua.startswith("Python-urllib/"):
        print(
            "❌ GUARD IS VACUOUS — refusing to report a result.\n"
            f"   This check only means anything if it sends urllib's DEFAULT User-Agent.\n"
            f"   It was about to send: {ua!r}\n"
            "   Something has overridden urllib's default (most likely main.py's\n"
            "   `http_ua_default` shim, or an added User-Agent header). A browser UA\n"
            "   is not blocked, so this script would pass unconditionally.\n"
            "   Fix the override; do not 'fix' this assertion.",
            file=sys.stderr,
        )
        sys.exit(2)
    return opener, ua


def probe(opener, url, timeout=30):
    """GET url. Return (status, cf_cache_status, cf_ray, body_snippet)."""
    try:
        with opener.open(url, timeout=timeout) as r:
            body = r.read(200)
            return (
                r.status,
                r.headers.get("cf-cache-status", "-"),
                r.headers.get("cf-ray", "-"),
                body[:120],
            )
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(200)
        except Exception:
            pass
        return (
            e.code,
            e.headers.get("cf-cache-status", "-") if e.headers else "-",
            e.headers.get("cf-ray", "-") if e.headers else "-",
            body[:120],
        )
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        # A transport failure is NOT a 403 and must not be reported as one.
        # It is also not a pass. Surface it as its own state.
        return (None, "-", "-", str(e).encode()[:120])


def _run_path(opener, base, path, timeout):
    sep = "&" if "?" in path else "?"
    # Cache-bust every request: a cached 200 is not evidence, and CF caches
    # /api/v1/* with override_origin.
    url = f"{base}{path}{sep}_={int(time.time() * 1000)}"
    status, cache, ray, snippet = probe(opener, url, timeout)
    return {
        "path": path,
        "status": status,
        "cf_cache_status": cache,
        "cf_ray": ray,
        "body_snippet": snippet.decode("utf-8", "replace"),
        "pages_bic": status == 403 and PAGES_BIC_SIGNATURE in snippet,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE, help="origin to probe")
    ap.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="override the path list (default: the free keyless surface)",
    )
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--report", help="write a JSON report here")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    paths = args.paths if args.paths else DEFAULT_PATHS

    # ── GUARD: real programmatic clients must get through ────────────────
    guard_results, blocked, transport_errors = [], [], []
    print(f"GUARD  Probing {base} as {len(REAL_CLIENT_UAS)} real client UA(s)  ({len(paths)} paths)")
    for ua in REAL_CLIENT_UAS:
        opener = build_client_opener(ua)
        for p in paths:
            r = _run_path(opener, base, p, args.timeout)
            r["user_agent"] = ua
            if r["status"] == 403:
                r["verdict"] = "BLOCKED"; blocked.append(f"{p} [{ua}]")
            elif r["status"] is None:
                r["verdict"] = "TRANSPORT-ERROR"; transport_errors.append(f"{p} [{ua}]")
            else:
                r["verdict"] = "ok"
            guard_results.append(r)
            mark = "❌" if r["verdict"] == "BLOCKED" else ("⚠️ " if r["status"] is None else "✅")
            detail = f"  {r['body_snippet'].strip()!r}" if r["verdict"] != "ok" else ""
            print(f"{mark} {str(r['status']):>4}  cf-cache={r['cf_cache_status']:<8} {p}  [{ua.split('/')[0]}]{detail}")

    # ── GAUGE: urllib's bare default UA, reported, never failing ─────────
    opener, ua = build_honest_opener()   # exits 2 if the default UA was overridden
    print(f"\nGAUGE  Probing {base} as {ua!r}  ({len(paths)} paths)")
    gauge_results = []
    for p in paths:
        r = _run_path(opener, base, p, args.timeout)
        r["user_agent"] = ua
        r["verdict"] = "pages_bic" if r["pages_bic"] else ("ok" if r["status"] not in (None, 403) else "other")
        gauge_results.append(r)
        mark = "📊" if r["pages_bic"] else ("✅" if r["verdict"] == "ok" else "⚠️ ")
        print(f"{mark} {str(r['status']):>4}  cf-cache={r['cf_cache_status']:<8} {p}")
    gauge_blocked = [r["path"] for r in gauge_results if r["pages_bic"]]

    if args.report:
        with open(args.report, "w") as fh:
            json.dump(
                {
                    "base": base,
                    "guard": {"user_agents": REAL_CLIENT_UAS, "results": guard_results, "blocked": blocked},
                    "gauge": {"user_agent": ua, "results": gauge_results,
                              "blocked_by_pages_bic": bool(gauge_blocked), "paths": gauge_blocked},
                },
                fh, indent=2,
            )

    print()
    if gauge_blocked:
        print(
            f"::warning::GAUGE — {len(gauge_blocked)}/{len(paths)} path(s) still answer 403/1010 to "
            f"urllib's default UA ({ua}). Known cause: the Cloudflare PAGES pipeline's own Browser "
            "Integrity Check (zone BIC is off; no zone setting reaches it; only a zone Worker route "
            "in front of Pages bypasses it). Owner decision 2026-08-22: tracked, not paged. Issue #2564."
        )
    else:
        print(
            f"::notice::GAUGE — urllib's default UA ({ua}) now passes on all {len(paths)} paths: the "
            "Pages-layer block has lifted. Consider promoting this gauge back to a guard."
        )

    if blocked:
        print(
            f"❌ REGRESSION: {len(blocked)} real-client probe(s) returned 403 on the free, keyless surface.\n"
            "   Blocked: " + ", ".join(blocked),
            file=sys.stderr,
        )
        return 1

    if transport_errors:
        print(
            f"⚠️  could not reach {len(transport_errors)} probe(s): " + ", ".join(transport_errors)
            + "\n   Treating as failure — an unreachable probe proves nothing.",
            file=sys.stderr,
        )
        return 1

    print(f"✅ all {len(paths)} free, keyless paths answer all {len(REAL_CLIENT_UAS)} real client UAs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
