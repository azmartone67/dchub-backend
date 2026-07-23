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

## Converted in this PR (central chokepoints + highest traffic)

| Site | What | Coverage |
|---|---|---|
| `utils/anthropic_helper.py` | added `cached_system()` | the helper |
| `routes/brain_llm_structured.py` `build_messages_body()` | caches `system` (opt-out `cache_system=False`) | **6 brain callers**: brain_answer_cache, brain_lane_driver, brain_feature_proposer, brain_strategic_planner, brain_investigator, analyst_note |
| `agent_hub.py` `call_claude()` | caches `system_prompt` | all `call_claude` callers |
| `ai_agent.py` | caches `CHAT_PROMPT` (public chat endpoint) | 1 |

`brain_lane_driver` already wrapped its own system in `cache_control`; because
`cached_system()` no-ops on a non-`str`, there's no double-wrap.

## Remaining STATIC sites — apply the same one-liner (verified static constants)

Each is `"system": <CONST>` in a raw body **or** `system=<CONST>` in an SDK call,
where `<CONST>` is a module-level constant. Convert to `cached_system(<CONST>)`
and add `from utils.anthropic_helper import cached_system`:

- `ai_wars_automation.py:571` `SYSTEM_PROMPT`, `:800` `MCP_SYSTEM_PROMPT`
- `routes/dcpi_ask.py:79` `SYSTEM_PROMPT`  (public "ask" endpoint — do this next)
- `routes/demo.py:294` `DEMO_SYSTEM_PROMPT`
- `routes/feedback_triage.py:203` `_TRIAGE_SYSTEM`
- `routes/geo_autopublish.py:153` `_SYSTEM`
- `routes/linkedin_content_engine.py:619` `_VOICE_SYSTEM`
- `routes/media_citation_gap.py:335` `_DRAFT_SYSTEM`
- `routes/media_journalist_lane.py:402` `_VOICE_SYSTEM`
- `routes/media_recurring_formats.py:510` `_VOICE_SYSTEM`
- `routes/wins_poster.py:388` `ANALYST_VOICE`
- SDK: `extractor_cron.py:131` `_SYSTEM_PROMPT`,
  `routes/media_comment_engagement.py:614` `_PROMPT_SYSTEM`,
  `routes/media_dm_follow_up.py:729` `_DM_PROMPT_SYSTEM`,
  `routes/media_spike_responder.py:558` `_PROMPT_SYSTEM`,
  `routes/sales_outreach_automator.py:930` `_OUTREACH_SYSTEM_PROMPT`

## Needs a human check before converting (local `system` var — trace the source)

These pass a local `system` variable to their own raw body. Most route through
`build_messages_body` (already cached) — confirm, and only wrap the ones whose
`system` is a **stable** value: `content_publisher.py:2315`, `main.py:16927`,
`routes/agentic_master_shell.py:272`, `routes/ai_platform_onboarder.py:253`,
`routes/brain_inspector.py:788`, `routes/brain_v2_layer4.py:355`,
`routes/media_*` (`_data_story_factory`, `_reactive_news`, `_thread_generator`),
`routes/news_entity_extraction.py:283`.

## Do NOT convert (dynamic prefix — would cost more)

- `routes/brain_lane_driver.py:422` `_CHARTER.format(kpi_table=…)` — per-request.
  (It already handles its own caching decision.)
