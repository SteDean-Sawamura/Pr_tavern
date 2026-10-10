/* api.js -- thin wrappers around the RPG map UI backend endpoints */

const BASE = window.location.pathname.startsWith("/rpg") ? "/rpg" : "";
const RPG_SESSION_KEY = "tavern_rpg_session";
let rpgSessionPromise;

function _rpgSession() {
  if (!rpgSessionPromise) {
    rpgSessionPromise = new Promise(resolve => {
      const saved = sessionStorage.getItem(RPG_SESSION_KEY);
      let session = saved || crypto.randomUUID();
      if (!saved) sessionStorage.setItem(RPG_SESSION_KEY, session);
      if (typeof BroadcastChannel !== "function") {
        resolve(session);
        return;
      }
      const channel = new BroadcastChannel("tavern-rpg-session");
      const probe = crypto.randomUUID();
      let claimed = !saved;
      let timer;
      channel.onmessage = event => {
        const message = event.data;
        if (message.session !== session) return;
        if (message.type === "probe" && claimed) {
          channel.postMessage({ type: "occupied", session, probe: message.probe });
        } else if (message.type === "occupied" && message.probe === probe && !claimed) {
          clearTimeout(timer);
          session = crypto.randomUUID();
          sessionStorage.setItem(RPG_SESSION_KEY, session);
          claimed = true;
          resolve(session);
        }
      };
      if (saved) {
        channel.postMessage({ type: "probe", session, probe });
        timer = setTimeout(() => {
          claimed = true;
          resolve(session);
        }, 100);
      } else {
        resolve(session);
      }
    });
  }
  return rpgSessionPromise;
}

async function _headers(extra = {}) {
  const headers = { ...extra, "X-RPG-Session": await _rpgSession() };
  const token = localStorage.getItem("tavern_api_token");
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function _json(res) {
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "HTTP " + res.status);
  }
  return res.json();
}

async function _post(url, body) {
  return fetch(BASE + url, {
    method: "POST",
    headers: await _headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  }).then(_json);
}

async function _get(url) {
  return fetch(BASE + url, { headers: await _headers() }).then(_json);
}

// ----- state -----
export function getState() { return _get("/api/state"); }

// ----- scripts -----
export function getScripts() { return _get("/api/scripts"); }
export function getScript(id) { return _get("/api/scripts/" + id); }

// ----- AI profiles -----
export function getAIProfiles() { return _get("/api/ai-profiles"); }

// ----- saves -----
export function getSaves() { return _get("/api/saves"); }
export function loadSave(id, aiProfileId) { return _post("/api/load", { id: id, ai_profile_id: aiProfileId }); }
export function deleteSave(id) { return _post("/api/delete-save", { id: id }); }
export function saveGame() { return _post("/api/save", {}); }

// ----- game lifecycle -----
export function startGame(payload) { return _post("/api/start", payload); }
export function resetGame() { return _post("/api/reset", {}); }

// ----- turn / confirm -----
export function submitTurn(action) { return _post("/api/turn", { action: action }); }
export function confirm(body) { return _post("/api/confirm", body || {}); }

// ----- NPC talk -----
export function talkToNpc(npcId, message) { return _post("/api/talk", { npc_id: npcId, message: message }); }

// ----- retry NPC -----
export function retryNpc(npcId, hint) { return _post("/api/retry-npc/" + npcId, { hint: hint }); }

// ----- scene regenerate / swipe -----
export function regenerate() { return _post("/api/regenerate", {}); }
export function swipe(direction) { return _post("/api/swipe", { direction: direction }); }

// ----- create mode -----
export function createStart(theme) { return _post("/api/create/start", { theme: theme }); }
export function createNext(input) { return _post("/api/create/next", { input: input }); }
