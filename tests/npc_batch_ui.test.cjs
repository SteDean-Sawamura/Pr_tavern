const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const handler = source.slice(source.indexOf('async function triggerNpcDialogue()'), source.indexOf('function renderNpcSpeeches('));

function setup() {
    const button = { disabled: false, textContent: 'NPC对话' };
    const actionButton = { disabled: false, textContent: '行动' };
    const waitButton = { disabled: false, textContent: '等待' };
    const restButton = { disabled: false, textContent: '休息' };
    const choiceButton = { disabled: false, textContent: '观察' };
    const streamButton = { disabled: false, textContent: '流式' };
    const actionButtons = [button, actionButton, waitButton, restButton, choiceButton];
    const input = { disabled: false, value: '先观察，不出发' };
    const requests = [];
    const speeches = [];
    const notifications = [];
    const context = vm.createContext({
        currentSaveId: 'first', isStreaming: false, window: {},
        document: {
            getElementById: id => ({ 'npc-dialogue-btn': button, 'freeform-input': input })[id],
            querySelectorAll: selector => {
                if (selector === '#freeform-area button:not(#stream-toggle), .choice-btn') return actionButtons;
                if (selector === '#freeform-area button:not(#stream-toggle)') return actionButtons.filter(control => control !== choiceButton);
                throw new Error(`Unexpected selector: ${selector}`);
            },
        },
        API: { post: (url, payload) => new Promise((resolve, reject) => requests.push({ url, payload, resolve, reject })) },
        renderNpcSpeeches: value => speeches.push(value),
        showNotification: (...args) => notifications.push(args),
    });
    vm.runInContext(handler, context);
    return { context, button, input, requests, speeches, notifications, actionButtons, choiceButton, streamButton };
}

test('batch dialogue locks gameplay, prevents double submission and preserves the draft', async () => {
    const view = setup();
    const pending = view.context.triggerNpcDialogue();
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.button.disabled, true);
    assert.equal(view.button.textContent, '对话中…');
    assert.equal(view.input.disabled, true);
    assert.ok(view.actionButtons.every(control => control.disabled));
    assert.equal(view.streamButton.disabled, false);
    await view.context.triggerNpcDialogue();
    assert.equal(view.requests.length, 1);
    assert.equal(view.requests[0].url, '/api/game/first/npc-dialogue');
    const result = [{ npc_id: 'guest', name: '访客', speech: '你好' }];
    view.requests[0].resolve({ speeches: result });
    await pending;
    assert.deepEqual(view.speeches, [result]);
    assert.equal(view.input.value, '先观察，不出发');
    assert.equal(view.input.disabled, false);
    assert.equal(view.button.disabled, false);
    assert.equal(view.button.textContent, 'NPC对话');
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.context.window._activeGameAction, null);
    assert.ok(view.actionButtons.every(control => !control.disabled));
});

test('batch dialogue failure shows the server message and restores controls', async () => {
    const view = setup();
    const pending = view.context.triggerNpcDialogue();
    view.requests[0].reject(new Error('请先配置 AI 模型'));
    await pending;
    assert.deepEqual(view.notifications, [['请先配置 AI 模型', 'error']]);
    assert.equal(view.context.isStreaming, false);
    assert.equal(view.input.disabled, false);
    assert.equal(view.input.value, '先观察，不出发');
    assert.equal(view.button.disabled, false);
    assert.equal(view.button.textContent, 'NPC对话');
    assert.ok(view.actionButtons.every(control => !control.disabled));
});

test('batch completion preserves controls that were already disabled', async () => {
    const view = setup();
    view.choiceButton.disabled = true;
    const pending = view.context.triggerNpcDialogue();
    view.requests[0].resolve({ speeches: [] });
    await pending;
    assert.equal(view.choiceButton.disabled, true);
    assert.ok(view.actionButtons.filter(control => control !== view.choiceButton).every(control => !control.disabled));
});

test('an empty batch gives feedback without rendering an empty reply', async () => {
    const view = setup();
    const pending = view.context.triggerNpcDialogue();
    view.requests[0].resolve({ speeches: [] });
    await pending;
    assert.equal(view.speeches.length, 0);
    assert.deepEqual(view.notifications, [['当前场景没有NPC发言', 'info']]);
    assert.equal(view.context.isStreaming, false);
});

for (const result of ['success', 'failure']) {
    test(`a stale batch ${result} cannot affect a different save or its pending action`, async () => {
        const view = setup();
        const pending = view.context.triggerNpcDialogue();
        view.context.currentSaveId = 'second';
        const currentAction = {};
        view.context.window._activeGameAction = currentAction;
        view.context.isStreaming = true;
        view.input.disabled = true;
        view.button.disabled = true;
        if (result === 'success') view.requests[0].resolve({ speeches: [{ speech: '旧存档回复' }] });
        else view.requests[0].reject(new Error('旧存档失败'));
        await pending;
        assert.equal(view.speeches.length, 0);
        assert.equal(view.notifications.length, 0);
        assert.equal(view.context.window._activeGameAction, currentAction);
        assert.equal(view.context.isStreaming, true);
        assert.equal(view.input.disabled, true);
        assert.equal(view.button.disabled, true);
        assert.ok(view.actionButtons.every(control => control.disabled));
    });
}

test('a replaced request in the same save cannot apply its late reply', async () => {
    const view = setup();
    const pending = view.context.triggerNpcDialogue();
    view.context.window._activeGameAction = {};
    view.requests[0].resolve({ speeches: [{ speech: '旧分支回复' }] });
    await pending;
    assert.equal(view.speeches.length, 0);
    assert.equal(view.context.isStreaming, true);
});

test('busy and unloaded games cannot start a batch', async () => {
    const view = setup();
    view.context.isStreaming = true;
    await view.context.triggerNpcDialogue();
    view.context.isStreaming = false;
    view.context.currentSaveId = null;
    await view.context.triggerNpcDialogue();
    assert.equal(view.requests.length, 0);
});

test('batch replies scroll the new conversation into view across nested map containers', () => {
    const scrolls = [];
    const content = { children: [], appendChild(element) { this.children.push(element); } };
    const area = { scrollTop: 0, scrollHeight: 2000 };
    const context = vm.createContext({
        document: {
            getElementById: id => ({ 'narrative-content': content, 'narrative-area': area })[id],
            createElement: () => ({ appendChild() {}, scrollIntoView(options) { scrolls.push({ element: this, options }); } }),
        },
        escapeHtml: value => value,
    });
    vm.runInContext(source.slice(source.indexOf('function renderNpcSpeeches('), source.indexOf('async function _runImmediateStateAction(')), context);
    context.renderNpcSpeeches([{ name: '访客', speech: '你好' }]);
    assert.equal(scrolls.length, 1);
    assert.equal(scrolls[0].element, content.children[0]);
    assert.equal(scrolls[0].options.block, 'nearest');
    assert.equal(area.scrollTop, 0);
});

test('entering a saved game clears the previous batch button and input state', () => {
    const view = setup();
    const resetStart = source.indexOf('function showGameScreen(');
    const resetEnd = source.indexOf("document.getElementById('game-start-screen').style.display", resetStart);
    const elements = { 'npc-dialogue-btn': view.button, 'freeform-input': view.input,
        'game-play-screen': { style: { display: 'none' } } };
    Object.assign(view.context, { _historyRenderSequence: 0, _pendingHistoryPage: null, _showGameOverlay() {} });
    view.context.document.getElementById = id => elements[id] || null;
    view.button.disabled = true;
    view.button.textContent = '对话中…';
    view.actionButtons.forEach(control => { control.disabled = true; });
    view.input.disabled = true;
    view.context.isStreaming = true;
    vm.runInContext(source.slice(resetStart, resetEnd) + '}', view.context);
    view.context.showGameScreen({});
    assert.equal(view.button.disabled, false);
    assert.equal(view.button.textContent, 'NPC对话');
    assert.equal(view.input.disabled, false);
    assert.equal(view.context.isStreaming, false);
    assert.ok(view.actionButtons.filter(control => control !== view.choiceButton).every(control => !control.disabled));
});
