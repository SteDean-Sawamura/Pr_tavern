const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/game_ui.js'), 'utf8');
const appSource = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup(firstEvent = { type: 'agent_waiting', reason: 'Waiting' }, planCard = null) {
    const timers = new Map();
    const cleared = [];
    const mapUpdates = [];
    const agentStatusMessages = [];
    const toastMessages = [];
    let nextTimer = 0;
    let sendReply;
    let finishStream;
    let agentControls;
    let agentToolsBody;
    let renderedPlanCard;
    let reads = 0;
    const reply = new Promise(resolve => { sendReply = resolve; });
    const ending = new Promise(resolve => { finishStream = resolve; });
    const encode = event => new TextEncoder().encode('data: ' + JSON.stringify(event) + '\n\n');
    const reader = { read() {
        reads += 1;
        if (reads === 1) return Promise.resolve({ done: false, value: encode(firstEvent) });
        if (reads === 2) return reply;
        if (reads === 3) return ending;
        return Promise.resolve({ done: true });
    } };

    function element() {
        return {
            className: '', style: {}, innerHTML: '', textContent: '', value: '', disabled: false,
            children: [], classList: { add() {}, remove() {} },
            listeners: {}, setAttribute() {}, remove() {}, addEventListener(type, callback) { this.listeners[type] = callback; }, appendChild(child) {
                if (this.className === 'agent-tools-body') {
                    agentStatusMessages.push(child.textContent);
                    this.children.push(child);
                    child.remove = () => { this.children = this.children.filter(item => item !== child); };
                }
                if (child.className === 'agent-plan-card') renderedPlanCard = child;
                if (child.className === 'agent-controls') agentControls = child;
            }, insertBefore() {},
            querySelectorAll: () => [],
            querySelector(selector) {
                if (this.className === 'agent-tools-block' && selector === '.agent-tools-body') {
                    this.toolsBody ||= Object.assign(element(), { className: 'agent-tools-body' });
                    agentToolsBody = this.toolsBody;
                    return this.toolsBody;
                }
                if (selector === '.agent-waiting') return this.children.find(child => child.className.split(' ').includes('agent-waiting')) || null;
                if (this.className === 'agent-tools-block' && selector === '.agent-plan-card') return renderedPlanCard || planCard;
                if (this.className === 'agent-controls' && ['.stop-btn', '.inject-input', '.inject-btn'].includes(selector)) {
                    this.controls ||= {};
                    return this.controls[selector] ||= element();
                }
                return null;
            },
        };
    }

    const body = element();
    body.appendChild = child => toastMessages.push(child.textContent);
    const elements = {
        'choices-area': element(), 'freeform-input': element(),
        'narrative-content': element(), 'narrative-area': element(),
    };
    const context = vm.createContext({
        currentSaveId: 'first', currentNodeId: 'opening-node', currentState: {}, isStreaming: false,
        window: { addEventListener() {}, removeEventListener() {} },
        document: {
            addEventListener() {}, createElement: element, body,
            getElementById: id => elements[id], querySelectorAll: () => [],
        },
        localStorage: { getItem: () => null },
        _clearNotifStack() {}, _showGameOverlay() {},
        setMainMapAgentStatus: (...args) => mapUpdates.push(args),
        requestAnimationFrame: callback => callback(),
        setTimeout: (callback, ms) => {
            const id = ++nextTimer;
            timers.set(id, { callback, ms });
            return id;
        },
        clearTimeout: id => { cleared.push(id); timers.delete(id); },
        AbortController: class { abort() { throw new Error('Unexpected timeout'); } },
        fetch: async () => ({ ok: true, body: { getReader: () => reader } }),
        TextDecoder,
        ...Object.fromEntries([
            'renderChoices', 'renderDice', 'showStateChanges', 'showTriggeredEvents',
            'showExpiredStates', 'showCheckResult', 'showTriggeredConsequences',
            'showAchievedMilestones', 'showMilestoneProgress', 'showThresholdEvents',
            'showDataBankPanel', 'showWarnings', 'showNpcInterjections',
            'showNpcAttitudeNotifications', 'showEmotionLabel', 'showTriggerNotifications',
            'showStoryTreeUpdates', 'showTurnRecap', 'updateStatusPanel',
            'showChoiceRipples', 'showMemoryEchoes', 'updateHeader',
            'updateTurnIndicator', 'updatePerformanceDashboard',
        ].map(name => [name, () => {}])),
    });
    vm.runInContext(source, context);
    return {
        context, timers, cleared, mapUpdates, agentStatusMessages, toastMessages,
        getAgentControls: () => agentControls, getPlanCard: () => renderedPlanCard,
        getAgentToolsBody: () => agentToolsBody,
        sendReply: event => sendReply({ done: false, value: encode(event) }),
        sendHeartbeat: () => sendReply({ done: false, value: new TextEncoder().encode(': heartbeat\n\n') }),
        finishStream: () => finishStream({ done: false, value: encode({ type: 'final', state: {}, choices: [] }) }),
        endStream: () => finishStream({ done: true }),
    };
}

test('model heartbeat extends active inactivity timeout without becoming a player wait', async () => {
    const view = setup({ type: 'model_waiting', label: '统一Agent', round: 1 });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    assert.equal(view.timers.size, 1);
    const initialTimer = [...view.timers.keys()][0];
    assert.equal([...view.timers.values()][0].ms, 300000);
    assert.equal(view.mapUpdates.at(-1)[1], '模型处理中，可请求停止');

    view.sendHeartbeat();
    await flush();
    assert.equal(view.timers.size, 1);
    assert.ok(view.cleared.includes(initialTimer));
    assert.notEqual([...view.timers.keys()][0], initialTimer);
    assert.equal([...view.timers.values()][0].ms, 300000);
    assert.equal(view.mapUpdates.at(-1)[1], '模型处理中，可请求停止');

    view.finishStream();
    await generation;
    assert.equal(view.timers.size, 0);
});

function setupDiscardedAction(action, withDraft = true, failureCount = 1) {
    const view = setup();
    const requests = [];
    const events = [];
    const storage = new Map();
    const createElement = view.context.document.createElement;
    function connected(element) {
        const append = element.appendChild;
        element.remove = () => {
            if (!element.parentElement) return;
            const siblings = element.parentElement.children;
            siblings.splice(siblings.indexOf(element), 1);
            element.parentElement = null;
        };
        element.appendChild = child => {
            child.remove();
            append.call(element, child);
            element.children.push(child);
            child.parentElement = element;
            if (element.className === 'narrative-text') element.textContent += child.innerHTML;
            return child;
        };
        element.insertBefore = child => element.appendChild(child);
        element.querySelectorAll = selector => selector === ':scope > .stream-interrupted'
            ? element.children.filter(child => child.className === 'stream-interrupted') : [];
        return element;
    }
    view.context.document.createElement = () => connected(createElement());
    const content = connected(view.context.document.getElementById('narrative-content'));
    const previousDraft = view.context.document.createElement('div');
    previousDraft.className = 'turn-block latest';
    previousDraft.textContent = 'Synthetic rejected earlier draft';
    content.appendChild(previousDraft);
    view.context.escapeHtml = text => text;
    view.context.API = { get: async () => ({}) };
    view.context.sessionStorage = {
        setItem: (key, value) => storage.set(key, value), getItem: key => storage.get(key),
        removeItem: key => storage.delete(key),
    };
    view.context.recordMainMapStreamEvent = (_save, _token, event) => events.push(event);
    view.context.fetch = async (_url, options) => {
        requests.push(JSON.parse(options.body));
        const updates = requests.length <= failureCount ? [
            { type: 'stream_ready' },
            ...(withDraft ? [{ type: 'text', content: 'Synthetic rejected current draft' }] : []),
            { type: 'error', message: 'Synthetic review rejected', discard_partial: true },
        ] : [{ type: 'final', state: {}, choices: [], node_id: 'saved-node' }];
        const payload = updates.map((event, index) => `id: ${index + 1}\ndata: ${JSON.stringify(event)}\n\n`).join('');
        let sent = false;
        return { ok: true, body: { getReader: () => ({
            read: async () => sent ? { done: true } : (sent = true, { done: false, value: new TextEncoder().encode(payload) }),
            cancel: async () => {},
        }) } };
    };
    const pending = { action, baseNodeId: 'opening-node', actionId: 'synthetic-discard-action',
        streamId: 'synthetic-discard-stream', interruptedBlocks: [previousDraft] };
    return { ...view, content, requests, events, storage, pending, previousDraft };
}

function setupReloadAction(action, failureCount = 1) {
    const view = setupDiscardedAction(action, true, failureCount);
    const snapshots = [];
    const alerts = [];
    const getElement = view.context.document.getElementById;
    const screenElements = new Map(['game-start-screen', 'game-play-screen', 'status-panel',
        'status-panel-toggle', 'main-area'].map(id => {
        const element = view.context.document.createElement('div');
        element.classList.toggle = () => {};
        return [id, element];
    }));
    view.context.document.getElementById = id => getElement(id) || screenElements.get(id);
    Object.defineProperty(view.content, 'innerHTML', {
        get: () => '',
        set: () => [...view.content.children].forEach(child => child.remove()),
    });
    Object.assign(view.context, {
        _saveLoadSequence: 0, _historyRenderSequence: 0, _pendingSwipeRequests: new Set(),
        _npcChatId: null, _npcChatRequest: null, AbortController,
        API: { get: url => url.includes('/snapshot?')
            ? new Promise(resolve => snapshots.push({ url, resolve })) : Promise.resolve({}) },
        alert: message => alerts.push(message),
        switchPanel() {}, loadSidebarSaves() {}, renderLogitBiasList() {},
        _syncTurnOverflow() {}, updateSwipeControls() {},
    });
    vm.runInContext(
        appSource.slice(appSource.indexOf('function cancelActiveGameStream() {'), appSource.indexOf('async function deleteSave(')) +
        appSource.slice(appSource.indexOf('function showGameScreen('), appSource.indexOf('function _appendHistoryDetails(')),
        view.context,
    );
    return { ...view, snapshots, alerts };
}

function recoverySnapshot(overrides = {}) {
    return { state: {}, node_id: 'opening-node', history: { entries: [], total: 0 }, ...overrides };
}

for (const action of [
    { type: 'freeform', text: 'Inspect', intent_hint: 'explicit_action' },
    { type: 'freeform', text: 'Describe', intent_hint: 'explicit_query' },
    { type: 'system', text: 'Opening' },
    { type: 'choice', choice_id: 'synthetic-choice', target_id: 'synthetic-target' },
]) test(`reloading a failed ${action.intent_hint || action.type} runs the same action through the real screen and stream`, async () => {
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const retryBar = view.content.children[0];
    const pending = JSON.parse(view.storage.get('tavern-pending-action:first'));
    const reload = view.context.continueGame('first');
    assert.equal(view.snapshots[0].url, `/api/game/first/snapshot?last=10&action_id=${pending.actionId}`);
    view.snapshots[0].resolve(recoverySnapshot({ pending_action: pending }));
    assert.equal(await reload, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 2);
    assert.deepEqual(view.requests[1], view.requests[0]);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.context.document.getElementById('freeform-input').disabled, false);
    assert.equal(view.storage.size, 0);
    assert.equal(retryBar.parentElement, null);
    assert.equal(view.events.filter(event => event.type === 'final').length, 1);
});

test('a fresh page reload resumes locally retained failure identifiers without a server pending action', async () => {
    const action = { type: 'freeform', text: 'Inspect', intent_hint: 'explicit_action' };
    const failed = setupReloadAction(action);
    failed.pending.actionId = 'synthetic action/+?';
    await failed.context.submitActionStream(action, undefined, failed.pending);
    const view = setupReloadAction(action, 0);
    for (const [key, value] of failed.storage) view.storage.set(key, value);
    view.context.currentSaveId = null;
    view.context.currentNodeId = null;
    const loading = view.context.continueGame('first');
    assert.equal(view.snapshots[0].url, '/api/game/first/snapshot?last=10&action_id=synthetic%20action%2F%2B%3F');
    view.snapshots[0].resolve(recoverySnapshot());
    assert.equal(await loading, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.deepEqual(view.requests, failed.requests);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.storage.size, 0);
});

for (const completed of [false, true]) test(`server pending action takes precedence over local recovery when completed=${completed}`, async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const pending = { action: { type: 'choice', choice_id: 'synthetic-choice' },
        baseNodeId: 'opening-node', actionId: 'synthetic-server-action', streamId: 'synthetic-server-stream' };
    const loading = view.context.continueGame('first');
    view.snapshots[0].resolve(recoverySnapshot({ pending_action: pending, action_completed: completed }));
    assert.equal(await loading, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 2);
    assert.deepEqual(view.requests[1], { ...pending.action, expected_node_id: pending.baseNodeId,
        action_id: pending.actionId, stream_id: pending.streamId });
    assert.equal(view.storage.size, 0);
});

test('a completed same-node query does not restart the real action stream after reload', async () => {
    const action = { type: 'freeform', text: 'Describe', intent_hint: 'explicit_query' };
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const loading = view.context.continueGame('first');
    view.snapshots[0].resolve(recoverySnapshot({ action_completed: true }));
    assert.equal(await loading, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 1);
    assert.equal(view.context.currentNodeId, 'opening-node');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.content.children.length, 0);
    assert.equal(view.storage.size, 0);
});

for (const fromServer of [false, true]) test(`reload refuses an old-node recovery with server pending=${fromServer}`, async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const pending = JSON.parse(view.storage.get('tavern-pending-action:first'));
    const loading = view.context.continueGame('first');
    view.snapshots[0].resolve(recoverySnapshot({ node_id: 'different-node', pending_action: fromServer ? pending : null }));
    assert.equal(await loading, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 1);
    assert.equal(view.context.currentNodeId, 'different-node');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.storage.size, 0);
});

test('server recovery reaches the real stream even when browser storage rejects access', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const pending = JSON.parse(view.storage.get('tavern-pending-action:first'));
    view.context.sessionStorage = Object.fromEntries(['getItem', 'setItem', 'removeItem'].map(method => [
        method, () => { throw new Error('Synthetic storage unavailable'); },
    ]));
    const loading = view.context.continueGame('first');
    assert.equal(view.snapshots[0].url, '/api/game/first/snapshot?last=10');
    view.snapshots[0].resolve(recoverySnapshot({ pending_action: pending }));
    assert.equal(await loading, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 2);
    assert.deepEqual(view.requests[1], view.requests[0]);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.context.isStreaming, false);
});

test('only the latest concurrent reload can resume a failed action', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const staleLoad = view.context.continueGame('first');
    const latestLoad = view.context.continueGame('first');
    view.snapshots[1].resolve(recoverySnapshot());
    assert.equal(await latestLoad, true);
    await flush();
    view.snapshots[0].resolve(recoverySnapshot());
    assert.equal(await staleLoad, false);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 2);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.events.filter(event => event.type === 'final').length, 1);
    assert.equal(view.storage.size, 0);
});

test('switching saves while reloading does not resume the previous save pending action', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    const retained = view.storage.get('tavern-pending-action:first');
    const staleLoad = view.context.continueGame('first');
    const otherLoad = view.context.continueGame('other');
    view.snapshots[1].resolve(recoverySnapshot({ node_id: 'other-node' }));
    assert.equal(await otherLoad, true);
    view.snapshots[0].resolve(recoverySnapshot());
    assert.equal(await staleLoad, false);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.requests.length, 1);
    assert.equal(view.context.currentSaveId, 'other');
    assert.equal(view.context.currentNodeId, 'other-node');
    assert.equal(view.storage.get('tavern-pending-action:first'), retained);
    assert.equal(view.storage.has('tavern-pending-action:other'), false);
});

for (const lateEvent of [
    { type: 'final', state: {}, choices: [], node_id: 'stale-node' },
    { type: 'error', message: 'Synthetic stale rejection', discard_partial: true },
]) test(`a cancelled stream's late ${lateEvent.type} cannot interfere with its reloaded replacement`, async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action);
    const streams = [];
    view.context.fetch = async (url, options) => {
        let deliver;
        let delivered = false;
        const payload = new Promise(resolve => { deliver = resolve; });
        streams.push({ url, request: JSON.parse(options.body), signal: options.signal,
            send: event => deliver({ done: false,
                value: new TextEncoder().encode(`id: 1\ndata: ${JSON.stringify(event)}\n\n`) }),
        });
        return { ok: true, body: { getReader: () => ({
            read: () => delivered ? Promise.resolve({ done: true }) : (delivered = true, payload),
            cancel: async () => {},
        }) } };
    };
    const original = view.context.submitActionStream(action, undefined, view.pending);
    await flush();
    const originalToken = view.context.window._activeGameAction;
    const loading = view.context.continueGame('first');
    assert.equal(streams[0].signal.aborted, true);
    assert.equal(view.snapshots.length, 1);
    view.snapshots[0].resolve(recoverySnapshot());
    assert.equal(await loading, true);
    await flush();
    assert.equal(streams.length, 2);
    assert.deepEqual(streams[1].request, streams[0].request);
    const replacementToken = view.context.window._activeGameAction;
    assert.notEqual(replacementToken, originalToken);
    const retained = view.storage.get('tavern-pending-action:first');
    assert.ok(retained);
    streams[0].send(lateEvent);
    await original;
    assert.equal(view.context.window._activeGameAction, replacementToken);
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.context.document.getElementById('freeform-input').disabled, true);
    assert.equal(view.context.currentNodeId, 'opening-node');
    assert.equal(view.storage.get('tavern-pending-action:first'), retained);
    assert.equal(view.events.some(event => ['error', 'final'].includes(event.type)), false);
    streams[1].send({ type: 'final', state: {}, choices: [], node_id: 'saved-node' });
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.context.document.getElementById('freeform-input').disabled, false);
    assert.equal(view.storage.size, 0);
    assert.equal(view.events.filter(event => event.type === 'final').length, 1);
});

test('a reload that fails again retains one usable same-action retry', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupReloadAction(action, 2);
    await view.context.submitActionStream(action, undefined, view.pending);
    const loading = view.context.continueGame('first');
    view.snapshots[0].resolve(recoverySnapshot());
    assert.equal(await loading, true);
    await flush();
    assert.deepEqual(view.alerts, []);
    assert.deepEqual(view.content.children.map(child => child.className), ['stream-interrupted']);
    assert.equal(view.context.isStreaming, false);
    assert.ok(view.storage.get('tavern-pending-action:first'));
    await view.content.children[0].children[0].listeners.click();
    assert.equal(view.requests.length, 3);
    assert.deepEqual(view.requests[1], view.requests[0]);
    assert.deepEqual(view.requests[2], view.requests[0]);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.storage.size, 0);
});

for (const [description, action, withDraft] of [
    ['freeform draft', { type: 'freeform', text: 'Inspect', intent_hint: 'explicit_action' }, true],
    ['failure before text', { type: 'freeform', text: 'Inspect' }, false],
    ['opening', { type: 'system', text: 'Opening' }, false],
    ['declared choice', { type: 'choice', choice_id: 'synthetic-choice', target_id: 'synthetic-target' }, true],
]) test(`discarded ${description} withdraws all drafts but preserves an explicit same-action retry`, async () => {
    const view = setupDiscardedAction(action, withDraft);
    await view.context.submitActionStream(action, undefined, view.pending);
    const failure = view.events.find(event => event.type === 'error');
    assert.equal(failure.retryable, true);
    assert.equal(typeof failure.retry, 'function');
    assert.equal(view.previousDraft.parentElement, null);
    assert.deepEqual(view.content.children.map(child => child.className), ['stream-interrupted']);
    const retryBar = view.content.children[0];
    assert.equal(retryBar.children[0].textContent, '重新推演');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.context.document.getElementById('freeform-input').disabled, false);
    const pending = JSON.parse(view.storage.get('tavern-pending-action:first'));
    assert.deepEqual(pending, { action, baseNodeId: view.pending.baseNodeId,
        actionId: view.pending.actionId, streamId: view.pending.streamId });
    const retried = retryBar.children[0].listeners.click();
    await failure.retry();
    await retried;
    assert.equal(view.requests.length, 2);
    assert.deepEqual(view.requests[1], view.requests[0]);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.storage.has('tavern-pending-action:first'), false);
    assert.equal(retryBar.parentElement, null);
});

for (const boundary of ['save', 'node', 'busy', 'detached', 'new-action']) {
    test(`discarded action retry cannot cross the ${boundary} boundary`, async () => {
        const action = { type: 'freeform', text: 'Inspect' };
        const view = setupDiscardedAction(action);
        await view.context.submitActionStream(action, undefined, view.pending);
        const retry = view.events.find(event => event.type === 'error').retry;
        assert.equal(typeof retry, 'function');
        if (boundary === 'save') view.context.currentSaveId = 'other';
        if (boundary === 'node') view.context.currentNodeId = 'other-node';
        if (boundary === 'busy') view.context.isStreaming = true;
        if (boundary === 'detached') view.content.children[0].remove();
        if (boundary === 'new-action') {
            await view.context.submitActionStream({ type: 'freeform', text: 'Different action' });
            view.context.currentNodeId = 'opening-node';
        }
        const count = view.requests.length;
        await retry();
        assert.equal(view.requests.length, count);
    });
}

for (const edited of [false, true]) {
    test(`retrying a discarded action ${edited ? 'preserves newly edited input' : 'clears only the restored original input'}`, async () => {
        const action = { type: 'freeform', text: 'Inspect' };
        const view = setupDiscardedAction(action);
        await view.context.submitActionStream(action, undefined, view.pending);
        const input = view.context.document.getElementById('freeform-input');
        assert.equal(input.value, action.text);
        if (edited) input.value = 'A different next action';
        await view.events.find(event => event.type === 'error').retry();
        assert.equal(input.value, edited ? 'A different next action' : '');
        assert.deepEqual(view.requests[1], view.requests[0]);
    });
}

test('discarded action retry works when session storage is unavailable', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupDiscardedAction(action);
    view.context.sessionStorage = {
        setItem() { throw new Error('Storage blocked'); },
        getItem() { throw new Error('Storage blocked'); },
        removeItem() { throw new Error('Storage blocked'); },
    };
    await view.context.submitActionStream(action, undefined, view.pending);
    await view.events.find(event => event.type === 'error').retry();
    assert.equal(view.requests.length, 2);
    assert.deepEqual(view.requests[1], view.requests[0]);
    assert.equal(view.context.currentNodeId, 'saved-node');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.content.children.some(child => child.className === 'stream-interrupted'), false);
});

test('repeated discarded failures replace the retry entry without changing action identifiers', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupDiscardedAction(action, true, 2);
    await view.context.submitActionStream(action, undefined, view.pending);
    const firstRetry = view.events.find(event => event.type === 'error').retry;
    const firstBar = view.content.children[0];
    await firstRetry();
    assert.equal(firstBar.parentElement, null);
    assert.deepEqual(view.content.children.map(child => child.className), ['stream-interrupted']);
    assert.equal(view.storage.has('tavern-pending-action:first'), true);
    await firstRetry();
    assert.equal(view.requests.length, 2);
    await view.events.filter(event => event.type === 'error').at(-1).retry();
    assert.equal(view.requests.length, 3);
    view.requests.slice(1).forEach(request => assert.deepEqual(request, view.requests[0]));
    assert.equal(view.storage.has('tavern-pending-action:first'), false);
    assert.equal(view.context.currentNodeId, 'saved-node');
});

test('an action conflict ends discarded-action recovery and clears its pending identifiers', async () => {
    const action = { type: 'freeform', text: 'Inspect' };
    const view = setupDiscardedAction(action);
    await view.context.submitActionStream(action, undefined, view.pending);
    view.context.fetch = async (_url, options) => {
        view.requests.push(JSON.parse(options.body));
        return { ok: false, status: 409, json: async () => ({ detail: 'Synthetic action conflict' }) };
    };
    await view.events.find(event => event.type === 'error').retry();
    assert.equal(view.requests.length, 2);
    assert.deepEqual(view.requests[1], view.requests[0]);
    const failure = view.events.filter(event => event.type === 'error').at(-1);
    assert.equal(failure.retryable, false);
    assert.equal(failure.retry, null);
    assert.equal(view.storage.has('tavern-pending-action:first'), false);
    assert.equal(view.content.children.length, 0);
    assert.equal(view.context.isStreaming, false);
});

test('tool preparation updates map status before execution without exposing narrative', async () => {
    const view = setup({ type: 'model_waiting', label: '统一Agent', round: 1 });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    view.sendReply({ type: 'tool_preparing', tool_name: 'query_lorebook' });
    await flush();
    assert.equal(view.mapUpdates.at(-1)[1], '模型正在准备工具调用');
    assert.equal(view.context.document.getElementById('narrative-content').children.length, 0);
    view.finishStream();
    await generation;
});

test('model output signal changes status without revealing private model content', async () => {
    const view = setup({ type: 'model_waiting', label: '统一Agent', round: 1 });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    view.sendReply({ type: 'model_output_started', content: '幕后秘密', reasoning_content: '内部推理' });
    await flush();
    assert.equal(view.mapUpdates.at(-1)[1], '模型已开始生成，可请求停止');
    assert.equal(view.context.document.getElementById('narrative-content').children.length, 0);
    assert.doesNotMatch(view.mapUpdates.map(update => update[1]).join(' '), /幕后秘密|内部推理/);
    view.finishStream();
    await generation;
});

test('agent_done keeps the map in settlement state until the final event', async () => {
    const view = setup({ type: 'agent_done', label: '统一Agent', round: 4 });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.mapUpdates.at(-1)[1], '正在核验叙事并结算回合…');
    assert.deepEqual(view.agentStatusMessages, ['统一Agent 阶段完成，共4轮']);
    view.sendReply({ type: 'agent_controls_closed' });
    await flush();
    view.finishStream();
    await generation;
});

test('model waiting offers injection only after agent controls are ready', async () => {
    const view = setup({ type: 'agent_controls_ready' });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    view.sendReply({ type: 'model_waiting', label: '统一Agent', round: 1 });
    await flush();
    assert.equal(view.mapUpdates.at(-1)[1], '模型处理中，可插话或请求停止');
    view.finishStream();
    await generation;
});

test('a late interjection response does not move an acknowledged message back to queued', async () => {
    const view = setup({ type: 'agent_controls_ready' });
    let finishPost;
    view.context.API = { post: () => new Promise(resolve => { finishPost = resolve; }) };
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    const controls = view.getAgentControls();
    controls.querySelector('.inject-input').value = '改为观察';
    const sending = controls.querySelector('.inject-btn').listeners.click();
    view.sendReply({ type: 'user_inject', message: '改为观察' });
    await flush();
    finishPost({ ok: true });
    await sending;
    assert.equal(view.mapUpdates.at(-1)[1], 'Agent 继续推演…');
    assert.match(view.agentStatusMessages.at(-1), /Agent 已接收插话/);
    view.finishStream();
    await generation;
});

for (const nextDraft of ['改为观察', '接着等待']) {
    test(`an acknowledged interjection ignores HTTP failure without losing draft ${nextDraft}`, async () => {
        const view = setup({ type: 'agent_controls_ready' });
        let failPost;
        view.context.API = { post: () => new Promise((_, reject) => { failPost = reject; }) };
        const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
        await flush();
        const controls = view.getAgentControls();
        const input = controls.querySelector('.inject-input');
        input.value = '改为观察';
        const sending = controls.querySelector('.inject-btn').listeners.click();
        view.sendReply({ type: 'user_inject', message: '改为观察' });
        await flush();
        input.value = nextDraft;
        failPost(new Error('HTTP 503'));
        await sending;
        assert.equal(view.toastMessages.length, 0);
        assert.equal(input.value, nextDraft === '改为观察' ? '' : nextDraft);
        assert.equal(view.mapUpdates.at(-1)[1], 'Agent 继续推演…');
        view.finishStream();
        await generation;
    });
}

for (const eventType of ['agent_inject_dropped', 'agent_controls_closed']) {
    test(`a late interjection response does not requeue after ${eventType}`, async () => {
        const view = setup({ type: 'agent_controls_ready' });
        let finishPost;
        view.context.API = { post: () => new Promise(resolve => { finishPost = resolve; }) };
        const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
        await flush();
        const controls = view.getAgentControls();
        controls.querySelector('.inject-input').value = '改为观察';
        const sending = controls.querySelector('.inject-btn').listeners.click();
        view.sendReply({ type: eventType, message: '改为观察' });
        await flush();
        finishPost({ ok: true });
        await sending;
        assert.equal(view.agentStatusMessages.some(message => message.includes('插话已排队')), false);
        assert.notEqual(view.mapUpdates.at(-1)[1], '插话已排队，等待 Agent 接收');
        view.finishStream();
        await generation;
    });
}

for (const [stage, expected] of [
    ['background', '正在同步上一轮的后台任务，可请求停止'],
    ['intent', '正在准备行动，可请求停止'],
    ['query', '正在检索资料并回答询问，可请求停止'],
    ['route_retrieval', '正在检索剧本并规划行动，可请求停止'],
]) {
    test(`preflight ${stage} updates the map status during preparation`, async () => {
        const view = setup({ type: 'stream_ready', controls: false, stop_available: true });
        const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
        await flush();
        view.sendReply({ type: 'preflight', stage });
        await flush();
        assert.equal(view.mapUpdates.at(-1)[1], expected);
        view.finishStream();
        await generation;
    });
}

test('route preparation keeps injection available without claiming the agent has started', async () => {
    const view = setup({ type: 'agent_controls_ready' });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    assert.equal(view.mapUpdates.at(-1)[1], '正在准备推演，可发送插话或请求停止');
    view.sendReply({ type: 'preflight', stage: 'route_retrieval' });
    await flush();
    assert.equal(view.mapUpdates.at(-1)[1], '正在检索剧本并规划行动，可插话或请求停止');
    view.finishStream();
    await generation;
});

test('unknown preflight stage cannot replace the map status', async () => {
    const view = setup({ type: 'stream_ready', controls: false, stop_available: true });
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    const previousStatus = view.mapUpdates.at(-1)[1];
    view.sendReply({ type: 'preflight', stage: 'constructor' });
    await flush();
    assert.equal(view.mapUpdates.at(-1)[1], previousStatus);
    view.finishStream();
    await generation;
});

test('heartbeat during player wait refreshes only the connection timeout', async () => {
    const view = setup();
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    assert.equal(view.timers.size, 1);
    const firstTimer = [...view.timers.keys()][0];
    assert.equal(view.timers.get(firstTimer).ms, 60000);
    const waitingMessage = view.getAgentToolsBody().querySelector('.agent-waiting');
    assert.match(waitingMessage.textContent, /Waiting/);
    view.sendHeartbeat();
    await flush();
    assert.equal(view.timers.size, 1);
    assert.equal(view.timers.get(firstTimer), undefined);
    assert.equal([...view.timers.values()][0].ms, 60000);
    assert.equal(view.mapUpdates.at(-1)[1], 'Waiting；刷新后可续接当前回合');
    assert.equal(view.getAgentToolsBody().querySelector('.agent-waiting'), waitingMessage);
    view.finishStream();
    await generation;
});

test('model call after player wait restores inactivity timeout', async () => {
    const view = setup();
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    assert.equal([...view.timers.values()][0].ms, 60000);
    assert.ok(view.getAgentToolsBody().querySelector('.agent-waiting'));

    view.sendReply({ type: 'model_waiting', label: '统一Agent', round: 2 });
    await flush();
    assert.equal(view.timers.size, 1);
    assert.equal([...view.timers.values()][0].ms, 300000);
    assert.equal(view.getAgentToolsBody().querySelector('.agent-waiting'), null);
    assert.equal(view.mapUpdates.at(-1)[1], '模型处理中，可请求停止');

    view.finishStream();
    await generation;
    assert.equal(view.timers.size, 0);
});

for (const eventType of ['user_inject', 'plan_auto_confirmed']) {
    test(`stream switches from connection timeout to generation timeout on ${eventType}`, async () => {
        const view = setup();
        const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
        await flush();
        assert.equal(view.context.isStreaming, true);
        assert.equal([...view.timers.values()][0].ms, 60000);
        assert.ok(view.cleared.includes(1));
        assert.equal(view.mapUpdates[0][1], '正在生成回合…');
        assert.equal(view.mapUpdates[1][1], 'Waiting；刷新后可续接当前回合');
        assert.equal(view.mapUpdates[1][2].className, 'agent-controls');

        view.sendReply({ type: eventType, message: 'OK' });
        await flush();
        assert.equal(view.timers.size, 1);
        assert.equal([...view.timers.values()][0].ms, 300000);
        assert.equal(view.mapUpdates[2][1], 'Agent 继续推演…');
        assert.equal(view.getAgentToolsBody().querySelector('.agent-waiting'), null);
        view.finishStream();
        await generation;
        assert.equal(view.context.isStreaming, false);
        assert.equal(view.timers.size, 0);
        assert.equal(view.mapUpdates.at(-1)[1], '');
    });
}

for (const [description, injection, expected, received] of [
    ['another tab answers the current plan', { plan_id: 'plan-1', message: '街口' }, '已被 Agent 接收', true],
    ['an ordinary interjection replaces the plan', { message: '改走小巷' }, '已插话，原计划失效', false],
    ['a different plan is answered', { plan_id: 'plan-2', message: '街口' }, '已插话，原计划失效', false],
]) {
    test(`plan card handles ${description}`, async () => {
        const button = { disabled: false };
        const input = { disabled: false };
        const actions = { textContent: '' };
        const planCard = {
            planId: 'plan-1',
            querySelectorAll: () => [button],
            querySelector: selector => ({
                '.plan-edit-input': input, '.plan-confirm-btn': button, '.plan-actions': actions,
            })[selector] || null,
        };
        const view = setup({ type: 'agent_waiting', reason: 'Waiting' }, planCard);
        const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
        await flush();
        view.sendReply({ type: 'user_inject', ...injection });
        await flush();
        assert.equal(!!planCard.replyReceived, received);
        assert.equal(planCard.expired, true);
        assert.equal(actions.textContent, expected);
        assert.equal(button.disabled, true);
        assert.equal(input.disabled, true);
        view.finishStream();
        await generation;
    });
}

test('closing controls, stopping, or auto-confirming expires a plan before settlement', async () => {
    for (const [eventType, message] of [
        ['agent_controls_closed', undefined],
        ['aborted', undefined],
        ['plan_auto_confirmed', '已自动确认'],
    ]) {
        const view = setup();
        const closures = [];
        view.context.closePendingAgentPlan = (_card, label) => closures.push(label);
        const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
        await flush();
        view.sendReply({ type: eventType });
        await flush();
        assert.deepEqual(closures, [message]);
        assert.equal(view.context.isStreaming, true);
        view.finishStream();
        await generation;
    }
});

test('stopping without a final event clears the map interaction and does not show an error', async () => {
    const view = setup();
    const events = [];
    view.context.recordMainMapStreamEvent = (_saveId, _token, event) => events.push(event.type);
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    view.sendReply({ type: 'aborted', label: 'Agent' });
    await flush();
    view.endStream();
    await generation;
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.mapUpdates.at(-1)[1], '');
    assert.deepEqual(events, ['begin', 'agent_waiting', 'aborted']);
    assert.deepEqual(view.toastMessages, []);
});

test('reconnect replays an action without rendering earlier events twice', async () => {
    const view = setup({ type: 'stream_ready' });
    const requests = [];
    const events = [];
    const storage = new Map();
    const originalTimeout = view.context.setTimeout;
    view.context.setTimeout = (callback, ms) => ms < 2000 ? setImmediate(callback) : originalTimeout(callback, ms);
    view.context.sessionStorage = {
        setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key),
    };
    view.context.recordMainMapStreamEvent = (_save, _token, event) => events.push(event.type);
    view.context.fetch = async (_url, options) => {
        requests.push(JSON.parse(options.body));
        const prefix = 'id: 1\ndata: {"type":"stream_ready","controls":false}\n\n' +
            'id: 2\ndata: {"type":"agent_controls_ready"}\n\n';
        const payload = prefix + (requests.length === 1 ? '' :
            'id: 3\ndata: {"type":"final","choices":[]}\n\n');
        let sent = false;
        return { ok: true, body: { getReader: () => ({
            read: async () => sent ? { done: true } : (sent = true, { done: false, value: new TextEncoder().encode(payload) }),
            cancel: async () => {},
        }) } };
    };

    await view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    assert.equal(requests.length, 2);
    assert.equal(requests[0].action_id, requests[1].action_id);
    assert.equal(requests[0].stream_id, requests[1].stream_id);
    assert.equal(events.filter(type => type === 'agent_controls_ready').length, 1);
    assert.equal(events.filter(type => type === 'final').length, 1);
    assert.equal(storage.size, 0);
});

test('a fresh page rebuilds an answerable plan from the resumed action stream', async () => {
    const view = setup();
    const requests = [];
    const replies = [];
    const events = [];
    const option = textContent => {
        const classes = new Set();
        const button = {
            textContent, disabled: false,
            classList: {
                add: name => classes.add(name), remove: name => classes.delete(name),
                contains: name => classes.has(name),
            },
            addEventListener(event, listener) { if (event === 'click') this.clickHandler = listener; },
            click() { this.clickHandler?.(); },
        };
        return button;
    };
    const choices = ['街口', '车站'].map(option);
    const group = {
        querySelector: selector => selector === '.pq-text' ? { textContent: '先去哪里？' }
            : choices.find(button => button.classList.contains('selected')),
        querySelectorAll: () => choices,
    };
    choices.forEach(button => { button.closest = () => group; });
    const confirm = option('提交选择');
    const edit = option('发送');
    const input = { value: '', addEventListener() {} };
    const actions = { innerHTML: '', appendChild() {} };
    const originalCreate = view.context.document.createElement;
    view.context.document.createElement = () => {
        const created = originalCreate();
        return new Proxy(created, {
            set(target, key, value) {
                if (key === 'className' && value === 'agent-plan-card') {
                    target.querySelector = selector => ({
                        '.plan-actions': actions, '.plan-confirm-btn': confirm,
                        '.plan-edit-btn': edit, '.plan-edit-input': input,
                    })[selector] || null;
                    target.querySelectorAll = selector => ({
                        '.pq-option': choices, '.plan-question': [group],
                        '.pq-option.selected': choices.filter(button => button.classList.contains('selected')),
                        'button': [...choices, confirm, edit],
                    })[selector] || [];
                }
                target[key] = value;
                return true;
            },
        });
    };
    view.context.escapeHtml = value => value;
    view.context.recordMainMapStreamEvent = (_saveId, _token, event) => events.push(event.type);
    view.context.API = { post: async (url, body) => { replies.push({ url, body }); return { queued: true }; } };
    view.context.fetch = async (_url, options) => {
        requests.push(JSON.parse(options.body));
        return { ok: true, body: { getReader: () => ({
            read: (() => {
                let count = 0;
                return () => {
                    count++;
                    if (count === 1) return Promise.resolve({ done: false, value: new TextEncoder().encode(
                        'id: 1\ndata: {"type":"agent_plan","plan_id":"plan-1","plan":"核对当前行动和现场","questions":[{"question":"先去哪里？","options":["街口","车站"]}]}\n\n' +
                        'id: 2\ndata: {"type":"agent_waiting","reason":"等待用户选择"}\n\n',
                    ) });
                    if (count === 2) return new Promise(resolve => { view.finishStream = () => resolve({ done: false,
                        value: new TextEncoder().encode('id: 3\ndata: {"type":"final","state":{"game_time":"1979-10-25T18:10:00"},"choices":[]}\n\n') }); });
                    return Promise.resolve({ done: true });
                };
            })(),
            cancel: async () => {},
        }) } };
    };
    const pending = { actionId: 'action-1', streamId: 'stream-1', baseNodeId: 'opening-node' };
    const generation = view.context.submitActionStream({ type: 'freeform', text: '核实消息' }, undefined, pending);
    await flush();
    const card = view.getPlanCard();
    assert.ok(card, JSON.stringify({ mapUpdates: view.mapUpdates, toastMessages: view.toastMessages }));
    assert.match(card.innerHTML, /先去哪里？/);
    assert.equal(view.mapUpdates.at(-1)[1].includes('等待用户选择'), true);
    assert.equal(typeof choices[1].clickHandler, 'function');
    assert.equal(typeof confirm.clickHandler, 'function');
    choices[1].click();
    confirm.click();
    await flush();
    assert.equal(requests[0].action_id, 'action-1');
    assert.equal(requests[0].stream_id, 'stream-1');
    assert.deepEqual(JSON.parse(JSON.stringify(replies)), [{
        url: '/api/game/first/inject', body: { message: '1. 先去哪里？：车站', stream_id: 'stream-1', plan_id: 'plan-1' },
    }]);
    view.finishStream();
    await generation;
    assert.deepEqual(events.filter(type => ['agent_plan', 'agent_waiting', 'final'].includes(type)),
        ['agent_plan', 'agent_waiting', 'final']);
    assert.equal(view.context.currentState.game_time, '1979-10-25T18:10:00');
    assert.equal(view.timers.size, 0);
});

test('silent connection during player wait reconnects without replaying the plan twice', async () => {
    const view = setup();
    const requests = [];
    const statuses = [];
    const originalTimeout = view.context.setTimeout;
    view.context.setTimeout = (callback, ms) => ms < 2000 ? setImmediate(callback) : originalTimeout(callback, ms);
    view.context.AbortController = class {
        constructor() { this.signal = {}; }
        abort() { this.signal.onabort?.(); }
    };
    view.context.recordMainMapStreamEvent = (_save, _token, event) => statuses.push(event.type);
    view.context.fetch = async (_url, options) => {
        requests.push(JSON.parse(options.body));
        const firstConnection = requests.length === 1;
        const waiting = 'id: 1\ndata: {"type":"agent_waiting","reason":"等待用户选择"}\n\n';
        const final = 'id: 2\ndata: {"type":"final","choices":[]}\n\n';
        let sent = false;
        return { ok: true, body: { getReader: () => ({
            read: () => {
                if (!sent) {
                    sent = true;
                    return Promise.resolve({ done: false, value: new TextEncoder().encode(waiting + (firstConnection ? '' : final)) });
                }
                if (!firstConnection) return Promise.resolve({ done: true });
                return new Promise((_resolve, reject) => {
                    options.signal.onabort = () => reject(Object.assign(new Error('Disconnected'), { name: 'AbortError' }));
                });
            },
            cancel: async () => {},
        }) } };
    };

    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    const waitingTimer = [...view.timers.values()][0];
    assert.equal(waitingTimer.ms, 60000);
    waitingTimer.callback();
    await generation;
    assert.equal(requests.length, 2);
    assert.equal(requests[0].action_id, requests[1].action_id);
    assert.equal(requests[0].stream_id, requests[1].stream_id);
    assert.equal(statuses.filter(type => type === 'agent_waiting').length, 1);
    assert.equal(statuses.filter(type => type === 'final').length, 1);
    assert.equal(view.timers.size, 0);
});

test('reconnects with the same action before the first event arrives', async () => {
    const view = setup();
    const requests = [];
    const storage = new Map();
    const originalTimeout = view.context.setTimeout;
    view.context.setTimeout = (callback, ms) => ms < 2000 ? setImmediate(callback) : originalTimeout(callback, ms);
    view.context.sessionStorage = {
        setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key),
    };
    view.context.fetch = async (_url, options) => {
        requests.push(JSON.parse(options.body));
        const payload = requests.length === 1 ? '' :
            'id: 1\ndata: {"type":"stream_ready"}\n\n' +
            'id: 2\ndata: {"type":"final","choices":[]}\n\n';
        let sent = false;
        return { ok: true, body: { getReader: () => ({
            read: async () => sent ? { done: true } : (sent = true, { done: false, value: new TextEncoder().encode(payload) }),
            cancel: async () => {},
        }) } };
    };

    await view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    assert.equal(requests.length, 2);
    assert.equal(requests[0].action_id, requests[1].action_id);
    assert.equal(requests[0].stream_id, requests[1].stream_id);
    assert.equal(storage.size, 0);
});

test('stores only serializable action identifiers when retrying', async () => {
    const view = setup();
    const storage = new Map();
    view.context.sessionStorage = {
        setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key),
    };
    const interruptedBlock = {};
    interruptedBlock.parentElement = interruptedBlock;
    const retry = { baseNodeId: 'opening-node', actionId: 'action-1', streamId: 'stream-1',
        interruptedBlocks: [interruptedBlock] };
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' }, undefined, retry);
    await flush();
    assert.deepEqual(JSON.parse(storage.get('tavern-pending-action:first')), {
        action: { type: 'freeform', text: 'Go' },
        baseNodeId: 'opening-node', actionId: 'action-1', streamId: 'stream-1',
    });
    view.sendReply({ type: 'user_inject', message: 'OK' });
    view.finishStream();
    await generation;
    assert.equal(storage.size, 0);
});

for (const [description, action, firstPayload] of [
    ['with partial text', { type: 'freeform', text: 'Go' },
        'data: {"type":"text","content":"Draft"}\n\ndata: {"type":"error","message":"model failed"}\n\n'],
    ['before any text in an opening system turn', { type: 'system', text: 'Opening' },
        'id: 1\ndata: {"type":"stream_ready"}\n\n' +
        'id: 2\ndata: {"type":"error","message":"model failed"}\n\n'],
]) test(`retrying a failed agent turn ${description} reuses its identifiers`, async () => {
    const view = setup();
    const content = view.context.document.getElementById('narrative-content');
    const createElement = view.context.document.createElement;
    const requests = [];
    let retryButton;
    content.appendChild = block => { block.parentElement = content; };
    view.context.document.createElement = tag => {
        const element = createElement(tag);
        element.remove = () => {};
        element.addEventListener = (event, listener) => {
            if (element.className === 'retry-btn' && event === 'click') retryButton = listener;
        };
        element.appendChild = child => {
            if (element.className === 'narrative-text') element.textContent += child.innerHTML;
        };
        return element;
    };
    view.context.document.body = { appendChild() {} };
    view.context.escapeHtml = text => text;
    view.context.API = { get: async () => ({}) };
    view.context.fetch = async (_url, options) => {
        requests.push(JSON.parse(options.body));
        const payload = requests.length === 1 ? firstPayload : 'id: 1\ndata: {"type":"final","choices":[]}\n\n';
        let sent = false;
        return { ok: true, body: { getReader: () => ({
            read: async () => sent ? { done: true } : (sent = true, { done: false, value: new TextEncoder().encode(payload) }),
            cancel: async () => {},
        }) } };
    };

    await view.context.submitActionStream(action);
    assert.equal(typeof retryButton, 'function');
    await retryButton();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].action_id, requests[0].action_id);
    assert.equal(requests[1].stream_id, requests[0].stream_id);
});

for (const stage of ['all', 'choices', 'state']) test(`failed ${stage} regeneration restores the original turn and reports a server error`, async () => {
    const view = setup({ type: 'stream_ready', controls: stage !== 'all' });
    const content = view.context.document.getElementById('narrative-content');
    const createElement = view.context.document.createElement;
    const original = createElement('div');
    const replacements = [];
    const events = [];
    const removed = [];
    original.replaceWith = block => replacements.push(block);
    content.querySelectorAll = () => [original];
    content.appendChild = block => { block.parentElement = content; };
    view.context.document.createElement = tag => {
        const element = createElement(tag);
        element.replaceWith = block => replacements.push(block);
        element.remove = () => removed.push(element);
        return element;
    };
    const freshState = { player: { gold: 10 } };
    const refreshes = [];
    view.context.API = { get: async url => { refreshes.push(url); return freshState; } };
    view.context.recordMainMapStreamEvent = (_saveId, _token, event) => events.push(event);
    const choices = view.context.document.getElementById('choices-area');
    choices.innerHTML = '<button>Original choice</button>';
    const generation = view.context.submitActionStream({ type: 'regen', stage }, `/api/game/first/regenerate/stream?stage=${stage}`);
    await flush();
    view.sendReply({ type: 'error', message: '生成失败，请稍后重试', discard_partial: true });
    view.endStream();
    await generation;
    const error = events.find(event => event.type === 'error');
    assert.ok(error);
    assert.equal(error.server_error, true);
    assert.equal(error.discard_partial, true);
    assert.equal(error.regeneration, true);
    assert.equal(error.retryable, false);
    assert.equal(events.some(event => event.type === 'final'), false);
    if (stage === 'all') assert.equal(replacements.at(-1), original);
    else assert.ok(removed.some(element => element.className === 'turn-block latest'));
    assert.equal(choices.innerHTML, '<button>Original choice</button>');
    assert.equal(view.context.currentNodeId, 'opening-node');
    assert.equal(view.context.currentState, freshState);
    assert.deepEqual(refreshes, ['/api/game/first/state']);
    assert.ok(view.toastMessages.includes('生成失败: 生成失败，请稍后重试'));
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.context.window._activeGameAction, null);
    assert.equal(view.context.document.getElementById('freeform-input').disabled, false);
});
