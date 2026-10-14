/* 酒馆 - 主应用逻辑 */

const API = {
    _headers(extra) {
        const h = { ...extra };
        const token = localStorage.getItem('tavern_api_token');
        if (token) h['Authorization'] = `Bearer ${token}`;
        return h;
    },
    async get(url) {
        const res = await fetch(url, { headers: this._headers() });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    },
    async post(url, data) {
        const res = await fetch(url, {
            method: 'POST',
            headers: this._headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const error = new Error(err.detail || `HTTP ${res.status}`);
            error.status = res.status;
            throw error;
        }
        return res.json();
    },
    async put(url, data) {
        const res = await fetch(url, {
            method: 'PUT',
            headers: this._headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(data),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    },
    async del(url) {
        const res = await fetch(url, { method: 'DELETE', headers: this._headers() });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    }
};

// Attribute display name translations (shared between char-select and status panel)
const _ATTR_LABELS = {
    health:"健康",mood:"心情",study_progress:"学习进度",
    strength:"力量",intelligence:"智力",charisma:"魅力",
    agility:"敏捷",stamina:"体力",luck:"幸运",
    money:"金钱",pocket_money:"零花钱",energy:"精力",
    hunger:"饱食度",reputation:"声望",morality:"道德",
};

// Global state
let currentSaveId = null;
let currentState = null;
let currentNodeId = null;
let _prevState = null;   // Previous state snapshot for change highlighting
let isStreaming = false;
let _gameCreationPending = false;
let _saveLoadSequence = 0;
let _historyRenderSequence = 0;
let _pendingHistoryPage = null;
let _pendingSwipeRequests = new Set();
let currentSwipeIndex = 0;
let totalSwipes = 1;

// --- Panel switching ---

function switchPanel(panel) {
    if (_closeMobilePanels(false)) document.querySelector('.sidebar-toggle').focus();
    if (panel !== 'game') document.getElementById('swipe-picker-modal')?.remove();
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(`panel-${panel}`).classList.add('active');
    document.querySelector(`[data-panel="${panel}"]`)?.classList.add('active');

    const statusPanel = document.getElementById('status-panel');
    const statusToggle = document.getElementById('status-panel-toggle');
    const showStatus = panel === 'game' && currentSaveId;
    statusPanel.style.display = showStatus ? 'flex' : 'none';
    statusToggle.style.display = showStatus ? 'flex' : 'none';
    if (showStatus) {
        const collapsed = localStorage.getItem('statusPanelCollapsed') === 'true';
        statusPanel.classList.toggle('collapsed', collapsed);
        statusToggle.innerHTML = collapsed ? '&#10094;' : '&#10095;';
    }

    if (panel === 'scripts') loadScripts();
    if (panel === 'materials') { loadMaterials(); loadTags(); }
    if (panel === 'settings') { loadProfiles(); loadImageProfiles(); }
    if (panel === 'game' && !currentSaveId) loadGameStart();
}

function switchStatusTab(btn) {
    const page = btn.dataset.page;
    if (page === 'sp-storytree' && document.getElementById('game-play-screen').classList.contains('map-view')) return;
    document.querySelectorAll('.status-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.status-page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(page)?.classList.add('active');
    if (page === 'sp-log') loadAdventureLog();
    if (page === 'sp-tree' && currentSaveId) loadWorldTree();
    if (page === 'sp-storytree' && currentSaveId) loadStoryTree();
    if (page === 'sp-clues' && currentState) renderClueBoard(currentState);
    if (page === 'sp-chronicle' && currentState) renderChronicle(currentState);
}

// --- Initialize ---

document.addEventListener('DOMContentLoaded', () => {
    window.matchMedia('(max-width: 600px)').addEventListener('change', _syncSidebarForViewport);
    _syncSidebarForViewport();
    loadGameStart();
    loadSidebarSaves();
    loadProfilesDropdown();
    loadPresets();
    // Skill check hint on input (debounced)
    const freeformInput = document.getElementById('freeform-input');
    if (freeformInput) {
        let _skillHintTimer = null;
        freeformInput.addEventListener('input', () => {
            clearTimeout(_skillHintTimer);
            const hint = document.getElementById('skill-check-hint');
            const text = freeformInput.value.trim();
            if (!text || text.length < 4) {
                if (hint) { hint.style.display = 'none'; hint.removeAttribute('data-loading'); }
                return;
            }
            // Show loading state
            if (hint) {
                hint.innerHTML = '<span class="skill-hint-spinner"></span> 检测技能检定...';
                hint.style.display = 'block';
                hint.setAttribute('data-loading', '1');
            }
            _skillHintTimer = setTimeout(() => {
                showSkillCheckHint(text);
            }, 800);
        });
    }
});

// ================================================
//  GAME
// ================================================

function _formatPlayTime(seconds) {
    if (!seconds || seconds < 60) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h}h${m > 0 ? m + 'm' : ''}`;
    return `${m}m`;
}

function _formatSaveTime(value) {
    const timestamp = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(value)
        ? value.replace(' ', 'T') + 'Z' : value;
    return new Date(timestamp).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
}

async function loadGameStart() {
    const requestId = ++_gameStartSequence;
    const select = document.getElementById('script-select');
    const status = document.getElementById('script-selection-status');
    const retryButton = document.getElementById('btn-retry-script');
    select.innerHTML = '';
    select.disabled = true;
    onScriptSelected();
    status.textContent = '正在加载剧本…';
    refreshPipelineMode();
    try {
        const scripts = await API.get('/api/scripts');
        if (requestId !== _gameStartSequence) return;
        (Array.isArray(scripts) ? scripts : []).forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name || s.id;
            select.appendChild(opt);
        });
        select.disabled = false;
        if (select.value) {
            await onScriptSelected();
        } else {
            status.textContent = '暂无可用剧本，请到剧本工坊创建或导入。';
        }
    } catch (e) {
        if (requestId !== _gameStartSequence) return;
        console.error('Failed to load scripts:', e);
        select.disabled = false;
        status.textContent = '剧本列表加载失败，请重试。';
        retryButton.hidden = false;
    }

    if (requestId !== _gameStartSequence) return;
    try {
        const saves = await API.get('/api/saves');
        if (requestId !== _gameStartSequence) return;
        const container = document.getElementById('continue-saves');
        // P6: 一次性构建后赋值，避免循环内 innerHTML += 反复重解析 DOM
        const items = (Array.isArray(saves) ? saves : []).map(s => {
            const timeStr = s.updated_at ? _formatSaveTime(s.updated_at) : '';
            const playTime = s.play_time_seconds > 0 ? _formatPlayTime(s.play_time_seconds) : '';
            return `
                <div class="save-card">
                    <button type="button" class="save-open" onclick="continueGame('${escapeAttr(s.id)}')" aria-label="继续游戏：${escapeAttr(s.name || s.script_id)}">
                        <span class="save-name">${escapeHtml(s.name || s.script_id)}</span>
                        <span class="save-info">回合 ${s.total_nodes || 0}${playTime ? ' | ' + playTime : ''}${timeStr ? ' | 保存于 ' + timeStr : ''}</span>
                    </button>
                    <div class="save-actions">
                        <button type="button" class="save-export" onclick="exportSave('${escapeAttr(s.id)}')" aria-label="导出存档：${escapeAttr(s.name || s.script_id)}" title="导出存档">&#x2B07;</button>
                        <button type="button" class="save-delete" onclick="deleteSave('${escapeAttr(s.id)}')" aria-label="删除存档：${escapeAttr(s.name || s.script_id)}" title="删除存档">&times;</button>
                    </div>
                </div>`;
        });
        container.innerHTML = items.join('');
    } catch (e) {
        if (requestId !== _gameStartSequence) return;
        console.error('Failed to load game start:', e);
    }
}

let _selectedPresetId = null;
let _selectedCustomChar = null;
let _currentScriptData = null;
let _scriptSelectionSequence = 0;
let _loadedScriptId = null;
let _gameStartSequence = 0;

async function retryScriptSelection() {
    if (document.getElementById('script-select').value) return onScriptSelected();
    return loadGameStart();
}

async function onScriptSelected() {
    const requestId = ++_scriptSelectionSequence;
    const scriptId = document.getElementById('script-select').value;
    const charArea = document.getElementById('character-select-area');
    const presetCards = document.getElementById('preset-cards');
    const customArea = document.getElementById('custom-char-area');
    const startButton = document.getElementById('btn-start-game');
    const status = document.getElementById('script-selection-status');
    const retryButton = document.getElementById('btn-retry-script');
    const oanPanel = document.getElementById('opening-authors-note');

    _selectedPresetId = null;
    _selectedCustomChar = null;
    _currentScriptData = null;
    _loadedScriptId = null;
    _selectedClassId = null;
    _selectedSkillProfs = [];
    _classSelectionSequence++;
    window._classSystemData = null;
    document.querySelector('#custom-char-area .custom-char-form')?.classList.remove('selected');
    for (const id of ['preset-cards', 'custom-char-attrs', 'custom-char-location', 'class-cards', 'skill-checkboxes', 'npc-edit-cards']) {
        document.getElementById(id).innerHTML = '';
    }
    for (const id of ['character-select-area', 'custom-char-area', 'class-select-area', 'skill-proficiency-area', 'npc-edit-area']) {
        document.getElementById(id).style.display = 'none';
    }
    if (oanPanel) oanPanel.style.display = 'none';
    startButton.disabled = true;
    if (!_gameCreationPending) {
        document.getElementById('game-start-status').textContent = '';
        document.getElementById('game-start-status').dataset.state = '';
    }
    retryButton.hidden = true;
    status.textContent = scriptId ? '正在加载剧本与角色…' : '请先选择剧本。';

    if (!scriptId) return;

    try {
        const data = await API.get(`/api/scripts/${encodeURIComponent(scriptId)}`);
        if (requestId !== _scriptSelectionSequence) return;
        const script = data.content || data;
        const presets = script.player_presets || [];
        const allowCustom = script.allow_custom_character !== false;
        const classSystem = allowCustom && (script.settings || {}).class_system;
        const classes = classSystem ? await API.get(`/api/game/classes/${encodeURIComponent(classSystem)}`) : [];
        if (requestId !== _scriptSelectionSequence) return;
        _currentScriptData = script;

        // Build attribute label lookup from script's player_character.attributes
        const pcAttrsRef = (script.player_character || {}).attributes || {};
        function _resolveAttrLabel(key) {
            const def = pcAttrsRef[key];
            if (typeof def === 'object' && def && (def.display_name || def.name)) return def.display_name || def.name;
            return _ATTR_LABELS[key] || key;
        }

        presetCards.innerHTML = '';
        if (presets.length > 0) {
            presets.forEach((preset, presetIndex) => {
                // Show preset's own attributes merged with script defaults
                const baseAttrs = pcAttrsRef;
                const presetAttrs = preset.attributes || {};
                // Gather all attribute keys (script defaults + preset overrides)
                const allKeys = Object.keys(baseAttrs);
                const attrLines = allKeys.slice(0, 4).map(attributeKey => {
                    const presetDef = presetAttrs[attributeKey];
                    const baseDef = baseAttrs[attributeKey];
                    let val;
                    if (presetDef !== undefined) {
                        val = typeof presetDef === 'object' ? (presetDef.value ?? 50) : presetDef;
                    } else {
                        val = typeof baseDef === 'object' ? (baseDef.value ?? 50) : (baseDef ?? 50);
                    }
                    return `<span class="char-card-attr"><span>${escapeHtml(_resolveAttrLabel(attributeKey))}</span><strong>${escapeHtml(String(val))}</strong></span>`;
                }).join('');

                const card = document.createElement('button');
                card.type = 'button';
                card.className = 'char-card';
                card.dataset.presetId = preset.id;
                card.setAttribute('aria-label', `选择角色：${preset.name || preset.id}`);
                card.setAttribute('aria-pressed', 'false');
                card.setAttribute('aria-describedby', `preset-details-${presetIndex}`);
                card.innerHTML = `
                    <span class="char-card-heading">
                        <span class="char-card-name">${escapeHtml(preset.name || preset.id)}</span>
                        <span class="char-card-selection" aria-hidden="true">已选择</span>
                    </span>
                    <span class="char-card-details" id="preset-details-${presetIndex}">
                        <span class="char-card-bio">${escapeHtml(preset.bio || '')}</span>
                        ${attrLines ? `<span class="char-card-attrs">${attrLines}</span>` : ''}
                        ${preset.portrait_desc ? `<span class="char-card-portrait">${escapeHtml(preset.portrait_desc)}</span>` : ''}
                    </span>`;
                card.addEventListener('click', () => selectPreset(preset.id));
                presetCards.appendChild(card);
            });
        }

        customArea.style.display = allowCustom ? '' : 'none';
        charArea.style.display = presets.length > 0 || allowCustom ? '' : 'none';

        // Populate custom character location dropdown
        if (allowCustom) {
            const locSelect = document.getElementById('custom-char-location');
            locSelect.innerHTML = '';
            for (const loc of (script.locations || [])) {
                if (loc.initially_visible !== false) {
                    const opt = document.createElement('option');
                    opt.value = loc.id;
                    opt.textContent = loc.name || loc.id;
                    locSelect.appendChild(opt);
                }
            }
            // Set default from script player_character
            const defaultLoc = (script.player_character || {}).initial_location || '';
            if (defaultLoc) locSelect.value = defaultLoc;

            // Populate attribute sliders
            const attrsDiv = document.getElementById('custom-char-attrs');
            attrsDiv.innerHTML = '';
            for (const [key, def] of Object.entries(pcAttrsRef)) {
                const val = typeof def === 'object' ? (def.value ?? 50) : def;
                const min = typeof def === 'object' ? (def.min ?? 0) : 0;
                const max = typeof def === 'object' ? (def.max ?? 100) : 100;
                const label = _resolveAttrLabel(key);
                attrsDiv.innerHTML += `
                    <div class="custom-attr-row">
                        <span class="custom-attr-label">${escapeHtml(label)}</span>
                        <input type="range" min="${min}" max="${max}" value="${val}"
                               class="custom-attr-slider" data-key="${escapeAttr(key)}"
                               oninput="this.nextElementSibling.textContent=this.value">
                        <span class="custom-attr-val">${val}</span>
                    </div>`;
            }

            // Class system: show class selection if script has class_system setting
            const classArea = document.getElementById('class-select-area');
            if (classSystem && classArea) {
                classArea.style.display = '';
                const classCards = document.getElementById('class-cards');
                classes.forEach((characterClass, classIndex) => {
                    const tags = (characterClass.narrative_tags || []).slice(0, 3).join('、');
                    const primAttrs = (characterClass.primary_attributes || []).join(' / ');
                    const card = document.createElement('button');
                    card.type = 'button';
                    card.className = 'char-card class-card';
                    card.dataset.classId = characterClass.id;
                    card.setAttribute('aria-label', `选择职业：${characterClass.name || characterClass.id}`);
                    card.setAttribute('aria-pressed', 'false');
                    card.setAttribute('aria-describedby', `class-details-${classIndex}`);
                    card.innerHTML = `
                        <span class="char-card-heading">
                            <span class="char-card-name">${escapeHtml(characterClass.name || characterClass.id)}</span>
                            <span class="char-card-selection" aria-hidden="true">已选择</span>
                        </span>
                        <span class="char-card-details" id="class-details-${classIndex}">
                            ${characterClass.name_en ? `<span class="class-card-name-en">${escapeHtml(characterClass.name_en)}</span>` : ''}
                            <span class="char-card-bio">${escapeHtml(characterClass.description || '')}</span>
                            <span class="char-card-attrs">${escapeHtml(primAttrs)}${characterClass.hit_die ? ' | d' + escapeHtml(String(characterClass.hit_die)) : ''}</span>
                            ${tags ? `<span class="char-card-portrait">${escapeHtml(tags)}</span>` : ''}
                        </span>`;
                    card.addEventListener('click', () => selectClass(characterClass.id));
                    classCards.appendChild(card);
                });
                window._classSystemData = {system: classSystem, classes};
            } else if (classArea) {
                classArea.style.display = 'none';
            }
        }
        _loadedScriptId = scriptId;
        startButton.disabled = !(script.player_character?.name || '').trim();
        if (startButton.disabled) {
            document.getElementById('game-start-status').textContent = presets.length
                ? (allowCustom ? '请选择预设角色，或创建并确认自建角色。' : '请选择预设角色。')
                : (allowCustom ? '请创建并确认自建角色。' : '剧本没有默认主角或可选角色，无法开局。');
        }
        status.textContent = '';
    } catch (e) {
        if (requestId !== _scriptSelectionSequence) return;
        console.error('Failed to load script for char select:', e);
        charArea.style.display = 'none';
        status.textContent = '剧本或职业加载失败，请重试。';
        retryButton.hidden = false;
        return;
    }

    // Show opening authors note panel when a script is selected
    if (oanPanel) {
        oanPanel.style.display = scriptId ? '' : 'none';
        _populateOpeningStylePresets();
        _renderOpeningImagePresets();
        _loadOpeningImageStyle();
    }
}

function selectPreset(presetId) {
    _selectedPresetId = presetId;
    _selectedCustomChar = null;
    document.getElementById('btn-start-game').disabled = false;
    document.getElementById('game-start-status').textContent = '';
    document.getElementById('game-start-status').dataset.state = '';
    // Visual selection
    document.querySelectorAll('#preset-cards .char-card').forEach(c => {
        c.classList.toggle('selected', c.dataset.presetId === presetId);
        c.setAttribute('aria-pressed', String(c.dataset.presetId === presetId));
    });
    // Clear custom char highlight
    document.querySelector('#custom-char-area .custom-char-form')?.classList.remove('selected');

    // Update custom char sliders to reflect the preset's attribute values (preview)
    if (_currentScriptData) {
        const presets = _currentScriptData.player_presets || [];
        const preset = presets.find(p => p.id === presetId);
        if (preset) {
            const presetAttrs = preset.attributes || {};
            const baseAttrs = (_currentScriptData.player_character || {}).attributes || {};
            document.querySelectorAll('.custom-attr-slider').forEach(slider => {
                const key = slider.dataset.key;
                const definition = presetAttrs[key] !== undefined ? presetAttrs[key] : baseAttrs[key];
                const value = typeof definition === 'object' ? (definition.value ?? 50) : (definition ?? 50);
                slider.value = value;
                slider.nextElementSibling.textContent = value;
            });
        }
    }

    // Render editable NPC cards
    _renderNpcEditCards(presetId);
}

function _renderNpcEditCards(presetId) {
    const area = document.getElementById('npc-edit-area');
    const container = document.getElementById('npc-edit-cards');
    if (!area || !container || !_currentScriptData) { if (area) area.style.display = 'none'; return; }

    const npcs = _currentScriptData.npcs || [];
    if (npcs.length === 0) { area.style.display = 'none'; return; }

    const preset = (_currentScriptData.player_presets || []).find(p => p.id === presetId);
    const overrides = {};
    if (preset && preset.npc_overrides) {
        for (const o of preset.npc_overrides) if (o.id) overrides[o.id] = o;
    }

    container.innerHTML = '';
    for (const npc of npcs) {
        const ov = overrides[npc.id] || {};
        const name = ov.name || npc.name || npc.id;
        const personality = ov.personality || npc.personality || '';
        const bio = ov.bio || npc.bio || '';
        const role = npc.role || npc.id;
        container.innerHTML += `
            <div class="npc-edit-card" data-npc-id="${escapeHtml(npc.id)}">
                <div class="npc-card-header" onclick="this.parentElement.classList.toggle('open')">
                    <span class="npc-card-label">${escapeHtml(role)} — ${escapeHtml(name)}</span>
                    <span class="npc-card-arrow">&#9662;</span>
                </div>
                <div class="npc-card-body">
                    <div class="form-row">
                        <div class="form-group form-half">
                            <label>名字:</label>
                            <input type="text" class="npc-edit-name" value="${escapeAttr(name)}">
                        </div>
                        <div class="form-group form-half">
                            <label>性格:</label>
                            <input type="text" class="npc-edit-personality" value="${escapeAttr(personality)}">
                        </div>
                    </div>
                    <div class="form-group" style="margin-top:0.3rem">
                        <label>简介:</label>
                        <textarea class="npc-edit-bio" rows="2" placeholder="NPC背景简介（可选）">${escapeHtml(bio)}</textarea>
                    </div>
                    <div class="npc-card-actions">
                        <button class="npc-delete-btn" onclick="deleteNpcCard(this)">删除</button>
                    </div>
                </div>
            </div>`;
    }
    area.style.display = '';
}

function toggleNpcEditPanel() {
    const body = document.getElementById('npc-edit-body');
    const arrow = document.getElementById('npc-edit-toggle');
    if (body.style.display === 'none') {
        body.style.display = '';
        arrow.style.transform = 'rotate(180deg)';
    } else {
        body.style.display = 'none';
        arrow.style.transform = '';
    }
}

function deleteNpcCard(btn) {
    const card = btn.closest('.npc-edit-card');
    if (!card) return;
    card.dataset.deleted = 'true';
    card.style.display = 'none';
}

let _npcAddCounter = 0;
function addCustomNpc() {
    _npcAddCounter++;
    const container = document.getElementById('npc-edit-cards');
    const newId = `custom_npc_${_npcAddCounter}_${Date.now()}`;
    const html = `
        <div class="npc-edit-card open" data-npc-id="${newId}" data-new="true">
            <div class="npc-card-header" onclick="this.parentElement.classList.toggle('open')">
                <span class="npc-card-label">新NPC #${_npcAddCounter}</span>
                <span class="npc-card-arrow">&#9662;</span>
            </div>
            <div class="npc-card-body">
                <div class="form-row">
                    <div class="form-group form-half">
                        <label>名字:</label>
                        <input type="text" class="npc-edit-name" value="" placeholder="角色名">
                    </div>
                    <div class="form-group form-half">
                        <label>身份/角色:</label>
                        <input type="text" class="npc-edit-role" value="" placeholder="如：商人、守卫">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group form-half">
                        <label>性格:</label>
                        <input type="text" class="npc-edit-personality" value="" placeholder="性格特征">
                    </div>
                    <div class="form-group form-half">
                        <label>所在地:</label>
                        <input type="text" class="npc-edit-location" value="" placeholder="初始位置（可选）">
                    </div>
                </div>
                <div class="form-group" style="margin-top:0.3rem">
                    <label>简介:</label>
                    <textarea class="npc-edit-bio" rows="2" placeholder="NPC背景简介（可选）"></textarea>
                </div>
                <div class="npc-card-actions">
                    <button class="npc-delete-btn" onclick="deleteNpcCard(this)">删除</button>
                </div>
            </div>
        </div>`;
    container.insertAdjacentHTML('beforeend', html);
    const newCard = container.lastElementChild;
    newCard.querySelector('.npc-edit-name')?.focus();
}

function _collectNpcOverrides() {
    const cards = document.querySelectorAll('.npc-edit-card');
    if (cards.length === 0) return null;
    const overrides = [];
    cards.forEach(card => {
        const id = card.dataset.npcId;
        if (card.dataset.deleted === 'true') {
            overrides.push({ id, _delete: true });
            return;
        }
        if (card.dataset.new === 'true') {
            const nameInput = card.querySelector('.npc-edit-name');
            const name = nameInput?.value.trim();
            if (!name) return;
            overrides.push({
                id,
                _new: true,
                name,
                role: card.querySelector('.npc-edit-role')?.value.trim() || '',
                personality: card.querySelector('.npc-edit-personality')?.value.trim() || '',
                bio: card.querySelector('.npc-edit-bio')?.value.trim() || '',
                location: card.querySelector('.npc-edit-location')?.value.trim() || '',
            });
            return;
        }
        const nameInput = card.querySelector('.npc-edit-name');
        const persInput = card.querySelector('.npc-edit-personality');
        const bioInput = card.querySelector('.npc-edit-bio');
        if (!nameInput || !persInput) return;
        const name = nameInput.value.trim();
        const personality = persInput.value.trim();
        const bio = bioInput?.value.trim() || '';
        if (name || personality || bio) overrides.push({ id, name, personality, bio });
    });
    return overrides.length > 0 ? overrides : null;
}

let _selectedClassId = null;
let _selectedSkillProfs = [];
let _classSelectionSequence = 0;

async function selectClass(classId) {
    const csData = window._classSystemData;
    const cls = csData?.classes.find(characterClass => characterClass.id === classId);
    if (!cls) return;
    const requestId = ++_classSelectionSequence;
    _selectedClassId = classId;
    _selectedSkillProfs = [];
    document.querySelectorAll('#class-cards .class-card').forEach(c => {
        c.classList.toggle('selected', c.dataset.classId === classId);
        c.setAttribute('aria-pressed', String(c.dataset.classId === classId));
    });
    // Show skill proficiency selection
    const area = document.getElementById('skill-proficiency-area');
    const container = document.getElementById('skill-checkboxes');
    const status = document.getElementById('skill-selection-status');
    const retryButton = document.getElementById('btn-retry-skills');
    area.style.display = 'none';
    container.innerHTML = '';
    status.textContent = '';
    retryButton.hidden = true;
    const choose = cls.skill_proficiencies_choose;
    if (!choose) return;

    const countEl = document.getElementById('skill-choice-count');
    countEl.textContent = choose.count;
    area.style.display = '';
    status.textContent = '正在加载可选技能…';

    // Fetch available skills
    try {
        const skills = await API.get(`/api/game/skills/${encodeURIComponent(csData.system)}`);
        if (requestId !== _classSelectionSequence || window._classSystemData !== csData) return;
        const availableIds = choose.from || skills.map(s => s.id);
        container.innerHTML = '';
        for (const sid of availableIds) {
            const sk = skills.find(s => s.id === sid);
            const label = sk ? `${sk.name} (${sk.parent_attribute})` : sid;
            container.innerHTML += `
                <label class="skill-checkbox-label">
                    <input type="checkbox" value="${escapeAttr(sid)}" onchange="onSkillProfChange(this, ${choose.count})">
                    ${escapeHtml(label)}
                </label>`;
        }
        status.textContent = '';
    } catch (e) {
        if (requestId !== _classSelectionSequence || window._classSystemData !== csData) return;
        console.error('Failed to load skills:', e);
        status.textContent = '技能加载失败，请重试。';
        retryButton.hidden = false;
    }
}

function onSkillProfChange(cb, maxCount) {
    const checked = document.querySelectorAll('#skill-checkboxes input:checked');
    if (checked.length > maxCount) {
        cb.checked = false;
        return;
    }
    _selectedSkillProfs = Array.from(checked).map(c => c.value);
}

function _readCustomCharacter() {
    const attrs = {};
    document.querySelectorAll('.custom-attr-slider').forEach(s => {
        attrs[s.dataset.key] = parseInt(s.value);
    });
    const character = {
        name: document.getElementById('custom-char-name').value.trim(),
        bio: document.getElementById('custom-char-bio').value.trim(),
        personality: document.getElementById('custom-char-personality').value.trim(),
        portrait_desc: document.getElementById('custom-char-portrait').value.trim(),
        long_term_goal: document.getElementById('custom-char-goal').value.trim(),
        initial_location: document.getElementById('custom-char-location').value,
        attributes: attrs,
    };
    if (_selectedClassId) {
        character.class_id = _selectedClassId;
        character.skill_proficiencies = [..._selectedSkillProfs];
    }
    return character;
}

function selectCustomChar() {
    const character = _readCustomCharacter();
    if (!character.name) { alert('请输入角色名'); return; }
    _selectedPresetId = null;
    _selectedCustomChar = character;
    document.getElementById('btn-start-game').disabled = false;
    document.getElementById('game-start-status').textContent = '';
    document.getElementById('game-start-status').dataset.state = '';
    // Visual feedback
    document.querySelectorAll('#preset-cards .char-card').forEach(card => {
        card.classList.remove('selected');
        card.setAttribute('aria-pressed', 'false');
    });
    document.querySelector('#custom-char-area .custom-char-form')?.classList.add('selected');
    // Show NPC edit cards with script defaults (no preset overrides)
    _renderNpcEditCards(null);
}

async function startNewGame() {
    const scriptId = document.getElementById('script-select').value;
    if (!scriptId || _loadedScriptId !== scriptId || _gameCreationPending) return;
    if (!_selectedPresetId && !_selectedCustomChar && !(_currentScriptData?.player_character?.name || '').trim()) {
        document.getElementById('game-start-status').textContent = '请先选择预设角色或创建并确认自建角色。';
        return;
    }
    const requestId = ++_saveLoadSequence;
    const startSequence = _gameStartSequence;
    const selectionSequence = _scriptSelectionSequence;
    const displayedSaveId = currentSaveId;
    const isCurrent = () => requestId === _saveLoadSequence && startSequence === _gameStartSequence &&
        selectionSequence === _scriptSelectionSequence && currentSaveId === displayedSaveId &&
        document.getElementById('panel-game').classList.contains('active');
    const form = document.getElementById('game-start-form');
    const button = document.getElementById('btn-start-game');
    const status = document.getElementById('game-start-status');
    const mapView = document.getElementById('game-view-select')?.value === 'map';
    const openingImgStyle = document.getElementById('opening-image-style-input')?.value.trim() || '';
    _gameCreationPending = true;
    form.disabled = true;
    button.disabled = true;
    button.textContent = '正在开局…';
    button.setAttribute('aria-busy', 'true');
    status.dataset.state = 'pending';
    status.textContent = '正在准备世界与开场，请勿重复提交。已创建的存档可从侧栏继续。';
    let result;
    try {
        const payload = { script_id: scriptId };
        if (_selectedPresetId) payload.preset_id = _selectedPresetId;
        if (_selectedCustomChar) {
            payload.custom_character = _readCustomCharacter();
            if (!payload.custom_character.name) throw new Error('请输入角色名');
        }
        const npcOverrides = _collectNpcOverrides();
        if (npcOverrides) payload.npc_overrides = npcOverrides;
        const anInput = document.getElementById('opening-an-input');
        if (anInput && anInput.value.trim()) {
            payload.authors_note = anInput.value.trim();
        }
        // Save opening image style to global config before starting game
        if (openingImgStyle) {
            const activeChip = document.querySelector('#opening-image-style-presets .style-chip.active');
            const presetLabel = activeChip ? activeChip.textContent : '';
            try { await API.put('/api/config/image/style', { preset: presetLabel, custom: openingImgStyle }); } catch (_) {}
        }
        if (!isCurrent()) return;
        if (typeof submitActionStream === 'function') payload.stream_opening = true;
        result = await API.post('/api/game/new', payload);
    } catch (e) {
        if (isCurrent()) {
            status.dataset.state = 'error';
            status.textContent = '启动游戏失败：' + e.message + '。请检查设置后重试。';
        }
    } finally {
        _gameCreationPending = false;
        form.disabled = false;
        button.disabled = !_loadedScriptId || _loadedScriptId !== document.getElementById('script-select').value ||
            (!_selectedPresetId && !_selectedCustomChar && !(_currentScriptData?.player_character?.name || '').trim());
        button.textContent = '开始游戏';
        button.setAttribute('aria-busy', 'false');
        if (status.dataset.state === 'pending') status.textContent = '';
    }
    if (!result) return;
    if (mapView) setMainMapViewForSave(result.save_id, true);
    loadSidebarSaves();
    if (!isCurrent()) return;
    currentSaveId = result.save_id;
    currentState = result.state;
    _prevState = null;
    if (result.state?._pipeline_mode) {
        if (typeof updatePipelineModeBtn === 'function') updatePipelineModeBtn(result.state._pipeline_mode);
        else _updateCachedPipelineMode(result.state._pipeline_mode);
    }
    showGameScreen(result);
    if (openingImgStyle) {
        const igImgInput = document.getElementById('image-style-input');
        if (igImgInput) igImgInput.value = openingImgStyle;
    }
    _selectedPresetId = null;
    _selectedCustomChar = null;
    if (result.opening_stream_required && typeof submitActionStream === 'function') {
        try {
            await submitActionStream({type: 'system', text: '游戏开始'});
        } catch (e) {
            if (currentSaveId === result.save_id && requestId === _saveLoadSequence) {
                alert('存档已创建，开场生成失败：' + e.message);
            }
        }
    }
}

function cancelActiveGameStream() {
    window._activeGameAction?.cancel?.();
}

async function continueGame(saveId) {
    const requestId = ++_saveLoadSequence;
    const displayedSaveId = currentSaveId;
    const pendingSwipes = [..._pendingSwipeRequests].filter(request => request.saveId === saveId);
    const waitsForSwipe = pendingSwipes.length > 0;
    try {
        if (waitsForSwipe) {
            await Promise.all(pendingSwipes.map(request => request.settled));
            if (requestId !== _saveLoadSequence || currentSaveId !== displayedSaveId) return false;
        }
        if (saveId === displayedSaveId) cancelActiveGameStream();
        const pendingKey = `tavern-pending-action:${saveId}`;
        let localPending = null;
        try { localPending = JSON.parse(sessionStorage.getItem(pendingKey) || 'null'); } catch (_) { /* storage is optional */ }
        const actionQuery = localPending?.actionId ? `&action_id=${encodeURIComponent(localPending.actionId)}` : '';
        const { state, node_id: nodeId, history: historyResp, pending_action: serverPending, action_completed: actionCompleted } = await API.get(`/api/game/${saveId}/snapshot?last=10${actionQuery}`);
        if (requestId !== _saveLoadSequence || currentSaveId !== displayedSaveId) return false;
        if (displayedSaveId !== saveId) {
            cancelActiveGameStream();
            if (_npcChatId) closeNpcChat();
            if (_npcChatRequest) {
                _npcChatRequest = null;
                document.getElementById('npc-chat-input').disabled = false;
            }
        }
        currentSaveId = saveId;
        currentState = state;
        currentNodeId = nodeId || null;
        _prevState = null;
        showGameScreen({ state, node_id: nodeId, history: historyResp.entries, historyTotal: historyResp.total, swipeIndex: historyResp.swipe_index, swipeTotal: historyResp.total_swipes }, displayedSaveId !== saveId);
        switchPanel('game');
        loadSidebarSaves();
        if (actionCompleted && localPending) {
            try { sessionStorage.removeItem(pendingKey); } catch (_) { /* storage is optional */ }
            localPending = null;
        }
        const pending = serverPending || localPending;
        if (pending?.action && pending.actionId && pending.streamId && typeof submitActionStream === 'function') {
            if (pending.baseNodeId === currentNodeId) {
                void submitActionStream(pending.action, undefined, pending);
            } else {
                try { sessionStorage.removeItem(pendingKey); } catch (_) { /* storage is optional */ }
            }
        }
        return true;
    } catch (e) {
        if (requestId === _saveLoadSequence && currentSaveId === displayedSaveId) {
            if (waitsForSwipe) isStreaming = false;
            alert('加载存档失败: ' + e.message);
        }
        return false;
    }
}

async function deleteSave(saveId) {
    if (!confirm('确定删除此存档？')) return;
    try {
        const container = document.getElementById('continue-saves');
        const scrollTop = container ? container.scrollTop : 0;
        await API.del(`/api/saves/${saveId}`);
        clearMainMapViewForSave(saveId);
        if (currentSaveId === saveId) {
            currentSaveId = null;
            currentState = null;
            currentNodeId = null;
            document.getElementById('game-start-screen').style.display = 'block';
            document.getElementById('game-play-screen').style.display = 'none';
            document.getElementById('status-panel').style.display = 'none';
            document.getElementById('status-panel-toggle').style.display = 'none';
        }
        await loadGameStart();
        loadSidebarSaves();
        if (container) container.scrollTop = scrollTop;
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

async function exportSave(saveId) {
    try {
        const resp = await fetch(`/api/saves/${saveId}/export`);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        let fname = `save_${saveId.slice(0,8)}.tavernsave.json`;
        const cd = resp.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i);
        if (m) fname = decodeURIComponent(m[1]);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = fname;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('导出失败: ' + e.message);
    }
}

async function importSaveFile(event) {
    const input = event.target;
    const file = input.files && input.files[0];
    if (!file) return;
    try {
        const fd = new FormData();
        fd.append('file', file);
        const resp = await fetch('/api/saves/import', { method: 'POST', body: fd });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
        alert(`导入成功，共 ${data.nodes_imported || 0} 个节点`);
        loadGameStart();
    } catch (e) {
        alert('导入失败: ' + e.message);
    } finally {
        input.value = '';
    }
}

function showGameScreen(data, resetScroll = false) {
    const enteringGame = document.getElementById('game-play-screen').style.display === 'none';
    _historyRenderSequence++;
    _pendingHistoryPage = null;
    if (typeof clearMainMapActivity === 'function') clearMainMapActivity();
    window._activeGameAction = null;
    isStreaming = false;  // A1: 任何进入游戏画面的路径都清除流状态
    document.getElementById('swipe-picker-modal')?.remove();
    _showGameOverlay(false);
    document.getElementById('freeform-input').disabled = false;
    document.querySelectorAll('#freeform-area button:not(#stream-toggle)').forEach(button => { button.disabled = false; });
    for (const id of ['btn-regenerate', 'btn-regen-choices', 'btn-regen-state', 'btn-continue']) {
        const button = document.getElementById(id);
        if (button) button.disabled = false;
    }
    const npcDialogueButton = document.getElementById('npc-dialogue-btn');
    if (npcDialogueButton) {
        npcDialogueButton.disabled = false;
        npcDialogueButton.textContent = 'NPC对话';
    }
    document.getElementById('game-start-screen').style.display = 'none';
    document.getElementById('game-play-screen').style.display = 'block';
    if (typeof restoreMainMapView === 'function') restoreMainMapView();
    document.getElementById('status-panel').style.display = 'flex';
    const _spToggle = document.getElementById('status-panel-toggle');
    _spToggle.style.display = 'flex';
    const _spCollapsed = localStorage.getItem('statusPanelCollapsed') === 'true';
    document.getElementById('status-panel').classList.toggle('collapsed', _spCollapsed);
    _spToggle.innerHTML = _spCollapsed ? '&#10094;' : '&#10095;';

    // Restore Author's Note UI from state metadata
    const st = data.state || currentState;
    if (st) {
        const anInput = document.getElementById('authors-note-input');
        if (anInput) anInput.value = st._authors_note || '';
        const anPos = document.getElementById('authors-note-position');
        if (anPos) { anPos.value = st._authors_note_position || 'end'; toggleANDepth(); }
        const anDepth = document.getElementById('authors-note-depth');
        if (anDepth) anDepth.value = st._authors_note_depth ?? 4;
        const negInput = document.getElementById('negative-prompt-input');
        if (negInput) negInput.value = st._negative_prompt || '';
        _logitBiasEntries = st._logit_bias || [];
        renderLogitBiasList();
        const lrInput = document.getElementById('lorebook-recursion-input');
        if (lrInput) lrInput.value = st._lorebook_max_recursion ?? 4;
    }

    if (data.narrative) {
        appendNarrative(data.narrative, null, true, data.thinking, data.agent_trace);
        renderSceneImage(data.scene_image_path ? {url: data.scene_image_path} : null);
    }
    if (data.history) {
        const content = document.getElementById('narrative-content');
        content.innerHTML = '';
        // "Load more" button if there are older turns
        const loaded = data.history.length;
        const total = data.historyTotal || loaded;
        if (loaded < total) {
            const oldestTurn = data.history[0] ? data.history[0].turn : 0;
            const loadMoreBtn = document.createElement('button');
            loadMoreBtn.className = 'load-more-btn';
            loadMoreBtn.textContent = `加载更早的回合 (已显示 ${loaded}/${total})`;
            loadMoreBtn.onclick = () => loadOlderHistory(oldestTurn);
            content.appendChild(loadMoreBtn);
        }
        data.history.forEach((h, i) => {
            const isLast = i === data.history.length - 1;
            const block = appendNarrative(h.narrative, h.action, isLast, h.thinking, h.agent_trace);
            _appendHistoryDetails(block, h, st);
            _appendQueryExchanges(block, h.query_exchanges);
            if (isLast) {
                if (h.dice_rolls && h.dice_rolls.length) renderDice(h.dice_rolls);
                if (h.check_result) showCheckResult(h.check_result);
                if (h.state_changes && h.state_changes.length) showStateChanges(h.state_changes, st);
                if (h.triggered_events && h.triggered_events.length) showTriggeredEvents(h.triggered_events);
                if (h.triggered_consequences && h.triggered_consequences.length) showTriggeredConsequences(h.triggered_consequences);
                if (h.achieved_milestones && h.achieved_milestones.length) showAchievedMilestones(h.achieved_milestones);
            }
            // Show scene image for the latest turn (fall back to most recent turn with image)
            if (isLast) {
                let imgPath = null;
                for (let k = data.history.length - 1; k >= 0; k--) {
                    if (data.history[k].scene_image_path) { imgPath = data.history[k].scene_image_path; break; }
                }
                renderSceneImage(imgPath ? {url: imgPath} : null);
            }
        });
        const last = data.history[data.history.length - 1];
        if (last) {
            currentNodeId = last.id;
            if (last.choices && last.choices.length) renderChoices(last.choices);
            if (document.getElementById('main-map-view').hidden === false &&
                typeof restoreMainMapResult === 'function') restoreMainMapResult(currentSaveId, last.narrative);
        }
    }
    if (data.choices) renderChoices(data.choices);
    _syncTurnOverflow();
    if (data.node_id) currentNodeId = data.node_id;
    if (data.dice_rolls) renderDice(data.dice_rolls);
    const st2 = data.state || currentState;
    if (st2 && st2.last_npc_speeches && st2.last_npc_speeches.length) {
        renderNpcSpeeches(st2.last_npc_speeches);
    }
    updateStatusPanel(st2);
    updateHeader();
    updateSwipeControls(data.swipeIndex || 0, data.swipeTotal || 1);
    updateTurnIndicator();

    if (typeof renderAgentTrace === 'function') {
        const lastEntry = data.history?.length ? data.history[data.history.length - 1] : null;
        renderAgentTrace(lastEntry ? lastEntry.agent_trace || null : data.agent_trace || null);
    }
    const performance = document.getElementById('perf-dashboard');
    if (performance) performance.textContent = '正在载入当前存档统计…';
    if (typeof updatePerformanceDashboard === 'function') updatePerformanceDashboard();
    if (enteringGame || resetScroll) document.getElementById('main-area').scrollTop = 0;
}

function _appendHistoryDetails(block, entry, state) {
    const rows = [];
    const add = (label, value) => rows.push(`<div><span class="history-meta-label">${label}</span>${escapeHtml(String(value))}</div>`);
    (entry.agent_trace?.plan_interactions || []).forEach(interaction => {
        add('计划', interaction.plan || '');
        (interaction.questions || []).forEach(question => {
            add('待选择', typeof question === 'string' ? question :
                `${question.question || ''}${question.options?.length ? `：${question.options.join(' / ')}` : ''}`);
        });
        if (interaction.reply != null) add('玩家选择', interaction.reply);
    });
    (entry.agent_trace?.user_injections || []).forEach(injection => {
        if (!injection.plan_reply) add('玩家插话', `${injection.label} #${injection.round}：${injection.message}`);
    });
    if (entry.agent_trace?.lore_preview_count || entry.agent_trace?.lore_preview_ids?.length) {
        add('自动知识', `激活 ${entry.agent_trace.lore_activated_count ?? entry.agent_trace.lore_preview_count ?? entry.agent_trace.lore_preview_ids.length} 条`);
    }
    if (Number.isFinite(entry.agent_trace?.rag_history_hit_count)) {
        add('自动检索', `历史 ${entry.agent_trace.rag_history_hit_count} 条 / 知识库 ${entry.agent_trace.databank_hit_count || 0} 条 / 剧本知识 ${entry.agent_trace.rag_lore_hit_count || 0} 条`);
    }
    const retrieval = entry.agent_trace?.retrieval_timings_ms || {};
    const retrievalTimings = [
        ['index_wait', '索引等待'], ['vector_query', '历史向量'], ['data_bank_query', '知识库'],
    ].filter(([key]) => Number.isFinite(retrieval[key]))
        .map(([key, label]) => `${label} ${retrieval[key]}ms`);
    if (retrievalTimings.length) add('检索耗时', retrievalTimings.join(' / '));
    if (entry.agent_trace?.agent_model_timings_ms?.length) {
        const trace = entry.agent_trace;
        add('Agent 轮次耗时', trace.agent_model_timings_ms.map((ms, index) => {
            const phase = trace.agent_model_phases?.[index];
            const chars = trace.agent_request_chars?.[index];
            const tokens = trace.agent_model_token_usage?.[index];
            const firstOutput = trace.agent_first_output_ms?.[index];
            return `${phase ? `${phase} ` : ''}${ms}ms` +
                (Number.isFinite(firstOutput) ? ` · 首输出 ${firstOutput}ms` : '') +
                (Number.isFinite(chars) ? ` · ${chars}字符` : '') +
                (Number.isFinite(tokens?.prompt) && Number.isFinite(tokens?.completion)
                    ? ` · 输入 ${tokens.prompt} / 输出 ${tokens.completion} token`
                    : trace.agent_model_token_usage ? ' · token 用量未知' : '');
        }).join(' / '));
    }
    const toolTimings = (entry.agent_trace?.rounds || [])
        .filter(step => Number.isFinite(step.duration_ms))
        .map(step => `${step.tool} ${step.duration_ms}ms`);
    if (toolTimings.length) add('工具耗时', toolTimings.join(' / '));
    (entry.agent_trace?.lore_retrievals || []).forEach(retrieval => {
        add('剧本检索', `匹配 ${retrieval.hit_count ?? (retrieval.entry_ids || []).length} 条`);
    });
    (entry.dice_rolls || []).forEach(d => add('骰子', `${d.description || d.random_item_id || '骰子'}: ${d.range_label ?? d.total ?? ''}`));
    if (entry.check_result) {
        const check = entry.check_result;
        const labels = { critical_success: '大成功', success: '成功', failure: '失败', critical_failure: '大失败' };
        add('检定', `${check.related_attribute || '检定'}: ${labels[check.outcome] || check.outcome || ''}${check.roll != null ? ` (掷出 ${check.roll})` : ''}`);
    }
    (entry.state_changes || []).forEach(change => add('状态',
        _formatSpatialChange(change, state || entry.state) || `${_resolveTargetName(change.target || '')}: ${change.old ?? '?'} → ${change.new ?? '?'}`));
    (entry.triggered_events || []).forEach(event => add('事件', typeof event === 'string' ? event : (event.name || event.title || event.description || event.event_id || '')));
    (entry.triggered_consequences || []).forEach(item => add('后果', typeof item === 'string' ? item : (item.description || item.name || item.id || '')));
    (entry.achieved_milestones || []).forEach(item => add('里程碑', typeof item === 'string' ? item : (item.name || item.id || '')));
    if (!rows.length) return;
    const details = document.createElement('details');
    details.className = 'turn-history-details';
    details.innerHTML = `<summary>本回合记录 (${rows.length})</summary><div class="turn-history-meta">${rows.join('')}</div>`;
    block.appendChild(details);
}

function _formatSpatialChange(change, state = currentState) {
    const names = state?.display_names || currentState?.display_names || {};
    const locationName = value => value == null ? '无' : (names[value] || String(value));
    if (change.target === 'visible_locations' &&
        (change.op === 'append' || change.op === 'remove') && typeof change.value === 'string') {
        const label = change.op === 'remove' ? '失去地点' :
            (Array.isArray(change.old) && change.old.includes(change.value) ? '已知地点' : '发现地点');
        return `${label}：${locationName(change.value)}`;
    }
    if (change.target === 'player.location') {
        return `位置：${locationName(change.old)} → ${locationName(change.new)}`;
    }
    return null;
}

let _turnOverflowObserver = null;

function _syncTurnOverflow() {
    const content = document.getElementById('narrative-content');
    if (!_turnOverflowObserver) {
        if (typeof ResizeObserver === 'function') {
            _turnOverflowObserver = new ResizeObserver(_syncTurnOverflow);
            _turnOverflowObserver.observe(content);
        } else if (typeof window.addEventListener === 'function') {
            window.addEventListener('resize', _syncTurnOverflow);
            _turnOverflowObserver = true;
        }
    }
    if (!content.clientWidth) return;
    content.querySelectorAll('.turn-block:not(.latest):not(.expanded)').forEach(block => {
        block.classList.add('is-truncated');
        block.classList.toggle('is-truncated', block.scrollHeight > block.clientHeight + 1);
    });
}

function createTurnBlock(text, action, isLatest, thinking, agentTrace) {
    const block = document.createElement('div');
    block.className = `turn-block${isLatest ? ' latest' : ''}`;
    if (action && (typeof action !== 'object' || action.type !== 'system')) {
        const actionDiv = document.createElement('div');
        actionDiv.className = 'turn-action';
        actionDiv.textContent = typeof action === 'object' ? (action.text || '') : action;
        block.appendChild(actionDiv);
    }
    if (thinking && !agentTrace) {
        const thinkWrap = document.createElement('div');
        thinkWrap.className = 'think-block';
        thinkWrap.innerHTML = '<div class="think-header" onclick="this.parentElement.classList.toggle(\'open\')">&#128161; AI 思考过程 <span class="toggle-arrow">&#9662;</span></div><div class="think-body">' + escapeHtml(thinking).replace(/\n/g, '<br>') + '</div>';
        block.appendChild(thinkWrap);
    }
    const textDiv = document.createElement('div');
    textDiv.className = 'narrative-text';
    textDiv.innerHTML = formatNarrativeHtml(text);
    block.appendChild(textDiv);
    return block;
}

async function loadOlderHistory(beforeTurn) {
    if (!currentSaveId || _pendingHistoryPage) return;
    const saveId = currentSaveId;
    const nodeId = currentNodeId;
    const loadSequence = _saveLoadSequence;
    const renderSequence = _historyRenderSequence;
    const content = document.getElementById('narrative-content');
    const loadMoreBtn = content.querySelector('.load-more-btn');
    const request = {};
    const isCurrent = () => currentSaveId === saveId && currentNodeId === nodeId &&
        _saveLoadSequence === loadSequence && _historyRenderSequence === renderSequence;
    _pendingHistoryPage = request;
    if (loadMoreBtn) loadMoreBtn.disabled = true;
    try {
        const resp = await API.get(`/api/game/${saveId}/history?last=10&before_turn=${beforeTurn}`);
        if (!isCurrent()) return;
        const entries = resp.entries || [];
        const total = resp.total || 0;
        if (!entries.length) return;
        const area = document.getElementById('narrative-area');
        const prevScrollHeight = area.scrollHeight;
        // Remove old load-more button
        const oldBtn = content.querySelector('.load-more-btn');
        if (oldBtn) oldBtn.remove();
        // Insert new load-more button if there are still more
        const earliestTurn = entries[0] ? entries[0].turn : 0;
        const loadedSoFar = content.querySelectorAll('.turn-block').length + entries.length;
        if (loadedSoFar < total) {
            const loadMoreBtn = document.createElement('button');
            loadMoreBtn.className = 'load-more-btn';
            loadMoreBtn.textContent = `加载更早的回合 (已显示 ${loadedSoFar}/${total})`;
            loadMoreBtn.onclick = () => loadOlderHistory(earliestTurn);
            content.prepend(loadMoreBtn);
        }
        // Prepend older turns (in order)
        const firstTurnBlock = content.querySelector('.turn-block');
        entries.forEach(h => {
            const block = createTurnBlock(h.narrative || '', h.action, false, h.thinking, h.agent_trace);
            _appendHistoryDetails(block, h);
            _appendQueryExchanges(block, h.query_exchanges);
            content.insertBefore(block, firstTurnBlock);
        });
        _syncTurnOverflow();
        // Maintain scroll position
        const newScrollHeight = area.scrollHeight;
        area.scrollTop += (newScrollHeight - prevScrollHeight);
    } catch (e) {
        if (isCurrent()) console.error('Failed to load older history:', e);
    } finally {
        if (_pendingHistoryPage === request) _pendingHistoryPage = null;
        if (currentSaveId === saveId && _historyRenderSequence === renderSequence &&
            loadMoreBtn === content.querySelector('.load-more-btn')) {
            loadMoreBtn.disabled = false;
        }
    }
}

function showTurnRecap(result, prevState, block) {
    const items = [];
    // Attribute changes
    if (result.state_changes && result.state_changes.length) {
        const parts = result.state_changes.map(c => {
            const spatial = _formatSpatialChange(c, result.state);
            if (spatial) return `<span class="recap-neutral">${escapeHtml(spatial)}</span>`;
            const tName = _resolveTargetName(c.target || '');
            if (typeof c.new === 'number' && (typeof c.old === 'number' || c.old == null)) {
                const diff = (c.new || 0) - (c.old || 0);
                if (diff === 0) return null;
                const sign = diff > 0 ? '+' : '';
                const cls = diff > 0 ? 'recap-pos' : 'recap-neg';
                return `<span class="${cls}">${escapeHtml(tName)} ${sign}${diff}</span>`;
            }
            return `<span class="recap-neutral">${escapeHtml(tName)}: ${escapeHtml(String(c.old ?? '?'))}→${escapeHtml(String(c.new ?? '?'))}</span>`;
        }).filter(Boolean);
        if (parts.length) items.push(parts.join(', '));
    }
    // Inventory diff
    if (prevState && result.state) {
        const prevInv = (prevState.inventory || []).map(i => typeof i === 'string' ? i : (i.item || i.name));
        const curInv = (result.state.inventory || []).map(i => typeof i === 'string' ? i : (i.item || i.name));
        const gained = curInv.filter(n => !prevInv.includes(n));
        const lost = prevInv.filter(n => !curInv.includes(n));
        if (gained.length) items.push(gained.map(n => `<span class="recap-pos">+${escapeHtml(n)}</span>`).join(' '));
        if (lost.length) items.push(lost.map(n => `<span class="recap-neg">-${escapeHtml(n)}</span>`).join(' '));
    }
    // Location change
    if (prevState && result.state) {
        const prevLoc = prevState.player?.location;
        const curLoc = result.state.player?.location;
        if (curLoc && curLoc !== prevLoc && !(result.state_changes || []).some(c => c.target === 'player.location')) {
            const dn = result.state.display_names || {};
            items.push(`<span class="recap-neutral">➜ ${escapeHtml(dn[curLoc] || curLoc)}</span>`);
        }
    }
    // Check result
    if (result.check_result) {
        const cr = result.check_result;
        const isSuccess = cr.outcome === 'success' || cr.outcome === 'critical_success';
        const icon = isSuccess ? '✔' : '✘';
        const cls = isSuccess ? 'recap-pos' : 'recap-neg';
        items.push(`<span class="${cls}">${icon} ${escapeHtml(cr.related_attribute || '检定')} ${isSuccess ? '成功' : '失败'}</span>`);
    }
    // Time change
    if (prevState && result.state) {
        const prevTime = prevState.game_time;
        const curTime = result.state.game_time;
        if (curTime && curTime !== prevTime) {
            const ta = result.state.time_atmosphere;
            const period = ta?.period_label || '';
            const fmtTime = (typeof formatGameTime === 'function') ? formatGameTime(curTime) : curTime;
            items.push(`<span class="recap-neutral">🕓 ${escapeHtml(fmtTime || curTime)}${period ? ' ' + escapeHtml(period) : ''}</span>`);
        }
    }
    // Milestones
    if (result.achieved_milestones && result.achieved_milestones.length) {
        const dn = result.state?.display_names || {};
        result.achieved_milestones.forEach(m => {
            const name = typeof m === 'string' ? (dn[m] || m) : (m.name || m.id || '');
            items.push(`<span class="recap-milestone">⭐ ${escapeHtml(name)}</span>`);
        });
    }
    if (!items.length || !block) return;
    const card = document.createElement('details');
    card.className = 'turn-recap';
    card.open = true;
    card.innerHTML = `<summary class="turn-recap-summary">📋 回合摘要</summary><div class="turn-recap-body">${items.join('<span class="recap-sep"> · </span>')}</div>`;
    block.appendChild(card);
    const area = document.getElementById('narrative-area');
    area.scrollTop = area.scrollHeight;
}

function appendNarrative(text, action, isLatest, thinking, agentTrace) {
    const content = document.getElementById('narrative-content');
    const block = createTurnBlock(text, action, isLatest, thinking, agentTrace);

    // Remove "latest" from previous blocks so CSS compression kicks in
    if (isLatest) {
        content.querySelectorAll('.turn-block.latest').forEach(b => b.classList.remove('latest'));
    }

    content.appendChild(block);
    _syncTurnOverflow();

    const area = document.getElementById('narrative-area');
    area.scrollTop = area.scrollHeight;
    return block;
}

function formatNarrativeHtml(text) {
    // Safety: strip <reflect>...</reflect> tags that should have been removed by backend
    text = text.replace(/<reflect>[\s\S]*?<\/reflect>/gi, '');
    const escaped = escapeHtml(text);
    const paragraphs = escaped.split(/\n\n+/);
    const htmlText = paragraphs.map(p => `<p class="narrative-para">${p.replace(/\n/g, '<br>')}</p>`).join('');
    return enhanceNarrative(htmlText);
}

function renderChoices(choices) {
    _clearSkillCheckHint();
    const area = document.getElementById('choices-area');
    area.innerHTML = '';
    choices.forEach((c, idx) => {
        const btn = document.createElement('button');
        const isLocked = c.locked === true;
        const riskClass = c.risk ? ` choice-risk-${c.risk}` : '';
        btn.className = 'choice-btn' + (isLocked ? ' choice-locked' : '') + riskClass;
        // Build subtitle: time hint + hint/lock_reason
        const timePart = c.time_hint ? formatDuration(c.time_hint) : '';
        const lockPart = isLocked ? (c.lock_reason || '条件不满足') : '';
        const hintPart = c.hint || '';
        const riskLabels = { safe: '安全', moderate: '有风险', risky: '高风险' };
        const riskPart = (!isLocked && c.risk && c.risk !== 'safe') ? riskLabels[c.risk] || '' : '';
        let subtitle = '';
        if (isLocked) {
            subtitle = lockPart + (timePart ? ` | ${timePart}` : '');
        } else {
            const parts = [];
            if (riskPart && (!hintPart || !hintPart.includes(riskPart))) parts.push(riskPart);
            if (timePart && (!hintPart || !hintPart.includes(timePart))) parts.push(timePart);
            if (hintPart) parts.push(hintPart);
            subtitle = parts.join(' | ');
        }
        const filteredPreviews = (!isLocked && c.previews && c.previews.length) ? c.previews.filter(p => {
            const pText = p.replace(/^[^\w一-鿿]+/, '').trim();
            return !subtitle || !pText || !subtitle.includes(pText);
        }) : [];
        const previewHtml = filteredPreviews.length ? `<span class="choice-previews">${filteredPreviews.map(p => `<span class="choice-preview-tag">${escapeHtml(p)}</span>`).join('')}</span>` : '';
        const riskBar = c.risk ? `<span class="choice-risk-bar choice-rbar-${c.risk}"></span>` : '';
        if (subtitle || previewHtml) {
            btn.innerHTML = `${riskBar}<span class="choice-text">${escapeHtml(c.text)}</span><span class="choice-hint${isLocked ? ' choice-lock-reason' : ''}">${isLocked ? '&#128274; ' : ''}${escapeHtml(subtitle)}</span>${previewHtml}`;
        } else {
            btn.innerHTML = `${riskBar}<span class="choice-text">${escapeHtml(c.text)}</span>`;
        }
        // Keyboard shortcut label
        if (idx < 4) {
            const badge = document.createElement('span');
            badge.className = 'choice-key-badge';
            badge.textContent = idx + 1;
            btn.prepend(badge);
        }
        if (isLocked) {
            btn.disabled = true;
            btn.title = c.lock_reason || '条件不满足';
        } else {
            btn.onclick = () => submitChoice(c.id, c.text);
        }
        area.appendChild(btn);
    });
}

function formatDuration(iso) {
    if (!iso) return '';
    const m = iso.match(/P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?/);
    if (!m) return iso;
    const d = parseInt(m[1] || 0), h = parseInt(m[2] || 0), min = parseInt(m[3] || 0);
    const parts = [];
    if (d) parts.push(`${d}天`);
    if (h) parts.push(`${h}小时`);
    if (min) parts.push(`${min}分钟`);
    return parts.length ? '约' + parts.join('') : '';
}

function renderDice(diceRolls) {
    const display = document.getElementById('dice-display');
    if (!diceRolls || diceRolls.length === 0) {
        display.style.display = 'none';
        return;
    }
    display.style.display = 'block';
    display.innerHTML = diceRolls.map(d => {
        const label = d.description || d.random_item_id || '骰子';
        const sustainedTag = d.sustained ? `<span class="dice-sustained" title="剩余${d.remaining_turns}回合">持续中</span>` : '';
        return `
        <span class="dice-result${d.sustained ? ' dice-sustained-result' : ''}">
            <span class="dice-label">${escapeHtml(label)}</span>
            ${d.range_label ? `<span class="dice-outcome">${escapeHtml(d.range_label)}</span>` : `<span class="dice-value">${d.total}</span>`}
            ${sustainedTag}
        </span>`;
    }).join('');
}

async function submitChoice(choiceId, text) {
    if (isStreaming) return;
    const action = { type: 'choice', choice_id: choiceId, text };
    if (_shouldUseStream() && typeof submitActionStream === 'function') {
        await submitActionStream(action);
    } else {
        await submitAction(action);
    }
}

async function submitFreeform(mode = '') {
    if (isStreaming) return;
    const input = document.getElementById('freeform-input');
    const text = input.value.trim();
    if (!text) return;
    const mapIntent = mode !== 'query' && typeof _mainMapPreparedIntent !== 'undefined' &&
        ['map_travel', 'map_npc_search'].includes(_mainMapPreparedIntent) &&
        !!_mainMapPreparedAction && input.value === _mainMapPreparedAction ? _mainMapPreparedIntent : '';
    const mapTargetId = mapIntent === 'map_travel' && typeof _mainMapPreparedTargetId !== 'undefined'
        ? _mainMapPreparedTargetId : '';
    input.value = '';
    if (typeof _clearMainMapPreparedAction === 'function') _clearMainMapPreparedAction();
    _clearSkillCheckHint();
    const intentHint = mode === 'query' ? 'explicit_query' :
        mapIntent || (mode === 'action' ? 'explicit_action' : '');
    const action = { type: 'freeform', text, ...(intentHint ? { intent_hint: intentHint } : {}),
        ...(mapTargetId ? { target_id: mapTargetId } : {}) };
    if (_shouldUseStream() && typeof submitActionStream === 'function') {
        await submitActionStream(action);
    } else {
        await submitAction(action);
    }
}

function handleFreeformKeydown(event) {
    if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    const mapView = document.getElementById('main-map-view');
    submitFreeform(event.ctrlKey || event.metaKey || mapView?.hidden === false ? 'action' : '');
}

async function quickAction(text, explicitAction = false) {
    if (isStreaming) return;
    const preset = text === '等待1小时，观察周围变化' || text === '找一个安全的地方休息到天亮';
    const intentHint = preset ? 'preset_action' : explicitAction ? 'explicit_action' : '';
    const action = { type: 'freeform', text, ...(intentHint ? { intent_hint: intentHint } : {}) };
    if (_shouldUseStream() && typeof submitActionStream === 'function') {
        await submitActionStream(action);
    } else {
        await submitAction(action);
    }
}

async function triggerNpcDialogue() {
    if (!currentSaveId || isStreaming) return;
    const saveId = currentSaveId;
    const actionToken = {};
    window._activeGameAction = actionToken;
    isStreaming = true;
    const input = document.getElementById('freeform-input');
    input.disabled = true;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === actionToken;
    const lockedButtons = [...document.querySelectorAll('#freeform-area button:not(#stream-toggle), .choice-btn')]
        .filter(button => !button.disabled);
    lockedButtons.forEach(button => { button.disabled = true; });
    const btn = document.getElementById('npc-dialogue-btn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '对话中…';
    }
    try {
        const res = await API.post(`/api/game/${saveId}/npc-dialogue`, { topic: '' });
        if (!isActive()) return;
        if (res.speeches && res.speeches.length > 0) {
            renderNpcSpeeches(res.speeches);
        } else {
            if (typeof showNotification === 'function') showNotification('当前场景没有NPC发言', 'info');
        }
    } catch (error) {
        if (isActive() && typeof showNotification === 'function') showNotification(error.message || 'NPC对话失败', 'error');
    } finally {
        if (isActive()) {
            window._activeGameAction = null;
            isStreaming = false;
            input.disabled = false;
            lockedButtons.forEach(button => { button.disabled = false; });
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'NPC对话';
            }
        }
    }
}

function renderNpcSpeeches(speeches) {
    const content = document.getElementById('narrative-content');
    if (!content) return;
    const container = document.createElement('div');
    container.className = 'npc-dialogue-round';
    for (const s of speeches) {
        const bubble = document.createElement('div');
        bubble.className = 'npc-speech-bubble';
        bubble.innerHTML = `<span class="npc-speech-name">${escapeHtml(s.name)}</span><span class="npc-speech-text">${escapeHtml(s.speech)}</span>`;
        container.appendChild(bubble);
    }
    content.appendChild(container);
    container.scrollIntoView({ block: 'nearest' });
}

async function _runImmediateStateAction(route, payload, presentResult, notificationType, errorMessage) {
    if (isStreaming || !currentSaveId) return;
    const saveId = currentSaveId;
    const actionToken = {};
    window._activeGameAction = actionToken;
    isStreaming = true;
    const input = document.getElementById('freeform-input');
    input.disabled = true;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === actionToken;
    let fallbackAction;
    try {
        const result = await API.post(`/api/game/${saveId}/${route}`, payload);
        if (!isActive()) return;
        fallbackAction = presentResult(result);
        if (result.success && result.state) {
            currentState = result.state;
            updateStatusPanel(currentState);
            if (typeof updateMainMapView === 'function') updateMainMapView(currentState, saveId);
        }
        if (result.success && result.state_changes) showStateChanges(result.state_changes, result.state);
    } catch (e) {
        if (isActive()) {
            const notif = _createNotifElement(notificationType, `<div class="change-negative">${escapeHtml(e.message || errorMessage)}</div>`);
            _pushNotif(notif);
        }
    } finally {
        if (isActive()) {
            window._activeGameAction = null;
            isStreaming = false;
            input.disabled = false;
        }
    }
    if (fallbackAction && currentSaveId === saveId && !isStreaming && !window._activeGameAction)
        quickAction(fallbackAction, true);
}

async function useItem(itemName) {
    return _runImmediateStateAction('use_item', { item_name: itemName }, result => {
        if (result.has_effect === false) {
            return `使用${itemName}`;
        }
        if (!result.success) {
            const notif = _createNotifElement('notif-item', `<div class="change-negative">${escapeHtml(result.message || '使用失败')}</div>`);
            _pushNotif(notif);
            return null;
        }
        const notif = _createNotifElement('notif-item', `<div class="change-positive">\u{1F48A} ${escapeHtml(result.message)}</div>`);
        _pushNotif(notif);
        return null;
    }, 'notif-item', '使用失败，请重试');
}

async function interactWith(iid, actionHint) {
    return _runImmediateStateAction('interact', { interactable_id: iid }, result => {
        if (result.has_rules === false) {
            return result.action_hint || actionHint;
        }
        if (!result.success) {
            const notif = _createNotifElement('notif-interact', `<div class="change-negative">${escapeHtml(result.message || '互动失败')}</div>`);
            _pushNotif(notif);
            return null;
        }
        const parts = [`<div class="change-positive">⚙ ${escapeHtml(result.message)}</div>`];
        if (result.revealed_locations && result.revealed_locations.length) {
            parts.push(`<div class="change-positive">\u{1F5FA} 发现新地点: ${result.revealed_locations.map(n => escapeHtml(n)).join(', ')}</div>`);
        }
        const notif = _createNotifElement('notif-interact', parts.join(''));
        _pushNotif(notif);
        return null;
    }, 'notif-interact', '互动失败，请重试');
}

const _defaultSkillKeywords = {
    extreme: {
        "暗杀":"敏捷","刺杀":"敏捷",
        "召唤":"智力","复活":"智力",
    },
    hard: {
        "偷":"敏捷","窃":"敏捷","撬":"敏捷","撬锁":"敏捷",
        "翻墙":"力量","攀爬":"力量","跳":"力量",
        "潜入":"敏捷","隐藏":"敏捷","偷袭":"敏捷",
        "逃跑":"敏捷","躲避":"敏捷",
        "攻击":"力量","打":"力量","格挡":"力量",
        "射击":"敏捷",
        "欺骗":"魅力","说谎":"魅力","伪装":"魅力",
        "威胁":"魅力","恐吓":"魅力",
        "说服":"魅力","诱惑":"魅力",
        "施法":"智力",
        "游泳":"体力","破解":"智力",
    },
    medium: {
        "调查":"智力","搜索":"智力","观察":"智力",
        "询问":"魅力","打听":"魅力","交涉":"魅力","谈判":"魅力",
        "追踪":"智力","解读":"智力",
        "修理":"智力","制作":"智力","治疗":"智力",
    },
    easy: {
        "打招呼":"魅力","闲聊":"魅力","问路":"魅力",
        "翻找":"智力","聆听":"智力","感知":"智力",
        "推":"力量","拉":"力量","搬":"力量",
    }
};

// G1: 动态读取服务端 skill_check_map，fallback 到默认值
function _getSkillKeywords() {
    const serverMap = currentState?._skill_check_map;
    if (serverMap && serverMap.hard) return serverMap;
    return _defaultSkillKeywords;
}

function showSkillCheckHint(text) {
    const hint = document.getElementById('skill-check-hint');
    if (!hint || !text) { if (hint) { hint.style.display = 'none'; hint.removeAttribute('data-loading'); } return; }
    hint.removeAttribute('data-loading');
    const lower = text.toLowerCase();
    const skillKw = _getSkillKeywords();
    let matched = null;
    const diffOrder = [
        { key: 'extreme', label: '极难', threshold: 80 },
        { key: 'hard', label: '困难', threshold: 60 },
        { key: 'medium', label: '普通', threshold: 40 },
        { key: 'easy', label: '简单', threshold: 25 },
    ];
    for (const { key, label, threshold } of diffOrder) {
        for (const [kw, attr] of Object.entries(skillKw[key] || {})) {
            if (lower.includes(kw)) { matched = { difficulty: label, attr, threshold, kw }; break; }
        }
        if (matched) break;
    }
    if (matched) {
        let attrVal = '';
        if (currentState && currentState.player && currentState.player.attributes) {
            for (const [k, v] of Object.entries(currentState.player.attributes)) {
                if (k.includes(matched.attr) || matched.attr.includes(k)) {
                    const val = typeof v === 'object' ? v.value : v;
                    attrVal = ` (当前${k}:${val})`;
                    break;
                }
            }
        }
        hint.innerHTML = `&#127922; 可能触发<strong>${matched.difficulty}</strong>技能检定 — 关联属性:${matched.attr}${attrVal} <span style="opacity:0.6">(实际由AI判定)</span>`;
        hint.style.display = 'block';
    } else {
        hint.style.display = 'none';
    }
}

function _clearSkillCheckHint() {
    const hint = document.getElementById('skill-check-hint');
    if (hint) { hint.style.display = 'none'; hint.removeAttribute('data-loading'); }
}

async function submitAction(action) {
    if (!currentSaveId || isStreaming) return;
    const saveId = currentSaveId;
    const baseNodeId = currentNodeId;
    const input = document.getElementById('freeform-input');
    const restoreMapInputFocus = action.type === 'freeform' &&
        document.getElementById('main-map-view')?.hidden === false && document.activeElement === input;
    const actionToken = {};
    window._activeGameAction = actionToken;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === actionToken;
    isStreaming = true;
    if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, actionToken, { type: 'begin', is_query: action.intent_hint === 'explicit_query' });
    _clearNotifStack();  // 新一轮开始时清除上一轮的通知
    _showGameOverlay(true);
    document.querySelectorAll('.choice-btn').forEach(b => b.disabled = true);
    input.disabled = true;

    let receivedResponse = false;
    let pendingUndo = false;
    try {
        const actionId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
        const result = await API.post(`/api/game/${saveId}/action`, {
            ...action, expected_node_id: baseNodeId, action_id: actionId,
        });
        if (!isActive()) return;
        receivedResponse = true;
        if (result.query) {
            if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, actionToken, { type: 'final', is_query: true });
            _showQueryReply(action, result.reply);
            return;
        }
        if (result.removed_node_id) {
            pendingUndo = true;
            if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, actionToken, { type: 'final', is_undo: true });
            await _showUndoneTurn(result, saveId, isActive);
            return;
        }
        const prevStateSnap = currentState ? Object.assign({}, currentState) : null;
        const _s = (label, fn) => { try { fn(); } catch(e) { console.error(`[${label}]`, e); } };
        let turnBlock;
        try {
            turnBlock = appendNarrative(result.narrative, action, true, result.thinking, result.agent_trace);
        } catch (narErr) {
            console.error('[appendNarrative]', narErr);
            const content = document.getElementById('narrative-content');
            if (content) {
                const fb = document.createElement('div');
                fb.className = 'turn-block latest';
                fb.innerHTML = '<div class="narrative-text">' + escapeHtml(result.narrative || '') + '</div>';
                content.appendChild(fb);
                turnBlock = fb;
            }
        }
        if (turnBlock) _s('historyDetails', () => _appendHistoryDetails(turnBlock, result));
        _s('showTurnRecap', () => showTurnRecap(result, prevStateSnap, turnBlock));
        _s('renderChoices', () => renderChoices(result.choices || []));
        _s('renderDice', () => renderDice(result.dice_rolls));
        _s('showStateChanges', () => showStateChanges(result.state_changes, result.state));
        _s('showTriggeredEvents', () => showTriggeredEvents(result.triggered_events));
        _s('showExpiredStates', () => showExpiredStates(result.expired_states));
        _s('showCheckResult', () => showCheckResult(result.check_result));
        _s('showTriggeredConsequences', () => showTriggeredConsequences(result.triggered_consequences));
        _s('showAchievedMilestones', () => showAchievedMilestones(result.achieved_milestones));
        _s('showMilestoneProgress', () => showMilestoneProgress(result.milestone_progress));
        _s('showThresholdEvents', () => showThresholdEvents(result.threshold_events));
        _s('showNpcAttitudeNotifications', () => showNpcAttitudeNotifications(result.npc_attitude_notifications));
        _s('showLoreHints', () => showDataBankPanel(result.activated_lore, result.databank_hits));
        _s('showWarnings', () => showWarnings(result.warnings));
        _s('showNpcInterjections', () => showNpcInterjections(result.npc_interjections));
        _s('showEmotionLabel', () => showEmotionLabel(result.emotion));
        _s('showTriggerNotifications', () => showTriggerNotifications(result.trigger_notifications));
        _s('showStoryTreeUpdates', () => showStoryTreeUpdates(result.story_tree_updates));
        _s('showNews', () => showNews(result.news));
        currentState = result.state;
        _s('updateStatusPanel', () => updateStatusPanel(currentState));
        _s('showChoiceRipples', () => showChoiceRipples(currentState));
        _s('showMemoryEchoes', () => showMemoryEchoes(currentState));
        _s('updateHeader', () => updateHeader());
        _s('updateTurnIndicator', () => updateTurnIndicator());
        _s('loadSidebarSaves', () => loadSidebarSaves());
        _s('resetSwipe', () => updateSwipeControls(0, 1));
        if (result.game_over) _s('showGameOver', () => showGameOver(result.game_over, result.game_statistics));
        // Agentic: agent trace + performance (非流式 fallback)
        if (result.agent_trace && typeof renderAgentTrace === 'function') _s('renderAgentTrace', () => renderAgentTrace(result.agent_trace));
        if (typeof updatePerformanceDashboard === 'function') _s('updatePerfDashboard', () => updatePerformanceDashboard());
        if (result.proactive_hint) {
            _s('showProactiveHint', () => {
                const block = document.querySelector('.turn-block.latest');
                if (block) {
                    const hint = document.createElement('div');
                    hint.className = 'proactive-hint';
                    hint.textContent = '💡 ' + result.proactive_hint;
                    block.appendChild(hint);
                }
            });
        }
        if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, actionToken, { type: 'final', narrative: result.narrative, news: result.news, triggered_events: result.triggered_events, state: result.state });
    } catch (e) {
        if (!isActive()) return;
        if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, actionToken, { type: pendingUndo ? 'undo_refresh_error' : 'error' });
        if (!receivedResponse && action.type === 'freeform') {
            const input = document.getElementById('freeform-input');
            if (!input.value?.trim()) input.value = action.text || '';
        }
        console.error('[submitAction]', e, e.stack);
        alert('行动失败: ' + e.message + '\n\n' + (e.stack || ''));
    } finally {
        if (isActive()) {
            window._activeGameAction = null;
            isStreaming = false;
            _showGameOverlay(false);
            input.disabled = false;
            if (restoreMapInputFocus && document.getElementById('main-map-view')?.hidden === false &&
                document.activeElement === document.body) input.focus({ preventScroll: true });
        }
    }
}

function _showGameOverlay(show) {
    const ov = document.getElementById('game-overlay');
    if (!ov) return;
    if (show && (_shouldUseStream() || document.getElementById('game-play-screen').classList.contains('map-view'))) {
        ov.style.display = 'none';
        return;
    }
    ov.style.display = show ? 'flex' : 'none';
}

function _renderLocationMap(state, dn, currentLocId) {
    const mapDiv = document.getElementById('status-location-map');
    if (!mapDiv) return;

    const visIds = state.visible_locations || [];
    if (visIds.length === 0) { mapDiv.innerHTML = ''; return; }
    const locDescs = state.location_descriptions || {};

    const W = 280, H = 220;
    const CX = W / 2, CY = H / 2;
    const R = Math.min(W, H) / 2 - 30;

    // Build node list: current location at center, others around it
    const nodes = [];
    const otherIds = visIds.filter(id => id !== currentLocId);

    // Current location node (center)
    if (currentLocId) {
        nodes.push({ id: currentLocId, label: dn[currentLocId] || currentLocId, x: CX, y: CY, current: true });
    }

    // Other locations arranged in a circle
    const count = otherIds.length;
    otherIds.forEach((id, i) => {
        const angle = (2 * Math.PI * i / count) - Math.PI / 2;
        nodes.push({
            id, label: dn[id] || id,
            x: CX + R * Math.cos(angle),
            y: CY + R * Math.sin(angle),
            current: false,
        });
    });

    // Draw SVG
    let svg = `<svg viewBox="0 0 ${W} ${H}" class="location-map">`;

    // G12: 按连接关系画边；无 connections 数据则 fallback 到全连中心
    const conns = state.location_connections || {};
    const hasConnections = Object.keys(conns).length > 0;
    const nodeById = {};
    nodes.forEach(n => { nodeById[n.id] = n; });

    if (hasConnections) {
        const drawnEdges = new Set();
        for (const [locId, targets] of Object.entries(conns)) {
            const from = nodeById[locId];
            if (!from) continue;
            for (const tgt of targets) {
                const to = nodeById[tgt];
                if (!to) continue;
                const edgeKey = [locId, tgt].sort().join('_');
                if (drawnEdges.has(edgeKey)) continue;
                drawnEdges.add(edgeKey);
                svg += `<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}" class="map-edge"/>`;
            }
        }
    } else {
        // Fallback: all locations connect to center
        const centerNode = nodes.find(n => n.current);
        if (centerNode) {
            for (const n of nodes) {
                if (n.current) continue;
                svg += `<line x1="${centerNode.x}" y1="${centerNode.y}" x2="${n.x}" y2="${n.y}" class="map-edge"/>`;
            }
        }
    }

    // Nodes — B8: 添加描述 tooltip
    for (const n of nodes) {
        const cls = n.current ? 'map-node map-node-current' : 'map-node';
        const r = n.current ? 8 : 6;
        const desc = locDescs[n.id] || '';
        const titleAttr = desc ? ` title="${escapeHtml(desc)}"` : '';
        svg += `<circle cx="${n.x}" cy="${n.y}" r="${r}" class="${cls}" data-loc-id="${escapeHtml(n.id)}"${titleAttr}/>`;
        // Label
        const ly = n.y > CY ? n.y + 16 : n.y - 12;
        svg += `<text x="${n.x}" y="${ly}" class="map-label${n.current ? ' map-label-current' : ''}">${escapeHtml(n.label)}</text>`;
    }

    svg += '</svg>';
    // B8: 当前位置描述
    const curDesc = currentLocId ? (locDescs[currentLocId] || '') : '';
    const descHtml = curDesc ? `<div class="loc-cur-desc">${escapeHtml(curDesc)}</div>` : '';
    mapDiv.innerHTML = svg + descHtml;

    // Click handler: insert movement command into freeform input
    mapDiv.querySelectorAll('.map-node:not(.map-node-current)').forEach(circle => {
        circle.style.cursor = 'pointer';
        circle.addEventListener('click', () => {
            const locId = circle.dataset.locId;
            const locName = dn[locId] || locId;
            const input = document.getElementById('freeform-input');
            if (input) {
                input.value = `前往${locName}`;
                input.focus();
            }
        });
    });

    // Zoom & pan interaction
    const svgEl = mapDiv.querySelector('svg');
    if (svgEl) _initMapInteraction(svgEl, W, H);
}

function _initMapInteraction(svg, origW, origH) {
    const orig = { x: 0, y: 0, w: origW, h: origH };
    let vb = { ...orig };
    const MIN_SCALE = 0.3, MAX_SCALE = 1.0;

    function setVB() {
        svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    }

    // Wheel zoom centered on cursor
    svg.addEventListener('wheel', e => {
        e.preventDefault();
        const rect = svg.getBoundingClientRect();
        const mx = (e.clientX - rect.left) / rect.width;
        const my = (e.clientY - rect.top) / rect.height;
        const factor = e.deltaY > 0 ? 1.15 : 0.87;
        const nw = Math.max(orig.w * MIN_SCALE, Math.min(orig.w * MAX_SCALE, vb.w * factor));
        const nh = nw * (orig.h / orig.w);
        vb.x += (vb.w - nw) * mx;
        vb.y += (vb.h - nh) * my;
        vb.w = nw; vb.h = nh;
        setVB();
    }, { passive: false });

    // Drag pan
    let dragging = false, startPt = null, dragMoved = false;
    svg.addEventListener('mousedown', e => {
        if (e.target.closest('.map-node')) return;
        dragging = true; dragMoved = false;
        startPt = { x: e.clientX, y: e.clientY, vx: vb.x, vy: vb.y };
        svg.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', e => {
        if (!dragging) return;
        dragMoved = true;
        const rect = svg.getBoundingClientRect();
        const dx = (e.clientX - startPt.x) / rect.width * vb.w;
        const dy = (e.clientY - startPt.y) / rect.height * vb.h;
        vb.x = startPt.vx - dx; vb.y = startPt.vy - dy;
        setVB();
    });
    window.addEventListener('mouseup', () => {
        if (dragging) { dragging = false; svg.style.cursor = 'grab'; }
    });

    // Double-click reset
    svg.addEventListener('dblclick', e => {
        e.preventDefault();
        vb = { ...orig }; setVB();
    });

    // Touch support: single-finger drag + two-finger pinch
    let lastTouches = null;
    svg.addEventListener('touchstart', e => {
        if (e.touches.length === 1) {
            dragging = true; dragMoved = false;
            startPt = { x: e.touches[0].clientX, y: e.touches[0].clientY, vx: vb.x, vy: vb.y };
        }
        if (e.touches.length === 2) {
            dragging = false;
            lastTouches = [
                { x: e.touches[0].clientX, y: e.touches[0].clientY },
                { x: e.touches[1].clientX, y: e.touches[1].clientY },
            ];
        }
    }, { passive: true });
    svg.addEventListener('touchmove', e => {
        if (e.touches.length === 1 && dragging && startPt) {
            dragMoved = true;
            const rect = svg.getBoundingClientRect();
            const dx = (e.touches[0].clientX - startPt.x) / rect.width * vb.w;
            const dy = (e.touches[0].clientY - startPt.y) / rect.height * vb.h;
            vb.x = startPt.vx - dx; vb.y = startPt.vy - dy;
            setVB();
        } else if (e.touches.length === 2 && lastTouches) {
            const cur = [
                { x: e.touches[0].clientX, y: e.touches[0].clientY },
                { x: e.touches[1].clientX, y: e.touches[1].clientY },
            ];
            const prevDist = Math.hypot(lastTouches[1].x - lastTouches[0].x, lastTouches[1].y - lastTouches[0].y);
            const curDist = Math.hypot(cur[1].x - cur[0].x, cur[1].y - cur[0].y);
            const factor = prevDist / curDist;
            const nw = Math.max(orig.w * MIN_SCALE, Math.min(orig.w * MAX_SCALE, vb.w * factor));
            const nh = nw * (orig.h / orig.w);
            vb.x += (vb.w - nw) * 0.5;
            vb.y += (vb.h - nh) * 0.5;
            vb.w = nw; vb.h = nh;
            setVB();
            lastTouches = cur;
        }
    }, { passive: true });
    svg.addEventListener('touchend', () => { dragging = false; lastTouches = null; });

    svg.style.cursor = 'grab';
}

// ================================================
//  NOTIFICATION MANAGER — 右下角持久通知，手动关闭或被新一轮顶替
// ================================================

const _notifStack = [];  // 当前显示的右下角通知DOM列表
const _NOTIF_MAX = 5;    // 最多同时显示几条

function _clearNotifStack() {
    _notifStack.forEach(el => el.remove());
    _notifStack.length = 0;
}

function _isMapMetaNotification(notif) {
    return notif.className.split(' ').some(name =>
        name === 'notif-story-tree' || name === 'notif-story-branch' || name === 'notif-thread' || name === 'notif-event');
}

function _clearMapMetaNotifications() {
    for (let index = _notifStack.length - 1; index >= 0; index--) {
        const notif = _notifStack[index];
        if (!_isMapMetaNotification(notif)) continue;
        notif.remove();
        _notifStack.splice(index, 1);
    }
    _repositionNotifs();
}

function _pushNotif(el, type) {
    if (typeof el === 'string') {
        el = _createNotifElement('notif-' + (type || 'info'), `<div class="change-neutral">${escapeHtml(el)}</div>`);
    }
    if (document.getElementById('game-play-screen').classList.contains('map-view') && _isMapMetaNotification(el)) return;
    // 超过上限时移除最早的
    while (_notifStack.length >= _NOTIF_MAX) {
        const oldest = _notifStack.shift();
        oldest.classList.add('notif-fade-out');
        setTimeout(() => oldest.remove(), 300);
    }
    document.body.appendChild(el);
    _notifStack.push(el);
    // 重新排列位置（从底部堆叠）
    _repositionNotifs();
}

function _repositionNotifs() {
    let bottom = 16;  // px from bottom
    if (document.getElementById('game-play-screen').classList.contains('map-view')) {
        const dockTop = document.getElementById('main-map-command-dock').getBoundingClientRect().top;
        if (dockTop > 0 && dockTop < window.innerHeight) bottom = window.innerHeight - dockTop + 8;
    }
    for (let i = _notifStack.length - 1; i >= 0; i--) {
        _notifStack[i].style.bottom = bottom + 'px';
        bottom += _notifStack[i].offsetHeight + 8;
    }
}

if (typeof ResizeObserver === 'function') {
    new ResizeObserver(_repositionNotifs).observe(document.getElementById('main-map-command-dock'));
}
if (typeof window.addEventListener === 'function') window.addEventListener('resize', _repositionNotifs);

function _createNotifElement(className, innerHTML) {
    const notif = document.createElement('div');
    notif.className = `state-change-notification ${className}`;
    const closeBtn = document.createElement('button');
    closeBtn.className = 'notif-close-btn';
    closeBtn.innerHTML = '&times;';
    closeBtn.onclick = () => {
        notif.classList.add('notif-fade-out');
        setTimeout(() => {
            notif.remove();
            const idx = _notifStack.indexOf(notif);
            if (idx >= 0) _notifStack.splice(idx, 1);
            _repositionNotifs();
        }, 300);
    };
    notif.innerHTML = innerHTML;
    notif.appendChild(closeBtn);
    return notif;
}

function _resolveNpcName(npcId, npcs, dn) {
    for (const [id, data] of Object.entries(npcs || {})) {
        if (id === npcId && typeof data === 'object') return data.display_name || data.name || npcId;
    }
    if (dn) {
        for (const [k, v] of Object.entries(dn)) {
            if (k.toLowerCase() === npcId.toLowerCase()) return v;
        }
    }
    return npcId.replace(/_/g, ' ');
}

function _resolveTargetName(target) {
    const dn = currentState?.display_names || {};
    if (dn[target]) return dn[target];
    const parts = target.split('.');
    const lastPart = parts[parts.length - 1];
    if (dn[lastPart]) return dn[lastPart];
    if (_ATTR_LABELS && _ATTR_LABELS[lastPart]) return _ATTR_LABELS[lastPart];
    const npcState = currentState?.npcs?.[lastPart];
    if (npcState && typeof npcState === 'object' && npcState.name) return npcState.name;
    return lastPart.replace(/_/g, ' ');
}

function showStateChanges(changes, state = currentState) {
    if (!changes || changes.length === 0) return;
    const html = changes.map(c => {
        const spatial = _formatSpatialChange(c, state);
        const reason = c.reason ? ` (${escapeHtml(c.reason)})` : '';
        if (spatial) return `<div class="change-neutral">${escapeHtml(spatial)}${reason}</div>`;
        const targetName = _resolveTargetName(c.target || '');
        const isNumeric = typeof c.new === 'number' && (typeof c.old === 'number' || c.old == null);
        if (isNumeric) {
            const diff = (c.new || 0) - (c.old || 0);
            const cls = diff >= 0 ? 'change-positive' : 'change-negative';
            const sign = diff >= 0 ? '+' : '';
            return `<div class="${cls}">${escapeHtml(targetName)}: ${sign}${diff}${reason}</div>`;
        }
        return `<div class="change-neutral">${escapeHtml(targetName)}: ${escapeHtml(String(c.old ?? '无'))} → ${escapeHtml(String(c.new ?? '无'))}${reason}</div>`;
    }).join('');
    const notif = _createNotifElement('notif-state', html);
    _pushNotif(notif);
}

function showTriggeredEvents(events) {
    if (!events || events.length === 0) return;
    const content = document.getElementById('narrative-content');
    const banner = document.createElement('div');
    banner.className = 'event-banner';
    const dn = currentState?.display_names || {};
    banner.innerHTML = events.map(e => {
        const id = typeof e === 'string' ? e : (e.event_id || '');
        const name = typeof e === 'object' ? (e.name || e.title || dn[id] || e.description || id) : (dn[id] || id);
        const desc = typeof e === 'object' ? (e.description || e.narrative || '') : '';
        const showDesc = desc && desc !== name;
        return `<div class="event-item"><span class="event-icon">&#128276;</span><strong>${escapeHtml(name)}</strong>${showDesc ? `<div class="event-desc">${escapeHtml(desc)}</div>` : ''}</div>`;
    }).join('');
    content.appendChild(banner);
    const area = document.getElementById('narrative-area');
    area.scrollTop = area.scrollHeight;
}

function showExpiredStates(expired) {
    if (!expired || expired.length === 0) return;
    const html = expired.map(id =>
        `<div class="change-negative">状态消失: ${escapeHtml(id)}</div>`
    ).join('');
    const notif = _createNotifElement('notif-expired', html);
    _pushNotif(notif);
}

function _getAttrVal(val) { return typeof val === 'object' ? val.value : val; }

function renderPcEventReports(state) {
    const container = document.getElementById('status-pc-reports');
    const summary = document.getElementById('status-pc-reports-summary');
    if (!container || !summary) return;
    const reports = (Array.isArray(state?.pc_event_reports) ? state.pc_event_reports : [])
        .filter(report => report && typeof report.evidence === 'string' && report.evidence.trim());
    summary.textContent = `主角听闻 · 未经核实（${reports.length}）`;
    container.innerHTML = reports.length
        ? reports.slice().reverse().map(report => {
            const source = typeof report.npc_name === 'string' && report.npc_name.trim() ? report.npc_name : '在场人物';
            const heardAt = typeof report.heard_at === 'string' && report.heard_at.trim()
                ? report.heard_at.replace('T', ' ') : '时间不详';
            return `<div class="pc-event-report"><div class="pc-event-report-meta">${escapeHtml(source)} · ${escapeHtml(heardAt)} 获知</div><div class="pc-event-report-quote">${escapeHtml(report.evidence)}</div></div>`;
        }).join('')
        : '<span class="pc-event-report-empty">暂无主角听闻的转述</span>';
}

function showPcEventReports() {
    const panel = document.getElementById('status-panel');
    if (window.matchMedia('(max-width: 600px)').matches) {
        if (!panel.classList.contains('mobile-open')) toggleStatusPanel();
    } else if (document.getElementById('game-play-screen').classList.contains('map-view') &&
        window.matchMedia('(min-width: 601px) and (max-width: 1650px)').matches) {
        if (!panel.classList.contains('map-open')) toggleStatusPanel();
    } else if (panel.classList.contains('collapsed')) {
        toggleStatusPanel();
    }
    const tab = document.querySelector('.status-tab[data-page="sp-world"]');
    if (tab) switchStatusTab(tab);
    const reports = document.querySelector('.pc-event-reports');
    if (reports) {
        reports.open = true;
        reports.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

function updateStatusPanel(state) {
    if (!state) return;
    if (state._pipeline_mode) updatePipelineModeBtn(state._pipeline_mode);
    const player = state.player || {};
    const attrs = player.attributes || {};
    const rels = player.relationships || {};
    const dn = state.display_names || {};

    // Previous state for change detection
    const prevPlayer = (_prevState?.player) || {};
    const prevAttrs = prevPlayer.attributes || {};
    const prevRels = prevPlayer.relationships || {};

    // Player info card
    const piDiv = document.getElementById('status-player-info');
    if (piDiv && player.name) {
        const bio = player.bio || '';
        const personality = player.personality || '';
        const portrait = player.portrait_desc || '';
        const details = [bio, personality, portrait].filter(Boolean);
        let personaHtml = '';
        const remaining = state._pov_switches_remaining;
        if (remaining !== undefined && remaining !== null) {
            personaHtml = `<div class="pov-switch" style="margin-top:0.3rem"><button onclick="openPovSwitchModal()" class="btn-secondary" style="font-size:0.75rem;padding:2px 8px" ${remaining<=0?'disabled':''}>${remaining > 0 ? '切换视角' : '已达上限'}</button></div>`;
        }
        piDiv.innerHTML = `<div class="player-name">${escapeHtml(player.name)}</div>${details.length ? `<div class="player-desc">${details.map(d => escapeHtml(d)).join('<br>')}</div>` : ''}${personaHtml}`;
    } else if (piDiv) {
        piDiv.innerHTML = '';
    }

    // XP / Level display
    const xpContainer = document.getElementById('status-xp-level');
    if (xpContainer) {
        const pxp = state.player_xp;
        const plv = state.player_level;
        if (pxp !== undefined && plv !== undefined) {
            const xpConf = (window._currentScript?.player_character?.xp_config) || {};
            const thresholds = xpConf.level_thresholds || [0,100,300,600,1000,1500];
            const nextTh = plv < thresholds.length ? thresholds[plv] : thresholds[thresholds.length - 1];
            const prevTh = plv > 1 && plv - 1 < thresholds.length ? thresholds[plv - 1] : 0;
            const pct = nextTh > prevTh ? Math.min(100, Math.round((pxp - prevTh) / (nextTh - prevTh) * 100)) : 100;
            xpContainer.innerHTML = `<div class="xp-level-row"><span class="xp-level-label">Lv.${plv}</span><div class="xp-bar-outer"><div class="xp-bar-inner" style="width:${pct}%"></div></div><span class="xp-val">${pxp}/${nextTh} XP</span></div>`;
            xpContainer.style.display = '';
        } else {
            xpContainer.style.display = 'none';
        }
    }

    const attrDiv = document.getElementById('status-attributes');
    const barColors = ['health', 'mood', 'study', 'health', 'mood', 'study'];
    let ci = 0;
    const skillGrowth = state.skill_growth || {};
    // P7: 一次性构建 HTML，避免循环内 innerHTML += 重解析
    const attrHtml = [];
    for (const [key, val] of Object.entries(attrs)) {
        const v = typeof val === 'object' ? val.value : val;
        const max = typeof val === 'object' ? (val.max || 100) : 100;
        const pct = Math.round((v / max) * 100);
        const label = dn[key] || _ATTR_LABELS[key] || key;
        const prevV = _getAttrVal(prevAttrs[key]);
        const hlClass = (prevV === undefined && _prevState) ? ' status-new' : (prevV !== undefined && prevV !== v ? ' status-changed' : '');
        const sg = skillGrowth[key] || skillGrowth[label];
        const sgBadge = sg && sg.level > 0 ? `<span class="skill-lv" title="熟练度Lv.${sg.level} (+${sg.bonus})">Lv.${sg.level}</span>` : '';
        attrHtml.push(`
            <div class="stat-bar${hlClass}">
                <div class="stat-label"><span>${escapeHtml(label)}${sgBadge}</span><span>${v}/${max}</span></div>
                <div class="bar-outer"><div class="bar-inner bar-${barColors[ci++ % barColors.length]}" style="width:${pct}%"></div></div>
            </div>`);
    }
    attrDiv.innerHTML = attrHtml.join('');

    document.getElementById('status-location').textContent = dn[player.location] || player.location || '未知';

    // Location access blocked notification
    const accessBlocked = state.location_access_blocked;
    const locEl = document.getElementById('status-location');
    const existingBlock = locEl?.nextElementSibling;
    if (existingBlock?.classList?.contains('location-blocked-notice')) existingBlock.remove();
    if (accessBlocked && locEl) {
        const notice = document.createElement('div');
        notice.className = 'location-blocked-notice';
        notice.textContent = `\u{1F6AB} ${accessBlocked.reason}`;
        locEl.after(notice);
    }

    // Location interactables
    const iaDiv = document.getElementById('status-interactables');
    if (iaDiv) {
        const items = state.location_interactables || [];
        if (items.length > 0) {
            iaDiv.innerHTML = '';
            items.forEach(ia => {
                const div = document.createElement('div');
                div.className = 'ia-item' + (ia.used ? ' ia-used' : '');
                div.title = ia.description || '';
                if (ia.used) {
                    div.innerHTML = `<span class="ia-icon">⚙</span><span class="ia-name">${escapeHtml(ia.name)}</span><span class="ia-used-tag">已使用</span>`;
                } else {
                    const req = ia.required_item ? ` <span class="ia-req">[需${escapeHtml(ia.required_item)}]</span>` : '';
                    div.innerHTML = `<span class="ia-icon">⚙</span><span class="ia-name">${escapeHtml(ia.name)}</span>${req}`;
                    div.style.cursor = 'pointer';
                    div.addEventListener('click', () => interactWith(ia.id, ia.action_hint || ia.name));
                }
                iaDiv.appendChild(div);
            });
        } else {
            iaDiv.innerHTML = '';
        }
    }

    // Shop button — show if current location has shops defined
    let shopBtnEl = document.getElementById('status-shop-btn');
    const hasShops = (state.shop_inventories && Object.keys(state.shop_inventories).length > 0);
    if (hasShops && player.location) {
        if (!shopBtnEl) {
            shopBtnEl = document.createElement('button');
            shopBtnEl.id = 'status-shop-btn';
            shopBtnEl.className = 'btn-secondary';
            shopBtnEl.style.cssText = 'margin-top:6px;font-size:0.82em;padding:4px 12px';
            shopBtnEl.textContent = '\u{1F6D2} 商店';
            shopBtnEl.onclick = () => openShopModal(player.location);
            document.getElementById('status-interactables')?.after(shopBtnEl);
        }
    } else if (shopBtnEl) {
        shopBtnEl.remove();
    }

    // Location map
    _renderLocationMap(state, dn, player.location);

    // Play style summary
    const psStyleDiv = document.getElementById('status-play-style');
    if (psStyleDiv) {
        const style = state.play_style_summary;
        if (style && typeof style === 'object' && style.tag) {
            const prevStyle = _prevState?.play_style_summary;
            const isNew = !prevStyle || prevStyle.tag !== style.tag;
            psStyleDiv.innerHTML = `<div class="play-style-card${isNew ? ' status-changed' : ''}"><span class="play-style-tag">${escapeHtml(style.tag)}</span>${style.description ? `<span class="play-style-desc">${escapeHtml(style.description)}</span>` : ''}</div>`;
        } else {
            psStyleDiv.innerHTML = '';
        }
    }

    // Discovery progress
    const discDiv = document.getElementById('status-discovery');
    if (discDiv) {
        const dh = state.discovery_hints;
        if (dh && dh.exploration_progress) {
            const p = dh.exploration_progress;
            const locPct = p.locations_total ? Math.round(p.locations_found / p.locations_total * 100) : 0;
            const npcPct = p.npcs_total ? Math.round(p.npcs_met / p.npcs_total * 100) : 0;
            const msPct = p.milestones_total ? Math.round(p.milestones_done / p.milestones_total * 100) : 0;
            const hints = (dh.nearby_hints || []).map(h => `<div class="disc-hint">${escapeHtml(h.hint)}</div>`).join('');
            const achHtml = (dh.achievements || []).filter(a => a.earned).map(a => `<span class="disc-ach">${escapeHtml(a.name)}</span>`).join(' ');
            discDiv.innerHTML = `
                <div class="disc-bars">
                    <div class="disc-row"><span>地点</span><div class="bar-outer"><div class="bar-inner bar-relation" style="width:${locPct}%"></div></div><span>${p.locations_found}/${p.locations_total}</span></div>
                    <div class="disc-row"><span>NPC</span><div class="bar-outer"><div class="bar-inner bar-mood" style="width:${npcPct}%"></div></div><span>${p.npcs_met}/${p.npcs_total}</span></div>
                    ${p.milestones_total ? `<div class="disc-row"><span>里程碑</span><div class="bar-outer"><div class="bar-inner bar-trust" style="width:${msPct}%"></div></div><span>${p.milestones_done}/${p.milestones_total}</span></div>` : ''}
                </div>
                ${hints ? `<div class="disc-hints">${hints}</div>` : ''}
                ${achHtml ? `<div class="disc-achievements">${achHtml}</div>` : ''}`;
        } else {
            discDiv.innerHTML = '';
        }
    }

    // Inventory
    const invDiv = document.getElementById('status-inventory');
    if (invDiv) {
        const inventory = state.inventory || [];
        const prevInv = _prevState?.inventory || [];
        const prevInvMap = {};
        prevInv.forEach(it => { prevInvMap[it.item] = it.quantity || 1; });
        if (inventory.length > 0) {
            invDiv.innerHTML = '';
            inventory.forEach(it => {
                const prevQ = prevInvMap[it.item];
                const invHl = (prevQ === undefined && _prevState) ? ' status-new' : (prevQ !== undefined && prevQ !== (it.quantity || 1) ? ' status-changed' : '');
                const div = document.createElement('div');
                div.className = 'status-kv inv-item' + invHl;
                div.title = it.description || '点击使用';
                div.style.cursor = 'pointer';
                const effectBadge = it.use_effect ? '<span class="inv-effect-badge">可用</span>' : '';
                div.innerHTML = `<span class="status-kv-key">${escapeHtml(it.item)}${effectBadge}</span><span class="status-kv-val">x${it.quantity || 1}${it.description ? ' <span class="inv-desc">' + escapeHtml(it.description) + '</span>' : ''}</span>`;
                div.addEventListener('click', () => useItem(it.item));
                invDiv.appendChild(div);
            });
        } else {
            invDiv.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">空</span>';
        }
    }

    // NPC attitudes
    const npcDiv = document.getElementById('status-npcs');
    const npcs = state.npcs || {};
    const prevNpcs = _prevState?.npcs || {};
    const playerLoc = (player.location || '').toLowerCase();
    // G11: 如果 NPC ID 集合和每个NPC的 attitude/known 都没变，跳过重建
    const _npcIds = Object.keys(npcs).sort().join(',');
    const _prevNpcIds = Object.keys(prevNpcs).sort().join(',');
    // NPC 表情/情感标签映射
    const npcEmotions = {};
    const sceneDetails = state.scene_details;
    if (sceneDetails && Array.isArray(sceneDetails.npc_expressions)) {
        for (const ex of sceneDetails.npc_expressions) {
            if (ex && ex.npc_id && ex.expression) npcEmotions[ex.npc_id] = ex.expression;
        }
    }

    const _npcUnchanged = _prevState && _npcIds === _prevNpcIds && _npcIds.length > 0 &&
        Object.entries(npcs).every(([id, d]) => {
            const p = prevNpcs[id];
            if (!p) return false;
            const att = typeof d === 'object' ? d.attitude_toward_player : d;
            const pAtt = typeof p === 'object' ? p.attitude_toward_player : p;
            const known = typeof d === 'object' ? d.known : true;
            const pKnown = typeof p === 'object' ? p.known : true;
            const loc = typeof d === 'object' ? (d.current_location || '') : '';
            const pLoc = typeof p === 'object' ? (p.current_location || '') : '';
            return att === pAtt && known === pKnown && loc === pLoc;
        }) &&
        player.location === prevPlayer.location &&
        JSON.stringify(rels) === JSON.stringify(prevRels) &&
        JSON.stringify(state.npc_relationship_depths || {}) === JSON.stringify(_prevState.npc_relationship_depths || {}) &&
        JSON.stringify(state.npc_last_seen || {}) === JSON.stringify(_prevState.npc_last_seen || {}) &&
        JSON.stringify(state.npc_contacts || {}) === JSON.stringify(_prevState.npc_contacts || {}) &&
        (state.companions || []).join(',') === (_prevState.companions || []).join(',');
    if (_npcUnchanged) {
        // Skip NPC section rebuild — but still refresh schedules for time-based location changes
        if (!document.getElementById('npc-rel-graphs')) {
            npcDiv.insertAdjacentHTML('beforeend', '<div id="npc-rel-graphs"></div>');
        }
        _loadNpcSchedules();
    } else if (Object.keys(npcs).length > 0) {
        // P8: 一次性构建 NPC HTML，避免循环内 innerHTML += 导致 O(n²) DOM 重解析
        const npcHtmlParts = [];
        const companionSet = new Set(state.companions || []);
        const npcEntries = Object.entries(npcs).filter(([id]) => id !== player.id).sort((a, b) => {
            const ac = companionSet.has(a[0]) ? 0 : 1;
            const bc = companionSet.has(b[0]) ? 0 : 1;
            return ac - bc;
        });
        for (const [npcId, npcData] of npcEntries) {
            const att = typeof npcData === 'object' ? (npcData.attitude_toward_player ?? -1) : npcData;
            const rawName = dn[npcId] || (typeof npcData === 'object' ? (npcData.name || npcId) : npcId);
            const known = (typeof npcData === 'object') ? (npcData.known !== false) : true;
            const met = (typeof npcData === 'object') ? (npcData.met !== false) : known;
            // B12: 未知NPC隐藏名字和详情
            const displayName = known ? rawName : '???';
            const unknownClass = known ? '' : ' npc-unknown';
            const attPct = Math.max(0, Math.min(100, att >= 0 ? att : 0));
            const color = att >= 60 ? 'bar-relation' : att >= 40 ? 'bar-mood' : 'bar-health';
            const showAtt = known && met && att >= 0;
            const attBar = showAtt ? `
                    <div class="bar-outer"><div class="bar-inner ${color}" style="width:${attPct}%"></div></div>` : '';
            const attText = showAtt ? `<span>${att}/100</span>` : (known && !met ? '<span style="color:var(--text-muted);font-size:0.7rem">未接触</span>' : '<span style="color:var(--text-muted);font-size:0.7rem">未知</span>');
            const prevNpc = prevNpcs[npcId];
            const prevAtt = prevNpc ? (typeof prevNpc === 'object' ? (prevNpc.attitude_toward_player ?? -1) : prevNpc) : undefined;
            const npcHlClass = (prevAtt === undefined && _prevState) ? ' status-new' : (prevAtt !== undefined && prevAtt !== att ? ' status-changed' : '');
            const npcCurLoc = (typeof npcData === 'object' ? (npcData.current_location || '') : '').toLowerCase();
            const npcHere = npcCurLoc && playerLoc && playerLoc === npcCurLoc;
            const isCompanion = (state.companions || []).includes(npcId);
            const effectiveHere = npcHere || isCompanion;
            const btnDisabled = (effectiveHere && met) ? '' : 'disabled';
            const btnAwayClass = effectiveHere ? '' : ' btn-npc-away';
            const npcLocDisplay = effectiveHere ? player.location : '';
            const mapContact = document.getElementById('main-map-view')?.hidden === false && !isCompanion && known && met &&
                !NpcPresence.hasCurrentContact(npcId, state);
            const btnTitle = !known ? '尚未认识' : (!met ? '未接触' : (effectiveHere ? (mapContact ? `寻找${rawName}，尝试接触` : `与${rawName}对话`) : '不在当前位置'));
            const locTag = known && npcLocDisplay ? `<span class="npc-loc-tag">${escapeHtml(dn[npcLocDisplay] || npcLocDisplay)}</span>` : '';
            const sighting = state.npc_last_seen?.[npcId];
            const lastSeenTag = known && !effectiveHere && sighting?.location
                ? `<span class="npc-last-seen-tag">上次目击：${escapeHtml(dn[sighting.location] || sighting.location)} · ${escapeHtml(sighting.game_time || '时间不详')}（旧情报）</span>` : '';
            const companionBadge = isCompanion ? '<span class="npc-companion-badge">同伴</span>' : '';
            const hereBadge = (npcHere && met && !isCompanion) ? '<span class="npc-here-badge">同区</span>' : '';
            const npcBio = (known && typeof npcData === 'object' && npcData.bio) ? `<div class="npc-bio" onclick="this.classList.toggle('expanded')">${escapeHtml(npcData.bio)}</div>` : '';
            const npcOpinion = (known && typeof npcData === 'object' && npcData.opinion) ? `<div class="npc-opinion">${escapeHtml(npcData.opinion)}</div>` : '';
            const npcRelDesc = (known && typeof npcData === 'object' && npcData.relationship_desc) ? `<span class="npc-rel-tag">${escapeHtml(npcData.relationship_desc)}</span>` : '';
            // Relationship depth label
            const relDepth = (state.npc_relationship_depths || {})[npcId];
            const depthTag = (known && met && relDepth && relDepth.level > 0) ? `<span class="npc-depth-tag npc-depth-${relDepth.level}">${relDepth.label}</span>` : '';

            // Inline relationship bars (merged from relationship section)
            let relBarsHtml = '';
            if (known && met) {
                const rel = rels[npcId];
                if (rel) {
                    const prevRel = prevRels[npcId];
                    if (typeof rel === 'object' && rel !== null && ('trust' in rel || 'affection' in rel || 'fear' in rel)) {
                        const t = rel.trust ?? 50, a = rel.affection ?? 50, f = rel.fear ?? 0;
                        relBarsHtml = `<div class="rel-3d-bars npc-rel-inline">
                            <div class="rel-3d-bar"><span class="rel-dim-label">信任</span><div class="bar-outer"><div class="bar-inner bar-trust" style="width:${t}%"></div></div><span class="rel-dim-val">${t}</span></div>
                            <div class="rel-3d-bar"><span class="rel-dim-label">好感</span><div class="bar-outer"><div class="bar-inner bar-affection" style="width:${a}%"></div></div><span class="rel-dim-val">${a}</span></div>
                            <div class="rel-3d-bar"><span class="rel-dim-label">畏惧</span><div class="bar-outer"><div class="bar-inner bar-fear" style="width:${f}%"></div></div><span class="rel-dim-val">${f}</span></div>
                        </div>`;
                    } else {
                        const v = typeof rel === 'object' ? (rel.value ?? 50) : rel;
                        relBarsHtml = `<div class="npc-rel-inline"><div class="bar-outer"><div class="bar-inner bar-relation" style="width:${v}%"></div></div></div>`;
                    }
                }
            }

            // NPC secret layers
            let secretsHtml = '';
            if (known && met) {
                const unlockedMap = state.npc_unlocked_secrets || {};
                const npcUnlocked = unlockedMap[npcId] || [];
                const npcSecretsDef = (state._npc_secret_counts || {})[npcId];
                const totalSecrets = npcSecretsDef || 0;
                if (totalSecrets > 0 || npcUnlocked.length > 0) {
                    const total = Math.max(totalSecrets, npcUnlocked.length);
                    const pct = total > 0 ? Math.round(npcUnlocked.length / total * 100) : 0;
                    secretsHtml = `<div class="npc-secrets"><div class="npc-secrets-bar"><span class="npc-secrets-label">\u{1F512} 了解 ${npcUnlocked.length}/${total}</span><div class="bar-outer bar-outer-sm"><div class="bar-inner bar-trust" style="width:${pct}%"></div></div></div></div>`;
                }
            }

            // Companion loyalty bar
            let loyaltyHtml = '';
            if (isCompanion) {
                const loyData = (state.companion_loyalty || {})[npcId] || {};
                const loyVal = loyData.value ?? 50;
                const loyColor = loyVal >= 60 ? 'bar-trust' : loyVal >= 30 ? 'bar-mood' : 'bar-fear';
                loyaltyHtml = `<div class="npc-loyalty"><span class="npc-loyalty-label">\u{1F91D} 忠诚 ${loyVal}/100</span><div class="bar-outer bar-outer-sm"><div class="bar-inner ${loyColor}" style="width:${loyVal}%"></div></div></div>`;
            }

            npcHtmlParts.push(`
                <div class="stat-bar npc-stat${npcHlClass}${unknownClass}${isCompanion ? ' npc-companion' : ''}">
                    <div class="stat-label">
                        <span>${escapeHtml(displayName)}${npcEmotions[npcId] ? `<span class="npc-emotion-tag">${escapeHtml(npcEmotions[npcId])}</span>` : ''}${(known && met && typeof npcData === 'object' && npcData.current_mood) ? `<span class="npc-mood-tag">${escapeHtml(npcData.current_mood)}</span>` : ''}${depthTag}${companionBadge}${hereBadge}${locTag}${lastSeenTag}</span>
                        <span class="npc-actions">
                            <button class="btn-npc-talk${btnAwayClass}" id="btn-talk-${escapeAttr(npcId)}" onclick="openNpcChat('${escapeAttr(npcId)}')" ${btnDisabled} title="${escapeAttr(btnTitle)}">${mapContact ? '寻找' : '对话'}</button>
                            ${attText}
                        </span>
                    </div>${npcBio}${npcRelDesc ? `<div class="npc-extra">${npcRelDesc}${npcOpinion}</div>` : npcOpinion}${attBar}${relBarsHtml}
                    <div class="npc-schedule" id="npc-sched-${escapeAttr(npcId)}"></div>${secretsHtml}${loyaltyHtml}
                </div>`);
        }
        npcDiv.innerHTML = npcHtmlParts.join('') + '<div id="npc-rel-graphs"></div>';
        // Fetch NPC schedules and update location-based availability
        _loadNpcSchedules();
    } else {
        npcDiv.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">无</span>';
    }

    // Show/hide NPC dialogue button based on present NPCs
    const npcDialogueBtn = document.getElementById('npc-dialogue-btn');
    if (npcDialogueBtn) {
        let presentCount = 0;
        for (const [, nd] of Object.entries(npcs)) {
            if (typeof nd === 'object') {
                const loc = (nd.location || nd.current_location || '').toLowerCase();
                if (loc && loc === playerLoc) presentCount++;
            }
        }
        npcDialogueBtn.style.display = presentCount >= 2 ? '' : 'none';
    }

    const knownRels = state.npc_relationships_known || {};
    let relHtml = '';
    if (Object.keys(knownRels).length > 0) {
        const typeColors = {'友好':'var(--success)','敌对':'var(--danger)','中立':'var(--text-muted)','竞争':'var(--warning)','暗中勾结':'var(--warning)','合作':'var(--success)','仇恨':'var(--danger)','亲密':'var(--gold)'};
        const _relA = (rel) => rel.a || rel.from || '';
        const _relB = (rel) => rel.b || rel.to || '';
        const _relName = (id) => dn[id] || (npcs[id] && typeof npcs[id] === 'object' ? npcs[id].name : null) || id;
        const _relType = (rel) => {
            if (rel.type) return rel.type;
            if (rel.trust == null && rel.affection == null && rel.fear == null) return '已知';
            const t = rel.trust ?? 50, a = rel.affection ?? 50, f = rel.fear ?? 0;
            if (a >= 70) return '亲密';
            if (t >= 70) return '友好';
            if (f >= 60) return '敌对';
            if (t <= 30) return '冷淡';
            return '中立';
        };

        const _buildGraph = (rels) => {
            const nodeSet = new Set();
            const edges = [];
            for (const [key, rel] of Object.entries(rels)) {
                const aId = _relA(rel), bId = _relB(rel);
                if (!aId || !bId) continue;
                nodeSet.add(aId);
                nodeSet.add(bId);
                const type = _relType(rel);
                edges.push({ a: aId, b: bId, type, intensity: rel.intensity });
            }
            return { nodes: [...nodeSet], edges };
        };

        const _renderRelGraph = (rels) => {
            const { nodes, edges } = _buildGraph(rels);
            if (nodes.length === 0) return '';
            const W = 300, H = Math.max(180, nodes.length * 40);
            const cx = W / 2, cy = H / 2;
            const radius = Math.min(W, H) * 0.35;
            const positions = {};
            nodes.forEach((id, i) => {
                const angle = (2 * Math.PI * i / nodes.length) - Math.PI / 2;
                positions[id] = { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
            });

            let svg = `<svg class="npc-rel-graph" viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
            // Edges
            for (const e of edges) {
                const pa = positions[e.a], pb = positions[e.b];
                if (!pa || !pb) continue;
                const color = typeColors[e.type] || 'var(--text-muted)';
                const sw = e.intensity != null ? Math.max(1, Math.min(4, e.intensity / 25)) : 1.5;
                const mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
                svg += `<line x1="${pa.x}" y1="${pa.y}" x2="${pb.x}" y2="${pb.y}" stroke="${color}" stroke-width="${sw}"/>`;
                const intLabel = e.intensity != null ? ` ${e.intensity}` : '';
                svg += `<text x="${mx}" y="${my - 4}" text-anchor="middle" fill="${color}" font-size="9">${escapeHtml(e.type)}${intLabel}</text>`;
            }
            // Nodes
            for (const id of nodes) {
                const p = positions[id];
                const name = _relName(id);
                svg += `<circle cx="${p.x}" cy="${p.y}" r="16" fill="var(--bg-tertiary)" stroke="var(--accent)" stroke-width="1.5"/>`;
                svg += `<text x="${p.x}" y="${p.y + 3}" text-anchor="middle" fill="var(--text-primary)" font-size="9" font-weight="500">${escapeHtml(name.length > 4 ? name.slice(0, 3) + '…' : name)}</text>`;
            }
            svg += '</svg>';
            return svg;
        };

        relHtml = `<div class="npc-rel-section"><div class="npc-rel-header">已知关系</div>${_renderRelGraph(knownRels)}</div>`;
    }
    const relContainer = document.getElementById('npc-rel-graphs');
    if (relContainer) relContainer.innerHTML = relHtml;

    // Faction reputation
    const repDiv = document.getElementById('status-reputation');
    const repHeader = document.getElementById('status-reputation-header');
    const factionRep = state.faction_reputation || {};
    const factionKeys = Object.keys(factionRep);
    if (factionKeys.length > 0 && repDiv) {
        if (repHeader) repHeader.style.display = '';
        const repColorMap = {'崇拜':'var(--gold)','友好':'var(--success)','中立':'var(--text-muted)','冷淡':'var(--warning)','敌对':'var(--danger)','通缉':'var(--danger)'};
        repDiv.innerHTML = factionKeys.map(fid => {
            const d = factionRep[fid];
            const val = d.value ?? 50;
            const title = d.title || '中立';
            const fname = dn[fid] || fid;
            const color = repColorMap[title] || 'var(--text-muted)';
            return `<div class="rep-item"><div class="rep-header"><span class="rep-name">${escapeHtml(fname)}</span><span class="rep-title" style="color:${color}">${title}(${val})</span></div><div class="bar-outer"><div class="bar-inner" style="width:${val}%;background:${color}"></div></div></div>`;
        }).join('');
    } else if (repDiv) {
        if (repHeader) repHeader.style.display = 'none';
        repDiv.innerHTML = '';
    }

    // Moral alignment
    const maDiv = document.getElementById('status-moral-alignment');
    const maHeader = document.getElementById('status-moral-alignment-header');
    const ma = state.moral_alignment;
    if (ma && maDiv) {
        const axes = [
            {key:'mercy_vs_cruelty', pos:'仁慈', neg:'残忍'},
            {key:'honesty_vs_deception', pos:'诚实', neg:'欺骗'},
            {key:'order_vs_chaos', pos:'秩序', neg:'混沌'},
        ];
        const hasValue = axes.some(a => (ma[a.key] || 0) !== 0);
        if (hasValue) {
            if (maHeader) maHeader.style.display = '';
            maDiv.innerHTML = axes.map(a => {
                const v = ma[a.key] || 0;
                const pct = (v + 100) / 2;
                const color = v >= 40 ? 'var(--success)' : v <= -40 ? 'var(--danger)' : 'var(--text-muted)';
                const label = v > 0 ? `+${v}` : `${v}`;
                return `<div class="ma-item"><div class="ma-header"><span class="ma-neg">${a.neg}</span><span class="ma-val" style="color:${color}">${label}</span><span class="ma-pos">${a.pos}</span></div><div class="bar-outer"><div class="bar-inner" style="width:${pct}%;background:${color}"></div></div></div>`;
            }).join('');
        } else {
            if (maHeader) maHeader.style.display = 'none';
            maDiv.innerHTML = '';
        }
    } else if (maDiv) {
        if (maHeader) maHeader.style.display = 'none';
        maDiv.innerHTML = '';
    }

    const psDiv = document.getElementById('status-persistent');
    const effSum = state.active_effects_summary || [];
    const activePs = state.active_persistent_states || [];
    const prevPs = new Set(_prevState?.active_persistent_states || []);
    const psDescs = state.persistent_state_descriptions || {};
    if (effSum.length > 0) {
        psDiv.innerHTML = effSum.map(e => {
            const isNew = e.source === 'persistent_state' && !prevPs.has(e.id) && _prevState;
            const hl = isNew ? ' status-new' : '';
            const effs = (e.effects && e.effects.length) ? `<div class="eff-tags">${e.effects.map(f => `<span class="eff-tag">${escapeHtml(f)}</span>`).join('')}</div>` : '';
            const desc = e.description ? `<div class="ps-item-desc">${escapeHtml(e.description)}</div>` : '';
            return `<div class="ps-item eff-card${hl}"><div class="ps-item-name"><span class="eff-icon">${e.icon || ''}</span>${escapeHtml(e.name)}<span class="eff-src-badge eff-src-${e.source}">${{'persistent_state':'状态','time_atmosphere':'时段','weather':'天气'}[e.source]||''}</span></div>${desc}${effs}</div>`;
        }).join('');
    } else if (activePs.length > 0) {
        psDiv.innerHTML = activePs.map(id => {
            const psHl = (!prevPs.has(id) && _prevState) ? ' status-new' : '';
            const name = dn[id] || id.replace(/_/g, ' ');
            const desc = psDescs[id] || '';
            return `<div class="ps-item${psHl}"><div class="ps-item-name">${escapeHtml(name)}</div>${desc ? `<div class="ps-item-desc">${escapeHtml(desc)}</div>` : ''}</div>`;
        }).join('');
    } else {
        psDiv.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">无</span>';
    }

    // Active deadlines
    const dlDiv = document.getElementById('status-deadlines');
    const dlHeader = document.getElementById('status-deadlines-header');
    const deadlines = (state.active_deadlines || []).filter(d => d.visible !== false);
    if (deadlines.length > 0 && dlDiv) {
        if (dlHeader) dlHeader.style.display = '';
        dlDiv.innerHTML = deadlines.map(d => {
            const tr = d.turns_remaining || 0;
            const urgency = tr <= 1 ? 'dl-urgent' : tr <= 3 ? 'dl-warn' : 'dl-normal';
            return `<div class="deadline-item ${urgency}"><span class="deadline-desc">\u{23F3} ${escapeHtml(d.description || d.id)}</span><span class="deadline-turns">剩余 ${tr} 回合</span></div>`;
        }).join('');
    } else if (dlDiv) {
        if (dlHeader) dlHeader.style.display = 'none';
        dlDiv.innerHTML = '';
    }

    // Available quests
    const questsDiv = document.getElementById('status-quests');
    const questsHeader = document.getElementById('status-quests-header');
    const quests = state.available_quests || [];
    if (quests.length > 0 && questsDiv) {
        if (questsHeader) questsHeader.style.display = '';
        questsDiv.innerHTML = quests.map(q =>
            `<div class="quest-item" title="${escapeAttr(q.reward_hint || '')}"><span class="quest-item-name">❗ ${escapeHtml(q.name || q.id)}</span>${q.trigger_hint ? `<span class="quest-item-hint">${escapeHtml(q.trigger_hint)}</span>` : ''}</div>`
        ).join('');
    } else if (questsDiv) {
        if (questsHeader) questsHeader.style.display = 'none';
        questsDiv.innerHTML = '';
    }

    // Narrative threads
    const threadsDiv = document.getElementById('status-threads');
    const threadsHeader = document.getElementById('status-threads-header');
    const threads = state.narrative_threads || [];
    const activeThreads = threads.filter(t => t.status === 'active' || t.status === 'dormant');
    if (activeThreads.length > 0 && threadsDiv) {
        if (threadsHeader) threadsHeader.style.display = '';
        threadsDiv.innerHTML = activeThreads.map(t => {
            const icon = t.status === 'active' ? '\u{1F4D6}' : '\u{1F4A4}';
            const cls = t.status === 'dormant' ? ' thread-dormant' : '';
            return `<div class="thread-item${cls}"><span class="thread-name">${icon} ${escapeHtml(t.name || t.id)}</span>${t.description ? `<span class="thread-desc">${escapeHtml(t.description)}</span>` : ''}</div>`;
        }).join('');
    } else if (threadsDiv) {
        if (threadsHeader) threadsHeader.style.display = 'none';
        threadsDiv.innerHTML = '';
    }

    // World properties
    const wpDiv = document.getElementById('status-world-props');
    if (wpDiv) {
        const wp = state.world_properties || {};
        const prevWp = _prevState?.world_properties || {};
        const wpKeys = Object.keys(wp);
        if (wpKeys.length > 0) {
            wpDiv.innerHTML = wpKeys.map(k => {
                const wpHl = (!(k in prevWp) && _prevState) ? ' status-new' : (k in prevWp && String(prevWp[k]) !== String(wp[k]) ? ' status-changed' : '');
                const wpLabel = dn[k] || k;
                return `<div class="status-kv${wpHl}"><span class="status-kv-key">${escapeHtml(wpLabel)}</span><span class="status-kv-val">${escapeHtml(String(wp[k]))}</span></div>`;
            }).join('');
        } else {
            wpDiv.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">无</span>';
        }
    }

    renderPcEventReports(state);
    const wdDiv = document.getElementById('status-world-dynamics');
    if (wdDiv) {
        const news = Array.isArray(state.news_feed) ? state.news_feed.slice(-5).reverse() : [];
        wdDiv.innerHTML = news.length > 0
            ? news.map(item => `<div class="world-dynamic-item"><span class="wd-npc">${escapeHtml(item.title || '快讯')}</span>: ${escapeHtml(item.content || '')}</div>`).join('')
            : '<span style="color:var(--text-muted);font-size:0.85rem">暂无公开动态</span>';
    }

    // Refresh tabs if active
    const logPage = document.getElementById('sp-log');
    if (logPage && logPage.classList.contains('active')) {
        loadAdventureLog();
    }
    _updateRunningStats(state);
    const cluesPage = document.getElementById('sp-clues');
    if (cluesPage && cluesPage.classList.contains('active')) {
        renderClueBoard(state);
    }
    const chroniclePage = document.getElementById('sp-chronicle');
    if (chroniclePage && chroniclePage.classList.contains('active')) {
        renderChronicle(state);
    }
    const treePage = document.getElementById('sp-tree');
    if (treePage && treePage.classList.contains('active') && currentSaveId) {
        loadWorldTree();
    }
    const storyTreePage = document.getElementById('sp-storytree');
    if (storyTreePage && storyTreePage.classList.contains('active') && currentSaveId) {
        loadStoryTree();
    }

    // NPC secret unlock notifications
    const curSecrets = state.npc_unlocked_secrets || {};
    const prevSecrets = _prevState?.npc_unlocked_secrets || {};
    if (_prevState) {
        for (const [nid, sids] of Object.entries(curSecrets)) {
            const prevSids = new Set(prevSecrets[nid] || []);
            const npcName = dn[nid] || (npcs[nid] && typeof npcs[nid] === 'object' ? npcs[nid].name : nid);
            sids.forEach(sid => {
                if (!prevSids.has(sid)) _pushNotif(`\u{1F513} 你对 ${npcName} 有了更深的了解`, 'relationship');
            });
        }
    }

    // Deadline result notifications
    const dlResults = state._deadline_results || [];
    dlResults.forEach(r => {
        if (r.outcome === 'completed') {
            _pushNotif(`\u{2705} 时限达成：${r.description}`, 'milestone');
        } else {
            _pushNotif(`\u{23F0} 时限耗尽：${r.description}`, 'consequence');
        }
    });
    if (dlResults.length) delete state._deadline_results;

    // Companion join/leave notifications
    const companionEvents = state._companion_events || [];
    companionEvents.forEach(e => {
        if (e.event === 'join') {
            _pushNotif(`\u{1F91D} ${e.name} 加入了队伍`, 'relationship');
        } else {
            _pushNotif(`\u{1F44B} ${e.name} 离开了队伍`, 'consequence');
        }
    });
    if (companionEvents.length) delete state._companion_events;

    // Narrative thread notifications
    const curThreads = state.narrative_threads || [];
    const prevThreadIds = new Set((_prevState?.narrative_threads || []).map(t => t.id));
    const prevThreadStatuses = Object.fromEntries((_prevState?.narrative_threads || []).map(t => [t.id, t.status]));
    if (_prevState) {
        curThreads.forEach(t => {
            if (!prevThreadIds.has(t.id) && t.status === 'active') {
                _pushNotif(`\u{1F4D6} 新剧情线：${t.name}`, 'thread');
            } else if (prevThreadStatuses[t.id] && prevThreadStatuses[t.id] !== 'resolved' && t.status === 'resolved') {
                _pushNotif(`\u{2705} 剧情完结：${t.name}`, 'thread');
            }
        });
    }

    // Dynamic event notifications
    const dynEvents = state._dynamic_events_this_turn || [];
    const prevDynCount = _prevState?._dynEventCount || 0;
    if (_prevState && dynEvents.length > 0) {
        dynEvents.forEach(de => {
            const cbTexts = (de.effects || []).filter(e => e.type === 'narrative_callback' && e.text).map(e => e.text);
            const label = cbTexts.length ? cbTexts.join('; ') : de.id;
            _pushNotif(_createNotifElement('notif-event', `<div class="change-new">⚡ 事件触发：${escapeHtml(label)}</div>`));
        });
    }

    // Level-up notification
    if (_prevState && state._level_up_this_turn) {
        _pushNotif(_createNotifElement('notif-levelup', `<div class="change-positive">⬆ 等级提升！当前 Lv.${state.player_level}</div>`));
    }

    // Skill tree rendering
    const stContainer = document.getElementById('status-skill-trees');
    if (stContainer) {
        const trees = window._currentScript?.player_character?.skill_trees || [];
        if (trees.length > 0) {
            const unlocked = state.unlocked_skills || [];
            const curXp = state.player_xp || 0;
            let html = '';
            for (const tree of trees) {
                html += `<details class="skill-tree-group"><summary>${escapeHtml(tree.name || tree.id)}</summary><div class="skill-tree-skills">`;
                for (const sk of (tree.skills || [])) {
                    const sid = sk.id || '';
                    const isUnlocked = unlocked.includes(sid);
                    const prereqsMet = (sk.prerequisites || []).every(p => unlocked.includes(p));
                    const canAfford = curXp >= (sk.cost || 50);
                    const cls = isUnlocked ? 'skill-unlocked' : (prereqsMet && canAfford ? 'skill-available' : 'skill-locked');
                    const btn = isUnlocked ? '<span class="skill-check">✓</span>' : (prereqsMet ? `<button class="skill-unlock-btn" onclick="doUnlockSkill('${sid}')" ${canAfford ? '' : 'disabled'}>解锁 (${sk.cost || 50} XP)</button>` : '');
                    html += `<div class="skill-node ${cls}"><span class="skill-name">${escapeHtml(sk.name || sid)}</span>${btn}</div>`;
                }
                html += '</div></details>';
            }
            stContainer.innerHTML = html;
            stContainer.style.display = '';
        } else {
            stContainer.style.display = 'none';
        }
    }

    // New clue notifications
    const curClues = state.clue_board || [];
    const prevClueCount = _prevState?._clueCount || 0;
    if (_prevState && curClues.length > prevClueCount) {
        const newClues = curClues.slice(prevClueCount);
        newClues.forEach(c => _pushNotif(`\u{1F50D} 发现线索：${c.text}`, 'clue'));
    }

    // Save only comparison-relevant fields (avoid deep-cloning adventure_log etc.)
    _prevState = {
        player: state.player ? {
            attributes: Object.assign({}, state.player.attributes),
            relationships: JSON.parse(JSON.stringify(state.player.relationships || {})),
            location: state.player.location,
        } : {},
        npcs: Object.fromEntries(Object.entries(state.npcs || {}).map(([id, d]) => [id,
            typeof d === 'object' ? { attitude_toward_player: d.attitude_toward_player, known: d.known, current_location: d.current_location, name: d.name } : d
        ])),
        inventory: (state.inventory || []).map(it => ({ item: it.item, quantity: it.quantity })),
        play_style_summary: state.play_style_summary ? { tag: state.play_style_summary.tag, description: state.play_style_summary.description } : null,
        active_persistent_states: state.active_persistent_states ? [...state.active_persistent_states] : [],
        world_properties: Object.assign({}, state.world_properties),
        npc_relationship_depths: state.npc_relationship_depths ? JSON.parse(JSON.stringify(state.npc_relationship_depths)) : {},
        npc_last_seen: state.npc_last_seen ? JSON.parse(JSON.stringify(state.npc_last_seen)) : {},
        npc_unlocked_secrets: Object.fromEntries(Object.entries(curSecrets).map(([k, v]) => [k, [...v]])),
        _clueCount: curClues.length,
        companions: state.companions ? [...state.companions] : [],
        narrative_threads: (state.narrative_threads || []).map(t => ({ id: t.id, status: t.status })),
        _dynEventCount: (state._dynamic_events_this_turn || []).length,
    };
    updateSummaryFreezeIndicator();
    _showContextualTips(state);
    if (typeof updateMainMapView === 'function') updateMainMapView(state, currentSaveId);
}

// ================================================
//  CLUE BOARD
// ================================================
let _selectedClueIds = new Set();

function renderClueBoard(state) {
    const list = document.getElementById('clue-board-list');
    const filters = document.getElementById('clue-board-filters');
    if (!list) return;
    const clues = state.clue_board || [];
    if (clues.length === 0) {
        list.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">暂未发现线索</span>';
        if (filters) filters.innerHTML = '';
        return;
    }
    const cats = [...new Set(clues.map(c => c.category))];
    if (filters) {
        filters.innerHTML = `<button class="clue-filter active" onclick="_filterClues(this,'')">\u{1F50D} 全部 (${clues.length})</button>` +
            cats.map(cat => `<button class="clue-filter" onclick="_filterClues(this,'${escapeAttr(cat)}')">${escapeHtml(cat)}</button>`).join('');
    }
    _renderClueList(clues);
}

function _filterClues(btn, cat) {
    btn.parentElement.querySelectorAll('.clue-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const clues = (currentState?.clue_board || []).filter(c => !cat || c.category === cat);
    _renderClueList(clues);
}

function _renderClueList(clues) {
    const list = document.getElementById('clue-board-list');
    list.innerHTML = clues.map(c => {
        const sel = _selectedClueIds.has(c.id) ? ' clue-selected' : '';
        const links = (c.linked_to || []).length;
        return `<div class="clue-card${sel}" data-cid="${escapeAttr(c.id)}" onclick="_toggleClue('${escapeAttr(c.id)}')">
            <div class="clue-cat">${escapeHtml(c.category)}</div>
            <div class="clue-text">${escapeHtml(c.text)}</div>
            <div class="clue-meta">${escapeHtml(c.source || '')}${links ? ` · ${links}条关联` : ''} · 第${c.turn_discovered}回合</div>
        </div>`;
    }).join('');
    _updateDeductionArea();
}

function _toggleClue(cid) {
    if (_selectedClueIds.has(cid)) _selectedClueIds.delete(cid);
    else _selectedClueIds.add(cid);
    const card = document.querySelector(`.clue-card[data-cid="${cid}"]`);
    if (card) card.classList.toggle('clue-selected');
    _updateDeductionArea();
}

function _updateDeductionArea() {
    const area = document.getElementById('clue-deduction');
    const countEl = document.getElementById('clue-selected-count');
    if (!area) return;
    area.style.display = _selectedClueIds.size >= 2 ? '' : 'none';
    if (countEl) countEl.textContent = _selectedClueIds.size;
}

async function submitDeduction() {
    if (_selectedClueIds.size < 2 || !currentSaveId) return;
    const resultEl = document.getElementById('clue-deduction-result');
    resultEl.innerHTML = '<span style="color:var(--text-muted)">推理中...</span>';
    try {
        const res = await API.post(`/api/game/${currentSaveId}/deduce`, { clue_ids: [..._selectedClueIds] });
        const cls = res.success ? 'deduction-success' : 'deduction-fail';
        resultEl.innerHTML = `<div class="${cls}">${res.success ? '\u{2705}' : '\u{274C}'} ${escapeHtml(res.message)}</div>`;
        if (res.success) {
            _selectedClueIds.clear();
            const freshState = await API.get(`/api/game/${currentSaveId}/state`);
            currentState = freshState;
            renderClueBoard(currentState);
            updateStatusPanel(currentState);
        }
    } catch (e) {
        resultEl.innerHTML = `<div class="deduction-fail">推理失败: ${escapeHtml(e.message)}</div>`;
    }
}

// ================================================
//  CHRONICLE (Player Journal)
// ================================================
let _chronicleFilter = '';

function renderChronicle(state) {
    const timeline = document.getElementById('chronicle-timeline');
    const filtersEl = document.getElementById('chronicle-filters');
    if (!timeline) return;
    const searchVal = (document.getElementById('chronicle-search')?.value || '').toLowerCase();
    const dn = state.display_names || {};

    // Merge all sources into unified entries
    const entries = [];
    const catMap = { decision: '决策', event: '剧情', consequence: '后果', milestone: '里程碑',
        threshold: '阈值', location: '地点', discovery: '发现', relationship: '人物',
        chapter: '章节', thread: '叙事线' };
    for (const log of (state.adventure_log || [])) {
        for (const ev of (log.events || [])) {
            if (ev.type === 'chapter_summary') continue;
            entries.push({ turn: log.turn, time: log.time || '', cat: catMap[ev.type] || ev.type, text: ev.text || '', src: 'log' });
        }
    }
    for (const t of (state.narrative_threads || [])) {
        entries.push({ turn: t.created_turn || 0, time: '', cat: '叙事线', text: `${t.name}：${t.description || ''}（${t.status === 'resolved' ? '已完结' : t.status === 'dormant' ? '搁置' : '进行中'}）`, src: 'thread' });
    }
    for (const c of (state.clue_board || [])) {
        entries.push({ turn: c.turn_discovered || 0, time: '', cat: '发现', text: `[${c.category}] ${c.text}`, src: 'clue' });
    }
    entries.sort((a, b) => b.turn - a.turn);

    // Collect categories
    const cats = [...new Set(entries.map(e => e.cat))];
    if (filtersEl) {
        filtersEl.innerHTML = `<button class="chronicle-filter${_chronicleFilter === '' ? ' active' : ''}" onclick="_setChronicleFilter('')">全部</button>` +
            cats.map(c => `<button class="chronicle-filter${_chronicleFilter === c ? ' active' : ''}" onclick="_setChronicleFilter('${escapeAttr(c)}')">${escapeHtml(c)}</button>`).join('');
    }

    // Filter
    let filtered = entries;
    if (_chronicleFilter) filtered = filtered.filter(e => e.cat === _chronicleFilter);
    if (searchVal) filtered = filtered.filter(e => e.text.toLowerCase().includes(searchVal));

    if (filtered.length === 0) {
        timeline.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">暂无记录</span>';
        return;
    }

    // Group by turn ranges (every 5 turns)
    const groups = {};
    for (const e of filtered) {
        const g = Math.floor(e.turn / 5) * 5;
        (groups[g] = groups[g] || []).push(e);
    }
    const sortedKeys = Object.keys(groups).map(Number).sort((a, b) => b - a);
    timeline.innerHTML = sortedKeys.map(g => {
        const items = groups[g];
        return `<div class="chronicle-turn-group"><div class="chronicle-turn-label">回合 ${g + 1}-${g + 5}</div>${items.map(e =>
            `<div class="chronicle-entry type-${e.src}"><span class="chronicle-cat">${escapeHtml(e.cat)}</span> ${escapeHtml(e.text)}</div>`
        ).join('')}</div>`;
    }).join('');
}

function _setChronicleFilter(cat) {
    _chronicleFilter = cat;
    if (currentState) renderChronicle(currentState);
}

// ================================================
//  SHOP SYSTEM
// ================================================
async function openShopModal(locationId) {
    if (!currentSaveId) return;
    const modal = document.getElementById('shop-modal');
    if (!modal) return;
    modal.style.display = 'flex';
    document.getElementById('shop-items').innerHTML = '<span style="color:var(--text-muted)">加载中...</span>';
    document.getElementById('shop-sell-items').innerHTML = '';
    try {
        const data = await API.get(`/api/game/${currentSaveId}/shop/${locationId}`);
        document.getElementById('shop-currency').textContent = `${data.currency_name}: ${data.currency}`;
        const itemsDiv = document.getElementById('shop-items');
        if (data.shops.length === 0) {
            itemsDiv.innerHTML = '<span style="color:var(--text-muted)">此处没有商店</span>';
            return;
        }
        let html = '';
        for (const shop of data.shops) {
            html += `<h4 style="margin:8px 0 4px">${escapeHtml(shop.name)}</h4>`;
            for (const it of shop.items) {
                const stockText = it.stock < 0 ? '无限' : (it.stock === 0 ? '售罄' : `${it.stock}`);
                const disabled = it.stock === 0 ? 'disabled' : '';
                html += `<div class="shop-item"><span class="shop-item-name">${escapeHtml(it.name)}</span><span class="shop-item-price">${it.price}金</span><span class="shop-item-stock">库存:${stockText}</span><button class="shop-buy-btn btn-secondary" ${disabled} onclick="doBuyItem('${escapeAttr(shop.id)}','${escapeAttr(it.item_id)}')">购买</button></div>`;
            }
        }
        itemsDiv.innerHTML = html;
        // Sell section
        const inv = currentState?.inventory || [];
        const sellDiv = document.getElementById('shop-sell-items');
        if (inv.length > 0) {
            sellDiv.innerHTML = inv.map(it =>
                `<div class="shop-item"><span class="shop-item-name">${escapeHtml(it.item)}</span><span class="shop-item-stock">×${it.quantity || 1}</span><button class="shop-sell-btn btn-secondary" onclick="doSellItem('${escapeAttr(data.shops[0]?.id || '')}','${escapeAttr(it.item)}')">出售</button></div>`
            ).join('');
        } else {
            sellDiv.innerHTML = '<span style="color:var(--text-muted)">背包为空</span>';
        }
    } catch (e) {
        document.getElementById('shop-items').innerHTML = `<span style="color:#ff5555">${escapeHtml(e.message)}</span>`;
    }
}

async function doBuyItem(shopId, itemId) {
    if (!currentSaveId) return;
    try {
        const res = await API.post(`/api/game/${currentSaveId}/shop/buy`, { shop_id: shopId, item_id: itemId });
        _pushNotif(res.message, res.success ? 'item' : 'consequence');
        if (res.state) { currentState = res.state; updateStatusPanel(currentState); }
        openShopModal(currentState?.player?.location || '');
    } catch (e) { _pushNotif(e.message, 'consequence'); }
}

async function doSellItem(shopId, itemName) {
    if (!currentSaveId) return;
    try {
        const res = await API.post(`/api/game/${currentSaveId}/shop/sell`, { shop_id: shopId, item_name: itemName });
        _pushNotif(res.message, res.success ? 'item' : 'consequence');
        if (res.state) { currentState = res.state; updateStatusPanel(currentState); }
        openShopModal(currentState?.player?.location || '');
    } catch (e) { _pushNotif(e.message, 'consequence'); }
}

async function doUnlockSkill(skillId) {
    if (!currentSaveId) return;
    try {
        const res = await API.post(`/api/game/${currentSaveId}/unlock-skill`, { skill_id: skillId });
        _pushNotif(_createNotifElement('notif-skill', `<div class="${res.success ? 'change-positive' : 'change-negative'}">${escapeHtml(res.message)}</div>`));
        if (res.state) { currentState = res.state; updateStatusPanel(currentState); }
    } catch (e) { _pushNotif(e.message, 'consequence'); }
}

function _updateRunningStats(state) {
    const el = document.getElementById('game-running-stats');
    if (!el) return;
    const turns = document.querySelectorAll('#narrative-content .turn-block').length;
    const locs = (state.visible_locations || []).length;
    const npcsMet = Object.values(state.npcs || {}).filter(n => typeof n === 'object' && (n.met !== false || n.known !== false)).length;
    const items = (state.inventory || []).length;
    const milestones = (state.achieved_milestones || []).length;
    el.innerHTML = `<span>${turns} 回合</span><span>${locs} 地点</span><span>${npcsMet} 角色</span><span>${items} 物品</span>${milestones ? `<span>${milestones} 里程碑</span>` : ''}`;
}

const _TIPS = [
    { id: 'tip_freeform', turn: 1, cond: () => true, text: '在输入框中可以自由描述你想做的任何事' },
    { id: 'tip_npc_talk', turn: 2, cond: s => Object.keys(s.npcs || {}).length > 0, text: '点击NPC旁的"对话"按钮可以进入专属对话模式' },
    { id: 'tip_inventory', turn: 3, cond: s => (s.inventory || []).length > 0, text: '点击背包中的物品可以使用它' },
    { id: 'tip_location_map', turn: 5, cond: s => (s.visible_locations || []).length > 2, text: '点击地图中的地点可以快速前往' },
    { id: 'tip_skill_growth', turn: 8, cond: s => Object.keys(s.skill_growth || {}).length > 0, text: '频繁使用的技能会升级，获得检定加成' },
];
function _showContextualTips(state) {
    const turns = document.querySelectorAll('#narrative-content .turn-block').length;
    for (const tip of _TIPS) {
        if (turns < tip.turn) continue;
        const key = 'tavern_tip_' + tip.id;
        if (localStorage.getItem(key)) continue;
        if (!tip.cond(state)) continue;
        localStorage.setItem(key, '1');
        const el = document.createElement('div');
        el.className = 'tip-toast';
        el.textContent = tip.text;
        document.getElementById('narrative-area').appendChild(el);
        setTimeout(() => el.classList.add('tip-show'), 50);
        setTimeout(() => { el.classList.remove('tip-show'); setTimeout(() => el.remove(), 400); }, 5500);
        break;
    }
}

function updateHeader() {
    if (!currentState) return;
    const rawTime = currentState.game_time || '';
    document.getElementById('game-time').textContent = formatGameTime(rawTime) || rawTime;
    const weather = currentState.current_weather || '';
    const weatherEmojis = { '晴朗': '☀', '多云': '☁', '阴雨': '🌧', '极端天气（暴雨/大雪）': '⛈' };
    document.getElementById('weather-badge').textContent = weatherEmojis[weather] ? `${weatherEmojis[weather]} ${weather}` : weather;
    // Time-of-day atmosphere icon
    const atmo = currentState.time_atmosphere;
    const timeEl = document.getElementById('game-time');
    if (atmo && timeEl) {
        const icons = { dawn: '🌅', morning: '☀', noon: '🌞', afternoon: '🌤', dusk: '🌇', night: '🌙', late_night: '🌑' };
        const icon = icons[atmo.period] || '';
        if (icon) timeEl.textContent = icon + ' ' + timeEl.textContent;
    }
    const pacingBadge = document.getElementById('pacing-badge');
    if (pacingBadge) {
        const pacing = currentState.pacing_state;
        if (pacing && typeof pacing.tension === 'number') {
            const t = pacing.tension;
            const label = t >= 70 ? '\u{1F525}紧张' : t <= 30 ? '\u{1F319}平静' : '\u{2696}平稳';
            const trends = { rising: '↗', falling: '↘', stable: '→' };
            const trend = trends[pacing.trend] || '';
            pacingBadge.textContent = label + (trend ? ' ' + trend : '');
            pacingBadge.style.display = '';
        } else {
            pacingBadge.style.display = 'none';
        }
    }
}

// ================================================
//  AI PROFILES
// ================================================

async function loadProfiles() {
    try {
        const profiles = await API.get('/api/config/profiles');
        const container = document.getElementById('profiles-list');
        container.innerHTML = '';

        if (!profiles || profiles.length === 0) {
            container.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem">暂无配置，请添加你的第一组AI配置。</p>';
            return;
        }

        profiles.forEach(p => {
            const isActive = p.is_active ? ' is-active' : '';
            const badge = p.is_active ? '<span class="profile-badge">当前</span>' : '';
            const sm = p.stage_models || {};
            const smEntries = Object.entries(sm).filter(([k, v]) => v);
            const smLine = smEntries.length
                ? `<div class="profile-detail" style="color:var(--text-muted);font-size:0.78rem">阶段覆盖: ${smEntries.map(([k, v]) => escapeHtml(k) + '=' + escapeHtml(v)).join(', ')}</div>`
                : '';
            container.innerHTML += `
                <div class="profile-card${isActive}">
                    <div class="profile-info">
                        <div class="profile-name">${escapeHtml(p.name)}${badge}</div>
                        <div class="profile-detail">${escapeHtml(p.provider_type)} | ${escapeHtml(p.model)} ${p.base_url ? '| ' + escapeHtml(p.base_url) : ''}</div>
                        ${smLine}
                    </div>
                    <div class="profile-actions">
                        ${!p.is_active ? `<button class="btn-primary" onclick="activateProfile(${p.id})">启用</button>` : ''}
                        <button class="btn-secondary" onclick="editProfile(${p.id})">编辑</button>
                        <button class="btn-secondary" onclick="testProfile(${p.id})">测试</button>
                        <button class="btn-secondary" onclick="deleteProfile(${p.id})">删除</button>
                    </div>
                </div>`;
        });
    } catch (e) {
        console.error('Failed to load profiles:', e);
    }
}

async function loadProfilesDropdown() {
    try {
        const profiles = await API.get('/api/config/profiles');
        const select = document.getElementById('quick-profile-select');
        select.innerHTML = '<option value="">无AI配置</option>';
        (profiles || []).forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.name;
            if (p.is_active) opt.selected = true;
            select.appendChild(opt);
        });
    } catch (e) {
        console.log('Profiles not loaded yet');
    }
}

async function quickSwitchProfile(profileId) {
    if (!profileId) return;
    try {
        await API.post(`/api/config/profiles/${profileId}/activate`);
        showConfigStatus('已切换AI配置', true);
        loadProfilesDropdown();
        // Refresh profiles list if settings panel is open
        if (document.getElementById('panel-settings').classList.contains('active')) {
            loadProfiles();
        }
    } catch (e) {
        showConfigStatus('切换失败: ' + e.message, false);
    }
}

async function activateProfile(profileId) {
    try {
        await API.post(`/api/config/profiles/${profileId}/activate`);
        showConfigStatus('已切换', true);
        loadProfiles();
        loadProfilesDropdown();
    } catch (e) {
        showConfigStatus('切换失败: ' + e.message, false);
    }
}

async function testProfile(profileId) {
    showConfigStatus('正在测试连接...', true);
    try {
        const result = await API.post(`/api/config/profiles/${profileId}/test`);
        if (result.success) {
            showConfigStatus(`连接成功 (${result.provider} / ${result.model})`, true);
        } else {
            showConfigStatus(`连接失败: ${result.error}`, false);
        }
    } catch (e) {
        showConfigStatus('测试失败: ' + e.message, false);
    }
}

async function deleteProfile(profileId) {
    if (!confirm('确定删除此配置？')) return;
    try {
        await API.del(`/api/config/profiles/${profileId}`);
        loadProfiles();
        loadProfilesDropdown();
    } catch (e) {
        showConfigStatus('删除失败: ' + e.message, false);
    }
}

// Track editing state
let _editingProfileId = null;

async function editProfile(profileId) {
    try {
        const profiles = await API.get('/api/config/profiles');
        const p = profiles.find(x => x.id === profileId);
        if (!p) return;

        _editingProfileId = profileId;
        document.getElementById('profile-name').value = p.name || '';
        document.getElementById('profile-provider-type').value = p.provider_type || 'openai_compatible';
        document.getElementById('profile-api-key').value = '';
        document.getElementById('profile-api-key').placeholder = p.has_api_key ? '留空保持不变' : 'sk-你的密钥';
        document.getElementById('profile-base-url').value = p.base_url || '';
        document.getElementById('profile-model').value = p.model || '';
        document.getElementById('profile-max-tokens').value = p.max_tokens || 2048;
        document.getElementById('profile-review-max-tokens').value = p.review_max_tokens ?? 500;
        document.getElementById('profile-review-retry-max-tokens').value = p.review_retry_max_tokens ?? 1500;

        // Fill stage_models overrides
        const sm = p.stage_models || {};
        for (const stage of ['narrative', 'choices', 'state', 'review', 'summary', 'knowledge_graph']) {
            const el = document.getElementById('profile-sm-' + stage);
            if (el) el.value = sm[stage] || '';
        }

        // Update form UI for edit mode
        const saveBtn = document.querySelector('#panel-settings .btn-primary');
        if (saveBtn) saveBtn.textContent = '更新配置';
        const formTitle = document.querySelector('#panel-settings h4:nth-of-type(2)');
        if (formTitle) formTitle.textContent = `编辑配置: ${p.name}`;

        // Scroll form into view
        document.getElementById('profile-name').scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
        showConfigStatus('加载失败: ' + e.message, false);
    }
}

function _resetProfileForm() {
    _editingProfileId = null;
    document.getElementById('profile-name').value = '';
    document.getElementById('profile-api-key').value = '';
    document.getElementById('profile-api-key').placeholder = 'sk-你的密钥';
    document.getElementById('profile-base-url').value = '';
    document.getElementById('profile-model').value = 'gpt-4o';
    document.getElementById('profile-max-tokens').value = '2048';
    document.getElementById('profile-review-max-tokens').value = '500';
    document.getElementById('profile-review-retry-max-tokens').value = '1500';
    for (const stage of ['narrative', 'choices', 'state', 'review', 'summary', 'knowledge_graph']) {
        const el = document.getElementById('profile-sm-' + stage);
        if (el) el.value = '';
    }
    const saveBtn = document.querySelector('#panel-settings .btn-primary');
    if (saveBtn) saveBtn.textContent = '保存配置';
    const formTitle = document.querySelector('#panel-settings h4:nth-of-type(2)');
    if (formTitle) formTitle.textContent = '添加新配置';
}

async function saveNewProfile() {
    const name = document.getElementById('profile-name').value.trim();
    const providerType = document.getElementById('profile-provider-type').value;
    const apiKey = document.getElementById('profile-api-key').value.trim();
    const baseUrl = document.getElementById('profile-base-url').value.trim();
    const model = document.getElementById('profile-model').value.trim();
    const maxTokens = parseInt(document.getElementById('profile-max-tokens').value) || 2048;
    const reviewMaxTokens = Number(document.getElementById('profile-review-max-tokens').value);
    const reviewRetryMaxTokens = Number(document.getElementById('profile-review-retry-max-tokens').value);

    const stageModels = {};
    for (const stage of ['narrative', 'choices', 'state', 'review', 'summary', 'knowledge_graph']) {
        const el = document.getElementById('profile-sm-' + stage);
        const v = el ? el.value.trim() : '';
        if (v) stageModels[stage] = v;
    }

    if (!name) { alert('请填写配置名称'); return; }
    if (!model) { alert('请填写模型名称'); return; }
    if (![reviewMaxTokens, reviewRetryMaxTokens].every(value => Number.isInteger(value) && value >= 1 && value <= 32768)) {
        alert('评审输出上限必须为 1 到 32768 的整数');
        return;
    }

    try {
        if (_editingProfileId) {
            const data = {
                name,
                provider_type: providerType,
                model,
                max_tokens: maxTokens,
                review_max_tokens: reviewMaxTokens,
                review_retry_max_tokens: reviewRetryMaxTokens,
                base_url: baseUrl || null,
                stage_models: stageModels,
            };
            if (apiKey) data.api_key = apiKey;
            await API.put(`/api/config/profiles/${_editingProfileId}`, data);
            showConfigStatus('配置已更新', true);
        } else {
            await API.post('/api/config/profiles', {
                name,
                provider_type: providerType,
                api_key: apiKey,
                model,
                max_tokens: maxTokens,
                review_max_tokens: reviewMaxTokens,
                review_retry_max_tokens: reviewRetryMaxTokens,
                base_url: baseUrl || null,
                stage_models: Object.keys(stageModels).length ? stageModels : null,
            });
            showConfigStatus('配置已保存', true);
        }
        _resetProfileForm();
        loadProfiles();
        loadProfilesDropdown();
    } catch (e) {
        showConfigStatus('保存失败: ' + e.message, false);
    }
}

// --- Presets ---

async function loadPresets() {
    try {
        const presets = await API.get('/api/config/presets');
        const select = document.getElementById('preset-select');
        select.innerHTML = '<option value="">-- 选择预设或手动填写 --</option>';
        for (const [key, p] of Object.entries(presets)) {
            const opt = document.createElement('option');
            opt.value = key;
            opt.textContent = p.name;
            select.appendChild(opt);
        }
    } catch (e) {
        console.log('Presets not loaded');
    }
}

async function applyPreset(presetKey) {
    if (!presetKey) return;
    try {
        const presets = await API.get('/api/config/presets');
        const p = presets[presetKey];
        if (!p) return;
        document.getElementById('profile-name').value = p.name || '';
        document.getElementById('profile-provider-type').value = p.provider_type || 'openai_compatible';
        document.getElementById('profile-base-url').value = p.base_url || '';
        document.getElementById('profile-model').value = p.model || '';
    } catch (e) {
        console.error('Failed to apply preset:', e);
    }
}

function showConfigStatus(msg, success) {
    const el = document.getElementById('config-status');
    el.textContent = msg;
    el.className = success ? 'status-success' : 'status-error';
    if (success) setTimeout(() => { el.textContent = ''; el.className = ''; }, 3000);
}

// ================================================
//  SCRIPTS
// ================================================

async function loadScripts() {
    try {
        const scripts = await API.get('/api/scripts');
        const container = document.getElementById('scripts-list');
        container.innerHTML = '';
        (Array.isArray(scripts) ? scripts : []).forEach(s => {
            container.innerHTML += `
                <div class="result-card">
                    <h4>${escapeHtml(s.name || s.id)}</h4>
                    <div class="result-source">ID: ${escapeHtml(s.id)} | 版本: ${s.version || '1.0'}</div>
                    <div class="result-actions">
                        <button onclick="editScriptVisual('${escapeAttr(s.id)}')" class="btn-primary">可视化编辑</button>
                        <button onclick="exportScript('${escapeAttr(s.id)}')" class="btn-secondary">导出</button>
                        <button onclick="deleteScript('${escapeAttr(s.id)}')" class="btn-secondary">删除</button>
                    </div>
                </div>`;
        });
    } catch (e) {
        console.error('Failed to load scripts:', e);
    }
}

async function deleteScript(scriptId) {
    if (!confirm('确定删除此剧本？')) return;
    try {
        await API.del(`/api/scripts/${scriptId}`);
        loadScripts();
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

function exportScript(scriptId) {
    window.open(`/api/scripts/${encodeURIComponent(scriptId)}/export`, '_blank');
}

async function importScript(input) {
    const file = input.files[0];
    if (!file) return;
    input.value = '';
    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await fetch('/api/scripts/import', {
            method: 'POST',
            headers: API._headers(),
            body: formData,
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const result = await res.json();
        const msg = result.overwritten ? `剧本「${result.name}」已更新` : `剧本「${result.name}」导入成功`;
        alert(msg);
        loadScripts();
    } catch (e) {
        alert('导入失败: ' + e.message);
    }
}

// ================================================
//  UTILITY
// ================================================

function escapeHtml(text) {
    if (!text) return '';
    return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _syncSidebarForViewport() {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    const button = document.querySelector('.sidebar-toggle');
    const statusPanel = document.getElementById('status-panel');
    const headerControls = document.getElementById('header-controls');
    const controls = [
        [sidebar, button],
        [statusPanel, document.getElementById('mobile-status-toggle')],
        [headerControls, document.getElementById('header-controls-toggle')],
    ];
    const mobile = window.matchMedia('(max-width: 600px)').matches;
    if (!mobile) {
        const wasOpen = controls.some(([panel]) => panel.classList.contains('mobile-open'));
        controls.forEach(([panel]) => panel.classList.remove('mobile-open'));
        if (wasOpen) button.focus();
    } else {
        statusPanel.classList.remove('map-open');
        if (statusPanel.style.display === 'none') statusPanel.classList.remove('mobile-open');
    }
    const open = mobile && controls.some(([panel]) => panel.classList.contains('mobile-open'));
    backdrop.hidden = !open;
    document.getElementById('main-area').inert = open;
    controls.forEach(([panel, trigger]) => {
        const panelOpen = mobile && panel.classList.contains('mobile-open');
        panel.inert = mobile && !panelOpen;
        trigger.setAttribute('aria-expanded', String(panelOpen));
        if (mobile && !panelOpen && panel.contains(document.activeElement)) trigger.focus();
    });
    const sidebarOpen = sidebar.classList.contains('mobile-open');
    button.setAttribute('aria-expanded', String(mobile ? sidebarOpen : !sidebar.classList.contains('collapsed')));
    button.setAttribute('aria-label', mobile ? (sidebarOpen ? '关闭导航' : '打开导航') :
        (sidebar.classList.contains('collapsed') ? '展开侧栏' : '收起侧栏'));
}

function _closeMobilePanels(restoreFocus = true) {
    const panelId = ['sidebar', 'status-panel', 'header-controls'].find(id =>
        document.getElementById(id).classList.contains('mobile-open'));
    if (!panelId) return false;
    document.getElementById(panelId).classList.remove('mobile-open');
    _syncSidebarForViewport();
    if (restoreFocus) {
        const trigger = panelId === 'sidebar' ? document.querySelector('.sidebar-toggle') :
            document.getElementById(panelId === 'status-panel' ? 'mobile-status-toggle' : 'header-controls-toggle');
        trigger.focus();
    }
    return true;
}

function _toggleMobilePanel(panelId, focusSelector) {
    const panel = document.getElementById(panelId);
    if (panel.classList.contains('mobile-open')) {
        _closeMobilePanels();
        return;
    }
    _closeMobilePanels(false);
    panel.classList.add('mobile-open');
    _syncSidebarForViewport();
    panel.querySelector(focusSelector)?.focus();
}

function toggleHeaderControls() {
    if (window.matchMedia('(max-width: 600px)').matches) _toggleMobilePanel('header-controls', '#pipeline-mode-btn');
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (window.matchMedia('(max-width: 600px)').matches) {
        _toggleMobilePanel('sidebar', '.nav-btn.active');
        return;
    }
    sidebar.classList.toggle('collapsed');
    _syncSidebarForViewport();
}

document.addEventListener('keydown', event => {
    if (event.key === 'Escape') _closeMobilePanels();
});

function toggleStatusPanel() {
    const panel = document.getElementById('status-panel');
    if (window.matchMedia('(max-width: 600px)').matches) {
        if (currentSaveId && panel.style.display !== 'none') {
            _toggleMobilePanel('status-panel', panel.classList.contains('storytree-expanded') ? '.mobile-status-close' : '.status-tab.active');
        }
        return;
    }
    if (document.getElementById('game-play-screen').classList.contains('map-view') &&
        window.matchMedia('(min-width: 601px) and (max-width: 1650px)').matches) {
        panel.classList.toggle('map-open');
        return;
    }
    panel.classList.toggle('collapsed');
    const btn = document.getElementById('status-panel-toggle');
    btn.innerHTML = panel.classList.contains('collapsed') ? '&#10094;' : '&#10095;';
    localStorage.setItem('statusPanelCollapsed', panel.classList.contains('collapsed'));
}

let useStreamMode = false;
let _cachedPipelineMode = 'workflow';

function toggleStreamMode() {
    useStreamMode = !useStreamMode;
    const btn = document.getElementById('stream-toggle');
    btn.classList.toggle('active-toggle', useStreamMode);
    btn.textContent = useStreamMode ? '流式(开)' : '流式';
}

// agentic 模式自动启用流式（SSE 是 Agent 工具调用实时展示的前提）
function _shouldUseStream() {
    if (useStreamMode) return true;
    if (_cachedPipelineMode === 'agentic' || _cachedPipelineMode === 'agentic_unified') return true;
    return false;
}

function _updateCachedPipelineMode(mode) {
    _cachedPipelineMode = mode || 'workflow';
    const unified = _cachedPipelineMode === 'agentic_unified';
    const queryButton = document.getElementById('freeform-query');
    const hint = document.getElementById('intent-mode-hint');
    if (queryButton) queryButton.hidden = !unified;
    if (hint) hint.hidden = !unified;
}

async function testActiveProfile() {
    showConfigStatus('正在测试连接...', true);
    try {
        const result = await API.post('/api/config/test', {});
        if (result.success) {
            showConfigStatus(`连接成功 (${result.provider} / ${result.name || ''})`, true);
        } else {
            showConfigStatus(`连接失败: ${result.error}`, false);
        }
    } catch (e) {
        showConfigStatus('测试失败: ' + e.message, false);
    }
}

// ================================================
//  SWIPE / REGENERATE
// ================================================

function updateSwipeControls(swipeIndex, total) {
    currentSwipeIndex = swipeIndex || 0;
    totalSwipes = total || 1;
    const controls = document.getElementById('swipe-controls');
    if (totalSwipes > 1) {
        controls.style.display = 'flex';
        document.getElementById('swipe-counter').textContent = `${currentSwipeIndex + 1}/${totalSwipes}`;
        document.getElementById('swipe-left').disabled = currentSwipeIndex <= 0;
        document.getElementById('swipe-right').disabled = currentSwipeIndex >= totalSwipes - 1;
    } else {
        // Always show if game is active (for regenerate button)
        controls.style.display = currentSaveId ? 'flex' : 'none';
        document.getElementById('swipe-counter').textContent = '1/1';
        document.getElementById('swipe-left').disabled = true;
        document.getElementById('swipe-right').disabled = true;
    }
}

function requestRegenerationHint() {
    const modal = document.getElementById('regenerate-hint-modal');
    const form = document.getElementById('regenerate-hint-form');
    const input = document.getElementById('regenerate-hint-input');
    const cancel = document.getElementById('regenerate-hint-cancel');
    input.value = '';
    modal.style.display = 'flex';
    input.focus();
    return new Promise(resolve => {
        const close = value => {
            modal.style.display = 'none';
            form.removeEventListener('submit', submit);
            cancel.removeEventListener('click', dismiss);
            modal.removeEventListener('click', dismissOutside);
            document.removeEventListener('keydown', dismissEscape);
            resolve(value);
        };
        const submit = event => { event.preventDefault(); close(input.value); };
        const dismiss = () => close(null);
        const dismissOutside = event => { if (event.target === modal) dismiss(); };
        const dismissEscape = event => { if (event.key === 'Escape') dismiss(); };
        form.addEventListener('submit', submit);
        cancel.addEventListener('click', dismiss);
        modal.addEventListener('click', dismissOutside);
        document.addEventListener('keydown', dismissEscape);
    });
}

async function regenerateResponse() {
    if (!currentSaveId || isStreaming) return;
    const saveId = currentSaveId;
    const rawHint = await requestRegenerationHint();
    if (rawHint === null || currentSaveId !== saveId || isStreaming) return;
    const hint = rawHint || '';
    const params = hint ? `?hint=${encodeURIComponent(hint)}` : '';
    if (_shouldUseStream() && typeof submitActionStream === 'function') {
        return submitActionStream({type: 'regen'}, `/api/game/${saveId}/regenerate/stream${params}`);
    }
    const actionToken = {};
    window._activeGameAction = actionToken;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === actionToken;
    isStreaming = true;
    document.getElementById('btn-regenerate').disabled = true;

    try {
        const result = await API.post(`/api/game/${saveId}/regenerate${params}`);
        if (!isActive()) return;
        const _s = (label, fn) => { try { fn(); } catch(e) { console.error(`[regen:${label}]`, e); } };
        const content = document.getElementById('narrative-content');
        const blocks = content.querySelectorAll('.turn-block');
        const lastBlock = blocks[blocks.length - 1];
        if (lastBlock) {
            const textDiv = lastBlock.querySelector('.narrative-text') || lastBlock.querySelector('div:not(.turn-action):not(.think-block)');
            if (textDiv) textDiv.innerHTML = formatNarrativeHtml(result.narrative);
        }
        _s('renderChoices', () => renderChoices(result.choices || []));
        _s('updateSwipeControls', () => updateSwipeControls(result.swipe_index, result.total_swipes));
        _s('showStateChanges', () => showStateChanges(result.state_changes, result.state));
        if (result.scene_image_path) {
            _s('renderSceneImage', () => renderSceneImage({url: result.scene_image_path}));
        }
        if (result.state) {
            currentState = result.state;
            _s('updateStatusPanel', () => updateStatusPanel(currentState));
            _s('updateHeader', () => updateHeader());
        }
    } catch (e) {
        if (!isActive()) return;
        console.error('[regenerateResponse]', e, e.stack);
        alert('重新生成失败: ' + e.message + '\n\n' + (e.stack || ''));
    } finally {
        if (isActive()) {
            window._activeGameAction = null;
            isStreaming = false;
            document.getElementById('btn-regenerate').disabled = false;
        }
    }
}

async function regenerateStage(stage) {
    if (!currentSaveId || isStreaming) return;
    const saveId = currentSaveId;
    if (_shouldUseStream() && typeof submitActionStream === 'function') {
        return submitActionStream({type: 'regen', stage}, `/api/game/${saveId}/regenerate/stream?stage=${stage}`);
    }
    const actionToken = {};
    window._activeGameAction = actionToken;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === actionToken;
    isStreaming = true;
    const btnId = stage === 'choices' ? 'btn-regen-choices' : 'btn-regen-state';
    document.getElementById(btnId).disabled = true;

    try {
        const result = await API.post(`/api/game/${saveId}/regenerate?stage=${stage}`);
        if (!isActive()) return;
        const _s = (label, fn) => { try { fn(); } catch(e) { console.error(`[regen:${label}]`, e); } };
        if (stage === 'choices') {
            _s('renderChoices', () => renderChoices(result.choices || []));
        }
        if (stage === 'state') {
            _s('showStateChanges', () => showStateChanges(result.state_changes, result.state));
            if (result.state) {
                currentState = result.state;
                _s('updateStatusPanel', () => updateStatusPanel(currentState));
                _s('updateHeader', () => updateHeader());
            }
        }
        _s('updateSwipeControls', () => updateSwipeControls(result.swipe_index, result.total_swipes));
    } catch (e) {
        if (!isActive()) return;
        console.error('[regenerateStage]', e, e.stack);
        alert(`重新生成${stage === 'choices' ? '选项' : '状态'}失败: ` + e.message + '\n\n' + (e.stack || ''));
    } finally {
        if (isActive()) {
            window._activeGameAction = null;
            isStreaming = false;
            document.getElementById(btnId).disabled = false;
        }
    }
}

async function continueNarrative() {
    if (!currentSaveId || isStreaming) return;
    const saveId = currentSaveId;
    const actionToken = {};
    window._activeGameAction = actionToken;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === actionToken;
    isStreaming = true;
    const btn = document.getElementById('btn-continue');
    if (btn) btn.disabled = true;

    try {
        const result = await API.post(`/api/game/${saveId}/continue`);
        if (!isActive()) return;
        const content = document.getElementById('narrative-content');
        const blocks = content.querySelectorAll('.turn-block');
        const lastBlock = blocks[blocks.length - 1];
        if (lastBlock) {
            const textDiv = lastBlock.querySelector('.narrative-text') || lastBlock.querySelector('div:not(.turn-action):not(.think-block)');
            if (textDiv) textDiv.innerHTML = formatNarrativeHtml(result.narrative);
        }
    } catch (e) {
        if (!isActive()) return;
        alert('续写失败: ' + e.message);
    } finally {
        if (isActive()) {
            window._activeGameAction = null;
            isStreaming = false;
            if (btn) btn.disabled = false;
        }
    }
}

async function swipeLeft() {
    return _swipeTo('left');
}

async function swipeRight() {
    return _swipeTo('right');
}

// Flow#7: 撤销上一步
async function undoLastTurn() {
    if (!currentSaveId || isStreaming) return;
    if (!confirm('撤销上一步？这会回到上一回合的状态，当前回合将被删除。')) return;
    const saveId = currentSaveId;
    isStreaming = true;
    let undone = false;
    try {
        const result = await API.post(`/api/game/${saveId}/undo`);
        undone = true;
        if (currentSaveId !== saveId) return;
        await _showUndoneTurn(result, saveId);
    } catch (e) {
        if (currentSaveId === saveId) alert(undone
            ? '撤销已生效，但画面刷新失败，请重新加载存档: ' + e.message
            : '撤销失败: ' + e.message);
    } finally {
        if (currentSaveId === saveId) isStreaming = false;
    }
}

function _showQueryReply(action, reply) {
    document.getElementById('narrative-content').appendChild(_createQueryReply(action.text, reply));
}

function _appendQueryExchanges(block, exchanges) {
    (exchanges || []).forEach(exchange => {
        block.appendChild(_createQueryReply(exchange.question, exchange.answer));
    });
}

function _createQueryReply(questionText, reply) {
    const block = document.createElement('div');
    block.className = 'query-response';
    const question = document.createElement('div');
    question.className = 'query-question';
    question.textContent = questionText || '';
    const answer = document.createElement('div');
    answer.className = 'query-answer';
    answer.textContent = reply || '';
    block.appendChild(question);
    block.appendChild(answer);
    return block;
}

async function _showUndoneTurn(result, saveId, isActive = () => currentSaveId === saveId) {
    if (!result || !result.state) return;
    const historyResp = await API.get(`/api/game/${saveId}/history?last=10`);
    if (!isActive()) return;
    currentState = result.state;
    _prevState = null;
    document.getElementById('narrative-content').innerHTML = '';
    document.getElementById('choices-area').innerHTML = '';
    showGameScreen({ state: result.state, node_id: result.node_id, history: historyResp.entries, historyTotal: historyResp.total, swipeIndex: historyResp.swipe_index, swipeTotal: historyResp.total_swipes, choices: result.choices });
    renderChoices(result.choices || []);
    loadSidebarSaves();
}

async function openSwipePicker() {
    if (!currentSaveId || isStreaming) return;
    const saveId = currentSaveId;
    const nodeId = currentNodeId;
    const loadSequence = _saveLoadSequence;
    const swipeIndex = currentSwipeIndex;
    const swipeCount = totalSwipes;
    const gamePanel = document.getElementById('panel-game');
    const isCurrent = () => currentSaveId === saveId && currentNodeId === nodeId &&
        _saveLoadSequence === loadSequence && currentSwipeIndex === swipeIndex &&
        totalSwipes === swipeCount && (!gamePanel || gamePanel.classList.contains('active'));
    try {
        const swipes = await API.get(`/api/game/${saveId}/swipes`);
        if (!isCurrent() || isStreaming) return;
        if (!swipes || !swipes.length) return;

        const existing = document.getElementById('swipe-picker-modal');
        if (existing) existing.remove();

        const modal = document.createElement('div');
        modal.id = 'swipe-picker-modal';
        modal.className = 'modal';
        modal.onclick = (e) => { if (e.target === modal) modal.remove(); };

        let listHtml = swipes.map(s => {
            const preview = escapeHtml(s.narrative_preview || '').replace(/\n/g, '<br>');
            const choicesText = (s.choices || []).map((c, i) => `${i + 1}. ${escapeHtml(c)}`).join('<br>');
            const activeClass = s.active ? ' swipe-item-active' : '';
            return `<div class="swipe-item${activeClass}" data-index="${s.index}" role="button" tabindex="0" aria-label="切换到第${s.index + 1}版" aria-current="${s.active ? 'true' : 'false'}">
                <div class="swipe-item-header">
                    <span class="swipe-item-idx">#${s.index + 1}</span>
                    ${s.active ? '<span class="swipe-item-current">当前</span>' : ''}
                </div>
                <div class="swipe-item-preview">${preview}</div>
                ${choicesText ? `<div class="swipe-item-choices">${choicesText}</div>` : ''}
            </div>`;
        }).join('');

        modal.innerHTML = `<div class="modal-content swipe-picker-content">
            <span class="modal-close" onclick="this.closest('.modal').remove()">&times;</span>
            <h3>Swipe 总览 (${swipes.length})</h3>
            <div class="swipe-picker-list">${listHtml}</div>
        </div>`;
        const selectSwipe = event => {
            if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') return;
            const item = event.target.closest('.swipe-item');
            if (!item || !modal.contains(item)) return;
            if (event.type === 'keydown') event.preventDefault();
            if (!isCurrent()) {
                modal.remove();
                return;
            }
            jumpToSwipe(Number(item.dataset.index));
        };
        const list = modal.querySelector('.swipe-picker-list');
        list.addEventListener('click', selectSwipe);
        list.addEventListener('keydown', selectSwipe);
        document.body.appendChild(modal);
    } catch (e) {
        console.error('Failed to load swipes:', e);
    }
}

async function jumpToSwipe(index) {
    if (!currentSaveId || isStreaming) return;
    const modal = document.getElementById('swipe-picker-modal');
    if (modal) modal.remove();
    return _swipeTo(`jump?index=${index}`);
}

async function _swipeTo(path) {
    if (!currentSaveId || !currentNodeId || isStreaming) return;
    const saveId = currentSaveId;
    const nodeId = currentNodeId;
    const loadSequence = _saveLoadSequence;
    const swipeIndex = currentSwipeIndex;
    const swipeCount = totalSwipes;
    const isCurrent = () => currentSaveId === saveId && currentNodeId === nodeId &&
        _saveLoadSequence === loadSequence;
    let refreshSequence = null;
    let pendingSwipe = null;
    isStreaming = true;
    try {
        const separator = path.includes('?') ? '&' : '?';
        const contextQuery = `expected_node_id=${encodeURIComponent(nodeId)}&expected_swipe_index=${swipeIndex}&expected_total_swipes=${swipeCount}`;
        const request = API.post(`/api/game/${saveId}/swipe/${path}${separator}${contextQuery}`);
        pendingSwipe = { saveId, settled: request.then(() => {}, () => {}) };
        _pendingSwipeRequests.add(pendingSwipe);
        const result = await request;
        if (isCurrent()) applySwipeResult(result);
    } catch (e) {
        console.error('Swipe failed:', e);
        if (e.status === 409 && isCurrent()) {
            refreshSequence = _saveLoadSequence + 1;
            if (await continueGame(saveId) && currentSaveId === saveId &&
                    typeof showNotification === 'function') {
                showNotification('回合版本已变化，请重新选择 Swipe 版本', 'info');
            }
        }
    } finally {
        if (pendingSwipe) _pendingSwipeRequests.delete(pendingSwipe);
        if (isCurrent() || (refreshSequence !== null && currentSaveId === saveId &&
                _saveLoadSequence === refreshSequence)) isStreaming = false;
    }
}

function applySwipeResult(result) {
    const content = document.getElementById('narrative-content');
    const blocks = content.querySelectorAll('.turn-block');
    const lastBlock = blocks[blocks.length - 1];
    if (lastBlock) {
        const textDiv = lastBlock.querySelector('.narrative-text') || lastBlock.querySelector('div:not(.turn-action):not(.think-block)');
        if (textDiv) textDiv.innerHTML = formatNarrativeHtml(result.narrative);
    }
    renderChoices(result.choices || []);
    updateSwipeControls(result.swipe_index, result.total_swipes);
    if (result.state) {
        currentState = result.state;
        updateStatusPanel(currentState);
        updateHeader();
    }
    showStateChanges(result.state_changes);
}

// ================================================
//  AUTHOR'S NOTE
// ================================================

const STYLE_PRESETS = [
    { label: '文艺细腻', prompt: '请使用文学性强的笔触，注重环境氛围与心理刻画，语言优美细腻，善用比喻、通感等修辞手法，营造沉浸感。' },
    { label: '简洁利落', prompt: '请使用简洁直接的叙事风格，少用修饰词，节奏明快，重点突出动作和对话，避免冗长描写。' },
    { label: '悬疑紧张', prompt: '请营造紧张悬疑的氛围，善用伏笔与悬念，控制信息揭露的节奏，通过细节暗示危险，增强读者的不安感。' },
    { label: '幽默诙谐', prompt: '请使用轻松幽默的语调，适当加入俏皮的比喻、夸张和反讽，对话风趣自然，让叙事充满趣味。' },
    { label: '史诗恢弘', prompt: '请使用恢弘大气的叙事风格，注重宏大场面与气势渲染，语言庄重有力，赋予事件史诗般的厚重感。' },
    { label: '暗黑写实', prompt: '请营造阴郁沉重的氛围，描写注重残酷与真实感，不回避人性的黑暗面，叙事冷峻克制。' },
    { label: '轻小说风', prompt: '请使用日式轻小说的叙事风格，第一人称内心独白丰富，对话生动活泼，适当使用夸张的情绪反应和吐槽。' },
    { label: '古风武侠', prompt: '请使用半文言的古风笔法，用词典雅凝练，打斗场面行云流水，对话有江湖豪气，意境深远。' },
    { label: 'TRPG写实', prompt: '请使用硬派写实的TRPG主持人风格：以摄影机视角叙事，只描述角色能看到、听到、闻到的；大量具体感官细节（气味、温度、材质、光线）；NPC对话自然口语化，有停顿、打断和语气词；环境中自然嵌入线索和信息，不做直白提示；基调冷峻，可穿插黑色幽默和讽刺；注重时代感与物质细节的准确。' },
];

const IMAGE_STYLE_PRESETS = [
    { label: '电影质感', prompt: 'cinematic lighting, film grain, dramatic shadows, wide-angle shot, moody color grading, depth of field' },
    { label: '日系动漫', prompt: 'anime style, cel-shading, vibrant colors, detailed linework, studio ghibli aesthetic, soft lighting' },
    { label: '水彩画风', prompt: 'watercolor painting, soft edges, translucent washes, delicate brushstrokes, ethereal atmosphere, muted palette' },
    { label: '油画质感', prompt: 'oil painting style, rich impasto texture, classical composition, warm color palette, museum quality, chiaroscuro' },
    { label: '像素艺术', prompt: 'pixel art, 16-bit retro game style, limited color palette, crisp pixels, nostalgic, dithering' },
    { label: '写实摄影', prompt: 'photorealistic, DSLR quality, natural lighting, shallow depth of field, 8K detail, bokeh' },
    { label: '奇幻插画', prompt: 'fantasy illustration, detailed concept art, magical atmosphere, epic composition, rich colors, artstation quality' },
    { label: '赛博朋克', prompt: 'cyberpunk aesthetic, neon lights, rain-slicked streets, holographic displays, dark futuristic, high contrast' },
    { label: '哥特暗黑', prompt: 'dark gothic style, dramatic chiaroscuro, muted desaturated tones, ominous atmosphere, baroque details' },
];

function renderImageStylePresets() {
    const container = document.getElementById('image-style-presets');
    if (!container) return;
    container.innerHTML = IMAGE_STYLE_PRESETS.map((p, i) =>
        `<span class="style-chip" onclick="applyImageStylePreset(${i})">${escapeHtml(p.label)}</span>`
    ).join('');
}

function applyImageStylePreset(index) {
    const preset = IMAGE_STYLE_PRESETS[index];
    if (!preset) return;
    const ta = document.getElementById('image-style-input');
    ta.value = preset.prompt;
    ta.focus();
    document.querySelectorAll('#image-style-presets .style-chip').forEach((el, i) => {
        el.classList.toggle('active', i === index);
    });
}

async function saveImageStyle() {
    const custom = document.getElementById('image-style-input').value.trim();
    const activeChip = document.querySelector('#image-style-presets .style-chip.active');
    const presetLabel = activeChip ? activeChip.textContent : '';
    try {
        await API.put('/api/config/image/style', { preset: presetLabel, custom });
        document.getElementById('image-style-status').textContent = '已保存';
        setTimeout(() => { document.getElementById('image-style-status').textContent = ''; }, 2000);
    } catch (e) {
        document.getElementById('image-style-status').textContent = '保存失败';
    }
}

async function loadImageStyle() {
    try {
        const data = await API.get('/api/config/image/style');
        const ta = document.getElementById('image-style-input');
        if (data.custom) {
            ta.value = data.custom;
        } else if (data.preset) {
            const idx = IMAGE_STYLE_PRESETS.findIndex(p => p.label === data.preset);
            if (idx >= 0) {
                ta.value = IMAGE_STYLE_PRESETS[idx].prompt;
                document.querySelectorAll('#image-style-presets .style-chip').forEach((el, i) => {
                    el.classList.toggle('active', i === idx);
                });
            }
        }
    } catch (e) { /* ignore */ }
}

function renderStylePresets() {
    const container = document.getElementById('style-presets');
    if (!container) return;
    container.innerHTML = STYLE_PRESETS.map((p, i) =>
        `<span class="style-chip" onclick="applyStylePreset(${i})">${escapeHtml(p.label)}</span>`
    ).join('');
}

function applyStylePreset(index) {
    const preset = STYLE_PRESETS[index];
    if (!preset) return;
    const ta = document.getElementById('authors-note-input');
    ta.value = preset.prompt;
    ta.focus();
    document.querySelectorAll('.style-chip').forEach((el, i) => {
        el.classList.toggle('active', i === index);
    });
}

function toggleAuthorsNote() {
    const body = document.getElementById('authors-note-body');
    const arrow = document.getElementById('authors-note-toggle');
    if (body.style.display === 'none') {
        body.style.display = 'block';
        arrow.classList.add('open');
        renderStylePresets();
        renderImageStylePresets();
        loadImageStyle();
    } else {
        body.style.display = 'none';
        arrow.classList.remove('open');
    }
}

function toggleOpeningAN() {
    const body = document.getElementById('opening-an-body');
    const arrow = document.getElementById('opening-an-toggle');
    if (body.style.display === 'none') {
        body.style.display = 'block';
        arrow.classList.add('open');
        _renderOpeningImagePresets();
        _loadOpeningImageStyle();
    } else {
        body.style.display = 'none';
        arrow.classList.remove('open');
    }
}

function _populateOpeningStylePresets() {
    const container = document.getElementById('opening-style-presets');
    if (!container) return;
    container.innerHTML = STYLE_PRESETS.map((p, i) =>
        `<span class="style-chip" onclick="_applyOpeningStylePreset(${i})">${escapeHtml(p.label)}</span>`
    ).join('');
}

function _applyOpeningStylePreset(index) {
    const preset = STYLE_PRESETS[index];
    if (!preset) return;
    const ta = document.getElementById('opening-an-input');
    if (!ta) return;
    ta.value = preset.prompt;
    ta.focus();
    document.querySelectorAll('#opening-style-presets .style-chip').forEach((el, i) => {
        el.classList.toggle('active', i === index);
    });
}

function _renderOpeningImagePresets() {
    const container = document.getElementById('opening-image-style-presets');
    if (!container) return;
    container.innerHTML = IMAGE_STYLE_PRESETS.map((p, i) =>
        `<span class="style-chip" onclick="_applyOpeningImagePreset(${i})">${escapeHtml(p.label)}</span>`
    ).join('');
}

function _applyOpeningImagePreset(index) {
    const preset = IMAGE_STYLE_PRESETS[index];
    if (!preset) return;
    const ta = document.getElementById('opening-image-style-input');
    if (!ta) return;
    ta.value = preset.prompt;
    ta.focus();
    document.querySelectorAll('#opening-image-style-presets .style-chip').forEach((el, i) => {
        el.classList.toggle('active', i === index);
    });
}

async function _loadOpeningImageStyle() {
    try {
        const data = await API.get('/api/config/image/style');
        const ta = document.getElementById('opening-image-style-input');
        if (!ta) return;
        if (data.custom) {
            ta.value = data.custom;
        }
        if (data.preset) {
            const idx = IMAGE_STYLE_PRESETS.findIndex(p => p.label === data.preset);
            if (idx >= 0) {
                if (!data.custom) ta.value = IMAGE_STYLE_PRESETS[idx].prompt;
                document.querySelectorAll('#opening-image-style-presets .style-chip').forEach((el, i) => {
                    el.classList.toggle('active', i === idx);
                });
            }
        }
    } catch (_) {}
}

async function saveAuthorsNote() {
    if (!currentSaveId) return;
    const note = document.getElementById('authors-note-input').value;
    const position = document.getElementById('authors-note-position').value || 'end';
    const depth = parseInt(document.getElementById('authors-note-depth').value) || 4;
    try {
        await API.post(`/api/game/${currentSaveId}/authors-note`, { note, position, depth });
        document.getElementById('authors-note-status').textContent = '已保存';
        setTimeout(() => {
            document.getElementById('authors-note-status').textContent = '';
        }, 2000);
    } catch (e) {
        document.getElementById('authors-note-status').textContent = '保存失败';
    }
}

function toggleANDepth() {
    const sel = document.getElementById('authors-note-position');
    const label = document.getElementById('an-depth-label');
    if (label) label.style.display = sel.value === 'at_depth' ? 'inline' : 'none';
}

async function saveNegativePrompt() {
    if (!currentSaveId) return;
    const text = document.getElementById('negative-prompt-input').value;
    try {
        await API.post(`/api/game/${currentSaveId}/negative-prompt`, { text });
        const st = document.getElementById('negative-prompt-status');
        if (st) { st.textContent = '已保存'; setTimeout(() => { st.textContent = ''; }, 2000); }
    } catch (e) {
        const st = document.getElementById('negative-prompt-status');
        if (st) st.textContent = '保存失败';
    }
}

let _logitBiasEntries = [];

function renderLogitBiasList() {
    const list = document.getElementById('logit-bias-list');
    if (!list) return;
    if (!_logitBiasEntries.length) {
        list.innerHTML = '<span style="color:var(--text-muted)">暂无偏置规则</span>';
        return;
    }
    list.innerHTML = _logitBiasEntries.map((e, i) => {
        const color = e.bias > 0 ? '#4caf50' : e.bias < 0 ? '#e57373' : 'var(--text-muted)';
        const sign = e.bias > 0 ? '+' : '';
        return `<div class="status-kv" style="margin-bottom:2px"><span class="status-kv-key">${escapeHtml(e.text)}</span><span class="status-kv-val" style="color:${color}">${sign}${e.bias} <button onclick="removeLogitBias(${i})" style="font-size:0.7rem;cursor:pointer">&times;</button></span></div>`;
    }).join('');
}

async function addLogitBias() {
    const wordEl = document.getElementById('lb-word');
    const valEl = document.getElementById('lb-value');
    const word = (wordEl.value || '').trim();
    const bias = parseInt(valEl.value, 10);
    if (!word || bias === 0) return;
    const existing = _logitBiasEntries.findIndex(e => e.text === word);
    if (existing >= 0) {
        _logitBiasEntries[existing].bias = bias;
    } else {
        _logitBiasEntries.push({ text: word, bias });
    }
    wordEl.value = '';
    valEl.value = '0';
    document.getElementById('lb-value-display').textContent = '0';
    renderLogitBiasList();
    await saveLogitBias();
}

async function removeLogitBias(index) {
    _logitBiasEntries.splice(index, 1);
    renderLogitBiasList();
    await saveLogitBias();
}

async function saveLogitBias() {
    if (!currentSaveId) return;
    try {
        await API.post(`/api/game/${currentSaveId}/logit-bias`, { entries: _logitBiasEntries });
    } catch (e) {}
}

async function saveLorebookRecursion() {
    if (!currentSaveId) return;
    const input = document.getElementById('lorebook-recursion-input');
    const status = document.getElementById('lorebook-recursion-status');
    const val = parseInt(input.value, 10);
    if (isNaN(val) || val < 1 || val > 8) {
        if (status) status.textContent = '范围1-8';
        return;
    }
    try {
        const res = await API.post(`/api/game/${currentSaveId}/lorebook-recursion`, { max_recursion: val });
        if (status) { status.textContent = `已设为 ${res.max_recursion}`; setTimeout(() => status.textContent = '', 2000); }
    } catch (e) {
        if (status) status.textContent = '保存失败';
    }
}

const _EMOTION_COLORS = {
    joy: '#f0c040', sadness: '#6088c0', anger: '#d04040', fear: '#8040a0',
    surprise: '#e08030', love: '#e06080', tension: '#c04040', calm: '#40a080',
    excitement: '#f08020', melancholy: '#7080a0',
};
const _EMOTION_LABELS = {
    joy: '喜悦', sadness: '悲伤', anger: '愤怒', fear: '恐惧',
    surprise: '惊讶', love: '爱意', tension: '紧张', calm: '平静',
    excitement: '兴奋', melancholy: '忧郁',
};

async function switchPersona(presetId) {
    // Legacy — redirect to POV switch
    await executePovSwitch(presetId, null);
}

function openPovSwitchModal() {
    const povs = currentState?._available_povs || [];
    const locations = currentState?.visible_locations || [];
    const dn = currentState?.display_names || {};
    let html = '<div class="pov-modal-overlay" id="pov-modal" onclick="if(event.target===this)closePovModal()">';
    html += '<div class="pov-modal">';
    html += '<h3 style="margin:0 0 0.5rem">切换视角</h3>';
    html += '<p style="font-size:0.8rem;color:var(--text-muted);margin-bottom:0.8rem">选择一个角色继续在同一世界中推演。当前角色将保留状态并转为NPC。</p>';
    // Preset list
    if (povs.length > 0) {
        html += '<div class="pov-list">';
        for (const p of povs) {
            const badge = p.status === 'shelved' ? '<span class="pov-badge">可恢复</span>' : '';
            html += `<div class="pov-option" onclick="executePovSwitch('${escapeHtml(p.id)}', null)"><span>${escapeHtml(p.name)}</span>${badge}</div>`;
        }
        html += '</div>';
    }
    // Custom character form
    html += '<details class="pov-custom-details" style="margin-top:0.8rem"><summary style="cursor:pointer;font-size:0.85rem;color:var(--accent)">自建新角色</summary>';
    html += '<div class="pov-custom-form" style="margin-top:0.5rem;display:flex;flex-direction:column;gap:0.4rem">';
    html += '<input id="pov-custom-name" placeholder="角色名" style="font-size:0.8rem;padding:4px 6px">';
    html += '<input id="pov-custom-bio" placeholder="身份简介" style="font-size:0.8rem;padding:4px 6px">';
    html += '<input id="pov-custom-personality" placeholder="性格关键词" style="font-size:0.8rem;padding:4px 6px">';
    html += '<input id="pov-custom-goal" placeholder="长期目标" style="font-size:0.8rem;padding:4px 6px">';
    // Title (combo: select existing or type new)
    const titles = currentState?._pov_titles || [];
    html += '<datalist id="pov-titles-list">';
    for (const t of titles) html += `<option value="${escapeHtml(t)}">`;
    html += '</datalist>';
    html += '<input id="pov-custom-title" list="pov-titles-list" placeholder="职位/头衔" style="font-size:0.8rem;padding:4px 6px">';
    // Organization (combo: select existing or type new)
    const orgs = currentState?._pov_orgs || [];
    html += '<datalist id="pov-orgs-list">';
    for (const o of orgs) html += `<option value="${escapeHtml(o.id)}">${escapeHtml(o.name)}</option>`;
    html += '</datalist>';
    html += '<input id="pov-custom-org" list="pov-orgs-list" placeholder="所属组织" style="font-size:0.8rem;padding:4px 6px">';
    // Location (combo: select existing or type new)
    html += '<datalist id="pov-locs-list">';
    for (const locId of locations) {
        html += `<option value="${escapeHtml(locId)}">${escapeHtml(dn[locId] || locId)}</option>`;
    }
    html += '</datalist>';
    html += '<input id="pov-custom-location" list="pov-locs-list" placeholder="初始位置" style="font-size:0.8rem;padding:4px 6px">';
    html += `<button onclick="submitPovCustomChar()" class="btn-secondary" style="font-size:0.8rem;margin-top:0.3rem">确认创建并切换</button>`;
    html += '</div></details>';
    html += '<div style="margin-top:0.8rem;text-align:right"><button onclick="closePovModal()" class="btn-secondary" style="font-size:0.8rem">取消</button></div>';
    html += '</div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
}

function submitPovCustomChar() {
    const name = document.getElementById('pov-custom-name')?.value.trim();
    if (!name) { alert('请输入角色名'); return; }
    const custom = {
        name,
        bio: document.getElementById('pov-custom-bio')?.value.trim() || '',
        personality: document.getElementById('pov-custom-personality')?.value.trim() || '',
        long_term_goal: document.getElementById('pov-custom-goal')?.value.trim() || '',
        title: document.getElementById('pov-custom-title')?.value.trim() || '',
        organization: document.getElementById('pov-custom-org')?.value.trim() || '',
        initial_location: document.getElementById('pov-custom-location')?.value || '',
        attributes: {},
    };
    executePovSwitch(null, custom);
}

function closePovModal() {
    document.getElementById('pov-modal')?.remove();
}

async function executePovSwitch(presetId, customChar) {
    closePovModal();
    if (!currentSaveId) return;
    try {
        const body = {};
        if (presetId) body.preset_id = presetId;
        if (customChar) body.custom_character = customChar;
        const res = await API.post(`/api/game/${currentSaveId}/switch-pov`, body);
        if (res.state) {
            currentState = res.state;
            updateStatusPanel(currentState);
        }
        // Insert POV switch separator + narrative
        const chatBox = document.getElementById('chat-box');
        if (chatBox && res.narrative) {
            const sep = document.createElement('div');
            sep.className = 'pov-switch-separator';
            sep.innerHTML = `<div class="pov-switch-label">—— 视角切换：${escapeHtml(res.state?.player?.name || '新角色')} ——</div>`;
            chatBox.appendChild(sep);
            appendNarrative(res.narrative);
        }
        // Render new choices
        if (res.choices) {
            renderChoices(res.choices);
        }
        if (typeof showNotification === 'function') showNotification('视角已切换', 'success');
    } catch (e) {
        if (typeof showNotification === 'function') showNotification('切换失败: ' + e.message, 'error');
    }
}

function toggleDataBankLore() {
    const body = document.getElementById('databank-lore-body');
    const arrow = document.getElementById('databank-lore-toggle');
    if (body.style.display === 'none') {
        body.style.display = 'block';
        arrow.classList.add('open');
        loadDataBankFiles();
    } else {
        body.style.display = 'none';
        arrow.classList.remove('open');
    }
}

async function loadSidebarSaves() {
    const list = document.getElementById('sidebar-save-list');
    if (!list) return;
    try {
        const saves = await API.get('/api/saves');
        const arr = Array.isArray(saves) ? saves : [];
        if (arr.length === 0) {
            list.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem">暂无存档</span>';
            return;
        }
        list.innerHTML = arr.map(s => {
            const timeStr = s.updated_at ? _formatSaveTime(s.updated_at) : '';
            const active = s.id === currentSaveId ? ' style="border-color:var(--accent)"' : '';
            return `<button type="button" class="save-card sidebar-save"${active} onclick="continueGame('${escapeAttr(s.id)}')" aria-label="继续游戏：${escapeAttr(s.name || s.script_id)}">
                <span class="save-name" style="font-size:0.82rem">${escapeHtml(s.name || s.script_id)}</span>
                <span class="save-info" style="font-size:0.72rem">回合${s.total_nodes || 0}${timeStr ? ' 保存于 ' + timeStr : ''}</span>
            </button>`;
        }).join('');
    } catch (e) {}
}

async function loadDataBankFiles() {
    if (!currentSaveId) return;
    try {
        const res = await API.get(`/api/game/${currentSaveId}/databank`);
        const list = document.getElementById('databank-file-list');
        if (!res.files || res.files.length === 0) {
            list.innerHTML = '<span style="color:var(--text-muted)">暂无文档</span>';
            return;
        }
        list.innerHTML = res.files.map(f => `<div class="status-kv"><span class="status-kv-key">${escapeHtml(f.filename)}</span><span class="status-kv-val">${f.chunks}块 ${f.chars == null ? '字数未知' : `${f.chars}字`} <button onclick="deleteDataBankFile('${f.id}')" style="font-size:0.7rem;cursor:pointer">&times;</button></span></div>`).join('');
    } catch (e) {}
}

async function uploadDataBankFile(input) {
    if (!currentSaveId || !input.files.length) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);
    try {
        const response = await fetch(`/api/game/${currentSaveId}/databank/upload`, { method: 'POST', body: formData });
        if (!response.ok) throw new Error('上传失败');
        loadDataBankFiles();
    } catch (e) {
        if (typeof showNotification === 'function') showNotification('上传失败', 'error');
    }
    input.value = '';
}

async function deleteDataBankFile(fileId) {
    if (!currentSaveId) return;
    try {
        await API.delete(`/api/game/${currentSaveId}/databank/${fileId}`);
        loadDataBankFiles();
    } catch (e) {}
}

async function scrapeToDataBank() {
    if (!currentSaveId) return;
    const input = document.getElementById('db-scrape-url');
    const url = (input.value || '').trim();
    if (!url) return;
    try {
        new URL(url);
    } catch {
        if (typeof showNotification === 'function') showNotification('请输入有效的URL', 'error');
        return;
    }
    try {
        input.disabled = true;
        await API.post(`/api/game/${currentSaveId}/databank/scrape`, { url });
        input.value = '';
        loadDataBankFiles();
        if (typeof showNotification === 'function') showNotification('网页抓取成功', 'success');
    } catch (e) {
        const msg = e?.message || '抓取失败';
        if (typeof showNotification === 'function') showNotification(msg, 'error');
    } finally {
        input.disabled = false;
    }
}

function showEmotionLabel(emotion) {
    let el = document.getElementById('emotion-label');
    if (!emotion) {
        if (el) el.style.display = 'none';
        return;
    }
    if (!el) {
        el = document.createElement('div');
        el.id = 'emotion-label';
        el.style.cssText = 'position:absolute;top:8px;right:12px;font-size:0.78rem;padding:2px 8px;border-radius:10px;color:#fff;z-index:5;transition:opacity 0.3s';
        const area = document.getElementById('narrative-area');
        if (area) { area.style.position = 'relative'; area.appendChild(el); }
    }
    el.style.display = '';
    el.style.background = _EMOTION_COLORS[emotion] || '#888';
    el.textContent = _EMOTION_LABELS[emotion] || emotion;
}

// ================================================
//  NEW GAMEPLAY NOTIFICATIONS
// ================================================

function showCheckResult(check) {
    if (!check) return;
    const labels = { critical_success: '大成功', success: '成功', failure: '失败', critical_failure: '大失败' };
    const colors = { critical_success: 'var(--gold)', success: 'var(--success)', failure: 'var(--danger)', critical_failure: 'var(--danger)' };
    const diffLabels = { extreme: '极难', hard: '困难', medium: '普通', easy: '简单' };
    const label = labels[check.outcome] || check.outcome;
    const color = colors[check.outcome] || 'var(--text-secondary)';
    const attr = check.related_attribute || '';
    const diff = diffLabels[check.difficulty] || '';
    const skillDesc = attr ? ` [${escapeHtml(attr)}${diff ? '·' + diff : ''}]` : '';

    let detail = '';
    if (check.rule === 'brp') {
        const brpSuccess = check.outcome === 'success' || check.outcome === 'critical_success';
        const brpScales = { hard: '×½', extreme: '×⅕' };
        const scaleStr = brpScales[check.difficulty] || '';
        const attrRaw = check.attr_value != null && scaleStr ? `${check.attr_value}${scaleStr}=` : '';
        let diceNote = '';
        if (check.dice_info && check.dice_info.tens_rolls) {
            const di = check.dice_info;
            const label = di.type === 'bonus' ? '奖励骰' : '惩罚骰';
            const tensStr = di.tens_rolls.map(t => t + '0').join('/');
            diceNote = ` [${label}:${tensStr}→${di.chosen_tens}0]`;
        }
        detail = `d100=${check.roll}${diceNote} ${brpSuccess ? '≤' : '>'} ${attrRaw}${check.threshold}`;
    } else if (check.rule === 'dnd') {
        const mod = check.modifier || 0;
        const modStr = mod >= 0 ? `+${mod}` : `${mod}`;
        detail = `d20=${check.roll}${modStr}=${check.total || check.roll} vs DC${check.threshold}`;
    } else {
        detail = `d100=${check.roll} vs ${check.threshold}`;
    }

    const content = document.getElementById('narrative-content');
    const banner = document.createElement('div');
    banner.className = 'check-result-banner';
    const ms = check.momentum_streak || 0;
    const momTag = ms >= 2 ? ` <span class="check-momentum check-momentum-up" title="连续${ms}次成功">&#9650;${ms}连</span>` : (ms <= -2 ? ` <span class="check-momentum check-momentum-down" title="连续${-ms}次失败">&#9660;${-ms}连</span>` : '');
    banner.innerHTML = `<span class="check-icon">&#127922;</span> <strong>技能检定${skillDesc}</strong> <span style="color:${color};font-weight:600;margin-left:0.3rem">${escapeHtml(label)}</span>${momTag} <span class="check-detail">${detail}</span>`;
    content.appendChild(banner);
    const area = document.getElementById('narrative-area');
    area.scrollTop = area.scrollHeight;
}

function showTriggeredConsequences(consequences) {
    if (!consequences || consequences.length === 0) return;
    const content = document.getElementById('narrative-content');
    const banner = document.createElement('div');
    banner.className = 'consequence-banner';
    banner.innerHTML = consequences.map(c =>
        `<div class="consequence-item"><span class="consequence-icon">&#9888;</span> <strong>延迟后果触发</strong>: ${escapeHtml(c.description || '')}</div>`
    ).join('');
    content.appendChild(banner);
    const area = document.getElementById('narrative-area');
    area.scrollTop = area.scrollHeight;
}

function showAchievedMilestones(milestones) {
    if (!milestones || milestones.length === 0) return;
    const notif = document.createElement('div');
    notif.className = 'milestone-notification';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'notif-close-btn milestone-close';
    closeBtn.innerHTML = '&times;';
    closeBtn.onclick = () => { notif.classList.add('notif-fade-out'); setTimeout(() => notif.remove(), 300); };
    notif.innerHTML = milestones.map(m =>
        `<div class="milestone-item"><span class="milestone-icon">&#127942;</span> <strong>里程碑达成!</strong> ${escapeHtml(m.name || m.id || '')}${m.description ? ` — ${escapeHtml(m.description)}` : ''}</div>`
    ).join('');
    notif.appendChild(closeBtn);
    document.body.appendChild(notif);
}

function showMilestoneProgress(progress) {
    if (!progress || progress.length === 0) return;
    const html = progress.map(p =>
        `<div class="milestone-progress-item">&#127919; 接近达成: <strong>${escapeHtml(p.name)}</strong> (${p.progress}% — ${p.current}/${p.target})</div>`
    ).join('');
    const notif = _createNotifElement('notif-progress', html);
    _pushNotif(notif);
}

function showThresholdEvents(events) {
    if (!events || events.length === 0) return;
    const html = events.map(e => {
        const cls = e.direction === 'above' ? 'change-positive' : 'change-negative';
        return `<div class="${cls}">&#9888; ${escapeHtml(e.attribute)}${e.direction === 'above' ? '升至' : '降至'}${e.value}(阈值${e.threshold}): ${escapeHtml(e.description || '')}</div>`;
    }).join('');
    const notif = _createNotifElement('notif-threshold', html);
    _pushNotif(notif);
}

function showNpcAttitudeNotifications(notifications) {
    if (!notifications || notifications.length === 0) return;
    const html = notifications.map(n => {
        const cls = n.direction === 'up' ? 'change-positive' : 'change-negative';
        const icon = n.direction === 'up' ? '&#9829;' : '&#9760;';
        return `<div class="${cls}">${icon} ${escapeHtml(n.message)}</div>`;
    }).join('');
    const notif = _createNotifElement('notif-npc', html);
    _pushNotif(notif);
}

function showRelationshipEvents(crossings, npcName) {
    if (!crossings || crossings.length === 0) return;
    const dimLabels = { trust: '信任', affection: '好感', fear: '畏惧' };
    const icons = { trust: '\u{1F91D}', affection: '\u{1F497}', fear: '\u{1F630}' };
    const html = crossings.map(c => {
        const dim = dimLabels[c.dim] || c.dim;
        const icon = icons[c.dim] || '\u{1F4AB}';
        const dir = c.direction === 'up' ? '↑' : '↓';
        const cls = c.dim === 'fear' ? (c.direction === 'up' ? 'rel-event-negative' : 'rel-event-positive') : (c.direction === 'up' ? 'rel-event-positive' : 'rel-event-negative');
        const name = npcName || '???';
        const msgs = {
            trust: { up: { 30: '开始信任你', 60: '对你十分信任', 80: '完全信赖你' }, down: { 80: '信任动摇了', 60: '变得警惕', 30: '不再信任你' } },
            affection: { up: { 30: '对你产生好感', 60: '很喜欢你', 80: '对你产生了深厚的感情' }, down: { 80: '热情消退', 60: '变得疏远', 30: '对你失去好感' } },
            fear: { up: { 30: '开始畏惧你', 60: '非常害怕你', 80: '对你恐惧至极' }, down: { 80: '不再那么害怕', 60: '恐惧消退', 30: '不再畏惧你' } },
        };
        const msg = msgs[c.dim]?.[c.direction]?.[c.threshold] || `${dim}${dir}${c.threshold}`;
        return `<div class="rel-event-card ${cls}">${icon} ${escapeHtml(name)}${msg}</div>`;
    }).join('');
    const notif = _createNotifElement('notif-rel-event', html);
    _pushNotif(notif);
}

function showNpcInterjections(interjections) {
    if (!interjections || interjections.length === 0) return;
    const dn = currentState?.display_names || {};
    const npcs = currentState?.npcs || {};
    const content = document.getElementById('narrative-content');
    for (const ij of interjections) {
        if (!ij.npc_id || !ij.text) continue;
        const npcData = npcs[ij.npc_id];
        const name = dn[ij.npc_id] || (typeof npcData === 'object' ? (npcData.name || ij.npc_id) : ij.npc_id);
        const bubble = document.createElement('div');
        bubble.className = 'npc-interject';
        bubble.innerHTML = `<span class="npc-interject-name">${escapeHtml(name)}</span> ${escapeHtml(ij.text)}`;
        content.appendChild(bubble);
    }
}

function showGameOver(gameOver, stats) {
    if (!gameOver) return;
    const reason = typeof gameOver === 'object' ? (gameOver.reason || gameOver.ending || '游戏结束') : String(gameOver);
    const content = document.getElementById('narrative-content');
    const banner = document.createElement('div');
    banner.className = 'game-over-banner';

    let statsHtml = '';
    if (stats && typeof stats === 'object') {
        const playTime = stats.play_time_seconds > 0 ? _formatPlayTime(stats.play_time_seconds) : '—';
        statsHtml = `<div class="game-over-stats">
            <div class="go-stat"><span class="go-stat-val">${stats.total_turns || 0}</span><span class="go-stat-label">回合</span></div>
            <div class="go-stat"><span class="go-stat-val">${playTime}</span><span class="go-stat-label">游戏时长</span></div>
            <div class="go-stat"><span class="go-stat-val">${stats.locations_visited || 0}</span><span class="go-stat-label">探索地点</span></div>
            <div class="go-stat"><span class="go-stat-val">${stats.npcs_met || 0}</span><span class="go-stat-label">结识角色</span></div>
            <div class="go-stat"><span class="go-stat-val">${stats.milestones_achieved || 0}</span><span class="go-stat-label">达成里程碑</span></div>
            <div class="go-stat"><span class="go-stat-val">${stats.items_collected || 0}</span><span class="go-stat-label">获得物品</span></div>
        </div>`;
    }
    banner.innerHTML = `<div class="game-over-title">&#127937; 游戏结束</div><div class="game-over-reason">${escapeHtml(reason)}</div>${statsHtml}`;
    content.appendChild(banner);
    // Disable further input
    document.querySelectorAll('.choice-btn').forEach(b => b.disabled = true);
    const input = document.getElementById('freeform-input');
    if (input) input.disabled = true;
    const area = document.getElementById('narrative-area');
    area.scrollTop = area.scrollHeight;
}

// ================================================
//  ADVENTURE LOG
// ================================================

async function loadAdventureLog() {
    if (!currentSaveId) return;
    try {
        const log = await API.get(`/api/game/${currentSaveId}/adventure-log`);
        renderAdventureLog(log);
    } catch (e) {
        console.error('Failed to load adventure log:', e);
    }
}

function renderAdventureLog(log) {
    const container = document.getElementById('adventure-log-content');
    if (!container) return;
    if (!log || log.length === 0) {
        container.innerHTML = '<span style="color:var(--text-muted);font-size:0.78rem">暂无记录</span>';
        return;
    }
    // Show most recent 20 entries, newest first
    const entries = log.slice(-20).reverse();
    container.innerHTML = entries.map(e => {
        const turnLabel = e.turn ? `第${e.turn}回合` : '';
        const items = (e.events || []).map(ev => {
            const text = typeof ev === 'object' ? (ev.text || ev.description || '') : String(ev);
            const t = ev.type || 'event';
            const icons = { event:'&#128276;', consequence:'&#9888;', milestone:'&#127942;', location:'&#128205;', discovery:'&#128161;', threshold:'&#9888;', decision:'&#9998;', relationship:'&#128149;' };
            const classes = { event:'log-event', consequence:'log-consequence', milestone:'log-milestone', location:'log-location', discovery:'log-discovery', threshold:'log-consequence', decision:'log-decision', relationship:'log-relationship' };
            return `<div class="${classes[t] || 'log-event'}">${icons[t] || ''} ${escapeHtml(text)}</div>`;
        });
        return `<div class="log-entry"><div class="log-turn">${escapeHtml(turnLabel)}</div>${items.join('')}</div>`;
    }).join('');
}

// ================================================
//  LOREBOOK HINTS (activated entries visible to player)
// ================================================

function showDataBankPanel(loreEntries, databankHits) {
    const container = document.getElementById('databank-lore-entries');
    if (!container) return;
    const hasLore = loreEntries && loreEntries.length > 0;
    const hasBank = databankHits && databankHits.length > 0;
    if (!hasLore && !hasBank) {
        container.innerHTML = '<span style="color:var(--text-muted)">暂无本回合知识</span>';
        return;
    }
    let html = '';
    if (hasLore) {
        html += loreEntries.map(e => `
            <div class="lore-card">
                <div class="lore-card-title">${escapeHtml(e.comment || e.id || '未命名')}</div>
                <div class="lore-card-content">${escapeHtml(e.content || '').replace(/\n/g, '<br>')}</div>
            </div>
        `).join('');
    }
    if (hasBank) {
        html += databankHits.map(h => `
            <div class="lore-card databank-card">
                <div class="lore-card-title">&#128196; ${escapeHtml(h.filename || '文档')}</div>
                <div class="lore-card-content">${escapeHtml(h.text || '').replace(/\n/g, '<br>')}</div>
            </div>
        `).join('');
    }
    const total = (loreEntries ? loreEntries.length : 0) + (databankHits ? databankHits.length : 0);
    container.innerHTML = `<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.3rem">本回合命中 ${total} 条</div>${html}`;
}

function showWarnings(warnings) {
    if (!warnings || warnings.length === 0) return;
    const content = document.getElementById('narrative-content');
    if (!content) return;
    const div = document.createElement('div');
    div.className = 'turn-warnings';
    div.innerHTML = warnings.map(w => `<span class="warning-item">⚠ ${escapeHtml(w)}</span>`).join('');
    content.appendChild(div);
}

function showTriggerNotifications(notifications) {
    if (!notifications || notifications.length === 0) return;
    const branches = notifications.filter(n => String(n).startsWith('【剧情分支】'));
    const visible = notifications.filter(n => !String(n).startsWith('【剧情分支】'));
    for (const [items, className] of [[visible, 'notif-trigger'], [branches, 'notif-story-branch']]) {
        if (!items.length) continue;
        const html = items.map(n => `<div class="change-neutral">&#9889; ${escapeHtml(String(n))}</div>`).join('');
        _pushNotif(_createNotifElement(className, html));
    }
}

function showStoryTreeUpdates(updates) {
    if (!updates) return;
    if (typeof markStoryTreeUpdates === 'function') markStoryTreeUpdates(updates);
    const items = [];
    for (const node of (updates.newly_completed || [])) {
        items.push(`<div class="change-positive">&#9989; 剧情完成：<strong>${escapeHtml(node.name || node.id)}</strong></div>`);
    }
    for (const node of (updates.newly_active || [])) {
        items.push(`<div class="change-neutral">&#127381; 新剧情开启：<strong>${escapeHtml(node.name || node.id)}</strong></div>`);
    }
    if (items.length === 0) return;
    const notif = _createNotifElement('notif-story-tree', items.join(''));
    _pushNotif(notif);
}

function showNews(newsItems) {
    if (!newsItems || newsItems.length === 0) return;
    const content = document.getElementById('narrative-content');
    if (!content) return;
    for (const news of newsItems) {
        const card = document.createElement('div');
        card.className = 'news-card';
        card.innerHTML = `<div class="news-card-header"><span class="news-card-icon">\u{1F4F0}</span><span class="news-card-title">${escapeHtml(news.title || '快讯')}</span></div>`
            + `<div class="news-card-body">${escapeHtml(news.content || '')}</div>`
            + (news.time ? `<div class="news-card-meta">${escapeHtml(news.time)}</div>` : '');
        content.appendChild(card);
    }
}

function showMemoryEchoes(state) {
    const echoes = (state || {}).memory_echoes;
    if (!echoes || echoes.length === 0) return;
    const content = document.getElementById('narrative-content');
    if (!content) return;
    for (const echo of echoes) {
        const card = document.createElement('div');
        card.className = 'memory-echo-card' + (echo.emotional_weight === 'high' ? ' memory-echo-high' : '');
        const typeIcons = { location_revisit: '\u{1F3DA}', npc_reunion: '\u{1F91D}', consequence_echo: '\u{1F517}', moral_echo: '\u{2696}' };
        const icon = typeIcons[echo.type] || '\u{1F4AD}';
        card.innerHTML = `<div class="memory-echo-trigger">${icon} ${escapeHtml(echo.trigger)}</div>`
            + `<div class="memory-echo-memory">${escapeHtml(echo.memory)}${echo.turns_ago ? ` <span class="memory-echo-ago">(${echo.turns_ago}回合前)</span>` : ''}</div>`;
        content.appendChild(card);
    }
}

function showChoiceRipples(state) {
    const ripples = (state || {}).choice_ripples || [];
    if (!ripples.length) return;
    const content = document.getElementById('narrative-content');
    if (!content) return;
    const wrapper = document.createElement('div');
    wrapper.className = 'ripple-cards';
    const impactIcons = { positive: '🌱', negative: '🔥', neutral: '🦋' };
    wrapper.innerHTML = `<details class="ripple-details"><summary class="ripple-summary">🦋 因果回响 (${ripples.length})</summary>` +
        ripples.map(r => {
            const icon = impactIcons[r.impact_type] || '🦋';
            return `<div class="ripple-card ripple-${r.impact_type || 'neutral'}"><span class="ripple-icon">${icon}</span><span class="ripple-text"><strong>${escapeHtml(r.current_event)}</strong> — 源于第${r.cause_turn}回合：${escapeHtml(r.cause_action)}</span></div>`;
        }).join('') + '</details>';
    content.appendChild(wrapper);
}

// ================================================
//  HISTORY SUMMARY
// ================================================

// ================================================
//  NARRATIVE TEXT ENHANCEMENT
// ================================================

function enhanceNarrative(html) {
    if (!html) return html;

    // Helper: apply replacer only to text segments (skips HTML tags)
    function replaceTextOnly(src, fn) {
        const segs = src.split(/(<[^>]+>)/g);
        for (let i = 0; i < segs.length; i++) {
            if (!segs[i] || segs[i].charAt(0) === '<') continue;
            segs[i] = fn(segs[i]);
        }
        return segs.join('');
    }

    // 0. Build NPC color map
    const npcColors = {};
    if (currentState) {
        const npcs = currentState.npcs || {};
        const palette = [
            'var(--npc-color-1, #e8a87c)', 'var(--npc-color-2, #85cdca)',
            'var(--npc-color-3, #d5a6bd)', 'var(--npc-color-4, #b5d99c)',
            'var(--npc-color-5, #f6c48a)', 'var(--npc-color-6, #a2c4e0)',
            'var(--npc-color-7, #d4a0a0)', 'var(--npc-color-8, #c3b1e1)',
        ];
        let ci = 0;
        for (const [npcId, npcData] of Object.entries(npcs)) {
            const name = (typeof npcData === 'object') ? (npcData.name || npcId) : npcId;
            npcColors[name] = palette[ci % palette.length];
            if (npcId !== name) npcColors[npcId] = palette[ci % palette.length];
            ci++;
        }
    }

    // Pass 1: Speaker-attributed dialogue (own split-join cycle)
    const speakerPattern = currentState ? Object.keys(npcColors).sort((a, b) => b.length - a.length).map(escapeRegex).join('|') : '';
    if (speakerPattern) {
        const speechVerbs = '说|道|喊|问|答|叫|吼|笑|叹|喃喃|低声|冷声|沉声|轻声|高声|怒声|淡淡';
        const speechRe = new RegExp('(' + speakerPattern + ')(.{0,6}(?:' + speechVerbs + ')[^\\u201c\\u300c]{0,10}?)([\\u201c\\u300c\\u300e])([^\\u201d\\u300d\\u300f]{1,200})([\\u201d\\u300d\\u300f])', 'g');
        html = replaceTextOnly(html, seg => seg.replace(speechRe, (m, name, verb, oq, dialogue, cq) => {
            const color = npcColors[name] || 'var(--info)';
            return name + verb + '<span class="dialogue" style="color:' + color + '">' + oq + dialogue + cq + '</span>';
        }));
    }

    // Pass 2: Fallback dialogue — own split-join so Pass 1 spans are recognized as tags
    html = (function(src) {
        const segs = src.split(/(<[^>]+>)/g);
        for (let i = 0; i < segs.length; i++) {
            if (!segs[i] || segs[i].charAt(0) === '<') continue;
            // Skip text that's already inside a dialogue span (check preceding tag)
            const prevTag = (i > 0) ? segs[i - 1] : '';
            if (prevTag.includes('class="dialogue"')) continue;
            let seg = segs[i];
            seg = seg.replace(/“([^“”]{1,200})”/g, '<span class="dialogue">“$1”</span>');
            seg = seg.replace(/&quot;([^&<>]{1,200})&quot;/g, '<span class="dialogue">&quot;$1&quot;</span>');
            seg = seg.replace(/「([^」]{1,200})」/g, '<span class="dialogue">「$1」</span>');
            seg = seg.replace(/『([^』]{1,200})』/g, '<span class="dialogue">『$1』</span>');
            segs[i] = seg;
        }
        return segs.join('');
    })(html);

    // Pass 3: Entity-link highlighting — own split-join so dialogue spans are skipped
    if (currentState) {
        const entities = [];
        const dn = currentState.display_names || {};
        const npcs = currentState.npcs || {};
        for (const [npcId, npcData] of Object.entries(npcs)) {
            const name = (typeof npcData === 'object') ? (npcData.name || npcId) : npcId;
            if (name && name.length >= 2) entities.push({ name, type: 'npc', id: npcId, displayName: name });
            if (npcId !== name && npcId.length >= 2) entities.push({ name: npcId, type: 'npc', id: npcId, displayName: name });
        }
        const visLocs = currentState.visible_locations || [];
        for (const locId of visLocs) {
            const locName = dn[locId] || locId;
            if (locName && locName.length >= 2) entities.push({ name: locName, type: 'location', id: locId, displayName: locName });
            if (locId !== locName && locId.length >= 2) entities.push({ name: locId, type: 'location', id: locId, displayName: locName });
        }
        const factions = currentState.faction_reputation || {};
        for (const orgId of Object.keys(factions)) {
            const orgName = dn[orgId] || orgId;
            if (orgName && orgName.length >= 2) entities.push({ name: orgName, type: 'org', id: orgId, displayName: orgName });
            if (orgId !== orgName && orgId.length >= 2) entities.push({ name: orgId, type: 'org', id: orgId, displayName: orgName });
        }
        entities.sort((a, b) => b.name.length - a.name.length);
        if (entities.length > 0) {
            const entityMap = {};
            const patterns = [];
            for (const ent of entities) {
                const escaped = escapeRegex(ent.name);
                if (!entityMap[ent.name]) { entityMap[ent.name] = ent; patterns.push(escaped); }
            }
            const entityRe = new RegExp('(' + patterns.join('|') + ')', 'g');
            html = replaceTextOnly(html, seg => seg.replace(entityRe, (match) => {
                const ent = entityMap[match];
                if (!ent) return match;
                return '<span class="entity-link" data-type="' + ent.type + '" data-id="' + escapeAttr(ent.id) + '">' + escapeHtml(ent.displayName) + '</span>';
            }));
        }
    }

    return html;
}

function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function escapeAttr(str) {
    return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Delegate click on entity-link spans
document.addEventListener('click', function(e) {
    const el = e.target.closest('.entity-link');
    if (el) {
        e.stopPropagation();
        showEntityInfo(el.dataset.type, el.dataset.id);
        return;
    }
    // Click to expand/collapse old turn blocks
    // Skip if click originated inside a think-block (has its own toggle)
    if (e.target.closest('.think-block')) return;
    const turnBlock = e.target.closest('.turn-block:not(.latest).is-truncated');
    if (turnBlock) {
        if (e.target.closest('.turn-history-details, .turn-recap')) {
            turnBlock.classList.add('expanded');
            return;
        }
        turnBlock.classList.toggle('expanded');
        if (!turnBlock.classList.contains('expanded')) _syncTurnOverflow();
    }
});

function showEntityInfo(type, id) {
    const modal = document.getElementById('material-detail-modal');
    const content = document.getElementById('material-detail-content');
    if (!modal || !content) return;

    if (type === 'npc' && currentState) {
        const npcData = (currentState.npcs || {})[id];
        if (!npcData) {
            const dn = currentState.display_names || {};
            const fallbackName = dn[id] || id;
            content.innerHTML = '<div class="entity-popup"><h3>' + escapeHtml(fallbackName) + '</h3><div class="ep-muted">暂无详细信息</div><div class="result-actions"><button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button></div></div>';
            modal.style.display = 'flex';
            return;
        }
        const name = (typeof npcData === 'object') ? (npcData.name || id) : id;
        const att = (typeof npcData === 'object') ? (npcData.attitude_toward_player ?? 50) : npcData;
        const rel = currentState.player?.relationships?.[id];
        const npcKnown = (typeof npcData === 'object') ? (npcData.known !== false) : true;
        const npcMet = (typeof npcData === 'object') ? (npcData.met !== false) : true;

        const sections = [];

        // Title & personality
        const title = npcData.title || '';
        const personality = npcData.personality || '';
        if (title || personality) {
            const sub = [title, personality].filter(Boolean).join(' · ');
            sections.push('<div class="ep-subtitle">' + escapeHtml(sub) + '</div>');
        }

        // Bio
        if (npcData.bio) {
            sections.push('<div class="ep-bio">' + escapeHtml(npcData.bio) + '</div>');
        }

        // Attitude bar
        const attColor = att >= 70 ? 'var(--success, #4caf50)' : att >= 40 ? 'var(--warning, #ff9800)' : 'var(--danger, #f44336)';
        sections.push('<div class="ep-section"><div class="ep-label">态度</div><div class="ep-bar-wrap"><div class="ep-bar" style="width:' + att + '%;background:' + attColor + '"></div><span class="ep-bar-val">' + att + '</span></div></div>');

        // Relationship (trust/affection/fear)
        if (npcKnown && npcMet && rel && typeof rel === 'object' && ('trust' in rel || 'affection' in rel || 'fear' in rel)) {
            const trust = rel.trust ?? 50;
            const affection = rel.affection ?? 50;
            const fear = rel.fear ?? 0;
            sections.push('<div class="ep-section"><div class="ep-label">关系维度</div>' +
                '<div class="ep-rel-row"><span class="ep-rel-name">信任</span><div class="ep-bar-wrap"><div class="ep-bar" style="width:' + trust + '%;background:var(--npc-color-6,#a2c4e0)"></div><span class="ep-bar-val">' + trust + '</span></div></div>' +
                '<div class="ep-rel-row"><span class="ep-rel-name">好感</span><div class="ep-bar-wrap"><div class="ep-bar" style="width:' + affection + '%;background:var(--npc-color-3,#d5a6bd)"></div><span class="ep-bar-val">' + affection + '</span></div></div>' +
                '<div class="ep-rel-row"><span class="ep-rel-name">畏惧</span><div class="ep-bar-wrap"><div class="ep-bar" style="width:' + fear + '%;background:var(--npc-color-7,#d4a0a0)"></div><span class="ep-bar-val">' + fear + '</span></div></div>' +
                '</div>');
        } else if (rel != null && typeof rel !== 'object') {
            sections.push('<div class="ep-section"><div class="ep-label">关系</div><div class="ep-val">' + escapeHtml(String(rel)) + '</div></div>');
        }

        // Opinion (NPC's current opinion of player)
        if (npcData.opinion) {
            sections.push('<div class="ep-section"><div class="ep-label">当前看法</div><div class="ep-val ep-opinion">' + escapeHtml(npcData.opinion) + '</div></div>');
        }
        if (npcData.relationship_desc) {
            sections.push('<div class="ep-section"><div class="ep-label">关系描述</div><div class="ep-val">' + escapeHtml(npcData.relationship_desc) + '</div></div>');
        }

        // Location
        const loc = npcData.current_location === currentState.player?.location ? npcData.current_location : '';
        if (loc) {
            const dn = currentState.display_names || {};
            const locName = dn[loc] || loc;
            sections.push('<div class="ep-section"><div class="ep-label">所在位置</div><div class="ep-val">' + escapeHtml(locName) + '</div></div>');
        }

        // Organizations
        if (npcData.organizations && npcData.organizations.length) {
            const orgNames = npcData.organizations.map(function(o) {
                const dn = currentState.display_names || {};
                return escapeHtml(dn[o.org_id || o] || o.org_id || o) + (o.role ? ' (' + escapeHtml(o.role) + ')' : '');
            }).join(', ');
            sections.push('<div class="ep-section"><div class="ep-label">所属组织</div><div class="ep-val">' + orgNames + '</div></div>');
        }

        // Superior
        if (npcData.superior) {
            const dn = currentState.display_names || {};
            const supName = dn[npcData.superior] || npcData.superior;
            sections.push('<div class="ep-section"><div class="ep-label">上级</div><div class="ep-val">' + escapeHtml(supName) + '</div></div>');
        }

        // Encounter & dialogue counts
        const encCount = (currentState.npc_encounter_counts || {})[id] || 0;
        const dlgCount = (currentState.npc_dialogue_counts || {})[id] || 0;
        if (encCount || dlgCount) {
            let statsHtml = '';
            if (encCount) statsHtml += '<span>相遇 ' + encCount + ' 次</span>';
            if (dlgCount) statsHtml += '<span>对话 ' + dlgCount + ' 次</span>';
            sections.push('<div class="ep-section ep-stats">' + statsHtml + '</div>');
        }

        // Met status
        if (!npcMet) {
            sections.push('<div class="ep-section ep-unmet">尚未正式见面</div>');
        }

        content.innerHTML = '<div class="entity-popup"><h3>' + escapeHtml(name) + '</h3>' + sections.join('') + '<div class="result-actions"><button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button></div></div>';
        modal.style.display = 'flex';
    } else if (type === 'location') {
        const dn = currentState?.display_names || {};
        const locDisplayName = dn[id] || id;
        var locSecs = [];
        // Description
        const locDesc = (currentState.location_descriptions || {})[id];
        if (locDesc) {
            locSecs.push('<div class="ep-bio">' + escapeHtml(locDesc) + '</div>');
        }
        // NPCs here
        const npcsHere = [];
        if (currentState && currentState.npcs && id === currentState.player?.location) {
            for (const [nid, nd] of Object.entries(currentState.npcs)) {
                if (!nd || typeof nd !== 'object' || nd.known === false) continue;
                if (nd.current_location === id) npcsHere.push(nd.name || nid);
            }
        }
        if (npcsHere.length) {
            locSecs.push('<div class="ep-section"><div class="ep-label">此处NPC</div><div class="ep-val">' + npcsHere.map(escapeHtml).join(', ') + '</div></div>');
        }
        // Connected locations
        const conns = (currentState.location_connections || {})[id];
        if (conns && conns.length) {
            const connNames = conns.map(function(c) { return escapeHtml(dn[c] || c); }).join(', ');
            locSecs.push('<div class="ep-section"><div class="ep-label">相连区域</div><div class="ep-val">' + connNames + '</div></div>');
        }
        content.innerHTML = '<div class="entity-popup"><h3>' + escapeHtml(locDisplayName) + '</h3>' + locSecs.join('') + '<div class="result-actions"><button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button></div></div>';
        modal.style.display = 'flex';
    } else if (type === 'org') {
        const dn = currentState?.display_names || {};
        const orgName = dn[id] || id;
        var orgSecs = [];
        // Reputation
        const rep = (currentState.faction_reputation || {})[id];
        if (rep) {
            const repVal = rep.value ?? 50;
            const repTitle = rep.title || '';
            const repColor = repVal >= 70 ? 'var(--success,#4caf50)' : repVal >= 40 ? 'var(--warning,#ff9800)' : 'var(--danger,#f44336)';
            orgSecs.push('<div class="ep-section"><div class="ep-label">声望' + (repTitle ? ' · ' + escapeHtml(repTitle) : '') + '</div><div class="ep-bar-wrap"><div class="ep-bar" style="width:' + repVal + '%;background:' + repColor + '"></div><span class="ep-bar-val">' + repVal + '</span></div></div>');
        }
        // Members (NPCs in this org)
        const members = [];
        if (currentState.npcs) {
            for (const [nid, nd] of Object.entries(currentState.npcs)) {
                if (typeof nd !== 'object') continue;
                const orgs = nd.organizations || [];
                for (const o of orgs) {
                    const oid = (typeof o === 'object') ? (o.org_id || '') : o;
                    if (oid === id) {
                        const role = (typeof o === 'object') ? o.role : '';
                        members.push(escapeHtml(nd.name || nid) + (role ? ' (' + escapeHtml(role) + ')' : ''));
                        break;
                    }
                }
            }
        }
        if (members.length) {
            orgSecs.push('<div class="ep-section"><div class="ep-label">已知成员</div><div class="ep-val">' + members.join(', ') + '</div></div>');
        }
        content.innerHTML = '<div class="entity-popup"><h3>' + escapeHtml(orgName) + '</h3>' + orgSecs.join('') + '<div class="result-actions"><button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button></div></div>';
        modal.style.display = 'flex';
    } else {
        content.innerHTML = '<div class="entity-popup"><h3>' + escapeHtml(id) + '</h3><div class="ep-muted">暂无详细信息</div><div class="result-actions"><button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button></div></div>';
        modal.style.display = 'flex';
    }
}

// ================================================
//  HISTORY SUMMARY
// ================================================

async function viewFullSummary() {
    if (!currentSaveId) return;
    try {
        const data = await API.get(`/api/game/${currentSaveId}/summary`);
        const modal = document.getElementById('material-detail-modal');
        const content = document.getElementById('material-detail-content');
        const summary = data.summary || '暂无摘要（需要更多回合积累）';
        const frozen = currentState && currentState.summary_frozen;
        content.innerHTML = `
            <h3>历史摘要 — 第${data.turn || 0}回合</h3>
            <div style="margin-top:0.8rem;line-height:1.8;color:var(--text-secondary);white-space:pre-wrap">${escapeHtml(summary)}</div>
            <div class="result-actions" style="margin-top:1rem;display:flex;gap:0.6rem;align-items:center">
                <button onclick="toggleSummaryFreeze()" class="btn-secondary" id="btn-summary-freeze">${frozen ? '解冻摘要' : '冻结摘要'}</button>
                ${frozen ? '<span class="summary-frozen-badge">已冻结</span>' : ''}
                <button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button>
            </div>
        `;
        modal.style.display = 'flex';
    } catch (e) {
        alert('加载摘要失败: ' + e.message);
    }
}

async function toggleSummaryFreeze() {
    if (!currentSaveId) return;
    try {
        const result = await API.post(`/api/game/${currentSaveId}/summary/freeze`);
        if (currentState) currentState.summary_frozen = result.frozen;
        const btn = document.getElementById('btn-summary-freeze');
        if (btn) btn.textContent = result.frozen ? '解冻摘要' : '冻结摘要';
        const badge = btn && btn.parentElement.querySelector('.summary-frozen-badge');
        if (result.frozen && !badge) {
            const span = document.createElement('span');
            span.className = 'summary-frozen-badge';
            span.textContent = '已冻结';
            btn.insertAdjacentElement('afterend', span);
        } else if (!result.frozen && badge) {
            badge.remove();
        }
        updateSummaryFreezeIndicator();
    } catch (e) {
        alert('切换冻结状态失败: ' + e.message);
    }
}

function updateSummaryFreezeIndicator() {
    const btn = document.querySelector('.btn-summary');
    if (!btn) return;
    const frozen = currentState && currentState.summary_frozen;
    btn.textContent = frozen ? '查看完整摘要 (已冻结)' : '查看完整摘要';
    btn.classList.toggle('summary-frozen', !!frozen);
}

// ================================================
//  NPC CHAT SYSTEM
// ================================================

let _npcChatId = null;
let _npcChatVersion = 0;
let _npcChatRequest = null;

function openNpcChat(npcId) {
    if (!currentSaveId || !currentState) return;
    if (isStreaming) {
        alert('推演进行中，请等待本次行动结束再对话');
        return;
    }
    const unavailable = NpcPresence.talkBlockReason(npcId, currentState,
        NpcPresence.getCachedSchedules(currentSaveId, currentState));
    if (unavailable) {
        alert(unavailable);
        return;
    }
    if (document.getElementById('main-map-view')?.hidden === false &&
        !(currentState.companions || []).includes(npcId) &&
        !NpcPresence.hasCurrentContact(npcId, currentState) &&
        typeof prepareMainMapNpcContact === 'function') {
        prepareMainMapNpcContact(npcId, currentSaveId, currentState);
        return;
    }
    _npcChatId = npcId;
    _npcChatVersion++;
    const npcData = (currentState.npcs || {})[npcId];
    const name = (typeof npcData === 'object') ? (npcData.name || npcId) : npcId;
    const att = (typeof npcData === 'object') ? (npcData.attitude_toward_player ?? 50) : npcData;

    document.getElementById('npc-chat-name').textContent = name;

    // Build info line
    const known = (typeof npcData === 'object') ? (npcData.known !== false) : true;
    const met = (typeof npcData === 'object') ? (npcData.met !== false) : known;
    const rel = currentState.player?.relationships?.[npcId];
    let infoHtml = '';
    if (known && att >= 0) {
        infoHtml = `<span>态度: ${att}/100</span>`;
        if (met && rel && typeof rel === 'object' && ('trust' in rel || 'affection' in rel || 'fear' in rel)) {
            infoHtml += ` <span>信任${rel.trust ?? 50} 好感${rel.affection ?? 50} 畏惧${rel.fear ?? 0}</span>`;
        }
    } else {
        infoHtml = '<span style="color:var(--text-muted)">态度未知</span>';
    }
    document.getElementById('npc-chat-info').innerHTML = infoHtml;

    document.getElementById('npc-chat-messages').innerHTML = `
        <div class="npc-msg system-msg">开始与 ${escapeHtml(name)} 的对话。对话会推进游戏时间，回复后同步世界状态。</div>`;
    document.getElementById('npc-chat-modal').style.display = 'flex';
    document.getElementById('npc-chat-input').focus();
}

function closeNpcChat() {
    document.getElementById('npc-chat-modal').style.display = 'none';
    _npcChatId = null;
    _npcChatVersion++;
    // Refresh status panel since attitudes may have changed
    if (currentState) updateStatusPanel(currentState);
}

function _npcChatBlockReason(npcId, state) {
    const reason = NpcPresence.talkBlockReason(npcId, state,
        NpcPresence.getCachedSchedules(currentSaveId, state));
    if (reason) return reason;
    if (document.getElementById('main-map-view')?.hidden === false &&
        !(state.companions || []).includes(npcId) && !NpcPresence.hasCurrentContact(npcId, state)) {
        return '接触已中断，请重新寻找该角色';
    }
    return '';
}

async function sendNpcMessage() {
    if (!currentSaveId || !_npcChatId || !currentState) return;
    const input = document.getElementById('npc-chat-input');
    if (input.disabled || isStreaming) return;
    const msg = input.value.trim();
    if (!msg) return;
    const reason = _npcChatBlockReason(_npcChatId, currentState);
    if (reason) {
        alert(reason);
        return;
    }
    const saveId = currentSaveId;
    const npcId = _npcChatId;
    const chatVersion = _npcChatVersion;
    const loadSequence = _saveLoadSequence;
    const isCurrentGame = () => currentSaveId === saveId && _saveLoadSequence === loadSequence;
    const isCurrentChat = () => isCurrentGame() && _npcChatId === npcId && _npcChatVersion === chatVersion;
    const request = {};
    _npcChatRequest = request;
    isStreaming = true;
    input.value = '';
    input.disabled = true;

    const msgArea = document.getElementById('npc-chat-messages');
    // Show player message
    msgArea.insertAdjacentHTML('beforeend', `<div class="npc-msg player-msg">${escapeHtml(msg)}</div>`);
    // G3: 流式 NPC 对话
    msgArea.insertAdjacentHTML('beforeend', `<div class="npc-msg npc-response" id="npc-typing"></div>`);
    msgArea.scrollTop = msgArea.scrollHeight;
    const typingEl = document.getElementById('npc-typing');

    let committed = false;
    try {
        const resp = await fetch(`/api/game/${saveId}/talk/${npcId}/stream`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ message: msg }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalResult = null;

        while (true) {
            let packet;
            try {
                packet = await reader.read();
            } catch (error) {
                if (finalResult) break;
                throw error;
            }
            const { done, value } = packet;
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const payload = line.slice(6).trim();
                if (payload === '[DONE]') continue;
                try {
                    const chunk = JSON.parse(payload);
                    if (chunk.type === 'text') {
                        if (isCurrentChat()) {
                            typingEl.insertAdjacentHTML('beforeend', escapeHtml(chunk.content).replace(/\n/g, '<br>'));
                            msgArea.scrollTop = msgArea.scrollHeight;
                        }
                    } else if (chunk.type === 'narrative_revised') {
                        if (isCurrentChat()) {
                            typingEl.innerHTML = escapeHtml(chunk.content).replace(/\n/g, '<br>');
                            msgArea.scrollTop = msgArea.scrollHeight;
                        }
                    } else if (chunk.type === 'final') {
                        finalResult = chunk;
                    }
                } catch(_) {}
            }
        }

        if (!finalResult) throw new Error('对话未完成，请重试');
        if (finalResult.error) throw new Error(finalResult.error);
        committed = true;
        if (!isCurrentGame()) return;

        if (finalResult.state) currentState = finalResult.state;
        if (isCurrentChat()) {
            if (finalResult.talk_check) {
                const tc = finalResult.talk_check;
                const tcLabels = { critical_success: '大成功', success: '成功', failure: '失败', critical_failure: '大失败' };
                const tcColors = { critical_success: 'var(--gold)', success: 'var(--success)', failure: 'var(--danger)', critical_failure: 'var(--danger)' };
                const tcDiff = { extreme: '极难', hard: '困难', medium: '普通', easy: '简单' };
                const tcLabel = tcLabels[tc.outcome] || tc.outcome;
                const tcColor = tcColors[tc.outcome] || 'var(--text-secondary)';
                const tcAttr = tc.related_attribute || '';
                const tcDiffLabel = tcDiff[tc.difficulty] || '';
                msgArea.insertAdjacentHTML('beforeend', `<div class="npc-msg system-msg">&#127922; 对话检定 [${escapeHtml(tcAttr)}·${tcDiffLabel}] <span style="color:${tcColor};font-weight:600">${tcLabel}</span> <span style="opacity:0.7">${tc.rule === 'brp' ? `d100=${tc.roll} ≤ 技能${tc.threshold}` : tc.rule === 'dnd' ? `d20=${tc.roll}${(tc.modifier||0)>=0?'+':''}${tc.modifier||0}=${tc.total||tc.roll} vs DC${tc.threshold}` : `d100=${tc.roll} vs ${tc.threshold}`}</span></div>`);
            }
            if (finalResult.state_changes && finalResult.state_changes.length > 0) {
                const changes = finalResult.state_changes.map(sc => {
                    const diff = (sc.new ?? 0) - (sc.old ?? 0);
                    const sign = diff > 0 ? '+' : '';
                    return `${sc.target.split('.').pop()} ${sign}${diff}`;
                }).join(', ');
                msgArea.insertAdjacentHTML('beforeend', `<div class="npc-msg system-msg">态度变化: ${changes}</div>`);
            }
            if (finalResult.relationship_crossings && finalResult.relationship_crossings.length > 0) {
                const dn = currentState?.display_names || {};
                const nd = (currentState?.npcs || {})[npcId];
                const rName = dn[npcId] || (typeof nd === 'object' ? (nd.name || npcId) : npcId);
                showRelationshipEvents(finalResult.relationship_crossings, rName);
            }
            if (finalResult.state) {
                const npcData = (currentState.npcs || {})[npcId];
                const att = (typeof npcData === 'object') ? (npcData.attitude_toward_player ?? 50) : npcData;
                const known = (typeof npcData === 'object') ? (npcData.known !== false) : true;
                const met = (typeof npcData === 'object') ? (npcData.met !== false) : known;
                const rel = currentState.player?.relationships?.[npcId];
                let infoHtml = '';
                if (known && att >= 0) {
                    infoHtml = `<span>态度: ${att}/100</span>`;
                    if (met && rel && typeof rel === 'object' && ('trust' in rel || 'affection' in rel || 'fear' in rel)) {
                        infoHtml += ` <span>信任${rel.trust ?? 50} 好感${rel.affection ?? 50} 畏惧${rel.fear ?? 0}</span>`;
                    }
                } else {
                    infoHtml = '<span style="color:var(--text-muted)">态度未知</span>';
                }
                document.getElementById('npc-chat-info').innerHTML = infoHtml;
            }
            const oldQr = msgArea.querySelector('.npc-quick-replies');
            if (oldQr) oldQr.remove();
            const blocked = _npcChatBlockReason(npcId, currentState);
            if (blocked) {
                msgArea.insertAdjacentHTML('beforeend', `<div class="npc-msg system-msg">${escapeHtml(blocked)}</div>`);
            } else if (finalResult.quick_replies && finalResult.quick_replies.length > 0) {
                const qrDiv = document.createElement('div');
                qrDiv.className = 'npc-quick-replies';
                finalResult.quick_replies.forEach(text => {
                    const btn = document.createElement('button');
                    btn.className = 'quick-reply-btn';
                    btn.textContent = text;
                    btn.onclick = () => {
                        input.value = text;
                        qrDiv.remove();
                        sendNpcMessage();
                    };
                    qrDiv.appendChild(btn);
                });
                msgArea.appendChild(qrDiv);
            }
        }
        if (finalResult.state) {
            if (finalResult.node_id) currentNodeId = finalResult.node_id;
            appendNarrative(finalResult.response || '', { type: 'npc_talk', npc_id: npcId, text: msg }, true);
            renderChoices([]);
            updateStatusPanel(currentState);
            updateHeader();
            updateSwipeControls(0, 1);
            updateTurnIndicator();
        }
    } catch (e) {
        if (isCurrentChat()) {
            const label = committed ? '显示失败，请重新加载存档' : '发送失败';
            typingEl.insertAdjacentHTML('beforeend', `<span style="color:var(--danger)">（${label}: ${escapeHtml(e.message)}）</span>`);
            if (!committed) input.value = msg;
        }
    } finally {
        if (_npcChatRequest === request) {
            _npcChatRequest = null;
            if (isCurrentGame()) isStreaming = false;
            input.disabled = false;
        }
        if (isCurrentChat()) {
            input.focus();
            msgArea.scrollTop = msgArea.scrollHeight;
        }
    }
}

// ================================================
//  GAME TOOLBAR FUNCTIONS
// ================================================

function backToHome() {
    if (isStreaming) return;
    // Confirm if game is in progress
    if (currentSaveId && !confirm('返回首页？当前进度已自动保存。')) return;
    currentSaveId = null;
    currentState = null;
    currentNodeId = null;
    _prevState = null;
    document.getElementById('game-play-screen').style.display = 'none';
    document.getElementById('game-start-screen').style.display = 'block';
    document.getElementById('status-panel').style.display = 'none';
    document.getElementById('status-panel-toggle').style.display = 'none';
    document.getElementById('narrative-content').innerHTML = '';
    document.getElementById('choices-area').innerHTML = '';
    document.getElementById('dice-display').style.display = 'none';
    // A2: 清空角色选择区，避免与新剧本错配
    document.getElementById('character-select-area').style.display = 'none';
    document.getElementById('preset-cards').innerHTML = '';
    _selectedPresetId = null;
    _selectedCustomChar = null;
    _currentScriptData = null;
    loadGameStart();
}

function scrollToLatest() {
    const area = document.getElementById('narrative-area');
    const latest = area?.querySelector('#narrative-content .turn-block:last-child');
    if (latest) latest.scrollIntoView({ block: 'end' });
    else if (area) area.scrollTop = area.scrollHeight;
}

function updateTurnIndicator() {
    const el = document.getElementById('turn-indicator');
    if (!el) return;
    const blocks = document.querySelectorAll('#narrative-content .turn-block');
    el.textContent = blocks.length > 0 ? `第 ${blocks.length} 回合` : '';
}

let _npcScheduleTimer = null;
let _lastScheduleGameTime = null;
let _lastScheduleSaveId = null;
let _lastScheduleState = null;
function _syncNpcTalkMode() {
    const state = currentState;
    if (!state) return;
    const mapMode = document.getElementById('main-map-view')?.hidden === false;
    for (const [npcId, npc] of Object.entries(state.npcs || {})) {
        const btn = document.getElementById(`btn-talk-${npcId}`);
        if (!btn) continue;
        const name = state.display_names?.[npcId] || (typeof npc === 'object' ? (npc.name || npcId) : npcId);
        const seeking = mapMode && !(state.companions || []).includes(npcId) &&
            npc?.known !== false && npc?.met !== false && !NpcPresence.hasCurrentContact(npcId, state);
        btn.textContent = seeking ? '寻找' : '对话';
        if (!btn.disabled) btn.title = seeking ? `寻找${name}，尝试接触` : `与${name}对话`;
    }
}
async function _loadNpcSchedules() {
    const saveId = currentSaveId;
    const state = currentState;
    if (!saveId || !state) return;
    const curTime = state.game_time || '';
    if (saveId === _lastScheduleSaveId && state === _lastScheduleState && curTime === _lastScheduleGameTime) return;
    if (_npcScheduleTimer) clearTimeout(_npcScheduleTimer);
    _npcScheduleTimer = setTimeout(() => {
        if (currentSaveId !== saveId || currentState !== state) return;
        _lastScheduleSaveId = saveId;
        _lastScheduleState = state;
        _lastScheduleGameTime = curTime;
        _doLoadNpcSchedules(saveId, state);
    }, 500);
}
function _setNpcTalkAvailability(btn, reason, displayName, npcId, state) {
    if (!btn) return;
    btn.disabled = !!reason;
    const npc = state.npcs?.[npcId];
    const seeking = document.getElementById('main-map-view')?.hidden === false &&
        !(state.companions || []).includes(npcId) && npc?.known !== false && npc?.met !== false &&
        !NpcPresence.hasCurrentContact(npcId, state);
    btn.textContent = seeking ? '寻找' : '对话';
    btn.title = reason || (seeking ? `寻找${displayName}，尝试接触` : `与${displayName}对话`);
    if (reason === '不在当前位置') btn.classList.add('btn-npc-away');
    else btn.classList.remove('btn-npc-away');
}
async function _doLoadNpcSchedules(saveId, state) {
    const npcs = state.npcs || {};
    const dn = state.display_names || {};
    const playerLoc = (state.player?.location || '').toLowerCase();
    const gameTime = state.game_time || '';
    try {
        const data = await NpcPresence.getSchedules(saveId, state);
        if (currentSaveId !== saveId || currentState !== state || (state.game_time || '') !== gameTime) return;
        for (const [npcId, npcData] of Object.entries(npcs)) {
            const info = data[npcId];
            const el = document.getElementById(`npc-sched-${npcId}`);
            const btn = document.getElementById(`btn-talk-${npcId}`);
            const displayName = dn[npcId] || (typeof npcData === 'object' ? (npcData.name || npcId) : npcId);

            const npcLoc = info?.location || '';

            // Show schedule info (resolve location display name)
            const npcKnown = (typeof npcData === 'object') ? (npcData.known !== false) : true;
            if (el) {
                if (npcKnown && playerLoc && npcLoc.toLowerCase() === playerLoc) {
                    const locDisplay = dn[npcLoc] || npcLoc;
                    el.innerHTML = `<span class="npc-sched-text">${escapeHtml(locDisplay)}</span>`;
                } else {
                    el.innerHTML = `<span class="npc-sched-text" style="color:var(--text-muted)">当前位置未知</span>`;
                }
            }

            _setNpcTalkAvailability(btn, NpcPresence.talkBlockReason(npcId, state, data), displayName, npcId, state);
        }
    } catch (e) {
        if (currentSaveId !== saveId || currentState !== state || (state.game_time || '') !== gameTime) return;
        _lastScheduleState = null;
        for (const [npcId, npcData] of Object.entries(npcs)) {
            const btn = document.getElementById(`btn-talk-${npcId}`);
            const displayName = dn[npcId] || (typeof npcData === 'object' ? (npcData.name || npcId) : npcId);
            _setNpcTalkAvailability(btn, NpcPresence.talkBlockReason(npcId, state, null), displayName, npcId, state);
        }
    }
}
