"""A checker may not report a marker as ABSENT from bytes it never read.

★ 2026-09-15. Site Sentinel read the first 64 KiB of a page and then asserted
the nav tag was not in it:

    body = r.raw.read(64 * 1024, ...) if r.raw else r.content[:64*1024]
    ...
    out["has_nav"] = _has_dchub_nav(body_str)      # False => "nav_missing"

MEASURED against production, 2026-09-15:

    https://dchub.cloud/markets/  ->  HTTP 200, 129,740 bytes,
    contains dchub-nav.js TWICE, first occurrence at byte 77,126.

77,126 > 65,536, so the page could never pass. Sentinel reported
"Page 'Markets' returns 200 with 65536 bytes but does NOT include dchub-nav"
and held /markets/ STUCK on nav_missing for 325.7 hours (~13 days). The page
was never broken; the read window was smaller than the page being checked.
The reported byte count was the window too, not the page.

The failure was silent in BOTH directions: a page over the cap could never
pass, and a page that really had lost its nav was indistinguishable from one
that had not.

Same sweep, same day, the rest of the manifest: 9 of 30 wants_nav pages
already exceed 64 KiB (/ai 360,918 · / 177,224 · /markets/ 129,740 ·
/ecosystem 78,891 · /pricing 78,220 · /dc-hub-media 74,840 · /api-docs 69,593
· /tax-incentives 69,114 · /capacity-pipeline 66,716), and several carry the
nav tag in the last 1% of the body (/news at 39,517 of 39,683; /operators at
14,992 of 15,039) — one growth spurt from the same false alarm.

THE CONTRACT
────────────
  S1. A marker PAST the old 64 KiB window is FOUND, not reported absent.
      This is the regression: it fails on the unpatched module.
  S2. When the page really is bigger than the window, the verdict is
      INDETERMINATE (has_nav None -> SQL NULL), never `nav_missing`. The
      probe may report its own limit; it may not invent a fact about the page.
  S3. The guard can still FAIL: a page read IN FULL with no nav tag is still
      nav_missing. (Without S3 this file would pass on a checker that had
      simply been switched off.)
  S4. An indeterminate scan never reaches the nav_missing finding — that
      finding sends the autopilot to edit a page template that is correct.
  S5. The window clears the largest page actually served (/ai, 360,918 B).
      A cap below that silently recreates the bug for one real page.

House rules: no DB, never import main. The module imports clean (its DB
handles are lazy), so this drives the REAL _scan_one over a REAL socket
rather than mocking urllib3's read semantics — the exact layer the bug lived
in.

EXPECTED PASS/FAIL — MEASURED, not predicted. See the PR body.
"""
import http.server
import threading

import pytest

pytest.importorskip("flask")
pytest.importorskip("requests")

import routes.site_sentinel as S  # noqa: E402

OLD_WINDOW = 64 * 1024
TAG = b'<script src="/js/dchub-nav.js" defer></script>'


def _body(total: int, nav_at: int | None) -> bytes:
    """A body of exactly `total` bytes whose first nav marker starts at
    `nav_at` (or which contains no marker at all when nav_at is None)."""
    if nav_at is None:
        out = b"<html>" + b"x" * (total - 13) + b"</html>"
    else:
        off = TAG.index(b"dchub-nav.js")
        head = b"x" * (nav_at - off)
        out = head + TAG
        out += b"y" * (total - len(out))
    assert len(out) == total, (len(out), total)
    found = out.lower().find(b"dchub-nav.js")
    assert found == (-1 if nav_at is None else nav_at), found
    return out


# The exact production shape measured above.
MARKETS = _body(129_740, 77_126)
NAVLESS = _body(5_000, None)
PAGES = {"/markets/": MARKETS, "/navless": NAVLESS}


class _H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep pytest output clean
        pass

    def handle_error(self, *a):
        # The truncation test stops reading mid-body on purpose; the broken
        # pipe that follows is the POINT of that test, not a failure.
        pass


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def probe(base, monkeypatch):
    """_scan_one against the local server. Restores _SITE_BASE in teardown."""
    monkeypatch.setattr(S, "_SITE_BASE", base)
    monkeypatch.setattr(S, "_ADMIN_KEY", "", raising=False)

    def run(path, **entry):
        e = {"path": path, "category": "high", "min_bytes": 100,
             "label": "T", "wants_nav": True}
        e.update(entry)
        return S._scan_one(e)
    return run


# ── S1 — the regression ──────────────────────────────────────────────────
def test_marker_past_the_old_window_is_not_reported_absent(probe):
    """/markets/ shape: 129,740 bytes, nav at 77,126. Must be FOUND."""
    assert MARKETS.lower().find(b"dchub-nav.js") > OLD_WINDOW, "fixture too small"
    r = probe("/markets/")
    assert r["status_code"] == 200
    assert r["reason"] != "nav_missing", (
        "nav tag at byte 77,126 reported ABSENT — the read window is smaller "
        "than the page it checks")
    assert r["has_nav"] is True
    assert r["healthy"] is True
    assert r["reason"] == "ok"


def test_reported_byte_count_is_the_page_not_the_window(probe):
    """The finding text quotes `bytes`; 65536 was the window, not the page."""
    r = probe("/markets/")
    assert r["bytes"] == 129_740, r["bytes"]
    assert r["truncated"] is False


# ── S2 — over the window is UNKNOWN, not absent ──────────────────────────
def test_page_over_the_window_is_indeterminate_never_missing(probe, monkeypatch):
    monkeypatch.setattr(S, "_SCAN_CAP_BYTES", 8_192)
    r = probe("/markets/")
    assert r["truncated"] is True
    assert r["reason"].startswith("nav_indeterminate"), r["reason"]
    assert r["reason"] != "nav_missing"
    assert r["has_nav"] is None, (
        "has_nav must be NULL (unknown) when the body was not read in full — "
        f"got {r['has_nav']!r}, which asserts a fact the probe never read")


def test_nav_verdict_is_three_valued():
    """The unit under the bug: absent vs unknown must not collapse."""
    assert S._nav_verdict("...dchub-nav.js...", False) == (True, None)
    assert S._nav_verdict("...dchub-nav.js...", True) == (True, None)
    has_nav, reason = S._nav_verdict("no marker here", True)
    assert has_nav is None and reason.startswith("nav_indeterminate")
    assert S._nav_verdict("no marker here", False) == (False, "nav_missing")


# ── S3 — the guard can still fail ────────────────────────────────────────
def test_genuinely_navless_page_read_in_full_still_fails(probe):
    r = probe("/navless", min_bytes=100)
    assert r["truncated"] is False, "fixture must fit inside the window"
    assert r["has_nav"] is False
    assert r["reason"] == "nav_missing"
    assert r["healthy"] is False


# ── S4 — the finding never mislabels a checker limit as a page defect ────
def test_indeterminate_scan_does_not_emit_a_nav_missing_finding(monkeypatch):
    rows = [{"path": "/markets/", "label": "Markets", "category": "high",
             "healthy": False, "has_nav": None, "bytes": 8192,
             "reason": "nav_indeterminate:page_exceeds_8192B_read_window"}]
    monkeypatch.setattr(S, "latest_results", lambda: rows)
    out = S.unhealthy_findings()
    issues = [f["issue"] for f in out]
    assert not any(i.startswith("nav_missing") for i in issues), issues
    assert "nav_indeterminate:/markets/" in issues, issues
    detail = next(f["detail"] for f in out if f["issue"].startswith("nav_indet"))
    assert "UNKNOWN" in detail and "not absent" in detail


def test_real_nav_missing_still_emits_its_finding(monkeypatch):
    """S4's sibling: the finding path itself is not switched off."""
    rows = [{"path": "/navless", "label": "Navless", "category": "high",
             "healthy": False, "has_nav": False, "bytes": 5000,
             "reason": "nav_missing"}]
    monkeypatch.setattr(S, "latest_results", lambda: rows)
    issues = [f["issue"] for f in S.unhealthy_findings()]
    assert "nav_missing:/navless" in issues, issues


# ── S5 — the window clears the largest page actually served ──────────────
def test_window_clears_the_largest_real_page():
    """Measured 2026-09-15: /ai is 360,918 bytes, the largest wants_nav page.
    A cap under that silently recreates this bug for a real page."""
    assert S._SCAN_CAP_BYTES >= 512 * 1024, S._SCAN_CAP_BYTES
    assert S._SCAN_CAP_BYTES > OLD_WINDOW * 4
