const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/game_ui.js'), 'utf8');
const handler = source.slice(source.indexOf('// Keyboard shortcuts:'), source.indexOf('/* ──── Scene Image Functions'));

function setup() {
    function element(tagName = 'DIV') {
        return {
            tagName, disabled: false, hidden: false, inert: false, isContentEditable: false,
            visibility: 'visible', rendered: true, parentElement: null,
            classList: { contains: name => name === 'active' },
            getClientRects() {
                for (let current = this; current; current = current.parentElement) {
                    if (!current.rendered || current.hidden) return [];
                }
                return [{}];
            },
            contains(target) {
                for (let current = target; current; current = current.parentElement) {
                    if (current === this) return true;
                }
                return false;
            },
            closest(selector) {
                for (let current = this; current; current = current.parentElement) {
                    if (selector.includes('[inert]') && current.inert) return current;
                    if (selector.includes('[hidden]') && current.hidden) return current;
                    if (selector.includes('input') && current.tagName === 'INPUT') return current;
                    if (selector.includes('textarea') && current.tagName === 'TEXTAREA') return current;
                    if (selector.includes('select') && current.tagName === 'SELECT') return current;
                    if (selector.includes('[role="textbox"]') && current.role === 'textbox') return current;
                }
                return null;
            },
        };
    }
    const body = element('BODY');
    const main = element('MAIN');
    main.parentElement = body;
    const panel = element();
    panel.parentElement = main;
    const game = element();
    game.parentElement = panel;
    const narrative = element('P');
    narrative.parentElement = game;
    const choices = Array.from({ length: 4 }, () => {
        const button = element('BUTTON');
        button.parentElement = game;
        button.clicks = 0;
        button.click = () => button.clicks++;
        return button;
    });
    const suggestions = element('DETAILS');
    suggestions.open = true;
    suggestions.parentElement = game;
    const blockers = [];
    const listeners = {};
    const document = {
        body, documentElement: element('HTML'),
        getElementById: id => ({ 'panel-game': panel, 'game-play-screen': game, 'main-area': main, 'main-map-suggested-actions': suggestions })[id],
        querySelector: selector => selector.includes('mobile-open') ? blockers.find(blocker => blocker.mobile) || null : null,
        querySelectorAll: selector => selector === '#choices-area .choice-btn' ? choices : blockers.filter(blocker => !blocker.mobile),
        addEventListener(type, listener) { listeners[type] = listener; },
    };
    const context = vm.createContext({ document, isStreaming: false, currentSaveId: 'save-1',
        getComputedStyle: target => ({ visibility: target.visibility }) });
    vm.runInContext(handler, context);
    function press(overrides = {}) {
        const event = { key: '1', target: body, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; }, ...overrides };
        listeners.keydown(event);
        return event;
    }
    return { context, document, panel, game, main, narrative, choices, suggestions, blockers, element, press };
}

test('collapsed map suggestions do not accept number shortcuts until expanded', () => {
    const { choices, suggestions, press } = setup();
    for (const choice of choices) choice.parentElement = suggestions;
    suggestions.open = false;
    assert.equal(press().defaultPrevented, false);
    assert.equal(choices[0].clicks, 0);
    suggestions.open = true;
    assert.equal(press().defaultPrevented, true);
    assert.equal(choices[0].clicks, 1);
});

test('unmodified number keys select exactly the corresponding active game choice', () => {
    const { document, narrative, choices, press } = setup();
    for (let index = 0; index < choices.length; index++) {
        const event = press({ key: String(index + 1), target: index % 2 ? narrative : document.body });
        assert.equal(choices[index].clicks, 1);
        assert.equal(event.defaultPrevented, true);
    }
    press({ key: '5' });
    press({ key: 'F1' });
    assert.deepEqual(choices.map(choice => choice.clicks), [1, 1, 1, 1]);
});

test('hidden game screens and other application panels cannot dispatch actions', () => {
    for (const hide of [view => { view.game.rendered = false; },
        view => { view.panel.classList.contains = () => false; },
        view => { view.main.inert = true; }, view => { view.game.hidden = true; }]) {
        const view = setup();
        hide(view);
        const event = view.press();
        assert.equal(view.choices[0].clicks, 0);
        assert.equal(event.defaultPrevented, false);
    }
});

test('visible modals and mobile panels block shortcuts even with focus on the page body', () => {
    for (const mobile of [false, true]) {
        const { blockers, element, choices, press } = setup();
        const blocker = element();
        blocker.mobile = mobile;
        blockers.push(blocker);
        press();
        assert.equal(choices[0].clicks, 0);
    }
});

test('closed or visually hidden modals do not disable gameplay shortcuts', () => {
    for (const hide of [blocker => { blocker.rendered = false; },
        blocker => { blocker.hidden = true; }, blocker => { blocker.visibility = 'hidden'; }]) {
        const { blockers, element, choices, press } = setup();
        const blocker = element();
        hide(blocker);
        blockers.push(blocker);
        press();
        assert.equal(choices[0].clicks, 1);
    }
});

test('modifier keys, repeats, composition and previously handled events cannot select choices', () => {
    for (const flag of ['ctrlKey', 'metaKey', 'altKey', 'shiftKey', 'repeat', 'isComposing', 'defaultPrevented']) {
        const { choices, press } = setup();
        press({ [flag]: true });
        assert.equal(choices[0].clicks, 0, flag);
    }
});

test('editing fields and controls outside the game do not trigger choices', () => {
    for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A']) {
        const { element, choices, press } = setup();
        press({ target: element(tagName) });
        assert.equal(choices[0].clicks, 0, tagName);
    }
    for (const fieldType of ['editable', 'textbox']) {
        const { element, game, choices, press } = setup();
        const editor = element();
        editor.parentElement = game;
        const text = element('SPAN');
        text.parentElement = editor;
        if (fieldType === 'editable') text.isContentEditable = true;
        else editor.role = 'textbox';
        press({ target: text });
        assert.equal(choices[0].clicks, 0, fieldType);
    }
});

test('unavailable choices and busy or unloaded saves leave shortcuts untouched', () => {
    for (const block of [view => { view.choices[0].disabled = true; },
        view => { view.choices[0].rendered = false; }, view => { view.choices[0].visibility = 'hidden'; },
        view => { view.context.isStreaming = true; }, view => { view.context.currentSaveId = null; },
        view => { view.choices.length = 0; }]) {
        const view = setup();
        const first = view.choices[0];
        block(view);
        const event = view.press();
        assert.equal(first.clicks, 0);
        assert.equal(event.defaultPrevented, false);
    }
});
