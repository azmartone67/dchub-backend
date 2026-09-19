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
    dsn = "postgresql://u:sup3rsecret@ep-x.aws.neon.tech/neondb"  # secretscan:allow — synthetic

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
    dsn = "postgresql://u:p@ep-x.aws.neon.tech/neondb"  # secretscan:allow — synthetic
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
    no connection_uris and THIS is the live path, not the branch above.

    The rebuild now DISCOVERS the database and its owner rather than assuming
    neondb/neondb_owner, so the stub answers both calls.
    """
    def _req(method, path, key, body=None):
        if path.endswith("/databases"):
            return {"databases": [{"name": "dchub", "owner_name": "dchub_admin"}]}
        if path.endswith("/reveal_password"):
            assert "/roles/dchub_admin/" in path, (
                "the password must be revealed for the DISCOVERED owner")
            return {"password": "pw"}
        raise AssertionError(f"unexpected call: {path}")

    monkeypatch.setattr(nb, "_req", _req)
    out = {"connection_uris": [], "endpoints": [{"host": "ep-x.aws.neon.tech"}]}
    got = nb._connection_uri(out, "k", "p", "br", "neondb", "neondb_owner")
    assert got == "postgresql://dchub_admin:pw@ep-x.aws.neon.tech/dchub?sslmode=require"


def test_it_refuses_rather_than_returning_a_half_built_uri(monkeypatch):
    def _req(method, path, key, body=None):
        if path.endswith("/databases"):
            return {"databases": [{"name": "db", "owner_name": "role"}]}
        return {}                                            # no password
    monkeypatch.setattr(nb, "_req", _req)
    out = {"connection_uris": [], "endpoints": [{"host": "h"}]}
    with pytest.raises(SystemExit):
        nb._connection_uri(out, "k", "p", "br", "db", "role")


# ── _preflight: turning a bare 404 into something actionable ─────────────────

def _project_lookup(known_id):
    """Model GET /projects/<id> — what _preflight actually calls now."""
    def _req(method, path, key, body=None):
        if path == f"/projects/{known_id}":
            return {"project": {"id": known_id}}
        raise SystemExit(f"neon api GET {path} -> 404: not found")
    return _req


def test_a_project_id_with_a_trailing_newline_is_named_as_malformed(monkeypatch):
    """The likeliest real misconfiguration, and the one Neon reports worst.

    `gh secret set` stores whatever was pasted, newline included; the stray
    character lands in the URL PATH, and Neon answers "this route does not
    exist" — which reads like the API moved rather than like a bad id.
    """
    monkeypatch.setattr(nb, "_req", _project_lookup("winter-frost-12345678"))
    with pytest.raises(SystemExit) as e:
        nb._preflight("k", "winter-frost-12345678\n")
    assert "malformed" in str(e.value)


def test_a_console_url_pasted_instead_of_an_id_is_named_as_malformed(monkeypatch):
    monkeypatch.setattr(nb, "_req", _project_lookup("winter-frost-12345678"))
    with pytest.raises(SystemExit) as e:
        nb._preflight("k", "https://console.neon.tech/app/projects/winter-frost-12345678")
    assert "malformed" in str(e.value)


def test_a_wellformed_id_the_key_cannot_see_is_reported_separately(monkeypatch):
    """Distinct from malformed: the shape is fine, the key is the problem."""
    monkeypatch.setattr(nb, "_req", _project_lookup("other-project-87654321"))
    with pytest.raises(SystemExit) as e:
        nb._preflight("k", "winter-frost-12345678")
    msg = str(e.value)
    assert "malformed" not in msg
    assert "cannot reach it" in msg


def test_a_matching_id_passes(monkeypatch):
    monkeypatch.setattr(nb, "_req", _project_lookup("winter-frost-12345678"))
    nb._preflight("k", "winter-frost-12345678")        # must not raise


def test_preflight_leaks_no_identifier_into_a_public_log(monkeypatch, capsys):
    """CI logs on this repo are PUBLIC. The failure message may describe shape
    and counts; it may not enumerate project ids or echo the configured one."""
    monkeypatch.setattr(nb, "_req", _project_lookup("secret-project-11112222"))
    with pytest.raises(SystemExit) as e:
        nb._preflight("k", "configured-id-99998888")
    blob = str(e.value) + capsys.readouterr().out
    for leaked in ("secret-project-11112222", "other-project-33334444",
                   "configured-id-99998888"):
        assert leaked not in blob, f"{leaked} reached a public log"


def test_main_strips_a_pasted_newline_before_it_reaches_the_url(monkeypatch):
    """`gh secret set` stores the paste verbatim, trailing newline included.

    _preflight would REPORT that, but reporting a paste artefact the script can
    simply absorb is a worse outcome than absorbing it. This covers the absorb;
    _preflight still covers the shapes that cannot be absorbed (a console URL).
    """
    seen = {}
    monkeypatch.setattr(nb, "cmd_create", lambda a: seen.update(
        key=a.api_key, project=a.project_id))
    monkeypatch.setenv("NEON_API_KEY", "  key-with-space \n")
    monkeypatch.setenv("NEON_PROJECT_ID", "winter-frost-12345678\n")
    monkeypatch.setattr("sys.argv", ["neon_ci_branch.py", "create", "--name", "x"])

    nb.main()

    assert seen["project"] == "winter-frost-12345678"
    assert seen["key"] == "key-with-space"


def test_a_connection_string_pasted_as_the_project_id_is_named_as_such(monkeypatch):
    """The mistake that actually happened on the first live run.

    "152 chars containing 7 whitespace/path characters" is accurate and nearly
    useless — it does not tell you WHICH wrong thing you pasted. This case is
    checked before the generic one so the message names it.
    """
    monkeypatch.setattr(nb, "_req", _project_lookup("winter-frost-12345678"))
    with pytest.raises(SystemExit) as e:
        nb._preflight(
            "k",
            "postgresql://neondb_owner:pw@ep-x.aws.neon.tech/neondb?sslmode=require")  # secretscan:allow — synthetic
    msg = str(e.value)
    assert "CONNECTION STRING" in msg
    assert "LIVE DATABASE CREDENTIAL" in msg, "must flag that the value is a credential"
    assert "neondb_owner" not in msg and "pw@" not in msg, "echoed the credential"


def test_a_branch_id_pasted_as_the_project_id_is_named_as_such(monkeypatch):
    """The second wrong paste, and invisible to a shape check.

    `br-winter-resonance-afqm5ih8` is a well-formed 28-character slug with no
    whitespace and no path characters — it is simply the wrong KIND of object,
    so only the 'br-' prefix distinguishes it. Printing it is fine: a branch id
    is an identifier, not a credential.
    """
    monkeypatch.setattr(nb, "_req", _project_lookup("winter-resonance-12345678"))
    with pytest.raises(SystemExit) as e:
        nb._preflight("k", "br-winter-resonance-afqm5ih8")
    msg = str(e.value)
    assert "BRANCH id" in msg
    assert "not among" not in msg, "must not fall through to the generic case"


def test_preflight_survives_an_organisation_scoped_key(monkeypatch):
    """The regression that broke the third live run.

    The first _preflight listed /projects and checked membership. An
    ORG-scoped key cannot answer that at all — Neon returns
    `400 org_id is required` — so the diagnostic failed in front of the
    configuration it existed to diagnose. A direct GET of the project works for
    personal and org keys alike, and it is the narrower question anyway.
    """
    def _req(method, path, key, body=None):
        if path == "/projects":
            raise SystemExit("neon api GET /projects -> 400: org_id is required")
        if path == "/projects/polished-scene-74402045":
            return {"project": {"id": "polished-scene-74402045"}}
        raise AssertionError(f"unexpected call: {path}")

    monkeypatch.setattr(nb, "_req", _req)
    nb._preflight("k", "polished-scene-74402045")      # must not raise


def test_a_403_is_reported_as_authorisation_not_as_a_wrong_id(monkeypatch):
    def _req(method, path, key, body=None):
        raise SystemExit(f"neon api GET {path} -> 403: forbidden")
    monkeypatch.setattr(nb, "_req", _req)
    with pytest.raises(SystemExit) as e:
        nb._preflight("k", "polished-scene-74402045")
    assert "not authorised" in str(e.value)


# ── _resolve_db_and_role: stop guessing neondb / neondb_owner ────────────────

def _branch_dbs(*pairs):
    def _req(method, path, key, body=None):
        if path.endswith("/databases"):
            return {"databases": [{"name": n, "owner_name": o} for n, o in pairs]}
        raise AssertionError(f"unexpected call: {path}")
    return _req


def test_it_pairs_the_database_with_ITS_owner(monkeypatch):
    """Two independent lookups would let a role that does not own the database
    through, and that fails at connect time looking like a bad password."""
    monkeypatch.setattr(nb, "_req", _branch_dbs(("dchub", "dchub_admin"),
                                                ("other", "someone_else")))
    assert nb._resolve_db_and_role("k", "p", "br", "neondb", "neondb_owner") == (
        "dchub", "dchub_admin")


def test_an_explicitly_named_database_that_is_absent_is_an_error(monkeypatch):
    """Silently substituting another database would run the suite against the
    wrong one and report green."""
    monkeypatch.setattr(nb, "_req", _branch_dbs(("dchub", "dchub_admin")))
    with pytest.raises(SystemExit) as e:
        nb._resolve_db_and_role("k", "p", "br", "typo_db", "")
    assert "not on this branch" in str(e.value)
    assert "dchub" in str(e.value), "must list what IS available"


def test_an_explicit_role_overrides_the_owner(monkeypatch):
    monkeypatch.setattr(nb, "_req", _branch_dbs(("dchub", "dchub_admin")))
    assert nb._resolve_db_and_role("k", "p", "br", "dchub", "readonly_role") == (
        "dchub", "readonly_role")


def test_a_branch_with_no_databases_is_an_error_not_an_empty_dsn(monkeypatch):
    monkeypatch.setattr(nb, "_req", lambda *a, **k: {"databases": []})
    with pytest.raises(SystemExit):
        nb._resolve_db_and_role("k", "p", "br", "", "")


# ── _create_branch: degrade the tuning, never the branch ─────────────────────

def test_a_plan_that_refuses_the_tuning_still_gets_a_branch(monkeypatch, capsys):
    """412 "suspend interval is too short for your plan" killed the fourth live
    run. The tuning is an optimisation; the branch is the point."""
    calls = []

    def _req(method, path, key, body=None):
        calls.append(body["endpoints"][0])
        if len(calls) == 1:
            raise SystemExit("neon api POST /x -> 412: suspend interval is too short")
        return {"branch": {"id": "br-new"}}

    monkeypatch.setattr(nb, "_req", _req)
    out = nb._create_branch("k", "p", {"branch": {}, "endpoints": [
        {"type": "read_write", "autoscaling_limit_max_cu": 0.25}]})

    assert out["branch"]["id"] == "br-new"
    assert len(calls) == 2, "should retry exactly once"
    assert calls[1] == {"type": "read_write"}, "retry must drop the tuning"


def test_the_fallback_says_the_branch_is_no_longer_cost_pinned(monkeypatch, capsys):
    """A silent fallback turns a cost regression into something you learn from
    a bill. The warning must name what was lost."""
    state = {"n": 0}

    def _req(method, path, key, body=None):
        state["n"] += 1
        if state["n"] == 1:
            raise SystemExit("neon api POST /x -> 412: suspend interval is too short")
        return {"branch": {"id": "br-new"}}

    monkeypatch.setattr(nb, "_req", _req)
    nb._create_branch("k", "p", {"branch": {}, "endpoints": [{"type": "read_write"}]})
    err = capsys.readouterr().err
    assert "::warning::" in err
    assert "0.25 CU" in err, "must name the pinning that was dropped"


def test_a_non_412_error_is_not_retried(monkeypatch):
    """Retrying a 401 or a 404 just doubles the failure."""
    calls = []

    def _req(method, path, key, body=None):
        calls.append(1)
        raise SystemExit("neon api POST /x -> 401: unauthorised")

    monkeypatch.setattr(nb, "_req", _req)
    with pytest.raises(SystemExit):
        nb._create_branch("k", "p", {"branch": {}, "endpoints": [{}]})
    assert len(calls) == 1, "a non-412 must surface immediately"


def test_a_second_412_surfaces_rather_than_looping(monkeypatch):
    """One retry. A 412 on the untuned body is about something else."""
    calls = []

    def _req(method, path, key, body=None):
        calls.append(1)
        raise SystemExit("neon api POST /x -> 412: something else entirely")

    monkeypatch.setattr(nb, "_req", _req)
    with pytest.raises(SystemExit):
        nb._create_branch("k", "p", {"branch": {}, "endpoints": [{"type": "read_write"}]})
    assert len(calls) == 2, "exactly one retry, then surface"


# ── root-slot admission control ──────────────────────────────────────────────
#
# Added 2026-09-18 after `ROOT_BRANCHES_LIMIT_EXCEEDED` took the lane down with
# FOUR live ci- roots plus production against a 5-root cap. Nothing had leaked:
# `destroy` ran in every run, the sweeper ran and correctly swept 0. What was
# missing was a bound on how many jobs held a branch at the same time.


class _Roots:
    """Serves a branch list that can CHANGE between polls, as the real one does."""

    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def __call__(self, method, path, key, body=None):
        if method == "GET" and path.endswith("/branches"):
            i = min(self.calls, len(self.snapshots) - 1)
            self.calls += 1
            return {"branches": self.snapshots[i]}
        raise AssertionError(f"unexpected {method} {path}")


def _root(name, parent=None, hours=0.1):
    b = {"id": "br-" + name, "name": name, "created_at": _ago(hours)}
    if parent:
        b["parent_id"] = parent
    return b


def _no_sleeping(monkeypatch):
    """Let the loop run at full speed but keep its deadline arithmetic real."""
    clock = {"t": 0.0}
    monkeypatch.setattr(nb.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(nb.time, "monotonic", lambda: clock["t"])
    return clock


def test_a_free_slot_is_taken_without_waiting(monkeypatch):
    _no_sleeping(monkeypatch)
    api = _Roots([_root("ci-1-1-parity"), _root("production")])
    monkeypatch.setattr(nb, "_req", api)
    nb._await_root_slot("k", "p", "ci-", ceiling=3, wait_minutes=8)
    assert api.calls == 1


def test_it_waits_for_a_slot_and_then_proceeds(monkeypatch):
    """The whole point: a full cap must queue, not fail."""
    _no_sleeping(monkeypatch)
    full = [_root(f"ci-{i}-1-parity") for i in range(3)]
    api = _Roots(full, full, full[:2])
    monkeypatch.setattr(nb, "_req", api)
    nb._await_root_slot("k", "p", "ci-", ceiling=3, wait_minutes=8)
    assert api.calls == 3


def test_it_gives_up_with_the_holders_named(monkeypatch, capsys):
    """A timeout must say WHICH branches hold the cap, not just that it is full.

    It reports False rather than raising: whose PR is queued is not a fact about
    this diff, and cmd_create turns the False into an explicit UNMEASURED.
    """
    _no_sleeping(monkeypatch)
    api = _Roots([_root("ci-777-1-parity"), _root("ci-778-1-fullsuite"),
                  _root("ci-779-1-parity")])
    monkeypatch.setattr(nb, "_req", api)
    got = nb._await_root_slot("k", "p", "ci-", ceiling=3, wait_minutes=1)
    assert got is False
    msg = capsys.readouterr().err
    assert "ci-777-1-parity" in msg and "ci-779-1-parity" in msg
    assert "NEON_CI_MAX_ROOTS" in msg


def test_a_child_branch_does_not_consume_a_root_slot(monkeypatch):
    """Only ROOTS are capped. Counting children would queue behind nothing."""
    _no_sleeping(monkeypatch)
    api = _Roots([_root("ci-1-1-parity"),
                  _root("ci-2-1-parity", parent="br-production"),
                  _root("ci-3-1-parity", parent="br-production")])
    monkeypatch.setattr(nb, "_req", api)
    nb._await_root_slot("k", "p", "ci-", ceiling=2, wait_minutes=1)
    assert api.calls == 1


def test_production_does_not_consume_a_ci_slot(monkeypatch):
    """The ceiling counts what this lane owns; prod's root is the headroom."""
    _no_sleeping(monkeypatch)
    api = _Roots([_root("production"), _root("staging"), _root("ci-1-1-parity")])
    monkeypatch.setattr(nb, "_req", api)
    nb._await_root_slot("k", "p", "ci-", ceiling=2, wait_minutes=1)
    assert api.calls == 1


def test_a_zero_ceiling_disables_the_wait_without_an_api_call(monkeypatch):
    api = _Roots([])
    monkeypatch.setattr(nb, "_req", api)
    nb._await_root_slot("k", "p", "ci-", ceiling=0, wait_minutes=1)
    assert api.calls == 0


# ── expires_at is READ BACK, never assumed ───────────────────────────────────


def _create(monkeypatch, capsys, branch):
    monkeypatch.setattr(nb, "_preflight", lambda *a, **k: None)
    # Returns True, like the real one. A stub returning None reads as "no slot"
    # and routes cmd_create into the UNMEASURED branch, which is how this fixture
    # first went wrong when the verdict was introduced.
    monkeypatch.setattr(nb, "_await_root_slot", lambda *a, **k: True)
    monkeypatch.setattr(nb, "_default_branch_id", lambda *a, **k: "br-parent")
    monkeypatch.setattr(nb, "_create_branch", lambda *a, **k: {"branch": branch})
    monkeypatch.setattr(nb, "_connection_uri", lambda *a, **k: "postgres://x/y")
    monkeypatch.setattr(nb, "_emit", lambda **k: None)
    nb.cmd_create(type("A", (), {
        "api_key": "k", "project_id": "p", "parent_id": "", "name": "ci-1-1-parity",
        "database": "neondb", "role": "neondb_owner", "ttl_hours": 3,
        "prefix": "ci-", "max_roots": 3, "wait_minutes": 8})())
    return capsys.readouterr().err


def test_an_echoed_expires_at_is_reported_as_confirmed(monkeypatch, capsys):
    err = _create(monkeypatch, capsys,
                  {"id": "br-x", "expires_at": "2026-09-18T10:00:00Z"})
    assert "expires_at=2026-09-18T10:00:00Z" in err
    assert "::warning::" not in err


def test_a_dropped_expires_at_is_a_loud_warning_not_a_silent_pass(monkeypatch, capsys):
    """Sending the field is not evidence it was stored. Read the response."""
    err = _create(monkeypatch, capsys, {"id": "br-x"})
    assert "::warning::" in err
    assert "backstop is NOT armed" in err


def test_the_cap_422_is_explained_as_capacity_not_as_a_bad_request(monkeypatch):
    def boom(method, path, key, body=None):
        raise SystemExit('neon api POST /projects/p/branches -> 422: '
                         '{"code":"ROOT_BRANCHES_LIMIT_EXCEEDED"}')
    monkeypatch.setattr(nb, "_req", boom)
    with pytest.raises(SystemExit) as exc:
        nb._create_branch("k", "p", {"branch": {}})
    assert "ROOT-branch cap" in str(exc.value)
    assert "NEON_CI_MAX_ROOTS" in str(exc.value)


def test_the_sweeper_prints_the_population_it_examined(monkeypatch, capsys):
    """`swept 0` alone cannot tell "nothing leaked" from "the prefix matched
    nothing". The inventory is what makes a zero readable."""
    _sweep(monkeypatch, [
        {"id": "br-p", "name": "production", "created_at": _ago(900)},
        {"id": "br-y", "name": "ci-1-1-parity", "created_at": _ago(0.2)},
    ])
    err = capsys.readouterr().err
    assert "2 branches, 2 root, 1 matching 'ci-'" in err
    assert "ci-1-1-parity" in err and "expires_at=NOT SET" in err


# ── starvation is UNMEASURED, and UNMEASURED is not a pass ───────────────────
#
# 2026-09-19. The ceiling went 3 -> 4 because supply was below demand, not
# because anything leaked: the holders were live runs aged 3/7/9m with 3h
# expiries, five ci-neon-db runs overlapped in one 30-minute window, and the
# 8m wait was shorter than the 15-23m hold. A fourth job could not ever win.
#
# Raising the ceiling spends the last spare root, so the create can now lose a
# race it previously could not. Both outcomes — lost the wait, lost the race —
# must land on the SAME honest result: exit 0, lane declared UNMEASURED, and
# nothing anywhere claiming the database lane passed.


def _starved(monkeypatch, tmp_path, *, create=None, max_roots=4):
    """cmd_create with no slot available, wired to a real GITHUB_OUTPUT file."""
    out = tmp_path / "gh_out"
    out.write_text("")
    summary = tmp_path / "gh_summary"
    summary.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(nb, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(nb, "_default_branch_id", lambda *a, **k: "br-parent")
    monkeypatch.setattr(nb, "_connection_uri", lambda *a, **k: "postgres://x/y")
    # No slot unless a create stub is supplied, in which case the wait succeeds
    # and the create is what fails.
    monkeypatch.setattr(nb, "_await_root_slot", lambda *a, **k: create is not None)
    if create is not None:
        monkeypatch.setattr(nb, "_create_branch", create)
    nb.cmd_create(type("A", (), {
        "api_key": "k", "project_id": "p", "parent_id": "", "name": "ci-9-1-parity",
        "database": "neondb", "role": "neondb_owner", "ttl_hours": 3,
        "prefix": "ci-", "max_roots": max_roots, "wait_minutes": 8})())
    return out.read_text(), summary.read_text()


def test_a_starved_lane_is_unmeasured_not_a_failure(monkeypatch, tmp_path):
    """Whose PR is queued is not a fact about this diff."""
    gh_out, _ = _starved(monkeypatch, tmp_path)          # must not raise
    assert "slot=none" in gh_out
    assert "dsn=" not in gh_out, "a starved lane must hand out no database"
    assert "branch_id=" not in gh_out


def test_unmeasured_never_claims_the_lane_passed(monkeypatch, tmp_path):
    """★ The whole risk of exiting 0. A green check that is read as 'the DB
    lane passed' is strictly worse than the red it replaced."""
    _, summary = _starved(monkeypatch, tmp_path)
    assert "UNMEASURED" in summary
    assert "did NOT run" in summary
    assert "not a pass" in summary


def test_losing_the_last_root_race_is_unmeasured_too(monkeypatch, tmp_path):
    """Ceiling 4 spends the spare root, so the admission check can be beaten
    between its GET and the POST. Same event, same honest outcome."""
    def cap(*a, **k):
        raise SystemExit('neon api POST /branches -> 422: '
                         '{"code":"ROOT_BRANCHES_LIMIT_EXCEEDED"}')
    gh_out, summary = _starved(monkeypatch, tmp_path, create=cap)
    assert "slot=none" in gh_out and "dsn=" not in gh_out
    assert "UNMEASURED" in summary


def test_a_real_create_failure_still_fails_the_job(monkeypatch, tmp_path):
    """★ The catch must be scoped to the cap. Swallowing every SystemExit here
    would turn a broken API key, a 500, or a malformed body into a quiet green
    — the exact hole this whole change exists to avoid widening."""
    def boom(*a, **k):
        raise SystemExit("neon api POST /branches -> 401: unauthorized")
    with pytest.raises(SystemExit) as exc:
        _starved(monkeypatch, tmp_path, create=boom)
    assert "401" in str(exc.value)


def test_the_happy_path_marks_the_slot_taken(monkeypatch, tmp_path):
    """Both branches must set `slot`, or the gated steps skip themselves on the
    path where the database actually exists."""
    out = tmp_path / "gh_out"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    nb._emit(branch_id="br-x", dsn="postgres://x/y")
    assert "slot=taken" in out.read_text()


def test_the_default_ceiling_leaves_no_root_unspent(monkeypatch):
    """4 is the whole remaining supply under the 5-root Launch cap (production
    holds one). If this ever reads 5+, the plan must have changed with it.

    Driven through main(), not through a re-derived default: the number that
    matters is the one the CLI actually hands cmd_create, and the workflow
    passes neither --max-roots nor NEON_CI_MAX_ROOTS.
    """
    monkeypatch.delenv("NEON_CI_MAX_ROOTS", raising=False)
    seen = {}
    monkeypatch.setattr(nb, "cmd_create", lambda a: seen.update(vars(a)))
    monkeypatch.setenv("NEON_API_KEY", "k")
    monkeypatch.setenv("NEON_PROJECT_ID", "polished-scene-74402045")
    monkeypatch.setattr(nb.sys, "argv",
                        ["neon_ci_branch.py", "create", "--name", "ci-1-1-parity"])
    nb.main()
    assert seen["max_roots"] == 4
    assert seen["wait_minutes"] == 8, (
        "the wait is sized against the 15-23m hold; change it deliberately")
