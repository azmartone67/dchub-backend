"""A finding nothing can locate is not a finding.

52 open bug_squash findings had never produced a single code-fix proposal while
consistency_radar produced 48 from 129. Part of the reason was pure
addressability: every row was filed under the scanning runner's absolute path,
so no consumer could tell which repo — let alone which file — to change.

These tests pin the normalisation, and in particular the one way it silently
produced a wrong answer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.brain_bug_squash import _normalise_url  # noqa: E402
from scripts.bug_squash import BACKEND_ROOT, repo_relative  # noqa: E402

# The exact string shape found in production on 2026-09-08.
_CI_FRONTEND = ("/home/runner/work/dchub-backend/dchub-backend/"
                "dchub-frontend/audience/eyeball-card.html#L163")


def test_ci_frontend_path_normalises_to_repo_and_line():
    assert _normalise_url(_CI_FRONTEND) == "dchub-frontend/audience/eyeball-card.html:163"


def test_nested_checkout_does_not_take_the_outer_repo():
    """★ THE BUG THIS TEST EXISTS FOR.

    In CI the frontend is checked out INSIDE the backend workspace, so the path
    contains `dchub-backend` twice before `dchub-frontend`. A non-greedy prefix
    binds the FIRST marker and yields
    `dchub-backend/dchub-backend/dchub-frontend/...` — a path that exists
    nowhere, carrying the WRONG repo, which would route every frontend fix at
    the backend tree. Caught by running the function, not by reading it.
    """
    out = _normalise_url(_CI_FRONTEND)
    assert out.startswith("dchub-frontend/"), out
    assert "dchub-backend" not in out, out
    assert out.count("dchub-frontend") == 1, out


def test_backend_path_still_resolves_to_backend():
    assert _normalise_url("/Users/x/dchub-backend/routes/foo.py#L12") == \
        "dchub-backend/routes/foo.py:12"


def test_already_current_returns_none_not_a_rewrite():
    """None means 'nothing to do'. If a no-op returned a value the migration
    would report rows as rewritten every time it ran, forever."""
    assert _normalise_url("dchub-frontend/index.html:1382") is None


def test_unmatched_shapes_are_left_alone():
    for u in ("", None, "https://dchub.cloud/api/v1/thing",
              "/tmp/scratch/orphan.html#L4"):
        assert _normalise_url(u) is None, u


def test_forward_and_backward_paths_produce_the_same_form():
    """The scanner's forward path (repo_relative, on a live file) and the
    migration's backward path (_normalise_url, on a legacy CI string) must land
    on the same SHAPE, or new findings and migrated ones split into two
    populations that never dedupe against each other.

    ★ They are not symmetric and that is deliberate. `repo_relative` keys on the
    configured ROOTS, so it is correct whatever the checkout directory is
    called — including this worktree, which is not named `dchub-backend`.
    `_normalise_url` keys on the directory NAME, because all it ever sees is a
    stored string with no roots to compare against. That is sufficient: the only
    rows it has to migrate were written by CI, where the checkout is named
    `dchub-backend`. A path from anywhere else is left alone rather than guessed
    at — see test_unmatched_shapes_are_left_alone.
    """
    forward = f"{repo_relative(str(BACKEND_ROOT / 'routes' / 'brain_bug_squash.py'))}:7"
    backward = _normalise_url(
        "/home/runner/work/dchub-backend/dchub-backend/routes/brain_bug_squash.py#L7")
    assert forward == "dchub-backend/routes/brain_bug_squash.py:7"
    assert backward == "dchub-backend/routes/brain_bug_squash.py:7"
    assert forward == backward


def test_repo_relative_never_invents_a_repo():
    """A path under neither root comes back untouched. A wrong repo label is
    worse than an ugly absolute path — it routes a fix at the wrong tree."""
    assert repo_relative("/nowhere/else/file.html") == "/nowhere/else/file.html"
