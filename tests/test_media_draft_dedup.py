"""tests/test_media_draft_dedup.py — pending-draft fingerprint dedup (2026-07-18).

Fences the digest-jam regressions:
  * the same story with refreshed NUMBERS must fingerprint EQUAL (so a lane
    UPDATEs the pending draft instead of INSERTing a second copy);
  * different stories must fingerprint DIFFERENT;
  * the one-time cleanup keeps the NEWEST of each fingerprint;
  * the partnership lane refreshes an existing pending duplicate in place
    (UPDATE, never a second INSERT) even though its slug embeds the ISO week.

No Postgres: cursors are stubbed.
"""
import pytest

dd = pytest.importorskip("routes.media_draft_dedup")


# ── fingerprint semantics ────────────────────────────────────────────────
def test_same_title_different_numbers_same_fingerprint():
    a = dd.draft_fingerprint(
        "partnership",
        "DC Hub Publishes Open Partnership Invitations Under the 'Switzerland' "
        "Model — CC-BY-4.0, No Channel Conflict (21,405 facilities)")
    b = dd.draft_fingerprint(
        "partnership",
        "DC Hub Publishes Open Partnership Invitations Under the 'Switzerland' "
        "Model — CC-BY-4.0, No Channel Conflict (22,237 facilities)")
    assert a == b


def test_week_slugs_normalize_equal():
    # the partnership slug embeds the ISO week — the digit-run collapse must
    # make w23 and w29 fingerprint equal
    assert dd.draft_fingerprint("partnership", "partnership-partners-2026-w23") == \
        dd.draft_fingerprint("partnership", "partnership-partners-2026-w29")


def test_different_stories_different_fingerprint():
    a = dd.draft_fingerprint("ai-citation",
                             "Asked to Compare Data-Center Sources, Perplexity "
                             "Named DC Hub a Primary Reference")
    b = dd.draft_fingerprint("ai-citation",
                             "Google Gemini Cites DC Hub #1 for Data Center "
                             "Intelligence alongside CBRE")
    assert a != b


def test_kind_and_target_partition_the_space():
    t = "Asked to Compare Data-Center Sources, X Named DC Hub a Primary Reference"
    assert dd.draft_fingerprint("ai-citation", t) != dd.draft_fingerprint("partnership", t)
    assert dd.draft_fingerprint("ai-citation", t, "perplexity") != \
        dd.draft_fingerprint("ai-citation", t, "gemini")


# ── pending-duplicate lookup ─────────────────────────────────────────────
class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_find_unpublished_duplicate_matches_and_excludes():
    title = ("Asked to Compare Data-Center Sources, Perplexity Named DC Hub "
             "a Primary Reference")
    cur = FakeCursor([
        (100160, "ai-citation-perplexity-2026-07-16-dcpi-definition", title),
        (100159, "ai-citation-perplexity-2026-07-16-competitor-dchawk", title),
    ])
    dup = dd.find_unpublished_press_duplicate(cur, "ai-citation", title)
    assert dup and dup["id"] == 100160  # newest first
    # exclude_slug skips the row being written
    dup2 = dd.find_unpublished_press_duplicate(
        cur, "ai-citation", title,
        exclude_slug="ai-citation-perplexity-2026-07-16-dcpi-definition")
    assert dup2 and dup2["id"] == 100159
    # only unpublished rows are queried
    assert "published = FALSE" in cur.executed[0][0]


def test_find_unpublished_duplicate_no_match():
    cur = FakeCursor([(1, "some-other-slug", "A Completely Different Story")])
    assert dd.find_unpublished_press_duplicate(
        cur, "ai-citation", "Perplexity Named DC Hub a Primary Reference") is None


# ── cleanup: newest of each fingerprint survives ─────────────────────────
def test_pick_survivors_keeps_newest():
    rows = [  # newest-first, as the SQL orders them
        {"id": 4, "t": "same"},
        {"id": 3, "t": "same"},
        {"id": 2, "t": "other"},
        {"id": 1, "t": "same"},
    ]
    survivors, dupes = dd._pick_survivors(rows, lambda r: r["t"])
    assert [s["id"] for s in survivors] == [4, 2]
    assert [d["id"] for d in dupes] == [3, 1]


# ── partnership lane: pending duplicate → UPDATE, not INSERT ─────────────
def test_partnership_insert_refreshes_pending_duplicate(monkeypatch):
    ppt = pytest.importorskip("routes.partnership_press_template")

    executed = []

    class Cur:
        def __init__(self):
            self._next = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            sql_flat = " ".join(sql.split())
            executed.append((sql_flat, params))
            if sql_flat.startswith("SELECT id, published FROM press_releases"):
                self._next = None                     # new-week slug: no row
            elif sql_flat.startswith("SELECT id, slug, title FROM press_releases"):
                # one pending duplicate from an earlier week, same title
                self._rows = [(54, "partnership-partners-2026-w23",
                               "DC Hub Publishes Open Partnership Invitations "
                               "Under the 'Switzerland' Model — CC-BY-4.0, No "
                               "Channel Conflict")]
            elif sql_flat.startswith("UPDATE press_releases"):
                self._next = (54,)
            return None

        def fetchone(self):
            return self._next

        def fetchall(self):
            return getattr(self, "_rows", [])

    class Conn:
        autocommit = True
        def cursor(self): return Cur()
        def close(self): pass

    import contextlib

    @contextlib.contextmanager
    def fake_conn():
        yield Conn()

    monkeypatch.setattr(ppt, "_conn", fake_conn)
    monkeypatch.setattr(ppt, "_pg", object())          # truthy
    monkeypatch.setattr(ppt, "_dsn", lambda: "postgres://x")

    release = {
        "title": ("DC Hub Publishes Open Partnership Invitations Under the "
                  "'Switzerland' Model — CC-BY-4.0, No Channel Conflict"),
        "subheadline": "sub", "summary": "sum", "body": "body",
        "slug": "partnership-partners-2026-w29", "source": "DC Hub Media",
        "source_url": "https://dchub.cloud/partners", "category": "partnership",
    }
    out = ppt._insert_press_release(release, auto_publish=False)
    assert out["ok"] and out.get("refreshed") is True and out["id"] == 54
    updates = [s for s, _ in executed if s.startswith("UPDATE press_releases")]
    inserts = [s for s, _ in executed if s.startswith("INSERT INTO press_releases")]
    assert updates and not inserts, "pending duplicate must be UPDATEd, never re-INSERTed"
    assert "published = FALSE" in updates[0]
