/* persistence.js -- save / exit game logic */

import * as state from '../state.js';
import * as api   from '../api.js';

// External hooks -- set via initPersistence()
let _updateStatus   = null;  // (text) => void
let _loadScriptList = null;  // () => Promise<void>
let _loadAIProfiles = null;  // () => Promise<void>
let _loadSaveList   = null;  // () => Promise<void>

// ===== Save =====

async function saveGame() {
  _updateStatus?.('保存中...');
  try {
    const result = await api.saveGame();
    if (result.ok) {
      _updateStatus?.('已保存: #' + result.id);
    } else {
      _updateStatus?.('保存失败');
    }
  } catch (e) {
    _updateStatus?.('保存失败: ' + e.message);
  }
}

// ===== Exit =====

async function exitGame() {
  const gs = state.get('gameState');
  if (gs?.turn > 0 && !confirm('确定要退出吗？未保存的进度将丢失。')) return;
  await api.resetGame().catch(() => {});
  document.body.classList.remove('in-game');
  document.getElementById('game-screen').classList.remove('active');
  document.getElementById('start-screen').classList.remove('hidden');
  document.getElementById('btn-save').style.display = 'none';
  state.merge({
    gameState: null,
    pendingResult: null,
    turnHistory: [],
    lastChoice: null,
  });
  await _loadScriptList?.();
  await _loadAIProfiles?.();
  await _loadSaveList?.();
}

// ===== Init =====

export function initPersistence({ updateStatus, loadScriptList, loadAIProfiles, loadSaveList } = {}) {
  _updateStatus   = updateStatus   || null;
  _loadScriptList = loadScriptList || null;
  _loadAIProfiles = loadAIProfiles || null;
  _loadSaveList   = loadSaveList   || null;

  // Mount functions to window for HTML onclick compatibility
  window.saveGame = saveGame;
  window.exitGame = exitGame;
}
