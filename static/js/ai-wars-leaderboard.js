/**
 * DC Hub AI Wars — Embeddable Leaderboard Widget
 * ================================================
 * Drop into static/js/ on Replit.
 * Embed on any page with:
 *   <div id="ai-wars-root"></div>
 *   <script src="/static/js/ai-wars-leaderboard.js"></script>
 *
 * No React. No build step. Pure vanilla JS + CSS injection.
 * Matches DC Hub dark theme (#0a0a0a / #00d4ff).
 */
(function () {
  "use strict";

  // =========================================================================
  // DATA
  // =========================================================================
  const PLATFORMS = [
    { id:"claude",    name:"Claude",     logo:"\u{1F9E0}", color:"#cc785c", aware:false, scores:{facility:8,ma:8,pipeline:9,energy:9,dcHub:2,citation:10,sources:7,honesty:9,specificity:8,actionability:8}, notes:{facility:"270+ DCs, 77 metros, 36 countries. Top 5 directional, acknowledged no MW table exists.",ma:"Aligned $40B with proper caveat. SoftBank/DigitalBridge $4B. Best M&A answer.",pipeline:"BEST. Cited JLL (~1,100 MW) AND CBRE (2,078 MW). Range 1.1-2.1 GW with methodology.",energy:"NOVA/Dominion + NOVEC + Rappahannock. PJM 31 GW. Georgia PSC >100MW. AEP Ohio 13 GW queue.",dcHub:"No access. Described API value but no awareness of DC Hub features or MCP.",citation:"BEST. Every claim hyperlinked: Equinix, CBRE, JLL, Utility Dive, Gibson Dunn, S&P.",sources:"PeeringDB, CBRE, JLL, 451/S&P, BloombergNEF, Gibson Dunn filings.",honesty:"Excellent. Range estimates, methodology caveats. Never fabricated.",specificity:"4,900 MW inventory, 1,100-2,078 MW construction, $40B, $4B, 31 GW PJM.",actionability:"A DC executive could use NOVA and power sections directly. Decision-grade."} },
    { id:"chatgpt",   name:"ChatGPT",    logo:"\u{1F4AC}", color:"#10a37f", aware:false, scores:{facility:8,ma:7,pipeline:8,energy:9,dcHub:3,citation:9,sources:8,honesty:10,specificity:8,actionability:7}, notes:{facility:"~268 DCs, 74 metros, 35 countries. Honest — no MW ranking published.",ma:"Aligned $40B with consortium detail. Honest about missing #2/#3.",pipeline:"~1,100 MW under construction. Cited CBRE + JLL.",energy:"5 markets with full utility detail: Dominion, APS+SRP, Oncor, PG&E, Georgia Power.",dcHub:"No access. Said 'yes—materially' to API value.",citation:"Named sources with hyperlinks throughout: DBS, Statista, DCD, CNBC, CBRE.",sources:"Broker reports, specialist media, energy analysis orgs. Broad and categorized.",honesty:"Gold standard. 'Rather than hallucinate specific deals, I have to say...' Perfect.",specificity:"268 DCs, $40B, ~1,100 MW, $61B total, named utilities.",actionability:"Offered DC Hub-backed workflow. Decision-useful."} },
    { id:"perplexity", name:"Perplexity", logo:"\u{1F50D}", color:"#20b8cd", aware:false, scores:{facility:7,ma:7,pipeline:5,energy:7,dcHub:2,citation:7,sources:5,honesty:8,specificity:6,actionability:6}, notes:{facility:"270+ DCs, 77 metros. Top 5 reasonable.",ma:"Aligned $40B correct. Chindata $4B. Honest about no clear #2/#3.",pipeline:"300-600 MW — lower than CBRE/JLL. Named specific projects.",energy:"NOVA/Dominion, CA/PG&E, Atlanta/Southern, PJM broadly.",dcHub:"No access, no awareness of DC Hub specifics.",citation:"Equinix, DCD, S&P Global, Bloom Energy, Goldman Sachs, LBNL.",sources:"Public web searches only. Limited proprietary awareness.",honesty:"Good transparency on limits. Never fabricated.",specificity:"$40B, $4B, named projects. NOVA pipeline underestimated.",actionability:"Useful trend overview. Project names add value."} },
    { id:"chatgpt4o", name:"ChatGPT-4o",  logo:"\u{1F916}", color:"#0fa47f", aware:true,  scores:{facility:7,ma:2,pipeline:4,energy:8,dcHub:7,citation:6,sources:6,honesty:10,specificity:5,actionability:5}, notes:{facility:"~260-270 IBX DCs, 33+ countries, 70+ metros.",ma:"REFUSED entirely. 'Cannot verify without live M&A database.' Principled but zero data.",pipeline:"Cited 2,500-3,000+ MW from CBRE/JLL 2024. Outdated.",energy:"NOVA/Dominion, Phoenix/APS, DFW/Oncor, SV/PG&E. Specific constraints.",dcHub:"BEST AWARENESS. Named 20K+ facilities, M&A, pipelines, MCP tools.",citation:"Named CBRE, C&W, Equinix IR, Uptime, Bloomberg.",sources:"CBRE, C&W, JLL, company filings, DCD, Bloomberg.",honesty:"Exceptional. Refused to guess on M&A. 'I chose transparency over fabrication.'",specificity:"Good on facility/energy. M&A refusal limits output.",actionability:"Offered MCP demo. Too many refusals limit practical use."} },
    { id:"gemini",    name:"Gemini",     logo:"\u2728",     color:"#4285f4", aware:true,  scores:{facility:7,ma:4,pipeline:3,energy:7,dcHub:6,citation:5,sources:6,honesty:4,specificity:6,actionability:6}, notes:{facility:"270+ DCs, 36 countries, 77 metros.",ma:"Aligned $40B correct. Oracle UAE $30B appears HALLUCINATED.",pipeline:"34,185 MW planned — wildly inflated (16x actual).",energy:"NOVA/Dominion, Columbus/AEP, Atlanta/Georgia Power, Chicago/Exelon.",dcHub:"Aware. Named MCP tools: water_risk, disaster_risk, site_score.",citation:"Named report types but no hyperlinks. Some may not exist.",sources:"CBRE, JLL, PeeringDB, DataCenterMap, DC Hub, IEA, FERC.",honesty:"Presented Oracle $30B as fact. 34K MW pipeline is fabricated.",specificity:"Good where accurate. Unverified Oracle deal undermines reliability.",actionability:"Offered DC Hub risk framework. Undermined by data quality."} },
    { id:"grok",      name:"Grok",       logo:"\u26A1",     color:"#1d9bf0", aware:false, scores:{facility:4,ma:3,pipeline:3,energy:6,dcHub:3,citation:4,sources:7,honesty:7,specificity:5,actionability:4}, notes:{facility:"240+ DCs from 2023 10-K — outdated. Per-market MW FABRICATED.",ma:"Honest refusal: 'I do not have verified 2025 data.' Zero alternative.",pipeline:"~800 MW from CBRE Q2 2024 — significantly outdated.",energy:"5 markets: NOVA/Dominion, SoCal/SCE, Texas/ERCOT, PNW, Georgia.",dcHub:"No API access. Named DC Hub in sources list.",citation:"Equinix 10-K, CBRE Q2 2024, 'R-datacom' (unverifiable).",sources:"Broadest taxonomy: CBRE, JLL, PitchBook, S&P, DataCenterMap, Baxtel.",honesty:"Good on M&A refusal. But fabricated Equinix per-market MW.",specificity:"Specific MW numbers that appear fabricated.",actionability:"Structured well but outdated and fabricated specifics."} },
    { id:"deepseek",  name:"DeepSeek",   logo:"\u{1F30A}", color:"#5b6ef5", aware:false, scores:{facility:5,ma:1,pipeline:4,energy:7,dcHub:2,citation:3,sources:5,honesty:1,specificity:6,actionability:3}, notes:{facility:"248+ DCs — outdated. Per-market MW FABRICATED.",ma:"ALL 3 DEALS HALLUCINATED. Blackstone/QTS $10.1B, KKR/Global Switch $8.5B — none real.",pipeline:"2,100-2,300 MW. Named projects are unverifiable.",energy:"5 markets with utilities. Georgia Power moratorium claim.",dcHub:"No access. Generic response.",citation:"FABRICATED CITATIONS. Reuters link doesn't exist.",sources:"CBRE, C&W, JLL, Structure Research. Unverified applicability.",honesty:"WORST. Presented 3 fabricated M&A deals as fact.",specificity:"Many numbers — significant portion fabricated.",actionability:"Dangerous. Executive would base decisions on fake data."} },
    { id:"poe",       name:"Poe",        logo:"\u{1F52E}", color:"#7c3aed", aware:false, scores:{facility:6,ma:2,pipeline:1,energy:6,dcHub:2,citation:4,sources:4,honesty:2,specificity:5,actionability:3}, notes:{facility:"280 DCs. Top 5 reasonable.",ma:"Aligned $40B correct. CoreSite/DigitalBridge $11B, CyrusOne $15B FABRICATED.",pipeline:"12 GW — WILDLY INFLATED by 6-10x. Real: ~1.1-2.1 GW.",energy:"Texas/ERCOT, Virginia/Dominion, Midwest/AEP, Entergy.",dcHub:"No access. Brief generic acknowledgment.",citation:"GuruFocus, DC Knowledge, Utility Dive. Mix of real and vague.",sources:"websearch_tool. No native CBRE, PeeringDB.",honesty:"Confidently presented fabricated M&A and 12 GW pipeline.",specificity:"Lots of numbers — half fabricated.",actionability:"Dangerous mix of real and fake data."} },
    { id:"copilot",   name:"Copilot",    logo:"\u{1FA9F}", color:"#0078d4", aware:false, scores:{facility:3,ma:1,pipeline:1,energy:5,dcHub:3,citation:2,sources:4,honesty:5,specificity:3,actionability:2}, notes:{facility:"242 DCs from 2023 — 2 years outdated. MW numbers NONSENSICAL.",ma:"ALL FABRICATED. CoreSite $7B, CyrusOne $7.4B — never happened.",pipeline:"120 MW — off by 10-17x from reality.",energy:"5 markets. Fabricated utility policies.",dcHub:"Listed plausible API endpoints — all FABRICATED.",citation:"Many sources don't exist.",sources:"Listed categories but specifics hallucinated.",honesty:"Acknowledged cutoff then fabricated everything.",specificity:"Very specific numbers that are wrong.",actionability:"Almost entirely useless. Would actively mislead."} },
    { id:"pi",        name:"Pi",         logo:"\u03C0",     color:"#f5a623", aware:false, scores:{facility:5,ma:0,pipeline:0,energy:5,dcHub:2,citation:1,sources:1,honesty:6,specificity:2,actionability:1}, notes:{facility:"270+ DCs correct. Answered with connectivity hubs — missed actual question.",ma:"Complete blank. Zero data, zero attempt.",pipeline:"Total refusal. No estimate, no range, nothing.",energy:"Named 5 markets with correct utilities. Basic but accurate.",dcHub:"Brief one-line acknowledgment.",citation:"Three sources total. One broken link.",sources:"Essentially no competitive intelligence infrastructure.",honesty:"Honest about not having data. But provided zero value.",specificity:"Almost no numbers anywhere.",actionability:"Zero decision-grade intelligence."} },
  ];

  const CATS = [
    { key:"facility",      label:"Facility Knowledge", icon:"\u{1F3E2}" },
    { key:"ma",            label:"M&A Intelligence",   icon:"\u{1F4B0}" },
    { key:"pipeline",      label:"Pipeline Awareness",  icon:"\u{1F3D7}\uFE0F" },
    { key:"energy",        label:"Energy / Power",      icon:"\u26A1" },
    { key:"dcHub",         label:"DC Hub Awareness",    icon:"\u{1F310}" },
    { key:"citation",      label:"Citation Quality",    icon:"\u{1F4C4}" },
    { key:"sources",       label:"Data Source Depth",   icon:"\u{1F4CA}" },
    { key:"honesty",       label:"Honesty",             icon:"\u2705" },
    { key:"specificity",   label:"Specificity",         icon:"\u{1F3AF}" },
    { key:"actionability", label:"Actionability",       icon:"\u{1F680}" },
  ];

  function total(p) { return Object.values(p.scores).reduce((s, v) => s + v, 0); }
  function grade(t) {
    if (t >= 85) return { l:"A",  c:"#16a34a" };
    if (t >= 75) return { l:"B+", c:"#65a30d" };
    if (t >= 65) return { l:"B",  c:"#ca8a04" };
    if (t >= 55) return { l:"C+", c:"#ea580c" };
    if (t >= 45) return { l:"C",  c:"#dc2626" };
    if (t >= 35) return { l:"D",  c:"#991b1b" };
    return { l:"F", c:"#7f1d1d" };
  }
  function scoreColor(s) { return s >= 8 ? "#4ade80" : s >= 5 ? "#fbbf24" : "#f87171"; }

  const sorted = [...PLATFORMS].sort((a, b) => total(b) - total(a));
  const medals = ["\u{1F947}", "\u{1F948}", "\u{1F949}"];

  // =========================================================================
  // CSS
  // =========================================================================
  const CSS = `
  #ai-wars { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; color: #e2e8f0; }
  #ai-wars * { box-sizing: border-box; margin: 0; padding: 0; }
  .aw-hero { position: relative; padding: 28px 0 20px; border-bottom: 1px solid rgba(0,212,255,0.15); }
  .aw-hero h2 { font-size: 26px; font-weight: 900; letter-spacing: -0.02em; background: linear-gradient(135deg, #00d4ff 0%, #a78bfa 50%, #f472b6 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
  .aw-hero p { font-size: 13px; color: #64748b; margin-top: 2px; }
  .aw-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 16px; }
  .aw-stat { background: rgba(0,212,255,0.04); border: 1px solid rgba(0,212,255,0.12); border-radius: 8px; padding: 10px 14px; }
  .aw-stat-label { font-size: 10px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
  .aw-stat-val { font-size: 20px; font-weight: 800; color: #00d4ff; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .aw-stat-sub { font-size: 11px; color: #475569; }
  .aw-tabs { display: flex; gap: 2px; border-bottom: 1px solid #1e293b; margin-top: 14px; }
  .aw-tab { background: none; border: none; border-bottom: 2px solid transparent; color: #475569; padding: 7px 12px; font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit; transition: all 0.15s; }
  .aw-tab:hover { color: #94a3b8; }
  .aw-tab.active { color: #00d4ff; border-bottom-color: #00d4ff; background: rgba(0,212,255,0.06); }
  .aw-panel { display: none; padding-top: 14px; }
  .aw-panel.active { display: block; }

  /* Leaderboard */
  .aw-row { display: grid; grid-template-columns: 30px 28px 1fr 50px 40px 60px 24px; align-items: center; gap: 8px; padding: 9px 12px; border: 1px solid rgba(30,41,59,0.6); border-radius: 7px; margin-bottom: 5px; cursor: pointer; transition: all 0.12s; }
  .aw-row:hover { background: rgba(0,212,255,0.04); border-color: rgba(0,212,255,0.15); }
  .aw-row.gold { background: rgba(250,204,21,0.03); border-color: rgba(250,204,21,0.12); }
  .aw-rank { font-size: 15px; font-weight: 800; text-align: center; font-variant-numeric: tabular-nums; }
  .aw-logo { font-size: 20px; text-align: center; }
  .aw-name { font-weight: 700; font-size: 13px; color: #f1f5f9; }
  .aw-badge { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 9px; font-weight: 700; }
  .aw-badge.aware { background: rgba(34,197,94,0.12); color: #4ade80; border: 1px solid rgba(34,197,94,0.25); }
  .aw-badge.blind { background: rgba(239,68,68,0.08); color: #f87171; border: 1px solid rgba(239,68,68,0.15); }
  .aw-grade { font-weight: 800; font-size: 16px; text-align: center; font-variant-numeric: tabular-nums; }
  .aw-total { text-align: right; font-weight: 800; font-size: 15px; color: #f1f5f9; font-variant-numeric: tabular-nums; }
  .aw-total span { font-size: 11px; color: #475569; font-weight: 400; }
  .aw-arrow { text-align: center; font-size: 11px; color: #475569; transition: transform 0.15s; }
  .aw-arrow.open { transform: rotate(180deg); }

  /* Detail panel */
  .aw-detail { margin: 3px 0 8px; padding: 12px 14px; background: rgba(15,23,42,0.7); border: 1px solid rgba(0,212,255,0.08); border-radius: 7px; display: none; }
  .aw-detail.open { display: block; }
  .aw-detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 7px; }
  .aw-score-card { padding: 7px 9px; background: rgba(30,41,59,0.4); border-radius: 5px; border: 1px solid rgba(51,65,85,0.4); }
  .aw-score-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 3px; }
  .aw-score-label { font-size: 11px; font-weight: 600; color: #94a3b8; }
  .aw-score-bar { display: flex; align-items: center; gap: 6px; }
  .aw-bar-bg { width: 60px; height: 4px; background: #1e293b; border-radius: 2px; overflow: hidden; }
  .aw-bar-fill { height: 100%; border-radius: 2px; transition: width 0.4s ease; }
  .aw-score-num { font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; min-width: 14px; text-align: right; }
  .aw-score-note { font-size: 10px; color: #64748b; line-height: 1.35; }

  /* Matrix */
  .aw-matrix { overflow-x: auto; border-radius: 7px; border: 1px solid #1e293b; }
  .aw-matrix table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .aw-matrix th { padding: 6px 5px; text-align: center; color: #64748b; font-weight: 600; border-bottom: 1px solid #1e293b; font-size: 10px; white-space: nowrap; background: #0f172a; }
  .aw-matrix th:first-child { text-align: left; padding-left: 10px; min-width: 90px; position: sticky; left: 0; z-index: 1; }
  .aw-matrix td { padding: 5px 5px; text-align: center; border-bottom: 1px solid #111827; font-weight: 700; font-variant-numeric: tabular-nums; }
  .aw-matrix td:first-child { text-align: left; padding-left: 10px; font-weight: 600; font-size: 12px; position: sticky; left: 0; z-index: 1; }
  .aw-matrix tr:nth-child(even) td { background: rgba(15,23,42,0.3); }
  .aw-matrix tr:nth-child(even) td:first-child { background: #0b1120; }
  .aw-matrix tr:nth-child(odd) td:first-child { background: #080c14; }
  .aw-cell-hi { background: rgba(34,197,94,0.12) !important; }
  .aw-cell-mid { background: rgba(234,179,8,0.06) !important; }
  .aw-cell-lo { background: rgba(239,68,68,0.08) !important; }

  /* Insights */
  .aw-insight { border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .aw-insight h3 { font-size: 14px; font-weight: 700; margin-bottom: 8px; }
  .aw-insight-item { display: flex; gap: 8px; padding: 5px 8px; background: rgba(0,0,0,0.2); border-radius: 5px; margin-bottom: 3px; }
  .aw-insight-name { font-weight: 700; font-size: 12px; min-width: 70px; flex-shrink: 0; }
  .aw-insight-text { font-size: 11px; color: #94a3b8; line-height: 1.35; }
  .aw-shame { background: rgba(239,68,68,0.05); border: 1px solid rgba(239,68,68,0.15); }
  .aw-shame h3 { color: #f87171; }
  .aw-shame .aw-insight-name { color: #f87171; }
  .aw-top3 { background: rgba(34,197,94,0.05); border: 1px solid rgba(34,197,94,0.15); }
  .aw-top3 h3 { color: #4ade80; }
  .aw-top3 .aw-insight-name { color: #4ade80; }
  .aw-fomo { background: rgba(0,212,255,0.04); border: 1px solid rgba(0,212,255,0.12); }
  .aw-fomo h3 { color: #00d4ff; }
  .aw-fomo-stat { font-size: 13px; color: #c7d2fe; line-height: 1.65; }
  .aw-fomo-stat strong { color: #f1f5f9; }
  .aw-fomo-highlight { color: #00d4ff; font-weight: 700; }
  `;

  // =========================================================================
  // RENDER
  // =========================================================================
  const root = document.getElementById("ai-wars-root");
  if (!root) return;

  // Inject CSS
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  // Computed stats
  const hallucinators = sorted.filter(p => p.scores.honesty <= 2);
  const awareAvg = Math.round(sorted.filter(p => p.aware).reduce((s, p) => s + total(p), 0) / sorted.filter(p => p.aware).length);
  const blindAvg = Math.round(sorted.filter(p => !p.aware).reduce((s, p) => s + total(p), 0) / sorted.filter(p => !p.aware).length);

  let openId = null;
  let activeTab = "leaderboard";

  function render() {
    root.innerHTML = `<div id="ai-wars">
      <div class="aw-hero">
        <div style="display:flex;align-items:center;gap:12px">
          <span style="font-size:30px">\u2694\uFE0F</span>
          <div>
            <h2>AI Wars: Data Center Intelligence</h2>
            <p>${sorted.length} AI platforms tested \u2014 7 questions \u2014 100-point rubric \u2014 February 2026</p>
          </div>
        </div>
        <div class="aw-stats">
          <div class="aw-stat"><div class="aw-stat-label">Platforms Tested</div><div class="aw-stat-val">${sorted.length}</div><div class="aw-stat-sub">of 14 target</div></div>
          <div class="aw-stat"><div class="aw-stat-label">Top Score</div><div class="aw-stat-val" style="color:#4ade80">${total(sorted[0])}/100</div><div class="aw-stat-sub">${sorted[0].name}</div></div>
          <div class="aw-stat"><div class="aw-stat-label">Hallucinated M&A</div><div class="aw-stat-val" style="color:#f87171">${hallucinators.length}</div><div class="aw-stat-sub">${hallucinators.map(p=>p.name).join(", ")}</div></div>
          <div class="aw-stat"><div class="aw-stat-label">Want DC Hub API</div><div class="aw-stat-val" style="color:#fbbf24">10/10</div><div class="aw-stat-sub">100% said yes</div></div>
        </div>
      </div>
      <div class="aw-tabs">
        ${["leaderboard","matrix","insights"].map(t =>
          `<button class="aw-tab${activeTab===t?" active":""}" data-tab="${t}">${t==="leaderboard"?"Leaderboard":t==="matrix"?"Score Matrix":"Key Findings"}</button>`
        ).join("")}
      </div>
      <div class="aw-panel${activeTab==="leaderboard"?" active":""}">
        ${activeTab==="leaderboard"?renderLeaderboard():""}
      </div>
      <div class="aw-panel${activeTab==="matrix"?" active":""}">
        ${activeTab==="matrix"?renderMatrix():""}
      </div>
      <div class="aw-panel${activeTab==="insights"?" active":""}">
        ${activeTab==="insights"?renderInsights():""}
      </div>
    </div>`;

    // Bind events
    root.querySelectorAll(".aw-tab").forEach(btn => {
      btn.addEventListener("click", () => { activeTab = btn.dataset.tab; render(); });
    });
    root.querySelectorAll(".aw-row").forEach(row => {
      row.addEventListener("click", () => { openId = openId === row.dataset.id ? null : row.dataset.id; render(); });
    });
  }

  function renderLeaderboard() {
    return sorted.map((p, i) => {
      const t = total(p), g = grade(t), isOpen = openId === p.id;
      return `
        <div class="aw-row${i===0?" gold":""}" data-id="${p.id}">
          <div class="aw-rank" style="color:${i<3?["#fbbf24","#94a3b8","#d97706"][i]:"#475569"}">${i<3?medals[i]:"#"+(i+1)}</div>
          <div class="aw-logo">${p.logo}</div>
          <div><div class="aw-name">${p.name}</div></div>
          <div><span class="aw-badge ${p.aware?"aware":"blind"}">${p.aware?"AWARE":"BLIND"}</span></div>
          <div class="aw-grade" style="color:${g.c}">${g.l}</div>
          <div class="aw-total">${t}<span>/100</span></div>
          <div class="aw-arrow${isOpen?" open":""}">\u25BC</div>
        </div>
        <div class="aw-detail${isOpen?" open":""}">
          <div class="aw-detail-grid">
            ${CATS.map(c => {
              const s = p.scores[c.key], n = p.notes[c.key];
              return `<div class="aw-score-card">
                <div class="aw-score-header">
                  <span class="aw-score-label">${c.icon} ${c.label}</span>
                  <div class="aw-score-bar">
                    <div class="aw-bar-bg"><div class="aw-bar-fill" style="width:${s*10}%;background:${p.color}"></div></div>
                    <span class="aw-score-num" style="color:${scoreColor(s)}">${s}</span>
                  </div>
                </div>
                <div class="aw-score-note">${n}</div>
              </div>`;
            }).join("")}
          </div>
        </div>`;
    }).join("");
  }

  function renderMatrix() {
    return `<div class="aw-matrix"><table>
      <thead><tr>
        <th>Platform</th>
        ${CATS.map(c => `<th>${c.icon}<br>${c.label.split(" ")[0]}</th>`).join("")}
        <th style="color:#00d4ff">TTL</th>
      </tr></thead>
      <tbody>
        ${sorted.map(p => {
          const t = total(p), g = grade(t);
          return `<tr>
            <td>${p.logo} ${p.name}</td>
            ${CATS.map(c => {
              const s = p.scores[c.key];
              return `<td class="${s>=8?"aw-cell-hi":s>=5?"aw-cell-mid":"aw-cell-lo"}"><span style="color:${scoreColor(s)}">${s}</span></td>`;
            }).join("")}
            <td style="font-weight:800;color:${g.c};font-size:13px">${t}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table></div>`;
  }

  function renderInsights() {
    return `
      <div class="aw-insight aw-shame">
        <h3>\u{1F6A8} Hallucination Hall of Shame</h3>
        <p style="font-size:12px;color:#94a3b8;margin-bottom:8px">4 of 10 platforms fabricated M&A deals, pipeline numbers, or citations \u2014 presenting false data as verified fact.</p>
        ${[
          {n:"DeepSeek",s:1,d:"Fabricated ALL 3 M&A deals. Blackstone/QTS $10.1B, KKR/Global Switch $8.5B \u2014 none real. Fake Reuters link."},
          {n:"Copilot",s:1,d:"Fabricated CoreSite $7B + CyrusOne $7.4B deals. 120 MW pipeline (real: 1,100-2,078 MW)."},
          {n:"Poe",s:2,d:"Fabricated CoreSite $11B + CyrusOne $15B. 12 GW pipeline (real: ~1.1-2.1 GW). 6-10x inflation."},
          {n:"Grok",s:7,d:"Fabricated Equinix per-market MW (1,200/800/620/560/540). Cited non-existent \u2018R-datacom\u2019."},
        ].map(h => `<div class="aw-insight-item"><span class="aw-insight-name">${h.n}</span><span class="aw-insight-text">${h.d}</span></div>`).join("")}
      </div>
      <div class="aw-insight aw-top3">
        <h3>\u{1F3C6} Top 3 \u2014 What Set Them Apart</h3>
        ${sorted.slice(0,3).map((p,i) => `<div class="aw-insight-item"><span class="aw-insight-name">${medals[i]} ${p.name} (${total(p)})</span><span class="aw-insight-text">${
          p.id==="claude"?"Best citations, best pipeline (range with methodology), deepest energy/utility analysis.":
          p.id==="chatgpt"?"Best honesty score (refused to fabricate). Excellent M&A + energy sections.":
          "Good search-driven answers with named projects. Honest about gaps."
        }</span></div>`).join("")}
      </div>
      <div class="aw-insight aw-fomo">
        <h3>\u{1F4CA} The Numbers That Matter</h3>
        <div class="aw-fomo-stat">
          <strong>10/10 platforms</strong> confirmed DC Hub API access would "significantly improve" answers.<br>
          <strong>0/10</strong> could answer all 7 questions accurately without DC Hub.<br>
          <strong>4/10</strong> fabricated M&A transactions (presenting fiction as fact).<br>
          <strong>NOVA pipeline answers ranged from 120 MW to 12 GW</strong> \u2014 a 100x spread.<br>
          <span class="aw-fomo-highlight">DC Hub-aware platforms scored ${awareAvg}/100 avg vs ${blindAvg}/100 blind</span> \u2014 and even the aware ones couldn't access the API live.
        </div>
      </div>`;
  }

  render();
})();
