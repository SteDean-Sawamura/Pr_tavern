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
