"""ai_platform_crawl_drop declares that `count` is a volume (be#5316).

The detector writes `"count": cur_7d` — how many requests the platform sent in
the last 7 days. Undeclared, that integer read as a recurrence tally: 503 is
under the 10k untyped ceiling, so the enhancer rendered
"ai_platform_crawl_drop:you @ ai_requests (seen x503)" and the agenda filed a
spec asking for a dedup key on a finding that was already one row per platform.

House rule: tests NEVER import main.
"""


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return ("ai_requests",)            # to_regclass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


def _findings(monkeypatch, rows):
    import routes.brain_consistency_radar as bcr
    monkeypatch.setattr(bcr, "_db", lambda: _Conn(rows))
    return {f["issue"]: f for f in bcr.check_ai_platform_crawl_drop()}


def test_crawl_drop_count_is_not_an_occurrence(monkeypatch):
    """Rows are (platform, cur_7d, prior_7d). 503 vs 2,600 is an 81% drop."""
    from routes.brain_work_selector import occurrence_signal, row_count_is_value
    found = _findings(monkeypatch, [("you", 503, 2600)])
    f = found["ai_platform_crawl_drop:you"]
    assert f["count"] == 503
    occ, why = occurrence_signal(f)
    assert occ == 0 and why["source"] == "declared_value", (occ, why)
    assert row_count_is_value(f)


def test_the_detector_still_fires_and_still_skips(monkeypatch):
    """The declaration must not change WHAT is detected: an 81% drop on an
    active platform fires, a 50% drop and an inactive platform do not."""
    found = _findings(monkeypatch, [
        ("you",     503, 2600),   # 81% drop, active  -> fires
        ("chatgpt", 1000, 2000),  # 50% drop          -> below threshold
        ("tiny",    0,   100),    # prior < 140       -> was never active
    ])
    assert set(found) == {"ai_platform_crawl_drop:you"}
