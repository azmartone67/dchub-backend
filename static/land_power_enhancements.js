// =====================================================
// DC HUB v49 - LAND & POWER ENHANCEMENTS
// Copy/paste these additions into your existing Land & Power page
// =====================================================

// =====================================================
// SECTION 1: ADD THESE NEW MARKET MARKERS
// Find your existing market markers array and ADD these
// =====================================================

const NEW_MARKETS_2025 = [
    // OHIO - #4 state for data centers, massive expansion
    { name: "New Albany", lat: 40.08, lng: -82.81, state: "OH", tier: 2, facilities: 104, power: "0.8 GW", projects: ["AWS", "Meta", "Google", "Cologix"], status: "hot" },
    { name: "Johnstown", lat: 40.15, lng: -82.69, state: "OH", tier: 3, facilities: 8, power: "0.8 GW", projects: ["Cologix $7B 800MW"], status: "hot" },
    { name: "Sunbury", lat: 40.24, lng: -82.86, state: "OH", tier: 3, facilities: 4, power: "0.4 GW", projects: ["Amazon $2B"], status: "hot" },
    { name: "Cleveland", lat: 41.50, lng: -81.69, state: "OH", tier: 3, facilities: 24, power: "0.3 GW", projects: [], status: "growing" },
    
    // PENNSYLVANIA - Emerging hub
    { name: "Upper Burrell", lat: 40.58, lng: -79.73, state: "PA", tier: 3, facilities: 2, power: "3.0 GW", projects: ["TECfusions Keystone 3GW"], status: "hot" },
    { name: "Pittsburgh", lat: 40.44, lng: -79.99, state: "PA", tier: 3, facilities: 35, power: "0.4 GW", projects: [], status: "growing" },
    { name: "Lehigh Valley", lat: 40.62, lng: -75.37, state: "PA", tier: 3, facilities: 25, power: "0.3 GW", projects: ["Amazon AI"], status: "growing" },
    
    // TEXAS - Massive AI/Stargate expansion
    { name: "Abilene", lat: 32.45, lng: -99.73, state: "TX", tier: 2, facilities: 5, power: "5.0 GW", projects: ["OpenAI Stargate $100B"], status: "mega" },
    { name: "Amarillo", lat: 35.22, lng: -101.83, state: "TX", tier: 3, facilities: 2, power: "11.0 GW", projects: ["Fermi/UT 11GW Campus"], status: "mega" },
    { name: "West Texas", lat: 31.99, lng: -102.08, state: "TX", tier: 3, facilities: 8, power: "2.0 GW", projects: ["Lancium", "Meta"], status: "hot" },
    { name: "San Antonio", lat: 29.42, lng: -98.49, state: "TX", tier: 3, facilities: 110, power: "0.38 GW", projects: [], status: "established" },
    { name: "Plano", lat: 33.02, lng: -96.70, state: "TX", tier: 3, facilities: 45, power: "0.3 GW", projects: ["Lambda Labs"], status: "growing" },
    { name: "Houston", lat: 29.76, lng: -95.37, state: "TX", tier: 2, facilities: 160, power: "0.58 GW", projects: ["Lambda Labs"], status: "established" },
    
    // INDIANA - Google & Meta expansion
    { name: "Fort Wayne", lat: 41.08, lng: -85.14, state: "IN", tier: 3, facilities: 6, power: "0.5 GW", projects: ["Google 700 Acre Zodiac"], status: "hot" },
    { name: "Jeffersonville", lat: 38.28, lng: -85.74, state: "IN", tier: 3, facilities: 4, power: "0.3 GW", projects: ["Meta 700K SF"], status: "hot" },
    { name: "Indianapolis", lat: 39.77, lng: -86.16, state: "IN", tier: 3, facilities: 35, power: "0.15 GW", projects: ["Stargate Site"], status: "growing" },
    
    // WISCONSIN - Microsoft mega expansion
    { name: "Mount Pleasant", lat: 42.72, lng: -87.90, state: "WI", tier: 3, facilities: 3, power: "0.5 GW", projects: ["Microsoft $3.3B"], status: "hot" },
    { name: "Fairwater", lat: 43.74, lng: -88.87, state: "WI", tier: 4, facilities: 1, power: "1.0 GW", projects: ["Microsoft $7B"], status: "hot" },
    
    // NEBRASKA - Google expansion
    { name: "Omaha/Papillion", lat: 41.15, lng: -96.04, state: "NE", tier: 3, facilities: 25, power: "0.6 GW", projects: ["Google Agate 580 Acres"], status: "hot" },
    { name: "Lincoln", lat: 40.81, lng: -96.70, state: "NE", tier: 4, facilities: 8, power: "0.4 GW", projects: ["Google Campus"], status: "growing" },
    
    // LOUISIANA - Meta's largest campus ever
    { name: "Monroe", lat: 32.51, lng: -92.12, state: "LA", tier: 3, facilities: 2, power: "5.0 GW", projects: ["Meta Hyperion 5GW"], status: "mega" },
    
    // MISSISSIPPI - Compass mega campus
    { name: "Meridian", lat: 32.40, lng: -88.66, state: "MS", tier: 4, facilities: 1, power: "0.5 GW", projects: ["Compass $10B 320MW"], status: "hot" },
    
    // ALABAMA - Meta expansion
    { name: "Montgomery", lat: 32.37, lng: -86.30, state: "AL", tier: 4, facilities: 8, power: "0.3 GW", projects: ["Meta 715K SF"], status: "growing" },
    
    // WYOMING - Low cost, crypto-friendly
    { name: "Cheyenne", lat: 41.14, lng: -104.82, state: "WY", tier: 4, facilities: 8, power: "0.15 GW", projects: ["Meta", "Stargate"], status: "growing" },
    
    // DAKOTAS - Wind power hub
    { name: "Fargo", lat: 46.88, lng: -96.79, state: "ND", tier: 4, facilities: 6, power: "0.2 GW", projects: ["Applied Digital 150MW"], status: "growing" },
    { name: "Sioux Falls", lat: 43.55, lng: -96.73, state: "SD", tier: 4, facilities: 6, power: "0.1 GW", projects: [], status: "emerging" },
    
    // MONTANA - Quantica mega campus
    { name: "Billings", lat: 45.78, lng: -108.50, state: "MT", tier: 4, facilities: 3, power: "0.5 GW", projects: ["Quantica 5000 Acre"], status: "hot" },
    
    // UTAH - Caterpillar power campus
    { name: "Eagle Mountain", lat: 40.31, lng: -112.01, state: "UT", tier: 3, facilities: 4, power: "4.0 GW", projects: ["Caterpillar/Joule 4GW"], status: "hot" },
    { name: "Salt Lake City", lat: 40.76, lng: -111.89, state: "UT", tier: 2, facilities: 85, power: "0.3 GW", projects: [], status: "established" },
    
    // NEW YORK - Anthropic expansion
    { name: "Buffalo", lat: 42.89, lng: -78.88, state: "NY", tier: 4, facilities: 15, power: "0.3 GW", projects: ["Anthropic/Fluidstack $50B"], status: "hot" },
    
    // RURAL VIRGINIA - NoVA overflow
    { name: "Prince Edward Co", lat: 37.22, lng: -78.44, state: "VA", tier: 4, facilities: 2, power: "0.2 GW", projects: [], status: "emerging" },
    { name: "Mecklenburg Co", lat: 36.68, lng: -78.37, state: "VA", tier: 4, facilities: 3, power: "0.3 GW", projects: [], status: "emerging" },
    { name: "Halifax Co", lat: 36.77, lng: -78.93, state: "VA", tier: 4, facilities: 2, power: "0.2 GW", projects: [], status: "emerging" },
    
    // GEORGIA - Atlanta overflow
    { name: "Social Circle", lat: 33.66, lng: -83.72, state: "GA", tier: 4, facilities: 6, power: "0.5 GW", projects: ["Meta Campus"], status: "growing" },
    
    // NORTH CAROLINA
    { name: "Charlotte", lat: 35.23, lng: -80.84, state: "NC", tier: 3, facilities: 65, power: "0.3 GW", projects: [], status: "established" },
    { name: "Raleigh-Durham", lat: 35.78, lng: -78.64, state: "NC", tier: 3, facilities: 45, power: "0.4 GW", projects: [], status: "established" },
    
    // TENNESSEE
    { name: "Nashville", lat: 36.16, lng: -86.78, state: "TN", tier: 3, facilities: 45, power: "0.25 GW", projects: [], status: "growing" },
    { name: "Memphis", lat: 35.15, lng: -90.05, state: "TN", tier: 4, facilities: 22, power: "0.2 GW", projects: [], status: "growing" },
    
    // NEVADA
    { name: "Las Vegas", lat: 36.17, lng: -115.14, state: "NV", tier: 3, facilities: 85, power: "0.35 GW", projects: ["Jet.AI/CCE 50MW"], status: "established" },
    { name: "Reno", lat: 39.53, lng: -119.81, state: "NV", tier: 3, facilities: 55, power: "0.25 GW", projects: [], status: "growing" },
    
    // MISSOURI/KANSAS
    { name: "Kansas City", lat: 39.10, lng: -94.58, state: "MO", tier: 3, facilities: 75, power: "0.2 GW", projects: [], status: "established" },
    
    // FLORIDA
    { name: "Miami", lat: 25.76, lng: -80.19, state: "FL", tier: 2, facilities: 190, power: "0.6 GW", projects: [], status: "established" },
    { name: "Jacksonville", lat: 30.33, lng: -81.66, state: "FL", tier: 4, facilities: 30, power: "0.25 GW", projects: [], status: "growing" },
    
    // NEW MEXICO - Stargate expansion
    { name: "Albuquerque", lat: 35.08, lng: -106.65, state: "NM", tier: 4, facilities: 15, power: "0.2 GW", projects: ["Stargate Site"], status: "growing" },
    
    // MICHIGAN - Stargate site
    { name: "Detroit Metro", lat: 42.33, lng: -83.05, state: "MI", tier: 3, facilities: 40, power: "0.3 GW", projects: ["Stargate Site"], status: "growing" },
    
    // IOWA
    { name: "Des Moines", lat: 41.59, lng: -93.62, state: "IA", tier: 4, facilities: 20, power: "0.2 GW", projects: ["Meta", "Microsoft"], status: "growing" },
    
    // INTERNATIONAL
    { name: "Melbourne", lat: -37.81, lng: 144.96, state: "AU", tier: 3, facilities: 45, power: "0.5 GW", projects: ["AirTrunk 354MW"], status: "hot" },
    { name: "Berlin", lat: 52.52, lng: 13.40, state: "DE", tier: 3, facilities: 35, power: "0.4 GW", projects: ["Maincubes 400MW"], status: "hot" },
    { name: "Lisbon", lat: 38.72, lng: -9.14, state: "PT", tier: 4, facilities: 20, power: "0.2 GW", projects: ["AtlasEdge $292M"], status: "growing" },
];


// =====================================================
// SECTION 2: MARKER STYLING BY STATUS
// Use these colors for the new market markers
// =====================================================

const MARKET_COLORS = {
    mega: "#ef4444",      // Red - Mega projects (5GW+)
    hot: "#f59e0b",       // Orange - Hot markets with active projects
    growing: "#10b981",   // Green - Growing markets
    established: "#6366f1", // Purple - Established markets
    emerging: "#8b5cf6"   // Light purple - Emerging markets
};

const MARKET_SIZES = {
    mega: 14,
    hot: 11,
    growing: 9,
    established: 8,
    emerging: 7
};


// =====================================================
// SECTION 3: POPUP CONTENT GENERATOR
// Use this function to generate rich popups for markets
// =====================================================

function createMarketPopup(market) {
    const projectsList = market.projects.length > 0 
        ? `<div style="margin-top:8px;padding-top:8px;border-top:1px solid #333;">
             <strong>🚧 Active Projects:</strong><br>
             ${market.projects.map(p => `• ${p}`).join('<br>')}
           </div>`
        : '';
    
    const statusBadge = {
        mega: '<span style="background:#ef4444;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">MEGA PROJECT</span>',
        hot: '<span style="background:#f59e0b;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">HOT MARKET</span>',
        growing: '<span style="background:#10b981;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">GROWING</span>',
        established: '<span style="background:#6366f1;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">ESTABLISHED</span>',
        emerging: '<span style="background:#8b5cf6;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">EMERGING</span>'
    };

    return `
        <div style="font-family:Outfit,sans-serif;padding:12px;min-width:220px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <strong style="font-size:14px;">${market.name}, ${market.state}</strong>
                ${statusBadge[market.status] || ''}
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;">
                <div>⚡ <strong>${market.power}</strong> Available</div>
                <div>🏢 <strong>${market.facilities}</strong> Facilities</div>
            </div>
            ${projectsList}
        </div>
    `;
}


// =====================================================
// SECTION 4: ADD MARKETS TO MAP
// Call this function after your map initializes
// =====================================================

function addNewMarketsToMap(map, markersLayer) {
    NEW_MARKETS_2025.forEach(market => {
        const color = MARKET_COLORS[market.status] || MARKET_COLORS.emerging;
        const size = MARKET_SIZES[market.status] || 7;
        
        const marker = L.circleMarker([market.lat, market.lng], {
            radius: size,
            fillColor: color,
            color: '#fff',
            weight: 2,
            fillOpacity: 0.9
        });
        
        marker.bindPopup(createMarketPopup(market), {
            className: 'dc-hub-popup'
        });
        
        marker.bindTooltip(market.name + (market.projects.length > 0 ? ' 🚧' : ''), {
            direction: 'top',
            offset: [0, -5]
        });
        
        markersLayer.addLayer(marker);
    });
}


// =====================================================
// SECTION 5: MARKET REGION DROPDOWN OPTIONS
// Add these to your "Select Market Region" dropdown
// =====================================================

const MARKET_REGIONS = [
    { value: "all", label: "All Markets (115+)" },
    { value: "tier1", label: "Tier 1 - Primary (10)" },
    { value: "tier2", label: "Tier 2 - Secondary (15)" },
    { value: "tier3", label: "Tier 3 - Emerging (35)" },
    { value: "tier4", label: "Tier 4 - Frontier (55+)" },
    { value: "---", label: "──────────────" },
    { value: "mega", label: "🔥 Mega Projects (5GW+)" },
    { value: "hot", label: "⚡ Hot Markets 2025" },
    { value: "stargate", label: "🌟 Stargate Sites" },
    { value: "---", label: "──────────────" },
    { value: "ohio", label: "Ohio Cluster" },
    { value: "texas", label: "Texas Markets" },
    { value: "pjm", label: "PJM Interconnection" },
    { value: "ercot", label: "ERCOT Grid" },
    { value: "midwest", label: "Midwest" },
    { value: "southeast", label: "Southeast" },
    { value: "west", label: "West Coast" },
    { value: "mountain", label: "Mountain West" }
];


// =====================================================
// SECTION 6: STARGATE PROJECT TRACKING
// Special data for OpenAI/Oracle/SoftBank Stargate sites
// =====================================================

const STARGATE_SITES = [
    { name: "Abilene, TX", status: "Primary", power: "5 GW", investment: "$100B", timeline: "2025-2028" },
    { name: "Texas Site 2", status: "Announced", power: "600 MW", investment: "TBD", timeline: "2026+" },
    { name: "New Mexico", status: "Planned", power: "TBD", investment: "TBD", timeline: "2026+" },
    { name: "Ohio", status: "Planned", power: "TBD", investment: "TBD", timeline: "2026+" },
    { name: "Michigan", status: "Planned", power: "TBD", investment: "TBD", timeline: "2027+" },
    { name: "Wisconsin", status: "Planned", power: "TBD", investment: "TBD", timeline: "2027+" },
    { name: "Wyoming", status: "Planned", power: "TBD", investment: "TBD", timeline: "2027+" },
    { name: "Pennsylvania", status: "Planned", power: "TBD", investment: "TBD", timeline: "2027+" },
    { name: "Georgia", status: "Planned", power: "TBD", investment: "TBD", timeline: "2027+" }
];

// Total Stargate capacity: 7 GW across all sites
// Total investment: $400B+ announced


// =====================================================
// SECTION 7: 2024-2025 MEGA PROJECTS SUMMARY
// Quick reference for largest announced projects
// =====================================================

const MEGA_PROJECTS_2025 = [
    { project: "Fermi/UT AI Campus", location: "Amarillo, TX", power: "11 GW", investment: "TBD", company: "Fermi America/UT System" },
    { project: "OpenAI Stargate", location: "Abilene, TX", power: "5+ GW", investment: "$100B+", company: "OpenAI/Oracle/SoftBank" },
    { project: "Meta Hyperion", location: "Monroe, LA", power: "5 GW", investment: "TBD", company: "Meta" },
    { project: "Caterpillar/Joule Campus", location: "Eagle Mountain, UT", power: "4 GW", investment: "TBD", company: "Caterpillar/Joule Capital" },
    { project: "TECfusions Keystone", location: "Upper Burrell, PA", power: "3 GW", investment: "TBD", company: "TECfusions" },
    { project: "Anthropic/Fluidstack", location: "NY & TX", power: "TBD", investment: "$50B", company: "Anthropic/Fluidstack" },
    { project: "Compass Meridian", location: "Lauderdale, MS", power: "320 MW", investment: "$10B", company: "Compass Datacenters" },
    { project: "Cologix Johnstown", location: "Johnstown, OH", power: "800 MW", investment: "$7B", company: "Cologix" },
    { project: "Microsoft Fairwater", location: "Fairwater, WI", power: "1 GW", investment: "$7B", company: "Microsoft" },
    { project: "Quantica Big Sky", location: "Billings, MT", power: "TBD", investment: "TBD", company: "EnCap/Quantica" }
];


// =====================================================
// SECTION 8: STATE TAX INCENTIVES LAYER DATA
// Enhance your Tax Incentives layer with this data
// =====================================================

const STATE_TAX_INCENTIVES = {
    "VA": { rating: "A+", incentives: ["Sales Tax Exemption ($150M+ threshold)", "Enterprise Zone Credits", "VEDP Grants"], highlight: true },
    "TX": { rating: "A+", incentives: ["Chapter 313 (expiring)", "No State Income Tax", "Foreign Trade Zones", "Property Tax Abatement"], highlight: true },
    "OH": { rating: "A", incentives: ["Sales Tax Exemption", "Job Creation Tax Credit", "CRA Abatements", "Data Center Incentive Program"], highlight: true },
    "NV": { rating: "A", incentives: ["Sales Tax Abatement", "No Corporate Income Tax", "No Personal Income Tax", "Property Tax Abatement"], highlight: true },
    "WY": { rating: "A", incentives: ["No Corporate Income Tax", "No Personal Income Tax", "Sales Tax Exemption"], highlight: true },
    "SD": { rating: "A", incentives: ["No Corporate Income Tax", "No Personal Income Tax", "Property Tax Freeze"], highlight: true },
    "NC": { rating: "A-", incentives: ["JDIG Grants", "Sales Tax Exemption", "One NC Fund"], highlight: false },
    "GA": { rating: "A-", incentives: ["Job Tax Credit ($4,000/job)", "Sales Tax Exemption", "Port Tax Credit Bonus"], highlight: false },
    "IN": { rating: "B+", incentives: ["EDGE Tax Credit", "Property Tax Abatement", "TIF Districts", "Hoosier Energy Rebates"], highlight: false },
    "NE": { rating: "B+", incentives: ["Nebraska Advantage Act", "Property Tax Exemption", "ImagiNE Nebraska"], highlight: false },
    "AZ": { rating: "B+", incentives: ["GPLET Abatement", "Foreign Trade Zone", "Quality Jobs Tax Credit"], highlight: false },
    "WI": { rating: "B+", incentives: ["Enterprise Zone Credits", "TIF Districts", "Property Tax Exemption"], highlight: false },
    "UT": { rating: "B+", incentives: ["EDTIF Tax Credit", "Enterprise Zone", "Industrial Assistance Fund"], highlight: false },
    "PA": { rating: "B", incentives: ["KOZ Tax Exemption", "RACP Grants", "Job Creation Tax Credit"], highlight: false },
    "LA": { rating: "B", incentives: ["Industrial Tax Exemption", "Quality Jobs Program", "Enterprise Zone"], highlight: false },
    "MS": { rating: "B", incentives: ["Fee-in-Lieu", "Job Tax Credit", "Sales Tax Exemption"], highlight: false },
    "AL": { rating: "B", incentives: ["Abatement Program", "Job Creation Credit", "Site Preparation"], highlight: false }
};


// =====================================================
// USAGE INSTRUCTIONS
// =====================================================
/*
1. Copy the NEW_MARKETS_2025 array and add to your existing markets data

2. In your map initialization, call:
   addNewMarketsToMap(map, yourMarkersLayerGroup);

3. Update your region dropdown with MARKET_REGIONS options

4. Add the MEGA_PROJECTS_2025 data to create a "Mega Projects" info panel

5. Use STATE_TAX_INCENTIVES to enhance your Tax Incentives layer

6. The createMarketPopup() function will generate rich popups with
   project info and status badges

Total markets after enhancement: 115+
Including all major 2024-2025 announcements through December 2025
*/
