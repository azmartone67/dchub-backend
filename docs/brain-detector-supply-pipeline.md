# Brain detector-supply pipeline (design)

Status: **design only — no runtime code exists yet.** This document specifies
what to build; nothing in it ships until each phase's exit criteria are met.

Source: an external write-up of a GitHub→Agent-Skill discovery pipeline (Scout ·
Filter · Reader · Extractor · Score · Generator · Reviewer · Publisher), retargeted
here at the constraint this repo has already measured.

---

## 1. Why this, and why now

The white-glove BRAIN lane (`routes/white_glove_loop_master_shell.py:22-27`, lane
body at `:246`) has been `critical=True` red on purpose since 2026-07-30, and it
names the constraint precisely:

> The six mechanical transform classes are EXHAUSTED — every instance found and
> fixed, last autofix PR 2026-07-19. Measured 2026-07-30 against the REAL
> classifier: only EIGHT proposals are blocked SOLELY by the class gate, so
> widening the allowlist is NOT the lever. **Autonomy is capped by DETECTOR
> SUPPLY**: new narrow, known-shape, high-frequency detectors of the kind that
> gave `now_text_cast` 12 merged PRs. Adding a detector is additive and safe;
> loosening the merge gate is neither.

Detector supply is currently a human job: someone notices a recurring bug shape,
hand-writes a regex pair and a transform, and adds a class. That has happened six
times, ever. This pipeline mechanises the noticing.

### The distinction this design rests on — read before objecting

The lane says widening the allowlist is not the lever, and this pipeline widens
the allowlist. That is not a contradiction, but the difference is load-bearing:

| | what it changes | supply effect |
|---|---|---|
| **Widening the class gate alone** | lets *already-queued* proposals through | +8 proposals. Measured. That is the whole ceiling. |
| **Adding a detector** | a new *sweep spec* generates proposals that do not exist yet, and a matching class lets them classify | unbounded by the existing queue — `now_text_cast` produced 12 merged PRs from zero prior proposals |

A class without a finder unblocks nothing new. A finder without a class produces
proposals that never classify mechanical. **A detector is both halves, or it is
not a detector.** The pipeline must emit both or it has done nothing.

---

## 2. What a detector actually is here

This is the part the source design does not have an answer for. Its output is a
generic `SKILL.md` package, and this repo has no skill runtime — the only
`SKILL.md` is one static file served at `main.py:22160` for MCP discovery. A
`SKILL.md` would land nowhere.

A detector in this codebase is **five coupled artifacts**, three of which are
mandatory:

| # | Artifact | File | Shape |
|---|---|---|---|
| 1 | **Sweep spec** (the finder) | `routes/brain_autonomy_loop.py` | `{klass, line_re, transform(line) -> line}` — see `_t_now_text_cast` at `:162` |
| 2 | **Allowlist class** (the gate) | `routes/brain_mechanical_classifier.py::ALLOWLIST_CLASSES` (`:265`) | `{klass, search: re, replace: re\|None, allowed_new_tokens: set}` |
| 3 | **Reconcile pattern** (the closer) | `routes/brain_v2_layer5.py::_KNOWN_BUG_PATTERNS` (`:151`) | literal string, so a fixed bug marks its proposal resolved instead of lingering |
| 4 | Seed-module exclusion | `routes/brain_autonomy_loop.py::_SKIP_REL_SUBSTR` (`:103`) | only if the new literals would make a module self-matching |
| 5 | Test | `tests/test_brain_detector_<klass>.py` | must run at function scope only — see §8 |

`klass` must be identical across 1 and 2, or the sweep spec is silently dropped
(`brain_autonomy_loop.py:150` — "A spec is dropped silently if its klass is not in
ALLOWLIST_CLASSES"). That silent drop is the most likely way a generated detector
ships as a no-op and reports success. §9 covers detecting it.

**So the pipeline's deliverable is a draft PR containing artifacts 1-3 (+4, +5),
not a skill package.**

---

## 3. Reuse map

Six of the eight stages already exist, pointed inward. This is not an eight-agent
build; it is stages 1, 3, 4 and 6 bolted onto an existing spine.

| Stage | Status | Module |
|---|---|---|
| 1 Scout | **new** — plumbing exists | `proactive_discovery.py:166` and `routes/mcp_registry_watch.py:313` already call `api.github.com/search/repositories`. Target is wrong (fiber KMZ / registry presence), transport is right. |
| 2 Filter | **new, pattern exists** | mirror `routes/brain_mechanical_classifier.py` — deterministic rules, `reasons[]` / `blocked_by[]` accounting |
| 3 Reader | **new** | README → docs/ → examples/ → manifests → source, stopping early |
| 4 Extractor | **new** | closest prior art `routes/brain_architecture_proposer.py`; LLM budget via `routes/brain_llm_spend.py`, structured output via `routes/brain_llm_structured.py` |
| 5 Score | **new, and mostly not an LLM** | §6 — the dry-run measurement is the centrepiece |
| 6 Generator | **new** | emits the five artifacts of §2 |
| 7 Reviewer | **exists** | `.github/workflows/brain-pr-substance-gate.yml`, plus L22 whitelist / diff-cap / dedup |
| 8 Publisher | **exists** | `routes/brain_draft_pr_writer.py` + `routes/brain_pr_opener.py`. Already draft-only, `base=main`, never auto-merged, `MAX_DRAFT_PRS_PER_RUN` capped, re-classifies at apply time against live `main`. |

Do not add a second GitHub client. `brain_draft_pr_writer.py` is explicit about
this: token handling, repo/fork/base config and git mechanics all come from
`routes/brain_layer22_pr_writer`.

---

## 4. Stage detail

### 4.1 Scout

Not "agent frameworks". The source design searches for `langgraph OR mcp OR
"multi-agent"`, which surfaces architecture, not bug shapes. We want repos whose
*content is a corpus of known-shape code transforms*:

```
Queries (rotated, one per tick):
  "codemod" language:Python stars:>50
  libcst OR "libcst.CSTTransformer" stars:>50
  path:**/rules/*.yml semgrep stars:>200
  ruff rule OR "flake8 plugin" language:Python stars:>100
  pyupgrade OR "auto-fix" OR autofix language:Python stars:>100
  "psycopg" AND (migration OR antipattern) stars:>30

Filters: pushed:>{now-180d}  archived:false  is:public
Licence: permissive only (see §7) — MIT/Apache-2.0/BSD/ISC/Unlicense
```

Rate limit: authenticated GitHub search is 30 req/min. One tick = one query, ≤30
results. Persist `etag` per query and skip unchanged pages.

### 4.2 Filter (deterministic, no LLM)

Rejects before any token is spent. Same accounting shape as the classifier —
every reject records *which* rule fired.

- licence not in the permissive set → `REJECT:licence`
- no `README` → `REJECT:no_readme`
- repo already in `detector_scout_repos` with `head_sha` unchanged → `SKIP:seen`
- primary language not Python/YAML/TOML → `REJECT:language`
- fewer than 3 files matching `rule|transform|codemod|fixer|check` → `REJECT:no_rule_corpus`
- archived, or `pushed_at` older than 180d → `REJECT:stale`

### 4.3 Reader

README → `docs/` → `examples/` → `pyproject.toml`/`setup.cfg` → source, and stop
as soon as a rule corpus is located. Cap: 40 KB of text into the model, hard.
Record `context_loaded[]` and `source_code_loaded` so we can audit later whether
reading source ever changed an outcome — if it never does, delete that step.

### 4.4 Extractor

Output is **not** a workflow description. It is a candidate detector:

```json
{
  "klass": "psycopg_string_interp_sql",
  "provenance": {"repo": "owner/name", "sha": "…", "path": "rules/sql.yml", "licence": "MIT"},
  "buggy_form":  "cur.execute(\"… %s …\" % var)",
  "fixed_form":  "cur.execute(\"… %s …\", (var,))",
  "line_re":     "cur\\.execute\\([^,]*%[^,]*\\)\\s*%\\s*",
  "search_re":   "…",
  "replace_re":  "…",
  "allowed_new_tokens": [],
  "rationale":   "…",
  "confidence":  0.91
}
```

A candidate that cannot express both a `line_re` and a deterministic `transform`
is rejected here. "Use a linter" is not a detector.

### 4.5 Score — **the dry-run is the gate**

The source design asks an LLM whether a workflow "looks good". We can do far
better, because a candidate detector is *executable against our own tree right
now*. This converts the central judgment from opinion into measurement.

Run the candidate's `line_re` + `transform` across the repo using the **existing**
`_iter_py_files()` walker (`brain_autonomy_loop.py:110`), which already prunes
forbidden paths via `_forbidden_path_hits`, skips `tests/`, and skips the
seed-pattern modules. Then gate on what came back:

| # | Gate | Threshold | Why |
|---|---|---|---|
| G1 | live hit count | `3 ≤ hits ≤ 200` | <3 is not "high-frequency"; >200 means the regex is too broad and this would be a mass edit, not a detector |
| G2 | files touched | `≤ 40` | same |
| G3 | transform is idempotent | `transform(transform(x)) == transform(x)` on every hit | non-idempotent transforms oscillate across ticks |
| G4 | transform is a fixed point on clean code | `transform(x) == x` for 200 sampled non-matching lines | catches regexes that rewrite innocent code |
| G5 | every produced pair classifies mechanical | `classify_mechanical()` returns `is_mechanical` on **100%** of sampled hits | if the class it ships can't pass its own gate, the detector is a no-op |
| G6 | syntax survives | every transformed file still `ast.parse()`s | |
| G7 | no forbidden path in hits | `_forbidden_path_hits(rel) == []` | redundant with the walker; asserted anyway |
| G8 | class name unused | `klass` not in `ALLOWLIST_CLASSES` | |
| G9 | `allowed_new_tokens` ⊆ audited set | new tokens must be SQL keywords or stdlib names, never a call into anything else | this is the injection surface |
| G10 | confidence | `≥ 0.85` | |

**Any gate fails → rejected, with the failing gate recorded.** G5 in particular
is free and catches the silent-drop failure of §2.

G1's upper bound is the one that matters most. An LLM-authored regex that is
slightly too greedy is the realistic bad outcome here, and a 5,000-hit sweep is
how it would present.

### 4.6 Generator

Emits the five artifacts of §2 as a search→replace change set the existing draft
PR writer can already apply. Nothing bespoke — `brain_draft_pr_writer.py` takes an
arbitrary single-hunk search/replace and was built for exactly this.

The PR body must carry: provenance (repo, sha, licence, file), the dry-run report
(hit count, files, sample diffs), every gate result, and the rollback note.

### 4.7 Reviewer / 4.8 Publisher

Unchanged. Draft PR, `base=main`, human merge. `substance-gate`, `syntax-check`
and `unit-tests` are already required on `main`.

---

## 5. Schema

`migrations/2026-08-05_detector_scout.sql`:

```sql
CREATE TABLE IF NOT EXISTS detector_scout_repos (
    id            BIGSERIAL PRIMARY KEY,
    full_name     TEXT NOT NULL,
    head_sha      TEXT,
    stars         INT,
    language      TEXT,
    licence       TEXT,
    pushed_at     TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued|filtered|read|extracted|scored|drafted|rejected
    reject_reason TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT detector_scout_repos_uniq UNIQUE (full_name)
);
CREATE INDEX IF NOT EXISTS detector_scout_repos_status_idx
  ON detector_scout_repos(status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS detector_candidates (
    id            BIGSERIAL PRIMARY KEY,
    repo_id       BIGINT REFERENCES detector_scout_repos(id),
    klass         TEXT NOT NULL,
    candidate_json JSONB NOT NULL,      -- the §4.4 object
    dryrun_json   JSONB,                -- hits, files, sample diffs
    gates_json    JSONB,                -- {G1: true, G5: false, ...}
    score         REAL,
    decision      TEXT,                 -- pass|reject
    blocked_by    TEXT[],
    pr_url        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT detector_candidates_uniq UNIQUE (klass)
);
```

`UNIQUE (klass)` is the dedup — the same bug shape found in four repos produces
one candidate, not four PRs. Mirrors
`brain_proposed_code_fixes_unique` (`brain_v2_layer5.py:802`).

---

## 6. Kill switches

Every one defaults to the safe value. The pipeline ships dark, as
`brain-innovation-digest.yml` does.

| Env | Default | Effect |
|---|---|---|
| `DETECTOR_SCOUT_ENABLED` | `0` | master switch; off = every endpoint no-ops with `{skipped:"disabled"}` |
| `DETECTOR_SCOUT_DRAFT_PR` | `0` | off = scores and stores, opens nothing |
| `DETECTOR_SCOUT_MAX_PR_PER_WEEK` | `1` | hard cap on generated detector PRs |
| `DETECTOR_SCOUT_MAX_HITS` | `200` | G1 upper bound |
| `DETECTOR_SCOUT_MIN_HITS` | `3` | G1 lower bound |
| `DETECTOR_SCOUT_MIN_CONF` | `0.85` | G10 |
| `DETECTOR_SCOUT_LLM_BUDGET_USD_WEEK` | `5` | via `brain_llm_spend` |

Real PR opening additionally still requires `DCHUB_L22_REAL_PR=1` and a token —
inherited from the writer, not re-implemented.

---

## 7. Provenance and licence

Detectors are derived from other people's repositories. Permissive licences only
(MIT / Apache-2.0 / BSD-2 / BSD-3 / ISC / Unlicense); GPL and unlicensed repos are
rejected at the Filter, not later. Every generated artifact carries a comment
naming source repo, sha, path and licence, and the PR body repeats it. If a
candidate would copy more than a regex and a transform — i.e. actual code — it is
rejected; we take the *shape*, not the implementation.

---

## 8. Test conventions this must obey

From `CLAUDE.md`, both learned the hard way:

- **Nothing under `tests/` may run at module scope.** A module-scope `sys.exit()`
  aborts collection and kills the whole session — that shipped twice on
  2026-07-28 and left the backend with no test gate for hours.
  `scripts/check_collection_safety.py` blocks it in `syntax-check`; a generated
  test that trips it fails CI, which is the desired outcome but a slow one, so
  the Generator emits function-scoped tests only.
- **Tests never import `main.py`.** Generated tests pull the function out of
  source with `ast` and execute it against stubs, like the existing brain tests.
- The generated test must assert the detector's own dry-run invariants (G3, G4,
  G6) so they are re-checked on every future run, not only at generation time.

---

## 9. Failure modes, and how we would know

Named up front because the repo's own history says these are the ones that bite.

| Failure | How it presents | Detection |
|---|---|---|
| `klass` mismatch between spec and class | sweep spec silently dropped; detector ships as a no-op; PR merges green | G5 at score time; plus a post-merge assertion that every `ALLOWLIST_CLASSES` entry has a matching sweep spec and vice versa |
| Regex too greedy | thousands of proposals, or a mass-edit PR | G1/G2 upper bounds |
| Scout finds nothing for weeks | pipeline reports "ok" with zero candidates | the white-glove lane must read *candidates produced*, not *ticks completed* — see below |
| Green run ≠ landed | `weekly-shadow-audit` reported success for two weeks while its push was rejected and swallowed by `\|\| true` | assert the PR URL exists in `detector_candidates.pr_url`, not that the workflow exited 0 |
| LLM spend runs away | quiet bill | `DETECTOR_SCOUT_LLM_BUDGET_USD_WEEK`, checked before each Extractor call |

The white-glove BRAIN lane should gain a second check reading
`detector_candidates` — *candidates scored in 30d* and *detectors merged in 90d* —
and it should stay red until a generated detector has actually merged and produced
a merged autofix PR. Not when the pipeline runs. When it works.

---

## 10. Rollout

Each phase has an exit criterion. Do not start the next one until it is met.

**Phase 0 — Scout + Filter, shadow.** No LLM, no PRs. Endpoint + table + read-only
surface. *Exit: ≥20 repos surviving Filter over 2 weeks, hand-eyeballed as
plausibly containing rule corpora. If the funnel is empty, the query set is wrong
and stages 3-6 are premature.*

**Phase 1 — Reader + Extractor, shadow.** LLM spend capped. Candidates stored,
never scored against the tree. *Exit: ≥5 candidates whose `buggy_form` a human
agrees is a real, recurring shape.*

**Phase 2 — Score.** The dry-run harness. This is the highest-value phase and can
be built and run against the six *existing* classes first as a self-test: a
correct harness must pass all six. *Exit: all six existing detectors pass G1-G10
when replayed through it.*

**Phase 3 — Generator, `DETECTOR_SCOUT_DRAFT_PR=0`.** Emits the artifact set to
the surface. A human copies one out by hand and opens the PR. *Exit: one
hand-carried generated detector merged and producing merged autofix PRs.*

**Phase 4 — Publisher on, 1 PR/week.** Only after Phase 3's detector has a track
record.

Phase 2 is where to spend the effort. Phases 0-1 are plumbing; Phase 2 is the
thing that makes this safe enough to ever reach Phase 4.

---

## 11. Open questions

1. Should the Scout also read *our own* merged-PR history for shapes we fixed by
   hand more than twice? That is a strictly cheaper source than GitHub and needs
   no licence handling. Possibly it should be Phase 0 instead.
2. `_KNOWN_BUG_PATTERNS` is a literal-string list, so it cannot express every
   regex a detector might use. Does artifact 3 need to become regex-capable, or do
   we constrain generated detectors to shapes with a literal anchor?
3. G9's audited-token set is currently hand-maintained. Generated detectors will
   want tokens outside it. Who signs off — and does that make Phase 4 permanently
   human-gated in practice?
