#!/usr/bin/env python3
"""
DC Hub — Generate per-market HTML pages from markets/registry.json.

Each generated file is a THIN wrapper (~45 lines) containing:
  - Full SEO meta + OpenGraph + JSON-LD breadcrumb/CollectionPage
  - A <meta name="market-slug"> the renderer reads
  - A <noscript> fallback with static description so crawlers see content
  - Links to the shared CSS + renderer JS

All volatile data (MW, $/kWh, vacancy, operators, pipeline, news) is
fetched at runtime by /js/market-page.js from /api/v1/* endpoints.

Usage:   python3 scripts/generate-market-pages.py
Output:  markets/<slug>.html  for every market in the registry
"""
import json, pathlib, sys, html as H, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
REG  = ROOT / "markets" / "registry.json"
OUT  = ROOT / "markets"

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="market-slug" content="{slug}">
<title>{name} Data Center Market | Live Intelligence | DC Hub</title>
<meta name="description" content="{meta_desc}">
<meta name="keywords" content="{keywords}">
<link rel="canonical" href="https://dchub.cloud/markets/{slug}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/css/market-page.css">
<meta property="og:title" content="{name} Data Center Market | DC Hub">
<meta property="og:description" content="{og_desc}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://dchub.cloud/markets/{slug}">
<meta property="og:image" content="https://dchub.cloud/og-default.png">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{jsonld}</script>
<script src="/js/dchub-nav.js" defer></script>
</head>
<body>
<nav class="dchub-nav"><div class="ni" style="max-width:1200px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;height:60px;padding:0 2rem"><a href="/" style="font-size:1.25rem;font-weight:700;color:#fafafa;text-decoration:none">DC <span style="color:#3b82f6">Hub</span></a><ul style="display:flex;gap:1.5rem;list-style:none;margin:0;padding:0"><li><a href="/" style="color:#8a8a95;text-decoration:none;font-size:.9rem">Map</a></li><li><a href="/land-power" style="color:#8a8a95;text-decoration:none;font-size:.9rem">Land &amp; Power</a></li><li><a href="/markets/" style="color:#8a8a95;text-decoration:none;font-size:.9rem">Markets</a></li><li><a href="/gdci" style="color:#8a8a95;text-decoration:none;font-size:.9rem">GDCI</a></li><li><a href="/research/grid-intelligence/" style="color:#8a8a95;text-decoration:none;font-size:.9rem">Grid Intel</a></li><li><a href="/pricing" style="color:#8a8a95;text-decoration:none;font-size:.9rem">Pricing</a></li></ul></div></nav>

<main id="market-container">
  <noscript>
    <div class="bc"><a href="/">DC Hub</a> › <a href="/markets/">Markets</a> › {name}</div>
    <div class="mp-hero">
      <div class="mp-ht"><span class="fl">{flag}</span><h1>{name}</h1><span class="bdg" style="background:rgba(59,130,246,0.12);color:#3b82f6">{tier_label}</span></div>
      <div class="tl">{tagline}</div>
      <div class="desc">{description}</div>
      <p style="color:#f59e0b">Enable JavaScript to load live market intelligence (facilities, grid status, energy pricing, pipeline, news).</p>
    </div>
  </noscript>
</main>

<footer>
  © 2026 DC Hub · Live data via DC Hub MCP · <a href="/about">About</a> · <a href="/api-docs">API</a> · <a href="/faq">FAQ</a> · <a href="/glossary">Glossary</a>
</footer>

<script src="/js/market-page.js" defer></script>
</body>
</html>
"""

def build_jsonld(slug, m, tier_label):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "Article",
        "name": f"{m['name']} Data Center Market",
        "headline": f"{m['name']} — {m['tagline']}",
        "description": m.get("description", ""),
        "url": f"https://dchub.cloud/markets/{slug}",
        "about": {
            "@type": "Place",
            "name": m["name"],
            "geo": {"@type": "GeoCoordinates", "latitude": m["lat"], "longitude": m["lon"]},
            "addressCountry": m.get("country", ""),
        },
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "breadcrumb": {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type":"ListItem","position":1,"name":"DC Hub","item":"https://dchub.cloud"},
                {"@type":"ListItem","position":2,"name":"Markets","item":"https://dchub.cloud/markets/"},
                {"@type":"ListItem","position":3,"name":m["name"],"item":f"https://dchub.cloud/markets/{slug}"},
            ],
        },
        "keywords": ", ".join(filter(None, [
            "data center", m["name"], m.get("state",""), m.get("country",""), m.get("iso",""), tier_label,
            "colocation","hyperscale","power capacity","site selection","AI infrastructure",
        ])),
    }, separators=(",", ":"))

def build_meta_desc(m):
    bits = [f"{m['name']} data center market"]
    bits.append(m["tagline"])
    if m.get("operators"):
        bits.append("Operators: " + ", ".join(m["operators"][:5]))
    bits.append("Live intelligence: facilities, grid, energy pricing, pipeline, news.")
    out = " · ".join(bits)
    return (out[:157] + "…") if len(out) > 160 else out

def build_keywords(m, tier_label):
    words = [
        f"{m['name']} data center",
        f"{m['name']} colocation",
        f"{m['name']} data center market",
        tier_label.lower() if tier_label else "",
        m.get("state","").lower(),
        m.get("country","").lower(),
        m.get("iso","").lower(),
    ] + [f"{op.lower()} {m['name'].lower()}" for op in (m.get("operators") or [])[:3]]
    return ", ".join(w for w in words if w)

def main():
    if not REG.exists():
        print(f"registry missing: {REG}", file=sys.stderr); sys.exit(1)
    data = json.loads(REG.read_text())
    markets = data["markets"]
    tiers = data.get("tiers", {})
    count = 0
    for slug, m in markets.items():
        tier_label = tiers.get(m.get("tier",""), {}).get("label", m.get("tier","").replace("-"," ").title())
        html_str = TEMPLATE.format(
            slug=slug,
            name=H.escape(m["name"]),
            flag=m.get("flag",""),
            tagline=H.escape(m.get("tagline","")),
            description=H.escape(m.get("description","")),
            tier_label=H.escape(tier_label),
            meta_desc=H.escape(build_meta_desc(m)),
            og_desc=H.escape(m.get("tagline","") + " — " + (m.get("description","")[:140])),
            keywords=H.escape(build_keywords(m, tier_label)),
            jsonld=build_jsonld(slug, m, tier_label),
        )
        out_path = OUT / f"{slug}.html"
        out_path.write_text(html_str)
        count += 1
    print(f"generated {count} market pages in {OUT}")
    print(f"timestamp: {datetime.datetime.utcnow().isoformat()}Z")

if __name__ == "__main__":
    main()
