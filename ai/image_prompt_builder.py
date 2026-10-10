"""Build image generation prompts from narrative text + scene context."""

import logging
import re

logger = logging.getLogger("tavern.image")

_CJK_RE = re.compile(r'[一-鿿㐀-䶿]')

_SYSTEM = """You are an expert visual scene describer for AI image generation.
Given a narrative text and rich scene context, craft a detailed image generation prompt.

Guidelines:
- Describe the visual scene vividly: setting, architecture, landscape, key objects
- Describe characters by appearance (clothing, hair, build, posture, expression, action) — NEVER use character names
- Include lighting direction, quality, color temperature (e.g. "warm golden hour sidelighting", "cold blue moonlight from above")
- Specify atmosphere and mood through visual cues (fog, dust particles, lens flare, rain streaks)
- Include composition hints (wide establishing shot, close-up, over-the-shoulder, bird's eye view)
- Include color palette and tonal range
- NO text, NO dialogue, NO speech bubbles, NO abstract concepts, NO character names
- If a style directive is provided, integrate it as the dominant visual style
- Output ONLY the prompt, nothing else"""

_SYSTEM_ZH = """你是一个专业的AI图像生成场景描述师。
根据给定的叙事文本和场景上下文，生成详细的图像生成提示词。

要求：
- 详细描述视觉场景：环境、建筑、地形、关键物品
- 用外貌描述角色（服饰、发型、体态、姿势、表情、动作）——绝对不要使用角色名字
- 包含光照方向、质感、色温（如"温暖的金色侧光"、"冰冷的蓝色月光从头顶洒下"）
- 通过视觉元素传达氛围（雾气、尘埃粒子、光晕、雨丝）
- 包含构图提示（全景远景、特写、过肩镜头、俯瞰视角）
- 描述色彩基调和色调范围
- 如果提供了风格指令，将其作为主导视觉风格融入
- 不要包含文字、对话、对话气泡、抽象概念、角色名字
- 只输出提示词本身，不要任何解释"""


async def build_image_prompt(
    narrative: str,
    location_name: str,
    mood: str,
    time_of_day: str,
    ai_provider,
    *,
    weather: str = "",
    characters: list[str] | None = None,
    location_desc: str = "",
    image_style: str = "",
) -> str:
    """Use existing LLM to distill narrative into a rich image prompt."""
    use_zh = bool(image_style and _CJK_RE.search(image_style))
    system = _SYSTEM_ZH if use_zh else _SYSTEM

    parts = []
    parts.append(f"Location: {location_name}")
    if location_desc:
        parts.append(f"Environment: {location_desc}")
    parts.append(f"Time: {time_of_day}")
    if weather:
        parts.append(f"Weather: {weather}")
    if characters:
        parts.append("Characters present:\n" + "\n".join(f"- {c}" for c in characters))
    parts.append(f"Mood: {mood}")
    if image_style:
        parts.append(f"Art style directive (HIGHEST PRIORITY): {image_style}")

    user_msg = "Scene context:\n" + "\n".join(parts)
    user_msg += f"\n\nNarrative (distill into visual prompt):\n{narrative}"

    try:
        result = await ai_provider.generate(
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            max_tokens=8192,
        )
        prompt = result.strip().strip('"').strip("'")
        if len(prompt) < 10:
            prompt = _fallback_prompt(location_name, mood, time_of_day, weather, image_style)
        logger.info("Image prompt (%d chars): %s", len(prompt), prompt[:200])
        return prompt
    except Exception as e:
        logger.warning("Failed to build image prompt: %s", e)
        return _fallback_prompt(location_name, mood, time_of_day, weather, image_style)


def _fallback_prompt(location: str, mood: str, tod: str, weather: str, style: str) -> str:
    parts = [f"cinematic scene, {location}, {mood} atmosphere"]
    if weather:
        parts.append(weather)
    parts.append(f"{tod} lighting")
    if style:
        parts.append(style)
    else:
        parts.append("dramatic lighting, painterly style")
    return ", ".join(parts)
