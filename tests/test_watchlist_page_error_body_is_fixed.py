"""The /watchlist fallback page answers a fixed 500 body on its error path.

The exception detail goes to the server log (stderr via `_log`), never into
the HTML. The route is driven through a real Flask app with the blueprint
registered; the only fault injected is the first Response() construction
inside the handler's try block. TESTING is on so a broken except path raises
here instead of turning into Flask's own generic 500 page.
"""
from flask import Flask

import routes.watchlist as wl

MARKER = "zq-watchlist-exc-marker"


def _client():
    app = Flask(__name__)
    app.testing = True
    app.register_blueprint(wl.watchlist_bp)
    return app.test_client()


def _fail_first_response(monkeypatch, message):
    real = wl.Response
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError(message)
        return real(*args, **kwargs)

    monkeypatch.setattr(wl, "Response", flaky)
    return calls


def test_the_happy_path_serves_the_stub():
    r = _client().get("/watchlist")
    assert r.status_code == 200
    assert "url=/watchlist.html" in r.get_data(as_text=True)


def test_the_error_path_answers_a_fixed_body(monkeypatch):
    calls = _fail_first_response(monkeypatch, f"<b>{MARKER}</b>")
    r = _client().get("/watchlist")
    body = r.get_data(as_text=True)
    assert len(calls) == 2, "the handler's except path did not run"
    assert r.status_code == 500
    assert r.mimetype == "text/html"
    assert "watchlist is temporarily unavailable" in body
    assert "<b>" not in body
    assert MARKER not in body, "exception text is echoed into the page"


def test_the_error_path_keeps_the_detail_in_the_log(monkeypatch, capsys):
    _fail_first_response(monkeypatch, f"<b>{MARKER}</b>")
    _client().get("/watchlist")
    assert f"[watchlist] page_failed: <b>{MARKER}</b>" in capsys.readouterr().err
