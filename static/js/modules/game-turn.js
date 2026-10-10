import { esc, LOC_NAMES, npcColor, sleep, svgEl } from '../utils.js';
import * as state from '../state.js';
import * as api from '../api.js';

// ===== Local helpers =====

function renderInferenceCard(npcId, out) {
  const pendingResult = state.get('pendingResult');
  const gameState = state.get('gameState');
  const isActive = out.action_type !== '观望' && out.action_type !== '背景支持';
  const hasNeg = (out.proposed_changes || []).some(c => c.value < 0);
  const cls = !isActive ? 'neutral' : hasNeg ? 'negative' : 'positive';
  const statusCls = cls;

  let html = `<div class="inference-result ${cls}">
    <div class="inference-header">
      <span class="inference-status ${statusCls}"></span>
      <span>${esc(out.name || npcId)}</span>
      <span style="color:var(--text-muted);font-size:10px;margin-left:auto">${esc(out.action_type)}</span>
    </div>`;

  // Emotion tag
  if (out.emotion) {
    html += `<div style="font-size:10px;color:var(--info);margin-bottom:4px">
      <span style="opacity:0.7">情绪:</span> ${esc(out.emotion)}
    </div>`;
  }

  // Think block (collapsible)
  if (out.thought) {
    html += `<div class="think-block" onclick="this.classList.toggle('open')">
      <div class="think-header">
        <span>内心想法</span>
        <span class="toggle-arrow">&#9660;</span>
      </div>
      <div class="think-body">${esc(out.thought)}</div>
    </div>`;
  }

  // Dialogue
  if (out.dialogue) {
    html += `<div class="dialogue-block">
      <div class="dialogue-speaker">${esc(out.name || npcId)}</div>
      "${esc(out.dialogue)}"
    </div>`;
  }

  // Proposed changes
  if (out.proposed_changes && out.proposed_changes.length) {
    html += '<ul class="change-list">';
    out.proposed_changes.forEach(c => {
      const isPos = c.value > 0 || c.value === true;
      html += `<li class="change-item">
        <span>${esc(c.var)}</span>
        <span class="${isPos ? 'change-positive' : 'change-negative'}">${typeof c.value === 'boolean' ? (c.value ? 'true' : 'false') : (isPos ? '+' : '') + c.value}</span>
      </li>`;
    });
    html += '</ul>';
  }

  // Tool calls
  if (out.tool_calls && out.tool_calls.length) {
    html += '<div style="margin-top:8px;padding:4px;background:rgba(100,150,255,0.05);border-left:2px solid var(--info);font-size:10px">';
    html += '<div style="color:var(--info);font-weight:500;margin-bottom:4px">工具调用:</div>';
    out.tool_calls.forEach(tc => {
      html += `<div style="margin-bottom:2px;color:var(--text-secondary)">
        <span style="font-weight:500">${esc(tc.name)}</span>
        <span style="opacity:0.7">→ ${JSON.stringify(tc.result).slice(0, 60)}...</span>
      </div>`;
    });
    html += '</div>';
  }

  // Uncertainty
  if (out.uncertainty && out.uncertainty.length) {
    html += `<div style="font-size:10px;color:var(--text-muted);margin-top:4px;font-style:italic">
      <span style="opacity:0.7">不确定: </span>${out.uncertainty.map(u => esc(u)).join('; ')}
    </div>`;
  }

  // Accept / Retry buttons
  if (pendingResult) {
    html += `<div class="action-buttons">
      <button class="btn btn-accept" onclick="event.stopPropagation();acceptNpc('${esc(npcId)}')">接受</button>
      <button class="btn btn-retry" onclick="event.stopPropagation();openRetryModal('${esc(npcId)}', '${esc(out.name || npcId)}')">重推</button>
    </div>`;
  }

  html += '</div>';
  return html;
}

// ===== Exported functions =====

async function submitAction() {
  if (state.get('pendingResult')) {
    updateStatus('请先确认当前推演');
    return;
  }
  const gameState = state.get('gameState');
  const input = document.getElementById('action-input');
  const action = input.value.trim();
  if (!action) return;

  input.disabled = true;
  document.getElementById('btn-submit').disabled = true;

  const phaseBar = document.getElementById('phase-bar');
  phaseBar.style.display = '';
  document.getElementById('map-area').classList.add('inferring');

  setPhase(0);
  updateStatus('Director 分析中...');

  try {
    const result = await api.submitTurn(action);
    if (state.get('gameState') !== gameState) return;

    // 处理 Director 非推演响应
    if (result.side_results) {
      for (const sr of result.side_results) {
        if (sr.type === 'ui_command') executeUICommand(sr);
        else if (sr.type === 'query') showDirectorReply(sr.answer);
        else if (sr.type === 'meta_feedback') showDirectorReply(sr.reply || '已记录你的反馈');
        else if (sr.type === 'save_load') {
          if (sr.action === 'save') updateStatus('已保存: #' + sr.id);
          else updateStatus('请使用左侧面板读档');
        }
      }
    }

    // 纯非推演请求（无 game_action）
    if (!result.pending && result.pending !== undefined) {
      if (result.reply) showDirectorReply(result.reply);
      phaseBar.style.display = 'none';
      document.getElementById('map-area').classList.remove('inferring');
      input.disabled = false;
      document.getElementById('btn-submit').disabled = false;
      return;
    }

    // 有推演结果
    state.set('pendingResult', result);

    // 显示 Director 规划摘要
    if (result.director_plan) {
      const dp = result.director_plan;
      const ga = (dp.intents || []).find(i => i.type === 'game_action');
      if (ga && ga.resolved_action && ga.resolved_action !== action) {
        updateStatus(`Director: ${ga.resolved_action}`);
        await sleep(500);
        if (state.get('gameState') !== gameState || state.get('pendingResult') !== result) return;
      }
      const skipped = ga?.skip_npcs?.length || 0;
      if (skipped) updateStatus(`Director: 跳过 ${skipped} 个无关NPC`);
    }

    setPhase(1);
    updateStatus('信息收集中...');
    await sleep(200);
    if (state.get('gameState') !== gameState || state.get('pendingResult') !== result) return;

    setPhase(2);
    updateStatus('NPC推理完成');
    await sleep(300);
    if (state.get('gameState') !== gameState || state.get('pendingResult') !== result) return;

    setPhase(3);
    updateStatus('综合生成完成 - 请审查各NPC推演结果');

    renderInferenceBubbles(result.npc_outputs || {});
    document.getElementById('btn-confirm').style.display = '';
    showTurnResults(result);

    // Reload state and re-render panels
    const newState = await api.getState();
    if (state.get('gameState') !== gameState || state.get('pendingResult') !== result) return;
    state.set('gameState', newState);
    (newState.locations || []).forEach(l => { LOC_NAMES[l.id] = l.name; });

  } catch (e) {
    console.error('Error:', e);
    if (state.get('gameState') === gameState) updateStatus('推演失败: ' + e.message);
  } finally {
    if (state.get('gameState') === gameState) {
      input.disabled = Boolean(state.get('pendingResult'));
      document.getElementById('btn-submit').disabled = Boolean(state.get('pendingResult'));
    }
  }
}

function showTurnResults(result) {
  switchTab('tab-detail');
  let html = '';

  // Scene text (narrative)
  if (result.scene?.scene_text) {
    html += `<div class="scene-text">${esc(result.scene.scene_text)}</div>`;
    html += `<div style="display:flex;gap:6px;margin-top:8px;justify-content:flex-end">
      <button class="btn" style="padding:3px 8px;font-size:10px;background:var(--bg-lighter);border:1px solid var(--border);color:var(--text-secondary);border-radius:4px" onclick="swipeScene('left')" title="上一个版本">◀</button>
      <span id="swipe-indicator" style="font-size:10px;color:var(--text-muted);line-height:24px">1/1</span>
      <button class="btn" style="padding:3px 8px;font-size:10px;background:var(--bg-lighter);border:1px solid var(--border);color:var(--text-secondary);border-radius:4px" onclick="swipeScene('right')" title="下一个版本">▶</button>
      <button class="btn" style="padding:3px 8px;font-size:10px;background:var(--accent);color:white;border:none;border-radius:4px" onclick="regenerateScene()" title="生成新版本">换一版</button>
    </div>`;
  }

  // Atmosphere
  if (result.scene?.atmosphere) {
    html += `<div style="margin-top:12px;padding:8px;background:rgba(200,150,100,0.1);border-radius:4px;font-size:12px;color:var(--text-secondary);font-style:italic">
      🎭 ${esc(result.scene.atmosphere)}
    </div>`;
  }

  // Consequence notes
  if (result.scene?.consequence_notes?.length) {
    html += '<div style="margin-top:12px;padding:8px;background:rgba(100,200,255,0.08);border-left:3px solid var(--info)">';
    html += '<div style="font-size:11px;color:var(--info);font-weight:500;margin-bottom:4px">后果：</div>';
    result.scene.consequence_notes.forEach(note => {
      html += `<div style="font-size:11px;color:var(--text-secondary);margin-bottom:3px">• ${esc(note)}</div>`;
    });
    html += '</div>';
  }

  // UI Effects
  if (result.scene?.ui_effects?.length) {
    for (const effect of result.scene.ui_effects) {
      if (effect.type === 'play_sound') {
        console.log(`[Sound] ${effect.params.sound_type}`);
      } else if (effect.type === 'set_weather') {
        console.log(`[Weather] ${effect.params.weather}`);
      } else if (effect.type === 'set_mood_filter') {
        console.log(`[Mood] ${effect.params.mood}`);
      }
    }
  }

  // Outline summary
  const outline = result.outline || {};
  if (outline.summary) {
    html += `<div class="card"><div class="card-label">大纲摘要</div>
      <div style="font-size:12px;color:var(--text-secondary);line-height:1.6">${esc(outline.summary)}</div>
    </div>`;
  }

  // Conflicts
  if (outline.conflicts?.length) {
    html += '<div class="card"><div class="card-label">冲突检测</div>';
    outline.conflicts.forEach(c => {
      const severity = c.severity === 'high' ? 'var(--danger)' : c.severity === 'medium' ? 'var(--warning)' : 'var(--text-secondary)';
      html += `<div style="font-size:11px;color:${severity};margin-bottom:4px;display:flex;gap:6px;align-items:center">
        <span class="choice-risk-bar" style="background:${severity}"></span>
        <span>${esc((c.between || []).join(' vs '))}</span>
        <span style="color:var(--text-muted)">- ${esc(c.topic)}</span>
      </div>`;
    });
    html += '</div>';
  }

  // Dialogue log
  if (result.scene?.dialogue_log?.length) {
    html += '<div class="card"><div class="card-label">对话</div>';
    result.scene.dialogue_log.forEach(d => {
      html += `<div class="dialogue-block">
        <div class="dialogue-speaker">${esc(d.speaker)}</div>
        ${esc(d.text)}
      </div>`;
    });
    html += '</div>';
  }

  // NPC actions
  const npcOutputs = result.npc_outputs || {};
  if (Object.keys(npcOutputs).length) {
    html += '<div class="card-label mt-8">NPC行动</div>';
    Object.entries(npcOutputs).forEach(([npcId, out]) => {
      html += renderInferenceCard(npcId, out);
    });
  }

  // State updates summary
  if (outline.state_updates?.length) {
    html += '<div class="card"><div class="card-label">状态变化预览</div>';
    outline.state_updates.forEach(c => {
      const isPos = c.value > 0 || c.value === true;
      const cls = isPos ? 'change-positive' : typeof c.value === 'boolean' ? '' : 'change-negative';
      html += `<div class="change-item" style="font-size:11px;margin-bottom:3px">
        <span>${esc(c.var)}</span>
        <span class="${cls}" style="font-weight:500">${c.op === 'set' ? '= ' : (c.value > 0 ? '+' : '')}${c.value}</span>
      </div>`;
    });
    html += '</div>';
  }

  // Choices
  if (result.scene?.choices?.length) {
    html += '<div class="card-label mt-8">选择</div>';
    result.scene.choices.forEach(c => {
      const effects = c.effects?.relationship_changes || {};
      const tags = Object.entries(effects).map(([k, v]) => `${k} ${v > 0 ? '+' : ''}${v}`);
      html += `<button class="choice-btn" onclick="makeChoice('${esc(c.id)}')">
        <span class="choice-text">${esc(c.id)}. ${esc(c.text)}</span>
        ${c.preview ? `<span class="choice-hint">${esc(c.preview)}</span>` : ''}
        ${tags.length ? `<div class="choice-previews">${tags.map(t => `<span class="choice-preview-tag">${esc(t)}</span>`).join('')}</div>` : ''}
      </button>`;
    });
  }

  showDetail(html);
}

let confirmInFlight = false;

function makeChoice(choiceId) {
  if (confirmInFlight) return;
  const pendingResult = state.get('pendingResult');
  if (!pendingResult) return;
  const choice = (pendingResult.scene?.choices || []).find(c => c.id === choiceId);
  if (!choice) return;

  state.set('lastChoice', { id: choiceId, text: choice.text });
  updateStatus(`已选择: ${choice.text}，正在确认...`);
  return confirmTurnWithChoice(choiceId);
}

async function confirmTurnWithChoice(choiceId) {
  return confirmPendingTurn(choiceId);
}

async function confirmTurn() {
  return confirmPendingTurn();
}

async function confirmPendingTurn(choiceId) {
  const pendingResult = state.get('pendingResult');
  if (confirmInFlight || !pendingResult) return;
  const gameState = state.get('gameState');
  confirmInFlight = true;
  document.getElementById('btn-confirm').disabled = true;
  if (!choiceId) updateStatus('确认推演中...');
  try {
    let result;
    try {
      result = await api.confirm(choiceId ? { choice_id: choiceId } : undefined);
    } catch (e) {
      if (state.get('gameState') === gameState && state.get('pendingResult') === pendingResult) {
        updateStatus('确认失败: ' + e.message);
      }
      return;
    }
    if (state.get('gameState') !== gameState || state.get('pendingResult') !== pendingResult) return;
    const choice = choiceId ? (pendingResult.scene?.choices || []).find(c => c.id === choiceId) : null;

    if (pendingResult) {
      const turnHistory = state.get('turnHistory');
      turnHistory.push({
        turn_num: pendingResult.turn_num || gameState?.turn,
        timestamp: new Date().toISOString(),
        player_action: pendingResult.player_action || document.getElementById('action-input').value,
        ...(choice ? { choice: { id: choice.id, text: choice.text } } : {}),
        summary: pendingResult.outline?.summary || '',
        scene_text: pendingResult.scene?.scene_text || '',
      });
      state.set('turnHistory', turnHistory);
    }

    state.set('pendingResult', null);
    state.set('lastChoice', null);
    document.getElementById('btn-confirm').style.display = 'none';
    document.getElementById('btn-save').style.display = '';
    document.getElementById('btn-submit').disabled = false;
    document.getElementById('phase-bar').style.display = 'none';
    document.getElementById('action-input').value = '';
    document.getElementById('action-input').disabled = false;
    document.getElementById('map-area').classList.remove('inferring');
    clearDetail();

    try {
      const newState = await api.getState();
      if (state.get('gameState') !== gameState) return;
      state.set('gameState', newState);
      (newState.locations || []).forEach(l => { LOC_NAMES[l.id] = l.name; });
      updateStatus(choice ? `已确认: ${choice.text}，请记得存档` : '推演已确认，请记得存档');
    } catch (e) {
      if (state.get('gameState') !== gameState) return;
      if (result.state_after) {
        state.set('gameState', {
          ...gameState, ...result.state_after,
          location: result.state_after.location_id || result.state_after.location,
          pending_result: null,
        });
      } else {
        document.getElementById('btn-submit').disabled = true;
        document.getElementById('action-input').disabled = true;
      }
      updateStatus('推演已确认，但状态刷新失败，请刷新页面: ' + e.message);
    }
  } finally {
    confirmInFlight = false;
    document.getElementById('btn-confirm').disabled = false;
  }
}

async function regenerateScene() {
  updateStatus('正在重新生成场景...');
  try {
    const data = await api.regenerate();
    if (data.error) { updateStatus(data.error); return; }
    const st = document.querySelector('.scene-text');
    if (st && data.scene?.scene_text) st.textContent = data.scene.scene_text;
    const ind = document.getElementById('swipe-indicator');
    if (ind) ind.textContent = `${(data.swipe_index||0)+1}/${data.total_swipes||1}`;
    updateStatus(`已生成第 ${(data.swipe_index||0)+1} 个版本`);
  } catch(e) { updateStatus('重生成失败: ' + e.message); }
}

async function swipeScene(direction) {
  try {
    const data = await api.swipe(direction);
    const st = document.querySelector('.scene-text');
    if (st && data.scene_text) st.textContent = data.scene_text;
    const ind = document.getElementById('swipe-indicator');
    if (ind) ind.textContent = `${(data.swipe_index||0)+1}/${data.total_swipes||1}`;
  } catch(e) { updateStatus('切换失败'); }
}

function showDirectorReply(text) {
  if (!text) return;
  switchTab('tab-detail');
  const html = `<div class="card" style="border-left:3px solid var(--accent);padding:12px">
    <div class="card-label" style="color:var(--accent)">Director</div>
    <div style="font-size:13px;line-height:1.7;white-space:pre-wrap">${esc(text)}</div>
  </div>`;
  showDetail(html);
}

function executeUICommand(cmd) {
  const command = cmd.command || '';
  const params = cmd.params || {};
  if (command === 'zoom_map' || command === 'zoom_in') {
    const svg = document.getElementById('map-svg');
    if (svg) { const vb = svg.getAttribute('viewBox')?.split(' ').map(Number); if (vb) { const s = 0.8; svg.setAttribute('viewBox', `${vb[0]+vb[2]*(1-s)/2} ${vb[1]+vb[3]*(1-s)/2} ${vb[2]*s} ${vb[3]*s}`); } }
  } else if (command === 'zoom_out') {
    const svg = document.getElementById('map-svg');
    if (svg) { const vb = svg.getAttribute('viewBox')?.split(' ').map(Number); if (vb) { const s = 1.25; svg.setAttribute('viewBox', `${vb[0]-vb[2]*(s-1)/2} ${vb[1]-vb[3]*(s-1)/2} ${vb[2]*s} ${vb[3]*s}`); } }
  } else if (command === 'toggle_panel' || command === 'show_panel') {
    const panel = params.panel || 'tab-detail';
    switchTab(panel);
  }
  updateStatus(`UI: ${command}`);
}

function showDetail(bodyHtml) {
  document.getElementById('detail-empty').style.display = 'none';
  const content = document.getElementById('detail-content');
  content.style.display = '';
  content.innerHTML = bodyHtml;
}

function clearDetail() {
  document.getElementById('detail-content').style.display = 'none';
  document.getElementById('detail-empty').style.display = '';
}

function updateStatus(text) {
  const bar = document.getElementById('status-bar');
  bar.querySelector('span').textContent = text;
}

function setPhase(phase) {
  for (let i = 0; i <= 3; i++) {
    const el = document.getElementById(`phase-${i}`);
    if (el) el.className = 'phase-step' + (i < phase ? ' done' : i === phase ? ' active' : '');
  }
}

// ===== Helper used by submitAction =====

function renderInferenceBubbles(npcOutputs) {
  const svg = document.getElementById('map-svg');
  svg.querySelectorAll('.inference-bubble').forEach(el => el.remove());

  Object.entries(npcOutputs).forEach(([npcId, out]) => {
    const npcDot = svg.querySelector(`[data-npc-id="${npcId}"]`);
    if (!npcDot) return;

    const tf = npcDot.getAttribute('transform') || '';
    const m = tf.match(/translate\(([\d.]+)\s*,\s*([\d.]+)\)/);
    if (!m) return;
    const cx = parseFloat(m[1]);
    const cy = parseFloat(m[2]);
    const isActive = out.action_type !== '观望' && out.action_type !== '背景支持';
    const hasNeg = (out.proposed_changes || []).some(c => c.value < 0);
    const color = !isActive ? 'var(--text-muted)' : hasNeg ? 'var(--danger)' : 'var(--success)';

    const bubble = svgEl('circle', {
      cx, cy: cy - 16, r: 5,
      fill: color, class: 'inference-bubble',
      opacity: isActive ? '1' : '0.5',
    });
    svg.appendChild(bubble);

    if (isActive) {
      const ring = svgEl('circle', {
        cx, cy: cy - 16, r: 8,
        fill: 'none', stroke: color, 'stroke-width': '1',
        class: 'inference-bubble', opacity: '0.4',
      });
      ring.innerHTML = `<animate attributeName="r" from="5" to="12" dur="1.5s" repeatCount="indefinite"/>
        <animate attributeName="opacity" from="0.4" to="0" dur="1.5s" repeatCount="indefinite"/>`;
      svg.appendChild(ring);
    }
  });
}

// ===== Tab switching (local reference) =====

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

// ===== Window bindings & init =====

export function initGameTurn() {
  // Functions referenced by HTML onclick / innerHTML
  window.submitAction = submitAction;
  window.showTurnResults = showTurnResults;
  window.makeChoice = makeChoice;
  window.confirmTurnWithChoice = confirmTurnWithChoice;
  window.confirmTurn = confirmTurn;
  window.regenerateScene = regenerateScene;
  window.swipeScene = swipeScene;
  window.showDirectorReply = showDirectorReply;
  window.executeUICommand = executeUICommand;
  window.showDetail = showDetail;
  window.clearDetail = clearDetail;
  window.updateStatus = updateStatus;
  window.setPhase = setPhase;
}
