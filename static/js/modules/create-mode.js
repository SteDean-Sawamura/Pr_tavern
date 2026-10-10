/* create-mode.js -- script creation wizard */

import * as state from '../state.js';
import * as api   from '../api.js';

// ===== Enter / Exit =====

function enterCreateMode() {
  document.getElementById('start-screen').classList.add('hidden');
  document.getElementById('create-screen').style.display = '';
  state.set('createStarted', false);
  document.getElementById('create-chat').innerHTML = '';
  updateCreatePhase('theme');
  addCreateMessage('system',
    '请输入你想创建的剧本主旨。\n\n' +
    '例如："中世纪骑士冒险"、"都市悬疑推理"、"太空殖民生存"、"二战谍战"');
}

function exitCreateMode() {
  document.getElementById('create-screen').style.display = 'none';
  document.getElementById('start-screen').classList.remove('hidden');
  state.set('createStarted', false);
}

// ===== Chat helpers =====

function addCreateMessage(role, text) {
  const chat = document.getElementById('create-chat');
  const div = document.createElement('div');
  div.className = 'create-msg ' + role;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function updateCreatePhase(currentPhase) {
  const phases = ['theme', 'world_and_player', 'npcs', 'rules', 'review'];
  const els = document.querySelectorAll('#create-phases .create-phase');
  const idx = phases.indexOf(currentPhase);
  els.forEach((el, i) => {
    el.classList.remove('active', 'done');
    if (i < idx) el.classList.add('done');
    else if (i === idx) el.classList.add('active');
  });
}

// ===== Submit =====

async function submitCreateInput() {
  const ta = document.getElementById('create-input');
  const input = ta.value.trim();
  if (!input) return;
  ta.value = '';
  addCreateMessage('user', input);
  const loadingEl = addCreateMessage('system', '正在生成...');
  loadingEl.classList.add('loading');

  try {
    const createStarted = state.get('createStarted');
    let result;
    if (createStarted) {
      result = await api.createNext(input);
    } else {
      result = await api.createStart(input);
    }
    state.set('createStarted', true);

    loadingEl.remove();
    updateCreatePhase(result.phase || 'review');
    addCreateMessage('system', result.reply || '(无回复)');

    if (result.done && result.script_id) {
      const doneEl = addCreateMessage('system', '剧本已保存！正在启动游戏...');
      doneEl.style.color = 'var(--success)';
      setTimeout(() => {
        if (typeof window.startGameWithScript === 'function') {
          window.startGameWithScript(result.script_id);
        }
      }, 1500);
    }
  } catch (e) {
    loadingEl.textContent = '错误: ' + e.message;
    loadingEl.classList.remove('loading');
  }
}

// ===== Init =====

export function initCreateMode() {
  // Mount functions to window for HTML onclick compatibility
  window.enterCreateMode   = enterCreateMode;
  window.exitCreateMode    = exitCreateMode;
  window.addCreateMessage  = addCreateMessage;
  window.updateCreatePhase = updateCreatePhase;
  window.submitCreateInput = submitCreateInput;
}
