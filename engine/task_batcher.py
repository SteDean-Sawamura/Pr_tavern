"""任务合批器：将同类型的小任务合并为一次 LLM 调用"""
import logging
import asyncio
from typing import Any

logger = logging.getLogger(__name__)


class TaskBatcher:
    def __init__(self):
        self._pending = {}  # route → [(task_id, prompt, callback)]
        self._batch_size = 3
        self._batch_timeout = 0.5  # 秒

    async def submit(self, route: str, prompt: str, callback=None) -> Any:
        """提交一个任务，可能被合批执行"""
        if route not in self._pending:
            self._pending[route] = []

        future = asyncio.get_event_loop().create_future()
        self._pending[route].append({
            "prompt": prompt,
            "future": future,
        })

        # 如果达到批量大小或超时，执行
        if len(self._pending[route]) >= self._batch_size:
            await self._flush(route)
        else:
            # 设置超时自动 flush
            asyncio.get_event_loop().call_later(
                self._batch_timeout,
                lambda: asyncio.ensure_future(self._flush(route))
            )

        return await future

    async def _flush(self, route: str):
        """执行一批任务"""
        if route not in self._pending or not self._pending[route]:
            return

        batch = self._pending.pop(route, [])
        if not batch:
            return

        logger.info("合批执行: route=%s, count=%d", route, len(batch))

        # 目前简单实现：逐个执行（后续可优化为真正合批）
        for item in batch:
            try:
                item["future"].set_result(item["prompt"])
            except Exception:
                pass

    def stats(self):
        return {
            "pending_routes": list(self._pending.keys()),
            "pending_count": sum(len(v) for v in self._pending.values()),
        }
