"""Unit tests for routes/competitor_recon.py — pure functions only.

No network, no database, no main import (house rule). Flask is stubbed
if absent so these run anywhere.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover
    import flask  # noqa: F401
except Exception:  # minimal stub — module only needs Blueprint at import
    fake = types.ModuleType("flask")

    class _BP:
        def __init__(self, *a, **k):
            pass

        def route(self, *a, **k):
            def deco(f):
                return f
            return deco

    fake.Blueprint = _BP
    fake.jsonify = lambda *a, **k: {}
    fake.request = None
    sys.modules["flask"] = fake

from routes.competitor_recon import (  # noqa: E402
    AXES, TARGETS, ai_access_score, assess_target, build_target_row,
    parse_feed_velocity, parse_home, parse_robots, planned_fetches,
    render_report_md, synthesize,
)

ROBOTS_BLOCKING = """
User-agent: GPTBot
Disallow: /

User-agent: anthropic-ai
User-agent: ClaudeBot
Disallow: /

User-agent: *
Disallow: /admin
Sitemap: https://example.com/sitemap.xml
"""


def _target(slug):
    return next(t for t in TARGETS if t["slug"] == slug)


class TestReconPure(unittest.TestCase):

    def test_dcbyte_never_fetched(self):
        self.assertEqual(_target("dcbyte")["policy"], "no_crawl_tos")
        self.assertEqual(planned_fetches(_target("dcbyte")), [])

    def test_total_fetch_plan_bounded(self):
        total = sum(len(planned_fetches(t)) for t in TARGETS)
        self.assertLessEqual(total, 40)
        for t in TARGETS:
            self.assertLessEqual(len(planned_fetches(t)), 8)

    def test_parse_robots_stances(self):
        rb = parse_robots(ROBOTS_BLOCKING)
        self.assertEqual(rb["ai_stance"]["gptbot"], "blocked_all")
        self.assertEqual(rb["ai_stance"]["claudebot"], "blocked_all")
        self.assertEqual(rb["ai_stance"]["anthropic-ai"], "blocked_all")
        self.assertEqual(rb["ai_stance"]["perplexitybot"], "default_open")
        self.assertIn("gptbot", rb["blocks_ai"])
        self.assertNotIn("perplexitybot", rb["blocks_ai"])
        self.assertEqual(rb["sitemaps"], ["https://example.com/sitemap.xml"])

    def test_ai_access_scoring(self):
        s, ev = ai_access_score({"policy": "no_crawl_tos"})
        self.assertEqual(s, 0)
        self.assertIn("ToS", ev)
        s, _ = ai_access_score({"policy": "crawl",
                                "robots": parse_robots(ROBOTS_BLOCKING),
                                "llms_txt": {"present": False}})
        self.assertEqual(s, 0)  # 3 bots blocked
        s, _ = ai_access_score({"policy": "crawl",
                                "robots": {"fetched": True, "blocks_ai": []},
                                "llms_txt": {"present": True}})
        self.assertEqual(s, 2)

    def test_parse_home_and_keywords(self):
        html = ("<html><head><title> Rival — data center intel </title>"
                '<meta name="description" content="Market analytics">'
                '</head><body><h1>Real-time AI agents API</h1>'
                '<script>{"@type": "Organization"}</script></body></html>')
        h = parse_home(html)
        self.assertEqual(h["title"], "Rival — data center intel")
        self.assertEqual(h["meta_description"], "Market analytics")
        self.assertTrue(h["keywords"]["api"])
        self.assertTrue(h["keywords"]["ai_agent"])
        self.assertTrue(h["keywords"]["real_time"])
        self.assertIn("Organization", h["jsonld_types"])

    def test_feed_velocity_rss(self):
        import datetime
        now = datetime.datetime(2026, 7, 25, 12, 0, 0)
        rss = """<rss><channel>
        <item><title>Fresh story</title>
          <pubDate>Fri, 24 Jul 2026 09:00:00 GMT</pubDate></item>
        <item><title><![CDATA[Old story]]></title>
          <pubDate>Mon, 01 Jun 2026 09:00:00 GMT</pubDate></item>
        </channel></rss>"""
        v = parse_feed_velocity(rss, "rss", now=now)
        self.assertEqual(v["items"], 2)
        self.assertEqual(v["items_7d"], 1)
        self.assertIn("Fresh story", v["latest"][0])

    def test_synthesis_moats_and_moves(self):
        signals, rows = {}, {}
        for t in TARGETS:
            sig = {"policy": t["policy"], "category": t["category"],
                   "fetches": 0 if t["policy"] == "no_crawl_tos" else 3,
                   "robots": parse_robots(ROBOTS_BLOCKING),
                   "llms_txt": {"present": False}}
            signals[t["slug"]] = sig
            rows[t["slug"]] = build_target_row(t["slug"], sig)
        dchub_row = {a: 3 for a, _ in AXES}
        dchub_row["news_editorial"] = 1
        synth = synthesize(rows, dchub_row, ["evidence"], signals,
                           prev_titles={})
        moat_axes = {w["axis"] for w in synth["gaps"]["whitespace_moats"]}
        self.assertIn("live_telemetry", moat_axes)
        gap_axes = {g["axis"] for g in synth["gaps"]["dchub_gaps"]}
        self.assertIn("news_editorial", gap_axes)
        keys = [m["key"] for m in synth["win_moves"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(any(k.startswith("agent_flank_") for k in keys))
        pris = [m["priority"] for m in synth["win_moves"]]
        self.assertEqual(pris, sorted(pris, reverse=True))
        for m in synth["win_moves"]:
            self.assertTrue(m["evidence"] is not None)
        self.assertEqual(len(synth["ai_access_exhibit"]), len(TARGETS))

    def test_positioning_shift_detected(self):
        signals = {"dchawk": {"policy": "crawl", "category": "analyst_platform",
                              "fetches": 1,
                              "home": {"title": "Now an AI agent platform"}}}
        rows = {"dchawk": build_target_row("dchawk", signals["dchawk"])}
        synth = synthesize(rows, {a: 3 for a, _ in AXES}, [], signals,
                           prev_titles={"dchawk": "Data center analytics"})
        self.assertEqual(len(synth["positioning_shifts"]), 1)
        self.assertTrue(any(m["key"] == "positioning_shift_dchawk"
                            for m in synth["win_moves"]))

    def test_assess_target_flags_agent_locked(self):
        t = _target("dchawk")
        sig = {"policy": "crawl", "category": "analyst_platform", "fetches": 3,
               "robots": parse_robots(ROBOTS_BLOCKING),
               "llms_txt": {"present": False}}
        row = build_target_row("dchawk", sig)
        a = assess_target(t, sig, row)
        self.assertTrue(any("Closed to AI agents" in b for b in a["bad"]))
        self.assertTrue(a["good"])  # curated strengths present

    def test_report_md_sections(self):
        t = _target("baxtel")
        sig = {"policy": "crawl", "category": "directory", "fetches": 2,
               "robots": {"fetched": True, "blocks_ai": []},
               "llms_txt": {"present": False}}
        rows = {"baxtel": build_target_row("baxtel", sig)}
        assessments = {"baxtel": assess_target(t, sig, rows["baxtel"])}
        dchub_row = {a: 3 for a, _ in AXES}
        synth = synthesize(rows, dchub_row, ["ev"], {"baxtel": sig}, {})
        md = render_report_md("2026-07-25", rows, dchub_row, ["ev"],
                              assessments, synth)
        for section in ("Capability matrix", "AI-agent access exhibit",
                        "the good / the bad", "## Gaps", "Win moves"):
            self.assertIn(section, md)
        self.assertIn("DC Byte never fetched", md)


if __name__ == "__main__":
    unittest.main()
