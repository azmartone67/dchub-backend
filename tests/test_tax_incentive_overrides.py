"""tax_incentives_routes: DEFAULT_INCENTIVES is truth, the DB holds overrides.

★ WHY THIS FILE EXISTS. _load_from_db() read a bare `cursor` that was never
defined in its scope. Every call raised NameError, a bare `except:` returned
[], and the caller read [] as "nothing stored" and reseeded the whole table
from the module — so the fallback path always worked and the defect was
invisible for as long as it existed. There was no test that ever opened a
connection, so nothing could have caught it.

These tests use a REAL sqlite3 connection rather than a fake cursor. That is
the point: a fake that accepts whatever SQL it is handed would have passed
against the broken loader too, because the broken loader never reached the
fake. The statements here are the ones production runs — `_placeholder()`
swaps only the DB-API placeholder, and `ON CONFLICT ... DO UPDATE SET ... =
EXCLUDED.x` is the same text on both sqlite3 >= 3.24 and Postgres >= 9.5.
"""
import json
import sqlite3

import pytest

import tax_incentives_routes as tir


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    tir._init_db(conn)
    yield conn
    conn.close()


# A three-state stand-in for DEFAULT_INCENTIVES, so a real statutory edit to
# the module cannot make these assertions fail.
DEFAULTS = [
    {'abbr': 'OH', 'name': 'Ohio', 'rating': 4, 'summary': 'original OH summary'},
    {'abbr': 'TX', 'name': 'Texas', 'rating': 5, 'summary': 'original TX summary'},
    {'abbr': 'CA', 'name': 'California', 'rating': 1, 'summary': 'original CA summary'},
]


# ── the loader actually reaches the database ────────────────────────────────

def test_load_overrides_returns_what_save_wrote(db):
    """The direct NameError regression. The broken loader returns {} here."""
    tir._save_override(db, 'OH', {'rating': 2}, {'rating': 4})

    loaded = tir._load_overrides(db)

    assert set(loaded) == {'OH'}, "loader did not see the row it just wrote"
    assert loaded['OH']['fields'] == {'rating': 2}
    assert loaded['OH']['base'] == {'rating': 4}


def test_load_overrides_is_empty_before_anything_is_written(db):
    """The zero control. Without it, a loader that always returns {} would
    satisfy every other 'defaults win' assertion in this file."""
    assert tir._load_overrides(db) == {}


def test_loader_does_not_swallow_a_programming_error(db):
    """A NameError/AttributeError inside the loader must REACH the caller.

    This is the class of defect, not the instance: repairing `cursor` while
    leaving `except:` in place would let the next one hide just as long.
    """
    class ConnectionWhoseCursorIsBroken:
        def cursor(self):
            return self

        def execute(self, *_args, **_kwargs):
            raise NameError("name 'cursor' is not defined")

    with pytest.raises(NameError):
        tir._load_overrides(ConnectionWhoseCursorIsBroken())


# ── the layering rule ───────────────────────────────────────────────────────

def test_defaults_win_where_no_override_exists():
    data, notes = tir._layer_overrides(DEFAULTS, {})
    assert data['OH']['rating'] == 4
    assert data['TX']['summary'] == 'original TX summary'
    assert notes == []


def test_an_override_masks_only_its_own_fields():
    """The Ohio guarantee. An admin edit to one field must not freeze the rest
    of that state against later module corrections."""
    overrides = {'OH': {'fields': {'rating': 2}, 'base': {'rating': 4}}}

    data, _ = tir._layer_overrides(DEFAULTS, overrides)

    assert data['OH']['rating'] == 2, 'override lost'
    assert data['OH']['summary'] == 'original OH summary', 'override froze an unedited field'
    assert data['TX']['rating'] == 5, 'override leaked into another state'


def test_a_module_correction_reaches_a_state_that_carries_an_override():
    """DEFAULT_INCENTIVES moves; the override is on a DIFFERENT field of the
    same state. The correction must land. Under the pre-fix design — seed the
    table, then read the table back as truth — it would not have."""
    corrected = [dict(s) for s in DEFAULTS]
    corrected[0]['summary'] = 'ORC 122.175 paused to new applicants 2026-05-27'
    overrides = {'OH': {'fields': {'rating': 2}, 'base': {'rating': 4}}}

    data, notes = tir._layer_overrides(corrected, overrides)

    assert data['OH']['summary'] == 'ORC 122.175 paused to new applicants 2026-05-27'
    assert data['OH']['rating'] == 2
    assert notes == [], 'an untouched field moving is not drift'


def test_drift_is_reported_when_the_module_moves_under_an_override():
    """The one real hazard of allowing overrides: an admin edit silently
    masking a newer statutory value. It gets named, not swallowed."""
    corrected = [dict(s) for s in DEFAULTS]
    corrected[0]['rating'] = 1
    overrides = {'OH': {'fields': {'rating': 2}, 'base': {'rating': 4}}}

    data, notes = tir._layer_overrides(corrected, overrides)

    assert data['OH']['rating'] == 2
    assert len(notes) == 1
    assert 'OH.rating' in notes[0]
    assert '4' in notes[0] and '1' in notes[0], 'note must carry both values'


def test_override_for_an_unknown_state_is_ignored_and_reported():
    data, notes = tir._layer_overrides(DEFAULTS, {'ZZ': {'fields': {'rating': 5}}})

    assert 'ZZ' not in data
    assert len(notes) == 1 and 'ZZ' in notes[0]


def test_updated_at_surfaces_as_last_modified():
    overrides = {'OH': {'fields': {'rating': 2}, 'updated_at': '2026-09-18T00:00:00Z'}}
    data, _ = tir._layer_overrides(DEFAULTS, overrides)
    assert data['OH']['last_modified'] == '2026-09-18T00:00:00Z'


# ── writes ──────────────────────────────────────────────────────────────────

def test_successive_edits_accumulate_rather_than_replace(db):
    tir._save_override(db, 'OH', {'rating': 2}, {'rating': 4})
    tir._save_override(db, 'OH', {'summary': 'edited'}, {'summary': 'original OH summary'})

    entry = tir._load_overrides(db)['OH']

    assert entry['fields'] == {'rating': 2, 'summary': 'edited'}
    assert entry['base'] == {'rating': 4, 'summary': 'original OH summary'}


def test_save_stores_only_the_changed_fields_not_a_snapshot(db):
    """A full snapshot is what made the old design unsafe: it pinned every
    field of the state to the module as it stood on the day of the edit."""
    tir._save_override(db, 'OH', {'rating': 2}, tir._DEFAULTS_BY_ABBR['OH'])

    stored = json.loads(db.cursor().execute(
        'SELECT payload FROM ' + tir._OVERRIDES_TABLE + " WHERE abbr = 'OH'"
    ).fetchone()[0])

    assert list(stored['fields']) == ['rating']
    assert 'summary' not in stored['fields']
    assert 'name' not in stored['fields']


def test_a_second_boot_does_not_seed_or_clobber_the_table(db):
    """The clobber regression. _seed_db wrote all 50 states with
    `ON CONFLICT DO UPDATE SET data = EXCLUDED.data` on EVERY boot, so an admin
    edit survived only until the next restart. Boot must now write nothing.
    """
    from flask import Flask

    tir._save_override(db, 'OH', {'rating': 2}, {'rating': 4})
    before = tir._load_overrides(db)

    for name in ('boot-one', 'boot-two'):
        app = Flask(name)
        tir.setup_tax_incentive_routes(app, db)
        # Drive a request: initialisation is lazy, so a bare setup() would make
        # this assertion vacuously true without ever reaching the store.
        with app.test_client() as client:
            assert client.get('/api/v1/tax-incentives').status_code == 200

    after = tir._load_overrides(db)
    assert set(after) == {'OH'}, 'boot seeded rows for states nobody edited'
    assert after['OH']['fields'] == before['OH']['fields'], 'boot overwrote an admin edit'


def test_boot_serves_defaults_layered_with_the_stored_override(db):
    """End to end through the real entrypoint, on a real connection.

    Asserted on the fields an ANONYMOUS caller is actually served. GET
    /api/v1/tax-incentives gates the rich detail fields (summary, details,
    source, ...) to IDENTIFIED+, so `summary` is absent here by design — this
    test does not hand itself a privileged seat to reach past that gate, it
    picks unedited fields that survive it.
    """
    from flask import Flask

    oh = tir._DEFAULTS_BY_ABBR['OH']
    assert oh['rating'] != 2, 'fixture must differ from the module value it overrides'
    tir._save_override(db, 'OH', {'rating': 2}, oh)

    app = Flask('boot-layered')
    tir.setup_tax_incentive_routes(app, db)

    with app.test_client() as client:
        served = {s['abbr']: s for s in client.get('/api/v1/tax-incentives').get_json()['data']}

    assert served['OH']['rating'] == 2, 'stored override never reached the response'
    assert served['OH']['name'] == oh['name'], 'override froze an unedited field'
    assert served['OH']['has_incentive'] == oh['has_incentive'], 'override froze an unedited field'
    assert served['TX']['rating'] == tir._DEFAULTS_BY_ABBR['TX']['rating']


def test_setup_does_not_mutate_the_module_constant(db):
    """`{s['abbr']: s for s in DEFAULT_INCENTIVES}` aliased the module dicts,
    so a PUT rewrote the source of truth in place for the life of the process.
    """
    from flask import Flask

    tir._save_override(db, 'OH', {'rating': 2}, {'rating': 4})
    app = Flask('boot-alias')
    tir.setup_tax_incentive_routes(app, db)
    with app.test_client() as client:
        client.get('/api/v1/tax-incentives')   # lazy: force the layer to load

    assert tir._DEFAULTS_BY_ABBR['OH']['rating'] != 2
    assert next(s for s in tir.DEFAULT_INCENTIVES if s['abbr'] == 'OH')['rating'] != 2


# ── the admin PUT, through the real route ───────────────────────────────────

ADMIN_KEY = 'test-admin-key-tax-incentives'


@pytest.fixture
def admin(monkeypatch):
    """Authenticate as the admin operator the PUT route is written for.

    Sets the env var internal_auth reads rather than stubbing the gate, so the
    fail-closed check on the route still runs for real.
    """
    monkeypatch.setenv('DCHUB_ADMIN_KEY', ADMIN_KEY)
    return {'X-Admin-Key': ADMIN_KEY}


def test_put_without_a_db_does_not_mutate_the_module_constant(admin):
    """★ This is PRODUCTION's path: main.py calls setup_tax_incentive_routes(app)
    with NO db, so `if db:` never runs and every store below is unreachable.

    With the dicts aliased, `incentives_data[abbr].update(updates)` rewrote the
    entry inside DEFAULT_INCENTIVES itself — the module constant, shared by the
    v2 routes, the CSV export and the GeoJSON map layer — for the life of the
    worker. The edit still does not SURVIVE a restart here (nothing persists it
    without a db); it must at least not corrupt the source of truth in place.
    """
    from flask import Flask

    before = dict(tir._DEFAULTS_BY_ABBR['OH'])
    app = Flask('no-db-put')
    tir.setup_tax_incentive_routes(app, None)

    with app.test_client() as client:
        r = client.put('/api/v1/tax-incentives/OH', json={'rating': 2}, headers=admin)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['data']['rating'] == 2, 'the edit did not take effect in-process'
    assert tir._DEFAULTS_BY_ABBR['OH'] == before, 'PUT mutated DEFAULT_INCENTIVES in place'
    assert next(s for s in tir.DEFAULT_INCENTIVES if s['abbr'] == 'OH')['rating'] == before['rating']


def test_put_persists_only_the_changed_field_as_an_override(db, admin):
    """The whole point of step 3, asserted through the HTTP route rather than
    by calling _save_override directly."""
    from flask import Flask

    app = Flask('put-override')
    tir.setup_tax_incentive_routes(app, db)

    with app.test_client() as client:
        r = client.put('/api/v1/tax-incentives/OH', json={'rating': 2}, headers=admin)
    assert r.status_code == 200, r.get_data(as_text=True)

    entry = tir._load_overrides(db)['OH']
    assert entry['fields'] == {'rating': 2}, 'PUT stored more than it changed'
    assert entry['base'] == {'rating': tir._DEFAULTS_BY_ABBR['OH']['rating']}
    assert set(tir._load_overrides(db)) == {'OH'}, 'PUT wrote rows for untouched states'


def test_the_admin_put_is_still_fail_closed(db):
    """The override store must not have widened who can write to it."""
    from flask import Flask

    app = Flask('put-unauth')
    tir.setup_tax_incentive_routes(app, db)

    with app.test_client() as client:
        r = client.put('/api/v1/tax-incentives/OH', json={'rating': 2})

    assert r.status_code == 401
    assert tir._load_overrides(db) == {}, 'an unauthorized PUT reached the store'


# ── connection lifecycle: production passes a FACTORY, not a connection ─────
#
# main.get_db() checks a connection out of the Neon pool; it returns to the pool
# only on .close(), and the pool is capped at DB_POOL_MAX (50). A module that
# took one at boot and kept it in a route closure would hold one per worker for
# the life of the process. These tests use a file-backed sqlite3 DB so a real
# open/close cycle per operation is observable.

class _TrackedConnection:
    """Proxy around a real connection that counts its return to the pool.

    A proxy and not a patched method: `.close` on sqlite3.Connection — and on
    psycopg2's — is a read-only C attribute, which is the same reason main.py
    wraps pooled read connections in _ReadPoolConn instead of monkey-patching.
    Proxying also makes these tests exercise the shape production actually
    passes: a wrapper whose type lives outside the driver module.
    """

    def __init__(self, conn, factory):
        self._conn = conn
        self._factory = factory

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._factory.closed += 1
        self._conn.close()


class RecordingFactory:
    """Hands out real sqlite3 connections and records open/close pairs."""

    def __init__(self, path):
        self.path = path
        self.opened = 0
        self.closed = 0

    def __call__(self):
        conn = sqlite3.connect(self.path)
        self.opened += 1
        return _TrackedConnection(conn, self)

    @property
    def outstanding(self):
        return self.opened - self.closed


@pytest.fixture
def factory(tmp_path):
    f = RecordingFactory(str(tmp_path / 'overrides.db'))
    tir._init_db(f)
    return f


def test_a_factory_connection_is_returned_after_every_operation(factory):
    """The pool-leak regression: nothing may stay checked out."""
    assert factory.outstanding == 0, 'init_db kept a connection'

    tir._save_override(factory, 'OH', {'rating': 2}, {'rating': 4})
    assert factory.outstanding == 0, '_save_override kept a connection'

    assert tir._load_overrides(factory)['OH']['fields'] == {'rating': 2}
    assert factory.outstanding == 0, '_load_overrides kept a connection'
    assert factory.opened >= 3, 'operations did not each acquire a connection'


def test_a_factory_connection_is_returned_even_when_the_query_raises(factory):
    """Release must be in a finally, or one bad query starves the pool."""
    before = factory.outstanding

    class Boom(Exception):
        pass

    def exploding_factory():
        conn = factory()

        def bad_cursor():
            raise Boom('driver blew up')

        conn.cursor = bad_cursor
        return conn

    with pytest.raises(Boom):
        tir._load_overrides(exploding_factory)

    assert factory.outstanding == before, 'connection leaked on the error path'


def test_a_live_connection_is_never_closed(db):
    """The tests — and any caller holding its own handle — must keep it.

    A shared in-memory sqlite3 database is destroyed by close(), so closing a
    caller-owned connection would silently empty the store between calls.
    """
    tir._save_override(db, 'OH', {'rating': 2}, {'rating': 4})
    tir._load_overrides(db)
    tir._load_overrides(db)

    assert tir._load_overrides(db)['OH']['fields'] == {'rating': 2}


def test_a_sqlite_connection_is_not_mistaken_for_a_factory(db):
    """★ sqlite3.Connection defines __call__, so `callable(conn)` is True.

    Discriminating on callable() sent every live connection down the factory
    branch and called it as `db()` — TypeError on every operation. Pinned
    because the trap is invisible at the call site.
    """
    assert callable(db), 'premise changed: sqlite3 connections used to be callable'
    assert hasattr(db, 'cursor')
    tir._load_overrides(db)  # must not raise


# ── TTL refresh: an edit on one worker reaches the others ───────────────────

def test_an_override_written_elsewhere_is_picked_up_on_the_ttl(factory, monkeypatch):
    """Worker A writes; worker B must stop serving its boot-time layer.

    Without this, wiring the DB would only fix 'survives a restart' and leave
    'invisible to the other workers until one' exactly as broken as before.
    """
    from flask import Flask

    monkeypatch.setattr(tir, '_OVERRIDE_TTL_SECONDS', 0)
    app = Flask('worker-b')
    tir.setup_tax_incentive_routes(app, factory)

    with app.test_client() as client:
        first = [s for s in client.get('/api/v1/tax-incentives').get_json()['data']
                 if s['abbr'] == 'OH'][0]
        assert first['rating'] == tir._DEFAULTS_BY_ABBR['OH']['rating']

        # another worker edits the shared store
        tir._save_override(factory, 'OH', {'rating': 2}, tir._DEFAULTS_BY_ABBR['OH'])

        second = [s for s in client.get('/api/v1/tax-incentives').get_json()['data']
                  if s['abbr'] == 'OH'][0]

    assert second['rating'] == 2, 'worker B never re-read the override store'
    assert factory.outstanding == 0


def test_the_layer_is_held_until_the_ttl_expires(factory, monkeypatch):
    """The positive control for the test above: with a TTL in force, the second
    read must NOT hit the database. Without this, a refresh-on-every-request
    implementation would pass the TTL test and nobody would notice the load."""
    from flask import Flask

    monkeypatch.setattr(tir, '_OVERRIDE_TTL_SECONDS', 3600)
    app = Flask('worker-ttl')
    tir.setup_tax_incentive_routes(app, factory)

    with app.test_client() as client:
        client.get('/api/v1/tax-incentives')
        opened = factory.opened
        tir._save_override(factory, 'OH', {'rating': 2}, tir._DEFAULTS_BY_ABBR['OH'])
        after_write = factory.opened

        served = [s for s in client.get('/api/v1/tax-incentives').get_json()['data']
                  if s['abbr'] == 'OH'][0]

    assert factory.opened == after_write, 'read hit the DB despite a live TTL'
    assert served['rating'] == tir._DEFAULTS_BY_ABBR['OH']['rating']
    assert opened > 0


def test_a_failing_refresh_keeps_serving_the_last_good_layer(factory, monkeypatch, caplog):
    """before_request must not turn a DB blip into a 500 on a public endpoint —
    and must not go quiet about it either.

    ★ The log assertion is not decoration. This module ran with a NameError on
    every call for its whole life because the failure was swallowed into
    silence; a rescue path that degrades without saying so recreates exactly
    that. Mutation N13 — replace the handler body with `pass` — passed every
    other assertion in this file.
    """
    import logging

    from flask import Flask

    tir._save_override(factory, 'OH', {'rating': 2}, tir._DEFAULTS_BY_ABBR['OH'])
    monkeypatch.setattr(tir, '_OVERRIDE_TTL_SECONDS', 0)

    app = Flask('worker-blip')
    tir.setup_tax_incentive_routes(app, factory)

    def dead_pool(_db):
        raise RuntimeError('Circuit breaker OPEN: database unavailable')

    with app.test_client() as client:
        assert client.get('/api/v1/tax-incentives').status_code == 200
        monkeypatch.setattr(tir, '_load_overrides', dead_pool)
        with caplog.at_level(logging.ERROR, logger='tax_incentives_routes'):
            r = client.get('/api/v1/tax-incentives')

    assert r.status_code == 200, 'a DB blip 500d a public endpoint'
    oh = [s for s in r.get_json()['data'] if s['abbr'] == 'OH'][0]
    assert oh['rating'] == 2, 'a failed refresh wiped the live override layer'

    complaints = [rec for rec in caplog.records
                  if rec.levelno >= logging.ERROR and 'refresh failed' in rec.getMessage()]
    assert complaints, 'the refresh degraded silently — no ERROR was logged'
    assert complaints[0].exc_info, 'logged without a traceback; use logger.exception'


def test_a_driver_error_raises_instead_of_reading_as_no_overrides(factory):
    """One return value, two meanings, is what would let a blip wipe the layer.

    `{}` must mean 'nobody has overridden anything' and nothing else.
    """
    import sqlite3 as real_sqlite3

    def broken_factory():
        conn = factory()
        conn.execute('DROP TABLE ' + tir._OVERRIDES_TABLE)
        conn.commit()
        return conn

    with pytest.raises(real_sqlite3.Error):
        tir._load_overrides(broken_factory)


# ── driver detection through a pool wrapper ─────────────────────────────────
#
# _placeholder() picks '%s' vs '?' from the driver's declared paramstyle, and
# production never hands this module a bare driver connection — it hands a
# _PoolConnWrapper defined in main.py. Getting this wrong sends Postgres
# placeholders to sqlite or the reverse, so both wrapper shapes are pinned.

class _RawSlotWrapper:
    """Shaped like main._PoolConnWrapper: real connection in `_raw`."""
    __slots__ = ('_raw',)

    def __init__(self, raw):
        object.__setattr__(self, '_raw', raw)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _ConnSlotWrapper:
    """Shaped like main._ReadPoolConn: real connection in `_conn`."""
    __slots__ = ('_conn',)

    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.mark.parametrize('wrap', [_RawSlotWrapper, _ConnSlotWrapper])
def test_the_driver_is_found_through_a_pool_wrapper(db, wrap):
    wrapped = wrap(db)

    assert tir._driver_module(wrapped) is sqlite3, 'wrapper hid the real driver'
    assert tir._placeholder(wrapped) == '?', 'wrapped sqlite3 would get Postgres SQL'


def test_an_unwrapped_connection_still_resolves(db):
    """Positive control: the unwrap loop must not break the plain case."""
    assert tir._driver_module(db) is sqlite3
    assert tir._placeholder(db) == '?'


def test_the_unwrap_loop_terminates_on_a_self_referencing_wrapper():
    """A wrapper whose slot returns itself must not spin forever."""
    class Ouroboros:
        @property
        def _raw(self):
            return self

    assert tir._placeholder(Ouroboros()) == '%s'  # falls through to the default


def test_wrapped_and_unwrapped_connections_write_identically(db, tmp_path):
    """The end-to-end consequence: a wrapped connection must execute the same
    statements, not just resolve the same module."""
    tir._save_override(_RawSlotWrapper(db), 'OH', {'rating': 2}, {'rating': 4})

    assert tir._load_overrides(_ConnSlotWrapper(db))['OH']['fields'] == {'rating': 2}


def test_boot_does_not_touch_the_database(factory):
    """★ Registration must not acquire a connection.

    db is main.get_db, and get_pg_connection() waits up to _POOL_ACQUIRE_TIMEOUT
    (10s) per attempt over 3 attempts. Loading at boot would stall every
    worker's startup for ~30s against a saturated or unreachable pool — to end
    up serving the defaults it already had in memory.
    """
    from flask import Flask

    before = factory.opened
    app = Flask('lazy-boot')
    tir.setup_tax_incentive_routes(app, factory)

    assert factory.opened == before, 'registration acquired a connection'

    with app.test_client() as client:
        assert client.get('/api/v1/tax-incentives').status_code == 200

    assert factory.opened > before, 'the first request never reached the store'
    assert factory.outstanding == 0


def test_the_schema_ddl_runs_once_per_process(factory, monkeypatch):
    """CREATE TABLE on every refresh would put DDL on a public read path."""
    from flask import Flask

    monkeypatch.setattr(tir, '_OVERRIDE_TTL_SECONDS', 0)
    calls = []
    real_init = tir._init_db
    monkeypatch.setattr(tir, '_init_db', lambda d: (calls.append(1), real_init(d))[1])

    app = Flask('ddl-once')
    tir.setup_tax_incentive_routes(app, factory)
    with app.test_client() as client:
        for _ in range(4):
            client.get('/api/v1/tax-incentives')

    assert calls == [1], 'DDL ran %d times, expected once' % len(calls)


def test_a_request_to_another_route_does_not_touch_the_store(factory):
    """before_request is app-wide; it must do nothing for everyone else."""
    from flask import Flask

    app = Flask('other-route')

    @app.route('/api/v1/something-else')
    def _other():
        return {'ok': True}

    tir.setup_tax_incentive_routes(app, factory)
    before = factory.opened

    with app.test_client() as client:
        assert client.get('/api/v1/something-else').status_code == 200

    assert factory.opened == before, 'an unrelated route hit the override store'
