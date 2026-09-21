"""The watchlist routes answer fixed error bodies on their except paths.

The exception detail goes to the server log (stderr via `_log`), never into
the response. Each route is driven through a real Flask app with the
blueprint registered; the only fault injected is the first step inside the
handler's try block: the /watchlist page's first Response() construction, or
conn.cursor() on the JSON routes. Gates in front of the try (table bootstrap,
IP rate limit, tier lookup) are stubbed; the list route's token is minted
with the module's own _email_token and checked by the real _verify_token.
TESTING is on so a broken except path raises here instead of turning into
Flask's own generic 500 page.
"""
import pytest
from flask import Flask

import routes.watchlist as wl

MARKER = "zq-watchlist-exc-marker"
MESSAGE = f"<b>{MARKER}</b>"
EMAIL = "watcher@example.com"
ENTRY = {"email": EMAIL, "market_slug": "phoenix", "channel": "email"}


def _client():
    app = Flask(__name__)
    app.testing = True
    app.register_blueprint(wl.watchlist_bp)
    return app.test_client()


# ── /watchlist HTML page ──────────────────────────────────────────────
def _fail_first_response(monkeypatch):
    real = wl.Response
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError(MESSAGE)
        return real(*args, **kwargs)

    monkeypatch.setattr(wl, "Response", flaky)
    return calls


def test_the_page_happy_path_serves_the_stub():
    r = _client().get("/watchlist")
    assert r.status_code == 200
    assert "url=/watchlist.html" in r.get_data(as_text=True)


def test_the_page_error_path_answers_a_fixed_body(monkeypatch):
    calls = _fail_first_response(monkeypatch)
    r = _client().get("/watchlist")
    body = r.get_data(as_text=True)
    assert len(calls) == 2, "the handler's except path did not run"
    assert r.status_code == 500
    assert r.mimetype == "text/html"
    assert "watchlist is temporarily unavailable" in body
    assert "<b>" not in body
    assert MARKER not in body, "exception text is echoed into the page"


def test_the_page_error_path_keeps_the_detail_in_the_log(monkeypatch, capsys):
    _fail_first_response(monkeypatch)
    _client().get("/watchlist")
    assert f"[watchlist] page_failed: {MESSAGE}" in capsys.readouterr().err


# ── JSON routes ───────────────────────────────────────────────────────
class _FailingConn:
    def cursor(self):
        raise RuntimeError(MESSAGE)

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def failing_db(monkeypatch):
    monkeypatch.setattr(wl, "_ensure_tables", lambda *a, **k: None)
    monkeypatch.setattr(wl, "_ip_rate_limit_ok", lambda *_: True)
    monkeypatch.setattr(wl, "_lookup_tier", lambda *_: "free")
    monkeypatch.setattr(wl, "_db_conn", _FailingConn)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public")


JSON_ROUTES = [
    # error key, _log key, method, path, request kwargs
    pytest.param("add_failed", "add_failed", "post", "/api/v1/watchlist/add",
                 lambda: {"json": ENTRY}, id="add"),
    pytest.param("remove_failed", "remove_failed", "post", "/api/v1/watchlist/remove",
                 lambda: {"json": ENTRY}, id="remove"),
    pytest.param("list_failed", "list_failed", "get", "/api/v1/watchlist/list",
                 lambda: {"query_string": {"email": EMAIL,
                                           "token": wl._email_token(EMAIL)}},
                 id="list"),
    pytest.param("subscribe_failed", "push_subscribe_failed", "post",
                 "/api/v1/watchlist/push/subscribe",
                 lambda: {"json": {"endpoint": "https://push.example/sub",
                                   "keys": {"p256dh": "k", "auth": "a"}}},
                 id="subscribe"),
]


@pytest.mark.parametrize("error, log_key, method, path, kwargs", JSON_ROUTES)
def test_the_json_error_path_answers_a_fixed_body(failing_db, error, log_key,
                                                  method, path, kwargs):
    r = getattr(_client(), method)(path, **kwargs())
    assert r.status_code == 500
    assert r.get_json()["error"] == error
    assert MARKER not in r.get_data(as_text=True), "exception text is echoed into the response"


@pytest.mark.parametrize("error, log_key, method, path, kwargs", JSON_ROUTES)
def test_the_json_error_path_keeps_the_detail_in_the_log(failing_db, capsys, error,
                                                         log_key, method, path, kwargs):
    getattr(_client(), method)(path, **kwargs())
    assert f"[watchlist] {log_key}: {MESSAGE}" in capsys.readouterr().err
