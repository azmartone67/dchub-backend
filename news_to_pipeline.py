"""
DC Hub â News-to-Pipeline Extractor
Parses DC Hub news articles into structured pipeline_drafts entries.

Called by the scheduled news-digest task. Compares against existing
capacity_pipeline to detect new projects vs. updates to existing ones.

Usage:
    from news_to_pipeline import extract_pipeline_drafts
    drafts = extract_pipeline_drafts(news_articles, existing_pipeline)
"""

import re
import json
import logging
from datetime import datetime
from difflib import SequenceMatcher

log = logging.getLogger("news-to-pipeline")

# ---------------------------------------------------------------------------
# Known operators (fuzzy matching targets)
# ---------------------------------------------------------------------------
KNOWN_OPERATORS = {
    "meta": "Meta",
    "facebook": "Meta",
    "google": "Google",
    "alphabet": "Google",
    "amazon": "Amazon/AWS",
    "aws": "Amazon/AWS",
    "microsoft": "Microsoft",
    "azure": "Microsoft",
    "oracle": "Oracle",
    "openai": "Oracle/OpenAI",
    "stargate": "Oracle/OpenAI",
    "xai": "xAI",
    "anthropic": "Anthropic",
    "coreweave": "CoreWeave",
    "equinix": "Equinix",
    "digital realty": "Digital Realty",
    "dlr": "Digital Realty",
    "qts": "QTS (Blackstone)",
    "blackstone": "QTS (Blackstone)",
    "aligned": "Aligned",
    "switch": "Switch",
    "compass": "Compass Datacenters",
    "vantage": "Vantage Data Centers",
    "cloudhq": "CloudHQ",
    "yondr": "Yondr Group",
    "stack": "STACK Infrastructure",
    "ntt": "NTT",
    "vertiv": "Vertiv",
    "crusoe": "Crusoe Energy",
    "iren": "IREN",
    "coresite": "CoreSite",
    "databank": "DataBank",
    "edgecore": "EdgeCore Digital Infrastructure",
    "airtrunk": "AirTrunk",
    "nebius": "Nebius",
    "nscale": "Nscale",
    "cleanarc": "CleanArc Data Centers",
    "lancium": "Lancium",
    "applied digital": "Applied Digital",
    "serverfarm": "ServerFarm",
    "powerhouse": "PowerHouse Data Centers",
}

# ---------------------------------------------------------------------------
# Capacity extraction patterns
# ---------------------------------------------------------------------------
CAPACITY_PATTERNS = [
    # "500 MW", "1.5 GW", "7 gigawatts"
    (r"(\d+(?:\.\d+)?)\s*GW", lambda m: float(m.group(1)) * 1000),
    (r"(\d+(?:\.\d+)?)\s*gigawatt", lambda m: float(m.group(1)) * 1000),
    (r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*MW", lambda m: float(m.group(1).replace(",", ""))),
    (r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*megawatt", lambda m: float(m.group(1).replace(",", ""))),
]

# Investment patterns
INVESTMENT_PATTERNS = [
    # "$2 billion", "$500 million"
    (r"\$(\d+(?:\.\d+)?)\s*billion", lambda m: float(m.group(1)) * 1000),
    (r"\$(\d+(?:\.\d+)?)\s*million", lambda m: float(m.group(1))),
    (r"\$(\d+(?:\.\d+)?)\s*B\b", lambda m: float(m.group(1)) * 1000),
    (r"\$(\d+(?:\.\d+)?)\s*M\b", lambda m: float(m.group(1))),
]

# Location patterns â "in <City>, <State>" or "in <State>"
LOCATION_PATTERNS = [
    r"[Ii]n\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*([A-Z]{2})\b",  # "in Putnam County, WV"
    r"[Ii]n\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",  # "in Memphis, Tennessee"
    r"[Ii]n\s+([A-Z][a-z]+\s+County)\b",  # "in Putnam County"
    r"ð\s*(.+)",  # DC Hub format
    r"(?:Louisiana|Texas|Virginia|Ohio|Indiana|Georgia|Michigan|Wisconsin|Tennessee|"
    r"California|Oregon|Washington|Arizona|Nevada|North Carolina|South Carolina|"
    r"New Jersey|New York|Pennsylvania|Florida|Illinois|Iowa|Colorado|Maryland|"
    r"West Virginia|Mississippi|Kentucky|Alabama|New Mexico|Oklahoma|Utah|Arkansas|"
    r"Nebraska|Kansas|Minnesota|Missouri|Connecticut|Montana|Wyoming|Idaho|Maine|"
    r"Massachusetts|New Hampshire|Vermont|Rhode Island|Delaware|Hawaii|Alaska|"
    r"North Dakota|South Dakota)\s+(?:AI\s+)?(?:data\s+center|campus|facility)",  # "Louisiana AI facility"
]

# US state name to abbreviation
STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

# Status keywords
STATUS_KEYWORDS = {
    "announces": "announced",
    "announced": "announced",
    "plans": "announced",
    "planning": "announced",
    "proposes": "announced",
    "proposed": "announced",
    "purchases land": "announced",
    "land purchase": "announced",
    "breaks ground": "construction",
    "groundbreaking": "construction",
    "under construction": "construction",
    "construction begins": "construction",
    "building": "construction",
    "expansion": "construction",
    "opens": "operational",
    "operational": "operational",
    "launches": "operational",
    "goes live": "operational",
    "completed": "operational",
    "online": "operational",
}

# Type inference
TYPE_KEYWORDS = {
    "ai": "ai-gpu",
    "gpu": "ai-gpu",
    "training": "ai-gpu",
    "inference": "ai-gpu",
    "hyperscale": "hyperscale",
    "colocation": "interconnection",
    "interconnection": "interconnection",
    "edge": "enterprise",
    "enterprise": "enterprise",
}


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_operator(title: str) -> str | None:
    """Extract operator name from headline."""
    title_lower = title.lower()
    for key, canonical in KNOWN_OPERATORS.items():
        if key in title_lower:
            return canonical
    return None


def extract_capacity(title: str) -> float | None:
    """Extract capacity in MW from headline."""
    for pattern, converter in CAPACITY_PATTERNS:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return converter(match)
    return None


def extract_investment(title: str) -> float | None:
    """Extract investment in $M from headline."""
    for pattern, converter in INVESTMENT_PATTERNS:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return converter(match)
    return None


def extract_location(title: str) -> str | None:
    """Extract location from headline."""
    # Try structured patterns first
    for pattern in LOCATION_PATTERNS:
        match = re.search(pattern, title)
        if match:
            loc = match.group(0).replace("ð ", "").strip()
            # Remove prefixes
            loc = re.sub(r"^[Ii]n\s+", "", loc)
            return loc

    # Check for full state names in title
    title_lower = title.lower()
    for state_name, abbrev in STATE_ABBREVS.items():
        if state_name in title_lower:
            return f"{state_name.title()}, {abbrev}"

    # Check for US state abbreviations
    states = re.findall(r"\b([A-Z]{2})\b", title)
    us_states = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
                 "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
                 "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
                 "TX","UT","VT","VA","WA","WV","WI","WY"}
    for s in states:
        if s in us_states:
            return s
    return None


def extract_status(title: str) -> str:
    """Infer project status from headline."""
    title_lower = title.lower()
    for keyword, status in STATUS_KEYWORDS.items():
        if keyword in title_lower:
            return status
    return "announced"  # default


def extract_type(title: str) -> str:
    """Infer project type from headline."""
    title_lower = title.lower()
    for keyword, ptype in TYPE_KEYWORDS.items():
        if keyword in title_lower:
            return ptype
    return "hyperscale"  # default


def calculate_confidence(article: dict, operator: str, capacity: float, location: str) -> float:
    """
    Score confidence 0-1 based on how much structured data we extracted.
    Higher = more likely a real pipeline entry.
    """
    score = 0.0

    # Operator identified
    if operator:
        score += 0.25

    # Capacity found
    if capacity and capacity > 0:
        score += 0.25

    # Location found
    if location:
        score += 0.20

    # Source reliability boost
    reliable_sources = ["bloomberg", "financial times", "reuters", "data center dynamics",
                       "the register", "tom's hardware", "network world"]
    source = article.get("source", "").lower()
    if any(s in source for s in reliable_sources):
        score += 0.15

    # Category relevance boost
    category = article.get("category", "").lower()
    if category in ("m&a", "expansion", "ai"):
        score += 0.10

    # Has a concrete project name (not just generic news)
    title = article.get("title", "")
    if any(w in title.lower() for w in ["campus", "facility", "data center", "site", "phase"]):
        score += 0.05

    return min(round(score, 2), 1.0)


def find_existing_match(draft: dict, existing_pipeline: list) -> tuple[int | None, str]:
    """
    Check if this draft matches an existing pipeline entry.
    Returns (matched_id, match_type).
    """
    if not existing_pipeline:
        return None, "new"

    company = (draft.get("company") or "").lower()
    market = (draft.get("market") or "").lower()

    for entry in existing_pipeline:
        entry_company = (entry.get("company") or "").lower()
        entry_market = (entry.get("market") or "").lower()
        entry_project = (entry.get("project") or "").lower()

        # Company match
        company_match = (
            company in entry_company or
            entry_company in company or
            SequenceMatcher(None, company, entry_company).ratio() > 0.7
        )

        # Location match
        market_match = (
            market in entry_market or
            entry_market in market or
            SequenceMatcher(None, market, entry_market).ratio() > 0.6
        )

        if company_match and market_match:
            entry_id = entry.get("id")

            # Determine what kind of update
            if draft.get("capacity_mw") and draft["capacity_mw"] != entry.get("capacity_mw"):
                return entry_id, "update_capacity"
            if draft.get("status") and draft["status"] != entry.get("status"):
                return entry_id, "update_status"

            # Same entry, no meaningful change
            return entry_id, "update_status"

    return None, "new"


def is_pipeline_relevant(title: str) -> bool:
    """
    Quick filter: is this headline about a real project/facility?
    Filters out earnings reports, stock analysis, general industry commentary.
    """
    title_lower = title.lower()

    # Exclude patterns
    exclude = [
        "stock", "valuation", "buy rating", "sell rating", "earnings",
        "which.*better", "webinar", "conference", "podcast", "opinion",
        "grid.*broken", "grid constraints",  # commentary, not projects
    ]
    for pattern in exclude:
        if re.search(pattern, title_lower):
            return False

    # Include patterns (at least one should match)
    include = [
        "data center", "campus", "facility", "MW", "GW", "gigawatt",
        "megawatt", "expansion", "announces", "land purchase", "breaks ground",
        "construction", "power plant", "partnership", "fund",
    ]
    return any(kw in title_lower for kw in include)
¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ¢2ÖâWG&7FöâgVæ7Föà¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ ¦FVbWG&7E÷VÆæUöG&gG2¢æWw5ö'F6ÆW3¢Æ7E¶F7EÒÀ¢W7Fæu÷VÆæS¢Æ7E¶F7EÒÂæöæRÒæöæRÀ¢ÓâÆ7E¶F7EÓ ¢"" ¢&ö6W72æWw2'F6ÆW2æB&WGW&â7G'V7GW&VBVÆæRG&gBVçG&W2à ¢&w3 ¢æWw5ö'F6ÆW3¢Æ7Böb'F6ÆW2g&öÒD2V"vWEöæWw0¢W7Fæu÷VÆæS¢7W'&VçB66G÷VÆæRVçG&W2f÷"FVGW ¢&WGW&ç3 ¢Æ7BöbG&gBF7G2&VGFòõ5BFòö÷VÆæRöG&gG0¢"" ¢bW7Fæu÷VÆæR2æöæS ¢W7Fæu÷VÆæRÒµÐ ¢G&gG2ÒµÐ ¢f÷"'F6ÆRâæWw5ö'F6ÆW3 ¢FFÆRÒ'F6ÆRævWB'FFÆR"Â"" ¢2V6²&VÆWfæ6RfÇFW ¢bæ÷B5÷VÆæU÷&VÆWfçBFFÆR ¢ÆöræFV'Vrb%6¶æræöâ×VÆæR'F6ÆS¢·FFÆU³£c×Ò"¢6öçFçVP ¢2WG&7B7G'V7GW&VBFF¢÷W&F÷"ÒWG&7Eö÷W&F÷"FFÆR¢bæ÷B÷W&F÷# ¢ÆöræFV'Vrb$æò÷W&F÷"f÷VæBã¢·FFÆU³£c×Ò"¢6öçFçVP ¢66GÒWG&7Eö66GFFÆR¢Æö6FöâÒWG&7EöÆö6FöâFFÆR¢çfW7FÖVçBÒWG&7EöçfW7FÖVçBFFÆR¢7FGW2ÒWG&7E÷7FGW2FFÆR¢&ö¥÷GRÒWG&7E÷GRFFÆR ¢2'VÆB&ö¦V7BæÖRg&öÒvBvR¶æ÷p¢bÆö6Föã ¢&ö¦V7EöæÖRÒb'¶÷W&F÷'Ò¶Æö6FöçÒFF6VçFW" ¢VÇ6S ¢2W6R6æWBöbFRVFÆæP¢&ö¦V7EöæÖRÒFFÆU³£Ð ¢6öæfFVæ6RÒ6Æ7VÆFUö6öæfFVæ6R'F6ÆRÂ÷W&F÷"Â66GÂÆö6Föâ ¢26¶fW'Æ÷r6öæfFVæ6P¢b6öæfFVæ6RÂã3 ¢ÆöræFV'Vrb$Æ÷r6öæfFVæ6R¶6öæfFVæ6WÒÂ6¶æs¢·FFÆU³£c×Ò"¢6öçFçVP ¢G&gBÒ°¢&6ö×ç#¢÷W&F÷"À¢'&ö¦V7B#¢&ö¦V7EöæÖRÀ¢&Ö&¶WB#¢Æö6Föâ÷"%D$B"À¢&66Gö×r#¢66GÀ¢&çfW7FÖVçEöÒ#¢çfW7FÖVçBÀ¢'7FGW2#¢7FGW2À¢&FVÆfW'#¢%D$B"À¢'GR#¢&ö¥÷GRÀ¢'&VÆV6VB#¢fÇ6RÀ¢&6öæfFVæ6R#¢6öæfFVæ6RÀ¢'6÷W&6U÷FFÆR#¢FFÆRÀ¢'6÷W&6U÷W&Â#¢'F6ÆRævWB'W&Â"À¢'6÷W&6UöFFR#¢'F6ÆRævWB'V&Æ6VEöB"À¢&æ÷FW2#¢b$WFòÖWG&7FVBg&öÒ¶'F6ÆRævWBw6÷W&6RrÂwVæ¶æ÷vârÒÂ6FVv÷'¢¶'F6ÆRævWBv6FVv÷'rÂwVæ¶æ÷vârÒ"À¢Ð ¢26V6²vç7BW7FærVÆæP¢ÖF6VEöBÂÖF6÷GRÒfæEöW7FæuöÖF6G&gBÂW7Fæu÷VÆæR¢G&gE²&ÖF6VE÷VÆæUöB%ÒÒÖF6VEö@¢G&gE²&ÖF6÷GR%ÒÒÖF6÷GP ¢G&gG2æVæBG&gB¢Æörææfòb$WG&7FVBG&gC¢¶÷W&F÷'ÒÂ¶66GÒÕrÂ¶Æö6FöçÒÂ6öæc×¶6öæfFVæ6WÒÂ¶ÖF6÷GWÒ" ¢&WGW&âG&gG0  ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ¢27FæFÆöæRFW7@¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ ¦bõöæÖUõòÓÒ%õöÖåõò# ¢2FW7BvF6×ÆRVFÆæW0¢FW7Eö'F6ÆW2Ò°¢°¢'FFÆR#¢$vöövÆRææ÷Væ6W2ÆæBW&66Rf÷"FF6VçFW"âWFæÒ6÷VçG"À¢'6÷W&6R#¢%vW7Bf&væV&Æ2'&öF67Fær"À¢'V&Æ6VEöB#¢###bÓ2Ó#uC#£#£"À¢&6FVv÷'#¢$Òd"À¢ÒÀ¢°¢'FFÆR#¢$ÖWFFògVæB6WfVâæWræGW&Âv2÷vW"ÆçG2FògVVÂFF6VçFW'2(	BVçFW&w'FæW'6FòFVÆfW"rvvvGG2öb÷vW"f÷"Æ÷V6æf6ÆG"À¢'6÷W&6R#¢%FöÒw2&Gv&R"À¢'V&Æ6VEöB#¢###bÓ2Ó#C#££"À¢&6FVv÷'#¢$"À¢ÒÀ¢°¢'FFÆR#¢$Ö7&÷6ögBF¶W2W&W6FVæ6RæWBFò÷VäÂ÷&6ÆRB7'W6öRw2ÕrFW2FF6VçFW"Wç6öâ"À¢'6÷W&6R#¢%FR&Vv7FW"D2"À¢'V&Æ6VEöB#¢###bÓ2Ó#uC#£3£#"À¢&6FVv÷'#¢$Wç6öâ"À¢ÒÀ¢°¢'FFÆR#¢%v6æg&7G'V7GW&R7Fö6²Ö&R&WGFW"÷6FöæVBGW&ærVæ6W'FçGò"À¢'6÷W&6R#¢%FRÖ÷FÆWfööÂ"À¢'V&Æ6VEöB#¢###bÓ2Ó#CS£#£3r"À¢&6FVv÷'#¢$"À¢ÒÀ¢Ð ¢Æövværæ&646öæfrÆWfVÃÖÆövværäädò¢G&gG2ÒWG&7E÷VÆæUöG&gG2FW7Eö'F6ÆW2¢&çB§6öâæGV×2G&gG2ÂæFVçCÓ"ÂFVfVÇC×7G"¢&çBb%Æç¶ÆVâG&gG2ÒG&gG2WG&7FVBg&öÒ¶ÆVâFW7Eö'F6ÆW2Ò'F6ÆW2"
