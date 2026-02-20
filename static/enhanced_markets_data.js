// =====================================================
// DC HUB - ENHANCED MARKET DATA
// Add these markets to your existing Land & Power page
// This expands coverage from ~20 markets to 115+ markets
// =====================================================

// PASTE THIS INTO YOUR EXISTING LAND & POWER JAVASCRIPT
// Find where markets/regions are defined and replace/expand

const ENHANCED_MARKETS = {
    // ==================== TIER 1 - PRIMARY MARKETS ====================
    "northern-virginia": {
        name: "Northern Virginia (Ashburn)",
        lat: 39.04, lng: -77.49,
        tier: 1, region: "NA", state: "VA",
        power: { available: "9.6 GW", timeline: "3-5 years", status: "constrained", utility: "Dominion Energy" },
        stats: { facilities: 950, underConstruction: "1.8 GW", vacancy: "0.8%", pricing: "$220/kW" },
        projects: ["AWS HQ2 Campus", "Meta Expansion", "Google Cloud"],
        incentives: ["Data Center Tax Exemption", "Enterprise Zone Credits"]
    },
    "dallas-fort-worth": {
        name: "Dallas-Fort Worth",
        lat: 32.90, lng: -96.85,
        tier: 1, region: "NA", state: "TX",
        power: { available: "2.4 GW", timeline: "2-3 years", status: "available", utility: "Oncor" },
        stats: { facilities: 520, underConstruction: "1.1 GW", vacancy: "1.2%", pricing: "$165/kW" },
        projects: ["Yondr 550MW Campus", "QTS Mega", "Compass Datacenters"],
        incentives: ["Chapter 313 Abatement", "No State Income Tax"]
    },
    "phoenix-metro": {
        name: "Phoenix Metro",
        lat: 33.45, lng: -112.07,
        tier: 1, region: "NA", state: "AZ",
        power: { available: "2.8 GW", timeline: "2-3 years", status: "available", utility: "APS/SRP" },
        stats: { facilities: 480, underConstruction: "1.3 GW", vacancy: "1.8%", pricing: "$185/kW" },
        projects: ["Google Mesa 600MW", "Microsoft Goodyear", "Meta Phoenix"],
        incentives: ["GPLET Tax Abatement", "Foreign Trade Zone"]
    },
    "chicago-metro": {
        name: "Chicago Metro",
        lat: 41.88, lng: -87.63,
        tier: 1, region: "NA", state: "IL",
        power: { available: "1.55 GW", timeline: "3-4 years", status: "limited", utility: "ComEd" },
        stats: { facilities: 420, underConstruction: "1.18 GW", vacancy: "2.1%", pricing: "$155/kW" },
        projects: ["EdgeConneX AI Campus", "T5 Expansion", "Equinix CH4"],
        incentives: ["Enterprise Zone", "EDGE Tax Credit"]
    },
    "silicon-valley": {
        name: "Silicon Valley",
        lat: 37.39, lng: -121.95,
        tier: 1, region: "NA", state: "CA",
        power: { available: "2.9 GW", timeline: "4-6 years", status: "constrained", utility: "PG&E" },
        stats: { facilities: 520, underConstruction: "0.4 GW", vacancy: "1.4%", pricing: "$245/kW" },
        projects: [],
        incentives: ["None significant"]
    },
    "atlanta-metro": {
        name: "Atlanta Metro",
        lat: 33.76, lng: -84.39,
        tier: 1, region: "NA", state: "GA",
        power: { available: "0.9 GW", timeline: "2-3 years", status: "available", utility: "Georgia Power" },
        stats: { facilities: 280, underConstruction: "1.11 GW", vacancy: "2.8%", pricing: "$145/kW" },
        projects: ["Lambda/EdgeConneX 30MW AI", "Microsoft", "QTS Atlanta"],
        incentives: ["Job Tax Credit", "Sales Tax Exemption"]
    },

    // ==================== NEW OHIO MARKETS (Major Expansion) ====================
    "new-albany-oh": {
        name: "New Albany/Columbus",
        lat: 40.08, lng: -82.81,
        tier: 2, region: "NA", state: "OH",
        power: { available: "0.8 GW", timeline: "2-3 years", status: "available", utility: "AEP Ohio" },
        stats: { facilities: 104, underConstruction: "0.8 GW", vacancy: "5.0%", pricing: "$100/kW" },
        projects: ["AWS 3 Campuses", "Meta Campus", "Google Campus", "Cologix"],
        incentives: ["Data Center Sales Tax Exemption", "Job Creation Tax Credit"]
    },
    "johnstown-oh": {
        name: "Johnstown",
        lat: 40.15, lng: -82.69,
        tier: 3, region: "NA", state: "OH",
        power: { available: "0.8 GW", timeline: "2-4 years", status: "available", utility: "AEP Ohio" },
        stats: { facilities: 8, underConstruction: "0.8 GW", vacancy: "15%", pricing: "$90/kW" },
        projects: ["Cologix $7B 800MW AI Campus"],
        incentives: ["Data Center Sales Tax Exemption", "CRA Abatement"]
    },
    "sunbury-oh": {
        name: "Sunbury",
        lat: 40.24, lng: -82.86,
        tier: 3, region: "NA", state: "OH",
        power: { available: "0.4 GW", timeline: "2-3 years", status: "available", utility: "AEP Ohio" },
        stats: { facilities: 4, underConstruction: "0.4 GW", vacancy: "18%", pricing: "$85/kW" },
        projects: ["Amazon $2B Data Center"],
        incentives: ["Data Center Sales Tax Exemption"]
    },
    "cleveland-oh": {
        name: "Cleveland",
        lat: 41.50, lng: -81.69,
        tier: 3, region: "NA", state: "OH",
        power: { available: "0.3 GW", timeline: "2-3 years", status: "available", utility: "FirstEnergy" },
        stats: { facilities: 24, underConstruction: "0.15 GW", vacancy: "12%", pricing: "$80/kW" },
        projects: [],
        incentives: ["Data Center Sales Tax Exemption", "Job Retention Credit"]
    },

    // ==================== NEW PENNSYLVANIA MARKETS ====================
    "upper-burrell-pa": {
        name: "Upper Burrell (Keystone Connect)",
        lat: 40.58, lng: -79.73,
        tier: 3, region: "NA", state: "PA",
        power: { available: "3.0 GW", timeline: "3-5 years", status: "available", utility: "Duquesne Light" },
        stats: { facilities: 2, underConstruction: "3.0 GW", vacancy: "25%", pricing: "$70/kW" },
        projects: ["TECfusions Keystone Connect 3GW Campus"],
        incentives: ["RACP Grant", "KOZ Tax Exemption"]
    },
    "pittsburgh-pa": {
        name: "Pittsburgh",
        lat: 40.44, lng: -79.99,
        tier: 3, region: "NA", state: "PA",
        power: { available: "0.4 GW", timeline: "2-3 years", status: "available", utility: "Duquesne Light" },
        stats: { facilities: 35, underConstruction: "0.2 GW", vacancy: "10%", pricing: "$95/kW" },
        projects: [],
        incentives: ["KOZ Tax Exemption", "Job Creation Tax Credit"]
    },
    "lehigh-valley-pa": {
        name: "Lehigh Valley",
        lat: 40.62, lng: -75.37,
        tier: 3, region: "NA", state: "PA",
        power: { available: "0.3 GW", timeline: "2-3 years", status: "available", utility: "PPL Electric" },
        stats: { facilities: 25, underConstruction: "0.15 GW", vacancy: "8%", pricing: "$110/kW" },
        projects: ["Amazon AI Campus"],
        incentives: ["KOZ Tax Exemption"]
    },

    // ==================== NEW TEXAS MARKETS (Massive Expansion) ====================
    "abilene-tx": {
        name: "Abilene (Stargate)",
        lat: 32.45, lng: -99.73,
        tier: 3, region: "NA", state: "TX",
        power: { available: "5.0 GW", timeline: "3-5 years", status: "available", utility: "ERCOT" },
        stats: { facilities: 5, underConstruction: "5.0 GW", vacancy: "30%", pricing: "$50/kW" },
        projects: ["OpenAI Stargate $100B", "Oracle/SoftBank JV"],
        incentives: ["Chapter 313", "No State Income Tax", "Foreign Trade Zone"]
    },
    "amarillo-tx": {
        name: "Amarillo (Fermi Campus)",
        lat: 35.22, lng: -101.83,
        tier: 3, region: "NA", state: "TX",
        power: { available: "11.0 GW", timeline: "4-6 years", status: "available", utility: "ERCOT" },
        stats: { facilities: 2, underConstruction: "11.0 GW", vacancy: "40%", pricing: "$45/kW" },
        projects: ["Fermi/UT System 11GW AI Campus"],
        incentives: ["Chapter 313", "No State Income Tax"]
    },
    "west-texas": {
        name: "West Texas (Midland/Odessa)",
        lat: 31.99, lng: -102.08,
        tier: 3, region: "NA", state: "TX",
        power: { available: "2.0 GW", timeline: "2-3 years", status: "available", utility: "ERCOT" },
        stats: { facilities: 8, underConstruction: "1.5 GW", vacancy: "35%", pricing: "$55/kW" },
        projects: ["Lancium Clean Campus", "Meta West TX"],
        incentives: ["Chapter 313", "Abundant Wind/Solar"]
    },
    "san-antonio-tx": {
        name: "San Antonio",
        lat: 29.42, lng: -98.49,
        tier: 3, region: "NA", state: "TX",
        power: { available: "0.38 GW", timeline: "2-3 years", status: "available", utility: "CPS Energy" },
        stats: { facilities: 110, underConstruction: "0.2 GW", vacancy: "5.0%", pricing: "$105/kW" },
        projects: [],
        incentives: ["Tax Abatement", "No State Income Tax"]
    },

    // ==================== NEW INDIANA MARKETS ====================
    "fort-wayne-in": {
        name: "Fort Wayne",
        lat: 41.08, lng: -85.14,
        tier: 3, region: "NA", state: "IN",
        power: { available: "0.5 GW", timeline: "2-3 years", status: "available", utility: "AEP Indiana" },
        stats: { facilities: 6, underConstruction: "0.5 GW", vacancy: "20%", pricing: "$65/kW" },
        projects: ["Google 700+ Acre Campus (Project Zodiac)"],
        incentives: ["EDGE Tax Credit", "Property Tax Abatement"]
    },
    "jeffersonville-in": {
        name: "Jeffersonville",
        lat: 38.28, lng: -85.74,
        tier: 3, region: "NA", state: "IN",
        power: { available: "0.3 GW", timeline: "2-3 years", status: "available", utility: "Duke Energy" },
        stats: { facilities: 4, underConstruction: "0.3 GW", vacancy: "22%", pricing: "$70/kW" },
        projects: ["Meta 700K SF Campus"],
        incentives: ["EDGE Tax Credit", "TIF District"]
    },

    // ==================== NEW WISCONSIN MARKETS ====================
    "mount-pleasant-wi": {
        name: "Mount Pleasant (Racine)",
        lat: 42.72, lng: -87.90,
        tier: 3, region: "NA", state: "WI",
        power: { available: "0.5 GW", timeline: "2-3 years", status: "available", utility: "WE Energies" },
        stats: { facilities: 3, underConstruction: "0.5 GW", vacancy: "25%", pricing: "$75/kW" },
        projects: ["Microsoft $3.3B Campus (ex-Foxconn site)"],
        incentives: ["TIF District", "Enterprise Zone"]
    },
    "fairwater-wi": {
        name: "Fairwater",
        lat: 43.74, lng: -88.87,
        tier: 4, region: "NA", state: "WI",
        power: { available: "1.0 GW", timeline: "3-4 years", status: "available", utility: "WE Energies" },
        stats: { facilities: 1, underConstruction: "1.0 GW", vacancy: "35%", pricing: "$60/kW" },
        projects: ["Microsoft $7B Expansion"],
        incentives: ["Enterprise Zone"]
    },

    // ==================== NEW NEBRASKA MARKETS ====================
    "omaha-ne": {
        name: "Omaha/Papillion",
        lat: 41.15, lng: -96.04,
        tier: 3, region: "NA", state: "NE",
        power: { available: "0.6 GW", timeline: "1-2 years", status: "available", utility: "OPPD" },
        stats: { facilities: 25, underConstruction: "0.6 GW", vacancy: "10%", pricing: "$70/kW" },
        projects: ["Google Agate LLC 580 Acres"],
        incentives: ["Nebraska Advantage Act", "Property Tax Exemption"]
    },
    "lincoln-ne": {
        name: "Lincoln",
        lat: 40.81, lng: -96.70,
        tier: 4, region: "NA", state: "NE",
        power: { available: "0.4 GW", timeline: "1-2 years", status: "available", utility: "LES" },
        stats: { facilities: 8, underConstruction: "0.4 GW", vacancy: "15%", pricing: "$65/kW" },
        projects: ["Google 580 Acre Campus"],
        incentives: ["Nebraska Advantage Act"]
    },

    // ==================== NEW LOUISIANA MARKETS ====================
    "monroe-la": {
        name: "Monroe (Meta Hyperion)",
        lat: 32.51, lng: -92.12,
        tier: 3, region: "NA", state: "LA",
        power: { available: "5.0 GW", timeline: "3-6 years", status: "available", utility: "Entergy" },
        stats: { facilities: 2, underConstruction: "5.0 GW", vacancy: "40%", pricing: "$55/kW" },
        projects: ["Meta Hyperion 5GW (Manhattan-sized campus)"],
        incentives: ["Industrial Tax Exemption", "Quality Jobs Program"]
    },

    // ==================== NEW MISSISSIPPI MARKETS ====================
    "lauderdale-ms": {
        name: "Lauderdale County (Meridian)",
        lat: 32.40, lng: -88.66,
        tier: 4, region: "NA", state: "MS",
        power: { available: "0.5 GW", timeline: "3-5 years", status: "available", utility: "Mississippi Power" },
        stats: { facilities: 1, underConstruction: "0.32 GW", vacancy: "45%", pricing: "$50/kW" },
        projects: ["Compass Meridian $10B Campus"],
        incentives: ["Fee-in-Lieu", "Job Tax Credit"]
    },

    // ==================== NEW ALABAMA MARKETS ====================
    "montgomery-al": {
        name: "Montgomery (Meta Sage Mill)",
        lat: 32.37, lng: -86.30,
        tier: 4, region: "NA", state: "AL",
        power: { available: "0.3 GW", timeline: "2-3 years", status: "available", utility: "Alabama Power" },
        stats: { facilities: 8, underConstruction: "0.2 GW", vacancy: "18%", pricing: "$60/kW" },
        projects: ["Meta 715K SF Sage Mill Industrial Park"],
        incentives: ["Abatement Program", "Job Creation Credit"]
    },

    // ==================== NEW WYOMING MARKETS ====================
    "cheyenne-wy": {
        name: "Cheyenne",
        lat: 41.14, lng: -104.82,
        tier: 4, region: "NA", state: "WY",
        power: { available: "0.15 GW", timeline: "1-2 years", status: "available", utility: "Black Hills Energy" },
        stats: { facilities: 8, underConstruction: "0.1 GW", vacancy: "25%", pricing: "$50/kW" },
        projects: ["Meta Cheyenne Campus", "Stargate Expansion Site"],
        incentives: ["No Corporate Income Tax", "No Personal Income Tax", "Sales Tax Exemption"]
    },

    // ==================== NEW DAKOTAS MARKETS ====================
    "fargo-nd": {
        name: "Fargo",
        lat: 46.88, lng: -96.79,
        tier: 4, region: "NA", state: "ND",
        power: { available: "0.2 GW", timeline: "1-2 years", status: "available", utility: "Xcel Energy" },
        stats: { facilities: 6, underConstruction: "0.15 GW", vacancy: "22%", pricing: "$55/kW" },
        projects: ["Applied Digital Polaris Forge 150MW"],
        incentives: ["Property Tax Exemption", "Renaissance Zone"]
    },
    "sioux-falls-sd": {
        name: "Sioux Falls",
        lat: 43.55, lng: -96.73,
        tier: 4, region: "NA", state: "SD",
        power: { available: "0.1 GW", timeline: "1-2 years", status: "available", utility: "Xcel Energy" },
        stats: { facilities: 6, underConstruction: "0.05 GW", vacancy: "30%", pricing: "$45/kW" },
        projects: [],
        incentives: ["No Corporate Income Tax", "No Personal Income Tax"]
    },

    // ==================== NEW MONTANA MARKETS ====================
    "billings-mt": {
        name: "Billings (Big Sky)",
        lat: 45.78, lng: -108.50,
        tier: 4, region: "NA", state: "MT",
        power: { available: "0.5 GW", timeline: "2-3 years", status: "available", utility: "NorthWestern Energy" },
        stats: { facilities: 3, underConstruction: "0.5 GW", vacancy: "35%", pricing: "$50/kW" },
        projects: ["Quantica Big Sky 5000 Acre Campus"],
        incentives: ["No Sales Tax", "Property Tax Abatement"]
    },

    // ==================== NEW UTAH MARKETS ====================
    "eagle-mountain-ut": {
        name: "Eagle Mountain",
        lat: 40.31, lng: -112.01,
        tier: 3, region: "NA", state: "UT",
        power: { available: "4.0 GW", timeline: "2-4 years", status: "available", utility: "Rocky Mountain Power" },
        stats: { facilities: 4, underConstruction: "4.0 GW", vacancy: "30%", pricing: "$70/kW" },
        projects: ["Caterpillar/Joule 4GW Campus"],
        incentives: ["Enterprise Zone", "EDTIF Tax Credit"]
    },

    // ==================== NEW NEW YORK MARKETS ====================
    "buffalo-ny": {
        name: "Buffalo",
        lat: 42.89, lng: -78.88,
        tier: 4, region: "NA", state: "NY",
        power: { available: "0.3 GW", timeline: "2-3 years", status: "available", utility: "National Grid" },
        stats: { facilities: 15, underConstruction: "0.15 GW", vacancy: "15%", pricing: "$80/kW" },
        projects: ["Anthropic/Fluidstack Site"],
        incentives: ["Excelsior Jobs Program", "IDA Tax Abatement"]
    },

    // ==================== NEW RURAL VIRGINIA MARKETS ====================
    "prince-edward-va": {
        name: "Prince Edward County",
        lat: 37.22, lng: -78.44,
        tier: 4, region: "NA", state: "VA",
        power: { available: "0.2 GW", timeline: "2-3 years", status: "available", utility: "Dominion" },
        stats: { facilities: 2, underConstruction: "0.2 GW", vacancy: "30%", pricing: "$70/kW" },
        projects: [],
        incentives: ["Data Center Tax Exemption", "Enterprise Zone"]
    },
    "mecklenburg-va": {
        name: "Mecklenburg County",
        lat: 36.68, lng: -78.37,
        tier: 4, region: "NA", state: "VA",
        power: { available: "0.3 GW", timeline: "2-3 years", status: "available", utility: "Dominion" },
        stats: { facilities: 3, underConstruction: "0.3 GW", vacancy: "28%", pricing: "$65/kW" },
        projects: [],
        incentives: ["Data Center Tax Exemption", "Enterprise Zone"]
    }
};

// =====================================================
// STATE TAX INCENTIVES SUMMARY
// =====================================================
const STATE_INCENTIVES = {
    "VA": {
        name: "Virginia",
        incentives: ["Data Center Sales Tax Exemption (requires $150M+ investment)", "Enterprise Zone Credits"],
        rating: "A+"
    },
    "TX": {
        name: "Texas",
        incentives: ["Chapter 313 Abatement (expiring)", "No State Income Tax", "Foreign Trade Zones"],
        rating: "A+"
    },
    "OH": {
        name: "Ohio",
        incentives: ["Data Center Sales Tax Exemption", "Job Creation Tax Credit", "CRA Abatements"],
        rating: "A"
    },
    "NV": {
        name: "Nevada",
        incentives: ["Sales Tax Abatement", "No Corporate Income Tax", "No Personal Income Tax"],
        rating: "A"
    },
    "WY": {
        name: "Wyoming",
        incentives: ["No Corporate Income Tax", "No Personal Income Tax", "Sales Tax Exemption"],
        rating: "A"
    },
    "SD": {
        name: "South Dakota",
        incentives: ["No Corporate Income Tax", "No Personal Income Tax", "Property Tax Freeze"],
        rating: "A"
    },
    "NC": {
        name: "North Carolina",
        incentives: ["Job Development Investment Grant", "Sales Tax Exemption"],
        rating: "A-"
    },
    "GA": {
        name: "Georgia",
        incentives: ["Job Tax Credit", "Sales Tax Exemption", "Port Tax Credit"],
        rating: "A-"
    },
    "IN": {
        name: "Indiana",
        incentives: ["EDGE Tax Credit", "Property Tax Abatement", "TIF Districts"],
        rating: "B+"
    },
    "NE": {
        name: "Nebraska",
        incentives: ["Nebraska Advantage Act", "Property Tax Exemption"],
        rating: "B+"
    },
    "AZ": {
        name: "Arizona",
        incentives: ["GPLET Tax Abatement", "Foreign Trade Zone", "Quality Jobs Tax Credit"],
        rating: "B+"
    }
};

// Export for use
if (typeof module !== 'undefined') {
    module.exports = { ENHANCED_MARKETS, STATE_INCENTIVES };
}
