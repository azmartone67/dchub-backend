"""
DC Hub — Market Intelligence API
=================================
Drop this file into your Replit project alongside main.py.
Register the blueprint in main.py with:

    from market_intelligence_api import market_intel_bp
    app.register_blueprint(market_intel_bp)

Provides:
  GET /api/market-intelligence          → All 28 markets summary
  GET /api/market-intelligence/<market> → Single market deep-dive
  GET /api/v2/market-intelligence/<market> → Alias (backward compat)
"""

from flask import Blueprint, jsonify, request
from datetime import datetime

market_intel_bp = Blueprint('market_intelligence', __name__)

# ─────────────────────────────────────────────────────────────
# MARKET DATA — sourced from CBRE H1 2025, JLL H1 2025
# This is the canonical dataset the frontend market-intelligence
# page renders. Update quarterly when new broker reports drop.
# ─────────────────────────────────────────────────────────────

MARKET_DATA = {
    # ── NORTH AMERICA ──────────────────────────────────────
    "Northern Virginia": {
        "region": "North America",
        "vacancy_rate": 1.2,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 215,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 18.5,
        "absorption_mw": 680,
        "absorption_period": "H1 2025",
        "inventory_mw": 4200,
        "under_construction_mw": 5900,
        "pre_leased_pct": 82,
        "num_facilities": 350,
        "top_providers": ["Equinix", "Digital Realty", "QTS", "CloudHQ", "Vantage"],
        "power_cost_kwh": 0.065,
        "fiber_carriers": 45,
        "highlights": [
            "Largest data center market globally",
            "5.9 GW planned through 2028",
            "Dominion Energy capacity constraints easing",
            "Loudoun County remains epicenter"
        ]
    },
    "Dallas-Fort Worth": {
        "region": "North America",
        "vacancy_rate": 2.8,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 165,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 14.2,
        "absorption_mw": 420,
        "absorption_period": "H1 2025",
        "inventory_mw": 2800,
        "under_construction_mw": 3900,
        "pre_leased_pct": 75,
        "num_facilities": 220,
        "top_providers": ["CyrusOne", "Digital Realty", "QTS", "DataBank", "Flexential"],
        "power_cost_kwh": 0.058,
        "fiber_carriers": 35,
        "highlights": [
            "3.9 GW development pipeline",
            "ERCOT grid concerns being addressed",
            "Garland, Allen, and Midlothian expanding",
            "Competitive power pricing advantage"
        ]
    },
    "Phoenix": {
        "region": "North America",
        "vacancy_rate": 3.5,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 155,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 12.8,
        "absorption_mw": 380,
        "absorption_period": "H1 2025",
        "inventory_mw": 1800,
        "under_construction_mw": 4200,
        "pre_leased_pct": 70,
        "num_facilities": 85,
        "top_providers": ["Aligned", "QTS", "Vantage", "Digital Realty", "Stream"],
        "power_cost_kwh": 0.072,
        "fiber_carriers": 22,
        "highlights": [
            "4.2 GW planned — fastest growing US market",
            "Mesa, Goodyear, and Avondale leading expansion",
            "APS and SRP increasing grid capacity",
            "Water sustainability a key consideration"
        ]
    },
    "Chicago": {
        "region": "North America",
        "vacancy_rate": 2.1,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 175,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 10.5,
        "absorption_mw": 290,
        "absorption_period": "H1 2025",
        "inventory_mw": 2200,
        "under_construction_mw": 1800,
        "pre_leased_pct": 78,
        "num_facilities": 165,
        "top_providers": ["Equinix", "Digital Realty", "CyrusOne", "QTS", "DataBank"],
        "power_cost_kwh": 0.082,
        "fiber_carriers": 40,
        "highlights": [
            "Elk Grove Village and Franklin Park remain core",
            "Strong interconnection hub",
            "ComEd power reliability high",
            "Financial services anchor tenant base"
        ]
    },
    "Silicon Valley": {
        "region": "North America",
        "vacancy_rate": 2.8,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 225,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 8.5,
        "absorption_mw": 180,
        "absorption_period": "H1 2025",
        "inventory_mw": 800,
        "under_construction_mw": 650,
        "pre_leased_pct": 85,
        "num_facilities": 150,
        "top_providers": ["Equinix", "CoreSite", "Digital Realty", "Vantage", "CyrusOne"],
        "power_cost_kwh": 0.145,
        "fiber_carriers": 50,
        "highlights": [
            "Highest pricing in North America",
            "Land-constrained market",
            "Santa Clara remains primary hub",
            "AI/ML workloads driving demand"
        ]
    },
    "Atlanta": {
        "region": "North America",
        "vacancy_rate": 3.2,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 140,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 9.8,
        "absorption_mw": 210,
        "absorption_period": "H1 2025",
        "inventory_mw": 1400,
        "under_construction_mw": 1600,
        "pre_leased_pct": 65,
        "num_facilities": 95,
        "top_providers": ["QTS", "Switch", "Digital Realty", "Flexential", "DataBank"],
        "power_cost_kwh": 0.068,
        "fiber_carriers": 28,
        "highlights": [
            "Douglas County mega-campus developments",
            "Georgia Power expanding capacity",
            "Strong Southeast connectivity hub",
            "Favorable tax incentives"
        ]
    },
    "Seattle": {
        "region": "North America",
        "vacancy_rate": 2.5,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 185,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 11.2,
        "absorption_mw": 250,
        "absorption_period": "H1 2025",
        "inventory_mw": 1100,
        "under_construction_mw": 1400,
        "pre_leased_pct": 80,
        "num_facilities": 75,
        "top_providers": ["Sabey", "Equinix", "Digital Realty", "CyrusOne", "Vantage"],
        "power_cost_kwh": 0.048,
        "fiber_carriers": 30,
        "highlights": [
            "Quincy and Moses Lake for hyperscale",
            "Cheapest hydro power in US",
            "Microsoft and cloud anchor tenants",
            "Tukwila/Renton for colo"
        ]
    },
    "Denver": {
        "region": "North America",
        "vacancy_rate": 3.8,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 145,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 8.2,
        "absorption_mw": 160,
        "absorption_period": "H1 2025",
        "inventory_mw": 900,
        "under_construction_mw": 850,
        "pre_leased_pct": 60,
        "num_facilities": 60,
        "top_providers": ["Flexential", "Aligned", "CoreSite", "ViaWest", "DataBank"],
        "power_cost_kwh": 0.075,
        "fiber_carriers": 22,
        "highlights": [
            "Aurora and Englewood primary clusters",
            "Growing AI/ML presence",
            "Renewable energy availability",
            "Moderate seismic and weather risk"
        ]
    },
    "Austin": {
        "region": "North America",
        "vacancy_rate": 4.2,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 135,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 15.5,
        "absorption_mw": 140,
        "absorption_period": "H1 2025",
        "inventory_mw": 650,
        "under_construction_mw": 1100,
        "pre_leased_pct": 55,
        "num_facilities": 45,
        "top_providers": ["Digital Realty", "DataBank", "Stream", "Flexential", "Skybox"],
        "power_cost_kwh": 0.062,
        "fiber_carriers": 18,
        "highlights": [
            "Fastest-growing Texas secondary market",
            "Samsung, Tesla driving enterprise demand",
            "Hutto and Pflugerville development areas",
            "ERCOT grid expansion underway"
        ]
    },
    "Los Angeles": {
        "region": "North America",
        "vacancy_rate": 3.0,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 195,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 7.5,
        "absorption_mw": 150,
        "absorption_period": "H1 2025",
        "inventory_mw": 750,
        "under_construction_mw": 500,
        "pre_leased_pct": 72,
        "num_facilities": 110,
        "top_providers": ["CoreSite", "Equinix", "Digital Realty", "DataPacket", "US Signal"],
        "power_cost_kwh": 0.155,
        "fiber_carriers": 35,
        "highlights": [
            "Content and media anchor tenant base",
            "One Wilshire premier interconnection hub",
            "El Segundo emerging market",
            "High power costs limit hyperscale"
        ]
    },
    "Kansas City": {
        "region": "North America",
        "vacancy_rate": 5.1,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 115,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 6.5,
        "absorption_mw": 90,
        "absorption_period": "H1 2025",
        "inventory_mw": 400,
        "under_construction_mw": 350,
        "pre_leased_pct": 50,
        "num_facilities": 35,
        "top_providers": ["Netrality", "QTS", "DataBank", "Flexential", "US Signal"],
        "power_cost_kwh": 0.065,
        "fiber_carriers": 20,
        "highlights": [
            "1102 Grand premier interconnection facility",
            "Meta hyperscale campus expanding",
            "Central US latency advantage",
            "Evergy power competitive pricing"
        ]
    },
    "Nashville": {
        "region": "North America",
        "vacancy_rate": 4.5,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 130,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 11.0,
        "absorption_mw": 80,
        "absorption_period": "H1 2025",
        "inventory_mw": 350,
        "under_construction_mw": 600,
        "pre_leased_pct": 58,
        "num_facilities": 30,
        "top_providers": ["NashvilleIX", "DataBank", "QTS", "Flexential", "365 Data Centers"],
        "power_cost_kwh": 0.072,
        "fiber_carriers": 18,
        "highlights": [
            "Emerging Southeast market",
            "TVA power reliability",
            "Clarksville mega-campus planned",
            "Healthcare IT driving demand"
        ]
    },
    "Columbus": {
        "region": "North America",
        "vacancy_rate": 2.8,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 125,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 22.5,
        "absorption_mw": 200,
        "absorption_period": "H1 2025",
        "inventory_mw": 600,
        "under_construction_mw": 2200,
        "pre_leased_pct": 72,
        "num_facilities": 40,
        "top_providers": ["QTS", "Aligned", "Digital Realty", "Cologix", "DataBank"],
        "power_cost_kwh": 0.068,
        "fiber_carriers": 16,
        "highlights": [
            "AWS, Google, Meta mega-campuses",
            "New Albany and Licking County boom",
            "AEP Ohio grid upgrades underway",
            "Fastest YoY growth in US"
        ]
    },
    "Houston": {
        "region": "North America",
        "vacancy_rate": 4.8,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 130,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 8.0,
        "absorption_mw": 120,
        "absorption_period": "H1 2025",
        "inventory_mw": 550,
        "under_construction_mw": 700,
        "pre_leased_pct": 55,
        "num_facilities": 55,
        "top_providers": ["CyrusOne", "Digital Realty", "DataBank", "Skybox", "DC Blox"],
        "power_cost_kwh": 0.058,
        "fiber_carriers": 25,
        "highlights": [
            "Energy sector IT demand strong",
            "Subsea cable landing proximity",
            "ERCOT grid exposure",
            "Affordable land and power"
        ]
    },
    "Portland": {
        "region": "North America",
        "vacancy_rate": 3.5,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 140,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 10.0,
        "absorption_mw": 180,
        "absorption_period": "H1 2025",
        "inventory_mw": 700,
        "under_construction_mw": 900,
        "pre_leased_pct": 68,
        "num_facilities": 40,
        "top_providers": ["Vantage", "Digital Realty", "Flexential", "QTS", "H5"],
        "power_cost_kwh": 0.045,
        "fiber_carriers": 20,
        "highlights": [
            "Hillsboro primary hyperscale cluster",
            "PGE and BPA cheap hydro power",
            "Google and Meta major presence",
            "Tax incentives through 2027"
        ]
    },
    "Las Vegas": {
        "region": "North America",
        "vacancy_rate": 1.8,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 150,
        "asking_rate_unit": "$/kW/mo",
        "yoy_price_change": 15.3,
        "absorption_mw": 200,
        "absorption_period": "H1 2025",
        "inventory_mw": 1000,
        "under_construction_mw": 800,
        "pre_leased_pct": 78,
        "num_facilities": 30,
        "top_providers": ["Switch", "Digital Realty", "Aligned", "LasVegas.net", "VegasNAP"],
        "power_cost_kwh": 0.055,
        "fiber_carriers": 18,
        "highlights": [
            "Switch SuperNAP campus dominates",
            "NV Energy favorable rates",
            "Renewable solar potential",
            "Tightest vacancy in secondary markets"
        ]
    },
    "Toronto": {
        "region": "North America",
        "vacancy_rate": 3.2,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 170,
        "asking_rate_unit": "$/kW/mo (CAD)",
        "yoy_price_change": 9.5,
        "absorption_mw": 120,
        "absorption_period": "H1 2025",
        "inventory_mw": 650,
        "under_construction_mw": 500,
        "pre_leased_pct": 65,
        "num_facilities": 55,
        "top_providers": ["Equinix", "Digital Realty", "Cologix", "eStruxture", "Rogers"],
        "power_cost_kwh": 0.085,
        "fiber_carriers": 22,
        "highlights": [
            "Largest Canadian market",
            "Markham and Vaughan primary clusters",
            "Data sovereignty driving local demand",
            "151 Front St premier interconnection"
        ]
    },
    "Montreal": {
        "region": "North America",
        "vacancy_rate": 4.0,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 120,
        "asking_rate_unit": "$/kW/mo (CAD)",
        "yoy_price_change": 12.0,
        "absorption_mw": 90,
        "absorption_period": "H1 2025",
        "inventory_mw": 450,
        "under_construction_mw": 600,
        "pre_leased_pct": 60,
        "num_facilities": 35,
        "top_providers": ["Cologix", "QScale", "eStruxture", "Vantage", "OVHcloud"],
        "power_cost_kwh": 0.038,
        "fiber_carriers": 15,
        "highlights": [
            "Cheapest power in North America",
            "Hydro-Québec renewable base",
            "AI/ML training workloads growing",
            "Beauharnois hyperscale corridor"
        ]
    },
    # ── EMEA ───────────────────────────────────────────────
    "London": {
        "region": "EMEA",
        "vacancy_rate": 2.5,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 190,
        "asking_rate_unit": "£/kW/mo",
        "yoy_price_change": 8.0,
        "absorption_mw": 250,
        "absorption_period": "H1 2025",
        "inventory_mw": 1500,
        "under_construction_mw": 1200,
        "pre_leased_pct": 75,
        "num_facilities": 130,
        "top_providers": ["Equinix", "Digital Realty", "Virtus", "Vantage", "NTT"],
        "power_cost_kwh": 0.185,
        "fiber_carriers": 50,
        "highlights": [
            "Slough/Langley traditional hub",
            "West London moratorium pressuring supply",
            "High energy costs limiting margins",
            "LINX and LONAP interconnection"
        ]
    },
    "Frankfurt": {
        "region": "EMEA",
        "vacancy_rate": 3.1,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 175,
        "asking_rate_unit": "€/kW/mo",
        "yoy_price_change": 7.5,
        "absorption_mw": 200,
        "absorption_period": "H1 2025",
        "inventory_mw": 1100,
        "under_construction_mw": 800,
        "pre_leased_pct": 70,
        "num_facilities": 85,
        "top_providers": ["Equinix", "Digital Realty", "NTT", "Interxion", "Mainova"],
        "power_cost_kwh": 0.195,
        "fiber_carriers": 45,
        "highlights": [
            "DE-CIX world's largest internet exchange",
            "Financial services anchor tenants",
            "Hanau and Offenbach expansion",
            "Grid capacity concerns emerging"
        ]
    },
    "Amsterdam": {
        "region": "EMEA",
        "vacancy_rate": 4.2,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 160,
        "asking_rate_unit": "€/kW/mo",
        "yoy_price_change": 5.5,
        "absorption_mw": 130,
        "absorption_period": "H1 2025",
        "inventory_mw": 800,
        "under_construction_mw": 400,
        "pre_leased_pct": 68,
        "num_facilities": 70,
        "top_providers": ["Equinix", "Digital Realty", "NorthC", "Iron Mountain", "Global Switch"],
        "power_cost_kwh": 0.165,
        "fiber_carriers": 40,
        "highlights": [
            "AMS-IX major peering hub",
            "Municipal moratorium easing",
            "Haarlemmermeer and Schiphol corridor",
            "Sustainability mandates shaping builds"
        ]
    },
    "Dublin": {
        "region": "EMEA",
        "vacancy_rate": 3.8,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 155,
        "asking_rate_unit": "€/kW/mo",
        "yoy_price_change": 6.0,
        "absorption_mw": 100,
        "absorption_period": "H1 2025",
        "inventory_mw": 600,
        "under_construction_mw": 350,
        "pre_leased_pct": 72,
        "num_facilities": 55,
        "top_providers": ["Equinix", "Digital Realty", "Amazon", "Microsoft", "Echelon"],
        "power_cost_kwh": 0.175,
        "fiber_carriers": 20,
        "highlights": [
            "Grid connection moratorium lifted",
            "EirGrid investment plan",
            "FLAP+D market fundamentals",
            "Hyperscaler campus concentration"
        ]
    },
    "Paris": {
        "region": "EMEA",
        "vacancy_rate": 3.5,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 165,
        "asking_rate_unit": "€/kW/mo",
        "yoy_price_change": 7.0,
        "absorption_mw": 140,
        "absorption_period": "H1 2025",
        "inventory_mw": 700,
        "under_construction_mw": 550,
        "pre_leased_pct": 65,
        "num_facilities": 60,
        "top_providers": ["Equinix", "Digital Realty", "Data4", "Scaleway", "Interxion"],
        "power_cost_kwh": 0.145,
        "fiber_carriers": 30,
        "highlights": [
            "Nuclear baseload power advantage",
            "Île-de-France primary region",
            "France-IX peering",
            "2024 Olympics drove infrastructure investment"
        ]
    },
    # ── APAC ───────────────────────────────────────────────
    "Singapore": {
        "region": "APAC",
        "vacancy_rate": 1.5,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 210,
        "asking_rate_unit": "$/kW/mo (SGD)",
        "yoy_price_change": 12.0,
        "absorption_mw": 100,
        "absorption_period": "H1 2025",
        "inventory_mw": 850,
        "under_construction_mw": 300,
        "pre_leased_pct": 90,
        "num_facilities": 65,
        "top_providers": ["Equinix", "Digital Realty", "ST Telemedia", "AirTrunk", "Keppel"],
        "power_cost_kwh": 0.155,
        "fiber_carriers": 35,
        "highlights": [
            "Moratorium partially lifted 2024",
            "Tightest APAC market",
            "Jurong and Tuas expansion zones",
            "Sustainability requirements (PUE < 1.3)"
        ]
    },
    "Tokyo": {
        "region": "APAC",
        "vacancy_rate": 2.8,
        "vacancy_source": "CBRE H1 2025",
        "avg_asking_rate": 200,
        "asking_rate_unit": "$/kW/mo (JPY equiv)",
        "yoy_price_change": 9.5,
        "absorption_mw": 180,
        "absorption_period": "H1 2025",
        "inventory_mw": 1200,
        "under_construction_mw": 900,
        "pre_leased_pct": 78,
        "num_facilities": 100,
        "top_providers": ["Equinix", "NTT", "Digital Realty", "KDDI", "MCDigital Realty"],
        "power_cost_kwh": 0.165,
        "fiber_carriers": 30,
        "highlights": [
            "Largest APAC market by revenue",
            "Inzai and Chiba hyperscale corridor",
            "TEPCO power constraints in metro",
            "Subsea cable hub for trans-Pacific"
        ]
    },
    "Sydney": {
        "region": "APAC",
        "vacancy_rate": 3.5,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 175,
        "asking_rate_unit": "$/kW/mo (AUD)",
        "yoy_price_change": 10.5,
        "absorption_mw": 120,
        "absorption_period": "H1 2025",
        "inventory_mw": 700,
        "under_construction_mw": 600,
        "pre_leased_pct": 70,
        "num_facilities": 50,
        "top_providers": ["Equinix", "AirTrunk", "Digital Realty", "NextDC", "Macquarie"],
        "power_cost_kwh": 0.125,
        "fiber_carriers": 20,
        "highlights": [
            "Western Sydney growth corridor",
            "Blackstone/AirTrunk $24B deal impact",
            "Eastern Creek and Silverwater hubs",
            "Government data sovereignty mandates"
        ]
    },
    "Hong Kong": {
        "region": "APAC",
        "vacancy_rate": 4.0,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 195,
        "asking_rate_unit": "$/kW/mo (HKD equiv)",
        "yoy_price_change": 5.0,
        "absorption_mw": 80,
        "absorption_period": "H1 2025",
        "inventory_mw": 550,
        "under_construction_mw": 300,
        "pre_leased_pct": 65,
        "num_facilities": 45,
        "top_providers": ["Equinix", "Digital Realty", "SUNeVision", "NTT", "AirTrunk"],
        "power_cost_kwh": 0.145,
        "fiber_carriers": 25,
        "highlights": [
            "MEGA Plus campus development",
            "Tseung Kwan O industrial zone",
            "Gateway to mainland China",
            "Land scarcity constraining supply"
        ]
    },
    "Mumbai": {
        "region": "APAC",
        "vacancy_rate": 5.5,
        "vacancy_source": "JLL H1 2025",
        "avg_asking_rate": 95,
        "asking_rate_unit": "$/kW/mo (INR equiv)",
        "yoy_price_change": 18.0,
        "absorption_mw": 150,
        "absorption_period": "H1 2025",
        "inventory_mw": 500,
        "under_construction_mw": 800,
        "pre_leased_pct": 55,
        "num_facilities": 40,
        "top_providers": ["AdaniConneX", "Nxtra (Airtel)", "STT GDC", "Yotta", "CtrlS"],
        "power_cost_kwh": 0.085,
        "fiber_carriers": 15,
        "highlights": [
            "Navi Mumbai and Panvel expansion zones",
            "Fastest growing APAC market",
            "Data localization regulations driving build",
            "Adani entry transforming scale expectations"
        ]
    },
}


def _market_summary(name, data):
    """Build a compact summary dict for list views."""
    return {
        "market": name,
        "region": data["region"],
        "vacancy_rate": data["vacancy_rate"],
        "vacancy_source": data["vacancy_source"],
        "avg_asking_rate": data["avg_asking_rate"],
        "asking_rate_unit": data["asking_rate_unit"],
        "yoy_price_change": data["yoy_price_change"],
        "absorption_mw": data["absorption_mw"],
        "inventory_mw": data["inventory_mw"],
        "under_construction_mw": data["under_construction_mw"],
        "pre_leased_pct": data["pre_leased_pct"],
        "num_facilities": data["num_facilities"],
        "top_providers": data.get("top_providers", [])[:3],
    }


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

# AUTO-REPAIR: duplicate route '/api/market-intelligence' also in main.py:14927 — review and remove one
@market_intel_bp.route('/api/market-intelligence', methods=['GET'])
def get_all_market_intelligence():
    """
    GET /api/market-intelligence
    Returns summary data for all 28 markets.
    Optional query params:
      ?region=EMEA          — filter by region
      ?sort=vacancy_rate    — sort field
      %sorder=asc            — asc or desc
    """
    region = request.args.get('region', '').strip()
    sort_by = request.args.get('sort', 'vacancy_rate').strip()
    order = request.args.get('order', 'asc').strip()

    markets = []
    for name, data in MARKET_DATA.items():
        if region and data["region"].lower() != region.lower():
            continue
        markets.append(_market_summary(name, data))

    # Sort
    valid_sort_fields = [
        'vacancy_rate', 'avg_asking_rate', 'yoy_price_change',
        'absorption_mw', 'inventory_mw', 'under_construction_mw',
        'num_facilities', 'market', 'pre_leased_pct'
    ]
    if sort_by in valid_sort_fields:
        reverse = (order.lower() == 'desc')
        markets.sort(key=lambda m: m.get(sort_by, 0) or 0, reverse=reverse)

    # Compute aggregate stats
    total_inventory = sum(d["inventory_mw"] for d in MARKET_DATA.values())
    total_construction = sum(d["under_construction_mw"] for d in MARKET_DATA.values())
    total_absorption = sum(d["absorption_mw"] for d in MARKET_DATA.values())
    avg_vacancy = round(sum(d["vacancy_rate"] for d in MARKET_DATA.values()) / len(MARKET_DATA), 1)
    total_facilities = sum(d["num_facilities"] for d in MARKET_DATA.values())

    tightest = min(MARKET_DATA.items(), key=lambda x: x[1]["vacancy_rate"])
    most_expensive = max(MARKET_DATA.items(), key=lambda x: x[1]["avg_asking_rate"])
    fastest_growing = max(MARKET_DATA.items(), key=lambda x: x[1]["yoy_price_change"])
    biggest_pipeline = max(MARKET_DATA.items(), key=lambda x: x[1]["under_construction_mw"])

    return jsonify({
        "success": True,
        "count": len(markets),
        "markets": markets,
        "summary": {
            "total_markets": len(MARKET_DATA),
            "total_inventory_mw": total_inventory,
            "total_under_construction_mw": total_construction,
            "total_absorption_mw": total_absorption,
            "avg_vacancy_rate": avg_vacancy,
            "total_facilities": total_facilities,
            "tightest_market": tightest[0],
            "tightest_vacancy": tightest[1]["vacancy_rate"],
            "most_expensive_market": most_expensive[0],
            "highest_asking_rate": most_expensive[1]["avg_asking_rate"],
            "fastest_growing_market": fastest_growing[0],
            "fastest_yoy_change": fastest_growing[1]["yoy_price_change"],
            "biggest_pipeline_market": biggest_pipeline[0],
            "biggest_pipeline_mw": biggest_pipeline[1]["under_construction_mw"],
        },
        "sources": ["CBRE H1 2025", "JLL H1 2025"],
        "last_updated": "2025-H1",
        "timestamp": datetime.utcnow().isoformat(),
    })


@market_intel_bp.route('/api/market-intelligence/<path:market_name>', methods=['GET'])
@market_intel_bp.route('/api/v2/market-intelligence/<path:market_name>', methods=['GET'])
def get_single_market_intelligence(market_name):
    """
    GET /api/market-intelligence/<market>
    GET /api/v2/market-intelligence/<market>  (backward compat)
    Returns full detail for a single market.
    Accepts URL-encoded names or slugs:
      /api/market-intelligence/phoenix
      /api/market-intelligence/Northern%20Virginia
      /api/market-intelligence/dallas-fort-worth
    """
    # Normalize: lowercase, replace hyphens with spaces
    search = market_name.lower().replace('-', ' ').strip()

    matched_name = None
    matched_data = None
    for name, data in MARKET_DATA.items():
        if name.lower() == search:
            matched_name = name
            matched_data = data
            break

    # Fuzzy fallback: partial match
    if not matched_data:
        for name, data in MARKET_DATA.items():
            if search in name.lower() or name.lower() in search:
                matched_name = name
                matched_data = data
                break

    if not matched_data:
        return jsonify({
            "success": False,
            "error": f"Market '{market_name}' not found",
            "available_markets": sorted(MARKET_DATA.keys()),
        }), 404

    result = dict(matched_data)
    result["market"] = matched_name

    return jsonify({
        "success": True,
        "data": result,
        "sources": ["CBRE H1 2025", "JLL H1 2025"],
        "last_updated": "2025-H1",
        "timestamp": datetime.utcnow().isoformat(),
    })
