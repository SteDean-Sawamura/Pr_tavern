"""Pydantic models for database entities."""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ScriptMeta(BaseModel):
    id: str
    name: str
    version: str = "1.0"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SaveMeta(BaseModel):
    id: str
    script_id: str
    name: Optional[str] = None
    active_node_id: Optional[str] = None
    total_nodes: int = 0
    play_time_seconds: int = 0
    summary: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Material(BaseModel):
    id: Optional[int] = None
    title: str
    content: str
    summary: Optional[str] = None
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    search_query: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class SearchResult(BaseModel):
    title: str
    content: str
    url: Optional[str] = None
    source_type: str
    relevance_score: float = 0.0


class DiceConfig(BaseModel):
    count: int = 1
    faces: int = 100
    modifier: int = 0


class DiceRange(BaseModel):
    min: int
    max: int
    label: str
    state_changes: list[dict] = Field(default_factory=list)


class StateChange(BaseModel):
    target: str
    op: str = "add"
    value: float = 0
    reason: str = ""


class AIConfig(BaseModel):
    active_provider: str = "openai"
    providers: dict = Field(default_factory=lambda: {
        "claude": {
            "api_key": "",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8192,
            "base_url": None
        },
        "openai": {
            "api_key": "",
            "model": "gpt-4o",
            "max_tokens": 8192,
            "base_url": None
        },
        "ollama": {
            "model": "llama3",
            "max_tokens": 8192,
            "base_url": "http://localhost:11434"
        }
    })
    streaming: bool = True
