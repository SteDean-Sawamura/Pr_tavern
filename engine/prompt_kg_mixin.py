"""PromptBuilder Mixin: 知识图谱（KG）相关方法"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class PromptKGMixin:
    """知识图谱构建、刷新与位置场景更新"""

    @staticmethod
    def _build_kg_helper_maps(script: dict) -> dict:
        """Build shared helper maps for KG content generation."""
        npcs = script.get("npcs", [])
        npc_map = {n["id"]: n for n in npcs}
        organizations = script.get("organizations", [])
        org_by_id = {o["id"]: o for o in organizations if o.get("id")}
        org_rank_title: dict[str, dict[int, str]] = {}
        for o in organizations:
            oid = o.get("id", "")
            hierarchy = o.get("hierarchy", [])
            if hierarchy:
                org_rank_title[oid] = {h["rank"]: h["title"] for h in hierarchy if "rank" in h and "title" in h}
        org_members: dict[str, list[dict]] = {}
        subordinates: dict[str, list[dict]] = {}
        for npc in npcs:
            for om in npc.get("organizations", []):
                oid = om.get("org_id", "")
                if oid:
                    org_members.setdefault(oid, []).append(npc)
            sup_id = npc.get("superior")
            if sup_id:
                subordinates.setdefault(sup_id, []).append(npc)
        return {
            "npcs": npcs, "npc_map": npc_map,
            "organizations": organizations, "org_by_id": org_by_id,
            "org_rank_title": org_rank_title,
            "org_members": org_members, "subordinates": subordinates,
        }

    @staticmethod
    def _build_kg_npc_content(npc: dict, maps: dict, npc_rels: list[dict]) -> tuple[str, list[str], list[str]]:
        """Build KG content for a single NPC. Returns (content, keys, rel_ids)."""
        npc_id = npc["id"]
        name = npc.get("name", npc_id)
        keys = [name, npc_id]
        if npc.get("title"):
            keys.append(npc["title"])

        npc_map = maps["npc_map"]
        org_by_id = maps["org_by_id"]
        org_rank_title = maps["org_rank_title"]
        subordinates = maps["subordinates"]

        lines = [f"【{name}】"]
        if npc.get("bio"):
            lines.append(f"简介: {npc['bio']}")
        if npc.get("personality"):
            lines.append(f"性格: {npc['personality']}")
        if npc.get("capabilities"):
            lines.append(f"能力: {npc['capabilities']}")
        if npc.get("title"):
            lines.append(f"头衔: {npc['title']}")
        npc_orgs = npc.get("organizations", [])
        if npc_orgs:
            org_labels = []
            for om in npc_orgs:
                om_oid = om.get("org_id", "")
                org_name = org_by_id.get(om_oid, {}).get("name", om_oid)
                npc_rank = om.get("rank")
                rank_map = org_rank_title.get(om_oid, {})
                rank_title = rank_map.get(npc_rank) if npc_rank is not None else None
                label = org_name
                if rank_title:
                    label += f"（{rank_title}）"
                role = om.get("role", "")
                if role:
                    label += f"[{role}]"
                org_labels.append(label)
            lines.append(f"所属组织: {'、'.join(org_labels)}")
        if npc.get("superior"):
            sup_name = npc_map.get(npc["superior"], {}).get("name", npc["superior"])
            lines.append(f"上级: {sup_name}")
        subs = subordinates.get(npc_id, [])
        if subs:
            sub_names = "、".join(s.get("name", s["id"]) for s in subs)
            lines.append(f"下属: {sub_names}")
        if npc.get("default_location"):
            lines.append(f"通常出没: {npc['default_location']}")

        rel_ids = []
        # Link to organization KG entries for deterministic Phase 3 activation
        for om in npc_orgs:
            om_oid = om.get("org_id", "")
            if om_oid:
                rel_ids.append(f"_kg_org_{om_oid}")
        for rel in npc_rels:
            if "from" in rel:
                f_id, t_id = rel.get("from", ""), rel.get("to", "")
                if npc_id in (f_id, t_id):
                    other_id = t_id if f_id == npc_id else f_id
                    other_name = npc_map.get(other_id, {}).get("name", other_id)
                    t, a, fe = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                    lines.append(f"与{other_name}: 信任{t}/好感{a}/畏惧{fe}" +
                                 (f" — {rel['description']}" if rel.get("description") else ""))
                    rel_ids.append(f"_kg_rel_{f_id}_{t_id}")
            else:
                a_id, b_id = rel.get("a", ""), rel.get("b", "")
                if npc_id in (a_id, b_id):
                    other_id = b_id if a_id == npc_id else a_id
                    other_name = npc_map.get(other_id, {}).get("name", other_id)
                    lines.append(f"与{other_name}的关系: {rel.get('type', '中立')}" +
                                 (f" — {rel['description']}" if rel.get("description") else ""))
                    rel_ids.append(f"_kg_rel_{a_id}_{b_id}")

        return "\n".join(lines), keys, rel_ids

    @staticmethod
    def _build_kg_org_content(org: dict, maps: dict) -> tuple[str, list[str]]:
        """Build KG content for a single organization. Returns (content, keys)."""
        org_id = org.get("id", "")
        org_name = org.get("name", org_id)
        keys = [org_name] + org.get("aliases", [])
        npc_map = maps["npc_map"]
        org_rank_title = maps["org_rank_title"]
        org_members = maps["org_members"]

        org_type = org.get("type", "")
        label = f"【{org_type or '组织'}】{org_name}" if org_type else f"【组织】{org_name}"
        lines = [label]
        if org.get("stance"):
            lines[0] += f"（立场: {org['stance']}）"
        if org.get("leader"):
            leader_name = npc_map.get(org["leader"], {}).get("name", org["leader"])
            lines.append(f"领导者: {leader_name}")
        if org.get("description"):
            lines.append(org["description"])
        hierarchy = org.get("hierarchy", [])
        if hierarchy:
            rank_labels = "→".join(h["title"] for h in sorted(hierarchy, key=lambda h: h.get("rank", 99)))
            lines.append(f"层级: {rank_labels}")
        if org.get("parent_org"):
            parent_name = maps["org_by_id"].get(org["parent_org"], {}).get("name", org["parent_org"])
            lines.append(f"上级组织: {parent_name}")
        child_orgs = [o.get("name", o.get("id", "")) for o in maps["organizations"] if o.get("parent_org") == org_id]
        if child_orgs:
            lines.append(f"下属组织: {'、'.join(child_orgs)}")
        rank_map = org_rank_title.get(org_id, {})
        members = org_members.get(org_id, [])
        if members:
            def _member_rank(m):
                for om in m.get("organizations", []):
                    if om.get("org_id") == org_id and om.get("rank") is not None:
                        return om["rank"]
                return 999
            sorted_members = sorted(members, key=_member_rank)
            parts = []
            for m in sorted_members:
                m_name = m.get("name", m["id"])
                m_rank = _member_rank(m)
                rank_title = rank_map.get(m_rank) if m_rank != 999 else None
                om_entry = next((om for om in m.get("organizations", []) if om.get("org_id") == org_id), {})
                role = om_entry.get("role", "")
                detail = rank_title or role or m.get("title", "")
                sup_id = m.get("superior")
                if sup_id:
                    sup_name = npc_map.get(sup_id, {}).get("name", sup_id)
                    detail += f"，上级: {sup_name}" if detail else f"上级: {sup_name}"
                parts.append(f"{m_name}（{detail}）" if detail else m_name)
            lines.append(f"成员: {'、'.join(parts)}")
        goals = org.get("goals", [])
        if goals:
            goal_descs = [g.get("description", g.get("id", "")) for g in goals[:3]]
            lines.append(f"目标: {'；'.join(goal_descs)}")
        return "\n".join(lines), keys

    @staticmethod
    def _build_kg_loc_content(loc: dict, loc_name_map: dict) -> tuple[str, list[str]]:
        """Build KG content for a single location. Returns (content, keys)."""
        loc_id = loc.get("id", "")
        loc_name = loc.get("name", loc_id)
        keys = [loc_name, loc_id]
        conns = loc.get("connections", [])

        lines = [f"【地点】{loc_name}", f"地点ID: {loc_id}"]
        if loc.get("description"):
            lines.append(loc["description"])
        if loc.get("travel_time"):
            lines.append(f"移动耗时: {loc['travel_time']}")
        if loc.get("access_condition"):
            lines.append(f"进入条件: {loc['access_condition']}")
        if conns:
            conn_names = [loc_name_map.get(c, c) for c in conns]
            lines.append(f"相连地点: {'、'.join(conn_names)}")
        return "\n".join(lines), keys

    @staticmethod
    def _generate_knowledge_graph(script: dict) -> list[dict]:
        """Auto-generate lorebook entries from NPCs, relationships, events, orgs, locations."""
        entries = []
        npcs = script.get("npcs", [])
        npc_map = {n["id"]: n for n in npcs}
        npc_rels = script.get("npc_relationships", [])
        organizations = script.get("organizations", [])
        org_rels = script.get("org_relationships", [])

        maps = PromptKGMixin._build_kg_helper_maps(script)

        # 1. NPC profile entries
        for npc in npcs:
            content, keys, rel_ids = PromptKGMixin._build_kg_npc_content(npc, maps, npc_rels)
            entries.append({
                "id": f"_kg_npc_{npc['id']}",
                "keys": keys,
                "content": content,
                "entry_type": "npc_profile",
                "related_entries": rel_ids,
                "priority": 90,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"NPC档案: {npc.get('name', npc['id'])}",
            })

        # 2. NPC-NPC relationship entries
        for rel in npc_rels:
            if "from" in rel:
                from_id = rel.get("from", "")
                to_id = rel.get("to", "")
                from_name = npc_map.get(from_id, {}).get("name", from_id)
                to_name = npc_map.get(to_id, {}).get("name", to_id)
                t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                desc = rel.get("description", "")
                content = f"{from_name} → {to_name}: 信任{t}/好感{a}/畏惧{f}"
                if desc:
                    content += f"\n{desc}"
                entries.append({
                    "id": f"_kg_rel_{from_id}_{to_id}",
                    "keys": [from_name, to_name],
                    "content": content,
                    "entry_type": "npc_relationship",
                    "related_entries": [f"_kg_npc_{from_id}", f"_kg_npc_{to_id}"],
                    "priority": 80,
                    "position": "after_world",
                    "constant": False,
                    "enabled": True,
                    "scan_depth": 3,
                    "comment": f"NPC关系: {from_name} → {to_name}",
                })
            else:
                a_id = rel.get("a", "")
                b_id = rel.get("b", "")
                a_name = npc_map.get(a_id, {}).get("name", a_id)
                b_name = npc_map.get(b_id, {}).get("name", b_id)
                rel_type = rel.get("type", "中立")
                desc = rel.get("description", "")
                content = f"{a_name} ↔ {b_name}: {rel_type}"
                if desc:
                    content += f"\n{desc}"
                entries.append({
                    "id": f"_kg_rel_{a_id}_{b_id}",
                    "keys": [a_name, b_name],
                    "content": content,
                    "entry_type": "npc_relationship",
                    "related_entries": [f"_kg_npc_{a_id}", f"_kg_npc_{b_id}"],
                    "priority": 80,
                    "position": "after_world",
                    "constant": False,
                    "enabled": True,
                    "scan_depth": 3,
                    "comment": f"NPC关系: {a_name} ↔ {b_name}",
                })

        # 3. Event context entries
        for evt in script.get("one_time_events", []):
            evt_desc = evt.get("description", "")
            if not evt_desc:
                continue
            evt_keys = []
            trigger = evt.get("trigger", {})
            conditions = trigger.get("conditions", []) if isinstance(trigger, dict) else []
            for cond in conditions:
                target = cond.get("target", "")
                for npc in npcs:
                    if npc["id"] in target:
                        evt_keys.append(npc.get("name", npc["id"]))
            if evt.get("id"):
                evt_keys.append(evt["id"])
            if not evt_keys:
                continue
            entries.append({
                "id": f"_kg_evt_{evt.get('id', 'unknown')}",
                "keys": evt_keys,
                "content": f"事件: {evt_desc}",
                "entry_type": "event_context",
                "available_from": evt.get("trigger_time", ""),
                "priority": 70,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"事件: {evt.get('id', '?')}",
            })

        # 4. Organization entries
        for org in organizations:
            content, keys = PromptKGMixin._build_kg_org_content(org, maps)
            entries.append({
                "id": f"_kg_org_{org.get('id', '')}",
                "keys": keys,
                "content": content,
                "entry_type": "organization",
                "priority": 85,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"组织: {org.get('name', org.get('id', ''))}",
            })

        # 5. Inter-organization relationship entries
        org_by_id = maps["org_by_id"]
        for orel in org_rels:
            a_id = orel.get("a", "")
            b_id = orel.get("b", "")
            a_name = org_by_id.get(a_id, {}).get("name", a_id)
            b_name = org_by_id.get(b_id, {}).get("name", b_id)
            rel_type = orel.get("type", "中立")
            desc = orel.get("description", "")
            content = f"{a_name} ↔ {b_name}: {rel_type}"
            if desc:
                content += f"\n{desc}"
            entries.append({
                "id": f"_kg_orel_{a_id}_{b_id}",
                "keys": [a_name, b_name],
                "content": content,
                "entry_type": "org_relationship",
                "visibility": orel.get("visibility", "public"),
                "known_by_npcs": orel.get("known_by_npcs", []),
                "discoverable": orel.get("discoverable", {}),
                "priority": 80,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"组织关系: {a_name} ↔ {b_name}",
            })

        # 6. Location entries
        loc_name_map = {l["id"]: l.get("name", l["id"]) for l in script.get("locations", [])}
        for loc in script.get("locations", []):
            if not loc.get("description"):
                continue
            content, keys = PromptKGMixin._build_kg_loc_content(loc, loc_name_map)
            entries.append({
                "id": f"_kg_loc_{loc['id']}",
                "keys": keys,
                "content": content,
                "entry_type": "location",
                "priority": 75,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"地点: {loc.get('name', loc['id'])}",
            })

        return entries

    def refresh_kg_entries(self, changed_npc_ids: list[str], changed_org_ids: list[str]):
        """Refresh KG lorebook entries for NPCs/orgs whose data changed at runtime."""
        if not changed_npc_ids and not changed_org_ids:
            return
        maps = self._build_kg_helper_maps(self.script)
        npc_rels = self.script.get("npc_relationships", [])
        npc_by_id = maps["npc_map"]
        for npc_id in changed_npc_ids:
            npc = npc_by_id.get(npc_id)
            if not npc:
                continue
            content, keys, _ = self._build_kg_npc_content(npc, maps, npc_rels)
            self.lorebook.update_entry(f"_kg_npc_{npc_id}", content, keys)
        org_by_id = maps["org_by_id"]
        for org_id in changed_org_ids:
            org = org_by_id.get(org_id)
            if not org:
                continue
            content, keys = self._build_kg_org_content(org, maps)
            self.lorebook.update_entry(f"_kg_org_{org_id}", content, keys)

    def add_location_kg_entry(self, loc_id: str, loc_name: str, description: str = ""):
        """Add a lorebook entry for a dynamically revealed location."""
        entry_id = f"_kg_loc_{loc_id}"
        if any(e.id == entry_id for e in self.lorebook.entries):
            return
        loc = {"id": loc_id, "name": loc_name, "description": description}
        content, keys = self._build_kg_loc_content(loc, self._loc_name_map)
        self.lorebook.add_entries([{
            "id": entry_id,
            "keys": keys,
            "content": content,
            "entry_type": "location",
            "priority": 75,
            "position": "after_world",
            "constant": False,
            "enabled": True,
            "scan_depth": 3,
            "comment": f"地点: {loc_name}",
        }])

    def update_location_scene(self, loc_id: str, scene_details: dict):
        """Update location scene in KG entry (if exists) + maintain a dynamic lorebook entry for physical facts."""
        # --- KG entry update (existing behavior) ---
        kg_entry_id = f"_kg_loc_{loc_id}"
        for e in self.lorebook.entries:
            if e.id == kg_entry_id:
                parts = []
                if scene_details.get("atmosphere"):
                    parts.append(f"氛围: {scene_details['atmosphere']}")
                if scene_details.get("sensory"):
                    parts.append(f"感官: {scene_details['sensory']}")
                if parts:
                    scene_block = "\n[当前场景]\n" + "\n".join(parts)
                    base = re.sub(r'\n\[当前场景\]\n.*', '', e.content, flags=re.DOTALL)
                    self.lorebook.update_entry(kg_entry_id, base + scene_block)
                break

        # --- Dynamic lorebook entry for stable physical facts ---
        physical = scene_details.get("physical")
        if not physical or not isinstance(physical, dict):
            return
        phys_parts = []
        loc_name = self._resolve_location_name(loc_id)
        if physical.get("lighting"):
            phys_parts.append(f"照明: {physical['lighting']}")
        if physical.get("floor"):
            phys_parts.append(f"地面: {physical['floor']}")
        if physical.get("spatial_note"):
            phys_parts.append(f"空间: {physical['spatial_note']}")
        if not phys_parts:
            return
        content = f"{loc_name}的物理环境:\n" + "\n".join(phys_parts)
        scene_entry_id = f"_scene_loc_{loc_id}"
        existing = any(e.id == scene_entry_id for e in self.lorebook.entries)
        if existing:
            self.lorebook.update_entry(scene_entry_id, content)
        else:
            self.lorebook.add_entries([{
                "id": scene_entry_id,
                "keys": [loc_name, loc_id],
                "content": content,
                "comment": f"loc:{loc_id}",
                "priority": 80,
                "position": "after_world",
                "entry_type": "scene_physical",
            }])
