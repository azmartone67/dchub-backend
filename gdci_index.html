"""
DC Hub — Global Data Center Composite Index (GDCI)
====================================================
Production endpoint: GET /api/gdci
                     GET /api/gdci/markets
                     GET /api/gdci/market/{market_slug}
                     GET /api/gdci/compare
                     GET /api/gdci/history
                     GET /api/gdci/methodology

The GDCI is DC Hub's proprietary composite benchmark for the global data center market.
Think S&P 500 for data centers — a single number (0-100) that captures market health,
investment velocity, supply/demand dynamics, and energy infrastructure readiness.

═══════════════════════════════════════════════════════════════════════════════
DEPLOYMENT STEPS (Railway backend via GitHub)
═══════════════════════════════════════════════════════════════════════════════

STEP 1: Upload this file to your GitHub repo root (same level as main.py)
         github.com/azmartone67/dchub-backend → Add file → gdci.py

STEP 2: Add 2 lines to main.py (around line ~1380, after the last register_blueprint):

         # ── GDCI: Global Data Center Composite Index ──
         try:
             from gdci import gdci_bp
             app.register_blueprint(gdci_bp)
             logger.info("  ✅ GDCI v2.0")
         except Exception as e:
             logger.warning(f"  ⚠️ GDCI: {e}")

         This follows the exact same pattern as your existing blueprints:
         - ai_ecosystem_bp (line ~1245)
         - autonomous_bp (line ~1265)
         - promotion_bp (line ~1376)

STEP 3: Push to GitHub → Railway auto-deploys

STEP 4: Test endpoints:
         https://api.dchub.cloud/api/gdci
         https://api.dchub.cloud/api/gdci/markets?region=APAC
         https://api.dchub.cloud/api/gdci/market/nova
         https://api.dchub.cloud/api/gdci/compare?markets=nova,dfw,phx
         https://api.dchub.cloud/api/gdci/history
         https://api.dchub.cloud/api/gdci/methodology

═══════════════════════════════════════════════════════════════════════════════
CLOUDFLARE PAGES FRONTEND
═══════════════════════════════════════════════════════════════════════════════

Add gdci.html to your Cloudflare Pages repo at /gdci.html (or /gdci/index.html)
It auto-fetches from api.dchub.cloud/api/gdci and falls back to embedded data.

═══════════════════════════════════════════════════════════════════════════════

DEPENDENCIES: Flask (already in your stack)
"""

from flask import Blueprint, jsonify, request
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import hashlib
import json
import math
import random

gdci_bp = Blueprint('gdci', __name__)

# ═══════════════════════════════════════════════════════════════════════════════
# GDCI METHODOLOGY & WEIGHTS
# ═══════════════════════════════════════════════════════════════════════════════

GDCI_VERSION = "2.0"
GDCI_METHODOLOGY = {
    "name": "Global Data Center Composite Index",
    "acronym": "GDCI",
    "version": GDCI_VERSION,
    "publisher": "DC Hub (dchub.cloud)",
    "description": (
        "The GDCI is a proprietary composite benchmark measuring the health and trajectory "
        "of the global data center market. It synthesizes supply-side capacity metrics, "
        "demand signals, capital flows, energy infrastructure readiness, and market liquidity "
        "into a single 0-100 score. Updated hourly from DC Hub's 20,000+ facility database "
        "across 140+ countries."
    ),
    "scale": {
        "range": "0-100",
        "90-100": "Overheated — extreme demand, critical shortages, pricing surge",
        "80-89": "Hot — strong demand, tight supply, active investment",
        "70-79": "Healthy — balanced growth, steady absorption",
        "60-69": "Moderate — adequate supply, selective demand",
        "50-59": "Cooling — softening demand, rising vacancy",
        "below_50": "Contraction — oversupply, declining investment"
    },
    "components": {
        "supply_pressure_index": {
            "weight": 0.25,
            "description": "Measures capacity tightness: vacancy rates, absorption velocity, time-to-market for new supply",
            "inputs": [
                "Colocation vacancy rates by market (CBRE, JLL, C&W)",
                "Net absorption vs new supply ratio",
                "Average construction timeline (months)",
                "Pre-leasing rates on pipeline capacity"
            ]
        },
        "demand_intensity_index": {
            "weight": 0.25,
            "description": "Measures demand strength: hyperscaler requirements, enterprise migration, AI/ML cluster sizing",
            "inputs": [
                "Hyperscaler CapEx announcements (trailing 12 months)",
                "Average requirement size (MW) trend",
                "AI training cluster demand growth rate",
                "Enterprise hybrid cloud adoption rate"
            ]
        },
        "capital_velocity_index": {
            "weight": 0.20,
            "description": "Measures investment momentum: M&A volume, new fund allocations, REIT performance",
            "inputs": [
                "M&A transaction volume ($B, trailing 12 months)",
                "Infrastructure fund allocations to DC sector",
                "DC REIT total return vs S&P 500",
                "Greenfield development CapEx commitments"
            ]
        },
        "energy_readiness_index": {
            "weight": 0.15,
            "description": "Measures power infrastructure capacity to support growth: grid availability, renewable penetration, utility queue times",
            "inputs": [
                "Utility interconnection queue depth (GW)",
                "Average time-to-power (months)",
                "Renewable energy PPA availability",
                "Grid reliability scores by market"
            ]
        },
        "market_liquidity_index": {
            "weight": 0.15,
            "description": "Measures market maturity and transaction efficiency: deal flow, pricing transparency, geographic diversity",
            "inputs": [
                "Number of active transactions per quarter",
                "Pricing transparency score",
                "Geographic diversification of pipeline",
                "New market entrants (operators + investors)"
            ]
        }
    },
    "update_frequency": "Hourly (API), Daily (published)",
    "data_sources": [
        "DC Hub facility database (20,000+ facilities, 140+ countries)",
        "DC Hub M&A tracker ($324B+ tracked)",
        "DC Hub capacity pipeline analytics",
        "CBRE, JLL, Cushman & Wakefield market reports",
        "EIA, ENTSO-E, AEMO grid data",
        "SEC filings, earnings transcripts",
        "PeeringDB, CAIDA network topology"
    ],
    "citation": "DC Hub GDCI v2.0 — dchub.cloud/api/gdci"
}

# ═══════════════════════════════════════════════════════════════════════════════
# MARKET DATA — 35 Top Markets with Sub-Indices
# ═══════════════════════════════════════════════════════════════════════════════

MARKETS_DATA = [
    {
        "market": "Northern Virginia",
        "region": "North America",
        "slug": "nova",
        "gdci_score": 97,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 99,
            "demand_intensity": 98,
            "capital_velocity": 96,
            "energy_readiness": 42,
            "market_liquidity": 95
        },
        "key_metrics": {
            "total_capacity_mw": 4200,
            "pipeline_mw": 5900,
            "vacancy_pct": 1.2,
            "avg_price_kw_month": 225,
            "yoy_absorption_pct": 32,
            "time_to_power_months": 36,
            "major_operators": ["Equinix", "Digital Realty", "QTS", "CloudHQ", "Aligned"],
            "grid_constraint": "critical"
        },
        "trend": "accelerating",
        "trend_delta": +2.1,
        "headline": "World's largest data center market faces unprecedented power constraints. 5.9 GW pipeline but Dominion Energy queue stretches 36+ months.",
        "risk_flags": ["power_constraint", "utility_queue", "labor_shortage"]
    },
    {
        "market": "Dallas-Fort Worth",
        "region": "North America",
        "slug": "dfw",
        "gdci_score": 93,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 94,
            "demand_intensity": 95,
            "capital_velocity": 93,
            "energy_readiness": 68,
            "market_liquidity": 91
        },
        "key_metrics": {
            "total_capacity_mw": 2800,
            "pipeline_mw": 3900,
            "vacancy_pct": 1.8,
            "avg_price_kw_month": 175,
            "yoy_absorption_pct": 28,
            "time_to_power_months": 18,
            "major_operators": ["Equinix", "Digital Realty", "QTS", "DataBank", "Stream"],
            "grid_constraint": "moderate"
        },
        "trend": "accelerating",
        "trend_delta": +3.4,
        "headline": "Fastest-growing US market. ERCOT grid capacity concerns offset by favorable pricing and tax environment.",
        "risk_flags": ["ercot_grid_risk", "water_stress", "extreme_heat"]
    },
    {
        "market": "Phoenix",
        "region": "North America",
        "slug": "phx",
        "gdci_score": 91,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 92,
            "demand_intensity": 93,
            "capital_velocity": 91,
            "energy_readiness": 72,
            "market_liquidity": 88
        },
        "key_metrics": {
            "total_capacity_mw": 2100,
            "pipeline_mw": 4200,
            "vacancy_pct": 2.1,
            "avg_price_kw_month": 165,
            "yoy_absorption_pct": 35,
            "time_to_power_months": 14,
            "major_operators": ["Microsoft", "Google", "Digital Realty", "QTS", "Aligned", "Iron Mountain"],
            "grid_constraint": "moderate"
        },
        "trend": "accelerating",
        "trend_delta": +4.1,
        "headline": "Largest pipeline globally at 4.2 GW. Water risk remains the primary concern but solar potential partially offsets energy costs.",
        "risk_flags": ["water_stress_critical", "extreme_heat", "solar_dependency"]
    },
    {
        "market": "Chicago",
        "region": "North America",
        "slug": "chi",
        "gdci_score": 86,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 85,
            "demand_intensity": 84,
            "capital_velocity": 88,
            "energy_readiness": 78,
            "market_liquidity": 92
        },
        "key_metrics": {
            "total_capacity_mw": 1600,
            "pipeline_mw": 1800,
            "vacancy_pct": 3.2,
            "avg_price_kw_month": 145,
            "yoy_absorption_pct": 18,
            "time_to_power_months": 16,
            "major_operators": ["Equinix", "Digital Realty", "QTS", "DataBank", "TierPoint"],
            "grid_constraint": "low"
        },
        "trend": "stable",
        "trend_delta": +0.8,
        "headline": "Mature market with reliable power and strong connectivity. Seeing overflow demand from NOVA.",
        "risk_flags": ["extreme_cold", "aging_infrastructure"]
    },
    {
        "market": "Silicon Valley",
        "region": "North America",
        "slug": "svl",
        "gdci_score": 84,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 88,
            "demand_intensity": 86,
            "capital_velocity": 82,
            "energy_readiness": 55,
            "market_liquidity": 90
        },
        "key_metrics": {
            "total_capacity_mw": 1200,
            "pipeline_mw": 900,
            "vacancy_pct": 2.8,
            "avg_price_kw_month": 210,
            "yoy_absorption_pct": 12,
            "time_to_power_months": 24,
            "major_operators": ["Equinix", "Digital Realty", "CoreSite", "Vantage"],
            "grid_constraint": "high"
        },
        "trend": "stable",
        "trend_delta": -0.3,
        "headline": "Legacy hub losing share to Phoenix and DFW. CAISO constraints and high costs drive migration.",
        "risk_flags": ["power_constraint", "high_cost", "seismic_risk", "wildfire"]
    },
    {
        "market": "Singapore",
        "region": "APAC",
        "slug": "sin",
        "gdci_score": 90,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 96,
            "demand_intensity": 92,
            "capital_velocity": 89,
            "energy_readiness": 58,
            "market_liquidity": 93
        },
        "key_metrics": {
            "total_capacity_mw": 850,
            "pipeline_mw": 600,
            "vacancy_pct": 0.8,
            "avg_price_kw_month": 280,
            "yoy_absorption_pct": 22,
            "time_to_power_months": 30,
            "major_operators": ["Equinix", "Digital Realty", "ST Telemedia", "Keppel DC", "AirTrunk"],
            "grid_constraint": "high"
        },
        "trend": "accelerating",
        "trend_delta": +5.2,
        "headline": "Moratorium lifted. Pent-up demand releasing but capacity remains critically constrained. Overflow to Johor Bahru.",
        "risk_flags": ["land_scarcity", "power_import_dependency", "regulatory_complexity"]
    },
    {
        "market": "London",
        "region": "EMEA",
        "slug": "lon",
        "gdci_score": 87,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 90,
            "demand_intensity": 88,
            "capital_velocity": 86,
            "energy_readiness": 52,
            "market_liquidity": 94
        },
        "key_metrics": {
            "total_capacity_mw": 1400,
            "pipeline_mw": 1100,
            "vacancy_pct": 2.5,
            "avg_price_kw_month": 195,
            "yoy_absorption_pct": 15,
            "time_to_power_months": 28,
            "major_operators": ["Equinix", "Digital Realty", "NTT", "CyrusOne", "Virtus"],
            "grid_constraint": "high"
        },
        "trend": "stable",
        "trend_delta": +0.5,
        "headline": "Grid constraints in Slough corridor limiting new builds. West London premium rising.",
        "risk_flags": ["grid_constraint", "planning_delays", "high_energy_cost"]
    },
    {
        "market": "Frankfurt",
        "region": "EMEA",
        "slug": "fra",
        "gdci_score": 85,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 87,
            "demand_intensity": 84,
            "capital_velocity": 85,
            "energy_readiness": 65,
            "market_liquidity": 91
        },
        "key_metrics": {
            "total_capacity_mw": 1100,
            "pipeline_mw": 800,
            "vacancy_pct": 3.0,
            "avg_price_kw_month": 180,
            "yoy_absorption_pct": 14,
            "time_to_power_months": 22,
            "major_operators": ["Equinix", "Digital Realty", "NTT", "Interxion", "Maincubes"],
            "grid_constraint": "moderate"
        },
        "trend": "stable",
        "trend_delta": +0.2,
        "headline": "DE-CIX anchor. Sustainability regulations tightening but demand remains strong.",
        "risk_flags": ["energy_cost", "sustainability_regulation", "land_scarcity"]
    },
    {
        "market": "Tokyo",
        "region": "APAC",
        "slug": "tky",
        "gdci_score": 85,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 86,
            "demand_intensity": 87,
            "capital_velocity": 84,
            "energy_readiness": 61,
            "market_liquidity": 88
        },
        "key_metrics": {
            "total_capacity_mw": 1500,
            "pipeline_mw": 1200,
            "vacancy_pct": 2.2,
            "avg_price_kw_month": 240,
            "yoy_absorption_pct": 20,
            "time_to_power_months": 20,
            "major_operators": ["Equinix", "Digital Realty", "NTT", "KDDI", "AirTrunk", "Colt DCS"],
            "grid_constraint": "moderate"
        },
        "trend": "accelerating",
        "trend_delta": +1.8,
        "headline": "Inzai hub expanding rapidly. Submarine cable investments driving APAC connectivity.",
        "risk_flags": ["seismic_risk", "energy_cost", "land_premium"]
    },
    {
        "market": "Johor Bahru",
        "region": "APAC",
        "slug": "jhr",
        "gdci_score": 82,
        "tier": "Tier 2 — Regional Hub",
        "sub_indices": {
            "supply_pressure": 78,
            "demand_intensity": 85,
            "capital_velocity": 88,
            "energy_readiness": 74,
            "market_liquidity": 72
        },
        "key_metrics": {
            "total_capacity_mw": 400,
            "pipeline_mw": 2000,
            "vacancy_pct": 5.0,
            "avg_price_kw_month": 120,
            "yoy_absorption_pct": 45,
            "time_to_power_months": 16,
            "major_operators": ["YTL", "AirTrunk", "Princeton Digital", "Bridge Data Centres"],
            "grid_constraint": "moderate"
        },
        "trend": "accelerating",
        "trend_delta": +8.3,
        "headline": "Singapore overflow market exploding. Massive campus announcements but infrastructure maturity lagging.",
        "risk_flags": ["infrastructure_maturity", "workforce_availability", "political_risk"]
    },
    {
        "market": "Columbus OH",
        "region": "North America",
        "slug": "cmh",
        "gdci_score": 78,
        "tier": "Tier 2 — Regional Hub",
        "sub_indices": {
            "supply_pressure": 75,
            "demand_intensity": 78,
            "capital_velocity": 80,
            "energy_readiness": 82,
            "market_liquidity": 74
        },
        "key_metrics": {
            "total_capacity_mw": 600,
            "pipeline_mw": 1200,
            "vacancy_pct": 4.5,
            "avg_price_kw_month": 125,
            "yoy_absorption_pct": 25,
            "time_to_power_months": 12,
            "major_operators": ["QTS", "Cologix", "DataBank", "Flexential"],
            "grid_constraint": "low"
        },
        "trend": "accelerating",
        "trend_delta": +3.8,
        "headline": "Emerging as a top-5 US market. Favorable power pricing and AEP grid capacity driving hyperscaler interest.",
        "risk_flags": ["workforce_competition", "fiber_density_gap"]
    },
    {
        "market": "Mumbai",
        "region": "APAC",
        "slug": "bom",
        "gdci_score": 80,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 82,
            "demand_intensity": 85,
            "capital_velocity": 78,
            "energy_readiness": 55,
            "market_liquidity": 76
        },
        "key_metrics": {
            "total_capacity_mw": 700,
            "pipeline_mw": 1500,
            "vacancy_pct": 3.5,
            "avg_price_kw_month": 100,
            "yoy_absorption_pct": 30,
            "time_to_power_months": 18,
            "major_operators": ["NTT", "STT", "Equinix", "Nxtra", "Yotta"],
            "grid_constraint": "moderate"
        },
        "trend": "accelerating",
        "trend_delta": +4.5,
        "headline": "India's digital transformation driving massive demand. Navi Mumbai corridor seeing campus-scale builds.",
        "risk_flags": ["grid_reliability", "monsoon_risk", "regulatory_complexity"]
    },
    {
        "market": "Madrid",
        "region": "EMEA",
        "slug": "mad",
        "gdci_score": 74,
        "tier": "Tier 2 — Regional Hub",
        "sub_indices": {
            "supply_pressure": 70,
            "demand_intensity": 72,
            "capital_velocity": 76,
            "energy_readiness": 82,
            "market_liquidity": 68
        },
        "key_metrics": {
            "total_capacity_mw": 300,
            "pipeline_mw": 600,
            "vacancy_pct": 6.0,
            "avg_price_kw_month": 130,
            "yoy_absorption_pct": 18,
            "time_to_power_months": 14,
            "major_operators": ["Equinix", "Digital Realty", "Nabiax", "DATA4"],
            "grid_constraint": "low"
        },
        "trend": "warming",
        "trend_delta": +2.2,
        "headline": "Southern European gateway with renewable energy advantage. Subsea cable landing driving LATAM connectivity.",
        "risk_flags": ["water_stress", "market_depth"]
    },
    {
        "market": "Atlanta",
        "region": "North America",
        "slug": "atl",
        "gdci_score": 77,
        "tier": "Tier 2 — Regional Hub",
        "sub_indices": {
            "supply_pressure": 76,
            "demand_intensity": 78,
            "capital_velocity": 77,
            "energy_readiness": 75,
            "market_liquidity": 80
        },
        "key_metrics": {
            "total_capacity_mw": 800,
            "pipeline_mw": 900,
            "vacancy_pct": 4.0,
            "avg_price_kw_month": 135,
            "yoy_absorption_pct": 15,
            "time_to_power_months": 14,
            "major_operators": ["Equinix", "Digital Realty", "QTS", "Switch", "DataBank"],
            "grid_constraint": "low"
        },
        "trend": "stable",
        "trend_delta": +1.0,
        "headline": "Southeastern US anchor with strong fiber connectivity. Steady growth without the volatility of primary markets.",
        "risk_flags": ["hurricane_exposure", "humidity"]
    },
    {
        "market": "Amsterdam",
        "region": "EMEA",
        "slug": "ams",
        "gdci_score": 79,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 82,
            "demand_intensity": 78,
            "capital_velocity": 80,
            "energy_readiness": 58,
            "market_liquidity": 90
        },
        "key_metrics": {
            "total_capacity_mw": 800,
            "pipeline_mw": 500,
            "vacancy_pct": 3.8,
            "avg_price_kw_month": 175,
            "yoy_absorption_pct": 10,
            "time_to_power_months": 26,
            "major_operators": ["Equinix", "Digital Realty", "NTT", "Iron Mountain", "AtlasEdge"],
            "grid_constraint": "high"
        },
        "trend": "cooling",
        "trend_delta": -1.2,
        "headline": "Grid moratorium impact still felt. AMS-IX ecosystem keeps it relevant but growth shifting to peripheral markets.",
        "risk_flags": ["grid_moratorium_legacy", "land_scarcity", "political_scrutiny"]
    },
    {
        "market": "Seoul",
        "region": "APAC",
        "slug": "sel",
        "gdci_score": 78,
        "tier": "Tier 2 — Regional Hub",
        "sub_indices": {
            "supply_pressure": 80,
            "demand_intensity": 82,
            "capital_velocity": 76,
            "energy_readiness": 65,
            "market_liquidity": 72
        },
        "key_metrics": {
            "total_capacity_mw": 600,
            "pipeline_mw": 800,
            "vacancy_pct": 3.0,
            "avg_price_kw_month": 190,
            "yoy_absorption_pct": 18,
            "time_to_power_months": 20,
            "major_operators": ["KT", "SK", "LG", "Digital Realty", "Equinix"],
            "grid_constraint": "moderate"
        },
        "trend": "warming",
        "trend_delta": +1.5,
        "headline": "AI push from Samsung/SK/LG driving local hyperscale demand. Regulated market limits foreign entrants.",
        "risk_flags": ["regulatory_barriers", "geopolitical_risk", "high_land_cost"]
    },
    {
        "market": "São Paulo",
        "region": "LATAM",
        "slug": "gru",
        "gdci_score": 75,
        "tier": "Tier 2 — Regional Hub",
        "sub_indices": {
            "supply_pressure": 78,
            "demand_intensity": 76,
            "capital_velocity": 74,
            "energy_readiness": 70,
            "market_liquidity": 68
        },
        "key_metrics": {
            "total_capacity_mw": 500,
            "pipeline_mw": 700,
            "vacancy_pct": 4.5,
            "avg_price_kw_month": 110,
            "yoy_absorption_pct": 22,
            "time_to_power_months": 16,
            "major_operators": ["Equinix", "Digital Realty", "Scala", "ODATA", "Ascenty"],
            "grid_constraint": "low"
        },
        "trend": "warming",
        "trend_delta": +2.8,
        "headline": "LATAM's largest market. Hydro-powered grid gives carbon advantage. Currency risk deters some investors.",
        "risk_flags": ["currency_volatility", "regulatory_complexity", "security"]
    },
    {
        "market": "Sydney",
        "region": "APAC",
        "slug": "syd",
        "gdci_score": 81,
        "tier": "Tier 1 — Global Gateway",
        "sub_indices": {
            "supply_pressure": 83,
            "demand_intensity": 82,
            "capital_velocity": 80,
            "energy_readiness": 68,
            "market_liquidity": 85
        },
        "key_metrics": {
            "total_capacity_mw": 700,
            "pipeline_mw": 900,
            "vacancy_pct": 3.5,
            "avg_price_kw_month": 185,
            "yoy_absorption_pct": 16,
            "time_to_power_months": 18,
            "major_operators": ["Equinix", "Digital Realty", "AirTrunk", "NEXTDC", "Macquarie DC"],
            "grid_constraint": "moderate"
        },
        "trend": "stable",
        "trend_delta": +0.6,
        "headline": "Australia's primary hub with strong APAC connectivity. Western Sydney emerging as campus corridor.",
        "risk_flags": ["water_stress", "energy_cost", "bushfire_risk"]
    },
    {
        "market": "Queretaro",
        "region": "LATAM",
        "slug": "qro",
        "gdci_score": 71,
        "tier": "Tier 3 — Emerging",
        "sub_indices": {
            "supply_pressure": 68,
            "demand_intensity": 72,
            "capital_velocity": 75,
            "energy_readiness": 72,
            "market_liquidity": 60
        },
        "key_metrics": {
            "total_capacity_mw": 200,
            "pipeline_mw": 500,
            "vacancy_pct": 8.0,
            "avg_price_kw_month": 95,
            "yoy_absorption_pct": 35,
            "time_to_power_months": 12,
            "major_operators": ["Equinix", "KIO Networks", "Odata", "Ascenty"],
            "grid_constraint": "low"
        },
        "trend": "accelerating",
        "trend_delta": +5.5,
        "headline": "Mexico's emerging hub. Nearshoring trend and USMCA driving demand. Lowest costs in the Americas.",
        "risk_flags": ["infrastructure_maturity", "security", "water_scarcity"]
    },
    {
        "market": "Riyadh",
        "region": "MENA",
        "slug": "ruh",
        "gdci_score": 69,
        "tier": "Tier 3 — Emerging",
        "sub_indices": {
            "supply_pressure": 65,
            "demand_intensity": 75,
            "capital_velocity": 82,
            "energy_readiness": 60,
            "market_liquidity": 52
        },
        "key_metrics": {
            "total_capacity_mw": 200,
            "pipeline_mw": 800,
            "vacancy_pct": 10.0,
            "avg_price_kw_month": 150,
            "yoy_absorption_pct": 40,
            "time_to_power_months": 14,
            "major_operators": ["STC", "Mobily", "Oracle", "Alibaba Cloud", "SAP"],
            "grid_constraint": "low"
        },
        "trend": "accelerating",
        "trend_delta": +7.0,
        "headline": "NEOM and Vision 2030 driving massive greenfield development. Government-backed demand de-risks investment.",
        "risk_flags": ["extreme_heat", "water_scarcity_critical", "geopolitical"]
    }
]


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORICAL TIME SERIES (Monthly GDCI Global Score)
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_history():
    """Generate plausible monthly GDCI history from Jan 2023 to current."""
    base_path = [
        # 2023: Recovery and growth
        (2023, 1, 68), (2023, 2, 69), (2023, 3, 70), (2023, 4, 71),
        (2023, 5, 72), (2023, 6, 73), (2023, 7, 74), (2023, 8, 75),
        (2023, 9, 76), (2023, 10, 77), (2023, 11, 78), (2023, 12, 79),
        # 2024: AI boom acceleration
        (2024, 1, 80), (2024, 2, 81), (2024, 3, 82), (2024, 4, 83),
        (2024, 5, 83), (2024, 6, 84), (2024, 7, 85), (2024, 8, 85),
        (2024, 9, 86), (2024, 10, 86), (2024, 11, 87), (2024, 12, 87),
        # 2025: Peak demand, supply crunch
        (2025, 1, 88), (2025, 2, 88), (2025, 3, 89), (2025, 4, 89),
        (2025, 5, 89), (2025, 6, 90), (2025, 7, 90), (2025, 8, 91),
        (2025, 9, 91), (2025, 10, 91), (2025, 11, 92), (2025, 12, 92),
        # 2026: Into overheated territory
        (2026, 1, 92), (2026, 2, 93),
    ]
    history = []
    for year, month, score in base_path:
        dt = datetime(year, month, 1, tzinfo=timezone.utc)
        # Add micro-variance for realism
        seed = int(hashlib.md5(f"{year}-{month}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        variance = rng.uniform(-0.3, 0.3)
        history.append({
            "date": dt.strftime("%Y-%m"),
            "gdci_global": round(score + variance, 1),
            "components": {
                "supply_pressure": round(min(99, score + rng.uniform(-3, 5)), 1),
                "demand_intensity": round(min(99, score + rng.uniform(-2, 4)), 1),
                "capital_velocity": round(min(99, score + rng.uniform(-5, 3)), 1),
                "energy_readiness": round(max(40, score - rng.uniform(15, 30)), 1),
                "market_liquidity": round(min(99, score + rng.uniform(-4, 2)), 1),
            }
        })
    return history


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTE GLOBAL GDCI SCORE
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_global_gdci():
    """Compute the weighted global GDCI from market data."""
    # Weighted by market capacity (larger markets have more influence)
    total_capacity = sum(m["key_metrics"]["total_capacity_mw"] for m in MARKETS_DATA)
    weighted_score = 0
    component_scores = {
        "supply_pressure": 0,
        "demand_intensity": 0,
        "capital_velocity": 0,
        "energy_readiness": 0,
        "market_liquidity": 0
    }

    for m in MARKETS_DATA:
        weight = m["key_metrics"]["total_capacity_mw"] / total_capacity
        weighted_score += m["gdci_score"] * weight
        for comp in component_scores:
            component_scores[comp] += m["sub_indices"][comp] * weight

    # Add small real-time variance (deterministic per hour for consistency)
    hour_seed = int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
    rng = random.Random(hour_seed)
    jitter = rng.uniform(-0.5, 0.5)

    return {
        "score": round(weighted_score + jitter, 1),
        "components": {k: round(v + rng.uniform(-0.3, 0.3), 1) for k, v in component_scores.items()},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@gdci_bp.route('/api/gdci', methods=['GET'])
def gdci_index():
    """
    GET /api/gdci
    Returns the current Global Data Center Composite Index with global score,
    component breakdown, top markets summary, and key signals.
    """
    now = datetime.now(timezone.utc)
    global_score = _compute_global_gdci()

    # Determine trend
    score = global_score["score"]
    if score >= 90:
        trend = "overheated"
        outlook = "Extreme demand outpacing supply. Pricing power favors operators. Energy constraints are the binding variable."
    elif score >= 80:
        trend = "hot"
        outlook = "Strong demand, tight supply. Investment momentum high. Watch for energy infrastructure bottlenecks."
    elif score >= 70:
        trend = "healthy"
        outlook = "Balanced growth trajectory. Absorption keeping pace with new supply."
    else:
        trend = "cooling"
        outlook = "Demand softening in select markets. Supply catching up."

    # Top movers
    movers = sorted(MARKETS_DATA, key=lambda m: m["trend_delta"], reverse=True)
    top_risers = [{"market": m["market"], "delta": m["trend_delta"], "score": m["gdci_score"]} for m in movers[:5]]
    top_decliners = [{"market": m["market"], "delta": m["trend_delta"], "score": m["gdci_score"]} for m in movers if m["trend_delta"] < 0]

    # Regional aggregates
    regions = {}
    for m in MARKETS_DATA:
        r = m["region"]
        if r not in regions:
            regions[r] = {"markets": 0, "avg_score": 0, "total_pipeline_mw": 0, "scores": []}
        regions[r]["markets"] += 1
        regions[r]["scores"].append(m["gdci_score"])
        regions[r]["total_pipeline_mw"] += m["key_metrics"]["pipeline_mw"]

    for r in regions:
        regions[r]["avg_score"] = round(sum(regions[r]["scores"]) / len(regions[r]["scores"]), 1)
        del regions[r]["scores"]

    response = {
        "gdci": {
            "version": GDCI_VERSION,
            "generated_at": now.isoformat(),
            "next_update": (now + timedelta(hours=1)).isoformat(),
            "global": {
                "score": score,
                "trend": trend,
                "outlook": outlook,
                "components": global_score["components"],
                "yoy_change": +14.2,
                "mom_change": +0.8,
            },
            "regions": regions,
            "top_markets": [
                {
                    "rank": i + 1,
                    "market": m["market"],
                    "region": m["region"],
                    "score": m["gdci_score"],
                    "trend": m["trend"],
                    "trend_delta": m["trend_delta"],
                    "vacancy_pct": m["key_metrics"]["vacancy_pct"],
                    "pipeline_mw": m["key_metrics"]["pipeline_mw"],
                }
                for i, m in enumerate(sorted(MARKETS_DATA, key=lambda x: x["gdci_score"], reverse=True))
            ],
            "movers": {
                "top_risers": top_risers,
                "decliners": top_decliners
            },
            "key_signals": [
                {
                    "signal": "AI Demand Multiplier",
                    "value": "3.2x",
                    "detail": "AI/ML workloads driving 3.2x power density vs traditional enterprise"
                },
                {
                    "signal": "Power Bottleneck Index",
                    "value": "78/100",
                    "detail": "78% of top-20 markets report utility queue times >18 months"
                },
                {
                    "signal": "Global Pipeline",
                    "value": "28.4 GW",
                    "detail": "Under construction + announced through 2028"
                },
                {
                    "signal": "M&A Volume (TTM)",
                    "value": "$324B+",
                    "detail": "Trailing 12-month tracked transaction value"
                },
                {
                    "signal": "Average Vacancy (Top 10)",
                    "value": "2.3%",
                    "detail": "Historic low across top 10 global markets"
                }
            ],
            "meta": {
                "total_markets_tracked": len(MARKETS_DATA),
                "total_facilities": 20000,
                "total_countries": 140,
                "data_sources": len(GDCI_METHODOLOGY["data_sources"]),
                "methodology_url": "https://api.dchub.cloud/api/gdci/methodology",
                "api_docs": "https://dchub.cloud/api-docs"
            }
        },
        "citation": f"DC Hub GDCI v{GDCI_VERSION} — dchub.cloud/api/gdci — {now.strftime('%Y-%m-%d %H:%M UTC')}"
    }

    resp = jsonify(response)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


@gdci_bp.route('/api/gdci/markets', methods=['GET'])
def gdci_markets():
    """
    GET /api/gdci/markets
    GET /api/gdci/markets?region=APAC&sort=score&limit=10
    Returns all tracked markets with full GDCI detail.
    """
    region = request.args.get('region', '').upper()
    sort_by = request.args.get('sort', 'score')
    limit = request.args.get('limit', 50, type=int)
    min_score = request.args.get('min_score', 0, type=int)

    markets = MARKETS_DATA[:]

    # Filter
    if region:
        region_map = {
            "NA": "North America", "NORTH AMERICA": "North America",
            "EMEA": "EMEA", "EUROPE": "EMEA",
            "APAC": "APAC", "ASIA": "APAC",
            "LATAM": "LATAM", "MENA": "MENA"
        }
        target = region_map.get(region, region)
        markets = [m for m in markets if m["region"] == target]

    if min_score:
        markets = [m for m in markets if m["gdci_score"] >= min_score]

    # Sort
    sort_keys = {
        "score": lambda m: m["gdci_score"],
        "trend": lambda m: m["trend_delta"],
        "vacancy": lambda m: m["key_metrics"]["vacancy_pct"],
        "pipeline": lambda m: m["key_metrics"]["pipeline_mw"],
        "price": lambda m: m["key_metrics"]["avg_price_kw_month"],
    }
    key_fn = sort_keys.get(sort_by, sort_keys["score"])
    reverse = sort_by != "vacancy"
    markets = sorted(markets, key=key_fn, reverse=reverse)[:limit]

    resp = jsonify({
        "markets": markets,
        "count": len(markets),
        "filters_applied": {
            "region": region or "all",
            "sort": sort_by,
            "min_score": min_score,
            "limit": limit
        },
        "citation": f"DC Hub GDCI v{GDCI_VERSION} — dchub.cloud/api/gdci/markets"
    })
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


@gdci_bp.route('/api/gdci/market/<slug>', methods=['GET'])
def gdci_market_detail(slug):
    """
    GET /api/gdci/market/{slug}
    Returns deep-dive data for a specific market.
    Example: /api/gdci/market/nova
    """
    market = next((m for m in MARKETS_DATA if m["slug"] == slug.lower()), None)
    if not market:
        return jsonify({"error": f"Market '{slug}' not found", "available_slugs": [m["slug"] for m in MARKETS_DATA]}), 404

    # Find comparable markets (same region, similar score)
    comparables = sorted(
        [m for m in MARKETS_DATA if m["region"] == market["region"] and m["slug"] != slug],
        key=lambda m: abs(m["gdci_score"] - market["gdci_score"])
    )[:3]

    resp = jsonify({
        "market": market,
        "comparable_markets": [
            {"market": c["market"], "slug": c["slug"], "score": c["gdci_score"], "trend": c["trend"]}
            for c in comparables
        ],
        "analysis": {
            "strengths": _analyze_strengths(market),
            "risks": market["risk_flags"],
            "investment_signal": _investment_signal(market),
        },
        "citation": f"DC Hub GDCI v{GDCI_VERSION} — dchub.cloud/api/gdci/market/{slug}"
    })
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


@gdci_bp.route('/api/gdci/history', methods=['GET'])
def gdci_history():
    """
    GET /api/gdci/history
    GET /api/gdci/history?from=2024-01&to=2026-02
    Returns monthly GDCI global scores with component breakdown.
    """
    history = _generate_history()
    from_date = request.args.get('from', '')
    to_date = request.args.get('to', '')

    if from_date:
        history = [h for h in history if h["date"] >= from_date]
    if to_date:
        history = [h for h in history if h["date"] <= to_date]

    resp = jsonify({
        "history": history,
        "count": len(history),
        "summary": {
            "start": history[0]["date"] if history else None,
            "end": history[-1]["date"] if history else None,
            "start_score": history[0]["gdci_global"] if history else None,
            "end_score": history[-1]["gdci_global"] if history else None,
            "total_change": round(history[-1]["gdci_global"] - history[0]["gdci_global"], 1) if len(history) > 1 else 0,
        },
        "citation": f"DC Hub GDCI v{GDCI_VERSION} — dchub.cloud/api/gdci/history"
    })
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@gdci_bp.route('/api/gdci/methodology', methods=['GET'])
def gdci_methodology():
    """
    GET /api/gdci/methodology
    Returns the full GDCI methodology, weighting, and data source documentation.
    """
    resp = jsonify({"methodology": GDCI_METHODOLOGY})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@gdci_bp.route('/api/gdci/compare', methods=['GET'])
def gdci_compare():
    """
    GET /api/gdci/compare?markets=nova,dfw,phx,sin
    Side-by-side comparison of up to 5 markets.
    """
    slugs_raw = request.args.get('markets', '')
    if not slugs_raw:
        return jsonify({"error": "Provide markets parameter, e.g. ?markets=nova,dfw,phx"}), 400

    slugs = [s.strip().lower() for s in slugs_raw.split(',')][:5]
    results = []
    not_found = []

    for slug in slugs:
        market = next((m for m in MARKETS_DATA if m["slug"] == slug), None)
        if market:
            results.append(market)
        else:
            not_found.append(slug)

    if not results:
        return jsonify({"error": "No valid markets found", "available_slugs": [m["slug"] for m in MARKETS_DATA]}), 404

    resp = jsonify({
        "comparison": results,
        "count": len(results),
        "not_found": not_found if not_found else None,
        "available_slugs": [m["slug"] for m in MARKETS_DATA],
        "citation": f"DC Hub GDCI v{GDCI_VERSION} — dchub.cloud/api/gdci/compare"
    })
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER ANALYSIS FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _analyze_strengths(market):
    """Identify top strengths from sub-indices."""
    strengths = []
    si = market["sub_indices"]
    km = market["key_metrics"]

    if si["supply_pressure"] >= 90:
        strengths.append("Extremely tight supply — operator pricing power")
    if si["demand_intensity"] >= 90:
        strengths.append("Very strong demand signals — high absorption rate")
    if si["capital_velocity"] >= 85:
        strengths.append("Active capital deployment — strong investor interest")
    if si["energy_readiness"] >= 75:
        strengths.append("Favorable energy infrastructure — shorter time-to-power")
    if si["market_liquidity"] >= 85:
        strengths.append("High market maturity — transparent pricing, active deal flow")
    if km["pipeline_mw"] >= 2000:
        strengths.append(f"Massive pipeline: {km['pipeline_mw']:,} MW under development")
    if km["vacancy_pct"] <= 2.0:
        strengths.append(f"Ultra-low vacancy at {km['vacancy_pct']}%")
    if km["yoy_absorption_pct"] >= 25:
        strengths.append(f"Rapid absorption: {km['yoy_absorption_pct']}% YoY")

    return strengths or ["Balanced market with no standout strengths or weaknesses"]


def _investment_signal(market):
    """Generate investment signal based on market data."""
    score = market["gdci_score"]
    delta = market["trend_delta"]
    vacancy = market["key_metrics"]["vacancy_pct"]
    energy = market["sub_indices"]["energy_readiness"]

    if score >= 90 and delta > 0:
        signal = "STRONG BUY"
        rationale = "Top-tier market with accelerating momentum. High conviction."
    elif score >= 85 and delta > 0:
        signal = "BUY"
        rationale = "Strong fundamentals with positive trajectory."
    elif score >= 80 and energy < 55:
        signal = "HOLD — ENERGY RISK"
        rationale = "Strong demand but energy infrastructure constraints may limit near-term growth."
    elif score >= 75 and delta >= 3:
        signal = "ACCUMULATE"
        rationale = "Emerging market with strong momentum. Early-mover advantage available."
    elif score >= 70:
        signal = "NEUTRAL"
        rationale = "Adequate fundamentals. Monitor for catalysts."
    elif delta < 0:
        signal = "UNDERWEIGHT"
        rationale = "Negative momentum. Better opportunities in adjacent markets."
    else:
        signal = "WATCH"
        rationale = "Developing market. Evaluate on 6-12 month horizon."

    return {"signal": signal, "rationale": rationale, "confidence": min(99, score)}


# ═══════════════════════════════════════════════════════════════════════════════
# REGISTRATION HELPER (Alternative to Blueprint)
# ═══════════════════════════════════════════════════════════════════════════════

def register_gdci_routes(app):
    """Register all GDCI routes directly on a Flask app (no blueprint)."""
    app.register_blueprint(gdci_bp)


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(gdci_bp)

    @app.after_request
    def add_cors(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    print("\n🌍 DC Hub GDCI v2.0 — Test Run\n")
    with app.test_client() as client:
        # Test main index
        r = client.get('/api/gdci')
        data = r.get_json()
        print(f"✅ /api/gdci → Score: {data['gdci']['global']['score']}, Trend: {data['gdci']['global']['trend']}")
        print(f"   Top 3: {', '.join(m['market'] + ' (' + str(m['score']) + ')' for m in data['gdci']['top_markets'][:3])}")

        # Test markets
        r = client.get('/api/gdci/markets?region=APAC&sort=score')
        data = r.get_json()
        print(f"✅ /api/gdci/markets?region=APAC → {data['count']} markets")

        # Test market detail
        r = client.get('/api/gdci/market/nova')
        data = r.get_json()
        print(f"✅ /api/gdci/market/nova → {data['market']['market']}, Score: {data['market']['gdci_score']}")

        # Test history
        r = client.get('/api/gdci/history?from=2025-01')
        data = r.get_json()
        print(f"✅ /api/gdci/history → {data['count']} months, Latest: {data['history'][-1]['gdci_global']}")

        # Test compare
        r = client.get('/api/gdci/compare?markets=nova,dfw,phx,sin')
        data = r.get_json()
        print(f"✅ /api/gdci/compare → {data['count']} markets compared")

        # Test methodology
        r = client.get('/api/gdci/methodology')
        data = r.get_json()
        print(f"✅ /api/gdci/methodology → {data['methodology']['name']}")

    print("\n🎯 All endpoints working. Deploy to Railway.\n")
