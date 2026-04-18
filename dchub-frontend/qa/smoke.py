#!/usr/bin/env python3
"""
DC Hub — Live Smoke Tests (v7.9.10)
====================================
Runs AFTER deploy. Exercises each bug family from the Apr-14 2026 QA pass
against the actual deployed site. Pair with qa/squasher.py which catches
them pre-deploy in the static source.

Covers:
  S1  Assets page loads facilities without CORS error
  S2  Capacity-pipeline API returns dated rows
  S3  /ai-integrations serves distinct content from /ai
  S4  /api/press-releases returns JSON (not 503)
  S5  /api/v1/testimonials OR fallback paints 12+ cards
  S6  /markets/ serves the static index + city pages return 200
  S7  CF Worker CORS: preflight + response headers are correct
  S8  MCP endpoint (/mcp) returns tools/list
  S9  /.well-known/mcp/server-card.json returns 200

Exit 0 = all green. 1 = at least one smoke failed.

USAGE
-----
    pip install requests
    python qa/smoke.py                       # hit https://dchub.cloud
    python qa/smoke.py --base https://staging.dchub.cloud
    python qa/smoke.py --json                # machine-readable
    python qa/smoke.py --only S1 S3          # subset
    python qa/smoke.py --fail-fast           # stop on first failure

Add to a cron / GitHub Action to run every 15 min:
    */15 * * * *  python qa/smoke.py --json > /var/log/dchub-smoke.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable

try:
    import requests
except ImportError:
    print("smoke.py requires `requests`. Install with: pip install requests", file=sys.stderr)
    sys.exit(2)


DEFAULT_BASE = "https://dchub.cloud"
DEFAULT_TIMEOUT = 15
UA = "DCHub-SmokeTests/7.9.10 (+https://dchub.cloud/qa)"


@dataclass
class Result:
    id: str
    name: str
    ok: bool
    ms: int
    detail: str = ""
    status_code: int | None = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.ok]

    def to_json(self) -> str:
        return json.dumps({"results": [asdict(r) for r in self.results]}, indent=2)


# ─── HTTP helpers ─────────────────────────────────────────────────────────────
def _get(url: str, *, timeout: int = DEFAULT_TIMEOUT, **kw) -> requests.Response:
    kw.setdefault("headers", {})
    kw["headers"].setdefault("User-Agent", UA)
    kw["headers"].setdefault("Accept", "*/*")
    return requests.get(url, timeout=timeout, **kw)


def _options(url: str, origin: str, method: str = "GET", *, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    hdr = {
        "User-Agent": UA,
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "Authorization, Content-Type",
    }
    return requests.options(url, timeout=timeout, headers=hdr)


def _timed(fn: Callable[[], Result]) -> Result:
    t0 = time.time()
    try:
        r = fn()
    except Exception as exc:
        return Result(id="?", name="?", ok=False, ms=int((time.time() - t0) * 1000),
                      detail=f"exception: {exc!r}")
    r.ms = int((time.time() - t0) * 1000)
    return r


# ─── Smoke tests ──────────────────────────────────────────────────────────────
def s1_assets(base: str) -> Result:
    """Assets page must load >0 facilities via /api/v1/map same-origin."""
    def _run() -> Result:
        url = f"{base}/api/v1/map?all=true&limit=50"
        r = _get(url)
        if r.status_code != 200:
            return Result("S1", "assets-facilities", False, 0,
                          f"HTTP {r.status_code}", r.status_code)
        try:
            body = r.json()
        except Exception:
            return Result("S1", "assets-facilities", False, 0, "invalid JSON", r.status_code)
        rows = body.get("data") or body.get("facilities") or body
        n = len(rows) if isinstance(rows, list) else 0
        if n < 1:
            return Result("S1", "assets-facilities", False, 0,
                          f"0 facilities in response", r.status_code)
        return Result("S1", "assets-facilities", True, 0, f"{n} facilities", r.status_code)
    return _timed(_run)


def s2_capacity_pipeline(base: str) -> Result:
    """Pipeline API must return rows with usable date fields."""
    def _run() -> Result:
        for path in ("/api/pipeline", "/api/v1/pipeline"):
            r = _get(f"{base}{path}")
            if r.status_code != 200:
                continue
            try:
                body = r.json()
            except Exception:
                continue
            rows = body.get("pipeline") or body.get("projects") \
                   or (body.get("data") or {}).get("records") or []
            if not rows:
                continue
            DATE_FIELDS = (
                "announcement_date", "announced_date", "expected_online",
                "expected_completion", "online_date", "commissioning_date",
                "rfc_date", "target_date", "updated_at", "created_at",
            )
            dated = sum(1 for row in rows if any(row.get(f) for f in DATE_FIELDS))
            pct = round(100 * dated / len(rows), 1)
            ok = pct >= 40  # 40%+ dated is our acceptance floor
            return Result("S2", "pipeline-dates", ok, 0,
                          f"{dated}/{len(rows)} rows dated ({pct}%)", r.status_code)
        return Result("S2", "pipeline-dates", False, 0, "no pipeline endpoint responded")
    return _timed(_run)


def s3_ai_integrations_not_redirected(base: str) -> Result:
    """/ai-integrations must serve the integrations dashboard, NOT redirect to /ai."""
    def _run() -> Result:
        r = requests.get(f"{base}/ai-integrations", allow_redirects=False,
                         timeout=DEFAULT_TIMEOUT, headers={"User-Agent": UA})
        if 300 <= r.status_code < 400:
            loc = r.headers.get("Location", "")
            if loc.rstrip("/").endswith("/ai"):
                return Result("S3", "ai-integrations-no-redirect", False, 0,
                              f"redirected to {loc}", r.status_code)
        body = r.text if r.status_code == 200 else requests.get(
            f"{base}/ai-integrations", timeout=DEFAULT_TIMEOUT, headers={"User-Agent": UA}).text
        # Integrations page has a clearly distinct fingerprint
        marker_found = any(m in body for m in (
            "AI Platform Integrations",
            "DC Hub Nexus MCP Server",
            "Connected Platforms",
        ))
        if not marker_found:
            return Result("S3", "ai-integrations-no-redirect", False, 0,
                          "page served but markers missing — may be /ai content")
        return Result("S3", "ai-integrations-no-redirect", True, 0, "integrations page served")
    return _timed(_run)


def s4_press_releases(base: str) -> Result:
    """Press page should get JSON from same-origin /api/press-releases."""
    def _run() -> Result:
        r = _get(f"{base}/api/press-releases")
        if r.status_code != 200:
            return Result("S4", "press-releases", False, 0, f"HTTP {r.status_code}", r.status_code)
        try:
            body = r.json()
        except Exception:
            return Result("S4", "press-releases", False, 0, "not JSON", r.status_code)
        n = len(body) if isinstance(body, list) else len(body.get("releases", []))
        if n < 1:
            return Result("S4", "press-releases", False, 0, "empty list", r.status_code)
        return Result("S4", "press-releases", True, 0, f"{n} releases", r.status_code)
    return _timed(_run)


def s5_testimonials(base: str) -> Result:
    """Testimonials API returns >= 12 items OR page renders fallback block."""
    def _run() -> Result:
        r = _get(f"{base}/api/v1/testimonials?limit=100")
        if r.status_code == 200:
            try:
                body = r.json()
                items = body.get("testimonials") or []
                if len(items) >= 12:
                    return Result("S5", "testimonials", True, 0, f"api:{len(items)}", 200)
            except Exception:
                pass
        # Fallback: HTML page must still paint 12+ testimonial cards
        html = _get(f"{base}/testimonials").text
        n = html.count('class="testimonial-card')
        ok = n >= 12
        return Result("S5", "testimonials", ok, 0,
                      f"html-fallback cards={n}", r.status_code)
    return _timed(_run)


def s6_markets(base: str) -> Result:
    """Static /markets/ serves AND every canonical city page returns 200."""
    def _run() -> Result:
        idx = _get(f"{base}/markets/")
        if idx.status_code != 200:
            return Result("S6", "markets-dir", False, 0,
                          f"/markets/ HTTP {idx.status_code}", idx.status_code)
        # If the prior /markets 301 → /market-intelligence ever comes back,
        # the index fetch would redirect. `requests` follows by default, so
        # the URL we end up on is what matters:
        if "market-intelligence" in idx.url:
            return Result("S6", "markets-dir", False, 0,
                          "/markets/ is redirecting to /market-intelligence", 301)
        # Spot-check canonical cities
        canon = ["northern-virginia", "dallas", "phoenix", "london",
                 "frankfurt", "tokyo", "singapore", "sao-paulo"]
        misses = []
        for slug in canon:
            r = _get(f"{base}/markets/{slug}")
            if r.status_code != 200:
                misses.append(f"{slug}={r.status_code}")
        if misses:
            return Result("S6", "markets-dir", False, 0,
                          "missing: " + ", ".join(misses), 200)
        return Result("S6", "markets-dir", True, 0,
                      f"index + {len(canon)} city pages ok", 200)
    return _timed(_run)


def s7_worker_cors(base: str) -> Result:
    """Worker must return proper CORS headers on /api/* preflight."""
    def _run() -> Result:
        r = _options(f"{base}/api/v1/stats", origin="https://dchub.cloud")
        if r.status_code not in (200, 204):
            return Result("S7", "worker-cors", False, 0,
                          f"OPTIONS HTTP {r.status_code}", r.status_code)
        missing = [h for h in (
            "Access-Control-Allow-Origin",
            "Access-Control-Allow-Methods",
            "Access-Control-Allow-Headers",
        ) if h not in r.headers]
        if missing:
            return Result("S7", "worker-cors", False, 0,
                          f"missing headers: {', '.join(missing)}", r.status_code)
        ac = r.headers.get("Access-Control-Allow-Credentials", "").lower()
        if ac != "true":
            return Result("S7", "worker-cors", False, 0,
                          f"Allow-Credentials={ac!r} (must be 'true')", r.status_code)
        return Result("S7", "worker-cors", True, 0, "preflight clean", r.status_code)
    return _timed(_run)


def s8_mcp_tools_list(base: str) -> Result:
    """POST /mcp tools/list must return >= 15 tools."""
    def _run() -> Result:
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        r = requests.post(f"{base}/mcp", json=rpc,
                          timeout=DEFAULT_TIMEOUT,
                          headers={"User-Agent": UA, "Content-Type": "application/json",
                                   "Accept": "application/json"})
        if r.status_code != 200:
            return Result("S8", "mcp-tools", False, 0, f"HTTP {r.status_code}", r.status_code)
        try:
            body = r.json()
        except Exception:
            return Result("S8", "mcp-tools", False, 0, "not JSON", r.status_code)
        tools = (body.get("result") or {}).get("tools") or []
        ok = len(tools) >= 15
        return Result("S8", "mcp-tools", ok, 0, f"{len(tools)} tools", r.status_code)
    return _timed(_run)


def s9_well_known_card(base: str) -> Result:
    """Server discovery card must be 200 + valid JSON."""
    def _run() -> Result:
        r = _get(f"{base}/.well-known/mcp/server-card.json")
        if r.status_code != 200:
            return Result("S9", "server-card", False, 0, f"HTTP {r.status_code}", r.status_code)
        try:
            body = r.json()
        except Exception:
            return Result("S9", "server-card", False, 0, "not JSON", r.status_code)
        tools = body.get("tools") or []
        ok = isinstance(tools, list) and len(tools) >= 15
        return Result("S9", "server-card", ok, 0, f"{len(tools)} tools in card", r.status_code)
    return _timed(_run)


TESTS = {
    "S1": s1_assets,
    "S2": s2_capacity_pipeline,
    "S3": s3_ai_integrations_not_redirected,
    "S4": s4_press_releases,
    "S5": s5_testimonials,
    "S6": s6_markets,
    "S7": s7_worker_cors,
    "S8": s8_mcp_tools_list,
    "S9": s9_well_known_card,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="DC Hub live smoke tests")
    p.add_argument("--base", default=DEFAULT_BASE, help="Base URL (default: %(default)s)")
    p.add_argument("--only", nargs="+", help="Only run listed test IDs (S1..S9)")
    p.add_argument("--json", action="store_true", help="Emit JSON report")
    p.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    args = p.parse_args(argv)

    ids = args.only if args.only else list(TESTS.keys())
    report = Report()
    for tid in ids:
        fn = TESTS.get(tid)
        if not fn:
            print(f"Unknown test: {tid}", file=sys.stderr); continue
        r = fn(args.base.rstrip("/"))
        r.id = tid  # ensure id is set
        report.results.append(r)
        if not r.ok and args.fail_fast:
            break

    if args.json:
        print(report.to_json())
    else:
        print(f"Smoke tests against {args.base}")
        for r in report.results:
            tag = "✓" if r.ok else "✗"
            print(f"  {tag} [{r.id}] {r.name:<28s} {r.ms:>5d}ms  {r.detail}")
        if report.failed:
            print(f"\n{len(report.failed)}/{len(report.results)} FAILED")
        else:
            print(f"\nAll {len(report.results)} smoke tests passed ✓")

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
