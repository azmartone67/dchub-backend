"""
send_winback_outreach.py — fires the 5 win-back drafts.

Usage:
    python3 send_winback_outreach.py            # dry-run (default)
    python3 send_winback_outreach.py --send     # actually send
"""
import argparse, importlib, re, sys
from pathlib import Path

OUTREACH_DIR = Path("dchub-mcp-v2.1/outreach")

def parse_draft(md_path):
    text = md_path.read_text()
    to    = re.search(r'\*\*To:\*\*\s+(\S+)', text)
    subj  = re.search(r'\*\*Subject:\*\*\s+(.+)', text)
    body  = re.sub(r'^# [^\n]+\n+', '', text, count=1)
    body  = re.sub(r'\*\*To:\*\*[^\n]+\n', '', body, count=1)
    body  = re.sub(r'\*\*Subject:\*\*[^\n]+\n+', '', body, count=1)
    return {
        "to":      to.group(1).strip() if to else None,
        "subject": subj.group(1).strip() if subj else None,
        "body":    body.strip(),
        "file":    md_path.name,
    }

def find_send_fn():
    """Try common signatures from email_service.py."""
    try:
        es = importlib.import_module("email_service")
    except Exception as e:
        return None, f"email_service.py not importable: {e}"
    for name in ("send_email", "send", "send_mail", "send_message"):
        fn = getattr(es, name, None)
        if callable(fn):
            return fn, name
    return None, f"no send function found in email_service (checked: send_email/send/send_mail/send_message)"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send (default is dry-run)")
    args = ap.parse_args()

    drafts = sorted(OUTREACH_DIR.glob("*.md"))
    print(f"Found {len(drafts)} drafts in {OUTREACH_DIR}\n")

    fn, fn_name = (None, None)
    if args.send:
        fn, fn_name = find_send_fn()
        if not fn:
            print(f"FAIL: {fn_name}")
            sys.exit(2)
        print(f"Using email_service.{fn_name}() to send.\n")

    sent = 0
    for d in drafts:
        info = parse_draft(d)
        print(f"── {info['file']} ──")
        print(f"  To:      {info['to']}")
        print(f"  Subject: {info['subject']}")
        print(f"  Body:    {len(info['body'])} chars")
        if not args.send:
            continue
        try:
            fn(to=info['to'], subject=info['subject'], body=info['body'])
            print(f"  ✓ sent")
            sent += 1
        except Exception as e:
            print(f"  ✗ failed: {type(e).__name__}: {e}")
            print(f"    (signature mismatch? Inspect email_service.{fn_name} args)")
        print()

    if args.send:
        print(f"\nSent {sent}/{len(drafts)}.")
    else:
        print("\nDry-run only. Re-run with --send to actually send.")

if __name__ == "__main__":
    main()
