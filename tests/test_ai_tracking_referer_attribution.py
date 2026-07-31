"""detect_platform reads UA + Referer — the cumulative cards stop lying low.

The defect (2026-07-31, operator-reported): the /ai live feed showed
HuggingFace and Cohere landing in real time while their cumulative cards sat
frozen ('1 total', '5 total'). Two classifiers, two truths: the feed's
sibling (ai_interconnection.detect_ai_platform) reads UA+Referer; the
cumulative pipeline's detect_platform read UA only AND short-circuited
generic HTTP libs to 'internal' before the platform loop could look — and a
HuggingFace Space presents exactly as python-requests/httpx + referer
huggingface.co.

Order of evidence, each pinned below:
  1. SELF markers win over everything, including a platform Referer (r62:
     our probe is ours no matter what page it claims to come from);
  2. named platforms match over UA + EXTERNAL referer;
  3. own-domain referers are discarded (a browser on our own
     /integrations/grok page must never count as Grok traffic);
  4. generic libs with no platform evidence stay 'internal'.
"""
import pytest

at = pytest.importorskip("ai_tracking")


def test_generic_lib_with_platform_referer_attributes():
    """The exact live case: HF Space calling with a stock python client."""
    assert at.detect_platform("python-requests/2.31",
                              "https://huggingface.co/spaces/x/y") == "huggingface"
    assert at.detect_platform("python-httpx/0.27",
                              "https://huggingface.co/") == "huggingface"
    assert at.detect_platform("aiohttp/3.9",
                              "https://dashboard.cohere.com/") == "cohere"


def test_generic_lib_alone_stays_internal():
    for ua in ("python-requests/2.31", "python-httpx/0.27", "curl/8.4",
               "go-http-client/2.0", "node-fetch/3.0", "axios/1.6"):
        assert at.detect_platform(ua) == "internal", ua
        assert at.detect_platform(ua, "") == "internal", ua


def test_self_markers_beat_platform_referers():
    """r62 regression pin: our own probes stay ours — a platform referer must
    never rescue a dchub-* UA into a named bucket."""
    assert at.detect_platform("dchub-brain-probe/1.0",
                              "https://huggingface.co/") == "internal"
    assert at.detect_platform("dchub-claude-helper",
                              "https://openai.com/") == "internal"


def test_domain_boundary_matching_is_strict():
    """Platform-KEY referer matching fires on domain boundaries only — a host
    merely containing the word must not attribute."""
    assert at.detect_platform("python-requests/2.31",
                              "https://mistralwinds.example.com/") == "internal"
    assert at.detect_platform("python-requests/2.31",
                              "https://chat.mistral.ai/") == "mistral"


def test_own_domain_referers_are_discarded():
    """/integrations/grok as a referer contains 'grok'. Counting a browser on
    our own Grok page as Grok traffic would be self-inflation wearing a
    platform's name."""
    assert at.detect_platform("python-requests/2.31",
                              "https://dchub.cloud/integrations/grok") == "internal"
    assert at.detect_platform("python-requests/2.31",
                              "http://localhost:8080/integrations/meta") == "internal"


def test_ua_only_named_platforms_unchanged():
    """The pre-existing contract: identifying UAs classify with no referer."""
    assert at.detect_platform("HuggingFace-Hub/0.20") == "huggingface"
    assert at.detect_platform("cohere-ai python client") == "cohere"


def test_backward_compatible_signature():
    """Every existing one-arg caller keeps working."""
    assert at.detect_platform("") == "direct"
    assert at.detect_platform("Mozilla/5.0 (bot crawler)") == "seo_bot"


def test_call_sites_thread_the_referer():
    src = open(at.__file__, encoding="utf-8").read()
    assert 'detect_platform(ua, request.headers.get("Referer", ""))' in src, \
        "the Flask after-request hook lost the referer"
    assert src.count('data.get("referer", "") or ""') >= 2, \
        "a POST tracking receiver lost the referer passthrough"
