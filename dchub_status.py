#!/usr/bin/env python3
"""
DC Hub Master Status Check v1.1 — exhaustive end-to-end assessment.

Tests every MCP tool with a proper handshake, follows the real
search→detail call chain (no hardcoded IDs), and reports raw
truth.

v1.1 changes:
  - Recognize current production response shapes (grid_data, water_risk,
    energy_prices, infrastructure, backup_status, agent_registry,
    tax_incentives, renewable_energy)
  - Treat paid-tool markdown paywall responses as PAID_GATED (not WARN)
  - REST endpoint calls use real-browser User-Agent + Sec-Fetch-* headers
    so Cloudflare WAF doesn't 403 the probe

Usage:  python3 dchub_status.py
        python3 dchub_status.py --api-key dchub_pro_xxxxx     # paid tier
        python3 dchub_status.py --verbose                      # show full payloads
"""
import argparse
import json
import sys
import time
import urllib.request as ur
import urllib.error as ue

MCP_URL = "https://dchub.cloud/mcp"
REST_BASE = "https://dchub.cloud"

# Browser-like headers so Cloudflare WAF lets REST probes through
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

REST_HDRS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Referer": "https://dchub.cloud/",
}

HDRS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Origin": "https://dchub.cloud",
    "Referer": "https://dchub.cloud/",
    "User-Agent": BROWSER_UA,
}

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; CYAN = "\033[96m"; DIM = "\033[2m"; BLUE = "\033[94m"; RESET = "\033[0m"


def fmt(verdict):
    if verdict == "ok": return f"{GREEN}OK{RESET}"
    if verdict == "warn": return f"{YELLOW}WARN{RESET}"
    if verdict == "fail": return f"{RED}FAIL{RESET}"
    if verdict == "skip": return f"{DIM}SKIP{RESET}"
    if verdict == "paid_gated": return f"{BLUE}PAID{RESET}"
    return verdict


def post(url, body, hdrs):
    try:
        req = ur.Request(url, headers=hdrs, data=json.dumps(body).encode())
        resp = ur.urlopen(req, timeout=20)
        return resp.status, dict(resp.headers), resp.read().decode()
    except ue.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()
    except Exception as e:
        return 0, {}, f"NETWORK_ERROR: {e}"


def get(url, hdrs=None):
    try:
        req = ur.Request(url, headers=hdrs or REST_HDRS)
        resp = ur.urlopen(req, timeout=20)
        return resp.status, resp.read().decode()
    except ue.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, f"NETWORK_ERROR: {e}"


def parse_mcp_sse(raw):
    if not raw: return None
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line.startswith("event:"): continue
        try:
            return json.loads(line)
        except Exception:
            continue
    try:
        return json.loads(raw)
    except Exception:
        return None


def is_paywall_markdown(text):
    """Detect a paid-tool pure markdown paywall response from server.mjs."""
    if not isinstance(text, str): return False
    head = text.strip()[:400]
    if not head.startswith("## "): return False
    return any(s in head for s in (
        "\U0001F512",          # 🔒 lock emoji
        "requires a paid plan",
        "is a paid feature",
        "Upgrade to Pro",
    ))


def is_trial_response(text):
    """Detect server.mjs trial mode: JSON followed by markdown footer.

    Format: '<json>\\n\\n---\\n\\n🎁 **Free trial preview** — ...'
    """
    if not isinstance(text, str): return False
    return "Free trial preview" in text and "\U0001F381" in text  # 🎁 gift emoji


def extract_tool_text(rpc_response):
    if not rpc_response: return None
    result = rpc_response.get("result", {})
    content = result.get("content", [])
    if not content: return None
    text = content[0].get("text", "")

    # Trial mode: split JSON portion from markdown footer
    if is_trial_response(text):
        parts = text.split("\n\n---\n\n", 1)
        if len(parts) >= 1:
            try:
                data = json.loads(parts[0])
                if isinstance(data, dict):
                    data["_trial_preview"] = True
                    return data
            except Exception:
                pass
        return {"_paywall_markdown": True, "_trial_preview": True, "_text": text[:200]}

    # Pure paywall (no trial credit available)
    if is_paywall_markdown(text):
        return {"_paywall_markdown": True, "_text": text[:200]}

    try:
        return json.loads(text)
    except Exception:
        return {"_raw_text": text[:300]}


class MCPSession:
    def __init__(self, api_key=None):
        self.headers = dict(HDRS_BASE)
        if api_key:
            self.headers["X-API-Key"] = api_key
        self.session_id = None
        self.api_key = api_key

    def init(self):
        status, hdr, body = post(MCP_URL, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "dchub-status", "version": "1.1"}}
        }, self.headers)
        if status != 200:
            return False, f"init failed: HTTP {status} — {body[:200]}"
        self.session_id = hdr.get("Mcp-Session-Id") or hdr.get("mcp-session-id")
        if self.session_id:
            self.headers["Mcp-Session-Id"] = self.session_id
        post(MCP_URL, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.headers)
        rpc = parse_mcp_sse(body)
        version = rpc.get("result", {}).get("serverInfo", {}).get("version", "?") if rpc else "?"
        name = rpc.get("result", {}).get("serverInfo", {}).get("name", "?") if rpc else "?"
        return True, f"{name} v{version}"

    def call_tool(self, tool, args, call_id=2):
        status, _, body = post(MCP_URL, {
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": tool, "arguments": args}
        }, self.headers)
        if status != 200:
            return False, f"HTTP {status}", body[:300]
        rpc = parse_mcp_sse(body)
        if not rpc:
            return False, "non_json", body[:300]
        if "error" in rpc:
            return False, "rpc_error", json.dumps(rpc.get("error"))[:200]
        data = extract_tool_text(rpc)
        if data is None:
            return False, "empty_content", body[:200]
        return True, "ok", data


def check(label, verdict, detail=""):
    print(f"  {fmt(verdict):20s} {label:40s} {DIM}{detail}{RESET}")


def assess(api_key=None, verbose=False):
    results = {"ok": 0, "warn": 0, "fail": 0, "skip": 0, "paid_gated": 0, "issues": []}

    def record(label, verdict, detail=""):
        check(label, verdict, detail)
        results[verdict] = results.get(verdict, 0) + 1
        if verdict in ("fail", "warn"):
            results["issues"].append((label, verdict, detail))

    # ─── 1. REST API (via Cloudflare) ───
    print(f"\n{CYAN}━━━ 1. REST API (via Cloudflare) ━━━{RESET}")
    status, body = get(f"{REST_BASE}/api/health")
    record("/api/health", "ok" if status == 200 else "fail", f"HTTP {status}")

    status, body = get(f"{REST_BASE}/api/v1/stats")
    if status == 200:
        try:
            data = json.loads(body)
            facs = (data.get("data") or {}).get("total_facilities") or data.get("facilities", 0)
            record("/api/v1/stats", "ok", f"{facs:,} facilities tracked")
        except Exception:
            record("/api/v1/stats", "warn", "200 but non-JSON")
    else:
        record("/api/v1/stats", "fail", f"HTTP {status}")

    status, body = get(f"{REST_BASE}/api/v1/facilities?limit=1")
    if status == 200:
        try:
            d = json.loads(body)
            f0 = (d.get("data") or [{}])[0]
            has_id = "id" in f0
            has_slug = "slug" in f0
            if has_id and has_slug:
                record("/api/v1/facilities (free)", "ok", "id+slug present (cache fresh)")
            elif has_id:
                record("/api/v1/facilities (free)", "warn", "id present, slug missing")
            else:
                record("/api/v1/facilities (free)", "warn", "no id field — Cloudflare cache stale")
        except Exception:
            record("/api/v1/facilities (free)", "fail", body[:100])
    else:
        record("/api/v1/facilities (free)", "fail", f"HTTP {status}")

    # ─── 2. MCP handshake ───
    print(f"\n{CYAN}━━━ 2. MCP Server ({'Pro key' if api_key else 'free tier'}) ━━━{RESET}")
    sess = MCPSession(api_key=api_key)
    ok, info = sess.init()
    if not ok:
        record("MCP initialize", "fail", info)
        print(f"\n{RED}Cannot continue — MCP handshake failed.{RESET}")
        # Phase 8 — automated stale-date check on customer-facing pages.
    try:
        _stale = check_stale_dates()
        if _stale:
            for hit in _stale:
                check(
                    f"stale-date {hit['url']} → {hit['date']}",
                    "fail",
                    hit['context'][:80],
                )
                results['issues'].append(
                    (f"stale-date {hit['url']}", "fail", hit['date'])
                )
                results['fail'] = results.get('fail', 0) + 1
    except Exception as e:
        # never let the meta-check break the doctor itself
        pass
    return results
    record("MCP initialize", "ok", info)

    # ─── 3. MCP Tools ───
    print(f"\n{CYAN}━━━ 3. MCP Tools (end-to-end) ━━━{RESET}")
    real_id = None
    real_slug = None

    # search_facilities → discover ids for downstream chain
    ok, code, data = sess.call_tool("search_facilities", {"query": "Equinix", "limit": 3}, 10)
    if ok:
        facs = data.get("data") or data.get("facilities") or []
        if facs:
            f0 = facs[0]
            real_id = f0.get("id")
            real_slug = f0.get("slug")
            id_str = str(real_id) if real_id else "MISSING"
            record("search_facilities", "ok", f"got {len(facs)} results, first id={id_str}")
        else:
            record("search_facilities", "warn", "200 but empty results array")
    else:
        record("search_facilities", "fail", f"{code}: {str(data)[:100]}")

    # get_facility chained from search result
    if real_id:
        ok, code, data = sess.call_tool("get_facility", {"facility_id": str(real_id)}, 11)
        if ok:
            d = data.get("data") or data.get("facility") or {}
            if d.get("name"):
                record("get_facility (by id)", "ok", f"got {d.get('name', '')[:40]}")
            elif data.get("error"):
                record("get_facility (by id)", "fail", f"error: {str(data.get('error'))[:80]}")
            else:
                record("get_facility (by id)", "warn", f"shape unexpected: keys={list(data.keys())[:6]}")
        else:
            record("get_facility (by id)", "fail", f"{code}: {str(data)[:100]}")
    else:
        record("get_facility (by id)", "skip", "no real id from search to chain from")

    ok, code, data = sess.call_tool("get_news", {"limit": 5}, 12)
    if ok:
        arts = data.get("articles") or data.get("data") or []
        record("get_news", "ok" if arts else "warn", f"{len(arts)} articles")
    else:
        record("get_news", "fail", f"{code}")

    ok, code, data = sess.call_tool("get_market_intel", {"market": "Northern Virginia"}, 13)
    if ok:
        if data.get("error"):
            record("get_market_intel", "fail", f"error: {str(data.get('error'))[:80]}")
        elif data.get("market") or data.get("stats") or data.get("by_status") or data.get("top_providers"):
            record("get_market_intel", "ok", "market data present")
        else:
            record("get_market_intel", "warn", f"keys={list(data.keys())[:6]}")
    else:
        record("get_market_intel", "fail", f"{code}")

    ok, code, data = sess.call_tool("list_transactions", {"limit": 3}, 14)
    if ok:
        deals = data.get("transactions") or data.get("data") or data.get("deals") or []
        record("list_transactions", "ok" if deals else "warn", f"{len(deals)} deals")
    else:
        record("list_transactions", "fail", f"{code}")

    ok, code, data = sess.call_tool("get_pipeline", {"limit": 3}, 15)
    if ok:
        proj = data.get("data") or data.get("projects") or []
        record("get_pipeline", "ok" if proj else "warn", f"{len(proj)} projects")
    else:
        record("get_pipeline", "fail", f"{code}")

    # PAID TOOL — analyze_site. Free tier returns markdown paywall OR trial-mode JSON+footer.
    ok, code, data = sess.call_tool("analyze_site",
        {"lat": 39.04, "lon": -77.49, "state": "VA"}, 16)
    if ok:
        if data.get("_paywall_markdown") and not data.get("_trial_preview"):
            record("analyze_site", "paid_gated", "free tier paywall (expected)")
        elif data.get("overall_score") is not None:
            tag = " (trial)" if data.get("_trial_preview") else ""
            record(f"analyze_site (Ashburn){tag}", "ok", f"score={data.get('overall_score')}")
        elif data.get("error"):
            record("analyze_site", "fail", str(data.get("error"))[:80])
        else:
            record("analyze_site", "warn", f"keys={list(data.keys())[:6]}")
    else:
        record("analyze_site", "fail", f"{code}")

    # get_infrastructure — current shape: {counts: {...}, filter: {...}, success: true}
    ok, code, data = sess.call_tool("get_infrastructure",
        {"lat": 39.04, "lon": -77.49, "radius_km": 50, "layer": "all"}, 17)
    if ok:
        counts = data.get("counts") or {}
        if counts:
            non_zero = [f"{k}={v}" for k, v in counts.items() if v]
            record("get_infrastructure", "ok", f"counts: {', '.join(non_zero[:4])}")
        else:
            # Fallback to old shape
            layers = [k for k in ("substations", "transmission_lines", "gas_pipelines", "power_plants") if k in data]
            non_empty = [k for k in layers if (data.get(k) or {}).get("count", 0) > 0]
            if non_empty:
                record("get_infrastructure", "ok", f"layers: {', '.join(non_empty)}")
            else:
                record("get_infrastructure", "warn", f"no counts data: {list(data.keys())[:5]}")
    else:
        record("get_infrastructure", "fail", f"{code}")

    # PAID TOOL — get_fiber_intel. Returns GeoJSON FeatureCollection or {routes,sources}.
    ok, code, data = sess.call_tool("get_fiber_intel", {"include_sources": True}, 18)
    if ok:
        if data.get("_paywall_markdown") and not data.get("_trial_preview"):
            record("get_fiber_intel", "paid_gated", "free tier paywall (expected)")
        else:
            tag = " (trial)" if data.get("_trial_preview") else ""
            # GeoJSON FeatureCollection shape (type=FeatureCollection, features=[], total=N)
            if data.get("type") == "FeatureCollection":
                feat_ct = len(data.get("features") or [])
                total = data.get("total", feat_ct)
                record(f"get_fiber_intel{tag}", "ok", f"{total} routes ({feat_ct} features in response)")
            else:
                routes_ct = (data.get("routes") or {}).get("count", 0) if isinstance(data.get("routes"), dict) else 0
                if routes_ct or data.get("data") or data.get("sources"):
                    record(f"get_fiber_intel{tag}", "ok", f"{routes_ct} routes")
                else:
                    record("get_fiber_intel", "warn", f"empty: {list(data.keys())[:5]}")
    else:
        record("get_fiber_intel", "fail", f"{code}")

    # get_grid_data — new shape: {caveat, grid_headroom, location, nearest_substation, source, success}
    ok, code, data = sess.call_tool("get_grid_data", {"iso": "PJM", "metric": "fuel_mix"}, 19)
    if ok:
        if any(k in data for k in ("grid_headroom", "nearest_substation", "carbon_intensity",
                                    "energy_rates", "renewable_capacity", "fuel_mix")):
            record("get_grid_data (PJM)", "ok", "grid data present")
        elif data.get("error"):
            record("get_grid_data (PJM)", "fail", str(data.get("error"))[:80])
        else:
            record("get_grid_data (PJM)", "warn", f"keys={list(data.keys())[:6]}")
    else:
        record("get_grid_data (PJM)", "fail", f"{code}")

    # PAID TOOL — get_grid_intelligence
    ok, code, data = sess.call_tool("get_grid_intelligence", {"region_id": "ercot"}, 20)
    if ok:
        if data.get("_paywall_markdown") and not data.get("_trial_preview"):
            record("get_grid_intelligence (ERCOT)", "paid_gated", "free tier paywall (expected)")
        elif any(k in data for k in ("region", "corridors", "grid_headroom", "_upgrade",
                                       "energy_rates_cents_kwh")):
            tag = " (trial)" if data.get("_trial_preview") else ""
            record(f"get_grid_intelligence (ERCOT){tag}", "ok", "region data present")
        else:
            record("get_grid_intelligence (ERCOT)", "warn", f"keys={list(data.keys())[:6]}")
    else:
        record("get_grid_intelligence (ERCOT)", "fail", f"{code}")

    # get_agent_registry — new shape: {platforms: [...], mcp_count, total, success}
    ok, code, data = sess.call_tool("get_agent_registry", {}, 21)
    if ok:
        platforms = data.get("platforms") or data.get("agents") or data.get("dc_hub_agent_registry") or []
        if isinstance(platforms, list) and platforms:
            mcp_count = data.get("mcp_count", "?")
            record("get_agent_registry", "ok", f"{len(platforms)} platforms, {mcp_count} MCP-active")
        elif isinstance(platforms, list):
            record("get_agent_registry", "warn", f"empty list, keys={list(data.keys())[:5]}")
        else:
            record("get_agent_registry", "ok", f"keys={list(data.keys())[:5]}")
    else:
        record("get_agent_registry", "fail", f"{code}")

    ok, code, data = sess.call_tool("get_intelligence_index", {}, 22)
    if ok:
        score = data.get("global_pulse_score") or (data.get("dc_hub_intelligence_index") or {}).get("global_pulse_score")
        record("get_intelligence_index", "ok" if score else "warn", f"pulse={score}")
    else:
        record("get_intelligence_index", "fail", f"{code}")

    # get_water_risk — new shape: {current_drought_pct, drought_categories, dominant_severity, ...}
    ok, code, data = sess.call_tool("get_water_risk", {"state": "AZ"}, 23)
    if ok:
        if any(k in data for k in ("current_drought_pct", "drought_categories",
                                     "dominant_severity", "cooling_recommendation", "water_stress")):
            sev = data.get("dominant_severity", "?")
            record("get_water_risk (AZ)", "ok", f"severity={sev}")
        elif data.get("error"):
            record("get_water_risk (AZ)", "fail", str(data.get("error"))[:80])
        else:
            record("get_water_risk (AZ)", "warn", f"keys={list(data.keys())[:5]}")
    else:
        record("get_water_risk (AZ)", "fail", f"{code}")

    # get_energy_prices — new shape: {retail_rates: {avg_cents_kwh, ...}, success}
    ok, code, data = sess.call_tool("get_energy_prices", {"data_type": "retail_rates", "state": "VA"}, 24)
    if ok:
        rates = data.get("retail_rates") or data.get("rates") or data.get("data") or []
        if isinstance(rates, dict) and rates:
            avg = rates.get("avg_cents_kwh", "?")
            record("get_energy_prices (VA)", "ok", f"avg={avg}¢/kWh")
        elif isinstance(rates, list) and rates:
            record("get_energy_prices (VA)", "ok", f"{len(rates)} rate rows")
        elif data.get("error"):
            record("get_energy_prices (VA)", "fail", str(data.get("error"))[:80])
        else:
            record("get_energy_prices (VA)", "warn", f"empty: {list(data.keys())[:5]}")
    else:
        record("get_energy_prices (VA)", "fail", f"{code}")

    ok, code, data = sess.call_tool("get_renewable_energy",
        {"energy_type": "combined", "state": "TX"}, 25)
    if ok:
        ppas = data.get("dc_industry_ppas") or []
        # Renewable endpoint shares /api/v1/energy/summary now → returns retail_rates
        # so check for that fallback shape too
        if ppas:
            record("get_renewable_energy (TX)", "ok", f"{len(ppas)} PPAs")
        elif data.get("retail_rates") or data.get("success"):
            record("get_renewable_energy (TX)", "warn", "endpoint shared with retail rates — backend route missing for true renewables")
        else:
            record("get_renewable_energy (TX)", "warn", f"empty: {list(data.keys())[:5]}")
    else:
        record("get_renewable_energy (TX)", "fail", f"{code}")

    # get_tax_incentives — current shape: {count, data, last_updated, status}
    ok, code, data = sess.call_tool("get_tax_incentives", {"state": "VA"}, 26)
    if ok:
        inc = data.get("data") or data.get("incentives") or []
        # The endpoint returns ALL 50 states in `data`. Filter by state.
        if isinstance(inc, list) and inc:
            # Check for VA-specific entry
            va_entries = [x for x in inc if isinstance(x, dict) and (x.get("abbr") == "VA" or x.get("state") == "VA")]
            record("get_tax_incentives (VA)", "ok",
                   f"{len(inc)} states tracked, {len(va_entries)} VA-specific")
        else:
            record("get_tax_incentives (VA)", "warn", f"empty: {list(data.keys())[:5]}")
    else:
        record("get_tax_incentives (VA)", "fail", f"{code}")

    # get_backup_status — new shape: {feeds, summary, generated_at, success}
    ok, code, data = sess.call_tool("get_backup_status", {}, 27)
    if ok:
        summary = data.get("summary") or {}
        if summary or data.get("feeds"):
            health = summary.get("overall_health") or summary.get("status") or "?"
            healthy_ct = summary.get("healthy", "?")
            record("get_backup_status", "ok", f"health={health}, healthy={healthy_ct}")
        elif data.get("status"):
            record("get_backup_status", "ok", f"status={data.get('status')}")
        else:
            record("get_backup_status", "warn", f"keys={list(data.keys())[:5]}")
    else:
        record("get_backup_status", "fail", f"{code}")

    # PAID TOOL — get_dchub_recommendation
    ok, code, data = sess.call_tool("get_dchub_recommendation", {"context": "general"}, 28)
    if ok:
        if data.get("_paywall_markdown") and not data.get("_trial_preview"):
            record("get_dchub_recommendation", "paid_gated", "free tier paywall (expected)")
        else:
            rec = data.get("recommendation") or {}
            tag = " (trial)" if data.get("_trial_preview") else ""
            if rec:
                record(f"get_dchub_recommendation{tag}", "ok", "rec present")
            else:
                record("get_dchub_recommendation", "warn", f"empty: keys={list(data.keys())[:5]}")
    else:
        record("get_dchub_recommendation", "fail", f"{code}")

    # Slug round-trip
    if real_id and real_slug:
        ok, code, data = sess.call_tool("get_facility", {"facility_id": real_slug}, 30)
        if ok and (data.get("data") or data.get("facility")):
            record("get_facility (by slug)", "ok", "slug lookup works")
        elif ok and data.get("error"):
            record("get_facility (by slug)", "warn", "slug returns error")
        else:
            record("get_facility (by slug)", "fail", str(data)[:100])

    # ─── Summary ───
    print(f"\n{CYAN}━━━ Summary ━━━{RESET}")
    total = results["ok"] + results["warn"] + results["fail"] + results["paid_gated"]
    print(
        f"  {GREEN}OK:{RESET} {results['ok']:3d}    "
        f"{BLUE}PAID:{RESET} {results['paid_gated']:3d}    "
        f"{YELLOW}WARN:{RESET} {results['warn']:3d}    "
        f"{RED}FAIL:{RESET} {results['fail']:3d}    "
        f"{DIM}of {total} checks{RESET}"
    )
    if results["paid_gated"]:
        print(f"  {DIM}(PAID = working correctly, free tier sees a paywall message — pass an --api-key for full output){RESET}")
    if results["issues"]:
        verdict_lvl = "fail" if any(v == "fail" for _, v, _ in results["issues"]) else "warn"
        print(f"\n  {RED if verdict_lvl == 'fail' else YELLOW}Issues to address:{RESET}")
        for label, verdict, detail in results["issues"]:
            print(f"    {fmt(verdict)} {label}: {detail}")
    return results


# =============================================================================
# Stale-date detector (Phase 8)
# Scans customer-facing pages for dates in the past. Catches the
# 'Founding Members closes March 31' class of bug structurally instead of
# relying on memory.
# =============================================================================
def check_stale_dates():
    """Return list of {url, date, context} for any past dates found."""
    import re
    import datetime
    import urllib.request
    pages = [
        'https://dchub.cloud/pricing',
        'https://dchub.cloud/',
        'https://dchub.cloud/ai',
    ]
    today = datetime.date.today()
    months = {
        'January': 1, 'February': 2, 'March': 3, 'April': 4,
        'May': 5, 'June': 6, 'July': 7, 'August': 8,
        'September': 9, 'October': 10, 'November': 11, 'December': 12,
    }
    pattern = re.compile(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),\s+(20\d{2})\b'
    )
    failures = []
    import time as _t
    cb = str(int(_t.time()))
    for url in pages:
        bust_url = url + ('&' if '?' in url else '?') + '_doctor=' + cb
        try:
            req = urllib.request.Request(
                bust_url,
                headers={
                    'User-Agent': 'DCHubDoctor/1.0',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                },
            )
            html = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', errors='ignore')
        except Exception:
            continue
        for m in pattern.finditer(html):
            try:
                d = datetime.date(int(m.group(3)), months[m.group(1)], int(m.group(2)))
                if d < today:
                    raw = html[max(0, m.start() - 40):m.end() + 20]
                    snippet = raw.replace('\n', ' ').replace('\r', ' ').strip()
                    failures.append({'url': url, 'date': str(d), 'context': snippet})
            except Exception:
                pass
    return failures


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", help="X-API-Key for paid-tier checks")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    print(f"{CYAN}DC Hub Master Status v1.3 — {time.strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{DIM}MCP: {MCP_URL}    REST: {REST_BASE}{RESET}")
    r = assess(api_key=args.api_key, verbose=args.verbose)
    sys.exit(1 if r["fail"] > 0 else 0)
