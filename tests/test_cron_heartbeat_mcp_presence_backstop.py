"""Guard: the MCP-registry presence crawl must have a LIVE daily backstop.

routes/mcp_presence_crawler.py re-scrapes each of the ~16 tracked registry
listings on a 3-day SLA. The only thing that ever drove that crawl was
crawler_scheduler's in-process thread, whose slot tracking is in-memory and
resets on every worker restart (Railway deploys constantly). The intended
backstop lives in dchub-scheduler.py ("a RELIABLE daily backstop via the
proven job scheduler") — but that module's own docstring says it is NEVER
LAUNCHED from this repo (Procfile/railway.json only run start_web.sh), so the
backstop never actually fires. That silent gap is why check_mcp_presence_stale
(routes/brain_consistency_radar.py) kept re-filing mcp_presence_listing_stale:
12 of 16 registries sat >72h unscraped while the crawler's in-process
scheduler quietly lost a slot.

cron_heartbeat.py IS live (an external cron already hits it every 5 minutes),
so it is the only mechanism in this repo that can back the crawl without
touching a GitHub workflow file. This pins that the crawl is actually wired
into it, and that it cannot turn into a same-hour re-hammer of every tracked
registry on each 5-minute heartbeat tick.
"""
import datetime
import importlib

hb = importlib.import_module("routes.cron_heartbeat")


def _entry():
    for row in hb._DISPATCH:
        if row[0] == "mcp_presence_crawl_daily":
            return row
    return None


def test_mcp_presence_crawl_is_wired_into_the_live_heartbeat():
    row = _entry()
    assert row is not None, (
        "no _DISPATCH entry drives the MCP-presence crawl — the only "
        "backstop for crawler_scheduler's fragile in-memory slots "
        "(dchub-scheduler.py) is dead code that nothing launches, so "
        "listings rot silently past the 3-day SLA")
    _, url, method, _ = row
    assert url.endswith("/api/v1/admin/mcp-presence/crawl")
    assert method == "POST"


def test_the_predicate_fires_at_least_once_per_day():
    row = _entry()
    assert row is not None
    _, _, _, pred = row
    fired = [
        h for h in range(24)
        if pred(datetime.datetime(2026, 9, 25, h, 1, 0))
    ]
    assert fired, "the predicate never fires in a full 24h sweep of hours"


def test_it_cannot_re_hammer_every_registry_on_every_heartbeat_tick():
    """The predicate's own window is wide (GitHub drops most 5-min fires), so
    without a re-fire guard a healthy delivery hour would crawl all ~16
    registries repeatedly instead of once."""
    assert "mcp_presence_crawl_daily" in hb._MIN_REFIRE_S, (
        "mcp_presence_crawl_daily has a wide same-hour predicate with no "
        "re-fire window — a healthy heartbeat cadence would re-run the full "
        "registry sweep on every 5-minute tick inside that hour")
    assert hb._MIN_REFIRE_S["mcp_presence_crawl_daily"] >= 3600, (
        "the re-fire window is shorter than the wide predicate window it is "
        "meant to collapse")
