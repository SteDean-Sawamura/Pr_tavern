/**
 * 酒馆 RPG — ES Module 入口
 * 初始化所有功能模块，协调模块间依赖
 */

import * as state from './state.js';
import * as api from './api.js';
import { LOC_NAMES } from './utils.js';

import { initStartScreen } from './modules/start-screen.js';
import { initCreateMode } from './modules/create-mode.js';
import { initPersistence } from './modules/persistence.js';
import { initPanels } from './modules/panels.js';
import { initNpcPanel } from './modules/npc-panel.js';
import { initMapRenderer } from './modules/map-renderer.js';
import { initGameTurn } from './modules/game-turn.js';

// 初始化所有模块
function bootstrap() {
  // 基础模块（无依赖）
  initGameTurn();
  initMapRenderer();
  initNpcPanel();
  initCreateMode();

  // panels 需要 state 订阅
  initPanels();

  // persistence 依赖一些 UI 函数
  initPersistence({
    updateStatus: window.updateStatus,
    loadScriptList: window.loadScriptList,
    loadAIProfiles: window.loadAIProfiles,
    loadSaveList: window.loadSaveList,
  });

  // start-screen 最后初始化（它调用 boot()）
  initStartScreen({
    enterGameScreen: window.enterGameScreen,
  });

  // 启动应用
  window.boot();
}

document.addEventListener('DOMContentLoaded', bootstrap);
