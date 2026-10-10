"""Ollama (local model) AI provider."""

import httpx
from ai.base import AIProvider, strip_think_tags, stream_strip_think, with_retry
from typing import AsyncIterator
import json


class OllamaProvider(AIProvider):
    def __init__(self, config: dict):
        self.model = config.get("model", "llama3")
        self.max_tokens = config.get("max_tokens", 8192)
        self.base_url = config.get("base_url", "http://localhost:11434")

    @with_retry(max_retries=2, base_delay=1.0)
    async def generate(self, messages: list[dict], system: str = "", **kwargs) -> str:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system.replace("\n\n<|cache_break|>\n\n", "\n\n---\n\n")})
        all_messages.extend(messages)

        raw = kwargs.pop("raw", False)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": all_messages,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("message", {}).get("content", "")
            if raw:
                return content
            return strip_think_tags(content)

    async def generate_stream(self, messages: list[dict], system: str = "", raw: bool = False, **kwargs) -> AsyncIterator[str]:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system.replace("\n\n<|cache_break|>\n\n", "\n\n---\n\n")})
        all_messages.extend(messages)

        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        model = kwargs.get("model") or self.model

        async def _raw():
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": all_messages,
                        "stream": True,
                        "options": {"num_predict": max_tokens},
                    },
                ) as response:
                    async for line in response.aiter_lines():
                        if line.strip():
                            try:
                                data = json.loads(line)
                                content = data.get("message", {}).get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue

        if raw:
            async for text in _raw():
                yield text
        else:
            async for text in stream_strip_think(_raw()):
                yield text

    async def test_connection(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False
