"""Behaviour tests for iso-queue-ingest.yml's 202 handling and its beat.

These RUN the workflow's bash against a stubbed curl rather than asserting on
its source text — this bug class is invisible to a grep, which is why #2886
shipped the same defect in news-ner-discovery.yml after it had already been
fixed elsewhere.

Measured 2026-08-19. `/api/v1/iso-queue/ingest` is in main.py's
_WORKER_PROXY_POST_PATHS but NOT _WORKER_PROXY_SYNC_PATHS, so its relay read
budget is 15s, not 180 —
`_read_budget = 180 if request.path in _WORKER_PROXY_SYNC_PATHS else 15`.
When the ingest outlives it, _delegate_to_worker() answers:

    202 {"success": true, "delegated_to": "worker", "completed": false,
         "note": "job still running on dchub-worker; check worker logs"}

That body parses and carries no isos_fetched / isos_with_new_data. The step
read none of it — no %{http_code} at all — printed
`fetched None of None | with_data None | as_of None`, and exited 0. The beat
step at the end of the job then wrote a HARDCODED status="success" with
rows_inserted=${INSERTED_ROWS:-0} to https://dchub.cloud/api/v1/ops/deadman:
a healthy ZERO-ROW run asserted about an ingest whose end this workflow never
saw, which also climbs ingest_runs' consecutive-zero alarm.

Absence of a counter is UNKNOWN, never zero.

The watermark polled on the 202 path is MAX(last_run) over
/api/v1/iso-queue/ingest/status. It is DB-backed — routes/iso_queue_ingest.py
::status does SELECT MAX(ingested_at) ... GROUP BY iso over iso_queue_snapshots
— so web and dchub-worker read the SAME value and that GET does not need to be
in the worker-proxy allowlist, unlike /api/v1/brain/autonomy/status in #2929
(a module global, one never-written copy per service).
"""
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/iso-queue-ingest.yml")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow scripts need bash")

# The 202 envelope _delegate_to_worker() actually returns, verbatim.
DELEGATED_202 = ('{"success": true, "delegated_to": "worker", '
                 '"completed": false, "note": "job still running on '
                 'dchub-worker; check worker logs"}')

_T0 = "2026-08-18T06:01:18.424508+00:00"   # measured live 2026-08-19
_T1 = "2026-08-19T05:52:41.907114+00:00"


def _run_blocks():
    """Every `run: |` body in the workflow, de-indented by its 10-space stanza."""
    y = open(WORKFLOW, encoding="utf-8").read()
    marker = "        run: |\n"
    out, idx = [], 0
    while True:
        i = y.find(marker, idx)
        if i < 0:
            break
        start = i + len(marker)
        # Steps are separated by a 6-space `- name:`; comments between steps
        # sit at 6 spaces too, so cut at whichever comes first.
        ends = [y.find(sep, start) for sep in ("\n      - name:", "\n      # ")]
        ends = [e for e in ends if e > 0]
        nxt = min(ends) if ends else -1
        body = y[start:nxt if nxt > 0 else len(y)]
        lines = [l[10:] if l.startswith(" " * 10) else l
                 for l in body.split("\n")]
        out.append("\n".join(lines))
        idx = start
    return out


def _agg_block():
    for b in _run_blocks():
        if "/api/v1/iso-queue/ingest\"" in b:
            return b
    raise AssertionError("aggregate-ingest run block not found")


def _beat_block():
    for b in _run_blocks():
        if "ingest-runs/beat" in b:
            return b
    raise AssertionError("deadman-beat run block not found")


def _status(last_run):
    """An /ingest/status body whose MAX(last_run) is `last_run`.

    Two ISOs, the OLDER one listed first, so a test that accidentally read
    isos[0] instead of the max would fail rather than agree by luck.
    """
    older = "2026-01-01T00:00:00.000000+00:00"
    if not last_run:
        return '{"isos": [{"iso": "ERCOT", "last_run": null}]}'
    return ('{"isos": [{"iso": "ERCOT", "last_run": "%s"}, '
            '{"iso": "PJM", "last_run": "%s"}]}' % (older, last_run))


# Stub curl. With -o it writes the body to that file and echoes the status
# code, exactly as real curl does with -o/-w; without -o it writes the body to
# stdout. Any -d/--data payload is echoed to stderr as `BEAT_PAYLOAD <json>`
# so the beat tests can assert on the JSON actually sent.
#
# A URL containing /ingest/status is a watermark read, answered from its OWN
# pair of env vars: the FIRST such call returns $STATUS_BEFORE and every later
# one $STATUS_AFTER, at $STATUS_CODE. A test therefore picks whether the
# watermark advances, and can make the watermark read itself fail or 202
# independently of the job endpoint. Everything else in the block — the
# branching, the loop, the exits — is real bash.
_MOCK = r'''
curl() {
  local out="" prev="" url="" data="" body="" code="" n=0
  for a in "$@"; do
    case "$prev" in
      -o) out="$a" ;;
      -d|--data) data="$a" ;;
    esac
    case "$a" in http*://*) url="$a" ;; esac
    prev="$a"
  done
  if [ -n "$data" ]; then printf 'BEAT_PAYLOAD %s\n' "$data" >&2; fi
  case "$url" in
    */ingest/status*)
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


def _exec(block, run_body="", run_code="200", status_before="",
          status_after="", status_code="200", **env_extra):
    """Run one de-indented block against the stub.

    Returns (rc, stdout+stderr, dict of what the block wrote to $GITHUB_ENV).
    """
    script = block.replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        ghe = os.path.join(td, "env.txt")
        open(ghe, "w").close()
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", RUN_BODY=run_body,
                   RUN_CODE=run_code, STATUS_BEFORE=status_before,
                   STATUS_AFTER=status_after, STATUS_CODE=status_code,
                   CURL_STATE=os.path.join(td, "n"), GITHUB_ENV=ghe,
                   RAILWAY_BASE="https://example.invalid")
        env.update({k: str(v) for k, v in env_extra.items()})
        p = subprocess.run(["bash", sh], env=env, capture_output=True,
                           text=True, cwd=td)
        kv = {}
        for line in open(ghe):
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.rstrip("\n")
        return p.returncode, p.stdout + p.stderr, kv


# --------------------------------------------------------------------------
# Non-vacuity: if extraction broke, every scenario below would run an empty
# script and pass. Assert we really pulled the blocks we think we did.
# --------------------------------------------------------------------------

def test_extraction_is_not_vacuous():
    blocks = _run_blocks()
    assert len(blocks) == 3, f"expected 3 run blocks, got {len(blocks)}"
    for b in (_agg_block(), _beat_block()):
        assert len(b) > 400, "run block looks truncated"
        assert "curl" in b
    assert "%{http_code}" in _agg_block(), (
        "the aggregate step does not read the HTTP status — a relayed 202 is "
        "indistinguishable from a completed 200")


def test_the_status_stub_really_serves_the_watermark():
    """If the /ingest/status branch of the stub never fired, every 202 test
    below would poll an empty body and agree with whatever the block decided
    for the wrong reason. Prove the two bodies are distinguishable through it,
    and that the block takes the MAX rather than the first ISO."""
    rc, out, _ = _exec(_agg_block(), DELEGATED_202, "202",
                       _status(_T0), _status(_T1))
    assert rc == 0, out
    assert f"pre-run watermark max(last_run)={_T0}" in out, out
    assert f"poll 1/10 max(last_run)={_T1}" in out, out


# --------------------------------------------------------------------------
# The aggregate step. This cron beats the deadman board two ways — its own
# producer beat at the end of the job, and tools/deadman/watch.py off the run
# CONCLUSION (WORKFLOWS["iso-queue-ingest.yml"] = 30) — so a 202 mishandled
# in either direction is publicly visible.
# --------------------------------------------------------------------------

def test_202_with_an_advancing_watermark_is_not_a_failure():
    rc, out, kv = _exec(_agg_block(), DELEGATED_202, "202",
                        _status(_T0), _status(_T1))
    assert rc == 0, f"a delegated ingest that demonstrably ran failed:\n{out}"
    assert "::error::" not in out, out
    assert kv.get("AGG_OUTCOME") == "delegated_ok", kv
    assert "UNKNOWN" in out, "must say the counters are unknown, not zero"


def test_202_does_not_report_the_missing_counters():
    """THE REGRESSION. The 202 body has no isos_fetched/isos_total, and the
    old one-liner rendered every one of them as None on a healthy ingest."""
    rc, out, _ = _exec(_agg_block(), DELEGATED_202, "202",
                       _status(_T0), _status(_T1))
    assert rc == 0, out
    assert "fetched None" not in out, (
        "a still-running delegated ingest reported its counters:\n" + out)
    assert "with_data None" not in out, out


def test_202_that_never_advances_still_fails():
    """The gate must not become a blanket 202-is-fine. An ingest that was
    accepted and then died leaves the watermark where it was."""
    rc, out, kv = _exec(_agg_block(), DELEGATED_202, "202",
                        _status(_T0), _status(_T0))
    assert rc != 0, f"an ingest that never completed passed silently:\n{out}"
    assert "::error::" in out and "never advanced" in out, out
    assert kv.get("AGG_OUTCOME") == "failed", kv


def test_202_with_a_watermark_that_never_appears_still_fails():
    rc, out, kv = _exec(_agg_block(), DELEGATED_202, "202",
                        _status(None), _status(None))
    assert rc != 0, f"a null watermark was accepted as completion:\n{out}"
    assert kv.get("AGG_OUTCOME") == "failed", kv


def test_202_polls_a_bounded_number_of_times():
    """Unbounded polling would hang until the 20-minute job timeout, which
    watch.py reads as a failed conclusion just the same."""
    rc, out, _ = _exec(_agg_block(), DELEGATED_202, "202",
                       _status(_T0), _status(_T0))
    assert rc != 0
    assert "poll 10/10" in out, out
    assert "poll 11/10" not in out, out


def test_no_pre_run_watermark_rejects_a_stale_timestamp():
    """If the pre-run read failed there is no BEFORE to compare against, and
    YESTERDAY's ingested_at would otherwise pass as this run's completion."""
    rc, out, _ = _exec(_agg_block(), DELEGATED_202, "202",
                       _status(None), _status("2020-01-01T00:00:00+00:00"))
    assert rc != 0, f"a pre-existing watermark was accepted as completion:\n{out}"
    assert "stale, not this ingest" in out, out


def test_no_pre_run_watermark_accepts_a_run_stamped_after_the_post():
    """The other half — with no BEFORE, a watermark stamped after the POST is
    genuine completion and must still pass."""
    rc, out, kv = _exec(_agg_block(), DELEGATED_202, "202",
                        _status(None), _status("2099-01-01T00:00:00+00:00"))
    assert rc == 0, f"a genuinely fresh watermark was rejected:\n{out}"
    assert kv.get("AGG_OUTCOME") == "delegated_ok", kv


def test_says_so_when_the_watermark_read_itself_fails():
    """Reading a failed watermark read as "no progress" is the identical
    false-zero one level down. It must still fail (completion was never
    observed) but say the watermark was UNKNOWN, not that the ingest died."""
    rc, out, _ = _exec(_agg_block(), DELEGATED_202, "202",
                       "", "", status_code="503")
    assert rc != 0, out
    assert "watermark UNKNOWN" in out, (
        "a failed watermark read was silently treated as no-progress:\n" + out)


def test_a_real_200_still_reports_its_counters():
    body = ('{"isos_fetched": 10, "isos_total": 10, "isos_with_new_data": 4, '
            '"as_of": "2026-08-19"}')
    rc, out, kv = _exec(_agg_block(), body, "200")
    assert rc == 0, out
    assert "fetched 10 of 10 | with_data 4 | as_of 2026-08-19" in out, out
    assert kv.get("AGG_OUTCOME") == "ok", kv
    assert "::error::" not in out, out


def test_a_real_zero_new_data_run_is_still_reported_as_zero():
    """A genuine 200 that found nothing new is real information and stays
    visible — the fix must not swallow it along with the unknown case."""
    body = ('{"isos_fetched": 10, "isos_total": 10, "isos_with_new_data": 0, '
            '"as_of": "2026-08-19"}')
    rc, out, kv = _exec(_agg_block(), body, "200")
    assert rc == 0, out
    assert "with_data 0" in out, out
    assert kv.get("AGG_OUTCOME") == "ok", kv


def test_207_is_partial_success_not_a_failure():
    """ingest_all() returns `200 if healthy == len(INGESTORS) else 207` — 207
    is the endpoint's own partial-fetch code, not a relay artefact. It ran."""
    body = ('{"isos_fetched": 8, "isos_total": 10, "isos_with_new_data": 3, '
            '"as_of": "2026-08-19"}')
    rc, out, kv = _exec(_agg_block(), body, "207")
    assert rc == 0, f"a 207 partial ingest failed the step:\n{out}"
    assert kv.get("AGG_OUTCOME") == "ok", kv
    assert "::warning::" in out and "207" in out, (
        "a partial ingest passed with no warning at all:\n" + out)


def test_a_non_2xx_fails_loudly():
    for code in ("401", "403", "500", "503"):
        rc, out, kv = _exec(_agg_block(), '{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out
        assert kv.get("AGG_OUTCOME") == "failed", kv


def test_a_dead_origin_fails_loudly():
    rc, out, kv = _exec(_agg_block(), "", "000")
    assert rc != 0, f"a dead origin did not fail the step:\n{out}"
    assert "::error::" in out, out
    assert kv.get("AGG_OUTCOME") == "failed", kv


def test_a_200_in_an_unrecognised_shape_says_so():
    """A 200 whose body we cannot parse is not a zero-ISO ingest."""
    rc, out, kv = _exec(_agg_block(), "<html>gateway</html>", "200")
    assert rc == 0, out
    assert kv.get("AGG_OUTCOME") == "ok", kv
    assert "UNRECOGNISED" in out, out


# --------------------------------------------------------------------------
# The beat. It used to send status="success" HARDCODED with
# rows_inserted=${INSERTED_ROWS:-0} — two assertions this workflow had not
# earned. routes/ingest_runs.py::record_beat reads a MISSING rows_inserted as
# rows_sig=-1 (UNKNOWN: consecutive_zero untouched, stored count COALESCEd),
# which is the correct semantics for the unknown case.
# --------------------------------------------------------------------------

def _payload(out):
    for line in out.splitlines():
        if line.startswith("BEAT_PAYLOAD "):
            return line[len("BEAT_PAYLOAD "):]
    raise AssertionError(f"beat step sent no -d payload:\n{out}")


def test_beat_omits_rows_inserted_when_the_count_is_unknown():
    """THE FALSE ZERO. `:-0` turned "the producing step never set this" into
    "this run inserted nothing" and climbed the consecutive-zero alarm."""
    rc, out, _ = _exec(_beat_block(), AGG_OUTCOME="delegated_ok")
    assert rc == 0, out
    body = _payload(out)
    assert "rows_inserted" not in body, (
        f"an unknown row count was asserted as a number: {body}")
    assert '"status":"success"' in body, body
    assert "UNKNOWN" in out, out


def test_beat_sends_a_real_observed_count():
    rc, out, _ = _exec(_beat_block(), AGG_OUTCOME="ok", INSERTED_ROWS="5328")
    assert rc == 0, out
    assert '"rows_inserted":5328' in _payload(out), _payload(out)


def test_beat_still_sends_an_observed_zero():
    """An OBSERVED zero is real information. Only the UNKNOWN case is omitted;
    softening a genuine zero would mask a broken ingester."""
    rc, out, _ = _exec(_beat_block(), AGG_OUTCOME="ok", INSERTED_ROWS="0")
    assert rc == 0, out
    assert '"rows_inserted":0' in _payload(out), _payload(out)


def test_beat_reports_error_when_the_aggregate_ingest_failed():
    rc, out, _ = _exec(_beat_block(), AGG_OUTCOME="failed",
                       INSERTED_ROWS="5328")
    assert rc == 0, out
    body = _payload(out)
    assert '"status":"error"' in body, (
        f"a failed ingest beat the board green: {body}")


def test_beat_reports_error_when_the_aggregate_step_never_decided():
    """No AGG_OUTCOME means the producing step died before deciding, and that
    is an error, not a quiet success."""
    rc, out, _ = _exec(_beat_block(), INSERTED_ROWS="5328")
    assert rc == 0, out
    assert '"status":"error"' in _payload(out), _payload(out)


def test_beat_warns_when_the_board_refuses_it():
    """Fail-open but NOT silent: a beat that 401s leaves this feed reading
    stale on the board while the job itself is green."""
    rc, out, _ = _exec(_beat_block(), run_code="401", AGG_OUTCOME="ok",
                       INSERTED_ROWS="12")
    assert rc == 0, out
    assert "::warning::" in out and "401" in out, out
