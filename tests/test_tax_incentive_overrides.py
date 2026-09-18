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

    tir.setup_tax_incentive_routes(Flask('boot-one'), db)
    tir.setup_tax_incentive_routes(Flask('boot-two'), db)

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
    tir.setup_tax_incentive_routes(Flask('boot-alias'), db)

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
