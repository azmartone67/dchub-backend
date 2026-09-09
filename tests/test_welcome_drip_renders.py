"""Every drip template must RENDER — not merely contain the right tokens.

r-substation-canon (2026-09-09). The day-3 subject and stat box claimed a
three-digit substation count against a live COUNT(*) of ~127,000 — wrong by
roughly 208x, in a claim mailed to every new signup.

The fix is not "swap the number", it is "stop hardcoding it". But the obvious
swap — dropping a {canon_substations} token in — would have been WORSE than
the stale number, and that is what this guard exists to prove:

  * Only `day0_welcome` wraps its html in canon_text(). `day3_value` and
    `day7_offer` are plain strings.
  * Subjects are NEVER canon-wrapped, and _send_drip does
    template['subject'].format(name=name).
  * So an unresolvable token in a subject or in an unwrapped body raises
    KeyError at SEND time — a broken email, not a stale one.

Rendering each template end to end is the only check that catches that. A test
asserting "the token is present" would pass on exactly the broken version.
"""
import re

import pytest

import welcome_emails as we


TEMPLATE_KEYS = sorted(we.EMAILS.keys())

# Mirror the REAL call site (welcome_emails.py:472) exactly. Rendering with
# fewer kwargs than production passes invents failures that cannot happen —
# an earlier version of this file "found" a day7_convert bug that was purely
# an artefact of omitting signup_date.
_CALLER_KWARGS = {"name": "Larry", "signup_date": "September 08, 2026"}


def test_there_are_templates_to_check():
    """FLOOR. Every assertion below is vacuous on an empty EMAILS dict."""
    assert len(TEMPLATE_KEYS) >= 3, f"only {len(TEMPLATE_KEYS)} templates found"


@pytest.mark.parametrize("key", TEMPLATE_KEYS)
def test_subject_formats_without_error(key):
    """_send_drip calls template['subject'].format(name=name)."""
    subject = we.EMAILS[key]["subject"]
    try:
        rendered = subject.format(name="Larry")
    except KeyError as e:
        pytest.fail(
            f"{key} subject carries an unresolvable placeholder {e}. Subjects are "
            f"never passed through canon_text, so a canon token here raises at "
            f"send time and the email never goes out."
        )
    assert "{" not in rendered, f"{key} subject still has an unrendered brace: {rendered}"


@pytest.mark.parametrize("key", TEMPLATE_KEYS)
def test_body_renders_with_no_unresolved_placeholders(key):
    html = we.EMAILS[key]["html"]
    try:
        out = we._render(html, **_CALLER_KWARGS)
    except KeyError as e:
        pytest.fail(
            f"{key} body carries an unresolvable placeholder {e}. Only day0 is "
            f"canon_text-wrapped; the rest go straight to .format(), so the token "
            f"must be injected by _render or it raises at send time."
        )
    leftovers = re.findall(r"\{[a-z_][a-z0-9_]*\}", out)
    assert not leftovers, f"{key} rendered with unresolved placeholders: {set(leftovers)}"


def test_substation_count_is_not_hardcoded():
    """The claim must come from canon, not a literal."""
    src = open(we.__file__, encoding="utf-8").read()
    # Floor: prove the scan sees the file and the canon plumbing is present.
    assert "_CANON_SUBSTATIONS" in src, "the canon value is gone — guard aimed at a dead target"

    body = we._render(we.EMAILS["day3_value"]["html"], **_CALLER_KWARGS)
    subject = we.EMAILS["day3_value"]["subject"].format(name="Larry")
    stale = re.compile(r"\b612\+?\b")
    assert not stale.search(body), "the rendered day-3 body still shows the stale count"
    assert not stale.search(subject), "the day-3 subject still shows the stale count"
