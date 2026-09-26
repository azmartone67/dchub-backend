"""Read service settings out of .railway/railway.ts (Railway IaC) without a JS
runtime, for the static guards in test_web_dockerfile.py and
test_extractor_dockerfile.py.

Comments are stripped first, so a comment that quotes a setting can never
satisfy an assertion about the setting. String values are decoded as JSON
string literals (the file writes them double-quoted), so escaped quotes inside
a start command survive intact.
"""
import json
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
IAC = os.path.join(ROOT, ".railway", "railway.ts")

_STR = r'"(?:[^"\\\n]|\\.)*"'


def strip_ts_comments(text):
    """Drop // and /* */ comments, leaving string literals intact."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def service_block(name):
    """The object literal passed to service("<name>", {...}), comments removed."""
    with open(IAC, encoding="utf-8") as f:
        code = strip_ts_comments(f.read())
    m = re.search(r'\bservice\(\s*"%s"\s*,\s*\{' % re.escape(name), code)
    assert m, f".railway/railway.ts no longer declares service({name!r}, {{...}})"
    depth, i = 1, m.end()
    while depth:
        assert i < len(code), f"unbalanced braces in service({name!r})"
        c = code[i]
        if c in "\"'`":
            j = i + 1
            while code[j] != c:
                j += 2 if code[j] == "\\" else 1
            i = j + 1
            continue
        depth += {"{": 1, "}": -1}.get(c, 0)
        i += 1
    return code[m.end():i - 1]


def string_field(name, key):
    """Decoded value of `key: "..."` in the service's block, or "" if absent."""
    m = re.search(r'\b%s\s*:\s*(%s)' % (re.escape(key), _STR), service_block(name))
    return json.loads(m.group(1)) if m else ""
