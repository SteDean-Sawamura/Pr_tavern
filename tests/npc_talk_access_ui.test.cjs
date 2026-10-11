const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const presence = fs.readFileSync(path.join(__dirname, '../static/js/npc_presence.js'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const handlers = app.slice(app.indexOf('function _npcChatBlockReason('), app.indexOf('function backToHome('));
const openChat = app.slice(app.indexOf('function openNpcChat('), app.indexOf('function closeNpcChat('));

function setup(mapMode = false) {
    const input = { value: 'Hello.', disabled: false, focus() {} };
    const messages = [];
    const children = [];
    const alerts = [];
    const prepared = [];
    const requests = [];
    const oldQuickReplies = { removed: false, remove() { this.removed = true; } };
    const area = {
        insertAdjacentHTML: (_, html) => messages.push(html),
        querySelector: () => oldQuickReplies,
        appendChild: child => children.push(child),
        scrollHeight: 100,
    };
    const elements = {
        'main-map-view': { hidden: !mapMode },
        'npc-chat-input': input,
        'npc-chat-messages': area,
        'npc-typing': { insertAdjacentHTML: (_, html) => messages.push(html) },
        'npc-chat-info': {}, 'npc-chat-name': {}, 'npc-chat-modal': { style: {} },
    };
    const state = {
        player: { location: 'test_origin' }, game_time: '2030-01-01T09:00:00',
        npcs: { test_guide: { name: 'Practice guide', known: true, met: true, current_location: 'test_origin' } },
        npc_talk_block_reasons: { test_guide: '' }, npc_contacts: { test_guide: true },
    };
    const context = vm.createContext({
        currentSaveId: 'synthetic-save', currentState: state, currentNodeId: 'root',
        _npcChatId: 'test_guide', _npcChatVersion: 1, _saveLoadSequence: 1, _npcChatRequest: null,
        isStreaming: false, TextDecoder, finalResponse: null,
        alert: reason => alerts.push(reason),
        document: { getElementById: id => elements[id], createElement: () => ({ appendChild() {} }) },
        escapeHtml: value => String(value),
        prepareMainMapNpcContact: (...args) => prepared.push(args),
        appendNarrative() {}, renderChoices() {}, updateStatusPanel() {}, updateHeader() {},
        updateSwipeControls() {}, updateTurnIndicator() {},
        fetch: async (url, options) => {
            requests.push({ url, options });
            const packets = [new TextEncoder().encode(`data: ${JSON.stringify({ type: 'final', ...context.finalResponse })}\n\ndata: [DONE]\n\n`)];
            return { ok: true, body: { getReader: () => ({ read: async () => packets.length ? { value: packets.shift(), done: false } : { done: true } }) } };
        },
    });
    vm.runInContext(presence + '\n' + openChat + '\n' + handlers, context);
    return { context, input, messages, children, alerts, prepared, requests, oldQuickReplies, elements };
}

test('authoritative access takes precedence over schedule cache and companion shortcuts', () => {
    const { context } = setup();
    const state = context.currentState;
    assert.equal(context.NpcPresence.talkBlockReason('test_guide', state, {}), '');
    state.companions = ['test_guide'];
    state.npc_talk_block_reasons.test_guide = '当前无法对话';
    assert.equal(context.NpcPresence.talkBlockReason('test_guide', state, {}), '当前无法对话');
    state.npcs.test_guide.known = false;
    assert.equal(context.NpcPresence.talkBlockReason('test_guide', state, {}), '尚未认识');
});

test('legacy frontend fallback still rejects deceased and different-room actors', () => {
    const { context } = setup();
    const state = context.currentState;
    delete state.npc_talk_block_reasons;
    state.npcs.test_guide.alive = false;
    state.companions = ['test_guide'];
    assert.equal(context.NpcPresence.talkBlockReason('test_guide', state, null), '当前无法对话');
    delete state.npcs.test_guide.alive;
    state.companions = [];
    state.player.current_room = 'test_outer';
    state.npcs.test_guide.current_room = 'test_inner';
    assert.equal(context.NpcPresence.talkBlockReason('test_guide', state, null), '不在同一房间');
});

test('map opening prepares contact while traditional opening permits direct talk', () => {
    for (const mapMode of [false, true]) {
        const { context, prepared, elements } = setup(mapMode);
        context.currentState.npc_contacts = {};
        context.openNpcChat('test_guide');
        assert.equal(prepared.length, mapMode ? 1 : 0);
        assert.equal(elements['npc-chat-modal'].style.display, mapMode ? undefined : 'flex');
    }
});

test('sending rechecks changed access before consuming text or starting a request', async () => {
    for (const reason of ['当前无法对话', '不在当前位置', '不在同一房间']) {
        const { context, input, requests, alerts } = setup();
        context.currentState.npc_talk_block_reasons.test_guide = reason;
        await context.sendNpcMessage();
        assert.deepEqual(alerts, [reason]);
        assert.equal(requests.length, 0);
        assert.equal(input.value, 'Hello.');
        assert.equal(input.disabled, false);
        assert.equal(context.isStreaming, false);
    }
});

test('map sending cannot reuse a lost contact', async () => {
    const { context, input, requests, alerts } = setup(true);
    context.currentState.npc_contacts = {};
    await context.sendNpcMessage();
    assert.deepEqual(alerts, ['接触已中断，请重新寻找该角色']);
    assert.equal(requests.length, 0);
    assert.equal(input.value, 'Hello.');
});

test('a committed time change removes stale quick replies and blocks the next send', async () => {
    const { context, input, requests, messages, children, oldQuickReplies, alerts } = setup();
    context.finalResponse = {
        node_id: 'synthetic-talk-node', response: 'The practice shift starts.', quick_replies: ['Continue.'],
        state: { ...context.currentState, game_time: '2030-01-01T09:05:00', npc_contacts: {},
            npc_talk_block_reasons: { test_guide: '不在当前位置' } },
    };
    await context.sendNpcMessage();
    assert.equal(requests.length, 1);
    assert.equal(context.currentNodeId, 'synthetic-talk-node');
    assert.equal(context.currentState.game_time, '2030-01-01T09:05:00');
    assert.equal(oldQuickReplies.removed, true);
    assert.equal(children.length, 0);
    assert.ok(messages.some(html => html.includes('不在当前位置')));
    assert.ok(!messages.some(html => html.includes('显示失败')));
    input.value = 'Continue.';
    await context.sendNpcMessage();
    assert.equal(requests.length, 1);
    assert.deepEqual(alerts, ['不在当前位置']);
    assert.equal(input.value, 'Continue.');
});

test('traditional no-contact and map companion entry still complete a request', async () => {
    for (const mapMode of [false, true]) {
        const { context, requests, children, messages } = setup(mapMode);
        context.currentState.npc_contacts = {};
        if (mapMode) context.currentState.companions = ['test_guide'];
        context.finalResponse = { node_id: 'synthetic-talk-node', response: 'Welcome.',
            quick_replies: ['Continue.'], state: context.currentState };
        await context.sendNpcMessage();
        assert.equal(requests.length, 1);
        assert.equal(children.length, 1);
        assert.equal(context.isStreaming, false);
        assert.ok(!messages.some(html => html.includes('显示失败')));
    }
});
