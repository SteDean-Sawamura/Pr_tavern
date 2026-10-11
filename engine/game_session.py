"""Game session: core game loop orchestrator."""

import asyncio
import copy
import json
import logging
import random
import re
import time as _time
import uuid
import config
from dataclasses import asdict
from datetime import datetime, timedelta

from engine.dice import DiceRoller
from engine.script_loader import ScriptLoader
from engine.state_manager import StateManager
from engine.world_tree import WorldTree
from engine.event_scheduler import EventScheduler, is_public_event, parse_time, public_event_notifications
from engine.event_engine import EventEngine, EventResult, GameEvent
from engine.meta_events import MetaEventBus, MetaEvent, MetaEventTrigger
from engine.lorebook import Lorebook
from engine.prompt_builder import PromptBuilder, _RE_GAME_STATE_BLOCK, _RE_GAME_STATE_OPEN
from engine.history_summarizer import HistorySummarizer
from ai.response_parser import ResponseParser
from ai.base import stream_split_think, strip_think_tags, _THINK_EXTRACT_RE
from engine.vector_memory import VectorMemory, _VECTOR_AVAILABLE
from engine.script_variables import ScriptVariables
from engine.triggers import TriggerEngine
from engine.regex_scripts import RegexScriptEngine
from engine.ledger import Ledger
from engine.data_bank import DataBank
from engine.class_system import ClassRegistry
from engine.story_tree import StoryTreeEngine
from engine.narrative_graph import NarrativeGraph
from engine.npc_autonomy import NPCAutonomy
from engine.player_model import PlayerModel
from engine.world_clock import WorldClock
from engine.director_notes import DirectorNotes
from engine.tool_schemas import STATE_TOOLS_SCHEMA
from engine.session.tools_mixin import ToolsMixin
from engine.session.prepare_mixin import PrepareMixin
from engine.session.shop_mixin import ShopMixin
from engine.session.skill_check_mixin import SkillCheckMixin
from engine.session.pipeline_mixin import PipelineMixin
from engine.session.agentic_mixin import AgenticMixin
from engine.session.state_apply_mixin import StateApplyMixin
from engine.session.regenerate_mixin import RegenerateMixin
from engine.session.npc_mixin import NpcMixin
from engine.session.story_director_mixin import StoryDirectorMixin
from engine.session.background_tasks_mixin import BackgroundTasksMixin

logger = logging.getLogger(__name__)
_STREAM_WAIT_HEARTBEAT_SECONDS = 10


# ===== 游戏调优常量 =====
# 离屏 NPC 模拟频率（每 N 回合触发一次）
OFFSCREEN_SIM_INTERVAL = 3
# 玩家风格分析频率（每 N 回合）
PLAY_STYLE_INTERVAL = 5
# NPC 对话场景的聊天历史保留轮数
NPC_CHAT_HISTORY_DEPTH = 6
# NPC 对话历史最大存储轮数（超出后 FIFO 淘汰最旧记录）
NPC_CHAT_HISTORY_MAX = 20
# NPC 对话每轮推进的游戏时间（分钟）
NPC_CHAT_TIME_MINUTES = 5
# O-3: 预编译 CJK 检测正则，避免每回合重新编译
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
_TOOL_CALL_RE = re.compile(r'\[TOOL_CALL:\s*(\w+)\(([^)]*)\)\]')
_SPEAKER_RE = re.compile(
    r'([\u4e00-\u9fff\u3400-\u4dbf]{2,4}(?:\u5148\u751f|\u5973\u58eb|\u5c0f\u59d0|\u8001\u5e08|\u533b\u751f|\u6559\u6388|\u961f\u957f|\u90e8\u957f|\u5c40\u957f|\u79d8\u4e66|\u7ba1\u7406\u5458|\u4e3b\u4efb|\u8bfe\u957f|\u5904\u957f|\u7ec4\u957f|\u5e08\u5085|\u5927\u4eba|\u9601\u4e0b)?)'
    r'(?:\u8bf4\u9053|\u8bf4|\u558a\u9053|\u4f4e\u58f0\u9053|\u95ee\u9053|\u7b54\u9053|\u53f9\u9053|\u7b11\u9053|\u51b7\u7b11\u9053|\u6012\u9053|\u56de\u7b54|\u5f00\u53e3|\u8865\u5145\u9053|\u89e3\u91ca\u9053|\u63d0\u9192\u9053|\u561f\u56d4\u9053|\u4f4e\u58f0\u8bf4|\u8f7b\u58f0\u8bf4)'
    r'|(?:^|\n)\s*([\u4e00-\u9fff\u3400-\u4dbf]{2,6})(?:\uff1a|:)\s*[\u300c"\'"]'
)
# \u81ea\u52a8\u6ce8\u518c speaker \u65f6\u6392\u9664\u7684\u8bef\u5339\u914d\u6a21\u5f0f\uff08\u526f\u8bcd/\u52a8\u4f5c\u77ed\u8bed/\u4ee3\u8bcd/\u6cdb\u79f0\u7b49\uff09
_SPEAKER_REJECT_CHARS = set("\u7684\u5730\u5f97\u4e86\u7740\u8fc7\u6765\u53bb\u5728\u4ece\u5230\u628a\u88ab\u7ed9\u8ba9\u53eb\u662f\u6709\u6ca1\u4e0d\u4e5f\u5c31\u90fd\u8fd8\u53c8\u624d\u80fd\u4f1a\u8981\u60f3")
_SPEAKER_REJECT_WORDS = {
    "\u6b64\u523b", "\u6ca1\u4eba", "\u542b\u6df7", "\u538b\u4f4e", "\u62ff\u8d77", "\u653e\u4e0b", "\u62ac\u5934", "\u4f4e\u5934",
    "\u8f6c\u8eab", "\u8d77\u8eab", "\u4f38\u624b", "\u70b9\u5934", "\u6447\u5934", "\u6325\u624b", "\u7ec8\u4e8e", "\u7a81\u7136",
    "\u7136\u540e", "\u968f\u540e", "\u6700\u7ec8", "\u5ffd\u7136", "\u7acb\u523b", "\u9a6c\u4e0a", "\u8fde\u5fd9", "\u8d76\u7d27",
    "\u53ea\u89c1", "\u90a3\u4eba", "\u6b64\u4eba", "\u67d0\u4eba", "\u5bf9\u65b9", "\u8eab\u8fb9", "\u65c1\u8fb9", "\u95e8\u5916",
    "\u6709\u4eba", "\u65e0\u4eba", "\u4f17\u4eba", "\u4e00\u4eba", "\u4ed6\u4eba", "\u8def\u4eba", "\u51e0\u4eba", "\u4e24\u4eba",
    "\u8bdd\u7b52", "\u55d3\u5b50", "\u624b\u6307", "\u7709\u5934", "\u80a9\u8180", "\u8111\u888b", "\u8eab\u5b50", "\u8170\u8eab",
}

_ATTR_SYNONYMS = {
    "力量": ("武力", "力", "武", "攻击", "战斗", "体力", "strength", "str"),
    "敏捷": ("身法", "速度", "灵巧", "反应", "闪避", "dexterity", "dex", "agility"),
    "智力": ("智谋", "智慧", "才学", "学识", "知识", "情报", "intelligence", "int", "wisdom", "wis"),
    "魅力": ("口才", "话术", "社交", "外交", "领导", "统率", "政治", "charisma", "cha"),
    "体力": ("耐力", "体质", "生命", "constitution", "con", "stamina"),
}

def _extract_reasoning(raw: str) -> str:
    """Extract content inside <think>...</think> tags. Returns empty string if none."""
    if not raw:
        return ""
    m = _THINK_EXTRACT_RE.search(raw)
    return m.group(1).strip() if m else ""


class GameSession(
    ToolsMixin,
    PrepareMixin,
    ShopMixin,
    SkillCheckMixin,
    PipelineMixin,
    AgenticMixin,
    StateApplyMixin,
    RegenerateMixin,
    NpcMixin,
    StoryDirectorMixin,
    BackgroundTasksMixin,
):
    def __init__(self, script: dict, ai_provider, save_id: str | None = None,
                 stage_models: dict | None = None):
        script = copy.deepcopy(script)
        self.script = script
        self._initial_mechanics = copy.deepcopy({
            key: script.get(key, []) for key in ("random_items", "variables", "triggers")
        })
        self.ai_provider = ai_provider
        self.pipeline_mode = config.PIPELINE_MODE
        self.stage_models = stage_models or {}
        self.save_id = save_id or str(uuid.uuid4())
        self.dice = DiceRoller()
        self.state_manager = StateManager(script)
        self.event_scheduler = EventScheduler(script)
        self.prompt_builder = PromptBuilder(script)
        self.prompt_builder.condition_eval = self._evaluate_condition
        self.response_parser = ResponseParser()
        self.world_tree = WorldTree(script_id=script.get("script_id", ""))
        _settings = script.get("settings", {})
        self.history_summarizer = HistorySummarizer(
            threshold=_settings.get("summary_interval", 10),
            keep_recent=_settings.get("summary_keep_recent", 5),
            word_threshold=_settings.get("summary_word_threshold", 3000),
        )
        _vm_enabled = _settings.get("vector_memory_enabled", False)
        self.vector_memory = VectorMemory(self.save_id) if (_vm_enabled and _VECTOR_AVAILABLE) else None
        self.data_bank = DataBank(self.save_id, known_empty=save_id is None) if _VECTOR_AVAILABLE else None
        self.script_variables = ScriptVariables(script.get("variables", []))
        self.trigger_engine = TriggerEngine(script.get("triggers", []), self.script_variables)
        self.regex_engine = RegexScriptEngine(script.get("regex_scripts", []))
        self.ledger = Ledger()
        _class_system = _settings.get("class_system")
        self.class_registry = ClassRegistry(_class_system) if _class_system else None
        _story_tree_def = script.setdefault("story_tree", {})
        self._merge_legacy_events_into_tree(_story_tree_def, script)
        self.story_tree_engine = StoryTreeEngine(_story_tree_def) if _story_tree_def.get("trees") else None
        self.event_engine = EventEngine(script, shadow_story_nodes=self.story_tree_engine is not None)
        self._dynamic_story_tree_ids: set[str] = set()
        self.current_state: dict = {}
        self.turn_number = 0
        self.authors_note = ""  # Player's behind-the-scenes directive
        self.authors_note_position = "end"  # "end" or "at_depth"
        self.authors_note_depth = 4
        self.negative_prompt = ""  # CFG-style negative prompt (injected as prohibition rules)
        self.logit_bias: list[dict] = []  # [{"text": "暴力", "bias": -5}, ...]
        self._background_tasks: set[asyncio.Task] = set()
        self._index_tasks: set[asyncio.Task] = set()
        self._deferred_lore_vector_ids: set[str] | None = None
        self._deferred_tasks: set[asyncio.Task] = set()
        self._deferred_story_task: asyncio.Task | None = None
        self._deferred_story_source: tuple | None = None
        self._state_lock = asyncio.Lock()  # 保护 current_state 的后台任务锁
        self._4b_semaphore = asyncio.Semaphore(_settings.get("max_parallel_4b", 5))
        self._summary_fail_count = 0  # 摘要连续失败计数
        self._last_activity_time: float = 0.0  # 上次活跃时间戳（用于 play_time 计算）
        # Agentic mode: abort / inject controls
        self._abort_flag: bool = False
        self._inject_queue: list[str] = []
        self._agent_control_event = asyncio.Event()
        self._agent_stop_event = asyncio.Event()
        self._stable_prefix: str | None = None  # Frozen foreground system prompt (agentic mode)
        # Stage 4a/5 工具调用模式的每轮缓冲
        self._npc_reaction_tool_calls: list[dict] = []
        self._new_npc_tool_calls: list[dict] = []
        self._choices_tool_calls: list[dict] = []
        # P1: 预构建 ID → 名称/对象的字典查找，避免每次行动 O(n) 扫列表
        self._location_by_id: dict = {
            loc["id"]: loc for loc in script.get("locations", [])
            if isinstance(loc, dict) and loc.get("id")
        }
        self._event_desc_by_id: dict = {}
        self._event_def_by_id: dict = {}
        self._dynamic_scheduled_event_ids: set[str] = set()
        for ev in script.get("cyclic_events", []):
            if isinstance(ev, dict) and ev.get("id"):
                self._event_desc_by_id[ev["id"]] = ev.get("description", ev["id"])
                self._event_def_by_id[ev["id"]] = ev
        for ev in script.get("one_time_events", []):
            if isinstance(ev, dict) and ev.get("id"):
                self._event_desc_by_id[ev["id"]] = ev.get("description", ev["id"])
                self._event_def_by_id[ev["id"]] = ev
        self._npc_by_id: dict = {
            n["id"]: n for n in script.get("npcs", [])
            if isinstance(n, dict) and n.get("id")
        }
        self._initial_npc_ids = set(self._npc_by_id)
        self._npc_name_to_id: dict[str, str] = {
            n.get("name", ""): n["id"] for n in script.get("npcs", [])
            if isinstance(n, dict) and n.get("id") and n.get("name")
        }
        self._random_item_by_id: dict = {
            ri["id"]: ri for ri in script.get("random_items", [])
            if isinstance(ri, dict) and ri.get("id")
        }
        self._ps_by_id: dict = {
            ps["id"]: ps for ps in script.get("persistent_states", [])
            if isinstance(ps, dict) and ps.get("id")
        }
        self._org_members: dict[str, list[str]] = {}
        for _npc_def in script.get("npcs", []):
            for _om in _npc_def.get("organizations", []):
                _oid = _om.get("org_id", "")
                if _oid:
                    self._org_members.setdefault(_oid, []).append(_npc_def["id"])
        self.script_cursor = None  # 剧本游标（script 模式时初始化）
        self.meta_event_bus = MetaEventBus()
        self._register_meta_events()
        self._opening_draft = None
        self.narrative_graph = NarrativeGraph()
        self.npc_autonomy = NPCAutonomy(self.script.get("npcs", []), self.script.get("locations", []))
        self.player_model = PlayerModel()
        self.director_notes = DirectorNotes()
        self.world_clock = WorldClock()
        for npc in self.script.get("npcs", []):
            schedule = npc.get("schedule")
            if schedule and isinstance(schedule, list):
                self.world_clock.register_npc_schedule(
                    npc["id"], npc.get("name", npc["id"]), schedule,
                )
        self._last_tool_result: dict | None = None  # D1: 上一次工具调用记录（供 undo 回退）
        self._settlement_messages: list[dict] = []  # D3: 后台→前台消息队列
        # #5: 任务合批 + 模型路由
        from engine.task_batcher import TaskBatcher
        self.task_batcher = TaskBatcher()
        # #7: 精细用量统计
        from engine.usage_tracker import UsageTracker
        self.usage_tracker = UsageTracker()
        # E1/E2/E3: 可观测性
        from engine.ab_testing import ABTestRunner
        self.ab_tester = ABTestRunner()
        self._agent_traces: list[dict] = []

    def _stage_kwargs(self, stage: str) -> dict:
        """根据任务类型返回 AI 调用参数，支持模型路由"""
        # 优先使用 profile 级 stage_models（数据库配置）
        model = self.stage_models.get(stage)
        if not model:
            # 其次检查全局任务级模型路由（config.py / API 动态配置）
            import config
            routes = getattr(config, 'TASK_MODEL_ROUTES', {})
            model = routes.get(stage)
        return {"model": model} if model else {}

    def _restore_session_models_from_state(self):
        """Restore narrative_graph and player_model from current_state snapshot."""
        ng_data = self.current_state.pop("_narrative_graph", None)
        if ng_data:
            self.narrative_graph = NarrativeGraph.from_snapshot(ng_data)
        pm_data = self.current_state.pop("_player_model", None)
        if pm_data:
            self.player_model = PlayerModel.from_snapshot(pm_data)
        na_data = self.current_state.pop("_npc_autonomy", None)
        if na_data:
            self.npc_autonomy = NPCAutonomy.from_snapshot(
                self.script.get("npcs", []), self.script.get("locations", []), na_data
            )
        dn_data = self.current_state.pop("_director_notes", None)
        if dn_data:
            self.director_notes = DirectorNotes.from_snapshot(dn_data)


    def _build_logit_bias_hint(self) -> str:
        if not self.logit_bias:
            return ""
        encourage = [e["text"] for e in self.logit_bias if e.get("bias", 0) > 0]
        suppress = [e["text"] for e in self.logit_bias if e.get("bias", 0) < 0]
        parts = []
        if encourage:
            parts.append("鼓励使用的元素/词汇: " + "、".join(encourage))
        if suppress:
            parts.append("尽量避免的元素/词汇: " + "、".join(suppress))
        return "## 词汇偏置指引\n" + "\n".join(parts) if parts else ""


    def _mark_activity(self) -> int:
        """标记活跃并返回自上次活跃以来的秒数（上限600秒，避免计入挂机时间）。"""
        now = _time.time()
        if self._last_activity_time <= 0:
            self._last_activity_time = now
            return 0
        elapsed = int(now - self._last_activity_time)
        self._last_activity_time = now
        return min(elapsed, 600)


    async def _enrich_with_rag(
        self, action_text: str, recent: list[dict],
        activated_lore: list, history_context: str,
        retrieval_context: dict | None = None,
    ) -> tuple[str, set]:
        """Enrich history_context with vector memory retrieval + keyword lorebook fallback.

        Returns (enriched_history_context, lore_ids_from_rag).
        """
        if retrieval_context is not None:
            retrieval_context["rag_history_hits"] = []
            retrieval_context["databank_hits"] = []
            retrieval_context["rag_lore_order"] = []
            retrieval_context["retrieval_timings_ms"] = {}
        timings = retrieval_context["retrieval_timings_ms"] if retrieval_context is not None else None
        lore_ids_from_rag: set = set()
        excluded_ids = self.prompt_builder._excluded_lore_ids(self.current_state)
        available_lore = {
            entry.id: entry for entry in self.prompt_builder.lorebook.entries
            if entry.enabled and entry.id not in excluded_ids
        }
        bank_future = None
        bank_started = None
        if self.data_bank:
            bank_query = action_text + " " + (recent[-1].get("ai_response", "")[:100] if recent else "")
            bank_started = _time.monotonic()

            def query_bank():
                try:
                    return self.data_bank.query(bank_query, 3)
                except Exception:
                    return []

            bank_future = asyncio.get_running_loop().run_in_executor(None, query_bank)
        rag_lore_order = retrieval_context["rag_lore_order"] if retrieval_context is not None else []
        if self.vector_memory:
            started = _time.monotonic()
            try:
                index_ready = await self._drain_background_tasks(index_only=True)
            finally:
                if timings is not None:
                    timings["index_wait"] = int((_time.monotonic() - started) * 1000)
            exclude = [recent[-1].get("turn_number", 0)] if recent else []
            vq = action_text + " " + (recent[-1].get("ai_response", "")[:100] if recent else "")
            if self.story_tree_engine:
                sts = self.current_state.get("story_tree_state", {})
                for _nid in list(sts.get("active", []))[:3]:
                    _node = self.story_tree_engine._nodes.get(_nid)
                    if _node:
                        vq += " " + (_node.get("description", "") or _node.get("name", ""))[:60]
            # 追加当前地点名和在场 NPC 名以偏向相关内容
            _loc_id = self.current_state.get("player", {}).get("location", "")
            if _loc_id:
                _loc_name = self.current_state.get("display_names", {}).get(_loc_id, _loc_id)
                vq += " " + _loc_name
            _npc_states = self.current_state.get("npcs", {})
            present_npc_count = 0
            for _nid_k, _ns in _npc_states.items():
                if isinstance(_ns, dict) and _ns.get("current_location") == _loc_id:
                    vq += " " + _ns.get("name", _nid_k)
                    present_npc_count += 1
                    if present_npc_count == 3:
                        break
            branch_node_ids = {node["id"] for node in self.world_tree.get_active_branch()}
            retrieved = []
            if index_ready:
                started = _time.monotonic()
                try:
                    retrieved = await asyncio.get_running_loop().run_in_executor(
                        None, self.vector_memory.query, vq, 5, exclude, None, branch_node_ids
                    )
                except Exception:
                    logger.warning("语义检索失败，本轮继续使用关键词资料", exc_info=True)
                finally:
                    if timings is not None:
                        timings["vector_query"] = int((_time.monotonic() - started) * 1000)
            if retrieved:
                history_lines = []
                lore_lines = []
                for r in retrieved:
                    if r.get("doc_type") == "lorebook":
                        entry_id = r.get("entry_id", "")
                        entry = available_lore.get(entry_id)
                        if entry and entry_id not in lore_ids_from_rag:
                            lore_lines.append(f"[{entry.comment or entry.id}] {entry.content}")
                            lore_ids_from_rag.add(entry_id)
                            rag_lore_order.append(entry_id)
                    else:
                        history_lines.append(f"第{r['turn']}回合({r['game_time']}): {r['text']}")
                        # 第二层：从历史回合 metadata 提取关联 lore IDs
                        assoc_lore = r.get("active_lore_ids", "")
                        if assoc_lore:
                            for lid in assoc_lore.split(","):
                                lid = lid.strip()
                                if lid in available_lore and lid not in lore_ids_from_rag:
                                    lore_ids_from_rag.add(lid)
                                    rag_lore_order.append(lid)
                extra = ""
                if history_lines:
                    extra += "## 相关历史记忆（语义检索）\n" + "\n".join(history_lines)
                if lore_lines:
                    extra += ("\n\n" if extra else "") + "## 相关世界知识（语义检索）\n" + "\n".join(lore_lines)
                if extra:
                    history_context = (history_context + "\n\n" + extra).strip()
                if retrieval_context is not None:
                    retrieval_context["rag_history_hits"] = history_lines

        # 第一层：RAG 召回的 lorebook 条目回注 activated_lore，参与正常注入流程
        if lore_ids_from_rag:
            activated_ids = {entry.id for entry in activated_lore}
            for lid in rag_lore_order:
                entry = available_lore[lid]
                if lid not in activated_ids:
                    activated_lore.append(entry)
                    activated_ids.add(lid)

        keyword_only_lore = [e for e in activated_lore if e.id not in lore_ids_from_rag]
        if keyword_only_lore:
            fallback_lines = ["## 关键词匹配的补充知识"]
            for e in keyword_only_lore[:5]:
                fallback_lines.append(f"[{e.comment or e.id}] {e.content}")
            history_context = (history_context + "\n\n" + "\n".join(fallback_lines)).strip()

        # Data Bank retrieval
        if bank_future is not None:
            try:
                bank_results = await bank_future
                if bank_results:
                    if retrieval_context is not None:
                        retrieval_context["databank_hits"] = bank_results
                    bank_lines = ["## 知识库参考"]
                    for br in bank_results:
                        src = f"[{br['filename']}]" if br.get("filename") else ""
                        bank_lines.append(f"{src} {br['text'][:300]}")
                    history_context = (history_context + "\n\n" + "\n".join(bank_lines)).strip()
            except Exception:
                pass
            finally:
                if timings is not None:
                    timings["data_bank_query"] = int((_time.monotonic() - bank_started) * 1000)

        return history_context, lore_ids_from_rag

    async def _stage_rag_query(self, query_text: str, top_k: int = 3,
                                doc_type: str | None = None,
                                exclude_turns: list | None = None,
                                boost_lore_ids: set | None = None) -> list[dict]:
        """Lightweight per-stage RAG query with optional story-aware re-ranking."""
        if not self.vector_memory or not query_text:
            return []
        loop = asyncio.get_running_loop()
        fetch_k = top_k * 2 if boost_lore_ids else top_k
        branch_node_ids = {node["id"] for node in self.world_tree.get_active_branch()}
        results = await loop.run_in_executor(
            None, self.vector_memory.query, query_text, fetch_k, exclude_turns,
            doc_type, branch_node_ids,
        )
        if boost_lore_ids and results:
            for r in results:
                if r.get("doc_type") == "milestone":
                    r["score"] = r.get("score", 0.5) * 1.3
                lore_csv = r.get("active_lore_ids", "")
                if lore_csv and boost_lore_ids & set(lore_csv.split(",")):
                    r["score"] = r.get("score", 0.5) * 1.2
            results.sort(key=lambda x: x.get("score", 0), reverse=True)
            results = results[:top_k]
        return results

    def _boost_lore_by_route(self, activated_lore: list, route: dict):
        """根据 route 的 scene_type/scope 临时调整 activated_lore 排序。"""
        scene_type = route.get("scene_type", "")
        scope = route.get("scope", "moderate")
        type_keywords = {
            "combat": ["战斗", "武器", "军事", "combat"],
            "social": ["关系", "社交", "对话", "social"],
            "exploration": ["地点", "探索", "地理", "exploration"],
            "trade": ["交易", "商业", "经济", "trade"],
        }
        boost_kws = type_keywords.get(scene_type, [])
        if not boost_kws:
            return
        boosted = []
        rest = []
        for entry in activated_lore:
            comment_lower = (entry.comment or "").lower()
            if any(kw in comment_lower for kw in boost_kws):
                boosted.append(entry)
            else:
                rest.append(entry)
        if scope == "major" and hasattr(self, "prompt_builder") and self.prompt_builder.lorebook:
            existing_ids = {e.id for e in activated_lore}
            excluded_ids = self.prompt_builder._excluded_lore_ids(self.current_state)
            for entry in self.prompt_builder.lorebook.entries:
                if (entry.id not in existing_ids and entry.id not in excluded_ids
                        and entry.enabled and entry.priority >= 90):
                    rest.append(entry)
        activated_lore.clear()
        activated_lore.extend(boosted + rest)

    @staticmethod
    def _parse_route(raw: str) -> dict:
        raw = strip_think_tags(raw or "").strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```\s*$', '', raw)
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    pass
        return {"scene_type": "unknown", "systems": [], "scope": "moderate", "focus_npcs": [], "skill_check": {"needed": False}}

    async def _run_route_stage(self, action_text: str, ctx: dict) -> dict:
        player = self.current_state.get("player", {})
        location_id = player.get("location", "")
        location_name = self.prompt_builder._resolve_location_name(location_id)
        loc_def = self.prompt_builder._location_by_id.get(location_id, {})
        loc_desc = loc_def.get("description", "")
        present_npc_ids = ctx.get("present_npc_ids", [])
        npc_states = self.current_state.get("npcs", {})
        npc_entries = []
        for nid in present_npc_ids[:8]:
            npc_data = npc_states.get(nid, {})
            npc_name = npc_data.get("name", nid) if isinstance(npc_data, dict) else nid
            npc_entries.append(f"{nid}({npc_name})")
        npc_list_str = ", ".join(npc_entries)
        attrs = player.get("attributes", {})
        attr_names = list(attrs.keys())
        attr_list_str = "/".join(attr_names) if attr_names else "力量/敏捷/智力/魅力"
        route_system = "你是场景分析器。根据玩家行动判断涉及的游戏系统和是否需要技能检定。只返回紧凑JSON，不要解释。"
        loc_line = location_name
        if loc_desc:
            loc_line += f"（{loc_desc[:40]}）"
        route_user = (
            f"位置: {loc_line}\n"
            f"当前游戏时间: {ctx.get('old_time', '未知')}\n"
            f"在场NPC: {npc_list_str or '无'}\n"
            f"角色属性: {attr_list_str}\n"
            f"玩家行动: {action_text}\n\n"
            '返回: {"scene_type":"social|combat|exploration|trade|travel|rest",'
            '"systems":["从npc/location/inventory/faction/moral/companion/deadline/clue/quest/combat/new_npc中选择相关的"],'
            '"scope":"minor|moderate|major",'
            '"focus_npcs":["填写npc_id（英文ID），不要填中文名"],'
            '"skill_check":{"needed":true/false,"attr":"从角色属性中选择最相关的","difficulty":"easy|medium|hard|extreme"},'
            '"expand_story":true/false,'
            '"generate_image":true/false,'
            '"skip_state_settlement":true/false,'
            '"skip_npc_reaction":true/false,'
            '"skip_choices":true/false,'
            '"context_depth":"minimal|normal|deep",'
            '"recall_hints":["关键词1","关键词2"]}'
            "\n\nskill_check规则：\n"
            "- 日常对话、简单移动、等待、休息等不需要检定(needed:false)\n"
            "- 有风险或挑战性的行动需要检定(needed:true)，如潜行、说服、战斗、调查、偷窃等\n"
            "- difficulty根据行动难度和环境判断\n\n"
            "expand_story规则：\n"
            "- 当剧情树大部分节点已完成、或剧情出现重大转折(scope=major)、或玩家进入全新区域时 → true\n"
            "- 日常行动、推进中的剧情尚未完结时 → false\n\n"
            "generate_image规则：\n"
            "- 场景发生明显视觉变化时生成图像(true)：进入新地点、战斗场面、重大事件、环境剧变、初次见面\n"
            "- 纯对话、等待、思考、小幅移动、重复场景等无明显视觉变化时不生成(false)\n\n"
            "skip_state_settlement规则：\n"
            "- 纯对话、信息查询、观察环境等不改变任何游戏状态的行动 → true\n"
            "- 涉及物品获取/消耗、属性变化、位置移动、时间推进等 → false\n\n"
            "skip_npc_reaction规则：\n"
            "- 在场NPC为空或行动完全不涉及NPC → true\n"
            "- 有NPC在场且行动可能影响NPC态度/关系 → false\n\n"
            "skip_choices规则：\n"
            "- 玩家正在执行连续多步动作（如计划中的步骤）、或行动结果明确无需选择 → true\n"
            "- 需要玩家做出决策、或场景自然产生多种可能性 → false\n\n"
            "context_depth规则：\n"
            "- minimal: 简单日常行动，不需要大量历史上下文\n"
            "- normal: 一般互动，标准上下文量\n"
            "- deep: 涉及复杂剧情线、历史伏笔、多NPC关系等需要丰富上下文\n\n"
            "recall_hints规则：\n"
            "- 列出1-3个关键词，用于Stage 1检索相关历史记录\n"
            "- 应包含行动涉及的核心概念：地名、NPC名、物品名、事件名等\n"
            "- 如无特别需要检索的内容，返回空数组[]"
        )
        # Feature #3: Plan awareness in route
        plan = self.current_state.get("pending_plan")
        if plan and plan.get("status") == "active" and plan.get("steps_remaining"):
            route_user += (
                f"\n\n注意: 玩家有未完成计划「{plan['goal'][:30]}」，"
                f"当前步骤: {plan['steps_remaining'][0][:40]}"
            )
        if "preflight_model_calls" in ctx:
            ctx["preflight_model_calls"]["route"] += 1
        raw = await self.ai_provider.generate(
            [{"role": "user", "content": route_user}],
            system=route_system, max_tokens=500,
            **self._stage_kwargs("knowledge_graph"),
        )
        try:
            route = self._parse_route(raw)
        except Exception as e:
            logger.warning("Route Stage 解析失败: %s", e)
            route = {"scene_type": "unknown", "systems": [], "scope": "moderate", "focus_npcs": []}
        # Feature #1: Scene hijack overrides route
        hijack = self.current_state.get("_scene_hijack")
        if hijack:
            route["scene_type"] = "npc_hijack"
            route["scope"] = "major"
            route["skill_check"] = {"needed": False}
            hijack_npc = hijack.get("npc_id", "")
            if hijack_npc and hijack_npc not in route.get("focus_npcs", []):
                route.setdefault("focus_npcs", []).insert(0, hijack_npc)
        # Feature #3: Plan declaration detection
        _PLAN_KEYWORDS = ("计划", "打算", "准备", "预谋", "我的计划是", "策划", "筹划")
        if any(kw in action_text for kw in _PLAN_KEYWORDS):
            if not (plan and plan.get("status") == "active"):
                route["has_plan_declaration"] = True
        # Store route for MetaEvent handlers
        self.current_state["_last_route"] = route
        self.current_state["_last_present_npcs"] = present_npc_ids
        logger.debug("Route: type=%s scope=%s systems=%s focus=%s",
                      route.get("scene_type"), route.get("scope"),
                      route.get("systems", []), route.get("focus_npcs", []))
        return route

    async def _async_vector_store(self, node_id: str, text: str, metadata: dict):
        """Store a turn's text in the vector DB (runs in thread pool).

        If vector_summarize is enabled and text is long, summarize first for better embeddings.
        """
        embed_text = ""
        _settings = self.script.get("settings", {})
        if _settings.get("vector_summarize") and len(text) > 500 and self.ai_provider:
            try:
                summary = await self.ai_provider.generate(
                    [{"role": "user", "content": f"用50字概括以下剧情的核心事件和情感变化:\n{text[:1000]}"}],
                    max_tokens=100,
                )
                if summary and len(summary.strip()) > 10:
                    embed_text = summary.strip()
            except Exception:
                pass
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.vector_memory.add, node_id, text, metadata, embed_text)

    async def _async_lorebook_vector_sync(self, batch: list[tuple]):
        if self.vector_memory:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self.vector_memory.add_lorebook_batch, batch,
            )

    # Keys stripped from swipe snapshots to save memory (reconstructable from main state)
    _SWIPE_STRIP_KEYS = frozenset({
        "adventure_log", "npc_offscreen_log",
        "play_style_summary", "play_style_last_turn",
        "history_summary", "key_events", "last_summarized_turn",
        "npc_chat_history",
    })

    @staticmethod
    def _slim_snapshot(state: dict) -> dict:
        """Create a shallow copy of state with large reconstructable keys removed."""
        return {k: v for k, v in state.items() if k not in GameSession._SWIPE_STRIP_KEYS}

    @staticmethod
    def _sanitize_gather_results(
        results: tuple, labels: list[tuple[str, bool]],
    ) -> tuple[list[str], list[str]]:
        """Clean gather results: handle BaseException + strip_think_tags.

        labels: list of (display_label, emit_warning) per result.
        Returns (cleaned_strings, warnings).
        """
        cleaned: list[str] = []
        warnings: list[str] = []
        for raw, (label, warn_on_fail) in zip(results, labels):
            if isinstance(raw, BaseException):
                logger.warning("%s失败: %s", label, raw)
                cleaned.append("")
                if warn_on_fail:
                    warnings.append(f"{label}失败，本回合相关数据未更新。")
            else:
                cleaned.append(strip_think_tags(raw) if raw else "")
        return cleaned, warnings

    def _fire_lifecycle_event(self, event_name: str) -> tuple[list[str], list[str]]:
        """Fire story_tree, event_engine and unmatched legacy lifecycle hooks.
        Returns (inject_prompts, notifications).
        """
        inject_prompts: list[str] = []
        notifications: list[str] = []
        if self.story_tree_engine:
            st_res = self.story_tree_engine.evaluate(
                self.current_state, self.turn_number,
                condition_eval=self._evaluate_condition,
                advance_timed=False,
            )
            self._apply_story_tree_result(st_res)
            inject_prompts.extend(st_res.inject_prompts)
            notifications.extend(public_event_notifications(self.script, st_res))
        if self.event_engine:
            ee_res = self.event_engine.fire_event(
                event_name, self.current_state,
                condition_eval=self._evaluate_condition,
            )
            self._apply_event_result(ee_res)
            inject_prompts.extend(ee_res.inject_prompts)
            notifications.extend(public_event_notifications(self.script, ee_res))
        if self.trigger_engine and self.trigger_engine.triggers:
            actions = self.trigger_engine.fire(
                event_name, self.current_state, include_event_triggers=self.event_engine is None,
                condition_eval=self._evaluate_condition,
            )
            if actions:
                effects = self.trigger_engine.execute_actions(actions, self.current_state)
                self._apply_trigger_effects(effects)
                inject_prompts.extend(effects["inject_prompts"])
                notifications.extend(public_event_notifications(
                    self.script, EventResult(notifications=effects["notifications"]),
                ))
        return inject_prompts, notifications

    def _apply_event_def_effects(self, evt_def: dict, event_id: str) -> list:
        """Apply effects list from an event definition. Returns state change log."""
        state_log: list = []
        for eff in evt_def.get("effects", []):
            etype = eff.get("type", "")
            if etype == "state_change":
                changes = [{"target": eff["target"], "op": eff.get("op", "set"), "value": eff.get("value")}]
                self.current_state, log = self.state_manager.apply_changes(
                    self.current_state, changes, inplace=True
                )
                state_log.extend(log)
            elif etype == "activate_state":
                aps = self.current_state.setdefault("active_persistent_states", [])
                sid = eff.get("id", "")
                if sid and sid not in aps:
                    aps.append(sid)
            elif etype == "deactivate_state":
                aps = self.current_state.get("active_persistent_states", [])
                sid = eff.get("id", "")
                if sid in aps:
                    aps.remove(sid)
            elif etype == "schedule_override":
                self.trigger_engine.execute_actions(
                    [{"action": "schedule_override", "params": eff}], self.current_state,
                )
            elif etype == "narrative_callback":
                self._record_narrative_callback(
                    eff.get("text", ""), tags=[f"event:{event_id}"],
                    priority=eff.get("priority", "medium"),
                )
            elif etype == "activate_lore":
                self._set_lore_enabled(eff.get("id", ""), True)
            elif etype == "deactivate_lore":
                self._set_lore_enabled(eff.get("id", ""), False)
            elif etype == "add_lore":
                entry_data = eff.get("entry", {})
                if entry_data.get("id") and entry_data.get("content"):
                    self._apply_lorebook_changes(
                        additions=[{**entry_data, "entry_type": "event_generated"}],
                        entry_type="event_generated",
                    )
            elif etype == "update_lore":
                lid = eff.get("id", "")
                if lid and eff.get("content"):
                    self._apply_lorebook_changes(updates=[eff])
            elif etype == "remove_lore":
                rid = eff.get("id", "")
                if rid:
                    self._apply_lorebook_changes(removals=[rid])
            elif etype == "add_random_item":
                item_data = eff.get("item", {})
                if item_data.get("id"):
                    self._apply_random_item_updates({"add": [item_data]})
            elif etype == "update_random_item":
                upd_data = eff.get("update", {})
                if upd_data.get("id"):
                    self._apply_random_item_updates({"update": [upd_data]})
            elif etype == "remove_random_item":
                rid = eff.get("id", "")
                if rid:
                    self._apply_random_item_updates({"remove": [rid]})
            elif etype == "add_variable":
                var_data = eff.get("variable", {})
                if var_data.get("id"):
                    self._apply_variable_updates({"add": [var_data]})
            elif etype == "add_trigger":
                trg_data = eff.get("trigger", {})
                if trg_data.get("id"):
                    self._apply_trigger_updates({"add": [trg_data]})
            elif etype == "update_trigger":
                upd_data = eff.get("update", {})
                if upd_data.get("id"):
                    self._apply_trigger_updates({"update": [upd_data]})
        return state_log

    async def prepare_opening_draft(self) -> dict:
        """准备开场 draft：预览开场白 + 初始化世界书/变量，但不正式开始游戏

        Returns: {
            "draft_id": str,
            "openings": list[dict],  # 可选的开场白列表
            "initial_state": dict,   # 预览的初始状态
            "expires_at": str,       # 2小时过期
        }
        """
        from datetime import datetime, timedelta

        draft_id = str(uuid.uuid4())[:8]

        # 收集可选开场白
        openings = []
        first_mes = self.script.get("player_character", {}).get("first_mes", "")
        if first_mes:
            openings.append({"id": "default", "text": first_mes, "label": "默认开场"})

        # 预设角色的开场
        for preset in self.script.get("player_presets", []):
            opening_text = preset.get("opening_text", "")
            if opening_text:
                openings.append({
                    "id": preset.get("id", ""),
                    "text": opening_text,
                    "label": preset.get("name", ""),
                })

        # 剧本的通用开场
        script_opening = self.script.get("opening", {}).get("text", "")
        if script_opening and not any(o["text"] == script_opening for o in openings):
            openings.append({"id": "script", "text": script_opening, "label": "剧本开场"})

        # 预览初始状态（不写入）
        initial_state = {
            "location": self.current_state.get("player", {}).get("location", ""),
            "time": self.current_state.get("game_time", ""),
            "attributes": {k: v for k, v in self.current_state.get("player", {}).get("attributes", {}).items()},
        }

        self._opening_draft = {
            "draft_id": draft_id,
            "openings": openings,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(hours=2)).isoformat(),
        }

        return {
            "draft_id": draft_id,
            "openings": openings,
            "initial_state": initial_state,
            "expires_at": self._opening_draft["expires_at"],
        }

    def select_opening(self, draft_id: str, opening_id: str) -> dict | None:
        """从 draft 中选择一个开场白"""
        draft = getattr(self, '_opening_draft', None)
        if not draft or draft.get("draft_id") != draft_id:
            return None

        # 检查过期
        from datetime import datetime
        expires = datetime.fromisoformat(draft["expires_at"])
        if datetime.now() > expires:
            self._opening_draft = None
            return None

        selected = next((o for o in draft["openings"] if o["id"] == opening_id), None)
        self._opening_draft = None  # 使用后清除
        return selected

    async def initialize(self, skip_opening: bool = False) -> dict:
        """Initialize a new game, returning the opening data."""
        self.current_state = ScriptLoader.create_initial_state(self.script)
        self.script_variables.init_state(self.current_state)
        self._init_shop_inventories()
        self.turn_number = 0
        # 持久化当前管线模式到存档
        self.current_state["_pipeline_mode"] = self._get_pipeline_mode()

        # #4 叙事图谱：种子实体（玩家、脚本NPC、地点）
        player = self.current_state.get("player", {})
        player_id = player.get("id", "player")
        self.narrative_graph.add_entity(player_id, "player", player.get("name", "player"))
        for npc in self.script.get("npcs", []):
            npc_id = npc.get("id", "")
            if npc_id:
                self.narrative_graph.add_entity(npc_id, "npc", npc.get("name", npc_id))
        for loc in self.script.get("locations", []):
            loc_id = loc.get("id", "")
            if loc_id:
                self.narrative_graph.add_entity(loc_id, "location", loc.get("name", loc_id))

        # 如果剧本有线性剧本块，初始化游标
        story_blocks = self.script.get("story_tree", {}).get("nodes", [])
        if story_blocks and isinstance(story_blocks, list):
            from engine.script_cursor import ScriptCursor
            self.script_cursor = ScriptCursor(story_blocks)

        # Build opening base text and choices (synchronous, from script data)
        settings = self.script.get("settings", {})
        fixed_opening = settings.get("fixed_opening", {}).get("default_enabled", False)
        opening = self.script.get("opening", {})

        preset_opening = self.script.get("_preset_opening_text")
        opening_variants = self.script.get("opening_variants", [])
        selected_variant_id = self.script.get("_selected_opening_variant", "")
        if preset_opening:
            narrative = preset_opening
            choices = self._build_opening_choices({"choices": self._opening_choice_source()})
        elif opening_variants:
            if selected_variant_id:
                variant = next((v for v in opening_variants if v.get("id") == selected_variant_id), None)
            else:
                variant = None
            if not variant:
                variant = random.choice(opening_variants)
            narrative = variant.get("text", self.script.get("world_background", "游戏开始了。"))
            self.current_state["_opening_variant_id"] = variant.get("id", "")
            choices = self._build_opening_choices({"choices": self._opening_choice_source(variant)})
        elif fixed_opening and opening.get("text"):
            narrative = opening["text"]
            choices = self._build_opening_choices(opening)
        else:
            narrative = self.script.get("world_background", "游戏开始了。")
            choices = self._build_opening_choices(opening)

        scripted_choices = bool(choices)
        if not choices:
            choices = self._generate_opening_choices()

        # Roll initial weather if applicable (before AI branch — ctx needs dice_dicts)
        dice_results = self._roll_always_active_dice()
        self._apply_weather(dice_results)

        # 8 阶段 pipeline 开局，失败时回退到旧 3 阶段轻量润色
        _narrative_reasoning = ""
        if self.ai_provider and not skip_opening:
            try:
                present_npc_ids, nearby_npc_ids = self._compute_present_npcs(self.current_state)

                world_bg = self.script.get("world_background", "")
                opening_history = f"## 世界背景\n{world_bg}\n\n## 开局骨架（在此基础上大幅扩写润色）\n{narrative}"
                if choices:
                    choices_lines = "\n".join(
                        f"- {c.get('id','')}: {c.get('text','')}" for c in choices
                    )
                    opening_history += f"\n\n## 脚本预设选项（保留核心意图方向，可润色措辞）\n{choices_lines}"
                opening_history += (
                    f"\n\n## 开场时间\n这是系统开场，不是玩家行动；场景停留在 "
                    f"{self.current_state.get('game_time', '')}，不要推进时间或替玩家选择行动。"
                )

                self._ensure_identity_lore()
                activated_lore, _ = self.prompt_builder.scan_lorebook(
                    narrative, [],
                    timed_state=self.current_state.get("lorebook_timed_state"),
                    turn_number=self.turn_number,
                    state=self.current_state,
                )

                opening_ctx = {
                    "check_result": {},
                    "dice_dicts": [self._dice_result_to_dict(d) for d in dice_results],
                    "triggered_events": [],
                    "triggered_consequences": [],
                    "achieved_milestones": [],
                    "present_npc_ids": present_npc_ids,
                    "nearby_npc_ids": nearby_npc_ids,
                    "activated_lore": activated_lore,
                    "stage_directives": {},
                    "event_sections": PromptBuilder._format_events_for_prompt(
                        self.event_engine.get_events_for_prompt(self.current_state)
                    ),
                    "old_time": self.current_state.get("game_time", ""),
                    "new_time": self.current_state.get("game_time", ""),
                    "estimated_minutes": 0,
                    "is_system_opening": True,
                    "opening_scene": narrative,
                    "opening_choices": [choice.get("text", "") for choice in choices if choice.get("text")],
                    "recent_nodes": [],
                    "prev_plot_decision": "",
                    "base_history_context": opening_history,
                    "history_context": opening_history,
                    "context_memory": "",
                    "recent_reasoning": [],
                    "action_text": "游戏开始",
                    "all_state_changes": [],
                }
                opening_route = {
                    "scope": "major",
                    "scene_type": "opening",
                    "systems": None,
                    "focus_npcs": present_npc_ids[:4],
                }
                open_action = {"type": "system", "text": "游戏开始"}

                _narrative_reasoning = ""
                if self._agentic_unified_enabled():
                    _opening_pipeline = self._execute_unified_agent(
                        opening_ctx, opening_route, open_action,
                    )
                elif self._agentic_enabled():
                    _opening_pipeline = self._execute_agentic_pipeline(
                        opening_ctx, opening_route, open_action,
                    )
                else:
                    _opening_pipeline = self._execute_pipeline(
                        opening_ctx, opening_route, open_action,
                    )
                async for item in _opening_pipeline:
                    if item["type"] == "pipeline_result":
                        parsed = item["parsed"]
                        pipeline_narrative = parsed.get("narrative", "")
                        if pipeline_narrative:
                            narrative = pipeline_narrative
                        if parsed.get("choices") and not scripted_choices:
                            choices = parsed["choices"]
                        _narrative_reasoning = item.get("narrative_reasoning", "")
                        break
                old_attitudes = {
                    nid: nd.get("attitude_toward_player", 50)
                    for nid, nd in self.current_state.get("npcs", {}).items()
                    if isinstance(nd, dict)
                }
                old_faction_reps = {
                    fid: fd.get("value", 50)
                    for fid, fd in self.current_state.get("faction_reputation", {}).items()
                    if isinstance(fd, dict)
                }
                self.current_state, _ = self._apply_common_parsed_changes(
                    self.current_state, parsed, inplace=True,
                    present_npc_ids=present_npc_ids,
                )
                self._check_attitude_thresholds(
                    old_attitudes, parsed.get("npc_attitude_changes", [])
                )
                self._sync_world_changes_to_lorebook(old_attitudes)
                self._check_faction_reputation_events(old_faction_reps)

                self._compute_time_atmosphere()

                await self._safe_infer_initial_state(narrative)

            except Exception as e:
                logging.getLogger(__name__).warning(
                    "开局 pipeline 失败，回退到轻量润色: %s", e
                )
                personalized_narrative = await self._safe_personalize_narrative(narrative)
                if personalized_narrative:
                    narrative = personalized_narrative
                _infer_ok = await self._safe_infer_initial_state(narrative)
                if not scripted_choices:
                    personalized_choices = await self._safe_personalize_choices(
                        narrative, choices
                    )
                    if personalized_choices:
                        choices = personalized_choices
                if not _infer_ok:
                    narrative += "\n\n[初始状态推演未成功，背包和持续状态使用了默认配置]"

        # 扫描开局叙事，标记出现过的NPC为 known+met
        self._auto_mark_npcs_known(narrative)
        self.current_state["opening_context"] = narrative[:600] if narrative else ""

        # Index lorebook entries into vector memory for unified semantic retrieval
        if self.vector_memory:
            lore_batch = []
            for entry in self.prompt_builder.lorebook.entries:
                if entry.content and len(entry.content) >= 20:
                    lore_batch.append((entry.id, entry.content, {
                        "entry_type": entry.entry_type,
                        "comment": entry.comment,
                    }))
            if lore_batch:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.vector_memory.add_lorebook_batch, lore_batch)

        # Fire on_start triggers
        if self.story_tree_engine:
            start_result = self.story_tree_engine.evaluate(
                self.current_state, self.turn_number,
                condition_eval=self._evaluate_condition,
                active_events=["on_start"],
            )
            self._apply_story_tree_result(start_result)
        ee_start = self.event_engine.fire_event(
            "on_start", self.current_state, self._evaluate_condition,
            skip_story_triggers=self.story_tree_engine is not None,
        )
        self._apply_event_result(ee_start)
        self._refresh_npc_locations()
        self._record_npc_sightings()
        self._collect_interactables()

        node_id = self.world_tree.add_node(
            parent_id=None,
            game_time=self.current_state.get("game_time", ""),
            turn_number=0,
            player_action=None,
            ai_response=narrative,
            choices_presented=choices,
            dice_rolls=[self._dice_result_to_dict(d) for d in dice_results],
            state_snapshot=self.current_state,
        )
        if _narrative_reasoning:
            node = self.world_tree.get_node(node_id)
            if node:
                node["thinking"] = _narrative_reasoning

        result = {
            "save_id": self.save_id,
            "node_id": node_id,
            "narrative": narrative,
            "choices": choices,
            "state": self.current_state,
            "dice_rolls": [self._dice_result_to_dict(d) for d in dice_results],
        }
        if _narrative_reasoning:
            result["thinking"] = _narrative_reasoning

        # Opening scene image generation
        if getattr(self, "_image_provider", None) and narrative:
            try:
                from ai.image_prompt_builder import build_image_prompt
                loc = self.current_state.get("player", {}).get("location", "")
                loc_data = self._location_by_id.get(loc, {})
                loc_name = loc_data.get("name", loc) if loc_data else loc
                loc_desc = loc_data.get("description", "") if loc_data else ""
                tod = self.current_state.get("time_of_day", "day")
                weather = self.current_state.get("current_weather", "")

                characters = []
                pc = self.script.get("player_character", {})
                pc_app = pc.get("appearance") or pc.get("bio") or ""
                if pc_app:
                    characters.append(f"Player: {pc_app[:200]}")
                npc_states = self.current_state.get("npcs", {})
                for nid, ns in list(npc_states.items())[:5]:
                    if isinstance(ns, dict) and ns.get("current_location") == loc:
                        npc_def = self._npc_by_id.get(nid, {})
                        desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                        name = ns.get("name") or npc_def.get("name", nid)
                        if desc:
                            characters.append(f"{name}: {desc[:150]}")

                image_style = ""
                try:
                    from api.config_routes import get_image_style
                    style_data = await get_image_style()
                    image_style = style_data.get("custom") or style_data.get("preset") or ""
                except Exception:
                    pass

                img_prompt = await build_image_prompt(
                    narrative, loc_name, "atmospheric", tod, self.ai_provider,
                    weather=weather, characters=characters,
                    location_desc=loc_desc, image_style=image_style,
                )
                scene_img = await self._image_provider.generate_image(img_prompt)
                scene_img["_prompt"] = img_prompt
                result["scene_image"] = scene_img
            except Exception as e:
                logger.warning("Opening scene image generation failed: %s", e)
                result["scene_image"] = None
        else:
            result["scene_image"] = None

        return result


    def _shelve_current_pc(self):
        """Convert current PC into an NPC, preserving all evolved state."""
        state = self.current_state
        player = state.get("player", {})
        pc_id = player.get("id", "player")

        # Store PC-bound data
        shelved = state.setdefault("shelved_pc_data", {})
        shelved[pc_id] = {
            "player": dict(player),
            "inventory": list(state.get("inventory", [])),
            "discovered_lore": list(state.get("pc_discovered_lore", [])),
            "lore_clues": copy.deepcopy(state.get("pc_lore_clues", {})),
            "event_reports": copy.deepcopy(state.get("pc_event_reports", [])),
            "visible_locations": list(state.get("visible_locations", [])),
            "chat_history": dict(state.get("npc_chat_history", {})),
            "turn_shelved": self.turn_number,
        }

        # Add old PC as NPC
        pc_orgs = player.get("organizations", [])
        state.setdefault("npcs", {})[pc_id] = {
            "attitude_toward_player": 50,
            "name": player.get("name", pc_id),
            "known": True,
            "met": False,
            "default_location": player.get("location", ""),
            "bio": player.get("bio", ""),
            "personality": player.get("personality", ""),
            "title": player.get("title", ""),
            "current_room": player.get("current_room", ""),
            "capabilities": "",
            "organizations": pc_orgs,
            "superior": "",
            "_shelved_attributes": player.get("attributes", {}),
        }

        # Register shelved PC in script NPC list and KG lorebook
        script_npcs = self.script.setdefault("npcs", [])
        existing_entry = next((n for n in script_npcs if n.get("id") == pc_id), None)
        if not existing_entry:
            existing_entry = {
                "id": pc_id,
                "name": player.get("name", pc_id),
                "bio": player.get("bio", ""),
                "personality": player.get("personality", ""),
                "title": player.get("title", ""),
                "organizations": pc_orgs,
                "initial_location": player.get("location", ""),
            }
            script_npcs.append(existing_entry)
        self._npc_by_id[pc_id] = existing_entry
        self.prompt_builder._npc_name_map[pc_id] = player.get("name", pc_id)
        if self.prompt_builder._has_kg:
            self.prompt_builder.refresh_kg_entries([pc_id], [])

        self.prompt_builder.sync_shelved_pc_knowledge(state)

        # Record POV history
        state.setdefault("pov_history", []).append({
            "pc_id": pc_id,
            "turn": self.turn_number,
            "game_time": state.get("game_time", ""),
        })

        # Add display name for the shelved PC
        state.setdefault("display_names", {})[pc_id] = player.get("name", pc_id)

    def _init_new_pc(self, preset_id: str | None, custom_character: dict | None):
        """Initialize a new PC from preset or custom data, keeping world state intact."""
        state = self.current_state

        if preset_id:
            # Check if this is a returning PC (previously shelved)
            shelved = state.get("shelved_pc_data", {}).get(preset_id)
            if shelved:
                # Restore shelved PC
                state["player"] = shelved["player"]
                state["inventory"] = shelved["inventory"]
                state["pc_discovered_lore"] = shelved["discovered_lore"]
                state["pc_lore_clues"] = copy.deepcopy(shelved.get("lore_clues", {}))
                state["pc_event_reports"] = copy.deepcopy(shelved.get("event_reports", []))
                state["visible_locations"] = shelved["visible_locations"]
                state["npc_chat_history"] = shelved["chat_history"]
                # Remove from NPC pool
                state.get("npcs", {}).pop(preset_id, None)
                # Remove from shelved data
                state["shelved_pc_data"].pop(preset_id, None)
            else:
                # Fresh PC from preset
                presets = self.script.get("player_presets", [])
                preset = next((p for p in presets if p.get("id") == preset_id), None)
                if not preset:
                    return
                attrs = {}
                for attr_name, attr_def in preset.get("attributes", {}).items():
                    if isinstance(attr_def, dict):
                        attrs[attr_name] = attr_def.get("value", 50)
                    else:
                        attrs[attr_name] = attr_def

                state["player"] = {
                    "id": preset_id,
                    "name": preset.get("name", preset_id),
                    "title": preset.get("title", ""),
                    "bio": preset.get("bio", ""),
                    "personality": preset.get("personality", ""),
                    "portrait_desc": preset.get("portrait_desc", ""),
                    "location": preset.get("initial_location", ""),
                    "current_room": preset.get("default_room", ""),
                    "long_term_goal": preset.get("long_term_goal", ""),
                    "attributes": attrs,
                    "relationships": {},
                }
                state["inventory"] = []
                if preset.get("initial_inventory"):
                    for item in preset["initial_inventory"]:
                        if isinstance(item, dict):
                            state["inventory"].append({"item": item.get("item", item.get("name", "")), "quantity": item.get("quantity", 1)})
                        elif isinstance(item, str):
                            state["inventory"].append({"item": item, "quantity": 1})

                state["pc_discovered_lore"] = list(preset.get("known_lore", []))
                state["pc_lore_clues"] = {}
                state["pc_event_reports"] = []

                # Visible locations: script initially_visible + new PC's location
                vis = [loc["id"] for loc in self.script.get("locations", []) if loc.get("initially_visible", True)]
                new_loc = preset.get("initial_location", "")
                if new_loc and new_loc not in vis:
                    vis.append(new_loc)
                state["visible_locations"] = vis

        elif custom_character:
            pc_id = custom_character.get("id", f"custom_{len(state.get('pov_history', []))}")
            attrs = {}
            for k, v in custom_character.get("attributes", {}).items():
                attrs[k] = v.get("value", 50) if isinstance(v, dict) else v

            pc_name = custom_character.get("name", "自定义角色")
            new_loc = custom_character.get("initial_location", "")
            org = custom_character.get("organization", "")
            title = custom_character.get("title", "")

            state["player"] = {
                "id": pc_id,
                "name": pc_name,
                "bio": custom_character.get("bio", ""),
                "personality": custom_character.get("personality", ""),
                "portrait_desc": custom_character.get("portrait_desc", ""),
                "title": title,
                "location": new_loc,
                "current_room": "",
                "long_term_goal": custom_character.get("long_term_goal", ""),
                "attributes": attrs,
                "relationships": {},
            }
            if org:
                state["player"]["organizations"] = [{"org_id": org}]
            state["inventory"] = []
            state["pc_discovered_lore"] = []
            state["pc_lore_clues"] = {}
            state["pc_event_reports"] = []

            # Register new location via existing reveal system + lorebook KG
            vis = [loc["id"] for loc in self.script.get("locations", []) if loc.get("initially_visible", True)]
            if new_loc:
                if new_loc not in vis:
                    vis.append(new_loc)
                all_loc_ids = {loc["id"] for loc in self.script.get("locations", [])}
                if new_loc not in all_loc_ids:
                    state.setdefault("display_names", {})[new_loc] = new_loc
                    state.setdefault("location_descriptions", {})[new_loc] = ""
                    if self.prompt_builder and self.prompt_builder.lorebook:
                        self.prompt_builder.add_location_kg_entry(new_loc, new_loc, "")
                        self.prompt_builder._loc_name_map[new_loc] = new_loc
            state["visible_locations"] = vis

            # Register new organization via KG refresh
            if org:
                existing_orgs = {o.get("id", "") for o in self.script.get("organizations", [])}
                if org not in existing_orgs:
                    new_org_def = {"id": org, "name": org, "description": ""}
                    self.script.setdefault("organizations", []).append(new_org_def)
                    state.setdefault("display_names", {})[org] = org
                if self.prompt_builder and self.prompt_builder._has_kg:
                    self.prompt_builder.refresh_kg_entries([], [org])

            # Register the new PC as a known NPC profile in lorebook (for AI context)
            if self.prompt_builder and self.prompt_builder.lorebook:
                entry_id = f"_kg_npc_{pc_id}"
                if not any(e.id == entry_id for e in self.prompt_builder.lorebook.entries):
                    bio_parts = [pc_name]
                    if title:
                        bio_parts.append(f"职位: {title}")
                    if org:
                        dn = state.get("display_names", {})
                        bio_parts.append(f"所属: {dn.get(org, org)}")
                    if custom_character.get("bio"):
                        bio_parts.append(custom_character["bio"])
                    content = "；".join(bio_parts)
                    keys = [pc_name]
                    if title:
                        keys.append(title)
                    self.prompt_builder.lorebook.add_entries([{
                        "id": entry_id,
                        "keys": keys,
                        "content": content,
                        "entry_type": "character",
                        "priority": 80,
                        "position": "after_world",
                        "constant": False,
                        "enabled": True,
                        "scan_depth": 3,
                        "comment": f"角色: {pc_name}",
                    }])

        # Reset PC-specific transient state
        self.prompt_builder.sync_shelved_pc_knowledge(state)
        state["npc_chat_history"] = {}
        state["play_style_summary"] = ""
        self._stable_prefix = None  # 新 PC 需要重建冻结前缀
        pacing = state.get("pacing_state")
        if pacing:
            pacing["tension"] = 40
            pacing["trend"] = "stable"
            pacing["consecutive_high"] = 0
            pacing["consecutive_low"] = 0

    async def _generate_pov_narrative(self) -> str:
        """Generate a brief narrative introducing the new PC's current situation."""
        state = self.current_state
        player = state["player"]
        game_time = state.get("game_time", "")
        location_id = player.get("location", "")
        loc_name = state.get("display_names", {}).get(location_id, location_id)

        system = (
            "你是文字游戏的叙事整合师。用200字以内，以第二人称写一段简短的视角切换叙事。\n"
            "要求：交代新角色此刻的处境（位置、正在做什么、周围氛围），不要写前情回顾。\n"
            "直接输出叙事文本，不要JSON/标记。"
        )
        content = (
            f"角色: {player.get('name', '')}\n"
            f"身份: {player.get('bio', '')}\n"
            f"性格: {player.get('personality', '')}\n"
            f"当前时间: {self.prompt_builder.format_game_time(game_time) if game_time else '未知'}\n"
            f"当前位置: {loc_name}\n"
            f"天气: {state.get('current_weather', '未知')}"
        )

        try:
            response = await self.ai_provider.generate(
                [{"role": "user", "content": content}],
                system=system,
                max_tokens=400,
                **self._stage_kwargs("narrative"),
            )
            return response.strip() if isinstance(response, str) else f"视角切换至{player.get('name', '新角色')}。"
        except Exception:
            return f"视角切换至{player.get('name', '新角色')}。"


    # ================================================================
    # NPC 声音一致性校验（Stage 3 后置检查）
    # ================================================================

    _MODAL_PARTICLES = "呢啊嘛呀吧哦哎唉嗯嘿喂哈"


    async def _enrich_and_route(self, player_action: dict, ctx: dict) -> dict:
        """RAG 增强 + Route 分类 + 技能检定。返回 route。"""
        action_text = player_action.get("text", "")
        async def timed(stage, operation):
            started = _time.monotonic()
            try:
                return await operation
            finally:
                ctx.setdefault("preflight_timings_ms", {})[stage] = int((_time.monotonic() - started) * 1000)

        if player_action.get("type") == "system":
            history_context, lore_ids_from_rag = await timed("retrieval", self._enrich_with_rag(
                action_text, ctx["recent_nodes"], ctx.get("activated_lore", []),
                ctx.get("history_context", ""), ctx,
            ))
            route = {
                "scope": "major", "scene_type": "opening", "systems": None,
                "focus_npcs": ctx.get("present_npc_ids", [])[:4],
            }
            ctx["history_context"] = history_context
            ctx["lore_ids_from_rag"] = lore_ids_from_rag
            ctx["route"] = route
            ctx["check_result"] = None
            ctx.setdefault("preflight_timings_ms", {})["route"] = 0
            self.current_state["_last_route"] = route
            self.current_state["_last_present_npcs"] = ctx.get("present_npc_ids", [])
            return route

        retrieval_task = asyncio.create_task(timed("retrieval", self._enrich_with_rag(
            action_text, ctx["recent_nodes"],
            ctx.get("activated_lore", []), ctx.get("history_context", ""), ctx,
        )))
        route_task = asyncio.create_task(timed("route", self._run_route_stage(action_text, ctx)))
        try:
            (history_context, lore_ids_from_rag), route = await asyncio.gather(retrieval_task, route_task)
        except BaseException:
            retrieval_task.cancel()
            route_task.cancel()
            await asyncio.gather(retrieval_task, route_task, return_exceptions=True)
            raise
        ctx["history_context"] = history_context
        ctx["lore_ids_from_rag"] = lore_ids_from_rag
        ctx["route"] = route
        self._boost_lore_by_route(ctx.get("activated_lore", []), route)
        ctx["check_result"] = self._resolve_skill_check_from_route(route, action_text)
        return route

    def _get_pipeline_mode(self) -> str:
        """读取当前节点快照的模式，旧存档回退到会话创建时的默认值。"""
        if self.current_state and self.current_state.get("_pipeline_mode"):
            return self.current_state["_pipeline_mode"]
        return self.pipeline_mode

    def _agentic_enabled(self) -> bool:
        """True when PIPELINE_MODE == 'agentic' and the provider supports native tools."""
        mode = self._get_pipeline_mode()
        has_tools = hasattr(self.ai_provider, "generate_with_tools")
        ai_enabled = bool(self.script.get("settings", {}).get("ai_tools_enabled", True))
        result = mode in ("agentic", "agentic_dual") and has_tools and ai_enabled
        if mode in ("agentic", "agentic_dual") and not result:
            logger.warning("agentic 模式未生效: mode=%s, has_tools=%s, ai_enabled=%s", mode, has_tools, ai_enabled)
        return result

    def _agentic_unified_enabled(self) -> bool:
        """True when PIPELINE_MODE == 'agentic_unified'."""
        mode = self._get_pipeline_mode()
        return mode == "agentic_unified" and hasattr(self.ai_provider, "generate_with_tools")

    async def _quick_intent_check(self, action_text: str, preflight_model_calls: dict | None = None) -> str:
        """LLM 意图分类，在 _prepare_turn 之前执行"""
        # 仅独立的元指令走快捷路径，剧情内的“撤销任命”等交给分类器判断。
        command = re.sub(r"[。.!！?？\s]+$", "", action_text.strip().lower())
        if command.startswith("请"):
            command = command[1:].strip()
        if command in {"重写", "重新写", "重新生成", "重生成", "再写一遍", "再来一次",
                       "重写上一段", "重写上一轮", "重写刚才那段"}:
            return "rewrite"
        if command in {"撤销", "回退", "undo", "撤销上一轮", "撤销上一回合",
                       "回退上一轮", "回退上一步", "取消上一轮"}:
            return "undo"

        # LLM 分类（处理模糊意图）
        intent_tools = [{"type": "function", "function": {
                "name": "classify_intent",
                "description": "对玩家输入进行意图分类",
                "parameters": {"type": "object", "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["action", "query", "rewrite", "undo"],
                        "description": "action=推进剧情的行动, query=查询信息不推进时间, rewrite=对上一轮叙事不满意要重写, undo=撤销上一轮",
                    },
                }, "required": ["intent"]},
        }}]
        if preflight_model_calls is not None:
            preflight_model_calls["intent"] += 1
        classification = self.ai_provider.generate_with_tools(
                [{"role": "user", "content": f"玩家输入：{action_text}"}],
                system="你是意图分类器。根据玩家输入判断意图类型并调用 classify_intent。"
                       "\n- action: 玩家想做某件事（走动、对话、行动）"
                       "\n- query: 玩家在问问题或查看状态（几点了、在哪、背包里有什么）"
                       "\n- rewrite: 玩家对上一段叙事不满意想重新生成"
                       "\n- undo: 玩家想撤销/回退上一轮"
                       "\n剧情内撤销任命或命令、再次表决属于 action；仅明确要求修改回复或回退游戏进度才是 rewrite/undo。",
                tools=intent_tools,
                max_tokens=50,
                **self._stage_kwargs("planning"),
        )
        if getattr(self, "_active_agent_stream", False):
            classification_task = asyncio.create_task(classification)
            stop_task = asyncio.create_task(self._agent_stop_event.wait())
            try:
                await asyncio.wait({classification_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if self._abort_flag:
                    classification_task.cancel()
                    return "action"
                resp = await classification_task
            finally:
                stop_task.cancel()
                if not classification_task.done():
                    classification_task.cancel()
                await asyncio.gather(classification_task, stop_task, return_exceptions=True)
        else:
            resp = await classification
        try:
            tc_list = resp.get("tool_calls") or []
            if tc_list:
                args = tc_list[0].get("arguments") or {}
                intent = args.get("intent", "action")
                if intent in ("action", "query", "rewrite", "undo"):
                    return intent
        except Exception as exc:
            logger.warning("意图分类结果解析失败，降级为 action: %s", exc)

        return "action"

    def _map_travel_destination(self, player_action: dict, state: dict | None = None) -> str | None:
        if player_action.get("type") != "freeform" or player_action.get("intent_hint") != "map_travel":
            return None
        state = state if state is not None else self.current_state or {}
        current = (state.get("player") or {}).get("location")
        names = state.get("display_names") or {}
        target = player_action.get("target_id")
        for location in state.get("visible_locations") or []:
            if (location != current and (target is None or target == location)
                    and player_action.get("text") == f"前往{names.get(location) or location}"):
                return location
        return None

    async def _dispatch_meta_action(self, player_action: dict,
                                    preflight_model_calls: dict | None = None,
                                    on_query_event=None) -> tuple[str, dict] | None:
        if player_action.get("type") in ("choice", "system") or not self._agentic_unified_enabled():
            return None
        action_text = player_action.get("text", "")
        query_kwargs = {"on_event": on_query_event} if on_query_event is not None else {}
        if player_action.get("type") == "freeform":
            if player_action.get("intent_hint") == "explicit_action":
                return None
            if player_action.get("intent_hint") == "explicit_query":
                return "query", await self._agent_query(action_text, **query_kwargs)
        if self._map_travel_destination(player_action):
            return None
        if player_action.get("type") == "freeform" and player_action.get("intent_hint") == "map_npc_search":
            state = self.current_state or {}
            current_location = (state.get("player") or {}).get("location")
            names = state.get("display_names") or {}
            contacts = state.get("npc_contacts") or {}
            companions = state.get("companions") or []
            if current_location and any(
                isinstance(npc, dict) and npc.get("known") is not False and npc.get("met") is not False
                and npc_id not in companions and contacts.get(npc_id) is not True
                and self._get_npc_location(npc_id, state) == current_location
                and action_text == f"在{names.get(current_location) or current_location}寻找{names.get(npc_id) or npc.get('name') or npc_id}，尝试与其接触"
                for npc_id, npc in (state.get("npcs") or {}).items()
            ):
                return None
        if (player_action.get("type") == "freeform" and player_action.get("intent_hint") == "preset_action"
                and action_text in {"等待1小时，观察周围变化", "找一个安全的地方休息到天亮"}):
            return None
        intent = await self._quick_intent_check(action_text, preflight_model_calls)
        if self._abort_flag:
            return None
        if intent == "rewrite":
            result = await self._agent_rewrite(action_text)
        elif intent == "query":
            result = await self._agent_query(action_text, **query_kwargs)
        elif intent == "undo":
            result = await self.undo_last_turn()
        else:
            return None
        logger.info("意图预判: %s", intent)
        return intent, result

    async def process_action(self, player_action: dict, *, on_background_drained=None) -> dict:
        """Process a player action and return the result.

        8-stage pipeline: plot_decision → env‖char → compose → npc‖world → choices.
        Falls back to legacy 3-step if new pipeline encounters critical errors.
        """
        if not self.ai_provider:
            raise RuntimeError("AI provider not configured")
        turn_started = _time.monotonic()
        await self._drain_background_tasks(timeout=None, include_index=False)
        self._consume_script_blueprint()
        background_ms = int((_time.monotonic() - turn_started) * 1000)
        if on_background_drained is not None:
            on_background_drained()

        preflight_model_calls = {"intent": 0, "route": 0}
        intent_started = _time.monotonic()
        meta_action = await self._dispatch_meta_action(player_action, preflight_model_calls)
        intent_ms = int((_time.monotonic() - intent_started) * 1000)
        if meta_action is not None:
            return meta_action[1]

        rollback_turn = self.turn_number
        rollback_state = None
        try:
            context_started = _time.monotonic()
            ctx = self._prepare_turn(player_action)
            rollback_state = ctx["rollback_state"]
            ctx["preflight_model_calls"] = preflight_model_calls
            ctx["preflight_timings_ms"] = {
                "background": background_ms,
                "intent": intent_ms,
                "context": int((_time.monotonic() - context_started) * 1000),
            }

            route = await self._enrich_and_route(player_action, ctx)
            ctx["prepare_ms"] = int((_time.monotonic() - turn_started) * 1000)

            # --- Stage 1→5 via shared pipeline ---
            _warnings = []
            narrative = ""
            parsed = {}
            _narrative_reasoning = ""
            if self._agentic_unified_enabled():
                pipeline = self._execute_unified_agent(ctx, route, player_action)
            elif self._agentic_enabled():
                pipeline = self._execute_agentic_pipeline(ctx, route, player_action)
            else:
                pipeline = self._execute_pipeline(ctx, route, player_action)
            async for item in pipeline:
                if item["type"] == "pipeline_result":
                    narrative = item["narrative"]
                    parsed = item["parsed"]
                    _warnings = item["warnings"]
                    _narrative_reasoning = item.get("narrative_reasoning", "")
                    break

            result = await self._apply_parsed_response(parsed, narrative, player_action, ctx)
            if _narrative_reasoning:
                result["thinking"] = _narrative_reasoning
                node = self.world_tree.get_node(self.world_tree.active_node_id)
                if node:
                    node["thinking"] = _narrative_reasoning
            if _warnings:
                result.setdefault("warnings", []).extend(_warnings)
            return result
        except Exception:
            if rollback_state is not None:
                self.current_state = rollback_state
            self.turn_number = rollback_turn
            raise

    async def process_action_stream(self, player_action: dict, *, replay_instructions: dict | None = None,
                                    on_background_drained=None):
        """Process action with streaming AI response. Yields chunks.

        8-stage pipeline: plot → env‖char → compose(streamed) → npc‖world → choices.
        注意：流式模式下的回滚由 API 层 (saving_event_stream) 统一处理，
        session 层不做 rollback，避免双重回滚导致状态混乱。
        调用方需自行在 finally 中处理 state/turn_number/world_tree 的恢复。
        """
        if not self.ai_provider:
            raise RuntimeError("AI provider not configured")
        if self._agentic_unified_enabled():
            yield {"type": "preflight", "stage": "background"}
        turn_started = _time.monotonic()
        drain_task = asyncio.create_task(self._drain_background_tasks(timeout=None, include_index=False))
        stop_task = asyncio.create_task(self._agent_stop_event.wait())
        try:
            wait_tasks = {drain_task, stop_task}
            while not drain_task.done():
                done, _ = await asyncio.wait(
                    wait_tasks, timeout=_STREAM_WAIT_HEARTBEAT_SECONDS / 2,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    if self._abort_flag:
                        drain_task.cancel()
                        break
                    wait_tasks.discard(stop_task)
                if not done:
                    yield {"type": "heartbeat"}
            if not self._abort_flag:
                await drain_task
        finally:
            stop_task.cancel()
            if not drain_task.done():
                drain_task.cancel()
            await asyncio.gather(drain_task, stop_task, return_exceptions=True)
        background_ms = int((_time.monotonic() - turn_started) * 1000)
        if not self._abort_flag:
            self._consume_script_blueprint()
        if on_background_drained is not None and not self._abort_flag:
            on_background_drained()

        preflight_model_calls = {"intent": 0, "route": 0}
        if self._abort_flag:
            yield {"type": "aborted", "label": "准备阶段"}
            yield {"type": "agent_controls_closed"}
            return
        if self._agentic_unified_enabled():
            yield {"type": "preflight", "stage": "intent"}
        intent_started = _time.monotonic()
        progress = asyncio.Queue()
        query_active = False

        async def run_meta_action():
            try:
                result = await self._dispatch_meta_action(
                    player_action, preflight_model_calls,
                    on_query_event=lambda event: progress.put_nowait(("event", event)),
                )
                progress.put_nowait(("result", result))
            except Exception as exc:
                progress.put_nowait(("error", exc))

        meta_task = asyncio.create_task(run_meta_action())
        try:
            while True:
                progress_task = asyncio.create_task(progress.get())
                stop_task = asyncio.create_task(self._agent_stop_event.wait())
                stopped = False
                heartbeat = False
                try:
                    completed, _ = await asyncio.wait(
                        {progress_task, stop_task}, timeout=_STREAM_WAIT_HEARTBEAT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    stopped = stop_task in completed and self._abort_flag
                    if stopped:
                        meta_task.cancel()
                    elif progress_task in completed:
                        kind, value = await progress_task
                    else:
                        heartbeat = True
                finally:
                    progress_task.cancel()
                    stop_task.cancel()
                    await asyncio.gather(progress_task, stop_task, return_exceptions=True)
                if stopped:
                    await asyncio.gather(meta_task, return_exceptions=True)
                    yield {"type": "aborted", "label": "查询" if query_active else "意图识别"}
                    yield {"type": "agent_controls_closed"}
                    return
                if heartbeat:
                    yield {"type": "heartbeat"}
                    continue
                if kind == "event":
                    if value.get("type") == "preflight" and value.get("stage") == "query":
                        query_active = True
                    yield value
                elif kind == "error":
                    raise value
                else:
                    meta_action = value
                    break
        finally:
            if not meta_task.done():
                meta_task.cancel()
            await asyncio.gather(meta_task, return_exceptions=True)
        if self._abort_flag:
            yield {"type": "aborted", "label": "意图识别"}
            yield {"type": "agent_controls_closed"}
            return
        intent_ms = int((_time.monotonic() - intent_started) * 1000)
        if meta_action is not None:
            intent, result = meta_action
            if not result or result.get("error") or result.get("ok") is False:
                raise ValueError((result or {}).get("error", "无法完成行动"))
            yield {"type": "agent_controls_closed"}
            # 包装为 final 事件，确保 API 层能识别
            node = self.world_tree.get_node(self.world_tree.active_node_id)
            if intent != "undo":
                yield {"type": "text", "content": result.get("narrative", result.get("reply", ""))}
            yield {
                "type": "final",
                "node_id": self.world_tree.active_node_id,
                "narrative": result.get("narrative", result.get("reply", "")),
                "choices": node.get("choices_presented", []) if node else [],
                "state": self.current_state,
                "state_changes": [],
                "is_rewrite": intent == "rewrite",
                "is_query": intent == "query",
                "is_undo": intent == "undo",
                "removed_node_id": result.get("removed_node_id"),
                "swipe_index": node.get("active_swipe_index", 0) if node else 0,
                "total_swipes": len(node.get("swipes", [])) or 1 if node else 1,
            }
            return

        agent_controls_active = self._agentic_enabled() or self._agentic_unified_enabled()
        if agent_controls_active:
            yield {"type": "agent_controls_ready"}

        context_started = _time.monotonic()
        ctx = self._prepare_turn(player_action)
        ctx["preflight_model_calls"] = preflight_model_calls
        ctx["preflight_timings_ms"] = {
            "background": background_ms,
            "intent": intent_ms,
            "context": int((_time.monotonic() - context_started) * 1000),
        }
        if replay_instructions:
            ctx.update(copy.deepcopy(replay_instructions))

        unified_stream = self._agentic_unified_enabled()
        overlap_unified = (unified_stream and
                           getattr(self._execute_unified_agent, "__func__", None)
                           is AgenticMixin._execute_unified_agent)
        if unified_stream:
            yield {"type": "preflight", "stage": "route_retrieval"}
        route = {}

        async def prepare_route():
            if overlap_unified:
                ctx["_preflight_route_interval"]["start"] = _time.monotonic()
            resolved = await self._enrich_and_route(player_action, ctx)
            if overlap_unified:
                ctx["_preflight_route_interval"]["end"] = _time.monotonic()
            route.update(resolved)
            ctx["prepare_ms"] = int((_time.monotonic() - turn_started) * 1000)
            return resolved

        route_task = asyncio.create_task(prepare_route())
        if overlap_unified:
            ctx["_preflight_route_interval"] = {}
            ctx["_preflight_route_task"] = route_task
        else:
            stop_task = asyncio.create_task(self._agent_stop_event.wait())
            try:
                while True:
                    completed, _ = await asyncio.wait(
                        {route_task, stop_task}, timeout=_STREAM_WAIT_HEARTBEAT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if completed:
                        break
                    yield {"type": "heartbeat"}
                if self._abort_flag:
                    route_task.cancel()
                else:
                    await route_task
            finally:
                stop_task.cancel()
                if not route_task.done():
                    route_task.cancel()
                await asyncio.gather(route_task, stop_task, return_exceptions=True)
        if self._abort_flag:
            if overlap_unified:
                route_task.cancel()
                await asyncio.gather(route_task, return_exceptions=True)
                ctx.pop("_preflight_route_task", None)
            yield {"type": "aborted", "label": "检索准备"}
            if agent_controls_active:
                yield {"type": "agent_controls_closed"}
            return

        # --- Stage 1→5 via shared pipeline (streaming) ---
        _warnings = []
        narrative = ""
        parsed = {}
        _narrative_reasoning = ""
        _token_usage = None
        _proactive_hint = ""
        _agent_trace = None
        if unified_stream:
            pipeline = self._execute_unified_agent(ctx, route, player_action, streaming=True)
        elif self._agentic_enabled():
            pipeline = self._execute_agentic_pipeline(ctx, route, player_action, streaming=True)
        else:
            pipeline = self._execute_pipeline(ctx, route, player_action, streaming=True)
        try:
            async for item in pipeline:
                if item["type"] == "pipeline_result":
                    narrative = item["narrative"]
                    parsed = item["parsed"]
                    _warnings = item["warnings"]
                    _narrative_reasoning = item.get("narrative_reasoning", "")
                    _token_usage = item.get("token_usage")
                    _proactive_hint = item.get("proactive_hint", "")
                    _agent_trace = item.get("agent_trace")
                    if agent_controls_active:
                        yield {"type": "agent_controls_closed"}
                else:
                    if item["type"] == "agent_plan" and "plan_event_ms" not in ctx:
                        ctx["plan_event_ms"] = int((_time.monotonic() - turn_started) * 1000)
                    if (item["type"] in ("text", "narrative_revised")
                            and "first_text_ms" not in ctx
                            and re.sub(r"<reflect>.*?</reflect>", "", item.get("content") or "",
                                       flags=re.DOTALL | re.IGNORECASE).strip()):
                        ctx["first_text_ms"] = int((_time.monotonic() - turn_started) * 1000)
                    if item["type"] == "aborted":
                        if overlap_unified and not route_task.done():
                            route_task.cancel()
                            await asyncio.gather(route_task, return_exceptions=True)
                    yield item  # thinking/text/narrative_revised
                    if item["type"] == "aborted":
                        if agent_controls_active:
                            yield {"type": "agent_controls_closed"}
                        return
            if overlap_unified:
                await route_task
        finally:
            try:
                await pipeline.aclose()
            finally:
                if overlap_unified:
                    ctx.pop("_preflight_route_task", None)
                    ctx.pop("_preflight_route_interval", None)
                    if not route_task.done():
                        route_task.cancel()
                    await asyncio.gather(route_task, return_exceptions=True)

        result = await self._apply_parsed_response(
            parsed, narrative, player_action, ctx
        )
        if _narrative_reasoning:
            result["thinking"] = _narrative_reasoning
            node = self.world_tree.get_node(self.world_tree.active_node_id)
            if node:
                node["thinking"] = _narrative_reasoning
        if _warnings:
            result.setdefault("warnings", []).extend(_warnings)

        # Scene image generation — only when route decides visual change is significant
        should_gen_image = ctx.get("route", {}).get("generate_image", False)
        # agentic 模式可通过 set_scene_image_prompt 工具直接提供 prompt，跳过 LLM 构建
        agentic_img = ctx.get("scene_image_prompt")
        if agentic_img and getattr(self, "_image_provider", None):
            try:
                img_prompt = agentic_img.get("prompt", "")
                if img_prompt:
                    scene_img = await self._image_provider.generate_image(img_prompt)
                    scene_img["_prompt"] = img_prompt
                    result["scene_image"] = scene_img
                else:
                    result["scene_image"] = None
            except Exception as e:
                logger.warning("Scene image generation failed (agentic prompt): %s", e)
                result["scene_image"] = None
        elif should_gen_image and getattr(self, "_image_provider", None) and narrative:
            try:
                from ai.image_prompt_builder import build_image_prompt
                loc = self.current_state.get("player", {}).get("location", "")
                loc_data = self._location_by_id.get(loc, {})
                loc_name = loc_data.get("name", loc) if loc_data else loc
                loc_desc = loc_data.get("description", "") if loc_data else ""
                mood = ctx.get("mood", "tense")
                tod = self.current_state.get("time_of_day", "day")
                weather = self.current_state.get("current_weather", "")

                characters = []
                pc = self.script.get("player_character", {})
                pc_app = pc.get("appearance") or pc.get("bio") or ""
                if pc_app:
                    characters.append(f"Player: {pc_app[:200]}")
                npc_states = self.current_state.get("npcs", {})
                for nid, ns in list(npc_states.items())[:5]:
                    if isinstance(ns, dict) and ns.get("current_location") == loc:
                        npc_def = self._npc_by_id.get(nid, {})
                        desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                        name = ns.get("name") or npc_def.get("name", nid)
                        if desc:
                            characters.append(f"{name}: {desc[:150]}")

                image_style = ""
                try:
                    from api.config_routes import get_image_style
                    style_data = await get_image_style()
                    image_style = style_data.get("custom") or style_data.get("preset") or ""
                except Exception:
                    pass

                img_prompt = await build_image_prompt(
                    narrative, loc_name, mood, tod, self.ai_provider,
                    weather=weather, characters=characters,
                    location_desc=loc_desc, image_style=image_style,
                )
                scene_img = await self._image_provider.generate_image(img_prompt)
                scene_img["_prompt"] = img_prompt
                result["scene_image"] = scene_img
            except Exception as e:
                logger.warning("Scene image generation failed: %s", e)
                result["scene_image"] = None
        else:
            result["scene_image"] = None

        yield {
            "type": "final",
            **result,
            **({"token_usage": _token_usage} if _token_usage else {}),
            **({"proactive_hint": _proactive_hint} if _proactive_hint else {}),
            **({"agent_trace": _agent_trace} if _agent_trace else {}),
        }

    async def branch_to_node(self, node_id: str) -> dict | None:
        """Switch to a different branch by loading a past node's state."""
        await self._drain_background_tasks()
        node = self.world_tree.get_node(node_id)
        if not node:
            return None
        # Load snapshot from DB if it was evicted from memory
        snapshot = node.get("state_snapshot")
        if not snapshot:
            snapshot = await self._load_snapshot_from_db(node_id)
            if snapshot:
                node["state_snapshot"] = snapshot

        # B2: 如果快照为 None 且 DB 也加载不到，返回错误而非使用空 dict
        if not snapshot:
            return {"error": "状态快照已丢失且无法从数据库恢复，无法切换分支"}

        # 来源哈希校验：记录并校验分支来源状态
        expected_hash = node.get("_state_hash", "")
        actual_hash = WorldTree.state_hash(snapshot)
        if expected_hash and expected_hash != actual_hash:
            logger.warning("分支来源校验不匹配: node=%s expected=%s actual=%s",
                           node_id, expected_hash, actual_hash)

        self.world_tree.set_active_node(node_id)
        self.current_state = copy.deepcopy(snapshot)
        self._hydrate_branch_runtime(self.current_state)
        self._restore_session_models_from_state()
        self.turn_number = node.get("turn_number", 0)
        self._discard_stale_script_blueprint()
        # 清理 agentic 特有缓存
        self._stable_prefix = None
        self._narrative_buffer = []
        return {
            "node_id": node_id,
            "narrative": node.get("ai_response", ""),
            "choices": node.get("choices_presented", []),
            "state": self.current_state,
        }

    async def _load_snapshot_from_db(self, node_id: str) -> dict | None:
        """Load a state_snapshot from the database for an evicted node."""
        try:
            from db.database import get_db
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT state_snapshot FROM tree_nodes WHERE id = ?", (node_id,)
                )
                row = await cursor.fetchone()
                if row and row["state_snapshot"]:
                    snapshot = json.loads(row["state_snapshot"])
                    # 提取 _node_context 到 node dict（如果有），不留在 state 中
                    nc = snapshot.pop("_node_context", None)
                    node = self.world_tree.get_node(node_id)
                    if nc and isinstance(nc, dict) and node:
                        node.setdefault("check_result", nc.get("check_result"))
                        node.setdefault("triggered_consequences", nc.get("triggered_consequences"))
                        node.setdefault("achieved_milestones", nc.get("achieved_milestones"))
                    return snapshot
        except Exception:
            pass
        return None

    def set_authors_note(self, note: str, position: str = "end", depth: int = 4):
        """Set the player's behind-the-scenes directive."""
        self.authors_note = note
        self.authors_note_position = position
        self.authors_note_depth = max(1, depth)


    async def undo_last_turn(self) -> dict | None:
        """Flow#7: 撤销最近一次行动，回到父节点状态。"""
        await self._drain_background_tasks()
        active_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not active_node:
            return None
        parent_id = active_node.get("parent_id")
        if not parent_id:
            return {"error": "已经是开局节点，无法撤销"}

        parent = self.world_tree.get_node(parent_id)
        if not parent:
            return {"error": "父节点不存在"}

        # 加载父节点状态快照
        snapshot = parent.get("state_snapshot")
        if not snapshot:
            snapshot = await self._load_snapshot_from_db(parent_id)
            if snapshot:
                parent["state_snapshot"] = snapshot
        if not snapshot:
            return {"error": "父节点状态快照已丢失，无法撤销"}

        # B4: 先移除当前节点（叶节点），再切到父节点；处理移除失败
        removed_id = active_node["id"]
        if not self.world_tree.remove_node(removed_id):
            return {"error": "无法移除当前节点（可能不是叶节点）"}
        self.world_tree.set_active_node(parent_id)

        self.current_state = copy.deepcopy(snapshot)
        self._hydrate_branch_runtime(self.current_state)
        self._restore_session_models_from_state()
        self.turn_number = parent.get("turn_number", 0)
        self._discard_stale_script_blueprint()
        # 清理 agentic 特有缓存
        self._stable_prefix = None
        self._narrative_buffer = []
        return {
            "node_id": parent_id,
            "removed_node_id": removed_id,
            "narrative": parent.get("ai_response", ""),
            "choices": parent.get("choices_presented", []),
            "state": self.current_state,
        }


    # ── Stage 4 状态工具执行器 & 合并器 ──

    _REJECT_STATE_PATTERNS_TOOL = (
        "weather", "天气", "时段", "time_period", "dawn", "dusk",
        "morning", "afternoon", "night", "noon", "黎明", "黄昏",
        "上午", "午后", "夜晚", "深夜", "日出", "日落",
    )
    _REJECT_WP_PATTERNS_TOOL = (
        "lamp", "light", "door", "window", "clock", "alarm",
        "desk", "chair", "phone", "radio", "tv", "灯", "门",
        "窗", "桌", "椅", "电话", "闹钟", "台灯", "scattered",
        "curtain", "drawer", "paper", "document", "书桌", "抽屉",
    )

    def _is_undeclared_object_property(self, property_id: str) -> bool:
        if any(isinstance(definition, dict) and definition.get("id") == property_id
               for definition in self.script.get("world_properties", [])):
            return False
        return any(pattern in property_id.lower() for pattern in self._REJECT_WP_PATTERNS_TOOL)

    def _is_undeclared_environment_state(self, entry: str | dict) -> bool:
        if isinstance(entry, dict):
            state_id = entry.get("id", "")
            state_name = entry.get("name") or ""
            description = entry.get("description") or ""
        else:
            state_id, state_name, description = entry, "", ""
        if state_id in self._ps_by_id:
            return False
        if not state_name or state_name == state_id or (state_name.isascii() and "_" in state_name):
            state_name += description[:10]
        identifier = (state_id + state_name).lower()
        return any(pattern in identifier for pattern in self._REJECT_STATE_PATTERNS_TOOL)

    def _project_state_tool_updates(self, updates: list[dict], *, spatial_feedback: dict | None = None) -> dict:
        projected = copy.deepcopy(self.current_state)
        dirty_npc_ids: set[str] = set()
        resource_log = []
        spatial_changes = {"npc_location_changes": [], "room_changes": []}
        location_change = None
        requested_location = None
        inventory_changes = []
        for update in updates:
            requested_location = update.get("location_change") or requested_location
            parsed = self._merge_state_tool_results(self._explode_update_state(update), state=projected)
            if parsed.get("location_change"):
                location_change = parsed["location_change"]
            inventory_changes.extend(parsed.get("inventory_changes", []))
            for key in spatial_changes:
                spatial_changes[key].extend(parsed.get(key, []))
            changes = parsed.get("state_changes", [])
            for change in changes:
                if change.get("type") == "variable":
                    self.script_variables.apply_op(
                        projected, change.get("id", ""), change.get("op", "set"), change.get("value"),
                    )
            regular = []
            for change in changes:
                if change.get("type") == "variable":
                    continue
                op = change.get("op", "add")
                if op in ("add", "subtract") and not change.get("reason"):
                    continue
                regular.append(change)
                target = change.get("target", "")
                if target == "player.relationships":
                    dirty_npc_ids.update(projected.get("player", {}).get("relationships", {}))
                elif target.startswith("player.relationships."):
                    dirty_npc_ids.add(target.split(".")[2])
                elif target.startswith("npcs."):
                    dirty_npc_ids.add(target.split(".")[1])
            if regular:
                projected, log = self._apply_resource_state_changes(projected, regular, inplace=True)
                resource_log.extend(log)
                if any(change.get("target") == "player.relationships" for change in regular):
                    dirty_npc_ids.update(projected.get("player", {}).get("relationships", {}))
            active_states = projected.setdefault("active_persistent_states", [])
            for entry in parsed.get("activate_states", []):
                state_id = entry.get("id") if isinstance(entry, dict) else entry
                if state_id and state_id not in active_states:
                    active_states.append(state_id)
            for state_id in parsed.get("deactivate_states", []):
                if state_id in active_states:
                    active_states.remove(state_id)
            for prop in parsed.get("world_property_changes", []):
                projected.setdefault("world_properties", {})[prop["id"]] = prop.get("value")
            for entry in parsed.get("reveal_locations", []):
                location_id = entry.get("id", "") if isinstance(entry, dict) else entry
                if location_id and not self._is_duplicate_location(projected, location_id):
                    projected = self.state_manager.reveal_location(projected, location_id, inplace=True)
                    location_name = (entry.get("name", location_id) if isinstance(entry, dict)
                                     else self._location_by_id.get(location_id, {}).get("name", location_id))
                    projected.setdefault("display_names", {})[location_id] = location_name
            if parsed.get("world_property_changes"):
                self._update_dynamic_connections(projected)
        self._clear_derived_npc_positions(projected, resource_log)
        self._apply_player_location_change(projected, location_change)
        for change in inventory_changes:
            item_name = change.get("item", "")
            if not item_name:
                continue
            inventory = projected.setdefault("inventory", [])
            entry = next((item for item in inventory if item.get("item") == item_name), None)
            quantity = change.get("quantity", 1)
            if change.get("action", "add") == "add":
                if entry:
                    entry["quantity"] = entry.get("quantity", 1) + quantity
                    if change.get("description"):
                        entry["description"] = change["description"]
                else:
                    new_entry = {"item": item_name, "quantity": quantity}
                    if change.get("description"):
                        new_entry["description"] = change["description"]
                    inventory.append(new_entry)
            elif change.get("action") == "remove" and entry:
                entry["quantity"] = entry.get("quantity", 1) - quantity
                if entry["quantity"] <= 0:
                    inventory.remove(entry)
        self._apply_entity_spatial_changes(projected, spatial_changes)
        self._sync_dirty_npc_attitudes(projected, dirty_npc_ids)
        if spatial_feedback is not None:
            destination = location_change or requested_location
            player = projected.get("player", {})
            blocked = projected.get("location_access_blocked", {})
            status = "not_applied" if destination else "none"
            if location_change and self._can_use_location_id(projected, location_change):
                status = "blocked" if blocked.get("location_id") == location_change else "ready"
            spatial_feedback.update({
                "requested_location_id": destination, "location_id": player.get("location", ""),
                "room_id": player.get("current_room", ""), "movement_status": status,
            })
            if destination:
                spatial_feedback["rule_query"] = {"category": "locations", "query": destination}
            if status == "blocked":
                reason = str(blocked.get("reason", ""))
                spatial_feedback.update({"reason": reason[:500], "reason_truncated": len(reason) > 500})
        return projected

    def _validate_end_time(self, end_time: str, ctx: dict, location_change: str | None = None,
                           pending_changes: dict | None = None) -> str:
        old_time = ctx.get("old_time", "")
        start = parse_time(old_time)
        finish = parse_time(end_time)
        if not isinstance(end_time, str) or "T" not in end_time or not finish:
            return f"end_time 格式错误: {end_time}"
        if not start:
            return f"当前游戏时间无法解析: {old_time}"
        if ctx.get("is_system_opening"):
            if finish != start:
                return f"系统开场不推进时间，end_time 必须为 {old_time}"
            return ""
        minutes = (finish - start).total_seconds() / 60
        if minutes <= 0:
            return f"end_time 不得早于或等于当前时间 {old_time}"
        requested_wait_minutes = ctx.get("requested_wait_minutes")
        if requested_wait_minutes is None and ctx.get("action_text") == "等待1小时，观察周围变化":
            requested_wait_minutes = 60
        if requested_wait_minutes and minutes < requested_wait_minutes:
            interrupted = False
            for event in ctx.get("triggered_events", []):
                event_time = parse_time(event.get("fire_time"))
                if (event_time and start < event_time <= finish
                        and is_public_event(self.script, event.get("event_id", ""))):
                    interrupted = True
                    break
            if not interrupted:
                wait_label = (f"{requested_wait_minutes // 60}小时" if requested_wait_minutes % 60 == 0
                              else f"{requested_wait_minutes}分钟")
                return (f"玩家要求等待{wait_label}，未发生中断事件时不能只推进 {minutes:.0f} 分钟；"
                        f"请将 end_time 推进到至少 {ctx['new_time']}")
        travel_minutes, destination = self._travel_minutes_for_updates(ctx, location_change, pending_changes)
        if travel_minutes and minutes < travel_minutes:
            return f"到达 {destination} 至少需要 {travel_minutes} 分钟旅途，end_time 仅推进 {minutes:.0f} 分钟"
        limit = max(min(max(ctx.get("estimated_minutes", 30) * 10, 120), 720),
                    requested_wait_minutes or 0)
        if minutes > limit:
            return f"本次行动仅预估 {ctx.get('estimated_minutes', 30)} 分钟，不能推进 {minutes:.0f} 分钟；请按叙事实际进度重试"
        estimated_end = parse_time(ctx.get("new_time"))
        if estimated_end and finish > estimated_end:
            updates = [*ctx.get("_accepted_state_updates", [])]
            if pending_changes:
                updates.append(pending_changes)
            later_events = self._projected_events({
                **ctx, "_accepted_state_updates": updates, "new_time": end_time,
            })
            skipped = [event for event in later_events if parse_time(event.get("fire_time")) > estimated_end]
            if skipped:
                first = min(skipped, key=lambda event: event["fire_time"])
                return f"推进将跨过未纳入本轮推演的事件 {first['event_id']} ({first['fire_time']})；请先处理该事件"
        return ""

    def _run_state_tool(self, tool_name: str, args: dict, ctx: dict | None = None) -> str:
        """Execute a state-settlement tool call. Returns validation result as string.

        Does NOT modify state directly — caller merges via _merge_state_tool_results.
        """
        MAX_DELTA = 30

        if tool_name == "update_resources":
            issues = []
            # Validate state_changes
            for sc in args.get("state_changes", []):
                op = sc.get("op", "add")
                val = sc.get("value", 0)
                if op in ("add", "subtract") and not sc.get("reason"):
                    issues.append(f"丢弃无reason属性变更: {sc.get('target', '?')}")
                    continue
                if isinstance(val, (int, float)) and op in ("add", "subtract") and abs(val) > MAX_DELTA:
                    issues.append(f"属性变化幅度过大 {sc.get('target', '?')}: {val}, 已钳制到±{MAX_DELTA}")
            # Validate activate_states
            for entry in args.get("activate_states", []):
                if self._is_undeclared_environment_state(entry):
                    state_id = entry.get("id", "") if isinstance(entry, dict) else entry
                    issues.append(f"过滤天气/时段状态: {state_id}")
            result = "资源状态已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        if tool_name == "update_spatial":
            issues = []
            loc = args.get("location_change")
            revealed = {entry.get("id") for entry in args.get("reveal_locations", []) if isinstance(entry, dict)}
            state = (self._project_state_tool_updates(ctx["_accepted_state_updates"])
                     if ctx and ctx.get("_accepted_state_updates") else self.current_state)
            if loc and not self._can_use_location_id(state, loc, revealed):
                return f"错误: 地点 {loc} 尚未发现；请先揭示地点再移动"
            for rl in args.get("reveal_locations", []):
                if not isinstance(rl, dict) or not rl.get("id"):
                    issues.append("reveal_locations 缺少 id")
            result = "空间状态已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        if tool_name == "update_time":
            et = args.get("end_time", "")
            if ctx and ctx.get("old_time"):
                error = self._validate_end_time(et, ctx, args.get("location_change"), args)
                return f"错误: {error}" if error else f"时间状态已接收: {et}"
            if not et:
                return "错误: end_time 为空"
            # Basic ISO format check
            if "T" not in et and len(et) < 10:
                return f"警告: end_time 格式可能不正确: {et}"
            # 校验时间跳跃合理性
            current_time = self.current_state.get("game_time", "")
            if current_time and "T" in current_time and "T" in et:
                try:
                    from datetime import datetime
                    _ct = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
                    _et = datetime.fromisoformat(et.replace("Z", "+00:00"))
                    _delta_h = (_et - _ct).total_seconds() / 3600
                    if _delta_h > 4:
                        return (f"时间状态已接收: {et}（警告: 时间跳跃 {_delta_h:.1f} 小时，"
                                "请确认中间是否有重要事件被跳过。如果有，请先处理那些事件再推进时间。）")
                    if _delta_h < 0:
                        return f"时间状态已接收: {et}（警告: 时间倒退 {abs(_delta_h):.1f} 小时）"
                except (ValueError, TypeError):
                    pass
            return f"时间状态已接收: {et}"

        if tool_name == "update_world":
            issues = []
            for wp in args.get("world_property_changes", []):
                wp_id = wp.get("id", "")
                if wp_id and self._is_undeclared_object_property(wp_id):
                    issues.append(f"过滤物件级属性: {wp_id}")
            result = "世界属性已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        if tool_name == "update_extended":
            issues = []
            # NPC name→ID matching for offscreen_npc_updates
            for upd in args.get("offscreen_npc_updates", []):
                name = upd.get("name", "")
                if name and name not in self._npc_name_to_id and not any(
                    n.get("name") == name for n in self._npc_by_id.values() if isinstance(n, dict)
                ):
                    issues.append(f"离场NPC名称未匹配到已知NPC: {name}")
            result = "扩展状态已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        return f"未知状态工具: {tool_name}"

    def _merge_state_tool_results(self, tool_calls: list[dict], *, state: dict | None = None) -> dict:
        """Merge state tool call arguments into a parsed dict compatible with parse_split_v3 output.

        tool_calls: [{"name": "update_resources", "args": {...}}, ...]
        Returns a dict matching _empty_result() structure.
        """
        parsed = self.response_parser._empty_result()
        MAX_DELTA = 30
        state = self.current_state if state is None else state

        for tc in tool_calls:
            name = tc["name"]
            args = tc["args"]

            if name == "update_resources":
                # state_changes: validate and clamp
                validated = []
                for sc in args.get("state_changes", []):
                    op = sc.get("op", "add")
                    val = sc.get("value", 0)
                    if op in ("add", "subtract") and not sc.get("reason"):
                        continue
                    if isinstance(val, (int, float)) and op in ("add", "subtract") and abs(val) > MAX_DELTA:
                        sc = {**sc, "value": MAX_DELTA if val > 0 else -MAX_DELTA}
                    validated.append(sc)
                parsed["state_changes"].extend(validated)
                parsed["inventory_changes"].extend(args.get("inventory_changes", []))
                # activate_states: filter weather/time-period
                for entry in args.get("activate_states", []):
                    if self._is_undeclared_environment_state(entry):
                        continue
                    parsed["activate_states"].append(entry)
                    parsed.setdefault("_persistent_state_operations", []).append(("activate", entry))
                deactivations = args.get("deactivate_states", [])
                parsed["deactivate_states"].extend(deactivations)
                if deactivations:
                    parsed.setdefault("_persistent_state_operations", []).extend(
                        ("deactivate", state_id) for state_id in deactivations
                    )
                go = args.get("game_over")
                if go:
                    parsed["game_over"] = go

            elif name == "update_spatial":
                loc = args.get("location_change")
                parsed["reveal_locations"].extend(args.get("reveal_locations", []))
                revealed = {entry.get("id") if isinstance(entry, dict) else entry
                            for entry in parsed["reveal_locations"]}
                if loc and self._can_use_location_id(state, loc, revealed):
                    parsed["location_change"] = loc
                parsed["npc_location_changes"].extend(args.get("npc_location_changes", []))
                if args.get("room_changes"):
                    # room_changes is not in _empty_result default, add dynamically
                    parsed.setdefault("room_changes", []).extend(args["room_changes"])
                if args.get("npc_contact_changes"):
                    parsed.setdefault("npc_contact_changes", []).extend(args["npc_contact_changes"])
                sd = args.get("scene_details")
                if sd:
                    parsed["scene_details"] = sd

            elif name == "update_time":
                et = args.get("end_time")
                if et:
                    parsed["end_time"] = et

            elif name == "update_world":
                # Filter object-level properties
                for wp in args.get("world_property_changes", []):
                    wp_id = wp.get("id", "")
                    if wp_id and self._is_undeclared_object_property(wp_id):
                        continue
                    parsed["world_property_changes"].append(wp)

            elif name == "update_extended":
                parsed["offscreen_npc_updates"].extend(args.get("offscreen_npc_updates", []))
                parsed["new_npcs"].extend(args.get("new_npcs", []))
                parsed["faction_reputation_changes"].extend(args.get("faction_reputation_changes", []))
                parsed["moral_alignment_changes"].extend(args.get("moral_alignment_changes", []))
                parsed["recruit_companions"].extend(args.get("recruit_companions", []))
                parsed["dismiss_companions"].extend(args.get("dismiss_companions", []))
                parsed["invalidate_lore"].extend(args.get("invalidate_lore", []))

        return parsed

    async def _execute_state_settlement(
        self, ctx: dict, narrative_text: str, plot_decision: str,
        action_text: str, state: dict, old_time: str = "",
    ) -> dict:
        """Stage 4: 单次工具调用做状态推演（替代 5 路并行 LLM）。

        Returns a parsed dict compatible with parse_split_v3 output.
        """
        # 构建合并的 system prompt
        all_field_lines = [fdef["desc"] for fdef in self.prompt_builder._FIELD_DEFS.values()]
        system = (
            "你是游戏状态推演引擎。分析叙事文本和剧情骨架，通过工具调用更新游戏状态。\n"
            "你可以调用多个工具，每个工具负责不同的状态域。必须至少调用 update_time。\n\n"
            "各域对应工具：\n"
            "- update_resources: 属性/物品/持续状态/胜负\n"
            "- update_spatial: 位置/场景/NPC位置/房间\n"
            "- update_time: 时间推进（必调用）\n"
            "- update_world: 宏观世界属性\n"
            "- update_extended: 离场NPC/新NPC注册/阵营/道德/同伴\n\n"
            "字段参考：\n" + "\n".join(all_field_lines) + "\n\n"
            "规则：\n"
            "- 严格根据叙事中实际描写的事件推演，不推测或编造\n"
            "- 物品add限制：只能添加叙事中明确描写角色获得的物品\n"
            "- 仅[检定结果: 大成功]时可额外给予奖励；仅[检定结果: 大失败]时应施加惩罚\n"
            "- activate_states 可按精确ID激活剧本声明的持续状态；不得另造未声明的天气或时段状态\n"
            "- world_property_changes 只写宏观世界级变量，不写物件状态\n"
            "- location_change 必须使用已知地点列表中的精确ID\n"
            "- end_time 是绝对时间戳，不是时长\n"
            "- 无变化的域不需要调用对应工具\n"
        )

        # 构建合并的 user message（合并 5 个 group 的 XML 段落）
        source_for_non_temporal = plot_decision if plot_decision else narrative_text
        use_pd = bool(plot_decision)
        _check_res = ctx.get("check_result")
        _active_sys = ctx.get("route", {}).get("systems") if isinstance(ctx.get("route"), dict) else None

        # resource 段落
        resource_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="resource", use_plot_decision=use_pd,
        )
        # spatial 段落
        spatial_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="spatial", use_plot_decision=use_pd,
        )
        # temporal 段落（使用 narrative 全文）
        temporal_source = narrative_text if narrative_text else plot_decision
        temporal_pd = not bool(narrative_text)
        temporal_content = self.prompt_builder._build_world_state_user_message(
            temporal_source, action_text, state, _check_res,
            group="temporal", use_plot_decision=temporal_pd,
        )
        # world 段落
        world_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="world", use_plot_decision=use_pd,
        )
        # ext 段落
        ext_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="ext", use_plot_decision=use_pd,
            event_sections=ctx.get("event_sections"),
        )

        # 去重合并：各段落有重叠的 player_action / narrative 等，只保留一份
        # 用 XML tag 标识各段落（保留 resource/spatial/temporal/world/ext 的域特定信息）
        from collections import OrderedDict
        seen_sections = OrderedDict()
        for content in [resource_content, spatial_content, temporal_content, world_content, ext_content]:
            for block in content.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                # 用首行/tag 作为去重 key
                key = block[:60]
                if key not in seen_sections:
                    seen_sections[key] = block
        merged_content = "\n\n".join(seen_sections.values())

        # end_time 决策指引
        if old_time:
            merged_content += (
                f"\n\n## end_time 决策指引\n"
                f"当前游戏时间: {old_time}\n"
                "你是end_time的唯一决策者。根据叙事最后场景的时间输出绝对时间戳：\n"
                "- 对话/观察/翻阅文件: 当前时间 +10~30分钟\n"
                "- 常规互动/短途移动: 当前时间 +30分钟~2小时\n"
                "- 长途旅行/大型战斗: 当前时间 +2~8小时\n"
                "- 睡觉/过夜: 若叙事写到入睡那一刻则给入睡时间（如23:30），若叙事写到醒来才给次日早晨\n"
                f"格式示例: {old_time[:10] or '1970-01-01'}T10:00:00"
            )

        messages = [{"role": "user", "content": merged_content}]

        # 工具循环（复用 _stage1_with_native_tools 的模式）
        state_tool_results = []
        max_rounds = 3

        for _ in range(max_rounds):
            resp = await self.ai_provider.generate_with_tools(
                messages, system=system, tools=STATE_TOOLS_SCHEMA,
                max_tokens=4096, **self._stage_kwargs("state"),
            )
            tc_list = resp.get("tool_calls")
            if not tc_list:
                break

            assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
            reasoning = resp.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tc_list
            ]
            messages.append(assistant_msg)

            for tc in tc_list:
                result = self._run_state_tool(tc["name"], tc["arguments"])
                state_tool_results.append({"name": tc["name"], "args": tc["arguments"]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # 合并工具调用结果为 parsed 字典
        parsed = self._merge_state_tool_results(state_tool_results)
        parsed["narrative"] = narrative_text.strip() if narrative_text else ""
        return parsed


    async def _infer_initial_state(self, narrative: str = "") -> bool:
        """根据角色信息和开场叙事，AI 推演调整初始背包、持续状态和NPC认识状态。"""
        player = self.current_state.get("player", {})
        if not player.get("bio") and not player.get("personality"):
            return True

        inventory = self.current_state.get("inventory", [])
        active_ps = self.current_state.get("active_persistent_states", [])
        all_ps = self.script.get("persistent_states", [])

        ps_info = [{"id": ps["id"], "name": ps.get("name", ""),
                     "description": ps.get("description", ""),
                     "active": ps["id"] in active_ps} for ps in all_ps]

        # NPC 列表（供 AI 推演 known/met）
        npc_info = []
        for npc in self.script.get("npcs", []):
            npc_st = self.current_state.get("npcs", {}).get(npc["id"], {})
            npc_info.append({
                "id": npc["id"],
                "name": npc.get("name", npc["id"]),
                "bio": (npc.get("bio", "") or "")[:60],
                "title": npc.get("title", ""),
                "known": npc_st.get("known", npc.get("known", True)),
                "met": npc_st.get("met", npc.get("met", False)),
            })

        narrative_ctx = f"\n开场情境: {narrative[:300]}" if narrative else ""

        prompt = f"""你是一个游戏初始化助手。根据角色信息调整初始背包、持续状态和NPC认识状态。

角色信息:
- 姓名: {player.get('name', '')}
- 身份: {player.get('bio', '')}
- 性格: {player.get('personality', '')}
- 位置: {player.get('location', '')}

世界背景: {self.script.get('world_background', '')[:200]}{narrative_ctx}

当前背包: {json.dumps(inventory, ensure_ascii=False)}

可用持续状态:
{json.dumps(ps_info, ensure_ascii=False)}

NPC列表:
{json.dumps(npc_info, ensure_ascii=False)}

请根据角色身份合理调整:
1. 背包: 移除与角色身份明显不符的物品，可添加1-2件符合角色身份的物品
2. 持续状态: 根据角色背景，决定每个状态是否应该激活
3. NPC认识状态: 判断角色是否"认识"(known)和"熟识"(met)各NPC:
   - known=true: 角色知道此人（同事、名人、同学等），能叫出名字
   - met=true: 角色与此人有过直接接触/交流，会显示好感度
   - 一个角色可以known但未met（知道但没见过面）

返回JSON:
{{"inventory": [{{"item":"物品名","quantity":数量}}], "active_persistent_states": ["状态ID"], "npc_known": {{"npc_id": {{"known": true, "met": false}}}}}}
npc_known 中只需包含需要改变的NPC（与当前状态不同的）。只返回JSON。"""

        if self.authors_note:
            prompt += f"\n\n[创作指令（影响初始状态的设定偏好）]\n{self.authors_note}"

        raw = await self.ai_provider.generate(
            [{"role": "user", "content": prompt}],
            system="你是游戏初始化助手，简洁精确，只返回JSON。",
            max_tokens=8192, **self._stage_kwargs("state"),
        )
        raw = strip_think_tags(raw)
        parsed = self._extract_json(raw)
        if not parsed:
            return False
        if "inventory" in parsed and isinstance(parsed["inventory"], list):
            self.current_state["inventory"] = parsed["inventory"]
        if "active_persistent_states" in parsed and isinstance(parsed["active_persistent_states"], list):
            valid_ids = {ps["id"] for ps in all_ps}
            self.current_state["active_persistent_states"] = [
                sid for sid in parsed["active_persistent_states"] if sid in valid_ids
            ]
        # 应用 NPC known/met 变更
        npc_known = parsed.get("npc_known", {})
        if isinstance(npc_known, dict):
            npcs_state = self.current_state.setdefault("npcs", {})
            valid_npc_ids = {n["id"] for n in self.script.get("npcs", [])}
            for npc_id, vals in npc_known.items():
                if npc_id not in valid_npc_ids or not isinstance(vals, dict):
                    continue
                st = npcs_state.setdefault(npc_id, {})
                if "known" in vals:
                    st["known"] = bool(vals["known"])
                if "met" in vals:
                    st["met"] = bool(vals["met"])
        return True

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从 AI 输出中提取 JSON 对象（去除 code fences 后委托 _parse_simple_json）。"""
        stripped = text.strip()
        m = re.match(r'^```(?:json|JSON)?\s*\n?([\s\S]*?)\n?\s*```\s*$', stripped)
        if m:
            stripped = m.group(1).strip()
        return GameSession._parse_simple_json(stripped)

    async def _personalize_narrative(self, base_text: str) -> str | None:
        """AI 润色/重写开场叙事，返回润色后文本或 None。"""
        player = self.current_state.get("player", {})
        name = player.get("name", "")
        bio = player.get("bio", "")
        personality = player.get("personality", "")
        portrait = player.get("portrait_desc", "")
        location = player.get("location", "")
        long_term_goal = player.get("long_term_goal", "")
        world_bg = self.script.get("world_background", "")[:500]

        # 随机氛围标签，增加重复游玩多样性
        mood_tags = ["紧迫", "悠闲", "神秘", "幽默", "压抑", "温馨", "紧张", "荒诞", "庄严", "活泼"]
        mood = random.choice(mood_tags)

        npcs_present = []
        npcs_elsewhere = []
        for npc in self.script.get("npcs", []):
            npc_name = npc.get("name", npc["id"])
            npc_personality = npc.get("personality", "")
            npc_label = f"{npc_name}（{npc_personality}）" if npc_personality else npc_name
            npc_loc = npc.get("default_location", "")
            if not npc_loc or not location:
                npcs_present.append(npc_label)
            elif self._locations_match(location, npc_loc):
                npcs_present.append(npc_label)
            else:
                npcs_elsewhere.append(f"{npc_name}(在{npc_loc})")
        npc_presence = ""
        if npcs_present:
            npc_presence += f"\n- 在场NPC: {', '.join(npcs_present)}"
        if npcs_elsewhere:
            npc_presence += f"\n- 不在场NPC（不要出现在开场中）: {', '.join(npcs_elsewhere)}"

        # 声纹速查表：帮助区分在场NPC的说话风格
        voice_table = ""
        if npcs_present:
            voice_lines = []
            for npc in self.script.get("npcs", []):
                npc_name = npc.get("name", npc["id"])
                npc_personality = npc.get("personality", "")
                npc_loc = npc.get("default_location", "")
                if npc_loc and location and not self._locations_match(location, npc_loc):
                    continue
                hint = self.prompt_builder._derive_voice_hint(npc_personality)
                if hint:
                    voice_lines.append(f"  - {npc_name}: {hint}")
            if voice_lines:
                voice_table = "\n\nNPC说话风格（每个NPC的对话必须体现其独有风格，严禁雷同）:\n" + "\n".join(voice_lines)

        is_preset = bool(self.script.get("_preset_opening_text"))
        if is_preset:
            rewrite_guidance = (
                "这是剧本作者为该角色量身编写的专属开局。请在保留原文核心内容和风格的基础上，"
                "润色文笔、补充感官细节和环境氛围，使其更加生动。不要改变场景设定和情节走向。"
            )
        else:
            rewrite_guidance = (
                "如果角色的位置或身份与原始开场不匹配，请根据角色信息重新构建开场场景。\n"
                "如果匹配度高，可以在原始开场基础上融入角色特征。"
            )

        narrative_prompt = f"""你是一个文字游戏GM。请为这个角色生成贴合其身份和所在位置的开场描述。

{rewrite_guidance}

原始开场（{'请在此基础上润色' if is_preset else '仅供参考，可大幅改写'}）:
{base_text}

角色信息:
- 姓名: {name}
- 身份: {bio}
- 性格: {personality}
- 外貌: {portrait}
- 起始位置: {location}
- 长期目标: {long_term_goal}{npc_presence}

世界背景: {world_bg}{voice_table}

要求:
1. 用第二人称"你"描述，400-800字
2. 场景必须发生在角色的起始位置，自然描述其所处环境
3. 融入角色的性格、身份、外貌特征，让开局是为这个角色量身定做的
4. 角色的言行和内心活动要贴合其性格（活泼的角色不要写成冷静严肃的）
5. 在场的NPC可以出现在开场描述中，不在场的NPC不要出现
6. 角色对话使用中文弯引号 "…" 包裹
7. 本次开场的整体氛围倾向：{mood}（自然融入，不要生硬点明）
只输出润色后的叙事文本，不要输出选项或JSON，不要输出思考过程。"""

        if self.authors_note:
            narrative_prompt += f"\n\n[创作指令（在叙事中体现但不要直接提及）]\n{self.authors_note}"

        messages = [{"role": "user", "content": narrative_prompt}]
        raw_narrative = await self.ai_provider.generate(
            messages,
            system="你是一个创意写作助手。只输出叙事文本，不输出选项/JSON/思考过程。",
            **self._stage_kwargs("narrative"),
        )
        raw_narrative = strip_think_tags(raw_narrative)
        narrative = raw_narrative.strip()
        narrative = re.sub(r'\n```(?:game_state|json)?\s*\{.*$', '', narrative, flags=re.DOTALL).strip()
        narrative = re.sub(r'\n```\s*$', '', narrative).strip()
        return narrative or None

    async def _personalize_choices(self, narrative: str, base_choices: list) -> list | None:
        """基于润色后叙事，AI 润色开场选项。返回选项列表或 None。"""
        choices_lines = []
        for i, c in enumerate(base_choices):
            line = f"- open_{i}: {c['text']}"
            choices_lines.append(line)
        choices_text = "\n".join(choices_lines) if choices_lines else "（无固定选项）"
        choices_system = (
            "你是游戏选项设计师。请根据开场叙事润色已有的开场选项。\n"
            "- 保留核心意图方向，但措辞和情境应贴合角色\n"
            "- 必须保留原选项数量和 id（open_0, open_1, ... 一一对应）\n"
            "- hint 只提示行动前已知的风险、代价或耗时，不得预告 NPC 回应或确定结果；time_hint 使用 ISO 8601（如PT30M）\n"
            "- 每个选项必须有 risk（safe/moderate/risky）\n"
            "- 当选项有前置条件（属性/物品等）且不满足时，设置 locked:true 和 lock_reason\n"
            '只返回 JSON: {"choices":[{"id":"open_0","text":"...","hint":"...","time_hint":"PT30M","risk":"moderate"},...]}'
        )

        # 构建资源状态摘要
        state_parts = []
        player = self.current_state.get("player", {})
        pc_bits = []
        if player.get("name"):
            pc_bits.append(player["name"])
        if player.get("personality"):
            pc_bits.append(f"性格:{player['personality'][:15]}")
        if player.get("long_term_goal"):
            pc_bits.append(f"目标:{player['long_term_goal'][:25]}")
        if pc_bits:
            state_parts.append(f"主角: {' | '.join(pc_bits)}")
        attrs = player.get("attributes", {})
        attr_bits = []
        for attr_name, attr_val in attrs.items():
            v = attr_val if isinstance(attr_val, (int, float)) else (attr_val.get("value") if isinstance(attr_val, dict) else None)
            if v is not None:
                dn = attr_val.get("display_name", attr_name) if isinstance(attr_val, dict) else attr_name
                attr_bits.append(f"{dn}={v}")
        if attr_bits:
            state_parts.append(f"属性: {' | '.join(attr_bits)}")
        inventory = self.current_state.get("inventory", [])
        if inventory:
            inv_items = []
            for item in inventory[:8]:
                name = item.get("item", "") if isinstance(item, dict) else str(item)
                qty = item.get("quantity", 1) if isinstance(item, dict) else 1
                if name:
                    inv_items.append(f"{name}×{qty}" if qty > 1 else name)
            if inv_items:
                state_parts.append(f"背包: {', '.join(inv_items)}")
        state_context = "\n".join(state_parts)

        choices_user = f"""开场叙事:
{narrative}

原始选项（按 id 顺序润色，不要增减、不要改 id）:
{choices_text}"""

        if state_context:
            choices_user += f"\n\n当前角色状态:\n{state_context}"

        if self.authors_note:
            choices_user += f"\n\n[创作指令（选项风格应与此一致）]\n{self.authors_note}"

        raw_choices = await self.ai_provider.generate(
            [{"role": "user", "content": choices_user}],
            system=choices_system,
            max_tokens=8192,
            **self._stage_kwargs("choices"),
        )
        raw_choices = strip_think_tags(raw_choices)
        data = self.response_parser._extract_json_from_raw(raw_choices)
        ai_choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(ai_choices, list) or not ai_choices:
            return None
        for ac in ai_choices:
            idx_str = str(ac.get("id", "")).replace("open_", "")
            try:
                idx = int(idx_str)
            except (ValueError, TypeError):
                continue
            if idx < 0 or idx >= len(base_choices):
                continue
            base = base_choices[idx]
            if not ac.get("hint") and base.get("hint"):
                ac["hint"] = base["hint"]
            if not ac.get("text") and base.get("text"):
                ac["text"] = base["text"]
            if not ac.get("time_hint") and base.get("time_hint"):
                ac["time_hint"] = base["time_hint"]
        return ai_choices

    @staticmethod
    def _build_opening_choices(opening: dict) -> list[dict]:
        """Build visible choices without exposing their scripted results."""
        choices = []
        for i, c in enumerate(opening.get("choices", [])):
            choice_data = {"id": f"open_{i}", "text": c.get("text", "")}
            for key in ("hint", "time_hint", "risk", "locked", "lock_reason"):
                if c.get(key):
                    choice_data[key] = c[key]
            result = c.get("result", {})
            if result.get("type") == "conditional":
                hint = choice_data.get("hint", "")
                choice_data["hint"] = (hint + "（结果由骰子决定）").strip() if hint else "结果由骰子决定"
            choices.append(choice_data)
        return choices

    def _generate_opening_choices(self) -> list[dict]:
        """Generate context-aware opening choices — delegates to _generate_context_choices."""
        return self._generate_context_choices()

    def _roll_or_sustain(self, item: dict) -> object | None:
        """Roll a random item or sustain its previous result based on duration/cooldown.

        Returns a dice result if the item is active (rolled or sustained), or None
        if the item is on cooldown.
        """
        item_id = item["id"]
        ri_state = self.current_state.setdefault("random_item_state", {})
        st = ri_state.get(item_id)
        duration = item.get("duration_turns", 0)
        cooldown = item.get("cooldown_turns", 0)

        if st:
            # Item has prior state — check remaining / cooldown
            if st.get("remaining_turns", 0) > 0:
                # Still active from previous roll — sustain without re-rolling
                st["remaining_turns"] -= 1
                # Bug#6: 直接构造 sustain 结果，不调用 roll_and_resolve
                from engine.dice import DiceResult
                result = DiceResult(
                    raw_rolls=st.get("raw_rolls", []),
                    total_raw=st["total"],
                    modifier=0,
                    total=st["total"],
                    formula=st.get("formula", ""),
                    range_label=st.get("result_label", ""),
                    range_state_changes=[],  # Don't re-apply state changes
                )
                result.random_item_id = item_id
                result._sustained = True
                return result

            if st.get("cooldown_remaining", 0) > 0:
                st["cooldown_remaining"] -= 1
                return None  # On cooldown, skip

        # Roll fresh
        result = self.dice.roll_and_resolve(
            item.get("dice", {}), item.get("ranges", [])
        )
        result.random_item_id = item_id
        result._sustained = False

        # Track state if item has duration
        if duration > 0:
            ri_state[item_id] = {
                "total": result.total,
                "result_label": result.range_label or "",
                "raw_rolls": result.raw_rolls if hasattr(result, 'raw_rolls') else [],
                "formula": result.formula if hasattr(result, 'formula') else "",
                "remaining_turns": duration - 1,  # This turn counts as first
                "cooldown_remaining": 0,
            }
        elif cooldown > 0:
            # No duration but has cooldown — enter cooldown after this roll
            ri_state[item_id] = {
                "total": result.total,
                "result_label": result.range_label or "",
                "raw_rolls": [],
                "formula": "",
                "remaining_turns": 0,
                "cooldown_remaining": cooldown,
            }

        return result


    def _enrich_choice_previews(self, choices: list[dict]) -> list[dict]:
        """Post-process choices to add preview tags for informed decision-making."""
        inventory = {it.get("item"): it for it in self.current_state.get("inventory", []) if isinstance(it, dict)}
        atmo = self.current_state.get("time_atmosphere", {})
        da = self.current_state.get("difficulty_awareness", {})

        for c in choices:
            text = c.get("text", "")
            previews = []

            # Time cost from time_hint
            th = c.get("time_hint", "")
            if th:
                hrs = 0
                hm = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?', th)
                if hm:
                    hrs = int(hm.group(1) or 0) + int(hm.group(2) or 0) / 60
                    if hrs >= 1:
                        previews.append(f"\U0001F552 约{int(hrs)}小时")
                    elif int(hm.group(2) or 0) > 0:
                        previews.append(f"\U0001F552 约{hm.group(2)}分钟")

            # Item usage detection
            for item_name, item_data in inventory.items():
                if item_name in text and item_data.get("use_effect"):
                    eff = item_data["use_effect"]
                    for sc in eff.get("state_changes", []):
                        target = sc.get("target", "").split(".")[-1]
                        change = sc.get("value", sc.get("change", 0))
                        sign = "+" if change > 0 else ""
                        previews.append(f"\U0001F48A {target}{sign}{change}")
                    if eff.get("consumable", True):
                        previews.append(f"\U0001F4E6 消耗{item_name}")
                    break

            # Travel detection
            travel_kws = ("前往", "去", "移动", "出发", "赶往", "返回")
            if any(kw in text for kw in travel_kws):
                previews.append("\U0001F6B6 移动")

            # Difficulty hint
            momentum = da.get("player_momentum", "balanced")
            risk = c.get("risk", "")
            if risk == "risky" and momentum == "struggling":
                previews.append("⚠ 当前状态不佳")

            # Time atmosphere hint
            period = atmo.get("period_label", "")
            if period and any(kw in text for kw in ("潜行", "偷", "暗中")):
                light = atmo.get("light_level", "bright")
                if light == "dark":
                    previews.append("\U0001F319 夜间加成")
                elif light == "bright":
                    previews.append("☀ 白天不利")

            if previews:
                c["previews"] = previews
        return choices

    def _generate_context_choices(self, state: dict | None = None) -> list[dict]:
        """Generate context-aware default choices based on current game state."""
        state = state if state is not None else self.current_state
        choices = []
        player = state.get("player", {})
        player_loc = player.get("location", "")

        # Choice 1: use the player's visible location, not pending event outcomes
        loc_name = self.prompt_builder._resolve_location_name(player_loc) if player_loc else "周围"
        choices.append({"id": "c1", "text": f"探索{loc_name}"})

        # Choice 2: talk to present NPC(s) — show specific names when <=2
        present_npcs = []
        for npc_id in self._compute_present_npcs(state)[0]:
            npc_state = state.get("npcs", {}).get(npc_id, {})
            if not isinstance(npc_state, dict):
                npc_state = {}
            npc_def = self._npc_by_id.get(npc_id, {})
            if npc_state.get("known", npc_def.get("known", True)) is False:
                continue
            present_npcs.append((npc_id, npc_state.get("name") or npc_def.get("name", npc_id)))
        if len(present_npcs) == 1:
            choices.append({"id": "c2", "text": f"与{present_npcs[0][1]}交谈"})
        elif len(present_npcs) == 2:
            choices.append({"id": "c2", "text": f"与{present_npcs[0][1]}交谈"})
            choices.append({"id": "c2b", "text": f"与{present_npcs[1][1]}交谈"})
        elif len(present_npcs) > 2:
            names = "、".join(n[1] for n in present_npcs[:3])
            choices.append({"id": "c2", "text": f"与附近的人交谈（{names}）"})
        else:
            choices.append({"id": "c2", "text": "寻找附近的人交谈"})

        # Choice 3: based on weather/time/inventory/milestones
        inventory = state.get("inventory", [])
        weather = state.get("current_weather", "")
        milestones = state.get("active_milestones", [])
        if weather and any(w in weather for w in ("暴雨", "大雪", "极端")):
            choices.append({"id": "c3", "text": "寻找避难处"})
        elif milestones:
            ms_desc = milestones[0] if isinstance(milestones[0], str) else milestones[0].get("name", "")
            if ms_desc:
                choices.append({"id": "c3", "text": f"思考如何推进「{ms_desc[:10]}」"})
            else:
                choices.append({"id": "c3", "text": "整理思绪，规划下一步"})
        elif inventory:
            choices.append({"id": "c3", "text": "查看背包物品"})
        else:
            choices.append({"id": "c3", "text": "环顾四周，观察情况"})

        # Choice 4: difficulty-adaptive suggestion
        da = state.get("difficulty_awareness", {})
        momentum = da.get("player_momentum", "balanced")
        pacing = state.get("pacing_state", {})
        if pacing.get("consecutive_low", 0) >= 4 and pacing.get("tension", 50) <= 25:
            choices.append({"id": "c4", "text": "调查周围是否有异常动静"})
        elif momentum == "struggling":
            choices.append({"id": "c4", "text": "寻找休息或恢复的机会", "risk": "safe"})
        elif momentum == "dominating":
            active_quests = state.get("story_tree_state", {}).get("active", [])
            if active_quests:
                choices.append({"id": "c4", "text": "主动推进当前目标"})
            else:
                choices.append({"id": "c4", "text": "寻找更大的挑战"})

        return choices

    async def _ai_check_same_location(self, loc_a: str, loc_b: str) -> bool:
        """Use AI to check if two location descriptions refer to the same or nearby place."""
        if not self.ai_provider:
            return False
        cache_key = (loc_a, loc_b) if loc_a <= loc_b else (loc_b, loc_a)
        if not hasattr(self, '_location_match_cache'):
            self._location_match_cache = {}
        if cache_key in self._location_match_cache:
            return self._location_match_cache[cache_key]
        # Resolve IDs to names for better AI understanding
        name_a = self._resolve_location_id(loc_a) or loc_a
        name_b = self._resolve_location_id(loc_b) or loc_b
        try:
            response = await self.ai_provider.generate(
                [{"role": "user", "content":
                    f"在这个游戏世界中，以下两个位置是否指同一个地方或足够近可以直接对话？\n"
                    f"位置A: {name_a}\n位置B: {name_b}\n"
                    f"仅回答'是'或'否'，不需要解释。"}],
                system="你是一个简洁的位置判断助手。"
            )
            result = "是" in response.strip()[:5]
            self._location_match_cache[cache_key] = result
            return result
        except Exception:
            return False

    def _locations_match(self, loc_a: str, loc_b: str) -> bool:
        """Check if two locations refer to the same place.

        Tries exact match first, then resolves location IDs to names for comparison.
        """
        if not loc_a or not loc_b:
            return False
        # Exact match (case-insensitive)
        if loc_a.strip().lower() == loc_b.strip().lower():
            return True
        # Resolve IDs to names and compare
        name_a = self._resolve_location_id(loc_a)
        name_b = self._resolve_location_id(loc_b)
        if name_a and name_b and name_a.lower() == name_b.lower():
            return True
        # Also check if one is an ID and the other is its name
        if name_a and name_a.lower() == loc_b.strip().lower():
            return True
        if name_b and name_b.lower() == loc_a.strip().lower():
            return True
        # 层级位置匹配：如果一方是另一方的父/子位置，也算在场
        if self._is_child_location(loc_a, loc_b) or self._is_child_location(loc_b, loc_a):
            return True
        return False

    def _is_child_location(self, parent_id: str, child_id: str) -> bool:
        """Check if child_id is a sub-location of parent_id (one level)."""
        child_def = self._location_by_id.get(child_id)
        if child_def:
            p = child_def.get("parent") or child_def.get("parent_location") or ""
            if p and p.strip().lower() == parent_id.strip().lower():
                return True
        parent_def = self._location_by_id.get(parent_id)
        if parent_def:
            contains = parent_def.get("contains") or parent_def.get("sub_locations") or []
            if child_id in contains:
                return True
        return False

    def _resolve_location_id(self, location: str) -> str:
        """Resolve a location ID to its display name, or return empty if not found."""
        loc = self._location_by_id.get(location)
        return loc.get("name", location) if loc else ""

    def _get_npc_location(self, npc_id: str, state: dict | None = None) -> str:
        """Get NPC's current location: state.current_location > schedule > default_location."""
        _st = state if state is not None else self.current_state
        if npc_id not in self._initial_npc_ids and npc_id not in _st.get("npcs", {}):
            return ""
        # Highest priority: AI-updated current_location (from offscreen updates / new NPCs)
        npc_state = _st.get("npcs", {}).get(npc_id, {})
        if isinstance(npc_state, dict) and npc_state.get("alive") is False:
            return ""
        if (isinstance(npc_state, dict) and npc_state.get("current_location")
                and npc_state["current_location"] != npc_state.get("_derived_location")):
            return npc_state["current_location"]
        game_time = _st.get("game_time", "")
        # Try schedule
        info = self.prompt_builder.get_npc_current_info(npc_id, game_time, state=_st)
        if info and info.get("location"):
            return info["location"]
        # Fall back to default_location in state
        if isinstance(npc_state, dict) and npc_state.get("default_location"):
            return npc_state["default_location"]
        # Fall back to script definition (P1: 使用字典查找)
        npc_def = self._npc_by_id.get(npc_id)
        if npc_def:
            return npc_def.get("default_location", npc_def.get("initial_location", ""))
        return ""

    def _ensure_rooms_initialized(self, state: dict | None = None):
        """刷新日程推断的房间，保留显式指定的房间。"""
        state = state if state is not None else self.current_state
        game_time = state.get("game_time", "")
        npcs_state = state.get("npcs", {})
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            ns = npcs_state.get(npc_id)
            if not isinstance(ns, dict):
                continue
            derived_room = ns.get("_derived_room")
            if ns.get("current_room") and ns["current_room"] != derived_room:
                ns.pop("_derived_room", None)
                continue
            info = self.prompt_builder.get_npc_current_info(npc_id, game_time, state=state)
            room = ""
            if info and info.get("location") == self._get_npc_location(npc_id, state) and info.get("activity"):
                room = self._extract_room_from_activity(info["activity"])
            if room:
                ns["current_room"] = room
                ns["_derived_room"] = room
            elif derived_room:
                ns.pop("current_room", None)
                ns.pop("_derived_room", None)
        # 玩家房间
        player = state.get("player", {})
        if not player.get("current_room"):
            loc = player.get("location", "")
            loc_def = self._location_by_id.get(loc, {})
            default_room = loc_def.get("default_room", "")
            if default_room:
                player["current_room"] = default_room

    @staticmethod
    def _extract_room_from_activity(activity: str) -> str:
        """从 activity 描述中提取房间/位置关键词。"""
        m = re.search(
            r'在([^，。、,\s]{0,10}?(?:办公室|卧室|走廊|会议室|值班室|食堂|茶水间|资料室|门口|大厅|庭院|官邸|营房|书房|寝室|餐厅|接待室|警卫室|指挥所|车库))',
            activity
        )
        return m.group(1) if m else ""


    # ================================================
    #  NEW GAMEPLAY SYSTEMS
    # ================================================


    def _get_location_travel_time(self, location_id: str) -> str | None:
        """Get travel time for moving to a location."""
        loc = self._location_by_id.get(location_id)
        return loc.get("travel_time") if loc else None

    def _configured_travel_minutes(self, origin: str | None, target: str,
                                   map_destination: str | None = None,
                                   state: dict | None = None) -> int:
        route = (self._visible_location_route(origin, target, state=state)
                 if target == map_destination else None)
        return sum(self._parse_duration_minutes(self._get_location_travel_time(stop)) or 0
                   for stop in (route[1:] if route else [target]))

    def _travel_minutes_for_updates(self, ctx: dict, location_change: str | None,
                                    pending_changes: dict | None) -> tuple[int, str | None]:
        origin = ctx.get("_travel_origin", (self.current_state.get("player") or {}).get("location"))
        updates = [*ctx.get("_accepted_state_updates", [])]
        if pending_changes:
            updates.append(pending_changes)
        elif location_change:
            updates.append({"location_change": location_change})
        total = 0
        destination = None
        for update in updates:
            target = update.get("location_change")
            if not target or target == origin:
                continue
            total += self._configured_travel_minutes(origin, target, ctx.get("_map_travel_destination"))
            origin = destination = target
        return total, destination

    def _get_choice_time_hint(self, player_action: dict) -> int | None:
        """Extract time_hint from the selected choice and convert to minutes."""
        choice_id = player_action.get("choice_id", "")
        if not choice_id:
            return None
        active_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not active_node:
            return None
        for c in active_node.get("choices_presented", []):
            if c.get("id") == choice_id and c.get("time_hint"):
                return self._parse_duration_minutes(c["time_hint"])
        return None

    @staticmethod
    def _parse_duration_minutes(duration: str) -> int | None:
        """Parse ISO 8601 duration (PT30M, PT1H, P1D) to minutes."""
        if not isinstance(duration, str):
            return None
        dur = duration.strip()
        m = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$', dur)
        if m and (m.group(1) or m.group(2) or m.group(3)):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            minutes = int(m.group(3) or 0)
            total = days * 1440 + hours * 60 + minutes
            return total if total > 0 else None
        return None

    def _is_duplicate_location(self, state: dict, new_loc_id: str) -> bool:
        """Check if a new location is semantically duplicate with existing ones."""
        visible = state.get("visible_locations", [])
        if new_loc_id in visible:
            return True
        dn = state.get("display_names", {})
        existing_names = [dn.get(vid) or self._location_by_id.get(vid, {}).get("name") or vid for vid in visible]
        new_name = dn.get(new_loc_id) or self._location_by_id.get(new_loc_id, {}).get("name") or new_loc_id
        for ename in existing_names:
            if self._location_names_similar(new_name, ename):
                return True
        return False

    def _can_use_location_id(self, state: dict, location_id: str, revealed=()) -> bool:
        visible = state.get("visible_locations")
        known = self._location_by_id if visible is None else visible
        return (location_id == (state.get("player") or {}).get("location")
                or location_id in known or location_id in revealed)

    @staticmethod
    def _location_names_similar(a: str, b: str) -> bool:
        """Check if two location names are similar enough to be duplicates."""
        if a == b:
            return True
        if not a or not b:
            return False
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if shorter in longer:
            return True
        # Compare common prefix ratio
        common = 0
        for ca, cb in zip(a, b):
            if ca == cb:
                common += 1
            else:
                break
        min_len = min(len(a), len(b))
        if min_len >= 4 and common >= min_len * 0.7:
            return True
        return False


    @staticmethod
    def _merge_legacy_events_into_tree(tree_def: dict, script: dict):
        """Convert script triggers and dynamic_events into story tree nodes."""
        triggers = script.get("triggers", [])
        dynamic_events = script.get("dynamic_events", [])
        if not triggers and not dynamic_events:
            return

        tree_def.setdefault("trees", [])

        if triggers:
            sys_tree = {"id": "__system_triggers", "name": "系统触发器", "icon": "scroll", "nodes": []}
            for i, t in enumerate(triggers):
                if not t.get("enabled", True):
                    continue
                effects = {}
                action = t.get("action", "")
                params = t.get("params", {})
                if action == "set_var":
                    effects["set_var"] = [params]
                elif action == "inject_prompt":
                    effects["inject_prompt"] = params.get("text", "")
                elif action in ("activate_lore", "reveal_lore"):
                    effects["activate_lore"] = [params.get("entry_id", "")]
                elif action == "deactivate_lore":
                    effects["deactivate_lore"] = [params.get("entry_id", "")]
                elif action == "schedule_override":
                    effects["schedule_override"] = [{"action": action, "params": params}]
                elif action == "activate_persistent_state" and params.get("target"):
                    effects["activate_state"] = [params["target"]]
                elif action == "deactivate_persistent_state" and params.get("target"):
                    effects["deactivate_state"] = [params["target"]]
                elif action == "notify":
                    effects["notify"] = params.get("message", "")
                sys_tree["nodes"].append({
                    "id": t.get("id", f"__trigger_{i}"),
                    "name": t.get("name", f"trigger_{i}"),
                    "type": "trigger",
                    "event": t.get("event", ""),
                    "condition": t.get("condition", ""),
                    "effects": effects,
                })
            if sys_tree["nodes"]:
                tree_def["trees"].append(sys_tree)

        if dynamic_events:
            de_tree = {"id": "__dynamic_events", "name": "动态事件", "icon": "fire", "nodes": []}
            for de in dynamic_events:
                conditions = de.get("conditions", [])
                cond_parts = []
                for c in conditions:
                    path = c.get("path", "")
                    op = c.get("op", "==")
                    val = c.get("value", "")
                    cond_parts.append(f"{path} {op} {val}")
                condition_str = " AND ".join(cond_parts) if cond_parts else ""

                effects = {}
                for eff in de.get("effects", []):
                    etype = eff.get("type", "")
                    if etype == "state_change":
                        effects.setdefault("set_var", []).append({
                            "var_id": eff.get("target", ""),
                            "op": eff.get("op", "set"),
                            "value": eff.get("value"),
                        })
                    elif etype == "activate_state":
                        effects.setdefault("activate_state", []).append(eff.get("id", ""))
                    elif etype == "narrative_callback":
                        effects["narrative_callback"] = {
                            "text": eff.get("text", ""),
                            "priority": eff.get("priority", "medium"),
                        }

                fire_evts = de.get("fire_events", [])
                if fire_evts:
                    effects["fire_events"] = fire_evts

                chain_unlock = []
                for chain in de.get("chain_events", []):
                    chain_unlock.append(chain.get("event_id", ""))

                de_node = {
                    "id": de.get("id", ""),
                    "name": de.get("name", de.get("id", "")),
                    "description": de.get("description", ""),
                    "type": "periodic",
                    "condition": condition_str,
                    "cooldown": de.get("cooldown", 5),
                    "weight": de.get("weight", 10),
                    "repeatable": True,
                    "effects": effects,
                    "on_complete_unlock": chain_unlock if chain_unlock else [],
                }
                if de.get("related_npcs"):
                    de_node["related_npcs"] = de["related_npcs"]
                if de.get("related_orgs"):
                    de_node["related_orgs"] = de["related_orgs"]
                de_tree["nodes"].append(de_node)
            if de_tree["nodes"]:
                tree_def["trees"].append(de_tree)

    def _settle_story_choice(self, node_id: str, choice_id: str):
        story_node = self.story_tree_engine._nodes.get(node_id) if self.story_tree_engine else None
        if story_node is not None:
            if story_node.get("type") != "choice":
                return None
            engine = self.story_tree_engine
        else:
            event = self.event_engine.events.get(node_id) if self.event_engine else None
            if event is None or event.category != "choice":
                return None
            engine = self.event_engine
        result = engine.make_choice(
            self.current_state, node_id, choice_id, condition_eval=self._evaluate_condition,
        )
        if not result.newly_completed:
            return result
        if story_node is not None:
            if self.event_engine:
                self.event_engine.sync_story_tree_state(self.current_state)
            self._apply_story_tree_result(result)
            if self.event_engine:
                self.event_engine.sync_story_tree_state(self.current_state)
        else:
            self._apply_event_result(result)
        return result

    def _apply_story_tree_result(self, result):
        """Apply story tree effects to game state."""
        from engine.story_tree import StoryTreeResult
        if not isinstance(result, StoryTreeResult):
            return
        target = self.current_state
        self._apply_effects_and_lore(
            effects=result.effects,
            lore_activations=result.lore_activations,
            lore_deactivations=result.lore_deactivations,
            lore_additions=result.lore_additions,
            lore_updates=result.lore_updates,
            lore_removals=result.lore_removals,
            game_events=result.game_events,
            narrative_callbacks=result.narrative_callbacks,
            state_activations=result.state_activations,
            target=target,
            entry_type="story_tree",
        )

        for effect in result.effects:
            if effect.get("action") == "unlock_node":
                nid = effect["params"]["node_id"]
                sts = target.setdefault("story_tree_state", {})
                ul = set(sts.get("unlocked", []))
                ul.add(nid)
                sts["unlocked"] = list(ul)

        self._sync_node_lorebook(result.newly_completed, result.newly_active)

    def _fire_event_dual(self, event_name: str, state: dict | None = None):
        """Fire an event through both event_engine and story_tree_engine."""
        st = state or self.current_state
        if self.event_engine:
            ee_r = self.event_engine.fire_event(event_name, st, condition_eval=self._evaluate_condition)
            self._apply_event_result(ee_r)
        if self.story_tree_engine:
            self.story_tree_engine.fire_event(event_name, st, condition_eval=self._evaluate_condition)
        if self.trigger_engine and self.trigger_engine.triggers:
            actions = self.trigger_engine.fire(
                event_name, st, include_event_triggers=self.event_engine is None,
                condition_eval=self._evaluate_condition,
            )
            if actions:
                effects = self.trigger_engine.execute_actions(actions, st)
                self._apply_trigger_effects(effects, state=st)

    def _apply_lorebook_changes(self, *, additions=(), updates=(), removals=(),
                               state=None, entry_type=None):
        target = self.current_state if state is None else state
        lorebook = self.prompt_builder.lorebook
        existing_ids = {entry.id for entry in lorebook.entries}
        changed_ids = set()
        for original in additions:
            if not isinstance(original, dict) or not original.get("id"):
                continue
            entry_id = original["id"]
            if entry_id in existing_ids:
                continue
            entry_data = copy.deepcopy(original)
            if entry_type is not None:
                entry_data.setdefault("enabled", True)
                entry_data.setdefault("position", "after_world")
                entry_data.setdefault("priority", 80)
                entry_data.setdefault("scan_depth", 3)
                entry_data.setdefault("entry_type", entry_type)
            lorebook.add_entries([entry_data])
            existing_ids.add(entry_id)
            changed_ids.add(entry_id)
        for update in updates:
            entry_id = update.get("id", "")
            if entry_id not in existing_ids:
                continue
            lorebook.update_entry(entry_id, content=update.get("content", ""),
                                  keys=copy.deepcopy(update.get("keys")))
            changed_ids.add(entry_id)
        for entry_id in removals:
            if entry_id:
                lorebook.remove_entry(entry_id)
                changed_ids.add(entry_id)
        if not changed_ids:
            return
        entries = {entry.id: entry for entry in lorebook.entries}
        overrides = target.setdefault("lorebook_entry_overrides", {})
        for entry_id in changed_ids:
            entry = entries.get(entry_id)
            overrides[entry_id] = asdict(entry) if entry is not None else None
            target.get("lorebook_enabled_overrides", {}).pop(entry_id, None)
        self.prompt_builder._lorebook_override_ids.update(changed_ids)
        self._sync_lorebook_vectors(changed_ids)

    def _sync_lorebook_vectors(self, entry_ids):
        if self._deferred_lore_vector_ids is not None:
            self._deferred_lore_vector_ids.update(entry_ids)
            return
        if not self.vector_memory or not entry_ids:
            return
        entries = {entry.id: entry for entry in self.prompt_builder.lorebook.entries}
        batch = [(entry_id, entries[entry_id].content, {
            "entry_type": entries[entry_id].entry_type, "comment": entries[entry_id].comment,
        }) for entry_id in sorted(entry_ids)
            if entry_id in entries and len(entries[entry_id].content) >= 20]
        removals = sorted(set(entry_ids) - {entry_id for entry_id, _, _ in batch})
        vector_memory = self.vector_memory

        def sync():
            for entry_id in removals:
                vector_memory.remove_lorebook(entry_id)
            if batch:
                vector_memory.add_lorebook_batch(batch)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            sync()
            return
        prior_tasks = tuple(self._index_tasks)

        async def sync_after_index():
            if prior_tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in prior_tasks),
                                     return_exceptions=True)
            await asyncio.to_thread(sync)

        self._schedule_index_task(sync_after_index())

    def _set_lore_enabled(self, entry_id: str, enabled: bool, state: dict | None = None):
        entry = next((entry for entry in self.prompt_builder.lorebook.entries
                      if entry.id == entry_id), None)
        if entry is None:
            return
        entry.enabled = enabled
        target = self.current_state if state is None else state
        target.setdefault("lorebook_enabled_overrides", {})[entry_id] = enabled

    def _apply_trigger_effects(self, effects: dict, state: dict | None = None):
        """Apply side-effects returned by TriggerEngine.execute_actions."""
        for text in effects.get("inject_prompts", []):
            self._record_narrative_callback(text, tags=["trigger"], priority="high")
        for entry_id in effects.get("lore_activations", []):
            self._set_lore_enabled(entry_id, True, state)
        for entry_id in effects.get("lore_deactivations", []):
            self._set_lore_enabled(entry_id, False, state)
        for entry_id in effects.get("lore_reveals", []):
            self._set_lore_enabled(entry_id, True, state)

    def _check_quest_keywords(self, text: str):
        """Check active quests in both engines for keyword-based completion."""
        text_lower = text.lower()
        if self.event_engine:
            es = self.current_state.get("events", {})
            for qid, qev in list(self.event_engine.events.items()):
                if qev.category != "quest":
                    continue
                if es.get(qid, {}).get("status") != "active":
                    continue
                kws = qev.completion_keywords or qev.metadata.get("completion_keywords", [])
                if kws and any(kw.lower() in text_lower for kw in kws):
                    r = self.event_engine.complete_quest(self.current_state, qid)
                    self._apply_event_result(r)
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            for nid in list(sts.get("active", [])):
                node = self.story_tree_engine._nodes.get(nid)
                if not node or node.get("type") != "quest":
                    continue
                kws = node.get("completion_keywords", [])
                if kws and any(kw.lower() in text_lower for kw in kws):
                    r = self.story_tree_engine.complete_quest(self.current_state, nid)
                    self._apply_story_tree_result(r)

    def _apply_event_result(self, result, state=None):
        """Apply unified EventResult to game state (replaces _apply_story_tree_result for EventEngine)."""
        if not isinstance(result, EventResult):
            return
        target = state if state is not None else self.current_state

        self._apply_effects_and_lore(
            effects=result.effects,
            lore_activations=result.lore_activations,
            lore_deactivations=result.lore_deactivations,
            lore_additions=result.lore_additions,
            lore_updates=result.lore_updates,
            lore_removals=result.lore_removals,
            lore_reveals=getattr(result, "lore_reveals", []),
            game_events=result.game_events,
            narrative_callbacks=result.narrative_callbacks,
            state_activations=result.state_activations,
            state_deactivations=getattr(result, "state_deactivations", []),
            target=target,
            entry_type="event_generated",
        )

        for effect in result.effects:
            if effect.get("action") == "unlock_node":
                nid = effect["params"].get("node_id", "")
                if nid:
                    es = target.setdefault("events", {})
                    es.setdefault(nid, {})["status"] = "unlocked"
                    sts = target.setdefault("story_tree_state", {})
                    ul = set(sts.get("unlocked", []))
                    ul.add(nid)
                    sts["unlocked"] = list(ul)

        for sync in result.lorebook_sync:
            action = sync.get("action", "")
            entry_id = sync.get("entry_id", "")
            if action == "remove" and entry_id:
                self._apply_lorebook_changes(removals=[entry_id], state=target)
            elif action == "add" and sync.get("entry"):
                self._apply_lorebook_changes(additions=[sync["entry"]], state=target)
            elif action == "update" and entry_id:
                self._apply_lorebook_changes(updates=[{**sync, "id": entry_id}], state=target)

        if result.imminent_warnings:
            scene = target.setdefault("scene_details", {})
            existing_tension = scene.get("pending_tension", "")
            warning_text = "；".join(result.imminent_warnings)
            if existing_tension:
                scene["pending_tension"] = f"{existing_tension}；{warning_text}"
            else:
                scene["pending_tension"] = warning_text

        if result.deadline_results:
            target.setdefault("_deadline_results", []).extend(result.deadline_results)
            for dr in result.deadline_results:
                if dr.get("outcome") != "expired":
                    continue
                dl_id = dr.get("id", "")
                ev = self.event_engine.events.get(dl_id)
                if not ev or not ev.metadata.get("is_promise"):
                    continue
                direction = ev.metadata.get("direction", "")
                npc_id = ev.metadata.get("npc_id", "")
                npc_name = ev.metadata.get("npc_name", "")
                content = ev.metadata.get("content", "")
                if direction == "player_to_npc" and npc_id:
                    witnesses = ev.metadata.get("witnesses", [])
                    known_by = list(set([npc_id] + witnesses))
                    network = target.setdefault("information_network", [])
                    network.append({
                        "id": f"broken_prm_{self.turn_number}_{npc_id}",
                        "origin_turn": self.turn_number,
                        "fact": f"玩家未兑现对{npc_name}的承诺「{content[:30]}」",
                        "known_by": known_by,
                        "spread_chance": 0.5, "distortion": 0, "max_spread": 5,
                        "tags": ["broken_promise", "social", npc_id],
                    })
                    self._record_narrative_callback(
                        f"{npc_name}想起你未兑现的承诺「{content[:20]}」",
                        ["promise", npc_id], priority="high",
                    )
                elif direction == "npc_to_player" and npc_name:
                    self._record_narrative_callback(
                        f"{npc_name}之前安排的「{content[:20]}」已到时间",
                        ["npc_appointment", npc_id], priority="high",
                    )

        if result.expired_consequences:
            target["expired_consequences"] = [
                {"description": ec.get("description", "")} for ec in result.expired_consequences
            ]

        shadow_nodes = (self.story_tree_engine._nodes
                        if self.story_tree_engine and self.event_engine.shadow_story_nodes else {})
        self._sync_node_lorebook(
            [node for node in result.newly_completed if node.get("id") not in shadow_nodes],
            [node for node in result.newly_active if node.get("id") not in shadow_nodes],
            target=target,
        )

    def _apply_effects_and_lore(
        self, *, effects, lore_activations, lore_deactivations,
        lore_additions, lore_updates, lore_removals,
        lore_reveals=(),
        game_events, narrative_callbacks, state_activations,
        state_deactivations=(), target=None, entry_type="event_generated",
    ):
        """Shared effect processing for StoryTreeResult and EventResult."""
        target = self.current_state if target is None else target
        for effect in effects:
            action = effect.get("action", "")
            params = effect.get("params", {})
            if not isinstance(params, dict):
                params = {}
            if action == "set_var":
                var_id = params.get("var_id") or params.get("target", "")
                op = params.get("op", "set")
                value = params.get("value")
                if "." in var_id:
                    self.state_manager.apply_changes(
                        target, [{"target": var_id, "op": op, "value": value}], inplace=True,
                    )
                else:
                    self.script_variables.apply_op(target, var_id, op, value)
            elif action == "unlock_location":
                loc_id = params.get("location_id", "")
                if loc_id:
                    visible = target.setdefault("visible_locations", [])
                    if loc_id not in visible:
                        visible.append(loc_id)
            elif action == "reveal_npc":
                npc_id = params.get("npc_id", "")
                if npc_id:
                    npcs = target.setdefault("npcs", {})
                    if npc_id not in npcs:
                        npcs[npc_id] = {}
                    npcs[npc_id]["known"] = True
            elif action == "set_reputation":
                fid = params.get("faction_id", "")
                if fid:
                    rep = target.setdefault("faction_reputation", {})
                    entry = rep.setdefault(fid, {"value": 50})
                    op = params.get("op", "add")
                    val = params.get("value", 0)
                    if op == "set":
                        entry["value"] = max(0, min(100, val))
                    else:
                        entry["value"] = max(0, min(100, entry.get("value", 50) + val))
                    entry["title"] = self._reputation_title(entry["value"])

            elif action == "schedule_override":
                npc_id = params.get("npc_id", "")
                if npc_id:
                    overrides = target.setdefault("npc_schedule_overrides", {})
                    npc_ov = overrides.setdefault(npc_id, [])
                    entry = {
                        "time_range": params.get("time_range", ""),
                        "location": params.get("location", ""),
                        "activity": params.get("activity", ""),
                        "priority": params.get("priority", 100),
                    }
                    if params.get("expires"):
                        entry["expires"] = params["expires"]
                    if "condition" in params:
                        entry["condition"] = params["condition"]
                    if entry["time_range"] and entry["location"]:
                        npc_ov.append(entry)

        for eid in lore_activations:
            self._set_lore_enabled(eid, True, target)
        for eid in lore_deactivations:
            self._set_lore_enabled(eid, False, target)

        self._apply_lorebook_changes(
            additions=lore_additions, updates=lore_updates, removals=lore_removals,
            state=target, entry_type=entry_type,
        )

        for rid in lore_reveals:
            discovered = target.setdefault("pc_discovered_lore", [])
            if rid not in discovered:
                discovered.append(rid)

        for ev in game_events:
            self._fire_event_dual(ev, target)

        for cb in narrative_callbacks:
            self._record_narrative_callback(
                cb.get("text", ""), tags=["event"], priority=cb.get("priority", "medium"),
            )

        for sid in state_activations:
            aps = target.setdefault("active_persistent_states", [])
            if sid not in aps:
                aps.append(sid)

        for sid in state_deactivations:
            aps = target.get("active_persistent_states", [])
            if sid in aps:
                aps.remove(sid)

    def _sync_node_lorebook(self, newly_completed, newly_active, target=None):
        """Sync completed/active nodes to lorebook and defer vector indexing until commit."""
        target = target or self.current_state
        for node in newly_completed:
            ntype = node.get("type", "auto")
            name = node.get("name", node.get("id", ""))
            nid = node.get("id", "")
            is_internal = nid.startswith("_bp_") or nid.startswith("_dyn_")
            if ntype == "choice":
                chosen = ""
                for c in (node.get("choices") or []):
                    if c.get("chosen"):
                        chosen = c.get("label", "")
                        break
                text = f"剧情树选择「{name}」" + (f"→ {chosen}" if chosen else "")
                self._record_narrative_callback(text, ["story_tree", "choice"], priority="high")
            elif ntype == "quest":
                self._record_narrative_callback(
                    f"完成任务「{name}」", ["story_tree", "quest"], priority="high",
                )
            elif ntype in ("story", "auto", "timed") and not is_internal:
                self._record_narrative_callback(
                    f"剧情节点「{name}」完成", ["story_tree"], priority="low",
                )

            desc = node.get("description", name)
            if nid.startswith("__trigger_") or not desc:
                continue
            self.prompt_builder.lorebook.remove_entry(f"_st_active_{nid}")
            if is_internal:
                continue
            keywords = [name]
            for npc_id in node.get("related_npcs", []):
                keywords.append(self._get_npc_or_org_name(npc_id))
            for org_id in node.get("related_orgs", []):
                keywords.append(self._get_npc_or_org_name(org_id))
            driver = node.get("_bp_driver", "")
            if driver:
                keywords.append(self._get_npc_or_org_name(driver))
            keywords.extend(self._extract_name_keywords(desc))
            keywords = [k for k in keywords if k]
            entry = {
                "id": f"_st_{nid}",
                "keys": keywords,
                "content": f"[已发生·第{self.turn_number}回合] {desc}",
                "entry_type": "story_event",
                "priority": 85,
                "position": "after_world",
                "enabled": True,
                "scan_depth": 3,
            }
            self.prompt_builder.lorebook.add_entries([entry])
            dynamic = target.setdefault("dynamic_lorebook", [])
            dynamic.append(entry)
            _related_ids = set(node.get("related_npcs", []) + node.get("related_orgs", []))
            if _related_ids:
                for _le in self.prompt_builder.lorebook.entries:
                    if _le.entry_type in ("npc_profile", "event_context", "npc_relationship"):
                        if any(k in _related_ids for k in _le.keys):
                            _le.priority = max(_le.priority - 15, 10)

        for node in newly_active:
            nid = node.get("id", "")
            name = node.get("name", nid)
            desc = node.get("description", name)
            if not desc:
                continue
            if nid.startswith("_bp_") or nid.startswith("_dyn_"):
                continue
            keywords = [name]
            for npc_id in node.get("related_npcs", []):
                keywords.append(self._get_npc_or_org_name(npc_id))
            for org_id in node.get("related_orgs", []):
                keywords.append(self._get_npc_or_org_name(org_id))
            keywords.extend(self._extract_name_keywords(desc))
            keywords = [k for k in keywords if k]
            entry = {
                "id": f"_st_active_{nid}",
                "keys": keywords,
                "content": f"[进行中] {desc}",
                "entry_type": "story_active",
                "priority": 80,
                "position": "after_world",
                "enabled": True,
                "scan_depth": 3,
            }
            self.prompt_builder.lorebook.add_entries([entry])

        if self.vector_memory and newly_completed:
            for node in newly_completed:
                nid = node.get("id", "")
                if nid.startswith("__trigger_"):
                    continue
                name = node.get("name", nid)
                desc = node.get("description", "")
                if not desc:
                    continue
                if hasattr(self, "_pending_vector_milestones"):
                    self._pending_vector_milestones.append(node)

    def _check_milestones(self) -> tuple[list[dict], list[dict]]:
        """Check if any milestones have been achieved. Returns (newly_achieved, reward_changes)."""
        milestones = self.script.get("milestones", [])
        if not milestones:
            return [], []

        achieved = self.current_state.get("achieved_milestones", [])
        newly_achieved = []
        reward_changes = []

        for ms in milestones:
            ms_id = ms.get("id", "")
            if ms_id in achieved:
                continue

            condition = ms.get("condition", "")
            if not condition:
                continue

            if self._evaluate_condition(condition):
                newly_achieved.append(ms)
                achieved.append(ms_id)

                # Apply milestone rewards
                rewards = ms.get("rewards", [])
                if rewards:
                    self.current_state, log = self.state_manager.apply_changes(
                        self.current_state, rewards, inplace=True
                    )
                    reward_changes.extend(log)

        self.current_state["achieved_milestones"] = achieved
        return newly_achieved, reward_changes

    def _check_milestone_progress(self) -> list[dict]:
        """Check milestones that are close to being achieved (>=60% of threshold)."""
        milestones = self.script.get("milestones", [])
        if not milestones:
            return []

        achieved = self.current_state.get("achieved_milestones", [])
        progress_hints = []

        for ms in milestones:
            ms_id = ms.get("id", "")
            if ms_id in achieved:
                continue
            condition = ms.get("condition", "")
            if not condition:
                continue
            # Only parse simple numeric conditions (path op value)
            m = re.match(r'([\w.]+)\s*(>=|>|<=|<)\s*(\d+)', condition.strip())
            if not m:
                continue
            path, op, threshold_str = m.group(1), m.group(2), m.group(3)
            threshold = int(threshold_str)
            current = self.state_manager._get_value(self.current_state, path)
            if not isinstance(current, (int, float)):
                continue
            # Calculate progress towards threshold
            if op in (">=", ">"):
                if threshold == 0:
                    continue
                progress = current / threshold
            elif op in ("<=", "<"):
                if current <= threshold:
                    continue  # already met
                # current > threshold: progress = how close to dropping below threshold
                # e.g. "stress <= 50", current=60 → need to drop 10 → progress = threshold/current
                if current == 0:
                    continue
                progress = threshold / current
            else:
                continue
            if 0.6 <= progress < 1.0:
                progress_hints.append({
                    "milestone_id": ms_id,
                    "name": ms.get("name", ms_id),
                    "progress": round(progress * 100),
                    "current": current,
                    "target": threshold,
                })

        return progress_hints

    def _update_adventure_log(
        self,
        player_action: dict,
        parsed: dict,
        triggered_events: list[dict],
        triggered_consequences: list[dict] | None,
        achieved_milestones: list[dict] | None,
        threshold_events: list[dict] | None,
        all_state_changes: list[dict] | None = None,
    ):
        """Update the adventure log with key events from this turn."""
        log = self.current_state.setdefault("adventure_log", [])

        entry = {
            "turn": self.turn_number,
            "time": self.current_state.get("game_time", ""),
            "location": self.current_state.get("player", {}).get("location", ""),
            "events": [],
        }

        # Log player decision
        action_text = player_action.get("text", "")
        if action_text:
            entry["events"].append({
                "type": "decision",
                "text": f"你{action_text}" if player_action.get("type") == "freeform" else f"你选择了: {action_text}",
            })

        # Log triggered events
        for e in triggered_events:
            if not is_public_event(self.script, e.get("event_id", "")):
                continue
            entry["events"].append({
                "type": "event",
                "event_id": e.get("event_id", ""),
                "text": e.get("description", e.get("event_id", "")),
            })

        # Log consequences
        for c in (triggered_consequences or []):
            entry["events"].append({
                "type": "consequence",
                "text": c.get("description", ""),
            })

        # Log milestones
        for m in (achieved_milestones or []):
            entry["events"].append({
                "type": "milestone",
                "text": f"达成: {m.get('name', m.get('id', ''))}",
            })

        # Log threshold events
        for t in (threshold_events or []):
            entry["events"].append({
                "type": "threshold",
                "text": t.get("description", ""),
            })

        # Log location changes
        if parsed.get("location_change"):
            entry["events"].append({
                "type": "location",
                "text": f"移动至: {parsed['location_change']}",
            })

        # Log new location reveals
        for loc_entry in parsed.get("reveal_locations", []):
            if isinstance(loc_entry, dict):
                loc_display = loc_entry.get("name", loc_entry.get("id", ""))
            else:
                loc_display = self.current_state.get("display_names", {}).get(loc_entry, loc_entry)
            entry["events"].append({
                "type": "discovery",
                "text": f"发现新地点: {loc_display}",
            })

        # Log relationship changes
        for sc in (all_state_changes or []):
            if sc.get("_source_event_id") and not is_public_event(
                self.script, sc["_source_event_id"]
            ):
                continue
            target = sc.get("target", "")
            if "relationships" in target:
                parts = target.split(".")
                # Handle both "player.relationships.guard" and "player.relationships.guard.trust"
                npc_id = parts[2] if len(parts) > 2 else ""
                dimension = parts[3] if len(parts) > 3 else ""
                npc_name = self._get_npc_display_name(npc_id)
                old_val = sc.get("old", 0)
                new_val = sc.get("new", 0)
                if old_val is not None and new_val is not None:
                    diff = (new_val if isinstance(new_val, (int, float)) else 0) - (old_val if isinstance(old_val, (int, float)) else 0)
                    if diff != 0:
                        sign = "+" if diff > 0 else ""
                        dim_label = {"trust": "信任", "affection": "好感", "fear": "畏惧"}.get(dimension, "")
                        reason = f" ({sc.get('reason', '')})" if sc.get("reason") else ""
                        dim_str = f"({dim_label})" if dim_label else ""
                        entry["events"].append({
                            "type": "relationship",
                            "text": f"{npc_name} {dim_str}关系{sign}{diff}{reason}",
                        })

        event_text = ""
        if entry["events"]:
            log.append(entry)
            important = [ev for ev in entry["events"] if ev.get("type") in
                         ("milestone", "consequence", "relationship", "event")]
            event_text = " | ".join(ev.get("text", "") for ev in important)

            self._record_location_memory(entry)

        # Chapter condensation: every 25 entries, compress older entries into a summary chapter
        last_condensed = self.current_state.get("_log_last_condensed_turn", 0)
        non_chapter_entries = [e for e in log if e.get("type") != "chapter"]
        if len(non_chapter_entries) >= 25 and (self.turn_number - last_condensed) >= 20:
            # Take the oldest 15 non-chapter entries and condense them
            to_condense = non_chapter_entries[:15]
            first_turn = to_condense[0].get("turn", "?")
            last_turn = to_condense[-1].get("turn", "?")
            first_time = to_condense[0].get("time", "")
            last_time = to_condense[-1].get("time", "")
            # 按事件类型分类提取摘要素材
            decisions = []
            milestones_found = []
            consequences_found = []
            discoveries = []
            relationships = []
            for e in to_condense:
                for ev in e.get("events", []):
                    etype = ev.get("type", "")
                    text = ev.get("text", "")
                    if not text:
                        continue
                    if etype == "milestone":
                        milestones_found.append(text)
                    elif etype == "consequence":
                        consequences_found.append(text)
                    elif etype == "discovery":
                        discoveries.append(text)
                    elif etype == "relationship":
                        relationships.append(text)
                    elif etype == "decision" and len(decisions) < 5:
                        decisions.append(text)
            # 结构化摘要：优先显示里程碑和后果，再补充决策
            summary_parts = []
            if milestones_found:
                summary_parts.append(f"[成就] {'; '.join(milestones_found[:3])}")
            if consequences_found:
                summary_parts.append(f"[后果] {'; '.join(consequences_found[:3])}")
            if discoveries:
                summary_parts.append(f"[发现] {'; '.join(discoveries[:3])}")
            if relationships:
                summary_parts.append(f"[关系] {'; '.join(relationships[:4])}")
            if decisions and len(summary_parts) < 3:
                summary_parts.append(f"[决策] {'; '.join(decisions[:3])}")
            summary_text = " | ".join(summary_parts) if summary_parts else f"回合{first_turn}-{last_turn}的冒险"
            chapter = {
                "type": "chapter",
                "turn_range": f"{first_turn}-{last_turn}",
                "time_range": f"{first_time} ~ {last_time}",
                "summary": summary_text,
                "events": [{"type": "chapter_summary", "text": summary_text}],
                "turn": first_turn,
                "time": first_time,
                "needs_ai_summary": True,
            }
            # Remove condensed entries and prepend chapter
            condensed_ids = set(id(e) for e in to_condense)
            remaining_log = [e for e in log if id(e) not in condensed_ids]
            self.current_state["adventure_log"] = [chapter] + remaining_log
            self.current_state["_log_last_condensed_turn"] = self.turn_number
            log = self.current_state["adventure_log"]

        # Keep log reasonable size, but always preserve milestone entries
        if len(log) > 100:
            milestone_entries = [e for e in log if any(
                ev.get("type") == "milestone" for ev in e.get("events", [])
            )]
            recent_entries = log[-80:]
            # Merge: milestones that were trimmed + recent entries (deduplicate)
            recent_set = set(id(e) for e in recent_entries)
            preserved = [e for e in milestone_entries if id(e) not in recent_set]
            self.current_state["adventure_log"] = preserved + recent_entries

        # --- Narrative callbacks: auto-record key moments for future AI references ---
        player_choice = parsed.get("player_choice") or player_action.get("choice_text", "")
        if player_choice:
            self._record_narrative_callback(
                f"玩家选择了: {player_choice}", ["choice"], priority="medium",
            )
        for m in (achieved_milestones or []):
            self._record_narrative_callback(
                f"达成里程碑「{m.get('name', m.get('id', ''))}」", ["milestone"], priority="high",
            )
        for c in (triggered_consequences or []):
            if c.get("importance", 0.3) >= 0.6:
                self._record_narrative_callback(
                    f"后果触发: {c.get('description', '')}", ["consequence"], priority="high",
                )
        for t in (threshold_events or []):
            if "relationship" in t.get("type", "") or "attitude" in t.get("type", ""):
                self._record_narrative_callback(
                    t.get("description", ""), ["relationship"], priority="medium",
                )

        callbacks = self.current_state.get("narrative_callbacks", [])
        if len(callbacks) > 30:
            self.current_state["narrative_callbacks"] = [
                cb for cb in callbacks if not cb.get("used")
            ][-20:]
        return event_text

    def _record_location_memory(self, log_entry: dict):
        """记录本回合在当前地点发生的重大事件。"""
        loc_id = self.current_state.get("player", {}).get("location", "")
        if not loc_id:
            return
        events = log_entry.get("events", [])
        significant = [e for e in events if e.get("type") in
                       ("consequence", "milestone", "relationship", "event", "combat")]
        if not significant:
            return
        loc_mem = self.current_state.setdefault("location_memory", {})
        loc_entries = loc_mem.setdefault(loc_id, [])
        for ev in significant[:2]:
            loc_entries.append({
                "turn": self.turn_number,
                "text": ev.get("text", "")[:60],
                "type": ev.get("type", ""),
            })
        if len(loc_entries) > 10:
            loc_mem[loc_id] = loc_entries[-8:]

    def _record_narrative_callback(self, text: str, tags: list, priority: str = "medium"):
        if not text:
            return
        callbacks = self.current_state.setdefault("narrative_callbacks", [])
        cb_id = f"cb_{self.turn_number}_{len(callbacks)}"
        callbacks.append({
            "id": cb_id,
            "turn": self.turn_number,
            "text": text[:200],
            "tags": tags,
            "callback_after": 3 if priority == "high" else 5,
            "callback_before": 15 if priority == "high" else 25,
            "used": False,
            "priority": priority,
        })

    def _mark_used_callbacks(self, narrative_text: str):
        callbacks = self.current_state.get("narrative_callbacks", [])
        if not callbacks or not narrative_text:
            return
        text_lower = narrative_text.lower()
        for cb in callbacks:
            if cb.get("used"):
                continue
            turn_diff = self.turn_number - cb.get("turn", 0)
            if turn_diff < cb.get("callback_after", 3):
                continue
            keywords = [w for w in cb.get("text", "").replace(":", " ").split() if len(w) >= 2][:5]
            if any(kw.lower() in text_lower for kw in keywords):
                cb["used"] = True


    def _update_dynamic_connections(self, state: dict):
        """Re-evaluate location connection_conditions after world_properties change.

        Locations can define conditional connections:
            {"id": "loc_a", "connections": ["loc_b"],
             "conditional_connections": [
               {"target": "loc_c", "condition": "world_properties.bridge_intact == true"}
             ]}

        When the condition becomes true, the connection is added;
        when false, it's removed. This makes the map reactive to world state.
        """
        conns = state.get("location_connections", {})
        changed = False
        for loc_def in self.script.get("locations", []):
            loc_id = loc_def.get("id", "")
            cc_list = loc_def.get("conditional_connections", [])
            if not loc_id or not cc_list:
                continue
            loc_conns = conns.get(loc_id, [])
            if not isinstance(loc_conns, list):
                loc_conns = list(loc_conns)
            for cc in cc_list:
                target = cc.get("target", "")
                condition = cc.get("condition", "")
                if not target or not condition:
                    continue
                met = self._evaluate_condition(condition, state)
                if met and target not in loc_conns:
                    loc_conns.append(target)
                    changed = True
                    reverse = conns.get(target, [])
                    if isinstance(reverse, list) and loc_id not in reverse:
                        reverse.append(loc_id)
                        conns[target] = reverse
                elif not met and target in loc_conns:
                    loc_conns.remove(target)
                    changed = True
                    reverse = conns.get(target, [])
                    if isinstance(reverse, list) and loc_id in reverse:
                        reverse.remove(loc_id)
                        conns[target] = reverse
            conns[loc_id] = loc_conns
        if changed:
            state["location_connections"] = conns

    @staticmethod
    def _build_player_profile_hint(state: dict) -> str:
        """Aggregate moral_alignment + pacing + relationship_depths into a
        concise player profile hint for Stage 1 plot_decision prompt.

        Lets the AI adapt narrative tone and NPC reactions to the player's
        established behavioral pattern.
        """
        parts: list[str] = []
        ma = state.get("moral_alignment", {})
        _AXIS = {"mercy_vs_cruelty": ("仁慈", "残忍"),
                 "honesty_vs_deception": ("诚实", "欺诈"),
                 "order_vs_chaos": ("秩序", "混沌")}
        dominant = []
        for axis, (pos, neg) in _AXIS.items():
            val = ma.get(axis, 0)
            if val >= 30:
                dominant.append(pos)
            elif val <= -30:
                dominant.append(neg)
        if dominant:
            parts.append(f"玩家行为倾向: {'/'.join(dominant)}")
        pacing = state.get("pacing_state", {})
        tension = pacing.get("tension", 50)
        trend = pacing.get("trend", "stable")
        _TREND_LABELS = {"rising": "上升", "falling": "下降", "stable": "平稳"}
        parts.append(f"紧张度{tension}/100({_TREND_LABELS.get(trend, trend)})")
        depths = state.get("npc_relationship_depths", {})
        close_npcs = [v.get("label", "") for v in depths.values()
                      if isinstance(v, dict) and v.get("level", 0) >= 3]
        if close_npcs:
            parts.append(f"亲密NPC: {', '.join(close_npcs[:3])}")
        if not parts:
            return ""
        return "## 玩家画像（自适应叙事风格参考）\n" + " | ".join(parts)

    def _build_npc_knowledge_context(self, ctx: dict, state: dict) -> str:
        """Build NPC cognitive asymmetry context from information_network.

        Each present NPC sees a different subset of facts (with possible
        distortion). This lets the AI write NPC dialogue/reactions that
        reflect what each NPC actually knows — not omniscient.
        """
        network = state.get("information_network") or []
        present_ids = set(ctx.get("present_npc_ids", []))
        if not present_ids:
            return ""
        _DISTORTION_LABELS = {0: "", 1: "（略有偏差）", 2: "（严重失真）", 3: "（面目全非）"}
        action = ctx.get("action_text", "").lower()
        action_terms = {action[index:index + 2] for index in range(len(action) - 1)
                        if action[index:index + 2].isalnum()}
        npc_facts: dict[str, list[tuple[str, str]]] = {}
        npc_sources: dict[str, list[tuple[str, str]]] = {}
        current_time = parse_time(state.get("game_time"))
        for info in network:
            known_by = set(info.get("known_by", []))
            overlapping = present_ids & known_by
            if not overlapping:
                continue
            fact = info.get("fact", "")
            if not fact:
                continue
            distortion = min(info.get("distortion", 0), 3)
            label = _DISTORTION_LABELS.get(distortion, "")
            event_id = info.get("source_event_id")
            if event_id:
                if event_id not in state.get("fired_one_time_events", []):
                    continue
                event = self._event_def_by_id.get(event_id)
                fire_time = parse_time(event.get("trigger_time")) if event else None
                known_at = info.get("known_at")
                if not current_time or not fire_time or not isinstance(known_at, dict):
                    continue
                for npc_id in overlapping:
                    learned = parse_time(known_at.get(npc_id))
                    if learned and fire_time <= learned <= current_time:
                        npc_sources.setdefault(npc_id, []).append((
                            f"- npc_id={npc_id} event_id={event_id} "
                            f"known_at={learned.isoformat()} settled=true fact={fact[:220]}{label}", fact[:220]))
                continue
            for npc_id in overlapping:
                npc_facts.setdefault(npc_id, []).append((f"{fact[:60]}{label}", fact[:60]))
        # 注入 NPC 通过 known_by_npcs 知道的 hidden 词条
        if self.prompt_builder.lorebook:
            excluded_lore_ids = self.prompt_builder._excluded_lore_ids(state)
            for npc_id in present_ids:
                for entry in self.prompt_builder.lorebook.entries:
                    if (entry.visibility == "hidden" and npc_id in entry.known_by_npcs
                            and entry.enabled and entry.id not in excluded_lore_ids):
                        search_text = " ".join(entry.keys + entry.secondary_keys) + " " + entry.content[:60]
                        npc_facts.setdefault(npc_id, []).insert(
                            0, (f"[机密][entry_id={entry.id}]{entry.content[:60]}", search_text))
        if not npc_facts and not npc_sources:
            return ""
        lines = ["## NPC 认知差异（各NPC只知道各自的信息；不代表主角知情，对话/反应须反映认知差异）"]
        for npc_id in dict.fromkeys([*npc_facts, *npc_sources]):
            facts = npc_facts.get(npc_id, [])
            if action_terms:
                facts.sort(key=lambda item: sum(term in item[1].lower() for term in action_terms),
                           reverse=True)
            if facts:
                name = self._get_npc_display_name(npc_id)
                lines.append(f"- {name}知道: {'; '.join(fact for fact, _ in facts[:3])}")
            sources = npc_sources.get(npc_id, [])
            if action_terms:
                sources.sort(key=lambda item: sum(term in item[1].lower() for term in action_terms),
                             reverse=True)
            lines.extend(source for source, _ in sources[:3])
        return "\n".join(lines)

    def _discover_lore(self, entry_id: str, source: str = ""):
        """将词条标记为主角已发现。"""
        discovered = self.current_state.setdefault("pc_discovered_lore", [])
        if entry_id not in discovered:
            discovered.append(entry_id)
            logger.info("Lore discovered: %s (source=%s)", entry_id, source)

    @staticmethod
    def _build_causal_context(state: dict) -> str:
        """Build causal chain context from choice_ripples and memory_echoes.

        Injects into compose prompt so the AI naturally weaves cause-and-effect
        references into the narrative (e.g. "you remember releasing that thief
        at the market — now he blocks your path with a gang").
        """
        parts: list[str] = []
        echoes = state.get("memory_echoes", [])
        if echoes:
            for e in echoes[:2]:
                trigger = e.get("trigger", "")
                memory = e.get("memory", "")
                if trigger and memory:
                    parts.append(f"- 记忆回响：{trigger} → {memory}")
        ripples = state.get("choice_ripples", [])
        if ripples:
            for r in ripples[:2]:
                cause = r.get("cause_action", "")
                event = r.get("current_event", "")
                if cause and event:
                    parts.append(f"- 因果伏线：因「{cause}」→ 今「{event}」")
        if not parts:
            return ""
        header = "## 因果回响（在叙事中自然暗示因果关联，不要生硬点明）"
        return header + "\n" + "\n".join(parts)

    def _compute_compose_feedback(self, narrative: str, ctx: dict):
        """Compute zero-cost feedback on Stage 3 narrative quality for next turn's Stage 1."""
        if not narrative:
            return
        issues = []
        plot_decision = ctx.get("plot_decision", "")
        if plot_decision:
            npc_names = set()
            for nid, nd in self.current_state.get("npcs", {}).items():
                if isinstance(nd, dict) and nd.get("name"):
                    npc_names.add(nd["name"])
            mentioned = [n for n in npc_names if n in plot_decision]
            if mentioned:
                missed = [n for n in mentioned if n not in narrative]
                if len(missed) > len(mentioned) * 0.5:
                    issues.append(f"骨架提及的{','.join(missed[:3])}未在叙事中出现")

        # Cross-turn phantom object detection
        self._track_dangling_clues(plot_decision, issues)

        if issues:
            self.current_state["_compose_feedback"] = "；".join(issues[:5])
        else:
            self.current_state.pop("_compose_feedback", None)

    def _track_dangling_clues(self, plot_decision: str, issues: list[str]):
        """Track objects/clues from [关键事件] and flag those unresolved after 2 turns."""
        import re as _re
        tracker = self.current_state.setdefault("_dangling_clue_tracker", [])
        turn = self.turn_number

        # Extract items from [关键事件] section
        key_event_match = _re.search(r'\[关键事件\]\s*(.+?)(?:\n\[|$)', plot_decision, _re.DOTALL)
        if key_event_match:
            event_text = key_event_match.group(1)
            # Look for noun-like props: quoted items, or items after 发现/获得/出现/看到
            prop_patterns = [
                _re.findall(r'[「「"\'](.*?)[」」"\'"]', event_text),
                _re.findall(r'(?:发现|获得|出现|看到|注意到|拿起|收到)\s*[了]?\s*(.{2,8})', event_text),
            ]
            new_clues = set()
            for matches in prop_patterns:
                for m in matches:
                    clue = m.strip()
                    if clue and len(clue) >= 2:
                        new_clues.add(clue)

            # Register new clues
            for clue in new_clues:
                if not any(c["text"] == clue for c in tracker):
                    tracker.append({"text": clue, "first_turn": turn, "resolved": False})

        # Check resolution: clue is in inventory, lorebook, or events
        known_sources = set()
        for it in self.current_state.get("inventory", []):
            known_sources.add(it.get("item", ""))
        for ev in self.current_state.get("key_events", []):
            known_sources.add(ev.get("event", ""))
        if self.prompt_builder.lorebook:
            for entry in self.prompt_builder.lorebook.entries:
                if entry.enabled:
                    known_sources.add(entry.comment or entry.id)

        for c in tracker:
            if not c["resolved"]:
                if any(c["text"] in src or src in c["text"] for src in known_sources if src):
                    c["resolved"] = True

        # Flag dangling clues (unresolved for 2+ turns)
        dangling = [c for c in tracker if not c["resolved"] and turn - c["first_turn"] >= 2]
        if dangling:
            names = ", ".join(c["text"] for c in dangling[:3])
            issues.append(f"悬空线索（连续2+回合未消化，勿再提及或需给出交代）: {names}")

        # Prune old entries (resolved or older than 5 turns)
        tracker[:] = [c for c in tracker if not c["resolved"] and turn - c["first_turn"] <= 5]

    def _update_pacing_state(self, ctx: dict):
        """Analyze recent turn events to compute narrative pacing tension."""
        tension_delta = 0
        if ctx.get("triggered_consequences"):
            tension_delta += 20
        if ctx.get("achieved_milestones"):
            tension_delta += 15
        dice_dicts = ctx.get("dice_dicts", [])
        for d in dice_dicts:
            dtype = d.get("type", "")
            if any(k in dtype for k in ("combat", "attack", "conflict", "战斗")):
                tension_delta += 15
                break
        if ctx.get("story_tree_updates"):
            st = ctx["story_tree_updates"]
            if hasattr(st, "newly_completed") and st.newly_completed:
                tension_delta += 10
                for node in st.newly_completed:
                    if node.get("type") == "choice":
                        tension_delta += 5
        npc_att = ctx.get("all_state_changes", [])
        big_att = any(
            abs((sc.get("new") or 0) - (sc.get("old") or 0)) >= 15
            for sc in npc_att
            if "relationships" in sc.get("target", "")
        )
        if big_att:
            tension_delta += 10
        if ctx.get("all_state_changes"):
            loc_changes = [sc for sc in ctx["all_state_changes"] if "location" in sc.get("target", "")]
            if loc_changes:
                tension_delta += 5
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            total = len(self.story_tree_engine._nodes)
            if total > 0:
                completed_count = len(sts.get("completed", []))
                if completed_count / total > 0.7:
                    tension_delta += 15
                if len(sts.get("active", [])) >= 3:
                    tension_delta += 10
        if tension_delta == 0:
            tension_delta = -10
        elif tension_delta <= 5:
            # Minor events: still allow slight decay
            tension_delta -= 5

        pacing = self.current_state.get("pacing_state", {
            "tension": 50, "trend": "stable",
            "consecutive_high": 0, "consecutive_low": 0,
            "history": [],
        })
        old_tension = pacing.get("tension", 50)
        new_tension = max(0, min(100, old_tension + tension_delta))
        history = pacing.get("history", [])
        history.append(new_tension)
        if len(history) > 10:
            history = history[-10:]

        if new_tension >= 70:
            pacing["consecutive_high"] = pacing.get("consecutive_high", 0) + 1
            pacing["consecutive_low"] = 0
        elif new_tension <= 30:
            pacing["consecutive_low"] = pacing.get("consecutive_low", 0) + 1
            pacing["consecutive_high"] = 0
        else:
            # Hysteresis: 只有回到中间区域（40-60）才重置对侧计数器
            if new_tension <= 50:
                pacing["consecutive_high"] = 0
            if new_tension >= 50:
                pacing["consecutive_low"] = 0

        if len(history) >= 3:
            recent = history[-3:]
            if recent[-1] > recent[0] + 10:
                pacing["trend"] = "rising"
            elif recent[-1] < recent[0] - 10:
                pacing["trend"] = "falling"
            else:
                pacing["trend"] = "stable"
        else:
            pacing["trend"] = "stable"

        rec = ""
        if pacing["consecutive_high"] >= 3:
            rec = "建议插入喘息/日常场景，缓解连续紧张"
        elif pacing["consecutive_low"] >= 3:
            rec = "⚠ 节奏警告：连续低紧张回合，本轮必须引入新信息或推进。方式：NPC主动带来消息/旧线索有新发现/外部势力动态影响到玩家/[世界脉搏]中的事态升级"
        pacing["recommendation"] = rec
        pacing["tension"] = new_tension
        pacing["history"] = history
        self.current_state["pacing_state"] = pacing

    def _track_world_pulse(self, ctx: dict):
        """Track world pulse hints from plot_decision; expire stale ones."""
        plot_decision = ctx.get("plot_decision", "")
        pulse_re = re.compile(r'\[世界脉搏\]\s*(.*?)(?:\n\[|$)', re.DOTALL)
        pulses = self.current_state.setdefault("world_pulse_tracker", [])

        # Extract new hints from this turn's plot_decision
        if plot_decision:
            m = pulse_re.search(plot_decision)
            if m:
                text = m.group(1).strip()
                if text and len(text) > 5:
                    pulses.append({
                        "turn": self.turn_number,
                        "text": text[:150],
                        "status": "pending",
                        "expires_at": self.turn_number + random.randint(3, 6),
                    })

        # Expire hints past their individual expiry turn
        expired_texts = []
        for p in pulses:
            if p["status"] == "pending" and self.turn_number >= p.get("expires_at", p["turn"] + 5):
                p["status"] = "expired"
                expired_texts.append(p["text"][:60])

        # Tension reduction for expired hints
        if expired_texts:
            pacing = self.current_state.get("pacing_state", {})
            pacing["tension"] = max(0, pacing.get("tension", 50) - 5 * len(expired_texts))
            self.current_state["pacing_state"] = pacing

        # Store as soft suggestion for Stage 1 (not mandatory)
        if expired_texts:
            existing = self.current_state.get("_pulse_dissolves", [])
            for t in expired_texts:
                existing.append({"text": t, "shown": 0})
            self.current_state["_pulse_dissolves"] = existing[-5:]

        # Keep tracker compact: remove resolved entries older than 10 turns
        pulses[:] = [p for p in pulses if self.turn_number - p["turn"] < 10]

        # Increment shown count on dissolves (skip newly added ones with shown=0)
        dissolves = self.current_state.get("_pulse_dissolves", [])
        if dissolves:
            for d in dissolves:
                if d.get("shown", 0) > 0:
                    d["shown"] = d["shown"] + 1
                else:
                    d["shown"] = 1  # first turn: mark as shown=1, will be injected this turn
            self.current_state["_pulse_dissolves"] = [d for d in dissolves if d["shown"] <= 3]

    def _check_npc_goals(self):
        """Check NPC goal completion conditions and record callbacks."""
        completed_goals = self.current_state.get("completed_npc_goals", [])
        completed_ids = {(g["id"] if isinstance(g, dict) else g) for g in completed_goals}
        goal_progress = self.current_state.get("npc_goal_progress", {})
        all_npc_defs = list(self.script.get("npcs", []))
        npcs_state = self.current_state.get("npcs", {})
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_goals = dyn_st.get("goals", [])
                if dyn_goals:
                    all_npc_defs.append({
                        "id": dyn_id,
                        "name": dyn_st.get("name", dyn_id),
                        "goals": dyn_goals,
                    })
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            goals = npc_def.get("goals", [])
            if not goals:
                continue
            npc_name = npc_def.get("name", npc_id)
            for goal in goals:
                goal_id = goal.get("id", "")
                full_id = f"{npc_id}:{goal_id}"
                if full_id in completed_ids:
                    continue
                condition = goal.get("condition_met", "")
                condition_met = condition and self._evaluate_condition(condition)
                offscreen_met = goal_progress.get(npc_id, {}).get("progress", 0) >= 100
                if condition_met or offscreen_met:
                    desc = goal.get("description", goal_id)
                    source = "offscreen" if offscreen_met and not condition_met else "condition"
                    completed_goals.append({
                        "id": full_id,
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "description": desc,
                        "turn": self.turn_number,
                        "source": source,
                    })
                    completed_ids.add(full_id)
                    self._record_narrative_callback(
                        f"NPC「{npc_name}」达成目标「{desc}」",
                        ["npc_goal", npc_id], priority="high",
                    )
                    network = self.current_state.setdefault("information_network", [])
                    network.append({
                        "id": f"npc_goal_{npc_id}_{self.turn_number}",
                        "origin_turn": self.turn_number,
                        "fact": f"{npc_name}完成了目标「{desc[:40]}」",
                        "known_by": [npc_id],
                        "spread_chance": 0.5,
                        "distortion": 0,
                        "max_spread": 5,
                        "tags": ["npc_goal", npc_id],
                    })
                    if len(network) > 30:
                        self.current_state["information_network"] = network[-30:]
        self.current_state["completed_npc_goals"] = completed_goals

        if self.event_engine:
            for npc_id, gp_data in goal_progress.items():
                progress = gp_data.get("progress", 0)
                if progress < 70 or progress >= 100:
                    continue
                cons_id = f"_npc_goal_imm_{npc_id}"
                if cons_id in self.event_engine.events:
                    continue
                npc_name = self._get_npc_display_name(npc_id)
                npc_def = self._npc_by_id.get(npc_id, {})
                goals = npc_def.get("goals", [])
                active_goals = [g for g in goals if f"{npc_id}:{g.get('id', '')}" not in completed_ids]
                if not active_goals:
                    continue
                goal_desc = active_goals[0].get("description", "")[:40]
                self.event_engine.add_consequence(self.current_state, {
                    "id": cons_id,
                    "description": f"{npc_name}正在积极推进「{goal_desc}」（进度{progress}%）",
                    "trigger_chance": 0.4,
                    "turns_delay": 2,
                    "max_turns": 6,
                    "importance": "high",
                    "related_npcs": [npc_id],
                    "on_trigger": {
                        "description": f"{npc_name}的计划「{goal_desc}」即将实现，世界将因此改变",
                    },
                })

    def _check_org_goals(self):
        """Check organization goal completion conditions and record callbacks."""
        completed_goals = self.current_state.get("completed_org_goals", [])
        completed_ids = {(g["id"] if isinstance(g, dict) else g) for g in completed_goals}
        for org_def in self.script.get("organizations", []):
            org_id = org_def.get("id", "")
            goals = org_def.get("goals", [])
            if not goals:
                continue
            org_name = org_def.get("name", org_id)
            for goal in goals:
                goal_id = goal.get("id", "")
                full_id = f"{org_id}:{goal_id}"
                if full_id in completed_ids:
                    continue
                condition = goal.get("condition_met", "")
                if not condition:
                    continue
                if self._evaluate_condition(condition):
                    desc = goal.get("description", goal_id)
                    completed_goals.append({
                        "id": full_id,
                        "org_id": org_id,
                        "org_name": org_name,
                        "description": desc,
                        "turn": self.turn_number,
                    })
                    completed_ids.add(full_id)
                    self._record_narrative_callback(
                        f"组织「{org_name}」达成目标「{desc}」",
                        ["org_goal", org_id], priority="high",
                    )
                    self._apply_org_goal_effects(goal, org_id, org_name, desc)
        self.current_state["completed_org_goals"] = completed_goals

    def _apply_org_goal_effects(self, goal: dict, org_id: str, org_name: str, desc: str):
        """Apply on_complete_effects from an organization goal and inject into information_network."""
        effects = goal.get("on_complete_effects", {})
        if effects:
            for sc in effects.get("state_change", []):
                self.current_state, _ = self.state_manager.apply_changes(
                    self.current_state, [sc], inplace=True,
                )
            for loc_id in effects.get("unlock_locations", []):
                if not self._is_duplicate_location(self.current_state, loc_id):
                    self.current_state = self.state_manager.reveal_location(
                        self.current_state, loc_id, inplace=True,
                    )
            for rep in effects.get("set_reputation", []):
                faction = rep.get("faction", "")
                value = rep.get("value")
                if faction and value is not None:
                    factions = self.current_state.setdefault("faction_reputation", {})
                    entry = factions.setdefault(faction, {"value": 50})
                    if isinstance(entry, dict):
                        entry["value"] = value

        network = self.current_state.setdefault("information_network", [])
        known_by = []
        for org in self.script.get("organizations", []):
            if org.get("id") == org_id:
                for member in org.get("members", []):
                    mid = member if isinstance(member, str) else member.get("id", "")
                    if mid:
                        known_by.append(mid)
                break
        network.append({
            "id": f"org_goal_{org_id}_{self.turn_number}",
            "origin_turn": self.turn_number,
            "fact": f"{org_name}达成了目标「{desc[:40]}」",
            "known_by": known_by,
            "spread_chance": 0.6,
            "distortion": 0,
            "max_spread": 6,
            "tags": ["org_goal", org_id],
        })
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    # ================================================================
    # Feature #2: Promise/Lie Tracking System
    # ================================================================

    # _check_promise_expirations removed — promises are now deadline events,
    # expiry handled by event_engine._tick_deadlines + deadline_results processing below.


    # ================================================================
    # Feature #8: NPC Goal Conflict Detection
    # ================================================================


    # ================================================================
    # Feature #3: Player Multi-turn Planning System
    # ================================================================


    # ================================================================
    # Feature #5: Retroactive Revelation (Flashbacks)
    # ================================================================


    # ================================================================
    # Feature #4: Faction Warfare Simulator
    # ================================================================


    # ================================================================
    # StoryDirector: 统一后台剧情系统
    # ================================================================


    def _check_quest_templates(self):
        """Check quest_templates conditions and populate available_quests."""
        templates = self.script.get("quest_templates", [])
        if not templates:
            return
        available = self.current_state.get("available_quests", [])
        available_ids = {q["id"] for q in available}
        cooldowns = self.current_state.get("quest_cooldowns", {})

        for tpl in templates:
            tpl_id = tpl.get("id", "")
            if not tpl_id or tpl_id in available_ids:
                continue
            cd_until = cooldowns.get(tpl_id, 0)
            if self.turn_number < cd_until:
                continue
            condition = tpl.get("condition", "")
            if condition and not self._evaluate_condition(condition):
                continue
            available.append({
                "id": tpl_id,
                "name": tpl.get("name", tpl_id),
                "trigger_hint": tpl.get("trigger_hint", ""),
                "reward_hint": tpl.get("reward_hint", ""),
                "activated_turn": self.turn_number,
            })
            cooldowns[tpl_id] = self.turn_number + tpl.get("cooldown_turns", 10)
        self.current_state["available_quests"] = available
        self.current_state["quest_cooldowns"] = cooldowns

    def _check_npc_interventions(self):
        """Evaluate conditions for NPC proactive interventions.

        Types: confrontation, aid, warning, ambush, quest_offer.
        Results are written to state["npc_interventions"] for prompt injection.
        """
        state = self.current_state
        interventions = []
        cooldowns = state.get("npc_intervention_cooldowns", {})
        player_loc = state.get("player", {}).get("location", "")
        network = state.get("information_network", [])
        pacing = state.get("pacing_state", {})
        tension = pacing.get("tension", 50)
        low_pacing = pacing.get("consecutive_low", 0) >= 4 and tension <= 25
        completed_goals = {(g["id"] if isinstance(g, dict) else g) for g in state.get("completed_npc_goals", [])}

        player_attrs = state.get("player", {}).get("attributes", {})
        player_low_health = False
        for attr_name, val in player_attrs.items():
            v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
            mx = val.get("max", 100) if isinstance(val, dict) else 100
            if any(k in attr_name for k in ("hp", "health", "生命", "体力")) and v < mx * 0.3:
                player_low_health = True
                break

        # 合并脚本NPC和动态NPC，确保动态NPC也能参与介入
        all_npc_defs = list(self.script.get("npcs", []))
        for dyn_id, dyn_st in state.get("npcs", {}).items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                all_npc_defs.append({
                    "id": dyn_id,
                    "name": dyn_st.get("name", dyn_id),
                    "capabilities": dyn_st.get("capabilities", ""),
                    "goals": [],
                    "faction": dyn_st.get("faction", ""),
                })

        for npc_def in all_npc_defs:
            if len(interventions) >= 2:
                break
            npc_id = npc_def.get("id", "")
            if not npc_id:
                continue
            cd_until = cooldowns.get(npc_id, 0)
            if self.turn_number < cd_until:
                continue

            npc_name = npc_def.get("name", npc_id)
            npc_state = state.get("npcs", {}).get(npc_id, {})
            if not isinstance(npc_state, dict):
                continue
            attitude = npc_state.get("attitude_toward_player", 50)
            npc_loc = self._get_npc_location(npc_id)
            is_present = npc_loc and player_loc and self._locations_match(player_loc, npc_loc)
            is_nearby = False
            if not is_present and npc_loc and player_loc:
                player_loc_def = self._location_by_id.get(player_loc, {})
                connections = player_loc_def.get("connections", [])
                if npc_loc in connections:
                    is_nearby = True

            capabilities = npc_def.get("capabilities", "")
            has_combat = any(k in capabilities for k in ("战斗", "攻击", "武力", "执法", "守卫", "combat", "fight"))
            has_healing = any(k in capabilities for k in ("治疗", "医术", "heal", "治愈", "魔法"))

            # --- confrontation ---
            if attitude < 30 and has_combat and (is_present or is_nearby):
                negative_info = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and any(t in info.get("tags", []) for t in ("violence", "theft", "crime", "player_action"))
                ]
                if negative_info:
                    info = negative_info[-1]
                    distorted = "（传闻失真）" if info.get("distortion", 0) >= 2 else ""
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "confrontation",
                        "reason": f"得知：{info['fact'][:40]}{distorted}",
                        "urgency": "high",
                        "suggested_action": f"{npc_name}拦住你的去路，质问此事",
                    })
                    cooldowns[npc_id] = self.turn_number + 5
                    continue

            # --- aid ---
            if attitude > 75 and player_low_health and (is_present or is_nearby):
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "type": "aid",
                    "reason": "察觉你状态不佳",
                    "urgency": "medium",
                    "suggested_action": f"{npc_name}主动{'赶来' if is_nearby else '上前'}关心你的状况" + (f"并施以{capabilities[:10]}" if has_healing else ""),
                })
                cooldowns[npc_id] = self.turn_number + 5
                continue

            # --- warning ---
            if attitude > 50 and is_present:
                danger_info = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and any(t in info.get("tags", []) for t in ("danger", "threat", "ambush"))
                ]
                if danger_info:
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "warning",
                        "reason": f"获知：{danger_info[-1]['fact'][:40]}",
                        "urgency": "medium",
                        "suggested_action": f"{npc_name}低声警告你注意危险",
                    })
                    cooldowns[npc_id] = self.turn_number + 5
                    continue

            # --- ambush ---
            if attitude < 20 and has_combat and tension < 40 and (is_present or is_nearby):
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "type": "ambush",
                    "reason": f"对你怀有敌意（好感{attitude}）",
                    "urgency": "high",
                    "suggested_action": f"{npc_name}{'突然出现并' if is_nearby else ''}对你发起攻击",
                })
                cooldowns[npc_id] = self.turn_number + 8
                continue

            # --- quest_offer ---
            quest_attitude_threshold = 40 if low_pacing else 60
            if attitude > quest_attitude_threshold and is_present:
                goals = npc_def.get("goals", [])
                active_goals = [g for g in goals if f"{npc_id}:{g.get('id', '')}" not in completed_goals]
                if active_goals:
                    goal = active_goals[0]
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "quest_offer",
                        "reason": f"需要帮助完成目标「{goal.get('description', '')[:20]}」",
                        "urgency": "low",
                        "suggested_action": f"{npc_name}向你提出请求",
                    })
                    cooldowns[npc_id] = self.turn_number + 10

            # --- goal_pursuit: NPC actively pursuing their goal ---
            if not any(iv["npc_id"] == npc_id for iv in interventions):
                gp_data = state.get("npc_goal_progress", {}).get(npc_id, {})
                gp_progress = gp_data.get("progress", 0)
                if gp_progress >= 50 and is_present:
                    goals = npc_def.get("goals", [])
                    active_goals = [g for g in goals if f"{npc_id}:{g.get('id', '')}" not in completed_goals]
                    if active_goals:
                        goal = active_goals[0]
                        gp_urgency = "high" if gp_progress >= 80 else "medium"
                        gp_action = gp_data.get("last_action", "")
                        interventions.append({
                            "npc_id": npc_id,
                            "npc_name": npc_name,
                            "type": "goal_pursuit",
                            "reason": f"目标「{goal.get('description', '')[:20]}」进度{gp_progress}%",
                            "urgency": gp_urgency,
                            "suggested_action": f"{npc_name}正在采取行动推进自己的计划" + (f"（{gp_action[:20]}）" if gp_action else ""),
                        })
                        cooldowns[npc_id] = self.turn_number + 4

            # --- info_share: NPC shares valuable knowledge based on relationship ---
            if attitude > 65 and is_present and not any(iv["npc_id"] == npc_id for iv in interventions):
                shareable = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and info.get("distortion", 0) <= 1
                    and any(t in info.get("tags", []) for t in
                            ("danger", "threat", "treasure", "secret", "quest", "org_goal", "offscreen"))
                ]
                if shareable:
                    info = shareable[-1]
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "info_share",
                        "reason": f"掌握情报：{info['fact'][:40]}",
                        "urgency": "low",
                        "suggested_action": f"{npc_name}凑近你，透露一个消息",
                    })
                    cooldowns[npc_id] = self.turn_number + 8

            # --- misunderstanding: high-distortion info causes wrong reaction ---
            if 30 <= attitude <= 60 and (is_present or is_nearby) and not any(iv["npc_id"] == npc_id for iv in interventions):
                distorted_info = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and info.get("distortion", 0) >= 2
                    and any(t in info.get("tags", []) for t in ("player_action", "crime", "offscreen"))
                ]
                if distorted_info:
                    info = distorted_info[-1]
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "misunderstanding",
                        "reason": f"听到失实传闻：{info['fact'][:40]}（失真度{info['distortion']}）",
                        "urgency": "medium",
                        "suggested_action": f"{npc_name}态度突变，似乎对你产生了误解",
                    })
                    cooldowns[npc_id] = self.turn_number + 6

            # --- conflict_plea: NPC asks player to side with them in a conflict ---
            npc_conflicts = state.get("npc_conflicts", [])
            if attitude > 50 and is_present and not any(iv["npc_id"] == npc_id for iv in interventions):
                for conflict in npc_conflicts:
                    if conflict.get("status") != "active":
                        continue
                    if conflict.get("player_sided_with"):
                        continue
                    if npc_id in (conflict.get("npc_a"), conflict.get("npc_b")):
                        opponent = conflict["npc_b"] if npc_id == conflict["npc_a"] else conflict["npc_a"]
                        opponent_name = self._get_npc_display_name(opponent)
                        interventions.append({
                            "npc_id": npc_id,
                            "npc_name": npc_name,
                            "type": "conflict_plea",
                            "reason": f"与{opponent_name}存在冲突，希望获得你的帮助",
                            "urgency": "medium",
                            "suggested_action": f"{npc_name}恳请你在与{opponent_name}的争端中支持自己",
                        })
                        cooldowns[npc_id] = self.turn_number + 8
                        break

            # --- faction_recruit: faction at war tries to recruit player ---
            faction_wars = state.get("faction_wars", [])
            if is_present and not any(iv["npc_id"] == npc_id for iv in interventions):
                npc_org = self._get_npc_org(npc_id)
                if npc_org:
                    for war in faction_wars:
                        if war.get("status") not in ("skirmish", "open_war"):
                            continue
                        if npc_org in (war.get("faction_a"), war.get("faction_b")):
                            interventions.append({
                                "npc_id": npc_id,
                                "npc_name": npc_name,
                                "type": "faction_recruit",
                                "reason": f"所属势力正处于{'全面战争' if war['status'] == 'open_war' else '冲突'}中",
                                "urgency": "low",
                                "suggested_action": f"{npc_name}试图拉拢你加入{'战斗' if war['status'] == 'open_war' else '他们一方'}",
                            })
                            cooldowns[npc_id] = self.turn_number + 10
                            break

        state["npc_interventions"] = interventions
        state["npc_intervention_cooldowns"] = cooldowns
        # Feature #1: Scene hijack — high urgency intervention takes over the scene
        if interventions and interventions[0].get("urgency") == "high":
            state["_scene_hijack"] = interventions[0]

    def _collect_interactables(self):
        """Collect interactable elements at the player's current location."""
        state = self.current_state
        player_loc = state.get("player", {}).get("location", "")
        if not player_loc:
            state["location_interactables"] = []
            return

        loc_def = self._location_by_id.get(player_loc, {})
        raw = loc_def.get("interactables", [])
        if not raw:
            state["location_interactables"] = []
            return

        used = set(state.get("used_interactables", []))
        skill_growth = state.get("skill_growth", {})
        result = []
        for item in raw:
            iid = item.get("id", "")
            if not iid:
                continue
            is_used = item.get("one_time") and iid in used
            condition = item.get("condition", "")
            if condition and not is_used and not self._evaluate_condition(condition):
                continue
            entry = {
                "id": iid,
                "name": item.get("name", iid),
                "description": item.get("description", ""),
                "action_hint": item.get("action_hint", ""),
            }
            if is_used:
                entry["used"] = True
            if item.get("required_item"):
                entry["required_item"] = item["required_item"]
            req_skill = item.get("required_skill", {})
            if req_skill:
                skill_name = req_skill.get("skill", "")
                req_level = req_skill.get("level", 1)
                cur_level = skill_growth.get(skill_name, {}).get("level", 0)
                if cur_level < req_level:
                    entry["locked_skill"] = f"{skill_name}≥{req_level}级（当前{cur_level}级）"
            result.append(entry)
        state["location_interactables"] = result

    def _mark_used_interactables(self, player_action: dict):
        """Mark one_time interactables as used if the player action matches."""
        action_text = player_action.get("text", "")
        if not action_text:
            return
        player_loc = self.current_state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(player_loc, {})
        raw = loc_def.get("interactables", [])
        if not raw:
            return
        used = self.current_state.setdefault("used_interactables", [])
        for item in raw:
            iid = item.get("id", "")
            if not iid or iid in used:
                continue
            hint = item.get("action_hint", "")
            name = item.get("name", "")
            if not (hint and hint in action_text) and not (name and name in action_text):
                continue
            if item.get("condition") and not self._evaluate_condition(item["condition"]):
                continue
            if item.get("one_time"):
                used.append(iid)
            for loc_id in item.get("reveals", []):
                if not self._is_duplicate_location(self.current_state, loc_id):
                    self.current_state = self.state_manager.reveal_location(
                        self.current_state, loc_id, inplace=True
                    )
            sc = item.get("state_changes", [])
            if sc:
                self.current_state, _ = self.state_manager.apply_changes(
                    self.current_state, sc, inplace=True
                )

    def _compute_time_atmosphere(self):
        """Compute time-of-day atmosphere from game_time and write to state."""
        game_time = self.current_state.get("game_time", "")
        if not game_time:
            return
        try:
            dt = parse_time(game_time)
            if not dt:
                return
        except Exception:
            return
        hour = dt.hour
        _PERIODS = [
            (5, 7, "dawn", "黎明", "dim", "天边泛起微光，空气清冷而新鲜"),
            (7, 12, "morning", "上午", "bright", "阳光明媚，正是活动的好时候"),
            (12, 14, "noon", "午间", "bright", "烈日当空，热浪蒸腾"),
            (14, 17, "afternoon", "午后", "bright", "午后的暖意弥漫"),
            (17, 19, "dusk", "黄昏", "dim", "落日余晖中，影子被拉长"),
            (19, 22, "night", "夜晚", "dark", "夜幕降临，灯火渐明"),
            (22, 24, "late_night", "深夜", "dark", "万籁俱寂，只有虫鸣"),
            (0, 5, "late_night", "深夜", "dark", "夜深人静，世界沉睡"),
        ]
        period = "day"
        label = "白天"
        light = "bright"
        mood = ""
        for start, end, p, l, li, m in _PERIODS:
            if start <= hour < end:
                period, label, light, mood = p, l, li, m
                break
        effects = []
        if light == "dark":
            effects.append("潜行/隐蔽+5")
            effects.append("视觉观察-5")
        elif light == "dim":
            effects.append("潜行/隐蔽+3")
        self.current_state["time_atmosphere"] = {
            "period": period,
            "period_label": label,
            "light_level": light,
            "mood_hint": mood,
            "gameplay_effects": effects,
            "hour": hour,
        }

    def _compute_difficulty_awareness(self):
        """Track recent check results and compute difficulty adjustment."""
        state = self.current_state
        history = state.setdefault("_check_result_history", [])
        # Recent window: last 10 checks
        recent = history[-10:] if len(history) > 10 else history
        if len(recent) < 3:
            return
        successes = sum(1 for r in recent if r in ("success", "critical_success"))
        failures = len(recent) - successes
        rate = successes / len(recent) if recent else 0.5
        if rate <= 0.3:
            momentum = "struggling"
            adjustment = "ease"
            hint = "玩家近期频繁失败，可以给予一些意外的帮助、线索或转机"
        elif rate >= 0.8:
            momentum = "dominating"
            adjustment = "challenge"
            hint = "玩家几乎全部成功，可以适当增加复杂度或引入意外挫折"
        else:
            momentum = "balanced"
            adjustment = "neutral"
            hint = ""
        state["difficulty_awareness"] = {
            "success_rate": round(rate, 2),
            "player_momentum": momentum,
            "adjustment": adjustment,
            "hint_for_ai": hint,
        }

    def _compute_npc_relationship_depth(self):
        """Calculate relationship depth for each known NPC based on interaction counts."""
        dlg = self.current_state.get("npc_dialogue_counts", {})
        enc = self.current_state.get("npc_encounter_counts", {})
        npcs_state = self.current_state.get("npcs", {})
        depths = {}
        all_npc_ids = set()
        for npc in self.script.get("npcs", []):
            all_npc_ids.add(npc["id"])
        for dyn_id in npcs_state:
            if dyn_id not in all_npc_ids:
                all_npc_ids.add(dyn_id)
        for npc_id in all_npc_ids:
            npc_data = npcs_state.get(npc_id, {})
            if not isinstance(npc_data, dict) or not npc_data.get("known"):
                continue
            talks = dlg.get(npc_id, 0)
            meets = enc.get(npc_id, 0)
            score = talks * 3 + meets
            if score >= 30:
                level, label = 4, "挚友"
            elif score >= 18:
                level, label = 3, "朋友"
            elif score >= 10:
                level, label = 2, "熟人"
            elif score >= 4:
                level, label = 1, "点头之交"
            else:
                level, label = 0, "陌生人"
            attitude = npc_data.get("attitude_toward_player", 50)
            if attitude < 30 and level > 1:
                level, label = 1, "点头之交"
            elif attitude < 50 and level > 2:
                level, label = 2, "熟人"
            elif attitude < 65 and level > 3:
                level, label = 3, "朋友"
            depths[npc_id] = {
                "level": level, "label": label,
                "talks": talks, "encounters": meets, "score": score,
            }
        self.current_state["npc_relationship_depths"] = depths

    def _compute_discovery_hints(self):
        """Calculate exploration progress, nearby hints, and achievements."""
        state = self.current_state
        vis = set(state.get("visible_locations", []))
        all_locs = [loc["id"] for loc in self.script.get("locations", [])]
        all_npcs = self.script.get("npcs", [])
        milestones = self.script.get("milestones", [])
        achieved = set(state.get("achieved_milestones", []))

        npcs_met = sum(
            1 for n in state.get("npcs", {}).values()
            if isinstance(n, dict) and n.get("met", n.get("known", False))
        )

        progress = {
            "locations_found": len(vis), "locations_total": len(all_locs),
            "npcs_met": npcs_met, "npcs_total": max(len(all_npcs), len(state.get("npcs", {}))),
            "milestones_done": len(achieved), "milestones_total": len(milestones),
        }

        nearby = []
        player_loc = state.get("player", {}).get("location", "")
        conns = state.get("location_connections", {})
        if player_loc and player_loc in conns:
            for adj in conns[player_loc]:
                if adj not in vis:
                    loc_def = self._location_by_id.get(adj)
                    hint = loc_def.get("discovery_hint", "附近似乎还有未探索的区域...") if loc_def else "附近似乎还有未探索的区域..."
                    nearby.append({"type": "unvisited_location", "hint": hint})
                    if len(nearby) >= 2:
                        break

        for npc in all_npcs:
            npc_id = npc["id"]
            npc_state = state.get("npcs", {}).get(npc_id, {})
            if isinstance(npc_state, dict) and not npc_state.get("known"):
                dl = npc.get("default_location", "")
                if dl and dl == player_loc:
                    nearby.append({"type": "unmet_npc", "hint": f"据说这里有一位{npc.get('name', '神秘人')}..."})
                    if len(nearby) >= 3:
                        break

        npcs_total = max(len(all_npcs), len(state.get("npcs", {})))
        achievements = []
        enc_counts = state.get("npc_encounter_counts", {})
        total_enc = sum(enc_counts.values())
        achievements.append({"id": "explorer", "name": "探索者", "progress": f"{len(vis)}/{max(len(all_locs),1)}", "earned": len(vis) >= len(all_locs) and len(all_locs) > 0})
        achievements.append({"id": "social", "name": "交际家", "progress": f"{npcs_met}/{max(npcs_total,1)}", "earned": npcs_met >= npcs_total and npcs_total > 0})
        achievements.append({"id": "veteran", "name": "冒险老手", "progress": f"{self.turn_number}/50", "earned": self.turn_number >= 50})

        state["discovery_hints"] = {
            "exploration_progress": progress,
            "nearby_hints": nearby,
            "achievements": achievements,
        }

    def _compute_active_effects_summary(self):
        """Aggregate all active effects (persistent states, time atmosphere, weather) into a summary list."""
        state = self.current_state
        summary = []
        _ICONS = {"buff": "\U0001F6E1", "debuff": "\U0001F480", "status": "\U0001F4A0", "environment": "\U0001F30D"}
        for sid in state.get("active_persistent_states", []):
            ps_def = self._ps_by_id.get(sid, {})
            name = ps_def.get("name") or state.get("display_names", {}).get(sid, sid)
            desc = state.get("persistent_state_descriptions", {}).get(sid, ps_def.get("description", ""))
            effects = []
            for eff in ps_def.get("effects", []):
                if isinstance(eff, str):
                    effects.append(eff)
                elif isinstance(eff, dict):
                    effects.append(eff.get("description", str(eff)))
            cat = ps_def.get("category", "status")
            summary.append({"id": sid, "name": name, "description": desc, "source": "persistent_state", "effects": effects, "icon": _ICONS.get(cat, "\U0001F4A0")})
        atmo = state.get("time_atmosphere", {})
        if atmo.get("gameplay_effects"):
            summary.append({"id": "_time", "name": atmo.get("period_label", ""), "description": atmo.get("mood_hint", ""), "source": "time_atmosphere", "effects": atmo["gameplay_effects"], "icon": "\U0001F319" if atmo.get("light_level") == "dark" else "☀"})
        weather = state.get("current_weather", "")
        if weather and isinstance(weather, str):
            w_effects = []
            active_ps = state.get("active_persistent_states", [])
            if "weather_extreme" in active_ps:
                w_effects.append("所有户外活动受阻")
            elif "weather_storm" in active_ps:
                w_effects.append("户外行动困难")
            elif "weather_rain" in active_ps:
                w_effects.append("视野受限")
            if w_effects:
                summary.append({"id": "_weather", "name": weather, "description": "", "source": "weather", "effects": w_effects, "icon": "☁"})
        state["active_effects_summary"] = summary

    # ================================================================
    # Player XP / Level / Skill Tree
    # ================================================================

    def _compute_xp_and_level(self, ctx: dict):
        xp_config = self.script.get("player_character", {}).get("xp_config", {})
        if not xp_config and not self.script.get("player_character", {}).get("skill_trees"):
            return
        state = self.current_state
        state["_level_up_this_turn"] = False
        milestone_xp = xp_config.get("milestone_xp", 25)
        check_success_xp = xp_config.get("check_success_xp", 10)
        thresholds = xp_config.get("level_thresholds", [0, 100, 300, 600, 1000, 1500])

        prev_xp = state.get("player_xp", 0)
        prev_level = state.get("player_level", 1)
        xp_gained = 0

        milestones = ctx.get("achieved_milestones") or []
        xp_gained += len(milestones) * milestone_xp

        check_result = ctx.get("check_result")
        if isinstance(check_result, dict) and check_result.get("outcome") in ("success", "critical_success"):
            xp_gained += check_success_xp
            if check_result.get("outcome") == "critical_success":
                xp_gained += check_success_xp // 2

        if xp_gained <= 0:
            return

        new_xp = prev_xp + xp_gained
        new_level = prev_level
        while new_level + 1 < len(thresholds) and new_xp >= thresholds[new_level]:
            new_level += 1

        state["player_xp"] = new_xp
        state["player_level"] = new_level
        if new_level > prev_level:
            state["_level_up_this_turn"] = True
            self._record_narrative_callback(
                f"经验积累，你的等级提升到了 {new_level} 级",
                ["level_up"], "high",
            )

    def unlock_skill(self, skill_id: str) -> dict:
        trees = self.script.get("player_character", {}).get("skill_trees", [])
        if not trees:
            return {"success": False, "message": "当前剧本没有技能树"}
        all_skills = {}
        for tree in trees:
            for sk in tree.get("skills", []):
                all_skills[sk.get("id", "")] = sk
        skill = all_skills.get(skill_id)
        if not skill:
            return {"success": False, "message": f"技能 {skill_id} 不存在"}

        state = self.current_state
        unlocked = state.setdefault("unlocked_skills", [])
        if skill_id in unlocked:
            return {"success": False, "message": "该技能已解锁"}

        for pre in skill.get("prerequisites", []):
            if pre not in unlocked:
                pre_name = all_skills.get(pre, {}).get("name", pre)
                return {"success": False, "message": f"前置技能「{pre_name}」未解锁"}

        cost = skill.get("cost", 50)
        cur_xp = state.get("player_xp", 0)
        if cur_xp < cost:
            return {"success": False, "message": f"XP 不足（需要 {cost}，当前 {cur_xp}）"}

        state["player_xp"] = cur_xp - cost
        unlocked.append(skill_id)

        effect = skill.get("effect", {})
        if effect:
            changes = []
            if effect.get("target"):
                changes.append({"target": effect["target"], "value": effect.get("value", 0),
                                "op": effect.get("op", "add"), "reason": f"解锁技能：{skill.get('name', skill_id)}"})
            if changes:
                applied, log = self.state_manager.apply_changes(state, changes, inplace=True)
                self.current_state = applied

        return {"success": True, "message": f"解锁了技能「{skill.get('name', skill_id)}」",
                "state": self.current_state}

    # ================================================================
    # Economy / Trading System
    # ================================================================


    def _trace_choice_ripples(self, ctx: dict):
        """Trace causal chains from past player choices to current events."""
        ripples: list[dict] = []
        state = self.current_state

        # Source 1: consequence triggers — they carry origin info
        triggered_cons = ctx.get("triggered_consequences", [])
        for csq in (triggered_cons or []):
            origin_turn = csq.get("origin_turn", 0)
            origin_desc = csq.get("origin_description", "")
            cur_desc = csq.get("description", "")
            if origin_turn and origin_desc:
                ripples.append({
                    "current_event": cur_desc[:60],
                    "cause_turn": origin_turn,
                    "cause_action": origin_desc[:60],
                    "chain": [origin_desc[:40], cur_desc[:40]],
                    "impact_type": "negative" if csq.get("severity") in ("high", "critical") else "neutral",
                })

        # Source 2: NPC attitude big shifts — trace via information_network
        info_net = state.get("information_network", [])
        rollback = ctx.get("rollback_state", {})
        for npc_id, npc_data in state.get("npcs", {}).items():
            if not isinstance(npc_data, dict):
                continue
            cur_att = npc_data.get("attitude_toward_player", 50)
            old_att = rollback.get("npcs", {}).get(npc_id, {}).get("attitude_toward_player", 50) if rollback else cur_att
            delta = cur_att - old_att
            if abs(delta) < 15:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            cause = ""
            cause_turn = 0
            for info in reversed(info_net):
                if info.get("target_npc") == npc_id or npc_id in info.get("known_by", []):
                    cause = info.get("fact", "")[:60]
                    cause_turn = info.get("origin_turn", info.get("turn", 0))
                    break
            if cause and cause_turn:
                ripples.append({
                    "current_event": f"{npc_name}对你的态度{'大幅改善' if delta > 0 else '急剧恶化'}",
                    "cause_turn": cause_turn,
                    "cause_action": cause,
                    "chain": [cause[:40], f"态度变化{delta:+d}"],
                    "impact_type": "positive" if delta > 0 else "negative",
                })

        # Source 3: faction reputation changes
        factions = state.get("faction_reputation", {})
        old_factions = rollback.get("faction_reputation", {}) if rollback else {}
        for fid, cur_rep in factions.items():
            old_rep = old_factions.get(fid, cur_rep)
            if isinstance(cur_rep, dict):
                cur_val = cur_rep.get("value", 50)
                old_val = old_rep.get("value", 50) if isinstance(old_rep, dict) else 50
            else:
                cur_val = cur_rep
                old_val = old_rep if not isinstance(old_rep, dict) else old_rep.get("value", 50)
            delta = cur_val - old_val
            if abs(delta) < 10:
                continue
            fname = state.get("display_names", {}).get(fid, fid)
            ripples.append({
                "current_event": f"{fname}声望{'提升' if delta > 0 else '下降'}{abs(delta)}点",
                "cause_turn": self.turn_number,
                "cause_action": f"本回合的行动影响了{fname}",
                "chain": [f"声望变化{delta:+d}"],
                "impact_type": "positive" if delta > 0 else "negative",
            })

        state["choice_ripples"] = ripples[:3]

    # ================================================
    #  ADVENTURE LOG AI POLISH
    # ================================================

    async def _maybe_ai_polish_chapters(self):
        """后台任务：对标记了 needs_ai_summary 的 chapter 条目进行 AI 润色。"""
        if not self.ai_provider:
            return
        log = self.current_state.get("adventure_log", [])
        for entry in log:
            if entry.get("type") != "chapter" or not entry.get("needs_ai_summary"):
                continue
            raw_summary = entry.get("summary", "")
            if not raw_summary:
                entry.pop("needs_ai_summary", None)
                continue
            try:
                messages = [{"role": "user", "content":
                    f"将以下游戏冒险日志摘要润色为一段流畅的50-80字叙事（第三人称），保留关键信息：\n\n{raw_summary}"}]
                polished = await self.ai_provider.generate(
                    messages,
                    system="你是游戏日志润色助手。输出简洁叙事摘要，不要JSON或标记。",
                    max_tokens=256,
                )
                polished = polished.strip()
                if polished and len(polished) > 10:
                    async with self._state_lock:
                        entry["summary"] = polished
                        entry["events"] = [{"type": "chapter_summary", "text": polished}]
            except Exception as e:
                logger.debug("chapter AI润色失败: %s", e)
            entry.pop("needs_ai_summary", None)

    # ================================================
    #  NPC OFFSCREEN & DYNAMIC NPC & RELATIONSHIP NETWORK
    # ================================================

    def _inject_offscreen_info(self, sim_results: list):
        """Record significant offscreen NPC actions into information_network.

        This lets NPC-to-NPC info propagation carry offscreen events,
        so in-scene NPCs may reference what happened elsewhere.
        """
        network = self.current_state.setdefault("information_network", [])
        for item in sim_results:
            npc_id = item.get("npc_id", "")
            action = item.get("action", "")
            if not npc_id or not action or len(action) < 5:
                continue
            has_effects = bool(item.get("world_effects")) or bool(item.get("npc_rel_updates"))
            gp = item.get("goal_progress")
            goal_significant = gp is not None and (int(gp) if isinstance(gp, (int, float, str)) else 0) >= 80
            if not has_effects and not goal_significant:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            info_id = f"offscreen_{self.turn_number}_{npc_id}"
            network.append({
                "id": info_id,
                "origin_turn": self.turn_number,
                "fact": f"{npc_name}在别处：{action[:60]}",
                "known_by": [npc_id],
                "spread_chance": 0.4,
                "distortion": 0,
                "max_spread": 4,
                "tags": ["offscreen", "npc_action"],
            })
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    def _record_npc_offscreen(self, update: dict):
        """Record an offscreen NPC action into the offscreen log.
        Also update the NPC's current location if provided."""
        npc_id = update.get("npc_id", "")
        if not npc_id:
            return
        log = self.current_state.setdefault("npc_offscreen_log", {})
        npc_log = log.setdefault(npc_id, [])
        npc_log.append({
            "turn": self.turn_number,
            "time": self.current_state.get("game_time", ""),
            "action": update.get("action", ""),
            "location": update.get("location", ""),
            "mood": update.get("mood", ""),
        })
        # Keep per-NPC log bounded
        if len(npc_log) > 20:
            log[npc_id] = npc_log[-15:]
        # Update NPC's current location in state so dialogue button reflects it
        new_loc = update.get("location", "")
        if new_loc:
            npc_state = self.current_state.get("npcs", {}).get(npc_id)
            if isinstance(npc_state, dict):
                npc_state.pop("_derived_location", None)
                npc_state["current_location"] = new_loc
        mood = update.get("mood", "")
        if mood:
            npc_state = self.current_state.get("npcs", {}).get(npc_id)
            if isinstance(npc_state, dict):
                npc_state["current_mood"] = mood
        # Sync to lorebook
        self._sync_offscreen_to_lorebook(npc_id, log.get(npc_id, []))

    def _sync_offscreen_to_lorebook(self, npc_id: str, npc_log: list[dict]):
        """Create/update a dynamic lorebook entry summarising an NPC's recent offscreen activity."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb or not npc_log:
            return
        npc_name = self._get_npc_display_name(npc_id)
        display_names = self.current_state.get("display_names", {})
        recent = npc_log[-3:]
        lines = []
        for r in recent:
            loc = display_names.get(r.get("location", ""), r.get("location", ""))
            action = r.get("action", "")
            if action:
                lines.append(f"- {action}（在{loc}）" if loc else f"- {action}")
        if not lines:
            return
        content = f"{npc_name}的近期离场动态:\n" + "\n".join(lines)
        entry_id = f"_offscreen_{npc_id}"
        existing = any(e.id == entry_id for e in lb.entries)
        if existing:
            lb.update_entry(entry_id, content)
        else:
            lb.add_entries([{
                "id": entry_id,
                "keys": [npc_name, npc_id],
                "content": content,
                "priority": 60,
                "position": "after_world",
                "entry_type": "npc_offscreen",
            }])


    # 关系网上限 — 超过后按优先级淘汰动态 NPC 间的旧条目


    async def _retry_ai_call(self, messages: list, system: str, parser, label: str = "", stage: str = ""):
        """O-5: 通用 AI 调用 + 单次重试辅助方法。"""
        kwargs = self._stage_kwargs(stage) if stage else {}
        try:
            raw = await self.ai_provider.generate(messages, system=system, **kwargs)
            return parser(raw)
        except Exception as e:
            logging.getLogger(__name__).debug("%s failed (attempt 1): %s", label, e)
            try:
                await asyncio.sleep(1)
                raw = await self.ai_provider.generate(messages, system=system, **kwargs)
                return parser(raw)
            except Exception as e2:
                logging.getLogger(__name__).debug("%s failed (attempt 2): %s", label, e2)
                return None

    async def _run_offscreen_simulation(self):
        """Asynchronously simulate off-screen NPC actions.

        Called at the end of each turn. Non-blocking — errors are logged and swallowed.
        Active NPCs (interacted in last 5 turns) get individual simulation.
        Inactive NPCs get batch simulation.
        """
        logger = logging.getLogger(__name__)

        if not self.ai_provider:
            return

        player_loc = self.current_state.get("player", {}).get("location", "")
        game_time = self.current_state.get("game_time", "")
        world_bg = self.script.get("world_background", "")[:200]
        npcs_state = self.current_state.get("npcs", {})
        offscreen_log = self.current_state.get("npc_offscreen_log", {})

        # Find NPCs not at the player's location (cooldown: skip if simulated within 2h game time)
        offscreen_npcs = []
        current_time_parsed = None
        if game_time:
            try:
                current_time_parsed = parse_time(game_time)
            except Exception:
                pass

        for npc_id, npc_data in npcs_state.items():
            if not isinstance(npc_data, dict):
                continue
            npc_loc = self._get_npc_location(npc_id)
            if not npc_loc or not player_loc:
                continue
            if self._locations_match(player_loc, npc_loc):
                continue  # NPC is with the player
            logs = offscreen_log.get(npc_id, [])
            if logs:
                last_time = logs[-1].get("time", "")
                if last_time and current_time_parsed:
                    try:
                        last_parsed = parse_time(last_time)
                        if last_parsed and current_time_parsed:
                            diff = current_time_parsed - last_parsed
                            if diff.total_seconds() < 7200:  # 2小时内跳过
                                continue
                    except Exception:
                        pass
                elif logs[-1].get("turn", 0) >= self.turn_number - 2:
                    continue  # 时间解析失败时回退到回合数判断
            offscreen_npcs.append((npc_id, npc_data, npc_loc))

        if not offscreen_npcs:
            return

        # Classify: active (interacted in last 5 turns) vs inactive
        active_npcs = []
        inactive_npcs = []
        for npc_id, npc_data, npc_loc in offscreen_npcs:
            logs = offscreen_log.get(npc_id, [])
            recent_turns = [l for l in logs if l.get("turn", 0) >= self.turn_number - 5]
            # Also check relationship changes as sign of activity
            if len(recent_turns) >= 2:
                active_npcs.append((npc_id, npc_data, npc_loc))
            else:
                inactive_npcs.append((npc_id, npc_data, npc_loc))

        try:
            # 统一批量模拟：活跃NPC包含上次行动信息以获得更连贯的模拟
            all_npcs_to_sim = []
            completed_goals = {(g["id"] if isinstance(g, dict) else g) for g in self.current_state.get("completed_npc_goals", [])}
            for npc_id, npc_data, npc_loc in active_npcs:
                npc_name = npc_data.get("name", npc_id)
                npc_bio = self._npc_by_id.get(npc_id, {}).get("bio", "")[:50]
                npc_personality = npc_data.get("personality", "")[:15]
                last_log = offscreen_log.get(npc_id, [])
                recent_action = last_log[-1]["action"] if last_log else ""
                line = f"- {npc_name}(id:{npc_id}): 位于{npc_loc}"
                if npc_personality:
                    line += f", 性格:{npc_personality}"
                if npc_bio:
                    line += f", {npc_bio}"
                if recent_action:
                    line += f", 上次行动: {recent_action}"
                npc_goals = self._npc_by_id.get(npc_id, {}).get("goals", [])
                active_goals = [g for g in npc_goals if f"{npc_id}:{g.get('id','')}" not in completed_goals]
                if active_goals:
                    hints = [f"{g.get('description','')}" + (f"({g.get('progress_hint','')})" if g.get('progress_hint') else "") for g in active_goals[:2]]
                    line += f", 目标: {'; '.join(hints)}"
                all_npcs_to_sim.append((npc_id, line))

            for npc_id, npc_data, npc_loc in inactive_npcs:
                npc_name = npc_data.get("name", npc_id)
                npc_personality = npc_data.get("personality", "")[:15]
                line = f"- {npc_name}(id:{npc_id}): 位于{npc_loc}"
                if npc_personality:
                    line += f", 性格:{npc_personality}"
                npc_goals = self._npc_by_id.get(npc_id, {}).get("goals", [])
                active_goals = [g for g in npc_goals if f"{npc_id}:{g.get('id','')}" not in completed_goals]
                if active_goals:
                    hints = [g.get("description", "") for g in active_goals[:2]]
                    line += f", 目标: {'; '.join(hints)}"
                all_npcs_to_sim.append((npc_id, line))

            if all_npcs_to_sim:
                npc_lines = [line for _, line in all_npcs_to_sim]

                # 注入 blueprint 活跃剧情线上下文
                bp_context = ""
                bp = self.current_state.get("plot_blueprint", {})
                bp_threads = bp.get("plot_threads", [])
                if bp_threads:
                    bp_lines = []
                    for thread in bp_threads[:5]:
                        for stage in thread.get("stages", []):
                            status = stage.get("status", "pending")
                            if status == "active":
                                npcs_str = ", ".join(thread.get("related_npcs", []))
                                bp_lines.append(f"- {thread.get('name', thread.get('id',''))}: {stage.get('description', '')}（相关: {npcs_str}）")
                                break
                            elif status == "pending":
                                break
                    if bp_lines:
                        bp_context = "\n活跃剧情线（NPC行动须与之一致）:\n" + "\n".join(bp_lines) + "\n"

                weather = self.current_state.get("current_weather", "")
                atmo = self.current_state.get("time_atmosphere", {})
                env_line = ""
                if weather or atmo:
                    parts = []
                    if weather:
                        parts.append(f"天气:{weather}")
                    period = atmo.get("period_label", "")
                    if period:
                        parts.append(f"时段:{period}")
                    if parts:
                        env_line = f"\n环境: {', '.join(parts)}（NPC行为须受环境影响：恶劣天气应躲避、夜间多数NPC应休息）"

                batch_prompt = f"""简述以下NPC此刻在做什么（每人一句话），返回JSON列表。
时间: {game_time} | 背景: {world_bg}{env_line}
{bp_context}NPC列表:
{chr(10).join(npc_lines)}
返回格式: [{{"npc_id":"xxx","action":"..","location":"..","goal_progress":0-100,"world_effects":[{{"target":"state.path","op":"add","value":1}}]}}, ...]
goal_progress: 如果NPC有目标，估算完成百分比(0-100)。无目标则省略。
world_effects: 如果NPC行动对世界状态有显著影响，列出状态变更。通常省略。
如果有NPC之间发生了新的认识或接触，追加 npc_rel_updates 字段：
{{"npc_id":"xxx","action":"..","location":"..","npc_rel_updates":[{{"target":"其他npc_id","known":true,"met":true}}]}}
如果NPC之间的互动改变了彼此关系（合作、冲突、帮助等），追加 rel_change 字段：
{{"npc_id":"xxx","action":"..","rel_change":[{{"target":"另一npc_id","delta":10,"reason":"原因"}}]}}
delta范围-20~20，正=关系改善，负=关系恶化。通常省略。"""

                data_list = await self._retry_ai_call(
                    [{"role": "user", "content": batch_prompt}],
                    "你是NPC行动模拟器。只返回JSON列表，不要其他内容。",
                    self._parse_simple_json_list,
                    label="Batch NPC sim",
                    stage="knowledge_graph",
                )
                if data_list:
                    async with self._state_lock:
                        rel_updates = []
                        goal_progress = self.current_state.setdefault("npc_goal_progress", {})
                        for item in data_list:
                            npc_id = item.get("npc_id", "")
                            if npc_id:
                                self._record_npc_offscreen({
                                    "npc_id": npc_id,
                                    "action": item.get("action", ""),
                                    "location": item.get("location", ""),
                                    "mood": item.get("mood", ""),
                                })
                                # Goal progress tracking
                                gp = item.get("goal_progress")
                                if gp is not None:
                                    try:
                                        gp = int(gp)
                                    except (ValueError, TypeError):
                                        gp = None
                                if gp is not None:
                                    prev_gp = goal_progress.get(npc_id, {}).get("progress", 0)
                                    goal_progress[npc_id] = {
                                        "progress": min(100, max(0, gp)),
                                        "last_action": item.get("action", ""),
                                        "turn": self.turn_number,
                                    }
                                    if gp >= 100 and prev_gp < 100:
                                        npc_name = self.current_state.get("npcs", {}).get(npc_id, {}).get("name", npc_id)
                                        self._record_narrative_callback(
                                            f"{npc_name}已完成了自己的目标，这可能对世界产生深远影响",
                                            [f"npc_goal_complete:{npc_id}"], "high",
                                        )
                                # World effects
                                for we in item.get("world_effects", []):
                                    target = we.get("target", "")
                                    if not target:
                                        continue
                                    val = we.get("value", 0)
                                    op = we.get("op", "add")
                                    if op in ("add", "subtract") and isinstance(val, (int, float)) and abs(val) > 30:
                                        val = 30 if val > 0 else -30
                                    elif op == "set" and isinstance(val, (int, float)):
                                        try:
                                            old = self.state_manager._get_value(self.current_state, target)
                                            if isinstance(old, (int, float)) and abs(val - old) > 30:
                                                val = old + (30 if val > old else -30)
                                        except Exception:
                                            pass
                                    try:
                                        self.state_manager.apply_changes(
                                            self.current_state,
                                            [{"target": target, "value": val,
                                              "op": op, "reason": f"NPC {npc_id} 行动影响"}],
                                            inplace=True,
                                        )
                                    except Exception:
                                        pass
                                for ru in item.get("npc_rel_updates", []):
                                    target = ru.get("target", "")
                                    if target:
                                        rel_updates.append({
                                            "a": npc_id, "b": target,
                                            "type": ru.get("type", "中立"),
                                            "description": ru.get("description", ""),
                                            "scope": "global",
                                            "known": ru.get("known"),
                                            "met": ru.get("met"),
                                        })
                                for rc in item.get("rel_change", []):
                                    if not isinstance(rc, dict):
                                        continue
                                    rc_target = rc.get("target", "")
                                    rc_delta = rc.get("delta", 0)
                                    if not rc_target or not isinstance(rc_delta, (int, float)):
                                        continue
                                    rc_delta = max(-20, min(20, int(rc_delta)))
                                    rc_key = self._normalize_rel_key(npc_id, rc_target)
                                    rels_g = self.current_state.setdefault("npc_relationships_global", {})
                                    rc_rel = rels_g.setdefault(rc_key, {"a": npc_id, "b": rc_target, "type": "中立", "intensity": 50})
                                    new_int = max(0, min(100, rc_rel.get("intensity", 50) + rc_delta))
                                    rc_rel["intensity"] = new_int
                                    if new_int < 20:
                                        rc_rel["type"] = "敌对"
                                    elif new_int > 80:
                                        rc_rel["type"] = "友好"
                                    elif new_int < 35:
                                        rc_rel["type"] = "紧张"
                                    elif new_int > 65:
                                        rc_rel["type"] = "亲近"
                                    else:
                                        rc_rel["type"] = "中立"
                        if rel_updates:
                            self._apply_npc_relationship_updates(rel_updates)
                        self._inject_offscreen_info(data_list)
                        self._sync_npc_interaction_memory()

        except Exception as e:
            logger.warning("Offscreen NPC simulation error: %s", e)

    @staticmethod
    def _parse_simple_json(text: str) -> dict | None:
        """Extract a JSON object from text that may contain extra content."""
        # Try direct parse
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        # Find the first '{' and try to find its matching '}' (supports nesting)
        start = text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        i = start
        while i < len(text):
            c = text[i]
            if in_string:
                if c == '\\':
                    i += 2  # skip escaped character
                    continue
                if c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            return None
            i += 1
        return None

    # ── MetaEventBus handlers ───────────────────────────────────────

    # --- Wrappers for migrated existing background tasks ---


    # --- New system-level handlers ---

