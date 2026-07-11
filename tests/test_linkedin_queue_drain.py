"""LinkedIn social-queue drain fixes — 2026-07-11 audit.

Locks the three defects behind the "21 failed rows in 7d, all market-verdict
posts" incident:

  1. ENQUEUE DEDUP NO-OP — content_enqueue._already_enqueued_recently matched
     content LIKE '%slug:VERDICT%', a substring that never appears in any
     shaped post, so the 2h cron flooded the queue with identical market copy
     (Papillion alone: 8 identical rows). Now dedups on the shaped content's
     normalized opening (same key style as the publish-side duplicate gate)
     over the same 7-day window.

  2. GATE REFUSALS MARKED 'failed' — _post_to_linkedin returns
     (False, {'error': <gate>, ...}) for its own editorial gates
     (quality/policy/duplicate) and (False, "LinkedIn API error ...") strings
     for real API errors. The drain marked both 'failed'. _li_gate_refusal
     now distinguishes them; refusals become terminal 'rejected' (r78).

  3. MOJIBAKE IMAGE BYTES UPLOADED — the CF worker failover serves og-card
     PNGs whose non-ASCII bytes were replaced with b'\\xef\\xbf\\xbd' (UTF-8
     replacement char). They pass the size check, PUT fine, then die async in
     LinkedIn's processor (PROCESSING_FAILED x3 retries, ~40-60s + image-API
     quota per attempt). _fetch_image_bytes_for_linkedin now validates magic
     bytes before returning.

DB-free: the enqueue-dedup test injects a fake connection; everything else is
pure. Never imports main (pre-merge CI has no DB/JWT_SECRET).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402
ce = pytest.importorskip("routes.content_enqueue")  # noqa: E402


# The exact copy class that was failing (post 3929, 2026-07-09).
FAILING_POST = (
    "Papillion (SPP) rates BUILD on the DC Hub Power Index this week, with an "
    "Excess Power score of 60/100 — one of the strongest grid-headroom "
    "readings we track.\n\n"
    "For teams screening SPP for AI capacity, that combination — headroom "
    "60/100 against a grid constraint of just 32/100 — usually means shorter "
    "interconnection timelines and real negotiating leverage on power "
    "contracts.\n\n"
    "Daily-refreshed score, methodology + sources on the live page: "
    "https://dchub.cloud/dcpi/papillion\n\n"
    "#datacenter #DCPI #spp"
)


# ── 1. gate-refusal vs API-error classification ─────────────────────

def test_duplicate_gate_is_refusal_not_error():
    r = cp._li_gate_refusal({"error": "duplicate_gate",
                             "reason": "already posted within 7 days"})
    assert r is not None
    assert "duplicate_gate" in r
    assert "already posted" in r


def test_quality_and_policy_gates_are_refusals():
    assert cp._li_gate_refusal({"error": "content_quality_gate",
                                "reason": "too short"}) is not None
    assert cp._li_gate_refusal({"error": "editorial_policy_gate",
                                "reason": "leads with a downgrade"}) is not None


def test_api_error_string_is_not_a_refusal():
    # Real API errors come back as strings and must keep status='failed'.
    assert cp._li_gate_refusal("LinkedIn API error 422: duplicate") is None
    assert cp._li_gate_refusal("LinkedIn API error 401: expired") is None
    assert cp._li_gate_refusal(None) is None
    # Unknown dict shapes are NOT silently swallowed as refusals either.
    assert cp._li_gate_refusal({"error": "something_else"}) is None


def test_gate_refusal_classifies_as_duplicate_error_class():
    # The state-machine tag for a duplicate refusal stays 'duplicate' (the
    # dict stringifies through _classify_publish_error unchanged).
    assert cp._classify_publish_error(
        {"error": "duplicate_gate", "reason": "already posted"}) == "duplicate"


# ── 2. image magic-byte validation ──────────────────────────────────

MOJIBAKE_PNG = b"\xef\xbf\xbdPNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 4000
REAL_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 4000
REAL_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 4000
REAL_WEBP = b"RIFF\x00\x10\x00\x00WEBPVP8 " + b"\x00" * 4000


def test_looks_like_image_bytes():
    assert cp._looks_like_image_bytes(REAL_PNG)
    assert cp._looks_like_image_bytes(REAL_JPEG)
    assert cp._looks_like_image_bytes(b"GIF89a" + b"\x00" * 100)
    assert cp._looks_like_image_bytes(REAL_WEBP)
    # The exact live corruption observed on /dcpi/og/papillion.png 2026-07-11:
    # every non-ASCII byte replaced with the UTF-8 replacement character.
    assert not cp._looks_like_image_bytes(MOJIBAKE_PNG)
    assert not cp._looks_like_image_bytes(b"<html>not an image</html>" * 100)
    assert not cp._looks_like_image_bytes(b"")
    assert not cp._looks_like_image_bytes(None)


class _FakeResp:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def test_fetch_image_bytes_rejects_mojibake(monkeypatch):
    monkeypatch.setattr(cp.requests, "get",
                        lambda *a, **k: _FakeResp(MOJIBAKE_PNG))
    assert cp._fetch_image_bytes_for_linkedin("https://x/corrupt.png") is None


def test_fetch_image_bytes_accepts_real_png(monkeypatch):
    monkeypatch.setattr(cp.requests, "get",
                        lambda *a, **k: _FakeResp(REAL_PNG))
    assert cp._fetch_image_bytes_for_linkedin("https://x/ok.png") == REAL_PNG


def test_fetch_image_bytes_keeps_size_gate(monkeypatch):
    # <1KB (tracking-pixel fallbacks) still rejected even with a valid magic.
    monkeypatch.setattr(cp.requests, "get",
                        lambda *a, **k: _FakeResp(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10))
    assert cp._fetch_image_bytes_for_linkedin("https://x/tiny.png") is None


# ── 3. enqueue-side content dedup actually matches ──────────────────

def test_content_dedup_key_normalizes_whitespace():
    key = ce._content_dedup_key(FAILING_POST)
    assert key == ("Papillion (SPP) rates BUILD on the DC Hub Power Index "
                   "this w")
    assert len(key) == 60
    # Newlines/multi-spaces collapse to the same key.
    assert ce._content_dedup_key(FAILING_POST.replace(" ", "  ", 3)) == key


def test_like_escape_neutralizes_wildcards():
    assert ce._like_escape("60% _x_ 100\\") == "60\\% \\_x\\_ 100\\\\"


class _FakeCursor:
    def __init__(self, count):
        self._count = count
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return [self._count]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, count):
        self.cur = _FakeCursor(count)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def test_already_enqueued_recently_matches_reenqueued_copy(monkeypatch):
    """The regression: the SAME shaped market post re-enqueued within the
    window must now be detected (the old topic_key LIKE never matched)."""
    conn = _FakeConn(count=1)
    monkeypatch.setattr(ce, "_db_conn", lambda: conn)
    assert ce._already_enqueued_recently("linkedin", FAILING_POST) is True

    sql, params = conn.cur.executed[0]
    platform, like_pattern, hours = params
    assert platform == "linkedin"
    # The LIKE pattern is a PREFIX match on the post's own normalized opening
    # — a substring that provably occurs in the stored row (unlike the old
    # 'papillion:BUILD' key, which occurs in no post ever generated).
    assert like_pattern.endswith("%")
    assert like_pattern.startswith("Papillion (SPP) rates BUILD")
    # 7-day window, mirroring the publish-side duplicate gate.
    assert int(hours) == 168


def test_already_enqueued_recently_allows_fresh_content(monkeypatch):
    conn = _FakeConn(count=0)
    monkeypatch.setattr(ce, "_db_conn", lambda: conn)
    assert ce._already_enqueued_recently("linkedin", FAILING_POST) is False


def test_already_enqueued_recently_fails_open(monkeypatch):
    monkeypatch.setattr(ce, "_db_conn", lambda: None)
    assert ce._already_enqueued_recently("linkedin", FAILING_POST) is False
    assert ce._already_enqueued_recently("linkedin", "") is False


def test_score_change_produces_new_dedup_key():
    """A market whose DCPI score moves gets fresh copy — the score is inside
    the first-60-char key, so the new story is NOT deduped away."""
    mover_a = {"name": "Papillion", "iso": "SPP", "verdict": "BUILD",
               "excess": 60.0, "constraint": 32.0, "slug": "papillion"}
    mover_b = dict(mover_a, excess=72.0)
    key_a = ce._content_dedup_key(ce._shape_linkedin(mover_a, None), n=120)
    key_b = ce._content_dedup_key(ce._shape_linkedin(mover_b, None), n=120)
    assert key_a != key_b
