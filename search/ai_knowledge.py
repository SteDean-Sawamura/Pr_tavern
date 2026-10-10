"""AI knowledge generation: use AI models to generate background knowledge."""


class AIKnowledge:
    def __init__(self, ai_provider):
        self.ai_provider = ai_provider

    async def generate(self, query: str) -> dict:
        """Use AI to generate encyclopedia-style knowledge for a topic."""
        system = """你是一个百科知识助手。用户会给你一个关键词或主题，你需要提供详细的百科式背景知识。
内容应当包括：
- 基本概念和定义
- 历史背景或起源
- 重要特征和细节
- 相关的文化或社会影响
- 可以用于故事创作的有趣细节

请用中文回答，内容要详实但精炼（300-800字）。"""

        messages = [{"role": "user", "content": f"请为以下主题提供详细的百科知识：{query}"}]

        try:
            response = await self.ai_provider.generate(messages, system=system)
            return {
                "title": f"AI知识: {query}",
                "content": response,
                "url": None,
                "source_type": "ai_generated",
                "success": True,
            }
        except Exception as e:
            return {
                "title": f"AI知识: {query}",
                "content": "",
                "source_type": "ai_generated",
                "success": False,
                "error": str(e),
            }

    async def summarize(self, content: str) -> str:
        """Summarize content into a concise form."""
        system = "你是一个精炼摘要助手。请将给定内容提炼为100-200字的简洁摘要，保留关键信息。用中文回答。"
        messages = [{"role": "user", "content": f"请摘要以下内容：\n\n{content}"}]
        try:
            return await self.ai_provider.generate(messages, system=system)
        except Exception:
            return content[:200] + "..."

    async def suggest_tags(self, content: str) -> list[str]:
        """Suggest classification tags for content."""
        system = """你是一个内容分类助手。分析给定内容，返回2-5个分类标签。
只返回标签，用逗号分隔，不要其他内容。
可选标签包括但不限于：历史、地理、人物、文化、科技、自然、军事、经济、社会、神话、建筑、美食、艺术、宗教、教育"""
        messages = [{"role": "user", "content": f"请为以下内容推荐分类标签：\n\n{content[:500]}"}]
        try:
            response = await self.ai_provider.generate(messages, system=system)
            tags = [t.strip() for t in response.split(",") if t.strip()]
            return tags[:5]
        except Exception:
            return ["未分类"]

    async def transform_to_script_element(
        self, content: str, target_type: str, script_summary: str = "",
    ) -> list[dict]:
        """Transform material content into script elements. Returns a list.

        script_summary: 由 _build_full_script_summary 生成的完整剧本上下文，
                        用于保持风格一致并避免重复生成已有条目。
        """
        prompts = {
            "background": "请将以下素材内容转化为游戏世界背景描述（2-3段）：",
            "location": '请将以下素材转化为游戏地点，根据素材内容生成尽可能多的地点。返回JSON数组，每个元素格式：{{"id": "地点ID", "name": "地点名称", "description": "详细描述", "initially_visible": true}}',
            "npc": '请将以下素材转化为游戏NPC，根据素材中涉及的人物生成尽可能多的NPC。返回JSON数组，每个元素格式：{{"id": "npc_id", "name": "名字", "bio": "简介", "personality": "性格", "capabilities": "能力", "title": "头衔", "organizations": [{{"org_id": "组织ID", "rank": 1, "role": "可选角色"}}], "attitude_toward_player": 50, "goals": [{{"id": "目标ID", "description": "NPC目标描述", "type": "short_term/long_term", "priority": "high/medium/low", "conflict_with_player": "与玩家冲突点(可选)"}}]}}',
            "event": '请将以下素材转化为游戏事件，根据素材内容生成尽可能多的事件。返回JSON数组，每个元素格式：{{"id": "event_id", "name": "事件名称", "description": "事件描述", "trigger_time": "ISO时间或null", "condition": "触发条件表达式(可选，如 player.health>30)", "fire_events": ["触发的游戏事件名(可选)"], "activate_events": ["被哪些游戏事件触发解锁(可选)"]}}',
            "lorebook": '请将以下素材转化为知识库词条，根据素材内容生成尽可能多的词条。返回JSON数组，每个元素格式：{{"keys": ["关键词1", "关键词2"], "content": "当这些关键词出现时AI应知道的背景知识", "comment": "词条简述"}}',
            "organization": '请将以下素材转化为游戏组织，根据素材内容生成尽可能多的组织。返回JSON数组，每个元素格式：{{"id": "org_id", "name": "组织名称", "type": "类型", "parent_org": "父组织ID(可选)", "leader": "领导者NPC的ID", "stance": "对主角立场", "description": "组织描述", "aliases": ["别名"], "hierarchy": [{{"rank": 1, "title": "最高职位"}}, {{"rank": 2, "title": "次级职位"}}, {{"rank": 3, "title": "基层职位"}}], "goals": [{{"id": "目标ID", "description": "组织目标描述", "priority": "high/medium/low", "condition_met": "完成条件(可选)", "conflict_with_player": "与玩家冲突点(可选)"}}]}}',
            "faction": '请将以下素材转化为游戏势力，根据素材内容生成尽可能多的势力。返回JSON数组，每个元素格式：{{"id": "fac_id", "name": "势力名称", "stance": "立场", "description": "势力描述"}}',
            "random_item": '请将以下素材转化为游戏随机项(骰子驱动的随机系统)。返回JSON数组，每个元素格式：{{"id": "英文ID", "description": "随机项描述", "trigger": "触发时机", "trigger_type": "always", "dice": {{"count": 1, "faces": 100, "modifier": 0}}, "ranges": [{{"min": 1, "max": 30, "label": "结果1简称", "description": "结果1描述"}}, {{"min": 31, "max": 70, "label": "结果2简称", "description": "结果2描述"}}, {{"min": 71, "max": 100, "label": "结果3简称", "description": "结果3描述"}}]}}',
        }

        prompt = prompts.get(target_type, prompts["background"])

        context_hint = ""
        if script_summary:
            context_hint = f"\n\n【当前剧本已有内容（不要重复生成已有的条目，保持风格一致）】\n{script_summary}"

        system = (
            "你是游戏剧本设计助手。将素材转化为指定格式的游戏元素。"
            "【重要】必须保留素材中所有真实的人名、地名、国家名、朝代名、历史事件名称和日期。"
            "不要用虚构名称替代真实存在的人物或地点。如果素材涉及历史人物（如孙中山、拿破仑）或真实地点（如上海、巴黎），"
            "必须原样保留，不得改写为笼统的描述。如果需要返回JSON，只返回JSON数组，不要其他内容。"
        )
        messages = [{"role": "user", "content": f"{prompt}{context_hint}\n\n素材内容：\n{content}"}]

        try:
            response = await self.ai_provider.generate(messages, system=system)
            if target_type in ("location", "npc", "event", "lorebook", "organization", "faction", "random_item"):
                import json
                import re
                # Try JSON array first, then single object
                arr_match = re.search(r'\[.*\]', response, re.DOTALL)
                if arr_match:
                    parsed = json.loads(arr_match.group())
                    if isinstance(parsed, list):
                        return parsed
                obj_match = re.search(r'\{.*\}', response, re.DOTALL)
                if obj_match:
                    return [json.loads(obj_match.group())]
            return [{"content": response}]
        except Exception:
            return [{"content": content}]
