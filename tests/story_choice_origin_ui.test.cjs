const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
const storySource = source('story_tree_view.js');
const mapSource = source('main_map_view.js');
const branchSource = source('world_tree_view.js');
const extract = (text, name) => text.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n\\}`))[0];
const pendingChoices = [{ id: 'decision', name: 'Synthetic decision', choices: [{ id: 'select', label: 'Select' }] }];

function deferred() {
    let resolve, reject;
    const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
    return { promise, resolve, reject };
}

function element() {
    const classes = new Set();
    return {
        children: [], hidden: false, disabled: false, style: {},
        appendChild(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        setAttribute() {},
        classList: {
            contains: name => classes.has(name),
            remove: name => classes.delete(name),
            toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
        },
    };
}

function setup(surface = 'map') {
    const elements = new Map();
    const byId = id => {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
    };
    byId('main-map-view').hidden = surface !== 'map';
    byId('game-play-screen').classList.toggle('map-view', surface === 'map');
    const requests = { get: [], post: [] };
    const notifications = [], renders = [], panels = [];
    const replies = {
        get: async url => url.includes('/state') ? { marker: 'settled' } : {
            base_node_id: 'node-1', choices: pendingChoices, trees: [{ id: 'tree-1', nodes: [] }],
        },
        post: async url => url.includes('summary_only=true') ? { success: true } : { success: true, notifications: ['Selected'] },
    };
    const context = vm.createContext({
        currentSaveId: 'save-1', currentNodeId: 'node-1', _saveLoadSequence: 0,
        currentState: { marker: 'original' }, isStreaming: false,
        document: { getElementById: byId, createElement: element },
        window: { _activeGameAction: null }, localStorage: { getItem: () => surface === 'map' ? 'true' : 'false' },
        _mainMapChoosing: null, _mainMapLayoutSaveId: 'save-1', _mainMapChoiceSaveId: null,
        _mainMapActivitySaveId: 'save-1', _mainMapAgentSaveId: null,
        _mainMapPrivacySaveId: 'save-1', _mainMapFocusViewSaveId: 'save-1',
        _mainMapChoiceRequest: 0, _mainMapChoiceState: null,
        _mainMapPreparedAction: '', _mainMapAgentAction: null,
        _mainMapViewKey: saveId => saveId,
        _syncMainMapFocus() {}, _syncMainMapCommandDock() {}, _syncMainMapAgentDock() {},
        API: {
            get: async url => { requests.get.push(url); return replies.get(url); },
            post: async (url, payload) => { requests.post.push({ url, payload }); return replies.post(url, payload); },
        },
        console: { error() {} }, confirm: () => true, alert: message => notifications.push(message),
        closeMaterialDetail() {}, switchPanel() {},
        showGameScreen: result => { context.currentNodeId = result.node_id; panels.push(result); },
        renderStoryTreeGraph: () => renders.push(vm.runInContext('_storyTreeData', context)),
        showNotification: message => notifications.push(message),
        updateStatusPanel: state => panels.push(state),
    });
    vm.runInContext(storySource.slice(0, storySource.indexOf('function _getTreeIcon(')), context);
    vm.runInContext(extract(storySource, 'makeStoryChoice'), context);
    for (const name of ['renderMainMapStoryChoices', 'loadMainMapStoryChoices', 'chooseMainMapStoryChoice', 'restoreMainMapView']) {
        vm.runInContext(extract(mapSource, name), context);
    }
    vm.runInContext(extract(branchSource, 'branchFromNode'), context);
    const choiceBlock = storySource.match(/        if \(node\._source === 'tree' && node\.type === 'choice'[\s\S]*?\n        \}/)[0];
    vm.runInContext(`function renderChoiceNode(node, el) {\n${choiceBlock}\n}`, context);
    return { context, byId, replies, requests, notifications, renders, panels };
}

function origin(view) {
    return { saveId: view.context.currentSaveId, nodeId: view.context.currentNodeId, loadSequence: view.context._saveLoadSequence };
}

function renderedButton(view, surface) {
    if (surface === 'map') {
        return view.byId('main-map-story-choices').children[1].children[1];
    }
    const parent = element();
    view.context.renderChoiceNode({ ...pendingChoices[0], _source: 'tree', type: 'choice', status: 'active' }, parent);
    return parent.children[0].children[0];
}

async function button(view, surface) {
    await (surface === 'map' ? view.context.loadMainMapStoryChoices() : view.context.loadStoryTree());
    return renderedButton(view, surface);
}

for (const surface of ['map', 'tree']) {
    test(`${surface} buttons submit their displayed source node`, async () => {
        const view = setup(surface);
        const control = await button(view, surface);
        await control.onclick();
        assert.equal(view.requests.post.length, 1);
        assert.equal(view.requests.post[0].payload.expected_node_id, 'node-1');
        assert.equal(view.requests.post[0].payload.node_id, 'decision');
        if (surface === 'map') {
            assert.match(view.requests.get.find(url => url.includes('/state')), /expected_node_id=node-1/);
            assert.equal(view.context.currentState.marker, 'settled');
            assert.equal(view.byId('freeform-input').disabled, false);
        }
    });

    for (const changed of ['node', 'save', 'reload']) {
        test(`${surface} old buttons cannot act after a ${changed} change`, async () => {
            const view = setup(surface);
            const control = await button(view, surface);
            if (changed === 'node') view.context.currentNodeId = 'node-2';
            if (changed === 'save') view.context.currentSaveId = 'save-2';
            if (changed === 'reload') view.context._saveLoadSequence++;
            view.replies.get = async () => ({ base_node_id: view.context.currentNodeId, choices: pendingChoices, trees: [{ id: 'new', nodes: [] }] });
            await button(view, surface);
            await control.onclick();
            assert.equal(view.requests.post.length, 0);
            assert.equal(view.byId('freeform-input').disabled, false);
        });
    }

    for (const changed of ['node', 'reload', 'server']) {
        test(`${surface} ignores choice responses from a different ${changed} context`, async () => {
            const view = setup(surface);
            const response = deferred();
            view.replies.get = () => response.promise;
            const loading = surface === 'map' ? view.context.loadMainMapStoryChoices() : view.context.loadStoryTree();
            if (changed === 'node') view.context.currentNodeId = 'node-2';
            if (changed === 'reload') view.context._saveLoadSequence++;
            response.resolve({ base_node_id: changed === 'server' ? 'node-2' : 'node-1', choices: pendingChoices, trees: [{ id: 'old' }] });
            await loading;
            assert.equal(view.byId('main-map-story-choices').children.length, 0);
            assert.equal(view.renders.length, 0);
        });
    }

    test(`${surface} rejects missing response origins`, async () => {
        const view = setup(surface);
        view.replies.get = async () => ({ choices: pendingChoices, trees: [{ id: 'old' }] });
        await (surface === 'map' ? view.context.loadMainMapStoryChoices() : view.context.loadStoryTree());
        assert.equal(view.byId('main-map-story-choices').children.length, 0);
        assert.equal(view.renders.length, 0);
    });

    test(`${surface} keeps the newer view when responses arrive out of order`, async () => {
        const view = setup(surface);
        const first = deferred();
        view.replies.get = () => first.promise;
        const old = surface === 'map' ? view.context.loadMainMapStoryChoices() : view.context.loadStoryTree();
        view.replies.get = async () => ({ base_node_id: 'node-1', choices: [], trees: [{ id: 'new' }] });
        await (surface === 'map' ? view.context.loadMainMapStoryChoices() : view.context.loadStoryTree());
        first.resolve({ base_node_id: 'node-1', choices: pendingChoices, trees: [{ id: 'old' }] });
        await old;
        if (surface === 'map') assert.equal(view.byId('main-map-story-choices').children.length, 0);
        else assert.equal(view.renders.at(-1)[0].id, 'new');
    });
}

for (const failure of [false, true]) {
    for (const surface of ['map', 'tree']) {
        test(`${surface} ignores a late ${failure ? 'failed' : 'successful'} choice after navigation`, async () => {
            const view = setup(surface);
            const response = deferred();
            const control = await button(view, surface);
            const before = view.requests.get.length;
            view.replies.post = () => response.promise;
            const choosing = control.onclick();
            view.context.currentNodeId = 'node-2';
            view.context._saveLoadSequence++;
            if (failure) response.reject(new Error('Old failure'));
            else response.resolve({ success: true, notifications: ['Old success'], trees: [{ id: 'old' }] });
            await choosing;
            assert.equal(view.requests.get.length, before);
            assert.deepEqual(view.notifications, []);
            assert.equal(view.context.currentState.marker, 'original');
        });
    }
}

test('a late state refresh cannot replace a new branch or unlock its input', async () => {
    const view = setup();
    const response = deferred(), started = deferred();
    const control = await button(view, 'map');
    view.replies.get = url => {
        if (url.includes('/state')) { started.resolve(); return response.promise; }
        return { base_node_id: 'node-2', choices: [] };
    };
    const choosing = control.onclick();
    await started.promise;
    view.context.currentNodeId = 'node-2';
    view.context._saveLoadSequence++;
    view.context.currentState = { marker: 'new-branch' };
    view.context.isStreaming = true;
    view.context.window._activeGameAction = {};
    view.byId('freeform-input').disabled = true;
    response.resolve({ marker: 'old-state' });
    await choosing;
    assert.equal(view.context.currentState.marker, 'new-branch');
    assert.equal(view.byId('freeform-input').disabled, true);
    assert.deepEqual(view.panels, []);
});

test('restoring a new branch releases only the old choice token', async () => {
    const view = setup();
    const response = deferred();
    const control = await button(view, 'map');
    view.replies.post = () => response.promise;
    const choosing = control.onclick();
    const oldToken = view.context._mainMapChoosing;
    view.context.currentNodeId = 'node-2';
    view.context._saveLoadSequence++;
    view.replies.get = async () => ({ base_node_id: 'node-2', choices: pendingChoices });
    view.context.restoreMainMapView();
    assert.equal(view.context._mainMapChoosing, null);
    assert.equal(view.byId('freeform-input').disabled, false);
    const fresh = { context: origin(view) };
    view.context._mainMapChoosing = fresh;
    view.byId('freeform-input').disabled = true;
    response.resolve({ success: true });
    await choosing;
    assert.notEqual(fresh, oldToken);
    assert.equal(view.context._mainMapChoosing, fresh);
    assert.equal(view.byId('freeform-input').disabled, true);
});

test('branch navigation invalidates displayed buttons before its response arrives', async () => {
    const view = setup();
    const control = await button(view, 'map');
    const response = deferred();
    view.replies.post = () => response.promise;
    const branching = view.context.branchFromNode('node-2');
    assert.equal(view.context._saveLoadSequence, 1);
    await control.onclick();
    assert.equal(view.requests.post.length, 1);
    response.resolve({ node_id: 'node-2', state: { marker: 'new-branch' } });
    await branching;
    assert.equal(view.context.currentNodeId, 'node-2');
});

for (const failure of [false, true]) {
    test(`an obsolete ${failure ? 'failed' : 'successful'} branch response cannot replace a new navigation`, async () => {
        const view = setup();
        const response = deferred();
        view.replies.post = () => response.promise;
        const branching = view.context.branchFromNode('node-2');
        view.context._saveLoadSequence++;
        view.context.currentNodeId = 'node-3';
        if (failure) response.reject(new Error('Old branch failure'));
        else response.resolve({ node_id: 'node-2', state: { marker: 'old-branch' } });
        await branching;
        assert.equal(view.context.currentNodeId, 'node-3');
        assert.equal(view.context.currentState.marker, 'original');
        assert.deepEqual(view.panels, []);
        assert.deepEqual(view.notifications, []);
    });
}

for (const surface of ['map', 'tree']) {
    test(`${surface} reloads usable choices after a current branch failure`, async () => {
        const view = setup(surface);
        const old = await button(view, surface);
        view.replies.post = async () => { throw new Error('Synthetic branch failure'); };
        await view.context.branchFromNode('node-2');
        assert.equal(view.context.currentNodeId, 'node-1');
        assert.equal(view.context._saveLoadSequence, 1);
        assert.equal(view.notifications.length, 1);
        await old.onclick();
        assert.equal(view.requests.post.length, 1);
        view.replies.post = async () => ({ success: true });
        const fresh = renderedButton(view, surface);
        await fresh.onclick();
        assert.equal(view.requests.post.length, 2);
        assert.equal(view.requests.post[1].payload.expected_node_id, 'node-1');
    });
}

for (const phase of ['choice', 'state']) {
    test(`a current ${phase} conflict leaves state intact and releases the map input`, async () => {
        const view = setup();
        const control = await button(view, 'map');
        if (phase === 'choice') view.replies.post = async () => { throw new Error('Conflict'); };
        else view.replies.get = async url => {
            if (url.includes('/state')) throw new Error('Conflict');
            return { base_node_id: 'node-1', choices: pendingChoices };
        };
        await control.onclick();
        assert.equal(view.context.currentState.marker, 'original');
        assert.equal(view.byId('freeform-input').disabled, false);
        assert.equal(view.context._mainMapChoosing, null);
        assert.equal(view.notifications.length, 1);
        assert.match(view.notifications[0], /Conflict/);
    });
}

test('restoring stale choice ownership does not enable an active game action', () => {
    const view = setup();
    view.context._mainMapChoosing = { context: origin(view) };
    view.context._saveLoadSequence++;
    view.context.isStreaming = true;
    view.context.window._activeGameAction = {};
    view.byId('freeform-input').disabled = true;
    view.context.restoreMainMapView();
    assert.equal(view.context._mainMapChoosing, null);
    assert.equal(view.byId('freeform-input').disabled, true);
});

test('double-clicking a map choice sends only one request', async () => {
    const view = setup();
    const response = deferred();
    const control = await button(view, 'map');
    view.replies.post = () => response.promise;
    const choosing = control.onclick();
    await control.onclick();
    assert.equal(view.requests.post.length, 1);
    response.resolve({ success: true });
    await choosing;
    assert.equal(view.byId('freeform-input').disabled, false);
});

test('missing or null choice context cannot send a request', async () => {
    const view = setup();
    await view.context.makeStoryChoice('decision', 'select');
    await view.context.makeStoryChoice('decision', 'select', true, null);
    await view.context.chooseMainMapStoryChoice('decision', 'select');
    assert.equal(view.requests.post.length, 0);
});

test('cancelling branch confirmation does not invalidate current choice buttons', async () => {
    const view = setup();
    const control = await button(view, 'map');
    view.context.confirm = () => false;
    await view.context.branchFromNode('node-2');
    assert.equal(view.context._saveLoadSequence, 0);
    await control.onclick();
    assert.equal(view.requests.post.length, 1);
});
