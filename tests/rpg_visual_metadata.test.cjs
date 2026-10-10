const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const utils = fs.readFileSync(path.join(__dirname, '../static/js/utils.js'), 'utf8')
    .replace(/^import .*;$/gm, '').replace(/^export /gm, '');
const renderer = fs.readFileSync(path.join(__dirname, '../static/js/modules/map-renderer.js'), 'utf8');
const drawPerson = renderer.slice(renderer.indexOf('function drawPixelPerson('),
    renderer.indexOf('function drawPixelPlayerCharacter('));

test('NPC colors follow active script metadata without leaking across saves', () => {
    let gameState = { npcs: [{ id: 'visitor', visual_profile: { map_color: '#123456' } }] };
    const context = vm.createContext({ get: () => gameState });
    vm.runInContext(utils, context);
    assert.equal(vm.runInContext("npcColor('visitor')", context), '#123456');
    gameState = { npcs: [{ id: 'visitor' }] };
    const fallback = vm.runInContext("npcColor('visitor')", context);
    assert.notEqual(fallback, '#123456');
    assert.equal(vm.runInContext("npcColor('visitor')", context), fallback);
});

test('NPC hairstyle and accessories are selected from visual profiles', () => {
    function render(profile) {
        const elements = [];
        const context = vm.createContext({ profile, group: { appendChild: element => elements.push(element) },
            svgEl: (tag, attributes) => ({ tag, ...attributes }) });
        vm.runInContext(drawPerson + "\ndrawPixelPerson(group, '#112233', profile);", context);
        return elements;
    }
    const plain = render({});
    assert.equal(render({ accessory: 'tie' }).length, plain.length + 2);
    assert.equal(render({ accessory: 'whistle' }).length, plain.length + 2);
    assert.equal(render({ accessory: 'earrings' }).length, plain.length + 2);
    const custom = render({ hair_style: 'long', hair_color: '#987654', shirt_color: '#456789' });
    assert.ok(custom.some(element => element.fill === '#987654'));
    assert.ok(custom.some(element => element.fill === '#456789'));
});
