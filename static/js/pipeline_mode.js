const _PIPELINE_MODES = ['workflow', 'agentic', 'agentic_unified'];
let _pipelineModeBusy = false;
let _pipelineModeRequest = 0;

function _pipelineModeUrl(saveId) {
    return '/api/config/pipeline-mode' + (saveId ? `?save_id=${encodeURIComponent(saveId)}` : '');
}

async function refreshPipelineMode() {
    const saveId = currentSaveId;
    const request = ++_pipelineModeRequest;
    try {
        const data = await API.get(_pipelineModeUrl(saveId));
        if (request === _pipelineModeRequest && saveId === currentSaveId) {
            updatePipelineModeBtn(data.mode);
        }
    } catch (error) {
        if (request !== _pipelineModeRequest || saveId !== currentSaveId) return;
        const button = document.getElementById('pipeline-mode-btn');
        if (button) {
            button.textContent = '模式未同步';
            button.title = `读取推演模式失败：${error.message}。点击重试切换。`;
        }
    }
}

async function togglePipelineMode() {
    if (_pipelineModeBusy) return;
    if (isStreaming) {
        alert('正在推演，请等待本轮完成后再切换模式。');
        return;
    }
    const button = document.getElementById('pipeline-mode-btn');
    const saveId = currentSaveId;
    const request = ++_pipelineModeRequest;
    _pipelineModeBusy = true;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    try {
        const data = await API.get(_pipelineModeUrl(saveId));
        if (request !== _pipelineModeRequest || saveId !== currentSaveId) return;
        if (isStreaming) throw new Error('正在推演，请等待本轮完成后再切换模式。');
        const index = _PIPELINE_MODES.indexOf(data.mode);
        if (index < 0) throw new Error('服务器返回了未知推演模式');
        const payload = { mode: _PIPELINE_MODES[(index + 1) % _PIPELINE_MODES.length] };
        if (saveId) payload.save_id = saveId;
        const result = await API.post('/api/config/pipeline-mode', payload);
        if (request !== _pipelineModeRequest || saveId !== currentSaveId) return;
        updatePipelineModeBtn(result.mode);
        if (saveId && currentState) currentState._pipeline_mode = result.mode;
    } catch (error) {
        if (saveId === currentSaveId) alert('切换推演模式失败：' + error.message);
    } finally {
        _pipelineModeBusy = false;
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}

function updatePipelineModeBtn(mode) {
    if (!_PIPELINE_MODES.includes(mode)) return;
    const button = document.getElementById('pipeline-mode-btn');
    if (button) {
        button.classList.remove('mode-workflow', 'mode-agentic', 'mode-unified');
        const labels = {
            workflow: ['⚡ 流程', 'mode-workflow'],
            agentic: ['🤖 双 Agent', 'mode-agentic'],
            agentic_unified: ['✨ 统一 Agent', 'mode-unified'],
        };
        const [label, className] = labels[mode];
        button.textContent = label;
        button.classList.add(className);
        button.title = currentSaveId
            ? '当前存档的推演模式；切换只影响此存档，下次行动生效'
            : '新游戏的默认推演模式；不影响已有存档';
    }
    _updateCachedPipelineMode(mode);
    const streamButton = document.getElementById('stream-toggle');
    if (streamButton) streamButton.style.display = mode === 'workflow' ? '' : 'none';
}
