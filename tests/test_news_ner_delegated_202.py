"""Behaviour tests for news-ner-discovery.yml's beat decision.

These RUN the workflow's bash against a stubbed curl rather than asserting on
its source text — the bug being fixed was invisible to a grep. The source said
"reports this run honestly" while the code read a MISSING counter as zero.

Measured 2026-08-18. `/api/v1/admin/news-ner/run` is in main.py's
_WORKER_PROXY_SYNC_PATHS, so web relays it to dchub-worker with a 180s read
budget. The scan outlives that, and the proxy answers honestly:

    07:02:26 INFO workerproxy: /api/v1/admin/news-ner/run
                  still running on worker after 180s — 202
    {"success": true, "delegated_to": "worker", "completed": false,
     "note": "job still running on dchub-worker; check worker logs"}

That body parses and carries no `articles_scanned`, so
`int(d.get('articles_scanned') or 0)` made it a zero-article scan and the feed
beat `error` with `rows_inserted: 0` — a false zero that also climbs
ingest_runs' consecutive-zero alarm. The scan itself was fine.

Absence of a counter is UNKNOWN, never zero. The delegated path now decides on
observed progress (the /status watermark) and omits rows_inserted.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/news-ner-discovery.yml")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow script needs bash")


def _run_blocks():
    """Both `run: |` bodies, de-indented, secrets templated out."""
    y = open(WORKFLOW, encoding="utf-8").read()
    marker = "        run: |\n"
    out = []
    idx = 0
    while True:
        i = y.find(marker, idx)
        if i < 0:
            break
        start = i + len(marker)
        nxt = y.find("\n      - name:", start)
        body = y[start:nxt if nxt > 0 else len(y)]
        lines = [l[10:] if l.startswith(" " * 10) else l for l in body.split("\n")]
        out.append("\n".join(lines))
        idx = start
    return out


def test_extraction_found_both_blocks():
    """Non-vacuity gate: every scenario below executes one of these strings.
    If extraction broke, they would all run empty scripts and pass."""
    blocks = _run_blocks()
    assert len(blocks) == 2, f"expected 2 run blocks, got {len(blocks)}"
    assert "news-ner/run" in blocks[0], "block 0 is not the ingest step"
    assert "ingest-runs/beat" in blocks[1], "block 1 is not the beat step"
    for b in blocks:
        assert len(b) > 400, "run block looks truncated"


# Stub: /status returns WM_<n> from a counter file; /run writes RUN_BODY to the
# -o target and echoes RUN_CODE. sleep is a no-op so 10 polls cost nothing.
_MOCK = r'''
curl() {
  local out="" ; local prev=""
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    prev="$a"
  done
  case "$*" in
    *news-ner/status*)
      local n; n=$(cat "$CNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$CNT"
      if [ "$n" -ge "$ADVANCE_AT" ]; then echo '{"ok":true,"last_seen":"T-LATER"}'
      else echo '{"ok":true,"last_seen":"T-BASE"}'; fi
      ;;
    *news-ner/run*)
      [ -n "$out" ] && printf '%s' "$RUN_BODY" > "$out"
      printf '%s' "$RUN_CODE"
      ;;
    *) printf '200' ;;
  esac
  return 0
}
sleep() { :; }
'''


def _exec(run_body, run_code, advance_at):
    """Run the ingest block; return (rc, GITHUB_OUTPUT dict, stdout)."""
    script = _run_blocks()[0].replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        gho = os.path.join(td, "out.txt")
        ghs = os.path.join(td, "sum.md")
        cnt = os.path.join(td, "cnt")
        open(gho, "w").close()
        open(ghs, "w").close()
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ,
                   GITHUB_OUTPUT=gho, GITHUB_STEP_SUMMARY=ghs, CNT=cnt,
                   RUN_BODY=run_body, RUN_CODE=run_code,
                   ADVANCE_AT=str(advance_at),
                   ADMIN_KEY="k", DAYS="", DRY="")
        p = subprocess.run(["bash", sh], env=env, capture_output=True, text=True)
        kv = {}
        for line in open(gho):
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.rstrip("\n")
        return p.returncode, kv, p.stdout + p.stderr


_DELEGATED = ('{"success":true,"delegated_to":"worker","completed":false,'
              '"note":"job still running on dchub-worker; check worker logs"}')


def test_delegated_202_with_progress_is_not_an_error():
    """THE REGRESSION. This exact body beat `error` on 2026-08-18."""
    # ADVANCE_AT=2: the pre-run watermark read is call 1, the first poll is 2.
    rc, kv, log = _exec(_DELEGATED, "202", advance_at=2)
    assert rc == 0, log
    assert kv.get("delegated") == "1", kv
    assert kv.get("beat_status") == "success", (
        f"a delegated scan that demonstrably progressed beat "
        f"{kv.get('beat_status')!r} — this is the 08-18 bug: {log[-800:]}")


def test_delegated_202_emits_no_row_count():
    """rows_inserted must be UNKNOWN, not 0 — a false zero climbs the
    consecutive-zero alarm on a healthy feed."""
    rc, kv, log = _exec(_DELEGATED, "202", advance_at=2)
    assert rc == 0, log
    assert kv.get("promoted", "") == "", (
        f"promoted={kv.get('promoted')!r} — the 202 path never saw a count "
        f"and must not assert one")


def test_delegated_202_that_never_progresses_is_an_error():
    """The fix must not become a blanket 'delegated => healthy'."""
    rc, kv, log = _exec(_DELEGATED, "202", advance_at=99)
    assert rc == 0, log
    assert kv.get("beat_status") == "error", (
        f"a worker job that showed no progress in 10 polls beat "
        f"{kv.get('beat_status')!r}")


def test_sync_200_with_promotions_is_success():
    rc, kv, log = _exec('{"articles_scanned":12,"promotion":{"promoted":3}}',
                        "200", advance_at=99)
    assert rc == 0, log
    assert kv.get("beat_status") == "success", kv
    assert kv.get("promoted") == "3", kv


def test_sync_200_scanned_but_nothing_promotable_is_no_new_data():
    """Preserved from 2026-08-10: the promoter refusing junk is healthy."""
    rc, kv, log = _exec('{"articles_scanned":12,"promotion":{"promoted":0}}',
                        "200", advance_at=99)
    assert rc == 0, log
    assert kv.get("beat_status") == "no_new_data", kv
    assert kv.get("promoted") == "0", "a real observed zero must still be sent"


def test_sync_200_zero_articles_is_still_an_error():
    """An OBSERVED zero-article scan is a genuine finding — do not soften it."""
    rc, kv, log = _exec('{"articles_scanned":0}', "200", advance_at=99)
    assert rc == 0, log
    assert kv.get("beat_status") == "error", kv


def test_sync_200_without_the_counter_is_unknown_not_zero():
    """A 200 in a shape we do not understand must not be reported as a
    zero-article scan — that conflation is the whole bug.

    Both end in beat_status=error, so the STATUS cannot discriminate them.
    The annotation is what does: a coercion of the missing key to 0 makes this
    look like an ordinary zero-article run and emits nothing."""
    rc, kv, log = _exec('{"success":true}', "200", advance_at=99)
    assert rc == 0, log
    assert kv.get("beat_status") == "error", kv
    assert kv.get("promoted", "") == "", (
        "no counter came back, so no count may be asserted")
    assert "UNRECOGNISED shape" in log, (
        "a 200 with no articles_scanned was silently treated as a zero-article "
        f"scan — no annotation distinguishes them: {log[-500:]}")


def test_a_real_zero_article_scan_is_not_mislabelled_unrecognised():
    """The mirror of the test above: an OBSERVED 0 must not claim the shape
    was unreadable, or the annotation stops meaning anything."""
    rc, kv, log = _exec('{"articles_scanned":0}', "200", advance_at=99)
    assert rc == 0, log
    assert "UNRECOGNISED shape" not in log, log[-500:]


def _exec_beat(rows, status="success"):
    """Run the beat block with curl stubbed to echo the JSON body it was given."""
    script = _run_blocks()[1].replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    script = script.replace('${{ steps.ingest.outputs.promoted }}', '')
    mock = (
        'curl() { local prev=""; for a in "$@"; do '
        'if [ "$prev" = "-d" ]; then echo "BODY:$a" >&2; fi; prev="$a"; done; '
        'printf "200"; return 0; }\n')
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "b.sh")
        with open(sh, "w") as fh:
            fh.write(mock + script)
        env = dict(os.environ, ADMIN_KEY="k", ROWS=rows, BEAT_STATUS=status)
        p = subprocess.run(["bash", sh], env=env, capture_output=True, text=True)
        body = ""
        for line in (p.stdout + p.stderr).splitlines():
            if line.startswith("BODY:"):
                body = line[5:]
        return p.returncode, body, p.stdout + p.stderr


def test_beat_omits_rows_inserted_when_the_count_is_unknown():
    rc, body, log = _exec_beat(rows="")
    assert rc == 0, log
    assert body, f"no beat body captured: {log[-500:]}"
    assert "rows_inserted" not in body, (
        f"an unknown count was sent as a number: {body}")
    assert '"feed":"news-ner-discovery"' in body and '"cadence_hours":30' in body, (
        f"omitting rows_inserted corrupted the payload: {body}")


def test_beat_sends_rows_inserted_when_the_count_is_known():
    rc, body, log = _exec_beat(rows="4")
    assert rc == 0, log
    assert '"rows_inserted":4' in body, f"observed count was dropped: {body}"


def test_beat_body_is_valid_json_on_both_paths():
    """The omission is string surgery on a JSON literal — prove both shapes
    still parse rather than trusting the commas."""
    import json
    for rows in ("", "4"):
        _, body, _ = _exec_beat(rows=rows)
        json.loads(body)  # raises on a stray or missing comma
