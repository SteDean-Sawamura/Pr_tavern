"""玩家建模：追踪玩家偏好，供 Agent 自适应"""


class PlayerModel:
    """追踪玩家偏好，供 Agent 自适应"""

    def __init__(self):
        self.choice_history = []  # 最近 20 个选择的 risk 分布
        self.style_tags = {}      # {"cautious": 0.7, "exploratory": 0.3, ...}
        self.avg_action_length = 0
        self.preferred_pace = "normal"  # slow/normal/fast

    def record_choice(self, choice):
        """记录玩家选择"""
        risk = choice.get("risk", "moderate")
        self.choice_history.append(risk)
        if len(self.choice_history) > 20:
            self.choice_history.pop(0)
        self._update_tags()

    def _update_tags(self):
        if not self.choice_history:
            return
        safe = self.choice_history.count("safe") / len(self.choice_history)
        risky = self.choice_history.count("risky") / len(self.choice_history)
        self.style_tags = {
            "cautious": round(safe, 2),
            "balanced": round(1 - safe - risky, 2),
            "aggressive": round(risky, 2),
        }
        # 节奏推断
        if safe > 0.6:
            self.preferred_pace = "slow"
        elif risky > 0.4:
            self.preferred_pace = "fast"
        else:
            self.preferred_pace = "normal"

    def hint_for_agent(self):
        """生成给 Agent 的玩家偏好提示"""
        if not self.style_tags:
            return ""
        dominant = max(self.style_tags, key=self.style_tags.get)
        hints = {
            "cautious": "玩家偏好谨慎行动，选项应提供充分的安全选择",
            "aggressive": "玩家偏好冒险，叙事节奏可以更快，选项可以更大胆",
            "balanced": "玩家风格均衡",
        }
        return f"[玩家画像] {hints.get(dominant, '')} 节奏偏好：{self.preferred_pace}"

    def snapshot(self):
        return {
            "choice_history": self.choice_history,
            "style_tags": self.style_tags,
            "preferred_pace": self.preferred_pace,
        }

    @classmethod
    def from_snapshot(cls, data):
        m = cls()
        m.choice_history = data.get("choice_history", [])
        m.style_tags = data.get("style_tags", {})
        m.preferred_pace = data.get("preferred_pace", "normal")
        return m
