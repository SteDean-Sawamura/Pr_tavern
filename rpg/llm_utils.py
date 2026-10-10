"""LLM 调用工具函数和 AI provider 管理"""

import json
import asyncio

from ai.openai_provider import OpenAIProvider
from ai.response_parser import ResponseParser
from .runtime import current_client, request_llm_state

_ai_provider: OpenAIProvider | None = None
_llm_state: dict = {"client": None, "model": "", "profile": None}


def _semantic_attr_value(name: str, value, attr_def: dict = None) -> str:
    if isinstance(value, (int, float)):
        v = int(value)
        if v <= 20: level = "极低"
        elif v <= 40: level = "偏低"
        elif v <= 60: level = "中等"
        elif v <= 80: level = "较高"
        else: level = "极高"
        desc = f"{name}：{level}（{v}）"
        if attr_def and attr_def.get("rule"):
            desc += f" — {attr_def['rule'][:40]}"
        return desc
    return f"{name}：{value}"


def _semantic_relationship(npc_name: str, value: int) -> str:
    if value <= 20: return f"{npc_name}（敌对）"
    elif value <= 40: return f"{npc_name}（冷淡）"
    elif value <= 60: return f"{npc_name}（中立）"
    elif value <= 80: return f"{npc_name}（友好）"
    else: return f"{npc_name}（亲密）"


def get_llm_state() -> dict:
    state = request_llm_state.get()
    return state if state is not None else _llm_state


def set_ai_profile(profile: dict | None):
    global _ai_provider
    provider = OpenAIProvider(profile) if profile is not None else None
    state = {"client": provider, "model": profile.get("model", "") if profile is not None else "", "profile": profile}
    client = current_client.get()
    if client is not None:
        client.llm_state = state
        request_llm_state.set(state.copy())
    else:
        _ai_provider = provider
        _llm_state.update(state)


async def llm_call(system_prompt: str, user_prompt: str, max_tokens: int = 2048, raise_on_error: bool = False) -> str:
    provider = get_llm_state().get("client")
    if not provider:
        if raise_on_error:
            raise RuntimeError("LLM 客户端未配置")
        return ""
    try:
        return await provider.generate(
            [{"role": "user", "content": user_prompt}],
            system=system_prompt, max_tokens=max_tokens,
        )
    except Exception as e:
        print(f"[LLM ERROR] {e}")
        if raise_on_error:
            raise RuntimeError(f"LLM 调用失败: {e}")
        return ""


async def llm_call_with_tools(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    tool_executor: dict,
    max_tokens: int = 2048,
    max_rounds: int = 7,
) -> tuple[str, list[dict]]:
    provider = get_llm_state().get("client")
    if not provider:
        return "", []

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_calls_log = []
    content = ""

    for _ in range(max_rounds):
        try:
            result = await provider.generate_with_tools(
                messages, tools=tools, max_tokens=max_tokens,
            )
            content = result.get("content", "") or ""
            raw_tool_calls = result.get("tool_calls") or []

            if not raw_tool_calls:
                return content, tool_calls_log

            assistant_msg = {"role": "assistant", "tool_calls": []}
            if content:
                assistant_msg["content"] = content

            tool_result_msgs = []
            for tc in raw_tool_calls:
                tool_name = tc["name"]
                tool_args = tc["arguments"]

                try:
                    if tool_name in tool_executor:
                        executor = tool_executor[tool_name]
                        if asyncio.iscoroutinefunction(executor):
                            exec_result = await executor(**tool_args)
                        else:
                            exec_result = executor(**tool_args)
                    else:
                        exec_result = {"error": f"工具 {tool_name} 未注册"}
                except Exception as e:
                    exec_result = {"error": str(e)}

                tool_calls_log.append({"name": tool_name, "args": tool_args, "result": exec_result})

                assistant_msg["tool_calls"].append({
                    "id": tc["id"], "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
                })
                tool_result_msgs.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(exec_result, ensure_ascii=False)})

            messages.append(assistant_msg)
            messages.extend(tool_result_msgs)

        except Exception as e:
            print(f"[LLM ERROR] {e}")
            break

    return content, tool_calls_log


def _parse_json_from_llm(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        end = next((i for i, l in enumerate(lines) if l.strip() == "```"), len(lines))
        text = "\n".join(lines[:end])
    return ResponseParser._try_parse_json(text) or {}
