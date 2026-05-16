"""Phase TT-2 (2026-05-15) — Canonical CSP loader.

Tries to read the Content-Security-Policy directly from the
dchub-frontend repo's /_headers file. In dev (both repos checked
out side-by-side) this returns the live source-of-truth CSP. In
production (Railway-only deploy) it raises FileNotFoundError; the
caller is responsible for falling back to a hardcoded mirror.

Why this design: there's no shared package between the two repos.
A direct file read is the simplest mechanism that doesn't introduce
a deploy-time copy step or a runtime HTTP fetch.

Future improvement (deferred): vendor the CSP into a small JSON file
that both repos can read, OR build a pre-deploy CI step that copies
_headers into the backend repo. For now, the hardcoded fallback +
sync-rule comment in routes/dcpi.py is the safety net.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _find_frontend_headers() -> Path | None:
    """Walk up from this file looking for a sibling dchub-frontend repo.

    Layouts that work:
      ~/dchub-backend/util/csp_canonical.py
      ~/dchub-frontend/_headers

    Layouts that don't (production):
      /app/util/csp_canonical.py  (Railway container — no sibling repo)
    """
    here = Path(__file__).resolve().parent.parent  # dchub-backend/
    parent = here.parent
    candidate = parent / "dchub-frontend" / "_headers"
    if candidate.exists():
        return candidate
    return None


def get_canonical_csp() -> str:
    """Return the Content-Security-Policy from dchub-frontend/_headers.

    Raises FileNotFoundError if the sibling repo isn't checked out.
    Raises ValueError if the file exists but the CSP line is missing
    or malformed.
    """
    headers_file = _find_frontend_headers()
    if headers_file is None:
        raise FileNotFoundError(
            "dchub-frontend/_headers not found — caller should use the "
            "hardcoded fallback. This is expected in production where "
            "only one repo is deployed.")
    text = headers_file.read_text(encoding="utf-8", errors="replace")
    # Match: "  Content-Security-Policy: ..." (Pages headers are indented).
    m = re.search(r"^\s*Content-Security-Policy:\s*(.+)$",
                  text, re.MULTILINE)
    if not m:
        raise ValueError(
            f"No Content-Security-Policy directive found in {headers_file}")
    return m.group(1).strip()


def verify_csp_matches(hardcoded: str) -> tuple[bool, str]:
    """For CI / consistency-radar use: compare the hardcoded fallback
    in routes/dcpi.py to the canonical source. Returns (ok, message).
    Used by the brain consistency radar to flag drift."""
    try:
        canonical = get_canonical_csp()
    except (FileNotFoundError, ValueError) as e:
        return False, f"could not load canonical CSP: {e}"
    # Normalize whitespace before compare — _headers and Python source
    # have different formatting conventions.
    norm = lambda s: re.sub(r"\s+", " ", s.strip())
    if norm(canonical) == norm(hardcoded):
        return True, "CSP matches canonical source"
    # Show a short diff hint
    return False, (f"CSP drift detected. Canonical length="
                    f"{len(canonical)}, hardcoded length={len(hardcoded)}")
