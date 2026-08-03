"""build-info: telling "not deployed" from "failed to register" (2026-08-03).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

Shell #50 merged and its endpoint 404'd. A deploy that has not landed and a
blueprint that raised at import look IDENTICAL from outside — both are a bare
404, and main.py's try/except turns the second into a logger warning nobody
reads. The only reason we resolved it was noticing that the 404 handler's
`suggestions` are built from the live url_map. That is detective work, not
diagnostics.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_missing_sha_is_unanswerable_not_current(monkeypatch):
    """★The whole point is answering 'is my merge running'. Defaulting a
    missing SHA to anything would answer it wrongly and confidently."""
    from routes import build_info as b
    for v in b._SHA_VARS:
        monkeypatch.delenv(v, raising=False)
    out = b.running_commit()
    assert out["sha"] is None
    assert "UNANSWERABLE" in out["note"]


def test_the_first_set_var_wins(monkeypatch):
    from routes import build_info as b
    for v in b._SHA_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456")
    out = b.running_commit()
    assert out["sha"] == "abc123def456" and out["short"] == "abc123de"
    assert out["var"] == "RAILWAY_GIT_COMMIT_SHA"


def test_no_shell_out_to_git():
    """★A git command inside the container would report the BUILD image's
    commit, not the running deploy's — a confidently wrong answer to the one
    question this module exists to answer."""
    src = _src("routes", "build_info.py")
    for bad in ("subprocess", "rev-parse", "os.system", "os.popen"):
        assert bad not in src, f"build_info shells out: {bad}"


def test_a_missing_route_is_interpreted_not_just_reported():
    """Siblings present → registration failure (a bug). No siblings → the
    deploy has not landed (patience). The distinction is the deliverable."""
    src = _src("routes", "build_info.py")
    assert "FAILED" in src and "not landed" in src
    assert '"interpretation"' in src


def test_how_to_read_states_both_outcomes():
    src = _src("routes", "build_info.py")
    i = src.index('"how_to_read"')
    window = src[i:i + 700]
    assert "not landed" in window and "permanent" in window


def test_it_touches_no_database():
    src = _src("routes", "build_info.py")
    for bad in ("psycopg2", "SELECT ", "INSERT ", "_conn("):
        assert bad not in src, f"build_info should be DB-free: {bad}"


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.build_info import build_info_bp" in src
    assert "app.register_blueprint(build_info_bp)" in src
