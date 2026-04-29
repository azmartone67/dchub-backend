"""
Headless-browser smoke test for the two map pages that broke on 2026-04-22.

Why this exists: the original outage went undetected by every server-side
check we had, because CSP violations only fire in a real browser. A simple
HTTP 200 on the HTML is not proof the page works — Leaflet, markercluster,
esri-leaflet, and html2canvas all load from external origins, and if CSP
blocks them the page renders as a blank shell with a console full of
`ReferenceError: L is not defined`.

This script loads /map and /land-power-map in a real headless Chromium,
waits for window.map to be defined, and fails if ANY of these happen:
  - page returns non-2xx
  - the browser logs a CSP violation for a required FIRST-PARTY origin
    (we ignore failures from vendor analytics / tracking beacons; see
    NON_FATAL_DOMAINS below)
  - window.map is undefined after a reasonable timeout
  - console has uncaught errors referencing Leaflet, L, or our key scripts

Vendor analytics beacon failures (Google Analytics, DoubleClick, Facebook
pixel, etc.) are logged but do NOT mark the test as fatal. Those fail for
ad-block reasons, transient vendor outages, or regional restrictions, and
they don't affect the actual user experience of the map. We care about
first-party functionality, not third-party telemetry.

Run locally:
    pip install playwright --break-system-packages
    playwright install chromium
    python smoke_map_pages.py

Run in CI (GitHub Actions / Railway post-deploy hook):
    python smoke_map_pages.py --base https://dchub.cloud --json

Exit 0 = all green. Exit 1 = at least one failure (alert / fail the deploy).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from playwright.sync_api import ConsoleMessage, Page, Response, sync_playwright
except ImportError:
    print(
        "ERROR: playwright not installed. Run:\n"
        "    pip install playwright --break-system-packages\n"
        "    playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(2)

PAGES = ["/map", "/land-power-map"]

# Domains/paths whose failures are NON-FATAL. These are vendor analytics,
# tracking pixels, and remarketing beacons — their failure doesn't break
# the user experience of the map. Match is a simple substring check against
# the URL or error message (case-insensitive).
NON_FATAL_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googleadservices.com",
    "google.com/ads",          # ga-audiences remarketing beacon
    "google.com/pagead",
    "facebook.com/tr",
    "connect.facebook.net",
    "clarity.ms",              # Microsoft Clarity
    "hotjar.com",
    "hs-scripts.com",          # HubSpot
    "hs-analytics.net",
    "linkedin.com/px",
    "bat.bing.com",            # Bing UET
    "intercom.io",
    "cdn.segment.com",
)

# Fail if any of these appear in a console message — they are the exact
# fingerprints of the 2026-04-22 outage (first-party CSP / Leaflet breaks).
FATAL_CONSOLE_PATTERNS = [
    re.compile(r"ReferenceError:\s*L\s+is\s+not\s+defined", re.I),
    re.compile(r"map\.on\s+is\s+not\s+a\s+function", re.I),
    re.compile(r"Refused to load the (script|stylesheet) because it violates", re.I),
    re.compile(r"Content Security Policy", re.I),
]

# How long we'll wait for window.map to exist before giving up.
MAP_INIT_TIMEOUT_MS = 15_000


def _is_vendor_noise(text: str) -> bool:
    """Return True if the message/URL looks like a third-party analytics
    beacon whose failure is expected/harmless."""
    low = text.lower()
    return any(d in low for d in NON_FATAL_DOMAINS)


@dataclass
class PageResult:
    path: str
    ok: bool
    http_status: int | None = None
    map_initialized: bool = False
    map_init_ms: int | None = None
    csp_violations: list[str] = field(default_factory=list)          # FATAL
    console_errors: list[str] = field(default_factory=list)          # informational
    failed_requests: list[str] = field(default_factory=list)         # informational
    ignored_vendor_failures: list[str] = field(default_factory=list) # tracked, non-fatal
    fatal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "http_status": self.http_status,
            "map_initialized": self.map_initialized,
            "map_init_ms": self.map_init_ms,
            "csp_violations": self.csp_violations,
            "console_errors": self.console_errors,
            "failed_requests": self.failed_requests,
            "ignored_vendor_failures": self.ignored_vendor_failures,
            "fatal_reason": self.fatal_reason,
        }


def _check_page(page: Page, base_url: str, path: str) -> PageResult:
    result = PageResult(path=path, ok=False)
    console_messages: list[str] = []
    failed_requests: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        if msg.type in {"error", "warning"}:
            console_messages.append(f"[{msg.type}] {msg.text}")

    def on_request_failed(req: Any) -> None:
        failed_requests.append(f"{req.method} {req.url} ({req.failure})")

    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)

    url = base_url.rstrip("/") + path
    try:
        response: Response | None = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:  # noqa: BLE001
        result.fatal_reason = f"navigation exception: {e}"
        return result

    if response is None:
        result.fatal_reason = "no response received"
        return result

    result.http_status = response.status
    if not (200 <= response.status < 300):
        result.fatal_reason = f"http {response.status}"
        return result

    # Wait up to MAP_INIT_TIMEOUT_MS for window.map to be defined.
    t0 = time.time()
    try:
        page.wait_for_function(
            "() => typeof window.map !== 'undefined' && window.map !== null",
            timeout=MAP_INIT_TIMEOUT_MS,
        )
        result.map_initialized = True
        result.map_init_ms = int((time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        result.fatal_reason = f"window.map never initialized: {e}"

    # Give CSP violations a beat to fire before we collect messages.
    page.wait_for_timeout(500)

    # Partition messages: vendor analytics → ignored; everything else → fatal candidates.
    for msg in console_messages:
        if _is_vendor_noise(msg):
            result.ignored_vendor_failures.append(msg)
            continue
        result.console_errors.append(msg)
        for pat in FATAL_CONSOLE_PATTERNS:
            if pat.search(msg):
                result.csp_violations.append(msg)
                break

    for fr in failed_requests:
        if _is_vendor_noise(fr):
            result.ignored_vendor_failures.append(fr)
            continue
        result.failed_requests.append(fr)

    if result.csp_violations and not result.fatal_reason:
        result.fatal_reason = (
            f"CSP/leaflet fingerprint found in {len(result.csp_violations)} first-party message(s)"
        )

    result.ok = (
        result.map_initialized
        and not result.csp_violations
        and result.fatal_reason is None
    )
    return result


def run(base_url: str) -> list[PageResult]:
    results: list[PageResult] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = browser.new_context(
                ignore_https_errors=False,
                user_agent="DCHub-Smoke-Test/1.1 (+playwright)",
            )
            for path in PAGES:
                page = context.new_page()
                try:
                    results.append(_check_page(page, base_url, path))
                finally:
                    page.close()
            context.close()
        finally:
            browser.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://dchub.cloud", help="Base URL")
    parser.add_argument("--json", action="store_true", help="Emit JSON only (for CI)")
    parser.add_argument(
        "--verbose-vendor",
        action="store_true",
        help="Include ignored_vendor_failures in text output (debug)",
    )
    args = parser.parse_args()

    results = run(args.base)
    all_ok = all(r.ok for r in results)

    if args.json:
        print(json.dumps(
            {"ok": all_ok, "results": [r.to_dict() for r in results]},
            indent=2,
        ))
    else:
        for r in results:
            status_txt = "PASS" if r.ok else "FAIL"
            print(f"[{status_txt}] {r.path}  (http={r.http_status}  map_init={r.map_initialized}  ms={r.map_init_ms})")
            if r.fatal_reason:
                print(f"       reason: {r.fatal_reason}")
            if r.csp_violations:
                for v in r.csp_violations[:3]:
                    print(f"       csp: {v[:200]}")
            if r.failed_requests:
                for fr in r.failed_requests[:3]:
                    print(f"       net: {fr[:200]}")
            if args.verbose_vendor and r.ignored_vendor_failures:
                print(f"       (ignored {len(r.ignored_vendor_failures)} vendor-analytics failure(s))")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
