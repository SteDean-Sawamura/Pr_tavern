const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/main_map_view.js'), 'utf8');
const restore = source.match(/function restoreMainMapView\(\) \{[\s\S]*?\n\}/)[0];

function setup(visible, enabled = true) {
    const elements = new Map();
    const byId = id => {
        if (!elements.has(id)) elements.set(id, {
            hidden: false, textContent: '', scrollTop: 0,
            classList: { remove() {}, toggle() {}, contains: () => false },
            setAttribute() {},
        });
        return elements.get(id);
    };
    const map = byId('main-map-view');
    map.hidden = !visible;
    const activity = byId('main-map-activity');
    let scrollTop = 0;
    Object.defineProperties(activity, {
        scrollHeight: { get: () => map.hidden ? 0 : 160 },
        scrollTop: {
            get: () => scrollTop,
            set: value => { scrollTop = Math.max(0, Math.min(value, activity.scrollHeight - 40)); },
        },
    });
    const context = vm.createContext({
        document: { getElementById: byId }, window: { _activeGameAction: null },
        localStorage: { getItem: () => enabled ? 'true' : 'false' },
        currentSaveId: 'test-save', currentState: null,
        _mainMapChoosing: null, _mainMapLayoutSaveId: 'test-save',
        _mainMapActivitySaveId: 'test-save', _mainMapAgentSaveId: null,
        _mainMapPrivacySaveId: 'test-save', _mainMapFocusViewSaveId: 'test-save',
        _mainMapChoiceRequest: 0, _mainMapChoiceSaveId: 'test-save',
        _mainMapPreparedAction: '', _mainMapAgentAction: null,
        _mainMapViewKey: saveId => saveId,
        _syncMainMapFocus() {}, _syncMainMapCommandDock() {},
        _syncMainMapAgentDock() {}, renderMainMapStoryChoices() {},
    });
    vm.runInContext(restore, context);
    return { context, activity, map };
}

test('entering the map reveals the newest progress recorded while it was hidden', () => {
    const view = setup(false);
    view.activity.scrollTop = view.activity.scrollHeight;
    assert.equal(view.activity.scrollTop, 0);
    view.context.restoreMainMapView();
    assert.equal(view.map.hidden, false);
    assert.equal(view.activity.scrollTop, 120);
});

test('restoring an already visible map preserves the progress position chosen by the player', () => {
    const view = setup(true);
    view.activity.scrollTop = 40;
    view.context.restoreMainMapView();
    assert.equal(view.activity.scrollTop, 40);
});

test('leaving the map does not reposition its progress history', () => {
    const view = setup(true, false);
    view.activity.scrollTop = 40;
    view.context.restoreMainMapView();
    assert.equal(view.map.hidden, true);
    assert.equal(view.activity.scrollTop, 40);
});

for (const [description, event, expected] of [
    ['failed regeneration with a discarded draft', { server_error: true, regeneration: true, discard_partial: true },
        '重新生成失败，原回合仍保留；可重试'],
    ['failed regeneration without a draft', { server_error: true, regeneration: true },
        '重新生成失败，原回合仍保留；可重试'],
    ['a rejected action draft', { server_error: true, discard_partial: true },
        '结算校验未通过，草稿已撤回'],
    ['an interrupted connection', { regeneration: true }, '推演断开，请核对存档后再行动'],
]) test(`map explains ${description} without conflating failure causes`, () => {
    const view = setup(true);
    const token = {};
    const removed = [];
    const draft = { className: 'main-map-activity-entry draft', remove: () => removed.push(true) };
    view.activity.children = [draft];
    view.activity.appendChild = child => view.activity.children.push(child);
    view.context.document.createElement = () => ({});
    view.context.window._activeGameAction = token;
    view.context._mainMapActivityAction = token;
    const record = source.match(/function recordMainMapStreamEvent\(saveId, actionToken, event\) \{[\s\S]*?\n\}/)[0];
    vm.runInContext(record, view.context);
    view.context.recordMainMapStreamEvent('test-save', token, { type: 'error', ...event });
    assert.equal(view.activity.children.at(-1).textContent, expected);
    assert.deepEqual(removed, [true]);
    assert.equal(view.activity.hidden, false);
});

for (const boundary of ['retry', 'save', 'activity-save', 'action', 'newer-entry', 'detached']) {
    test(`map withdraws a rejected draft and guards its retry against ${boundary}`, async () => {
        const view = setup(true);
        const token = {};
        let retries = 0;
        function element() {
            return {
                className: '', children: [], listeners: {},
                classList: { add() {}, remove() {} },
                appendChild(child) {
                    child.remove();
                    this.children.push(child);
                    child.parentElement = this;
                },
                remove() {
                    if (!this.parentElement) return;
                    const siblings = this.parentElement.children;
                    siblings.splice(siblings.indexOf(this), 1);
                    this.parentElement = null;
                },
                addEventListener(type, callback) { this.listeners[type] = callback; },
            };
        }
        Object.assign(view.activity, element());
        view.context.document.createElement = element;
        view.context.window._activeGameAction = token;
        view.context._mainMapActivityAction = token;
        const record = source.match(/function recordMainMapStreamEvent\(saveId, actionToken, event\) \{[\s\S]*?\n\}/)[0];
        vm.runInContext(record, view.context);
        view.context.recordMainMapStreamEvent('test-save', token, { type: 'text', content: 'Synthetic rejected draft' });
        const draft = view.activity.children[0];
        assert.equal(draft.className, 'main-map-activity-entry draft');
        view.context.recordMainMapStreamEvent('test-save', token, {
            type: 'error', discard_partial: true, server_error: true, retryable: true,
            retry: async () => { retries += 1; return 'retried'; },
        });
        assert.equal(draft.parentElement, null);
        assert.equal(view.activity.children.length, 1);
        const entry = view.activity.children[0];
        assert.equal(entry.textContent, '结算校验未通过，草稿已撤回');
        const button = entry.children[0];
        assert.equal(button.textContent, '重新推演');
        if (boundary === 'save') view.context.currentSaveId = 'other';
        if (boundary === 'activity-save') view.context._mainMapActivitySaveId = 'other';
        if (boundary === 'action') view.context._mainMapActivityAction = {};
        if (boundary === 'newer-entry') view.activity.appendChild(element());
        if (boundary === 'detached') entry.remove();
        const result = await button.listeners.click();
        assert.equal(retries, boundary === 'retry' ? 1 : 0);
        assert.equal(result, boundary === 'retry' ? 'retried' : undefined);
    });
}
