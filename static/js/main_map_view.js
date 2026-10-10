let _mainMapState = null;
let _mainMapScheduleKey = '';
let _mainMapSchedules = null;
let _mainMapPreparedAction = '';
let _mainMapPreparedIntent = '';
let _mainMapPreparedTargetId = '';
let _mainMapSelectedRoute = null;
let _mainMapAgentSaveId = null;
let _mainMapAgentTarget = null;
let _mainMapAgentSource = null;
let _mainMapAgentAction = null;
let _mainMapActivitySaveId = null;
let _mainMapActivityAction = null;
let _mainMapReportKeys = new Set();
let _mainMapLayoutSaveId = null;
let _mainMapPositions = Object.create(null);
let _mainMapCommandSource = null;
let _mainMapCamera = null;
let _mainMapCameraReady = false;
let _mainMapFocusSaveId = null;
let _mainMapFocusViewSaveId = null;
let _mainMapPrivacySaveId = null;
let _mainMapChoiceSaveId = null;
let _mainMapChoiceState = null;
let _mainMapChoiceRequest = 0;
let _mainMapChoosing = null;

function _updateMainMapLabels(svg, camera) {
    if (!camera.nodes || !camera.view) return;
    const scale = 1 / Math.min(1, camera.zoom);
    const view = camera.view;
    const visibleNodes = camera.nodes.filter(node => {
        const radius = node.current ? 32 : 25;
        return node.x - radius >= view.x && node.x + radius <= view.x + view.width
            && node.y - radius >= view.y && node.y + radius <= view.y + view.height;
    });
    const placements = _mainMapLabelPlacements(visibleNodes, { scale, links: camera.links,
        view: camera.view });
    for (const group of svg.children) {
        if (!group.mapLocationId) continue;
        const label = Array.from(group.children).find(child => child.getAttribute('class') === 'main-map-label');
        const placement = placements.get(group.mapLocationId);
        const leader = Array.from(group.children).find(child => child.getAttribute('class') === 'main-map-label-leader');
        const hit = Array.from(group.children).find(child => child.getAttribute('class') === 'main-map-node-hit');
        label.setAttribute('visibility', placement ? 'visible' : 'hidden');
        hit.setAttribute('visibility', placement ? 'visible' : 'hidden');
        if (!placement) {
            leader.setAttribute('visibility', 'hidden');
            continue;
        }
        const node = visibleNodes.find(place => place.id === group.mapLocationId);
        const box = placement.box;
        const endpointX = Math.max(box.left, Math.min(node.x, box.right));
        const endpointY = Math.max(box.top, Math.min(node.y, box.bottom));
        const distance = Math.hypot(node.x - endpointX, node.y - endpointY);
        leader.setAttribute('x2', endpointX);
        leader.setAttribute('y2', endpointY);
        hit.setAttribute('x2', endpointX);
        hit.setAttribute('y2', endpointY);
        leader.setAttribute('visibility', (distance - (node.current ? 32 : 25)) * camera.zoom > 16 ? 'visible' : 'hidden');
        label.setAttribute('x', placement.x);
        label.setAttribute('y', placement.y);
        label.setAttribute('style', `font-size: ${15 * scale}px`);
        for (const [index, line] of Array.from(label.children).entries()) {
            line.setAttribute('x', placement.x);
            line.setAttribute('dy', index ? 17 * scale : 0);
        }
    }
}

function _renderMainMapCamera() {
    if (!_mainMapCamera) return;
    const svg = document.getElementById('main-map-svg');
    const size = svg.getBoundingClientRect();
    if (!size.width || !size.height) return;
    const camera = _mainMapCamera;
    const bounds = camera.bounds;
    const minimum = Math.min(1, size.width / bounds.width, size.height / bounds.height);
    if (camera.auto) {
        camera.zoom = Math.max(0.7, minimum);
        camera.auto = false;
    }
    camera.zoom = camera.fit ? minimum : Math.max(minimum, Math.min(2.5, camera.zoom));
    const width = size.width / camera.zoom;
    const height = size.height / camera.zoom;
    if (camera.fit) {
        camera.x = bounds.x + bounds.width / 2;
        camera.y = bounds.y + bounds.height / 2;
    }
    const halfWidth = Math.min(width, bounds.width) / 2;
    const halfHeight = Math.min(height, bounds.height) / 2;
    const keepCurrent = camera.followCurrent && !camera.fit;
    const minX = keepCurrent ? Math.min(bounds.x + halfWidth, camera.current.x) : bounds.x + halfWidth;
    const maxX = keepCurrent ? Math.max(bounds.x + bounds.width - halfWidth, camera.current.x) : bounds.x + bounds.width - halfWidth;
    const minY = keepCurrent ? Math.min(bounds.y + halfHeight, camera.current.y) : bounds.y + halfHeight;
    const maxY = keepCurrent ? Math.max(bounds.y + bounds.height - halfHeight, camera.current.y) : bounds.y + bounds.height - halfHeight;
    camera.x = Math.max(minX, Math.min(maxX, camera.x));
    camera.y = Math.max(minY, Math.min(maxY, camera.y));
    camera.view = { x: camera.x - width / 2, y: camera.y - height / 2, width, height };
    svg.setAttribute('viewBox', `${camera.view.x} ${camera.view.y} ${width} ${height}`);
    _updateMainMapLabels(svg, camera);
    document.getElementById('main-map-zoom-level').textContent = `${Math.round(camera.zoom * 100)}%`;
    document.getElementById('main-map-zoom-out').disabled = camera.zoom <= minimum;
    document.getElementById('main-map-zoom-in').disabled = camera.zoom >= 2.5;
}

function zoomMainMap(factor) {
    if (!_mainMapCamera) return;
    _mainMapCamera.auto = false;
    _mainMapCamera.fit = false;
    _mainMapCamera.zoom *= factor;
    _renderMainMapCamera();
}

function panMainMap(horizontal, vertical) {
    if (!_mainMapCamera) return;
    _mainMapCamera.auto = false;
    _mainMapCamera.fit = false;
    _mainMapCamera.followCurrent = false;
    _mainMapCamera.x += horizontal / _mainMapCamera.zoom;
    _mainMapCamera.y += vertical / _mainMapCamera.zoom;
    _renderMainMapCamera();
}

function fitMainMap() {
    if (!_mainMapCamera) return;
    _mainMapCamera.auto = false;
    _mainMapCamera.fit = true;
    _mainMapCamera.followCurrent = false;
    _renderMainMapCamera();
}

function centerMainMap() {
    if (!_mainMapCamera) return;
    _mainMapCamera.auto = false;
    _mainMapCamera.fit = false;
    _mainMapCamera.followCurrent = true;
    _mainMapCamera.zoom = 1;
    _mainMapCamera.x = _mainMapCamera.current.x;
    _mainMapCamera.y = _mainMapCamera.current.y;
    _renderMainMapCamera();
}

function _initMainMapCamera() {
    if (_mainMapCameraReady) return;
    _mainMapCameraReady = true;
    const svg = document.getElementById('main-map-svg');
    let drag = null;
    const endDrag = () => {
        if (!drag) return;
        const pointerId = drag.pointerId;
        drag = null;
        svg.classList.remove('panning');
        if (svg.hasPointerCapture(pointerId)) svg.releasePointerCapture(pointerId);
    };
    svg.addEventListener('pointerdown', event => {
        if (!_mainMapCamera || event.button !== 0 || event.target.closest('.main-map-node')) return;
        drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, camera: _mainMapCamera };
        svg.setPointerCapture(event.pointerId);
        svg.classList.add('panning');
    });
    svg.addEventListener('pointermove', event => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        if (drag.camera !== _mainMapCamera || _mainMapCamera.saveId !== currentSaveId) {
            endDrag();
            return;
        }
        panMainMap(drag.x - event.clientX, drag.y - event.clientY);
        drag.x = event.clientX;
        drag.y = event.clientY;
    });
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);
    svg.addEventListener('lostpointercapture', endDrag);
    svg.addEventListener('keydown', event => {
        if (event.target !== svg) return;
        const directions = { ArrowLeft: [-64, 0], ArrowRight: [64, 0], ArrowUp: [0, -64], ArrowDown: [0, 64] };
        if (directions[event.key]) panMainMap(...directions[event.key]);
        else if (event.key === '+' || event.key === '=') zoomMainMap(1.25);
        else if (event.key === '-') zoomMainMap(0.8);
        else if (event.key === '0') fitMainMap();
        else if (event.key === 'Home') centerMainMap();
        else return;
        event.preventDefault();
    });
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => {
        _renderMainMapCamera();
        _syncMainMapAgentDock();
    }).observe(svg);
}

function _revealMainMapLocation(bounds) {
    const camera = _mainMapCamera;
    if (!camera?.view) return;
    const view = camera.view;
    if (bounds.left >= view.x && bounds.right <= view.x + view.width &&
        bounds.top >= view.y && bounds.bottom <= view.y + view.height) return;
    camera.fit = false;
    camera.x = (bounds.left + bounds.right) / 2;
    camera.y = (bounds.top + bounds.bottom) / 2;
    _renderMainMapCamera();
}

function _mainMapViewKey(saveId) {
    return `tavern_main_map_view:${saveId}`;
}

function setMainMapViewForSave(saveId, enabled) {
    if (saveId) localStorage.setItem(_mainMapViewKey(saveId), String(enabled));
}

function clearMainMapViewForSave(saveId) {
    if (saveId) localStorage.removeItem(_mainMapViewKey(saveId));
}

function _clearMainMapPreparedAction() {
    const input = document.getElementById('freeform-input');
    if (_mainMapPreparedAction && input.value === _mainMapPreparedAction) input.value = '';
    _mainMapPreparedAction = '';
    _mainMapPreparedIntent = '';
    _mainMapPreparedTargetId = '';
    _mainMapSelectedRoute = null;
    _highlightMainMapRoute();
    document.getElementById('freeform-submit').textContent = '行动';
}

function clearMainMapActivity() {
    const activity = document.getElementById('main-map-activity');
    activity.replaceChildren();
    activity.classList.remove('has-result');
    activity.hidden = true;
    _mainMapActivitySaveId = null;
    _mainMapActivityAction = null;
    _mainMapReportKeys = new Set();
}

function restoreMainMapResult(saveId, narrative) {
    if (saveId !== currentSaveId || typeof narrative !== 'string' || !narrative.trim() ||
        window._activeGameAction || document.getElementById('main-map-view').hidden ||
        !document.getElementById('main-map-activity').hidden) return;
    recordMainMapStreamEvent(saveId, null, { type: 'final', narrative, restored: true });
}

function restoreMainMapView() {
    const wasEnabled = !document.getElementById('main-map-view').hidden;
    if (_mainMapChoosing && _mainMapChoosing.saveId !== currentSaveId) {
        _mainMapChoosing = null;
        document.getElementById('freeform-input').disabled = false;
    }
    if (_mainMapLayoutSaveId && _mainMapLayoutSaveId !== currentSaveId) {
        _clearMainMapPreparedAction();
        document.getElementById('main-map-scene').replaceChildren();
    }
    if (_mainMapActivitySaveId && _mainMapActivitySaveId !== currentSaveId) {
        clearMainMapActivity();
    }
    if (_mainMapAgentSaveId && (_mainMapAgentSaveId !== currentSaveId ||
        (_mainMapAgentAction && window._activeGameAction !== _mainMapAgentAction))) {
        _returnMainMapAgentControls();
        _mainMapAgentSource = null;
        _mainMapAgentSaveId = null;
        _mainMapAgentTarget = null;
        _mainMapAgentAction = null;
        document.getElementById('main-map-agent-status').hidden = true;
    }
    const enabled = !!currentSaveId && localStorage.getItem(_mainMapViewKey(currentSaveId)) === 'true';
    if ((_mainMapPrivacySaveId !== currentSaveId || enabled) && typeof _clearMapMetaNotifications === 'function') {
        _clearMapMetaNotifications();
    }
    _mainMapPrivacySaveId = currentSaveId;
    if (!enabled) document.getElementById('status-panel').classList.remove('map-open');
    document.getElementById('game-play-screen').classList.toggle('map-view', enabled);
    document.getElementById('main-map-view').hidden = !enabled;
    if (enabled && (!wasEnabled || _mainMapFocusViewSaveId !== currentSaveId)) {
        _mainMapFocusSaveId = currentSaveId;
    }
    _mainMapFocusViewSaveId = enabled ? currentSaveId : null;
    _syncMainMapFocus(enabled);
    if (!enabled) {
        _mainMapChoiceRequest++;
        _mainMapChoiceSaveId = null;
        renderMainMapStoryChoices([]);
    } else if (currentState && (!wasEnabled || _mainMapChoiceSaveId !== currentSaveId)) {
        loadMainMapStoryChoices(currentState);
    }
    const storyTab = document.getElementById('storytree-status-tab');
    storyTab.hidden = enabled;
    if (enabled && document.getElementById('sp-storytree').classList.contains('active')) {
        const statusPanel = document.getElementById('status-panel');
        statusPanel.classList.remove('storytree-expanded');
        document.getElementById('btn-storytree-expand').textContent = '⛶ 展开';
        switchStatusTab(document.getElementById('clues-status-tab'));
    }
    if (typeof _syncNpcTalkMode === 'function') _syncNpcTalkMode();
    _syncMainMapCommandDock(enabled);
    if (typeof _repositionNotifs === 'function') _repositionNotifs();
    document.getElementById('freeform-submit').textContent = enabled && _mainMapPreparedAction ? '确认行动' : '行动';
    _syncMainMapAgentDock();
    if (enabled && !wasEnabled) {
        const activity = document.getElementById('main-map-activity');
        activity.scrollTop = activity.scrollHeight;
    }
    if (enabled && !wasEnabled && _mainMapAgentAction &&
        _mainMapAgentAction === window._activeGameAction) {
        document.getElementById('main-area').scrollTop = 0;
    }
    const button = document.getElementById('map-mode-toggle');
    button.setAttribute('aria-label', enabled ? '切换到叙事视图' : '切换到大地图视图');
    button.setAttribute('title', enabled ? '切换到叙事视图（同一存档）' : '切换到大地图视图（同一存档）');
    button.textContent = enabled ? '叙事视图' : '大地图';
}

function _syncMainMapFocus(enabled) {
    if (!enabled || _mainMapFocusSaveId !== currentSaveId) _mainMapFocusSaveId = null;
    const focused = !!_mainMapFocusSaveId;
    document.getElementById('game-play-screen').classList.toggle('map-focus', focused);
    const button = document.getElementById('main-map-focus-toggle');
    button.setAttribute('aria-pressed', String(focused));
    button.textContent = focused ? '还原双栏' : '聚焦地图';
}

function toggleMainMapFocus() {
    if (!currentSaveId || document.getElementById('main-map-view').hidden) return;
    _mainMapFocusSaveId = _mainMapFocusSaveId === currentSaveId ? null : currentSaveId;
    _syncMainMapFocus(true);
    _syncMainMapAgentDock();
}

function _syncMainMapCommandDock(enabled) {
    const choices = document.getElementById('choices-area');
    const form = document.getElementById('freeform-area');
    const dock = document.getElementById('main-map-command-dock');
    const suggestions = document.getElementById('main-map-suggested-actions');
    if (enabled) {
        if (!_mainMapCommandSource) {
            _mainMapCommandSource = { parent: form.parentElement, after: form.nextSibling };
        }
        if (choices.parentElement !== suggestions) suggestions.appendChild(choices);
        if (form.parentElement !== dock) dock.appendChild(form);
    } else if (_mainMapCommandSource) {
        const { parent, after } = _mainMapCommandSource;
        if (form.parentElement !== parent) parent.insertBefore(form, after);
        if (choices.parentElement !== parent) parent.insertBefore(choices, form);
    }
}

function renderMainMapStoryChoices(pending) {
    const panel = document.getElementById('main-map-story-choices');
    panel.replaceChildren();
    pending = pending || [];
    panel.hidden = !pending.length;
    document.getElementById('main-map-command-dock').classList.toggle('has-story-choices', !!pending.length);
    if (pending.length) document.getElementById('main-map-suggested-actions').open = false;
    if (!pending.length) return;
    const heading = document.createElement('div');
    heading.className = 'main-map-story-heading';
    heading.textContent = `待决剧情选择 · ${pending.length}`;
    panel.appendChild(heading);
    for (const node of pending) {
        const item = document.createElement('div');
        item.className = 'main-map-story-item';
        const name = document.createElement('span');
        name.textContent = node.name;
        item.appendChild(name);
        for (const choice of node.choices) {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = choice.label;
            button.title = choice.description || '';
            if (choice.description) {
                const description = document.createElement('span');
                description.className = 'main-map-story-description';
                description.textContent = choice.description;
                button.appendChild(description);
            }
            button.disabled = !!_mainMapChoosing || document.getElementById('freeform-input').disabled;
            button.onclick = () => chooseMainMapStoryChoice(node.id, choice.id);
            item.appendChild(button);
        }
        panel.appendChild(item);
    }
}

async function loadMainMapStoryChoices(state = currentState) {
    const saveId = currentSaveId;
    if (!saveId || document.getElementById('main-map-view').hidden) return;
    const request = ++_mainMapChoiceRequest;
    _mainMapChoiceSaveId = saveId;
    _mainMapChoiceState = state;
    renderMainMapStoryChoices([]);
    try {
        const data = await API.get(`/api/game/${saveId}/story-tree/active-choices`);
        if (request === _mainMapChoiceRequest && saveId === currentSaveId && !document.getElementById('main-map-view').hidden) {
            renderMainMapStoryChoices(data.choices);
        }
    } catch (error) {
        if (request === _mainMapChoiceRequest && saveId === currentSaveId) console.error('Failed to load map choices:', error);
    }
}

async function chooseMainMapStoryChoice(nodeId, choiceId) {
    if (_mainMapChoosing || document.getElementById('freeform-input').disabled ||
        document.getElementById('main-map-view').hidden) return;
    const saveId = currentSaveId;
    const choiceToken = { saveId };
    _mainMapChoosing = choiceToken;
    let refreshedState = false;
    const input = document.getElementById('freeform-input');
    input.disabled = true;
    for (const item of document.getElementById('main-map-story-choices').children) {
        for (const button of item.children) if (button.type === 'button') button.disabled = true;
    }
    try {
        if (await makeStoryChoice(nodeId, choiceId, true) && saveId === currentSaveId) {
            const state = await API.get(`/api/game/${saveId}/state`);
            if (saveId === currentSaveId) {
                currentState = state;
                updateStatusPanel(state);
                refreshedState = true;
            }
        }
    } catch (error) {
        if (saveId === currentSaveId) showNotification('刷新选择状态失败: ' + (error.message || '未知错误'));
    } finally {
        if (_mainMapChoosing === choiceToken) {
            _mainMapChoosing = null;
            input.disabled = false;
            if (!refreshedState && saveId === currentSaveId) await loadMainMapStoryChoices();
        }
    }
}

function _returnMainMapAgentControls() {
    if (!_mainMapAgentSource) return;
    const { block, toolsWrap, controls, toolsAfter, controlsAfter } = _mainMapAgentSource;
    if (toolsWrap.parentElement !== block) block.insertBefore(toolsWrap, toolsAfter);
    if (controls.parentElement !== block) block.insertBefore(controls, controlsAfter);
    document.getElementById('main-map-agent-dock').hidden = true;
}

function _syncMainMapAgentDock() {
    const source = _mainMapAgentSource;
    const dock = document.getElementById('main-map-agent-dock');
    const mapVisible = !document.getElementById('main-map-view').hidden;
    const mapPlan = source?.saveId === currentSaveId && mapVisible &&
        _mainMapAgentTarget?.classList.contains('agent-plan-card') &&
        _mainMapAgentTarget.querySelector('.plan-confirm-btn');
    const narrative = document.getElementById('narrative-area');
    const screen = document.getElementById('game-play-screen');
    const width = screen.getBoundingClientRect().width;
    const focused = mapVisible && screen.classList.contains('map-focus') &&
        width >= 800;
    const inMap = focused || (mapVisible && width < 800 && source?.saveId === currentSaveId);
    const parent = inMap ? document.getElementById('main-map-view') : narrative;
    if (dock.parentElement !== parent) {
        const focusedControl = document.activeElement;
        let focusedNode = focusedControl;
        while (focusedNode && focusedNode !== dock) focusedNode = focusedNode.parentElement;
        parent.insertBefore(dock, document.getElementById(inMap ? 'main-map-command-dock' : 'narrative-content'));
        if (focusedNode === dock) focusedControl.focus({ preventScroll: true });
    }
    dock.classList.toggle('map-plan-active', !!mapPlan);
    if (!source || source.saveId !== currentSaveId || !mapVisible) {
        _returnMainMapAgentControls();
        return;
    }
    dock.hidden = false;
    dock.appendChild(source.toolsWrap);
    dock.appendChild(source.controls);
    if (_mainMapAgentTarget?.classList.contains('agent-plan-card')) {
        source.toolsWrap.classList.add('open');
    }
}

function setMainMapAgentStatus(saveId, message, target = null) {
    if (saveId !== currentSaveId) return;
    const revealPlan = target?.classList.contains('agent-plan-card')
        && target !== _mainMapAgentTarget && !document.getElementById('main-map-view').hidden;
    const activeAction = typeof window === 'undefined' ? null : window._activeGameAction;
    if (_mainMapAgentAction && _mainMapAgentAction !== activeAction) {
        _returnMainMapAgentControls();
        _mainMapAgentSource = null;
    }
    if (!message) {
        if (_mainMapAgentSource?.toolsWrap.parentElement === document.getElementById('main-map-agent-dock')) {
            _mainMapAgentSource.toolsWrap.classList.remove('open');
        }
        _returnMainMapAgentControls();
        _mainMapAgentSource = null;
    } else if (target && !_mainMapAgentSource) {
        const block = target.closest?.('.turn-block');
        const toolsWrap = block?.querySelector('.agent-tools-block');
        const controls = block?.querySelector('.agent-controls');
        if (toolsWrap && controls) {
            _mainMapAgentSource = {
                saveId, block, toolsWrap, controls,
                toolsAfter: toolsWrap.nextSibling, controlsAfter: controls.nextSibling,
            };
        }
    }
    _mainMapAgentSaveId = message ? saveId : null;
    _mainMapAgentTarget = target;
    _mainMapAgentAction = message ? activeAction : null;
    document.getElementById('main-map-agent-status').hidden = !message;
    document.getElementById('main-map-agent-message').textContent = message;
    const button = document.getElementById('main-map-agent-jump');
    button.hidden = !target;
    button.textContent = target?.classList.contains('agent-plan-card')
        ? (target.querySelector?.('.plan-question') ? '填写选择' : '确认计划') : '查看交互';
    _syncMainMapAgentDock();
    if (revealPlan && _mainMapAgentSource) {
        _mainMapAgentSource.toolsWrap.querySelector('.agent-tools-body').scrollTop = 0;
        document.getElementById('main-map-agent-dock').scrollTop = 0;
        document.getElementById('main-area').scrollTop = 0;
    }
}

function recordMainMapStreamEvent(saveId, actionToken, event) {
    if (saveId !== currentSaveId || (actionToken && window._activeGameAction !== actionToken) ||
        (!actionToken && !(event.restored && event.type === 'final' && !window._activeGameAction))) return;
    if (event.type === 'begin') {
        const reports = Array.isArray(currentState?.pc_event_reports) ? currentState.pc_event_reports : [];
        _mainMapReportKeys = new Set(reports.map(report =>
            JSON.stringify([report?.npc_id, report?.evidence, report?.heard_at])));
    }
    if (event.type === 'begin' && !document.getElementById('main-map-view').hidden) {
        document.getElementById('main-area').scrollTop = 0;
    }
    let message;
    switch (event.type) {
        case 'begin': message = event.regeneration ? '正在重新推演回合' : event.is_query ? '询问已提交，等待回答' : '行动已提交，等待推演'; break;
        case 'preflight':
            if (event.stage === 'background') message = '正在同步上一轮的后台任务';
            else if (event.stage === 'intent') message = '正在准备行动';
            else if (event.stage === 'query') message = '正在检索资料并回答询问';
            else if (event.stage === 'route_retrieval') message = '正在检索剧本并规划行动';
            else if (event.stage === 'narrative_review') message = '正在生成并核验叙事正文';
            break;
        case 'tool_preparing': message = '模型正在准备工具调用'; break;
        case 'tool_started': message = `${String(event.label || 'Agent').slice(0, 32)} · 正在调用 ${String(event.tool_name || '工具').slice(0, 48)}`; break;
        case 'tool_call': message = `${String(event.label || 'Agent').slice(0, 32)} · ${String(event.tool_name || '调用工具').slice(0, 48)}`; break;
        case 'agent_done': message = `${String(event.label || 'Agent').slice(0, 32)} ${event.is_query ? '已生成回答' : '完成推演'}`; break;
        case 'agent_controls_closed': message = event.is_query ? '查询 Agent 已完成，正在整理回答' : 'Agent 推演完成，正在结算'; break;
        case 'text':
        case 'narrative_revised':
            if (event.reviewed === true) message = '叙事初审通过，回合仍在结算（内容可能修订）';
            break;
        case 'agent_plan': message = event.plan_id ? 'Agent 提出计划，等待选择' : 'Agent 提出计划，继续推演'; break;
        case 'agent_waiting': message = String(event.reason || 'Agent 等待中').slice(0, 80); break;
        case 'model_waiting': message = `${String(event.label || 'Agent').slice(0, 32)} · 正在等待模型响应`; break;
        case 'model_output_started': message = `${String(event.label || 'Agent').slice(0, 32)} · 模型已开始生成`; break;
        case 'user_inject': message = event.plan_id ? 'Agent 已接收计划回复，继续推演' : 'Agent 已接收插话，继续推演'; break;
        case 'agent_inject_dropped': message = event.plan_id ? '计划回复未生效，Agent 已结束' : '插话未生效，Agent 已结束'; break;
        case 'agent_stop_missed': message = '停止请求未赶上 Agent 终结'; break;
        case 'plan_auto_confirmed': message = '计划已自动确认，继续推演'; break;
        case 'aborted': message = '推演已停止，回合未结算；可修改指令后重新行动'; break;
        case 'final':
            if (event.is_query) message = '查询完成';
            else if (event.is_undo) message = '撤销已写入，正在刷新画面';
            else if (event.is_rewrite) message = '当前回合已重写';
            else if (event.regeneration) message = event.stage === 'choices' ? '选项已更新'
                : event.stage === 'state' ? '状态已重新推演' : '回合已重新推演';
            else {
                const eventCount = Array.isArray(event.triggered_events) ? event.triggered_events.length : 0;
                message = `本回合已结算${eventCount ? ` · 世界动态 ${eventCount} 项` : ''}`;
            }
            break;
        case 'undo_refresh_error': message = '撤销已生效，画面刷新失败'; break;
        case 'error': message = event.discard_partial ? '结算校验未通过，草稿已撤回'
            : event.server_error ? (event.regeneration ? '重新生成失败，原回合仍保留；可重试'
                : '推演生成失败，请核对存档后重试')
            : event.retryable ? (typeof event.retry === 'function'
                ? '推演断开，先核对存档；可在这里续接' : '推演断开，先核对存档；叙事视图可重试')
            : '推演断开，请核对存档后再行动'; break;
        default: return;
    }
    if (!message && event.type !== 'text' && event.type !== 'narrative_revised') return;
    if (event.type === 'final' && !event.is_query && !event.is_undo && !event.is_rewrite &&
        !event.regeneration && !event.restored && !document.getElementById('main-map-view').hidden) {
        document.getElementById('main-area').scrollTop = 0;
    }
    const activity = document.getElementById('main-map-activity');
    if (_mainMapActivitySaveId !== saveId || _mainMapActivityAction !== actionToken) {
        activity.replaceChildren();
        activity.classList.remove('has-result');
        activity.classList.remove('has-draft');
        _mainMapActivitySaveId = saveId;
        _mainMapActivityAction = actionToken;
    }
    let draft = Array.from(activity.children).find(child => child.className === 'main-map-activity-entry draft');
    if ((event.type === 'text' || event.type === 'narrative_revised') && !event.is_query) {
        const chunk = String(event.content || '').replace(/<reflect>[\s\S]*?<\/reflect>/gi, '');
        if (chunk.trim()) {
            if (!draft) {
                draft = document.createElement('li');
                draft.className = 'main-map-activity-entry draft';
                draft.textContent = '推演草稿 · 未结算，内容可能修订';
                const preview = document.createElement('span');
                preview.className = 'main-map-result-preview';
                draft.appendChild(preview);
            }
            draft.previewText = ((event.type === 'narrative_revised' ? '' : draft.previewText || '') + chunk).slice(0, 160);
            draft.children[0].textContent = draft.previewText.replace(/\s+/g, ' ').trim();
            activity.appendChild(draft);
            activity.classList.add('has-draft');
            activity.hidden = false;
        }
    }
    if (!message) {
        activity.scrollTop = activity.scrollHeight;
        return;
    }
    if (event.type === 'final' || event.type === 'aborted' ||
        (event.type === 'error' && (!event.retryable || event.discard_partial))) {
        draft?.remove();
        activity.classList.remove('has-draft');
        draft = null;
    }
    const entry = document.createElement('li');
    entry.className = `main-map-activity-entry ${event.type}`;
    entry.textContent = message;
    if (event.type === 'error' && event.retryable && typeof event.retry === 'function') {
        const button = document.createElement('button');
        button.className = 'retry-btn';
        button.textContent = event.regeneration ? '重试' : event.server_error ? '重新推演' : '续接';
        button.addEventListener('click', () => {
            if (entry.parentElement !== activity || activity.children[activity.children.length - 1] !== entry ||
                _mainMapActivitySaveId !== saveId || _mainMapActivityAction !== actionToken || currentSaveId !== saveId) return;
            return event.retry();
        });
        entry.appendChild(button);
    }
    if (event.type === 'final' && (event.is_query || (!event.is_undo && !event.is_rewrite && !event.regeneration))) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-secondary btn-small main-map-result-jump';
        button.textContent = event.is_query ? '查看回答' : '查看本回合';
        button.addEventListener('click', () => {
            if (entry.parentElement !== activity || _mainMapActivitySaveId !== saveId ||
                _mainMapActivityAction !== actionToken || currentSaveId !== saveId ||
                document.getElementById('main-map-view').hidden) return;
            const narrative = document.getElementById('narrative-content');
            const target = event.is_query ? narrative.children[narrative.children.length - 1]
                : Array.from(narrative.children).reverse().find(child => child.classList.contains('turn-block'));
            target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
        entry.appendChild(button);
    }
    if (event.type === 'final' && !event.is_query && !event.is_undo && !event.is_rewrite &&
        !event.regeneration && !event.restored && Array.isArray(event.state?.pc_event_reports)) {
        const newReports = event.state.pc_event_reports.filter(report => report?.evidence &&
            !_mainMapReportKeys.has(JSON.stringify([report.npc_id, report.evidence, report.heard_at])));
        if (newReports.length) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn-secondary btn-small main-map-result-jump';
            button.textContent = `查看听闻（${newReports.length}）`;
            button.addEventListener('click', () => {
                if (entry.parentElement !== activity || _mainMapActivitySaveId !== saveId ||
                    _mainMapActivityAction !== actionToken || currentSaveId !== saveId ||
                    document.getElementById('main-map-view').hidden) return;
                showPcEventReports();
            });
            entry.appendChild(button);
        }
    }
    activity.appendChild(entry);
    if (draft && event.type !== 'error') activity.appendChild(draft);
    if (event.type === 'final' && !event.is_query && !event.is_undo && !event.is_rewrite && !event.regeneration &&
        typeof event.narrative === 'string' && event.narrative.trim()) {
        const excerpt = document.createElement('span');
        excerpt.className = 'main-map-result-preview';
        excerpt.textContent = event.narrative.replace(/\s+/g, ' ').trim().slice(0, 160);
        entry.appendChild(excerpt);
        activity.classList.add('has-result');
    }
    if (event.type === 'final' && !event.is_query && !event.is_undo && !event.is_rewrite && !event.regeneration) {
        for (const news of Array.isArray(event.news) ? event.news : []) {
            const title = String(news?.title || '快讯').slice(0, 40);
            const content = String(news?.content || '').slice(0, 100);
            const bulletin = document.createElement('li');
            bulletin.className = 'main-map-activity-entry news';
            bulletin.textContent = `快讯 · ${title}${content ? `：${content}` : ''}`;
            activity.insertBefore(bulletin, entry);
        }
    }
    while (activity.children.length > 4) activity.removeChild(activity.firstChild);
    activity.hidden = false;
    activity.scrollTop = activity.scrollHeight;
}

function jumpToMainMapAgent() {
    const target = _mainMapAgentTarget;
    if (!target || _mainMapAgentSaveId !== currentSaveId) return;
    if (target.classList.contains('agent-plan-card')) {
        target.closest('.agent-tools-block').classList.add('open');
    }
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const confirm = target.querySelector('.plan-confirm-btn');
    (confirm && !confirm.disabled ? confirm : target.querySelector('.pq-option') || target.querySelector('.inject-input'))?.focus({ preventScroll: true });
}

function toggleMainMapView() {
    if (!currentSaveId) return;
    const enabled = !document.getElementById('game-play-screen').classList.contains('map-view');
    setMainMapViewForSave(currentSaveId, enabled);
    restoreMainMapView();
    if (enabled && currentState) updateMainMapView(currentState, currentSaveId);
}

function _mainMapElement(tag, attributes = {}) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
}

function _mainMapLayout(ids, edges, previousPositions) {
    const positions = Object.create(null);
    for (const id of ids) {
        if (Object.hasOwn(previousPositions, id)) positions[id] = previousPositions[id];
    }
    if (ids.length && !Object.keys(positions).length) {
        const degree = Object.fromEntries(ids.map(id => [id, 0]));
        for (const edge of edges) { degree[edge.source]++; degree[edge.target]++; }
        const hub = [...ids].sort((left, right) => degree[right] - degree[left] || left.localeCompare(right))[0];
        positions[hub] = { x: 480, y: 340 };
    }
    const slots = [];
    const compact = ids.length <= 16;
    for (const [count, horizontalRadius, verticalRadius] of [[8, compact ? 220 : 165, compact ? 125 : 165],
        [Math.max(16, ids.length - 9), 290, compact ? 225 : 290]]) {
        for (let index = 0; index < count; index++) {
            const angle = 2 * Math.PI * index / count - Math.PI / 2;
            slots.push({ x: 480 + horizontalRadius * Math.cos(angle), y: 340 + verticalRadius * Math.sin(angle) });
        }
    }
    for (const id of [...ids].sort()) {
        if (positions[id]) continue;
        const neighbors = edges.map(edge => edge.source === id ? edge.target : edge.target === id ? edge.source : null)
            .filter(other => other && positions[other]).map(other => positions[other]);
        let candidates = slots.filter(slot => !Object.values(positions).some(placed =>
            (placed.x - slot.x) ** 2 + (placed.y - slot.y) ** 2 < 3600));
        if (!candidates.length) {
            for (let gridY = 100; gridY <= 580; gridY += 80) {
                for (let gridX = 200; gridX <= 760; gridX += 80) {
                    const slot = { x: gridX, y: gridY };
                    if (!Object.values(positions).some(placed =>
                        (placed.x - gridX) ** 2 + (placed.y - gridY) ** 2 < 3600)) candidates.push(slot);
                }
            }
        }
        const ranked = candidates.length ? candidates : slots;
        positions[id] = ranked.reduce((best, slot) => {
            const score = neighbors.length
                ? neighbors.reduce((sum, neighbor) => sum + (neighbor.x - slot.x) ** 2 + (neighbor.y - slot.y) ** 2, 0) / neighbors.length
                : slots.indexOf(slot);
            return !best || score < best.score ? { slot, score } : best;
        }, null).slot;
    }
    return positions;
}

function _mainMapModel(state, schedules, previousPositions = {}) {
    const currentId = state.player?.location || '';
    const ids = [...new Set([currentId, ...(state.visible_locations || [])].filter(Boolean))];
    const names = state.display_names || {};
    const edges = [];
    const seenEdges = new Set();
    const visible = new Set(ids);
    for (const [source, targets] of Object.entries(state.location_connections || {})) {
        if (!visible.has(source) || !Array.isArray(targets)) continue;
        for (const target of targets) {
            if (!visible.has(target) || source === target) continue;
            const key = [source, target].sort().join('\0');
            if (seenEdges.has(key)) continue;
            seenEdges.add(key);
            edges.push({ source, target, bidirectional: Array.isArray(state.location_connections?.[target]) &&
                state.location_connections[target].includes(source) });
        }
    }
    const positions = _mainMapLayout(ids, edges, previousPositions);

    const counts = {};
    const lastSeen = {};
    for (const [id, npc] of Object.entries(state.npcs || {})) {
        if (!npc || typeof npc !== 'object' || npc.known === false) continue;
        const location = schedules ? schedules[id]?.location : npc.current_location;
        if (currentId && location === currentId) counts[currentId] = (counts[currentId] || 0) + 1;
        const sighting = state.npc_last_seen?.[id];
        if (sighting?.location && sighting.location !== currentId && visible.has(sighting.location) && location !== currentId) {
            (lastSeen[sighting.location] ||= []).push({
                name: names[id] || npc.name || id,
                gameTime: sighting.game_time || '',
            });
        }
    }
    return { nodes: ids.map(id => ({ id, name: names[id] || id, current: id === currentId, ...positions[id], npcCount: counts[id] || 0, lastSeen: lastSeen[id] || [] })), edges };
}

function _mainMapLabelLines(name) {
    const characters = Array.from(name);
    if (characters.length <= 8) return characters.length ? [name] : [];
    if (characters.length <= 12) {
        let midpoint = Math.ceil(characters.length / 2);
        while (midpoint < characters.length - 2 && /[0-9]/.test(characters[midpoint - 1]) && /[0-9]/.test(characters[midpoint])) midpoint++;
        return [characters.slice(0, midpoint).join(''), characters.slice(midpoint).join('')];
    }
    return [characters.slice(0, 6).join(''), `${characters.slice(6, 11).join('')}…`];
}

function _mainMapLabelPlacements(nodes, { scale = 1, view, links = [] } = {}) {
    const placements = new Map();
    const occupied = [];
    const overlap = (left, right) => Math.max(0, Math.min(left.right, right.right) - Math.max(left.left, right.left))
        * Math.max(0, Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top));
    const crosses = (box, { from, to }) => {
        const horizontal = to.x - from.x;
        const vertical = to.y - from.y;
        let entry = 0;
        let exit = 1;
        for (const [direction, limit] of [[-horizontal, from.x - box.left], [horizontal, box.right - from.x],
            [-vertical, from.y - box.top], [vertical, box.bottom - from.y]]) {
            if (!direction) {
                if (limit < 0) return false;
            } else if (direction < 0) entry = Math.max(entry, limit / direction);
            else exit = Math.min(exit, limit / direction);
        }
        return entry <= exit;
    };
    for (const node of nodes) {
        const lines = _mainMapLabelLines(node.name);
        const width = Math.max(...lines.map(line => Array.from(line).reduce((sum, character) =>
            sum + (/[\u0000-\u007f]/.test(character) ? 8 : 15), 0)), 0) + 6;
        const labelWidth = width * scale;
        const height = (lines.length * 17 + (view ? 6 : 0)) * scale;
        const radius = node.current ? 32 : 25;
        const candidates = [
            { x: node.x, top: node.y + radius + 10 * scale },
            { x: node.x, top: node.y - radius - 10 * scale - height },
            { x: node.x + radius + 8 * scale + labelWidth / 2, top: node.y - height / 2 },
            { x: node.x - radius - 8 * scale - labelWidth / 2, top: node.y - height / 2 },
            { x: node.x + radius + 12 * scale + labelWidth / 2, top: node.y + radius + 6 * scale },
            { x: node.x - radius - 12 * scale - labelWidth / 2, top: node.y + radius + 6 * scale },
            { x: node.x + radius + 12 * scale + labelWidth / 2, top: node.y - radius - 10 * scale - height },
            { x: node.x - radius - 12 * scale - labelWidth / 2, top: node.y - radius - 10 * scale - height },
        ];
        if (view) {
            for (let top = view.y + 8 * scale; top + height <= view.y + view.height - 8 * scale; top += height + 8 * scale) {
                for (let x = view.x + labelWidth / 2 + 8 * scale; x + labelWidth / 2 <= view.x + view.width - 8 * scale; x += labelWidth + 8 * scale) {
                    candidates.push({ x, top });
                }
            }
        }
        const ranked = candidates.map((candidate, index) => {
            const box = { left: candidate.x - labelWidth / 2, right: candidate.x + labelWidth / 2,
                top: candidate.top, bottom: candidate.top + height };
            const limits = view ? { left: view.x, right: view.x + view.width,
                top: view.y, bottom: view.y + view.height } : { left: 130, right: 830, top: 12, bottom: 668 };
            const outside = Math.max(0, limits.left - box.left) + Math.max(0, box.right - limits.right)
                + Math.max(0, limits.top - box.top) + Math.max(0, box.bottom - limits.bottom);
            const circles = nodes.reduce((sum, other) => {
                const otherRadius = (other.current ? 32 : 25) + 5;
                return sum + overlap(box, { left: other.x - otherRadius, right: other.x + otherRadius,
                    top: other.y - otherRadius, bottom: other.y + otherRadius });
            }, 0);
            const gap = 6 * scale;
            const paddedBox = { left: box.left - gap, right: box.right + gap,
                top: box.top - gap, bottom: box.bottom + gap };
            const labels = occupied.reduce((sum, other) => sum + overlap(paddedBox, other), 0);
            const crossedLinks = links.reduce((sum, link) => sum + Number(crosses(box, link)), 0);
            return { x: candidate.x, y: candidate.top + (view ? 16 : 15) * scale, box,
                score: outside * (view ? 10000 : 100) + circles * 10 + labels * 10 + crossedLinks * 200
                    + (view ? ((candidate.x - node.x) ** 2 + (candidate.top - node.y) ** 2) / 500 : 0) + index };
        });
        const best = ranked.reduce((preferred, candidate) => candidate.score < preferred.score ? candidate : preferred);
        placements.set(node.id, { x: best.x, y: best.y, lines, box: best.box });
        occupied.push(best.box);
    }
    return placements;
}

function _mainMapKnownRoute(state, destination, visible) {
    const origin = state.player?.location;
    if (!origin || !visible.has(origin) || !visible.has(destination)) return null;
    const parents = new Map([[origin, null]]);
    const queue = [origin];
    for (let index = 0; index < queue.length; index++) {
        const current = queue[index];
        if (current === destination) {
            const route = [];
            for (let place = destination; place !== null; place = parents.get(place)) route.unshift(place);
            return route;
        }
        const neighbors = state.location_connections?.[current];
        for (const next of Array.isArray(neighbors) ? neighbors : []) {
            if (visible.has(next) && !parents.has(next)) {
                parents.set(next, current);
                queue.push(next);
            }
        }
    }
    return null;
}

function _highlightMainMapRoute() {
    const route = _mainMapSelectedRoute || [];
    const places = new Set(route);
    const edges = new Set();
    for (let index = 1; index < route.length; index++) {
        edges.add([route[index - 1], route[index]].sort().join('\0'));
    }
    const destination = route[route.length - 1];
    for (const child of document.getElementById('main-map-svg').children) {
        if (child.mapEdgeKey || child.mapArrowEdgeKey) {
            child.classList.toggle('on-route', edges.has(child.mapEdgeKey || child.mapArrowEdgeKey));
        }
        if (child.mapLocationId) {
            child.classList.toggle('on-route', places.has(child.mapLocationId));
            child.classList.toggle('selected', child.mapLocationId === destination);
            const circle = Array.from(child.children).find(element => element.getAttribute('r') !== null);
            circle.setAttribute('aria-pressed', String(child.mapLocationId === destination));
        }
    }
    for (const button of document.getElementById('main-map-locations').children) {
        button.classList.toggle('on-route', places.has(button.mapLocationId));
        button.classList.toggle('selected', button.mapLocationId === destination);
        button.setAttribute('aria-pressed', String(button.mapLocationId === destination));
    }
}

function _mainMapSceneCanAct(saveId, state) {
    return currentSaveId === saveId && currentState === state &&
        !document.getElementById('main-map-view').hidden &&
        !document.getElementById('freeform-input').disabled &&
        !(typeof isStreaming !== 'undefined' && isStreaming) && !window._activeGameAction;
}

function prepareMainMapNpcContact(npcId, saveId, state) {
    if (!_mainMapSceneCanAct(saveId, state)) return;
    const location = state.player?.location;
    const npc = state.npcs?.[npcId];
    if (!location || !npc || npc.known === false || npc.met === false ||
        NpcPresence.hasCurrentContact(npcId, state)) return;
    const name = state.display_names?.[npcId] || npc.name || npcId;
    const input = document.getElementById('freeform-input');
    const hint = document.getElementById('main-map-hint');
    if (input.value.trim() && input.value !== _mainMapPreparedAction) {
        hint.textContent = '输入框中有未提交行动，未覆盖；可自行描述如何接触同区人物。';
        return;
    }
    _mainMapPreparedAction = `在${state.display_names?.[location] || location}寻找${name}，尝试与其接触`;
    _mainMapPreparedIntent = 'map_npc_search';
    _mainMapPreparedTargetId = '';
    _mainMapSelectedRoute = null;
    _highlightMainMapRoute();
    input.value = _mainMapPreparedAction;
    document.getElementById('freeform-submit').textContent = '确认行动';
    hint.textContent = `已填写寻找${name}；同区不等于当面，能否接触由推演决定。`;
    input.focus();
}

function _drawMainMapScene(state) {
    const scene = document.getElementById('main-map-scene');
    const saveId = currentSaveId;
    const location = state.player?.location;
    const focused = document.activeElement;
    const focusedAction = focused?.mapSceneSaveId === saveId && focused.mapSceneLocation === location &&
        focused.parentElement?.parentElement === scene ? focused.mapSceneKey : null;
    scene.replaceChildren();
    if (!saveId || currentState !== state) return;
    const heading = document.createElement('strong');
    heading.className = 'main-map-scene-heading';
    heading.tabIndex = -1;
    heading.textContent = `${state.display_names?.[location] || location || '未知地点'} · 区域行动`;
    scene.appendChild(heading);
    const actions = document.createElement('div');
    actions.className = 'main-map-scene-actions';
    scene.appendChild(actions);

    for (const item of state.location_interactables || []) {
        const control = document.createElement(item.used ? 'span' : 'button');
        control.className = item.used ? 'main-map-scene-used' : 'main-map-scene-action';
        control.textContent = `⚙ ${item.name}${item.used ? ' · 已使用' : ''}${item.required_item && !item.used ? ` · 需${item.required_item}` : ''}${item.locked_skill && !item.used ? ` · ${item.locked_skill}` : ''}`;
        control.title = item.description || '';
        if (!item.used) {
            control.type = 'button';
            control.mapSceneKey = `item:${item.id}`;
            control.mapSceneSaveId = saveId;
            control.mapSceneLocation = location;
            control.addEventListener('click', () => {
                if (_mainMapSceneCanAct(saveId, state)) interactWith(item.id, item.action_hint || item.name);
            });
        }
        actions.appendChild(control);
    }
    for (const [npcId, npc] of Object.entries(state.npcs || {})) {
        if (!npc || typeof npc !== 'object' || npc.known === false || npc.met === false) continue;
        const npcLocation = _mainMapSchedules ? _mainMapSchedules[npcId]?.location : npc.current_location;
        if (!location || (npcLocation !== location && !(state.companions || []).includes(npcId))) continue;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'main-map-scene-action';
        button.mapSceneKey = `npc:${npcId}`;
        button.mapSceneSaveId = saveId;
        button.mapSceneLocation = location;
        const name = state.display_names?.[npcId] || npc.name || npcId;
        const companion = (state.companions || []).includes(npcId);
        const canTalk = companion || NpcPresence.hasCurrentContact(npcId, state);
        button.textContent = canTalk ? `与${name}对话` : `寻找${name} · 同区`;
        button.addEventListener('click', () => {
            if (!_mainMapSceneCanAct(saveId, state)) return;
            if (canTalk) {
                openNpcChat(npcId);
                return;
            }
            prepareMainMapNpcContact(npcId, saveId, state);
        });
        actions.appendChild(button);
    }
    if (!actions.children.length) {
        const empty = document.createElement('span');
        empty.className = 'main-map-scene-empty';
        const description = state.location_descriptions?.[location];
        empty.textContent = description
            ? `${description} 可在下方自由描述行动。`
            : '暂无现场互动；也可以在下方自由描述行动。';
        actions.appendChild(empty);
    }
    if (focusedAction && currentSaveId === saveId && document.activeElement === document.body) {
        let replacement = heading;
        for (const action of actions.children) {
            if (action.mapSceneKey !== focusedAction) continue;
            replacement = action;
            break;
        }
        replacement.focus({ preventScroll: true });
    }
}

function _drawMainMap(state) {
    _initMainMapCamera();
    const svg = document.getElementById('main-map-svg');
    const list = document.getElementById('main-map-locations');
    const hint = document.getElementById('main-map-hint');
    const saveId = currentSaveId;
    const focused = document.activeElement;
    const focusedContainer = focused?.parentElement?.parentElement === svg ? svg : focused?.parentElement;
    const focusedLocation = focused?.mapSaveId === saveId &&
        (focusedContainer === svg || focusedContainer === list) ? focused.mapLocationId : null;
    const model = _mainMapModel(state, _mainMapSchedules, _mainMapPositions);
    const nodesById = new Map(model.nodes.map(node => [node.id, node]));
    const links = model.edges.map(edge => ({ from: nodesById.get(edge.source), to: nodesById.get(edge.target) }));
    const labels = _mainMapLabelPlacements(model.nodes);
    for (const node of model.nodes) _mainMapPositions[node.id] = { x: node.x, y: node.y };
    svg.replaceChildren();
    list.replaceChildren();
    document.getElementById('main-map-location-count').textContent = `已发现 ${model.nodes.length} 处`;
    document.getElementById('main-map-time').textContent = formatGameTime(state.game_time) || state.game_time || '';
    _drawMainMapScene(state);
    document.getElementById('main-map-navigation').hidden = !model.nodes.length;
    if (!model.nodes.length) {
        _mainMapCamera = null;
        svg.setAttribute('viewBox', '120 0 720 680');
        hint.textContent = '这个剧本尚未提供可见地点。';
        return;
    }
    const bounds = model.nodes.map(node => {
        const box = labels.get(node.id).box;
        const radius = node.current ? 32 : 25;
        return {
            left: Math.min(box.left, node.x - radius),
            right: Math.max(box.right, node.x + radius),
            top: Math.min(box.top, node.y - radius),
            bottom: Math.max(box.bottom, node.y + radius),
        };
    });
    const left = Math.min(...bounds.map(box => box.left));
    const right = Math.max(...bounds.map(box => box.right));
    const top = Math.min(...bounds.map(box => box.top));
    const bottom = Math.max(...bounds.map(box => box.bottom));
    const width = Math.max(480, right - left + 64);
    const height = Math.max(480, bottom - top + 64);
    const current = model.nodes.find(node => node.current) || model.nodes[0];
    if (!_mainMapCamera || _mainMapCamera.saveId !== saveId) {
        _mainMapCamera = { saveId, zoom: 1, fit: false, auto: true, followCurrent: true, x: current.x, y: current.y };
    } else if (_mainMapCamera.current.id !== current.id) {
        _mainMapCamera.x = current.x;
        _mainMapCamera.y = current.y;
        _mainMapCamera.fit = false;
        _mainMapCamera.followCurrent = true;
    }
    _mainMapCamera.current = current;
    _mainMapCamera.bounds = { x: (left + right - width) / 2, y: (top + bottom - height) / 2, width, height };
    _mainMapCamera.nodes = model.nodes;
    _mainMapCamera.links = links;
    _renderMainMapCamera();
    for (const edge of model.edges) {
        const from = model.nodes.find(node => node.id === edge.source);
        const to = model.nodes.find(node => node.id === edge.target);
        const line = _mainMapElement('line', {
            x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: 'main-map-link',
        });
        line.mapEdgeKey = [edge.source, edge.target].sort().join('\0');
        svg.appendChild(line);
        if (!edge.bidirectional) {
            const angle = Math.atan2(to.y - from.y, to.x - from.x) * 180 / Math.PI;
            const arrow = _mainMapElement('path', {
                d: 'M -6 -4 L 0 0 L -6 4',
                transform: `translate(${from.x + (to.x - from.x) * 0.6} ${from.y + (to.y - from.y) * 0.6}) rotate(${angle})`,
                class: 'main-map-arrow',
            });
            arrow.mapArrowEdgeKey = line.mapEdgeKey;
            svg.appendChild(arrow);
        }
    }
    for (const node of model.nodes) {
        const ariaLabel = `${node.name}${node.current ? '，当前位置' : '，选择目的地'}${node.lastSeen.length ? `，最后目击 ${node.lastSeen.length} 人，旧情报` : ''}`;
        const group = _mainMapElement('g', { class: `main-map-node${node.current ? ' current' : ''}${node.lastSeen.length ? ' last-seen' : ''}` });
        group.mapLocationId = node.id;
        group.mapSaveId = saveId;
        const title = _mainMapElement('title');
        title.textContent = node.name + (node.lastSeen.length ? ` · 最后目击：${node.lastSeen.map(sighting => `${sighting.name}（${sighting.gameTime || '时间不详'}）`).join('、')}，不代表目前在场` : '');
        group.appendChild(title);
        const placement = labels.get(node.id);
        group.appendChild(_mainMapElement('line', {
            x1: node.x, y1: node.y, x2: placement.x, y2: placement.y, class: 'main-map-node-hit', visibility: 'hidden',
        }));
        group.appendChild(_mainMapElement('line', {
            x1: node.x, y1: node.y, x2: placement.x, y2: placement.y, class: 'main-map-label-leader', visibility: 'hidden',
        }));
        const circle = _mainMapElement('circle', { cx: node.x, cy: node.y, r: node.current ? 32 : 25,
            tabindex: '0', role: 'button', 'aria-label': ariaLabel });
        circle.mapLocationId = node.id;
        circle.mapSaveId = saveId;
        group.appendChild(circle);
        const label = _mainMapElement('text', { x: placement.x, y: placement.y, class: 'main-map-label' });
        for (const [index, text] of placement.lines.entries()) {
            const line = _mainMapElement('tspan', { x: placement.x, dy: index ? 17 : 0 });
            line.textContent = text;
            label.appendChild(line);
        }
        group.appendChild(label);
        if (node.npcCount) {
            const count = _mainMapElement('text', { x: node.x, y: node.y + 4, class: 'main-map-count' });
            count.textContent = `${node.npcCount} 人`;
            group.appendChild(count);
        } else if (node.lastSeen.length) {
            const count = _mainMapElement('text', { x: node.x, y: node.y + 4, class: 'main-map-last-seen' });
            count.textContent = `${node.lastSeen.length} 人`;
            group.appendChild(count);
        }
        const select = () => {
            if (!_mainMapSceneCanAct(saveId, state)) return;
            const input = document.getElementById('freeform-input');
            if (!node.current) {
                const visible = new Set(model.nodes.map(place => place.id));
                const route = _mainMapKnownRoute(state, node.id, visible);
                _mainMapSelectedRoute = route || [node.id];
                _highlightMainMapRoute();
                const routeText = route
                    ? `已知路线：${route.map(id => state.display_names?.[id] || id).join(' → ')}`
                    : '尚无已知路线';
                const sightingText = node.lastSeen.length
                    ? `旧情报：${node.lastSeen.map(sighting => `${sighting.name}于${sighting.gameTime || '未知时间'}目击`).join('；')}，不代表目前在场。`
                    : '';
                if (input.value.trim() && input.value !== _mainMapPreparedAction) {
                    _mainMapPreparedAction = '';
                    _mainMapPreparedIntent = '';
                    _mainMapPreparedTargetId = '';
                    hint.textContent = `${routeText}。${sightingText}输入框中有未提交行动，未覆盖；是否可抵达由推演决定。`;
                    document.getElementById('freeform-submit').textContent = '行动';
                } else {
                    _mainMapPreparedAction = `前往${node.name}`;
                    _mainMapPreparedIntent = 'map_travel';
                    _mainMapPreparedTargetId = node.id;
                    input.value = _mainMapPreparedAction;
                    hint.textContent = `已填写「前往${node.name}」。${routeText}。${sightingText}是否可抵达由推演决定。`;
                    document.getElementById('freeform-submit').textContent = '确认行动';
                }
            } else {
                _clearMainMapPreparedAction();
                hint.textContent = `当前位置：${node.name}。地图显示已发现地点和已知人物，游戏时间随行动推进。`;
            }
        };
        group.addEventListener('click', select);
        circle.addEventListener('focus', () => {
            if (circle.matches(':focus-visible')) _revealMainMapLocation(bounds[model.nodes.indexOf(node)]);
        });
        circle.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); }
        });
        svg.appendChild(group);
        const button = document.createElement('button');
        button.type = 'button';
        button.mapLocationId = node.id;
        button.mapSaveId = saveId;
        button.className = `main-map-place${node.current ? ' current' : ''}`;
        button.textContent = `${node.name}${node.current ? ' · 当前位置' : ''}${node.npcCount ? ` · ${node.npcCount} 人同区` : ''}${node.lastSeen.length ? ` · 旧情报 ${node.lastSeen.length} 人` : ''}`;
        button.addEventListener('click', select);
        list.appendChild(button);
    }
    _updateMainMapLabels(svg, _mainMapCamera);
    _highlightMainMapRoute();
    if (focusedLocation && currentSaveId === saveId && document.activeElement === document.body) {
        const container = focusedContainer === svg ? svg : list;
        for (const child of container.children) {
            if (child.mapLocationId !== focusedLocation) continue;
            (container === svg ? Array.from(child.children).find(element => element.getAttribute('r') !== null) : child)
                .focus({ preventScroll: true });
            break;
        }
    }
}

function updateMainMapView(state, saveId) {
    if (!state || document.getElementById('main-map-view').hidden) return;
    if (_mainMapLayoutSaveId !== saveId) {
        _mainMapLayoutSaveId = saveId;
        _mainMapPositions = Object.create(null);
    }
    const stateChanged = _mainMapState !== state;
    _mainMapState = state;
    if (saveId && !document.getElementById('main-map-view').hidden &&
        (_mainMapChoiceSaveId !== saveId || _mainMapChoiceState !== state)) loadMainMapStoryChoices(state);
    const gameTime = state.game_time || '';
    const key = `${saveId}|${gameTime}`;
    if (stateChanged || key !== _mainMapScheduleKey) {
        _clearMainMapPreparedAction();
        const location = state.player?.location;
        const name = state.display_names?.[location] || location;
        document.getElementById('main-map-hint').textContent = name
            ? `当前位置：${name}。点击地点查看路线并填写行动；地图随回合更新。`
            : '点击地点查看路线并填写行动；地图随回合更新。';
    }
    if (saveId && (stateChanged || key !== _mainMapScheduleKey)) {
        _mainMapScheduleKey = key;
        _mainMapSchedules = null;
        NpcPresence.getSchedules(saveId, state).then(schedules => {
            if (_mainMapScheduleKey !== key || _mainMapState !== state || (state.game_time || '') !== gameTime) return;
            _mainMapSchedules = schedules;
            if (!document.getElementById('main-map-view').hidden) _drawMainMap(_mainMapState);
        }).catch(() => {
            if (_mainMapScheduleKey === key && _mainMapState === state && (state.game_time || '') === gameTime)
                _mainMapScheduleKey = '';
        });
    }
    _drawMainMap(state);
}

document.getElementById('freeform-input').addEventListener('input', event => {
    if (_mainMapPreparedAction && event.target.value !== _mainMapPreparedAction) {
        _mainMapPreparedAction = '';
        _mainMapPreparedIntent = '';
        _mainMapPreparedTargetId = '';
        document.getElementById('freeform-submit').textContent = '行动';
        document.getElementById('main-map-hint').textContent = '行动草稿已修改，地图选择仅供参考；请使用「行动」按钮提交。';
    }
});
