"""Branded LinkedIn stat cards (DCHUB_LI_CARDS, 2026-07-31).

Operator complaint: "the linkedin posts are all texts." routes/media_card.py
renders a 1200x627 stat card in-process; content_publisher attaches it in
_post_to_linkedin between the r51 image-first path and the r64 fetched-card
fallback. These tests lock the three contracts that make the feature safe:

  1. RENDER — deterministic 1200x627 RGB PNG that passes the SAME validator
     the upload path applies (_looks_like_image_bytes + the 1KB..5MB window).
  2. NUMBERS — the card's headline is the post text's own matched substring,
     agreeing with _post_headline_signature (the gate's extraction). A card
     can never show a number its post doesn't say.
  3. FALLBACK — render failure, upload failure, or the kill-switch all end in
     the pre-existing text path with the commentary BYTE-IDENTICAL; a card
     can never block or alter a post.

DB-free (dup gate disabled via DCHUB_LINKEDIN_DUP_DAYS=0; HTTP stubbed at
cp.requests). Never imports main (pre-merge CI has no DB/JWT_SECRET).

PIL is imported HARD, not via importorskip: Pillow is in requirements.txt and
in pre-merge.yml's install line, and a skipped render test would be a silent
green — losing Pillow must fail the suite loudly.
"""
import inspect
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402  (hard import — see module docstring)

cp = pytest.importorskip("content_publisher")  # noqa: E402
mc = pytest.importorskip("routes.media_card")  # noqa: E402


SAMPLE = (
    "DC Hub MCP served 142,318 AI tool calls in the last 24h — up 18% "
    "week-over-week.\n\n"
    "Live infrastructure answers for AI agents across 170+ countries, "
    "refreshed continuously from independent grid and market feeds.\n\n"
    "#datacenter #AI"
)


class _Resp:
    def __init__(self, status_code, json_data=None, headers=None, text=''):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


def _publish_env(monkeypatch, cards='1'):
    """Env for driving _post_to_linkedin directly, DB- and network-free."""
    monkeypatch.delenv('LINKEDIN_PUBLISHER_DRY_RUN', raising=False)
    monkeypatch.setenv('DCHUB_LINKEDIN_DUP_DAYS', '0')   # dup gate → no DB
    monkeypatch.setenv('LINKEDIN_ATTACH_IMAGES', '1')
    monkeypatch.setenv('DCHUB_LI_CARDS', cards)
    # r64's fetched-card fallback must not hit the network in any test.
    monkeypatch.setattr(cp, '_fetch_image_bytes_for_linkedin', lambda url: None)


# ── 1. lead extraction: the text's numbers, verbatim ────────────────────────

def test_lead_headline_is_verbatim_text_substring():
    lead = cp._media_card_lead(SAMPLE)
    assert lead is not None
    assert lead['headline'] == '142,318'
    assert lead['headline'] in SAMPLE          # never reformatted, never recomputed
    assert lead['unit'] == 'AI tool calls'
    assert lead['trend'] is not None and 'up 18%' in lead['trend']
    assert lead['label'].startswith('DC Hub MCP served')


def test_lead_agrees_with_headline_signature():
    lead = cp._media_card_lead(SAMPLE)
    sig = cp._post_headline_signature(SAMPLE)
    assert float(lead['headline'].replace(',', '')) == sig['metric_value']
    assert sig['metric_label'] == 'mcp_tool_calls'


def test_coverage_ratio_headline():
    text = ("Analyst note: 7 of 7 US grid operators are now covered with "
            "independent live telemetry on DC Hub.")
    lead = cp._media_card_lead(text)
    assert lead is not None and lead['headline'] == '7 of 7'
    assert lead['unit'] is None                # the headline carries its own unit


def test_zero_stat_means_no_card():
    text = ("DC Hub MCP served 0 AI tool calls in the last 24h across the "
            "entire agent network today.")
    assert cp._media_card_lead(text) is None


def test_statless_text_means_no_card():
    text = ("We are excited about the future of infrastructure intelligence "
            "and everything agents will build together.")
    assert cp._media_card_lead(text) is None


# ── 2. render: deterministic dimensions/format, upload-validator clean ──────

def test_render_dimensions_format_and_validator():
    png = mc.render_stat_card(cp._media_card_lead(SAMPLE))
    img = Image.open(io.BytesIO(png))
    assert img.size == (1200, 627)
    assert img.format == 'PNG'
    assert img.mode == 'RGB'
    # The exact acceptance the publisher applies before upload.
    assert cp._looks_like_image_bytes(png)
    assert 1000 < len(png) < 5_000_000


def test_render_is_deterministic():
    lead = cp._media_card_lead(SAMPLE)
    assert mc.render_stat_card(lead) == mc.render_stat_card(dict(lead))


def test_render_minimal_and_missing_headline():
    png = mc.render_stat_card({'headline': '$4.2B'})   # no unit/label/trend
    assert Image.open(io.BytesIO(png)).size == (1200, 627)
    with pytest.raises(ValueError):
        mc.render_stat_card({})


# ── 3. publish path: attach on success, text-only on ANY failure ────────────

def test_card_attach_success_posts_image_with_identical_commentary(monkeypatch):
    _publish_env(monkeypatch)
    monkeypatch.setattr(cp, '_upload_image_to_linkedin',
                        lambda b, tok, org: 'urn:li:image:TEST')
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        if url.endswith('/rest/posts'):
            return _Resp(201, headers={'x-restli-id': 'urn:li:share:CARD1'})
        raise AssertionError(f'unexpected POST {url}')

    monkeypatch.setattr(cp.requests, 'post', fake_post)
    ok, res = cp._post_to_linkedin(SAMPLE, 'tok-test')
    assert ok is True and res == 'urn:li:share:CARD1'
    assert len(calls) == 1 and calls[0][0].endswith('/rest/posts')
    payload = calls[0][1]['json']
    assert payload['content']['media']['id'] == 'urn:li:image:TEST'
    # The wire text is exactly what the text-only path would send — a card
    # never changes what the gate scored.
    assert payload['commentary'] == cp.escape_li_commentary(SAMPLE)


def test_upload_failure_falls_back_to_text_only(monkeypatch):
    _publish_env(monkeypatch)
    monkeypatch.setattr(cp, '_upload_image_to_linkedin', lambda b, tok, org: None)
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        if url.endswith('/v2/ugcPosts'):
            return _Resp(201, json_data={'id': 'urn:li:share:UGC1'})
        raise AssertionError(f'unexpected POST {url}')

    monkeypatch.setattr(cp.requests, 'post', fake_post)
    ok, res = cp._post_to_linkedin(SAMPLE, 'tok-test')
    assert ok is True and res == 'urn:li:share:UGC1'
    body = calls[-1][1]['json']['specificContent']['com.linkedin.ugc.ShareContent']
    assert body['shareMediaCategory'] == 'NONE'
    assert body['shareCommentary']['text'] == SAMPLE      # byte-identical text


def test_render_exception_falls_back_to_text_only(monkeypatch):
    _publish_env(monkeypatch)

    def boom(lead):
        raise RuntimeError('render exploded')

    monkeypatch.setattr(mc, 'render_stat_card', boom)
    monkeypatch.setattr(
        cp, '_upload_image_to_linkedin',
        lambda *a, **k: pytest.fail('upload must not run when render fails'))

    def fake_post(url, **kw):
        assert url.endswith('/v2/ugcPosts')
        return _Resp(201, json_data={'id': 'urn:li:share:UGC2'})

    monkeypatch.setattr(cp.requests, 'post', fake_post)
    ok, res = cp._post_to_linkedin(SAMPLE, 'tok-test')
    assert ok is True and res == 'urn:li:share:UGC2'


def test_kill_switch_skips_card_entirely(monkeypatch):
    _publish_env(monkeypatch, cards='0')
    monkeypatch.setattr(
        mc, 'render_stat_card',
        lambda lead: pytest.fail('DCHUB_LI_CARDS=0 must skip the card path'))

    def fake_post(url, **kw):
        assert url.endswith('/v2/ugcPosts')
        return _Resp(201, json_data={'id': 'urn:li:share:UGC3'})

    monkeypatch.setattr(cp.requests, 'post', fake_post)
    ok, res = cp._post_to_linkedin(SAMPLE, 'tok-test')
    assert ok is True and res == 'urn:li:share:UGC3'


def test_statless_post_skips_card_without_touching_render(monkeypatch):
    _publish_env(monkeypatch)
    monkeypatch.setattr(
        mc, 'render_stat_card',
        lambda lead: pytest.fail('no headline metric → card path must not run'))

    def fake_post(url, **kw):
        assert url.endswith('/v2/ugcPosts')
        return _Resp(201, json_data={'id': 'urn:li:share:UGC4'})

    monkeypatch.setattr(cp.requests, 'post', fake_post)
    text = ("Our editorial desk keeps refining how infrastructure stories "
            "reach the agents that need them, every single day.")
    ok, _ = cp._post_to_linkedin(text, 'tok-test')
    assert ok is True


# ── 4. schema discipline: no DDL, linkedin_posts untouched ──────────────────

def test_media_card_module_runs_no_ddl_and_never_touches_linkedin_posts():
    import re
    src = open(os.path.join(ROOT, 'routes', 'media_card.py'),
               encoding='utf-8').read()
    assert 'CREATE TABLE' not in src
    assert 'ALTER TABLE' not in src
    # SQL references only — the docstring is allowed to NAME the table while
    # promising not to touch it.
    assert not re.search(r'(?i)\b(?:from|into|update|join|table)\s+linkedin_posts\b',
                         src)


def test_card_lead_helper_runs_no_sql():
    src = inspect.getsource(cp._media_card_lead)
    for kw in ('CREATE', 'ALTER', 'INSERT', 'UPDATE', 'SELECT'):
        assert kw not in src
