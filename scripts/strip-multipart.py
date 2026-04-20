#!/usr/bin/env python3
"""Strip multipart-form envelope from a captured Worker upload body."""
import re, sys
src = sys.stdin.read()
m = re.search(r"(/\*\*[\s\S]*?^\};)\s*(?:--[A-Fa-f0-9]+--)?\s*\Z", src, re.M)
sys.stdout.write(m.group(1) + "\n" if m else src)
