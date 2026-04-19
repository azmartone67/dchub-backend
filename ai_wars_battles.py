"""
AI Wars — Additional Battle Content
======================================
18 new battles matching the exact ai_wars.py schema.

Two ways to use this:
  A) Run it — it POSTs each battle to your live API
  B) Paste the EXTRA_BATTLES list into _seed_data() in ai_wars.py

Fighter tuple format:
  (platform, role, accuracy, depth, speed, citation, insight, overall, api_calls, pick)
"""

import os
import json
import requests

DCHUB_BASE = os.environ.get("DCHUB_BASE_URL",
    "https://dc-hub-replit-fixedzip--azmartone1.replit.app")

EXTRA_BATTLES = [

    # ═══════════════════════════════════════════════════════════════════
    # WEEK 3 — Foundational battles
    # ═══════════════════════════════════════════════════════════════════

    {
        'id': 'battle-wk3-site-selection',
        'category': 'site-selection',
        'title': 'Edge of the Grid: Last 50MW in the Southeast',
        'description': 'A colo provider needs 50MW in the Southeast within 12 months. AIs compared Atlanta, Charlotte, and Nashville using site scores, energy pricing, carbon data, and M&A signals.',
        'date': '2026-01-20',
        'week_number': 3, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Claude — Pick: Charlotte NC',
        'api_calls': 38,
        'fighters': [
            ('grok',       'Scanner',    86, 80, 96, 78, 84, 85, 6, 'Nashville'),
            ('gemini',     'Energy',     90, 88, 87, 86, 87, 88, 7, 'Atlanta'),
            ('copilot',    'Finance',    82, 77, 89, 80, 78, 81, 5, 'Atlanta'),
            ('chatgpt',    'Risk',       88, 84, 90, 82, 83, 85, 6, 'Charlotte'),
            ('perplexity', 'Verify',     80, 74, 93, 90, 76, 79, 5, 'Atlanta'),
            ('claude',     'Synthesis',  95, 94, 79, 93, 93, 93, 8, 'Charlotte'),
            ('mistral',    'Europe Alt', 76, 72, 85, 70, 73, 75, 4, 'Dublin'),
        ],
    },
    {
        'id': 'battle-wk3-ma-forensics',
        'category': 'ma-forensics',
        'title': 'Anatomy of a $2.3B Take-Private',
        'description': 'AIs dissected a major PE take-private transaction using DC Hub deal data, market comparables, and pipeline analysis to determine if the acquirer overpaid.',
        'date': '2026-01-22',
        'week_number': 3, 'year': 2026,
        'winner_platform': 'gemini', 'winner_label': 'Gemini — Verdict: Fair Value',
        'api_calls': 30,
        'fighters': [
            ('grok',    'Deal Scout',  88, 84, 95, 81, 86, 87, 7, None),
            ('gemini',  'Valuation',   94, 93, 86, 90, 92, 92, 8, None),
            ('copilot', 'Comparable',  85, 80, 88, 83, 81, 84, 6, None),
            ('claude',  'Risk Report', 93, 95, 78, 92, 91, 91, 7, None),
            ('chatgpt', 'Structure',   86, 82, 90, 80, 83, 84, 5, None),
        ],
    },
    {
        'id': 'battle-wk3-market-deep-dive',
        'category': 'market-deep-dive',
        'title': 'ERCOT Under Pressure: Can Texas Handle the AI Surge?',
        'description': 'Deep dive into the Texas grid — fuel mix, carbon trajectory, pipeline capacity, and whether ERCOT can absorb another 2-3 GW of data center load.',
        'date': '2026-01-21',
        'week_number': 3, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Transmission is the bottleneck',
        'api_calls': 34,
        'fighters': [
            ('grok',       'Grid Stress',  87, 83, 95, 80, 86, 86, 6, None),
            ('gemini',     'Supply Model', 91, 90, 87, 88, 89, 89, 7, None),
            ('claude',     'Full Analysis',96, 97, 78, 94, 95, 94, 9, None),
            ('chatgpt',    'PPA Strategy', 88, 85, 89, 82, 84, 86, 6, None),
            ('perplexity', 'News Scan',    79, 73, 92, 91, 75, 78, 5, None),
            ('copilot',    'Exec Summary', 83, 78, 88, 81, 79, 82, 5, None),
        ],
    },
    {
        'id': 'battle-wk3-weekly-brief',
        'category': 'weekly-brief',
        'title': 'Week 3 Intelligence Roundup',
        'description': 'First full weekly brief — AIs pulled DC Hub news, transactions, and market shifts to compile a Monday morning executive summary.',
        'date': '2026-01-20',
        'week_number': 3, 'year': 2026,
        'winner_platform': None, 'winner_label': None,
        'api_calls': 18,
        'fighters': [
            ('grok',    'Headlines', 87, 82, 96, 80, 84, 86, 5, None),
            ('gemini',  'Global',    90, 87, 88, 85, 87, 88, 5, None),
            ('copilot', 'Executive', 83, 78, 87, 81, 79, 82, 4, None),
            ('mistral', 'EU Focus',  77, 74, 85, 72, 74, 76, 4, None),
        ],
    },

    # ═══════════════════════════════════════════════════════════════════
    # WEEK 4 — Escalation
    # ═══════════════════════════════════════════════════════════════════

    {
        'id': 'battle-wk4-site-selection',
        'category': 'site-selection',
        'title': 'The 200MW AI Campus: Phoenix vs. Dallas vs. Portland',
        'description': 'GPU-dense AI training campus requiring massive power, low carbon, and strong fiber. AIs evaluated three finalists using every DC Hub energy and site endpoint.',
        'date': '2026-01-27',
        'week_number': 4, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Claude — Pick: Dallas',
        'api_calls': 45,
        'fighters': [
            ('grok',       'Scanner',      89, 84, 96, 82, 87, 88, 7, 'Dallas'),
            ('gemini',     'Infra',        93, 91, 88, 89, 90, 91, 8, 'Portland'),
            ('copilot',    'Cost Model',   87, 82, 89, 85, 83, 85, 6, 'Phoenix'),
            ('claude',     'Full Brief',   97, 96, 78, 95, 96, 95, 9, 'Dallas'),
            ('chatgpt',    'Risk Matrix',  86, 82, 91, 80, 84, 85, 6, 'Dallas'),
            ('perplexity', 'Verify',       78, 72, 93, 91, 74, 77, 5, 'Portland'),
            ('deepseek',   'Alt Analysis', 84, 80, 86, 78, 82, 82, 6, 'Dallas'),
        ],
    },
    {
        'id': 'battle-wk4-operator-showdown',
        'category': 'operator-showdown',
        'title': 'Who Wins the AI Workload War?',
        'description': 'AIs championed different operators for AI/ML workloads — QTS for wholesale scale, Equinix for interconnection, CoreWeave for GPU-native. Each made their case from DC Hub portfolio data.',
        'date': '2026-01-28',
        'week_number': 4, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'QTS — Wholesale scale wins',
        'api_calls': 32,
        'fighters': [
            ('grok',    'QTS Case',      90, 87, 94, 83, 89, 89, 7, 'QTS'),
            ('gemini',  'Equinix Case',  92, 91, 87, 89, 90, 90, 7, 'Equinix'),
            ('copilot', 'CoreWeave',     84, 79, 88, 82, 80, 83, 5, 'CoreWeave'),
            ('claude',  'Judge',         96, 97, 80, 94, 95, 94, 8, 'QTS'),
            ('chatgpt', 'DLR Case',      85, 81, 90, 79, 82, 83, 5, 'Digital Realty'),
        ],
    },
    {
        'id': 'battle-wk4-ma-forensics',
        'category': 'ma-forensics',
        'title': 'Follow the Money: PE Deal Flow Decoded',
        'description': 'Infrastructure fund deal patterns analyzed — AIs grouped transactions by type, calculated price-per-MW trends, and identified which markets are undervalued based on deal velocity vs. capacity.',
        'date': '2026-01-29',
        'week_number': 4, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Claude — Target: Atlanta wholesale',
        'api_calls': 36,
        'fighters': [
            ('grok',       'Deal Scan',  89, 85, 96, 82, 87, 88, 7, None),
            ('gemini',     'Model',      92, 90, 87, 88, 90, 90, 7, None),
            ('claude',     'Strategy',   96, 96, 78, 93, 95, 94, 8, None),
            ('chatgpt',    'Risk',       86, 82, 90, 80, 83, 84, 6, None),
            ('copilot',    'Exec Brief', 84, 79, 88, 82, 80, 83, 5, None),
            ('perplexity', 'News Cross', 80, 75, 93, 91, 77, 80, 6, None),
        ],
    },
    {
        'id': 'battle-wk4-stump-the-ai',
        'category': 'stump-the-ai',
        'title': 'CARWSS Challenge: Build a Custom Score',
        'description': 'Maximum difficulty — AIs had to create a Carbon-Adjusted Risk-Weighted Site Score for 5 US locations using 7 different DC Hub endpoints and show every calculation step.',
        'date': '2026-01-30',
        'week_number': 4, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Dallas wins CARWSS at 78.4',
        'api_calls': 52,
        'fighters': [
            ('grok',       'Solar Focus', 82, 78, 94, 76, 80, 82, 5, 'Phoenix'),
            ('gemini',     'Efficient',   84, 82, 88, 80, 82, 83, 5, 'Portland'),
            ('claude',     'Full Calc',   92, 94, 76, 90, 88, 90, 9, 'Dallas'),
            ('chatgpt',    'Strong Calc', 88, 86, 86, 84, 84, 86, 7, 'Dallas'),
            ('copilot',    'Partial',     70, 66, 84, 72, 68, 72, 4, 'Ashburn'),
            ('mistral',    'Incomplete',  62, 58, 82, 64, 60, 65, 3, 'Ashburn'),
            ('perplexity', 'Good Try',    78, 75, 90, 86, 74, 78, 6, 'Dallas'),
        ],
    },
    {
        'id': 'battle-wk4-market-deep-dive',
        'category': 'market-deep-dive',
        'title': 'Green Grid Gauntlet: Cleanest Power in America',
        'description': 'AIs ranked the top 5 US data center markets by grid cleanliness — pulling carbon intensity, fuel mix, and renewable potential to find the best path to 24/7 carbon-free energy.',
        'date': '2026-01-28',
        'week_number': 4, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Portland wins clean, Dallas wins 24/7 CFE',
        'api_calls': 40,
        'fighters': [
            ('grok',       'Carbon Rank', 86, 82, 95, 80, 85, 86, 6, None),
            ('gemini',     'Fuel Mix',    90, 88, 87, 86, 88, 88, 7, None),
            ('claude',     'Full Grid',   94, 96, 78, 92, 93, 92, 8, None),
            ('chatgpt',    'Renewables',  87, 84, 90, 82, 84, 85, 6, None),
            ('perplexity', 'PPA News',    80, 75, 92, 90, 77, 80, 6, None),
        ],
    },
    {
        'id': 'battle-wk4-weekly-brief',
        'category': 'weekly-brief',
        'title': 'Week 4 Intelligence Roundup',
        'description': 'Mega-deal week: 2 new transactions, ERCOT grid stress event, and a surprising data point about pipeline-to-operational conversion rates dropping.',
        'date': '2026-01-27',
        'week_number': 4, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Pipeline conversion dropping',
        'api_calls': 22,
        'fighters': [
            ('grok',    'Headlines',  89, 84, 96, 82, 86, 87, 6, None),
            ('gemini',  'Global',     91, 89, 88, 87, 88, 89, 6, None),
            ('claude',  'Insight',    95, 94, 80, 93, 94, 93, 7, None),
            ('copilot', 'Executive',  84, 79, 88, 82, 80, 83, 5, None),
            ('chatgpt', 'Clean Copy', 87, 83, 90, 81, 84, 85, 5, None),
        ],
    },

    # ═══════════════════════════════════════════════════════════════════
    # WEEK 5 — Heavy hitters
    # ═══════════════════════════════════════════════════════════════════

    {
        'id': 'battle-wk5-site-selection',
        'category': 'site-selection',
        'title': 'The Global 500MW Expansion Blueprint',
        'description': 'Fortune 50 tech company planning 500MW across 3 continents. AIs used every DC Hub endpoint — global facility data, US market comparisons, energy economics, M&A landscape, and construction pipeline.',
        'date': '2026-02-03',
        'week_number': 5, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Claude — 200MW Dallas, 150MW Frankfurt, 150MW Singapore',
        'api_calls': 58,
        'fighters': [
            ('grok',       'US Scan',    88, 84, 95, 81, 86, 87, 7, None),
            ('gemini',     'Global Map', 93, 92, 87, 90, 91, 91, 8, None),
            ('claude',     'Full Brief', 97, 98, 76, 95, 96, 95, 10, None),
            ('chatgpt',    'Risk',       87, 83, 91, 80, 84, 85, 7, None),
            ('copilot',    'Finance',    84, 79, 88, 82, 80, 83, 6, None),
            ('perplexity', 'Verify',     79, 74, 93, 91, 76, 79, 6, None),
            ('deepseek',   'APAC Focus', 85, 82, 86, 80, 83, 83, 6, None),
        ],
    },
    {
        'id': 'battle-wk5-operator-showdown',
        'category': 'operator-showdown',
        'title': 'Pipeline Kings: Who Is Building the Most?',
        'description': 'Construction pipeline analysis across the top 5 US markets — AIs compared MW under construction to existing capacity, identified which operators are building fastest, and flagged oversupply risks.',
        'date': '2026-02-04',
        'week_number': 5, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'Phoenix oversupply risk flagged',
        'api_calls': 28,
        'fighters': [
            ('grok',    'Pipeline Scan', 88, 84, 95, 81, 86, 87, 6, None),
            ('gemini',  'Ratios',        91, 90, 87, 88, 89, 89, 7, None),
            ('claude',  'Risk Matrix',   94, 96, 78, 92, 93, 93, 8, None),
            ('copilot', 'Top Line',      83, 78, 88, 81, 79, 82, 5, None),
            ('chatgpt', 'Operator ID',   86, 82, 90, 80, 83, 84, 5, None),
        ],
    },
    {
        'id': 'battle-wk5-ma-forensics',
        'category': 'ma-forensics',
        'title': 'Predict the Next Mega-Deal',
        'description': 'Using every DC Hub data source — transactions, markets, pipeline, facilities, news, and energy — AIs predicted the next likely >$1B deal with target profile, acquirer type, and valuation range.',
        'date': '2026-02-05',
        'week_number': 5, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': '$2-3B Dallas wholesale PE acquisition',
        'api_calls': 42,
        'fighters': [
            ('grok',       'PE Thesis',     88, 85, 95, 82, 87, 88, 7, 'Secondary roll-up'),
            ('gemini',     'EU Arbitrage',  90, 88, 87, 86, 88, 88, 6, 'European platform'),
            ('claude',     'Full Model',    93, 94, 78, 91, 92, 91, 9, 'Dallas wholesale'),
            ('chatgpt',    'Conservative',  86, 83, 90, 80, 83, 84, 6, 'NoVA colo'),
            ('perplexity', 'News Signal',   80, 76, 92, 90, 78, 80, 6, 'Phoenix'),
            ('copilot',    'Generic',       76, 72, 87, 78, 74, 77, 4, 'Growing markets'),
        ],
    },
    {
        'id': 'battle-wk5-stump-the-ai',
        'category': 'stump-the-ai',
        'title': 'Impossible: Model a Greenfield Campus from Scratch',
        'description': 'AIs had to design a complete greenfield data center campus — selecting location via site scoring, sizing power from grid data, modeling energy costs, assessing carbon exposure, and estimating build timeline from pipeline data.',
        'date': '2026-02-06',
        'week_number': 5, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': '150MW campus, Hillsboro OR, $1.2B',
        'api_calls': 48,
        'fighters': [
            ('grok',       'Bold Pick',    86, 83, 94, 80, 85, 86, 6, 'West Texas'),
            ('gemini',     'Systematic',   91, 90, 87, 88, 89, 89, 8, 'Ashburn VA'),
            ('claude',     'Full Design',  95, 96, 76, 93, 94, 93, 10, 'Hillsboro OR'),
            ('chatgpt',    'Safe Pick',    87, 84, 90, 82, 84, 85, 7, 'Dallas TX'),
            ('deepseek',   'Cost Focus',   83, 80, 86, 78, 81, 82, 5, 'Salt Lake City'),
            ('perplexity', 'Cited Well',   80, 76, 92, 91, 77, 80, 6, 'Phoenix AZ'),
            ('copilot',    'Incomplete',   72, 68, 86, 74, 70, 74, 4, 'Ashburn VA'),
        ],
    },
    {
        'id': 'battle-wk5-weekly-brief',
        'category': 'weekly-brief',
        'title': 'Week 5 Intelligence Roundup',
        'description': 'Record pipeline week — 3 new construction announcements, 1 major acquisition, and Northern Virginia vacancy hitting all-time lows.',
        'date': '2026-02-03',
        'week_number': 5, 'year': 2026,
        'winner_platform': 'grok', 'winner_label': 'AI demand > total US pipeline',
        'api_calls': 24,
        'fighters': [
            ('grok',    'Headlines',  90, 86, 96, 83, 88, 89, 6, None),
            ('gemini',  'Global',     89, 87, 88, 86, 87, 88, 6, None),
            ('claude',  'Depth',      93, 94, 79, 92, 92, 92, 7, None),
            ('copilot', 'Executive',  84, 79, 88, 82, 80, 83, 5, None),
            ('chatgpt', 'Clean',      86, 82, 90, 80, 83, 84, 5, None),
        ],
    },

    # ═══════════════════════════════════════════════════════════════════
    # WEEK 6 — Additional (complement existing wk6 battles)
    # ═══════════════════════════════════════════════════════════════════

    {
        'id': 'battle-wk6-market-deep-dive',
        'category': 'market-deep-dive',
        'title': 'Singapore vs. Tokyo vs. Mumbai: The APAC Showdown',
        'description': 'Three APAC markets compared on facility density, energy costs, connectivity, regulatory environment, and growth trajectory using DC Hub global data.',
        'date': '2026-02-11',
        'week_number': 6, 'year': 2026,
        'winner_platform': 'gemini', 'winner_label': 'Singapore — Despite the moratorium',
        'api_calls': 30,
        'fighters': [
            ('grok',       'Singapore',  89, 86, 94, 83, 88, 88, 6, 'Singapore'),
            ('gemini',     'Full APAC',  93, 92, 88, 90, 91, 91, 8, 'Singapore'),
            ('claude',     'Deep Dive',  95, 96, 78, 93, 94, 93, 8, 'Tokyo'),
            ('chatgpt',    'Mumbai',     86, 83, 90, 80, 84, 85, 6, 'Mumbai'),
            ('deepseek',   'China Alt',  82, 79, 86, 76, 80, 81, 5, 'Singapore'),
            ('perplexity', 'News',       79, 74, 93, 91, 76, 79, 5, 'Singapore'),
        ],
    },
    {
        'id': 'battle-wk6-stump-v2',
        'category': 'stump-the-ai',
        'title': 'Stress Test: What Breaks First in a Power Crisis?',
        'description': 'Nightmare scenario — a summer heat wave hits 3 major US markets simultaneously. AIs modeled which market fails first using grid data, fuel mix, carbon intensity, and facility density.',
        'date': '2026-02-12',
        'week_number': 6, 'year': 2026,
        'winner_platform': 'claude', 'winner_label': 'ERCOT fails first, PJM holds longest',
        'api_calls': 44,
        'fighters': [
            ('grok',       'ERCOT Risk',   90, 87, 95, 83, 89, 89, 7, 'Texas fails'),
            ('gemini',     'Grid Model',   91, 90, 87, 88, 89, 89, 7, 'Texas fails'),
            ('claude',     'Full Model',   96, 97, 76, 94, 95, 94, 9, 'Texas first, then AZ'),
            ('chatgpt',    'Conservative', 85, 82, 90, 80, 83, 84, 6, 'All at risk'),
            ('copilot',    'Basic',        78, 74, 88, 76, 76, 78, 4, 'Texas'),
            ('deepseek',   'Alt Model',    83, 80, 86, 78, 81, 82, 5, 'Arizona first'),
            ('perplexity', 'News Cross',   80, 76, 93, 91, 78, 80, 6, 'Texas fails'),
        ],
    },
]


def format_for_api(battle):
    """Convert battle dict to the POST /api/v1/ai-wars/battles format."""
    fighters_raw = battle.pop('fighters')
    fighters = []
    for f in fighters_raw:
        fighters.append({
            'platform': f[0],
            'role': f[1],
            'score_accuracy': f[2],
            'score_depth': f[3],
            'score_speed': f[4],
            'score_citation': f[5],
            'score_insight': f[6],
            'score_overall': f[7],
            'api_calls': f[8],
            'pick': f[9],
        })
    battle['fighters'] = fighters
    return battle


def main():
    print(f"🔥 AI Wars Battle Loader")
    print(f"   {len(EXTRA_BATTLES)} battles to load")
    print(f"   Target: {DCHUB_BASE}/api/v1/ai-wars/battles")
    print()

    success = 0
    errors = 0

    for battle in EXTRA_BATTLES:
        b = format_for_api(dict(battle))  # copy to preserve original
        b['fighters'] = []
        # Re-extract fighters from original
        for f in battle['fighters']:
            if isinstance(f, tuple):
                b['fighters'].append({
                    'platform': f[0], 'role': f[1],
                    'score_accuracy': f[2], 'score_depth': f[3],
                    'score_speed': f[4], 'score_citation': f[5],
                    'score_insight': f[6], 'score_overall': f[7],
                    'api_calls': f[8], 'pick': f[9],
                })
            else:
                b['fighters'].append(f)

        try:
            r = requests.post(
                f"{DCHUB_BASE}/api/v1/ai-wars/battles",
                json=b, timeout=15
            )
            if r.status_code in (200, 201):
                print(f"   ✅ {battle['id']}: {battle['title']}")
                success += 1
            else:
                print(f"   ⚠️  {battle['id']}: HTTP {r.status_code} — {r.text[:100]}")
                errors += 1
        except Exception as e:
            print(f"   ❌ {battle['id']}: {e}")
            errors += 1

    print()
    print(f"   Done: {success} loaded, {errors} errors")
    print()

    if errors == len(EXTRA_BATTLES):
        print("   💡 API unreachable. To load manually:")
        print("   1. Copy EXTRA_BATTLES into ai_wars.py _seed_data()")
        print("   2. Or save as JSON and import via Replit shell")
        print()
        # Dump as JSON for manual import
        import copy
        json_battles = []
        for battle in EXTRA_BATTLES:
            b = copy.deepcopy(battle)
            if isinstance(b['fighters'][0], tuple):
                b['fighters'] = [
                    {'platform': f[0], 'role': f[1],
                     'score_accuracy': f[2], 'score_depth': f[3],
                     'score_speed': f[4], 'score_citation': f[5],
                     'score_insight': f[6], 'score_overall': f[7],
                     'api_calls': f[8], 'pick': f[9]}
                    for f in b['fighters']
                ]
            json_battles.append(b)
        with open('extra_battles.json', 'w') as f:
            json.dump(json_battles, f, indent=2)
        print(f"   📁 Saved extra_battles.json for manual import")


if __name__ == "__main__":
    main()
