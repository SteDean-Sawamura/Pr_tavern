from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RPGClient:
    session: Any = None
    builder: Any = None
    llm_state: dict = field(default_factory=lambda: {"client": None, "model": "", "profile": None})
    active_requests: int = 0


current_client: ContextVar[RPGClient | None] = ContextVar("rpg_client", default=None)
request_llm_state: ContextVar[dict | None] = ContextVar("rpg_request_llm_state", default=None)
