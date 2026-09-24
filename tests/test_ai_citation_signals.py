"""ai_citation_signals.classify — user fetch vs crawler vs human click-through.

Pins every UA token and every referral host / utm_source case, plus the
negatives that would inflate the one number meant to prove an assistant sent
a person to us: a normal Chrome visit from google.com, plain bing.com, our own
utm_source values, a bot carrying a chatgpt.com Referer, a subresource load.
"""
import pytest

import ai_citation_signals as acs  # noqa: E402 — plain import: a missing module must FAIL, not skip

CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
SAFARI_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
              "Mobile/15E148 Safari/604.1")


def _c(ua, referer="", query="", dest=None):
    r = acs.classify(ua, referer, query, dest)
    return r["signal_class"], r["source"], r["agent"]


# Real UA strings as the vendors publish them (or their documented shape).
UA_CASES = [
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
     "ChatGPT-User/1.0; +https://openai.com/bot",
     "user_fetch", "openai", "ChatGPT-User"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
     "Claude-User/1.0; +Claude-User@anthropic.com)",
     "user_fetch", "anthropic", "Claude-User"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
     "Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)",
     "user_fetch", "perplexity", "Perplexity-User"),
    ("meta-externalfetcher/1.1 (+https://developers.facebook.com/docs/"
     "sharing/webmasters/crawler)",
     "user_fetch", "meta", "Meta-ExternalFetcher"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko; "
     "compatible; Google-GeminiNotebook) Chrome/128.0.0.0 Safari/537.36",
     "user_fetch", "google", "Google-GeminiNotebook"),
    ("Google-NotebookLM", "user_fetch", "google", "Google-NotebookLM"),
    ("Mozilla/5.0 (compatible; Google-Agent) Chrome/128.0.0.0",
     "user_fetch", "google", "Google-Agent"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
     "OAI-SearchBot/1.4; +https://openai.com/searchbot",
     "search_crawler", "openai", "OAI-SearchBot"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
     "Claude-SearchBot/1.0; +Claude-SearchBot@anthropic.com)",
     "search_crawler", "anthropic", "Claude-SearchBot"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
     "PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
     "search_crawler", "perplexity", "PerplexityBot"),
    ("meta-webindexer/1.1 (+https://developers.facebook.com/docs/sharing/"
     "webmasters/crawler)", "search_crawler", "meta", "Meta-WebIndexer"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
     "GPTBot/1.4; +https://openai.com/gptbot",
     "training_crawler", "openai", "GPTBot"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
     "ClaudeBot/1.0; +claudebot@anthropic.com)",
     "training_crawler", "anthropic", "ClaudeBot"),
    ("anthropic-ai", "training_crawler", "anthropic", "anthropic-ai"),
    ("meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/"
     "webmasters/crawler)", "training_crawler", "meta", "Meta-ExternalAgent"),
    ("Google-Extended", "training_crawler", "google", "Google-Extended"),
    ("Applebot-Extended", "training_crawler", "apple", "Applebot-Extended"),
    ("CCBot/2.0 (https://commoncrawl.org/faq/)",
     "training_crawler", "commoncrawl", "CCBot"),
    ("Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@"
     "bytedance.com)", "training_crawler", "bytedance", "Bytespider"),
]


@pytest.mark.parametrize("ua,klass,src,agent", UA_CASES,
                         ids=[c[3] for c in UA_CASES])
def test_every_ua_token(ua, klass, src, agent):
    assert _c(ua) == (klass, src, agent)


def test_every_declared_token_is_pinned():
    """A token added to UA_TOKENS without a real-UA case above fails here."""
    assert {c[3] for c in UA_CASES} == set(acs.AGENTS)


def test_tokens_do_not_bleed_into_each_other():
    # The whole point of the split: the user-triggered and crawler tokens of
    # one vendor are different classes.
    assert _c("ClaudeBot/1.0")[0] == "training_crawler"
    assert _c("Claude-User/1.0")[0] == "user_fetch"
    assert _c("Claude-SearchBot/1.0")[0] == "search_crawler"
    assert _c("PerplexityBot/1.0")[0] == "search_crawler"
    assert _c("Perplexity-User/1.0")[0] == "user_fetch"
    assert _c("GPTBot/1.4")[0] == "training_crawler"
    assert _c("ChatGPT-User/1.0")[0] == "user_fetch"
    assert _c("OAI-SearchBot/1.4")[0] == "search_crawler"
    assert _c("Meta-ExternalAgent/1.1")[0] == "training_crawler"
    assert _c("Meta-ExternalFetcher/1.1")[0] == "user_fetch"


def test_token_match_is_bounded_not_substring():
    # 'GPTBot' inside another product name is not GPTBot.
    assert _c("MyGPTBotClone/1.0") == ("other", "none", "no_signal")
    assert _c("xCCBot/1.0") == ("other", "none", "no_signal")
    assert _c("Claude-Userland/2.0") == ("other", "none", "no_signal")


def test_user_fetch_ua_wins_over_utm():
    # ChatGPT fetching a cited URL that carries utm_source=chatgpt.com is the
    # assistant fetching, not a person clicking.
    assert _c("ChatGPT-User/1.0", "", "utm_source=chatgpt.com") == (
        "user_fetch", "openai", "ChatGPT-User")


REFERER_CASES = [
    ("https://chatgpt.com/", "openai"),
    ("https://chatgpt.com/c/66f0-abc", "openai"),
    ("https://chat.openai.com/", "openai"),
    ("android-app://com.openai.chatgpt/", "openai"),
    ("https://claude.ai/", "anthropic"),
    ("https://claude.ai/chat/abc", "anthropic"),
    ("android-app://com.anthropic.claude/", "anthropic"),
    ("https://www.perplexity.ai/", "perplexity"),
    ("https://perplexity.ai/search/xyz", "perplexity"),
    ("android-app://ai.perplexity.app.android/", "perplexity"),
    ("https://gemini.google.com/", "google"),
    ("https://bard.google.com/", "google"),
    ("https://copilot.microsoft.com/", "microsoft"),
    ("https://copilot.cloud.microsoft/", "microsoft"),
    ("https://edgeservices.bing.com/", "microsoft"),
    ("https://www.meta.ai/", "meta"),
]


@pytest.mark.parametrize("referer,src", REFERER_CASES,
                         ids=[c[0] for c in REFERER_CASES])
def test_referral_by_referer(referer, src):
    assert _c(CHROME, referer) == ("assistant_referral", src, "referer")
    assert _c(SAFARI_IOS, referer, dest="document") == (
        "assistant_referral", src, "referer")


UTM_CASES = [
    ("utm_source=chatgpt.com", "openai"),
    ("utm_source=chat.openai.com", "openai"),
    ("utm_source=claude.ai", "anthropic"),
    ("utm_source=perplexity.ai", "perplexity"),
    ("utm_source=perplexity", "perplexity"),
    ("utm_source=gemini.google.com", "google"),
    ("utm_source=copilot.microsoft.com", "microsoft"),
    ("?utm_source=ChatGPT.com&utm_medium=referral", "openai"),
    ("foo=1&utm_source=www.chatgpt.com", "openai"),
]


@pytest.mark.parametrize("query,src", UTM_CASES, ids=[c[0] for c in UTM_CASES])
def test_referral_by_utm_source(query, src):
    # ChatGPT appends utm_source=chatgpt.com to cited links; the Referer is
    # often stripped, so the query alone must be enough.
    assert _c(CHROME, "", query) == ("assistant_referral", src, "utm_source")


def test_referer_and_utm_together():
    assert _c(CHROME, "https://chatgpt.com/", "utm_source=chatgpt.com") == (
        "assistant_referral", "openai", "referer+utm_source")


# ── negatives ────────────────────────────────────────────────────────────

NEGATIVE_HUMAN = [
    ("https://www.google.com/", ""),          # organic search
    ("https://google.com/search?q=dchub", ""),
    ("https://www.bing.com/", ""),            # Bing SEARCH, not Copilot
    ("https://duckduckgo.com/", ""),
    ("https://dchub.cloud/markets", ""),      # our own site
    ("https://notchatgpt.com/", ""),          # host must match exactly
    ("https://chatgpt.com.evil.example/", ""),
    ("https://evilclaude.ai/", ""),
    ("", ""),                                 # direct
    ("", "utm_source=mcp"),                   # our own tags
    ("", "utm_source=meta-ai"),               # routes/phx_live.py campaign
    ("", "utm_source=mcp_upgrade&utm_medium=paywall"),
    ("", "utm_source=chatgpt"),               # not the value ChatGPT sends
    ("", "utm_medium=chatgpt.com"),           # wrong parameter
    ("", "ref=chatgpt.com"),
]


@pytest.mark.parametrize("referer,query", NEGATIVE_HUMAN,
                         ids=[f"{r}|{q}" for r, q in NEGATIVE_HUMAN])
def test_normal_browser_traffic_is_not_a_referral(referer, query):
    assert _c(CHROME, referer, query) == ("other", "none", "no_signal")


def test_googlebot_and_bingbot_are_not_ai_signals():
    gb = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    bb = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
    assert _c(gb) == ("other", "none", "no_signal")
    assert _c(bb) == ("other", "none", "no_signal")


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "python-requests/2.31",
    "curl/8.4.0",
    "Mozilla/5.0 HeadlessChrome/128.0.0.0",
    "dchub-brain-probe/1.0",
    "",
])
def test_non_human_ua_with_assistant_referer_is_other(ua):
    assert _c(ua, "https://chatgpt.com/") == ("other", "openai", "bot_ua")
    assert _c(ua, "", "utm_source=claude.ai") == ("other", "anthropic", "bot_ua")


@pytest.mark.parametrize("dest", ["image", "script", "style", "empty", "iframe"])
def test_subresource_from_assistant_page_is_not_a_click(dest):
    # chatgpt.com rendering our og:image is not a person landing on a page.
    assert _c(CHROME, "https://chatgpt.com/", dest=dest) == (
        "other", "openai", "non_document")


def test_our_probe_with_a_bot_token_stays_out():
    assert _c("dchub-probe ChatGPT-User/1.0")[0] == "other"


def test_classify_never_raises_and_is_bounded():
    for args in [(None, None, None, None), ("x" * 10000, "::::", "%%%", 7),
                 (CHROME, "http://[::1", "utm_source=%ZZ", "document")]:
        r = acs.classify(*args)
        assert r["signal_class"] in acs.CLASSES
        assert r["source"] in acs.SOURCES
        assert r["agent"] in (acs.AGENTS + acs.REFERRAL_AGENTS
                              + acs.OTHER_AGENTS)


def test_every_source_value_is_declared():
    produced = ({s for _, _, s in acs.UA_TOKENS}
                | set(acs.REFERRAL_HOSTS.values())
                | set(acs.REFERRAL_UTM.values()))
    assert produced <= set(acs.SOURCES)


# ── landing paths (bounded) ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("/markets/ashburn?utm_source=chatgpt.com", "/markets/ashburn"),
    ("/markets/ashburn/", "/markets/ashburn"),
    ("/", "/"),
    ("", "/"),
    ("/facilities/x#frag", "/facilities/x"),
    ("/a b", acs.UNPARSEABLE_PATH),
    ("/<script>", acs.UNPARSEABLE_PATH),
    ("markets", acs.UNPARSEABLE_PATH),
])
def test_normalize_landing_path(raw, want):
    assert acs.normalize_landing_path(raw) == want


def test_landing_path_is_capped():
    assert len(acs.normalize_landing_path("/" + "a" * 5000)) == acs.PATH_MAX


def test_row_for_stores_paths_only_for_referral_and_user_fetch():
    ref = acs.classify(CHROME, "https://claude.ai/")
    uf = acs.classify("Claude-User/1.0")
    tc = acs.classify("ClaudeBot/1.0")
    assert acs.row_for(ref, "/pricing?x=1")[4] == "/pricing"
    assert acs.row_for(uf, "/answers/q")[4] == "/answers/q"
    assert acs.row_for(tc, "/answers/q")[4] == ""


def test_record_is_fail_soft():
    calls = []

    def boom(sql, params=None):
        calls.append(sql)
        raise RuntimeError("db down")

    acs._ddl_done = False
    assert acs.record(acs.classify("GPTBot/1.4"), "/x", boom) is False
    assert calls


def test_record_writes_ddl_once_then_upserts():
    calls = []
    acs._ddl_done = False
    for _ in range(3):
        assert acs.record(acs.classify("GPTBot/1.4"), "/x",
                          lambda s, p=None: calls.append((s, p))) is True
    assert sum(1 for s, _ in calls if "CREATE TABLE" in s) == 1
    ups = [p for s, p in calls if "ON CONFLICT" in s]
    assert len(ups) == 3
    assert ups[0][1:5] == ("training_crawler", "openai", "GPTBot", "")


def test_fold_window_keeps_every_class_key():
    w = acs.fold_window([
        {"signal_class": "user_fetch", "source": "openai",
         "agent": "ChatGPT-User", "hits": 3},
        {"signal_class": "assistant_referral", "source": "openai",
         "agent": "utm_source", "hits": 2},
        {"signal_class": "user_fetch", "source": "anthropic",
         "agent": "Claude-User", "hits": 1},
    ])
    assert set(w["by_class"]) == set(acs.CLASSES)
    assert w["total"] == 6
    assert w["by_class"]["user_fetch"] == 4
    assert w["by_class_source"]["user_fetch"] == {"openai": 3, "anthropic": 1}
    assert w["by_class"]["training_crawler"] == 0
    assert w["by_agent"]["utm_source"] == 2


def test_top_paths_sql_refuses_unbounded_class():
    with pytest.raises(ValueError):
        acs.top_paths_sql("training_crawler", 7)
    assert "LIMIT 20" in acs.top_paths_sql("assistant_referral", 7)
