"""精细用量追踪：按模型/任务维度统计 token 和成本"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 模型价格（每百万 token）
DEFAULT_PRICING = {
    "deepseek-chat": {"input": 0.27, "output": 1.10, "cache_hit": 0.07},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "cache_hit": 0.14},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_hit": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_hit": 0.075},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00, "cache_hit": 0.30},
}


class UsageTracker:
    def __init__(self):
        self.records = []  # 每次 LLM 调用的记录
        self.by_model = {}  # model → {requests, input_tokens, output_tokens, cache_tokens, cost}
        self.by_task = {}   # task_type → same
        self.total = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                      "cache_tokens": 0, "cost": 0.0}
        self.pricing = dict(DEFAULT_PRICING)
        self.currency = "USD"

    def record(self, model: str, task_type: str, input_tokens: int, output_tokens: int,
               cache_tokens: int = 0, latency_ms: int = 0):
        """记录一次 LLM 调用"""
        price = self.pricing.get(model, {"input": 0, "output": 0, "cache_hit": 0})
        cost = (input_tokens * price["input"] + output_tokens * price["output"] +
                cache_tokens * price.get("cache_hit", 0)) / 1_000_000

        record = {
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "task": task_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_tokens": cache_tokens,
            "cost": round(cost, 6),
            "latency_ms": latency_ms,
        }
        self.records.append(record)
        if len(self.records) > 500:
            self.records.pop(0)

        # 按模型聚合
        m = self.by_model.setdefault(model, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0, "cost": 0.0})
        m["requests"] += 1
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens
        m["cache_tokens"] += cache_tokens
        m["cost"] += cost

        # 按任务聚合
        t = self.by_task.setdefault(task_type, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0, "cost": 0.0})
        t["requests"] += 1
        t["input_tokens"] += input_tokens
        t["output_tokens"] += output_tokens
        t["cache_tokens"] += cache_tokens
        t["cost"] += cost

        # 总计
        self.total["requests"] += 1
        self.total["input_tokens"] += input_tokens
        self.total["output_tokens"] += output_tokens
        self.total["cache_tokens"] += cache_tokens
        self.total["cost"] += cost

    def get_stats(self) -> dict:
        cache_rate = 0
        total_input = self.total["input_tokens"] + self.total["cache_tokens"]
        if total_input > 0:
            cache_rate = round(self.total["cache_tokens"] / total_input * 100, 1)

        return {
            "total": {**self.total, "cost": round(self.total["cost"], 4)},
            "cache_hit_rate": cache_rate,
            "by_model": {k: {**v, "cost": round(v["cost"], 4)} for k, v in self.by_model.items()},
            "by_task": {k: {**v, "cost": round(v["cost"], 4)} for k, v in self.by_task.items()},
            "recent_records": self.records[-10:],
            "currency": self.currency,
        }

    def set_pricing(self, model: str, input_price: float, output_price: float, cache_price: float = 0):
        self.pricing[model] = {"input": input_price, "output": output_price, "cache_hit": cache_price}

    def snapshot(self):
        return {"by_model": self.by_model, "by_task": self.by_task, "total": self.total}
