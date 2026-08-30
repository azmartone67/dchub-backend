"""Human contact must be recordable for EVERY payer, not just founding ones.

★2026-08-30. `human_contacted_at` read only founding_customers.contacted_at, so
contact could only ever be RECORDED for someone already in that table.

Measured that day against production: founding_customers held 18 rows and
contained NONE of the four oldest stranded payers — 3 pro + 1 founding, paid
~110 days, zero calls by any path, all welcomed and nudged. Their
human_contacted_at read NULL and the board rendered "no human contact ever".

That was true BY ACCIDENT. The field was unwritable for them, so it would have
read NULL even after a phone call. ★"never contacted" and "cannot be shown as
contacted" are different facts, and the old schema could not tell them apart —
the same class as `drift_detected=FALSE` meaning "I could not look".

Guarded here: the column is ensured, BOTH sources are read, and the merge takes
the most recent rather than preferring one table.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes import customer_white_glove as cwg  # noqa: E402

_MEASURE_SRC = inspect.getsource(cwg._measure)
_ENSURE_SRC = inspect.getsource(cwg._ensure_columns)


def _sql_text(src: str) -> str:
    """Python splits long SQL across adjacent string literals, so the raw source
    reads `... IF NOT EXISTS "` NEWLINE `"contacted_at ...`. Rebuild the SQL as
    the interpreter sees it before matching, instead of writing a regex that
    tolerates the quotes — a tolerant regex here matches almost anything."""
    return re.sub(r"\s+", " ", src.replace('"', "").replace("'", ""))


def test_the_column_is_ensured_on_users():
    """Without this the endpoint's UPDATE hits a missing column on a fresh DB."""
    assert "ADD COLUMN IF NOT EXISTS contacted_at TIMESTAMPTZ" in _sql_text(_ENSURE_SRC)


def test_the_ensure_guard_is_not_vacuous():
    """CONTROL for the matcher above: it must NOT match a version without the
    column, or the guard passes on the shipped code."""
    shipped = 'cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS "\n "engagement_stage TEXT")'
    assert "ADD COLUMN IF NOT EXISTS contacted_at" not in _sql_text(shipped)
    assert "ADD COLUMN IF NOT EXISTS engagement_stage" in _sql_text(shipped)


def test_human_contact_reads_the_per_user_column():
    """THE guard: the old query named only founding_customers."""
    assert "u.contacted_at" in _MEASURE_SRC, (
        "human_contacted_at must read users.contacted_at, or contact stays "
        "unloggable for every payer outside founding_customers")


def test_founding_customers_history_is_not_orphaned():
    """Real prior contact lives there for 18 rows; dropping it would erase it."""
    assert "founding_customers fc" in _MEASURE_SRC
    assert "fc.contacted_at" in _MEASURE_SRC


def test_the_merge_takes_the_most_recent_not_a_preferred_table():
    """GREATEST, not COALESCE. COALESCE returns users.contacted_at even when it
    is OLDER than a founding_customers timestamp, quietly reporting a stale
    contact as the latest one. Verified against Postgres 2026-08-30: GREATEST
    ignores NULLs and yields NULL only when every argument is NULL."""
    assert "GREATEST(" in _MEASURE_SRC
    seg = _MEASURE_SRC[_MEASURE_SRC.index("GREATEST("):]
    seg = seg[:seg.index("AS human_contacted_at")]
    assert "u.contacted_at" in seg and "fc.contacted_at" in seg, seg


def test_the_endpoint_reports_addresses_it_could_not_stamp():
    """A typo'd email must not read as a logged contact. RETURNING + set-diff
    is how the caller learns nothing was written."""
    src = inspect.getsource(cwg.cwg_mark_contacted)
    assert "RETURNING" in src
    assert "not_found" in src


def test_the_endpoint_is_admin_gated_and_writes_only_on_post():
    src = inspect.getsource(cwg.cwg_mark_contacted)
    assert "_admin_ok()" in src
    assert 'methods=["POST"]' in inspect.getsource(cwg).split(
        "def cwg_mark_contacted")[0].rsplit("@customer_white_glove_bp.route", 1)[-1]


def test_must_fail_control_the_shipped_query_had_no_per_user_source():
    """CONTROL: the shipped expression named exactly one table. If this stops
    being true the guards above are asserting against nothing."""
    shipped = ("(SELECT MAX(fc.contacted_at) FROM founding_customers fc "
               "WHERE lower(fc.email)=lower(u.email)) AS human_contacted_at")
    assert "u.contacted_at" not in shipped
    assert "founding_customers" in shipped
