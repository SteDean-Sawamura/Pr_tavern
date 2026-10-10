"""GameSession Mixin: 技能检定"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # 避免循环导入

_ATTR_SYNONYMS = {
    "力量": ("武力", "力", "武", "攻击", "战斗", "体力", "strength", "str"),
    "敏捷": ("身法", "速度", "灵巧", "反应", "闪避", "dexterity", "dex", "agility"),
    "智力": ("智谋", "智慧", "才学", "学识", "知识", "情报", "intelligence", "int", "wisdom", "wis"),
    "魅力": ("口才", "话术", "社交", "外交", "领导", "统率", "政治", "charisma", "cha"),
    "体力": ("耐力", "体质", "生命", "constitution", "con", "stamina"),
}


class SkillCheckMixin:
    """技能检定相关方法"""

    def _opening_choice_source(self, variant: dict | None = None) -> list[dict]:
        if self.script.get("_preset_opening_text") and self.script.get("_preset_opening_choices"):
            return self.script["_preset_opening_choices"]
        if variant and variant.get("choices"):
            return variant["choices"]
        return self.script.get("opening", {}).get("choices", [])

    def _get_skill_bonus(self, skill: str) -> int:
        player = self.current_state.get("player", {})
        # 优先从职业技能系统查找
        skills = player.get("skills", {})
        for sid, sdata in skills.items():
            sname = sdata.get("name", "") if isinstance(sdata, dict) else ""
            if skill in (sid, sname):
                prof = sdata.get("bonus", 0) if isinstance(sdata, dict) else 0
                parent_attr = sdata.get("parent_attribute", "") if isinstance(sdata, dict) else ""
                attrs = player.get("attributes", {})
                attr_val = 50
                if parent_attr:
                    av = attrs.get(parent_attr)
                    if isinstance(av, dict):
                        attr_val = av.get("value", 50)
                    elif isinstance(av, (int, float)):
                        attr_val = av
                return (attr_val - 10) // 2 + prof if isinstance(attr_val, (int, float)) else prof
        # 回退到原始属性查找
        attrs = player.get("attributes", {})
        for k, v in attrs.items():
            if isinstance(v, dict):
                if skill in (k, v.get("display_name", ""), v.get("name", "")):
                    val = v.get("value", 10)
                    return (val - 10) // 2 if isinstance(val, (int, float)) else 0
        return 0

    def _check_opening_choice(self, player_action: dict) -> dict | None:
        """Check if this is an opening choice and return its result.

        For conditional results, roll dice to determine the actual outcome.
        """
        if player_action.get("type") != "choice":
            return None
        choice_id = player_action.get("choice_id", "")
        if not choice_id.startswith("open_"):
            return None
        root_id = self.world_tree.root_node_id
        if not root_id:
            return None
        if self.world_tree.active_node_id != root_id:
            active_node = self.world_tree.get_node(self.world_tree.active_node_id)
            opening_action = (active_node or {}).get("player_action") or {}
            if (not active_node or active_node.get("parent_id") != root_id
                    or opening_action.get("type") != "system"
                    or opening_action.get("text") != "游戏开始"):
                return None

        variant_id = self.current_state.get("_opening_variant_id", "")
        variant = next((item for item in self.script.get("opening_variants", [])
                        if item.get("id") == variant_id), None) if variant_id else None
        choices = self._opening_choice_source(variant)

        try:
            idx = int(choice_id.split("_")[1])
            if 0 <= idx < len(choices):
                result = choices[idx].get("result", {})
                if result.get("type") == "conditional":
                    return self._evaluate_conditional_result(result)
                return result
        except (IndexError, ValueError):
            pass
        return None

    def _resolve_skill_check_from_route(self, route: dict, action_text: str) -> dict | None:
        if not self.current_state.get("dice_check_enabled", True):
            return None
        check_result = None
        sc = route.get("skill_check", {})
        if isinstance(sc, dict) and sc.get("needed"):
            attr = sc.get("attr", "")
            difficulty = sc.get("difficulty", "medium")
            if difficulty not in ("easy", "medium", "hard", "extreme"):
                difficulty = "medium"
            attrs = self.current_state.get("player", {}).get("attributes", {})
            attr_val = None
            matched_attr_name = attr
            if attr:
                # Phase 1: exact match
                for attr_name, val in attrs.items():
                    if attr == attr_name:
                        attr_val = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                        matched_attr_name = attr_name
                        break
                # Phase 2: containment match (only if attr is >= 2 chars to avoid single-char ambiguity)
                if attr_val is None and len(attr) >= 2:
                    for attr_name, val in attrs.items():
                        if attr in attr_name or attr_name in attr:
                            attr_val = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                            matched_attr_name = attr_name
                            break
                # Phase 3: synonym mapping for AI-generated attr names
                if attr_val is None:
                    attr_lower = attr.lower()
                    for canonical, synonyms in _ATTR_SYNONYMS.items():
                        if attr_lower == canonical or any(attr_lower == s or attr_lower in s or s in attr_lower for s in synonyms):
                            for attr_name, val in attrs.items():
                                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                                name_lower = attr_name.lower()
                                if canonical in name_lower or name_lower in canonical or any(s in name_lower or name_lower in s for s in synonyms):
                                    attr_val = v
                                    matched_attr_name = attr_name
                                    break
                            break
            check_result = self._resolve_check(difficulty, matched_attr_name, attr_val)
            logging.getLogger(__name__).debug("Route skill check: attr=%s diff=%s outcome=%s", attr, difficulty, check_result.get("outcome"))
        if not check_result and action_text:
            check_result = self._maybe_skill_check(action_text)
        if check_result:
            self._apply_skill_growth(check_result)
            self._record_check_outcome(check_result.get("outcome", "failure"))
        return check_result

    def _maybe_skill_check(self, action_text: str) -> dict | None:
        """Determine if a freeform action warrants a skill check.

        Uses keyword heuristics and adjusts difficulty by relevant attributes.
        Flow#1: 从剧本 settings.skill_check_map 读取自定义技能映射，否则使用默认值。
        """
        if not self.current_state.get("dice_check_enabled", True):
            return None

        # 优先从剧本设置读取技能映射
        script_skill_map = self.script.get("settings", {}).get("skill_check_map")
        if script_skill_map:
            skill_map = script_skill_map
        elif self.class_registry:
            # 从 ClassRegistry 生成技能映射
            class_system = self.script.get("settings", {}).get("class_system", "dnd5e")
            player_skills = list(self.current_state.get("player", {}).get("skills", {}).keys())
            skill_map = self.class_registry.build_skill_check_map(class_system, player_skills)
        else:
            # 默认 skill_map — 属性名动态匹配玩家实际属性
            attrs = self.current_state.get("player", {}).get("attributes", {})
            attr_names = list(attrs.keys())
            # 将关键词映射到语义类别，再从玩家属性中找最佳匹配
            _CATEGORY_KEYWORDS = {
                "physical_power": ("力量", "武力", "力", "武", "攻击", "战斗", "strength", "str"),
                "agility": ("敏捷", "身法", "速度", "灵巧", "反应", "dexterity", "dex", "agility"),
                "intelligence": ("智力", "智谋", "智慧", "才学", "学识", "知识", "情报", "intelligence", "int", "wisdom", "wis"),
                "charisma": ("魅力", "口才", "话术", "社交", "外交", "领导", "统率", "政治", "charisma", "cha"),
                "stamina": ("体力", "耐力", "体质", "constitution", "con", "stamina"),
            }
            # 从玩家属性中为每个类别找最佳匹配
            _resolved = {}
            for cat, synonyms in _CATEGORY_KEYWORDS.items():
                for a in attr_names:
                    al = a.lower()
                    if any(s in al or al in s for s in synonyms):
                        _resolved[cat] = a
                        break
            # 如果找不到匹配，回退到第一个属性
            fallback_attr = attr_names[0] if attr_names else "未知"
            p = _resolved.get("physical_power", fallback_attr)
            a = _resolved.get("agility", fallback_attr)
            i = _resolved.get("intelligence", fallback_attr)
            c = _resolved.get("charisma", fallback_attr)
            s = _resolved.get("stamina", p)
            skill_map = {
                "extreme": {
                    "暗杀": a, "刺杀": a,
                    "召唤": i, "复活": i,
                },
                "hard": {
                    "偷窃": a, "偷东西": a, "扒窃": a, "行窃": a, "撬锁": a, "撬开": a,
                    "翻墙": p, "攀爬": p, "跳跃": p, "跳下": p, "跳上": p, "跳过去": p,
                    "潜入": a, "隐藏": a, "偷袭": a,
                    "逃跑": a, "躲避": a,
                    "攻击": p, "打斗": p, "殴打": p, "格挡": p, "拳击": p,
                    "射击": a,
                    "欺骗": c, "说谎": c, "伪装": c,
                    "威胁": c, "恐吓": c,
                    "说服": c, "诱惑": c,
                    "施法": i,
                    "游泳": s, "破解": i,
                },
                "medium": {
                    "调查": i, "搜索": i, "观察": i,
                    "询问": c, "打听": c, "交涉": c, "谈判": c,
                    "追踪": i, "解读": i,
                    "修理": i, "制作": i, "治疗": i,
                },
                "easy": {
                    "打招呼": c, "闲聊": c, "问路": c,
                    "翻找": i, "聆听": i, "感知": i,
                    "推动": p, "拉动": p, "搬运": p, "搬开": p, "推开": p, "拉开": p,
                },
            }

        action_lower = action_text.lower()
        difficulty = None
        related_attr = None
        matched_keyword = None

        # 将所有难度的关键词汇总，按长度降序排序（最长优先匹配，避免"打招呼"被"打"抢先）
        all_keywords: list[tuple[str, str, str]] = []  # (keyword, difficulty, attribute)
        for diff_level in ("extreme", "hard", "medium", "easy"):
            level_map = skill_map.get(diff_level, {})
            for kw, attr in level_map.items():
                all_keywords.append((kw, diff_level, attr))
        all_keywords.sort(key=lambda x: len(x[0]), reverse=True)

        for kw, diff_level, attr in all_keywords:
            if kw in action_lower:
                difficulty = diff_level
                related_attr = attr
                matched_keyword = kw
                break

        if not difficulty:
            return None

        # 查找关联属性值
        attrs = self.current_state.get("player", {}).get("attributes", {})
        attr_val = None
        matched_attr_name = related_attr
        if related_attr and attrs:
            # Phase 1: 精确或包含匹配
            for attr_name, val in attrs.items():
                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                if related_attr in attr_name or attr_name in related_attr:
                    attr_val = v
                    matched_attr_name = attr_name
                    break
            # Phase 2: 属性不存在时，用语义类别映射到玩家实际属性
            if attr_val is None:
                synonyms = _ATTR_SYNONYMS.get(related_attr, ())
                if synonyms:
                    for attr_name, val in attrs.items():
                        v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                        name_lower = attr_name.lower()
                        if any(s in name_lower or name_lower in s for s in synonyms):
                            attr_val = v
                            matched_attr_name = attr_name
                            break
        related_attr = matched_attr_name

        # 职业系统增强：查找技能ID和熟练加值
        skill_id = None
        prof_bonus = 0
        if self.class_registry and matched_keyword:
            class_system = self.script.get("settings", {}).get("class_system", "dnd5e")
            skill_id, _ = self.class_registry.find_skill_by_keyword(matched_keyword, class_system)
            if skill_id:
                player_state = self.current_state.get("player", {})
                prof_bonus = self.class_registry.get_proficiency_bonus_for_skill(
                    skill_id, player_state, class_system,
                )
                # CoC: 用技能百分比值替代原始属性值
                if class_system in ("coc", "brp"):
                    skill_val = self.class_registry.get_skill_value(skill_id, player_state, class_system)
                    if skill_val is not None:
                        attr_val = skill_val

        return self._resolve_check(difficulty, related_attr, attr_val,
                                    skill_id=skill_id, prof_bonus=prof_bonus)

    def _maybe_talk_skill_check(self, message: str) -> dict | None:
        """Simplified skill check for NPC dialogue — triggers on persuade/threaten/deceive."""
        if not self.current_state.get("dice_check_enabled", True):
            return None
        talk_skills = {
            "说服": ("魅力", "medium"), "劝说": ("魅力", "medium"),
            "威胁": ("魅力", "hard"), "恐吓": ("魅力", "hard"),
            "欺骗": ("魅力", "hard"), "说谎": ("魅力", "hard"),
            "套话": ("智力", "medium"), "打听": ("魅力", "easy"),
            "求助": ("魅力", "easy"), "请求": ("魅力", "easy"),
        }
        msg_lower = message.lower()
        related_attr = None
        difficulty = None
        for kw, (attr, diff) in talk_skills.items():
            if kw in msg_lower:
                related_attr = attr
                difficulty = diff
                break
        if not difficulty:
            return None
        attrs = self.current_state.get("player", {}).get("attributes", {})
        attr_val = None
        matched_attr_name = related_attr
        if related_attr and attrs:
            for attr_name, val in attrs.items():
                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                if related_attr in attr_name or attr_name in related_attr:
                    attr_val = v
                    matched_attr_name = attr_name
                    break
            if attr_val is None:
                synonyms = _ATTR_SYNONYMS.get(related_attr, ())
                if synonyms:
                    for attr_name, val in attrs.items():
                        v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                        name_lower = attr_name.lower()
                        if any(s in name_lower or name_lower in s for s in synonyms):
                            attr_val = v
                            matched_attr_name = attr_name
                            break
        return self._resolve_check(difficulty, matched_attr_name, attr_val)

    def _resolve_check(self, difficulty: str, related_attr: str, attr_val: int | None,
                        *, skill_id: str | None = None, prof_bonus: int = 0) -> dict:
        """根据剧本 check_rule 设置分发到对应规则的骰子判定。"""
        growth_bonus = 0
        if related_attr:
            growth_entry = self.current_state.get("skill_growth", {}).get(related_attr)
            if growth_entry:
                growth_bonus = growth_entry.get("bonus", 0)

        rule = self.script.get("settings", {}).get("check_rule", "default")
        atmo = self.current_state.get("time_atmosphere", {})
        light = atmo.get("light_level", "bright")
        bonus_dice = 0
        penalty_dice = 0

        if related_attr:
            stealth_kw = ("敏捷", "潜行", "隐蔽", "偷", "dexterity", "stealth")
            visual_kw = ("智力", "观察", "感知", "搜索", "perception")
            r = related_attr.lower()
            if rule == "brp":
                if light == "dark":
                    if any(k in r for k in stealth_kw):
                        bonus_dice += 1
                    elif any(k in r for k in visual_kw):
                        penalty_dice += 1
                elif light == "dim":
                    if any(k in r for k in stealth_kw):
                        bonus_dice += 1
            else:
                if light == "dark":
                    if any(k in r for k in stealth_kw):
                        growth_bonus += 5
                    elif any(k in r for k in visual_kw):
                        growth_bonus -= 5
                elif light == "dim":
                    if any(k in r for k in stealth_kw):
                        growth_bonus += 3

        # BRP 惩罚骰：负面持续状态（受伤、中毒等）
        if rule == "brp":
            _neg_kw = ("受伤", "重伤", "中毒", "眩晕", "恐惧", "疲惫", "疲劳", "虚弱")
            active_ps = self.current_state.get("active_persistent_states", [])
            dn = self.current_state.get("display_names", {})
            for sid in active_ps:
                name = dn.get(sid, sid)
                if any(k in name for k in _neg_kw):
                    penalty_dice += 1
                    break

        da = self.current_state.get("difficulty_awareness", {})
        adj = da.get("adjustment", "neutral")
        if adj == "ease":
            growth_bonus += 4
        elif adj == "challenge":
            growth_bonus -= 3

        # 检定势头：同属性连续成功/失败给予加成/惩罚
        momentum = self.current_state.get("check_momentum", {})
        if related_attr:
            attr_mom = momentum.get(related_attr, {})
            if self.turn_number - attr_mom.get("last_turn", 0) > 3:
                attr_mom = {"streak": 0, "last_turn": 0}
            streak = attr_mom.get("streak", 0)
            if streak >= 2:
                growth_bonus += min(streak * 2, 8)
            elif streak <= -2:
                growth_bonus += max(streak * 2, -8)

        momentum_streak = streak if related_attr else 0

        if rule == "brp":
            result = self._check_brp(difficulty, related_attr, attr_val,
                                   growth_bonus=growth_bonus,
                                   bonus_dice=bonus_dice, penalty_dice=penalty_dice)
        elif rule == "dnd":
            result = self._check_dnd(difficulty, related_attr, attr_val,
                                   prof_bonus=prof_bonus, skill_id=skill_id, growth_bonus=growth_bonus)
        else:
            result = self._check_default(difficulty, related_attr, attr_val, growth_bonus=growth_bonus)

        if momentum_streak:
            result["momentum_streak"] = momentum_streak

        # 更新势头
        if related_attr:
            momentum = self.current_state.setdefault("check_momentum", {})
            entry = momentum.setdefault(related_attr, {"streak": 0, "last_turn": 0})
            outcome = result.get("outcome", "")
            if "success" in outcome:
                entry["streak"] = (entry["streak"] + 1) if entry["streak"] > 0 else 1
            else:
                entry["streak"] = (entry["streak"] - 1) if entry["streak"] < 0 else -1
            entry["last_turn"] = self.turn_number

        return result

    def _check_default(self, difficulty: str, related_attr: str, attr_val: int | None,
                        *, growth_bonus: int = 0) -> dict:
        """默认规则：d100 ≥ threshold 为成功。"""
        base_thresholds = {"easy": 25, "medium": 40, "hard": 60, "extreme": 80}
        threshold = base_thresholds.get(difficulty, 40)
        if attr_val is not None:
            threshold = threshold - int((attr_val - 50) / 10 * 5)
        threshold -= growth_bonus
        threshold = max(10, min(90, threshold))
        rng = getattr(self.dice, "_rng", random)
        roll = rng.randint(1, 100)
        gap = roll - threshold
        if roll >= 95:
            outcome = "critical_success"
            hint = "大成功！行动完美达成，获得额外收益"
        elif roll >= threshold:
            outcome = "success"
            if gap >= 20:
                hint = "轻松成功。行动游刃有余"
            elif gap >= 10:
                hint = "成功。行动顺利完成"
            else:
                hint = "险些成功。行动勉强达成"
        elif roll >= 5:
            outcome = "failure"
            if gap >= -5:
                hint = "差一点就成功了！功亏一篑"
            elif gap >= -20:
                hint = "失败。行动未能达成"
            else:
                hint = "远远不够。行动彻底失败"
        else:
            outcome = "critical_failure"
            hint = "大失败！产生严重负面后果"
        attr_info = f"（{related_attr}={attr_val}）" if related_attr and attr_val is not None else ""
        return {
            "roll": roll, "threshold": threshold, "difficulty": difficulty,
            "related_attribute": related_attr, "attr_value": attr_val,
            "gap": gap, "outcome": outcome, "rule": "default",
            "narrative_hint": f"{hint}{attr_info}",
        }

    def _check_brp(self, difficulty: str, related_attr: str, attr_val: int | None,
                    *, growth_bonus: int = 0,
                    bonus_dice: int = 0, penalty_dice: int = 0) -> dict:
        """BRP/CoC 7e 规则：d100 ≤ 技能值为成功。

        难度缩放：普通=原值，困难=½，极难=⅕。
        简单难度给一颗奖励骰而非缩放。
        奖励骰/惩罚骰：额外投十位骰，取最有利/最不利的。
        """
        skill = (attr_val if attr_val is not None else 50) + growth_bonus
        scale = {"easy": 1.0, "medium": 1.0, "hard": 0.5, "extreme": 0.2}
        effective = max(1, int(skill * scale.get(difficulty, 1.0)))
        if difficulty == "easy":
            bonus_dice += 1

        # 奖励骰与惩罚骰互相抵消
        net = bonus_dice - penalty_dice
        extra = abs(net)

        rng = getattr(self.dice, "_rng", random)
        units = rng.randint(0, 9)
        tens_rolls = [rng.randint(0, 9) for _ in range(1 + extra)]
        if net > 0:
            chosen_tens = min(tens_rolls)
        elif net < 0:
            chosen_tens = max(tens_rolls)
        else:
            chosen_tens = tens_rolls[0]

        roll = chosen_tens * 10 + units
        if roll == 0:
            roll = 100

        # CoC 7e 大成功/大失败判定
        if roll == 1:
            outcome = "critical_success"
            hint = "大成功！决定性的极限发挥"
        elif roll <= effective:
            outcome = "success"
            margin = effective - roll
            if margin >= 20:
                hint = "轻松成功。技巧游刃有余"
            elif margin >= 5:
                hint = "成功。顺利完成"
            else:
                hint = "险些成功。刚好在能力范围内"
        elif ((attr_val or 50) < 50 and roll >= 96) or roll == 100:
            outcome = "critical_failure"
            hint = "大失败！灾难性的失误"
        else:
            outcome = "failure"
            overshoot = roll - effective
            if overshoot <= 10:
                hint = "差一点就成功了"
            elif overshoot <= 30:
                hint = "失败。超出能力范围"
            else:
                hint = "远远不够。完全力不从心"

        attr_info = f"（{related_attr}={skill}）" if related_attr else ""
        dice_info = {}
        if net != 0:
            dice_info = {
                "type": "bonus" if net > 0 else "penalty",
                "tens_rolls": tens_rolls,
                "units": units,
                "chosen_tens": chosen_tens,
            }
        return {
            "roll": roll, "threshold": effective, "difficulty": difficulty,
            "related_attribute": related_attr, "attr_value": attr_val,
            "outcome": outcome, "rule": "brp",
            "narrative_hint": f"{hint}{attr_info}",
            "dice_info": dice_info,
        }

    def _check_dnd(self, difficulty: str, related_attr: str, attr_val: int | None,
                   *, prof_bonus: int = 0, skill_id: str | None = None,
                   growth_bonus: int = 0) -> dict:
        """D&D规则：d20 + 修正值 + 熟练加值 ≥ DC 为成功。"""
        mod = (attr_val - 50) // 5 if attr_val is not None else 0
        mod += prof_bonus + growth_bonus
        dc_map = {"easy": 8, "medium": 12, "hard": 16, "extreme": 20}
        dc = dc_map.get(difficulty, 12)
        rng = getattr(self.dice, "_rng", random)
        roll = rng.randint(1, 20)
        total = roll + mod
        if roll == 20:
            outcome = "critical_success"
            hint = "天命20！完美发挥，获得额外收益"
        elif roll == 1:
            outcome = "critical_failure"
            hint = "天命1！灾难性失误"
        elif total >= dc:
            outcome = "success"
            margin = total - dc
            if margin >= 8:
                hint = "轻松成功。游刃有余"
            elif margin >= 3:
                hint = "成功。顺利完成"
            else:
                hint = "险些成功。勉强通过"
        else:
            outcome = "failure"
            shortfall = dc - total
            if shortfall <= 3:
                hint = "差一点就成功了"
            elif shortfall <= 8:
                hint = "失败。能力不足"
            else:
                hint = "远远不够。完全无法达成"
        mod_str = f"+{mod}" if mod >= 0 else str(mod)
        prof_str = f"(含熟练+{prof_bonus})" if prof_bonus else ""
        skill_name = ""
        if skill_id and self.class_registry:
            sd = self.class_registry.get_skill(skill_id)
            if sd:
                skill_name = sd.get("name", "")
        attr_info = f"（{skill_name or related_attr}{mod_str}{prof_str}）" if related_attr else ""
        return {
            "roll": roll, "modifier": mod, "total": total, "threshold": dc,
            "difficulty": difficulty, "related_attribute": related_attr,
            "attr_value": attr_val, "outcome": outcome, "rule": "dnd",
            "skill_id": skill_id, "proficiency_bonus": prof_bonus,
            "narrative_hint": f"{hint}{attr_info}",
        }

    def _apply_skill_growth(self, check_result: dict):
        """Award XP to the attribute used in a skill check and handle level-ups."""
        attr_name = check_result.get("related_attribute")
        if not attr_name:
            return
        outcome = check_result.get("outcome", "")
        xp_map = {
            "critical_success": 20,
            "success": 10,
            "failure": 5,
            "critical_failure": 15,
        }
        xp_gain = xp_map.get(outcome, 5)
        growth = self.current_state.setdefault("skill_growth", {})
        entry = growth.setdefault(attr_name, {"xp": 0, "level": 0, "bonus": 0})
        entry["xp"] += xp_gain
        old_level = entry["level"]
        # Level N -> N+1 costs N*50 XP (level 0->1 = 50, 1->2 = 50, etc.), cap at 5
        while entry["level"] < 5:
            needed = max(1, entry["level"]) * 50 if entry["level"] > 0 else 50
            if entry["xp"] >= needed:
                entry["xp"] -= needed
                entry["level"] += 1
                entry["bonus"] = entry["level"] * 2
            else:
                break
        if entry["level"] > old_level:
            self._record_narrative_callback(
                f"你的{attr_name}技能因反复磨练提升到了{entry['level']}级",
                ["skill_growth", attr_name], "medium",
            )
        # Inject growth bonus into check_result so prompt_builder can reference it
        check_result["skill_growth_bonus"] = entry["bonus"]
        check_result["skill_level"] = entry["level"]

    def _record_check_outcome(self, outcome: str):
        """Append a check outcome to the rolling history for difficulty awareness."""
        history = self.current_state.setdefault("_check_result_history", [])
        history.append(outcome)
        if len(history) > 20:
            self.current_state["_check_result_history"] = history[-20:]
