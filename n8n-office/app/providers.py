"""app/providers.py — LLM provider abstraction for the office assistant.

One normalized async interface over three back ends so `app.agent` never sees
provider-specific request/response shapes:

    provider = get_provider()          # None when CHAT_PROVIDER == 'none'
    out = await provider.complete(system, messages, tools)
    # out = {'stop': 'text'|'tool_calls', 'text': str,
    #        'tool_calls': [{'id','name','input'}],
    #        'raw_assistant_msg': <provider-native assistant message>}
    followups = provider.make_tool_result_messages(out['raw_assistant_msg'],
                                                    results)
    messages.extend(followups)         # then loop provider.complete again

Providers:
  * AnthropicProvider  — anthropic.AsyncAnthropic; native tool-use blocks.
  * OpenAIProvider     — openai.AsyncOpenAI; function-calling. Used for BOTH
    'openai' (api.openai.com) and 'openai_compat' (a local, NON-PHI-safe
    proxy at SETTINGS.GPT_ENDPOINT). The provider layer is identical; the PHI
    gate lives in SETTINGS.PHI_SAFE_LLM and is enforced by app.agent, not here.

Honesty / safety notes:
  * Tools are passed in a NEUTRAL schema — a list of
    {'name','description','input_schema'(JSON Schema)} — and each provider
    converts to its own wire format. Callers never hand-roll provider JSON.
  * This layer performs NO EMR access and makes NO write decisions. It only
    ferries text + tool-call intents between the model and the agent loop.
  * `raw_assistant_msg` is stored verbatim and echoed back on the next round so
    each SDK sees a well-formed transcript (Anthropic needs the exact
    content blocks; OpenAI needs the tool_calls it emitted).
  * On any provider we cap output at max_tokens=2048 (short back-office turns).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import SETTINGS

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """Normalized wrapper around anthropic.AsyncAnthropic (Messages API).

    Tool calls surface as `tool_use` content blocks; tool results are returned
    to the model as a `user` message carrying `tool_result` blocks keyed by the
    original tool_use id.
    """

    def __init__(self, api_key: str, model: str) -> None:
        # Imported lazily so a missing SDK only breaks the provider that needs
        # it, never module import.
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    @staticmethod
    def _to_native_tools(tools: list[dict]) -> list[dict]:
        """Neutral schema -> Anthropic tool spec (input_schema is native)."""
        native = []
        for t in tools or []:
            native.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                }
            )
        return native

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = self._to_native_tools(tools)

        resp = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input or {},
                    }
                )

        stop = "tool_calls" if tool_calls else "text"
        # Preserve the assistant turn verbatim for the next round. The SDK
        # accepts the serialized content blocks as the message content.
        raw_assistant_msg = {
            "role": "assistant",
            "content": [
                b.model_dump() if hasattr(b, "model_dump") else b
                for b in resp.content
            ],
        }
        return {
            "stop": stop,
            "text": "".join(text_parts).strip(),
            "tool_calls": tool_calls,
            "raw_assistant_msg": raw_assistant_msg,
        }

    def make_tool_result_messages(
        self,
        raw_assistant_msg: Any,
        results: list[dict],
    ) -> list[dict]:
        """Append the assistant turn + a user turn of tool_result blocks.

        `results` = [{'id','name','content'}]; content is a plain string
        (JSON-encoded tool output). Anthropic keys results by tool_use id.
        """
        tool_result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r["id"],
                "content": r["content"],
            }
            for r in results
        ]
        return [
            raw_assistant_msg,
            {"role": "user", "content": tool_result_blocks},
        ]


# ---------------------------------------------------------------------------
# OpenAI (and OpenAI-compatible local proxy)
# ---------------------------------------------------------------------------


class OpenAIProvider:
    """Normalized wrapper around openai.AsyncOpenAI (Chat Completions).

    Used for both the real OpenAI API and an OpenAI-compatible local proxy
    (base_url = SETTINGS.GPT_ENDPOINT). Tool calls surface as `tool_calls` on
    the assistant message; results go back as `role='tool'` messages keyed by
    tool_call_id.

    NOTE: the openai_compat proxy is NOT PHI-safe. That policy is enforced by
    app.agent (it does not offer EMR tools when SETTINGS.PHI_SAFE_LLM is
    False); this transport layer is provider-shape-only and unaware of PHI.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
    ) -> None:
        from openai import AsyncOpenAI

        if base_url:
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    @staticmethod
    def _to_native_tools(tools: list[dict]) -> list[dict]:
        """Neutral schema -> OpenAI function-tool spec."""
        native = []
        for t in tools or []:
            native.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get(
                            "input_schema",
                            {"type": "object", "properties": {}},
                        ),
                    },
                }
            )
        return native

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> dict[str, Any]:
        # OpenAI wants the system prompt as the first message, not a kwarg.
        wire_messages = [{"role": "system", "content": system}, *messages]

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "messages": wire_messages,
        }
        if tools:
            kwargs["tools"] = self._to_native_tools(tools)

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[dict] = []
        for tc in msg.tool_calls or []:
            import json

            raw_args = tc.function.arguments or "{}"
            try:
                parsed = json.loads(raw_args)
            except (ValueError, TypeError):
                logger.warning(
                    "OpenAI tool_call args not valid JSON: %r", raw_args
                )
                parsed = {}
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "input": parsed}
            )

        stop = "tool_calls" if tool_calls else "text"
        # Serialize the assistant message so the next round can replay it. We
        # rebuild a plain dict (never mutate the SDK object) that the API
        # accepts back verbatim, including the tool_calls it emitted.
        assistant_dict: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in msg.tool_calls
            ]
        return {
            "stop": stop,
            "text": (msg.content or "").strip(),
            "tool_calls": tool_calls,
            "raw_assistant_msg": assistant_dict,
        }

    def make_tool_result_messages(
        self,
        raw_assistant_msg: Any,
        results: list[dict],
    ) -> list[dict]:
        """Append the assistant turn + one `tool` message per result.

        OpenAI keys each tool result by tool_call_id and requires the assistant
        message that requested them to precede the tool messages.
        """
        out: list[dict] = [raw_assistant_msg]
        for r in results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": r["id"],
                    "content": r["content"],
                }
            )
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_provider():
    """Return a provider instance per SETTINGS.CHAT_PROVIDER, or None.

    None when CHAT_PROVIDER == 'none' (no API key configured). The agent turns
    that into a plain "no LLM configured" reply — it never crashes.
    """
    provider = SETTINGS.CHAT_PROVIDER

    if provider == "anthropic":
        if not SETTINGS.ANTHROPIC_API_KEY:
            logger.warning("CHAT_PROVIDER=anthropic but no ANTHROPIC_API_KEY")
            return None
        return AnthropicProvider(
            api_key=SETTINGS.ANTHROPIC_API_KEY,
            model=SETTINGS.CHAT_MODEL,
        )

    if provider == "openai":
        if not SETTINGS.OPENAI_API_KEY:
            logger.warning("CHAT_PROVIDER=openai but no OPENAI_API_KEY")
            return None
        return OpenAIProvider(
            api_key=SETTINGS.OPENAI_API_KEY,
            model=SETTINGS.CHAT_MODEL,
        )

    if provider == "openai_compat":
        # Local OpenAI-compatible proxy. NOT PHI-safe (see PHI_SAFE_LLM).
        if not (SETTINGS.GPT_ENDPOINT and SETTINGS.GPT_API_KEY):
            logger.warning(
                "CHAT_PROVIDER=openai_compat but GPT_ENDPOINT/GPT_API_KEY "
                "missing"
            )
            return None
        return OpenAIProvider(
            api_key=SETTINGS.GPT_API_KEY,
            model=SETTINGS.CHAT_MODEL,
            base_url=SETTINGS.GPT_ENDPOINT,
        )

    # 'none' or anything unrecognized -> no LLM.
    return None
