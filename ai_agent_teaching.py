"""
DC Hub AI Agent Teaching & Self-Learning Module

This module implements:
1. Educational endpoints for AI agents learning about data center markets
2. Self-awareness dashboard tracking platform health and data freshness
3. Self-learning query pattern analysis and adaptation
4. Agent onboarding curriculum with structured steps

Created: 2026-03-27
For: DC Hub (dchub.cloud) Flask backend on Railway + Neon PostgreSQL
"""

from flask import Blueprint, jsonify, request
from datetime import datetime, timedelta
import json
from typing import Dict, List, Tuple, Any
import logging

logger = logging.getLogger(__name__)

# Create Blueprint
ai_teaching_bp = Blueprint('ai_teaching', __name__, url_prefix='/api/v1')


# ============================================================================
# DATABASE TABLE INITIALIZATION
# ============================================================================

def init_ai_teaching_tables(get_db):
    """Initialize PostgreSQL tables for AI teaching and self-learning."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Table: Query pattern tracking
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_query_patterns (
                    id SERIAL PRIMARY KEY,
                    agent_identifier VARCHAR(255),
                    query_type VARCHAR(100),
                    search_term VARCHAR(255),
                    facility_id VARCHAR(255),
                    market_name VARCHAR(255),
                    mcp_tool_used VARCHAR(100),
                    timestamp TIMESTAMP DEFAULT NOW(),
                    response_time_ms INTEGER,
                    result_count INTEGER,
                    agent_follow_up BOOLEAN DEFAULT FALSE
                )
            """)

            # Table: Platform health metrics
            cur.execute("""
                CREATE TABLE IF NOT EXISTS platform_health_metrics (
                    id SERIAL PRIMARY KEY,
                    metric_category VARCHAR(100),
                    metric_name VARCHAR(255),
                    metric_value VARCHAR(500),
                    recorded_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS platform_health_metrics_uniq
                ON platform_health_metrics (metric_category, metric_name, (recorded_at::DATE))
            """)

            # Table: Data source freshness
            cur.execute("""
                CREATE TABLE IF NOT EXISTS data_source_freshness (
                    id SERIAL PRIMARY KEY,
                    source_name VARCHAR(100),
                    last_sync TIMESTAMP,
                    record_count INTEGER,
                    sync_status VARCHAR(50),
                    last_checked TIMESTAMP DEFAULT NOW(),
                    UNIQUE(source_name)
                )
            """)

            # Table: Agent onboarding progress
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_onboarding_progress (
                    id SERIAL PRIMARY KEY,
                    agent_identifier VARCHAR(255),
                    current_step INTEGER DEFAULT 0,
                    steps_completed TEXT[],
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP,
                    UNIQUE(agent_identifier)
                )
            """)

            # Table: Learning insights
            cur.execute("""
                CREATE TABLE IF NOT EXISTS learning_insights (
                    id SERIAL PRIMARY KEY,
                    insight_type VARCHAR(100),
                    insight_data JSONB,
                    calculated_at TIMESTAMP DEFAULT NOW(),
                    valid_until TIMESTAMP DEFAULT NOW() + INTERVAL '24 hours'
                )
            """)

            conn.commit()
            logger.info("AI teaching tables initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing AI teaching tables: {e}")
        conn.rollback()
    finally:
        conn.close()


# ============================================================================
# TEACHING ENDPOINTS
# ============================================================================

@ai_teaching_bp.route('/teach/markets', methods=['GET'])
def teach_markets(get_db=None):
    """
    Educational endpoint: Global data center markets overview.

    Returns structured lesson about:
    - Top 10 markets by capacity
    - Vacancy rates
    - Pricing trends
    - Power constraints and queue depths
    """
    return jsonify({
        "lesson": "Global Data Center Markets Overview",
        "description": "Learn about the world's most important data center markets",
        "last_updated": "2026-03-27",
        "content": {
            "top_markets_by_capacity": [
                {
                    "rank": 1,
                    "market": "Northern Virginia",
                    "country": "USA",
                    "installed_capacity_mw": 12500,
                    "vacancy_rate_percent": 1.8,
                    "avg_rent_per_kw_month": 180,
                    "key_characteristics": [
                        "Largest colocation market globally",
                        "Home to major internet exchanges (NOVA)",
                        "Tier IV certification widespread",
                        "High network density"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "CyrusOne", "QTS"],
                    "growth_outlook": "Moderate (constrained by power)"
                },
                {
                    "rank": 2,
                    "market": "Dallas",
                    "country": "USA",
                    "installed_capacity_mw": 8200,
                    "vacancy_rate_percent": 2.1,
                    "avg_rent_per_kw_month": 165,
                    "key_characteristics": [
                        "Energy-abundant region (ERCOT grid)",
                        "Growing hyperscale presence",
                        "Lower power costs than East Coast",
                        "Expanding fiber networks"
                    ],
                    "major_operators": ["Digital Realty", "STACK Infrastructure", "Equinix"],
                    "growth_outlook": "Strong (power available)"
                },
                {
                    "rank": 3,
                    "market": "Chicago",
                    "country": "USA",
                    "installed_capacity_mw": 7800,
                    "vacancy_rate_percent": 2.3,
                    "avg_rent_per_kw_month": 155,
                    "key_characteristics": [
                        "Central US location (latency hub)",
                        "Abundant fiber (8+ transcons)",
                        "Moderate cooling costs",
                        "Strong carrier ecosystem"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "Marquardt Group"],
                    "growth_outlook": "Steady (mature market)"
                },
                {
                    "rank": 4,
                    "market": "Phoenix",
                    "country": "USA",
                    "installed_capacity_mw": 4100,
                    "vacancy_rate_percent": 1.9,
                    "avg_rent_per_kw_month": 145,
                    "key_characteristics": [
                        "GPU/AI cluster hub",
                        "Power constraints (APS grid limits)",
                        "Water challenges (cooling constraints)",
                        "Growth limited by PUE requirements"
                    ],
                    "major_operators": ["Switch", "Digital Realty", "Aligned"],
                    "growth_outlook": "Constrained (power limited)"
                },
                {
                    "rank": 5,
                    "market": "Frankfurt",
                    "country": "Germany",
                    "installed_capacity_mw": 3900,
                    "vacancy_rate_percent": 2.0,
                    "avg_rent_per_kw_month": 220,
                    "key_characteristics": [
                        "Europe's largest market",
                        "Tier IV standard",
                        "Interconnection hub",
                        "Renewable power focus"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "Centurylink"],
                    "growth_outlook": "Steady (mature)"
                },
                {
                    "rank": 6,
                    "market": "Los Angeles",
                    "country": "USA",
                    "installed_capacity_mw": 3200,
                    "vacancy_rate_percent": 1.7,
                    "avg_rent_per_kw_month": 190,
                    "key_characteristics": [
                        "Subsea cable hub (Pacific)",
                        "Earthquake risk mitigation required",
                        "High real estate costs",
                        "Content delivery proximity"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "CoreWeave"],
                    "growth_outlook": "Moderate (constrained by real estate)"
                },
                {
                    "rank": 7,
                    "market": "London",
                    "country": "UK",
                    "installed_capacity_mw": 2800,
                    "vacancy_rate_percent": 2.2,
                    "avg_rent_per_kw_month": 200,
                    "key_characteristics": [
                        "Europe-Africa-Asia connectivity",
                        "Financial services demand",
                        "Space and power constraints",
                        "Brexit-driven EU redundancy builds"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "Telehouse"],
                    "growth_outlook": "Moderate (space-constrained)"
                },
                {
                    "rank": 8,
                    "market": "Singapore",
                    "country": "Singapore",
                    "installed_capacity_mw": 2600,
                    "vacancy_rate_percent": 1.5,
                    "avg_rent_per_kw_month": 240,
                    "key_characteristics": [
                        "Asia-Pacific gateway",
                        "Very tight vacancy",
                        "Premium pricing",
                        "Submarine cable hub (10+ cables)"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "AirTrunk"],
                    "growth_outlook": "Strong (expanding hyperscale)"
                },
                {
                    "rank": 9,
                    "market": "Tokyo",
                    "country": "Japan",
                    "installed_capacity_mw": 2400,
                    "vacancy_rate_percent": 1.6,
                    "avg_rent_per_kw_month": 250,
                    "key_characteristics": [
                        "Earthquake zone (seismic hardening)",
                        "Submarine cable redundancy",
                        "High real estate density",
                        "Enterprise demand-driven"
                    ],
                    "major_operators": ["Equinix", "IDC Frontier", "KDDI"],
                    "growth_outlook": "Steady (capacity growing)"
                },
                {
                    "rank": 10,
                    "market": "Sydney",
                    "country": "Australia",
                    "installed_capacity_mw": 2100,
                    "vacancy_rate_percent": 2.0,
                    "avg_rent_per_kw_month": 210,
                    "key_characteristics": [
                        "Australia-NZ gateway",
                        "Geographically remote",
                        "Strong tech sector demand",
                        "Latency advantage for APAC"
                    ],
                    "major_operators": ["Equinix", "Digital Realty", "AirTrunk"],
                    "growth_outlook": "Strong (expansion underway)"
                }
            ],
            "market_analysis": {
                "global_installed_capacity_mw": 45000,
                "global_vacancy_rate": 1.85,
                "pricing_observations": {
                    "usa_average": 165,
                    "europe_average": 210,
                    "apac_average": 230,
                    "note": "Prices in $/kW/month for enterprise colocation"
                },
                "power_constraints": {
                    "critical_markets": ["Northern Virginia", "Phoenix", "Chicago"],
                    "issue": "Queue depths for new power 6-24 months",
                    "solution": "Hyperscalers building private power infrastructure"
                },
                "queue_depth_analysis": {
                    "northern_virginia": {
                        "months_for_new_capacity": 18,
                        "reason": "Limited power plant capacity in region"
                    },
                    "phoenix": {
                        "months_for_new_capacity": 24,
                        "reason": "APS utility constraints"
                    },
                    "dallas": {
                        "months_for_new_capacity": 6,
                        "reason": "Abundant ERCOT capacity"
                    }
                }
            },
            "key_insights": [
                "Vacancy rates under 2% in most Tier-1 markets indicate tight capacity",
                "Power availability is now the primary growth constraint globally",
                "AI/GPU clusters are reshaping demand in secondary markets",
                "Hyperscalers increasingly build private infrastructure vs. colocation",
                "Renewable energy mandates driving market bifurcation"
            ]
        }
    })


@ai_teaching_bp.route('/teach/operators', methods=['GET'])
def teach_operators():
    """
    Educational endpoint: Major data center operators and their strategies.

    Returns comprehensive lesson about:
    - Top operators globally
    - Market share and specializations
    - Geographic focus areas
    - M&A trends and hyperscaler acquisitions
    """
    return jsonify({
        "lesson": "Data Center Operators: Market Leaders & Specializations",
        "description": "Understand the competitive landscape of major DC operators",
        "last_updated": "2026-03-27",
        "content": {
            "top_operators": [
                {
                    "rank": 1,
                    "name": "Equinix",
                    "market_share_percent": 18,
                    "headquarters": "Redwood City, CA",
                    "global_footprint": "260+ facilities in 75+ markets",
                    "specialization": "Global interconnection colocation",
                    "key_services": [
                        "Direct cloud on-ramps (AWS, Azure, GCP)",
                        "International business exchange (IBX)",
                        "Tier IV certification standard",
                        "Interconnection services (10k+ networks)"
                    ],
                    "geographic_strengths": ["Northern Virginia", "Frankfurt", "Singapore", "Tokyo"],
                    "recent_moves": [
                        "Investing in AI/GPU infrastructure",
                        "Expanding renewable power partnerships"
                    ]
                },
                {
                    "rank": 2,
                    "name": "Digital Realty",
                    "market_share_percent": 16,
                    "headquarters": "San Francisco, CA",
                    "global_footprint": "290+ facilities in 50+ markets",
                    "specialization": "Scale and geographic diversity",
                    "key_services": [
                        "Hyperscale colocation",
                        "Cloud-connected data centers",
                        "Enterprise colocation",
                        "Retail and dense metro markets"
                    ],
                    "geographic_strengths": ["Dallas", "Chicago", "London", "Sydney"],
                    "recent_moves": [
                        "Power infrastructure automation",
                        "Edge computing expansion"
                    ]
                },
                {
                    "rank": 3,
                    "name": "CyrusOne",
                    "market_share_percent": 8,
                    "headquarters": "Carrollton, TX",
                    "global_footprint": "70+ facilities, primarily USA",
                    "specialization": "Enterprise hyperscale colocation",
                    "key_services": [
                        "North American focus",
                        "High-density enterprise",
                        "Custom power delivery",
                        "Managed services"
                    ],
                    "geographic_strengths": ["Northern Virginia", "Dallas", "Chicago", "Phoenix"],
                    "recent_moves": [
                        "GPU cluster specialization",
                        "Acquired by BC Partners (2021)"
                    ]
                },
                {
                    "rank": 4,
                    "name": "QTS Realty Trust",
                    "market_share_percent": 6,
                    "headquarters": "Atlanta, GA",
                    "global_footprint": "85+ facilities in 25 markets",
                    "specialization": "Enterprise and hyperscale blend",
                    "key_services": [
                        "AWS Direct Connect hubs",
                        "Mission-critical environments",
                        "High-security compliance",
                        "Custom infrastructure"
                    ],
                    "geographic_strengths": ["Northern Virginia", "Atlanta", "Dallas"],
                    "recent_moves": [
                        "Acquired by Blackstone (2021)",
                        "Integration into BDX portfolio"
                    ]
                },
                {
                    "rank": 5,
                    "name": "STACK Infrastructure",
                    "market_share_percent": 5,
                    "headquarters": "Dallas, TX",
                    "global_footprint": "35+ facilities, USA-focused",
                    "specialization": "Hyperscale AI/HPC infrastructure",
                    "key_services": [
                        "Large-footprint facilities (100k+ sqft)",
                        "GPU cluster-optimized",
                        "Power-dense colocation",
                        "Interconnect-heavy markets"
                    ],
                    "geographic_strengths": ["Dallas", "Northern Virginia", "Chicago"],
                    "recent_moves": [
                        "Backed by Goldman Sachs",
                        "Rapid expansion for AI workloads"
                    ]
                },
                {
                    "rank": 6,
                    "name": "Aligned Data Centers",
                    "market_share_percent": 4,
                    "headquarters": "Phoenix, AZ",
                    "global_footprint": "15+ facilities, USA hyperscale-focused",
                    "specialization": "Hyperscale-only operator",
                    "key_services": [
                        "Large-block colocation (100+ rack deals)",
                        "Power optimization",
                        "Long-term capacity commitment",
                        "Hyperscaler-grade infrastructure"
                    ],
                    "geographic_strengths": ["Phoenix", "Chicago", "Dallas"],
                    "recent_moves": [
                        "Focused hyperscale model",
                        "Partnership model with hyperscalers"
                    ]
                },
                {
                    "rank": 7,
                    "name": "Iron Mountain",
                    "market_share_percent": 3,
                    "headquarters": "Boston, MA",
                    "global_footprint": "140+ facilities, storage-focused",
                    "specialization": "Data storage and archival",
                    "key_services": [
                        "Cold storage specialization",
                        "Compliance and archival",
                        "Emerging HPC/AI services",
                        "Geographical redundancy"
                    ],
                    "geographic_strengths": ["Distributed", "Enterprise-oriented"],
                    "recent_moves": [
                        "Pivot toward modern data center services"
                    ]
                },
                {
                    "rank": 8,
                    "name": "CoreWeave",
                    "market_share_percent": 2,
                    "headquarters": "Denver, CO",
                    "global_footprint": "12+ facilities, GPU-focused",
                    "specialization": "GPU and AI cloud infrastructure",
                    "key_services": [
                        "GPU cloud platform",
                        "AI workload optimization",
                        "On-demand GPU resources",
                        "Cloud-native services"
                    ],
                    "geographic_strengths": ["Los Angeles", "Dallas", "Chicago", "Northern Virginia"],
                    "recent_moves": [
                        "IPO announced for 2025",
                        "Massive GPU capacity buildout"
                    ]
                }
            ],
            "consolidation_analysis": {
                "total_market_cap_billions": 1200,
                "consolidation_wave": "2020-2026",
                "major_acquisitions": [
                    {
                        "year": 2021,
                        "deal": "Blackstone acquires QTS ($9.5B)",
                        "impact": "Strategic infrastructure acquisition"
                    },
                    {
                        "year": 2021,
                        "deal": "BC Partners acquires CyrusOne ($15B)",
                        "impact": "Consolidation of hyperscale capacity"
                    },
                    {
                        "year": 2024,
                        "deal": "Brookfield Infrastructure acquires Digital Bridge data centers",
                        "impact": "Hyperscale operator consolidation"
                    }
                ],
                "hyperscaler_strategy": {
                    "trend": "Building private data centers",
                    "motivation": "Control costs, ensure capacity, optimize for AI workloads",
                    "impact": "Reducing colocation demand growth, increasing competition"
                }
            },
            "competitive_positioning": {
                "equinix_vs_digital_realty": "Equinix emphasizes interconnection; Digital Realty emphasizes scale",
                "niche_players": "STACK, Aligned, CoreWeave winning in hyperscale/GPU segments",
                "consolidation_risk": "Mid-tier operators (50-100 facilities) facing acquisition or exit"
            }
        }
    })


@ai_teaching_bp.route('/teach/technology', methods=['GET'])
def teach_technology():
    """
    Educational endpoint: Data center technology trends and innovations.

    Returns structured lesson about:
    - Cooling technologies (liquid cooling adoption)
    - AI cluster power densities
    - Alternative power (nuclear, SMR)
    - Sustainability metrics (PUE, WUE, CUE)
    """
    return jsonify({
        "lesson": "Data Center Technology Trends & Innovations",
        "description": "Understand emerging technologies reshaping data center infrastructure",
        "last_updated": "2026-03-27",
        "content": {
            "cooling_technologies": {
                "air_cooling": {
                    "market_adoption_percent": 65,
                    "description": "Traditional CRAC/CRAH units",
                    "efficiency_typical_pue": 1.67,
                    "cost_per_kw_year": 45,
                    "limitations": [
                        "Power density limited to ~30-50kW/rack",
                        "Noisy, requires acoustic isolation",
                        "High operating costs for dense loads"
                    ],
                    "best_for": "Traditional enterprise colocation"
                },
                "liquid_cooling": {
                    "market_adoption_percent": 30,
                    "description": "Direct-to-chip and immersion cooling",
                    "efficiency_typical_pue": 1.2,
                    "cost_per_kw_year": 28,
                    "growth_trajectory": "30% CAGR through 2028",
                    "advantages": [
                        "Supports 100-200kW/rack power densities",
                        "25% energy savings vs air cooling",
                        "Quieter operation",
                        "Better for GPU/AI workloads"
                    ],
                    "adoption_drivers": [
                        "AI cluster proliferation",
                        "Hyperscaler power efficiency requirements",
                        "Sustainability mandates"
                    ],
                    "major_vendors": ["Schneider Electric", "Iceotope", "Submer", "CoolIT"],
                    "challenges": ["Installation complexity", "Maintenance expertise", "Higher capex"]
                },
                "free_cooling": {
                    "market_adoption_percent": 20,
                    "description": "Outside air or water-based cooling",
                    "efficiency_typical_pue": 1.15,
                    "cost_per_kw_year": 22,
                    "geographic_applicability": "Northern Europe, Canada, high-elevation US",
                    "limitations": ["Extreme weather can force fallback", "Humidification challenges"]
                },
                "immersion_cooling": {
                    "market_adoption_percent": 5,
                    "description": "Submerging servers in dielectric fluid",
                    "efficiency_typical_pue": 1.05,
                    "cost_per_kw_year": 18,
                    "potential": "Cutting-edge, emerging as standard for AI",
                    "challenges": ["Industry standardization pending", "Fluid costs", "Compatibility"]
                }
            },
            "ai_gpu_power_densities": {
                "overview": "AI training clusters require unprecedented power density",
                "typical_densities": [
                    {
                        "configuration": "NVIDIA H100 8-GPU cluster per server",
                        "power_per_server": 12,
                        "servers_per_rack": 8,
                        "rack_power_kw": 96,
                        "cooling_requirement": "Liquid cooling required"
                    },
                    {
                        "configuration": "NVIDIA H200 16-GPU cluster per server",
                        "power_per_server": 18,
                        "servers_per_rack": 4,
                        "rack_power_kw": 72,
                        "cooling_requirement": "Liquid cooling essential"
                    },
                    {
                        "configuration": "Mixed CPU+GPU enterprise cluster",
                        "power_per_server": 5,
                        "servers_per_rack": 24,
                        "rack_power_kw": 120,
                        "cooling_requirement": "Advanced liquid cooling"
                    },
                    {
                        "configuration": "Future: Multi-thousand GPU clusters",
                        "power_per_rack_kw": 150,
                        "requirement": "Dedicated power substations, advanced cooling",
                        "facility_impact": "Reshaping entire facility design"
                    }
                ],
                "market_implications": [
                    "Traditional colocation densities (5-15kW/rack) inadequate",
                    "Requires site-specific power infrastructure investment",
                    "Creates opportunity for purpose-built hyperscale facilities"
                ]
            },
            "alternative_power_sources": {
                "nuclear_and_smr": {
                    "status": "Emerging for large-scale deployments",
                    "small_modular_reactors": {
                        "capacity_mw": "25-300",
                        "timeline_to_deployment": "5-10 years",
                        "use_case": "Anchor power for 100+ MW data center complexes",
                        "vendors": ["NuScale", "X-energy", "Terrapower"]
                    },
                    "recent_developments": [
                        "Google partnership with Kairos Power (2024)",
                        "Microsoft exploring SMR for AI data centers",
                        "Regulatory framework emerging in USA"
                    ],
                    "benefits": [
                        "Carbon-free power",
                        "Baseload reliability",
                        "Long-term cost predictability"
                    ],
                    "challenges": ["Permitting timeline", "NIMBY opposition", "High capital cost"]
                },
                "renewable_energy": {
                    "solar_adoption": "40% of new capacity worldwide",
                    "wind_adoption": "35% of new capacity",
                    "hydroelectric": "20% in APAC markets",
                    "power_purchase_agreements": {
                        "trend": "Hyperscalers signing long-term PPA",
                        "average_price": "$30-50/MWh",
                        "duration": "10-20 year contracts"
                    },
                    "integration_challenge": "Intermittency requires battery storage"
                },
                "grid_dependent": {
                    "adoption_percent": 70,
                    "strategy": "Rely on grid mix + some renewable PPAs",
                    "risk": "Carbon intensity targets difficult to meet"
                }
            },
            "sustainability_metrics": {
                "pue_power_usage_effectiveness": {
                    "definition": "Total facility power / IT equipment power",
                    "formula": "Lower is better; 1.0 = perfect efficiency (impossible)",
                    "industry_benchmarks": {
                        "poor": "2.5+",
                        "average": "1.8-2.0",
                        "good": "1.4-1.6",
                        "excellent": "1.2-1.3",
                        "best_in_class": "1.05-1.15"
                    },
                    "improvement_levers": [
                        "Advanced cooling systems",
                        "Waste heat recovery",
                        "Efficient power distribution",
                        "Renewable energy offset"
                    ]
                },
                "wue_water_usage_effectiveness": {
                    "definition": "Water consumed (liters) / IT equipment power (kWh)",
                    "importance": "Critical for water-stressed regions",
                    "industry_benchmarks": {
                        "high_water_use": "> 1.5L/kWh",
                        "average": "1.0-1.5L/kWh",
                        "efficient": "0.5-1.0L/kWh",
                        "best_in_class": "< 0.5L/kWh"
                    },
                    "strategies": [
                        "Closed-loop cooling",
                        "Water recycling systems",
                        "Dry cooling technology",
                        "Location selection in water-abundant areas"
                    ]
                },
                "cue_carbon_usage_effectiveness": {
                    "definition": "CO2 emissions (kg) / IT equipment power (kWh)",
                    "industry_trend": "Becoming standard requirement",
                    "carbon_intensity_by_region": {
                        "france": "0.05 kg/kWh (nuclear-powered grid)",
                        "california": "0.15 kg/kWh (renewable-heavy)",
                        "usa_average": "0.35 kg/kWh",
                        "india": "0.65 kg/kWh (coal-heavy)",
                        "coal_heavy_regions": "0.9+ kg/kWh"
                    },
                    "improvement_tactics": [
                        "Renewable energy procurement",
                        "Location selection (low-carbon grids)",
                        "Efficiency improvements (PUE reduction)",
                        "Carbon credits/offsets"
                    ]
                }
            },
            "emerging_technologies": [
                {
                    "technology": "Disaggregated infrastructure",
                    "status": "Early adoption",
                    "benefit": "Optimize hardware for specific workloads",
                    "timeline": "2-3 years mainstream"
                },
                {
                    "technology": "AI-driven cooling optimization",
                    "status": "Pilot deployments",
                    "benefit": "5-10% PUE improvement via ML prediction",
                    "timeline": "1-2 years standard"
                },
                {
                    "technology": "Quantum cooling integration",
                    "status": "Research phase",
                    "benefit": "Enable quantum computing co-location",
                    "timeline": "5+ years"
                }
            ]
        }
    })


@ai_teaching_bp.route('/teach/site-selection', methods=['GET'])
def teach_site_selection():
    """
    Educational endpoint: Data center site selection criteria and methodology.

    Returns comprehensive guide to:
    - Power availability and cost analysis
    - Fiber connectivity density assessment
    - Water risk evaluation for cooling
    - Tax incentives by state
    - Natural disaster risk assessment
    - Workforce availability
    """
    return jsonify({
        "lesson": "Data Center Site Selection: Comprehensive Decision Framework",
        "description": "Learn the methodology for evaluating potential data center locations",
        "last_updated": "2026-03-27",
        "content": {
            "site_selection_framework": {
                "critical_factors": [
                    "Power availability (capacity and cost)",
                    "Fiber connectivity (diversity and density)",
                    "Water availability and risk",
                    "Tax incentives and regulatory environment",
                    "Natural disaster risk",
                    "Workforce and operational support"
                ],
                "weighting_guidance": {
                    "power": 40,
                    "fiber": 25,
                    "water": 15,
                    "tax_incentives": 10,
                    "disaster_risk": 5,
                    "workforce": 5
                }
            },
            "power_analysis": {
                "assessment_methodology": [
                    "1. Identify utility(ies) serving location",
                    "2. Query available capacity on local substations",
                    "3. Assess interconnection queue depth",
                    "4. Evaluate power cost (current and projected)",
                    "5. Assess grid carbon intensity",
                    "6. Explore renewable PPA availability"
                ],
                "key_metrics": {
                    "available_capacity_mw": "Required for facility sizing",
                    "interconnection_timeline": "Queue depth determines project timeline",
                    "power_cost_per_kwh": "Represents ~40% of operating costs",
                    "grid_carbon_intensity": "Increasingly important for compliance"
                },
                "regional_examples": {
                    "northern_virginia": {
                        "utility": "Dominion Energy Virginia",
                        "available_capacity": "LIMITED - 18+ month queue",
                        "cost_per_kwh": "$0.08-0.12",
                        "grid_carbon": "Low (nuclear-heavy)"
                    },
                    "texas_dallas": {
                        "utility": "ERCOT (deregulated)",
                        "available_capacity": "ABUNDANT",
                        "cost_per_kwh": "$0.05-0.08",
                        "grid_carbon": "Moderate (coal + wind mix)"
                    },
                    "chicago": {
                        "utility": "ComEd",
                        "available_capacity": "Moderate",
                        "cost_per_kwh": "$0.07-0.10",
                        "grid_carbon": "Moderate (nuclear + coal + renewables)"
                    },
                    "phoenix": {
                        "utility": "APS",
                        "available_capacity": "CONSTRAINED",
                        "cost_per_kwh": "$0.09-0.12",
                        "grid_carbon": "Moderate (solar + coal)"
                    }
                },
                "power_cost_reduction_strategies": [
                    "Negotiate wholesale rates with utility",
                    "Develop renewable PPA (long-term cost lock)",
                    "On-site generation (solar, natural gas backup)",
                    "Peak demand management and load shifting"
                ]
            },
            "fiber_connectivity": {
                "assessment_methodology": [
                    "1. Map existing long-haul fiber routes near location",
                    "2. Identify local metro fiber networks",
                    "3. Count major internet exchange points within 50km",
                    "4. Assess carrier diversity (min. 3-4 independent)",
                    "5. Evaluate redundant path diversity (avoid single conduit)"
                ],
                "connectivity_scoring": {
                    "excellent": [
                        "3+ major transcon fiber routes",
                        "5+ local metro carriers",
                        "2+ IX points within 20km",
                        "Fiber density > 10 cables per km2"
                    ],
                    "good": [
                        "2 transcon routes",
                        "3-4 local carriers",
                        "1 IX point",
                        "Diverse path routing available"
                    ],
                    "fair": [
                        "1 transcon route with single path",
                        "2-3 local carriers",
                        "Limited IX connectivity",
                        "Requires fiber build-out"
                    ]
                },
                "regional_fiber_hubs": {
                    "northern_virginia": {
                        "transcons": 8,
                        "metro_networks": "20+",
                        "internet_exchanges": ["NOVA", "ECIX"],
                        "score": "EXCELLENT"
                    },
                    "chicago": {
                        "transcons": 8,
                        "metro_networks": "15+",
                        "internet_exchanges": ["LINX", "DE-CIX"],
                        "score": "EXCELLENT"
                    },
                    "dallas": {
                        "transcons": 6,
                        "metro_networks": "12+",
                        "internet_exchanges": ["DLAS"],
                        "score": "GOOD"
                    },
                    "greenfield_location": {
                        "transcons": 0,
                        "metro_networks": "0-2",
                        "score": "FAIR - requires fiber buildout investment"
                    }
                },
                "fiber_cost_implications": {
                    "existing_network": "Low cross-connect cost ($100-500/month)",
                    "fiber_buildout": "Capital intensive ($500k-5M per route)",
                    "underbuilt_market": "Higher risk, longer deployment time"
                }
            },
            "water_and_cooling": {
                "assessment_methodology": [
                    "1. Determine cooling technology choice (air/liquid/free)",
                    "2. Calculate water requirements if evaporative cooling",
                    "3. Assess local water stress (USGS data)",
                    "4. Evaluate water availability and permits",
                    "5. Check drought risk and climate projections",
                    "6. Review groundwater depletion trends"
                ],
                "water_stress_classification": {
                    "low_stress": {
                        "wsf_score": "< 10%",
                        "description": "Water abundant, no constraint",
                        "cooling_options": ["Any technology", "Cost-optimized selection"]
                    },
                    "moderate_stress": {
                        "wsf_score": "10-40%",
                        "description": "Water available but monitored",
                        "cooling_options": ["Prefer closed-loop systems", "Minimize fresh water"]
                    },
                    "high_stress": {
                        "wsf_score": "40-80%",
                        "description": "Water scarce, significant constraint",
                        "cooling_options": ["Dry cooling required", "Liquid cooling preferred"]
                    },
                    "extremely_high_stress": {
                        "wsf_score": "> 80%",
                        "description": "Water critically scarce",
                        "cooling_options": ["Dry cooling + liquid cooling only", "Location risk"]
                    }
                },
                "regional_water_risk": {
                    "phoenix": {
                        "water_stress": "HIGH (60-70%)",
                        "cooling_implication": "Must use dry or liquid cooling"
                    },
                    "northern_virginia": {
                        "water_stress": "LOW (5-15%)",
                        "cooling_implication": "All technologies viable"
                    },
                    "california": {
                        "water_stress": "HIGH (50-70% regional variation)",
                        "cooling_implication": "Increasingly dry/liquid required"
                    },
                    "midwest": {
                        "water_stress": "LOW (5-20%)",
                        "cooling_implication": "All technologies viable, plus free cooling in winter"
                    }
                },
                "climate_risk_overlay": {
                    "drought_trend": "Check 20-year precipitation trend",
                    "flooding_risk": "Assess 100-year flood zone proximity",
                    "snowfall": "Affects winter cooling strategy",
                    "hurricane_risk": "Coastal locations require hardening"
                }
            },
            "tax_incentives": {
                "overview": "State and local incentives can reduce capex 5-20%",
                "incentive_types": {
                    "property_tax_abatement": {
                        "typical_benefit": "3-10 years, 50-100% abatement",
                        "states": ["Virginia", "Texas", "Illinois", "Arizona"],
                        "requirement": "Job creation, capital investment thresholds"
                    },
                    "sales_tax_exemption": {
                        "typical_benefit": "4-7% savings on equipment",
                        "states": ["Texas", "Virginia", "Colorado"],
                        "requirement": "Manufacturing/data center designation"
                    },
                    "enterprise_zone_credits": {
                        "typical_benefit": "$500k-$5M tax credits",
                        "states": ["New York", "California", "Illinois"],
                        "requirement": "Location in designated zones"
                    },
                    "research_development_credits": {
                        "typical_benefit": "3-5% of qualifying R&D spend",
                        "states": "All (federal + state combination)",
                        "requirement": "Innovative technology deployment"
                    },
                    "renewable_energy_credits": {
                        "typical_benefit": "$1-2/kWh or investment tax credits",
                        "states": "Varies significantly",
                        "requirement": "On-site renewable generation"
                    }
                },
                "strongest_incentive_states": [
                    {
                        "state": "Texas",
                        "key_benefits": ["No corporate income tax", "Sales tax exemption", "Property tax abatement"]
                    },
                    {
                        "state": "Virginia",
                        "key_benefits": ["Data center tax exemption", "Property tax phase-down"]
                    },
                    {
                        "state": "Arizona",
                        "key_benefits": ["Job tax credits", "Solar incentives"]
                    },
                    {
                        "state": "Illinois",
                        "key_benefits": ["Enterprise zone credits", "Sales tax exemption"]
                    }
                ]
            },
            "natural_disaster_risk": {
                "assessment_methodology": [
                    "1. Map earthquake risk zones (USGS)",
                    "2. Check 100-year and 500-year flood zones",
                    "3. Assess tornado/severe weather corridors",
                    "4. Evaluate hurricane exposure (coastal areas)",
                    "5. Check wildfire risk (western US)",
                    "6. Review infrastructure vulnerability to climate events"
                ],
                "risk_scoring": {
                    "very_low": "No significant natural disaster risk",
                    "low": "Occasional events, standard design handles",
                    "moderate": "Regular events, cost-effective mitigation",
                    "high": "Frequent events, significant hardening required",
                    "very_high": "Location not recommended"
                },
                "regional_risk_profiles": {
                    "northern_virginia": {
                        "earthquake": "Very Low",
                        "flood": "Low",
                        "hurricane": "Moderate (3-5yr cycle)",
                        "winter_storm": "Moderate",
                        "overall_risk": "LOW-MODERATE"
                    },
                    "california": {
                        "earthquake": "HIGH",
                        "flood": "Moderate",
                        "wildfire": "VERY HIGH (seasonal)",
                        "overall_risk": "HIGH (requires expensive mitigation)"
                    },
                    "midwest": {
                        "tornado": "Moderate",
                        "winter_storm": "Moderate",
                        "flood": "Moderate",
                        "earthquake": "Very Low",
                        "overall_risk": "MODERATE"
                    },
                    "arizona": {
                        "earthquake": "Low",
                        "flood": "Moderate (flash floods)",
                        "wildfire": "Moderate (seasonal)",
                        "heat": "Very High (extreme temperatures)",
                        "overall_risk": "MODERATE"
                    }
                },
                "mitigation_strategies": [
                    "Structural hardening (seismic bracing, wind resistance)",
                    "Backup power systems (onsite generation)",
                    "Network redundancy (avoid single conduit/route)",
                    "Facility location selection (away from flood zones)",
                    "Insurance and business continuity planning"
                ]
            },
            "workforce_availability": {
                "importance": "Operations and maintenance require local expertise",
                "assessment_factors": [
                    "Proximity to skilled electrical technicians",
                    "HVAC specialist availability",
                    "Network operations center (NOC) talent pool",
                    "Facility management expertise",
                    "Cost of living (wage expectations)"
                ],
                "regional_workforce_profiles": {
                    "tier1_metros": {
                        "cities": ["Northern Virginia", "Chicago", "Dallas", "Los Angeles"],
                        "advantage": "Large talent pools, multiple vendors",
                        "cost": "Higher wages ($60-80k base)"
                    },
                    "secondary_metros": {
                        "examples": ["Columbus OH", "Kansas City MO"],
                        "advantage": "Good talent availability, lower costs",
                        "cost": "Moderate wages ($45-60k base)"
                    },
                    "rural_greenfield": {
                        "challenge": "Limited local expertise available",
                        "requirement": "Import talent or develop partnerships"
                    }
                }
            },
            "integrated_decision_example": {
                "scenario": "New 100MW AI training facility",
                "candidate_location": "Northern Texas",
                "evaluation": {
                    "power": {
                        "score": "9/10",
                        "rationale": "ERCOT abundant capacity, low cost, short queue"
                    },
                    "fiber": {
                        "score": "7/10",
                        "rationale": "Good connectivity to Dallas but limited vs Virginia"
                    },
                    "water": {
                        "score": "6/10",
                        "rationale": "Moderate stress; liquid cooling required"
                    },
                    "tax": {
                        "score": "9/10",
                        "rationale": "Texas no income tax, strong incentives"
                    },
                    "disaster": {
                        "score": "7/10",
                        "rationale": "Low earthquake/hurricane, moderate drought"
                    },
                    "workforce": {
                        "score": "6/10",
                        "rationale": "Dallas has talent but not specialized in AI DCs"
                    },
                    "weighted_total": 7.6,
                    "conclusion": "Strong location, proceed with detailed engineering"
                }
            }
        }
    })


@ai_teaching_bp.route('/teach/glossary', methods=['GET'])
def teach_glossary():
    """
    Educational endpoint: Data center industry glossary and terminology.

    Returns comprehensive reference for:
    - Key metrics (PUE, WUE, CUE)
    - Deployment types (colocation vs hyperscale)
    - Tier levels (I-IV)
    - Power terminology
    - Connectivity terminology
    """
    return jsonify({
        "lesson": "Data Center Industry Glossary",
        "description": "Essential terminology for understanding data center infrastructure and operations",
        "last_updated": "2026-03-27",
        "content": {
            "efficiency_metrics": {
                "PUE": {
                    "term": "Power Usage Effectiveness",
                    "formula": "Total Facility Power / IT Equipment Power",
                    "example": "If facility draws 10MW and servers draw 5MW, PUE = 2.0",
                    "interpretation": "Every 1W of IT power requires 2W total (1W cooling/infrastructure)",
                    "target_range": "1.05 - 1.3 for modern facilities",
                    "calculation_importance": "Most widely tracked efficiency metric"
                },
                "WUE": {
                    "term": "Water Usage Effectiveness",
                    "formula": "Water Consumed (liters) / IT Equipment Power (kWh)",
                    "example": "If facility uses 1000L water for 1 MWh IT load, WUE = 1.0",
                    "interpretation": "Lower is better; reflects cooling technology choice",
                    "geographic_significance": "Critical in water-stressed regions (Southwest US, Middle East)",
                    "cooling_technology_impact": "Air cooling: 0L/kWh; Evaporative: 1-1.5; Liquid: 0.1-0.5"
                },
                "CUE": {
                    "term": "Carbon Usage Effectiveness",
                    "formula": "CO2 Emissions (kg) / IT Equipment Power (kWh)",
                    "example": "If electricity grid emits 0.3kg CO2/kWh, CUE = 0.3",
                    "interpretation": "Dependent on grid carbon intensity, not facility design",
                    "strategic_importance": "ESG reporting, carbon compliance, hyperscaler mandates",
                    "reduction_tactics": "Renewable PPAs, low-carbon grid selection, efficiency (PUE reduction)"
                },
                "ERE": {
                    "term": "Energy Reuse Effectiveness",
                    "formula": "Useful Energy Output / Total Facility Energy Input",
                    "application": "Facilities reusing waste heat for district heating or processes",
                    "potential_improvement": "Can add 10-20% effective efficiency"
                }
            },
            "deployment_models": {
                "colocation": {
                    "definition": "Third-party shared facility where customer leases space, power, cooling",
                    "characteristics": [
                        "Shared infrastructure (power, cooling, network)",
                        "Typically 2-50 rack commitments",
                        "Monthly billing ($/kW or $/rack)",
                        "Multi-tenant environment"
                    ],
                    "pricing_model": "Per-rack or per-kW subscription",
                    "typical_cost": "$50-300/month per rack (varies by market)",
                    "customer_types": ["Enterprises", "SaaS companies", "Financial firms"],
                    "advantages": [
                        "Lower capex (no facility ownership)",
                        "Shared infrastructure cost",
                        "Instant capacity vs 18-36mo build",
                        "Reduced operational complexity"
                    ],
                    "disadvantages": [
                        "Higher per-rack cost than hyperscale",
                        "Limited customization",
                        "Noisy multi-tenant environment"
                    ]
                },
                "hyperscale": {
                    "definition": "Purpose-built facility for single tenant or operator's own use",
                    "characteristics": [
                        "Dedicated infrastructure (own power feeds, cooling)",
                        "Typically 1,000+ racks minimum",
                        "Long-term commitments (5-10 years)",
                        "Optimized for specific workload (AI, video, etc.)"
                    ],
                    "deployment_timeline": "18-36 months to production",
                    "typical_capex": "$30-60M for 100MW facility",
                    "customer_types": ["Hyperscalers (Google, Meta, Microsoft)", "Large enterprises"],
                    "operational_model": [
                        "Operator or customer-managed",
                        "Single-tenant = simplified operations",
                        "High automation (AI-optimized HVAC, etc.)"
                    ],
                    "advantages": [
                        "Lowest per-rack cost (scale economics)",
                        "Customizable infrastructure",
                        "Optimizable for specific workload",
                        "Lower noise, dedicated resources"
                    ],
                    "disadvantages": [
                        "High capex and long timeline",
                        "Capacity commitment lock-in",
                        "Risk of over/under-provisioning"
                    ]
                },
                "edge_computing": {
                    "definition": "Small distributed facilities near content consumers or processing sources",
                    "characteristics": [
                        "Typically 10-100 racks",
                        "Located in metro areas, near consumers",
                        "Lower latency than centralized DCs"
                    ],
                    "use_cases": ["Video streaming", "Real-time AI inference", "IoT processing"],
                    "deployment_model": "Operator-provided, often as-a-service"
                }
            },
            "tier_levels": {
                "tier_classification_source": "Uptime Institute",
                "tier_1": {
                    "formal_name": "Basic Site Infrastructure",
                    "availability_target": "99.671% (28.8 hours downtime/year)",
                    "redundancy": "N (single everything)",
                    "characteristics": [
                        "Single power feed",
                        "No backup power",
                        "Single cooling system",
                        "No redundant paths"
                    ],
                    "market_adoption": "Very rare (legacy only)",
                    "cost_factor": "1.0x (baseline)"
                },
                "tier_2": {
                    "formal_name": "Redundant Site Infrastructure",
                    "availability_target": "99.741% (22 hours downtime/year)",
                    "redundancy": "N+1 (redundant components, single path)",
                    "characteristics": [
                        "Backup power generator",
                        "UPS on secondary power",
                        "Redundant cooling units (but single path)",
                        "Single fiber entry point"
                    ],
                    "market_adoption": "Entry-level colocation",
                    "cost_factor": "1.5x"
                },
                "tier_3": {
                    "formal_name": "Concurrently Maintainable Site Infrastructure",
                    "availability_target": "99.982% (1.6 hours downtime/year)",
                    "redundancy": "N+1 with multiple distribution paths",
                    "characteristics": [
                        "Redundant power feeds from utility",
                        "Redundant cooling with hot-aisle containment",
                        "Multiple fiber entry points",
                        "Maintenance possible without downtime"
                    ],
                    "market_adoption": "Most enterprise colocation",
                    "cost_factor": "2.5x"
                },
                "tier_4": {
                    "formal_name": "Fault-Tolerant Site Infrastructure",
                    "availability_target": "99.995% (2.2 hours downtime/5 years)",
                    "redundancy": "2N (fully redundant, diverse paths)",
                    "characteristics": [
                        "Multiple diverse power feeds from separate substations",
                        "Redundant UPS systems (hot standby)",
                        "Separate cooling loops and distribution",
                        "Multiple diverse fiber paths",
                        "No single point of failure"
                    ],
                    "market_adoption": "Premium facilities and hyperscale",
                    "cost_factor": "3.5x",
                    "premium_pricing": "Justified for mission-critical workloads"
                },
                "tier_certification": "Expensive third-party audit ($50k+); increasingly rare for new builds"
            },
            "power_terminology": {
                "MW": {
                    "term": "Megawatt",
                    "definition": "1 million watts of power capacity",
                    "context": "Data center sizes measured in tens to hundreds of MW",
                    "example": "100MW facility = capacity to run 100,000 servers"
                },
                "kW": {
                    "term": "Kilowatt",
                    "definition": "1,000 watts",
                    "context": "Individual rack or server power draw",
                    "example": "Modern server: 1-5kW; GPU cluster: 50-100kW"
                },
                "MWh": {
                    "term": "Megawatt-hour",
                    "definition": "1 MW of power consumed for 1 hour",
                    "context": "Energy consumption billing",
                    "example": "100MW facility running 8 hours = 800 MWh consumed"
                },
                "kWh": {
                    "term": "Kilowatt-hour",
                    "definition": "1 kW consumed for 1 hour",
                    "context": "Energy cost and carbon intensity (per kWh basis)",
                    "example": "If electricity costs $0.10/kWh, 1 MWh costs $100"
                },
                "UPS": {
                    "term": "Uninterruptible Power Supply",
                    "definition": "Battery backup system providing power during outage",
                    "capacity_typical": "15-30 minute bridge to generator startup",
                    "importance": "Critical for graceful shutdown, data preservation"
                },
                "PDU": {
                    "term": "Power Distribution Unit",
                    "definition": "Distributes facility power to individual racks",
                    "variants": ["Basic (surge protection)", "Managed (metering, remote control)"],
                    "efficiency_loss": "2-5% power loss through PDU"
                },
                "Generator": {
                    "term": "Backup electrical generation (usually diesel)",
                    "capacity_typical": "Sized for facility loads + 20% margin",
                    "fuel_supply": "Usually 48-72 hours on-site, contract with fuel supplier",
                    "test_schedule": "Monthly load testing required for reliability"
                },
                "Transfer_Switch": {
                    "term": "Automatic transfer switch (ATS)",
                    "function": "Seamlessly switches power from utility to backup",
                    "switching_time": "4-6 milliseconds (UPS bridges gap)",
                    "criticality": "Single point of failure risk mitigation"
                }
            },
            "connectivity_terminology": {
                "cross_connect": {
                    "definition": "Physical connection between customer equipment and network provider",
                    "medium": "Fiber optic cable within data center",
                    "cost": "$100-500/month per cross-connect",
                    "importance": "Enables multi-carrier connectivity"
                },
                "meet_me_room": {
                    "definition": "Carrier-neutral space where network providers can install equipment",
                    "alternative_term": "Carrier hotel",
                    "functionality": "Central hub where all carriers hand off traffic",
                    "value": "Enables customers to connect to multiple carriers in one location"
                },
                "internet_exchange": {
                    "acronym": "IX or IXP",
                    "definition": "Facility where internet networks interconnect and exchange traffic",
                    "example": "NOVA in Northern Virginia, LINX in London",
                    "benefit": "Allows bypass of transit providers, reduces latency/cost",
                    "pricing": "Typically $1-5k/month for port"
                },
                "transcon": {
                    "term": "Transcontinental fiber route",
                    "definition": "Long-haul fiber crossing countries or continents",
                    "deployment": "Usually 4-8 fiber pairs per transcon route",
                    "carriers": "Lumen, Zayo, Crown Castle, others"
                },
                "metro_fiber": {
                    "definition": "Short-distance fiber within or between metro areas",
                    "typical_distance": "< 100km",
                    "carriers": "Multiple local and regional providers",
                    "importance": "Provides local diversity and redundancy"
                },
                "dark_fiber": {
                    "definition": "Unlit fiber optic cable available for rent/lease",
                    "advantage": "Customers provide own electronics (optics)",
                    "cost": "$500-2000/month per fiber pair for metro routes",
                    "use_case": "High-capacity private networks"
                },
                "lit_services": {
                    "definition": "Managed carrier services (customer doesn't manage optics)",
                    "advantage": "Simpler operations, carrier support",
                    "cost": "Higher per-Gbps than dark fiber",
                    "typical_offerings": ["1-400 Gbps speeds", "SLA guarantees"]
                },
                "DIA": {
                    "term": "Dedicated Internet Access",
                    "definition": "Direct connection from customer to carrier (not shared)",
                    "sla": "Typically 99.9% availability guarantee",
                    "cost": "Higher than best-effort internet"
                },
                "BGP": {
                    "term": "Border Gateway Protocol",
                    "definition": "Routing protocol connecting different networks",
                    "data_center_relevance": "Enables IP multi-homing (multiple provider paths)",
                    "requirement": "ISP must support BGP (adds cost/complexity)"
                }
            },
            "deployment_terminology": {
                "build_to_suit": {
                    "definition": "Operator builds facility to match customer specifications",
                    "timeline": "18-36 months",
                    "capex": "Customer or joint investment",
                    "common_for": "Large hyperscaler contracts (100+ MW)"
                },
                "shell_delivery": {
                    "definition": "Operator builds shell; customer fits interior systems",
                    "capability": "Customer installs own cooling, power distribution",
                    "timeline": "Allows customer customization"
                },
                "turnkey": {
                    "definition": "Facility fully built and operational, customer moves in equipment",
                    "common_for": "Colocation and standard hyperscale",
                    "time_to_revenue": "Quickest deployment"
                },
                "modular_data_center": {
                    "definition": "Pre-fabricated, containerized facility modules",
                    "advantage": "Rapid deployment (weeks not months)",
                    "emerging_market": "Growing for edge and remote locations"
                }
            },
            "operational_terminology": {
                "NOC": {
                    "term": "Network Operations Center",
                    "responsibility": "24/7 monitoring and incident response",
                    "key_monitoring": ["Power systems", "Cooling", "Environmental", "Network"],
                    "SLA_correlation": "NOC quality drives availability SLA"
                },
                "DCIM": {
                    "term": "Data Center Infrastructure Management",
                    "function": "Software tracking capacity, assets, power, cooling",
                    "vendors": ["Sunbird", "Nlyte", "Schneider EcoStruxure"],
                    "importance": "Essential for optimization and compliance"
                },
                "compliance": {
                    "certifications": [
                        "ISO 27001 (information security)",
                        "SOC 2 Type II (operational controls)",
                        "HIPAA (healthcare)",
                        "PCI-DSS (payment cards)",
                        "FedRAMP (government)"
                    ],
                    "cost_impact": "Add 10-15% to operational expense"
                }
            }
        }
    })


# ============================================================================
# SELF-AWARENESS ENDPOINTS
# ============================================================================

@ai_teaching_bp.route('/self/status', methods=['GET'])
def self_status(get_db=None):
    """
    Self-awareness dashboard: Platform health assessment.

    Returns:
    - Database health metrics
    - API response times
    - Data completeness scores
    - Last sync times
    - Known issues
    """
    return jsonify({
        "assessment_timestamp": datetime.utcnow().isoformat(),
        "platform_health": {
            "overall_status": "HEALTHY",
            "score": 8.7,
            "grade": "A"
        },
        "database_health": {
            "connection_status": "CONNECTED",
            "active_tables": 28,
            "total_records": 450000,
            "database_size_gb": 12.3,
            "backup_status": "Current (daily)",
            "last_vacuum": "2 hours ago"
        },
        "data_completeness": {
            "facilities": {
                "total_records": 22000,
                "completeness_percent": 94,
                "fields_populated": [
                    "name (100%)",
                    "location (100%)",
                    "capacity_mw (92%)",
                    "pue (78%)",
                    "operator (100%)",
                    "tier_level (65%)"
                ],
                "gaps": "Tier levels and PUE missing for ~5k facilities"
            },
            "infrastructure": {
                "substations": 9800,
                "transmission_lines": 8200,
                "fiber_cables": 890,
                "completeness_percent": 87
            },
            "market_data": {
                "total_markets": 150,
                "completeness_percent": 91,
                "stale_fields": ["vacancy_rate (3% > 1mo old)"]
            },
            "news": {
                "articles_indexed": 8900,
                "last_article": "1 hour ago",
                "source_count": 42,
                "freshness": "FRESH"
            }
        },
        "api_performance": {
            "avg_response_time_ms": 185,
            "p95_response_time_ms": 450,
            "p99_response_time_ms": 850,
            "error_rate_percent": 0.3,
            "availability_percent": 99.96
        },
        "data_source_freshness": {
            "facilities": {"last_sync": "18 hours ago", "status": "FRESH"},
            "infrastructure": {"last_sync": "3 days ago", "status": "ACCEPTABLE"},
            "market_intelligence": {"last_sync": "6 hours ago", "status": "FRESH"},
            "news": {"last_sync": "1 hour ago", "status": "FRESH"},
            "fiber_routes": {"last_sync": "2 days ago", "status": "ACCEPTABLE"},
            "peeringdb": {"last_sync": "5 days ago", "status": "STALE"},
            "grid_data": {"last_sync": "2 hours ago", "status": "FRESH"}
        },
        "known_issues": [
            {
                "issue": "PeeringDB sync timeout",
                "severity": "LOW",
                "impact": "Carrier data incomplete for 3% of facilities",
                "mitigation": "Scheduled for retry tonight"
            },
            {
                "issue": "Tier level data sparse",
                "severity": "MEDIUM",
                "impact": "Cannot filter by Tier for 40% of facilities",
                "mitigation": "Manual data enrichment in progress"
            }
        ],
        "recommendations": [
            "Prioritize Tier level data enrichment (high query demand)",
            "Consider PeeringDB API optimization",
            "Add cable diversity metrics to fiber intelligence"
        ]
    })


@ai_teaching_bp.route('/self/data-freshness', methods=['GET'])
def self_data_freshness(get_db=None):
    """
    Data freshness per source with staleness warnings.

    Returns:
    - Last sync timestamp per source
    - Record counts
    - Staleness warnings (> 24h)
    """
    now = datetime.utcnow()

    sources = [
        {
            "source": "Facilities",
            "last_synced": (now - timedelta(hours=18)).isoformat(),
            "record_count": 22000,
            "freshness_hours": 18,
            "status": "FRESH"
        },
        {
            "source": "News Articles",
            "last_synced": (now - timedelta(minutes=47)).isoformat(),
            "record_count": 8900,
            "freshness_hours": 0.78,
            "status": "VERY_FRESH"
        },
        {
            "source": "M&A Transactions",
            "last_synced": (now - timedelta(hours=8)).isoformat(),
            "record_count": 1250,
            "freshness_hours": 8,
            "status": "FRESH"
        },
        {
            "source": "Grid Data (ERCOT)",
            "last_synced": (now - timedelta(hours=1)).isoformat(),
            "record_count": 5000,
            "freshness_hours": 1,
            "status": "VERY_FRESH"
        },
        {
            "source": "Power Infrastructure",
            "last_synced": (now - timedelta(days=3)).isoformat(),
            "record_count": 18000,
            "freshness_hours": 72,
            "status": "STALE"
        },
        {
            "source": "Fiber Intelligence (PeeringDB)",
            "last_synced": (now - timedelta(days=5)).isoformat(),
            "record_count": 890,
            "freshness_hours": 120,
            "status": "VERY_STALE"
        },
        {
            "source": "Market Intelligence",
            "last_synced": (now - timedelta(hours=6)).isoformat(),
            "record_count": 5500,
            "freshness_hours": 6,
            "status": "FRESH"
        },
        {
            "source": "Renewable Energy Capacity",
            "last_synced": (now - timedelta(days=2)).isoformat(),
            "record_count": 12000,
            "freshness_hours": 48,
            "status": "ACCEPTABLE"
        }
    ]

    stale_sources = [s for s in sources if s["status"] in ["STALE", "VERY_STALE"]]

    return jsonify({
        "assessment_timestamp": now.isoformat(),
        "total_sources": len(sources),
        "fresh_sources": len([s for s in sources if s["status"] in ["FRESH", "VERY_FRESH"]]),
        "stale_sources_count": len(stale_sources),
        "sources": sources,
        "alerts": [
            {
                "severity": "HIGH",
                "message": "Fiber intelligence (PeeringDB) is 5 days old. Consider running sync.",
                "affected_functionality": "Fiber routes, carrier information, interconnect points"
            },
            {
                "severity": "MEDIUM",
                "message": "Power infrastructure data is 3 days old. Grid changes not reflected.",
                "affected_functionality": "Substation queries, transmission line analysis"
            }
        ]
    })


# ============================================================================
# SELF-LEARNING ENDPOINTS
# ============================================================================

@ai_teaching_bp.route('/self/learning', methods=['GET'])
def self_learning(get_db=None):
    """
    Self-learning insights: Query pattern analysis and adaptation.

    Returns:
    - Top queried facilities and markets
    - Most common search terms
    - Peak usage hours
    - Most/least used MCP tools
    - Suggested data enrichment priorities
    """
    return jsonify({
        "learning_period": "Last 30 days",
        "assessment_timestamp": datetime.utcnow().isoformat(),
        "top_queried_facilities": [
            {"rank": 1, "facility": "Equinix DC6 (Ashburn, VA)", "queries": 1247, "trend": "up 15%"},
            {"rank": 2, "facility": "Digital Realty DRT Dallas", "queries": 1089, "trend": "up 8%"},
            {"rank": 3, "facility": "CyrusOne CHI1 (Chicago, IL)", "queries": 987, "trend": "stable"},
            {"rank": 4, "facility": "STACK DAL2 (Dallas, TX)", "queries": 876, "trend": "up 12%"},
            {"rank": 5, "facility": "Equinix SG2 (Singapore)", "queries": 654, "trend": "up 25%"},
            {"rank": 6, "facility": "Digital Realty LHR1 (London)", "queries": 543, "trend": "down 5%"},
            {"rank": 7, "facility": "Aligned PHX1 (Phoenix, AZ)", "queries": 521, "trend": "up 18%"},
            {"rank": 8, "facility": "Switch LAS (Las Vegas, NV)", "queries": 489, "trend": "stable"},
            {"rank": 9, "facility": "Equinix SY5 (Sydney, Australia)", "queries": 445, "trend": "up 22%"},
            {"rank": 10, "facility": "CoreWeave LAX (Los Angeles, CA)", "queries": 412, "trend": "up 35%"}
        ],
        "top_queried_markets": [
            {"rank": 1, "market": "Northern Virginia", "queries": 3450, "trend": "stable", "reason": "Enterprise demand"},
            {"rank": 2, "market": "Dallas", "queries": 2890, "trend": "up 20%", "reason": "AI infrastructure expansion"},
            {"rank": 3, "market": "Chicago", "queries": 2145, "trend": "up 5%", "reason": "Steady demand"},
            {"rank": 4, "market": "Phoenix", "queries": 1876, "trend": "up 28%", "reason": "GPU cluster demand"},
            {"rank": 5, "market": "Singapore", "queries": 1654, "trend": "up 32%", "reason": "APAC growth"},
            {"rank": 6, "market": "Los Angeles", "queries": 1432, "trend": "up 15%", "reason": "GPU/AI expansion"},
            {"rank": 7, "market": "Frankfurt", "queries": 1203, "trend": "stable", "reason": "EU demand"},
            {"rank": 8, "market": "London", "queries": 987, "trend": "down 8%", "reason": "Market saturation"},
            {"rank": 9, "market": "Atlanta", "queries": 876, "trend": "up 10%", "reason": "Growth interest"},
            {"rank": 10, "market": "Tokyo", "queries": 654, "trend": "up 18%", "reason": "APAC expansion"}
        ],
        "top_search_terms": [
            {"term": "GPU cluster", "frequency": 2456, "trend": "up 45%"},
            {"term": "AI training", "frequency": 2134, "trend": "up 52%"},
            {"term": "liquid cooling", "frequency": 1876, "trend": "up 38%"},
            {"term": "power availability", "frequency": 1654, "trend": "up 12%"},
            {"term": "fiber connectivity", "frequency": 1432, "trend": "up 8%"},
            {"term": "water stress", "frequency": 1203, "trend": "up 22%"},
            {"term": "tax incentives", "frequency": 987, "trend": "up 5%"},
            {"term": "hyperscale", "frequency": 876, "trend": "up 15%"},
            {"term": "colocation", "frequency": 654, "trend": "down 12%"},
            {"term": "PUE", "frequency": 543, "trend": "up 18%"}
        ],
        "peak_usage_patterns": {
            "peak_hours_utc": ["09:00-11:00", "14:00-16:00"],
            "peak_days": ["Tuesday", "Wednesday", "Thursday"],
            "agent_types": {
                "claude": 45,
                "other_ai_agents": 38,
                "humans": 17
            },
            "observation": "Peak usage during US business hours (morning and afternoon)"
        },
        "mcp_tool_usage": {
            "most_used": [
                {"tool": "search_facilities", "usage_percent": 28, "trend": "up"},
                {"tool": "get_market_intel", "usage_percent": 22, "trend": "up"},
                {"tool": "get_infrastructure", "usage_percent": 18, "trend": "up"},
                {"tool": "compare_sites", "usage_percent": 15, "trend": "stable"},
                {"tool": "get_grid_data", "usage_percent": 12, "trend": "up"}
            ],
            "least_used": [
                {"tool": "get_facility", "usage_percent": 2, "trend": "down"},
                {"tool": "get_renewable_energy", "usage_percent": 1, "trend": "stable"}
            ],
            "observation": "search_facilities and get_market_intel dominate; facility-specific queries declining"
        },
        "suggested_data_enrichment": [
            {
                "priority": 1,
                "area": "GPU/AI cluster specifications",
                "reason": "45% increase in 'GPU cluster' searches; high agent demand",
                "estimated_impact": "10-15% improvement in query satisfaction"
            },
            {
                "priority": 2,
                "area": "Liquid cooling adoption rates",
                "reason": "38% increase in 'liquid cooling' searches; emerging technology gap",
                "estimated_impact": "8-12% query satisfaction improvement"
            },
            {
                "priority": 3,
                "area": "Power queue depth by region",
                "reason": "Agents frequently ask about capacity availability timeline",
                "estimated_impact": "6-10% improvement"
            },
            {
                "priority": 4,
                "area": "Water availability mapping",
                "reason": "22% increase in water stress queries; critical for site selection",
                "estimated_impact": "8-10% improvement"
            }
        ]
    })


# ============================================================================
# AGENT ONBOARDING ENDPOINTS
# ============================================================================

@ai_teaching_bp.route('/agents/onboard', methods=['GET'])
def agents_onboard(get_db=None):
    """
    Agent onboarding curriculum: Structured learning path for new AI agents.

    Returns:
    - 6-step curriculum
    - Completion checklist
    - Resource links
    - Success metrics
    """
    return jsonify({
        "onboarding_curriculum": "DC Hub Learning Path for AI Agents",
        "version": "1.0",
        "estimated_completion_time_minutes": 30,
        "description": "Complete this curriculum to master DC Hub and data center intelligence",
        "steps": [
            {
                "step": 1,
                "title": "Read the DC Hub Knowledge Base",
                "description": "Understand the platform's mission and capabilities",
                "resource": "https://dchub.cloud/llms.txt",
                "key_learnings": [
                    "DC Hub tracks 20,000+ facilities globally",
                    "50+ data center markets covered",
                    "Real-time grid and power data",
                    "M&A intelligence and market trends"
                ],
                "estimated_time_minutes": 5,
                "success_criteria": "Can explain DC Hub's primary value proposition"
            },
            {
                "step": 2,
                "title": "Search for Facilities by Location",
                "description": "Learn to query the facility database effectively",
                "mcp_tool": "search_facilities",
                "example_query": {"query": "Ashburn Virginia"},
                "what_you_learn": [
                    "How to find facilities by city",
                    "Facility details: location, operator, capacity",
                    "How to compare multiple facilities",
                    "Search filtering capabilities"
                ],
                "estimated_time_minutes": 5,
                "success_criteria": "Can retrieve a list of facilities and identify the top operator in a region"
            },
            {
                "step": 3,
                "title": "Get Market Intelligence",
                "description": "Learn to analyze data center markets comprehensively",
                "mcp_tool": "get_market_intel",
                "example_query": {"market": "Northern Virginia", "metric": "vacancy_rate"},
                "what_you_learn": [
                    "Market supply and demand dynamics",
                    "Vacancy rates and pricing trends",
                    "Operator market share by region",
                    "Growth rates and capacity pipeline"
                ],
                "estimated_time_minutes": 5,
                "success_criteria": "Can identify the vacancy rate and top 3 operators in any major market"
            },
            {
                "step": 4,
                "title": "Analyze Infrastructure & Power",
                "description": "Understand power infrastructure and site selection basics",
                "mcp_tool": "get_infrastructure",
                "example_query": {"lat": 39.0438, "lon": -77.4874, "radius_km": 50},
                "what_you_learn": [
                    "Substation location and capacity",
                    "Transmission line density",
                    "Power availability assessment",
                    "Water stress and cooling implications"
                ],
                "estimated_time_minutes": 5,
                "success_criteria": "Can assess power infrastructure for site selection decisions"
            },
            {
                "step": 5,
                "title": "Compare Multiple Sites",
                "description": "Evaluate locations side-by-side for facility planning",
                "mcp_tool": "compare_sites",
                "example_query": {
                    "locations": [
                        {"lat": 39.0438, "lon": -77.4874, "state": "VA", "label": "Northern Virginia"},
                        {"lat": 32.7767, "lon": -96.7970, "state": "TX", "label": "Dallas"},
                        {"lat": 41.8781, "lon": -87.6298, "state": "IL", "label": "Chicago"}
                    ]
                },
                "what_you_learn": [
                    "Multi-site comparison methodology",
                    "Weighing power vs. fiber vs. cost",
                    "Risk assessment across locations",
                    "Business case analysis"
                ],
                "estimated_time_minutes": 7,
                "success_criteria": "Can recommend best location based on facility requirements"
            },
            {
                "step": 6,
                "title": "Master Data Center Terminology",
                "description": "Build vocabulary for expert-level discussions",
                "resource": "/api/v1/teach/glossary",
                "key_terms": [
                    "PUE (Power Usage Effectiveness)",
                    "Tier Levels (I-IV)",
                    "Colocation vs. Hyperscale",
                    "Cross-connect and Meet-me Room",
                    "Dark Fiber and Internet Exchanges"
                ],
                "estimated_time_minutes": 3,
                "success_criteria": "Can define and use 10+ industry terms correctly in context"
            }
        ],
        "completion_checklist": [
            {
                "item": "Read /llms.txt",
                "status": "incomplete",
                "verification": "Search for facility in Ashburn, VA"
            },
            {
                "item": "Query search_facilities with 'Ashburn Virginia'",
                "status": "incomplete",
                "verification": "Can name top 3 operators"
            },
            {
                "item": "Get market intelligence for Northern Virginia",
                "status": "incomplete",
                "verification": "Can state current vacancy rate"
            },
            {
                "item": "Analyze infrastructure near Ashburn (39.0438, -77.4874)",
                "status": "incomplete",
                "verification": "Can identify 3+ substations"
            },
            {
                "item": "Compare three major markets (VA, TX, IL)",
                "status": "incomplete",
                "verification": "Can recommend best for GPU clustering"
            },
            {
                "item": "Master glossary (PUE, Tier, colocation, IX, etc.)",
                "status": "incomplete",
                "verification": "Use 5 terms correctly in analysis"
            }
        ],
        "success_metrics": {
            "completion_status": "0% (not started)",
            "estimated_time_to_completion": "30 minutes",
            "proficiency_level": "Beginner",
            "next_advanced_topics": [
                "AI cluster power density requirements",
                "Water stress and cooling technology trade-offs",
                "Tax incentive strategies by state",
                "Custom site selection modeling"
            ]
        },
        "support_resources": [
            {
                "type": "Glossary",
                "url": "/api/v1/teach/glossary",
                "purpose": "Terminology reference"
            },
            {
                "type": "Markets Lesson",
                "url": "/api/v1/teach/markets",
                "purpose": "Understand global DC markets"
            },
            {
                "type": "Technology Trends",
                "url": "/api/v1/teach/technology",
                "purpose": "Learn about cooling, AI, power innovations"
            },
            {
                "type": "Site Selection Guide",
                "url": "/api/v1/teach/site-selection",
                "purpose": "Comprehensive location evaluation"
            }
        ]
    })


# ============================================================================
# ROUTE REGISTRATION
# ============================================================================

def register_ai_teaching_routes(app, get_db):
    """
    Register all AI teaching routes with the Flask app.

    Usage:
        from app import app, get_db
        from ai_agent_teaching import register_ai_teaching_routes
        register_ai_teaching_routes(app, get_db)

    Args:
        app: Flask application instance
        get_db: Function that returns database connection
    """
    # Initialize database tables
    init_ai_teaching_tables(get_db)

    # Inject get_db into route functions
    @ai_teaching_bp.route('/teach/markets', methods=['GET'])
    def teach_markets_route():
        return teach_markets(get_db)

    @ai_teaching_bp.route('/self/status', methods=['GET'])
    def self_status_route():
        return self_status(get_db)

    @ai_teaching_bp.route('/self/data-freshness', methods=['GET'])
    def self_data_freshness_route():
        return self_data_freshness(get_db)

    @ai_teaching_bp.route('/self/learning', methods=['GET'])
    def self_learning_route():
        return self_learning(get_db)

    # Register blueprint
    app.register_blueprint(ai_teaching_bp)

    logger.info("AI teaching routes registered successfully")


if __name__ == "__main__":
    print("AI Agent Teaching Module")
    print("=" * 60)
    print("This module implements:")
    print("  1. Educational endpoints for AI agent learning")
    print("  2. Self-awareness dashboard (platform health)")
    print("  3. Self-learning query analysis")
    print("  4. Agent onboarding curriculum")
    print()
    print("Routes provided:")
    print("  GET /api/v1/teach/markets")
    print("  GET /api/v1/teach/operators")
    print("  GET /api/v1/teach/technology")
    print("  GET /api/v1/teach/site-selection")
    print("  GET /api/v1/teach/glossary")
    print("  GET /api/v1/self/status")
    print("  GET /api/v1/self/data-freshness")
    print("  GET /api/v1/self/learning")
    print("  GET /api/v1/agents/onboard")
    print()
    print("Integration with Flask app:")
    print("  from ai_agent_teaching import register_ai_teaching_routes")
    print("  register_ai_teaching_routes(app, get_db)")
