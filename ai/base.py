"""Abstract base class for AI providers."""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from functools import wraps
from typing import AsyncIterator

logger = logging.getLogger("tavern.ai")


def _is_retryable(exc: Exception) -> bool:
    """B21: 只对瞬时错误重试 — 网络/超时/限流/5xx；不对鉴权/参数错误等永久错误重试。"""
    name = type(exc).__name__
    msg = str(exc).lower()
    # 永久错误：API key 无效、参数错误、内容策略拦截
    if name in ("AuthenticationError", "PermissionDeniedError", "BadRequestError",
                "NotFoundError", "InvalidRequestError"):
        return False
    if "401" in msg or "403" in msg or "invalid api key" in msg or "authentication" in msg:
        return False
    # 瞬时：超时、连接、速率限制、5xx、内部错误
    if name in ("TimeoutError", "APITimeoutError", "APIConnectionError",
                "RateLimitError", "InternalServerError", "APIError",
                "ConnectionError", "ServiceUnavailableError"):
        return True
    if any(k in msg for k in ("timeout", "timed out", "rate limit", "429",
                              "500", "502", "503", "504", "connection",
                              "temporarily unavailable")):
        return True
    # 默认：仅在显式标记为可重试时才重试
    return False


def with_retry(max_retries: int = 2, base_delay: float = 1.0, retryable_exceptions: tuple = (Exception,)):
    """Decorator for async methods: retry with exponential backoff on transient failures."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    # B21: 永久性错误不重试
                    if not _is_retryable(e):
                        logger.error("AI调用失败(非瞬时错误，不重试): %s", e)
                        raise
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning("AI调用失败(第%d次), %.1f秒后重试: %s", attempt + 1, delay, e)
                        await asyncio.sleep(delay)
                    else:
                        logger.error("AI调用失败(已耗尽%d次重试): %s", max_retries + 1, e)
            raise last_exc
        return wrapper
    return decorator

_THINK_RE = re.compile(r'<think(?:ing)?>\s*[\s\S]*?</think(?:ing)?>\s*', re.IGNORECASE)
_THINK_EXTRACT_RE = re.compile(r'<think(?:ing)?>\s*([\s\S]*?)</think(?:ing)?>\s*', re.IGNORECASE)
# Match unclosed <think>/<thinking> tag at start — everything after it is thinking (truncated output)
_STRAY_OPEN_THINK_RE = re.compile(r'^<think(?:ing)?>\s*[\s\S]*$', re.IGNORECASE)
# Match stray closing </think> without an opening tag — everything before it is thinking
_STRAY_CLOSE_THINK_RE = re.compile(r'^([\s\S]*?)</think(?:ing)?>\s*', re.IGNORECASE)
# Match untagged thinking at the start: English reasoning paragraphs before Chinese narrative
_UNTAGGED_THINK_RE = re.compile(
    r'^(?:(?:Let me|I need to|I should|I\'ll|I will|I want to|'
    r'Okay|OK|Alright|Now|First|So|Hmm|Wait|Think|'
    r'The player|The user|Looking at|Given that|'
    r'Let\'s|Here\'s|This is|For )'
    r'[\s\S]*?\n\n)+',
    re.IGNORECASE
)


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks and untagged thinking from model output."""
    # First strip paired <think>...</think> blocks
    text = _THINK_RE.sub('', text).strip()
    # Handle stray </think> without opening tag — everything before it is thinking
    text = _STRAY_CLOSE_THINK_RE.sub('', text).strip()
    # Handle stray <think>/<thinking> without closing tag — output was truncated
    # Try to salvage content after thinking (e.g. JSON that follows)
    if _STRAY_OPEN_THINK_RE.match(text):
        # Look for JSON or code fence after the thinking — salvage it
        # Find ```json or first { after the opening tag
        tag_end = text.index('>') + 1
        rest = text[tag_end:]
        # Look for code fence or JSON start
        fence_pos = rest.find('```')
        brace_pos = rest.find('{')
        if fence_pos != -1:
            text = rest[fence_pos:].strip()
        elif brace_pos != -1:
            text = rest[brace_pos:].strip()
        else:
            # Pure thinking with no useful content — return empty
            text = ""
    # B22: 仅在匹配区不含 CJK（纯英文推理）时才剥离未标记的思考
    m = _UNTAGGED_THINK_RE.match(text)
    if m and not _CJK_RE.search(m.group()):
        text = text[m.end():].strip()
    return text


def extract_think_tags(text: str) -> tuple[str, str]:
    """Extract think content and return (think_text, clean_text)."""
    think_parts = _THINK_EXTRACT_RE.findall(text)
    clean_text = _THINK_RE.sub('', text).strip()
    # Handle stray </think> without opening tag
    stray_match = _STRAY_CLOSE_THINK_RE.match(clean_text)
    if stray_match:
        think_parts.append(stray_match.group(1).strip())
        clean_text = _STRAY_CLOSE_THINK_RE.sub('', clean_text).strip()
    think_text = "\n".join(think_parts).strip()
    return think_text, clean_text


def _find_open_think(text: str, start: int = 0) -> tuple[int, int]:
    """Find <think> or <thinking> open tag. Returns (pos, tag_len) or (-1, 0)."""
    lower = text.lower()
    pos_short = lower.find("<think>", start)
    pos_long = lower.find("<thinking>", start)
    if pos_short == -1 and pos_long == -1:
        return -1, 0
    if pos_long != -1 and (pos_short == -1 or pos_long < pos_short):
        return pos_long, len("<thinking>")
    return pos_short, len("<think>")


def _find_close_think(text: str, start: int = 0) -> tuple[int, int]:
    """Find </think> or </thinking> close tag. Returns (pos, tag_len) or (-1, 0)."""
    lower = text.lower()
    pos_short = lower.find("</think>", start)
    pos_long = lower.find("</thinking>", start)
    if pos_short == -1 and pos_long == -1:
        return -1, 0
    if pos_long != -1 and (pos_short == -1 or pos_long < pos_short):
        return pos_long, len("</thinking>")
    return pos_short, len("</think>")


async def stream_strip_think(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap a streaming iterator to suppress <think>/<thinking> blocks.

    Buffers content while inside a think block, drops it on close tag,
    and flushes the buffer if the stream ends mid-block.
    """
    inside = False
    buf = ""
    for_flush = ""

    async for chunk in chunks:
        i = 0
        while i < len(chunk):
            if inside:
                end, tag_len = _find_close_think(chunk, i)
                if end != -1:
                    inside = False
                    i = end + tag_len
                    # skip any trailing whitespace/newline after close tag
                    while i < len(chunk) and chunk[i] in (' ', '\n', '\r'):
                        i += 1
                else:
                    break  # still inside, consume rest of chunk
            else:
                start, tag_len = _find_open_think(chunk, i)
                close, close_len = _find_close_think(chunk, i)
                if start != -1 and (close == -1 or start <= close):
                    # emit everything before the tag
                    part = chunk[i:start]
                    if part:
                        yield part
                    inside = True
                    i = start + tag_len
                elif close != -1 and (start == -1 or close < start):
                    # Stray </think> without opening — skip the tag and anything before it
                    i = close + close_len
                    while i < len(chunk) and chunk[i] in (' ', '\n', '\r'):
                        i += 1
                else:
                    # No tag — but chunk might end with partial "<thin..."
                    # Keep last 10 chars in buffer in case tag spans chunks
                    safe = chunk[i:]
                    if len(safe) > 10:
                        yield safe[:-10]
                        for_flush = safe[-10:]
                    else:
                        for_flush += safe
                        if len(for_flush) > 10:
                            yield for_flush[:-10]
                            for_flush = for_flush[-10:]
                    break

    # Flush remaining buffer
    if for_flush:
        yield for_flush


_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
# 英文思考语言特征：常见于模型 reasoning 阶段
_ENGLISH_THINKING_RE = re.compile(
    r'(?:Let me|I need to|I should|I\'ll|I will|I want to|'
    r'The player|The user|Looking at|Given that|Key dice|'
    r'Let\'s|Here\'s |This is |For this|Now |First |So |Hmm|'
    r'Okay|OK |Alright|chose to|chooses to|response|narrative)',
    re.IGNORECASE
)


async def stream_split_think(chunks: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """Like stream_strip_think but yields (type, text) tuples.

    type is "think" for content inside <think>/<thinking> and "text" for normal content.
    Used by game streaming to display thinking in a collapsible area.
    Also detects untagged English thinking at stream start (before any CJK text).
    """
    inside = False
    for_flush = ""
    # Phase: "detect" = buffering stream start to detect untagged thinking
    #        "normal" = standard tag-based processing
    phase = "detect"
    detect_buf = ""

    async for chunk in chunks:
        # In detect phase, buffer until we see CJK or <think>/<thinking> tag
        if phase == "detect":
            detect_buf += chunk
            lower_buf = detect_buf.lower()
            # Check for <think> or <thinking> tag — switch to normal processing from start
            if "<think>" in lower_buf or "<thinking>" in lower_buf:
                phase = "normal"
                chunk = detect_buf
                detect_buf = ""
                # Fall through to normal processing below
            elif "</think>" in lower_buf or "</thinking>" in lower_buf:
                # Stray closing tag without opening — everything before it is thinking
                close_pos, close_len = _find_close_think(detect_buf)
                if close_pos != -1:
                    before = detect_buf[:close_pos]
                    after_pos = close_pos + close_len
                    rest = detect_buf[after_pos:].lstrip()
                    if before.strip():
                        yield ("think", before)
                    phase = "normal"
                    chunk = rest
                    detect_buf = ""
                    if not chunk:
                        continue
                    # Fall through to normal processing below
                else:
                    continue
            elif _CJK_RE.search(detect_buf):
                # Found CJK，但要小心：模型可能先用中文回显玩家选择，
                # 再用英文 reasoning，最后才输出 </think> 与正文。
                # 1) 缓冲太短先等待，避免误判
                # 2) 已观察到英文思考模式 → 继续等 </think>
                if len(detect_buf) < 400:
                    continue
                if _ENGLISH_THINKING_RE.search(detect_buf) and len(detect_buf) < 4000:
                    continue
                # 否则按 CJK 切分（纯中文叙事开头的常见情况）
                m = _CJK_RE.search(detect_buf)
                before = detect_buf[:m.start()]
                rest = detect_buf[m.start():]
                if before.strip():
                    yield ("think", before)
                phase = "normal"
                chunk = rest
                detect_buf = ""
                # Fall through to normal processing below
            elif len(detect_buf) > 4000:
                # Very long without CJK — probably all thinking, flush as think
                yield ("think", detect_buf)
                detect_buf = ""
                # Stay in detect phase for more
                continue
            else:
                continue  # Keep buffering

        i = 0
        while i < len(chunk):
            if inside:
                end, tag_len = _find_close_think(chunk, i)
                if end != -1:
                    think_part = chunk[i:end]
                    if think_part:
                        yield ("think", think_part)
                    inside = False
                    i = end + tag_len
                    while i < len(chunk) and chunk[i] in (' ', '\n', '\r'):
                        i += 1
                else:
                    yield ("think", chunk[i:])
                    break
            else:
                start, tag_len = _find_open_think(chunk, i)
                close, close_len = _find_close_think(chunk, i)
                if start != -1 and (close == -1 or start <= close):
                    part = chunk[i:start]
                    if part:
                        yield ("text", part)
                    inside = True
                    i = start + tag_len
                elif close != -1 and (start == -1 or close < start):
                    # Stray </think> without opening — text before it is thinking
                    before = chunk[i:close]
                    if before.strip():
                        yield ("think", before)
                    i = close + close_len
                    while i < len(chunk) and chunk[i] in (' ', '\n', '\r'):
                        i += 1
                else:
                    safe = chunk[i:]
                    if len(safe) > 10:
                        yield ("text", safe[:-10])
                        for_flush = safe[-10:]
                    else:
                        for_flush += safe
                        if len(for_flush) > 10:
                            yield ("text", for_flush[:-10])
                            for_flush = for_flush[-10:]
                    break

    # Flush remaining
    if detect_buf:
        # Stream ended while still detecting — check for stray close tag first
        close_pos, close_len = _find_close_think(detect_buf)
        if close_pos != -1:
            before = detect_buf[:close_pos]
            rest = detect_buf[close_pos + close_len:].strip()
            if before.strip():
                yield ("think", before)
            if rest:
                yield ("text", rest)
        elif _CJK_RE.search(detect_buf):
            m = _CJK_RE.search(detect_buf)
            before = detect_buf[:m.start()]
            rest = detect_buf[m.start():]
            if before.strip():
                yield ("think", before)
            if rest:
                yield ("text", rest)
        else:
            yield ("think", detect_buf)
    if for_flush:
        yield ("text", for_flush)


class AIProvider(ABC):
    @abstractmethod
    async def generate(self, messages: list[dict], system: str = "", **kwargs) -> str:
        """Send messages and return complete text response."""

    @abstractmethod
    async def generate_stream(self, messages: list[dict], system: str = "", raw: bool = False, **kwargs) -> AsyncIterator[str]:
        """Send messages and yield response chunks for streaming.

        If raw=True, emit chunks without stripping <think> tags.
        Used by game session to split think/text for UI display.
        """

    async def generate_with_tools(self, messages: list[dict], system: str = "",
                                  tools: list[dict] | None = None, **kwargs) -> dict:
        """Send messages with tool definitions and return structured response.

        Returns {"content": str, "reasoning_content": str|None, "tool_calls": list|None}
        Default implementation falls back to regular generate (no native tool support).
        """
        text = await self.generate(messages, system=system, **kwargs)
        return {"content": text, "reasoning_content": None, "tool_calls": None}

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test if the provider is configured and reachable."""
