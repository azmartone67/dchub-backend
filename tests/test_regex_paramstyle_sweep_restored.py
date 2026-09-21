"""Regex literals the SQLite->Postgres paramstyle sweep rewrote as SQL.

The sweep replaced `?` with `%s` inside regexes as well as SQL: `(?:` became
`(%s:`, `acquires?` became `acquires%s`, `.*?` became `.*%s`. Each such pattern
demands a literal "%s", so it never matches and the code behind it silently
falls back: None, 'unknown', 'Undisclosed', the default country, an empty deal
target, a tuple where a name belongs.

One test per live module. Each runs the module's own function on a real input
and fails on the corrupted pattern. Headlines are rows from DC Hub's
news_articles table (source and date noted); inputs marked "representative" are
written in the same shape because no stored row exercised that branch.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Seeking Alpha, 2026-09-02
KKR_JV = "KKR's $10B AI joint venture hires from Equinix, AES for key roles - report (KKR:NYSE)"
# Telecompaper, 2026-09-15
DLR_TURKIYE = ("Digital Realty enters Turkiye data centre market, "
               "sets up joint venture with Ronesans Infrastructure")


def _load_function(filename, name, namespace):
    """Exec one top-level def from a module whose import needs a database."""
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(namespace)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), filename, "exec"), ns)
    return ns[name]


# ── news_facility_extractor.py (crawler_scheduler facility_discovery, daily) ──

def test_news_extractor_reads_dollar_amounts():
    from news_facility_extractor import extract_investment_usd
    assert extract_investment_usd(KKR_JV) == 10_000_000_000
    # PV Magazine, 2026-09-08
    assert extract_investment_usd(
        "The $105.5 million facility is expected to begin commercial production "
        "in June 2027.") == 105_500_000


def test_news_extractor_reads_square_feet():
    from news_facility_extractor import extract_sqft
    # Data Center Dynamics, 2026-08-17
    assert extract_sqft("Plans for 100,000 sq ft development withdrawn") == 100_000
    # Now that the pattern matches, a bare comma before "SF" must not reach int('').
    assert extract_sqft("Campuses planned in Oakland, SF and San Jose") is None


def test_news_extractor_reads_uk_abbreviation_but_not_the_word_us():
    from news_facility_extractor import extract_country
    # representative: a UK site named only by the abbreviation (was the 'US' default)
    assert extract_country("Ark Data Centres plans 200MW campus in the UK") == "GB"
    # The search is IGNORECASE and US is checked first: "us" in a quote is not the US.
    assert extract_country(
        "'Dublin gives us the power we need,' the operator said of its Ireland site") == "IE"


# ── ai_wars_automation.py (dchub-jobs.yml ai-wars cron -> run_master_tick) ──

def test_ai_wars_counts_figures_toward_accuracy():
    from ai_wars_automation import _score_response
    question = "Where should a hyperscaler build next?"
    # representative answer; the two texts differ only in the figures
    with_figures = "Grid access decides it: a 300 MW campus needs 1.2 GW of firm supply."
    without = "Grid access decides it: a large campus needs plenty of firm supply."
    gained = (_score_response(with_figures, question, had_real_response=True)["accuracy"]
              - _score_response(without, question, had_real_response=True)["accuracy"])
    assert gained == 2 * 4  # two figures, 4 points each


# ── alert_processor.py (POST /api/v1/alerts/process, /api/v2/alerts/check) ──

def test_alert_processor_capacity_alert_reads_mw_from_headline():
    match = _load_function("alert_processor.py", "match_capacity_threshold", {"re": re})
    # Data Center Dynamics, 2026-09-14
    news = [{"title": "Digital Realty enters Turkey, plans 22MW facility in Ankara",
             "description": "Forms joint venture with local holding company Rönesans"}]
    hits = match({"min_mw": 10, "max_mw": 50}, news, [], [])
    assert [(h["type"], h["capacity_mw"]) for h in hits] == [("news", 22.0)]


# ── auto_pilot.py (imported by routes/autopilot_routes.py) ──

def test_auto_pilot_reads_abbreviated_deal_value():
    from auto_pilot import extract_value_from_title
    assert extract_value_from_title(KKR_JV) == "$10.0B"


# ── deal_ingestion_scheduler.py (start_deal_scheduler in main.py, every 6h) ──

def test_deal_ingestion_classifies_joint_venture():
    from deal_ingestion_scheduler import _deal_type
    assert _deal_type(DLR_TURKIYE) == "joint_venture"


# ── deep_learning_engine.py (POST /api/autopilot/deep-learning/run) ──

def _engine():
    from deep_learning_engine import DeepLearningEngine
    return object.__new__(DeepLearningEngine)  # these methods never read self


def test_deep_learning_extracts_operator_names_not_group_tuples():
    assert _engine()._extract_operators(DLR_TURKIYE) == ["Ronesans"]


def test_deep_learning_reads_deal_target_and_value():
    # the QTS headline tests/test_deal_scraper_* already use
    txs = _engine()._detect_transactions("Blackstone to acquire QTS in $10 billion deal")
    assert len(txs) == 1
    assert "QTS" in txs[0]["target"]
    assert txs[0]["value_millions"] == 10_000.0


# ── discovery_pipeline.py (POST /api/admin/discovery-pipeline/run) ──

def test_discovery_pipeline_names_the_developer():
    from discovery_pipeline import extract_facility_data
    # representative: an operator outside KNOWN_OPERATORS, introduced by "developer"
    data = extract_facility_data({
        "title": "Data center developer Tract has filed plans for a 1,000-acre campus "
                 "in Buckeye, Arizona",
        "summary": ""})
    assert data["operator"] == "Tract"


# ── self_learning_discovery.py (POST /api/autopilot/self-learning/run) ──

def test_self_learning_discovery_keeps_multi_word_city():
    from bs4 import BeautifulSoup
    from self_learning_discovery import SelfLearningDiscovery
    elem = BeautifulSoup('<div class="facility"><h3>CoreSite SV8</h3>'
                         '<p>Santa Clara, CA</p></div>', "html.parser").div
    got = object.__new__(SelfLearningDiscovery)._extract_from_element(elem)
    assert got["city"] == "Santa Clara"


# ── main.py (boot, when NEON_DATABASE_URL is set) ──

def test_main_strips_channel_binding_wherever_it_sits():
    strip = _load_function("main.py", "_strip_channel_binding", {"_re_db": re})
    base = "postgresql://neondb_owner:pw@ep-example-123456-pooler.us-east-2.aws.neon.tech/neondb"
    # Neon's console order, channel_binding last: `[&%s]` still caught this one.
    assert strip(base + "?sslmode=require&channel_binding=require") == base + "?sslmode=require"
    # channel_binding first: `[&%s]` never matched `?`, and a bare `[&?]` restore
    # would have left `neondb&sslmode=require`.
    assert strip(base + "?channel_binding=require&sslmode=require") == base + "?sslmode=require"
    assert strip(base + "?channel_binding=require") == base
    assert strip(base + "?sslmode=require") == base + "?sslmode=require"
