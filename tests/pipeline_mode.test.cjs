const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const apiSource = fs.readFileSync(path.join(root, 'static/js/app.js'), 'utf8').split('// Attribute display')[0];
const modeSource = fs.readFileSync(path.join(root, 'static/js/pipeline_mode.js'), 'utf8');

function setup(responses, saveId = 'first') {
    const classes = new Set();
    const button = {
        textContent: '', disabled: false, title: '',
        classList: { add: value => classes.add(value), remove: (...values) => values.forEach(value => classes.delete(value)) },
        setAttribute() {}, removeAttribute() {},
    };
    const streamButton = { style: { display: '' } };
    const requests = [];
    const alerts = [];
    const context = vm.createContext({
        currentSaveId: saveId, currentState: {}, isStreaming: false, cachedMode: 'workflow',
        localStorage: { getItem: () => 'test-token' },
        document: { getElementById: id => id === 'pipeline-mode-btn' ? button : streamButton },
        alert: message => alerts.push(message),
        _updateCachedPipelineMode(mode) { context.cachedMode = mode; },
        fetch: async (url, options) => {
            requests.push({ url, options });
            const response = responses.shift();
            if (typeof response === 'function') return response();
            return { ok: response.status === undefined || response.status < 400, status: response.status || 200, json: async () => response.body };
        },
    });
    vm.runInContext(apiSource + '\n' + modeSource, context);
    return { context, button, streamButton, requests, alerts, classes };
}

test('session toggle uses authenticated requests and persists only this save', async () => {
    const view = setup([{ body: { mode: 'workflow' } }, { body: { mode: 'agentic' } }]);
    await view.context.togglePipelineMode();
    assert.equal(view.requests[0].url, '/api/config/pipeline-mode?save_id=first');
    assert.deepEqual(JSON.parse(view.requests[1].options.body), { mode: 'agentic', save_id: 'first' });
    for (const request of view.requests) assert.equal(request.options.headers.Authorization, 'Bearer test-token');
    assert.equal(view.context.cachedMode, 'agentic');
    assert.equal(view.context.currentState._pipeline_mode, 'agentic');
    assert.equal(view.streamButton.style.display, 'none');
    assert.equal(view.button.disabled, false);
    assert.match(view.button.title, /只影响此存档/);
});

test('home toggle changes the new-game default without a save id', async () => {
    const view = setup([{ body: { mode: 'agentic_unified' } }, { body: { mode: 'workflow' } }], null);
    await view.context.togglePipelineMode();
    assert.equal(view.requests[0].url, '/api/config/pipeline-mode');
    assert.deepEqual(JSON.parse(view.requests[1].options.body), { mode: 'workflow' });
    assert.match(view.button.title, /不影响已有存档/);
    assert.equal(view.streamButton.style.display, '');
});

test('failed POST does not display a successful switch', async () => {
    const view = setup([{ body: { mode: 'workflow' } }, { status: 409, body: { detail: '本轮仍在推演' } }]);
    view.context.updatePipelineModeBtn('workflow');
    await view.context.togglePipelineMode();
    assert.equal(view.context.cachedMode, 'workflow');
    assert.match(view.button.textContent, /流程/);
    assert.match(view.alerts[0], /本轮仍在推演/);
    assert.equal(view.button.disabled, false);
});

test('authentication failure prevents POST', async () => {
    const view = setup([{ status: 401, body: { detail: 'Unauthorized' } }]);
    await view.context.togglePipelineMode();
    assert.equal(view.requests.length, 1);
    assert.equal(view.context.cachedMode, 'workflow');
    assert.match(view.alerts[0], /401/);
});

test('generation blocks mode switching', async () => {
    const view = setup([]);
    view.context.isStreaming = true;
    await view.context.togglePipelineMode();
    assert.equal(view.requests.length, 0);
    assert.match(view.alerts[0], /等待本轮完成/);
});

test('duplicate clicks send one mutation', async () => {
    let finishRead;
    const view = setup([
        () => new Promise(resolve => { finishRead = resolve; }),
        { body: { mode: 'agentic' } },
    ]);
    const pending = view.context.togglePipelineMode();
    assert.equal(view.button.disabled, true);
    await view.context.togglePipelineMode();
    finishRead({ ok: true, json: async () => ({ mode: 'workflow' }) });
    await pending;
    assert.equal(view.requests.length, 2);
});

test('navigation during GET cancels the stale mutation', async () => {
    let finishRead;
    const view = setup([() => new Promise(resolve => { finishRead = resolve; })]);
    const pending = view.context.togglePipelineMode();
    view.context.currentSaveId = 'second';
    view.context.updatePipelineModeBtn('agentic_unified');
    finishRead({ ok: true, json: async () => ({ mode: 'workflow' }) });
    await pending;
    assert.equal(view.requests.length, 1);
    assert.equal(view.context.cachedMode, 'agentic_unified');
});

test('navigation during POST does not overwrite the new save indicator', async () => {
    let finishWrite;
    const view = setup([
        { body: { mode: 'workflow' } },
        () => new Promise(resolve => { finishWrite = resolve; }),
    ]);
    const pending = view.context.togglePipelineMode();
    await new Promise(resolve => setImmediate(resolve));
    view.context.currentSaveId = 'second';
    view.context.updatePipelineModeBtn('workflow');
    finishWrite({ ok: true, json: async () => ({ mode: 'agentic' }) });
    await pending;
    assert.equal(view.context.cachedMode, 'workflow');
    assert.equal(view.context.currentState._pipeline_mode, undefined);
});

test('home refresh rejects a late response after entering a game', async () => {
    let finishRead;
    const view = setup([() => new Promise(resolve => { finishRead = resolve; })], null);
    const pending = view.context.refreshPipelineMode();
    view.context.currentSaveId = 'first';
    view.context.updatePipelineModeBtn('agentic');
    finishRead({ ok: true, json: async () => ({ mode: 'workflow' }) });
    await pending;
    assert.equal(view.context.cachedMode, 'agentic');
});

test('failed refresh reports unsynchronized state', async () => {
    const view = setup([{ status: 401, body: {} }]);
    await view.context.refreshPipelineMode();
    assert.equal(view.button.textContent, '模式未同步');
    assert.match(view.button.title, /401/);
});
