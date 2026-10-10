const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const styles = fs.readFileSync(path.join(__dirname, '../static/css/style.css'), 'utf8');

test('latest button reveals the newest turn through outer scrolling on compact layouts', () => {
    const latest = { scrollIntoView(options) { this.scrollOptions = options; } };
    const area = { scrollTop: 0, scrollHeight: 1200, querySelector: () => latest };
    const context = vm.createContext({ document: { getElementById: () => area } });
    vm.runInContext(source.slice(source.indexOf('function scrollToLatest()'), source.indexOf('function updateTurnIndicator()')), context);

    context.scrollToLatest();
    assert.equal(latest.scrollOptions.block, 'end');
    assert.equal(area.scrollTop, 0);

    area.querySelector = () => null;
    context.scrollToLatest();
    assert.equal(area.scrollTop, 1200);
});

function setup(mobile) {
    function element() {
        const classes = new Set();
        return {
            style: {}, attributes: {}, hidden: true, children: [],
            classList: {
                add: name => classes.add(name),
                remove: name => classes.delete(name),
                contains: name => classes.has(name),
                toggle(name, force) {
                    if (force !== undefined) {
                        if (force) classes.add(name);
                        else classes.delete(name);
                        return force;
                    }
                    if (classes.has(name)) { classes.delete(name); return false; }
                    classes.add(name);
                    return true;
                },
            },
            setAttribute(name, value) { this.attributes[name] = value; },
            focus() { document.activeElement = this; },
            contains(target) { return this === target || this.children.some(child => child.contains(target)); },
            remove() {},
            querySelector() { return null; },
        };
    }
    const elements = new Map();
    const byId = id => {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
    };
    const button = element();
    const nav = element();
    nav.classList.add('active');
    byId('sidebar').querySelector = () => nav;
    byId('sidebar').children.push(nav);
    const statusTab = element();
    byId('status-panel').querySelector = () => statusTab;
    byId('status-panel').children.push(statusTab);
    byId('status-panel').style.display = 'flex';
    byId('header-controls').querySelector = () => byId('pipeline-mode-btn');
    byId('header-controls').children.push(byId('pipeline-mode-btn'));
    const storage = new Map();
    const listeners = {};
    const document = {
        activeElement: null,
        getElementById: byId,
        querySelector: selector => selector === '.sidebar-toggle' ? button : selector === '[data-panel="settings"]' ? nav : null,
        querySelectorAll: () => [],
        addEventListener(type, listener) { listeners[type] = listener; },
    };
    const context = vm.createContext({
        document, window: { matchMedia: query => ({ matches: query === '(max-width: 600px)' && mobile }) },
        localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
        currentSaveId: 'save-1',
        loadProfiles() {}, loadImageProfiles() {},
    });
    vm.runInContext(
        source.slice(source.indexOf('function switchPanel(panel) {'), source.indexOf('function switchStatusTab(')) +
        source.slice(source.indexOf('function _syncSidebarForViewport() {'), source.indexOf('let useStreamMode')),
        context,
    );
    return { context, document, byId, button, nav, statusTab, listeners, storage,
        resize(value) { mobile = value; context._syncSidebarForViewport(); } };
}

test('mobile navigation opens over the game and closes after panel selection', () => {
    const { context, document, byId, button, nav } = setup(true);
    assert.match(html, /id="sidebar-backdrop"[^>]*hidden/);
    context.toggleSidebar();
    assert.equal(byId('sidebar').classList.contains('mobile-open'), true);
    assert.equal(byId('main-area').inert, true);
    assert.equal(byId('sidebar-backdrop').hidden, false);
    assert.equal(button.attributes['aria-expanded'], 'true');
    assert.equal(document.activeElement, nav);
    context.switchPanel('settings');
    assert.equal(byId('sidebar').classList.contains('mobile-open'), false);
    assert.equal(byId('main-area').inert, false);
    assert.equal(byId('sidebar-backdrop').hidden, true);
    assert.equal(button.attributes['aria-expanded'], 'false');
    assert.equal(document.activeElement, button);
});

test('desktop navigation continues to collapse without a backdrop', () => {
    const { context, byId, button } = setup(false);
    context._syncSidebarForViewport();
    assert.equal(button.attributes['aria-expanded'], 'true');
    context.toggleSidebar();
    assert.equal(byId('sidebar').classList.contains('collapsed'), true);
    assert.equal(byId('sidebar-backdrop').hidden, true);
    assert.equal(button.attributes['aria-expanded'], 'false');
    context.toggleSidebar();
    assert.equal(byId('sidebar').classList.contains('collapsed'), false);
    assert.equal(button.attributes['aria-expanded'], 'true');
});

test('Escape closes mobile navigation even after focus leaves the sidebar', () => {
    const { context, document, byId, button, listeners } = setup(true);
    assert.doesNotMatch(html, /<aside id="sidebar"[^>]*onkeydown=/);
    context.toggleSidebar();
    byId('pipeline-mode-btn').focus();
    listeners.keydown({ key: 'Escape' });
    assert.equal(byId('sidebar').classList.contains('mobile-open'), false);
    assert.equal(byId('sidebar-backdrop').hidden, true);
    assert.equal(document.activeElement, button);
});

test('mobile status drawer preserves desktop collapse preference and restores focus', () => {
    const { context, document, byId, statusTab, storage } = setup(true);
    storage.set('statusPanelCollapsed', 'true');
    byId('status-panel').classList.add('collapsed');
    context.toggleStatusPanel();
    assert.equal(byId('status-panel').classList.contains('mobile-open'), true);
    assert.equal(byId('status-panel').inert, false);
    assert.equal(byId('main-area').inert, true);
    assert.equal(byId('sidebar-backdrop').hidden, false);
    assert.equal(byId('mobile-status-toggle').attributes['aria-expanded'], 'true');
    assert.equal(document.activeElement, statusTab);
    context.toggleStatusPanel();
    assert.equal(byId('status-panel').classList.contains('mobile-open'), false);
    assert.equal(byId('status-panel').classList.contains('collapsed'), true);
    assert.equal(byId('main-area').inert, false);
    assert.equal(byId('mobile-status-toggle').attributes['aria-expanded'], 'false');
    assert.equal(document.activeElement, byId('mobile-status-toggle'));
    assert.equal(storage.get('statusPanelCollapsed'), 'true');
});

test('mobile status, navigation and header settings are mutually exclusive', () => {
    const { context, document, byId, listeners } = setup(true);
    context.toggleStatusPanel();
    context.toggleSidebar();
    assert.equal(byId('status-panel').classList.contains('mobile-open'), false);
    assert.equal(byId('sidebar').classList.contains('mobile-open'), true);
    context.toggleHeaderControls();
    assert.equal(byId('sidebar').classList.contains('mobile-open'), false);
    assert.equal(byId('header-controls').classList.contains('mobile-open'), true);
    assert.equal(byId('header-controls-toggle').attributes['aria-expanded'], 'true');
    assert.equal(byId('main-area').inert, true);
    assert.equal(document.activeElement, byId('pipeline-mode-btn'));
    listeners.keydown({ key: 'Escape' });
    assert.equal(byId('header-controls').classList.contains('mobile-open'), false);
    assert.equal(byId('header-controls-toggle').attributes['aria-expanded'], 'false');
    assert.equal(byId('main-area').inert, false);
    assert.equal(document.activeElement, byId('header-controls-toggle'));
    context.toggleStatusPanel();
    byId('pipeline-mode-btn').focus();
    listeners.keydown({ key: 'Escape' });
    assert.equal(byId('status-panel').classList.contains('mobile-open'), false);
    assert.equal(document.activeElement, byId('mobile-status-toggle'));
});

test('changing panels closes mobile overlays and leaves focus on visible navigation', () => {
    for (const open of ['toggleStatusPanel', 'toggleHeaderControls']) {
        const { context, document, byId, button } = setup(true);
        context[open]();
        context.switchPanel('settings');
        assert.equal(byId('status-panel').classList.contains('mobile-open'), false);
        assert.equal(byId('header-controls').classList.contains('mobile-open'), false);
        assert.equal(byId('status-panel').style.display, 'none');
        assert.equal(byId('sidebar-backdrop').hidden, true);
        assert.equal(byId('main-area').inert, false);
        assert.equal(document.activeElement, button);
    }
});

test('leaving mobile clears overlays and does not reopen them on return', () => {
    for (const open of ['toggleStatusPanel', 'toggleSidebar', 'toggleHeaderControls']) {
        const { context, document, byId, button, resize, storage } = setup(true);
        context[open]();
        resize(false);
        assert.equal(byId('sidebar-backdrop').hidden, true);
        assert.equal(byId('main-area').inert, false);
        assert.equal(byId('status-panel').inert, false);
        assert.equal(byId('header-controls').inert, false);
        assert.equal(document.activeElement, button);
        resize(true);
        for (const panel of ['sidebar', 'status-panel', 'header-controls']) {
            assert.equal(byId(panel).classList.contains('mobile-open'), false);
        }
        assert.equal(storage.has('statusPanelCollapsed'), false);
    }
});

test('entering mobile moves focus away from newly hidden controls', () => {
    const { document, byId, button, nav, statusTab, resize } = setup(false);
    for (const [focused, trigger] of [[nav, button], [statusTab, byId('mobile-status-toggle')],
        [byId('pipeline-mode-btn'), byId('header-controls-toggle')]]) {
        resize(false);
        focused.focus();
        resize(true);
        assert.equal(document.activeElement, trigger);
    }
});

test('status drawer cannot open outside an active visible game', () => {
    const { context, byId } = setup(true);
    context.currentSaveId = null;
    context.toggleStatusPanel();
    assert.equal(byId('status-panel').classList.contains('mobile-open'), false);
    context.currentSaveId = 'save-1';
    byId('status-panel').style.display = 'none';
    context.toggleStatusPanel();
    assert.equal(byId('status-panel').classList.contains('mobile-open'), false);
});

test('mobile controls reuse existing model and state elements with accessible close actions', () => {
    assert.match(html, /id="sidebar-backdrop"[^>]*onclick="_closeMobilePanels\(\)"/);
    assert.match(html, /id="mobile-status-toggle"[^>]*aria-controls="status-panel"[^>]*aria-expanded="false"/);
    assert.match(html, /id="header-controls-toggle"[^>]*aria-controls="header-controls"[^>]*aria-expanded="false"/);
    assert.match(html, /class="mobile-status-close"[^>]*aria-label="关闭游戏状态"/);
    for (const id of ['pipeline-mode-btn', 'quick-profile-select', 'status-panel', 'freeform-input']) {
        assert.equal(html.split(`id="${id}"`).length - 1, 1);
    }
    assert.match(styles, /#app-container #status-panel\.mobile-open\s*\{[^}]*display:\s*flex\s*!important/);
    assert.match(styles, /#header-controls\.mobile-open\s*\{[^}]*display:\s*flex/);
});

test('turn controls wrap instead of shrinking or overflowing narrow screens', () => {
    assert.match(styles, /#swipe-controls\s*\{[^}]*flex-wrap:\s*wrap/);
    assert.match(styles, /\.swipe-btn\s*\{[^}]*flex-shrink:\s*0/);
    const compactStyles = styles.slice(styles.indexOf('@container gameplay (max-width: 560px)'), styles.indexOf('#narrative-content {'));
    assert.match(compactStyles, /\.swipe-btn\s*\{[^}]*min-width:\s*44px;[^}]*min-height:\s*44px/);
});
