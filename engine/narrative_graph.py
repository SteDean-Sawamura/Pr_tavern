"""叙事知识图谱：追踪角色关系、事件因果、物品流转"""


class NarrativeGraph:
    def __init__(self):
        self.nodes = {}  # id → {type, name, properties}
        self.edges = []  # [{from, to, relation, turn, description}]

    def add_entity(self, id, type, name, **props):
        """添加实体节点（NPC/地点/物品/事件）"""
        self.nodes[id] = {"type": type, "name": name, **props}

    def add_relation(self, from_id, to_id, relation, turn=0, description=""):
        """添加关系边"""
        self.edges.append({
            "from": from_id, "to": to_id,
            "relation": relation, "turn": turn,
            "description": description,
        })

    def query(self, entity_id, depth=2):
        """从某实体出发，返回 depth 层内的关联网络"""
        visited = set()
        result = {"entity": self.nodes.get(entity_id), "relations": []}
        self._traverse(entity_id, depth, visited, result["relations"])
        return result

    def _traverse(self, eid, depth, visited, relations):
        if depth <= 0 or eid in visited:
            return
        visited.add(eid)
        for edge in self.edges:
            if edge["from"] == eid:
                relations.append({**edge, "target_name": self.nodes.get(edge["to"], {}).get("name", edge["to"])})
                self._traverse(edge["to"], depth - 1, visited, relations)
            elif edge["to"] == eid:
                relations.append({**edge, "source_name": self.nodes.get(edge["from"], {}).get("name", edge["from"])})
                self._traverse(edge["from"], depth - 1, visited, relations)

    def get_timeline(self, entity_id=None):
        """获取事件时间线"""
        events = [e for e in self.edges if e["relation"] in ("triggered", "caused", "witnessed")]
        if entity_id:
            events = [e for e in events if entity_id in (e["from"], e["to"])]
        return sorted(events, key=lambda e: e.get("turn", 0))

    def add_causal_link(self, cause_id: str, effect_id: str, turn: int, description: str = ""):
        """记录因果关系：A 导致 B"""
        self.edges.append({
            "from": cause_id, "to": effect_id,
            "relation": "caused",
            "turn": turn, "description": description,
        })

    def trace_causes(self, entity_id: str, max_depth: int = 5) -> list[dict]:
        """追溯某个事件/状态的因果链"""
        chain = []
        visited = set()
        self._trace_back(entity_id, max_depth, visited, chain)
        return chain

    def _trace_back(self, eid, depth, visited, chain):
        if depth <= 0 or eid in visited:
            return
        visited.add(eid)
        for edge in self.edges:
            if edge["to"] == eid and edge["relation"] == "caused":
                chain.append({
                    "cause": edge["from"],
                    "cause_name": self.nodes.get(edge["from"], {}).get("name", edge["from"]),
                    "effect": eid,
                    "effect_name": self.nodes.get(eid, {}).get("name", eid),
                    "turn": edge.get("turn", 0),
                    "description": edge.get("description", ""),
                })
                self._trace_back(edge["from"], depth - 1, visited, chain)

    def trace_effects(self, entity_id: str, max_depth: int = 5) -> list[dict]:
        """追踪某个事件/状态的后续影响"""
        chain = []
        visited = set()
        self._trace_forward(entity_id, max_depth, visited, chain)
        return chain

    def _trace_forward(self, eid, depth, visited, chain):
        if depth <= 0 or eid in visited:
            return
        visited.add(eid)
        for edge in self.edges:
            if edge["from"] == eid and edge["relation"] == "caused":
                chain.append({
                    "cause": eid,
                    "effect": edge["to"],
                    "effect_name": self.nodes.get(edge["to"], {}).get("name", edge["to"]),
                    "turn": edge.get("turn", 0),
                })
                self._trace_forward(edge["to"], depth - 1, visited, chain)

    def snapshot(self):
        return {"nodes": self.nodes, "edges": self.edges}

    @classmethod
    def from_snapshot(cls, data):
        g = cls()
        g.nodes = data.get("nodes", {})
        g.edges = data.get("edges", [])
        return g
