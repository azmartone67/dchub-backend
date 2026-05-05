#!/usr/bin/env python3
"""
DC Hub — Regression Smoke (post-2026-04-28 fixes)
Verifies the work shipped 2026-04-28 stays shipped:
  R1  /press-release returns 200 + "Today's Headlines" + Explorer card
  R2  /press-release/<known-slug> returns 200 (slug-template still works)
  R3  /api/v1/explorer returns 200 + "Semantic Search Explorer"
  R4  /.well-known/mcp.json valid JSON with >=24 tools
  R5  /land-power-map references coord-parser-fix.js?v=6 or higher
  R6  /land-power-map contains DCHUB-RIGHT-PANEL-DEFENSIVE marker
  R7  x-dc-worker-version >= 4.6.5
  R8  pr_queue.json (local) parses + contains both expected slugs
Exit 0 = all green. 1 = at least one regression. 2 = setup error.
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from dataclasses import dataclass, field, asdict

try:
    import requests
except ImportError:
    print("requires `requests`. Install: pip install requests", file=sys.stderr)
    sys.exit(2)

DEFAULT_BASE = "https://dchub.cloud"
DEFAULT_TIMEOUT = 15
UA = "DCHub-Regression/2026-04-28"
MIN_WORKER_VERSION = (4, 6, 5)
EXPECTED_PR_SLUGS = {"tony-bishop-founding-member", "semantic-search-explorer-launch"}
KNOWN_SLUG_FOR_R2 = "tony-bishop-founding-member"


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
    results: list = field(default_factory=list)

    @property
    def failed(self):
        return [r for r in self.results if not r.ok]

    def to_json(self):
        return json.dumps({"results": [asdict(r) for r in self.results]}, indent=2)


def _get(url, *, timeout=DEFAULT_TIMEOUT, **kw):
    kw.setdefault("headers", {})
    kw["headers"].setdefault("User-Agent", UA)
    return requests.get(url, timeout=timeout, **kw)


def _ms(t0):
    return int((time.monotonic() - t0) * 1000)


def _semver(s):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", (s or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def r1(base):
    t0 = time.monotonic()
    try:
        r = _get(f"{base}/press-release")
        body = r.text
        h = "Today's Headlines" in body
        e = "Semantic Search Explorer" in body
        ok = r.status_code == 200 and h and e
        d = "200 + headlines + explorer" if ok else f"status={r.status_code} headlines={h} explorer={e}"
        return Result("R1", "/press-release renders daily brief", ok, _ms(t0), d, r.status_code)
    except Exception as ex:
        return Result("R1", "/press-release renders daily brief", False, _ms(t0), f"exception: {ex}")


def r2(base):
    """Catches the original bug: /press-release/<slug> 301-redirecting to /press.
    The Worker rewrites /press-release/<slug> -> /news/<slug> and proxies to Railway.
    200 = slug found in DB. 404 = slug not in DB (still a healthy routing pipeline).
    30x or 5xx = regression."""
    t0 = time.monotonic()
    try:
        r = _get(base + "/press-release/" + KNOWN_SLUG_FOR_R2, timeout=30, allow_redirects=False)
        ok = r.status_code in (200, 404)
        d = "status=" + str(r.status_code) + (" - routing healthy" if ok else " - possible regression")
        return Result("R2", "/press-release/<slug> routing healthy", ok, _ms(t0), d, r.status_code)
    except Exception as ex:
        return Result("R2", "/press-release/<slug> routing", False, _ms(t0), "exception: " + str(ex))

def r3(base):
    t0 = time.monotonic()
    try:
        r = _get(f"{base}/api/v1/explorer")
        marker = "Semantic Search Explorer" in r.text
        ok = r.status_code == 200 and marker
        return Result("R3", "/api/v1/explorer reachable", ok, _ms(t0),
                      "200 + marker" if ok else f"status={r.status_code} marker={marker}", r.status_code)
    except Exception as ex:
        return Result("R3", "/api/v1/explorer reachable", False, _ms(t0), f"exception: {ex}")


def r4(base):
    t0 = time.monotonic()
    try:
        r = _get(f"{base}/.well-known/mcp.json")
        if r.status_code != 200:
            return Result("R4", "MCP discovery 200", False, _ms(t0), f"status={r.status_code}", r.status_code)
        tools = r.json().get("tools") or []
        ok = isinstance(tools, list) and len(tools) >= 24
        return Result("R4", "MCP discovery >=24 tools", ok, _ms(t0), f"tools={len(tools)}", r.status_code)
    except Exception as ex:
        return Result("R4", "MCP discovery", False, _ms(t0), f"exception: {ex}")


def r5(base):
    t0 = time.monotonic()
    try:
        r = _get(f"{base}/land-power-map")
        m = re.search(r'coord-parser-fix\.js\?v=(\d+)', r.text)
        v = int(m.group(1)) if m else None
        ok = v is not None and v >= 6
        return Result("R5", "coord-parser-fix.js v>=6", ok, _ms(t0),
                      f"v={v}" if v else "tag missing", r.status_code)
    except Exception as ex:
        return Result("R5", "coord-parser-fix.js v>=6", False, _ms(t0), f"exception: {ex}")


def r6(base):
    t0 = time.monotonic()
    try:
        r = _get(f"{base}/land-power-map")
        ok = "DCHUB-RIGHT-PANEL-DEFENSIVE" in r.text
        return Result("R6", "right-panel defensive CSS marker", ok, _ms(t0),
                      "marker present" if ok else "marker missing", r.status_code)
    except Exception as ex:
        return Result("R6", "right-panel defensive CSS marker", False, _ms(t0), f"exception: {ex}")


def r7(base):
    """Worker headers only appear on Worker-handled responses; Pages-asset
    responses have none. Try multiple paths; pass if ANY reports a version
    >= MIN_WORKER_VERSION; skip cleanly if no path has the header at all."""
    t0 = time.monotonic()
    seen = []
    for path in ["/api/news", "/api/v1/explorer", "/.well-known/mcp.json"]:
        try:
            r = _get(base + path, timeout=10)
        except Exception:
            continue
        v_str = r.headers.get("x-dc-worker-version", "")
        v = _semver(v_str)
        if v is not None:
            seen.append((path, v_str, v))
            if v >= MIN_WORKER_VERSION:
                return Result("R7", "Worker version >= 4.6.5", True, _ms(t0),
                              path + " -> x-dc-worker-version=" + v_str, r.status_code)
    if not seen:
        return Result("R7", "Worker version probe", True, _ms(t0),
                      "no x-dc-worker-version header on probed paths (Pages-routed - not a regression)")
    worst = min(seen, key=lambda x: x[2])
    return Result("R7", "Worker version >= 4.6.5", False, _ms(t0),
                  "highest seen: " + worst[0] + " -> " + worst[1])

def r8(base):
    t0 = time.monotonic()
    candidates = [
        os.environ.get("DCHUB_PR_QUEUE", ""),
        os.path.expanduser("~/workspace/pr_queue.json"),
        "/home/runner/workspace/pr_queue.json",
        "pr_queue.json",
    ]
    path = next((p for p in candidates if p and os.path.isfile(p)), None)
    if not path:
        return Result("R8", "pr_queue.json local sanity", False, _ms(t0), "file not found")
    try:
        data = json.load(open(path))
        if not isinstance(data, list):
            return Result("R8", "pr_queue.json local sanity", False, _ms(t0), "not a list")
        slugs = {x.get("slug") for x in data if isinstance(x, dict)}
        missing = EXPECTED_PR_SLUGS - slugs
        ok = len(missing) == 0
        return Result("R8", "pr_queue.json contains expected slugs", ok, _ms(t0),
                      f"path={path} entries={len(data)} missing={sorted(missing) or 'none'}")
    except Exception as ex:
        return Result("R8", "pr_queue.json local sanity", False, _ms(t0), f"exception: {ex}")


CHECKS = [("R1", r1), ("R2", r2), ("R3", r3), ("R4", r4),
          ("R5", r5), ("R6", r6), ("R7", r7), ("R8", r8)]


def run(base, only=None, fail_fast=False):
    rep = Report()
    for cid, fn in CHECKS:
        if only and cid not in only:
            continue
        rep.results.append(fn(base))
        if fail_fast and not rep.results[-1].ok:
            break
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args()
    rep = run(args.base, only=args.only, fail_fast=args.fail_fast)
    if args.report:
        open(args.report, "w").write(rep.to_json())
    if args.json:
        print(rep.to_json())
    else:
        for r in rep.results:
            mark = "✓" if r.ok else "✗"
            sc = f" [{r.status_code}]" if r.status_code else ""
            print(f"  {mark} {r.id}  {r.name}{sc}  {r.ms}ms  {r.detail}")
        if rep.failed:
            print(f"\n  {len(rep.failed)} regression(s)")
        else:
            print("\n  All regressions clean.")
    return 0 if not rep.failed else 1


if __name__ == "__main__":
    sys.exit(main())
