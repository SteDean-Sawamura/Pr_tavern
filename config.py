"""Application configuration."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.getenv("TAVERN_DATA_DIR") or BASE_DIR / "data").expanduser().resolve()
DB_PATH = Path(os.getenv("TAVERN_DB_PATH") or DATA_DIR / "tavern.db")
SCRIPTS_DIR = DATA_DIR / "scripts"
SAVES_DIR = DATA_DIR / "saves"
STATIC_DIR = BASE_DIR / "static"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
SAVES_DIR.mkdir(parents=True, exist_ok=True)

# Server config
HOST = os.getenv("TAVERN_HOST", "127.0.0.1")
PORT = int(os.getenv("TAVERN_PORT", "8000"))

# Pipeline mode: "workflow" (fixed multi-stage), "agentic" (dual agent loop), or "agentic_unified" (single unified agent)
PIPELINE_MODE = os.getenv("PIPELINE_MODE", "workflow")

# API authentication — set TAVERN_API_TOKEN to enable; leave empty to disable
API_TOKEN = os.getenv("TAVERN_API_TOKEN", "")

# 任务类型→模型路由（可通过 API 动态配置）
TASK_MODEL_ROUTES = {
    "planning": None,      # 使用默认模型（或 flash 快模型）
    "narrative": None,     # 使用默认模型（或强模型）
    "settlement": None,    # 使用默认模型（或 flash）
    "review": None,        # 使用默认模型（或 flash）
    "memory": None,        # 使用默认模型（或 flash）
    "image": None,         # 使用图像模型
}
