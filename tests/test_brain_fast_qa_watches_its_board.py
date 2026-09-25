"""fast-QA watches the brain's own public board — without crying wolf on it.

2026-09-12: /brain-live and /brain/public returned 500 for ~40 minutes. fast-QA,
every ~30 min, reported healthy throughout, because neither URL was on its list.
The L11 QA agent meant to sweep every public surface had been disabled since
2026-05-19.

Adding them naively would have traded silence for noise: the board renders in
2.2-5.0s cold against fast-QA's 4s default, so its slow runs would be filed as
HIGH outages — the same false alarm /api/v1/mcp/funnel had raised for 77 days
while returning 200 in 3.3-6.5s.
"""
import ast
import importlib
import pathlib

import pytest

flask = pytest.importorskip("flask")

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BOARD = ("/brain-live", "/brain/public")


def _fq():
    return importlib.import_module("routes.brain_fast_qa")


def _radar():
    return importlib.import_module("routes.brain_consistency_radar")


# ── the board is on the list ─────────────────────────────────────────
def test_the_board_is_on_the_probe_list():
    urls = _fq()._PUBLIC_URLS
    for p in _BOARD:
        assert p in urls, "%s is not probed — an outage there goes unnoticed" % p


# ── and gets a budget it can meet ────────────────────────────────────
class _Resp:
    def __init__(self, status):
        self.status_code = status


class _Session:
    """Records the timeout each URL was fetched with. Carries only what
    _check_urls actually uses on a real requests.Session."""
    def __init__(self, behaviour=None):
        self.headers = {}
        self.seen = {}
        self.behaviour = behaviour or {}

    def get(self, url, timeout=None, allow_redirects=True, headers=None):
        # `headers`: _check_urls passes per-request headers (the ops read
        # key on /api/v1/mcp/funnel), which a real Session.get accepts.
        path = url.replace("https://dchub.cloud", "", 1)
        self.seen[path] = timeout
        b = self.behaviour.get(path, 200)
        if isinstance(b, BaseException):
            raise b
        return _Resp(b)

    def close(self):
        pass


def _sweep(monkeypatch, behaviour=None):
    import requests
    sess = _Session(behaviour)
    monkeypatch.setattr(requests, "Session", lambda: sess)
    problems, rate_limited = _fq()._check_urls()
    return sess, problems, rate_limited


def test_the_board_is_fetched_with_its_own_budget_not_the_default(monkeypatch):
    m = _fq()
    sess, _p, _r = _sweep(monkeypatch)
    for p in _BOARD:
        assert sess.seen.get(p) is not None, "%s was never fetched" % p
        assert sess.seen[p] > m._PER_URL_TIMEOUT, (
            "%s fetched with %ss — its cold render measured up to 4.99s"
            % (p, sess.seen[p]))
    assert sess.seen["/"] == m._PER_URL_TIMEOUT, "unlisted URLs keep the default"


def test_each_budget_clears_its_worst_measured_latency():
    """The samples that set these budgets (2026-09-12, 8x through the edge).
    A budget that does not clearly clear its own worst sample still cries wolf."""
    m = _fq()
    worst = {"/api/v1/mcp/funnel": 6.53, "/brain-live": 4.99, "/brain/public": 2.89}
    for p, w in worst.items():
        assert m.url_timeout(p) >= 1.5 * w, (p, m.url_timeout(p), w)


def test_a_500_on_the_board_is_filed(monkeypatch):
    """The exact failure of 2026-09-12 must become a problem, not a pass."""
    _s, problems, _r = _sweep(monkeypatch, {"/brain-live": 500})
    assert any(pr["path"] == "/brain-live" and pr["status"] == 500 for pr in problems), problems


def test_a_hang_on_the_board_is_still_filed(monkeypatch):
    """A longer budget must not silence a board that genuinely hangs."""
    import requests
    _s, problems, _r = _sweep(
        monkeypatch, {"/brain/public": requests.exceptions.ReadTimeout("slow")})
    assert any(pr["path"] == "/brain/public" for pr in problems), problems


def test_a_healthy_sweep_files_nothing(monkeypatch):
    _s, problems, _r = _sweep(monkeypatch)
    assert problems == [], problems


# ── the detector keeps it that way ───────────────────────────────────
def test_public_pages_are_derived_from_the_real_module():
    """Floor: an empty derivation would read as 'nothing unwatched', so today's
    result is pinned — the two public board routes, neither admin one."""
    src = (_ROOT / "routes" / "brain_v2_public.py").read_text(encoding="utf-8")
    pages = _radar().derive_public_brain_pages(src)
    assert set(_BOARD) <= set(pages), pages
    assert "/brain" not in pages and "/brain/" not in pages, \
        "the admin-gated /brain must not be required on an anonymous probe list"


def test_derivation_skips_admin_gated_parameterised_and_non_get_routes():
    src = '''
from flask import Blueprint
bp = Blueprint("x", __name__)
def _pub_admin_ok():
    return False

@bp.route("/open", methods=["GET"])
def open_view():
    return "ok"

@bp.route("/closed")
def closed_view():
    if not _pub_admin_ok():
        return "no", 403
    return "ok"

@bp.route("/thing/<slug>")
def param_view(slug):
    return slug

@bp.route("/write", methods=["POST"])
def write_view():
    return "ok"
'''
    assert _radar().derive_public_brain_pages(src) == ["/open"]


def test_the_detector_fires_when_a_board_page_is_dropped(monkeypatch):
    m = _fq()
    monkeypatch.setattr(m, "_PUBLIC_URLS", [u for u in m._PUBLIC_URLS if u != "/brain-live"])
    out = _radar().check_brain_public_pages_are_fast_qa_watched()
    assert len(out) == 1, out
    assert out[0]["issue"] == "brain_public_page_unwatched"
    assert "/brain-live" in out[0]["detail"]


def test_the_detector_is_silent_when_the_board_is_watched():
    assert _radar().check_brain_public_pages_are_fast_qa_watched() == []


def test_the_detector_is_registered_in_the_sweep():
    """A check defined but absent from scan_all's container never runs."""
    tree = ast.parse((_ROOT / "routes" / "brain_consistency_radar.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "scan_all":
            for sub in ast.walk(node):
                if isinstance(sub, ast.For) and isinstance(sub.iter, (ast.Tuple, ast.List)):
                    names |= {e.id for e in sub.iter.elts if isinstance(e, ast.Name)}
    assert "check_brain_public_pages_are_fast_qa_watched" in names
