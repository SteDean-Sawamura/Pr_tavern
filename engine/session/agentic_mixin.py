"""Agentic mixin -- dual-agent loop mode (foreground narration + background settlement).

Alternate to the fixed multi-stage pipeline in PipelineMixin. Selected by the
PIPELINE_MODE config flag; both paths produce a `parsed` dict compatible with
ResponseParser._empty_result() so the downstream state application is unchanged.

Foreground agent : lean context + retrieval tools -> free-text narrative
Background agent : narrative -> state settlement tool calls -> parsed dict
"""

from __future__ import annotations

import asyncio
import base64
import copy
from difflib import SequenceMatcher
from functools import partial
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field, replace
from types import MethodType
from uuid import uuid4

from typing import TYPE_CHECKING, Callable

from ai.base import strip_think_tags
from engine.conditions import CONDITION_RULE_SECTIONS, condition_error
from engine.tool_schemas import CONSOLIDATED_SETTLEMENT_TOOLS, FOREGROUND_TOOLS_SCHEMA, _unified_tools

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)
_MODEL_WAIT_HEARTBEAT_SECONDS = 10
_PLAYER_SPATIAL_FEEDBACK_LABEL = "玩家空间候选值（未提交，不含尚未结算的事件效果，非主角知情）: "
_NPC_CONTACT_FEEDBACK_LABEL = "NPC接触候选（未提交，非角色知情）: "
_EVENT_INSTRUCTION_FEEDBACK_LABEL = "定时事件叙事指令: "
_PRE_PLAN_READ_TOOLS = frozenset({
    "recall_history", "query_lorebook", "query_npc_history",
    "check_inventory", "get_npc_attitude", "peek_upcoming_events",
    "traverse_graph", "read_director_notes",
})
_PRIVATE_PLAN_SUMMARY = "正在核对资料并推进当前行动"
_PRIVATE_PRE_PLAN_TOOLS = frozenset({
    "query_lorebook", "get_npc_attitude", "peek_upcoming_events", "traverse_graph", "read_director_notes",
})


def _tool_result_failed(result) -> bool:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result.startswith(("错误:", "错误："))
    return isinstance(result, dict) and ("error" in result or result.get("success") is False)


def _clean_narrative_text(text: str, ctx: dict) -> str:
    reflection = re.search(r'<reflect>(.*?)</reflect>', text, re.DOTALL)
    if reflection:
        ctx["_reflection"] = reflection.group(1).strip()
        logger.info("[反思] %s", ctx["_reflection"][:200])
    return strip_think_tags(re.sub(r'<reflect>.*?</reflect>', '', text, flags=re.DOTALL)).strip()


def _candidate_value_feedback(category: str, values: dict, missing=()) -> str:
    label = "变量" if category == "variables" else "世界属性"
    content = json.dumps(values, ensure_ascii=False)
    if missing:
        content += "；未设置: " + json.dumps(missing, ensure_ascii=False)
    if len(content) > 4000:
        content = ("候选值全文过长已省略，不是空值；用 consult_rules(category=\"" + category
                   + "\", include_candidates=true) 查询，携带条目 snapshot 按 rule_index 和 next_offset 完整续读；"
                     "每次更新或撤销后从 offset=0 重查，不要重复执行已接收的更新")
    return f" | {label}候选值（未提交，不含尚未结算的事件效果）: {content}"


def _choice_preview_changes(before: dict, after: dict, path: tuple = ()) -> list[dict]:
    changes = []
    for key in sorted(before.keys() | after.keys()):
        current_path = (*path, key)
        if key not in before:
            changes.append({"operation": "add", "path": list(current_path),
                            "after": copy.deepcopy(after[key])})
        elif key not in after:
            changes.append({"operation": "remove", "path": list(current_path),
                            "before": copy.deepcopy(before[key])})
        elif isinstance(before[key], dict) and isinstance(after[key], dict):
            changes.extend(_choice_preview_changes(before[key], after[key], current_path))
        elif json.dumps(before[key], sort_keys=True) != json.dumps(after[key], sort_keys=True):
            changes.append({"operation": "replace", "path": list(current_path),
                            "before": copy.deepcopy(before[key]), "after": copy.deepcopy(after[key])})
    return changes


def _json_result_section(result: dict, result_path: list, offset: int, summary_keys: tuple = ()) -> dict:
    selected = result
    for part in result_path:
        if isinstance(selected, dict) and isinstance(part, str) and part in selected:
            selected = selected[part]
        elif isinstance(selected, list) and type(part) is int and 0 <= part < len(selected):
            selected = selected[part]
        else:
            raise ValueError("result_path 不存在或键/索引类型不匹配")

    def describe(value):
        value_type = {dict: "object", list: "array", str: "string", bool: "boolean",
                      int: "number", float: "number", type(None): "null"}[type(value)]
        description = {"value_type": value_type}
        if isinstance(value, (dict, list, str)):
            description["size"] = len(value)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        description["complete"] = len(encoded) <= 400
        if description["complete"]:
            description["value"] = value
        elif isinstance(value, dict) and summary_keys:
            description["summary"] = {key: value[key] for key in summary_keys if key in value}
        return description

    description = describe(selected)
    page = {key: value for key, value in description.items() if key not in {"value", "complete"}}
    page.update(content_format="result_section", offset=offset, next_offset=None, complete=False)
    if isinstance(selected, (dict, list)):
        keys = sorted(selected) if isinstance(selected, dict) else range(len(selected))
        if offset > len(keys):
            raise ValueError("offset 超出本字段的成员数量")
        entries = []
        page_size = 0
        for position in range(offset, len(keys)):
            key = keys[position]
            entry = {"key": key, **describe(selected[key])}
            entry_size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            if entries and page_size + entry_size > 2000:
                break
            entries.append(entry)
            page_size += entry_size
        next_offset = offset + len(entries)
        page.update(entries=entries, next_offset=next_offset if next_offset < len(keys) else None,
                    complete=offset == 0 and next_offset == len(keys) and all(entry["complete"] for entry in entries))
    elif isinstance(selected, str):
        if offset > len(selected):
            raise ValueError("offset 超出本字段的字符长度")
        page.update(content=selected[offset:offset + 2000],
                    next_offset=offset + 2000 if offset + 2000 < len(selected) else None,
                    complete=offset == 0 and len(selected) <= 2000)
    else:
        if offset:
            raise ValueError("标量字段的 offset 必须为0")
        page.update(value=selected, complete=True)
    return page


def _bounded_event_instruction_feedback(feedback: dict, key: str, limit: int) -> str:
    encoded = json.dumps(feedback, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    preview = {field: value for field, value in feedback.items() if field != key}
    instructions = feedback.get(key)
    if isinstance(instructions, list) and instructions:
        preview.update(instructions_omitted=True, instruction_count=len(instructions), instruction_refs=[
            {field: instruction[field] for field in ("event_id", "fire_time") if field in instruction}
            for instruction in instructions
        ])
    elif instructions == []:
        preview[key] = []
    preview["read_hint"] = (
        "正文省略，不算已读或撤回；用 consult_rules 在 one_time_events/cyclic_events 按 event_id 查询，"
        "取 category、rule_index、snapshot，以 result_path=[definition,effects,inject_prompt]、offset=0 读字段，"
        "按 next_offset 续读；快照失效丢弃旧片段重查。按各 fire_time 核对，非已发生或角色知情。"
    )
    encoded = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    preview = {field: preview[field] for field in (
        "through", "provisional", "instructions_omitted", "instruction_count", "read_hint", "error_code",
    ) if field in preview}
    preview["metadata_omitted"] = True
    preview["read_hint"] = (
        "指令及坐标省略，非撤回；用 consult_rules 重查 one_time_events/cyclic_events，按候选时间核对声明；"
        "取新 rule_index/snapshot，以 result_path=[definition,effects,inject_prompt]、offset=0 重读，"
        "按 next_offset 续读；失效重查，非已发生或角色知情。"
    )
    if instructions == []:
        preview[key] = []
    if _tool_result_failed(feedback):
        preview.update(error="提交反馈详情省略，不能视为检查通过。", state_update_accepted=False,
                       retry_hint="先重读指令并核对声明，再重传本次未接收参数；不要重复先前已接收更新。")
    return json.dumps(preview, ensure_ascii=False, separators=(",", ":"))


def _standalone_event_instruction_feedback(content: str) -> str | None:
    if _EVENT_INSTRUCTION_FEEDBACK_LABEL not in content:
        return None
    try:
        feedback, _ = json.JSONDecoder().raw_decode(content.split(_EVENT_INSTRUCTION_FEEDBACK_LABEL, 1)[1])
    except ValueError:
        return None
    if not isinstance(feedback, dict) or ("instructions" not in feedback and not feedback.get("instructions_omitted")):
        return None
    return ("[定时事件指令反馈] " + _EVENT_INSTRUCTION_FEEDBACK_LABEL
            + _bounded_event_instruction_feedback(feedback, "instructions", 6000))


def _historical_event_instruction_feedback(content: str, name: str) -> str | None:
    if name == "finalize_turn" and _tool_result_failed(content):
        prefix, payload, key = "", content, "event_instructions"
    elif (name in ("update_state", "undo_my_last_action", "write_narrative")
          and not _tool_result_failed(content) and _EVENT_INSTRUCTION_FEEDBACK_LABEL in content):
        prefix, payload = content.split(_EVENT_INSTRUCTION_FEEDBACK_LABEL, 1)
        prefix += _EVENT_INSTRUCTION_FEEDBACK_LABEL
        key = "instructions"
    else:
        return None
    try:
        feedback, end = json.JSONDecoder().raw_decode(payload)
    except ValueError:
        return None
    if not isinstance(feedback, dict) or (key not in feedback and not feedback.get("instructions_omitted")):
        return None
    historical = {field: value for field, value in feedback.items() if field not in (
        key, "instructions_omitted", "instruction_count", "instruction_refs", "read_hint",
    )}
    if key == "instructions":
        historical.pop("hint", None)
    historical.update(instructions_superseded=True, history_note=(
        "历史指令已被后续反馈取代，仅参考最新指令反馈，不要恢复旧候选；"
        "本条参数接收状态及其他失败详情仍按原回执，不代表提交成功。"
    ))
    return prefix + json.dumps(historical, ensure_ascii=False, separators=(",", ":")) + payload[end:]


def _bounded_tool_content(content: str, name: str, limit: int) -> str:
    if name == "finalize_turn":
        try:
            feedback = json.loads(content)
        except ValueError:
            feedback = None
        if isinstance(feedback, dict) and ("event_instructions" in feedback or feedback.get("instructions_omitted")):
            return _bounded_event_instruction_feedback(feedback, "event_instructions", limit)
    if name in ("update_state", "undo_my_last_action", "write_narrative") and _EVENT_INSTRUCTION_FEEDBACK_LABEL in content:
        prefix, instruction_content = content.split(_EVENT_INSTRUCTION_FEEDBACK_LABEL, 1)
        try:
            feedback, end = json.JSONDecoder().raw_decode(instruction_content)
        except ValueError:
            feedback = None
        if isinstance(feedback, dict):
            reserve = min(len(prefix) + len(instruction_content[end:]), 600, limit // 3)
            encoded = _EVENT_INSTRUCTION_FEEDBACK_LABEL + _bounded_event_instruction_feedback(
                feedback, "instructions", limit - reserve - len(_EVENT_INSTRUCTION_FEEDBACK_LABEL) - 3,
            )
            remainder = prefix + instruction_content[end:]
            available = limit - len(encoded) - 3
            if len(remainder) <= available:
                return prefix + encoded + instruction_content[end:]
            return _bounded_tool_content(remainder, name, available) + " | " + encoded
    if name in ("update_state", "undo_my_last_action", "write_narrative") and _NPC_CONTACT_FEEDBACK_LABEL in content:
        prefix, contact_content = content.split(_NPC_CONTACT_FEEDBACK_LABEL, 1)
        try:
            feedback, end = json.JSONDecoder().raw_decode(contact_content)
        except ValueError:
            feedback = None
        if isinstance(feedback, dict):
            note = "；候选不等于已接触；详情省略时用 consult_rules(question=角色ID+接触, include_candidates=true) 重新查询。"
            encoded = _NPC_CONTACT_FEEDBACK_LABEL + json.dumps(feedback, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) + len(note) > limit - 160:
                encoded = _NPC_CONTACT_FEEDBACK_LABEL + json.dumps({
                    "through": feedback.get("through"), "feedback_omitted": True,
                }, ensure_ascii=False, separators=(",", ":"))
            available = limit - len(encoded) - len(note) - 1
            remainder = prefix + contact_content[end:]
            if len(remainder) <= available:
                return encoded + note + "\n" + remainder
            if available >= 400:
                return encoded + note + "\n" + _bounded_tool_content(remainder, name, available)
            return encoded + note
    if name in ("update_state", "undo_my_last_action", "write_narrative") and _PLAYER_SPATIAL_FEEDBACK_LABEL in content:
        prefix, spatial_content = content.split(_PLAYER_SPATIAL_FEEDBACK_LABEL, 1)
        try:
            feedback, end = json.JSONDecoder().raw_decode(spatial_content)
        except ValueError:
            feedback = None
        if isinstance(feedback, dict):
            remainder = prefix + spatial_content[end:]
            for omit_reason in (False, True):
                if omit_reason and "reason" in feedback:
                    feedback.update({"reason": "", "reason_truncated": True})
                encoded = _PLAYER_SPATIAL_FEEDBACK_LABEL + json.dumps(feedback, ensure_ascii=False, separators=(",", ":"))
                available = limit - len(encoded) - 1
                if available < 160:
                    continue
                if len(remainder) <= available:
                    return encoded + "\n" + remainder
                if available >= 400:
                    return encoded + "\n" + _bounded_tool_content(remainder, name, available)
                return encoded + "\n[其余工具反馈已省略；按 rule_query 核对完整声明，不能把候选可通行当作已抵达]"
            return (_PLAYER_SPATIAL_FEEDBACK_LABEL + '{"feedback_omitted":true}'
                    + "；空间候选详情因预算不足已省略，不能据此判断通行成功；须重新核对候选状态和地点声明。")
    if name == "query_lorebook":
        try:
            result = json.loads(content)
        except ValueError:
            result = None
        if isinstance(result, dict) and result.get("restart_required"):
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= limit:
                return encoded
            return json.dumps({
                "error_code": result.get("error_code"), "restart_required": True, "metadata_omitted": True,
                "error": "世界书快照缺失或失效；丢弃旧片段，按原查询从 page=0/offset=0 重查，不传 snapshot。",
            }, ensure_ascii=False, separators=(",", ":"))
        if isinstance(result, dict) and isinstance(result.get("entries"), list) and not _tool_result_failed(result):
            preview = {key: result[key] for key in (
                "total_matches", "next_page", "approximate", "projection", "snapshot",
            ) if key in result}
            preview["entries"] = [
                {key: entry[key] for key in (
                    "id", "visibility", "offset", "next_offset", "total_length", "truncated", "related_entries", "snapshot",
                ) if key in entry}
                for entry in result["entries"] if isinstance(entry, dict)
            ]
            preview["content_omitted"] = True
            preview["notice"] = (
                "正文已省略，不算已读；按条目 id、offset 和条目 snapshot 重读当前片段，不要直接跳到 next_offset。"
                "需要完整阅读时从 offset=0 开始，再按新结果 next_offset 续读；定位片段不等于全文，失效按 restart 重查。"
                "资料仅供推演，非已发生或角色已知。"
            )
            encoded = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= limit:
                return encoded
            preview = {
                "content_omitted": True, "metadata_omitted": True,
                "notice": "正文和元数据已省略，不表示词条未返回；按原查询从 page=0/offset=0 重查，不传 snapshot；缩小范围后携带新快照续读。"
                          "资料仅供推演，非已发生或角色已知。",
            }
            if result.get("projection"):
                preview["projection"] = {"provisional": True}
            return json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
    if name == "invoke_character":
        try:
            result = json.loads(content)
        except ValueError:
            result = None
        if isinstance(result, dict) and result.get("mode") == "advisory":
            preview = {
                "mode": "advisory", "response_omitted": True,
                "notice": "角色建议及上下文已省略，不是已发生事件或互动许可；候选未提交。"
                          "用原 npc_id、situation、include_candidates 重新调用；更新、撤销、切换分支后须重新调用。",
            }
            if result.get("projection"):
                preview["projection"] = {"provisional": True}
            return json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
    if name == "consult_rules":
        try:
            result = json.loads(content)
        except ValueError:
            result = None
        if isinstance(result, dict) and not result.get("category") and isinstance(result.get("answers"), list):
            preview = {
                "answers_omitted": True,
                "notice": "结果省略；用原 question 和 include_candidates 重新查询。更新、撤销、切换分支后重查；候选未提交、非角色知情。",
            }
            if result.get("projection"):
                preview["projection"] = {"provisional": True}
            return json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
        if isinstance(result, dict) and result.get("category") and isinstance(result.get("entries"), list):
            preview = {key: result[key] for key in (
                "category", "source", "game_time", "offset", "next_offset", "total", "projection", "snapshot", "view", "node_status",
            ) if key in result}
            preview["entries"] = [
                {key: entry[key] for key in ("rule_index", "offset", "next_offset", "total_chars", "snapshot", "read") if key in entry}
                for entry in result["entries"] if isinstance(entry, dict)
            ]
            preview["truncated"] = True
            preview["notice"] = (
                "正文省略；携带条目 snapshot、category、rule_index 从 offset=0 重读；"
                "保留 view；失效则按原 node_status 重查列表。非已触发或角色已知。"
            )
            if result.get("projection"):
                preview["notice"] = (
                    "正文省略；保留 include_candidates=true 和条目 snapshot，用 category、rule_index 从 offset=0 重读；"
                    "保留 view；候选值未提交、非角色知情；失效时按原 node_status 重查列表。"
                )
                preview["notice"] += (
                    "仅含已接收时间内定时部分，不含事件掷骰或完整生命周期。"
                    if result["projection"].get("excludes_unsettled_events") is False else "不含未结算效果。"
                )
            if any(entry.get("read") for entry in preview["entries"]):
                preview["notice"] += "字段正文也已省略；按条目 read 保留 result_path，从 offset=0 重读该字段，不表示已知全部规则。"
            serialized = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) <= limit:
                return serialized
            return json.dumps({
                "truncated": True, "metadata_omitted": True,
                "notice": "正文与坐标省略；按原查询类别、view、node_status 和候选视图从 offset=0 重查列表，不传 rule_index/snapshot，再携带新快照重读。非已发生或角色已知。",
            }, ensure_ascii=False, separators=(",", ":"))
    if name == "preview_choice_outcome":
        try:
            result = json.loads(content)
        except ValueError:
            result = None
        if isinstance(result, dict) and result.get("mode") == "declared_choice":
            preview = {key: result[key] for key in (
                "mode", "node_id", "choice_id", "available", "reason", "condition_error",
                "snapshot", "total_chars", "read", "error", "error_code", "restart_required", "restart",
            ) if key in result}
            if isinstance(result.get("projection"), dict):
                preview["projection"] = {key: result["projection"][key] for key in (
                    "state_view", "effects_applied", "scope", "persisted", "excludes_unsettled_events",
                    "scheduled_through", "excludes_lifecycle", "excludes_event_dice",
                    "staged_event_dice_ids", "excludes_unstaged_event_dice",
                ) if key in result["projection"]}
            preview.update(content_omitted=True, changes_omitted=True,
                           state_change_count=len(result["state_changes"]) if "state_changes" in result else result.get("state_change_count", 0),
                           lore_change_count=len(result["lore_changes"]) if "lore_changes" in result else result.get("lore_change_count", 0), notice=(
                "正文省略，不表示无效果或已保存；按 read 从 offset=0 重读。"
                "可加 result_path=[] 查目录，再按键/索引读字段；无路径时须拼齐 JSON。"
                "失效则丢弃旧片段按 restart 重查；consult_rules 只读声明。非玩家已知。"
            ))
            encoded = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= limit and not result.get("metadata_omitted") and (preview.get("read") or preview.get("restart")):
                return encoded
            return json.dumps({
                "mode": "declared_choice", "content_omitted": True, "metadata_omitted": True,
                "changes_omitted": True,
                "notice": "正文和坐标省略；丢弃旧片段，按原节点、选项及候选视图重新初查，不传 offset/snapshot/result_path。不表示无效果、已保存或玩家已知。",
            }, ensure_ascii=False, separators=(",", ":"))
    if name == "read_director_notes":
        try:
            result = json.loads(content)
        except ValueError:
            result = None
        notes = result.get("notes") if isinstance(result, dict) else None
        if isinstance(notes, list) and notes:
            excerpt_size = min(2000, max(250, (limit - 2000) // len(notes) - 150))
            excerpts = []
            for note in notes:
                if not isinstance(note, dict):
                    continue
                text = str(note.get("content") or "")
                if len(text) > excerpt_size:
                    start = excerpt_size * 3 // 4
                    text = (text[:start] + f"…[正文截取，原长{len(text)}字]…"
                            + text[-(excerpt_size - start):])
                excerpts.append({**note, "content": text})
            preview = json.dumps({**result, "notes": excerpts, "truncated": True}, ensure_ascii=False)
            if len(preview) <= limit:
                return preview
    excerpt_size = max(200, limit - 150)
    start = excerpt_size * 3 // 4
    return (f"[工具结果过长，正文截取；原长{len(content)}字，请缩小范围重查]\n"
            + content[:start] + "\n…\n" + content[-(excerpt_size - start):])


def _bound_tool_messages(messages: list[dict], max_chars: int) -> list[dict]:
    sizes = [len(json.dumps(message, ensure_ascii=False)) for message in messages]
    total = sum(sizes)
    tool_names = {
        call.get("id"): call.get("function", {}).get("name", "")
        for message in messages if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    }
    for index, message in enumerate(messages):
        if total <= max_chars - 1000:
            break
        if (message.get("role") != "tool"
                or tool_names.get(message.get("tool_call_id")) == "submit_plan"):
            continue
        content = message.get("content") or ""
        limit = max(1000, min(20000, max_chars - 1000 - (total - sizes[index])))
        if sizes[index] <= limit:
            continue
        preview_limit = limit
        while True:
            preview = _bounded_tool_content(
                content, tool_names.get(message.get("tool_call_id"), ""), preview_limit,
            )
            bounded_message = {**message, "content": preview}
            new_size = len(json.dumps(bounded_message, ensure_ascii=False))
            if new_size <= limit or preview_limit <= 200:
                break
            preview_limit = max(200, preview_limit * limit // new_size - 100)
        messages[index] = bounded_message
        total += new_size - sizes[index]
        sizes[index] = new_size
    return messages


def _public_agent_trace(trace: dict) -> dict:
    rounds = trace.get("rounds", [])
    first_plan_step = next((step["step"] for step in rounds
                            if step.get("tool") == "submit_plan" and isinstance(step.get("step"), int)), None)
    legacy_private_plan = (trace.get("plan_visibility") is None and first_plan_step is not None
                           and any(step.get("tool") in _PRIVATE_PRE_PLAN_TOOLS
                                   and isinstance(step.get("step"), int) and step["step"] < first_plan_step
                                   for step in rounds))
    public = {**trace, "rounds": [
        {key: step[key] for key in ("step", "tool", "superseded", "duration_ms") if key in step}
        for step in trace.get("rounds", [])
    ]}
    if trace.get("plan_visibility") == "private" or legacy_private_plan:
        if public.get("plan"):
            public["plan"] = _PRIVATE_PLAN_SUMMARY
        public["plan_interactions"] = [
            {**interaction, "plan": _PRIVATE_PLAN_SUMMARY, "questions": []}
            for interaction in public.get("plan_interactions", [])
        ]
    for key in ("lore_preview_ids", "lore_candidate_ids"):
        if key in public:
            public[key.replace("_ids", "_count")] = len(public.pop(key))
    if "lore_retrievals" in public:
        public["lore_retrievals"] = [
            {"step": retrieval["step"], "hit_count": (
                len(retrieval["entry_ids"]) if "entry_ids" in retrieval
                else retrieval.get("hit_count", 0)
            )}
            for retrieval in public["lore_retrievals"]
        ]
    public.pop("reflection", None)
    return public


@dataclass
class _AgentResult:
    """Holder for async-generator _agent_loop's final return value."""
    text: str = ""
    records: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    model_timings_ms: list[int] = field(default_factory=list)
    model_first_output_ms: list[int | None] = field(default_factory=list)
    model_request_chars: list[int] = field(default_factory=list)
    model_token_usage: list[dict | None] = field(default_factory=list)
    model_request_breakdowns: list[dict] = field(default_factory=list)
    model_phases: list[str] = field(default_factory=list)
    plan_waits: int = 0
    plan_wait_ms: int = 0
    user_injections: list[dict] = field(default_factory=list)


class IncompleteAgentTurnError(RuntimeError):
    pass


class NarrativeSettlementError(IncompleteAgentTurnError):
    def __init__(self, feedback: str):
        self.feedback = feedback
        super().__init__("叙事与实际事件或时间结算不符，本轮未保存，请重试")


class PendingConsequenceDisclosureError(IncompleteAgentTurnError):
    pass


class SemanticReviewError(IncompleteAgentTurnError):
    def __init__(self):
        super().__init__("叙事语义审查连续未通过，本轮未保存，请重试")


class MapTravelSettlementError(IncompleteAgentTurnError):
    def __init__(self):
        super().__init__("地图旅行叙事与位置结算不符，本轮未保存，请重试")


class AgenticMixin:
    """Dual Agent Loop mode -- foreground narration + background settlement."""

    # ================================================================
    # Shared ReAct loop
    # ================================================================

    async def _agent_loop(
        self, messages: list[dict], system: str, tools: list[dict],
        dispatch, *, max_rounds: int = 7, label: str = "agent",
        stage_key: str = "narrative", max_tokens: int = 8192,
        result_holder: _AgentResult | None = None,
        interactive_plan: bool = False,
        plan_before_tools: bool = False,
        before_tool_calls=None,
        terminal_tool_required: bool = False,
        report_tool_start: bool = False,
        final_reply_after_tools: bool = False,
        after_plan_context: tuple[str, str] | Callable[[], tuple[str, str]] | None = None,
        on_plan_reply: Callable[[str], None] | None = None,
        on_tool_batch: Callable[[list[dict]], None] | None = None,
        suppress_plan_questions: bool = False,
        first_model_interval: dict | None = None,
    ):
        """ReAct loop as async generator. Yields intermediate events.

        Final text + tool records are written into *result_holder* (since
        Python async generators cannot ``return`` a value).

        dispatch(tool_name, args) -> str result fed back to the model.
        """
        holder = result_holder or _AgentResult()
        msgs = list(messages)
        records: list[dict] = []
        content = ""
        _narrative_text = ""  # 最终叙事：仅从明确终止点获取
        _continuation_prefix = ""  # #11: 截断续写时保存前一段文本
        pending_inject = None
        pending_interrupted_injections = list(getattr(self, "_inject_queue", ()) or ())
        msgs.extend({"role": "user", "content": message} for message in pending_interrupted_injections)
        pending_plan_record = None
        pending_plan_id = None
        terminal_repair_count = 0
        initial_max_rounds = max_rounds
        round_num = -1
        restrict_later_plans = after_plan_context is not None
        first_plan_seen = False
        full_tools = tools
        if plan_before_tools:
            tools = [tool for tool in tools if tool["function"]["name"] in _PRE_PLAN_READ_TOOLS
                     or tool["function"]["name"] == "submit_plan"]

        async def prepare_tool_calls():
            preparation = before_tool_calls()
            if not asyncio.iscoroutine(preparation):
                return
            preparation_task = asyncio.create_task(preparation)
            preparation_stop = (asyncio.create_task(stop_event.wait())
                                if stop_event is not None else None)
            try:
                while not preparation_task.done():
                    waiting = {preparation_task}
                    if preparation_stop is not None:
                        waiting.add(preparation_stop)
                    done, _ = await asyncio.wait(waiting, timeout=_MODEL_WAIT_HEARTBEAT_SECONDS,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if getattr(self, '_abort_flag', False):
                        preparation_task.cancel()
                        yield {"type": "aborted", "round": round_num + 1, "label": label}
                        return
                    if preparation_stop is not None and preparation_stop in done:
                        preparation_stop = None
                    if not done:
                        yield {"type": "heartbeat"}
                await preparation_task
            finally:
                if not preparation_task.done():
                    preparation_task.cancel()
                if preparation_stop is not None:
                    preparation_stop.cancel()
                await asyncio.gather(preparation_task, *((preparation_stop,)
                                                        if preparation_stop is not None else ()),
                                     return_exceptions=True)

        while round_num + 1 < max_rounds:
            round_num += 1
            # --- abort check ---
            if getattr(self, '_abort_flag', False):
                logger.info("[%s] 用户中断", label)
                yield {"type": "aborted", "round": round_num, "label": label}
                break

            # --- F1: 上下文窗口压缩 ---
            request_overhead = len(system) + len(json.dumps(tools, ensure_ascii=False))
            _MAX_CONTEXT_CHARS = min(30000, max(20000, 40000 - request_overhead))
            message_sizes = [len(json.dumps(message, ensure_ascii=False)) for message in msgs]
            total_chars = sum(message_sizes)
            if total_chars > _MAX_CONTEXT_CHARS and len(msgs) <= 4:
                msgs = _bound_tool_messages(msgs, _MAX_CONTEXT_CHARS)
                message_sizes = [len(json.dumps(message, ensure_ascii=False)) for message in msgs]
                total_chars = sum(message_sizes)
            if total_chars > _MAX_CONTEXT_CHARS and len(msgs) > 4:
                kept_head = msgs[:1]  # system / 首条
                tail_start = max(1, len(msgs) - 4)
                # 不以 tool 开头
                while tail_start > 1 and msgs[tail_start].get("role") == "tool":
                    tail_start -= 1
                # 如果尾部首条 assistant 有 tool_calls，确保其 tool response 也在尾部
                if (tail_start > 1 and msgs[tail_start].get("role") == "assistant"
                        and msgs[tail_start].get("tool_calls")):
                    # 需要包含它后面所有 tool response
                    tc_count = len(msgs[tail_start].get("tool_calls", []))
                    needed_end = tail_start + 1 + tc_count
                    if needed_end > len(msgs):
                        # tool response 部分在 middle 段 → 把这条 assistant 也压缩掉
                        tail_start -= 1
                        while tail_start > 1 and msgs[tail_start].get("role") == "tool":
                            tail_start -= 1
                tail_chars = message_sizes[0] + sum(message_sizes[tail_start:])
                while tail_chars > _MAX_CONTEXT_CHARS - 3000 and tail_start < len(msgs) - 2:
                    next_start = tail_start + 1
                    while next_start < len(msgs) and msgs[next_start].get("role") == "tool":
                        next_start += 1
                    if next_start >= len(msgs) - 1:
                        break
                    tail_chars -= sum(message_sizes[tail_start:next_start])
                    tail_start = next_start
                kept_tail = msgs[tail_start:]
                middle = msgs[1:tail_start]
                if middle:
                    summary_parts = []
                    tool_names = {
                        call.get("id"): call.get("function", {}).get("name")
                        for message in msgs if message.get("role") == "assistant"
                        for call in message.get("tool_calls", [])
                    }
                    lore_queries = {}
                    exact_lore_queries = {}
                    for message in msgs:
                        for call in message.get("tool_calls", []):
                            if call.get("function", {}).get("name") != "query_lorebook":
                                continue
                            try:
                                arguments = json.loads(call["function"].get("arguments", "{}"))
                            except (TypeError, ValueError):
                                continue
                            if isinstance(arguments, dict):
                                lore_queries[call.get("id")] = arguments
                                if arguments.get("entry_id") or arguments.get("entry_ids"):
                                    exact_lore_queries[call.get("id")] = arguments
                    latest_exact_lore_result = None
                    for message in reversed(msgs):
                        if (message.get("role") != "tool"
                                or message.get("tool_call_id") not in exact_lore_queries):
                            continue
                        try:
                            result = json.loads(message.get("content") or "{}")
                        except (TypeError, ValueError):
                            continue
                        entries = result.get("entries") if isinstance(result, dict) else None
                        if (not _tool_result_failed(result) and isinstance(entries, list)
                                and any(isinstance(entry, dict) and entry.get("id") and entry.get("content")
                                        for entry in entries)):
                            latest_exact_lore_result = message
                            break
                    state_feedback_tools = ("update_state", "undo_my_last_action", "write_narrative")
                    state_feedback_markers = ("玩家空间候选值", "NPC接触候选", "变量候选值", "世界属性候选值", _EVENT_INSTRUCTION_FEEDBACK_LABEL, "本轮候选结算", "状态投影显示条件事件可能触发")
                    state_results = [message for message in msgs
                                     if message.get("role") == "tool"
                                      and tool_names.get(message.get("tool_call_id")) in state_feedback_tools
                                      and any(marker in str(message.get("content", ""))
                                              for marker in state_feedback_markers)
                                     and not _tool_result_failed(message.get("content"))]
                    latest_state_result = state_results[-1] if state_results else None
                    latest_state_instructions = next((
                        message for message in reversed(state_results)
                        if _standalone_event_instruction_feedback(str(message.get("content", ""))) is not None
                    ), None)
                    for m in middle:
                        role = m.get("role", "")
                        mc = str(m.get("content", ""))
                        if role == "tool":
                            try:
                                tool_result = json.loads(mc)
                            except ValueError:
                                tool_result = None
                            entries = tool_result.get("entries") if isinstance(tool_result, dict) else None
                            entry_ids = ([entry.get("id") for entry in entries
                                          if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
                                         if isinstance(entries, list) else [])
                            lore_arguments = exact_lore_queries.get(m.get("tool_call_id"))
                            if (tool_names.get(m.get("tool_call_id")) == "query_lorebook"
                                    and isinstance(tool_result, dict) and tool_result.get("restart_required")):
                                summary_parts.append("[世界书快照失效] " + json.dumps(tool_result, ensure_ascii=False))
                                continue
                            if (tool_names.get(m.get("tool_call_id")) == "query_lorebook"
                                    and not lore_arguments and isinstance(tool_result, dict) and tool_result.get("snapshot")
                                    and not _tool_result_failed(tool_result)):
                                summary_parts.append("[世界书搜索进度] " + json.dumps({
                                    "query": lore_queries.get(m.get("tool_call_id"), {}),
                                    **{key: tool_result[key] for key in (
                                        "snapshot", "next_page", "total_matches", "approximate", "projection",
                                    ) if key in tool_result},
                                    "entries": [
                                        {key: entry[key] for key in (
                                            "id", "snapshot", "offset", "next_offset", "total_length", "truncated", "visibility", "related_entries",
                                        ) if key in entry}
                                        for entry in entries if isinstance(entry, dict)
                                    ] if isinstance(entries, list) else [],
                                    "content_omitted": True,
                                    "notice": "正文已省略；条目 offset 是搜索定位，不是完整阅读进度；按 entry_id、该 offset 和条目 snapshot 重读定位段，"
                                              "完整阅读须从 offset=0 开始。保留当前候选视图；快照失效按 query 从 page=0 重查，不携带旧 snapshot。"
                                              "资料仅供推演，非角色已知。",
                                }, ensure_ascii=False))
                            if lore_arguments and isinstance(entries, list) and not _tool_result_failed(tool_result):
                                requested_ids = lore_arguments.get("entry_ids") or [lore_arguments["entry_id"]]
                                not_returned_ids = [entry_id for entry_id in requested_ids if entry_id not in entry_ids]
                                query_status = {
                                    "requested_ids": requested_ids,
                                    "returned_ids": entry_ids,
                                    "not_returned_ids": not_returned_ids,
                                }
                                if "offset" in lore_arguments:
                                    query_status["offset"] = lore_arguments["offset"]
                                if "projection" in tool_result:
                                    query_status["projection"] = tool_result["projection"]
                                if "snapshot" in tool_result:
                                    query_status["snapshot"] = tool_result["snapshot"]
                                if not_returned_ids:
                                    query_status["note"] = (
                                        "仅记录本次精确查询未返回的条目，不证明词条不存在，"
                                        "也不能用旧片段证明本次仍可用。"
                                    )
                                summary_parts.append("[世界书查询状态] " + json.dumps(query_status, ensure_ascii=False))
                                if not entry_ids and not tool_result.get("content_omitted"):
                                    continue
                            if (tool_names.get(m.get("tool_call_id")) == "query_lorebook"
                                    and isinstance(tool_result, dict) and tool_result.get("content_omitted")):
                                summary_parts.append("[世界书正文省略] " + json.dumps({
                                    "query": lore_queries.get(m.get("tool_call_id"), {}), **tool_result,
                                }, ensure_ascii=False))
                            elif entry_ids:
                                is_latest_exact = m is latest_exact_lore_result
                                is_exact_read = m.get('tool_call_id') in exact_lore_queries
                                read_spans = [
                                    f"{entry['id']} offset={entry['offset']}, "
                                    f"next_offset={entry.get('next_offset')}, "
                                    f"total_length={entry.get('total_length')}, snapshot={entry.get('snapshot')}"
                                    for entry in entries[:3]
                                    if isinstance(entry, dict) and entry.get('id')
                                    and 'offset' in entry and is_exact_read
                                ]
                                excerpts = [
                                    f"{entry['id']} ({entry.get('visibility', 'public')}): "
                                    + ' '.join(entry.get('content', '')[:2000 if is_latest_exact else 180].splitlines())
                                    for entry in entries[:3]
                                    if isinstance(entry, dict) and entry.get('id') and entry.get('content')
                                ]
                                summary_parts.append(
                                    f"[世界书{'精确读取' if is_latest_exact else '结果'}] "
                                    f"entry_ids={', '.join(entry_ids)}；"
                                    + ("阅读进度：" + " | ".join(read_spans) + "；" if read_spans else "")
                                    + ("证据片段：" + " | ".join(excerpts) + "；" if excerpts else "")
                                    + "资料仅供推演、非角色自动知情；"
                                    + ("本轮状态投影，非已发生或角色已知；" if tool_result.get("projection") else "")
                                    + "可按 entry_id 从 offset=0 重查正文，携带条目 snapshot 按 next_offset 续读；失效丢弃旧片段按 restart 重查"
                                )
                            elif (tool_names.get(m.get("tool_call_id")) == "invoke_character"
                                  and isinstance(tool_result, dict) and tool_result.get("mode") == "advisory"):
                                summary_parts.append("[角色推演建议] " + _bounded_tool_content(mc, "invoke_character", 1000))
                            elif (tool_names.get(m.get("tool_call_id")) == "consult_rules"
                                  and isinstance(tool_result, dict) and not tool_result.get("category")
                                  and (isinstance(tool_result.get("answers"), list) or tool_result.get("answers_omitted"))):
                                summary_parts.append(
                                    "[在场与物品查询] "
                                    + ("候选未提交；" if tool_result.get("projection") else "当时已提交状态；")
                                    + "旧答案省略，非角色知情或互动许可；用原 question 和 include_candidates 重新查询。"
                                      "更新、撤销或切换分支后必须重查。"
                                )
                            elif (tool_names.get(m.get("tool_call_id")) == "consult_rules"
                                  and isinstance(tool_result, dict)
                                  and tool_result.get("category") in self._queryable_mechanics_definitions()
                                  and isinstance(entries, list)):
                                preview = {key: tool_result[key] for key in (
                                    "category", "source", "game_time", "offset", "next_offset", "total", "notice", "projection", "snapshot", "view", "node_status",
                                ) if key in tool_result}
                                if tool_result.get("view") == "runtime":
                                    preview["notice"] = "只读运行态，不推进状态；条件可选性不是已提交后果；候选未提交且不含未结算事件；非角色知情。"
                                    if tool_result.get("projection", {}).get("excludes_unsettled_events") is False:
                                        preview["notice"] = ("只读候选运行态，未提交；仅含已接收时间内定时部分，"
                                                             "不含事件掷骰或完整生命周期，不提前激活节点；非角色知情。")
                                preview["entries"] = [
                                    {key: entry[key] for key in ("rule_index", "offset", "next_offset", "total_chars", "snapshot", "read")
                                     if key in entry}
                                    for entry in entries[:5] if isinstance(entry, dict)
                                ]
                                summary_parts.append(
                                    "[剧本机制检索] " + json.dumps(preview, ensure_ascii=False)
                                    + "；本次匹配的规则正文已省略；用 consult_rules 的 category、rule_index 和条目 snapshot "
                                      "从 offset=0 重读；字段读取按条目 read 保留 result_path，从0重读该字段，不表示已知全部规则；"
                                      "保留 view 和候选视图；不得只续读尾段；快照失效时按原 node_status 重查列表。"
                                )
                            elif (tool_names.get(m.get("tool_call_id")) == "preview_choice_outcome"
                                  and isinstance(tool_result, dict) and tool_result.get("mode") == "declared_choice"):
                                summary_parts.append("[选择模拟] " + _bounded_tool_content(mc, "preview_choice_outcome", 1000))
                            elif (isinstance(tool_result, dict) and isinstance(tool_result.get("events"), list)
                                  and isinstance(tool_result.get("conditional_candidates"), list)):
                                preview = {
                                    key: [{field: event.get(field) for field in ("id", "time", "hint")}
                                          for event in tool_result[key] if isinstance(event, dict)]
                                    for key in ("events", "conditional_candidates")
                                }
                                for key in ("total_matches", "truncated", "conditional_total_matches",
                                            "conditional_truncated", "offset", "window_end", "note", "projection"):
                                    if key in tool_result:
                                        preview[key] = tool_result[key]
                                summary_parts.append("[事件预览] " + json.dumps(preview, ensure_ascii=False))
                            elif (tool_names.get(m.get("tool_call_id")) in ("recall_history", "query_npc_history")
                                  and isinstance(tool_result, dict)):
                                key = ("results" if tool_names.get(m.get("tool_call_id")) == "recall_history"
                                       else "interactions")
                                matches = tool_result.get(key)
                                if isinstance(matches, list):
                                    preview = [{field: hit.get(field) for field in ("turn", "summary", "relevance")
                                                if field in hit}
                                               for hit in matches[:5] if isinstance(hit, dict)]
                                    summary_parts.append("[历史检索] " + json.dumps({
                                        **({"npc": tool_result["npc"]} if "npc" in tool_result else {}),
                                        key: preview,
                                    }, ensure_ascii=False))
                                else:
                                    summary_parts.append(f"[工具结果] {mc[:60]}")
                            elif (tool_names.get(m.get("tool_call_id")) == "finalize_turn"
                                  and isinstance(tool_result, dict) and _tool_result_failed(tool_result)
                                  and ("event_instructions" in tool_result or tool_result.get("instructions_omitted"))):
                                summary_parts.append("[工具失败] finalize_turn: " + _bounded_tool_content(mc, "finalize_turn", 6000))
                            elif tool_names.get(m.get("tool_call_id")) == "submit_plan":
                                summary_parts.append("[计划反馈] " + mc.replace("\n", "\\n"))
                            elif tool_names.get(m.get("tool_call_id")) in state_feedback_tools:
                                name = tool_names[m.get("tool_call_id")]
                                if _tool_result_failed(mc):
                                    summary_parts.append(f"[工具失败] {name}: {mc}")
                                else:
                                    receipt = mc.split(" | ", 1)[0]
                                    for marker in state_feedback_markers:
                                        receipt = receipt.split(marker, 1)[0]
                                    summary_parts.append(f"[工具执行记录] {name}: {receipt}")
                                    if m is latest_state_result:
                                        start = min(mc.find(marker) for marker in state_feedback_markers if marker in mc)
                                        feedback = mc[start:]
                                        if _EVENT_INSTRUCTION_FEEDBACK_LABEL in feedback and len(feedback) > 6000:
                                            feedback = _bounded_tool_content(feedback, name, 6000)
                                        summary_parts.append("[状态投影反馈] " + feedback.replace("\n", "\\n"))
                                    elif m is latest_state_instructions:
                                        summary_parts.append(_standalone_event_instruction_feedback(mc))
                            else:
                                summary_parts.append(f"[工具结果] {mc[:60]}")
                        elif role == "assistant":
                            tc = m.get("tool_calls", [])
                            if tc:
                                names = [t.get("function", {}).get("name", "") for t in tc]
                                summary_parts.append(f"[调用] {', '.join(names)}")
                            elif mc:
                                summary_parts.append(f"[Agent] {mc[:60]}")
                        elif role == "user" and mc:
                            if mc.startswith("[上下文压缩] 之前的 ") and "\n" in mc:
                                previous_summary = mc.partition("\n")[2]
                                previous_lines = []
                                for line in previous_summary.splitlines():
                                    if latest_state_result is not None and line.startswith("[状态投影反馈] "):
                                        line = _standalone_event_instruction_feedback(line)
                                        if line is None:
                                            continue
                                    if latest_exact_lore_result is not None and line.startswith("[世界书精确读取] "):
                                        line = (line.split("；证据片段：", 1)[0]
                                                .split("；资料仅供推演", 1)[0]
                                                + "；可按 entry_id 和 offset 重查正文")
                                    previous_lines.append(line)
                                summary_parts.append("\n".join(previous_lines))
                            else:
                                summary_parts.append(f"[玩家消息] {mc}")
                    summary_lines = []
                    mechanics_reads = set()
                    historical_failures = set()
                    failure_prefix = "[工具失败] finalize_turn: "
                    historical_prefix = "[历史提交失败] finalize_turn: "
                    state_prefix = "[状态投影反馈] "
                    instruction_prefix = "[定时事件指令反馈] "
                    latest_instruction_seen = any(
                        _historical_event_instruction_feedback(
                            str(message.get("content", "")), tool_names.get(message.get("tool_call_id"), ""),
                        ) is not None
                        for message in kept_tail if message.get("role") == "tool"
                    )
                    for line in reversed("\n".join(summary_parts).splitlines()):
                        if line.startswith((failure_prefix, state_prefix, instruction_prefix)):
                            prefix = next(
                                marker for marker in (failure_prefix, state_prefix, instruction_prefix) if line.startswith(marker)
                            )
                            historical = _historical_event_instruction_feedback(
                                line.removeprefix(prefix), "finalize_turn" if prefix == failure_prefix else "update_state",
                            )
                            if historical is not None:
                                if latest_instruction_seen:
                                    if prefix == instruction_prefix:
                                        continue
                                    line = (historical_prefix if prefix == failure_prefix else state_prefix) + historical
                                latest_instruction_seen = True
                        if line.startswith(historical_prefix):
                            if line in historical_failures:
                                continue
                            historical_failures.add(line)
                        if line.startswith("[剧本机制检索] "):
                            try:
                                retrieval, _ = json.JSONDecoder().raw_decode(line.removeprefix("[剧本机制检索] "))
                            except ValueError:
                                retrieval = None
                            entries = retrieval.get("entries") if isinstance(retrieval, dict) else None
                            if (isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries)
                                    and isinstance(retrieval.get("snapshot"), str)):
                                read_key = json.dumps([
                                    {key: retrieval.get(key) for key in ("category", "view", "node_status", "snapshot")},
                                    [{key: entry.get(key) for key in ("rule_index", "snapshot", "read")} for entry in entries],
                                ], ensure_ascii=False, sort_keys=True)
                                if read_key in mechanics_reads:
                                    continue
                                mechanics_reads.add(read_key)
                        summary_lines.append(line)
                    summary = "\n".join(reversed(summary_lines))
                    compressed = {"role": "user", "content": f"[上下文压缩] 之前的 {len(middle)} 条消息摘要：\n{summary}"}
                    old_count = len(msgs)
                    msgs = kept_head + [compressed] + kept_tail
                    new_chars = sum(len(json.dumps(message, ensure_ascii=False)) for message in msgs)
                    if new_chars > _MAX_CONTEXT_CHARS:
                        msgs = _bound_tool_messages(msgs, _MAX_CONTEXT_CHARS)
                        new_chars = sum(len(json.dumps(message, ensure_ascii=False)) for message in msgs)
                    logger.info("[%s] 上下文压缩: %d→%d 条消息 (%d→%d chars)",
                                label, old_count, len(msgs), total_chars, new_chars)
                else:
                    msgs = _bound_tool_messages(msgs, _MAX_CONTEXT_CHARS)

            logger.info("[%s:R%d] 调用 LLM (tools=%d, msgs=%d)", label, round_num + 1, len(tools), len(msgs))
            queued_before_call = len(getattr(self, '_inject_queue', ()) or ())
            model_started = time.monotonic()
            if first_model_interval is not None and holder.model_calls == 0:
                first_model_interval["start"] = model_started
            holder.model_calls += 1
            holder.model_first_output_ms.append(None)
            holder.model_token_usage.append(None)
            holder.model_phases.append("规划" if after_plan_context is not None else
                                       "推演" if restrict_later_plans or label == "统一Agent" else label)
            tool_names = {
                call.get("id"): call.get("function", {}).get("name", "")
                for message in msgs for call in message.get("tool_calls", [])
            }
            message_parts = []
            for message in msgs:
                part = {
                    "role": message.get("role", ""),
                    "chars": len(json.dumps(message, ensure_ascii=False)),
                    "content_chars": len(message.get("content") or ""),
                }
                if message.get("reasoning_content"):
                    part["reasoning_chars"] = len(message["reasoning_content"])
                if message.get("tool_calls"):
                    part["tool_args_chars"] = sum(
                        len(call.get("function", {}).get("arguments") or "")
                        for call in message["tool_calls"]
                    )
                if message.get("role") == "tool":
                    part["tool_name"] = tool_names.get(message.get("tool_call_id"), "")
                message_parts.append(part)
            breakdown = {
                "system_chars": len(system),
                "tools_chars": len(json.dumps(tools, ensure_ascii=False)),
                "messages": message_parts,
            }
            holder.model_request_breakdowns.append(breakdown)
            holder.model_request_chars.append(
                breakdown["system_chars"] + breakdown["tools_chars"]
                + sum(part["chars"] for part in message_parts)
            )
            stop_event = getattr(self, '_agent_stop_event', None)
            streamed_tools = getattr(self.ai_provider, 'generate_with_tools_stream', None)
            progress_queue = None
            if getattr(self, '_active_agent_stream', False) and stop_event is not None and callable(streamed_tools):
                progress_queue = asyncio.Queue()

                async def collect_streamed_response():
                    async for update in streamed_tools(
                            msgs, system=system, tools=tools, max_tokens=max_tokens,
                            **self._stage_kwargs(stage_key)):
                        if update.get("type") == "response":
                            return update["response"]
                        if update.get("type") == "model_output_started":
                            holder.model_first_output_ms[-1] = int((time.monotonic() - model_started) * 1000)
                            await progress_queue.put({"type": "model_output_started"})
                        elif update.get("type") == "tool_preparing":
                            await progress_queue.put(update)
                    raise RuntimeError("AI 工具流缺少最终响应")

                model_request = collect_streamed_response()
            else:
                model_request = self.ai_provider.generate_with_tools(
                    msgs, system=system, tools=tools,
                    max_tokens=max_tokens, **self._stage_kwargs(stage_key),
                )
            if getattr(self, '_active_agent_stream', False) and stop_event is not None:
                model_task = asyncio.create_task(model_request)
                stop_task = asyncio.create_task(stop_event.wait())
                control_event = getattr(self, '_agent_control_event', None)
                control_task = asyncio.create_task(control_event.wait()) if control_event else None
                progress_task = asyncio.create_task(progress_queue.get()) if progress_queue else None
                interrupted_injections = []
                try:
                    await asyncio.sleep(0)
                    if not model_task.done() and not stop_task.done():
                        yield {"type": "model_waiting", "label": label, "round": round_num + 1}
                    while True:
                        waiting = {model_task, stop_task}
                        if control_task is not None:
                            waiting.add(control_task)
                        if progress_task is not None:
                            waiting.add(progress_task)
                        done, _ = await asyncio.wait(
                            waiting, timeout=_MODEL_WAIT_HEARTBEAT_SECONDS,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if model_task in done or stop_task in done:
                            break
                        if control_task in done:
                            control_event.clear()
                            inject_queue = getattr(self, '_inject_queue', None)
                            already_sent = len(pending_interrupted_injections) + (pending_inject is not None)
                            if (inject_queue and len(inject_queue) > already_sent
                                    and (round_num < max_rounds - 1
                                         or max_rounds < initial_max_rounds + 3)):
                                interrupted_injections = list(inject_queue[already_sent:])
                                model_task.cancel()
                                break
                            control_task = asyncio.create_task(control_event.wait())
                            continue
                        if progress_task in done:
                            yield {**progress_task.result(), "label": label, "round": round_num + 1}
                            progress_task = asyncio.create_task(progress_queue.get())
                            continue
                        yield {"type": "heartbeat"}
                    if getattr(self, '_abort_flag', False):
                        model_task.cancel()
                    elif not interrupted_injections:
                        resp = await model_task
                finally:
                    stop_task.cancel()
                    if control_task is not None:
                        control_task.cancel()
                    if progress_task is not None:
                        progress_task.cancel()
                    if not model_task.done():
                        model_task.cancel()
                    cleanup_tasks = (model_task, stop_task)
                    if control_task is not None:
                        cleanup_tasks += (control_task,)
                    if progress_task is not None:
                        cleanup_tasks += (progress_task,)
                    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
                if getattr(self, '_abort_flag', False):
                    yield {"type": "aborted", "round": round_num + 1, "label": label}
                    break
                if interrupted_injections:
                    if first_model_interval is not None and "end" not in first_model_interval:
                        first_model_interval["end"] = time.monotonic()
                    holder.model_timings_ms.append(int((time.monotonic() - model_started) * 1000))
                    if round_num == max_rounds - 1:
                        max_rounds += 1
                    for message in interrupted_injections:
                        msgs.append({"role": "user", "content": message})
                    pending_interrupted_injections.extend(interrupted_injections)
                    continue
                if (progress_queue is not None
                        and len(getattr(self, '_inject_queue', ()) or ()) <= queued_before_call):
                    if progress_task is not None and progress_task.done() and not progress_task.cancelled():
                        yield {**progress_task.result(), "label": label, "round": round_num + 1}
                    while (not progress_queue.empty()
                           and len(getattr(self, '_inject_queue', ()) or ()) <= queued_before_call):
                        yield {**progress_queue.get_nowait(), "label": label, "round": round_num + 1}
            else:
                resp = await model_request
            if first_model_interval is not None and "end" not in first_model_interval:
                first_model_interval["end"] = time.monotonic()
            holder.model_timings_ms.append(int((time.monotonic() - model_started) * 1000))
            if getattr(self, '_abort_flag', False):
                yield {"type": "aborted", "round": round_num + 1, "label": label}
                break
            late_inject = len(getattr(self, '_inject_queue', ()) or ()) > queued_before_call
            had_pending_inject = pending_inject is not None
            had_interrupted_injections = bool(pending_interrupted_injections)
            if pending_inject is not None:
                holder.user_injections.append({
                    "round": round_num + 1, "label": label,
                    "message": pending_inject, "plan_reply": pending_plan_record is not None,
                })
                if pending_plan_record is not None:
                    pending_plan_record["player_reply"] = pending_inject
                self._inject_queue.pop(0)
                event = {"type": "user_inject", "label": label, "message": pending_inject}
                if pending_plan_id:
                    event["plan_id"] = pending_plan_id
                    self._queued_agent_plan_reply_id = None
                yield event
                pending_inject = None
                pending_plan_record = None
                pending_plan_id = None
            if pending_interrupted_injections:
                del self._inject_queue[:len(pending_interrupted_injections)]
                for message in pending_interrupted_injections:
                    holder.user_injections.append({
                        "round": round_num + 1, "label": label,
                        "message": message, "plan_reply": False,
                    })
                    yield {"type": "user_inject", "label": label, "message": message}
                pending_interrupted_injections.clear()
            needs_replay = ((late_inject or had_pending_inject or had_interrupted_injections)
                            and bool(getattr(self, '_inject_queue', None)))
            if needs_replay and round_num == max_rounds - 1 and max_rounds < initial_max_rounds + 3:
                max_rounds += 1
            replay_after_inject = needs_replay and round_num < max_rounds - 1
            if replay_after_inject:
                pending_inject = self._inject_queue[0]
                msgs.append({"role": "user", "content": pending_inject})

            # #10 成本追踪: 累加 token 用量
            usage = resp.get("usage") or {}
            holder.prompt_tokens += usage.get("prompt_tokens", 0)
            holder.completion_tokens += usage.get("completion_tokens", 0)
            if usage:
                holder.model_token_usage[-1] = {
                    "prompt": usage.get("prompt_tokens"),
                    "completion": usage.get("completion_tokens"),
                }

            # #7 精细用量统计
            if usage and hasattr(self, 'usage_tracker'):
                _model = resp.get("model", "") or getattr(self.ai_provider, 'model', '')
                self.usage_tracker.record(
                    model=_model,
                    task_type=stage_key,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    cache_tokens=usage.get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0)),
                )

            if needs_replay:
                continue
            queued_after_response = len(getattr(self, '_inject_queue', ()) or ())
            tc_list = resp.get("tool_calls") or []
            if suppress_plan_questions:
                tc_list = [
                    {**tc, "arguments": {**tc["arguments"], "questions": []}}
                    if (tc.get("name") == "submit_plan" and isinstance(tc.get("arguments"), dict)
                        and tc["arguments"].get("questions")) else tc
                    for tc in tc_list
                ]
            needs_final_reply = (bool(tc_list) and final_reply_after_tools
                                 and round_num == max_rounds - 1 and max_rounds == initial_max_rounds)
            if tc_list and on_tool_batch is not None:
                on_tool_batch(tc_list)
            if needs_final_reply:
                max_rounds += 1
            defer_preparation = (plan_before_tools and not first_plan_seen
                                 and after_plan_context is not None and tc_list
                                 and tc_list[0].get("name") == "submit_plan"
                                  and (len(tc_list) == 1 or all(
                                      call.get("name") != "submit_plan" for call in tc_list[1:])))
            if tc_list and before_tool_calls is not None and not defer_preparation:
                async for event in prepare_tool_calls():
                    yield event
                if getattr(self, '_abort_flag', False):
                    return
            content = strip_think_tags(resp.get("content") or "")

            # #11: 如果是续写轮次，拼接前段文本
            if _continuation_prefix:
                content = _continuation_prefix.rstrip("…——,，") + content
                _continuation_prefix = ""

            # ========== 终止点1：纯文本响应（无工具调用）= 叙事输出 ==========
            if not tc_list:
                _narrative_text = content

                # #11 叙事断点续写: 检测截断并自动续写
                if _narrative_text and self._looks_truncated(_narrative_text) and round_num < max_rounds - 1:
                    logger.info("[%s] 检测到叙事截断，自动续写 (第%d轮, %d字, 末尾: %s)",
                                label, round_num + 1, len(_narrative_text), repr(_narrative_text[-10:]))
                    _continuation_prefix = _narrative_text
                    msgs.append({"role": "assistant", "content": _narrative_text})
                    msgs.append({"role": "user", "content": "请继续完成叙事，直接续写。"})
                    continue

                if terminal_tool_required and terminal_repair_count < 1:
                    terminal_repair_count += 1
                    if round_num == max_rounds - 1:
                        max_rounds += 1
                    msgs.append({"role": "assistant", "content": content})
                    msgs.append({"role": "user", "content": (
                        "本轮尚未完成结算，请用工具提交而非纯文本结束。"
                        "若本轮尚未提交计划，先调用 submit_plan；若叙事尚未提交，再调用 write_narrative；"
                        "随后调用 finalize_turn 或 delegate_settlement。"
                        "不要重复已成功的状态工具调用。"
                    )})
                    continue

                text_preview = _narrative_text[:80].replace('\n', ' ') if _narrative_text else "(空)"
                logger.info("[%s] 完成 (第%d轮, %d字): %s...", label, round_num + 1, len(_narrative_text), text_preview)
                holder.text = _narrative_text
                holder.records = records
                yield {"type": "agent_done", "label": label,
                       "round": round_num + 1, "text": _narrative_text}
                return

            # ========== 有工具调用的轮次 ==========
            # 记录本轮伴随文本（Agent 可能在调工具的同时输出部分叙事）
            if content and len(content) > 100:
                _narrative_text = content

            assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
            reasoning = resp.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc.get("name", ""),
                              "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
                for tc in tc_list
            ]
            msgs.append(assistant_msg)

            active_plan_index = next((index for index in range(len(tc_list) - 1, -1, -1)
                                      if tc_list[index].get("name") == "submit_plan"), None)
            active_plan_call = tc_list[active_plan_index] if active_plan_index is not None else None
            plan_args = (active_plan_call.get("arguments") or {}) if active_plan_call else {}
            if isinstance(plan_args, str):
                try:
                    plan_args = json.loads(plan_args)
                except Exception:
                    plan_args = {}
            if not isinstance(plan_args, dict):
                plan_args = {}
            has_plan_questions = bool(plan_args.get("questions"))
            defer_for_questions = (interactive_plan and active_plan_call is not None and has_plan_questions)
            if (defer_for_questions and round_num == max_rounds - 1
                    and max_rounds < initial_max_rounds + 3):
                max_rounds = initial_max_rounds + 3
            plan_id = None
            plan_record = None
            hit_terminal = False
            plan_seen = False
            same_batch_read = (plan_before_tools and any(
                call.get("name") in _PRE_PLAN_READ_TOOLS for call in tc_list))
            for tc in tc_list:
                if getattr(self, '_abort_flag', False):
                    holder.text = _narrative_text
                    holder.records = records
                    yield {"type": "aborted", "round": round_num + 1, "label": label}
                    return
                name = tc.get("name", "")
                args = tc.get("arguments") or {}
                deferred_for_plan = (plan_before_tools and active_plan_call is not None
                                     and tc is not active_plan_call
                                     and ((not plan_seen and name not in _PRE_PLAN_READ_TOOLS)
                                          or (plan_seen and after_plan_context is not None
                                              and (has_plan_questions or name not in _PRE_PLAN_READ_TOOLS))))
                deferred_for_read = (same_batch_read and name in (
                    "write_narrative", "finalize_turn", "delegate_settlement"))
                deferred_for_injection = len(getattr(self, '_inject_queue', ()) or ()) > queued_after_response
                deferred = (defer_for_questions and tc is not active_plan_call
                            and (not plan_before_tools or plan_seen
                                 or name not in _PRE_PLAN_READ_TOOLS)) or deferred_for_plan or deferred_for_read or deferred_for_injection
                if not deferred and report_tool_start:
                    yield {"type": "tool_started", "label": label,
                           "round": round_num + 1, "tool_name": name}
                    if getattr(self, '_abort_flag', False):
                        holder.text = _narrative_text
                        holder.records = records
                        yield {"type": "aborted", "round": round_num + 1, "label": label}
                        return
                    deferred_for_injection = len(getattr(self, '_inject_queue', ()) or ()) > queued_after_response
                    deferred = deferred_for_injection
                if deferred:
                    if deferred_for_injection:
                        reason = "玩家补充了新指令，此调用未执行；请先读取插话，再根据已完成工具结果重新推演"
                    elif defer_for_questions and (plan_seen or not deferred_for_plan):
                        reason = "计划等待玩家选择；收到玩家回复后重新调用此工具"
                    elif deferred_for_plan and plan_seen:
                        reason = "请等待下一轮完整推演上下文后重新调用此工具"
                    elif deferred_for_read and not deferred_for_plan:
                        reason = "本轮同时查询资料；请等待下一轮收到完整工具结果后重新提交叙事与结算"
                    else:
                        reason = "请先调用 submit_plan 提交本轮计划，再重新调用此工具"
                    result = json.dumps({"error": reason}, ensure_ascii=False)
                else:
                    tool_started = time.monotonic()
                    try:
                        result = dispatch(name, args)
                        if asyncio.iscoroutine(result):
                            if getattr(self, '_active_agent_stream', False):
                                tool_task = asyncio.create_task(result)
                                stop_task = (asyncio.create_task(stop_event.wait())
                                             if name in ("recall_history", "finalize_turn", "delegate_settlement")
                                             and stop_event is not None else None)
                                control_event = getattr(self, '_agent_control_event', None)
                                control_task = (asyncio.create_task(control_event.wait())
                                                if name in ("finalize_turn", "delegate_settlement")
                                                and control_event is not None else None)
                                interrupted_for_injection = False
                                try:
                                    while not tool_task.done():
                                        waiting = {tool_task}
                                        if stop_task is not None:
                                            waiting.add(stop_task)
                                        if control_task is not None:
                                            waiting.add(control_task)
                                        done, _ = await asyncio.wait(
                                            waiting, timeout=_MODEL_WAIT_HEARTBEAT_SECONDS,
                                            return_when=asyncio.FIRST_COMPLETED,
                                        )
                                        if stop_task is not None and stop_task in done:
                                            if getattr(self, '_abort_flag', False):
                                                tool_task.cancel()
                                                await asyncio.gather(tool_task, return_exceptions=True)
                                                yield {"type": "aborted", "round": round_num + 1, "label": label}
                                                return
                                            stop_task = None
                                        if control_task is not None and control_task in done:
                                            control_event.clear()
                                            if (len(getattr(self, '_inject_queue', ()) or ()) > queued_after_response
                                                    and not tool_task.done()):
                                                tool_task.cancel()
                                                await asyncio.gather(tool_task, return_exceptions=True)
                                                interrupted_for_injection = True
                                                result = json.dumps({"error": "玩家补充了新指令，此调用未执行；请先读取插话，再根据已完成工具结果重新推演"}, ensure_ascii=False)
                                                break
                                            control_task = asyncio.create_task(control_event.wait())
                                        if not done:
                                            yield {"type": "heartbeat"}
                                    if not interrupted_for_injection:
                                        result = await tool_task
                                finally:
                                    if stop_task is not None:
                                        stop_task.cancel()
                                    if control_task is not None:
                                        control_task.cancel()
                                    if not tool_task.done():
                                        tool_task.cancel()
                                    await asyncio.gather(
                                        tool_task, *((stop_task,) if stop_task is not None else ()),
                                        *((control_task,) if control_task is not None else ()),
                                        return_exceptions=True,
                                    )
                            else:
                                result = await result
                    except Exception as exc:
                        logger.warning("[%s] 工具 %s 执行失败: %s", label, name, exc)
                        error_ctx = {"error": str(exc)}
                        if name.startswith("update_"):
                            error_ctx["hint"] = "请检查参数格式和值范围后重试"
                        result = json.dumps(error_ctx, ensure_ascii=False)
                    finally:
                        tool_duration_ms = int((time.monotonic() - tool_started) * 1000)
                failed = _tool_result_failed(result)
                if tc is active_plan_call and not failed:
                    plan_seen = True
                    if (after_plan_context is not None and round_num == max_rounds - 1
                            and max_rounds < initial_max_rounds + 3):
                        max_rounds += 1
                show_plan = (tc is active_plan_call and not failed and interactive_plan
                             and (not restrict_later_plans or not first_plan_seen)
                             and round_num < max_rounds - 1
                             and not getattr(self, '_inject_queue', None)
                              and (defer_for_questions or after_plan_context is not None or not any(
                                  call.get("name") in ("finalize_turn", "delegate_settlement")
                                  for call in tc_list[active_plan_index + 1:])))
                if show_plan and has_plan_questions:
                    plan_id = uuid4().hex
                    self._pending_agent_plan_id = plan_id
                if name == "submit_plan" and not failed:
                    first_plan_seen = True
                record = {"name": name, "args": args, "result": result,
                          **({"deferred": True} if deferred else {"duration_ms": tool_duration_ms})}
                records.append(record)
                if plan_id and tc is active_plan_call:
                    plan_record = record
                result_preview = str(result)[:120].replace('\n', ' ')
                logger.info("[%s:R%d] %s → %s", label, round_num + 1, name, result_preview)
                tool_event = {
                    "type": "tool_call",
                    "label": label,
                    "round": round_num + 1,
                    "tool_name": name,
                    "tool_args": args,
                    "tool_result": str(result)[:500],
                }
                if show_plan:
                    tool_event["show_plan"] = True
                if show_plan and plan_id:
                    tool_event["plan_id"] = plan_id
                yield tool_event
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result if isinstance(result, str)
                               else json.dumps(result, ensure_ascii=False),
                })
                if name in ("finalize_turn", "delegate_settlement") and not failed:
                    hit_terminal = True
                    break

            if hit_terminal:
                holder.text = _narrative_text
                holder.records = records
                logger.info("[%s] 终结工具触发，结束循环 (第%d轮, 叙事%d字)", label, round_num + 1, len(holder.text or ""))
                yield {"type": "agent_done", "label": label,
                       "round": round_num + 1, "text": holder.text or ""}
                return
            if plan_before_tools and plan_seen:
                tools = full_tools
            if needs_final_reply:
                msgs.append({"role": "user", "content": "资料已查询完毕，请直接回答玩家的问题，不再调用工具。"})

            # --- inject check: let the user inject a message between rounds ---
            inject_queue = getattr(self, '_inject_queue', None)
            if (len(inject_queue or ()) > queued_after_response and round_num == max_rounds - 1
                    and max_rounds < initial_max_rounds + 3):
                max_rounds += 1
            if plan_id and inject_queue is not None and round_num < max_rounds - 1:
                control_event = getattr(self, '_agent_control_event', None)
                heartbeat_counter = 0
                logger.info("[%s] submit_plan 带问题，等待用户选择...", label)
                holder.plan_waits += 1
                wait_started = time.monotonic()
                yield {"type": "agent_waiting", "label": label, "reason": "等待用户选择"}
                while not inject_queue:
                    if getattr(self, '_abort_flag', False):
                        break
                    if control_event is not None:
                        try:
                            await asyncio.wait_for(control_event.wait(), timeout=10)
                        except asyncio.TimeoutError:
                            yield {"type": "heartbeat"}
                        else:
                            control_event.clear()
                    else:
                        await asyncio.sleep(0.5)
                        heartbeat_counter += 1
                        if heartbeat_counter % 20 == 0:
                            yield {"type": "heartbeat"}
                holder.plan_wait_ms += int((time.monotonic() - wait_started) * 1000)
            if inject_queue and round_num < max_rounds - 1 and not getattr(self, '_abort_flag', False):
                if plan_id:
                    self._pending_agent_plan_id = None
                injected = inject_queue[0]
                msgs.append({"role": "user", "content": injected})
                if plan_id and on_plan_reply is not None:
                    on_plan_reply(injected)
                pending_plan_id = plan_id if plan_id == getattr(self, "_queued_agent_plan_reply_id", None) else None
                pending_plan_record = plan_record if pending_plan_id else None
                pending_inject = injected
            if after_plan_context is not None and plan_seen and not getattr(self, '_abort_flag', False):
                if defer_preparation and before_tool_calls is not None:
                    async for event in prepare_tool_calls():
                        yield event
                    if getattr(self, '_abort_flag', False):
                        return
                system, private_context = (after_plan_context() if callable(after_plan_context)
                                           else after_plan_context)
                msgs[0] = {"role": "user", "content": private_context}
                after_plan_context = None

        else:
            # Exhausted max_rounds without returning
            logger.warning("[%s] 达到最大轮数 %d", label, max_rounds)
            yield {"type": "agent_max_rounds", "label": label, "max_rounds": max_rounds}

        holder.text = _narrative_text or content
        holder.records = records

    # ================================================================
    # Foreground agent -- retrieval + narration
    # ================================================================

    async def _dispatch_foreground_tool(self, ctx: dict, name: str, args: dict) -> str:
        """Execute a foreground tool. Scene tools record into ctx, no state mutation."""
        if name == "set_atmosphere":
            ctx["atmosphere"] = args
            return "氛围已设置: " + ", ".join(
                f"{k}={v}" for k, v in args.items() if v
            )
        if name == "set_scene_image":
            prompt = str(args.get("prompt", "")).strip()
            if not prompt:
                return "错误: prompt 为空，已忽略"
            ctx["scene_image_prompt"] = args
            return "场景图生成已记录"
        if name == "review_narrative":
            return await self._review_narrative_tool(args.get("narrative", ""), ctx)
        # Retrieval / dice tools reuse the native executor
        return await self._run_tool_native_async(name, args)

    async def _review_narrative_tool(self, narrative: str, ctx: dict) -> str:
        """通过共享评审入口返回结构化结果。"""
        if not narrative.strip():
            return "错误：叙事为空"

        review = None
        try:
            review = await self._review_narrative(narrative, ctx.get("action_text", ""), ctx)
        except Exception as e:
            logger.warning("review LLM 调用失败: %s", e)
        return json.dumps(review if review is not None else {
            "available": False, "pass": None, "violations": [],
        }, ensure_ascii=False)

    def _format_agent_instructions(self, ctx: dict, stages: tuple[str, ...], *, state: dict | None = None) -> str:
        lines = list(ctx.get("turn_instructions", []))
        directives = ctx.get("stage_directives", {})
        for stage in stages:
            lines.extend(f"[{stage}] {directive}" for directive in directives.get(stage, []))
        instructions = self.script_variables.expand_macros("\n".join(lines), self.current_state if state is None else state)
        return "<turn_instructions>\n" + instructions + "\n</turn_instructions>" if lines else ""

    def _visible_interactables_context(self) -> str:
        items = self.current_state.get("location_interactables") or []
        lines = []
        for item in items[:12]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            details = [f"{item.get('name') or item['id']}({item['id']})"]
            for field, label in (("description", "描述"), ("action_hint", "行动提示"),
                                 ("required_item", "所需物品"), ("locked_skill", "技能限制")):
                if item.get(field):
                    details.append(f"{label}：{str(item[field])[:160]}")
            if item.get("used"):
                details.append("已使用")
            lines.append("- " + "；".join(details))
        return ("<scene_interactables>\n玩家当前地点可见交互项：\n"
                + "\n".join(lines) + "\n</scene_interactables>") if lines else ""

    def _recent_settled_events_context(self, recent_nodes: list[dict]) -> str:
        from datetime import timedelta
        from engine.event_scheduler import parse_time

        settled_lines = []
        recent_ids = set()
        for node in recent_nodes[-2:]:
            for event_id in node.get("triggered_events") or []:
                event = self._event_def_by_id.get(event_id)
                if not event:
                    continue
                recent_ids.add(event_id)
                settled_lines.append(
                    f"- 第{node.get('turn_number', '?')}回合（截至{node.get('game_time', '')}）"
                    f"{event_id} [visibility={event.get('visibility', 'public')}]: "
                    f"{str(event.get('description') or '')[:220]}"
                )
        settled_lines = settled_lines[-12:]
        current_time = parse_time(self.current_state.get("game_time"))
        if current_time and len(settled_lines) < 12:
            fired = set(self.current_state.get("fired_one_time_events") or [])
            earlier = []
            for event in self.script.get("one_time_events", []):
                event_id = event.get("id")
                if event_id not in fired or event_id in recent_ids:
                    continue
                fire_time = parse_time(event.get("trigger_time"))
                if not fire_time or not timedelta(0) <= current_time - fire_time <= timedelta(hours=6):
                    continue
                earlier.append((fire_time, f"- 已结算于{fire_time.isoformat()} "
                                f"{event_id} [visibility={event.get('visibility', 'public')}]: "
                                f"{str(event.get('description') or '')[:220]}"))
            earlier.sort(key=lambda item: item[0], reverse=True)
            settled_lines.extend(line for _, line in earlier[:12 - len(settled_lines)])
        if not settled_lines:
            return ""
        return ("<settled_events>\n最近已结算的剧本事件，仅供幕后推演；不代表主角已知。"
                "世界或隐藏事件不能直接写成主角见闻或面向玩家的计划，须有可靠获知途径。\n"
                + "\n".join(settled_lines) + "\n</settled_events>")

    def _build_foreground_context(self, ctx: dict, player_action: dict,
                                  *, unified: bool = False) -> str:
        """Minimal per-turn context for the foreground agent."""
        state = self.current_state
        player = state.get("player", {})
        dn = state.get("display_names", {})

        loc_id = player.get("location", "")
        loc_name = self.prompt_builder._resolve_location_name(loc_id)
        location_description = state.get("location_descriptions", {}).get(loc_id, "")
        scene_detail = (f"地点底稿（仅写入可感知的细节）：{location_description[:300]}\n"
                        if location_description else "")

        # Compact present NPC list
        npc_states = state.get("npcs", {})
        npc_lines = []
        for nid in (ctx.get("present_npc_ids") or [])[:6]:
            ns = npc_states.get(nid, {})
            if not isinstance(ns, dict):
                continue
            nm = ns.get("name", dn.get(nid, nid))
            att = ns.get("attitude_toward_player", "")
            npc_lines.append(f"- {nm}({nid})" + (f" 态度:{att}" if att != "" else ""))

        attrs = player.get("attributes", {})
        attrs_line = ", ".join(
            f"{dn.get(k, k)}={v}" for k, v in attrs.items()
        ) if isinstance(attrs, dict) else ""
        inv = state.get("inventory", [])
        inv_line = ", ".join(
            f"{it.get('item', '')}x{it.get('quantity', 1)}" for it in inv
            if isinstance(it, dict)
        ) if inv else "无"

        # Full previous-round narrative for continuity (truncated to 1500 chars)
        prev_round_text = ""
        recent = ctx.get("recent_nodes", [])
        if recent:
            rn = recent[-1]
            prev_narrative = rn.get("ai_response", "") or rn.get("narrative", "")
            action_raw = rn.get("player_action")
            prev_action = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")
            if prev_action:
                prev_round_text = f"[玩家行动] {prev_action}\n\n{prev_narrative}"
            else:
                prev_round_text = prev_narrative

        parts = [
            f"<current_scene>\n位置：{loc_name}（{loc_id}）\n"
            f"{scene_detail}"
            f"时间：{state.get('game_time', '')}\n"
            f"天气：{state.get('current_weather', '')}\n</current_scene>",
            f"<player>\n姓名：{player.get('name', '')}\n"
            + (f"属性：{attrs_line}\n" if attrs_line else "")
            + f"背包：{inv_line}\n</player>",
        ]
        interactables = self._visible_interactables_context()
        if interactables:
            parts.append(interactables)
        world_props = state.get("world_properties") or {}
        if world_props or "active_persistent_states" in state:
            world_lines = ["当前存档中的世界状态（仅供推演，不代表主角已知；以当前值为准）："]
            if world_props:
                prop_names = {prop["id"]: prop.get("name", prop["id"])
                              for prop in self.script.get("world_properties", [])}
                world_lines.append("世界属性：")
                for prop_id, value in world_props.items():
                    world_lines.append(f"- {prop_names.get(prop_id, dn.get(prop_id, prop_id))}({prop_id}): {str(value)[:100]}")
            if "active_persistent_states" in state:
                ps_defs = {ps["id"]: ps for ps in self.script.get("persistent_states", [])}
                ps_descs = state.get("persistent_state_descriptions", {})
                world_lines.append("已激活持续状态（未列出的状态视为未激活）：")
                for ps_id in state["active_persistent_states"]:
                    ps = ps_defs.get(ps_id, {})
                    name = ps.get("name") or dn.get(ps_id, ps_id)
                    description = ps_descs.get(ps_id) or ps.get("description", "")
                    world_lines.append(f"- {name}({ps_id})" + (f": {description[:120]}" if description else ""))
                if not state["active_persistent_states"]:
                    world_lines.append("- 无")
            parts.append("<world_state>\n" + "\n".join(world_lines) + "\n</world_state>")
        settled_events = self._recent_settled_events_context(recent)
        if settled_events:
            parts.append(settled_events)
        if npc_lines:
            parts.append("<npcs_present>\n" + "\n".join(npc_lines) + "\n</npcs_present>")
        pc_clues = self.prompt_builder.format_pc_lore_clues(state)
        if pc_clues:
            parts.append(pc_clues)
        if unified:
            event_reports = self._pc_event_reports_context(state)
            if event_reports:
                parts.append(event_reports)
            npc_knowledge = self._build_npc_knowledge_context(ctx, state)
            if npc_knowledge:
                parts.append("<npc_private_knowledge>\n仅用于塑造各NPC的言行，不代表主角知情；不得直接泄漏幕后信息。\n"
                             + npc_knowledge + "\n</npc_private_knowledge>")

        # 首轮注入开局文本（预设角色的 opening_text + 开局剧情描述）
        if self.turn_number <= 1 and (not prev_round_text or player_action.get("type") == "system"):
            opening_parts = []
            if player_action.get("type") == "system":
                opening_parts.append("以下是剧本预置的开场素材，不是已经发生的玩家回合；据此生成开场。")
                opening_parts.append(
                    f"这是当前时刻的开场展示，不消耗玩家行动时间；结算 end_time 必须为 {ctx['old_time']}。"
                )
            # 预设角色开局文本
            opening_text = state.get("opening_text", "") or (
                recent[-1].get("ai_response", "") if player_action.get("type") == "system" and recent else ""
            )
            if opening_text:
                opening_parts.append(opening_text[:500])
            # 剧本通用开局
            script_opening = self.script.get("opening", {}).get("text", "")
            if script_opening and script_opening != opening_text:
                opening_parts.append(script_opening[:500])
            # 角色背景
            bio = player.get("bio", "")
            if bio:
                opening_parts.append(f"角色背景：{bio[:200]}")
            goal = player.get("long_term_goal", "") or state.get("long_term_goal", "")
            if goal:
                opening_parts.append(f"当前目标：{goal[:150]}")
            if player_action.get("type") == "system" and recent:
                opening_choices = [choice.get("text", "") for choice in recent[-1].get("choices_presented", [])[:6]
                                   if isinstance(choice, dict) and choice.get("text")]
                if opening_choices:
                    opening_parts.append("剧本预设可行动方向（尚未被玩家选择）：\n" + "\n".join(
                        f"- {text}" for text in opening_choices
                    ))
            if opening_parts:
                parts.append("<opening_context>\n" + "\n\n".join(opening_parts) + "\n</opening_context>")

        if unified:
            recent_queries = self._recent_query_context()
            if recent_queries:
                parts.append(recent_queries)
            history_summary = state.get("history_summary", "")
            if history_summary:
                if len(history_summary) > 2000:
                    history_summary = history_summary[:1000] + "\n…\n" + history_summary[-997:]
                parts.append(f"<history_summary>\n{history_summary}\n</history_summary>")
            if player_action.get("type") != "system":
                last_summarized = state.get("last_summarized_turn", 0) if history_summary else 0
                recent_lines = []
                for node in recent[-5:-1]:
                    if node.get("turn_number", 0) <= last_summarized:
                        continue
                    action = node.get("player_action")
                    action_text = action.get("text", "") if isinstance(action, dict) else str(action or "")
                    narrative = node.get("ai_response") or node.get("narrative") or ""
                    if action_text or narrative:
                        recent_lines.append(f"第{node.get('turn_number', '?')}回合："
                                            + (f"[玩家行动] {action_text[:160]}\n" if action_text else "")
                                            + narrative[-400:])
                if recent_lines:
                    parts.append("<recent_history>\n" + "\n\n".join(recent_lines)
                                 + "\n</recent_history>")

        if prev_round_text and player_action.get("type") != "system":
            parts.append(f"<previous_round>\n{prev_round_text[-1500:]}\n</previous_round>")
        if ctx.get("opening_description"):
            parts.append(f"<opening_choice_result>\n{ctx['opening_description']}\n</opening_choice_result>")

        history_hits = ctx.get("rag_history_hits") or []
        if history_hits:
            parts.append("<retrieved_history>\n相关过往回合记录：\n"
                         + "\n".join(hit[:500] for hit in history_hits[:5])
                         + "\n</retrieved_history>")
        bank_hits = ctx.get("databank_hits") or []
        if bank_hits:
            parts.append("<databank_reference>\n知识库参考材料（不代表主角已知）：\n"
                         + "\n".join(f"[{hit.get('filename', '')}] {hit.get('text', '')[:300]}"
                                     for hit in bank_hits[:3])
                         + "\n</databank_reference>")

        # 注入 workflow 模式有的关键上下文
        hint_parts = []

        # 1. Author's note
        an = getattr(self, 'authors_note', '') or ''
        if an:
            hint_parts.append(f"创作指令：{an[:200]}")

        # 2. Negative prompt
        neg = getattr(self, 'negative_prompt', '') or ''
        if neg:
            hint_parts.append(f"禁止事项：{neg[:150]}")

        keywords = []
        recall_hints = ctx.get("route", {}).get("recall_hints", [])
        if isinstance(recall_hints, list):
            keywords = [hint.strip()[:40] for hint in recall_hints[:3]
                        if isinstance(hint, str) and hint.strip()]
            if keywords:
                hint_parts.append("可检索线索（仅为关键词，不代表主角知情；需要时用 query_lorebook 核实）："
                                  + "、".join(keywords))

        # 3. Activated lorebook entries relevant to this turn
        activated_lore = ctx.get("activated_lore", [])
        action_text = player_action.get("text", "").lower()
        ctx["lore_preview_ids"] = []
        preview = []
        scene_detail_covered = False
        if activated_lore and hasattr(self, 'prompt_builder') and self.prompt_builder.lorebook:
            lore_texts = []
            normalized_action = self.prompt_builder.lorebook._normalize_query_dates(action_text)
            primary_matches = {
                entry.id: self.prompt_builder.lorebook.primary_key_match_count(entry.id, action_text)
                for entry in activated_lore
            }
            longest_matches = {
                entry.id: max((len(key) for key in self.prompt_builder.lorebook._precomputed.get(entry.id, ([], []))[0]
                               if key in normalized_action), default=0)
                for entry in activated_lore
            }
            rag_ids = ctx.get("lore_ids_from_rag", set())
            rag_rank = {entry_id: rank for rank, entry_id in enumerate(ctx.get("rag_lore_order") or [])
                        if entry_id in rag_ids}
            loc_id = state.get("player", {}).get("location", "")
            scene_kg_id = f"_kg_loc_{loc_id}" if loc_id else ""
            present_profiles = {f"_npc_profile_{npc_id}" for npc_id in ctx.get("present_npc_ids") or []}
            present_profiles.update(f"_kg_npc_{npc_id}" for npc_id in ctx.get("present_npc_ids") or [])
            prior_context_ids = ctx.get("lore_prior_context_ids") or set()
            ranked_lore = sorted(activated_lore, key=lambda entry: (
                -(longest_matches[entry.id] if entry.entry_type not in ("pc_identity", "npc_profile") else 0),
                -primary_matches[entry.id],
                entry.id not in rag_ids,
                entry.entry_type in ("pc_identity", "npc_profile"),
                entry.entry_type == "npc_profile" and entry.id not in present_profiles,
                rag_rank.get(entry.id, len(rag_rank)),
                -entry.priority,
            ))
            seen_relationships = set()
            for entry in ranked_lore:
                if (entry.id in prior_context_ids and not
                        (primary_matches[entry.id] or entry.id in rag_ids
                         or entry.id in present_profiles)):
                    continue
                source = next((prefix for prefix in ("_kg_rel_", "_npc_rel_")
                               if entry.id.startswith(prefix)), "")
                if source and entry.entry_type == "npc_relationship" and len(entry.keys) == 2:
                    head, separator, details = entry.content.partition(":")
                    names = tuple(entry.keys)
                    if (separator and names[0] != names[1]
                            and head.replace(" ", "") == f"{names[0]}→{names[1]}".replace(" ", "")):
                        reverse = (source, names[::-1], details, entry.visibility)
                        if reverse in seen_relationships:
                            continue
                        seen_relationships.add((source, names, details, entry.visibility))
                preview.append(entry)
            scene_kg = next((entry for entry in preview if entry.id == scene_kg_id), None)
            scene_desc = next((entry for entry in preview
                               if loc_id and entry.id == f"_loc_desc_{loc_id}"
                               and entry.entry_type == "location_desc"), None)
            if scene_kg and scene_desc:
                description = scene_desc.content.partition("\n")[2].strip()
                kg_content = scene_kg.content[:400 if len(scene_kg.content) <= 400 else 150]
                if description and description in kg_content:
                    without_duplicate = [entry for entry in preview if entry.id != scene_desc.id]
                    if scene_kg in without_duplicate[:5]:
                        preview = without_duplicate
            for entry in preview[:5]:
                title = getattr(entry, 'comment', '') or ', '.join(getattr(entry, 'keys', [])[:2])
                full_content = getattr(entry, 'content', '') or ''
                preview_length = (len(full_content) if (entry.visibility == "public"
                                  or (unified and entry.visibility == "world"))
                                  and len(full_content) <= 400
                                  and (primary_matches[entry.id] or rag_rank.get(entry.id, 2) < 2
                                       or entry.id == scene_kg_id)
                                  else 150)
                content = full_content[:preview_length]
                if content:
                    ctx["lore_preview_ids"].append(entry.id)
                    if (entry.id in (scene_kg_id, f"_loc_desc_{loc_id}")
                            and scene_detail and location_description[:300] in content):
                        scene_detail_covered = True
                    suffix = "（摘要已截断）" if len(full_content) > preview_length else ""
                    visibility = entry.visibility
                    if visibility == "hidden":
                        visibility += "（已发现）" if entry.id in state.get("pc_discovered_lore", []) else "（未发现）"
                    lore_texts.append(f"- {title} [entry_id={entry.id}, visibility={visibility}]: {content}{suffix}")
            if lore_texts:
                hint_parts.append("背景资料（仅供推演，不自动代表角色已知；未截断条目已提供全文；仅在摘要已截断且需要更多细节时，才用 query_lorebook 的 entry_id 读取全文）：\n" + "\n".join(lore_texts))
        if scene_detail_covered:
            parts[0] = parts[0].replace(scene_detail, "", 1)

        ctx["lore_candidate_ids"] = []
        if action_text and self.prompt_builder.lorebook:
            action_text = self.prompt_builder.lorebook._normalize_query_dates(action_text)
            partial_candidates = []
            activated_ids = {entry.id for entry in activated_lore}
            excluded_ids = self.prompt_builder._excluded_lore_ids(state)
            for term in (action_text, *keywords):
                if len(partial_candidates) >= 7:
                    break
                for entry_id in self.prompt_builder.lorebook.partial_keyword_candidates(
                    term, activated_ids, excluded_ids, max_entries=7,
                ):
                    if entry_id not in partial_candidates:
                        partial_candidates.append(entry_id)
            candidate_ids = []
            lorebook = self.prompt_builder.lorebook
            covered_positions = set()

            def key_spans(entry_id):
                primary, _ = lorebook._precomputed[entry_id]
                return [range(index, index + len(key)) for key in primary
                        if (index := action_text.find(key)) >= 0]

            def partial_order(entry_id):
                primary, _ = lorebook._precomputed[entry_id]
                canonical_position = action_text.find(primary[0]) if primary else -1
                fallback_position = min(
                    (span.start for span in key_spans(entry_id)), default=len(action_text),
                )
                return (entry_id not in action_text, canonical_position < 0,
                        canonical_position if canonical_position >= 0 else fallback_position)

            preview_positions = {
                position for entry_id in ctx["lore_preview_ids"]
                for span in key_spans(entry_id) for position in span
            }
            direct_action_candidates = sorted(
                (entry.id for entry in preview
                 if entry.id not in ctx["lore_preview_ids"] and not entry.id.startswith("_")
                 and primary_matches[entry.id]
                 and any(any(position not in preview_positions for position in span)
                         for span in key_spans(entry.id))),
                key=partial_order,
            )
            for entry_id in direct_action_candidates:
                if len(candidate_ids) >= 3:
                    break
                spans = key_spans(entry_id)
                if any(any(position not in covered_positions | preview_positions for position in span)
                       for span in spans):
                    candidate_ids.append(entry_id)
                    covered_positions.update(position for span in spans for position in span)

            ordered_partial = sorted(partial_candidates, key=partial_order)
            for entry_id in ordered_partial:
                if len(candidate_ids) >= 3:
                    break
                primary, _ = lorebook._precomputed[entry_id]
                canonical_position = action_text.find(primary[0]) if primary else -1
                spans = ([range(canonical_position, canonical_position + len(primary[0]))]
                         if canonical_position >= 0 else key_spans(entry_id))
                if (entry_id not in ctx["lore_preview_ids"] and not entry_id.startswith("_")
                        and any(any(position not in covered_positions for position in span)
                                for span in spans)):
                    candidate_ids.append(entry_id)
                    covered_positions.update(position for span in spans for position in span)
            remaining = "".join(" " if index in covered_positions else character
                                for index, character in enumerate(action_text))
            content_matches = []
            content_phrases = [
                (match.start() + offset, match.group()[offset:offset + 6])
                for match in re.finditer(r"[\u3400-\u9fff]{6,}", action_text)
                for offset in range(len(match.group()) - 5)
                if any(position not in covered_positions
                       for position in range(match.start() + offset, match.start() + offset + 6))
            ]
            for entry in lorebook.entries:
                if (not entry.enabled or not entry.content or entry.id.startswith("_")
                        or entry.id in excluded_ids or entry.id in ctx["lore_preview_ids"]
                        or entry.id in candidate_ids):
                    continue
                searchable = lorebook._normalize_query_dates(
                    " ".join([entry.comment, *entry.keys, entry.content]).lower()
                )
                for block in SequenceMatcher(None, remaining, searchable, autojunk=False).get_matching_blocks():
                    if block.size >= 4 and re.fullmatch(r"[\u3400-\u9fff]+", remaining[block.a:block.a + block.size]):
                        content_matches.append((block.a, -block.size, -entry.priority, entry.id, False))
                if (content_phrases and (entry.visibility != "hidden"
                        or entry.id in state.get("pc_discovered_lore", []))):
                    content = lorebook._normalize_query_dates(entry.content.lower())
                    for start, phrase in content_phrases:
                        if phrase in content:
                            content_matches.append((start, -len(phrase), -entry.priority, entry.id, True))
                            break
            for start, negative_size, _, entry_id, overlapping in sorted(content_matches):
                if len(candidate_ids) >= 3:
                    break
                span = range(start, start - negative_size)
                if (entry_id not in candidate_ids
                        and (sum(position not in covered_positions for position in span) >= 4
                             or overlapping and any(position not in covered_positions for position in span))):
                    candidate_ids.append(entry_id)
                    covered_positions.update(span)
            for entry_id in partial_candidates:
                if len(candidate_ids) >= 3:
                    break
                if entry_id not in candidate_ids:
                    candidate_ids.append(entry_id)
            rag_ids = ctx.get("lore_ids_from_rag") or set()
            preview_by_id = {entry.id: entry for entry in preview}
            rag_order = ctx.get("rag_lore_order") or [entry.id for entry in preview]
            rag_candidates = [entry_id for entry_id in rag_order
                              if (entry := preview_by_id.get(entry_id)) and entry_id in rag_ids
                              and entry_id not in ctx["lore_preview_ids"]
                              and entry_id not in excluded_ids and entry.enabled and entry.content]
            action_candidates = [entry.id for entry in preview
                                 if entry.id not in ctx["lore_preview_ids"]
                                 and entry.id not in excluded_ids and entry.content
                                 and primary_matches[entry.id]]
            long_aliases = [
                (len(key), entry.priority, entry.id, action_text.find(key))
                for entry in lorebook.entries
                if entry.enabled and entry.content and entry.id not in excluded_ids
                for key in lorebook._precomputed[entry.id][0]
                if len(lorebook._CJK_RE.findall(key)) >= 6 and key in action_text
            ]
            named_ids = []
            matched_positions = set()
            for key_length, _, entry_id, start in sorted(long_aliases, reverse=True):
                span = range(start, start + key_length)
                if any(position in matched_positions for position in span):
                    continue
                matched_positions.update(span)
                if entry_id not in ctx["lore_preview_ids"] and entry_id not in candidate_ids:
                    named_ids.append(entry_id)
                if len(named_ids) >= 2:
                    break
            candidate_ids[:0] = named_ids
            asks_question = (action_text.rstrip().endswith(("?", "？"))
                             or any(word in action_text for word in
                                     ("如何", "为何", "为什么", "怎么", "什么", "是否", "谁", "哪里")))
            investigates = any(word in action_text for word in ("调查", "核实", "核对", "了解"))
            clauses = re.split(r"、|以及", action_text)
            if (2 <= len(clauses) <= 3
                    and (asks_question or any(verb in action_text for verb in
                                              ("调查", "核实", "核对", "了解"))
                         or action_text.lstrip().startswith(("询问", "请询问"))
                         or (len(clauses) == 3 and "询问" in action_text))):
                for clause in clauses:
                    if "询问" in clause:
                        clause = clause.split("询问", 1)[1]
                    subject = self._plan_reply_subject({"_plan_reply": clause})
                    if not subject:
                        continue
                    matches = lorebook.query(keyword=subject, excluded_ids=excluded_ids)["entries"]
                    if matches:
                        entry_id = matches[0]["id"]
                        if entry_id not in candidate_ids and entry_id not in ctx["lore_preview_ids"]:
                            candidate_ids.append(entry_id)
            if asks_question or (investigates and len(candidate_ids) < 3):
                question_matches = lorebook.query(keyword=action_text, excluded_ids=excluded_ids)
                question_limit = (4 if partial_candidates else 3) if asks_question else 3
                for result in question_matches["entries"]:
                    if len(candidate_ids) >= question_limit:
                        break
                    entry_id = result["id"]
                    if entry_id not in candidate_ids and entry_id not in ctx["lore_preview_ids"]:
                        candidate_ids.append(entry_id)
            elif len(candidate_ids) < 3 and not action_candidates:
                action_matches = lorebook.query(keyword=action_text, excluded_ids=excluded_ids)
                if action_matches.get("approximate"):
                    action_results = action_matches["entries"]
                    if candidate_ids:
                        action_results = action_results[:1]
                    for result in action_results:
                        entry_id = result["id"]
                        if (entry_id not in candidate_ids and entry_id not in ctx["lore_preview_ids"]
                                and not entry_id.startswith("_")):
                            candidate_ids.append(entry_id)
                            if len(candidate_ids) >= 2:
                                break
            direct_limit = 5 if rag_candidates else 6
            available_scene_entries = {
                entry.id for entry in self.prompt_builder.lorebook.entries
                if entry.enabled and entry.content and entry.id not in excluded_ids
            }
            scene_ids = [f"_kg_loc_{loc_id}"] if loc_id else []
            for npc_id in ctx.get("present_npc_ids") or []:
                kg_id = f"_kg_npc_{npc_id}"
                profile_id = f"_npc_profile_{npc_id}"
                scene_ids.append(kg_id if kg_id in available_scene_entries else profile_id)
            scene_candidates = [entry_id for entry_id in scene_ids
                                if entry_id in available_scene_entries
                                and entry_id not in ctx["lore_preview_ids"]
                                and entry_id not in candidate_ids]
            scene_reserve = int(bool(scene_candidates)) + sum(
                lorebook.primary_key_match_count(entry_id, action_text) > 0
                for entry_id in scene_candidates[1:3]
            )
            for entry_id in ordered_partial:
                if len(candidate_ids) >= direct_limit - scene_reserve:
                    break
                if entry_id in candidate_ids or entry_id in ctx["lore_preview_ids"] or entry_id.startswith("_"):
                    continue
                spans = key_spans(entry_id)
                if not any(any(position not in covered_positions for position in span) for span in spans):
                    continue
                candidate_ids.append(entry_id)
                covered_positions.update(position for span in spans for position in span)
            for entry_id in action_candidates:
                if len(candidate_ids) >= direct_limit - scene_reserve:
                    break
                primary = lorebook._precomputed[entry_id][0]
                if (entry_id not in candidate_ids and not entry_id.startswith("_")
                        and primary and primary[0] in action_text):
                    candidate_ids.append(entry_id)
            discovered_lore = set(state.get("pc_discovered_lore") or [])
            secondary_matches = []
            covered_subject_positions = preview_positions | {
                position for entry_id in candidate_ids
                for span in key_spans(entry_id) for position in span
            }
            for entry in lorebook.entries:
                if (not entry.enabled or not entry.content or entry.id.startswith("_")
                        or entry.id in excluded_ids or entry.id in ctx["lore_preview_ids"]
                        or entry.selective_logic not in ("AND_ANY", "AND_ALL")
                        or entry.visibility == "hidden" and entry.id not in discovered_lore):
                    continue
                _, secondary = lorebook._precomputed[entry.id]
                matches = [key for key in secondary
                           if len(lorebook._CJK_RE.findall(key)) >= 3 and key in action_text
                           and any(position not in covered_subject_positions
                                   for position in range(action_text.find(key),
                                                         action_text.find(key) + len(key)))]
                if matches:
                    longest = max(matches, key=len)
                    secondary_matches.append((action_text.find(longest), -len(longest),
                                              -entry.priority, entry.id))
            secondary_ids = []
            secondary_limit = min(2, max(0, direct_limit - scene_reserve - len(named_ids)))
            for _, _, _, entry_id in sorted(secondary_matches):
                if len(secondary_ids) >= secondary_limit:
                    break
                if entry_id not in candidate_ids:
                    secondary_ids.append(entry_id)
            if secondary_ids:
                candidate_ids[len(named_ids):len(named_ids)] = secondary_ids
                del candidate_ids[direct_limit - scene_reserve:]
            for start, negative_size, _, entry_id, overlapping in sorted(content_matches):
                if len(candidate_ids) >= direct_limit - scene_reserve:
                    break
                span = range(start, start - negative_size)
                if (entry_id not in candidate_ids
                        and (sum(position not in covered_positions for position in span) >= 4
                             or overlapping and any(position not in covered_positions for position in span))):
                    candidate_ids.append(entry_id)
                    covered_positions.update(span)
            for entry_id in scene_candidates[:3]:
                if len(candidate_ids) >= direct_limit:
                    break
                candidate_ids.append(entry_id)
            for entry_id in action_candidates:
                if len(candidate_ids) >= direct_limit:
                    break
                if entry_id not in candidate_ids:
                    candidate_ids.append(entry_id)
            for entry_id in scene_candidates[3:]:
                if len(candidate_ids) >= direct_limit:
                    break
                if entry_id not in candidate_ids:
                    candidate_ids.append(entry_id)
            for entry_id in rag_candidates:
                if len(candidate_ids) >= 6:
                    break
                if entry_id not in candidate_ids:
                    candidate_ids.append(entry_id)
            for entry_id in action_candidates:
                if len(candidate_ids) >= 6:
                    break
                if entry_id not in candidate_ids:
                    candidate_ids.append(entry_id)
            for entry_id in ctx.get("event_lore_candidate_ids", [])[:2]:
                if len(candidate_ids) >= 6:
                    break
                if entry_id not in candidate_ids and entry_id not in ctx["lore_preview_ids"]:
                    candidate_ids.append(entry_id)
            ctx["lore_candidate_ids"] = candidate_ids
            if candidate_ids:
                hint_parts.append("待核实资料（仅提供候选 ID，不代表主角知情；需要时用 query_lorebook 的 entry_id 查询）：\n"
                                  + "\n".join(f"- entry_id={entry_id}" for entry_id in candidate_ids))
                if unified:
                    entries_by_id = {entry.id: entry for entry in lorebook.entries}
                    candidate_lines = []
                    for entry_id in candidate_ids:
                        entry = entries_by_id[entry_id]
                        title = re.sub(r"\s+", " ", entry.comment or ", ".join(entry.keys[:2])).strip()[:60]
                        visibility = entry.visibility
                        if visibility == "hidden":
                            visibility += ("（已发现）" if entry_id in state.get("pc_discovered_lore", [])
                                           else "（未发现）")
                        candidate_lines.append(f"- {entry_id}"
                                               + (f"；标题={title}" if title else "")
                                               + f"；visibility={visibility}")
                    hint_parts.append("幕后候选索引（仅标题与可见性，不含正文，不代表主角知情）：\n"
                                      + "\n".join(candidate_lines))
            exposed_ids = set(ctx["lore_preview_ids"] + candidate_ids)
            if any(entry_id not in exposed_ids for entry_id in partial_candidates):
                hint_parts.append(
                    "另有条件匹配的候选资料未列出；如需全面核对，"
                    "用 query_lorebook 按人名、组织名等 keyword 检索，并通过 page 翻页。"
                )
            if preview:
                unlisted_count = sum(entry.id not in exposed_ids and primary_matches[entry.id] > 0
                                     for entry in preview)
                if unlisted_count:
                    hint_parts.append(
                        f"另有{unlisted_count}条直接匹配的资料未列出；如需全面核对，"
                        "用 query_lorebook 按人名、组织名等 keyword 检索，并通过 page 翻页。"
                    )

        # 4. Event sections (事件/后果提示)
        event_sections = ctx.get("event_sections", {})
        if event_sections:
            for section_name, section_text in event_sections.items():
                if section_text:
                    text = str(section_text)
                    if len(text) > 2000:
                        text = text[:2000].rsplit("\n", 1)[0] + "\n（后续事件条目已省略）"
                    hint_parts.append(f"{section_name}：{text}")

        dice_hints = []
        for dice in ctx.get("dice_dicts", [])[:5]:
            label = f"{dice.get('source_label', '骰子')}={dice.get('total', '')}（{dice.get('range_label', '')}）"
            if dice.get("candidate_event_id"):
                dice_hints.append(f"候选：{label}；仅事件 {dice['candidate_event_id']} 在 {dice.get('candidate_fire_time', '触发时间')} 后发生时生效")
            elif dice.get("candidate_destination"):
                dice_hints.append(f"候选：{label}；仅实际抵达行动所指目的地时生效")
            else:
                dice_hints.append(label)
        if dice_hints:
            hint_parts.append("机制骰子（候选结果未结算）：\n" + "\n".join(dice_hints))

        candidate_events = ctx.get("triggered_events") or []
        if candidate_events:
            event_lines = [
                f"- {event.get('fire_time', '')}: {event.get('description') or event.get('event_id', '')}"
                for event in candidate_events
            ]
            hint_parts.append(
                f"预估行动窗口终点：{ctx.get('new_time', '')}（不是已结算时间）\n"
                "候选定时事件（仅在行动实际推进到触发时间后才发生，未到时间不得提前叙述；"
                "不代表角色已知）：\n" + "\n".join(event_lines)
            )

        conditional_events = ctx.get("conditional_event_candidates") or []
        if conditional_events:
            event_lines = [
                f"- {event.get('fire_time', '')}: {event.get('description') or event.get('event_id', '')}"
                for event in conditional_events
            ]
            hint_parts.append(
                "待验证的条件事件（前序事件或玩家行动可能改变触发条件；仅条件满足且推进到"
                "触发时间后才发生，不能当作确定结果或角色已知）：\n" + "\n".join(event_lines)
            )

        # 5. Pacing/tone
        pacing = state.get("pacing_state", {})
        if pacing:
            tension = pacing.get("tension", 50)
            trend = pacing.get("trend", "stable")
            hint_parts.append(f"节奏：张力{tension}/100 趋势{trend}")

        if hint_parts:
            parts.append("<context_hints>\n" + "\n".join(hint_parts) + "\n</context_hints>")

        stages = ("plot", "env", "char", "compose", "world", "npc") if unified else ("plot", "env", "char", "compose")
        instructions = self._format_agent_instructions(ctx, stages)
        if instructions:
            parts.append(instructions)
        parts.append(f"<player_action>\n{player_action.get('text', '')}\n</player_action>")
        return "\n\n".join(parts)

    async def _run_foreground_agent(self, ctx: dict, route: dict, player_action: dict,
                                     *, streaming: bool = False):
        """Foreground agent: autonomous retrieval + narrative writing.

        When *streaming* is True, operates as an async generator that yields
        intermediate tool-call / agent-done events.  The final (narrative, records)
        are written into the returned _AgentResult.
        """
        system = self._build_foreground_system()
        user = self._build_foreground_context(ctx, player_action)
        messages = [{"role": "user", "content": user}]

        # 动态裁剪前台工具集
        tools = list(FOREGROUND_TOOLS_SCHEMA)

        # 无骰子设定去掉 roll_dice
        dice_enabled = self.script.get("settings", {}).get("dice_check", {}).get("default_enabled", True)
        if not dice_enabled:
            tools = [t for t in tools if t["function"]["name"] != "roll_dice"]

        # F2: 前台只读工具缓存（本轮有效）
        _fg_cache: dict[str, str] = {}
        _FG_CACHEABLE = frozenset({
            "recall_history", "query_lorebook", "query_npc_history",
            "check_inventory", "get_npc_attitude",
        })

        async def _dispatch(name: str, args: dict) -> str:
            if name in _FG_CACHEABLE:
                cache_key = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                if cache_key in _fg_cache:
                    logger.info("[缓存命中] %s", name)
                    return _fg_cache[cache_key]
                result = await self._dispatch_foreground_tool(ctx, name, args)
                if not _tool_result_failed(result):
                    _fg_cache[cache_key] = result
                return result
            return await self._dispatch_foreground_tool(ctx, name, args)

        holder = _AgentResult()
        async for event in self._agent_loop(
            messages, system, tools, _dispatch,
            max_rounds=7, label="前台叙事",
            stage_key="narrative", max_tokens=8192,
            result_holder=holder,
        ):
            if streaming:
                yield event
        # When not streaming, caller accesses holder directly (no yield).
        # Tag holder onto the last yield-cycle for the pipeline to read.
        yield {"type": "_fg_result", "holder": holder}

    def _build_foreground_system(self) -> str:
        """Stable segment (frozen) + dynamic segment (rebuilt each turn)."""
        # 稳定段：首轮冻结
        if self._stable_prefix is None:
            from engine.prompt_loader import PromptLoader
            self._stable_prefix = PromptLoader.get().render_system(
                "agentic_foreground",
                world_background=(self.script.get("world_background", "") or "")[:500],
                player_name=self.current_state.get("player", {}).get("name", ""),
            )

        # 动态段：每轮重建
        dynamic = self._build_foreground_dynamic_section()
        return self._stable_prefix + "\n\n" + dynamic if dynamic else self._stable_prefix

    def _build_foreground_dynamic_section(self) -> str:
        """每轮变化的上下文，追加到 system prompt 末尾。"""
        parts = []
        state = self.current_state
        player = state.get("player", {})

        # 角色身份
        bio = player.get("bio", "")
        if bio:
            parts.append(f"角色身份：{bio[:120]}")
        personality = player.get("personality", "")
        if personality:
            parts.append(f"角色性格：{personality[:100]}")
        goal = state.get("long_term_goal", "") or player.get("long_term_goal", "")
        if goal:
            parts.append(f"长期目标：{goal[:120]}")

        # 当前 tone
        an = getattr(self, 'authors_note', '')
        if an:
            parts.append(f"[创作指令] {an[:200]}")

        # #5 玩家建模提示
        model_hint = self.player_model.hint_for_agent()
        if model_hint:
            parts.append(model_hint)

        return "\n".join(parts)

    def _record_narrative_facts(self, narrative: str, ctx: dict):
        """Cache settled narration for recall_history fallback."""
        if not narrative:
            return
        if not hasattr(self, '_narrative_buffer'):
            self._narrative_buffer = []
        self._narrative_buffer.append({
            "turn": self.turn_number,
            "text": narrative[:1000],
        })

    # ================================================================
    # Background agent -- state settlement
    # ================================================================

    def _dispatch_settlement_tool(self, name: str, args: dict, ctx: dict | None = None) -> str:
        """Execute a consolidated settlement tool. Routes to existing validators."""
        if name == "consult_rules":
            return self._handle_consult_rules(args, ctx)
        if name == "write_narrative" and ctx is not None:
            text = _clean_narrative_text(args.get("text") or "", ctx)
            args["text"] = text
            if ctx.get("_unified_narrative_output"):
                text = self._prepare_unified_narrative(text, ctx)
            if not text:
                return json.dumps({"error": "叙事不能为空"}, ensure_ascii=False)
            if self._narrative_quotes_private_script(text):
                return json.dumps({"error": "叙事逐字复述了活跃剧本节点的幕后底稿，请依据玩家可见线索重写。"},
                                  ensure_ascii=False)
            if self._narrative_reveals_pending_consequence(ctx, text):
                return json.dumps({"error": "叙事提前揭露了尚未触发的后果，请重写。"}, ensure_ascii=False)
            ctx["_finalize_narrative"] = text
            ctx["_narrative_timed_event_ids"] = args.get("event_ids", [])
            ctx["_narrative_conditional_events"] = ctx.get("_conditioned_event_keys", [])
            return "叙事修订已记录；此前结算将撤销，请按修订后的叙事重新结算状态、选项和摘要。"
        if name == "update_state":
            result = self._handle_update_state(args, ctx, prepare_event_dice=True)
            if ctx is not None and not _tool_result_failed(result):
                feedback = (self._timed_event_revision_feedback(ctx)
                            or self._conditional_event_revision_feedback(ctx, ctx.get("_finalize_narrative", "")))
                if feedback:
                    result += f" | 请先修订叙事再结束结算: {feedback}"
                if self._map_travel_revision_feedback(ctx, ctx.get("_finalize_narrative", "")):
                    result += " | 地图旅行叙事与位置结算矛盾；请核对位置变更，或调用 write_narrative 修订叙事并重新结算"
            return result
        if name == "manage_npcs":
            return self._handle_manage_npcs(args)
        if name == "add_choices":
            return self._handle_add_choices(args)
        if name == "set_turn_meta":
            return self._handle_set_turn_meta(args)
        return f"未知工具: {name}"

    # ── update_state: 拆分到原有 _run_state_tool 验证 ──

    _UPDATE_STATE_RES_KEYS = ("state_changes", "variable_changes", "inventory_changes", "activate_states", "deactivate_states", "game_over")
    _UPDATE_STATE_SPATIAL_KEYS = ("location_change", "reveal_locations", "npc_location_changes", "room_changes", "npc_contact_changes", "scene_details")

    def _validate_variable_changes(self, args: dict, ctx: dict | None = None) -> str:
        changes = args.get("variable_changes", [])
        if not isinstance(changes, list):
            return "错误: variable_changes 必须是数组"
        entries = [("state_changes", index, change) for index, change in enumerate(args.get("state_changes", []))
                   if isinstance(change, dict) and change.get("type") == "variable"]
        entries.extend(("variable_changes", index, change) for index, change in enumerate(changes))
        for field, index, change in entries:
            prefix = f"错误: {field}[{index}]"
            if not isinstance(change, dict):
                return f"{prefix} 必须是变量变更对象"
            variable_id = change.get("id")
            if not isinstance(variable_id, str) or variable_id not in self.script_variables.definitions:
                return f"{prefix} id 必须是当前分支已声明的变量；请用 consult_rules 查询，不可凭空创建"
            definition = self.script_variables.definitions[variable_id]
            variable_type = definition.get("type")
            operation = change.get("op", "set" if field == "state_changes" else None)
            allowed = {"number": ("set", "inc", "dec"), "bool": ("set", "toggle")}.get(variable_type, ("set",))
            if operation not in allowed:
                return f"{prefix} op 不支持此变量类型；允许: {', '.join(allowed)}"
            if operation == "set" and "value" not in change:
                return f"{prefix} set 必须提供 value"
            if operation != "toggle":
                value = change.get("value", 1)
                expected = {"number": int, "bool": bool, "string": str}.get(variable_type)
                if expected is not None and type(value) is not expected:
                    return f"{prefix} value 类型错误；{variable_type} 需要 {expected.__name__}"
        if entries:
            projected = self._project_state_tool_updates((ctx or {}).get("_accepted_state_updates", []))
            for field, index, change in entries:
                try:
                    self.script_variables.apply_op(projected, change["id"], change.get("op", "set"), change.get("value"))
                except (TypeError, ValueError, OverflowError):
                    return (f"错误: {field}[{index}] 当前变量值或定义不支持此操作；"
                            "请用 consult_rules 核对定义和现值，修正后重试；本次调用未接收")
        return ""

    def _handle_update_state(self, args: dict, ctx: dict | None = None, *, prepare_event_dice: bool = False,
                             defer_event_instructions: bool = False) -> str:
        contacts = args.get("npc_contact_changes", [])
        if not isinstance(contacts, list):
            return "错误: npc_contact_changes 必须是数组"
        for index, change in enumerate(contacts):
            if (not isinstance(change, dict) or not isinstance(change.get("npc_id"), str)
                    or not change["npc_id"].strip() or type(change.get("in_contact")) is not bool):
                return f"错误: npc_contact_changes[{index}] 须提供非空 npc_id 和布尔值 in_contact"
        error = self._validate_variable_changes(args, ctx)
        if error:
            return error
        results = []
        if "end_time" in args:
            time_result = self._run_state_tool("update_time", args, ctx)
            if time_result.startswith("错误:"):
                return time_result
            results.append(time_result)
        elif ctx is not None and ctx.get("old_time"):
            settled_time = next((update["end_time"] for update in reversed(
                ctx.get("_accepted_state_updates", [])) if update.get("end_time")), None)
            if settled_time:
                error = self._validate_end_time(settled_time, ctx, args.get("location_change"), args)
                if error:
                    return f"错误: {error}"
        res_args = {k: args[k] for k in self._UPDATE_STATE_RES_KEYS if k in args}
        if res_args:
            results.append(self._run_state_tool("update_resources", res_args))
        sp_args = {k: args[k] for k in self._UPDATE_STATE_SPATIAL_KEYS if k in args}
        if sp_args:
            spatial_result = self._run_state_tool("update_spatial", sp_args, ctx)
            if spatial_result.startswith("错误:"):
                return spatial_result
            results.append(spatial_result)
        if "world_property_changes" in args:
            results.append(self._run_state_tool("update_world", {"world_property_changes": args["world_property_changes"]}))
        result = " | ".join(results) if results else "无状态变更"
        if ctx is not None and (ctx.get("old_time") or "_accepted_state_updates" in ctx):
            ctx.setdefault("_accepted_state_updates", []).append(args)
            if prepare_event_dice and any(update.get("end_time") for update in ctx["_accepted_state_updates"]):
                prepared = self._prepare_unstaged_event_dice(ctx)
                if prepared:
                    result += " | 事件骰子准备: " + json.dumps({
                        "status": "event_dice_prepared", "prepared_event_dice": prepared,
                        "provisional": True, "state_update_accepted": True,
                        "hint": "本次状态更新已接收，无需重复；请核对固定骰子结果和候选事件。"
                                "若须改写，调用 write_narrative 后重新提交本阶段被撤销的结算；"
                                "骰子本轮不重掷，候选不代表事件已发生或主角知情。",
                    }, ensure_ascii=False)
            result += self._state_projection_feedback(ctx, acknowledge_event_instructions=not defer_event_instructions)
        return result

    def _state_projection_feedback(self, ctx: dict, *, acknowledge_event_instructions: bool = True) -> str:
        if not ctx.get("old_time") and "_accepted_state_updates" not in ctx:
            return ""
        updates = ctx.get("_accepted_state_updates", [])
        variable_ids = list(ctx.get("_state_feedback_variable_ids", []))
        property_ids = list(ctx.get("_state_feedback_world_property_ids", []))
        contact_ids = list(ctx.get("_state_feedback_npc_contact_ids", []))
        current_contacts = self.current_state.get("npc_contacts", {})
        if isinstance(current_contacts, dict):
            contact_ids.extend(current_contacts)
        contact_requests = {}
        player_spatial = ctx.get("_state_feedback_player_spatial", False)
        for update in updates:
            for change in update.get("npc_contact_changes", []):
                contact_ids.append(change["npc_id"])
                contact_requests[change["npc_id"]] = change["in_contact"]
            player_spatial = player_spatial or bool(
                update.get("location_change")
                or any(change.get("id") == "player" for change in update.get("room_changes", []))
                or any(change.get("target") in ("player.location", "player.current_room")
                       for change in update.get("state_changes", []))
            )
            variable_ids.extend(change["id"] for change in update.get("state_changes", [])
                                if change.get("type") == "variable")
            variable_ids.extend(change["id"] for change in update.get("variable_changes", []))
            property_ids.extend(change["id"] for change in update.get("world_property_changes", [])
                                if change.get("id") and not self._is_undeclared_object_property(change["id"]))
        variable_ids = list(dict.fromkeys(variable_ids))
        property_ids = list(dict.fromkeys(property_ids))
        contact_ids = list(dict.fromkeys(contact_ids))
        ctx["_state_feedback_variable_ids"] = variable_ids
        ctx["_state_feedback_world_property_ids"] = property_ids
        ctx["_state_feedback_npc_contact_ids"] = contact_ids
        ctx["_state_feedback_player_spatial"] = player_spatial
        result = ""
        spatial = {} if player_spatial else None
        if variable_ids or property_ids or player_spatial:
            projected_state = self._project_state_tool_updates(updates, spatial_feedback=spatial)
        if player_spatial:
            result += (" | " + _PLAYER_SPATIAL_FEEDBACK_LABEL
                       + json.dumps(spatial, ensure_ascii=False)
                       + "；ready 仅表示当前候选可通行，不代表已抵达；blocked 表示本次通行受阻；"
                         "none 表示无待处理移动，not_applied 表示移动未应用；"
                         "可按 rule_query 查完整地点声明，reason_truncated=true 时须续读完整原因；"
                         "补足条件后会重算原待移动，无需重复提交 location_change")
        if variable_ids:
            values = {variable_id: self.script_variables.get(projected_state, variable_id)
                      for variable_id in variable_ids}
            result += _candidate_value_feedback("variables", values)
        if property_ids:
            properties = projected_state.get("world_properties", {})
            values = {property_id: properties.get(property_id) for property_id in property_ids}
            missing = [property_id for property_id in property_ids if property_id not in properties]
            result += _candidate_value_feedback("world_properties", values, missing)
        if not ctx.get("old_time"):
            return result + (f" | 状态候选（未提交、未执行回合生命周期；已接受 {len(updates)} 次状态更新）；"
                             "已接受的更新无需重复提交")
        end_time = next((update["end_time"] for update in reversed(updates) if update.get("end_time")), None)
        event_instructions = []
        if contact_ids:
            projected, contact_state = self._project_interaction_state(ctx, event_instructions=event_instructions)
            contact_feedback = {"through": contact_state.get("game_time"), "contacts": {
                npc_id: {"requested_in_contact": contact_requests.get(npc_id),
                         "in_contact": self._npc_contact_is_current(npc_id, contact_state),
                         "block_reason": self._npc_contact_block_reason(npc_id, contact_state)}
                for npc_id in contact_ids
            }}
            encoded = json.dumps(contact_feedback, ensure_ascii=False)
            if len(encoded) > 4000:
                encoded = json.dumps({"through": contact_state.get("game_time"),
                                      "feedback_omitted": True, "contact_count": len(contact_ids)}, ensure_ascii=False)
            result += (" | " + _NPC_CONTACT_FEEDBACK_LABEL + encoded
                       + "；仅按已接收更新及已接受时间内的定时事件、日程、房间重算，不含完整回合生命周期；"
                         "声明已接收不等于接触成立，候选仍未提交。详情省略时用 "
                         "consult_rules(question=角色ID+接触, include_candidates=true) 查询；更新或撤销后重查。")
        else:
            projected = self._projected_events(ctx, event_instructions=event_instructions) if end_time else []
        if event_instructions or ctx.get("_known_event_instructions"):
            result += " | " + _EVENT_INSTRUCTION_FEEDBACK_LABEL + json.dumps({
                "through": end_time or ctx["old_time"], "instructions": event_instructions, "provisional": True,
                "hint": "仅供叙事 Agent 使用，不是玩家通知或角色知情证据；以本次候选为准，"
                        "未列出的旧指令已撤回。请按指令确认或修订叙事，不要重复提交已接收的状态更新。",
            }, ensure_ascii=False)
            if acknowledge_event_instructions:
                ctx["_known_event_instructions"] = copy.deepcopy(event_instructions)
        event_times = ", ".join(
            f"{event['event_id']} ({event['fire_time']})" for event in projected[:20]
        ) or "无"
        if len(projected) > 20:
            event_times += f"；另有 {len(projected) - 20} 项未列出"
        result += (f" | 本轮候选结算（截至 {end_time or ctx['old_time']}，未提交、非主角知情；"
                   f"已接受 {len(updates)} 次状态更新）：{event_times}；"
                   "已接受的更新无需重复调用；只有推进到触发时间且条件满足时才发生")
        unlocked = self._projected_conditional_events(ctx)
        ctx["_conditioned_event_keys"] = [
            (event["event_id"], event["fire_time"]) for event in unlocked
        ]
        if unlocked:
            event_times = ", ".join(
                f"{event['event_id']} ({event['fire_time']})" for event in unlocked
            )
            result += (f" | 状态投影显示条件事件可能触发: {event_times}；"
                       "仅推进到触发时间且条件仍满足时发生，不代表主角知情")
        return result

    # ── manage_npcs: 遍历 operations 分发 ──

    def _handle_manage_npcs(self, args: dict) -> str:
        results = []
        for op in args.get("operations", []):
            op_type = op.get("op", "")
            if op_type == "lookup":
                results.append(self._dispatch_npc_lookup(op.get("name", "")))
            elif op_type == "register":
                results.append(self._dispatch_npc_register(op))
            elif op_type == "attitude":
                npc_id = op.get("npc_id", "")
                result = self._run_npc_reaction_tool("update_npc_attitude", {
                    "npc_id": npc_id,
                    "dimension": op.get("dimension", ""),
                    "change": op.get("change", 0),
                    "reason": op.get("reason", ""),
                })
                results.append(result)
                if not _tool_result_failed(result):
                    reason = op.get("reason", "")
                    player_id = self.current_state.get("player", {}).get("id", "player")
                    self.narrative_graph.add_relation(
                        player_id, npc_id, f"attitude_{op.get('dimension', 'overall')}",
                        turn=self.turn_number, description=reason,
                    )
            elif op_type == "offscreen":
                results.append(self._run_state_tool("update_extended", {
                    "offscreen_npc_updates": [{
                        "name": op.get("name", ""),
                        "action": op.get("action", ""),
                        "location": op.get("location", ""),
                    }],
                }))
            elif op_type == "faction":
                results.append(f"已记录阵营 {op.get('faction', '?')} 声望变化: {op.get('delta', 0):+d}")
            elif op_type == "moral":
                results.append(f"已记录道德维度 {op.get('axis', '?')} 变化: {op.get('change', 0):+d}")
            elif op_type == "recruit":
                results.append(f"已记录 {op.get('npc_id', '?')} 加入队伍")
            elif op_type == "dismiss":
                results.append(f"已记录 {op.get('npc_id', '?')} 离队")
            else:
                results.append(f"未知操作类型: {op_type}")
        return " | ".join(results) if results else "无NPC操作"

    def _dispatch_npc_lookup(self, query: str) -> str:
        """NPC lookup by name or ID (same logic as old lookup_npc tool)."""
        query = query.strip()
        npcs = self.current_state.get("npcs", {})
        if query in npcs:
            npc = npcs[query]
            return json.dumps({"found": True, "id": query, "name": npc.get("name", query)}, ensure_ascii=False)
        for nid, ndata in npcs.items():
            if isinstance(ndata, dict) and query in (ndata.get("name", ""), nid):
                return json.dumps({"found": True, "id": nid, "name": ndata.get("name", nid)}, ensure_ascii=False)
        for npc in self.script.get("npcs", []):
            if query in (npc.get("name", ""), npc.get("id", "")):
                return json.dumps({"found": True, "id": npc["id"], "name": npc.get("name", ""), "source": "script"}, ensure_ascii=False)
        return json.dumps({"found": False}, ensure_ascii=False)

    def _dispatch_npc_register(self, op: dict) -> str:
        """Immediately register a new NPC so same-turn attitude changes work."""
        npc_id = op.get("id", "")
        npc_name = op.get("name", "")
        player_id = self.current_state.get("player", {}).get("id", "player")
        player_name = self.current_state.get("player", {}).get("name", "")
        if npc_id == player_id or (player_name and npc_name == player_name):
            logger.warning("跳过注册玩家角色为NPC: %s/%s", npc_id, npc_name)
            return f"跳过: {npc_name} 是玩家角色"
        if npc_id and npc_id in self.current_state.get("npcs", {}):
            logger.info("NPC %s 已存在，跳过重复注册", npc_id)
            return json.dumps({"registered": False, "id": npc_id, "reason": "already_exists"}, ensure_ascii=False)
        if npc_id:
            self._register_dynamic_npc({key: value for key, value in op.items() if key != "op"})
            # #4 叙事图谱：自动添加 NPC 实体
            self.narrative_graph.add_entity(npc_id, "npc", npc_name or npc_id)
        return json.dumps({"registered": True, "id": npc_id}, ensure_ascii=False)

    # ── add_choices: 批量 ──

    def _handle_add_choices(self, args: dict) -> str:
        results = []
        for choice in args.get("choices", []):
            results.append(self._run_choices_tool("add_choice", choice))
        return " | ".join(results) if results else "无选项"

    # ── set_turn_meta: summary + image ──

    def _handle_set_turn_meta(self, args: dict) -> str:
        parts = []
        summary = args.get("summary", "").strip()
        if summary:
            self._turn_summary_override = summary
            parts.append("摘要已设置: " + summary[:30])
        image_prompt = args.get("image_prompt", "").strip()
        if image_prompt:
            self._scene_image_prompt_override = {
                "prompt": image_prompt,
                "style": args.get("image_style", "realistic"),
            }
            parts.append("图片 prompt 已设置")
        return " | ".join(parts) if parts else "无元数据"

    # ── 合并工具 → 旧工具格式转换（供 _merge_state_tool_results 复用）──

    @staticmethod
    def _explode_update_state(args: dict) -> list[dict]:
        """Convert one update_state call into old-style tool calls for merging."""
        calls = []
        if "end_time" in args:
            calls.append({"name": "update_time", "args": {"end_time": args["end_time"]}})
        _res_keys = ("state_changes", "inventory_changes", "activate_states", "deactivate_states", "game_over")
        res_args = {k: args[k] for k in _res_keys if k in args}
        if args.get("variable_changes"):
            res_args["state_changes"] = [*res_args.get("state_changes", []),
                                         *({**change, "type": "variable"} for change in args["variable_changes"])]
        if res_args:
            calls.append({"name": "update_resources", "args": res_args})
        _sp_keys = ("location_change", "reveal_locations", "npc_location_changes", "room_changes", "npc_contact_changes", "scene_details")
        sp_args = {k: args[k] for k in _sp_keys if k in args}
        if sp_args:
            calls.append({"name": "update_spatial", "args": sp_args})
        if "world_property_changes" in args:
            calls.append({"name": "update_world", "args": {"world_property_changes": args["world_property_changes"]}})
        return calls

    @staticmethod
    def _explode_manage_npcs(args: dict) -> list[dict]:
        """Extract state-level operations from manage_npcs into old-style update_extended calls."""
        ext: dict = {}
        for op in args.get("operations", []):
            op_type = op.get("op", "")
            if op_type == "register":
                npc_entry = {k: op[k] for k in ("id", "name", "title", "bio", "personality",
                             "location", "trust", "affection", "fear") if k in op}
                ext.setdefault("new_npcs", []).append(npc_entry)
            elif op_type == "offscreen":
                ext.setdefault("offscreen_npc_updates", []).append({
                    "name": op.get("name", ""), "action": op.get("action", ""),
                    "location": op.get("location", ""),
                })
            elif op_type == "faction":
                ext.setdefault("faction_reputation_changes", []).append({
                    "faction_id": op.get("faction", ""), "change": op.get("delta", 0),
                    "reason": op.get("reason", ""),
                })
            elif op_type == "moral":
                ext.setdefault("moral_alignment_changes", []).append({
                    "axis": op.get("axis", ""), "change": op.get("change", 0),
                    "reason": op.get("reason", ""),
                })
            elif op_type == "recruit":
                ext.setdefault("recruit_companions", []).append(op.get("npc_id", ""))
            elif op_type == "dismiss":
                ext.setdefault("dismiss_companions", []).append(op.get("npc_id", ""))
            # lookup / attitude are handled during dispatch (buffered), not in merge
        return [{"name": "update_extended", "args": ext}] if ext else []

    def _build_settlement_system(self) -> str:
        """Load the settlement agent system prompt from YAML."""
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system("agentic_background")
        all_field_lines = [fdef["desc"] for fdef in self.prompt_builder._FIELD_DEFS.values()]
        return system + "\n\n字段参考：\n" + "\n".join(all_field_lines)

    def _known_locations_for_agent(self) -> list[tuple[str, str]]:
        state = self.current_state
        current = (state.get("player") or {}).get("location")
        location_ids = dict.fromkeys([current, *(state.get("visible_locations") or [])[::-1]])
        display_names = state.get("display_names") or {}
        locations = getattr(self, "_location_by_id", {})
        return [
            (location_id, display_names.get(location_id) or locations.get(location_id, {}).get("name") or location_id)
            for location_id in location_ids if location_id
        ][:20]

    def _map_travel_route_hint(self, destination: str) -> str:
        state = self.current_state or {}
        origin = (state.get("player") or {}).get("location")
        visible = state.get("visible_locations") or []
        if not state.get("location_connections") or origin not in visible or destination not in visible:
            return ""
        route = self._visible_location_route(origin, destination)
        if route:
            return ("当前已知地图路线（仅供推演，不保证通行或获准进入）："
                    + " → ".join(route) + "。")
        return "当前没有已知可见连接路线；不等于绝对不可达。"

    def _visible_location_route(self, origin: str | None, destination: str,
                                state: dict | None = None) -> list[str] | None:
        state = state if state is not None else self.current_state or {}
        connections = state.get("location_connections")
        if not isinstance(connections, dict) or not connections:
            return None
        visible = set(state.get("visible_locations") or [])
        if origin not in visible or destination not in visible:
            return None
        parents = {origin: None}
        queue = [origin]
        for location in queue:
            if location == destination:
                route = []
                while location is not None:
                    route.append(location)
                    location = parents[location]
                return list(reversed(route))
            neighbors = connections.get(location)
            for neighbor in neighbors if isinstance(neighbors, list) else []:
                if isinstance(neighbor, str) and neighbor in visible and neighbor not in parents:
                    parents[neighbor] = location
                    queue.append(neighbor)
        return None

    async def _run_background_agent(
        self, ctx: dict, narrative_text: str, plot_decision: str, player_action: dict,
        *, streaming: bool = False, skip_hints: str = "", route: dict | None = None,
    ):
        """Background agent: autonomously settle all state via tool calls.

        When *streaming*, yields intermediate events.  Final records are in the
        returned _AgentResult holder.
        """
        state = self.current_state
        system = self._build_settlement_system()

        action_text = player_action.get("text", "")
        user_parts = [f"<narrative>\n{narrative_text}\n</narrative>"]
        if plot_decision:
            user_parts.append(f"<plot_skeleton>\n{plot_decision}\n</plot_skeleton>")
        instructions = self._format_agent_instructions(ctx, ("world", "npc"))
        if instructions:
            user_parts.append(instructions)
        user_parts.append(f"<player_action>\n{action_text}\n</player_action>")
        player_inputs = self.prompt_builder.format_turn_player_inputs(ctx.get("_turn_player_inputs"))
        if player_inputs:
            user_parts.append(player_inputs)
        plan_reply_context = ctx.get("_review_plan_reply")
        if plan_reply_context:
            user_parts.append("<plan_reply_context>\n"
                              + json.dumps(plan_reply_context, ensure_ascii=False) + "\n</plan_reply_context>")
            system += (
                "\nplan_reply_context 是玩家回复时的计划、问题、选项与回复原文，仅供解释玩家行动。"
                "按原问题理解序号等简短回复；不要把整个计划、未确认部分或默认选项当作玩家已确认。"
            )
        old_time = ctx.get("old_time", "")
        if old_time:
            if ctx.get("is_system_opening"):
                user_parts.append(
                    f"<time_context>\n系统开场不消耗玩家行动时间；end_time 必须为 {old_time}。\n</time_context>"
                )
            else:
                user_parts.append(
                    f"<time_context>\n当前游戏时间: {old_time}\n"
                    "你是end_time的唯一决策者。根据叙事最后场景的时间输出绝对时间戳：\n"
                    "- 对话/观察/翻阅文件: 当前时间 +10~30分钟\n"
                    "- 常规互动/短途移动: 当前时间 +30分钟~2小时\n"
                    "- 长途旅行/大型战斗: 当前时间 +2~8小时\n"
                    "- 睡觉/过夜: 若叙事写到入睡那一刻则给入睡时间，若叙事写到醒来才给次日早晨\n"
                    f"格式示例: {old_time[:10] or '1970-01-01'}T10:00:00\n</time_context>"
                )
        # Bug 2 fix: 注入已知地点 ID 列表，防止 Agent 虚构地点
        known_locations = self._known_locations_for_agent()
        if known_locations:
            loc_lines = [f"  {lid}: {name}" for lid, name in known_locations]
            user_parts.append(
                "<known_locations>\n当前已发现地点ID（新发现地点须同次提交reveal_locations）：\n"
                + "\n".join(loc_lines) + "\n</known_locations>"
            )

        if skip_hints:
            user_parts.append(skip_hints)

        messages = [{"role": "user", "content": "\n\n".join(user_parts)}]

        # 按 route skip 标志裁剪后台工具集
        # 合并工具后，NPC态度是 manage_npcs 的子操作，不再单独移除工具
        # skip 标志通过 skip_hints 文本传递给 Agent
        unified_tools = _unified_tools()
        narrative_tool = next(tool for tool in unified_tools
                              if tool["function"]["name"] == "write_narrative")
        rules_tool = next(tool for tool in unified_tools if tool["function"]["name"] == "consult_rules")
        tools = [*CONSOLIDATED_SETTLEMENT_TOOLS, rules_tool, {
            **narrative_tool, "function": {
                **narrative_tool["function"],
                "description": "仅在玩家插话要求改写已展示叙事，或结算发现叙事必须修正时，提交完整的新叙事。",
            },
        }]
        ctx.setdefault("_finalize_narrative", narrative_text)

        settlement_snapshot = {
            "current_state": copy.deepcopy(self.current_state),
            "npc_reactions": copy.deepcopy(self._npc_reaction_tool_calls),
            "new_npcs": copy.deepcopy(self._new_npc_tool_calls),
            "graph": copy.deepcopy(self.narrative_graph.snapshot()),
            "accepted_state_updates": copy.deepcopy(ctx.get("_accepted_state_updates")),
            "conditioned_event_keys": copy.deepcopy(ctx.get("_conditioned_event_keys")),
            "conditional_event_revision": copy.deepcopy(ctx.get("_conditional_event_revision")),
            "npc_registry": copy.deepcopy((
                self.script.get("npcs", []), self._npc_by_id, self._npc_name_to_id,
                self.prompt_builder._npc_by_id, self.prompt_builder._npc_name_map,
            )),
        }
        tool_count = 0

        def dispatch(name, args):
            nonlocal tool_count
            previous_narrative = ctx.get("_finalize_narrative")
            try:
                result = self._dispatch_settlement_tool(name, args, ctx)
                if (name == "write_narrative" and not _tool_result_failed(result)
                        and ctx["_finalize_narrative"] != previous_narrative):
                    self.current_state = copy.deepcopy(settlement_snapshot["current_state"])
                    self._npc_reaction_tool_calls = copy.deepcopy(settlement_snapshot["npc_reactions"])
                    self._new_npc_tool_calls = copy.deepcopy(settlement_snapshot["new_npcs"])
                    self._choices_tool_calls = []
                    graph = copy.deepcopy(settlement_snapshot["graph"])
                    self.narrative_graph.nodes = graph["nodes"]
                    self.narrative_graph.edges = graph["edges"]
                    (self.script["npcs"], self._npc_by_id, self._npc_name_to_id,
                     self.prompt_builder._npc_by_id, self.prompt_builder._npc_name_map) = copy.deepcopy(
                        settlement_snapshot["npc_registry"])
                    self._turn_summary_override = ""
                    self._scene_image_prompt_override = None
                    for key, snapshot_key in (
                        ("_accepted_state_updates", "accepted_state_updates"),
                        ("_conditioned_event_keys", "conditioned_event_keys"),
                        ("_conditional_event_revision", "conditional_event_revision"),
                    ):
                        if settlement_snapshot[snapshot_key] is None:
                            ctx.pop(key, None)
                        else:
                            ctx[key] = copy.deepcopy(settlement_snapshot[snapshot_key])
                    ctx["_settlement_revision_index"] = tool_count
                    result += self._state_projection_feedback(ctx)
                return result
            finally:
                tool_count += 1

        holder = _AgentResult()
        async for event in self._agent_loop(
            messages, system, tools, dispatch,
            max_rounds=7, label="后台结算",
            stage_key="state", max_tokens=4096,
            result_holder=holder,
        ):
            if event.get("type") == "user_inject":
                ctx.setdefault("_turn_player_inputs", []).append({"message": event["message"]})
            if streaming:
                if event.get("type") == "tool_call" and event.get("tool_name") == "write_narrative":
                    event = {**event, "tool_args": {
                        key: value for key, value in event.get("tool_args", {}).items() if key != "text"
                    }}
                yield event
        revision_index = ctx.get("_settlement_revision_index")
        if revision_index is not None:
            for record in holder.records[:revision_index]:
                record["superseded"] = True
        feedback = (self._timed_event_revision_feedback(ctx)
                    or self._conditional_event_revision_feedback(ctx, ctx["_finalize_narrative"]))
        if feedback:
            raise RuntimeError(f"后台叙事未通过事件校验: {feedback}")
        if self._map_travel_revision_feedback(ctx, ctx["_finalize_narrative"]):
            raise MapTravelSettlementError()
        if ctx.get("old_time") and not any(
            update.get("end_time") for update in ctx.get("_accepted_state_updates", [])
        ):
            raise IncompleteAgentTurnError("后台结算缺少有效 end_time，本轮未保存，请重试")
        yield {"type": "_bg_result", "holder": holder}
        logger.info("[%s] 结算工具调用 %d 次", "background", len(holder.records))

    # ================================================================
    # Truncation detection (#11)
    # ================================================================

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        """保守检测叙事是否被 max_tokens 截断。"""
        text = (text or "").rstrip()
        if not text or len(text) < 200:
            return False
        # reflect 标签结尾不算截断
        if text.endswith("</reflect>"):
            return False
        last_char = text[-1]
        # 正常结尾字符
        if last_char in "。！？」）…\n\"'":
            return False
        return True

    # ================================================================
    # Audit helper
    # ================================================================

    def _audit_settlement(self, parsed: dict) -> None:
        """轻量审计：记录结算摘要到日志"""
        audit = []
        for sc in parsed.get("state_changes", []):
            audit.append(f"  {sc.get('target')}: {sc.get('op')} {sc.get('value')} ({sc.get('reason', '')})")
        for nc in parsed.get("npc_attitude_changes", []):
            audit.append(f"  NPC {nc.get('npc_id')}: {nc.get('dimension')} {nc.get('change', 0):+d}")
        for ch in parsed.get("choices", []):
            audit.append(f"  选项 {ch.get('id')}: {ch.get('text', '')[:30]} [{ch.get('risk', '')}]")
        if parsed.get("location_change"):
            audit.append(f"  位置: → {parsed['location_change']}")
        if parsed.get("end_time"):
            audit.append(f"  时间: → {parsed['end_time']}")
        if audit:
            logger.info("=== 结算审计 ===\n%s", "\n".join(audit))

    # ================================================================
    # Partial regen via agent (choices-only / state-only)
    # ================================================================

    async def _regen_partial_agent(self, stage: str, narrative: str, action_text: str,
                                   current_state: dict, streaming: bool = True):
        """Lightweight agent call to regenerate just choices or state.

        stage: "choices" or "state"
        """
        started_at = time.monotonic()
        # 通用上下文
        player = current_state.get("player", {})
        loc_id = player.get("location", "")
        loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id) if hasattr(self, '_location_by_id') else loc_id
        present_npcs = []
        npcs_state = current_state.get("npcs", {})
        for nid, ns in npcs_state.items():
            if isinstance(ns, dict) and ns.get("current_location") == loc_id:
                if stage == "choices":
                    if (not loc_id or ns.get("known", self._npc_by_id.get(nid, {}).get("known", True)) is False):
                        continue
                    if player.get("current_room") and ns.get("current_room") != player["current_room"]:
                        continue
                present_npcs.append(f"{ns.get('name', nid)}(ID:{nid})")
        attrs = []
        for k, v in player.items():
            if k not in ("name", "id", "location", "bio", "appearance", "personality") and isinstance(v, (int, float)):
                attrs.append(f"{k}={v}")

        state_context = (
            f"当前时间: {current_state.get('game_time', '')}\n"
            f"位置: {loc_name}({loc_id})\n"
            f"在场NPC: {', '.join(present_npcs) if present_npcs else '无'}\n"
            f"玩家属性: {', '.join(attrs[:15]) if attrs else '无'}"
        )
        unified_tools = _unified_tools()
        read_tools = [tool for tool in unified_tools
                      if tool["function"]["name"] in ("query_lorebook", "peek_upcoming_events", "consult_rules")]
        finalize = copy.deepcopy(next(tool for tool in unified_tools
                                      if tool["function"]["name"] == "finalize_turn"))
        node = self.world_tree.get_node(self.world_tree.active_node_id) or {}

        if stage == "choices":
            system = (
                "你是游戏结算引擎。以下是本轮已完成的叙事，请为玩家生成4个新的行动选项。\n"
                "涉及剧本设定或临近事件时，可先查询资料或预览事件。\n"
                "完整声明与现值可用 consult_rules 按类别查询；只读已提交状态，提出选项不等于执行或结算选项。\n"
                "调用 finalize_turn 工具提交选项和一句话摘要。不要修改叙事内容。\n"
                + state_context
            )
            finalize_fields = {"choices", "summary"}
            tools = read_tools + [finalize]
        elif stage == "state":
            system = (
                "你是游戏结算引擎。以下是本轮已完成的叙事，请分析叙事内容并结算状态变更。\n"
                "涉及剧本设定或时间推进时，可先查询资料或预览临近事件。\n"
                "声明机制与现值可用 consult_rules 按类别查询；状态校验失败时先核对再重试。\n"
                "variables、world_properties 可用 include_candidates=true 查询已接收更新的候选值；候选不含事件或回合生命周期。\n"
                "调用 update_state 和/或 manage_npcs 工具提交状态变更，然后调用 finalize_turn 完成。\n"
                "不要修改叙事或选项，只关注状态结算。\n"
                + state_context
            )
            instructions = self._format_agent_instructions(node, ("world", "npc"), state=current_state)
            if instructions:
                system += "\n\n" + instructions
            action_tools = [t for t in CONSOLIDATED_SETTLEMENT_TOOLS
                            if t["function"]["name"] in ("update_state", "manage_npcs")]
            finalize_fields = {"end_time", "state_changes", "inventory_changes",
                               "activate_states", "location_change", "reveal_locations",
                               "npc_operations", "summary"}
            finalize["function"]["parameters"]["required"] = []
            tools = read_tools + action_tools + [finalize]
        else:
            return
        parameters = finalize["function"]["parameters"]
        parameters["properties"] = {key: value for key, value in parameters["properties"].items()
                                    if key in finalize_fields}
        allowed_tools = {tool["function"]["name"] for tool in tools}

        async def partial_dispatch(name, args):
            if name not in allowed_tools:
                return json.dumps({"error": f"未开放工具: {name}"}, ensure_ascii=False)
            if name == "finalize_turn" and (not isinstance(args, dict) or set(args) - finalize_fields):
                return json.dumps({"error": "finalize_turn 包含非本阶段字段"}, ensure_ascii=False)
            return await self._dispatch_unified_tool(ctx, name, args)

        self._reset_stage45_tool_buffers()
        self._turn_summary_override = ""
        ctx = {"tool_results": [], "_finalize_narrative": narrative}
        if stage == "state":
            ctx["_accepted_state_updates"] = []
        user_msg = f"玩家行动: {action_text}\n\n叙事:\n{narrative}"
        turn_player_inputs = copy.deepcopy(node.get("_turn_player_inputs") or [])
        player_inputs = self.prompt_builder.format_turn_player_inputs(turn_player_inputs)
        if player_inputs:
            user_msg += "\n\n" + player_inputs
        holder = _AgentResult()

        agent_events = self._agent_loop(
            [{"role": "user", "content": user_msg}],
            system, tools, partial_dispatch,
            max_rounds=5, label=f"局部重生成({stage})",
            result_holder=holder,
        )
        try:
            async for event in agent_events:
                if streaming:
                    yield event
                if event.get("type") == "aborted":
                    if not streaming:
                        yield event
                    return
        finally:
            await agent_events.aclose()

        if getattr(self, "_abort_flag", False):
            yield {"type": "aborted", "label": f"局部重生成({stage})"}
            return
        if not any(record.get("name") == "finalize_turn"
                   and not record.get("deferred") and not _tool_result_failed(record.get("result"))
                   for record in holder.records):
            raise IncompleteAgentTurnError("局部Agent未完成结算，重生成失败，请重试")

        # 提取结果
        result = {"stage": stage, "_turn_player_inputs": turn_player_inputs + [
            {"message": entry["message"]} for entry in holder.user_injections
        ]}
        if stage == "choices":
            result["choices"] = list(self._choices_tool_calls)
            result["summary"] = self._turn_summary_override or ""
        elif stage == "state":
            state_calls = []
            for record in holder.records:
                if record.get("deferred") or _tool_result_failed(record.get("result")):
                    continue
                if record["name"] == "update_state":
                    state_calls.extend(self._explode_update_state(record["args"]))
                elif record["name"] == "manage_npcs":
                    state_calls.extend(self._explode_manage_npcs(record["args"]))
                elif record["name"] == "finalize_turn":
                    state_calls.extend(self._explode_update_state(record["args"]))
                    state_calls.extend(self._explode_manage_npcs({"operations": record["args"].get("npc_operations", [])}))
            if not state_calls and not self._npc_reaction_tool_calls:
                raise RuntimeError("状态结算为空，重生成失败")
            parsed = self._merge_state_tool_results(state_calls)
            self._merge_npc_reaction_tool_results(parsed)
            result["parsed_state"] = parsed

        lore_results = []
        for record in holder.records:
            if record["name"] != "query_lorebook":
                continue
            try:
                lore_results.append(json.loads(record["result"]))
            except (TypeError, ValueError):
                continue
        result["agent_trace"] = _public_agent_trace({
            "turn": self.turn_number,
            "mode": f"regen_{stage}",
            "user_injections": list(holder.user_injections),
            "rounds": [{
                "step": index + 1,
                "tool": record["name"],
                "args_summary": str(record.get("args", {}))[:100],
                "result_summary": str(record.get("result", ""))[:100],
                **({"duration_ms": record["duration_ms"]} if "duration_ms" in record else {}),
            } for index, record in enumerate(holder.records)],
            "total_tokens": {"prompt": holder.prompt_tokens, "completion": holder.completion_tokens},
            "tools_count": len(holder.records),
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "model_calls": holder.model_calls,
            "lore_queries": sum(record["name"] == "query_lorebook" for record in holder.records),
            "lore_hits": sum(len(value.get("entries", [])) for value in lore_results if isinstance(value, dict)),
        })
        yield {"type": "partial_regen_done", **result}

    # ================================================================
    # Pipeline entry (agentic)
    # ================================================================

    async def _execute_agentic_pipeline(self, ctx: dict, route: dict, player_action: dict,
                                        *, streaming: bool = False):
        """Agentic twin-loop pipeline. Async generator yielding the same items as
        _execute_pipeline: intermediate chunks (streaming) + a pipeline_result.
        """
        _start = time.monotonic()
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")
        ctx.setdefault("tool_results", [])
        self._reset_stage45_tool_buffers()
        # 后台 Agent 后处理 override 初始化
        self._turn_summary_override = ""
        self._scene_image_prompt_override = None

        logger.info("=" * 50)
        logger.info("AGENTIC 模式开始 — 行动: %s", action_text[:60])
        logger.info("=" * 50)

        # --- Foreground: retrieval + narration ---
        logger.info(">>> 前台叙事 Agent 启动")
        fg_holder: _AgentResult | None = None
        async for event in self._run_foreground_agent(ctx, route, player_action, streaming=streaming):
            if event.get("type") == "_fg_result":
                fg_holder = event["holder"]
            elif streaming:
                yield event

        narrative = (fg_holder.text if fg_holder else "").strip()
        fg_calls = fg_holder.records if fg_holder else []
        if not narrative:
            raise RuntimeError("Agentic 前台叙事为空（工具调用后未输出文本）")
        ctx["tool_results"].extend(fg_calls)
        logger.info(">>> 前台叙事完成 (%d字, %d次工具调用)", len(narrative), len(fg_calls))

        # 工具调用模式无法增量流式，叙事完成后整体下发一次
        if streaming:
            yield {"type": "text", "content": narrative}

        # NPC voice consistency (reuses the workflow path's post-check)
        pre_fix_narrative = narrative
        narrative = await self._maybe_fix_npc_voices(
            narrative, self.current_state, ctx.get("present_npc_ids"),
        )
        if streaming and narrative != pre_fix_narrative:
            yield {"type": "narrative_revised", "content": narrative}

        # --- Background: state settlement ---
        _skip_settlement = route.get("skip_state_settlement", False)
        _skip_npc = route.get("skip_npc_reaction", False)
        _skip_choices = route.get("skip_choices", False)

        bg_calls: list[dict] = []
        bg_holder: _AgentResult | None = None
        if _skip_settlement and _skip_npc and _skip_choices:
            logger.info(">>> 路由指示跳过全部后台结算")
        else:
            # 构造 skip hints 供后台 Agent 参考
            skip_notes: list[str] = []
            if _skip_settlement:
                skip_notes.append("本轮无需修改任何状态（skip_state_settlement=true）")
            if _skip_npc:
                skip_notes.append("本轮无需修改NPC态度（skip_npc_reaction=true）")
            if _skip_choices:
                skip_notes.append("本轮无需生成选项（skip_choices=true）")
            skip_hint_text = ""
            if skip_notes:
                skip_hint_text = "<skip_hints>\n" + "\n".join(skip_notes) + "\n</skip_hints>"

            # 后台 Agent 只需要少量字段，不做 deepcopy
            bg_ctx = {
                "action_text": ctx.get("action_text", ""),
                "old_time": ctx.get("old_time", ""),
                "present_npc_ids": ctx.get("present_npc_ids", []),
                "_map_travel_destination": self._map_travel_destination(player_action),
            }
            logger.info(">>> 后台结算 Agent 启动")
            async for event in self._run_background_agent(
                bg_ctx, narrative, "", player_action,
                streaming=streaming, skip_hints=skip_hint_text, route=route,
            ):
                if event.get("type") == "_bg_result":
                    bg_holder = event["holder"]
                elif streaming:
                    yield event

            bg_calls = bg_holder.records if bg_holder else []
            ctx["tool_results"].extend(bg_calls)
            logger.info(">>> 后台结算完成 (%d次工具调用)", len(bg_calls))
            revised = bg_ctx.get("_finalize_narrative", narrative).strip()
            if revised != narrative:
                narrative = await self._maybe_fix_npc_voices(
                    revised, self.current_state, ctx.get("present_npc_ids"),
                )
                if streaming:
                    yield {"type": "narrative_revised", "content": narrative}

        # Reset abort flag after pipeline completes
        if hasattr(self, '_abort_flag'):
            self._abort_flag = False

        # Merge settlement tool calls -> parsed (same shape as parse_split_v3)
        # Explode consolidated tools into old-style calls for _merge_state_tool_results
        state_calls = []
        for c in bg_calls:
            if c.get("superseded") or _tool_result_failed(c.get("result")):
                continue
            if c["name"] == "update_state":
                state_calls.extend(self._explode_update_state(c["args"]))
            elif c["name"] == "manage_npcs":
                state_calls.extend(self._explode_manage_npcs(c["args"]))
        parsed = self._merge_state_tool_results(state_calls)
        # NPC attitude + choices buffers
        self._merge_npc_reaction_tool_results(parsed)
        self._merge_choices_tool_results(parsed)
        if not parsed.get("choices") and not _skip_choices:
            parsed["choices"] = self._generate_context_choices()

        parsed["narrative"] = narrative
        # Keys consumed downstream by _apply_parsed_response (absent in agentic path)
        ctx.setdefault("plot_decision", "")
        ctx.setdefault("plot_reasoning", "")

        # Build reasoning trace from tool call records
        reasoning_parts = []
        for call in fg_calls:
            reasoning_parts.append(f"[工具] {call['name']}")
        for call in bg_calls:
            reasoning_parts.append(f"[结算] {call['name']}")
        _narrative_reasoning = "\n".join(reasoning_parts) if reasoning_parts else ""

        choices_count = len(parsed.get("choices", []))
        # Bug 4 fix: 计算所有状态变更类型，不仅是 state_changes
        state_changes = (
            len(parsed.get("state_changes", []))
            + len(parsed.get("activate_states", []))
            + len(parsed.get("deactivate_states", []))
            + len(parsed.get("inventory_changes", []))
        )
        npc_att = len(parsed.get("npc_attitude_changes", []))
        logger.info("=" * 50)
        logger.info("AGENTIC 完成 — 叙事%d字 | 选项%d | 状态变更%d | NPC态度%d | 工具调用%d+%d",
                    len(narrative), choices_count, state_changes, npc_att, len(fg_calls), len(bg_calls))
        logger.info("=" * 50)

        self._audit_settlement(parsed)

        # E1/E3: trace + performance for agentic pipeline
        _total_prompt = (fg_holder.prompt_tokens if fg_holder else 0) + (bg_holder.prompt_tokens if bg_holder else 0)
        _total_completion = (fg_holder.completion_tokens if fg_holder else 0) + (bg_holder.completion_tokens if bg_holder else 0)
        elapsed_ms = int((time.monotonic() - _start) * 1000)
        trace = {
            "turn": self.turn_number,
            "mode": "agentic",
            "plan": "",
            "reflection": "",
            "rounds": [],
            "user_injections": (list(fg_holder.user_injections) if fg_holder else [])
                               + (list(bg_holder.user_injections) if bg_holder else []),
            "total_tokens": {"prompt": _total_prompt, "completion": _total_completion},
            "narrative_length": len(narrative),
            "tools_count": len(fg_calls) + len(bg_calls),
            "duration_ms": elapsed_ms,
            "prepare_ms": ctx.get("prepare_ms"),
            "first_text_ms": ctx.get("first_text_ms") if streaming else None,
            "plan_wait_ms": (fg_holder.plan_wait_ms if fg_holder else 0)
                            + (bg_holder.plan_wait_ms if bg_holder else 0),
            "lore_activated_count": len(ctx.get("activated_lore", [])),
            "retrieval_timings_ms": dict(ctx.get("retrieval_timings_ms", {})),
            "lore_preview_ids": ctx.get("lore_preview_ids", []),
            "rag_history_hit_count": len(ctx.get("rag_history_hits") or []),
            "databank_hit_count": len(ctx.get("databank_hits") or []),
            "rag_lore_hit_count": len(ctx.get("lore_ids_from_rag") or []),
        }
        for i, record in enumerate(fg_calls + bg_calls):
            trace["rounds"].append({
                "step": i + 1,
                "tool": record["name"],
                "args_summary": str(record.get("args", {}))[:100],
                "result_summary": str(record.get("result", ""))[:100],
                **({"duration_ms": record["duration_ms"]} if "duration_ms" in record else {}),
                **({"superseded": True} if record.get("superseded") else {}),
            })
        if not hasattr(self, '_agent_traces'):
            self._agent_traces = []
        trace = _public_agent_trace(trace)
        ctx["agent_trace"] = trace
        self._agent_traces.append(trace)
        if len(self._agent_traces) > 10:
            self._agent_traces.pop(0)

        # 标记后处理已在 agentic 管线中完成
        ctx["_agentic_post_processed"] = True
        if self._turn_summary_override:
            ctx["turn_summary_override"] = self._turn_summary_override
        if self._scene_image_prompt_override:
            ctx["scene_image_prompt"] = self._scene_image_prompt_override

        yield {
            "type": "pipeline_result",
            "narrative": narrative,
            "parsed": parsed,
            "warnings": _warnings,
            "plot_decision": "",
            "plot_reasoning": "",
            "narrative_reasoning": _narrative_reasoning,
            "compose_msgs": [],
            "compose_sys": "",
            "proactive_hint": ctx.get("_proactive_hint", ""),
            "token_usage": {
                "prompt": _total_prompt,
                "completion": _total_completion,
                "total": _total_prompt + _total_completion,
            },
            "agent_trace": trace,
        }

    # ================================================================
    # Feedback-based regeneration
    # ================================================================

    async def regenerate_with_feedback(self, feedback: str) -> dict:
        """基于用户反馈重新生成当前轮叙事，不改变游戏状态。"""
        last_node = self.world_tree.get_node(self.world_tree.active_node_id) if self.world_tree else None
        if not last_node:
            return {"error": "没有可重生成的回合"}

        action_raw = last_node.get("player_action")
        action_text = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")

        original_narrative = last_node.get("ai_response", "")

        feedback_system = self._build_foreground_system()
        feedback_system += (
            f"\n\n## 用户反馈\n上一版叙事的问题：{feedback}\n"
            "请根据反馈重新写一版叙事。保持同样的事件和剧情走向，但改善用户指出的问题。"
        )

        user = (
            f"上一版叙事（需要修改）：\n{original_narrative[:2000]}\n\n"
            f"玩家行动：{action_text}\n\n"
            "请根据反馈重写叙事。"
        )
        messages = [{"role": "user", "content": user}]

        fg_holder = _AgentResult()
        async for _event in self._agent_loop(
            messages, feedback_system, FOREGROUND_TOOLS_SCHEMA,
            lambda name, args: self._dispatch_foreground_tool({}, name, args),
            max_rounds=5, label="反馈重写",
            result_holder=fg_holder,
        ):
            pass

        new_narrative = fg_holder.text
        if not new_narrative:
            return {"error": "重写失败"}

        # 更新世界树节点的叙事文本
        last_node["ai_response"] = new_narrative

        # 补充完整返回值，使调用方（process_action 早返回路径）拿到与正常管线一致的 dict
        state = self.current_state
        return {
            "ok": True,
            "narrative": new_narrative,
            "choices": last_node.get("choices_presented", []),
            "state": self._slim_snapshot(state),
            "node_id": self.world_tree.active_node_id if self.world_tree else "",
            "game_time": state.get("game_time", ""),
            "dice_rolls": [],
            "warnings": [],
            "feedback_regen": True,  # 标记这是反馈重生成
        }

    # ================================================================
    # Adaptive tools -- dynamic tool set based on game state (#3)
    # ================================================================

    def _build_adaptive_tools(self, ctx: dict) -> list[dict]:
        """Build tool list dynamically based on game state."""
        tools = list(_unified_tools())

        state = self.current_state
        inventory = state.get("inventory", [])

        # Unlock send_message if player has a communication device
        comm_items = ["电话", "对讲机", "无线电", "传呼机", "手机"]
        has_comm = any(
            any(ci in (it.get("item", "") if isinstance(it, dict) else str(it))
                for ci in comm_items)
            for it in inventory
        )
        if has_comm:
            tools.append({"type": "function", "function": {
                "name": "send_message",
                "description": "通过通讯设备发送消息给NPC",
                "parameters": {"type": "object", "properties": {
                    "recipient": {"type": "string", "description": "收件人NPC名"},
                    "message": {"type": "string", "description": "消息内容"},
                }, "required": ["recipient", "message"]},
            }})

        return tools

    def _handle_send_message(self, args: dict) -> str:
        """Handle send_message tool call (adaptive tool)."""
        recipient = args.get("recipient", "")
        message = args.get("message", "").strip()
        if not message:
            return json.dumps({"error": "消息内容不能为空"}, ensure_ascii=False)
        npc_id = None
        for nid, ndata in self.current_state.get("npcs", {}).items():
            if isinstance(ndata, dict) and recipient in (ndata.get("name", ""), nid):
                npc_id = nid
                break
        if not npc_id:
            return json.dumps({"error": f"找不到NPC: {recipient}"}, ensure_ascii=False)
        messages = self.current_state.setdefault("sent_messages", [])
        messages.append({
            "to": npc_id, "message": message,
            "time": self.current_state.get("game_time", ""),
        })
        if len(messages) > 20:
            del messages[:-20]
        return json.dumps({"sent": True, "to": recipient,
                           "note": "消息已记录，后续回合可据此推演对方反应"},
                          ensure_ascii=False)

    # ================================================================
    # Unified Agent mode (single-loop: retrieval + narrative + settlement)
    # ================================================================

    def _build_unified_system(self) -> str:
        """Build system prompt for the unified agent."""
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system(
            "agentic_unified",
            world_background=(self.script.get("world_background", "") or "")[:500],
            player_name=self.current_state.get("player", {}).get("name", ""),
        )
        # Append dynamic section (same as foreground agent)
        system += "\n\n" + self._build_foreground_dynamic_section()
        return system

    def _build_unified_plan_system(self) -> str:
        from engine.prompt_loader import PromptLoader
        return PromptLoader.get().render_system("agentic_plan")

    def _recent_query_context(self) -> str:
        if not self.world_tree:
            return ""
        exchanges = [exchange for node in self.world_tree.get_active_branch()
                     for exchange in node.get("query_exchanges", [])
                     if isinstance(exchange, dict)]
        if not exchanges:
            return ""
        lines = [f"[玩家询问] {str(exchange.get('question') or '')[:160]}\n"
                 f"[助手答复] {str(exchange.get('answer') or '')[:300]}"
                 for exchange in exchanges[-3:]]
        return ("<recent_queries>\n先前问答仅供对话衔接；答复不等于已核实的世界事实。\n"
                + "\n\n".join(lines) + "\n</recent_queries>")

    @staticmethod
    def _pc_event_reports_context(state: dict) -> str:
        reports = state.get("pc_event_reports") or []
        if not isinstance(reports, list):
            return ""
        lines = [f"- {report.get('npc_name', '未知人物')}：{report.get('evidence', '')}"
                 f"（获知于{report.get('heard_at', '')}，未经核实）"
                 for report in reports[-8:] if isinstance(report, dict) and report.get("evidence")]
        return ("<pc_event_reports>\n主角听到的 NPC 转述，不等于事件已获证实；"
                "只能按来源与原话推演，不能补全幕后事件详情。\n"
                + "\n".join(lines) + "\n</pc_event_reports>") if lines else ""

    def _build_unified_plan_context(self, ctx: dict, player_action: dict) -> str:
        state = self.current_state
        player = state.get("player", {})
        location = player.get("location", "")
        inventory = ", ".join(
            f"{item.get('item', '')}x{item.get('quantity', 1)}"
            for item in state.get("inventory", []) if isinstance(item, dict)
        ) or "无"
        player_details = [f"姓名：{player.get('name', '')}", f"背包：{inventory}"]
        for label, value, limit in (
            ("角色身份", player.get("bio", ""), 120),
            ("角色性格", player.get("personality", ""), 100),
            ("长期目标", state.get("long_term_goal") or player.get("long_term_goal", ""), 120),
        ):
            if value:
                player_details.append(f"{label}：{value[:limit]}")
        parts = [
            f"<current_scene>位置：{self.prompt_builder._resolve_location_name(location)} "
            f"({location})；时间：{state.get('game_time', '')}</current_scene>",
            f"<player>{'；'.join(player_details)}</player>",
        ]
        contacts = []
        for npc_id in ctx.get("present_npc_ids") or []:
            npc = state.get("npcs", {}).get(npc_id)
            if (isinstance(npc, dict) and npc.get("known") is not False
                    and npc.get("met") is not False
                    and self._npc_contact_is_current(npc_id, state)):
                contacts.append(f"{npc.get('name') or npc_id}({npc_id})")
                if len(contacts) == 6:
                    break
        if contacts:
            parts.append("<current_contacts>\n玩家当前可确认接触的人物："
                         + "、".join(contacts) + "\n</current_contacts>")
        recent_queries = self._recent_query_context()
        if recent_queries:
            parts.append(recent_queries)
        pc_clues = self.prompt_builder.format_pc_lore_clues(state)
        if pc_clues:
            parts.append(pc_clues)
        recent = ctx.get("recent_nodes") or []
        event_reports = self._pc_event_reports_context(state)
        if event_reports:
            parts.append(event_reports)
        if ctx.get("is_system_opening"):
            opening_scene = ctx.get("opening_scene") or (
                (recent[-1].get("ai_response") or recent[-1].get("narrative") or "") if recent else ""
            )
            opening_parts = [
                "这是系统开场，不是玩家行动；只呈现当前开场，不替玩家选择行动或提前询问去向。",
                f"系统开场不推进时间，end_time 必须为 {ctx.get('old_time') or state.get('game_time', '')}。",
            ]
            if opening_scene:
                opening_parts.append(f"剧本预置开场素材：\n{opening_scene[:500]}")
            choices = ctx.get("opening_choices") or (
                [choice.get("text", "") for choice in recent[-1].get("choices_presented", [])[:6]
                 if isinstance(choice, dict) and choice.get("text")] if recent else []
            )
            if choices:
                opening_parts.append("尚未被玩家选择的行动方向：\n" + "\n".join(f"- {text}" for text in choices))
            parts.append("<system_opening>\n" + "\n\n".join(opening_parts) + "\n</system_opening>")
        interactables = self._visible_interactables_context()
        if interactables:
            parts.append(interactables)
        if len(recent) > 1 and not ctx.get("is_system_opening"):
            visible_recent = []
            for node in recent[-3:-1]:
                action = node.get("player_action")
                action_text = action.get("text", "") if isinstance(action, dict) else str(action or "")
                narrative = node.get("ai_response") or node.get("narrative") or ""
                if action_text or narrative:
                    visible_recent.append(f"第{node.get('turn_number', '?')}回合："
                                          + (f"[玩家行动] {action_text[:100]}\n" if action_text else "")
                                          + narrative[-260:])
            if visible_recent:
                parts.append("<recent_history>\n" + "\n\n".join(visible_recent)
                             + "\n</recent_history>")
        if recent and not ctx.get("is_system_opening"):
            previous = recent[-1]
            visible_narrative = previous.get("ai_response") or previous.get("narrative") or ""
            if visible_narrative:
                parts.append(f"<previous_round>{visible_narrative[-1500:]}</previous_round>")
        parts.append(f"<player_action>{player_action.get('text', '')}</player_action>")
        player_inputs = self.prompt_builder.format_turn_player_inputs(ctx.get("_turn_player_inputs"))
        if player_inputs:
            parts.append(player_inputs)
        destination = self._map_travel_destination(player_action)
        if destination:
            parts.append(f"<map_travel>玩家尝试前往目标地点ID：{destination}；尚未抵达。"
                         f"{self._map_travel_route_hint(destination)}</map_travel>")
        return "\n\n".join(parts)

    @staticmethod
    def _plan_reply_subject(ctx: dict) -> str:
        reply = ctx.get("_plan_reply", "")
        if not isinstance(reply, str):
            return ""
        reply = reply.strip()[:160]
        correction = re.search(
            r"(?:^|[，,；;])\s*(?:改查|转而(?:查|核实)|改为(?:查|核实))\s*([^，,；;。！？!?]+)",
            reply,
        )
        if correction:
            reply = correction.group(1).strip()
        else:
            requested = re.search(
                r"(?:^|[，,；;])\s*(?:请)?(?:先|再|接着|然后)?(?:去)?(?:查|核实|核对|调查|了解)\s*([^，,；;。！？!?]+)",
                reply,
            )
            if requested:
                reply = requested.group(1).strip()
            elif re.search(r"(?:不想|不要|不再|不用|不必|别|不)(?:再|去)?(?:查|核对|核实|调查|了解)", reply):
                return ""
        if (len(reply) < 2 or reply in ("是", "否", "确认", "继续")
                or re.fullmatch(r"(?:选|第)?[0-9一二三四五六七八九十]+(?:项|个)?", reply)):
            return ""
        return reply

    @classmethod
    def _plan_reply_subjects(cls, ctx: dict) -> list[str]:
        reply = ctx.get("_plan_reply", "")
        if not isinstance(reply, str):
            return []
        lines = reply.splitlines()
        numbered = [re.fullmatch(
            r"\s*(?:\d+|[一二三四五六七八九十]+)[.、．]\s*.+?[：:]\s*(.+?)\s*", line)
            for line in lines]
        answers = ([match.group(1) if match else line.strip()
                    for line, match in zip(lines, numbered)
                    if match or re.match(
                        r"\s*(?:(?:请)?(?:先|再|接着|然后)?(?:去)?(?:查|核实|核对|调查|了解)"
                        r"|改查|改为(?:查|核实)|转而(?:查|核实))", line)]
                   if any(numbered) else [reply])
        clauses = [clause for answer in answers
                   for clause in re.split(
                       r"(?:[，,；;。.!！？?]\s*|\s+)(?=(?:再|接着|然后)(?:去)?(?:查|核实|核对|调查|了解))", answer)]
        return [subject for clause in clauses for topic in re.split(r"、|以及", clause)
                if (subject := cls._plan_reply_subject({"_plan_reply": topic}))]

    def _plan_reply_lore_context(self, ctx: dict) -> str:
        subjects = list(dict.fromkeys(self._plan_reply_subjects(ctx)))
        if not subjects or not self.prompt_builder.lorebook:
            return ""
        max_entries = min(6, max(3, len(subjects)))
        previewed = set(ctx.get("lore_preview_ids") or [])
        candidates = []
        for subject in subjects:
            result = self.prompt_builder.lorebook.query(
                keyword=subject,
                excluded_ids=self.prompt_builder._excluded_lore_ids(self.current_state) | previewed,
            )
            candidates.append(result.get("entries", []))
        entries = []
        for matches in candidates:
            if len(entries) >= max_entries:
                break
            entry = next((item for item in matches
                          if not item["id"].startswith("_") and item["id"] not in previewed), None)
            if entry is None:
                entry = next((item for item in matches if item["id"] not in previewed), None)
            if entry is not None:
                entries.append(entry)
                previewed.add(entry["id"])
        for matches in candidates:
            if len(entries) >= max_entries:
                break
            authored = next((item for item in matches
                             if not item["id"].startswith("_") and item in entries), None)
            dynamic = next((item for item in matches
                            if item["id"].startswith("_") and item["id"] not in previewed), None)
            if authored is not None and dynamic is not None:
                entries.insert(entries.index(authored), dynamic)
                previewed.add(dynamic["id"])
        for matches in candidates:
            if len(entries) >= max_entries:
                break
            for entry in matches:
                if entry["id"] not in previewed:
                    entries.append(entry)
                    previewed.add(entry["id"])
                if len(entries) >= max_entries:
                    break
        if not entries:
            return ""
        lines = [f"- entry_id={entry['id']}；visibility={entry['visibility']}；摘要={entry['content'][:300]}"
                 for entry in entries]
        return ("<plan_reply_lore>\n玩家澄清方向后的资料候选；仅供推演，不代表主角知情或事件已经发生。"
                "需要时对回复中的独立主题逐项核对；可用 query_lorebook 的 entry_ids 一次读取最多3条候选，"
                "超长条目按 entry_id 分段续读。\n"
                + "\n".join(lines) + "\n</plan_reply_lore>")

    def _plan_reply_event_context(self, ctx: dict) -> str:
        from datetime import timedelta
        from engine.event_scheduler import parse_time

        subjects = [(subject.split("的", 1)[0].casefold(),
                     subject.rsplit("的", 1)[-1].casefold())
                    for subject in self._plan_reply_subjects(ctx)]
        current = parse_time(self.current_state.get("game_time"))
        if not subjects or current is None:
            return ""
        definitions = {
            event["id"]: event for section in ("one_time_events", "cyclic_events")
            for event in self.script.get(section, [])
        }
        if not definitions:
            return ""
        deadline = (current + timedelta(hours=6)).isoformat()
        scheduled = self.event_scheduler.check_events(
            self.current_state, current.isoformat(), deadline,
            condition_eval=self._evaluate_condition,
        )
        scheduled_keys = {(event["event_id"], event["fire_time"]) for event in scheduled}
        candidates = self.event_scheduler.check_events(
            self.current_state, current.isoformat(), deadline,
        )
        lines = []
        for event in candidates:
            definition = definitions[event["event_id"]]
            searchable = " ".join(str(definition.get(key) or "") for key in ("id", "name", "description"))
            if not any(term in searchable.casefold() for subject in subjects
                       for term in subject if len(term) >= 2):
                continue
            conditional = (event["event_id"], event["fire_time"]) not in scheduled_keys
            lines.append(f"- event_id={event['event_id']}；time={event['fire_time']}；"
                         f"状态={'待验证条件' if conditional else '当前条件满足'}；"
                         f"提示={event['description'][:100]}")
            if len(lines) == 3:
                break
        if not lines:
            return ""
        return ("<plan_reply_events>\n玩家澄清方向后的六小时内事件候选，仅供推演；"
                "尚未发生，不代表主角知情，不得直接作为已发生事实写入叙事。"
                "待验证条件需在实际触发时再次核对。\n"
                + "\n".join(lines) + "\n</plan_reply_events>")

    def _build_unified_context(self, ctx: dict, player_action: dict) -> str:
        """Build per-turn user message for the unified agent (reuses foreground context)."""
        parts = [self._build_foreground_context(ctx, player_action, unified=True)]
        player_inputs = self.prompt_builder.format_turn_player_inputs(ctx.get("_turn_player_inputs"))
        if player_inputs:
            parts.append(player_inputs)
        mechanics = {category: len(entries) for category, entries in self._queryable_mechanics_definitions().items() if entries}
        if mechanics:
            parts.append(
                "<script_mechanics>\n当前分支可查询的剧本机制："
                + json.dumps(mechanics, ensure_ascii=False)
                + "。涉及这些规则时，用 consult_rules(category=类别, query=关键词) 按需读取定义、变量现值与目标完成记录，"
                  "不要凭名称猜测规则。查询不会执行机制，也不模拟本轮未提交的行动；"
                  "幕后规则不等于角色已知事实。\n</script_mechanics>"
            )
        if mechanics.get("story_nodes"):
            parts.append(
                '<story_discovery>需要当前剧情选择时，先 consult_rules(category="story_nodes", '
                'view="runtime", node_status="active") 分页发现节点和选项身份、可选性；'
                '此目录不激活节点、不结算效果。unlocked 不等于可提交。需要规则时改用默认定义视图，'
                'query=node_id 重新查询并按字段读取；需要后果时用 preview_choice_outcome。'
                '长选项用 result_path=["choices"] 分页查看 summary 中的身份和可用性，标签仍按索引读取。'
                '本轮候选判断显式加 include_candidates=true，未结算事件仍不在其中；不是角色已知信息。</story_discovery>'
            )
        reply_lore = self._plan_reply_lore_context(ctx)
        if reply_lore:
            parts.append(reply_lore)
        reply_events = self._plan_reply_event_context(ctx)
        if reply_events:
            parts.append(reply_events)
        active_script = [brief for brief in self._script_anchor_briefs()
                         if brief["status"] == "active"]
        if active_script:
            parts.append("<script_situation>\n活跃剧本节点的幕后底稿，仅用于判断可能的局势方向；"
                         "不代表已经发生或主角知情，不得直接写入叙事。\n"
                         + json.dumps(active_script, ensure_ascii=False)
                         + "\n</script_situation>")
        check_result = ctx.get("check_result")
        if isinstance(check_result, dict):
            outcome = {"success": "成功", "failure": "失败",
                       "critical_success": "大成功", "critical_failure": "大失败"}.get(check_result.get("outcome"))
            if outcome:
                difficulty = {"easy": "简单", "medium": "普通", "hard": "困难",
                              "extreme": "极难"}.get(check_result.get("difficulty"), "")
                attribute = str(check_result.get("related_attribute") or "")[:40]
                details = "、".join(part for part in (attribute, difficulty) if part)
                parts.append(
                    f"<skill_check>系统已结算本次技能检定：检定结果：{outcome}"
                    + (f"（{details}）" if details else "")
                    + "。据此推演本次尝试，不要再次掷骰或推翻检定；"
                      "实际后果仍须遵守剧本和已发生事件。不要在叙事中提及具体数值。"
                      "</skill_check>"
                )
        if ctx.get("requested_wait_minutes"):
            parts.append(
                f"<wait_duration>\n玩家明确要求等待{ctx['requested_wait_minutes']}分钟；"
                f"未发生实际打断等待的事件时，end_time 至少推进到 {ctx['new_time']}。"
                "不要套用一般观察耗时或仅为停在未来事件前夕而缩短玩家明确的等待。\n</wait_duration>"
            )
        destination = self._map_travel_destination(player_action)
        if destination:
            parts.append(f"<map_travel>玩家尝试前往目标地点ID：{destination}。"
                         "这是行动意图，不代表已经抵达；根据推演结果决定是否在结算中提交 location_change。"
                         "如果途中受阻、未抵达，不要提交目标地点的 location_change。"
                         f"{self._map_travel_route_hint(destination)}</map_travel>")

        preview_hours = min(24, max(3, ((ctx.get("requested_wait_minutes") or 0) + 59) // 60))
        upcoming = json.loads(self._handle_peek_upcoming_events({"hours_ahead": preview_hours}, ctx=ctx))
        if upcoming.get("events") or upcoming.get("conditional_candidates"):
            parts.append("<upcoming_events>\nevents 是按触发时刻和前序事件效果只读推演的计划事件；conditional_candidates 是投影未触发的条件事件，状态改变后应重新查询。均非已发生或主角已知：\n"
                         + json.dumps(upcoming, ensure_ascii=False)
                         + ("\n列表未显示完整；需要后续事件时调用 peek_upcoming_events，offset=5、10……继续读取。"
                            if upcoming.get("truncated") or upcoming.get("conditional_truncated") else "")
                         + "\n</upcoming_events>")
        if (ctx.get("requested_wait_minutes") or 0) > 24 * 60:
            next_preview_hours = min(24, (ctx["requested_wait_minutes"] - 24 * 60 + 59) // 60)
            parts.append("<preview_limit>事件预览仅覆盖等待的前24小时；"
                         f"可调用 peek_upcoming_events(start_after_hours=24, hours_ahead={next_preview_hours}) "
                         "继续查看下一段，更长等待按24小时递增查询；窗口外仍可能有剧本事件，"
                         "不能因预览中未出现就判定后续无事发生。</preview_limit>")

        recent_traces = [node["agent_trace"] for node in ctx.get("recent_nodes", [])
                         if isinstance(node.get("agent_trace"), dict)
                         and node["agent_trace"].get("mode") == "unified"]
        if recent_traces:
            exp_lines = []
            for trace in recent_traces[-3:]:
                tools = ", ".join(step.get("tool", "") for step in trace.get("rounds", [])[:5]
                                  if isinstance(step, dict) and not step.get("superseded"))
                exp_lines.append(f"T{trace.get('turn', '?')}: {str(trace.get('plan') or '无计划')[:50]} [{tools}]")
            parts.append("<agent_experience>\n" + "\n".join(exp_lines) + "\n</agent_experience>")

        # C1: NPC 自主行为事件
        npc_events = ctx.get("npc_autonomy_events", [])
        if npc_events:
            event_lines = [f"- {e['npc_name']}位置由{e['from']}变为{e['to']}（{e.get('activity', '')}）" for e in npc_events[:5]]
            parts.append("<npc_movements>\n按当前状态与日程核对的位置变化；不代表目击移动或主角知情。\n"
                         + "\n".join(event_lines) + "\n</npc_movements>")

        candidate_npc_events = ctx.get("candidate_npc_autonomy_events", [])
        if candidate_npc_events:
            event_lines = [f"- {e['npc_name']}可能移动到{e['to']}（{e.get('activity', '')}）" for e in candidate_npc_events[:5]]
            parts.append("<candidate_npc_movements>\n预估窗口内的可能日程，仅供推演参考；并非已发生或主角已知。\n"
                         + "\n".join(event_lines) + "\n</candidate_npc_movements>")

        sent_messages = self.current_state.get("sent_messages", [])
        if sent_messages:
            message_lines = [
                f"- {item.get('time', '')} → {item.get('to', '')}: {item.get('message', '')[:500]}"
                for item in sent_messages[-3:]
            ]
            parts.append("<sent_messages>\n玩家此前发出的消息；不代表已收到回复。\n"
                         + "\n".join(message_lines) + "\n</sent_messages>")

        # G3: 世界时钟后台事件
        world_events = self.current_state.pop("_pending_world_events", [])
        if world_events:
            event_lines = [
                f"- {e.get('time', '')} {e.get('npc_name', '')}："
                f"{e.get('previous_location', '')} / {e.get('previous_activity', '')} → "
                f"{e.get('location', '')} / {e.get('activity', '')}"
                for e in world_events if e.get("type") == "npc_schedule_change"
            ]
            if event_lines:
                parts.append("<world_events>\n结算时核对的位置/活动变化，不包含途中经过，也不代表主角已知。"
                             "仅在主角有可靠获知途径时才能写入主角视角：\n"
                             + "\n".join(event_lines[-5:]) + "\n</world_events>")
        # 导演笔记（自动 Push 最近 3 条）
        if hasattr(self, 'director_notes'):
            dn_summary = self.director_notes.get_summary(limit=3)
            if dn_summary:
                parts.append("<director_notes>\n" + dn_summary + "\n</director_notes>")

        # 已知地点列表（供 location_change 参数使用）
        known_locations = self._known_locations_for_agent()
        if known_locations:
            loc_lines = [f"- {lid}: {name}" for lid, name in known_locations]
            parts.append("<known_locations>\n" + "\n".join(loc_lines) + "\n</known_locations>")

        return "\n\n".join(parts)

    def _build_unified_parsed(self, records: list[dict], ctx: dict,
                              route: dict | None = None) -> dict:
        """Merge tool call records into a parsed dict for _apply_parsed_response."""
        # Explode consolidated tool calls into old-style for _merge_state_tool_results
        state_calls = []
        for index, c in enumerate(c for c in records if not c.get("deferred")):
            if c.get("superseded") or index in ctx.get("_undone_record_indexes", ()):
                continue
            if _tool_result_failed(c.get("result")):
                continue
            if c["name"] in ("update_state", "finalize_turn"):
                state_calls.extend(self._explode_update_state(c["args"]))
                if c["name"] == "finalize_turn":
                    state_calls.extend(self._explode_manage_npcs({"operations": c["args"].get("npc_operations", [])}))
            elif c["name"] == "manage_npcs":
                state_calls.extend(self._explode_manage_npcs(c["args"]))
        parsed = self._merge_state_tool_results(state_calls)
        for field_name in ("lore_reveals", "event_reports"):
            parsed[field_name] = [claim for index, record in enumerate(
                record for record in records if not record.get("deferred")
            ) if record.get("name") in ("finalize_turn", "delegate_settlement")
                and not record.get("superseded")
                and not _tool_result_failed(record.get("result"))
                and index not in ctx.get("_undone_record_indexes", ())
                for claims in [record.get("args", {}).get(field_name)]
                if isinstance(claims, list)
                for claim in claims
                if isinstance(claim, dict)]
        # NPC attitude + choices buffers (populated by _handle_finalize_turn / _dispatch_unified_tool)
        self._merge_npc_reaction_tool_results(parsed)
        self._merge_choices_tool_results(parsed)
        if ctx.get("is_system_opening") and ctx.get("recent_nodes"):
            opening_choices = ctx["recent_nodes"][-1].get("choices_presented") or []
            if opening_choices:
                parsed["choices"] = copy.deepcopy(opening_choices)
        if not parsed.get("choices") and not (route or {}).get("skip_choices", False):
            parsed["choices"] = self._generate_context_choices()
        return parsed

    async def _execute_unified_agent(self, ctx: dict, route: dict, player_action: dict,
                                     *, streaming: bool = False):
        """Unified Agent: single loop handles retrieval + narrative + settlement."""
        _start = time.monotonic()
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")
        ctx.pop("_review_plan_reply", None)
        ctx["_unified_narrative_output"] = True
        ctx["_turn_player_inputs"] = copy.deepcopy(ctx.get("_turn_player_inputs") or [])
        ctx["_map_travel_destination"] = self._map_travel_destination(player_action)
        ctx.setdefault("tool_results", [])
        self._reset_stage45_tool_buffers()
        self._turn_summary_override = ""
        self._scene_image_prompt_override = None
        self._delegated_settlement = False
        self._last_tool_result = None
        self._last_state_snapshot = None

        logger.info("=" * 50)
        logger.info("UNIFIED AGENT 开始 — 行动: %s", action_text[:60])
        logger.info("=" * 50)

        plan_system = self._build_unified_plan_system()
        plan_user = self._build_unified_plan_context(ctx, player_action)

        tools = self._build_adaptive_tools(ctx)

        # F2: 只读工具结果缓存（本轮有效）
        _tool_cache: dict[str, str] = {}
        _CACHEABLE_TOOLS = _PRE_PLAN_READ_TOOLS
        call_index = 0
        plan_submitted = False
        semantic_review_task = None
        reviewed_narrative = ""
        semantic_review_calls = 0
        semantic_review_attempts = 0
        semantic_review_timings_ms = []
        semantic_revision_required = False
        semantic_review_exhausted = False
        first_draft_at = None
        first_text_at = None

        def _record_plan_reply(reply):
            ctx["_plan_reply"] = reply
            ctx["_review_plan_reply"] = {
                "plan": ctx.get("_agent_plan", ""),
                "questions": copy.deepcopy(ctx.get("_agent_plan_questions") or []),
                "reply": reply,
            }

        async def _timed_semantic_review(narrative):
            started = time.monotonic()
            try:
                return await self._review_narrative(narrative, action_text, ctx)
            finally:
                semantic_review_timings_ms.append(int((time.monotonic() - started) * 1000))

        async def _fix_npc_narrative(narrative):
            revised = await self._maybe_fix_npc_voices(narrative, self.current_state, ctx.get("present_npc_ids"))
            return narrative if revised == narrative else self._prepare_unified_narrative(revised, ctx)

        async def _capture_visible_events():
            route_task = ctx.get("_preflight_route_task")
            if route_task is not None:
                route.update(await route_task)
            ctx["_visible_conditioned_event_keys"] = copy.deepcopy(ctx.get("_conditioned_event_keys", []))

        async def _dispatch_and_record(name, args):
            nonlocal call_index, terminal_completed, plan_submitted
            nonlocal semantic_review_task, reviewed_narrative, semantic_review_calls, semantic_revision_required
            nonlocal semantic_review_exhausted
            nonlocal semantic_review_attempts
            nonlocal first_draft_at
            record_index = call_index
            call_index += 1
            if name == "submit_plan" and plan_submitted and args.get("questions"):
                return json.dumps({"error": "幕后推演已经开始，不能再向玩家提出基于幕后资料的问题"}, ensure_ascii=False)
            if name == "submit_plan" and ctx.get("_private_preplan_read") and args.get("questions"):
                return json.dumps({"error": "已预读幕后资料，不能将带选项的计划展示给玩家；请依据玩家已明确的行动重新提交无问题计划，若必须询问玩家，应在预读幕后资料前提问"}, ensure_ascii=False)
            if name != "submit_plan" and name not in _PRE_PLAN_READ_TOOLS and not plan_submitted:
                return json.dumps({"error": "请先调用 submit_plan 提交本轮计划，再使用其他工具"}, ensure_ascii=False)
            if name == "write_narrative":
                args["text"] = _clean_narrative_text(args.get("text") or "", ctx)
            elif name in ("finalize_turn", "delegate_settlement") and args.get("narrative"):
                args["narrative"] = _clean_narrative_text(args["narrative"], ctx)
                if args["narrative"] != ctx.get("_finalize_narrative", ""):
                    args["narrative"] = self._prepare_unified_narrative(args["narrative"], ctx)
            if name == "write_narrative" and semantic_review_attempts >= 3:
                if self._prepare_unified_narrative(args.get("text") or "", ctx) != ctx.get("_finalize_narrative", "").strip():
                    return json.dumps({"error": "叙事已达审查次数上限，请使用上一稿结算。"}, ensure_ascii=False)
            if name in ("finalize_turn", "delegate_settlement"):
                if args.get("narrative") and args["narrative"].strip() != ctx.get("_finalize_narrative", "").strip():
                    return json.dumps({"error": "请先调用 write_narrative 提交修订后的叙事，再进行结算。"}, ensure_ascii=False)
                if semantic_review_task is not None:
                    try:
                        review = await semantic_review_task
                    except Exception as exc:
                        logger.warning("统一Agent叙事语义审查不可用: %s", exc)
                        review = None
                    semantic_review_task = None
                    if ctx.get("_finalize_narrative", "") == reviewed_narrative:
                        violations = [
                            {"type": str(item.get("type", ""))[:60], "detail": str(item.get("detail", ""))[:300]}
                            for item in (review.get("violations") or []) if isinstance(item, dict)
                        ] if isinstance(review, dict) else []
                        if review and review.get("pass") is False and violations:
                            semantic_revision_required = True
                            ctx["_semantic_review_status"] = "revision_required"
                            if semantic_review_attempts >= 3:
                                semantic_review_exhausted = True
                            return json.dumps({
                                "error": "叙事语义审查未通过；请调用 write_narrative 修订后重新结算。",
                                "violations": violations,
                            }, ensure_ascii=False)
                        semantic_revision_required = False
                        ctx["_semantic_review_status"] = "passed" if review and review.get("pass") else "unavailable"
                    else:
                        ctx["_semantic_review_status"] = "stale"
                if getattr(self, '_inject_queue', None):
                    return json.dumps({"error": "玩家补充了新指令，此调用未执行；请先读取插话，再根据已完成工具结果重新推演"}, ensure_ascii=False)
                if ctx.get("_finalize_narrative", "") != reviewed_narrative:
                    return json.dumps({"error": "当前叙事未经过语义审查，请调用 write_narrative 确认或修订叙事后重新结算。"}, ensure_ascii=False)
                if semantic_revision_required and ctx.get("_finalize_narrative", "") == reviewed_narrative:
                    return json.dumps({"error": "请先调用 write_narrative 修订审查未通过的叙事。"}, ensure_ascii=False)
            is_read_only = name in _PRE_PLAN_READ_TOOLS or name in (
                "consult_rules", "trace_causality", "preview_choice_outcome", "invoke_character",
            )
            undone = self._last_tool_result if name == "undo_my_last_action" else None
            previous_narrative = ctx.get("_finalize_narrative") if name == "write_narrative" else None
            previous_narrative_events = ctx.get("_narrative_conditional_events") if name == "write_narrative" else None
            previous_timed_event_ids = ctx.get("_narrative_timed_event_ids") if name == "write_narrative" else None
            if name != "undo_my_last_action" and not is_read_only:
                self._last_state_snapshot = None
                if name in ("update_state", "manage_npcs", "send_message", "attempt_deduction"):
                    snapshot = {
                        "current_state": copy.deepcopy(self.current_state),
                        "npc_reactions": copy.deepcopy(self._npc_reaction_tool_calls),
                        "new_npcs": copy.deepcopy(self._new_npc_tool_calls),
                        "choices": copy.deepcopy(self._choices_tool_calls),
                        "graph": copy.deepcopy(self.narrative_graph.snapshot()),
                        "conditioned_event_keys": copy.deepcopy(ctx.get("_conditioned_event_keys")),
                        "accepted_state_updates": copy.deepcopy(ctx.get("_accepted_state_updates")),
                    }
                    if name == "manage_npcs":
                        snapshot["npc_registry"] = (
                            copy.deepcopy(self.script.get("npcs", [])),
                            copy.deepcopy(self._npc_by_id),
                            copy.deepcopy(self._npc_name_to_id),
                            copy.deepcopy(self.prompt_builder._npc_by_id),
                            copy.deepcopy(self.prompt_builder._npc_name_map),
                        )
                    self._last_state_snapshot = snapshot
            # F2: 对只读工具做缓存
            if name in _CACHEABLE_TOOLS:
                cache_key = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                if cache_key in _tool_cache:
                    logger.info("[缓存命中] %s", name)
                    result = _tool_cache[cache_key]
                else:
                    result = await self._dispatch_unified_tool(ctx, name, args)
                    if not _tool_result_failed(result):
                        _tool_cache[cache_key] = result
            else:
                result = await self._dispatch_unified_tool(ctx, name, args)
                if name == "finalize_turn" or name in ("update_state", "manage_npcs", "send_message", "attempt_deduction",
                            "delegate_settlement", "undo_my_last_action") and not _tool_result_failed(result):
                    _tool_cache.clear()
            if not plan_submitted and not _tool_result_failed(result):
                if name == "query_lorebook":
                    try:
                        entries = json.loads(result).get("entries", [])
                    except (TypeError, ValueError, AttributeError):
                        entries = []
                    discovered = set(self.current_state.get("pc_discovered_lore") or [])
                    if any(isinstance(entry, dict) and (
                        entry.get("visibility") == "world" or
                        (entry.get("visibility") == "hidden" and entry.get("id") not in discovered)
                    ) for entry in entries):
                        ctx["_private_preplan_read"] = True
                elif name in _PRIVATE_PRE_PLAN_TOOLS:
                    ctx["_private_preplan_read"] = True
            if name == "write_narrative" and not _tool_result_failed(result):
                submitted = ctx.get("_finalize_narrative", "")
                if submitted and first_draft_at is None:
                    first_draft_at = time.monotonic()
                if submitted and submitted != reviewed_narrative and semantic_review_attempts < 3:
                    if semantic_review_task is not None:
                        semantic_review_task.cancel()
                        await asyncio.gather(semantic_review_task, return_exceptions=True)
                    reviewed_narrative = submitted
                    semantic_review_task = asyncio.create_task(
                        _timed_semantic_review(submitted))
                    semantic_review_calls += 1
                    semantic_review_attempts += 1
                    ctx["_semantic_review_status"] = "pending"
                elif semantic_revision_required:
                    ctx["_semantic_review_status"] = (
                        "revised" if submitted != reviewed_narrative else "revision_required")
            if name == "submit_plan" and not _tool_result_failed(result):
                if not plan_submitted:
                    ctx["_public_agent_plan"] = _PRIVATE_PLAN_SUMMARY if ctx.get("_private_preplan_read") else args.get("plan", "")
                plan_submitted = True
            if name == "undo_my_last_action" and undone and result.startswith("已撤销上一个工具调用"):
                ctx.setdefault("_undone_record_indexes", set()).add(undone["record_index"])
            elif name != "undo_my_last_action" and not is_read_only:
                self._last_tool_result = {"name": name, "args": args,
                                          "record_index": record_index,
                                           "previous_narrative": previous_narrative,
                                           "previous_narrative_events": previous_narrative_events,
                                           "previous_timed_event_ids": previous_timed_event_ids}
            if name in ("finalize_turn", "delegate_settlement") and not _tool_result_failed(result):
                terminal_completed = True
                self._agent_inject_enabled = False
            return result

        holder = _AgentResult()
        first_model_interval = {}
        streamed_narrative = ""
        first_text_model_calls = None
        first_text_plan_wait_ms = None
        ctx["_review_active_script"] = self._narrative_review_evidence(ctx)[0]
        buffer_sensitive_narrative = bool(ctx["_review_active_script"] or ctx.get("_map_travel_destination"))
        buffer_timed_narrative = False
        buffer_until_settlement = bool(ctx.get("conditional_event_candidates"))
        narrative_review_started = False
        review_ready_for_stream = False
        terminal_completed = False
        max_agent_rounds = 20

        def _capture_tool_batch(calls):
            nonlocal buffer_until_settlement
            if any(call.get("name") == "delegate_settlement" for call in calls):
                buffer_until_settlement = True

        async def _interleave_review(agent_events):
            next_event = None
            observed_review = None
            try:
                while True:
                    if next_event is None:
                        next_event = asyncio.create_task(anext(agent_events))
                    current_review = semantic_review_task
                    watch_review = (review_ready_for_stream and not buffer_timed_narrative
                                    and not buffer_until_settlement
                                    and current_review is not None and current_review is not observed_review)
                    pending = {next_event, current_review} if watch_review else {next_event}
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    if watch_review and current_review in done:
                        observed_review = current_review
                        try:
                            review = current_review.result()
                        except (Exception, asyncio.CancelledError):
                            review = None
                        if (current_review is semantic_review_task
                                and review and review.get("pass") is True
                                and not getattr(self, '_inject_queue', None)
                                and ctx.get("_finalize_narrative", "") == reviewed_narrative
                                and not self._map_travel_revision_feedback(ctx, reviewed_narrative)):
                            yield {"type": "_early_reviewed_text", "content": reviewed_narrative}
                    if next_event in done:
                        try:
                            event = next_event.result()
                        except StopAsyncIteration:
                            break
                        next_event = None
                        yield event
            finally:
                if next_event is not None:
                    if not next_event.done():
                        next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                await agent_events.aclose()

        agent_events = self._agent_loop(
                [{"role": "user", "content": plan_user}],
                plan_system, tools,
                _dispatch_and_record,
                max_rounds=max_agent_rounds, label="统一Agent",
                result_holder=holder,
                interactive_plan=streaming,
                plan_before_tools=True,
                before_tool_calls=_capture_visible_events,
                terminal_tool_required=True,
                report_tool_start=streaming,
                after_plan_context=lambda: (
                    self._build_unified_system(), self._build_unified_context(ctx, player_action)),
                on_plan_reply=_record_plan_reply,
                on_tool_batch=_capture_tool_batch,
                suppress_plan_questions=bool(ctx.get("is_system_opening")),
                first_model_interval=first_model_interval,
            )
        if streaming and buffer_sensitive_narrative:
            agent_events = _interleave_review(agent_events)
        try:
            async for event in agent_events:
                if semantic_review_exhausted:
                    raise SemanticReviewError()
                if event.get("type") == "user_inject":
                    player_input = {"message": event["message"]}
                    if holder.user_injections[-1].get("plan_reply") and ctx.get("_review_plan_reply"):
                        player_input["plan_reply_context"] = copy.deepcopy(ctx["_review_plan_reply"])
                    ctx["_turn_player_inputs"].append(player_input)
                    action_text += "\n玩家补充：" + event["message"]
                    ctx["action_text"] = action_text
                    if semantic_review_task is not None:
                        semantic_review_task.cancel()
                        await asyncio.gather(semantic_review_task, return_exceptions=True)
                    semantic_review_task = None
                    reviewed_narrative = ""
                    semantic_review_attempts = 0
                    semantic_revision_required = False
                    review_ready_for_stream = False
                    ctx["_semantic_review_status"] = "stale"
                if event.get("type") == "_early_reviewed_text":
                    submitted = event["content"].strip()
                    if submitted and submitted != streamed_narrative:
                        if first_text_model_calls is None:
                            first_text_model_calls = holder.model_calls
                            first_text_plan_wait_ms = holder.plan_wait_ms
                            first_text_at = time.monotonic()
                        yield {"type": "narrative_revised" if streamed_narrative else "text", "content": submitted,
                               "reviewed": True}
                        streamed_narrative = submitted
                    continue
                # Emit agent_plan SSE event when submit_plan is called
                if event.get("type") == "tool_call" and event.get("tool_name") == "submit_plan":
                    plan_args = event.get("tool_args") or {}
                    plan_text = plan_args.get("plan", "")
                    plan_questions = plan_args.get("questions", [])
                    if streaming and event.get("show_plan"):
                        if event.get("plan_id"):
                            self._pending_agent_plan_signature = hashlib.sha256(
                                json.dumps([plan_text, plan_questions], sort_keys=True,
                                           ensure_ascii=False).encode("utf-8")
                            ).hexdigest()
                        yield {"type": "agent_plan", "plan": (
                            _PRIVATE_PLAN_SUMMARY if ctx.get("_private_preplan_read") else plan_text
                        ), "questions": plan_questions,
                               **({"plan_id": event["plan_id"]} if event.get("plan_id") else {})}
                elif streaming:
                    if event.get("type") == "agent_done" and not terminal_completed:
                        continue
                    if event.get("type") == "tool_call" and event.get("tool_name") in (
                            "write_narrative", "finalize_turn", "delegate_settlement"):
                        claimed_event = (ctx.get("_narrative_timed_event_ids") or
                                         self._narrative_claimed_timed_event_ids(
                                             ctx, ctx.get("_finalize_narrative", "")))
                        submitted_claim = event["tool_name"] == "write_narrative" and (
                            (event.get("tool_args") or {}).get("event_ids") or
                            self._narrative_claimed_timed_event_ids(
                                ctx, (event.get("tool_args") or {}).get("text", "")) or
                            self._narrative_reveals_pending_consequence(
                                ctx, (event.get("tool_args") or {}).get("text", "")))
                        buffer_timed_narrative = bool(claimed_event or submitted_claim)
                        if not narrative_review_started and (buffer_sensitive_narrative or buffer_timed_narrative
                                                             or buffer_until_settlement):
                            narrative_review_started = True
                            yield {"type": "preflight", "stage": "narrative_review"}
                    if (buffer_sensitive_narrative or buffer_timed_narrative or buffer_until_settlement) and event.get("type") == "tool_call":
                        event = {**event, "tool_args": {
                            key: value for key, value in event.get("tool_args", {}).items()
                            if key not in ("text", "narrative")
                        }}
                    if event.get("type") == "tool_call" and event.get("tool_name") == "write_narrative" and semantic_review_task is not None:
                        review_ready_for_stream = True
                    yield {**event, "text": ""} if (buffer_sensitive_narrative or buffer_timed_narrative or buffer_until_settlement) and event.get("type") == "agent_done" else event
                if event.get("type") == "aborted":
                    if not streaming:
                        yield event
                    return
                if streaming and event.get("type") == "tool_call":
                    submitted = ctx.get("_finalize_narrative", "").strip()
                    if submitted and submitted != streamed_narrative and not (buffer_sensitive_narrative or buffer_timed_narrative or buffer_until_settlement):
                        if first_text_model_calls is None:
                            first_text_model_calls = holder.model_calls
                            first_text_plan_wait_ms = holder.plan_wait_ms
                            first_text_at = time.monotonic()
                        yield {"type": "narrative_revised" if streamed_narrative else "text", "content": submitted}
                        streamed_narrative = submitted
        finally:
            try:
                await agent_events.aclose()
            finally:
                if semantic_review_task is not None:
                    semantic_review_task.cancel()
                    await asyncio.gather(semantic_review_task, return_exceptions=True)

        route_task = ctx.get("_preflight_route_task")
        if route_task is not None and not self._abort_flag:
            route.update(await route_task)

        if getattr(self, '_abort_flag', False):
            yield {"type": "aborted", "label": "统一Agent"}
            return
        if not terminal_completed:
            if ctx.get("_map_travel_revision_feedback"):
                raise MapTravelSettlementError()
            raise IncompleteAgentTurnError("统一Agent未完成结算：请重试本轮行动")

        # 叙事来源优先级：write_narrative 工具 > content 自由文本 > 摘要兜底
        submitted_narrative = ctx.get("_finalize_narrative", "").strip()
        narrative = submitted_narrative or (holder.text or "").strip()
        if not narrative:
            logger.warning("统一Agent未输出叙事文本，使用摘要兜底")
            narrative = ctx.get("_turn_summary_override") or self._turn_summary_override or "（叙事生成失败）"

        narrative = (_clean_narrative_text(narrative, ctx) if submitted_narrative
                     else self._prepare_unified_narrative(narrative, ctx))
        if not narrative:
            raise IncompleteAgentTurnError("剧本输出处理后叙事为空，无法完成本轮")

        # Streaming: emit complete narrative
        if streaming and narrative != streamed_narrative and not (buffer_sensitive_narrative or buffer_timed_narrative or buffer_until_settlement):
            if first_text_model_calls is None:
                first_text_model_calls = holder.model_calls
                first_text_plan_wait_ms = holder.plan_wait_ms
                first_text_at = time.monotonic()
            yield {"type": "narrative_revised" if streamed_narrative else "text", "content": narrative}
            streamed_narrative = narrative

        # NPC voice consistency
        narrative = await _fix_npc_narrative(narrative)

        if self._delegated_settlement and not getattr(self, '_abort_flag', False):
            async for event in self._run_background_agent(
                ctx, narrative, "", player_action, streaming=streaming,
                skip_hints=ctx.get("_delegated_settlement_hint", ""), route=route,
            ):
                if event.get("type") == "_bg_result":
                    background = event["holder"]
                    holder.records.extend(background.records)
                    holder.prompt_tokens += background.prompt_tokens
                    holder.completion_tokens += background.completion_tokens
                    holder.model_calls += background.model_calls
                    holder.model_timings_ms.extend(background.model_timings_ms)
                    holder.model_first_output_ms.extend(background.model_first_output_ms)
                    holder.model_request_chars.extend(background.model_request_chars)
                    holder.model_token_usage.extend(background.model_token_usage)
                    holder.model_request_breakdowns.extend(background.model_request_breakdowns)
                    holder.model_phases.extend(background.model_phases)
                    holder.plan_waits += background.plan_waits
                    holder.plan_wait_ms += background.plan_wait_ms
                    holder.user_injections.extend(background.user_injections)
                elif streaming:
                    yield event
                if event.get("type") == "aborted":
                    self._delegated_settlement = False
                    if not streaming:
                        yield event
                    return
            self._delegated_settlement = False
            revised = _clean_narrative_text(ctx.get("_finalize_narrative", narrative).strip(), ctx)
            if revised != narrative:
                narrative = await _fix_npc_narrative(revised)

        if not narrative:
            raise IncompleteAgentTurnError("剧本输出处理后叙事为空，无法完成本轮")
        if narrative != reviewed_narrative:
            semantic_review_calls += 1
            try:
                review = await _timed_semantic_review(narrative)
            except Exception as exc:
                logger.warning("统一Agent最终叙事语义审查不可用: %s", exc)
                review = None
            violations = [item for item in (review.get("violations") or [])
                          if isinstance(item, dict)] if isinstance(review, dict) else []
            if review and review.get("pass") is False and violations:
                raise SemanticReviewError()
            ctx["_semantic_review_status"] = "passed" if review and review.get("pass") else "unavailable"

        if streaming and narrative != streamed_narrative:
            if first_text_model_calls is None:
                first_text_model_calls = holder.model_calls
                first_text_plan_wait_ms = holder.plan_wait_ms
                first_text_at = time.monotonic()
            event_type = "narrative_revised" if streamed_narrative else "text"
            if buffer_timed_narrative or buffer_until_settlement:
                ctx["_deferred_event_narrative"] = {
                    "type": event_type, "first_draft_at": first_draft_at,
                    "plan_wait_ms": first_text_plan_wait_ms,
                }
            else:
                yield {"type": event_type, "content": narrative}

        ctx["tool_results"].extend(holder.records)

        # Build parsed result from tool call records
        parsed = self._build_unified_parsed(holder.records, ctx, route)
        parsed["narrative"] = narrative

        # Reset abort flag
        if hasattr(self, '_abort_flag'):
            self._abort_flag = False

        # Post-processing flags
        ctx["_agentic_post_processed"] = True
        ctx.setdefault("plot_decision", "")
        ctx.setdefault("plot_reasoning", "")
        if self._turn_summary_override:
            ctx["turn_summary_override"] = self._turn_summary_override
        if self._scene_image_prompt_override:
            ctx["scene_image_prompt"] = self._scene_image_prompt_override

        # Build reasoning trace
        reasoning_parts = []
        for c in holder.records:
            reasoning_parts.append(f"[{c['name']}]")
        _narrative_reasoning = "\n".join(reasoning_parts)

        # Stats logging
        choices_count = len(parsed.get("choices", []))
        state_changes = (
            len(parsed.get("state_changes", []))
            + len(parsed.get("activate_states", []))
            + len(parsed.get("deactivate_states", []))
            + len(parsed.get("inventory_changes", []))
        )
        npc_att = len(parsed.get("npc_attitude_changes", []))
        logger.info("=" * 50)
        logger.info("UNIFIED AGENT 完成 — 叙事%d字 | 选项%d | 状态变更%d | NPC态度%d | 工具%d",
                    len(narrative), choices_count, state_changes, npc_att, len(holder.records))
        logger.info("=" * 50)

        self._audit_settlement(parsed)

        # E1: Agent Trace 可视化数据
        elapsed_ms = int((time.monotonic() - _start) * 1000)
        lore_queries = [record for record in holder.records
                        if record["name"] == "query_lorebook" and not record.get("deferred")]
        lore_hits = 0
        lore_entry_reads = 0
        lore_retrievals = []
        exact_lore_read_ids = set()
        for step, record in enumerate(holder.records, 1):
            if record["name"] != "query_lorebook" or record.get("deferred"):
                continue
            try:
                result = record["result"]
                result = json.loads(result) if isinstance(result, str) else result
            except (TypeError, ValueError):
                continue
            entries = result.get("entries", []) if isinstance(result, dict) else []
            lore_hits += len(entries)
            read_entries = [entry for entry in entries if isinstance(entry, dict)
                            and entry.get("id") and entry.get("content")]
            lore_entry_reads += (len(read_entries) if record.get("args", {}).get("entry_ids")
                                 else bool(read_entries and record.get("args", {}).get("entry_id")))
            args = record.get("args", {})
            if args.get("entry_id") or args.get("entry_ids"):
                exact_lore_read_ids.update(entry["id"] for entry in read_entries)
            lore_retrievals.append({
                "step": step,
                "keyword": args.get("keyword", ""),
                "entry_id": args.get("entry_id", ""),
                "entry_ids": [entry["id"] for entry in entries if isinstance(entry, dict) and entry.get("id")],
            })
        route_interval = ctx.get("_preflight_route_interval") or {}
        route_plan_overlap_ms = (max(0, int((min(route_interval["end"], first_model_interval["end"])
                                          - max(route_interval["start"], first_model_interval["start"])) * 1000))
                                 if all(key in route_interval for key in ("start", "end"))
                                 and all(key in first_model_interval for key in ("start", "end")) else None)
        trace = {
            "turn": self.turn_number,
            "mode": "unified",
            "plan": ctx.get("_public_agent_plan", ""),
            "plan_visibility": "private" if ctx.get("_private_preplan_read") else "public",
            "reflection": ctx.get("_reflection", ""),
            "rounds": [],
            "plan_interactions": [],
            "user_injections": list(holder.user_injections),
            "total_tokens": {"prompt": holder.prompt_tokens, "completion": holder.completion_tokens},
            "narrative_length": len(narrative),
            "tools_count": len(holder.records),
            "duration_ms": elapsed_ms,
            "prepare_ms": ctx.get("prepare_ms"),
            "plan_event_ms": ctx.get("plan_event_ms") if streaming else None,
            "route_plan_overlap_ms": route_plan_overlap_ms,
            "first_text_ms": ctx.get("first_text_ms") if streaming else None,
            "first_draft_ms": int((first_draft_at - _start) * 1000)
            if streaming and first_draft_at is not None else None,
            "first_draft_to_text_ms": max(0, int((first_text_at - first_draft_at) * 1000))
            if streaming and first_draft_at is not None and first_text_at is not None else None,
            "first_text_active_ms": max(0, ctx["first_text_ms"] - first_text_plan_wait_ms)
            if streaming and ctx.get("first_text_ms") is not None
            and first_text_plan_wait_ms is not None else None,
            "model_calls": holder.model_calls,
            "semantic_review_calls": semantic_review_calls,
            "semantic_review_model_calls": max(semantic_review_calls, ctx.get("_semantic_review_model_calls", 0)),
            "semantic_review_timings_ms": semantic_review_timings_ms,
            "semantic_review_status": ctx.get("_semantic_review_status", "not_run"),
            "agent_model_timings_ms": list(holder.model_timings_ms),
            "agent_first_output_ms": list(holder.model_first_output_ms),
            "agent_request_chars": list(holder.model_request_chars),
            "agent_model_token_usage": list(holder.model_token_usage),
            "agent_request_breakdowns": list(holder.model_request_breakdowns),
            "agent_model_phases": list(holder.model_phases),
            "preflight_model_calls": dict(ctx.get("preflight_model_calls", {})),
            "first_text_model_calls": first_text_model_calls if streaming else None,
            "preflight_timings_ms": dict(ctx.get("preflight_timings_ms", {})),
            "retrieval_timings_ms": dict(ctx.get("retrieval_timings_ms", {})),
            "primary_model_calls": holder.model_calls + max(semantic_review_calls, ctx.get("_semantic_review_model_calls", 0)) + sum(ctx.get("preflight_model_calls", {}).values()),
            "plan_waits": holder.plan_waits,
            "plan_wait_ms": holder.plan_wait_ms,
            "lore_queries": len(lore_queries),
            "lore_hits": lore_hits,
            "lore_entry_reads": lore_entry_reads,
            "lore_candidate_reads": len(exact_lore_read_ids & set(ctx.get("lore_candidate_ids", []))),
            "lore_retrievals": lore_retrievals,
            "lore_activated_count": len(ctx.get("activated_lore", [])),
            "lore_preview_ids": ctx.get("lore_preview_ids", []),
            "lore_candidate_ids": ctx.get("lore_candidate_ids", []),
            "rag_history_hit_count": len(ctx.get("rag_history_hits") or []),
            "databank_hit_count": len(ctx.get("databank_hits") or []),
            "rag_lore_hit_count": len(ctx.get("lore_ids_from_rag") or []),
        }
        for i, record in enumerate(holder.records):
            trace["rounds"].append({
                "step": i + 1,
                "tool": record["name"],
                "args_summary": str(record.get("args", {}))[:100],
                "result_summary": str(record.get("result", ""))[:100],
                **({"duration_ms": record["duration_ms"]} if "duration_ms" in record else {}),
                **({"superseded": True} if record.get("superseded") else {}),
            })
            if (record["name"] == "submit_plan" and isinstance(record.get("args"), dict)
                    and not _tool_result_failed(record.get("result"))
                    and not trace["plan_interactions"]):
                args = record["args"]
                trace["plan_interactions"].append({
                    "step": i + 1,
                    "plan": (_PRIVATE_PLAN_SUMMARY if ctx.get("_private_preplan_read")
                             else args.get("plan", "")),
                    "questions": [] if ctx.get("_private_preplan_read") else args.get("questions", []),
                    "reply": record.get("player_reply"),
                })
        if not hasattr(self, '_agent_traces'):
            self._agent_traces = []
        trace = _public_agent_trace(trace)
        ctx["agent_trace"] = trace
        self._agent_traces.append(trace)
        if len(self._agent_traces) > 10:
            self._agent_traces.pop(0)

        yield {
            "type": "pipeline_result",
            "narrative": narrative,
            "parsed": parsed,
            "warnings": _warnings,
            "plot_decision": "",
            "plot_reasoning": "",
            "narrative_reasoning": _narrative_reasoning,
            "compose_msgs": [],
            "compose_sys": "",
            "proactive_hint": ctx.get("_proactive_hint", ""),
            "token_usage": {
                "prompt": holder.prompt_tokens,
                "completion": holder.completion_tokens,
                "total": holder.prompt_tokens + holder.completion_tokens,
            },
            "agent_trace": trace,
        }

    def _prepare_unified_narrative(self, text: str, ctx: dict) -> str:
        text = _clean_narrative_text(text, ctx)
        if not text or text == ctx.get("_finalize_narrative"):
            return text
        return _clean_narrative_text(self._apply_narrative_output_rules(text), ctx)

    async def _dispatch_unified_tool(self, ctx: dict, name: str, args: dict):
        """Dispatch tool calls for the unified agent."""
        if name in ("finalize_turn", "delegate_settlement") and args.get("narrative"):
            args["narrative"] = _clean_narrative_text(args["narrative"], ctx)
            if args["narrative"] != ctx.get("_finalize_narrative", ""):
                args["narrative"] = self._prepare_unified_narrative(args["narrative"], ctx)
        # D1: undo_my_last_action — 自我回退
        if name == "undo_my_last_action":
            reason = args.get("reason", "")
            if hasattr(self, '_last_tool_result') and self._last_tool_result:
                undone = self._last_tool_result
                undone_name = undone.get("name", "")
                logger.info("[统一Agent] 自我回退: %s (原因: %s)", undone_name, reason)
                snapshot = getattr(self, '_last_state_snapshot', None)
                if undone_name in ("update_state", "manage_npcs", "send_message", "attempt_deduction") and snapshot is not None:
                    self.current_state = snapshot["current_state"]
                    if undone_name == "update_state":
                        previous_updates = snapshot["accepted_state_updates"]
                        if previous_updates is None:
                            ctx.pop("_accepted_state_updates", None)
                        else:
                            ctx["_accepted_state_updates"] = previous_updates
                        previous_keys = snapshot["conditioned_event_keys"]
                        if previous_keys is None:
                            ctx.pop("_conditioned_event_keys", None)
                        else:
                            ctx["_conditioned_event_keys"] = previous_keys
                    self._npc_reaction_tool_calls = snapshot["npc_reactions"]
                    self._new_npc_tool_calls = snapshot["new_npcs"]
                    self._choices_tool_calls = snapshot["choices"]
                    self.narrative_graph.nodes = snapshot["graph"]["nodes"]
                    self.narrative_graph.edges = snapshot["graph"]["edges"]
                    if "npc_registry" in snapshot:
                        (self.script["npcs"], self._npc_by_id, self._npc_name_to_id,
                         self.prompt_builder._npc_by_id, self.prompt_builder._npc_name_map) = snapshot["npc_registry"]
                    self._last_state_snapshot = None
                    logger.info("[统一Agent] 状态已还原到上一工具调用前")
                elif undone_name == "write_narrative":
                    previous = undone.get("previous_narrative")
                    if previous is None:
                        ctx.pop("_finalize_narrative", None)
                    else:
                        ctx["_finalize_narrative"] = previous
                    previous_events = undone.get("previous_narrative_events")
                    if previous_events is None:
                        ctx.pop("_narrative_conditional_events", None)
                    else:
                        ctx["_narrative_conditional_events"] = previous_events
                    previous_timed = undone.get("previous_timed_event_ids")
                    if previous_timed is None:
                        ctx.pop("_narrative_timed_event_ids", None)
                    else:
                        ctx["_narrative_timed_event_ids"] = previous_timed
                else:
                    self._last_tool_result = None
                    return f"无法撤销上一个工具调用（{undone_name}）：没有可回退的状态"
                self._last_tool_result = None
                result = f"已撤销上一个工具调用（{undone_name}），状态已还原。原因：{reason}"
                result += "；此前基于候选状态的查询结果可能已失效，请重新查询后再继续推演。"
                if undone_name == "update_state":
                    result += self._state_projection_feedback(ctx)
                return result
            return "没有可撤销的操作"

        # Plan tool
        if name == "submit_plan":
            plan = args.get("plan", "")
            questions = args.get("questions", [])
            if not isinstance(plan, str) or not isinstance(questions, list):
                return json.dumps({"error": "计划必须是文字，问题必须是列表；请重新提交计划"}, ensure_ascii=False)
            for question in questions:
                if not isinstance(question, dict) or not isinstance(question.get("question"), str) or not question["question"].strip():
                    return json.dumps({"error": "每个问题都需要非空的问题文字；请重新提交计划"}, ensure_ascii=False)
                options = question.get("options")
                if (not isinstance(options, list) or not 2 <= len(options) <= 4
                        or any(not isinstance(option, str) or not option.strip() for option in options)):
                    return json.dumps({"error": "每个问题需要 2-4 个非空文字选项；请重新提交计划"}, ensure_ascii=False)
            ctx["_agent_plan"] = plan
            ctx["_agent_plan_questions"] = questions
            logger.info("[统一Agent] 计划: %s (问题: %d)", plan[:100], len(questions))
            if questions:
                q_text = "\n".join(f"Q: {q.get('question','')} 选项: {', '.join(q.get('options',[]))}" for q in questions[:3])
                return f"计划已记录：{plan}\n\n已向用户提出以下问题：\n{q_text}\n\n收到用户回复后请逐题按其选择执行；若当前流程无法等待回复，每题第一选项仅作你的默认方案，不要声称玩家已选择。"
            return f"计划已记录：{plan}\n继续执行。"

        # Narrative tool (dedicated creative writing)
        if name == "write_narrative":
            text = self._prepare_unified_narrative(args.get("text") or "", ctx)
            if not text:
                return json.dumps({"error": "叙事不能为空"}, ensure_ascii=False)
            if self._narrative_quotes_private_script(text):
                return json.dumps({"error": "叙事逐字复述了活跃剧本节点的幕后底稿，请依据玩家可见线索重写。"},
                                  ensure_ascii=False)
            if self._narrative_reveals_pending_consequence(ctx, text):
                return json.dumps({"error": "叙事提前揭露了尚未触发的后果，请重写。"}, ensure_ascii=False)
            ctx["_finalize_narrative"] = text
            ctx["_narrative_timed_event_ids"] = args.get("event_ids", [])
            ctx["_narrative_conditional_events"] = ctx.get(
                "_visible_conditioned_event_keys", ctx.get("_conditioned_event_keys", []))
            logger.info("[统一Agent] 叙事已提交 (%d字)", len(text))
            return f"叙事已记录（{len(text)}字）。请调用 finalize_turn 提交选项和摘要。"

        # Info tools
        if name == "query_lorebook":
            return await self._query_lorebook_for_turn(ctx, args)
        if name == "check_inventory" and ctx.get("_accepted_state_updates"):
            original_state = self.current_state
            try:
                self.current_state = self._project_state_tool_updates(ctx["_accepted_state_updates"])
                result = await self._run_tool_native_async(name, args)
            finally:
                self.current_state = original_state
            return "本轮投影（未提交）：" + result
        if name == "get_npc_attitude" and (self._npc_reaction_tool_calls or ctx.get("_accepted_state_updates")):
            projected = self._project_state_tool_updates(ctx.get("_accepted_state_updates", []))
            if self._npc_reaction_tool_calls:
                parsed = {}
                self._merge_npc_reaction_tool_results(parsed)
                changes = self._npc_attitude_to_state_changes(parsed["npc_attitude_changes"], state=projected)
                projected, _ = self.state_manager.apply_changes(projected, changes, inplace=True)
                self._sync_dirty_npc_attitudes(
                    projected, {change["target"].split(".")[2] for change in changes},
                )
            original_state = self.current_state
            try:
                self.current_state = projected
                result = await self._run_tool_native_async(name, args)
            finally:
                self.current_state = original_state
            return "本轮投影（未提交）：" + result
        if name in ("recall_history", "query_npc_history", "check_inventory", "get_npc_attitude"):
            return await self._run_tool_native_async(name, args)

        # Adaptive: send_message
        if name == "send_message":
            return self._handle_send_message(args)

        # Action tools
        if name == "update_state":
            return self._handle_update_state(args, ctx)
        if name == "manage_npcs":
            return self._handle_manage_npcs(args)

        # Output tools
        if name == "finalize_turn":
            return self._handle_finalize_turn(ctx, args)
        if name == "delegate_settlement":
            return await self._handle_delegate_settlement(ctx, args)

        # --- #4 叙事图谱遍历 ---
        if name == "traverse_graph":
            entity = args.get("entity", "")
            depth = args.get("depth", 2)
            # 先按 ID 查，再按名字查
            eid = entity
            if entity not in self.narrative_graph.nodes:
                for nid, ndata in self.narrative_graph.nodes.items():
                    if ndata.get("name") == entity:
                        eid = nid
                        break
            result = self.narrative_graph.query(eid, depth)
            return json.dumps(result, ensure_ascii=False, default=str)

        # --- #6 规则咨询 ---
        if name == "consult_rules":
            return self._handle_consult_rules(args, ctx)

        # --- #8 事件预演 ---
        if name == "peek_upcoming_events":
            return self._peek_upcoming_events_for_turn(ctx, args)

        # --- C2 因果链追踪 ---
        if name == "trace_causality":
            entity = args.get("entity", "")
            direction = args.get("direction", "both")
            depth = args.get("depth", 3)
            # 先按 ID 查，再按名字查
            eid = entity
            if entity not in self.narrative_graph.nodes:
                for nid, ndata in self.narrative_graph.nodes.items():
                    if ndata.get("name") == entity:
                        eid = nid
                        break
            result = {}
            if direction in ("causes", "both"):
                result["causes"] = self.narrative_graph.trace_causes(eid, depth)
            if direction in ("effects", "both"):
                result["effects"] = self.narrative_graph.trace_effects(eid, depth)
            return json.dumps(result, ensure_ascii=False, default=str)

        # --- C3 分支预演 ---
        if name == "preview_choice_outcome":
            if any(key in args for key in ("node_id", "choice_id", "offset", "snapshot", "result_path")):
                return self._preview_declared_choice(args, ctx)
            if args.get("include_candidates", False) is not False:
                return json.dumps({"error": "候选视图仅支持同时提供 node_id 和 choice_id 的声明预检"}, ensure_ascii=False)
            choice = args.get("choice_text", "")
            if not isinstance(choice, str) or not choice.strip():
                return json.dumps({"error": "请提供 node_id 和 choice_id，或非空 choice_text"}, ensure_ascii=False)
            # 规则推测风险
            risk_words = {"冒险": "risky", "直接": "moderate", "等待": "safe", "观察": "safe",
                          "追": "risky", "逃": "moderate", "询问": "moderate", "忽略": "safe"}
            risk = "moderate"
            for word, r in risk_words.items():
                if word in choice:
                    risk = r
                    break
            # 检查是否涉及已知 NPC
            npcs_involved = []
            for nid, ndata in self.current_state.get("npcs", {}).items():
                if isinstance(ndata, dict) and ndata.get("name", "") in choice:
                    att = ndata.get("attitude_toward_player", 50)
                    npcs_involved.append(f"{ndata['name']}(态度{att})")
            # 检查因果图
            causal_hint = ""
            if hasattr(self, 'narrative_graph'):
                for node_id, node in self.narrative_graph.nodes.items():
                    if node.get("name", "") in choice:
                        effects = self.narrative_graph.trace_effects(node_id, 2)
                        if effects:
                            causal_hint = f"历史因果：{effects[0].get('effect_name', '')}可能受影响"
                        break
            return json.dumps({
                "predicted_risk": risk,
                "npcs_involved": npcs_involved,
                "causal_hint": causal_hint,
                "note": "规则预测，仅供参考"
            }, ensure_ascii=False)

        # --- 导演笔记 ---
        if name == "read_director_notes":
            cats = None if args.get("category", "all") == "all" else [args["category"]]
            limit = args.get("limit", 5)
            notes = self.director_notes.get_notes(categories=cats, limit=limit)
            return json.dumps({"notes": notes}, ensure_ascii=False, default=str)

        # --- 角色独立推演 ---
        if name == "invoke_character":
            return await self._handle_invoke_character(args, ctx)

        # --- 线索推理 ---
        if name == "attempt_deduction":
            clue_ids = args.get("clue_ids", [])
            if len(clue_ids) < 2:
                return json.dumps({"valid": False, "message": "至少需要2条线索"})
            result = await self.attempt_deduction(clue_ids)
            return json.dumps(result, ensure_ascii=False)

        return json.dumps({"error": f"未知工具: {name}"})

    def _fork_query_session(self, base_state: dict):
        candidate = copy.copy(self)
        for name, value in self.__dict__.items():
            if isinstance(value, MethodType) and value.__self__ is self:
                candidate.__dict__[name] = MethodType(value.__func__, candidate)
        candidate.__dict__.update(copy.deepcopy({
            "current_state": base_state, "script": self.script,
            "script_variables": self.script_variables, "trigger_engine": self.trigger_engine,
            "story_tree_engine": self.story_tree_engine, "event_engine": self.event_engine,
            "state_manager": self.state_manager, "prompt_builder": self.prompt_builder,
            "event_scheduler": self.event_scheduler, "_event_def_by_id": self._event_def_by_id,
            "_random_item_by_id": self._random_item_by_id,
        }, {id(self): candidate}))
        candidate.ai_provider = candidate.vector_memory = candidate.data_bank = None
        candidate._background_tasks = set()
        candidate._index_tasks = set()
        candidate._deferred_tasks = set()
        candidate._deferred_lore_vector_ids = set()
        return candidate

    def _story_query_candidate(self, ctx: dict | None = None):
        from engine.event_scheduler import parse_time

        updates = ctx.get("_accepted_state_updates", []) if ctx is not None else []
        base_state = self._project_state_tool_updates(updates) if ctx is not None else self.current_state
        candidate = self._fork_query_session(base_state)
        end_time = next((update["end_time"] for update in reversed(updates) if update.get("end_time")), None)
        includes_scheduled = bool(ctx and ctx.get("old_time") and parse_time(end_time))
        events = []
        staged_item_ids = set()
        if includes_scheduled:
            candidate._settle_timed_events(ctx["old_time"], end_time, events, on_event=partial(
                candidate._settle_event_linked_dice, ctx=ctx,
                settled_item_ids=staged_item_ids, staged_only=True,
            ))
            candidate.current_state["game_time"] = end_time
        return candidate, {
            "event_ids": list(dict.fromkeys(event["event_id"] for event in events)),
            "scheduled_through": end_time if includes_scheduled else None,
            "excludes_unsettled_events": not includes_scheduled, "excludes_lifecycle": True,
            "staged_event_dice_ids": sorted(staged_item_ids),
            "excludes_event_dice": not bool(staged_item_ids), "excludes_unstaged_event_dice": True,
        }

    def _preview_declared_choice(self, args: dict, ctx: dict) -> str:
        node_id, choice_id = args.get("node_id"), args.get("choice_id")
        if not all(isinstance(value, str) and value.strip() for value in (node_id, choice_id)):
            return json.dumps({"error": "node_id 和 choice_id 必须同时提供非空字符串"}, ensure_ascii=False)
        include_candidates = args.get("include_candidates", False)
        if type(include_candidates) is not bool:
            return json.dumps({"error": "include_candidates 必须是布尔值"}, ensure_ascii=False)
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0:
            return json.dumps({"error": "offset 必须是非负整数"}, ensure_ascii=False)
        result_path = args.get("result_path")
        if "result_path" in args and (not isinstance(result_path, list) or any(
            not (isinstance(part, str) or type(part) is int and part >= 0) for part in result_path
        )):
            return json.dumps({"error": "result_path 必须是字符串键或非负整数索引组成的数组"}, ensure_ascii=False)
        supplied_snapshot = args.get("snapshot")
        if "snapshot" in args and (not isinstance(supplied_snapshot, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", supplied_snapshot)):
            return json.dumps({"error": "snapshot 必须原样使用预检返回的快照标识"}, ensure_ascii=False)
        restart = {"node_id": node_id, "choice_id": choice_id, "include_candidates": include_candidates}

        def restart_response(required=False):
            return json.dumps({
                "mode": "declared_choice", "node_id": node_id, "choice_id": choice_id,
                "error": "续读需要有效快照；丢弃旧片段按 restart 重新初查。节点或选项已变化时先用 consult_rules 重查 story_nodes。",
                "error_code": "snapshot_required" if required else "snapshot_expired",
                "restart_required": True, "restart": restart,
            }, ensure_ascii=False)

        if ("offset" in args or "result_path" in args) and supplied_snapshot is None:
            return restart_response(required=True)
        if include_candidates and not ctx.get("old_time"):
            if supplied_snapshot is not None:
                return restart_response()
            return json.dumps({"error": "候选预检仅支持进行中的回合"}, ensure_ascii=False)
        engine = self.story_tree_engine
        node = engine._nodes.get(node_id) if engine else None
        if node is None or node.get("type") != "choice":
            if supplied_snapshot is not None:
                return restart_response()
            return json.dumps({"error": "当前分支没有此剧情树选择节点；请用 consult_rules 查询 story_nodes"}, ensure_ascii=False)
        choice = next((item for item in node.get("choices", []) if item.get("id") == choice_id), None)
        if choice is None:
            if supplied_snapshot is not None:
                return restart_response()
            return json.dumps({"error": "此节点没有该 choice_id；请重新读取当前分支的 story_nodes 声明"}, ensure_ascii=False)

        candidate, event_projection = self._story_query_candidate(ctx if include_candidates else None)
        base_state = copy.deepcopy(candidate.current_state)
        before_lore = {entry.id: asdict(entry) for entry in candidate.prompt_builder.lorebook.entries}
        story_state = base_state.get("story_tree_state", {})
        was_active = node_id in story_state.get("active", [])
        previously_unlocked = set(story_state.get("unlocked", []))
        result = candidate._settle_story_choice(node_id, choice_id)
        available = bool(result.newly_completed)
        state = candidate.current_state if available else base_state
        after_lore = {entry.id: asdict(entry) for entry in candidate.prompt_builder.lorebook.entries}
        response = {
            "mode": "declared_choice", "source": "runtime_story_tree",
            "node_id": node_id, "choice_id": choice_id, "available": available,
            "projection": {
                "state_view": "candidate" if include_candidates else "committed",
                "effects_applied": available,
                **(event_projection if include_candidates else {"excludes_unsettled_events": True}),
                "scope": "choice_and_emitted_events", "persisted": False,
                "base_state_hash": self.world_tree.state_hash(base_state),
                "result_state_hash": self.world_tree.state_hash(state),
            },
            "completed": [entry["id"] for entry in result.newly_completed],
            "unlocked": sorted(set(state.get("story_tree_state", {}).get("unlocked", [])) - previously_unlocked),
            "state_changes": _choice_preview_changes(base_state, state) if available else [],
            "lore_changes": _choice_preview_changes(before_lore, after_lore) if available else [],
            "pending_effects": {
                key: value for key, value in asdict(result).items()
                if value and key not in {"newly_completed", "newly_active"}
            },
            "note": ("仅在隔离副本结算当前激活剧情树节点的选择及其派发事件，未保存或创建索引任务；"
                    "pending_effects 是直接声明效果，state_changes/lore_changes 是副本实际差异。"
                    + ("候选基线仅在接收 end_time 后纳入定时事件；差异不包含基线自身的变化。"
                       "仅复用已准备的事件骰子结果，不重新掷骰或执行完整回合生命周期，不提前激活节点；" if include_candidates
                       else "不含其他待结算事件或完整回合生命周期；")
                    + "不是NPC回应或玩家已知信息，不得写入玩家 hint。"
                    "状态变化后须重新预检，实际提交重新检查条件。"),
        }
        if not available:
            response["reason"] = "node_not_active" if not was_active else "condition_not_met"
        error = condition_error(choice.get("condition", ""))
        if error:
            response["condition_error"] = error
        content = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        revision = json.dumps({
            "save_id": self.save_id, "tree_id": self.world_tree.tree_id,
            "active_node_id": self.world_tree.active_node_id, "turn_number": self.turn_number,
            "state_view": include_candidates, "state": self.current_state, "base_state": base_state,
            "updates": ctx.get("_accepted_state_updates", []) if include_candidates else [],
            "old_time": ctx.get("old_time") if include_candidates else None,
            "script": self.script, "story_tree": engine.trees, "story_nodes": engine._nodes,
            "story_node_trees": engine._tree_for_node,
            "variables": self.script_variables.definitions, "triggers": self.trigger_engine.triggers,
            "events": {key: asdict(event) for key, event in self.event_engine.events.items()} if self.event_engine else {},
            "event_trees": self.event_engine._trees if self.event_engine else [],
            "event_node_trees": self.event_engine._tree_for_event if self.event_engine else {},
            "shadow_story_nodes": self.event_engine.shadow_story_nodes if self.event_engine else False,
            "state_rules": self.state_manager._attr_rules, "world_aliases": self.state_manager._world_aliases,
            "lore": before_lore, "content": content,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot = base64.urlsafe_b64encode(hashlib.sha256(revision.encode("utf-8")).digest()[:16]).decode("ascii").rstrip("=")
        if supplied_snapshot is not None and supplied_snapshot != snapshot:
            return restart_response()
        read = {**restart, "snapshot": snapshot, "offset": 0}
        metadata = {"snapshot": snapshot, "total_chars": len(content), "read": read}
        if "result_path" in args:
            read["result_path"] = result_path
            try:
                section = _json_result_section(json.loads(content), result_path, offset)
            except ValueError as error:
                return json.dumps({"error": str(error), "error_code": "invalid_result_path"}, ensure_ascii=False)
            return json.dumps({
                **{key: response[key] for key in ("mode", "node_id", "choice_id", "available", "projection")},
                **metadata, **section,
                "state_change_count": len(response["state_changes"]), "lore_change_count": len(response["lore_changes"]),
            }, ensure_ascii=False)
        if "offset" in args:
            if offset > len(content):
                return json.dumps({"error": "offset 超出预检正文长度"}, ensure_ascii=False)
            return json.dumps({
                **{key: response[key] for key in ("mode", "node_id", "choice_id", "available", "projection")},
                **metadata, "content_format": "json_fragment", "content": content[offset:offset + 2000],
                "offset": offset, "next_offset": offset + 2000 if offset + 2000 < len(content) else None,
                "state_change_count": len(response["state_changes"]), "lore_change_count": len(response["lore_changes"]),
            }, ensure_ascii=False)
        response.update(metadata)
        return json.dumps(response, ensure_ascii=False)

    def _projected_events(self, ctx: dict, *, with_state: bool = False,
                          base_state: dict | None = None,
                          settled_item_ids: set | None = None,
                          event_instructions: list[dict] | None = None) -> list[dict] | tuple[list[dict], dict]:
        if not ctx.get("old_time"):
            return ([], copy.deepcopy(base_state if base_state is not None else self.current_state)) if with_state else []
        from engine.event_scheduler import parse_time

        updates = ctx.get("_accepted_state_updates", [])
        end_time = next((update["end_time"] for update in reversed(updates)
                         if update.get("end_time")), ctx.get("new_time"))
        if not parse_time(end_time):
            return ([], copy.deepcopy(base_state if base_state is not None else self.current_state)) if with_state else []
        candidate = self._fork_query_session(
            base_state if base_state is not None else self._project_state_tool_updates(updates),
        )
        events = []
        _, _, scheduled_result = candidate._settle_timed_events(ctx["old_time"], end_time, events, on_event=partial(
            candidate._settle_event_linked_dice, ctx=ctx,
            settled_item_ids=settled_item_ids if settled_item_ids is not None else set(), staged_only=True,
        ))
        if event_instructions is not None:
            event_instructions.extend(scheduled_result.scheduled_instructions)
        candidate.current_state["game_time"] = end_time
        return (events, candidate.current_state) if with_state else events

    def _project_interaction_state(self, ctx: dict, *, event_instructions: list[dict] | None = None) -> tuple[list[dict], dict]:
        updates = ctx.get("_accepted_state_updates", [])
        if any(update.get("end_time") for update in updates):
            events, state = self._projected_events(ctx, with_state=True, event_instructions=event_instructions)
        else:
            events, state = [], self._project_state_tool_updates(updates)
        self._refresh_npc_locations(state)
        self._ensure_rooms_initialized(state)
        self._apply_npc_contact_changes(state, [
            change for update in updates for change in update.get("npc_contact_changes", [])
        ])
        return events, state

    def _projected_conditional_events(self, ctx: dict) -> list[dict]:
        candidates = ctx.get("conditional_event_candidates") or []
        if not candidates:
            return []
        candidate_keys = {(event["event_id"], event["fire_time"]) for event in candidates}
        return [event for event in self._projected_events(ctx)
                if (event["event_id"], event["fire_time"]) in candidate_keys]

    def _peek_upcoming_events_for_turn(self, ctx: dict, args: dict) -> str:
        updates = ctx.get("_accepted_state_updates", [])
        if not updates or not ctx.get("old_time"):
            return self._handle_peek_upcoming_events(args, ctx=ctx)
        staged_item_ids = set()
        end_time = next((update["end_time"] for update in reversed(updates)
                         if update.get("end_time")), None)
        if end_time:
            projected_events, projected_state = self._projected_events(
                ctx, with_state=True, settled_item_ids=staged_item_ids,
            )
        else:
            projected_events = []
            projected_state = self._project_state_tool_updates(updates)
        original_state = self.current_state
        try:
            self.current_state = projected_state
            result = json.loads(self._handle_peek_upcoming_events(args, ctx=ctx, settled_item_ids=staged_item_ids))
        finally:
            self.current_state = original_state
        if "error" not in result:
            result["note"] = result["note"].replace("当前存档条件", "本轮状态投影")
            result["projection"] = {
                "through": projected_state.get("game_time"),
                "event_ids": list(dict.fromkeys(event["event_id"] for event in projected_events)),
                "staged_event_dice_ids": sorted(staged_item_ids),
                "excludes_event_dice": not bool(staged_item_ids), "excludes_unstaged_event_dice": True,
                "provisional": True,
            }
        return json.dumps(result, ensure_ascii=False)

    def _project_lorebook_for_turn(self, projected_state: dict, events: list[dict]):
        lorebook = getattr(self.prompt_builder, "lorebook", None)
        if lorebook is None:
            return None
        original_dynamic_lore = {
            entry["id"]: entry for entry in self.current_state.get("dynamic_lorebook", []) if entry.get("id")
        }
        projected_dynamic_lore = {
            entry["id"]: entry for entry in projected_state.get("dynamic_lorebook", []) if entry.get("id")
        }
        dynamic_lore_changed = original_dynamic_lore != projected_dynamic_lore
        overrides_changed = any(projected_state.get(key, {}) != self.current_state.get(key, {}) for key in (
            "lorebook_entry_overrides", "lorebook_enabled_overrides",
        ))
        if not (events or dynamic_lore_changed or overrides_changed):
            return lorebook
        preview_lorebook = copy.deepcopy(lorebook)
        if dynamic_lore_changed:
            for entry_id in original_dynamic_lore.keys() - projected_dynamic_lore.keys():
                if entry_id not in self.prompt_builder._static_lore_ids:
                    preview_lorebook.remove_entry(entry_id)
            changed_entries = [
                entry for entry_id, entry in projected_dynamic_lore.items()
                if entry_id not in self.prompt_builder._static_lore_ids
                and entry != original_dynamic_lore.get(entry_id)
            ]
            if changed_entries:
                preview_lorebook.add_entries(changed_entries, replace_existing=True)
        for entry_id, entry in projected_state.get("lorebook_entry_overrides", {}).items():
            if entry is None:
                preview_lorebook.remove_entry(entry_id)
            else:
                preview_lorebook.add_entries([copy.deepcopy(entry)], replace_existing=True)
        for entry_id, enabled in projected_state.get("lorebook_enabled_overrides", {}).items():
            preview_lorebook.update_entry_enabled(entry_id, enabled)
        return preview_lorebook

    async def _query_lorebook_for_turn(self, ctx: dict, args: dict) -> str:
        updates = ctx.get("_accepted_state_updates", [])
        end_time = next((update["end_time"] for update in reversed(updates)
                         if update.get("end_time")), None)
        if not updates or getattr(self.prompt_builder, "lorebook", None) is None:
            return await self._run_tool_native_async("query_lorebook", args)
        if end_time and ctx.get("old_time"):
            events, projected_state = self._projected_events(ctx, with_state=True)
        else:
            events, projected_state = [], self._project_state_tool_updates(updates)
        if end_time:
            projected_state["game_time"] = end_time
        preview_lorebook = self._project_lorebook_for_turn(projected_state, events)

        result = preview_lorebook.query(
            keyword=args.get("keyword", ""), entry_id=args.get("entry_id", ""),
            page=args.get("page", 0), offset=args.get("offset", 0),
            excluded_ids=self.prompt_builder._excluded_lore_ids(projected_state, preview_lorebook),
            entry_ids=args.get("entry_ids"),
            snapshot=args.get("snapshot"),
            snapshot_scope=(self.world_tree.tree_id, self.world_tree.active_node_id,
                            self.current_state.get("game_time"), "candidate", updates),
        )
        if "error" not in result:
            result["projection"] = {
                "through": projected_state.get("game_time"),
                "event_ids": [event["event_id"] for event in events],
                "provisional": True,
                "note": "仅供本轮推演，不代表状态已结算或主角知情。",
            }
        return json.dumps(result, ensure_ascii=False)

    def _conditional_event_revision_feedback(self, ctx: dict, narrative: str) -> str:
        unlocked = self._projected_conditional_events(ctx)
        if not unlocked:
            ctx.pop("_conditional_event_revision", None)
            return ""
        event_keys = [(event["event_id"], event["fire_time"]) for event in unlocked]
        pending = ctx.get("_conditional_event_revision")
        if pending and pending["events"] == event_keys and pending["narrative"] != narrative:
            ctx.pop("_conditional_event_revision", None)
            return ""
        if not pending and ctx.get("_narrative_conditional_events") == event_keys:
            return ""
        ctx["_conditional_event_revision"] = {"events": event_keys, "narrative": narrative}
        return json.dumps({
            "error": "本轮状态变更使条件事件在结算时间前触发；请根据事件重写叙事，再调用 finalize_turn。",
            "events": [{"id": event["event_id"], "time": event["fire_time"],
                        "description": event.get("description", "")} for event in unlocked],
            "hint": "请先调用 write_narrative 修订；事件本身不代表主角知情，不得直接泄漏幕后信息。",
        }, ensure_ascii=False)

    def _narrative_claimed_timed_event_ids(self, ctx: dict, narrative: str) -> set[str]:
        return {event_id for event_id, definition in self._event_def_by_id.items()
                if any(claim in narrative for claim in definition.get("narrative_claims", []) if claim)}

    def _map_travel_revision_feedback(self, ctx: dict, narrative: str) -> str:
        destination = ctx.get("_map_travel_destination")
        if not destination:
            return ""
        name = (self.current_state.get("display_names") or {}).get(destination) or destination
        target = re.escape(name)
        player = r"(?:我|你|我们|玩家|主角)"
        failed = re.compile(player + r"(?:始终)?(?:没能|未能|没有|还没|还未|并未|无法|不能|不曾|未|没)(?:成功)?(?:进入|抵达|到达|走进)" + target)
        blocked = re.compile(player + r"(?:前往|赶往|去)" + target + r"[^。！？\n]{0,30}(?:途中受阻|仍留在原地)")
        arrived = re.compile(player + r"(?:已经|已|成功|终于|最终)?(?:进入|抵达|到达|走进)" + target)
        failed_claim = bool(failed.search(narrative) or blocked.search(narrative))
        arrival = arrived.search(failed.sub("", narrative))
        returned = bool(arrival and re.search(
            r"(?:离开|返回|回到|折返|退回|仍留在原地)", narrative[arrival.end():]))
        arrived_claim = bool(arrival) and not returned
        if failed_claim == arrived_claim and not returned:
            return ""
        settled_location = (self.current_state.get("player") or {}).get("location")
        for update in ctx.get("_accepted_state_updates", []):
            settled_location = update.get("location_change") or settled_location
        if (settled_location == destination) == arrived_claim:
            return ""
        return json.dumps({
            "error": "地图旅行叙事与位置结算矛盾；请先调用 write_narrative 修订，再调用 finalize_turn。",
            "destination": destination,
            "settled_location": settled_location,
        }, ensure_ascii=False)

    def _timed_event_revision_feedback(self, ctx: dict, narrative: str | None = None,
                                       *, settled_end_time: str | None = None,
                                       settled_state: dict | None = None) -> str:
        if not (settled_end_time or ctx.get("old_time") or ctx.get("new_time")):
            return ""
        from engine.event_scheduler import parse_time

        end_time = settled_end_time or next((update["end_time"] for update in reversed(
            ctx.get("_accepted_state_updates", [])) if update.get("end_time")), ctx.get("new_time"))
        finish = parse_time(end_time)
        if narrative is None:
            narrative = ctx.get("_finalize_narrative", "")
        referenced = (set(ctx.get("_narrative_timed_event_ids") or [])
                      | self._narrative_claimed_timed_event_ids(ctx, narrative))
        if not referenced or not finish:
            return ""
        if settled_state is None:
            _, settled_state = self._projected_events(ctx, with_state=True)
        occurred_ids = set(settled_state.get("fired_one_time_events", []))
        occurred_ids.update(event_id for event_id, tracker in settled_state.get("cyclic_event_trackers", {}).items()
                            if (last_fired := parse_time(tracker.get("last_fired"))) and last_fired <= finish)
        excluded = []
        for event_id in sorted(referenced - occurred_ids):
            definition = self._event_def_by_id.get(event_id, {})
            fire_time = next((event.get("fire_time") for event in ctx.get("triggered_events", [])
                              if event["event_id"] == event_id),
                             definition.get("trigger_time") or definition.get("first_trigger"))
            excluded.append({"id": event_id, **({"time": fire_time} if fire_time else {})})
        if not excluded:
            return ""
        return json.dumps({
            "error": "叙事引用了截至结算时间尚未发生的定时事件；请按实际事件重写叙事，再调用 finalize_turn。",
            "end_time": end_time,
            "excluded_events": excluded,
            "hint": "请先调用 write_narrative 修订；核对事件声明、触发条件和时间，不得把未发生的事件写成事实。",
        }, ensure_ascii=False)

    def _invalid_lore_reveals(self, ctx: dict, args: dict) -> list[str]:
        if not args.get("lore_reveals"):
            return []
        narrative = args.get("narrative") or ctx.get("_finalize_narrative", "")
        end_time = args.get("end_time") or next((update["end_time"] for update in reversed(
            ctx.get("_accepted_state_updates", [])) if update.get("end_time")), None)
        updates = list(ctx.get("_accepted_state_updates", []))
        final_update = {key: args[key] for key in (*self._UPDATE_STATE_RES_KEYS, *self._UPDATE_STATE_SPATIAL_KEYS)
                        if key in args}
        if final_update:
            updates.append(final_update)
        projected = self._project_state_tool_updates(updates) if updates else self.current_state
        reactions = list(self._npc_reaction_tool_calls)
        for operation in args.get("npc_operations", []):
            if not isinstance(operation, dict) or operation.get("op") != "attitude":
                continue
            npc_id = operation.get("npc_id", "")
            dimension = operation.get("dimension", "")
            change = operation.get("change", 0)
            if (npc_id not in self._npc_by_id and npc_id not in self.current_state.get("npcs", {})) or (
                    dimension not in self._VALID_ATTITUDE_DIMENSIONS or type(change) is not int):
                continue
            reactions.append({"npc_id": npc_id, "dimension": dimension,
                              "change": max(-self._MAX_ATTITUDE_DELTA, min(self._MAX_ATTITUDE_DELTA, change)),
                              "reason": operation.get("reason", "")})
        if reactions:
            unique_reactions = {}
            for reaction in reactions:
                unique_reactions.setdefault((reaction["npc_id"], reaction["dimension"]), reaction)
            projected = copy.deepcopy(projected)
            changes = self._npc_attitude_to_state_changes(list(unique_reactions.values()), state=projected)
            projected, _ = self.state_manager.apply_changes(projected, changes, inplace=True)
            self._sync_dirty_npc_attitudes(projected, {change["target"].split(".")[2] for change in changes})
        events = []
        if ctx.get("old_time") and (end_time or ctx.get("new_time")):
            events, projected = self._projected_events(
                {"old_time": ctx["old_time"], "new_time": end_time or ctx["new_time"]},
                with_state=True, base_state=projected,
            )
        lorebook = self._project_lorebook_for_turn(projected, events)
        return [str(claim.get("entry_id", "?")) if isinstance(claim, dict) else "?"
                for claim in args.get("lore_reveals", [])
                if self._validated_npc_lore_reveal(
                    projected, claim, narrative, ctx.get("present_npc_ids"),
                    turn_start_time=ctx.get("old_time"), turn_end_time=end_time,
                    lorebook=lorebook,
                ) is None]

    def _narrative_quotes_private_script(self, narrative: str) -> bool:
        return any(brief["status"] == "active" and len(brief["description"]) >= 20
                   and brief["description"] in narrative for brief in self._script_anchor_briefs())

    def _narrative_reveals_pending_consequence(self, ctx: dict, narrative: str,
                                               *, state: dict | None = None) -> bool:
        triggered = [consequence for consequence in ctx.get("triggered_consequences") or []
                     if isinstance(consequence, dict)]
        triggered_ids = {consequence.get("id") for consequence in triggered if consequence.get("id")}
        triggered_descriptions = {consequence.get("description") for consequence in triggered
                                  if consequence.get("description")}
        state = state if state is not None else self.current_state
        consequences = list(state.get("pending_consequences") or [])
        event_sections = ctx.get("event_sections") or {}
        if isinstance(event_sections, dict) and event_sections.get("consequences"):
            consequences.append(event_sections["consequences"])
        for consequence in consequences:
            if isinstance(consequence, dict):
                if consequence.get("id") in triggered_ids and consequence.get("id"):
                    continue
                description = consequence.get("description", "")
            else:
                description = consequence
            if not isinstance(description, str) or description in triggered_descriptions:
                continue
            for part in re.split(r"[。！？!?；;，,\n]", description):
                part = part.strip()
                if (len(part) >= 6 and part in narrative
                        and not any(part in triggered_description
                                    for triggered_description in triggered_descriptions)):
                    return True
        return False

    def _invalid_choice_references(self, ctx: dict, args: dict,
                                   *, state: dict | None = None) -> list[str]:
        narrative = args.get("narrative") or ctx.get("_finalize_narrative", "")
        state = state if state is not None else self.current_state
        protected = []
        for npc_id, npc in state.get("npcs", {}).items():
            if isinstance(npc, dict) and npc.get("known", self._npc_by_id.get(npc_id, {}).get("known", True)) is False:
                name = npc.get("name") or self._npc_by_id.get(npc_id, {}).get("name", "")
                if name:
                    protected.append((name, "人物"))
        if self.story_tree_engine:
            story_state = state.get("story_tree_state") or {}
            available = (set(story_state.get("completed", []))
                         | set(story_state.get("active", []))
                         | set(story_state.get("unlocked", [])))
            active = set(story_state.get("active", []))
            for node_id, node in self.story_tree_engine._nodes.items():
                name = node.get("name", "")
                if node_id not in available and isinstance(name, str) and len(name) >= 4:
                    protected.append((name, "剧情"))
                description = node.get("description", "")
                if node_id in active and isinstance(description, str) and len(description) >= 20:
                    protected.append((description[:500], "剧情幕后底稿"))
        consequences = list(state.get("pending_consequences") or [])
        event_sections = ctx.get("event_sections") or {}
        if isinstance(event_sections, dict) and event_sections.get("consequences"):
            consequences.append(event_sections["consequences"])
        for consequence in consequences:
            description = consequence.get("description", "") if isinstance(consequence, dict) else consequence
            if isinstance(description, str):
                protected.extend((part.strip(), "后果") for part in re.split(r"[。！？!?；;，,\n]", description)
                                 if len(part.strip()) >= 6)

        invalid = []
        for index, choice in enumerate(args.get("choices") or [], start=1):
            for field in ("text", "hint", "time_hint"):
                value = choice.get(field, "")
                if not isinstance(value, str):
                    continue
                for secret, category in protected:
                    if secret not in narrative and secret in value:
                        invalid.append(f"第{index}项{field}引用未公开{category}")
                        break
        return invalid

    def _filter_player_visible_choices(self, choices: list[dict], narrative: str,
                                       *, state: dict | None = None,
                                       ctx: dict | None = None) -> list[dict]:
        return [choice for choice in choices if isinstance(choice, dict)
                and not self._invalid_choice_references(
                    ctx or {}, {"narrative": narrative, "choices": [choice]}, state=state,
                )]

    def _prepare_unstaged_event_dice(self, ctx: dict) -> list[dict]:
        from engine.event_scheduler import parse_time

        staged_ids = set(ctx.get("candidate_event_dice_state", {}))
        if not ctx.get("old_time") or not any(
            item.get("trigger_type") == "event_linked" and item["id"] not in staged_ids
            for item in self.script.get("random_items", [])
        ):
            return []
        updates = ctx.get("_accepted_state_updates", [])
        end_time = next((update["end_time"] for update in reversed(updates)
                         if update.get("end_time")), ctx.get("new_time"))
        if not parse_time(end_time):
            return []
        candidate = self._fork_query_session(self._project_state_tool_updates(updates))
        candidate._settle_timed_events(ctx["old_time"], end_time, [], on_event=partial(
            candidate._settle_event_linked_dice, ctx=ctx, settled_item_ids=set(), stage_unstaged=True,
        ))
        return [{
            "random_item_id": item_id,
            "result": self._dice_result_to_dict(result) if (
                result := ctx.get("candidate_event_dice_results", {}).get(item_id)
            ) is not None else None,
            "state": copy.deepcopy(ctx["candidate_event_dice_state"][item_id]),
        } for item_id in sorted(set(ctx.get("candidate_event_dice_state", {})) - staged_ids)]

    def _handle_finalize_turn(self, ctx: dict, args: dict) -> str:
        """Synchronous settlement: narrative + state + choices + summary in one call."""
        error = self._validate_variable_changes(args, ctx)
        if error:
            return error
        if not (args.get("narrative") or ctx.get("_finalize_narrative", "")).strip():
            return json.dumps({"error": "请先调用 write_narrative 提交叙事，再调用 finalize_turn。"}, ensure_ascii=False)
        if self._narrative_reveals_pending_consequence(
                ctx, args.get("narrative") or ctx.get("_finalize_narrative", "")):
            return json.dumps({"error": "叙事提前揭露了尚未触发的后果，请重写。"}, ensure_ascii=False)
        invalid = self._invalid_lore_reveals(ctx, args)
        if invalid:
            return json.dumps({"error": f"词条揭露未通过叙事/来源核验: {', '.join(invalid)}；请修正叙事或移除声明后重试。"}, ensure_ascii=False)
        invalid_choices = self._invalid_choice_references(ctx, args)
        if invalid_choices:
            return json.dumps({"error": f"选项包含玩家尚未知晓的信息: {', '.join(invalid_choices)}；请根据已呈现叙事修正选项后重试。"}, ensure_ascii=False)
        state_args = {k: args[k] for k in ("end_time", "state_changes", "variable_changes", "inventory_changes",
                      "activate_states", "location_change", "reveal_locations") if k in args}
        previous_event_keys = ctx.get("_conditioned_event_keys")
        state_result = self._handle_update_state(state_args, ctx, defer_event_instructions=True) if state_args else ""
        if state_result.startswith("错误:"):
            return state_result
        prepared_dice = self._prepare_unstaged_event_dice(ctx)
        narrative = (args.get("narrative") or ctx.get("_finalize_narrative", "")).strip()
        map_travel_feedback = self._map_travel_revision_feedback(ctx, narrative)
        feedback = (self._timed_event_revision_feedback(ctx, narrative)
                    or self._conditional_event_revision_feedback(ctx, narrative)
                    or map_travel_feedback)
        if prepared_dice:
            response = json.loads(feedback) if feedback else {
                "error": "本轮新触发事件的骰子结果已准备但未提交；请核对结果和事件，确认或修订叙事后重新调用 finalize_turn。",
            }
            response.update(error_code="event_dice_prepared", prepared_event_dice=prepared_dice, provisional=True,
                            state_update_accepted=False,
                            retry_hint="本次结算参数未接收，重试时须重传；骰子结果本轮固定，不重新掷骰，不代表事件已发生或主角知情。")
            feedback = json.dumps(response, ensure_ascii=False)
        event_instructions = []
        self._projected_events(ctx, event_instructions=event_instructions)
        if any(instruction not in ctx.get("_known_event_instructions", []) for instruction in event_instructions):
            response = json.loads(feedback) if feedback else {
                "error": "本轮定时事件有尚未反馈的叙事指令；请阅读后确认或修订正文，再调用 finalize_turn。",
            }
            response.setdefault("error_code", "event_instructions_available")
            response.update(event_instructions=event_instructions, provisional=True, state_update_accepted=False,
                            retry_hint="本次结算参数未接收，重试须重传；这些幕后指令不代表主角知情，不应逐字展示给玩家。")
            feedback = json.dumps(response, ensure_ascii=False)
            ctx["_known_event_instructions"] = copy.deepcopy(event_instructions)
        if feedback:
            if map_travel_feedback:
                ctx["_map_travel_revision_feedback"] = map_travel_feedback
            if state_args and "_accepted_state_updates" in ctx:
                ctx["_accepted_state_updates"].pop()
                if previous_event_keys is None:
                    ctx.pop("_conditioned_event_keys", None)
                else:
                    ctx["_conditioned_event_keys"] = previous_event_keys
            return feedback
        ctx.pop("_map_travel_revision_feedback", None)
        # Narrative（通过工具提交，确保不丢失）
        narrative = args.get("narrative", "")
        if narrative:
            ctx["_finalize_narrative"] = narrative

        results = []

        # State changes
        if state_args:
            results.append(state_result)

        # NPC operations
        npc_ops = args.get("npc_operations", [])
        if npc_ops:
            results.append(self._handle_manage_npcs({"operations": npc_ops}))

        # Choices
        choices = args.get("choices", [])
        if choices:
            self._choices_tool_calls = []
            for ch in choices:
                self._run_choices_tool("add_choice", ch)

        # Summary and image
        self._turn_summary_override = args.get("summary", "")
        if args.get("image_prompt"):
            self._scene_image_prompt_override = {
                "prompt": args["image_prompt"],
                "style": args.get("image_style", "realistic"),
            }

        # D2: proactive_hint
        hint = args.get("proactive_hint", "")
        if hint:
            ctx["_proactive_hint"] = hint

        return ("已同步完成本轮结算；词条声明仍需在最终状态和叙事中复核"
                if args.get("lore_reveals") else "已同步完成本轮结算")

    async def _handle_delegate_settlement(self, ctx: dict, args: dict) -> str:
        """Request settlement within this turn before its state is committed."""
        if not (args.get("narrative") or ctx.get("_finalize_narrative", "")).strip():
            return json.dumps({"error": "请先调用 write_narrative 提交叙事，再委托结算。"}, ensure_ascii=False)
        if self._narrative_reveals_pending_consequence(
                ctx, args.get("narrative") or ctx.get("_finalize_narrative", "")):
            return json.dumps({"error": "叙事提前揭露了尚未触发的后果，请重写。"}, ensure_ascii=False)
        invalid = self._invalid_lore_reveals(ctx, args)
        if invalid:
            return json.dumps({"error": f"词条揭露未通过叙事/来源核验: {', '.join(invalid)}；请修正叙事或移除声明后重试。"}, ensure_ascii=False)
        invalid_choices = self._invalid_choice_references(ctx, args)
        if invalid_choices:
            return json.dumps({"error": f"选项包含玩家尚未知晓的信息: {', '.join(invalid_choices)}；请根据已呈现叙事修正选项后重试。"}, ensure_ascii=False)
        # Narrative
        narrative = args.get("narrative", "")
        if narrative:
            ctx["_finalize_narrative"] = narrative

        # Save choices and summary immediately
        choices = args.get("choices", [])
        if choices:
            self._choices_tool_calls = []
            for ch in choices:
                self._run_choices_tool("add_choice", ch)
        self._turn_summary_override = args.get("summary", "")

        # D2: proactive_hint
        proactive = args.get("proactive_hint", "")
        if proactive:
            ctx["_proactive_hint"] = proactive

        self._delegated_settlement = True
        ctx["_delegated_settlement_hint"] = args.get("hint", "")
        return ("已接收结算委托。词条声明仍需在最终状态和叙事中复核。"
                if args.get("lore_reveals") else "已接收结算委托。叙事与结算核验完成后保存本轮状态。")

    def _handle_consult_rules(self, args: dict, ctx: dict | None = None) -> str:
        """纯规则检查（不调 LLM）：NPC 在场、物品持有等。"""
        if "category" in args:
            return self._handle_mechanics_query(args, ctx)
        if "view" in args or "node_status" in args:
            return json.dumps({"error": "view 和 node_status 须指定 category"}, ensure_ascii=False)
        if "result_path" in args:
            return json.dumps({"error": "result_path 须指定 category、rule_index 和条目 snapshot"}, ensure_ascii=False)
        include_candidates = args.get("include_candidates", False)
        if type(include_candidates) is not bool:
            return json.dumps({"error": "include_candidates 必须是布尔值"}, ensure_ascii=False)
        if include_candidates and not (ctx or {}).get("old_time"):
            return json.dumps({"error": "候选在场、接触与物品查询仅支持进行中的回合"}, ensure_ascii=False)
        if "snapshot" in args:
            return json.dumps({"error": "snapshot 仅用于指定 category 的规则查询"}, ensure_ascii=False)
        question = args.get("question", "").lower()
        events = []
        if include_candidates:
            events, state = self._project_interaction_state(ctx)
        else:
            state = copy.deepcopy(self.current_state)

        answers = []
        contacts = {}
        if "接触" in question:
            self._ensure_rooms_initialized(state)
            for npc_id, npc in state.get("npcs", {}).items():
                if (isinstance(npc, dict) and (npc_id.lower() in question
                        or (npc.get("name") and npc["name"].lower() in question))):
                    contacts[npc_id] = {"in_contact": self._npc_contact_is_current(npc_id, state),
                                        "block_reason": self._npc_contact_block_reason(npc_id, state)}
                    answers.append(f"{npc.get('name', npc_id)} " + ("已接触" if contacts[npc_id]["in_contact"] else "未接触"))

        # NPC 在场检查
        if "在场" in question or "在这" in question:
            present, nearby = self._compute_present_npcs(state)
            for npc in self.script.get("npcs", []):
                if npc.get("name") and npc["name"].lower() in question:
                    loc = self._get_npc_location(npc["id"], state)
                    status = ("在场" if npc["id"] in present else
                              "附近（不同房间）" if npc["id"] in nearby else "不在场")
                    answers.append(f"{npc['name']} {status}（当前位置：{loc}）")

        # 物品检查
        if "可用" in question or "有没有" in question:
            inv = state.get("inventory", [])
            for item in inv:
                item_name = item.get("item", "") if isinstance(item, dict) else str(item)
                if item_name and item_name.lower() in question:
                    answers.append(f"持有 {item_name}")

        if not answers:
            answers.append("无法确定，请根据叙事自行判断")

        result = {"answers": answers, "game_time": state.get("game_time")}
        if "接触" in question:
            result["contacts"] = contacts
        if include_candidates:
            result["projection"] = {
                "through": state.get("game_time"), "provisional": True,
                "scope": "accepted_updates_and_scheduled_events",
                "event_ids": list(dict.fromkeys(event["event_id"] for event in events)),
                "note": "只读已接收更新及候选结束时间内的定时事件，随后刷新日程、房间并重算接触；不含完整回合生命周期。"
                        "未提交，非角色知情；在场不等于获准互动。更新、撤销、切换分支后须重新查询。",
            }
        return json.dumps(result, ensure_ascii=False)

    def _story_node_runtime(self, definition: dict, state: dict) -> dict:
        node = definition["node"]
        node_id = node["id"]
        story_state = state.get("story_tree_state", {})
        active = node_id in story_state.get("active", [])
        status = ("completed" if node_id in story_state.get("completed", []) else
                  "active" if active else "unlocked" if node_id in story_state.get("unlocked", []) else "locked")
        result = {
            "tree_id": definition["tree"]["id"], "node_id": node_id,
            "name": node.get("name", node_id), "type": node.get("type", "auto"), "status": status,
            "chosen_choice_id": story_state.get("choices_made", {}).get(node_id), "choices": [],
        }
        if node.get("type") == "choice":
            for choice in node.get("choices", []):
                condition = choice.get("condition", "")
                reason = ("node_not_active" if not active else
                          "condition_not_met" if not self._evaluate_condition(condition, state) else None)
                choice_view = {
                    "choice_id": choice["id"], "label": choice.get("label", choice["id"]),
                    "available": reason is None, "reason": reason,
                }
                error = condition_error(condition)
                if error:
                    choice_view["condition_error"] = error
                result["choices"].append(choice_view)
        if node.get("type") == "timed":
            result["timed_progress"] = story_state.get("timed_progress", {}).get(node_id)
        if node.get("type") == "periodic":
            result["cooldown_remaining_turns"] = max(
                0, story_state.get("periodic_cooldowns", {}).get(node_id, 0) - self.turn_number,
            )
        return result

    def _queryable_mechanics_definitions(self):
        npcs = list(self.script.get("npcs", []))
        schedule_overrides = self.current_state.get("npc_schedule_overrides", {})
        npcs.extend({"id": npc_id, "name": npc.get("name", npc_id), "goals": npc.get("goals", [])}
                    for npc_id, npc in self.current_state.get("npcs", {}).items()
                    if npc_id not in self._npc_by_id and isinstance(npc, dict))
        return {
            **self._mechanics_definitions(),
            "one_time_events": self.script.get("one_time_events", []),
            "cyclic_events": self.script.get("cyclic_events", []),
            "persistent_states": self.script.get("persistent_states", []),
            "world_properties": self.script.get("world_properties", []),
            "locations": self.script.get("locations", []),
            "npc_schedules": [
                {"npc_id": npc["id"], "npc_name": npc.get("name", npc["id"]),
                 "schedule": npc.get("schedule") or [], "overrides": schedule_overrides.get(npc["id"]) or []}
                for npc in self.script.get("npcs", [])
                if npc.get("schedule") or schedule_overrides.get(npc["id"])
            ],
            **{category: [
                {"actor_id": actor.get("id", ""), "actor_name": actor.get("name", actor.get("id", "")), "goal": goal}
                for actor in actors for goal in (actor.get("goals") or [])
            ] for category, actors in (("npc_goals", npcs), ("org_goals", self.script.get("organizations", [])))},
            "story_nodes": [
                {"tree": {key: value for key, value in tree.items() if key != "nodes"}, "node": node}
                for tree in (self.story_tree_engine.trees if self.story_tree_engine else [])
                for node in tree.get("nodes", [])
            ],
        }

    def _handle_mechanics_query(self, args: dict, ctx: dict | None = None) -> str:
        definitions = self._queryable_mechanics_definitions()
        category = args.get("category")
        if not isinstance(category, str) or category not in definitions:
            return json.dumps({"error": "category 必须是：" + "、".join(definitions)}, ensure_ascii=False)
        view = args.get("view", "definition")
        if view not in ("definition", "runtime") or (view == "runtime" and category != "story_nodes"):
            return json.dumps({"error": "view 必须是 definition 或 runtime；runtime 仅支持 story_nodes"}, ensure_ascii=False)
        runtime = view == "runtime"
        node_status = args.get("node_status")
        if "node_status" in args and (not runtime or node_status not in ("active", "completed", "unlocked", "locked") or "rule_index" in args):
            return json.dumps({"error": "node_status 仅用于运行态列表筛选：active、completed、unlocked、locked"}, ensure_ascii=False)
        view_args = {"view": view} if runtime else {}
        list_args = {**view_args, **({"node_status": node_status} if node_status is not None else {})}
        include_candidates = args.get("include_candidates", False)
        if type(include_candidates) is not bool:
            return json.dumps({"error": "include_candidates 必须是布尔值"}, ensure_ascii=False)
        state_candidates = category in ("variables", "world_properties") and "_accepted_state_updates" in (ctx or {})
        if include_candidates and (not (runtime or category in ("variables", "world_properties"))
                                   or not ((ctx or {}).get("old_time") or state_candidates)):
            return json.dumps({"error": "候选值仅支持进行中回合的 variables、world_properties 或 story_nodes 运行态查询；"
                                       "局部状态重生成仅支持 variables、world_properties"}, ensure_ascii=False)
        runtime_session = self
        event_projection = {}
        projected_state = None
        if include_candidates:
            if runtime:
                runtime_session, event_projection = self._story_query_candidate(ctx)
                projected_state = runtime_session.current_state
                definitions = runtime_session._queryable_mechanics_definitions()
            else:
                projected_state = self._project_state_tool_updates(ctx.get("_accepted_state_updates", []))
        category_definitions = list(definitions[category])
        if include_candidates and category == "world_properties":
            declared_ids = {definition["id"] for definition in category_definitions}
            runtime_ids = dict.fromkeys([
                *projected_state.get("world_properties", {}), *ctx.get("_state_feedback_world_property_ids", []),
            ])
            category_definitions.extend({"id": property_id} for property_id in runtime_ids if property_id not in declared_ids)
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0:
            return json.dumps({"error": "offset 必须是非负整数"}, ensure_ascii=False)
        query = args.get("query", "")
        if not isinstance(query, str):
            return json.dumps({"error": "query 必须是字符串"}, ensure_ascii=False)
        detailed = "rule_index" in args
        rule_index = args.get("rule_index")
        if detailed and (type(rule_index) is not int or rule_index < 0):
            return json.dumps({"error": "rule_index 超出当前类别的范围"}, ensure_ascii=False)
        if detailed and query:
            return json.dumps({"error": "rule_index 不能与 query 混用；切换分支后请重新查询索引"}, ensure_ascii=False)
        scoped = "result_path" in args
        result_path = args.get("result_path")
        if scoped and (not isinstance(result_path, list) or any(
                not isinstance(part, str) and not (type(part) is int and part >= 0) for part in result_path)):
            return json.dumps({"error": "result_path 必须是字符串键和非负整数索引组成的列表"}, ensure_ascii=False)
        if scoped and not detailed:
            return json.dumps({"error": "result_path 须指定 rule_index 和条目 snapshot，不能用于列表翻页"}, ensure_ascii=False)
        supplied_snapshot = args.get("snapshot")
        if "snapshot" in args and (not isinstance(supplied_snapshot, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", supplied_snapshot)):
            return json.dumps({"error": "snapshot 必须原样使用查询返回的快照标识"}, ensure_ascii=False)

        completed_goals = {
            (record["id"] if isinstance(record, dict) else record): record
            for record in self.current_state.get(f"completed_{category}", [])
        } if category in ("npc_goals", "org_goals") else {}
        records = []
        for index, definition in enumerate(category_definitions):
            payload = {"definition": definition}
            if category in ("npc_goals", "org_goals"):
                goal_key = f"{definition['actor_id']}:{definition['goal'].get('id', '')}"
                payload.update({
                    "goal_key": goal_key, "status": "completed" if goal_key in completed_goals else "pending",
                    "completion": completed_goals.get(goal_key),
                })
            if runtime:
                payload = runtime_session._story_node_runtime(definition, projected_state if include_candidates else self.current_state)
            if category in CONDITION_RULE_SECTIONS:
                error = condition_error(definition.get("condition", ""))
                if error:
                    payload["condition_error"] = error
            if category == "variables":
                payload["current_value"] = self.script_variables.get(self.current_state, definition["id"])
            elif category == "world_properties":
                payload["current_value"] = self.current_state.get("world_properties", {}).get(definition["id"])
            if include_candidates and not runtime:
                if category == "variables":
                    payload["candidate_value"] = self.script_variables.get(projected_state, definition["id"])
                    payload["candidate_exists"] = True
                else:
                    properties = projected_state.get("world_properties", {})
                    payload["candidate_value"] = properties.get(definition["id"])
                    payload["candidate_exists"] = definition["id"] in properties
                    if index >= len(definitions[category]):
                        payload.update({"definition": None, "id": definition["id"]})
            content = json.dumps(payload, ensure_ascii=False)
            records.append((index, content))
        projection = None
        if include_candidates:
            updates = ctx.get("_accepted_state_updates", [])
            projection = {
                "provisional": True, "accepted_updates": len(updates), "excludes_unsettled_events": True,
                "as_of": next((update["end_time"] for update in reversed(updates) if update.get("end_time")),
                              ctx.get("old_time", self.current_state.get("game_time"))),
                **event_projection,
            }
        revision = hashlib.sha256(json.dumps([
            self.world_tree.tree_id, self.world_tree.active_node_id, self.current_state.get("game_time"),
            category, include_candidates, projection, records,
            *([view, self.turn_number, self.current_state, projected_state, category_definitions] if runtime else []),
        ], ensure_ascii=False).encode("utf-8")).hexdigest()

        def snapshot_for(index=None):
            scope = [revision, "list", query.casefold(), *([node_status] if runtime else [])] if index is None else [revision, "entry", index]
            digest = hashlib.sha256(json.dumps(scope).encode("utf-8")).digest()[:16]
            return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        snapshot = snapshot_for(rule_index if detailed else None)
        if (((detailed or offset > 0) and supplied_snapshot is None)
                or (supplied_snapshot is not None and supplied_snapshot != snapshot)):
            return json.dumps({
                "error": "快照缺失或已失效；丢弃旧正文片段，按 restart 从列表首屏重查，再携带新快照完整读取。",
                "error_code": "snapshot_required" if supplied_snapshot is None else "snapshot_expired",
                "restart_required": True,
                "restart": {"category": category, "query": query, "include_candidates": include_candidates, "offset": 0, **list_args},
            }, ensure_ascii=False)
        if detailed and rule_index >= len(records):
            return json.dumps({"error": "rule_index 超出当前类别的范围"}, ensure_ascii=False)
        records = [record for record in records if record[0] == rule_index] if detailed else [
            record for record in records if query.casefold() in record[1].casefold()
            and (node_status is None or json.loads(record[1])["status"] == node_status)
        ]
        page = records if detailed else records[offset:offset + 5]
        content_offset = offset if detailed else 0
        if detailed and not scoped and content_offset > len(page[0][1]):
            return json.dumps({"error": "offset 超出机制正文长度"}, ensure_ascii=False)
        if scoped:
            index, content = page[0]
            try:
                summary_keys = ("choice_id", "available", "reason") if runtime and result_path[:1] == ["choices"] else ()
                section = _json_result_section(json.loads(content), result_path, offset, summary_keys)
            except ValueError as error:
                return json.dumps({"error": str(error)}, ensure_ascii=False)
            entries = [{
                "rule_index": index, "snapshot": snapshot, "total_chars": len(content), **section,
                "read": {"category": category, "rule_index": index, "snapshot": snapshot,
                         "include_candidates": include_candidates, "result_path": result_path, "offset": 0, **view_args},
            }]
        else:
            entries = [{
                "rule_index": index, "content": content[content_offset:content_offset + 2000],
                "snapshot": snapshot_for(index),
                "offset": content_offset, "total_chars": len(content),
                "next_offset": content_offset + 2000 if len(content) > content_offset + 2000 else None,
            } for index, content in page]
            if runtime:
                for entry in entries:
                    entry["read"] = {
                        "category": category, "rule_index": entry["rule_index"], "snapshot": entry["snapshot"],
                        "include_candidates": include_candidates, "offset": 0, **view_args,
                    }
        result = {
            "category": category, "source": "current_branch", "game_time": self.current_state.get("game_time"),
            "notice": "当前分支定义与现值；不模拟未提交的行动，不代表机制已触发或角色已知。",
            "entries": entries, "total": len(records), "offset": offset, "snapshot": snapshot, **list_args,
            "next_offset": offset + 5 if not detailed and len(records) > offset + 5 else None,
        }
        if category == "npc_schedules":
            result["notice"] = (
                "当前分支已注册角色的完整基础 schedule 与正式状态中的 overrides；包含已到期覆盖，不表示正在生效。"
                "不判断条件、优先级或到期，不解析位置、执行日程或纳入候选更新，非角色知情。"
                "需要正式或候选在场、接触时，另用 question 查询；声明不能作为已发生或未来必然发生的事实。"
            )
        if category in ("npc_goals", "org_goals"):
            result["notice"] = (
                "当前分支完整目标声明与正式完成记录；goal_key 为 actor_id:目标id。"
                "pending 仅表示没有完成记录，不检查当前条件，也不预判本轮未结算结果。"
                "查询不执行效果、推进离屏进度或更新状态，非角色知情；NPC 与组织仍使用各自既有结算规则。"
            )
        if include_candidates:
            result["projection"] = projection
            result["notice"] = (
                "current_value 为已提交值；candidate_value 为查询时累计候选值，不含未结算事件效果、非角色知情。"
                "candidate_exists=false 表示未设置，definition=null 表示仅运行时属性、不是剧本声明。"
                "续读保留 include_candidates=true。"
            )
        if runtime:
            result["notice"] = (
                "当前分支剧情节点运行态；status 只描述所选状态视图，unlocked 不代表激活，计时和冷却不在查询中推进。"
                "choices.available 仅按当前激活状态和选项条件判断，不结算效果，不预测未来激活或NPC回应。"
                "需要完整规则时改用 definition 视图、query=node_id 从列表重新查询；需要后果时调用 preview_choice_outcome。"
                "choices 字段的长选项保留 summary 身份与可用性，complete=false 不表示已读标签；按成员 key 下钻读取原文。"
                "幕后信息不是角色已知；续读保留 view=runtime。"
                + ("本次为未提交候选状态，仅在接收 end_time 后纳入定时事件及其剧情解锁；仅复用已准备的事件骰子结果，不重新掷骰或执行完整生命周期，"
                   "不提前激活节点；续读保留 include_candidates=true。" if include_candidates else "本次只读已提交状态。")
            )
        result["notice"] += (
            "列表翻页携带顶层 snapshot；指定 rule_index（包括 offset=0）须携带条目 snapshot。"
            "长规则可加 result_path=[] 读取条目目录，再按键/索引下钻；字段 next_offset 只表示本层进度，不等于整条规则已读完。"
            "更新、撤销、切换分支或查询视图可使快照失效；收到 restart_required 时丢弃旧片段，按 restart 重查，不得拼接不同快照。"
        )
        return json.dumps(result, ensure_ascii=False)

    def _handle_peek_upcoming_events(self, args: dict, *, ctx: dict | None = None,
                                     settled_item_ids: set | None = None) -> str:
        """预览即将触发的事件（hint 级别，不剧透）。"""
        from datetime import timedelta
        from engine.event_scheduler import parse_time

        hours = args.get("hours_ahead", 3)
        if type(hours) is not int or not 1 <= hours <= 24:
            return json.dumps({"error": "hours_ahead 必须是 1 到 24 的整数"}, ensure_ascii=False)
        start_after_hours = args.get("start_after_hours", 0)
        if type(start_after_hours) is not int or start_after_hours < 0:
            return json.dumps({"error": "start_after_hours 必须是非负整数"}, ensure_ascii=False)
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0:
            return json.dumps({"error": "offset 必须是非负整数"}, ensure_ascii=False)
        current = parse_time(self.current_state.get("game_time"))
        if current is None:
            return json.dumps({"error": "当前存档时间无效，无法预览事件"}, ensure_ascii=False)
        window_start = current + timedelta(hours=start_after_hours)
        deadline = window_start + timedelta(hours=hours)
        query_start = window_start + timedelta(microseconds=1) if start_after_hours else window_start
        conditioned_ids = {
            event["id"] for section in ("one_time_events", "cyclic_events")
            for event in self.script.get(section, []) if event.get("condition")
        }
        candidate = self._fork_query_session(self.current_state)
        projected_events = []
        staged_item_ids = set(settled_item_ids or ())
        candidate._settle_timed_events(current.isoformat(), deadline.isoformat(), projected_events, on_event=partial(
            candidate._settle_event_linked_dice, ctx=ctx or {},
            settled_item_ids=staged_item_ids, staged_only=True,
        ))
        scheduled = [event for event in projected_events
                     if query_start <= parse_time(event["fire_time"]) <= deadline]
        scheduled_keys = {(event["event_id"], event["fire_time"]) for event in scheduled}
        scheduled_total = len(scheduled)
        scheduled = scheduled[offset:offset + 5]
        conditional_candidates = []
        conditional_total = 0
        if conditioned_ids:
            for page in self.event_scheduler.iter_event_pages(
                self.current_state, query_start.isoformat(), deadline.isoformat(),
            ):
                page_candidates = [
                    event for event in page[2] if event["event_id"] in conditioned_ids
                    and (event["event_id"], event["fire_time"]) not in scheduled_keys
                ]
                conditional_candidates.extend(page_candidates[
                    max(0, offset - conditional_total):max(0, offset + 5 - conditional_total)
                ])
                conditional_total += len(page_candidates)
        definitions = {
            event["id"]: event for section in ("one_time_events", "cyclic_events")
            for event in self.script.get(section, [])
        }
        events = [{
            "id": event["event_id"], "type": event["type"],
            "name": definitions.get(event["event_id"], {}).get("name", event["event_id"]),
            "hint": event.get("description", "")[:100], "time": event["fire_time"],
        } for event in scheduled]
        candidates = [{
            "id": event["event_id"], "type": event["type"],
            "name": definitions.get(event["event_id"], {}).get("name", event["event_id"]),
            "hint": event.get("description", "")[:100], "time": event["fire_time"],
        } for event in conditional_candidates]
        return json.dumps({
            "events": events, "total_matches": scheduled_total, "truncated": scheduled_total > offset + 5,
            "conditional_candidates": candidates,
            "conditional_total_matches": conditional_total,
            "conditional_truncated": conditional_total > offset + 5,
            "offset": offset,
            "window_start": window_start.isoformat(),
            "window_end": deadline.isoformat(),
            "staged_event_dice_ids": sorted(staged_item_ids),
            "excludes_event_dice": not bool(staged_item_ids), "excludes_unstaged_event_dice": True,
            "note": "events 基于当前存档条件按触发时刻和前序事件效果只读推演，事件骰子仅复用本轮已准备的结果、不重新掷骰；conditional_candidates 为投影未触发的条件事件，状态改变后应重新查询；均未发生，也不代表主角已知。不包含后续玩家行动、未准备的骰子或非定时生命周期结算。",
        }, ensure_ascii=False)

    # ================================================================
    # Character Cast: 角色独立推演
    # ================================================================

    async def _handle_invoke_character(self, args: dict, ctx: dict | None = None) -> str:
        """启动一个NPC的独立思考，返回行为/对话建议。"""
        npc_id = args.get("npc_id", "")
        situation = args.get("situation", "")
        include_candidates = args.get("include_candidates", False)
        if not isinstance(npc_id, str) or not npc_id.strip() or not isinstance(situation, str):
            return json.dumps({"error": "npc_id 必须是非空字符串，situation 必须是字符串"}, ensure_ascii=False)
        if type(include_candidates) is not bool:
            return json.dumps({"error": "include_candidates 必须是布尔值"}, ensure_ascii=False)
        if include_candidates and not (ctx or {}).get("old_time"):
            return json.dumps({"error": "候选角色推演仅支持进行中的回合"}, ensure_ascii=False)
        events = []
        if include_candidates:
            events, state = self._project_interaction_state(ctx)
        else:
            state = copy.deepcopy(self.current_state)
        self._ensure_rooms_initialized(state)
        npc_script = next((npc for npc in self.script.get("npcs", []) if npc.get("id") == npc_id), None)
        npc_state = state.get("npcs", {}).get(npc_id)
        if npc_script is None and not isinstance(npc_state, dict):
            return json.dumps({"error": f"NPC {npc_id} 不存在"}, ensure_ascii=False)
        actor = {**(npc_script or {}), **(npc_state if isinstance(npc_state, dict) else {})}
        if actor.get("alive") is False:
            return json.dumps({"error": "该角色已故，无法推演当前反应"}, ensure_ascii=False)
        npc_name = actor.get("name", npc_id)
        completed_goals = {goal.get("id") if isinstance(goal, dict) else goal
                           for goal in state.get("completed_npc_goals", [])}
        actor_goals = state.get("dynamic_story_content", {}).get("goal_overrides", {}).get(
            "npcs", {}).get(npc_id, actor.get("goals", []))
        character_context = {
            "in_contact": self._npc_contact_is_current(npc_id, state),
            "location": self._get_npc_location(npc_id, state),
            "current_room": actor.get("current_room", ""),
            "current_mood": actor.get("current_mood", ""),
            "attitude_toward_player": actor.get("attitude_toward_player", 50),
            "relationship": state.get("player", {}).get("relationships", {}).get(npc_id),
            "active_goals": [
                {"id": goal.get("id", ""), "description": goal.get("description", "")}
                for goal in actor_goals
                if f"{npc_id}:{goal.get('id', '')}" not in completed_goals
            ],
        }
        result = {
            "npc_id": npc_id, "npc": npc_name, "mode": "advisory", "game_time": state.get("game_time"),
            "context": character_context, "knowledge_source": "committed_state",
            "note": "仅供GM参考的角色建议，不是已发生事件、玩家知情或互动许可，不建立接触、不保存状态。"
                    "更新、撤销或切换分支后须重新调用。",
        }
        if include_candidates:
            result["projection"] = {
                "through": state.get("game_time"), "provisional": True,
                "scope": "accepted_updates_and_scheduled_events",
                "event_ids": list(dict.fromkeys(event["event_id"] for event in events)),
                "note": "候选未提交，只含已接收更新及候选结束时间内的定时事件，"
                        "随后刷新日程、房间并重算接触；不含完整生命周期或候选信息传播，候选事件不自动成为角色知识。",
            }
        knowledge = self._build_npc_knowledge_context(
            {"present_npc_ids": [npc_id], "action_text": situation}, self.current_state,
        )
        system = (
            f"你是{npc_name}。{actor.get('bio', '')[:200]}\n"
            f"性格：{actor.get('personality', '')[:100]}\n"
            "为GM推演你的反应（对话+行为意图），用第一人称简短回答（3-5句话）。\n"
            "建议尚未发生，不能代替行动结算或证明玩家已接触到你。"
            "情境只可作为可感知刺激或假设，不可据此认定幕后秘密已知；只使用属于你的已知信息。\n"
            + json.dumps(result, ensure_ascii=False)
            + "\n## 已提交状态中的角色知识\n" + (knowledge or "没有可用的角色专属知识记录，不要编造内幕。")
        )

        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": f"情境：{situation[:500]}"}],
                system=system, max_tokens=300,
                **self._stage_kwargs("knowledge_graph"),
            )
            return json.dumps({**result, "response": raw.strip()}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ================================================================
    # Intent shortcuts (rewrite / query) — no turn advancement
    # ================================================================

    async def _agent_rewrite(self, feedback: str) -> dict:
        """Rewrite last turn narrative without advancing the turn."""
        if not self.world_tree:
            return {"error": "无可重写的回合"}
        last_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not last_node:
            return {"error": "无可重写的回合"}

        original = last_node.get("ai_response", "")
        action_raw = last_node.get("player_action", {})
        action_text = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")

        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system(
            "agentic_unified",
            world_background=(self.script.get("world_background", "") or "")[:500],
            player_name=self.current_state.get("player", {}).get("name", ""),
        )
        system += (
            f"\n\n## 重写模式\n"
            f"用户对上一版叙事不满意。反馈：{feedback}\n"
            "保持相同事件走向，改善用户指出的问题。只输出新版叙事文本。"
        )

        user = f"原版叙事（需修改）：\n{original[:2000]}\n\n玩家行动：{action_text}"

        # Rewrite uses info tools only
        all_tools = _unified_tools()
        info_tools = [t for t in all_tools if t["function"]["name"] in
                      ("recall_history", "query_lorebook", "query_npc_history", "check_inventory")]
        allowed_tools = {tool["function"]["name"] for tool in info_tools}

        async def rewrite_dispatch(name, args):
            if name not in allowed_tools:
                return json.dumps({"error": f"未开放工具: {name}"}, ensure_ascii=False)
            return await self._run_tool_native_async(name, args)

        holder = _AgentResult()
        async for _ in self._agent_loop(
            [{"role": "user", "content": user}], system, info_tools,
            rewrite_dispatch,
            max_rounds=5, label="重写Agent", result_holder=holder,
        ):
            pass

        new_narrative = holder.text
        if not new_narrative:
            return {"error": "重写失败"}

        # 维护 swipe 历史（保留原版可回看）
        swipes = last_node.setdefault("swipes", [])
        if not swipes:
            swipes.append({
                "narrative": original,
                "choices": last_node.get("choices_presented", []),
                "state_changes": last_node.get("state_changes", []),
                "state_snapshot": last_node.get("state_snapshot"),
                "agent_trace": copy.deepcopy(last_node.get("agent_trace")),
            })
        else:
            active_index = last_node.get("active_swipe_index", 0)
            if 0 <= active_index < len(swipes):
                swipes[active_index]["agent_trace"] = copy.deepcopy(last_node.get("agent_trace"))
        swipes.append({
            "narrative": new_narrative,
            "choices": last_node.get("choices_presented", []),
            "state_changes": last_node.get("state_changes", []),
            "state_snapshot": last_node.get("state_snapshot"),
        })
        last_node["active_swipe_index"] = len(swipes) - 1
        last_node["ai_response"] = new_narrative
        last_node.pop("agent_trace", None)

        state = self.current_state
        return {
            "ok": True,
            "narrative": new_narrative,
            "rewrite": True,
            "choices": last_node.get("choices_presented", []),
            "state": state,
            "node_id": self.world_tree.active_node_id if self.world_tree else "",
            "game_time": state.get("game_time", ""),
            "dice_rolls": [],
            "warnings": [],
        }

    async def _agent_query(self, query: str, *, on_event=None) -> dict:
        """Answer a player query without advancing the turn."""
        if on_event is not None:
            on_event({"type": "preflight", "stage": "query"})
        await self._drain_background_tasks()
        state = self.current_state or {}
        player = state.get("player", {})
        loc_id = player.get("location", "")
        loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id) if hasattr(self, '_location_by_id') else loc_id
        presence_state = copy.deepcopy(state)
        present_ids, _ = self._compute_present_npcs(presence_state)
        present_npcs = []
        npcs_state = state.get("npcs", {})
        for nid in present_ids:
            npc = npcs_state.get(nid) or self._npc_by_id.get(nid, {})
            if isinstance(npc, dict) and npc.get("known") is not False:
                present_npcs.append(f"{npc.get('name', nid)}(ID:{nid})")

        system = (
            "你是游戏助手。回答玩家的查询，简洁明了。可以使用工具获取更多信息。\n"
            "仅依据玩家已知的经历和当前可用资料回答；世界书仅供参考，不自动代表玩家知情。\n"
            f"当前时间: {state.get('game_time', '未知')}\n"
            f"玩家: {player.get('name', '未知')}，位于 {loc_name}\n"
            f"在场NPC: {', '.join(present_npcs) if present_npcs else '无'}\n"
            f"背包: {', '.join(i.get('name', i.get('id', '?')) for i in state.get('inventory', [])[:10]) or '空'}"
        )
        context = []
        interactables = self._visible_interactables_context()
        if interactables:
            system += "\n现场交互项仅提供可见描述与行动提示；未执行的检查不代表已查明具体内容。"
            context.append(interactables)
        pc_clues = self.prompt_builder.format_pc_lore_clues(state)
        if pc_clues:
            context.append(pc_clues)
        history_summary = state.get("history_summary", "")
        event_reports = self._pc_event_reports_context(state)
        if event_reports:
            context.append(event_reports)
        if history_summary:
            if len(history_summary) > 2000:
                history_summary = history_summary[:1000] + "\n…\n" + history_summary[-997:]
            context.append(f"<history_summary>\n{history_summary}\n</history_summary>")
        recent_queries = self._recent_query_context()
        if recent_queries:
            context.append(recent_queries)
        recent = self.world_tree.get_recent_history(1) if self.world_tree else []
        if recent:
            last_node = recent[-1]
            previous_action = last_node.get("player_action")
            action_text = (previous_action.get("text", "") if isinstance(previous_action, dict)
                           else str(previous_action or ""))
            narrative = last_node.get("ai_response") or last_node.get("narrative") or ""
            if len(narrative) > 1300:
                narrative = narrative[:650] + "\n…\n" + narrative[-650:]
            previous_round = (f"[玩家行动] {action_text[:120]}\n\n" if action_text else "") + narrative
            if previous_round:
                context.append(f"<previous_round>\n{previous_round}\n</previous_round>")
        user = "\n\n".join([*context, f"查询：{query}"])

        all_tools = _unified_tools()
        info_tools = [t for t in all_tools if t["function"]["name"] in
                      ("recall_history", "query_lorebook", "check_inventory", "get_npc_attitude")]
        allowed_tools = {tool["function"]["name"] for tool in info_tools}
        known_relation_ids = set()
        for relationship in (state.get("npc_relationships_known") or {}).values():
            first = relationship.get("from", relationship.get("a"))
            second = relationship.get("to", relationship.get("b"))
            if first and second:
                known_relation_ids.add(f"_kg_rel_{first}_{second}")
        public_relations = {}
        for relation in self.script.get("npc_relationships", []):
            if not relation.get("initially_known", True) or not relation.get("public_description"):
                continue
            first, second = relation.get("from"), relation.get("to")
            key = f"{first}_{second}"
            known = (state.get("npc_relationships_known") or {}).get(key, {})
            if (not known or known.get("discovery_reason")
                    or known.get("description") not in (relation.get("description"), relation["public_description"])):
                continue
            first_name = self._npc_by_id.get(first, {}).get("name", first)
            second_name = self._npc_by_id.get(second, {}).get("name", second)
            summary = f"{first_name} → {second_name}: {relation.get('public_type', '已知关系')}\n{relation['public_description']}"
            public_relations[f"_kg_rel_{key}"] = summary
            public_relations[f"_npc_rel_{key}"] = summary

        async def query_dispatch(name, args):
            if name not in allowed_tools:
                return json.dumps({"error": f"未开放工具: {name}"}, ensure_ascii=False)
            if name == "query_lorebook" and self.prompt_builder.lorebook is not None:
                lorebook = self.prompt_builder.lorebook
                excluded = self.prompt_builder._excluded_lore_ids(state)
                discovered = set(state.get("pc_discovered_lore") or [])
                clues = state.get("pc_lore_clues") or {}
                excluded.update(entry.id for entry in lorebook.entries
                                if entry.visibility != "public"
                                and not (entry.visibility == "hidden"
                                         and (entry.id in discovered or clues.get(entry.id))))
                excluded.update(entry.id for entry in lorebook.entries
                                if entry.id.startswith("_kg_rel_") and entry.id not in known_relation_ids)
                excluded.update(
                    f"{prefix}{npc_id}"
                    for npc_id, npc in npcs_state.items()
                    if isinstance(npc, dict) and npc.get("known") is False
                    for prefix in ("_npc_profile_", "_kg_npc_")
                )
                safe_lorebook = copy.copy(lorebook)
                safe_lorebook.entries = []
                safe_lorebook._precomputed = lorebook._precomputed.copy()
                for entry in lorebook.entries:
                    if entry.id in public_relations:
                        entry = replace(entry, content=public_relations[entry.id])
                    if entry.visibility == "hidden" and entry.id not in discovered and clues.get(entry.id):
                        entry = replace(entry, content="\n".join(
                                            self.prompt_builder.pc_lore_clue_lines(state, entry.id)),
                                        comment="已获知的线索", keys=[], secondary_keys=[], related_entries=[])
                        safe_lorebook._precomputed[entry.id] = ([], [])
                    relation_count = sum(related.startswith("_kg_rel_") for related in entry.related_entries)
                    lines = entry.content.splitlines()
                    if entry.id.startswith("_kg_npc_") and relation_count:
                        if not all(line.startswith("与") for line in lines[-relation_count:]):
                            excluded.add(entry.id)
                        else:
                            entry = replace(entry, content="\n".join(lines[:-relation_count]))
                    safe_lorebook.entries.append(entry)
                return json.dumps(safe_lorebook.query(
                    keyword=args.get("keyword", ""), entry_id=args.get("entry_id", ""),
                    page=args.get("page", 0), offset=args.get("offset", 0),
                    excluded_ids=excluded, entry_ids=args.get("entry_ids"),
                    snapshot=args.get("snapshot"),
                    snapshot_scope=(self.world_tree.tree_id, self.world_tree.active_node_id,
                                    state.get("game_time"), "player"),
                ), ensure_ascii=False)
            if name == "get_npc_attitude":
                npc = npcs_state.get(args.get("npc_id", ""), {})
                if isinstance(npc, dict) and (npc.get("known") is False or npc.get("met") is False):
                    return f"{args.get('npc_id', '')}的态度: 未知"
            return await self._run_tool_native_async(name, args)

        holder = _AgentResult()
        completed = False
        async for event in self._agent_loop(
            [{"role": "user", "content": user}], system, info_tools,
            query_dispatch,
            max_rounds=3, label="查询Agent", result_holder=holder,
            final_reply_after_tools=True,
        ):
            if event.get("type") == "agent_done":
                completed = True
            if on_event is not None and event.get("type") in (
                "model_waiting", "heartbeat", "tool_call", "agent_done", "agent_max_rounds",
            ):
                visible = ("type", "label", "round", "tool_name", "max_rounds")
                on_event({key: event[key] for key in visible if key in event})

        if not completed or not holder.text.strip():
            return {"ok": False, "error": "查询未完成，请重试。", "reply": "", "query": True}
        return {"ok": True, "reply": holder.text, "query": True}
