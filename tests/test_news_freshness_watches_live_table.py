#!/usr/bin/env python3
"""tests/test_news_freshness_watches_live_table.py — a freshness probe must
watch a table something still writes.

NO NETWORK, NO DB.

r-newsdead (2026-08-13). DC Hub has two news tables:

    news            LEGACY. 3,078 rows, newest published_date 2026-08-09. Its
                    ONLY writer is news_aggregator.py, which no workflow or
                    cron invokes.
    news_articles   LIVE. 11,998 rows, 2,016 published in the last 14 days,
                    written by /api/jobs/news-refresh -> auto_sync.sync_news.
                    Measured mid-fix: last fetched_at 2.4 minutes earlier.

Four separate monitors measured `news`. So the board reported the news feed
stale while the loader was fetching every few hours, and the job it blamed
returned {"success": true, "new_articles": 337} on demand.

That false alarm was expensive: brain investigation #100046 (confidence 0.15,
refutation survived=false) plus three earlier "heartbeat_surfaces_stale" fixes,
every one of them aimed at a pipeline that was working.

★ fetched_at, NOT published_at. Feeds publish ahead — the max published_at in
news_articles is 2026-09-21, five weeks out — so a published_at probe would
read fresh for weeks after the loader died. Same bug, pointing the other way,
and far harder to notice.

Run standalone:   python3 tests/test_news_freshness_watches_live_table.py
Run under pytest: pytest tests/test_news_freshness_watches_live_table.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Files that MONITOR freshness/health. Product reads of `news` (e.g. the
# winback digest) are out of scope — this is about what raises the alarm.
# ★2026-08-30: routes/facilities_by_dims.py ADDED, after this guard caught the
# dead table on main.py but could not see it on the CITABLE surface.
#
# /api/v1/stats/canonical ("canonical truth ... use this") computed
# news_articles as COUNT(*) FROM news and published a FROZEN 3,503 while the
# live count was ~13,000. It is not a freshness monitor, so it sat outside this
# list — but a citable surface quoting an abandoned table does the same damage
# a monitor does and travels further: it is what agents cite, and on 2026-08-30
# it convinced a change to point the ai-agents manifest at the dead table too,
# "to agree with canon". #3394 fixed both instances; this closes the class.
MONITORS = ("routes/_freshness.py", "main.py", "routes/selfheal_master_shell.py",
            "routes/facilities_by_dims.py")

# `FROM news` where `news` is the whole identifier — not news_articles,
# news_discovered_entities, newsletter_*, etc.
DEAD_TABLE = re.compile(r"FROM\s+news\b(?!_)", re.IGNORECASE)


def _read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _code_only(text):
    """Drop comment lines: these files NAME the dead table in comments on
    purpose, and matching prose would read the history as the defect."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_no_monitor_measures_the_abandoned_news_table():
    offenders = []
    for rel in MONITORS:
        for i, line in enumerate(_code_only(_read(rel)).splitlines(), start=1):
            if DEAD_TABLE.search(line) and any(
                    k in line.lower() for k in ("count(", "max(", "select")):
                offenders.append(f"{rel}: {line.strip()[:110]}")
    assert not offenders, (
        "a freshness/health probe still measures the abandoned `news` table — "
        "nothing writes it, so it will report a working pipeline as stale:\n  "
        + "\n  ".join(offenders))


def test_freshness_board_uses_the_loader_heartbeat_not_published_at():
    """published_at is future-dated by feeds; only fetched_at dies with the loader."""
    src = _read("routes/_freshness.py")
    m = re.search(r'"news_age_seconds":\s*"([^"]+)"', src)
    assert m, "news_age_seconds probe not found"
    sql = m.group(1)
    assert "news_articles" in sql, f"news probe must read the live table, got: {sql}"
    assert "fetched_at" in sql, (
        f"news probe must use fetched_at (the loader's heartbeat), not a feed-supplied "
        f"date that can be weeks in the future. Got: {sql}"
    )


def test_selfheal_shell_tracks_the_live_news_table():
    from routes import selfheal_master_shell as S
    entry = [e for e in S.FRESHNESS if "news" in e[0]]
    assert entry, "the shell must track news freshness at all"
    table, col, sla, _why = entry[0]
    assert table == "news_articles", f"shell watches {table}, which nothing writes"
    assert col == "fetched_at", f"shell measures {col}; only fetched_at dies with the loader"


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
