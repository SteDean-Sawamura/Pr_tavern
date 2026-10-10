const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/script_builder.js'), 'utf8');
const helper = source.slice(source.indexOf('function _preserveScriptVisibilityMetadata('),
    source.indexOf('async function buildScriptFromWizard('));

function preserve(script, original) {
    const context = vm.createContext({ script, original });
    vm.runInContext(helper + '\n_preserveScriptVisibilityMetadata(script, original);', context);
    return script;
}

test('wizard preserves the script-level visibility policy', () => {
    const begin = source.indexOf('const _OVERLAY_KEYS =');
    const end = source.indexOf('const script =', begin);
    const policy = { default_event_visibility: 'world', private_world_properties: ['counter'] };
    const aliases = { old_room: 'new_room' };
    const context = vm.createContext({ _rawScriptOverlay: { visibility_policy: policy, location_aliases: aliases } });
    vm.runInContext(source.slice(begin, end) + '\nglobalThis.picked = _pickedOverlay;', context);
    assert.deepEqual(context.picked.visibility_policy, policy);
    assert.deepEqual(context.picked.location_aliases, aliases);
    assert.match(source, /_preserveScriptVisibilityMetadata\(script, _rawScriptOverlay\);\s*return script;/);
});

test('entity metadata survives round trips without overwriting edited fields', () => {
    const original = {
        world_properties: [{ id: 'counter', visibility: 'hidden' }],
        persistent_states: [{ id: 'status', expiration_visibility: 'world' }],
        locations: [{ id: 'room', public_description: 'Public outline' }],
        npcs: [{ id: 'visitor', public_profile: { title: 'Visitor' }, visual_profile: { accessory: 'tie' } }],
        lorebook: [{ id: 'fact', visibility: 'world', discoverable: false, known_by_npcs: ['visitor'] }],
        one_time_events: [{ id: 'notice', visibility: 'world', known_by_npcs: ['visitor'] }],
        story_tree: { trees: [{ id: 'main', nodes: [{ id: 'step', visibility: 'world' }] }] },
        npc_relationships: [{ from: 'visitor', to: 'host', public_type: 'acquaintance' }],
    };
    const script = {
        world_properties: [{ id: 'counter' }], persistent_states: [{ id: 'status' }],
        locations: [{ id: 'room' }], npcs: [{ id: 'visitor' }], lorebook: [{ id: 'fact' }],
        one_time_events: [{ id: 'notice', visibility: 'public' }],
        story_tree: { trees: [{ id: 'main', nodes: [{ id: 'step' }] }] },
        npc_relationships: [{ from: 'visitor', to: 'host' }],
    };
    preserve(script, original);
    assert.equal(script.world_properties[0].visibility, 'hidden');
    assert.equal(script.persistent_states[0].expiration_visibility, 'world');
    assert.equal(script.locations[0].public_description, 'Public outline');
    assert.equal(script.lorebook[0].discoverable, false);
    assert.equal(script.one_time_events[0].visibility, 'public');
    assert.equal(script.story_tree.trees[0].nodes[0].visibility, 'world');
    assert.equal(script.npc_relationships[0].public_type, 'acquaintance');
    assert.equal(script.npcs[0].visual_profile.accessory, 'tie');
    script.npcs[0].visual_profile.accessory = 'earrings';
    assert.equal(original.npcs[0].visual_profile.accessory, 'tie');
    script.npcs[0].public_profile.title = 'Edited title';
    script.lorebook[0].known_by_npcs.push('host');
    assert.equal(original.npcs[0].public_profile.title, 'Visitor');
    assert.deepEqual(original.lorebook[0].known_by_npcs, ['visitor']);
});

test('removed entities and unrelated IDs are never reintroduced', () => {
    const script = {
        npcs: [{ id: 'new' }],
        story_tree: { trees: [{ id: 'other', nodes: [{ id: 'step' }] }] },
    };
    preserve(script, {
        npcs: [{ id: 'old', public_profile: { title: 'Old' } }],
        story_tree: { trees: [{ id: 'main', nodes: [{ id: 'step', visibility: 'world' }] }] },
    });
    assert.deepEqual(script.npcs, [{ id: 'new' }]);
    assert.equal(script.story_tree.trees[0].nodes[0].visibility, undefined);
});
