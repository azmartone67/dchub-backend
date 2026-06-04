"""
methodology_pages.py — public landing pages for the methodology URLs cited
in DC Hub PDFs and citation footnotes.

Background (2026-06-04): the Power Delivery Methodology PDF v1.0 we sent
to CBRE Research cites URLs that were 404 on ship — fatal first impression
for an audit-grade document. This blueprint closes those URLs:

  /methodology                          — index of versioned methodologies
  /methodology/queue                    — alias to current Power Delivery version
  /methodology/queue/v1.0               — Power Delivery Methodology v1.0 web page
  /methodology/data-dictionary.json     — machine-readable field reference
  /partners/cbre                        — CBRE-specific landing (neutrality preserved)

r-cf-bypass (2026-06-04): the CF zone-level worker is intercepting
/methodology* and returning Error 1000 ("DNS points to prohibited IP") —
same pattern as /research/* per reference_dchub_research_path_error1000.md.
Until the CF dashboard fix lands, every methodology view is ALSO registered
under /docs/methodology/* which CF Pages proxies cleanly to Flask. The
PDF cites /methodology/* (preserved for when zone routing is fixed); the
follow-up corrective email points Gordon at the /docs/methodology/* paths.

Each page renders inline HTML (no Jinja dependency) using the same dark/purple
visual language as the PDF and the proof deck so the brand stays coherent.
"""
from __future__ import annotations

import os
from flask import Blueprint, Response, jsonify, request

methodology_pages_bp = Blueprint("methodology_pages", __name__)


# ── Shared style ----------------------------------------------------------

_CSS = """
<style>
  :root {
    --bg: #0F172A; --panel: #1E293B; --panel-alt: #182131;
    --white: #FFFFFF; --gray-l: #9CA3AF; --gray-m: #6B7280; --gray-d: #374151;
    --purple: #A78BFA; --purple-h: #7C3AED;
    --green: #10B981; --amber: #F59E0B;
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:0; background:var(--bg); color:var(--white);
         font-family: 'Instrument Sans', system-ui, -apple-system, sans-serif;
         line-height:1.55; -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 56px 32px 96px; }
  .eyebrow { color: var(--purple); font-weight: 700; font-size: 12px;
             letter-spacing: 0.12em; text-transform: uppercase; }
  h1 { font-size: 44px; font-weight: 700; margin: 8px 0 12px; line-height:1.1; }
  .lede { color: var(--gray-l); font-size: 17px; max-width: 720px; }
  .rule { height:1px; background: var(--purple-h); margin: 32px 0; opacity:0.55;}
  h2 { font-size: 24px; font-weight: 700; color: var(--white);
       margin: 36px 0 10px; }
  h2 .num { color: var(--purple); font-family: 'JetBrains Mono', monospace;
            font-size: 16px; margin-right: 12px; }
  h3 { font-size: 16px; font-weight: 700; color: var(--purple); margin: 22px 0 6px; }
  p, li { color: var(--white); font-size: 14.5px; }
  .muted { color: var(--gray-l); }
  .card { background: var(--panel); border-left: 4px solid var(--purple);
          padding: 18px 22px; border-radius: 2px; margin: 16px 0; }
  .card.warn { border-left-color: var(--amber); }
  .card.ok   { border-left-color: var(--green); }
  code, .mono { font-family: 'JetBrains Mono', 'SF Mono', Menlo, monospace;
                font-size: 13px; }
  pre.mono { background: var(--panel-alt); padding: 14px 18px; overflow:auto;
             border-radius: 2px; color: var(--white); }
  table { width:100%; border-collapse: collapse; margin: 14px 0; }
  th { text-align:left; padding: 10px 12px; background: var(--panel);
       color: var(--gray-l); font-weight: 700; font-size: 12px;
       letter-spacing: 0.04em; text-transform: uppercase; }
  td { padding: 10px 12px; border-top: 1px solid var(--gray-d);
       font-size: 13.5px; }
  tr:nth-child(odd) td { background: var(--panel-alt); }
  a { color: var(--purple); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .foot { color: var(--gray-m); font-size: 12px; margin-top: 56px;
          padding-top: 18px; border-top: 1px solid var(--gray-d); }
  .pill { display:inline-block; background: var(--panel); color: var(--purple);
          font-family: 'JetBrains Mono', monospace; font-size: 11.5px;
          padding: 3px 10px; border-radius: 999px; margin-right: 6px;
          border: 1px solid var(--gray-d); }
</style>
"""

def _page(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<title>{title} · DC Hub</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel=stylesheet>
{_CSS}
</head>
<body><div class=wrap>{body_html}</div></body></html>"""


# ── /methodology — index page --------------------------------------------

_INDEX_HTML = """
<div class=eyebrow>DC HUB · METHODOLOGY INDEX</div>
<h1>Methodology versions</h1>
<p class=lede>Public, audit-trailed methodologies for every DC Hub data product cited in third-party research, broker reports, and AI-agent outputs. Every published number traces to a versioned methodology document on this page.</p>
<div class=rule></div>

<h2><span class=num>01</span>Power Delivery Index</h2>
<p>Months-to-energization per market. AI vs traditional facility classification. Shell-co M&amp;A pattern matching.</p>
<ul>
  <li><a href="/methodology/queue/v1.0">v1.0</a> · 2026-06-04 · current · <span class=pill>active</span></li>
</ul>

<h2><span class=num>02</span>DC Hub Power Index (DCPI)</h2>
<p>BUILD / CAUTION / AVOID verdicts across 233 markets. Composite scoring framework.</p>
<ul>
  <li><a href="/dcpi#methodology">DCPI methodology anchor</a> · maintained on /dcpi · <span class=pill>active</span></li>
</ul>

<h2><span class=num>03</span>DC Hub Gas Index (DCGI)</h2>
<p>Per-market gas pricing + heat-rate-derived $/MWh.</p>
<ul>
  <li><a href="/api/v1/dcgi/methodology">DCGI methodology JSON</a> · <span class=pill>active</span></li>
</ul>

<div class=rule></div>
<h2><span class=num>04</span>Data Dictionary</h2>
<p>Machine-readable field reference for every endpoint in /api/v1/*.</p>
<ul>
  <li><a href="/methodology/data-dictionary.json">/methodology/data-dictionary.json</a></li>
</ul>

<p class=foot>For compliance escalation or any data-traceability request:
<a href="mailto:jonathan@dchub.cloud">jonathan@dchub.cloud</a> · 48-hour SLA.</p>
"""

def _nocache(resp):
    """Force-disable any upstream caching of methodology pages.
    A 404 from this blueprint during a rolling-deploy gap was getting
    cached by Cloudflare for 1h via the global Flask cache-control
    middleware (private, max-age=3600). no-store + immediate expiry
    overrides that so the next correct response replaces the cache."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["CDN-Cache-Control"] = "no-store"
    resp.headers["Surrogate-Control"] = "no-store"
    return resp


@methodology_pages_bp.route("/methodology", methods=["GET"], strict_slashes=False)
@methodology_pages_bp.route("/docs/methodology", methods=["GET"], strict_slashes=False)
@methodology_pages_bp.route("/api/v1/methodology", methods=["GET"], strict_slashes=False)
def methodology_index():
    return _nocache(Response(_page("Methodology", _INDEX_HTML), mimetype="text/html"))


# ── /methodology/queue — alias to current version ------------------------

@methodology_pages_bp.route("/methodology/queue", methods=["GET"],
                              strict_slashes=False)
@methodology_pages_bp.route("/docs/methodology/queue", methods=["GET"],
                              strict_slashes=False)
@methodology_pages_bp.route("/api/v1/methodology/queue", methods=["GET"],
                              strict_slashes=False)
def methodology_queue_current():
    """Alias: bare /methodology/queue resolves to current version."""
    return methodology_queue_v1_0()


# ── /methodology/queue/v1.0 — Power Delivery Methodology v1.0 ------------

_QUEUE_V1_HTML = """
<div class=eyebrow>AUDIT DOCUMENT · DC HUB × CBRE RESEARCH · PRE-CITATION REVIEW</div>
<h1>DC Hub Power Delivery Methodology</h1>
<p class=lede>Audit-grade methodology specification for citation review. Version 1.0 — 2026-06-04. Maintained by Jonathan Martone, DC Hub. Companion PDF and data dictionary linked below.</p>
<div class=rule></div>

<div class=card>
<strong>Scope of v1.0:</strong>
<ul>
  <li>Months-to-energization (Power Delivery Index)</li>
  <li>AI vs traditional facility classification (<code>derived_use</code> field)</li>
  <li>Shell-co M&amp;A pattern matching</li>
  <li>Citation practice + audit-trail SLA</li>
</ul>
</div>

<div class="card warn">
<strong>Out of scope for v1.0:</strong> International (non-US) queue ·
gas-to-grid heat-rate ranges · per-utility tariff lookups · ICE forward curves.
Targeted for v1.1.
</div>

<h2 id=power-delivery><span class=num>01</span>Months-to-energization</h2>

<h3>Source data</h3>
<p>ISO/RTO interconnection queues (live):
<span class=pill>PJM</span><span class=pill>ERCOT</span><span class=pill>MISO</span><span class=pill>SPP</span><span class=pill>CAISO</span><span class=pill>ISO-NE</span><span class=pill>NYISO</span></p>
<p>Vertically-integrated utilities (filing-based):
<span class=pill>Duke (DEC, DEP)</span><span class=pill>Southern Co</span><span class=pill>SRP</span><span class=pill>APS</span><span class=pill>TVA</span><span class=pill>BPA</span><span class=pill>AESO</span><span class=pill>Hydro-Québec</span><span class=pill>NV Energy</span><span class=pill>PacifiCorp</span></p>
<p>Per row: queue position, MW requested, queue entry date, current study phase
(FS / SIS / FIS), commercial-operation-date target.</p>

<h3>Refresh cadence</h3>
<p>Daily snapshot at 04:00 UTC via
<code>/api/v1/interconnection-queue/snapshot</code>. Each row is stamped
<code>fetched_at</code> + <code>source_publish_date</code>. Historical
snapshots: 5 years retained, queryable. Snapshot deltas surfaced via
<code>/api/v1/interconnection-queue/diff?since=YYYY-MM-DD</code>.</p>
<p>Source-document hash recorded per snapshot so any published number can be
traced to the ISO's original filing, not a DC Hub-mediated interpretation.</p>

<h3 id=calculation>Calculation</h3>
<ol>
  <li>Filter to active large-load queue (&ge; 20 MW) per market.</li>
  <li>Compute median months between queue entry and the last completed study
      transition (FS → SIS → FIS).</li>
  <li>Multiply by remaining-stage factor based on 90-day rolling
      study-completion velocity in the parent ISO.</li>
</ol>
<p>Published as <strong>P25 / P50 / P75</strong>. Single-point summary in
customer reports cites P50 by default.</p>

<h3 id=withdrawal-decay>False-positive handling (withdrawal decay)</h3>
<p>Speculative requests that withdraw inflate the P50. Each queue row gets a
<code>continuity_weight</code> = 1.0 at entry, decaying exponentially toward 0
if the request is withdrawn, reaching 0.0 at
<code>withdrawal_date</code> + 180 days.</p>
<p>Markets with fewer than 8 completed reference projects in the trailing 24
months publish <code>NULL</code> with a <code>low_confidence</code> flag
rather than a guessed value.</p>

<div class=rule></div>

<h2 id=ai-classifier><span class=num>02</span>AI vs traditional facility classifier</h2>

<h3>Feature inputs (heuristic stack)</h3>
<ul>
  <li>Density (kW/rack) — AI workloads start &ge; 30 kW</li>
  <li>PUE published — cooling-optimized vs liquid/immersion footprints</li>
  <li>Public press / SEC filings naming use case</li>
  <li>Construction permits + IT manufacturer disclosures (NVIDIA / AMD shipment correlations)</li>
  <li>Cooling type (air / liquid / immersion) — from air-permit filings</li>
  <li>Power-capacity uplift (&ge; 50 MW announced expansion in 24 mo → high prior for AI repurposing)</li>
</ul>

<h3>Output classes</h3>
<p><code>AI · Traditional · Mixed · Unknown</code></p>

<h3 id=confidence-bands>Confidence bands</h3>
<table>
<tr><th>Band</th><th>Score</th><th>Action</th></tr>
<tr><td>high</td><td><code>&ge; 0.85</code></td><td>published with strong claim</td></tr>
<tr><td>medium</td><td><code>0.60–0.85</code></td><td>published with caveat</td></tr>
<tr><td>low</td><td><code>&lt; 0.60</code></td><td>collapsed to <code>Unknown</code></td></tr>
</table>
<p><strong>Critical default:</strong> when confidence &lt; 0.60 we publish
<code>Unknown</code> rather than guess.</p>

<h3 id=false-positive-rates>Holdout-set false-positive rates</h3>
<p>Against a manually-labeled holdout set (n = 412 facilities):</p>
<table>
<tr><th>Class</th><th>Correct rate</th></tr>
<tr><td>AI → claimed correctly</td><td style="color:#10B981"><code>93.4%</code></td></tr>
<tr><td>Traditional → claimed correctly</td><td style="color:#10B981"><code>91.1%</code></td></tr>
<tr><td>Unknown (refuse-to-guess) rate</td><td style="color:#F59E0B"><code>8.2%</code></td></tr>
</table>

<div class="card warn">
<strong>Acknowledged limitations</strong> (publish in any CBRE methodology footnote):
<ul>
  <li>Greenfield announcements (unbuilt facilities) frequently lack disclosure
      on workload type. Default class is <code>Unknown</code> until construction
      permits or tenant disclosures land.</li>
  <li>Co-location operators serving multiple tenants are classified
      <code>Mixed</code> when any tenant disclosure indicates AI workload —
      under-counts traditional-only co-lo blocks. Refresh frequency: weekly
      re-classification on any new public signal.</li>
</ul>
</div>

<div class=rule></div>

<h2 id=shell-co><span class=num>03</span>Shell-co M&amp;A pattern matching</h2>

<p>Source corpus: 2,032 verified data-center M&amp;A transactions (2018–present)
+ weekly utility-filing ingest across 22 grids.</p>

<p>Match logic: utility-filing entity name + filing address ↔ deal-corpus
shell-co naming patterns + acquirer pipeline. Two confidence tiers:</p>

<table>
<tr><th>Tier</th><th>Description</th><th>Behavior</th></tr>
<tr><td><span style="color:#10B981">explicit_match</span></td>
    <td>named entity equality</td>
    <td>highest confidence; surfaces immediately</td></tr>
<tr><td><span style="color:#F59E0B">inferred_match</span></td>
    <td>naming pattern + queue-position + capacity bracket</td>
    <td>medium; flagged for review</td></tr>
</table>

<h2 id=citation><span class=num>04</span>Recommended citation practice</h2>

<p>Use the same footnote convention CBRE uses for CoStar in office leasing reports:</p>

<pre class=mono>DC Hub Power Delivery data layer (v1.0).
/api/v1/interconnection-queue/snapshot. Refreshed daily.
Methodology: dchub.cloud/methodology/queue/v1.0  ·  Snapshot date: {YYYY-MM-DD}</pre>

<p>Methodology version is tagged in every API response header
(<code>X-DC-Methodology-Version: v1.0</code>) so a published number remains
reproducible even after the methodology is updated.</p>

<div class="card ok">
<strong>Audit-trail SLA</strong> — Every published number traces to a specific
row in <code>interconnection_queue_snapshot</code> + <code>market_gas_pricing</code> +
<code>transactions</code>, stamped with <code>fetched_at</code> +
<code>source_publish_date</code> + <code>run_id</code>.
<br><br>
<strong>Recovery procedure (compliance escalation):</strong> provide the
published number + report date → DC Hub returns the source rows +
ISO-published source URLs <strong>within 48 hours</strong>.
</div>

<p class=foot>Compliance contact: Jonathan Martone ·
<a href="mailto:jonathan@dchub.cloud">jonathan@dchub.cloud</a> ·
48-hour SLA on any data-traceability request from CBRE Research or CBRE Legal.
<br>Companion PDF: <a href="/static/DCHUB_POWER_DELIVERY_METHODOLOGY_v1.0.pdf">v1.0 PDF</a> ·
Data dictionary: <a href="/methodology/data-dictionary.json">/methodology/data-dictionary.json</a>
<br>— End of methodology v1.0 —
</p>
"""

@methodology_pages_bp.route("/methodology/queue/v1.0", methods=["GET"],
                              strict_slashes=False)
@methodology_pages_bp.route("/docs/methodology/queue/v1.0", methods=["GET"],
                              strict_slashes=False)
@methodology_pages_bp.route("/api/v1/methodology/queue/v1.0", methods=["GET"],
                              strict_slashes=False)
def methodology_queue_v1_0():
    resp = Response(
        _page("Power Delivery Methodology v1.0", _QUEUE_V1_HTML),
        mimetype="text/html",
    )
    resp.headers["X-DC-Methodology-Version"] = "v1.0"
    return _nocache(resp)


# ── /methodology/data-dictionary.json ------------------------------------

_DATA_DICTIONARY = {
    "version": "v1.0",
    "as_of": "2026-06-04",
    "endpoints": {
        "/api/v1/interconnection-queue/snapshot": {
            "description": "Daily snapshot of active large-load queue per market.",
            "refresh_utc": "04:00",
            "fields": {
                "market_slug":         "string · DCPI market slug",
                "iso":                 "enum · PJM ERCOT MISO SPP CAISO ISO-NE NYISO AESO HQ",
                "queue_position":      "int · ISO-published queue rank",
                "mw_requested":        "float · interconnection capacity in MW",
                "queue_entry_date":    "ISO-8601 date · ISO-published queue entry",
                "study_phase":         "enum · FS | SIS | FIS | completed | withdrawn",
                "cod_target":          "ISO-8601 date · expected commercial operation date (nullable)",
                "continuity_weight":   "float · 0.0–1.0, withdrawal-decay weight",
                "source_publish_date": "ISO-8601 timestamp · ISO publication timestamp",
                "source_url":          "string · ISO original filing URL",
                "fetched_at":          "ISO-8601 timestamp · DC Hub fetch timestamp",
                "run_id":              "string · DC Hub batch identifier for audit"
            }
        },
        "/api/v1/markets/<slug>/power-delivery": {
            "description": "Computed months-to-energization for a single DCPI market.",
            "refresh_utc": "04:00 (derived)",
            "fields": {
                "market_slug":         "string · DCPI market slug",
                "iso":                 "enum",
                "months_p25":          "float · 25th percentile months-to-energization",
                "months_p50":          "float · median (the headline number)",
                "months_p75":          "float · 75th percentile",
                "active_queue_mw":     "int · sum of MW with continuity_weight > 0",
                "trailing_90d_delta":  "float · change in P50 vs 90 days ago",
                "n_reference_projects": "int · sample size for the median (<8 → low_confidence)",
                "low_confidence":      "bool · true when n < 8 OR P50 is null",
                "snapshot_id":         "string · interconnection_queue_snapshot run_id"
            }
        },
        "/api/v1/facilities/classify": {
            "description": "AI vs traditional facility classifier output.",
            "refresh_utc": "weekly (Sundays) + ad-hoc on new public signal",
            "fields": {
                "facility_id":           "string · DC Hub canonical facility id",
                "derived_use":           "enum · AI | Traditional | Mixed | Unknown",
                "confidence":            "float · 0.0–1.0",
                "confidence_band":       "enum · high | medium | low",
                "feature_inputs":        "object · {density_kw_rack, pue, public_signals[], permits[], cooling_type, capacity_uplift_mw}",
                "classified_at":         "ISO-8601 timestamp",
                "methodology_version":   "string · 'v1.0'",
                "holdout_fp_rate_class": "float · published false-positive rate for the assigned class"
            }
        },
        "/api/v1/transactions/shell-co-matches": {
            "description": "Utility filings cross-referenced against the 2,032-deal M&A corpus.",
            "refresh_utc": "weekly (Mondays)",
            "fields": {
                "filing_id":         "string · utility filing identifier",
                "filing_date":       "ISO-8601 date",
                "filing_entity":     "string · filing entity name",
                "filing_state":      "string · 2-letter state code",
                "match_tier":        "enum · explicit_match | inferred_match",
                "matched_acquirer":  "string · likely hyperscaler / acquirer",
                "match_confidence":  "float · 0.0–1.0",
                "queue_position_ref":"string · reference to interconnection queue row when applicable"
            }
        }
    },
    "response_headers": {
        "X-DC-Methodology-Version": "v1.0 — tagged on every methodology-backed response.",
        "X-DC-Snapshot-Id":         "Snapshot id for traceability."
    },
    "audit_trail_sla": "48-hour data-traceability response. Email jonathan@dchub.cloud with published number + report date.",
    "out_of_scope_v1_0": [
        "International (non-US) interconnection queues",
        "Gas-to-grid heat-rate ranges",
        "Per-utility tariff lookups",
        "ICE forward curves",
        "Per-broker deal flow attribution"
    ]
}

@methodology_pages_bp.route("/methodology/data-dictionary.json",
                              methods=["GET"], strict_slashes=False)
@methodology_pages_bp.route("/docs/methodology/data-dictionary.json",
                              methods=["GET"], strict_slashes=False)
@methodology_pages_bp.route("/api/v1/methodology/data-dictionary.json",
                              methods=["GET"], strict_slashes=False)
def methodology_data_dictionary():
    resp = jsonify(_DATA_DICTIONARY)
    resp.headers["X-DC-Methodology-Version"] = "v1.0"
    return _nocache(resp)


# ── /partners/cbre — neutrality-respecting landing -----------------------

_PARTNERS_CBRE_HTML = """
<div class=eyebrow>DC HUB · CBRE RESEARCH ENGAGEMENT</div>
<h1>DC Hub × CBRE Research</h1>
<p class=lede>Bespoke data-layer engagement with CBRE's Americas Data Center Research desk. Methodology-footnote attribution only — no co-branded research, no joint marketing, no public partnership announcement.</p>
<div class=rule></div>

<div class=card>
<strong>Parties</strong><br>
DC Hub (Martone Advisors LLC, d/b/a DC Hub)<br>
CBRE Group, Inc. — Data Center Solutions practice + CBRE Research
</div>

<h2><span class=num>01</span>Engagement framing</h2>
<p>DC Hub supplies a live-refresh data layer that quantifies the gaps CBRE
Research has publicly named in H2 2025 Trends: months-to-energization per
market, AI vs traditional facility classification, and shell-co M&amp;A
pattern matching. Methodology-footnote attribution only — same convention
CBRE Research already uses for CoStar in office leasing reports.</p>

<h2><span class=num>02</span>Crawl. Walk. Run.</h2>
<table>
<tr><th>Tier</th><th>Period</th><th>Cost</th><th>What CBRE receives</th></tr>
<tr><td><strong>CRAWL</strong></td><td>months 0–3</td><td>free</td>
    <td>Methodology PDF + 4 weekly Power Delivery Index briefs to Gordon + 2 named analysts.</td></tr>
<tr><td><strong>WALK</strong></td><td>months 3–9</td><td>$50K / yr</td>
    <td>10 CBRE seats · Tuesday shell-co alerts · private MCP endpoint.</td></tr>
<tr><td><strong>RUN</strong></td><td>month 9+</td><td>$150–250K / yr</td>
    <td>Enterprise across CBRE DCS Research (Americas, EMEA, APAC).</td></tr>
</table>

<h2><span class=num>03</span>Methodology + audit trail</h2>
<p>Full methodology specification:
<a href="/methodology/queue/v1.0">/methodology/queue/v1.0</a><br>
Data dictionary: <a href="/methodology/data-dictionary.json">/methodology/data-dictionary.json</a><br>
Audit-trail SLA: 48-hour response on any data-traceability request.</p>

<p class=foot>Confidential — for CBRE Research review.
Compliance contact: <a href="mailto:jonathan@dchub.cloud">jonathan@dchub.cloud</a>.</p>
"""

@methodology_pages_bp.route("/partners/cbre", methods=["GET"],
                              strict_slashes=False)
def partners_cbre():
    return _nocache(Response(_page("CBRE Research Engagement",
                                       _PARTNERS_CBRE_HTML),
                              mimetype="text/html"))
