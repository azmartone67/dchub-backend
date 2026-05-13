"""routes.system_loops._classify status thresholds.

The truth endpoint reports each loop as alive/stale/dead based on
age_hours vs cadence_hours. This 3-line classifier is the literal
definition of "is the system healing itself?" — get the thresholds
wrong and the dashboard lies.

Note: imported via source extraction to avoid hitting psycopg2 at
module load.
"""
import os
import re
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SL = os.path.join(ROOT, "routes", "system_loops.py")


def _extract(name):
    src = open(SL, encoding="utf-8").read()
    m = re.search(
        rf"^def {name}\(.*?(?=^def |^class |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m, f"function {name} not found in system_loops.py"
    ns = {}
    # _hours_since uses datetime + timezone; preload them.
    from datetime import datetime as _dt, timezone as _tz
    ns["datetime"], ns["timezone"] = _dt, _tz
    exec(m.group(0), ns)
    return ns[name]


def test_classify_alive_within_cadence():
    cl = _extract("_classify")
    assert cl(0.5, 1.0) == "alive"
    assert cl(1.0, 1.0) == "alive"
    # Exactly at threshold is alive (cadence boundary is inclusive).


def test_classify_stale_at_2x_cadence():
    cl = _extract("_classify")
    assert cl(2.0, 1.0) == "stale"
    assert cl(2.9, 1.0) == "stale"


def test_classify_dead_at_3x_plus_cadence():
    cl = _extract("_classify")
    assert cl(3.0, 1.0) == "stale"      # boundary
    assert cl(3.5, 1.0) == "dead"
    assert cl(72.0, 1.0) == "dead"


def test_classify_dead_when_never_seen():
    """age_hours=None means 'we have no record of this loop running' —
    that's the most-dead state."""
    cl = _extract("_classify")
    assert cl(None, 1.0) == "dead"
    assert cl(None, 24.0) == "dead"


def test_classify_respects_cadence_scale():
    """A 24h cadence (auto_press_daily) should still be 'alive' at
    23h — verifies the threshold scales with cadence, not absolute."""
    cl = _extract("_classify")
    assert cl(23.0, 24.0) == "alive"
    assert cl(48.0, 24.0) == "stale"
    assert cl(96.0, 24.0) == "dead"


def test_hours_since_handles_tz_naive():
    """The DB returns timezone-naive datetimes via psycopg2 unless
    configured otherwise. _hours_since must promote them to UTC."""
    fn = _extract("_hours_since")
    # 2 hours ago, no tzinfo
    naive = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
    age = fn(naive)
    assert age is not None
    assert 1.9 < age < 2.1


def test_hours_since_handles_none():
    fn = _extract("_hours_since")
    assert fn(None) is None


def test_hours_since_handles_tz_aware():
    fn = _extract("_hours_since")
    aware = datetime.now(timezone.utc) - timedelta(hours=5)
    age = fn(aware)
    assert age is not None
    assert 4.9 < age < 5.1
