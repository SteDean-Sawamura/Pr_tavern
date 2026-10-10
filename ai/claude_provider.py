"""Claude (Anthropic) AI provider."""

from ai.base import AIProvider, strip_think_tags, stream_strip_think, with_retry
from typing import AsyncIterator

_CACHE_SENTINEL = "\n\n<|cache_break|>\n\n"


class ClaudeProvider(AIProvider):
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = config.get("max_tokens", 8192)
        self.base_url = config.get("base_url")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            import httpx
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            kwargs["timeout"] = httpx.Timeout(300.0, connect=10.0)
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    @staticmethod
    def _prepare_system(system: str):
        """Split system prompt on cache sentinel into cached + uncached blocks."""
        if not system or _CACHE_SENTINEL not in system:
            return system
        stable, dynamic = system.split(_CACHE_SENTINEL, 1)
        blocks = [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        ]
        if dynamic.strip():
            blocks.append({"type": "text", "text": dynamic})
        return blocks

    @with_retry(max_retries=2, base_delay=1.0)
    async def generate(self, messages: list[dict], system: str = "", **kwargs) -> str:
        client = self._get_client()
        raw = kwargs.pop("raw", False)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=self._prepare_system(system),
            messages=messages,
        )
        # B20: 空 content 防御
        content = getattr(response, "content", None) or []
        if not content:
            raise RuntimeError("Claude 返回空响应（content 为空）— 可能是限流或安全策略拦截")
        first = content[0]
        text = getattr(first, "text", None) or ""
        if raw:
            return text
        return strip_think_tags(text)

    async def generate_stream(self, messages: list[dict], system: str = "", raw: bool = False, **kwargs) -> AsyncIterator[str]:
        client = self._get_client()
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model

        async def _raw():
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=self._prepare_system(system),
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        if raw:
            async for text in _raw():
                yield text
        else:
            async for text in stream_strip_think(_raw()):
                yield text

    async def test_connection(self) -> bool:
        try:
            client = self._get_client()
            response = await client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return bool(response.content)
        except Exception:
            return False
