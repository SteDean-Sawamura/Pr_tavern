const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const hintHandler = source.slice(
    source.indexOf('function requestRegenerationHint() {'),
    source.indexOf('async function regenerateResponse() {'),
);
const handlers = source.slice(
    source.indexOf('async function regenerateResponse() {'),
    source.indexOf('async function swipeLeft() {'),
);

function setup() {
    const rendered = [];
    const text = { innerHTML: '<p>Old</p>' };
    const block = { querySelector: () => text };
    const content = { querySelectorAll: () => [block] };
    const buttons = Object.fromEntries(['btn-regenerate', 'btn-regen-choices', 'btn-regen-state', 'btn-continue']
        .map(id => [id, { disabled: false }]));
    const context = vm.createContext({
        currentSaveId: 'save', currentState: { scene: 'old' }, isStreaming: false,
        window: {}, document: { getElementById: id => ({ 'narrative-content': content, ...buttons })[id] },
        requestRegenerationHint: async () => '', _shouldUseStream: () => false,
        API: { post: async () => ({}) },
        renderChoices: choices => rendered.push(choices),
        updateSwipeControls() {}, showStateChanges() {}, updateStatusPanel() {}, updateHeader() {},
        formatNarrativeHtml: value => `<p>${value}</p>`,
        alert: message => { throw new Error(message); }, console: { error() {} },
        localStorage: { getItem: () => null }, TextDecoder,
    });
    vm.runInContext(handlers, context);
    return { context, buttons, rendered, text };
}

function switchSave(view) {
    view.context.currentSaveId = 'other';
    view.context.currentState = { scene: 'other' };
    view.context.window._activeGameAction = null;
    view.context.isStreaming = true;
}

test('late non-stream regeneration does not update another save or release its lock', async () => {
    const view = setup();
    let resolveResponse;
    view.context.API.post = () => new Promise(resolve => { resolveResponse = resolve; });
    const pending = view.context.regenerateResponse();
    await new Promise(setImmediate);
    switchSave(view);
    view.buttons['btn-regenerate'].disabled = true;
    resolveResponse({ narrative: 'Stale', choices: ['Stale choice'], state: { scene: 'stale' } });
    await pending;

    assert.equal(view.text.innerHTML, '<p>Old</p>');
    assert.equal(view.rendered.length, 0);
    assert.equal(view.context.currentState.scene, 'other');
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.buttons['btn-regenerate'].disabled, true);
});

test('late partial-regeneration response does not change another save', async () => {
    const view = setup();
    let resolveResponse;
    view.context.API.post = () => new Promise(resolve => { resolveResponse = resolve; });
    const pending = view.context.regenerateStage('choices');
    switchSave(view);
    resolveResponse({ choices: ['Stale choice'], state: { scene: 'stale' } });
    await pending;

    assert.equal(view.rendered.length, 0);
    assert.equal(view.context.currentState.scene, 'other');
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.buttons['btn-regen-choices'].disabled, true);
});

test('agentic partial regeneration uses the shared stream handler', async () => {
    const view = setup();
    view.context._shouldUseStream = () => true;
    let selected;
    view.context.submitActionStream = (action, url) => {
        selected = { action, url };
        return Promise.resolve();
    };
    view.context.fetch = () => { throw new Error('independent fetch should not run'); };
    await view.context.regenerateStage('choices');

    assert.equal(selected.action.type, 'regen');
    assert.equal(selected.action.stage, 'choices');
    assert.equal(selected.url, '/api/game/save/regenerate/stream?stage=choices');
    assert.equal(view.context.isStreaming, false);
});

test('late continuation does not replace the loaded narrative', async () => {
    const view = setup();
    let resolveResponse;
    view.context.API.post = () => new Promise(resolve => { resolveResponse = resolve; });
    const pending = view.context.continueNarrative();
    switchSave(view);
    resolveResponse({ narrative: 'Stale continuation' });
    await pending;

    assert.equal(view.text.innerHTML, '<p>Old</p>');
    assert.equal(view.context.isStreaming, true);
    assert.equal(view.buttons['btn-continue'].disabled, true);
});

test('stream regeneration wrapper does not unlock a new action', async () => {
    const view = setup();
    view.context._shouldUseStream = () => true;
    let finish;
    view.context.submitActionStream = () => new Promise(resolve => { finish = resolve; });
    const pending = view.context.regenerateResponse();
    await new Promise(setImmediate);
    switchSave(view);
    finish();
    await pending;

    assert.equal(view.context.isStreaming, true);
});

test('regeneration hint dialog submits text and cancels without native prompt', async () => {
    function element() {
        const listeners = {};
        return {
            style: {}, value: '', listeners,
            addEventListener: (type, listener) => { listeners[type] = listener; },
            removeEventListener: type => { delete listeners[type]; },
            focus() {},
        };
    }
    const elements = Object.fromEntries([
        'regenerate-hint-modal', 'regenerate-hint-form',
        'regenerate-hint-input', 'regenerate-hint-cancel',
    ].map(id => [id, element()]));
    const document = {
        listeners: {}, getElementById: id => elements[id],
        addEventListener(type, listener) { this.listeners[type] = listener; },
        removeEventListener(type) { delete this.listeners[type]; },
    };
    const context = vm.createContext({ document, Promise });
    vm.runInContext(hintHandler, context);

    const submitted = context.requestRegenerationHint();
    elements['regenerate-hint-input'].value = '核对街口';
    elements['regenerate-hint-form'].listeners.submit({ preventDefault() {} });
    assert.equal(await submitted, '核对街口');
    assert.equal(elements['regenerate-hint-modal'].style.display, 'none');
    assert.equal(Object.keys(document.listeners).length, 0);

    const cancelled = context.requestRegenerationHint();
    elements['regenerate-hint-cancel'].listeners.click();
    assert.equal(await cancelled, null);
});

test('regeneration does not switch the new save after hint confirmation', async () => {
    const view = setup();
    let confirmHint;
    let called = false;
    view.context.requestRegenerationHint = () => new Promise(resolve => { confirmHint = resolve; });
    view.context.API.post = () => { called = true; return Promise.resolve({}); };
    const pending = view.context.regenerateResponse();
    switchSave(view);
    confirmHint('重写');
    await pending;
    assert.equal(called, false);
});
