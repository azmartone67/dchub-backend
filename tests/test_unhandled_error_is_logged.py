"""main.py's @app.errorhandler(Exception) must log the errors it swallows.

Registering a handler for Exception replaces Flask's own log_exception. Before
2026-09-21 handle_error only built the JSON body, so /api/v1/deals answered 230
500s in a week with zero log lines; the cause ("'<' not supported between
instances of 'NoneType' and 'int'") was only readable from a response body.

main.py cannot be imported in a unit test, so this compiles the real
handle_error out of it with its decorator and registers it on a bare app.
"""
import ast
import logging
import pathlib

import flask
import pytest

MAIN = pathlib.Path(__file__).resolve().parents[1] / 'main.py'
_MSG = "'<' not supported between instances of 'NoneType' and 'int'"


def _handle_error_module():
    tree = ast.parse(MAIN.read_text())
    hits = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == 'handle_error']
    assert len(hits) == 1, f'expected one top-level handle_error in main.py, found {len(hits)}'
    return ast.Module(body=hits, type_ignores=[])


@pytest.fixture
def client():
    # A module elsewhere in the suite runs logging.disable(CRITICAL) at import
    # time, which would drop the record before caplog could see it.
    was = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    app = flask.Flask('handle-error-test')
    ns = {'app': app, 'jsonify': flask.jsonify, 'request': flask.request,
          'ALLOWED_ORIGINS': set(), 'logger': logging.getLogger('main')}
    exec(compile(_handle_error_module(), str(MAIN), 'exec'), ns)

    @app.route('/boom')
    def boom():
        raise TypeError(_MSG)

    @app.route('/post-only', methods=['POST'])
    def post_only():
        return 'ok'

    yield app.test_client()
    logging.disable(was)


def test_unhandled_error_is_logged_with_its_traceback(client, caplog):
    with caplog.at_level(logging.ERROR):
        resp = client.get('/boom')
    # The caller-facing contract is unchanged.
    assert resp.status_code == 500
    assert resp.get_json() == {'success': False, 'error': _MSG}
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1, [r.getMessage() for r in caplog.records]
    assert '/boom' in errs[0].getMessage()
    assert errs[0].exc_info and isinstance(errs[0].exc_info[1], TypeError)


def test_http_exceptions_stay_quiet(client, caplog):
    with caplog.at_level(logging.ERROR):
        resp = client.get('/post-only')
    assert resp.status_code == 405
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
