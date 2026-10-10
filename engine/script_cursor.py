"""剧本游标：跟踪线性剧本的当前进度位置"""


class ScriptCursor:
    """
    剧本游标状态机：
    - prepare(text) → 根据叙事文本匹配剧本位置，暂存
    - commit() → 确认推进到暂存位置
    - restore() → 回退到上次 commit 的位置
    - focus(position) → 用户手动跳转
    """

    def __init__(self, script_blocks: list[dict]):
        """
        script_blocks: [{id, text, summary, order}] 剧本的线性分块
        """
        self.blocks = script_blocks
        self.position = 0           # 当前确认的位置
        self._staged_position = None  # 暂存位置（prepare 后、commit 前）
        self.ended = False

    def prepare(self, narrative_text: str) -> dict:
        """根据叙事文本匹配最佳剧本位置（3-gram 加权匹配）"""
        if self.ended or not self.blocks:
            return {"position": self.position, "matched": False}

        best_pos = self.position
        best_score = 0

        # 从当前位置开始向前搜索（不回退）
        for i in range(self.position, len(self.blocks)):
            score = self._match_score(narrative_text, self.blocks[i].get("text", ""))
            if score > best_score:
                best_score = score
                best_pos = i

        self._staged_position = best_pos
        return {
            "position": best_pos,
            "block_id": self.blocks[best_pos].get("id", ""),
            "score": best_score,
            "matched": best_score > 0,
            "summary": self.blocks[best_pos].get("summary", "")[:100],
        }

    def commit(self) -> int:
        """确认推进到暂存位置"""
        if self._staged_position is not None:
            self.position = self._staged_position
            self._staged_position = None
            if self.position >= len(self.blocks) - 1:
                self.ended = True
        return self.position

    def restore(self):
        """丢弃暂存，回到上次 commit 的位置"""
        self._staged_position = None

    def focus(self, position: int):
        """用户手动跳转（只能前进不能后退）"""
        if position >= self.position:
            self.position = max(self.position, position - 1)
            self._staged_position = None

    def current_block(self) -> dict | None:
        """获取当前位置的剧本块"""
        if 0 <= self.position < len(self.blocks):
            return self.blocks[self.position]
        return None

    def next_blocks(self, count: int = 2) -> list[dict]:
        """获取接下来的几个剧本块（供 Agent 参考）"""
        start = self.position + 1
        return self.blocks[start:start + count]

    def progress(self) -> float:
        """剧本完成进度 0.0-1.0"""
        if not self.blocks:
            return 0.0
        return self.position / max(len(self.blocks) - 1, 1)

    @staticmethod
    def _match_score(text_a: str, text_b: str) -> float:
        """3-gram 加权匹配分数"""
        if not text_a or not text_b:
            return 0.0

        def ngrams(text, n=3):
            text = text.replace(" ", "").replace("\n", "")
            return set(text[i:i+n] for i in range(len(text) - n + 1))

        grams_a = ngrams(text_a)
        grams_b = ngrams(text_b)

        if not grams_a or not grams_b:
            return 0.0

        overlap = len(grams_a & grams_b)
        return overlap / min(len(grams_a), len(grams_b))

    def snapshot(self) -> dict:
        return {
            "position": self.position,
            "staged": self._staged_position,
            "ended": self.ended,
            "progress": self.progress(),
        }

    @classmethod
    def from_snapshot(cls, blocks, snap: dict):
        c = cls(blocks)
        c.position = snap.get("position", 0)
        c._staged_position = snap.get("staged")
        c.ended = snap.get("ended", False)
        return c
