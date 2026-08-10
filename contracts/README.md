# API Response Contract

**The backend response shape is a contract. Breaking it fails the PR.**

## The one command

```bash
python3 scripts/api_response_contract.py baseline
```

That regenerates `contracts/api_response_surface.json`. Run it whenever a PR
legitimately adds keys. Nothing in this directory is hand-maintained.

```bash
python3 scripts/api_response_contract.py check     # what CI runs
python3 scripts/api_response_contract.py extract   # print surface, write nothing
```

## Why

2026-08-08: a change to `/api/v1/ai/reach` altered its response shape. A
frontend reader of the old shape rendered **"0 AI PLATFORMS CONNECTED"** on the
live `/ai` page — a confident zero manufactured from an `undefined` lookup.
Nothing warned. It was found in production, by the owner.

The frontend guard (`scripts/qa-api-contract.mjs` in `dchub-frontend`) catches
this class by scanning the frontend — but only *after* the backend shipped.
This is the proactive half: it runs on the backend PR that causes it.

## What it does

| Change | Verdict |
|---|---|
| Key added | **PASS** — additive change is never blocked |
| Key removed | **FAIL** — endpoint, key, and who reads it |
| Key renamed with no alias | **FAIL** — reported as a rename, with the suspected new name |
| Endpoint deleted | **FAIL** |
| Surface not computable | **UNMEASURED** — and UNMEASURED fails CI |

Exit codes: `0` PASS, `1` FAIL, `2` UNMEASURED.

**UNMEASURED is never rounded up to a pass.** An empty surface, a surface that
collapsed below 70% of baseline, a file that stopped parsing, an extractor
crash, or a response dict that became dynamic — all report UNMEASURED and fail
the job. "Could not measure" is not "fine".

## How the surface is derived

By AST analysis of the Flask handlers in this repo — never a hand-written list,
because a hand-written list is a second source of truth and rots exactly the
way these contracts did.

A response is **resolved** when the returned dict can be reconstructed
statically: dict literals, plus locals built up with `out = {...}`,
`out["k"] = v`, `out.update({...})`, `out.setdefault(...)`, and the
`resp = jsonify(payload); return resp, 200` idiom.

Two honest limits, both recorded in the baseline rather than papered over:

- **`open` levels.** A `**splat` or a dynamic key (`stats[var] = ...`) means
  keys may exist that static analysis cannot see. The keys we *can* see stay
  under contract; a key disappearing from an open level reports UNMEASURED
  rather than FAIL, because it might still be served invisibly.
- **`opaque` endpoints.** A handler returning `jsonify(build_it())` cannot be
  reconstructed. These are listed by name in the baseline and are **NOT
  COVERED**. The guard does not imply it protects them.

Only non-error returns count — `return jsonify({...}), 401` is skipped — so
error-branch churn cannot manufacture phantom contract breaks.

## Coverage, stated honestly

Read the live numbers from `api_response_surface.json` → `stats`; the figures
below are the state at introduction (2026-08-09) and will drift.

| | |
|---|---|
| In-scope endpoints (`/api/*`, minus admin/internal/debug/ops) | 1,846 |
| **Protected** (resolved + partial) | **1,409** |
| **Not covered** (opaque) | **437** |
| Keys under contract | 10,978 |
| — of those, strictly protected (removal ⇒ FAIL) | 9,992 |
| — under an `open` level (removal ⇒ UNMEASURED) | 878 |

**Not covered at all:** admin, internal, debug and `/api/ops/*` routes; any
path outside `/api/`; the MCP tool surface in `server.mjs` (JavaScript — this
extractor is Python-only); and the 437 opaque endpoints named in the baseline.

## When it fails

1. **Preferred:** keep serving the old key alongside the new one. Additive
   change is never blocked, so a compatibility alias costs nothing.
2. **Last resort:** if the removal is deliberate, add an entry to
   `api_response_exceptions.json` with a reason, then regenerate the baseline.
   That file is an audit log of exceptions — not a description of the surface.

## The guard can fail

`scripts/api_response_contract_selftest.py` runs first in CI, on every run. It
plants ten defects (key removed, key renamed, endpoint deleted, empty surface,
collapsed surface, laundered-to-opaque, missing baseline, extractor crash…) and
asserts the real `check()` goes red for each — and stays green for an added
key. Sabotaging `check()` to always return PASS turns the self-test red.

A guard that cannot fail is worse than no guard.
