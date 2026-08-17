"""Tests for routes/brain_spec_debt.py — the spec-debt queue (Phase 0).

The queue's honesty rules are the whole point, so each one is pinned:
  · unchecked checklist  => OPEN
  · fully checked        => CLOSED
  · NO checklist         => UNKNOWN — never folded into closed (the
    mpp_challenge self-flattery class)
  · empty/missing corpus => UNMEASURED — never "zero debt"
Mutation targets (run on the real path, see the PR body for traces):
  reclassifying unknown->closed must turn this file red; returning zeros for
  an empty corpus must turn this file red.
"""
import os

from routes.brain_spec_debt import (CLOSED, OPEN, UNKNOWN, agenda_id_for,
                                    classify_doc_text, parse_filed_at,
                                    scan_corpus, spec_debt_summary)

OPEN_DOC = """# Brain proposal — something

_Filed 2026-07-13T21:53:08.575548Z · agenda #100094_

## Human checklist

- [ ] Confirm this is still worth doing
- [x] Scope it
"""

CLOSED_DOC = """# Brain proposal — done thing

_Filed 2026-07-01T00:00:00Z · agenda #100001_

- [x] Confirm this is still worth doing
- [X] Implement + verify
"""

NO_CHECKLIST_DOC = """# Brain proposal — a doc that never wrote its obligation

Some prose. No checklist anywhere.
"""


# ── classification: the three buckets ───────────────────────────────

def test_unchecked_checklist_is_OPEN_even_with_checked_siblings():
    assert classify_doc_text(OPEN_DOC) == OPEN


def test_fully_checked_checklist_is_CLOSED():
    assert classify_doc_text(CLOSED_DOC) == CLOSED


def test_no_checklist_is_UNKNOWN_not_closed():
    # Kills: the flattering reclassification (unknown -> closed). A doc that
    # never wrote its obligation cannot claim the obligation was met.
    assert classify_doc_text(NO_CHECKLIST_DOC) == UNKNOWN
    assert classify_doc_text(NO_CHECKLIST_DOC) != CLOSED


def test_empty_text_is_UNKNOWN():
    assert classify_doc_text("") == UNKNOWN


# ── filed-date + id parsing ─────────────────────────────────────────

def test_filed_stamp_parses_to_utc():
    dt = parse_filed_at(OPEN_DOC)
    assert dt is not None and dt.year == 2026 and dt.month == 7

def test_no_date_returns_None_never_epoch():
    assert parse_filed_at("no dates here at all") is None


def test_agenda_id_from_filename():
    assert agenda_id_for("agenda-100094-data-coverage.md") == 100094
    assert agenda_id_for("prop-7-data-coverage.md") == 7
    assert agenda_id_for("README.md") is None


# ── the scan: counts carry their basis; UNMEASURED is never zero ────

def _write(d, name, text):
    with open(os.path.join(d, name), "w") as fh:
        fh.write(text)


def test_scan_counts_all_three_buckets(tmp_path):
    d = str(tmp_path)
    _write(d, "agenda-1-open.md", OPEN_DOC)
    _write(d, "agenda-2-closed.md", CLOSED_DOC)
    _write(d, "agenda-3-unknown.md", NO_CHECKLIST_DOC)
    s = scan_corpus(d)
    assert s["state"] == "MEASURED"
    assert s["counts"] == {OPEN: 1, CLOSED: 1, UNKNOWN: 1, "total_docs": 3}
    assert s["basis"]["docs_scanned"] == 3
    assert "unchecked" in s["basis"]["derivation"]
    opens = s["open_obligations"]
    assert len(opens) == 1 and opens[0]["doc"] == "agenda-1-open.md"
    assert opens[0]["age_days"] is not None and opens[0]["age_days"] > 0
    assert opens[0]["unchecked_items"] == 1


def test_open_list_sorts_oldest_first_undated_last(tmp_path):
    d = str(tmp_path)
    _write(d, "agenda-1-old.md", OPEN_DOC.replace("2026-07-13", "2026-06-01"))
    _write(d, "agenda-2-new.md", OPEN_DOC)
    _write(d, "agenda-3-undated.md", "# t\n\n- [ ] do it\n")
    s = scan_corpus(d)
    docs = [r["doc"] for r in s["open_obligations"]]
    assert docs == ["agenda-1-old.md", "agenda-2-new.md",
                    "agenda-3-undated.md"]
    assert s["open_obligations"][2]["age_days"] is None


def test_missing_corpus_is_UNMEASURED_not_zero(tmp_path):
    s = scan_corpus(str(tmp_path / "does-not-exist"))
    assert s["state"] == "UNMEASURED"
    assert s["counts"] is None          # never a flattering zero
    assert s["open_obligations"] is None
    assert "unreadable" in s["reason"]


def test_empty_corpus_is_UNMEASURED_not_zero(tmp_path):
    s = scan_corpus(str(tmp_path))
    assert s["state"] == "UNMEASURED"
    assert s["counts"] is None
    assert "empty" in s["reason"] or "no .md" in s["reason"]


def test_summary_propagates_UNMEASURED(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_DEBT_CORPUS_DIR", str(tmp_path))
    out = spec_debt_summary()
    assert out["known"] is False
    assert out.get("open") is None  # absent/None — never 0


def test_summary_measured_shape(tmp_path, monkeypatch):
    _write(str(tmp_path), "agenda-9-open.md", OPEN_DOC)
    monkeypatch.setenv("SPEC_DEBT_CORPUS_DIR", str(tmp_path))
    out = spec_debt_summary()
    assert out["known"] is True
    assert out["open"] == 1 and out["closed"] == 0 and out["unknown"] == 0
    assert out["oldest_doc"] == "agenda-9-open.md"


# ── the real corpus, on this tree ───────────────────────────────────

def test_real_corpus_scan_is_measured_and_unknown_bucket_exists():
    """The deployed tree carries docs/brain-proposals/. The scan must be
    MEASURED there and every doc must land in exactly one bucket."""
    s = scan_corpus()
    assert s["state"] == "MEASURED"
    c = s["counts"]
    assert c["total_docs"] == c[OPEN] + c[CLOSED] + c[UNKNOWN]
    assert c["total_docs"] > 0


# ── class collapse must never lose a target (2026-08-17) ────────────────────
# 63 per-target docs were closed into 20 class canonicals. The whole risk of a
# class collapse is that a per-target finding gets buried, so each canonical
# carries a rolled-up roster naming every member and its target. This pins that
# the roster is complete for every doc closed against it.

def test_class_collapse_canonicals_enumerate_every_member():
    """Mutation: delete a roster line from any canonical -> red.

    ★ LIVE-CORPUS invariant, on purpose (audit 2026-08-17): the corpus IS the
    subject — a PR that drops a member from a canonical's roster (or breaks a
    member's pointer) goes red ITSELF, pre-merge. Do not seal this scan.

    No automated writer produces these markers: "class member of X.md (class
    collapse)" appears nowhere in routes/tools/scripts (verified 2026-08-17)
    — the collapse was the one-off 2026-08-16 spec-debt triage, done by hand.
    So a red here is a MANUAL edit slip, and it can only reach main around
    the PR gate (admin merge past red checks, or a direct push).

    If you are reading this because MAIN is red, repair the DOCS, never this
    test — the assertion message lists (member, why). Either:
      · add the member's filename to its canonical's rolled-up roster, or
      · fix the member's "class member of <file>.md" pointer to the canonical
        that actually rosters it (restore the canonical if it was deleted).
    """
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, "docs", "brain-proposals")
    if not os.path.isdir(d):
        import pytest
        pytest.skip("docs corpus absent in this environment")
    member_of = re.compile(
        r"class member of ([A-Za-z0-9_.\-]+\.md) \(class collapse\)")
    missing = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        body = open(os.path.join(d, name), encoding="utf-8",
                    errors="replace").read()
        m = member_of.search(body)
        if not m:
            continue
        canon = os.path.join(d, m.group(1))
        if not os.path.isfile(canon):
            missing.append((name, "canonical does not exist"))
            continue
        croster = open(canon, encoding="utf-8", errors="replace").read()
        if name not in croster:
            missing.append((name, f"absent from {m.group(1)} roster"))
    assert not missing, (
        "class collapse dropped targets — a closed member must appear in its "
        f"canonical's rolled-up roster: {missing[:5]}")
