/* 酒馆 - 搜索与素材界面 */

let _searchResults = [];  // store results by index to avoid inline JSON in HTML

// Notification helper — falls back to alert if showNotification isn't defined globally
function _notify(msg, type) {
    if (typeof showNotification === 'function') {
        showNotification(msg, type || 'info');
    } else {
        alert(msg);
    }
}

// Web Search
async function performSearch() {
    const query = document.getElementById('search-input').value.trim();
    if (!query) return;

    const sources = [];
    if (document.getElementById('src-web').checked) sources.push('web');
    if (document.getElementById('src-ai').checked) sources.push('ai');
    if (document.getElementById('src-scrape').checked) sources.push('scrape');

    // Multi-URL support: parse textarea (one URL per line)
    const urlsRaw = (document.getElementById('scrape-urls')?.value || '').trim();
    const scrapeUrls = urlsRaw ? urlsRaw.split('\n').map(u => u.trim()).filter(u => u) : [];

    const maxResults = parseInt(document.getElementById('search-max-results')?.value || '5', 10);

    const container = document.getElementById('search-results');
    container.innerHTML = '<div class="loading"></div> 搜索中...';

    try {
        const result = await API.post('/api/search/web', {
            query,
            sources,
            scrape_urls: scrapeUrls.length ? scrapeUrls : undefined,
            max_results: maxResults,
        });
        container.innerHTML = '';
        _searchResults = result.results || [];

        if (_searchResults.length === 0) {
            container.innerHTML = '<p class="placeholder-text">没有找到结果</p>';
            return;
        }

        _searchResults.forEach((r, i) => {
            const sourceLabels = {
                web_search: '网页搜索',
                ai_generated: 'AI知识',
                web_scrape: '网页抓取',
            };
            // S2: 转义 URL（href 与显示文本均需转义）；只允许 http/https
            const safeUrl = r.url && /^https?:\/\//i.test(r.url) ? r.url : '';
            const urlHtml = safeUrl
                ? ` | <a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${escapeHtml(safeUrl.substring(0, 50))}...</a>`
                : '';
            container.innerHTML += `
                <div class="result-card" id="search-result-${i}">
                    <h4>${escapeHtml(r.title)}</h4>
                    <div class="result-source">${sourceLabels[r.source_type] || escapeHtml(r.source_type || '')}
                        ${urlHtml}
                    </div>
                    <div class="result-content">${escapeHtml(r.content || '').substring(0, 300)}${(r.content||'').length > 300 ? '...' : ''}</div>
                    <div class="result-actions">
                        <button onclick="saveSearchResult(${i})" class="btn-primary">收藏到素材库</button>
                    </div>
                </div>`;
        });
    } catch (e) {
        container.innerHTML = `<p style="color:var(--accent)">搜索失败: ${e.message}</p>`;
    }
}

async function saveSearchResult(index) {
    const r = _searchResults[index];
    if (!r) return;
    const btn = document.querySelector(`#search-result-${index} .btn-primary`);
    if (btn) { btn.disabled = true; btn.textContent = '收藏中...'; }
    const query = document.getElementById('search-input').value.trim();
    try {
        const saved = await API.post('/api/search/materials', {
            title: r.title, content: r.content, url: r.url,
            source_type: r.source_type, search_query: query,
        });
        if (btn) { btn.textContent = '已收藏'; btn.classList.remove('btn-primary'); btn.classList.add('btn-secondary'); }
        _notify(`已收藏: ${saved.material?.title || '素材'}${saved.material?.tags ? ' | 标签: ' + saved.material.tags.join(', ') : ''}`, 'success');
        loadMaterials();
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = '收藏到素材库'; }
        _notify('收藏失败: ' + e.message, 'error');
    }
}

// Materials
async function loadMaterials() {
    try {
        const result = await API.get('/api/search/materials');
        renderMaterialsList(result.materials || []);
    } catch (e) {
        console.error('Failed to load materials:', e);
    }
}

async function searchMaterials() {
    const query = document.getElementById('material-search-input').value.trim();
    try {
        const result = await API.get(`/api/search/materials?query=${encodeURIComponent(query)}`);
        renderMaterialsList(result.materials || []);
    } catch (e) {
        console.error('Search failed:', e);
    }
}

async function loadTags() {
    try {
        const result = await API.get('/api/search/materials/tags');
        const container = document.getElementById('material-tags-filter');
        container.innerHTML = (result.tags || []).map(t => {
            const safe = escapeHtml(t);
            return `<span class="tag" onclick="filterByTag(this.dataset.tag)" data-tag="${safe}">${safe}</span>`;
        }).join('');
    } catch (e) { /* ignore */ }
}

async function filterByTag(tag) {
    try {
        const result = await API.get(`/api/search/materials?tags=${encodeURIComponent(tag)}`);
        renderMaterialsList(result.materials || []);
    } catch (e) {
        console.error('Filter failed:', e);
    }
}

function renderMaterialsList(materials) {
    const container = document.getElementById('materials-list');
    if (materials.length === 0) {
        container.innerHTML = '<p class="placeholder-text">素材库为空</p>';
        return;
    }

    container.innerHTML = materials.map(m => `
        <div class="result-card material-card">
            <label class="material-select-label" title="勾选后可批量生成剧本"><input type="checkbox" class="material-select-cb" value="${m.id}"><span>选择</span></label>
            <h4>${escapeHtml(m.title)}</h4>
            <div class="result-source">${escapeHtml(m.source_type || '未知来源')} | ${escapeHtml(m.created_at || '')}</div>
            <div class="tags-area">
                ${(m.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('')}
            </div>
            <div class="result-content">${escapeHtml(m.summary || m.content || '').substring(0, 200)}...</div>
            <div class="result-actions">
                <button onclick="viewMaterial(${m.id})" class="btn-secondary">查看详情</button>
                <button onclick="showInjectDialog(${m.id})" class="btn-primary">导入剧本</button>
                <button onclick="deleteMaterial(${m.id})" class="btn-secondary">删除</button>
            </div>
        </div>
    `).join('');
}

async function viewMaterial(id) {
    try {
        const m = await API.get(`/api/search/materials/${id}`);
        const modal = document.getElementById('material-detail-modal');
        const content = document.getElementById('material-detail-content');
        // S2: 安全处理 source_url，并对所有用户内容转义
        const safeSourceUrl = m.source_url && /^https?:\/\//i.test(m.source_url) ? m.source_url : '';
        const sourceLink = safeSourceUrl
            ? `| <a href="${escapeHtml(safeSourceUrl)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">来源链接</a>`
            : '';
        content.innerHTML = `
            <h3>${escapeHtml(m.title)}</h3>
            <div class="result-source">${escapeHtml(m.source_type || '')} ${sourceLink}</div>
            <div class="tags-area" style="margin:0.5rem 0">
                ${(m.tags || []).map(t => `<span class="tag" data-tag="${escapeHtml(t)}">${escapeHtml(t)} <span class="tag-remove" onclick="removeTagFromMaterial(${id},this.parentNode.dataset.tag)">&times;</span></span>`).join('')}
                <input type="text" id="new-tag-input" placeholder="添加标签..." style="width:120px;padding:0.2rem 0.5rem;background:var(--bg-card);color:var(--text-primary);border:1px solid var(--border);border-radius:12px;font-size:0.75rem"
                    onkeypress="if(event.key==='Enter')addTagToMaterial(${id},this.value)">
            </div>
            ${m.summary ? `<h4 style="margin-top:1rem">AI摘要</h4><p style="color:var(--text-secondary);line-height:1.6">${escapeHtml(m.summary)}</p>` : ''}
            <h4 style="margin-top:1rem">原始内容</h4>
            <div style="color:var(--text-secondary);line-height:1.6;white-space:pre-wrap;max-height:400px;overflow-y:auto">${escapeHtml(m.content || '')}</div>
        `;
        modal.style.display = 'flex';
    } catch (e) {
        _notify('加载失败: ' + e.message, 'error');
    }
}

function closeMaterialDetail() {
    document.getElementById('material-detail-modal').style.display = 'none';
}

async function addTagToMaterial(id, tag) {
    tag = tag.trim();
    if (!tag) return;
    try {
        await API.post(`/api/search/materials/${id}/tags`, { tags: [tag] });
        viewMaterial(id); // Refresh
        loadTags();
    } catch (e) {
        _notify('添加标签失败: ' + e.message, 'error');
    }
}

async function removeTagFromMaterial(id, tag) {
    try {
        await API.del(`/api/search/materials/${id}/tags/${encodeURIComponent(tag)}`);
        viewMaterial(id);
        loadTags();
    } catch (e) {
        _notify('删除标签失败: ' + e.message, 'error');
    }
}

async function deleteMaterial(id) {
    if (!confirm('确定删除此素材？')) return;
    try {
        await API.del(`/api/search/materials/${id}`);
        loadMaterials();
    } catch (e) {
        _notify('删除失败: ' + e.message, 'error');
    }
}

// --- Inject / Import to script ---

async function showInjectDialog(materialId) {
    const modal = document.getElementById('material-detail-modal');
    const content = document.getElementById('material-detail-content');

    const isEditing = typeof editingScriptId !== 'undefined' && editingScriptId;

    let html = '<h3>导入素材到剧本</h3>';

    if (isEditing) {
        html += '<p style="color:var(--text-secondary);margin:0.5rem 0">将素材AI转化后导入到当前编辑中的剧本：</p>';
    } else {
        try {
            const scripts = await API.get('/api/scripts');
            html += `<div class="form-group"><label>选择目标剧本:</label>
                <select id="inject-script-select">
                    ${(Array.isArray(scripts) ? scripts : []).map(s => `<option value="${s.id}">${escapeHtml(s.name || s.id)}</option>`).join('')}
                </select></div>
                <p style="color:var(--text-secondary);margin:0.5rem 0">素材将直接写入选中的剧本：</p>`;
        } catch (e) {
            html += '<p style="color:var(--accent)">加载剧本列表失败</p>';
        }
    }

    html += `<div class="inject-options">
        <div class="inject-option" onclick="doInject(${materialId},'background',${isEditing})">
            <strong>世界背景</strong> — 补充到世界观描述</div>
        <div class="inject-option" onclick="doInject(${materialId},'location',${isEditing})">
            <strong>新地点</strong> — AI转化为地点</div>
        <div class="inject-option" onclick="doInject(${materialId},'npc',${isEditing})">
            <strong>新NPC</strong> — AI转化为角色</div>
        <div class="inject-option" onclick="doInject(${materialId},'event',${isEditing})">
            <strong>新事件</strong> — AI转化为事件</div>
        <div class="inject-option" onclick="doInject(${materialId},'lorebook',${isEditing})">
            <strong>知识库词条</strong> — AI转化为lorebook词条</div>
    </div>`;

    content.innerHTML = html;
    modal.style.display = 'flex';
}

async function doInject(materialId, target, toWizard) {
    if (toWizard) {
        await doInjectToWizard(materialId, target);
    } else {
        const scriptId = document.getElementById('inject-script-select')?.value;
        if (!scriptId) { _notify('请选择剧本', 'warning'); return; }
        try {
            await API.post('/api/search/inject', {
                material_id: materialId, script_id: scriptId, target,
            });
            _notify('注入成功！', 'success');
            closeMaterialDetail();
        } catch (e) {
            _notify('注入失败: ' + e.message, 'error');
        }
    }
}

async function doInjectToWizard(materialId, target) {
    const content = document.getElementById('material-detail-content');
    content.innerHTML = '<div class="loading"></div> 正在AI转化素材，请稍候...';

    const script = await buildScriptFromWizard();

    try {
        const result = await API.post('/api/search/transform', {
            material_id: materialId, target, script,
        });
        closeMaterialDetail();
        switchPanel('scripts');
        _applyTransformedToWizard(result.transformed, target);
    } catch (e) {
        closeMaterialDetail();
        _notify('素材转化失败: ' + e.message, 'error');
    }
}

function _applyTransformedToWizard(dataList, target) {
    const items = Array.isArray(dataList) ? dataList : [dataList];
    const tabMap = {
        background: 'world', location: 'world', npc: 'characters',
        event: 'events', lorebook: 'lorebook', organization: 'organizations',
        random_item: 'dice',
    };
    const tab = tabMap[target] || 'world';

    let wrapped = {};
    if (target === 'background') {
        const existing = document.getElementById('w-background')?.value || '';
        const newTexts = items.map(d => d.content || '').filter(Boolean);
        const combined = newTexts.filter(t => !existing.includes(t)).join('\n\n');
        if (combined) {
            document.getElementById('w-background').value = existing + (existing ? '\n\n' : '') + combined;
        }
        _notify('世界背景已更新', 'success');
        wizardStep(tab);
        return;
    } else if (target === 'location') {
        wrapped.locations = items;
    } else if (target === 'npc') {
        wrapped.npcs = items;
    } else if (target === 'event') {
        wrapped.one_time_events = items;
    } else if (target === 'lorebook') {
        wrapped.lorebook = items;
    } else if (target === 'organization') {
        wrapped.organizations = items;
    } else if (target === 'random_item') {
        wrapped.random_items = items;
    }

    wizardStep(tab);
    const snapshot = _snapshotTab(tab);
    const mergeInfo = _smartMergeTab(tab, wrapped);
    _switchCopilotMode('result');
    _renderMergeSummary(tab, mergeInfo, snapshot);
    const label = _TAB_LABELS[tab] || tab;
    _notify(`素材已导入 ${items.length} 条到「${label}」`, 'success');
}

// --- Material picker modal (for wizard tab-gen-bar) ---

async function openMaterialPicker(tab) {
    const tabTargetMap = {
        characters: 'npc', world: 'location', organizations: 'organization',
        events: 'event', lorebook: 'lorebook', dice: 'random_item',
    };
    const target = tabTargetMap[tab] || 'background';

    const modal = document.getElementById('material-detail-modal');
    const content = document.getElementById('material-detail-content');

    content.innerHTML = '<div class="loading"></div> 加载素材库...';
    modal.style.display = 'flex';

    try {
        const result = await API.get('/api/search/materials');
        const materials = result.materials || [];
        if (materials.length === 0) {
            content.innerHTML = '<h3>从素材库导入</h3><p class="placeholder-text">素材库为空，请先通过素材面板搜索并收藏素材。</p>';
            return;
        }
        const label = _TAB_LABELS[tab] || tab;
        let html = `<h3>选择素材导入到「${escapeHtml(label)}」</h3>`;
        html += '<p style="color:var(--text-secondary);font-size:0.82rem;margin-bottom:0.5rem">可勾选多个素材，每个素材将分别转化为独立的条目。</p>';
        html += '<div class="material-picker-list">';
        for (const m of materials) {
            html += `<label class="material-picker-item">
                <input type="checkbox" class="mp-cb" value="${m.id}" data-target="${escapeAttr(target)}">
                <div>
                    <div class="mp-title">${escapeHtml(m.title)}</div>
                    <div class="mp-source">${escapeHtml(m.source_type || '')} | ${escapeHtml((m.summary || m.content || '').substring(0, 100))}...</div>
                </div>
            </label>`;
        }
        html += '</div>';
        html += `<div style="margin-top:0.8rem;text-align:right">
            <button class="btn-primary" onclick="doPickMaterialBatch('${escapeAttr(target)}')">导入选中素材</button>
        </div>`;
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<p style="color:var(--accent)">加载失败: ${e.message}</p>`;
    }
}

async function doPickMaterialBatch(target) {
    const checkboxes = document.querySelectorAll('.mp-cb:checked');
    if (checkboxes.length === 0) {
        _notify('请至少选择一个素材', 'warning');
        return;
    }

    const ids = [...checkboxes].map(cb => cb.value);
    const content = document.getElementById('material-detail-content');
    content.innerHTML = `<div class="loading"></div> 正在AI转化 ${ids.length} 个素材，请稍候...`;

    let successCount = 0;
    for (const id of ids) {
        try {
            // 每次循环重建 script，包含前一轮 merge 的结果
            const currentScript = await buildScriptFromWizard();
            const result = await API.post('/api/search/transform', {
                material_id: parseInt(id), target, script: currentScript,
            });
            _applyTransformedToWizard(result.transformed, target);
            successCount++;
            content.innerHTML = `<div class="loading"></div> 已完成 ${successCount}/${ids.length} ...`;
        } catch (e) {
            _notify(`素材 #${id} 转化失败: ${e.message}`, 'error');
        }
    }
    closeMaterialDetail();
    if (successCount > 0) {
        _notify(`成功导入 ${successCount} 个素材`, 'success');
    }
}

// --- Generate script from selected materials ---

async function aiGenerateFromMaterials() {
    const checkboxes = document.querySelectorAll('.material-select-cb:checked');
    if (checkboxes.length === 0) {
        _notify('请先勾选至少一个素材', 'warning');
        return;
    }

    const materialIds = [...checkboxes].map(cb => cb.value);
    _notify(`正在加载 ${materialIds.length} 个素材...`, 'info');

    const contents = [];
    for (const id of materialIds) {
        try {
            const m = await API.get(`/api/search/materials/${id}`);
            contents.push(`【${m.title}】\n${m.content}`);
        } catch (e) { /* skip failed */ }
    }
    if (contents.length === 0) {
        _notify('未能加载任何素材', 'error');
        return;
    }

    const prompt = `请基于以下素材内容，生成一个完整的游戏世界剧本：\n\n${contents.join('\n\n---\n\n')}`;

    switchPanel('scripts');

    // Show the AI full generate modal with pre-filled prompt
    document.getElementById('ai-generate-modal').style.display = 'flex';
    document.getElementById('ai-full-prompt').value = prompt;
    _notify('已预填素材内容，点击「开始生成」即可', 'success');
}

// escapeHtml is defined in app.js

// --- Manual add & file import ---

function showManualAddMaterial() {
    const modal = document.getElementById('material-detail-modal');
    const content = document.getElementById('material-detail-content');
    content.innerHTML = `
        <h3>手动添加素材</h3>
        <div class="form-group">
            <label>标题</label>
            <input type="text" id="manual-material-title" placeholder="素材标题" style="width:100%">
        </div>
        <div class="form-group">
            <label>内容</label>
            <textarea id="manual-material-content" rows="12" placeholder="粘贴或输入素材文本..." style="width:100%;resize:vertical"></textarea>
        </div>
        <div style="text-align:right;margin-top:0.8rem">
            <button class="btn-secondary" onclick="closeMaterialDetail()">取消</button>
            <button class="btn-primary" onclick="doManualAddMaterial()">保存</button>
        </div>
    `;
    modal.style.display = 'flex';
    setTimeout(() => document.getElementById('manual-material-title')?.focus(), 100);
}

async function doManualAddMaterial() {
    const title = document.getElementById('manual-material-title').value.trim();
    const contentText = document.getElementById('manual-material-content').value.trim();
    if (!contentText) {
        _notify('内容不能为空', 'warning');
        return;
    }
    try {
        await API.post('/api/search/materials', {
            title: title || '手动素材',
            content: contentText,
            source_type: 'manual',
        });
        closeMaterialDetail();
        _notify('素材已添加', 'success');
        loadMaterials();
    } catch (e) {
        _notify('保存失败: ' + e.message, 'error');
    }
}

function triggerMaterialFileImport() {
    const input = document.getElementById('material-file-input');
    input.value = '';
    input.click();
}

async function handleMaterialFileImport(input) {
    const files = input.files;
    if (!files || files.length === 0) return;

    let successCount = 0;
    for (const file of files) {
        const formData = new FormData();
        formData.append('file', file);

        try {
            const token = localStorage.getItem('tavern_api_token');
            const headers = {};
            if (token) headers['Authorization'] = `Bearer ${token}`;

            const resp = await fetch('/api/search/materials/upload', {
                method: 'POST',
                headers,
                body: formData,
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${resp.status}`);
            }
            successCount++;
        } catch (e) {
            _notify(`导入 "${file.name}" 失败: ${e.message}`, 'error');
        }
    }
    if (successCount > 0) {
        _notify(`成功导入 ${successCount} 个文件`, 'success');
        loadMaterials();
    }
}
