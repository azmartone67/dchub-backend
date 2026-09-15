"""/connect/<client>: a mint the validator will refuse shows the bind step.

r-connect-bind (2026-09-15). routes/auto_trial.mint_trial_for_request can hand
an install page a key that /api/v1/keys/validate refuses from its first call:

  * this network already holds an unbound key past its free calls, and the
    mint returns that same key (reused: true);
  * a fresh key minted from such a network is seeded with that count
    (notes "gate_carry:N", reused: false).

Both answers carry bind_required: true and gate: "bind_email_required". The MCP
server drops a refused key and serves the call anonymously, yet the page wrote
"<tier> tier · <n> req/day · <n>-day trial" and swapped the key into the
install snippet. Now a gated answer shows the bind step where the tier line
goes, and the key stays out of the snippet until an email is bound.

HOW, so that nothing here is a hand-typed mirror:

1. A Flask app serves the REAL auto-trial and connect blueprints on a loopback
   port. Only their database, mailer and mcp_dev_keys mirror are stubbed, so
   the mint answers come from the real mint_trial_for_request: rename
   bind_required there and these tests go red.
2. The page is the rendered template. Its script runs in node against a DOM
   built from the ids that HTML defines, so an id the script reads and the
   page lacks is null, as in a browser. Its fetch goes to the loopback app:
   every request the page sends reaches the real handler it names.
3. A gated key is still attributed to the view (mint-update). That is a
   decision, pinned here; the page script says why.

No network beyond loopback, no database. The bind email uses the reserved
.invalid TLD and reaches only the stubbed handler.

Run:  python3 -m pytest tests/test_connect_gated_mint_shows_bind_step.py -v
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading

import pytest
from flask import Flask
from werkzeug.serving import make_server

import canonical_stats as cs
import routes.auto_trial as at
import routes.mcp_connect as mc

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
VIEW_ID = 4242
BIND_EMAIL = "someone@example.invalid"
KEY_CLIENTS = sorted(k for k, c in mc._CLIENTS.items() if mc.TRIAL_KEY_SENTINEL in c["snippet"])

MINT = {"call": "mintKey"}
MINT_AGAIN = {"call": "mintKey", "args": [True]}
BIND = {"call": "bindEmail", "event": True}


def TYPE(value):
    return {"type": {"id": "bind-email", "value": value}}


def _existing_key():
    # Built, not typed: a key-shaped literal trips scripts/check_no_leaked_credentials.py.
    return "dch_trial_" + "r" * 32


# ── the database behind the real handlers ────────────────────────────────
class _Db:
    """Answers the statements the mint, bind and mint-update handlers run, by
    what they select. The mint path swallows exceptions (fail-open), so an
    unscripted statement is recorded and answers None rather than raising,
    and every test asserts there were none."""

    def __init__(self, gated=(), carried=0):
        self.expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5)
        # One answer per run of the gated-identity probe, in order; None after.
        self.gated = [(key, self.expires) if key else None for key in gated]
        self.carried = carried
        self.keys = {key for key in gated if key}
        self.executed = []
        self.unscripted = []

    def answer(self, sql, params):
        if sql.startswith("update auto_trial_keys set operator_email = %s,"):
            return (self.expires,) if params[2] in self.keys else None
        if sql.startswith("update connect_landing_views set key_minted_for"):
            return None
        if "coalesce(call_count, 0) >= %s" in sql:            # the gated-identity probe
            return self.gated.pop(0) if self.gated else None
        if "and request_ua = %s" in sql:                      # same ip + user agent
            return None
        if "max(coalesce(call_count, 0))" in sql:             # count carried from this ip
            return (self.carried,)
        if "from mcp_dev_keys" in sql:                        # ...and from its claim keys
            return (0,)
        if sql.startswith("insert into auto_trial_keys"):
            self.keys.add(params[0])
            return (self.expires,)
        self.unscripted.append(sql)
        return None


class _Cur:
    def __init__(self, db):
        self._db, self._row = db, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).lower()
        self._db.executed.append((norm, params))
        self._row = self._db.answer(norm, params)

    def fetchone(self):
        return self._row


class _Conn:
    autocommit = True

    def __init__(self, db):
        self._db = db

    def cursor(self):
        return _Cur(self._db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


SCENARIOS = {
    "reused": lambda: _Db(gated=[_existing_key()]),
    "born_gated": lambda: _Db(carried=at.TRIAL_FREE_CALLS_UNBOUND + 3),
}


def _attributed(db):
    return [(p[0], p[1]) for sql, p in db.executed
            if sql.startswith("update connect_landing_views set key_minted_for")]


def _bound(db):
    return [(p[0], p[2]) for sql, p in db.executed
            if sql.startswith("update auto_trial_keys set operator_email = %s,")]


def _seeded_calls(db):
    (params,) = [p for sql, p in db.executed if sql.startswith("insert into auto_trial_keys")]
    return params[7], params[8]


class _ValidatorDb:
    """validate_trial_key's SELECT, answered with an unbound live row."""

    def __init__(self, calls):
        self.row = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5),
                    None, None, 0, None, calls)
        self.sql = ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return self.row if self.sql.lstrip().upper().startswith("SELECT") else None

    def close(self):
        pass


def _app(monkeypatch, db):
    monkeypatch.setattr(at, "_conn", lambda: _Conn(db))
    monkeypatch.setattr(at, "_ensure_schema", lambda c: None)
    monkeypatch.setattr(at, "note_swallowed_write", lambda *a, **k: None)
    monkeypatch.setattr(at, "_mirror_trial_to_mcp_dev_keys", lambda key, email: None)
    monkeypatch.setattr(at, "_send_bind_receipt",
                        lambda key, email, name: {"armed": False, "sent": False})
    monkeypatch.setattr(mc, "_get_db", lambda: _Conn(db))
    app = Flask(__name__)
    app.register_blueprint(at.auto_trial_bp)
    app.register_blueprint(mc.mcp_connect_bp)
    return app


@pytest.fixture
def backend(monkeypatch):
    """start(db) -> base URL of the real blueprints, served on loopback."""
    servers = []

    def start(db):
        server = make_server("127.0.0.1", 0, _app(monkeypatch, db))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return "http://127.0.0.1:%d" % server.server_port

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def stats_state():
    """Restore canonical_stats' module cache, which rendering reads."""
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


# ── running the page ─────────────────────────────────────────────────────
_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

const els = {};
for (const [id, display] of Object.entries(cfg.ids)) {
  const classes = new Set();
  els[id] = {
    id, innerText: "", value: "", href: "", disabled: false,
    style: { display },
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c),
                 contains: (c) => classes.has(c) },
    classes,
  };
}
const requests = [];
const clarity = [];
const storage = {};
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const sandbox = {
  console: { log() {}, warn() {}, error() {} },
  setTimeout: () => 0,
  document: {
    getElementById: (id) => (own(els, id) ? els[id] : null),
    querySelectorAll: () => [],
  },
  navigator: { clipboard: { writeText: async () => {} } },
  localStorage: {
    getItem: (k) => (own(storage, k) ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
  },
  clarity: (...args) => { clarity.push(args); },
  fetch: async (url, opts) => {
    const o = opts || {};
    const method = o.method || "GET";
    const resp = await fetch(cfg.base + url, { method, headers: o.headers, body: o.body });
    const text = await resp.text();
    requests.push({ url, method, body: o.body === undefined ? null : o.body,
                    status: resp.status, response: text });
    return { ok: resp.ok, status: resp.status, json: async () => JSON.parse(text) };
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);

function snapshot() {
  const out = {};
  for (const [id, el] of Object.entries(els)) {
    out[id] = { text: el.innerText, display: el.style.display, disabled: el.disabled,
                value: el.value, classes: [...el.classes].sort() };
  }
  return out;
}

(async () => {
  const result = { snapshots: [], requests, clarity, storage, events: [], error: null };
  try {
    vm.runInContext(cfg.script, sandbox, { filename: "connect-page-script.js" });
    for (const step of cfg.steps) {
      if (step.type) els[step.type.id].value = step.type.value;
      if (step.call) {
        const ev = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
        await sandbox[step.call](...(step.event ? [ev] : (step.args || [])));
        if (step.event) result.events.push({ defaultPrevented: ev.defaultPrevented });
      }
      result.snapshots.push(snapshot());
    }
  } catch (e) {
    result.error = String((e && e.stack) || e);
  }
  process.stdout.write(JSON.stringify(result));
})();
"""

_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_TAG_WITH_ID = re.compile(r"<[a-zA-Z][^>]*?\sid=\"([^\"]+)\"[^>]*>")


def _page_script(html):
    start = html.rindex("<script>") + len("<script>")
    return html[start:html.index("</script>", start)]


def _page_ids(html):
    """{id: inline display} for every element the page's markup defines."""
    ids = {}
    for m in _TAG_WITH_ID.finditer(_SCRIPT_BLOCK.sub("", html)):
        style = re.search(r'\sstyle="([^"]*)"', m.group(0))
        display = re.search(r"display:\s*([a-z-]+)", style.group(1)) if style else None
        ids[m.group(1)] = display.group(1) if display else ""
    return ids


def _run_page(tmp_path, base, steps, client="chatgpt"):
    if NODE is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("node is not on PATH in CI, so the page script cannot run; "
                        "a skip here would pass the bind step without running it")
        pytest.skip("node is not on PATH")
    html = mc._render_page(client, VIEW_ID)
    cfg = tmp_path / "page.json"
    cfg.write_text(json.dumps({"base": base, "script": _page_script(html),
                               "ids": _page_ids(html), "steps": steps}))
    harness = tmp_path / "harness.cjs"
    harness.write_text(_HARNESS)
    run = subprocess.run([NODE, str(harness), str(cfg)], capture_output=True, text=True,
                         timeout=90)
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout)
    assert out["error"] is None, out["error"]
    return out


def _sent(out, path):
    return [r for r in out["requests"] if r["url"].split("?")[0] == path]


def _mint_answer(out, i=0):
    r = _sent(out, "/api/v1/keys/auto-mint")[i]
    assert r["status"] == 200, r
    return json.loads(r["response"])


# ── 0. what the page is fed ──────────────────────────────────────────────
def test_these_are_the_real_modules():
    """A stub another file left in sys.modules would let every test here pass
    against a fake."""
    assert pathlib.Path(at.__file__).resolve() == ROOT / "routes" / "auto_trial.py"
    assert pathlib.Path(mc.__file__).resolve() == ROOT / "routes" / "mcp_connect.py"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_real_mint_marks_a_key_the_validator_refuses(monkeypatch, scenario):
    """The precondition for every page test below: both gated shapes carry the
    marks, and the validator refuses the key before any call is made with it."""
    db = SCENARIOS[scenario]()
    r = _app(monkeypatch, db).test_client().post(
        "/api/v1/keys/auto-mint?tool=connect_landing&platform=chatgpt",
        json={"client_name": "chatgpt", "intended_use": "connect_landing_page"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["api_key"].startswith("dch_trial_"), j
    assert j["bind_required"] is True and j["gate"] == "bind_email_required", j
    assert j["free_calls_unbound"] == at.TRIAL_FREE_CALLS_UNBOUND
    assert j["daily_calls_when_email_bound"] == at.TRIAL_DAILY_CALLS
    assert j["reused"] is (scenario == "reused")
    assert not db.unscripted, db.unscripted
    if scenario == "reused":
        calls = at.TRIAL_FREE_CALLS_UNBOUND           # the least the probe admits
    else:
        calls, notes = _seeded_calls(db)
        assert notes.startswith("gate_carry:%d" % calls), notes
    monkeypatch.setattr(at, "_conn", lambda: _ValidatorDb(calls))
    assert at.validate_trial_key(j["api_key"]) == (False, "bind_email_required")


# ── 1. a clean mint keeps the tier line ──────────────────────────────────
def test_a_clean_mint_shows_the_tier_line_and_fills_the_snippet(backend, tmp_path, stats_state):
    db = _Db()
    out = _run_page(tmp_path, backend(db), [MINT])
    j = _mint_answer(out)
    assert "bind_required" not in j and "gate" not in j, j
    page = out["snapshots"][-1]
    key = j["api_key"]
    assert page["key-meta"]["display"] == "flex"
    assert "%d req/day" % j["daily_calls"] in page["key-meta"]["text"]
    assert page["bind-step"]["display"] == "none"
    assert key in page["snippet-body"]["text"]
    assert "paste in step 2" in page["mint-btn"]["text"]
    assert _attributed(db) == [(key, VIEW_ID)]
    assert not db.unscripted, db.unscripted


# ── 2. a gated mint shows the bind step in its place ─────────────────────
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("client", KEY_CLIENTS)
def test_a_gated_mint_shows_the_bind_step_instead_of_the_tier_line(
        backend, tmp_path, stats_state, client, scenario):
    db = SCENARIOS[scenario]()
    out = _run_page(tmp_path, backend(db), [MINT], client=client)
    j = _mint_answer(out)
    assert j["bind_required"] is True, j
    page = out["snapshots"][-1]
    key = j["api_key"]
    # The key is shown, so the person can see which key an email would bind to...
    assert page["key-box"]["text"] == key and "shown" in page["key-box"]["classes"]
    # ...but not as working: no tier line, the bind step where it would be.
    assert page["key-meta"]["display"] == "none"
    assert page["bind-step"]["display"] == "block"
    why = page["bind-why"]["text"]
    assert "%d free calls" % j["free_calls_unbound"] in why, why
    assert "%d req/day" % j["daily_calls_when_email_bound"] in why, why
    assert page["bind-btn"]["disabled"] is False
    # ...and not in the install snippet, which is copied as a working install.
    assert page["snippet-body"]["text"] == mc._CLIENTS[client]["snippet"]
    assert key not in page["snippet-body"]["text"]
    assert "paste" not in page["mint-btn"]["text"].lower()
    assert not _sent(out, "/api/v1/keys/auto-trial/bind"), "nothing binds until the person asks"
    # The decision: the page still attributes the key it handed out.
    assert _attributed(db) == [(key, VIEW_ID)]
    assert not db.unscripted, db.unscripted


@pytest.mark.parametrize("drop", ["bind_required", "gate"])
def test_either_mark_alone_shows_the_bind_step(backend, tmp_path, stats_state, monkeypatch, drop):
    """The page reads bind_required OR gate. Every gated branch sets both
    today; either one alone must still keep the key out of the snippet."""
    real = at.mint_trial_for_request

    def one_mark(*args, **kwargs):
        answer = real(*args, **kwargs)
        answer.pop(drop, None)
        return answer

    monkeypatch.setattr(at, "mint_trial_for_request", one_mark)
    db = SCENARIOS["born_gated"]()
    out = _run_page(tmp_path, backend(db), [MINT])
    j = _mint_answer(out)
    assert drop not in j and (j.get("bind_required") or j.get("gate")), j
    page = out["snapshots"][-1]
    assert page["bind-step"]["display"] == "block"
    assert page["key-meta"]["display"] == "none"
    assert j["api_key"] not in page["snippet-body"]["text"]


# ── 3. binding the email puts the same key to work ───────────────────────
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_binding_an_email_puts_the_same_key_in_the_snippet(backend, tmp_path, stats_state, scenario):
    db = SCENARIOS[scenario]()
    out = _run_page(tmp_path, backend(db), [MINT, TYPE(BIND_EMAIL), BIND])
    j = _mint_answer(out)
    key = j["api_key"]
    (bind,) = _sent(out, "/api/v1/keys/auto-trial/bind")
    assert bind["method"] == "POST"
    assert json.loads(bind["body"]) == {"api_key": key, "email": BIND_EMAIL}
    answer = json.loads(bind["response"])
    assert bind["status"] == 200 and answer["ok"] and answer["bound"], answer
    assert _bound(db) == [(BIND_EMAIL, key)], "the real handler must write the email onto that key"
    assert out["events"] == [{"defaultPrevented": True}], "the form must not navigate away"
    page = out["snapshots"][-1]
    assert page["key-meta"]["display"] == "flex"
    assert "%d req/day" % j["daily_calls_when_email_bound"] in page["key-meta"]["text"]
    if j["daily_calls"] != j["daily_calls_when_email_bound"]:
        assert "%d req/day" % j["daily_calls"] not in page["key-meta"]["text"]
    assert page["bind-form"]["display"] == "none"
    assert "done" in page["bind-step"]["classes"]
    snippet = page["snippet-body"]["text"]
    assert key in snippet and mc.TRIAL_KEY_SENTINEL not in snippet
    assert "https://dchub.cloud/mcp?apiKey=" + key in snippet
    assert "paste in step 2" in page["mint-btn"]["text"]
    # The address stays in its input: never page text, Clarity or storage.
    assert BIND_EMAIL not in json.dumps({k: v["text"] for k, v in page.items()})
    assert BIND_EMAIL not in json.dumps(out["clarity"]) + json.dumps(out["storage"])
    assert not db.unscripted, db.unscripted


def test_a_refused_email_leaves_the_bind_step_up(backend, tmp_path, stats_state):
    db = SCENARIOS["reused"]()
    out = _run_page(tmp_path, backend(db), [MINT, TYPE("not-an-email"), BIND])
    key = _mint_answer(out)["api_key"]
    (bind,) = _sent(out, "/api/v1/keys/auto-trial/bind")
    assert bind["status"] == 400, bind
    assert json.loads(bind["response"]) == {"error": "valid_email_required"}
    assert _bound(db) == []
    page = out["snapshots"][-1]
    assert page["bind-step"]["display"] == "block"
    assert page["bind-form"]["display"] != "none"
    assert page["bind-btn"]["disabled"] is False
    assert page["bind-status"]["text"]
    assert page["key-meta"]["display"] == "none"
    assert key not in page["snippet-body"]["text"]


# ── 4. minting again moves the page with the answer ──────────────────────
def test_a_clean_mint_after_a_gated_one_takes_the_bind_step_down(backend, tmp_path, stats_state):
    db = SCENARIOS["reused"]()            # the gated key is answered once
    out = _run_page(tmp_path, backend(db), [MINT, MINT_AGAIN])
    first, second = _mint_answer(out, 0), _mint_answer(out, 1)
    assert first.get("bind_required") and not second.get("bind_required")
    page = out["snapshots"][-1]
    assert page["bind-step"]["display"] == "none"
    assert page["key-meta"]["display"] == "flex"
    assert second["api_key"] in page["snippet-body"]["text"]


def test_a_gated_mint_after_a_clean_one_takes_the_key_out_of_the_snippet(
        backend, tmp_path, stats_state):
    db = _Db(gated=[None, _existing_key()])
    out = _run_page(tmp_path, backend(db), [MINT, MINT_AGAIN])
    first, second = _mint_answer(out, 0), _mint_answer(out, 1)
    assert not first.get("bind_required") and second.get("bind_required")
    assert first["api_key"] in out["snapshots"][0]["snippet-body"]["text"]
    page = out["snapshots"][-1]
    assert page["bind-step"]["display"] == "block"
    assert page["snippet-body"]["text"] == mc._CLIENTS["chatgpt"]["snippet"]
