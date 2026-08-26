"""Agent-concierge intent→recipe matching — hot-path hardening tests
(fix-rag-gap-concierge, 2026-07-04).

Covers the reviewer-blocker fixes in routes/agent_concierge.py — the
PUBLIC unauthenticated /api/v1/agent/solve path must never make an
unbounded synchronous Cohere call:

  (a) KEYWORD-FIRST   — a keyword hit makes ZERO embed calls
  (b) SEMANTIC BACKSTOP — keyword miss + embed ok → semantic result
  (c) CIRCUIT BREAKER — 3 consecutive embed failures open the breaker;
                        subsequent calls skip Cohere until cooldown
  (d) LRU             — a repeat question never re-embeds
  (e) timeout         — the local embed helper passes timeout=4 (never
                        brain_rag's 30)

No network, no DB — urllib.request.urlopen is mocked at the module
level so the real _cohere_embed body (incl. breaker accounting) runs.

Run with:  python3 -m pytest tests/test_agent_concierge_matching.py -v
"""
import io
import json
import time

import pytest

import routes.agent_concierge as ac


# ── fakes ─────────────────────────────────────────────────────────────

class FakeEmbedServer:
    """Stands in for urllib.request.urlopen against api.cohere.ai.

    Batch calls (len(texts) > 1, the cookbook build) return orthogonal
    unit basis vectors e_0..e_{n-1}. Single-text calls (the query
    embed) return e_{target_ix}, so cosine == 1.0 for _COOKBOOK[target_ix]
    and 0.0 for every other recipe.
    """

    def __init__(self, target_ix=0, fail=False, dim=None):
        self.calls = []           # list of (n_texts, timeout)
        self.target_ix = target_ix
        self.fail = fail
        self.dim = dim or len(ac._COOKBOOK)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        texts = body["texts"]
        self.calls.append((len(texts), timeout))
        if self.fail:
            raise OSError("embed backend down")
        if len(texts) > 1:
            vecs = [[1.0 if j == i else 0.0 for j in range(self.dim)]
                    for i in range(len(texts))]
        else:
            vecs = [[1.0 if j == self.target_ix else 0.0
                     for j in range(self.dim)]]
        payload = json.dumps({"embeddings": vecs}).encode()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(payload)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Reset all module-level caches/breaker state around every test."""
    monkeypatch.setenv("COHERE_API_KEY", "test-key")
    monkeypatch.delenv("CONCIERGE_MATCH_COSINE", raising=False)
    monkeypatch.delenv("CONCIERGE_BREAKER_COOLDOWN", raising=False)
    ac._RECIPE_VEC_CACHE = None
    ac._MATCH_LRU.clear()
    ac._embed_fail_count = 0
    ac._breaker_open_until = 0.0
    yield
    ac._RECIPE_VEC_CACHE = None
    ac._MATCH_LRU.clear()
    ac._embed_fail_count = 0
    ac._breaker_open_until = 0.0


# A query that misses every cookbook keyword (asserted in the tests
# that rely on it) but is a clear semantic sibling of recipe 0.
_KEYWORD_MISS_QUERY = "optimal geography for locating a giant GPU campus"


# ── (a) keyword hit → zero embed calls ────────────────────────────────

def test_keyword_hit_makes_zero_embed_calls(monkeypatch):
    fake = FakeEmbedServer()
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)
    hit = ac._match_recipe("What's the DCPI verdict for Ashburn?")
    assert hit is not None
    assert hit["id"] == "dcpi-verdict-single-market"
    assert fake.calls == [], "keyword hit must never touch Cohere"


def test_keyword_hit_immune_to_embed_outage(monkeypatch):
    # Even with the embed backend hard-down, keyword hits behave
    # exactly as before the semantic layer existed.
    fake = FakeEmbedServer(fail=True)
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)
    hit = ac._match_recipe("Compare behind the meter gas vs grid for Texas")
    assert hit is not None and hit["id"] == "gas-vs-grid-economics"
    assert fake.calls == []


# ── (b) keyword miss + embed ok → semantic result ─────────────────────

def test_keyword_miss_falls_through_to_semantic(monkeypatch):
    assert ac._match_recipe_keyword(_KEYWORD_MISS_QUERY) is None, \
        "test query must miss the keyword layer"
    fake = FakeEmbedServer(target_ix=0)
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)
    hit = ac._match_recipe(_KEYWORD_MISS_QUERY)
    assert hit is not None
    assert hit["id"] == ac._COOKBOOK[0]["id"]
    # Exactly two embed calls: the ~30-text cookbook batch + the query.
    assert [n for n, _ in fake.calls] == [len(ac._COOKBOOK), 1]


def test_embed_uses_short_timeout(monkeypatch):
    fake = FakeEmbedServer(target_ix=0)
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)
    ac._match_recipe(_KEYWORD_MISS_QUERY)
    assert fake.calls, "semantic path should have embedded"
    for _, timeout in fake.calls:
        assert timeout == 4, f"hot-path embed must use timeout=4, got {timeout}"


def test_below_threshold_cosine_returns_no_match(monkeypatch):
    # Query vector orthogonal to every recipe vector → best cosine 0.0
    # < 0.55 threshold → the /solve fallback (None) — never a random hit.
    class OrthogonalQuery(FakeEmbedServer):
        def __init__(self, dim):
            super().__init__(dim=dim)

        def __call__(self, req, timeout=None):
            body = json.loads(req.data.decode())
            texts = body["texts"]
            self.calls.append((len(texts), timeout))
            if len(texts) > 1:
                vecs = [[1.0 if j == i else 0.0 for j in range(self.dim)]
                        for i in range(len(texts))]
            else:  # query → last axis, unused by any recipe
                vecs = [[1.0 if j == self.dim - 1 else 0.0
                         for j in range(self.dim)]]
            payload = json.dumps({"embeddings": vecs}).encode()

            class _Resp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _Resp(payload)

    fake = OrthogonalQuery(dim=len(ac._COOKBOOK) + 1)
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)
    assert ac._match_recipe(_KEYWORD_MISS_QUERY) is None


# ── (c) circuit breaker ───────────────────────────────────────────────

def test_breaker_opens_after_three_failures_and_cooldown_gates(monkeypatch):
    monkeypatch.setenv("CONCIERGE_BREAKER_COOLDOWN", "300")
    fake = FakeEmbedServer(fail=True)
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)

    # 3 keyword-miss requests, each attempting (and failing) the
    # cookbook batch embed.
    for _ in range(3):
        assert ac._match_recipe_semantic(_KEYWORD_MISS_QUERY) is None
    assert len(fake.calls) == 3
    assert ac._breaker_open_until > time.time(), "breaker must be OPEN"
    # Cooldown honors the env var (~300s from now).
    assert 290 < ac._breaker_open_until - time.time() <= 300.5

    # While open: no Cohere traffic at all.
    for _ in range(5):
        assert ac._match_recipe_semantic(_KEYWORD_MISS_QUERY) is None
    assert len(fake.calls) == 3, "open breaker must skip embeds entirely"

    # Full /solve-path behavior stays keyword-only while open.
    hit = ac._match_recipe("What's the DCPI verdict for Ashburn?")
    assert hit is not None and hit["id"] == "dcpi-verdict-single-market"
    assert len(fake.calls) == 3

    # After cooldown expiry the semantic path retries.
    ac._breaker_open_until = time.time() - 1
    assert ac._match_recipe_semantic(_KEYWORD_MISS_QUERY) is None
    assert len(fake.calls) == 4, "expired breaker must allow a retry"


def test_breaker_needs_consecutive_failures(monkeypatch):
    # fail, fail, SUCCESS, ... , fail → never 3 consecutive → shut.
    ok = FakeEmbedServer(target_ix=0)
    bad = FakeEmbedServer(fail=True)
    seq = [bad, bad, ok, ok, bad]   # batch, batch, batch, query, query

    def dispatch(req, timeout=None):
        server = seq.pop(0) if seq else ok
        return server(req, timeout=timeout)

    monkeypatch.setattr(ac.urllib.request, "urlopen", dispatch)
    ac._match_recipe_semantic(_KEYWORD_MISS_QUERY)   # batch bad (fail 1)
    ac._match_recipe_semantic(_KEYWORD_MISS_QUERY)   # batch bad (fail 2)
    ac._match_recipe_semantic(_KEYWORD_MISS_QUERY)   # batch ok → reset,
    #                                                  query ok → still 0
    assert ac._embed_fail_count == 0
    ac._match_recipe_semantic(_KEYWORD_MISS_QUERY)   # cached batch;
    #                                                  query bad (fail 1)
    assert ac._embed_fail_count == 1
    assert not ac._breaker_open()


# ── (d) LRU ───────────────────────────────────────────────────────────

def test_lru_hit_makes_no_second_embed(monkeypatch):
    fake = FakeEmbedServer(target_ix=0)
    monkeypatch.setattr(ac.urllib.request, "urlopen", fake)

    first = ac._match_recipe(_KEYWORD_MISS_QUERY)
    assert first is not None
    assert len(fake.calls) == 2          # batch + query

    # Same question again (case/whitespace-normalized) → LRU, no embeds.
    second = ac._match_recipe("  Optimal   geography for locating a "
                              "giant GPU campus ")
    assert second is first
    assert len(fake.calls) == 2, "repeat question must not re-embed"


def test_lru_caches_keyword_hits_and_caps_at_200():
    for i in range(ac._MATCH_LRU_CAP + 40):
        ac._lru_put(f"q{i}", ac._COOKBOOK[0])
    assert len(ac._MATCH_LRU) == ac._MATCH_LRU_CAP
    assert "q0" not in ac._MATCH_LRU          # oldest evicted
    assert f"q{ac._MATCH_LRU_CAP + 39}" in ac._MATCH_LRU


# ── threshold env semantics preserved ────────────────────────────────

def test_match_cosine_env_still_honored(monkeypatch):
    monkeypatch.setenv("CONCIERGE_MATCH_COSINE", "0.99")
    assert ac._concierge_match_cosine() == 0.99
    monkeypatch.setenv("CONCIERGE_MATCH_COSINE", "not-a-float")
    assert ac._concierge_match_cosine() == 0.55


# ── /agent landing renders canon counts (2026-07-30) ─────────────────
#
# The landing page sat on a stale hardcoded tool count for weeks (title said
# one retired count, ChatGPT's cached citation card an even older one) because
# nothing asserted the RENDERED body against ai_surface_canon. The fence
# (tests/test_canonical_counts_drift.py) now scans this module's SOURCE for
# re-hardcodes; this test covers the other half — the substitution actually
# runs, on the body an agent receives.

def test_agent_landing_renders_canon_counts():
    from ai_surface_canon import PINNED

    body = ac.agent_landing().get_data(as_text=True)

    # Every placeholder substituted — a dropped .replace() is exactly how a
    # computed fix gets silently discarded.
    assert "{canon_" not in body
    assert "{cookbook_html}" not in body

    tools = PINNED["tools_advertised"]
    assert f"— {tools} tools cited by" in body, "<title> must carry canon count"
    assert f"<h1>{tools} tools, built for agents.</h1>" in body
    # ★2026-08-25: assert the DERIVED value, not the pin. canon_nums() prefers
    # canonical_stats' last-known-good and falls back to PINNED, so these agree
    # here (CI has no DB) but diverge in production — where this page used to
    # serve the stale pin while every sibling surface had healed. Asserting the
    # literal would keep passing through exactly that divergence.
    from ai_surface_canon import canon_nums
    nums = canon_nums()
    for key in ("facilities", "markets", "countries", "deals"):
        want = nums["{canon_%s}" % key]
        assert want and want in body, f"canon public[{key!r}] missing from /agent body"

    # The retired literals this fix removed must never reappear.
    for stale in ("73 tools", "48 tools", "12,650+", "180 countries",
                  "311 DCPI", "4,000+"):
        assert stale not in body, f"retired literal {stale!r} back on /agent"


def test_agent_cookbook_copy_renders_canon_counts():
    """The find-facilities recipe's why/sample_answer are agent-served copy
    (via /api/v1/agent/solve and /api/v1/agent/cookbook) — they must follow
    canon too, not carry their own frozen floor."""
    from ai_surface_canon import PINNED

    blob = json.dumps(ac._COOKBOOK)
    assert PINNED["public"]["facilities"] in blob
    assert PINNED["public"]["deals"] in blob
    for stale in ("12,650+", "180 countries", "4,000+"):
        assert stale not in blob
