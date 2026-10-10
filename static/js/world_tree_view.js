/* 酒馆 - 世界树可视化 */

// 大树虚拟化阈值：超过这个总节点数则启用折叠
const _TREE_VIRTUALIZE_THRESHOLD = 50;
// 默认渲染的最大深度（从根开始）；超出部分折叠为"展开更多"按钮
const _TREE_MAX_DEPTH_DEFAULT = 30;
// 单个分歧点下默认渲染的子分支数；超出则折叠
const _TREE_MAX_CHILDREN_DEFAULT = 8;

// 缓存当前树的总节点数，用于决定是否启用虚拟化
let _treeTotalNodes = 0;

function _countTreeNodes(node) {
    if (!node) return 0;
    let n = 1;
    if (node.children) {
        for (const c of node.children) n += _countTreeNodes(c);
    }
    return n;
}

// 计算从该节点到 active 节点是否在路径上（用于优先渲染主路径）
function _markActivePath(node, parentOnPath) {
    if (!node) return false;
    let onPath = !!node.is_active;
    if (node.children) {
        for (const c of node.children) {
            if (_markActivePath(c, false)) onPath = true;
        }
    }
    node._on_active_path = onPath;
    return onPath;
}

async function loadWorldTree() {
    if (!currentSaveId) {
        document.getElementById('tree-view').innerHTML = '<p class="placeholder-text">请先开始游戏</p>';
        return;
    }

    try {
        const tree = await API.get(`/api/game/${currentSaveId}/tree`);
        const container = document.getElementById('tree-view');
        container.innerHTML = '';

        if (!tree || !tree.id) {
            container.innerHTML = '<p class="placeholder-text">世界树为空</p>';
            return;
        }

        _treeTotalNodes = _countTreeNodes(tree);
        _markActivePath(tree, false);

        // G6: 搜索框
        const searchBar = document.createElement('div');
        searchBar.className = 'tree-search-bar';
        searchBar.innerHTML = `<input type="text" class="tree-search-input" placeholder="搜索节点（按行动或时间）..." oninput="_filterTreeNodes(this.value)">`;
        container.appendChild(searchBar);

        // 大树时显示统计信息
        if (_treeTotalNodes > _TREE_VIRTUALIZE_THRESHOLD) {
            const info = document.createElement('div');
            info.className = 'tree-info-bar';
            info.style.cssText = 'padding:0.4rem 0.6rem;background:var(--bg-secondary,#222);border-radius:4px;margin-bottom:0.5rem;font-size:0.8rem;color:var(--text-muted)';
            info.innerHTML = `共 <strong>${_treeTotalNodes}</strong> 个节点 — 已启用折叠模式 <button onclick="_expandAllTree()" style="margin-left:0.6rem;font-size:0.75rem;padding:0.1rem 0.4rem;cursor:pointer">展开全部</button>`;
            container.appendChild(info);
        }

        container.appendChild(renderTreeNode(tree, 0));
    } catch (e) {
        document.getElementById('tree-view').innerHTML = `<p style="color:var(--accent)">加载失败: ${e.message}</p>`;
    }
}

function _expandAllTree() {
    // 取消虚拟化阈值，重新渲染
    const orig = _TREE_VIRTUALIZE_THRESHOLD;
    window._TREE_VIRTUALIZE_OVERRIDE = true;
    loadWorldTree().finally(() => { window._TREE_VIRTUALIZE_OVERRIDE = false; });
}

function renderTreeNode(node, depth) {
    if (!node) return document.createTextNode('');
    depth = depth || 0;

    const wrapper = document.createElement('div');

    // Node itself
    const nodeEl = document.createElement('div');
    nodeEl.className = 'tree-node';
    if (node.is_active) nodeEl.classList.add('active');
    if (node.is_divergence) nodeEl.classList.add('divergence');

    nodeEl.innerHTML = `
        <div class="node-turn">回合 ${node.turn}${node.is_divergence ? ' [分歧点]' : ''}${node.is_active ? ' [当前]' : ''}</div>
        <div class="node-summary">${formatGameTime(node.time)} ${node.action_summary ? '— ' + escapeHtml(node.action_summary) : ''}</div>
        ${!node.is_active ? `<button class="btn-branch-inline" onclick="event.stopPropagation();branchFromNode('${node.id}')" title="从此处分支">&#9095; 分支</button>` : ''}
    `;

    nodeEl.onclick = (e) => {
        e.stopPropagation();
        showNodeDetail(node.id);
    };

    wrapper.appendChild(nodeEl);

    // Children
    if (node.children && node.children.length > 0) {
        const useVirtualization = !window._TREE_VIRTUALIZE_OVERRIDE && _treeTotalNodes > _TREE_VIRTUALIZE_THRESHOLD;
        const childrenEl = document.createElement('div');
        childrenEl.className = 'tree-children';

        // 是否超过深度限制？
        if (useVirtualization && depth >= _TREE_MAX_DEPTH_DEFAULT) {
            // 只展示主路径上的下一节点，其余折叠
            const onPathChild = node.children.find(c => c && c._on_active_path);
            if (onPathChild) {
                childrenEl.appendChild(renderTreeNode(onPathChild, depth + 1));
            }
            const hiddenCount = node.children.length - (onPathChild ? 1 : 0);
            if (hiddenCount > 0) {
                childrenEl.appendChild(_makeExpandStub(node.children.filter(c => c !== onPathChild), depth + 1, `展开 ${hiddenCount} 个深层分支`));
            }
        } else {
            // 决定哪些子节点直接渲染、哪些折叠
            const childrenToShow = [];
            const childrenToHide = [];
            if (useVirtualization && node.children.length > _TREE_MAX_CHILDREN_DEFAULT) {
                // 优先保留：active path、最近的（按出现顺序的最后几个）
                for (const c of node.children) {
                    if (c && c._on_active_path) childrenToShow.push(c);
                    else childrenToHide.push(c);
                }
                // 从尾部补足到 _TREE_MAX_CHILDREN_DEFAULT
                while (childrenToShow.length < _TREE_MAX_CHILDREN_DEFAULT && childrenToHide.length > 0) {
                    childrenToShow.push(childrenToHide.pop());
                }
            } else {
                for (const c of node.children) childrenToShow.push(c);
            }

            // 按原始顺序渲染
            const orderMap = new Map(node.children.map((c, i) => [c, i]));
            childrenToShow.sort((a, b) => (orderMap.get(a) || 0) - (orderMap.get(b) || 0));

            childrenToShow.forEach(child => {
                if (child) childrenEl.appendChild(renderTreeNode(child, depth + 1));
            });
            if (childrenToHide.length > 0) {
                childrenEl.appendChild(_makeExpandStub(childrenToHide, depth + 1, `展开 ${childrenToHide.length} 个其他分支`));
            }
        }
        wrapper.appendChild(childrenEl);
    }

    return wrapper;
}

function _makeExpandStub(hiddenChildren, depth, label) {
    const stub = document.createElement('div');
    stub.className = 'tree-expand-stub';
    stub.style.cssText = 'margin:0.3rem 0 0.3rem 1.2rem;padding:0.2rem 0.5rem;background:var(--bg-tertiary,#333);border:1px dashed var(--border-color,#555);border-radius:4px;font-size:0.78rem;color:var(--text-muted);cursor:pointer';
    stub.textContent = '+ ' + label;
    stub.onclick = (e) => {
        e.stopPropagation();
        const parent = stub.parentNode;
        // 用真实节点替换 stub
        const frag = document.createDocumentFragment();
        for (const child of hiddenChildren) {
            if (child) frag.appendChild(renderTreeNode(child, depth));
        }
        parent.replaceChild(frag, stub);
    };
    return stub;
}

async function showNodeDetail(nodeId) {
    if (!currentSaveId) return;

    try {
        const node = await API.get(`/api/game/${currentSaveId}/tree/${nodeId}`);
        const modal = document.getElementById('material-detail-modal');
        const content = document.getElementById('material-detail-content');

        const action = node.player_action;
        const actionText = action ? (typeof action === 'object' ? action.text : action) : '(游戏开始)';

        content.innerHTML = `
            <h3>回合 ${node.turn_number} — ${formatGameTime(node.game_time)}</h3>
            <div style="margin:0.8rem 0">
                <strong>玩家行动:</strong> ${escapeHtml(actionText)}
            </div>
            <div style="margin:0.8rem 0;line-height:1.8;color:var(--text-secondary)">
                ${escapeHtml(node.ai_response || '').replace(/\n/g, '<br>')}
            </div>
            ${node.dice_rolls && node.dice_rolls.length > 0 ? `
                <div style="margin:0.8rem 0">
                    <strong>骰子结果:</strong>
                    ${node.dice_rolls.map(d => `<span class="dice-result"><span class="dice-label">${d.random_item_id}</span> <span class="dice-value">${d.formula}=${d.total}</span>${d.range_label ? ` → ${d.range_label}` : ''}</span>`).join(' ')}
                </div>
            ` : ''}
            ${node.state_changes && node.state_changes.length > 0 ? `
                <div style="margin:0.8rem 0">
                    <strong>状态变化:</strong>
                    ${node.state_changes.map(c => {
                        const diff = (c.new || 0) - (c.old || 0);
                        const color = diff >= 0 ? 'var(--success)' : 'var(--accent)';
                        return `<div style="color:${color}">${c.target}: ${diff >= 0 ? '+' : ''}${diff} ${c.reason ? `(${c.reason})` : ''}</div>`;
                    }).join('')}
                </div>
            ` : ''}
            <div class="result-actions" style="margin-top:1rem">
                <button onclick="branchFromNode('${nodeId}')" class="btn-primary">从此处分支</button>
                <button onclick="closeMaterialDetail()" class="btn-secondary">关闭</button>
            </div>
        `;
        modal.style.display = 'flex';
    } catch (e) {
        alert('加载节点失败: ' + e.message);
    }
}

async function branchFromNode(nodeId) {
    if (!currentSaveId) return;
    // Flow#6: 分支前确认，提醒当前进度会切换
    if (!confirm('切换到此分支将改变当前游戏状态。确定要从这里开始新的分支吗？')) {
        return;
    }
    try {
        const result = await API.post(`/api/game/${currentSaveId}/branch/${nodeId}`, {});
        currentState = result.state;
        closeMaterialDetail();
        switchPanel('game');
        showGameScreen(result);
    } catch (e) {
        alert('切换分支失败: ' + e.message);
    }
}

// G6: 世界树搜索 — 高亮匹配节点，淡化不匹配
function _filterTreeNodes(query) {
    const container = document.getElementById('tree-view');
    const nodes = container.querySelectorAll('.tree-node');
    const q = (query || '').trim().toLowerCase();
    if (!q) {
        nodes.forEach(n => { n.classList.remove('tree-search-match', 'tree-search-dim'); });
        return;
    }
    nodes.forEach(n => {
        const text = (n.textContent || '').toLowerCase();
        if (text.includes(q)) {
            n.classList.add('tree-search-match');
            n.classList.remove('tree-search-dim');
        } else {
            n.classList.add('tree-search-dim');
            n.classList.remove('tree-search-match');
        }
    });
}
