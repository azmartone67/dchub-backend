"""tests/test_iso_intermittent_leash.py — a detector must not outlive its cause.

THE DEFECT, AS MEASURED. The squasher board carried one
`iso_metric_count_zero_24h` row per EU bidding zone. When the 2026-09-01
ENTSO-E outage cleared, 27 of 31 zones resumed and their rows became ghosts —
but four did not, and those four are not broken either. Probed live via
/api/v1/iso/eu/debug on 2026-09-03:

    zone       http  document                        parsed
    EU_DK_1    200   Acknowledgement_MarketDocument  None
    EU_DK_2    200   Acknowledgement_MarketDocument  None
    EU_GR      200   Acknowledgement_MarketDocument  None
    EU_IE_SEM  200   Acknowledgement_MarketDocument  None
    EU_BE      200   GL_MarketDocument               {fuels:{...}}   <- control

An Acknowledgement is ENTSO-E saying "nothing published for this query". The
EIC codes are correct and the fetch works; these zones simply do not publish
A75 Actual-Generation-per-Production-Type. Nothing is broken, so nothing is
fixable — yet at a 24h threshold each regenerated a finding EVERY DAY, forever.
That is a detector outliving its cause, and it is how the board reached 60 open
rows with a 313.9h oldest.

What is proved here:
  · an intermittent stream inside its leash files NOTHING;
  · past the leash it files again, and the detail SAYS the leash was passed —
    a longer leash, never immunity (the rule freshness_public already states);
  · a stream that is NOT registered intermittent is unaffected;
  · the leash is READ from routes/freshness_public, never restated here — the
    mutation test below fails if the detector ever grows its own copy;
  · the read is fail-soft toward FILING, never toward silence;
  · (2026-09-11) the SAME leash covers `iso_metric_count_dropped`: an EIA-930
    stream a day behind files nothing for having one row in the 24h window,
    while a normal stream, a stream past the leash, and an unknown age all
    still file.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_iso_intermittent_leash.py -v
"""
from __future__ import annotations

import inspect

import pytest

from routes import brain_consistency_radar as r
from routes import freshness_public as fp

MEASURED_DEAD = ("EU_DK_1", "EU_DK_2", "EU_GR", "EU_IE_SEM")
LEASH = fp._INTERMITTENT_MAX_H


class _Cur:
    """Answers fetches by SQL substring; records every statement."""

    def __init__(self, recent, ages):
        self.recent, self.ages = recent, ages
        self.sql = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.sql.append(self._last)

    def fetchone(self):
        if "to_regclass" in self._last:
            return ["public.grid_data"]
        return None

    def fetchall(self):
        if "INTERVAL '24 hours'" in self._last:
            return [(iso, n, None) for iso, n in self.recent.items()]
        if "GROUP BY iso" in self._last:
            return [(iso, age) for iso, age in self.ages.items()]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _run(monkeypatch, *, recent, ages):
    cur = _Cur(recent, ages)
    monkeypatch.setattr(r, "_db", lambda: _Conn(cur))
    return r.check_iso_metric_dropped(), cur


def _zeros(findings):
    return {f["url"].split("iso=")[-1] for f in findings
            if f["issue"] == "iso_metric_count_zero_24h"}


# ── the leash ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("zone", MEASURED_DEAD)
def test_an_intermittent_zone_inside_its_leash_files_nothing(monkeypatch, zone):
    findings, _ = _run(monkeypatch, recent={"PJM": 9},
                       ages={zone: LEASH - 1.0, "PJM": 0.2})
    assert zone not in _zeros(findings), (
        "%s publishes an Acknowledgement, not data — filing this daily is a "
        "detector outliving its cause" % zone)


@pytest.mark.parametrize("zone", MEASURED_DEAD)
def test_past_the_leash_it_files_again_and_says_so(monkeypatch, zone):
    """A LONGER LEASH, NEVER IMMUNITY — otherwise this list becomes a place to
    hide a genuinely dead feed, the exact failure the worst-stream rule exists
    to prevent."""
    findings, _ = _run(monkeypatch, recent={"PJM": 9},
                       ages={zone: LEASH + 1.0, "PJM": 0.2})
    assert zone in _zeros(findings)
    detail = next(f["detail"] for f in findings if zone in f["url"])
    assert "INTERMITTENT" in detail and "leash" in detail, (
        "a re-filed intermittent stream must say why it is back, or a reader "
        "re-diagnoses it from scratch")


def test_a_normal_zone_with_zero_writes_is_untouched(monkeypatch):
    """CONTROL: the leash must not become a blanket amnesty."""
    findings, _ = _run(monkeypatch, recent={"PJM": 9},
                       ages={"ERCOT": 30.0, "PJM": 0.2})
    assert "ERCOT" in _zeros(findings)
    detail = next(f["detail"] for f in findings if "ERCOT" in f["url"])
    assert "INTERMITTENT" not in detail


def test_the_detail_reports_the_real_age(monkeypatch):
    """The 24h window cannot say HOW stale a stream is — only that it is. The
    age is what tells a reader "3 days" from "3 months"."""
    findings, _ = _run(monkeypatch, recent={}, ages={"ERCOT": 73.5})
    assert "73.5h ago" in next(f["detail"] for f in findings if "ERCOT" in f["url"])


def test_a_null_last_write_still_files(monkeypatch):
    """An unknown age must not buy a free pass — that would be immunity via a
    NULL, which is how a leash becomes a hiding place."""
    findings, _ = _run(monkeypatch, recent={}, ages={"EU_GR": None})
    assert "EU_GR" in _zeros(findings)


# ── one source of truth ───────────────────────────────────────────────────

def test_the_four_measured_zones_are_registered(monkeypatch):
    got = r._intermittent_grid_streams()[0]
    for z in MEASURED_DEAD:
        assert z in got, "%s was measured returning an Acknowledgement" % z
    assert "EU_BG" in got, "the original entry must survive"
    assert "EU_BE" not in got, "BE returns real fuel data — it is the control"


def test_the_leash_is_READ_from_freshness_public_not_restated(monkeypatch):
    """★ THE ANTI-DRIFT TEST. Two lists of the same fact drift, and the half
    that drifts is the one nobody is looking at. Move the owning list and the
    detector must follow it — if it ever grows its own copy, this fails."""
    monkeypatch.setattr(fp, "_INTERMITTENT_STREAMS",
                        {"grid_data": frozenset({"ZZ_TEST_ONLY"})})
    monkeypatch.setattr(fp, "_INTERMITTENT_MAX_H", 999.0)
    streams, leash = r._intermittent_grid_streams()
    assert streams == frozenset({"ZZ_TEST_ONLY"}) and leash == 999.0
    findings, _ = _run(monkeypatch, recent={}, ages={"ZZ_TEST_ONLY": 500.0})
    assert "ZZ_TEST_ONLY" not in _zeros(findings), (
        "the detector read a stale copy instead of the owning module")
    src = inspect.getsource(r)
    assert "EU_DK_1" not in src, (
        "the zone list must live in freshness_public ONLY — a second copy here "
        "is the drift this test exists to prevent")


def test_the_read_fails_soft_toward_FILING_never_toward_silence(monkeypatch):
    """The failure DIRECTION is the whole safety property: an unreadable list
    must never quietly disarm a detector."""
    def _boom():
        raise RuntimeError("import blew up")
    monkeypatch.setattr(r, "_intermittent_grid_streams",
                        lambda: (frozenset(), 0.0))
    findings, _ = _run(monkeypatch, recent={}, ages={"EU_GR": 1.0})
    assert "EU_GR" in _zeros(findings), (
        "with no leash readable the detector must behave exactly as before"
    )


def test_the_helper_itself_swallows_and_returns_empty(monkeypatch):
    import builtins
    real = builtins.__import__

    def _blocked(name, *a, **k):
        if "freshness_public" in name:
            raise ImportError("blocked")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert r._intermittent_grid_streams() == (frozenset(), 0.0)


def test_one_query_gives_both_the_iso_list_and_the_ages(monkeypatch):
    """The old code ran SELECT DISTINCT iso; the ages it needs are free in the
    same GROUP BY, so this is one query, not two."""
    _, cur = _run(monkeypatch, recent={"PJM": 9}, ages={"PJM": 0.2})
    assert not any("SELECT DISTINCT iso" in s for s in cur.sql), (
        "the DISTINCT query should be gone — its work is done by the GROUP BY")
    assert any("GROUP BY iso" in s and "EPOCH" in s for s in cur.sql)


# ── the dropped branch (2026-09-11) ───────────────────────────────────────
#
# 10 of the 20 rows on the squasher portal's Submit-a-fix list were
# `iso_metric_count_dropped` on registered intermittent streams. EIA-930
# publishes ~26-28h late, so while a stream's newest hour is 22-24h old only
# that hour sits inside the 24h window — and Tacoma Power writes one metric
# (fuel_wat), so it "wrote only 1 metric in 24h". The leash only ever covered
# the zero-writes branch.

MEASURED_LAGGED = ("SPA", "TAL", "TPWR", "DOPD", "CHPD", "TIDC", "GVL",
                   "GCPD", "SEC", "SCL")


def _dropped(findings):
    return {f["url"].split("iso=")[-1] for f in findings
            if f["issue"] == "iso_metric_count_dropped"}


def test_the_ten_measured_streams_are_registered_intermittent():
    """The premise, pinned: if one of these ever leaves the registry, the tests
    below would pass for a reason that no longer holds."""
    got = r._intermittent_grid_streams()[0]
    missing = [s for s in MEASURED_LAGGED if s not in got]
    assert not missing, "not registered intermittent: %s" % missing


@pytest.mark.parametrize("iso", MEASURED_LAGGED)
def test_a_lagged_stream_with_one_row_in_the_window_files_nothing(monkeypatch, iso):
    """THE regression. TPWR on 2026-09-11: newest EIA hour 22.35h old, one row
    inside the 24h window — on schedule for a feed published a day late."""
    findings, _ = _run(monkeypatch, recent={iso: 1, "PJM": 9},
                       ages={iso: 22.35, "PJM": 0.2})
    assert iso not in _dropped(findings), (
        "%s is registered upstream-intermittent; a 24h row count on a feed "
        "published ~26-28h late measures the lag, not the loop" % iso)


def test_a_normal_stream_with_one_row_still_files_dropped(monkeypatch):
    """CONTROL: the leash must not become a blanket amnesty on this branch."""
    findings, _ = _run(monkeypatch, recent={"ERCOT": 1, "PJM": 9},
                       ages={"ERCOT": 0.5, "PJM": 0.2})
    assert "ERCOT" in _dropped(findings)


def test_dropped_past_the_leash_files_again(monkeypatch):
    """A LONGER LEASH, NEVER IMMUNITY — on this branch too. Reachable only with
    a leash below the 24h window, so set one there."""
    monkeypatch.setattr(fp, "_INTERMITTENT_MAX_H", 1.0)
    findings, _ = _run(monkeypatch, recent={"TPWR": 1, "PJM": 9},
                       ages={"TPWR": 5.0, "PJM": 0.2})
    assert "TPWR" in _dropped(findings)


def test_dropped_with_an_unknown_age_still_files(monkeypatch):
    """An unknown age must not buy a pass here either — the dangerous direction."""
    findings, _ = _run(monkeypatch, recent={"TPWR": 1, "PJM": 9},
                       ages={"TPWR": None, "PJM": 0.2})
    assert "TPWR" in _dropped(findings)
