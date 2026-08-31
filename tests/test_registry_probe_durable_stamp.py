"""The registry-watch probe stamp must be shared across replicas.

WHY THIS FEED SAT RED FOR 1,002 HOURS
-------------------------------------
mcp-registry-watch.yml spawns an async scan (202) and then polls
GET /api/v1/brain/mcp-registries until `probed_at` rises above the trigger time.
That contract is right and the workflow implements it correctly. It could still
never pass, because `probed_at` was read out of `_PROBE_CACHE` — a module-level
dict, private to one process.

dchub-backend runs `gunicorn --workers 1 --threads 8` across TWO Railway
replicas behind a round-robin edge, so the POST spawns the scan on replica A
while polls land on either. The 2026-08-31 07:48 run is the proof: consecutive
polls returned 07:34:20 then 07:34:00 — two different stamps from two replicas,
neither ever rising past the 07:49 trigger.

These tests pin the three properties that make the poll observable:
  1. a completed scan is PUBLISHED durably, not only cached in-process,
  2. the read prefers the durable stamp,
  3. a DB failure degrades to the in-process stamp, never to null or a 500.

Property 4 — second precision — is not cosmetic. The poller string-compares
this value, and ".123456Z" sorts BEFORE "Z", so microseconds would make a later
stamp read as earlier and the poll would never terminate.

Functions are pulled out with `ast` and run against stubs: no DB, no network.
"""

import ast
import datetime
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "mcp_registry_watch.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _fn(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _load(durable=None, cache_at=None):
    ns = {
        "datetime": datetime,
        "_durable_probed_at": lambda: durable,
        "_PROBE_CACHE": {"data": None, "at": cache_at},
    }
    mod = ast.Module(body=[_fn("_probed_at_iso")], type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)          # noqa: S102 — the point
    return ns["_probed_at_iso"]


UTC = datetime.timezone.utc


# ── 1. the durable stamp wins ────────────────────────────────────────

def test_durable_stamp_is_preferred_over_the_in_process_one():
    """The whole fix. If the in-process value wins, a poll landing on the
    replica that did not scan reports a stale stamp and the loop never ends."""
    dur = datetime.datetime(2026, 8, 31, 9, 30, 0, tzinfo=UTC)
    stale_cache = datetime.datetime(2026, 8, 31, 7, 34, 0, tzinfo=UTC).timestamp()
    assert _load(durable=dur, cache_at=stale_cache)() == "2026-08-31T09:30:00Z"


def test_falls_back_to_the_in_process_stamp_when_the_db_is_unreadable():
    """Degrade to the OLD behaviour, never to null — a null would make the
    poller report 'never scanned' during an unrelated DB blip."""
    at = datetime.datetime(2026, 8, 31, 7, 34, 0, tzinfo=UTC).timestamp()
    assert _load(durable=None, cache_at=at)() == "2026-08-31T07:34:00Z"


def test_returns_none_only_when_nothing_has_ever_scanned():
    assert _load(durable=None, cache_at=None)() is None


def test_naive_durable_timestamp_is_treated_as_utc():
    """psycopg2 can hand back a naive datetime depending on column type; a
    naive value must not raise or silently shift the hour."""
    dur = datetime.datetime(2026, 8, 31, 9, 30, 0)      # no tzinfo
    assert _load(durable=dur)() == "2026-08-31T09:30:00Z"


# ── 2. second precision, or the poll never terminates ────────────────

@pytest.mark.parametrize("micro", [0, 1, 123456, 999999])
def test_stamp_never_carries_microseconds(micro):
    """'...:57.123456Z' vs '...:57Z' compares '.' (0x2E) < 'Z' (0x5A), so a
    microsecond stamp reads as EARLIER than a whole-second one and the poller's
    string compare would never see it rise."""
    dur = datetime.datetime(2026, 8, 31, 9, 30, 57, micro, tzinfo=UTC)
    out = _load(durable=dur)()
    assert out == "2026-08-31T09:30:57Z"
    assert "." not in out


def test_stamps_sort_chronologically_as_plain_strings():
    """The property the poller actually relies on."""
    a = _load(durable=datetime.datetime(2026, 8, 31, 7, 49, 0, tzinfo=UTC))()
    b = _load(durable=datetime.datetime(2026, 8, 31, 7, 49, 1, 500000, tzinfo=UTC))()
    assert a < b, f"{b!r} must sort after {a!r}"


# ── 3. a completed scan is published ─────────────────────────────────

def test_probe_all_cached_publishes_after_a_scan():
    """A durable read is useless if nothing ever writes. This is the pairing
    that the 1,002-hour outage came down to."""
    src = ast.get_source_segment(TEXT, _fn("_probe_all_cached"))
    assert "_persist_probe(data)" in src, \
        "a completed probe must publish its stamp for the other replica"
    # and it must publish AFTER the probe, not before — a stamp written first
    # would claim completion for a scan that then died.
    assert src.index("_probe_all()") < src.index("_persist_probe(data)")


def test_persist_uses_direct_ddl_not_the_wrapper():
    """safe_db SKIPs DDL — documented four times over in this repo. A CREATE
    through the wrapper is surrendered silently and the INSERT then fails on a
    table that was never made."""
    src = ast.get_source_segment(TEXT, _fn("_persist_probe"))
    assert "psycopg2.connect" in src
    assert "_STAMP_DDL" in src


def test_persist_upserts_a_single_row():
    """Without ON CONFLICT this table grows a row per scan and `WHERE id = 1`
    starts returning an arbitrary one."""
    src = ast.get_source_segment(TEXT, _fn("_persist_probe"))
    assert "ON CONFLICT (id) DO UPDATE" in src
    ddl = None
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_STAMP_DDL" for t in node.targets):
            ddl = ast.literal_eval(node.value)
    assert ddl, "_STAMP_DDL missing"
    norm = " ".join(ddl.split()).lower()
    assert "id smallint primary key" in norm
    assert "check (id = 1)" in norm, "nothing else may claim the singleton row"


def test_persist_and_read_both_fail_soft():
    """Neither may take down the public status route."""
    for name in ("_persist_probe", "_durable_probed_at"):
        src = ast.get_source_segment(TEXT, _fn(name))
        assert "except Exception" in src, f"{name} must not raise"


def test_status_route_declares_which_stamp_it_used():
    """Diagnosing this outage took reading two replicas' answers by hand. The
    route now says which source it answered from."""
    assert '"probed_at_source"' in TEXT
    for token in ('"durable"', '"in_process"', '"never"'):
        assert token in TEXT, f"{token} not reported"
