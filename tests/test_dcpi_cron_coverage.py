"""Behaviour tests for the DCPI daily cron's coverage guard.

r-dcpi-exactcoverage (2026-07-28). This suite RUNS the workflow's bash
against a mock endpoint rather than asserting on its source text — a
comment mentioning "coverage" satisfies a grep while the check is a
constant floor, which is precisely the state this replaced.

Each scenario is a failure actually observed live on 2026-07-28 while
sweeping the market universe by hand.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/dcpi-daily.yml")

pytestmark = pytest.mark.skipif(
    not shutil.which("jq") or not shutil.which("bash"),
    reason="cron script needs bash + jq (both present on ubuntu-latest)",
)


def _run_block():
    """The `run:` body, de-indented, with the secret templated out."""
    y = open(WORKFLOW, encoding="utf-8").read()
    marker = "        run: |\n"
    body = y[y.index(marker) + len(marker):]
    lines = [l[10:] if l.startswith(" " * 10) else l for l in body.split("\n")]
    script = "\n".join(lines)
    assert "for ATTEMPT" in script, "run block extraction looks wrong"
    return script.replace("${{ secrets.DCHUB_ADMIN_KEY }}", "$MOCK_KEY")


_MOCK = r'''
curl() {
  local url="" ; for a in "$@"; do case "$a" in *offset=*) url="$a";; esac; done
  local off; off=$(sed -n 's/.*offset=\([0-9]*\).*/\1/p' <<<"$url")
  local lim; lim=$(sed -n 's/.*limit=\([0-9]*\).*/\1/p' <<<"$url")
  local remain=$(( TOTAL_UNIVERSE - off )); (( remain < 0 )) && remain=0
  local n=$(( remain < lim ? remain : lim ))
  local ok='{"markets_scored":%d,"total_markets_known":%d}\n200\n'
  case "$MODE" in
    happy)   printf "$ok" "$n" "$TOTAL_UNIVERSE" ;;
    nofield) if [ "$off" = "50" ]; then printf '{"status":"ok","total_markets_known":%d}\n200\n' "$TOTAL_UNIVERSE"
             else printf "$ok" "$n" "$TOTAL_UNIVERSE"; fi ;;
    short)   if [ "$off" = "75" ]; then printf '{"markets_scored":1,"total_markets_known":%d}\n200\n' "$TOTAL_UNIVERSE"
             else printf "$ok" "$n" "$TOTAL_UNIVERSE"; fi ;;
    http502) if [ "$off" = "100" ]; then printf '{"status":"error","code":502}\n502\n'
             else printf "$ok" "$n" "$TOTAL_UNIVERSE"; fi ;;
    bodyerr) if [ "$off" = "25" ]; then printf '{"error":"unauthorized"}\n200\n'
             else printf "$ok" "$n" "$TOTAL_UNIVERSE"; fi ;;
    flaky)   local mark="$FLAKY_DIR/$off"
             if [ ! -e "$mark" ]; then : > "$mark"; printf '{"status":"error"}\n502\n'
             else printf "$ok" "$n" "$TOTAL_UNIVERSE"; fi ;;
  esac
  return 0
}
sleep() { :; }
MOCK_KEY="test-key"
'''


def _exec(mode, universe, key="test-key"):
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cron.sh")
        with open(path, "w") as fh:
            fh.write(_MOCK + "\n" + _run_block())
        env = dict(os.environ, MODE=mode, TOTAL_UNIVERSE=str(universe),
                   FLAKY_DIR=td)
        if key is None:
            body = _run_block().replace('MOCK_KEY="test-key"', '')
            with open(path, "w") as fh:
                fh.write(_MOCK.replace('MOCK_KEY="test-key"', 'MOCK_KEY=""')
                         + "\n" + body)
        p = subprocess.run(["bash", path], capture_output=True, text=True,
                           env=env, timeout=120)
        return p.returncode, p.stdout + p.stderr


# ── the run must go GREEN only on complete coverage ─────────────────────
@pytest.mark.parametrize("universe", [312, 450, 26])
def test_full_coverage_passes(universe):
    """Includes 450 — the growth case. Offsets used to be hardcoded
    0/100/200/300 (indices 0-399), so a universe of 450 scored 400, missed
    50, and still cleared the constant floor."""
    rc, out = _exec("happy", universe)
    assert rc == 0, out[-1500:]
    assert f"{universe}/{universe} markets scored" in out


def test_transient_failure_self_heals_and_stays_green():
    """One 502 per chunk then success. A blip must NOT red the cron —
    that is why the work is chunked and retried in the first place."""
    rc, out = _exec("flaky", 312)
    assert rc == 0, out[-1500:]
    assert "::error::" not in out
    assert "::warning::" in out, "retry should still be visible as a warning"


# ── every silent-gap shape observed live must go RED ────────────────────
@pytest.mark.parametrize("mode,why", [
    ("nofield", "HTTP 200 + valid JSON with NO markets_scored field — "
                "observed live 2026-07-28, cost a silent 25-market gap"),
    ("short",   "chunk scored 1 of the 25 it was asked for; the old "
                "`scored >= 1` check called that healthy"),
    ("http502", "Railway 502 — curl -sS exits 0 on HTTP errors, so the "
                "status must be read explicitly"),
    ("bodyerr", "HTTP 200 carrying a body-level {\"error\": …}"),
])
def test_silent_gap_shapes_fail_the_run(mode, why):
    rc, out = _exec(mode, 312)
    assert rc != 0, f"{mode} did NOT fail the run — {why}\n{out[-1500:]}"
    assert "::error::" in out


def test_missing_secret_fails_instead_of_skipping():
    """A missing secret means the cron does nothing while reporting
    success — the exact failure this workflow exists to prevent."""
    rc, out = _exec("happy", 312, key=None)
    assert rc != 0, out[-1500:]
    assert "DCHUB_ADMIN_KEY" in out


# ── structural guards ───────────────────────────────────────────────────
def test_coverage_check_is_exact_not_a_floor():
    script = _run_block()
    assert '-ne "$TOTAL_KNOWN"' in script, (
        "coverage check is no longer exact — a constant floor is what let "
        "32 markets go stale inside tolerance"
    )
    assert not re.search(r'-lt\s+280', script), "the 280 floor is back"


def test_offsets_are_derived_not_hardcoded():
    script = _run_block()
    assert "total_markets_known" in script
    assert not re.search(r'for OFFSET in 0 100 200 300', script), (
        "hardcoded offsets cover indices 0-399 only; universe growth then "
        "silently drops the tail"
    )
