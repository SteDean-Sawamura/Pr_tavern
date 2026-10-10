const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const streamSource = fs.readFileSync(path.join(__dirname, '../static/js/game_ui.js'), 'utf8');
const styles = fs.readFileSync(path.join(__dirname, '../static/css/style.css'), 'utf8');

function turn(naturalHeight, classes = []) {
    const names = new Set(classes);
    return {
        set naturalHeight(value) { naturalHeight = value; },
        get scrollHeight() { return naturalHeight; },
        get clientHeight() { return names.has('is-truncated') ? Math.min(naturalHeight, 280) : naturalHeight; },
        classList: {
            contains: name => names.has(name),
            add: name => names.add(name),
            toggle(name, enabled) {
                if (enabled) names.add(name);
                else names.delete(name);
            },
        },
    };
}

test('only overflowing old turns show the expand hint, including after map reflow', () => {
    const short = turn(70);
    const long = turn(400);
    const latest = turn(400, ['latest']);
    const blocks = [short, long, latest];
    let resized;
    let observed;
    const content = {
        clientWidth: 700,
        querySelectorAll(selector) {
            assert.equal(selector, '.turn-block:not(.latest):not(.expanded)');
            return blocks.filter(block => !block.classList.contains('latest') && !block.classList.contains('expanded'));
        },
    };
    const context = vm.createContext({
        document: { getElementById: () => content },
        ResizeObserver: class {
            constructor(callback) { resized = callback; }
            observe(target) { observed = target; }
        },
    });
    vm.runInContext(source.slice(source.indexOf('let _turnOverflowObserver'), source.indexOf('function createTurnBlock(')), context);
    context._syncTurnOverflow();
    assert.equal(observed, content);
    assert.equal(short.classList.contains('is-truncated'), false);
    assert.equal(long.classList.contains('is-truncated'), true);
    assert.equal(latest.classList.contains('is-truncated'), false);

    short.naturalHeight = 320;
    long.naturalHeight = 90;
    content.clientWidth = 400;
    resized();
    assert.equal(short.classList.contains('is-truncated'), true);
    assert.equal(long.classList.contains('is-truncated'), false);

    content.clientWidth = 0;
    short.naturalHeight = 70;
    resized();
    assert.equal(short.classList.contains('is-truncated'), true);
    content.clientWidth = 700;
    resized();
    assert.equal(short.classList.contains('is-truncated'), false);
    assert.match(styles, /\.turn-block:not\(\.latest\)\.is-truncated::after\s*\{/);
    assert.match(styles, /\.turn-block:not\(\.latest\)\.is-truncated\s*\{\s*max-height: 280px/);
    assert.doesNotMatch(styles, /\.turn-block:not\(\.latest\)::after\s*\{/);
});

test('restoration, pagination, new turns and streaming all refresh overflow', () => {
    assert.match(source, /if \(data\.choices\) renderChoices\(data\.choices\);\s*_syncTurnOverflow\(\)/);
    assert.match(source, /content\.insertBefore\(block, firstTurnBlock\);\s*\}\);\s*_syncTurnOverflow\(\)/);
    assert.match(source, /content\.appendChild\(block\);\s*_syncTurnOverflow\(\)/);
    assert.match(streamSource, /if \(b !== block\) b\.classList\.remove\('latest'\);\s*\}\);\s*if \(typeof _syncTurnOverflow === 'function'\) _syncTurnOverflow\(\)/);
});

test('map interruption controls stay pinned above the scrolling tool log', () => {
    const controls = styles.match(/#main-map-agent-dock > \.agent-controls\s*\{([^}]+)\}/)?.[1];
    assert.ok(controls, 'the map dock needs its own persistent interruption surface');
    assert.match(controls, /position:\s*sticky\s*;/);
    assert.match(controls, /bottom:\s*0\s*;/);
    assert.match(controls, /z-index:\s*1\s*;/);
    assert.match(controls, /background:\s*var\(--bg-inset\)\s*;/);
});
