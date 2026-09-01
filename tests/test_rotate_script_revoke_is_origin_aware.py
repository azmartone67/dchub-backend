"""`PATCHES/ROTATE_ENTERPRISE_KEY.sh revoke` must be ORIGIN-AWARE.

A DC Hub key authenticates from one of two disjoint tables, and which one
depends on where it was MINTED:

  * api_keys      dashboard/partner/paid keys — tier_gate.resolve_tier step 1b.
  * mcp_dev_keys  MCP-minted keys — resolve_tier step 1a (REST, since #3288 on
                  2026-08-28) and POST /api/v1/keys/validate (the /mcp hop).

Until 2026-09-01 the script's revoke made `api_keys matched 0 rows` its failure
condition, so revoking an MCP-minted key printed "the key was NOT revoked" for a
revoke that had fully succeeded. In a rotation runbook that is the expensive
direction of wrong: the operator either re-runs a rotation that already worked,
or stops believing the tool.

★ WHY THIS FILE EXECUTES THE SCRIPT'S OWN PYTHON INSTEAD OF RESTATING IT.
The revoke logic lives in a heredoc inside a shell script, so it cannot be
imported. Copying it here would create a second implementation that drifts —
which is exactly how dchub-mcp-v2.1/gen_dev_key.py sat six weeks behind the
fix it was supposed to have (see tests/test_revoke_tool_has_one_implementation).
So the body is EXTRACTED from the shipped file and exec'd against a stub
psycopg2. If the extraction anchors ever stop matching, that is a hard failure
here, not a silent skip — an uncollectable guard is worse than no guard.

The stub models ROWS, not answers, and honours whichever live-predicate the
script actually wrote. That is deliberate: narrowing `is_active IS NULL OR
is_active = 1` to `is_active = 1` (or dropping the COALESCE on status) makes the
stub compute a genuinely different verdict, so the predicate tests below fail on
behaviour rather than on source text.
"""

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT   = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "PATCHES" / "ROTATE_ENTERPRISE_KEY.sh"

_OPEN  = "python3 <<'PY' || rc=$?"
_CLOSE = "PY"


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def _revoke_body() -> str:
    """The python heredoc inside the `revoke)` branch, verbatim."""
    assert _SCRIPT.is_file(), f"missing {_SCRIPT}"
    lines = _SCRIPT.read_text().splitlines(keepends=True)
    opens = [i for i, l in enumerate(lines) if l.strip() == _OPEN]
    assert len(opens) == 1, (
        f"expected exactly one {_OPEN!r} heredoc in {_SCRIPT.name}, found "
        f"{len(opens)}. The revoke body could not be located, so NOTHING below "
        f"is testing the shipped code — fix the anchor, do not delete the test."
    )
    start = opens[0]
    close = [i for i, l in enumerate(lines) if i > start and l.strip() == _CLOSE]
    assert close, f"unterminated heredoc after line {start + 1}"
    body = "".join(lines[start + 1:close[0]])
    assert "psycopg2" in body and "sys.exit" in body, (
        "extracted block does not look like the revoke body"
    )
    return body


def test_revoke_body_is_extractable():
    """Guard the guard: everything else here is vacuous if this breaks."""
    body = _revoke_body()
    assert len(body.splitlines()) > 30, "revoke body suspiciously short"


# --------------------------------------------------------------------------
# stub psycopg2 — models rows, honours the script's own predicates
# --------------------------------------------------------------------------
class _Stub:
    def __init__(self, api=(), mcp=(), trial=(), update_is_noop=False):
        # api:   is_active value per matching api_keys row  (None | 0 | 1)
        # mcp:   status value per matching mcp_dev_keys row  (None | str)
        # trial: matching auto_trial_keys rows, or None to simulate NO TABLE
        self.api = list(api)
        self.mcp = list(mcp)
        self.trial = None if trial is None else list(trial)
        self.noop = update_is_noop
        self.sql: list[str] = []
        self.committed = False

    # -- the two "is it live?" predicates, as the SCRIPT wrote them ----------
    def _api_live(self, sql):
        if "is_active IS NULL" in sql:            # NULL-tolerant, matches 1b
            return sum(1 for v in self.api if v is None or v == 1)
        return sum(1 for v in self.api if v == 1)  # narrowed

    def _mcp_live(self, sql):
        if "COALESCE" in sql:                     # NULL-tolerant, matches 1a
            return sum(1 for v in self.mcp if (v or "active") == "active")
        return sum(1 for v in self.mcp if v == "active")  # narrowed

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s)
        if s.startswith("UPDATE api_keys"):
            self.rowcount = len(self.api)
            if not self.noop:
                self.api = [0] * len(self.api)
            return
        if s.startswith("UPDATE mcp_dev_keys"):
            self.rowcount = len(self.mcp)
            if not self.noop:
                self.mcp = ["revoked"] * len(self.mcp)
            return
        if "FROM auto_trial_keys" in s:
            if self.trial is None:
                raise RuntimeError('relation "auto_trial_keys" does not exist')
            self._r = (len(self.trial),)
            return
        if "FROM api_keys" in s:
            self._r = (self._api_live(s) if "is_active" in s else len(self.api),)
            return
        if "FROM mcp_dev_keys" in s:
            live = "status" in s or "COALESCE" in s
            self._r = (self._mcp_live(s) if live else len(self.mcp),)
            return
        raise AssertionError(
            "revoke ran SQL this test does not model, so the verdict below is "
            f"unverified: {s}"
        )

    def fetchone(self):
        return self._r

    # connection surface
    def cursor(self):
        return self
    def commit(self):
        self.committed = True
    def rollback(self):
        pass


def _run(monkeypatch, capsys, **state):
    """exec the shipped revoke body against a stub DB. Returns (exit, json, err)."""
    stub = _Stub(**state)
    fake = types.ModuleType("psycopg2")
    fake.connect = lambda dsn: stub
    monkeypatch.setitem(sys.modules, "psycopg2", fake)   # restored by pytest
    monkeypatch.setenv("TARGET", "dch_live_" + "0" * 32)
    monkeypatch.setenv("DATABASE_URL", "stub://")

    code = 0
    try:
        exec(compile(_revoke_body(), str(_SCRIPT), "exec"), {"__name__": "__main__"})
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    out = capsys.readouterr()
    payload = json.loads(out.out) if out.out.strip() else {}
    return code, payload, out.err, stub


# --------------------------------------------------------------------------
# the verdict matrix
# --------------------------------------------------------------------------
def test_api_keys_homed_key_revokes_clean(monkeypatch, capsys):
    code, j, err, _ = _run(monkeypatch, capsys, api=[1], mcp=[])
    assert code == 0
    assert j["found_in"] == ["api_keys"]
    assert j["still_live_in"] == []
    assert j["revoked"] is True


def test_mcp_homed_key_is_not_reported_as_a_failed_revoke(monkeypatch, capsys):
    """★ THE REGRESSION. An MCP-minted key has no api_keys row by design.

    Setting mcp_dev_keys.status='revoked' is a COMPLETE revoke for that class —
    it closes both resolve_tier 1a and /api/v1/keys/validate. Reporting that as
    a failure is what sent an operator hunting a live credential that was
    already dead.
    """
    code, j, err, _ = _run(monkeypatch, capsys, api=[], mcp=["active"])
    assert code == 0, f"a complete revoke exited {code}; stderr:\n{err}"
    assert j["found_in"] == ["mcp_dev_keys"]
    assert j["still_live_in"] == []
    assert j["revoked"] is True
    assert "NOT REVOKED" not in err and "DID NOT TAKE" not in err


def test_key_present_in_both_tables_revokes_both(monkeypatch, capsys):
    code, j, err, _ = _run(monkeypatch, capsys, api=[1], mcp=["active"])
    assert code == 0
    assert set(j["found_in"]) == {"api_keys", "mcp_dev_keys"}
    assert j["still_live_in"] == []


def test_key_with_no_row_anywhere_exits_nonzero(monkeypatch, capsys):
    """Genuine non-revoke: a typo, or DATABASE_URL pointing at the wrong DB."""
    code, j, err, _ = _run(monkeypatch, capsys, api=[], mcp=[])
    assert code != 0
    assert j["found_in"] == []
    assert j["revoked"] is False
    assert "NOT REVOKED" in err


def test_update_that_does_not_take_exits_nonzero(monkeypatch, capsys):
    """The `is_active = FALSE`-on-an-INTEGER-column class.

    That UPDATE reports a rowcount and changes nothing, so a rowcount-based
    verdict calls it success. Only the post-commit re-read catches it.
    """
    code, j, err, _ = _run(monkeypatch, capsys, api=[1], mcp=[], update_is_noop=True)
    assert code != 0
    assert j["still_live_in"] == ["api_keys"]
    assert j["revoked"] is False
    assert "DID NOT TAKE" in err


def test_already_revoked_key_is_success_not_failure(monkeypatch, capsys):
    code, j, err, _ = _run(monkeypatch, capsys, api=[0], mcp=[])
    assert code == 0
    assert j["already_revoked"] is True
    assert j["still_live_in"] == []


# --------------------------------------------------------------------------
# the third id space
# --------------------------------------------------------------------------
def test_trial_key_is_named_rather_than_blamed(monkeypatch, capsys):
    """dch_trial_ keys live in auto_trial_keys, which this script does not
    manage. Say which table owns it instead of implying the revoke broke."""
    code, j, err, _ = _run(monkeypatch, capsys, api=[], mcp=[], trial=[object()])
    assert code != 0
    assert "auto_trial_keys" in err


def test_missing_auto_trial_table_degrades_soft(monkeypatch, capsys):
    """The auto_trial probe is a courtesy. If that table is absent the revoke
    verdict must still be delivered, not replaced by a traceback."""
    code, j, err, _ = _run(monkeypatch, capsys, api=[], mcp=[], trial=None)
    assert code != 0
    assert "NOT REVOKED" in err


# --------------------------------------------------------------------------
# the live-predicates must keep matching the gates
# --------------------------------------------------------------------------
def test_null_is_active_is_treated_as_live(monkeypatch, capsys):
    """resolve_tier 1b accepts `is_active IS NULL` as live. If revoke narrows
    its check to `is_active = 1`, a row left NULL by a failed UPDATE reads as
    revoked and the script blesses a still-authenticating credential."""
    code, j, err, _ = _run(monkeypatch, capsys, api=[None], mcp=[], update_is_noop=True)
    assert code != 0, "a NULL is_active row is LIVE per resolve_tier 1b"
    assert j["still_live_in"] == ["api_keys"]


def test_null_status_is_treated_as_live(monkeypatch, capsys):
    """resolve_tier 1a matches COALESCE(status,'active')='active', so a NULL
    status authenticates on REST. The looser of the two gates is the safe read."""
    code, j, err, _ = _run(monkeypatch, capsys, api=[], mcp=[None], update_is_noop=True)
    assert code != 0, "a NULL status row is LIVE per resolve_tier 1a"
    assert j["still_live_in"] == ["mcp_dev_keys"]


def test_verdict_is_measured_after_commit_not_inferred(monkeypatch, capsys):
    """Both tables must be censused AFTER the commit, not just before."""
    _, _, _, stub = _run(monkeypatch, capsys, api=[1], mcp=["active"])
    assert stub.committed
    counts = [s for s in stub.sql if s.startswith("SELECT COUNT(*)")]
    upd = max(i for i, s in enumerate(stub.sql) if s.startswith("UPDATE"))
    after = [s for s in stub.sql[upd + 1:] if s.startswith("SELECT COUNT(*)")]
    assert len(counts) >= 8, "expected a 4-query census before AND after"
    assert any("api_keys" in s for s in after), "api_keys not re-read after UPDATE"
    assert any("mcp_dev_keys" in s for s in after), "mcp_dev_keys not re-read"


# --------------------------------------------------------------------------
# shell plumbing — the exit code has to survive the probe
# --------------------------------------------------------------------------
def _script_with_stubbed_python(tmp_path, exit_code):
    """The script with the DB block replaced by a fixed exit, and probe() forced
    to fail.

    probe() is overridden by REDEFINITION rather than by patching its call site:
    a call site that lost its `|| true` would silently escape a text replace and
    send the suite at the live API. Redefining the function stubs it whichever
    way it is called, so `set -e` + a failing probe is genuinely exercised.
    """
    lines = _SCRIPT.read_text().splitlines(keepends=True)
    s = next(i for i, l in enumerate(lines) if l.strip() == _OPEN)
    e = next(i for i, l in enumerate(lines) if i > s and l.strip() == _CLOSE)
    lines[s:e + 1] = [f"    (exit {exit_code}) || rc=$?\n"]

    opens = [i for i, l in enumerate(lines) if l.strip() == "probe() {"]
    assert len(opens) == 1, f"expected one probe() definition, found {len(opens)}"
    close = next(i for i, l in enumerate(lines) if i > opens[0] and l.rstrip() == "}")
    lines.insert(close + 1, 'probe() { echo "  [stub probe: FAILING]"; return 1; }\n')

    p = tmp_path / "rotate.sh"
    p.write_text("".join(lines))
    return p


def _bash(script):
    """Run the stubbed script. bash is resolved to an absolute path: with a
    trimmed env, lookup of the executable itself is platform-dependent, and a
    NoSuchFile here would read as a script bug rather than a test-rig one.
    """
    bash = shutil.which("bash") or "/bin/bash"
    return subprocess.run(
        [bash, str(script), "revoke", "KEY"],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "DATABASE_URL": "stub://"},
        capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("want", [0, 1])
def test_revoke_exit_code_reaches_the_caller(tmp_path, want):
    """`set -e` + a piped probe must not swallow the verdict. The operator's
    automation reads this exit code; a revoke that failed has to be loud."""
    p = _script_with_stubbed_python(tmp_path, want)
    r = _bash(p)
    assert r.returncode == want, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_fallout_guidance_still_prints_on_failure(tmp_path):
    """Exiting non-zero must not cost the operator the follow-up query — the
    evidence is most needed exactly when the revoke is in doubt."""
    p = _script_with_stubbed_python(tmp_path, 1)
    r = _bash(p)
    assert r.returncode == 1
    assert "api_endpoint_log" in r.stdout
