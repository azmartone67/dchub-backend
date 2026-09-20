#!/usr/bin/env python3
"""/connect must name the REST key endpoint, not only the MCP tool.

NO NETWORK, NO DB.

The page told every reader to call the `claim_free_key` MCP tool and never
named `POST /api/v1/keys/claim`. That is fine for a chat client and useless for
a partner integrating over the OpenAPI spec, which is how agent catalogues
generate connectors — they never speak MCP. The curated spec publishes the
endpoint; this is the human half, and /connect is the URL our partner
correspondence points developers at.

The IP-metering caveat is pinned too, and deliberately. Measured live
2026-09-19: a key claimed without an email draws on an allowance metered per
SOURCE IP that carries across re-mints, so a hosted integration proxying many
end users through one address exhausts it and every later key arrives already
gated. Passing `email` on the same POST lifts it. An integrator who reads only
"one POST, no email" and ships it hits this on their second user.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "static" / "connect.html"


def _html():
    """The page WITHOUT its HTML comments.

    An early revision of this test passed while the curl block was deleted,
    because the comment above that block quotes the endpoint it documents —
    the string survived in text no reader ever sees. Assert against what is
    served to a human, never against a comment about it.
    """
    raw = PAGE.read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S)


def test_connect_names_the_rest_claim_endpoint():
    html = _html()
    assert "/api/v1/keys/claim" in html, (
        "/connect never names the REST key endpoint — a partner integrating "
        "over the spec has no route to a key from the page we point them at"
    )


def test_connect_shows_the_header_the_key_is_sent_as():
    """A key with no instruction for using it is half an answer."""
    html = _html()
    idx = html.find("/api/v1/keys/claim")
    assert idx != -1
    nearby = html[idx:idx + 1600]
    assert "X-API-Key" in nearby, (
        "the REST claim block does not say to send the key as X-API-Key"
    )


def test_connect_warns_that_the_free_allowance_meters_per_ip():
    """The fact a hosted integrator loses a day to."""
    html = _html().lower()
    assert "per <strong>source ip</strong>" in html or "per source ip" in html, (
        "/connect does not say the unbound allowance meters per source IP"
    )


def test_connect_names_the_remedy_not_just_the_trap():
    """Naming a trap without its fix sends the reader away with a problem."""
    html = _html()
    idx = html.find("/api/v1/keys/claim")
    nearby = html[idx:idx + 2200]
    assert re.search(r'pass\s+<code>"email"</code>', nearby), (
        "the IP-metering warning does not name passing `email` on the same "
        "POST as the fix"
    )
