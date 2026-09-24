"""No trial keys for AI fetchers/crawlers (r-no-keys-for-ai-crawlers, 2026-09-24).

Measured (auto_trial_keys, last 24h, owner's read-only query): OAI-SearchBot
minted 169 keys across 62 IPs and 129 REST paths, YouBot 8 — one key per fetch,
never presented again. mint_trial_for_request's bot list predates the named AI
agents; it now also skips every UA carrying a CRAWLER token (search or
training class) from the citation classifier's maintained list
(ai_citation_signals.UA_TOKENS), plus YouBot. They still get the anonymous
data; they get no key. User-fetch agents (Claude-User = the Messages-API MCP
connector, ChatGPT-User = a person asking ChatGPT) keep getting keys.

Guards: every crawler token is skipped with nothing written; every user-fetch
token and real agent UAs (including one named "...-bot") still mint.
"""
import pytest

import ai_citation_signals as acs
from tests.test_mint_scan_guard import _mint, _row

REAL_UAS = {
    "OAI-SearchBot": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36; compatible; "
                     "OAI-SearchBot/1.4; +https://openai.com/searchbot",
    "YouBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; YouBot/1.0; "
              "+https://docs.you.com/youbot; env:prod) Chrome/142.0.0.0 Safari/537.36",
}


CRAWLER_TOKENS = [t for t, k, _ in acs.UA_TOKENS if k in acs.CRAWLER_CLASSES]
USER_FETCH_TOKENS = [t for t, k, _ in acs.UA_TOKENS if k == acs.USER_FETCH]


def test_the_split_covers_the_measured_agents():
    assert "OAI-SearchBot" in CRAWLER_TOKENS and "GPTBot" in CRAWLER_TOKENS
    assert "Claude-User" in USER_FETCH_TOKENS and "ChatGPT-User" in USER_FETCH_TOKENS


@pytest.mark.parametrize("token", CRAWLER_TOKENS)
def test_every_crawler_token_gets_no_key(token):
    out, cur = _mint(_row(), headers={"User-Agent": f"Mozilla/5.0 (compatible; {token}/1.0)"})
    assert out == {"ok": False, "reason": "bot_skip", "bot": True}, (token, out)
    assert not cur.inserted


@pytest.mark.parametrize("token", USER_FETCH_TOKENS)
def test_a_user_fetch_agent_still_gets_a_key(token):
    """Claude-User is the Messages-API MCP connector; ChatGPT-User is a person
    asking ChatGPT. A human is behind both."""
    out, cur = _mint(_row(), headers={"User-Agent": f"{token}/1.0"})
    assert out.get("ok") is True, (token, out)
    assert cur.inserted


@pytest.mark.parametrize("name", sorted(REAL_UAS))
def test_the_measured_live_uas_get_no_key(name):
    out, cur = _mint(_row(), headers={"User-Agent": REAL_UAS[name]})
    assert out.get("reason") == "bot_skip", (name, out)
    assert not cur.inserted


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Claude/2.7032.0 Chrome/152.0.7977.130 Safari/537.36",      # Claude desktop app
    "node", "curl/8.7.1", "python-requests/2.34.2",
    "acme-siting-bot/2.0",                                       # a real agent named *-bot
    "Cursor/1.2", "Grok/1.0",
])
def test_real_agent_uas_still_mint(ua):
    out, cur = _mint(_row(), headers={"User-Agent": ua})
    assert out.get("ok") is True, (ua, out)
    assert cur.inserted


def test_ai_agent_for_is_the_classifier_lookup():
    assert acs.ai_agent_for("x OAI-SearchBot/1.4 y")[0] == "OAI-SearchBot"
    assert acs.ai_agent_for("ChatGPT-User/1.0")[1] == acs.USER_FETCH
    assert acs.ai_agent_for("Mozilla/5.0 Chrome/131") is None
    assert acs.ai_agent_for("") is None and acs.ai_agent_for(None) is None
