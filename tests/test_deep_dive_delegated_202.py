"""Behaviour tests for facility-snapshot-daily.yml's deep-dive step, and for
the run watermark it polls.

These RUN the workflow's bash against a stubbed curl rather than asserting on
its source text — this bug class is invisible to a grep, which is why #2886
shipped the same defect in news-ner-discovery.yml after it had been fixed
elsewhere.

Measured 2026-08-19. The step was blind twice over:

  1. It POSTed through dchub.cloud, where CF's ROUTE_TIMEOUTS 15s default
     answers an admin POST with a 503 long before a rotation of up to 15
     sequential Claude calls finishes. The DCPI-snapshot step in the same file
     already goes origin-direct for exactly this reason.
  2. It read no status code. /api/v1/markets/deep-dive/cron is in main.py's
     _WORKER_PROXY_POST_PATHS but NOT _WORKER_PROXY_SYNC_PATHS, so the origin
     relays it to dchub-worker on a 15s read budget and answers:

        202 {"success": true, "delegated_to": "worker", "completed": false,
             "note": "job still running on dchub-worker; check worker logs"}

     `curl -sS ... | python3 -m json.tool || true` printed that and exited 0.
     tools/deadman/watch.py beats this feed off the run CONCLUSION (30h), so
     the public board read green either way.

Absence of a counter is UNKNOWN, never zero.

★ The watermark is /deep-dive/status last_cron_run, NOT MAX(generated_at).
generate_for_market() writes nothing on market_not_found, nothing on an
_ask_claude_to_write error, and its brief-guard seed is
INSERT ... ON CONFLICT DO NOTHING — so a rotation whose targets are all
guarded completes without moving generated_at at all. Polling that column
would fail a healthy cron, which is the false-red #2929 removed from
brain-autonomy. _record_cron_run() stamps the run itself, unconditionally, in
the DB — so web and dchub-worker read the same value.
"""
import ast
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/facility-snapshot-daily.yml")
MODULE = os.path.join(ROOT, "routes/market_deep_dive.py")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow scripts need bash")

DELEGATED_202 = ('{"success": true, "delegated_to": "worker", '
                 '"completed": false, "note": "job still running on '
                 'dchub-worker; check worker logs"}')

_T0 = "2026-08-18T05:31:04.221900+00:00"
_T1 = "2026-08-19T05:24:19.660412+00:00"


def _run_blocks():
    y = open(WORKFLOW, encoding="utf-8").read()
    marker = "        run: |\n"
    out, idx = [], 0
    while True:
        i = y.find(marker, idx)
        if i < 0:
            break
        start = i + len(marker)
        ends = [y.find(sep, start) for sep in ("\n      - name:", "\n      # ")]
        ends = [e for e in ends if e > 0]
        nxt = min(ends) if ends else -1
        body = y[start:nxt if nxt > 0 else len(y)]
        lines = [l[10:] if l.startswith(" " * 10) else l
                 for l in body.split("\n")]
        out.append("\n".join(lines))
        idx = start
    return out


def _dd_block():
    for b in _run_blocks():
        if "deep-dive/cron" in b:
            return b
    raise AssertionError("deep-dive run block not found")


def _status(last_cron_run):
    if not last_cron_run:
        return '{"ok": true, "last_cron_run": null, "generated_count": null}'
    return ('{"ok": true, "last_cron_run": "%s", "targets": 5, '
            '"generated_count": 0, "latest_generated_at": "2026-01-01T00:00:00+00:00"}'
            % last_cron_run)


# Stub curl. With -o it writes the body to that file and echoes the status
# code, as real curl does with -o/-w. A URL containing /deep-dive/status is a
# watermark read, answered from its OWN env vars: the FIRST such call returns
# $STATUS_BEFORE and every later one $STATUS_AFTER, at $STATUS_CODE.
# $LAST_URL records the last non-status URL so a test can prove the POST went
# to the Railway origin rather than through the CF edge.
_MOCK = r'''
curl() {
  local out="" prev="" url="" body="" code="" n=0
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    case "$a" in http*://*) url="$a" ;; esac
    prev="$a"
  done
  printf '%s\n' "$url" >> "$URL_LOG"
  case "$url" in
    */deep-dive/status*)
      n=$(cat "$CURL_STATE" 2>/dev/null || echo 0)
      n=$((n + 1))
      printf '%s' "$n" > "$CURL_STATE"
      if [ "$n" = "1" ]; then body="$STATUS_BEFORE"; else body="$STATUS_AFTER"; fi
      code="$STATUS_CODE"
      ;;
    *) body="$RUN_BODY"; code="$RUN_CODE" ;;
  esac
  if [ -n "$out" ]; then
    printf '%s' "$body" > "$out"
    printf '%s' "$code"
  else
    printf '%s' "$body"
  fi
  return 0
}
sleep() { :; }
'''


def _exec(run_body, run_code, status_before="", status_after="",
          status_code="200"):
    script = _dd_block().replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        log = os.path.join(td, "urls")
        open(log, "w").close()
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", RUN_BODY=run_body,
                   RUN_CODE=run_code, STATUS_BEFORE=status_before,
                   STATUS_AFTER=status_after, STATUS_CODE=status_code,
                   CURL_STATE=os.path.join(td, "n"), URL_LOG=log)
        p = subprocess.run(["bash", sh], env=env, capture_output=True,
                           text=True, cwd=td)
        urls = [l for l in open(log).read().splitlines() if l]
        return p.returncode, p.stdout + p.stderr, urls


# --------------------------------------------------------------------------
# Non-vacuity
# --------------------------------------------------------------------------

def test_extraction_is_not_vacuous():
    blocks = _run_blocks()
    assert len(blocks) == 7, f"expected 7 run blocks, got {len(blocks)}"
    b = _dd_block()
    assert len(b) > 400, "run block looks truncated"
    assert "%{http_code}" in b, "the step does not read the HTTP status"


def test_the_status_stub_really_serves_the_watermark():
    rc, out, _ = _exec(DELEGATED_202, "202", _status(_T0), _status(_T1))
    assert rc == 0, out
    assert f"pre-run last_cron_run={_T0}" in out, out
    assert f"poll 1/10 last_cron_run={_T1}" in out, out


# --------------------------------------------------------------------------
# Defect 1 — the edge. A 15s CF route timeout and a cached watermark are both
# fatal to this step, and neither is visible in the step's own output.
# --------------------------------------------------------------------------

def test_every_call_goes_to_the_railway_origin_not_the_cf_edge():
    """dchub.cloud applies ROUTE_TIMEOUTS 15s to admin POSTs, so the rotation
    could never finish through it — and CF Rule #3 caches /api/v1/* with
    mode: override_origin, so a watermark polled through the edge can return a
    cached value and never move, failing every delegated run."""
    rc, out, urls = _exec(DELEGATED_202, "202", _status(_T0), _status(_T1))
    assert rc == 0, out
    assert urls, "no curl calls were made at all"
    assert any("deep-dive/cron" in u for u in urls), urls
    assert any("deep-dive/status" in u for u in urls), urls
    for u in urls:
        assert "dchub.cloud" not in u, (
            f"this call still goes through the CF edge: {u}")
        assert u.startswith("https://dchub-backend-production.up.railway.app"), u


# --------------------------------------------------------------------------
# Defect 2 — the 202
# --------------------------------------------------------------------------

def test_202_with_an_advancing_watermark_is_not_a_failure():
    rc, out, _ = _exec(DELEGATED_202, "202", _status(_T0), _status(_T1))
    assert rc == 0, f"a delegated rotation that demonstrably ran failed:\n{out}"
    assert "::error::" not in out, out
    assert "UNKNOWN" in out, "must say the counter is unknown, not zero"


def test_202_does_not_report_a_zero_generated_count():
    """THE REGRESSION. The 202 body has no generated_count."""
    rc, out, _ = _exec(DELEGATED_202, "202", _status(_T0), _status(_T1))
    assert rc == 0, out
    assert "generated=0" not in out, (
        "a still-running delegated rotation was reported as 0 generated:\n"
        + out)


def test_202_that_never_advances_still_fails():
    rc, out, _ = _exec(DELEGATED_202, "202", _status(_T0), _status(_T0))
    assert rc != 0, f"a rotation that never completed passed silently:\n{out}"
    assert "::error::" in out and "never advanced" in out, out


def test_202_with_a_watermark_that_never_appears_still_fails():
    rc, out, _ = _exec(DELEGATED_202, "202", _status(None), _status(None))
    assert rc != 0, f"a null watermark was accepted as completion:\n{out}"
    assert "::error::" in out, out


def test_202_polls_a_bounded_number_of_times():
    rc, out, _ = _exec(DELEGATED_202, "202", _status(_T0), _status(_T0))
    assert rc != 0
    assert "poll 10/10" in out, out
    assert "poll 11/10" not in out, out


def test_no_pre_run_watermark_rejects_a_stale_timestamp():
    """With no BEFORE, yesterday's rotation would otherwise pass as today's."""
    rc, out, _ = _exec(DELEGATED_202, "202", _status(None),
                       _status("2020-01-01T00:00:00+00:00"))
    assert rc != 0, f"a pre-existing watermark was accepted:\n{out}"
    assert "stale, not this rotation" in out, out


def test_no_pre_run_watermark_accepts_a_run_stamped_after_the_post():
    rc, out, _ = _exec(DELEGATED_202, "202", _status(None),
                       _status("2099-01-01T00:00:00+00:00"))
    assert rc == 0, f"a genuinely fresh watermark was rejected:\n{out}"
    assert "completed on the worker" in out, out


def test_says_so_when_the_watermark_read_itself_fails():
    rc, out, _ = _exec(DELEGATED_202, "202", "", "", status_code="503")
    assert rc != 0, out
    assert "watermark UNKNOWN" in out, (
        "a failed watermark read was silently treated as no-progress:\n" + out)


# --------------------------------------------------------------------------
# The 200 paths
# --------------------------------------------------------------------------

def test_a_real_200_reports_what_it_generated():
    body = ('{"generated_count": 4, "results": [{"ok": true}, {"ok": true}, '
            '{"ok": true}, {"ok": true}, {"ok": false, "error": '
            '"brief_guard_no_facilities"}], "ran_at": "2026-08-19T05:24:19Z"}')
    rc, out, _ = _exec(body, "200")
    assert rc == 0, out
    assert "generated=4 of 5 targets" in out, out
    assert "brief_guard_no_facilities" in out, (
        "per-target errors must stay visible:\n" + out)
    assert "::error::" not in out, out


def test_an_observed_zero_rotation_is_still_reported_as_zero():
    """Every target brief-guarded is a completed run that wrote nothing. That
    is real information and stays visible — the fix must not swallow it along
    with the unknown case, and it must not fail the job either."""
    body = ('{"generated_count": 0, "results": [{"ok": false, "error": '
            '"brief_guard_no_facilities"}], "ran_at": "2026-08-19T05:24:19Z"}')
    rc, out, _ = _exec(body, "200")
    assert rc == 0, f"an observed zero-generation rotation failed the job:\n{out}"
    assert "generated=0 of 1 targets" in out, out


def test_a_200_in_an_unrecognised_shape_is_not_a_zero_rotation():
    rc, out, _ = _exec('{"ok": true}', "200")
    assert rc == 0, out
    assert "UNRECOGNISED" in out, out
    assert "generated=0" not in out, out


def test_a_non_200_fails_loudly():
    """Including the CF 503 this step used to receive and discard."""
    for code in ("401", "403", "500", "503"):
        rc, out, _ = _exec('{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out


def test_a_dead_origin_fails_loudly():
    rc, out, _ = _exec("", "000")
    assert rc != 0, f"a dead origin did not fail the step:\n{out}"
    assert "::error::" in out, out


# --------------------------------------------------------------------------
# The watermark itself
# --------------------------------------------------------------------------

def _module_src():
    return open(MODULE, encoding="utf-8").read()


def test_the_watermark_is_stamped_on_every_rotation_not_only_on_a_write():
    """If _record_cron_run moved behind `if generated:` — or inside
    generate_for_market — a rotation whose targets are all brief-guarded would
    stop advancing the watermark and the cron would fail on a healthy run."""
    src = _module_src()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "cron_rotate")
    calls = [n for n in fn.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
             and isinstance(n.value.func, ast.Name)
             and n.value.func.id == "_record_cron_run"]
    assert calls, (
        "_record_cron_run() is not called unconditionally at the top level of "
        "cron_rotate — a rotation that generated nothing would leave the "
        "watermark unmoved and fail the cron")


def test_the_watermark_is_db_backed_not_a_module_global():
    """The #2929 trap, asserted rather than assumed: process-local state gives
    web and dchub-worker separate copies, and the poll can never see an
    advance. A DB read needs no worker-proxy allowlist entry."""
    src = _module_src()
    tree = ast.parse(src)
    for name in ("_record_cron_run", "_last_cron_run"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_conn" for n in ast.walk(fn)), (
            f"{name}() no longer opens a DB connection — if the watermark is "
            f"now process-local, /api/v1/markets/deep-dive/status must be "
            f"added to main.py's worker-proxy allowlist or the poll in "
            f"facility-snapshot-daily.yml can never see an advance")
        # ★ AST string CONSTANTS, not ast.get_source_segment. That helper
        # includes COMMENTS, and the block above these functions explains
        # brain_state at length — so a substring check against the source
        # segment passes on prose alone even after every SQL statement is
        # gone. (Measured: the equivalent check in
        # tests/test_data_sync_delegated_202.py survived a mutation that
        # replaced its whole query with a process-local read.) Comments are
        # not AST nodes.
        lits = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert any("brain_state" in s for s in lits), (
            f"{name}() no longer QUERIES brain_state")


def test_the_status_endpoint_exists_and_is_admin_gated():
    src = _module_src()
    assert '"/api/v1/markets/deep-dive/status"' in src, (
        "the endpoint facility-snapshot-daily.yml polls does not exist")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "cron_status")
    seg = ast.get_source_segment(src, fn)
    assert "unauthorized" in seg, "/markets/deep-dive/status is not gated"
    # Fail-CLOSED specifically. `if _ADMIN_KEY and provided != _ADMIN_KEY`
    # skips auth entirely when the env var is unset — which is what a
    # misconfigured process looks like. This endpoint was written that way
    # first and tests/test_admin_gate_fail_closed.py caught it.
    assert "require_internal_or_admin" in seg, (
        "/markets/deep-dive/status must use internal_auth."
        "require_internal_or_admin, not a self-disabling _ADMIN_KEY compare")


def test_the_status_endpoint_does_not_report_max_generated_at_as_completion():
    """latest_generated_at is informational. If the workflow ever polls IT
    instead of last_cron_run, an all-guarded rotation reads as a failure."""
    b = _dd_block()
    assert "last_cron_run" in b, b
    assert "latest_generated_at" not in b, (
        "the workflow is polling latest_generated_at — a rotation whose "
        "targets are all brief-guarded completes without moving it")
