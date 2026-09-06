"""Both enterprise notifiers must treat public input as hostile — in the
BODY and in the HEADERS.

The body was escaped in both files and already covered by
tests/test_enterprise_inquiry_notify_escapes.py and
tests/test_enterprise_contact_request_level.py. The SUBJECT was not:

    f"New enterprise inquiry — {payload.get('org_name') or '?'}"

org_name is public form input and a subject is a mail header. The sibling
notifier strips CR/LF for exactly that reason and this one did not — the same
class of input, two files, two different standards.

★ THE OTHER HALF OF THIS CHANGE IS A COMMENT. routes/enterprise.py carried a
docstring stating that the sibling "interpolates the same class of input RAW —
that is a live injection into the admin inbox and is not fixed here." The
sibling was fixed on 2026-09-05; the claim outlived the defect by a day and
was still being read as current. A stale assertion that a vulnerability is
OPEN is its own defect: it sends the next reader hunting for a bug that is not
there. Pinned below so the claim cannot come back without the code.
"""
import inspect
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import enterprise as ent  # noqa: E402
from routes import enterprise_inquiry as inq  # noqa: E402

_HOSTILE = "Acme\r\nBcc: attacker@evil.com"


# ── headers ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,why", [
    (_HOSTILE, "CRLF starts a new mail header"),
    ("Acme\nX-Priority: 1", "bare LF is enough on some transports"),
    ("Acme\rX", "bare CR too"),
])
def test_header_sanitiser_removes_line_breaks(raw, why):
    out = ent._hdr(raw)
    assert "\r" not in out and "\n" not in out, why
    assert "Acme" in out, "it must not simply blank the field"


def test_the_subject_is_built_through_the_sanitiser():
    """Bound to the call, not to a rendered string: _notify_sales needs a live
    Resend key and main import to run, so pin the construction."""
    src = inspect.getsource(ent._notify_sales)
    i = src.index("New enterprise inquiry")
    before = src[:i]
    assert before.rstrip().endswith("_hdr(f\"") or "_hdr(f\"New enterprise" in src, (
        "the subject is interpolated without the header sanitiser:\n"
        + src[max(0, i - 160):i + 90])


def test_a_long_org_name_cannot_run_away_with_the_subject():
    assert len(ent._hdr("A" * 5000)) <= 200


def test_both_notifiers_sanitise_headers_the_same_way():
    """★ The defect was two files applying different standards to the same
    input. Pin that they agree, so fixing one and not the other fails here."""
    for fn in (ent._hdr,):
        assert fn(_HOSTILE) == "Acme  Bcc: attacker@evil.com"
    sib = inspect.getsource(inq._notify_admin)
    assert "def _hdr(" in sib, "the sibling lost its header sanitiser"
    assert "_subject = _hdr(" in sib, "the sibling stopped using it"


# ── bodies (both files, one assertion each) ───────────────────────────

def test_the_contact_body_escapes_markup():
    h = ent._sales_email_html({"org_name": "<script>alert(1)</script>",
                                "email": "a@b.c", "use_case": "x"})
    assert "<script>" not in h
    assert "&lt;script&gt;" in h, (
        "escaped output must still CONTAIN the text — asserting 'alert' is "
        "absent would pass on a body that dropped the field entirely")


def test_the_inquiry_notifier_still_escapes_every_field():
    src = inspect.getsource(inq._notify_admin)
    assert "_html.escape" in src
    for field in ("firm", "tier_requested", "name", "email", "use_case"):
        assert f"_t('{field}')" in src, f"{field} is no longer escaped"


# ── the stale claim ───────────────────────────────────────────────────

def test_no_source_comment_still_calls_the_sibling_injection_live():
    """★ The comment outlived the bug. It read as current for a day after the
    fix landed, asserting the product ships a known injection into the admin
    inbox. Checked across both files."""
    for mod in (ent, inq):
        src = open(mod.__file__, encoding="utf-8").read()
        low = " ".join(src.split()).lower()
        for claim in ("is a live injection into the admin inbox and is not fixed",
                      "interpolates the same class of input raw — that is a live"):
            assert claim.lower() not in low, (
                f"{os.path.basename(mod.__file__)} still asserts the sibling is "
                f"unfixed: {claim!r}")


def test_the_correction_records_what_was_true_and_when():
    """Not merely deleted — a reader of the old claim needs to find its
    retraction, or they will re-add it."""
    src = open(ent.__file__, encoding="utf-8").read()
    assert "NO LONGER TRUE" in src
    assert "2026-09-05" in src, "the correction does not date the fix"


def test_no_unused_escaper_advertises_coverage_it_does_not_provide():
    """_a() was defined and never called. An escaper nothing routes through
    reads as though that context is handled."""
    src = inspect.getsource(inq._notify_admin)
    if "def _a(" in src:
        assert src.count("_a(") > 1, (
            "_a() is defined but never called — either route an attribute "
            "through it or drop it")
