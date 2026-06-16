"""Security tests for the C1 L22 route_alias_404 auto-merge gate.

This is the only path that permits an AUTOMATED main.py merge, so its safety
invariants are tested explicitly (no network, no DB → CI-safe):

  1. Master OFF switch: with L22_AUTO_MERGE_ENABLE unset, the whole feature is
     inert — _l22_automerge_on() is False and _list_l22_alias_prs() returns []
     (so no L22 PR is ever considered, regardless of the other gates).
  2. The signature matcher accepts ONLY the deterministic L22-auto-alias
     @app.route line that brain_layer22_pr_writer emits, and rejects plain
     routes, non-route lines, and injection attempts.
"""
import os
import importlib


def _mod():
    os.environ.pop("L22_AUTO_MERGE_ENABLE", None)
    import routes.sentinel_auto_merge as S
    return importlib.reload(S)


def test_feature_off_by_default():
    S = _mod()
    assert S._l22_automerge_on() is False
    assert S._list_l22_alias_prs() == []   # inert: no PRs considered when flag off


def test_signature_matcher_accepts_only_real_alias():
    S = _mod()
    rx = S._L22_ALIAS_LINE_RE
    good = [
        "@app.route('/api/v1/foo')  # r57 L22-auto-alias of /api/v1/foos",
        '@app.route("/grid/pjm")  # r60 L22-auto-alias of /grids/pjm',
    ]
    bad = [
        "@app.route('/x')",                                   # no marker
        "import os  # r57 L22-auto-alias of /x",              # not a route
        "@app.route('/x'); os.system('rm -rf /')  # nope",    # injection, no marker
        "@app.route('/x')  # malicious no marker",            # wrong comment
        "  @app.route('/x')  # r1 L22-auto-alias of /y",      # leading indent (must be col 0)
    ]
    for line in good:
        assert rx.match(line), f"should ACCEPT real alias: {line!r}"
    for line in bad:
        assert not rx.match(line), f"should REJECT non-alias: {line!r}"


def test_required_ci_checks_present():
    S = _mod()
    # the green-CI gate must require the real pre-merge-gauntlet jobs
    assert "syntax-check" in S._REQUIRED_CHECKS
    assert "unit-tests" in S._REQUIRED_CHECKS
    assert "regression-lint" in S._REQUIRED_CHECKS
