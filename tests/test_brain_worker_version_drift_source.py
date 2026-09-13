"""check_worker_version_drift reads the worker from the repo that deploys it,
and says WHY when it cannot.

#3871 (2026-09-04) retired the vendored frontend mirror inside dchub-backend.
The detector kept reading dchub-backend/main/dchub-frontend/_worker.js, got a
404 on every scan for eight days, and filed worker_source_unreachable telling
its reader to check whether raw.githubusercontent.com was reachable. It was.

azmartone67/dchub-frontend is private. Measured 2026-09-12 against
raw.githubusercontent.com: a valid token reads _worker.js and _routes.json (200);
no token, an invalid token and a missing file all answer 404. So a 404 alone
cannot say what went wrong — these tests pin the diagnosis that can.
"""
import pytest

pytest.importorskip("flask")

import routes.brain_consistency_radar as R

_WORKER = "/* banner */\nconst WORKER_VERSION = '4.99.0-revert-edge';\n"
_PROBE_OK = {"x-dc-worker-version": "4.99.0-revert-edge"}


def _install(monkeypatch, *, source=(None, "HTTP 404 Not Found"), marker=(None, "HTTP 404 Not Found"),
             probe_headers=_PROBE_OK, token="t0k"):
    """Fake _http_get. Each entry is (body, error-if-body-is-None)."""
    calls = []
    table = {R._WORKER_SOURCE_URL: source, R._WORKER_REPO_MARKER_URL: marker}

    def fake(url, timeout=8):
        calls.append(url)
        if url == R._WORKER_PROBE_URL:
            return ("", probe_headers) if probe_headers is not None else (None, None)
        body, err = table.get(url, (None, "URLError: unmapped in test"))
        if body is None:
            R._LAST_FETCH_ERROR[url] = err
            return None, None
        return body, {}

    monkeypatch.setattr(R, "_http_get", fake)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("BACKEND_PAT", raising=False)
    if token:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    R._LAST_FETCH_ERROR.pop(R._WORKER_SOURCE_URL, None)
    return calls


def _only(findings):
    assert len(findings) == 1, findings
    return findings[0]


# ── where the source is read from ───────────────────────────────────
def test_source_is_read_from_the_repo_that_deploys_the_worker():
    assert "/azmartone67/dchub-frontend/" in R._WORKER_SOURCE_URL
    assert R._WORKER_SOURCE_URL.endswith("/_worker.js")
    assert "/dchub-backend/" not in R._WORKER_SOURCE_URL, \
        "the vendored mirror inside dchub-backend was retired in #3871"


def test_the_marker_lives_in_the_same_repo_and_branch_as_the_source():
    """A marker in a different repo could not tell 'file moved' from
    'token cannot see this repo'."""
    assert R._WORKER_REPO_MARKER_URL.rsplit("/", 1)[0] == R._WORKER_SOURCE_URL.rsplit("/", 1)[0]
    assert R._WORKER_REPO_MARKER_URL != R._WORKER_SOURCE_URL


# ── the comparison itself is unchanged ──────────────────────────────
def test_matching_versions_file_nothing(monkeypatch):
    _install(monkeypatch, source=(_WORKER, None))
    assert R.check_worker_version_drift() == []


def test_a_healthy_scan_spends_exactly_two_fetches(monkeypatch):
    """The diagnosis must cost nothing when nothing is wrong."""
    calls = _install(monkeypatch, source=(_WORKER, None))
    R.check_worker_version_drift()
    assert calls == [R._WORKER_SOURCE_URL, R._WORKER_PROBE_URL], calls


def test_production_older_than_source_is_filed(monkeypatch):
    _install(monkeypatch, source=(_WORKER, None),
             probe_headers={"x-dc-worker-version": "4.98.0-older"})
    f = _only(R.check_worker_version_drift())
    assert f["issue"] == "worker_version_drift"
    assert f["deployed"] == "4.98.0-older"


def test_production_ahead_of_source_is_not_drift(monkeypatch):
    """Prod routinely runs a newer out-of-git worker; flagging it would
    suggest a redeploy that DOWNGRADES production."""
    _install(monkeypatch, source=(_WORKER, None),
             probe_headers={"x-dc-worker-version": "5.0.0-newer"})
    assert R.check_worker_version_drift() == []


def test_a_missing_constant_is_still_reported(monkeypatch):
    _install(monkeypatch, source=("/* no version here */", None))
    assert _only(R.check_worker_version_drift())["issue"] == "worker_version_constant_not_found"


def test_an_empty_body_is_a_fetch_that_worked_not_a_failure(monkeypatch):
    """An empty file is not unreachable — and must not borrow a stale error
    left in _LAST_FETCH_ERROR by an earlier scan."""
    _install(monkeypatch, source=("", None))
    R._LAST_FETCH_ERROR[R._WORKER_SOURCE_URL] = "HTTP 404 Not Found (a previous scan)"
    assert _only(R.check_worker_version_drift())["issue"] == "worker_version_constant_not_found"


def test_a_missing_version_header_is_reported(monkeypatch):
    _install(monkeypatch, source=(_WORKER, None), probe_headers={"content-type": "application/json"})
    assert _only(R.check_worker_version_drift())["issue"] == "worker_version_header_missing"


# ── why the source could not be read ────────────────────────────────
def test_no_token_is_named_and_needs_no_second_fetch(monkeypatch):
    calls = _install(monkeypatch, token=None)
    f = _only(R.check_worker_version_drift())
    assert f["issue"] == "worker_source_unreachable"
    assert f["cause"] == "no_token", f
    assert R._WORKER_REPO_MARKER_URL not in calls


def test_a_readable_repo_with_no_worker_file_says_the_file_moved(monkeypatch):
    _install(monkeypatch, marker=("{}", None))
    f = _only(R.check_worker_version_drift())
    assert f["cause"] == "file_moved", f


def test_an_unreadable_repo_says_the_token_cannot_read_it(monkeypatch):
    _install(monkeypatch)
    f = _only(R.check_worker_version_drift())
    assert f["cause"] == "token_cannot_read_repo", f


def test_a_network_failure_is_not_blamed_on_the_token(monkeypatch):
    calls = _install(monkeypatch, source=(None, "URLError: timed out"))
    f = _only(R.check_worker_version_drift())
    assert f["cause"] == "fetch_error", f
    assert R._WORKER_REPO_MARKER_URL not in calls, "a timeout says nothing about the repo"


@pytest.mark.parametrize("setup", [
    {"token": None},
    {"marker": ("{}", None)},
    {},
])
def test_a_404_no_longer_sends_the_reader_to_check_github_reachability(monkeypatch, setup):
    """The shipped advice was wrong for eight days: GitHub was reachable."""
    _install(monkeypatch, **setup)
    detail = _only(R.check_worker_version_drift())["detail"]
    assert "reachable from the Railway runtime" not in detail, detail


@pytest.mark.parametrize("setup", [
    {"token": None},
    {"marker": ("{}", None)},
    {},
    {"source": (None, "URLError: timed out")},
])
def test_every_cause_keeps_the_same_finding_identity(monkeypatch, setup):
    """Same issue key and url for every cause, so findings already open — and
    spec-debt issue #4089 — stay linked to what this now reports."""
    _install(monkeypatch, **setup)
    f = _only(R.check_worker_version_drift())
    assert f["issue"] == "worker_source_unreachable"
    assert f["url"] == R._WORKER_SOURCE_URL
