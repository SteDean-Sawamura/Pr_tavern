"""Class/profession and skill registry for D&D 5e and CoC systems."""

import json
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"

_PROFICIENCY_TABLE = {1: 2, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 3, 8: 3,
                      9: 4, 10: 4, 11: 4, 12: 4, 13: 5, 14: 5, 15: 5,
                      16: 5, 17: 6, 18: 6, 19: 6, 20: 6}

# DMG Spell Points variant (PHB-equivalent spell slot resources as a point pool)
_SPELL_POINTS = {
    1: 4, 2: 6, 3: 14, 4: 17, 5: 27, 6: 32, 7: 38, 8: 44, 9: 57, 10: 64,
    11: 73, 12: 73, 13: 83, 14: 83, 15: 94, 16: 94, 17: 107, 18: 114,
    19: 123, 20: 133,
}


def _get_attr_value(attributes: dict, key: str, default: int = 50) -> int:
    v = attributes.get(key)
    if isinstance(v, dict):
        return v.get("value", default)
    if isinstance(v, (int, float)):
        return int(v)
    return default


class ClassRegistry:
    def __init__(self, system: str | None = None):
        self.system = system
        self._skills: dict[str, dict] = {}
        self._classes: dict[str, dict] = {}
        self._classes_by_system: dict[str, list[dict]] = {}
        self._load_skills()
        if system:
            self._load_classes(system)
        else:
            for s in ("dnd5e", "coc"):
                self._load_classes(s)

    def _load_skills(self):
        path = _DATA_DIR / "skills.json"
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for skill in data.get("skills", []):
            self._skills[skill["id"]] = skill

    def _load_classes(self, system: str):
        filenames = {"dnd5e": "dnd5e_classes.json", "coc": "coc_classes.json"}
        path = _DATA_DIR / filenames.get(system, f"{system}_classes.json")
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        classes = data.get("classes", [])
        self._classes_by_system[system] = classes
        for cls in classes:
            self._classes[cls["id"]] = cls

    def get_class(self, class_id: str) -> dict | None:
        return self._classes.get(class_id)

    def get_skill(self, skill_id: str) -> dict | None:
        return self._skills.get(skill_id)

    def get_available_classes(self, system: str | None = None) -> list[dict]:
        if system:
            return self._classes_by_system.get(system, [])
        result = []
        for classes in self._classes_by_system.values():
            result.extend(classes)
        return result

    def get_skills_for_system(self, system: str) -> list[dict]:
        return [s for s in self._skills.values() if system in s.get("system", [])]

    @staticmethod
    def compute_proficiency_bonus(level: int, system: str = "dnd5e") -> int:
        if system == "dnd5e":
            return _PROFICIENCY_TABLE.get(min(max(level, 1), 20), 2)
        return 0

    def build_skill_check_map(
        self, system: str, skill_proficiencies: list[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Build a difficulty-tiered keyword→attribute map from skill definitions.

        Returns {"easy": {...}, "medium": {...}, "hard": {...}, "extreme": {...}}.
        """
        difficulty_map: dict[str, dict[str, str]] = {
            "easy": {}, "medium": {}, "hard": {}, "extreme": {},
        }
        danger_skills = {
            "fighting", "firearms", "dodge", "arcana", "occult",
            "cthulhu_mythos", "psychoanalysis",
        }
        hard_skills = {
            "stealth", "sleight_of_hand", "deception", "locksmith",
            "disguise", "intimidation", "survival", "track",
        }
        easy_skills = {
            "perception", "insight", "spot_hidden", "listen",
            "persuasion", "fast_talk", "performance", "ride",
        }

        for skill in self._skills.values():
            if system not in skill.get("system", []):
                continue
            sid = skill["id"]
            attr = skill.get("parent_attribute", "INT")
            keywords = skill.get("keywords", [])
            if sid in danger_skills:
                tier = "extreme"
            elif sid in hard_skills:
                tier = "hard"
            elif sid in easy_skills:
                tier = "easy"
            else:
                tier = "medium"
            for kw in keywords:
                difficulty_map[tier][kw] = attr

        return difficulty_map

    def build_flat_skill_check_map(
        self, system: str, skill_proficiencies: list[str] | None = None,
    ) -> dict[str, str]:
        """Build a flat keyword→attribute map (for scripts using flat skill_check_map)."""
        result: dict[str, str] = {}
        for skill in self._skills.values():
            if system not in skill.get("system", []):
                continue
            attr = skill.get("parent_attribute", "INT")
            for kw in skill.get("keywords", []):
                result[kw] = attr
        return result

    def find_skill_by_keyword(
        self, keyword: str, system: str,
    ) -> tuple[str | None, str | None]:
        """Find which skill a keyword belongs to. Returns (skill_id, parent_attribute)."""
        kw_lower = keyword.lower()
        for skill in self._skills.values():
            if system not in skill.get("system", []):
                continue
            for k in skill.get("keywords", []):
                if k in kw_lower or kw_lower in k:
                    return skill["id"], skill.get("parent_attribute")
        return None, None

    def compute_hp(self, class_def: dict, level: int, attributes: dict,
                   system: str | None = None) -> tuple[int, int]:
        """Compute (current_hp, max_hp) using official formulas.

        D&D 5e: Lv1 = hit_die + CON_mod; subsequent levels += avg_roll + CON_mod.
          CON_mod = (engine_CON - 50) // 5  (maps engine 0-100 scale to D&D modifier).
        CoC 7th: HP = (CON + SIZ) / 10.
        """
        system = system or self.system or "dnd5e"

        if system == "dnd5e":
            hit_die = class_def.get("hit_die", 8)
            con_val = _get_attr_value(attributes, "CON", 50)
            con_mod = (con_val - 50) // 5
            avg_roll = hit_die // 2 + 1
            hp = hit_die + con_mod + (level - 1) * (avg_roll + con_mod)
            return max(1, hp), max(1, hp)

        if system in ("coc", "brp"):
            con_val = _get_attr_value(attributes, "CON", 50)
            siz_val = _get_attr_value(attributes, "SIZ", 50)
            hp = (con_val + siz_val) // 10
            return max(1, hp), max(1, hp)

        return _get_attr_value(attributes, "HP", 10), 100

    def compute_mp(self, class_def: dict, level: int, attributes: dict,
                   system: str | None = None) -> tuple[int, int]:
        """Compute (current_mp, max_mp) using official formulas.

        D&D 5e: DMG Spell Points variant. Full casters use class level,
          half casters use ceil(class_level / 2). Non-casters get 0.
        CoC 7th: MP = POW / 5.
        """
        system = system or self.system or "dnd5e"

        if system == "dnd5e":
            sc = class_def.get("spellcasting")
            if not sc:
                return 0, 0
            if sc.get("type") == "full":
                caster_level = level
            else:
                caster_level = max(level // 2, 1) if level >= 2 else 0
            if caster_level <= 0:
                return 0, 0
            mp = _SPELL_POINTS.get(min(caster_level, 20), 4)
            return mp, mp

        if system in ("coc", "brp"):
            pow_val = _get_attr_value(attributes, "POW", 50)
            mp = pow_val // 5
            return max(0, mp), max(1, mp)

        return 0, 0

    def apply_class_to_character(
        self,
        class_def: dict,
        level: int,
        skill_choices: list[str],
        base_attrs: dict,
    ) -> dict:
        """Apply class definition to character, returning class-specific state fields.

        Returns dict with: class_id, class_name, level, skills, narrative_tags,
        abilities, unlocked_tree_nodes, proficiency_bonus.
        """
        system = class_def.get("_system", self.system or "dnd5e")
        prof_bonus = self.compute_proficiency_bonus(level, system)

        # Determine all proficient skills
        all_proficient = set(skill_choices)
        occupation_skills = class_def.get("occupation_skills", [])
        if occupation_skills:
            all_proficient.update(occupation_skills)

        # Build skills dict
        skills: dict[str, dict] = {}
        for sid in all_proficient:
            skill_def = self._skills.get(sid, {})
            base_val = skill_def.get("default_value", 0)
            skills[sid] = {
                "proficient": True,
                "value": base_val,
                "bonus": prof_bonus if system == "dnd5e" else 0,
                "name": skill_def.get("name", sid),
                "parent_attribute": skill_def.get("parent_attribute", ""),
            }

        # Collect narrative tags
        tags = list(class_def.get("narrative_tags", []))

        # Apply attribute bonuses
        attr_bonuses = dict(class_def.get("attribute_bonuses", {}))

        # Apply skill tree nodes up to level
        abilities = []
        unlocked_nodes = []
        tree = class_def.get("skill_tree", [])
        for node in tree:
            if node.get("level", 1) > level:
                continue
            reqs = node.get("requires", [])
            if reqs and not all(r in unlocked_nodes for r in reqs):
                continue
            unlocked_nodes.append(node["id"])
            grants = node.get("grants", {})
            tags.extend(grants.get("narrative_tags", []))
            abilities.extend(grants.get("abilities", []))
            for mod in grants.get("check_modifiers", []):
                sid = mod.get("skill", "")
                if sid in skills:
                    skills[sid]["bonus"] = skills[sid].get("bonus", 0) + mod.get("bonus", 0)
            for sb in grants.get("skill_bonuses", []):
                sid = sb.get("skill", "")
                if sid in skills:
                    skills[sid]["value"] = skills[sid].get("value", 0) + sb.get("bonus", 0)
                elif sid:
                    skill_def = self._skills.get(sid, {})
                    skills[sid] = {
                        "proficient": True,
                        "value": sb.get("bonus", 0),
                        "bonus": 0,
                        "name": skill_def.get("name", sid),
                        "parent_attribute": skill_def.get("parent_attribute", ""),
                    }
            node_attr_bonus = grants.get("attribute_bonuses", {})
            for k, v in node_attr_bonus.items():
                attr_bonuses[k] = attr_bonuses.get(k, 0) + v

        # Apply attribute bonuses to base_attrs (mutates in place for caller to use)
        for attr_key, bonus in attr_bonuses.items():
            if attr_key in base_attrs:
                attr_def = base_attrs[attr_key]
                if isinstance(attr_def, dict):
                    old = attr_def.get("value", 50)
                    hi = attr_def.get("max", 100)
                    attr_def["value"] = min(old + bonus, hi)
                elif isinstance(attr_def, (int, float)):
                    base_attrs[attr_key] = attr_def + bonus

        # Compute HP/MP from class formulas (after attribute bonuses applied)
        hp, hp_max = self.compute_hp(class_def, level, base_attrs, system)
        mp, mp_max = self.compute_mp(class_def, level, base_attrs, system)

        return {
            "class_id": class_def["id"],
            "class_name": class_def.get("name", class_def["id"]),
            "level": level,
            "skills": skills,
            "narrative_tags": tags,
            "abilities": abilities,
            "unlocked_tree_nodes": unlocked_nodes,
            "proficiency_bonus": prof_bonus,
            "computed_hp": hp,
            "computed_hp_max": hp_max,
            "computed_mp": mp,
            "computed_mp_max": mp_max,
        }

    def get_skill_value(
        self, skill_id: str, player_state: dict, system: str = "dnd5e",
    ) -> int | None:
        """Compute effective skill value for a check.

        D&D: attribute_modifier + proficiency_bonus (if proficient).
        CoC/BRP: the skill's percentage value directly.
        """
        skills = player_state.get("skills", {})
        attrs = player_state.get("attributes", {})
        skill_state = skills.get(skill_id)

        if system == "dnd5e":
            skill_def = self._skills.get(skill_id, {})
            parent_attr = skill_def.get("parent_attribute", "")
            attr_val = None
            if parent_attr:
                av = attrs.get(parent_attr)
                if isinstance(av, dict):
                    attr_val = av.get("value")
                elif isinstance(av, (int, float)):
                    attr_val = av
            if attr_val is None:
                attr_val = 50
            return attr_val

        if system in ("coc", "brp"):
            if skill_state:
                return skill_state.get("value", 50)
            skill_def = self._skills.get(skill_id, {})
            return skill_def.get("default_value", 1)

        return None

    def is_proficient(self, skill_id: str, player_state: dict) -> bool:
        skills = player_state.get("skills", {})
        skill_state = skills.get(skill_id)
        if skill_state:
            return skill_state.get("proficient", False)
        return False

    def get_proficiency_bonus_for_skill(
        self, skill_id: str, player_state: dict, system: str = "dnd5e",
    ) -> int:
        if not self.is_proficient(skill_id, player_state):
            return 0
        level = player_state.get("level", 1)
        return self.compute_proficiency_bonus(level, system)
