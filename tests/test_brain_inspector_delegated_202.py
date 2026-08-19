"""Behaviour tests for brain-inspector.yml's brief-generate step.

These RUN the workflow's bash against a stubbed curl rather than asserting on
its source text — this bug class is invisible to a grep, which is why #2886
shipped the same defect in news-ner-discovery.yml after it had been fixed
elsewhere.

Measured 2026-08-19. `/api/v1/brain/brief/generate` is in main.py's
_WORKER_PROXY_SYNC_PATHS, so web relays it to dchub-worker with a 180s read
budget and answers honestly when the generate outlives it — the LLM narrative
was observed at ~61s in the 2026-07-11 pool audit, so this is the slow-morning
case:

    202 {"success": true, "delegated_to": "worker", "completed": false,
         "note": "job still running on dchub-worker; check worker logs"}

That body parses, has no 'ok' and no 'id'. Three consequences, all silent:

  · the r-inspector-retry loop read ATTEMPT_OK=0 and burned its ONE retry on a
    generate that was already running — two concurrent Opus calls, exactly what
    L20 durability throttles;
  · BRIEF_ID came out EMPTY;
  · both downstream steps are `if: steps.gen.outputs.brief_id != ''`, so
    "Apply recommendations" and "Draft PRs via L22" did not run.

The job stayed green. This cron does not beat the deadman board directly, which
is why the failure stayed quiet rather than going red — the "quiet class" the
_KNOWN_GAPS entry named.

Absence of a counter is UNKNOWN, never zero.

★ The watermark is /brain/brief/latest `id`. DB-backed
(SELECT ... FROM brain_briefs WHERE error IS NULL), so web and dchub-worker
read the SAME value — no worker-proxy allowlist entry needed, unlike
/api/v1/brain/autonomy/status in #2929, whose module-global watermark had one
never-written copy per service. And brain_inspector deliberately does NOT
persist an empty or errored brief (r33-Q empty-brief-guard), so a moved id is
proof a real, content-bearing brief landed.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/brain-inspector.yml")
MODULE = os.path.join(ROOT, "routes/brain_inspector.py")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow scripts need bash")

DELEGATED_202 = ('{"success": true, "delegated_to": "worker", '
                 '"completed": false, "note": "job still running on '
                 'dchub-worker; check worker logs"}')


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


def _gen_block():
    hits = [b for b in _run_blocks() if "brief/generate" in b]
    assert len(hits) == 1, f"expected 1 generate block, got {len(hits)}"
    return hits[0]


def _latest(brief_id):
    return ('{"ok": true, "id": %d, "generated_at": "2026-08-19T06:07:00Z", '
            '"model": "opus", "summary": "s"}' % brief_id)


# Stub curl. With -o it writes the body to that file and echoes the status
# code, exactly as real curl does with -o/-w. A URL containing /brief/latest is
# a WATERMARK read, answered from its own env vars: the FIRST such call returns
# $STATUS_BEFORE and every later one $STATUS_AFTER, at $STATUS_CODE.
# $URL_LOG records every URL so a test can count how many generates were sent.
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
    */brief/latest*)
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
    """Run the generate block; return (rc, log, $GITHUB_OUTPUT dict, urls)."""
    script = _gen_block().replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        gho = os.path.join(td, "out.txt")
        ghs = os.path.join(td, "sum.md")
        log = os.path.join(td, "urls")
        for f in (gho, ghs, log):
            open(f, "w").close()
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", RUN_BODY=run_body,
                   RUN_CODE=run_code, STATUS_BEFORE=status_before,
                   STATUS_AFTER=status_after, STATUS_CODE=status_code,
                   CURL_STATE=os.path.join(td, "n"), URL_LOG=log,
                   GITHUB_OUTPUT=gho, GITHUB_STEP_SUMMARY=ghs)
        p = subprocess.run(["bash", sh], env=env, capture_output=True,
                           text=True, cwd=td)
        kv = {}
        for line in open(gho):
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.rstrip("\n")
        urls = [l for l in open(log).read().splitlines() if l]
        summary = open(ghs).read()
        return p.returncode, p.stdout + p.stderr + summary, kv, urls


# --------------------------------------------------------------------------
# Non-vacuity
# --------------------------------------------------------------------------

def test_extraction_is_not_vacuous():
    blocks = _run_blocks()
    assert len(blocks) == 4, f"expected 4 run blocks, got {len(blocks)}"
    b = _gen_block()
    assert len(b) > 400, "run block looks truncated"
    assert "%{http_code}" in b, "the step does not read the HTTP status"


def test_the_watermark_stub_really_serves_two_distinguishable_bodies():
    rc, out, _, _ = _exec(DELEGATED_202, "202", _latest(41), _latest(42))
    assert rc == 0, out
    assert "pre-run latest brief id=41" in out, out
    assert "poll 1/10 latest brief id=42" in out, out


# --------------------------------------------------------------------------
# The 202 path
# --------------------------------------------------------------------------

def test_202_with_an_advancing_id_is_not_a_failure():
    rc, out, _, _ = _exec(DELEGATED_202, "202", _latest(41), _latest(42))
    assert rc == 0, f"a delegated generate that demonstrably ran failed:\n{out}"
    assert "::error::" not in out, out


def test_202_publishes_the_new_brief_id_so_the_downstream_steps_run():
    """THE REGRESSION, and the part that actually cost work. Both later steps
    are `if: steps.gen.outputs.brief_id != ''`. On the 202 path brief_id came
    out empty, so applying the Inspector's recommendations and drafting its
    code-fix PRs silently did not happen."""
    rc, out, kv, _ = _exec(DELEGATED_202, "202", _latest(41), _latest(42))
    assert rc == 0, out
    assert kv.get("brief_id") == "42", (
        f"brief_id={kv.get('brief_id')!r} — the apply and draft-PR steps are "
        f"gated on it being non-empty and would silently skip")


def test_202_does_not_invent_counters_it_never_received():
    rc, out, _, _ = _exec(DELEGATED_202, "202", _latest(41), _latest(42))
    assert rc == 0, out
    assert "Tokens: 0" not in out and "Tokens: None" not in out, out
    assert "Duration: 0ms" not in out and "Duration: Nonems" not in out, out
    assert "Delegated to dchub-worker (202)" in out, (
        "the summary must say the counters are absent, not print them:\n" + out)


def test_202_is_not_retried():
    """Retrying a 202 starts a SECOND generate while the first is still
    running on the worker — two concurrent Opus calls, exactly what L20
    durability throttles. The old loop did this on every relayed run."""
    rc, out, _, urls = _exec(DELEGATED_202, "202", _latest(41), _latest(42))
    assert rc == 0, out
    generates = [u for u in urls if "brief/generate" in u]
    assert len(generates) == 1, (
        f"a 202 triggered {len(generates)} generates — the second one races "
        f"the first on the worker")


def test_202_that_never_advances_still_fails():
    """The gate must not become a blanket 202-is-fine."""
    rc, out, kv, _ = _exec(DELEGATED_202, "202", _latest(41), _latest(41))
    assert rc != 0, f"a generate that never landed passed silently:\n{out}"
    assert "::error::" in out and "never advanced" in out, out
    assert kv.get("brief_id", "") == "", (
        "a brief_id was published for a brief that was never written")


def test_202_polls_a_bounded_number_of_times():
    rc, out, _, _ = _exec(DELEGATED_202, "202", _latest(41), _latest(41))
    assert rc != 0
    assert "poll 10/10" in out and "poll 11/10" not in out, out


def test_202_with_an_empty_table_accepts_the_first_brief_ever():
    """404 = no_brief_yet is a real state, not a failed read: an id appearing
    where there was none is this run's brief."""
    rc, out, kv, _ = _exec(DELEGATED_202, "202", "", _latest(1),
                           status_code="404")
    # The pre-run read 404s (empty table); every later read is stubbed 404 too,
    # so this asserts the 404-is-empty branch does not warn or crash.
    assert "::warning::" not in out, (
        "404 no_brief_yet was reported as a failed watermark read:\n" + out)
    assert rc != 0, "an empty table with no new brief must still fail"


def test_says_so_when_the_watermark_read_itself_fails():
    """Reading a failed watermark read as "no brief" is the identical
    false-zero one level down."""
    rc, out, _, _ = _exec(DELEGATED_202, "202", "", "", status_code="503")
    assert rc != 0, out
    assert "watermark UNKNOWN" in out, (
        "a failed watermark read was silently treated as no-progress:\n" + out)


# --------------------------------------------------------------------------
# The synchronous paths — none of them swallowed by the fix
# --------------------------------------------------------------------------

def test_a_real_200_still_publishes_its_brief_id_and_summary():
    body = ('{"ok": true, "id": 77, "model": "opus", "tokens_in": 12000, '
            '"tokens_out": 900, "duration_ms": 61000, "healthy_count": 9, '
            '"degrading_count": 2, "attention_count": 1, "summary": "all fine"}')
    rc, out, kv, _ = _exec(body, "200")
    assert rc == 0, out
    assert kv.get("brief_id") == "77", kv
    assert "Brain Brief #77" in out, out
    assert "::error::" not in out, out


def test_a_200_that_is_not_ok_still_fails():
    """Preserved from 2026-08-12: the diagnostics go to stderr and the step
    exits 1 — except for the one carve-out below."""
    rc, out, _, _ = _exec('{"ok": false, "error": "opus_throttled"}', "200")
    assert rc != 0, f"a not-ok brief passed:\n{out}"
    assert "opus_throttled" in out, out


def test_the_missing_api_key_carve_out_survives():
    """A host with no ANTHROPIC_API_KEY is a configuration state, not a
    regression — it exits 0 on purpose and must keep doing so."""
    rc, out, _, _ = _exec(
        '{"ok": false, "error": "ANTHROPIC_API_KEY not set"}', "200")
    assert rc == 0, f"the ANTHROPIC_API_KEY carve-out was lost:\n{out}"


def test_a_transient_non_200_is_retried_once_then_fails_loudly():
    """The r-inspector-retry behaviour (deploy-churn 502s) is preserved — but
    a run that never recovers now FAILS instead of exiting 0 with an empty
    brief_id and two silently-skipped steps."""
    rc, out, kv, urls = _exec('{"error": "bad gateway"}', "502")
    assert rc != 0, f"a persistent 502 did not fail the step:\n{out}"
    assert "::error::" in out and "502" in out, out
    generates = [u for u in urls if "brief/generate" in u]
    assert len(generates) == 2, (
        f"expected exactly 2 attempts (one retry), got {len(generates)}")
    assert kv.get("brief_id", "") == "", kv


def test_a_dead_origin_fails_loudly():
    rc, out, _, _ = _exec("", "000")
    assert rc != 0, f"a dead origin did not fail the step:\n{out}"
    assert "::error::" in out, out


# --------------------------------------------------------------------------
# The watermark endpoint
# --------------------------------------------------------------------------

def test_the_watermark_endpoint_is_db_backed_and_admin_gated():
    """The poll is only meaningful if /brain/brief/latest is answered from the
    DB — both services read the same brain_briefs table, so it needs no
    worker-proxy allowlist entry. A process-local cache would be the #2929
    trap: web answering from a copy the worker never writes."""
    import ast
    src = open(MODULE, encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "brief_latest")
    # AST string CONSTANTS, not ast.get_source_segment — that helper includes
    # COMMENTS, so a substring check against it can pass on prose alone after
    # the query it describes is gone.
    lits = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("FROM brain_briefs" in s for s in lits), (
        "brief_latest() no longer queries brain_briefs — if the watermark is "
        "now process-local, brain-inspector.yml's poll reads web's copy while "
        "the generate runs on dchub-worker and can never see an advance")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_admin_ok" for n in ast.walk(fn)), (
        "/brain/brief/latest is not admin-gated — it embeds customer emails "
        "and revenue")


def test_an_errored_brief_is_not_a_watermark_advance():
    """r33-Q empty-brief-guard: an empty or errored generate is not persisted,
    and brief_latest filters `WHERE error IS NULL`. Both halves matter — the
    poll treats a moved id as proof a real brief landed, so a junk row that
    could reach that query would make the gate pass on a failed generate."""
    import ast
    src = open(MODULE, encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "brief_latest")
    lits = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("error IS NULL" in s for s in lits), (
        "brief_latest() no longer filters out errored briefs — a junk row "
        "would satisfy brain-inspector.yml's poll")
