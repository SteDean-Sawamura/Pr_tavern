/* 酒馆 - 剧本可视化编辑器 + AI辅助 */

// --- 生成引导 ---
const _GENERATION_ORDER = [
    { step: 'basic',         label: '基本信息', num: 1 },
    { step: 'world',         label: '世界构建', num: 2 },
    { step: 'organizations', label: '组织势力', num: 3 },
    { step: 'characters',    label: '角色设计', num: 4 },
    { step: 'events',        label: '事件系统', num: 5 },
    { step: 'dice',          label: '随机项',   num: 6 },
    { step: 'lorebook',      label: '知识库',   num: 7 },
    { step: 'story_tree',    label: '剧情树',   num: 8 },
];

const _TAB_DEPENDENCY_HINTS = {
    characters:    { deps: ['world', 'organizations'], hint: '建议先完成「世界构建」和「组织势力」，以便 AI 生成更准确的角色和 NPC' },
    organizations: { deps: ['world'],                  hint: '建议先完成「世界构建」，组织势力的据点和活动范围依赖地点信息' },
    events:        { deps: ['world', 'characters'],    hint: '建议先完成「世界构建」和「角色设计」，事件通常涉及特定地点和角色' },
    dice:          { deps: ['basic'],                  hint: '建议先完成「基本信息」，随机项需要基于世界背景设计' },
    lorebook:      { deps: ['basic'],                  hint: '建议先完成其他内容页，知识库可汇总所有设定生成百科词条' },
    story_tree:    { deps: ['world', 'characters', 'events', 'dice'], hint: '建议先完成「世界构建」「角色设计」「事件系统」「随机项(变量)」，剧情树依赖地点、NPC、事件和变量' },
};

function _getTabCompletionStatus() {
    const status = {};
    const bg = document.getElementById('w-background');
    const name = document.getElementById('w-script-name');
    status.basic = !!(bg && bg.value.trim()) || !!(name && name.value.trim());

    const hasChildren = (id) => {
        const el = document.getElementById(id);
        return el && el.children.length > 0;
    };

    const ps = _pendingScript;

    if (!_loadedTabs.has('world') && ps) {
        status.world = !!(ps.locations?.length || ps.world_properties?.length);
    } else {
        status.world = hasChildren('w-locations') || hasChildren('w-world-props');
    }

    if (!_loadedTabs.has('organizations') && ps) {
        status.organizations = !!(ps.organizations?.length);
    } else {
        status.organizations = hasChildren('w-organizations');
    }

    if (!_loadedTabs.has('characters') && ps) {
        status.characters = !!(ps.npcs?.length || ps.player_character?.bio?.trim());
    } else {
        status.characters = hasChildren('w-npcs') || !!(document.getElementById('w-pc-bio') && document.getElementById('w-pc-bio').value.trim());
    }

    if (!_loadedTabs.has('events') && ps) {
        status.events = !!(ps.cyclic_events?.length || ps.one_time_events?.length || ps.tone_rules?.length || ps.quest_templates?.length);
    } else {
        status.events = hasChildren('w-cyclic-events') || hasChildren('w-onetime-events') || hasChildren('w-tone-rules');
    }

    if (!_loadedTabs.has('dice') && ps) {
        status.dice = !!(ps.random_items?.length);
    } else {
        status.dice = hasChildren('w-random-items');
    }

    if (!_loadedTabs.has('lorebook') && ps) {
        status.lorebook = !!(ps.lorebook?.length);
    } else {
        status.lorebook = hasChildren('w-lorebook');
    }

    if (!_loadedTabs.has('story_tree') && ps) {
        status.story_tree = !!(ps.story_tree?.trees?.length);
    } else {
        status.story_tree = hasChildren('w-story-trees');
    }

    return status;
}

function _getNextRecommendedStep() {
    const status = _getTabCompletionStatus();
    for (const item of _GENERATION_ORDER) {
        if (!status[item.step]) return item;
    }
    return null;
}

function _renderGuidePanel() {
    const panel = document.getElementById('copilot-content');
    if (!panel) return;
    if (panel._hasGenerateResult) return;

    const status = _getTabCompletionStatus();
    const next = _getNextRecommendedStep();
    const completedCount = _GENERATION_ORDER.filter(i => status[i.step]).length;

    let html = '<div class="guide-panel">';
    html += '<div class="guide-title">生成引导</div>';
    html += `<div class="guide-progress">${completedCount} / ${_GENERATION_ORDER.length} 步已完成</div>`;
    html += '<div class="guide-steps">';
    for (const item of _GENERATION_ORDER) {
        const done = status[item.step];
        const isNext = next && next.step === item.step;
        const cls = done ? 'guide-step done' : (isNext ? 'guide-step next' : 'guide-step');
        html += `<div class="${cls}" onclick="wizardStep('${item.step}')">`;
        html += `<span class="guide-num">${done ? '✓' : item.num}</span>`;
        html += `<span class="guide-label">${item.label}</span>`;
        html += '</div>';
        if (item.num < _GENERATION_ORDER.length) {
            html += '<div class="guide-arrow">↓</div>';
        }
    }
    html += '</div>';

    if (next) {
        html += `<button class="btn-primary guide-next-btn" onclick="wizardStep('${next.step}')">前往「${next.label}」</button>`;
    } else {
        html += '<div class="guide-complete">所有内容页已填写，可以保存剧本了</div>';
    }
    html += '</div>';
    panel.innerHTML = html;
}

function _updateTabBadges() {
    const status = _getTabCompletionStatus();
    const next = _getNextRecommendedStep();
    document.querySelectorAll('.wizard-tab').forEach(tab => {
        const step = tab.dataset.step;
        const order = _GENERATION_ORDER.find(i => i.step === step);
        tab.classList.remove('step-done', 'step-recommended');
        if (order && status[step]) tab.classList.add('step-done');
        if (next && step === next.step) tab.classList.add('step-recommended');
        if (order && !tab.dataset.numSet) {
            tab.dataset.stepNum = order.num;
            tab.dataset.numSet = '1';
        }
    });
}

function _showDepHint(step) {
    document.querySelectorAll('.tab-dep-hint').forEach(el => el.remove());
    const info = _TAB_DEPENDENCY_HINTS[step];
    if (!info) return;
    const status = _getTabCompletionStatus();
    const missing = info.deps.filter(d => !status[d]);
    if (missing.length === 0) return;

    const stepEl = document.getElementById('step-' + step);
    if (!stepEl) return;
    const hint = document.createElement('div');
    hint.className = 'tab-dep-hint';
    hint.textContent = info.hint;
    stepEl.insertBefore(hint, stepEl.firstChild);
}

function _updateGuide() {
    _updateTabBadges();
    _renderGuidePanel();
}

// --- Wizard state ---
let editingScriptId = null;
// 保留 AI 生成或加载的未知字段（如 supplementary_lorebook、knowledge_graph_seed 等），
// 否则 buildScriptFromWizard 每次重建时会全部丢失。
let _rawScriptOverlay = {};
// 记录当前 wizard 处于哪个 step，用于检测从 json tab 切回 wizard 时反向同步
let _currentWizardStep = 'basic';
// 延迟加载：保存待加载的剧本数据，在首次切换到对应 tab 时才填充 DOM
let _pendingScript = null;
const _loadedTabs = new Set();

/**
 * 将 item-card 变为可折叠卡片。
 * collapsed=true 时默认折叠（加载已有数据），false 时展开（手动新增）。
 */
function _makeCardCollapsible(div, collapsed) {
    const summary = document.createElement('div');
    summary.className = 'card-summary';
    const body = document.createElement('div');
    body.className = 'card-body';

    // 把除 btn-remove 外的子元素移入 body
    const children = Array.from(div.children);
    const removeBtn = div.querySelector('.btn-remove');
    for (const child of children) {
        if (child === removeBtn) continue;
        body.appendChild(child);
    }

    // 从前两个 text input 取摘要
    const _updateLabel = () => {
        const inputs = body.querySelectorAll('input[type="text"], textarea');
        const parts = [];
        for (let i = 0; i < inputs.length && parts.length < 2; i++) {
            const v = inputs[i].value.trim();
            if (v) parts.push(v);
        }
        summary.textContent = parts.join(' — ') || '(未命名)';
    };

    summary.onclick = (e) => { e.stopPropagation(); div.classList.toggle('collapsed'); };
    div.insertBefore(summary, removeBtn ? removeBtn.nextSibling : div.firstChild);
    div.appendChild(body);

    body.addEventListener('input', _updateLabel);
    _updateLabel();
    if (collapsed) div.classList.add('collapsed');
}

/* ========== 列表分页系统 ========== */
const _paginators = {};
const _PAGE_SIZE = 10;
let _isLoadingScript = false;

function _initPagination(containerId) {
    if (_paginators[containerId]) return _paginators[containerId];
    const container = document.getElementById(containerId);
    if (!container) return null;

    const controls = document.createElement('div');
    controls.className = 'page-controls';
    container.after(controls);

    const state = { container, controls, page: 0, _timer: null };

    const refresh = () => {
        const items = Array.from(container.children);
        const total = items.length;
        const totalPages = Math.max(1, Math.ceil(total / _PAGE_SIZE));
        if (state.page >= totalPages) state.page = totalPages - 1;
        if (state.page < 0) state.page = 0;
        const start = state.page * _PAGE_SIZE;
        const end = start + _PAGE_SIZE;
        items.forEach((el, i) => {
            el.style.display = (i >= start && i < end) ? '' : 'none';
        });
        if (total <= _PAGE_SIZE) { controls.style.display = 'none'; return; }
        controls.style.display = '';
        controls.innerHTML = '';
        const firstBtn = document.createElement('button');
        firstBtn.textContent = '⏮ 首页';
        firstBtn.disabled = state.page === 0;
        firstBtn.onclick = () => { state.page = 0; refresh(); };
        const prevBtn = document.createElement('button');
        prevBtn.textContent = '◀ 上页';
        prevBtn.disabled = state.page === 0;
        prevBtn.onclick = () => { state.page--; refresh(); };
        const pageInput = document.createElement('input');
        pageInput.type = 'number';
        pageInput.min = 1;
        pageInput.max = totalPages;
        pageInput.value = state.page + 1;
        pageInput.onchange = () => {
            const v = parseInt(pageInput.value, 10);
            if (v >= 1 && v <= totalPages) { state.page = v - 1; refresh(); }
            else pageInput.value = state.page + 1;
        };
        const info = document.createElement('span');
        info.textContent = ` / ${totalPages}页`;
        const nextBtn = document.createElement('button');
        nextBtn.textContent = '下页 ▶';
        nextBtn.disabled = state.page >= totalPages - 1;
        nextBtn.onclick = () => { state.page++; refresh(); };
        controls.append(firstBtn, prevBtn, pageInput, info, nextBtn);
    };

    state.refresh = refresh;
    state.goToLast = () => {
        state.page = Math.max(0, Math.ceil(container.children.length / _PAGE_SIZE) - 1);
        refresh();
    };

    // 用 MutationObserver 自动刷新（debounce 50ms 合并批量操作）
    const observer = new MutationObserver((mutations) => {
        if (_isLoadingScript) {
            clearTimeout(state._timer);
            return;
        }
        clearTimeout(state._timer);
        let hasAdded = false;
        for (const m of mutations) if (m.addedNodes.length) hasAdded = true;
        state._timer = setTimeout(() => {
            if (_isLoadingScript) return;
            if (hasAdded) state.goToLast();
            else refresh();
        }, 50);
    });
    observer.observe(container, { childList: true });

    _paginators[containerId] = state;
    return state;
}

// 初始化所有需要分页的容器
function _initAllPaginators() {
    const ids = [
        'w-npcs', 'w-locations', 'w-world-props', 'w-persistent-states',
        'w-organizations', 'w-org-rels', 'w-cyclic-events', 'w-onetime-events',
        'w-random-items', 'w-lorebook', 'w-presets',
    ];
    for (const id of ids) _initPagination(id);
}

// ── NPC 关系网 SVG 编辑器 ──
const _NPC_REL_TYPE_MAP = {
    '友好': {trust:60,affection:70,fear:0}, '中立': {trust:50,affection:50,fear:0},
    '敌对': {trust:10,affection:10,fear:60}, '竞争': {trust:30,affection:20,fear:20},
    '合作': {trust:70,affection:60,fear:0}, '亲密': {trust:80,affection:90,fear:0},
    '仇恨': {trust:0,affection:0,fear:80},
};
const _NpcRelGraph = {
    svg: null,
    editorEl: null,
    containerEl: null,
    edges: [],       // {from, to, trust, affection, fear, initially_known, initially_met, description}
    selected: null,  // 第一个选中的 NPC id
    editingIdx: -1,  // 当前编辑的边索引
    _nodes: [],      // 缓存的 NPC 列表 [{id, name}]
    _svgNS: 'http://www.w3.org/2000/svg',

    init(containerId) {
        this.containerEl = document.getElementById(containerId);
        if (!this.containerEl || this.svg) return;
        this.svg = document.createElementNS(this._svgNS, 'svg');
        this.svg.setAttribute('viewBox', '0 0 600 420');
        // 箭头标记定义
        const defs = document.createElementNS(this._svgNS, 'defs');
        const colors = {trust: '#4a9eff', affection: '#4acf6a', fear: '#e85555', neutral: '#888'};
        for (const [name, color] of Object.entries(colors)) {
            const marker = document.createElementNS(this._svgNS, 'marker');
            marker.setAttribute('id', 'arrow-' + name);
            marker.setAttribute('viewBox', '0 0 10 6');
            marker.setAttribute('refX', '10'); marker.setAttribute('refY', '3');
            marker.setAttribute('markerWidth', '8'); marker.setAttribute('markerHeight', '6');
            marker.setAttribute('orient', 'auto');
            const poly = document.createElementNS(this._svgNS, 'polygon');
            poly.setAttribute('points', '0,0 10,3 0,6');
            poly.setAttribute('fill', color);
            marker.appendChild(poly);
            defs.appendChild(marker);
        }
        this.svg.appendChild(defs);
        this.containerEl.appendChild(this.svg);
        this.editorEl = document.createElement('div');
        this.editorEl.id = 'rel-edge-editor-panel';
        this.containerEl.appendChild(this.editorEl);
    },

    _getNpcs() {
        const cards = document.querySelectorAll('#w-npcs .item-card');
        const npcs = [];
        cards.forEach(c => {
            const idInput = c.querySelector('.w-npc-id');
            const nameInput = c.querySelector('.w-npc-name');
            if (idInput && idInput.value.trim()) {
                npcs.push({ id: idInput.value.trim(), name: (nameInput && nameInput.value.trim()) || idInput.value.trim() });
            }
        });
        return npcs;
    },

    refresh() {
        this._nodes = this._getNpcs();
        // 移除引用不存在 NPC 的边
        const ids = new Set(this._nodes.map(n => n.id));
        const before = this.edges.length;
        this.edges = this.edges.filter(e => ids.has(e.from) && ids.has(e.to));
        const dropped = before - this.edges.length;
        if (dropped > 0 && !_isLoadingScript && _loadedTabs.has('characters') && typeof showNotification === 'function') {
            showNotification(`NPC 关系图：因 NPC 删除而移除了 ${dropped} 条关系`, 'warning');
        }
        this.selected = null;
        this.editingIdx = -1;
        this.editorEl.innerHTML = '';
        this.render();
    },

    render() {
        const ns = this._svgNS;
        // 保留 <defs>，清除其余
        while (this.svg.childNodes.length > 1) this.svg.removeChild(this.svg.lastChild);

        const nodes = this._nodes;
        if (nodes.length === 0) {
            this.svg.setAttribute('viewBox', '0 0 600 120');
            const txt = document.createElementNS(ns, 'text');
            txt.setAttribute('x', '300'); txt.setAttribute('y', '60');
            txt.setAttribute('text-anchor', 'middle');
            txt.setAttribute('fill', '#888'); txt.setAttribute('font-size', '13');
            txt.textContent = '请先添加 NPC，再来编辑关系网';
            this.svg.appendChild(txt);
            return;
        }

        const cx = 300, cy = 195, r = Math.min(170, 60 + nodes.length * 12);
        const svgH = Math.max(420, (cy + r + 40));
        this.svg.setAttribute('viewBox', `0 0 600 ${svgH}`);

        // 计算节点位置
        const pos = {};
        nodes.forEach((n, i) => {
            const angle = (2 * Math.PI * i / nodes.length) - Math.PI / 2;
            pos[n.id] = { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
        });

        // 绘制边
        const edgesGroup = document.createElementNS(ns, 'g');
        this.edges.forEach((edge, idx) => {
            const p1 = pos[edge.from], p2 = pos[edge.to];
            if (!p1 || !p2) return;
            const path = this._createEdgePath(p1, p2, edge, idx);
            path.addEventListener('click', (e) => { e.stopPropagation(); this._onEdgeClick(idx); });
            edgesGroup.appendChild(path);
        });
        this.svg.appendChild(edgesGroup);

        // 绘制节点
        nodes.forEach(n => {
            const p = pos[n.id];
            const g = document.createElementNS(ns, 'g');
            g.classList.add('rel-node');
            if (this.selected === n.id) g.classList.add('selected');

            const circle = document.createElementNS(ns, 'circle');
            circle.setAttribute('cx', p.x); circle.setAttribute('cy', p.y);
            circle.setAttribute('r', '22');
            circle.setAttribute('fill', 'var(--bg-card)');
            circle.setAttribute('stroke', 'var(--border)');
            circle.setAttribute('stroke-width', '1.5');

            const text = document.createElementNS(ns, 'text');
            text.setAttribute('x', p.x); text.setAttribute('y', p.y + 4);
            const label = n.name.length > 4 ? n.name.slice(0, 4) + '..' : n.name;
            text.textContent = label;

            g.appendChild(circle);
            g.appendChild(text);
            g.addEventListener('click', (e) => { e.stopPropagation(); this._onNodeClick(n.id); });
            this.svg.appendChild(g);
        });

        // 点击空白取消选中
        this.svg.onclick = () => { this.selected = null; this.render(); };
    },

    _edgeColor(edge) {
        const t = edge.trust, a = edge.affection, f = edge.fear;
        if (f > t && f > a) return 'fear';
        if (a > t && a > f) return 'affection';
        if (t > a && t > f) return 'trust';
        return 'neutral';
    },

    _createEdgePath(p1, p2, edge, idx) {
        const ns = this._svgNS;
        const path = document.createElementNS(ns, 'path');

        // 判断是否有反向边，决定弧线偏移方向
        const hasReverse = this.edges.some(e => e.from === edge.to && e.to === edge.from);
        const dx = p2.x - p1.x, dy = p2.y - p1.y;
        const len = Math.sqrt(dx * dx + dy * dy) || 1;
        // 法线方向
        const nx = -dy / len, ny = dx / len;

        // 箭头要指向目标节点边缘（radius=22），所以终点向回缩
        const nodeR = 22;
        const offset = hasReverse ? 20 : 0;
        const mx = (p1.x + p2.x) / 2 + nx * offset;
        const my = (p1.y + p2.y) / 2 + ny * offset;

        // 终点缩到圆边缘
        const endDx = p2.x - mx, endDy = p2.y - my;
        const endLen = Math.sqrt(endDx * endDx + endDy * endDy) || 1;
        const ex = p2.x - (endDx / endLen) * nodeR;
        const ey = p2.y - (endDy / endLen) * nodeR;

        // 起点也从圆边缘开始
        const startDx = mx - p1.x, startDy = my - p1.y;
        const startLen = Math.sqrt(startDx * startDx + startDy * startDy) || 1;
        const sx = p1.x + (startDx / startLen) * nodeR;
        const sy = p1.y + (startDy / startLen) * nodeR;

        path.setAttribute('d', `M ${sx} ${sy} Q ${mx} ${my} ${ex} ${ey}`);
        path.setAttribute('fill', 'none');

        const colorKey = this._edgeColor(edge);
        const colors = {trust: '#4a9eff', affection: '#4acf6a', fear: '#e85555', neutral: '#888'};
        path.setAttribute('stroke', colors[colorKey]);
        path.setAttribute('stroke-width', idx === this.editingIdx ? '3' : '1.8');
        if (!edge.initially_known) path.setAttribute('stroke-dasharray', '5,4');
        path.setAttribute('marker-end', `url(#arrow-${colorKey})`);
        path.classList.add('rel-edge');
        if (idx === this.editingIdx) path.classList.add('active');
        return path;
    },

    _onNodeClick(id) {
        if (this.selected === null || this.selected === id) {
            this.selected = (this.selected === id) ? null : id;
            this.render();
            return;
        }
        // 第二个节点 → 创建或编辑 A→B 边
        const fromId = this.selected;
        const toId = id;
        this.selected = null;
        let idx = this.edges.findIndex(e => e.from === fromId && e.to === toId);
        if (idx < 0) {
            this.edges.push({ from: fromId, to: toId, trust: 50, affection: 50, fear: 0, initially_known: true, description: '' });
            idx = this.edges.length - 1;
        }
        this.editingIdx = idx;
        this.render();
        this._showEdgeEditor(idx);
    },

    _onEdgeClick(idx) {
        this.editingIdx = idx;
        this.selected = null;
        this.render();
        this._showEdgeEditor(idx);
    },

    _showEdgeEditor(idx) {
        const edge = this.edges[idx];
        if (!edge) { this.editorEl.innerHTML = ''; return; }
        const fromName = this._nodes.find(n => n.id === edge.from)?.name || edge.from;
        const toName = this._nodes.find(n => n.id === edge.to)?.name || edge.to;

        this.editorEl.innerHTML = `
            <div class="rel-edge-editor">
                <h4>${escapeHtml(fromName)} → ${escapeHtml(toName)}</h4>
                <label><span>信任</span><input type="range" min="0" max="100" value="${edge.trust}" data-f="trust"><span class="rv">${edge.trust}</span></label>
                <label><span>好感</span><input type="range" min="0" max="100" value="${edge.affection}" data-f="affection"><span class="rv">${edge.affection}</span></label>
                <label><span>畏惧</span><input type="range" min="0" max="100" value="${edge.fear}" data-f="fear"><span class="rv">${edge.fear}</span></label>
                <label><input type="checkbox" ${edge.initially_known ? 'checked' : ''} data-f="known"> 开局即认识</label>
                <label><input type="checkbox" ${edge.initially_met ? 'checked' : ''} data-f="met"> 开局即熟识</label>
                <input type="text" placeholder="关系描述" value="${escapeHtml(edge.description || '')}" data-f="desc">
                <div class="edge-actions">
                    <button class="btn-secondary" data-act="flip">查看/创建 ${escapeHtml(toName)}→${escapeHtml(fromName)}</button>
                    <button class="btn-secondary" data-act="delete" style="color:var(--danger)">删除此边</button>
                </div>
            </div>`;

        // 绑定滑块
        this.editorEl.querySelectorAll('input[type="range"]').forEach(inp => {
            inp.oninput = () => {
                const f = inp.dataset.f;
                edge[f] = parseInt(inp.value, 10);
                inp.nextElementSibling.textContent = inp.value;
                this.render();
            };
        });
        // checkboxes
        const knownCb = this.editorEl.querySelector('input[data-f="known"]');
        knownCb.onchange = () => { edge.initially_known = knownCb.checked; this.render(); };
        const metCb = this.editorEl.querySelector('input[data-f="met"]');
        metCb.onchange = () => { edge.initially_met = metCb.checked; this.render(); };
        // 描述
        const descInput = this.editorEl.querySelector('input[data-f="desc"]');
        descInput.oninput = () => { edge.description = descInput.value; };
        // 按钮
        this.editorEl.querySelector('[data-act="flip"]').onclick = () => {
            let revIdx = this.edges.findIndex(e => e.from === edge.to && e.to === edge.from);
            if (revIdx < 0) {
                this.edges.push({ from: edge.to, to: edge.from, trust: 50, affection: 50, fear: 0, initially_known: true, initially_met: true, description: '' });
                revIdx = this.edges.length - 1;
            }
            this.editingIdx = revIdx;
            this.render();
            this._showEdgeEditor(revIdx);
        };
        this.editorEl.querySelector('[data-act="delete"]').onclick = () => {
            this.edges.splice(idx, 1);
            this.editingIdx = -1;
            this.editorEl.innerHTML = '';
            this.render();
        };
    },

    loadEdges(rels) {
        this.edges = [];
        if (!rels || !Array.isArray(rels)) return;
        const seen = new Set();
        for (const r of rels) {
            if ('from' in r && 'to' in r) {
                const key = r.from + '|' + r.to;
                if (seen.has(key)) continue;
                seen.add(key);
                this.edges.push({
                    from: r.from, to: r.to,
                    trust: r.trust ?? 50, affection: r.affection ?? 50, fear: r.fear ?? 0,
                    initially_known: r.initially_known !== false, initially_met: r.initially_met !== false, description: r.description || ''
                });
            } else if ('a' in r && 'b' in r) {
                // 旧格式 → 对称双边
                const vals = _NPC_REL_TYPE_MAP[r.type] || {trust:50,affection:50,fear:0};
                const ik = r.initially_known !== false;
                const im = r.initially_met !== false;
                const desc = r.description || '';
                const k1 = r.a + '|' + r.b, k2 = r.b + '|' + r.a;
                if (!seen.has(k1)) { seen.add(k1); this.edges.push({from: r.a, to: r.b, ...vals, initially_known: ik, initially_met: im, description: desc}); }
                if (!seen.has(k2)) { seen.add(k2); this.edges.push({from: r.b, to: r.a, ...vals, initially_known: ik, initially_met: im, description: desc}); }
            }
        }
    },

    toRelationships() {
        return this.edges.map(e => ({
            from: e.from, to: e.to,
            trust: e.trust, affection: e.affection, fear: e.fear,
            initially_known: e.initially_known, initially_met: e.initially_met, description: e.description
        }));
    }
};

function showNewScriptWizard() {
    editingScriptId = null;
    _rawScriptOverlay = {};
    document.getElementById('scripts-list-view').style.display = 'none';
    document.getElementById('script-wizard').style.display = 'block';
    document.getElementById('wizard-title').textContent = '新建剧本';
    clearWizard();
    _initAllPaginators();
    _NpcRelGraph.init('npc-rel-graph');
    _NpcRelGraph.refresh();
    const panel = document.getElementById('copilot-content');
    if (panel) panel._hasGenerateResult = false;
    _updateGuide();
    wizardStep('basic');
}

function closeWizard() {
    document.getElementById('script-wizard').style.display = 'none';
    document.getElementById('scripts-list-view').style.display = 'block';
    loadScripts();
}

async function wizardStep(step) {
    // 如果是从 json tab 切到其他 tab，反向同步 JSON 编辑器内容到 wizard 表单
    if (_currentWizardStep === 'json' && step !== 'json') {
        const editor = document.getElementById('script-json-editor');
        const text = editor ? editor.value.trim() : '';
        if (text) {
            let parsed;
            try {
                parsed = JSON.parse(text);
            } catch (e) {
                if (!confirm('JSON 格式有误（' + e.message + '），切换将丢失手动修改。继续切换吗？')) {
                    return;
                }
                parsed = null;
            }
            if (parsed) {
                try {
                    _rawScriptOverlay = parsed;
                    loadScriptIntoWizard(parsed);
                } catch (e) {
                    console.error('JSON 应用到 wizard 失败:', e);
                    if (typeof showNotification === 'function') {
                        showNotification('JSON 应用失败: ' + e.message, 'error');
                    } else {
                        alert('JSON 应用失败: ' + e.message);
                    }
                    return;
                }
            }
        }
    }
    _currentWizardStep = step;
    document.querySelectorAll('.wizard-step').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.wizard-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('step-' + step).classList.add('active');
    document.querySelector(`.wizard-tab[data-step="${step}"]`).classList.add('active');

    // 延迟加载：首次切换到某 tab 时填充其编辑器
    _ensureTabLoaded(step);

    // Sync JSON when switching to JSON tab — 先确保所有 tab 都已加载
    if (step === 'json') {
        for (const t of _ALL_CONTENT_TABS) _ensureTabLoaded(t);
        await _waitAllTabsLoaded();
        document.getElementById('script-json-editor').value = JSON.stringify(await buildScriptFromWizard(), null, 2);
    }

    _showDepHint(step);
    _updateGuide();
}

function clearWizard() {
    _pendingScript = null;
    _loadedTabs.clear();
    document.getElementById('w-script-id').value = '';
    document.getElementById('w-script-name').value = '';
    document.getElementById('w-start-time').value = '2025-09-01T07:00';
    document.getElementById('w-background').value = '';
    document.getElementById('w-bg-prompt').value = '';
    document.getElementById('w-opening-text').value = '';
    document.querySelectorAll('.w-choice-text').forEach(e => e.value = '');
    document.querySelectorAll('.w-choice-result').forEach(e => e.value = '');
    document.getElementById('w-pc-bio').value = '';
    document.getElementById('w-pc-personality').value = '';
    document.getElementById('w-pc-portrait').value = '';
    document.getElementById('w-pc-location').value = '';
    document.getElementById('w-pc-goal').value = '';
    document.getElementById('w-npcs').innerHTML = '';
    _NpcRelGraph.edges = [];
    _NpcRelGraph.selected = null;
    _NpcRelGraph.editingIdx = -1;
    if (_NpcRelGraph.editorEl) _NpcRelGraph.editorEl.innerHTML = '';
    const presetsEl = document.getElementById('w-presets');
    if (presetsEl) presetsEl.innerHTML = '';
    const allowCustomEl = document.getElementById('w-allow-custom-char');
    if (allowCustomEl) allowCustomEl.checked = true;
    document.getElementById('w-locations').innerHTML = '';
    document.getElementById('w-world-props').innerHTML = '';
    document.getElementById('w-persistent-states').innerHTML = '';
    document.getElementById('w-cyclic-events').innerHTML = '';
    document.getElementById('w-onetime-events').innerHTML = '';
    document.getElementById('w-tone-rules').innerHTML = '';
    document.getElementById('w-quest-templates').innerHTML = '';
    document.getElementById('w-random-items').innerHTML = '';
    document.getElementById('w-inventory').innerHTML = '';
    document.getElementById('w-lorebook').innerHTML = '';
    document.getElementById('w-organizations').innerHTML = '';
    const orgRelsEl = document.getElementById('w-org-rels');
    if (orgRelsEl) orgRelsEl.innerHTML = '';
    const storyTreesEl = document.getElementById('w-story-trees');
    if (storyTreesEl) storyTreesEl.innerHTML = '';
    for (const p of Object.values(_paginators)) p.page = 0;
}

// --- Build script JSON from wizard fields ---

function _ensureAllTabsLoaded() {
    for (const t of _ALL_CONTENT_TABS) _ensureTabLoaded(t);
}

async function _waitAllTabsLoaded() {
    _ensureAllTabsLoaded();
    const pending = Object.values(_tabLoadPromises);
    if (pending.length) await Promise.all(pending);
}

function _preserveScriptVisibilityMetadata(script, source) {
    const copyMissing = (items, originals, fields, keyFor = item => item.id) => {
        const references = new Map((originals || []).map(item => [keyFor(item), item]));
        for (const item of items || []) {
            const reference = references.get(keyFor(item));
            if (!reference) continue;
            for (const field of fields) {
                if (!(field in item) && field in reference) {
                    item[field] = JSON.parse(JSON.stringify(reference[field]));
                }
            }
        }
    };
    const sections = {
        world_properties: ['visibility'],
        persistent_states: ['expiration_visibility'],
        locations: ['public_description', 'visual_profile'],
        npcs: ['public_profile', 'visual_profile'],
        lorebook: ['visibility', 'discoverable', 'known_by_npcs'],
        cyclic_events: ['visibility', 'known_by_npcs'],
        one_time_events: ['visibility', 'known_by_npcs'],
    };
    for (const [section, fields] of Object.entries(sections)) {
        copyMissing(script[section], source[section], fields);
    }
    for (const tree of script.story_tree?.trees || []) {
        const reference = source.story_tree?.trees?.find(original => original.id === tree.id);
        copyMissing(tree.nodes, reference?.nodes, ['visibility', 'known_by_npcs']);
    }
    copyMissing(script.npc_relationships, source.npc_relationships,
        ['public_description', 'public_type'],
        relation => JSON.stringify([relation.from ?? relation.a, relation.to ?? relation.b]));
}

async function buildScriptFromWizard() {
    await _waitAllTabsLoaded();
    // 用过的 ID 集合，避免重名 NPC/地点等生成相同拼音 ID
    const _usedIds = new Set();
    const _uniqueId = (base) => {
        let id = base || ('item_' + Date.now());
        if (!_usedIds.has(id)) { _usedIds.add(id); return id; }
        let n = 2;
        while (_usedIds.has(id + '-' + n)) n++;
        const out = id + '-' + n;
        _usedIds.add(out);
        return out;
    };
    // 只保留 wizard 未显式构建但后端使用的 overlay 字段（白名单）
    const _OVERLAY_KEYS = [
        'milestones', 'consequences', 'stage_models', 'post_processing_rules',
        'custom_opening_instructions', '_preset_opening_text', '_preset_opening_choices',
        'visibility_policy', 'location_aliases',
    ];
    const _pickedOverlay = {};
    for (const k of _OVERLAY_KEYS) {
        if (k in _rawScriptOverlay) _pickedOverlay[k] = _rawScriptOverlay[k];
    }
    const script = {
        ..._pickedOverlay,
        script_id: document.getElementById('w-script-id').value || 'untitled',
        script_name: document.getElementById('w-script-name').value || '未命名',
        version: '1.0',
        start_time: (document.getElementById('w-start-time').value || '2025-09-01T07:00').replace(' ', 'T'),
        world_background: document.getElementById('w-background').value || '',
        settings: {
            dice_check: { default_enabled: document.getElementById('w-dice-enabled').checked, player_can_toggle: true },
            check_rule: document.getElementById('w-check-rule').value || 'default',
            fixed_opening: { default_enabled: document.getElementById('w-fixed-opening').checked, player_can_toggle: true },
            allow_player_control_switch: false,
            lorebook_token_budget: parseInt(document.getElementById('w-lore-token-budget').value) || 0,
            lorebook_max_recursion: parseInt(document.getElementById('w-lore-max-recursion').value) ?? 2,
            summary_word_threshold: parseInt(document.getElementById('w-summary-word-threshold').value) || 3000,
            reasoning_lookback: parseInt(document.getElementById('w-reasoning-lookback').value) || 2,
            vector_summarize: document.getElementById('w-vector-summarize').checked,
        },
        opening: {
            text: document.getElementById('w-opening-text').value || '',
            choices: [],
        },
        random_items: [],
        variables: [],
        triggers: [],
        regex_scripts: [],
        locations: [],
        player_character: {
            id: 'player',
            bio: document.getElementById('w-pc-bio').value || '',
            personality: document.getElementById('w-pc-personality').value || '',
            portrait_desc: document.getElementById('w-pc-portrait').value || '',
            long_term_goal: document.getElementById('w-pc-goal').value || '',
            attributes: {},
            relationships: {},
        },
        npcs: [],
        player_presets: [],
        allow_custom_character: document.getElementById('w-allow-custom-char').checked,
        world_properties: [],
        persistent_states: [],
        cyclic_events: [],
        one_time_events: [],
        lorebook: [],
        organizations: [],
    };
    const _pcLoc = (document.getElementById('w-pc-location').value || '').trim();
    if (_pcLoc) script.player_character.initial_location = _pcLoc;

    // Opening choices
    document.querySelectorAll('#w-opening-choices .choice-editor').forEach((el, i) => {
        const text = el.querySelector('.w-choice-text').value;
        if (text) {
            const type = el.querySelector('.w-choice-type').value;
            const result = {
                type,
                description: el.querySelector('.w-choice-result').value || '',
                state_changes: [],
            };
            // S4: 条件类型收集 threshold/success/failure
            if (type === 'conditional') {
                result.threshold = parseInt(el.querySelector('.w-choice-threshold')?.value) || 50;
                result.success = { description: el.querySelector('.w-choice-success')?.value || '' };
                result.failure = { description: el.querySelector('.w-choice-failure')?.value || '' };
            }
            script.opening.choices.push({
                id: `open_${script.opening.choices.length}`,
                text,
                result,
            });
        }
    });

    // Attributes
    document.querySelectorAll('#w-attributes .attr-editor').forEach(el => {
        const name = el.querySelector('.w-attr-name').value.trim();
        const rawVal = parseInt(el.querySelector('.w-attr-val').value);
        const val = Number.isNaN(rawVal) ? 50 : rawVal;
        const rule = el.querySelector('.w-attr-rule').value || '';
        if (name) {
            const key = toPinyin(name);
            script.player_character.attributes[key] = { value: val, min: 0, max: 100, rule, display_name: name };
        }
    });

    // Initial inventory
    script.player_character.initial_inventory = [];
    document.querySelectorAll('#w-inventory .attr-editor').forEach(el => {
        const item = el.querySelector('.w-inv-item').value;
        if (item) {
            const entry = {
                item,
                quantity: parseInt(el.querySelector('.w-inv-qty').value) || 1,
            };
            const desc = el.querySelector('.w-inv-desc')?.value;
            if (desc) entry.description = desc;
            const effectStr = el.querySelector('.w-inv-effect')?.value?.trim();
            if (effectStr) {
                try { entry.use_effect = JSON.parse(effectStr); } catch(_) {}
            }
            script.player_character.initial_inventory.push(entry);
        }
    });

    // Player presets
    document.querySelectorAll('#w-presets .preset-editor').forEach(el => {
        const name = el.querySelector('.w-preset-name').value || '';
        if (name) {
            const preset = {
                id: el.querySelector('.w-preset-id').value || toPinyin(name),
                name,
                bio: el.querySelector('.w-preset-bio').value || '',
                personality: el.querySelector('.w-preset-personality')?.value || '',
                long_term_goal: el.querySelector('.w-preset-goal').value || '',
                portrait_desc: el.querySelector('.w-preset-portrait').value || '',
                opening_text: el.querySelector('.w-preset-opening')?.value || '',
                attributes: {},
            };
            const _presetLoc = (el.querySelector('.w-preset-location').value || '').trim();
            if (_presetLoc) preset.initial_location = _presetLoc;
            el.querySelectorAll('.w-preset-attr-val').forEach(inp => {
                const attrName = inp.dataset.attr;
                const v = inp.value;
                if (attrName && v !== '') {
                    preset.attributes[attrName] = { value: parseInt(v) || 50, min: 0, max: 100 };
                }
            });
            // Collect preset opening choices
            const presetChoices = [];
            el.querySelectorAll('.preset-choice-row').forEach((row, ci) => {
                const text = row.querySelector('.w-preset-choice-text').value.trim();
                if (text) {
                    presetChoices.push({
                        id: `open_${ci}`,
                        text,
                        result: {
                            type: 'deterministic',
                            description: row.querySelector('.w-preset-choice-result').value.trim(),
                            state_changes: [],
                        },
                    });
                }
            });
            if (presetChoices.length > 0) {
                preset.opening_choices = presetChoices;
            }
            script.player_presets.push(preset);
        }
    });

    // NPCs
    document.querySelectorAll('#w-npcs .npc-editor').forEach(el => {
        const id = el.querySelector('.w-npc-id').value || '';
        const name = el.querySelector('.w-npc-name').value || '';
        if (id || name) {
            const relatedLoreRaw = (el.querySelector('.w-npc-lore')?.value || '').trim();
            const npcOrgs = [];
            el.querySelectorAll('.npc-org-row').forEach(row => {
                const org_id = row.querySelector('.w-npc-orgid')?.value?.trim() || '';
                if (org_id) {
                    const entry = { org_id };
                    const rankVal = row.querySelector('.w-npc-orgrank')?.value?.trim();
                    if (rankVal) entry.rank = parseInt(rankVal);
                    const role = row.querySelector('.w-npc-orgrole')?.value?.trim();
                    if (role) entry.role = role;
                    npcOrgs.push(entry);
                }
            });
            const npc = {
                id: id || toPinyin(name),
                name,
                bio: el.querySelector('.w-npc-bio').value || '',
                personality: el.querySelector('.w-npc-personality').value || '',
                capabilities: el.querySelector('.w-npc-caps').value || '',
                title: el.querySelector('.w-npc-title')?.value || '',
                organizations: npcOrgs,
                related_lore: relatedLoreRaw ? relatedLoreRaw.split(',').map(s => s.trim()).filter(Boolean) : [],
                attitude_toward_player: parseInt(el.querySelector('.w-npc-attitude').value) || 50,
                talkativeness: parseInt(el.querySelector('.w-npc-talkativeness')?.value) ?? 50,
                known: !el.querySelector('.w-npc-known') || el.querySelector('.w-npc-known').checked,
                met: !el.querySelector('.w-npc-met') || el.querySelector('.w-npc-met').checked,
                portrait_desc: el.querySelector('.w-npc-portrait')?.value || '',
            };
            const _superior = (el.querySelector('.w-npc-superior')?.value || '').trim();
            if (_superior) npc.superior = _superior;
            const _defLoc = (el.querySelector('.w-npc-location')?.value || '').trim();
            if (_defLoc) npc.default_location = _defLoc;
            // S5: 收集日程表
            const schedRows = el.querySelectorAll('.npc-schedule-row');
            if (schedRows.length > 0) {
                npc.schedule = [];
                schedRows.forEach(row => {
                    const time_range = row.querySelector('.w-sched-time')?.value || '';
                    const location = row.querySelector('.w-sched-loc')?.value || '';
                    const activity = row.querySelector('.w-sched-act')?.value || '';
                    if (time_range || location || activity) {
                        const schedEntry = { time_range, location, activity };
                        const cond = row.querySelector('.w-sched-cond')?.value || '';
                        const prio = parseInt(row.querySelector('.w-sched-prio')?.value);
                        if (cond) schedEntry.condition = cond;
                        if (prio) schedEntry.priority = prio;
                        npc.schedule.push(schedEntry);
                    }
                });
            }
            // 收集 NPC 目标
            const goalRows = el.querySelectorAll('.npc-goal-row');
            if (goalRows.length > 0) {
                npc.goals = [];
                goalRows.forEach(row => {
                    const gid = row.querySelector('.w-goal-id')?.value || '';
                    const desc = row.querySelector('.w-goal-desc')?.value || '';
                    if (gid || desc) {
                        const goal = {
                            id: gid || toPinyin(desc).slice(0, 20),
                            description: desc,
                            type: row.querySelector('.w-goal-type')?.value || 'short_term',
                            priority: row.querySelector('.w-goal-priority')?.value || 'medium',
                        };
                        const cond = row.querySelector('.w-goal-condition')?.value || '';
                        if (cond) goal.condition_met = cond;
                        const prog = row.querySelector('.w-goal-progress')?.value || '';
                        if (prog) goal.progress_hint = prog;
                        const conflict = row.querySelector('.w-goal-conflict')?.value || '';
                        if (conflict) goal.conflict_with_player = conflict;
                        npc.goals.push(goal);
                    }
                });
            }
            script.npcs.push(npc);
        }
    });

    // NPC-NPC relationships（从 SVG 关系图读取）
    script.npc_relationships = _NpcRelGraph.toRelationships();

    // Locations
    document.querySelectorAll('#w-locations .loc-editor').forEach(el => {
        const name = el.querySelector('.w-loc-name').value;
        if (name) {
            const loc = {
                id: el.querySelector('.w-loc-id').value || toPinyin(name),
                name,
                description: el.querySelector('.w-loc-desc').value || '',
                initially_visible: el.querySelector('.w-loc-visible').checked,
            };
            // G12: 收集连接关系
            const connRaw = (el.querySelector('.w-loc-connections')?.value || '').trim();
            if (connRaw) {
                loc.connections = connRaw.split(',').map(s => s.trim()).filter(Boolean);
            }
            const travelTime = (el.querySelector('.w-loc-travel-time')?.value || '').trim();
            if (travelTime) loc.travel_time = travelTime;
            // 收集可互动元素
            const iaRows = el.querySelectorAll('.w-loc-interactables > div');
            if (iaRows.length > 0) {
                loc.interactables = [];
                iaRows.forEach(row => {
                    const iaName = row.querySelector('.w-ia-name')?.value || '';
                    if (!iaName) return;
                    const ia = {
                        id: row.querySelector('.w-ia-id')?.value || toPinyin(iaName),
                        name: iaName,
                        description: row.querySelector('.w-ia-desc')?.value || '',
                        action_hint: row.querySelector('.w-ia-hint')?.value || '',
                    };
                    const cond = row.querySelector('.w-ia-cond')?.value || '';
                    if (cond) ia.condition = cond;
                    if (row.querySelector('.w-ia-onetime')?.checked) ia.one_time = true;
                    const revRaw = (row.querySelector('.w-ia-reveals')?.value || '').trim();
                    if (revRaw) ia.reveals = revRaw.split(',').map(s => s.trim()).filter(Boolean);
                    loc.interactables.push(ia);
                });
                if (loc.interactables.length === 0) delete loc.interactables;
            }
            script.locations.push(loc);
        }
    });

    // World properties
    document.querySelectorAll('#w-world-props .prop-editor').forEach(el => {
        const name = el.querySelector('.w-prop-name').value;
        if (name) {
            script.world_properties.push({
                id: el.querySelector('.w-prop-id')?.value || toPinyin(name),
                name,
                value: el.querySelector('.w-prop-value').value || '',
                rule: el.querySelector('.w-prop-rule').value || '',
            });
        }
    });

    // Persistent states
    document.querySelectorAll('#w-persistent-states .ps-editor').forEach(el => {
        const desc = el.querySelector('.w-ps-desc').value;
        if (desc) {
            const expiry = el.querySelector('.w-ps-expiry').value;
            script.persistent_states.push({
                id: el.querySelector('.w-ps-id')?.value || toPinyin(desc.substring(0, 10)),
                name: el.querySelector('.w-ps-name')?.value || '',
                description: desc,
                initially_active: el.querySelector('.w-ps-active')?.checked ?? true,
                expires_at: expiry || null,
            });
        }
    });

    // Cyclic events
    document.querySelectorAll('#w-cyclic-events .ce-editor').forEach(el => {
        const desc = el.querySelector('.w-ce-desc').value;
        if (desc) {
            const ceExpiry = el.querySelector('.w-ce-expiry')?.value || '';
            const ceCondition = el.querySelector('.w-ce-condition')?.value || '';
            const ceFireRaw = el.querySelector('.w-ce-fire-events')?.value || '';
            const ceFireEvents = ceFireRaw ? ceFireRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
            const ceActRaw = el.querySelector('.w-ce-activate-events')?.value || '';
            const ceActEvents = ceActRaw ? ceActRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
            script.cyclic_events.push({
                id: el.querySelector('.w-ce-id')?.value || toPinyin(desc.substring(0, 10)),
                name: el.querySelector('.w-ce-name')?.value || '',
                description: desc,
                visibility: el.querySelector('.w-ce-visibility').value,
                frequency_value: parseInt(el.querySelector('.w-ce-freq-val').value) || 1,
                frequency_unit: el.querySelector('.w-ce-freq-unit').value || 'day',
                first_trigger: el.querySelector('.w-ce-first').value || script.start_time,
                condition: ceCondition || undefined,
                expires_at: ceExpiry || null,
                fire_events: ceFireEvents.length ? ceFireEvents : undefined,
                activate_events: ceActEvents.length ? ceActEvents : undefined,
            });
        }
    });

    // One-time events
    document.querySelectorAll('#w-onetime-events .ote-editor').forEach(el => {
        const desc = el.querySelector('.w-ote-desc').value;
        if (desc) {
            const oteCondition = el.querySelector('.w-ote-condition')?.value || '';
            const oteFireRaw = el.querySelector('.w-ote-fire-events')?.value || '';
            const oteFireEvents = oteFireRaw ? oteFireRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
            const oteActRaw = el.querySelector('.w-ote-activate-events')?.value || '';
            const oteActEvents = oteActRaw ? oteActRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
            script.one_time_events.push({
                id: el.querySelector('.w-ote-id')?.value || toPinyin(desc.substring(0, 10)),
                name: el.querySelector('.w-ote-name')?.value || '',
                description: desc,
                visibility: el.querySelector('.w-ote-visibility').value,
                trigger_time: el.querySelector('.w-ote-time').value || '',
                condition: oteCondition || undefined,
                fire_events: oteFireEvents.length ? oteFireEvents : undefined,
                activate_events: oteActEvents.length ? oteActEvents : undefined,
            });
        }
    });

    // Tone rules
    const toneRules = [];
    document.querySelectorAll('#w-tone-rules .tone-rule-editor').forEach(el => {
        const name = el.querySelector('.w-tone-name').value;
        if (name) {
            toneRules.push({
                id: el.querySelector('.w-tone-id')?.value || toPinyin(name.substring(0, 10)),
                name,
                condition: el.querySelector('.w-tone-condition')?.value || '',
                tone: el.querySelector('.w-tone-tone')?.value || '',
                narrative_style: el.querySelector('.w-tone-style')?.value || '',
                priority: parseInt(el.querySelector('.w-tone-priority')?.value) || 0,
            });
        }
    });
    if (toneRules.length > 0) script.tone_rules = toneRules;

    // Quest templates
    const questTemplates = [];
    document.querySelectorAll('#w-quest-templates .quest-tpl-editor').forEach(el => {
        const name = el.querySelector('.w-qt-name').value;
        if (name) {
            questTemplates.push({
                id: el.querySelector('.w-qt-id')?.value || toPinyin(name.substring(0, 15)),
                name,
                description: el.querySelector('.w-qt-desc')?.value || '',
                condition: el.querySelector('.w-qt-condition')?.value || '',
                trigger_hint: el.querySelector('.w-qt-trigger')?.value || '',
                reward_hint: el.querySelector('.w-qt-reward')?.value || '',
                cooldown_turns: parseInt(el.querySelector('.w-qt-cooldown')?.value) || 10,
            });
        }
    });
    if (questTemplates.length > 0) script.quest_templates = questTemplates;

    // Random items
    document.querySelectorAll('#w-random-items .ri-editor').forEach(el => {
        const desc = el.querySelector('.w-ri-desc').value;
        if (desc) {
            const ranges = [];
            el.querySelectorAll('.ri-range-row').forEach(row => {
                const label = row.querySelector('.w-range-label').value;
                if (label) {
                    const rangeObj = {
                        min: parseInt(row.querySelector('.w-range-min').value) || 0,
                        max: parseInt(row.querySelector('.w-range-max').value) || 100,
                        label,
                        description: row.querySelector('.w-range-desc')?.value || '',
                    };
                    const scRaw = row.getAttribute('data-sc');
                    if (scRaw) { try { rangeObj.state_changes = JSON.parse(scRaw); } catch(_) {} }
                    ranges.push(rangeObj);
                }
            });
            const riLinked = el.querySelector('.w-ri-linked-event')?.value || '';
            const riCondition = el.querySelector('.w-ri-condition')?.value || '';
            const keepHigh = parseInt(el.querySelector('.w-ri-keep-high')?.value) || 0;
            const keepLow = parseInt(el.querySelector('.w-ri-keep-low')?.value) || 0;
            const diceObj = {
                count: parseInt(el.querySelector('.w-ri-dice-count').value) || 1,
                faces: parseInt(el.querySelector('.w-ri-dice-faces').value) || 100,
                modifier: parseInt(el.querySelector('.w-ri-dice-mod').value) || 0,
            };
            if (keepHigh) diceObj.keep_highest = keepHigh;
            if (keepLow) diceObj.keep_lowest = keepLow;
            script.random_items.push({
                id: el.querySelector('.w-ri-id').value || toPinyin(desc.substring(0, 10)),
                description: desc,
                trigger: el.querySelector('.w-ri-trigger').value || '永远生效',
                trigger_type: el.querySelector('.w-ri-type').value || 'always',
                duration_turns: parseInt(el.querySelector('.w-ri-duration').value) || 0,
                cooldown_turns: parseInt(el.querySelector('.w-ri-cooldown').value) || 0,
                linked_event_id: riLinked || undefined,
                condition: riCondition || undefined,
                dice: diceObj,
                ranges,
            });
        }
    });

    // Script variables
    document.querySelectorAll('#w-variables .var-editor').forEach(el => {
        const vid = el.querySelector('.w-var-id').value.trim();
        if (!vid) return;
        const vtype = el.querySelector('.w-var-type').value || 'number';
        const obj = {
            id: vid,
            name: el.querySelector('.w-var-name').value || vid,
            type: vtype,
            default: vtype === 'number' ? (parseFloat(el.querySelector('.w-var-default').value) || 0)
                   : vtype === 'bool' ? (el.querySelector('.w-var-default').value === 'true')
                   : (el.querySelector('.w-var-default').value || ''),
        };
        if (vtype === 'number') {
            const mn = el.querySelector('.w-var-min').value;
            const mx = el.querySelector('.w-var-max').value;
            if (mn !== '') obj.min = parseInt(mn);
            if (mx !== '') obj.max = parseInt(mx);
        }
        script.variables.push(obj);
    });

    // Triggers
    script.triggers = [];
    document.querySelectorAll('#w-triggers .trigger-editor').forEach(el => {
        const paramsStr = el.querySelector('.w-trig-params').value.trim();
        let params = {};
        if (paramsStr) { try { params = JSON.parse(paramsStr); } catch(_) {} }
        script.triggers.push({
            id: 't_' + Math.random().toString(36).slice(2, 8),
            event: el.querySelector('.w-trig-event').value,
            action: el.querySelector('.w-trig-action').value,
            condition: el.querySelector('.w-trig-condition').value || undefined,
            params,
            enabled: el.querySelector('.w-trig-enabled').checked,
        });
    });

    // Regex scripts
    script.regex_scripts = [];
    document.querySelectorAll('#w-regex-scripts .regex-editor').forEach(el => {
        const find = el.querySelector('.w-rx-find').value.trim();
        if (!find) return;
        script.regex_scripts.push({
            id: 'rx_' + Math.random().toString(36).slice(2, 8),
            name: el.querySelector('.w-rx-name').value || '',
            find,
            replace: el.querySelector('.w-rx-replace').value || '',
            placement: el.querySelector('.w-rx-placement').value || 'ai_output',
            enabled: el.querySelector('.w-rx-enabled').checked,
        });
    });

    // Lorebook entries
    document.querySelectorAll('#w-lorebook .lore-editor').forEach(el => {
        const keys = el.querySelector('.w-lore-keys').value;
        if (keys) {
            script.lorebook.push({
                id: el.querySelector('.w-lore-id').value || toPinyin(keys.split(',')[0] || 'lore'),
                keys: keys.split(',').map(k => k.trim()).filter(Boolean),
                secondary_keys: (el.querySelector('.w-lore-secondary-keys').value || '')
                    .split(',').map(k => k.trim()).filter(Boolean),
                content: el.querySelector('.w-lore-content').value || '',
                position: el.querySelector('.w-lore-position').value || 'after_world',
                enabled: true,
                constant: el.querySelector('.w-lore-constant').checked,
                priority: parseInt(el.querySelector('.w-lore-priority').value) || 100,
                scan_depth: 3,
                comment: el.querySelector('.w-lore-comment').value || '',
                related_entries: (el.querySelector('.w-lore-related')?.value || '').split(',').map(s => s.trim()).filter(Boolean),
                probability: parseInt(el.querySelector('.w-lore-probability')?.value) ?? 100,
                sticky: parseInt(el.querySelector('.w-lore-sticky')?.value) || 0,
                cooldown: parseInt(el.querySelector('.w-lore-cooldown')?.value) || 0,
                group: el.querySelector('.w-lore-group')?.value || '',
                group_weight: parseInt(el.querySelector('.w-lore-group-weight')?.value) || 100,
                depth: parseInt(el.querySelector('.w-lore-depth')?.value) || 4,
                role: el.querySelector('.w-lore-role')?.value || 'system',
            });
        }
    });

    // Organizations
    document.querySelectorAll('#w-organizations .org-editor').forEach(el => {
        const name = el.querySelector('.w-org-name').value;
        if (name) {
            const hierarchy = [];
            el.querySelectorAll('.w-org-rank-row').forEach(row => {
                const rank = parseInt(row.querySelector('.w-org-rank-num').value);
                const title = row.querySelector('.w-org-rank-title').value.trim();
                if (!isNaN(rank) && title) hierarchy.push({ rank, title });
            });
            const org = {
                id: el.querySelector('.w-org-id').value || toPinyin(name),
                name,
                type: el.querySelector('.w-org-type').value || '',
                stance: el.querySelector('.w-org-stance')?.value || '',
                aliases: (el.querySelector('.w-org-aliases')?.value || '').split(',').map(s => s.trim()).filter(Boolean),
                description: el.querySelector('.w-org-desc').value || '',
            };
            const _parentOrg = (el.querySelector('.w-org-parent')?.value || '').trim();
            if (_parentOrg) org.parent_org = _parentOrg;
            const _leader = (el.querySelector('.w-org-leader').value || '').trim();
            if (_leader) org.leader = _leader;
            if (hierarchy.length) org.hierarchy = hierarchy;
            const _initRep = el.querySelector('.w-org-reputation')?.value;
            if (_initRep !== '' && _initRep != null) org.initial_reputation = parseInt(_initRep) || 50;
            const orgGoals = [];
            el.querySelectorAll('.org-goals-rows .npc-goal-row').forEach(row => {
                const goal = {};
                const gid = row.querySelector('.w-goal-id')?.value?.trim();
                if (gid) goal.id = gid;
                const gdesc = row.querySelector('.w-goal-desc')?.value?.trim();
                if (gdesc) goal.description = gdesc;
                const gtype = row.querySelector('.w-goal-type')?.value;
                if (gtype) goal.type = gtype;
                const gprio = row.querySelector('.w-goal-priority')?.value;
                if (gprio) goal.priority = gprio;
                const gcond = row.querySelector('.w-goal-condition')?.value?.trim();
                if (gcond) goal.condition_met = gcond;
                const gprog = row.querySelector('.w-goal-progress')?.value?.trim();
                if (gprog) goal.progress_hint = gprog;
                const gconf = row.querySelector('.w-goal-conflict')?.value?.trim();
                if (gconf) goal.conflict_with_player = gconf;
                if (goal.id || goal.description) orgGoals.push(goal);
            });
            if (orgGoals.length) org.goals = orgGoals;
            script.organizations.push(org);
        }
    });

    // Org relationships — 先清空避免与 _rawScriptOverlay 残留叠加
    script.org_relationships = [];
    document.querySelectorAll('#w-org-rels .org-rel-editor').forEach(el => {
        const a = el.querySelector('.w-orgrel-a').value;
        const b = el.querySelector('.w-orgrel-b').value;
        if (a && b) {
            const visibility = el.querySelector('.w-orgrel-visibility').value;
            const original = el._orgRelSource;
            const relation = {
                ...(original?.a === a && original?.b === b ? original : {}),
                a,
                b,
                type: el.querySelector('.w-orgrel-type').value || 'neutral',
                description: el.querySelector('.w-orgrel-desc').value || '',
            };
            if (visibility === 'public') delete relation.visibility;
            else relation.visibility = visibility;
            script.org_relationships.push(relation);
        }
    });

    // Story tree
    if (typeof stIsGraphMode === 'function' && stIsGraphMode()) {
        script.story_tree = typeof getStoryTreeFromEditor === 'function' ? getStoryTreeFromEditor() : undefined;
        if (typeof getEventsFromEditor === 'function') {
            const edEvts = getEventsFromEditor();
            script.one_time_events = edEvts.one_time_events;
            script.cyclic_events = edEvts.cyclic_events;
        }
    } else {
    const storyTrees = [];
    document.querySelectorAll('#w-story-trees .story-tree-editor').forEach(treeEl => {
        const treeName = treeEl.querySelector('.w-st-name')?.value?.trim();
        if (!treeName) return;
        const tree = {
            id: treeEl.querySelector('.w-st-id')?.value || toPinyin(treeName),
            name: treeName,
            description: treeEl.querySelector('.w-st-desc')?.value || '',
            icon: treeEl.querySelector('.w-st-icon')?.value || 'scroll',
            nodes: [],
        };
        treeEl.querySelectorAll('.story-node-editor').forEach(nodeEl => {
            const nodeName = nodeEl.querySelector('.w-stn-name')?.value?.trim();
            if (!nodeName) return;
            const nodeCondition = (nodeEl.querySelector('.w-stn-condition')?.value || '').trim();
            const node = {
                id: nodeEl.querySelector('.w-stn-id')?.value || toPinyin(nodeName),
                name: nodeName,
                description: nodeEl.querySelector('.w-stn-desc')?.value || '',
                type: nodeEl.querySelector('.w-stn-type')?.value || 'auto',
                requires: (nodeEl.querySelector('.w-stn-requires')?.value || '').split(',').map(s => s.trim()).filter(Boolean),
                condition: nodeCondition || undefined,
                on_complete_unlock: (nodeEl.querySelector('.w-stn-unlock')?.value || '').split(',').map(s => s.trim()).filter(Boolean),
            };
            const activateEvts = (nodeEl.querySelector('.w-stn-activate-events')?.value || '').split(',').map(s => s.trim()).filter(Boolean);
            if (activateEvts.length) node.activate_events = activateEvts;
            if (node.type === 'trigger') {
                const evts = (nodeEl.querySelector('.w-stn-event')?.value || '').split(',').map(s => s.trim()).filter(Boolean);
                if (evts.length) node.event = evts;
            }
            if (node.type === 'periodic') {
                const cd = parseInt(nodeEl.querySelector('.w-stn-cooldown')?.value);
                if (cd > 0) node.cooldown = cd;
                const wt = parseInt(nodeEl.querySelector('.w-stn-weight')?.value);
                if (wt > 0) node.weight = wt;
                node.repeatable = nodeEl.querySelector('.w-stn-repeatable')?.checked !== false;
            }
            const effectsRaw = nodeEl.querySelector('.w-stn-effects')?.value?.trim();
            if (effectsRaw) { try { node.effects = JSON.parse(effectsRaw); } catch(_) {} }
            const choicesRaw = nodeEl.querySelector('.w-stn-choices')?.value?.trim();
            if (choicesRaw) { try { node.choices = JSON.parse(choicesRaw); } catch(_) {} }
            const dur = parseInt(nodeEl.querySelector('.w-stn-duration')?.value);
            if (dur > 0) node.duration_turns = dur;
            tree.nodes.push(node);
        });
        storyTrees.push(tree);
    });
    script.story_tree = storyTrees.length > 0 ? { trees: storyTrees } : undefined;
    } // end else (form mode)

    // 后处理：确保每个集合内 ID 唯一（同名 NPC/地点会生成相同拼音 ID）
    // 返回 oldId→newId 重命名映射，供下游修补关系引用
    const _dedupIds = (arr) => {
        const renames = {};
        if (!Array.isArray(arr)) return renames;
        const seen = new Set();
        for (const item of arr) {
            if (!item || typeof item !== 'object' || !item.id) continue;
            const oldId = item.id;
            let id = oldId;
            if (seen.has(id)) {
                let n = 2;
                while (seen.has(id + '-' + n)) n++;
                id = id + '-' + n;
                item.id = id;
                renames[oldId] = id;
            }
            seen.add(id);
        }
        return renames;
    };
    const npcRenames = _dedupIds(script.npcs);
    const locRenames = _dedupIds(script.locations);
    _dedupIds(script.world_properties);
    _dedupIds(script.persistent_states);
    _dedupIds(script.cyclic_events);
    _dedupIds(script.one_time_events);
    _dedupIds(script.random_items);
    _dedupIds(script.lorebook);
    const orgRenames = _dedupIds(script.organizations);
    _dedupIds(script.player_presets);
    if (script.story_tree?.trees) {
        _dedupIds(script.story_tree.trees);
        for (const tree of script.story_tree.trees) _dedupIds(tree.nodes);
    }

    // 同名 dedup 会重命名 ID，需修补关系两端的引用，否则保存后引用会丢失
    // 注意：dedup 只重命名"后出现的同名项"，被指向首项的边/关系不需要改
    const _fixRef = (obj, key, map) => { if (obj[key] && map[obj[key]]) obj[key] = map[obj[key]]; };
    const _fixArrayRefs = (arr, map) => {
        if (!Array.isArray(arr)) return;
        for (let i = 0; i < arr.length; i++) { if (map[arr[i]]) arr[i] = map[arr[i]]; }
    };
    if (Object.keys(npcRenames).length) {
        for (const r of (script.npc_relationships || [])) {
            _fixRef(r, 'from', npcRenames);
            _fixRef(r, 'to', npcRenames);
        }
        for (const npc of script.npcs) {
            _fixRef(npc, 'superior', npcRenames);
            _fixArrayRefs(npc.related_lore, npcRenames);
        }
        for (const org of script.organizations) {
            _fixRef(org, 'leader', npcRenames);
        }
    }
    if (Object.keys(locRenames).length) {
        for (const npc of script.npcs) {
            _fixRef(npc, 'default_location', locRenames);
        }
        _fixRef(script.player_character, 'initial_location', locRenames);
        for (const p of script.player_presets) {
            _fixRef(p, 'initial_location', locRenames);
        }
        for (const loc of script.locations) {
            _fixArrayRefs(loc.connections, locRenames);
        }
    }
    if (Object.keys(orgRenames).length) {
        for (const r of (script.org_relationships || [])) {
            _fixRef(r, 'a', orgRenames);
            _fixRef(r, 'b', orgRenames);
        }
        for (const org of script.organizations) {
            _fixRef(org, 'parent_org', orgRenames);
        }
        for (const npc of script.npcs) {
            if (Array.isArray(npc.organizations)) {
                for (const o of npc.organizations) {
                    _fixRef(o, 'org_id', orgRenames);
                }
            }
        }
    }

    // Auto-create player↔NPC relationships（在 dedup 之后，确保使用最终 ID）
    for (const npc of script.npcs) {
        script.player_character.relationships[npc.id] = {
            value: npc.attitude_toward_player,
            min: 0, max: 100,
            rule: `与${npc.name}的关系`,
        };
    }

    _preserveScriptVisibilityMetadata(script, _rawScriptOverlay);
    return script;
}

function toPinyin(str) {
    // Keep Chinese characters, letters, digits, underscores — strip everything else
    const cleaned = str.replace(/[^\w\u4e00-\u9fff\u3400-\u4dbf]/g, '_').substring(0, 30) || 'item_' + Date.now();
    // If result is all underscores (no valid chars), use original string
    if (/^_+$/.test(cleaned)) return str.substring(0, 30);
    return cleaned;
}

// --- Add element editors ---

function addPresetEditor(data, targetContainer, cachedAttrEntries) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-presets');
    const div = document.createElement('div');
    div.className = 'preset-editor item-card';

    // Gather current attribute keys for attribute overrides (use toPinyin key to match main dict)
    const attrEntries = cachedAttrEntries || [];
    if (!cachedAttrEntries) {
        document.querySelectorAll('#w-attributes .attr-editor').forEach(el => {
            const name = el.querySelector('.w-attr-name').value;
            if (name) attrEntries.push({ key: toPinyin(name), label: name });
        });
    }

    const attrOverrides = d.attributes || {};
    const attrHtml = attrEntries.map(({ key, label }) => {
        // Check both the internal key and display name for backwards compatibility
        const override = attrOverrides[key] ?? attrOverrides[label];
        const v = typeof override === 'object' ? (override.value ?? '') : (override ?? '');
        return `<div class="preset-attr"><span class="preset-attr-label">${escapeHtml(label)}</span><input type="number" class="w-preset-attr-val" data-attr="${escapeHtml(key)}" value="${v}" min="0" max="100" placeholder="默认"></div>`;
    }).join('');

    // Build opening choices HTML for preset
    const presetChoices = d.opening_choices || [];
    const choicesHtml = presetChoices.map((c, i) => `
        <div class="preset-choice-row">
            <button class="btn-remove-sm" onclick="this.parentElement.remove()" title="删除">&times;</button>
            <input type="text" class="w-preset-choice-text" placeholder="选项描述" value="${escapeHtml(c.text || '')}">
            <input type="text" class="w-preset-choice-result" placeholder="结果方向" value="${escapeHtml((c.result && c.result.description) || '')}">
        </div>
    `).join('');

    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()" title="删除">&times;</button>
        <div class="form-row">
            <div class="form-group"><label>ID:</label><input type="text" class="w-preset-id" value="${escapeHtml(d.id || '')}"></div>
            <div class="form-group"><label>姓名:</label><input type="text" class="w-preset-name" value="${escapeHtml(d.name || '')}"></div>
        </div>
        <div class="form-group"><label>简介:</label><input type="text" class="w-preset-bio" value="${escapeHtml(d.bio || '')}" placeholder="角色身份和背景"></div>
        <div class="form-group"><label>性格:</label><input type="text" class="w-preset-personality" value="${escapeHtml(d.personality || '')}" placeholder="性格特点（可选）"></div>
        <div class="form-row">
            <div class="form-group"><label>初始位置:</label><input type="text" class="w-preset-location" value="${escapeHtml(d.initial_location || '')}" placeholder="留空则用默认"></div>
            <div class="form-group"><label>长期目标:</label><input type="text" class="w-preset-goal" value="${escapeHtml(d.long_term_goal || '')}" placeholder="留空则用默认"></div>
        </div>
        <div class="form-group"><label>形象描述:</label><input type="text" class="w-preset-portrait" value="${escapeHtml(d.portrait_desc || '')}" placeholder="简短外貌描述（可选）"></div>
        <div class="form-group"><label>专属开局:</label><textarea class="w-preset-opening" rows="3" placeholder="留空则使用全局开局（AI会根据角色信息个性化改写）">${escapeHtml(d.opening_text || '')}</textarea></div>
        <details class="preset-choices-section">
            <summary>专属开局选项（留空则使用全局选项）</summary>
            <div class="preset-choices-rows">${choicesHtml}</div>
            <button type="button" class="btn-sm" onclick="addPresetChoiceRow(this.closest('.preset-choices-section').querySelector('.preset-choices-rows'))">+ 添加选项</button>
        </details>
        ${attrEntries.length > 0 ? '<div class="preset-attrs-area"><label style="font-size:0.78rem;color:var(--text-muted)">属性值覆盖（留空=使用默认值）:</label><div class="preset-attrs">' + attrHtml + '</div></div>' : ''}
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addPresetChoiceRow(container, data) {
    const c = data || {};
    const row = document.createElement('div');
    row.className = 'preset-choice-row';
    row.innerHTML = `
        <button class="btn-remove-sm" onclick="this.parentElement.remove()" title="删除">&times;</button>
        <input type="text" class="w-preset-choice-text" placeholder="选项描述" value="${escapeHtml(c.text || '')}">
        <input type="text" class="w-preset-choice-result" placeholder="结果方向" value="${escapeHtml((c.result && c.result.description) || '')}">
    `;
    container.appendChild(row);
}

function _addNpcOrgRow(container, data) {
    const d = data || {};
    const row = document.createElement('div');
    row.className = 'npc-org-row';
    row.style.cssText = 'display:flex;gap:4px;margin-top:2px';
    row.innerHTML = `
        <input type="text" class="w-npc-orgid" placeholder="组织ID" value="${escapeHtml(d.org_id||'')}">
        <input type="text" class="w-npc-orgrank" placeholder="层级(数字)" value="${escapeHtml(d.rank!=null?String(d.rank):'')}" style="width:80px">
        <input type="text" class="w-npc-orgrole" placeholder="角色(可选)" value="${escapeHtml(d.role||'')}">
        <button type="button" class="btn-remove-sm" onclick="this.parentElement.remove()" style="padding:0 6px">&times;</button>
    `;
    container.appendChild(row);
}

function addNpcEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-npcs');
    const div = document.createElement('div');
    div.className = 'npc-editor item-card';
    div.setAttribute('data-id', d.id || '');
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-npc-id" placeholder="ID(英文)" value="${escapeHtml(d.id||'')}"><input type="text" class="w-npc-name" placeholder="名字" value="${escapeHtml(d.name||'')}"></div>
        <input type="text" class="w-npc-bio" placeholder="简介" value="${escapeHtml(d.bio||'')}">
        <div class="row2"><input type="text" class="w-npc-personality" placeholder="性格" value="${escapeHtml(d.personality||'')}"><input type="text" class="w-npc-caps" placeholder="能力/作用" value="${escapeHtml(d.capabilities||'')}"></div>
        <div class="row2"><input type="text" class="w-npc-title" placeholder="头衔/职位" value="${escapeHtml(d.title||'')}"><input type="text" class="w-npc-superior" placeholder="上级NPC的ID" value="${escapeHtml(d.superior||'')}"><input type="text" class="w-npc-lore" placeholder="关联知识库词条(逗号分隔)" value="${escapeHtml((Array.isArray(d.related_lore)?d.related_lore.join(', '):d.related_lore)||'')}"></div>
        <div class="row2"><input type="text" class="w-npc-location" placeholder="默认地点ID" value="${escapeHtml(d.default_location||'')}"><input type="text" class="w-npc-portrait" placeholder="外貌简述" value="${escapeHtml(d.portrait_desc||'')}"></div>
        <div class="row2">
            <label>初始好感: <input type="number" class="w-npc-attitude" value="${d.attitude_toward_player||50}" min="0" max="100" style="width:60px"></label>
            <label>话痨度: <input type="number" class="w-npc-talkativeness" value="${d.talkativeness!=null?d.talkativeness:50}" min="0" max="100" style="width:55px" title="0=沉默 100=话多，影响NPC主动发言频率"></label>
            <label><input type="checkbox" class="w-npc-known" ${d.known!==false?'checked':''}> 初始已知</label>
            <label><input type="checkbox" class="w-npc-met" ${d.met!==false?'checked':''}> 初始熟识</label>
        </div>
        <div class="npc-orgs-section" style="margin-top:4px">
            <label style="font-size:0.85em;color:#888">所属组织 <button type="button" class="btn-add-npc-org" style="font-size:0.8em;padding:0 6px">+添加</button></label>
            <div class="npc-orgs-list"></div>
        </div>
        <details class="npc-schedule-section">
            <summary>日程表</summary>
            <div class="npc-schedule-rows"></div>
            <button type="button" class="btn-sm" onclick="addNpcScheduleRow(this.closest('.npc-schedule-section').querySelector('.npc-schedule-rows'))">+ 添加时段</button>
        </details>
        <details class="npc-goals-section">
            <summary>目标</summary>
            <div class="npc-goals-rows"></div>
            <button type="button" class="btn-sm btn-add-npc-goal">+ 添加目标</button>
        </details>
    `;
    // 填充多组织
    const orgsList = div.querySelector('.npc-orgs-list');
    const orgs = d.organizations || [];
    if (d.organization && !orgs.length) {
        _addNpcOrgRow(orgsList, { org_id: d.organization, rank: d.rank });
    } else {
        orgs.forEach(om => _addNpcOrgRow(orgsList, om));
    }
    div.querySelector('.btn-add-npc-org').addEventListener('click', () => {
        _addNpcOrgRow(div.querySelector('.npc-orgs-list'));
    });
    // S5: 填充已有日程
    const schedRows = div.querySelector('.npc-schedule-rows');
    const schedule = d.schedule || [];
    if (schedule.length > 0) {
        schedule.forEach(s => addNpcScheduleRow(schedRows, s));
    } else {
        addNpcScheduleRow(schedRows, { time_range: '06:00-12:00', location: '', activity: '' });
        addNpcScheduleRow(schedRows, { time_range: '12:00-18:00', location: '', activity: '' });
        addNpcScheduleRow(schedRows, { time_range: '18:00-06:00', location: '', activity: '' });
    }
    // 填充 NPC 目标
    const goalsRows = div.querySelector('.npc-goals-rows');
    const goals = d.goals || [];
    goals.forEach(g => _addNpcGoalRow(goalsRows, g));
    div.querySelector('.btn-add-npc-goal').addEventListener('click', () => {
        _addNpcGoalRow(div.querySelector('.npc-goals-rows'));
    });
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addNpcScheduleRow(container, data) {
    const d = data || {};
    const row = document.createElement('div');
    row.className = 'npc-schedule-row';
    row.innerHTML = `
        <input type="text" class="w-sched-time" placeholder="时段(如06:00-12:00)" value="${escapeHtml(d.time_range||d.time||'')}">
        <input type="text" class="w-sched-loc" placeholder="地点ID" value="${escapeHtml(d.location||'')}">
        <input type="text" class="w-sched-act" placeholder="活动" value="${escapeHtml(d.activity||'')}">
        <input type="text" class="w-sched-cond" placeholder="条件(可选)" value="${escapeHtml(d.condition||'')}" style="max-width:140px">
        <input type="number" class="w-sched-prio" placeholder="优先级" value="${d.priority||''}" style="width:50px" title="高优先级覆盖低优先级">
        <button type="button" class="btn-remove-sm" onclick="this.parentElement.remove()">&times;</button>
    `;
    container.appendChild(row);
}

function _addNpcGoalRow(container, data) {
    const d = data || {};
    const row = document.createElement('div');
    row.className = 'npc-goal-row';
    row.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;padding:4px;border:1px solid var(--border);border-radius:4px';
    row.innerHTML = `
        <input type="text" class="w-goal-id" placeholder="目标ID" value="${escapeHtml(d.id||'')}" style="width:100px">
        <input type="text" class="w-goal-desc" placeholder="目标描述" value="${escapeHtml(d.description||'')}" style="flex:1;min-width:120px">
        <select class="w-goal-type" style="width:80px"><option value="short_term" ${d.type==='short_term'?'selected':''}>短期</option><option value="long_term" ${d.type==='long_term'?'selected':''}>长期</option></select>
        <select class="w-goal-priority" style="width:70px"><option value="low" ${d.priority==='low'?'selected':''}>低</option><option value="medium" ${d.priority==='medium'||!d.priority?'selected':''}>中</option><option value="high" ${d.priority==='high'?'selected':''}>高</option></select>
        <input type="text" class="w-goal-condition" placeholder="完成条件(可选)" value="${escapeHtml(d.condition_met||'')}" style="min-width:140px;flex:1">
        <input type="text" class="w-goal-progress" placeholder="推进提示" value="${escapeHtml(d.progress_hint||'')}" style="min-width:100px;flex:1">
        <input type="text" class="w-goal-conflict" placeholder="与玩家冲突点(可选)" value="${escapeHtml(d.conflict_with_player||'')}" style="min-width:120px;flex:1">
        <button type="button" class="btn-remove-sm" onclick="this.parentElement.remove()">&times;</button>
    `;
    container.appendChild(row);
}

function _addInteractableRow(container, data) {
    const d = data || {};
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;padding:4px;border:1px solid var(--border);border-radius:4px';
    row.innerHTML = `
        <input type="text" class="w-ia-id" placeholder="ID(英文)" value="${escapeHtml(d.id||'')}" style="width:80px">
        <input type="text" class="w-ia-name" placeholder="名称" value="${escapeHtml(d.name||'')}" style="width:80px">
        <input type="text" class="w-ia-desc" placeholder="描述" value="${escapeHtml(d.description||'')}" style="flex:1;min-width:100px">
        <input type="text" class="w-ia-hint" placeholder="动作提示" value="${escapeHtml(d.action_hint||'')}" style="flex:1;min-width:100px">
        <input type="text" class="w-ia-cond" placeholder="条件(可选)" value="${escapeHtml(d.condition||'')}" style="width:120px">
        <label style="font-size:0.8em;display:flex;align-items:center;gap:2px"><input type="checkbox" class="w-ia-onetime" ${d.one_time?'checked':''}> 一次性</label>
        <input type="text" class="w-ia-reveals" placeholder="揭示地点ID(逗号分隔)" value="${escapeHtml((Array.isArray(d.reveals)?d.reveals.join(', '):d.reveals)||'')}" style="width:140px">
        <button type="button" class="btn-remove-sm" onclick="this.parentElement.remove()">&times;</button>
    `;
    container.appendChild(row);
}

function addLocationEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-locations');
    const div = document.createElement('div');
    div.className = 'loc-editor item-card';
    div.setAttribute('data-id', d.id || '');
    const iaData = Array.isArray(d.interactables) ? d.interactables : [];
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-loc-id" placeholder="ID" value="${escapeHtml(d.id||'')}"><input type="text" class="w-loc-name" placeholder="名称" value="${escapeHtml(d.name||'')}"></div>
        <input type="text" class="w-loc-desc" placeholder="描述" value="${escapeHtml(d.description||'')}">
        <div class="row2">
            <label><input type="checkbox" class="w-loc-visible" ${d.initially_visible!==false?'checked':''}> 初始可见</label>
            <input type="text" class="w-loc-connections" placeholder="连接地点ID(逗号分隔)" value="${escapeHtml((Array.isArray(d.connections)?d.connections.join(', '):d.connections)||'')}">
        </div>
        <input type="text" class="w-loc-travel-time" placeholder="抵达此地点耗时（ISO 8601，如 PT20M；仅格式正确时校验）" value="${escapeHtml(d.travel_time||'')}">
        <details style="margin-top:4px"><summary style="font-size:0.85em;cursor:pointer">可互动元素 (${iaData.length}) <button type="button" class="btn-add-ia" style="font-size:0.8em;padding:0 6px">+</button></summary>
        <div class="w-loc-interactables"></div></details>
    `;
    const iaContainer = div.querySelector('.w-loc-interactables');
    for (const ia of iaData) _addInteractableRow(iaContainer, ia);
    div.querySelector('.btn-add-ia').addEventListener('click', e => { e.preventDefault(); _addInteractableRow(iaContainer); });
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addOrganizationEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-organizations');
    const div = document.createElement('div');
    div.className = 'org-editor item-card';
    const hierarchy = Array.isArray(d.hierarchy) ? d.hierarchy : [];
    const hierarchyHtml = hierarchy.map(h =>
        `<div class="w-org-rank-row" style="display:flex;gap:4px;margin-top:2px"><input type="number" class="w-org-rank-num" placeholder="级别" value="${h.rank||''}" style="width:60px"><input type="text" class="w-org-rank-title" placeholder="职位名称" value="${escapeHtml(h.title||'')}"><button class="btn-remove-sm" onclick="this.parentElement.remove()" style="padding:0 6px">&times;</button></div>`
    ).join('');
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-org-id" placeholder="ID(英文)" value="${escapeHtml(d.id||'')}"><input type="text" class="w-org-name" placeholder="名称" value="${escapeHtml(d.name||'')}"></div>
        <div class="row2"><input type="text" class="w-org-type" placeholder="类型(帮派/公司/军队/国家/阵营等)" value="${escapeHtml(d.type||'')}"><input type="text" class="w-org-leader" placeholder="领导者NPC的ID" value="${escapeHtml(d.leader||'')}"></div>
        <div class="row2"><input type="text" class="w-org-parent" placeholder="父组织ID(可选)" value="${escapeHtml(d.parent_org||'')}"><input type="text" class="w-org-stance" placeholder="对主角立场(可选)" value="${escapeHtml(d.stance||'')}"><input type="text" class="w-org-aliases" placeholder="别名(逗号分隔)" value="${escapeHtml((Array.isArray(d.aliases)?d.aliases.join(', '):d.aliases)||'')}"></div>
        <label style="font-size:0.82rem;color:var(--text-muted)">初始声望: <input type="number" class="w-org-reputation" value="${d.initial_reputation ?? ''}" min="0" max="100" style="width:60px" placeholder="50"> <small>(0-100, 留空不启用)</small></label>
        <textarea class="w-org-desc" rows="2" placeholder="描述...">${escapeHtml(d.description||'')}</textarea>
        <div class="w-org-hierarchy-section" style="margin-top:4px">
            <label style="font-size:0.85em;color:#888">层级架构 <button type="button" class="btn-add-rank" style="font-size:0.8em;padding:0 6px">+添加层级</button></label>
            <div class="w-org-hierarchy-list">${hierarchyHtml}</div>
        </div>
        <details class="org-goals-section" style="margin-top:4px">
            <summary style="font-size:0.85em;color:#888;cursor:pointer">组织目标 <button type="button" class="btn-add-org-goal" style="font-size:0.8em;padding:0 6px">+添加目标</button></summary>
            <div class="org-goals-rows"></div>
        </details>
    `;
    div.querySelector('.btn-add-rank').addEventListener('click', () => {
        const list = div.querySelector('.w-org-hierarchy-list');
        const rows = list.querySelectorAll('.w-org-rank-row');
        const nextRank = rows.length + 1;
        const row = document.createElement('div');
        row.className = 'w-org-rank-row';
        row.style.cssText = 'display:flex;gap:4px;margin-top:2px';
        row.innerHTML = `<input type="number" class="w-org-rank-num" placeholder="级别" value="${nextRank}" style="width:60px"><input type="text" class="w-org-rank-title" placeholder="职位名称"><button class="btn-remove-sm" onclick="this.parentElement.remove()" style="padding:0 6px">&times;</button>`;
        list.appendChild(row);
    });
    const goalsRows = div.querySelector('.org-goals-rows');
    const orgGoals = Array.isArray(d.goals) ? d.goals : [];
    orgGoals.forEach(g => _addNpcGoalRow(goalsRows, g));
    div.querySelector('.btn-add-org-goal').addEventListener('click', (e) => {
        e.preventDefault();
        _addNpcGoalRow(div.querySelector('.org-goals-rows'));
    });
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addOrgRelEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-org-rels');
    if (!container) return null;
    const div = document.createElement('div');
    div.className = 'org-rel-editor item-card';
    div._orgRelSource = d;
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row3">
            <input type="text" class="w-orgrel-a" placeholder="组织/势力A的ID" value="${escapeHtml(d.a||'')}">
            <select class="w-orgrel-type">
                <option value="同盟" ${d.type==='同盟'||d.type==='ally'?'selected':''}>同盟</option>
                <option value="敌对" ${d.type==='敌对'||d.type==='rival'?'selected':''}>敌对</option>
                <option value="竞争" ${d.type==='竞争'||d.type==='competitive'?'selected':''}>竞争</option>
                <option value="从属" ${d.type==='从属'||d.type==='subordinate'?'selected':''}>从属</option>
                <option value="中立" ${d.type==='中立'||d.type==='neutral'||!d.type?'selected':''}>中立</option>
                <option value="合作" ${d.type==='合作'||d.type==='cooperative'?'selected':''}>合作</option>
            </select>
            <input type="text" class="w-orgrel-b" placeholder="组织/势力B的ID" value="${escapeHtml(d.b||'')}">
        </div>
        <input type="text" class="w-orgrel-desc" placeholder="关系描述" value="${escapeHtml(d.description||'')}">
        <label>玩家可见性
            <select class="w-orgrel-visibility">
                <option value="public" ${!d.visibility||d.visibility==='public'?'selected':''}>公开</option>
                <option value="world" ${d.visibility==='world'?'selected':''}>仅世界背景</option>
                <option value="hidden" ${d.visibility==='hidden'?'selected':''}>发现后可见</option>
            </select>
        </label>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addWorldPropEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-world-props');
    const div = document.createElement('div');
    div.className = 'prop-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <input type="hidden" class="w-prop-id" value="${escapeHtml(d.id||'')}">
        <input type="text" class="w-prop-name" placeholder="属性名" value="${escapeHtml(d.name||'')}">
        <input type="text" class="w-prop-value" placeholder="属性值" value="${escapeHtml(d.value||'')}">
        <input type="text" class="w-prop-rule" placeholder="说明" value="${escapeHtml(d.rule||'')}">
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addPersistentStateEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-persistent-states');
    const div = document.createElement('div');
    div.className = 'ps-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <input type="hidden" class="w-ps-id" value="${escapeHtml(d.id||'')}">
        <div class="row2"><input type="text" class="w-ps-name" placeholder="显示名称(中文简称)" value="${escapeHtml(d.name||'')}"><label><input type="checkbox" class="w-ps-active" ${d.initially_active!==false?'checked':''}> 初始激活</label></div>
        <input type="text" class="w-ps-desc" placeholder="状态描述（AI会参考这个调整叙事）" value="${escapeHtml(d.description||'')}">
        <label>过期时间: <input type="datetime-local" class="w-ps-expiry" value="${d.expires_at?d.expires_at.substring(0,16):''}"> (留空=永久)</label>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addCyclicEventEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-cyclic-events');
    const div = document.createElement('div');
    div.className = 'ce-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-ce-id" placeholder="事件ID(英文)" value="${escapeHtml(d.id||'')}"><input type="text" class="w-ce-name" placeholder="事件名称" value="${escapeHtml(d.name||'')}"></div>
        <input type="text" class="w-ce-desc" placeholder="事件描述" value="${escapeHtml(d.description||'')}">
        <label>玩家可见性 <select class="w-ce-visibility"><option value="public" ${!d.visibility||d.visibility==='public'?'selected':''}>公开</option><option value="world" ${d.visibility==='world'?'selected':''}>仅世界推演</option></select></label>
        <div class="row3">
            <label>每 <input type="number" class="w-ce-freq-val" value="${d.frequency_value||1}" min="1" style="width:50px"></label>
            <select class="w-ce-freq-unit"><option value="day" ${d.frequency_unit==='day'?'selected':''}>天</option><option value="week" ${d.frequency_unit==='week'?'selected':''}>周</option><option value="month" ${d.frequency_unit==='month'?'selected':''}>月</option></select>
            <label>首次: <input type="datetime-local" class="w-ce-first" value="${d.first_trigger?d.first_trigger.substring(0,16):''}"></label>
        </div>
        <div class="row2"><input type="text" class="w-ce-condition" placeholder="触发条件(可选，如 player.health>30)" value="${escapeHtml(d.condition||'')}"><label>过期: <input type="datetime-local" class="w-ce-expiry" value="${d.expires_at?d.expires_at.substring(0,16):''}"></label></div>
        <input type="text" class="w-ce-fire-events" placeholder="触发事件(逗号分隔，可触发剧情树节点)" value="${escapeHtml((d.fire_events||[]).join(', '))}">
        <input type="text" class="w-ce-activate-events" placeholder="激活事件(逗号分隔，被其他事件触发解锁)" value="${escapeHtml((d.activate_events||[]).join(', '))}">
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addOnetimeEventEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-onetime-events');
    const div = document.createElement('div');
    div.className = 'ote-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-ote-id" placeholder="事件ID(英文)" value="${escapeHtml(d.id||'')}"><input type="text" class="w-ote-name" placeholder="事件名称" value="${escapeHtml(d.name||'')}"></div>
        <input type="text" class="w-ote-desc" placeholder="事件描述" value="${escapeHtml(d.description||'')}">
        <label>玩家可见性 <select class="w-ote-visibility"><option value="public" ${!d.visibility||d.visibility==='public'?'selected':''}>公开</option><option value="world" ${d.visibility==='world'?'selected':''}>仅世界推演</option></select></label>
        <label>触发时间: <input type="datetime-local" class="w-ote-time" value="${d.trigger_time?d.trigger_time.substring(0,16):''}"></label>
        <input type="text" class="w-ote-condition" placeholder="触发条件(可选，如 player.health>30)" value="${escapeHtml(d.condition||'')}">
        <input type="text" class="w-ote-fire-events" placeholder="触发事件(逗号分隔，可触发剧情树节点)" value="${escapeHtml((d.fire_events||[]).join(', '))}">
        <input type="text" class="w-ote-activate-events" placeholder="激活事件(逗号分隔，被其他事件触发解锁)" value="${escapeHtml((d.activate_events||[]).join(', '))}">
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addToneRuleEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-tone-rules');
    const div = document.createElement('div');
    div.className = 'tone-rule-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <input type="hidden" class="w-tone-id" value="${escapeHtml(d.id||'')}">
        <input type="text" class="w-tone-name" placeholder="规则名称（如：战争氛围）" value="${escapeHtml(d.name||'')}">
        <input type="text" class="w-tone-condition" placeholder="触发条件（如 tension >= 80）" value="${escapeHtml(d.condition||'')}">
        <input type="text" class="w-tone-tone" placeholder="基调描述（如：紧张、压迫、军事化）" value="${escapeHtml(d.tone||'')}">
        <input type="text" class="w-tone-style" placeholder="叙事风格（如：短句、急促、多使用听觉描写）" value="${escapeHtml(d.narrative_style||'')}">
        <label>优先级: <input type="number" class="w-tone-priority" value="${d.priority||0}" min="0" max="100" style="width:60px"></label>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addQuestTemplateEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-quest-templates');
    const div = document.createElement('div');
    div.className = 'quest-tpl-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-qt-id" placeholder="ID(英文)" value="${escapeHtml(d.id||'')}"><input type="text" class="w-qt-name" placeholder="支线名称" value="${escapeHtml(d.name||'')}"></div>
        <input type="text" class="w-qt-desc" placeholder="描述（何时触发）" value="${escapeHtml(d.description||'')}">
        <input type="text" class="w-qt-condition" placeholder="触发条件（如 faction_reputation.thieves_guild.value < 30）" value="${escapeHtml(d.condition||'')}">
        <input type="text" class="w-qt-trigger" placeholder="引入提示（AI 叙事中的触发线索）" value="${escapeHtml(d.trigger_hint||'')}">
        <div class="row2"><input type="text" class="w-qt-reward" placeholder="奖励提示" value="${escapeHtml(d.reward_hint||'')}"><label>冷却回合: <input type="number" class="w-qt-cooldown" value="${d.cooldown_turns||10}" min="1" style="width:60px"></label></div>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addRegexScriptEditor(data) {
    const d = data || {};
    const container = document.getElementById('w-regex-scripts');
    const div = document.createElement('div');
    div.className = 'regex-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-rx-name" placeholder="规则名称" value="${escapeHtml(d.name||'')}">
        <select class="w-rx-placement"><option value="ai_output" ${d.placement==='ai_output'||!d.placement?'selected':''}>AI输出</option><option value="user_input" ${d.placement==='user_input'?'selected':''}>用户输入</option></select></div>
        <input type="text" class="w-rx-find" placeholder="查找正则 (如 \\(OOC:.*?\\))" value="${escapeHtml(d.find||'')}">
        <input type="text" class="w-rx-replace" placeholder="替换为 (留空=删除)" value="${escapeHtml(d.replace||'')}">
        <label style="font-size:0.8rem"><input type="checkbox" class="w-rx-enabled" ${d.enabled!==false?'checked':''}> 启用</label>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addTriggerEditor(data) {
    const d = data || {};
    const container = document.getElementById('w-triggers');
    const div = document.createElement('div');
    div.className = 'trigger-editor item-card';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2">
            <select class="w-trig-event"><option value="on_start" ${d.event==='on_start'?'selected':''}>游戏开始</option><option value="before_generation" ${d.event==='before_generation'?'selected':''}>生成前</option><option value="after_ai" ${d.event==='after_ai'||!d.event?'selected':''}>AI回复后</option></select>
            <select class="w-trig-action"><option value="set_var" ${d.action==='set_var'||!d.action?'selected':''}>修改变量</option><option value="inject_prompt" ${d.action==='inject_prompt'?'selected':''}>注入Prompt</option><option value="activate_lore" ${d.action==='activate_lore'?'selected':''}>激活词条</option><option value="deactivate_lore" ${d.action==='deactivate_lore'?'selected':''}>禁用词条</option><option value="notify" ${d.action==='notify'?'selected':''}>通知玩家</option></select>
        </div>
        <input type="text" class="w-trig-condition" placeholder="条件(可选，如 quest_count >= 3)" value="${escapeHtml(d.condition||'')}">
        <input type="text" class="w-trig-params" placeholder="参数JSON(如 {&quot;var_id&quot;:&quot;x&quot;,&quot;op&quot;:&quot;inc&quot;,&quot;value&quot;:1})" value="${escapeHtml(d._params_str || (d.params ? JSON.stringify(d.params) : ''))}">
        <label style="font-size:0.8rem"><input type="checkbox" class="w-trig-enabled" ${d.enabled!==false?'checked':''}> 启用</label>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function addVariableEditor(data) {
    const d = data || {};
    const container = document.getElementById('w-variables');
    const div = document.createElement('div');
    div.className = 'var-editor item-card';
    const defVal = d.default !== undefined ? d.default : '';
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-var-id" placeholder="变量ID (英文)" value="${escapeHtml(d.id||'')}"><input type="text" class="w-var-name" placeholder="显示名称" value="${escapeHtml(d.name||'')}"></div>
        <div class="row3">
            <select class="w-var-type" onchange="toggleVarMinMax(this)"><option value="number" ${d.type==='number'||!d.type?'selected':''}>数字</option><option value="string" ${d.type==='string'?'selected':''}>字符串</option><option value="bool" ${d.type==='bool'?'selected':''}>布尔</option></select>
            <input type="text" class="w-var-default" placeholder="默认值" value="${escapeHtml(String(defVal))}">
            <input type="number" class="w-var-min" placeholder="最小" value="${d.min!==undefined?d.min:''}" style="width:60px;${d.type&&d.type!=='number'?'display:none':''}">
            <input type="number" class="w-var-max" placeholder="最大" value="${d.max!==undefined?d.max:''}" style="width:60px;${d.type&&d.type!=='number'?'display:none':''}">
        </div>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function toggleVarMinMax(sel) {
    const row = sel.closest('.row3');
    const minEl = row.querySelector('.w-var-min');
    const maxEl = row.querySelector('.w-var-max');
    const show = sel.value === 'number';
    minEl.style.display = show ? '' : 'none';
    maxEl.style.display = show ? '' : 'none';
}

function addRandomItemEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-random-items');
    const div = document.createElement('div');
    div.className = 'ri-editor item-card';

    // Build ranges HTML
    const ranges = d.ranges || [];
    let rangesHtml = '';
    for (const r of ranges) {
        rangesHtml += _buildRangeRowHtml(r);
    }

    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2"><input type="text" class="w-ri-id" placeholder="ID" value="${escapeHtml(d.id||'')}"><input type="text" class="w-ri-desc" placeholder="描述" value="${escapeHtml(d.description||'')}"></div>
        <div class="row2"><input type="text" class="w-ri-trigger" placeholder="触发时机" value="${escapeHtml(d.trigger||'永远生效')}">
        <select class="w-ri-type"><option value="always" ${d.trigger_type==='always'?'selected':''}>永远</option><option value="conditional" ${d.trigger_type==='conditional'?'selected':''}>条件</option><option value="event_linked" ${d.trigger_type==='event_linked'?'selected':''}>关联事件</option></select></div>
        <div class="row3 ri-row">
            <label>骰子: <input type="number" class="w-ri-dice-count" value="${d.dice?.count||1}" min="1" max="999" style="width:70px">d</label>
            <input type="number" class="w-ri-dice-faces" value="${d.dice?.faces||100}" min="2" max="9999" style="width:80px">
            <label>修正: <input type="number" class="w-ri-dice-mod" value="${d.dice?.modifier||0}" min="-9999" max="9999" style="width:80px"></label>
        </div>
        <div class="row3 ri-row">
            <label>持续: <input type="number" class="w-ri-duration" value="${d.duration_turns||0}" min="0" max="9999" style="width:70px" title="结果持续回合数(0=仅当回合)"> 回合</label>
            <label>冷却: <input type="number" class="w-ri-cooldown" value="${d.cooldown_turns||0}" min="0" max="9999" style="width:70px" title="冷却回合数(0=无冷却)"> 回合</label>
        </div>
        <div class="row3 ri-row">
            <label>保留最高: <input type="number" class="w-ri-keep-high" value="${d.dice?.keep_highest||0}" min="0" max="99" style="width:60px" title="保留最高N个骰子(0=全部)"></label>
            <label>保留最低: <input type="number" class="w-ri-keep-low" value="${d.dice?.keep_lowest||0}" min="0" max="99" style="width:60px" title="保留最低N个骰子(0=全部)"></label>
        </div>
        <input type="text" class="w-ri-linked-event" placeholder="关联事件ID(可选)" value="${escapeHtml(d.linked_event_id||'')}">
        <input type="text" class="w-ri-condition" placeholder="触发条件(可选，如 state.is_raining)" value="${escapeHtml(d.condition||'')}">
        <div class="ri-ranges-area">
            <div class="ri-ranges-header">
                <label style="font-size:0.78rem;color:var(--text-muted)">结果区间（骰子值对应的结果描述）:</label>
                <button type="button" class="btn-add-inline" onclick="addRangeRow(this)">+ 区间</button>
            </div>
            <div class="w-ri-ranges">${rangesHtml}</div>
        </div>
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function _buildRangeRowHtml(r) {
    const d = r || {};
    const scJson = d.state_changes ? JSON.stringify(d.state_changes) : '';
    return `<div class="ri-range-row" data-sc="${escapeHtml(scJson)}">
        <input type="number" class="w-range-min" placeholder="最小" value="${d.min||1}" min="0" max="99999" style="width:60px">
        <span>~</span>
        <input type="number" class="w-range-max" placeholder="最大" value="${d.max||100}" min="0" max="99999" style="width:60px">
        <input type="text" class="w-range-label" placeholder="结果简述（如: 晴天/暴雨）" value="${escapeHtml(d.label||'')}" style="flex:1">
        <input type="text" class="w-range-desc" placeholder="详细描述(可选)" value="${escapeHtml(d.description||'')}" style="flex:1">
        <button class="btn-remove-inline" onclick="this.parentElement.remove()" title="删除">&times;</button>
    </div>`;
}

function addRangeRow(btn) {
    const rangesContainer = btn.closest('.ri-ranges-area').querySelector('.w-ri-ranges');
    const row = document.createElement('div');
    row.innerHTML = _buildRangeRowHtml({});
    // Insert the inner div (not wrapper)
    rangesContainer.appendChild(row.firstElementChild);
}

// --- Lorebook editor ---

function addLorebookEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-lorebook');
    const div = document.createElement('div');
    div.className = 'lore-editor item-card';
    const keys = Array.isArray(d.keys) ? d.keys.join(', ') : (d.keys || '');
    const secKeys = Array.isArray(d.secondary_keys) ? d.secondary_keys.join(', ') : (d.secondary_keys || '');
    // Handle content: if object, stringify it; if string, use as-is
    let contentText = d.content || '';
    if (typeof contentText === 'object') {
        contentText = JSON.stringify(contentText, null, 2);
    }
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2">
            <input type="text" class="w-lore-id" placeholder="ID(英文)" value="${escapeHtml(d.id||'')}">
            <input type="text" class="w-lore-comment" placeholder="标签/名称" value="${escapeHtml(d.comment||'')}">
        </div>
        <div class="lore-keys-hint">关键词(逗号分隔): 当这些词出现在玩家输入或近期剧情中时激活</div>
        <input type="text" class="w-lore-keys" placeholder="关键词1, 关键词2, 别名..." value="${escapeHtml(keys)}">
        <input type="text" class="w-lore-secondary-keys" placeholder="二级关键词(可选，需同时命中)" value="${escapeHtml(secKeys)}">
        <textarea class="w-lore-content" rows="3" placeholder="该词条被激活时注入的背景知识...">${escapeHtml(contentText)}</textarea>
        <div class="row3">
            <select class="w-lore-position" onchange="toggleLoreDepth(this)">
                <option value="after_world" ${d.position==='after_world'||!d.position?'selected':''}>世界背景后</option>
                <option value="at_end" ${d.position==='at_end'?'selected':''}>提示词末尾</option>
                <option value="at_depth" ${d.position==='at_depth'?'selected':''}>指定深度插入</option>
            </select>
            <span class="w-lore-depth-group" style="display:${d.position==='at_depth'?'inline':'none'}">
                <label>深度:<input type="number" class="w-lore-depth" value="${d.depth||4}" min="0" max="50" style="width:40px"></label>
                <select class="w-lore-role">
                    <option value="system" ${d.role==='system'||!d.role?'selected':''}>system</option>
                    <option value="user" ${d.role==='user'?'selected':''}>user</option>
                    <option value="assistant" ${d.role==='assistant'?'selected':''}>assistant</option>
                </select>
            </span>
            <label>优先级: <input type="number" class="w-lore-priority" value="${d.priority||100}" min="1" max="999" style="width:50px"></label>
            <label><input type="checkbox" class="w-lore-constant" ${d.constant?'checked':''}> 常驻</label>
        </div>
        <div class="row3" style="margin-top:0.3rem">
            <label>触发率:<input type="number" class="w-lore-probability" value="${d.probability!=null?d.probability:100}" min="0" max="100" style="width:45px">%</label>
            <label>粘滞:<input type="number" class="w-lore-sticky" value="${d.sticky||0}" min="0" max="99" style="width:40px" title="激活后持续N回合"></label>
            <label>冷却:<input type="number" class="w-lore-cooldown" value="${d.cooldown||0}" min="0" max="99" style="width:40px" title="粘滞结束后N回合不可激活"></label>
            <input type="text" class="w-lore-group" placeholder="互斥组(可选)" value="${escapeHtml(d.group||'')}" style="width:90px">
            <label>组权重:<input type="number" class="w-lore-group-weight" value="${d.group_weight||100}" min="1" max="999" style="width:45px"></label>
        </div>
        <input type="text" class="w-lore-related" placeholder="关联词条ID(逗号分隔，如 lore_a, lore_b)" value="${escapeHtml(Array.isArray(d.related_entries)?d.related_entries.join(', '):(d.related_entries||''))}">
    `;
    container.appendChild(div);
    _makeCardCollapsible(div, !!data);
    return div;
}

function toggleLoreDepth(sel) {
    const group = sel.closest('.lore-editor').querySelector('.w-lore-depth-group');
    if (group) group.style.display = sel.value === 'at_depth' ? 'inline' : 'none';
}

function addStoryTreeEditor(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-story-trees');
    const div = document.createElement('div');
    div.className = 'story-tree-editor item-card';
    const icons = ['scroll','crown','sword','shield','star','skull','eye','fire','moon','tree','castle','gem'];
    const iconOptions = icons.map(i => `<option value="${i}" ${d.icon===i?'selected':''}>${i}</option>`).join('');
    div.innerHTML = `
        <button class="btn-remove" onclick="this.parentElement.remove()">&times;</button>
        <div class="row2">
            <input type="text" class="w-st-id" placeholder="剧情线ID(英文)" value="${escapeHtml(d.id||'')}">
            <input type="text" class="w-st-name" placeholder="剧情线名称" value="${escapeHtml(d.name||'')}">
        </div>
        <div class="row2">
            <select class="w-st-icon">${iconOptions}</select>
            <input type="text" class="w-st-desc" placeholder="描述（可选）" value="${escapeHtml(d.description||'')}">
        </div>
        <div class="story-nodes-container"></div>
        <button class="btn-secondary" onclick="addStoryNodeEditor(null, this.closest('.story-tree-editor').querySelector('.story-nodes-container'))">+ 添加节点</button>
    `;
    container.appendChild(div);
    const nodesContainer = div.querySelector('.story-nodes-container');
    for (const node of (d.nodes || [])) {
        addStoryNodeEditor(node, nodesContainer);
    }
    _makeCardCollapsible(div, !!data);
    return div;
}

function addStoryNodeEditor(data, targetContainer) {
    const d = data || {};
    const div = document.createElement('div');
    div.className = 'story-node-editor';
    div.style.cssText = 'border:1px solid var(--border);border-radius:6px;padding:0.5rem;margin:0.4rem 0;background:var(--bg-darker,#1a1a2e)';
    const typeOptions = ['auto','choice','quest','timed','trigger','periodic'].map(t =>
        `<option value="${t}" ${d.type===t?'selected':''}>${t}</option>`
    ).join('');
    const requires = Array.isArray(d.requires) ? d.requires.join(', ') : (d.requires || '');
    const unlock = Array.isArray(d.on_complete_unlock) ? d.on_complete_unlock.join(', ') : (d.on_complete_unlock || '');
    const activateEvents = Array.isArray(d.activate_events) ? d.activate_events.join(', ') : (d.activate_events || '');
    const eventVal = Array.isArray(d.event) ? d.event.join(', ') : (d.event || '');
    const effectsStr = d.effects ? JSON.stringify(d.effects, null, 2) : '';
    const choicesStr = d.choices ? JSON.stringify(d.choices, null, 2) : '';
    const isTrigger = d.type === 'trigger';
    const isPeriodic = d.type === 'periodic';
    div.innerHTML = `
        <button class="btn-remove-inline" onclick="this.parentElement.remove()" style="float:right">&times;</button>
        <div class="row2">
            <input type="text" class="w-stn-id" placeholder="节点ID" value="${escapeHtml(d.id||'')}">
            <input type="text" class="w-stn-name" placeholder="节点名称" value="${escapeHtml(d.name||'')}">
            <select class="w-stn-type">${typeOptions}</select>
        </div>
        <input type="text" class="w-stn-desc" placeholder="节点描述" value="${escapeHtml(d.description||'')}" style="width:100%;margin:0.3rem 0">
        <div class="row2">
            <input type="text" class="w-stn-requires" placeholder="前置节点ID(逗号分隔)" value="${escapeHtml(requires)}">
            <input type="text" class="w-stn-condition" placeholder="触发条件(如 var >= 20)" value="${escapeHtml(d.condition||'')}">
        </div>
        <div class="row2">
            <input type="text" class="w-stn-unlock" placeholder="完成后解锁节点ID(逗号分隔)" value="${escapeHtml(unlock)}">
            <input type="number" class="w-stn-duration" placeholder="持续回合(timed)" value="${d.duration_turns||''}" min="0" style="width:100px">
        </div>
        <div class="row2 w-stn-trigger-fields" style="display:${isTrigger?'flex':'none'}">
            <input type="text" class="w-stn-event" placeholder="生命周期事件(on_start/before_generation/after_ai，逗号分隔)" value="${escapeHtml(eventVal)}">
        </div>
        <div class="row2 w-stn-periodic-fields" style="display:${isPeriodic?'flex':'none'}">
            <input type="number" class="w-stn-cooldown" placeholder="冷却回合" value="${d.cooldown||''}" min="0" style="width:100px">
            <input type="number" class="w-stn-weight" placeholder="权重(默认10)" value="${d.weight||''}" min="1" style="width:100px">
            <label style="display:flex;align-items:center;gap:0.3rem;font-size:0.8rem"><input type="checkbox" class="w-stn-repeatable" ${d.repeatable!==false?'checked':''}>可重复</label>
        </div>
        <input type="text" class="w-stn-activate-events" placeholder="激活事件(被哪些事件名触发解锁，逗号分隔)" value="${escapeHtml(activateEvents)}" style="width:100%;margin:0.2rem 0">
        <textarea class="w-stn-effects" rows="2" placeholder="效果JSON(可选): {&quot;set_var&quot;:[...],&quot;activate_lore&quot;:[...],&quot;notify&quot;:&quot;...&quot;,&quot;fire_events&quot;:[...],&quot;unlock_nodes&quot;:[...]}">${escapeHtml(effectsStr)}</textarea>
        <textarea class="w-stn-choices" rows="2" placeholder="选项JSON(choice类型): [{&quot;id&quot;:&quot;...&quot;,&quot;label&quot;:&quot;...&quot;,&quot;unlock&quot;:[...]}]" style="display:${d.type==='choice'||choicesStr?'block':'none'}">${escapeHtml(choicesStr)}</textarea>
    `;
    const typeSelect = div.querySelector('.w-stn-type');
    typeSelect.addEventListener('change', () => {
        div.querySelector('.w-stn-choices').style.display = typeSelect.value === 'choice' ? 'block' : 'none';
        div.querySelector('.w-stn-trigger-fields').style.display = typeSelect.value === 'trigger' ? 'flex' : 'none';
        div.querySelector('.w-stn-periodic-fields').style.display = typeSelect.value === 'periodic' ? 'flex' : 'none';
    });
    targetContainer.appendChild(div);
    return div;
}

function addAttrRow(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-attributes');
    const div = document.createElement('div');
    div.className = 'attr-editor';
    div.innerHTML = `
        <input type="text" placeholder="属性名" class="w-attr-name" value="${escapeHtml(d.name||'')}">
        <input type="number" class="w-attr-val" value="${d.value||50}" min="0" max="100">
        <input type="text" placeholder="说明" class="w-attr-rule" value="${escapeHtml(d.rule||'')}">
        <button class="btn-remove-inline" onclick="this.parentElement.remove()">&times;</button>
    `;
    container.appendChild(div);
    return div;
}

function addInventoryRow(data, targetContainer) {
    const d = data || {};
    const container = targetContainer || document.getElementById('w-inventory');
    const div = document.createElement('div');
    div.className = 'attr-editor';
    const hasEffect = d.use_effect ? JSON.stringify(d.use_effect, null, 2) : '';
    div.innerHTML = `
        <input type="text" placeholder="物品名称" class="w-inv-item" value="${escapeHtml(d.item||'')}">
        <input type="number" class="w-inv-qty" value="${d.quantity||1}" min="1" max="9999" style="width:70px">
        <input type="text" placeholder="描述（可选）" class="w-inv-desc" value="${escapeHtml(d.description||'')}" style="width:140px">
        <button class="btn-remove-inline" onclick="this.parentElement.remove()">&times;</button>
        <details style="width:100%;margin-top:2px"><summary style="font-size:0.75rem;color:var(--text-muted);cursor:pointer">使用效果</summary>
        <textarea class="w-inv-effect" rows="3" placeholder='{"consumable":true,"state_changes":[{"target":"player.attributes.health","change":20}],"success_message":"恢复了生命"}'
            style="width:100%;font-size:0.75rem;font-family:monospace">${escapeHtml(hasEffect)}</textarea></details>
    `;
    container.appendChild(div);
    return div;
}

// --- Save wizard script ---

async function saveWizardScript() {
    const script = await buildScriptFromWizard();
    if (!script.script_id || !script.script_name) {
        alert('请填写剧本ID和名称');
        return;
    }

    try {
        const result = await API.post('/api/scripts', {
            id: script.script_id,
            name: script.script_name,
            content: script,
        });
        if (result.errors && result.errors.length) {
            // S2: 结构化验证错误 — 显示并高亮
            const msgs = result.errors.map(e => typeof e === 'string' ? e : e.msg);
            alert('已保存，但有警告:\n' + msgs.join('\n'));
            _highlightValidationErrors(result.errors);
        } else {
            alert('剧本已保存！');
        }
        editingScriptId = script.script_id;
    } catch (e) {
        alert('保存失败: ' + e.message);
    }
}

function _highlightValidationErrors(errors) {
    // S2: 根据 target 定位并高亮对应编辑器区域
    // 清除旧高亮
    document.querySelectorAll('.validation-error-highlight').forEach(el => el.classList.remove('validation-error-highlight'));
    const targetToTab = {
        'basic': 'basic', 'player_character': 'characters', 'npcs': 'characters',
        'locations': 'world', 'npc_relationships': 'characters',
        'opening': 'characters', 'organizations': 'organizations',
        'org_relationships': 'organizations',
    };
    for (const err of errors) {
        if (typeof err === 'string') continue;
        const target = err.target || '';
        // Map target to wizard tab and element
        let tab = targetToTab[target];
        if (!tab) {
            if (target.startsWith('npc:')) tab = 'characters';
            else if (target.startsWith('dice:')) tab = 'dice';
            else if (target.startsWith('preset:')) tab = 'characters';
            else if (target.startsWith('opening:')) tab = 'characters';
            else if (target.startsWith('org:')) tab = 'organizations';
            else continue;
        }
        // Navigate to the tab
        const tabStep = document.getElementById(`step-${tab}`);
        if (!tabStep) continue;
        // Try to find specific editor card by NPC/item id
        const parts = target.split(':');
        if (parts.length === 2) {
            const id = parts[1];
            const card = tabStep.querySelector(`[data-id="${id}"]`);
            if (card) {
                card.classList.add('validation-error-highlight');
                continue;
            }
        }
        // Fallback: highlight the tab step itself
        tabStep.classList.add('validation-error-highlight');
    }
    // Auto-remove highlight after 10s
    setTimeout(() => {
        document.querySelectorAll('.validation-error-highlight').forEach(el => el.classList.remove('validation-error-highlight'));
    }, 10000);
}

function toggleChoiceConditional(selectEl) {
    const fields = selectEl.closest('.choice-editor').querySelector('.choice-conditional-fields');
    if (fields) fields.style.display = selectEl.value === 'conditional' ? 'flex' : 'none';
}

async function previewWizardJson() {
    await wizardStep('json');
}

function syncJsonToWizard() {
    try {
        const script = JSON.parse(document.getElementById('script-json-editor').value);
        _rawScriptOverlay = script;  // 保留未知字段
        loadScriptIntoWizard(script);
        wizardStep('basic');
    } catch (e) {
        alert('JSON格式错误: ' + e.message);
    }
}

// --- Load existing script into wizard ---

async function editScriptVisual(scriptId) {
    try {
        const data = await API.get(`/api/scripts/${scriptId}`);
        const script = data.content;
        editingScriptId = scriptId;
        _rawScriptOverlay = script;  // 保留未知字段
        document.getElementById('scripts-list-view').style.display = 'none';
        document.getElementById('script-wizard').style.display = 'block';
        document.getElementById('wizard-title').textContent = `编辑: ${data.name || scriptId}`;
        _initAllPaginators();
        loadScriptIntoWizard(script);
        const panel = document.getElementById('copilot-content');
        if (panel) panel._hasGenerateResult = false;
        _updateGuide();
        wizardStep('basic');
    } catch (e) {
        alert('加载失败: ' + e.message);
    }
}

function loadScriptIntoWizard(script) {
    _isLoadingScript = true;
    clearWizard();

    // 保存完整数据供延迟加载使用
    _pendingScript = script;
    _loadedTabs.clear();

    // ── basic tab（立即加载，因为是默认展示的） ──
    document.getElementById('w-script-id').value = script.script_id || '';
    document.getElementById('w-script-name').value = script.script_name || '';
    const startTime = (script.start_time || '').substring(0, 16);
    document.getElementById('w-start-time').value = startTime;
    document.getElementById('w-background').value = script.world_background || '';
    document.getElementById('w-dice-enabled').checked = script.settings?.dice_check?.default_enabled !== false;
    document.getElementById('w-check-rule').value = script.settings?.check_rule || 'default';
    document.getElementById('w-fixed-opening').checked = script.settings?.fixed_opening?.default_enabled !== false;
    document.getElementById('w-lore-token-budget').value = script.settings?.lorebook_token_budget || 0;
    document.getElementById('w-lore-max-recursion').value = script.settings?.lorebook_max_recursion ?? 2;
    document.getElementById('w-summary-word-threshold').value = script.settings?.summary_word_threshold || 3000;
    document.getElementById('w-reasoning-lookback').value = script.settings?.reasoning_lookback || 2;
    document.getElementById('w-vector-summarize').checked = !!script.settings?.vector_summarize;
    _loadedTabs.add('basic');

    // ── characters tab 也立即加载（opening 在里面，且是第二常用 tab） ──
    // 先 add 再 load，与 _ensureTabLoaded 一致，确保 _NpcRelGraph.refresh()
    // 中 _loadedTabs.has('characters') 判定一致
    _loadedTabs.add('characters');
    _loadTabCharacters(script);
    _refreshPaginatorsForTab('characters');

    _isLoadingScript = false;
}

// ── 批量加载辅助：使用 DocumentFragment 避免逐条 reflow ──

function _batchLoadContainer(containerId, items, addFn) {
    if (!items.length) return;
    const container = document.getElementById(containerId);
    if (!container) return;
    const frag = document.createDocumentFragment();
    for (const item of items) addFn(item, frag);
    container.appendChild(frag);
}

// 异步分片版本：每帧渲染 CHUNK_SIZE 个条目，避免阻塞主线程
const _BATCH_CHUNK_SIZE = 15;

function _batchLoadContainerAsync(containerId, items, addFn) {
    return new Promise(resolve => {
        if (!items.length) { resolve(); return; }
        const container = document.getElementById(containerId);
        if (!container) { resolve(); return; }
        let idx = 0;
        const tick = () => {
            const frag = document.createDocumentFragment();
            const end = Math.min(idx + _BATCH_CHUNK_SIZE, items.length);
            while (idx < end) addFn(items[idx++], frag);
            container.appendChild(frag);
            if (idx < items.length) requestAnimationFrame(tick);
            else resolve();
        };
        requestAnimationFrame(tick);
    });
}

// ── 按 tab 延迟加载 ──

const _ALL_CONTENT_TABS = ['basic', 'characters', 'world', 'organizations', 'events', 'dice', 'lorebook', 'story_tree'];

function _maybeClearPendingScript() {
    if (!_pendingScript) return;
    for (const t of _ALL_CONTENT_TABS) {
        if (!_loadedTabs.has(t)) return;
    }
    _pendingScript = null;
}

// 跟踪正在异步加载的 tab 的 Promise，用于 _ensureAllTabsLoaded 等待
const _tabLoadPromises = {};

function _ensureTabLoaded(step) {
    if (_loadedTabs.has(step) || !_pendingScript) return;
    _loadedTabs.add(step);
    _isLoadingScript = true;
    const script = _pendingScript;
    let result;
    try {
        switch (step) {
            case 'characters': _loadTabCharacters(script); break;
            case 'world':      _loadTabWorld(script); break;
            case 'organizations':
                result = _loadTabOrganizations(script);
                break;
            case 'events':     _loadTabEvents(script); break;
            case 'dice':       _loadTabDice(script); break;
            case 'lorebook':   _loadTabLorebook(script); break;
            case 'story_tree': _loadTabStoryTree(script); break;
        }
    } catch (e) {
        _loadedTabs.delete(step);
        _isLoadingScript = false;
        console.error(`Tab "${step}" 加载失败:`, e);
        if (typeof showNotification === 'function') showNotification(`"${step}" 加载失败: ${e.message}`, 'error');
        return;
    }
    if (result && typeof result.then === 'function') {
        _tabLoadPromises[step] = result.then(() => {
            _isLoadingScript = false;
            _refreshPaginatorsForTab(step);
            delete _tabLoadPromises[step];
            _maybeClearPendingScript();
        }).catch(e => {
            _loadedTabs.delete(step);
            _isLoadingScript = false;
            delete _tabLoadPromises[step];
            console.error(`Tab "${step}" 异步加载失败:`, e);
        });
    } else {
        _isLoadingScript = false;
        _refreshPaginatorsForTab(step);
        _maybeClearPendingScript();
    }
}

function _loadTabCharacters(script) {
    // Opening
    document.getElementById('w-opening-text').value = script.opening?.text || '';
    const choices = script.opening?.choices || [];
    const choiceEditors = document.querySelectorAll('#w-opening-choices .choice-editor');
    choices.forEach((c, i) => {
        if (choiceEditors[i]) {
            choiceEditors[i].querySelector('.w-choice-text').value = c.text || '';
            choiceEditors[i].querySelector('.w-choice-result').value = c.result?.description || '';
            choiceEditors[i].querySelector('.w-choice-type').value = c.result?.type || 'deterministic';
            const condFields = choiceEditors[i].querySelector('.choice-conditional-fields');
            if (condFields && c.result?.type === 'conditional') {
                condFields.style.display = 'flex';
                const thEl = choiceEditors[i].querySelector('.w-choice-threshold');
                if (thEl) thEl.value = c.result.threshold || 50;
                const succEl = choiceEditors[i].querySelector('.w-choice-success');
                if (succEl) succEl.value = c.result.success?.description || '';
                const failEl = choiceEditors[i].querySelector('.w-choice-failure');
                if (failEl) failEl.value = c.result.failure?.description || '';
            }
        }
    });
    if (choices.length > choiceEditors.length && typeof showNotification === 'function') {
        showNotification(`开局选项超出可编辑数量（${choices.length}/${choiceEditors.length}），多余 ${choices.length - choiceEditors.length} 项已忽略`, 'warning');
    }
    // Player character
    const pc = script.player_character || {};
    document.getElementById('w-pc-bio').value = pc.bio || '';
    document.getElementById('w-pc-personality').value = pc.personality || '';
    document.getElementById('w-pc-portrait').value = pc.portrait_desc || '';
    document.getElementById('w-pc-location').value = pc.initial_location || '';
    document.getElementById('w-pc-goal').value = pc.long_term_goal || '';
    // Attributes
    const attrItems = Object.entries(pc.attributes || {}).map(([key, attr]) => ({
        name: (typeof attr === 'object' ? (attr.display_name || attr.name) : null) || key,
        value: typeof attr === 'object' ? attr.value : attr,
        rule: typeof attr === 'object' ? (attr.rule || '') : '',
    }));
    document.getElementById('w-attributes').innerHTML = '';
    _batchLoadContainer('w-attributes', attrItems, addAttrRow);
    // Initial inventory
    document.getElementById('w-inventory').innerHTML = '';
    _batchLoadContainer('w-inventory', pc.initial_inventory || [], addInventoryRow);
    // Allow custom character
    document.getElementById('w-allow-custom-char').checked = script.allow_custom_character !== false;
    // Player presets — 预缓存属性列表避免每个 preset 重新查询 DOM
    document.getElementById('w-presets').innerHTML = '';
    const cachedAttrEntries = [];
    document.querySelectorAll('#w-attributes .attr-editor').forEach(el => {
        const name = el.querySelector('.w-attr-name').value;
        if (name) cachedAttrEntries.push({ key: toPinyin(name), label: name });
    });
    _batchLoadContainer('w-presets', script.player_presets || [], (data, tc) => addPresetEditor(data, tc, cachedAttrEntries));
    // NPCs
    _batchLoadContainer('w-npcs', script.npcs || [], addNpcEditor);
    // NPC relationships
    _NpcRelGraph.init('npc-rel-graph');
    _NpcRelGraph.loadEdges(script.npc_relationships || []);
    _NpcRelGraph.refresh();
}

function _loadTabWorld(script) {
    _batchLoadContainer('w-locations', script.locations || [], addLocationEditor);
    _batchLoadContainer('w-world-props', script.world_properties || [], addWorldPropEditor);
    _batchLoadContainer('w-persistent-states', script.persistent_states || [], addPersistentStateEditor);
}

async function _loadTabOrganizations(script) {
    // 旧剧本兼容：将 factions 合并入 organizations（不修改原始数据）
    const orgs = [...(script.organizations || [])];
    if (script.factions && Array.isArray(script.factions)) {
        for (const fac of script.factions) {
            orgs.push({ ...fac, type: fac.type || fac.stance || '势力' });
        }
    }
    // NPC 的 faction/organization → organizations 兼容已在 addNpcEditor 内处理，无需修改原始数据
    const stepEl = document.getElementById('step-organizations');
    let hint;
    const total = orgs.length + (script.org_relationships || []).length;
    if (total > _BATCH_CHUNK_SIZE && stepEl) {
        hint = document.createElement('div');
        hint.className = 'tab-loading-hint';
        hint.textContent = `正在加载 ${total} 个条目...`;
        stepEl.prepend(hint);
    }
    await _batchLoadContainerAsync('w-organizations', orgs, addOrganizationEditor);
    await _batchLoadContainerAsync('w-org-rels', script.org_relationships || [], addOrgRelEditor);
    if (hint) hint.remove();
}

function _loadTabEvents(script) {
    _batchLoadContainer('w-cyclic-events', script.cyclic_events || [], addCyclicEventEditor);
    _batchLoadContainer('w-onetime-events', script.one_time_events || [], addOnetimeEventEditor);
    _batchLoadContainer('w-tone-rules', script.tone_rules || [], addToneRuleEditor);
    _batchLoadContainer('w-quest-templates', script.quest_templates || [], addQuestTemplateEditor);
}

function _loadTabDice(script) {
    _batchLoadContainer('w-random-items', script.random_items || [], addRandomItemEditor);
    _batchLoadContainer('w-variables', script.variables || [], addVariableEditor);
    _batchLoadContainer('w-triggers', script.triggers || [], addTriggerEditor);
    _batchLoadContainer('w-regex-scripts', script.regex_scripts || [], addRegexScriptEditor);
}

function _loadTabLorebook(script) {
    _batchLoadContainer('w-lorebook', script.lorebook || [], addLorebookEditor);
}

function _loadTabStoryTree(script) {
    const trees = script.story_tree?.trees || [];
    _batchLoadContainer('w-story-trees', trees, addStoryTreeEditor);
    if (typeof stLoadFromScript === 'function') stLoadFromScript(script);
}

const _TAB_PAGINATOR_IDS = {
    characters: ['w-npcs', 'w-presets'],
    world: ['w-locations', 'w-world-props', 'w-persistent-states'],
    organizations: ['w-organizations', 'w-org-rels'],
    events: ['w-cyclic-events', 'w-onetime-events', 'w-tone-rules', 'w-quest-templates'],
    dice: ['w-random-items'],
    lorebook: ['w-lorebook'],
    story_tree: ['w-story-trees'],
};

function _refreshPaginatorsForTab(tab) {
    const ids = _TAB_PAGINATOR_IDS[tab];
    if (!ids) return;
    for (const id of ids) {
        const p = _paginators[id];
        if (p) { p.page = 0; p.refresh(); }
    }
}

// AI full script generate
function showAIFullGenerate() {
    document.getElementById('ai-generate-modal').style.display = 'flex';
    document.getElementById('ai-full-prompt').value = '';
    document.getElementById('ai-gen-status').innerHTML = '';
}

async function doAIFullGenerate() {
    const prompt = document.getElementById('ai-full-prompt').value;
    if (!prompt) { alert('请描述你想要的游戏世界'); return; }

    const btn = document.getElementById('ai-gen-btn');
    btn.disabled = true;
    btn.textContent = '正在生成...';
    const statusEl = document.getElementById('ai-gen-status');
    statusEl.innerHTML = '<div class="loading"></div> 正在生成基本框架（名称/背景/设定）...';

    try {
        const result = await API.post('/api/scripts/ai-generate', { field: 'full', prompt });

        if (result.parsed) {
            _rawScriptOverlay = result.parsed;  // 保留 AI 生成的未知字段
            loadScriptIntoWizard(result.parsed);
            document.getElementById('ai-generate-modal').style.display = 'none';
            document.getElementById('scripts-list-view').style.display = 'none';
            document.getElementById('script-wizard').style.display = 'block';
            document.getElementById('wizard-title').textContent = '新建剧本 (AI生成)';
            wizardStep('basic');
            if (typeof showNotification === 'function') {
                showNotification('基本框架已就绪！请先到「角色」tab生成主角和NPC，再补充其他内容', 'success');
            }
        } else {
            statusEl.innerHTML = `<div style="color:var(--warning)">AI返回的格式可能有问题</div><textarea rows="10" style="width:100%;margin-top:0.5rem">${escapeHtml(result.raw)}</textarea>`;
        }
    } catch (e) {
        statusEl.innerHTML = `<span style="color:var(--accent)">生成失败: ${e.message}</span>`;
    } finally {
        btn.disabled = false;
        btn.textContent = '开始生成';
    }
}


// --- Per-tab AI bulk generation ---

const _TAB_LABELS = {
    characters: '角色设计',
    world: '世界构建',
    organizations: '组织势力',
    events: '事件系统',
    dice: '随机项',
    lorebook: '知识库',
    story_tree: '剧情树',
};

// Tabs with multiple sections — generated sequentially to avoid timeout
const _TAB_SECTIONS = {
    characters: [
        { key: 'player', label: '玩家角色与预设' },
        { key: 'npcs',   label: 'NPC与关系' },
    ],
    world: [
        { key: 'locations',        label: '地点' },
        { key: 'properties_states', label: '世界属性与持续状态' },
    ],
    organizations: [
        { key: 'organizations', label: '组织/势力' },
    ],
    events: [
        { key: 'cyclic',   label: '周期事件' },
        { key: 'one_time', label: '一次性事件' },
    ],
    story_tree: [
        { key: 'trees', label: '剧情树' },
    ],
};

async function aiGenerateTab(tab) {
    const script = await buildScriptFromWizard();

    if (!script.script_name && !script.world_background) {
        alert('请先填写基本信息（剧本名称/世界背景）');
        return;
    }

    const label = _TAB_LABELS[tab] || tab;
    const btn = document.querySelector(`#step-${tab} .btn-ai-tab-gen`);
    // S1: 读取用户补充需求
    const hintInput = document.querySelector(`#step-${tab} .ai-hint-input`);
    const userHint = hintInput ? hintInput.value.trim() : '';

    // Show overlay mask on wizard-main
    const wizardMain = document.querySelector('.wizard-main');
    const overlay = document.createElement('div');
    overlay.className = 'wizard-gen-overlay';
    overlay.innerHTML = '<div class="overlay-spinner"></div><div class="overlay-text">AI 正在生成...</div>';
    if (wizardMain) wizardMain.appendChild(overlay);
    if (btn) {
        btn.disabled = true;
        btn._origText = btn.textContent;
        btn.textContent = '生成中...';
    }

    // Snapshot for undo (once, before any changes)
    const snapshot = _snapshotTab(tab);
    const sections = _TAB_SECTIONS[tab];

    try {
        if (sections && sections.length > 1) {
            // Multi-section: call AI for each section sequentially
            const combinedMerge = { added: [], updated: [], skipped: [] };
            for (let i = 0; i < sections.length; i++) {
                const sec = sections[i];
                if (btn) btn.textContent = `生成中(${i + 1}/${sections.length}) ${sec.label}...`;
                const overlayText = overlay.querySelector('.overlay-text');
                if (overlayText) overlayText.textContent = `正在生成「${sec.label}」(${i + 1}/${sections.length})...`;
                if (typeof showNotification === 'function') {
                    showNotification(`正在生成「${sec.label}」(${i + 1}/${sections.length})...`, 'info');
                }
                // Rebuild script each iteration so previous section results are included
                const currentScript = await buildScriptFromWizard();
                const result = await API.post('/api/scripts/ai-generate/tab', {
                    tab, script: currentScript, section: sec.key, user_hint: userHint,
                });
                // Skip if section content is already sufficient
                if (result.complete) continue;
                if (result.generated && Object.keys(result.generated).length > 0) {
                    const mergeInfo = _smartMergeTab(tab, result.generated);
                    combinedMerge.added.push(...mergeInfo.added);
                    combinedMerge.updated.push(...mergeInfo.updated);
                    combinedMerge.skipped.push(...mergeInfo.skipped);
                }
            }
            if (combinedMerge.added.length === 0 && combinedMerge.updated.length === 0) {
                if (typeof showNotification === 'function') {
                    showNotification(`「${label}」内容已完整，无需补充`, 'info');
                }
            } else {
                _switchCopilotMode('result');
                _renderMergeSummary(tab, combinedMerge, snapshot);
                if (typeof showNotification === 'function') {
                    showNotification(`「${label}」已完成: 新增${combinedMerge.added.length}项, 更新${combinedMerge.updated.length}项`, 'success');
                }
            }
        } else {
            // Single-section: one call
            if (typeof showNotification === 'function') {
                showNotification(`正在AI生成「${label}」内容...`, 'info');
            }
            const payload = { tab, script, user_hint: userHint };
            if (sections && sections.length === 1) payload.section = sections[0].key;
            const result = await API.post('/api/scripts/ai-generate/tab', payload);
            if (result.complete) {
                if (typeof showNotification === 'function') {
                    showNotification(`「${label}」内容已完整，无需补充`, 'info');
                }
                return;
            }
            const generated = result.generated;
            if (!generated || Object.keys(generated).length === 0) {
                if (typeof showNotification === 'function') {
                    showNotification(`「${label}」内容已完整，无需补充`, 'info');
                }
                return;
            }
            const mergeInfo = _smartMergeTab(tab, generated);
            _switchCopilotMode('result');
            _renderMergeSummary(tab, mergeInfo, snapshot);
            if (typeof showNotification === 'function') {
                showNotification(`「${label}」已完成: 新增${mergeInfo.added.length}项, 更新${mergeInfo.updated.length}项`, 'success');
            }
        }
    } catch (e) {
        if (typeof showNotification === 'function') {
            showNotification(`「${label}」生成失败: ${e.message}`, 'error');
        } else {
            alert(`生成失败: ${e.message}`);
        }
    } finally {
        // Remove overlay
        if (overlay.parentNode) overlay.remove();
        if (btn) {
            btn.disabled = false;
            btn.textContent = btn._origText || `AI生成${label}`;
        }
        _updateGuide();
    }
}

async function aiSupplementTab(tab) {
    const script = await buildScriptFromWizard();
    if (!script.script_name && !script.world_background) {
        alert('请先填写基本信息（剧本名称/世界背景）');
        return;
    }

    // --- Phase 1: entity-level field polish tasks (characters/world/organizations) ---
    const tabEntityTypes = Object.entries(_FILL_FIELD_SCHEMA)
        .filter(([_, v]) => v.tab === tab).map(([k]) => k);
    const _COL_MAP = { npc:'npcs', preset:'player_presets', location:'locations', organization:'organizations', one_time_event:'one_time_events', cyclic_event:'cyclic_events', random_item:'random_items', variable:'variables', trigger:'triggers', regex_script:'regex_scripts' };

    const entityTasks = [];
    for (const type of tabEntityTypes) {
        const allFields = _FILL_FIELD_SCHEMA[type].fields;
        if (type === 'player') {
            entityTasks.push({ type, entityId: 'main', fields: allFields, name: '主角' });
        } else {
            for (const entity of (script[_COL_MAP[type]] || [])) {
                if (!entity.id) continue;
                entityTasks.push({ type, entityId: entity.id, fields: allFields, name: entity.name || entity.id });
            }
        }
    }

    // --- Phase 2: tab-section polish tasks (covers fields not in entity schemas) ---
    const _EXTRA_POLISH = {
        characters: [{ key: 'player', label: '玩家角色与预设' }],
        world: [{ key: 'properties_states', label: '世界属性与持续状态' }],
    };
    const _FULL_POLISH = ['events', 'dice', 'lorebook', 'story_tree'];

    let sectionTasks = [];
    if (_FULL_POLISH.includes(tab)) {
        const sections = _TAB_SECTIONS[tab];
        sectionTasks = sections
            ? sections.map(s => ({ key: s.key, label: s.label }))
            : [{ key: null, label: _TAB_LABELS[tab] }];
    } else if (_EXTRA_POLISH[tab]) {
        sectionTasks = _EXTRA_POLISH[tab];
    }

    const needRelations = tab === 'characters' || tab === 'organizations';

    if (!entityTasks.length && !sectionTasks.length) {
        showNotification('当前 tab 没有可优化的内容', 'info');
        return;
    }

    const btn = document.querySelector(`#step-${tab} .btn-ai-tab-supp`);
    const hintInput = document.querySelector(`#step-${tab} .ai-hint-input`);
    const userHint = hintInput ? hintInput.value.trim() : '';
    const snapshot = _snapshotTab(tab);

    const wizardMain = document.querySelector('.wizard-main');
    const overlay = document.createElement('div');
    overlay.className = 'wizard-gen-overlay';
    overlay.innerHTML = '<div class="overlay-spinner"></div><div class="overlay-text">AI 正在优化...</div>';
    if (wizardMain) wizardMain.appendChild(overlay);
    if (btn) { btn.disabled = true; btn._origText = btn.textContent; btn.textContent = '优化中...'; }

    let doneCount = 0;
    const entityTypeCount = new Set(entityTasks.map(t => t.type)).size;
    const totalSteps = entityTypeCount + sectionTasks.length + (needRelations ? 1 : 0);
    const _progress = (label) => {
        doneCount++;
        if (btn) btn.textContent = `优化中 (${doneCount}/${totalSteps})...`;
        const ot = overlay.querySelector('.overlay-text');
        if (ot) ot.textContent = label || `正在优化 (${doneCount}/${totalSteps})...`;
    };

    try {
        const wrapped = {};

        // --- Run entity-level field polish (batch by type) ---
        if (entityTasks.length) {
            const byType = {};
            for (const t of entityTasks) {
                (byType[t.type] ??= []).push(t);
            }
            const batchPromises = Object.entries(byType).map(([type, tasks]) =>
                API.post('/api/scripts/ai-generate/fields-batch', {
                    script, entity_type: type,
                    entities: tasks.map(t => ({ entity_id: t.entityId, fields: t.fields })),
                    user_hint: userHint, mode: 'polish', batch_size: 5,
                }).then(r => ({ type, tasks, results: r.results || {} }))
                  .finally(() => _progress(`正在优化${_FILL_FIELD_SCHEMA[type]?.label || type}...`))
            );
            const settled = await Promise.allSettled(batchPromises);
            let failCount = 0;
            for (const s of settled) {
                if (s.status === 'fulfilled') {
                    const { type, tasks, results } = s.value;
                    for (const t of tasks) {
                        const generated = results[t.entityId];
                        if (!generated || !Object.keys(generated).length) continue;
                        const item = { id: t.entityId, ...generated };
                        if (type === 'npc') (wrapped.npcs = wrapped.npcs || []).push(item);
                        else if (type === 'preset') (wrapped.player_presets = wrapped.player_presets || []).push(item);
                        else if (type === 'player') wrapped.player_character = generated;
                        else if (type === 'location') {
                            if (item.connections && typeof item.connections === 'string') item.connections = item.connections.split(',').map(s => s.trim());
                            (wrapped.locations = wrapped.locations || []).push(item);
                        } else if (type === 'organization') {
                            if (item.aliases && typeof item.aliases === 'string') item.aliases = item.aliases.split(',').map(s => s.trim());
                            (wrapped.organizations = wrapped.organizations || []).push(item);
                        } else if (_COL_MAP[type]) {
                            const col = _COL_MAP[type];
                            (wrapped[col] = wrapped[col] || []).push(item);
                        }
                    }
                } else { failCount++; console.error('批量实体优化失败:', s.reason); }
            }
            if (failCount) showNotification(`${failCount} 类实体优化失败`, 'warning');
        }

        // --- Run section-level tab polish (sequential, each rebuilds script for context) ---
        for (const sec of sectionTasks) {
            _progress(`正在优化「${sec.label}」...`);
            try {
                const currentScript = await buildScriptFromWizard();
                const payload = { tab, script: currentScript, user_hint: userHint, mode: 'polish' };
                if (sec.key) payload.section = sec.key;
                const result = await API.post('/api/scripts/ai-generate/tab', payload);
                if (result.generated && Object.keys(result.generated).length) {
                    for (const [k, v] of Object.entries(result.generated)) {
                        if (Array.isArray(v)) {
                            wrapped[k] = (wrapped[k] || []).concat(v);
                        } else if (typeof v === 'object' && v !== null) {
                            wrapped[k] = Object.assign(wrapped[k] || {}, v);
                        }
                    }
                }
            } catch (secErr) {
                console.error(`Section polish 失败 (${sec.key}):`, secErr);
                showNotification(`「${sec.label}」优化失败`, 'warning');
            }
        }

        // --- Relations polish (characters/organizations) ---
        if (needRelations) {
            _progress('正在优化关系网络...');
            try {
                const currentScript = await buildScriptFromWizard();
                const relResult = await API.post('/api/scripts/ai-generate/optimize-relations', {
                    script: currentScript, user_hint: userHint,
                    scope: tab === 'characters' ? 'npc' : 'org',
                });
                if (relResult.generated) {
                    Object.assign(wrapped, relResult.generated);
                }
            } catch (relErr) {
                console.error('关系优化失败:', relErr);
                showNotification('关系优化部分失败', 'warning');
            }
        }

        if (!Object.keys(wrapped).length) {
            showNotification('AI未生成任何优化内容', 'info');
            return;
        }

        const mergeInfo = _smartMergeTab(tab, wrapped);
        _switchCopilotMode('result');
        _renderMergeSummary(tab, mergeInfo, snapshot);
        const total = mergeInfo.added.length + mergeInfo.updated.length;
        showNotification(`优化完成: ${total}项已更新`, 'success');
    } catch (e) {
        showNotification(`优化失败: ${e.message}`, 'error');
    } finally {
        if (overlay.parentNode) overlay.remove();
        if (btn) { btn.disabled = false; btn.textContent = btn._origText || 'AI一键优化'; }
        _updateGuide();
    }
}

function _appendSupplementaryLorebook(data, mergeInfo) {
    if (data.supplementary_lorebook && Array.isArray(data.supplementary_lorebook)) {
        // 确保 lorebook tab 已加载，否则 _pendingScript 中的现有词条尚未渲染到 DOM，
        // 会导致 supplementary 项被误判为"新增"，与稍后懒加载的现有词条形成重复
        _ensureTabLoaded('lorebook');
        const container = document.getElementById('w-lorebook');
        // O(N) 预构建 id→element Map，避免内层 querySelectorAll 形成 O(N²)
        const idToEl = new Map();
        container.querySelectorAll('.lore-editor').forEach(el => {
            const idEl = el.querySelector('.w-lore-id');
            if (idEl && idEl.value) idToEl.set(idEl.value, el);
        });
        for (const entry of data.supplementary_lorebook) {
            if (!entry.keys && !entry.content) continue;
            const existing = entry.id ? idToEl.get(entry.id) : null;
            if (existing) {
                const el = existing;
                const oldContent = el.querySelector('.w-lore-content')?.value || '';
                const oldKeys = el.querySelector('.w-lore-keys')?.value || '';
                const oldComment = el.querySelector('.w-lore-comment')?.value || '';
                const relEl2 = el.querySelector('.w-lore-related');
                const oldRelated2 = relEl2 ? relEl2.value : '';
                if (entry.content) el.querySelector('.w-lore-content').value = entry.content;
                if (entry.keys) el.querySelector('.w-lore-keys').value = Array.isArray(entry.keys) ? entry.keys.join(', ') : entry.keys;
                if (entry.comment) el.querySelector('.w-lore-comment').value = entry.comment;
                if (entry.related_entries && relEl2) relEl2.value = Array.isArray(entry.related_entries) ? entry.related_entries.join(', ') : entry.related_entries;
                mergeInfo.updated.push({
                    label: `知识库: ${entry.comment || entry.id}`,
                    undoFn: () => {
                        el.querySelector('.w-lore-content').value = oldContent;
                        el.querySelector('.w-lore-keys').value = oldKeys;
                        el.querySelector('.w-lore-comment').value = oldComment;
                        if (relEl2) relEl2.value = oldRelated2;
                    },
                });
            } else {
                const newEl = addLorebookEditor(entry);
                if (newEl && entry.id) idToEl.set(entry.id, newEl);
                mergeInfo.added.push({ label: `知识库: ${entry.comment || entry.id || '新词条'}`, el: newEl });
            }
        }
    }
}

/** Snapshot all editor containers for a tab so we can undo later. */
function _snapshotTab(tab) {
    const ids = _tabContainerIds(tab);
    const snap = {};
    for (const id of ids) {
        const el = document.getElementById(id);
        if (el) snap[id] = el.innerHTML;
    }
    // Also snapshot simple value fields
    const fields = _tabValueFieldIds(tab);
    const vals = {};
    for (const fid of fields) {
        const el = document.getElementById(fid);
        if (el) vals[fid] = el.value;
    }
    snap._values = vals;
    // Snapshot lorebook container if this tab generates supplementary lorebook
    if (['characters', 'world', 'organizations', 'events'].includes(tab)) {
        const lbEl = document.getElementById('w-lorebook');
        if (lbEl) snap['w-lorebook'] = lbEl.innerHTML;
    }
    // Snapshot NPC relationship graph edges (stored in JS, not DOM)
    if (tab === 'characters') {
        snap._npcRelEdges = JSON.parse(JSON.stringify(_NpcRelGraph.edges));
    }
    return snap;
}

function _tabContainerIds(tab) {
    switch(tab) {
        case 'characters': return ['w-attributes', 'w-inventory', 'w-npcs', 'w-presets', 'w-opening-choices'];
        case 'world': return ['w-locations', 'w-world-props', 'w-persistent-states'];
        case 'organizations': return ['w-organizations', 'w-org-rels'];
        case 'events': return ['w-cyclic-events', 'w-onetime-events', 'w-tone-rules'];
        case 'dice': return ['w-random-items', 'w-variables', 'w-triggers', 'w-regex-scripts'];
        case 'lorebook': return ['w-lorebook'];
        case 'story_tree': return ['w-story-trees'];
        default: return [];
    }
}

function _tabValueFieldIds(tab) {
    switch(tab) {
        case 'characters': return ['w-pc-bio', 'w-pc-personality', 'w-pc-portrait', 'w-pc-goal', 'w-pc-location', 'w-opening-text'];
        default: return [];
    }
}

function _buildEditorIdMap(container, idClass) {
    const map = new Map();
    for (const el of container.querySelectorAll('.item-card')) {
        const input = el.querySelector('.' + idClass);
        if (input && input.value) map.set(input.value, el);
    }
    return map;
}

/** Update an existing editor card's fields from data. */
function _updateEditorFields(el, data, fieldMap) {
    let changed = false;
    for (const [dataKey, cssClass] of Object.entries(fieldMap)) {
        if (data[dataKey] !== undefined && data[dataKey] !== null) {
            const input = el.querySelector('.' + cssClass);
            if (input) {
                const v = data[dataKey];
                const newVal = Array.isArray(v) ? v.join(', ') : String(v);
                if (input.value !== newVal) {
                    input.value = newVal;
                    changed = true;
                }
            }
        }
    }
    // 直接设置 .value 不会触发 input 事件，需手动触发以刷新折叠摘要
    if (changed) {
        const body = el.querySelector('.card-body');
        if (body) body.dispatchEvent(new Event('input', { bubbles: true }));
        // 高亮提示已更新
        el.classList.add('card-updated');
        setTimeout(() => el.classList.remove('card-updated'), 4000);
    }
}

/** Capture current values of an editor card's fields, keyed by dataKey. */
function _captureEditorFields(el, fieldMap) {
    const snap = {};
    for (const [dataKey, cssClass] of Object.entries(fieldMap)) {
        const input = el.querySelector('.' + cssClass);
        if (input) snap[dataKey] = input.value;
    }
    return snap;
}

/** Restore previously captured editor field values. */
function _restoreEditorFields(el, snap, fieldMap) {
    for (const [dataKey, cssClass] of Object.entries(fieldMap)) {
        if (snap[dataKey] === undefined) continue;
        const input = el.querySelector('.' + cssClass);
        if (input) input.value = snap[dataKey];
    }
    // 刷新折叠摘要
    const body = el.querySelector('.card-body');
    if (body) body.dispatchEvent(new Event('input', { bubbles: true }));
    el.classList.remove('card-updated');
}

/**
 * Smart merge: compare generated data with existing editor content by ID.
 * - Existing ID → update fields in-place
 * - New ID → append via addXxxEditor
 * Returns { added: object[], updated: object[], skipped: string[] }
 * Each added item: { label, el }   — undo = el.remove()
 * Each updated item: { label, undoFn }  — undo = undoFn()
 */
function _smartMergeTab(tab, data) {
    const info = { added: [], updated: [], skipped: [] };

    switch(tab) {
        case 'characters': {
            // Player character — always overwrite non-empty fields
            const pc = data.player_character;
            if (pc) {
                const bioEl = document.getElementById('w-pc-bio');
                const goalEl = document.getElementById('w-pc-goal');
                const locEl = document.getElementById('w-pc-location');
                const persEl = document.getElementById('w-pc-personality');
                const portEl = document.getElementById('w-pc-portrait');
                const oldBio = bioEl.value, oldGoal = goalEl.value, oldLoc = locEl.value;
                const oldPers = persEl.value, oldPort = portEl.value;
                if (pc.bio) bioEl.value = pc.bio;
                if (pc.long_term_goal || pc.goal) goalEl.value = pc.long_term_goal || pc.goal;
                if (pc.initial_location || pc.location) locEl.value = pc.initial_location || pc.location;
                if (pc.personality) persEl.value = pc.personality;
                if (pc.portrait_desc) portEl.value = pc.portrait_desc;
                info.updated.push({
                    label: '主角设定',
                    undoFn: () => { bioEl.value = oldBio; goalEl.value = oldGoal; locEl.value = oldLoc; persEl.value = oldPers; portEl.value = oldPort; },
                });

                if (pc.attributes && typeof pc.attributes === 'object') {
                    const container = document.getElementById('w-attributes');
                    const oldHtml = container.innerHTML;
                    const attrNameMap = new Map();
                    container.querySelectorAll('.attr-editor').forEach(row => {
                        const n = row.querySelector('.w-attr-name')?.value;
                        if (n) attrNameMap.set(n, row);
                    });
                    for (const [key, attr] of Object.entries(pc.attributes)) {
                        const rule = typeof attr === 'object' ? (attr.rule || '') : '';
                        const val = typeof attr === 'object' ? (attr.value ?? 50) : 50;
                        const name = (typeof attr === 'object' ? (attr.display_name || attr.name) : null) || key;
                        const existing = attrNameMap.get(name) || null;
                        if (existing) {
                            const oldVal = existing.querySelector('.w-attr-val').value;
                            const oldRule = existing.querySelector('.w-attr-rule').value;
                            existing.querySelector('.w-attr-val').value = val;
                            if (rule) existing.querySelector('.w-attr-rule').value = rule;
                            info.updated.push({
                                label: `属性: ${name}`,
                                undoFn: () => { existing.querySelector('.w-attr-val').value = oldVal; existing.querySelector('.w-attr-rule').value = oldRule; },
                            });
                        } else {
                            const newEl = addAttrRow({ name, value: val, rule });
                            info.added.push({ label: `属性: ${name}`, el: newEl });
                        }
                    }
                }

                if (pc.initial_inventory && Array.isArray(pc.initial_inventory)) {
                    const invContainer = document.getElementById('w-inventory');
                    const invNameMap = new Map();
                    invContainer.querySelectorAll('.attr-editor').forEach(row => {
                        const n = row.querySelector('.w-inv-item')?.value;
                        if (n) invNameMap.set(n, row);
                    });
                    for (const inv of pc.initial_inventory) {
                        const existing = invNameMap.get(inv.item) || null;
                        if (existing) {
                            const oldQty = existing.querySelector('.w-inv-qty').value;
                            existing.querySelector('.w-inv-qty').value = inv.quantity || 1;
                            info.updated.push({
                                label: `背包: ${inv.item}`,
                                undoFn: () => { existing.querySelector('.w-inv-qty').value = oldQty; },
                            });
                        } else {
                            const newEl = addInventoryRow(inv);
                            info.added.push({ label: `背包: ${inv.item}`, el: newEl });
                        }
                    }
                }
            }
            // Opening — merge text and choices
            if (data.opening) {
                const openEl = document.getElementById('w-opening-text');
                const oldText = openEl.value;
                if (data.opening.text) openEl.value = data.opening.text;
                const choicesContainer = document.getElementById('w-opening-choices');
                const oldChoicesHtml = choicesContainer.innerHTML;
                if (data.opening.choices && Array.isArray(data.opening.choices)) {
                    const editors = choicesContainer.querySelectorAll('.choice-editor');
                    editors.forEach((ce, idx) => {
                        const ch = data.opening.choices[idx];
                        if (!ch) return;
                        if (ch.text) ce.querySelector('.w-choice-text').value = ch.text;
                        if (ch.result) {
                            const desc = typeof ch.result === 'string' ? ch.result : (ch.result.description || '');
                            ce.querySelector('.w-choice-result').value = desc;
                            if (typeof ch.result === 'object' && ch.result.type === 'conditional') {
                                ce.querySelector('.w-choice-type').value = 'conditional';
                                toggleChoiceConditional(ce.querySelector('.w-choice-type'));
                                if (ch.result.threshold) ce.querySelector('.w-choice-threshold').value = ch.result.threshold;
                                if (ch.result.success_description) ce.querySelector('.w-choice-success').value = ch.result.success_description;
                                if (ch.result.failure_description) ce.querySelector('.w-choice-failure').value = ch.result.failure_description;
                            }
                        }
                    });
                    const excess = data.opening.choices.length - editors.length;
                    if (excess > 0) {
                        for (let i = editors.length; i < data.opening.choices.length; i++) {
                            info.skipped.push(`开局选项: ${data.opening.choices[i].text || '(未命名)'}`);
                        }
                    }
                }
                info.updated.push({
                    label: '开局设定',
                    undoFn: () => { openEl.value = oldText; choicesContainer.innerHTML = oldChoicesHtml; },
                });
            }
            // NPCs — merge by ID
            if (data.npcs && Array.isArray(data.npcs)) {
                const container = document.getElementById('w-npcs');
                // Note: schedule/organizations/relationships have separate merge logic below; not in this map.
                const npcFieldMap = {
                    name: 'w-npc-name', bio: 'w-npc-bio', personality: 'w-npc-personality',
                    capabilities: 'w-npc-caps', title: 'w-npc-title',
                    superior: 'w-npc-superior', related_lore: 'w-npc-lore',
                    default_location: 'w-npc-location', attitude_toward_player: 'w-npc-attitude',
                    portrait_desc: 'w-npc-portrait',
                };
                const npcIdMap = _buildEditorIdMap(container, 'w-npc-id');
                for (const npc of data.npcs) {
                    const existing = npc.id ? npcIdMap.get(npc.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, npcFieldMap);
                        _updateEditorFields(existing, npc, npcFieldMap);
                        // known checkbox
                        const knownCb = existing.querySelector('.w-npc-known');
                        const oldKnown = knownCb ? knownCb.checked : true;
                        if (knownCb && npc.known !== undefined) knownCb.checked = npc.known;
                        // met checkbox
                        const metCb = existing.querySelector('.w-npc-met');
                        const oldMet = metCb ? metCb.checked : true;
                        if (metCb && npc.met !== undefined) metCb.checked = npc.met;
                        // organizations merge
                        const orgsContainer = existing.querySelector('.npc-orgs-list');
                        let oldOrgsHtml = '';
                        const npcOrgs = npc.organizations || (npc.organization ? [{ org_id: npc.organization, rank: npc.rank }] : null);
                        if (npcOrgs && Array.isArray(npcOrgs) && orgsContainer) {
                            oldOrgsHtml = orgsContainer.innerHTML;
                            orgsContainer.innerHTML = '';
                            for (const om of npcOrgs) _addNpcOrgRow(orgsContainer, om);
                        }
                        const capturedOldOrgsHtml = oldOrgsHtml;
                        // schedule merge
                        const schedContainer = existing.querySelector('.npc-schedule-rows');
                        let oldSchedHtml = '';
                        if (npc.schedule && Array.isArray(npc.schedule) && schedContainer) {
                            oldSchedHtml = schedContainer.innerHTML;
                            schedContainer.innerHTML = '';
                            for (const s of npc.schedule) {
                                const row = document.createElement('div');
                                row.className = 'npc-schedule-row';
                                row.innerHTML = `<input type="text" class="w-sched-time" placeholder="时间段" value="${escapeHtml(s.time_range||s.time||'')}"><input type="text" class="w-sched-loc" placeholder="地点ID" value="${escapeHtml(s.location||'')}"><input type="text" class="w-sched-act" placeholder="活动" value="${escapeHtml(s.activity||'')}"><input type="text" class="w-sched-cond" placeholder="条件(可选)" value="${escapeHtml(s.condition||'')}" style="max-width:140px"><input type="number" class="w-sched-prio" placeholder="优先级" value="${s.priority||''}" style="width:50px"><button class="btn-remove-inline" onclick="this.parentElement.remove()">&times;</button>`;
                                schedContainer.appendChild(row);
                            }
                        }
                        const capturedOldSchedHtml = oldSchedHtml;
                        info.updated.push({
                            label: `NPC: ${npc.name || npc.id}`,
                            undoFn: () => {
                                _restoreEditorFields(existing, oldVals, npcFieldMap);
                                if (knownCb) knownCb.checked = oldKnown;
                                if (metCb) metCb.checked = oldMet;
                                if (capturedOldOrgsHtml && orgsContainer) orgsContainer.innerHTML = capturedOldOrgsHtml;
                                if (capturedOldSchedHtml && schedContainer) schedContainer.innerHTML = capturedOldSchedHtml;
                            },
                        });
                    } else {
                        const newEl = addNpcEditor(npc);
                        info.added.push({ label: `NPC: ${npc.name || npc.id}`, el: newEl });
                    }
                }
            }
            // Player presets — merge by ID (append new, update existing)
            if (data.player_presets && Array.isArray(data.player_presets)) {
                const container = document.getElementById('w-presets');
                const presetFieldMap = {
                    name: 'w-preset-name', bio: 'w-preset-bio', personality: 'w-preset-personality',
                    initial_location: 'w-preset-location', long_term_goal: 'w-preset-goal',
                    portrait_desc: 'w-preset-portrait', opening_text: 'w-preset-opening',
                };
                const presetIdMap = _buildEditorIdMap(container, 'w-preset-id');
                for (const p of data.player_presets) {
                    const existing = p.id ? presetIdMap.get(p.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, presetFieldMap);
                        _updateEditorFields(existing, p, presetFieldMap);
                        // Merge opening_choices if AI returned them
                        const choicesContainer = existing.querySelector('.preset-choices-rows');
                        let oldChoicesHtml = '';
                        if (p.opening_choices && Array.isArray(p.opening_choices) && choicesContainer) {
                            oldChoicesHtml = choicesContainer.innerHTML;
                            choicesContainer.innerHTML = '';
                            for (const c of p.opening_choices) {
                                addPresetChoiceRow(choicesContainer, c);
                            }
                        }
                        const capturedOldChoicesHtml = oldChoicesHtml;
                        info.updated.push({
                            label: `预设: ${p.name || p.id}`,
                            undoFn: () => {
                                _restoreEditorFields(existing, oldVals, presetFieldMap);
                                if (capturedOldChoicesHtml && choicesContainer) choicesContainer.innerHTML = capturedOldChoicesHtml;
                            },
                        });
                    } else {
                        const newEl = addPresetEditor(p);
                        info.added.push({ label: `预设: ${p.name || p.id}`, el: newEl });
                    }
                }
            }
            // NPC relationships — merge by from+to
            if (data.npc_relationships && Array.isArray(data.npc_relationships)) {
                const oldEdges = JSON.parse(JSON.stringify(_NpcRelGraph.edges));
                const incoming = [];
                for (const r of data.npc_relationships) {
                    if ('from' in r && 'to' in r) {
                        incoming.push({ from: r.from, to: r.to, trust: r.trust ?? 50, affection: r.affection ?? 50, fear: r.fear ?? 0, initially_known: r.initially_known !== false, initially_met: r.initially_met !== false, description: r.description || '' });
                    } else if ('a' in r && 'b' in r) {
                        const vals = _NPC_REL_TYPE_MAP[r.type] || {trust:50,affection:50,fear:0};
                        const ik = r.initially_known !== false;
                        const im = r.initially_met !== false;
                        const desc = r.description || '';
                        incoming.push({from: r.a, to: r.b, ...vals, initially_known: ik, initially_met: im, description: desc});
                        incoming.push({from: r.b, to: r.a, ...vals, initially_known: ik, initially_met: im, description: desc});
                    }
                }
                let addedCount = 0, updatedCount = 0;
                const edgeKeyMap = new Map();
                _NpcRelGraph.edges.forEach((e, i) => edgeKeyMap.set(e.from + '|' + e.to, i));
                for (const inc of incoming) {
                    const key = inc.from + '|' + inc.to;
                    const idx = edgeKeyMap.has(key) ? edgeKeyMap.get(key) : -1;
                    if (idx >= 0) {
                        Object.assign(_NpcRelGraph.edges[idx], inc);
                        updatedCount++;
                    } else {
                        edgeKeyMap.set(key, _NpcRelGraph.edges.length);
                        _NpcRelGraph.edges.push(inc);
                        addedCount++;
                    }
                }
                if (addedCount > 0 || updatedCount > 0) {
                    _NpcRelGraph.refresh();
                    const parts = [];
                    if (updatedCount) parts.push(`更新${updatedCount}`);
                    if (addedCount) parts.push(`新增${addedCount}`);
                    info.updated.push({
                        label: `NPC关系 ${parts.join(', ')}`,
                        undoFn: () => { _NpcRelGraph.edges = oldEdges; _NpcRelGraph.refresh(); },
                    });
                }
            }
            _appendSupplementaryLorebook(data, info);
            break;
        }
        case 'world': {
            // Locations — merge by ID
            if (data.locations && Array.isArray(data.locations)) {
                const container = document.getElementById('w-locations');
                const locFieldMap = { name: 'w-loc-name', description: 'w-loc-desc', travel_time: 'w-loc-travel-time' };
                const locIdMap = _buildEditorIdMap(container, 'w-loc-id');
                for (const loc of data.locations) {
                    const existing = loc.id ? locIdMap.get(loc.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, locFieldMap);
                        const visCb = existing.querySelector('.w-loc-visible');
                        const oldVis = visCb ? visCb.checked : true;
                        const connEl = existing.querySelector('.w-loc-connections');
                        const oldConn = connEl ? connEl.value : '';
                        _updateEditorFields(existing, loc, locFieldMap);
                        if (visCb && loc.initially_visible !== undefined) visCb.checked = loc.initially_visible;
                        if (connEl && loc.connections) connEl.value = Array.isArray(loc.connections) ? loc.connections.join(', ') : loc.connections;
                        // 合并可互动元素
                        if (Array.isArray(loc.interactables)) {
                            const iaCont = existing.querySelector('.w-loc-interactables');
                            if (iaCont) {
                                const oldIaHtml = iaCont.innerHTML;
                                iaCont.innerHTML = '';
                                for (const ia of loc.interactables) _addInteractableRow(iaCont, ia);
                                info.updated.push({
                                    label: `地点: ${loc.name || loc.id}`,
                                    undoFn: () => { _restoreEditorFields(existing, oldVals, locFieldMap); if (visCb) visCb.checked = oldVis; if (connEl) connEl.value = oldConn; if (iaCont) iaCont.innerHTML = oldIaHtml; },
                                });
                            } else {
                                info.updated.push({
                                    label: `地点: ${loc.name || loc.id}`,
                                    undoFn: () => { _restoreEditorFields(existing, oldVals, locFieldMap); if (visCb) visCb.checked = oldVis; if (connEl) connEl.value = oldConn; },
                                });
                            }
                        } else {
                        info.updated.push({
                            label: `地点: ${loc.name || loc.id}`,
                            undoFn: () => { _restoreEditorFields(existing, oldVals, locFieldMap); if (visCb) visCb.checked = oldVis; if (connEl) connEl.value = oldConn; },
                        });
                        }
                    } else {
                        const newEl = addLocationEditor(loc);
                        info.added.push({ label: `地点: ${loc.name || loc.id}`, el: newEl });
                    }
                }
            }
            // World properties — merge by ID
            if (data.world_properties && Array.isArray(data.world_properties)) {
                const container = document.getElementById('w-world-props');
                const wpFieldMap = { name: 'w-prop-name', value: 'w-prop-value', rule: 'w-prop-rule' };
                const wpIdMap = _buildEditorIdMap(container, 'w-prop-id');
                for (const wp of data.world_properties) {
                    const existing = wp.id ? wpIdMap.get(wp.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, wpFieldMap);
                        _updateEditorFields(existing, wp, wpFieldMap);
                        info.updated.push({
                            label: `世界属性: ${wp.name || wp.id}`,
                            undoFn: () => _restoreEditorFields(existing, oldVals, wpFieldMap),
                        });
                    } else {
                        const newEl = addWorldPropEditor(wp);
                        info.added.push({ label: `世界属性: ${wp.name || wp.id}`, el: newEl });
                    }
                }
            }
            // Persistent states — merge by ID
            if (data.persistent_states && Array.isArray(data.persistent_states)) {
                const container = document.getElementById('w-persistent-states');
                const psFieldMap = { name: 'w-ps-name', description: 'w-ps-desc' };
                const psIdMap = _buildEditorIdMap(container, 'w-ps-id');
                for (const ps of data.persistent_states) {
                    const existing = ps.id ? psIdMap.get(ps.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, psFieldMap);
                        const activeCb = existing.querySelector('.w-ps-active');
                        const oldActive = activeCb ? activeCb.checked : true;
                        const expiryEl = existing.querySelector('.w-ps-expiry');
                        const oldExpiry = expiryEl ? expiryEl.value : '';
                        _updateEditorFields(existing, ps, psFieldMap);
                        if (activeCb && ps.initially_active !== undefined) activeCb.checked = ps.initially_active;
                        if (expiryEl && ps.expires_at) expiryEl.value = String(ps.expires_at).substring(0, 16);
                        info.updated.push({
                            label: `持续状态: ${ps.name || ps.id}`,
                            undoFn: () => { _restoreEditorFields(existing, oldVals, psFieldMap); if (activeCb) activeCb.checked = oldActive; if (expiryEl) expiryEl.value = oldExpiry; },
                        });
                    } else {
                        const newEl = addPersistentStateEditor(ps);
                        info.added.push({ label: `持续状态: ${ps.name || ps.id}`, el: newEl });
                    }
                }
            }
            _appendSupplementaryLorebook(data, info);
            break;
        }
        case 'organizations': {
            // Organizations — merge by ID
            if (data.organizations && Array.isArray(data.organizations)) {
                const container = document.getElementById('w-organizations');
                const orgFieldMap = { name: 'w-org-name', type: 'w-org-type', parent_org: 'w-org-parent', leader: 'w-org-leader', stance: 'w-org-stance', aliases: 'w-org-aliases', description: 'w-org-desc' };
                const orgIdMap = _buildEditorIdMap(container, 'w-org-id');
                for (const org of data.organizations) {
                    const existing = org.id ? orgIdMap.get(org.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, orgFieldMap);
                        _updateEditorFields(existing, org, orgFieldMap);
                        // Hierarchy needs special handling (object array, not simple value)
                        const hList = existing.querySelector('.w-org-hierarchy-list');
                        const oldHierarchyHtml = hList ? hList.innerHTML : '';
                        if (Array.isArray(org.hierarchy) && org.hierarchy.length) {
                            if (hList) {
                                hList.innerHTML = '';
                                for (const h of org.hierarchy) {
                                    const row = document.createElement('div');
                                    row.className = 'w-org-rank-row';
                                    row.style.cssText = 'display:flex;gap:4px;margin-top:2px';
                                    row.innerHTML = `<input type="number" class="w-org-rank-num" placeholder="级别" value="${h.rank||''}" style="width:60px"><input type="text" class="w-org-rank-title" placeholder="职位名称" value="${escapeHtml(h.title||'')}"><button class="btn-remove-sm" onclick="this.parentElement.remove()" style="padding:0 6px">&times;</button>`;
                                    hList.appendChild(row);
                                }
                            }
                        }
                        info.updated.push({
                            label: `组织: ${org.name || org.id}`,
                            undoFn: () => { _restoreEditorFields(existing, oldVals, orgFieldMap); if (hList) hList.innerHTML = oldHierarchyHtml; },
                        });
                    } else {
                        const newEl = addOrganizationEditor(org);
                        info.added.push({ label: `组织: ${org.name || org.id}`, el: newEl });
                    }
                }
            }
            // Org relationships — merge by a+b pair
            if (data.org_relationships && Array.isArray(data.org_relationships)) {
                const container = document.getElementById('w-org-rels');
                if (container) {
                    const orFieldMap = { description: 'w-orgrel-desc' };
                    const orgRelMap = new Map();
                    container.querySelectorAll('.org-rel-editor').forEach(el => {
                        const elA = el.querySelector('.w-orgrel-a')?.value || '';
                        const elB = el.querySelector('.w-orgrel-b')?.value || '';
                        if (elA && elB) orgRelMap.set(elA + '|' + elB, el);
                    });
                    for (const rel of data.org_relationships) {
                        const existing = orgRelMap.get(rel.a + '|' + rel.b) || null;
                        if (existing) {
                            const oldType = existing.querySelector('.w-orgrel-type')?.value || '';
                            const oldDesc = existing.querySelector('.w-orgrel-desc')?.value || '';
                            const oldVisibility = existing.querySelector('.w-orgrel-visibility')?.value || 'public';
                            const oldSource = existing._orgRelSource;
                            if (rel.type) existing.querySelector('.w-orgrel-type').value = rel.type;
                            if (rel.description) existing.querySelector('.w-orgrel-desc').value = rel.description;
                            if (rel.visibility) existing.querySelector('.w-orgrel-visibility').value = rel.visibility;
                            existing._orgRelSource = { ...oldSource, ...rel };
                            info.updated.push({
                                label: `组织关系: ${rel.a}↔${rel.b}`,
                                undoFn: () => {
                                    existing.querySelector('.w-orgrel-type').value = oldType;
                                    existing.querySelector('.w-orgrel-desc').value = oldDesc;
                                    existing.querySelector('.w-orgrel-visibility').value = oldVisibility;
                                    existing._orgRelSource = oldSource;
                                },
                            });
                        } else {
                            const newEl = addOrgRelEditor(rel);
                            info.added.push({ label: `组织关系: ${rel.a}↔${rel.b}`, el: newEl });
                        }
                    }
                }
            }
            _appendSupplementaryLorebook(data, info);
            break;
        }
        case 'events': {
            // Cyclic events — merge by ID
            if (data.cyclic_events && Array.isArray(data.cyclic_events)) {
                const container = document.getElementById('w-cyclic-events');
                const ceFieldMap = { name: 'w-ce-name', description: 'w-ce-desc', condition: 'w-ce-condition', fire_events: 'w-ce-fire-events', activate_events: 'w-ce-activate-events' };
                const ceIdMap = _buildEditorIdMap(container, 'w-ce-id');
                for (const evt of data.cyclic_events) {
                    const existing = evt.id ? ceIdMap.get(evt.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, ceFieldMap);
                        const freqValEl = existing.querySelector('.w-ce-freq-val');
                        const freqUnitEl = existing.querySelector('.w-ce-freq-unit');
                        const firstEl = existing.querySelector('.w-ce-first');
                        const expiryEl = existing.querySelector('.w-ce-expiry');
                        const oldFreqVal = freqValEl?.value, oldFreqUnit = freqUnitEl?.value;
                        const oldFirst = firstEl?.value, oldExpiry = expiryEl?.value;
                        _updateEditorFields(existing, evt, ceFieldMap);
                        if (freqValEl && evt.frequency_value) freqValEl.value = evt.frequency_value;
                        if (freqUnitEl && evt.frequency_unit) freqUnitEl.value = evt.frequency_unit;
                        if (firstEl && evt.first_trigger) firstEl.value = String(evt.first_trigger).substring(0, 16);
                        if (expiryEl && evt.expires_at) expiryEl.value = String(evt.expires_at).substring(0, 16);
                        info.updated.push({
                            label: `周期事件: ${evt.description || evt.id}`,
                            undoFn: () => {
                                _restoreEditorFields(existing, oldVals, ceFieldMap);
                                if (freqValEl) freqValEl.value = oldFreqVal;
                                if (freqUnitEl) freqUnitEl.value = oldFreqUnit;
                                if (firstEl) firstEl.value = oldFirst;
                                if (expiryEl) expiryEl.value = oldExpiry;
                            },
                        });
                    } else {
                        const newEl = addCyclicEventEditor(evt);
                        info.added.push({ label: `周期事件: ${evt.description || evt.id}`, el: newEl });
                    }
                }
            }
            // One-time events — merge by ID
            if (data.one_time_events && Array.isArray(data.one_time_events)) {
                const container = document.getElementById('w-onetime-events');
                const oteFieldMap = { name: 'w-ote-name', description: 'w-ote-desc', condition: 'w-ote-condition', fire_events: 'w-ote-fire-events', activate_events: 'w-ote-activate-events' };
                const oteIdMap = _buildEditorIdMap(container, 'w-ote-id');
                for (const evt of data.one_time_events) {
                    const existing = evt.id ? oteIdMap.get(evt.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, oteFieldMap);
                        const timeEl = existing.querySelector('.w-ote-time');
                        const oldTime = timeEl?.value;
                        _updateEditorFields(existing, evt, oteFieldMap);
                        if (timeEl && evt.trigger_time) timeEl.value = String(evt.trigger_time).substring(0, 16);
                        info.updated.push({
                            label: `一次性事件: ${evt.description || evt.id}`,
                            undoFn: () => { _restoreEditorFields(existing, oldVals, oteFieldMap); if (timeEl) timeEl.value = oldTime; },
                        });
                    } else {
                        const newEl = addOnetimeEventEditor(evt);
                        info.added.push({ label: `一次性事件: ${evt.description || evt.id}`, el: newEl });
                    }
                }
            }
            // Tone rules — merge by ID
            if (data.tone_rules && Array.isArray(data.tone_rules)) {
                const container = document.getElementById('w-tone-rules');
                const trFieldMap = { name: 'w-tone-name', condition: 'w-tone-condition', tone: 'w-tone-tone', narrative_style: 'w-tone-style' };
                const trIdMap = _buildEditorIdMap(container, 'w-tone-id');
                for (const rule of data.tone_rules) {
                    const existing = rule.id ? trIdMap.get(rule.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, trFieldMap);
                        const prioEl = existing.querySelector('.w-tone-priority');
                        const oldPrio = prioEl?.value;
                        _updateEditorFields(existing, rule, trFieldMap);
                        if (prioEl && rule.priority != null) prioEl.value = rule.priority;
                        info.updated.push({
                            label: `基调规则: ${rule.name || rule.id}`,
                            undoFn: () => { _restoreEditorFields(existing, oldVals, trFieldMap); if (prioEl) prioEl.value = oldPrio; },
                        });
                    } else {
                        const newEl = addToneRuleEditor(rule);
                        info.added.push({ label: `基调规则: ${rule.name || rule.id}`, el: newEl });
                    }
                }
            }
            // Quest templates — merge by ID
            if (data.quest_templates && Array.isArray(data.quest_templates)) {
                const container = document.getElementById('w-quest-templates');
                const qtFieldMap = { name: 'w-qt-name', description: 'w-qt-desc', condition: 'w-qt-condition', trigger_hint: 'w-qt-trigger', reward_hint: 'w-qt-reward' };
                const qtIdMap = _buildEditorIdMap(container, 'w-qt-id');
                for (const qt of data.quest_templates) {
                    const existing = qt.id ? qtIdMap.get(qt.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, qtFieldMap);
                        const cdEl = existing.querySelector('.w-qt-cooldown');
                        const oldCd = cdEl?.value;
                        _updateEditorFields(existing, qt, qtFieldMap);
                        if (cdEl && qt.cooldown_turns != null) cdEl.value = qt.cooldown_turns;
                        info.updated.push({
                            label: `支线模板: ${qt.name || qt.id}`,
                            undoFn: () => { _restoreEditorFields(existing, oldVals, qtFieldMap); if (cdEl) cdEl.value = oldCd; },
                        });
                    } else {
                        const newEl = addQuestTemplateEditor(qt);
                        info.added.push({ label: `支线模板: ${qt.name || qt.id}`, el: newEl });
                    }
                }
            }
            _appendSupplementaryLorebook(data, info);
            break;
        }
        case 'dice': {
            // Random items — merge by ID
            if (data.random_items && Array.isArray(data.random_items)) {
                const container = document.getElementById('w-random-items');
                const riFieldMap = {
                    description: 'w-ri-desc', trigger: 'w-ri-trigger',
                    linked_event_id: 'w-ri-linked-event', condition: 'w-ri-condition',
                };
                const riIdMap = _buildEditorIdMap(container, 'w-ri-id');
                for (const item of data.random_items) {
                    const existing = item.id ? riIdMap.get(item.id) : null;
                    if (existing) {
                        const oldVals = _captureEditorFields(existing, riFieldMap);
                        // Snapshot select + dice + ranges
                        const typeEl = existing.querySelector('.w-ri-type');
                        const oldType = typeEl ? typeEl.value : '';
                        const diceFields = ['w-ri-dice-count','w-ri-dice-faces','w-ri-dice-mod','w-ri-duration','w-ri-cooldown','w-ri-keep-high','w-ri-keep-low'];
                        const oldDice = {};
                        for (const cls of diceFields) { const el = existing.querySelector('.'+cls); if (el) oldDice[cls] = el.value; }
                        const rangesContainer = existing.querySelector('.w-ri-ranges');
                        const oldRangesHtml = rangesContainer ? rangesContainer.innerHTML : '';
                        _updateEditorFields(existing, item, riFieldMap);
                        // trigger_type select
                        if (typeEl && item.trigger_type) typeEl.value = item.trigger_type;
                        // dice sub-fields
                        if (item.dice) {
                            const d = item.dice;
                            const _s = (cls, v) => { const el = existing.querySelector('.'+cls); if (el && v !== undefined) el.value = v; };
                            _s('w-ri-dice-count', d.count); _s('w-ri-dice-faces', d.faces); _s('w-ri-dice-mod', d.modifier);
                            _s('w-ri-keep-high', d.keep_highest); _s('w-ri-keep-low', d.keep_lowest);
                        }
                        if (item.duration_turns !== undefined) { const el = existing.querySelector('.w-ri-duration'); if (el) el.value = item.duration_turns; }
                        if (item.cooldown_turns !== undefined) { const el = existing.querySelector('.w-ri-cooldown'); if (el) el.value = item.cooldown_turns; }
                        // Update ranges if provided
                        if (item.ranges && Array.isArray(item.ranges) && rangesContainer) {
                            rangesContainer.innerHTML = '';
                            for (const r of item.ranges) {
                                const row = document.createElement('div');
                                row.innerHTML = _buildRangeRowHtml(r);
                                rangesContainer.appendChild(row.firstElementChild);
                            }
                        }
                        info.updated.push({
                            label: `随机项: ${item.description || item.id}`,
                            undoFn: () => {
                                _restoreEditorFields(existing, oldVals, riFieldMap);
                                if (typeEl) typeEl.value = oldType;
                                for (const cls of diceFields) { const el = existing.querySelector('.'+cls); if (el && oldDice[cls] !== undefined) el.value = oldDice[cls]; }
                                if (rangesContainer) rangesContainer.innerHTML = oldRangesHtml;
                            },
                        });
                    } else {
                        const newEl = addRandomItemEditor(item);
                        info.added.push({ label: `随机项: ${item.description || item.id}`, el: newEl });
                    }
                }
            }
            // Variables — merge by ID
            if (data.variables && Array.isArray(data.variables)) {
                const container = document.getElementById('w-variables');
                const varIdMap = _buildEditorIdMap(container, 'w-var-id');
                for (const v of data.variables) {
                    const existing = v.id ? varIdMap.get(v.id) : null;
                    if (existing) {
                        const nameEl = existing.querySelector('.w-var-name');
                        const oldName = nameEl ? nameEl.value : '';
                        if (nameEl && v.name) nameEl.value = v.name;
                        const typeEl = existing.querySelector('.w-var-type');
                        const oldType = typeEl ? typeEl.value : '';
                        if (typeEl && v.type) typeEl.value = v.type;
                        const defEl = existing.querySelector('.w-var-default');
                        const oldDef = defEl ? defEl.value : '';
                        if (defEl && v.default !== undefined) defEl.value = v.default;
                        const minEl = existing.querySelector('.w-var-min');
                        const maxEl = existing.querySelector('.w-var-max');
                        const oldMin = minEl ? minEl.value : '';
                        const oldMax = maxEl ? maxEl.value : '';
                        if (minEl && v.min !== undefined) minEl.value = v.min;
                        if (maxEl && v.max !== undefined) maxEl.value = v.max;
                        info.updated.push({
                            label: `变量: ${v.name || v.id}`,
                            undoFn: () => {
                                if (nameEl) nameEl.value = oldName;
                                if (typeEl) typeEl.value = oldType;
                                if (defEl) defEl.value = oldDef;
                                if (minEl) minEl.value = oldMin;
                                if (maxEl) maxEl.value = oldMax;
                            },
                        });
                    } else {
                        const newEl = addVariableEditor(v);
                        info.added.push({ label: `变量: ${v.name || v.id}`, el: newEl });
                    }
                }
            }
            // Triggers — merge by ID
            if (data.triggers && Array.isArray(data.triggers)) {
                const container = document.getElementById('w-triggers');
                const trigIdMap = new Map();
                container.querySelectorAll('.trigger-editor').forEach(el => {
                    const params = el.querySelector('.w-trig-params');
                    try {
                        const p = JSON.parse(params?.value || '{}');
                        if (p.id) trigIdMap.set(p.id, el);
                    } catch {}
                    // Also check if there's a data-id attribute or embedded id
                    const eventEl = el.querySelector('.w-trig-event');
                    const actionEl = el.querySelector('.w-trig-action');
                    const condEl = el.querySelector('.w-trig-condition');
                    const key = `${eventEl?.value}|${actionEl?.value}|${condEl?.value}`;
                    if (!trigIdMap.has(key)) trigIdMap.set(key, el);
                });
                for (const t of data.triggers) {
                    const existing = t.id ? trigIdMap.get(t.id) : null;
                    if (existing) {
                        const condEl = existing.querySelector('.w-trig-condition');
                        const oldCond = condEl ? condEl.value : '';
                        if (condEl && t.condition !== undefined) condEl.value = t.condition;
                        info.updated.push({
                            label: `触发器: ${t.id}`,
                            undoFn: () => { if (condEl) condEl.value = oldCond; },
                        });
                    } else {
                        const newEl = addTriggerEditor(t);
                        info.added.push({ label: `触发器: ${t.id || t.event}`, el: newEl });
                    }
                }
            }
            // Regex scripts — merge by ID
            if (data.regex_scripts && Array.isArray(data.regex_scripts)) {
                const container = document.getElementById('w-regex-scripts');
                const rxIdMap = new Map();
                container.querySelectorAll('.regex-editor').forEach(el => {
                    const nameEl = el.querySelector('.w-rx-name');
                    if (nameEl && nameEl.value) rxIdMap.set(nameEl.value, el);
                });
                for (const r of data.regex_scripts) {
                    const existing = (r.id && rxIdMap.get(r.id)) || (r.name && rxIdMap.get(r.name)) || null;
                    if (existing) {
                        const nameEl = existing.querySelector('.w-rx-name');
                        const oldName = nameEl ? nameEl.value : '';
                        if (nameEl && r.name) nameEl.value = r.name;
                        info.updated.push({
                            label: `正则: ${r.name || r.id}`,
                            undoFn: () => { if (nameEl) nameEl.value = oldName; },
                        });
                    } else {
                        const newEl = addRegexScriptEditor(r);
                        info.added.push({ label: `正则: ${r.name || r.id}`, el: newEl });
                    }
                }
            }
            _appendSupplementaryLorebook(data, info);
            break;
        }
        case 'lorebook': {
            // Lorebook — merge by ID
            if (data.lorebook && Array.isArray(data.lorebook)) {
                const container = document.getElementById('w-lorebook');
                const loreIdMap = _buildEditorIdMap(container, 'w-lore-id');
                for (const entry of data.lorebook) {
                    const existing = entry.id ? loreIdMap.get(entry.id) : null;
                    if (existing) {
                        const oldContent = existing.querySelector('.w-lore-content')?.value || '';
                        const oldKeys = existing.querySelector('.w-lore-keys')?.value || '';
                        const oldComment = existing.querySelector('.w-lore-comment')?.value || '';
                        const relEl = existing.querySelector('.w-lore-related');
                        const oldRelated = relEl ? relEl.value : '';
                        if (entry.content) existing.querySelector('.w-lore-content').value = entry.content;
                        if (entry.keys) existing.querySelector('.w-lore-keys').value = Array.isArray(entry.keys) ? entry.keys.join(', ') : entry.keys;
                        if (entry.comment) existing.querySelector('.w-lore-comment').value = entry.comment;
                        if (entry.related_entries && relEl) relEl.value = Array.isArray(entry.related_entries) ? entry.related_entries.join(', ') : entry.related_entries;
                        info.updated.push({
                            label: `知识库: ${entry.comment || entry.id}`,
                            undoFn: () => {
                                existing.querySelector('.w-lore-content').value = oldContent;
                                existing.querySelector('.w-lore-keys').value = oldKeys;
                                existing.querySelector('.w-lore-comment').value = oldComment;
                                if (relEl) relEl.value = oldRelated;
                            },
                        });
                    } else {
                        const newEl = addLorebookEditor(entry);
                        info.added.push({ label: `知识库: ${entry.comment || entry.id || '新词条'}`, el: newEl });
                    }
                }
            }
            _appendSupplementaryLorebook(data, info);
            break;
        }

        case 'story_tree': {
            const trees = data.trees || data.story_tree?.trees || [];
            if (trees.length > 0) {
                const container = document.getElementById('w-story-trees');
                const treeIdMap = _buildEditorIdMap(container, 'w-st-id');
                for (const tree of trees) {
                    const existing = tree.id ? treeIdMap.get(tree.id) : null;
                    if (existing) {
                        const oldName = existing.querySelector('.w-st-name')?.value || '';
                        const oldDesc = existing.querySelector('.w-st-desc')?.value || '';
                        if (tree.name) existing.querySelector('.w-st-name').value = tree.name;
                        if (tree.description) existing.querySelector('.w-st-desc').value = tree.description;
                        const nodesContainer = existing.querySelector('.story-nodes-container');
                        const addedNodeEls = [];
                        const updatedNodeUndos = [];
                        if (tree.nodes && nodesContainer) {
                            const existingNodeMap = new Map();
                            for (const nel of nodesContainer.querySelectorAll('.story-node-editor')) {
                                const nid = nel.querySelector('.w-stn-id')?.value;
                                if (nid) existingNodeMap.set(nid, nel);
                            }
                            for (const node of tree.nodes) {
                                const existingNode = node.id ? existingNodeMap.get(node.id) : null;
                                if (existingNode) {
                                    const oldNName = existingNode.querySelector('.w-stn-name')?.value || '';
                                    const oldNDesc = existingNode.querySelector('.w-stn-desc')?.value || '';
                                    const oldNCond = existingNode.querySelector('.w-stn-condition')?.value || '';
                                    const oldNEvent = existingNode.querySelector('.w-stn-event')?.value || '';
                                    const oldNCooldown = existingNode.querySelector('.w-stn-cooldown')?.value || '';
                                    const oldNWeight = existingNode.querySelector('.w-stn-weight')?.value || '';
                                    const oldNActivate = existingNode.querySelector('.w-stn-activate-events')?.value || '';
                                    if (node.name) existingNode.querySelector('.w-stn-name').value = node.name;
                                    if (node.description) existingNode.querySelector('.w-stn-desc').value = node.description;
                                    if (node.condition !== undefined) existingNode.querySelector('.w-stn-condition').value = node.condition || '';
                                    if (node.event) existingNode.querySelector('.w-stn-event').value = Array.isArray(node.event) ? node.event.join(', ') : node.event;
                                    if (node.cooldown !== undefined) existingNode.querySelector('.w-stn-cooldown').value = node.cooldown;
                                    if (node.weight !== undefined) existingNode.querySelector('.w-stn-weight').value = node.weight;
                                    if (node.activate_events) existingNode.querySelector('.w-stn-activate-events').value = Array.isArray(node.activate_events) ? node.activate_events.join(', ') : node.activate_events;
                                    if (node.type) {
                                        existingNode.querySelector('.w-stn-type').value = node.type;
                                        existingNode.querySelector('.w-stn-type').dispatchEvent(new Event('change'));
                                    }
                                    updatedNodeUndos.push(() => {
                                        existingNode.querySelector('.w-stn-name').value = oldNName;
                                        existingNode.querySelector('.w-stn-desc').value = oldNDesc;
                                        existingNode.querySelector('.w-stn-condition').value = oldNCond;
                                        existingNode.querySelector('.w-stn-event').value = oldNEvent;
                                        existingNode.querySelector('.w-stn-cooldown').value = oldNCooldown;
                                        existingNode.querySelector('.w-stn-weight').value = oldNWeight;
                                        existingNode.querySelector('.w-stn-activate-events').value = oldNActivate;
                                    });
                                } else {
                                    addedNodeEls.push(addStoryNodeEditor(node, nodesContainer));
                                }
                            }
                        }
                        info.updated.push({
                            label: `剧情线: ${tree.name || tree.id}`,
                            undoFn: () => {
                                existing.querySelector('.w-st-name').value = oldName;
                                existing.querySelector('.w-st-desc').value = oldDesc;
                                for (const el of addedNodeEls) el.remove();
                                for (const fn of updatedNodeUndos) fn();
                            },
                        });
                    } else {
                        const newEl = addStoryTreeEditor(tree);
                        info.added.push({ label: `剧情线: ${tree.name || tree.id || '新剧情线'}`, el: newEl });
                    }
                }
            }
            if (typeof stIsGraphMode === 'function' && stIsGraphMode() && typeof _stSyncFormsToEditor === 'function') {
                _stSyncFormsToEditor();
            }
            break;
        }
    }

    return info;
}

/** Render merge summary into copilot panel with per-item undo buttons. */
function _renderMergeSummary(tab, info, snapshot) {
    const panel = document.getElementById('copilot-content');
    if (!panel) return;
    panel._hasGenerateResult = true;

    // Build flat items list for undo tracking
    const allItems = [];
    for (const a of info.added) allItems.push({ ...a, type: 'added' });
    for (const u of info.updated) allItems.push({ ...u, type: 'updated' });

    const label = _TAB_LABELS[tab] || tab;
    let html = `<div class="merge-summary" style="padding:0.5rem">`;
    html += `<h4 style="margin:0 0 0.5rem">「${escapeHtml(label)}」生成结果</h4>`;

    if (allItems.length === 0) {
        html += `<p style="color:var(--text-muted)">未检测到可合并的内容。</p>`;
    } else {
        let addedHtml = '', updatedHtml = '';
        for (let i = 0; i < allItems.length; i++) {
            const li = `<li data-undo-idx="${i}" style="font-size:0.82rem;display:flex;align-items:center;gap:0.3rem;margin-bottom:0.15rem"><span style="flex:1">${escapeHtml(allItems[i].label)}</span><button class="btn-undo-item" onclick="_undoSingleItem(${i})" style="font-size:0.7rem;padding:0.1rem 0.3rem;cursor:pointer">撤销</button></li>`;
            if (allItems[i].type === 'added') addedHtml += li;
            else updatedHtml += li;
        }
        if (info.added.length > 0) {
            html += `<div style="margin-bottom:0.4rem"><strong style="color:var(--success)">新增 ${info.added.length} 项:</strong><ul style="margin:0.2rem 0 0 1rem;padding:0;list-style:none">${addedHtml}</ul></div>`;
        }
        if (info.updated.length > 0) {
            html += `<div style="margin-bottom:0.4rem"><strong style="color:var(--warning, #e6a817)">更新 ${info.updated.length} 项:</strong><ul style="margin:0.2rem 0 0 1rem;padding:0;list-style:none">${updatedHtml}</ul></div>`;
        }
    }

    html += `<button class="btn-secondary" style="margin-top:0.5rem" onclick="_undoTabGenerate()">撤销全部</button>`;
    html += `</div>`;
    panel.innerHTML = html;

    // Store undo state
    panel._undoSnapshot = snapshot;
    panel._undoTab = tab;
    panel._undoItems = allItems;
}

/** Undo a single generated item. */
function _undoSingleItem(idx) {
    const panel = document.getElementById('copilot-content');
    if (!panel || !panel._undoItems) return;
    const item = panel._undoItems[idx];
    if (!item || item.undone) return;

    // Perform undo
    if (item.el) {
        item.el.remove();
    }
    if (item.undoFn) {
        item.undoFn();
    }
    item.undone = true;

    // Update UI — mark the line as undone
    const li = panel.querySelector(`[data-undo-idx="${idx}"]`);
    if (li) {
        li.style.textDecoration = 'line-through';
        li.style.opacity = '0.5';
        const btn = li.querySelector('.btn-undo-item');
        if (btn) btn.disabled = true;
    }

    if (typeof showNotification === 'function') {
        showNotification(`已撤销: ${item.label}`, 'info');
    }
}

/** 重新绑定 innerHTML 还原后丢失的动态按钮事件。 */
function _rebindDynamicButtons() {
    // NPC 编辑器内的 "+添加组织" 按钮
    document.querySelectorAll('#w-npcs .npc-editor').forEach(npcEl => {
        const addBtn = npcEl.querySelector('.btn-add-npc-org');
        if (addBtn && !addBtn._bound) {
            addBtn._bound = true;
            addBtn.addEventListener('click', () => {
                _addNpcOrgRow(npcEl.querySelector('.npc-orgs-list'));
            });
        }
    });
    // 组织编辑器内的 "+添加层级" 按钮
    document.querySelectorAll('#w-organizations .org-editor').forEach(orgEl => {
        const addBtn = orgEl.querySelector('.btn-add-rank');
        if (addBtn && !addBtn._bound) {
            addBtn._bound = true;
            addBtn.addEventListener('click', () => {
                const list = orgEl.querySelector('.w-org-hierarchy-list');
                const rows = list.querySelectorAll('.w-org-rank-row');
                const nextRank = rows.length + 1;
                const row = document.createElement('div');
                row.className = 'w-org-rank-row';
                row.style.cssText = 'display:flex;gap:4px;margin-top:2px';
                row.innerHTML = `<input type="number" class="w-org-rank-num" placeholder="级别" value="${nextRank}" style="width:60px"><input type="text" class="w-org-rank-title" placeholder="职位名称"><button class="btn-remove-sm" onclick="this.parentElement.remove()" style="padding:0 6px">&times;</button>`;
                list.appendChild(row);
            });
        }
    });
}

/** Undo the last tab generation by restoring DOM snapshot. */
function _undoTabGenerate() {
    const panel = document.getElementById('copilot-content');
    if (!panel || !panel._undoSnapshot) {
        alert('没有可撤销的操作');
        return;
    }

    const snapshot = panel._undoSnapshot;
    const tab = panel._undoTab;

    // Restore container innerHTML
    for (const [id, html] of Object.entries(snapshot)) {
        if (id.startsWith('_')) continue;
        const el = document.getElementById(id);
        if (el) el.innerHTML = html;
    }
    // innerHTML 还原会丢失 addEventListener 绑定，需重新绑定
    _rebindDynamicButtons();

    // Restore simple value fields
    if (snapshot._values) {
        for (const [id, val] of Object.entries(snapshot._values)) {
            const el = document.getElementById(id);
            if (el) el.value = val;
        }
    }

    // Restore NPC relationship graph edges
    if (snapshot._npcRelEdges) {
        _NpcRelGraph.edges = snapshot._npcRelEdges;
        _NpcRelGraph.refresh();
    }

    // Clear undo state
    panel._undoSnapshot = null;
    panel._undoTab = null;
    panel._undoItems = null;

    const label = _TAB_LABELS[tab] || tab;
    panel.innerHTML = `<div class="copilot-placeholder"><p>「${escapeHtml(label)}」已撤销，恢复到生成前状态。</p></div>`;
    panel._hasGenerateResult = false;
    _updateGuide();

    if (typeof showNotification === 'function') {
        showNotification(`「${label}」生成已撤销`, 'info');
    }
}

// ================================================
//  COPILOT: 补字段模式
// ================================================

const _FILL_FIELD_SCHEMA = {
    npc: {
        fields: ['bio','personality','capabilities','title','default_location','portrait_desc','schedule','superior','related_lore','goals'],
        labels: {bio:'简介',personality:'性格',capabilities:'能力/作用',title:'头衔/职位',default_location:'默认地点',portrait_desc:'外貌描述',schedule:'日程表',superior:'上级NPC',related_lore:'关联知识库',goals:'NPC目标'},
        tab: 'characters',
        listId: 'w-npcs',
        editorClass: 'npc-editor',
        idClass: 'w-npc-id',
        nameClass: 'w-npc-name',
        fieldClassMap: {bio:'w-npc-bio',personality:'w-npc-personality',capabilities:'w-npc-caps',title:'w-npc-title',default_location:'w-npc-location',portrait_desc:'w-npc-portrait',superior:'w-npc-superior',related_lore:'w-npc-lore'},
    },
    player: {
        fields: ['bio','personality','portrait_desc','long_term_goal','initial_location'],
        labels: {bio:'简介',personality:'性格',portrait_desc:'外貌描述',long_term_goal:'长期目标',initial_location:'初始位置'},
        tab: 'characters',
    },
    preset: {
        fields: ['bio','personality','initial_location','long_term_goal','portrait_desc','opening_text','opening_choices'],
        labels: {bio:'简介',personality:'性格',initial_location:'初始位置',long_term_goal:'长期目标',portrait_desc:'外貌描述',opening_text:'专属开局',opening_choices:'开局选项'},
        tab: 'characters',
        listId: 'w-presets',
        editorClass: 'preset-editor',
        idClass: 'w-preset-id',
        nameClass: 'w-preset-name',
        fieldClassMap: {bio:'w-preset-bio',personality:'w-preset-personality',initial_location:'w-preset-location',long_term_goal:'w-preset-goal',portrait_desc:'w-preset-portrait',opening_text:'w-preset-opening'},
    },
    location: {
        fields: ['description','connections','initially_visible'],
        labels: {description:'描述',connections:'连接地点',initially_visible:'初始可见'},
        tab: 'world',
        listId: 'w-locations',
        editorClass: 'loc-editor',
        idClass: 'w-loc-id',
        nameClass: 'w-loc-name',
        fieldClassMap: {description:'w-loc-desc',connections:'w-loc-connections'},
    },
    organization: {
        fields: ['type','parent_org','description','leader','stance','aliases','hierarchy','goals'],
        labels: {type:'类型',parent_org:'父组织',description:'描述',leader:'领导者',stance:'对主角立场',aliases:'别名',hierarchy:'层级架构',goals:'组织目标'},
        tab: 'organizations',
        listId: 'w-organizations',
        editorClass: 'org-editor',
        idClass: 'w-org-id',
        nameClass: 'w-org-name',
        fieldClassMap: {type:'w-org-type',parent_org:'w-org-parent',description:'w-org-desc',leader:'w-org-leader',stance:'w-org-stance',aliases:'w-org-aliases'},
    },
    one_time_event: {
        fields: ['name','description','trigger_time','condition','fire_events','activate_events'],
        labels: {name:'事件名称',description:'事件描述',trigger_time:'触发时间',condition:'触发条件',fire_events:'触发事件',activate_events:'激活事件'},
        tab: 'events',
        listId: 'w-onetime-events',
        editorClass: 'ote-editor',
        idClass: 'w-ote-id',
        nameClass: 'w-ote-name',
        fieldClassMap: {name:'w-ote-name',description:'w-ote-desc',trigger_time:'w-ote-time',condition:'w-ote-condition',fire_events:'w-ote-fire-events',activate_events:'w-ote-activate-events'},
    },
    cyclic_event: {
        fields: ['name','description','frequency_value','frequency_unit','first_trigger','condition','expires_at','fire_events','activate_events'],
        labels: {name:'事件名称',description:'事件描述',frequency_value:'频率值',frequency_unit:'频率单位',first_trigger:'首次触发',condition:'触发条件',expires_at:'过期时间',fire_events:'触发事件',activate_events:'激活事件'},
        tab: 'events',
        listId: 'w-cyclic-events',
        editorClass: 'ce-editor',
        idClass: 'w-ce-id',
        nameClass: 'w-ce-name',
        fieldClassMap: {name:'w-ce-name',description:'w-ce-desc',frequency_value:'w-ce-freq-val',frequency_unit:'w-ce-freq-unit',first_trigger:'w-ce-first',condition:'w-ce-condition',expires_at:'w-ce-expiry',fire_events:'w-ce-fire-events',activate_events:'w-ce-activate-events'},
    },
    random_item: {
        fields: ['description','trigger','trigger_type','condition','ranges','duration_turns','cooldown_turns'],
        labels: {description:'描述',trigger:'触发时机',trigger_type:'触发类型',condition:'触发条件',ranges:'结果区间',duration_turns:'持续回合',cooldown_turns:'冷却回合'},
        tab: 'dice',
        listId: 'w-random-items',
        editorClass: 'ri-editor',
        idClass: 'w-ri-id',
        nameClass: 'w-ri-desc',
        fieldClassMap: {description:'w-ri-desc',trigger:'w-ri-trigger',trigger_type:'w-ri-type',condition:'w-ri-condition',duration_turns:'w-ri-duration',cooldown_turns:'w-ri-cooldown'},
    },
    variable: {
        fields: ['name','type','default','min','max'],
        labels: {name:'显示名称',type:'类型',default:'默认值',min:'最小值',max:'最大值'},
        tab: 'dice',
        listId: 'w-variables',
        editorClass: 'var-editor',
        idClass: 'w-var-id',
        nameClass: 'w-var-name',
        fieldClassMap: {name:'w-var-name',type:'w-var-type',default:'w-var-default',min:'w-var-min',max:'w-var-max'},
    },
    trigger: {
        fields: ['event','action','condition','params'],
        labels: {event:'触发事件',action:'执行动作',condition:'触发条件',params:'参数'},
        tab: 'dice',
        listId: 'w-triggers',
        editorClass: 'trigger-editor',
        idClass: 'w-trig-event',
        nameClass: 'w-trig-condition',
        fieldClassMap: {event:'w-trig-event',action:'w-trig-action',condition:'w-trig-condition',params:'w-trig-params'},
    },
    regex_script: {
        fields: ['name','find','replace','placement'],
        labels: {name:'规则名称',find:'查找正则',replace:'替换内容',placement:'应用位置'},
        tab: 'dice',
        listId: 'w-regex-scripts',
        editorClass: 'regex-editor',
        idClass: 'w-rx-name',
        nameClass: 'w-rx-name',
        fieldClassMap: {name:'w-rx-name',find:'w-rx-find',replace:'w-rx-replace',placement:'w-rx-placement'},
    },
    story_tree_node: {
        fields: ['name','description','condition','activate_events','effects','choices'],
        labels: {name:'节点名称',description:'节点描述',condition:'触发条件',activate_events:'激活事件',effects:'效果',choices:'选项(choice类型)'},
        tab: 'story_tree',
        listId: 'w-story-trees',
        nested: true,
    },
};

let _copilotMode = 'result';
let _fillFieldsResultHtml = '';

function _switchCopilotMode(mode) {
    _copilotMode = mode;
    document.querySelectorAll('.copilot-mode-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
    });
    const panel = document.getElementById('copilot-content');
    if (mode === 'fillFields') {
        _fillFieldsResultHtml = panel.innerHTML;
        _renderFillFieldsPanel();
    } else {
        if (_fillFieldsResultHtml) panel.innerHTML = _fillFieldsResultHtml;
    }
}

function _renderFillFieldsPanel() {
    const panel = document.getElementById('copilot-content');
    panel.innerHTML = `
        <div class="fill-fields-form">
            <label>实体类型</label>
            <select id="ff-entity-type" onchange="_onFillTypeChange()">
                <option value="">-- 选择 --</option>
                <option value="npc">NPC</option>
                <option value="player">玩家角色</option>
                <option value="preset">预设角色</option>
                <option value="location">地点</option>
                <option value="organization">组织/势力</option>
                <option value="one_time_event">一次性事件</option>
                <option value="cyclic_event">周期性事件</option>
                <option value="random_item">随机项</option>
                <option value="story_tree_node">剧情树节点</option>
            </select>
            <div id="ff-entity-select-wrap" style="display:none">
                <label>选择实体 <span class="ff-select-actions"><a href="#" onclick="_ffToggleAll(true);return false">全选缺字段</a> / <a href="#" onclick="_ffToggleAll(false);return false">全不选</a></span></label>
                <div id="ff-entity-checks" class="fill-fields-checkboxes"></div>
            </div>
            <div id="ff-fields-wrap" style="display:none">
                <label>要补充的字段</label>
                <div id="ff-fields-checks" class="fill-fields-checkboxes"></div>
                <label>补充需求（可选）</label>
                <textarea id="ff-hint" rows="2" placeholder="对AI的额外指导..."></textarea>
                <button class="btn-primary" style="width:100%;margin-top:0.3rem" id="ff-run-btn" onclick="_runFillFields()">AI 补全</button>
            </div>
        </div>`;
}

async function _onFillTypeChange() {
    const type = document.getElementById('ff-entity-type').value;
    const wrap = document.getElementById('ff-entity-select-wrap');
    const fieldsWrap = document.getElementById('ff-fields-wrap');

    if (!type) {
        wrap.style.display = 'none';
        fieldsWrap.style.display = 'none';
        return;
    }

    const schemaForTab = _FILL_FIELD_SCHEMA[type];
    if (schemaForTab && schemaForTab.tab) {
        _ensureTabLoaded(schemaForTab.tab);
        if (_tabLoadPromises[schemaForTab.tab]) {
            await _tabLoadPromises[schemaForTab.tab];
        }
    }

    if (type === 'player') {
        wrap.style.display = 'none';
        fieldsWrap.style.display = '';
        _populateFieldChecks(type, [null]);
        return;
    }

    wrap.style.display = '';
    fieldsWrap.style.display = 'none';
    const schema = _FILL_FIELD_SCHEMA[type];

    if (type === 'story_tree_node') {
        const container = document.getElementById('w-story-trees');
        let html = '';
        if (container) {
            container.querySelectorAll('.story-tree-editor').forEach(treeEl => {
                const treeId = treeEl.querySelector('.w-st-id')?.value || '';
                const treeName = treeEl.querySelector('.w-st-name')?.value || treeId;
                treeEl.querySelectorAll('.story-node-editor').forEach(nodeEl => {
                    const nodeId = nodeEl.querySelector('.w-stn-id')?.value || '';
                    const nodeName = nodeEl.querySelector('.w-stn-name')?.value || nodeId;
                    const desc = nodeEl.querySelector('.w-stn-desc')?.value?.trim();
                    const hasEffects = nodeEl.querySelector('.w-stn-effects')?.value?.trim();
                    const missing = (!desc ? 1 : 0) + (!hasEffects ? 1 : 0);
                    const tag = missing > 0 ? ` <span style="color:var(--warning);font-size:0.7rem">(缺${missing}项)</span>` : ' <span style="font-size:0.7rem;opacity:0.6">(完整)</span>';
                    const checked = missing > 0 ? 'checked' : '';
                    const compositeId = treeId + '/' + nodeId;
                    html += `<label><input type="checkbox" value="${escapeAttr(compositeId)}" ${checked} onchange="_onFillEntityCheckChange()"> [${escapeHtml(treeName)}] ${escapeHtml(nodeName)}${tag}</label>`;
                });
            });
        }
        document.getElementById('ff-entity-checks').innerHTML = html;
        _onFillEntityCheckChange();
        return;
    }

    const container = document.getElementById(schema.listId);
    const editors = container ? container.querySelectorAll('.' + schema.editorClass) : [];

    let html = '';
    editors.forEach(ed => {
        const id = ed.querySelector('.' + schema.idClass)?.value || '';
        const name = ed.querySelector('.' + schema.nameClass)?.value || id;
        const missing = _countMissingFields(ed, type);
        const tag = missing > 0 ? ` <span style="color:var(--warning);font-size:0.7rem">(缺${missing}项)</span>` : ' <span style="font-size:0.7rem;opacity:0.6">(完整)</span>';
        const checked = missing > 0 ? 'checked' : '';
        html += `<label><input type="checkbox" value="${escapeAttr(id)}" ${checked} onchange="_onFillEntityCheckChange()"> ${escapeHtml(name)}${tag}</label>`;
    });
    document.getElementById('ff-entity-checks').innerHTML = html;
    _onFillEntityCheckChange();
}

function _countMissingFields(editorEl, type) {
    const schema = _FILL_FIELD_SCHEMA[type];
    let count = 0;
    for (const f of schema.fields) {
        const cls = (schema.fieldClassMap || {})[f];
        if (cls) {
            const el = editorEl.querySelector('.' + cls);
            if (el && !el.value.trim()) count++;
        } else if (f === 'schedule') {
            const rows = editorEl.querySelectorAll('.npc-schedule-row');
            const hasData = Array.from(rows).some(r => r.querySelector('.w-sched-loc')?.value.trim());
            if (!hasData) count++;
        } else if (f === 'opening_choices') {
            const rows = editorEl.querySelectorAll('.preset-choice-row');
            if (!rows.length) count++;
        } else if (f === 'hierarchy') {
            const rows = editorEl.querySelectorAll('.w-org-rank-row');
            const hasData = Array.from(rows).some(r => r.querySelector('.w-org-rank-title')?.value.trim());
            if (!hasData) count++;
        }
    }
    return count;
}

function _ffToggleAll(selectAll) {
    const boxes = document.querySelectorAll('#ff-entity-checks input[type="checkbox"]');
    boxes.forEach(cb => { cb.checked = selectAll; });
    _onFillEntityCheckChange();
}

function _onFillEntityCheckChange() {
    const type = document.getElementById('ff-entity-type').value;
    const fieldsWrap = document.getElementById('ff-fields-wrap');
    const checked = Array.from(document.querySelectorAll('#ff-entity-checks input:checked'));

    if (type === 'player') {
        fieldsWrap.style.display = '';
        _populateFieldChecks(type, [null]);
        return;
    }

    if (!checked.length) { fieldsWrap.style.display = 'none'; return; }
    fieldsWrap.style.display = '';

    const schema = _FILL_FIELD_SCHEMA[type];

    if (type === 'story_tree_node') {
        _populateFieldChecks(type, [null]);
        return;
    }

    const container = document.getElementById(schema.listId);
    const allEditors = container.querySelectorAll('.' + schema.editorClass);
    const ids = new Set(checked.map(cb => cb.value));
    const editorEls = [];
    allEditors.forEach(ed => {
        if (ids.has(ed.querySelector('.' + schema.idClass)?.value)) editorEls.push(ed);
    });
    _populateFieldChecks(type, editorEls);
}

function _populateFieldChecks(type, editorEls) {
    const schema = _FILL_FIELD_SCHEMA[type];
    const checksEl = document.getElementById('ff-fields-checks');
    let html = '';

    for (const f of schema.fields) {
        let isEmpty = false;
        if (type === 'player') {
            const idMap = {bio:'w-pc-bio',personality:'w-pc-personality',portrait_desc:'w-pc-portrait',long_term_goal:'w-pc-goal',initial_location:'w-pc-location'};
            const el = document.getElementById(idMap[f]);
            if (!el || !el.value.trim()) isEmpty = true;
        } else if (type === 'story_tree_node') {
            isEmpty = true;
        } else {
            for (const editorEl of editorEls) {
                if (!editorEl) { isEmpty = true; break; }
                const cls = (schema.fieldClassMap || {})[f];
                if (cls) {
                    const el = editorEl.querySelector('.' + cls);
                    if (!el || !el.value.trim()) { isEmpty = true; break; }
                } else if (f === 'schedule') {
                    const rows = editorEl.querySelectorAll('.npc-schedule-row');
                    if (!Array.from(rows).some(r => r.querySelector('.w-sched-loc')?.value.trim())) { isEmpty = true; break; }
                } else if (f === 'opening_choices') {
                    if (!editorEl.querySelectorAll('.preset-choice-row').length) { isEmpty = true; break; }
                } else if (f === 'hierarchy') {
                    const rows = editorEl.querySelectorAll('.w-org-rank-row');
                    if (!Array.from(rows).some(r => r.querySelector('.w-org-rank-title')?.value.trim())) { isEmpty = true; break; }
                }
            }
        }
        const label = schema.labels[f] || f;
        const tag = isEmpty ? ' <span style="color:var(--warning);font-size:0.7rem">(空)</span>' : '';
        html += `<label><input type="checkbox" value="${f}" ${isEmpty ? 'checked' : ''}> ${escapeHtml(label)}${tag}</label>`;
    }
    checksEl.innerHTML = html;
}

async function _runFillFields() {
    const type = document.getElementById('ff-entity-type').value;
    const checks = document.querySelectorAll('#ff-fields-checks input:checked');
    const fields = Array.from(checks).map(c => c.value);

    let entityIds;
    if (type === 'player') {
        entityIds = ['main'];
    } else {
        const entityChecks = document.querySelectorAll('#ff-entity-checks input:checked');
        entityIds = Array.from(entityChecks).map(cb => cb.value);
    }

    if (!type || fields.length === 0 || entityIds.length === 0) {
        if (typeof showNotification === 'function') showNotification('请选择实体和要补全的字段', 'warning');
        return;
    }

    const script = await buildScriptFromWizard();
    const schema = _FILL_FIELD_SCHEMA[type];
    const tab = schema.tab;
    const snapshot = _snapshotTab(tab);

    const btn = document.getElementById('ff-run-btn');
    if (btn) { btn.disabled = true; btn.textContent = `生成中 (0/${entityIds.length})...`; }

    try {
        const hint = document.getElementById('ff-hint')?.value || '';
        let doneCount = 0;
        const promises = entityIds.map(eid =>
            API.post('/api/scripts/ai-generate/fields', {
                script, entity_type: type, entity_id: eid, fields, user_hint: hint,
            }).then(r => {
                return { entityId: eid, generated: r.generated || {} };
            }).finally(() => {
                doneCount++;
                if (btn) btn.textContent = `生成中 (${doneCount}/${entityIds.length})...`;
            })
        );
        const settled = await Promise.allSettled(promises);
        const results = [];
        let failCount = 0;
        for (const s of settled) {
            if (s.status === 'fulfilled') results.push(s.value);
            else failCount++;
        }
        if (failCount > 0 && typeof showNotification === 'function') {
            showNotification(`${failCount} 个实体补全失败`, 'warning');
        }

        const allEmpty = results.every(r => Object.keys(r.generated).length === 0);
        if (allEmpty) {
            if (typeof showNotification === 'function') showNotification('AI未生成任何内容', 'info');
            return;
        }

        let wrapped;
        if (type === 'npc') {
            wrapped = { npcs: results.filter(r => Object.keys(r.generated).length).map(r => ({ id: r.entityId, ...r.generated })) };
        } else if (type === 'preset') {
            wrapped = { player_presets: results.filter(r => Object.keys(r.generated).length).map(r => ({ id: r.entityId, ...r.generated })) };
        } else if (type === 'player') {
            wrapped = { player_character: results[0].generated };
        } else if (type === 'location') {
            wrapped = { locations: results.filter(r => Object.keys(r.generated).length).map(r => {
                const gen = r.generated;
                if (gen.connections && typeof gen.connections === 'string') gen.connections = gen.connections.split(',').map(s => s.trim());
                return { id: r.entityId, ...gen };
            }) };
        } else if (type === 'organization') {
            wrapped = { organizations: results.filter(r => Object.keys(r.generated).length).map(r => {
                const gen = r.generated;
                if (gen.aliases && typeof gen.aliases === 'string') gen.aliases = gen.aliases.split(',').map(s => s.trim());
                return { id: r.entityId, ...gen };
            }) };
        } else if (type === 'one_time_event') {
            wrapped = { one_time_events: results.filter(r => Object.keys(r.generated).length).map(r => ({ id: r.entityId, ...r.generated })) };
        } else if (type === 'cyclic_event') {
            wrapped = { cyclic_events: results.filter(r => Object.keys(r.generated).length).map(r => ({ id: r.entityId, ...r.generated })) };
        } else if (type === 'random_item') {
            wrapped = { random_items: results.filter(r => Object.keys(r.generated).length).map(r => ({ id: r.entityId, ...r.generated })) };
        } else if (type === 'story_tree_node') {
            const treesMap = {};
            for (const r of results.filter(r => Object.keys(r.generated).length)) {
                const [treeId, nodeId] = r.entityId.split('/');
                if (!treesMap[treeId]) treesMap[treeId] = { id: treeId, nodes: [] };
                treesMap[treeId].nodes.push({ id: nodeId, ...r.generated });
            }
            wrapped = { trees: Object.values(treesMap) };
        }

        const mergeInfo = _smartMergeTab(tab, wrapped);
        _switchCopilotMode('result');
        _renderMergeSummary(tab, mergeInfo, snapshot);

        const totalChanges = mergeInfo.added.length + mergeInfo.updated.length;
        if (typeof showNotification === 'function') {
            showNotification(`字段补全完成: ${totalChanges}项已更新`, 'success');
        }
    } catch (e) {
        if (typeof showNotification === 'function') {
            showNotification(`字段补全失败: ${e.message}`, 'error');
        }
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'AI 补全'; }
    }
}

async function aiOptimizeEventSystem() {
    const script = await buildScriptFromWizard();
    if (!script.script_name && !script.world_background) {
        alert('请先填写基本信息（剧本名称/世界背景）');
        return;
    }

    const btn = document.getElementById('btn-ai-optimize-events');
    const hintInput = document.querySelector('#step-story_tree .ai-hint-input');
    const userHint = hintInput ? hintInput.value.trim() : '';

    const wizardMain = document.querySelector('.wizard-main');
    const overlay = document.createElement('div');
    overlay.className = 'wizard-gen-overlay';
    overlay.innerHTML = '<div class="overlay-spinner"></div><div class="overlay-text">AI 正在分析事件系统...</div>';
    if (wizardMain) wizardMain.appendChild(overlay);
    if (btn) { btn.disabled = true; btn._origText = btn.textContent; btn.textContent = '优化中...'; }

    try {
        const result = await API.post('/api/scripts/ai-generate/optimize-events', {
            script, user_hint: userHint,
        });

        if (!result.generated || !Object.keys(result.generated).length) {
            showNotification('AI认为当前事件系统已足够完善', 'info');
            return;
        }

        const data = result.generated;

        _ensureTabLoaded('story_tree');
        _ensureTabLoaded('events');

        const container = document.getElementById('w-story-trees');
        const treeIdMap = _buildEditorIdMap(container, 'w-st-id');
        let addedCount = 0;

        if (data.trees && data.trees.length) {
            for (const tree of data.trees) {
                if (!treeIdMap.has(tree.id)) {
                    addStoryTreeEditor(tree);
                    addedCount++;
                }
            }
        }

        if (data.new_nodes_for_existing_trees) {
            for (const entry of data.new_nodes_for_existing_trees) {
                const treeEl = treeIdMap.get(entry.tree_id);
                if (!treeEl) continue;
                const nodesContainer = treeEl.querySelector('.story-nodes-container');
                if (!nodesContainer) continue;
                const existingNodeMap = new Map();
                nodesContainer.querySelectorAll('.story-node-editor').forEach(nel => {
                    const nid = nel.querySelector('.w-stn-id')?.value;
                    if (nid) existingNodeMap.set(nid, nel);
                });
                for (const node of (entry.nodes || [])) {
                    if (!existingNodeMap.has(node.id)) {
                        addStoryNodeEditor(node, nodesContainer);
                        addedCount++;
                    }
                }
            }
        }

        if (data.one_time_events && data.one_time_events.length) {
            const otContainer = document.getElementById('w-onetime-events');
            const otIdMap = _buildEditorIdMap(otContainer, 'w-ote-id');
            for (const evt of data.one_time_events) {
                if (!otIdMap.has(evt.id)) {
                    addOnetimeEventEditor(evt);
                    addedCount++;
                }
            }
        }

        if (data.cyclic_events && data.cyclic_events.length) {
            const ceContainer = document.getElementById('w-cyclic-events');
            const ceIdMap = _buildEditorIdMap(ceContainer, 'w-ce-id');
            for (const evt of data.cyclic_events) {
                if (!ceIdMap.has(evt.id)) {
                    addCyclicEventEditor(evt);
                    addedCount++;
                }
            }
        }

        if (typeof stIsGraphMode === 'function' && stIsGraphMode() && typeof stLoadFromScript === 'function') {
            const updatedScript = await buildScriptFromWizard();
            stLoadFromScript(updatedScript);
        }

        showNotification(`事件系统优化完成: 新增 ${addedCount} 项`, 'success');
    } catch (e) {
        showNotification(`事件系统优化失败: ${e.message}`, 'error');
    } finally {
        if (overlay.parentNode) overlay.remove();
        if (btn) { btn.disabled = false; btn.textContent = btn._origText || 'AI优化事件'; }
    }
}

