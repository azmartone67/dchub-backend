"""tests/test_iso_cadence_source_of_truth.py — one surface, one cadence (2026-08-12).

routes/heartbeat.py OWNS the iso_metrics surface and declares when it is stale.
routes/system_loops.py used to carry its own copy of that number — 6.0 against
heartbeat's 12 — and _classify() calls anything past 1x cadence "stale". So the
loop board flipped iso_extract to stale for half of every normal cycle, and
because dcpi_recompute declares iso_extract as an input, shell #49's edge logic
then reported DCPI as GREEN-ON-STALE. A false alarm manufactured entirely by two
copies of one constant disagreeing.

★The same bug is documented one line above the iso_metrics entry in heartbeat.py
for a different surface: "news_cache cap 6h → 8h. Cron is every 6h; 6h cap left
zero jitter budget so the dashboard alternated FRESH/STALE." Second surface,
same cause: the number was copied instead of read.

Guards:
  (1) DRIFT — system_loops re-grows a hardcoded cadence for iso_extract.
  (2) SILENT-TIGHTEN — the fallback drops below heartbeat's declared value, so
      an import failure quietly restores the alarming threshold.
  (3) THE ACTUAL REGRESSION — 6.72h (the observed value on 2026-08-12) must not
      classify as stale.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_iso_cadence_source_of_truth.py -v
"""
from __future__ import annotations

import importlib
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _loops():
    return importlib.import_module("routes.system_loops")


def _declared_stale_hours() -> float:
    from routes.heartbeat import SURFACES
    for s in SURFACES:
        if s.get("name") == "iso_metrics":
            return float(s["stale_hours"])
    raise AssertionError("iso_metrics is no longer declared in heartbeat.SURFACES")


def test_cadence_comes_from_heartbeat_not_a_copy():
    assert _loops()._iso_cadence_hours() == _declared_stale_hours()


def test_fallback_never_tightens_below_the_declared_value():
    """★If heartbeat cannot be imported, the probe must not quietly revert to a
    stricter number — that is how the false alarm comes back, invisibly."""
    m = _loops()
    assert m._ISO_CADENCE_FALLBACK >= _declared_stale_hours()


def test_the_observed_false_alarm_no_longer_classifies_as_stale():
    """6.72h is what the board actually read at 2026-08-12T04:00Z, while the ISO
    telemetry workflow had succeeded on all 8 of its previous 20-minute runs."""
    m = _loops()
    assert m._classify(6.72, m._iso_cadence_hours()) == "alive"


def test_a_genuinely_dead_surface_still_classifies_dead():
    """The fix must not blind the probe. Past 3x cadence is still dead."""
    m = _loops()
    cadence = m._iso_cadence_hours()
    assert m._classify(cadence * 3.5, cadence) == "dead"
    assert m._classify(cadence * 1.5, cadence) == "stale"


def test_no_hardcoded_iso_cadence_remains_in_the_probe():
    src = (_ROOT / "routes" / "system_loops.py").read_text(encoding="utf-8")
    probe = src.split("def _probe_iso_extract")[1].split("\ndef ")[0]
    assert "6.0" not in probe, \
        "_probe_iso_extract re-grew a hardcoded cadence — read heartbeat.SURFACES"


def test_inspector_failure_diagnostics_reach_the_log():
    """★The 2026-08-12T00:36Z failure produced ONE line of evidence: 'Process
    completed with exit code 1'. The block's stdout is redirected to
    $GITHUB_STEP_SUMMARY, which swallowed both the annotation and the JSON dump,
    and the annotations API had nothing either. Diagnostics must go to stderr,
    which is not redirected."""
    wf = (_ROOT / ".github" / "workflows" / "brain-inspector.yml").read_text(
        encoding="utf-8")
    assert "file=sys.stderr" in wf, \
        "inspector failure diagnostics no longer go to stderr — a failing run " \
        "will be silent in the log again"
    assert "print(f'::warning::Inspector returned" not in wf, \
        "the ::warning:: is back on stdout, where the summary redirect eats it"
