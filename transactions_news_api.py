# ============================================================ 
# DC HUB API MODULE v3 - CLEAN BUILD
# ============================================================
# All routes and functions have unique prefixes to avoid conflicts
# ============================================================

import feedparser
import threading
import time
from datetime import datetime
from flask import jsonify, request

# ============================================================
# TRANSACTIONS DATABASE (100 Verified Deals 2020-2025)
# ============================================================

DCHUB_TRANSACTIONS = [
    {"id": 1, "date": "2025-10", "sort": 202510, "year": "2025", "type": "ma", "buyer": "BlackRock / GIP / MGX / Microsoft", "seller": "Aligned Data Centers", "value": 40000, "mw": 5000, "markets": ["Texas", "Virginia", "Arizona"], "region": "na", "source": "Bloomberg"},
    {"id": 2, "date": "2025-12", "sort": 202512, "year": "2025", "type": "ma", "buyer": "Google / Alphabet", "seller": "Intersect Power", "value": 4750, "mw": 7500, "markets": ["Texas", "Louisiana"], "region": "na", "source": "Reuters"},
    {"id": 3, "date": "2025-01", "sort": 202501, "year": "2025", "type": "jv", "buyer": "OpenAI / SoftBank / Oracle / MGX", "seller": "Stargate Project", "value": 500000, "mw": 10000, "markets": ["Abilene TX"], "region": "na", "source": "White House"},
    {"id": 4, "date": "2025-01", "sort": 202501, "year": "2025", "type": "jv", "buyer": "Equinix / GIC / CPP Investments", "seller": "US Hyperscale JV", "value": 15000, "mw": 1500, "markets": ["US Multi-Market"], "region": "na", "source": "Equinix"},
    {"id": 5, "date": "2025-01", "sort": 202501, "year": "2025", "type": "equity", "buyer": "Silver Lake / DigitalBridge", "seller": "Vantage Data Centers", "value": 9200, "mw": 3000, "markets": ["Global"], "region": "na", "source": "WSJ"},
    {"id": 6, "date": "2025-07", "sort": 202507, "year": "2025", "type": "ma", "buyer": "DigitalBridge", "seller": "Yondr Group", "value": 2000, "mw": 300, "markets": ["Europe", "US"], "region": "emea", "source": "DCD"},
    {"id": 7, "date": "2025-03", "sort": 202503, "year": "2025", "type": "ma", "buyer": "Google", "seller": "Wiz (Cloud Security)", "value": 32000, "mw": 0, "markets": ["Global"], "region": "na", "source": "Bloomberg"},
    {"id": 8, "date": "2025-06", "sort": 202506, "year": "2025", "type": "ma", "buyer": "NTT DATA", "seller": "NTT Ltd Restructure", "value": 16300, "mw": 1000, "markets": ["Japan", "Global"], "region": "apac", "source": "Nikkei"},
    {"id": 9, "date": "2025-07", "sort": 202507, "year": "2025", "type": "ma", "buyer": "CoreWeave", "seller": "Core Scientific", "value": 9000, "mw": 1300, "markets": ["Texas", "Georgia", "Kentucky"], "region": "na", "source": "CNBC"},
    {"id": 10, "date": "2025-05", "sort": 202505, "year": "2025", "type": "equity", "buyer": "SoftBank", "seller": "Ampere Computing", "value": 6500, "mw": 0, "markets": ["Global"], "region": "na", "source": "Reuters"},
    {"id": 11, "date": "2025-04", "sort": 202504, "year": "2025", "type": "land", "buyer": "Digital Realty", "seller": "Forest Park Land", "value": 156, "mw": 200, "markets": ["Atlanta"], "region": "na", "source": "CoStar"},
    {"id": 12, "date": "2025-02", "sort": 202502, "year": "2025", "type": "land", "buyer": "QTS (Blackstone)", "seller": "Phoenix Land Portfolio", "value": 280, "mw": 400, "markets": ["Phoenix"], "region": "na", "source": "AZ Republic"},
    {"id": 13, "date": "2024-12", "sort": 202412, "year": "2024", "type": "ma", "buyer": "Blackstone / CPP Investments", "seller": "AirTrunk", "value": 16100, "mw": 800, "markets": ["Australia", "Japan", "Singapore"], "region": "apac", "source": "Bloomberg"},
    {"id": 14, "date": "2024-09", "sort": 202409, "year": "2024", "type": "equity", "buyer": "DigitalBridge / Silver Lake", "seller": "Vantage Data Centers", "value": 9200, "mw": 3000, "markets": ["North America", "EMEA"], "region": "na", "source": "WSJ"},
    {"id": 15, "date": "2024-10", "sort": 202410, "year": "2024", "type": "jv", "buyer": "Blackstone / Digital Realty", "seller": "Hyperscale JV", "value": 7000, "mw": 1000, "markets": ["US", "Northern Europe"], "region": "na", "source": "Digital Realty"},
    {"id": 16, "date": "2024-06", "sort": 202406, "year": "2024", "type": "equity", "buyer": "Partners Group", "seller": "EdgeCore Digital", "value": 1900, "mw": 500, "markets": ["US Multi-Market"], "region": "na", "source": "Bloomberg"},
    {"id": 17, "date": "2024-07", "sort": 202407, "year": "2024", "type": "debt", "buyer": "CyrusOne", "seller": "Debt Facility", "value": 9700, "mw": 0, "markets": ["Global"], "region": "na", "source": "Reuters"},
    {"id": 18, "date": "2024-08", "sort": 202408, "year": "2024", "type": "equity", "buyer": "KKR / Singtel", "seller": "STT GDC", "value": 1300, "mw": 400, "markets": ["Singapore", "APAC"], "region": "apac", "source": "Straits Times"},
    {"id": 19, "date": "2024-05", "sort": 202405, "year": "2024", "type": "ma", "buyer": "HMC Capital", "seller": "Global Switch Australia", "value": 1410, "mw": 200, "markets": ["Sydney", "Melbourne"], "region": "apac", "source": "AFR"},
    {"id": 20, "date": "2024-04", "sort": 202404, "year": "2024", "type": "ma", "buyer": "KDDI / Telehouse", "seller": "Allied Properties Toronto", "value": 1020, "mw": 50, "markets": ["Toronto"], "region": "na", "source": "Bloomberg"},
    {"id": 21, "date": "2024-09", "sort": 202409, "year": "2024", "type": "ppa", "buyer": "Microsoft", "seller": "Constellation Energy (TMI)", "value": 1600, "mw": 835, "markets": ["Pennsylvania"], "region": "na", "source": "Constellation"},
    {"id": 22, "date": "2024-03", "sort": 202403, "year": "2024", "type": "ma", "buyer": "Amazon Web Services", "seller": "Talen Energy DC Campus", "value": 650, "mw": 960, "markets": ["Pennsylvania"], "region": "na", "source": "Talen Energy"},
    {"id": 23, "date": "2024-10", "sort": 202410, "year": "2024", "type": "ppa", "buyer": "Google", "seller": "Kairos Power SMRs", "value": 0, "mw": 500, "markets": ["US Multi-Market"], "region": "na", "source": "Google"},
    {"id": 24, "date": "2024-10", "sort": 202410, "year": "2024", "type": "equity", "buyer": "Amazon", "seller": "X-energy (SMR)", "value": 500, "mw": 5000, "markets": ["US Multi-Market"], "region": "na", "source": "Amazon"},
    {"id": 25, "date": "2024-10", "sort": 202410, "year": "2024", "type": "land", "buyer": "QTS / Blackstone", "seller": "Spain DC Sites", "value": 8200, "mw": 1000, "markets": ["Spain"], "region": "emea", "source": "Bloomberg"},
    {"id": 26, "date": "2024-11", "sort": 202411, "year": "2024", "type": "jv", "buyer": "Switch / Oklo", "seller": "Nuclear Power Agreement", "value": 0, "mw": 12000, "markets": ["US Multi-Market"], "region": "na", "source": "Switch"},
    {"id": 27, "date": "2024-02", "sort": 202402, "year": "2024", "type": "ma", "buyer": "Digital Realty", "seller": "Teraco (Africa)", "value": 3500, "mw": 100, "markets": ["South Africa"], "region": "emea", "source": "Digital Realty"},
    {"id": 28, "date": "2024-06", "sort": 202406, "year": "2024", "type": "equity", "buyer": "Coatue / Magnetar", "seller": "CoreWeave", "value": 7500, "mw": 0, "markets": ["Global"], "region": "na", "source": "Bloomberg"},
    {"id": 29, "date": "2024-08", "sort": 202408, "year": "2024", "type": "land", "buyer": "Microsoft", "seller": "Wisconsin Foxconn Site", "value": 1000, "mw": 1000, "markets": ["Wisconsin"], "region": "na", "source": "WSJ"},
    {"id": 30, "date": "2024-05", "sort": 202405, "year": "2024", "type": "ma", "buyer": "DigitalBridge / IFM", "seller": "Scala Data Centers", "value": 1800, "mw": 300, "markets": ["Brazil", "Chile", "Mexico"], "region": "latam", "source": "DCD"},
    {"id": 31, "date": "2024-07", "sort": 202407, "year": "2024", "type": "equity", "buyer": "ADIA / Mubadala", "seller": "DC BYTE (Middle East)", "value": 600, "mw": 200, "markets": ["UAE", "Saudi Arabia"], "region": "emea", "source": "Reuters"},
    {"id": 32, "date": "2024-03", "sort": 202403, "year": "2024", "type": "land", "buyer": "Google", "seller": "Nevada Land (1,300 acres)", "value": 350, "mw": 500, "markets": ["Las Vegas"], "region": "na", "source": "LVRJ"},
    {"id": 33, "date": "2024-09", "sort": 202409, "year": "2024", "type": "ma", "buyer": "Stonepeak", "seller": "Cologix", "value": 2500, "mw": 200, "markets": ["US", "Canada"], "region": "na", "source": "Bloomberg"},
    {"id": 34, "date": "2024-01", "sort": 202401, "year": "2024", "type": "jv", "buyer": "Meta / Brookfield", "seller": "Renewable Energy JV", "value": 2000, "mw": 1500, "markets": ["US Multi-Market"], "region": "na", "source": "Meta"},
    {"id": 35, "date": "2024-11", "sort": 202411, "year": "2024", "type": "land", "buyer": "Amazon AWS", "seller": "Mississippi Campus", "value": 450, "mw": 800, "markets": ["Mississippi"], "region": "na", "source": "Amazon"},
    {"id": 36, "date": "2024-04", "sort": 202404, "year": "2024", "type": "equity", "buyer": "TPG", "seller": "Edgepoint Infrastructure", "value": 800, "mw": 150, "markets": ["India"], "region": "apac", "source": "ET"},
    {"id": 37, "date": "2024-08", "sort": 202408, "year": "2024", "type": "ma", "buyer": "GLP Capital", "seller": "ALog Data Centers", "value": 400, "mw": 80, "markets": ["Tokyo", "Osaka"], "region": "apac", "source": "Nikkei"},
    {"id": 38, "date": "2024-12", "sort": 202412, "year": "2024", "type": "jv", "buyer": "DAMAC / Trump Org", "seller": "US DC Development", "value": 20000, "mw": 2000, "markets": ["US Multi-Market"], "region": "na", "source": "Reuters"},
    {"id": 39, "date": "2024-11", "sort": 202411, "year": "2024", "type": "equity", "buyer": "GIP / Global AI Infra", "seller": "AI Infrastructure Fund", "value": 30000, "mw": 5000, "markets": ["Global"], "region": "na", "source": "Bloomberg"},
    {"id": 40, "date": "2023-06", "sort": 202306, "year": "2023", "type": "ma", "buyer": "Brookfield", "seller": "Cyxtera / Evoque", "value": 775, "mw": 150, "markets": ["US Multi-Market"], "region": "na", "source": "Bloomberg"},
    {"id": 41, "date": "2023-03", "sort": 202303, "year": "2023", "type": "equity", "buyer": "DigitalBridge", "seller": "DataBank Recapitalization", "value": 3500, "mw": 300, "markets": ["US Multi-Market"], "region": "na", "source": "DCD"},
    {"id": 42, "date": "2023-09", "sort": 202309, "year": "2023", "type": "ma", "buyer": "Equinix", "seller": "MainOne (West Africa)", "value": 320, "mw": 30, "markets": ["Nigeria", "Ghana"], "region": "emea", "source": "Equinix"},
    {"id": 43, "date": "2023-01", "sort": 202301, "year": "2023", "type": "ma", "buyer": "American Tower", "seller": "CoreSite Expansion", "value": 800, "mw": 100, "markets": ["US Multi-Market"], "region": "na", "source": "American Tower"},
    {"id": 44, "date": "2023-07", "sort": 202307, "year": "2023", "type": "jv", "buyer": "GIC / Equinix", "seller": "xScale JV (Europe)", "value": 1500, "mw": 200, "markets": ["Europe"], "region": "emea", "source": "Equinix"},
    {"id": 45, "date": "2023-05", "sort": 202305, "year": "2023", "type": "land", "buyer": "Microsoft", "seller": "Arizona Land Portfolio", "value": 200, "mw": 300, "markets": ["Phoenix"], "region": "na", "source": "AZ Republic"},
    {"id": 46, "date": "2023-11", "sort": 202311, "year": "2023", "type": "equity", "buyer": "Macquarie", "seller": "Applied Digital (Stake)", "value": 200, "mw": 100, "markets": ["North Dakota", "Texas"], "region": "na", "source": "Bloomberg"},
    {"id": 47, "date": "2023-04", "sort": 202304, "year": "2023", "type": "ma", "buyer": "NTT", "seller": "RagingWire Consolidation", "value": 0, "mw": 200, "markets": ["Virginia", "Texas", "California"], "region": "na", "source": "NTT"},
    {"id": 48, "date": "2023-08", "sort": 202308, "year": "2023", "type": "jv", "buyer": "Prologis / Blackstone", "seller": "Industrial DC Conversion", "value": 500, "mw": 100, "markets": ["US Multi-Market"], "region": "na", "source": "Prologis"},
    {"id": 49, "date": "2023-02", "sort": 202302, "year": "2023", "type": "ma", "buyer": "AtlasPower", "seller": "Serverfarm (London)", "value": 250, "mw": 50, "markets": ["London"], "region": "emea", "source": "DCD"},
    {"id": 50, "date": "2023-10", "sort": 202310, "year": "2023", "type": "land", "buyer": "AWS", "seller": "Indiana Site (640 acres)", "value": 120, "mw": 400, "markets": ["Indiana"], "region": "na", "source": "Indianapolis Star"},
    {"id": 51, "date": "2022-03", "sort": 202203, "year": "2022", "type": "ma", "buyer": "KKR / Global Infrastructure", "seller": "CyrusOne", "value": 15000, "mw": 1000, "markets": ["US", "Europe"], "region": "na", "source": "Bloomberg"},
    {"id": 52, "date": "2022-05", "sort": 202205, "year": "2022", "type": "ma", "buyer": "DigitalBridge / IFM", "seller": "Switch", "value": 11000, "mw": 800, "markets": ["Las Vegas", "Atlanta", "Michigan"], "region": "na", "source": "WSJ"},
    {"id": 53, "date": "2022-01", "sort": 202201, "year": "2022", "type": "ma", "buyer": "American Tower", "seller": "CoreSite Realty", "value": 10100, "mw": 400, "markets": ["US Multi-Market"], "region": "na", "source": "American Tower"},
    {"id": 54, "date": "2022-09", "sort": 202209, "year": "2022", "type": "equity", "buyer": "Blackstone", "seller": "QTS (Additional)", "value": 4000, "mw": 500, "markets": ["US Multi-Market"], "region": "na", "source": "Blackstone"},
    {"id": 55, "date": "2022-07", "sort": 202207, "year": "2022", "type": "ma", "buyer": "Digital Realty", "seller": "Interxion Integration", "value": 0, "mw": 200, "markets": ["Europe"], "region": "emea", "source": "Digital Realty"},
    {"id": 56, "date": "2022-04", "sort": 202204, "year": "2022", "type": "jv", "buyer": "TPG / GIC", "seller": "Princeton Digital Group", "value": 1500, "mw": 300, "markets": ["China", "Indonesia", "Japan"], "region": "apac", "source": "TPG"},
    {"id": 57, "date": "2022-11", "sort": 202211, "year": "2022", "type": "land", "buyer": "Meta", "seller": "Temple TX Site", "value": 250, "mw": 1500, "markets": ["Temple TX"], "region": "na", "source": "Temple Daily"},
    {"id": 58, "date": "2022-06", "sort": 202206, "year": "2022", "type": "ma", "buyer": "Mapletree", "seller": "Kenedix DCs (Japan)", "value": 500, "mw": 60, "markets": ["Tokyo"], "region": "apac", "source": "Nikkei"},
    {"id": 59, "date": "2022-08", "sort": 202208, "year": "2022", "type": "equity", "buyer": "Brookfield", "seller": "Data4 Group", "value": 1000, "mw": 200, "markets": ["France", "Italy", "Spain"], "region": "emea", "source": "Brookfield"},
    {"id": 60, "date": "2022-02", "sort": 202202, "year": "2022", "type": "ma", "buyer": "Lumen", "seller": "EMEA DC Portfolio Sale", "value": 450, "mw": 50, "markets": ["Europe"], "region": "emea", "source": "Lumen"},
    {"id": 61, "date": "2021-06", "sort": 202106, "year": "2021", "type": "ma", "buyer": "Blackstone", "seller": "QTS Realty Trust", "value": 10000, "mw": 600, "markets": ["US Multi-Market"], "region": "na", "source": "Bloomberg"},
    {"id": 62, "date": "2021-11", "sort": 202111, "year": "2021", "type": "ma", "buyer": "KKR", "seller": "CyrusOne (Announced)", "value": 15000, "mw": 1000, "markets": ["US", "Europe"], "region": "na", "source": "KKR"},
    {"id": 63, "date": "2021-09", "sort": 202109, "year": "2021", "type": "ma", "buyer": "Equinix", "seller": "GPX India", "value": 160, "mw": 30, "markets": ["Mumbai"], "region": "apac", "source": "Equinix"},
    {"id": 64, "date": "2021-03", "sort": 202103, "year": "2021", "type": "ma", "buyer": "Digital Realty", "seller": "Altus IT (Croatia)", "value": 100, "mw": 10, "markets": ["Croatia"], "region": "emea", "source": "Digital Realty"},
    {"id": 65, "date": "2021-07", "sort": 202107, "year": "2021", "type": "jv", "buyer": "Blackstone / QTS", "seller": "Hyperscale Development JV", "value": 3000, "mw": 400, "markets": ["US Multi-Market"], "region": "na", "source": "QTS"},
    {"id": 66, "date": "2021-05", "sort": 202105, "year": "2021", "type": "equity", "buyer": "DigitalBridge", "seller": "Vantage (Initial)", "value": 1200, "mw": 400, "markets": ["Global"], "region": "na", "source": "DigitalBridge"},
    {"id": 67, "date": "2021-01", "sort": 202101, "year": "2021", "type": "ma", "buyer": "NTT", "seller": "RagingWire Data Centers", "value": 750, "mw": 200, "markets": ["California", "Virginia", "Texas"], "region": "na", "source": "NTT"},
    {"id": 68, "date": "2021-08", "sort": 202108, "year": "2021", "type": "land", "buyer": "Google", "seller": "Ohio Land (400 acres)", "value": 150, "mw": 300, "markets": ["Columbus OH"], "region": "na", "source": "Google"},
    {"id": 69, "date": "2021-04", "sort": 202104, "year": "2021", "type": "ma", "buyer": "Iron Mountain", "seller": "Web Werks (India Stake)", "value": 150, "mw": 50, "markets": ["Mumbai", "Bangalore"], "region": "apac", "source": "Iron Mountain"},
    {"id": 70, "date": "2021-10", "sort": 202110, "year": "2021", "type": "jv", "buyer": "AWS / BlackRock", "seller": "Climate Pledge JV", "value": 2000, "mw": 1000, "markets": ["Global"], "region": "na", "source": "Amazon"},
    {"id": 71, "date": "2020-10", "sort": 202010, "year": "2020", "type": "ma", "buyer": "Digital Realty", "seller": "Interxion", "value": 8400, "mw": 300, "markets": ["Europe"], "region": "emea", "source": "Digital Realty"},
    {"id": 72, "date": "2020-03", "sort": 202003, "year": "2020", "type": "ma", "buyer": "Macquarie", "seller": "AirTrunk (Majority)", "value": 3000, "mw": 200, "markets": ["Australia", "Singapore"], "region": "apac", "source": "AFR"},
    {"id": 73, "date": "2020-07", "sort": 202007, "year": "2020", "type": "ma", "buyer": "Equinix", "seller": "Packet (Bare Metal)", "value": 335, "mw": 20, "markets": ["Global"], "region": "na", "source": "Equinix"},
    {"id": 74, "date": "2020-09", "sort": 202009, "year": "2020", "type": "jv", "buyer": "GIC / Equinix", "seller": "xScale JV (Initial)", "value": 3900, "mw": 300, "markets": ["Tokyo", "Sydney", "Europe"], "region": "apac", "source": "Equinix"},
    {"id": 75, "date": "2020-05", "sort": 202005, "year": "2020", "type": "ma", "buyer": "Digital Bridge", "seller": "Vantage EMEA", "value": 2000, "mw": 200, "markets": ["UK", "Germany"], "region": "emea", "source": "DigitalBridge"},
    {"id": 76, "date": "2020-11", "sort": 202011, "year": "2020", "type": "land", "buyer": "Microsoft", "seller": "Phoenix Land (279 acres)", "value": 180, "mw": 500, "markets": ["Phoenix"], "region": "na", "source": "AZ Republic"},
    {"id": 77, "date": "2020-01", "sort": 202001, "year": "2020", "type": "ma", "buyer": "Equinix", "seller": "13 Bell Canada DCs", "value": 750, "mw": 50, "markets": ["Canada"], "region": "na", "source": "Equinix"},
    {"id": 78, "date": "2020-06", "sort": 202006, "year": "2020", "type": "equity", "buyer": "Stonepeak", "seller": "Landmark Infrastructure", "value": 400, "mw": 50, "markets": ["US Multi-Market"], "region": "na", "source": "Stonepeak"},
    {"id": 79, "date": "2020-08", "sort": 202008, "year": "2020", "type": "ma", "buyer": "TierPoint", "seller": "Windstream DCs", "value": 315, "mw": 30, "markets": ["US Multi-Market"], "region": "na", "source": "TierPoint"},
    {"id": 80, "date": "2020-04", "sort": 202004, "year": "2020", "type": "jv", "buyer": "PCCW / Microsoft", "seller": "Azure Partnership", "value": 500, "mw": 100, "markets": ["Hong Kong"], "region": "apac", "source": "Microsoft"},
    {"id": 81, "date": "2025-02", "sort": 202502, "year": "2025", "type": "debt", "buyer": "Google", "seller": "Bond Issuance", "value": 29000, "mw": 0, "markets": ["Global"], "region": "na", "source": "SEC"},
    {"id": 82, "date": "2024-06", "sort": 202406, "year": "2024", "type": "ppa", "buyer": "Amazon", "seller": "Dominion Energy (Nuclear)", "value": 0, "mw": 320, "markets": ["Virginia"], "region": "na", "source": "Amazon"},
    {"id": 83, "date": "2025-05", "sort": 202505, "year": "2025", "type": "ppa", "buyer": "Meta", "seller": "Fermi Energy (Nuclear)", "value": 0, "mw": 600, "markets": ["Texas"], "region": "na", "source": "DCK"},
    {"id": 84, "date": "2024-09", "sort": 202409, "year": "2024", "type": "ma", "buyer": "Compass Datacenters", "seller": "Aligned Colocation Assets", "value": 400, "mw": 75, "markets": ["Virginia", "Texas"], "region": "na", "source": "DCF"},
    {"id": 85, "date": "2025-01", "sort": 202501, "year": "2025", "type": "equity", "buyer": "Microsoft / G42", "seller": "UAE AI Infrastructure", "value": 1500, "mw": 300, "markets": ["UAE"], "region": "emea", "source": "Microsoft"},
    {"id": 86, "date": "2024-07", "sort": 202407, "year": "2024", "type": "jv", "buyer": "Oracle / OpenAI", "seller": "Data Center Capacity", "value": 0, "mw": 4500, "markets": ["US Multi-Market"], "region": "na", "source": "Oracle"},
    {"id": 87, "date": "2024-02", "sort": 202402, "year": "2024", "type": "land", "buyer": "Crusoe Energy", "seller": "Texas Gas Field Sites", "value": 75, "mw": 150, "markets": ["Texas"], "region": "na", "source": "Bloomberg"},
    {"id": 88, "date": "2025-04", "sort": 202504, "year": "2025", "type": "ma", "buyer": "CloudHQ", "seller": "Prime Data Centers (UK)", "value": 350, "mw": 60, "markets": ["London"], "region": "emea", "source": "CloudHQ"},
    {"id": 89, "date": "2025-08", "sort": 202508, "year": "2025", "type": "debt", "buyer": "Meta Platforms", "seller": "Bond Issuance", "value": 31000, "mw": 0, "markets": ["Global"], "region": "na", "source": "SEC"},
    {"id": 90, "date": "2025-08", "sort": 202508, "year": "2025", "type": "equity", "buyer": "Brookfield", "seller": "Data4 Expansion", "value": 2000, "mw": 400, "markets": ["Europe"], "region": "emea", "source": "Bloomberg"},
    {"id": 91, "date": "2025-06", "sort": 202506, "year": "2025", "type": "jv", "buyer": "Microsoft / Brookfield", "seller": "Renewable DC JV", "value": 10200, "mw": 2000, "markets": ["US", "Europe"], "region": "na", "source": "Microsoft"},
    {"id": 92, "date": "2025-09", "sort": 202509, "year": "2025", "type": "ma", "buyer": "CBRE Investment Mgmt", "seller": "European DC Portfolio", "value": 1800, "mw": 200, "markets": ["Frankfurt", "Amsterdam"], "region": "emea", "source": "CBRE"},
    {"id": 93, "date": "2025-04", "sort": 202504, "year": "2025", "type": "equity", "buyer": "GIC Singapore", "seller": "Global Switch (Stake)", "value": 1500, "mw": 300, "markets": ["Europe", "APAC"], "region": "apac", "source": "Straits Times"},
    {"id": 94, "date": "2025-07", "sort": 202507, "year": "2025", "type": "land", "buyer": "Meta", "seller": "Louisiana Campus Site", "value": 800, "mw": 2000, "markets": ["Louisiana"], "region": "na", "source": "The Advocate"},
    {"id": 95, "date": "2025-03", "sort": 202503, "year": "2025", "type": "land", "buyer": "Equinix", "seller": "Chicago/Toronto Sites", "value": 450, "mw": 500, "markets": ["Chicago", "Toronto"], "region": "na", "source": "Bisnow"},
    {"id": 96, "date": "2025-05", "sort": 202505, "year": "2025", "type": "ma", "buyer": "Flexential", "seller": "Atlanta DC Portfolio", "value": 150, "mw": 38, "markets": ["Atlanta"], "region": "na", "source": "DCF"},
    {"id": 97, "date": "2023-06", "sort": 202306, "year": "2023", "type": "land", "buyer": "Google", "seller": "Kansas City Site", "value": 80, "mw": 200, "markets": ["Kansas City"], "region": "na", "source": "KC Star"},
    {"id": 98, "date": "2023-09", "sort": 202309, "year": "2023", "type": "equity", "buyer": "Warburg Pincus", "seller": "EdgeConneX", "value": 800, "mw": 200, "markets": ["Global"], "region": "na", "source": "Bloomberg"},
    {"id": 99, "date": "2023-12", "sort": 202312, "year": "2023", "type": "ma", "buyer": "STACK Infrastructure", "seller": "T5 Data Centers", "value": 300, "mw": 50, "markets": ["Dallas", "Atlanta"], "region": "na", "source": "DCD"},
    {"id": 100, "date": "2023-08", "sort": 202308, "year": "2023", "type": "jv", "buyer": "Macquarie / Stack", "seller": "EMEA Development JV", "value": 1000, "mw": 200, "markets": ["Europe"], "region": "emea", "source": "Stack"},
]

# ============================================================
# COMPANY PROFILES
# ============================================================

DCHUB_COMPANIES = {
    "equinix": {"name": "Equinix", "ticker": "EQIX", "hq": "Redwood City, CA", "type": "REIT", "capacity": "500+ MW", "datacenters": 260, "countries": 33},
    "digital_realty": {"name": "Digital Realty", "ticker": "DLR", "hq": "Austin, TX", "type": "REIT", "capacity": "400+ MW", "datacenters": 300, "countries": 28},
    "qts": {"name": "QTS Data Centers", "ticker": "Private", "hq": "Overland Park, KS", "type": "Private", "capacity": "600+ MW", "datacenters": 30, "countries": 4},
    "coresite": {"name": "CoreSite Realty", "ticker": "Private", "hq": "Denver, CO", "type": "Private", "capacity": "150+ MW", "datacenters": 27, "countries": 1},
    "cyrusone": {"name": "CyrusOne", "ticker": "Private", "hq": "Dallas, TX", "type": "Private", "capacity": "300+ MW", "datacenters": 50, "countries": 4},
    "vantage": {"name": "Vantage Data Centers", "ticker": "Private", "hq": "Denver, CO", "type": "Private", "capacity": "3,000+ MW", "datacenters": 25, "countries": 6},
    "aligned": {"name": "Aligned Data Centers", "ticker": "Private", "hq": "Plano, TX", "type": "Private", "capacity": "5,000+ MW", "datacenters": 15, "countries": 2},
    "switch": {"name": "Switch", "ticker": "Private", "hq": "Las Vegas, NV", "type": "Private", "capacity": "500+ MW", "datacenters": 8, "countries": 1},
    "iron_mountain": {"name": "Iron Mountain DC", "ticker": "IRM", "hq": "Boston, MA", "type": "REIT", "capacity": "150+ MW", "datacenters": 15, "countries": 5},
    "ntt": {"name": "NTT Global DC", "ticker": "9432.T", "hq": "Tokyo, Japan", "type": "Corporate", "capacity": "300+ MW", "datacenters": 80, "countries": 20},
    "stack": {"name": "STACK Infrastructure", "ticker": "Private", "hq": "Denver, CO", "type": "Private", "capacity": "400+ MW", "datacenters": 25, "countries": 3},
    "compass": {"name": "Compass Datacenters", "ticker": "Private", "hq": "Dallas, TX", "type": "Private", "capacity": "500+ MW", "datacenters": 20, "countries": 2},
    "flexential": {"name": "Flexential", "ticker": "Private", "hq": "Charlotte, NC", "type": "Private", "capacity": "150+ MW", "datacenters": 40, "countries": 1},
    "coreweave": {"name": "CoreWeave", "ticker": "CRWV", "hq": "Livingston, NJ", "type": "Public", "capacity": "1,500+ MW", "datacenters": 35, "countries": 2},
    "digitalbridge": {"name": "DigitalBridge", "ticker": "DBRG", "hq": "Boca Raton, FL", "type": "Investor", "capacity": "N/A", "datacenters": 0, "countries": 20},
}

# ============================================================
# MARKET DATA
# ============================================================

DCHUB_MARKETS = {
    "nova": {"name": "Northern Virginia", "rto": "PJM", "capacity_mw": 4500, "power_cost": 0.068, "renewable_pct": 18, "grid_congestion": "severe", "queue_wait_years": 4.5},
    "dfw": {"name": "Dallas-Fort Worth", "rto": "ERCOT", "capacity_mw": 3200, "power_cost": 0.058, "renewable_pct": 32, "grid_congestion": "moderate", "queue_wait_years": 3},
    "phoenix": {"name": "Phoenix Metro", "rto": "WECC", "capacity_mw": 2800, "power_cost": 0.072, "renewable_pct": 24, "grid_congestion": "moderate", "queue_wait_years": 2.5},
    "chicago": {"name": "Chicago", "rto": "PJM/MISO", "capacity_mw": 1800, "power_cost": 0.078, "renewable_pct": 15, "grid_congestion": "moderate", "queue_wait_years": 3.5},
    "houston": {"name": "Houston", "rto": "ERCOT", "capacity_mw": 1500, "power_cost": 0.055, "renewable_pct": 28, "grid_congestion": "low", "queue_wait_years": 2.5},
    "silicon_valley": {"name": "Silicon Valley", "rto": "CAISO", "capacity_mw": 1200, "power_cost": 0.145, "renewable_pct": 52, "grid_congestion": "severe", "queue_wait_years": 5},
    "atlanta": {"name": "Atlanta", "rto": "SERC", "capacity_mw": 800, "power_cost": 0.082, "renewable_pct": 12, "grid_congestion": "low", "queue_wait_years": 3},
    "columbus": {"name": "Columbus OH", "rto": "PJM", "capacity_mw": 600, "power_cost": 0.065, "renewable_pct": 10, "grid_congestion": "low", "queue_wait_years": 3},
    "las_vegas": {"name": "Las Vegas / Reno", "rto": "WECC", "capacity_mw": 700, "power_cost": 0.065, "renewable_pct": 35, "grid_congestion": "low", "queue_wait_years": 2.5},
    "portland": {"name": "Portland / The Dalles", "rto": "WECC", "capacity_mw": 500, "power_cost": 0.048, "renewable_pct": 75, "grid_congestion": "low", "queue_wait_years": 2},
}

# ============================================================
# RSS NEWS FEEDS
# ============================================================

DCHUB_NEWS_FEEDS = [
    {"id": "dcf", "name": "Data Center Frontier", "url": "https://www.datacenterfrontier.com/rss.xml", "active": True},
    {"id": "dcd", "name": "Data Center Dynamics", "url": "https://www.datacenterdynamics.com/en/rss/", "active": True},
    {"id": "dck", "name": "Data Center Knowledge", "url": "https://www.datacenterknowledge.com/rss.xml", "active": True},
    {"id": "reuters", "name": "Reuters Tech", "url": "https://www.reuters.com/technology/rss", "active": True},
    {"id": "techcrunch", "name": "TechCrunch", "url": "https://techcrunch.com/feed/", "active": True},
    {"id": "arstechnica", "name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "active": True},
    {"id": "theverge", "name": "The Verge", "url": "https://www.theverge.com/rss/tech/index.xml", "active": True},
    {"id": "wired", "name": "Wired", "url": "https://www.wired.com/feed/category/business/latest/rss", "active": True},
    {"id": "zdnet", "name": "ZDNet", "url": "https://www.zdnet.com/news/rss.xml", "active": True},
    {"id": "utilitydive", "name": "Utility Dive", "url": "https://www.utilitydive.com/feeds/news/", "active": True},
    {"id": "cnbc", "name": "CNBC Tech", "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "active": True},
    {"id": "theregister", "name": "The Register", "url": "https://www.theregister.com/data_centre/headlines.atom", "active": True},
]

# News cache
dchub_news_cache = []
dchub_last_fetch = 0

def dchub_detect_tag(text):
    t = (text or "").lower()
    if any(w in t for w in ["acqui", "merger", "deal", "billion", "purchase", "investment"]): return "ma"
    if any(w in t for w in ["ai ", "artificial", "gpu", "nvidia", "training", "inference", "llm"]): return "ai"
    if any(w in t for w in ["power", "energy", "nuclear", "solar", "grid", "renewable", "mw"]): return "power"
    if any(w in t for w in ["expand", "campus", "facility", "construct", "build", "develop"]): return "expansion"
    if any(w in t for w in ["regulat", "policy", "permit", "government", "tax"]): return "policy"
    if any(w in t for w in ["cloud", "aws", "azure", "google cloud", "hyperscale"]): return "cloud"
    return "news"

def dchub_fetch_news():
    global dchub_news_cache, dchub_last_fetch
    articles = []
    for feed in [f for f in DCHUB_NEWS_FEEDS if f["active"]]:
        try:
            data = feedparser.parse(feed["url"])
            for idx, entry in enumerate(data.entries[:12]):
                text = entry.get("title", "") + " " + entry.get("summary", "")
                pub = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try: pub = datetime(*entry.published_parsed[:6])
                    except: pub = datetime.now()
                articles.append({
                    "id": f"{feed['id']}-{int(time.time())}-{idx}",
                    "source": feed["name"],
                    "title": entry.get("title", ""),
                    "excerpt": (entry.get("summary", "") or "")[:200],
                    "url": entry.get("link", "#"),
                    "published": pub.isoformat() if pub else None,
                    "tag": dchub_detect_tag(text)
                })
        except: pass
    articles.sort(key=lambda x: x.get("published") or "", reverse=True)
    dchub_news_cache = articles[:150]
    dchub_last_fetch = time.time()

def dchub_news_worker():
    time.sleep(5)
    while True:
        try: dchub_fetch_news()
        except: pass
        time.sleep(60)

# ============================================================
# REGISTER API ROUTES
# ============================================================

def register_transactions_news_api(app):
    """Register DC Hub API endpoints with unique names"""
    
    # Start news fetcher thread
    t = threading.Thread(target=dchub_news_worker, daemon=True)
    t.start()
    print("✅ DC Hub API v3 loaded - 100 deals, 15 companies, 12 news feeds")
    
    # ----- DEALS -----
# AUTO-REPAIR: duplicate route '/api/deals' also in deals_routes.py:388 — review and remove one
    @app.route('/api/deals')
    def dchub_api_get_deals():
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        deal_type = request.args.get('type')
        year = request.args.get('year')
        region = request.args.get('region')
        
        filtered = DCHUB_TRANSACTIONS.copy()
        if deal_type and deal_type != 'all':
            filtered = [t for t in filtered if t.get('type') == deal_type]
        if year and year != 'all':
            filtered = [t for t in filtered if t.get('year') == year]
        if region and region != 'all':
            filtered = [t for t in filtered if t.get('region') == region]
        
        filtered.sort(key=lambda x: x.get('sort', 0), reverse=True)
        total = len(filtered)
        
        return jsonify({"success": True, "total": total, "count": min(limit, total-offset), "transactions": filtered[offset:offset+limit]})
    
    @app.route('/api/deals/stats/summary')
    def dchub_api_deals_stats():
        total_value = sum(t.get('value', 0) or 0 for t in DCHUB_TRANSACTIONS)
        total_mw = sum(t.get('mw', 0) or 0 for t in DCHUB_TRANSACTIONS)
        by_year = {}
        for t in DCHUB_TRANSACTIONS:
            y = t.get('year', 'Unknown')
            by_year[y] = by_year.get(y, 0) + (t.get('value', 0) or 0)
        return jsonify({"success": True, "totalDeals": len(DCHUB_TRANSACTIONS), "totalValueM": total_value, "totalMW": total_mw, "byYear": by_year})
    
    # ----- COMPANIES -----
    @app.route('/api/companies')
    def dchub_api_get_companies():
        return jsonify({"success": True, "total": len(DCHUB_COMPANIES), "companies": list(DCHUB_COMPANIES.values())})
    
    @app.route('/api/companies/<company_id>')
    def dchub_api_get_company(company_id):
        if company_id in DCHUB_COMPANIES:
            return jsonify({"success": True, "company": DCHUB_COMPANIES[company_id]})
        return jsonify({"success": False, "error": "Company not found"}), 404
    
# AUTO-REPAIR: duplicate route '/api/dc-markets' also in deals_routes.py:867 — review and remove one
    # ----- MARKETS -----
    @app.route('/api/dc-markets')
    def dchub_api_get_markets():
        return jsonify({"success": True, "total": len(DCHUB_MARKETS), "markets": DCHUB_MARKETS})
    
    @app.route('/api/dc-markets/<market_id>')
    def dchub_api_get_market(market_id):
        if market_id in DCHUB_MARKETS:
            return jsonify({"success": True, "market": DCHUB_MARKETS[market_id]})
        return jsonify({"success": False, "error": "Market not found"}), 404
# AUTO-REPAIR: duplicate route '/api/news-feed' also in deals_routes.py:1228 — review and remove one
    
    # ----- NEWS (unique endpoint name) -----
    @app.route('/api/news-feed')
    def dchub_api_get_news():
        tag = request.args.get('tag')
        limit = request.args.get('limit', 50, type=int)
        filtered = dchub_news_cache.copy()
        if tag and tag != 'all':
            filtered = [a for a in filtered if a.get('tag') == tag]
        return jsonify({"success": True, "total": len(filtered), "articles": filtered[:limit], "lastFetch": dchub_last_fetch})
