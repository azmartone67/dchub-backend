"""The evolution-story contract (wave 3, 2026-07-31).

The operator's complaint: "our story on our evolution is so strong, but we
aren't telling it." The fix has two halves — the analyst note's evolution
section, and the ascension shell lane that greps for it. These tests pin the
CONTRACT between them: one heading constant, imported by both sides.
"""
import inspect


def test_heading_constant_and_prompt_wiring():
    from routes import analyst_note as an
    assert an.EVOLUTION_HEADING == "What DC Hub shipped this week"
    src = inspect.getsource(an._compose_prompt)
    # evolution must be IN the serialized tuple (and first — the blob truncates
    # at 14k; a section the writer never sees never gets written).
    assert '"evolution"' in src
    assert src.index('"evolution"') < src.index('"leads"')
    # the required-section instruction rides EVOLUTION_HEADING, not a copy
    assert "EVOLUTION_HEADING" in src


def test_prompt_demands_section_only_when_inputs_exist():
    import datetime as dt
    from routes import analyst_note as an
    week = dt.date(2026, 7, 27)
    with_ev = an._compose_prompt(
        {"evolution": {"merged_prs_7d": 9, "recent_prs": []}}, [], week)
    without = an._compose_prompt({}, [], week)
    assert an.EVOLUTION_HEADING in with_ev
    assert an.EVOLUTION_HEADING not in without


def test_shell_lane_reads_the_contract_not_a_transcription():
    from routes import brain_ascension_master_shell as shell
    src = inspect.getsource(shell._lane_evolution_story)
    assert "from routes.analyst_note import EVOLUTION_HEADING" in src
    # the heading STRING must not be re-typed in the shell — that is the
    # transcribed-contract drift the anchor-contract incident taught us.
    assert "What DC Hub shipped this week" not in src


def test_lane_fails_soft_without_db(monkeypatch):
    from routes import brain_ascension_master_shell as shell
    monkeypatch.setattr(shell, "_conn", lambda: None)
    checks = shell._lane_evolution_story()
    assert isinstance(checks, list) and checks
    assert all(isinstance(c, dict) for c in checks)
    assert not any(c.get("passed") for c in checks)  # degraded, never green
