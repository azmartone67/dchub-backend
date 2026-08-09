"""News headlines published as facilities (2026-08-09).

Three news paths used the article title — or an NER span cut out of it — as
the facility `name`, so headlines became live indexable /facilities/<slug>
pages with a self-canonical and a sitemap entry:

    /facilities/stack-breaks-ground-on-second-tokyo-data-center-900992d1
    /facilities/12-billion-data-center-breaks-ground-in-cheyenne-…-oil-city-news-…
    /facilities/copilot-07a85c97      <title>Copilot — US Data Center | DC Hub
    /facilities/ferc-ferc-9e0a2b63    <title>FERC — US Data Center | DC Hub

Fixes are ingestion-rejection / robots-meta / sitemap-emission ONLY. Slugs
are FROZEN — no test here may assert a slug change.

★ The load-bearing test in this file is
  test_predicate_spares_real_live_facility_names. Every name in it is a REAL
  row from the live corpus that an earlier, wider version of the predicate
  flagged. A false ACCEPT costs one junk page; a false REJECT de-indexes a
  real facility that can never be renamed back.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.facility_name_sanity import (  # noqa: E402
    evidence_reject_reason,
    facility_reject_reason,
    headline_reject_reason,
)

MAIN_SRC = (ROOT / "main.py").read_text()
NER_SRC = (ROOT / "routes" / "news_entity_extraction.py").read_text()


# ── 1. the measured junk, by family ──────────────────────────────────────

MEASURED_JUNK = [
    # the headline family (source news_extraction / news_pipeline)
    "Stack breaks ground on second Tokyo data center",
    "$1.2 billion data center breaks ground in Cheyenne, promises "
    "water-free operations - Oil City News",
    "Digital Bridge-owned AIMS acquires 10 acres of land in Malaysia, "
    "says it will develop 200MW data cen",
    "Middlesex Township supervisors approve breaking ground on $15B "
    "data center project - fox43.com",
    "Amazon to invest $12 billion in first data center campuses in "
    "Louisiana - About Amazon",
    "Xcel Energy to power new Google data center in Minnesota - "
    "Xcel Energy Newsroom",
    "Warehouse in Provo, Utah, to be replaced with data center",
    "Commercial data centers emerge as targets in modern warfare after "
    "drones hit 3 AWS facilities - DefenseScoop",
    # market-report titles
    "Data Center Market Size, Share, And Growth Report [2034] - "
    "Fortune Business Insights",
    "Netherlands Data Center Colocation Market Supply &amp; Demand - "
    "GlobeNewswire",
    "New Data Center Developments: January 2026 - Data Center Knowledge",
    # the 2026-05-04 NER batch (source dchub_pipeline)
    "Barcelona to build",
    "Secures Landmark MW AI",
    "Crusoe Expands Abilene Abilene AI",
    "Digital Realty Acquires Malaysia",
    "Khazna Secures Uptime Tier Unknown",
    "Ormat Signs Signs",
    "Google Taps Earth in",
    "Bahrain reveals qualified",
    "Meta Strikes to Power",
    # the '-unknown-<hash8>' family, keyed on the NAME this time
    "Meta Unknown",
    "Lithuania Unknown",
    "Energy Secretary Unknown",
    "Equinor Expands Brazil Renewables With Unknown",
    # scraped nav/landing titles that became facilities
    "Home | Data Center Frontier",
    "Home of Data Centre News | Data Centre Magazine",
    # news_ner span
    "State Pauses Projects Over",
]


@pytest.mark.parametrize("name", MEASURED_JUNK)
def test_predicate_rejects_measured_junk(name):
    assert headline_reject_reason(name), f"not caught: {name!r}"


# ── 2. ★ the anti-regression half ────────────────────────────────────────

REAL_LIVE_NAMES = [
    # each of these tripped an earlier, wider rule — see the calibration
    # notes in util/facility_name_sanity.py
    "ATMAN Data Center Warsaw-2 (WAW-2, Konstruktorska 5)",       # comma rule
    "Equinix TR7 - Toronto, Brampton (formerly Q9 Brampton 4/5)",  # comma rule
    "Ark Data Centres - Spring Park - SQ17, P1, P2, P3, P4, P5",   # comma rule
    "Státní pokladna Centrum sdílených služeb, s. p.",             # comma rule
    "TCC Technology Data Center - Bangkok,Thailand (TCC ETDC , TCCtech)",
    "PIX Eyes Nwhere",                                             # verb 'eyes'
    "Google Intersect Power Sites (Acquired)",                     # 'acquired'
    "CDC3 - 800 E Business Center Dr",                             # 'business'
    "Colocation Leipzig Mitte (ZNK) - PŸUR Business",              # 'business'
    "Datacenter Berlin Mahlsdorf (RZB) - PŸUR Business",           # 'business'
    # unicode names the ASCII tokenizer shredded into 15 length-rule tokens
    "Belügyminisztérium Nyilvántartások Vezetéséért Felelős Helyettes "
    "Államtitkárság",
    "政府數據中心大樓 Government Data Centre Complex",
    # long but real: no function words, so the sentence-length rule sits out
    "Digital Realty Av 2 50 Quadra G1 Distrito Industrial Benedito "
    "Storani Bldg 2",
    # ordinary registry names
    "Equinix TY3 - Tokyo",
    "Telehouse – TOKYO Otemachi (KDDI Otemachi)",
    "DataBank Minneapolis (MSP1)",
    "Vantage Berlin II",
    "AT TOKYO (CC1/CC2)",
    "Digital Edge TYO2 (ComSpace 1)",
    "Amazon Web Services  CMH50",
    "Sabey Intergate.Manhattan",
    "nLighten Frankfurt",
    "Colt DCS Inzai 3",
    # 'unknown' elsewhere in the name is NOT the junk shape
    "Acme Unknown Harbor Campus",
]


@pytest.mark.parametrize("name", REAL_LIVE_NAMES)
def test_predicate_spares_real_live_facility_names(name):
    reason = headline_reject_reason(name)
    assert reason is None, f"real facility would be de-indexed: {name!r} ({reason})"


# ── 3. the evidence prong is INGEST-ONLY ─────────────────────────────────

def test_evidence_gate_rejects_bare_ner_spans():
    # the shape _promote_candidates built for Copilot / FERC / GitHub
    for entity in ("Copilot", "FERC", "GitHub", "Waymo", "Texas Batch Zero"):
        fac = {"name": entity, "provider": entity, "city": None, "state": None,
               "country": "US", "latitude": None, "longitude": None,
               "power_mw": None, "sqft": None, "acreage": None,
               "investment_usd": None}
        assert evidence_reject_reason(fac) == "no-location-evidence"
        assert facility_reject_reason(fac) == "no-location-evidence"
        # …but the name alone must NOT de-index them, or the same rule would
        # take out every real single-word operator page.
        assert headline_reject_reason(entity) is None


@pytest.mark.parametrize("evidence", [
    {"city": "Ashburn"},
    {"address": "21561 Smith Switch Rd"},
    {"latitude": 39.0, "longitude": -77.4},
    {"lat": 39.0, "lon": -77.4},
    {"power_mw": 150},
    {"sqft": "220000"},
    {"acreage": 760},
    {"investment_usd": 6_000_000_000},
    {"market": "Northern Virginia"},
])
def test_evidence_gate_passes_anything_with_a_shred_of_evidence(evidence):
    fac = {"name": "Some Campus", "provider": "SomeCo"}
    fac.update(evidence)
    assert evidence_reject_reason(fac) is None


def test_evidence_gate_treats_zero_as_absent():
    # power_mw/sqft default to 0 rather than NULL on several writers
    fac = {"name": "Some Campus", "power_mw": 0, "sqft": "0"}
    assert evidence_reject_reason(fac) == "no-location-evidence"


# ── 4. ingestion actually refuses (behavioural, no DB) ───────────────────

class _RecordingConn:
    """Records whether the writer reached the DB.

    ★ It must RECORD, not raise: insert_discovered_facility wraps its whole
    body in `except Exception` and returns None, so a raising stub is
    swallowed and every assertion below would pass vacuously.
    """

    def __init__(self):
        self.touched = False

    def cursor(self, *a, **kw):
        self.touched = True
        raise RuntimeError("stop here — the guard let this row through")

    def rollback(self):
        pass

    def commit(self):
        pass


def _news_facility(name, **kw):
    fac = {"name": name, "provider": None, "city": None, "state": "WY",
           "country": "US", "latitude": None, "longitude": None,
           "power_mw": 150, "sqft": None, "status": "Announced",
           "source": "news_ner", "source_url": "https://example.com/a",
           "confidence_score": 0.62, "discovered_at": "2026-08-09",
           "notes": "", "investment_usd": None, "acreage": None}
    fac.update(kw)
    return fac


def test_insert_refuses_headline_names_before_touching_the_db():
    import news_facility_extractor as nfe
    conn = _RecordingConn()
    fac = _news_facility("Stack breaks ground on second Tokyo data center")
    assert nfe.insert_discovered_facility(conn, fac) is None
    assert conn.touched is False


def test_insert_refuses_evidence_free_ner_spans():
    import news_facility_extractor as nfe
    conn = _RecordingConn()
    fac = _news_facility("Copilot", provider="Copilot", state=None,
                         power_mw=None)
    assert nfe.insert_discovered_facility(conn, fac) is None
    assert conn.touched is False


def test_insert_still_reaches_the_db_for_a_real_candidate():
    """Non-vacuity: without this, a guard that rejected EVERYTHING would pass
    both tests above."""
    import news_facility_extractor as nfe
    conn = _RecordingConn()
    fac = _news_facility("AVAIO Little Rock Campus", city="Little Rock")
    nfe.insert_discovered_facility(conn, fac)
    assert conn.touched is True


# ── 5. source pins for the DB-bound call sites ───────────────────────────

def test_sitemap_loop_applies_the_headline_guard():
    assert "from util.facility_name_sanity import (" in MAIN_SRC
    assert "_headline_reject_reason(name)" in MAIN_SRC
    assert "_headline_junk_skipped" in MAIN_SRC
    # keyed on the NAME, never on the slug — a slug pattern for this class
    # would eat real facilities (see test_seo_index_hygiene's spare-real test)
    assert "_headline_reject_reason(full_slug)" not in MAIN_SRC
    # bound before the loop so the fail-open except can't leave it unbound
    region = MAIN_SRC[MAIN_SRC.index("def _build_sitemap_sections"):]
    assert region.index("_headline_junk_skipped = 0") < region.index(
        "for row in fac_rows:")


def test_ner_promotion_marks_rejects_instead_of_promoted():
    """The old flip set status='promoted' whether or not a row was created,
    so a refused candidate looked promoted forever."""
    assert "facility_reject_reason" in NER_SRC
    assert "SET status = 'rejected'" in NER_SRC


def test_profile_page_noindexes_headline_names():
    import routes.facility_profile_page as fpp
    fac = {"name": "Stack breaks ground on second Tokyo data center",
           "provider": "STACK Infrastructure", "city": "", "state": "",
           "country": "JP",
           "canonical_slug": "stack-breaks-ground-on-second-tokyo-"
                             "data-center-900992d1"}
    assert 'content="noindex"' in fpp._render_profile(fac, "x")
    real = {"name": "DC5 Ashburn", "provider": "Equinix", "city": "Ashburn",
            "state": "VA", "country": "USA",
            "canonical_slug": "equinix-dc5-ashburn-ab12cd34"}
    assert 'content="index, follow"' in fpp._render_profile(real, "x")
