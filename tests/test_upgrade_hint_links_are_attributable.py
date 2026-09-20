#!/usr/bin/env python3
"""_upgrade_hint must not hand an agent an unattributable purchase link.

NO NETWORK, NO DB.

#4872 attributed the structured paywall fields and #4886 the prose. Measured
live after BOTH shipped, a gated payload still carried exactly two Stripe links
with no client_reference_id, and a recursive walk found them nested where the
earlier top-level scans never looked:

    ref=NO   _upgrade_hint.starter_url
    ref=NO   _upgrade_hint.developer_url

They came from `_HINT_BASE`, a module-level dict that hardcoded bare
buy.stripe.com URLs. An agent that surfaces `starter_url` — and that field
exists precisely so an agent can hand its human a quick-buy URL — produced a
sale with nothing to bridge.

Two defences, tested separately:
  1. the static default names no Stripe link at all
  2. when the response body carries this caller's attributed URLs, the hint
     inherits them, so it points at the SAME destination as the rest of the
     response and carries the same reference
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "paywall_hint_middleware.py"


def _src():
    """Source WITHOUT comments — an earlier guard of mine passed because the
    comment explaining the fix quoted the very string it forbade."""
    raw = SRC.read_text(encoding="utf-8")
    return re.sub(r"#.*?$", "", raw, flags=re.M)


def test_the_static_hint_names_no_stripe_link():
    """_HINT_BASE is static — it can carry no per-caller reference, so it must
    not carry a Stripe URL either."""
    links = re.findall(r"https://buy\.stripe\.com/\S+", _src())
    assert not links, (
        f"_upgrade_hint still hardcodes bare Stripe link(s): {links} — point "
        f"them at /checkout/start, which is attributable by the session it "
        f"creates on arrival"
    )


def test_the_static_defaults_are_dchub_checkout_urls():
    src = _src()
    for key in ("starter_url", "developer_url"):
        m = re.search(rf'"{key}":\s*"([^"]+)"', src)
        assert m, f"{key} missing from _HINT_BASE"
        assert m.group(1).startswith("https://dchub.cloud/checkout/start"), (
            f"{key} default is {m.group(1)!r}, which keeps no session to bridge"
        )


def test_the_hint_inherits_the_bodys_attributed_urls():
    """The body's URLs already hold THIS caller's pair code. Minting again, or
    ignoring them, would make the hint disagree with the response around it."""
    src = _src()
    assert '("starter_url", "recommended_upgrade_url")' in src, (
        "starter_url does not inherit the body's attributed recommended URL"
    )
    assert '("developer_url", "one_click_upgrade_url")' in src, (
        "developer_url does not inherit the body's attributed one-click URL"
    )
    # and the override must land AFTER the base spread, or it does nothing
    base_at = src.index("**_HINT_BASE")
    over_at = src.index("**_hint_over")
    assert base_at < over_at, (
        "_hint_over is spread before _HINT_BASE, so the defaults win and the "
        "inheritance is dead code"
    )


def test_only_dchub_urls_are_inherited():
    """A body value that is itself a raw Stripe link must not be copied in."""
    src = _src()
    assert 'startswith("https://dchub.cloud/")' in src, (
        "the inheritance does not check the body URL is a DC Hub URL, so a raw "
        "Stripe link in the body would be propagated into the hint"
    )
