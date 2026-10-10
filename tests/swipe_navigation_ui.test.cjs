const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const swipeHandlers = source.slice(
    source.indexOf('async function swipeLeft()'),
    source.indexOf('function applySwipeResult(result)'),
);
const undoHandler = source.slice(
    source.indexOf('async function undoLastTurn()'),
    source.indexOf('async function openSwipePicker()'),
);

function setup() {
    const requests = [];
    const applied = [];
    const errors = [];
    const refreshed = [];
    const notifications = [];
    const context = vm.createContext({
        currentSaveId: 'first', currentNodeId: 'node-1', _saveLoadSequence: 0,
        _pendingSwipeRequests: new Set(),
        currentSwipeIndex: 0, totalSwipes: 2, isStreaming: false,
        API: { post: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })) },
        document: { getElementById: () => null },
        console: { error: (...args) => errors.push(args) },
        continueGame: async saveId => {
            refreshed.push(saveId);
            context._saveLoadSequence++;
            context.isStreaming = false;
            return true;
        },
        showNotification: (...args) => notifications.push(args),
    });
    vm.runInContext(swipeHandlers, context);
    context.applySwipeResult = result => applied.push(result);
    return { context, requests, applied, errors, refreshed, notifications };
}

test('swipe requests exclude duplicate clicks and other actions until settled', async () => {
    const view = setup();
    const pending = view.context.swipeRight();
    const duplicate = view.context.swipeRight();
    const opposite = view.context.swipeLeft();
    const jump = view.context.jumpToSwipe(0);
    assert.equal(view.requests.length, 1);
    assert.equal(view.requests[0].url,
        '/api/game/first/swipe/right?expected_node_id=node-1&expected_swipe_index=0&expected_total_swipes=2');
    assert.equal(view.context.isStreaming, true);
    view.requests[0].resolve({ narrative: 'second branch' });
    await Promise.all([pending, duplicate, opposite, jump]);
    assert.equal(view.applied.length, 1);
    assert.equal(view.context.isStreaming, false);
});

test('swipe response cannot overwrite another loaded save', async () => {
    const view = setup();
    const pending = view.context.swipeLeft();
    view.context.currentSaveId = 'second';
    view.context.isStreaming = false;
    const second = view.context.swipeRight();
    view.requests[0].resolve({ narrative: 'first save branch' });
    await pending;
    assert.equal(view.applied.length, 0);
    assert.equal(view.context.isStreaming, true);
    view.requests[1].resolve({ narrative: 'second save branch' });
    await second;
    assert.deepEqual(view.applied, [{ narrative: 'second save branch' }]);
    assert.equal(view.context.isStreaming, false);
});

test('swipe response from a previous load cannot overwrite the same save or unlock a new request', async () => {
    const view = setup();
    const oldRequest = view.context.swipeRight();
    view.context._saveLoadSequence++;
    view.context.isStreaming = false;
    const newRequest = view.context.swipeLeft();
    view.requests[0].resolve({ narrative: 'Previous load' });
    await oldRequest;
    assert.equal(view.applied.length, 0);
    assert.equal(view.context.isStreaming, true);
    view.requests[1].resolve({ narrative: 'Current load' });
    await newRequest;
    assert.deepEqual(view.applied, [{ narrative: 'Current load' }]);
    assert.equal(view.context.isStreaming, false);
});

test('swipe response from a previous node cannot overwrite the current turn', async () => {
    const view = setup();
    const pending = view.context.swipeRight();
    view.context.currentNodeId = 'node-2';
    view.requests[0].resolve({ narrative: 'Previous turn' });
    await pending;
    assert.equal(view.applied.length, 0);
});

test('failed swipe releases the action guard', async () => {
    const view = setup();
    const pending = view.context.jumpToSwipe(1);
    view.requests[0].reject(new Error('network failure'));
    await pending;
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.errors.length, 1);
});

test('stale swipe conflict reloads the current save before another action', async () => {
    const view = setup();
    const pending = view.context.swipeRight();
    view.requests[0].reject(Object.assign(new Error('version conflict'), { status: 409 }));
    await pending;
    assert.deepEqual(view.refreshed, ['first']);
    assert.equal(view.applied.length, 0);
    assert.equal(view.context.isStreaming, false);
    assert.match(view.notifications[0][0], /重新选择/);
});

test('failed conflict refresh does not leave the action locked', async () => {
    const view = setup();
    view.context.continueGame = async () => {
        view.context._saveLoadSequence++;
        return false;
    };
    const pending = view.context.swipeRight();
    view.requests[0].reject(Object.assign(new Error('version conflict'), { status: 409 }));
    await pending;
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.notifications.length, 0);
});

test('API POST keeps HTTP status on a conflict error', async () => {
    const context = vm.createContext({
        localStorage: { getItem: () => null },
        fetch: async () => ({ ok: false, status: 409, json: async () => ({ detail: 'version conflict' }) }),
    });
    vm.runInContext(source.slice(0, source.indexOf('// Attribute display name')), context);
    await assert.rejects(vm.runInContext("API.post('/api/game/swipe')", context), error => {
        assert.equal(error.status, 409);
        assert.equal(error.message, 'version conflict');
        return true;
    });
});

function setupPicker() {
    const gets = [];
    const posts = [];
    const modals = [];
    const list = { listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; } };
    const gamePanel = { active: true, classList: { contains: name => name === 'active' && gamePanel.active } };
    const document = {
        getElementById: id => id === 'panel-game' ? gamePanel : modals.find(modal => modal.id === id) || null,
        createElement: () => ({
            querySelector: () => list,
            contains: () => true,
            remove() { modals.splice(modals.indexOf(this), 1); },
        }),
        body: { appendChild: modal => modals.push(modal) },
    };
    const context = vm.createContext({
        currentSaveId: 'first', currentNodeId: 'node-1', _saveLoadSequence: 0,
        _pendingSwipeRequests: new Set(),
        currentSwipeIndex: 0, totalSwipes: 2, isStreaming: false,
        API: {
            get: url => new Promise(resolve => gets.push({ url, resolve })),
            post: url => new Promise(resolve => posts.push({ url, resolve })),
        },
        document, escapeHtml: text => text,
        console: { error: () => {} },
    });
    vm.runInContext(swipeHandlers, context);
    context.applySwipeResult = () => {};
    return { context, gets, posts, modals, gamePanel };
}

test('a late swipe list does not open over another save', async () => {
    const view = setupPicker();
    const pending = view.context.openSwipePicker();
    assert.equal(view.gets[0].url, '/api/game/first/swipes');
    view.context.currentSaveId = 'second';
    view.gets[0].resolve([{ index: 0, narrative_preview: 'First save' }]);
    await pending;
    assert.equal(view.modals.length, 0);
});

for (const field of ['currentNodeId', '_saveLoadSequence']) {
    test(`a late swipe list does not open after ${field} changes`, async () => {
        const view = setupPicker();
        const pending = view.context.openSwipePicker();
        view.context[field] = field === 'currentNodeId' ? 'node-2' : 1;
        view.gets[0].resolve([{ index: 0, narrative_preview: 'Previous turn' }]);
        await pending;
        assert.equal(view.modals.length, 0);
    });
}

test('an open swipe picker cannot jump to a different save', async () => {
    const view = setupPicker();
    const pending = view.context.openSwipePicker();
    view.gets[0].resolve([{ index: 1, narrative_preview: 'First save' }]);
    await pending;
    const modal = view.modals[0];
    const list = modal.querySelector('.swipe-picker-list');
    const item = { dataset: { index: '1' } };
    view.context.currentSaveId = 'second';
    await list.listeners.click({ target: { closest: () => item } });
    assert.equal(view.posts.length, 0);
    assert.equal(view.modals.length, 0);
});

test('a late swipe list does not open after leaving the game panel', async () => {
    const view = setupPicker();
    const pending = view.context.openSwipePicker();
    view.gamePanel.active = false;
    view.gets[0].resolve([{ index: 0, narrative_preview: 'Old panel' }]);
    await pending;
    assert.equal(view.modals.length, 0);
});

test('a late swipe list does not show a previous version after a swipe', async () => {
    const view = setupPicker();
    const pending = view.context.openSwipePicker();
    view.context.currentSwipeIndex = 1;
    view.gets[0].resolve([{ index: 0, narrative_preview: 'Old version' }]);
    await pending;
    assert.equal(view.modals.length, 0);
});

test('the current swipe picker still jumps to its selected version', async () => {
    const view = setupPicker();
    const pending = view.context.openSwipePicker();
    view.gets[0].resolve([{ index: 1, narrative_preview: 'Current save' }]);
    await pending;
    const list = view.modals[0].querySelector('.swipe-picker-list');
    list.listeners.click({ target: { closest: () => ({ dataset: { index: '1' } }) } });
    assert.equal(view.posts[0].url,
        '/api/game/first/swipe/jump?index=1&expected_node_id=node-1&expected_swipe_index=0&expected_total_swipes=2');
    assert.equal(view.modals.length, 0);
    view.posts[0].resolve({ narrative: 'Selected version' });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(view.context.isStreaming, false);
});

for (const key of ['Enter', ' ']) {
    test(`the swipe picker selects a version with ${key === ' ' ? 'Space' : key}`, async () => {
        const view = setupPicker();
        const pending = view.context.openSwipePicker();
        view.gets[0].resolve([{ index: 1, narrative_preview: 'Alternate' }]);
        await pending;
        const modal = view.modals[0];
        assert.match(modal.innerHTML, /role="button" tabindex="0"/);
        const list = modal.querySelector('.swipe-picker-list');
        const item = { dataset: { index: '1' } };
        const event = { type: 'keydown', key: 'Escape', target: { closest: () => item }, preventDefault() {} };
        list.listeners.keydown(event);
        assert.equal(view.posts.length, 0);
        let prevented = false;
        event.key = key;
        event.preventDefault = () => { prevented = true; };
        list.listeners.keydown(event);
        assert.equal(prevented, true);
        assert.equal(view.posts[0].url,
            '/api/game/first/swipe/jump?index=1&expected_node_id=node-1&expected_swipe_index=0&expected_total_swipes=2');
        view.posts[0].resolve({ narrative: 'Alternate' });
        await new Promise(resolve => setImmediate(resolve));
    });
}

function setupUndo() {
    const posts = [];
    const gets = [];
    const rendered = [];
    const alerts = [];
    const context = vm.createContext({
        currentSaveId: 'first', currentState: { scene: 'old' }, isStreaming: false,
        _prevState: { scene: 'older' },
        confirm: () => true, alert: message => alerts.push(message),
        API: {
            post: url => new Promise((resolve, reject) => posts.push({ url, resolve, reject })),
            get: url => new Promise((resolve, reject) => gets.push({ url, resolve, reject })),
        },
        document: { getElementById: () => ({ innerHTML: '' }) },
        showGameScreen: result => rendered.push(result), renderChoices: () => {},
    });
    vm.runInContext(undoHandler, context);
    return { context, posts, gets, rendered, alerts };
}

test('double-clicking undo deletes only one turn', async () => {
    const view = setupUndo();
    const first = view.context.undoLastTurn();
    const second = view.context.undoLastTurn();
    assert.equal(view.posts.length, 1);
    assert.equal(view.context.isStreaming, true);
    view.posts[0].resolve({ state: { scene: 'previous' }, choices: ['Next'] });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(view.gets.length, 1);
    view.gets[0].resolve({ entries: [], total: 0 });
    await Promise.all([first, second]);
    assert.equal(view.rendered.length, 1);
    assert.equal(view.context.isStreaming, false);
});

test('undo history arriving after save switch cannot replace the current screen', async () => {
    const view = setupUndo();
    const pending = view.context.undoLastTurn();
    view.posts[0].resolve({ state: { scene: 'previous' }, choices: [] });
    await new Promise(resolve => setImmediate(resolve));
    view.context.currentSaveId = 'second';
    view.context.currentState = { scene: 'second' };
    view.context.isStreaming = false;
    view.gets[0].resolve({ entries: [], total: 0 });
    await pending;
    assert.equal(view.rendered.length, 0);
    assert.equal(view.context.currentState.scene, 'second');
    assert.equal(view.context.isStreaming, false);
});

test('undo reports successful deletion when subsequent history refresh fails', async () => {
    const view = setupUndo();
    const pending = view.context.undoLastTurn();
    view.posts[0].resolve({ state: { scene: 'previous' }, choices: [] });
    await new Promise(resolve => setImmediate(resolve));
    view.gets[0].reject(new Error('history offline'));
    await pending;
    assert.match(view.alerts[0], /撤销已生效.*重新加载存档/);
    assert.equal(view.context.isStreaming, false);
});
