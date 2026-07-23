# Prompt caching (Anthropic native prefix cache)

Anthropic emailed that DC Hub's **prompt cache hit rate is low** — caching the
repeated system-prompt/tool prefix could save up to ~37% of direct API spend.
This is Anthropic's **native prefix cache** (`cache_control`), which is separate
from the Cloudflare AI Gateway whole-response cache we already use (`utils/anthropic_helper.aig_metadata_headers`). Both help; this doc is about the native one.

## The one transform

Wrap the **static** system prompt in a `cache_control: ephemeral` block. Use the
helper so it's uniform and safe:

```python
from utils.anthropic_helper import cached_system

# SDK
client.messages.create(model=..., system=cached_system(SYSTEM_PROMPT), messages=[...])

# raw /v1/messages body
body = {"model": ..., "system": cached_system(SYSTEM_PROMPT), "messages": [...]}
```

`cached_system(p)` returns `[{"type":"text","text":p,"cache_control":{"type":"ephemeral"}}]`
for a non-empty `str`, and returns the input **unchanged** for anything else
(None / "" / an already-built block list) — so it's idempotent and safe to apply
broadly.

## Rules (why "safe" sweep, not blanket)

1. **Only cache STATIC prompts.** A per-request `f"..."` / `.format(...)` system
   prefix changes bytes every call, so the prefix hash never matches → you pay a
   cache **write** every time and never read = **net cost increase**. Leave those
   as plain strings (or cache only the stable head, per the Anthropic docs).
2. **Per-model minimums matter.** Below the minimum, caching is a silent no-op:
   - Opus 4.8 / Sonnet 5 / Sonnet 4.6 / Sonnet 4.5: **1,024 tokens**
   - Opus 4.6 / Opus 4.5: **4,096 tokens**
   - **Haiku 4.5: 4,096 tokens** ← several DC Hub SDK calls use Haiku; a short
     chat/system prompt there won't cache. Still safe to mark (no-op), but the
     savings land mostly on the Opus/Sonnet brain calls with big charters.
3. **tools → system → messages** hierarchy: marking the end of `system` also
   caches any `tools` (they come first). Big tool schemas = extra savings.
4. **Verify:** on the 2nd+ identical-prefix call within 5 min, check
   `resp.usage.cache_read_input_tokens > 0`. If it's `0` and
   `cache_creation_input_tokens` is `0` too, the prefix was under the model's
   minimum or the breakpoint sat on changing content.

## Converted in this PR — FULL safe sweep of static system prompts

**Central chokepoints (cover many callers each):**
- `utils/anthropic_helper.py` — added `cached_system()`.
- `routes/brain_llm_structured.py` `build_messages_body()` — caches `system` by
  default (`cache_system=False` opt-out). Covers **6 brain callers**:
  brain_answer_cache, brain_lane_driver, brain_feature_proposer,
  brain_strategic_planner, brain_investigator, analyst_note.
- `agent_hub.py` `call_claude()` — covers all `call_claude` callers.

**Individual static-prompt call sites (all wrapped with `cached_system(...)`):**
`ai_agent.py` (CHAT_PROMPT) · `ai_wars_automation.py` (SYSTEM_PROMPT,
MCP_SYSTEM_PROMPT) · `routes/dcpi_ask.py` · `routes/demo.py` ·
`routes/feedback_triage.py` · `routes/geo_autopublish.py` ·
`routes/linkedin_content_engine.py` · `routes/media_citation_gap.py` ·
`routes/media_journalist_lane.py` · `routes/media_recurring_formats.py` ·
`routes/wins_poster.py` · `extractor_cron.py` ·
`routes/media_comment_engagement.py` · `routes/media_dm_follow_up.py` ·
`routes/media_spike_responder.py` · `routes/sales_outreach_automator.py` ·
`routes/ai_platform_onboarder.py` (sysmsg — verified pure-literal) ·
`routes/news_entity_extraction.py` (system — verified pure-literal).

`brain_lane_driver` already wrapped its own system in `cache_control`; because
`cached_system()` no-ops on a non-`str`, there's no double-wrap.

## Deliberately NOT converted (would raise cost or unverifiable)

- **Dynamic prefix (per-request bytes → wasted cache writes):**
  - `main.py:16912` `system_prompt = f"{role} …"` — f-string keyed on `role`.
  - `routes/brain_lane_driver.py:422` `_CHARTER.format(kpi_table=…)` — already
    makes its own caching decision.
- **`system` is a function PARAM (can't prove static from the call site alone) —
  trace the callers, then wrap if stable:** `routes/agentic_master_shell.py:272`,
  `routes/brain_inspector.py:788`, `routes/brain_v2_layer4.py:355`,
  `routes/media_data_story_factory.py`, `routes/media_reactive_news.py`,
  `routes/media_thread_generator.py`.
- **`content_publisher.py:2284`** `sys_prompt = "…" + _canon + "…"` where `_canon`
  is a *function-local* var — confirm `_canon` is pure-static, then wrap.
