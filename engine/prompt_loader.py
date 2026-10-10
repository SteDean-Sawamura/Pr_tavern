"""Prompt template loader — reads YAML files, renders with variables."""
from __future__ import annotations

import os
import yaml
import logging
from typing import Dict, Any, Tuple, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


class PromptLoader:
    _instance: Optional["PromptLoader"] = None

    def __init__(self, prompts_dir: str = _PROMPTS_DIR):
        self._dir = prompts_dir
        self._cache: Dict[str, dict] = {}

    @classmethod
    def get(cls) -> "PromptLoader":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self, name: str) -> dict:
        if name in self._cache:
            return self._cache[name]

        # name can be "stage_route" or "world_state/resource"
        path = os.path.join(self._dir, f"{name}.yml")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Prompt template not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            tpl = yaml.safe_load(f)

        self._cache[name] = tpl
        return tpl

    def render(self, name: str, **kwargs) -> Tuple[str, str]:
        """Load template and render with variables. Returns (system, user)."""
        tpl = self._load(name)
        system = tpl.get("system", "")
        user = tpl.get("user", "")

        # Safe format: only replace known keys, leave unknown {xxx} as-is
        for key, value in kwargs.items():
            system = system.replace(f"{{{key}}}", str(value) if value is not None else "")
            user = user.replace(f"{{{key}}}", str(value) if value is not None else "")

        return system, user

    def render_system(self, name: str, **kwargs) -> str:
        system, _ = self.render(name, **kwargs)
        return system

    def render_user(self, name: str, **kwargs) -> str:
        _, user = self.render(name, **kwargs)
        return user

    def try_render(self, name: str, **kwargs) -> Optional[Tuple[str, str]]:
        """Try to load and render; return None if template not found."""
        try:
            return self.render(name, **kwargs)
        except FileNotFoundError:
            return None

    def get_meta(self, name: str) -> dict:
        """Get template metadata (stage, max_tokens, etc.)"""
        tpl = self._load(name)
        return {k: v for k, v in tpl.items() if k not in ("system", "user")}

    def reload(self, name: str = None):
        """Clear cache for hot-reload during development."""
        if name:
            self._cache.pop(name, None)
        else:
            self._cache.clear()

    def list_templates(self) -> list:
        """List all available template names."""
        templates = []
        for root, _, files in os.walk(self._dir):
            for f in files:
                if f.endswith(".yml"):
                    rel = os.path.relpath(os.path.join(root, f), self._dir)
                    templates.append(rel.replace("\\", "/").replace(".yml", ""))
        return sorted(templates)
