"""Cover the two parts of scripts/neon_ci_branch.py that can do damage.

`sweep` DELETES branches and `_emit` handles a live DSN in a PUBLIC repo's
logs. Neither has a safe failure mode, so neither ships untested.

The HTTP boundary (`_req`) is stubbed. That is the point of the seam — these
tests cover the DECISIONS the script makes about what to delete and what to
print, not Neon's API, which is exercised for real by the workflow.
"""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

_p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "neon_ci_branch.py"
_spec = importlib.util.spec_from_file_location("neon_ci_branch", _p)
nb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nb)


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class _Api:
    """Records DELETEs so a test can assert on what the sweep chose."""

    def __init__(self, branches):
        self.branches = branches
        self.deleted = []

    def __call__(self, method, path, key, body=None):
        if method == "GET" and path.endswith("/branches"):
            return {"branches": self.branches}
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return {}
        raise AssertionError(f"unexpected {method} {path}")


def _sweep(monkeypatch, branches, ttl_hours=6, prefix="ci-"):
    api = _Api(branches)
    monkeypatch.setattr(nb, "_req", api)
    nb.cmd_sweep(type("A", (), {"api_key": "k", "project_id": "p",
                                "prefix": prefix, "ttl_hours": ttl_hours})())
    return api.deleted


def test_it_sweeps_an_old_ci_branch(monkeypatch):
    deleted = _sweep(monkeypatch, [
        {"id": "br-old", "name": "ci-123-1-parity", "created_at": _ago(9)},
    ])
    assert deleted == ["br-old"]


def test_it_leaves_a_young_ci_branch_alone(monkeypatch):
    """A branch from a run still in flight must survive its own sweeper."""
    deleted = _sweep(monkeypatch, [
        {"id": "br-young", "name": "ci-123-1-parity", "created_at": _ago(1)},
    ])
    assert deleted == []


def test_it_never_touches_a_branch_outside_the_prefix(monkeypatch):
    """The production branch is old by definition. Age alone must not select."""
    deleted = _sweep(monkeypatch, [
        {"id": "br-prod", "name": "production", "created_at": _ago(90 * 24)},
        {"id": "br-dev", "name": "dev-scratch", "created_at": _ago(500)},
    ])
    assert deleted == []


def test_it_refuses_a_default_branch_even_when_the_name_matches(monkeypatch):
    """Belt for the configuration accident: a default branch named ci-*.

    Prefix + age would both select it. Only the explicit default/primary check
    stops the sweeper deleting the production branch.
    """
    deleted = _sweep(monkeypatch, [
        {"id": "br-oops", "name": "ci-legacy", "created_at": _ago(99),
         "default": True},
    ])
    assert deleted == []


def test_an_unparseable_timestamp_is_skipped_not_swept(monkeypatch):
    """Unknown age must mean "leave it", never "it must be old"."""
    deleted = _sweep(monkeypatch, [
        {"id": "br-weird", "name": "ci-x", "created_at": "not-a-date"},
    ])
    assert deleted == []


# ── _emit: the public-log safety property ────────────────────────────────────

def test_the_mask_is_printed_before_the_dsn_is_ever_written(tmp_path, monkeypatch):
    """Ordering here is the whole protection, and ordering is easy to break.

    A DSN written to GITHUB_OUTPUT before ::add-mask:: reaches the runner is in
    a public log permanently.

    ★ This test was VACUOUS in its first form. It asserted on stdout ordering
      alone, so moving the mask to AFTER the GITHUB_OUTPUT write — the exact
      regression it exists to catch — left stdout unchanged and the test green.
      The output write is a FILE write; it never appears on stdout at all. So
      both channels have to land in ONE ordered log, which is what this does.
    """
    import builtins

    out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    dsn = "postgresql://u:sup3rsecret@ep-x.aws.neon.tech/neondb"

    events = []
    real_open = builtins.open

    def recording_open(f, *a, **k):
        if str(f) == str(out):
            events.append("WRITE_OUTPUT")
        return real_open(f, *a, **k)

    monkeypatch.setattr(builtins, "print",
                        lambda *a, **k: events.append(" ".join(str(x) for x in a)))
    monkeypatch.setattr(builtins, "open", recording_open)

    nb._emit(branch_id="br-1", dsn=dsn)

    masked = [i for i, e in enumerate(events) if e.startswith("::add-mask::")]
    written = [i for i, e in enumerate(events) if e == "WRITE_OUTPUT"]
    assert masked, "no ::add-mask:: was emitted at all"
    assert written, "the DSN was never written to GITHUB_OUTPUT"
    assert masked[0] < written[0], (
        f"the DSN reached GITHUB_OUTPUT before the mask: {events}")
    assert f"::add-mask::{dsn}" in events[masked[0]]
    assert "branch_id=br-1" in out.read_text()


def test_the_dsn_is_not_printed_bare_when_github_output_is_set(tmp_path, capsys,
                                                               monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    dsn = "postgresql://u:p@ep-x.aws.neon.tech/neondb"
    nb._emit(branch_id="br-1", dsn=dsn)
    lines = [l for l in capsys.readouterr().out.splitlines()
             if not l.startswith("::add-mask::")]
    assert all(dsn not in l for l in lines), "DSN echoed outside the masked line"


# ── _connection_uri: the fallback that will actually run ─────────────────────

def test_it_prefers_the_uri_the_api_returned(monkeypatch):
    out = {"connection_uris": [{"connection_uri": "postgresql://given"}]}
    assert nb._connection_uri(out, "k", "p", "br", "db", "role") == "postgresql://given"


def test_it_rebuilds_the_uri_when_the_api_omits_one(monkeypatch):
    """dchub's prod branch carries multiple roles/databases, so the API returns
    no connection_uris and THIS is the live path, not the branch above."""
    monkeypatch.setattr(nb, "_req", lambda *a, **k: {"password": "pw"})
    out = {"connection_uris": [], "endpoints": [{"host": "ep-x.aws.neon.tech"}]}
    got = nb._connection_uri(out, "k", "p", "br", "neondb", "neondb_owner")
    assert got == "postgresql://neondb_owner:pw@ep-x.aws.neon.tech/neondb?sslmode=require"


def test_it_refuses_rather_than_returning_a_half_built_uri(monkeypatch):
    monkeypatch.setattr(nb, "_req", lambda *a, **k: {})      # no password
    out = {"connection_uris": [], "endpoints": [{"host": "h"}]}
    with pytest.raises(SystemExit):
        nb._connection_uri(out, "k", "p", "br", "db", "role")
