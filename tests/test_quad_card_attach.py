"""Quad card-image attach hardening — 2026-07-17 cards-never-seen audit.

The branded data-cards (og_cards style=data_card) won editorial slots and
recorded og=data_card + success=TRUE, yet no card ever appeared on the
LinkedIn feed. The quad's _fetch_image_bytes validated only SIZE — a
CF-failover mojibake PNG or a bot-shell HTML page (>1000B) passed, uploaded
fine, then died ASYNC in LinkedIn's processor, and linkedin_poster fell back
to a text/article post while still reporting success. Locked here:

  1. _og_fetch_candidates — own-origin card URLs fetch via LOOPBACK first
     (bypassing the CF edge entirely), public URL as fallback.
  2. _looks_like_image — magic-byte validation; corrupt bytes are rejected
     at fetch time instead of uploaded to fail silently.
  3. _record stamps image_attached so "did the card really attach?" is
     auditable per slot (exposed in /status).

DB-free; never imports main.
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import contextlib  # noqa: E402
import datetime  # noqa: E402

import pytest  # noqa: E402

lq = pytest.importorskip("routes.linkedin_quad_daily")  # noqa: E402


REAL_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 4000
MOJIBAKE_PNG = b"\xef\xbf\xbdPNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 4000
BOT_SHELL_HTML = (b"<!doctype html><html><head><title>DC Hub</title></head>"
                  b"<body>challenge</body></html>" + b" " * 2000)


# ── 1. loopback-first candidates ─────────────────────────────────────

def test_own_origin_url_gets_loopback_first(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    cands = lq._og_fetch_candidates(
        "https://api.dchub.cloud/api/v1/og/dynamic.png?style=data_card&kind=weekly_ledger")
    assert cands[0] == ("http://localhost:8080/api/v1/og/dynamic.png"
                        "?style=data_card&kind=weekly_ledger")
    assert cands[1].startswith("https://api.dchub.cloud/")


def test_apex_and_www_also_loop_back():
    for host in ("dchub.cloud", "www.dchub.cloud"):
        cands = lq._og_fetch_candidates(f"https://{host}/api/v1/og/x.png")
        assert cands[0].startswith("http://localhost:")


def test_foreign_url_is_not_rewritten():
    cands = lq._og_fetch_candidates("https://example.com/some.png")
    assert cands == ["https://example.com/some.png"]
    assert lq._og_fetch_candidates("") == []


# ── 2. magic-byte validation ─────────────────────────────────────────

def test_looks_like_image_accepts_real_formats():
    assert lq._looks_like_image(REAL_PNG)
    assert lq._looks_like_image(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100)
    assert lq._looks_like_image(b"GIF89a" + b"\x00" * 100)
    assert lq._looks_like_image(b"RIFF\x00\x10\x00\x00WEBPVP8 " + b"\x00" * 100)


def test_looks_like_image_rejects_corruption():
    # The exact live classes that killed the cards silently:
    assert not lq._looks_like_image(MOJIBAKE_PNG)   # CF-failover mojibake
    assert not lq._looks_like_image(BOT_SHELL_HTML)  # WAF/bot-shell HTML page
    assert not lq._looks_like_image(b"")
    assert not lq._looks_like_image(None)


class _FakeResp:
    def __init__(self, content, status=200):
        self._c = content
        self.status = status

    def read(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_rejects_html_and_falls_through(monkeypatch):
    """Loopback serves the bot-shell HTML (or garbage) → the public fallback
    with a REAL png wins. Before this fix the HTML itself would have been
    'fetched' successfully and uploaded."""
    import urllib.request

    def _fake_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.startswith("http://localhost:"):
            return _FakeResp(BOT_SHELL_HTML)
        return _FakeResp(REAL_PNG)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = lq._fetch_image_bytes("https://api.dchub.cloud/api/v1/og/dynamic.png?style=data_card&kind=agent_memory")
    assert out == REAL_PNG


def test_fetch_returns_none_when_all_candidates_corrupt(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp(MOJIBAKE_PNG))
    assert lq._fetch_image_bytes("https://api.dchub.cloud/x.png") is None


def test_fetch_prefers_loopback_when_healthy(monkeypatch):
    import urllib.request
    seen = []

    def _fake_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        return _FakeResp(REAL_PNG)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = lq._fetch_image_bytes("https://api.dchub.cloud/api/v1/og/dynamic.png?style=data_card&kind=tool_catalog")
    assert out == REAL_PNG
    assert len(seen) == 1 and seen[0].startswith("http://localhost:")


# ── 3. image_attached is recorded per slot ───────────────────────────

class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


def test_record_stamps_image_attached(monkeypatch):
    cur = _FakeCursor()

    @contextlib.contextmanager
    def _ctx():
        yield _FakeConn(cur)

    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _ctx)
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    lq._record(datetime.date(2026, 7, 17), 8, "dcpi_mover", "data", "body",
               "https://l", "https://og",
               {"ok": True, "urn": "urn:li:share:1", "image_attached": True})
    sql, params = cur.executed[0]
    assert "image_attached" in sql
    assert params[-1] is True

    lq._record(datetime.date(2026, 7, 17), 12, "hyperscaler_deal", "data",
               "body", "https://l", "https://og",
               {"ok": True, "urn": "urn:li:share:2"})
    _sql2, params2 = cur.executed[1]
    assert params2[-1] is False  # degraded-to-text is now visible


def test_quad_result_normalizer_keeps_image_attached():
    import inspect
    src = inspect.getsource(lq._post_to_linkedin)
    assert "image_attached" in src
