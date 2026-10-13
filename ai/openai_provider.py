"""OpenAI-compatible AI provider."""

import logging
import json
import re
from ai.base import AIProvider, ModelOutputUnavailableError, strip_think_tags, stream_strip_think, with_retry
from typing import AsyncIterator

logger = logging.getLogger("tavern.ai")

_CACHE_SENTINEL = "\n\n<|cache_break|>\n\n"

# 已知用 max_completion_tokens 而非 max_tokens 的模型名前缀
_MAX_COMPLETION_TOKENS_RE = re.compile(r'^(o\d|gpt-5|gpt-4\.\d-(mini|nano)?o)', re.IGNORECASE)


def _max_tokens_param(model: str) -> str:
    """Return the correct max-tokens parameter name for the model."""
    return "max_completion_tokens" if _MAX_COMPLETION_TOKENS_RE.match(model or "") else "max_tokens"


async def _create_with_token_fallback(client, **params):
    """Try create; if rejected for max_tokens unsupported, swap to max_completion_tokens and retry."""
    try:
        return await client.chat.completions.create(**params)
    except Exception as e:
        msg = str(e)
        if "max_tokens" in params and "max_tokens" in msg and "max_completion_tokens" in msg:
            params["max_completion_tokens"] = params.pop("max_tokens")
            logger.info("retrying with max_completion_tokens for model=%s", params.get("model"))
            return await client.chat.completions.create(**params)
        raise


class OpenAIProvider(AIProvider):
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gpt-4o")
        self.max_tokens = config.get("max_tokens", 8192)
        self.base_url = config.get("base_url")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            import httpx
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            kwargs["timeout"] = httpx.Timeout(300.0, connect=10.0)
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    @with_retry(max_retries=2, base_delay=1.0)
    async def generate(self, messages: list[dict], system: str = "", **kwargs) -> str:
        client = self._get_client()
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system.replace(_CACHE_SENTINEL, "\n\n---\n\n")})
        all_messages.extend(messages)

        raw = kwargs.pop("raw", False)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model
        token_param = _max_tokens_param(model)
        logger.info("generate → model=%s, msgs=%d, %s=%d", model, len(all_messages), token_param, max_tokens)

        response = await _create_with_token_fallback(
            client,
            model=model,
            messages=all_messages,
            **{token_param: max_tokens},
        )
        # B19: 空 choices 防御 — 某些代理在限流/失败时返回空 choices 而非抛错
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("AI 返回空响应（choices 为空）— 可能是限流或上游错误")
        msg = choices[0].message
        content = getattr(msg, "content", None) or ""
        # Some reasoning models put thinking in reasoning_content instead of <think> tags
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning and "<think>" not in content.lower():
            content = f"<think>{reasoning}</think>{content}"
        if raw:
            return content
        return strip_think_tags(content)

    async def generate_stream(self, messages: list[dict], system: str = "", raw: bool = False, **kwargs) -> AsyncIterator[str]:
        client = self._get_client()
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system.replace(_CACHE_SENTINEL, "\n\n---\n\n")})
        all_messages.extend(messages)

        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model
        token_param = _max_tokens_param(model)
        logger.info("generate_stream → model=%s, msgs=%d", model, len(all_messages))

        stream = await _create_with_token_fallback(
            client,
            model=model,
            messages=all_messages,
            stream=True,
            **{token_param: max_tokens},
        )

        async def _raw():
            _in_reasoning = False
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                # Support reasoning_content field (DeepSeek-R1 and similar reasoning models)
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    if not _in_reasoning:
                        yield "<think>"
                        _in_reasoning = True
                    yield reasoning
                else:
                    if _in_reasoning:
                        yield "</think>"
                        _in_reasoning = False
                if delta.content:
                    yield delta.content
            # Close unclosed reasoning block
            if _in_reasoning:
                yield "</think>"

        if raw:
            async for text in _raw():
                yield text
        else:
            async for text in stream_strip_think(_raw()):
                yield text

    async def test_connection(self) -> bool:
        try:
            client = self._get_client()
            token_param = _max_tokens_param(self.model)
            response = await _create_with_token_fallback(
                client,
                model=self.model,
                messages=[{"role": "user", "content": "Hi"}],
                **{token_param: 10},
            )
            return bool(response.choices)
        except Exception:
            return False

    @with_retry(max_retries=2, base_delay=1.0)
    async def generate_with_tools(self, messages: list[dict], system: str = "",
                                  tools: list[dict] | None = None, **kwargs) -> dict:
        client = self._get_client()
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system.replace(_CACHE_SENTINEL, "\n\n---\n\n")})
        all_messages.extend(messages)

        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model
        token_param = _max_tokens_param(model)

        params = {
            "model": model,
            "messages": all_messages,
            token_param: max_tokens,
        }
        if tools:
            params["tools"] = tools

        logger.info("generate_with_tools → model=%s, msgs=%d, tools=%d",
                     model, len(all_messages), len(tools) if tools else 0)

        response = await _create_with_token_fallback(client, **params)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("AI 返回空响应（choices 为空）")

        msg = choices[0].message
        content = getattr(msg, "content", None) or ""
        reasoning = getattr(msg, "reasoning_content", None)
        tool_calls_raw = getattr(msg, "tool_calls", None)

        tool_calls = None
        if tool_calls_raw:
            import json as _json
            tool_calls = []
            for tc in tool_calls_raw:
                fn = tc.function
                try:
                    args = _json.loads(fn.arguments) if fn.arguments else {}
                except _json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": fn.name,
                    "arguments": args,
                })

        if reasoning and "<think>" not in content.lower():
            content = f"<think>{reasoning}</think>{content}"

        # Extract usage if available
        usage_data = {}
        raw_usage = getattr(response, "usage", None)
        if raw_usage:
            usage_data["prompt_tokens"] = getattr(raw_usage, "prompt_tokens", 0) or 0
            usage_data["completion_tokens"] = getattr(raw_usage, "completion_tokens", 0) or 0

        return {
            "content": strip_think_tags(content),
            "reasoning_content": reasoning,
            "tool_calls": tool_calls,
            "usage": usage_data,
        }

    async def generate_with_tools_stream(self, messages: list[dict], system: str = "",
                                         tools: list[dict] | None = None, **kwargs) -> AsyncIterator[dict]:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system.replace(_CACHE_SENTINEL, "\n\n---\n\n")})
        all_messages.extend(messages)
        model = kwargs.get("model") or self.model
        params = {
            "model": model, "messages": all_messages,
            _max_tokens_param(model): kwargs.get("max_tokens", self.max_tokens),
            "stream": True, "stream_options": {"include_usage": True},
        }
        if tools:
            params["tools"] = tools
        try:
            stream = await _create_with_token_fallback(self._get_client(), **params)
        except Exception as exc:
            if "stream_options" not in str(exc).lower():
                raise
            yield {"type": "response", "response": await self.generate_with_tools(
                messages, system=system, tools=tools, **kwargs)}
            return

        allowed_names = {tool.get("function", {}).get("name") for tool in tools or []}
        content_parts = []
        reasoning_parts = []
        calls = {}
        usage_data = {}
        output_started = False
        diagnostics = {"chunk_count": 0, "choice_chunk_count": 0, "finish_reasons": [],
                       "requested_output_limit": kwargs.get("max_tokens", self.max_tokens),
                       "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None}
        async with stream:
            async for chunk in stream:
                diagnostics["chunk_count"] += 1
                usage = getattr(chunk, "usage", None)
                if usage:
                    usage_data = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                    }
                    diagnostics.update(
                        prompt_tokens=getattr(usage, "prompt_tokens", None),
                        completion_tokens=getattr(usage, "completion_tokens", None),
                        reasoning_tokens=getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
                    )
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                diagnostics["choice_chunk_count"] += 1
                finish_reason = getattr(choices[0], "finish_reason", None)
                if finish_reason is not None and finish_reason not in diagnostics["finish_reasons"]:
                    diagnostics["finish_reasons"].append(finish_reason)
                delta = choices[0].delta
                if not output_started and (getattr(delta, "content", None)
                                           or getattr(delta, "reasoning_content", None)
                                           or getattr(delta, "tool_calls", None)):
                    output_started = True
                    yield {"type": "model_output_started"}
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                if getattr(delta, "reasoning_content", None):
                    reasoning_parts.append(delta.reasoning_content)
                for call in getattr(delta, "tool_calls", None) or []:
                    entry = calls.setdefault(call.index, {"id": "", "name": "", "arguments": "", "prepared": False})
                    if getattr(call, "id", None):
                        entry["id"] += call.id
                    function = getattr(call, "function", None)
                    if function is not None:
                        entry["name"] += getattr(function, "name", None) or ""
                        arguments = getattr(function, "arguments", None) or ""
                        entry["arguments"] += arguments
                        if arguments and entry["name"] in allowed_names and not entry["prepared"]:
                            entry["prepared"] = True
                            yield {"type": "tool_preparing", "tool_name": entry["name"]}

        content = "".join(content_parts)
        diagnostics["raw_content_characters"] = len(content)
        reasoning = "".join(reasoning_parts) or None
        if reasoning and "<think>" not in content.lower():
            content = f"<think>{reasoning}</think>{content}"
        visible_content = strip_think_tags(content)
        diagnostics.update(visible_content_characters=len(visible_content),
                           reasoning_characters=len(reasoning or ""), tool_call_count=len(calls))
        if not visible_content.strip() and not calls:
            raise ModelOutputUnavailableError(diagnostics)
        tool_calls = []
        for entry in (calls[index] for index in sorted(calls)):
            try:
                arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append({"id": entry["id"], "name": entry["name"], "arguments": arguments})
        yield {"type": "response", "response": {
            "content": visible_content, "reasoning_content": reasoning,
            "tool_calls": tool_calls or None, "usage": usage_data,
        }}
