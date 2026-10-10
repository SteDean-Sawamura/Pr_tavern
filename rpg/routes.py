"""FastAPI routes for the Tavern RPG engine."""

import os
import json
import glob
import sqlite3
from collections import OrderedDict
from typing import Dict, Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from db import database
from config import SCRIPTS_DIR

from .llm_utils import set_ai_profile, llm_call, get_llm_state, _llm_state
from .runtime import RPGClient, current_client, request_llm_state
from .agents import DirectorAgent
from .script_builder import ScriptBuilder
from .session import TavernRPGSession

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Helper: DB init
# ---------------------------------------------------------------------------

def _init_rpg_saves_table():
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rpg_saves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id TEXT NOT NULL,
            name TEXT NOT NULL,
            turn INTEGER DEFAULT 0,
            game_time TEXT,
            data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def load_ai_profiles():
    try:
        conn = sqlite3.connect(database.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, name, provider_type, api_key, model, max_tokens, base_url, is_active FROM ai_profiles"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Helper: location alias
# ---------------------------------------------------------------------------
def _fix_loc(loc_id, script):
    return script.get("location_aliases", {}).get(loc_id, loc_id) if loc_id else ""


# ---------------------------------------------------------------------------
# Helper: script scanning / loading
# ---------------------------------------------------------------------------

def scan_scripts():
    results = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS_DIR, "*.json"))):
        fn = os.path.basename(path)
        if fn.startswith("enrich") or fn.endswith("_current.json"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        sid = fn.replace(".json", "")
        results.append({
            "id": sid,
            "filename": fn,
            "name": d.get("script_name", d.get("name", sid)),
            "description": d.get("description", d.get("script_description", ""))[:200],
            "npc_count": len(d.get("npcs", [])),
            "preset_count": len(d.get("player_presets", [])),
            "loc_count": len(d.get("locations", [])),
        })
    return results


def load_script_data(script_id):
    path = os.path.join(SCRIPTS_DIR, script_id + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Session singleton
# ---------------------------------------------------------------------------
_state: Dict[str, Any] = {"session": None}
_clients: OrderedDict[str, RPGClient] = OrderedDict()
_MAX_CLIENTS = 50


def _trim_clients():
    for client_id, client in list(_clients.items()):
        if len(_clients) <= _MAX_CLIENTS:
            break
        if client.session is None and client.builder is None and client.active_requests == 0:
            del _clients[client_id]


def _s():
    client = current_client.get()
    return client.session if client is not None else _state["session"]


def _set_session(session):
    client = current_client.get()
    if client is not None:
        client.session = session
    else:
        _state["session"] = session


# ---------------------------------------------------------------------------
# Helper: build a TavernRPGSession from loaded script data
# ---------------------------------------------------------------------------

def _make_session(data, script_id):
    return TavernRPGSession(script_id=script_id, script_data=data)


# ===================================================================
# FastAPI application & routes
# ===================================================================

app = FastAPI(title="\u9152\u9986 RPG", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def bind_client(request: Request, call_next):
    client_id = request.headers.get("X-RPG-Session")
    if client_id is None:
        return await call_next(request)
    try:
        client_id = str(UUID(client_id))
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "无效的 RPG 会话标识"})
    client = _clients.get(client_id)
    if client is None:
        client = RPGClient()
        _clients[client_id] = client
    else:
        _clients.move_to_end(client_id)
    client.active_requests += 1
    _trim_clients()
    client_token = current_client.set(client)
    state_token = request_llm_state.set(client.llm_state.copy())
    try:
        return await call_next(request)
    finally:
        request_llm_state.reset(state_token)
        current_client.reset(client_token)
        client.active_requests -= 1
        _trim_clients()

static_dir = os.path.join(_BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    html_path = os.path.join(_BASE_DIR, "rpg_map_ui_v2.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>RPG \u7cfb\u7edf\u542f\u52a8\u4e2d...</h1>")


# ---------------------------------------------------------------------------
# Script builder API
# ---------------------------------------------------------------------------

_builder_state: Dict[str, Any] = {"builder": None}


def _get_builder():
    client = current_client.get()
    return client.builder if client is not None else _builder_state["builder"]


def _set_builder(builder):
    client = current_client.get()
    if client is not None:
        client.builder = builder
    else:
        _builder_state["builder"] = builder

@app.post("/api/create/start")
async def create_start(req: dict):
    theme = req.get("theme", "").strip()
    if not theme:
        raise HTTPException(400, "\u8bf7\u8f93\u5165\u5267\u672c\u4e3b\u65e8")
    if not get_llm_state().get("client"):
        profiles = load_ai_profiles()
        active_profile = next((p for p in profiles if p.get("is_active")), None)
        if not active_profile:
            raise HTTPException(400, "\u521b\u5efa\u5267\u672c\u9700\u8981\u914d\u7f6e AI \u6a21\u578b")
        set_ai_profile(active_profile)
    builder = ScriptBuilder()
    _set_builder(builder)
    try:
        return await builder.process_input(theme)
    except RuntimeError as e:
        _set_builder(None)
        raise HTTPException(502, str(e))


@app.post("/api/create/next")
async def create_next(req: dict):
    builder = _get_builder()
    if not builder:
        raise HTTPException(400, "\u6ca1\u6709\u6b63\u5728\u8fdb\u884c\u7684\u6784\u5efa")
    user_input = req.get("input", "").strip()
    if not user_input:
        raise HTTPException(400, "\u8bf7\u8f93\u5165\u5185\u5bb9")
    try:
        result = await builder.process_input(user_input)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if result.get("done"):
        _set_builder(None)
        result["auto_start"] = True
    return result


@app.get("/api/create/status")
async def create_status():
    builder = _get_builder()
    if not builder:
        return {"active": False}
    return {
        "active": True,
        "phase": builder.phase,
        "theme": builder.theme,
    }


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

@app.get("/api/scripts")
async def list_scripts():
    return scan_scripts()


@app.get("/api/scripts/{script_id}")
async def get_script_detail(script_id: str):
    data = load_script_data(script_id)
    if not data:
        raise HTTPException(404, "\u5267\u672c\u4e0d\u5b58\u5728")
    presets = []
    for p in data.get("player_presets", []):
        attrs = {}
        for k, v in (p.get("attributes") or {}).items():
            attrs[k] = v["value"] if isinstance(v, dict) else v
        presets.append({
            "id": p["id"],
            "name": p.get("name", p["id"]),
            "bio": p.get("bio", ""),
            "personality": p.get("personality", ""),
            "long_term_goal": p.get("long_term_goal", ""),
            "portrait_desc": p.get("portrait_desc", ""),
            "opening_text": p.get("opening_text", ""),
            "initial_location": p.get("initial_location", ""),
            "attributes": attrs,
        })
    return {
        "id": script_id,
        "name": data.get("script_name", data.get("name", script_id)),
        "description": data.get("description", data.get("script_description", "")),
        "npc_count": len(data.get("npcs", [])),
        "loc_count": len(data.get("locations", [])),
        "locations": [{"id": loc["id"], "name": loc.get("name", loc["id"])} for loc in data.get("locations", [])],
        "npcs": [{"id": n["id"], "name": n.get("name", n["id"]), "title": n.get("title", "")} for n in data.get("npcs", [])],
        "player_presets": presets,
    }


# ---------------------------------------------------------------------------
# Game lifecycle
# ---------------------------------------------------------------------------

@app.post("/api/start")
async def start_game(req: dict):
    script_id = req.get("script_id", "")
    preset_id = req.get("preset_id")
    ai_profile_id = req.get("ai_profile_id")

    data = load_script_data(script_id)
    if not data:
        raise HTTPException(404, "\u5267\u672c\u4e0d\u5b58\u5728")

    profile = None
    if ai_profile_id:
        profiles = load_ai_profiles()
        profile = next((p for p in profiles if p["id"] == ai_profile_id), None)
        if profile is None:
            raise HTTPException(404, "AI 配置不存在")

    s = _make_session(data, script_id)

    opening_text = ""
    if preset_id:
        preset = next((p for p in data.get("player_presets", []) if p.get("id") == preset_id), None)
        if preset:
            if preset.get("attributes"):
                for k, v in preset["attributes"].items():
                    val = v["value"] if isinstance(v, dict) else v
                    s.world_state.player_attrs[k] = val
            if preset.get("initial_location"):
                loc = _fix_loc(preset["initial_location"], data)
                s.world_state.current_location = loc
            if preset.get("name"):
                s.script_data["_player_name"] = preset["name"]
            opening_text = preset.get("opening_text", "")
    if not opening_text:
        opening_text = data.get("opening", {}).get("text", "")
    s.opening_text = opening_text

    set_ai_profile(profile)
    if profile is not None:
        print(f"[OK] AI profile: {profile['name']} ({profile['model']})")
    _set_session(s)
    print(f"[OK] \u6e38\u620f\u5f00\u59cb: {data.get('script_name', script_id)}, \u9884\u8bbe={preset_id or 'none'}")
    return {
        "started": True,
        "script_name": data.get("script_name", data.get("name", script_id)),
        "npc_count": len(s.npc_agents),
        "opening_text": opening_text,
        "ai_enabled": get_llm_state().get("client") is not None,
    }


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def get_state():
    if not _s():
        return {"started": False}
    session = _s()
    return {
        "started": True,
        "turn": session.world_state.current_turn,
        "opening_text": session.opening_text if session.world_state.current_turn == 0 else "",
        "location": session.world_state.current_location,
        "date": session.world_state.current_date.isoformat(),
        "player_attrs": session.world_state.player_attrs,
        "relationships": session.world_state.relationships,
        "npcs": [
            {
                "id": npc.get("id"),
                "name": npc.get("name"),
                "default_location": _fix_loc(npc.get("default_location", ""), session.script_data),
                "personality": npc.get("personality", ""),
                "title": npc.get("title", ""),
                "visual_profile": npc.get("visual_profile"),
            }
            for npc in session.script_data.get("npcs", [])
        ],
        "locations": [
            {"id": loc["id"], "name": loc.get("name"), "visual_profile": loc.get("visual_profile")}
            for loc in session.script_data.get("locations", [])
        ],
        "world_props": session.world_state.world_props,
        "variables": session.world_state.variables,
        "pending_result": session.get_pending_preview(),
    }


@app.get("/api/ai-profiles")
async def list_ai_profiles():
    profiles = load_ai_profiles()
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "provider_type": p["provider_type"],
            "model": p["model"],
            "is_active": bool(p.get("is_active")),
        }
        for p in profiles
    ]


# ---------------------------------------------------------------------------
# Turn processing
# ---------------------------------------------------------------------------

@app.post("/api/turn")
async def submit_turn(req: dict):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    s = _s()
    if s._pending_turn:
        raise HTTPException(409, "请先确认当前推演")
    raw_input = req.get("action", "")

    use_llm = get_llm_state().get("client") is not None
    if use_llm:
        plan = await DirectorAgent.plan(raw_input, s.world_state, s.script_data, s.world_state.turns[-5:])
    else:
        plan = DirectorAgent.plan_rules(raw_input, s.world_state)

    side_results = []
    game_action_intent = None

    for intent in plan.get("intents", []):
        itype = intent.get("type", "game_action")
        if itype == "game_action":
            game_action_intent = intent
        elif itype == "ui_command":
            side_results.append({"type": "ui_command", "command": intent.get("command", ""), "params": intent.get("params", {})})
        elif itype == "save_load":
            action = intent.get("action", "save")
            if action == "save":
                save_id = s.save_checkpoint()
                side_results.append({"type": "save_load", "action": "save", "id": save_id})
            else:
                side_results.append({"type": "save_load", "action": action})
        elif itype == "query":
            side_results.append({"type": "query", "answer": intent.get("answer", plan.get("reply_to_player", ""))})
        elif itype == "meta_feedback":
            side_results.append({"type": "meta_feedback", "noted": True, "reply": plan.get("reply_to_player")})

    if game_action_intent:
        turn_result = await s.process_turn_preview(raw_input, director_plan=game_action_intent)
        turn_result["director_plan"] = plan
        turn_result["side_results"] = side_results
        return turn_result

    return {
        "pending": False,
        "director_plan": plan,
        "side_results": side_results,
        "reply": plan.get("reply_to_player"),
    }


@app.post("/api/retry-npc/{npc_id}")
async def retry_npc_handler(npc_id: str, req: dict):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    user_hint = req.get("hint", "")
    return await _s().retry_npc(npc_id, user_hint)


@app.post("/api/confirm")
async def confirm_handler(req: dict = None):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    s = _s()
    if not s._pending_turn:
        raise HTTPException(409, "没有待确认的推演结果")
    req = req or {}
    choice_id = req.get("choice_id")
    choice = None
    if choice_id:
        pending_scene = s._pending_turn["scene"]
        choices = pending_scene.get("choices", [])
        choice = next((c for c in choices if c.get("id") == choice_id), None)
        if choice is None:
            raise HTTPException(400, "无效的选择")
    result = s.confirm_turn()
    if choice is not None:
        effects = choice.get("effects", {})
        rel_changes = effects.get("relationship_changes", {})
        changes = [{"var": f"relationship.{k}", "op": "add", "value": v} for k, v in rel_changes.items()]
        resource_cost = effects.get("resource_cost", {})
        for attr, delta in resource_cost.items():
            changes.append({"var": f"player.{attr}", "op": "add", "value": delta})
        if changes:
            s.world_state.apply_changes(changes)
        skill_check = effects.get("skill_check")
        if skill_check and isinstance(skill_check, dict):
            check_result = s._resolve_skill_check(
                skill_check.get("attr", ""),
                skill_check.get("difficulty", "medium"),
            )
            result["skill_check"] = check_result
            try:
                outcome = check_result.get("outcome", "")
                labels = {"critical_success": "\u5927\u6210\u529f", "success": "\u6210\u529f",
                          "failure": "\u5931\u8d25", "critical_failure": "\u5927\u5931\u8d25"}
                label = labels.get(outcome, outcome)
                attr_display = check_result.get("attribute", "")
                narr_prompt = (
                    f"\u6280\u80fd\u68c0\u5b9a\uff08{attr_display}\uff09\u2192 {label}\u3002{check_result.get('description', '')}\n"
                    f"\u73a9\u5bb6\u9009\u62e9\u4e86\uff1a{choice.get('text', '')}\n"
                    f"\u752850-100\u5b57\u7b2c\u4e8c\u4eba\u79f0\u63cf\u5199\u8fd9\u4e2a\u68c0\u5b9a\u7684\u7ed3\u679c\u573a\u666f\u3002"
                    f"\u4e0d\u8981\u63d0\u53ca\u5177\u4f53\u6570\u503c\uff0c\u7528\u89d2\u8272\u611f\u53d7\u548c\u884c\u4e3a\u4f53\u73b0\u7ed3\u679c\u3002"
                )
                narr = await llm_call(
                    "\u4f60\u662fRPG\u53d9\u4e8b\u5e08\uff0c\u6839\u636e\u6280\u80fd\u68c0\u5b9a\u7ed3\u679c\u751f\u6210\u7b80\u77ed\u573a\u666f\u63cf\u5199\u3002",
                    narr_prompt, max_tokens=200,
                )
                if narr:
                    result["skill_check_narrative"] = narr
            except Exception:
                pass
        delayed = effects.get("delayed_consequence")
        if delayed and isinstance(delayed, dict) and s.event_engine:
            s.event_engine.add_consequence(
                s.world_state.event_state,
                {
                    "description": delayed.get("description", ""),
                    "turns_delay": delayed.get("turns_delay", 2),
                    "trigger_chance": delayed.get("trigger_chance", 0.5),
                },
            )
            result["consequence_registered"] = delayed.get("description", "")
        result["choice_applied"] = choice_id
        turn = s.world_state.turns[-1]
        turn.state_after = s.world_state.get_snapshot()
        result["state_after"] = turn.state_after
    return result


# ---------------------------------------------------------------------------
# NPC chat / regeneration / swipe
# ---------------------------------------------------------------------------

@app.post("/api/talk")
async def talk_to_npc_handler(req: dict):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    npc_id = req.get("npc_id")
    message = req.get("message", "").strip()
    if not npc_id or not message:
        raise HTTPException(400, "\u9700\u8981 npc_id \u548c message")
    return await _s().talk_to_npc(npc_id, message)


@app.post("/api/regenerate")
async def regenerate_handler(req: dict = None):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    req = req or {}
    hint = req.get("hint", "")
    return await _s().regenerate_scene(hint)


@app.post("/api/swipe")
async def swipe_handler(req: dict):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    direction = req.get("direction", "right")
    result = _s().swipe_to(direction)
    if result is None:
        raise HTTPException(400, "\u6ca1\u6709\u5176\u4ed6\u7248\u672c\u53ef\u5207\u6362")
    return result


# ---------------------------------------------------------------------------
# Reset / save / load
# ---------------------------------------------------------------------------

@app.post("/api/reset")
async def reset_game():
    _set_session(None)
    set_ai_profile(None)
    return {"ok": True}


@app.post("/api/save")
async def save_game(req: dict = None):
    if not _s():
        raise HTTPException(400, "\u6e38\u620f\u672a\u5f00\u59cb")
    req = req or {}
    name = req.get("name")
    save_id = _s().save_checkpoint(name)
    return {"ok": True, "id": save_id}


@app.get("/api/saves")
async def list_saves():
    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, script_id, name, turn, game_time, created_at FROM rpg_saves ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "filename": f"save_{r['id']}.json", "script_id": r["script_id"], "turn": r["turn"], "timestamp": r["created_at"]} for r in rows]


@app.post("/api/load")
async def load_save(req: dict):
    save_id = req.get("id")
    if not save_id:
        raise HTTPException(400, "\u7f3a\u5c11 id")

    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM rpg_saves WHERE id = ?", (save_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "\u5b58\u6863\u4e0d\u5b58\u5728")

    checkpoint = json.loads(row["data"])
    script_id = row["script_id"]
    data = load_script_data(script_id)
    if not data:
        raise HTTPException(404, f"\u5267\u672c\u4e0d\u5b58\u5728: {script_id}")

    profile = None
    if req.get("ai_profile_id"):
        profile = next((item for item in load_ai_profiles() if item["id"] == req["ai_profile_id"]), None)
        if profile is None:
            raise HTTPException(404, "AI 配置不存在")

    s = _make_session(data, script_id)
    s.restore_checkpoint(checkpoint)

    set_ai_profile(profile)
    _set_session(s)

    turn_history = []
    for t in s.world_state.turns:
        turn_history.append({
            "turn_num": t.turn_num,
            "timestamp": t.timestamp,
            "player_action": t.player_action,
            "summary": t.outline_summary.get("summary", "") if isinstance(t.outline_summary, dict) else "",
            "scene_text": t.scene_output.get("scene_text", "") if isinstance(t.scene_output, dict) else "",
        })

    return {
        "ok": True,
        "script_name": data.get("script_name", script_id),
        "turn": s.world_state.current_turn,
        "turn_history": turn_history,
    }


@app.post("/api/delete-save")
async def delete_save(req: dict):
    save_id = req.get("id")
    if not save_id:
        raise HTTPException(400, "\u7f3a\u5c11 id")

    conn = sqlite3.connect(database.DB_PATH)
    try:
        conn.execute("DELETE FROM rpg_saves WHERE id = ?", (save_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# History / validation
# ---------------------------------------------------------------------------

@app.get("/api/history")
async def get_history():
    if not _s():
        return {"turns": []}
    turns = []
    for turn in _s().world_state.turns[-10:]:
        turns.append({
            "turn_num": turn.turn_num,
            "timestamp": turn.timestamp,
            "player_action": turn.player_action,
        })
    return {"turns": turns}


@app.get("/api/validation")
async def get_validation():
    s = _s()
    if not s:
        return {"status": "no_session"}
    v = getattr(s, '_last_validation', {})
    if v.get("pending"):
        return {"status": "pending"}
    return {"status": "done", "validation": v}


# ===================================================================
# Entrypoint
# ===================================================================

def main():
    _init_rpg_saves_table()
    print("\u542f\u52a8\u9152\u9986 RPG \u591aAgent\u63a8\u6f14\u7cfb\u7edf\u539f\u578b\n")
    print("\u542f\u52a8 FastAPI \u670d\u52a1...")
    print("\u8bbf\u95ee http://localhost:8080 \u6253\u5f00 RPG \u7cfb\u7edf")
    uvicorn.run(app, host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
