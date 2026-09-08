"""worker.js's title line must not carry a version that disagrees with the const.

FENCES a drift that has now happened TWICE in the same file:

  * title read v4.9.30 for EIGHTEEN edits while WORKER_VERSION was 4.9.48
  * title read v4.9.55 while WORKER_VERSION was 4.9.62   (found 2026-09-08)

Both times the line's own comment asked a human to keep it in step by hand, and
both times that failed — a rule with nothing enforcing it is a wish. The title
line claims to describe the file as it stands, so a stale version there tells
someone comparing the Cloudflare dashboard against a file they were handed that
they are looking at the same code when they are not.

The fix was to REMOVE the version rather than re-sync it, leaving `const
WORKER_VERSION` as the single source. This guard does not mandate its absence —
someone may want the convenience back — it mandates that if a version appears
there it is the RIGHT one, which is the only property that ever mattered.

★ Scope matters here. The explanatory note directly beneath the title
deliberately QUOTES the stale versions (4.9.30, 4.9.48, 4.9.55, 4.9.62) as
evidence, and the CHANGES blocks below legitimately name the versions they
shipped in. A guard that scanned the whole header would fire on its own
explanation. This one reads exactly one line.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "worker.js")

TITLE_RE = re.compile(r"^ \* DC Hub API Proxy Worker\b.*$", re.M)
CONST_RE = re.compile(r"^const WORKER_VERSION = '([^']*)';", re.M)
# 4.9.62 / v4.9.62 / 4.9.62-og-card-body-not-spent
VERSION_RE = re.compile(r"\bv?(\d+\.\d+\.\d+(?:-[A-Za-z0-9-]+)?)")


def _read():
    with open(WORKER, encoding="utf-8") as fh:
        return fh.read()


def _title_line(src):
    hits = TITLE_RE.findall(src)
    assert len(hits) == 1, (
        f"expected exactly one ' * DC Hub API Proxy Worker' title line, found "
        f"{len(hits)}. Extraction drifted, so this guard would prove nothing: {hits}")
    return hits[0]


def _const_version(src):
    m = CONST_RE.search(src)
    assert m, ("no `const WORKER_VERSION = '...';` found — "
               "scripts/check_worker_version_bump.sh depends on that exact shape too")
    return m.group(1)


def test_title_line_does_not_state_a_version_that_disagrees_with_the_const():
    """THE regression. Twice this line has claimed a version the file was not."""
    src = _read()
    title = _title_line(src)
    const = _const_version(src)
    found = VERSION_RE.findall(title)
    for v in found:
        assert v == const, (
            f"worker.js title line says version {v!r} but WORKER_VERSION is "
            f"{const!r}.\n  title: {title.strip()}\n"
            "This is the third occurrence of a drift documented in this very "
            "file. Either drop the version from the title line (preferred — one "
            "source of truth) or make it match the const exactly.")


def test_the_guard_can_actually_see_the_title_line():
    """Fail-closed control: if extraction breaks, the test above passes
    vacuously by finding no versions in an empty string. Prove we read a real
    line that really is the header."""
    src = _read()
    title = _title_line(src)
    assert "DC Hub API Proxy Worker" in title
    assert src.index(title) < 400, (
        "the title line is no longer near the top of the file — re-check this guard")


def test_the_explanatory_note_and_changelog_are_out_of_scope():
    """Negative control. The note beneath the title QUOTES the stale versions on
    purpose and the CHANGES blocks name their own; a guard that fired on those
    would be unfixable. Assert those versions are present and tolerated."""
    src = _read()
    header = src[:src.index("*/")]
    assert "4.9.30" in header and "4.9.55" in header, (
        "the drift evidence was scrubbed out of the header note — it is the "
        "reason the version was removed from the title; keep it")
    # and the guard above still passes with them present
    test_title_line_does_not_state_a_version_that_disagrees_with_the_const()
