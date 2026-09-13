"""
dchub_heartbeat.py — tiny client for the source-registry heartbeat endpoint.

Usage from any extractor:

    from dchub_heartbeat import heartbeat
    heartbeat("backend-eia-api", status="success", rows_affected=142)

Or as a context manager that auto-times and reports:

    from dchub_heartbeat import tracked_run
    with tracked_run("backend-news-engine") as run:
        rows = do_extraction()
        run.rows_affected = rows
    # heartbeat fires automatically with success/failure on exit

Or as a decorator on a main entry function:

    from dchub_heartbeat import with_heartbeat

    @with_heartbeat("backend-facility-ingestion")
    def main():
        ...
        return rows_processed   # gets used as rows_affected

Best-effort: never crashes the caller. Network errors get swallowed and
logged but the extractor continues.

★ 2026-09-03 — that "logged" claim used to be FALSE: this module had no logger
at all, and every failure (including a 401 from a credential-less process)
returned False in silence. A P1 source read `dead` for 5+ days while a live
process was heartbeating into a 401 nobody could see. Failures are now logged
for real; see _warn_once_no_credential below.
"""

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from functools import wraps


# r78: on Railway the extractors run INSIDE the backend container — POSTing
# telemetry to themselves through the public Cloudflare edge added hundreds
# of edge round-trips/day (and chronic 5xx rows whenever the edge hiccuped).
# Loopback is the same handler minus the internet. Off-Railway callers (local
# dev, any external runner) keep the public URL; env override always wins.
_DEFAULT_HEARTBEAT_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/api/v1/sources"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else "https://dchub.cloud/api/v1/sources"
)
HEARTBEAT_BASE = os.environ.get(
    "DCHUB_HEARTBEAT_BASE",
    _DEFAULT_HEARTBEAT_BASE,
)
# SECURITY (2026-07-31): send a real admin key, never the removed hardcoded
# literal. DCHUB_ADMIN_KEY is set in the Railway container this runs inside and
# is accepted by the /sources heartbeat gate; fall back through the other admin
# envs. If none is set the heartbeat simply 401s — best-effort, never blocks.
HEARTBEAT_SECRET = (
    os.environ.get("DCHUB_ADMIN_KEY")
    or os.environ.get("DCHUB_INTERNAL_KEY")
    or os.environ.get("DCHUB_ADMIN_SECRET")
    or ""
)
HEARTBEAT_TIMEOUT = 5  # seconds — short, never blocks an extractor

_log = logging.getLogger(__name__)


def _safe_warn(fmt, *args):
    """Log a warning that can never propagate.

    This module's contract is "never crashes the caller" — its heartbeats fire
    from inside extractor runs, where a raised exception turns a successful
    run into a failed one. A logging handler CAN raise (bad handler, closed
    stream, %-format mismatch), so every warning here goes through this.
    """
    try:
        _log.warning(fmt, *args)
    except Exception:
        pass

# ★ The silent-401 trap this guard closes (measured 2026-09-03).
# HEARTBEAT_SECRET resolves at IMPORT. In any process where none of the three
# admin envs is set it becomes "", and the client still POSTed
# `Authorization: Bearer ` — which routes/sources.py::_check_auth treats as NO
# credential and answers 401, byte-identical to a wrong key. heartbeat() then
# swallowed that 401 and returned False without a word, so:
#   • the operator saw a source go `dead` with no error anywhere, and
#   • the registry's last_run_at froze at the last CREDENTIALED report.
# Live evidence: backend-news-facility-extractor (tier p1, 24h cadence) read
# `dead` since 2026-08-28 while a heartbeat for it 401'd at 2026-09-03T01:23Z.
# Introduced 2026-07-31 by #2049 (a correct security fix that removed a
# hardcoded literal); the silence is why it survived 34 days.
#
# Two deliberate choices:
#  1. SKIP the POST rather than send one that provably cannot be accepted. A
#     credential-less heartbeat is not a degraded heartbeat, it is a guaranteed
#     401 — sending it only adds edge traffic and a misleading access-log row.
#  2. Do NOT invent a credential (e.g. reading a bare ADMIN_KEY that some CI
#     runners bind). A heartbeat fired from a context that merely IMPORTED an
#     extractor would then write a FALSE success into the source registry —
#     trading an under-report for an over-report, which is strictly worse for a
#     freshness signal. Give the operator the diagnosis and let them decide
#     which contexts are supposed to report.
# ★ A TEST RUN MUST NOT WRITE TO THE PRODUCTION SOURCE REGISTRY.
# Measured 2026-09-03: a full local suite run emitted a real POST to
# https://dchub.cloud/api/v1/sources/... at exit. Ten extractor modules then
# registered atexit hooks at IMPORT, so the hooks fired when pytest exited — on
# a machine holding a real admin key, a genuine "success" for an extractor that
# never ran. Those hooks are gone (2026-09-13: a beat now fires from the run
# itself, see with_heartbeat), but the suite still CALLS those runs with faked
# I/O, and a faked run must not report either.
#
# That is the OVER-REPORT direction, and it is the dangerous one: a source that
# looks stale gets investigated, a source that falsely looks fresh does not.
# `last_success_at` would track "someone ran the test suite", not "the extractor
# ran" — and nothing downstream could tell the difference.
#
# Detection is `pytest in sys.modules` rather than PYTEST_CURRENT_TEST, because
# code that runs at process exit runs AFTER pytest has torn that variable down;
# the module object is still loaded. Escape hatch for tests that need the real
# code path: DCHUB_HEARTBEAT_ALLOW_IN_TESTS=1.
_ALLOW_IN_TESTS = os.environ.get("DCHUB_HEARTBEAT_ALLOW_IN_TESTS") == "1"
_TEST_CONTEXT_LOGGED = False


def _test_context_reason():
    """Name the reason this process must not write to the registry, else None."""
    if _ALLOW_IN_TESTS:
        return None
    if "pytest" in sys.modules:
        return "pytest is loaded in this process"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "PYTEST_CURRENT_TEST is set"
    return None


def _warn_once_test_context(source_id, reason):
    global _TEST_CONTEXT_LOGGED
    if not _TEST_CONTEXT_LOGGED:
        _TEST_CONTEXT_LOGGED = True
        _safe_warn(
            "heartbeat %s SUPPRESSED (test context: %s). A test run must not "
            "write to the production source registry — an extractor that never "
            "ran would be recorded as fresh. Set "
            "DCHUB_HEARTBEAT_ALLOW_IN_TESTS=1 to exercise the real path.",
            source_id, reason,
        )


_MISSING_CREDENTIAL_LOGGED = False


def _warn_once_no_credential(source_id):
    """Say — once per process — that this process can never heartbeat."""
    global _MISSING_CREDENTIAL_LOGGED
    if not _MISSING_CREDENTIAL_LOGGED:
        _MISSING_CREDENTIAL_LOGGED = True
        _safe_warn(
            "heartbeat %s SKIPPED (no credential): none of DCHUB_ADMIN_KEY, "
            "DCHUB_INTERNAL_KEY, DCHUB_ADMIN_SECRET is set in this process, so "
            "the source-registry gate would reject every POST from here with "
            "401. No heartbeat from this process can land; the registry will "
            "show this source going stale even though it ran. Set one of those "
            "envs in whatever launches this process, or stop it heartbeating.",
            source_id,
        )


def heartbeat(source_id, status="success", rows_affected=None,
              duration_ms=None, error=None, metadata=None):
    """Fire a single heartbeat. Returns True on success, False on any failure."""
    if not source_id:
        return False

    body = {"status": status}
    if rows_affected is not None:
        body["rows_affected"] = int(rows_affected)
    if duration_ms is not None:
        body["duration_ms"] = int(duration_ms)
    if error:
        body["error"] = str(error)[:500]
    if metadata:
        body["metadata"] = metadata

    _test_reason = _test_context_reason()
    if _test_reason:
        _warn_once_test_context(source_id, _test_reason)
        return False

    if not HEARTBEAT_SECRET:
        _warn_once_no_credential(source_id)
        return False

    url = f"{HEARTBEAT_BASE}/{source_id}/heartbeat"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {HEARTBEAT_SECRET}",
            "Content-Type": "application/json",
            "User-Agent": "dchub-heartbeat/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # An HTTP status the server chose — the operator needs to see it. A 401
        # here means a credential WAS sent and rejected (a wrong/stale key),
        # which is a different fault from the no-credential skip above.
        _safe_warn("heartbeat %s failed: HTTP %s from %s", source_id,
                   getattr(e, "code", "?"), url)
        return False
    except (urllib.error.URLError, OSError) as e:
        # Transport-level: unreachable, DNS, timeout. Best-effort by design, but
        # no longer invisible.
        _safe_warn("heartbeat %s failed: %s: %s", source_id,
                   type(e).__name__, e)
        return False
    except Exception as e:
        # ★ PRE-EXISTING GAP, surfaced by test_heartbeat_never_raises_when_the
        # _transport_explodes: the old handler caught only URLError/HTTPError/
        # OSError, so ANY other exception out of urlopen (a RuntimeError from a
        # patched opener, an ssl error subclass, a bad Request) propagated into
        # an extractor's atexit hook and could fail an otherwise-good run. The
        # docstring has always promised this never happens; now it is true.
        _safe_warn("heartbeat %s failed: unexpected %s: %s", source_id,
                   type(e).__name__, e)
        return False


def _publishable_error(text, fallback):
    """Error text as it may be shown on the PUBLIC source registry.

    GET /api/v1/sources/<id> serves last_error and every run's error to anyone.
    An exception or step message can carry a credential (userinfo in a DSN, a
    key= parameter, an ISO basic-auth pair), so it goes through the scrubber the
    ISO extractors already publish through. `fallback` holds no message text: it
    is what gets sent when that scrubber cannot be loaded.
    """
    try:
        from routes._iso_common import scrub_secrets
        return scrub_secrets(str(text))
    except Exception:
        return fallback


def _row_count(value):
    """A non-negative int row count, else None. A bool is not a count."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _run_verdict(result, rows_key, use_int_return_as_rows):
    """(status, rows_affected, error) for the value a wrapped run returned.

    ★ The sync entry points in this repo catch their own failures and RETURN a
    summary dict with `success: False` instead of raising. A wrapper that only
    watched for exceptions would report those runs as successes — the same lie
    the import-time atexit beats told: a fresh row for a run that wrote nothing.
    """
    if not isinstance(result, dict):
        rows = _row_count(result) if use_int_return_as_rows else None
        return "success", rows, None
    rows = _row_count(result.get(rows_key)) if rows_key is not None else None
    if result.get("success") is not False:
        return "success", rows, None
    failed_steps = [k for k, v in result.items()
                    if isinstance(v, dict) and v.get("success") is False]
    summary = "success=False" + (
        " in " + ", ".join(str(k) for k in failed_steps) if failed_steps else "")
    errors = result.get("errors")
    if not isinstance(errors, (list, tuple)):
        errors = [errors]
    messages = [str(m) for m in [result.get("error"), *errors] if m]
    messages += ["%s: %s" % (k, result[k]["error"])
                 for k in failed_steps if result[k].get("error")]
    return "failure", rows, _publishable_error(
        "; ".join(messages) or summary, summary)


@contextmanager
def tracked_run(source_id):
    """Context manager: auto-time the block, fire success/failure heartbeat.

    Set ctx.rows_affected = N inside the block to record row count.
    Set ctx.metadata = {...} for richer dashboard data.
    """
    class _Ctx:
        rows_affected = None
        metadata = None
    ctx = _Ctx()
    started = time.time()
    try:
        yield ctx
        elapsed_ms = int((time.time() - started) * 1000)
        heartbeat(
            source_id,
            status="success",
            rows_affected=ctx.rows_affected,
            duration_ms=elapsed_ms,
            metadata=ctx.metadata,
        )
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        heartbeat(
            source_id,
            status="failure",
            duration_ms=elapsed_ms,
            error=_publishable_error(f"{type(e).__name__}: {e}",
                                     type(e).__name__),
        )
        raise


def with_heartbeat(source_id, on_success_use_return_as_rows=True,
                   rows_key=None):
    """Decorator: wrap a function with heartbeat reporting.

    The beat fires when the wrapped function RUNS — never when its module is
    imported or its process exits — so a registry row means a run happened.

    If `on_success_use_return_as_rows=True` and the wrapped function
    returns an int, that int is used as rows_affected. If it returns a dict,
    `rows_key` names the entry holding rows_affected, and `success: False`
    reports the run as a failure carrying the dict's own error text.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            started = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                elapsed_ms = int((time.time() - started) * 1000)
                heartbeat(
                    source_id,
                    status="failure",
                    duration_ms=elapsed_ms,
                    error=_publishable_error(f"{type(e).__name__}: {e}",
                                             type(e).__name__),
                )
                raise
            elapsed_ms = int((time.time() - started) * 1000)
            try:
                status, rows, error = _run_verdict(
                    result, rows_key, on_success_use_return_as_rows)
            except Exception as e:
                # Reporting must never break the run it reports on, and a
                # result it could not read must not be sent as a success.
                _safe_warn("heartbeat %s NOT sent: could not read the run's "
                           "result (%s)", source_id, type(e).__name__)
                return result
            heartbeat(
                source_id,
                status=status,
                rows_affected=rows,
                duration_ms=elapsed_ms,
                error=error,
            )
            return result
        return wrapper
    return deco
