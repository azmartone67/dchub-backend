"""loop_name must not carry a measured value, or the dedupe never fires.

WHY (2026-09-08). `brain_proposed_code_fixes` has a UNIQUE index on
(loop_name, file_path, search_text), and the proposal INSERT's
`ON CONFLICT ... DO UPDATE` is what bumps approval_count/cycles_seen when a
proposal recurs. For producers that put a measurement in loop_name, every
observation minted a fresh key, so the conflict never happened and a new row
was inserted each time.

Live that night: **17 pending proposals with byte-identical search_text**
(`# Check if news_articles table exists`) on `api_fixes.py`, under loop_names
`cache_rate:24.41%` ... `cache_rate:24.97%`. The opener drafts 5 per run, so
the same one-line fix would have become 5 draft PRs per run until the pile
drained.

It also starved calibration: `_calibration_stats` groups by loop_name and
needs `_CALIB_MIN_SAMPLES` (3) resolved outcomes to tune. A key that is unique
per observation can never reach 3 — which is why every source with pending
work sat at the cold-start prior forever.

The rule is that a trailing NUMERIC segment is a measurement and an
identity segment is not. Both halves matter: over-stripping would collapse
`table:press_releases` and `table:linkedin_posts` onto one key, and the ten
`/api/...?country=XX` rows onto another, silently discarding real proposals
as duplicates. That would be worse than the bug.

Stdlib + pytest only; no DB, no network, no module import (routes/ pulls
flask + psycopg2 and registers blueprints).
"""
import pathlib
import re

import pytest

LAYER5 = (pathlib.Path(__file__).resolve().parents[1]
          / "routes/brain_v2_layer5.py")


def _load():
    """Pull the helper out of the source and exec it against a real `re`."""
    src = LAYER5.read_text()
    m = re.search(r"_MEASURED_SEGMENT_RE = .*?return stripped or name",
                  src, re.S)
    assert m, "_stable_loop_name not found — was it renamed or removed?"
    ns = {"re": re}
    exec(compile(m.group(0), str(LAYER5), "exec"), ns)
    return ns["_stable_loop_name"]


@pytest.fixture(scope="module")
def stable():
    return _load()


@pytest.mark.parametrize("raw,expected", [
    # the live cluster: 17 rows that should have been one
    ("cache_rate:24.42%", "cache_rate"),
    ("cache_rate:23.77%", "cache_rate"),
    ("cache_rate:24.97%", "cache_rate"),
    # a line number is positional — it moves when the file changes
    ("agent_hub.py:909", "agent_hub.py"),
    ("routes/foo.py:12", "routes/foo.py"),
    # no percent sign, still a measurement
    ("depth:7", "depth"),
    ("ratio:0.83", "ratio"),
    ("delta:-4.5", "delta"),
])
def test_measured_segments_are_stripped(stable, raw, expected):
    assert stable(raw) == expected


@pytest.mark.parametrize("raw", [
    # THE over-stripping guard: these are identities, not measurements.
    "table:press_releases",
    "table:linkedin_posts",
    "/api/v1/admin/facility-dedup/analyze?country=US",
    "/api/v1/admin/facility-dedup/analyze?country=DE",
    "autonomy_proactive",
    "mcp_upgrade_signals:tools",
    "dchub://audit/SH52-027",
])
def test_identity_segments_are_preserved(stable, raw):
    assert stable(raw) == raw


def test_two_table_sources_never_collapse(stable):
    """Explicitly: over-stripping would make these one key and silently
    discard one source's proposals as duplicates of the other's."""
    assert stable("table:press_releases") != stable("table:linkedin_posts")


def test_the_live_cluster_collapses_to_exactly_one_key(stable):
    """The 17 loop_names measured live on 2026-09-07 must yield one key."""
    live = ["cache_rate:23.77%", "cache_rate:24.17%", "cache_rate:24.38%",
            "cache_rate:24.41%", "cache_rate:24.42%", "cache_rate:24.44%",
            "cache_rate:24.49%", "cache_rate:24.57%", "cache_rate:24.58%",
            "cache_rate:24.59%", "cache_rate:24.67%", "cache_rate:24.68%",
            "cache_rate:24.7%", "cache_rate:24.72%", "cache_rate:24.77%",
            "cache_rate:24.81%", "cache_rate:24.97%"]
    assert len(live) == 17
    assert len({stable(n) for n in live}) == 1


@pytest.mark.parametrize("raw,expected", [
    (":42", ":42"),      # entirely a measurement — keep, never collapse to ""
    ("", ""),
    (None, None),
])
def test_never_returns_empty(stable, raw, expected):
    """An empty key would collapse every such row onto one another."""
    assert stable(raw) == expected


def test_the_insert_actually_uses_it():
    """The helper is worthless if the INSERT still passes the raw name.
    Anchors on the call so renaming the param cannot silently bypass it."""
    src = LAYER5.read_text()
    ins = src[src.index("INSERT INTO brain_proposed_code_fixes"):]
    ins = ins[:ins.index("RETURNING id, approval_count, cycles_seen") + 200]
    assert "_stable_loop_name(source_name)" in ins, (
        "the proposal INSERT no longer normalizes loop_name — the UNIQUE "
        "index cannot dedupe and ON CONFLICT DO UPDATE will not fire")
