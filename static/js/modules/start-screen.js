/* start-screen.js -- start / script / save / boot logic */

import * as state from '../state.js';
import * as api   from '../api.js';
import { esc }    from '../utils.js';

// External hooks -- set via initStartScreen()
let _enterGameScreen = null;

// ===== Boot =====

async function boot() {
  try {
    const data = await api.getState();
    if (data.started) {
      _enterGameScreen?.(data);
      return;
    }
  } catch (e) { /* not started yet */ }
  await loadScriptList();
  await loadAIProfiles();
  await loadSaveList();
}

// ===== AI Profiles =====

async function loadAIProfiles() {
  try {
    const profiles = await api.getAIProfiles();
    const select = document.getElementById('ai-profile-select');
    profiles.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name} (${p.model})`;
      if (p.is_active) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener('change', () => {
      const hint = document.getElementById('ai-profile-hint');
      if (select.value) {
        const p = profiles.find(x => x.id == select.value);
        hint.textContent = `AI模式：NPC通过 ${p?.model || 'AI'} 进行智能推理`;
      } else {
        hint.textContent = '规则模式：NPC使用预设规则响应，速度快但不智能';
      }
    });
    if (select.value) select.dispatchEvent(new Event('change'));
  } catch (e) {
    console.warn('Failed to load AI profiles:', e);
  }
}

// ===== Saves =====

async function loadSaveList() {
  try {
    const saves = await api.getSaves();
    const section = document.getElementById('saves-section');
    const list = document.getElementById('saves-list');
    if (!saves.length) { section.style.display = 'none'; return; }
    section.style.display = '';
    list.innerHTML = '';
    const scriptList = state.get('scriptList') || [];
    saves.forEach(s => {
      const scriptName = (scriptList.find(x => x.id === s.script_id) || {}).name || s.script_id;
      const time = s.timestamp ? new Date(s.timestamp).toLocaleString('zh-CN') : '';
      const card = document.createElement('div');
      card.className = 'save-card';
      card.innerHTML =
        '<div class="save-card-info">' +
          '<div class="save-card-name">' + esc(scriptName) + ' - 轮次 ' + s.turn + '</div>' +
          '<div class="save-card-meta">' + time + '</div>' +
        '</div>' +
        '<div style="display:flex;gap:8px;">' +
          '<button class="save-card-btn" onclick="event.stopPropagation(); loadSave(' + s.id + ')">继续</button>' +
          '<button class="save-card-btn" style="background:#d32f2f;flex:0;" onclick="event.stopPropagation(); deleteSave(' + s.id + ')">删除</button>' +
        '</div>';
      list.appendChild(card);
    });
  } catch (e) {
    console.warn('Failed to load saves:', e);
  }
}

let loadSaveSequence = 0;
let loadSaveRequest = Promise.resolve();

function loadSave(saveId) {
  const sequence = ++loadSaveSequence;
  const run = async () => {
    if (sequence !== loadSaveSequence) return;
    try {
      const aiProfileId = parseInt(document.getElementById('ai-profile-select').value) || null;
      const result = await api.loadSave(saveId, aiProfileId);
      if (sequence !== loadSaveSequence) return;
      if (result.ok) {
        if (result.turn_history && Array.isArray(result.turn_history)) {
          state.set('turnHistory', result.turn_history);
        } else {
          state.set('turnHistory', []);
        }
        const stateData = await api.getState();
        if (sequence !== loadSaveSequence) return;
        _enterGameScreen?.(stateData);
      }
    } catch (e) {
      if (sequence === loadSaveSequence) alert('加载存档失败: ' + e.message);
    }
  };
  loadSaveRequest = loadSaveRequest.then(run, run);
  return loadSaveRequest;
}

async function deleteSave(saveId) {
  if (!confirm('确定删除该存档？')) return;
  try {
    const result = await api.deleteSave(saveId);
    if (result.ok) {
      loadSaveList();
    } else {
      alert('删除失败: ' + (result.error || '未知错误'));
    }
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

// ===== Script List =====

async function loadScriptList() {
  try {
    const list = await api.getScripts();
    state.set('scriptList', list);
  } catch (e) {
    state.set('scriptList', []);
  }

  const scriptList = state.get('scriptList');
  const select = document.getElementById('script-select');
  select.innerHTML = '<option value="">-- 选择剧本 --</option>';
  scriptList.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = `${s.name} (${s.npc_count}人 / ${s.loc_count}地 / ${s.preset_count}预设)`;
    select.appendChild(opt);
  });
}

// ===== Script Selection =====

async function onScriptSelected() {
  const scriptId = document.getElementById('script-select').value;
  const infoEl = document.getElementById('script-info');
  const charSection = document.getElementById('character-section');
  const cardsEl = document.getElementById('preset-cards');
  const btnStart = document.getElementById('btn-start-game');

  state.set('currentScriptDetail', null);
  state.set('selectedPresetId', null);
  btnStart.disabled = true;

  if (!scriptId) {
    infoEl.classList.remove('show');
    charSection.style.display = 'none';
    return;
  }

  try {
    const detail = await api.getScript(scriptId);
    state.set('currentScriptDetail', detail);
  } catch (e) {
    infoEl.classList.remove('show');
    return;
  }

  const d = state.get('currentScriptDetail');

  document.getElementById('script-info-name').textContent = d.name;
  document.getElementById('script-info-desc').textContent = d.description || '';
  document.getElementById('script-info-meta').innerHTML =
    '<span>' + d.npc_count + ' 位角色</span>' +
    '<span>' + d.loc_count + ' 个地点</span>' +
    '<span>' + (d.player_presets || []).length + ' 个预设开局</span>';
  infoEl.classList.add('show');

  const presets = d.player_presets || [];
  if (presets.length === 0) {
    cardsEl.innerHTML = '<div class="no-presets">该剧本无预设角色，将使用默认开局</div>';
    charSection.style.display = '';
    btnStart.disabled = false;
    return;
  }

  charSection.style.display = '';
  cardsEl.innerHTML = '';
  presets.forEach(p => {
    const attrs = p.attributes || {};
    const attrKeys = Object.keys(attrs).slice(0, 4);

    const card = document.createElement('div');
    card.className = 'char-card';
    card.dataset.presetId = p.id;
    card.onclick = () => selectPreset(p.id);

    let attrHtml = '';
    if (attrKeys.length) {
      attrHtml = '<div class="char-card-attrs">';
      attrKeys.forEach(k => {
        const v = attrs[k];
        attrHtml +=
          '<div class="char-attr-row">' +
          '<span class="char-attr-label">' + esc(k) + '</span>' +
          '<div class="char-attr-bar"><div class="char-attr-fill" style="width:' + Math.min(100, v) + '%"></div></div>' +
          '<span class="char-attr-val">' + v + '</span>' +
          '</div>';
      });
      attrHtml += '</div>';
    }

    card.innerHTML =
      '<div class="char-card-name">' + esc(p.name) + '</div>' +
      '<div class="char-card-bio">' + esc(p.bio) + '</div>' +
      attrHtml +
      (p.long_term_goal ? '<div class="char-card-goal">' + esc(p.long_term_goal) + '</div>' : '') +
      (p.portrait_desc ? '<div class="char-card-portrait">' + esc(p.portrait_desc) + '</div>' : '');
    cardsEl.appendChild(card);
  });
}

// ===== Preset Selection =====

function selectPreset(presetId) {
  state.set('selectedPresetId', presetId);
  document.querySelectorAll('#preset-cards .char-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.presetId === presetId);
  });
  document.getElementById('btn-start-game').disabled = false;
}

// ===== Start Game =====

async function startGame() {
  const scriptId = document.getElementById('script-select').value;
  if (!scriptId) return;

  const btn = document.getElementById('btn-start-game');
  btn.disabled = true;
  btn.textContent = '加载中...';

  try {
    const payload = {
      script_id: scriptId,
      preset_id: state.get('selectedPresetId'),
      ai_profile_id: parseInt(document.getElementById('ai-profile-select').value) || null,
    };
    const result = await api.startGame(payload);
    state.set('openingText', result.opening_text || '');

    if (state.get('openingText')) {
      document.getElementById('opening-text').textContent = state.get('openingText');
      document.getElementById('opening-overlay').style.display = '';
    } else {
      const stateData = await api.getState();
      _enterGameScreen?.(stateData);
    }
  } catch (e) {
    alert('启动失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = '开始游戏';
  }
}

// ===== Opening =====

async function dismissOpening() {
  document.getElementById('opening-overlay').style.display = 'none';
  const stateData = await api.getState();
  _enterGameScreen?.(stateData);
}

// ===== Start with script (from create mode) =====

async function startGameWithScript(scriptId) {
  const aiProfileId = parseInt(document.getElementById('ai-profile-select')?.value) || null;
  try {
    const result = await api.startGame({ script_id: scriptId, ai_profile_id: aiProfileId });
    document.getElementById('create-screen').style.display = 'none';
    state.set('openingText', result.opening_text || '');
    if (state.get('openingText')) {
      document.getElementById('opening-text').textContent = state.get('openingText');
      document.getElementById('opening-overlay').style.display = '';
    } else {
      const stateData = await api.getState();
      _enterGameScreen?.(stateData);
    }
  } catch (e) {
    alert('启动失败: ' + e.message);
  }
}

// ===== Init =====

export function initStartScreen({ enterGameScreen } = {}) {
  _enterGameScreen = enterGameScreen || null;

  // Mount functions to window for HTML onclick compatibility
  window.boot              = boot;
  window.loadAIProfiles    = loadAIProfiles;
  window.loadSaveList      = loadSaveList;
  window.loadSave          = loadSave;
  window.deleteSave        = deleteSave;
  window.loadScriptList    = loadScriptList;
  window.onScriptSelected  = onScriptSelected;
  window.selectPreset      = selectPreset;
  window.startGame         = startGame;
  window.dismissOpening    = dismissOpening;
  window.startGameWithScript = startGameWithScript;
}
