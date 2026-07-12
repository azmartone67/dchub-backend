"""Multi-word free-text search matching (pure, stdlib-only).

r-search-multiword (2026-07-12): the facility search endpoints wrapped the
WHOLE query in a single ILIKE '%...%' phrase match, e.g.

    q="Ashburn data centers" -> WHERE city ILIKE '%Ashburn data centers%' OR ...

No column ever contains that literal phrase, so realistic natural-language
queries (exactly what ChatGPT Deep Research emits) self-defeated to 0 rows
while the single word "Ashburn" returned 25. See main.py search_facilities /
_list_facilities_full / _list_facilities_free and dchub_mcp_server.py.

This module is DELIBERATELY dependency-free (stdlib only, no Flask/DB import)
so it can be unit-tested without a live DB or JWT_SECRET (see
reference_dchub_green_main: unit tests must never import main). The call sites
pass their own column list + market-alias dict.

Strategy (lowest-risk that works):
  1. Exact market-alias hit on the full query      -> caller expands alias.
  2. Drop domain/stop words ("data", "centers", ...) then re-check the
     reduced phrase against the market aliases      -> caller expands alias.
     (This routes "Ashburn data centers" -> "ashburn" and "Northern Virginia
      data centers" -> "northern virginia", both real aliases, so they reuse
      the existing, well-tested city-expansion path.)
  3. Otherwise OR / token-overlap match: split into tokens, match ANY token
     across the weighted columns, and rank by how many tokens hit which field.
     Single-word queries collapse to one token = identical to the old
     per-column ILIKE, so "Ashburn" is unchanged. Over-match is bounded by the
     caller's existing LIMIT and pushed below real hits by the rank score.
"""
from __future__ import annotations

import re

# Domain words that are implied by the corpus (everything here IS a data
# center) plus common English connectors. Dropping them keeps the meaningful
# tokens so "data centers in Ashburn" -> ["ashburn"]. A connector like "in"
# left in would ILIKE '%in%' and match half the table (Arlington, Washington…).
SEARCH_STOPWORDS = frozenset({
    # domain
    'data', 'center', 'centers', 'centre', 'centres',
    'datacenter', 'datacenters', 'datacentre', 'datacentres',
    'dc', 'dcs', 'facility', 'facilities', 'campus', 'campuses',
    'site', 'sites', 'colo', 'colocation',
    # english connectors
    'in', 'of', 'the', 'a', 'an', 'and', 'or', 'for', 'near',
    'at', 'with', 'to', 'on', 'by', 'from',
})

# Field -> rank weight. Higher = a hit there is more relevant. Used to build
# the ORDER BY relevance term so multi-token queries surface the row that
# matches the most tokens in the most important fields.
SEARCH_FIELD_WEIGHTS = (('name', 4), ('city', 3), ('provider', 2), ('state', 1))

_TOKEN_RE = re.compile(r'[a-z0-9]+')


def tokenize(query):
    """Lowercase, split on non-alphanumerics, de-dup preserving order."""
    if not query:
        return []
    seen = set()
    out = []
    for tok in _TOKEN_RE.findall(str(query).lower()):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def meaningful_tokens(query):
    """tokenize() minus stop/domain words.

    If EVERY token is a stopword (e.g. the query is literally "data centers"),
    fall back to the raw tokens rather than returning nothing — better a broad
    (LIMIT-bounded, ranked) result than an empty one.
    """
    toks = tokenize(query)
    filtered = [t for t in toks if t not in SEARCH_STOPWORDS]
    return filtered if filtered else toks


def reduced_phrase(query):
    """Space-joined meaningful tokens — for a second market-alias lookup."""
    return ' '.join(meaningful_tokens(query))


def plan_search(query, market_aliases=None):
    """Decide how to match a free-text query.

    Returns a 2-tuple:
      ('alias', alias_key) -> caller expands market_aliases[alias_key]
      ('tokens', [tokens]) -> caller builds token_where()/token_rank()

    Never raises; an empty query yields ('tokens', []).
    """
    ql = (query or '').strip().lower()
    if not ql:
        return ('tokens', [])
    if market_aliases and ql in market_aliases:
        return ('alias', ql)
    toks = meaningful_tokens(query)
    reduced = ' '.join(toks)
    if market_aliases and reduced and reduced in market_aliases:
        return ('alias', reduced)
    return ('tokens', toks)


def token_where(tokens, columns):
    """Build an OR-any-token WHERE fragment + its params.

    Result matches a row if ANY token appears (ILIKE substring) in ANY column:
        ((c1 ILIKE %s OR c2 ILIKE %s ...)  -- token1
      OR (c1 ILIKE %s OR c2 ILIKE %s ...)) -- token2

    `columns` are trusted, code-supplied identifiers (never user input); the
    token VALUES are parameterized, so this is injection-safe.
    Returns ('', []) when there are no tokens (caller adds no condition).
    """
    if not tokens or not columns:
        return '', []
    groups = []
    params = []
    col_expr = ' OR '.join('{} ILIKE %s'.format(c) for c in columns)
    for tok in tokens:
        like = '%{}%'.format(tok)
        groups.append('(' + col_expr + ')')
        params.extend([like] * len(columns))
    return '(' + ' OR '.join(groups) + ')', params


def token_rank(tokens, weights=SEARCH_FIELD_WEIGHTS):
    """Build a relevance SQL expression + params for ORDER BY <expr> DESC.

    Score = sum over (token, field) of `weight` when the token hits that field,
    so a row matching more tokens in higher-weighted fields ranks first.
    Returns ('', []) when there are no tokens.
    """
    if not tokens or not weights:
        return '', []
    parts = []
    params = []
    for tok in tokens:
        like = '%{}%'.format(tok)
        for col, wt in weights:
            parts.append('(CASE WHEN {} ILIKE %s THEN {} ELSE 0 END)'.format(col, int(wt)))
            params.append(like)
    return ' + '.join(parts), params
