"""tests/test_sponsor_report_and_crawls.py — the advertiser report must
under-state, and must never render a failure as a zero (B5, 2026-08-28).

THE MEASUREMENT THAT SHAPED THIS. The handoff recorded "CF GraphQL: zone caps
one query at 1w1d — use datetime_geq, not date_geq", which reads as a
QUERY-SIZE cap you can work around by chunking. It is not. Against the live
zone on 2026-08-28:

    days=30 -> REFUSED  "cannot request data older than 1w1d, but your query
                         requests data from 4w2d ago"
    days=14 -> REFUSED
    days=8  -> ok (435 AI crawls)
    days=7  -> ok
    days=6  -> ok

It is a RETENTION limit. Chunking a 30-day window does not help, because the
old chunks are refused outright. A monthly crawl table can therefore only be
ACCUMULATED from daily snapshots, and until it has been, the report must say
how many days it actually covers rather than printing a monthly heading over a
weekly number.

THE OTHER HALF: a section that cannot be read must say UNAVAILABLE, never 0.
An advertiser reading "0 crawls" concludes nobody came; an advertiser reading
"unavailable" asks us. Only one of those is honest about what we know.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CRAWL = ROOT / "routes" / "sponsor_crawl.py"
REPORT = ROOT / "routes" / "sponsor_report.py"

from routes import sponsor_crawl as sc
from routes import sponsor_report as sr


# ── engine classification ────────────────────────────────────────────
@pytest.mark.parametrize("ua,expect", [
    ("Mozilla/5.0 (compatible; GPTBot/1.1; +https://openai.com/gptbot)", "OpenAI (GPTBot)"),
    ("Mozilla/5.0 ... ChatGPT-User/1.0", "OpenAI (ChatGPT browsing)"),
    ("Mozilla/5.0 (compatible; OAI-SearchBot/1.0)", "OpenAI (SearchBot)"),
    ("Mozilla/5.0 (compatible; ClaudeBot/1.0)", "Anthropic (ClaudeBot)"),
    ("Mozilla/5.0 (compatible; PerplexityBot/1.0)", "Perplexity (PerplexityBot)"),
    ("Mozilla/5.0 (compatible; Google-Extended)", "Google (Google-Extended)"),
])
def test_ai_agents_are_classified(ua, expect):
    assert sc.classify_engine(ua) == expect


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "curl/8.5.0",
    "",
])
def test_search_crawlers_and_humans_are_not_counted_as_ai_engines(ua):
    """★ Googlebot is search indexing and our Bing traffic is overwhelmingly
    Webmaster Tools, not Copilot. Counting either would inflate the headline
    an advertiser is paying against with crawls unrelated to AI answers."""
    assert sc.classify_engine(ua) is None


def test_more_specific_agents_win_over_generic_ones():
    """OAI-SearchBot must not fall through to a looser 'bot' rule."""
    assert sc.classify_engine("OAI-SearchBot/1.0") == "OpenAI (SearchBot)"
    assert sc.classify_engine("Applebot-Extended/1.0") == "Apple (Applebot-Extended)"


# ── the retention limit is honoured and disclosed ────────────────────
def test_a_month_request_is_clamped_and_says_so(monkeypatch):
    monkeypatch.setattr(sc, "_token", lambda: "t")
    monkeypatch.setattr(sc, "_query", lambda *a, **k: [])
    out = sc.engine_crawls(["/"], days=30)
    assert out["requested_days"] == 30
    assert out["window_days"] == sc.MAX_LOOKBACK_DAYS == 8
    joined = " ".join(out["limits"]).lower()
    assert "retains only" in joined and "8" in joined, (
        "the window was silently clamped; the report would print a 30-day "
        "heading over 8 days of data"
    )


def test_a_failed_chunk_is_not_reported_as_a_partial_total(monkeypatch):
    """★ A partial sum presented as a total under-states with no sign
    anything was missing — the one failure mode worse than an error."""
    monkeypatch.setattr(sc, "_token", lambda: "t")

    def boom(*a, **k):
        raise RuntimeError("quota")

    monkeypatch.setattr(sc, "_query", boom)
    out = sc.engine_crawls(["/"], days=8)
    assert out["ok"] is False
    assert out["total_ai_crawls"] == 0
    assert any("partial" in l for l in out["limits"])


def test_missing_token_is_not_zero_crawls(monkeypatch):
    monkeypatch.setattr(sc, "_token", lambda: "")
    out = sc.engine_crawls(["/"], days=8)
    assert out["ok"] is False and out["total_ai_crawls"] == 0


def test_cloudflare_errors_inside_a_200_are_raised(monkeypatch):
    """★ CF returns HTTP 200 with an `errors` array. Treating 200 as success
    is how a permissions failure becomes 'no crawlers visited this month'."""
    import json as _json

    # ★ `data` is POPULATED here on purpose. With data:None the request falls
    #   through to the "no zone in response" guard and raises anyway, so the
    #   test would pass with the errors check deleted — it would be fencing a
    #   different guard than the one it names. A well-formed zone payload
    #   alongside an errors array can only be caught by the errors check, and
    #   the message assertion pins WHICH guard fired.
    class _R:
        def json(self): return {
            "data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": []}]}},
            "errors": [{"message": "not authorized"}]}

    monkeypatch.setattr(sc.requests, "post", lambda *a, **k: _R())
    from datetime import datetime, timezone
    with pytest.raises(RuntimeError) as ei:
        sc._query("tok", datetime.now(timezone.utc), datetime.now(timezone.utc), ["/"])
    assert "not authorized" in str(ei.value), (
        "a different guard raised; the errors-array check is not what fired"
    )


# ── snapshots are corrections, not accumulations ─────────────────────
def test_snapshot_upsert_sets_the_count_it_does_not_add():
    """Re-running a day must REPLACE its figure. `crawls = crawls + EXCLUDED`
    would double an advertiser's number every time the job re-ran."""
    tree = ast.parse(CRAWL.read_text(encoding="utf-8"))
    sql = [n.value for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "ON CONFLICT" in n.value.upper()]
    assert sql, "no upsert found in the snapshot writer"
    # Whitespace-normalised: the statement is column-aligned in one literal
    # (it has to be, or regression_lint's INSERT regex stops at the first quote
    # and reports it as a non-idempotent insert).
    import re as _re
    joined = _re.sub(r"\s+", " ", " ".join(sql))
    assert "crawls = EXCLUDED.crawls" in joined, (
        "the snapshot upsert does not SET the count from EXCLUDED"
    )
    assert "crawls + " not in joined.replace("crawls = EXCLUDED.crawls", ""), (
        "the snapshot upsert ADDS to the stored count; a re-run inflates it"
    )


def test_snapshot_reader_reports_how_much_of_the_window_it_covers():
    tree = ast.parse(CRAWL.read_text(encoding="utf-8"))
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "crawls_from_snapshots"]
    assert fn, "crawls_from_snapshots missing"
    src = ast.get_source_segment(CRAWL.read_text(encoding="utf-8"), fn[0]) or ""
    assert "days_covered" in src, (
        "the snapshot reader does not report coverage, so a report could print "
        "a 30-day heading over 6 days of accrual"
    )


# ── the report never renders a failure as a zero ─────────────────────
def _rep(**over):
    base = {"ok": True, "window_days": 30,
            "sponsorship": {"id": 1, "slot": "ai_source_block", "status": "active",
                            "sponsor_name": "Acme", "activated_at": "2026-08-01",
                            "impressions": 0, "clicks": 0, "link_url": "x"},
            "crawl": {"ok": False, "limits": ["cloudflare unavailable"]},
            "delivery": {"impressions_stamped": 0, "clicks_counted": 0, "limits": ["u"]},
            "mentions": {"ok": False, "limits": ["db down"]},
            "limits": ["under-states"]}
    base.update(over)
    return base


def test_an_unreadable_crawl_section_says_unavailable_not_zero():
    """★ Scoped to the CRAWL section on purpose.

    "UNAVAILABLE — not zero" appears in the mentions section too, so a
    whole-document assertion is satisfied by the other occurrence and fences
    nothing. Found by a mutation run that expected RED and got GREEN.
    """
    text = sr.render_text(_rep())
    seg = text.split("AI ENGINE CRAWLS")[1].split("DELIVERY")[0]
    assert "UNAVAILABLE — not zero" in seg, (
        "the crawl section rendered a number instead of saying it could not "
        "be read; an advertiser reads '0 crawls' as 'nobody came'"
    )
    assert "cloudflare unavailable" in seg
    assert "TOTAL" not in seg, "a total was printed for a section that failed"


def test_an_unreadable_mention_section_says_unavailable_not_zero():
    text = sr.render_text(_rep())
    seg = text.split("YOUR BRAND IN AI ANSWERS")[1]
    assert "UNAVAILABLE — not zero" in seg
    assert "db down" in seg


def test_delivery_always_states_all_three_undercounts():
    """★ These are the reasons the invoice is smaller than reality. Dropping
    any of them turns an honest under-count into an unexplained one."""
    import routes.sponsor_report as m

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self): return (1, "ai_source_block", "Acme", "u", "active",
                                    5, 2, None, None)

    class _Conn:
        def cursor(self): return _C()
        def close(self): pass

    rep = m.monthly_report(1, days=30, conn=_Conn())
    joined = " ".join(rep["delivery"]["limits"]).lower()
    assert "max-age=120" in joined, "the DCPI cache undercount is not disclosed"
    assert "zero impressions" in joined, "the root-domain undercount is not disclosed"
    assert "never replayed" in joined, "the dropped-click undercount is not disclosed"


def test_the_report_names_the_root_domain_as_the_dominant_surface():
    """Measured 2026-08-28 over the 8-day window: / took 427 of 435 AI crawls
    (98%), /llms.txt 8, /api/v1/dcpi/scores 0. A surface list that omitted the
    root domain would describe ~2% of the reach."""
    assert "/" in sr.SPONSOR_SURFACES
    assert "/llms.txt" in sr.SPONSOR_SURFACES
