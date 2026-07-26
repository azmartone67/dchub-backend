"""Shell #35 — pure tests for routes/grid_payload_freshness (no DB/network/main)."""
import os
import sys
import datetime
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routes.grid_payload_freshness import (  # noqa: E402
    adjust_headroom, build_freshness_block, measured_headroom_block, parse_ts,
)

NOW = datetime.datetime(2026, 7, 26, 12, 0, 0)


class TestFreshness(unittest.TestCase):
    def test_parse_ts_shapes(self):
        self.assertEqual(parse_ts("2026-07-26T11").hour, 11)          # EIA hour
        self.assertEqual(parse_ts("2026-07-26T10:30:00Z").minute, 30)  # Z suffix
        self.assertEqual(parse_ts("2026-07-26T06:00:00-04:00").hour, 10)  # offset→UTC
        self.assertIsNone(parse_ts("garbage"))
        self.assertIsNone(parse_ts(None))

    def test_adjust_signs(self):
        e = adjust_headroom("ERCOT", 68000, 60000, 8000)
        self.assertLess(e["headroom_mw_adjusted"], e["headroom_mw_raw"])  # +13pp removed
        self.assertAlmostEqual(e["headroom_mw_adjusted"], 8000 - 0.13 * 60000, 0)
        m = adjust_headroom("MISO", 90000, 95000, -5000)
        self.assertGreater(m["headroom_mw_adjusted"], m["headroom_mw_raw"])  # −14pp added back
        p = adjust_headroom("PJM", 90000, 80000, 10000)
        self.assertNotIn("headroom_mw_adjusted", p)  # no documented offset
        self.assertIn("reserve_margin_pct_raw", p)
        self.assertEqual(adjust_headroom("ERCOT", None, 0, None), {})

    def test_measured_block_stale_guard(self):
        row = {"observed_at": "2026-07-24T00:00:00Z", "online_gen_mw": 1,
               "load_mw": 1, "headroom_mw": 0}
        self.assertEqual(measured_headroom_block(row, "ERCOT", now=NOW), {})  # >24h
        fresh = dict(row, observed_at="2026-07-26T11:40:00Z",
                     online_gen_mw=68000, load_mw=60000, headroom_mw=8000)
        b = measured_headroom_block(fresh, "ERCOT", now=NOW)
        self.assertEqual(b["age_minutes"], 20)
        self.assertIn("headroom_mw_adjusted", b)
        self.assertIn("measured", b["basis"])

    def test_freshness_block(self):
        payload = {"demand_period": "2026-07-26T11",
                   "generation_mix_period": "2026-07-26T04",
                   "lmp_as_of": "2026-07-26T11:45:00Z",
                   "extended_metrics": {"dc_load_queue": {"as_of": "2026-07-24T10:00:00+00:00"}}}
        fr = build_freshness_block(payload, now=NOW)
        self.assertTrue(fr["within_sla_core"])  # demand 1h, lmp 15m within 4h
        self.assertEqual(fr["layers"]["demand"]["age_minutes"], 60)
        self.assertIn("verify_url", fr)
        self.assertTrue(fr["layers"]["dc_queue"]["within_sla"])  # 14d SLA
        stale = build_freshness_block({"demand_period": "2026-07-25T01"}, now=NOW)
        self.assertFalse(stale["within_sla_core"])
        self.assertEqual(build_freshness_block({}, now=NOW), {})


if __name__ == "__main__":
    unittest.main()
