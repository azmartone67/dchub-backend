"""No radar detector may read a module-global that does not exist.

The radar has now shipped this bug three times, and each one was invisible
for a different reason:

  * `logger` (2026-06-15) — NameError after a savepoint was released, so the
    trailing commit silently discarded every upsert: brain_findings froze for
    22h while the scan reported success. Guarded by
    tests/test_radar_logger_defined.py — but by NAME, only for `logger`.
  * `datetime` in check_event_submission_pending — NameError the moment the
    query returned rows, outside the query guard. See the r62-fix comment.
  * `_conn` in check_auto_trial_conversion_rate (#2152) — NameError on the
    detector's FIRST line, so scan_all() booked
    consistency_radar_detector_crashed:check_auto_trial_conversion_rate on
    every sweep and the auto-trial signup rate was never measured.

...plus a fourth this test found on the way in: a duplicated paid-key canary
in check_deploy_queue_churn read `_rnd`, `_req` and `canary`, none of which
were bound there, INSIDE a bare `except Exception: pass`. That one raised on
every run and was swallowed whole — a severity:critical "a paying user is
walled on the flagship map" probe that had never been able to fire.

★That last one is why this guard is STATIC rather than a call-every-detector
runtime test. A runtime sweep would have caught `_conn` (first line, outside
the try) and missed the canary entirely, because the radar's house style is
to wrap probes in `except Exception: pass` — swallowing the NameError is the
detector working as designed. It would also have to stub the DB, HTTP and
GitHub helpers ~128 detectors reach for, and any detector whose stub returned
an early-exit value would be scanned vacuously. This check has no such holes:
it asks the CPython compiler which names each function will look up in module
globals, and whether anything binds them.

Scope analysis is the compiler's, not ours — locals compile to LOAD_FAST and
closure variables to LOAD_DEREF, so a LOAD_GLOBAL is a module-global lookup
and nothing else. No import of main, no DB, no network.
"""
import builtins
import dis
import os

_RADAR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "routes", "brain_consistency_radar.py"))

# Injected into every module's globals by the interpreter; no STORE_* binds
# them, so the compiler cannot show them to us.
_MODULE_DUNDERS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__debug__",
}

# Opcodes that bind a name into module globals. STORE_NAME is the usual one,
# but CPython switches a module-level assignment to STORE_GLOBAL as soon as
# some function declares that same name `global` (observed on 3.14). Collect
# both, or every piece of module-level mutable state reads as undefined.
_BIND_OPS = ("STORE_NAME", "DELETE_NAME", "STORE_GLOBAL", "DELETE_GLOBAL")


def _lineno(ins):
    pos = getattr(ins, "positions", None)
    if pos is not None and getattr(pos, "lineno", None):
        return pos.lineno
    return getattr(ins, "line_number", None) or "?"


def _code_objects(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            yield from _code_objects(const)


def _undefined_globals(src, path, extra_names=()):
    """-> (violations, stats). A violation is (function, name, line)."""
    top = compile(src, path, "exec")
    bound = {i.argval for i in dis.get_instructions(top)
             if i.opname in _BIND_OPS}
    known = bound | set(extra_names) | set(dir(builtins)) | _MODULE_DUNDERS

    violations, n_code, n_loads, names = [], 0, 0, set()
    for code in _code_objects(top):
        if code is top:
            continue
        n_code += 1
        names.add(code.co_name)
        for ins in dis.get_instructions(code):
            if ins.opname != "LOAD_GLOBAL":
                continue
            n_loads += 1
            if ins.argval not in known:
                violations.append((code.co_name, ins.argval, _lineno(ins)))
    return violations, {"code_objects": n_code, "global_loads": n_loads,
                        "names": names}


# ── the guard's own mutation check, run on every invocation ───────────

def test_the_checker_can_actually_fail():
    """★A guard nobody has watched fail is decoration. This is the mutation
    check, wired in permanently rather than done once by hand: a synthetic
    module with a known-undefined global must be flagged, and its clean twin
    must not."""
    dirty = "def f():\n    return _no_such_helper()\n"
    found, _ = _undefined_globals(dirty, "<dirty>")
    assert [(fn, name) for fn, name, _ in found] == [("f", "_no_such_helper")]

    clean = "_no_such_helper = 1\n\n\ndef f():\n    return _no_such_helper()\n"
    found, _ = _undefined_globals(clean, "<clean>")
    assert found == []


def test_the_checker_catches_the_shape_that_shipped():
    """Both real shapes: an undefined name on the first line, and one buried
    inside `except Exception: pass` where no runtime sweep could see it."""
    src = (
        "def outside_the_try():\n"
        "    conn = _conn()\n"
        "    try:\n"
        "        return conn.go()\n"
        "    except Exception:\n"
        "        return []\n"
        "\n"
        "def swallowed():\n"
        "    try:\n"
        "        return _req.get(canary)\n"
        "    except Exception:\n"
        "        pass\n"
        "    return []\n"
    )
    found, _ = _undefined_globals(src, "<shipped>")
    assert {(fn, name) for fn, name, _ in found} == {
        ("outside_the_try", "_conn"),
        ("swallowed", "_req"),
        ("swallowed", "canary"),
    }


def test_the_checker_does_not_flag_ordinary_scoping():
    """The false-positive classes that would make this guard unlivable. Each
    one is a real pattern in routes/: closures, comprehensions, `global`
    module state, except-as, with-as, star-args."""
    src = (
        "import os\n"
        "_STATE = {}\n"
        "\n"
        "def outer(a, *args, **kw):\n"
        "    local = a + 1\n"
        "    def inner():\n"
        "        return local + a\n"
        "    for i in range(3):\n"
        "        local += i\n"
        "    with open(os.devnull) as fh:\n"
        "        fh.read()\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        del exc\n"
        "    return ([x for x in args],\n"
        "            {k: v for k, v in kw.items()},\n"
        "            inner(), _STATE)\n"
        "\n"
        "def mutate():\n"
        "    global _STATE\n"
        "    _STATE = {}\n"
        "    return _STATE\n"
    )
    found, _ = _undefined_globals(src, "<scoping>")
    assert found == [], found


# ── the guard itself ──────────────────────────────────────────────────

def test_no_radar_function_reads_a_name_that_does_not_exist():
    # Union the live module's globals with what the compiler shows us: a name
    # injected at import time is legitimately defined even though no STORE_*
    # in the source binds it. This also makes a future `from x import *`
    # safe rather than a wall of false positives.
    from routes import brain_consistency_radar as radar

    with open(_RADAR, encoding="utf-8") as fh:
        src = fh.read()
    violations, stats = _undefined_globals(src, _RADAR,
                                           extra_names=vars(radar))

    # ★Anti-vacuity floors. A refactor that split the radar up, or a checker
    # that quietly stopped walking nested code objects, would otherwise turn
    # this guard silently green — which is the failure mode it exists to
    # prevent.
    #
    # ★Keep these version-agnostic. The raw code-object count is NOT: the same
    # file compiles to 230 objects on 3.12 and 394 on 3.14, because PEP 649
    # gives every annotated function its own __annotate__ object. CI runs
    # 3.11-3.13 and this was authored on 3.14, so a floor set from the local
    # number alone (250) passed here and would have failed every CI job. The
    # detector count is the floor that actually means something and it does not
    # move: 128 on both. Global loads are stable too (1,608 vs 1,609).
    scanned = ("scanned %d code objects, %d global loads, %d check_* detectors"
               % (stats["code_objects"], stats["global_loads"],
                  len([n for n in stats["names"] if n.startswith("check_")])))
    assert len([n for n in stats["names"] if n.startswith("check_")]) >= 90, scanned
    assert stats["global_loads"] >= 1000, scanned
    assert stats["code_objects"] >= 150, scanned
    for must in ("check_auto_trial_conversion_rate", "check_deploy_queue_churn",
                 "scan_all"):
        assert must in stats["names"], f"{must} was not scanned"

    assert not violations, (
        "brain_consistency_radar.py reads module-globals that nothing binds. "
        "Every one of these raises NameError the moment the line executes — "
        "either crashing the detector (scan_all books "
        "consistency_radar_detector_crashed:<name> and the thing it measures "
        "goes unmeasured) or, worse, vanishing into an `except Exception: "
        "pass` so the detector reports clean forever:\n"
        + "\n".join(f"  line {ln}: {fn}() reads undefined `{name}`"
                    for fn, name, ln in violations))
