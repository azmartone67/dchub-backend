"""util/testimonial_sources.py — keep HUMAN customer quotes out of AI feeds.

The ai_testimonials table holds two different things:

* AI-assistant quotes and citations: source 'seed', 'verified', 'probe_*',
  'mcp-auto', 'auto' and so on. Every public "What AI agents say" surface,
  every AI citation count, and the platform counts read these.
* HUMAN customer quotes: source = 'claim_quote'. For these rows agent_name
  holds the PERSON's name and context holds their COMPANY. They are captured
  by POST /api/v1/keys/claim/quote and the /api/v1/keys/identify flow
  (flask_mcp_endpoints.py), or inserted by hand for a curated customer, and
  published only in the /cited-by "What customers say" section
  (routes/cited_by.py) after a human approves them.

A reader that presents rows as AI quotes, or counts them as AI citations or
platforms, must exclude claim_quote rows, or an approved customer shows up as
an "AI agent". Put NOT_CLAIM_QUOTE_SQL in its WHERE clause. It is NULL-safe
(a NULL source is kept, as the writers that omit it get the column default
'auto') and has no '%', so it is safe in queries with or without params.

tests/test_claim_quote_fence_guard.py fails when a non-test module reads
FROM ai_testimonials without this fence and is not on its allowlist.
"""

CLAIM_QUOTE_SOURCE = "claim_quote"

# The WHERE-clause predicate. Unqualified column name: every reader today
# selects from ai_testimonials without an alias.
NOT_CLAIM_QUOTE_SQL = "COALESCE(source, '') <> 'claim_quote'"
