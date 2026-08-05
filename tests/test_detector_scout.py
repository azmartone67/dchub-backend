"""Phase 0 detector-scout tests — NO network, NO DB, NO model, NO flask.

The module imports cleanly without flask (the blueprint is built lazily), so
these tests import it directly and exercise the real filter and the real tick
accounting with an injected `search`.

★EVERY STATEMENT IS INSIDE A FUNCTION. Nothing runs at module scope. A
module-scope exit aborts pytest COLLECTION and kills the whole session — exit 3,
zero tests run, rendered as an ordinary red job. That shipped twice on
2026-07-28 and left the backend with no test gate for hours.
scripts/check_collection_safety.py blocks it in syntax-check; this file must
never need that backstop.

What we assert are the SAFETY INVARIANTS, not the happy path:
  · DISABLED (default) ⇒ the tick performs ZERO search calls and ZERO writes.
    Asserted with a sentinel that raises if touched.
  · dry_run ⇒ ZERO writes even when enabled.
  · The filter rejects on ONE named reason, in a FIXED order, so the reject
    histogram is a real distribution.
  · A copyleft / unlicensed / archived / stale / off-language repo NEVER
    survives.
  · A search failure degrades to a reported no-op, never an exception.

Run:  python3 -m pytest tests/test_detector_scout.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.detector_scout as ds  # noqa: E402 (imports without flask)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test with the scout OFF and no token."""
    monkeypatch.delenv("DETECTOR_SCOUT_ENABLED", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield


def _repo(**over):
    """A repo that PASSES every filter rule, so each test can break exactly one."""
    base = {
        "full_name": "acme/py-codemod",
        "html_url": "https://github.com/acme/py-codemod",
        "description": "A libcst codemod collection",
        "stars": 400,
        "language": "Python",
        "licence": "mit",
        "pushed_at": _recent_iso(),
        "archived": False,
        "topics": ["codemod"],
    }
    base.update(over)
    return base


def _recent_iso(days_ago=3):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _explode(*a, **k):
    raise AssertionError("network/DB touched in a path that must not touch it")


# ── The OFF gate ─────────────────────────────────────────────────────
def test_disabled_by_default_does_no_search_and_no_write(monkeypatch):
    monkeypatch.setattr(ds, "persist", _explode)
    out = ds.scout_tick(dry_run=False, search=_explode)
    assert out["skipped"] == "disabled"
    assert out["enabled"] is False


def test_enabled_requires_exactly_the_string_1(monkeypatch):
    for val in ("0", "", "true", "yes", "TRUE"):
        monkeypatch.setenv("DETECTOR_SCOUT_ENABLED", val)
        assert ds._enabled() is False, f"{val!r} must not enable the scout"
    monkeypatch.setenv("DETECTOR_SCOUT_ENABLED", "1")
    assert ds._enabled() is True


def test_dry_run_never_writes_even_when_enabled(monkeypatch):
    monkeypatch.setenv("DETECTOR_SCOUT_ENABLED", "1")
    monkeypatch.setattr(ds, "persist", _explode)
    out = ds.scout_tick(dry_run=True, search=lambda q, **k: ([_gh_item()], ""))
    assert out["dry_run"] is True
    assert out["written"] == 0


# ── The filter ───────────────────────────────────────────────────────
def test_clean_repo_survives():
    keep, reason = ds.filter_repo(_repo())
    assert keep is True
    assert reason == "keep"


@pytest.mark.parametrize("over,expected", [
    ({"archived": True}, "archived"),
    ({"licence": None}, "licence:none"),
    ({"licence": "gpl-3.0"}, "licence:gpl-3.0"),
    ({"licence": "agpl-3.0"}, "licence:agpl-3.0"),
    ({"language": "TypeScript"}, "language:typescript"),
    ({"language": ""}, "language:none"),
    ({"stars": 3}, "too_few_stars"),
    ({"full_name": ""}, "no_full_name"),
    ({"pushed_at": ""}, "stale:unknown_pushed_at"),
    ({"pushed_at": "not-a-date"}, "stale:unknown_pushed_at"),
    ({"description": "a css framework", "topics": [],
      "full_name": "acme/css"}, "no_corpus_signal"),
])
def test_filter_rejects_with_one_named_reason(over, expected):
    keep, reason = ds.filter_repo(_repo(**over))
    assert keep is False
    assert reason == expected


def test_stale_repo_rejected():
    keep, reason = ds.filter_repo(_repo(pushed_at=_recent_iso(days_ago=400)))
    assert keep is False
    assert reason == "stale"


def test_archived_beats_licence_so_the_histogram_is_stable():
    """Order is fixed on purpose: a repo failing two rules must always report
    the SAME one, or the reject histogram shifts between releases for no
    reason."""
    keep, reason = ds.filter_repo(_repo(archived=True, licence="gpl-3.0"))
    assert keep is False
    assert reason == "archived"


def test_no_copyleft_licence_is_ever_permissive():
    for lic in ("gpl-3.0", "agpl-3.0", "lgpl-2.1", "mpl-2.0", "cc-by-sa-4.0",
                "other", "noassertion"):
        assert lic not in ds.PERMISSIVE_LICENCES
        keep, _ = ds.filter_repo(_repo(licence=lic))
        assert keep is False, f"{lic} must not survive"


def test_naive_and_aware_timestamps_both_parse_without_raising():
    """A naive value compared against an aware now() raises TypeError, which
    would take out the whole tick. _parse_ts must return aware-or-None."""
    for s in ("2026-08-01T00:00:00Z", "2026-08-01T00:00:00+00:00",
              "2026-08-01T00:00:00"):
        got = ds._parse_ts(s)
        assert got is not None and got.tzinfo is not None
    assert ds._parse_ts("") is None
    assert ds._parse_ts("garbage") is None


# ── Normalisation ────────────────────────────────────────────────────
def _gh_item(**over):
    base = {
        "full_name": "acme/py-codemod",
        "html_url": "https://github.com/acme/py-codemod",
        "description": "A libcst codemod collection",
        "stargazers_count": 400,
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "pushed_at": _recent_iso(),
        "archived": False,
        "topics": ["codemod"],
    }
    base.update(over)
    return base


def test_normalise_lowercases_spdx_and_survives_missing_licence_block():
    assert ds.normalise_repo(_gh_item())["licence"] == "mit"
    assert ds.normalise_repo(_gh_item(license=None))["licence"] is None
    assert ds.normalise_repo({})["licence"] is None


def test_normalise_never_raises_on_a_sparse_payload():
    got = ds.normalise_repo({})
    assert got["full_name"] == ""
    assert got["stars"] == 0
    assert got["topics"] == []


# ── Rotation ─────────────────────────────────────────────────────────
def test_rotation_covers_every_query_across_a_year():
    seen = {ds.pick_query(d)["slug"] for d in range(1, 367)}
    assert seen == {q["slug"] for q in ds.SCOUT_QUERIES}


def test_rotation_is_deterministic():
    assert ds.pick_query(42)["slug"] == ds.pick_query(42)["slug"]


# ── Tick accounting ──────────────────────────────────────────────────
def test_tick_counts_keeps_and_rejects(monkeypatch):
    monkeypatch.setenv("DETECTOR_SCOUT_ENABLED", "1")
    items = [
        _gh_item(full_name="acme/keep-1"),
        _gh_item(full_name="acme/keep-2"),
        _gh_item(full_name="acme/gpl", license={"spdx_id": "GPL-3.0"}),
        _gh_item(full_name="acme/old", pushed_at=_recent_iso(days_ago=900)),
        _gh_item(full_name="acme/arch", archived=True),
    ]
    out = ds.scout_tick(dry_run=True, search=lambda q, **k: (items, ""))
    assert out["seen"] == 5
    assert out["kept"] == 2
    assert sorted(out["kept_names"]) == ["acme/keep-1", "acme/keep-2"]
    assert out["rejects"]["licence:gpl-3.0"] == 1
    assert out["rejects"]["stale"] == 1
    assert out["rejects"]["archived"] == 1


def test_search_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("DETECTOR_SCOUT_ENABLED", "1")
    monkeypatch.setattr(ds, "persist", _explode)
    out = ds.scout_tick(dry_run=False, search=lambda q, **k: ([], "HTTPError:403"))
    assert out["skipped"] == "search_failed"
    assert out["seen"] == 0
    assert "403" in out["error"]


class _Resp:
    """Minimal requests.Response stand-in."""

    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def _patch_requests(monkeypatch, resp=None, raiser=None):
    import types
    mod = types.SimpleNamespace()

    def _get(url, params=None, headers=None, timeout=None):
        if raiser:
            raise raiser
        return resp

    mod.get = _get
    monkeypatch.setitem(sys.modules, "requests", mod)


def test_github_search_reports_rate_limit_distinctly(monkeypatch):
    """403 + remaining=0 is a rate limit, NOT a bad query. Conflating them
    would make a throttled scout look like a broken query set — the exact
    misreading that would send someone rewriting the queries."""
    _patch_requests(monkeypatch, _Resp(
        status=403, headers={"X-RateLimit-Remaining": "0"}, text="rate limited"))
    items, err = ds.github_search("anything")
    assert items == []
    assert err.startswith("rate_limited:")


def test_github_search_reports_other_403_as_http_error(monkeypatch):
    _patch_requests(monkeypatch, _Resp(
        status=403, headers={"X-RateLimit-Remaining": "42"}, text="forbidden"))
    items, err = ds.github_search("anything")
    assert items == [] and err.startswith("http_403:")


def test_github_search_never_raises_on_transport_error(monkeypatch):
    _patch_requests(monkeypatch, raiser=OSError("dns boom"))
    items, err = ds.github_search("anything")
    assert items == [] and err.startswith("OSError:")


def test_github_search_never_raises_on_bad_json(monkeypatch):
    class _Bad(_Resp):
        def json(self):
            raise ValueError("not json")
    _patch_requests(monkeypatch, _Bad(status=200))
    items, err = ds.github_search("anything")
    assert items == [] and err.startswith("bad_json:")


def test_github_search_happy_path_returns_items(monkeypatch):
    _patch_requests(monkeypatch, _Resp(status=200, payload={"items": [_gh_item()]}))
    items, err = ds.github_search("anything")
    assert err == "" and len(items) == 1


def test_module_does_not_call_urllib_urlopen():
    """scripts/regression_lint.py blocks urllib.request.urlopen
    (urllib-request-on-railway). Mirror its AST check rather than scanning raw
    text — the rule is about CALLS, and a text scan false-positives on a
    docstring that merely names the banned API."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ds))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        f = node.func
        assert not (
            f.attr == "urlopen"
            and isinstance(f.value, ast.Attribute) and f.value.attr == "request"
            and isinstance(f.value.value, ast.Name) and f.value.value.id == "urllib"
        ), f"urllib.request.urlopen call at line {node.lineno}"


def test_empty_result_set_is_zero_not_an_error(monkeypatch):
    monkeypatch.setenv("DETECTOR_SCOUT_ENABLED", "1")
    out = ds.scout_tick(dry_run=True, search=lambda q, **k: ([], ""))
    assert out["seen"] == 0 and out["kept"] == 0


# ── Honesty of the status surface ────────────────────────────────────
def test_status_reports_missing_db_as_unavailable_not_zero(monkeypatch):
    monkeypatch.setattr(ds, "_connect", lambda: None)
    snap = ds.status_snapshot()
    assert snap["db"] is False
    assert "survivors_in_window" not in snap
    assert "not zero" in snap["note"]


def test_status_exposes_the_exit_criterion(monkeypatch):
    monkeypatch.setattr(ds, "_connect", lambda: None)
    snap = ds.status_snapshot()
    assert snap["target_survivors"] >= 1
    assert snap["window_days"] >= 1


# ── The interval trap this repo already has a detector class for ─────
def test_status_sql_does_not_use_the_interval_literal_antipattern():
    """`INTERVAL '%s days'` cannot bind an int safely — it is one of the six
    allowlisted transform classes (interval_literal). The scout must not ship
    the very shape the pipeline exists to hunt."""
    import inspect
    src = inspect.getsource(ds.status_snapshot)
    assert "INTERVAL '%s" not in src
    assert "%s * INTERVAL '1 day'" in src


def test_module_has_no_llm_call():
    """Phase 0 is explicitly model-free. If this starts failing, the phase
    boundary moved without the doc moving with it."""
    import inspect
    src = inspect.getsource(ds)
    for banned in ("anthropic", "ANTHROPIC_API_KEY", "openai", "messages.create"):
        assert banned not in src, f"Phase 0 must not reference {banned}"
