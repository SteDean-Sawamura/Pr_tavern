"""Material manager: CRUD, tags, AI summarization, script injection."""

import json
import aiosqlite
from db.database import get_db
from search.ai_knowledge import AIKnowledge


class MaterialManager:
    def __init__(self, ai_provider=None):
        self.ai_knowledge = AIKnowledge(ai_provider) if ai_provider else None

    async def save_material(self, data: dict) -> int:
        """Save a search result as a material. Returns material_id."""
        async with get_db() as db:
            cursor = await db.execute(
                """INSERT INTO materials (title, content, source_url, source_type, search_query)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    data.get("title", ""),
                    data.get("content", ""),
                    data.get("url"),
                    data.get("source_type", ""),
                    data.get("search_query", ""),
                ),
            )
            await db.commit()
            material_id = cursor.lastrowid

            # Auto-summarize and tag if AI available
            if self.ai_knowledge and data.get("content"):
                await self._auto_enrich(db, material_id, data["content"])

            return material_id

    async def _auto_enrich(self, db: aiosqlite.Connection, material_id: int, content: str):
        """Auto-generate summary and tags for a material."""
        try:
            summary = await self.ai_knowledge.summarize(content)
            await db.execute(
                "UPDATE materials SET summary = ? WHERE id = ?",
                (summary, material_id),
            )

            tags = await self.ai_knowledge.suggest_tags(content)
            for tag in tags:
                await db.execute(
                    "INSERT OR IGNORE INTO material_tags (material_id, tag) VALUES (?, ?)",
                    (material_id, tag),
                )
            await db.commit()
        except Exception:
            pass

    async def get_material(self, material_id: int) -> dict | None:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM materials WHERE id = ?", (material_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None

            material = dict(row)

            cursor = await db.execute(
                "SELECT tag FROM material_tags WHERE material_id = ?",
                (material_id,),
            )
            tags = [r["tag"] async for r in cursor]
            material["tags"] = tags
            return material

    async def list_materials(
        self, query: str | None = None, tags: list[str] | None = None
    ) -> list[dict]:
        async with get_db() as db:
            if tags:
                placeholders = ",".join("?" for _ in tags)
                cursor = await db.execute(
                    f"""SELECT DISTINCT m.* FROM materials m
                        JOIN material_tags mt ON m.id = mt.material_id
                        WHERE mt.tag IN ({placeholders})
                        ORDER BY m.created_at DESC""",
                    tags,
                )
            elif query:
                cursor = await db.execute(
                    """SELECT * FROM materials
                       WHERE title LIKE ? OR content LIKE ? OR summary LIKE ?
                       ORDER BY created_at DESC""",
                    (f"%{query}%", f"%{query}%", f"%{query}%"),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM materials ORDER BY created_at DESC"
                )

            rows = await cursor.fetchall()
            materials = [dict(row) for row in rows]
            if not materials:
                return materials
            # P5: 单批查询替代 N+1
            ids = [m["id"] for m in materials]
            placeholders = ",".join("?" for _ in ids)
            tag_cursor = await db.execute(
                f"SELECT material_id, tag FROM material_tags WHERE material_id IN ({placeholders})",
                ids,
            )
            tags_by_id: dict = {}
            for tr in await tag_cursor.fetchall():
                tags_by_id.setdefault(tr["material_id"], []).append(tr["tag"])
            for m in materials:
                m["tags"] = tags_by_id.get(m["id"], [])
            return materials

    async def add_tags(self, material_id: int, tags: list[str]):
        async with get_db() as db:
            for tag in tags:
                await db.execute(
                    "INSERT OR IGNORE INTO material_tags (material_id, tag) VALUES (?, ?)",
                    (material_id, tag),
                )
            await db.commit()

    async def remove_tag(self, material_id: int, tag: str):
        async with get_db() as db:
            await db.execute(
                "DELETE FROM material_tags WHERE material_id = ? AND tag = ?",
                (material_id, tag),
            )
            await db.commit()

    async def delete_material(self, material_id: int):
        async with get_db() as db:
            await db.execute("DELETE FROM materials WHERE id = ?", (material_id,))
            await db.commit()

    async def inject_to_script(
        self, material_id: int, script_id: str, target: str
    ) -> dict:
        """Transform material and inject into a script."""
        material = await self.get_material(material_id)
        if not material:
            raise ValueError("Material not found")

        # Load script from DB first (needed for AI context)
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT content FROM scripts WHERE id = ?", (script_id,)
            )
            row = await cursor.fetchone()
            if not row:
                raise ValueError("Script not found")

            script = json.loads(row["content"])

            # Use AI to transform content into script elements (returns list)
            transformed_list = [{"content": material["content"]}]
            if self.ai_knowledge:
                script_summary = ""
                try:
                    from api.script_routes import _build_full_script_summary
                    _target_tab_map = {
                        "npc": "characters", "location": "world", "event": "events",
                        "lorebook": "lorebook", "organization": "organizations",
                        "background": "world", "faction": "organizations",
                    }
                    script_summary = _build_full_script_summary(
                        script, focus_tab=_target_tab_map.get(target)
                    )
                except Exception:
                    pass
                transformed_list = await self.ai_knowledge.transform_to_script_element(
                    material["content"], target, script_summary=script_summary
                )

            for transformed in transformed_list:
                if target == "background":
                    new_text = transformed.get("content", "")
                    existing = script.get("world_background", "")
                    if new_text and new_text not in existing:
                        script["world_background"] = existing + "\n\n" + new_text
                elif target == "location":
                    locations = script.setdefault("locations", [])
                    tid = transformed.get("id")
                    if not tid or not any(l.get("id") == tid for l in locations):
                        locations.append(transformed)
                elif target == "npc":
                    npcs = script.setdefault("npcs", [])
                    tid = transformed.get("id")
                    if not tid or not any(n.get("id") == tid for n in npcs):
                        npcs.append(transformed)
                elif target == "event":
                    events = script.setdefault("one_time_events", [])
                    tid = transformed.get("id")
                    if not tid or not any(e.get("id") == tid for e in events):
                        events.append(transformed)
                elif target == "lorebook":
                    lorebook = script.setdefault("lorebook", [])
                    keys = transformed.get("keys", [])
                    content = transformed.get("content", "")
                    if content:
                        lorebook.append({
                            "keys": keys,
                            "content": content,
                            "comment": transformed.get("comment", ""),
                        })

            # Save updated script
            await db.execute(
                "UPDATE scripts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(script, ensure_ascii=False), script_id),
            )

            # Record the link
            await db.execute(
                """INSERT OR REPLACE INTO material_script_links
                   (material_id, script_id, inject_target, inject_data)
                   VALUES (?, ?, ?, ?)""",
                (material_id, script_id, target, json.dumps(transformed_list, ensure_ascii=False)),
            )
            await db.commit()

            return script

    async def get_all_tags(self) -> list[str]:
        """Get all unique tags."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT DISTINCT tag FROM material_tags ORDER BY tag"
            )
            return [row["tag"] async for row in cursor]
