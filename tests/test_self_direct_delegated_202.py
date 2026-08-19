"""Behaviour tests for brain-self-direct.yml's 202 handling, and for the
watermark it polls.

These RUN the workflow's bash against a stubbed curl rather than asserting on
its source text — this bug class is invisible to a grep, which is why #2886
shipped the same defect in news-ner-discovery.yml after it had been fixed
elsewhere.

Measured 2026-08-19. `/api/v1/brain/self-direct/tick` is in main.py's
_WORKER_PROXY_POST_PATHS but NOT _WORKER_PROXY_SYNC_PATHS, so its relay read
budget is 15s, not 180 —
`_read_budget = 180 if request.path in _WORKER_PROXY_SYNC_PATHS else 15`.
The handler was observed at 121.3s on 2026-07-11, so the 202 is the NORMAL
answer here:

    202 {"success": true, "delegated_to": "worker", "completed": false,
         "note": "job still running on dchub-worker; check worker logs"}

That body parses, has no 'ran' key, and fell into the workflow's else-branch as
`NO-OP (skipped_reason=None) — propose-only, no writes`: a still-running
investigation rendered as a healthy no-op, exit 0. tools/deadman/watch.py beats
this feed off the run CONCLUSION (16h cadence), so the public board read green
either way.

Absence of an outcome is UNKNOWN, never a no-op.

★ The watermark had to be built, not just polled. self_direct_tick() writes
ONLY on the fully-investigated path (_store_agenda); disabled / no_api_key /
daily_cap / no_candidate all return without touching the DB — and with
BRAIN_SELF_DIRECT_DAILY_CAP defaulting to 4 against a 6-tick/day cron, those
skips are the normal state. brain_self_agenda MAX(created_at) would therefore
read "never ran" for a healthy capped tick. _record_tick() stamps
brain_state['self_direct_last_tick'] at the END of every tick instead, skips
included — and in the DB, so web and dchub-worker read the same value.
"""
import ast
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github/workflows/brain-self-direct.yml")
MODULE = os.path.join(ROOT, "routes/brain_self_director.py")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow scripts need bash")

DELEGATED_202 = ('{"success": true, "delegated_to": "worker", '
                 '"completed": false, "note": "job still running on '
                 'dchub-worker; check worker logs"}')

_T0 = "2026-08-19T00:17:46.112004+00:00"
_T1 = "2026-08-19T04:19:02.887311+00:00"


def _run_blocks():
    y = open(WORKFLOW, encoding="utf-8").read()
    marker = "        run: |\n"
    out, idx = [], 0
    while True:
        i = y.find(marker, idx)
        if i < 0:
            break
        start = i + len(marker)
        nxt = y.find("\n      - name:", start)
        body = y[start:nxt if nxt > 0 else len(y)]
        lines = [l[10:] if l.startswith(" " * 10) else l
                 for l in body.split("\n")]
        out.append("\n".join(lines))
        idx = start
    return out


def _tick_block():
    for b in _run_blocks():
        if "self-direct/tick" in b:
            return b
    raise AssertionError("self-direct tick run block not found")


def _status(last_tick):
    if not last_tick:
        return '{"ok": true, "last_tick": null, "ran": null}'
    return ('{"ok": true, "last_tick": "%s", "ran": false, '
            '"skipped_reason": "daily_cap"}' % last_tick)


# Stub curl. With -o it writes the body to that file and echoes the status
# code, exactly as real curl does with -o/-w. A URL containing
# /self-direct/status is a watermark read, answered from its OWN env vars: the
# FIRST such call returns $STATUS_BEFORE and every later one $STATUS_AFTER, at
# $STATUS_CODE — so a test picks whether the watermark advances, and can make
# the watermark read fail independently of the tick endpoint. Everything else
# in the block — the branching, the loop, the exits — is real bash.
_MOCK = r'''
curl() {
  local out="" prev="" url="" body="" code="" n=0
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    case "$a" in http*://*) url="$a" ;; esac
    prev="$a"
  done
  case "$url" in
    */self-direct/status*)
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
    script = _tick_block().replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", RUN_BODY=run_body,
                   RUN_CODE=run_code, STATUS_BEFORE=status_before,
                   STATUS_AFTER=status_after, STATUS_CODE=status_code,
                   CURL_STATE=os.path.join(td, "n"),
                   RAILWAY_BASE="https://example.invalid")
        p = subprocess.run(["bash", sh], env=env, capture_output=True,
                           text=True, cwd=td)
        return p.returncode, p.stdout + p.stderr


# --------------------------------------------------------------------------
# Non-vacuity
# --------------------------------------------------------------------------

def test_extraction_is_not_vacuous():
    blocks = _run_blocks()
    assert len(blocks) == 1, f"expected 1 run block, got {len(blocks)}"
    b = _tick_block()
    assert len(b) > 400, "run block looks truncated"
    assert "%{http_code}" in b, "the step does not read the HTTP status"


def test_the_status_stub_really_serves_the_watermark():
    """If the /self-direct/status branch of the stub never fired, every 202
    test below would poll an empty body and agree for the wrong reason."""
    rc, out = _exec(DELEGATED_202, "202", _status(_T0), _status(_T1))
    assert rc == 0, out
    assert f"pre-run last_tick={_T0}" in out, out
    assert f"poll 1/10 last_tick={_T1}" in out, out


# --------------------------------------------------------------------------
# The 202 path
# --------------------------------------------------------------------------

def test_202_with_an_advancing_watermark_is_not_a_no_op():
    """THE REGRESSION. The 202 body has no 'ran', so the old else-branch
    printed `NO-OP (skipped_reason=None)` about a running investigation."""
    rc, out = _exec(DELEGATED_202, "202", _status(_T0), _status(_T1))
    assert rc == 0, f"a delegated tick that demonstrably ran failed:\n{out}"
    assert "::error::" not in out, out
    assert "skipped_reason=None" not in out, (
        "a still-running delegated tick was reported as a no-op:\n" + out)
    assert "UNKNOWN" in out, "must say the outcome is unknown, not a no-op"


def test_202_that_never_advances_still_fails():
    """The gate must not become a blanket 202-is-fine."""
    rc, out = _exec(DELEGATED_202, "202", _status(_T0), _status(_T0))
    assert rc != 0, f"a tick that never completed passed silently:\n{out}"
    assert "::error::" in out and "never advanced" in out, out


def test_202_with_a_watermark_that_never_appears_still_fails():
    rc, out = _exec(DELEGATED_202, "202", _status(None), _status(None))
    assert rc != 0, f"a null watermark was accepted as completion:\n{out}"
    assert "::error::" in out, out


def test_202_polls_a_bounded_number_of_times():
    rc, out = _exec(DELEGATED_202, "202", _status(_T0), _status(_T0))
    assert rc != 0
    assert "poll 10/10" in out, out
    assert "poll 11/10" not in out, out


def test_no_pre_run_watermark_rejects_a_stale_timestamp():
    """With no BEFORE, the PREVIOUS tick's timestamp — up to 4h old — would
    otherwise pass as this tick's completion."""
    rc, out = _exec(DELEGATED_202, "202", _status(None),
                    _status("2020-01-01T00:00:00+00:00"))
    assert rc != 0, f"a pre-existing watermark was accepted:\n{out}"
    assert "stale, not this tick" in out, out


def test_no_pre_run_watermark_accepts_a_tick_stamped_after_the_post():
    rc, out = _exec(DELEGATED_202, "202", _status(None),
                    _status("2099-01-01T00:00:00+00:00"))
    assert rc == 0, f"a genuinely fresh watermark was rejected:\n{out}"
    assert "completed on the worker" in out, out


def test_says_so_when_the_watermark_read_itself_fails():
    """Reading a failed watermark read as "no progress" is the identical
    false-zero one level down."""
    rc, out = _exec(DELEGATED_202, "202", "", "", status_code="503")
    assert rc != 0, out
    assert "watermark UNKNOWN" in out, (
        "a failed watermark read was silently treated as no-progress:\n" + out)


# --------------------------------------------------------------------------
# The 200 paths — all still reported, none swallowed by the fix
# --------------------------------------------------------------------------

def test_a_real_surfaced_tick_still_reports_its_agenda_row():
    body = ('{"ok": true, "ran": true, "agenda_id": 412, "area": "grid", '
            '"kind": "data_coverage", "confidence": 0.72}')
    rc, out = _exec(body, "200")
    assert rc == 0, out
    assert "SURFACED agenda item id=412" in out, out
    assert "::error::" not in out, out


def test_a_genuine_skip_is_still_reported_as_a_no_op():
    """A real skip is real information and stays visible — the fix must not
    swallow it along with the unknown case."""
    for reason in ("disabled", "no_api_key", "daily_cap", "no_candidate"):
        rc, out = _exec('{"ok": true, "ran": false, "skipped_reason": "%s"}'
                        % reason, "200")
        assert rc == 0, out
        assert f"skipped_reason={reason}" in out, out
        assert "::error::" not in out, out


def test_a_200_in_an_unrecognised_shape_is_not_a_no_op():
    """A 200 with neither key is a shape we do not understand. Reporting it as
    a no-op is exactly the conflation that hid the 202 relay."""
    rc, out = _exec('{"ok": true}', "200")
    assert rc == 0, out
    assert "UNRECOGNISED" in out, out
    assert "skipped_reason=None" not in out, out


def test_a_non_200_fails_loudly():
    for code in ("401", "403", "500", "503"):
        rc, out = _exec('{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out


def test_a_dead_origin_fails_loudly():
    rc, out = _exec("", "000")
    assert rc != 0, f"a dead origin did not fail the step:\n{out}"
    assert "::error::" in out, out


# --------------------------------------------------------------------------
# The watermark itself. The poll above is only meaningful if the watermark
# (a) is stamped on EVERY tick and (b) is read from storage both services
# share. Both halves are asserted so neither can be undone quietly.
# --------------------------------------------------------------------------

def _module_ast():
    return ast.parse(open(MODULE, encoding="utf-8").read())


def test_the_watermark_is_stamped_on_every_tick_not_only_on_a_write():
    """self_direct_tick() writes to the DB only on the fully-investigated
    path. If _record_tick moved inside it — or behind an `if result['ran']` —
    a dark/capped/no-candidate tick would stop advancing the watermark and
    brain-self-direct.yml would fail on a perfectly healthy run. It must be
    called unconditionally on the endpoint's result."""
    fn = next(n for n in ast.walk(_module_ast())
              if isinstance(n, ast.FunctionDef)
              and n.name == "self_direct_tick_endpoint")
    calls = [n for n in fn.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
             and isinstance(n.value.func, ast.Name)
             and n.value.func.id == "_record_tick"]
    assert calls, (
        "_record_tick() is not called unconditionally at the top level of "
        "self_direct_tick_endpoint — a skipped tick would leave the watermark "
        "unmoved and fail the cron")


def test_the_watermark_is_db_backed_not_a_module_global():
    """The #2929 trap, asserted rather than assumed. If this watermark ever
    becomes process-local state the way brain_autonomy_loop._LAST_TICK is,
    /self-direct/status starts answering from web's never-written copy while
    the ticks run on dchub-worker — and the poll can never observe an advance.
    A DB read needs no worker-proxy allowlist entry; a module global does."""
    src = open(MODULE, encoding="utf-8").read()
    tree = ast.parse(src)
    for name in ("_record_tick", "_last_tick"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_conn" for n in ast.walk(fn)), (
            f"{name}() no longer opens a DB connection — if the watermark is "
            f"now process-local, /api/v1/brain/self-direct/status must be "
            f"added to main.py's worker-proxy allowlist or the poll in "
            f"brain-self-direct.yml can never see an advance")
        # ★ 2026-08-19 — AST string CONSTANTS, not ast.get_source_segment.
        # That helper returns the raw source INCLUDING COMMENTS, so a
        # substring check against it is satisfied by prose. Demonstrated on
        # this exact test: point the SELECT at some_other_table AND add a
        # comment above it mentioning brain_state, and all 17 tests in this
        # file pass while the watermark reads a table nothing writes.
        # Comments are not AST nodes, so this form cannot be talked into
        # passing. (Same defect was caught by mutation E11 in the data-sync
        # PR and fixed in the deep-dive PR; this is the third instance.)
        lits = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert any("brain_state" in s for s in lits), (
            f"{name}() no longer QUERIES brain_state")
    # A global assignment named like a cached watermark is the thing that
    # would quietly reintroduce the split-brain read.
    globals_ = {t.id for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert "_LAST_TICK" not in globals_, (
        "a module-global _LAST_TICK reintroduces the #2929 split-brain read")


def test_the_status_endpoint_exists_and_is_admin_gated():
    """The workflow polls it with the admin key; an open endpoint would leak
    the brain's tick cadence, and a missing one fails every delegated run."""
    src = open(MODULE, encoding="utf-8").read()
    assert '"/api/v1/brain/self-direct/status"' in src, (
        "the endpoint brain-self-direct.yml polls does not exist")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "self_direct_status")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_admin_ok" for n in ast.walk(fn)), (
        "/self-direct/status is not admin-gated")
