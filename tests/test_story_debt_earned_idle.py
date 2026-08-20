#!/usr/bin/env python3
"""tests/test_story_debt_earned_idle.py — a healthy idle must be SAYABLE, and
an unproven one must stay unsaid.

NO NETWORK, NO DB.

2026-08-20. story-debt-author beat only `success` or `error`, so a run that
correctly staged nothing reported `success` with rows_inserted=0. Three of
those in a row trip the dead-man board's zero-row alarm. It fired, and
"Ingestion Integrity Master Tick" went red every day from 08-18 on
`story-debt-author (4 zero-row runs)` — while the story-debt shell's own
ship_vs_story lane read PASS, "every NEW-badged nav item is covered by a
published card". The producer was correct. The reporter had no word for it.

The fix gives the producer that word. Which makes the DANGEROUS direction the
other one: `no_new_data` is an AFFIRMATIVE healthy-idle status — routes/
ingest_runs.py counts it OK, RESETS consecutive_zero and exempts the >=3-zero
alarm. Asserted without evidence it disarms this feed forever. See
reference_dchub_zero_row_feed_semantics: a false no_new_data is worse than the
alarm it silences.

So the property under test is two-sided:
  · a proven idle MUST reach the board as no_new_data
  · an unproven one MUST NOT — unknown falls to `success`, which keeps the
    zero-row alarm armed
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "story-debt-author.yml")
AUTHOR = os.path.join(ROOT, "tools", "story_debt_author.py")


def _beat_step_run():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        d = yaml.safe_load(fh)
    for s in d["jobs"]["author"]["steps"]:
        if "beat" in str(s.get("name", "")).lower() and "run" in s:
            return s["run"]
    raise AssertionError("no beat step found in story-debt-author.yml")


def _decide(outcome, beat_status):
    """RUN the workflow's own status logic under bash and report what it picked.

    Executing the real fragment, not grepping it: a string assertion would have
    been just as green against the broken version, which is how this shipped."""
    run = _beat_step_run()
    m = re.search(r'(if \[ "\$OUTCOME".*?\nfi)', run, re.S)
    assert m, "the OUTCOME/BEAT_STATUS decision block was not found in the beat step"
    frag = m.group(1)
    script = f'OUTCOME="{outcome}"\nBEAT_STATUS="{beat_status}"\n{frag}\nprintf %s "$STATUS"\n'
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"decision fragment failed: {p.stderr[:300]}"
    return p.stdout.strip()


def test_a_proven_idle_reaches_the_board_as_no_new_data():
    assert _decide("success", "no_new_data") == "no_new_data", (
        "the author proved it ran and found nothing to write; if that still "
        "beats `success` with rows=0 the zero-row alarm convicts it again"
    )


def test_a_real_write_is_success():
    assert _decide("success", "success") == "success"


def test_a_failed_author_is_always_error():
    """error must win over everything — including a stale no_new_data output."""
    assert _decide("failure", "") == "error"
    assert _decide("failure", "no_new_data") == "error", (
        "a FAILED run that still carries a no_new_data output must beat error. "
        "Letting the assertion win here is how a stalled writer reads healthy."
    )
    assert _decide("cancelled", "no_new_data") == "error"


def test_an_unproven_idle_must_not_claim_no_new_data():
    """★ THE UNOBSERVED BRANCH, in the direction that actually hurts.

    An empty/garbage beat_status means the author did not assert anything —
    an older revision, a new code path, a truncated output. That is silence,
    and silence is not evidence. It must fall to `success`, which leaves
    rows=0 bumping consecutive_zero and the alarm armed."""
    for unproven in ("", "unknown", "NO_NEW_DATA", "no-new-data", "0", "true"):
        got = _decide("success", unproven)
        assert got == "success", (
            f"beat_status={unproven!r} produced {got!r}. Only the exact literal "
            "'no_new_data' is an assertion; anything else must keep the alarm "
            "armed rather than silently disarm this feed."
        )


def test_every_completing_author_path_declares_a_beat_status():
    """A `return 0` that sets no beat_status is an unproven idle by omission."""
    import ast
    with open(AUTHOR, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")

    returns_zero = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Return)
                    and isinstance(n.value, ast.Constant) and n.value.value == 0]
    assert len(returns_zero) >= 3, (
        f"expected >=3 success-exit paths in main(), found {len(returns_zero)} — "
        "this test is reading the wrong function or the file was restructured"
    )

    declared = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "_gh_output"
                and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "beat_status"]
    assert len(declared) >= len(returns_zero), (
        f"{len(returns_zero)} path(s) return 0 but only {len(declared)} declare "
        "a beat_status. An undeclared path beats plain `success` — safe, but it "
        "means a genuine idle keeps getting convicted."
    )


def test_the_author_refuses_to_conclude_from_an_empty_parse():
    """The empty-parse=PASS trap, guarded upstream of the beat.

    A zero-entry nav parse is instrument failure, not 'no debt'. It must exit
    non-zero so the beat is `error` — never a no_new_data asserted off an
    instrument that saw nothing."""
    with open(AUTHOR, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("if not items:")
    block = src[i:i + 400]
    assert "return 1" in block, (
        "an empty nav parse must return non-zero; concluding 'no debt' from an "
        "instrument that produced nothing is the exact trap utils/story_debt.py "
        "files as BLIND"
    )
    assert "beat_status" not in block, (
        "the empty-parse branch must not declare any beat_status — it has no "
        "evidence to assert from"
    )


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
