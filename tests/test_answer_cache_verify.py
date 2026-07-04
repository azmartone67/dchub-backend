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

  Part B — verifier:
    (7) flags a fabricated number ({ok:false, issues[]}) and
        strip_flagged_sentences removes the offending sentence
    (8) verifier timeout → served unverified (verified:false), 6s cap passed
    (9) VERIFY_MAX_PER_HOUR budget (second call over a cap of 1 skips)
   (10) ANSWER_VERIFY_DISABLE kill switch (no API call made)
   (11) no-numbers answers skip the API call entirely (trivially ok)

  Wiring (Flask test client, everything heavy mocked):
   (12) deal_autopsy cache HIT short-circuits compute
   (13) deal_autopsy MISS computes, stores POST-verification answer per-tier
   (14) paid deal_autopsy strips the verifier-flagged sentence in place

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
        self.rows = []      # {id, tool, tier, query_text, emb, answer, created_ts}
        self._id = 0
        self.fail_execute = False

    def add(self, tool, tier, emb, answer, created_ts=None, query_text=""):
        self._id += 1
        self.rows.append({
            "id": self._id, "tool": tool, "tier": tier,
            "query_text": query_text, "emb": list(emb), "answer": answer,
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
        if s.startswith("create"):
            return
        if s.startswith("insert into answer_cache"):
            tool, tier, q, vec_s, answer_json = params
            self.db.add(tool, tier, json.loads(vec_s),
                        json.loads(answer_json), query_text=q)
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
        if s.startswith("select answer"):
            qs, tool, tier, _qs2 = params
            qv = json.loads(qs)
            cands = [r for r in self.db.rows
                     if r["tool"] == tool and r["tier"] == tier]
            if not cands:
                return
            best = max(cands, key=lambda r: _cos(r["emb"], qv))
            self._one = (best["answer"], _cos(best["emb"], qv),
                         time.time() - best["created_ts"])
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
                        query_text="": real_add(tool, tier, emb, answer,
                                                created_ts=base + next(ts),
                                                query_text=query_text))
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
    monkeypatch.setattr("routes.brain_answer_cache.get_cached",
                        lambda tool, tier, q: {"ok": True, "deals": [],
                                               "count": 0, "tier": "FREE"})

    def _no_compute(limit):
        raise AssertionError("compute ran despite cache hit")
    monkeypatch.setattr(da, "_recent_deals", _no_compute)
    r = app.test_client().get("/api/v1/deal-autopsy")
    assert r.status_code == 200
    j = r.get_json()
    assert j["_cache"]["hit"] is True and j["ok"] is True


def test_deal_autopsy_miss_computes_and_stores_post_verification(monkeypatch):
    app, da = _autopsy_app()
    stored = {}
    monkeypatch.setattr("routes.brain_answer_cache.get_cached",
                        lambda tool, tier, q: None)

    def _capture_put(tool, tier, q, answer):
        stored.update(tool=tool, tier=tier, q=q, answer=answer)
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
    # Anonymous caller = FREE tier: cached under FREE, teaser envelope intact,
    # and no verification meta (verify is paid-only).
    assert stored["tool"] == "deal_autopsy" and stored["tier"] == "FREE"
    assert stored["q"] == "deal autopsy limit=5"
    assert stored["answer"]["autopsy"]["locked"] is True
    assert "verification" not in stored["answer"]


def test_deal_autopsy_paid_strips_flagged_sentence(monkeypatch):
    app, da = _autopsy_app()
    import routes.tier_gate as tg
    monkeypatch.setattr(tg, "_resolve_caller_tier", lambda: ("PRO", {}))
    monkeypatch.setattr("routes.brain_answer_cache.get_cached",
                        lambda tool, tier, q: None)
    stored = {}
    monkeypatch.setattr("routes.brain_answer_cache.put_cached",
                        lambda tool, tier, q, answer: stored.update(
                            tier=tier, answer=answer) or True)
    monkeypatch.setattr(da, "_recent_deals", lambda limit: [dict(DEAL)])
    monkeypatch.setattr(da, "_market_index", lambda: ({}, {}))
    monkeypatch.setattr(da, "_match_market", lambda d, idx, st: dict(MK))
    monkeypatch.setattr(da, "_rag_comparables", lambda d, mk: [])
    # Verifier flags the DCPI sentence as carrying an unsupported number.
    flagged = ("DCPI rates Atlanta AVOID (~48mo to power) — the grid queue "
               "is the wall.")
    monkeypatch.setattr("routes.brain_answer_cache.verify_answer",
                        lambda answer, facts: {"verified": True, "ok": False,
                                               "issues": [{"number": "48",
                                                           "sentence": flagged}]})
    r = app.test_client().get("/api/v1/deal-autopsy")
    assert r.status_code == 200
    j = r.get_json()
    assert j["verification"]["verified"] is True
    assert j["verification"]["sentences_stripped"] == 1
    read = j["deals"][0]["autopsy_read"]
    assert "48mo" not in read                      # flagged sentence gone
    assert "long-dated land/power OPTION" in read  # rest of the read intact
    # POST-verification answer is what got cached, under the PAID tier.
    assert stored["tier"] == "PRO"
    assert "48mo" not in stored["answer"]["deals"][0]["autopsy_read"]


def test_deal_autopsy_paid_serves_unverified_on_verifier_failure(monkeypatch):
    app, da = _autopsy_app()
    import routes.tier_gate as tg
    monkeypatch.setattr(tg, "_resolve_caller_tier", lambda: ("PRO", {}))
    monkeypatch.setattr("routes.brain_answer_cache.get_cached",
                        lambda tool, tier, q: None)
    monkeypatch.setattr("routes.brain_answer_cache.put_cached",
                        lambda tool, tier, q, answer: True)
    monkeypatch.setattr(da, "_recent_deals", lambda limit: [dict(DEAL)])
    monkeypatch.setattr(da, "_market_index", lambda: ({}, {}))
    monkeypatch.setattr(da, "_match_market", lambda d, idx, st: dict(MK))
    monkeypatch.setattr(da, "_rag_comparables", lambda d, mk: [])
    monkeypatch.setattr("routes.brain_answer_cache.verify_answer",
                        lambda answer, facts: {"verified": False,
                                               "reason": "TimeoutError"})
    r = app.test_client().get("/api/v1/deal-autopsy")
    assert r.status_code == 200
    j = r.get_json()
    # Answer served in full, flagged as unverified — never dropped.
    assert j["verification"]["verified"] is False
    assert "48mo" in j["deals"][0]["autopsy_read"]
