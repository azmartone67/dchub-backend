#!/usr/bin/env python3
"""Patch 1: Strip DB record counts from console logs visible to anonymous users."""
import glob, os

targets = [
    "Substations: 79,755+",
    "Gas Infrastructure: 37,705+",
    "HIFLD Transmission shown (300,000+",
    "79,755+",
    "37,705+",
    "300,000+",
]

# Lines to fully silence (replace console.log with nothing)
silent_phrases = [
    "Substations: 79,755+ in DB",
    "Gas Infrastructure: 37,705+",
    "HIFLD Transmission shown",
    "HIFLD Substations shown",
    "300,000+ miles",
]

js_files = glob.glob("static/**/*.js", recursive=True) + glob.glob("*.js")
changed = 0

for path in js_files:
    try:
        src = open(path).read()
        new = src
        for phrase in silent_phrases:
            # Find console.log lines containing the phrase and blank them
            import re
            new = re.sub(
                r"console\.log\([^;]*" + re.escape(phrase) + r"[^;]*\);?",
                "/* db-count redacted */",
                new
            )
        if new != src:
            open(path, "w").write(new)
            print(f"✅ Patched: {path}")
            changed += 1
    except Exception as e:
        print(f"  skip {path}: {e}")

print(f"\nDone — {changed} file(s) patched. Run: git add -A && git commit -m 'security: remove db counts from anonymous console logs' && git push")
