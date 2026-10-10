"""GameSession Mixin: 商店与物品交互"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # 避免循环导入


class ShopMixin:
    """商店、物品使用、可交互物件相关方法"""

    def _init_shop_inventories(self):
        """Initialize shop inventories from script location definitions."""
        if "shop_inventories" in self.current_state:
            return
        inventories: dict = {}
        for loc in self.script.get("locations", []):
            for shop in loc.get("shops", []):
                shop_id = shop.get("id", "")
                if not shop_id:
                    continue
                inventories[shop_id] = [
                    {"item_id": it.get("item_id", ""), "name": it.get("name", it.get("item_id", "")),
                     "stock": it.get("stock", -1), "base_price": it.get("base_price", 10),
                     "description": it.get("description", "")}
                    for it in shop.get("items", []) if it.get("item_id")
                ]
        if inventories:
            self.current_state["shop_inventories"] = inventories

    def _get_currency_attribute(self) -> str:
        attrs = self.script.get("player_character", {}).get("attributes", {})
        for key in attrs:
            if isinstance(attrs[key], dict) and attrs[key].get("is_currency"):
                return f"player.attributes.{key}"
            if any(k in key.lower() for k in ("gold", "coin", "money", "金币", "金钱", "银两")):
                return f"player.attributes.{key}"
        return "player.attributes.gold"

    def _get_currency_value(self) -> int:
        val = self.state_manager._get_value(self.current_state, self._get_currency_attribute())
        if isinstance(val, dict):
            val = val.get("value", 0)
        return int(val) if isinstance(val, (int, float)) else 0

    def _calculate_shop_price(self, shop_id: str, base_price: int, shop_def: dict | None = None) -> int:
        if shop_def is None:
            for loc in self.script.get("locations", []):
                for shop in loc.get("shops", []):
                    if shop.get("id") == shop_id:
                        shop_def = shop
                        break
                if shop_def:
                    break
        modifier = 1.0
        if shop_def:
            merchant_id = shop_def.get("merchant_npc", "")
            if merchant_id:
                npc_def = self._npc_by_id.get(merchant_id, {})
                faction = npc_def.get("faction", "")
                if faction:
                    rep = self.current_state.get("faction_reputation", {}).get(faction, {})
                    rep_val = rep.get("value", 50) if isinstance(rep, dict) else 50
                    modifier *= 1.0 - (rep_val - 50) / 200.0
                npc_state = self.current_state.get("npcs", {}).get(merchant_id, {})
                if isinstance(npc_state, dict):
                    attitude = npc_state.get("attitude_toward_player", 50)
                    if attitude < 30:
                        modifier *= 1.3
                    elif attitude > 75:
                        modifier *= 0.9
                depth = self.current_state.get("npc_relationship_depths", {}).get(merchant_id, {})
                if isinstance(depth, dict):
                    level = depth.get("level", 0)
                    if level >= 4:
                        modifier *= 0.8
                    elif level >= 3:
                        modifier *= 0.9
        return max(1, int(base_price * modifier))

    def buy_item(self, shop_id: str, item_id: str) -> dict:
        """Buy an item from a shop."""
        shops = self.current_state.get("shop_inventories", {})
        shop_items = shops.get(shop_id, [])
        item = next((it for it in shop_items if it["item_id"] == item_id), None)
        if not item:
            return {"success": False, "message": "商品不存在"}
        if item["stock"] == 0:
            return {"success": False, "message": "已售罄"}
        price = self._calculate_shop_price(shop_id, item["base_price"])
        current_gold = self._get_currency_value()
        if current_gold < price:
            return {"success": False, "message": f"金币不足（需要{price}，当前{current_gold}）"}
        attr_path = self._get_currency_attribute()
        changes = [{"target": attr_path, "op": "add", "value": -price, "reason": f"购买{item['name']}"}]
        self.current_state, log = self.state_manager.apply_changes(self.current_state, changes, inplace=True)
        inventory = self.current_state.setdefault("inventory", [])
        existing = next((it for it in inventory if it.get("item") == item["name"]), None)
        if existing:
            existing["quantity"] = existing.get("quantity", 1) + 1
        else:
            new_entry = {"item": item["name"], "quantity": 1}
            if item.get("description"):
                new_entry["description"] = item["description"]
            inventory.append(new_entry)
        if item["stock"] > 0:
            item["stock"] -= 1
        return {"success": True, "message": f"购买了{item['name']}（花费{price}金币）",
                "state_changes": log, "state": self.current_state}

    def sell_item(self, shop_id: str, item_name: str) -> dict:
        """Sell an item to a shop."""
        inventory = self.current_state.get("inventory", [])
        inv_item = next((it for it in inventory if it.get("item") == item_name), None)
        if not inv_item or inv_item.get("quantity", 1) <= 0:
            return {"success": False, "message": "你没有这个物品"}
        shop_items = self.current_state.get("shop_inventories", {}).get(shop_id, [])
        sell_price = None
        for it in shop_items:
            if it.get("name") == item_name:
                sell_price = max(1, it["base_price"] // 2)
                break
        if sell_price is None:
            inv_desc = (inv_item.get("description") or "").lower()
            if any(k in inv_desc for k in ("稀有", "珍贵", "legendary", "rare", "epic")):
                sell_price = 50
            elif any(k in inv_desc for k in ("精良", "uncommon", "优质")):
                sell_price = 20
            else:
                sell_price = 5
        attr_path = self._get_currency_attribute()
        changes = [{"target": attr_path, "op": "add", "value": sell_price, "reason": f"出售{item_name}"}]
        self.current_state, log = self.state_manager.apply_changes(self.current_state, changes, inplace=True)
        inv_item["quantity"] = inv_item.get("quantity", 1) - 1
        if inv_item["quantity"] <= 0:
            inventory.remove(inv_item)
        return {"success": True, "message": f"出售了{item_name}（获得{sell_price}金币）",
                "state_changes": log, "state": self.current_state}

    def get_location_shops(self, location_id: str) -> dict:
        """Get shops at a location."""
        loc_def = self._location_by_id.get(location_id, {})
        shops = loc_def.get("shops", [])
        inventories = self.current_state.get("shop_inventories", {})
        result = []
        for shop in shops:
            sid = shop.get("id", "")
            items = inventories.get(sid, [])
            priced_items = []
            for it in items:
                priced_items.append({
                    **it,
                    "price": self._calculate_shop_price(sid, it.get("base_price", 10), shop_def=shop),
                })
            result.append({"id": sid, "name": shop.get("name", sid), "items": priced_items})
        return {"shops": result, "currency": self._get_currency_value(),
                "currency_name": self._get_currency_attribute().split(".")[-1]}

    def use_item(self, item_name: str) -> dict:
        """Use an item from inventory. If it has use_effect, apply deterministically; otherwise return None to signal freeform fallback."""
        inventory = self.current_state.get("inventory", [])
        entry = None
        for it in inventory:
            if it.get("item") == item_name:
                entry = it
                break
        if not entry:
            return {"success": False, "message": f"背包中没有「{item_name}」"}
        if entry.get("quantity", 1) <= 0:
            return {"success": False, "message": f"「{item_name}」已用完"}

        effect = entry.get("use_effect")
        if not effect:
            return {"has_effect": False}

        if effect.get("condition"):
            if not self._evaluate_condition(effect["condition"]):
                return {"success": False, "message": effect.get("fail_message", f"现在无法使用「{item_name}」")}

        state_changes = []
        for sc in effect.get("state_changes", []):
            target = sc.get("target", "")
            value = sc.get("value", sc.get("change", 0))
            if not target:
                continue
            applied, log = self.state_manager.apply_changes(
                self.current_state,
                [{"target": target, "value": value, "op": sc.get("op", "add"), "reason": sc.get("reason", "")}],
                inplace=True,
            )
            self.current_state = applied
            state_changes.extend(log)

        for loc_id in effect.get("reveals", []):
            vis = self.current_state.setdefault("visible_locations", [])
            if loc_id not in vis:
                vis.append(loc_id)

        for sid in effect.get("activate_states", []):
            active = self.current_state.setdefault("active_persistent_states", [])
            if sid not in active:
                active.append(sid)

        if effect.get("consumable", True):
            entry["quantity"] = entry.get("quantity", 1) - 1
            if entry["quantity"] <= 0:
                inventory.remove(entry)

        msg = effect.get("success_message", f"你使用了「{item_name}」")
        self._record_narrative_callback(msg, ["item_use"], "low")
        return {
            "success": True, "has_effect": True,
            "message": msg,
            "state_changes": state_changes,
            "state": self.current_state,
        }

    def interact_with_object(self, interactable_id: str) -> dict:
        """Deterministic interaction with a location interactable. Returns has_rules=False to signal freeform fallback."""
        player_loc = self.current_state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(player_loc, {})
        raw = loc_def.get("interactables", [])
        item = next((i for i in raw if i.get("id") == interactable_id), None)
        if not item:
            return {"success": False, "message": "这里没有这个可互动的东西"}
        used = self.current_state.get("used_interactables", [])
        if item.get("one_time") and interactable_id in used:
            return {"success": False, "message": f"「{item.get('name', interactable_id)}」已经使用过了"}
        if item.get("condition") and not self._evaluate_condition(item["condition"]):
            return {"success": False, "message": "这里没有这个可互动的东西"}
        has_rules = bool(item.get("required_item") or item.get("state_changes") or item.get("reveals") or item.get("required_skill"))
        if not has_rules:
            return {"has_rules": False, "action_hint": item.get("action_hint", item.get("name", interactable_id))}
        req_skill = item.get("required_skill", {})
        if req_skill:
            skill_name = req_skill.get("skill", "")
            req_level = req_skill.get("level", 1)
            cur_level = self.current_state.get("skill_growth", {}).get(skill_name, {}).get("level", 0)
            if cur_level < req_level:
                return {"success": False, "message": f"需要「{skill_name}」达到{req_level}级才能操作（当前{cur_level}级）"}
        req_item = item.get("required_item")
        if req_item:
            inv_names = [it.get("item", "") for it in self.current_state.get("inventory", []) if isinstance(it, dict)]
            if req_item not in inv_names:
                return {"success": False, "message": f"需要「{req_item}」才能进行此操作"}
        state_changes = []
        for sc in item.get("state_changes", []):
            applied, log = self.state_manager.apply_changes(self.current_state, [sc], inplace=True)
            self.current_state = applied
            state_changes.extend(log)
        revealed = []
        for loc_id in item.get("reveals", []):
            vis = self.current_state.setdefault("visible_locations", [])
            if loc_id not in vis:
                vis.append(loc_id)
                dn = self.current_state.get("display_names", {})
                revealed.append(dn.get(loc_id, loc_id))
        if item.get("one_time"):
            self.current_state.setdefault("used_interactables", []).append(interactable_id)
        msg = item.get("success_message", f"你与「{item.get('name', interactable_id)}」互动了")
        self._record_narrative_callback(msg, ["interact"], "low")
        self._collect_interactables()
        return {"success": True, "has_rules": True, "message": msg, "state_changes": state_changes, "revealed_locations": revealed, "state": self.current_state}
