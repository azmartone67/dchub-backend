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

WHAT IT DELIBERATELY IGNORES:
  - placeholder passwords (user:pass@, YOUR_PASS@, :...@, ${VAR}@, {var}@)
  - localhost / 127.0.0.1 targets (CI scratch containers)
  - placeholder param values (YOUR_*, SENTINEL*, EXAMPLE*, FAKE*, …)
  - any line carrying the pragma  secretscan:allow
  - this file itself (its self-test embeds credential-shaped fixtures)

Run `--self-test` first in CI: it proves every pattern still FIRES on a
known-bad fixture and stays SILENT on the known-good ones, so a regex edit
cannot quietly turn the scan vacuous (the fence-goes-green-by-dying class).
"""
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


def _findings_in_line(line):
    """Return a list of human-readable findings for one line of text."""
    out = []
    if PRAGMA in line:
        return out
    for m in USERINFO.finditer(line):
        user, pw, host = m.group(1), m.group(2), m.group(3)
        if PLACEHOLDER_PW.match(pw):
            continue
        if pw == user:  # postgres:postgres@ — CI scratch idiom
            continue
        if host.split(":")[0].lower() in LOCAL_HOSTS:
            continue
        out.append(f"connection URL embeds a password ({m.group(0)[:40]}…)")
    for m in RENDER_HOOK.finditer(line):
        out.append("Render deploy hook with its key")
    for m in SECRET_PARAM.finditer(line):
        if PLACEHOLDER_VAL.match(m.group(1)):
            continue
        out.append("URL carries a long secret query param "
                   f"({m.group(1)[:8]}…)")
    return out


def scan(paths):
    findings = []
    for path in paths:
        if path == SELF:
            continue
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        if b"\0" in blob[:8192]:  # binary
            continue
        text = blob.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for what in _findings_in_line(line):
                findings.append((path, lineno, what))
    return findings


def self_test():
    """Every pattern must FIRE on bad fixtures and stay SILENT on good ones."""
    bad = [
        # each of these is the real leak SHAPE with a fabricated value
        "REDIS_URL=redis://default:uMBeYfakefakefakefakefake@caboose.proxy.rlwy.net:29436",
        "url = 'postgres://neondb_owner:npg_FAKEFAKE1234@ep-x.aws.neon.tech/db'",
        "hook: https://api.render.com/deploy/srv-abc123def456?key=FaKeKey99",
        "curl https://api.eia.gov/v2/x?api_key=Qz8LmNoP1234567890AbCdEf&f=a",
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
    ]
    dead = [s for s in bad if not _findings_in_line(s)]
    noisy = [(s, _findings_in_line(s)) for s in good if _findings_in_line(s)]
    if dead:
        print("SELF-TEST FAILED — pattern no longer fires on:", file=sys.stderr)
        for s in dead:
            print(f"  {s}", file=sys.stderr)
        return 1
    if noisy:
        print("SELF-TEST FAILED — false positive on known-good:", file=sys.stderr)
        for s, hits in noisy:
            print(f"  {s} -> {hits}", file=sys.stderr)
        return 1
    print(f"self-test ok: {len(bad)} bad fixtures fire, "
          f"{len(good)} good fixtures stay silent")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    files = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True,
    ).stdout.decode().split("\0")
    findings = scan([f for f in files if f])
    for path, lineno, what in findings:
        # ::error makes the hit a GitHub annotation on the exact line
        print(f"::error file={path},line={lineno}::{what} — real credentials "
              f"never belong in tracked files; if this is a genuine "
              f"placeholder, append '{PRAGMA}'")
        print(f"{path}:{lineno}: {what}", file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} credential-shaped string(s) found. "
              f"Rotate anything real FIRST (old SHAs stay public forever), "
              f"then remove the value.", file=sys.stderr)
        return 1
    print("no credential-shaped strings in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
