#!/usr/bin/env python3
"""A tracked doc must not record the OPERATOR'S machine as if it were config.

NO NETWORK, NO DB, NO APP BOOT.

What shipped
------------
CONFIG_SNAPSHOT.md said it was "captured from the live Railway dchub-backend
service", but it was produced with `railway run printenv` — which runs printenv
on the OPERATOR'S machine with Railway's vars merged into the shell. So 31 of
its entries were never service config: the macOS shell (HOME, PATH, PWD, USER,
LOGNAME, SHELL, TMPDIR, SSH_AUTH_SOCK, XPC_*) and the Claude Code desktop
client that ran the command (CLAUDE_CODE_*, CLAUDECODE, BAGGAGE, AI_AGENT).

In a PUBLIC repo that published a username, a home-directory layout, an
absolute path to the operator's Claude install, a client session id and a
Sentry trace — and it made the file WRONG about production besides.

This is the same class as the 2026-08-07 credential leak in the same file
(REDIS_URL / RENDER_DEPLOY_HOOK_URL), and it has the same fix: an env-dump doc
must filter by a KEY-NAME allowlist, never paste os.environ wholesale.
scripts/check_no_leaked_credentials.py catches credential SHAPES; it does not
catch `HOME = /Users/<name>`, because nothing about that looks like a secret.
This closes that half.

Why the pattern is anchored
---------------------------
★ The note in CONFIG_SNAPSHOT.md that EXPLAINS this fix necessarily names the
banned variables. A detector that flagged the bare word HOME would fail on the
very paragraph documenting the repair — the "comment explaining the drift
quotes the drift" trap. So the pattern matches the env-dump LINE SHAPE
(`- `VAR` = `value`` at the start of a line), which prose
mentions cannot satisfy. test_the_explainer_prose_does_not_trip_it pins that.
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Variables that describe a developer's workstation or their editor session.
# None of these can be meaningful config for a containerised service.
_MACHINE_VARS = (
    "COMMAND_MODE", "HOME", "LOGNAME", "OLDPWD", "PATH", "PWD", "SHELL",
    "SHLVL", "SSH_AUTH_SOCK", "TMPDIR", "USER", "XPC_FLAGS", "XPC_SERVICE_NAME",
    "AI_AGENT", "BAGGAGE", "CLAUDECODE", "CLAUDE_EFFORT", "GIT_EDITOR",
    "DISABLE_AUTOUPDATER", "DISABLE_MICROCOMPACT", "MCP_CONNECTION_NONBLOCKING",
)

# An env-dump list entry: "- `VAR` = ..." at the start of a line. Prose that
# merely mentions `HOME` in backticks does not match, which is the point.
_DUMP_LINE = re.compile(
    r"^- `(" + "|".join(_MACHINE_VARS) + r"|CLAUDE_CODE_[A-Z0-9_]+)` = ", re.M)

# A home directory recorded as a VALUE in that same line shape.
_HOME_PATH_VALUE = re.compile(r"^- `[A-Z0-9_]+` = .*?/Users/[^/`\s]+/", re.M)

_TEXT_SUFFIXES = (".md", ".txt", ".rst")


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0")
            if p and p.endswith(_TEXT_SUFFIXES)]


def _offences():
    hits, scanned = [], 0
    for rel in _tracked_text_files():
        try:
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for m in _DUMP_LINE.finditer(src):
            hits.append((rel, m.group(1)))
        for m in _HOME_PATH_VALUE.finditer(src):
            hits.append((rel, m.group(0).split("`")[1] + " (home path value)"))
    return hits, scanned


def test_no_tracked_doc_records_a_machine_identity_env_var():
    hits, _ = _offences()
    assert not hits, (
        "a tracked doc records the operator's machine as if it were service "
        "config — in a PUBLIC repo:\n"
        + "".join(f"  {rel}: {var}\n" for rel, var in sorted(set(hits)))
        + "\nRegenerate with `railway variables list -s <service>`, which "
          "returns the SERVICE's variables. `railway run printenv` merges your "
          "own shell into the output and is how this got in.")


def test_the_detector_fires_on_the_lines_that_actually_shipped():
    """Must-fail control, against the real text from the 2026-06-13 capture."""
    shipped = "\n".join((
        "- `HOME` = `/Users/jonathanmartone`",
        "- `CLAUDE_CODE_SESSION_ID` = `8c6f1bb4-e568-4128-80d9-94f981bdafc2`",
        "- `USER` = `jonathanmartone`",
    ))
    assert len(_DUMP_LINE.findall(shipped)) == 3, "detector missed a real line"
    assert _HOME_PATH_VALUE.search("- `PWD` = `/Users/jonathanmartone/dchub-backend`")


def test_the_explainer_prose_does_not_trip_it():
    """The paragraph documenting the fix names the banned vars. It must pass.

    Without this, the obvious 'flag the word HOME' detector looks correct and
    then fails on the note explaining why it exists.
    """
    prose = ("the operator's macOS shell (`HOME`, `PATH`, `PWD`, `USER`, "
             "`LOGNAME`, `SHELL`, `TMPDIR`, `SSH_AUTH_SOCK`, `XPC_*`) and the "
             "Claude Code desktop client (`CLAUDE_CODE_*`, `CLAUDECODE`)")
    assert not _DUMP_LINE.findall(prose)
    assert not _HOME_PATH_VALUE.findall(prose)


def test_the_scan_is_not_vacuous():
    """A scan that reads nothing cannot fail, so prove it read the real file."""
    _, scanned = _offences()
    assert scanned > 50, f"only scanned {scanned} tracked docs — scan collapsed"
    assert "CONFIG_SNAPSHOT.md" in _tracked_text_files(), (
        "CONFIG_SNAPSHOT.md is not in the scanned set — the guard is pointed "
        "somewhere else and would not have caught the leak it exists for")
