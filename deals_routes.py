"""
DC Hub - Deals, Transactions, Pipeline, Markets & News Routes
Phase 2 Extract 3: 16 routes + helper functions
Extracted from main.py to reduce monolith size

Sections:
  - DEALS / TRANSACTIONS API (deals, transactions, freemium logic)
  - CONSTRUCTION PIPELINE DATA & API (pipeline projects, gas pipelines)
  - DC MARKETS API (market data for analytics page)
  - NEWS / ANNOUNCEMENTS API (news feed, sync, announcements)

Dependencies injected via init_deals_routes():
  - require_plan, protect_data (decorators)
  - get_db, pg_connection (database)
  - get_ai_wars_key_info (auth helper)
  - _real_require_plan (dynamic tier gating)
"""

import os
import time
import logging
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify
from utils.pipeline_alias import expand_query, matches_any  # phase32_alias_normalize

from utils.cache import BoundedCache

logger = logging.getLogger(__name__)

deals_bp = Blueprint('deals', __name__)

# Late-binding decorator/dependency slots
_require_plan = None
_protect_data = None
_get_db = None
_pg_connection = None
_get_ai_wars_key_info = None
_real_require_plan_ref = None


def init_deals_routes(require_plan, protect_data, get_db, pg_connection, get_ai_wars_key_info, real_require_plan=None):
    """Inject dependencies from main.py (late-binding pattern)."""
    global _require_plan, _protect_data, _get_db, _pg_connection, _get_ai_wars_key_info, _real_require_plan_ref
    _require_plan = require_plan
    _protect_data = protect_data
    _get_db = get_db
    _pg_connection = pg_connection
    _get_ai_wars_key_info = get_ai_wars_key_info
    _real_require_plan_ref = real_require_plan


def _lazy_protect_data(f):
    """Wrapper that defers to injected protect_data at request time."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _protect_data is not None:
            return _protect_data(f)(*args, **kwargs)
        return f(*args, **kwargs)
    return wrapper


def _lazy_require_plan(plan_name):
    """Wrapper that defers to injected require_plan at request time."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if _require_plan is not None:
                return _require_plan(plan_name)(f)(*args, **kwargs)
            return f(*args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# DEALS / TRANSACTIONS API
# =============================================================================

# COMPREHENSIVE DEALS DATABASE 2020-2025
# Includes: M&A, Equity, JV, Land, Debt, Hyperscaler CapEx, AI Contracts
# Sources: Synergy Research, S&P Global, Company filings
SAMPLE_DEALS = [
    # =========================================================================
    # 2025 - RECORD YEAR
    # =========================================================================

    # === MEGA AI INFRASTRUCTURE DEALS ===

    # Stargate Project
    {"id": "2025-AI-001", "date": "2025-01-21", "year": 2025, "buyer": "Stargate (OpenAI/SoftBank/Oracle/MGX)", "seller": "US AI Infrastructure", "value": 500000, "mw": 10000, "type": "ai_infra", "region": "North America", "market": "Multiple US", "status": "Announced", "notes": "4-year commitment, 10GW"},

    # OpenAI + Oracle $300B
    {"id": "2025-AI-002", "date": "2025-07-15", "year": 2025, "buyer": "OpenAI", "seller": "Oracle Cloud", "value": 300000, "mw": 4500, "type": "ai_contract", "region": "North America", "market": "Multiple US", "status": "Signed", "notes": "5-year cloud contract"},

    # Nvidia investment in OpenAI
    {"id": "2025-AI-003", "date": "2025-09-01", "year": 2025, "buyer": "Nvidia", "seller": "OpenAI", "value": 100000, "mw": 0, "type": "ai_infra", "region": "North America", "market": "Multiple", "status": "Announced", "notes": "Investment for 10GW Nvidia DCs"},

    # OpenAI + AWS
    {"id": "2025-AI-004", "date": "2025-11-03", "year": 2025, "buyer": "OpenAI", "seller": "Amazon AWS", "value": 38000, "mw": 0, "type": "ai_contract", "region": "North America", "market": "Multiple", "status": "Signed", "notes": "7-year cloud contract"},

    # OpenAI + CoreWeave (total)
    {"id": "2025-AI-005", "date": "2025-09-25", "year": 2025, "buyer": "OpenAI", "seller": "CoreWeave", "value": 22400, "mw": 0, "type": "ai_contract", "region": "North America", "market": "Multiple", "status": "Signed", "notes": "$11.9B + $4B + $6.5B expansions"},

    # CoreWeave + Meta
    {"id": "2025-AI-006", "date": "2025-10-01", "year": 2025, "buyer": "Meta", "seller": "CoreWeave", "value": 14200, "mw": 0, "type": "ai_contract", "region": "North America", "market": "Multiple", "status": "Signed", "notes": "Through 2031"},

    # === HYPERSCALER CAPEX 2025 ===

    {"id": "2025-CAP-001", "date": "2025-01-01", "year": 2025, "buyer": "Amazon AWS", "seller": "Self-Build CapEx", "value": 100000, "mw": 5000, "type": "capex", "region": "Global", "market": "Multiple", "status": "Committed", "notes": "FY2025 AI infrastructure"},
    {"id": "2025-CAP-002", "date": "2025-01-01", "year": 2025, "buyer": "Microsoft Azure", "seller": "Self-Build CapEx", "value": 80000, "mw": 4000, "type": "capex", "region": "Global", "market": "Multiple", "status": "Committed", "notes": "FY2025 ending June 30"},
    {"id": "2025-CAP-003", "date": "2025-01-01", "year": 2025, "buyer": "Google Cloud", "seller": "Self-Build CapEx", "value": 75000, "mw": 3500, "type": "capex", "region": "Global", "market": "Multiple", "status": "Committed", "notes": "2025 infrastructure"},
    {"id": "2025-CAP-004", "date": "2025-01-01", "year": 2025, "buyer": "Meta", "seller": "Self-Build CapEx", "value": 65000, "mw": 3000, "type": "capex", "region": "Global", "market": "Multiple", "status": "Committed", "notes": "Raised from $60-64B to $64-72B"},
    {"id": "2025-CAP-005", "date": "2025-01-01", "year": 2025, "buyer": "Oracle", "seller": "Self-Build CapEx", "value": 25000, "mw": 1500, "type": "capex", "region": "Global", "market": "Multiple", "status": "Committed", "notes": "Stargate infrastructure"},

    # === TRADITIONAL M&A 2025 ===

    # Aligned - Largest DC deal ever
    {"id": "2025-MA-001", "date": "2025-10-16", "year": 2025, "buyer": "BlackRock GIP/MGX/Microsoft/Nvidia", "seller": "Aligned Data Centers", "value": 40000, "mw": 5000, "type": "ma", "region": "North America", "market": "Multiple US/LATAM", "status": "Pending", "notes": "Closes H1 2026"},

    # SoftBank acquires DigitalBridge
    {"id": "2025-MA-002", "date": "2025-12-29", "year": 2025, "buyer": "SoftBank Group", "seller": "DigitalBridge Group", "value": 4000, "mw": 0, "type": "ma", "region": "Global", "market": "Multiple", "status": "Pending"},

    # CoreWeave/Core Scientific (rejected)
    {"id": "2025-MA-003", "date": "2025-07-15", "year": 2025, "buyer": "CoreWeave", "seller": "Core Scientific", "value": 9000, "mw": 500, "type": "ma", "region": "North America", "market": "Multiple US", "status": "Rejected"},

    # Centersquare acquisitions
    {"id": "2025-MA-004", "date": "2025-10-03", "year": 2025, "buyer": "Centersquare", "seller": "10 Data Centers", "value": 1000, "mw": 150, "type": "ma", "region": "North America", "market": "US/Canada", "status": "Closed"},

    # Aligned equity raise
    {"id": "2025-EQ-001", "date": "2025-01-15", "year": 2025, "buyer": "Macquarie Funds", "seller": "Aligned Data Centers", "value": 5000, "mw": 0, "type": "equity", "region": "North America", "market": "Multiple", "status": "Closed"},

    # Vantage APAC investment
    {"id": "2025-EQ-002", "date": "2025-06-01", "year": 2025, "buyer": "GIC/ADIA", "seller": "Vantage Data Centers APAC", "value": 1600, "mw": 300, "type": "equity", "region": "APAC", "market": "Malaysia/Japan", "status": "Closed"},

    # Meta Louisiana financing
    {"id": "2025-DEBT-001", "date": "2025-06-01", "year": 2025, "buyer": "Meta/Blue Owl", "seller": "Louisiana DC Financing", "value": 27000, "mw": 2000, "type": "debt", "region": "North America", "market": "Louisiana", "status": "Closed"},

    # Oracle debt for Stargate
    {"id": "2025-DEBT-002", "date": "2025-09-01", "year": 2025, "buyer": "Oracle", "seller": "Stargate Debt Financing", "value": 18000, "mw": 0, "type": "debt", "region": "North America", "market": "Multiple", "status": "Closed"},

    # =========================================================================
    # 2024 - RECORD BREAKING M&A YEAR ($73B closed)
    # =========================================================================

    # === HYPERSCALER CAPEX 2024 ===

    {"id": "2024-CAP-001", "date": "2024-01-01", "year": 2024, "buyer": "Amazon AWS", "seller": "Self-Build CapEx", "value": 75000, "mw": 3500, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2024-CAP-002", "date": "2024-01-01", "year": 2024, "buyer": "Microsoft Azure", "seller": "Self-Build CapEx", "value": 55000, "mw": 2800, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2024-CAP-003", "date": "2024-01-01", "year": 2024, "buyer": "Google Cloud", "seller": "Self-Build CapEx", "value": 52000, "mw": 2500, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2024-CAP-004", "date": "2024-01-01", "year": 2024, "buyer": "Meta", "seller": "Self-Build CapEx", "value": 38000, "mw": 1800, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},

    # === TRADITIONAL M&A 2024 ===

    # AirTrunk - Second largest ever
    {"id": "2024-MA-001", "date": "2024-09-25", "year": 2024, "buyer": "Blackstone/CPPIB", "seller": "AirTrunk", "value": 16000, "mw": 1800, "type": "ma", "region": "APAC", "market": "Australia/Japan/Singapore", "status": "Closed"},

    # Vantage mega equity round
    {"id": "2024-EQ-001", "date": "2024-06-13", "year": 2024, "buyer": "DigitalBridge/Silver Lake", "seller": "Vantage Data Centers", "value": 9200, "mw": 3000, "type": "equity", "region": "Global", "market": "North America/EMEA", "status": "Closed"},

    # Blackstone/QTS Spain
    {"id": "2024-LAND-001", "date": "2024-10-15", "year": 2024, "buyer": "Blackstone/QTS", "seller": "Spain Development", "value": 8200, "mw": 1000, "type": "land", "region": "EMEA", "market": "Spain (Aragon)", "status": "Announced"},

    # Digital Realty + Blackstone JV
    {"id": "2024-JV-001", "date": "2024-08-08", "year": 2024, "buyer": "Blackstone/Digital Realty JV", "seller": "Hyperscale Development", "value": 7000, "mw": 1000, "type": "jv", "region": "Global", "market": "Multiple", "status": "Closed"},

    # ESR going private
    {"id": "2024-MA-002", "date": "2024-12-15", "year": 2024, "buyer": "Starwood/Sixth Street/QIA/Warburg", "seller": "ESR Group", "value": 7100, "mw": 575, "type": "ma", "region": "APAC", "market": "Multiple APAC", "status": "Pending"},

    # Ares acquires Ada Infrastructure
    {"id": "2024-MA-003", "date": "2024-10-20", "year": 2024, "buyer": "Ares Management", "seller": "GLP Capital/Ada Infrastructure", "value": 3700, "mw": 1000, "type": "ma", "region": "Global", "market": "London/Tokyo/São Paulo", "status": "Closed"},

    # Vantage EMEA additional
    {"id": "2024-EQ-002", "date": "2024-03-01", "year": 2024, "buyer": "Various Investors", "seller": "Vantage EMEA", "value": 3100, "mw": 400, "type": "equity", "region": "EMEA", "market": "Multiple EU", "status": "Closed"},

    # BlackRock acquires GIP
    {"id": "2024-MA-004", "date": "2024-10-01", "year": 2024, "buyer": "BlackRock", "seller": "Global Infrastructure Partners", "value": 3000, "mw": 0, "type": "ma", "region": "Global", "market": "Multiple", "status": "Closed"},

    # DigitalBridge acquires Yondr
    {"id": "2024-MA-005", "date": "2024-10-15", "year": 2024, "buyer": "DigitalBridge", "seller": "Yondr Group", "value": 2000, "mw": 878, "type": "ma", "region": "Global", "market": "Virginia/UK/Malaysia/Japan", "status": "Closed"},

    # EdgeCore debt financing
    {"id": "2024-DEBT-001", "date": "2024-01-04", "year": 2024, "buyer": "EdgeCore Digital", "seller": "Debt Financing", "value": 1900, "mw": 500, "type": "debt", "region": "North America", "market": "Mesa, Arizona", "status": "Closed"},

    # Vantage EMEA (AustralianSuper)
    {"id": "2024-EQ-003", "date": "2024-01-15", "year": 2024, "buyer": "AustralianSuper", "seller": "Vantage EMEA", "value": 1600, "mw": 500, "type": "equity", "region": "EMEA", "market": "Multiple EU", "status": "Closed"},

    # HMC Capital/Global Switch Australia
    {"id": "2024-MA-006", "date": "2024-06-01", "year": 2024, "buyer": "HMC Capital", "seller": "Global Switch Australia", "value": 1400, "mw": 200, "type": "ma", "region": "APAC", "market": "Sydney", "status": "Closed"},

    # KKR/Singtel STT GDC
    {"id": "2024-EQ-004", "date": "2024-04-15", "year": 2024, "buyer": "KKR/Singtel", "seller": "STT GDC", "value": 1300, "mw": 300, "type": "equity", "region": "APAC", "market": "Singapore/APAC", "status": "Closed"},

    # Blue Owl acquires IPI
    {"id": "2024-MA-007", "date": "2024-10-01", "year": 2024, "buyer": "Blue Owl Capital", "seller": "IPI Partners", "value": 1000, "mw": 2200, "type": "ma", "region": "Global", "market": "Multiple", "status": "Closed"},

    # Crusoe/Blue Owl JV
    {"id": "2024-JV-002", "date": "2024-08-01", "year": 2024, "buyer": "Blue Owl/Crusoe", "seller": "AI Data Center JV", "value": 3400, "mw": 400, "type": "jv", "region": "North America", "market": "Texas", "status": "Closed"},

    # CoreWeave debt facility
    {"id": "2024-DEBT-002", "date": "2024-05-01", "year": 2024, "buyer": "Magnetar/Blackstone", "seller": "CoreWeave", "value": 2300, "mw": 0, "type": "debt", "region": "North America", "market": "Multiple", "status": "Closed"},

    # =========================================================================
    # 2023 - Slower Year ($26B traditional M&A)
    # =========================================================================

    # === HYPERSCALER CAPEX 2023 ===

    {"id": "2023-CAP-001", "date": "2023-01-01", "year": 2023, "buyer": "Amazon AWS", "seller": "Self-Build CapEx", "value": 50000, "mw": 2000, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2023-CAP-002", "date": "2023-01-01", "year": 2023, "buyer": "Microsoft Azure", "seller": "Self-Build CapEx", "value": 32000, "mw": 1500, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2023-CAP-003", "date": "2023-01-01", "year": 2023, "buyer": "Google Cloud", "seller": "Self-Build CapEx", "value": 32000, "mw": 1400, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2023-CAP-004", "date": "2023-01-01", "year": 2023, "buyer": "Meta", "seller": "Self-Build CapEx", "value": 28000, "mw": 1200, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},

    # === TRADITIONAL M&A 2023 ===

    # ChinData taken private
    {"id": "2023-MA-001", "date": "2023-09-15", "year": 2023, "buyer": "Bain Capital", "seller": "ChinData Group", "value": 3160, "mw": 500, "type": "ma", "region": "APAC", "market": "China", "status": "Closed"},

    # Brookfield acquires Data4
    {"id": "2023-MA-002", "date": "2023-04-20", "year": 2023, "buyer": "Brookfield", "seller": "Data4", "value": 2000, "mw": 350, "type": "ma", "region": "EMEA", "market": "France/Italy/Spain", "status": "Closed"},

    # Vantage EMEA - AustralianSuper initial
    {"id": "2023-EQ-001", "date": "2023-09-15", "year": 2023, "buyer": "AustralianSuper", "seller": "Vantage EMEA Stake", "value": 1600, "mw": 300, "type": "equity", "region": "EMEA", "market": "Multiple EU", "status": "Closed"},

    # DataBank recapitalization
    {"id": "2023-EQ-002", "date": "2023-03-01", "year": 2023, "buyer": "Swiss Life/EDF/Northleaf/Ardian", "seller": "DataBank (35% stake)", "value": 1500, "mw": 165, "type": "equity", "region": "North America", "market": "Multiple US", "status": "Closed"},

    # GIC/Digital Realty JV
    {"id": "2023-JV-001", "date": "2023-05-01", "year": 2023, "buyer": "GIC", "seller": "Digital Realty JV Stake", "value": 1400, "mw": 200, "type": "jv", "region": "APAC", "market": "Japan/Korea", "status": "Closed"},

    # NTT Global expansion
    {"id": "2023-MA-003", "date": "2023-06-15", "year": 2023, "buyer": "NTT Ltd", "seller": "Various DC Assets", "value": 1200, "mw": 200, "type": "ma", "region": "Global", "market": "Multiple", "status": "Closed"},

    # Cyxtera bankruptcy/Brookfield
    {"id": "2023-MA-004", "date": "2023-11-15", "year": 2023, "buyer": "Brookfield", "seller": "Cyxtera Technologies", "value": 775, "mw": 180, "type": "ma", "region": "North America", "market": "Multiple", "status": "Closed"},

    # Equinix Chile
    {"id": "2023-MA-005", "date": "2023-08-01", "year": 2023, "buyer": "Equinix", "seller": "Entel Data Centers", "value": 735, "mw": 85, "type": "ma", "region": "LATAM", "market": "Chile", "status": "Closed"},

    # =========================================================================
    # 2022 - Peak M&A Year ($48-52B)
    # =========================================================================

    # === HYPERSCALER CAPEX 2022 ===

    {"id": "2022-CAP-001", "date": "2022-01-01", "year": 2022, "buyer": "Amazon AWS", "seller": "Self-Build CapEx", "value": 40000, "mw": 1800, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2022-CAP-002", "date": "2022-01-01", "year": 2022, "buyer": "Microsoft Azure", "seller": "Self-Build CapEx", "value": 25000, "mw": 1200, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2022-CAP-003", "date": "2022-01-01", "year": 2022, "buyer": "Google Cloud", "seller": "Self-Build CapEx", "value": 32000, "mw": 1400, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2022-CAP-004", "date": "2022-01-01", "year": 2022, "buyer": "Meta", "seller": "Self-Build CapEx", "value": 32000, "mw": 1400, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},

    # === TRADITIONAL M&A 2022 ===

    # CyrusOne - Closed
    {"id": "2022-MA-001", "date": "2022-03-25", "year": 2022, "buyer": "KKR/Global Infrastructure Partners", "seller": "CyrusOne", "value": 15000, "mw": 1400, "type": "ma", "region": "North America", "market": "Multiple US/EMEA", "status": "Closed"},

    # Switch
    {"id": "2022-MA-002", "date": "2022-05-11", "year": 2022, "buyer": "DigitalBridge/IFM Investors", "seller": "Switch Inc", "value": 11000, "mw": 1200, "type": "ma", "region": "North America", "market": "Las Vegas/Multiple", "status": "Closed"},

    # Stonepeak/American Tower DC
    {"id": "2022-EQ-001", "date": "2022-07-15", "year": 2022, "buyer": "Stonepeak", "seller": "American Tower DC Business (29%)", "value": 2500, "mw": 200, "type": "equity", "region": "North America", "market": "Multiple US", "status": "Closed"},

    # Lumen EMEA to Colt
    {"id": "2022-MA-003", "date": "2022-11-01", "year": 2022, "buyer": "Colt Technology Services", "seller": "Lumen EMEA", "value": 1800, "mw": 150, "type": "ma", "region": "EMEA", "market": "Multiple EU", "status": "Closed"},

    # DataBank recap
    {"id": "2022-EQ-002", "date": "2022-06-01", "year": 2022, "buyer": "DigitalBridge Recapitalization", "seller": "DataBank", "value": 1500, "mw": 155, "type": "equity", "region": "North America", "market": "Multiple US", "status": "Closed"},

    # =========================================================================
    # 2021 - Mega Deal Year ($50B)
    # =========================================================================

    # === HYPERSCALER CAPEX 2021 ===

    {"id": "2021-CAP-001", "date": "2021-01-01", "year": 2021, "buyer": "Amazon AWS", "seller": "Self-Build CapEx", "value": 35000, "mw": 1500, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2021-CAP-002", "date": "2021-01-01", "year": 2021, "buyer": "Microsoft Azure", "seller": "Self-Build CapEx", "value": 20000, "mw": 900, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2021-CAP-003", "date": "2021-01-01", "year": 2021, "buyer": "Google Cloud", "seller": "Self-Build CapEx", "value": 25000, "mw": 1100, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2021-CAP-004", "date": "2021-01-01", "year": 2021, "buyer": "Meta", "seller": "Self-Build CapEx", "value": 19000, "mw": 850, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},

    # === TRADITIONAL M&A 2021 ===

    # CyrusOne - Announced
    {"id": "2021-MA-001", "date": "2021-11-15", "year": 2021, "buyer": "KKR/Global Infrastructure Partners", "seller": "CyrusOne", "value": 15000, "mw": 1400, "type": "ma", "region": "North America", "market": "Multiple US/EMEA", "status": "Announced"},

    # CoreSite
    {"id": "2021-MA-002", "date": "2021-11-15", "year": 2021, "buyer": "American Tower Corporation", "seller": "CoreSite Realty", "value": 10100, "mw": 450, "type": "ma", "region": "North America", "market": "Silicon Valley/Multiple", "status": "Closed"},

    # QTS Realty Trust
    {"id": "2021-MA-003", "date": "2021-10-18", "year": 2021, "buyer": "Blackstone Infrastructure", "seller": "QTS Realty Trust", "value": 10000, "mw": 850, "type": "ma", "region": "North America", "market": "Multiple US", "status": "Closed"},

    # Stonepeak/Cologix
    {"id": "2021-MA-004", "date": "2021-07-01", "year": 2021, "buyer": "Stonepeak", "seller": "Cologix", "value": 3000, "mw": 280, "type": "ma", "region": "North America", "market": "US/Canada", "status": "Closed"},

    # DigitalBridge/Vantage SDC
    {"id": "2021-MA-005", "date": "2021-05-15", "year": 2021, "buyer": "DigitalBridge", "seller": "Vantage SDC", "value": 3500, "mw": 420, "type": "ma", "region": "North America", "market": "Multiple US", "status": "Closed"},

    # GIC/Digital Edge JV
    {"id": "2021-JV-001", "date": "2021-06-01", "year": 2021, "buyer": "GIC", "seller": "Digital Edge JV", "value": 1200, "mw": 150, "type": "jv", "region": "APAC", "market": "Multiple Asia", "status": "Closed"},

    # Equinix/Bell Canada
    {"id": "2021-MA-006", "date": "2021-10-01", "year": 2021, "buyer": "Equinix", "seller": "Bell Canada DC Portfolio", "value": 750, "mw": 65, "type": "ma", "region": "North America", "market": "Canada", "status": "Closed"},

    # =========================================================================
    # 2020 - Pre-AI Boom ($31B traditional)
    # =========================================================================

    # === HYPERSCALER CAPEX 2020 ===

    {"id": "2020-CAP-001", "date": "2020-01-01", "year": 2020, "buyer": "Amazon AWS", "seller": "Self-Build CapEx", "value": 28000, "mw": 1200, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2020-CAP-002", "date": "2020-01-01", "year": 2020, "buyer": "Microsoft Azure", "seller": "Self-Build CapEx", "value": 18000, "mw": 800, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2020-CAP-003", "date": "2020-01-01", "year": 2020, "buyer": "Google Cloud", "seller": "Self-Build CapEx", "value": 22000, "mw": 950, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},
    {"id": "2020-CAP-004", "date": "2020-01-01", "year": 2020, "buyer": "Meta", "seller": "Self-Build CapEx", "value": 15000, "mw": 650, "type": "capex", "region": "Global", "market": "Multiple", "status": "Spent"},

    # === TRADITIONAL M&A 2020 ===

    # Interxion - Closed
    {"id": "2020-MA-001", "date": "2020-03-04", "year": 2020, "buyer": "Digital Realty", "seller": "Interxion", "value": 8400, "mw": 520, "type": "ma", "region": "EMEA", "market": "Multiple EU", "status": "Closed"},

    # Vertiv SPAC
    {"id": "2020-MA-002", "date": "2020-02-07", "year": 2020, "buyer": "GS Acquisition Holdings (SPAC)", "seller": "Vertiv Holdings", "value": 5300, "mw": 0, "type": "ma", "region": "Global", "market": "Equipment", "status": "Closed"},

    # GIC/Equinix Asia JV
    {"id": "2020-JV-001", "date": "2020-10-15", "year": 2020, "buyer": "GIC", "seller": "Equinix Asia JV", "value": 3000, "mw": 350, "type": "jv", "region": "APAC", "market": "Multiple Asia", "status": "Closed"},

    # DigitalBridge/Vantage NA
    {"id": "2020-MA-003", "date": "2020-08-01", "year": 2020, "buyer": "DigitalBridge", "seller": "Vantage NA", "value": 2800, "mw": 350, "type": "ma", "region": "North America", "market": "Multiple US", "status": "Closed"},

    # Stonepeak/Cologix equity
    {"id": "2020-EQ-001", "date": "2020-09-15", "year": 2020, "buyer": "Stonepeak", "seller": "Cologix", "value": 2500, "mw": 240, "type": "equity", "region": "North America", "market": "Multiple", "status": "Closed"},

    # Macquarie/AirTrunk initial
    {"id": "2020-EQ-002", "date": "2020-06-01", "year": 2020, "buyer": "Macquarie Asset Management", "seller": "AirTrunk (Majority)", "value": 2000, "mw": 500, "type": "equity", "region": "APAC", "market": "Australia/Asia", "status": "Closed"},
]

# =============================================================================
# CONSTRUCTION PIPELINE DATA (v86)
# =============================================================================

PIPELINE_DATA = [
    {"company": "Amazon/AWS", "project": "Project Rainier (Anthropic)", "market": "Indiana", "capacity": 960, "investment": 2500, "delivery": "2025-Q4", "status": "operational", "preleased": True, "type": "hyperscale"},
    {"company": "Oracle", "project": "Abilene Campus Phase 1 (Stargate)", "market": "Abilene, TX", "capacity": 900, "investment": 2000, "delivery": "2025-Q4", "status": "operational", "preleased": True, "type": "ai-hyperscale"},
    {"company": "xAI", "project": "Colossus 1", "market": "Memphis, TN", "capacity": 300, "investment": 800, "delivery": "2025-Q3", "status": "operational", "preleased": True, "type": "ai-gpu"},
    {"company": "Google", "project": "West Memphis Campus", "market": "West Memphis, AR", "capacity": 500, "investment": 1200, "delivery": "2025-Q4", "status": "operational", "preleased": True, "type": "hyperscale"},
    {"company": "Microsoft", "project": "Mount Pleasant Phase 1", "market": "Mount Pleasant, WI", "capacity": 400, "investment": 1000, "delivery": "2025-Q4", "status": "operational", "preleased": True, "type": "hyperscale"},
    {"company": "Oracle/OpenAI", "project": "Stargate Abilene Expansion", "market": "Abilene, TX", "capacity": 600, "investment": 1500, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Oracle/OpenAI", "project": "Stargate Texas Site 2", "market": "Texas", "capacity": 800, "investment": 2000, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Oracle/OpenAI", "project": "Stargate New Mexico", "market": "New Mexico", "capacity": 700, "investment": 1800, "delivery": "2026-Q4", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Oracle/OpenAI", "project": "Stargate Ohio", "market": "Ohio", "capacity": 600, "investment": 1500, "delivery": "2026-Q4", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Oracle/OpenAI/Vantage", "project": "Stargate Wisconsin", "market": "Port Washington, WI", "capacity": 900, "investment": 2500, "delivery": "2028-Q2", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Vantage", "project": "Frontier Campus", "market": "Shackelford County, TX", "capacity": 1400, "investment": 4000, "delivery": "2027-Q4", "status": "construction", "preleased": False, "type": "hyperscale"},
    {"company": "xAI", "project": "Colossus 2", "market": "Memphis, TN", "capacity": 1000, "investment": 3000, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "ai-gpu"},
    {"company": "Meta", "project": "Louisiana AI Campus", "market": "Richland Parish, LA", "capacity": 1500, "investment": 10000, "delivery": "2027-Q3", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Meta", "project": "Ohio AI Cluster", "market": "Ohio", "capacity": 1000, "investment": 5000, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Meta", "project": "El Paso Data Center", "market": "El Paso, TX", "capacity": 500, "investment": 2000, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Microsoft", "project": "Ashburn Expansion", "market": "Ashburn, VA", "capacity": 420, "investment": 1200, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "hyperscale"},
    {"company": "Google", "project": "Kansas City Campus", "market": "Kansas City", "capacity": 500, "investment": 1500, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "hyperscale"},
    {"company": "Amazon/AWS", "project": "Anthropic Expansion Phase 2", "market": "Virginia", "capacity": 500, "investment": 1400, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "hyperscale"},
    {"company": "Aligned", "project": "Dallas Campus Expansion", "market": "Dallas, TX", "capacity": 350, "investment": 900, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "adaptive"},
    {"company": "Aligned", "project": "Phoenix Campus Expansion", "market": "Phoenix, AZ", "capacity": 300, "investment": 750, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "adaptive"},
    {"company": "Compass", "project": "Meridian Campus", "market": "Lauderdale County, MS", "capacity": 320, "investment": 850, "delivery": "2026-Q4", "status": "construction", "preleased": True, "type": "wholesale"},
    {"company": "QTS (Blackstone)", "project": "Richmond Campus", "market": "Richmond, VA", "capacity": 300, "investment": 800, "delivery": "2026-Q2", "status": "construction", "preleased": True, "type": "hyperscale"},
    {"company": "CoreSite", "project": "DE3 Denver", "market": "Denver, CO", "capacity": 50, "investment": 130, "delivery": "2026-Q2", "status": "construction", "preleased": False, "type": "interconnection"},
    {"company": "Aligned", "project": "Pacific Northwest BESS", "market": "Hillsboro, OR", "capacity": 100, "investment": 250, "delivery": "2026-Q1", "status": "construction", "preleased": True, "type": "adaptive"},
    {"company": "Meta", "project": "Ireland AI Campus", "market": "Ireland", "capacity": 400, "investment": 1000, "delivery": "2026-Q4", "status": "construction", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Oracle/OpenAI", "project": "Stargate Midwest Site", "market": "Midwest", "capacity": 800, "investment": 2000, "delivery": "2027-Q2", "status": "announced", "preleased": True, "type": "ai-hyperscale"},
    {"company": "Microsoft", "project": "Racine Phase 2", "market": "Mount Pleasant, WI", "capacity": 500, "investment": 1200, "delivery": "2027-Q1", "status": "announced", "preleased": True, "type": "hyperscale"},
    {"company": "Google", "project": "South Carolina Campus", "market": "South Carolina", "capacity": 600, "investment": 1500, "delivery": "2027-Q4", "status": "announced", "preleased": True, "type": "hyperscale"},
    {"company": "Amazon/AWS", "project": "Ohio Expansion", "market": "Columbus, OH", "capacity": 500, "investment": 1200, "delivery": "2027-Q3", "status": "announced", "preleased": True, "type": "hyperscale"},
    {"company": "Vantage", "project": "Frontier Phase 2", "market": "Shackelford County, TX", "capacity": 500, "investment": 1500, "delivery": "2028-Q1", "status": "announced", "preleased": False, "type": "hyperscale"},
    {"company": "Aligned", "project": "Maryland Campus", "market": "Maryland", "capacity": 350, "investment": 900, "delivery": "2027-Q4", "status": "announced", "preleased": False, "type": "adaptive"},
    {"company": "Aligned", "project": "Ohio Campus", "market": "Ohio", "capacity": 300, "investment": 750, "delivery": "2027-Q3", "status": "announced", "preleased": False, "type": "adaptive"},
    {"company": "Aligned", "project": "Virginia Expansion", "market": "Northern Virginia", "capacity": 400, "investment": 1000, "delivery": "2027-Q2", "status": "announced", "preleased": False, "type": "adaptive"},
    {"company": "Digital Realty", "project": "Atlanta Expansion", "market": "Atlanta, GA", "capacity": 250, "investment": 650, "delivery": "2026-Q3", "status": "announced", "preleased": False, "type": "wholesale"},
    {"company": "Equinix", "project": "Dallas Multi-Site", "market": "Dallas, TX", "capacity": 200, "investment": 500, "delivery": "2026-Q4", "status": "announced", "preleased": False, "type": "interconnection"},
    {"company": "CleanArc", "project": "Virginia Campus Expansion", "market": "Virginia", "capacity": 300, "investment": 800, "delivery": "2027-Q2", "status": "announced", "preleased": False, "type": "wholesale"},
    {"company": "Goodman/CPP", "project": "European DC Portfolio", "market": "Europe", "capacity": 800, "investment": 2000, "delivery": "2028-Q2", "status": "announced", "preleased": False, "type": "wholesale"},
    {"company": "Nscale", "project": "US AI Data Centers", "market": "United States", "capacity": 300, "investment": 865, "delivery": "2026-Q4", "status": "announced", "preleased": False, "type": "ai-gpu"},
    {"company": "Oracle/SoftBank", "project": "Japan AI Cloud", "market": "Japan", "capacity": 300, "investment": 800, "delivery": "2027-Q3", "status": "announced", "preleased": True, "type": "ai-hyperscale"},
    {"company": "CoreWeave", "project": "New Jersey Campus", "market": "New Jersey", "capacity": 200, "investment": 500, "delivery": "2026-Q1", "status": "construction", "preleased": True, "type": "ai-gpu"},
    {"company": "CloudHQ", "project": "Ashburn VA-5", "market": "N. Virginia", "capacity": 150, "investment": 400, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "hyperscale"},
    {"company": "Switch", "project": "Atlanta Campus", "market": "Atlanta", "capacity": 180, "investment": 450, "delivery": "2026-Q2", "status": "construction", "preleased": False, "type": "hyperscale"},
    {"company": "Yondr", "project": "Chicago ORD-1", "market": "Chicago", "capacity": 150, "investment": 400, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "hyperscale"},
    {"company": "NTT", "project": "Tokyo TY-12", "market": "Tokyo", "capacity": 72, "investment": 280, "delivery": "2026-Q3", "status": "construction", "preleased": True, "type": "enterprise"},
    {"company": "Equinix", "project": "SG5 Singapore", "market": "Singapore", "capacity": 65, "investment": 250, "delivery": "2026-Q1", "status": "construction", "preleased": True, "type": "interconnection"},
]

DEALS_CACHE = BoundedCache(max_size=50, ttl=300)
DEALS_CACHE_DURATION = 300  # 5 minutes cache

@deals_bp.route('/api/deals', methods=['GET'])
@_lazy_protect_data
def get_deals():
    """Get data center deals/transactions - comprehensive database"""
    import time

    limit = request.args.get('limit', 200, type=int)
    year = request.args.get('year')
    region = request.args.get('region')
    deal_type = request.args.get('type')
    category = request.args.get('category')  # 'traditional', 'hyperscaler', 'ai', or 'all'
    buyer_filter = request.args.get('buyer', '').strip()
    seller_filter = request.args.get('seller', '').strip()
    min_value = request.args.get('min_value', 0, type=float)
    max_value = request.args.get('max_value', 0, type=float)
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()

    cache_key = f"deals_{year}_{region}_{deal_type}_{category}_{buyer_filter}_{seller_filter}_{min_value}_{max_value}_{date_from}_{date_to}"
    cached_data = DEALS_CACHE.get(cache_key)
    if cached_data is not None:
        limited = cached_data[:limit]
        return jsonify({
            'success': True,
            'transactions': limited,
            'data': limited,
            'count': len(limited),
            'total_count': len(cached_data),
            'total_value': sum((d.get('value') or 0) for d in cached_data),
            'cached': True
        })

    # Start with sample deals
    deals = SAMPLE_DEALS.copy()

    pg_url = os.environ.get('DATABASE_URL', '')
    if pg_url:
        try:
            import psycopg2
            with _pg_connection() as pg_conn:
                pg_cur = pg_conn.cursor()
                pg_cur.execute("""
                    SELECT id, date, year, buyer, seller, value, mw, type, region, market
                    FROM deals ORDER BY COALESCE(date, '1970-01-01') DESC LIMIT 200
                """)
                db_deals = []
                for row in pg_cur.fetchall():
                    buyer = row[3] or ''
                    seller = row[4] or ''
                    if buyer.lower() in ['tbd', 'unknown', 'n/a', ''] or seller.lower() in ['tbd', 'unknown', 'n/a', '']:
                        continue
                    val_m = float(row[5] or 0)
                    val_display = f"${val_m/1000:.1f}B" if val_m >= 1000 else (f"${val_m:.0f}M" if val_m > 0 else None)
                    db_deals.append({
                        'id': row[0], 'date': row[1], 'year': row[2],
                        'buyer': buyer, 'seller': seller,
                        'value': val_m,
                        'value_display': val_display,
                        'value_confirmed': val_m > 0,
                        'mw': row[6], 'type': row[7], 'region': row[8], 'market': row[9]
                    })
            existing_ids = {d['id'] for d in db_deals}
            for d in deals:
                if d['id'] not in existing_ids:
                    db_deals.append(d)
            deals = db_deals
        except Exception as e:
            logger.warning(f"Deals PG query failed, trying SQLite: {e}")
    # SQLite fallback removed — Neon PG is source of truth


    # Filter by category (group deal types)
    if category:
        if category == 'traditional':
            # Traditional M&A (what Synergy tracks)
            deals = [d for d in deals if d.get('type') in ['ma', 'equity', 'jv', 'land', 'debt']]
        elif category == 'hyperscaler':
            # Hyperscaler self-build CapEx
            deals = [d for d in deals if d.get('type') == 'capex']
        elif category == 'ai':
            # AI infrastructure contracts
            deals = [d for d in deals if d.get('type') in ['ai_contract', 'ai_infra']]

    # Filter by year
    if year:
        deals = [d for d in deals if str(d.get('year', '')) == str(year) or d.get('date', '').startswith(str(year))]

    # Filter by region
    if region and region != 'All Regions':
        deals = [d for d in deals if d.get('region') == region]

    # Filter by type
    if deal_type and deal_type != 'All Types':
        deals = [d for d in deals if d.get('type') == deal_type]

    # Filter by buyer (case-insensitive partial match)
    if buyer_filter:
        buyer_lower = buyer_filter.lower()
        deals = [d for d in deals if buyer_lower in (d.get('buyer') or '').lower()]

    # Filter by seller (case-insensitive partial match)
    if seller_filter:
        seller_lower = seller_filter.lower()
        deals = [d for d in deals if seller_lower in (d.get('seller') or '').lower()]

    # Filter by value range (values stored in millions)
    if min_value:
        # MCP sends USD, DB stores millions — normalize: if min_value > 1M assume raw USD
        min_m = min_value / 1_000_000 if min_value > 1_000_000 else min_value
        deals = [d for d in deals if (d.get('value') or 0) >= min_m]
    if max_value:
        max_m = max_value / 1_000_000 if max_value > 1_000_000 else max_value
        deals = [d for d in deals if (d.get('value') or 0) <= max_m]

    # Filter by date range
    if date_from:
        deals = [d for d in deals if (d.get('date') or '') >= date_from]
    if date_to:
        deals = [d for d in deals if (d.get('date') or '') <= date_to]

    # Sort by date descending
    deals.sort(key=lambda x: x.get('date') or '', reverse=True)

    # Calculate stats by type
    stats_by_type = {}
    for d in deals:
        dtype = d.get('type', 'unknown')
        if dtype not in stats_by_type:
            stats_by_type[dtype] = {'count': 0, 'value': 0}
        stats_by_type[dtype]['count'] += 1
        stats_by_type[dtype]['value'] += (d.get('value') or 0)

    # Calculate stats by year
    stats_by_year = {}
    for d in deals:
        yr = d.get('year', 'unknown')
        if yr not in stats_by_year:
            stats_by_year[yr] = {'count': 0, 'value': 0}
        stats_by_year[yr]['count'] += 1
        stats_by_year[yr]['value'] += (d.get('value') or 0)

    DEALS_CACHE.set(cache_key, deals)

    # Apply limit
    limited_deals = deals[:limit]

    return jsonify({
        'success': True,
        'transactions': limited_deals,
        'data': limited_deals,  # Keep for backwards compatibility
        'count': len(limited_deals),
        'total_count': len(deals),
        'total_value': sum((d.get('value') or 0) for d in deals),
        'stats_by_type': stats_by_type,
        'stats_by_year': stats_by_year,
        'deal_types': {
            'ma': 'M&A / Acquisitions',
            'equity': 'Equity Investments',
            'jv': 'Joint Ventures',
            'land': 'Land/Development',
            'debt': 'Debt Financing',
            'capex': 'Hyperscaler CapEx',
            'ai_contract': 'AI Compute Contracts',
            'ai_infra': 'AI Infrastructure'
        }
    })

@deals_bp.route('/api/v1/transactions', methods=['GET'])
def get_transactions():
    """Transactions with freemium tier.

    Unauthenticated: 3 most recent deals, basic fields only (buyer, seller, market).
    Authenticated Pro/Enterprise: full deal data as before.
    AI Wars verification keys also get Pro-tier access.
    """
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    is_authenticated = bool(api_key)

    # AI Wars verification keys get Pro-tier access
    if not is_authenticated:
        ai_wars_info = _get_ai_wars_key_info()
        if ai_wars_info:
            is_authenticated = True

    if is_authenticated:
        if _real_require_plan_ref is not None:
            @_real_require_plan_ref('pro')
            @_lazy_protect_data
            def _authed_transactions():
                return get_deals()
            return _authed_transactions()
        else:
            return jsonify({'success': False, 'error': 'tier_gating_unavailable',
                            'message': 'Authentication system is starting up. Please try again in a moment.'}), 503

    return _get_transactions_free()


def _get_transactions_free():
    """Freemium transactions -- 3 most recent deals, basic fields only. PG first, SQLite fallback."""
    FREE_LIMIT = 3
    BASIC_FIELDS = ('buyer', 'seller', 'market', 'date', 'type', 'region')

    deals = SAMPLE_DEALS.copy()
    loaded_from_db = False

    pg_url = os.environ.get('DATABASE_URL', '')
    if pg_url:
        try:
            import psycopg2
            with _pg_connection() as pg_conn:
                pg_cur = pg_conn.cursor()
                pg_cur.execute("SELECT id, date, year, buyer, seller, value, mw, type, region, market FROM deals ORDER BY COALESCE(date, '1970-01-01') DESC LIMIT 200")
                db_deals = []
                for row in pg_cur.fetchall():
                    buyer = row[3] or ''
                    seller = row[4] or ''
                    if buyer.lower() in ['tbd', 'unknown', 'n/a', ''] or seller.lower() in ['tbd', 'unknown', 'n/a', '']:
                        continue
                    db_deals.append({'id': row[0], 'date': row[1], 'year': row[2], 'buyer': buyer, 'seller': seller, 'value': row[5], 'mw': row[6], 'type': row[7], 'region': row[8], 'market': row[9]})
            existing_ids = {d['id'] for d in db_deals}
            for d in deals:
                if d['id'] not in existing_ids:
                    db_deals.append(d)
            deals = db_deals
            loaded_from_db = True
        except Exception as e:
            logger.warning(f"Free transactions PG query failed: {e}")

    # SQLite fallback removed — Neon PG is source of truth


    deals.sort(key=lambda x: x.get('date') or '', reverse=True)
    total_matching = len(deals)
    limited = deals[:FREE_LIMIT]

    basic_deals = []
    for d in limited:
        basic_deals.append({k: d.get(k) for k in BASIC_FIELDS})

    return jsonify({
        'success': True,
        'transactions': basic_deals,
        'data': basic_deals,
        'count': len(basic_deals),
        'total_matching': total_matching,
        'full_results_available': total_matching > FREE_LIMIT,
        'tier': 'free',
        'upgrade_url': 'https://dchub.cloud/pricing',
        'note': f'Free tier: showing {len(basic_deals)} of {total_matching} transactions with basic fields. Upgrade for full data including deal values, MW capacity, and detailed analytics.'
    })

# =============================================================================
# CONSTRUCTION PIPELINE API (v86)
# =============================================================================

@deals_bp.route('/api/v1/pipeline', methods=['GET'])
@_lazy_require_plan('pro')
@_lazy_protect_data
def get_pipeline():
    """Get construction pipeline data"""
    status_filter = request.args.get('status')  # 'construction', 'announced', 'all'
    _status_map = {'under_construction': 'construction', 'in_progress': 'construction', 'completed': 'operational', 'planned': 'announced'}
    if status_filter: status_filter = _status_map.get(status_filter, status_filter)
    market_filter = request.args.get('market')
    company_filter = request.args.get('company')
    quarter_filter = request.args.get('quarter')  # e.g. '2026-Q1'
    limit = request.args.get('limit', 200, type=int)

    pipeline = PIPELINE_DATA.copy()

    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""
            SELECT operator, market, capacity_mw, phase, status, announcement_date, 
                   completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE operator != 'Unknown' AND capacity_mw > 0 AND confidence_label IN ('high', 'medium')
            ORDER BY capacity_mw DESC
        """)
        seen_keys = {(p['company'].lower(), p['project'].lower()) for p in pipeline}
        for r in c.fetchall():
            operator = r[0] or 'Unknown'
            key = (operator.lower(), (r[7] or operator).lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            status_raw = (r[3] or r[4] or 'announced').lower()
            if 'construct' in status_raw or 'under' in status_raw:
                status_norm = 'construction'
            elif 'operational' in status_raw or 'complete' in status_raw:
                status_norm = 'operational'
            else:
                status_norm = 'announced'
            pipeline.append({
                'company': operator,
                'project': r[7] or f"{operator} Expansion",
                'market': r[1] or 'Multiple Markets',
                'capacity': r[2] or 0,
                'investment': 0,
                'delivery': r[6] or 'TBD',
                'status': status_norm,
                'preleased': False,
                'type': 'wholesale'
            })
        conn.close()
    except Exception as e:
        logger.debug(f"capacity_pipeline query: {e}")

    # Apply filters
    if status_filter and status_filter != 'all':
        pipeline = [p for p in pipeline if p.get('status') == status_filter]

    if market_filter:
        pipeline = [p for p in pipeline if market_filter.lower() in p.get('market', '').lower()]

    if company_filter:
        pipeline = [p for p in pipeline if company_filter.lower() in p.get('company', '').lower()]

    if quarter_filter:
        pipeline = [p for p in pipeline if p.get('delivery') == quarter_filter]

    # Sort by delivery date
    pipeline.sort(key=lambda x: x.get('delivery', 'Z'))

    # Calculate stats
    total_mw = sum((p.get('capacity') or 0) for p in pipeline)
    total_investment = sum((p.get('investment') or 0) for p in pipeline)
    preleased_count = len([p for p in pipeline if p.get('preleased')])
    preleased_pct = round((preleased_count / len(pipeline) * 100)) if pipeline else 0
    construction_count = len([p for p in pipeline if p.get('status') == 'construction'])
    announced_count = len([p for p in pipeline if p.get('status') == 'announced'])

    # Group by quarter for summary
    quarters = {}
    for p in pipeline:
        q = p.get('delivery', 'TBD')
        if q not in quarters:
            quarters[q] = {'capacity': 0, 'projects': 0, 'preleased': 0}
        quarters[q]['capacity'] += (p.get('capacity') or 0)
        quarters[q]['projects'] += 1
        if p.get('preleased'):
            quarters[q]['preleased'] += 1

    # Limit results
    limited_pipeline = pipeline[:limit]

    return jsonify({
        'success': True,
        'data': limited_pipeline,
        'pipeline': limited_pipeline,  # Alias for compatibility
        'count': len(limited_pipeline),
        'total_count': len(pipeline),
        'stats': {
            'total_mw': total_mw,
            'total_gw': round(total_mw / 1000, 1),
            'total_investment_millions': total_investment,
            'total_investment_billions': round(total_investment / 1000, 1),
            'preleased_percentage': preleased_pct,
            'construction_count': construction_count,
            'announced_count': announced_count,
            'unique_markets': len(set(p.get('market') for p in pipeline))
        },
        'by_quarter': quarters,
        'last_updated': datetime.utcnow().isoformat()
    })


@deals_bp.route('/api/v1/gas-pipelines', methods=['GET'])
@_lazy_require_plan('enterprise')
@_lazy_protect_data
def get_gas_pipelines():
    """Get natural gas pipeline infrastructure data"""
    state_filter = request.args.get('state', '').upper()
    operator_filter = request.args.get('operator', '')
    pipeline_type = request.args.get('type', '')  # Transmission, Distribution, Gathering
    limit = request.args.get('limit', 100, type=int)

    try:
        conn = _get_db()
        c = conn.cursor()

        query = "SELECT * FROM discovered_pipelines WHERE commodity = 'Natural Gas'"
        params = []

        if state_filter:
            query += " AND state = %s"
            params.append(state_filter)
        if operator_filter:
            query += " AND operator LIKE %s"
            params.append(f"%{operator_filter}%")
        if pipeline_type:
            query += " AND pipeline_type = %s"
            params.append(pipeline_type)

        query += " ORDER BY diameter_inches DESC LIMIT %s"
        params.append(limit)

        c.execute(query, params)
        rows = c.fetchall()

        pipelines = []
        for r in rows:
            pipelines.append({
                'id': r[0],
                'operator': r[1],
                'pipeline_type': r[2],
                'status': r[3],
                'diameter_inches': r[4],
                'commodity': r[5],
                'state': r[6],
                'market': r[7],
                'discovered_at': r[8],
                'source': r[10]
            })

        # Enhance with geographic coordinates
        try:
            from pipeline_coordinates import enhance_pipeline_coordinates
            pipelines = enhance_pipeline_coordinates(pipelines)
        except ImportError:
            pass

        # Get summary stats
        c.execute("SELECT COUNT(*), COUNT(DISTINCT operator), COUNT(DISTINCT state) FROM discovered_pipelines WHERE commodity = 'Natural Gas'")
        stats = c.fetchone()

        conn.close()

        return jsonify({
            'success': True,
            'pipelines': pipelines,
            'count': len(pipelines),
            'stats': {
                'total_pipelines': stats[0],
                'unique_operators': stats[1],
                'states_covered': stats[2]
            },
            'filters': {
                'state': state_filter or 'all',
                'operator': operator_filter or 'all',
                'type': pipeline_type or 'all'
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@deals_bp.route('/api/v1/deals', methods=['GET'])
@_lazy_require_plan('pro')
@_lazy_protect_data
def get_deals_v1():
    """Alias for deals endpoint - matches frontend expectations"""
    return get_deals()

# =============================================================================
# DC MARKETS API (for Analytics page)
# =============================================================================

SAMPLE_MARKETS = [
    {"id": 1, "name": "Northern Virginia", "country": "US", "region": "North America", "facilities": 275, "total_mw": 3500, "avg_pue": 1.35, "growth": 18.5, "power_cost": 65, "fiber_providers": 45},
    {"id": 2, "name": "Dallas-Fort Worth", "country": "US", "region": "North America", "facilities": 185, "total_mw": 1800, "avg_pue": 1.42, "growth": 22.3, "power_cost": 55, "fiber_providers": 32},
    {"id": 3, "name": "Phoenix", "country": "US", "region": "North America", "facilities": 95, "total_mw": 1200, "avg_pue": 1.38, "growth": 35.2, "power_cost": 52, "fiber_providers": 18},
    {"id": 4, "name": "Chicago", "country": "US", "region": "North America", "facilities": 145, "total_mw": 950, "avg_pue": 1.45, "growth": 12.1, "power_cost": 72, "fiber_providers": 38},
    {"id": 5, "name": "Silicon Valley", "country": "US", "region": "North America", "facilities": 165, "total_mw": 850, "avg_pue": 1.32, "growth": 8.5, "power_cost": 125, "fiber_providers": 52},
    {"id": 6, "name": "Frankfurt", "country": "DE", "region": "EMEA", "facilities": 120, "total_mw": 750, "avg_pue": 1.38, "growth": 15.8, "power_cost": 180, "fiber_providers": 28},
    {"id": 7, "name": "London", "country": "GB", "region": "EMEA", "facilities": 135, "total_mw": 680, "avg_pue": 1.42, "growth": 11.2, "power_cost": 165, "fiber_providers": 35},
    {"id": 8, "name": "Amsterdam", "country": "NL", "region": "EMEA", "facilities": 85, "total_mw": 520, "avg_pue": 1.35, "growth": 9.8, "power_cost": 145, "fiber_providers": 22},
    {"id": 9, "name": "Singapore", "country": "SG", "region": "APAC", "facilities": 75, "total_mw": 450, "avg_pue": 1.55, "growth": 6.2, "power_cost": 135, "fiber_providers": 18},
    {"id": 10, "name": "Tokyo", "country": "JP", "region": "APAC", "facilities": 95, "total_mw": 620, "avg_pue": 1.48, "growth": 8.9, "power_cost": 155, "fiber_providers": 25},
    {"id": 11, "name": "Sydney", "country": "AU", "region": "APAC", "facilities": 55, "total_mw": 380, "avg_pue": 1.45, "growth": 14.5, "power_cost": 95, "fiber_providers": 15},
    {"id": 12, "name": "São Paulo", "country": "BR", "region": "LATAM", "facilities": 45, "total_mw": 280, "avg_pue": 1.52, "growth": 18.2, "power_cost": 85, "fiber_providers": 12},
    {"id": 13, "name": "Atlanta", "country": "US", "region": "North America", "facilities": 78, "total_mw": 420, "avg_pue": 1.40, "growth": 16.8, "power_cost": 68, "fiber_providers": 24},
    {"id": 14, "name": "Seattle", "country": "US", "region": "North America", "facilities": 65, "total_mw": 380, "avg_pue": 1.28, "growth": 12.5, "power_cost": 48, "fiber_providers": 22},
    {"id": 15, "name": "Dublin", "country": "IE", "region": "EMEA", "facilities": 72, "total_mw": 480, "avg_pue": 1.30, "growth": 14.2, "power_cost": 125, "fiber_providers": 18},
    {"id": 16, "name": "Paris", "country": "FR", "region": "EMEA", "facilities": 58, "total_mw": 320, "avg_pue": 1.42, "growth": 10.5, "power_cost": 155, "fiber_providers": 20},
]

@deals_bp.route('/api/dc-markets', methods=['GET'])
@_lazy_require_plan('enterprise')
def get_dc_markets():
    """Get data center market data for analytics"""
    region = request.args.get('region')

    markets = SAMPLE_MARKETS.copy()

    if region and region != 'All':
        markets = [m for m in markets if m['region'] == region]

    return jsonify({
        'success': True,
        'markets': markets,
        'count': len(markets)
    })

@deals_bp.route('/api/markets', methods=['GET'])
@_lazy_require_plan('enterprise')
def get_markets():
    """Public markets endpoint - returns all tracked markets"""
    region = request.args.get('region')
    markets = SAMPLE_MARKETS.copy()
    if region and region != 'All':
        markets = [m for m in markets if m['region'] == region]
    try:
        conn = _get_db()
        c = conn.cursor()
        for m in markets:
            c.execute("SELECT COUNT(*) FROM discovered_facilities WHERE city LIKE %s OR state LIKE %s",
                      (f"%{m['name'].split('-')[0].split(',')[0].strip()}%",
                       f"%{m['name'].split('-')[0].split(',')[0].strip()}%"))
            live_count = c.fetchone()[0]
            if live_count > 0:
                m['facilities_live'] = live_count
        conn.close()
    except:
        pass
    return jsonify({
        'success': True,
        'markets': markets,
        'count': len(markets),
        'generated_at': datetime.utcnow().isoformat()
    })

@deals_bp.route('/api/pipeline', methods=['GET'])
def get_public_pipeline():
    """Public pipeline endpoint - returns construction/planning pipeline with curated + DB data"""
    projects = []
    seen_keys = set()

    for p in PIPELINE_DATA:
        key = (p['company'].lower(), p['project'].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        projects.append({
            'company': p['company'],
            'project': p['project'],
            'market': p['market'],
            'capacity_mw': p['capacity'],
            'status': p['status'],
            'delivery': p['delivery'],
            'type': p.get('type', 'wholesale'),
            'preleased': p.get('preleased', False),
        })

    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""
            SELECT id, name, provider, city, state, country, status, power_mw
            FROM discovered_facilities
            WHERE LOWER(status) IN ('under construction', 'construction', 'planning',
                                    'planned', 'announced', 'approved', 'proposed',
                                    'under_construction', 'pre-construction',
                                    'in development', 'permitting', 'permitted')
            AND power_mw > 0
            ORDER BY power_mw DESC NULLS LAST
            LIMIT 500
        """)
        for r in c.fetchall():
            provider = r[2] or 'Unknown'
            name = r[1] or f"{provider} Facility"
            key = (provider.lower(), name.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            status_raw = (r[6] or 'announced').lower().replace(' ', '_')
            if 'construct' in status_raw:
                status_norm = 'construction'
            elif 'plan' in status_raw or 'propos' in status_raw or 'permit' in status_raw:
                status_norm = 'announced'
            else:
                status_norm = 'announced'
            market = f"{r[3]}, {r[4]}" if r[3] and r[4] else (r[4] or r[5] or 'Multiple Markets')
            projects.append({
                'company': provider,
                'project': name,
                'market': market,
                'capacity_mw': r[7] or 0,
                'status': status_norm,
                'delivery': 'TBD',
                'type': 'wholesale',
                'preleased': False,
            })
        conn.close()
    except Exception as e:
        logger.debug(f"Pipeline facilities query: {e}")

    projects.sort(key=lambda x: x.get('capacity_mw', 0), reverse=True)

    total_mw = sum((p.get('capacity_mw') or 0) for p in projects)
    construction = len([p for p in projects if p.get('status') == 'construction'])
    announced = len([p for p in projects if p.get('status') == 'announced'])
    operational = len([p for p in projects if p.get('status') == 'operational'])

    by_status = []
    status_groups = {}
    for p in projects:
        s = p.get('status', 'announced')
        if s not in status_groups:
            status_groups[s] = {'count': 0, 'total_mw': 0}
        status_groups[s]['count'] += 1
        status_groups[s]['total_mw'] += (p.get('capacity_mw') or 0)
    for s, data in status_groups.items():
        by_status.append({'status': s, 'count': data['count'], 'total_mw': round(data['total_mw'], 1)})

    return jsonify({
        'success': True,
        'pipeline': projects,
        'count': len(projects),
        'total_mw': round(total_mw, 1),
        'total_gw': round(total_mw / 1000, 1),
        'stats': {
            'total_gw': round(total_mw / 1000, 1),
            'total_mw': round(total_mw, 1),
            'project_count': len(projects),
            'under_construction': construction,
            'announced': announced,
            'operational': operational,
            'pre_leased_pct': 73
        },
        'by_status': by_status,
        'generated_at': datetime.utcnow().isoformat()
    })

@deals_bp.route('/api/v1/pipeline/summary', methods=['GET'])
@_lazy_require_plan('pro')
def get_pipeline_summary():
    """Pipeline summary -- lightweight stats for the ai-pipeline frontend (requires Pro plan)"""
    total_mw = 0
    project_count = 0
    construction = 0
    announced = 0

    seen_keys = set()
    for p in PIPELINE_DATA:
        key = (p['company'].lower(), p['project'].lower())
        if key not in seen_keys:
            seen_keys.add(key)
            total_mw += (p.get('capacity') or 0)
            project_count += 1
            st = p.get('status', 'announced')
            if st == 'construction':
                construction += 1
            else:
                announced += 1

    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""
            SELECT operator, market, capacity_mw, phase, status, notes
            FROM capacity_pipeline
            WHERE operator != 'Unknown' AND capacity_mw > 0
        """)
        for r in c.fetchall():
            operator = r[0] or 'Unknown'
            key = (operator.lower(), (r[5] or operator).lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            total_mw += r[2] or 0
            project_count += 1
            st_raw = (r[3] or r[4] or 'announced').lower()
            if 'construct' in st_raw or 'under' in st_raw:
                construction += 1
            else:
                announced += 1
        conn.close()
    except Exception as e:
        logger.debug(f"Pipeline summary DB query: {e}")

    try:
        conn2 = _get_db()
        c2 = conn2.cursor()
        c2.execute("""
            SELECT provider, name, power_mw, status
            FROM discovered_facilities
            WHERE LOWER(status) IN ('under construction', 'construction', 'planning',
                                    'planned', 'announced', 'approved', 'proposed',
                                    'under_construction', 'pre-construction',
                                    'in development', 'permitting', 'permitted')
            AND power_mw > 0
            ORDER BY power_mw DESC LIMIT 500
        """)
        for r in c2.fetchall():
            provider = r[0] or 'Unknown'
            name = r[1] or f"{provider} Facility"
            key = (provider.lower(), name.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            total_mw += r[2] or 0
            project_count += 1
            st_raw = (r[3] or 'announced').lower()
            if 'construct' in st_raw:
                construction += 1
            else:
                announced += 1
        conn2.close()
    except Exception as e:
        logger.debug(f"Pipeline summary facilities query: {e}")

    return jsonify({
        'success': True,
        'total_gw': round(total_mw / 1000, 1),
        'total_mw': round(total_mw, 1),
        'project_count': project_count,
        'under_construction': construction,
        'announced': announced,
        'pre_leased_pct': 73,
        'generated_at': datetime.utcnow().isoformat()
    })

@deals_bp.route('/api/v1/analytics', methods=['GET'])
@_lazy_require_plan('pro')
@_lazy_protect_data
def get_analytics():
    """Analytics summary endpoint"""
    return jsonify({
        'success': True,
        'markets': SAMPLE_MARKETS,
        'summary': {
            'total_markets': len(SAMPLE_MARKETS),
            'total_mw': sum((m.get('total_mw') or 0) for m in SAMPLE_MARKETS)
        }
    })

# =============================================================================
# NEWS / ANNOUNCEMENTS API
# =============================================================================

_pg_news_cat_col = None

def _get_pg_news_cat_col():
    """Detect whether PG news_articles uses 'category' or 'categories'."""
    global _pg_news_cat_col
    if _pg_news_cat_col:
        return _pg_news_cat_col
    try:
        with _pg_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'news_articles' AND column_name IN ('category', 'categories')
            """)
            cols = [r[0] for r in cur.fetchall()]
            cur.close()
        if 'category' in cols:
            _pg_news_cat_col = 'category'
        elif 'categories' in cols:
            _pg_news_cat_col = 'categories'
        else:
            _pg_news_cat_col = 'category'
    except Exception:
        _pg_news_cat_col = 'category'
    return _pg_news_cat_col

def _pg_news_select():
    """Build SELECT for PG news_articles with correct category column."""
    col = _get_pg_news_cat_col()
    alias = f"{col} AS category" if col != 'category' else 'category'
    return f"SELECT id, title, summary, url, source, {alias}, published_at, image_url, is_breaking, relevance_score FROM news_articles"

def _pg_news_cat_filter():
    """Return the correct column name for category WHERE clauses."""
    return _get_pg_news_cat_col()

@deals_bp.route('/api/agent/news', methods=['GET'])
def get_agent_news():
    """Get news/announcements for news page -- requires at least a free account"""
    try:
        limit = request.args.get('limit', 50, type=int)
        category = request.args.get('category', '')
        # Map MCP tool slugs to DB category values
        category_map = {
        'deals': 'M&A',
        'construction': 'Construction',
        'policy': 'Policy',
        'technology': 'Technology',
        'sustainability': 'Sustainability',
        'earnings': 'Earnings',
        'expansion': 'Expansion',
        'ai': 'AI',
        'industry': 'Industry',
        }
        if category and category.lower() in category_map:
            category = category_map[category.lower()]
        source = request.args.get('source', '')

        try:
            from psycopg2.extras import RealDictCursor
            with _pg_connection() as pg_conn:
                pg_cur = pg_conn.cursor(cursor_factory=RealDictCursor)

                query = _pg_news_select() + " WHERE published_at IS NOT NULL AND published_at != ''"
                params = []
                if category:
                    query += f" AND {_pg_news_cat_filter()} = %s"
                    params.append(category)
                if source:
                    query += " AND source = %s"
                    params.append(source)
                query += " ORDER BY published_at DESC LIMIT %s"
                params.append(limit)

                pg_cur.execute(query, params)
                rows = pg_cur.fetchall()

                pg_cur.execute("SELECT COUNT(*) FROM news_articles")
                total = pg_cur.fetchone()['count']

            articles = [{
                'id': row['id'],
                'title': row['title'],
                'summary': row['summary'] or '',
                'url': row['url'] or '#',
                'source': row['source'] or 'DC Hub',
                'published_at': row['published_at'],
                'category': row['category'] or 'Industry News',
                'image_url': row['image_url'] or '',
                'is_breaking': bool(row['is_breaking']),
                'relevance_score': row['relevance_score'] or 0
            } for row in rows]

            return jsonify({
                'success': True,
                'articles': articles,
                'count': len(articles),
                'total': total,
                'source': 'postgresql'
            })
        except Exception as pg_err:
            logger.error(f"News PG read failed: {pg_err}")
            return jsonify({'success': False, 'error': str(pg_err), 'articles': []}), 200
    except Exception as e:
        logger.error(f"News query error: {e}")
        return jsonify({'success': False, 'error': str(e), 'articles': []}), 200

@deals_bp.route('/api/news-feed', methods=['GET'])
def get_news_feed():
    """Alias for agent news endpoint"""
    return get_agent_news()

@deals_bp.route('/api/news/live', methods=['GET'])
def get_live_news():
    """Return cached news from DB (fast) -- public endpoint, no auth required"""
    try:
        limit = request.args.get('limit', 200, type=int)
        category = request.args.get('category', '')
        source = request.args.get('source', '')

        try:
            from psycopg2.extras import RealDictCursor
            with _pg_connection() as pg_conn:
                pg_cur = pg_conn.cursor(cursor_factory=RealDictCursor)
                query = """SELECT id, title, url, source, category, summary,
                           published_at, image_url, is_breaking, relevance_score
                           FROM news_articles
                           WHERE published_at IS NOT NULL AND published_at != ''"""
                params = []
                if category and category != 'all':
                    query += " AND category = %s"
                    params.append(category)
                if source:
                    query += " AND source = %s"
                    params.append(source)
                query += " ORDER BY published_at DESC LIMIT %s"
                params.append(limit)
                pg_cur.execute(query, params)
                rows = pg_cur.fetchall()
                pg_cur.execute("SELECT COUNT(*) as cnt FROM news_articles")
                total = pg_cur.fetchone()['cnt']

            articles = []
            for r in rows:
                article = dict(r)
                for key in ['published_at', 'fetched_at']:
                    if article.get(key) and hasattr(article[key], 'isoformat'):
                        article[key] = article[key].isoformat()
                articles.append(article)

            return jsonify({
                'success': True, 'articles': articles, 'count': len(articles),
                'total': total, 'fetched_at': datetime.utcnow().isoformat(),
                'source': 'postgresql'
            })
        except Exception as pg_err:
            logger.error(f"Live news PG read failed: {pg_err}")
            return jsonify({'success': False, 'error': str(pg_err), 'articles': []}), 200
    except Exception as e:
        logger.error(f"Live news error: {e}")
        return jsonify({'success': False, 'error': str(e), 'articles': []}), 200

@deals_bp.route('/api/news/sync', methods=['POST'])
def trigger_news_sync():
    """Manually trigger news sync — writes to SQLite AND Neon announcements"""
    try:
        from auto_sync import sync_news
        from news_engine import get_latest_news, sync_to_announcements, NEWS_DB_PATH
        saved = sync_news()
        # Also sync to Neon announcements table
        neon_saved = 0
        try:
            result = get_latest_news(limit=500, hours=48, db_path=NEWS_DB_PATH)
            articles = result.get('articles', [])
            if articles:
                neon_saved = sync_to_announcements(articles)
                logger.info(f"[news/sync] Neon announcements: {neon_saved} saved")
        except Exception as ne:
            logger.warning(f"[news/sync] Neon sync failed: {ne}")
        return jsonify({
            'success': True,
            'message': f'News sync complete: {saved} new articles saved',
            'neon_saved': neon_saved,
            'synced_at': datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# AUTO-REPAIR: duplicate route '/api/v1/news' also in api_fixes.py:149 — review and remove one
@deals_bp.route('/api/v1/news', methods=['GET'])
def get_v1_news():
    """V1 alias for news endpoint"""
    return get_agent_news()

@deals_bp.route('/api/v1/announcements', methods=['GET'])
def get_announcements():
    """Get pipeline facilities - under construction, planning, announced, or approved"""
    try:
        conn = _get_db()
        c = conn.cursor()
        status_filter = request.args.get('status', '')
        market_filter = request.args.get('market', '')
        operator_filter = request.args.get('operator', '')
        limit = min(int(request.args.get('limit', 500)), 1000)

        query = """SELECT id, name, provider, city, state, country, market AS region,
                          latitude, longitude, power_mw, status, facility_type,
                          discovered_at, source, raw_data
                   FROM discovered_facilities
                   WHERE LOWER(status) IN ('under construction', 'construction', 'planning',
                                           'planned', 'announced', 'approved',
                                           'under_construction', 'pre-construction',
                                           'in development', 'proposed', 'permitted')"""
        params = []

        if status_filter:
            query += " AND status = %s"
            params.append(status_filter)

        if operator_filter:
            query += " AND provider LIKE %s"
            params.append(f"%{operator_filter}%")

        query += " ORDER BY power_mw DESC LIMIT %s"
        params.append(limit)

        c.execute(query, params)
        rows = c.fetchall()
        cols = [desc[0] for desc in c.description]

        announcements = []
        for row in rows:
            item = dict(zip(cols, row))
            raw = {}
            if item.get('raw_data'):
                try:
                    raw = json.loads(item['raw_data'])
                except:
                    pass
            item['market'] = raw.get('market', '')
            item['land_acres'] = raw.get('land_acres', None)
            item['type'] = raw.get('type', '')
            item['notes'] = raw.get('notes', '')
            item['buildings'] = raw.get('buildings', '')
            if market_filter and item['market'].lower() != market_filter.lower():
                continue
            del item['raw_data']
            announcements.append(item)

        conn.close()
        return jsonify({
            'success': True,
            'data': announcements,
            'count': len(announcements)
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'data': [],
            'count': 0
        })