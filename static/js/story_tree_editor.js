/* Story Tree Graph Editor — unified canvas for all trees + events */

let _stEditorTrees = [];
let _stEditorEvents = [];   // {_kind:'one_time'|'cyclic', id, ...original fields, _editor_x, _editor_y}
let _stSelectedNodeId = null;
let _stSelectedKind = null;  // 'tree' | 'event'
let _stGraphMode = true;
let _stZoom = 1;
let _stPanX = 0, _stPanY = 0;

const _ST_NODE_W = 160;
const _ST_NODE_H = 62;
const _ST_GROUP_PAD = 24;
const _ST_GROUP_HEADER = 28;
const _ST_TYPE_ICONS = {
    auto: '\u{1F4D6}', choice: '⚔️', quest: '❗',
    timed: '⏳', trigger: '⚡', periodic: '\u{1F504}',
    one_time: '\u{1F4C5}', cyclic: '\u{1F504}',
};
const _ST_TYPE_NAMES = {
    auto: '自动', choice: '选择', quest: '任务',
    timed: '限时', trigger: '触发', periodic: '周期',
};

// ---- init ----
function stInit() {
    const wrap = document.getElementById('st-canvas-wrap');
    if (!wrap) return;
    wrap.addEventListener('wheel', _stOnWheel, { passive: false });
    wrap.addEventListener('mousedown', _stOnCanvasMouseDown);
    document.addEventListener('keydown', _stOnKeyDown);
    document.getElementById('st-form-wrapper')?.classList.add('st-hidden');
}

// ---- load from script ----
function stLoadFromScript(script) {
    const trees = script?.story_tree?.trees || [];
    _stEditorTrees = JSON.parse(JSON.stringify(trees));

    _stEditorEvents = [];
    for (const e of (script?.one_time_events || [])) {
        _stEditorEvents.push({ ...JSON.parse(JSON.stringify(e)), _kind: 'one_time' });
    }
    for (const e of (script?.cyclic_events || [])) {
        _stEditorEvents.push({ ...JSON.parse(JSON.stringify(e)), _kind: 'cyclic' });
    }

    _stSelectedNodeId = null;
    _stSelectedKind = null;
    _stLayoutAll();
    _stRenderCanvas();
    _stClearProps();
    setTimeout(_stFitView, 50);
}

function stIsGraphMode() { return _stGraphMode; }

function getStoryTreeFromEditor() {
    if (_stEditorTrees.length === 0) return undefined;
    const clean = JSON.parse(JSON.stringify(_stEditorTrees));
    for (const tree of clean) {
        for (const node of (tree.nodes || [])) {
            delete node._editor_x; delete node._editor_y;
        }
    }
    return { trees: clean };
}

function getEventsFromEditor() {
    const ot = [], ce = [];
    for (const e of _stEditorEvents) {
        const c = JSON.parse(JSON.stringify(e));
        const kind = c._kind; delete c._kind; delete c._editor_x; delete c._editor_y;
        if (kind === 'one_time') ot.push(c); else ce.push(c);
    }
    return { one_time_events: ot, cyclic_events: ce };
}

// ---- global node map ----
function _stAllNodes() {
    const all = [];
    for (const t of _stEditorTrees) {
        for (const n of (t.nodes || [])) all.push({ node: n, kind: 'tree', treeId: t.id });
    }
    for (const e of _stEditorEvents) all.push({ node: e, kind: 'event' });
    return all;
}

function _stFindNode(id) {
    for (const t of _stEditorTrees) {
        const n = t.nodes?.find(n => n.id === id);
        if (n) return { node: n, kind: 'tree', tree: t };
    }
    const e = _stEditorEvents.find(e => e.id === id);
    if (e) return { node: e, kind: 'event' };
    return null;
}

// ---- auto layout ----
function _stLayoutAll() {
    let groupX = 40;
    const topY = 40;
    for (const tree of _stEditorTrees) {
        const nodes = tree.nodes || [];
        if (nodes.length === 0) { tree._groupX = groupX; tree._groupY = topY; groupX += 240; continue; }
        const hasPos = nodes.some(n => n._editor_x != null);
        if (!hasPos) _stAutoLayoutTree(tree, groupX, topY + _ST_GROUP_HEADER);
        tree._groupX = groupX;
        tree._groupY = topY;
        let maxX = 0;
        for (const n of nodes) maxX = Math.max(maxX, (n._editor_x || 0) + _ST_NODE_W);
        groupX = maxX + 100;
    }

    const events = _stEditorEvents;
    if (events.length > 0) {
        const hasPos = events.some(e => e._editor_x != null);
        if (!hasPos) {
            let maxTreeY = 200;
            for (const tree of _stEditorTrees) {
                for (const n of (tree.nodes || [])) {
                    maxTreeY = Math.max(maxTreeY, (n._editor_y || 0) + _ST_NODE_H);
                }
            }
            const evtTopY = maxTreeY + 80;
            const otEvents = events.filter(e => e._kind === 'one_time');
            const cyEvents = events.filter(e => e._kind === 'cyclic');

            const layoutGroup = (group, startX, startY) => {
                const cols = Math.max(1, Math.min(Math.ceil(Math.sqrt(group.length)), Math.ceil(group.length / 2)));
                group.forEach((e, i) => {
                    e._editor_x = startX + (i % cols) * (_ST_NODE_W + 30);
                    e._editor_y = startY + Math.floor(i / cols) * 90;
                });
                const rows = Math.ceil(group.length / cols);
                return { width: cols * (_ST_NODE_W + 30), height: rows * 90 };
            };

            let ex = 40;
            if (otEvents.length > 0) {
                const info = layoutGroup(otEvents, ex, evtTopY + _ST_GROUP_HEADER);
                ex += info.width + 80;
            }
            if (cyEvents.length > 0) {
                layoutGroup(cyEvents, ex, evtTopY + _ST_GROUP_HEADER);
            }
        }
    }
}

function _stAutoLayoutTree(tree, offsetX, offsetY) {
    const nodes = tree.nodes || [];
    if (nodes.length === 0) return;

    const childMap = {};
    const parentMap = {};
    for (const node of nodes) {
        childMap[node.id] = [];
        parentMap[node.id] = parentMap[node.id] || [];
    }
    for (const node of nodes) {
        for (const req of (node.requires || [])) {
            if (childMap[req]) { childMap[req].push(node.id); (parentMap[node.id] ||= []).push(req); }
        }
        for (const uid of (node.on_complete_unlock || [])) {
            if (childMap[node.id]) childMap[node.id].push(uid);
            if (parentMap[uid]) parentMap[uid].push(node.id);
        }
    }
    for (const k in childMap) childMap[k] = [...new Set(childMap[k])];
    for (const k in parentMap) parentMap[k] = [...new Set(parentMap[k])];

    const nodeSet = new Set(nodes.map(n => n.id));
    let roots = nodes.filter(n => (parentMap[n.id] || []).length === 0);
    if (roots.length === 0) roots = [nodes[0]];

    const rowOf = {};
    const visited = new Set();
    const queue = roots.map(r => ({ id: r.id, row: 0 }));
    for (const r of queue) visited.add(r.id);
    while (queue.length > 0) {
        const { id, row } = queue.shift();
        rowOf[id] = Math.max(rowOf[id] || 0, row);
        for (const cid of (childMap[id] || [])) {
            if (!visited.has(cid) && nodeSet.has(cid)) {
                visited.add(cid);
                queue.push({ id: cid, row: row + 1 });
            }
        }
    }
    for (const n of nodes) { if (rowOf[n.id] == null) rowOf[n.id] = 0; }

    const maxRow = Math.max(...Object.values(rowOf));
    const subtreeW = {};
    for (let r = maxRow; r >= 0; r--) {
        for (const n of nodes) {
            if (rowOf[n.id] !== r) continue;
            const children = (childMap[n.id] || []).filter(c => nodeSet.has(c));
            subtreeW[n.id] = children.length === 0 ? 1 : children.reduce((s, c) => s + (subtreeW[c] || 1), 0);
        }
    }

    const colOf = {};
    let colCursor = 0;
    const assignCols = (nid, startCol) => {
        const w = subtreeW[nid] || 1;
        colOf[nid] = startCol + w / 2 - 0.5;
        const children = (childMap[nid] || []).filter(c => nodeSet.has(c));
        let cursor = startCol;
        for (const cid of children) {
            assignCols(cid, cursor);
            cursor += (subtreeW[cid] || 1);
        }
    };
    for (const root of roots) {
        assignCols(root.id, colCursor);
        colCursor += (subtreeW[root.id] || 1) + 1;
    }

    const GAP_X = _ST_NODE_W + 40, GAP_Y = 90;
    for (const node of nodes) {
        const col = colOf[node.id];
        const row = rowOf[node.id];
        if (col != null) {
            node._editor_x = offsetX + _ST_GROUP_PAD + col * GAP_X;
            node._editor_y = offsetY + _ST_GROUP_PAD + row * GAP_Y;
        } else {
            node._editor_x = offsetX + _ST_GROUP_PAD;
            node._editor_y = offsetY + _ST_GROUP_PAD + nodes.indexOf(node) * GAP_Y;
        }
    }
}

// ---- group bounding box ----
function _stGroupBounds(items) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of items) {
        const x = n._editor_x || 0, y = n._editor_y || 0;
        minX = Math.min(minX, x); minY = Math.min(minY, y);
        maxX = Math.max(maxX, x + _ST_NODE_W); maxY = Math.max(maxY, y + _ST_NODE_H);
    }
    return {
        x: minX - _ST_GROUP_PAD, y: minY - _ST_GROUP_PAD - _ST_GROUP_HEADER,
        w: maxX - minX + _ST_GROUP_PAD * 2, h: maxY - minY + _ST_GROUP_PAD * 2 + _ST_GROUP_HEADER,
    };
}

// ---- render ----
function _stRenderCanvas() {
    const canvas = document.getElementById('st-canvas');
    if (!canvas) return;
    canvas.innerHTML = '';
    canvas.style.transform = `translate(${_stPanX}px, ${_stPanY}px) scale(${_stZoom})`;

    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
    const defs = document.createElementNS(svgNS, 'defs');
    [['arr-req','#999'],['arr-unl','#6496ff'],['arr-fire','#e8a838'],['arr-act','#5ec46a']].forEach(([id,c]) => {
        const m = document.createElementNS(svgNS, 'marker');
        m.setAttribute('id', id); m.setAttribute('viewBox', '0 0 10 10');
        m.setAttribute('refX', '9'); m.setAttribute('refY', '5');
        m.setAttribute('markerWidth', '7'); m.setAttribute('markerHeight', '7');
        m.setAttribute('orient', 'auto');
        const a = document.createElementNS(svgNS, 'path');
        a.setAttribute('d', 'M0,1 L8,5 L0,9 Z'); a.setAttribute('fill', c);
        m.appendChild(a); defs.appendChild(m);
    });
    svg.appendChild(defs);
    canvas.appendChild(svg);

    const _esc = typeof escapeHtml === 'function' ? escapeHtml : (s => s);
    const allEntries = _stAllNodes();
    const nodeMap = {};
    for (const entry of allEntries) nodeMap[entry.node.id] = entry.node;

    // ---- group boxes ----
    const groupColors = ['rgba(100,150,255,0.08)','rgba(255,180,100,0.08)','rgba(100,255,150,0.08)','rgba(255,100,200,0.08)','rgba(200,200,100,0.08)'];
    const borderColors = ['rgba(100,150,255,0.35)','rgba(255,180,100,0.35)','rgba(100,255,150,0.35)','rgba(255,100,200,0.35)','rgba(200,200,100,0.35)'];
    _stEditorTrees.forEach((tree, ti) => {
        const nodes = tree.nodes || [];
        const b = nodes.length > 0 ? _stGroupBounds(nodes)
            : { x: (tree._groupX||40)-_ST_GROUP_PAD, y: (tree._groupY||40)-_ST_GROUP_PAD, w: 200, h: 80 };
        const box = document.createElement('div');
        box.className = 'st-group-box';
        box.style.cssText = `left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;background:${groupColors[ti%groupColors.length]};border-color:${borderColors[ti%borderColors.length]}`;
        box.innerHTML = `<div class="st-group-label">${_esc(tree.name||tree.id)}</div>`;
        box.querySelector('.st-group-label').addEventListener('mousedown', (ev) => _stGroupDragStart(ev, 'tree', ti));
        canvas.appendChild(box);
    });
    if (_stEditorEvents.length > 0) {
        const otEvts = _stEditorEvents.filter(e => e._kind === 'one_time');
        const cyEvts = _stEditorEvents.filter(e => e._kind === 'cyclic');
        const drawEvtGroup = (items, label, bg, border) => {
            if (items.length === 0) return;
            const b = _stGroupBounds(items);
            const box = document.createElement('div');
            box.className = 'st-group-box';
            box.style.cssText = `left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;background:${bg};border-color:${border}`;
            box.innerHTML = `<div class="st-group-label">${label}</div>`;
            box.querySelector('.st-group-label').addEventListener('mousedown', (ev) => _stGroupDragStart(ev, 'events', -1));
            canvas.appendChild(box);
        };
        drawEvtGroup(otEvts, '\u{1F4C5} 一次性事件', 'rgba(255,220,100,0.08)', 'rgba(255,220,100,0.35)');
        drawEvtGroup(cyEvts, '\u{1F504} 周期事件', 'rgba(180,220,255,0.08)', 'rgba(180,220,255,0.35)');
    }

    // ---- edges: intra-tree (requires / unlock) ----
    const edgeSet = new Set();
    const drawEdge = (fromN, toN, type) => {
        const key = `${fromN.id}->${toN.id}`;
        if (edgeSet.has(key)) return;
        edgeSet.add(key);
        const x1 = (fromN._editor_x||0)+_ST_NODE_W/2, y1 = (fromN._editor_y||0)+_ST_NODE_H;
        const x2 = (toN._editor_x||0)+_ST_NODE_W/2, y2 = (toN._editor_y||0);
        const midY = (y1+y2)/2;
        const path = document.createElementNS(svgNS, 'path');
        path.setAttribute('d', `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke-width', '2');
        if (type === 'fire_link') {
            path.setAttribute('stroke', '#e8a838');
            path.setAttribute('stroke-dasharray', '8,4');
            path.setAttribute('marker-end', 'url(#arr-fire)');
        } else if (type === 'activate_link') {
            path.setAttribute('stroke', '#5ec46a');
            path.setAttribute('stroke-dasharray', '4,4');
            path.setAttribute('marker-end', 'url(#arr-act)');
        } else if (type === 'unlock') {
            path.setAttribute('stroke', '#6496ff');
            path.setAttribute('stroke-dasharray', '6,3');
            path.setAttribute('marker-end', 'url(#arr-unl)');
        } else {
            path.setAttribute('stroke', '#999');
            path.setAttribute('marker-end', 'url(#arr-req)');
        }
        path.classList.add('st-edge');
        path.dataset.from = fromN.id; path.dataset.to = toN.id; path.dataset.etype = type;
        path.style.pointerEvents = 'stroke';
        const _ei = { from: fromN.id, to: toN.id, type };
        path.addEventListener('mousedown', (ev) => ev.stopPropagation());
        path.addEventListener('contextmenu', (ev) => _stEdgeContextMenu(ev, _ei));
        path.addEventListener('click', (ev) => { ev.stopPropagation(); _stEdgeContextMenu(ev, _ei); });
        svg.appendChild(path);
    };

    for (const tree of _stEditorTrees) {
        const tnodes = tree.nodes || [];
        const tmap = {}; tnodes.forEach(n => tmap[n.id] = n);
        for (const n of tnodes) {
            for (const uid of (n.on_complete_unlock||[])) { if (tmap[uid]) drawEdge(n, tmap[uid], 'unlock'); }
            for (const req of (n.requires||[])) { if (tmap[req]) drawEdge(tmap[req], n, 'requires'); }
        }
    }

    // ---- edges: cross-group via fire_events / activate_events ----
    const activateMap = {};  // event_name → [nodeObj]
    for (const entry of allEntries) {
        const ae = entry.node.activate_events || [];
        for (const evName of ae) (activateMap[evName] ||= []).push(entry.node);
    }
    const fireMap = {};  // event_name → [nodeObj]
    for (const entry of allEntries) {
        const n = entry.node;
        const fe = [].concat(n.effects?.fire_events||[], n.fire_events||[]);
        for (const evName of fe) (fireMap[evName] ||= []).push(n);
    }
    // fire_events → activate_events  (orange dashed = fire_link)
    for (const entry of allEntries) {
        const n = entry.node;
        const fe = [].concat(n.effects?.fire_events||[], n.fire_events||[]);
        for (const evName of fe) {
            for (const target of (activateMap[evName]||[])) {
                if (target.id !== n.id) drawEdge(n, target, evName.startsWith('act_') ? 'activate_link' : 'fire_link');
            }
        }
    }

    // ---- nodes ----
    for (const entry of allEntries) {
        const node = entry.node;
        const el = document.createElement('div');
        const isEvent = entry.kind === 'event';
        el.className = 'st-node' + (node.id === _stSelectedNodeId ? ' st-node--selected' : '') + (isEvent ? ' st-node--event' : '');
        el.dataset.id = node.id;
        el.dataset.kind = entry.kind;
        el.style.left = (node._editor_x||0)+'px';
        el.style.top = (node._editor_y||0)+'px';

        let icon, typeBadge;
        if (isEvent) {
            icon = node._kind === 'one_time' ? '\u{1F4C5}' : '\u{1F504}';
            typeBadge = node._kind === 'one_time' ? '一次性' : '周期';
        } else {
            icon = _ST_TYPE_ICONS[node.type] || _ST_TYPE_ICONS.auto;
            typeBadge = _ST_TYPE_NAMES[node.type] || node.type || '自动';
        }

        el.innerHTML = `
            <div class="st-port st-port-in" data-node="${node.id}"></div>
            <div class="st-node-body">
                <div style="display:flex;align-items:center;gap:4px">
                    <span class="st-node-icon">${icon}</span>
                    <span class="st-node-name">${_esc(node.name || node.description || node.id)}</span>
                </div>
                <span class="st-node-type-badge">${typeBadge}</span>
            </div>
            <div class="st-port st-port-out" data-node="${node.id}"></div>
        `;

        el.addEventListener('mousedown', (ev) => _stNodeMouseDown(ev, node.id));
        el.querySelector('.st-port-out').addEventListener('mousedown', (ev) => _stPortMouseDown(ev, node.id, 'out'));
        el.querySelector('.st-port-in').addEventListener('mousedown', (ev) => _stPortMouseDown(ev, node.id, 'in'));
        canvas.appendChild(el);
    }
}

// ---- node drag ----
let _stDragNode = null, _stDragStart = null;
function _stNodeMouseDown(ev, nodeId) {
    if (ev.target.classList.contains('st-port')) return;
    if (ev.button !== 0) return;
    ev.stopPropagation();
    ev.preventDefault();
    const found = _stFindNode(nodeId);
    if (!found) return;
    _stSelectNode(nodeId);
    const node = found.node;
    _stDragNode = node;
    let didMove = false;
    _stDragStart = { mx: ev.clientX, my: ev.clientY, nx: node._editor_x||0, ny: node._editor_y||0 };
    const onMove = (me) => {
        const sdx = me.clientX - _stDragStart.mx;
        const sdy = me.clientY - _stDragStart.my;
        if (!didMove && Math.abs(sdx) < 3 && Math.abs(sdy) < 3) return;
        didMove = true;
        _stDragNode._editor_x = Math.max(0, _stDragStart.nx + sdx / _stZoom);
        _stDragNode._editor_y = Math.max(0, _stDragStart.ny + sdy / _stZoom);
        _stRenderCanvas();
    };
    const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        if (_stDragNode && didMove) {
            const f = _stFindNode(_stDragNode.id);
            if (f && f.kind === 'event') _stCheckEventDropIntoTree(_stDragNode);
        }
        _stDragNode = null;
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

// ---- port drag (connect) ----
function _stPortMouseDown(ev, nodeId, portType) {
    ev.stopPropagation(); ev.preventDefault();
    const canvas = document.getElementById('st-canvas');
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = canvas.querySelector('svg');
    const tempLine = document.createElementNS(svgNS, 'line');
    const found = _stFindNode(nodeId);
    if (!found) return;
    const node = found.node;
    const startX = (node._editor_x||0) + _ST_NODE_W / 2;
    const startY = portType === 'out' ? (node._editor_y||0) + _ST_NODE_H : (node._editor_y||0);
    tempLine.setAttribute('x1', startX); tempLine.setAttribute('y1', startY);
    tempLine.setAttribute('x2', startX); tempLine.setAttribute('y2', startY);
    tempLine.setAttribute('stroke', '#e8a838'); tempLine.setAttribute('stroke-width', '2');
    tempLine.setAttribute('stroke-dasharray', '4,4');
    tempLine.style.pointerEvents = 'none';
    svg.appendChild(tempLine);

    const wrapRect = document.getElementById('st-canvas-wrap').getBoundingClientRect();
    const onMove = (me) => {
        const mx = (me.clientX - wrapRect.left - _stPanX) / _stZoom;
        const my = (me.clientY - wrapRect.top - _stPanY) / _stZoom;
        tempLine.setAttribute('x2', mx); tempLine.setAttribute('y2', my);
    };
    const onUp = (me) => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        tempLine.remove();
        const target = document.elementFromPoint(me.clientX, me.clientY);
        if (target && target.classList.contains('st-port')) {
            const targetId = target.dataset.node;
            const targetPort = target.classList.contains('st-port-in') ? 'in' : 'out';
            if (targetId !== nodeId) {
                let fromId, toId;
                if (portType === 'out' && targetPort === 'in') { fromId = nodeId; toId = targetId; }
                else if (portType === 'in' && targetPort === 'out') { fromId = targetId; toId = nodeId; }
                if (fromId && toId) _stAddEdge(fromId, toId);
            }
        }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

// ---- edge management ----
function _stAddEdge(fromId, toId) {
    const fromFound = _stFindNode(fromId);
    const toFound = _stFindNode(toId);
    if (!fromFound || !toFound) return;

    const sameTree = fromFound.kind === 'tree' && toFound.kind === 'tree' && fromFound.tree === toFound.tree;
    if (sameTree) {
        const unlock = fromFound.node.on_complete_unlock || [];
        if (!unlock.includes(toId)) fromFound.node.on_complete_unlock = [...unlock, toId];
    } else {
        const evtName = `link_${fromId}_${toId}`;
        const fe = fromFound.node.fire_events || (fromFound.node.effects?.fire_events);
        if (fromFound.kind === 'event') {
            const arr = fromFound.node.fire_events || [];
            if (!arr.includes(evtName)) fromFound.node.fire_events = [...arr, evtName];
        } else {
            if (!fromFound.node.effects) fromFound.node.effects = {};
            const arr = fromFound.node.effects.fire_events || [];
            if (!arr.includes(evtName)) fromFound.node.effects.fire_events = [...arr, evtName];
        }
        const ae = toFound.node.activate_events || [];
        if (!ae.includes(evtName)) toFound.node.activate_events = [...ae, evtName];
    }
    _stRenderCanvas();
    _stRenderProps();
}

function _stRemoveEdge(fromId, toId, type) {
    const fromFound = _stFindNode(fromId);
    const toFound = _stFindNode(toId);
    if (type === 'fire_link' || type === 'activate_link' || type === 'event_link') {
        if (fromFound) {
            const n = fromFound.node;
            if (n.fire_events) n.fire_events = n.fire_events.filter(e => !_stEventLinksTo(e, toId, fromId));
            if (n.effects?.fire_events) n.effects.fire_events = n.effects.fire_events.filter(e => !_stEventLinksTo(e, toId, fromId));
        }
        if (toFound) {
            const n = toFound.node;
            if (n.activate_events) n.activate_events = n.activate_events.filter(e => !_stEventLinksTo(e, toId, fromId));
        }
    } else {
        if (fromFound?.node) fromFound.node.on_complete_unlock = (fromFound.node.on_complete_unlock||[]).filter(id => id !== toId);
        if (toFound?.node) toFound.node.requires = (toFound.node.requires||[]).filter(id => id !== fromId);
    }
    _stRenderCanvas();
    _stRenderProps();
}

function _stEventLinksTo(evtName, toId, fromId) {
    return evtName === `link_${fromId}_${toId}` || evtName === `act_${fromId}_${toId}`;
}

// ---- edge context menu ----
const _ST_EDGE_TYPES = [
    { type: 'requires', label: '前置依赖', color: '#999' },
    { type: 'unlock',   label: '完成解锁', color: '#6496ff' },
    { type: 'fire_link',label: '触发事件', color: '#e8a838' },
    { type: 'activate_link', label: '激活事件', color: '#5ec46a' },
];
function _stEdgeContextMenu(ev, edge) {
    ev.preventDefault(); ev.stopPropagation();
    _stCloseEdgeMenu();
    const menu = document.createElement('div');
    menu.className = 'st-edge-menu';
    menu.style.left = ev.clientX + 'px'; menu.style.top = ev.clientY + 'px';
    for (const t of _ST_EDGE_TYPES) {
        const btn = document.createElement('button');
        btn.innerHTML = `<span style="color:${t.color}">●</span> ${t.label}`;
        if (edge.type === t.type) btn.style.fontWeight = '700';
        btn.onclick = () => { _stChangeEdgeType(edge.from, edge.to, edge.type, t.type); _stCloseEdgeMenu(); };
        menu.appendChild(btn);
    }
    const sep = document.createElement('hr');
    sep.style.cssText = 'border:none;border-top:1px solid var(--border);margin:4px 0';
    menu.appendChild(sep);
    const delBtn = document.createElement('button');
    delBtn.textContent = '删除连线';
    delBtn.style.color = 'var(--danger,#e55)';
    delBtn.onclick = () => { _stRemoveEdge(edge.from, edge.to, edge.type); _stCloseEdgeMenu(); };
    menu.appendChild(delBtn);
    document.body.appendChild(menu);
    setTimeout(() => document.addEventListener('click', _stCloseEdgeMenu, { once: true }), 0);
}

function _stChangeEdgeType(fromId, toId, oldType, newType) {
    if (oldType === newType) return;
    const fromFound = _stFindNode(fromId);
    const toFound = _stFindNode(toId);
    if (!fromFound || !toFound) return;
    // remove old
    if (oldType === 'fire_link' || oldType === 'activate_link' || oldType === 'event_link') {
        const n = fromFound.node;
        if (n.fire_events) n.fire_events = n.fire_events.filter(e => !_stEventLinksTo(e, toId, fromId));
        if (n.effects?.fire_events) n.effects.fire_events = n.effects.fire_events.filter(e => !_stEventLinksTo(e, toId, fromId));
        if (toFound.node.activate_events) toFound.node.activate_events = toFound.node.activate_events.filter(e => !_stEventLinksTo(e, toId, fromId));
    } else if (oldType === 'unlock') {
        fromFound.node.on_complete_unlock = (fromFound.node.on_complete_unlock||[]).filter(id => id !== toId);
    } else if (oldType === 'requires') {
        toFound.node.requires = (toFound.node.requires||[]).filter(id => id !== fromId);
    }
    // add new
    if (newType === 'requires') {
        const req = toFound.node.requires || [];
        if (!req.includes(fromId)) toFound.node.requires = [...req, fromId];
    } else if (newType === 'unlock') {
        const unl = fromFound.node.on_complete_unlock || [];
        if (!unl.includes(toId)) fromFound.node.on_complete_unlock = [...unl, toId];
    } else if (newType === 'fire_link' || newType === 'activate_link') {
        const prefix = newType === 'activate_link' ? 'act' : 'link';
        const evtName = `${prefix}_${fromId}_${toId}`;
        if (fromFound.kind === 'event') {
            const arr = fromFound.node.fire_events || [];
            if (!arr.includes(evtName)) fromFound.node.fire_events = [...arr, evtName];
        } else {
            if (!fromFound.node.effects) fromFound.node.effects = {};
            const arr = fromFound.node.effects.fire_events || [];
            if (!arr.includes(evtName)) fromFound.node.effects.fire_events = [...arr, evtName];
        }
        const ae = toFound.node.activate_events || [];
        if (!ae.includes(evtName)) toFound.node.activate_events = [...ae, evtName];
    }
    _stRenderCanvas();
    _stRenderProps();
}
function _stCloseEdgeMenu() { document.querySelectorAll('.st-edge-menu').forEach(m => m.remove()); }

// ---- pan / zoom ----
function _stOnWheel(ev) {
    ev.preventDefault();
    const delta = ev.deltaY > 0 ? -0.08 : 0.08;
    _stZoom = Math.max(0.3, Math.min(2.0, _stZoom + delta));
    const slider = document.getElementById('st-zoom-slider');
    if (slider) slider.value = Math.round(_stZoom * 100);
    _stApplyTransform();
}
function stSetZoom(z) { _stZoom = Math.max(0.3, Math.min(2.0, z)); _stApplyTransform(); }
function _stApplyTransform() {
    const c = document.getElementById('st-canvas');
    if (c) c.style.transform = `translate(${_stPanX}px, ${_stPanY}px) scale(${_stZoom})`;
}

function _stFitView() {
    const all = _stAllNodes().map(e => e.node);
    if (all.length === 0) { _stPanX = 0; _stPanY = 0; _stZoom = 1; _stApplyTransform(); return; }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of all) {
        const x = n._editor_x||0, y = n._editor_y||0;
        minX = Math.min(minX, x - _ST_GROUP_PAD); minY = Math.min(minY, y - _ST_GROUP_PAD - _ST_GROUP_HEADER);
        maxX = Math.max(maxX, x + _ST_NODE_W + _ST_GROUP_PAD); maxY = Math.max(maxY, y + _ST_NODE_H + _ST_GROUP_PAD);
    }
    const wrap = document.getElementById('st-canvas-wrap');
    if (!wrap) return;
    const ww = wrap.clientWidth || 600, wh = wrap.clientHeight || 400;
    const zoom = Math.max(0.3, Math.min(1.2, Math.min(ww / (maxX-minX+40), wh / (maxY-minY+40))));
    _stZoom = zoom;
    _stPanX = ww/2 - ((minX+maxX)/2)*zoom;
    _stPanY = wh/2 - ((minY+maxY)/2)*zoom;
    const slider = document.getElementById('st-zoom-slider');
    if (slider) slider.value = Math.round(_stZoom * 100);
    _stApplyTransform();
}

let _stPanning = false;
function _stOnCanvasMouseDown(ev) {
    if (ev.target.closest('.st-node')) return;
    if (ev.button === 0 || ev.button === 1) {
        ev.preventDefault();
        _stPanning = true; let didMove = false;
        const wrap = document.getElementById('st-canvas-wrap');
        wrap.classList.add('st-panning');
        const sx = ev.clientX, sy = ev.clientY, spx = _stPanX, spy = _stPanY;
        const onMove = (me) => { didMove = true; _stPanX = spx + (me.clientX-sx); _stPanY = spy + (me.clientY-sy); _stApplyTransform(); };
        const onUp = () => { _stPanning = false; wrap.classList.remove('st-panning'); document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); if (!didMove && ev.button===0) _stSelectNode(null); };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }
}

// ---- group drag ----
function _stGroupDragStart(ev, groupType, groupIdx) {
    if (ev.button !== 0) return;
    ev.stopPropagation(); ev.preventDefault();
    const nodes = groupType === 'tree' ? (_stEditorTrees[groupIdx]?.nodes || []) : _stEditorEvents;
    const starts = nodes.map(n => ({ x: n._editor_x||0, y: n._editor_y||0 }));
    const sx = ev.clientX, sy = ev.clientY;
    let didMove = false;
    const wrap = document.getElementById('st-canvas-wrap');
    const onMove = (me) => {
        const sdx = me.clientX - sx, sdy = me.clientY - sy;
        if (!didMove && Math.abs(sdx) < 3 && Math.abs(sdy) < 3) return;
        didMove = true;
        wrap.classList.add('st-panning');
        const dx = sdx / _stZoom, dy = sdy / _stZoom;
        nodes.forEach((n, i) => { n._editor_x = starts[i].x + dx; n._editor_y = starts[i].y + dy; });
        _stRenderCanvas();
    };
    const onUp = () => {
        wrap.classList.remove('st-panning');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

// ---- select ----
function _stSelectNode(nodeId) {
    _stSelectedNodeId = nodeId;
    if (nodeId) {
        const found = _stFindNode(nodeId);
        _stSelectedKind = found?.kind || null;
    } else {
        _stSelectedKind = null;
    }
    document.querySelectorAll('.st-node').forEach(el => el.classList.toggle('st-node--selected', el.dataset.id === nodeId));
    if (nodeId) _stRenderProps(); else _stClearProps();
}

// ---- props panel ----
function _stClearProps() {
    const p = document.getElementById('st-props-placeholder');
    const c = document.getElementById('st-props-content');
    if (p) p.style.display = '';
    if (c) { c.style.display = 'none'; c.innerHTML = ''; }
}

function _stRenderProps() {
    const found = _stFindNode(_stSelectedNodeId);
    if (!found) { _stClearProps(); return; }
    const node = found.node;
    const isEvent = found.kind === 'event';

    document.getElementById('st-props-placeholder').style.display = 'none';
    const content = document.getElementById('st-props-content');
    content.style.display = ''; content.innerHTML = '';

    const _esc = typeof escapeHtml === 'function' ? escapeHtml : (s => s);

    if (isEvent) {
        _stRenderEventProps(content, node, _esc);
    } else {
        _stRenderTreeNodeProps(content, node, found.tree, _esc);
    }
}

function _stRenderEventProps(content, node, _esc) {
    const isOT = node._kind === 'one_time';
    const fireEvts = (node.fire_events || []).join(', ');
    content.innerHTML = `
        <label>事件ID</label><input type="text" id="stp-id" value="${_esc(node.id||'')}" />
        <label>名称</label><input type="text" id="stp-name" value="${_esc(node.name||'')}" />
        <label>描述</label><input type="text" id="stp-desc" value="${_esc(node.description||'')}" />
        <label>条件</label><input type="text" id="stp-condition" value="${_esc(node.condition||'')}" />
        ${isOT ? `<label>触发时间</label><input type="datetime-local" id="stp-trigger-time" value="${(node.trigger_time||'').substring(0,16)}" />` :
        `<label>频率</label><div style="display:flex;gap:4px"><input type="number" id="stp-freq-val" value="${node.frequency_value||1}" min="1" style="width:60px" />
        <select id="stp-freq-unit"><option value="day" ${node.frequency_unit==='day'?'selected':''}>天</option><option value="week" ${node.frequency_unit==='week'?'selected':''}>周</option><option value="month" ${node.frequency_unit==='month'?'selected':''}>月</option></select></div>
        <label>首次触发</label><input type="datetime-local" id="stp-first" value="${(node.first_trigger||'').substring(0,16)}" />
        <label>过期</label><input type="datetime-local" id="stp-expiry" value="${(node.expires_at||'').substring(0,16)}" />`}
        <label>触发事件 (fire_events)</label><input type="text" id="stp-fire-events" value="${_esc(fireEvts)}" placeholder="逗号分隔" />
        <label>激活事件 (activate_events)</label><input type="text" id="stp-activate-events" value="${_esc((node.activate_events||[]).join(', '))}" />
        <label>移入剧情线</label>
        <select id="stp-assign-tree"><option value="">-- 独立事件 --</option>${_stEditorTrees.map((t,i) => `<option value="${i}">${_esc(t.name||t.id)}</option>`).join('')}</select>
        <button onclick="stDeleteSelectedNode()" style="margin-top:12px;color:var(--danger)" class="btn-secondary">删除事件</button>
    `;

    const bind = (id, field, parse) => {
        const el = content.querySelector('#' + id);
        if (!el) return;
        const h = () => { node[field] = parse ? parse(el) : el.value; _stRenderCanvas(); };
        el.addEventListener('input', h); el.addEventListener('change', h);
    };
    bind('stp-id', 'id');
    bind('stp-name', 'name');
    bind('stp-desc', 'description');
    bind('stp-condition', 'condition');
    bind('stp-fire-events', 'fire_events', el => el.value.split(',').map(s=>s.trim()).filter(Boolean));
    bind('stp-activate-events', 'activate_events', el => el.value.split(',').map(s=>s.trim()).filter(Boolean));
    if (isOT) {
        bind('stp-trigger-time', 'trigger_time');
    } else {
        bind('stp-freq-val', 'frequency_value', el => parseInt(el.value)||1);
        bind('stp-freq-unit', 'frequency_unit');
        bind('stp-first', 'first_trigger');
        bind('stp-expiry', 'expires_at', el => el.value || null);
    }
    // ID rename
    content.querySelector('#stp-id')?.addEventListener('change', () => {
        const newId = content.querySelector('#stp-id').value.trim();
        if (newId && newId !== _stSelectedNodeId) { node.id = newId; _stSelectedNodeId = newId; _stRenderCanvas(); }
    });
    // Assign to tree
    const assignEl = content.querySelector('#stp-assign-tree');
    if (assignEl) assignEl.addEventListener('change', () => {
        const idx = parseInt(assignEl.value);
        if (!isNaN(idx) && idx >= 0 && idx < _stEditorTrees.length) _stConvertEventToNode(node, _stEditorTrees[idx]);
    });
}

function _stRenderTreeNodeProps(content, node, tree, _esc) {
    const requires = node.requires || [];
    const unlock = node.on_complete_unlock || [];
    const activateEvents = (node.activate_events||[]).join(', ');
    const eventVal = Array.isArray(node.event) ? node.event.join(', ') : (node.event||'');
    const effectsStr = node.effects ? JSON.stringify(node.effects, null, 2) : '';
    const choicesStr = node.choices ? JSON.stringify(node.choices, null, 2) : '';
    const typeOptions = ['auto','choice','quest','timed','trigger','periodic'].map(t =>
        `<option value="${t}" ${node.type===t?'selected':''}>${_ST_TYPE_NAMES[t]||t}</option>`).join('');
    const isTrigger = node.type==='trigger', isPeriodic = node.type==='periodic';

    content.innerHTML = `
        <label>节点ID</label><input type="text" id="stp-id" value="${_esc(node.id||'')}" />
        <label>名称</label><input type="text" id="stp-name" value="${_esc(node.name||'')}" />
        <label>类型</label><select id="stp-type">${typeOptions}</select>
        <label>所属剧情线</label><div class="st-props-readonly"><span class="st-props-tag">${_esc(tree?.name||tree?.id||'?')}</span></div>
        <label>描述</label><input type="text" id="stp-desc" value="${_esc(node.description||'')}" />
        <label>条件</label><input type="text" id="stp-condition" value="${_esc(node.condition||'')}" />
        <label>前置节点 (连线)</label><div class="st-props-readonly">${requires.length?requires.map(r=>`<span class="st-props-tag">${_esc(r)}</span>`).join(''):'<span style="color:var(--text-muted)">无</span>'}</div>
        <label>完成解锁 (连线)</label><div class="st-props-readonly">${unlock.length?unlock.map(u=>`<span class="st-props-tag">${_esc(u)}</span>`).join(''):'<span style="color:var(--text-muted)">无</span>'}</div>
        <div id="stp-timed-fields" style="display:${node.type==='timed'?'block':'none'}"><label>持续回合</label><input type="number" id="stp-duration" value="${node.duration_turns||''}" min="0" /></div>
        <div id="stp-trigger-fields" style="display:${isTrigger?'block':'none'}"><label>生命周期事件</label><input type="text" id="stp-event" value="${_esc(eventVal)}" placeholder="on_start, before_generation, after_ai" /></div>
        <div id="stp-periodic-fields" style="display:${isPeriodic?'block':'none'}">
            <label>冷却回合</label><input type="number" id="stp-cooldown" value="${node.cooldown||''}" min="0" />
            <label>权重</label><input type="number" id="stp-weight" value="${node.weight||''}" min="1" />
            <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="stp-repeatable" ${node.repeatable!==false?'checked':''} /> 可重复</label>
        </div>
        <label>激活事件 (逗号分隔)</label><input type="text" id="stp-activate-events" value="${_esc(activateEvents)}" />
        <label>关联NPC (逗号分隔)</label><input type="text" id="stp-related-npcs" value="${_esc((node.related_npcs||[]).join(', '))}" />
        <label>关联组织 (逗号分隔)</label><input type="text" id="stp-related-orgs" value="${_esc((node.related_orgs||[]).join(', '))}" />
        <label>效果 JSON</label><textarea id="stp-effects" rows="3">${_esc(effectsStr)}</textarea>
        <div id="stp-choice-fields" style="display:${node.type==='choice'||choicesStr?'block':'none'}"><label>选项 JSON</label><textarea id="stp-choices" rows="3">${_esc(choicesStr)}</textarea>
        <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="stp-auto-resolve" ${node.auto_resolve?'checked':''} /> AI自动选择(条件满足时自动触发)</label></div>
        <button onclick="stDeleteSelectedNode()" style="margin-top:12px;color:var(--danger)" class="btn-secondary">删除节点</button>
    `;

    const bind = (id, field, parse) => {
        const el = content.querySelector('#'+id); if (!el) return;
        const h = () => { node[field] = parse ? parse(el) : el.value; _stRenderCanvas(); };
        el.addEventListener('input', h); el.addEventListener('change', h);
    };
    bind('stp-name', 'name');
    bind('stp-desc', 'description');
    bind('stp-condition', 'condition');
    bind('stp-duration', 'duration_turns', el => parseInt(el.value)||undefined);
    bind('stp-cooldown', 'cooldown', el => parseInt(el.value)||undefined);
    bind('stp-weight', 'weight', el => parseInt(el.value)||undefined);
    bind('stp-repeatable', 'repeatable', el => el.checked);
    bind('stp-activate-events', 'activate_events', el => el.value.split(',').map(s=>s.trim()).filter(Boolean));
    bind('stp-related-npcs', 'related_npcs', el => el.value.split(',').map(s=>s.trim()).filter(Boolean));
    bind('stp-related-orgs', 'related_orgs', el => el.value.split(',').map(s=>s.trim()).filter(Boolean));
    bind('stp-event', 'event', el => el.value.split(',').map(s=>s.trim()).filter(Boolean));
    bind('stp-effects', 'effects', el => { try { return JSON.parse(el.value); } catch(_) { return undefined; } });
    bind('stp-choices', 'choices', el => { try { return JSON.parse(el.value); } catch(_) { return undefined; } });
    bind('stp-auto-resolve', 'auto_resolve', el => el.checked || undefined);

    const typeEl = content.querySelector('#stp-type');
    typeEl.addEventListener('change', () => {
        node.type = typeEl.value;
        content.querySelector('#stp-timed-fields').style.display = typeEl.value==='timed'?'block':'none';
        content.querySelector('#stp-trigger-fields').style.display = typeEl.value==='trigger'?'block':'none';
        content.querySelector('#stp-periodic-fields').style.display = typeEl.value==='periodic'?'block':'none';
        content.querySelector('#stp-choice-fields').style.display = typeEl.value==='choice'?'block':'none';
        _stRenderCanvas();
    });

    const idEl = content.querySelector('#stp-id');
    idEl.addEventListener('change', () => {
        const oldId = _stSelectedNodeId, newId = idEl.value.trim();
        if (!newId || newId === oldId) return;
        for (const t of _stEditorTrees) for (const n of (t.nodes||[])) {
            n.requires = (n.requires||[]).map(r => r===oldId?newId:r);
            n.on_complete_unlock = (n.on_complete_unlock||[]).map(r => r===oldId?newId:r);
        }
        node.id = newId; _stSelectedNodeId = newId; _stRenderCanvas();
    });
}

// ---- add / delete ----
function _stViewCenter() {
    const wr = document.getElementById('st-canvas-wrap')?.getBoundingClientRect();
    return {
        x: wr ? (wr.width/2 - _stPanX) / _stZoom : 300,
        y: wr ? (wr.height/2 - _stPanY) / _stZoom : 200,
    };
}

function stAddNode() {
    if (_stEditorTrees.length === 0) { stAddTree(); return; }
    const tree = _stEditorTrees[0]; // add to first tree by default
    const id = 'node_' + Date.now().toString(36);
    const c = _stViewCenter();
    tree.nodes.push({
        id, name: '新节点', type: 'auto', description: '', requires: [], on_complete_unlock: [],
        _editor_x: Math.max(0, c.x - _ST_NODE_W/2), _editor_y: Math.max(0, c.y - 30),
    });
    _stSelectedNodeId = id; _stSelectedKind = 'tree';
    _stRenderCanvas(); _stRenderProps();
}

function stAddEvent() {
    const id = 'evt_' + Date.now().toString(36);
    const c = _stViewCenter();
    _stEditorEvents.push({
        _kind: 'one_time', id, description: '新事件', trigger_time: '', condition: '',
        _editor_x: Math.max(0, c.x - _ST_NODE_W/2), _editor_y: Math.max(0, c.y - 30),
    });
    _stSelectedNodeId = id; _stSelectedKind = 'event';
    _stRenderCanvas(); _stRenderProps();
}

function stDeleteSelectedNode() {
    if (!_stSelectedNodeId) return;
    const nodeId = _stSelectedNodeId;
    const found = _stFindNode(nodeId);
    if (!found) return;

    if (found.kind === 'event') {
        _stEditorEvents = _stEditorEvents.filter(e => e.id !== nodeId);
    } else {
        for (const t of _stEditorTrees) {
            t.nodes = (t.nodes||[]).filter(n => n.id !== nodeId);
            for (const n of t.nodes) {
                n.requires = (n.requires||[]).filter(r => r !== nodeId);
                n.on_complete_unlock = (n.on_complete_unlock||[]).filter(r => r !== nodeId);
            }
        }
    }
    _stSelectedNodeId = null; _stSelectedKind = null;
    _stRenderCanvas(); _stClearProps();
}

function _stCheckEventDropIntoTree(eventNode) {
    const ex = (eventNode._editor_x||0) + _ST_NODE_W/2;
    const ey = (eventNode._editor_y||0) + _ST_NODE_H/2;
    for (let ti = 0; ti < _stEditorTrees.length; ti++) {
        const tree = _stEditorTrees[ti];
        const nodes = tree.nodes || [];
        const b = nodes.length > 0 ? _stGroupBounds(nodes)
            : { x: (tree._groupX||40)-_ST_GROUP_PAD, y: (tree._groupY||40)-_ST_GROUP_PAD, w: 200, h: 80 };
        if (ex >= b.x && ex <= b.x + b.w && ey >= b.y && ey <= b.y + b.h) {
            _stConvertEventToNode(eventNode, tree);
            return;
        }
    }
}

function _stConvertEventToNode(eventNode, tree) {
    _stEditorEvents = _stEditorEvents.filter(e => e.id !== eventNode.id);
    const node = {
        id: eventNode.id,
        name: eventNode.description || eventNode.id,
        description: eventNode.description || '',
        type: eventNode._kind === 'cyclic' ? 'periodic' : 'trigger',
        requires: [], on_complete_unlock: [],
        _editor_x: eventNode._editor_x, _editor_y: eventNode._editor_y,
    };
    if (eventNode.condition) node.condition = eventNode.condition;
    if (eventNode.activate_events?.length) node.activate_events = eventNode.activate_events;
    if (eventNode.fire_events?.length) node.effects = { fire_events: eventNode.fire_events };
    if (eventNode._kind === 'cyclic') {
        if (eventNode.frequency_value) node.cooldown = eventNode.frequency_value;
        node.repeatable = true;
    }
    if (!tree.nodes) tree.nodes = [];
    tree.nodes.push(node);
    _stSelectedNodeId = node.id; _stSelectedKind = 'tree';
    _stRenderCanvas(); _stRenderProps();
}

function _stOnKeyDown(ev) {
    if (!_stGraphMode) return;
    if (ev.key === 'Delete' && _stSelectedNodeId) {
        const a = document.activeElement;
        if (a && (a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.tagName==='SELECT')) return;
        stDeleteSelectedNode();
    }
}

// ---- tree management ----
function stAddTree() {
    const name = prompt('剧情线名称:');
    if (!name) return;
    const id = typeof toPinyin === 'function' ? toPinyin(name) : 'tree_' + Date.now().toString(36);
    _stEditorTrees.push({ id, name, icon: 'scroll', nodes: [] });
    _stSelectedNodeId = null;
    _stRenderCanvas(); _stClearProps();
}

function stDeleteTree() {
    if (_stEditorTrees.length === 0) return;
    const names = _stEditorTrees.map((t,i) => `${i+1}. ${t.name||t.id}`).join('\n');
    const idx = parseInt(prompt(`输入要删除的剧情线序号:\n${names}`)) - 1;
    if (isNaN(idx) || idx < 0 || idx >= _stEditorTrees.length) return;
    if (!confirm(`确认删除「${_stEditorTrees[idx].name}」？`)) return;
    _stEditorTrees.splice(idx, 1);
    _stSelectedNodeId = null;
    _stRenderCanvas(); _stClearProps();
}

// ---- mode toggle ----
function stToggleMode() {
    _stGraphMode = !_stGraphMode;
    const graphEl = document.getElementById('st-graph-editor');
    const formEl = document.getElementById('st-form-wrapper');
    const toggleBtn = document.getElementById('st-mode-toggle');
    const toolbar = document.getElementById('st-editor-toolbar');
    if (_stGraphMode) {
        graphEl.classList.remove('st-hidden');
        formEl.classList.add('st-hidden');
        toggleBtn.textContent = '表单模式';
        toolbar.querySelectorAll('.st-graph-only').forEach(el => el.style.display = '');
        _stSyncFormsToEditor();
        _stFitView();
    } else {
        graphEl.classList.add('st-hidden');
        formEl.classList.remove('st-hidden');
        toggleBtn.textContent = '图编辑器';
        toolbar.querySelectorAll('.st-graph-only').forEach(el => el.style.display = 'none');
        _stSyncEditorToForms();
    }
}

function _stSyncFormsToEditor() {
    const container = document.getElementById('w-story-trees');
    if (!container) return;
    const trees = [];
    container.querySelectorAll('.story-tree-editor').forEach(treeEl => {
        const treeName = treeEl.querySelector('.w-st-name')?.value?.trim();
        if (!treeName) return;
        const tree = {
            id: treeEl.querySelector('.w-st-id')?.value || '',
            name: treeName,
            description: treeEl.querySelector('.w-st-desc')?.value || '',
            icon: treeEl.querySelector('.w-st-icon')?.value || 'scroll',
            nodes: [],
        };
        treeEl.querySelectorAll('.story-node-editor').forEach(nodeEl => {
            const nodeName = nodeEl.querySelector('.w-stn-name')?.value?.trim();
            if (!nodeName) return;
            const node = {
                id: nodeEl.querySelector('.w-stn-id')?.value || '',
                name: nodeName,
                description: nodeEl.querySelector('.w-stn-desc')?.value || '',
                type: nodeEl.querySelector('.w-stn-type')?.value || 'auto',
                requires: (nodeEl.querySelector('.w-stn-requires')?.value||'').split(',').map(s=>s.trim()).filter(Boolean),
                on_complete_unlock: (nodeEl.querySelector('.w-stn-unlock')?.value||'').split(',').map(s=>s.trim()).filter(Boolean),
                condition: nodeEl.querySelector('.w-stn-condition')?.value || undefined,
            };
            const ae = (nodeEl.querySelector('.w-stn-activate-events')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
            if (ae.length) node.activate_events = ae;
            if (node.type === 'trigger') {
                const evts = (nodeEl.querySelector('.w-stn-event')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
                if (evts.length) node.event = evts;
            }
            if (node.type === 'periodic') {
                const cd = parseInt(nodeEl.querySelector('.w-stn-cooldown')?.value); if (cd>0) node.cooldown = cd;
                const wt = parseInt(nodeEl.querySelector('.w-stn-weight')?.value); if (wt>0) node.weight = wt;
                node.repeatable = nodeEl.querySelector('.w-stn-repeatable')?.checked !== false;
            }
            const eff = nodeEl.querySelector('.w-stn-effects')?.value?.trim();
            if (eff) { try { node.effects = JSON.parse(eff); } catch(_) {} }
            const ch = nodeEl.querySelector('.w-stn-choices')?.value?.trim();
            if (ch) { try { node.choices = JSON.parse(ch); } catch(_) {} }
            const dur = parseInt(nodeEl.querySelector('.w-stn-duration')?.value);
            if (dur>0) node.duration_turns = dur;
            tree.nodes.push(node);
        });
        trees.push(tree);
    });
    if (trees.length > 0) {
        const oldPos = {};
        for (const t of _stEditorTrees) for (const n of (t.nodes||[])) if (n._editor_x!=null) oldPos[n.id]={x:n._editor_x,y:n._editor_y};
        for (const t of trees) { for (const n of (t.nodes||[])) if (oldPos[n.id]) { n._editor_x=oldPos[n.id].x; n._editor_y=oldPos[n.id].y; } }
        _stEditorTrees = trees;
    }
    // Also sync events from form editors
    const evtOldPos = {};
    for (const e of _stEditorEvents) if (e._editor_x!=null) evtOldPos[e.id]={x:e._editor_x,y:e._editor_y};
    _stEditorEvents = [];
    document.querySelectorAll('#w-cyclic-events .ce-editor').forEach(el => {
        const desc = el.querySelector('.w-ce-desc')?.value;
        if (!desc) return;
        const id = el.querySelector('.w-ce-id')?.value || '';
        const fe = (el.querySelector('.w-ce-fire-events')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
        const evt = { _kind:'cyclic', id, name:el.querySelector('.w-ce-name')?.value||'', description:desc, frequency_value:parseInt(el.querySelector('.w-ce-freq-val')?.value)||1, frequency_unit:el.querySelector('.w-ce-freq-unit')?.value||'day', first_trigger:el.querySelector('.w-ce-first')?.value||'', condition:el.querySelector('.w-ce-condition')?.value||undefined, expires_at:el.querySelector('.w-ce-expiry')?.value||null };
        if (fe.length) evt.fire_events = fe;
        if (evtOldPos[id]) { evt._editor_x=evtOldPos[id].x; evt._editor_y=evtOldPos[id].y; }
        _stEditorEvents.push(evt);
    });
    document.querySelectorAll('#w-onetime-events .ote-editor').forEach(el => {
        const desc = el.querySelector('.w-ote-desc')?.value;
        if (!desc) return;
        const id = el.querySelector('.w-ote-id')?.value || '';
        const fe = (el.querySelector('.w-ote-fire-events')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
        const evt = { _kind:'one_time', id, name:el.querySelector('.w-ote-name')?.value||'', description:desc, trigger_time:el.querySelector('.w-ote-time')?.value||'', condition:el.querySelector('.w-ote-condition')?.value||undefined };
        if (fe.length) evt.fire_events = fe;
        if (evtOldPos[id]) { evt._editor_x=evtOldPos[id].x; evt._editor_y=evtOldPos[id].y; }
        _stEditorEvents.push(evt);
    });

    _stLayoutAll();
    _stRenderCanvas();
}

function _stSyncEditorToForms() {
    // Sync trees
    const container = document.getElementById('w-story-trees');
    if (container) {
        container.innerHTML = '';
        for (const tree of _stEditorTrees) {
            const ct = JSON.parse(JSON.stringify(tree));
            for (const n of (ct.nodes||[])) { delete n._editor_x; delete n._editor_y; }
            if (typeof addStoryTreeEditor === 'function') addStoryTreeEditor(ct);
        }
    }
    // Sync events back to form editors
    const ceContainer = document.getElementById('w-cyclic-events');
    const oteContainer = document.getElementById('w-onetime-events');
    if (ceContainer) ceContainer.innerHTML = '';
    if (oteContainer) oteContainer.innerHTML = '';
    for (const e of _stEditorEvents) {
        const c = JSON.parse(JSON.stringify(e));
        delete c._kind; delete c._editor_x; delete c._editor_y;
        if (e._kind === 'cyclic' && typeof addCyclicEventEditor === 'function') addCyclicEventEditor(c);
        if (e._kind === 'one_time' && typeof addOnetimeEventEditor === 'function') addOnetimeEventEditor(c);
    }
}

document.addEventListener('DOMContentLoaded', stInit);
