"""No tracked doc may instruct an operator to SEND a retired hardcoded key
(2026-09-12).

WHAT WENT WRONG
---------------
`dchub-internal-sync-2026` was a hardcoded admin bypass accepted on 50+ gates
until r-sec (2026-06-07) flipped internal_auth.LEGACY_OK to OFF. The key is
dead — is_valid_internal_key refuses it unless INTERNAL_AUTH_LEGACY_OK=1.

Nine tracked docs still printed it, six of them inside a runnable curl:

    STEP_BY_STEP.md   x4   (publish-now, registry submit-all, site-probe,
                            security-scan)
    USER_ACTIONS.md   x1   (the brain probe template)
    PATCHES/dchubapiproxy-add-render-failover.md  x1

Every one of those curls answered 401. Two were worse than stale: STEP_BY_STEP
told the reader "the submit-all endpoint accepts the legacy key", which stopped
being true in 2026-06, and both site-probe curls named an endpoint deleted
2026-08-29 (a POST to it answers 404, measured 2026-09-12).

WHY THE EXISTING GUARD DID NOT CATCH IT
---------------------------------------
routes/audit_closure_master_shell.py's `z_intkey` check exists for exactly this
debt item -- SH52-127, worded "Legacy internal-key literal ... re-documented in
current public-repo docs". But it reads three .py paths:

    for rel in ("flask_mcp_endpoints.py", "routes/admin_ai_deals.py",
                "routes/stripe_metered.py"):

those being the three files commit 2ff33803c happened to scrub. It never opens a
.md. So it reported "clean" — truthfully, about three files — for as long as the
docs carried the literal. A register keyed to the files one fix touched cannot
see the next instance of the class.

WHAT THIS TEST BANS
-------------------
The SEND shape only: the literal appearing as a credential value, in a header
or a query parameter. Naming the key in prose is how a retirement gets
documented and must stay legal -- SECURITY_KEY_ROTATION.md is *about* this key.

The banned strings are read from internal_auth._LEGACY_KEYS rather than typed
here, so retiring a third key extends this guard with no edit. A guard that
hardcodes the canon it asserts goes stale the moment the canon moves.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Derived, never typed: the set internal_auth actually refuses.
internal_auth = pytest.importorskip("internal_auth")
_RETIRED = tuple(internal_auth._LEGACY_KEYS)

# Files allowed to show a retired key inside a send shape, and WHY. Each entry
# is re-validated below: if the file stops containing one, the entry is stale
# and this suite fails, so the list can only shrink deliberately.
_ALLOWED_SEND = {
    # The retirement runbook. Its curl is a NEGATIVE check -- "Literal must now
    # be REJECTED (expect 403)" -- so the literal is the subject of the command,
    # not a credential the reader is meant to succeed with.
    "SECURITY_KEY_ROTATION.md",
}

# `-H "X-Internal-Key: <key>"`, `-H 'X-Admin-Key:<key>'`, `?admin_key=<key>`,
# `&key=<key>` -- the shapes that put the value on the wire.
def _send_patterns(key):
    k = re.escape(key)
    return (
        re.compile(r"[Xx]-(?:Internal|Admin)-Key\s*:\s*['\"]?\s*" + k),
        re.compile(r"[?&](?:admin_key|key|internal_key)\s*=\s*" + k),
    )


def _docs():
    out = []
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".git/", "node_modules/")):
            continue
        try:
            out.append((rel, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def _offenders():
    hits = []
    for rel, text in _docs():
        if rel in _ALLOWED_SEND:
            continue
        for key in _RETIRED:
            for pat in _send_patterns(key):
                for m in pat.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    hits.append((rel, line, key))
    return hits


# ── vacuity: this guard's likeliest failure is finding nothing to read ──

def test_the_scan_reads_the_docs_and_knows_the_keys():
    """A repo-glob guard is green at zero. Pin a floor on both inputs: the docs
    it walks, and the key set it walks them for."""
    docs = _docs()
    assert len(docs) > 20, f"only found {len(docs)} tracked .md files"
    names = {rel for rel, _ in docs}
    for required in ("STEP_BY_STEP.md", "USER_ACTIONS.md",
                     "SECURITY_KEY_ROTATION.md"):
        assert required in names, f"{required} was not walked; scan is misrooted"
    assert len(_RETIRED) >= 2, (
        f"internal_auth._LEGACY_KEYS has {len(_RETIRED)} entries; this guard "
        "has nothing to look for"
    )


def test_the_send_pattern_actually_matches_a_send():
    """The pattern is the whole guard. If it matches nothing, every assertion
    below passes for the wrong reason -- so exercise it on a known send and a
    known mention, using a real retired key."""
    key = _RETIRED[0]
    sends = (f'-H "X-Internal-Key: {key}"',
             f"-H 'X-Admin-Key:{key}'",
             f"https://dchub.cloud/api/v1/x?admin_key={key}")
    mentions = (f"`{key}` was a hardcoded admin-bypass accepted on 50+ gates",
                f"the {key} literal stays in git history forever")
    pats = _send_patterns(key)
    for s in sends:
        assert any(p.search(s) for p in pats), f"send shape not matched: {s}"
    for m in mentions:
        assert not any(p.search(m) for p in pats), f"prose wrongly matched: {m}"


# ── the rule ──

def test_no_doc_tells_an_operator_to_send_a_retired_key():
    offenders = _offenders()
    assert not offenders, (
        "tracked doc(s) instruct sending a RETIRED hardcoded key, which "
        "internal_auth refuses unless INTERNAL_AUTH_LEGACY_OK=1 — the curl "
        "answers 401 and reads as a broken service:\n"
        + "\n".join(f"  {rel}:{line} -> {key}" for rel, line, key in offenders)
        + "\nSend a value from the env ($DCHUB_INTERNAL_KEY / $DCHUB_ADMIN_KEY)."
    )


def test_every_allowed_file_still_needs_its_exemption():
    """An allowlist nobody has to maintain stops describing anything. If a file
    no longer shows a retired key in a send shape, its entry is stale — delete
    it in the same change that cleaned the file."""
    stale = []
    for rel in sorted(_ALLOWED_SEND):
        path = ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (file is gone)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(p.search(text) for key in _RETIRED
                   for p in _send_patterns(key)):
            stale.append(f"{rel} (no send shape left)")
    assert not stale, (
        "_ALLOWED_SEND entries no longer describe reality: " + ", ".join(stale)
    )
