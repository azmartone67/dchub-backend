"""check_brain_evolution_jobs_alive — the detector that would have caught
be#5453's first live run (both jobs wrote nothing; nothing watched them)."""
import datetime as dt

from routes import brain_consistency_radar as R

NOW = dt.datetime(2026, 9, 25, 7, 0, tzinfo=dt.timezone.utc)


def _issues(last):
    return sorted(f["issue"] for f in R.evolution_job_findings(last, NOW))


def test_never_written_fires_for_both():
    """The 2026-09-24 state: tables empty after the first run failed."""
    assert _issues({"brain_lessons": None, "brain_evolution_scorecard": None}) == [
        "brain_evolution_scorecard_dead", "brain_lessons_compile_dead"]


def test_fresh_output_is_quiet_and_stale_output_fires():
    fresh = NOW - dt.timedelta(hours=20)
    stale = NOW - dt.timedelta(hours=40)      # MUTATION: drop the age test
    assert _issues({"brain_lessons": fresh, "brain_evolution_scorecard": fresh}) == []
    assert _issues({"brain_lessons": stale,
                    "brain_evolution_scorecard": fresh}) == ["brain_lessons_compile_dead"]


def test_naive_timestamps_are_read_as_utc():
    naive = (NOW - dt.timedelta(hours=1)).replace(tzinfo=None)
    assert _issues({"brain_lessons": naive}) == []


def test_unreadable_table_is_unmeasured_not_dead():
    """MUTATION: treat a missing key as None → a DB blip pages as dead jobs."""
    assert _issues({}) == []


def test_detector_is_registered_in_the_sweep():
    import ast, inspect
    src = inspect.getsource(R.scan_all)
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "check_brain_evolution_jobs_alive" in names
