"""Script/scenario loader and validator."""

import json
from pathlib import Path
from config import SCRIPTS_DIR


def _migrate_npc_orgs(script: dict):
    """旧格式迁移: NPC organization/rank 单值 → organizations 数组."""
    for npc in script.get("npcs", []):
        if "organizations" in npc:
            npc.pop("organization", None)
            npc.pop("rank", None)
            continue
        org_id = npc.pop("organization", "") or npc.pop("faction", "")
        rank = npc.pop("rank", None)
        if org_id:
            entry = {"org_id": org_id}
            if rank is not None:
                entry["rank"] = rank
            npc["organizations"] = [entry]
        else:
            npc["organizations"] = []


class ScriptLoader:
    @staticmethod
    def load(script_id: str) -> dict:
        path = SCRIPTS_DIR / f"{script_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Script not found: {script_id}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def load_from_db_content(content_str: str) -> dict:
        return json.loads(content_str)

    @staticmethod
    def validate(script: dict) -> list[dict]:
        """Validate script structure, return list of structured errors.

        Each error is a dict with keys: msg, target, field.
        """
        errors = []
        required_fields = ["script_id", "script_name", "start_time", "world_background"]
        for field in required_fields:
            if field not in script:
                errors.append({"msg": f"Missing required field: {field}", "target": "basic", "field": field})

        # Validate player character
        pc = script.get("player_character", {})
        if not pc:
            errors.append({"msg": "Missing player_character", "target": "player_character", "field": ""})
        else:
            for attr_name, attr in pc.get("attributes", {}).items():
                if isinstance(attr, dict):
                    val = attr.get("value", 0)
                    lo, hi = attr.get("min", 0), attr.get("max", 100)
                    if not (lo <= val <= hi):
                        errors.append({"msg": f"Attribute {attr_name} value {val} outside [{lo}, {hi}]", "target": "player_character", "field": f"attributes.{attr_name}"})

        # Validate random items dice configs
        for item in script.get("random_items", []):
            item_id = item.get('id', '?')
            dice = item.get("dice", {})
            if dice.get("count", 1) < 1:
                errors.append({"msg": f"Random item '{item_id}': dice count must be >= 1", "target": f"dice:{item_id}", "field": "dice.count"})
            if dice.get("faces", 6) < 2:
                errors.append({"msg": f"Random item '{item_id}': dice faces must be >= 2", "target": f"dice:{item_id}", "field": "dice.faces"})
            # Check for overlapping ranges
            ranges = item.get("ranges", [])
            for i, r1 in enumerate(ranges):
                if "min" not in r1 or "max" not in r1:
                    errors.append({"msg": f"Random item '{item_id}': range missing 'min' or 'max'", "target": f"dice:{item_id}", "field": "ranges"})
                    continue
                for r2 in ranges[i + 1:]:
                    if "min" not in r2 or "max" not in r2:
                        continue
                    if r1["min"] <= r2["max"] and r2["min"] <= r1["max"]:
                        errors.append({
                            "msg": f"Random item '{item_id}': overlapping ranges [{r1['min']}-{r1['max']}] and [{r2['min']}-{r2['max']}]",
                            "target": f"dice:{item_id}", "field": "ranges",
                        })

        # Validate location IDs are unique
        loc_ids = [loc["id"] for loc in script.get("locations", [])]
        if len(loc_ids) != len(set(loc_ids)):
            errors.append({"msg": "Duplicate location IDs found", "target": "locations", "field": "id"})

        # Validate NPC IDs are unique
        npc_ids = [npc["id"] for npc in script.get("npcs", [])]
        if len(npc_ids) != len(set(npc_ids)):
            errors.append({"msg": "Duplicate NPC IDs found", "target": "npcs", "field": "id"})

        return errors

    @staticmethod
    def validate_cross_refs(script: dict) -> list[dict]:
        """检查剧本内部 ID 引用一致性。返回结构化 warning 列表（非阻塞）。

        与 validate() 不同：交叉引用问题不应阻止启动游戏（兼容老剧本），
        而是作为 warning 反馈给剧本编辑器或日志。
        Each warning is a dict with keys: msg, target, field.
        """
        warnings = []
        loc_id_set = {loc["id"] for loc in script.get("locations", []) if loc.get("id")}
        npc_id_set = {npc["id"] for npc in script.get("npcs", []) if npc.get("id")}
        cyclic_ids = {e["id"] for e in script.get("cyclic_events", []) if e.get("id")}
        onetime_ids = {e["id"] for e in script.get("one_time_events", []) if e.get("id")}
        all_event_ids = cyclic_ids | onetime_ids

        pc = script.get("player_character", {})
        init_loc = pc.get("initial_location", "")
        if init_loc and loc_id_set and init_loc not in loc_id_set:
            warnings.append({"msg": f"player_character.initial_location '{init_loc}' 不在 locations 中", "target": "player_character", "field": "initial_location"})

        for preset in script.get("player_presets", []):
            iloc = preset.get("initial_location", "")
            pid = preset.get('id', '?')
            if iloc and loc_id_set and iloc not in loc_id_set:
                warnings.append({"msg": f"预设角色 '{pid}' initial_location '{iloc}' 不在 locations 中", "target": f"preset:{pid}", "field": "initial_location"})

        org_id_set = {o["id"] for o in script.get("organizations", []) if o.get("id")}
        org_hierarchy_ranks: dict[str, set] = {}
        for o in script.get("organizations", []):
            oid = o.get("id", "")
            hierarchy = o.get("hierarchy", [])
            if hierarchy:
                org_hierarchy_ranks[oid] = {h["rank"] for h in hierarchy if "rank" in h}
            parent = o.get("parent_org", "")
            if parent and org_id_set and parent not in org_id_set:
                warnings.append({"msg": f"组织 '{oid}' parent_org '{parent}' 不在 organizations 中", "target": f"org:{oid}", "field": "parent_org"})
            leader = o.get("leader", "")
            if leader and npc_id_set and leader not in npc_id_set:
                warnings.append({"msg": f"组织 '{oid}' leader '{leader}' 不在 NPC 列表中", "target": f"org:{oid}", "field": "leader"})

        for orel in script.get("org_relationships", []) or []:
            for k in ("a", "b"):
                ref = orel.get(k, "")
                if ref and org_id_set and ref not in org_id_set:
                    warnings.append({"msg": f"org_relationships.{k} 引用了不存在的组织 '{ref}'", "target": "org_relationships", "field": k})

        for npc in script.get("npcs", []):
            nid = npc.get("id", "?")
            dloc = npc.get("default_location", "") or npc.get("initial_location", "")
            if dloc and loc_id_set and dloc not in loc_id_set:
                warnings.append({"msg": f"NPC '{nid}' default_location '{dloc}' 不在 locations 中", "target": f"npc:{nid}", "field": "default_location"})
            for sched in npc.get("schedule", []) or []:
                sloc = sched.get("location", "")
                if sloc and loc_id_set and sloc not in loc_id_set:
                    warnings.append({"msg": f"NPC '{nid}' schedule.location '{sloc}' 不在 locations 中", "target": f"npc:{nid}", "field": "schedule"})
            for om in npc.get("organizations", []):
                oid = om.get("org_id", "")
                if oid and org_id_set and oid not in org_id_set:
                    warnings.append({"msg": f"NPC '{nid}' organizations 引用了不存在的组织 '{oid}'", "target": f"npc:{nid}", "field": "organizations"})
                om_rank = om.get("rank")
                if om_rank is not None and oid:
                    valid_ranks = org_hierarchy_ranks.get(oid)
                    if valid_ranks is not None and om_rank not in valid_ranks:
                        warnings.append({"msg": f"NPC '{nid}' rank {om_rank} 不在组织 '{oid}' 的 hierarchy 中", "target": f"npc:{nid}", "field": "organizations"})

        for rel in script.get("npc_relationships", []) or []:
            if "from" in rel:
                # 新格式 (from/to)
                for k in ("from", "to"):
                    rid = rel.get(k, "")
                    if rid and npc_id_set and rid not in npc_id_set:
                        warnings.append({"msg": f"npc_relationship.{k} 引用了不存在的 NPC '{rid}'", "target": "npc_relationships", "field": k})
            else:
                # 旧格式 (a/b)
                for k in ("a", "b"):
                    rid = rel.get(k, "")
                    if rid and npc_id_set and rid not in npc_id_set:
                        warnings.append({"msg": f"npc_relationship.{k} 引用了不存在的 NPC '{rid}'", "target": "npc_relationships", "field": k})

        for ri in script.get("random_items", []) or []:
            linked = ri.get("linked_event_id", "")
            ri_id = ri.get('id', '?')
            if linked and all_event_ids and linked not in all_event_ids:
                warnings.append({"msg": f"random_item '{ri_id}' linked_event_id '{linked}' 不存在", "target": f"dice:{ri_id}", "field": "linked_event_id"})

        for ch in script.get("opening", {}).get("choices", []) or []:
            ne = (ch.get("result") or {}).get("next_event", "")
            ch_id = ch.get('id', '?')
            if ne and all_event_ids and ne not in all_event_ids:
                warnings.append({"msg": f"opening.choice '{ch_id}' next_event '{ne}' 不存在", "target": f"opening:{ch_id}", "field": "next_event"})

        # Validate org goals IDs are unique within each org
        for o in script.get("organizations", []):
            oid = o.get("id", "?")
            goal_ids = [g.get("id", "") for g in o.get("goals", []) if g.get("id")]
            if len(goal_ids) != len(set(goal_ids)):
                warnings.append({"msg": f"组织 '{oid}' goals 中有重复ID", "target": f"org:{oid}", "field": "goals"})

        # Validate story_tree node related_npcs / related_orgs references
        st_trees = script.get("story_tree", {}).get("trees", [])
        for tree in st_trees:
            for node in tree.get("nodes", []):
                nid = node.get("id", "?")
                for ref_npc in node.get("related_npcs", []):
                    if ref_npc and npc_id_set and ref_npc not in npc_id_set:
                        warnings.append({"msg": f"剧情树节点 '{nid}' related_npcs 引用了不存在的NPC '{ref_npc}'", "target": f"story_tree:{nid}", "field": "related_npcs"})
                for ref_org in node.get("related_orgs", []):
                    if ref_org and org_id_set and ref_org not in org_id_set:
                        warnings.append({"msg": f"剧情树节点 '{nid}' related_orgs 引用了不存在的组织 '{ref_org}'", "target": f"story_tree:{nid}", "field": "related_orgs"})

        return warnings

    @staticmethod
    def create_initial_state(script: dict) -> dict:
        """Generate the turn-0 state snapshot from script initial values."""
        pc = script.get("player_character", {})

        # Build attributes
        attributes = {}
        for attr_name, attr in pc.get("attributes", {}).items():
            if isinstance(attr, dict):
                attributes[attr_name] = attr["value"]
            else:
                attributes[attr_name] = attr

        # Build relationships (support three-dimensional format)
        relationships = {}
        for rel_name, rel in pc.get("relationships", {}).items():
            if isinstance(rel, dict):
                # Check if it's already 3D format
                if any(k in rel for k in ("trust", "affection", "fear")):
                    relationships[rel_name] = {
                        "trust": rel.get("trust", 50),
                        "affection": rel.get("affection", 50),
                        "fear": rel.get("fear", 0),
                    }
                else:
                    val = rel.get("value", 50)
                    # Offset trust and affection slightly so they start distinct
                    relationships[rel_name] = {
                        "trust": max(0, min(100, val + 5)),
                        "affection": max(0, min(100, val - 5)),
                        "fear": 0,
                    }
            else:
                # Simple numeric value — upgrade to 3D with slight offset
                relationships[rel_name] = {
                    "trust": max(0, min(100, rel + 5)),
                    "affection": max(0, min(100, rel - 5)),
                    "fear": 0,
                }

        # NPC attitudes
        _migrate_npc_orgs(script)
        npcs = {}
        for npc in script.get("npcs", []):
            npcs[npc["id"]] = {
                "attitude_toward_player": npc.get("attitude_toward_player", 50),
                "name": npc.get("name", npc["id"]),
                "known": npc.get("known", True),
                "met": npc.get("met", False),
                "default_location": npc.get("default_location", npc.get("initial_location", "")),
                "bio": npc.get("bio", ""),
                "personality": npc.get("personality", ""),
                "capabilities": npc.get("capabilities", ""),
                "title": npc.get("title", ""),
                "organizations": npc.get("organizations", []),
                "superior": npc.get("superior", ""),
                "current_room": npc.get("default_room", ""),
            }

        # Visible locations
        visible_locations = [
            loc["id"] for loc in script.get("locations", [])
            if loc.get("initially_visible", True)
        ]

        # Active persistent states — only those marked initially_active
        active_persistent_states = [
            ps["id"] for ps in script.get("persistent_states", [])
            if ps.get("initially_active", False)
        ]

        # C5: apply preset persistent_state_overrides
        ps_overrides = script.get("_preset_ps_overrides")
        if ps_overrides:
            for sid, active in ps_overrides.items():
                if active and sid not in active_persistent_states:
                    active_persistent_states.append(sid)
                elif not active and sid in active_persistent_states:
                    active_persistent_states.remove(sid)

        # Cyclic event trackers
        cyclic_trackers = {}
        start_time = script.get("start_time", "")
        for event in script.get("cyclic_events", []):
            # 如果剧本未指定 first_trigger，回填为 start_time，避免事件永不触发
            first = event.get("first_trigger") or start_time
            cyclic_trackers[event["id"]] = {
                "last_fired": None,
                "next_fire": first,
            }

        # World properties
        world_properties = {}
        for prop in script.get("world_properties", []):
            world_properties[prop["id"]] = prop.get("value", "")

        # NPC-NPC relationships — build dual networks (global + known)
        npc_relationships_global = {}
        npc_relationships_known = {}
        for rel in script.get("npc_relationships", []):
            if "from" in rel:
                # 新格式：有向三维关系
                key = f"{rel['from']}_{rel['to']}"
                entry = {
                    "from": rel["from"],
                    "to": rel["to"],
                    "trust": rel.get("trust", 50),
                    "affection": rel.get("affection", 50),
                    "fear": rel.get("fear", 0),
                    "description": rel.get("description", ""),
                    "initially_known": rel.get("initially_known", True),
                    "initially_met": rel.get("initially_met", True),
                }
            else:
                # 旧格式：无向 type 关系 — key 按字典序排列防重复
                a, b = rel.get('a', ''), rel.get('b', '')
                key = f"{min(a, b)}_{max(a, b)}"
                entry = {
                    "a": rel.get("a", ""),
                    "b": rel.get("b", ""),
                    "type": rel.get("type", "中立"),
                    "description": rel.get("description", ""),
                    "intensity": rel.get("intensity", 50),
                }
            npc_relationships_global[key] = entry
            if rel.get("initially_known", True):
                known_entry = dict(entry)
                if rel.get("public_description"):
                    known_entry["description"] = rel["public_description"]
                    if rel.get("public_type"):
                        known_entry["type"] = rel["public_type"]
                npc_relationships_known[key] = known_entry
        # C4: filter known relationships by preset known_npcs
        preset_known_npcs = script.get("_preset_known_npcs")
        if preset_known_npcs:
            known_set = set(preset_known_npcs)
            npc_relationships_known = {
                k: v for k, v in npc_relationships_known.items()
                if (
                    # 新格式（有向）: from 和 to 都在已知列表中
                    (v.get("from", "") in known_set and v.get("to", "") in known_set)
                    or
                    # 旧格式（无向）: a 和 b 都在已知列表中
                    (v.get("a", "") in known_set and v.get("b", "") in known_set)
                )
            }

        # Build display name mappings for frontend
        # Common English → Chinese attribute name translations
        _attr_translations = {
            "health": "健康", "mood": "心情", "study_progress": "学习进度",
            "strength": "力量", "intelligence": "智力", "charisma": "魅力",
            "agility": "敏捷", "stamina": "体力", "luck": "幸运",
            "money": "金钱", "pocket_money": "零花钱", "energy": "精力",
            "hunger": "饱食度", "reputation": "声望", "morality": "道德",
        }
        display_names = {}
        for attr_name, attr_def in pc.get("attributes", {}).items():
            # Check if the attribute dict has an explicit display_name/name
            if isinstance(attr_def, dict) and attr_def.get("display_name"):
                display_names[attr_name] = attr_def["display_name"]
            elif isinstance(attr_def, dict) and attr_def.get("name"):
                display_names[attr_name] = attr_def["name"]
            else:
                display_names[attr_name] = _attr_translations.get(attr_name, attr_name)
        for npc in script.get("npcs", []):
            display_names[npc["id"]] = npc.get("name", npc["id"])
        # Location ID → display name + descriptions
        location_descriptions = {}
        for loc in script.get("locations", []):
            display_names[loc["id"]] = loc.get("name", loc["id"])
            if loc.get("description"):
                location_descriptions[loc["id"]] = loc["description"]

        # Persistent state ID → display name + descriptions
        persistent_state_descriptions = {}
        for ps in script.get("persistent_states", []):
            ps_id = ps.get("id", "")
            if not ps_id:
                continue
            ps_name = ps.get("name") or ps.get("display_name") or ps_id.replace("_", " ")
            display_names[ps_id] = ps_name
            if ps.get("description"):
                persistent_state_descriptions[ps_id] = ps["description"]

        # World property ID → display name
        for prop in script.get("world_properties", []):
            wp_id = prop.get("id", "")
            if wp_id:
                display_names[wp_id] = prop.get("name") or prop.get("display_name") or wp_id

        # Organization ID → display name
        for org in script.get("organizations", []):
            org_id = org.get("id", "")
            if org_id:
                display_names[org_id] = org.get("name") or org_id

        # Script variable ID → display name
        for var_def in script.get("variables", []):
            vid = var_def.get("id", "")
            if vid:
                display_names[vid] = var_def.get("name") or var_def.get("display_name") or vid

        # Event ID → display name (description)
        for event in script.get("cyclic_events", []):
            if event.get("id") and event.get("description"):
                display_names[event["id"]] = event["description"]
        for event in script.get("one_time_events", []):
            if event.get("id") and event.get("description"):
                display_names[event["id"]] = event["description"]

        # G12: Location connections
        location_connections = {}
        for loc in script.get("locations", []):
            conns = loc.get("connections")
            if conns and isinstance(conns, list):
                location_connections[loc["id"]] = conns

        # Initial inventory
        initial_inventory = []
        for item in pc.get("initial_inventory", []):
            if isinstance(item, dict):
                entry = {
                    "item": item.get("item", item.get("name", "")),
                    "quantity": item.get("quantity", 1),
                }
                if item.get("description"):
                    entry["description"] = item["description"]
                if item.get("use_effect"):
                    entry["use_effect"] = item["use_effect"]
                initial_inventory.append(entry)
            elif isinstance(item, str):
                initial_inventory.append({"item": item, "quantity": 1})

        # Class/profession system integration
        class_fields = {}
        _settings = script.get("settings", {})
        _class_system = _settings.get("class_system")
        _class_id = pc.get("class_id")
        if _class_system and _class_id:
            from engine.class_system import ClassRegistry
            _registry = ClassRegistry(_class_system)
            _class_def = _registry.get_class(_class_id)
            if _class_def:
                _level = pc.get("level", 1)
                _skill_choices = pc.get("skill_proficiencies", [])
                _base_attrs = pc.get("attributes", {})
                class_fields = _registry.apply_class_to_character(
                    _class_def, _level, _skill_choices, _base_attrs,
                )
                # Re-extract attribute values after class bonuses applied
                for attr_name, attr in _base_attrs.items():
                    if isinstance(attr, dict):
                        attributes[attr_name] = attr["value"]
                    else:
                        attributes[attr_name] = attr

                # Override HP/MP with computed values from class formulas
                _hp = class_fields.get("computed_hp")
                _hp_max = class_fields.get("computed_hp_max")
                if _hp is not None and "HP" in _base_attrs:
                    attributes["HP"] = _hp
                    hp_attr = _base_attrs.get("HP")
                    if isinstance(hp_attr, dict):
                        hp_attr["value"] = _hp
                        hp_attr["max"] = _hp_max
                _mp = class_fields.get("computed_mp")
                _mp_max = class_fields.get("computed_mp_max")
                if _mp is not None and "MP" in _base_attrs:
                    if _mp > 0:
                        attributes["MP"] = _mp
                        mp_attr = _base_attrs.get("MP")
                        if isinstance(mp_attr, dict):
                            mp_attr["value"] = _mp
                            mp_attr["max"] = _mp_max
                    else:
                        attributes["MP"] = 0

        return {
            "game_time": script.get("start_time", ""),
            "player": {
                "id": pc.get("id", "player"),
                "name": pc.get("name", ""),
                "bio": pc.get("bio", ""),
                "personality": pc.get("personality", ""),
                "portrait_desc": pc.get("portrait_desc", ""),
                "location": pc.get("initial_location", ""),
                "current_room": pc.get("default_room", ""),
                "long_term_goal": pc.get("long_term_goal", ""),
                "attributes": attributes,
                "relationships": relationships,
                **({
                    "class_id": class_fields["class_id"],
                    "class_name": class_fields["class_name"],
                    "level": class_fields["level"],
                    "skills": class_fields["skills"],
                    "narrative_tags": class_fields["narrative_tags"],
                    "abilities": class_fields["abilities"],
                    "unlocked_tree_nodes": class_fields["unlocked_tree_nodes"],
                    "proficiency_bonus": class_fields["proficiency_bonus"],
                } if class_fields else {}),
            },
            "inventory": initial_inventory,
            "npcs": npcs,
            "npc_last_seen": {},
            "npc_relationships_global": npc_relationships_global,
            "npc_relationships_known": npc_relationships_known,
            "npc_offscreen_log": {},
            "world_properties": world_properties,
            "visible_locations": visible_locations,
            "active_persistent_states": active_persistent_states,
            "fired_one_time_events": [],
            "cyclic_event_trackers": cyclic_trackers,
            "random_item_state": {},
            "play_style_summary": "",
            "play_style_last_turn": 0,
            "dice_check_enabled": script.get("settings", {}).get(
                "dice_check", {}
            ).get("default_enabled", True),
            "current_weather": None,
            "display_names": display_names,
            "location_descriptions": location_descriptions,
            "persistent_state_descriptions": persistent_state_descriptions,
            "location_connections": location_connections,
            "faction_reputation": {
                org["id"]: {
                    "value": org["initial_reputation"],
                    "title": (
                        "崇拜" if org["initial_reputation"] >= 90 else
                        "友好" if org["initial_reputation"] >= 70 else
                        "中立" if org["initial_reputation"] >= 50 else
                        "冷淡" if org["initial_reputation"] >= 30 else
                        "敌对" if org["initial_reputation"] >= 10 else "通缉"
                    ),
                }
                for org in script.get("organizations", [])
                if org.get("initial_reputation") is not None
            },
            "pc_discovered_lore": [],
            "pc_lore_clues": {},
            "pc_event_reports": [],
        }

    @staticmethod
    def list_scripts() -> list[dict]:
        """List all script files in the scripts directory."""
        scripts = []
        for path in SCRIPTS_DIR.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                scripts.append({
                    "id": data.get("script_id", path.stem),
                    "name": data.get("script_name", path.stem),
                    "version": data.get("version", "1.0"),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return scripts
