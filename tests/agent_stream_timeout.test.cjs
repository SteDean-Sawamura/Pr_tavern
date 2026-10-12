const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/game_ui.js'), 'utf8');
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
                if (this.className === 'agent-tools-body') agentStatusMessages.push(child.textContent);
                if (child.className === 'agent-plan-card') renderedPlanCard = child;
                if (child.className === 'agent-controls') agentControls = child;
            }, insertBefore() {},
            querySelectorAll: () => [],
            querySelector(selector) {
                if (this.className === 'agent-tools-block' && selector === '.agent-tools-body') return Object.assign(element(), { className: 'agent-tools-body' });
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
    view.sendHeartbeat();
    await flush();
    assert.equal(view.timers.size, 1);
    assert.equal(view.timers.get(firstTimer), undefined);
    assert.equal([...view.timers.values()][0].ms, 60000);
    assert.equal(view.mapUpdates.at(-1)[1], 'Waiting；刷新后可续接当前回合');
    view.finishStream();
    await generation;
});

test('model call after player wait restores inactivity timeout', async () => {
    const view = setup();
    const generation = view.context.submitActionStream({ type: 'freeform', text: 'Go' });
    await flush();
    assert.equal([...view.timers.values()][0].ms, 60000);

    view.sendReply({ type: 'model_waiting', label: '统一Agent', round: 2 });
    await flush();
    assert.equal(view.timers.size, 1);
    assert.equal([...view.timers.values()][0].ms, 300000);

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
