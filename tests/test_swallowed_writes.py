"""routes/_swallowed_writes.py test suite (2026-07-11).

The guard exists because a bare `except: pass` around a broken INSERT kept
the brain's fix-verifier dead for weeks. Contract under test:
  1. counters increment per (where, table) site and snapshot cleanly
  2. logging is rate-limited: first hit logs, repeats stay quiet until the
     _LOG_EVERY-th (a schema mismatch fails on EVERY call — no log storms)
  3. it never raises — with or without an active exception
"""
import logging

import routes._swallowed_writes as sw


def setup_function(_fn):
    sw._reset_for_tests()


def test_counts_increment_per_site():
    for _ in range(3):
        sw.note_swallowed_write("brain_automerge_log",
                                where="brain_automerge.record_merge")
    sw.note_swallowed_write("media_master_snapshots",
                            where="media_master_shell._persist")
    counts = sw.swallowed_write_counts()
    assert counts["brain_automerge.record_merge:brain_automerge_log"] == 3
    assert counts["media_master_shell._persist:media_master_snapshots"] == 1


def test_logs_first_then_rate_limited(caplog):
    with caplog.at_level(logging.WARNING, logger="dchub.swallowed_write"):
        for _ in range(sw._LOG_EVERY):
            sw.note_swallowed_write("t1", where="w1")
    # first call logs, then quiet until the _LOG_EVERY-th
    hits = [r for r in caplog.records if "swallowed DB write" in r.getMessage()]
    assert len(hits) == 2
    assert "#1 " in hits[0].getMessage()
    assert f"#{sw._LOG_EVERY} " in hits[1].getMessage()


def test_captures_active_exception(caplog):
    with caplog.at_level(logging.WARNING, logger="dchub.swallowed_write"):
        try:
            raise ValueError("column does not exist")
        except Exception:
            sw.note_swallowed_write("brain_fix_outcomes",
                                    where="brain_learning.record_proposal_outcome")
    msg = caplog.records[0].getMessage()
    assert "ValueError" in msg
    assert "column does not exist" in msg


def test_never_raises_without_active_exception():
    # called outside any except block — still fine, still counted
    sw.note_swallowed_write("t2", where="w2")
    assert sw.swallowed_write_counts()["w2:t2"] == 1


def test_never_raises_on_garbage_args():
    sw.note_swallowed_write(None, where=None)
    sw.note_swallowed_write("", where="")
    assert sw.swallowed_write_counts()["?:?"] == 2
