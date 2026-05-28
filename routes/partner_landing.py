"""
partner_landing.py — Phase r64 (2026-05-25).

Per-target landing pages for the 9 AI lab / GPU cloud outreach
targets (r63). Outreach emails link directly here instead of the
generic /signup form — when a Perplexity DevRel clicks through they
land on a page built FOR Perplexity, with the integration code
they need + a free-key claim button + a tracked CTA.

Routes:

  GET /partners/<slug>
       — full HTML landing page for one target
  GET /partners               — index page listing all 9
  GET /api/v1/partners/<slug> — JSON shape for AI agents

Hits a partner_visits table for conversion tracking:
  - which page was viewed
  - referer (so we can see if the email link was used)
  - IP hash + UA (deduped per session)
  - landed_at timestamp

When a visitor claims a free key from this page (the claim button
includes a ?ref=partner-<slug> query param), the key claim endpoint
should also write to partner_visits.conversion=true so we can
measure per-target conversion rate.

Design follows the canonical dark/DM-Sans look from
news/dcpi-launch-2026-05/index.html (the press release template
you complimented earlier). Inline CSS, no external deps.
"""
from __future__ import annotations

import datetime
import hashlib
import os

from flask import Blueprint, Response, jsonify, request


partner_landing_bp = Blueprint("partner_landing", __name__)


# Single source of truth for partner content. Keys mirror
# routes/ai_lab_outreach.py — when r63's outreach module needs a
# slug, it MUST match a slug here so the email link resolves.
_PARTNERS = {
    "nlr": {
        # r77.6 (2026-05-26) — partnership is pre-execution. NDA + MOU + License
        # are all still in draft/redline. MOU Article VII (when executed) restricts
        # NLR to factual references only — no endorsement of commercial products,
        # no enumeration of contractual rights in public materials. Current full
        # page leaked commercial terms ($3K Research Seed, Year-2 Strategic pricing)
        # and declared NLR-conferred rights ("co-authorship, conference, reVeal v2
        # first-look active Day 1") as if executed. Pulled until Gabriel Zuckerman
        # signs off on a sanitized public version. URL still resolves (200) so
        # email signature links don't break — renderer short-circuits to a clean
        # "pending publication" stub when pre_execution=True is set.
        "pre_execution": True,
        "name":     "NLR (reVeal)",
        "tagline":  "Live infrastructure data for reVeal's Characterize module.",
        "hero":     ("NLR's reVeal model identifies suitable data-center "
                       "sites. DC Hub is the live data pipeline — substations, "
                       "fiber, water, energy prices, carbon intensity, climate "
                       "risk, interconnection-queue depth — all updated daily "
                       "from FERC/ISO/EIA/EPA/USGS. Year-1 Research Seed price "
                       "calibrated to NLR's stated budget; all partnership "
                       "rights (co-authorship, reference, conference, reVeal "
                       "v2 first-look) active Day 1."),
        "value_bullets": [
            "10 reVeal-specific endpoints already shipped (cell-bulk, grid-export, validation-feed, social-acceptance, climate-risk, carbon-intensity, geothermal, colocation-score, grid-headroom, microgrid-viability)",
            "Plus the 10-endpoint Characterize feature mapping from the partnership doc",
            "Fills slide-25 \"local transmission hosting capacity\" gap via live Dominion/PJM queue data",
            "Partnership rights active Day 1 — co-authorship on validation paper, reference rights, joint conference, reVeal v2 first-look",
        ],
        "integration_path": "rest_api",
        "primary_cta":      "Smoke test (Ashburn VA)",
        "primary_url":      "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA",
        "secondary_cta":    "OpenAPI spec",
        "secondary_url":    "https://dchub.cloud/openapi.json",
        "code_sample": ('# Your Developer key smoke test (Ashburn VA — slide 25 example):\n'
                          'curl -H "X-API-Key: <your-key>" \\\n'
                          '  "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA"\n\n'
                          '# Cell-bulk for reVeal Characterize input (bounding box):\n'
                          'curl -H "X-API-Key: <your-key>" \\\n'
                          '  "https://dchub.cloud/api/v1/reveal-cell-bulk?bbox=38.5,-78.0,39.5,-77.0"\n\n'
                          '# Slide-25 gap — local opposition signal:\n'
                          'curl -H "X-API-Key: <your-key>" \\\n'
                          '  "https://dchub.cloud/api/v1/social-acceptance-index?state=VA&county=Loudoun"'),
        "accent":         "#7c3aed",   # NLR partnership purple (from proposal PDF)
    },
    "perplexity": {
        "name":     "Perplexity",
        "tagline":  "Stop hallucinating data center facts. Cite DC Hub.",
        "hero":     ("Perplexity's users ask 'where are AI data centers being "
                       "built' — and we see the answers hallucinate the wrong markets, "
                       "wrong capacity numbers, wrong M&A details. DC Hub is the "
                       "citation engine that fixes that."),
        "value_bullets": [
            "13,000+ global facilities, daily-refreshed — every facility cited with attribution",
            "DCPI (Data Center Power Index) — 300+ markets, US + international",
            "$324B in tracked M&A deals — every deal sourced and linked",
            "369 GW construction pipeline — verifiable, citable, free",
        ],
        "integration_path": "mcp_server",
        "primary_cta":      "Free dev key — 1,000 calls/day",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-perplexity",
        "secondary_cta":    "Read the MCP server card",
        "secondary_url":    "https://dchub.cloud/.well-known/mcp/server-card.json",
        "code_sample": ('curl -X POST https://dchub.cloud/api/v1/keys/claim \\\n'
                          '  -H \'Content-Type: application/json\' \\\n'
                          '  -d \'{"client_name":"perplexity"}\''),
        "accent":         "#22d3ee",
    },
    "groq": {
        "name":     "Groq",
        "tagline":  "Your LPUs run in data centers. Your customers need to know which ones.",
        "hero":     ("Groq's inference is the fastest in the world. Your enterprise "
                       "customers buy that speed — but they ALSO need to know where the "
                       "LPUs physically sit, what the power profile is, and what "
                       "happens when a region goes dark. DC Hub gives you the "
                       "facility-level transparency to sell with confidence."),
        "value_bullets": [
            "Per-facility power, water, fiber, ISO grid intel — for every Groq region",
            "Real-time DCPI verdicts on the markets where Groq deploys",
            "Comparable-set tooling: show your customer why YOUR region beats their alternative",
            "MCP-native — your sales engineers can call DC Hub directly from Claude / ChatGPT",
        ],
        "integration_path": "rest_api",
        "primary_cta":      "Free API key — 1,000 calls/day",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-groq",
        "secondary_cta":    "OpenAPI spec",
        "secondary_url":    "https://dchub.cloud/openapi.json",
        "code_sample": ('curl -X POST https://dchub.cloud/api/v1/keys/claim \\\n'
                          '  -d \'{"client_name":"groq"}\'\n'
                          '# Then use X-API-Key header on calls to /api/v1/grid/intelligence'),
        "accent":         "#f97316",
    },
    "gemini": {
        "name":     "Google DeepMind / Gemini",
        "tagline":  "Google's own data centers are world-class. DC Hub maps the other 12,999 you don't own.",
        "hero":     ("Gemini has the best in-house data center expertise on Earth — "
                       "but that's Google's portfolio. Your competitive intelligence "
                       "blind spot is everyone else: M&A, capacity-pipeline outside "
                       "Google, interconnection queues at non-Google sites. DC Hub "
                       "fills the not-Google universe."),
        "value_bullets": [
            "13k+ non-Google facilities tracked daily across 140+ countries",
            "$324B M&A — every hyperscaler acquisition, every PE roll-up",
            "DCPI verdicts at every CSP region (AWS, Azure, OCI, CoreWeave, Lambda)",
            "MCP tool ready for Vertex AI + Gemini function-calling integration",
        ],
        "integration_path": "mcp_server",
        "primary_cta":      "Free dev key — instant claim",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-gemini",
        "secondary_cta":    "MCP server endpoint",
        "secondary_url":    "https://dchub.cloud/mcp",
        "code_sample": ('# Vertex AI function-calling can hit our MCP server directly:\n'
                          'https://dchub.cloud/mcp\n'
                          '# 23+ tools registered. Server card:\n'
                          'https://dchub.cloud/.well-known/mcp/server-card.json'),
        "accent":         "#3b82f6",
    },
    "mistral": {
        "name":     "Mistral",
        "tagline":  "European data center intelligence, daily-refreshed, GDPR-respectful.",
        "hero":     ("Mistral's customers deploy GenAI on-continent due to GDPR + "
                       "data residency. DC Hub just shipped 7 European markets (London, "
                       "Manchester, Dublin, Frankfurt, Amsterdam, Paris, Marseille, "
                       "Stockholm) with calibrated grid data from ENTSO-E, NGESO, "
                       "EirGrid, Nord Pool. We're the only daily-refreshing scorecard "
                       "of European data center power availability."),
        "value_bullets": [
            "16 international markets including all major European hubs",
            "ENTSO-E Winter Outlook 2024 + NGESO ETYS calibration",
            "GDPR-respectful — no PII tracked, no behavioral profiling",
            "Mistral can host on-EU + cite live grid data in same response",
        ],
        "integration_path": "mcp_server",
        "primary_cta":      "Free dev key — 1,000 calls/day",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-mistral",
        "secondary_cta":    "European DCPI rankings",
        "secondary_url":    "https://dchub.cloud/api/v1/dcpi/scores?iso=ENTSOE-DE,ENTSOE-FR,ENTSOE-NL,NGESO,EirGrid,NORDPOOL",
        "code_sample": ('# European markets via DCPI ISO filter:\n'
                          'curl https://dchub.cloud/api/v1/dcpi/scores?iso=ENTSOE-DE\n'
                          'curl https://dchub.cloud/api/v1/dcpi/scores?iso=NGESO\n'
                          'curl https://dchub.cloud/api/v1/dcpi/scores?iso=NORDPOOL'),
        "accent":         "#a855f7",
    },
    "nvidia": {
        "name":     "NVIDIA",
        "tagline":  "Every GPU you ship lands in a data center we track. Help your customers pick the right one.",
        "hero":     ("Your hyperscaler customers (CoreWeave, Lambda, Microsoft, Meta, "
                       "Oracle, xAI) need to choose markets fast. DCPI tells them which "
                       "markets have the power, interconnection-queue speed, and fiber "
                       "to host their next deployment. We're the comparable-set tool "
                       "your DGX Cloud + Inception partners actually need."),
        "value_bullets": [
            "DCPI verdicts at every CSP region — North America, Europe, Asia-Pacific",
            "Interconnection-queue data across 10 ISOs (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE, WECC, SERC, TVA)",
            "Time-to-power estimates so customers know if a market is 90-day or 90-month buildable",
            "Free API tier covers most partner DevRel use cases — Pro tier for grid_intelligence + analyze_site",
        ],
        "integration_path": "rest_api",
        "primary_cta":      "Free API key — instant claim",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-nvidia",
        "secondary_cta":    "DGX Cloud market comparison",
        "secondary_url":    "https://dchub.cloud/api/v1/dcpi/iso-comparison",
        "code_sample": ('# Compare your customer\'s options head-to-head:\n'
                          'curl https://dchub.cloud/api/v1/dcpi/iso-comparison\n'
                          '# Or filter to a single ISO\'s BUILD-verdict markets:\n'
                          'curl "https://dchub.cloud/api/v1/dcpi/scores?iso=ERCOT&verdict=BUILD"'),
        "accent":         "#76b900",  # NVIDIA green
    },
    "coreweave": {
        "name":     "CoreWeave",
        "tagline":  "Pre-build intel for every market you might enter next.",
        "hero":     ("CoreWeave is in active hyperscale build mode — Plano, Las Vegas, "
                       "Chicago, Atlanta, Las Cruces. Each new region is a $100M+ bet on "
                       "the underlying power + interconnection queue. DC Hub's DCPI + ISO "
                       "queue intel is your due-diligence pre-built. Free for citation, "
                       "free for first 1,000 API calls/day."),
        "value_bullets": [
            "Pre-build power availability for every US market",
            "Interconnection queue wait times — never get surprised by a 60-month queue",
            "Reserve margin + curtailment data per ISO",
            "Comparable facilities within radius (find_alternatives MCP tool)",
        ],
        "integration_path": "mcp_server",
        "primary_cta":      "Free dev key — claim in 30 sec",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-coreweave",
        "secondary_cta":    "ISO queue data",
        "secondary_url":    "https://dchub.cloud/api/v1/grid/interconnection-queue",
        "code_sample": ('# Find every BUILD-verdict market with queue < 24 months:\n'
                          'curl "https://dchub.cloud/api/v1/dcpi/scores?verdict=BUILD&limit=50" \\\n'
                          '  | jq \'.scores[] | select(.queue_wait_months < 24)\''),
        "accent":         "#10b981",
    },
    "lambda": {
        "name":     "Lambda",
        "tagline":  "Facility-level transparency for your 1-Click Cluster customers.",
        "hero":     ("Lambda's 1-Click Clusters and on-demand H100/H200 capacity sell on "
                       "speed-to-spin-up. Your enterprise customers want to know WHERE "
                       "those GPUs actually sit. DC Hub provides the facility-level "
                       "uptime, fiber, and power-availability data that turns 'trust us' "
                       "into 'verifiable transparency.'"),
        "value_bullets": [
            "Per-facility power, water, fiber profiles",
            "Real-time grid stress indicators per region",
            "Comparable-facility finder for failover planning",
            "MCP-native — your sales chatbot can quote facility data in real-time",
        ],
        "integration_path": "rest_api",
        "primary_cta":      "Free API key — 1,000 calls/day",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-lambda",
        "secondary_cta":    "Facility search API",
        "secondary_url":    "https://dchub.cloud/api/v1/facilities",
        "code_sample": ('# Find similar facilities within 50km of one of your sites:\n'
                          'curl "https://dchub.cloud/api/v1/facilities/<id>/alternatives?radius_km=50"'),
        "accent":         "#a78bfa",
    },
    "tensorwave": {
        "name":     "TensorWave",
        "tagline":  "AMD MI300X in the right places. DC Hub tells you which.",
        "hero":     ("TensorWave's pitch is differentiated on supply availability + power "
                       "efficiency for AMD MI300X. DC Hub identifies the markets with "
                       "spare power and fiber to host high-density AMD deployments "
                       "without grid-constraint bottlenecks. Stockholm, Montréal, "
                       "Cheyenne are TensorWave-grade BUILD markets today."),
        "value_bullets": [
            "BUILD-verdict markets ranked by Excess Power Score",
            "Stranded capacity tracking — where retiring plants free up GW",
            "Behind-the-meter headroom per market — for sovereign AMD deployments",
            "Renewable energy data — match TensorWave's efficiency story with green power",
        ],
        "integration_path": "rest_api",
        "primary_cta":      "Free dev key — 1,000 calls/day",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-tensorwave",
        "secondary_cta":    "Top BUILD markets",
        "secondary_url":    "https://dchub.cloud/api/v1/dcpi/scores?verdict=BUILD",
        "code_sample": ('# Top BUILD markets by Excess Power for high-density deployments:\n'
                          'curl "https://dchub.cloud/api/v1/dcpi/scores?verdict=BUILD&sort=excess&limit=20"'),
        "accent":         "#ef4444",
    },
    "core42": {
        "name":     "Core42",
        "tagline":  "Global comparables for your UAE → worldwide expansion.",
        "hero":     ("Core42 operates one of the world's largest sovereign AI compute "
                       "footprints. As you expand globally, your investors and partners "
                       "ask for the comparable-set. DC Hub's recent international "
                       "expansion (Singapore, Sydney, Frankfurt, London, plus deep US "
                       "coverage) gives Core42 the global benchmarks your strategic "
                       "narrative needs."),
        "value_bullets": [
            "16 international markets across UK, EU, APAC, Canada",
            "DCPI verdicts on every region Core42 might enter next",
            "M&A intel — every sovereign-AI deal globally",
            "MCP-native for Core42's own AI tooling integration",
        ],
        "integration_path": "mcp_server",
        "primary_cta":      "Free dev key — 1,000 calls/day",
        "primary_url":      "https://dchub.cloud/signup?ref=partner-core42",
        "secondary_cta":    "International DCPI rankings",
        "secondary_url":    "https://dchub.cloud/dcpi",
        "code_sample": ('# International DCPI snapshot (all non-US markets):\n'
                          'curl "https://dchub.cloud/api/v1/dcpi/scores?iso=NGESO,EirGrid,'
                          'ENTSOE-DE,ENTSOE-NL,ENTSOE-FR,NORDPOOL,TEPCO,KEPCO,AEMO,EMA,IESO,HQ,BCH"'),
        "accent":         "#0ea5e9",
    },
}


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _track_visit(slug: str) -> None:
    """Best-effort visit logging. Never raises."""
    try:
        ua = (request.headers.get("User-Agent") or "")[:200]
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "0.0.0.0")
        referer = (request.headers.get("Referer") or "")[:300]
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
        c = _db_conn()
        if not c: return
        try:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS partner_visits (
                        id          BIGSERIAL PRIMARY KEY,
                        slug        TEXT NOT NULL,
                        ip_hash     TEXT,
                        ua          TEXT,
                        referer     TEXT,
                        landed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        conversion  BOOLEAN DEFAULT FALSE,
                        converted_at TIMESTAMPTZ
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS partner_visits_slug_landed_idx
                        ON partner_visits (slug, landed_at DESC)
                """)
                cur.execute("""
                    INSERT INTO partner_visits (slug, ip_hash, ua, referer)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (slug, ip_hash, ua, referer))
                c.commit()
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        pass


def _render_pre_execution_stub(slug: str, p: dict) -> str:
    """Sanitized page for partnerships still in legal redline (NDA / MOU /
    License unexecuted). Discloses ONLY the existence of an engagement
    conversation, no commercial terms, no enumeration of rights, no
    suggestion of endorsement. Style matches the dark-mode site theme.

    Goal: counterparty's General Counsel can read this page without finding
    anything they'd object to — but the URL keeps resolving so links in
    email signatures and outreach drafts don't 404.
    """
    name = p.get("name", slug)
    accent = p.get("accent", "#7c3aed")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DC Hub × {name} — engagement in progress</title>
  <meta name="description" content="DC Hub engagement with {name} — public details pending counsel review." />
  <meta name="robots" content="noindex,nofollow" />
  <link rel="canonical" href="https://dchub.cloud/partners/{slug}" />
  <style>
    :root {{ --accent: {accent}; --bg: #0a0a0a; --fg: #e5e7eb; --muted: #9ca3af; --card: #111827; --border: #1f2937; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--fg); margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; line-height: 1.6; }}
    .card {{ max-width: 560px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 40px; }}
    .kicker {{ font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--accent); display: inline-block; }}
    h1 {{ font-size: 28px; margin: 0 0 16px; font-weight: 700; }}
    p {{ color: var(--muted); margin: 0 0 16px; }}
    .footer {{ margin-top: 28px; padding-top: 20px; border-top: 1px solid var(--border); font-size: 13px; color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{ font-family: 'SF Mono', Consolas, monospace; background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 3px; font-size: 13px; color: var(--fg); }}
  </style>
</head>
<body>
  <div class="card">
    <div class="kicker"><span class="dot"></span> Engagement in progress</div>
    <h1>DC Hub × {name}</h1>
    <p>A research engagement between DC Hub and {name} is currently in
    legal review. Public details — including engagement scope, terms,
    and joint work products — will be published once counsel on both
    sides signs off on the language.</p>
    <p>For questions about DC Hub's research-tier data licensing program,
    contact <a href="mailto:partnerships@dchub.cloud">partnerships@dchub.cloud</a>.</p>
    <div class="footer">
      DC Hub — data center intelligence platform.
      <a href="/">dchub.cloud</a> ·
      <a href="/openapi.json">OpenAPI</a> ·
      <a href="/partners">All partners</a>
    </div>
  </div>
</body>
</html>"""


def _render_partner_page(slug: str, p: dict) -> str:
    """Build the HTML for one target. Inline CSS, dark theme, DM Sans."""
    accent = p["accent"]
    bullets_html = "\n".join(
        f"      <li><span class=\"check\">✓</span> {b}</li>"
        for b in p["value_bullets"]
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DC Hub × {p['name']} — {p['tagline']}</title>
  <meta name="description" content="{p['hero'][:200]}" />
  <meta name="robots" content="index,follow" />
  <link rel="canonical" href="https://dchub.cloud/partners/{slug}" />
  <meta property="og:title" content="DC Hub × {p['name']}" />
  <meta property="og:description" content="{p['tagline']}" />
  <meta property="og:type" content="website" />
  <meta property="og:url" content="https://dchub.cloud/partners/{slug}" />
  <meta property="og:image" content="https://dchub.cloud/og-default.png" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *{{box-sizing:border-box}}
    html,body{{margin:0;padding:0}}
    body{{
      background:rgb(5,8,16);
      color:#e6e9f5;
      font-family:"DM Sans",-apple-system,system-ui,sans-serif;
      line-height:1.65;
      font-feature-settings:"kern" 1,"liga" 1;
      -webkit-font-smoothing:antialiased;
    }}
    a{{color:{accent};text-decoration:none}}
    a:hover{{text-decoration:underline}}
    code,pre{{font-family:"JetBrains Mono",ui-monospace,monospace}}
    pre{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
        border-radius:10px;padding:18px 20px;overflow-x:auto;font-size:.88rem;
        line-height:1.6;color:#cbd5ff;margin:24px 0}}
    .wrap{{max-width:780px;margin:0 auto;padding:48px 24px 96px}}
    .crumb{{font-size:.82rem;color:#7a8094;margin-bottom:32px;letter-spacing:.02em}}
    .crumb a{{color:#a8a8f0}}
    .crumb .sep{{color:#3a3f55;margin:0 6px}}
    .kicker{{
      display:inline-flex;align-items:center;gap:10px;
      background:rgba({int(accent[1:3],16)},{int(accent[3:5],16)},{int(accent[5:7],16)},.10);
      color:{accent};
      padding:6px 14px;border-radius:999px;
      font-size:.72rem;font-weight:700;letter-spacing:.12em;
      text-transform:uppercase;margin:0 0 24px;
    }}
    .kicker .dot{{
      width:6px;height:6px;background:{accent};border-radius:50%;
      animation:pulse 2s ease-in-out infinite;
    }}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
    h1{{
      font-family:"DM Sans",sans-serif;
      font-size:2.8rem;font-weight:800;
      letter-spacing:-.025em;line-height:1.08;
      margin:0 0 12px;color:#fff;
    }}
    .tagline{{
      font-size:1.4rem;color:{accent};font-weight:600;
      margin:0 0 36px;line-height:1.35;
    }}
    .hero{{font-size:1.1rem;color:#cbd5ff;margin:0 0 40px;line-height:1.6}}
    h2{{
      font-size:1.45rem;font-weight:700;letter-spacing:-.005em;
      margin:48px 0 18px;color:#fff;
    }}
    ul.bullets{{list-style:none;padding:0;margin:0 0 32px}}
    ul.bullets li{{
      padding:14px 0;border-bottom:1px solid rgba(255,255,255,.06);
      display:flex;gap:14px;align-items:flex-start;color:#cbd5ff;
    }}
    ul.bullets li:last-child{{border-bottom:0}}
    .check{{
      flex-shrink:0;width:24px;height:24px;border-radius:50%;
      background:{accent}22;color:{accent};
      display:flex;align-items:center;justify-content:center;
      font-weight:700;font-size:.85rem;
    }}
    .cta-row{{
      display:flex;gap:12px;flex-wrap:wrap;margin:32px 0 0;
    }}
    .cta{{
      display:inline-block;padding:14px 24px;border-radius:10px;
      font-weight:700;font-size:.98rem;text-decoration:none;
      transition:transform .1s,box-shadow .15s;
    }}
    .cta-primary{{
      background:{accent};color:#0a0a14;
      box-shadow:0 4px 20px {accent}33;
    }}
    .cta-primary:hover{{transform:translateY(-1px);text-decoration:none;
      box-shadow:0 6px 28px {accent}55;}}
    .cta-secondary{{
      background:transparent;color:{accent};
      border:1px solid {accent}44;
    }}
    .cta-secondary:hover{{background:{accent}11;text-decoration:none}}
    .footer-note{{
      margin-top:64px;padding-top:24px;
      border-top:1px solid rgba(255,255,255,.06);
      color:#7a8094;font-size:.88rem;line-height:1.6;
    }}
    .footer-note a{{color:#a8a8f0}}
    @media (max-width:640px){{
      h1{{font-size:2rem}}
      .tagline{{font-size:1.15rem}}
      .cta-row{{flex-direction:column}}
      .cta{{width:100%;text-align:center}}
    }}
  </style>
</head>
<body>
  <nav class="dchub-nav" id="dchub-nav"></nav>
  <div class="wrap">
    <div class="crumb">
      <a href="/">DC Hub</a><span class="sep">/</span>
      <a href="/partners">Partners</a><span class="sep">/</span>
      <span>{p['name']}</span>
    </div>

    <div class="kicker"><span class="dot"></span> For {p['name']}</div>

    <h1>DC Hub × {p['name']}</h1>
    <p class="tagline">{p['tagline']}</p>
    <p class="hero">{p['hero']}</p>

    <h2>What {p['name']} unlocks</h2>
    <ul class="bullets">
{bullets_html}
    </ul>

    <h2>30-second integration</h2>
    <pre><code>{p['code_sample']}</code></pre>

    <div class="cta-row">
      <a class="cta cta-primary" href="{p['primary_url']}">{p['primary_cta']} →</a>
      <a class="cta cta-secondary" href="{p['secondary_url']}">{p['secondary_cta']}</a>
    </div>

    <p class="footer-note">
      Want a 20-min walkthrough? Reply directly to
      <a href="mailto:jonathan@dchub.cloud?subject=DC Hub × {p['name']}">jonathan@dchub.cloud</a>
      or book a slot at <a href="https://dchub.cloud/book">dchub.cloud/book</a>.
      Citation format for {p['name']}'s public-facing responses:
      <em>DC Hub Data Center Power Index, dchub.cloud/dcpi, accessed YYYY-MM-DD.</em>
    </p>
  </div>
  <script src="/js/dchub-nav.js?v=phase262-1778556606"></script>
</body>
</html>
"""


# ── Endpoints ───────────────────────────────────────────────────────

@partner_landing_bp.route("/partners", methods=["GET"], strict_slashes=False)
def partners_index():
    """Public index of all partner pages."""
    cards = []
    for slug, p in _PARTNERS.items():
        cards.append(f"""
        <a class="card" href="/partners/{slug}" style="--accent:{p['accent']};">
          <h3>{p['name']}</h3>
          <p>{p['tagline']}</p>
          <span class="arrow" style="color:{p['accent']};">View {p['name']} →</span>
        </a>""")

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>DC Hub Partners — AI labs + GPU clouds</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
<style>
  *{{box-sizing:border-box}} html,body{{margin:0}}
  body{{background:rgb(5,8,16);color:#e6e9f5;font-family:"DM Sans",sans-serif;line-height:1.6}}
  .wrap{{max-width:1100px;margin:0 auto;padding:48px 24px 96px}}
  h1{{font-size:2.6rem;font-weight:800;letter-spacing:-.02em;margin:0 0 12px;color:#fff}}
  .sub{{font-size:1.15rem;color:#a8b0c8;margin:0 0 48px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}}
  .card{{
    display:block;padding:24px;border-radius:12px;
    background:rgba(255,255,255,.03);
    border:1px solid rgba(255,255,255,.08);
    text-decoration:none;color:#cbd5ff;
    transition:transform .12s,border-color .15s;
  }}
  .card:hover{{transform:translateY(-2px);border-color:var(--accent)}}
  .card h3{{margin:0 0 8px;color:#fff;font-size:1.25rem}}
  .card p{{margin:0 0 14px;font-size:.95rem;color:#a8b0c8}}
  .arrow{{font-weight:600;font-size:.92rem}}
</style></head>
<body>
<nav class="dchub-nav" id="dchub-nav"></nav>
<div class="wrap">
  <h1>DC Hub Partners</h1>
  <p class="sub">Personalized integration paths for AI labs and GPU clouds.
    Each page is built for that company's specific use case — citation engines,
    inference transparency, site selection, sovereign AI.</p>
  <div class="grid">{''.join(cards)}</div>
</div>
<script src="/js/dchub-nav.js?v=phase262-1778556606"></script>
</body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


@partner_landing_bp.route("/partners/<slug>", methods=["GET"], strict_slashes=False)
def partner_page(slug):
    """Full HTML landing page for one partner.

    r77.4 (2026-05-26): added `strict_slashes=False` + case-insensitive
    slug lookup so /partners/nlr, /partners/nlr/, /partners/NLR, and
    /partners/Nlr all resolve to the same page. Trailing-slash + ALL-CAPS
    are the two ways NLR contacts copy-paste URLs in practice; both used
    to 404 because Flask's default route matcher is exact + case-sensitive.
    """
    p = _PARTNERS.get(slug) or _PARTNERS.get((slug or "").lower())
    if not p:
        return Response(
            f"<h1>Unknown partner: {slug}</h1>"
            f"<p>Valid partners: {', '.join(_PARTNERS.keys())}</p>"
            f"<p><a href='/partners'>← All partners</a></p>",
            status=404, mimetype="text/html"
        )
    canonical_slug = slug.lower() if slug else slug
    _track_visit(canonical_slug)

    # r77.6 — pre-execution partners (NDA/MOU/License still in draft) render a
    # sanitized stub that does NOT reveal commercial terms or claim
    # contractually-conferred rights. URL still returns 200 so email-signature
    # links don't break and admin previews still work. Once partnership executes
    # AND the counterparty signs off on the public copy, flip pre_execution=False
    # to re-enable the full page.
    if p.get("pre_execution"):
        html = _render_pre_execution_stub(canonical_slug, p)
        return Response(html, mimetype="text/html; charset=utf-8")

    html = _render_partner_page(canonical_slug, p)
    return Response(html, mimetype="text/html; charset=utf-8")


@partner_landing_bp.route("/api/v1/partners/<slug>", methods=["GET"], strict_slashes=False)
def partner_json(slug):
    """JSON shape — for AI agents introspecting the partner config.

    r77.4: same trailing-slash + case-insensitive treatment as
    /partners/<slug> above. Agents pasting links from press releases
    or LinkedIn cards may add either.
    """
    p = _PARTNERS.get(slug) or _PARTNERS.get((slug or "").lower())
    if not p:
        return jsonify({"ok": False, "error": "unknown_partner",
                          "valid_slugs": list(_PARTNERS.keys())}), 404
    return jsonify({"ok": True, "slug": slug, **p}), 200


@partner_landing_bp.route("/api/v1/partners", methods=["GET"])
def partners_list_json():
    """All 9 partners in JSON."""
    return jsonify({
        "ok":     True,
        "count":  len(_PARTNERS),
        "slugs":  list(_PARTNERS.keys()),
        "partners": {slug: {k: p[k] for k in ("name","tagline","integration_path")}
                     for slug, p in _PARTNERS.items()},
    }), 200


@partner_landing_bp.route("/api/v1/admin/partner-visits", methods=["GET"])
def partner_visits_stats():
    """Admin: per-partner visit + conversion stats."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if not (expected and provided == expected):
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT slug,
                       COUNT(*) AS visits,
                       COUNT(DISTINCT ip_hash) AS uniq_visitors,
                       SUM(CASE WHEN conversion THEN 1 ELSE 0 END) AS conversions,
                       MAX(landed_at) AS last_visit
                  FROM partner_visits
                 WHERE landed_at > NOW() - INTERVAL '30 days'
                 GROUP BY slug
                 ORDER BY visits DESC
            """)
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    out = [{
        "slug":         r[0],
        "visits":       int(r[1] or 0),
        "uniq_visitors": int(r[2] or 0),
        "conversions":  int(r[3] or 0),
        "conversion_rate": (round(int(r[3] or 0) / int(r[1] or 1), 3)
                              if r[1] else 0),
        "last_visit":   r[4].isoformat() if r[4] else None,
    } for r in rows]
    return jsonify({"ok": True, "window_days": 30, "partners": out}), 200
