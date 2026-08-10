#!/usr/bin/env python3
"""Guard: the FREE, KEYLESS surface must answer a bare `urllib` request.

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
    0  every checked path returned a non-403 status  (allowance is in place)
    1  at least one path returned 403                (REGRESSION)
    2  the guard could not run honestly              (vacuous UA, bad args)

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

    opener, ua = build_honest_opener()
    print(f"Probing {base} as {ua!r}  ({len(paths)} paths)\n")

    results, blocked, transport_errors = [], [], []
    for p in paths:
        # Cache-bust every request: a cached 200 is not evidence the allowance
        # is live, and CF caches /api/v1/* with override_origin.
        sep = "&" if "?" in p else "?"
        url = f"{base}{p}{sep}_={int(time.time() * 1000)}"
        status, cache, ray, snippet = probe(opener, url, args.timeout)

        if status == 403:
            verdict, blocked_flag = "BLOCKED", True
            blocked.append(p)
        elif status is None:
            verdict, blocked_flag = "TRANSPORT-ERROR", False
            transport_errors.append(p)
        else:
            verdict, blocked_flag = "ok", False

        results.append(
            {
                "path": p,
                "status": status,
                "cf_cache_status": cache,
                "cf_ray": ray,
                "verdict": verdict,
                "blocked": blocked_flag,
                "body_snippet": snippet.decode("utf-8", "replace"),
            }
        )
        mark = "❌" if verdict == "BLOCKED" else ("⚠️ " if status is None else "✅")
        detail = f"  {snippet.decode('utf-8', 'replace').strip()!r}" if verdict != "ok" else ""
        print(
            f"{mark} {str(status):>4}  cf-cache={cache:<8} {p}{detail}"
        )

    if args.report:
        with open(args.report, "w") as fh:
            json.dump(
                {"base": base, "user_agent": ua, "results": results}, fh, indent=2
            )

    print()
    if blocked:
        print(
            f"❌ REGRESSION: {len(blocked)}/{len(paths)} free, keyless path(s) return 403 "
            f"to urllib's default User-Agent ({ua}).\n"
            "   These are the surfaces third parties and AI crawlers verify us with.\n"
            "   Blocked: " + ", ".join(blocked) + "\n"
            "   This is a Cloudflare-side allowance, not application code — see\n"
            "   docs/cf-urllib-block-2026-08-10.md for the exact remediation steps.",
            file=sys.stderr,
        )
        return 1

    if transport_errors:
        # Network trouble is not a pass. Fail loudly rather than green-by-accident.
        print(
            f"⚠️  could not reach {len(transport_errors)} path(s): "
            + ", ".join(transport_errors)
            + "\n   Treating as failure — an unreachable probe proves nothing.",
            file=sys.stderr,
        )
        return 1

    print(f"✅ all {len(paths)} free, keyless paths answer bare urllib ({ua}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
