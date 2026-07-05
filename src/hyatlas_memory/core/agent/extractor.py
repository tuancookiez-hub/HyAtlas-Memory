"""
Agent Memory - Extractor 提取智能体

统一 Prompt 提取: 单次 LLM 调用提取 memory / intentions / basic_info。
  - memory: 一切值得长期记忆的信息（偏好/观点/事件/计划/经历/习惯/决定）
  - intentions: 用户未来要做的前瞻意图（带 valid_until 截止日）
  - basic_info: 结构化基础画像 kv

LLM 只需感知 memory 与 intention 两类，无需感知内部分层（layer）。

另保留 deep_extract (System 2 深度分析) 供后台精炼使用。
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import re

from .llm_provider import LLMProvider
from ..config import LLMConfig as GlobalLLMConfig

logger = logging.getLogger(__name__)


# ================================================================
# 数据结构
# ================================================================


class ExtractMode(str, Enum):
    """提取模式"""

    LIGHT = "light"  # System 1 轻量提取
    DEEP = "deep"  # System 2 深度提取


@dataclass
class ExtractResult:
    """提取结果 (V1 兼容)"""

    success: bool
    info: Dict[str, Any] = field(default_factory=dict)
    suggested_layer: Optional[str] = None
    confidence: float = 0.0
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None
    error_code: Optional[str] = (
        None  # "EMPTY_RESPONSE" / "JSON_PARSE_FAILED" / "LLM_ERROR"
    )
    raw_response: Optional[str] = (
        None  # LLM 原始返回（失败时保留，用于写 pipeline_log）
    )
    tool_calls_only: bool = False  # True 当 LLM 只返回了 tool_calls，content 为空
    _actual_prompt: Optional[str] = (
        None  # 实际发送给 LLM 的 user prompt（用于 trace 日志）
    )
    _actual_system_prompt: Optional[str] = None  # 实际的 system prompt


@dataclass
class V2ExtractResult:
    """V2 提取结果 (丰富结构)"""

    success: bool
    mode: ExtractMode = ExtractMode.LIGHT

    # 事实提取
    facts: List[Dict[str, Any]] = field(default_factory=list)

    # 实体与关系
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relations: List[Dict[str, Any]] = field(default_factory=list)

    # 情绪标注
    emotional_valence: float = 0.0
    emotional_arousal: float = 0.0

    # 时间信息
    temporal_expressions: List[Dict[str, Any]] = field(default_factory=list)

    # 意图检测
    intentions: List[Dict[str, Any]] = field(default_factory=list)

    # 画像信息
    profile_updates: List[Dict[str, Any]] = field(default_factory=list)

    # 更新类型判断 (深度模式)
    update_classifications: List[Dict[str, Any]] = field(default_factory=list)

    tokens_used: int = 0
    error: Optional[str] = None


# ================================================================
# Prompt 模板
# ================================================================

# 统一提取 Prompt
#
# 设计要点（v0.3.45_v0 post8）：
#   - 不再输出 basic_info 字段；稳定结构化属性（name/age/location/occupation/employer）
#     由 LLM 通过 function-calling tool `update_basic_user_profile` 表达。
#   - 不再输出 time_ref 字段。时间相关信息如果是时间敏感的（schedule / past event），
#     让 LLM 把已解析的时间自然嵌入 content 即可；稳定偏好类 content 保持不含时间。

# System prompt（固定，不参与 format）
EXTRACT_SYSTEM_PROMPT = """# ROLE

You are a Memory Extractor.

Extract durable, high-value memories from conversations and convert them into structured, self-contained statements.

Only extract information that is likely to matter in future conversations.

---

# INPUTS

You will receive:

* Last k Messages
* New Messages
* Memory Date
* Current Date

Use:

* Last k Messages only for coreference resolution
* Memory Date as the ONLY temporal anchor
* Current Date only as metadata, never for resolving relative time

---

# WHAT TO EXTRACT

Extract ONLY genuinely new information introduced in the NEW user messages.

Good memories include:

* preferences
* long-term projects
* habits
* goals
* recurring activities
* opinions
* relationships
* important experiences
* stable traits
* future plans
* decisions
* constraints
* accepted recommendations

Do NOT extract:

* small talk
* acknowledgements
* generic emotions
* temporary conversational context
* assistant reminders of old memories
* information already implied by prior context
* vague personality judgments

---

# USER AS SUBJECT

Every memory must:

* be self-contained
* be written in third person
* describe the user explicitly

English:

* "The user ..."

Chinese:

* "用户..."

---

# LANGUAGE RULES

* Output language must match input language.
* Preserve original scripts and technical terms exactly.
* Never transliterate names, brands, or equations.

---

# TWO KINDS OF MEMORY

You extract two kinds of memory: **memory** and **intentions**.

## memory

Everything durable and worth remembering about the user goes here:

* preferences, opinions, values, attitudes
* events, actions, experiences
* habits, behavioral patterns
* relationships, traits
* objective facts, decisions, constraints
* ongoing activities

Implicit preferences MUST be inferred when strongly suggested
(e.g. resuming an activity → positive interest; avoiding something →
negative preference; repeated voluntary effort → sustained interest).

Do NOT separate "what kind of person the user is" from "what happened".
Both go into `memory`.

## intentions

A forward-looking thing the user **plans or intends to do in the future**,
which can be acted on and which becomes irrelevant once done or past.

Examples:

* "The user has a job interview next Tuesday."
* "The user plans to move apartments at the end of the month."
* "The user intends to finish the report before Friday."

NOT intentions (these belong in `memory`):

* stable preferences / traits ("The user likes basketball")
* completed past events ("The user moved to Beijing last year")
* general goals with no actionable future step

### valid_until

For each intention, provide `valid_until`: the date after which the
intention no longer matters (the deadline / target date), resolved
against **Memory Date** (e.g. "next Tuesday" → that absolute date).
If no deadline can be determined, set `valid_until` to `null`.

---

# OWNER

Every memory and intention MUST carry an `owner` saying who it belongs to:

* `"user"` — facts/preferences/traits about the user, things the user states or
  plans to do themselves.
* `"agent"` — information the assistant provides (recommendations, confirmations,
  things it researched), or a future action the user wants the **assistant** to
  perform (e.g. "remind me in 8 hours" → an agent intention).

Decide `owner` per item. When unsure, default to `"user"`.

---

# TEMPORAL RULES

Resolve relative time using Memory Date.

Examples:

* "next week"
* "last month"
* "tomorrow"

If time cannot be resolved safely:

* remove the relative wording
* keep the memory atemporal

Do not invent chronology.

---

# QUALITY RULES

Each memory must be:

* self-contained
* contextually complete
* retrieval-friendly
* information-dense

Retain important details:

* names
* products
* locations
* numbers
* equations
* constraints

Avoid fragmentary memories lacking context.

---

# TAGS

Each memory must include 1–3 concise lowercase tags.

Examples:

* work
* travel
* ai
* gaming
* health

---
"""

# Few-shot examples (appended to the system prompt only when few_shot_enabled).
# Each example mirrors the real input shape (Last k Messages / New Messages /
# Memory Date) and our output schema (memory / intentions / basic_info with
# owner & tags), and ends with a one-line takeaway.
EXTRACT_FEW_SHOT_EN = """
---

# EXAMPLES

## Example 1 — Boundary: old context is for resolution only, never re-extracted

Last k Messages:
[{"role": "user", "content": "I've been looking at rescue dogs at the shelter downtown."},
 {"role": "assistant", "content": "That's lovely — found one you like?"}]
New Messages:
[{"role": "user", "content": "Yeah, picked her up yesterday — a 3-year-old border collie named Luna."}]
Memory Date: 2025-04-10

Output:
```json
{
  "memory": [
    {"content": "The user adopted a 3-year-old border collie named Luna from a downtown shelter on 2025-04-09.", "tags": ["pets", "family"], "owner": "user"}
  ],
  "intentions": [],
  "basic_info": {}
}
```
The old messages only resolve "her" and supply context ("downtown shelter"). Do NOT emit a stale memory like "the user is considering adopting a dog" — only the genuinely new fact (the completed adoption) is extracted, with "yesterday" resolved against Memory Date.

## Example 2 — Condense a long assistant reply

New Messages:
[{"role": "user", "content": "Write me a short essay on why remote work boosts productivity."},
 {"role": "assistant", "content": "Sure — opening: 'Remote work has reshaped how teams operate...' [~2000 words of body omitted] ...closing: 'In short, autonomy and fewer interruptions drive higher output.'"}]
Memory Date: 2025-03-22

Output:
```json
{
  "memory": [
    {"content": "The assistant drafted a short essay for the user arguing that remote work boosts productivity because autonomy and fewer interruptions drive higher output.", "tags": ["writing", "remote-work"], "owner": "agent"}
  ],
  "intentions": [],
  "basic_info": {}
}
```
The assistant's reply is long. Never store the verbatim essay — distill it to topic + thesis. owner is "agent" because the assistant produced it. Bloated verbatim content makes memory unusable.

## Example 3 — Composite extraction: basic_info + intention + condensed agent plan

New Messages:
[{"role": "user", "content": "Hi, I'm Kenji. I'm planning a trip to Japan next month — could you put together a 7-day itinerary?"},
 {"role": "assistant", "content": "# 7-Day Classic Japan Golden Route (Tokyo → Kyoto → Nara → Osaka)\\nFly into Tokyo, Shinkansen south to Kyoto, day-trip Nara, depart from Osaka (Kansai).\\n\\n## Day 1: Arrival in Tokyo\\n- Land at Haneda/Narita, drop bags at hotel near Tokyo Station.\\n- Late afternoon: Sensoji Temple (Asakusa), wander Nakamise Street, try taiyaki.\\n- Evening: Shibuya Crossing + Shibuya Sky rooftop for sunset; izakaya dinner (yakitori, sake).\\n\\n## Day 2: Tokyo markets & landmarks — [omitted]\\n## Day 3: Shinkansen to Kyoto, Fushimi Inari, Gion — [omitted]\\n## Day 4: Kyoto temples (Kiyomizu-dera, Kinkaku-ji, Ginkaku-ji) — [omitted]\\n## Day 5: Nara day trip (Nara Park deer, Todai-ji) — [omitted]\\n## Day 6: Osaka (Osaka Castle, Dotonbori street food) — [omitted]\\n## Day 7: Osaka sightseeing + depart from Kansai Airport — [omitted]\\n\\nTip: buy a 7-day JR Pass before arriving. Want me to adjust anything?"}]
Memory Date: 2025-09-15

Output:
```json
{
  "memory": [
    {"content": "The assistant created a 7-day Japan itinerary for the user along the classic golden route: Tokyo (Asakusa, Shibuya), Shinkansen to Kyoto (Fushimi Inari, Kiyomizu-dera, Kinkaku-ji, Gion), a Nara day trip (deer park, Todai-ji), then Osaka (Osaka Castle, Dotonbori), departing from Kansai Airport.", "tags": ["travel", "japan", "itinerary"], "owner": "agent"}
  ],
  "intentions": [
    {"content": "The user plans to take a 7-day trip to Japan in October 2025.", "tags": ["travel", "japan"], "valid_until": "2025-10-31", "owner": "user"}
  ],
  "basic_info": {"name": "Kenji"}
}
```
One turn produces three outputs in different buckets: the name goes to basic_info, the trip is a forward-looking intention with a deadline, and the assistant's long multi-section itinerary is condensed into ONE dense agent memory — the route and key stops per city, NOT every day's bullets, tips, or food lists. Storing the whole reply verbatim would bloat memory; storing the skeleton keeps it queryable.

## Example 4 — A later message confirming/adopting an earlier recommendation is itself a fact

New Messages:
[{"role": "user", "content": "My 6-year-old won't eat any vegetables. Any ideas?"},
 {"role": "assistant", "content": "Try blending spinach or carrots into pasta sauce — the color and flavor mostly disappear, and many kids accept it without noticing."},
 {"role": "user", "content": "Oh nice, we'll try that tonight. By the way, what's a good bedtime for that age?"},
 {"role": "assistant", "content": "For a 6-year-old, aim for 7–9 PM with about 10–11 hours of sleep."}]
Memory Date: 2025-06-20

Output:
```json
{
  "memory": [
    {"content": "The user has a 6-year-old child who refuses to eat vegetables, and accepted the assistant's suggestion to blend spinach or carrots into pasta sauce to hide them.", "tags": ["parenting", "food"], "owner": "user"},
    {"content": "The assistant advised the user that a 6-year-old should aim for a 7–9 PM bedtime with about 10–11 hours of sleep.", "tags": ["parenting", "sleep"], "owner": "agent"}
  ],
  "intentions": [],
  "basic_info": {}
}
```
In the third message the user said "we'll try that tonight" — explicitly adopting the earlier suggestion. That acceptance is valuable and must be recorded ("accepted the assistant's suggestion to ..."), not discarded as small talk. The bedtime advice was just given and not yet adopted, so it is recorded as advice (owner "agent").

## Example 5 — RAG/injected context: never store the injected text

New Messages:
[{"role": "user", "content": "[Retrieved context: The Eiffel Tower is 330m tall and was completed in 1889.] Given the above, I'm going to propose to my girlfriend at the top of the Eiffel Tower this summer."}]
Memory Date: 2025-05-02

Output:
```json
{
  "memory": [],
  "intentions": [
    {"content": "The user plans to propose to his girlfriend at the top of the Eiffel Tower in summer 2025.", "tags": ["relationship", "travel"], "valid_until": "2025-08-31", "owner": "user"}
  ],
  "basic_info": {}
}
```
The bracketed retrieved context (the tower's height and year) is RAG-injected reference material, NOT something the user told us about themselves — do NOT store it. Only the user's own forward-looking plan is extracted. System/RAG-injected text is context for the model, never a memory.

## Example 6 — Shared reference material: extract the content, not the sharing action

New Messages:
[{"role": "user", "content": "Remember my grandma's dumpling recipe so I don't lose it. Filling: 500g ground pork, 300g napa cabbage, 2 tbsp soy sauce, 1 tbsp sesame oil, a thumb of ginger minced. Cabbage salted 15 min then squeezed dry. Boil dumplings, add cold water twice, ready when they float."},
 {"role": "assistant", "content": "Got it, saved!"}]
Memory Date: 2025-07-01

Output:
```json
{
  "memory": [
    {"content": "The user's grandmother's dumpling recipe: filling of 500g ground pork, 300g napa cabbage (salted 15 minutes then squeezed dry), 2 tbsp soy sauce, 1 tbsp sesame oil, and a thumb of minced ginger; boil the dumplings, add cold water twice, and they are done when they float.", "tags": ["food", "recipe", "family"], "owner": "user"}
  ],
  "intentions": [],
  "basic_info": {}
}
```
Extract the actual factual content (ingredients, quantities, the salt-and-squeeze step, the boiling method) — NOT "the user shared a recipe" or "the user asked to remember the recipe". The sharing action is worthless; the content is what's queryable.

## Example 7 — Structured numbers and attributes preserved exactly

New Messages:
[{"role": "user", "content": "Logging my home-gym PRs this cycle: Squat 140kg x 3, Bench 100kg x 5, Deadlift 180kg x 2, Overhead Press 60kg x 6. Bodyweight 78kg, 3 sets per lift, 3 min rest."},
 {"role": "assistant", "content": "Logged — strong numbers!"}]
Memory Date: 2026-01-20

Output:
```json
{
  "memory": [
    {"content": "The user's current lifting PRs are Squat 140kg x 3, Bench 100kg x 5, Deadlift 180kg x 2, and Overhead Press 60kg x 6, at a bodyweight of 78kg.", "tags": ["fitness", "strength"], "owner": "user"},
    {"content": "The user trains with 3 sets per lift and 3 minutes of rest between sets.", "tags": ["fitness", "training"], "owner": "user"}
  ],
  "intentions": [],
  "basic_info": {}
}
```
Every number and unit (kg, reps, sets, rest time) is preserved exactly — dropping them would destroy the most queryable information. Split into two clean topics (PRs vs. training protocol), but never round or omit the specifics.

## Example 8 — Long multi-topic conversation: do not stop after the first topic

New Messages:
[{"role": "user", "content": "Big week — I finally defended my PhD thesis on Tuesday!"},
 {"role": "assistant", "content": "Huge congrats! How do you feel?"},
 {"role": "user", "content": "Relieved! I also signed a lease on an apartment in Berlin and move next month. And I've started learning German on Duolingo — 30-day streak. My partner Sofia is staying in Madrid for now, which is hard."}]
Memory Date: 2025-11-08

Output:
```json
{
  "memory": [
    {"content": "The user defended their PhD thesis on 2025-11-04.", "tags": ["education", "milestone"], "owner": "user"},
    {"content": "The user signed a lease on an apartment in Berlin.", "tags": ["housing"], "owner": "user"},
    {"content": "The user has been learning German on Duolingo and has a 30-day streak.", "tags": ["language", "learning"], "owner": "user"},
    {"content": "The user's partner Sofia is staying in Madrid for now, which the user finds hard.", "tags": ["relationship"], "owner": "user"}
  ],
  "intentions": [
    {"content": "The user plans to move to Berlin next month (around December 2025).", "tags": ["housing", "relocation"], "valid_until": "2025-12-31", "owner": "user"}
  ],
  "basic_info": {}
}
```
Four+ topics — thesis defense, the Berlin apartment, learning German, and the partner in Madrid. Do not stop after the first topic. Note one turn split into a completed fact (lease signed) AND an intention (the upcoming move), and the emotional-but-substantive detail about Sofia is kept because it carries lasting relationship context.

---
"""


EXTRACT_PROMPT = """## Last k Messages

{last_messages}

---

## New Messages

{content}

---

## Memory Date

{memory_at}

---

## Current Date

{current_date}

---

{basic_profile_section}# Task

Extract only durable, high-value memories newly introduced in the NEW user messages.

Use Last k Messages only for:

* resolving references
* understanding context

Do NOT extract:

* small talk
* acknowledgements
* temporary conversational details
* repeated information already known
* assistant restatements of old memories
* generic emotions without lasting significance

Each memory must:

* be self-contained
* preserve important context
* be retrieval-friendly
* describe the user explicitly in third person

Use:

* "The user ..." for English
* "用户..." for Chinese

Resolve relative time using Memory Date when possible.

---

# Output Format

Return ONLY a JSON object inside a ```json fenced block.

```json
{{
  "memory": [
    {{
      "content": "...",
      "tags": ["..."],
      "owner": "user"
    }}
  ],
  "intentions": [
    {{
      "content": "...",
      "tags": ["..."],
      "valid_until": "YYYY-MM-DD",
      "owner": "user"
    }}
  ],
  "basic_info": {{}}
}}
```

Rules:

* `memory` = durable info worth remembering: preferences, opinions, attitudes,
  traits, events, plans, experiences, habits, decisions
* `intentions` = forward-looking things the user plans/intends to do in the
  future, with `valid_until` = the deadline/target date (resolved against
  Memory Date) or `null` if none
* `owner` (required on every memory & intention) = `"user"` or `"agent"`
  (see "# OWNER" above; default `"user"` when unsure)
* `basic_info` = ONLY include fields **explicitly stated** in the NEW user messages
  for the basic profile schema (see "## User Basic Attributes" section above).
  Never infer, never invent. If no field is explicitly stated, output `{{}}`.
* No duplicate information across sections
* Tags must contain 1–3 concise lowercase keywords
* Use empty arrays `[]` or `{{}}` when nothing is extracted
* Return valid JSON only

Output the JSON now."""


# ================================================================
# EXTRACT PROMPT —— migration scene (English)
# ================================================================
# Only used when LLMConfig.extract_scene == "migration" (env: MEMORY_EXTRACT_SCENE=migration).
# Unlike the default EXTRACT_SYSTEM_PROMPT, the input is not a live conversation but the
# user's already-curated memory (MEMORY.md / journals / existing memory entries / past
# sessions). Hence fidelity-first: atomic, preserve concrete details verbatim, no value
# filtering, cover the whole input; dedup is left to the downstream reconcile step.
# The output JSON schema is identical to the default prompt, so downstream parsing is
# unchanged. Default conversation extraction is unaffected.
EXTRACT_SYSTEM_PROMPT_MIGRATION = """# ROLE

You are a Memory Migration Extractor.

Migrate the user's **already-curated memories** — e.g. MEMORY.md, journal notes, existing memory entries, or historical session logs — into structured, self-contained, retrievable memories.

Unlike live-conversation extraction, the input here has **usually already been curated / distilled** and is high-value by default. Your top priority is **fidelity**: preserve information completely and faithfully, rather than filtering or compressing it again.

---

# INPUTS

You will receive:

* Last k Messages — usually empty in migration; when present, use only for coreference resolution and context
* New Messages — this is actually the **raw memory text to migrate**
* Memory Date — the original timestamp of this memory (from filename / file mtime / session metadata); the ONLY anchor for resolving relative time
* Current Date — system metadata only, never for temporal reasoning

---

# CORE PRINCIPLE: FIDELITY FIRST

* By default every concrete piece of information in the input is worth keeping — do NOT drop anything for "looking unimportant".
* **Atomicity**: if a passage contains multiple independent facts / preferences / numbers / decisions, split them into **multiple** separate memories. Prefer many precise memories over one vague merged memory.
* **Preserve concrete details verbatim**: language preferences, amounts / numbers / units, names of people / products / projects, locations, dates, tech stack, config values, constraints — keep them exactly; never round, generalize, or omit.
* **Cover the whole input**: walk through every topic and extract item by item; do not stop at the first few, do not miss the later part.
* **Deduplication is not your job**: whether something already exists is handled by the downstream reconcile step; extract faithfully, do not skip because "it might already exist".

The only things you may skip are pure noise:

* markdown heading lines / horizontal rules / tables of contents / blank lines / pure formatting symbols themselves (but keep the substantive content under them)
* completely contentless small talk

---

# USER SUBJECT RULE

Each memory must be self-contained, understandable out of context, explicitly about the user, in third person ("The user ..." / "用户...").

---

# LANGUAGE RULE

Output language must match the input language. Preserve original terms, brand/project names, formulas, and code; do not transliterate or translate proper nouns.

---

# TWO KINDS OF MEMORIES

You extract **memory** and **intentions**.

* memory = any durable user info: preferences, opinions, attitudes, values, events, actions, experiences, habits, relationships, stable traits, facts, decisions, constraints, ongoing activities.
* intentions = forward-looking things the user plans/intends to do that lose meaning once done or expired; give `valid_until` (deadline/target date resolved against Memory Date, or null). Stable preferences and completed past events are NOT intentions — put them in memory.

---

# OWNER

Every memory and intention carries an `owner`:

* `"user"` — facts/preferences/traits about the user, or things the user does themselves. Migration sources are mostly the user's own memories, so **default to `"user"`**.
* `"agent"` — information clearly produced/recommended/researched by the assistant, or future actions the user wants the assistant to perform.

Decide per item; default `"user"` when unsure.

---

# TEMPORAL RULES

Resolve relative time using Memory Date. If it cannot be resolved safely, drop the relative wording and keep the memory atemporal. Do not invent chronology.

---

# QUALITY RULES

Each memory: information-complete, self-contained, retrieval-friendly, information-dense. **Always retain** names, products/projects, locations, numbers and units, formulas, language preferences, and constraints — dropping them ruins the most retrievable information.

---

# TAGS

Each memory includes 1–3 concise lowercase tags (e.g. ai, work, travel, finance, language).
"""

EXTRACT_PROMPT_MIGRATION = """## Last k Messages

{last_messages}

---

## Memory Text To Migrate (New Messages)

{content}

---

## Memory Date

{memory_at}

---

## Current Date

{current_date}

---

{basic_profile_section}# Task

Faithfully and completely extract the information in the "Memory Text To Migrate" above into structured memories.

* This is the user's already-curated memory; by default everything is worth keeping — **fidelity first**.
* **Atomicity**: when a passage holds multiple independent facts / preferences / numbers, split into multiple memories; do not merge or generalize.
* **Preserve verbatim** every concrete detail: language preferences, amounts / numbers / units, names of people / products / projects, locations, dates, tech stack, constraints.
* **Cover every topic** in the input; do not stop at the first few.
* Resolve relative time using Memory Date; if not resolvable, drop the relative wording.
* Deduplication is handled downstream; extract faithfully, do not skip because "it might already exist".

Each memory must be self-contained, preserve full context, be retrieval-friendly, and describe the user explicitly in third person ("The user ..." / "用户...").

---

# Output Format

Return ONLY a JSON object inside a ```json fenced block.

```json
{{
  "memory": [
    {{
      "content": "...",
      "tags": ["..."],
      "owner": "user"
    }}
  ],
  "intentions": [
    {{
      "content": "...",
      "tags": ["..."],
      "valid_until": "YYYY-MM-DD",
      "owner": "user"
    }}
  ],
  "basic_info": {{}}
}}
```

Rules:

* `memory` = durable info worth remembering (preferences, opinions, traits, events, plans, experiences, habits, decisions, constraints, numeric details)
* `intentions` = forward-looking things the user plans to do, with `valid_until` (resolved against Memory Date, or `null`)
* `owner` (required on every item) = `"user"` or `"agent"` (default `"user"` for migration)
* `basic_info` = ONLY fields **explicitly stated** in the text for the basic profile schema (see "## User Basic Attributes"); never infer, never invent; output `{{}}` if none
* No duplicate information across sections
* Tags must contain 1–3 concise lowercase keywords
* Use empty `[]` or `{{}}` when nothing is extracted
* Return valid JSON only

Output the JSON now."""


# ================================================================
# 基础画像 section 渲染（英文）
# ================================================================

def render_basic_profile_section_en(fields: Optional[Dict[str, str]]) -> str:
    """
    Render `{basic_profile_section}` block for EXTRACT_PROMPT (English).

    Returns "" when fields is empty — the prompt then has no basic_info
    schema section, but JSON output may still contain `basic_info: {}`.
    """
    if not fields:
        return ""

    lines: List[str] = []
    for k, desc in fields.items():
        if not k or not desc:
            continue
        lines.append(f"- `{k}`: {desc}")

    if not lines:
        return ""

    body = "\n".join(lines)
    return (
        "## User Basic Attributes (basic_info)\n\n"
        "If the NEW user messages **explicitly** state any of the following "
        "fields, extract them into a JSON `basic_info` object. Never infer, "
        "never invent. Omit any field that is not explicitly stated.\n\n"
        f"{body}\n\n"
        "---\n\n"
    )


# ================================================================
# 核心实现
# ================================================================


class Extractor:
    """
    提取智能体 (V2)

    支持 V1 兼容接口 + V2 双模式接口。
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        llm_config: Optional[GlobalLLMConfig] = None,
    ):
        self.llm = llm_provider
        self._llm_config = llm_config or GlobalLLMConfig()
        self._call_count = 0
        self._total_tokens = 0
        logger.info("Extractor V2 initialized")

    # ================================================================
    # V1 兼容接口
    # ================================================================

    async def extract(
        self,
        content: str,
        context: Dict[str, Any] = None,
        extract_types: List[str] = None,
        current_time: str = "",
        existing_tags: Optional[List[str]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_registry: Optional[Any] = None,
        tool_context: Optional[Dict[str, Any]] = None,
        history_context: str = "",
        basic_profile_fields: Optional[Dict[str, str]] = None,
        extract_scene: Optional[str] = None,
    ) -> ExtractResult:
        """
        统一提取：identity / facts / basic_info

        Args:
            content:       原始对话内容
            context:       业务上下文（目前透传，不使用）
            extract_types: 兼容参数（未使用）
            current_time:  当前时间字符串
            existing_tags: 可选，该用户已有的所有 tags（供 LLM 优先复用）
            tools:         **DEPRECATED**: 自 v0.1.5.13 起 extractor 不再调用 LLM
                           function-calling 工具——基础画像通过 prompt + JSON 输出
                           的 basic_info 字段表达。保留参数仅为向后兼容；传入也不
                           会被透传到 LLM API。
            tool_registry: **DEPRECATED**: 同上，不再使用。
            tool_context:  保留，作为 dispatch context 传给（未来可能扩展的）非 basic_info
                           tool；当前未使用。
            basic_profile_fields:
                可选，{field_name: description} 字段表，渲染到 EXTRACT prompt 的
                "用户基础属性" 段落，要求 LLM 在 JSON 输出 `basic_info` 字段中返回。
                None 或 {} 表示不渲染 prompt section（仍允许 basic_info: {} 输出）。
        """
        MAX_TOOL_ROUNDS = 5

        try:
            # 构建 last_messages（传入的 history_context 就是格式化好的历史对话文本）
            _last_messages = history_context or "(none)"

            # 构建日期字段
            from datetime import date as _date_cls

            _current_date = (
                _date_cls.today().isoformat()
            )  # 系统当前日期 e.g. "2026-05-23"
            # memory_at: 转为日精度
            if current_time:
                try:
                    from datetime import datetime as _dt_cls

                    _memory_at = _dt_cls.fromisoformat(current_time).strftime(
                        "%Y-%m-%d"
                    )
                except (ValueError, TypeError):
                    _memory_at = _current_date
            else:
                _memory_at = _current_date

            # Select prompt based on input language
            from ..utils.lang_detect import is_chinese

            # 抽取场景：'migration' 时改用迁移专用 prompt（保真优先、原子化、不做价值过滤），
            # 用于把用户已沉淀的历史记忆迁移进来；其余场景沿用默认对话提取，行为不变。
            # 优先用本次调用传入的 extract_scene（按请求），未传则回退到 LLMConfig 全局默认。
            # 这样同一台常驻 server 可按请求切换 normal/migration，无需为迁移单独起 server。
            _scene = (extract_scene or getattr(self._llm_config, "extract_scene", "chat") or "chat")
            _is_migration = _scene.lower() == "migration"

            if is_chinese(content):
                from .prompts_zh import (
                    EXTRACT_PROMPT_ZH,
                    EXTRACT_SYSTEM_PROMPT_ZH,
                    EXTRACT_FEW_SHOT_ZH,
                    render_basic_profile_section_zh,
                )

                _bp_section = render_basic_profile_section_zh(basic_profile_fields)
                if _is_migration:
                    from .prompts_zh import (
                        EXTRACT_PROMPT_MIGRATION_ZH,
                        EXTRACT_SYSTEM_PROMPT_MIGRATION_ZH,
                    )

                    system_prompt = EXTRACT_SYSTEM_PROMPT_MIGRATION_ZH
                    _user_template = EXTRACT_PROMPT_MIGRATION_ZH
                else:
                    system_prompt = EXTRACT_SYSTEM_PROMPT_ZH
                    if getattr(self._llm_config, "few_shot_enabled", False):
                        system_prompt = system_prompt + EXTRACT_FEW_SHOT_ZH
                    _user_template = EXTRACT_PROMPT_ZH
                prompt = _user_template.format(
                    content=content,
                    current_date=_current_date,
                    memory_at=_memory_at,
                    last_messages=_last_messages,
                    basic_profile_section=_bp_section,
                )
            else:
                _bp_section = render_basic_profile_section_en(basic_profile_fields)
                if _is_migration:
                    system_prompt = EXTRACT_SYSTEM_PROMPT_MIGRATION
                    _user_template = EXTRACT_PROMPT_MIGRATION
                else:
                    system_prompt = EXTRACT_SYSTEM_PROMPT
                    if getattr(self._llm_config, "few_shot_enabled", False):
                        system_prompt = system_prompt + EXTRACT_FEW_SHOT_EN
                    _user_template = EXTRACT_PROMPT
                prompt = _user_template.format(
                    content=content,
                    current_date=_current_date,
                    memory_at=_memory_at,
                    last_messages=_last_messages,
                    basic_profile_section=_bp_section,
                )

            # 累计 token 统计
            logger.debug(f"[extractor] user prompt: {prompt!r}")
            total_tokens = 0
            total_prompt_tokens = 0
            total_completion_tokens = 0
            all_tool_calls: List[Dict[str, Any]] = []
            all_tool_results: List[Dict[str, Any]] = []

            # 构造 messages（system + user）
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            # 保存实际 prompt 用于 trace 日志
            _actual_user_prompt = prompt
            _actual_sys_prompt = system_prompt

            # 第一轮调用
            # NOTE: tools / tool_choice 已废弃 — 基础画像走 prompt + JSON basic_info
            # 字段，而不是 LLM function-calling tool（弱模型乱填）。
            # 若调用方传入 tools/tool_registry，会被忽略。
            response = await self.llm.complete_messages(
                messages=messages,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=self._llm_config.temperature,
                tools=None,
                tool_choice=None,
            )
            self._call_count += 1
            total_tokens += response.tokens_used
            total_prompt_tokens += response.prompt_tokens
            total_completion_tokens += response.completion_tokens

            raw_text = response.content or ""

            # 多轮 tool-use loop（仅当传入 tool_registry 时启用）
            if response.tool_calls and tool_registry:
                from .tools.base import parse_tool_calls_from_json

                round_count = 0
                while response.tool_calls and round_count < MAX_TOOL_ROUNDS:
                    round_count += 1
                    logger.info(
                        f"[extractor] tool-use round {round_count}: "
                        f"{len(response.tool_calls)} tool call(s)"
                    )

                    all_tool_calls.extend(response.tool_calls)

                    # 追加 assistant 消息（含 tool_calls）
                    messages.append(
                        {
                            "role": "assistant",
                            "content": response.content or "",
                            "tool_calls": response.tool_calls,
                        }
                    )

                    # 执行每个 tool call
                    parsed_calls = parse_tool_calls_from_json(response.tool_calls)
                    for tc_raw, tc_parsed in zip(
                        response.tool_calls, parsed_calls or []
                    ):
                        tc_id = tc_raw.get("id") or ""
                        logger.info(
                            f"[extractor] tool call: name={tc_parsed.name}, "
                            f"tool_call_id={tc_id!r}, "
                            f"raw_keys={list(tc_raw.keys())}"
                        )
                        if not tc_id:
                            logger.error(
                                f"[extractor] tool_call_id is empty! "
                                f"raw tc: {json.dumps(tc_raw, ensure_ascii=False, default=str)[:500]}"
                            )
                        try:
                            t_res = await tool_registry.dispatch(
                                tc_parsed, tool_context or {}
                            )
                            result_content = json.dumps(
                                {
                                    "success": t_res.success,
                                    "data": t_res.data,
                                    "error": t_res.error,
                                },
                                ensure_ascii=False,
                                default=str,
                            )
                            all_tool_results.append(
                                {
                                    "tool": tc_parsed.name,
                                    "round": round_count,
                                    "success": t_res.success,
                                    "data": t_res.data,
                                    "error": t_res.error,
                                }
                            )
                        except Exception as tool_err:
                            logger.error(
                                f"[extractor] tool '{tc_parsed.name}' raised: {tool_err}",
                                exc_info=True,
                            )
                            result_content = json.dumps(
                                {"success": False, "error": str(tool_err)},
                                ensure_ascii=False,
                            )
                            all_tool_results.append(
                                {
                                    "tool": tc_parsed.name,
                                    "round": round_count,
                                    "success": False,
                                    "error": str(tool_err),
                                }
                            )

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": result_content,
                            }
                        )

                    # 下一轮 LLM 调用（已废弃 tools；保留 dispatch 路径仅用于
                    # 极端情况下 LLM 通过其他途径返回 tool_calls 的兼容处理）
                    response = await self.llm.complete_messages(
                        messages=messages,
                        max_tokens=self._llm_config.agent_max_tokens,
                        temperature=self._llm_config.temperature,
                        tools=None,
                        tool_choice=None,
                    )
                    self._call_count += 1
                    total_tokens += response.tokens_used
                    total_prompt_tokens += response.prompt_tokens
                    total_completion_tokens += response.completion_tokens
                    raw_text = response.content or ""

                if round_count >= MAX_TOOL_ROUNDS and response.tool_calls:
                    logger.warning(
                        f"[extractor] tool-use loop hit max rounds ({MAX_TOOL_ROUNDS})"
                    )

                logger.info(
                    f"[extractor] tool-use loop done: {round_count} round(s), "
                    f"{len(all_tool_calls)} tool call(s), content_len={len(raw_text)}"
                )

            elif response.tool_calls and not tool_registry:
                # 没有 tool_registry 时，透传 tool_calls 给上层（向后兼容）
                all_tool_calls = response.tool_calls

            self._total_tokens += total_tokens

            # 空响应判断
            if (not raw_text or not raw_text.strip()) and not all_tool_calls:
                logger.warning(f"[extractor] LLM returned empty response")
                return ExtractResult(
                    success=False,
                    error="LLM returned empty response",
                    error_code="EMPTY_RESPONSE",
                    raw_response=raw_text,
                    tokens_used=total_tokens,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                )

            # 解析 JSON
            info: Dict[str, Any]
            if raw_text and raw_text.strip():
                parsed = self._parse_json(raw_text)
                if parsed is None:
                    if not all_tool_calls:
                        logger.warning(
                            f"[extractor] JSON parse failed: {raw_text[:200]}"
                        )
                        return ExtractResult(
                            success=False,
                            error=f"JSON parse failed: {raw_text[:200]}",
                            error_code="JSON_PARSE_FAILED",
                            raw_response=raw_text,
                            tokens_used=total_tokens,
                            prompt_tokens=total_prompt_tokens,
                            completion_tokens=total_completion_tokens,
                        )
                    info = {}
                else:
                    info = parsed
            else:
                info = {}

            # 记录 tool 调用信息供上层（pipeline log / writer）使用
            if all_tool_calls:
                info["tool_calls"] = all_tool_calls
            if all_tool_results:
                info["tool_results"] = all_tool_results

            tool_calls_only = (
                bool(all_tool_calls)
                and not info.get("memory")
                and not info.get("facts")  # legacy compat
                and not info.get("intentions")
                and not info.get("identity")  # legacy compat
            )

            result = ExtractResult(
                success=True,
                info=info,
                confidence=0.8,
                raw_response=raw_text,
                tokens_used=total_tokens,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                tool_calls_only=tool_calls_only,
                _actual_prompt=_actual_user_prompt,
                _actual_system_prompt=_actual_sys_prompt,
            )
            return result
        except Exception as e:
            logger.error(f"Extractor.extract failed: {e}")
            return ExtractResult(
                success=False,
                error=str(e),
                error_code="LLM_ERROR",
                _actual_prompt=(
                    _actual_user_prompt if "_actual_user_prompt" in dir() else None
                ),
                _actual_system_prompt=(
                    _actual_sys_prompt if "_actual_sys_prompt" in dir() else None
                ),
            )

    async def extract_profile(
        self,
        content: str,
        context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """专门提取用户画像 (profile 已包含 preferences)"""
        result = await self.extract(content, context)
        if result.success:
            return result.info.get("profile", {})
        return {}

    async def extract_entities(
        self,
        content: str,
        context: Dict[str, Any] = None,
    ) -> List[Dict[str, Any]]:
        """提取实体"""
        result = await self.extract(content, context)
        if result.success:
            return result.info.get("entities", [])
        return []

    # ================================================================
    # 工具方法
    # ================================================================

    def _parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse JSON from LLM output. Handles reasoning model think blocks."""
        text = text.strip()
        # Strip think blocks from reasoning models (MiniMax-M3, DeepSeek-R1, etc.)
        # Handle closed think blocks: <think>...</think> or ⋖...⋗
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\u22d6.*?\u22d7", "", text, flags=re.DOTALL)
        # Handle UNCLOSED think blocks (truncated response — no closing tag):
        # strip from opening tag to first '{' or end of text
        text = re.sub(r"<think>.*?(?=\{)", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\u22d6.*?(?=\{)", "", text, flags=re.DOTALL)
        # Strip markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return None

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "call_count": self._call_count,
            "total_tokens": self._total_tokens,
            "avg_tokens_per_call": (
                self._total_tokens / self._call_count if self._call_count > 0 else 0
            ),
        }
