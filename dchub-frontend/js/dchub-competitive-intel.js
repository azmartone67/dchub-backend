/**
 * DC Hub Competitive Intelligence Suite v1.1.0
 * Frontend integration for 6 backend modules.
 *
 * VERIFIED ENDPOINTS (Feb 3, 2026):
 *   Real Estate:  /api/real-estate/markets, /land-values, /trends           ✅
 *   Fiber:        /api/fiber/routes                                          ✅
 *   SEC:          /api/sec/expansion-signals, /filings                       ✅
 *   Competitors:  /api/competitors/summary, /gaps, /matrix                   ✅
 *   Jobs:         /api/jobs/trends                                           ✅
 *   Permits:      /api/permits/pipeline                                      ✅
 */

(function () {
  'use strict';

  var API_BASE = window.DCHUB_API_BASE || 'https://dchub.cloud';
  var VERSION = '1.2.0';

  var C = {
    realEstate: '#e6a817', fiber: '#00c9a7', sec: '#ff6b6b',
    competitors: '#845ef7', jobs: '#339af0', permits: '#ff922b',
    panel: '#0f1923', border: '#1e3a5f', text: '#e0e6ed',
    accent: '#00c9a7', dim: '#7a8fa6'
  };

  function $(s, c) { return (c || document).querySelector(s); }
  function $$(s, c) { return Array.prototype.slice.call((c || document).querySelectorAll(s)); }

  function apiFetch(path) {
    return fetch(API_BASE + path, { mode: 'cors' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .catch(function (e) { console.warn('[CI] ' + path + ':', e.message); return null; });
  }

  function fm(n) {
    if (!n) return '';
    if (n >= 1e9) return '$' + (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return '$' + (n / 1e6).toFixed(0) + 'M';
    if (n >= 1e3) return '$' + (n / 1e3).toFixed(0) + 'K';
    return '$' + n;
  }
  function fn(n) { return n ? n.toLocaleString() : '0'; }

  // ── CSS ───────────────────────────────────────────────────────
  function injectStyles() {
    if ($('#ci-styles')) return;
    var s = document.createElement('style');
    s.id = 'ci-styles';
    s.textContent =
      '#ci-panel{position:fixed;top:60px;right:0;width:380px;height:calc(100vh - 60px);background:' + C.panel + ';border-left:1px solid ' + C.border + ';color:' + C.text + ';z-index:1100;transform:translateX(100%);transition:transform .35s cubic-bezier(.22,1,.36,1);overflow-y:auto;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:13px}' +
      '#ci-panel.open{transform:translateX(0)}' +
      '#ci-panel::-webkit-scrollbar{width:6px}#ci-panel::-webkit-scrollbar-thumb{background:' + C.border + ';border-radius:3px}' +
      '.ci-hdr{position:sticky;top:0;background:' + C.panel + ';padding:16px 18px 12px;border-bottom:1px solid ' + C.border + ';z-index:2;display:flex;align-items:center;justify-content:space-between}' +
      '.ci-hdr h2{margin:0;font-size:15px;font-weight:700;letter-spacing:.5px;color:#fff}' +
      '.ci-hdr .ci-x{background:none;border:none;color:' + C.dim + ';font-size:20px;cursor:pointer;padding:2px 6px;border-radius:4px}' +
      '.ci-hdr .ci-x:hover{background:rgba(255,255,255,.08);color:#fff}' +
      '.ci-tabs{display:flex;overflow-x:auto;gap:2px;padding:8px 12px;border-bottom:1px solid ' + C.border + ';background:rgba(0,0,0,.15)}' +
      '.ci-tabs::-webkit-scrollbar{display:none}' +
      '.ci-tab{flex:0 0 auto;padding:6px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:' + C.dim + ';background:none;border:1px solid transparent;border-radius:6px;cursor:pointer;white-space:nowrap;transition:all .2s}' +
      '.ci-tab:hover{color:#fff;background:rgba(255,255,255,.05)}' +
      '.ci-tab.active{color:' + C.accent + ';border-color:' + C.accent + ';background:rgba(0,201,167,.08)}' +
      '.ci-body{padding:14px 16px 20px}' +
      '.ci-sec{margin-bottom:18px}' +
      '.ci-sec-t{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:' + C.dim + ';margin-bottom:10px;display:flex;align-items:center;gap:6px}' +
      '.ci-badge{background:' + C.accent + ';color:#000;font-size:10px;padding:1px 6px;border-radius:10px;font-weight:700}' +
      '.ci-card{background:rgba(255,255,255,.03);border:1px solid ' + C.border + ';border-radius:8px;padding:12px 14px;margin-bottom:8px;transition:border-color .2s}' +
      '.ci-card:hover{border-color:' + C.accent + ';background:rgba(0,201,167,.04)}' +
      '.ci-card-t{font-weight:600;color:#fff;font-size:13px;margin-bottom:4px}' +
      '.ci-card-s{color:' + C.dim + ';font-size:11px;margin-bottom:6px}' +
      '.ci-meta{display:flex;gap:12px;flex-wrap:wrap}' +
      '.ci-meta span{font-size:11px;color:' + C.dim + '}' +
      '.ci-v{color:' + C.accent + ';font-weight:600}' +
      '.ci-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}' +
      '.ci-st{text-align:center;background:rgba(255,255,255,.03);border:1px solid ' + C.border + ';border-radius:8px;padding:10px 6px}' +
      '.ci-st-v{font-size:18px;font-weight:700;color:#fff}' +
      '.ci-st-l{font-size:10px;color:' + C.dim + ';text-transform:uppercase;letter-spacing:.5px;margin-top:2px}' +
      '.ci-load{text-align:center;padding:30px 0;color:' + C.dim + '}' +
      '.ci-load::before{content:"";display:block;width:24px;height:24px;border:2px solid ' + C.border + ';border-top-color:' + C.accent + ';border-radius:50%;margin:0 auto 10px;animation:ci-sp .8s linear infinite}' +
      '@keyframes ci-sp{to{transform:rotate(360deg)}}' +
      '.ci-empty{text-align:center;padding:24px 0;color:' + C.dim + ';font-size:12px}' +
      '#ci-trigger{position:fixed;bottom:80px;right:12px;z-index:1090;width:48px;height:48px;border-radius:12px;background:linear-gradient(135deg,#0d1b2a,#1a2d45);border:2px solid ' + C.accent + ';color:' + C.accent + ';font-size:20px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .25s;box-shadow:0 4px 20px rgba(0,201,167,.35);animation:ci-pulse 2s infinite}' +
      '@keyframes ci-pulse{0%,100%{box-shadow:0 4px 20px rgba(0,201,167,.35)}50%{box-shadow:0 4px 28px rgba(0,201,167,.6)}}' +
      '#ci-trigger:hover{border-color:#fff;box-shadow:0 4px 24px rgba(0,201,167,.5);transform:scale(1.08)}' +
      '#ci-trigger.open{right:392px}' +
      '.ci-bar{height:8px;background:' + C.border + ';border-radius:4px;overflow:hidden;flex:1}' +
      '.ci-bar-fill{height:100%;border-radius:4px}' +
      '.ci-toggle{display:flex;align-items:center;gap:8px;padding:6px 0}' +
      '.ci-toggle input{accent-color:' + C.accent + ';width:14px;height:14px}' +
      '.ci-toggle label{font-size:12px;cursor:pointer;flex:1}' +
      '.ci-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}' +
      '@media(max-width:768px){#ci-panel{width:100%}#ci-trigger.open{right:12px}}';
    document.head.appendChild(s);
  }

  // ── Panel DOM ─────────────────────────────────────────────────
  function buildPanel() {
    if ($('#ci-panel')) return;
    var btn = document.createElement('button');
    btn.id = 'ci-trigger';
    btn.title = 'Competitive Intelligence';
    btn.innerHTML = '⚡';
    document.body.appendChild(btn);

    var p = document.createElement('div');
    p.id = 'ci-panel';
    p.innerHTML =
      '<div class="ci-hdr"><h2>\u26A1 Competitive Intel</h2><button class="ci-x" title="Close">\u00D7</button></div>' +
      '<div class="ci-tabs">' +
      '<button class="ci-tab active" data-tab="overview">Overview</button>' +
      '<button class="ci-tab" data-tab="real-estate">\uD83C\uDFE0 Real Estate</button>' +
      '<button class="ci-tab" data-tab="fiber">\uD83D\uDD17 Fiber</button>' +
      '<button class="ci-tab" data-tab="sec">\uD83D\uDCC8 SEC</button>' +
      '<button class="ci-tab" data-tab="competitors">\uD83C\uDFAF Competitors</button>' +
      '<button class="ci-tab" data-tab="jobs">\uD83D\uDCBC Jobs</button>' +
      '<button class="ci-tab" data-tab="permits">\uD83C\uDFD7\uFE0F Permits</button>' +
      '</div>' +
      '<div class="ci-body" id="ci-body"><div class="ci-load">Loading intelligence\u2026</div></div>';
    document.body.appendChild(p);

    btn.addEventListener('click', function () {
      p.classList.toggle('open');
      btn.classList.toggle('open');
      if (p.classList.contains('open') && !CI._loaded) CI.switchTab('overview');
    });
    p.querySelector('.ci-x').addEventListener('click', function () {
      p.classList.remove('open');
      btn.classList.remove('open');
    });
    $$('.ci-tab', p).forEach(function (t) {
      t.addEventListener('click', function () {
        $$('.ci-tab', p).forEach(function (x) { x.classList.remove('active'); });
        t.classList.add('active');
        CI.switchTab(t.getAttribute('data-tab'));
      });
    });
  }

  // ── Map helpers ───────────────────────────────────────────────
  var layers = {};
  function getMap() { return window.map || window.landPowerMap || null; }

  function plotMarkers(key, items, color, popupFn) {
    var map = getMap();
    if (!map || !window.L) return 0;
    if (layers[key]) map.removeLayer(layers[key]);
    var mk = [];
    items.forEach(function (it) {
      var lat = it.lat || it.latitude;
      var lng = it.lng || it.lon || it.longitude;
      if (!lat || !lng) return;
      var icon = L.divIcon({ className: 'ci-marker', iconSize: [12, 12], iconAnchor: [6, 6], html: '<div style="width:12px;height:12px;border-radius:50%;background:' + color + ';border:2px solid rgba(255,255,255,.9);box-shadow:0 2px 6px rgba(0,0,0,.4)"></div>' });
      var m = L.marker([lat, lng], { icon: icon });
      if (popupFn) m.bindPopup(popupFn(it));
      mk.push(m);
    });
    if (mk.length) layers[key] = L.layerGroup(mk).addTo(map);
    return mk.length;
  }

  function toggleLayer(key, show) {
    var map = getMap();
    if (!map || !layers[key]) return;
    if (show) map.addLayer(layers[key]); else map.removeLayer(layers[key]);
  }

  // ── Cache ─────────────────────────────────────────────────────
  var D = {};

  // ── Known city coordinates for fiber/SEC map plotting ────────
  var COORDS = {
    'Ashburn, VA': [39.04,-77.49], 'Chicago, IL': [41.88,-87.63], 'Dallas, TX': [32.78,-96.80],
    'Phoenix, AZ': [33.45,-112.07], 'Los Angeles, CA': [34.05,-118.24], 'Atlanta, GA': [33.75,-84.39],
    'Denver, CO': [39.74,-104.99], 'New York, NY': [40.71,-74.01], 'San Jose, CA': [37.34,-121.89],
    'Seattle, WA': [47.61,-122.33], 'Las Vegas, NV': [36.17,-115.14], 'Portland, OR': [45.52,-122.68],
    'Quincy, WA': [47.23,-119.85], 'Warsaw, Poland': [52.23,21.01], 'Jurong, Singapore': [1.34,103.74],
    'Dublin, Ireland': [53.35,-6.26]
  };

  // ── Controller ────────────────────────────────────────────────
  var CI = {
    _loaded: false,
    set: function (h) { var el = $('#ci-body'); if (el) el.innerHTML = h; },
    loading: function () { this.set('<div class="ci-load">Loading intelligence\u2026</div>'); },

    switchTab: function (tab) {
      this.loading();
      var m = { 'overview': this.loadOverview, 'real-estate': this.loadRealEstate, 'fiber': this.loadFiber, 'sec': this.loadSEC, 'competitors': this.loadCompetitors, 'jobs': this.loadJobs, 'permits': this.loadPermits };
      if (m[tab]) m[tab].call(this);
    },

    // ── OVERVIEW ────────────────────────────────────────────────
    loadOverview: function () {
      var self = this;
      Promise.all([
        apiFetch('/api/real-estate/summary'),
        apiFetch('/api/fiber/routes'),
        apiFetch('/api/sec/expansion-signals'),
        apiFetch('/api/competitors/summary'),
        apiFetch('/api/jobs/trends'),
        apiFetch('/api/permits/pipeline')
      ]).then(function (r) {
        var re = r[0], fib = r[1], sec = r[2], comp = r[3], jobs = r[4], perm = r[5];

        var reMarkets = (re && re.modules && re.modules.county_assessor) ? re.modules.county_assessor.markets : 10;
        var fibCount = (fib && fib.count) || 0;
        var fibMiles = 0;
        if (fib && fib.routes) fib.routes.forEach(function (x) { fibMiles += (x.route_miles || 0); });
        var secCount = (sec && sec.count) || 0;
        var secTotal = 0;
        if (sec && sec.signals) sec.signals.forEach(function (x) { secTotal += (x.investment_amount || 0); });
        var compTotal = (comp && comp.total_competitors) || 6;
        var compGaps = (comp && comp.coverage_gaps_identified) || 0;
        var dcScore = (comp && comp.feature_score && comp.feature_score.dc_hub) || 10;
        var avgScore = (comp && comp.feature_score && comp.feature_score.competitor_avg) || 2.3;
        var jobTotal = (jobs && jobs.total_q1_postings) || 0;

        self._loaded = true;
        self.set(
          '<div class="ci-sec"><div class="ci-sec-t">Module Summary <span class="ci-badge">6 ACTIVE</span></div>' +
          '<div class="ci-stats">' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.realEstate + '">' + reMarkets + '</div><div class="ci-st-l">Markets</div></div>' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.fiber + '">' + fn(fibMiles) + '</div><div class="ci-st-l">Fiber Miles</div></div>' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.sec + '">' + fm(secTotal) + '</div><div class="ci-st-l">SEC Signals</div></div>' +
          '</div>' +
          '<div class="ci-stats">' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.competitors + '">' + compTotal + '</div><div class="ci-st-l">Competitors</div></div>' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.jobs + '">' + fn(jobTotal) + '</div><div class="ci-st-l">Q1 Postings</div></div>' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.permits + '">' + compGaps + '</div><div class="ci-st-l">Gaps Found</div></div>' +
          '</div></div>' +
          '<div class="ci-sec"><div class="ci-sec-t">DC Hub vs. Competitors</div>' +
          '<div class="ci-card"><div style="margin:4px 0">' +
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px"><span style="width:70px;font-size:11px;color:' + C.accent + '">DC Hub</span><div class="ci-bar"><div class="ci-bar-fill" style="width:' + (dcScore*10) + '%;background:' + C.accent + '"></div></div><span style="font-size:12px;font-weight:700;color:#fff">' + dcScore + '/10</span></div>' +
            '<div style="display:flex;align-items:center;gap:8px"><span style="width:70px;font-size:11px;color:' + C.dim + '">Avg Comp</span><div class="ci-bar"><div class="ci-bar-fill" style="width:' + (avgScore*10) + '%;background:' + C.dim + '"></div></div><span style="font-size:12px;font-weight:700;color:' + C.dim + '">' + avgScore + '/10</span></div>' +
          '</div></div></div>' +
          '<div class="ci-sec"><div class="ci-sec-t">Map Layers</div>' +
            '<div class="ci-toggle"><input type="checkbox" id="ci-ly-fiber" data-layer="fiber"><label for="ci-ly-fiber"><span class="ci-dot" style="background:' + C.fiber + '"></span>Fiber Routes (' + fibCount + ')</label></div>' +
            '<div class="ci-toggle"><input type="checkbox" id="ci-ly-sec" data-layer="sec"><label for="ci-ly-sec"><span class="ci-dot" style="background:' + C.sec + '"></span>SEC Expansion Signals (' + secCount + ')</label></div>' +
          '</div>'
        );

        // Cache data for map plotting
        D.fiber = fib;
        D.secSignals = sec;

        $$('.ci-toggle input', $('#ci-panel')).forEach(function (cb) {
          cb.addEventListener('change', function () {
            var key = cb.getAttribute('data-layer');
            if (cb.checked && !layers[key]) {
              if (key === 'fiber') self._plotFiber();
              if (key === 'sec') self._plotSEC();
            } else {
              toggleLayer(key, cb.checked);
            }
          });
        });
      });
    },

    _plotFiber: function () {
      var d = D.fiber;
      if (!d || !d.routes) return;
      var items = [];
      d.routes.forEach(function (r) {
        var sc = COORDS[r.start_city], ec = COORDS[r.end_city];
        if (sc) items.push({ lat: sc[0], lng: sc[1], provider: r.provider, route_name: r.route_name, route_miles: r.route_miles, fiber_count: r.fiber_count });
        if (ec) items.push({ lat: ec[0], lng: ec[1], provider: r.provider, route_name: r.route_name, route_miles: r.route_miles, fiber_count: r.fiber_count });
      });
      plotMarkers('fiber', items, C.fiber, function (it) {
        return '<div style="font-family:sans-serif;min-width:180px"><strong>' + it.provider + '</strong><br>' + it.route_name + '<br>' + (it.route_miles ? fn(it.route_miles) + ' mi' : '') + (it.fiber_count ? ' \u2022 ' + it.fiber_count + ' fibers' : '') + '</div>';
      });
    },

    _plotSEC: function () {
      var d = D.secSignals;
      if (!d || !d.signals) return;
      var items = [];
      d.signals.forEach(function (s) {
        var loc = COORDS[s.location];
        if (loc) items.push({ lat: loc[0], lng: loc[1], company: s.company, location: s.location, investment_amount: s.investment_amount, capacity_mw: s.capacity_mw, signal_type: s.signal_type });
      });
      plotMarkers('sec', items, C.sec, function (it) {
        return '<div style="font-family:sans-serif;min-width:200px"><strong>' + it.company + '</strong><br>' + it.location + '<br>' + (it.investment_amount ? fm(it.investment_amount) : '') + (it.capacity_mw ? ' \u2022 ' + it.capacity_mw + ' MW' : '') + '</div>';
      });
    },

    // ── REAL ESTATE ─────────────────────────────────────────────
    loadRealEstate: function () {
      var self = this;
      Promise.all([
        D.reMarkets || apiFetch('/api/real-estate/markets'),
        D.reLand || apiFetch('/api/real-estate/land-values'),
        D.reTrends || apiFetch('/api/real-estate/trends')
      ]).then(function (r) {
        var mkts = r[0]; if (mkts) D.reMarkets = mkts;
        var land = r[1]; if (land) D.reLand = land;
        var trends = r[2]; if (trends) D.reTrends = trends;

        var marketNames = mkts ? Object.keys(mkts).filter(function (k) { return k !== 'success' && k !== 'timestamp' && k !== 'modules' && Array.isArray(mkts[k]); }) : [];

        var h = '<div class="ci-sec"><div class="ci-sec-t">\uD83C\uDFE0 Real Estate Intelligence <span class="ci-badge">' + marketNames.length + ' Markets</span></div>';

        if (land && land.per_acre) {
          var pa = land.per_acre;
          var paText = (typeof pa === 'object') ? '$' + fn(pa.min) + ' – $' + fn(pa.max) + (pa.trend ? ' (' + pa.trend + ')' : '') : '$' + fn(pa);
          h += '<div class="ci-card"><div class="ci-card-t">Land Values</div><div class="ci-meta">' +
            '<span>County: <span class="ci-v">' + (land.county || 'N/A') + '</span></span>' +
            '<span>Per Acre: <span class="ci-v">' + paText + '</span></span>' +
            '<span>DC Premium: <span class="ci-v">' + (land.dc_premium || 'N/A') + '</span></span>' +
          '</div></div>';
        }

        if (trends && trends.markets) {
          h += '<div class="ci-sec-t" style="margin-top:12px">Market Trends</div>';
          var tm = Array.isArray(trends.markets) ? trends.markets : [];
          tm.slice(0, 8).forEach(function (t) {
            var mkt = t.market || 'Unknown';
            h += '<div class="ci-card"><div class="ci-card-t">' + mkt + '</div><div class="ci-meta">' +
              (t.vacancy_rate !== undefined ? '<span>Vacancy: <span class="ci-v">' + t.vacancy_rate + '%</span></span>' : '') +
              (t.avg_price_per_acre ? '<span>Land: <span class="ci-v">$' + fn(t.avg_price_per_acre) + '/acre</span></span>' : '') +
              (t.yoy_change ? '<span>YoY: <span class="ci-v">+' + t.yoy_change + '%</span></span>' : '') +
              (t.new_supply_mw ? '<span>New Supply: <span class="ci-v">' + t.new_supply_mw + ' MW</span></span>' : '') +
              (t.land_price_trend ? '<span>Trend: <span class="ci-v">' + t.land_price_trend + '</span></span>' : '') +
            '</div></div>';
          });
          if (trends.hottest) {
            var hot = (typeof trends.hottest === 'object') ? trends.hottest.market || JSON.stringify(trends.hottest) : trends.hottest;
            h += '<div class="ci-card" style="border-left:3px solid ' + C.realEstate + '"><div class="ci-card-t">\uD83D\uDD25 Hottest: ' + hot + '</div>';
            if (typeof trends.hottest === 'object' && trends.hottest.yoy_change) {
              h += '<div class="ci-card-s">+' + trends.hottest.yoy_change + '% YoY • $' + fn(trends.hottest.avg_price_per_acre) + '/acre • ' + trends.hottest.vacancy_rate + '% vacancy</div>';
            }
            h += '</div>';
          }
          if (trends.lowest_vacancy) {
            var lv = (typeof trends.lowest_vacancy === 'object') ? trends.lowest_vacancy : {};
            h += '<div class="ci-card" style="border-left:3px solid ' + C.accent + '"><div class="ci-card-t">\uD83C\uDFC6 Tightest: ' + (lv.market || trends.lowest_vacancy) + '</div>';
            if (lv.vacancy_rate !== undefined) {
              h += '<div class="ci-card-s">' + lv.vacancy_rate + '% vacancy • $' + fn(lv.avg_price_per_acre) + '/acre</div>';
            }
            h += '</div>';
          }
        }

        h += '<div class="ci-sec-t" style="margin-top:12px">Coverage</div>';
        marketNames.forEach(function (name) {
          var counties = mkts[name];
          h += '<div class="ci-card"><div class="ci-card-t">' + name + '</div><div class="ci-card-s">' + counties.map(function (c) { return c.county; }).join(', ') + ' (' + counties[0].state + ')</div></div>';
        });
        h += '</div>';
        self.set(h);
      });
    },

    // ── FIBER ───────────────────────────────────────────────────
    loadFiber: function () {
      var self = this;
      (D.fiber ? Promise.resolve(D.fiber) : apiFetch('/api/fiber/routes')).then(function (d) {
        if (d) D.fiber = d;
        var routes = (d && d.routes) || [];
        var totalMiles = 0;
        var providers = {};
        routes.forEach(function (r) { totalMiles += (r.route_miles || 0); providers[r.provider] = (providers[r.provider] || 0) + 1; });

        var h = '<div class="ci-sec"><div class="ci-sec-t">\uD83D\uDD17 Fiber Network Discovery <span class="ci-badge">' + routes.length + ' Routes</span></div>' +
          '<div class="ci-stats">' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.fiber + '">' + routes.length + '</div><div class="ci-st-l">Routes</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.fiber + '">' + fn(totalMiles) + '</div><div class="ci-st-l">Total Miles</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.fiber + '">' + Object.keys(providers).length + '</div><div class="ci-st-l">Providers</div></div>' +
          '</div></div>';

        h += '<div class="ci-sec"><div class="ci-sec-t">Routes</div>';
        routes.forEach(function (r) {
          h += '<div class="ci-card"><div class="ci-card-t">' + r.route_name + '</div><div class="ci-card-s">' + r.provider + '</div><div class="ci-meta">' +
            '<span>Miles: <span class="ci-v">' + fn(r.route_miles) + '</span></span>' +
            '<span>Fibers: <span class="ci-v">' + r.fiber_count + '</span></span>' +
            '<span>Dark: <span class="ci-v">' + (r.dark_fiber_available ? 'Yes \u2705' : 'No') + '</span></span>' +
            '<span>' + r.start_city + ' \u2192 ' + r.end_city + '</span>' +
          '</div></div>';
        });
        h += '</div>';
        self.set(h);
      });
    },

    // ── SEC ──────────────────────────────────────────────────────
    loadSEC: function () {
      var self = this;
      Promise.all([
        D.secSignals || apiFetch('/api/sec/expansion-signals'),
        D.secFilings || apiFetch('/api/sec/filings')
      ]).then(function (r) {
        var sig = r[0]; if (sig) D.secSignals = sig;
        var fil = r[1]; if (fil) D.secFilings = fil;
        var signals = (sig && sig.signals) || [];
        var filings = (fil && fil.filings) || [];
        var totalInv = 0, totalMW = 0;
        signals.forEach(function (s) { totalInv += (s.investment_amount || 0); totalMW += (s.capacity_mw || 0); });

        var h = '<div class="ci-sec"><div class="ci-sec-t">\uD83D\uDCC8 SEC Expansion Signals <span class="ci-badge">' + signals.length + '</span></div>' +
          '<div class="ci-stats">' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.sec + '">' + fm(totalInv) + '</div><div class="ci-st-l">Investment</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.sec + '">' + fn(totalMW) + '</div><div class="ci-st-l">MW Planned</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.sec + '">' + signals.length + '</div><div class="ci-st-l">Signals</div></div>' +
          '</div></div>';

        h += '<div class="ci-sec"><div class="ci-sec-t">Expansion Signals</div>';
        signals.forEach(function (s) {
          h += '<div class="ci-card"><div class="ci-card-t">' + s.company + '</div><div class="ci-card-s">' + s.signal_type + ' \u2022 ' + s.source + '</div><div class="ci-meta">' +
            '<span>Location: <span class="ci-v">' + s.location + '</span></span>' +
            '<span>CapEx: <span class="ci-v">' + fm(s.investment_amount) + '</span></span>' +
            '<span>MW: <span class="ci-v">' + s.capacity_mw + '</span></span>' +
            '<span>ETA: <span class="ci-v">' + (s.expected_completion || 'TBD') + '</span></span>' +
          '</div></div>';
        });
        h += '</div>';

        if (filings.length) {
          h += '<div class="ci-sec"><div class="ci-sec-t">Recent Filings <span class="ci-badge">' + filings.length + '</span></div>';
          filings.slice(0, 8).forEach(function (f) {
            h += '<div class="ci-card"><div class="ci-card-t">' + (f.company || f.ticker || 'Filing') + '</div><div class="ci-card-s">' + (f.form_type || '') + (f.filed_date ? ' \u2022 ' + f.filed_date : '') + '</div>' +
              (f.description ? '<div style="font-size:11px;color:' + C.dim + ';margin-top:4px">' + f.description.substring(0, 120) + '</div>' : '') + '</div>';
          });
          h += '</div>';
        }
        self.set(h);
      });
    },

    // ── COMPETITORS ─────────────────────────────────────────────
    loadCompetitors: function () {
      var self = this;
      Promise.all([
        D.compSummary || apiFetch('/api/competitors/summary'),
        D.compGaps || apiFetch('/api/competitors/gaps'),
        D.compMatrix || apiFetch('/api/competitors/matrix')
      ]).then(function (r) {
        var sum = r[0]; if (sum) D.compSummary = sum;
        var gaps = r[1]; if (gaps) D.compGaps = gaps;
        var matrix = r[2]; if (matrix) D.compMatrix = matrix;

        var dcScore = (matrix && matrix.dc_hub_score) || 10;
        var avgScore = (matrix && matrix.competitor_avg_score) || 2.3;
        var gapList = (gaps && gaps.gaps) || [];
        var matrixData = (matrix && matrix.matrix) || {};

        var h = '<div class="ci-sec"><div class="ci-sec-t">\uD83C\uDFAF Competitor Intelligence <span class="ci-badge">' + ((sum && sum.total_competitors) || 6) + ' Analyzed</span></div>' +
          '<div class="ci-stats">' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.accent + '">' + dcScore + '/10</div><div class="ci-st-l">DC Hub</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.dim + '">' + avgScore + '/10</div><div class="ci-st-l">Avg Comp</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.permits + '">' + gapList.length + '</div><div class="ci-st-l">Gaps</div></div>' +
          '</div></div>';

        if (matrixData && Object.keys(matrixData).length) {
          h += '<div class="ci-sec"><div class="ci-sec-t">Feature Matrix</div>';
          Object.keys(matrixData).forEach(function (comp) {
            var raw = matrixData[comp];
            var score = 0;
            if (typeof raw === 'number') {
              score = raw;
            } else if (raw && typeof raw === 'object') {
              // Could be {score: N} or {feature1: true, feature2: false, ...}
              if (typeof raw.score === 'number') {
                score = raw.score;
              } else if (typeof raw.total === 'number') {
                score = raw.total;
              } else {
                // Count truthy feature values to derive score
                var vals = Object.values(raw);
                vals.forEach(function(v) { if (v === true || v === 1 || v === 'yes' || v === '✓') score++; });
                // If no booleans found but has numeric values, sum/average them
                if (score === 0 && vals.length) {
                  var numVals = vals.filter(function(v) { return typeof v === 'number'; });
                  if (numVals.length) score = Math.round(numVals.reduce(function(a,b){return a+b;},0) / numVals.length);
                }
              }
            }
            var pct = Math.min(100, (score / 10 * 100));
            var isHub = comp.toLowerCase().indexOf('dc hub') >= 0 || comp.toLowerCase().indexOf('dchub') >= 0;
            if (isHub && score === 0) score = dcScore; // Fallback to dc_hub_score from API
            if (isHub) pct = Math.min(100, (score / 10 * 100));
            h += '<div style="display:flex;align-items:center;gap:8px;padding:4px 0"><span style="width:100px;font-size:11px;font-weight:600;color:' + (isHub ? C.accent : '#fff') + '">' + comp + '</span><div class="ci-bar"><div class="ci-bar-fill" style="width:' + pct + '%;background:' + (isHub ? C.accent : C.dim) + '"></div></div><span style="font-size:11px;font-weight:700;color:' + (isHub ? C.accent : C.dim) + '">' + score + '/10</span></div>';
          });
          h += '</div>';
        }

        if (gapList.length) {
          h += '<div class="ci-sec"><div class="ci-sec-t">\uD83D\uDD13 Coverage Gaps <span class="ci-badge">' + gapList.length + '</span></div>';
          gapList.forEach(function (g) {
            h += '<div class="ci-card" style="border-left:3px solid ' + C.permits + '"><div class="ci-card-t">' + (g.gap || g.feature || g.name || 'Gap') + '</div>' +
              (g.description || g.opportunity ? '<div style="font-size:11px;color:' + C.dim + ';margin-top:4px">' + (g.description || g.opportunity) + '</div>' : '') + '</div>';
          });
          h += '</div>';
        }

        h += '<div class="ci-sec"><div class="ci-sec-t">DC Hub Unique Features</div><div class="ci-card" style="border-left:3px solid ' + C.accent + '"><div style="font-size:12px;line-height:1.8">' +
          '\u2705 Real-time power infrastructure<br>\u2705 AI platform integration (6 agents)<br>\u2705 Government infrastructure layers<br>\u2705 SEC filing expansion analysis<br>\u2705 Fiber network mapping<br>\u2705 Construction permit tracking<br>\u2705 Job posting market signals<br>\u2705 Drought &amp; water risk layers</div></div></div>';
        self.set(h);
      });
    },

    // ── JOBS ────────────────────────────────────────────────────
    loadJobs: function () {
      var self = this;
      (D.jobs ? Promise.resolve(D.jobs) : apiFetch('/api/jobs/trends')).then(function (d) {
        if (d) D.jobs = d;
        if (!d || !d.success) { self.set('<div class="ci-empty">Jobs API not responding</div>'); return; }

        var total = d.total_q1_postings || 0;
        var trends = d.trends || {};
        var fastest = (d.fastest_growing && Array.isArray(d.fastest_growing)) ? d.fastest_growing : [];
        
        // Handle trends as array or object
        var companies;
        if (Array.isArray(trends)) {
          companies = trends.map(function(item, i) {
            return {
              name: item.company || item.name || ('Company ' + (i + 1)),
              count: item.count || item.q1_postings || item.total || item.postings || 0,
              data: item
            };
          });
        } else {
          companies = Object.keys(trends).map(function(co) {
            var t = trends[co];
            var count = (typeof t === 'number') ? t : (t && (t.count || t.q1_postings || t.total)) || 0;
            return { name: co, count: count, data: t };
          });
        }

        var h = '<div class="ci-sec"><div class="ci-sec-t">\uD83D\uDCBC Job Market Intelligence <span class="ci-badge">Q1 Data</span></div>' +
          '<div class="ci-stats">' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.jobs + '">' + fn(total) + '</div><div class="ci-st-l">Q1 Postings</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.jobs + '">' + companies.length + '</div><div class="ci-st-l">Companies</div></div>' +
          '<div class="ci-st"><div class="ci-st-v" style="color:' + C.jobs + '">' + fastest.length + '</div><div class="ci-st-l">Fast Growth</div></div>' +
          '</div></div>';

        if (companies.length) {
          h += '<div class="ci-sec"><div class="ci-sec-t">Hiring Trends</div>';
          var sorted = companies.slice().sort(function (a, b) { return b.count - a.count; });
          var mx = sorted[0] ? sorted[0].count : 1;
          if (mx === 0) mx = 1;
          sorted.slice(0, 12).forEach(function (co) {
            h += '<div class="ci-card"><div style="display:flex;justify-content:space-between;align-items:center"><div class="ci-card-t">' + co.name + '</div><span style="font-size:13px;font-weight:700;color:' + C.jobs + '">' + fn(co.count) + '</span></div>' +
              '<div style="margin-top:6px;height:4px;background:' + C.border + ';border-radius:2px;overflow:hidden"><div style="width:' + Math.min(100, (co.count / mx) * 100) + '%;height:100%;background:' + C.jobs + ';border-radius:2px"></div></div></div>';
          });
          h += '</div>';
        }

        if (fastest.length) {
          h += '<div class="ci-sec"><div class="ci-sec-t">\uD83D\uDE80 Fastest Growing</div>';
          fastest.slice(0, 5).forEach(function (f) {
            var name = (typeof f === 'string') ? f : (f.company || f.name || '');
            var growth = (typeof f === 'object' && f.growth) ? f.growth : '';
            h += '<div class="ci-card"><div class="ci-card-t">' + name + '</div>' + (growth ? '<div class="ci-card-s">Growth: ' + growth + '</div>' : '') + '</div>';
          });
          h += '</div>';
        }
        self.set(h);
      });
    },

    // ── PERMITS ─────────────────────────────────────────────────
    loadPermits: function () {
      var self = this;
      (D.permits ? Promise.resolve(D.permits) : apiFetch('/api/permits/pipeline')).then(function (d) {
        if (d) D.permits = d;
        if (!d || !d.pipeline_summary) { self.set('<div class="ci-empty">Permits pipeline data not available</div>'); return; }

        var ps = d.pipeline_summary;
        var keys = Object.keys(ps);

        var h = '<div class="ci-sec"><div class="ci-sec-t">\uD83C\uDFD7\uFE0F Construction Permit Pipeline <span class="ci-badge">LIVE</span></div>';

        if (ps.total_projects || ps.total_permits) {
          h += '<div class="ci-stats">' +
            '<div class="ci-st"><div class="ci-st-v" style="color:' + C.permits + '">' + (ps.total_projects || ps.total_permits || 0) + '</div><div class="ci-st-l">Projects</div></div>' +
            (ps.total_mw ? '<div class="ci-st"><div class="ci-st-v" style="color:' + C.permits + '">' + fn(ps.total_mw) + '</div><div class="ci-st-l">MW Pipeline</div></div>' : '') +
            (ps.total_value ? '<div class="ci-st"><div class="ci-st-v" style="color:' + C.permits + '">' + fm(ps.total_value) + '</div><div class="ci-st-l">Value</div></div>' : '') +
          '</div>';
        }

        h += '<div class="ci-sec"><div class="ci-sec-t">Pipeline Details</div>';
        keys.forEach(function (k) {
          var val = ps[k];
          if (typeof val === 'object' && val !== null) {
            // Parse nested objects like by_status: {"Issued": {"count": 8, "value": 3010000000}}
            h += '<div class="ci-card"><div class="ci-card-t">' + k.replace(/_/g, ' ') + '</div>';
            Object.keys(val).forEach(function(subKey) {
              var subVal = val[subKey];
              if (typeof subVal === 'object' && subVal !== null) {
                // e.g. {"count": 8, "value": 3010000000}
                var parts = [];
                if (subVal.count) parts.push(subVal.count + ' projects');
                if (subVal.value) parts.push(fm(subVal.value));
                if (subVal.mw) parts.push(subVal.mw + ' MW');
                h += '<div style="display:flex;justify-content:space-between;padding:4px 0;margin-left:8px;border-bottom:1px solid rgba(255,255,255,.03)"><span style="font-size:12px;color:#fff;font-weight:600">' + subKey + '</span><span style="font-size:12px;color:' + C.permits + '">' + parts.join(' · ') + '</span></div>';
              } else {
                h += '<div style="display:flex;justify-content:space-between;padding:4px 0;margin-left:8px;border-bottom:1px solid rgba(255,255,255,.03)"><span style="font-size:12px;color:#fff">' + subKey + '</span><span style="font-size:12px;color:' + C.permits + '">' + (typeof subVal === 'number' ? fn(subVal) : subVal) + '</span></div>';
              }
            });
            h += '</div>';
          } else {
            h += '<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.03)"><span style="font-size:12px;color:' + C.dim + '">' + k.replace(/_/g, ' ') + '</span><span style="font-size:12px;font-weight:600;color:' + C.permits + '">' + (typeof val === 'number' ? fn(val) : val) + '</span></div>';
          }
        });
        h += '</div></div>';
        self.set(h);
      });
    }
  };

  // ── Boot ──────────────────────────────────────────────────────
  function init() {
    console.log('[DC Hub] Competitive Intelligence Suite v' + VERSION + ' loading\u2026');
    injectStyles();
    buildPanel();
    console.log('[DC Hub] Competitive Intelligence Suite v' + VERSION + ' ready \u2705');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
  window.DCHubCI = CI;
})();
