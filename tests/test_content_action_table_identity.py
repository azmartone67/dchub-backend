"""Guard: an admin content action must mutate the table the operator was looking at.

WHAT THIS PINS
──────────────
`/api/admin/content/<id>/{approve,reject,edit}` served two tables through one
id. Both resolved the table from `request.args.get('type', 'social')` — and the
admin UI never sent `type` at all, so every action taken on a press row was
executed against `social_media_posts`.

That default is not conservative, because the two id spaces OVERLAP. Measured on
the live replica 2026-08-24 (`ep-dark-glade-af2837o8`, read-only):

    press_releases                                   196 rows
    social_media_posts                             2,466 rows
    press ids that ALSO exist as social ids           87

So approving press id 117 ("86 AI agents queried DC Hub's live power data…") ran

    UPDATE social_media_posts SET status='approved', approved_at=… WHERE id=117

against social id 117 — a PUBLISHED linkedin post about SPP/Kansas — and the UI
reported success. The other 109 press ids have no social twin, so they failed the
opposite way: rowcount 0 → 404 → "Failed to approve content", real row untouched.

Neither outcome is reachable through the press queue TODAY, because
`/api/admin/content-queue?type=press` still 500s on the same missing `status`
column (measured against the Railway origin the same day). This guard exists so
that fixing the queue — which is correct and wanted — cannot arm the hazard.

THE CONTRACT
────────────
  C1. The caller's DECLARED type is authoritative. An action route never
      mutates a table other than the one matching it.
  C2. With no declared type, an id present in BOTH tables is NOT actionable —
      409, and zero mutating statements issued. Guessing is the defect.
  C3. With no declared type and an id in exactly ONE table, that table is used.
      (Social-only ids keep behaving exactly as before; press-only ids stop
      reporting "Not found" for a row that plainly exists.)
  C4. An unrecognised `type` is refused, never silently downgraded to social.
  C5. `type=press` is refused EXPLICITLY on all three actions — press_releases
      has no approval model to run. Not a 500, not a write somewhere else.
  C6. No mutating statement in these routes interpolates its table name, so the
      table each branch can touch is readable off the source.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ 7c2be40a, swapped in over this branch, __pycache__
cleared, `hasattr(cp, "_resolve_content_table") is False` asserted first):
    12 failed, 5 passed          (pytest exit 1, read unpiped)
    The decisive failure is the hazard itself, reproduced verbatim:
        expected a refusal for an ambiguous id, got 200 {'success': True}
        approve on an untyped colliding id still wrote social_media_posts:
          [("UPDATE social_media_posts SET status = 'approved', approved_at = %s
             WHERE id = %s", ('2026-08-24T…Z', 117))]
    The 5 that pass unpatched pin facts the old code already satisfied:
        test_live_schema_press_has_no_social_action_columns
        test_live_id_spaces_overlap_so_a_default_type_cannot_be_safe
        test_social_only_id_still_works_untyped
        test_declared_social_wins_over_a_press_collision
        test_action_routes_only_ever_update_social_media_posts   ← see below
PATCHED (this branch):
    17 passed, 0 failed          (pytest exit 0)

★ test_action_routes_only_ever_update_social_media_posts is VACUOUS ON ITS OWN.
  It inspects string-literal SQL, and the old code built its UPDATEs as
  f"UPDATE {table} …" — an ast.JoinedStr, which that test skips, so it passed
  while the defect was live. It is meaningful only PAIRED with
  test_no_mutating_statement_interpolates_its_table_name, which is what fails on
  an interpolated table name. Never delete one and keep the other.

Run with:  python3 -m pytest tests/test_content_action_table_identity.py -v
"""
import ast
import os
from contextlib import contextmanager

import pytest

cp = pytest.importorskip("content_publisher")
flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(ROOT, "content_publisher.py")

# ── Live schema facts the contract rests on (measured 2026-08-24, replica) ────
LIVE_PRESS_COLS = {
    "id", "title", "summary", "source", "source_url", "category",
    "published_date", "featured", "created_at", "slug", "date", "subheadline",
    "body", "meta_description", "published", "published_at",
}
# The columns the social action model writes. press_releases has none of them.
SOCIAL_ACTION_COLS = {"status", "approved_at", "content"}
LIVE_OVERLAPPING_IDS = 87       # press ids that also exist as social ids
LIVE_PRESS_ROWS = 196
COLLIDING_ID = 117              # press "86 AI agents…" vs social published linkedin post

TABLE_OF = {"social_media_posts": "social", "press_releases": "press"}


# ── Fake DB: records every statement, answers existence from a chosen id space ──
class _Cur:
    def __init__(self, id_space):
        self.id_space = id_space
        self.executed = []
        self.rowcount = 0
        self._exists = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        head = flat.upper()
        if head.startswith("SELECT 1 FROM "):
            kind = TABLE_OF[flat.split()[3]]
            self._exists = params[0] in self.id_space[kind]
            self.rowcount = 1 if self._exists else 0
        elif head.startswith("UPDATE "):
            kind = TABLE_OF[flat.split()[1]]
            self.rowcount = 1 if params[-1] in self.id_space[kind] else 0
        else:
            self.rowcount = 0

    def fetchone(self):
        return (1,) if self._exists else None

    @property
    def mutations(self):
        return [(s, p) for s, p in self.executed
                if s.upper().startswith(("UPDATE ", "INSERT ", "DELETE "))]


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


@pytest.fixture
def bench(monkeypatch):
    """Flask test client over the real blueprint, with the DB faked.

    Default id space is the measured collision: id 117 lives in BOTH tables,
    202 is press-only, 303 is social-only.
    """
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-key")
    cur = _Cur({"social": {COLLIDING_ID, 303}, "press": {COLLIDING_ID, 202}})
    conn = _Conn(cur)

    @contextmanager
    def _fake_db_conn():
        yield conn

    monkeypatch.setattr(cp, "_db_conn", _fake_db_conn)
    app = flask.Flask(__name__)
    app.register_blueprint(cp.content_bp)
    client = app.test_client()

    def call(item_id, action, type_=None, body=None):
        url = f"/api/admin/content/{item_id}/{action}?key=test-key"
        if type_ is not None:
            url += f"&type={type_}"
        return client.post(url, json=body if body is not None
                           else {"content": "edited", "auto_approve": True})

    call.cur = cur
    call.conn = conn
    return call


def _mutated_tables(cur):
    return {s.split()[1] for s, _ in cur.mutations}


# ── The live facts ───────────────────────────────────────────────────────────

def test_live_schema_press_has_no_social_action_columns():
    """The deciding fact: there is no press approval model to run."""
    missing = SOCIAL_ACTION_COLS - LIVE_PRESS_COLS
    assert missing == SOCIAL_ACTION_COLS, (
        f"press_releases gained {sorted(SOCIAL_ACTION_COLS & LIVE_PRESS_COLS)} — "
        "if that is real, press may now have an approval model and C5 should be "
        "revisited rather than relaxed. Re-probe information_schema.columns.")
    assert "published" in LIVE_PRESS_COLS, (
        "press_releases lost `published` — the column the refusal message names "
        "as the real publish mechanism")


def test_live_id_spaces_overlap_so_a_default_type_cannot_be_safe():
    assert 0 < LIVE_OVERLAPPING_IDS <= LIVE_PRESS_ROWS
    assert LIVE_OVERLAPPING_IDS >= 1, (
        "if the id spaces ever stop overlapping, the wrong-table write becomes "
        "unreachable — but do not drop this guard, drop the overlap first")


# ── C1/C2/C3: which table an action is allowed to reach ──────────────────────

def test_colliding_id_is_not_actionable_without_a_declared_type(bench):
    """C2 — the exact 2026-08-24 hazard: press 117 vs social 117."""
    r = bench(COLLIDING_ID, "approve")
    assert r.status_code == 409, (
        f"expected a refusal for an ambiguous id, got {r.status_code} "
        f"{r.get_json()}")
    assert bench.cur.mutations == [], (
        f"an ambiguous action issued mutating SQL: {bench.cur.mutations}")
    assert bench.conn.commits == 0
    assert sorted(r.get_json().get("found_in", [])) == ["press", "social"]


@pytest.mark.parametrize("action", ["approve", "reject", "edit"])
def test_no_action_mutates_social_for_a_colliding_id(bench, action):
    """C1 — the silent corruption, pinned per action."""
    bench(COLLIDING_ID, action)
    assert "social_media_posts" not in _mutated_tables(bench.cur), (
        f"{action} on an untyped colliding id still wrote social_media_posts: "
        f"{bench.cur.mutations}")


@pytest.mark.parametrize("action", ["approve", "reject", "edit"])
def test_declared_press_never_writes_social(bench, action):
    """C1/C5 — an explicit press action is refused, and writes nothing."""
    r = bench(202, action, type_="press")
    assert r.status_code == 400, (
        f"press {action} returned {r.status_code}; expected an explicit refusal "
        f"(a 500 means the social UPDATE was aimed at press_releases)")
    assert bench.cur.mutations == [], bench.cur.mutations
    body = r.get_json()
    assert body["success"] is False
    assert action in body["error"]
    assert "status" in body["detail"] and "published" in body["detail"], (
        "the refusal must name what press_releases actually has, or the next "
        "reader re-derives the schema from scratch")


def test_press_only_id_resolves_to_press_instead_of_404ing_as_social(bench):
    """C3 — the 109-row failure mode: a real row reported 'Not found'."""
    r = bench(202, "approve")
    assert r.status_code == 400, (
        f"expected the press refusal, got {r.status_code} {r.get_json()}")
    assert r.get_json()["type"] == "press"
    assert bench.cur.mutations == []


def test_social_only_id_still_works_untyped(bench):
    """C3 — backward compatibility for every existing caller."""
    r = bench(303, "approve")
    assert r.status_code == 200, r.get_json()
    assert _mutated_tables(bench.cur) == {"social_media_posts"}
    assert bench.conn.commits == 1


def test_declared_social_wins_over_a_press_collision(bench):
    """C1 — a declared type is obeyed, not re-derived."""
    r = bench(COLLIDING_ID, "approve", type_="social")
    assert r.status_code == 200, r.get_json()
    assert _mutated_tables(bench.cur) == {"social_media_posts"}


def test_unknown_id_is_still_not_found(bench):
    r = bench(999999, "approve")
    assert r.status_code == 404
    assert bench.cur.mutations == []


def test_unknown_type_is_refused_not_defaulted_to_social(bench):
    """C4 — `type=all` is a real value the UI's own filter can produce."""
    r = bench(303, "approve", type_="all")
    assert r.status_code == 400, (
        f"type=all returned {r.status_code}; silently treating an unrecognised "
        "type as social is the same defect with a different spelling")
    assert bench.cur.mutations == []
    assert sorted(r.get_json()["expected"]) == ["press", "social"]


def test_edit_reports_whether_it_approved(bench):
    """The UI said 'edited & approved' on every success. Make it checkable."""
    r = bench(303, "edit", type_="social", body={"content": "x", "auto_approve": False})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["approved"] is False
    joined = " ".join(s for s, _ in bench.cur.mutations)
    assert "status = 'approved'" not in joined, (
        f"auto_approve=False still approved: {bench.cur.mutations}")


# ── C6: the table each branch can touch is readable off the source ───────────

def _action_functions():
    with open(SRC_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=SRC_PATH)
    assert len(tree.body) > 0, "content_publisher.py parsed to an EMPTY module"
    wanted = {"content_approve", "content_reject", "content_edit"}
    found = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name in wanted}
    assert set(found) == wanted, f"missing action routes: {wanted - set(found)}"
    return found


def test_no_mutating_statement_interpolates_its_table_name():
    """C6 — an f-string table name is how the wrong table got written."""
    offenders = []
    for name, fn in _action_functions().items():
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and node.args):
                continue
            sql = node.args[0]
            if isinstance(sql, ast.Constant) and isinstance(sql.value, str):
                continue
            rendered = ast.dump(sql).upper()
            if any(k in rendered for k in ("UPDATE ", "INSERT ", "DELETE ")):
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], (
        f"mutating SQL built by interpolation in {offenders} — the table a "
        "branch can write must be a literal, so it is greppable and cannot "
        "follow a mis-resolved type")


def test_action_routes_only_ever_update_social_media_posts():
    """C1, at the source: the press branches mutate nothing at all."""
    for name, fn in _action_functions().items():
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and node.args):
                continue
            sql = node.args[0]
            if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                continue
            flat = " ".join(sql.value.split())
            if flat.upper().startswith(("UPDATE ", "INSERT ", "DELETE ")):
                assert flat.split()[1] == "social_media_posts", (
                    f"{name}:{node.lineno} mutates {flat.split()[1]!r}; only the "
                    "social branch may mutate, and only social_media_posts")
