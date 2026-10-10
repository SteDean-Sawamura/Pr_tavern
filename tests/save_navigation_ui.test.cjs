const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');

function setup() {
    const requests = [];
    const rendered = [];
    const resetScrollFlags = [];
    const alerts = [];
    const panels = [];
    const context = vm.createContext({
        API: { get: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })) },
        window: {},
        showGameScreen: (data, resetScroll) => { rendered.push(data); resetScrollFlags.push(resetScroll); }, loadSidebarSaves() {},
        switchPanel: panel => panels.push(panel),
        alert: message => alerts.push(message),
    });
    vm.runInContext(
        source.slice(source.indexOf('// Global state'), source.indexOf('// --- Panel switching ---')) +
        'let _npcChatId = null; let _npcChatRequest = null;\n' +
        source.slice(source.indexOf('function cancelActiveGameStream() {'), source.indexOf('async function continueGame(saveId) {')) +
        source.slice(source.indexOf('async function continueGame(saveId) {'), source.indexOf('async function deleteSave(')),
        context,
    );
    vm.runInContext("currentSaveId = 'shown'; currentState = { scene: 'shown' }", context);
    return { context, requests, rendered, resetScrollFlags, alerts, panels };
}

function resolveSave(view, saveId) {
    view.requests.find(request => request.url.startsWith(`/api/game/${saveId}/snapshot?last=10`))
        .resolve({ state: { scene: saveId }, node_id: `${saveId}-node`, history: { entries: [], total: 0 } });
}

test('save list exposes native buttons for loading, exporting and deleting', async () => {
    const parsedDates = [];
    const elements = new Map([
        ['script-select', { value: '', innerHTML: '', appendChild() {} }],
        ['continue-saves', { innerHTML: '' }],
        ['character-select-area', { style: {} }],
        ['preset-cards', { innerHTML: '' }],
        ['custom-char-area', { style: {} }],
        ['sidebar-save-list', { innerHTML: '' }],
        ['script-selection-status', {}],
        ['btn-retry-script', {}],
    ]);
    const context = vm.createContext({
        currentSaveId: null,
        _gameStartSequence: 0, onScriptSelected() {},
        Date: class extends Date {
            constructor(value) { super(value); parsedDates.push(value); }
        },
        document: { getElementById: id => elements.get(id) },
        API: { get: async url => url === '/api/saves' ? [{ id: 'first', name: '设"甲<', total_nodes: 2, updated_at: '2026-10-01 10:28:16' }] : [] },
        refreshPipelineMode() {}, _formatPlayTime() {}, console,
    });
    vm.runInContext(
        source.slice(source.indexOf('function _formatPlayTime('), source.indexOf('async function loadGameStart() {')) +
        source.slice(source.indexOf('async function loadGameStart() {'), source.indexOf('let _selectedPresetId')) +
        source.slice(source.indexOf('function escapeHtml(text) {'), source.indexOf('function toggleSidebar()')) +
        source.slice(source.indexOf('async function loadSidebarSaves() {'), source.indexOf('async function loadDataBankFiles()')) +
        source.slice(source.indexOf('function escapeAttr(str) {'), source.indexOf('// Delegate click on entity-link spans')),
        context,
    );
    await context.loadGameStart();
    await context.loadSidebarSaves();
    const main = elements.get('continue-saves').innerHTML;
    const sidebar = elements.get('sidebar-save-list').innerHTML;
    assert.match(main, /<div class="save-card">\s*<button type="button" class="save-open"/);
    assert.match(main, /aria-label="继续游戏：设&quot;甲&lt;"/);
    assert.match(main, /保存于 10\/1/);
    assert.match(main, /<button type="button" class="save-export"/);
    assert.match(main, /<button type="button" class="save-delete"/);
    assert.doesNotMatch(main, /<span class="save-(?:export|delete)"/);
    assert.match(sidebar, /<button type="button" class="save-card sidebar-save"/);
    assert.match(sidebar, /保存于 10\/1/);
    assert.deepEqual(parsedDates, ['2026-10-01T10:28:16Z', '2026-10-01T10:28:16Z']);
});

test('save time preserves timestamps with explicit timezones', () => {
    const parsedDates = [];
    const context = vm.createContext({
        Date: class extends Date {
            constructor(value) { super(value); parsedDates.push(value); }
        },
    });
    vm.runInContext(source.slice(source.indexOf('function _formatSaveTime('), source.indexOf('async function loadGameStart() {')), context);
    context._formatSaveTime('2026-10-01T10:28:16Z');
    context._formatSaveTime('2026-10-01T18:28:16+08:00');
    assert.deepEqual(parsedDates, ['2026-10-01T10:28:16Z', '2026-10-01T18:28:16+08:00']);
});

test('latest save selection wins when requests finish out of order', async () => {
    const view = setup();
    const first = view.context.continueGame('first');
    const second = view.context.continueGame('second');
    resolveSave(view, 'second');
    await second;
    resolveSave(view, 'first');
    await first;
    assert.equal(vm.runInContext('currentSaveId', view.context), 'second');
    assert.equal(vm.runInContext('currentState.scene', view.context), 'second');
    assert.deepEqual(view.rendered.map(data => data.state.scene), ['second']);
    assert.deepEqual(view.panels, ['game']);
});

test('switching saves resets the outer scroll but reloading one does not', async () => {
    const view = setup();
    const first = view.context.continueGame('first');
    resolveSave(view, 'first');
    await first;
    const reload = view.context.continueGame('first');
    view.requests.at(-1).resolve({ state: { scene: 'first' }, node_id: 'first-node', history: { entries: [], total: 0 } });
    await reload;
    assert.deepEqual(view.resetScrollFlags, [true, false]);
});

test('reloading a waiting stream cancels it before requesting its locked snapshot', async () => {
    const view = setup();
    let cancelled = false;
    view.context.window._activeGameAction = { cancel() { cancelled = true; } };
    const reload = view.context.continueGame('shown');
    assert.equal(cancelled, true);
    assert.equal(view.requests.length, 1);
    resolveSave(view, 'shown');
    await reload;
});

test('loading a save resumes its pending action only at the same node', async () => {
    const view = setup();
    const pending = { action: { type: 'freeform', text: 'Go' },
        actionId: 'action-1', streamId: 'stream-1', baseNodeId: 'first-node' };
    const storage = new Map([['tavern-pending-action:first', JSON.stringify(pending)]]);
    const resumed = [];
    view.context.sessionStorage = {
        getItem: key => storage.get(key), removeItem: key => storage.delete(key),
    };
    view.context.submitActionStream = (...args) => resumed.push(args);
    const loading = view.context.continueGame('first');
    resolveSave(view, 'first');
    await loading;
    assert.equal(resumed.length, 1);
    assert.equal(resumed[0][2].actionId, 'action-1');

    const advanced = view.context.continueGame('first');
    view.requests.at(-1).resolve({ state: {}, node_id: 'next-node', history: { entries: [], total: 0 } });
    await advanced;
    assert.equal(resumed.length, 1);
    assert.equal(storage.size, 0);
});

test('another tab joins the pending action exposed by the save snapshot', async () => {
    const view = setup();
    const resumed = [];
    view.context.submitActionStream = (...args) => resumed.push(args);
    const loading = view.context.continueGame('first');
    view.requests[0].resolve({ state: {}, node_id: 'first-node', history: { entries: [], total: 0 },
        pending_action: { action: { type: 'freeform', text: 'Go' },
            actionId: 'action-1', streamId: 'stream-1', baseNodeId: 'first-node' } });
    await loading;
    assert.equal(resumed.length, 1);
    assert.equal(resumed[0][0].text, 'Go');
    assert.equal(resumed[0][2].actionId, 'action-1');
    assert.equal(resumed[0][2].streamId, 'stream-1');
});

test('a completed same-node query does not replay from session storage', async () => {
    const view = setup();
    const pending = { action: { type: 'freeform', text: 'What happened?' },
        actionId: 'completed-query', streamId: 'stream-1', baseNodeId: 'first-node' };
    const storage = new Map([['tavern-pending-action:first', JSON.stringify(pending)]]);
    const resumed = [];
    view.context.sessionStorage = {
        getItem: key => storage.get(key), removeItem: key => storage.delete(key),
    };
    view.context.submitActionStream = (...args) => resumed.push(args);
    const loading = view.context.continueGame('first');
    assert.match(view.requests[0].url, /action_id=completed-query/);
    view.requests[0].resolve({ state: {}, node_id: 'first-node',
        history: { entries: [{ query_exchanges: [{ question: 'What happened?', answer: 'It passed.' }] }], total: 1 },
        action_completed: true });
    await loading;
    assert.deepEqual(resumed, []);
    assert.equal(storage.size, 0);
    assert.equal(view.rendered[0].history[0].query_exchanges[0].answer, 'It passed.');
});

test('a completed local query yields to a different server pending action', async () => {
    const view = setup();
    const pending = { action: { type: 'freeform', text: 'Old query' },
        actionId: 'completed-query', streamId: 'old-stream', baseNodeId: 'first-node' };
    const storage = new Map([['tavern-pending-action:first', JSON.stringify(pending)]]);
    const resumed = [];
    view.context.sessionStorage = {
        getItem: key => storage.get(key), removeItem: key => storage.delete(key),
    };
    view.context.submitActionStream = (...args) => resumed.push(args);
    const loading = view.context.continueGame('first');
    view.requests[0].resolve({ state: {}, node_id: 'first-node', history: { entries: [], total: 0 },
        action_completed: true, pending_action: { action: { type: 'freeform', text: 'New action' },
            actionId: 'new-action', streamId: 'new-stream', baseNodeId: 'first-node' } });
    await loading;
    assert.equal(storage.size, 0);
    assert.equal(resumed.length, 1);
    assert.equal(resumed[0][2].actionId, 'new-action');
});

test('a stale pending action from the server does not replay on a newer node', async () => {
    const view = setup();
    const resumed = [];
    view.context.submitActionStream = (...args) => resumed.push(args);
    const loading = view.context.continueGame('first');
    view.requests[0].resolve({ state: {}, node_id: 'next-node', history: { entries: [], total: 0 },
        pending_action: { action: { type: 'freeform', text: 'Go' },
            actionId: 'action-1', streamId: 'stream-1', baseNodeId: 'first-node' } });
    await loading;
    assert.equal(resumed.length, 0);
});

test('reloading the same save cancels the old subscription before resuming', async () => {
    const view = setup();
    let cancelled = false;
    const pending = { action: { type: 'freeform', text: 'Go' },
        actionId: 'action-1', streamId: 'stream-1', baseNodeId: 'shown-node' };
    const storage = new Map([['tavern-pending-action:shown', JSON.stringify(pending)]]);
    view.context.sessionStorage = { getItem: key => storage.get(key), removeItem: key => storage.delete(key) };
    view.context.window._activeGameAction = { cancel() { cancelled = true; } };
    vm.runInContext('isStreaming = true', view.context);
    view.context.showGameScreen = () => {
        vm.runInContext('window._activeGameAction = null; isStreaming = false', view.context);
    };
    view.context.submitActionStream = (_action, _url, context) => {
        assert.equal(cancelled, true);
        assert.equal(vm.runInContext('isStreaming', view.context), false);
        assert.equal(view.context.window._activeGameAction, null);
        assert.equal(context.actionId, 'action-1');
    };
    const reload = view.context.continueGame('shown');
    resolveSave(view, 'shown');
    await reload;
});

test('switching saves cancels the old stream only after loading succeeds', async () => {
    const view = setup();
    let cancelled = false;
    view.context.window._activeGameAction = { cancel() { cancelled = true; } };
    const switchSave = view.context.continueGame('second');
    assert.equal(cancelled, false);
    resolveSave(view, 'second');
    await switchSave;
    assert.equal(cancelled, true);
    assert.equal(view.rendered[0].state.scene, 'second');
});

test('a failed switch leaves the current stream running', async () => {
    const view = setup();
    let cancelled = false;
    view.context.window._activeGameAction = { cancel() { cancelled = true; } };
    const switchSave = view.context.continueGame('missing');
    view.requests[0].reject(new Error('offline'));
    await switchSave;
    assert.equal(cancelled, false);
});

test('failed load leaves the displayed save active', async () => {
    const view = setup();
    const pending = view.context.continueGame('missing');
    view.requests.find(request => request.url.includes('/snapshot')).reject(new Error('offline'));
    await pending;
    assert.equal(vm.runInContext('currentSaveId', view.context), 'shown');
    assert.equal(vm.runInContext('currentState.scene', view.context), 'shown');
    assert.equal(view.rendered.length, 0);
    assert.match(view.alerts[0], /offline/);
});

test('returning home while a save loads does not reopen the game', async () => {
    const view = setup();
    const pending = view.context.continueGame('first');
    vm.runInContext('currentSaveId = null; currentState = null', view.context);
    resolveSave(view, 'first');
    await pending;
    assert.equal(vm.runInContext('currentSaveId', view.context), null);
    assert.equal(view.rendered.length, 0);
});

test('loading a save without history clears the previously displayed node', async () => {
    const view = setup();
    vm.runInContext("currentNodeId = 'old-node'", view.context);
    const pending = view.context.continueGame('empty');
    view.requests[0].resolve({ state: { scene: 'empty' }, node_id: null, history: { entries: [], total: 0 } });
    await pending;
    assert.equal(vm.runInContext('currentNodeId', view.context), null);
});

test('reloading the same save waits for its pending swipe before reading state', async () => {
    const posts = [];
    const gets = [];
    const rendered = [];
    const context = vm.createContext({
        API: {
            post: url => new Promise((resolve, reject) => posts.push({ url, resolve, reject })),
            get: url => new Promise((resolve, reject) => gets.push({ url, resolve, reject })),
        },
        window: {},
        showGameScreen: data => {
            rendered.push(data);
            vm.runInContext('isStreaming = false', context);
        },
        loadSidebarSaves() {},
        alert() {},
        console: { error() {} },
    });
    vm.runInContext(
        source.slice(source.indexOf('// Global state'), source.indexOf('// --- Panel switching ---')) +
        'let _npcChatId = null; let _npcChatRequest = null;\n' +
        source.slice(source.indexOf('function cancelActiveGameStream() {'), source.indexOf('async function continueGame(saveId) {')) +
        source.slice(source.indexOf('async function continueGame(saveId) {'), source.indexOf('async function deleteSave(')) +
        source.slice(source.indexOf('async function swipeLeft()'), source.indexOf('function applySwipeResult(result)')),
        context,
    );
    vm.runInContext("currentSaveId = 'first'; currentNodeId = 'node-1'; totalSwipes = 2", context);
    context.applySwipeResult = () => assert.fail('a stale swipe response must not overwrite the reload');

    const swipe = context.swipeRight();
    vm.runInContext("currentSaveId = 'second'; currentNodeId = 'node-2'; isStreaming = false", context);
    const otherSwipe = context.swipeLeft();
    vm.runInContext("currentSaveId = 'first'; currentNodeId = 'node-1'; isStreaming = false", context);
    const reload = context.continueGame('first');
    assert.equal(gets.length, 0);
    posts[0].resolve({ narrative: 'swiped' });
    await swipe;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(gets.length, 1);
    posts[1].resolve({ narrative: 'other save swipe' });
    await otherSwipe;
    gets[0].resolve({ state: { scene: 'swiped' }, node_id: 'node-1', history: { entries: [], total: 0 } });
    await reload;
    assert.equal(rendered[0].state.scene, 'swiped');
    assert.equal(vm.runInContext('isStreaming', context), false);
});

test('a failed reload after a pending swipe releases the action guard', async () => {
    const view = setup();
    vm.runInContext("_pendingSwipeRequests.add({ saveId: 'shown', settled: Promise.resolve() }); isStreaming = true", view.context);
    const reload = view.context.continueGame('shown');
    await new Promise(resolve => setImmediate(resolve));
    view.requests.find(request => request.url.includes('/snapshot')).reject(new Error('offline'));
    await reload;
    assert.equal(vm.runInContext('isStreaming', view.context), false);
    assert.match(view.alerts[0], /offline/);
});

test('a superseded reload skips reads after waiting for a swipe', async () => {
    const view = setup();
    vm.runInContext("_pendingSwipeRequests.add({ saveId: 'shown', settled: new Promise(resolve => { finishSwipe = resolve }) })", view.context);
    const oldReload = view.context.continueGame('shown');
    const newReload = view.context.continueGame('second');
    resolveSave(view, 'second');
    await newReload;
    view.context.finishSwipe();
    await oldReload;
    assert.equal(view.requests.length, 1);
    assert.equal(vm.runInContext('currentSaveId', view.context), 'second');
});
