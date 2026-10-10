"""导演笔记：后台独立子代理整理的叙事摘要，作为记忆中间层。
比历史检索精确（已整理过），比完整正文节省 token。"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DirectorNotes:
    def __init__(self):
        self.notes = []  # [{turn, category, content, source_turns, created_at}]
        self.max_notes = 30

    def add_note(self, turn: int, category: str, content: str, source_turns: list[int] = None):
        """添加一条导演笔记"""
        self.notes.append({
            "turn": turn,
            "category": category,  # character/plot/world/relationship/clue
            "content": content,
            "source_turns": source_turns or [turn],
            "created_at": datetime.now().isoformat(),
        })
        if len(self.notes) > self.max_notes:
            self.notes.pop(0)

    def get_notes(self, categories: list[str] = None, limit: int = 10) -> list[dict]:
        """获取笔记，可按类别过滤"""
        result = self.notes
        if categories:
            result = [n for n in result if n["category"] in categories]
        return result[-limit:]

    def get_summary(self, limit: int = 5) -> str:
        """获取最近笔记的摘要文本"""
        recent = self.notes[-limit:]
        if not recent:
            return ""
        lines = []
        for n in recent:
            lines.append(f"[T{n['turn']}|{n['category']}] {n['content']}")
        return "\n".join(lines)

    def search(self, keyword: str) -> list[dict]:
        """关键词搜索笔记"""
        return [n for n in self.notes if keyword in n.get("content", "")]

    def snapshot(self):
        return {"notes": self.notes}

    @classmethod
    def from_snapshot(cls, data):
        d = cls()
        d.notes = data.get("notes", [])
        return d
