"""The only file that knows the agent runs on Claude.

The rest of the agent calls call_model() and reads stop_reason and content
from what it returns, so swapping providers means rewriting this file only.
"""

import anthropic

from agent.settings import EFFORT, MAX_TOKENS, MODEL

# Server-side refusal fallback: if the model's safety classifier declines,
# the API re-runs the request on a fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client():
    """Real client. Reads ANTHROPIC_API_KEY from the environment (loaded from .env)."""
    return anthropic.Anthropic()


def call_model(client, system, messages, tools):
    return client.beta.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        tools=tools,
        # Forcing a tool ("any"/"tool") is a 400 on Opus 5.5; the prompt steers instead.
        tool_choice={"type": "auto"},
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT},
        betas=[FALLBACK_BETA],
        # "default" lets Anthropic pick the fallback model per refusal category.
        fallbacks="default",
    )
