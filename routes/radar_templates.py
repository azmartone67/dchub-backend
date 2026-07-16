"""
grid_radar_templates.py — the _tpl binding layer for grid_radar_daily.py.

    pull_core()  ->  normalize()  ->  render(template, data, tier)  ->  html

Portable + testable: NO repo needed. Run `python3 grid_radar_templates.py` to
self-test — it renders a demo edition from a sample core (the real 07-16 numbers)
and asserts both tiers.

TO BIND THE 5 REAL EDITION HTML FILES (mechanical, one-time):
  1. Replace hardcoded values with {{tokens}} — e.g. 1,737 -> {{us_queue_gw}}.
     (see TOKENS for the full set.)
  2. Wrap paid-only blocks in  <!--FULL--> ... <!--/FULL-->  and the anonymous
     substitutes in  <!--TEASE--> ... <!--/TEASE-->.
  3. render(open(file).read(), data, tier) yields the final HTML for that tier.
The engine below (normalize + render + tier gating) is the reusable part; the
templates are just the 5 files you already have.
"""
from __future__ import annotations
import re, html, os

# ---- 1. normalize: raw MCP responses -> flat token data ---------------------
US_ISOS = ["SPP", "CAISO", "MISO", "ERCOT", "ISO-NE", "NYISO", "PJM"]

def _num(v):
    try: return round(float(v), 2)
    except (TypeError, ValueError): return None

def _int(v):
    try: return int(round(float(v)))
    except (TypeError, ValueError): return None

def normalize(core: dict) -> dict:
    """Map DC Hub's live response envelope onto the flat fields the templates use."""
    sb = core.get("scoreboard", {}) or {}
    grids = {g.get("iso"): g for g in sb.get("grids", [])}
    isos = []
    for code in US_ISOS:
        g = grids.get(code, {}) or {}
        d = g.get("dcpi_detail", {}) or {}
        q = g.get("interconnection_queue", {}) or {}
        isos.append({
            "iso": code,
            "ren": _num(g.get("renewable_share_pct")),
            "queue_gw": _num(q.get("queued_gw")),
            "wait_mo": _num(d.get("avg_queue_wait_months")),
            "curtail": _num(d.get("avg_curtailment_pct")),
            "build_markets": d.get("build_markets", 0),
        })
    by = {r["iso"]: r for r in isos}

    ash = core.get("ashburn", {}) or {}
    markets = (core.get("markets", {}) or {}).get("results", [])
    mk = {(m.get("city") or m.get("name", "")): m for m in markets}
    va = [mk.get(c, {}) for c in ("Ashburn", "Sterling", "Manassas")]

    data = {
        "retrieved_at":       core.get("retrieved_at", ""),
        "depth":              core.get("depth", "free"),
        "us_queue_gw":        _int(sb.get("us_interconnection_queue_gw")),
        "ashburn_load_mw":    _int(ash.get("demand_mw")),
        "ashburn_lmp":        _num(ash.get("lmp_rt_usd_mwh")),
        "ashburn_congestion": _num(ash.get("lmp_congestion_usd_mwh")),
        "pjm_wait_mo":        by["PJM"]["wait_mo"],
        "spp_curtail":        by["SPP"]["curtail"],
        "ercot_queue_gw":     by["ERCOT"]["queue_gw"],
        "va_facilities":      sum(int(m.get("facility_count", 0) or 0) for m in va),
        "va_mw":              sum(int(m.get("total_mw", 0) or 0) for m in va),
        "va_operators":       sum(int(m.get("operator_count", 0) or 0) for m in va),
        "isos":               isos,        # for the scoreboard / quadrant loops
        "markets":            markets[:10],
    }
    # deep-only (paid key): ERCOT large-load queue, if the deep tool returned
    q = core.get("queue")
    data["ercot_largeload_gw"] = _int((q or {}).get("queued_load_data_center_gw")) if q else None

    # live chart data injected raw into <script> as {{{isos_json}}}
    import json as _j
    data["isos_json"] = _j.dumps({r["iso"]: {"wait": r["wait_mo"], "curtail": r["curtail"],
                                              "queue": r["queue_gw"], "ren": r["ren"]} for r in isos})
    ra = data["retrieved_at"] or ""
    data["retrieved_date"] = ra[:10]
    m = re.search(r"T(\d{2}:\d{2})", ra)
    data["retrieved_utc"] = f"{m.group(1)} UTC" if m else ""
    return data

TOKENS = ("us_queue_gw pjm_wait_mo spp_curtail ercot_queue_gw ashburn_load_mw "
          "ashburn_lmp ashburn_congestion va_facilities va_mw va_operators "
          "ercot_largeload_gw retrieved_at").split()

# ---- 2. render: {{token}} substitution + tier gating ------------------------
def _fmt(v) -> str:
    if v is None: return "—"                 # em dash for a missing value
    if isinstance(v, int):   return f"{v:,}"
    if isinstance(v, float): return f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)

def apply_tier(template: str, tier: str) -> str:
    """Keep FULL blocks only for paid; keep TEASE blocks only for anon/agent."""
    keep, drop = ("FULL", "TEASE") if tier == "full" else ("TEASE", "FULL")
    template = re.sub(rf"<!--{drop}-->.*?<!--/{drop}-->", "", template, flags=re.S)
    return template.replace(f"<!--{keep}-->", "").replace(f"<!--/{keep}-->", "")

def _raw(v) -> str:
    import json as _j
    if v is None: return "null"
    return v if isinstance(v, str) else _j.dumps(v)

def render(template: str, data: dict, tier: str = "full") -> str:
    out = apply_tier(template, tier)
    # raw triple-brace tokens (NOT escaped) — for JSON injected into <script>
    out = re.sub(r"\{\{\{\s*([a-z0-9_]+)\s*\}\}\}", lambda m: _raw(data.get(m.group(1))), out)
    # escaped double-brace tokens — for HTML text
    return re.sub(r"\{\{\s*([a-z0-9_]+)\s*\}\}",
                  lambda m: html.escape(_fmt(data.get(m.group(1)))), out)

def render_edition(slug: str, core: dict, tier: str, template_dir: str) -> str:
    """The _tpl entrypoint: load <slug>.html, normalize the core, render the tier."""
    data = normalize(core)
    with open(os.path.join(template_dir, f"{slug}.html"), encoding="utf-8") as f:
        return render(f.read(), data, tier)


# ---- self-test (real 07-16 numbers) ----------------------------------------
if __name__ == "__main__":
    sample_core = {
        "retrieved_at": "2026-07-16T07:25:00+00:00", "depth": "full",
        "scoreboard": {
            "us_interconnection_queue_gw": 1737.3,
            "grids": [
                {"iso": "SPP",    "renewable_share_pct": 33.6, "interconnection_queue": {"queued_gw": 187.2},
                 "dcpi_detail": {"avg_queue_wait_months": 24.0, "avg_curtailment_pct": 11.2, "build_markets": 2}},
                {"iso": "ERCOT",  "renewable_share_pct": 13.8, "interconnection_queue": {"queued_gw": 434.1},
                 "dcpi_detail": {"avg_queue_wait_months": 32.6, "avg_curtailment_pct": 4.3, "build_markets": 0}},
                {"iso": "PJM",    "renewable_share_pct": 2.8,  "interconnection_queue": {"queued_gw": 172.4},
                 "dcpi_detail": {"avg_queue_wait_months": 50.5, "avg_curtailment_pct": 1.2, "build_markets": 0}},
                {"iso": "MISO",   "renewable_share_pct": 6.8,  "interconnection_queue": {"queued_gw": 191.1},
                 "dcpi_detail": {"avg_queue_wait_months": 33.7, "avg_curtailment_pct": 6.2, "build_markets": 1}},
                {"iso": "CAISO",  "renewable_share_pct": 28.5, "interconnection_queue": {"queued_gw": 79.5},
                 "dcpi_detail": {"avg_queue_wait_months": 39.9, "avg_curtailment_pct": 9.0, "build_markets": 0}},
                {"iso": "NYISO",  "renewable_share_pct": 19.4, "interconnection_queue": {"queued_gw": 10.6},
                 "dcpi_detail": {"avg_queue_wait_months": 31.4, "avg_curtailment_pct": 2.0, "build_markets": 0}},
                {"iso": "ISO-NE", "renewable_share_pct": 10.7, "interconnection_queue": {"queued_gw": 14.8},
                 "dcpi_detail": {"avg_queue_wait_months": 33.3, "avg_curtailment_pct": 3.3, "build_markets": 0}},
            ],
        },
        "ashburn": {"demand_mw": 16295.57, "lmp_rt_usd_mwh": 36.94, "lmp_congestion_usd_mwh": 3.81},
        "markets": {"results": [
            {"city": "Ashburn",  "state": "VA", "facility_count": 176, "operator_count": 50, "total_mw": 6843},
            {"city": "Sterling", "state": "VA", "facility_count": 99,  "operator_count": 17, "total_mw": 2968},
            {"city": "Manassas", "state": "VA", "facility_count": 64,  "operator_count": 22, "total_mw": 1685},
        ]},
    }

    DEMO = (
        "<h1>{{us_queue_gw}} GW queued</h1>"
        "<p>Ashburn {{ashburn_load_mw}} MW &middot; ${{ashburn_lmp}}/MWh &middot; wait {{pjm_wait_mo}} mo</p>"
        "<!--FULL--><section>SPP curtailment {{spp_curtail}}% &middot; NoVA {{va_mw}} MW / {{va_facilities}} facilities</section><!--/FULL-->"
        "<!--TEASE--><section>Locked &middot; claim_free_key -&gt; unlock_more_data</section><!--/TEASE-->"
    )

    d = normalize(sample_core)
    assert d["us_queue_gw"] == 1737 and d["pjm_wait_mo"] == 50.5, d
    assert d["va_mw"] == 11496 and d["va_facilities"] == 339, d
    assert d["ashburn_lmp"] == 36.94, d
    assert len(d["isos"]) == 7, d

    full  = render(DEMO, d, "full")
    tease = render(DEMO, d, "tease")
    assert "1,737 GW" in full and "16,296 MW" in full and "$36.94" in full
    assert "SPP curtailment 11.2%" in full and "11,496 MW" in full   # FULL-only survives
    assert "claim_free_key" not in full
    assert "1,737 GW" in tease                                        # hook shown to anon
    assert "SPP curtailment" not in tease and "11,496" not in tease   # deep read locked
    assert "claim_free_key" in tease                                  # agent unlock path shown
    print("normalize + render + tier gating: ALL ASSERTIONS PASS")
    print("  full  :", full.split("</p>")[0].split("<p>")[-1])
    print("  gated :", "FULL section present in full, absent in tease; TEASE CTA vice-versa")
