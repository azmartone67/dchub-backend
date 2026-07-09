"""Semantic answer cache + verification pass (feat-cache-verify 2026-07-04).

Covers routes/brain_answer_cache.py and its deal_autopsy wiring:

  Part A — cache:
    (1) ★tier isolation — an anon-tier cached answer NEVER serves a paid
        caller and vice versa (cross-tier cache poisoning trap)
    (2) the 0.97 cosine near-duplicate threshold (miss below, hit above)
    (3) TTL expiry (default 3600s; env override honored)
    (4) per-tool row cap eviction (delete-oldest)
    (5) fail-soft → miss on: no DB, DB raising, embed down, cursor blowing up
    (6) ANSWER_CACHE_DISABLE kill switch
    (6b) ★params_hash EXACT equality — limit=3 vs limit=15 NEVER collide
         (exact_only mode: no embedding at all; semantic mode: identical
         embeddings still can't cross a params mismatch)

  Part B — verifier:
    (7) flags a fabricated number ({ok:false, issues[]}) and
        strip_flagged_sentences removes ONLY the explicitly-flagged sentence
        — a TRUE sentence sharing a digit sequence with a flagged number
        survives (no raw number-substring fallback); unlocatable flagged
        sentences → flag-not-strip
    (8) verifier timeout → served unverified (verified:false), 6s cap passed
    (9) VERIFY_MAX_PER_HOUR budget (second call over a cap of 1 skips)
   (10) ANSWER_VERIFY_DISABLE kill switch (no API call made)
   (11) no-numbers answers skip the API call entirely (trivially ok)

  Wiring (Flask test client, everything heavy mocked):
   (12) deal_autopsy cache HIT short-circuits compute
   (13) deal_autopsy MISS computes, stores per-tier under the EXACT
        params key
   (14) ★deal_autopsy makes ZERO verifier calls — it is deterministic
        template output; verification applies only to LLM-composed answers
        (get_dchub_recommendation)

No network, no real DB — _db/_embed_query/_post_messages are mocked.
Run with:  python3 -m pytest tests/test_answer_cache_verify.py -v
"""
import json
import math
import time

import pytest

import routes.brain_answer_cache as bac


# ── fakes ─────────────────────────────────────────────────────────────
def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def vec_at(c):
    """2-d unit vector whose cosine against [1, 0] is exactly c."""
    return [c, math.sqrt(max(0.0, 1.0 - c * c))]


class FakeAnswerCacheDB:
    """In-memory answer_cache table understanding the module's SQL shapes."""

    def __init__(self):
        self.rows = []  # {id, tool, tier, params_hash, query_text, emb, answer, created_ts}
        self._id = 0
        self.fail_execute = False

    def add(self, tool, tier, emb, answer, created_ts=None, query_text="",
            params_hash=None):
        # Default params_hash = the module's empty-params hash, so rows
        # seeded directly by tests match default (params=None) lookups.
        self._id += 1
        self.rows.append({
            "id": self._id, "tool": tool, "tier": tier,
            "params_hash": bac._params_hash(None) if params_hash is None else params_hash,
            "query_text": query_text,
            "emb": list(emb) if emb is not None else None,
            "answer": answer,
            "created_ts": time.time() if created_ts is None else created_ts,
        })


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.db.fail_execute:
            raise RuntimeError("boom")
        s = " ".join(sql.split()).lower()
        self._one = None
        if s.startswith("create") or s.startswith("alter"):
            return
        if s.startswith("insert into answer_cache"):
            tool, tier, ph, q, vec_s, answer_json = params
            self.db.add(tool, tier,
                        json.loads(vec_s) if vec_s is not None else None,
                        json.loads(answer_json), query_text=q, params_hash=ph)
            return
        if s.startswith("delete from answer_cache"):
            tool, tool2, cap = params
            assert tool == tool2
            mine = [r for r in self.db.rows if r["tool"] == tool]
            mine.sort(key=lambda r: (r["created_ts"], r["id"]), reverse=True)
            keep_ids = {r["id"] for r in mine[:int(cap)]}
            self.db.rows = [r for r in self.db.rows
                            if r["tool"] != tool or r["id"] in keep_ids]
            return
        if s.startswith("select answer") and "query_embedding <=>" in s:
            # semantic mode: exact (tool, tier, params_hash), ranked by cosine
            qs, tool, tier, ph, _qs2 = params
            qv = json.loads(qs)
            cands = [r for r in self.db.rows
                     if r["tool"] == tool and r["tier"] == tier
                     and r["params_hash"] == ph and r["emb"] is not None]
            if not cands:
                return
            best = max(cands, key=lambda r: _cos(r["emb"], qv))
            self._one = (best["answer"], _cos(best["emb"], qv),
                         time.time() - best["created_ts"])
            return
        if s.startswith("select answer"):
            # exact_only mode: exact (tool, tier, params_hash), newest first
            tool, tier, ph = params
            cands = [r for r in self.db.rows
                     if r["tool"] == tool and r["tier"] == tier
                     and r["params_hash"] == ph]
            if not cands:
                return
            best = max(cands, key=lambda r: (r["created_ts"], r["id"]))
            self._one = (best["answer"], time.time() - best["created_ts"])
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def db(monkeypatch):
    d = FakeAnswerCacheDB()
    monkeypatch.setattr(bac, "_db", lambda: FakeConn(d))
    monkeypatch.setattr(bac, "_embed_query", lambda t: [1.0, 0.0])
    return d


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    bac._reset_for_tests()
    for var in ("ANSWER_CACHE_DISABLE", "ANSWER_CACHE_COSINE",
                "ANSWER_CACHE_TTL_S", "ANSWER_CACHE_MAX_ROWS",
                "ANSWER_VERIFY_DISABLE", "VERIFY_MAX_PER_HOUR",
                "VERIFY_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    yield


# ── (1) ★ tier isolation ──────────────────────────────────────────────
def test_tier_isolation_anon_never_serves_paid(db):
    assert bac.put_cached("deal_autopsy", "FREE", "recent deals",
                          {"ok": True, "tier": "FREE"}) is True
    # Same near-duplicate question from a PAID caller → MUST miss.
    assert bac.get_cached("deal_autopsy", "PRO", "recent deals") is None
    # Same tier → hit.
    hit = bac.get_cached("deal_autopsy", "FREE", "recent deals")
    assert hit and hit["tier"] == "FREE"
    # Reverse direction: paid cached answer never serves anon.
    assert bac.put_cached("deal_autopsy", "PRO", "recent deals",
                          {"ok": True, "tier": "PRO"}) is True
    anon = bac.get_cached("deal_autopsy", "FREE", "recent deals")
    assert anon and anon["tier"] == "FREE"
    paid = bac.get_cached("deal_autopsy", "PRO", "recent deals")
    assert paid and paid["tier"] == "PRO"


def test_tier_key_case_normalized_but_never_crossed(db):
    assert bac.put_cached("t", "Pro", "q", {"tier": "PRO"}) is True
    assert bac.get_cached("t", "PRO", "q") == {"tier": "PRO"}   # same bucket
    assert bac.get_cached("t", "FREE", "q") is None             # different tier
    # Empty/None tier is un-cacheable — must not fall into any bucket.
    assert bac.put_cached("t", "", "q", {"x": 1}) is False
    assert bac.get_cached("t", None, "q") is None


# ── (2) cosine threshold ──────────────────────────────────────────────
def test_cosine_threshold_097(db, monkeypatch):
    db.add("tool", "FREE", [1.0, 0.0], {"a": 1})
    monkeypatch.setattr(bac, "_embed_query", lambda t: vec_at(0.96))
    assert bac.get_cached("tool", "FREE", "similar-ish question") is None
    monkeypatch.setattr(bac, "_embed_query", lambda t: vec_at(0.98))
    assert bac.get_cached("tool", "FREE", "near duplicate question") == {"a": 1}


def test_cosine_threshold_env_override(db, monkeypatch):
    db.add("tool", "FREE", [1.0, 0.0], {"a": 1})
    monkeypatch.setattr(bac, "_embed_query", lambda t: vec_at(0.90))
    assert bac.get_cached("tool", "FREE", "q") is None
    monkeypatch.setenv("ANSWER_CACHE_COSINE", "0.85")
    assert bac.get_cached("tool", "FREE", "q") == {"a": 1}


# ── (3) TTL expiry ────────────────────────────────────────────────────
def test_ttl_expiry(db, monkeypatch):
    db.add("tool", "FREE", [1.0, 0.0], {"a": 1},
           created_ts=time.time() - 7200)  # 2h old
    assert bac.get_cached("tool", "FREE", "q") is None      # default TTL 3600
    monkeypatch.setenv("ANSWER_CACHE_TTL_S", "10000")
    assert bac.get_cached("tool", "FREE", "q") == {"a": 1}  # within longer TTL


# ── (4) per-tool cap eviction ─────────────────────────────────────────
def test_row_cap_evicts_oldest(db, monkeypatch):
    monkeypatch.setenv("ANSWER_CACHE_MAX_ROWS", "3")
    base = time.time()
    ts = iter(range(100))
    real_add = db.add
    monkeypatch.setattr(db, "add", lambda tool, tier, emb, answer, created_ts=None,
                        query_text="", params_hash=None: real_add(
                            tool, tier, emb, answer,
                            created_ts=base + next(ts),
                            query_text=query_text, params_hash=params_hash))
    for i in range(5):
        assert bac.put_cached("toolX", "FREE", f"q{i}", {"i": i}) is True
    mine = [r for r in db.rows if r["tool"] == "toolX"]
    assert len(mine) == 3
    assert sorted(r["answer"]["i"] for r in mine) == [2, 3, 4]  # oldest gone
    # Other tools untouched by toolX's cap.
    assert bac.put_cached("toolY", "FREE", "q", {"y": 1}) is True
    assert len([r for r in db.rows if r["tool"] == "toolY"]) == 1


# ── (5) fail-soft miss ────────────────────────────────────────────────
def test_fail_soft_no_db(monkeypatch):
    monkeypatch.setattr(bac, "_embed_query", lambda t: [1.0, 0.0])
    monkeypatch.setattr(bac, "_db", lambda: None)
    assert bac.get_cached("t", "FREE", "q") is None
    assert bac.put_cached("t", "FREE", "q", {"a": 1}) is False


def test_fail_soft_db_raises(monkeypatch):
    monkeypatch.setattr(bac, "_embed_query", lambda t: [1.0, 0.0])

    def _boom():
        raise RuntimeError("pg down")
    monkeypatch.setattr(bac, "_db", _boom)
    assert bac.get_cached("t", "FREE", "q") is None
    assert bac.put_cached("t", "FREE", "q", {"a": 1}) is False


def test_fail_soft_embed_down(db, monkeypatch):
    db.add("t", "FREE", [1.0, 0.0], {"a": 1})
    monkeypatch.setattr(bac, "_embed_query", lambda t: None)
    assert bac.get_cached("t", "FREE", "q") is None
    assert bac.put_cached("t", "FREE", "q", {"a": 1}) is False


def test_fail_soft_cursor_blows_up(db):
    db.add("t", "FREE", [1.0, 0.0], {"a": 1})
    db.fail_execute = True
    assert bac.get_cached("t", "FREE", "q") is None
    assert bac.put_cached("t", "FREE", "q2", {"a": 2}) is False


# ── (6) cache kill switch ─────────────────────────────────────────────
def test_cache_kill_switch(db, monkeypatch):
    db.add("t", "FREE", [1.0, 0.0], {"a": 1})
    monkeypatch.setenv("ANSWER_CACHE_DISABLE", "1")
    assert bac.get_cached("t", "FREE", "q") is None
    assert bac.put_cached("t", "FREE", "q2", {"a": 2}) is False
    assert len(db.rows) == 1  # nothing written while disabled


# ── (6b) ★ params_hash exact equality ─────────────────────────────────
def test_autopsy_limit3_vs_limit15_never_collide(db, monkeypatch):
    """The reviewer blocker: 'deal autopsy limit=3' vs 'limit=15' sit at
    ~1.0 cosine as strings — only exact params_hash equality keeps them
    apart. exact_only mode must also never touch the embedder."""
    def _no_embed(t):
        raise AssertionError("exact_only caching must never embed")
    monkeypatch.setattr(bac, "_embed_query", _no_embed)
    assert bac.put_cached("deal_autopsy", "PRO", "deal autopsy",
                          {"ok": True, "count": 3},
                          params={"limit": 3}, exact_only=True) is True
    assert bac.put_cached("deal_autopsy", "PRO", "deal autopsy",
                          {"ok": True, "count": 15},
                          params={"limit": 15}, exact_only=True) is True
    got3 = bac.get_cached("deal_autopsy", "PRO", "deal autopsy",
                          params={"limit": 3}, exact_only=True)
    got15 = bac.get_cached("deal_autopsy", "PRO", "deal autopsy",
                           params={"limit": 15}, exact_only=True)
    assert got3 == {"ok": True, "count": 3}
    assert got15 == {"ok": True, "count": 15}
    # A limit that was never cached is a MISS, not someone else's answer.
    assert bac.get_cached("deal_autopsy", "PRO", "deal autopsy",
                          params={"limit": 5}, exact_only=True) is None


def test_params_mismatch_beats_identical_embeddings_semantic_mode(db):
    """Even in semantic mode, a params mismatch is a hard miss — cosine 1.0
    (the db fixture embeds everything to [1,0]) must not rescue it."""
    assert bac.put_cached("t", "FREE", "q", {"a": 1}, params={"x": 1}) is True
    assert bac.get_cached("t", "FREE", "q", params={"x": 2}) is None
    assert bac.get_cached("t", "FREE", "q", params={"x": 1}) == {"a": 1}


def test_params_hash_is_order_insensitive_and_stable():
    assert bac._params_hash({"a": 1, "b": 2}) == bac._params_hash({"b": 2, "a": 1})
    assert bac._params_hash(None) == bac._params_hash({})
    assert bac._params_hash({"limit": 3}) != bac._params_hash({"limit": 15})


# ── (7) verifier flags a fabricated number ────────────────────────────
ANSWER_3_SENT = ("The play is a land bank. DCPI rates Atlanta AVOID "
                 "(~48mo to power). Smart money banks the site.")


def test_verifier_flags_fabricated_number(monkeypatch):
    seen = {}

    def fake_post(body, timeout):
        seen["body"] = body
        return {"content": [{"type": "text", "text": json.dumps({
            "ok": False,
            "issues": [{"number": "48",
                        "sentence": "DCPI rates Atlanta AVOID (~48mo to power)."}],
        })}]}
    monkeypatch.setattr(bac, "_post_messages", fake_post)
    v = bac.verify_answer(ANSWER_3_SENT, "facts: Atlanta is AVOID. no months given")
    assert v["verified"] is True
    assert v["ok"] is False
    assert len(v["issues"]) == 1
    # Structured outputs were requested (haiku-4-5 supports them).
    assert "output_config" in seen["body"]
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    # And the offending sentence strips cleanly.
    clean, n = bac.strip_flagged_sentences(ANSWER_3_SENT, v["issues"])
    assert n == 1
    assert "48" not in clean
    assert "land bank" in clean and "Smart money" in clean


def test_strip_flags_instead_of_blanking(monkeypatch):
    text = "Everything here says 999 MW."
    issues = [{"number": "999", "sentence": text}]
    flagged, n = bac.strip_flagged_sentences(text, issues)
    assert n == 0
    assert text in flagged and flagged.startswith("⚠️")  # caveat, not empty


def test_true_sentence_sharing_digits_with_flagged_number_survives():
    """★ Reviewer blocker: NO raw number-substring fallback. '1,480 MW'
    contains the digits '48' — under the old fallback the TRUE Dallas
    sentence was stripped alongside the flagged Atlanta one."""
    text = ("Queue wait is 48 months in Atlanta. "
            "Dallas adds 1,480 MW of new capacity. "
            "The verdict is AVOID.")
    issues = [{"number": "48",
               "sentence": "Queue wait is 48 months in Atlanta."}]
    clean, n = bac.strip_flagged_sentences(text, issues)
    assert n == 1
    assert "Queue wait" not in clean            # flagged sentence gone
    assert "1,480 MW" in clean                  # digit-sharing TRUE sentence survives
    assert "The verdict is AVOID." in clean


def test_unlocatable_flagged_sentence_flags_not_strips():
    """If none of the verifier's quoted sentences can be located (normalized
    prefix match), nothing is stripped — the answer ships with the caveat."""
    text = "Atlanta is AVOID. Time to power is 48 months."
    issues = [{"number": "99", "sentence": "a sentence that is not in the text 99"}]
    flagged, n = bac.strip_flagged_sentences(text, issues)
    assert n == 0
    assert flagged.startswith("⚠️") and text in flagged
    # Same flag-not-strip when the issue carries no quotable sentence at all.
    flagged2, n2 = bac.strip_flagged_sentences(text, [{"number": "48", "sentence": ""}])
    assert n2 == 0 and flagged2.startswith("⚠️") and text in flagged2


def test_strip_matches_by_normalized_prefix_not_substring():
    """A truncated verifier quote (prefix) still locates its sentence, but a
    mid-sentence fragment must NOT — only prefix-anchored fuzzy matches."""
    text = ("DCPI rates Atlanta AVOID with a long queue. "
            "Smart money banks the site.")
    # Truncated prefix quote → matches, strips exactly that sentence.
    clean, n = bac.strip_flagged_sentences(
        text, [{"number": "1", "sentence": "DCPI rates Atlanta AVOID"}])
    assert n == 1 and "Smart money banks the site." == clean
    # Mid-sentence fragment → no prefix anchor → flag-not-strip.
    flagged, n2 = bac.strip_flagged_sentences(
        text, [{"number": "1", "sentence": "Atlanta AVOID with a long queue"}])
    assert n2 == 0 and flagged.startswith("⚠️") and text in flagged


# ── (8) verifier timeout → serve unverified ───────────────────────────
def test_verifier_timeout_serves_unverified(monkeypatch):
    def fake_post(body, timeout):
        assert timeout == 6.0  # the 6s cap
        raise TimeoutError("timed out")
    monkeypatch.setattr(bac, "_post_messages", fake_post)
    v = bac.verify_answer("we quote 42 MW", "facts")
    assert v["verified"] is False


# ── (9) hourly budget ─────────────────────────────────────────────────
def test_verify_hourly_budget(monkeypatch):
    monkeypatch.setenv("VERIFY_MAX_PER_HOUR", "1")
    monkeypatch.setattr(bac, "_post_messages", lambda body, timeout: {
        "content": [{"type": "text",
                     "text": json.dumps({"ok": True, "issues": []})}]})
    v1 = bac.verify_answer("42 MW", "42 MW confirmed")
    assert v1["verified"] is True and v1["ok"] is True
    v2 = bac.verify_answer("42 MW", "42 MW confirmed")
    assert v2["verified"] is False and v2["reason"] == "over_budget"


# ── (10) verifier kill switch ─────────────────────────────────────────
def test_verify_kill_switch(monkeypatch):
    monkeypatch.setenv("ANSWER_VERIFY_DISABLE", "1")

    def fake_post(body, timeout):
        raise AssertionError("must not call the API while disabled")
    monkeypatch.setattr(bac, "_post_messages", fake_post)
    v = bac.verify_answer("42 MW", "42 MW")
    assert v == {"verified": False, "reason": "disabled"}


# ── (11) no numbers → trivially ok, zero spend ────────────────────────
def test_verify_skips_numberless_answers(monkeypatch):
    def fake_post(body, timeout):
        raise AssertionError("must not spend on a numberless answer")
    monkeypatch.setattr(bac, "_post_messages", fake_post)
    v = bac.verify_answer("A build-rated market with real headroom.", "facts")
    assert v["verified"] is True and v["ok"] is True and v.get("trivial")


# ── wiring: deal_autopsy route ────────────────────────────────────────
def _autopsy_app():
    from flask import Flask
    import routes.deal_autopsy as da
    app = Flask(__name__)
    app.register_blueprint(da.deal_autopsy_bp)
    return app, da


DEAL = {"id": 7, "date": "2026-06-01", "buyer": "BuyerCo", "seller": "SellerCo",
        "value": 2.5e9, "type": "acquisition", "region": "Georgia",
        "market": "Atlanta", "source_url": "https://x.example/deal"}

MK = {"market_slug": "atlanta", "market_name": "Atlanta", "state": "GA",
      "iso": "SOCO", "constraint_score": 60, "excess_power_score": 20,
      "time_to_power_months": 48, "verdict": "AVOID", "composite_score": 30}


def test_deal_autopsy_cache_hit_short_circuits(monkeypatch):
    app, da = _autopsy_app()
    seen = {}

    def _fake_get(tool, tier, q, params=None, exact_only=False):
        seen.update(tool=tool, params=params, exact_only=exact_only)
        return {"ok": True, "deals": [], "count": 0, "tier": "FREE"}
    monkeypatch.setattr("routes.brain_answer_cache.get_cached", _fake_get)

    def _no_compute(limit):
        raise AssertionError("compute ran despite cache hit")
    monkeypatch.setattr(da, "_recent_deals", _no_compute)
    r = app.test_client().get("/api/v1/deal-autopsy")
    assert r.status_code == 200
    j = r.get_json()
    assert j["_cache"]["hit"] is True and j["ok"] is True
    # The lookup is EXACT-key: structural params, exact_only mode.
    assert seen["exact_only"] is True and seen["params"] == {"limit": 15, "comps": "none"}


def test_deal_autopsy_miss_computes_and_stores_exact_key(monkeypatch):
    app, da = _autopsy_app()
    stored = {}
    monkeypatch.setattr("routes.brain_answer_cache.get_cached",
                        lambda tool, tier, q, params=None, exact_only=False: None)

    def _capture_put(tool, tier, q, answer, params=None, exact_only=False):
        stored.update(tool=tool, tier=tier, q=q, answer=answer,
                      params=params, exact_only=exact_only)
        return True
    monkeypatch.setattr("routes.brain_answer_cache.put_cached", _capture_put)
    monkeypatch.setattr(da, "_recent_deals", lambda limit: [dict(DEAL)])
    monkeypatch.setattr(da, "_market_index", lambda: ({}, {}))
    monkeypatch.setattr(da, "_match_market", lambda d, idx, st: None)
    r = app.test_client().get("/api/v1/deal-autopsy?limit=5")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["count"] == 1
    assert "_cache" not in j
    # Anonymous caller = FREE tier: cached under FREE, teaser envelope
    # intact, EXACT structural key (limit is params, not free text), and no
    # verification meta — deal_autopsy is deterministic, never verified.
    assert stored["tool"] == "deal_autopsy" and stored["tier"] == "FREE"
    assert stored["params"] == {"limit": 5, "comps": "none"} and stored["exact_only"] is True
    assert stored["answer"]["autopsy"]["locked"] is True
    assert "verification" not in stored["answer"]


def test_deal_autopsy_makes_zero_verifier_calls(monkeypatch):
    """★ Reviewer blocker: deal_autopsy is DETERMINISTIC template output —
    every number comes from SQL, so it must NEVER invoke the LLM verifier
    (pure waste + false-positive risk). Paid tier, full read, zero calls."""
    app, da = _autopsy_app()
    import routes.tier_gate as tg
    import routes.brain_answer_cache as _bac
    monkeypatch.setattr(tg, "_resolve_caller_tier", lambda: ("PRO", {}))
    monkeypatch.setattr("routes.brain_answer_cache.get_cached",
                        lambda tool, tier, q, params=None, exact_only=False: None)
    monkeypatch.setattr("routes.brain_answer_cache.put_cached",
                        lambda tool, tier, q, answer, params=None,
                        exact_only=False: True)
    monkeypatch.setattr(da, "_recent_deals", lambda limit: [dict(DEAL)])
    monkeypatch.setattr(da, "_market_index", lambda: ({}, {}))
    monkeypatch.setattr(da, "_match_market", lambda d, idx, st: dict(MK))
    monkeypatch.setattr(da, "_rag_comparables", lambda d, mk: [])

    def _no_verify(*a, **k):
        raise AssertionError("deal_autopsy must make ZERO verifier calls")
    monkeypatch.setattr(_bac, "verify_answer", _no_verify)
    monkeypatch.setattr(_bac, "_post_messages", _no_verify)
    r = app.test_client().get("/api/v1/deal-autopsy")
    assert r.status_code == 200
    j = r.get_json()
    # No verification meta at all, and the deterministic read ships intact —
    # its true SQL-sourced numbers (~48mo) are never second-guessed.
    assert "verification" not in j
    assert "48mo" in j["deals"][0]["autopsy_read"]
    assert "long-dated land/power OPTION" in j["deals"][0]["autopsy_read"]
