"""Regression tests for multi-word facility search (r-search-multiword 2026-07-12).

CONFIRMED BUG (verified live): the facility search endpoints wrapped the whole
query in one ILIKE '%...%' phrase match, so the natural-language shape ChatGPT
Deep Research emits self-defeated:

    /api/v1/search?q=Ashburn                    -> 25 results  OK
    /api/v1/search?q=Ashburn data centers       -> 0 results   BUG
    /api/v1/search?q=Northern Virginia data centers -> 1 result BUG

These tests are PURE: they import only search_matching (stdlib-only, no Flask /
DB / main), matching the tests/conftest.py rule that unit tests never import
main. `simulate_search` faithfully reproduces the endpoint's SQL semantics
(`col ILIKE '%tok%'` == case-insensitive substring; market-alias city/state
expansion; token-overlap ranking) so we can assert real "returns >0 relevant,
ranked, bounded results" behavior without a live DB.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from search_matching import (
    plan_search,
    meaningful_tokens,
    reduced_phrase,
    tokenize,
    token_where,
    token_rank,
    SEARCH_FIELD_WEIGHTS,
    SEARCH_STOPWORDS,
)

# Faithful subset of main.py's MARKET_ALIASES (the two keys the failing queries
# reduce to). The real dict lives in main.py; mirrored here to keep this pure.
ALIASES = {
    'ashburn': ['Ashburn', 'Loudoun'],
    'northern virginia': ['Ashburn', 'Loudoun', 'Sterling', 'Reston',
                          'Herndon', 'Manassas', 'Prince William', 'Leesburg'],
    'phoenix': ['Phoenix', 'Mesa', 'Tempe'],
}

_COLS = ('name', 'city', 'state', 'provider')

# A small, realistic fixture with decoys that must NOT leak into 2-word queries.
FACILITIES = [
    {'id': 1, 'name': 'Equinix DC2', 'provider': 'Equinix', 'city': 'Ashburn',   'state': 'VA', 'confidence': 0.9, 'power_mw': 40},
    {'id': 2, 'name': 'Digital Realty ACC7', 'provider': 'Digital Realty', 'city': 'Ashburn', 'state': 'VA', 'confidence': 0.8, 'power_mw': 60},
    {'id': 3, 'name': 'Equinix Sterling Campus', 'provider': 'Equinix', 'city': 'Sterling', 'state': 'VA', 'confidence': 0.7, 'power_mw': 30},
    {'id': 4, 'name': 'Iron Mountain VA-1', 'provider': 'Iron Mountain', 'city': 'Manassas', 'state': 'VA', 'confidence': 0.6, 'power_mw': 20},
    {'id': 5, 'name': 'Google The Dalles', 'provider': 'Google', 'city': 'Portland', 'state': 'OR', 'confidence': 0.95, 'power_mw': 100},
    {'id': 6, 'name': 'Vantage AZ1', 'provider': 'Vantage', 'city': 'Phoenix', 'state': 'AZ', 'confidence': 0.85, 'power_mw': 50},
]


def _ilike(row, col, token):
    """Mimic SQL `col ILIKE '%token%'` — case-insensitive substring."""
    return token in (row.get(col) or '').lower()


def simulate_search(query, rows=FACILITIES, aliases=ALIASES, limit=25):
    """Reproduce the endpoint's WHERE + ORDER BY behavior in pure Python."""
    kind, val = plan_search(query, aliases)
    if kind == 'alias':
        cities = aliases[val]

        def match(r):
            for c in cities:
                if len(c) == 2 and c.isupper():
                    if (r.get('state') or '') == c:
                        return True
                elif c.lower() in (r.get('city') or '').lower():
                    return True
            return False

        matched = [r for r in rows if match(r)]
        matched.sort(key=lambda r: (-(r.get('confidence') or 0), -(r.get('power_mw') or 0)))
        return matched[:limit]

    tokens = val
    if not tokens:
        return []

    def where_any(r):
        return any(_ilike(r, col, t) for t in tokens for col in _COLS)

    weights = SEARCH_FIELD_WEIGHTS

    def rank(r):
        return sum(w for t in tokens for col, w in weights if _ilike(r, col, t))

    matched = [r for r in rows if where_any(r)]
    matched.sort(key=lambda r: (-rank(r), -(r.get('confidence') or 0)))
    return matched[:limit]


# ── plan_search: the core routing decision ─────────────────────────────────

def test_single_word_alias_unchanged():
    # "Ashburn" already routed through the alias path pre-fix — keep it.
    assert plan_search('Ashburn', ALIASES) == ('alias', 'ashburn')


def test_single_word_non_alias_is_tokens():
    assert plan_search('Equinix', ALIASES) == ('tokens', ['equinix'])


def test_multiword_reduces_to_alias_ashburn():
    # THE regression: "Ashburn data centers" must resolve, not zero out.
    assert plan_search('Ashburn data centers', ALIASES) == ('alias', 'ashburn')


def test_multiword_reduces_to_alias_nova():
    assert plan_search('Northern Virginia data centers', ALIASES) == ('alias', 'northern virginia')


def test_genuine_multiword_falls_to_tokens():
    # No alias for this combo -> OR-any-token path with stopwords dropped.
    assert plan_search('Equinix Sterling campus', ALIASES) == ('tokens', ['equinix', 'sterling'])


def test_empty_query():
    assert plan_search('', ALIASES) == ('tokens', [])


# ── tokenization / stopwords ───────────────────────────────────────────────

def test_meaningful_tokens_drops_domain_words():
    assert meaningful_tokens('Ashburn data centers') == ['ashburn']
    assert meaningful_tokens('data centers in Phoenix') == ['phoenix']


def test_all_stopwords_falls_back_to_raw():
    # If EVERY token is a stopword, keep them rather than returning nothing.
    assert meaningful_tokens('data centers') == ['data', 'centers']


def test_tokenize_dedups_preserving_order():
    assert tokenize('AWS AWS Ashburn') == ['aws', 'ashburn']


def test_reduced_phrase():
    assert reduced_phrase('Northern Virginia DATA Centers') == 'northern virginia'


# ── end-to-end simulated search: >0 relevant, ranked, bounded ──────────────

def test_single_word_ashburn_returns_results():
    res = simulate_search('Ashburn')
    assert len(res) > 0
    assert all('Ashburn' == r['city'] for r in res)  # alias 'ashburn' -> city


def test_ashburn_data_centers_returns_ashburn():
    # BEFORE fix: 0 rows. AFTER: the Ashburn facilities appear.
    res = simulate_search('Ashburn data centers')
    assert len(res) > 0
    assert any(r['city'] == 'Ashburn' for r in res)
    assert {r['id'] for r in res} == {1, 2}  # both Ashburn rows, nothing else


def test_northern_virginia_data_centers_returns_nova():
    # BEFORE fix: 1 row. AFTER: the NoVA facilities appear.
    res = simulate_search('Northern Virginia data centers')
    assert len(res) > 0
    ids = {r['id'] for r in res}
    assert {1, 2, 3, 4}.issubset(ids)          # Ashburn + Sterling + Manassas
    assert 5 not in ids and 6 not in ids       # not Portland OR / Phoenix AZ


def test_multiword_ranking_puts_best_overlap_first():
    # "Equinix Sterling": the row matching BOTH tokens ranks above rows that
    # match only one.
    res = simulate_search('Equinix Sterling')
    assert res[0]['id'] == 3  # Equinix Sterling Campus (name+provider+city hit)
    ids = {r['id'] for r in res}
    assert 1 in ids  # Equinix Ashburn matches the 'equinix' token too


def test_two_word_query_does_not_return_whole_table():
    # Over-match guard: a 2-word query must not sweep in rows that match
    # NEITHER token.
    res = simulate_search('Equinix Sterling')
    ids = {r['id'] for r in res}
    assert 5 not in ids  # Google Portland matches neither 'equinix' nor 'sterling'
    assert 6 not in ids  # Vantage Phoenix matches neither
    assert len(res) < len(FACILITIES)


def test_limit_is_respected():
    res = simulate_search('Equinix Sterling', limit=1)
    assert len(res) == 1


# ── shape / injection-safety contract of the SQL builders ──────────────────

def test_token_where_shape_and_param_count():
    sql, params = token_where(['ashburn', 'sterling'], _COLS)
    # One OR-group per token, one placeholder per (token, column).
    assert sql.count('ILIKE %s') == 2 * len(_COLS)
    assert len(params) == 2 * len(_COLS)
    assert sql.startswith('(') and sql.endswith(')')
    # Injection-safety: raw token text is carried in params, never inlined.
    assert 'ashburn' not in sql and 'sterling' not in sql
    assert params[0] == '%ashburn%'


def test_token_where_empty():
    assert token_where([], _COLS) == ('', [])


def test_token_rank_shape_and_param_count():
    sql, params = token_rank(['ashburn', 'sterling'], SEARCH_FIELD_WEIGHTS)
    assert len(params) == 2 * len(SEARCH_FIELD_WEIGHTS)
    assert 'CASE WHEN' in sql
    assert params == ['%ashburn%'] * len(SEARCH_FIELD_WEIGHTS) + ['%sterling%'] * len(SEARCH_FIELD_WEIGHTS)


def test_stopwords_include_domain_and_connectors():
    for w in ('data', 'center', 'centers', 'datacenter', 'in', 'the'):
        assert w in SEARCH_STOPWORDS
