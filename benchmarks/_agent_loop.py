"""Shared function-calling agent loop for the ANFS effectiveness benchmarks.

This is the one piece of new infrastructure behind the C5 token-efficiency
eval. It mirrors codegraph's methodology — give a real agent a tool set and
*measure the bill* — instead of scripting the retrieval ourselves. Each arm
hands the SAME model the SAME task with a DIFFERENT tool set; the agent
decides how many calls to make and how much to read, so token cost and tool-
call count are what the agent actually produced, not what we hard-coded.

A `Tool` bundles the JSON schema the API needs with the Python handler that
executes the call. `run_agent` runs the converse-call-feed loop until the
model answers (no more tool calls) or `max_steps` is hit, accounting every
`usage` block along the way.

Nondeterministic and network-bound — a benchmark, not a CI test.
"""

import json
from dataclasses import dataclass

from deepseek_client import DEFAULT_MODEL, chat


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema for the function arguments
    handler: object  # callable(args: dict) -> str

    @property
    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class AgentResult:
    answer: str
    prompt_tokens: int  # raw input tokens summed over steps (incl. cache hits)
    completion_tokens: int
    cache_hit_tokens: int  # re-sent context served from prompt cache (cheap)
    cache_miss_tokens: int  # genuinely new input the model had to process
    tool_calls: int
    steps: int
    stopped: str  # "answer" | "max_steps"

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens

    @property
    def uncached_tokens(self):
        """The price-dominant work: new input + output, cache hits excluded.

        With prompt caching a multi-step loop re-sends its context every step
        but pays almost nothing for it; what actually differs between arms is
        how much NEW content each strategy pulls into the model. This is the
        honest, price-agnostic efficiency metric (codegraph's "input cached +
        output" credits caching the same way).
        """
        return self.cache_miss_tokens + self.completion_tokens


def run_agent(
    api_key,
    system,
    user,
    toolset,
    model=DEFAULT_MODEL,
    max_steps=12,
    max_tokens=2048,
):
    """Drive one agent to an answer over `toolset`, accounting tokens + calls.

    `toolset` is a list of `Tool`. Returns an `AgentResult`. A handler that
    raises is reported back to the model as an error string (a real agent must
    cope with a failed tool call) rather than aborting the run.
    """
    tools_spec = [t.schema for t in toolset]
    handlers = {t.name: t.handler for t in toolset}
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    prompt_tokens = completion_tokens = tool_calls = 0
    cache_hit_tokens = cache_miss_tokens = 0

    for step in range(1, max_steps + 1):
        message, usage = chat(
            api_key,
            messages,
            model=model,
            tools=tools_spec,
            max_tokens=max_tokens,
            return_message=True,
            return_usage=True,
        )
        prompt_tokens += usage.get("prompt_tokens", 0)
        completion_tokens += usage.get("completion_tokens", 0)
        hit = usage.get("prompt_cache_hit_tokens")
        # DeepSeek reports hit/miss; fall back to "all miss" if a provider omits them.
        if hit is None:
            cache_miss_tokens += usage.get("prompt_tokens", 0)
        else:
            cache_hit_tokens += hit
            cache_miss_tokens += usage.get(
                "prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit
            )
        messages.append(message)

        calls = message.get("tool_calls") or []
        if not calls:
            return AgentResult(
                answer=message.get("content") or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                tool_calls=tool_calls,
                steps=step,
                stopped="answer",
            )

        for call in calls:
            tool_calls += 1
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            handler = handlers.get(name)
            if handler is None:
                content = f"error: unknown tool {name!r}"
            else:
                try:
                    content = str(handler(args))
                except Exception as exc:  # surface to the model, don't crash the run
                    content = f"error: {exc}"
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id"), "content": content}
            )

    return AgentResult(
        answer="",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        tool_calls=tool_calls,
        steps=max_steps,
        stopped="max_steps",
    )
