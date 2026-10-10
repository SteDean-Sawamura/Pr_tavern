import * as state from '../state.js';
import * as api from '../api.js';
import { esc, npcColor, svgEl, LOC_NAMES } from '../utils.js';

// ===== NPC chat log storage =====
const npcChatLogs = {};

// ===== Functions =====

function selectNpc(npcId) {
  const gameState = state.get('gameState');
  const pendingResult = state.get('pendingResult');
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

  // Pending inference
  if (pendingResult) {
    const out = pendingResult.npc_outputs?.[npcId];
    if (out) {
      html += '<div class="card-label mt-8">本轮推演</div>';
      html += renderInferenceCard(npcId, out);
    }
  }

  window.showDetail(html);
  renderChatLog(npcId);
}

function renderInferenceCard(npcId, out) {
  const pendingResult = state.get('pendingResult');
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

  // Think block (collapsible, Tavern style)
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

  // Tool calls (if any)
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

function renderInferenceBubbles(npcOutputs) {
  const svg = document.getElementById('map-svg');
  svg.querySelectorAll('.inference-bubble').forEach(el => el.remove());

  Object.entries(npcOutputs).forEach(([npcId, out]) => {
    const npcDot = svg.querySelector(`[data-npc-id="${npcId}"]`);
    if (!npcDot) return;

    // Extract position from transform attribute
    const tf = npcDot.getAttribute('transform') || '';
    const m = tf.match(/translate\(([\d.]+)\s*,\s*([\d.]+)\)/);
    if (!m) return;
    const cx = parseFloat(m[1]);
    const cy = parseFloat(m[2]);
    const isActive = out.action_type !== '观望' && out.action_type !== '背景支持';
    const hasNeg = (out.proposed_changes || []).some(c => c.value < 0);
    const color = !isActive ? 'var(--text-muted)' : hasNeg ? 'var(--danger)' : 'var(--success)';

    // Bubble
    const bubble = svgEl('circle', {
      cx, cy: cy - 16, r: 5,
      fill: color, class: 'inference-bubble',
      opacity: isActive ? '1' : '0.5',
    });
    svg.appendChild(bubble);

    // Pulsing ring for active
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

async function sendNpcChat(npcId) {
  const input = document.getElementById('npc-chat-input');
  const msg = (input?.value || '').trim();
  if (!msg) return;
  input.value = '';
  input.disabled = true;

  if (!npcChatLogs[npcId]) npcChatLogs[npcId] = [];
  npcChatLogs[npcId].push({role: 'player', text: msg});
  renderChatLog(npcId);

  try {
    const data = await api.talkToNpc(npcId, msg);
    if (data.error) {
      npcChatLogs[npcId].push({role: 'system', text: data.error});
    } else {
      npcChatLogs[npcId].push({role: 'npc', text: data.response, name: data.npc_name});
      const gameState = state.get('gameState');
      if (data.relationship !== undefined && gameState?.relationships) {
        gameState.relationships[npcId] = data.relationship;
      }
      if (data.relationship_crossings?.length) {
        data.relationship_crossings.forEach(c => {
          const dir = c.direction === 'up' ? '↑' : '↓';
          npcChatLogs[npcId].push({role: 'system', text: `关系跨越 ${c.threshold} ${dir} (${c.from}→${c.to})`});
        });
      }
    }
  } catch (e) {
    npcChatLogs[npcId].push({role: 'system', text: '对话失败: ' + e.message});
  }
  input.disabled = false;
  input.focus();
  renderChatLog(npcId);
}

function renderChatLog(npcId) {
  const log = document.getElementById('npc-chat-log');
  if (!log) return;
  const msgs = npcChatLogs[npcId] || [];
  log.innerHTML = msgs.map(m => {
    if (m.role === 'player') return `<div style="text-align:right;margin:2px 0"><span style="background:var(--accent);color:white;padding:2px 8px;border-radius:8px;display:inline-block;max-width:85%">${esc(m.text)}</span></div>`;
    if (m.role === 'npc') return `<div style="margin:2px 0"><span style="color:var(--text-muted);font-size:10px">${esc(m.name || '')}:</span><br><span style="background:var(--bg-lighter);padding:2px 8px;border-radius:8px;display:inline-block;max-width:85%;color:var(--text-secondary)">${esc(m.text)}</span></div>`;
    return `<div style="margin:2px 0;text-align:center;font-size:10px;color:var(--text-muted)">${esc(m.text)}</div>`;
  }).join('');
  log.scrollTop = log.scrollHeight;
}

function acceptNpc(npcId) {
  const gameState = state.get('gameState');
  const npc = (gameState?.npcs || []).find(n => n.id === npcId);
  window.updateStatus(`已接受 ${npc?.name || npcId} 的推演`);
}

function openRetryModal(npcId, npcName) {
  state.set('retryNpcId', npcId);
  document.getElementById('retry-npc-name').textContent = npcName;
  document.getElementById('retry-hint').value = '';
  document.getElementById('retry-modal').classList.add('show');
  setTimeout(() => document.getElementById('retry-hint').focus(), 100);
}

function closeRetryModal() {
  document.getElementById('retry-modal').classList.remove('show');
  state.set('retryNpcId', null);
}

async function executeRetry() {
  const retryNpcId = state.get('retryNpcId');
  if (!retryNpcId) return;
  const hint = document.getElementById('retry-hint').value.trim();
  const gameState = state.get('gameState');
  const npc = (gameState?.npcs || []).find(n => n.id === retryNpcId);
  window.updateStatus(`正在重推 ${npc?.name || retryNpcId}...`);

  try {
    const result = await api.retryNpc(retryNpcId, hint);
    const pendingResult = state.get('pendingResult');

    if (result.npc_output) pendingResult.npc_outputs[retryNpcId] = result.npc_output;
    if (result.outline) pendingResult.outline = result.outline;
    if (result.scene) pendingResult.scene = result.scene;

    renderInferenceBubbles(pendingResult.npc_outputs);
    window.showTurnResults(pendingResult);
    window.updateStatus(`${npc?.name || retryNpcId} 已重新推演`);

  } catch (e) {
    window.updateStatus('重推失败: ' + e.message);
  }

  closeRetryModal();
}

// ===== Local helper =====

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

// ===== Init =====

export function initNpcPanel() {
  window.selectNpc = selectNpc;
  window.renderInferenceCard = renderInferenceCard;
  window.renderInferenceBubbles = renderInferenceBubbles;
  window.sendNpcChat = sendNpcChat;
  window.renderChatLog = renderChatLog;
  window.acceptNpc = acceptNpc;
  window.openRetryModal = openRetryModal;
  window.closeRetryModal = closeRetryModal;
  window.executeRetry = executeRetry;
}
