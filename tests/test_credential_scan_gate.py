"""The credential gate must catch a BARE credential, in a file git has not
tracked yet (2026-08-07).

scripts/check_no_leaked_credentials.py shipped with five URL-shaped rules
inherited from the urllib credential-in-URL work — USERINFO, PLACEHOLDER_PW,
RENDER_HOOK, SECRET_PARAM, PLACEHOLDER_VAL. Every one of them needs a
`scheme://` or a `?param=`, so two things walked straight past it:

  1. VALUE SHAPE — a bare assignment. Verified: a file holding the real
     DCHUB_ADMIN_KEY as `KEY = "<64 hex>"` scored exit 0, and six live
     production credentials nearly reached this PUBLIC repo inside a test
     fixture.
  2. FILE SELECTION — it listed `git ls-files` only, so a brand-new file was
     invisible to a local pre-commit run both before AND after `git add`.

`--self-test` inside the script fences (1) — it is a line-level check and runs
in syntax-check even when the suite is down. It cannot fence (2), because file
selection is the one thing a line-level fixture never exercises. That is what
test_staged_and_untracked_* below is for.

Every credential-shaped string below is FABRICATED — this repo is public — but
each matches a real one's length and alphabet, so each carries the
`secretscan:allow` pragma that the scanner itself honours. That pragma is the
reason this file does not need to be exempted from the scan wholesale.

The script is loaded by path rather than imported: it lives in scripts/, not
on sys.path, and it has no module-scope side effects.
"""

import hashlib
import importlib.util
import pathlib

_REPO = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "check_no_leaked_credentials.py"


def _load():
    spec = importlib.util.spec_from_file_location("_credscan", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _messages(mod, line):
    return [what for what, _ in mod._findings_in_line(line)]


# --------------------------------------------------------------------------
# 1. VALUE SHAPE — the bare-assignment gap
# --------------------------------------------------------------------------

def test_script_self_test_is_green():
    """The script's own fixtures must pass; if they do not, every assertion
    below is being made against a scanner that is already broken."""
    assert _load().self_test() == 0


def test_bare_assignment_of_each_leaked_credential_shape_is_caught():
    """The six credentials that nearly shipped, by shape. Values fabricated —
    this repo is public — but each matches the real one's length and alphabet.
    """
    mod = _load()
    for name, line in (
        ("DCHUB_ADMIN_KEY 64-hex",
         'DCHUB_ADMIN_KEY = "3f8a1d0c7b46e295af13d8be602c74195e0ab3d7c81f462a9d05e7b3c418f6a2"'),  # secretscan:allow
        ("DCHUB_INTERNAL_KEY 62-char base62",
         'DCHUB_INTERNAL_KEY = "Zq4mXt7BvR2nKp9Ls6Yd3Wg8Fc1Hj5Ua0Ei7Ob2Tr4Nm6Vx9Cz3Sw8Pk1Ql5Ay"'),  # secretscan:allow
        ("DCHUB_API_KEY dchub_live_ prefix",
         'DCHUB_API_KEY = "dchub_live_e94b17c0a3d6528f7b10c4e9d283a56f"'),  # secretscan:allow
        ("ADMIN_API_KEY 32-hex",
         'ADMIN_API_KEY = "b71e04c9d3a856f2e0b94c17d658a3f0"'),  # secretscan:allow
        ("INTERNAL_SYNC_SECRET base62",
         'INTERNAL_SYNC_SECRET = "j7Kq2mZ9vRt4Yx6Bn3Cd8Fg1Hj0Lp5Sw2Ae7Ui"'),  # secretscan:allow
        ("BRAIN_ADMIN_KEY 32-hex",
         'BRAIN_ADMIN_KEY = "5c0e93b17a4d268fc51b0e7d94a36f28"'),  # secretscan:allow
        # the shape the gap was actually reported as: no vendor prefix, no
        # descriptive name, just KEY
        ("bare KEY",
         'KEY = "b7c1e94a2f60d38b5a17ce4092fb63d8a4e7015c9b2d6f38e0a71c45d9b3f682"'),  # secretscan:allow
        # a hardcoded fallback, which is how most of this repo's live keys
        # are actually written
        ("env fallback",
         'os.environ.get("BRAIN_ADMIN_KEY", "4d81f0a6b39ec5271ca7e08b4f6d3925")'),  # secretscan:allow
    ):
        assert _messages(mod, line), (
            "%s passed the credential gate — this is the 2026-08-07 hole "
            "reopening" % name)


def test_documented_false_positive_classes_stay_silent():
    """The rule binds a value to a secret-shaped NAME on purpose. These three
    classes are high-entropy but unnamed or declared public, and a scan that
    cries wolf on them gets switched off within a week."""
    mod = _load()
    for label, line in (
        ("sha256 denylist pin",
         '    "e259f445efd0c1e77d7f682f1bf40c949a742a6d3261e5f097f71671b71ea4b3",'),
        ("git SHAs in prose",
         "See 4945c4b2 / 0a18af76a4d815ba99937f6c34de208c14b990b7f3a1e2d0"),
        ("Stripe publishable key",
         "publishableKey: 'pk_live_51Si61EJ9ey2ATcQlDsF7z9YzsBIkp4hsFYuHsk53Z'"),
    ):
        assert not _messages(mod, line), "false positive on %s" % label


def test_real_denylist_pins_and_fixture_files_are_clean():
    """Read the REAL files the false-positive classes live in, so this tracks
    the shipped source rather than a copy of it that can drift."""
    mod = _load()
    for rel in ("util/admin_auth.py",
                "tests/test_admin_credential_strength.py"):
        blocking, _ = mod.scan([rel])
        assert not blocking, (
            "%s now trips the credential gate at %s — its sha256 pins and "
            "same-shape synthetic fixtures must stay quiet"
            % (rel, [(b[1], b[2]) for b in blocking]))


# --------------------------------------------------------------------------
# 2. FILE SELECTION — the git ls-files gap
# --------------------------------------------------------------------------

def _stub_git(mod, monkeypatch):
    """Replace the git shell-outs with a fixed three-way answer so the path
    set can be asserted without a scratch repo."""
    answers = {
        ("ls-files", "-z"): ["tracked.py"],
        ("diff", "--cached", "--name-only", "-z", "--diff-filter=ACM"):
            ["staged_new.py"],
        ("ls-files", "--others", "--exclude-standard", "-z"): ["untracked.py"],
    }
    monkeypatch.setattr(mod, "_git", lambda *a: answers[a])


def test_staged_but_new_files_are_scanned_by_default(monkeypatch):
    """A file `git add`-ed but never committed is exactly what the near-miss
    fixture was. It must be in the default path set."""
    mod = _load()
    _stub_git(mod, monkeypatch)
    paths = mod._paths(include_untracked=False)
    assert "staged_new.py" in paths, (
        "staged-but-new files are invisible again — `git ls-files` alone was "
        "the second half of the 2026-08-07 gap")
    assert "tracked.py" in paths


def test_untracked_files_are_scanned_on_request(monkeypatch):
    """Untracked files stay OUT of the default (CI) set and IN the
    --untracked set, which is what a pre-commit hook should call."""
    mod = _load()
    _stub_git(mod, monkeypatch)
    assert "untracked.py" not in mod._paths(include_untracked=False)
    assert "untracked.py" in mod._paths(include_untracked=True)


# --------------------------------------------------------------------------
# 3. The KNOWN_EXPOSURES ledger must downgrade, never silence
# --------------------------------------------------------------------------

def test_ledger_entries_are_bare_sha256_with_a_note():
    mod = _load()
    for digest, note in mod.KNOWN_EXPOSURES.items():
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), (
            "%r is not a bare sha256 — the ledger must never restate a "
            "credential in cleartext" % digest)
        assert note.strip(), "ledger entry %s carries no rotation note" % digest


def test_a_ledgered_value_is_still_detected_by_a_rule(tmp_path, monkeypatch):
    """The ledger must reclassify a finding, not suppress the rule that makes
    it. Otherwise a rule could rot away and the ledger's own staleness check
    would be the thing that reported it, far too late."""
    mod = _load()
    value = "dchub_live_c1f47b0e93a6d258fc70b41e8d59a3b6"  # secretscan:allow
    digest = hashlib.sha256(value.encode()).hexdigest()
    probe = tmp_path / "ledgered.py"
    probe.write_text('KEY = "%s"\n' % value)

    monkeypatch.setitem(mod.KNOWN_EXPOSURES, digest, "fixture")
    blocking, known = mod.scan([str(probe)])
    assert not blocking and len(known) == 1, (
        "a ledgered value must report as a known exposure, got "
        "blocking=%r known=%r" % (blocking, known))

    monkeypatch.delitem(mod.KNOWN_EXPOSURES, digest)
    blocking, known = mod.scan([str(probe)])
    assert len(blocking) == 1 and not known, (
        "the same value must BLOCK once it is not ledgered — the ledger is "
        "the only thing that may downgrade it")


def test_ledger_does_not_forgive_a_different_credential(tmp_path):
    """Pinning is per-value. A new key must block even though eleven others
    are ledgered."""
    mod = _load()
    probe = tmp_path / "fresh.py"
    probe.write_text('ADMIN_KEY = "6e2b90f4a71c58d3e0b47f19c6a2d850"\n')  # secretscan:allow
    blocking, known = mod.scan([str(probe)])
    assert len(blocking) == 1 and not known


def test_one_value_tripping_two_rules_is_reported_once(tmp_path):
    """A `dch_live_…` literal matches both the named-secret and the vendor-
    prefix rule. Counting it twice inflates the exposure ledger."""
    mod = _load()
    probe = tmp_path / "double.py"
    probe.write_text('API_KEY = "dchub_live_e94b17c0a3d6528f7b10c4e9d283a56f"\n')  # secretscan:allow
    blocking, _ = mod.scan([str(probe)])
    assert len(blocking) == 1, "reported %d times: %r" % (len(blocking), blocking)
