"""AI configuration API routes — multi-profile support."""

import copy
import json
import logging
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from db.database import get_db

router = APIRouter()
logger = logging.getLogger("tavern.config")


# --- Pydantic models ---

class ProfileCreateRequest(BaseModel):
    name: str
    provider_type: str  # 'openai', 'openai_compatible', 'claude', 'ollama'
    api_key: str = ""
    model: str = "gpt-4o"
    max_tokens: int = 8192
    review_max_tokens: int = Field(default=500, strict=True, ge=1, le=32768)
    review_retry_max_tokens: int = Field(default=1500, strict=True, ge=1, le=32768)
    base_url: Optional[str] = None
    stage_models: Optional[dict] = None


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    review_max_tokens: Optional[int] = Field(default=None, strict=True, ge=1, le=32768)
    review_retry_max_tokens: Optional[int] = Field(default=None, strict=True, ge=1, le=32768)
    base_url: Optional[str] = None
    stage_models: Optional[dict] = None


class ConfigUpdateRequest(BaseModel):
    active_provider: Optional[str] = None
    providers: Optional[dict] = None
    streaming: Optional[bool] = None


# --- Profile CRUD ---

@router.get("/profiles")
async def list_profiles():
    """List all saved AI profiles."""
    async with get_db() as db:
        # P4: 单次查询拿到 api_key，避免 N+1
        cursor = await db.execute(
            "SELECT id, name, provider_type, model, max_tokens, review_max_tokens, review_retry_max_tokens, base_url, is_active, created_at, api_key, stage_models FROM ai_profiles ORDER BY is_active DESC, id"
        )
        rows = await cursor.fetchall()
        profiles = []
        for row in rows:
            p = dict(row)
            key = p.pop("api_key", "") or ""
            p["has_api_key"] = bool(key)
            if key and len(key) > 8:
                p["api_key_preview"] = key[:4] + "****" + key[-4:]
            else:
                p["api_key_preview"] = "****" if key else ""
            sm = p.get("stage_models")
            if isinstance(sm, str) and sm:
                try:
                    p["stage_models"] = json.loads(sm)
                except Exception:
                    p["stage_models"] = {}
            else:
                p["stage_models"] = {}
            profiles.append(p)
        return profiles


@router.post("/profiles")
async def create_profile(req: ProfileCreateRequest):
    """Create a new AI profile."""
    logger.info("创建AI配置 — name=%s, provider=%s, model=%s", req.name, req.provider_type, req.model)
    stage_models_json = json.dumps(req.stage_models) if req.stage_models else None
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO ai_profiles (name, provider_type, api_key, model, max_tokens, base_url, stage_models,
                                       review_max_tokens, review_retry_max_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.name, req.provider_type, req.api_key, req.model, req.max_tokens, req.base_url, stage_models_json,
             req.review_max_tokens, req.review_retry_max_tokens),
        )
        await db.commit()
        profile_id = cursor.lastrowid

        # If this is the first profile, make it active
        count_cursor = await db.execute("SELECT COUNT(*) as cnt FROM ai_profiles")
        count_row = await count_cursor.fetchone()
        if count_row["cnt"] == 1:
            await db.execute("UPDATE ai_profiles SET is_active = 1 WHERE id = ?", (profile_id,))
            await db.commit()

        return {"status": "ok", "id": profile_id}


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: int, req: ProfileUpdateRequest):
    """Update an existing profile."""
    logger.info("更新AI配置 — profile_id=%d", profile_id)
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM ai_profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Profile not found")

        updates = []
        params = []
        for field in ("name", "provider_type", "model", "max_tokens", "base_url", "review_max_tokens", "review_retry_max_tokens"):
            val = getattr(req, field, None)
            if val is not None:
                updates.append(f"{field} = ?")
                params.append(val)
        # Handle api_key: don't overwrite with masked/preview value
        if req.api_key is not None and req.api_key and not re.match(r'^.{0,4}\*{4}.{0,4}$', req.api_key):
            updates.append("api_key = ?")
            params.append(req.api_key)
        # Handle stage_models: serialize to JSON, allow empty dict to clear
        if req.stage_models is not None:
            updates.append("stage_models = ?")
            params.append(json.dumps(req.stage_models) if req.stage_models else None)

        if updates:
            params.append(profile_id)
            await db.execute(
                f"UPDATE ai_profiles SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await db.commit()
            _invalidate_provider_cache()
        return {"status": "ok"}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: int):
    logger.info("删除AI配置 — profile_id=%d", profile_id)
    async with get_db() as db:
        await db.execute("DELETE FROM ai_profiles WHERE id = ?", (profile_id,))
        await db.commit()
    _invalidate_provider_cache()
    return {"status": "ok"}


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(profile_id: int):
    """Set a profile as the active one."""
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM ai_profiles WHERE id = ?", (profile_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Profile not found")

        await db.execute("UPDATE ai_profiles SET is_active = 0")
        await db.execute("UPDATE ai_profiles SET is_active = 1 WHERE id = ?", (profile_id,))
        await db.commit()
        _invalidate_provider_cache()
        logger.info("激活AI配置 — profile_id=%d", profile_id)
        return {"status": "ok", "active_id": profile_id}


@router.post("/profiles/{profile_id}/test")
async def test_profile(profile_id: int):
    """Test connectivity for a specific profile."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM ai_profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Profile not found")
        profile = dict(row)

    try:
        provider = _create_provider_from_profile(profile)
        success = await provider.test_connection()
        logger.info("测试AI配置 — profile_id=%d, provider=%s, model=%s, success=%s",
                     profile_id, profile["provider_type"], profile["model"], success)
        return {"success": success, "provider": profile["provider_type"], "model": profile["model"]}
    except Exception as e:
        logger.warning("测试AI配置失败 — profile_id=%d, error=%s", profile_id, e)
        return {"success": False, "error": str(e)}


# --- Pipeline mode (workflow / agentic) ---

@router.get("/pipeline-mode")
async def get_pipeline_mode(save_id: str | None = None):
    """Return a saved game's mode, or the default for new games."""
    import config
    if save_id is None:
        return {"mode": config.PIPELINE_MODE}
    if not save_id.strip():
        raise HTTPException(status_code=400, detail="save_id must be a non-empty string")
    from api.game_routes import _get_session_lock, _restore_session, _sessions
    async with _get_session_lock(save_id):
        session = _sessions.get(save_id) or await _restore_session(save_id)
        if not session:
            raise HTTPException(status_code=404, detail="Game session not found")
        return {"mode": session._get_pipeline_mode()}


@router.post("/pipeline-mode")
async def set_pipeline_mode(req: dict):
    """Change one saved game's mode, or the default for future games.

    Body: {"mode": "workflow" | "agentic" | "agentic_unified", "save_id": optional}
    """
    import config
    mode = req.get("mode", "workflow")
    if mode not in ("workflow", "agentic", "agentic_unified"):
        raise HTTPException(status_code=400, detail="mode must be 'workflow', 'agentic', or 'agentic_unified'")
    save_id = req.get("save_id")
    if save_id is None:
        config.PIPELINE_MODE = mode
        return {"ok": True, "mode": mode}
    if not isinstance(save_id, str) or not save_id.strip():
        raise HTTPException(status_code=400, detail="save_id must be a non-empty string")

    from api.game_routes import _get_session_lock, _restore_session, _sessions
    lock = _get_session_lock(save_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="当前存档正在推演，请在本轮完成后切换模式")
    async with lock:
        session = _sessions.get(save_id) or await _restore_session(save_id)
        if not session:
            raise HTTPException(status_code=404, detail="Game session not found")
        if any(not task.done() for task in session._background_tasks):
            raise HTTPException(status_code=409, detail="后台结算尚未完成，请稍后切换模式")
        node_id = session.world_tree.active_node_id
        node = session.world_tree.get_node(node_id)
        if not node:
            raise HTTPException(status_code=409, detail="当前存档尚未就绪，无法切换模式")
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state_snapshot FROM tree_nodes WHERE id = ? AND save_id = ?",
                (node_id, save_id),
            )
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="当前节点尚未保存，请稍后重试")
            snapshot = json.loads(row["state_snapshot"] or "{}")
            snapshot["_pipeline_mode"] = mode
            await db.execute(
                "UPDATE tree_nodes SET state_snapshot = ? WHERE id = ? AND save_id = ?",
                (json.dumps(snapshot, ensure_ascii=False), node_id, save_id),
            )
            await db.commit()
        session.pipeline_mode = mode
        session.current_state["_pipeline_mode"] = mode
        node["state_snapshot"] = {**(node.get("state_snapshot") or session.current_state), "_pipeline_mode": mode}
    logger.info("切换管线模式 — save_id=%s, mode=%s", save_id, mode)
    return {"ok": True, "mode": mode}


@router.post("/reload-prompts")
async def reload_prompts():
    """Hot-reload prompt templates (clears PromptLoader cache)."""
    from engine.prompt_loader import PromptLoader
    PromptLoader.get().reload()
    logger.info("Prompt 模板缓存已清除")
    return {"ok": True, "message": "Prompt 模板缓存已清除"}


# --- Model routing (task→model) ---

@router.post("/model-routes")
async def set_model_routes(req: dict):
    """Set task→model routing overrides at runtime."""
    import config
    routes = req.get("routes", {})
    for key in ("planning", "narrative", "settlement", "review", "memory", "image"):
        if key in routes:
            config.TASK_MODEL_ROUTES[key] = routes[key] or None
    return {"ok": True, "routes": config.TASK_MODEL_ROUTES}


@router.get("/model-routes")
async def get_model_routes():
    """Get current task→model routing configuration."""
    import config
    return {"routes": getattr(config, "TASK_MODEL_ROUTES", {})}


# --- Legacy config endpoints (kept for backward compat) ---

@router.get("")
async def get_config():
    """Get current active AI config."""
    profile = await get_active_profile()
    if not profile:
        return {"active_provider": None, "message": "No AI profile configured"}
    return {
        "active_provider": profile["provider_type"],
        "active_profile_id": profile["id"],
        "active_profile_name": profile["name"],
        "model": profile["model"],
        "base_url": profile.get("base_url"),
        "max_tokens": profile.get("max_tokens", 8192),
    }


@router.put("")
async def update_config(req: ConfigUpdateRequest):
    """Legacy: update via old format — creates/updates a profile."""
    if req.providers:
        for name, settings in req.providers.items():
            api_key = settings.get("api_key", "")
            if api_key and re.match(r'^.{0,4}\*{4}.{0,4}$', api_key):
                continue
            async with get_db() as db:
                # Check if profile with this provider type exists
                cursor = await db.execute(
                    "SELECT id FROM ai_profiles WHERE provider_type = ? LIMIT 1",
                    (name,),
                )
                row = await cursor.fetchone()
                if row:
                    updates = []
                    params = []
                    if api_key and not re.match(r'^.{0,4}\*{4}.{0,4}$', api_key):
                        updates.append("api_key = ?")
                        params.append(api_key)
                    if settings.get("model"):
                        updates.append("model = ?")
                        params.append(settings["model"])
                    if settings.get("base_url") is not None:
                        updates.append("base_url = ?")
                        params.append(settings["base_url"])
                    if settings.get("max_tokens"):
                        updates.append("max_tokens = ?")
                        params.append(settings["max_tokens"])
                    if updates:
                        params.append(row["id"])
                        await db.execute(
                            f"UPDATE ai_profiles SET {', '.join(updates)} WHERE id = ?",
                            params,
                        )
                else:
                    await db.execute(
                        """INSERT INTO ai_profiles (name, provider_type, api_key, model, max_tokens, base_url, is_active)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            name,
                            name,
                            api_key,
                            settings.get("model", "gpt-4o"),
                            settings.get("max_tokens", 8192),
                            settings.get("base_url"),
                            1 if req.active_provider == name else 0,
                        ),
                    )
                await db.commit()

    return {"status": "ok"}


@router.post("/test")
async def test_connection():
    """Test the active profile."""
    profile = await get_active_profile()
    if not profile:
        return {"success": False, "error": "No AI profile configured. Please add one in settings."}
    try:
        provider = _create_provider_from_profile(profile)
        success = await provider.test_connection()
        return {"success": success, "provider": profile["provider_type"], "name": profile["name"]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Presets ---

PROVIDER_PRESETS = {
    "xi-ai": {"name": "Xi-AI (第三方)", "provider_type": "openai_compatible", "base_url": "https://api.xi-ai.cn/v1", "model": "gpt-4o"},
    "deepseek": {"name": "DeepSeek", "provider_type": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "moonshot": {"name": "Moonshot (月之暗面)", "provider_type": "openai_compatible", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"name": "智谱 GLM", "provider_type": "openai_compatible", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4"},
    "siliconflow": {"name": "SiliconFlow", "provider_type": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-72B-Instruct"},
    "openrouter": {"name": "OpenRouter", "provider_type": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o"},
    "openai": {"name": "OpenAI 官方", "provider_type": "openai", "base_url": None, "model": "gpt-4o"},
    "claude": {"name": "Claude (Anthropic)", "provider_type": "claude", "base_url": None, "model": "claude-sonnet-4-20250514"},
    "ollama": {"name": "Ollama 本地", "provider_type": "ollama", "base_url": "http://localhost:11434", "model": "llama3"},
}


@router.get("/presets")
async def get_presets():
    return PROVIDER_PRESETS


# --- Helper functions ---

async def get_active_profile() -> dict | None:
    """Get the currently active AI profile. Used by other modules."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM ai_profiles WHERE is_active = 1 LIMIT 1")
        row = await cursor.fetchone()
        if row:
            return dict(row)

        # Fallback: get first profile
        cursor = await db.execute("SELECT * FROM ai_profiles ORDER BY id LIMIT 1")
        row = await cursor.fetchone()
        return dict(row) if row else None


# B7: 缓存 provider 实例，避免每次行动都重建（关键路径，每个 API 客户端都有连接池开销）
_provider_cache: dict = {"profile_id": None, "fingerprint": None, "instance": None}


def _profile_fingerprint(profile: dict) -> tuple:
    return (
        profile.get("id"),
        profile.get("provider_type"),
        profile.get("api_key", ""),
        profile.get("model"),
        profile.get("max_tokens"),
        profile.get("review_max_tokens"),
        profile.get("review_retry_max_tokens"),
        profile.get("base_url"),
        profile.get("stage_models"),
    )


def _invalidate_provider_cache():
    _provider_cache["profile_id"] = None
    _provider_cache["fingerprint"] = None
    _provider_cache["instance"] = None


def _create_provider_from_profile(profile: dict):
    """Create an AI provider from a profile dict."""
    ptype = profile.get("provider_type", "openai")
    config = {
        "api_key": profile.get("api_key", ""),
        "model": profile.get("model", "gpt-4o"),
        "max_tokens": profile.get("max_tokens", 8192),
        "base_url": profile.get("base_url"),
    }

    if ptype == "claude":
        from ai.claude_provider import ClaudeProvider
        provider = ClaudeProvider(config)
    elif ptype in ("openai", "openai_compatible"):
        from ai.openai_provider import OpenAIProvider
        provider = OpenAIProvider(config)
    elif ptype == "ollama":
        from ai.ollama_provider import OllamaProvider
        provider = OllamaProvider(config)
    else:
        from ai.openai_provider import OpenAIProvider
        provider = OpenAIProvider(config)
    provider.review_max_tokens = profile.get("review_max_tokens", 500)
    provider.review_retry_max_tokens = profile.get("review_retry_max_tokens", 1500)
    return provider


async def get_ai_provider_instance():
    """Public helper: get an AI provider from the active profile. Used by other route modules."""
    profile = await get_active_profile()
    if not profile:
        return None
    fp = _profile_fingerprint(profile)
    if _provider_cache["fingerprint"] == fp and _provider_cache["instance"] is not None:
        return _provider_cache["instance"]
    instance = _create_provider_from_profile(profile)
    instance._tavern_stage_models = _parse_profile_stage_models(profile)
    _provider_cache["profile_id"] = profile.get("id")
    _provider_cache["fingerprint"] = fp
    _provider_cache["instance"] = instance
    return instance


async def get_active_stage_models() -> dict:
    """Return parsed stage_models dict from active profile. Empty dict if none configured."""
    profile = await get_active_profile()
    if not profile:
        return {}
    return _parse_profile_stage_models(profile)


def _parse_profile_stage_models(profile: dict) -> dict:
    sm = profile.get("stage_models")
    if isinstance(sm, str) and sm:
        try:
            return json.loads(sm) or {}
        except Exception:
            return {}
    if isinstance(sm, dict):
        return copy.deepcopy(sm)
    return {}


# --- Image generation config (profile-based) ---

_CONFIG_KEY_IMG_PROFILES = "image_profiles"
_CONFIG_KEY_IMG_SETTINGS_LEGACY = "image_settings"
_CONFIG_KEY_IMG_STYLE = "image_style"


async def get_image_style() -> dict:
    """Load image style config from DB."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT value FROM config WHERE key = ?", (_CONFIG_KEY_IMG_STYLE,)
        )
        row = await cursor.fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                pass
    return {"preset": "", "custom": ""}


@router.get("/image/style")
async def get_image_style_endpoint():
    return await get_image_style()


@router.put("/image/style")
async def set_image_style_endpoint(req: dict):
    data = {"preset": req.get("preset", ""), "custom": req.get("custom", "")}
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (_CONFIG_KEY_IMG_STYLE, json.dumps(data, ensure_ascii=False)),
        )
        await db.commit()
    return data


async def _load_image_profiles() -> list[dict]:
    """Load image profiles from config table, with legacy migration."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT value FROM config WHERE key = ?", (_CONFIG_KEY_IMG_PROFILES,)
        )
        row = await cursor.fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except Exception:
                return []

        # Migrate from legacy single image_settings
        cursor2 = await db.execute(
            "SELECT value FROM config WHERE key = ?", (_CONFIG_KEY_IMG_SETTINGS_LEGACY,)
        )
        legacy = await cursor2.fetchone()
        if legacy:
            try:
                old = json.loads(legacy["value"])
                if old.get("provider", "disabled") != "disabled":
                    import uuid
                    profile = {
                        "id": f"ip_{uuid.uuid4().hex[:8]}",
                        "name": f"迁移配置 ({old['provider']})",
                        "provider": old["provider"],
                        "is_active": True,
                        "display_mode": old.get("display_mode", "split"),
                    }
                    for key in ("openai", "gemini", "comfyui"):
                        if key in old:
                            profile[key] = old[key]
                    profiles = [profile]
                    await db.execute(
                        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                        (_CONFIG_KEY_IMG_PROFILES, json.dumps(profiles, ensure_ascii=False)),
                    )
                    await db.commit()
                    return profiles
            except Exception:
                pass
    return []


async def _save_image_profiles(profiles: list[dict]):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (_CONFIG_KEY_IMG_PROFILES, json.dumps(profiles, ensure_ascii=False)),
        )
        await db.commit()
    _invalidate_image_provider_cache()


@router.get("/image/profiles")
async def list_image_profiles():
    """List all saved image profiles (api_key masked)."""
    profiles = await _load_image_profiles()
    safe = []
    for p in profiles:
        sp = {**p}
        for key in ("openai", "gemini"):
            if key in sp and isinstance(sp[key], dict) and sp[key].get("api_key"):
                k = sp[key]["api_key"]
                sp[key] = {**sp[key]}
                sp[key]["has_api_key"] = True
                sp[key]["api_key_preview"] = k[:4] + "****" + k[-4:] if len(k) > 8 else "****"
                sp[key]["api_key"] = ""
        safe.append(sp)
    return safe


@router.post("/image/profiles")
async def create_image_profile(req: dict):
    """Create a new image profile."""
    import uuid
    profiles = await _load_image_profiles()
    profile_id = f"ip_{uuid.uuid4().hex[:8]}"
    profile = {
        "id": profile_id,
        "name": req.get("name", "未命名"),
        "provider": req.get("provider", "disabled"),
        "is_active": False,
        "display_mode": req.get("display_mode", "split"),
    }
    for key in ("openai", "gemini", "comfyui"):
        if key in req:
            profile[key] = req[key]
    # If this is the first profile, auto-activate
    if not profiles:
        profile["is_active"] = True
    profiles.append(profile)
    await _save_image_profiles(profiles)
    return {"success": True, "id": profile_id}


@router.put("/image/profiles/{profile_id}")
async def update_image_profile(profile_id: str, req: dict):
    """Update an existing image profile."""
    profiles = await _load_image_profiles()
    target = None
    for p in profiles:
        if p["id"] == profile_id:
            target = p
            break
    if not target:
        raise HTTPException(status_code=404, detail="Image profile not found")

    if "name" in req:
        target["name"] = req["name"]
    if "provider" in req:
        target["provider"] = req["provider"]
    if "display_mode" in req:
        target["display_mode"] = req["display_mode"]
    for key in ("openai", "gemini", "comfyui"):
        if key in req:
            old = target.get(key, {})
            new = req[key]
            # Preserve api_key if not sent (empty = keep old)
            if key in ("openai", "gemini") and not new.get("api_key") and old.get("api_key"):
                new["api_key"] = old["api_key"]
            target[key] = {**old, **new}

    await _save_image_profiles(profiles)
    return {"success": True}


@router.delete("/image/profiles/{profile_id}")
async def delete_image_profile(profile_id: str):
    """Delete an image profile."""
    profiles = await _load_image_profiles()
    profiles = [p for p in profiles if p["id"] != profile_id]
    await _save_image_profiles(profiles)
    return {"success": True}


@router.post("/image/profiles/{profile_id}/activate")
async def activate_image_profile(profile_id: str):
    """Set a profile as active (deactivates others)."""
    profiles = await _load_image_profiles()
    found = False
    for p in profiles:
        if p["id"] == profile_id:
            p["is_active"] = True
            found = True
        else:
            p["is_active"] = False
    if not found:
        raise HTTPException(status_code=404, detail="Image profile not found")
    await _save_image_profiles(profiles)
    return {"success": True}


@router.get("/image")
async def get_image_settings():
    """Get the active image profile settings (backward-compatible)."""
    profiles = await _load_image_profiles()
    for p in profiles:
        if p.get("is_active"):
            return {**p, "enabled": True}
    return {"enabled": False, "provider": "disabled", "display_mode": "split"}


_image_provider_cache: dict = {"settings_hash": None, "instance": None}


def _invalidate_image_provider_cache():
    _image_provider_cache["settings_hash"] = None
    _image_provider_cache["instance"] = None


async def get_image_provider_instance():
    """Get an ImageProvider instance from the active image profile. Returns None if disabled."""
    profiles = await _load_image_profiles()
    active = None
    for p in profiles:
        if p.get("is_active"):
            active = p
            break
    if not active or active.get("provider", "disabled") == "disabled":
        return None

    import hashlib
    h = hashlib.md5(json.dumps(active, sort_keys=True).encode()).hexdigest()[:12]
    if _image_provider_cache["settings_hash"] == h and _image_provider_cache["instance"] is not None:
        return _image_provider_cache["instance"]

    # For openai: fallback to active AI profile's key if empty
    if active.get("provider") == "openai" and not active.get("openai", {}).get("api_key"):
        ai_profile = await get_active_profile()
        if ai_profile and ai_profile.get("api_key"):
            active.setdefault("openai", {})["api_key"] = ai_profile["api_key"]
            if ai_profile.get("base_url"):
                active["openai"].setdefault("base_url", ai_profile["base_url"])

    from ai.image_provider import create_image_provider
    active["enabled"] = True
    instance = create_image_provider(active)
    _image_provider_cache["settings_hash"] = h
    _image_provider_cache["instance"] = instance
    return instance


async def get_image_display_mode() -> str:
    """Get the current image display mode from the active profile."""
    profiles = await _load_image_profiles()
    for p in profiles:
        if p.get("is_active"):
            return p.get("display_mode", "split")
    return "split"
