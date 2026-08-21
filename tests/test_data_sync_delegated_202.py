"""Behaviour tests for data-sync.yml's three worker-delegated calls.

These RUN each step's bash against a stubbed curl rather than asserting on its
source text — this bug class is invisible to a grep, which is why #2886 shipped
the same defect in news-ner-discovery.yml after it had been fixed elsewhere.

Measured 2026-08-19. Three delegated endpoints, three different shapes of the
same blindness:

  /api/jobs/news-refresh   _WORKER_PROXY_SYNC_PATHS, 180s budget. The step DID
                           read %{http_code} — but with --max-time 180, exactly
                           the budget, so the 202 arrived only after curl had
                           given up. It saw 000 and reported "the loop did NOT
                           run" about a sync running normally on the worker:
                           the false RED that flipped brain-autonomy (#2929).

  /api/kmz-discovery/run   POST-only, 15s budget. Handler OBSERVED AT 1061.2s
                           (17.7 min) in the 2026-07-11 pool audit, so a 202 is
                           the NORMAL answer. The old one-liner piped the body
                           into `d.get('success')` — and the 202 envelope
                           CONTAINS "success": true, so it printed
                           "Success: True" for a job that had not produced
                           anything yet.

  /api/jobs/evolution      _WORKER_PROXY_SYNC_PATHS, 180s budget, but capped at
                           --max-time 60 — one THIRD of it. Every relayed run
                           hit curl's timeout, `-sf` exited non-zero, and
                           `|| echo "Evolution timed out"` printed a reassuring
                           sentence and exited 0.

Absence of a counter is UNKNOWN, never zero.

★ Watermarks. /api/jobs/status is NOT usable: it reports _scheduler_registry, a
module-level dict with one copy per service, and these jobs run on the worker —
the #2929 trap. cron_last_run.last_completed_at is, because
jobs_bp.after_request stamps it only for a request that actually ran a handler
(g._jobs_admin_authed), and a 202 relay short-circuits before the view. For KMZ,
`last_cycle` in /api/kmz-discovery/status is the same process-local trap (the
singleton's in-memory cache); `last_cycle_at` reads kmz_discovery_log instead.
"""
import ast
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/data-sync.yml")
JOBS = os.path.join(ROOT, "routes/jobs_routes.py")
KMZ = os.path.join(ROOT, "kmz_auto_discovery.py")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow scripts need bash")

DELEGATED_202 = ('{"success": true, "delegated_to": "worker", '
                 '"completed": false, "note": "job still running on '
                 'dchub-worker; check worker logs"}')

_T0 = "2026-08-19T00:04:11.221900+00:00"
_T1 = "2026-08-19T06:02:57.660412+00:00"


def _fresh(hours_ago):
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).isoformat()


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


def _block(needle):
    hits = [b for b in _run_blocks() if needle in b]
    assert len(hits) == 1, f"{needle}: expected 1 block, got {len(hits)}"
    return hits[0]


def _news_block():
    return _block("/api/jobs/news-refresh")


def _kmz_block():
    return _block("/api/kmz-discovery/run")


def _evo_block():
    return _block("/api/jobs/evolution")


def _jobs_wm(job, last_completed_at):
    if not last_completed_at:
        return ('{"success": true, "jobs": {"%s": {"last_started_at": null, '
                '"last_completed_at": null}}}' % job)
    return ('{"success": true, "jobs": {"%s": {"last_started_at": "%s", '
            '"last_completed_at": "%s", "last_status": "ok"}}}'
            % (job, last_completed_at, last_completed_at))


def _kmz_wm(last_cycle_at):
    # `last_cycle` is deliberately populated with a DIFFERENT, always-fresh
    # value: a step that read the process-local key instead of the DB-backed
    # one would then pass for the wrong reason, and the tests below would not
    # notice. This makes that substitution observable.
    if not last_cycle_at:
        return ('{"success": true, "last_cycle": "2099-01-01T00:00:00", '
                '"last_cycle_at": null}')
    return ('{"success": true, "last_cycle": "2099-01-01T00:00:00", '
            '"last_cycle_at": "%s"}' % last_cycle_at)


# Stub curl. With -o it writes the body to that file and echoes the status
# code, exactly as real curl does with -o/-w; without -o it writes the body to
# stdout. A URL containing last-run or kmz-discovery/status is a WATERMARK
# read, answered from its own env vars: the FIRST such call returns
# $STATUS_BEFORE and every later one $STATUS_AFTER, at $STATUS_CODE.
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
    *last-run*|*kmz-discovery/status*)
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


def _exec(block, run_body, run_code, status_before="", status_after="",
          status_code="200"):
    script = block.replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    script = script.replace("${{ secrets.DCHUB_INTERNAL_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        log = os.path.join(td, "urls")
        open(log, "w").close()
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", INTERNAL_KEY="k", BACKEND_URL="",
                   RUN_BODY=run_body, RUN_CODE=run_code,
                   STATUS_BEFORE=status_before, STATUS_AFTER=status_after,
                   STATUS_CODE=status_code,
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
    assert len(blocks) == 12, f"expected 12 run blocks, got {len(blocks)}"
    for b in (_news_block(), _kmz_block(), _evo_block()):
        assert len(b) > 400, "run block looks truncated"
        assert "%{http_code}" in b, (
            "a delegated call in this block does not read the HTTP status")


def test_the_watermark_stub_really_serves_two_distinguishable_bodies():
    rc, out, _ = _exec(_news_block(), DELEGATED_202, "202",
                       _jobs_wm("news-refresh", _T0),
                       _jobs_wm("news-refresh", _T1))
    assert rc == 0, out
    assert f"pre-run news-refresh last_completed_at={_T0}" in out, out
    assert f"poll 1/10 last_completed_at={_T1}" in out, out


# --------------------------------------------------------------------------
# news-refresh — the --max-time == budget defect
# --------------------------------------------------------------------------

def test_news_max_time_exceeds_the_180s_relay_budget():
    """The original defect was arithmetic, not logic: curl gave up at exactly
    the moment the 202 was due."""
    import re
    b = _news_block()
    m = re.search(r"--max-time (\d+) -o /tmp/news\.json", b)
    assert m, "could not find the news-refresh POST's --max-time"
    assert int(m.group(1)) > 180, (
        f"--max-time {m.group(1)} <= the 180s relay budget for a "
        f"_WORKER_PROXY_SYNC_PATHS endpoint — the 202 can never be observed")


def test_news_202_with_an_advancing_watermark_is_not_a_failure():
    rc, out, _ = _exec(_news_block(), DELEGATED_202, "202",
                       _jobs_wm("news-refresh", _T0),
                       _jobs_wm("news-refresh", _T1))
    assert rc == 0, f"a delegated sync that demonstrably ran failed:\n{out}"
    assert "::error::" not in out, out
    assert "UNKNOWN" in out, "must say the counter is unknown, not zero"


def test_news_202_that_never_advances_still_fails():
    rc, out, _ = _exec(_news_block(), DELEGATED_202, "202",
                       _jobs_wm("news-refresh", _T0),
                       _jobs_wm("news-refresh", _T0))
    assert rc != 0, f"a sync that never completed passed silently:\n{out}"
    assert "::error::" in out and "never advanced" in out, out


def test_news_202_polls_a_bounded_number_of_times():
    rc, out, _ = _exec(_news_block(), DELEGATED_202, "202",
                       _jobs_wm("news-refresh", _T0),
                       _jobs_wm("news-refresh", _T0))
    assert rc != 0
    assert "poll 10/10" in out and "poll 11/10" not in out, out


def test_news_no_pre_run_watermark_rejects_a_stale_timestamp():
    rc, out, _ = _exec(_news_block(), DELEGATED_202, "202",
                       _jobs_wm("news-refresh", None),
                       _jobs_wm("news-refresh", "2020-01-01T00:00:00+00:00"))
    assert rc != 0, f"a pre-existing watermark was accepted:\n{out}"
    assert "stale, not this sync" in out, out


def test_news_says_so_when_the_watermark_read_itself_fails():
    rc, out, _ = _exec(_news_block(), DELEGATED_202, "202", "", "",
                       status_code="503")
    assert rc != 0, out
    assert "watermark UNKNOWN" in out, out


def test_news_200_still_reports_its_counter():
    rc, out, _ = _exec(_news_block(),
                       '{"success": true, "new_articles": 17}', "200")
    assert rc == 0, out
    assert "new_articles= 17" in out or "new_articles= 17" in out, out
    assert "::error::" not in out, out


def test_news_non_200_still_fails_loudly():
    """The 2026-08-06 gate this replaces existed for a reason — keep it."""
    for code in ("401", "500", "000"):
        rc, out, _ = _exec(_news_block(), '{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out


# --------------------------------------------------------------------------
# kmz-discovery — the 202 envelope's own "success": true
# --------------------------------------------------------------------------

def test_kmz_202_is_not_reported_as_success():
    """THE REGRESSION. The 202 envelope contains "success": true, so
    `d.get('success')` printed "Success: True" for a job that had not started
    producing. A ~17.7-minute cycle answers 202 on nearly every run."""
    rc, out, _ = _exec(_kmz_block(), DELEGATED_202, "202",
                       _kmz_wm(_fresh(6)), _kmz_wm(_T1))
    assert rc == 0, out
    assert "Success: True" not in out, (
        "the 202 envelope's own success:true was reported as the job "
        "succeeding:\n" + out)


def test_kmz_202_with_an_advancing_watermark_is_a_confirmed_cycle():
    rc, out, _ = _exec(_kmz_block(), DELEGATED_202, "202",
                       _kmz_wm(_fresh(6)), _kmz_wm(_T1))
    assert rc == 0, out
    assert "completed on the worker" in out, out
    assert "::error::" not in out, out


def test_kmz_202_still_running_after_the_poll_is_unknown_not_failure():
    """The cycle was measured at 17.7 min and the poll covers 5. With the
    PREVIOUS cycle fresh, not-yet-landed is the expected reading — failing
    here would manufacture a red on every single run."""
    same = _fresh(6)   # ONE value: BEFORE and AFTER must be byte-identical,
                       # or the poll sees an advance for the wrong reason.
    rc, out, _ = _exec(_kmz_block(), DELEGATED_202, "202",
                       _kmz_wm(same), _kmz_wm(same))
    assert rc == 0, f"a normal 17-minute cycle was failed:\n{out}"
    assert "::error::" not in out, out
    assert "UNKNOWN" in out, out


def test_kmz_202_fails_when_the_previous_cycle_is_also_stale():
    """THE OTHER HALF — this is where a cycle that stops completing actually
    goes red. 2x the job's 6h cadence, the same rule the deadman board uses."""
    same = _fresh(30)
    rc, out, _ = _exec(_kmz_block(), DELEGATED_202, "202",
                       _kmz_wm(same), _kmz_wm(same))
    assert rc != 0, (
        "a cycle that has not landed for 30h passed as still-running:\n" + out)
    assert "::error::" in out and "stopped completing" in out, out


def test_kmz_202_fails_when_no_cycle_was_ever_recorded():
    rc, out, _ = _exec(_kmz_block(), DELEGATED_202, "202",
                       _kmz_wm(None), _kmz_wm(None))
    assert rc != 0, f"a never-recorded cycle passed:\n{out}"
    assert "::error::" in out, out


def test_kmz_reads_the_db_watermark_not_the_process_local_cache():
    """`last_cycle` comes from the _kmz_instance singleton's in-memory cache —
    one copy per service, and the cycle runs on the worker. The stub serves an
    always-fresh 2099 value there, so a step reading it would sail through the
    staleness gate and the poll for the wrong reason."""
    same = _fresh(30)
    rc, out, _ = _exec(_kmz_block(), DELEGATED_202, "202",
                       _kmz_wm(same), _kmz_wm(same))
    assert rc != 0, (
        "the step accepted the process-local last_cycle instead of the "
        "DB-backed last_cycle_at:\n" + out)
    assert "2099" not in out, out


def test_kmz_200_reports_real_counters():
    body = ('{"success": true, "results": {"total_new_routes": 12, '
            '"total_new_km": 340.5, "cycle_duration_seconds": 1061.2}}')
    rc, out, _ = _exec(_kmz_block(), body, "200")
    assert rc == 0, out
    assert "new_routes=12" in out, out
    assert "duration=1061.2s" in out, out


def test_kmz_200_skip_is_reported_as_a_skip():
    body = '{"success": true, "results": {"skipped": true, "reason": "cycle_in_progress"}}'
    rc, out, _ = _exec(_kmz_block(), body, "200")
    assert rc == 0, out
    assert "skipped" in out and "cycle_in_progress" in out, out


def test_kmz_non_2xx_fails_loudly():
    for code in ("401", "500", "000"):
        rc, out, _ = _exec(_kmz_block(), '{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out


# --------------------------------------------------------------------------
# evolution — the reassuring sentence
# --------------------------------------------------------------------------

def test_evolution_never_prints_the_reassuring_timeout_line():
    """`|| echo "Evolution timed out"` was a LIE on the relay path: the call
    did not time out, it was answered with a 202 curl never waited for."""
    rc, out, _ = _exec(_evo_block(), DELEGATED_202, "202",
                       _jobs_wm("evolution", _T0), _jobs_wm("evolution", _T1))
    assert rc == 0, out
    assert "Evolution timed out" not in out, out


def test_evolution_max_time_exceeds_the_180s_relay_budget():
    import re
    b = _evo_block()
    m = re.search(r"--max-time (\d+) -o /tmp/evo\.json", b)
    assert m, "could not find the evolution POST's --max-time"
    assert int(m.group(1)) > 180, (
        f"--max-time {m.group(1)} <= the 180s relay budget — the 202 can "
        f"never be observed (it was 60)")


def test_evolution_202_with_an_advancing_watermark_is_not_a_failure():
    rc, out, _ = _exec(_evo_block(), DELEGATED_202, "202",
                       _jobs_wm("evolution", _T0), _jobs_wm("evolution", _T1))
    assert rc == 0, out
    assert "::error::" not in out, out
    assert "UNKNOWN" in out, out


def test_evolution_202_that_never_advances_still_fails():
    rc, out, _ = _exec(_evo_block(), DELEGATED_202, "202",
                       _jobs_wm("evolution", _T0), _jobs_wm("evolution", _T0))
    assert rc != 0, f"an engine cycle that never completed passed:\n{out}"
    assert "::error::" in out and "never advanced" in out, out


def test_evolution_failure_does_not_skip_auto_approve():
    """auto-approve is a separate job in the same step. Failing evolution
    before it would silently stop staged-facility approval — the r80 defect,
    reintroduced from the other direction."""
    rc, out, urls = _exec(_evo_block(), DELEGATED_202, "202",
                          _jobs_wm("evolution", _T0),
                          _jobs_wm("evolution", _T0))
    assert rc != 0, out
    assert any("auto-approve" in u for u in urls), (
        f"auto-approve never ran when evolution failed: {urls}")


def test_evolution_200_still_reports_its_result():
    rc, out, _ = _exec(_evo_block(),
                       '{"success": true, "result": "3 mutations applied"}',
                       "200")
    assert rc == 0, out
    assert "Evolution: True" in out, out
    assert "::error::" not in out, out


def test_evolution_non_200_fails_loudly():
    for code in ("401", "500", "000"):
        rc, out, _ = _exec(_evo_block(), '{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out


# --------------------------------------------------------------------------
# The watermarks themselves
# --------------------------------------------------------------------------

def test_jobs_last_run_endpoint_exists_and_is_gated():
    src = open(JOBS, encoding="utf-8").read()
    assert "'/api/jobs/last-run'" in src, (
        "the endpoint data-sync.yml polls does not exist")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "job_last_run")
    seg = ast.get_source_segment(src, fn)
    assert "_require_admin_key()" in seg, "/api/jobs/last-run is not gated"
    assert "cron_last_run" in seg, (
        "/api/jobs/last-run no longer reads cron_last_run — _scheduler_registry "
        "is a module-level dict with one copy per service and cannot answer "
        "for a job that ran on dchub-worker")


def test_reading_the_watermark_does_not_stamp_it():
    """A read surface that refreshed the freshness it reports would be a
    perfect false green — the exact shape of the 2026-08-07 incident where
    401s from a zombie scheduler kept 12 jobs looking alive."""
    src = open(JOBS, encoding="utf-8").read()
    assert src.count("'status', 'keep-alive', 'last-run'") == 2, (
        "'last-run' is not excluded from BOTH cron_last_run stamp sites "
        "(_require_admin_key start-stamp and _stamp_cron_completion), so "
        "polling the watermark would create a phantom 'last-run' job and "
        "refresh it on every poll")


def test_kmz_status_exposes_a_db_backed_cycle_watermark():
    """last_cycle_at must come from kmz_discovery_log, not from the singleton's
    in-memory cache — web's copy is never written, the cycle runs on the
    worker. That is the #2929 trap one module over."""
    src = open(KMZ, encoding="utf-8").read()
    tree = ast.parse(src)

    # ★ AST string CONSTANTS, never the raw source segment. The block above
    # get_status explains at length why last_cycle_at is read from
    # kmz_discovery_log — and ast.get_source_segment includes comments, so a
    # substring check against it PASSES on that prose alone. Verified: a
    # mutation replacing the whole SELECT with
    # `status['last_cycle_at'] = self._cache.get('last_cycle')` survived the
    # source-segment version of this test. Comments are not AST nodes.
    def _strings(fn):
        return [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "get_status")
    lits = _strings(fn)
    assert "last_cycle_at" in lits, "get_status no longer reports last_cycle_at"
    assert any("kmz_discovery_log" in s and "discovered_at" in s for s in lits), (
        "last_cycle_at is no longer QUERIED from kmz_discovery_log — if it now "
        "comes from self._cache, data-sync.yml's poll reads web's "
        "never-written copy and can never see an advance")
    # _log_cycle is what writes that row, at the end of every completed cycle.
    log_fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_log_cycle")
    assert any("INSERT INTO kmz_discovery_log" in s for s in _strings(log_fn)), (
        "_log_cycle no longer writes kmz_discovery_log — the watermark has "
        "no writer")


def test_kmz_watermark_read_is_a_single_row_not_a_whole_table_cast():
    """2026-08-21: MAX(discovered_at::timestamptz) cast every row and hit the
    statement timeout (30s POOL HOLD per poll, 10 polls per data-sync run).
    The watermark must come from ONE row walked off the PK, never a full
    cast-and-aggregate over the log table."""
    src = open(KMZ, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "get_status")
    lits = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    wm = [s for s in lits if "kmz_discovery_log" in s and "discovered_at" in s]
    assert wm, "watermark query gone"
    q = " ".join(wm[0].split()).upper()
    assert "LIMIT 1" in q and "ORDER BY ID DESC" in q, q
    assert "::TIMESTAMPTZ" not in q and "MAX(" not in q, (
        "the watermark read casts/aggregates the whole table again — it timed "
        "out under load and held a pooled connection for 30s per poll")
