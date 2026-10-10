import { esc, LOC_NAMES, npcColor } from '../utils.js';
import * as state from '../state.js';
import * as api from '../api.js';

// ===== Render all panels =====

function renderAll() {
  renderPlayerSummary();
  renderLocationList();
  renderNpcList();
  renderSocialTab();
  renderWorldTab();
  renderHistoryTab();
  renderActionSuggestions();
}

// ===== Action Suggestions =====

function renderActionSuggestions() {
  const el = document.getElementById('action-suggestions');
  const gameState = state.get('gameState');
  if (!el || !gameState) return;
  el.innerHTML = '';

  const locId = gameState.location || '';
  const npcs = (gameState.npcs || []);
  const locNpcs = npcs.filter(n => n.default_location === locId);
  const otherLocs = (gameState.locations || []).filter(l => l.id !== locId);

  const suggestions = [];

  // Current location NPC-related actions
  locNpcs.forEach(npc => {
    const name = npc.name || npc.id;
    suggestions.push(`和${name}交谈`);
  });

  // Movement to other locations (pick up to 2)
  otherLocs.slice(0, 2).forEach(loc => {
    suggestions.push(`前往${loc.name || loc.id}`);
  });

  // Generic actions
  suggestions.push('观察周围环境', '休息');

  suggestions.forEach(text => {
    const chip = document.createElement('span');
    chip.style.cssText = 'background:var(--bg-hover);padding:3px 10px;border-radius:12px;cursor:pointer;transition:all .15s;border:1px solid var(--border);white-space:nowrap;';
    chip.textContent = text;
    chip.addEventListener('mouseenter', () => { chip.style.background = 'var(--accent-dim)'; chip.style.borderColor = 'var(--accent)'; chip.style.color = 'var(--accent)'; });
    chip.addEventListener('mouseleave', () => { chip.style.background = 'var(--bg-hover)'; chip.style.borderColor = 'var(--border)'; chip.style.color = ''; });
    chip.addEventListener('click', () => {
      document.getElementById('action-input').value = text;
      document.getElementById('action-input').focus();
    });
    el.appendChild(chip);
  });
}

// ===== Player Summary (left panel) =====

function renderPlayerSummary() {
  const el = document.getElementById('player-mini-stats');
  const gameState = state.get('gameState');
  if (!gameState) { el.innerHTML = ''; return; }

  const attrs = gameState.player_attrs || {};
  const keyAttrs = ['竞技状态', '速度', '射门', '传球', '体能', '疲劳值'];
  const colorMap = { '竞技状态': 'var(--accent)', '速度': 'var(--info)', '射门': 'var(--danger)', '传球': 'var(--success)', '体能': 'var(--mood)', '疲劳值': 'var(--health)' };

  let html = '';
  keyAttrs.forEach(attr => {
    const val = attrs[attr];
    if (val === undefined) return;
    html += `<div class="mini-stat">
      <span class="mini-stat-label">${esc(attr)}</span>
      <div class="mini-stat-bar"><div class="mini-stat-fill" style="width:${Math.min(100,val)}%;background:${colorMap[attr]||'var(--info)'}"></div></div>
      <span class="mini-stat-val">${val}</span>
    </div>`;
  });

  if (!html) {
    // Fallback: show all attrs
    Object.entries(attrs).slice(0, 6).forEach(([k, v]) => {
      if (typeof v !== 'number') return;
      html += `<div class="mini-stat">
        <span class="mini-stat-label">${esc(k)}</span>
        <div class="mini-stat-bar"><div class="mini-stat-fill" style="width:${Math.min(100,v)}%;background:var(--info)"></div></div>
        <span class="mini-stat-val">${v}</span>
      </div>`;
    });
  }

  el.innerHTML = html || '<div style="font-size:10px;color:var(--text-muted)">暂无数据</div>';
}

// ===== Location List =====

function renderLocationList() {
  const list = document.getElementById('location-list');
  const gameState = state.get('gameState');
  list.innerHTML = '';
  (gameState?.locations || []).forEach(loc => {
    const npcsHere = (gameState?.npcs || []).filter(n => n.default_location === loc.id).length;
    const isCurrent = loc.id === gameState?.location;
    const div = document.createElement('div');
    div.className = 'list-item' + (isCurrent ? ' current' : '');
    div.innerHTML = `
      <div class="list-dot" style="background:${isCurrent ? 'var(--gold)' : 'var(--text-muted)'}"></div>
      <div class="list-item-content">
        <div class="list-item-label">${esc(loc.name)}</div>
      </div>
      ${npcsHere ? `<span class="list-npc-count">${npcsHere}</span>` : ''}
    `;
    div.addEventListener('click', () => selectLocation(loc.id));
    list.appendChild(div);
  });
}

// ===== NPC List =====

function renderNpcList() {
  const list = document.getElementById('npc-list');
  const gameState = state.get('gameState');
  list.innerHTML = '';
  (gameState?.npcs || []).forEach(npc => {
    const rel = gameState?.relationships?.[npc.id];
    const div = document.createElement('div');
    div.className = 'list-item';
    div.innerHTML = `
      <div class="list-dot" style="background:${npcColor(npc.id)}"></div>
      <div class="list-item-content">
        <div class="list-item-label">${esc(npc.name)}</div>
        <div class="list-item-meta">${esc(npc.title || '')}${rel !== undefined ? ` | ${rel}` : ''}</div>
      </div>
    `;
    div.addEventListener('click', () => selectNpc(npc.id));
    list.appendChild(div);
  });
}

// ===== Social Tab =====

function renderSocialTab() {
  const el = document.getElementById('social-content');
  const gameState = state.get('gameState');
  if (!gameState) { el.innerHTML = ''; return; }

  const rels = gameState.relationships || {};
  const npcs = gameState.npcs || [];
  let html = '';

  npcs.forEach(npc => {
    const val = rels[npc.id];
    if (val === undefined) return;
    const barColor = val >= 70 ? 'bar-success' : val >= 40 ? 'bar-relation' : 'bar-danger';
    html += `<div class="rel-3d" onclick="selectNpc('${esc(npc.id)}')" style="cursor:pointer">
      <div class="rel-3d-name">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${npcColor(npc.id)}"></span>
        ${esc(npc.name)}
        <span class="rel-3d-title">${esc(npc.title || '')}</span>
      </div>
      <div class="rel-3d-bars">
        <div class="rel-3d-bar">
          <span class="rel-dim-label">好感</span>
          <div class="bar-outer" style="flex:1;height:4px;background:var(--bg-inset);border-radius:2px;overflow:hidden">
            <div class="bar-inner ${barColor}" style="width:${Math.min(100,val)}%"></div>
          </div>
          <span class="rel-dim-val">${val}</span>
        </div>
      </div>
    </div>`;
  });

  el.innerHTML = html || '<div style="font-size:11px;color:var(--text-muted)">暂无关系数据</div>';
}

// ===== World Tab =====

function renderWorldTab() {
  const propsEl = document.getElementById('world-props-content');
  const varsEl = document.getElementById('world-vars-content');
  const gameState = state.get('gameState');
  if (!gameState) { propsEl.innerHTML = ''; varsEl.innerHTML = ''; return; }

  // World properties
  const wp = gameState.world_props || {};
  let html = '';
  Object.entries(wp).forEach(([k, v]) => {
    html += `<div class="world-prop-item">
      <span class="world-prop-label">${esc(k)}</span>
      <span class="world-prop-value">${esc(String(v))}</span>
    </div>`;
  });
  propsEl.innerHTML = html || '<div style="font-size:11px;color:var(--text-muted)">--</div>';

  // Variables
  const vars = gameState.variables || {};
  html = '';
  Object.entries(vars).forEach(([k, v]) => {
    html += `<div class="world-prop-item">
      <span class="world-prop-label">${esc(k)}</span>
      <span class="world-prop-value">${esc(String(v))}</span>
    </div>`;
  });
  varsEl.innerHTML = html || '<div style="font-size:11px;color:var(--text-muted)">--</div>';
}

// ===== History Tab =====

function renderHistoryTab() {
  const el = document.getElementById('history-content');
  const turnHistory = state.get('turnHistory');
  if (!turnHistory.length) {
    el.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:20px 0;text-align:center">暂无推演记录</div>';
    return;
  }

  let html = '';
  turnHistory.slice().reverse().forEach((t, i) => {
    const isLatest = i === 0;
    html += `<div class="turn-block ${isLatest ? 'latest' : ''}" onclick="this.classList.toggle('expanded')">
      <div class="turn-header">T${t.turn_num} | ${t.timestamp ? t.timestamp.split('T')[0] : ''}</div>
      <div class="turn-action">${esc(t.player_action)}</div>
      ${t.scene_text ? `<div class="turn-narrative">${esc(t.scene_text)}</div>` : ''}
      ${t.summary ? `<div class="turn-summary">${esc(t.summary)}</div>` : ''}
    </div>`;
  });
  el.innerHTML = html;
}

// ===== Selection =====

function selectLocation(locId) {
  const gameState = state.get('gameState');
  const pendingResult = state.get('pendingResult');
  state.set('selectedEntity', { type: 'location', id: locId });
  switchTab('tab-detail');

  const loc = (gameState?.locations || []).find(l => l.id === locId);
  const npcsHere = (gameState?.npcs || []).filter(n => n.default_location === locId);
  const isCurrent = locId === gameState?.location;

  let html = '';

  // Location card
  html += `<div class="card">
    <div class="card-header">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${isCurrent ? 'var(--gold)' : 'var(--text-muted)'}"></span>
      ${esc(loc?.name || locId)}
      ${isCurrent ? '<span style="font-size:10px;color:var(--success);font-weight:400">当前位置</span>' : ''}
    </div>
  </div>`;

  // NPCs here
  if (npcsHere.length) {
    html += '<div class="card"><div class="card-label">此处角色</div>';
    npcsHere.forEach(npc => {
      const rel = gameState?.relationships?.[npc.id];
      html += `<div style="padding:4px 0;cursor:pointer;border-bottom:1px solid var(--border-light)" onclick="selectNpc('${esc(npc.id)}')">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${npcColor(npc.id)}"></span>
          <span style="font-weight:500;color:var(--text-bright);font-size:12px">${esc(npc.name)}</span>
          <span style="font-size:10px;color:var(--text-muted)">${esc(npc.title || '')}</span>
        </div>
        ${rel !== undefined ? `<div style="display:flex;align-items:center;gap:4px;margin-top:3px;margin-left:14px">
          <div style="flex:1;height:3px;background:var(--bg-inset);border-radius:2px;overflow:hidden">
            <div style="height:100%;width:${Math.min(100,rel)}%;background:var(--relation);border-radius:2px"></div>
          </div>
          <span style="font-size:10px;color:var(--text-muted);width:20px;text-align:right">${rel}</span>
        </div>` : ''}
      </div>`;
    });
    html += '</div>';
  }

  // Show detail via game-turn's showDetail (on window)
  window.showDetail(html);
}

// ===== Tab Switching =====

function switchTab(tabId) {
  document.querySelectorAll('.status-tab').forEach((t, i) => {
    const pages = ['tab-detail', 'tab-social', 'tab-world', 'tab-history'];
    const isActive = pages[i] === tabId;
    t.classList.toggle('active', isActive);
  });
  document.querySelectorAll('.tab-page').forEach(p => {
    p.classList.toggle('active', p.id === tabId);
  });
}

// ===== Header =====

function updateHeader() {
  const gameState = state.get('gameState');
  if (!gameState) return;
  document.getElementById('turn-info').textContent = gameState.turn || '0';
  document.getElementById('location-info').textContent = LOC_NAMES[gameState.location] || gameState.location || '--';
  const d = gameState.date;
  document.getElementById('date-info').textContent = d ? d.split('T')[0] : '--';
  const wp = gameState.world_props || {};
  document.getElementById('season-info').textContent = wp.current_season || '--';
  document.getElementById('status-turn').textContent = `T${gameState.turn || 0}`;
}

// ===== Init / Load =====

async function init() {
  await loadState();
  renderAll();
}

async function loadState() {
  try {
    const gameState = await api.getState();
    state.set('gameState', gameState);
    (gameState.locations || []).forEach(l => { LOC_NAMES[l.id] = l.name; });
    updateHeader();
  } catch (e) {
    console.error('Failed to load state:', e);
    window.updateStatus('连接失败 - 请确保后端服务在运行');
  }
}

function enterGameScreen(stateData) {
  document.getElementById('start-screen').classList.add('hidden');
  document.body.classList.add('in-game');
  const gs = document.getElementById('game-screen');
  gs.classList.add('active');
  const pending = stateData.pending_result || null;
  state.set('pendingResult', pending);
  state.set('lastChoice', null);
  state.set('gameState', stateData);
  (stateData.locations || []).forEach(l => { LOC_NAMES[l.id] = l.name; });
  updateHeader();
  renderAll();
  document.getElementById('btn-confirm').style.display = pending ? '' : 'none';
  document.getElementById('btn-save').style.display = '';
  document.getElementById('btn-submit').disabled = Boolean(pending);
  const input = document.getElementById('action-input');
  input.disabled = Boolean(pending);
  input.value = pending?.player_action || '';
  if (pending) {
    window.renderInferenceBubbles(pending.npc_outputs || {});
    window.showTurnResults(pending);
  } else {
    window.renderInferenceBubbles({});
    window.clearDetail();
    if (stateData.turn === 0 && stateData.opening_text) {
      switchTab('tab-detail');
      window.showDetail(`<div class="scene-text">${esc(stateData.opening_text)}</div>`);
    }
  }
}

// ===== Window bindings & init =====

export function initPanels() {
  // Register renderAll as gameState change listener
  state.on('gameState', renderAll);

  // Functions referenced by HTML onclick
  window.renderAll = renderAll;
  window.selectLocation = selectLocation;
  window.selectNpc = selectNpc;
  window.switchTab = switchTab;
  window.updateHeader = updateHeader;
  window.enterGameScreen = enterGameScreen;
  window.loadState = loadState;
  window.init = init;
}

// selectNpc is referenced by onclick in renderSocialTab and selectLocation HTML
function selectNpc(npcId) {
  const gameState = state.get('gameState');
  state.set('selectedEntity', { type: 'npc', id: npcId });
  switchTab('tab-detail');

  const npc = (gameState?.npcs || []).find(n => n.id === npcId);
  const rel = gameState?.relationships?.[npcId];

  let html = '';

  // NPC header card
  html += `<div class="card">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <div style="width:32px;height:32px;border-radius:50%;background:${npcColor(npcId)};display:flex;align-items:center;justify-content:center;color:white;font-weight:600;font-size:14px">${(npc?.name || '?')[0]}</div>
      <div>
        <div style="font-size:14px;font-weight:600;color:var(--text-bright)">${esc(npc?.name)}</div>
        <div style="font-size:10px;color:var(--text-muted)">${esc(npc?.title || '')}</div>
      </div>
    </div>
    ${npc?.personality ? `<div style="font-size:11px;color:var(--text-secondary);line-height:1.5;margin-top:4px">${esc(npc.personality)}</div>` : ''}
  </div>`;

  // Relationship bar
  if (rel !== undefined) {
    const barColor = rel >= 70 ? 'bar-success' : rel >= 40 ? 'bar-relation' : 'bar-danger';
    html += `<div class="card"><div class="card-section">
      <div class="card-label">好感度</div>
      <div class="stat-bar" style="margin-bottom:0">
        <div class="stat-label"><span>关系值</span><span>${rel}/100</span></div>
        <div class="bar-outer"><div class="bar-inner ${barColor}" style="width:${Math.min(100,rel)}%"></div></div>
      </div>
    </div></div>`;
  }

  // Location
  if (npc?.default_location) {
    html += `<div class="card"><div class="card-section">
      <div class="card-label">所在位置</div>
      <div style="font-size:12px;cursor:pointer;color:var(--info)" onclick="selectLocation('${esc(npc.default_location)}')">${esc(LOC_NAMES[npc.default_location] || npc.default_location)}</div>
    </div></div>`;
  }

  // NPC Talk UI
  html += `<div class="card"><div class="card-section">
    <div class="card-label">对话</div>
    <div id="npc-chat-log" style="max-height:200px;overflow-y:auto;margin-bottom:6px;font-size:11px;line-height:1.6"></div>
    <div style="display:flex;gap:4px">
      <input id="npc-chat-input" type="text" placeholder="和${esc(npc?.name || '?')}说些什么..."
        style="flex:1;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-lighter);color:var(--text-bright);font-size:11px"
        onkeydown="if(event.key==='Enter')sendNpcChat('${esc(npcId)}')" />
      <button class="btn btn-accept" style="padding:4px 10px;font-size:11px" onclick="sendNpcChat('${esc(npcId)}')">发送</button>
    </div>
  </div></div>`;

  // Show detail via game-turn's showDetail (on window)
  window.showDetail(html);
}
