#!/usr/bin/env python3
"""Patch 4b: Targeted tier ladder injection into land-power.html upgrade modals."""
import re

FILE = 'static/land-power.html'
src = open(FILE).read()
new = src

# ── 1. "Free Plan Limit Reached" modal (line ~1284) ──────────────────────────
# Replace single amber "Upgrade to Pro" button with tier ladder
OLD1 = '<a href="/pricing" style="display:inline-block;padding:10px 24px;background:#f59e0b;color:#000;font-weight:700;border-radius:8px;text-decoration:none;margin-right:8px">Upgrade to Pro</a><button onclick="document.getElementById(\'upgrade-prompt\').remove()" style="padding:10px 16px;background:transparent;border:1px solid #374151;color:#9ca3af;border-radius:8px;cursor:pointer">Dismiss</button>'

NEW1 = '''<div style="display:flex;gap:8px;margin-top:4px;flex-wrap:wrap;">
<div style="flex:1;min-width:140px;border:1px solid #374151;border-radius:8px;padding:12px;background:#111;text-align:center;">
<div style="font-size:11px;color:#9ca3af;text-transform:uppercase;margin-bottom:3px;">Developer</div>
<div style="font-size:22px;font-weight:700;color:#fff;">$49<span style="font-size:12px;color:#6b7280;">/mo</span></div>
<div style="font-size:11px;color:#6b7280;margin:4px 0 8px;">1,000 calls/day · All layers</div>
<a href="/pricing#developer" style="display:block;padding:7px;border-radius:6px;background:#1d4ed8;color:#fff;font-size:12px;font-weight:600;text-decoration:none;">Start Developer →</a>
</div>
<div style="flex:1;min-width:140px;border:2px solid #f59e0b;border-radius:8px;padding:12px;background:#0f1119;text-align:center;">
<div style="font-size:11px;color:#fbbf24;text-transform:uppercase;margin-bottom:3px;">⭐ Pro</div>
<div style="font-size:22px;font-weight:700;color:#fff;">$99<span style="font-size:12px;color:#6b7280;">/mo</span></div>
<div style="font-size:11px;color:#6b7280;margin:4px 0 8px;">10,000 calls/day · Export</div>
<a href="/pricing#pro" style="display:block;padding:7px;border-radius:6px;background:#f59e0b;color:#000;font-size:12px;font-weight:700;text-decoration:none;">Upgrade to Pro →</a>
</div>
</div>
<button onclick="document.getElementById(\'upgrade-prompt\').remove()" style="margin-top:10px;padding:7px 14px;background:transparent;border:1px solid #374151;color:#6b7280;border-radius:8px;cursor:pointer;font-size:12px;">Dismiss</button>'''

if OLD1 in new:
    new = new.replace(OLD1, NEW1, 1)
    print("✅ Patched: Free Plan Limit Reached modal")
else:
    print("⚠️  Skip modal 1 — pattern not found")

# ── 2. "Your Free Preview Has Ended" overlay (line ~1531) ────────────────────
OLD2 = '<a href="/pricing" style="display:inline-block;padding:12px 32px;background:#f59e0b;color:#000;font-weight:800;border-radius:10px;text-decoration:none;font-size:15px;margin-bottom:12px">Upgrade to Pro</a>'

NEW2 = '''<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
<div style="flex:1;min-width:140px;border:1px solid #374151;border-radius:10px;padding:14px;background:#111;text-align:center;">
<div style="font-size:11px;color:#9ca3af;text-transform:uppercase;margin-bottom:4px;">Developer</div>
<div style="font-size:24px;font-weight:700;color:#fff;">$49<span style="font-size:13px;color:#6b7280;">/mo</span></div>
<div style="font-size:11px;color:#6b7280;margin:5px 0 10px;">1,000 calls/day<br>All infrastructure layers</div>
<a href="/pricing#developer" style="display:block;padding:8px;border-radius:8px;background:#1d4ed8;color:#fff;font-size:13px;font-weight:600;text-decoration:none;">Start Developer →</a>
</div>
<div style="flex:1;min-width:140px;border:2px solid #f59e0b;border-radius:10px;padding:14px;background:#1a1200;text-align:center;position:relative;">
<div style="position:absolute;top:-9px;left:50%;transform:translateX(-50%);background:#f59e0b;color:#000;font-size:9px;font-weight:800;padding:2px 8px;border-radius:8px;">POPULAR</div>
<div style="font-size:11px;color:#fbbf24;text-transform:uppercase;margin-bottom:4px;">Pro</div>
<div style="font-size:24px;font-weight:700;color:#fff;">$99<span style="font-size:13px;color:#6b7280;">/mo</span></div>
<div style="font-size:11px;color:#6b7280;margin:5px 0 10px;">10,000 calls/day<br>Site scoring + PDF export</div>
<a href="/pricing#pro" style="display:block;padding:8px;border-radius:8px;background:#f59e0b;color:#000;font-size:13px;font-weight:800;text-decoration:none;">🚀 Upgrade to Pro →</a>
</div>
</div>'''

if OLD2 in new:
    new = new.replace(OLD2, NEW2, 1)
    print("✅ Patched: Free Preview Ended overlay")
else:
    print("⚠️  Skip overlay — pattern not found")

# ── 3. 30-second banner (line ~1496) — add Developer option ──────────────────
OLD3 = 'Your free preview ends in 30 seconds — <a href="/pricing" style="color:#fde68a;text-decoration:underline">Upgrade to Pro</a> to keep full access'
NEW3 = 'Your free preview ends in 30 seconds — <a href="/pricing#developer" style="color:#93c5fd;text-decoration:underline">Developer $49</a> or <a href="/pricing#pro" style="color:#fde68a;text-decoration:underline">Pro $99</a> to keep full access'

if OLD3 in new:
    new = new.replace(OLD3, NEW3, 1)
    print("✅ Patched: 30-second countdown banner")
else:
    print("⚠️  Skip banner — pattern not found")

# ── Save ──────────────────────────────────────────────────────────────────────
if new != src:
    open(FILE, 'w').write(new)
    print(f"\nSaved {FILE}")
    print("git add static/land-power.html && git commit -m 'growth: tier ladder in all upgrade modals' && git push")
else:
    print("\nNo changes made.")
