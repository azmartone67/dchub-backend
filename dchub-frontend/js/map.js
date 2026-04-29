// ─────────────────────────────────────────────────────────────
// DC Hub Intelligence Map  —  map.js
// Deploy to Cloudflare Pages alongside map.html
// Update this file for data/feature changes; map.html rarely changes
// ─────────────────────────────────────────────────────────────

const DCHUB_API  = 'https://dchub.cloud';
const API_LIMIT  = 500;   // facilities per load
const MCP_TIMEOUT = 6000; // ms before MCP fallback gives up

// ─── Albers Equal-Area USA projection ────────────────────────
function albers(lon, lat, W, H, zoom = 1, offX = 0, offY = 0) {
  const d2r = Math.PI / 180;
  const lon0 = -96, lat0 = 38, lat1 = 29.5, lat2 = 45.5;
  const n = (Math.sin(lat1 * d2r) + Math.sin(lat2 * d2r)) / 2;
  const C = Math.cos(lat1 * d2r) ** 2 + 2 * n * Math.sin(lat1 * d2r);
  const r0 = Math.sqrt(C - 2 * n * Math.sin(lat0 * d2r)) / n;
  const r  = Math.sqrt(C - 2 * n * Math.sin(lat  * d2r)) / n;
  const th = n * (lon - lon0) * d2r;
  const rx = r * Math.sin(th);
  const ry = r0 - r * Math.cos(th);
  const bx = W * 0.5  + rx * (W / 6.5);
  const by = H * 0.475 - ry * (H / 5.2);
  return [(bx - W / 2) * zoom + W / 2 + offX,
          (by - H / 2) * zoom + H / 2 + offY];
}

function albersInv(px, py, W, H, zoom = 1, offX = 0, offY = 0) {
  const bx = (px - W / 2 - offX) / zoom + W / 2;
  const by = (py - H / 2 - offY) / zoom + H / 2;
  const x  = (bx - W * 0.5) / (W / 6.5);
  const y  = -((by - H * 0.475) / (H / 5.2));
  const d2r = Math.PI / 180, r2d = 180 / Math.PI;
  const lon0 = -96, lat0 = 38, lat1 = 29.5, lat2 = 45.5;
  const n = (Math.sin(lat1 * d2r) + Math.sin(lat2 * d2r)) / 2;
  const C = Math.cos(lat1 * d2r) ** 2 + 2 * n * Math.sin(lat1 * d2r);
  const r0 = Math.sqrt(C - 2 * n * Math.sin(lat0 * d2r)) / n;
  const r  = Math.sqrt(x * x + (r0 - y) ** 2);
  const th = Math.atan2(x, r0 - y);
  return {
    lat: Math.asin((C - r * r * n * n) / (2 * n)) * r2d,
    lon: lon0 + (th * r2d) / n
  };
}

// ─── Simplified US outline + state grid ──────────────────────
const CONUS_OUTLINE = [
  [-124.7,48.4],[-95.2,49.4],[-83.1,45.8],[-76.9,44.8],[-75.2,44.3],
  [-72.1,45.0],[-66.9,44.8],[-67.1,44.1],[-70.7,43.0],[-70.2,42.0],
  [-73.5,40.6],[-74.3,40.0],[-75.5,38.5],[-76.0,37.2],[-75.7,35.2],
  [-77.0,34.0],[-79.5,33.0],[-81.0,31.0],[-80.0,29.0],[-82.0,28.0],
  [-84.0,29.5],[-85.5,29.8],[-87.5,30.2],[-89.5,29.0],[-90.0,29.0],
  [-93.0,29.0],[-97.0,26.0],[-97.5,27.0],[-97.4,28.2],[-97.0,30.0],
  [-100.0,28.0],[-104.0,29.0],[-106.6,31.8],[-108.2,31.3],[-114.8,32.5],
  [-117.1,32.5],[-118.3,33.8],[-120.5,34.5],[-122.5,37.5],[-124.2,40.5],
  [-124.7,48.4]
];

const STATE_GRID = [
  [[-104,49],[-104,37],[-104,36.5]],
  [[-111,42],[-111,37]],
  [[-120,42],[-114,42],[-111,42]],
  [[-97,49],[-97,46],[-97,44],[-97,42],[-97,40]],
  [[-91,42],[-91,39],[-89,37],[-88,37]],
  [[-84,42],[-84,40],[-84,38]],
  [[-80,42],[-80,40],[-77,39.7]],
  [[-76.5,38],[-77.4,39.7],[-79,39.7]],
  [[-87,42],[-87,38]],
  [[-94,36.5],[-94,40],[-94,42]],
  [[-100,37],[-100,40],[-102,40],[-104,40]],
  [[-108.5,37],[-109,31.3]],
  [[-114,37],[-114.6,32.5]],
  [[-86,35],[-86,31],[-88,31]],
  [[-90,32],[-90,29]],
  [[-81,31],[-81,29],[-80,29]],
];

// ─── Type helpers ─────────────────────────────────────────────
const TYPE_MAP = {
  hyperscale: { color: '#a78bfa', label: 'HYPE', cls: 't-hyp' },
  colocation:  { color: '#00d4ff', label: 'COLO', cls: 't-col' },
  edge:        { color: '#00e5a0', label: 'EDGE', cls: 't-edg' },
  enterprise:  { color: '#f5a623', label: 'ENTR', cls: 't-ent' },
  default:     { color: '#8a90a0', label: 'DC',   cls: 't-col' },
};

function typeInfo(raw) {
  const t = (raw || '').toLowerCase();
  if (t.includes('hyper') || t.includes('cloud') || t.includes('hype')) return TYPE_MAP.hyperscale;
  if (t.includes('colo'))   return TYPE_MAP.colocation;
  if (t.includes('edge'))   return TYPE_MAP.edge;
  if (t.includes('enter'))  return TYPE_MAP.enterprise;
  return TYPE_MAP.default;
}

// Normalise a raw API/MCP facility object to a consistent shape
function normaliseFacility(raw) {
  return {
    id:       raw.id || raw.facility_id || String(Math.random()),
    name:     raw.name || raw.facility_name || 'Unknown',
    city:     raw.city || raw.city_name || '',
    state:    raw.state_province || raw.state || raw.region || '',
    country:  raw.country_code || raw.country || 'US',
    lat:      parseFloat(raw.latitude  || raw.lat || 0),
    lon:      parseFloat(raw.longitude || raw.lon || raw.lng || 0),
    type:     raw.facility_type || raw.type || 'colocation',
    operator: raw.operator_name || raw.operator || raw.company || '',
    mw:       parseFloat(raw.total_power_mw || raw.capacity_mw || raw.power_mw || 0) || null,
    status:   raw.status || raw.operational_status || 'Operational',
    market:   raw.market || raw.metro || raw.state_province || '',
  };
}

// ─── Data loading — API first, MCP fallback ───────────────────
async function loadFacilities(apiKey) {
  // 1. Try REST API
  try {
    const headers = apiKey ? { 'X-API-Key': apiKey } : {};
    const res = await Promise.race([
      fetch(`${DCHUB_API}/api/facilities?limit=${API_LIMIT}&country_code=US`, { headers }),
      new Promise((_, rej) => setTimeout(() => rej('timeout'), 8000))
    ]);
    if (res.ok) {
      const data = await res.json();
      const raw = data.facilities || data.results || data.data || (Array.isArray(data) ? data : []);
      if (raw.length > 0) {
        console.log(`[DC Hub] API: loaded ${raw.length} facilities`);
        return raw.map(normaliseFacility).filter(f => f.lat && f.lon);
      }
    }
  } catch (e) {
    console.warn('[DC Hub] API failed, trying MCP fallback:', e);
  }

  // 2. MCP fallback
  try {
    const res = await Promise.race([
      fetch(`${DCHUB_API}/mcp`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0', id: 1, method: 'tools/call',
          params: { name: 'search_facilities', arguments: { limit: API_LIMIT, country_code: 'US' } }
        })
      }),
      new Promise((_, rej) => setTimeout(() => rej('timeout'), MCP_TIMEOUT))
    ]);
    const data = await res.json();
    const text = data?.result?.content?.[0]?.text || '[]';
    const parsed = JSON.parse(text);
    const raw = parsed.facilities || parsed.results || (Array.isArray(parsed) ? parsed : []);
    if (raw.length > 0) {
      console.log(`[DC Hub] MCP fallback: loaded ${raw.length} facilities`);
      return raw.map(normaliseFacility).filter(f => f.lat && f.lon);
    }
  } catch (e) {
    console.warn('[DC Hub] MCP fallback failed:', e);
  }

  // 3. Last resort — demo seed data so map isn't blank
  console.warn('[DC Hub] Using demo seed data');
  return DEMO_FACILITIES;
}

async function loadInfrastructure() {
  // Substations + fiber — MCP first, demo fallback
  let substations = [], fiberRoutes = [];
  try {
    const res = await Promise.race([
      fetch(`${DCHUB_API}/mcp`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0', id: 2, method: 'tools/call',
          params: { name: 'get_infrastructure', arguments: { country_code: 'US', limit: 200 } }
        })
      }),
      new Promise((_, rej) => setTimeout(() => rej('timeout'), MCP_TIMEOUT))
    ]);
    const data = await res.json();
    const text = data?.result?.content?.[0]?.text || '{}';
    const parsed = JSON.parse(text);
    substations  = (parsed.substations  || []).map(s => [parseFloat(s.longitude || s.lon), parseFloat(s.latitude || s.lat)]).filter(([lo, la]) => lo && la);
    fiberRoutes  = parsed.fiber_routes || [];
  } catch (_) {}

  if (!substations.length)  substations = DEMO_SUBSTATIONS;
  if (!fiberRoutes.length)  fiberRoutes = DEMO_FIBER;
  return { substations, fiberRoutes };
}

async function analyzeSite(lat, lon, apiKey) {
  const headers = { 'Content-Type': 'application/json', ...(apiKey ? { 'X-API-Key': apiKey } : {}) };

  // 1. Try REST API site analysis
  try {
    const res = await Promise.race([
      fetch(`${DCHUB_API}/api/site-analysis?lat=${lat}&lon=${lon}&radius_km=50`, { headers }),
      new Promise((_, rej) => setTimeout(() => rej('t'), 6000))
    ]);
    if (res.ok) return await res.json();
  } catch (_) {}

  // 2. MCP analyze_site
  try {
    const res = await Promise.race([
      fetch(`${DCHUB_API}/mcp`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          jsonrpc: '2.0', id: 3, method: 'tools/call',
          params: { name: 'analyze_site', arguments: { latitude: lat, longitude: lon, radius_km: 50 } }
        })
      }),
      new Promise((_, rej) => setTimeout(() => rej('t'), MCP_TIMEOUT))
    ]);
    const data = await res.json();
    const text = data?.result?.content?.[0]?.text || '{}';
    return JSON.parse(text);
  } catch (_) {}

  return null; // triggers demo scores in UI
}

// ─── Demo seed data ───────────────────────────────────────────
const DEMO_FACILITIES = [
  {id:'eq-dc10',name:'Equinix DC10 Ashburn',city:'Ashburn',state:'VA',country:'US',lat:39.043,lon:-77.488,type:'colocation',operator:'Equinix',mw:36,status:'Operational',market:'Northern Virginia'},
  {id:'eq-dc11',name:'Equinix DC11 Ashburn',city:'Ashburn',state:'VA',country:'US',lat:39.044,lon:-77.490,type:'colocation',operator:'Equinix',mw:42,status:'Operational',market:'Northern Virginia'},
  {id:'ms-boi', name:'Microsoft Azure East US',city:'Boydton',state:'VA',country:'US',lat:36.664,lon:-78.375,type:'hyperscale',operator:'Microsoft',mw:220,status:'Operational',market:'Northern Virginia'},
  {id:'amz-ue1',name:'Amazon AWS us-east-1',city:'Manassas',state:'VA',country:'US',lat:38.750,lon:-77.476,type:'hyperscale',operator:'Amazon',mw:180,status:'Operational',market:'Northern Virginia'},
  {id:'goo-mid',name:'Google Midlothian Campus',city:'Midlothian',state:'TX',country:'US',lat:32.481,lon:-96.994,type:'hyperscale',operator:'Google',mw:400,status:'Operational',market:'Dallas'},
  {id:'cyx-dfw',name:'Cyxtera DFW1',city:'Dallas',state:'TX',country:'US',lat:32.897,lon:-97.040,type:'colocation',operator:'Cyxtera',mw:22,status:'Operational',market:'Dallas'},
  {id:'qts-chi',name:'QTS Chicago',city:'Chicago',state:'IL',country:'US',lat:41.850,lon:-87.650,type:'colocation',operator:'QTS',mw:28,status:'Operational',market:'Chicago'},
  {id:'meta-mes',name:'Meta Mesa Campus',city:'Mesa',state:'AZ',country:'US',lat:33.415,lon:-111.831,type:'hyperscale',operator:'Meta',mw:300,status:'Operational',market:'Phoenix'},
  {id:'cyo-phx',name:'CyrusOne Phoenix',city:'Phoenix',state:'AZ',country:'US',lat:33.448,lon:-112.074,type:'colocation',operator:'CyrusOne',mw:48,status:'Operational',market:'Phoenix'},
  {id:'ms-phx',name:'Microsoft Chandler',city:'Chandler',state:'AZ',country:'US',lat:33.306,lon:-111.841,type:'hyperscale',operator:'Microsoft',mw:180,status:'Operational',market:'Phoenix'},
  {id:'irm-slc',name:'Iron Mountain Salt Lake',city:'Salt Lake City',state:'UT',country:'US',lat:40.760,lon:-111.891,type:'colocation',operator:'Iron Mountain',mw:18,status:'Operational',market:'Salt Lake City'},
  {id:'amz-uw2',name:'Amazon AWS us-west-2',city:'Hillsboro',state:'OR',country:'US',lat:45.522,lon:-122.989,type:'hyperscale',operator:'Amazon',mw:160,status:'Operational',market:'Portland'},
  {id:'goo-dal',name:'Google The Dalles',city:'The Dalles',state:'OR',country:'US',lat:45.594,lon:-121.179,type:'hyperscale',operator:'Google',mw:190,status:'Operational',market:'Portland'},
  {id:'eq-sv5',name:'Equinix SV5 San Jose',city:'San Jose',state:'CA',country:'US',lat:37.338,lon:-121.886,type:'colocation',operator:'Equinix',mw:24,status:'Operational',market:'Silicon Valley'},
  {id:'eq-la1',name:'Equinix LA1 El Segundo',city:'El Segundo',state:'CA',country:'US',lat:33.919,lon:-118.416,type:'colocation',operator:'Equinix',mw:30,status:'Operational',market:'Los Angeles'},
  {id:'sw-las',name:'Switch SUPERNAP Las Vegas',city:'Las Vegas',state:'NV',country:'US',lat:36.175,lon:-115.137,type:'colocation',operator:'Switch',mw:650,status:'Operational',market:'Las Vegas'},
  {id:'ms-chi',name:'Microsoft Northlake',city:'Northlake',state:'IL',country:'US',lat:41.908,lon:-87.903,type:'hyperscale',operator:'Microsoft',mw:120,status:'Operational',market:'Chicago'},
  {id:'nwn-col',name:'Nationwide Columbus',city:'Columbus',state:'OH',country:'US',lat:39.961,lon:-82.999,type:'enterprise',operator:'Nationwide',mw:14,status:'Operational',market:'Columbus'},
  {id:'amz-ue2',name:'Amazon AWS us-east-2',city:'Dublin',state:'OH',country:'US',lat:40.098,lon:-83.114,type:'hyperscale',operator:'Amazon',mw:200,status:'Operational',market:'Columbus'},
  {id:'van-atl',name:'Vantage Atlanta',city:'Atlanta',state:'GA',country:'US',lat:33.749,lon:-84.388,type:'colocation',operator:'Vantage',mw:32,status:'Operational',market:'Atlanta'},
  {id:'qts-atl',name:'QTS Atlanta',city:'Atlanta',state:'GA',country:'US',lat:33.800,lon:-84.200,type:'colocation',operator:'QTS',mw:55,status:'Operational',market:'Atlanta'},
  {id:'goo-cha',name:'Google Charlotte',city:'Charlotte',state:'NC',country:'US',lat:35.227,lon:-80.843,type:'hyperscale',operator:'Google',mw:250,status:'Operational',market:'Charlotte'},
  {id:'edg-mia',name:'EdgeConneX Miami',city:'Miami',state:'FL',country:'US',lat:25.775,lon:-80.209,type:'edge',operator:'EdgeConneX',mw:12,status:'Operational',market:'Miami'},
  {id:'edg-bos',name:'EdgeConneX Boston',city:'Boston',state:'MA',country:'US',lat:42.360,lon:-71.058,type:'edge',operator:'EdgeConneX',mw:10,status:'Operational',market:'Boston'},
  {id:'ntn-hou',name:'Netrality 1301 Fannin',city:'Houston',state:'TX',country:'US',lat:29.751,lon:-95.369,type:'colocation',operator:'Netrality',mw:16,status:'Operational',market:'Houston'},
  {id:'edg-sea',name:'EdgeConneX Seattle',city:'Seattle',state:'WA',country:'US',lat:47.606,lon:-122.332,type:'edge',operator:'EdgeConneX',mw:14,status:'Operational',market:'Seattle'},
  {id:'nue-sat',name:'Nu E Power San Antonio',city:'San Antonio',state:'TX',country:'US',lat:29.424,lon:-98.493,type:'enterprise',operator:'Nu E Power',mw:80,status:'Under Construction',market:'San Antonio'},
  {id:'trct-den',name:'Tract Data Denver',city:'Denver',state:'CO',country:'US',lat:39.739,lon:-104.984,type:'hyperscale',operator:'TBD',mw:500,status:'Under Construction',market:'Denver'},
  {id:'pjt-abi',name:'Project Panther Abilene',city:'Abilene',state:'TX',country:'US',lat:32.448,lon:-99.733,type:'hyperscale',operator:'Undisclosed',mw:640,status:'Approved',market:'West Texas'},
  {id:'edg-kc',name:'EdgeConneX Kansas City',city:'Kansas City',state:'MO',country:'US',lat:39.099,lon:-94.578,type:'edge',operator:'EdgeConneX',mw:8,status:'Operational',market:'Kansas City'},
];

const DEMO_SUBSTATIONS = [
  [-77.500,39.043],[-112.090,33.448],[-97.050,32.897],[-87.640,41.878],
  [-121.900,37.338],[-111.900,40.760],[-80.220,25.775],[-83.010,39.961],
  [-84.395,33.749],[-123.000,45.522],[-78.390,36.664],[-97.010,32.481],
  [-77.036,38.900],[-118.243,34.052],[-95.375,29.751],[-71.060,42.360],
  [-122.332,47.606],[-97.743,30.267],[-80.843,35.227],[-104.990,39.739],
  [-115.137,36.175],[-83.114,40.098],[-98.493,29.424],[-94.578,39.099],
  [-73.993,40.728],[-93.094,44.963],[-88.200,41.800],[-111.841,33.306],
];

const DEMO_FIBER = [
  [[-77.5,39.0],[-80.8,35.2],[-84.4,33.7],[-87.6,41.8],[-83.1,40.0]],
  [[-121.9,37.3],[-123.0,45.5],[-122.3,47.6]],
  [[-80.2,25.8],[-84.4,33.7],[-83.0,40.0],[-77.5,39.0]],
  [[-97.0,32.5],[-95.4,29.8],[-98.5,29.4],[-94.6,39.1],[-104.9,39.7]],
  [[-118.2,34.1],[-121.9,37.3],[-115.1,36.2],[-111.9,40.8],[-104.9,39.7],[-97.0,32.5]],
  [[-77.5,38.8],[-74.0,40.7],[-71.1,42.4]],
  [[-84.4,33.7],[-87.5,30.2],[-90.0,29.0],[-95.4,29.8]],
];

const DEMO_PIPELINE = [
  {lon:-104.984,lat:39.739,mw:500,name:'Tract Data Denver'},
  {lon:-99.733,lat:32.448,mw:640,name:'Project Panther'},
  {lon:-97.516,lat:35.467,mw:300,name:'Undisclosed OKC'},
  {lon:-95.995,lat:41.257,mw:400,name:'Undisclosed Omaha'},
];

// ─── Market jump coordinates [lon, lat, zoom] ─────────────────
const MARKET_COORDS = {
  nova:  [-77.488, 39.043, 3.5],
  phx:   [-111.9,  33.45,  4.0],
  dfw:   [-97.04,  32.9,   3.5],
  chi:   [-87.65,  41.85,  3.5],
  sv:    [-121.9,  37.3,   3.5],
  col:   [-83.0,   39.96,  4.0],
  atl:   [-84.4,   33.75,  3.5],
  sea:   [-122.3,  47.6,   3.5],
  las:   [-115.1,  36.2,   3.5],
  nyc:   [-74.0,   40.7,   4.0],
};

function guessState(lon, lat) {
  if (lat > 47 && lon < -116) return 'WA';
  if (lat > 42 && lon < -116) return 'OR';
  if (lat > 32 && lon < -114) return 'CA';
  if (lat > 31 && lat < 37 && lon > -114 && lon < -109) return 'AZ';
  if (lat > 37 && lat < 41 && lon > -109 && lon < -102) return 'CO';
  if (lat > 35 && lat < 37 && lon > -109 && lon < -103) return 'NM';
  if (lat > 26 && lat < 37 && lon > -106 && lon < -93)  return 'TX';
  if (lat > 37 && lat < 42 && lon > -104 && lon < -95)  return 'KS/NE';
  if (lat > 38 && lat < 42 && lon > -78)                return 'VA/MD';
  if (lat > 38 && lat < 42 && lon > -85 && lon < -80)   return 'OH';
  if (lat > 41 && lat < 43 && lon > -89 && lon < -87)   return 'IL';
  if (lat > 30 && lat < 35 && lon > -86 && lon < -80)   return 'GA/AL';
  if (lat > 24 && lat < 31 && lon > -82 && lon < -79)   return 'FL';
  if (lat > 40 && lon > -76 && lon < -66)                return 'NY/NE';
  return 'US';
}

// Export everything the HTML needs
window.DCHubMap = {
  albers, albersInv,
  CONUS_OUTLINE, STATE_GRID,
  typeInfo,
  normaliseFacility,
  loadFacilities,
  loadInfrastructure,
  analyzeSite,
  guessState,
  MARKET_COORDS,
  DEMO_PIPELINE,
  DEMO_SUBSTATIONS,
  DEMO_FIBER,
  DEMO_FACILITIES,
};
