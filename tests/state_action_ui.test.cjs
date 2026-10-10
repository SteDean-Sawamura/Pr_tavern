const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const handlers = source.slice(
    source.indexOf('async function _runImmediateStateAction('),
    source.indexOf('const _defaultSkillKeywords ='),
);

function setup(response) {
    const actions = [];
    const hints = [];
    const notifications = [];
    const requests = [];
    const maps = [];
    const panels = [];
    const changes = [];
    const input = { disabled: false };
    const context = vm.createContext({
        currentSaveId: 'save', currentState: {}, _saveLoadSequence: 0, isStreaming: false,
        window: { _activeGameAction: null },
        document: { getElementById: id => id === 'freeform-input' ? input : null },
        API: { post: async (url, payload) => {
            requests.push({ url, payload });
            if (typeof response === 'function') return response(url, payload);
            if (response instanceof Error) throw response;
            return response;
        } },
        quickAction: (action, explicitAction) => {
            assert.equal(context.isStreaming, false);
            assert.equal(context.window._activeGameAction, null);
            actions.push(action);
            hints.push(explicitAction);
        },
        _createNotifElement: (_type, html) => html,
        _pushNotif: notification => notifications.push(notification),
        updateStatusPanel: state => panels.push(state),
        updateMainMapView: (state, saveId) => maps.push({ state, saveId }),
        showStateChanges: stateChanges => changes.push(stateChanges),
        escapeHtml: text => text,
    });
    vm.runInContext(handlers, context);
    return { context, actions, hints, notifications, requests, maps, panels, changes, input };
}

test('failed item request reports the error without generating another action', async () => {
    const view = setup(new Error('Save failed'));
    await view.context.useItem('Potion');
    assert.deepEqual(view.actions, []);
    assert.match(view.notifications[0], /Save failed/);
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.input.disabled, false);
});

test('failed interaction reports the error without generating another action', async () => {
    const view = setup(new Error('Save failed'));
    await view.context.interactWith('lever', 'Pull lever');
    assert.deepEqual(view.actions, []);
    assert.match(view.notifications[0], /Save failed/);
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.input.disabled, false);
});

test('only explicit lack of rules falls back to a narrative action', async () => {
    const item = setup({ has_effect: false });
    await item.context.useItem('Potion');
    assert.deepEqual(item.actions, ['使用Potion']);
    assert.deepEqual(item.hints, [true]);

    const interaction = setup({ has_rules: false, action_hint: 'Pull lever' });
    await interaction.context.interactWith('lever', 'Interact');
    assert.deepEqual(interaction.actions, ['Pull lever']);
    assert.deepEqual(interaction.hints, [true]);
});

test('item requests block other actions and refresh the map after persisted state', async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const view = setup(() => response);
    const pending = view.context.useItem('Key');
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.input.disabled, true);
    await view.context.interactWith('gate', 'Open gate');
    assert.equal(view.requests.length, 1);
    const state = { visible_locations: ['gate'] };
    resolve({ success: true, state, state_changes: [{ target: 'gate' }], message: 'Opened' });
    await pending;
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.input.disabled, false);
    assert.equal(view.context.window._activeGameAction, null);
    assert.equal(view.context.currentState, state);
    assert.deepEqual(view.maps, [{ state, saveId: 'save' }]);
    assert.deepEqual(view.panels, [state]);
    assert.equal(view.changes.length, 1);
});

test('an old item response cannot replace a loaded save or release its lock', async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const view = setup(() => response);
    const pending = view.context.useItem('Key');
    const newAction = {};
    const newState = { player: { location: 'new' } };
    view.context.currentSaveId = 'new-save';
    view.context.currentState = newState;
    view.context._saveLoadSequence++;
    view.context.window._activeGameAction = newAction;
    view.input.disabled = true;
    resolve({ success: true, state: { player: { location: 'old' } }, message: 'Used' });
    await pending;
    assert.equal(view.context.currentState, newState);
    assert.equal(view.context.window._activeGameAction, newAction);
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.input.disabled, true);
    assert.deepEqual(view.maps, []);
    assert.deepEqual(view.notifications, []);
});

test('a failed same-save load attempt cannot strand an in-flight item action', async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const view = setup(() => response);
    const pending = view.context.useItem('Key');
    view.context._saveLoadSequence++;
    resolve({ success: false, message: 'Unavailable' });
    await pending;
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.context.window._activeGameAction, null);
    assert.equal(view.input.disabled, false);
});

test('reopening the same save ignores the old item fallback', async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const view = setup(() => response);
    const pending = view.context.useItem('Key');
    view.context.window._activeGameAction = null;
    view.context.isStreaming = false;
    view.input.disabled = false;
    resolve({ has_effect: false });
    await pending;
    assert.deepEqual(view.actions, []);
    assert.deepEqual(view.notifications, []);
});

test('rule-free interaction falls back after unlocking, without allowing duplicate clicks', async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const view = setup(() => response);
    const pending = view.context.interactWith('gate', 'Open gate');
    const duplicate = view.context.interactWith('gate', 'Open gate');
    assert.equal(view.requests.length, 1);
    resolve({ has_rules: false, action_hint: 'Open gate' });
    await Promise.all([pending, duplicate]);
    assert.deepEqual(view.actions, ['Open gate']);
    assert.equal(view.context.isStreaming, false);
});
