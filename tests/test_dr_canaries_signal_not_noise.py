"""Both DR canaries were failing on conditions that are not DR faults.

A disaster-recovery alarm that fires on ordinary events is worse than no alarm:
people learn to discount it, and the one time it means something they scroll
past. Both of these were in that state on 2026-08-31.

restore-test — FALSE DATA-LOSS ON A NEW TABLE
    [X] SIGNIFICANT source table absent from restore:
        gsc_daily_performance (~55071 rows)

    That table was created that morning, hours AFTER the dump it was compared
    against. restore_verify computes `missing = [t for t in src if t not in
    restored]` — a LIVE source against an OLDER dump — so any table newer than
    the backup reads as loss, permanently, until the next dump. The backup
    restored 72,942,005 rows perfectly.

failover-canary — FALSE OUTAGE ON ONE UNLUCKY REQUEST
    Green at 01:00, 06:44 and 12:33; red at 18:26, during deploy churn. Each
    hard check was a SINGLE un-retried request against a baseline 5xx rate of
    0.245% (170 of 69,307 requests over 8 hours) that is never zero.

Both fixes are deliberately one-directional: they can only downgrade a case
that is PROVABLY not a fault, and both fail closed when they cannot prove it.
"""

import ast
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIFY = ROOT / "restore_verify.py"
CANARY = ROOT / "dchub-failover-check.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "restore-test.yml"

VTEXT = VERIFY.read_text()
VTREE = ast.parse(VTEXT)
CTEXT = CANARY.read_text()


def _fn(name):
    for n in VTREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in restore_verify.py")


# ── restore-test: newer-than-dump is not data loss ───────────────────

def test_the_probe_fails_closed_on_every_unknown():
    """★ The safety property. This may only downgrade a case it can PROVE is
    newer than the dump. Unknown dump time, no date column, an unreadable
    table, or any row at or before the dump must all keep the hard failure."""
    src = ast.get_source_segment(VTEXT, _fn("table_is_newer_than_dump"))
    assert "if dump_ts is None:\n        return False" in src, \
        "unknown dump time must not forgive an absence"
    assert "if not cols:\n                return False" in src, \
        "a table with no date column cannot be dated — fail closed"
    assert "except Exception:\n        return False" in src, \
        "an unreadable table must not be forgiven"
    assert "return oldest > dump_ts" in src, \
        "strictly newer — a row AT the dump time means it could have been in it"


def test_it_uses_the_oldest_value_across_every_date_column():
    """A table is only provably newer than the dump if its EARLIEST datum is.
    Taking a MAX, or one arbitrary column, would forgive a table that has old
    rows and therefore should have been in the backup."""
    src = ast.get_source_segment(VTEXT, _fn("table_is_newer_than_dump"))
    assert "MIN(" in src and "LEAST(" in src
    assert "MAX(" not in src


def test_naive_timestamps_are_treated_as_utc():
    """A naive datetime compared against an aware one raises TypeError, which
    the except would swallow into a fail-closed — correct, but it would hide
    every `timestamp without time zone` table behind a false hard failure."""
    src = ast.get_source_segment(VTEXT, _fn("table_is_newer_than_dump"))
    assert "tzinfo is None" in src
    assert "timestamp without time zone" in src, "that column type must be probed"


def test_the_downgrade_is_a_warning_that_states_its_reason():
    """A silent downgrade is how a real loss slips through unread."""
    assert "table_is_newer_than_dump(src_conn" in VTEXT
    assert "every row postdates the" in VTEXT
    assert "not lost by it" in VTEXT


def test_source_connection_is_bound_before_use():
    """`sconn` is assigned inside a try inside an `if SOURCE:`. Referencing it
    unguarded would NameError on exactly the run where the source is
    unreachable — turning a soft check into a crash."""
    assert re.search(r"src_conn = None", VTEXT)
    assert VTEXT.index("src_conn = None") < VTEXT.index("table_is_newer_than_dump(src_conn")


def test_the_workflow_hands_over_the_dump_timestamp():
    """The probe is inert without it, and inert means the false failure stays."""
    wf = WORKFLOW.read_text()
    assert "BACKUP_TAKEN_AT.txt" in wf
    assert 'LastModified"].isoformat()' in wf
    assert "export BACKUP_TAKEN_AT=" in wf


def test_a_new_table_is_not_added_to_the_extension_allowlist():
    """The tempting shortcut was to drop gsc_daily_performance into
    NEON_EXT_DEPENDENT. That set means "depends on a Neon-only extension" — an
    ordinary app table does not, and adding it would permanently suppress a
    real signal for that table behind a false label."""
    for node in VTREE.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "NEON_EXT_DEPENDENT" for t in node.targets):
            names = ast.literal_eval(node.value)
            assert "gsc_daily_performance" not in names
            assert "mcp_registry_probe_state" not in names
            return
    raise AssertionError("NEON_EXT_DEPENDENT not found")


# ── failover-canary: one unlucky request is not an outage ────────────

def test_hard_checks_go_through_the_retry():
    assert "hdrs_with_retry" in CTEXT
    assert CTEXT.count("hdrs_with_retry 200") == 2, \
        "both the MCP and API hard checks must retry"
    assert "curl -sS -o /dev/null -D - -X POST \"$MCP_URL\"" not in CTEXT, \
        "the un-retried MCP call is back"


def test_retry_is_bounded_and_overridable():
    assert 'ATTEMPTS="${FAILOVER_CANARY_ATTEMPTS:-3}"' in CTEXT
    assert 'RETRY_SLEEP="${FAILOVER_CANARY_RETRY_SLEEP:-5}"' in CTEXT
    assert "while (( i <= ATTEMPTS ))" in CTEXT


def test_a_sustained_outage_still_fails():
    """★ The property that must survive. Retrying may only forgive a TRANSIENT
    fault — the function returns the last attempt's headers so the caller still
    sees, and reports, a real outage."""
    assert "# all attempts failed — caller reports the real fault" in CTEXT
    assert re.search(r"printf '%s' \"\$out\"\s*#? *# all attempts failed", CTEXT) \
        or "printf '%s' \"$out\"      # all attempts failed" in CTEXT


@pytest.fixture
def local_server():
    """A local HTTP server with a fixed status.

    ★ The first version of this test used httpstat.us. It failed because that
    service returned 502 on the 200 case — a test for "do not trust a single
    remote request" that itself trusted a single remote request. Serve it
    locally: deterministic, no network, and it cannot flake the suite it is
    protecting."""
    import http.server, threading, contextlib

    @contextlib.contextmanager
    def _serve(status):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
            do_POST = do_GET

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield f"http://127.0.0.1:{srv.server_port}/"
        finally:
            srv.shutdown()
            srv.server_close()

    return _serve


@pytest.mark.parametrize("status,want_retries", [(503, 2), (500, 2), (200, 0)])
def test_retry_behaviour_end_to_end(status, want_retries, tmp_path, local_server):
    """Run the REAL bash function. A failing endpoint is attempted ATTEMPTS
    times and still reports the fault; a healthy one exactly once, so the happy
    path gains no latency."""
    fn = subprocess.run(
        ["sed", "-n", "/^hdrs_with_retry() {/,/^}/p", str(CANARY)],
        capture_output=True, text=True, check=True).stdout
    assert "hdrs_with_retry" in fn, "could not extract the function under test"

    with local_server(status) as url:
        script = tmp_path / "t.sh"
        script.write_text(
            "set -uo pipefail\n"
            "http_status() { printf '%s' \"$1\" | head -1 | awk '{print $2}'; }\n"
            "ATTEMPTS=3; RETRY_SLEEP=0\n"
            + fn
            + f"\nout=$(hdrs_with_retry 200 '{url}' --max-time 10)\n"
              "echo \"FINAL=$(http_status \"$out\")\"\n"
        )
        r = subprocess.run(["bash", str(script)], capture_output=True,
                           text=True, timeout=90)

    assert r.stderr.count("retrying in") == want_retries, (
        f"expected {want_retries} retries for {status}, stderr={r.stderr[:200]}")
    assert f"FINAL={status}" in r.stdout, (
        f"the caller must still see the real status, got {r.stdout[:120]}")
