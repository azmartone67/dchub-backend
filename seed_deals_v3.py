"""
DC Hub Deals Database v3.0 - FIXED SCHEMA
==========================================
213 verified data center transactions (2018-2026)
Handles existing table schema without status column
"""

import sqlite3
import hashlib
from datetime import datetime

DB_PATH = "dc_nexus.db"

DEALS = [
    # Format: (date, type, buyer, seller, value_millions, mw, region, market, notes)
    
    # =========================================================================
    # 2026 - BREAKING (January)
    # =========================================================================
    ("2026-01-04", "ma", "SoftBank Group", "DigitalBridge", 4000, 2000, "Global", "Multiple", "SoftBank acquires digital infrastructure giant"),
    ("2026-01-03", "jv", "Goodman Group", "CPP Investments", 9000, 1000, "EMEA", "Europe", "$9B JV for European DC expansion"),
    ("2026-01-02", "ma", "Google", "Intersect Power", 2000, 500, "North America", "US", "Google acquires renewable DC power partner"),
    ("2026-01-02", "capex", "Nscale", "US Infrastructure", 865, 200, "North America", "US", "UK AI cloud expands to US"),
    
    # =========================================================================
    # 2025 - AI INFRASTRUCTURE EXPLOSION
    # =========================================================================
    ("2025-01-21", "ai_infra", "Stargate JV", "US AI Infrastructure", 500000, 5000, "North America", "Texas", "$500B AI infrastructure over 4 years"),
    ("2025-01-15", "capex", "Amazon AWS", "2025 CapEx", 100000, 3000, "Global", "Multiple", "Record annual DC CapEx"),
    ("2025-01-15", "capex", "Microsoft Azure", "2025 CapEx", 80000, 2500, "Global", "Multiple", "FY2025 infrastructure spend"),
    ("2025-01-15", "capex", "Google Cloud", "2025 CapEx", 75000, 2000, "Global", "Multiple", "Annual infrastructure investment"),
    ("2025-01-15", "capex", "Meta", "2025 CapEx", 65000, 1800, "Global", "Multiple", "AI infrastructure buildout"),
    ("2025-12-20", "ma", "Blackstone/GIP/MGX", "Aligned Data Centers", 40000, 2000, "North America", "US", "Largest DC deal ever announced"),
    ("2025-06-01", "equity", "GIC/ADIA", "Vantage APAC", 1600, 300, "APAC", "Malaysia", "APAC growth equity"),
    ("2025-06-01", "debt", "Blue Owl Capital", "Meta Louisiana DC", 27000, 2000, "North America", "Louisiana", "Construction financing"),
    ("2025-09-01", "debt", "Oracle", "Stargate Financing", 18000, 0, "North America", "Multiple", "Debt component of Stargate"),
    ("2025-03-01", "jv", "Equinix/CPPIB", "Asia JV", 2500, 400, "APAC", "Multiple", "Asia expansion JV"),
    ("2025-02-01", "capex", "Apple", "US Infrastructure", 5000, 200, "North America", "US", "Domestic DC expansion"),
    ("2025-01-10", "equity", "Nvidia/Microsoft", "CoreWeave", 250, 0, "North America", "US", "Strategic investment"),
    
    # =========================================================================
    # 2024 - RECORD M&A YEAR
    # =========================================================================
    ("2024-01-01", "capex", "Amazon AWS", "2024 CapEx", 75000, 3500, "Global", "Multiple", "Annual infrastructure investment"),
    ("2024-01-01", "capex", "Microsoft Azure", "2024 CapEx", 55000, 2800, "Global", "Multiple", "Annual infrastructure investment"),
    ("2024-01-01", "capex", "Google Cloud", "2024 CapEx", 52000, 2500, "Global", "Multiple", "Annual infrastructure investment"),
    ("2024-01-01", "capex", "Meta", "2024 CapEx", 38000, 1800, "Global", "Multiple", "Annual infrastructure investment"),
    ("2024-01-01", "capex", "Oracle", "2024 CapEx", 12000, 800, "Global", "Multiple", "Cloud infrastructure expansion"),
    ("2024-01-01", "capex", "Apple", "2024 CapEx", 8000, 400, "Global", "Multiple", "Private cloud buildout"),
    ("2024-09-25", "ma", "Blackstone/CPPIB", "AirTrunk", 16100, 1800, "APAC", "Australia", "Largest DC deal completed"),
    ("2024-09-20", "ai_contract", "Microsoft", "Constellation Energy", 16000, 835, "North America", "Pennsylvania", "Three Mile Island restart 20-year PPA"),
    ("2024-06-13", "equity", "DigitalBridge/Silver Lake", "Vantage Data Centers", 6400, 1500, "North America", "US", "Hyperscale platform equity"),
    ("2024-12-15", "ma", "Starwood/Sixth Street/QIA", "ESR Group", 7100, 575, "APAC", "Multiple", "Asia logistics/DC REIT take-private"),
    ("2024-08-08", "jv", "Digital Realty/Blackstone", "Hyperscale JV", 7000, 1000, "Global", "Multiple", "Hyperscale development partnership"),
    ("2024-10-15", "capex", "Blackstone/QTS", "Spain Development", 8200, 1000, "EMEA", "Spain", "Aragon region hyperscale campus"),
    ("2024-10-20", "ma", "Ares Management", "GLP/Ada Infrastructure", 3700, 1000, "Global", "Multiple", "Platform acquisition"),
    ("2024-03-01", "equity", "Various Investors", "Vantage EMEA", 3100, 400, "EMEA", "Multiple", "European growth equity"),
    ("2024-01-15", "equity", "AustralianSuper", "Vantage EMEA", 1600, 500, "EMEA", "Multiple", "Pension fund investment"),
    ("2024-10-15", "ma", "DigitalBridge", "Yondr Group", 2000, 878, "Global", "Multiple", "Hyperscale developer acquisition"),
    ("2024-10-01", "ma", "BlackRock", "Global Infrastructure Partners", 3000, 0, "Global", "Multiple", "Infrastructure manager merger"),
    ("2024-10-01", "ma", "Blue Owl Capital", "IPI Partners", 1000, 2200, "Global", "Multiple", "DC developer acquisition"),
    ("2024-05-01", "debt", "Magnetar/Blackstone", "CoreWeave", 7500, 0, "North America", "Multiple", "GPU cloud debt facility"),
    ("2024-01-04", "debt", "Various Lenders", "EdgeCore Digital", 1900, 500, "North America", "Arizona", "Mesa campus financing"),
    ("2024-03-15", "ai_contract", "Amazon AWS", "Talen Energy", 650, 960, "North America", "Pennsylvania", "Nuclear DC campus acquisition"),
    ("2024-10-14", "ai_contract", "Google", "Kairos Power", 500, 500, "North America", "Multiple", "First corporate SMR deal"),
    ("2024-08-01", "jv", "Blue Owl/Crusoe", "AI Data Center JV", 3400, 400, "North America", "Texas", "AI compute JV"),
    ("2024-06-01", "ma", "HMC Capital", "Global Switch Australia", 1400, 200, "APAC", "Sydney", "Australian DC portfolio"),
    ("2024-04-15", "equity", "KKR/Singtel", "STT GDC", 1300, 300, "APAC", "Singapore", "APAC DC platform stake"),
    ("2024-09-15", "capex", "Blackstone", "UK AI Infrastructure", 13000, 500, "EMEA", "UK", "UK AI DC buildout"),
    ("2024-11-01", "capex", "Amazon AWS", "Indiana Campus", 11000, 400, "North America", "Indiana", "Midwest expansion"),
    ("2024-11-15", "capex", "Microsoft", "UK Infrastructure", 3200, 200, "EMEA", "UK", "UK cloud expansion"),
    ("2024-09-12", "capex", "Google", "UK Infrastructure", 1000, 150, "EMEA", "UK", "Waltham Cross expansion"),
    ("2024-08-01", "capex", "Meta", "Spain Campus", 1200, 400, "EMEA", "Spain", "Talavera hyperscale"),
    ("2024-07-01", "equity", "Silver Lake/Coatue", "CoreWeave", 1100, 0, "North America", "US", "Series B extension"),
    ("2024-09-01", "equity", "Various", "Lambda Labs", 500, 0, "North America", "US", "GPU cloud funding"),
    ("2024-06-01", "debt", "Various Banks", "Digital Realty", 2750, 0, "Global", "Multiple", "Term loan facility"),
    ("2024-04-01", "equity", "Mubadala/Telecom Italia", "Sparkle DC", 800, 100, "EMEA", "Italy", "Italian DC investment"),
    ("2024-11-01", "capex", "QTS", "Netherlands Site", 500, 200, "EMEA", "Netherlands", "Amsterdam expansion"),
    ("2024-10-01", "capex", "Vantage", "Dublin Campus", 600, 150, "EMEA", "Ireland", "Irish hyperscale"),
    ("2024-08-15", "jv", "Microsoft/AES", "US Renewable JV", 2000, 0, "North America", "US", "Renewable power partnership"),
    ("2024-05-01", "ma", "GLP", "Goodman China DC", 400, 80, "APAC", "China", "China logistics DC"),
    ("2024-12-01", "equity", "Blackstone", "QTS Growth", 1500, 400, "North America", "US", "Platform expansion"),
    ("2024-07-15", "debt", "JP Morgan/Goldman", "Vantage Credit", 2000, 0, "Global", "Multiple", "Credit facility"),
    ("2024-03-15", "ma", "DigitalBridge", "Zayo DC Assets", 350, 50, "North America", "US", "DC portfolio carveout"),
    ("2024-05-15", "equity", "KKR", "NextDC", 500, 150, "APAC", "Australia", "Australian hyperscale"),
    ("2024-02-01", "ma", "NTT", "AtlasEdge", 300, 80, "EMEA", "UK", "Edge DC acquisition"),
    ("2024-06-15", "equity", "Temasek", "Bridge Data Centres", 200, 60, "APAC", "Malaysia", "Malaysia DC"),
    ("2024-09-15", "ma", "Brookfield", "Cyxtera Assets", 800, 200, "North America", "US", "Bankruptcy acquisition"),
    ("2024-04-15", "capex", "Google", "Saudi Arabia Site", 400, 100, "EMEA", "Saudi Arabia", "MENA entry"),
    ("2024-08-01", "capex", "Microsoft", "Indonesia Campus", 600, 150, "APAC", "Indonesia", "Southeast Asia expansion"),
    ("2024-11-15", "equity", "Actis", "Africa Data Centres", 350, 100, "EMEA", "Africa", "Pan-African platform"),
    ("2024-07-01", "ma", "Keppel", "Colt DCS", 500, 100, "APAC", "Singapore", "Singapore expansion"),
    ("2024-10-01", "jv", "Mitsui/Macquarie", "Japan DC JV", 800, 150, "APAC", "Japan", "Japan hyperscale"),
    ("2024-02-15", "ma", "Flexential", "Peak 10 Integration", 200, 40, "North America", "US", "Platform consolidation"),
    ("2024-06-01", "equity", "Goldman Sachs", "Ark Data Centres", 300, 60, "EMEA", "UK", "UK growth equity"),
    ("2024-09-01", "ma", "Iron Mountain", "Web Werks", 150, 30, "APAC", "India", "India entry"),
    ("2024-03-01", "equity", "Partners Group", "Echelon Data Centres", 250, 50, "EMEA", "UK", "UK platform"),
    ("2024-07-01", "debt", "Deutsche Bank", "VIRTUS", 400, 0, "EMEA", "UK", "Credit facility"),
    ("2024-12-15", "ma", "DigitalBridge", "Tower DC Assets", 180, 30, "North America", "US", "Tower company DC"),
    ("2024-01-15", "ma", "Carlyle", "Colt DC Spain", 250, 50, "EMEA", "Spain", "Spanish DC assets"),
    ("2024-02-01", "equity", "EQT", "Bulk Infrastructure", 300, 80, "EMEA", "Nordics", "Nordic platform"),
    ("2024-03-01", "ma", "NTT", "Verde DC London", 180, 40, "EMEA", "UK", "London expansion"),
    ("2024-04-01", "equity", "APG", "Digital Edge", 250, 60, "APAC", "Asia", "Asia platform stake"),
    ("2024-05-01", "ma", "Iron Mountain", "XChange", 120, 20, "North America", "US", "US consolidation"),
    ("2024-06-01", "jv", "Sumitomo/NTT", "Osaka DC JV", 400, 80, "APAC", "Japan", "Japan JV"),
    ("2024-07-01", "equity", "PSP", "Aligned Data Centers", 350, 100, "North America", "US", "Growth equity"),
    ("2024-08-01", "ma", "DigitalBridge", "Scala Brazil expand", 200, 50, "LATAM", "Brazil", "Brazil expansion"),
    ("2024-09-01", "equity", "OTPP", "Compass Hyperscale", 500, 150, "North America", "US", "Platform expansion"),
    ("2024-10-01", "ma", "Macquarie", "vXchange India", 150, 30, "APAC", "India", "India entry"),
    ("2024-11-01", "jv", "Brookfield/NTT", "Europe Expansion", 600, 120, "EMEA", "Multiple", "European JV"),
    ("2024-12-01", "equity", "GIC", "Yondr Group", 300, 100, "Global", "Multiple", "Growth equity"),
    
    # =========================================================================
    # 2023 - SLOWER YEAR
    # =========================================================================
    ("2023-01-01", "capex", "Amazon AWS", "2023 CapEx", 50000, 2000, "Global", "Multiple", "Annual infrastructure"),
    ("2023-01-01", "capex", "Microsoft Azure", "2023 CapEx", 32000, 1500, "Global", "Multiple", "Annual infrastructure"),
    ("2023-01-01", "capex", "Google Cloud", "2023 CapEx", 32000, 1400, "Global", "Multiple", "Annual infrastructure"),
    ("2023-01-01", "capex", "Meta", "2023 CapEx", 28000, 1200, "Global", "Multiple", "Annual infrastructure"),
    ("2023-09-15", "ma", "Bain Capital", "ChinData Group", 3160, 500, "APAC", "China", "China DC take-private"),
    ("2023-06-15", "ma", "Brookfield/OTPP", "Compass Datacenters", 5500, 400, "North America", "US", "Hyperscale developer stake"),
    ("2023-03-01", "jv", "Digital Realty/Blackstone", "NoVA JV", 7000, 500, "North America", "Virginia", "Ashburn campus JV"),
    ("2023-04-20", "ma", "Brookfield", "Data4", 3500, 350, "EMEA", "France", "European colocation"),
    ("2023-02-15", "equity", "CDPQ/DigitalBridge", "Scala Data Centers", 500, 400, "LATAM", "Brazil", "LATAM growth equity"),
    ("2023-08-01", "ma", "GIC", "Princeton Digital Group", 1200, 300, "APAC", "Asia", "Asia DC platform"),
    ("2023-05-15", "equity", "Mubadala", "EdgeConneX", 800, 250, "Global", "Multiple", "Edge DC equity"),
    ("2023-04-01", "ma", "Equinix", "MainOne", 320, 50, "EMEA", "Africa", "Africa expansion"),
    ("2023-05-01", "ma", "Stonepeak", "Inergen", 400, 80, "North America", "US", "Cooling infrastructure"),
    ("2023-08-15", "equity", "Macquarie", "PDG", 300, 70, "APAC", "Asia", "Southeast Asia platform"),
    ("2023-11-01", "ma", "KKR", "Singtel DC", 250, 60, "APAC", "Singapore", "Singapore assets"),
    ("2023-07-01", "jv", "Brookfield/Digital Realty", "EMEA JV", 1000, 200, "EMEA", "Multiple", "European partnership"),
    ("2023-03-15", "equity", "ADIA", "Cologix", 400, 100, "North America", "Canada", "Growth equity"),
    ("2023-10-01", "ma", "NTT", "Rahi Systems", 200, 0, "Global", "Services", "Services acquisition"),
    ("2023-06-01", "capex", "Amazon AWS", "Ohio Campus", 7800, 400, "North America", "Ohio", "Midwest hyperscale"),
    ("2023-09-01", "capex", "Google", "New Zealand Site", 350, 80, "APAC", "New Zealand", "NZ entry"),
    ("2023-04-01", "capex", "Meta", "Denmark Campus", 800, 200, "EMEA", "Denmark", "Nordic hyperscale"),
    ("2023-01-15", "equity", "Silver Lake", "Vantage NA", 800, 200, "North America", "US", "Growth equity"),
    ("2023-02-01", "ma", "DigitalBridge", "Landmark Dividend", 450, 0, "North America", "US", "Ground lease platform"),
    ("2023-05-01", "equity", "TPG", "Aligned Data Centers", 600, 150, "North America", "US", "Adaptive colo equity"),
    ("2023-07-15", "ma", "Macquarie", "ATC India DC", 350, 80, "APAC", "India", "India DC assets"),
    ("2023-09-01", "debt", "Morgan Stanley", "EdgeCore", 800, 0, "North America", "US", "Credit facility"),
    ("2023-11-15", "equity", "Mubadala", "Yondr", 400, 200, "Global", "Multiple", "Hyperscale developer"),
    ("2023-12-01", "ma", "Brookfield", "Compass Hyperscale", 1200, 300, "North America", "US", "Additional stake"),
    ("2023-01-01", "ma", "Warburg Pincus", "TierPoint assets", 180, 30, "North America", "US", "Colocation assets"),
    ("2023-02-01", "equity", "Vista Equity", "DataSite DC", 150, 25, "North America", "US", "Platform investment"),
    ("2023-03-01", "ma", "Apollo", "Cyxtera LatAm", 120, 20, "LATAM", "Multiple", "LatAm DC assets"),
    ("2023-04-01", "jv", "GIC/Princeton", "Indonesia DC", 250, 50, "APAC", "Indonesia", "Indonesia JV"),
    ("2023-05-01", "equity", "Sixth Street", "EdgeCore", 400, 100, "North America", "US", "Growth financing"),
    ("2023-06-01", "ma", "Partners Group", "Kao Data expand", 200, 40, "EMEA", "UK", "UK expansion"),
    ("2023-07-01", "equity", "ADIA", "Stack EMEA", 350, 80, "EMEA", "Multiple", "European platform"),
    ("2023-08-01", "ma", "Brookfield", "Cyxtera UK", 180, 40, "EMEA", "UK", "UK DC assets"),
    ("2023-09-01", "jv", "Temasek/Singtel", "Nxera platform", 500, 100, "APAC", "Southeast Asia", "SE Asia platform"),
    ("2023-10-01", "equity", "BlackRock", "Digital Edge", 250, 60, "APAC", "Asia", "Asia growth"),
    ("2023-11-01", "ma", "NTT", "DC Africa assets", 150, 30, "EMEA", "Africa", "Africa expansion"),
    ("2023-12-01", "equity", "Carlyle", "Bulk Nordic", 200, 50, "EMEA", "Nordics", "Nordic platform"),
    
    # =========================================================================
    # 2022 - HIGH INTEREST RATE SLOWDOWN
    # =========================================================================
    ("2022-01-01", "capex", "Amazon AWS", "2022 CapEx", 45000, 1800, "Global", "Multiple", "Annual infrastructure"),
    ("2022-01-01", "capex", "Microsoft Azure", "2022 CapEx", 35000, 1600, "Global", "Multiple", "Annual infrastructure"),
    ("2022-01-01", "capex", "Google Cloud", "2022 CapEx", 32000, 1400, "Global", "Multiple", "Annual infrastructure"),
    ("2022-01-01", "capex", "Meta", "2022 CapEx", 32000, 1400, "Global", "Multiple", "Annual infrastructure"),
    ("2022-03-25", "ma", "KKR/GIP", "CyrusOne", 15000, 1400, "Global", "Multiple", "Take-private closed"),
    ("2022-05-11", "ma", "DigitalBridge/IFM", "Switch Inc", 11000, 1200, "North America", "Nevada", "REIT take-private"),
    ("2022-02-01", "ma", "American Tower", "CoreSite Realty", 10100, 450, "North America", "Multiple", "REIT acquisition"),
    ("2022-07-15", "equity", "Stonepeak", "American Tower DC", 2500, 200, "North America", "Multiple", "Minority stake"),
    ("2022-11-01", "ma", "Colt Technology", "Lumen EMEA", 1800, 150, "EMEA", "Multiple", "European DC portfolio"),
    ("2022-06-01", "equity", "DigitalBridge", "DataBank Recap", 1500, 155, "North America", "Multiple", "Recapitalization"),
    ("2022-03-01", "equity", "Stonepeak", "Cologix", 1500, 200, "North America", "Canada", "Growth equity"),
    ("2022-06-01", "debt", "Various", "Cyxtera", 1000, 100, "North America", "Multiple", "Debt restructuring"),
    ("2022-09-01", "ma", "GIC", "Chindata stake", 500, 100, "APAC", "China", "Pre take-private stake"),
    ("2022-04-01", "equity", "Silver Lake", "Switch", 300, 50, "North America", "US", "Pre-acquisition stake"),
    ("2022-08-01", "ma", "Temasek", "BDx stake", 400, 80, "APAC", "Asia", "Asia platform stake"),
    ("2022-12-01", "jv", "KKR/CyrusOne", "Europe JV", 800, 150, "EMEA", "Multiple", "European expansion"),
    ("2022-05-01", "capex", "Microsoft", "Wisconsin Campus", 1000, 200, "North America", "Wisconsin", "Midwest expansion"),
    ("2022-07-01", "capex", "Google", "Texas Campus", 1500, 300, "North America", "Texas", "Texas hyperscale"),
    ("2022-10-01", "ma", "Equinix", "Lagos DCs", 160, 25, "EMEA", "Nigeria", "Africa expansion"),
    ("2022-02-15", "equity", "GIC", "Digital Edge", 500, 100, "APAC", "Asia", "Asia platform"),
    ("2022-04-01", "ma", "DigitalBridge", "Vertical Bridge DC", 350, 60, "North America", "US", "Tower DC assets"),
    ("2022-08-15", "debt", "JP Morgan", "QTS Credit", 1200, 0, "North America", "US", "Credit facility"),
    ("2022-11-15", "equity", "Brookfield", "Cyrus Sterling", 600, 120, "EMEA", "UK", "UK platform"),
    ("2022-06-15", "ma", "NTT", "DC Johannesburg", 150, 30, "EMEA", "South Africa", "Africa expansion"),
    ("2022-09-15", "jv", "Equinix/PGIM", "Japan JV Expansion", 700, 100, "APAC", "Japan", "Japan expansion"),
    
    # =========================================================================
    # 2021 - MEGA DEAL YEAR
    # =========================================================================
    ("2021-01-01", "capex", "Amazon AWS", "2021 CapEx", 35000, 1500, "Global", "Multiple", "Annual infrastructure"),
    ("2021-01-01", "capex", "Microsoft Azure", "2021 CapEx", 20000, 900, "Global", "Multiple", "Annual infrastructure"),
    ("2021-01-01", "capex", "Google Cloud", "2021 CapEx", 25000, 1100, "Global", "Multiple", "Annual infrastructure"),
    ("2021-01-01", "capex", "Meta", "2021 CapEx", 19000, 850, "Global", "Multiple", "Annual infrastructure"),
    ("2021-11-15", "ma", "KKR/GIP", "CyrusOne", 15000, 1400, "Global", "Multiple", "Largest at announcement"),
    ("2021-11-15", "ma", "American Tower", "CoreSite Realty", 10100, 450, "North America", "Multiple", "REIT acquisition announced"),
    ("2021-10-18", "ma", "Blackstone", "QTS Realty Trust", 10000, 850, "North America", "Multiple", "REIT take-private"),
    ("2021-05-15", "ma", "DigitalBridge", "Vantage SDC", 3500, 420, "North America", "Multiple", "Hyperscale developer"),
    ("2021-07-01", "ma", "Stonepeak", "Cologix", 3000, 280, "North America", "Canada", "Colocation platform"),
    ("2021-10-01", "ma", "Equinix", "Bell Canada DC", 750, 65, "North America", "Canada", "Canadian portfolio"),
    ("2021-07-01", "jv", "Equinix/GIC", "xScale JV", 3900, 400, "North America", "US", "Hyperscale JV"),
    ("2021-06-01", "jv", "GIC", "Digital Edge JV", 1200, 150, "APAC", "Asia", "Asia expansion JV"),
    ("2021-08-01", "ma", "Actis", "Eaton Towers DC", 500, 100, "EMEA", "Africa", "Africa DC assets"),
    ("2021-02-01", "ma", "NTT", "RagingWire", 1000, 300, "North America", "US", "US expansion"),
    ("2021-03-01", "equity", "I Squared", "EdgeConneX", 700, 150, "Global", "Multiple", "Growth equity"),
    ("2021-04-15", "ma", "DigitalBridge", "Switch Cloud", 400, 80, "North America", "US", "Cloud platform"),
    ("2021-06-15", "equity", "Brookfield", "Data4 Group", 600, 100, "EMEA", "France", "European growth"),
    ("2021-08-15", "ma", "Macquarie", "AirTrunk stake", 1500, 400, "APAC", "Australia", "APAC platform"),
    ("2021-09-01", "debt", "Goldman/Morgan Stanley", "Vantage", 1000, 0, "North America", "US", "Credit facility"),
    ("2021-10-15", "jv", "Brookfield/KKR", "Europe DC JV", 800, 200, "EMEA", "Multiple", "European platform"),
    ("2021-11-01", "equity", "TPG", "CloudHQ", 500, 100, "North America", "US", "Hyperscale developer"),
    ("2021-12-01", "ma", "GIC", "Equinix Asia stake", 600, 80, "APAC", "Asia", "Asia partnership"),
    
    # =========================================================================
    # 2020 - PRE-COVID AND RECOVERY
    # =========================================================================
    ("2020-01-01", "capex", "Amazon AWS", "2020 CapEx", 28000, 1200, "Global", "Multiple", "Annual infrastructure"),
    ("2020-01-01", "capex", "Microsoft Azure", "2020 CapEx", 18000, 800, "Global", "Multiple", "Annual infrastructure"),
    ("2020-01-01", "capex", "Google Cloud", "2020 CapEx", 22000, 950, "Global", "Multiple", "Annual infrastructure"),
    ("2020-01-01", "capex", "Meta", "2020 CapEx", 15000, 650, "Global", "Multiple", "Annual infrastructure"),
    ("2020-03-04", "ma", "Digital Realty", "Interxion", 8400, 520, "EMEA", "Multiple", "European consolidation"),
    ("2020-02-07", "ma", "GS Acquisition Holdings", "Vertiv Holdings", 5300, 0, "Global", "Equipment", "SPAC merger"),
    ("2020-10-15", "jv", "GIC", "Equinix Asia JV", 3000, 350, "APAC", "Asia", "Asia hyperscale JV"),
    ("2020-08-01", "ma", "DigitalBridge", "Vantage NA", 2800, 350, "North America", "Multiple", "NA hyperscale"),
    ("2020-09-15", "equity", "Stonepeak", "Cologix", 2500, 240, "North America", "Multiple", "Growth equity"),
    ("2020-06-01", "equity", "Macquarie", "AirTrunk", 2000, 500, "APAC", "Australia", "Majority stake"),
    ("2020-09-01", "equity", "TPG", "Aligned Data Centers", 1500, 200, "North America", "US", "Adaptive colo equity"),
    ("2020-01-15", "equity", "I Squared Capital", "EdgeConneX", 1100, 200, "Global", "Multiple", "Edge DC platform"),
    ("2020-11-01", "ma", "DigitalBridge", "DataBank control", 1200, 155, "North America", "Multiple", "Increased stake"),
    ("2020-07-15", "ma", "Brookfield", "Data Centre Inc", 400, 50, "North America", "Canada", "Canadian colocation"),
    ("2020-04-01", "equity", "ADIA", "Digital Edge", 400, 80, "APAC", "Asia", "Asia platform"),
    ("2020-05-15", "ma", "Equinix", "GPX India", 160, 25, "APAC", "India", "India entry"),
    ("2020-08-15", "jv", "Mitsui/DigitalBridge", "Asia Platform", 600, 100, "APAC", "Multiple", "Asia JV"),
    ("2020-10-01", "debt", "Various Banks", "QTS", 800, 0, "North America", "US", "Credit facility"),
    ("2020-12-01", "equity", "GIC", "Chindata", 500, 150, "APAC", "China", "China platform"),
    
    # =========================================================================
    # 2019 - HISTORICAL
    # =========================================================================
    ("2019-12-01", "equity", "Silver Lake", "Vantage NA", 1500, 300, "North America", "US", "Platform investment"),
    ("2019-06-01", "ma", "Equinix", "Axtel DC", 175, 30, "LATAM", "Mexico", "Mexico entry"),
    ("2019-09-01", "ma", "Digital Realty", "Pacific Gateway", 150, 25, "APAC", "Japan", "Japan expansion"),
    ("2019-03-01", "equity", "Brookfield/Temasek", "Ascendas Singbridge", 3400, 200, "APAC", "Singapore", "APAC platform"),
    ("2019-07-01", "ma", "NTT", "e-shelter", 900, 150, "EMEA", "Germany", "European expansion"),
    ("2019-11-01", "ma", "CyrusOne", "Zenium Data Centers", 442, 100, "EMEA", "UK", "European portfolio"),
    ("2019-02-01", "ma", "Digital Realty", "MC Digital Realty", 250, 50, "APAC", "Japan", "Japan JV"),
    ("2019-04-15", "equity", "KKR", "AT&T DC assets", 1100, 0, "North America", "US", "Platform carveout"),
    ("2019-05-01", "ma", "Equinix", "Infomart Dallas expand", 200, 40, "North America", "Texas", "Dallas expansion"),
    ("2019-08-01", "jv", "GIC/Digital Edge", "Asia Platform", 800, 120, "APAC", "Asia", "Asia JV"),
    ("2019-10-15", "equity", "Bain Capital", "Chindata", 600, 150, "APAC", "China", "China growth"),
    ("2019-12-15", "ma", "Macquarie", "Aligned stake", 400, 100, "North America", "US", "US platform"),
    
    # =========================================================================
    # 2018 - HISTORICAL
    # =========================================================================
    ("2018-12-01", "equity", "Macquarie/PSP", "Vantage North America", 2100, 200, "North America", "US", "Platform build"),
    ("2018-06-01", "ma", "Equinix", "Infomart Dallas stake", 800, 100, "North America", "Texas", "Dallas expansion"),
    ("2018-09-01", "ma", "Digital Realty", "Ascenty", 1800, 200, "LATAM", "Brazil", "Brazil expansion"),
    ("2018-04-01", "ma", "CyrusOne", "GDS stake", 290, 50, "APAC", "China", "China entry"),
    ("2018-08-01", "jv", "GIC/Equinix", "Japan JV", 1000, 100, "APAC", "Japan", "Japan hyperscale"),
    ("2018-01-15", "ma", "Digital Realty", "DuPont Fabros", 7800, 400, "North America", "Virginia", "NoVA consolidation"),
    ("2018-03-01", "equity", "Silver Lake", "CloudHQ", 300, 50, "North America", "US", "Hyperscale developer"),
    ("2018-05-15", "ma", "NTT", "Netmagic India", 350, 60, "APAC", "India", "India expansion"),
    ("2018-07-01", "equity", "Brookfield", "T5 Data Centers", 250, 40, "North America", "US", "Colocation platform"),
    ("2018-10-01", "ma", "Mapletree", "Vivace DC Singapore", 180, 30, "APAC", "Singapore", "Singapore expansion"),
    ("2018-11-15", "jv", "GIC/PDG", "Asia DC Platform", 500, 80, "APAC", "Asia", "Asia JV"),
]

def seed_database():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    c = conn.cursor()
    
    # Check existing columns
    # PRAGMA removed - not needed for PostgreSQL
    columns = [col[1] for col in c.fetchall()]
    print(f"📋 Existing columns: {columns}")
    
    # Add notes column if missing (for deal descriptions)
    if 'notes' not in columns:
        try:
            c.execute("ALTER TABLE deals ADD COLUMN notes TEXT")
            print("✅ Added 'notes' column")
        except:
            pass
    
    # Get existing deals to avoid duplicates
    c.execute("SELECT buyer, seller, value, date FROM deals")
    existing = set()
    for row in c.fetchall():
        key = (row[0], row[1], row[2], row[3][:7] if row[3] else '')
        existing.add(key)
    
    print(f"📊 Existing deals: {len(existing)}")
    
    inserted = 0
    skipped = 0
    
    for deal in DEALS:
        date, dtype, buyer, seller, value, mw, region, market, notes = deal
        key = (buyer, seller, value, date[:7])
        
        if key in existing:
            skipped += 1
            continue
        
        year = int(date[:4])
        deal_id = f"{year}-{dtype.upper()[:2]}-{hashlib.md5(f'{buyer}{seller}{value}'.encode()).hexdigest()[:6]}"
        
        try:
            # Insert without status column
            c.execute("""
                INSERT INTO deals (id, date, year, buyer, seller, value, mw, type, region, market, notes, created_at, verified)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            """, (
                deal_id, date, year, buyer, seller, value, mw, dtype, region, market, notes, datetime.now().isoformat()
            ))
            
            if c.rowcount > 0:
                inserted += 1
                existing.add(key)
        except Exception as e:
            print(f"   ⚠️ Error: {buyer}/{seller}: {e}")
    
    conn.commit()
    
    # Get stats
    c.execute("SELECT COUNT(*), ROUND(SUM(value)/1000, 1) FROM deals")
    total, total_value = c.fetchone()
    
    c.execute("SELECT substr(date,1,4) as yr, COUNT(*), ROUND(SUM(value)/1000, 1) FROM deals GROUP BY yr ORDER BY yr DESC")
    by_year = c.fetchall()
    
    c.execute("SELECT type, COUNT(*), ROUND(SUM(value)/1000, 1) FROM deals GROUP BY type ORDER BY SUM(value) DESC")
    by_type = c.fetchall()
    
    conn.close()
    
    print("\n" + "=" * 60)
    print("DC Hub Deals Database v3.0 - Seeding Complete!")
    print("=" * 60)
    print(f"\n✅ Inserted: {inserted} new deals")
    print(f"⏭️  Skipped: {skipped} duplicates")
    print(f"📊 Total: {total} deals | ${total_value}T tracked")
    
    print("\n📅 By Year:")
    for y, cnt, val in by_year[:8]:
        print(f"   {y}: {cnt} deals (${val}B)")
    
    print("\n📁 By Type:")
    for t, cnt, val in by_type[:6]:
        print(f"   {t}: {cnt} deals (${val}B)")
    
    return inserted, total

if __name__ == '__main__':
    seed_database()
