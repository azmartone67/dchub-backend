"""tests/test_brain_findings_writer_discipline.py — one writer, not eleven
(2026-08-29).

routes/brain_findings_writer.py was built 2026-06-06 after schema drift broke
4+ writers SILENTLY: each hand-rolled `INSERT ... seen_count ... ON CONFLICT
(issue,url)` against a live table that has neither, the INSERTs failed inside
bare `except` blocks, and recurrence tracking plus the learning loop quietly
broke for weeks.

The module was written. The writers were not all converted. On 2026-08-29 TEN
files still hand-rolled a raw INSERT — url_registry.py had even written the
warning in a comment ("8 writers / 5 column-lists; a wrong-column INSERT would
silently drop findings") directly above its own hand-rolled sixth column-list.
loop_control_master_shell.py item 5 named the debt and nothing collected it.

A convention that is documented but unenforced decays back to ten writers. This
is the enforcement.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_brain_findings_writer_discipline.py -v
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The ONE module allowed to name the table in an INSERT: it is the writer.
_WRITER = "routes/brain_findings_writer.py"

# Migration/one-shot tooling operates ON the table by definition.
_EXEMPT_DIRS = ("tools/", "tests/", "scripts/", "docs/", "migrations/")

# ★ THE ONE JUSTIFIED EXEMPTION, and it is not a style preference.
#
# self_growing_index.py runs on a RAW AUTOCOMMIT psycopg2 connection (it does
# DDL — CREATE INDEX — and must never go through db_utils, which sets
# SKIP_DDL='1' and silently drops it). The canonical writer wraps every
# operation in a SAVEPOINT, and SAVEPOINT raises outside a transaction block.
# _savepoint() catches that, returns False, and upsert_brain_finding returns
# "skipped" having issued ZERO INSERTs.
#
# Verified 2026-08-29 against a cursor whose SAVEPOINT raises:
#     upsert_brain_finding(...) -> 'skipped', INSERTs issued: 0
#
# So converting this file would have SILENTLY STOPPED its findings — the exact
# failure mode this whole lane exists to remove. The exemption is enforced
# below rather than merely granted: test_the_autocommit_exemption_is_earned
# fails the moment the file stops being autocommit, at which point it must be
# converted like the rest.
_AUTOCOMMIT_EXEMPT = {"self_growing_index.py"}

# \b matters: without it this also matches brain_findings_DISABLED, and a
# mutation renaming the writer's own table left the paired control green.
_RAW_INSERT = re.compile(r"INSERT\s+INTO\s+brain_findings\b", re.I)


def _python_sources():
    for p in sorted(_ROOT.rglob("*.py")):
        rel = p.relative_to(_ROOT).as_posix()
        if rel.startswith(_EXEMPT_DIRS) or "/.venv/" in rel or rel.startswith(".venv/"):
            continue
        if "__pycache__" in rel:
            continue
        yield rel, p


def test_only_the_canonical_writer_inserts_into_brain_findings():
    """★THE GATE. Every other writer goes through upsert_brain_finding(),
    which introspects the live columns, self-heals seen_count, upserts
    constraint-agnostically, and savepoint-wraps every op so a failure
    cannot poison the caller's transaction."""
    offenders = []
    for rel, path in _python_sources():
        if rel == _WRITER or rel in _AUTOCOMMIT_EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _RAW_INSERT.search(text):
            offenders.append(rel)
    assert not offenders, (
        "hand-rolled INSERT INTO brain_findings in: %s\n"
        "Use routes.brain_findings_writer.upsert_brain_finding(cur, ...) — it "
        "introspects the live schema instead of assuming it. That assumption "
        "is what broke four writers silently in June."
        % ", ".join(offenders))


def test_the_canonical_writer_still_owns_an_insert():
    """THE PAIRED CONTROL. The gate above passes trivially if the writer
    itself stops inserting — a green test proving nothing writes anywhere."""
    text = (_ROOT / _WRITER).read_text(encoding="utf-8")
    assert _RAW_INSERT.search(text), (
        "%s no longer contains an INSERT — the discipline test above would "
        "now pass vacuously" % _WRITER)


def test_the_converted_writers_actually_call_the_writer():
    """The ten converted files must IMPORT the writer, not merely have had
    their INSERT deleted. A finding that is no longer written at all is a
    worse outcome than one written badly, and both make the gate green."""
    converted = [
        "routes/mcp_registry_watch.py",
        "routes/brain_layer14_slo_burn.py",
        "routes/brain_v3.py",
        "routes/registry_freshness_master_shell.py",
        "routes/upgrade_pool_outreach.py",
        "routes/brain_layer15_tool_calibration.py",
        "routes/pockets.py",
        "routes/url_registry.py",
        "routes/paywall_test.py",
    ]
    missing = []
    for rel in converted:
        p = _ROOT / rel
        if not p.exists():
            continue
        if "upsert_brain_finding" not in p.read_text(encoding="utf-8"):
            missing.append(rel)
    assert not missing, (
        "these files had a raw INSERT removed but never call the canonical "
        "writer — their findings are no longer written at all: %s"
        % ", ".join(missing))


def test_a_magnitude_is_not_declared_an_occurrence():
    """★ COUNT SEMANTICS. count_kind='occurrence' is the ONE value that lets a
    consumer weigh `count` as a tally of sightings. Sites whose count is a
    quantity — emails sent, indexes created, a render boolean — must not
    claim it. consistency_radar's int(seconds_since) made 5.5 days of cron
    silence read as 477,455 sightings and re-win the agenda every tick."""
    magnitudes = {
        "routes/upgrade_pool_outreach.py": "count=sent",
        "routes/paywall_test.py": "count=1 if (renders_redeem or renders_upgrade) else 0",
    }
    for rel, needle in magnitudes.items():
        p = _ROOT / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        idx = text.find(needle)
        assert idx != -1, "%s no longer contains %r" % (rel, needle)
        # the upsert call this count belongs to must not declare occurrence
        window = text[idx:idx + 600]
        call_end = window.find(")\n")
        call = window[:call_end if call_end != -1 else len(window)]
        assert "occurrence" not in call, (
            "%s declares count_kind='occurrence' on a magnitude (%s) — that "
            "buys it agenda leverage it has not earned" % (rel, needle))


def test_the_autocommit_exemption_is_earned_not_granted():
    """★ An exemption nobody re-checks becomes a permanent excuse. This one is
    valid ONLY while the file genuinely runs on an autocommit connection; the
    day that changes it must join the other nine."""
    for rel in _AUTOCOMMIT_EXEMPT:
        p = _ROOT / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        assert "autocommit = True" in text, (
            "%s is exempt from the canonical-writer gate because SAVEPOINT "
            "cannot run on its autocommit connection. It no longer sets "
            "autocommit, so the exemption is void — convert it to "
            "upsert_brain_finding()." % rel)


def test_the_writer_still_no_ops_under_autocommit():
    """The documented reason for the exemption, asserted rather than trusted.
    If the writer ever learns to work without SAVEPOINT, this test fails and
    the exemption above should be deleted."""
    from routes import brain_findings_writer as w

    class AutocommitCursor:
        """SAVEPOINT raises, exactly as psycopg2 does outside a transaction."""

        def __init__(self):
            self.ops = []
            self._rows = []

        def execute(self, sql, params=None):
            flat = " ".join(str(sql).split())
            self.ops.append(flat)
            if flat.upper().startswith("SAVEPOINT"):
                raise RuntimeError(
                    "SAVEPOINT can only be used in transaction blocks")
            self._rows = ([("issue",), ("url",), ("count",), ("detail",)]
                          if "information_schema" in flat else [])

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    cur = AutocommitCursor()
    res = w.upsert_brain_finding(cur, issue="x", url="y", count=1,
                                 detail="d", detector="t")
    assert res == "skipped", (
        "the writer now returns %r under autocommit — if it can write there, "
        "delete _AUTOCOMMIT_EXEMPT and convert self_growing_index.py" % res)
    assert not [o for o in cur.ops if o.upper().startswith("INSERT")], \
        "the writer issued an INSERT under autocommit"
