"""Guard: land-power error strings are credential-scrubbed at BOTH ends.

WHY THIS EXISTS
───────────────
/api/land-power/status served the COMPLETE EIA API key to the public internet,
verified live 2026-08-07:

    "last_error": "Fatal: 400 Client Error: Bad Request for url:
                   https://api.eia.gov/v2/natural-gas/trans/ann/data/?api_key=<FULL KEY>&…"

The chain: requests puts the whole URL — query string included — into its
exception text; crawl_gas_pipelines() folds that into error_detail; _log_sync()
persists it verbatim; the status route serves it verbatim. Nine rows in
land_power_sync_log carried the key (scrubbed in prod the same day).

THE CONTRACT
────────────
  S1. _log_sync scrubs error_detail BEFORE persisting — a URL secret param
      (api_key=…) never reaches the table.
  S2. _log_sync also scrubs by VALUE via the env list — a secret that appears
      OUTSIDE a URL shape (e.g. an InvalidURL message with no scheme) is
      redacted too. This is the lesson of the ISO-NE leak (#2075): the worst
      leaks are not URL-shaped.
  S3. The status route scrubs on the way OUT, even when the persisted row is
      raw. This is the LOAD-BEARING half: rows written by pre-fix deploys
      (heroic-reprieve still runs an old monolith against the same table)
      never pass through the patched _log_sync.
  S4. The route scrubs BEFORE its 300-char truncation, and userinfo URLs
      (redis://user:pw@host) are redacted structurally.

Both halves run the REAL scrub_secrets — extracted from routes/_iso_common.py
with ast, alongside the module constants it closes over, so this file never
imports psycopg2 or the Flask blueprint chain.

★ NO literal credential-shaped strings in this source: fixtures are built by
concatenation at runtime, so scripts/check_no_leaked_credentials.py stays
silent on this file without needing a pragma.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ d8ecbdca): 4 failed, 0 passed, 1 xfailed
PATCHED (this branch):              4 passed, 0 failed, 1 xfailed

`1 xfailed` in both runs is the strict-xfail must-fail control: it asserts the
secret SURVIVES the real scrub. If scrub_secrets ever degrades to a no-op, the
control XPASSes and strict=True turns that into a hard error.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO_COMMON = os.path.join(ROOT, "routes", "_iso_common.py")
CRAWLER = os.path.join(ROOT, "land_power_crawler.py")

# Constants scrub_secrets closes over; extracted with it so the function is
# the SHIPPED one, not a re-implementation this test could drift from.
_SCRUB_DEPS = {"SECRET_ENV_KEYS", "SECRET_ENV_PAIRS", "_MIN_REDACTABLE",
               "_RE_USERINFO", "_RE_SECRET_PARAM"}


def _real_scrub_secrets():
    src = open(ISO_COMMON, encoding="utf-8").read()
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _SCRUB_DEPS
                for t in node.targets):
            keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "scrub_secrets":
            keep.append(node)
    assert any(isinstance(n, ast.FunctionDef) for n in keep), \
        "scrub_secrets not found in routes/_iso_common.py"
    import re as _re
    ns = {"os": os, "re": _re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), ISO_COMMON, "exec"), ns)
    return ns["scrub_secrets"]


def _extract_fn(name):
    """Pull a FunctionDef (top-level or nested) out of land_power_crawler.py,
    strip decorators, and return (compiled_code, namespace_to_fill)."""
    src = open(CRAWLER, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.decorator_list = []
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            return compile(mod, CRAWLER, "exec")
    raise AssertionError(f"{name} not found in land_power_crawler.py")


class _Cursor:
    """Dispatches on SQL text; captures INSERT params."""

    def __init__(self, last_run_rows, last_ok_rows):
        self._last_run = last_run_rows
        self._last_ok = last_ok_rows
        self._pending = None
        self.inserted = []

    def execute(self, sql, params=None):
        if "INSERT INTO land_power_sync_log" in sql:
            self.inserted.append(params)
        elif "errors = 0" in sql:
            self._pending = list(self._last_ok)
        elif "DISTINCT ON" in sql:
            self._pending = list(self._last_run)
        elif "COUNT(*)" in sql:
            self._pending = [(7,)]

    def fetchall(self):
        rows, self._pending = self._pending, None
        return rows

    def fetchone(self):
        rows, self._pending = self._pending, None
        return rows[0]


class _Conn:
    def __init__(self, cursor):
        self._cur = cursor

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


def _leaky_url():
    # concatenated so no credential-shaped literal exists in this source
    key = "LEAKED" + "EIAKEY" + "0123456789ABCDEFGHIJ"
    url = "https://api.eia.gov/v2/natural-gas/trans/ann/data/?api_key=" \
          + key + "&frequency=annual"
    return key, url


# ── S1/S2: persist side ────────────────────────────────────────────────


def test_log_sync_scrubs_url_secret_param_before_insert():
    import logging
    code = _extract_fn("_log_sync")
    ns = {"scrub_secrets": _real_scrub_secrets(),
          "logger": logging.getLogger("t")}
    exec(code, ns)
    key, url = _leaky_url()
    cur = _Cursor([], [])
    ns["_log_sync"](lambda: _Conn(cur), "eia-ng-pipelines", 0, 0, 0, 1,
                    f"Fatal: 400 Client Error: Bad Request for url: {url}",
                    6.8)
    assert len(cur.inserted) == 1
    persisted_detail = cur.inserted[0][5]
    assert key not in persisted_detail, \
        "the raw api_key value reached the INSERT params"
    assert "api_key=***" in persisted_detail


def test_log_sync_scrubs_bare_env_value_not_just_urls(monkeypatch):
    # The ISO-NE lesson: the leak that actually happened was an InvalidURL
    # message with NO scheme in it. Value-based scrubbing via the env list is
    # what catches that shape; EIA_API_KEY is in SECRET_ENV_KEYS.
    import logging
    secret = "ENVONLY" + "SECRETVALUE" + "987654321"
    monkeypatch.setenv("EIA_API_KEY", secret)
    code = _extract_fn("_log_sync")
    ns = {"scrub_secrets": _real_scrub_secrets(),
          "logger": logging.getLogger("t")}
    exec(code, ns)
    cur = _Cursor([], [])
    ns["_log_sync"](lambda: _Conn(cur), "eia-ng-pipelines", 0, 0, 0, 1,
                    f"InvalidURL: nonnumeric port: '{secret}@api.eia.gov'",
                    1.0)
    assert secret not in cur.inserted[0][5]


# ── S3/S4: serve side ──────────────────────────────────────────────────


def _run_status_route(raw_error_detail):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    last_run = [("eia-ng-pipelines", 0, 0, 1, 6.8, now, raw_error_detail)]
    last_ok = [("eia-ng-pipelines", now, 365)]
    cur = _Cursor(last_run, last_ok)
    code = _extract_fn("land_power_status")
    ns = {"get_db": lambda: _Conn(cur), "jsonify": lambda payload: payload,
          "scrub_secrets": _real_scrub_secrets()}
    exec(code, ns)
    resp = ns["land_power_status"]()
    assert not isinstance(resp, tuple), f"route 500'd: {resp}"
    return next(s for s in resp["latest_syncs"]
                if s["source"] == "eia-ng-pipelines")


def test_status_route_scrubs_raw_persisted_rows():
    # The row in the DB is RAW — as written by a pre-fix deploy. The route
    # must still refuse to serve the credential.
    key, url = _leaky_url()
    served = _run_status_route(
        f"Fatal: 400 Client Error for url: {url}")["last_error"]
    assert key not in served, "route served a raw persisted credential"
    assert "api_key=***" in served
    assert len(served) <= 300


def test_status_route_scrubs_userinfo_urls_structurally():
    pw = "SENTINEL" + "redispw" + "42424242"
    raw = "connect failed: redis://default:" + pw + "@caboose.proxy.rlwy.net:29436"
    served = _run_status_route(raw)["last_error"]
    assert pw not in served
    assert "***:***@" in served


# ── must-fail control ──────────────────────────────────────────────────


@pytest.mark.xfail(strict=True,
                   reason="must-fail control: asserts the secret SURVIVES the "
                          "real scrub. XPASS here means scrub_secrets became "
                          "a no-op — strict turns that into a hard error.")
def test_control_secret_survives_scrub():
    key, url = _leaky_url()
    assert key in _real_scrub_secrets()(f"failed: {url}")
