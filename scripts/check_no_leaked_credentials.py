#!/usr/bin/env python3
"""Block credential-shaped strings from entering tracked files.

WHY THIS EXISTS
───────────────
CONFIG_SNAPSHOT.md shipped a live Railway Redis password and a Render
deploy-hook key to the PUBLIC repo on 2026-06-13 (commit 4945c4b2). It was
flagged on 2026-07-24 as "rotate then redact" and was still there, live, on
2026-08-07. Old SHAs remain reachable forever, so redaction alone fixes
nothing — the only durable fix is to stop the NEXT credential at the gate.

WHAT IT CATCHES (in every tracked file):
  1. Connection URLs with real userinfo:   redis://user:PASSWORD@host
                                           postgres://user:PASSWORD@host  etc.
  2. Render deploy hooks with their key:   api.render.com/deploy/srv-…?key=…
  3. URLs carrying a long secret param:    https://…?api_key=<16+ chars>
  4. A secret-shaped NAME bound to a high-entropy VALUE:
                                           ADMIN_KEY = "<64 hex>"
                                           os.environ.get("X_TOKEN", "<62>")
  5. Known vendor key prefixes:            dchub_live_…  sk-…  ghp_…  npg_…

Rules 1-3 are URL shapes, inherited from the urllib credential-in-URL work.
They all require a `scheme://` or a `?param=`, so a bare assignment slipped
straight through: verified on 2026-08-07, a file holding the real
DCHUB_ADMIN_KEY as `KEY = "<64 hex>"` passed this gate with exit 0. Rules 4
and 5 close that.

WHY RULE 4 NEEDS A NAME AND DOES NOT JUST MATCH ENTROPY
───────────────────────────────────────────────────────
Measured on this tree before the rule was written: a context-free "any hex run
>= 32" matches 543 places, and essentially none are credentials — 168 are
decimal certificate serials in vendored certifi, and the rest are ArcGIS
dataset ids, ChatGPT g-… ids, Cloudflare account/zone ids, cdn-cgi email
obfuscation, PDF form-object names and `<!-- fingerprint: -->` comments. A
scan that noisy gets switched off within a week. Binding the run to a
secret-shaped identifier takes it to 32 hits, essentially all true.

That name requirement is also what keeps the two documented false-positive
classes quiet without a single pragma: the sha256 denylist pins in
util/admin_auth._COMPROMISED_SHA256 and every 40-hex git SHA in the docs are
bare literals with no `KEY =` in front of them.

WHAT IT DELIBERATELY IGNORES:
  - placeholder passwords (user:pass@, YOUR_PASS@, :...@, ${VAR}@, {var}@)
  - localhost / 127.0.0.1 targets (CI scratch containers)
  - placeholder param values (YOUR_*, SENTINEL*, EXAMPLE*, FAKE*, …)
  - values with no digit or no letter, under 10 distinct chars, or under
    3.0 bits/char of entropy — filler like "xxxxxxxx1", not a generated key
  - names that say they are public (publishable_key, sentry-public_key) —
    a Stripe pk_live_ is meant to ship in client JS
  - any line carrying the pragma  secretscan:allow
  - this file itself (its self-test embeds credential-shaped fixtures)

KNOWN_EXPOSURES is the ledger for credentials that were ALREADY public when
this rule shipped. They warn instead of blocking, pinned by sha256 so the
fence never restates the secret (same convention as
util/admin_auth._COMPROMISED_SHA256). Two properties keep it from becoming a
dumping ground: every entry prints a ::warning on every run, and an entry that
no longer matches anything fails the scan, so the ledger has to shrink as
credentials are rotated out. Adding to it is a security decision, not a
refactor — a NEW credential must be rotated, never ledgered.

Run `--self-test` first in CI: it proves every pattern still FIRES on a
known-bad fixture and stays SILENT on the known-good ones, so a regex edit
cannot quietly turn the scan vacuous (the fence-goes-green-by-dying class).
"""
import collections
import hashlib
import math
import re
import subprocess
import sys

PRAGMA = "secretscan:allow"
SELF = "scripts/check_no_leaked_credentials.py"

# 1. scheme://user:password@host — flag only when the password looks real.
USERINFO = re.compile(
    r"\b(?:redis|rediss|postgres|postgresql|mysql|mongodb|amqps?)"
    r"(?:\+[a-z0-9]+)?://"
    r"([^:/\s\"'@]{1,64}):([^@/\s\"']{1,128})@([^/\s\"'>\)\]]+)", re.I)

# Passwords that are documentation, not credentials.
PLACEHOLDER_PW = re.compile(
    r"^(?:pass(?:word)?\d*|pw|secret|changeme|change_me|\.{2,}|x{3,}"
    r"|YOUR_\w*|<[^>]*>|\*{2,}|%s|\$.*|\{.*)$", re.I)

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")

# 2. A Render deploy hook URL that includes its key. There is no legitimate
#    placeholder form of this — the srv id + key= only exist together in a
#    real hook.
RENDER_HOOK = re.compile(
    r"api\.render\.com/deploy/srv-[A-Za-z0-9]+\?key=[A-Za-z0-9_-]{6,}")

# 3. A URL whose query string carries a long secret-named param.
SECRET_PARAM = re.compile(
    r"https?://[^\s\"'<>]*[?&]"
    r"(?:api_?key|apikey|access_token|auth_token|admin_key|client_secret)"
    r"=([A-Za-z0-9_\-\.]{16,})", re.I)

PLACEHOLDER_VAL = re.compile(
    r"^(?:YOUR_|SENTINEL|EXAMPLE|PLACEHOLDER|CHANGE_?ME|TEST_|FAKE|DUMMY|XXX)",
    re.I)

# 4. A secret-shaped identifier bound to a high-entropy value.
#
#    The name may be the bare keyword — `KEY = "<64 hex>"` was the shape that
#    slipped through, so the prefix before the keyword is optional-width.
#    Separators cover  X = v   X: v   X => v   and  get("X", "v"), which is
#    how a hardcoded fallback credential is usually written.
_SECRET_NAME = (r"[A-Za-z0-9_.\-]{0,40}?"
                r"(?:api[_\-]?key|key|secret|token|password|passwd|pwd"
                r"|credential|auth)")
NAMED_SECRET = re.compile(
    r"(?<![A-Za-z0-9_])(?P<name>" + _SECRET_NAME + r")"
    r"[\"'`\]]?\s*(?:[:=]{1,2}>?|,)\s*[\"'`]?"
    r"(?P<val>[A-Za-z0-9_\-]{32,200})(?![A-Za-z0-9_\-])", re.I)

# Names that announce the value is meant to be public. A Stripe publishable
# key and a Sentry public_key both ship in client-side JS by design.
NOT_SECRET_NAME = re.compile(r"public|publishable|pubkey", re.I)

# 5. Vendor key prefixes. These are self-identifying, so no name is needed.
#    pk_live_/pk_test_ are deliberately absent — Stripe publishable keys are
#    public by design. sk_test_/dchub_test_ are absent for the same reason a
#    test key is not a credential.
VENDOR_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_\-])(?P<pfx>"
    r"dchub_(?:live|pro|qa|dev|developer|owner)_|dch_(?:live|trial)_"
    r"|sk-|sk_live_|rk_live_|whsec_|cfut_"
    r"|ghp_|gho_|ghs_|github_pat_|re_|rnd_|npg_"
    r"|xoxb-|xoxp-|glpat-|shpat_)"
    r"(?P<val>[A-Za-z0-9_\-]{12,200})(?![A-Za-z0-9_\-])")

# A generated secret has both letters and digits, spends a reasonable number
# of distinct symbols, and carries real per-character entropy. 64 hex measures
# ~3.9 bits/char; "dchub_live_xxxxxxxxxxxx" and "ghp_faketoken" do not clear
# the letter/digit bar at all.
_MIN_DISTINCT_CHARS = 10
_MIN_ENTROPY_BITS = 3.0


def _entropy_bits_per_char(value):
    counts = collections.Counter(value)
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_generated(value):
    """True when `value` reads as a generated secret rather than prose,
    filler or a documented placeholder."""
    if not (re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)):
        return False
    if PLACEHOLDER_VAL.match(value):
        return False
    if len(set(value)) < _MIN_DISTINCT_CHARS:
        return False
    return _entropy_bits_per_char(value) >= _MIN_ENTROPY_BITS


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────────────
# Credentials that were ALREADY public in this repo when rules 4 and 5
# shipped (2026-08-07). Pinned by sha256 so this file does not restate them.
# These WARN; they do not block, or the gate could never have landed.
#
# Each of these needs rotating at the vendor, not deleting from HEAD — the
# old SHAs stay reachable forever. Delete the entry here once the credential
# is rotated AND its literal is gone from the tree; a stale entry fails the
# scan on purpose.
# ──────────────────────────────────────────────────────────────────────────
KNOWN_EXPOSURES = {
    "f22dd328c3eb6349b23790a6c070d8b9413cfdd4e17d02a08011e1415a47ac20":
        "PJM_QUEUE_KEY fallback, 2 files — code calls it a public website "
        "constant; confirm with PJM, then pragma or rotate",
    "252398b0097dd725eee985eeade141fcd5de3337ef0ef3c1e65e694e9a563a97":
        "PJM_DM2_KEY fallback in routes/iso_lmp_ingest.py — per-account Data "
        "Miner 2 subscription key, rotate at PJM",
    "680baedd8cc1d3142a26e654916942bb580b5d278f8a8bfb6c234bb7ec240ee9":
        "UptimeRobot API key in setup_uptimerobot.py — rotate at uptimerobot",
    "c91ffd9c31f6dee291206ea68c875855f2675cf87fecc6c3c3627725ae14b593":
        "DC Hub dch_live_ key in dchub-mcp-v2.1/finish_v21.sh — revoke",
    "5f7e102827d13a2c5418ba24b6effd88e71cb1fe4cd67d56efe6c2df8698e1c4":
        "DC Hub dch_trial_ key in docs/MCP_AUTO_TRIAL.md — revoke",
    "17956fc20875ae6f90e55614307986dd9e4c03946913d1ca2549f9d372ba808f":
        "CUSTOMER developer key (GABE_KEY) in docs/NLR_* — revoke and reissue",
    "f5abdf2dd28bfd41317cee65df36786b9e77e84e3a6903ea8864d6f010260977":
        "CUSTOMER developer key (GALEN_KEY) in docs/NLR_* — revoke and reissue",
    "7bd7284fa72b37850d8d5d88fb71e56d05f4e44840eecf67680d5e2ea4a86c22":
        "CUSTOMER developer key (IAN_KEY) in docs/NLR_* — revoke and reissue",
    "bdb3f5f05fcc507f1bc23117c8cdf081fe68e55da26a9227fd15fbecad23805c":
        "owner DCHUB_API_KEY literal in deploy-v47-mega.sh — revoke",
    "80e2585ac01f06d97a85fe5df36bf4319f06382d4c314a72c8a5102b9ff63508":
        "Neon DB password quoted in HANDOFF_2026-04-29.md's own "
        "'needs rotation' list — confirm it was rotated",
}


# Cheap prefilters. Each is IMPLIED by the regex it guards, so gating on it
# cannot change the result — it only skips lines that could never match. This
# is not premature: the tree is 4.3M lines / 376MB, and NAMED_SECRET's
# variable-width prefix costs 22s across it unfiltered versus 0.4s behind
# _LONG_RUN, which only 0.6% of lines clear. Keep every gate a strict
# consequence of its pattern; if you widen a pattern, widen or drop its gate.
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-]{32,}")   # NAMED_SECRET's val is {32,}


def _findings_in_line(line):
    """Return [(message, secret_value_or_None)] for one line of text."""
    out = []
    if PRAGMA in line:
        return out
    has_scheme = "://" in line          # USERINFO and SECRET_PARAM both need it
    for m in (USERINFO.finditer(line) if has_scheme else ()):
        user, pw, host = m.group(1), m.group(2), m.group(3)
        if PLACEHOLDER_PW.match(pw):
            continue
        if pw == user:  # postgres:postgres@ — CI scratch idiom
            continue
        if host.split(":")[0].lower() in LOCAL_HOSTS:
            continue
        out.append((f"connection URL embeds a password ({m.group(0)[:40]}…)",
                    pw))
    for m in (RENDER_HOOK.finditer(line) if "render.com" in line else ()):
        out.append(("Render deploy hook with its key", m.group(0)))
    for m in (SECRET_PARAM.finditer(line) if has_scheme else ()):
        if PLACEHOLDER_VAL.match(m.group(1)):
            continue
        out.append(("URL carries a long secret query param "
                    f"({m.group(1)[:8]}…)", m.group(1)))
    for m in (NAMED_SECRET.finditer(line) if _LONG_RUN.search(line) else ()):
        name = m.group("name")
        if NOT_SECRET_NAME.search(name):
            continue
        # `${VAR:-default}` leaves the '-' of ':-' at the head of the value.
        value = m.group("val").lstrip("-")
        if not _looks_generated(value):
            continue
        out.append((f"'{name}' is bound to a {len(value)}-char high-entropy "
                    f"value ({value[:4]}…)", value))
    for m in VENDOR_PREFIX.finditer(line):
        if not _looks_generated(m.group("val")):
            continue
        token = m.group("pfx") + m.group("val")
        out.append((f"'{m.group('pfx')}' vendor key prefix with a "
                    f"{len(token)}-char value", token))
    return out


def _scan_blob(path, blob, blocking, known):
    """Sort one file's bytes into the blocking/known buckets, in place."""
    if b"\0" in blob[:8192]:  # binary
        return
    text = blob.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        # One value can trip two rules — a `dch_live_…` literal matches
        # both the named-secret and the vendor-prefix rule. Report the
        # LINE once per distinct value, or the ledger's line count
        # double-counts and reads as more exposure than there is.
        seen_here = set()
        for what, value in _findings_in_line(line):
            digest = _sha256(value) if value is not None else None
            if digest in seen_here:
                continue
            seen_here.add(digest)
            row = (path, lineno, what, digest)
            (known if digest in KNOWN_EXPOSURES else blocking).append(row)


def scan(paths):
    """Return (blocking, known) findings as (path, lineno, message, sha)."""
    blocking, known = [], []
    for path in paths:
        if path == SELF:
            continue
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        _scan_blob(path, blob, blocking, known)
    return blocking, known


def scan_staged():
    """Scan STAGED CONTENT — what this commit would actually record.

    Reads each blob from the INDEX, not the worktree. `git add`ing a
    credential and then editing it back out of the working copy must not
    produce a green hook while the commit still carries the value.

    Deliberately does NOT run the stale-KNOWN_EXPOSURES check that main()
    performs. That invariant is only meaningful across the full tracked set
    (see _paths); against the handful of files in one commit every ledger
    entry would look stale and the hook would block every commit — a guard
    that cries wolf gets uninstalled, which is worse than not shipping it.
    """
    blocking, known = [], []
    for path in _git("diff", "--cached", "--name-only", "-z",
                     "--diff-filter=ACM"):
        if path == SELF:
            continue
        out = subprocess.run(["git", "show", f":{path}"], capture_output=True)
        if out.returncode != 0:
            continue
        _scan_blob(path, out.stdout, blocking, known)
    return blocking, known


def _git(*args):
    out = subprocess.run(["git", *args], capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def _paths(include_untracked):
    """Tracked files, plus anything staged (so a staged-but-NEW file is
    visible to a local pre-commit run), plus optionally untracked files.

    The tracked-only listing was the second half of the 2026-08-07 gap: a
    brand-new file holding six live credentials was invisible to this script
    both before AND after `git add`, because `git ls-files` lists the index's
    tracked set from HEAD's point of view.
    """
    paths = list(_git("ls-files", "-z"))
    paths += _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACM")
    if include_untracked:
        paths += _git("ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(paths))


def self_test():
    """Every pattern must FIRE on bad fixtures and stay SILENT on good ones."""
    # Split literal ON PURPOSE. tests/test_brain_ascension_shell.py greps the
    # whole tree for `dchub_(pro|live)_[A-Za-z0-9]{20,}` and skips only
    # tests/ — as one literal, this fabricated fixture fails that guard. The
    # fix is to keep the fixture unrecognisable to a grep, not to buy this
    # file an exclusion: an exclusion would leave BOTH credential guards
    # blind to the one file already exempt from its own scan (see SELF).
    fake_dchub_key = "dchub_live_" + "c1f47b0e93a6d258fc70b41e8d59a3b6"
    bad = [
        # each of these is the real leak SHAPE with a fabricated value
        "REDIS_URL=redis://default:uMBeYfakefakefakefakefake@caboose.proxy.rlwy.net:29436",
        "url = 'postgres://neondb_owner:npg_FAKEFAKE1234@ep-x.aws.neon.tech/db'",
        "hook: https://api.render.com/deploy/srv-abc123def456?key=FaKeKey99",
        "curl https://api.eia.gov/v2/x?api_key=Qz8LmNoP1234567890AbCdEf&f=a",
        # rule 4 — the 2026-08-07 gap, verbatim shape: a bare assignment.
        # 64 hex, 40-char base62, base64url, a `,`-separated env fallback,
        # a leading-underscore module constant, and a YAML mapping.
        'KEY = "b7c1e94a2f60d38b5a17ce4092fb63d8a4e7015c9b2d6f38e0a71c45d9b3f682"',
        "ADMIN_API_KEY=9f3b71ce05a248d6b9e4370f1ac82d56",
        'TOKEN: "Kp7mZq2VrL9wXt4BnH6sJd3Yg8Fc5TvA1eUiO0aD"',
        "_INTERNAL_KEY = 'z-K4TnWq_RmXbP7yHscE2vJdLgNfA9UeCtY6ZoQiBx0'",
        'os.environ.get("BRAIN_ADMIN_KEY", "4d81f0a6b39ec5271ca7e08b4f6d3925")',
        "  admin_key: 5e2a9c74b18df063a52e7c91048bf3d6",
        # rule 5 — vendor prefixes with no name in front of them
        fake_dchub_key,
        "Authorization: Bearer ghp_A9fK2mQ7zR4tV6xB1nC3dE5gH8jL0pS2uW4y",
        "curl -H 'x-key: npg_Rt7vQ2mXp9La4Zc1'",
        "RESEND=re_8Kq2mZ7vRt4Yx9Bn3Cd6Fg1Hj0Lp5Sw",
    ]
    good = [
        "postgresql://user:pass@ep-xxx.aws.neon.tech/dchub?sslmode=require",
        "export DATABASE_URL='postgresql://neondb_owner:...@ep-old.neon.tech/neondb'",
        "postgresql://neondb_owner:YOUR_PASS@ep-xxx.azure.neon.tech/neondb",
        "RESTORE_TARGET_URL: postgres://postgres:postgres@localhost:5432/restore_test",
        'replit_url = f"postgresql://{pguser}:{pgpass}@{pghost}/{pgdb}"',
        "redis://default:${REDIS_PASSWORD}@host:6379",
        'curl "https://dchub.cloud/api/agent/stats?api_key=YOUR_DCHUB_KEY"',
        'url = "https://example.com/?api_key=SENTINEL_AKEY_0123456789"',
        "redis://default:realFakePassword123@host:6379  # " + PRAGMA,
        # rule 4 must not fire on the two documented false-positive classes:
        # a bare sha256 denylist pin, and a git SHA. Neither has a name bound
        # to it, which is exactly why the rule requires one.
        '    "e259f445efd0c1e77d7f682f1bf40c949a742a6d3261e5f097f71671b71ea4b3",',
        "See commit 4945c4b2 / 0a18af76a4d815ba99937f6c34de208c14b990b7f3a1e2",
        # …nor on an identifier that says the value is public by design
        "publishableKey: 'pk_live_51Si61EJ9ey2ATcQlDsF7z9YzsBIkp4hsFYuHsk53Z'",
        "BAGGAGE = sentry-public_key=2f98127cbffe4740b1f767a2de77d23b",
        # …nor on values that are filler rather than generated secrets
        'KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"',
        'API_KEY = "YOUR_DCHUB_API_KEY_GOES_HERE_0123"',
        'token = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa9"',
        # rule 5 must not fire on ordinary identifiers that merely start with
        # a vendor prefix — `re_` was the worst of these (re_engagement,
        # re_highlight appear ~50 times in this tree)
        "def re_highlight(self, re_engagement, sk-x-altquot): pass",
        "GITHUB_TOKEN=ghp_faketoken",
        'assert key == "dchub_pro_YOUR_KEY_HERE"',
        # …nor on a real-shaped value that carries the pragma
        'KEY = "b7c1e94a2f60d38b5a17ce4092fb63d8"  # ' + PRAGMA,
    ]
    failures = []
    dead = [s for s in bad if not _findings_in_line(s)]
    noisy = [(s, _findings_in_line(s)) for s in good if _findings_in_line(s)]
    if dead:
        failures.append("pattern no longer fires on:\n" +
                        "\n".join(f"    {s}" for s in dead))
    if noisy:
        failures.append("false positive on known-good:\n" +
                        "\n".join(f"    {s} -> {h}" for s, h in noisy))

    # The ledger must downgrade, not silence. A value whose sha256 is pinned
    # must still be FOUND by a rule — otherwise a rule could rot away and the
    # ledger's staleness check would be what reports it, far too late.
    hits = _findings_in_line(fake_dchub_key)
    if not hits or _sha256(hits[-1][1]) in KNOWN_EXPOSURES:
        failures.append("ledger probe: a fabricated key must be found and "
                        "must NOT be pre-ledgered")
    if not all(re.fullmatch(r"[0-9a-f]{64}", k) for k in KNOWN_EXPOSURES):
        failures.append("KNOWN_EXPOSURES keys must be bare sha256 digests")

    if failures:
        print("SELF-TEST FAILED — " + "\n".join(failures), file=sys.stderr)
        return 1
    print(f"self-test ok: {len(bad)} bad fixtures fire, "
          f"{len(good)} good fixtures stay silent, "
          f"{len(KNOWN_EXPOSURES)} ledgered exposures")
    return 0


def _staged_main():
    """Local pre-commit path — refuse the commit before the value can be pushed.

    Known exposures are REPORTED but do not block: they are already public,
    and blocking on them would stop every commit that touches those files
    until rotation lands, which is how a guard gets uninstalled.
    """
    blocking, known = scan_staged()
    for path, lineno, what, digest in known:
        print(f"  {path}:{lineno}: known exposure (already public) — "
              f"{KNOWN_EXPOSURES[digest]}", file=sys.stderr)
    for path, lineno, what, _ in blocking:
        print(f"  {path}:{lineno}: {what}", file=sys.stderr)
    if not blocking:
        return 0
    print(f"\npre-commit blocked: {len(blocking)} credential-shaped string(s) "
          f"in staged content.\n"
          f"  This repo is PUBLIC. Pushing the branch publishes the value, and\n"
          f"  CI's syntax-check does not run until after that push.\n"
          f"  Rotate anything real FIRST (old SHAs stay public forever), then\n"
          f"  remove it. Genuine placeholder? Use a password PLACEHOLDER_PW\n"
          f"  already accepts (pw, pass, secret, xxx…) rather than reaching for\n"
          f"  '{PRAGMA}', which switches that line off for EVERY rule.\n"
          f"  Bypass (only if you are sure): git commit --no-verify",
          file=sys.stderr)
    return 1


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if "--staged" in argv:
        return _staged_main()
    include_untracked = "--untracked" in argv
    paths = _paths(include_untracked)
    blocking, known = scan(paths)

    for path, lineno, what, _ in blocking:
        # ::error makes the hit a GitHub annotation on the exact line
        print(f"::error file={path},line={lineno}::{what} — real credentials "
              f"never belong in tracked files; if this is a genuine "
              f"placeholder, append '{PRAGMA}'")
        print(f"{path}:{lineno}: {what}", file=sys.stderr)

    seen = set()
    for path, lineno, what, digest in known:
        seen.add(digest)
        note = KNOWN_EXPOSURES[digest]
        print(f"::warning file={path},line={lineno}::KNOWN EXPOSURE — {note}")
        print(f"{path}:{lineno}: known exposure — {note}", file=sys.stderr)

    # A ledger entry that matches nothing is either already cleaned up or was
    # never right; either way it must go, or the ledger becomes a place
    # credentials are filed and forgotten. Every mode scans the full tracked
    # set (see _paths), so this is always a fair comparison.
    stale = sorted(set(KNOWN_EXPOSURES) - seen)
    for digest in stale:
        print(f"::error::stale KNOWN_EXPOSURES entry {digest[:12]}… "
              f"({KNOWN_EXPOSURES[digest]}) matches nothing — remove it",
              file=sys.stderr)

    if known:
        print(f"\n{len(known)} line(s) carry a KNOWN exposure "
              f"({len(seen)} distinct credentials). These are ledgered, not "
              f"forgiven — rotate at the vendor, then delete the "
              f"KNOWN_EXPOSURES entry.", file=sys.stderr)
    if blocking or stale:
        if blocking:
            print(f"\n{len(blocking)} credential-shaped string(s) found. "
                  f"Rotate anything real FIRST (old SHAs stay public "
                  f"forever), then remove the value.", file=sys.stderr)
        return 1
    print(f"no NEW credential-shaped strings in {len(paths)} file(s) "
          f"({len(seen)} known exposures pending rotation)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
