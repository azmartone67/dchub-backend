"""A credential-less heartbeat must be LOUD and must not pretend to have run.

THE DEFECT (measured live 2026-09-03, dchub_heartbeat.py before this guard):
  HEARTBEAT_SECRET resolves at module IMPORT from DCHUB_ADMIN_KEY /
  DCHUB_INTERNAL_KEY / DCHUB_ADMIN_SECRET, falling back to "". In a process
  with none of them set, the client still POSTed `Authorization: Bearer ` —
  which routes/sources.py::_check_auth reads as NO credential and answers 401,
  byte-identical to a wrong key. heartbeat() swallowed it and returned False
  with no logger in the module at all (0 occurrences of logging/logger/print).

  Consequence: backend-news-facility-extractor (tier p1, 24h cadence) read
  `dead` in the source registry from 2026-08-28, while a heartbeat for it was
  observed 401ing at 2026-09-03T01:23:31Z. A process WAS running; the registry
  could not know. 34 days, dating to #2049 (2026-07-31).

★ WHY THIS ASSERTS "NO REQUEST", NOT "REQUEST WITH A KEY".
  The tempting fix is to widen the credential chain until the POST succeeds.
  That is worse: these fire from atexit hooks registered at IMPORT, so a
  process that merely imported an extractor would start writing SUCCESS rows
  into the freshness registry. An under-report becomes an over-report, and a
  freshness signal that lies upward is undetectable. So the contract is:
  no credential -> no request, one loud warning, return False.

★ Stdlib only. CI installs pytest/requests/flask/pyyaml/psycopg2/psycopg/
  Unidecode/Pillow/feedparser and nothing else (.github/workflows/pre-merge.yml)
  — no pytest-mock, no responses.
"""
import importlib
import logging
import sys
from unittest import mock

import pytest

_ADMIN_ENVS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_ADMIN_SECRET")


@pytest.fixture(autouse=True)
def _restore_real_module():
    """Put the REAL dchub_heartbeat back in sys.modules after every test.

    ★ This is not hygiene, it is a live-side-effect guard, and it was caught by
    running the full suite: ~28 extractor modules register atexit hooks that
    call heartbeat() when the pytest PROCESS exits. Those hooks resolve the
    module from sys.modules at call time. Without this fixture the module left
    behind is whichever reloaded copy the last test made — so the suite exited
    POSTing a TEST credential ("stale-key") at PRODUCTION:

        heartbeat backend-subsea-cable failed: HTTP 401 from
        https://dchub.cloud/api/v1/sources/backend-subsea-cable/heartbeat

    Restoring the original object keeps a test run from reaching into the real
    source registry with fabricated state.
    """
    original = sys.modules.get("dchub_heartbeat")
    try:
        yield
    finally:
        if original is not None:
            sys.modules["dchub_heartbeat"] = original
        else:
            sys.modules.pop("dchub_heartbeat", None)


def _reload_heartbeat(monkeypatch, **env):
    """Re-import the module so its IMPORT-time constants pick up `env`.

    HEARTBEAT_SECRET is a module-level constant; monkeypatching os.environ
    after import would not change it. Reloading is the only way to exercise
    the real resolution path — which is itself the defect's mechanism.
    """
    for k in _ADMIN_ENVS + ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("dchub_heartbeat", None)
    return importlib.import_module("dchub_heartbeat")


def test_no_credential_resolves_to_empty_secret(monkeypatch):
    hb = _reload_heartbeat(monkeypatch)
    assert hb.HEARTBEAT_SECRET == "", (
        "with no admin env set the secret must be empty — if this fails the "
        "module grew a new credential source; make sure it cannot be one that "
        "lets a mere import write a false success into the registry"
    )


def test_no_credential_makes_NO_request_and_warns(monkeypatch, caplog):
    """THE REPRO: the exact state that produced the live 401s."""
    hb = _reload_heartbeat(monkeypatch)

    with mock.patch.object(hb.urllib.request, "urlopen") as urlopen:
        with caplog.at_level(logging.WARNING, logger="dchub_heartbeat"):
            result = hb.heartbeat("backend-news-facility-extractor")

    assert result is False
    # 1. It must not fire a POST that provably cannot be accepted.
    assert urlopen.call_count == 0, (
        "a credential-less heartbeat was sent anyway — it can only 401"
    )
    # 2. It must SAY so. This is the whole point: the old code returned False
    #    in silence, which is why nobody saw 34 days of 401s.
    assert caplog.records, "credential-less heartbeat logged NOTHING"
    msg = caplog.records[0].getMessage()
    for token in _ADMIN_ENVS:
        assert token in msg, f"warning does not name {token}: {msg!r}"
    assert "backend-news-facility-extractor" in msg, (
        "warning does not name the source, so an operator cannot tell which "
        f"source is dark: {msg!r}"
    )


def test_warning_fires_once_per_process_not_per_call(monkeypatch, caplog):
    """~28 importers x atexit would otherwise flood every log line."""
    hb = _reload_heartbeat(monkeypatch)
    with mock.patch.object(hb.urllib.request, "urlopen"):
        with caplog.at_level(logging.WARNING, logger="dchub_heartbeat"):
            for _ in range(5):
                hb.heartbeat("backend-subsea-cable")
    assert len(caplog.records) == 1, (
        f"expected exactly one warning per process, got {len(caplog.records)}"
    )


def test_with_a_credential_it_DOES_attempt_the_post(monkeypatch):
    """The happy path must be untouched — this guard must not mute real runs."""
    hb = _reload_heartbeat(monkeypatch, DCHUB_ADMIN_KEY="test-key-not-a-secret")
    assert hb.HEARTBEAT_SECRET == "test-key-not-a-secret"

    with mock.patch.object(hb.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.status = 200
        assert hb.heartbeat("backend-subsea-cable") is True

    assert urlopen.call_count == 1
    req = urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer test-key-not-a-secret"


@pytest.mark.parametrize("env_var", _ADMIN_ENVS)
def test_any_one_admin_env_is_enough(monkeypatch, env_var):
    hb = _reload_heartbeat(monkeypatch, **{env_var: "k"})
    with mock.patch.object(hb.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.status = 200
        assert hb.heartbeat("backend-subsea-cable") is True
    assert urlopen.call_count == 1


def test_http_error_is_logged_not_swallowed(monkeypatch, caplog):
    """A 401 WITH a credential is a different fault — a wrong/stale key — and
    must also be visible. Previously both paths returned False in silence."""
    import urllib.error
    hb = _reload_heartbeat(monkeypatch, DCHUB_ADMIN_KEY="stale-key")
    err = urllib.error.HTTPError("http://x/hb", 401, "Unauthorized", {}, None)
    with mock.patch.object(hb.urllib.request, "urlopen", side_effect=err):
        with caplog.at_level(logging.WARNING, logger="dchub_heartbeat"):
            assert hb.heartbeat("backend-subsea-cable") is False
    assert caplog.records, "a 401 response was swallowed silently"
    assert "401" in caplog.records[0].getMessage()


# ── the module's load-bearing contract: it must NEVER crash an extractor ──────
# This file ADDS code to the failure paths (a logger call). Logging is not
# free of failure modes — a handler can raise, a %-format can be wrong. These
# pin the contract the docstring promises ("never crashes the caller") against
# the new code, not just the old.

def test_heartbeat_never_raises_even_if_the_logger_explodes(monkeypatch):
    hb = _reload_heartbeat(monkeypatch)
    boom = mock.Mock(side_effect=RuntimeError("logging handler blew up"))
    monkeypatch.setattr(hb._log, "warning", boom)
    # Must not propagate. An extractor's atexit hook raising would turn a
    # successful run into a non-zero exit.
    try:
        result = hb.heartbeat("backend-subsea-cable")
    except Exception as e:                                  # pragma: no cover
        pytest.fail(f"heartbeat raised into the caller: {type(e).__name__}: {e}")
    assert result is False
    assert boom.called, "the mutation did not take — logger was never called"


def test_heartbeat_never_raises_when_the_transport_explodes(monkeypatch):
    hb = _reload_heartbeat(monkeypatch, DCHUB_ADMIN_KEY="k")
    with mock.patch.object(hb.urllib.request, "urlopen",
                           side_effect=RuntimeError("socket imploded")):
        try:
            assert hb.heartbeat("backend-subsea-cable") is False
        except Exception as e:                              # pragma: no cover
            pytest.fail(f"heartbeat raised into the caller: {type(e).__name__}: {e}")
