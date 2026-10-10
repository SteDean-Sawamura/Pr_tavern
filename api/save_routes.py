"""Save management API routes."""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from db.database import get_db

router = APIRouter()
logger = logging.getLogger("tavern.saves")

BUNDLE_FORMAT = "tavern-save-bundle"
BUNDLE_VERSION = 1


@router.get("")
async def list_saves():
    """List all saves with metadata."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT s.*, sc.name as script_name
               FROM saves s LEFT JOIN scripts sc ON s.script_id = sc.id
               ORDER BY s.updated_at DESC"""
        )
        rows = await cursor.fetchall()
        saves = [dict(row) for row in rows]
        logger.info("存档列表 — 共%d个", len(saves))
        return saves


@router.get("/{save_id}")
async def get_save(save_id: str):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM saves WHERE id = ?", (save_id,))
        row = await cursor.fetchone()
        if not row:
            logger.warning("存档未找到 — save_id=%s", save_id)
            raise HTTPException(status_code=404, detail="Save not found")
        logger.info("获取存档 — save_id=%s, script=%s", save_id, row["script_id"])
        return dict(row)


@router.delete("/{save_id}")
async def delete_save(save_id: str):
    from api.game_routes import _get_session_lock, _sessions
    async with _get_session_lock(save_id):
        async with get_db() as db:
            await db.execute("DELETE FROM tree_nodes WHERE save_id = ?", (save_id,))
            await db.execute("DELETE FROM saves WHERE id = ?", (save_id,))
            await db.commit()

        evicted = _sessions.pop(save_id, None)
        if evicted:
            tasks = set(evicted._background_tasks)
            tasks.update(getattr(evicted, "_background_persist_tasks", ()))
            eviction_task = getattr(evicted, "_eviction_task", None)
            if eviction_task:
                tasks.add(eviction_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("删除存档 — save_id=%s", save_id)
    return {"status": "ok"}


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "save").strip()
    return (name[:60] or "save")


@router.get("/{save_id}/export")
async def export_save(save_id: str):
    """Export save (with script + all tree nodes) as a downloadable JSON bundle."""
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM saves WHERE id = ?", (save_id,))
        save_row = await cur.fetchone()
        if not save_row:
            raise HTTPException(status_code=404, detail="Save not found")
        save = dict(save_row)

        cur = await db.execute(
            "SELECT * FROM tree_nodes WHERE save_id = ? ORDER BY created_at",
            (save_id,),
        )
        nodes = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT content FROM save_scripts WHERE save_id = ?", (save_id,)
        )
        effective_script_row = await cur.fetchone()

        script = None
        if save.get("script_id"):
            cur = await db.execute(
                "SELECT * FROM scripts WHERE id = ?", (save["script_id"],)
            )
            sr = await cur.fetchone()
            if sr:
                script = dict(sr)

    effective_script = json.loads(effective_script_row["content"]) if effective_script_row else None
    if effective_script is not None and script:
        from api.game_routes import _fill_snapshot_metadata
        _fill_snapshot_metadata(effective_script, json.loads(script["content"]))

    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "save": save,
        "nodes": nodes,
        "script": script,
        "effective_script": effective_script,
    }
    body = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")

    fname = f"save_{_sanitize_filename(save.get('name') or '')}_{save_id[:8]}.tavernsave.json"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"
    }
    logger.info("导出存档 — save_id=%s, nodes=%d, script=%s", save_id, len(nodes), bool(script))
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("/import")
async def import_save(file: UploadFile = File(...)):
    """Import a save bundle (with bundled script). Generates new save_id and node IDs."""
    try:
        raw = await file.read()
        bundle = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise HTTPException(status_code=400, detail="Not a tavern save bundle")

    save = bundle.get("save") or {}
    nodes = bundle.get("nodes") or []
    script = bundle.get("script")
    effective_script = bundle.get("effective_script")

    if not isinstance(save, dict) or not isinstance(nodes, list):
        raise HTTPException(status_code=400, detail="Malformed bundle")
    if effective_script is not None and (
        not isinstance(effective_script, dict)
        or effective_script.get("script_id") != save.get("script_id")
    ):
        raise HTTPException(status_code=400, detail="Invalid effective script")

    recursion = save.get("lorebook_max_recursion")
    if recursion is not None and (type(recursion) is not int or not 1 <= recursion <= 8):
        raise HTTPException(status_code=400, detail="Invalid lorebook recursion depth")

    new_save_id = uuid.uuid4().hex
    id_map: dict[str, str] = {}
    for n in nodes:
        old = n.get("id")
        if old:
            id_map[old] = uuid.uuid4().hex

    new_active = None
    old_active = save.get("active_node_id")
    if old_active and old_active in id_map:
        new_active = id_map[old_active]

    async with get_db() as db:
        try:
            # Script 处理：若 bundle 有 script 且 id 不存在则插入；已存在则复用
            script_id = save.get("script_id")
            if script and isinstance(script, dict) and script.get("id"):
                cur = await db.execute(
                    "SELECT 1 FROM scripts WHERE id = ?", (script["id"],)
                )
                exists = await cur.fetchone()
                if not exists:
                    await db.execute(
                        """INSERT INTO scripts (id, name, version, content, created_at, updated_at)
                           VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))""",
                        (
                            script["id"],
                            script.get("name") or "导入的剧本",
                            script.get("version") or "1.0",
                            script.get("content") or "",
                            script.get("created_at"),
                            script.get("updated_at"),
                        ),
                    )
                script_id = script["id"]

            await db.execute(
                """INSERT INTO saves (id, script_id, name, active_node_id, total_nodes,
                                       play_time_seconds, summary, authors_note,
                                       authors_note_position, authors_note_depth,
                                       negative_prompt, logit_bias,
                                       lorebook_max_recursion,
                                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (
                    new_save_id,
                    script_id,
                    (save.get("name") or "导入的存档") + " (导入)",
                    new_active,
                    save.get("total_nodes") or len(nodes),
                    save.get("play_time_seconds") or 0,
                    save.get("summary") or "",
                    save.get("authors_note") or "",
                    save.get("authors_note_position") or "end",
                    save.get("authors_note_depth") or 4,
                    save.get("negative_prompt") or "",
                    save.get("logit_bias") or "[]",
                    recursion,
                ),
            )
            if effective_script is not None:
                await db.execute(
                    "INSERT INTO save_scripts (save_id, content) VALUES (?, ?)",
                    (new_save_id, json.dumps(effective_script, ensure_ascii=False)),
                )

            for n in nodes:
                old_id = n.get("id")
                if not old_id:
                    continue
                new_id = id_map[old_id]
                old_parent = n.get("parent_id")
                new_parent = id_map.get(old_parent) if old_parent else None
                await db.execute(
                    """INSERT INTO tree_nodes (id, save_id, parent_id, game_time, turn_number,
                                                player_action, ai_response, choices_presented,
                                                dice_rolls, state_changes, triggered_events,
                                                state_snapshot, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))""",
                    (
                        new_id,
                        new_save_id,
                        new_parent,
                        n.get("game_time"),
                        n.get("turn_number"),
                        n.get("player_action"),
                        n.get("ai_response"),
                        n.get("choices_presented"),
                        n.get("dice_rolls"),
                        n.get("state_changes"),
                        n.get("triggered_events"),
                        n.get("state_snapshot"),
                        n.get("created_at"),
                    ),
                )

            await db.commit()
        except HTTPException:
            await db.rollback()
            raise
        except Exception as e:
            await db.rollback()
            logger.exception("导入存档失败")
            raise HTTPException(status_code=500, detail=f"Import failed: {e}")

    logger.info(
        "导入存档 — new_save_id=%s, nodes=%d, script_inserted=%s",
        new_save_id, len(nodes), bool(script),
    )
    return {"status": "ok", "save_id": new_save_id, "nodes_imported": len(nodes)}
