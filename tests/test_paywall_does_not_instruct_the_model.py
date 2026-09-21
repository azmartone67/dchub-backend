#!/usr/bin/env python3
"""Our paywall body must describe, not instruct.

NO NETWORK, NO DB.

A catalogue partner, 2026-09-20:

    "Your 403 body contains fields addressed to the model rather than to the
     operator ... a for_your_human object whose 'why' tells the agent to relay
     a URL verbatim and not to summarise it away. Our engine passes an upstream
     error body through to the caller, so all of that reaches the agent of
     every customer who installs the connector ... a connector that pipes
     vendor-authored directives into a customer's agent is a thing we will look
     at closely, including on our own catalogue. Yours is the first where it
     came up."

They were not asking us to change it. We changed it anyway — being the first
example of that category is not a position worth defending.

Everything TRUE survives: the URL, that it is durable, that redeeming a trial
key does not consume it, which plan is needed. What goes is the imperative
mood aimed at someone else's model.

★ Asserted on the RESPONSE VALUES, never on the source. A source scan would be
satisfied by the comment that explains this very change, which necessarily
quotes the language it removed.
"""
import re

import pytest

# Imperatives directed at the reading model. Matched against what we SEND.
_DIRECTIVES = (
    r"\bdo not (?:fetch|follow|summaris|summariz)",
    r"\bshow (?:the|this) url to your user\b",
    r"\bshow them this link verbatim\b",
    r"\byou must (?:render|relay|show)\b",
    r"\brender (?:this )?verbatim\b",
)


@pytest.fixture
def body(monkeypatch):
    import mcp_signal_canonical
    import routes.pair_code as pair_code
    import utils.paywall_response as pr
    from flask import Flask

    monkeypatch.setattr(pair_code, "get_or_create_code",
                        lambda *a, **k: {"code": "DCM-TEST", "expires_at": None})
    monkeypatch.setattr(mcp_signal_canonical, "_compute_caller_id",
                        lambda **k: "anon:test")
    app = Flask(__name__)
    with app.test_request_context("/api/v1/pipeline",
                                  headers={"User-Agent": "t"}):
        return pr.build_paywall_response(tool_name="pipeline", user_id=None)


def _all_text(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _all_text(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _all_text(v, out)
    return out


@pytest.mark.parametrize("pattern", _DIRECTIVES)
def test_no_field_we_send_instructs_the_reading_model(body, pattern):
    hits = [t[:160] for t in _all_text(body) if re.search(pattern, t, re.I)]
    assert not hits, (
        f"the paywall body issues an instruction to the consuming model "
        f"({pattern!r}); it reaches the agent of every customer of any "
        f"catalogue that relays our error bodies:\n  " + "\n  ".join(hits)
    )


def test_the_useful_facts_survived(body):
    """Stripping the imperatives must not strip the substance — otherwise the
    next person restores the old wording to get the information back."""
    fyh = body.get("for_your_human") or {}
    why = (fyh.get("why") or "").lower()
    assert fyh.get("url"), "the human upgrade URL is gone"
    assert "person" in why, "no longer says who the link is for"
    assert "durable" in why, "no longer says the link is durable"
    assert "consume" in why, "no longer says a trial key does not consume it"


# ── the two claims a partner put in their public catalogue copy ──────────────

def test_the_hint_block_does_not_hardcode_a_calls_per_day_number():
    """"Anonymous 5/day · Free key 10/day" were two hand-typed literals, and a
    partner published the 5. Both must resolve from tier_registry so the copy
    cannot disagree with the ladder."""
    import re as _re
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routes" / "paywall_hint_middleware.py").read_text(encoding="utf-8")
    src = _re.sub(r"#.*?$", "", src, flags=_re.M)      # comments quote the old copy
    for key in ("pricing_quick", "what_you_get"):
        i = src.index(f'"{key}":')
        block = src[i:i + 500]
        literals = _re.findall(r"(?<![\w.'])(\d+)/day", block)
        assert not literals, (
            f"{key} hardcodes {literals} calls/day; resolve it with "
            f"_tr.calls_per_day(...) so it tracks the registry"
        )


def test_the_free_key_is_not_described_as_needing_a_signup():
    """The one property the partner featured us for is that the free key needs
    no email. The block agents read most said "email signup"."""
    import routes.paywall_hint_middleware as m
    text = m._HINT_BASE["what_you_get"].lower()
    assert "email signup" not in text, (
        "what_you_get still claims the free dev key requires an email signup — "
        "it requires one POST and no email"
    )
    assert "no email" in text, "the no-email property is no longer stated"
