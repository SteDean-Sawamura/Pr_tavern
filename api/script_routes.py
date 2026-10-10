"""Script management API routes with AI-assisted generation."""

import json
import re
import logging
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from urllib.parse import quote

from db.database import get_db
from engine.script_loader import ScriptLoader

router = APIRouter()
logger = logging.getLogger("tavern.scripts")

_SCRIPT_AI_SYSTEM_PROMPT = (
    "你是一个专业的游戏剧本设计AI助手。内容精炼但有创意。"
    "【重要】涉及真实历史人物/地名/国家/朝代/事件，保留真实名称。"
    "不要输出思考过程，直接返回JSON。"
)


class ScriptCreateRequest(BaseModel):
    id: str
    name: str
    content: dict


class ScriptUpdateRequest(BaseModel):
    name: Optional[str] = None
    content: Optional[dict] = None


class AIGenerateRequest(BaseModel):
    """Request for AI-assisted content generation."""
    field: str  # Which field to generate: 'full', 'background', 'opening', 'npc', 'location', 'event', 'random_item', 'persistent_state'
    prompt: str  # User's description/requirements
    context: Optional[dict] = None  # Existing script data for context


async def _get_ai_provider():
    """Get the configured AI provider."""
    from api.config_routes import get_ai_provider_instance
    return await get_ai_provider_instance()


async def _ai_generate_json(
    provider,
    user_prompt: str,
    *,
    system: str = "",
    max_tokens: int = 8192,
    salvage_truncated: bool = True,
) -> dict:
    """调用 AI 生成 JSON 并解析。失败时抛出 HTTPException。"""
    response = await provider.generate(
        [{"role": "user", "content": user_prompt}],
        system=system or _SCRIPT_AI_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    response = _strip_code_fences(response)
    parsed = _robust_json_extract(response)
    if parsed:
        return parsed
    if salvage_truncated:
        partial = _salvage_truncated_json(response)
        if partial:
            return partial
    raise HTTPException(
        status_code=500,
        detail=f"AI返回内容无法解析为JSON。原文前200字: {response[:200]}",
    )


# --- CRUD Routes ---

@router.get("")
async def list_scripts():
    """List all available scripts (from DB + files)."""
    async with get_db() as db:
        cursor = await db.execute("SELECT id, name, version, created_at, updated_at FROM scripts")
        rows = await cursor.fetchall()
        db_scripts = [dict(row) for row in rows]

    file_scripts = ScriptLoader.list_scripts()
    db_ids = {s["id"] for s in db_scripts}
    for fs in file_scripts:
        if fs["id"] not in db_ids:
            db_scripts.append(fs)

    return db_scripts


@router.get("/{script_id}")
async def get_script(script_id: str):
    """Get full script content."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM scripts WHERE id = ?", (script_id,))
        row = await cursor.fetchone()

    if row:
        result = dict(row)
        result["content"] = json.loads(result["content"])
        return result

    try:
        script = ScriptLoader.load(script_id)
        return {"id": script_id, "name": script.get("script_name", script_id), "content": script}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Script not found")


@router.post("")
async def create_script(req: ScriptCreateRequest):
    """Create a new script."""
    logger.info("创建剧本 — id=%s, name=%s", req.id, req.name)
    errors = ScriptLoader.validate(req.content)
    # 交叉引用校验（非阻塞 warning，兼容老剧本）
    cross_warnings = ScriptLoader.validate_cross_refs(req.content)
    if cross_warnings:
        errors.extend(cross_warnings)

    async with get_db() as db:
        # B11: 用 INSERT ... ON CONFLICT 避免 REPLACE 重建行导致 created_at 重置
        await db.execute(
            """INSERT INTO scripts (id, name, content)
               VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   content = excluded.content,
                   updated_at = CURRENT_TIMESTAMP""",
            (req.id, req.name, json.dumps(req.content, ensure_ascii=False)),
        )
        await db.commit()

    if errors:
        logger.warning("剧本验证有问题 — id=%s, errors=%d", req.id, len(errors))
        return {"status": "warning", "errors": errors, "saved": True, "id": req.id}
    return {"status": "ok", "id": req.id}


@router.put("/{script_id}")
async def update_script(script_id: str, req: ScriptUpdateRequest):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM scripts WHERE id = ?", (script_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Script not found")

        updates = []
        params = []
        if req.name is not None:
            updates.append("name = ?")
            params.append(req.name)
        if req.content is not None:
            updates.append("content = ?")
            params.append(json.dumps(req.content, ensure_ascii=False))

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(script_id)
            await db.execute(
                f"UPDATE scripts SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await db.commit()

    return {"status": "ok"}


@router.delete("/{script_id}")
async def delete_script(script_id: str):
    logger.info("删除剧本 — script_id=%s", script_id)
    async with get_db() as db:
        # Delete related saves and their tree nodes first (FK without CASCADE)
        cursor = await db.execute("SELECT id FROM saves WHERE script_id = ?", (script_id,))
        save_rows = await cursor.fetchall()
        for row in save_rows:
            await db.execute("DELETE FROM tree_nodes WHERE save_id = ?", (row["id"],))
        await db.execute("DELETE FROM saves WHERE script_id = ?", (script_id,))
        await db.execute("DELETE FROM material_script_links WHERE script_id = ?", (script_id,))
        await db.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
        await db.commit()
    return {"status": "ok"}


@router.get("/{script_id}/export")
async def export_script(script_id: str):
    """Export a script as a downloadable JSON file."""
    async with get_db() as db:
        cursor = await db.execute("SELECT name, content FROM scripts WHERE id = ?", (script_id,))
        row = await cursor.fetchone()

    if row:
        content = json.loads(row["content"])
        name = row["name"]
    else:
        try:
            content = ScriptLoader.load(script_id)
            name = content.get("script_name", script_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Script not found")

    filename = f"{name}.json"
    encoded = quote(filename)
    return Response(
        content=json.dumps(content, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@router.post("/import")
async def import_script(file: UploadFile = File(...)):
    """Import a script from an uploaded JSON file."""
    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="请上传 .json 文件")

    raw = await file.read()
    try:
        content = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="文件不是有效的 JSON")

    if not isinstance(content, dict):
        raise HTTPException(status_code=400, detail="JSON 内容必须是对象")

    script_id = content.get("script_id", "")
    script_name = content.get("script_name", "")
    if not script_id:
        raise HTTPException(status_code=400, detail="JSON 中缺少 script_id 字段")

    errors = ScriptLoader.validate(content)
    cross_warnings = ScriptLoader.validate_cross_refs(content)

    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM scripts WHERE id = ?", (script_id,))
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                "UPDATE scripts SET name = ?, content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (script_name or script_id, json.dumps(content, ensure_ascii=False), script_id),
            )
        else:
            await db.execute(
                "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                (script_id, script_name or script_id, json.dumps(content, ensure_ascii=False)),
            )
        await db.commit()

    result = {"status": "ok", "id": script_id, "name": script_name, "overwritten": bool(existing)}
    all_warnings = errors + cross_warnings
    if all_warnings:
        result["warnings"] = all_warnings
    return result


@router.post("/{script_id}/validate")
async def validate_script(script_id: str):
    async with get_db() as db:
        cursor = await db.execute("SELECT content FROM scripts WHERE id = ?", (script_id,))
        row = await cursor.fetchone()

    if row:
        script = json.loads(row["content"])
    else:
        try:
            script = ScriptLoader.load(script_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Script not found")

    errors = ScriptLoader.validate(script)
    cross_warnings = ScriptLoader.validate_cross_refs(script)
    all_errors = errors + cross_warnings
    return {"valid": len(errors) == 0, "errors": all_errors}


# --- AI-Assisted Generation Routes ---

# Prompt templates for each field type
_FIELD_PROMPTS = {
    "full": """你是一个游戏剧本设计专家。请根据用户的描述，生成游戏剧本的【基本框架】JSON。
只需要生成世界观基本信息，不要生成角色、事件、组织、知识库等内容（后续会分步骤补充）。

用户描述：{prompt}

请生成JSON，只包含以下字段（内容精炼）：
{{
  "script_id": "英文小写ID",
  "script_name": "剧本名称",
  "start_time": "ISO时间(如2025-09-01T07:00)",
  "world_background": "世界背景(2段，每段80-150字)",
  "settings": {{"dice_check":{{"default_enabled":true,"player_can_toggle":true}},"fixed_opening":{{"default_enabled":true,"player_can_toggle":true}}}}
}}

注意：不要生成player_character、opening、npcs、locations、events、organizations、lorebook等，这些会在后续步骤中生成。
【重要】涉及真实历史人物/地名/国家保留真实名称。只返回JSON。""",
}


@router.post("/ai-generate")
async def ai_generate_field(req: AIGenerateRequest):
    """Use AI to generate a full script framework."""
    if req.field != "full":
        raise HTTPException(status_code=400, detail="仅支持 field='full'，其他内容请使用 /ai-generate/tab")

    logger.info("AI生成完整剧本 — prompt=%s", req.prompt[:80])
    provider = await _get_ai_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")

    template = _FIELD_PROMPTS["full"]
    system_prompt = (
        "你是一个专业的游戏剧本设计AI助手。请根据用户要求生成对应的游戏内容。保持创意但合理。"
        "【重要】如果世界背景或指令中涉及真实的历史人物、地名、国家、朝代、事件，"
        "必须保留这些真实名称，不要用虚构名称替代。"
    )
    user_prompt = template.format(prompt=req.prompt)

    try:
        response = await provider.generate(
            [{"role": "user", "content": user_prompt}],
            system=system_prompt,
            max_tokens=8192,
        )
        response = _strip_code_fences(response)
        result = {"raw": response}

        parsed = _robust_json_extract(response)
        if parsed:
            result["parsed"] = parsed

        return result

    except Exception as e:
        logger.error("AI生成失败 — error=%s", e)
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")


# --- Per-Tab Bulk Generation ---
# Each tab can be generated in a single focused AI call, based on existing script context.

_TAB_PROMPTS = {
    "characters": None,  # split into sections, see _SECTION_PROMPTS

    "world": None,  # split into sections, see _SECTION_PROMPTS

    "organizations": None,  # split into sections, see _SECTION_PROMPTS

    "events": None,  # split into sections, see _SECTION_PROMPTS

    "dice": """基于以下剧本，补充"随机项与自动化"tab的内容。

剧本概况：
{script_summary}

【补充规则】
- 分析已有随机项/变量/触发器/正则规则，只补充缺失或不足的部分
- 已有的条目不要重复生成（ID不能重复）
- 至少应有天气(weather)随机项，如已有则不要重复
- **请根据末尾【数量控制】指导生成合适数量**的随机项（事件氛围、突发遭遇、环境变化、NPC情绪等），每项的ranges描述要丰富立体
- 变量: 为剧本设计有意义的计数器/标志位（如quest_count、trust_level、is_discovered），供触发器和lorebook宏引用
- 触发器: 设计自动化逻辑（如每回合递增变量、到达阈值激活知识库词条等）
- 正则规则: 按需添加输出过滤规则（如去除OOC内容、格式化对话标签等）
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（只包含需要新增的内容）：
{{
  "random_items": [按需补充各类随机项(必须含天气weather) {{
    "id":"英文ID（不能与已有重复）",
    "description":"描述",
    "trigger":"触发时机",
    "trigger_type":"always"/"conditional"/"event_linked",
    "linked_event_id":"关联事件ID(trigger_type为event_linked时填写)",
    "condition":"触发条件表达式(trigger_type为conditional时填写，如player.health<50)",
    "duration_turns":0,
    "cooldown_turns":0,
    "dice":{{"count":1,"faces":100,"modifier":0,"keep_highest":0,"keep_lowest":0}},
    "ranges":[{{"min":1,"max":30,"label":"结果简称","description":"结果详细描述","state_changes":[{{"target":"player.属性名","op":"add","value":5}}]}},{{"min":31,"max":70,"label":"结果2","description":"描述","state_changes":[]}},{{"min":71,"max":100,"label":"结果3","description":"描述","state_changes":[]}}]
  }}],
  "variables": [按需补充 {{
    "id":"英文变量ID(如quest_count)",
    "name":"显示名称(如任务计数)",
    "type":"number"/"string"/"bool",
    "default":0,
    "min":-9999,
    "max":9999
  }}],
  "triggers": [按需补充 {{
    "id":"英文ID(如t_inc_quest)",
    "event":"on_start"/"before_generation"/"after_ai",
    "action":"set_var"/"inject_prompt"/"activate_lore"/"deactivate_lore"/"notify",
    "condition":"条件表达式(可选，如quest_count >= 3)",
    "params":{{"var_id":"变量ID","op":"inc","value":1}},
    "enabled":true
  }}],
  "regex_scripts": [按需补充 {{
    "id":"英文ID(如rx_remove_ooc)",
    "name":"规则名称",
    "find":"正则表达式",
    "replace":"替换内容(空=删除匹配)",
    "placement":"ai_output"/"user_input",
    "enabled":true
  }}]
}}
注意ranges必须覆盖骰子完整范围(1到faces)。只返回JSON。""",

    "story_tree": None,  # split into sections, see _SECTION_PROMPTS

    "lorebook": """基于以下剧本，补充"知识库"tab的内容。

剧本概况：
{script_summary}

【补充规则】
- 分析已有知识库词条，只补充缺失或不足的部分
- 已有的词条不要重复生成（ID和关键词不能重复）
- 每条content控制在80-150字以内，精炼但信息密度高
- **请根据末尾【数量控制】指导生成合适数量**的知识词条，覆盖世界观中的重要背景、历史事件、独特概念、地域文化、组织内幕等
- 如果所有内容都已充分，返回空JSON: {{}}
- **客观视角**：知识库是世界的客观百科，必须以第三人称、旁观者视角书写。禁止出现"你""主角""玩家"等字眼，不涉及主角的经历、感受或行动。只记录这个世界本身的事实：历史、地理、文化、组织、人物背景、规则等

请生成JSON（只包含需要新增的内容）：
{{
  "lorebook": [按需补充 {{
    "id":"英文ID（不能与已有重复）",
    "keys":["关键词1","关键词2"],
    "secondary_keys":[],
    "content":"客观世界百科条目(80-150字，第三人称，不涉及主角)",
    "position":"after_world",
    "enabled":true,
    "constant":false,
    "priority":100,
    "scan_depth":3,
    "comment":"标签",
    "related_entries":["关联词条ID(可选)"]
  }}]
}}
词条应覆盖该世界的重要背景知识、历史事件、独特概念。只返回JSON。""",
}

# Tabs that are split into multiple smaller AI calls to avoid timeout
_TAB_SECTIONS = {
    "characters": ["player", "npcs"],
    "world": ["locations", "properties_states"],
    "organizations": ["organizations"],
    "events": ["cyclic", "one_time", "tone_rules", "quest_templates"],
    "story_tree": ["trees"],
}

_SUPP_LORE_SCHEMA_NPC = """  "supplementary_lorebook": [为每个新NPC生成1+N条知识库词条 {{
    "id":"_kg_npc_{{npc的id}}_{{类型标签如intro/rel/hist1/secret}}",
    "keys":["NPC名字","别名/称号"],
    "secondary_keys":["关联人名或事件名"],
    "content":"词条内容(80-150字，第三人称客观百科视角)",
    "position":"after_world","priority":85,"constant":false,"scan_depth":3,
    "comment":"类型标签(人物简介/人物关系/历史事件/秘密动机)",
    "related_entries":["_kg_npc_对应NPC的ID","关联词条ID"]
  }}]

supplementary_lorebook生成规则：
- 每个新NPC至少1条"人物简介"词条(id后缀_intro)：身份、外貌特征、性格、背景概述
- 有复杂关系的NPC额外生成"人物关系"词条(id后缀_rel)：与其他NPC的关系网络
- 史实/传说人物额外生成1-3条历史事件词条(id后缀_hist1/_hist2)：重大经历、典故、成就
- 有隐藏动机的NPC生成"秘密动机"词条(id后缀_secret)
- keys必须包含NPC的名字和常见称呼，secondary_keys包含关联人物名
- related_entries互相引用以建立知识图谱联动"""

_SUPP_LORE_SCHEMA_LOC = """  "supplementary_lorebook": [按需补充地点相关知识词条 {{
    "id":"英文ID",
    "keys":["地点名关键词"],
    "secondary_keys":[],
    "content":"该地点的历史传说或隐藏信息(1-2段)，必须包含地理位置信息（所在城市/区域、与其他地标的方位关系）",
    "position":"after_world","priority":85,"constant":false,"scan_depth":3,
    "comment":"标签","related_entries":[]
  }}]"""

_SUPP_LORE_SCHEMA_ORG = """  "supplementary_lorebook": [按需补充组织相关知识词条 {{
    "id":"英文ID",
    "keys":["组织名关键词"],
    "secondary_keys":[],
    "content":"组织内部运作机制或隐秘信息(1-2段)，应包含各层级(hierarchy)的职权边界与权限范围描述",
    "position":"after_world","priority":85,"constant":false,"scan_depth":3,
    "comment":"标签","related_entries":[]
  }}]"""

_SUPP_LORE_SCHEMA_EVENT = """  "supplementary_lorebook": [按需补充事件相关知识词条 {{
    "id":"英文ID",
    "keys":["事件名或相关人物关键词"],
    "secondary_keys":[],
    "content":"事件的伏笔或关联人物的秘密动机(1-2段)",
    "position":"after_world","priority":85,"constant":false,"scan_depth":3,
    "comment":"标签","related_entries":[]
  }}]"""

_SECTION_PROMPTS = {
    "characters:player": """基于以下剧本，补充"玩家角色与预设"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析剧本概况中已有的内容，判断哪些需要新建、哪些需要补充
- 已有主角设定(bio/goal/attributes)→ 不要重复生成player_character
- 已有开局描述和选项 → 不要重复生成opening
- 已有预设主角 → 不要重复生成，可追加不同类型的预设
- **请根据末尾【数量控制】指导生成合适数量**，覆盖不同社会阶层、性格、专长
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（只包含需要新增/补充的字段）：
{{
  "player_character": {{
    "bio": "主角默认简介(1-2句)",
    "personality": "主角性格(1句)",
    "portrait_desc": "外貌简述(1句)",
    "long_term_goal": "主角默认目标(1句)",
    "initial_location": "初始地点ID(必须使用已有地点的ID)",
    "initial_inventory": [{{"item":"物品名","quantity":1}}],
    "attributes": {{"属性中文名":{{"value":50,"min":0,"max":100,"rule":"说明"}}, ...}} (3-5个属性，如健康/心情/金钱等)
  }},
  "opening": {{
    "text": "全局开局描述(2-3段，第二人称'你'，描述主角初入游戏世界时的场景)",
    "choices": [
      {{"id":"open_0","text":"选项1描述","result":{{"type":"deterministic","description":"选择后的结果描述","state_changes":[]}}}},
      {{"id":"open_1","text":"选项2描述","result":{{"type":"deterministic","description":"选择后的结果描述","state_changes":[]}}}},
      {{"id":"open_2","text":"带条件判定的选项(可选)","result":{{"type":"conditional","description":"结果方向","threshold":50,"success":{{"description":"成功时的结果"}},"failure":{{"description":"失败时的结果"}},"state_changes":[]}}}}
    ]
  }},
  "player_presets": [按需补充预设主角（不含已有的） {{
    "id":"英文小写ID（不能与已有ID重复）",
    "name":"角色名",
    "bio":"角色身份和背景(1-2句，每个预设应有明显不同的背景)",
    "personality":"性格特点(1句)",
    "initial_location":"初始地点ID(可选，必须使用已有地点的ID)",
    "long_term_goal":"角色目标(可选)",
    "portrait_desc":"外貌简述(可选)",
    "opening_text":"该角色的专属开局描述(2-4句，用第二人称'你'，描述角色在初始位置的开局场景，体现角色的身份和性格特征。可选，留空则使用全局开局)",
    "opening_choices":[（可选，2-4个贴合该预设角色身份/性格的专属开局选项；留空则使用全局开局选项；可混用 deterministic 和 conditional 两种 type）
      {{"id":"open_0","text":"选项1描述（贴合该角色身份/能力）","result":{{"type":"deterministic","description":"选择后的结果方向","state_changes":[]}}}},
      {{"id":"open_1","text":"选项2描述","result":{{"type":"conditional","description":"结果方向","threshold":50,"success":{{"description":"成功结果"}},"failure":{{"description":"失败结果"}},"state_changes":[]}}}}
    ],
    "attributes":{{"属性名":{{"value":数值}},...}} (根据角色背景设不同的初始属性值)
  }}]
}}

player_presets中的角色应有不同的社会阶层/性格/专长，给玩家多样化选择。
属性值应根据角色背景合理差异化（武者武力高、书生学识高等）。
每个预设角色如果有不同于全局开局的初始位置或身份，应提供opening_text描写其专属开局场景，并提供opening_choices设计贴合其身份的专属选项。
选项result.type支持两种: "deterministic"(固定结果) 和 "conditional"(属性判定，需提供threshold/success/failure)。建议大部分选项用deterministic，1个可选conditional增加随机性。
只返回JSON。""",

    "characters:npcs": """基于以下剧本，补充"NPC角色与关系"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析剧本概况中已有的NPC和关系，判断哪些需要新建、哪些需要补充
- 对标记为[缺:...]的已有NPC，请使用其**相同ID**输出需要补充的字段（只输出缺失字段即可，已有字段不要覆盖）
- 新NPC使用新ID（不能与已有ID重复）
- 已有的NPC关系不要重复，补充缺失的关系（特别是已有NPC之间还没有关系的）
- **请根据末尾【数量控制】指导生成合适数量**的NPC（含主要、次要、背景人物），覆盖不同角色定位、动机、立场，并补充新增 NPC 间以及与已有 NPC 的有意义关系
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（包含需要新增的NPC和需要补充字段的已有NPC）：
{{
  "npcs": [新NPC或需补充字段的已有NPC {{
    "id":"英文小写ID（新NPC用新ID，补充已有NPC用其原ID）",
    "name":"中文名",
    "bio":"简介(1-2句)",
    "personality":"性格(1句)",
    "capabilities":"能提供什么(1句)",
    "portrait_desc":"外貌简述(1句，可选)",
    "title":"头衔或空",
    "organizations":[{{"org_id":"组织ID(必须使用已有组织的ID)","rank":"组织内层级编号(对应hierarchy中的rank数字)","role":"特殊角色(可选，如间谍/顾问)"}}],
    "superior":"上级NPC的ID（可选，无则空）",
    "default_location":"该NPC通常所在的地点ID(必须使用已有地点的ID)",
    "attitude_toward_player":"根据关系设定0-100",
    "known":true,
    "met":true,
    "schedule": [
      {{"time_range":"07:00-12:00","location":"地点ID","activity":"NPC在该时段的活动描述"}},
      {{"time_range":"12:00-18:00","location":"地点ID","activity":"NPC在该时段的活动描述"}},
      {{"time_range":"18:00-23:00","location":"地点ID","activity":"NPC在该时段的活动描述"}}
    ],
    "related_lore":["关联知识库词条ID(可选，与该NPC相关的lorebook词条id列表)"],
    "goals":[{{"id":"目标ID","description":"目标描述","type":"short_term/long_term","priority":"low/medium/high","condition_met":"完成条件表达式(可选)","progress_hint":"推进提示(可选)","conflict_with_player":"与玩家冲突点(可选)"}}]
  }}],
  "npc_relationships": [按需补充NPC间有意义的关系 {{
    "from":"NPC_A的ID",
    "to":"NPC_B的ID",
    "trust":0-100(信任度，默认50),
    "affection":0-100(好感度，默认50),
    "fear":0-100(畏惧度，默认0),
    "description":"关系描述(1句)",
    "initially_known":true或false(玩家初始是否知道此关系),
    "initially_met":true或false(双方是否初始已熟识)
  }}],
""" + _SUPP_LORE_SCHEMA_NPC + """
}}

npc_relationships应体现NPC间有意义的关系（合作/对立/暗中联系等）。每条关系用from/to指定方向，trust/affection/fear三个0-100数值刻画关系强度——例如师徒(trust=85,affection=70,fear=10)、宿敌(trust=10,affection=5,fear=40)、暗恋(trust=60,affection=90,fear=0)。注意关系是单向的，A→B和B→A的数值可以不同（如徒弟畏惧师父，但师父不畏惧徒弟）。
supplementary_lorebook为每个NPC构建知识库词条组，通过关键词按需触发而非每次全量注入：
- 每个新NPC必须至少有1条"人物简介"词条（id: _kg_npc_{{id}}_intro）
- 史实人物/传说人物必须额外生成历史事件相关词条（如重要战役、典故、成就）
- 有复杂关系网络的NPC应额外生成"人物关系"词条
- related_entries填写关联NPC的ID（格式_kg_npc_{{npc的id}}）以建立知识图谱联动
NPC的related_lore字段用于关联这些词条ID（如_kg_npc_{{id}}_intro等）。
只返回JSON。""",

    # --- World tab sections ---

    "world:locations": """基于以下剧本，补充"地点"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析已有地点，只补充缺失或不足的部分
- 对标记为[缺:...]的已有地点，请使用其**相同ID**输出需要补充的字段（如缺描述就补description，缺连接就补connections）
- 新地点使用新ID（不能与已有ID重复）
- **请根据末尾【数量控制】指导生成合适数量**的地点（含公共场所、隐秘地点、特殊区域），每个地点描述要生动具体并包含地理方位信息（所在城市/区域、与其他地标的相对位置），部分设为隐藏(initially_visible:false)
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（包含需要新增的地点和需要补充字段的已有地点）：
{{
  "locations": [新地点或需补充字段的已有地点 {{"id":"英文ID（新地点用新ID，补充已有地点用其原ID）","name":"名称","description":"描述(1-2句)","initially_visible":true/false,"connections":["相邻可达的地点ID"]}}],
""" + _SUPP_LORE_SCHEMA_LOC + """
}}
只返回JSON。""",

    "world:properties_states": """基于以下剧本，补充"世界属性与持续状态"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析已有世界属性和持续状态，判断哪些需要新增、哪些需要补充
- 对已有但缺失字段的项目，使用其**相同ID**输出需要补充的字段
- 新项目使用新ID（不能与已有ID重复）
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（包含需要新增和需要补充的内容）：
{{
  "world_properties": [新增或补充的世界属性 {{"id":"英文ID（新项用新ID，补充已有项用原ID）","name":"名称","value":"初始值","rule":"说明"}}],
  "persistent_states": [新增或补充的持续状态 {{"id":"英文ID（新项用新ID，补充已有项用原ID）","name":"显示名(中文简称)","description":"状态效果描述","initially_active":true,"expires_at":null}}]
}}
只返回JSON。""",

    # --- Organizations tab sections ---

    "organizations:organizations": """基于以下剧本，补充"组织/势力"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析已有组织/势力，只补充缺失或不足的部分
- 已有的不要重复生成（ID不能重复）
- **请根据末尾【数量控制】指导生成合适数量**的组织/势力（帮派、公司、军队、国家、阵营、派系等各种类型），描述要具体
- hierarchy 各层级应明确职位名称，supplementary_lorebook 中应详细描述各层级的职权边界与权限范围（如：哪些层级可以对外发布指令、哪些仅限内部事务）
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（只包含需要新增的内容）：
{{
  "organizations": [按需补充各类组织/势力 {{"id":"英文ID（不能与已有重复）","name":"名称","type":"类型(帮派/公司/军队/国家/阵营等)","parent_org":"父组织ID(子组织时填写，可选)","leader":"领导者NPC的ID(可选)","stance":"对主角的立场(可选)","description":"描述(1-2句)","aliases":["别名(可选)"],"hierarchy":[{{"rank":1,"title":"最高职位名称"}},{{"rank":2,"title":"次级职位"}},{{"rank":3,"title":"基层职位"}}],"goals":[{{"id":"目标ID","description":"组织目标描述","priority":"low/medium/high","condition_met":"完成条件表达式(可选)","conflict_with_player":"与玩家冲突点(可选)"}}]}}],
  "org_relationships": [补充组织间关系 {{"a":"组织A的ID","b":"组织B的ID","type":"同盟/敌对/竞争/从属/中立","description":"关系描述(1句)"}}],
""" + _SUPP_LORE_SCHEMA_ORG + """
}}
只返回JSON。""",

    # --- Events tab sections ---

    "events:cyclic": """基于以下剧本，补充"周期事件"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析已有周期事件，只补充缺失或不足的部分
- 已有的事件不要重复生成（ID不能重复）
- **请根据末尾【数量控制】指导生成合适数量**的周期事件（日常作息、季节变化、特殊纪念日、阶段性活动等）
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（只包含需要新增的内容）：
{{
  "cyclic_events": [按需补充周期事件 {{"id":"英文ID（不能与已有重复）","name":"事件名称","description":"描述(1句)","frequency_value":1,"frequency_unit":"day","first_trigger":"与start_time一致的ISO时间","expires_at":null,"condition":"触发条件表达式(可选，如player.health>30)","fire_events":["触发的游戏事件名(可选，可被其他节点的activate_events接收)"],"activate_events":["被哪些游戏事件触发解锁(可选)"]}}]
}}
只返回JSON。""",

    "story_tree:trees": """基于以下剧本，生成/补充"剧情树"。

剧本概况：
{script_summary}

【设计规则】
- 分析已有剧情树（如有），只补充缺失或不足的部分
- 已有的剧情线和节点不要重复生成（ID不能重复）
- 每条剧情线设计4-6个有序节点，体现渐进式剧情推进
- 节点类型混搭使用：auto（条件满足自动触发）、choice（玩家选择分支）、quest（AI判定完成）、timed（限时倒计时）、trigger（生命周期钩子，匹配event事件名时执行effects，可重复触发）、periodic（周期事件，条件满足时按权重随机触发，有cooldown冷却）
- requires 填写前置节点ID（必须在同一剧情线内）
- condition 使用已有的变量/属性条件表达式（如 tension >= 20）
- on_complete_unlock 填写完成后解锁的节点ID
- choice 节点必须包含 choices 数组，每个选项有 id、label、description、effects、unlock
- effects 可包含: set_var（修改变量）、activate_lore/deactivate_lore（激活/禁用知识库词条）、inject_prompt（注入AI提示）、notify（通知玩家）、fire_events（触发游戏事件，可被其他节点的activate_events接收）、unlock_nodes（直接解锁指定节点）、activate_state（激活持久状态）、narrative_callback（叙事回调）、unlock_locations（解锁新地点ID列表）、reveal_npcs（揭示NPC ID列表）、set_reputation（修改声望[{{"faction_id":"势力ID","op":"add","value":10}}]）
- trigger 节点的 event 字段填写生命周期事件名（on_start/before_generation/after_ai），可填多个
- periodic 节点需填写 cooldown（冷却回合数）、weight（权重，默认10）、repeatable（是否可重复，默认true）
- activate_events 字段可让节点被指定的游戏事件解锁（如另一个节点 effects 中的 fire_events 触发）
- 引用的变量ID必须在已有变量列表中，引用的知识库词条ID必须在已有lorebook中
- **请根据末尾【数量控制】指导生成合适数量**的剧情线

请生成JSON（只包含需要新增的内容）：
{{
  "trees": [按需补充剧情线 {{
    "id": "英文ID（不能与已有重复）",
    "name": "剧情线名称",
    "description": "剧情线描述(1句)",
    "icon": "图标(crown/sword/shield/scroll/star/skull/eye/fire/moon/tree/castle/gem)",
    "nodes": [
      {{
        "id": "节点英文ID",
        "name": "节点名称",
        "description": "节点描述(1-2句)",
        "type": "auto/choice/quest/timed/trigger/periodic",
        "requires": ["前置节点ID"],
        "condition": "触发条件表达式(可选)",
        "duration_turns": 0,
        "on_complete_unlock": ["完成后解锁的节点ID"],
        "activate_events": ["被哪些游戏事件触发解锁(可选)"],
        "event": ["生命周期事件名(仅trigger类型，如on_start/before_generation/after_ai)"],
        "cooldown": 5,
        "weight": 10,
        "repeatable": true,
        "related_npcs": ["关联NPC的ID(可选)"],
        "related_orgs": ["关联组织的ID(可选)"],
        "effects": {{
          "set_var": [{{"var_id":"变量ID","op":"set/add/inc","value":"值"}}],
          "activate_lore": ["知识库词条ID"],
          "deactivate_lore": ["知识库词条ID"],
          "inject_prompt": "注入AI的提示语(可选)",
          "notify": "通知玩家的消息(可选)",
          "fire_events": ["触发的游戏事件名(可选)"],
          "unlock_nodes": ["直接解锁的节点ID(可选)"],
          "activate_state": ["激活的持久状态ID(可选)"],
          "narrative_callback": {{"text": "叙事回调文本(可选)", "priority": "medium"}}
        }},
        "choices": [仅choice类型 {{
          "id": "选项ID",
          "label": "选项显示文字",
          "description": "选项描述",
          "effects": {{}},
          "unlock": ["选择后解锁的节点ID"]
        }}]
      }}
    ]
  }}]
}}
注意节点间的 requires/on_complete_unlock/choices.unlock 形成的依赖图必须是有向无环图。
choice节点的不同选项应解锁不同的后续节点，体现分支互斥性。
只返回JSON。""",

    "events:one_time": """基于以下剧本，补充"一次性事件"部分。

剧本概况：
{script_summary}

【补充规则】
- 分析已有一次性事件，只补充缺失或不足的部分
- 已有的事件不要重复生成（ID不能重复）
- **请根据末尾【数量控制】指导生成合适数量**的一次性事件（含主线推进、支线触发、隐藏事件、突发危机等）
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（只包含需要新增的内容）：
{{
  "one_time_events": [按需补充一次性事件 {{"id":"英文ID（不能与已有重复）","name":"事件名称","description":"描述(1-2句)","trigger_time":"ISO时间","condition":"触发条件表达式(可选，如player.reputation>=80)","fire_events":["触发的游戏事件名(可选，可被其他节点的activate_events接收)"],"activate_events":["被哪些游戏事件触发解锁(可选)"]}}],
""" + _SUPP_LORE_SCHEMA_EVENT + """
}}
只返回JSON。""",

    "events:tone_rules": """基于以下剧本，补充"氛围基调规则"。

剧本概况：
{script_summary}

【补充规则】
- 分析已有基调规则，只补充缺失或不足的部分
- 基调规则用于控制 AI 叙事风格随剧情变化
- 每条规则定义一个条件（使用已有变量/属性表达式），满足时切换叙事基调
- priority 高的规则覆盖低的；通常设计 2-5 条规则覆盖不同氛围
- 如果所有内容都已充分，返回空JSON: {{}}

请生成JSON（只包含需要新增的内容）：
{{
  "tone_rules": [按需补充 {{"id":"英文ID","name":"规则名称","condition":"条件表达式(如 tension >= 80)","tone":"基调描述(如紧张、压迫)","narrative_style":"叙事风格描述(如短句、急促、多用听觉描写)","priority":10}}]
}}
只返回JSON。""",

    "events:quest_templates": """基于以下剧本，补充"支线任务模板"。

剧本概况：
{script_summary}

【补充规则】
- 支线模板定义条件触发的可选任务线索
- condition 使用已有变量/属性/阵营声望表达式
- trigger_hint 是 AI 在叙事中引出支线的提示文本
- cooldown_turns 控制同一支线触发间隔
- 设计 2-5 个支线模板，覆盖不同游玩路线

请生成JSON（只包含需要新增的内容）：
{{
  "quest_templates": [按需补充 {{"id":"英文ID","name":"支线名称","description":"触发说明","condition":"条件表达式","trigger_hint":"引入提示","reward_hint":"奖励提示","cooldown_turns":10}}]
}}
只返回JSON。""",
}

# ---- Polish-mode prompts (优化已有内容，不新增实体) ----

_TAB_POLISH_PROMPTS: dict[str, str | None] = {
    "characters": None,
    "world": None,
    "organizations": None,
    "events": None,

    "story_tree": None,  # split into sections

    "dice": """基于以下剧本，优化"随机项与自动化"tab 中已有内容的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增任何条目
- 随机项: 保持 id、trigger_type、dice、ranges 的 min/max/state_changes、duration_turns、cooldown_turns 不变；润色 description、trigger、condition、每个 range 的 label 和 description
- 变量: 保持 id、type、default、min、max 不变；润色 name 使其更直观
- 触发器: 保持 id、event、action、params 不变；润色 condition 使其更清晰
- 正则规则: 保持 id、find、replace、placement 不变；润色 name 使其更易理解
- 空白的描述性字段根据世界背景和已有NPC/地点/事件上下文补全
- 如果所有内容都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的条目）：
{{{{
  "random_items": [{{
    "id":"已有ID（不可改）",
    "description":"润色后的描述",
    "trigger":"润色后的触发时机",
    "condition":"润色后的条件(可选)",
    "ranges":[{{"min":原值,"max":原值,"label":"润色后简称","description":"润色后描述","state_changes":保持原值}}]
  }}],
  "variables": [{{
    "id":"已有ID（不可改）",
    "name":"润色后名称"
  }}],
  "triggers": [{{
    "id":"已有ID（不可改）",
    "condition":"润色后条件(可选)"
  }}],
  "regex_scripts": [{{
    "id":"已有ID（不可改）",
    "name":"润色后名称"
  }}]
}}}}
只返回JSON。""",

    "lorebook": """基于以下剧本，优化"知识库"tab 中已有词条的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增任何词条
- 保持 id、position、enabled、constant、priority、scan_depth 不变
- 润色以下字段：content（精炼文字、提升信息密度）、comment（简化标签）
- 补充/改善 keys 和 secondary_keys 使关键词覆盖更全面
- 补充 related_entries（引用已有词条/NPC/地点/组织的ID，格式如 _kg_npc_xxx）
- 空字段根据上下文补全
- 参考已有 NPC/地点/组织设定确保知识库内容一致
- 如果所有内容都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的词条）：
{{{{
  "lorebook": [{{
    "id":"已有ID（不可改）",
    "keys":["优化后关键词"],
    "secondary_keys":["补充的次要关键词"],
    "content":"润色后内容(80-150字，第三人称客观视角)",
    "comment":"简化后标签",
    "related_entries":["关联ID"]
  }}]
}}}}
只返回JSON。""",
}

_SECTION_POLISH_PROMPTS = {
    "characters:player": """基于以下剧本，优化"玩家角色与预设"部分的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增预设角色
- 保持 ID、名称、attributes 数值、initial_location 不变
- 润色以下字段使文字更生动精炼：
  · player_character: bio、personality、portrait_desc、long_term_goal
  · opening: text（场景描述更身临其境）、choices 的 text 和 result.description
  · player_presets 的: bio、personality、portrait_desc、long_term_goal、opening_text、opening_choices 的文字
- 空字段根据世界背景、已有NPC、地点上下文补全
- 如果所有内容都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的字段）：
{{{{
  "player_character": {{
    "bio": "润色后(可选)", "personality": "(可选)", "portrait_desc": "(可选)", "long_term_goal": "(可选)"
  }},
  "opening": {{
    "text": "润色后的开局描述(可选)",
    "choices": [{{"id":"原ID","text":"润色后选项","result":{{"type":"原类型","description":"润色后描述"}}}}]
  }},
  "player_presets": [{{
    "id":"已有ID", "bio":"(可选)", "personality":"(可选)", "portrait_desc":"(可选)",
    "long_term_goal":"(可选)", "opening_text":"(可选)",
    "opening_choices":[{{"id":"原ID","text":"润色后","result":{{"type":"原类型","description":"润色后"}}}}]
  }}]
}}}}
只返回需要更新的字段。只返回JSON。""",

    "characters:npcs": """基于以下剧本，优化"NPC角色"部分的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增NPC，不要新增NPC关系
- 保持 id、name、attitude_toward_player、known、met、organizations 不变
- 润色以下字段：bio、personality、capabilities、title、portrait_desc、schedule 的 activity 描述
- 补全空的描述性字段（参考世界背景、已有地点ID、组织设定）
- default_location 和 superior 可补全（必须引用已有ID）
- related_lore 可补充（引用已有知识库词条ID）
- 如果NPC缺少对应的知识库词条（无 _kg_npc_{{id}}_intro），在 supplementary_lorebook 中补充
- 史实人物若缺少历史相关词条，必须补充
- 如果所有NPC都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的NPC）：
{{{{
  "npcs": [{{
    "id":"已有ID（不可改）",
    "bio":"润色后", "personality":"润色后", "capabilities":"润色后",
    "title":"润色后", "portrait_desc":"润色后",
    "default_location":"补全的地点ID(可选)",
    "superior":"补全的上级NPC ID(可选)",
    "schedule":[{{"time_range":"时段","location":"地点ID","activity":"润色后活动"}}],
    "related_lore":["补充的词条ID"]
  }}],
  "supplementary_lorebook": [为缺少知识库词条的NPC补充 {{
    "id":"_kg_npc_{{npc的id}}_intro",
    "keys":["NPC名字","别名"],
    "secondary_keys":["关联人名"],
    "content":"人物简介/历史事件(80-150字，第三人称客观百科视角)",
    "position":"after_world","priority":85,"constant":false,"scan_depth":3,
    "comment":"类型标签(人物简介/人物关系/历史事件)","related_entries":["关联ID"]
  }}]
}}}}
只返回JSON。""",

    "world:locations": """基于以下剧本，优化"地点"部分的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增地点
- 保持 id、name、initially_visible 不变
- 润色 description 使场景描述更生动具体，确保包含地理方位信息（所在城市/区域、与其他地标的相对位置）
- 补全/优化 connections（相邻地点ID列表，必须引用已有地点）
- 空字段根据世界背景上下文补全
- 如果所有地点都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的地点）：
{{{{
  "locations": [{{"id":"已有ID（不可改）","description":"润色后","connections":["优化后连接"]}}]
}}}}
只返回JSON。""",

    "world:properties_states": """基于以下剧本，优化"世界属性与持续状态"的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增世界属性或持续状态条目
- 保持 id、name、value、initially_active、expires_at 不变
- 润色以下字段：world_properties 的 rule 说明、persistent_states 的 description
- 空字段根据世界背景和已有事件上下文补全
- 如果所有内容都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的条目）：
{{{{
  "world_properties": [{{"id":"已有ID（不可改）","rule":"润色后的规则说明"}}],
  "persistent_states": [{{"id":"已有ID（不可改）","description":"润色后的状态效果描述"}}]
}}}}
只返回JSON。""",

    "organizations:organizations": """基于以下剧本，优化"组织/势力"部分的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增组织
- 保持 id、name、parent_org、leader 不变
- 润色以下字段：type、description、stance、aliases、hierarchy 的 title
- 在 supplementary_lorebook 中补充各层级(hierarchy)的职权边界描述（各rank能做什么、不能做什么）
- 空字段根据世界背景、已有NPC上下文补全
- 如果所有组织都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的组织）：
{{{{
  "organizations": [{{
    "id":"已有ID（不可改）","type":"润色后","description":"润色后","stance":"润色后",
    "aliases":["优化后别名"],"hierarchy":[{{"rank":原值,"title":"润色后职位"}}]
  }}]
}}}}
只返回JSON。""",

    "events:cyclic": """基于以下剧本，优化"周期事件"部分的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增周期事件
- 保持 id、frequency_value、frequency_unit、first_trigger、expires_at 不变
- 润色 name、description 使事件描述更生动，结合已有NPC、地点、组织丰富叙事
- 优化 condition 表达式使其更准确（引用的变量必须是已有属性/状态）
- 补全空的 name 字段（根据事件内容生成简短名称）
- 空字段根据上下文补全
- 如果所有周期事件都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的事件）：
{{{{
  "cyclic_events": [{{"id":"已有ID（不可改）","name":"润色后名称(可选)","description":"润色后","condition":"优化后(可选)","fire_events":["优化后的触发事件(可选)"],"activate_events":["优化后的激活事件(可选)"]}}]
}}}}
只返回JSON。""",

    "story_tree:trees": """基于以下剧本，优化"剧情树"中已有内容的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增剧情线或节点
- 保持所有 id、type、requires、on_complete_unlock、event、cooldown、weight、repeatable、activate_events、choices 的 id/unlock 不变
- 润色以下字段：剧情线的 name/description、节点的 name/description、choices 的 label/description
- 优化 condition 表达式使其更准确
- 优化 effects 中的 inject_prompt、notify、narrative_callback 文字使其更生动
- 如果所有内容都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的剧情线）：
{{{{
  "trees": [{{
    "id": "已有ID（不可改）",
    "name": "润色后名称",
    "description": "润色后描述",
    "nodes": [{{
      "id": "已有节点ID（不可改）",
      "name": "润色后",
      "description": "润色后",
      "condition": "优化后(可选)",
      "effects": {{
        "inject_prompt": "润色后(可选)",
        "notify": "润色后(可选)"
      }},
      "choices": [{{
        "id": "已有选项ID（不可改）",
        "label": "润色后",
        "description": "润色后"
      }}]
    }}]
  }}]
}}}}
只返回JSON。""",

    "events:one_time": """基于以下剧本，优化"一次性事件"部分的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增一次性事件
- 保持 id、trigger_time 不变
- 润色 name、description 使事件描述更生动，结合已有NPC、地点、组织、世界事件丰富叙事
- 优化 condition 表达式使其更准确（引用的变量必须是已有属性/状态）
- 补全空的 name 字段（根据事件内容生成简短名称）
- 空字段根据上下文补全
- 如果所有事件都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的事件）：
{{{{
  "one_time_events": [{{"id":"已有ID（不可改）","name":"润色后名称(可选)","description":"润色后","condition":"优化后(可选)","fire_events":["优化后的触发事件(可选)"],"activate_events":["优化后的激活事件(可选)"]}}]
}}}}
只返回JSON。""",

    "events:tone_rules": """基于以下剧本，优化"氛围基调规则"的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增基调规则
- 保持 id、priority 不变
- 润色 name、tone、narrative_style 使描述更具体生动
- 优化 condition 表达式使其更准确
- 如果所有规则都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的规则）：
{{{{
  "tone_rules": [{{"id":"已有ID（不可改）","name":"润色后","condition":"优化后","tone":"润色后","narrative_style":"润色后","priority":原值}}]
}}}}
只返回JSON。""",

    "events:quest_templates": """基于以下剧本，优化"支线任务模板"的描述性字段。

剧本概况：
{{script_summary}}

【优化规则】
- 不要新增支线模板
- 保持 id、cooldown_turns 不变
- 润色 name、description、trigger_hint、reward_hint 使描述更生动
- 优化 condition 表达式使其更准确
- 如果所有模板都已完善，返回空JSON: {{{{}}}}

返回JSON（只包含需要更新的模板）：
{{{{
  "quest_templates": [{{"id":"已有ID（不可改）","name":"润色后","description":"润色后","condition":"优化后","trigger_hint":"润色后","reward_hint":"润色后","cooldown_turns":原值}}]
}}}}
只返回JSON。""",
}

def _build_full_script_summary(
    script: dict,
    focus_tab: str | None = None,
    focus_section: str | None = None,
    entity_type: str | None = None,
) -> str:
    """Build a context-aware summary of the script for AI generation.

    focus_tab/focus_section control detail levels: fields relevant to the
    current generation target get fuller representation.

    entity_type: when set, skip sections irrelevant to that entity type
    to reduce token count for field-level generation.
    """
    focus = f"{focus_tab}:{focus_section}" if focus_section else (focus_tab or "")

    # Section relevance for entity-level generation
    _RELEVANCE: dict[str, set[str]] = {
        "npc":             {"basics", "player", "npcs", "npc_rels", "locations_brief", "orgs", "org_rels", "lorebook_brief", "opening"},
        "player":          {"basics", "player", "npcs_brief", "locations_brief", "orgs_brief", "opening"},
        "preset":          {"basics", "player", "npcs_brief", "locations_brief", "opening"},
        "location":        {"basics", "locations", "npcs_brief", "orgs_brief"},
        "organization":    {"basics", "npcs", "npc_rels", "locations_brief", "orgs", "org_rels", "lorebook_brief"},
        "one_time_event":  {"basics", "npcs_brief", "locations_brief", "orgs_brief", "events", "story_tree_brief"},
        "cyclic_event":    {"basics", "npcs_brief", "locations_brief", "events"},
        "random_item":     {"basics", "npcs_brief", "random_items", "variables"},
        "variable":        {"basics", "variables", "random_items"},
        "trigger":         {"basics", "variables", "triggers", "events_brief"},
        "regex_script":    {"basics"},
        "story_tree_node": {"basics", "npcs_brief", "locations_brief", "orgs_brief", "events", "story_tree"},
    }
    allowed = _RELEVANCE.get(entity_type) if entity_type else None

    def _inc(full_tag: str, brief_tag: str = "") -> str | None:
        """Return detail level for a section: 'full', 'brief', or None (skip).

        When allowed is None (no entity_type), always returns 'full'.
        """
        if allowed is None:
            return "full"
        if full_tag in allowed:
            return "full"
        if brief_tag and brief_tag in allowed:
            return "brief"
        return None

    def _t(text, default_len: int, full_focus: set | None = None) -> str:
        """Truncate helper. Returns full text when focus matches."""
        if not text:
            return ""
        s = str(text)
        if full_focus and focus in full_focus:
            return s
        return s[:default_len] + ("..." if len(s) > default_len else "")

    parts = []
    if script.get("script_name"):
        parts.append(f"【剧本】{script['script_name']}")
    if script.get("world_background"):
        parts.append(f"【世界背景】{_t(script['world_background'], 1200)}")
    if script.get("start_time"):
        parts.append(f"【开局时间】{script['start_time']}")

    # --- 主角 ---
    pc = script.get("player_character", {})
    pc_lines = []
    if pc.get("bio"):
        pc_lines.append(f"  简介: {_t(pc['bio'], 200)}")
    if pc.get("personality"):
        pc_lines.append(f"  性格: {pc['personality']}")
    if pc.get("long_term_goal") or pc.get("goal"):
        pc_lines.append(f"  目标: {_t(pc.get('long_term_goal') or pc.get('goal'), 150)}")
    if pc.get("portrait_desc"):
        pc_lines.append(f"  外貌: {_t(pc['portrait_desc'], 80)}")
    if pc.get("initial_location"):
        pc_lines.append(f"  初始位置: {pc['initial_location']}")
    inv = pc.get("initial_inventory", [])
    if inv:
        inv_str = ", ".join(f"{it.get('item','')}x{it.get('quantity',1)}" for it in inv)
        pc_lines.append(f"  初始物品: {inv_str}")
    if pc_lines:
        parts.append("【主角】\n" + "\n".join(pc_lines))

    # --- 主角属性（完整传递 rule/value/display_name）---
    pc_attrs = pc.get("attributes", {})
    if pc_attrs:
        attr_lines = []
        for key, attr in pc_attrs.items():
            if isinstance(attr, dict):
                dn = attr.get("display_name") or attr.get("name") or key
                val = attr.get("value", "?")
                rng = f"[{attr.get('min', 0)},{attr.get('max', 100)}]"
                rule = attr.get("rule", "")
                rule_str = f", rule={_t(rule, 80)}" if rule else ""
                attr_lines.append(f"  {dn}({key}): value={val}, range={rng}{rule_str}")
            else:
                attr_lines.append(f"  {key}: {attr}")
        parts.append("【主角属性】\n" + "\n".join(attr_lines))

    # --- 全局开局 ---
    if _inc("opening"):
        opening = script.get("opening", {})
        if opening.get("text"):
            opening_lines = [f"  {_t(opening['text'], 300)}"]
            choices = opening.get("choices", [])
            if choices:
                for i, c in enumerate(choices):
                    r = c.get("result", {})
                    opening_lines.append(f"  选项{i+1}: {c.get('text','')} → {_t(r.get('description',''), 60)}")
            parts.append("【全局开局】\n" + "\n".join(opening_lines))
        else:
            parts.append("【全局开局】[缺]")

    # --- NPC ---
    _npc_level = _inc("npcs", "npcs_brief")
    if _npc_level and script.get("npcs"):
        _npc_detail_focus = {"characters", "characters:npcs", "events", "events:cyclic",
                             "events:one_time", "organizations", "organizations:organizations", "lorebook"}
        show_npc_detail = _npc_level == "full" and focus in _npc_detail_focus
        if _npc_level == "brief":
            npc_lines = [f"  {n.get('name','?')}({n.get('id','?')})" for n in script["npcs"]]
        else:
            npc_lines = []
            for n in script["npcs"]:
                nid = n.get("id", "?")
                nname = n.get("name", "?")
                missing = []
                if not n.get("default_location") and not n.get("initial_location"):
                    missing.append("地点")
                if not n.get("personality"):
                    missing.append("性格")
                if not n.get("schedule"):
                    missing.append("日程")
                if not n.get("bio"):
                    missing.append("简介")
                if not n.get("portrait_desc"):
                    missing.append("外貌")
                tag = f"[缺:{','.join(missing)}]" if missing else "[完整]"
                org_tag = ""
                npc_orgs = n.get("organizations", [])
                if npc_orgs:
                    org_parts_list = []
                    for om in npc_orgs:
                        oid = om.get("org_id", "")
                        r = om.get("rank")
                        org_parts_list.append(f"{oid}(rank={r})" if r is not None else oid)
                    org_tag = " org=" + "/".join(org_parts_list)
                line = f"  {nname}({nid}){org_tag} {tag}"
                if show_npc_detail:
                    details = []
                    if n.get("bio"):
                        details.append(f"bio={_t(n['bio'], 100)}")
                    if n.get("personality"):
                        details.append(f"性格={_t(n['personality'], 60)}")
                    if n.get("capabilities"):
                        details.append(f"能力={_t(n['capabilities'], 60)}")
                    if n.get("title"):
                        details.append(f"头衔={n['title']}")
                    if n.get("default_location"):
                        details.append(f"位置={n['default_location']}")
                    if n.get("superior"):
                        details.append(f"上级={n['superior']}")
                    if details:
                        line += "\n    " + "; ".join(details)
                    sched = n.get("schedule", [])
                    if sched and focus in ("characters:npcs", "characters"):
                        sched_str = " | ".join(f"{s.get('time_range','')}: {s.get('location','')}-{s.get('activity','')}" for s in sched)
                        line += f"\n    日程: {sched_str}"
                    goals = n.get("goals", [])
                    if goals:
                        goals_str = "; ".join(f"{g.get('description','')}" + (f"[{g.get('type','')}]" if g.get('type') else "") for g in goals)
                        line += f"\n    目标: {goals_str}"
                npc_lines.append(line)
        parts.append(f"【NPC列表】({len(script['npcs'])}个)\n" + "\n".join(npc_lines))

    # --- NPC 关系 ---
    if _inc("npc_rels") and script.get("npc_relationships"):
        rel_lines = []
        for r in script["npc_relationships"]:
            a = r.get("from") or r.get("a", "?")
            b = r.get("to") or r.get("b", "?")
            rtype = r.get("type", "?")
            extras = []
            if r.get("trust") is not None:
                extras.append(f"trust={r['trust']}")
            if r.get("affection") is not None:
                extras.append(f"affection={r['affection']}")
            if r.get("fear") is not None:
                extras.append(f"fear={r['fear']}")
            desc = _t(r.get("description", ""), 60) if r.get("description") else ""
            extra_str = ", ".join(extras)
            line_parts = [f"{a}↔{b}: {rtype}"]
            if extra_str:
                line_parts.append(extra_str)
            if desc:
                line_parts.append(desc)
            rel_lines.append("  " + ", ".join(line_parts))
        parts.append(f"【NPC关系】({len(rel_lines)}条)\n" + "\n".join(rel_lines))

    # --- 地点 ---
    _loc_level = _inc("locations", "locations_brief")
    if _loc_level and script.get("locations"):
        if _loc_level == "brief":
            loc_lines = [f"  {l.get('name','?')}({l.get('id','?')})" for l in script["locations"]]
        else:
            loc_lines = []
            for l in script["locations"]:
                lid = l.get("id", "?")
                lname = l.get("name", "?")
                vis = "" if l.get("initially_visible", True) else " [隐藏]"
                desc = _t(l.get("description", ""), 120, {"world", "world:locations", "lorebook"})
                conns = ",".join(l.get("connections", [])) if l.get("connections") else "[缺连接]"
                loc_lines.append(f"  {lname}({lid}){vis} conn=[{conns}] {desc}")
        parts.append(f"【地点】({len(loc_lines)}个)\n" + "\n".join(loc_lines))

    # --- 组织/势力 ---
    _org_level = _inc("orgs", "orgs_brief")
    if _org_level and script.get("organizations"):
        if _org_level == "brief":
            org_lines = [f"  {o.get('name','?')}({o.get('id','?')}, {o.get('type','')})" for o in script["organizations"]]
        else:
            org_lines = []
            for o in script["organizations"]:
                oid = o.get("id", "?")
                oname = o.get("name", "?")
                otype = o.get("type", "")
                desc = _t(o.get("description", ""), 100, {"organizations", "organizations:organizations", "lorebook"})
                leader = f" leader={o['leader']}" if o.get("leader") else ""
                parent = f" parent={o['parent_org']}" if o.get("parent_org") else ""
                stance = f" stance={o['stance']}" if o.get("stance") else ""
                rep = f" reputation={o['initial_reputation']}" if o.get("initial_reputation") is not None else ""
                aliases = f" aliases={','.join(o['aliases'])}" if o.get("aliases") else ""
                hierarchy = o.get("hierarchy", [])
                if hierarchy:
                    ranks = "→".join(
                        f"{h['title']}(rank={h.get('rank', '')})"
                        for h in sorted(hierarchy, key=lambda h: h.get("rank", 99))
                    )
                    hier_str = f" 层级:{ranks}"
                else:
                    hier_str = " [缺:层级]"
                org_lines.append(f"  {oname}({oid}, {otype}){leader}{parent}{stance}{rep}{aliases}{hier_str} {desc}")
                org_goals = o.get("goals", [])
                if org_goals:
                    goals_str = "; ".join(g.get("description", g.get("id", "")) for g in org_goals[:3])
                    org_lines.append(f"    目标: {goals_str}")
        parts.append(f"【组织/势力】({len(org_lines)}个)\n" + "\n".join(org_lines))

    # --- 组织关系 ---
    if _inc("org_rels") and script.get("org_relationships"):
        orel_lines = []
        for r in script["org_relationships"]:
            a = r.get("a", "?")
            b = r.get("b", "?")
            rtype = r.get("type", "?")
            desc = _t(r.get("description", ""), 60)
            orel_lines.append(f"  {a}↔{b}: {rtype} {desc}")
        parts.append(f"【组织关系】\n" + "\n".join(orel_lines))

    # --- 世界属性 ---
    if _inc("world_props") and script.get("world_properties"):
        wp_lines = []
        for p in script["world_properties"]:
            pid = p.get("id", "?")
            pname = p.get("name", pid)
            ptype = f" type={p['type']}" if p.get("type") else ""
            pval = f" value={p['value']}" if p.get("value") is not None else ""
            rule = f" rule={_t(p['rule'], 80)}" if p.get("rule") else ""
            wp_lines.append(f"  {pname}({pid}){ptype}{pval}{rule}")
        parts.append("【世界属性】\n" + "\n".join(wp_lines))

    # --- 持续状态 ---
    if _inc("persistent_states") and script.get("persistent_states"):
        ps_lines = []
        for p in script["persistent_states"]:
            pid = p.get("id", "?")
            pname = p.get("name", pid)
            desc = _t(p.get("description", ""), 80)
            active = "活跃" if p.get("initially_active") else "未激活"
            exp = f" expires={p['expires_at']}" if p.get("expires_at") else ""
            ps_lines.append(f"  {pname}({pid}) [{active}]{exp} {desc}")
        parts.append("【持续状态】\n" + "\n".join(ps_lines))

    # --- 周期事件 ---
    _evt_level = _inc("events", "events_brief")
    if _evt_level and script.get("cyclic_events"):
        ce_lines = []
        for e in script["cyclic_events"]:
            eid = e.get("id", "?")
            desc = _t(e.get("description", ""), 100)
            freq = f"{e.get('frequency_value', '?')}{e.get('frequency_unit', 'day')}"
            trigger = f" first={e['first_trigger']}" if e.get("first_trigger") else ""
            cond = f" cond={e['condition']}" if e.get("condition") else ""
            exp = f" expires={e['expires_at']}" if e.get("expires_at") else ""
            fe = f" fire_events={','.join(e['fire_events'])}" if e.get("fire_events") else ""
            ae = f" activate_events={','.join(e['activate_events'])}" if e.get("activate_events") else ""
            ce_lines.append(f"  {eid}: {desc} freq={freq}{trigger}{cond}{exp}{fe}{ae}")
        parts.append(f"【周期事件】({len(ce_lines)}个)\n" + "\n".join(ce_lines))

    # --- 一次性事件 ---
    if _evt_level and script.get("one_time_events"):
        oe_lines = []
        for e in script["one_time_events"]:
            eid = e.get("id", "?")
            desc = _t(e.get("description", ""), 120)
            trigger = f" trigger_time={e['trigger_time']}" if e.get("trigger_time") else ""
            cond = f" cond={e['condition']}" if e.get("condition") else ""
            fe = f" fire_events={','.join(e['fire_events'])}" if e.get("fire_events") else ""
            ae = f" activate_events={','.join(e['activate_events'])}" if e.get("activate_events") else ""
            oe_lines.append(f"  {eid}: {desc}{trigger}{cond}{fe}{ae}")
        parts.append(f"【一次性事件】({len(oe_lines)}个)\n" + "\n".join(oe_lines))

    # --- 氛围基调规则 ---
    if _inc("tone_rules") and script.get("tone_rules"):
        tr_lines = []
        for r in script["tone_rules"]:
            rid = r.get("id", "?")
            rname = r.get("name", rid)
            rcond = r.get("condition", "")
            rtone = _t(r.get("tone", ""), 60)
            rprio = r.get("priority", 0)
            tr_lines.append(f"  {rid}({rname}): cond={rcond} tone={rtone} prio={rprio}")
        parts.append(f"【氛围基调规则】({len(tr_lines)}个)\n" + "\n".join(tr_lines))

    # --- 支线任务模板 ---
    if _inc("quest_templates") and script.get("quest_templates"):
        qt_lines = []
        for qt in script["quest_templates"]:
            qtid = qt.get("id", "?")
            qtname = qt.get("name", qtid)
            qtcond = qt.get("condition", "")
            qtcd = qt.get("cooldown_turns", 10)
            qt_lines.append(f"  {qtid}({qtname}): cond={qtcond} cd={qtcd}")
        parts.append(f"【支线模板】({len(qt_lines)}个)\n" + "\n".join(qt_lines))

    # --- 脚本变量 ---
    if _inc("variables") and script.get("variables"):
        var_lines = []
        for v in script["variables"]:
            vid = v.get("id", "?")
            vname = v.get("name", vid)
            vtype = v.get("type", "number")
            vdefault = v.get("default", 0)
            vrange = ""
            if vtype == "number":
                vmin = v.get("min", "")
                vmax = v.get("max", "")
                if vmin != "" or vmax != "":
                    vrange = f" range=[{vmin},{vmax}]"
            var_lines.append(f"  {vname}({vid}) type={vtype} default={vdefault}{vrange}")
        parts.append(f"【脚本变量】({len(var_lines)}个)\n" + "\n".join(var_lines))

    # --- 随机项 ---
    if _inc("random_items") and script.get("random_items"):
        ri_lines = []
        for r in script["random_items"]:
            rid = r.get("id", "?")
            desc = _t(r.get("description", ""), 60)
            trigger = r.get("trigger", "")
            ttype = r.get("trigger_type", "")
            linked = f" linked={r['linked_event_id']}" if r.get("linked_event_id") else ""
            n_ranges = len(r.get("ranges", []))
            ri_lines.append(f"  {rid}: {desc} trigger={trigger}({ttype}) ranges={n_ranges}{linked}")
        parts.append(f"【随机项】({len(ri_lines)}个)\n" + "\n".join(ri_lines))

    # --- 知识库（按 priority 排序，数量由 focus 决定）---
    _lore_level = _inc("lorebook", "lorebook_brief")
    if _lore_level and script.get("lorebook"):
        lore_items = sorted(script["lorebook"], key=lambda e: e.get("priority", 100), reverse=True)
        if _lore_level == "brief":
            max_lore = 10
            lore_lines = [f"  {e.get('id','?')} [keys:{','.join(e.get('keys',[]))}]" for e in lore_items[:max_lore]]
        else:
            max_lore = 20 if focus_tab == "lorebook" else 15
            content_len = 300 if focus_tab == "lorebook" else 120
            lore_lines = []
            for e in lore_items[:max_lore]:
                eid = e.get("id", "?")
                keys = ",".join(e.get("keys", []))
                prio = e.get("priority", 100)
                content = _t(e.get("content", ""), content_len)
                lore_lines.append(f"  {eid} [keys:{keys}] (priority={prio}) {content}")
        remaining = len(lore_items) - max_lore
        if remaining > 0:
            lore_lines.append(f"  ...及另外{remaining}条")
        parts.append(f"【知识库】({len(lore_items)}条)\n" + "\n".join(lore_lines))

    # --- 预设角色 ---
    if _inc("presets") and script.get("player_presets"):
        preset_lines = []
        for p in script["player_presets"]:
            pid = p.get("id", "?")
            pname = p.get("name", "?")
            opening_tag = "[有专属开局]" if p.get("opening_text") else "[缺专属开局]"
            bio = _t(p.get("bio", ""), 100)
            pers = p.get("personality", "")
            loc = f" 位置={p['initial_location']}" if p.get("initial_location") else ""
            attrs = p.get("attributes", {})
            attr_str = ""
            if attrs:
                attr_keys = list(attrs.keys())[:5]
                attr_parts_list = []
                for ak in attr_keys:
                    av = attrs[ak]
                    val = av.get("value", av) if isinstance(av, dict) else av
                    attr_parts_list.append(f"{ak}={val}")
                attr_str = f" 属性=[{','.join(attr_parts_list)}]"
            preset_lines.append(f"  {pname}({pid}) {opening_tag}{loc}{attr_str} {bio} {pers}")
        parts.append(f"【预设角色】({len(preset_lines)}个)\n" + "\n".join(preset_lines))

    # --- 剧情树 ---
    _st_level = _inc("story_tree", "story_tree_brief")
    story_tree = script.get("story_tree", {})
    st_trees = story_tree.get("trees", [])
    if _st_level and st_trees:
        st_lines = []
        for tree in st_trees:
            tid = tree.get("id", "?")
            tname = tree.get("name", "?")
            nodes = tree.get("nodes", [])
            node_count = len(nodes)
            node_types = {}
            for n in nodes:
                ntype = n.get("type", "auto")
                node_types[ntype] = node_types.get(ntype, 0) + 1
            type_str = ", ".join(f"{k}:{v}" for k, v in node_types.items())
            st_lines.append(f"  {tname}({tid}) 节点数:{node_count} [{type_str}]")
            if focus_tab == "story_tree":
                for n in nodes:
                    nid = n.get("id", "?")
                    nname = n.get("name", "?")
                    ntype = n.get("type", "auto")
                    req = ",".join(n.get("requires", []))
                    cond = n.get("condition", "")
                    unlock = ",".join(n.get("on_complete_unlock", []))
                    desc = _t(n.get("description", ""), 80)
                    extra = ""
                    if ntype == "trigger":
                        evts = n.get("event", [])
                        if isinstance(evts, str): evts = [evts]
                        extra = f" event=[{','.join(evts)}]"
                    elif ntype == "periodic":
                        extra = f" cooldown={n.get('cooldown', 5)} weight={n.get('weight', 10)} repeatable={n.get('repeatable', True)}"
                    act_evts = n.get("activate_events", [])
                    act_str = f" activate_events=[{','.join(act_evts)}]" if act_evts else ""
                    fire_evts = n.get("effects", {}).get("fire_events", [])
                    fire_str = f" fire_events=[{','.join(fire_evts)}]" if fire_evts else ""
                    rel_npcs = n.get("related_npcs", [])
                    rel_orgs = n.get("related_orgs", [])
                    rel_str = ""
                    if rel_npcs:
                        rel_str += f" npcs=[{','.join(rel_npcs)}]"
                    if rel_orgs:
                        rel_str += f" orgs=[{','.join(rel_orgs)}]"
                    st_lines.append(f"    {nname}({nid}) type={ntype} req=[{req}] cond={cond} unlock=[{unlock}]{extra}{act_str}{fire_str}{rel_str} {desc}")
                    if ntype == "choice" and n.get("choices"):
                        for c in n["choices"]:
                            cid = c.get("id", "?")
                            clabel = c.get("label", "?")
                            cdesc = _t(c.get("description", ""), 60)
                            cunlock = ",".join(c.get("unlock", []))
                            st_lines.append(f"      -> {clabel}({cid}) unlock=[{cunlock}] {cdesc}")
        parts.append(f"【剧情树】({len(st_trees)}条线)\n" + "\n".join(st_lines))

    summary = "\n".join(parts)
    logger.debug("Script summary length: %d chars (focus=%s)", len(summary), focus or "none")
    return summary


def _count_existing_items(script: dict, section_key: str) -> int:
    """Count existing items in a script for a given section key."""
    _mapping = {
        "characters:player": lambda s: len(s.get("player_presets", [])),
        "characters:npcs": lambda s: len(s.get("npcs", [])),
        "world:locations": lambda s: len(s.get("locations", [])),
        "world:properties_states": lambda s: len(s.get("world_properties", [])) + len(s.get("persistent_states", [])),
        "organizations:organizations": lambda s: len(s.get("organizations", [])),
        "events:cyclic": lambda s: len(s.get("cyclic_events", [])),
        "events:one_time": lambda s: len(s.get("one_time_events", [])),
        "events:tone_rules": lambda s: len(s.get("tone_rules", [])),
        "events:quest_templates": lambda s: len(s.get("quest_templates", [])),
        "dice": lambda s: len(s.get("random_items", [])) + len(s.get("variables", [])) + len(s.get("triggers", [])) + len(s.get("regex_scripts", [])),
        "lorebook": lambda s: len(s.get("lorebook", [])),
        "story_tree:trees": lambda s: len(s.get("story_tree", {}).get("trees", [])),
    }
    counter = _mapping.get(section_key)
    return counter(script) if counter else 0


def _get_quantity_guidance(existing_count: int) -> str:
    """Return Chinese quantity guidance string based on existing item count."""
    if existing_count == 0:
        return "当前该类内容为空，请生成5-8项，确保内容丰富但不过多。"
    if existing_count <= 3:
        return f"已有{existing_count}项，请补充3-5项，与已有内容互补。"
    if existing_count <= 7:
        return f"已有{existing_count}项，请补充2-3项关键内容即可。"
    return f"已有{existing_count}项，内容已较充分，仅补充1-2项最关键的缺失内容，或返回空JSON {{}}。"


class TabGenerateRequest(BaseModel):
    tab: str  # characters / world / organizations / events / dice / lorebook
    script: dict  # current script content for context
    section: Optional[str] = None  # for multi-section tabs (e.g. characters: player/npcs)
    user_hint: Optional[str] = None  # S1: 用户补充需求提示
    mode: str = "fill"  # "fill" = 新建补充, "polish" = 优化已有


@router.post("/ai-generate/tab")
async def ai_generate_tab(req: TabGenerateRequest):
    """Generate content for a specific wizard tab (or section of a tab)."""
    tab_prompts = _TAB_PROMPTS if req.mode != "polish" else _TAB_POLISH_PROMPTS
    section_prompts = _SECTION_PROMPTS if req.mode != "polish" else _SECTION_POLISH_PROMPTS

    if req.tab not in tab_prompts:
        raise HTTPException(status_code=400, detail=f"未知tab: {req.tab}. 支持: {list(tab_prompts.keys())}")

    # Resolve prompt: section prompt if provided, otherwise full tab prompt
    if req.section:
        prompt_key = f"{req.tab}:{req.section}"
        if prompt_key not in section_prompts:
            raise HTTPException(status_code=400, detail=f"未知section: {prompt_key}")
        template = section_prompts[prompt_key]
    else:
        template = tab_prompts.get(req.tab)
        if template is None:
            sections = _TAB_SECTIONS.get(req.tab, [])
            raise HTTPException(
                status_code=400,
                detail=f"此tab需要分段生成，请指定section参数。可用sections: {sections}",
            )

    provider = await _get_ai_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")

    mode_label = "优化" if req.mode == "polish" else "生成"
    section_label = f":{req.section}" if req.section else ""
    logger.info("Tab%s — tab=%s%s, script=%s", mode_label, req.tab, section_label, req.script.get("script_name", "?")[:40])

    script_summary = _build_full_script_summary(req.script, focus_tab=req.tab, focus_section=req.section)
    if not script_summary.strip():
        raise HTTPException(status_code=400, detail="请先填写基本信息（剧本名称/世界背景）")

    user_prompt = template.format(script_summary=script_summary)
    # D1: 动态数量控制 — 仅 fill 模式
    if req.mode != "polish":
        section_key = f"{req.tab}:{req.section}" if req.section else req.tab
        existing = _count_existing_items(req.script, section_key)
        guidance = _get_quantity_guidance(existing)
        user_prompt += f"\n\n【数量控制】{guidance}"
    # S1: 注入用户补充需求
    if req.user_hint and req.user_hint.strip():
        user_prompt += f"\n\n【用户补充需求】{req.user_hint.strip()}"
    system_prompt = _SCRIPT_AI_SYSTEM_PROMPT

    try:
        response = await provider.generate(
            [{"role": "user", "content": user_prompt}],
            system=system_prompt,
            max_tokens=8192,
        )
        response = _strip_code_fences(response)
        parsed = _robust_json_extract(response)

        if not parsed:
            # AI returned empty JSON {} — content is already sufficient
            response_stripped = response.strip()
            if response_stripped in ('{}', '{ }'):
                logger.info("Tab生成 — 内容已充分, tab=%s%s", req.tab, section_label)
                return {"tab": req.tab, "generated": {}, "complete": True}
            # 尝试从截断的 JSON 中尽量提取已完成的条目
            partial = _salvage_truncated_json(response)
            if partial:
                logger.warning("Tab生成JSON被截断，已提取部分结果 — tab=%s%s", req.tab, section_label)
                return {"tab": req.tab, "generated": partial, "warnings": ["AI输出被截断，仅返回部分结果。可再次生成补充。"]}
            logger.error("Tab生成JSON解析失败 — tab=%s%s, response前200字: %s", req.tab, section_label, response[:200])
            raise HTTPException(
                status_code=500,
                detail=f"AI返回内容无法解析为JSON。原文前200字: {response[:200]}"
            )

        # Check if AI returned all-empty content (e.g. {"npcs": [], "npc_relationships": []})
        all_empty = all(
            (isinstance(v, (list, dict)) and len(v) == 0)
            for v in parsed.values()
        )
        if all_empty:
            logger.info("Tab生成 — 内容已充分, tab=%s%s", req.tab, section_label)
            return {"tab": req.tab, "generated": {}, "complete": True}

        # Script#3: 交叉验证生成的内容
        warnings = _cross_validate_generated(req.tab, parsed, req.script)

        logger.info("Tab生成完成 — tab=%s%s, 字段: %s", req.tab, section_label, ", ".join(parsed.keys()))
        result = {"tab": req.tab, "generated": parsed}
        if warnings:
            result["warnings"] = warnings
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Tab生成失败 — tab=%s, error=%s", req.tab, e)
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")


def _cross_validate_generated(tab: str, generated: dict, script: dict) -> list[str]:
    """Script#3: 交叉验证生成的内容与已有剧本的一致性。

    当 AI 用名称而非 ID 引用跨 tab 实体时，自动修正为 ID。
    """
    warnings = []

    # 收集已有的 ID 集合
    existing_location_ids = {loc.get("id") for loc in script.get("locations", [])}
    existing_npc_ids = {npc.get("id") for npc in script.get("npcs", [])}
    existing_event_ids = (
        {e.get("id") for e in script.get("cyclic_events", [])}
        | {e.get("id") for e in script.get("one_time_events", [])}
    )

    # 构建 name→ID 映射，用于自动修正名称引用
    loc_name_to_id = {}
    for loc in list(script.get("locations", [])) + list(generated.get("locations", [])):
        name, lid = loc.get("name", ""), loc.get("id", "")
        if name and lid:
            loc_name_to_id[name] = lid

    npc_name_to_id = {}
    for npc in list(script.get("npcs", [])) + list(generated.get("npcs", [])):
        name, nid = npc.get("name", ""), npc.get("id", "")
        if name and nid:
            npc_name_to_id[name] = nid

    org_name_to_id = {}
    for org in list(script.get("organizations", [])) + list(generated.get("organizations", [])):
        name, oid = org.get("name", ""), org.get("id", "")
        if name and oid:
            org_name_to_id[name] = oid

    # 合并新旧 ID 集合
    new_location_ids = {loc.get("id") for loc in generated.get("locations", [])}
    all_location_ids = existing_location_ids | new_location_ids

    existing_org_ids = {o.get("id") for o in script.get("organizations", [])}
    new_org_ids = {o.get("id") for o in generated.get("organizations", [])}
    all_org_ids = existing_org_ids | new_org_ids

    new_npc_ids = {npc.get("id") for npc in generated.get("npcs", [])}
    all_npc_ids = existing_npc_ids | new_npc_ids

    _org_hierarchy_ranks: dict[str, set] = {}
    for o in list(script.get("organizations", [])) + list(generated.get("organizations", [])):
        oid = o.get("id", "")
        hierarchy = o.get("hierarchy", [])
        if hierarchy:
            _org_hierarchy_ranks[oid] = {h["rank"] for h in hierarchy if "rank" in h}

    # --- 自动修正 + 验证 ---

    # 修正 player_character / player_presets 的 initial_location
    pc = generated.get("player_character", {})
    if pc:
        iloc = pc.get("initial_location", "")
        if iloc and iloc not in all_location_ids:
            resolved = loc_name_to_id.get(iloc)
            if resolved:
                pc["initial_location"] = resolved
    for preset in generated.get("player_presets", []):
        iloc = preset.get("initial_location", "")
        if iloc and iloc not in all_location_ids:
            resolved = loc_name_to_id.get(iloc)
            if resolved:
                preset["initial_location"] = resolved

    # 验证/修正 NPC 引用的地点、组织、上级
    for npc in generated.get("npcs", []):
        npc_label = npc.get("name", npc.get("id", "?"))
        loc = npc.get("default_location", "")
        if loc and loc not in all_location_ids:
            resolved = loc_name_to_id.get(loc)
            if resolved:
                npc["default_location"] = resolved
            else:
                warnings.append(f"NPC '{npc_label}' 引用了不存在的地点 '{loc}'")
        for sched in npc.get("schedule", []):
            sloc = sched.get("location", "")
            if sloc and sloc not in all_location_ids:
                resolved = loc_name_to_id.get(sloc)
                if resolved:
                    sched["location"] = resolved
                else:
                    warnings.append(f"NPC '{npc_label}' 的日程引用了不存在的地点 '{sloc}'")
        sup = npc.get("superior", "")
        if sup and sup not in all_npc_ids:
            resolved = npc_name_to_id.get(sup)
            if resolved:
                npc["superior"] = resolved
            else:
                warnings.append(f"NPC '{npc_label}' 的上级 '{sup}' 不在NPC列表中")
        # 兼容: AI 返回旧格式 organization/rank → organizations 数组
        if npc.get("organization") and not npc.get("organizations"):
            org_id = npc.pop("organization", "")
            rank = npc.pop("rank", None)
            if org_id:
                entry = {"org_id": org_id}
                if rank is not None:
                    entry["rank"] = rank
                npc["organizations"] = [entry]
        # 验证/修正 organizations 数组
        for om in npc.get("organizations", []):
            oid = om.get("org_id", "")
            if oid and oid not in all_org_ids:
                resolved = org_name_to_id.get(oid)
                if resolved:
                    om["org_id"] = resolved
                    oid = resolved
                else:
                    warnings.append(f"NPC '{npc_label}' 的组织 '{oid}' 不在组织列表中")
            om_rank = om.get("rank")
            if om_rank is not None and oid:
                valid_ranks = _org_hierarchy_ranks.get(oid)
                if valid_ranks is not None and om_rank not in valid_ranks:
                    warnings.append(f"NPC '{npc_label}' 的 rank {om_rank} 不在组织 '{oid}' 的 hierarchy 中")

    # 验证/修正组织 leader 和 parent_org
    for org_item in generated.get("organizations", []):
        leader = org_item.get("leader", "")
        if leader and leader not in all_npc_ids:
            resolved = npc_name_to_id.get(leader)
            if resolved:
                org_item["leader"] = resolved
            else:
                warnings.append(f"组织 '{org_item.get('name', '?')}' 的领导者 '{leader}' 不在NPC列表中")
        parent = org_item.get("parent_org", "")
        if parent and parent not in all_org_ids:
            resolved = org_name_to_id.get(parent)
            if resolved:
                org_item["parent_org"] = resolved
            else:
                warnings.append(f"组织 '{org_item.get('name', '?')}' 的 parent_org '{parent}' 不在组织列表中")

    # 验证/修正 org_relationships 引用
    for rel in generated.get("org_relationships", []):
        for key in ("a", "b"):
            ref = rel.get(key, "")
            if ref and ref not in all_org_ids:
                resolved = org_name_to_id.get(ref)
                if resolved:
                    rel[key] = resolved
                else:
                    warnings.append(f"组织关系引用了不存在的组织/势力 '{ref}'")

    # 验证/修正 NPC 关系引用
    for rel in generated.get("npc_relationships", []):
        for key in ("a", "b", "from", "to"):
            ref = rel.get(key, "")
            if ref and ref not in all_npc_ids:
                resolved = npc_name_to_id.get(ref)
                if resolved:
                    rel[key] = resolved
                else:
                    warnings.append(f"NPC关系引用了不存在的NPC '{ref}'")

    # 验证事件 ID 不与现有重复
    for event in (
        list(generated.get("cyclic_events", []))
        + list(generated.get("one_time_events", []))
    ):
        eid = event.get("id", "?")
        if eid in existing_event_ids:
            warnings.append(f"事件ID '{eid}' 与已有事件重复")

    # 验证随机事件链接的事件 ID
    for ri in generated.get("random_items", []):
        linked = ri.get("linked_event_id", "")
        if linked and linked not in existing_event_ids:
            new_event_ids = (
                {e.get("id") for e in generated.get("cyclic_events", [])}
                | {e.get("id") for e in generated.get("one_time_events", [])}
            )
            if linked not in new_event_ids:
                warnings.append(f"随机项 '{ri.get('id', '?')}' 链接了不存在的事件 '{linked}'")

    # 自动将 supplementary_lorebook 中的 NPC 词条关联到对应 NPC 的 related_lore
    supp_lore = generated.get("supplementary_lorebook", [])
    if supp_lore:
        npc_lore_map: dict[str, list[str]] = {}
        for lore in supp_lore:
            lid = lore.get("id", "")
            if lid.startswith("_kg_npc_"):
                # _kg_npc_{npc_id}_{type} → 提取 npc_id
                rest = lid[len("_kg_npc_"):]
                sep = rest.rfind("_")
                npc_id = rest[:sep] if sep > 0 else rest
                if npc_id:
                    npc_lore_map.setdefault(npc_id, []).append(lid)
        for npc in generated.get("npcs", []):
            nid = npc.get("id", "")
            auto_lore = npc_lore_map.get(nid, [])
            if auto_lore:
                existing_lore = set(npc.get("related_lore", []))
                npc.setdefault("related_lore", []).extend(
                    lid for lid in auto_lore if lid not in existing_lore
                )

    return warnings


def _salvage_truncated_json(text: str) -> dict | None:
    """从截断的 JSON 中提取尽可能多的已完成条目。

    当 _robust_json_extract 的 Strategy 5 失败时（例如截断在转义序列或
    Unicode 字符中间），逐步回退截断位置直到找到可解析的 JSON。
    """
    # 先去掉 code fence 前缀
    stripped = text.strip()
    if stripped.startswith('```'):
        first_nl = stripped.find('\n')
        if first_nl != -1:
            stripped = stripped[first_nl + 1:]
    # 找到第一个 {
    start = stripped.find('{')
    if start == -1:
        return None
    content = stripped[start:]
    for trim in range(0, min(500, len(content) // 2), 10):
        candidate = content[:len(content) - trim] if trim > 0 else content
        for end_char in ('}', ']', '"', ','):
            pos = candidate.rfind(end_char)
            if pos > 0:
                attempt = _repair_truncated_json(candidate[:pos + 1])
                try:
                    data = json.loads(attempt)
                    if isinstance(data, dict) and any(
                        isinstance(v, list) and len(v) > 0 for v in data.values()
                    ):
                        return data
                except json.JSONDecodeError:
                    continue
    return None


def _repair_truncated_json(text: str) -> str:
    """闭合截断的 JSON 字符串：补齐未闭合的引号和括号，清理尾部逗号。"""
    text = re.sub(r',\s*$', '', text)
    in_str = False
    stack = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == '\\' and in_str:
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
            if c == '{':
                stack.append('}')
            elif c == '[':
                stack.append(']')
            elif c in ('}', ']'):
                if stack and stack[-1] == c:
                    stack.pop()
        i += 1
    if in_str:
        text += '"'
    text = re.sub(r',\s*"[^"]*"\s*:\s*$', '', text)
    text = re.sub(r',\s*"[^"]*$', '', text)
    text = re.sub(r',\s*$', '', text)
    while stack:
        text += stack.pop()
    return re.sub(r',\s*([\]}])', r'\1', text)


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from AI output."""
    stripped = text.strip()
    # Match ```json or ``` at start, ``` at end
    m = re.match(r'^```(?:json|JSON)?\s*\n?([\s\S]*?)\n?\s*```\s*$', stripped)
    if m:
        return m.group(1).strip()
    return stripped


def _robust_json_extract(text: str) -> dict | None:
    """Try multiple strategies to extract a JSON object from AI text."""
    # Strip <thinking>/<think> blocks (some models emit chain-of-thought)
    text = re.sub(r'</?(?:thinking|think)>[\s\S]*?</?(?:thinking|think)>', '', text)
    text = re.sub(r'<(?:thinking|think)>[\s\S]*$', '', text)  # unclosed tag at end
    text = text.strip()

    # Strategy 1: direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip code fences that weren't caught
    stripped = _strip_code_fences(text)
    if stripped != text:
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # Strategy 3: find outermost { ... } using brace balancing
    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if depth == 0 and end > start:
        candidate = text[start:end]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        # Clean trailing commas
        cleaned = re.sub(r',\s*([\]}])', r'\1', candidate)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # Strategy 4: greedy regex fallback (original approach)
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            cleaned = re.sub(r',\s*([\]}])', r'\1', json_match.group())
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

    # Strategy 5: truncated JSON repair — close unclosed brackets/braces
    if start != -1 and depth > 0:
        cleaned = _repair_truncated_json(text[start:].rstrip())
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
#  Field-level AI generation for individual entities
# ---------------------------------------------------------------------------

_ENTITY_FIELD_SCHEMA: dict[str, dict[str, dict]] = {
    "npc": {
        "bio":              {"label": "简介", "hint": "角色身份和背景，50-100字"},
        "personality":      {"label": "性格", "hint": "性格特点，用逗号分隔几个关键词或一句话"},
        "capabilities":     {"label": "能力/作用", "hint": "角色的技能或在故事中的作用"},
        "title":            {"label": "头衔/职位", "hint": "社会身份或头衔"},
        "default_location": {"label": "默认地点", "hint": "角色常驻地点的 ID（必须是已有地点）"},
        "portrait_desc":    {"label": "外貌描述", "hint": "简短外貌特征描写，30-60字"},
        "schedule":         {"label": "日程表", "hint": "返回数组 [{\"time_range\":\"07:00-12:00\",\"location\":\"地点ID\",\"activity\":\"活动\"}]，至少3个时段覆盖全天", "format": "object[]"},
        "goals":            {"label": "目标", "hint": "返回数组 [{\"id\":\"目标ID\",\"description\":\"描述\",\"type\":\"long_term|short_term\",\"priority\":\"high|medium|low\",\"progress_hint\":\"推进方式\",\"conflict_with_player\":\"与玩家潜在冲突\"}]", "format": "object[]"},
        "superior":         {"label": "上级NPC", "hint": "上级NPC的ID（必须是已有NPC）"},
        "related_lore":     {"label": "关联知识库", "hint": "返回关联知识库词条ID数组", "format": "string[]"},
    },
    "player": {
        "bio":              {"label": "简介", "hint": "主角的身份和背景"},
        "personality":      {"label": "性格", "hint": "性格特点，用逗号分隔几个关键词或一句话"},
        "portrait_desc":    {"label": "外貌描述", "hint": "简短外貌特征描写，30-60字"},
        "long_term_goal":   {"label": "长期目标", "hint": "主角的终极目标"},
        "initial_location": {"label": "初始位置", "hint": "主角开局所在地点 ID"},
    },
    "preset": {
        "bio":              {"label": "简介", "hint": "预设角色的身份和背景"},
        "personality":      {"label": "性格", "hint": "性格特点"},
        "initial_location": {"label": "初始位置", "hint": "开局位置的地点 ID"},
        "long_term_goal":   {"label": "长期目标", "hint": "角色的终极目标"},
        "portrait_desc":    {"label": "外貌描述", "hint": "简短外貌描写，30-60字"},
        "opening_text":     {"label": "专属开局", "hint": "200-400字的开局叙事，用第二人称"},
        "opening_choices":  {"label": "专属开局选项", "hint": "返回2-4个选项数组 [{\"text\":\"选项描述\",\"result\":{\"description\":\"结果方向\"}}]", "format": "object[]"},
    },
    "location": {
        "description":  {"label": "描述", "hint": "地点的环境描述，50-100字"},
        "connections":  {"label": "连接地点", "hint": "返回相邻地点 ID 数组（必须是已有地点）", "format": "string[]"},
        "initially_visible": {"label": "初始可见", "hint": "是否初始可见(true/false)", "format": "boolean"},
    },
    "organization": {
        "type":        {"label": "类型", "hint": "组织类型（如：帮派、公司、军队、国家、学院等）"},
        "description": {"label": "描述", "hint": "组织的背景和目的，80-150字"},
        "parent_org":  {"label": "父组织", "hint": "父组织的ID（必须是已有组织，可选）"},
        "leader":      {"label": "领导者", "hint": "领导者NPC的ID（必须是已有NPC）"},
        "stance":      {"label": "对主角立场", "hint": "该组织对主角的初始态度，如友好/中立/敌对"},
        "aliases":     {"label": "别名", "hint": "返回字符串数组", "format": "string[]"},
        "hierarchy":   {"label": "层级架构", "hint": "返回数组 [{\"rank\":1,\"title\":\"最高职位\"},{\"rank\":2,\"title\":\"次级职位\"},{\"rank\":3,\"title\":\"基层\"}]，rank数字越小职位越高", "format": "object[]"},
    },
    "one_time_event": {
        "name":         {"label": "事件名称", "hint": "简洁的事件名称，5-15字"},
        "description":  {"label": "事件描述", "hint": "事件发生时的具体描述，50-100字"},
        "trigger_time": {"label": "触发时间", "hint": "ISO 8601格式时间，如 2024-10-26T19:00:00"},
        "condition":    {"label": "触发条件", "hint": "条件表达式，如 player.health>30 或留空"},
        "fire_events":  {"label": "触发事件", "hint": "触发的游戏事件名列表，可被剧情树节点的activate_events接收", "format": "string[]"},
        "activate_events": {"label": "激活事件", "hint": "此事件被哪些游戏事件激活，字符串数组", "format": "string[]"},
    },
    "cyclic_event": {
        "name":           {"label": "事件名称", "hint": "简洁的事件名称，5-15字"},
        "description":    {"label": "事件描述", "hint": "周期性事件的描述，50-100字"},
        "frequency_value": {"label": "频率值", "hint": "每多少个单位触发一次（数字）", "format": "number"},
        "frequency_unit": {"label": "频率单位", "hint": "day/week/month"},
        "first_trigger":  {"label": "首次触发时间", "hint": "ISO 8601格式时间"},
        "condition":      {"label": "触发条件", "hint": "条件表达式或留空"},
        "expires_at":     {"label": "过期时间", "hint": "ISO 8601格式时间或null"},
        "fire_events":    {"label": "触发事件", "hint": "触发的游戏事件名列表，可被剧情树节点的activate_events接收", "format": "string[]"},
        "activate_events": {"label": "激活事件", "hint": "此事件被哪些游戏事件激活，字符串数组", "format": "string[]"},
    },
    "random_item": {
        "description":   {"label": "描述", "hint": "随机项的描述，说明它代表什么"},
        "trigger":       {"label": "触发时机", "hint": "何时触发此随机骰，如'每次战斗开始'"},
        "trigger_type":  {"label": "触发类型", "hint": "always/conditional/event_linked"},
        "condition":     {"label": "触发条件", "hint": "条件表达式（trigger_type=conditional时使用）"},
        "ranges":        {"label": "结果区间", "hint": "返回数组 [{\"min\":1,\"max\":30,\"label\":\"简称\",\"description\":\"详述\"},{\"min\":31,\"max\":70,...},...]", "format": "object[]"},
        "duration_turns": {"label": "持续回合", "hint": "结果持续的回合数（0=仅当回合）", "format": "number"},
        "cooldown_turns": {"label": "冷却回合", "hint": "冷却回合数（0=无冷却）", "format": "number"},
    },
    "variable": {
        "name":    {"label": "显示名称", "hint": "变量的中文显示名称"},
        "type":    {"label": "类型", "hint": "number/string/bool"},
        "default": {"label": "默认值", "hint": "变量初始值"},
        "min":     {"label": "最小值", "hint": "数字类型的最小值", "format": "number"},
        "max":     {"label": "最大值", "hint": "数字类型的最大值", "format": "number"},
    },
    "trigger": {
        "event":     {"label": "触发事件", "hint": "on_start/before_generation/after_ai"},
        "action":    {"label": "执行动作", "hint": "set_var/inject_prompt/activate_lore/deactivate_lore/notify"},
        "condition": {"label": "触发条件", "hint": "条件表达式，如 quest_count >= 3"},
        "params":    {"label": "参数", "hint": "JSON参数对象，如 {\"var_id\":\"x\",\"op\":\"inc\",\"value\":1}", "format": "object"},
    },
    "regex_script": {
        "name":      {"label": "规则名称", "hint": "简短描述规则用途"},
        "find":      {"label": "查找正则", "hint": "正则表达式，如 \\\\(OOC:.*?\\\\)"},
        "replace":   {"label": "替换内容", "hint": "替换文本，留空则删除匹配内容"},
        "placement": {"label": "应用位置", "hint": "ai_output/user_input"},
    },
    "story_tree_node": {
        "name":             {"label": "节点名称", "hint": "简洁的节点名称，5-15字"},
        "description":      {"label": "节点描述", "hint": "节点触发时的描述，1-2句话"},
        "condition":        {"label": "触发条件", "hint": "条件表达式，如 player.reputation>=50 或留空"},
        "activate_events":  {"label": "激活事件", "hint": "此节点被哪些游戏事件解锁，字符串数组", "format": "string[]"},
        "effects":          {"label": "效果", "hint": "JSON对象，可含 set_var/activate_lore/deactivate_lore/inject_prompt/notify/fire_events/unlock_nodes/activate_state/narrative_callback", "format": "object"},
        "choices":          {"label": "选项", "hint": "仅choice类型，返回数组 [{\"id\":\"选项ID\",\"label\":\"显示文字\",\"description\":\"描述\",\"effects\":{},\"unlock\":[\"解锁节点ID\"]}]", "format": "object[]"},
    },
}


_ENTITY_TYPE_LABELS = {
    "npc": "NPC", "player": "主角", "preset": "预设角色",
    "location": "地点", "organization": "组织/势力",
    "one_time_event": "一次性事件", "cyclic_event": "周期事件",
    "random_item": "随机项", "variable": "变量",
    "trigger": "触发器", "regex_script": "正则规则",
    "story_tree_node": "剧情树节点",
}


_ENTITY_TYPE_TO_TAB = {
    "npc": "characters",
    "player": "characters",
    "preset": "characters",
    "location": "world",
    "organization": "organizations",
    "one_time_event": "events",
    "cyclic_event": "events",
    "random_item": "dice",
    "variable": "dice",
    "trigger": "dice",
    "regex_script": "dice",
    "story_tree_node": "story_tree",
}


class FieldsGenerateRequest(BaseModel):
    script: dict
    entity_type: str
    entity_id: str = ""
    fields: list[str]
    user_hint: Optional[str] = None
    mode: str = "fill"


def _find_entity(script: dict, entity_type: str, entity_id: str) -> dict | None:
    if entity_type == "player":
        return script.get("player_character", {})
    if entity_type == "story_tree_node":
        tree_id, _, node_id = entity_id.partition("/")
        for tree in script.get("story_tree", {}).get("trees", []):
            if tree.get("id") == tree_id:
                for node in tree.get("nodes", []):
                    if node.get("id") == node_id:
                        return node
        return None
    collection_map = {
        "npc": "npcs",
        "preset": "player_presets",
        "location": "locations",
        "organization": "organizations",
        "one_time_event": "one_time_events",
        "cyclic_event": "cyclic_events",
        "random_item": "random_items",
        "variable": "variables",
        "trigger": "triggers",
        "regex_script": "regex_scripts",
    }
    key = collection_map.get(entity_type)
    if not key:
        return None
    for item in script.get(key, []):
        if item.get("id") == entity_id:
            return item
    return None


def _build_fields_prompt(
    script_summary: str,
    entity: dict,
    entity_type: str,
    fields: list[str],
    schema: dict[str, dict],
    *,
    mode: str = "fill",
) -> str:
    type_label = _ENTITY_TYPE_LABELS.get(entity_type, entity_type)

    existing_parts = []
    for k, v in entity.items():
        if k == "id" or not v:
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        existing_parts.append(f"- {k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v}")
    existing_text = "\n".join(existing_parts) if existing_parts else "(暂无)"

    field_instructions = []
    for f in fields:
        info = schema.get(f, {})
        label = info.get("label", f)
        hint = info.get("hint", "")
        fmt = info.get("format", "string")
        line = f'- "{f}"({label}): {hint}'
        if fmt != "string":
            line += f"  [JSON格式: {fmt}]"
        field_instructions.append(line)
    fields_text = "\n".join(field_instructions)

    if mode == "polish":
        instruction = (
            f"请对以下{type_label}的字段进行优化：\n"
            "- 已有内容的字段：在保留原意和关键信息的前提下，精炼文字、提升生动性和一致性\n"
            "- 空白字段：根据剧本概况和已有信息，新建合适内容\n"
            "保持原有ID、名称等关键标识不变。"
        )
    else:
        instruction = "请补充以下字段（只返回这些字段，不要返回其他内容）："

    return f"""基于以下剧本概况，为一个{type_label}优化/补充字段。

剧本概况：
{script_summary}

当前{type_label}已有字段：
{existing_text}

{instruction}
{fields_text}

返回紧凑JSON，字段名作为key。只返回JSON，不要任何解释。"""


@router.post("/ai-generate/fields")
async def ai_generate_fields(req: FieldsGenerateRequest):
    if req.entity_type not in _ENTITY_FIELD_SCHEMA:
        raise HTTPException(
            status_code=400,
            detail=f"未知实体类型: {req.entity_type}. 支持: {list(_ENTITY_FIELD_SCHEMA.keys())}",
        )
    schema = _ENTITY_FIELD_SCHEMA[req.entity_type]
    bad = [f for f in req.fields if f not in schema]
    if bad:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {bad}")

    entity = _find_entity(req.script, req.entity_type, req.entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"未找到 {req.entity_type} id={req.entity_id}")

    provider = await _get_ai_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")

    script_summary = _build_full_script_summary(req.script, focus_tab=_ENTITY_TYPE_TO_TAB.get(req.entity_type), entity_type=req.entity_type)
    user_prompt = _build_fields_prompt(script_summary, entity, req.entity_type, req.fields, schema, mode=req.mode)
    if req.user_hint and req.user_hint.strip():
        user_prompt += f"\n\n【用户补充需求】{req.user_hint.strip()}"

    logger.info("字段补全 — type=%s, id=%s, fields=%s", req.entity_type, req.entity_id, req.fields)

    try:
        parsed = await _ai_generate_json(provider, user_prompt, salvage_truncated=False)

        generated = {k: v for k, v in parsed.items() if k in req.fields and v}
        logger.info("字段补全完成 — type=%s, id=%s, 生成: %s", req.entity_type, req.entity_id, list(generated.keys()))
        return {
            "entity_type": req.entity_type,
            "entity_id": req.entity_id,
            "generated": generated,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("字段补全失败 — %s", e)
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")


class FieldsBatchRequest(BaseModel):
    script: dict
    entity_type: str
    entities: list[dict]  # [{"entity_id": "xxx", "fields": ["bio", "personality"]}]
    user_hint: str = ""
    mode: str = "fill"
    batch_size: int = 5


def _build_batch_fields_prompt(
    script_summary: str,
    entities_with_data: list[tuple[str, dict, list[str]]],
    entity_type: str,
    schema: dict[str, dict],
    *,
    mode: str = "fill",
) -> str:
    type_label = _ENTITY_TYPE_LABELS.get(entity_type, entity_type)

    entity_blocks = []
    all_fields: set[str] = set()
    for entity_id, entity, fields in entities_with_data:
        all_fields.update(fields)
        existing_parts = []
        for k, v in entity.items():
            if k == "id" or not v:
                continue
            if isinstance(v, (list, dict)) and not v:
                continue
            existing_parts.append(f"  - {k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v}")
        existing_text = "\n".join(existing_parts) if existing_parts else "  (暂无)"
        fields_str = ", ".join(fields)
        entity_blocks.append(f'### {entity_id}\n已有字段:\n{existing_text}\n需补全字段: {fields_str}')

    entities_text = "\n\n".join(entity_blocks)

    field_instructions = []
    for f in sorted(all_fields):
        info = schema.get(f, {})
        label = info.get("label", f)
        hint = info.get("hint", "")
        fmt = info.get("format", "string")
        line = f'- "{f}"({label}): {hint}'
        if fmt != "string":
            line += f"  [JSON格式: {fmt}]"
        field_instructions.append(line)
    fields_text = "\n".join(field_instructions)

    if mode == "polish":
        instruction = (
            "请对以下每个实体的字段进行优化：\n"
            "- 已有内容的字段：在保留原意和关键信息的前提下，精炼文字、提升生动性和一致性\n"
            "- 空白字段：根据剧本概况和已有信息，新建合适内容\n"
            "保持原有ID、名称等关键标识不变。"
        )
    else:
        instruction = "请为每个实体补充所列字段。"

    return f"""基于以下剧本概况，为多个{type_label}批量优化/补充字段。

剧本概况：
{script_summary}

{instruction}

字段说明：
{fields_text}

以下是需要处理的{type_label}列表：

{entities_text}

返回紧凑JSON对象，以每个实体的ID作为key，值为该实体生成的字段对象。格式：
{{"entity_id_1": {{"field1": "...", "field2": "..."}}, "entity_id_2": {{...}}}}
只返回JSON，不要任何解释。"""


@router.post("/ai-generate/fields-batch")
async def ai_generate_fields_batch(req: FieldsBatchRequest):
    if req.entity_type not in _ENTITY_FIELD_SCHEMA:
        raise HTTPException(
            status_code=400,
            detail=f"未知实体类型: {req.entity_type}. 支持: {list(_ENTITY_FIELD_SCHEMA.keys())}",
        )
    schema = _ENTITY_FIELD_SCHEMA[req.entity_type]
    if not req.entities:
        raise HTTPException(status_code=400, detail="entities 列表不能为空")

    for ent in req.entities:
        bad = [f for f in ent.get("fields", []) if f not in schema]
        if bad:
            raise HTTPException(status_code=400, detail=f"不支持的字段: {bad}")

    provider = await _get_ai_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")

    script_summary = _build_full_script_summary(
        req.script,
        focus_tab=_ENTITY_TYPE_TO_TAB.get(req.entity_type),
        entity_type=req.entity_type,
    )

    system_prompt = _SCRIPT_AI_SYSTEM_PROMPT

    # Resolve entities and split into batches
    entities_with_data: list[tuple[str, dict, list[str]]] = []
    for ent in req.entities:
        eid = ent.get("entity_id", "")
        fields = ent.get("fields", [])
        entity = _find_entity(req.script, req.entity_type, eid)
        if entity is None:
            logger.warning("批量补全跳过未找到的实体 %s/%s", req.entity_type, eid)
            continue
        entities_with_data.append((eid, entity, fields))

    if not entities_with_data:
        raise HTTPException(status_code=404, detail="未找到任何有效实体")

    batch_size = max(1, min(req.batch_size, 20))
    batches = [
        entities_with_data[i:i + batch_size]
        for i in range(0, len(entities_with_data), batch_size)
    ]

    logger.info(
        "批量字段补全 — type=%s, 共%d个实体, 分%d批(每批%d)",
        req.entity_type, len(entities_with_data), len(batches), batch_size,
    )

    all_results: dict[str, dict] = {}

    for batch_idx, batch in enumerate(batches):
        user_prompt = _build_batch_fields_prompt(
            script_summary, batch, req.entity_type, schema, mode=req.mode,
        )
        if req.user_hint and req.user_hint.strip():
            user_prompt += f"\n\n【用户补充需求】{req.user_hint.strip()}"

        try:
            response = await provider.generate(
                [{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=16384,
            )
            response = _strip_code_fences(response)
            parsed = _robust_json_extract(response)
            if parsed:
                for eid, entity_data, fields in batch:
                    if eid in parsed and isinstance(parsed[eid], dict):
                        generated = {k: v for k, v in parsed[eid].items() if k in fields and v}
                        if generated:
                            all_results[eid] = generated
                    elif len(batch) == 1:
                        # Single entity in batch — top-level keys are fields directly
                        generated = {k: v for k, v in parsed.items() if k in fields and v}
                        if generated:
                            all_results[eid] = generated
            else:
                logger.warning("批量补全第%d批JSON解析失败", batch_idx + 1)
        except Exception as e:
            logger.error("批量补全第%d批失败 — %s", batch_idx + 1, e)

    logger.info(
        "批量字段补全完成 — type=%s, 成功%d/%d个实体",
        req.entity_type, len(all_results), len(entities_with_data),
    )

    return {
        "entity_type": req.entity_type,
        "results": all_results,
    }


class OptimizeRelationsRequest(BaseModel):
    script: dict
    user_hint: str = ""
    scope: str = "all"  # "npc" | "org" | "all"


@router.post("/ai-generate/optimize-relations")
async def optimize_relations(req: OptimizeRelationsRequest):
    """Supplement missing relationships and polish existing descriptions.

    scope='npc'：仅NPC间关系；'org'：仅组织间关系；'all'：两者都做（默认，向后兼容）。
    """
    provider = await _get_ai_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")

    do_npc = req.scope in ("npc", "all")
    do_org = req.scope in ("org", "all")
    if not (do_npc or do_org):
        raise HTTPException(status_code=400, detail=f"未知 scope: {req.scope}")

    focus = "characters" if do_npc else "organizations"
    script_summary = _build_full_script_summary(req.script, focus_tab=focus)
    if not script_summary.strip():
        raise HTTPException(status_code=400, detail="请先填写基本信息")

    npc_ids = [n.get("id", "") for n in req.script.get("npcs", []) if n.get("id")]
    org_ids = [o.get("id", "") for o in req.script.get("organizations", []) if o.get("id")]

    rules = []
    output_schema = []
    if do_npc:
        rules.append("- 补充已有NPC之间缺失的有意义关系（用 from/to 有向格式，注意关系是单向的，A→B和B→A的数值可以不同）")
        rules.append("- 对剧本概况中已有的NPC关系：保留trust/affection/fear数值不变，只精炼description文字使其更生动")
        output_schema.append('  "npc_relationships": [{"from":"NPC_A的ID","to":"NPC_B的ID","trust":0-100,"affection":0-100,"fear":0-100,"description":"关系描述(1句)","initially_known":true}]')
    if do_org:
        rules.append("- 补充已有组织之间缺失的关系")
        rules.append("- 对已有组织关系：保留type不变，只精炼description")
        output_schema.append('  "org_relationships": [{"a":"组织A的ID","b":"组织B的ID","type":"同盟/敌对/竞争/从属/中立","description":"关系描述(1句)"}]')

    id_lines = []
    if do_npc:
        id_lines.append(f"已有NPC ID列表: {json.dumps(npc_ids, ensure_ascii=False)}")
    if do_org:
        id_lines.append(f"已有组织ID列表: {json.dumps(org_ids, ensure_ascii=False)}")

    user_prompt = f"""基于以下剧本概况，完善关系网络。

剧本概况：
{script_summary}

{chr(10).join(id_lines)}

【规则】
- 仅使用上面列出的已有ID，不要创建新NPC或新组织
{chr(10).join(rules)}
- 如果所有关系都已充分且描述已足够好，返回空JSON: {{}}

返回JSON：
{{
{(',' + chr(10)).join(output_schema)}
}}
只返回JSON。"""

    if req.user_hint and req.user_hint.strip():
        user_prompt += f"\n\n【用户补充需求】{req.user_hint.strip()}"

    system_prompt = _SCRIPT_AI_SYSTEM_PROMPT

    logger.info("关系优化 — scope=%s, NPC数:%d, 组织数:%d", req.scope, len(npc_ids), len(org_ids))

    try:
        parsed = await _ai_generate_json(provider, user_prompt)

        valid_npc_ids = set(npc_ids)
        valid_org_ids = set(org_ids)
        npc_rels = [
            r for r in parsed.get("npc_relationships", [])
            if r.get("from") in valid_npc_ids and r.get("to") in valid_npc_ids
        ]
        org_rels = [
            r for r in parsed.get("org_relationships", [])
            if r.get("a") in valid_org_ids and r.get("b") in valid_org_ids
        ]

        logger.info("关系优化完成 — NPC关系:%d, 组织关系:%d", len(npc_rels), len(org_rels))
        result = {}
        if npc_rels:
            result["npc_relationships"] = npc_rels
        if org_rels:
            result["org_relationships"] = org_rels
        return {"generated": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("关系优化失败 — %s", e)
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")


class OptimizeEventsRequest(BaseModel):
    script: dict
    user_hint: str = ""


@router.post("/ai-generate/optimize-events")
async def optimize_events(req: OptimizeEventsRequest):
    """Analyze all event systems (story trees + one-time + cyclic) holistically and generate
    new events, connections (fire_events/activate_events), and supplements."""
    provider = await _get_ai_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="请先在设置中配置AI模型")

    script_summary = _build_full_script_summary(req.script, focus_tab="story_tree")
    if not script_summary.strip():
        raise HTTPException(status_code=400, detail="请先填写基本信息")

    existing_tree_ids = []
    existing_node_ids = []
    for tree in req.script.get("story_tree", {}).get("trees", []):
        if tree.get("id"):
            existing_tree_ids.append(tree["id"])
        for node in tree.get("nodes", []):
            if node.get("id"):
                existing_node_ids.append(node["id"])
    existing_ot_ids = [e["id"] for e in req.script.get("one_time_events", []) if e.get("id")]
    existing_ce_ids = [e["id"] for e in req.script.get("cyclic_events", []) if e.get("id")]

    user_prompt = f"""基于以下剧本，统一分析并优化整个事件系统（剧情树 + 一次性事件 + 周期事件），新增缺失内容并建立跨系统关联。

剧本概况：
{script_summary}

已有剧情线ID: {json.dumps(existing_tree_ids, ensure_ascii=False)}
已有剧情节点ID: {json.dumps(existing_node_ids, ensure_ascii=False)}
已有一次性事件ID: {json.dumps(existing_ot_ids, ensure_ascii=False)}
已有周期事件ID: {json.dumps(existing_ce_ids, ensure_ascii=False)}

【优化规则】
- 综合分析三个事件子系统的关联性和完整性
- 新增缺失的事件（剧情节点、一次性事件、周期事件）填补叙事空白
- 通过 fire_events / activate_events 建立跨系统关联（如：一次性事件触发 → 剧情节点解锁；剧情节点完成 → 触发周期事件）
- 新增的ID不能与已有ID重复
- 新增的剧情节点必须指定所属剧情线ID（tree_id），可以是已有剧情线或新建剧情线
- requires/on_complete_unlock 引用的节点ID必须在同一剧情线内
- 跨系统关联使用事件名：一个节点/事件的 effects.fire_events 触发事件名，另一个节点的 activate_events 接收该事件名
- 引用的变量ID必须在已有变量列表中
- 如果所有事件系统都已完善，返回空JSON: {{{{}}}}

请生成JSON（只包含需要新增的内容）：
{{{{
  "trees": [按需新增剧情线 {{
    "id": "英文ID",
    "name": "剧情线名称",
    "description": "描述",
    "icon": "图标(crown/sword/shield/scroll/star/skull/eye/fire/moon/tree/castle/gem)",
    "nodes": [{{
      "id": "节点英文ID",
      "name": "节点名称",
      "description": "描述(1-2句)",
      "type": "auto/choice/quest/timed/trigger/periodic",
      "requires": ["前置节点ID"],
      "condition": "条件表达式(可选)",
      "on_complete_unlock": ["完成后解锁的节点ID"],
      "activate_events": ["被哪些事件名触发解锁(可选)"],
      "effects": {{
        "fire_events": ["触发的事件名(可选)"],
        "set_var": [{{"var_id":"变量ID","op":"set/add","value":"值"}}],
        "inject_prompt": "注入AI提示(可选)",
        "notify": "通知玩家(可选)"
      }}
    }}]
  }}],
  "new_nodes_for_existing_trees": [按需为已有剧情线补充节点 {{
    "tree_id": "已有剧情线ID",
    "nodes": [同上节点格式]
  }}],
  "one_time_events": [按需新增 {{"id":"英文ID","name":"事件名称","description":"描述","trigger_time":"ISO时间","condition":"条件(可选)","fire_events":["触发的事件名(可选)"],"activate_events":["被哪些事件名触发解锁(可选)"]}}],
  "cyclic_events": [按需新增 {{"id":"英文ID","name":"事件名称","description":"描述","frequency_value":1,"frequency_unit":"day","first_trigger":"ISO时间","expires_at":null,"condition":"条件(可选)","fire_events":["触发的事件名(可选)"],"activate_events":["被哪些事件名触发解锁(可选)"]}}]
}}}}
只返回JSON。"""

    if req.user_hint and req.user_hint.strip():
        user_prompt += f"\n\n【用户补充需求】{req.user_hint.strip()}"

    system_prompt = _SCRIPT_AI_SYSTEM_PROMPT

    logger.info("事件系统优化 — 剧情线:%d, 节点:%d, 一次性:%d, 周期:%d",
                len(existing_tree_ids), len(existing_node_ids), len(existing_ot_ids), len(existing_ce_ids))

    try:
        parsed = await _ai_generate_json(provider, user_prompt)

        all_empty = all(
            (isinstance(v, (list, dict)) and len(v) == 0)
            for v in parsed.values()
        )
        if all_empty:
            return {"generated": {}, "complete": True}

        logger.info("事件系统优化完成 — 字段: %s", ", ".join(parsed.keys()))
        return {"generated": parsed}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("事件系统优化失败 — %s", e)
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")

