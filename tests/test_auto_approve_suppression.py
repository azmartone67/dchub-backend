"""discovery_auto_approve must not suppress a facility without evidence.

Traced 2026-07-28. Two sites ran a bare
    UPDATE discovered_facilities SET is_duplicate = 1 WHERE id = %s
with no dedup_method, no duplicate_of_id, no merged_at. That absence is the
signature: 861 rows carry it, 764 written in a single day, and 291 of the 311
keeperless groups repair_dedup_keeper_election.py had to fix came from it.

The reason was never actually missing -- it was written to discovery_approvals
(492 name_match rows WITH promoted_facility_id) which nothing downstream reads.
Every consumer reads discovered_facilities, where the flag looked arbitrary.

pytest functions only -- no module-scope work or exit.
"""
import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "discovery_auto_approve.py"


def _code():
    """Source minus comment lines -- comments satisfy grep."""
    return "\n".join(ln for ln in SRC.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


def test_no_bare_is_duplicate_write_remains():
    """Every suppression must say WHY, on the row."""
    code = _code()
    bare = re.findall(
        r"SET is_duplicate = 1\s+WHERE", code)
    assert not bare, (
        "a suppression with no dedup_method is indistinguishable from a legacy "
        "unexplained flag ({} site(s))".format(len(bare)))


def test_every_suppression_stamps_a_dedup_method():
    code = _code()
    sup = re.findall(r"SET is_duplicate = 1", code)
    stamps = re.findall(r"dedup_method = 'auto_approve/", code)
    assert len(stamps) == len(sup), (
        "{} suppression(s) but {} dedup_method stamp(s)".format(
            len(sup), len(stamps)))


def test_generic_exception_no_longer_suppresses():
    """A timeout or dropped connection must never mark a facility duplicate.

    Zero of the 'unique_constraint' rows carry a promoted_facility_id, because
    there was no match -- the label was a story about the failure.
    """
    code = _code()
    assert "_is_dupe_err" in code, "the handler must classify the exception"
    seg = code.split("except Exception as _promote_err:", 1)[1][:2200]
    i_guard = seg.find("if not _is_dupe_err")
    i_flag = seg.find("SET is_duplicate = 1")
    assert i_guard != -1 and i_flag != -1
    assert i_guard < i_flag, (
        "the non-duplicate exit must come BEFORE any suppression")
    assert "continue" in seg[i_guard:i_flag], (
        "a non-uniqueness error must bail out, not fall through to the flag")


def test_duplicate_detection_uses_the_driver_not_a_string_match():
    """Matching on the exception TEXT would re-break on a driver or locale
    change; UniqueViolation / SQLSTATE 23505 is the stable signal."""
    code = _code()
    seg = code.split("except Exception as _promote_err:", 1)[1][:2200]
    assert "UniqueViolation" in seg
    assert "23505" in seg, "keep the SQLSTATE fallback for wrapped drivers"
    assert "str(_promote_err)" not in seg.split("if not _is_dupe_err")[0], (
        "must not classify on the exception's text")


def test_the_error_is_still_reported_not_swallowed():
    code = _code()
    seg = code.split("except Exception as _promote_err:", 1)[1][:2200]
    assert "stats['errors']" in seg, (
        "a genuine promote failure must count as an error, not vanish")


def test_name_match_does_not_write_a_cross_id_space_pointer():
    """match_id is a `facilities` id; duplicate_of_id refers to
    discovered_facilities. Assigning across those id spaces would point at an
    unrelated row -- the documented two-id-spaces trap."""
    code = _code()
    seg = code.split("'duplicate_skipped', 'name_match'", 1)[1][:1600]
    assert "duplicate_of_id" not in seg, (
        "do not set duplicate_of_id from match_id -- different id space")
