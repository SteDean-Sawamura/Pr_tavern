/* Story Tree visualization — unified graph with all trees + events */

let _storyTreeData = null;
let _storyEventData = { one_time: [], cyclic: [], threads: [], clues: [] };
let _stViewZoom = 1, _stViewPanX = 0, _stViewPanY = 0, _stViewCanvas = null;
let _stRecentNodeIds = new Set();

function markStoryTreeUpdates(updates) {
    _stRecentNodeIds = new Set();
    if (!updates) return;
    for (const n of (updates.newly_completed || [])) _stRecentNodeIds.add(n.id);
    for (const n of (updates.newly_active || [])) _stRecentNodeIds.add(n.id);
}

async function loadStoryTree() {
    if (document.getElementById('game-play-screen').classList.contains('map-view')) return;
    if (!currentSaveId) {
        document.getElementById('storytree-placeholder').style.display = '';
        document.getElementById('storytree-content').style.display = 'none';
        return;
    }
    try {
        const saveId = currentSaveId;
        const data = await API.get(`/api/game/${saveId}/story-tree`);
        if (saveId !== currentSaveId || document.getElementById('game-play-screen').classList.contains('map-view')) return;
        _storyTreeData = data.trees || [];
        _storyEventData = {
            one_time: data.one_time_events || [],
            cyclic: data.cyclic_events || [],
            threads: data.narrative_threads || [],
            clues: data.clue_board || [],
        };
        const hasContent = _storyTreeData.length > 0 || _storyEventData.one_time.length > 0
            || _storyEventData.cyclic.length > 0 || _storyEventData.threads.length > 0
            || _storyEventData.clues.length > 0;
        if (!hasContent) {
            document.getElementById('storytree-placeholder').textContent = '暂无已显现的剧情或公开事件。';
            document.getElementById('storytree-placeholder').style.display = '';
            document.getElementById('storytree-content').style.display = 'none';
            return;
        }
        document.getElementById('storytree-placeholder').style.display = 'none';
        document.getElementById('storytree-content').style.display = 'flex';
        renderStoryTreeGraph();
    } catch (e) {
        console.error('Failed to load story tree:', e);
    }
}

function _getTreeIcon(icon) {
    const icons = {
        crown: '\u{1F451}', sword: '⚔️', shield: '\u{1F6E1}️',
        scroll: '\u{1F4DC}', star: '⭐', skull: '\u{1F480}',
        eye: '\u{1F441}️', fire: '\u{1F525}', moon: '\u{1F319}',
        tree: '\u{1F333}', castle: '\u{1F3F0}', gem: '\u{1F48E}',
    };
    return icons[icon] || '\u{1F4D6}';
}

function renderStoryTreeGraph() {
    const graph = document.getElementById('storytree-graph');
    graph.innerHTML = '';
    const _esc = typeof escapeHtml === 'function' ? escapeHtml : (s => s);
    const _fmtTime = typeof formatGameTime === 'function' ? formatGameTime : (s => s || '');

    // Collect all nodes from all trees + virtual event nodes
    const allNodes = [];
    const nodesById = {};
    const nodeTreeInfo = {};

    for (const tree of (_storyTreeData || [])) {
        for (const node of tree.nodes) {
            const n = { ...node, _source: 'tree', _treeId: tree.id, _treeName: tree.name, _treeIcon: tree.icon };
            allNodes.push(n);
            nodesById[n.id] = n;
            nodeTreeInfo[n.id] = { name: tree.name, icon: tree.icon };
        }
    }

    // Virtual nodes from one-time events
    for (const ev of (_storyEventData.one_time || [])) {
        const vid = 'ot:' + ev.id;
        const n = {
            id: vid, _realId: ev.id, name: ev.name || ev.id, description: ev.description || '',
            status: ev.status, type: 'one_time_event', _source: 'ot',
            fire_events: ev.fire_events || [], activate_events: ev.activate_events || [],
            trigger_time: ev.trigger_time, condition: ev.condition,
        };
        allNodes.push(n);
        nodesById[vid] = n;
    }

    // Virtual nodes from cyclic events
    for (const ev of (_storyEventData.cyclic || [])) {
        const vid = 'ce:' + ev.id;
        const n = {
            id: vid, _realId: ev.id, name: ev.name || ev.id, description: ev.description || '',
            status: ev.status, type: 'cyclic_event', _source: 'ce',
            fire_events: ev.fire_events || [], activate_events: ev.activate_events || [],
            frequency_value: ev.frequency_value, frequency_unit: ev.frequency_unit,
            next_fire: ev.next_fire, last_fired: ev.last_fired,
        };
        allNodes.push(n);
        nodesById[vid] = n;
    }

    // Virtual nodes from narrative threads
    for (const t of (_storyEventData.threads || [])) {
        const vid = 'thread:' + t.id;
        const n = {
            id: vid, _realId: t.id, name: t.name || t.id,
            description: t.description || '',
            status: t.status, type: 'narrative_thread', _source: 'thread',
            created_turn: t.created_turn, last_updated: t.last_updated,
        };
        allNodes.push(n);
        nodesById[vid] = n;
    }

    // Virtual nodes from clue board
    for (const c of (_storyEventData.clues || [])) {
        const vid = 'clue:' + c.id;
        const n = {
            id: vid, _realId: c.id,
            name: c.text ? c.text.slice(0, 20) : c.id,
            description: c.text || '',
            status: 'discovered', type: 'clue', _source: 'clue',
            category: c.category, source: c.source,
            turn_discovered: c.turn_discovered,
        };
        allNodes.push(n);
        nodesById[vid] = n;
    }

    if (allNodes.length === 0) {
        graph.innerHTML = '<p style="padding:16px;color:var(--text-muted)">无剧情树或事件</p>';
        return;
    }

    // Build adjacency maps
    const childMap = {};
    const hasIncoming = new Set();

    // Tree internal edges: requires / on_complete_unlock / choice.unlock
    for (const node of allNodes) {
        if (node._source !== 'tree') continue;
        for (const req of (node.requires || [])) {
            if (nodesById[req]) {
                if (!childMap[req]) childMap[req] = [];
                if (!childMap[req].includes(node.id)) { childMap[req].push(node.id); hasIncoming.add(node.id); }
            }
        }
        for (const uid of (node.on_complete_unlock || [])) {
            if (nodesById[uid]) {
                if (!childMap[node.id]) childMap[node.id] = [];
                if (!childMap[node.id].includes(uid)) { childMap[node.id].push(uid); hasIncoming.add(uid); }
            }
        }
        if (node.choices) {
            for (const c of node.choices) {
                for (const uid of (c.unlock || [])) {
                    if (nodesById[uid]) {
                        if (!childMap[node.id]) childMap[node.id] = [];
                        if (!childMap[node.id].includes(uid)) { childMap[node.id].push(uid); hasIncoming.add(uid); }
                    }
                }
            }
        }
    }

    // Cross-system edges: fire_events → activate_events
    // Build activate_events lookup: eventName → [nodeId]
    const activateIndex = {};
    for (const node of allNodes) {
        const acts = node.activate_events || (node._source === 'tree' ? [] : []);
        for (const ae of acts) {
            (activateIndex[ae] ||= []).push(node.id);
        }
    }
    // For tree nodes, also check effects.fire_events
    const crossEdges = new Set();
    for (const node of allNodes) {
        let fires = [];
        if (node._source === 'tree') {
            fires = (node.effects?.fire_events || []);
        } else {
            fires = (node.fire_events || []);
        }
        for (const fe of fires) {
            const targets = activateIndex[fe] || [];
            for (const tid of targets) {
                if (tid !== node.id) {
                    if (!childMap[node.id]) childMap[node.id] = [];
                    if (!childMap[node.id].includes(tid)) childMap[node.id].push(tid);
                    hasIncoming.add(tid);
                    crossEdges.add(node.id + '→' + tid);
                }
            }
        }
    }

    const NODE_W = 220;
    const NODE_H = 90;
    const GAP_X = 40;
    const GAP_Y = 30;

    // ── Per-tree layout using subtree-width algorithm ──
    const positions = {};
    const treeGroupBounds = {}; // treeId → { minCol, maxCol, maxRow }

    // Group tree nodes by their tree
    const treeGroups = {};
    for (const tree of (_storyTreeData || [])) {
        treeGroups[tree.id] = { tree, nodes: [] };
    }
    for (const node of allNodes) {
        if (node._source === 'tree' && treeGroups[node._treeId]) {
            treeGroups[node._treeId].nodes.push(node);
        }
    }

    let globalColOffset = 0;
    for (const tree of (_storyTreeData || [])) {
        const group = treeGroups[tree.id];
        const nodes = group.nodes;
        if (nodes.length === 0) continue;

        const localChildMap = {};
        const localParentMap = {};
        const nodeIds = new Set(nodes.map(n => n.id));
        for (const n of nodes) {
            localChildMap[n.id] = [];
            localParentMap[n.id] = [];
        }
        for (const n of nodes) {
            for (const req of (n.requires || [])) {
                if (nodeIds.has(req)) { localChildMap[req].push(n.id); localParentMap[n.id].push(req); }
            }
            for (const uid of (n.on_complete_unlock || [])) {
                if (nodeIds.has(uid)) { localChildMap[n.id].push(uid); localParentMap[uid].push(n.id); }
            }
            if (n.choices) {
                for (const c of n.choices) {
                    for (const uid of (c.unlock || [])) {
                        if (nodeIds.has(uid)) { localChildMap[n.id].push(uid); localParentMap[uid].push(n.id); }
                    }
                }
            }
        }
        for (const k of nodeIds) { localChildMap[k] = [...new Set(localChildMap[k])]; localParentMap[k] = [...new Set(localParentMap[k])]; }

        let roots = nodes.filter(n => localParentMap[n.id].length === 0);
        if (roots.length === 0) roots = [nodes[0]];

        // BFS assign rows
        const rowOf = {};
        const vis = new Set();
        const q = roots.map(r => ({ id: r.id, row: 0 }));
        for (const r of q) vis.add(r.id);
        while (q.length > 0) {
            const { id, row } = q.shift();
            rowOf[id] = Math.max(rowOf[id] || 0, row);
            for (const cid of localChildMap[id]) {
                if (!vis.has(cid)) { vis.add(cid); q.push({ id: cid, row: row + 1 }); }
            }
        }
        for (const n of nodes) { if (rowOf[n.id] == null) rowOf[n.id] = 0; }

        // Bottom-up subtree width
        const maxRow = Math.max(...Object.values(rowOf));
        const subtreeW = {};
        for (let r = maxRow; r >= 0; r--) {
            for (const n of nodes) {
                if (rowOf[n.id] !== r) continue;
                const ch = localChildMap[n.id].filter(c => nodeIds.has(c));
                subtreeW[n.id] = ch.length === 0 ? 1 : ch.reduce((s, c) => s + (subtreeW[c] || 1), 0);
            }
        }

        // Top-down assign columns
        const colOf = {};
        let cursor = globalColOffset;
        const assignCols = (nid, start) => {
            const w = subtreeW[nid] || 1;
            colOf[nid] = start + w / 2 - 0.5;
            const ch = localChildMap[nid].filter(c => nodeIds.has(c));
            let c2 = start;
            for (const cid of ch) { assignCols(cid, c2); c2 += (subtreeW[cid] || 1); }
        };
        for (const root of roots) {
            assignCols(root.id, cursor);
            cursor += (subtreeW[root.id] || 1) + 1;
        }

        let gMinCol = Infinity, gMaxCol = -Infinity;
        for (const n of nodes) {
            positions[n.id] = { row: rowOf[n.id], col: colOf[n.id] ?? globalColOffset };
            gMinCol = Math.min(gMinCol, positions[n.id].col);
            gMaxCol = Math.max(gMaxCol, positions[n.id].col);
        }
        treeGroupBounds[tree.id] = { minCol: gMinCol, maxCol: gMaxCol, maxRow };
        globalColOffset = gMaxCol + 3;
    }

    // ── Event nodes: place below tree nodes, split by type ──
    const otEvts = allNodes.filter(n => n._source === 'ot');
    const ceEvts = allNodes.filter(n => n._source === 'ce');
    const threadNodes = allNodes.filter(n => n._source === 'thread');
    const clueNodes = allNodes.filter(n => n._source === 'clue');

    let maxTreeRow = 0;
    for (const p of Object.values(positions)) maxTreeRow = Math.max(maxTreeRow, p.row);
    const evtBaseRow = maxTreeRow + 2;

    const placeEvtGroup = (group, startCol, baseRow) => {
        const cols = Math.max(1, Math.min(Math.ceil(Math.sqrt(group.length)), Math.ceil(group.length / 2)));
        group.forEach((n, i) => {
            positions[n.id] = { row: baseRow + Math.floor(i / cols), col: startCol + (i % cols) };
        });
        return { cols, rows: Math.ceil(group.length / cols) };
    };

    let evtCol = 0;
    if (otEvts.length > 0) {
        const info = placeEvtGroup(otEvts, evtCol, evtBaseRow);
        evtCol += info.cols + 1;
    }
    if (ceEvts.length > 0) {
        const info = placeEvtGroup(ceEvts, evtCol, evtBaseRow);
        evtCol += info.cols + 1;
    }
    if (threadNodes.length > 0) {
        const info = placeEvtGroup(threadNodes, evtCol, evtBaseRow);
        evtCol += info.cols + 1;
    }
    if (clueNodes.length > 0) {
        placeEvtGroup(clueNodes, evtCol, evtBaseRow);
    }

    // ── Normalize columns ──
    const posValues = Object.values(positions);
    if (posValues.length === 0) return;
    let minCol = Infinity, maxColVal = -Infinity, minRow = Infinity, maxRow = -Infinity;
    for (const p of posValues) {
        minCol = Math.min(minCol, p.col); maxColVal = Math.max(maxColVal, p.col);
        minRow = Math.min(minRow, p.row); maxRow = Math.max(maxRow, p.row);
    }
    const offsetCol = -minCol;
    const totalW = (maxColVal - minCol + 1) * (NODE_W + GAP_X);

    // ── Create inner canvas for pan/zoom ──
    const canvas = document.createElement('div');
    canvas.className = 'storytree-canvas';
    canvas.style.position = 'relative';
    canvas.style.transformOrigin = '0 0';

    // First pass: create node elements
    const nodeElements = {};
    for (const node of allNodes) {
        const pos = positions[node.id];
        if (!pos) continue;

        const el = document.createElement('div');

        if (node._source === 'ot') {
            el.className = `story-node story-node--ot-event${node.status === 'fired' ? ' story-node--fired' : ''}`;
        } else if (node._source === 'ce') {
            el.className = `story-node story-node--ce-event${node.status === 'expired' ? ' story-node--expired' : ''}`;
        } else if (node._source === 'thread') {
            el.className = `story-node story-node--thread${node.status === 'resolved' ? ' story-node--resolved' : node.status === 'dormant' ? ' story-node--dormant' : ''}`;
        } else if (node._source === 'clue') {
            el.className = 'story-node story-node--clue';
        } else {
            el.className = `story-node story-node--${node.status}`;
            if (node.type === 'choice' && node.status === 'active') {
                el.classList.add('story-node--choice-pending');
            }
        }
        el.style.width = NODE_W + 'px';
        el.style.visibility = 'hidden';
        el.style.position = 'absolute';

        let html = '';

        if (node._source === 'tree' && !hasIncoming.has(node.id) && nodeTreeInfo[node.id]) {
            html += `<div class="story-node-tree-label">${_getTreeIcon(node._treeIcon)} ${_esc(node._treeName)}</div>`;
        }

        let statusIcon = '', typeIcon = '';
        if (node._source === 'ot') {
            statusIcon = node.status === 'fired' ? '✅ ' : '\u{1F7E1} ';
            typeIcon = ' \u{1F4CC}';
        } else if (node._source === 'ce') {
            statusIcon = node.status === 'expired' ? '\u{1F6AB} ' : '\u{1F535} ';
            typeIcon = ' \u{1F504}';
        } else if (node._source === 'thread') {
            statusIcon = node.status === 'resolved' ? '✅ ' : node.status === 'dormant' ? '\u{1F4A4} ' : '\u{1F4D6} ';
            typeIcon = ' \u{1F9F5}';
        } else if (node._source === 'clue') {
            statusIcon = '\u{1F50D} ';
            typeIcon = ' \u{1F4CC}';
        } else {
            if (node.status === 'completed') statusIcon = '✅ ';
            else if (node.status === 'active') statusIcon = '\u{1F535} ';
            else if (node.status === 'available') statusIcon = '\u{1F7E1} ';
            else statusIcon = '\u{1F512} ';
            if (node.type === 'choice') typeIcon = ' ⚔';
            else if (node.type === 'quest') typeIcon = ' ❗';
            else if (node.type === 'timed') typeIcon = ' ⌛';
            else if (node.type === 'trigger') typeIcon = ' ⚡';
            else if (node.type === 'periodic') typeIcon = ' \u{1F504}';
        }

        const nodeName = (node._source === 'tree' && node.status === 'locked') ? '???' : node.name;
        html += `<div class="story-node-title">${statusIcon}${_esc(nodeName)}${typeIcon}</div>`;

        if (node.description && !(node._source === 'tree' && node.status === 'locked')) {
            html += `<div class="story-node-desc">${_esc(node.description)}</div>`;
        }

        if (node._source === 'ot' && node.trigger_time) {
            const label = node.status === 'fired' ? '已触发' : '触发于';
            html += `<div class="story-node-event-meta">${label} ${_esc(_fmtTime(node.trigger_time))}</div>`;
        }
        if (node._source === 'ce') {
            const _unitName = { day: '天', week: '周', month: '月', hour: '小时', minute: '分钟' };
            const freq = `每${node.frequency_value}${_unitName[node.frequency_unit] || node.frequency_unit}`;
            let meta = freq;
            if (node.next_fire) meta += ` | 下次: ${_esc(_fmtTime(node.next_fire))}`;
            html += `<div class="story-node-event-meta">${meta}</div>`;
        }
        if (node._source === 'thread') {
            let meta = `第${node.created_turn || '?'}回合`;
            if (node.last_updated && node.last_updated !== node.created_turn) meta += ` | 更新：第${node.last_updated}回合`;
            html += `<div class="story-node-event-meta">${meta}</div>`;
        }
        if (node._source === 'clue') {
            let meta = '';
            if (node.category) meta += node.category;
            if (node.source) meta += (meta ? ' | ' : '') + '来源: ' + _esc(node.source);
            if (node.turn_discovered) meta += (meta ? ' | ' : '') + `第${node.turn_discovered}回合`;
            if (meta) html += `<div class="story-node-event-meta">${meta}</div>`;
        }

        if (node.type === 'timed' && node.progress) {
            const pct = Math.max(0, Math.min(100, node.progress.percent || 0));
            html += `<div class="story-node-progress"><div class="story-node-progress-bar" style="width:${pct}%"></div></div>`;
        }

        if (node.type === 'periodic' && node.cooldown_remaining > 0) {
            html += `<div class="story-node-desc" style="opacity:0.7">冷却: ${node.cooldown_remaining}回合</div>`;
        }

        const relTags = [];
        if (node.related_npcs && node.related_npcs.length) {
            for (const nid of node.related_npcs) relTags.push(`<span class="story-rel-tag npc">${_esc(nid)}</span>`);
        }
        if (node.related_orgs && node.related_orgs.length) {
            for (const oid of node.related_orgs) relTags.push(`<span class="story-rel-tag org">${_esc(oid)}</span>`);
        }
        if (relTags.length) {
            html += `<div class="story-node-rel-tags">${relTags.join('')}</div>`;
        }

        el.innerHTML = html;

        if (node._source === 'tree' && node.type === 'choice' && node.status === 'active' && node.choices) {
            const choicesDiv = document.createElement('div');
            choicesDiv.className = 'story-node-choices';
            for (const c of node.choices) {
                const btn = document.createElement('button');
                btn.className = 'story-choice-btn';
                btn.textContent = c.label;
                btn.title = c.description || '';
                btn.onclick = () => makeStoryChoice(node.id, c.id);
                choicesDiv.appendChild(btn);
            }
            el.appendChild(choicesDiv);
        }

        if (node._source === 'tree' && node.type === 'choice' && node.status === 'completed' && node.choices) {
            const chosen = node.choices.find(c => c.chosen);
            if (chosen) {
                const div = document.createElement('div');
                div.className = 'story-node-chosen';
                div.textContent = '→ ' + chosen.label;
                el.appendChild(div);
            }
        }

        nodeElements[node.id] = el;
        if (_stRecentNodeIds.has(node.id) || _stRecentNodeIds.has(node._realId)) {
            el.classList.add('story-node--recent');
        }
        canvas.appendChild(el);
    }

    // Mount to measure
    graph.appendChild(canvas);
    const measuredHeights = {};
    for (const [nid, el] of Object.entries(nodeElements)) {
        measuredHeights[nid] = el.offsetHeight || NODE_H;
    }

    // Per-row height
    const rowHeight = {};
    for (const node of allNodes) {
        const p = positions[node.id];
        if (!p) continue;
        const h = measuredHeights[node.id] || NODE_H;
        rowHeight[p.row] = Math.max(rowHeight[p.row] || NODE_H, h);
    }
    const rowY = {};
    let cumY = 0;
    for (let r = minRow; r <= maxRow; r++) {
        rowY[r] = cumY;
        cumY += (rowHeight[r] || NODE_H) + GAP_Y;
    }
    const totalH = cumY + 40;
    canvas.style.width = Math.max(totalW, 300) + 'px';
    canvas.style.height = totalH + 'px';

    // Position nodes
    for (const node of allNodes) {
        const pos = positions[node.id];
        if (!pos) continue;
        const el = nodeElements[node.id];
        if (!el) continue;
        el.style.left = (pos.col + offsetCol) * (NODE_W + GAP_X) + 'px';
        el.style.top = ((rowY[pos.row] || 0) + 20) + 'px';
        el.style.visibility = '';
    }

    // ── Draw tree group background boxes ──
    const groupColors = ['rgba(100,150,255,0.06)','rgba(255,180,100,0.06)','rgba(100,255,150,0.06)','rgba(255,100,200,0.06)'];
    const groupBorders = ['rgba(100,150,255,0.25)','rgba(255,180,100,0.25)','rgba(100,255,150,0.25)','rgba(255,100,200,0.25)'];
    let ti = 0;
    for (const tree of (_storyTreeData || [])) {
        const group = treeGroups[tree.id];
        if (!group || group.nodes.length === 0) continue;
        let bMinX = Infinity, bMinY = Infinity, bMaxX = -Infinity, bMaxY = -Infinity;
        for (const n of group.nodes) {
            const p = positions[n.id]; if (!p) continue;
            const x = (p.col + offsetCol) * (NODE_W + GAP_X);
            const y = (rowY[p.row] || 0) + 20;
            const h = measuredHeights[n.id] || NODE_H;
            bMinX = Math.min(bMinX, x); bMinY = Math.min(bMinY, y);
            bMaxX = Math.max(bMaxX, x + NODE_W); bMaxY = Math.max(bMaxY, y + h);
        }
        const pad = 16;
        const box = document.createElement('div');
        box.style.cssText = `position:absolute;left:${bMinX-pad}px;top:${bMinY-pad-20}px;width:${bMaxX-bMinX+pad*2}px;height:${bMaxY-bMinY+pad*2+20}px;background:${groupColors[ti%4]};border:1px solid ${groupBorders[ti%4]};border-radius:8px;pointer-events:none`;
        const label = document.createElement('div');
        label.style.cssText = 'position:absolute;top:4px;left:8px;font-size:0.75rem;opacity:0.6;white-space:nowrap';
        label.textContent = `${_getTreeIcon(tree.icon)} ${tree.name}`;
        box.appendChild(label);
        canvas.appendChild(box);
        ti++;
    }

    // SVG for connectors
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');
    svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;overflow:visible';
    canvas.insertBefore(svg, canvas.firstChild);

    function getCenter(nid) {
        const p = positions[nid];
        if (!p) return { x: 0, y: 0 };
        const h = measuredHeights[nid] || NODE_H;
        return {
            x: (p.col + offsetCol) * (NODE_W + GAP_X) + NODE_W / 2,
            y: (rowY[p.row] || 0) + h / 2 + 20,
        };
    }

    const drawnEdges = new Set();
    for (const [parentId, children] of Object.entries(childMap)) {
        const from = getCenter(parentId);
        const parentH = measuredHeights[parentId] || NODE_H;
        for (const cid of [...new Set(children)]) {
            const edgeKey = parentId + '→' + cid;
            if (drawnEdges.has(edgeKey)) continue;
            drawnEdges.add(edgeKey);

            const to = getCenter(cid);
            const childH = measuredHeights[cid] || NODE_H;
            const parentNode = nodesById[parentId];
            const childNode = nodesById[cid];
            const isCross = crossEdges.has(edgeKey);
            const line = document.createElementNS(svgNS, 'path');
            const midY = (from.y + to.y) / 2;
            line.setAttribute('d', `M${from.x},${from.y + parentH / 2 - 5} C${from.x},${midY} ${to.x},${midY} ${to.x},${to.y - childH / 2 + 5}`);
            line.setAttribute('fill', 'none');

            if (isCross) {
                line.setAttribute('stroke', '#e8913a');
                line.setAttribute('stroke-width', '2');
                line.setAttribute('stroke-dasharray', '6,3');
            } else {
                const bothDone = parentNode?.status === 'completed' && childNode?.status === 'completed';
                line.setAttribute('stroke', bothDone ? 'var(--accent)' : 'var(--border)');
                line.setAttribute('stroke-width', bothDone ? '2.5' : '1.5');
                line.setAttribute('stroke-dasharray', childNode?.status === 'locked' ? '6,4' : 'none');
            }
            svg.appendChild(line);
        }
    }

    // ── Pan & Zoom ──
    _stViewZoom = 1;
    _stViewPanX = 0;
    _stViewPanY = 0;
    _stViewCanvas = canvas;
    const applyTransform = () => {
        canvas.style.transform = `translate(${_stViewPanX}px, ${_stViewPanY}px) scale(${_stViewZoom})`;
    };

    // Auto-fit
    const graphRect = graph.getBoundingClientRect();
    const gw = graphRect.width || 600, gh = graphRect.height || 400;
    const cw = Math.max(totalW, 300), ch = totalH;
    _stViewZoom = Math.max(0.2, Math.min(1, Math.min(gw / (cw + 40), gh / (ch + 40))));
    _stViewPanX = Math.max(0, (gw - cw * _stViewZoom) / 2);
    _stViewPanY = 10;
    applyTransform();

    // Wheel zoom at mouse position
    graph.onwheel = (ev) => {
        ev.preventDefault();
        const rect = graph.getBoundingClientRect();
        const mx = ev.clientX - rect.left;
        const my = ev.clientY - rect.top;
        const oldZ = _stViewZoom;
        const delta = ev.deltaY > 0 ? -0.08 : 0.08;
        _stViewZoom = Math.max(0.2, Math.min(2.5, _stViewZoom + delta));
        const ratio = _stViewZoom / oldZ;
        _stViewPanX = mx - ratio * (mx - _stViewPanX);
        _stViewPanY = my - ratio * (my - _stViewPanY);
        applyTransform();
    };

    // Mouse drag pan
    let _panning = false;
    graph.onmousedown = (ev) => {
        if (ev.target.closest('.story-choice-btn')) return;
        if (ev.button !== 0) return;
        _panning = true;
        const sx = ev.clientX, sy = ev.clientY;
        const spx = _stViewPanX, spy = _stViewPanY;
        graph.style.cursor = 'grabbing';
        const onMove = (me) => {
            _stViewPanX = spx + (me.clientX - sx);
            _stViewPanY = spy + (me.clientY - sy);
            applyTransform();
        };
        const onUp = () => {
            _panning = false;
            graph.style.cursor = '';
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    };
}

async function makeStoryChoice(nodeId, choiceId, summaryOnly = false) {
    if (!currentSaveId) return;
    const saveId = currentSaveId;
    try {
        const url = `/api/game/${saveId}/story-tree/choice${summaryOnly ? '?summary_only=true' : ''}`;
        const result = await API.post(url, {
            node_id: nodeId,
            choice_id: choiceId,
        });
        if (saveId !== currentSaveId) return false;
        if (!result.success) {
            showNotification('选择已失效，请刷新后重试');
            return false;
        }
        if (result.notifications) {
            for (const msg of result.notifications) {
                showNotification(msg);
            }
        }
        if (!summaryOnly) _storyTreeData = result.trees || _storyTreeData;
        if (!summaryOnly || document.getElementById('main-map-view').hidden) await loadStoryTree();
        return true;
    } catch (e) {
        if (saveId !== currentSaveId) return false;
        console.error('Story choice failed:', e);
        showNotification('选择失败: ' + (e.message || '未知错误'));
        return false;
    }
}

function showNotification(msg) {
    const el = document.createElement('div');
    el.className = 'story-notification';
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.classList.add('show'), 10);
    setTimeout(() => {
        el.classList.remove('show');
        setTimeout(() => el.remove(), 300);
    }, 4000);
}

function toggleStoryTreeExpand() {
    const panel = document.getElementById('status-panel');
    const btn = document.getElementById('btn-storytree-expand');
    if (!panel || !btn) return;
    const expanded = panel.classList.toggle('storytree-expanded');
    btn.textContent = expanded ? '✕ 收起' : '⛶ 展开';
    renderStoryTreeGraph();
}

function _renderNewspaperHtml(data) {
    const _esc = typeof escapeHtml === 'function' ? escapeHtml : (s => s);
    let html = '';
    html += `<div class="newspaper-title">${_esc(data.title || '每日快报')}</div>`;
    html += `<div class="newspaper-date">${_esc(data.date || '')}</div>`;
    if (data.headline) {
        html += `<div class="newspaper-headline">${_esc(data.headline)}</div>`;
    }
    for (const sec of (data.sections || [])) {
        html += `<div class="newspaper-section">`;
        html += `<div class="newspaper-section-title">${_esc(sec.title || '')}</div>`;
        html += `<div class="newspaper-section-body">${_esc(sec.content || '')}</div>`;
        html += `</div>`;
    }
    if (!data.sections?.length && data.content) {
        html += `<div class="newspaper-section-body">${_esc(data.content)}</div>`;
    }
    html += `<div class="newspaper-footer"><button class="newspaper-regenerate" onclick="regenerateNewspaper()">&#x1F504; 重新生成</button></div>`;
    return html;
}

async function viewNewspaper() {
    if (!currentSaveId) return;
    const modal = document.getElementById('newspaper-modal');
    const body = document.getElementById('newspaper-body');
    if (!modal || !body) return;
    body.innerHTML = '<div class="newspaper-loading">正在加载报纸...</div>';
    modal.style.display = 'flex';
    try {
        const data = await API.post(`/api/game/${currentSaveId}/newspaper`);
        body.innerHTML = _renderNewspaperHtml(data);
    } catch (e) {
        const _esc2 = typeof escapeHtml === 'function' ? escapeHtml : (s => s);
        body.innerHTML = '<div class="newspaper-loading">生成报纸失败: ' + _esc2(e.message || '未知错误') + '</div>';
    }
}

async function regenerateNewspaper() {
    if (!currentSaveId) return;
    const body = document.getElementById('newspaper-body');
    if (!body) return;
    body.innerHTML = '<div class="newspaper-loading">正在重新生成报纸...</div>';
    try {
        const data = await API.post(`/api/game/${currentSaveId}/newspaper/regenerate`);
        body.innerHTML = _renderNewspaperHtml(data);
    } catch (e) {
        const _esc2 = typeof escapeHtml === 'function' ? escapeHtml : (s => s);
        body.innerHTML = '<div class="newspaper-loading">重新生成失败: ' + _esc2(e.message || '未知错误') + '</div>';
    }
}

function closeNewspaperModal() {
    const modal = document.getElementById('newspaper-modal');
    if (modal) modal.style.display = 'none';
}

document.addEventListener('click', (e) => {
    const modal = document.getElementById('newspaper-modal');
    if (modal && e.target === modal) modal.style.display = 'none';
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const panel = document.getElementById('status-panel');
        if (panel && panel.classList.contains('storytree-expanded')) {
            toggleStoryTreeExpand();
            return;
        }
        const modal = document.getElementById('newspaper-modal');
        if (modal && modal.style.display !== 'none') {
            modal.style.display = 'none';
        }
    }
});
