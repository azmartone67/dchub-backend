"""/connect/chatgpt: a come-back-tomorrow nudge that is true, and Clarity's
day-1 -> day-2 events.

r-connect-return (2026-09-14). Rendered from the page's own template, the way
tests/test_connect_install_pages_derive_canon.py renders it, so every check
reads bytes that ship.

1. THE NUDGE is one line under the install step on /connect/chatgpt, and on no
   other install page.

2. "SAME KEY" HAS TO BE TRUE, and on the live page it was not, twice over:

   * ChatGPT's connector form has no header field, so "Header: X-API-Key" put
     the key nowhere ChatGPT sends it. The key now rides in the connector URL,
     which POST /mcp reads (?apiKey=).
   * MEASURED LIVE 2026-09-14 on every /connect/<client> page: the script ran
     RAW_SNIPPET.replaceAll("{TRIAL_KEY}", mintedKey) over a snippet holding
     "{{TRIAL_KEY}}". The sentinel had been typed into the .format() template,
     which halved its braces, so the swap left a brace on each side of the key
     and a copied snippet carried a key no server accepts. The sentinel now
     reaches the script as a format argument, and the swap is EXECUTED below
     on the constants the rendered page defines.
   * r-connect-bind (2026-09-15): past its free calls the same key serves
     only once an email is bound; unbound, /api/v1/keys/validate refuses it
     and the call is served anonymously. The nudge now says so, and the real
     validator is run below on both sides of that line.

3. CLARITY sees day 1 -> day 2: the page marks the UTC day it mints a key and
   fires connect_key_day2 when the same browser opens the page the next day.
   Only the client name and the day reach Clarity or localStorage.

Run:  python3 -m pytest tests/test_connect_return_nudge.py -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

import canonical_stats as cs
import routes.mcp_connect as mc

NUDGE = 'class="return-nudge"'
ALL_CLIENTS = sorted(mc._CLIENTS.keys())
OTHER_CLIENTS = [k for k in ALL_CLIENTS if k != "chatgpt"]
# Built, not typed: a key-shaped literal trips scripts/check_no_leaked_credentials.py.
KEY = "dch_trial_" + "x" * 32


@pytest.fixture
def stats_state():
    """Restore canonical_stats' module cache, which rendering reads."""
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _page(client="chatgpt"):
    return mc._render_page(client, 4242)


def _script(html):
    start = html.rindex("<script>") + len("<script>")
    return html[start:html.index("</script>", start)]


def _js_const(script, name):
    """The JSON literal a `const NAME = <json>;` line assigns, decoded."""
    m = re.search(r"^const " + name + r"\s*=\s*(.+);$", script, re.M)
    assert m, name + " is not defined on the page"
    return json.loads(m.group(1))


# ── 1. the nudge ─────────────────────────────────────────────────────────
def test_chatgpt_page_carries_the_nudge_once(stats_state):
    html = _page()
    assert html.count(NUDGE) == 1
    assert "Come back tomorrow: same key, new chat." in html
    assert "Once an email is bound to it, the key keeps working past its free calls." in html


def test_the_nudge_sits_under_the_install_snippet(stats_state):
    html = _page()
    assert html.index('id="snippet-body"') < html.index(NUDGE) < html.index("STEP 3")


@pytest.mark.parametrize("client", OTHER_CLIENTS)
def test_no_other_install_page_carries_it(client, stats_state):
    assert NUDGE not in _page(client)


def test_nudge_text_is_escaped(monkeypatch):
    monkeypatch.setitem(mc._CLIENTS["chatgpt"], "return_nudge", "a <b> & c")
    out = mc._return_nudge_html(mc._CLIENTS["chatgpt"])
    assert "a &lt;b&gt; &amp; c" in out and "<b>" not in out


# ── 2. "same key" is true ────────────────────────────────────────────────
def test_chatgpt_install_puts_the_key_in_the_connector_url():
    snippet = mc._CLIENTS["chatgpt"]["snippet"]
    assert "https://dchub.cloud/mcp?apiKey=" + mc.TRIAL_KEY_SENTINEL in snippet
    # ChatGPT cannot send a header, so the page must not tell anyone to.
    assert "X-API-Key" not in snippet


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_every_snippet_marks_the_key_with_the_sentinel_the_script_gets(client, stats_state):
    snippet = mc._CLIENTS[client]["snippet"]
    if "TRIAL_KEY" not in snippet:
        pytest.skip(client + " installs without a key")
    assert mc.TRIAL_KEY_SENTINEL in snippet
    script = _script(_page(client))
    assert _js_const(script, "KEY_SENTINEL") == mc.TRIAL_KEY_SENTINEL
    assert _js_const(script, "RAW_SNIPPET") == snippet


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_the_swap_leaves_exactly_the_key(client, stats_state):
    """Runs the page's own swap on the page's own constants. Before the fix
    the script's literal was the half-braced sentinel and this produced
    '{<key>}' on every page."""
    script = _script(_page(client))
    raw = _js_const(script, "RAW_SNIPPET")
    if mc.TRIAL_KEY_SENTINEL not in raw:
        pytest.skip(client + " installs without a key")
    m = re.search(r"RAW_SNIPPET\.replaceAll\(([A-Z_]+|\"[^\"]*\"), mintedKey\)", script)
    assert m, "the page no longer swaps the key into RAW_SNIPPET"
    needle = m.group(1)
    needle = json.loads(needle) if needle.startswith('"') else _js_const(script, needle)
    swapped = raw.replace(needle, KEY)
    assert KEY in swapped
    assert "{" + KEY not in swapped and KEY + "}" not in swapped
    assert mc.TRIAL_KEY_SENTINEL not in swapped
    if client == "chatgpt":
        assert "https://dchub.cloud/mcp?apiKey=" + KEY in swapped


class _TrialRow:
    """validate_trial_key's SELECT answered with one row; its UPDATE is a no-op."""

    def __init__(self, row):
        self.row, self.sql = row, ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return self.row if self.sql.lstrip().upper().startswith("SELECT") else None

    def close(self):
        pass


@pytest.mark.parametrize("operator_email, verdict", [
    (None, (False, "bind_email_required")),
    ("someone@example.invalid", (True, "ok")),
])
def test_past_its_free_calls_the_key_serves_only_once_an_email_is_bound(
        monkeypatch, operator_email, verdict):
    """The nudge's condition, run through the real validator: the same key,
    past its free unbound calls, is refused while unbound and served once bound."""
    import datetime

    import routes.auto_trial as at

    row = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3),
           None, operator_email, 0, None, at.TRIAL_FREE_CALLS_UNBOUND + 5)
    monkeypatch.setattr(at, "_conn", lambda: _TrialRow(row))
    assert at.validate_trial_key(KEY) == verdict


# ── 3. Clarity's day 1 -> day 2 ───────────────────────────────────────────
def test_mint_marks_day_one_once_the_key_exists(stats_state):
    script = _script(_page())
    ok = script.index("if (j && j.ok && j.api_key) {")
    assigned = script.index("mintedKey = j.api_key;", ok)
    marked = script.index("markKeyDay1();", assigned)
    assert marked < script.index("} else {", ok)


def test_the_return_check_runs_on_load_and_names_both_days(stats_state):
    script = _script(_page())
    assert re.search(r"\(function keyDayReturn\(\) \{.*\}\)\(\);", script, re.S)
    for event in ('"connect_key_day1"', '"connect_key_day2"', '"connect_key_return_later"'):
        assert event in script, event


def test_neither_clarity_nor_local_storage_is_handed_the_key(stats_state):
    script = _script(_page())
    calls = re.findall(r"(?:clarityTag|window\.clarity|localStorage\.setItem)\(([^;]*)\)", script)
    assert calls, "no Clarity or storage call found — the check below would be vacuous"
    for args in calls:
        assert "mintedKey" not in args and "api_key" not in args, args


def test_the_rendered_script_parses():
    if not shutil.which("node"):
        pytest.skip("node is not on PATH")
    script = _script(mc._render_page("chatgpt", 4242))
    r = subprocess.run(["node", "--check", "--input-type=commonjs"], input=script,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
