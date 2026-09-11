"""A GEO answer page is never committed with a figure the frontend accuracy fence
bans (2026-09-11).

routes/geo_answer_publisher.publish_answer() commits straight to dchub-frontend
main — no PR, no CI in between. dchub-frontend/scripts/accuracy_fence.py runs as
a step INSIDE deploy-pages.yml, so a page it bans does not fail alone: it stops
every frontend deploy. The 2026-09-10 page, "29,000+ data center facilities
(21,500+ verified)", held production for 29 consecutive runs, about ten hours.

These run the REAL render, the REAL claim scan and the REAL publish and
autopublish wiring. Only the edges that leave the process are replaced: GitHub
(_commit_file, _gh, _fence_canon), live canon (_live_canon, get_canonical_stats)
and the LLM (_llm_draft).
"""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.geo_answer_publisher as pub  # noqa: E402
import routes.geo_autopublish as g  # noqa: E402

# Canon on 2026-09-11: data/growth.json on frontend main and live canon agree.
FENCE = {"facilities": 21570, "deals": 2176}
LIVE = {"facilities": 21570, "deals": 2177}

# The short answer of /answers/where-can-i-get-free-data-center-industry-news-analytics
# as committed 2026-09-10 14:42Z (dchub-frontend 8dcde7b27) — the page that froze
# the deploy — and the same sentence as dchub-frontend#1444 rewrote it.
FROZE_DEPLOYS = (
    "DC Hub tracks 29,000+ data center facilities (21,500+ verified) across 170+ "
    "countries, scoring 300+ markets and monitoring 2,100+ M&A deals. The platform "
    "provides live grid telemetry on 5 continents—US, UK, EU, Taiwan, Japan, "
    "South Korea, Brazil, and Australia—via its MCP-native data layer at dchub.cloud.")
UNFROZE = FROZE_DEPLOYS.replace(
    "29,000+ data center facilities (21,500+ verified)",
    "21,500+ distinct data center facilities")


def _payload(**over):
    p = {"slug": "where-can-i-get-free-data-center-industry-news-analytics",
         "title": "Free Data Center Industry News + Analytics",
         "question": "Where can I get free data center industry news + analytics?",
         "lede": "A general model guesses; the live layer answers.",
         "short_answer": UNFROZE,
         "sections": [{"h": "What DC Hub Covers", "p": "Markets, grid and deals."}],
         "meta_description": "Live data center intelligence over MCP.",
         "tools": "discover_tools, get_market_dcpi_rank and search_facilities"}
    p.update(over)
    return p


@pytest.fixture
def canon(monkeypatch):
    monkeypatch.setattr(pub, "_fence_canon", lambda: dict(FENCE))
    monkeypatch.setattr(pub, "_live_canon", lambda: dict(LIVE))


@pytest.fixture
def commits(monkeypatch):
    made = []
    monkeypatch.setattr(pub, "_GH_TOKEN", "test-token")
    monkeypatch.setattr(pub, "_commit_file",
                        lambda path, content, message, overwrite:
                        made.append((path, content)) or (True, "sha"))
    monkeypatch.setattr(pub, "_append_sitemap", lambda slug: None)
    return made


# ── publish_answer: the commit is the thing refused ─────────────────────────

def test_the_page_that_froze_production_is_refused_and_never_committed(canon, commits):
    res = pub.publish_answer(_payload(short_answer=FROZE_DEPLOYS))
    assert res["ok"] is False and res["error"] == "over_canon", res
    assert commits == [], "the page reached the frontend repo"
    off = [o for o in res["offending"] if o["kind"] == "facilities"]
    assert [o["claimed"] for o in off] == [29000], res["offending"]
    assert off[0]["allowed"] == 22648


def test_the_rewrite_that_unfroze_it_is_committed(canon, commits):
    """A ceiling that refuses the truth is worse than none."""
    res = pub.publish_answer(_payload())
    assert res["ok"] is True, res
    assert len(commits) == 1
    assert "21,500+ distinct data center facilities" in commits[0][1]


def test_a_meta_description_is_read(canon, commits):
    """Attribute text vanishes under a tag strip, so the RAW line must be read too."""
    res = pub.publish_answer(_payload(meta_description="Tracks 29,000+ facilities live."))
    assert res["ok"] is False and res["error"] == "over_canon", res
    assert commits == []


# ── the arithmetic is the fence's own ────────────────────────────────────────

def test_the_ceiling_is_canon_times_the_fence_margin(canon):
    # int(21570 * 1.05) == 22648; int(2176 * 1.05) == 2284 — the fence's own log line.
    assert pub.claim_thresholds() == {"facilities": 22648, "deals": 2284}
    assert pub.over_canon_claims("22,648+ data center facilities") == []
    assert [o["claimed"] for o in pub.over_canon_claims("22,649+ data center facilities")] == [22649]
    assert pub.over_canon_claims("2,284+ M&A deals") == []
    assert [o["claimed"] for o in pub.over_canon_claims("2,285+ M&A deals")] == [2285]


@pytest.mark.parametrize("fence, live", [
    ({"facilities": 21570}, {"facilities": 30000}),   # live canon ahead of growth.json
    ({"facilities": 30000}, {"facilities": 21570}),   # growth.json ahead of live canon
])
def test_the_lower_canon_wins(monkeypatch, fence, live):
    """A stale growth.json must refuse the page HERE rather than freeze the deploy
    THERE, and a live figure below it is the truer ceiling."""
    monkeypatch.setattr(pub, "_fence_canon", lambda: dict(fence))
    monkeypatch.setattr(pub, "_live_canon", lambda: dict(live))
    assert pub.claim_thresholds()["facilities"] == 22648
    assert [o["claimed"] for o in pub.over_canon_claims("25,000+ facilities")] == [25000]


def test_an_unusable_growth_json_gets_the_fences_own_fallback(monkeypatch):
    """The fence bans >20,000 facilities when growth.json is unusable, and so
    would every deploy — so the publisher must not add a page to that."""
    monkeypatch.setattr(pub, "_fence_canon", lambda: {})
    monkeypatch.setattr(pub, "_live_canon", lambda: {})
    assert pub.claim_thresholds() == {"facilities": 20000, "deals": 3900}
    assert [o["claimed"] for o in pub.over_canon_claims("21,500+ facilities")] == [21500]


def test_no_readable_canon_refuses_any_claim_but_not_a_page_without_one(monkeypatch):
    monkeypatch.setattr(pub, "_fence_canon", lambda: None)
    monkeypatch.setattr(pub, "_live_canon", lambda: {})
    assert pub.claim_thresholds() == {"facilities": None, "deals": None}
    off = pub.over_canon_claims("21,500+ distinct facilities")
    assert [(o["claimed"], o["allowed"]) for o in off] == [(21500, None)]
    assert pub.over_canon_claims("Free tier: 10 calls/day, no signup.") == []


def test_a_page_with_no_figures_never_reads_canon(monkeypatch):
    def boom():
        raise AssertionError("canon read for a page with no claim")
    monkeypatch.setattr(pub, "_fence_canon", boom)
    monkeypatch.setattr(pub, "_live_canon", boom)
    assert pub.over_canon_claims(pub._render(_payload(short_answer="Live grid data over MCP."))) == []


# ── what the scan can see ────────────────────────────────────────────────────

def test_a_number_split_from_its_noun_by_markup_is_read(canon):
    assert [o["claimed"] for o in pub.over_canon_claims(
        "<p><b>29,000+</b> data center facilities</p>")] == [29000]
    assert [o["claimed"] for o in pub.over_canon_claims(
        "<li><b>5,471</b> M&amp;A deals</li>")] == [5471]


@pytest.mark.parametrize("text", [
    "29,000+ data centers",
    "29,000+ distinct facilities",
    "29,000+ data-center facilities",
])
def test_shapes_the_fence_misses_are_refused_here(canon, text):
    """Pin that each IS a fence gap, so this test fails if it stops being one
    rather than silently testing nothing new."""
    assert not any(rx.search(text) for _k, rx in pub._FENCE_MAGNITUDE), \
        f"{text!r} is no longer a fence gap — move it out of this test"
    assert [o["claimed"] for o in pub.over_canon_claims(text)] == [29000]


@pytest.mark.parametrize("text", [
    "340,000+ mapped power, grid, gas and fiber assets",
    "127,293 substations and 95,000 transmission lines near data centers",
    "182,000 global power generating units",
    "ERCOT (1,826 of 1,907 projects, 455,350 MW)",
    "21,500+ distinct data center facilities (170+ countries) and 2,100+ M&A deals",
])
def test_true_figures_beside_the_pattern_pass(canon, text):
    assert pub.over_canon_claims(text) == []


# ── the canon readers parse what the fence parses ────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_growth_json_is_parsed_from_the_contents_api_shape(monkeypatch):
    doc = {"snapshot_at": "2026-09-11T02:32:36Z",
           "current": {"facilities": 21570, "deals": 2176, "countries": 178}}
    b64 = base64.b64encode(json.dumps(doc).encode()).decode()
    wrapped = "\n".join(b64[i:i + 60] for i in range(0, len(b64), 60))   # GitHub wraps at 60
    monkeypatch.setattr(pub, "_gh", lambda method, path, body=None: _Resp(200, {"content": wrapped}))
    assert pub._fence_canon() == {"facilities": 21570, "deals": 2176}

    monkeypatch.setattr(pub, "_gh", lambda method, path, body=None: _Resp(404, {}))
    assert pub._fence_canon() is None, "an unread file is UNKNOWN, not unusable"

    monkeypatch.setattr(pub, "_gh", lambda method, path, body=None: _Resp(200, {"content": "bm90IGpzb24="}))
    assert pub._fence_canon() == {}, "a read, unusable file is the fence's fallback case"


def test_live_canon_is_the_distinct_population_not_the_raw_pile(monkeypatch):
    import canonical_stats as cs
    monkeypatch.setattr(cs, "get_canonical_stats", lambda force=False: {
        "facilities": 29945, "facilities_verified": 21570, "deals": 2177})
    assert pub._live_canon() == {"facilities": 21570, "deals": 2177}


# ── autopublish: refused before it is previewed or published ─────────────────

def _drafting(monkeypatch, short_answer):
    monkeypatch.setattr(g, "_gap_enabled", lambda: False)
    monkeypatch.setattr(g, "_page_exists", lambda slug: False)
    monkeypatch.setattr(g, "_gather_facts", lambda item: [{"facilities_distinct": "21,500+"}])
    monkeypatch.setattr(g, "_llm_draft", lambda item, rows: _payload(
        slug=item["slug"], question=item["query"], short_answer=short_answer))


def test_autopublish_refuses_the_draft_before_the_preview(monkeypatch, canon):
    _drafting(monkeypatch, FROZE_DEPLOYS)
    out = g.autopublish_next(dry=True)
    assert "would_publish" not in out, out
    assert {c.get("skip") for c in out["considered"]} == {"over_canon"}, out["considered"]
    assert out["considered"][0]["offending"][0]["claimed"] == 29000


def test_autopublish_never_hands_an_over_canon_draft_to_the_publisher(monkeypatch, canon):
    _drafting(monkeypatch, FROZE_DEPLOYS)
    published = []
    monkeypatch.setattr(pub, "publish_answer",
                        lambda draft, overwrite=False: published.append(draft) or {"ok": True})
    monkeypatch.setattr(g, "_record_publish", lambda *a: None)
    monkeypatch.setattr(g, "_enabled", lambda: True)
    out = g.autopublish_next(dry=False)
    assert published == [], "an over-canon draft reached publish_answer"
    assert out["acted"] is False


def test_autopublish_still_publishes_a_clean_draft(monkeypatch, canon):
    """The control: the same wire with the rewritten sentence goes through."""
    _drafting(monkeypatch, UNFROZE)
    published = []
    monkeypatch.setattr(pub, "publish_answer",
                        lambda draft, overwrite=False: published.append(draft) or {"ok": True})
    monkeypatch.setattr(g, "_record_publish", lambda *a: None)
    monkeypatch.setattr(g, "_enabled", lambda: True)
    out = g.autopublish_next(dry=False)
    assert out["acted"] is True and len(published) == 1


def test_autopublish_fails_closed_when_the_check_cannot_run(monkeypatch, canon):
    _drafting(monkeypatch, UNFROZE)

    def broken(draft):
        raise RuntimeError("github down")
    monkeypatch.setattr(pub, "check_answer", broken)
    published = []
    monkeypatch.setattr(pub, "publish_answer",
                        lambda draft, overwrite=False: published.append(draft) or {"ok": True})
    monkeypatch.setattr(g, "_record_publish", lambda *a: None)
    monkeypatch.setattr(g, "_enabled", lambda: True)
    out = g.autopublish_next(dry=False)
    assert published == []
    assert out["considered"][0]["skip"] == "over_canon"
    assert out["considered"][0]["offending"][0]["kind"] == "unchecked"


# ── the drafter is handed one population ─────────────────────────────────────

def test_the_drafter_is_handed_one_facility_population(monkeypatch):
    """Sentinel phrases, one per helper, so the row shows exactly which reached it."""
    import canonical_stats as cs
    monkeypatch.setattr(cs, "facilities_phrase", lambda: "29,000+")
    monkeypatch.setattr(cs, "facilities_verified_phrase", lambda: "21,500+")
    monkeypatch.setattr(cs, "countries_phrase", lambda: "170+")
    monkeypatch.setattr(cs, "markets_phrase", lambda: "300+")
    monkeypatch.setattr(cs, "deals_phrase", lambda: "2,100+")
    monkeypatch.setattr(cs, "grid_coverage_phrase", lambda style="full": "7 live feeds")
    rows = g._canonical_rows()
    blob = json.dumps(rows)
    assert "21,500+" in blob, rows
    assert "29,000+" not in blob, f"the raw pile reached the drafter: {rows}"
    assert [k for k in rows[0] if "facilit" in k] == ["facilities_distinct"], rows


def test_the_draft_prompt_names_the_one_population():
    """A pin: fails if the instruction is deleted, cannot tell whether the model
    obeyed. The refusal above is what enforces it."""
    assert "ONE FACILITY FIGURE" in g._SYSTEM
    assert "facilities_distinct" in g._SYSTEM
