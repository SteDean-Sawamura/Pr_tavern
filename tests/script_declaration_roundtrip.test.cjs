const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/script_builder.js'), 'utf8');
const builder = source.slice(source.indexOf('function _preserveScriptVisibilityMetadata('),
    source.indexOf('function toPinyin('));
const saver = source.slice(source.indexOf('async function saveWizardScript('),
    source.indexOf('function _highlightValidationErrors('));
const graphSource = fs.readFileSync(path.join(__dirname, '../static/js/story_tree_editor.js'), 'utf8');
const graphGetter = graphSource.slice(graphSource.indexOf('function getEventsFromEditor('),
    graphSource.indexOf('// ---- global node map ----'));
const clone = value => JSON.parse(JSON.stringify(value));

function declaration() {
    return {
        script_id: 'synthetic-form-declarations', script_name: 'Synthetic form declarations',
        start_time: '2030-01-01T09:00:00',
        variables: [{ id: 'test_ready', type: 'number', default: 0 }],
        one_time_events: [{
            id: 'test_once', name: 'Synthetic notice', description: 'A synthetic indicator changes.',
            trigger_time: '2030-01-01T09:02:00', condition: 'test_ready >= 0',
            effects: [{ type: 'state_change', target: 'script_variables.test_ready', value: 2 }],
            narrative_claims: ['The synthetic indicator has changed.'], rule_notes: 'SYNTHETIC_EVENT_RULE',
        }],
        cyclic_events: [{
            id: 'test_cycle', name: 'Synthetic cycle', description: 'A synthetic reminder repeats.',
            first_trigger: '2030-01-01T09:30:00', frequency_value: 1, frequency_unit: 'hour',
            effects: [{ type: 'state_change', target: 'script_variables.test_ready', value: 3 }],
            narrative_claims: ['The synthetic reminder has repeated.'], rule_notes: 'SYNTHETIC_CYCLE_RULE',
        }],
        npcs: [{ id: 'test_reader', name: 'Synthetic reader', goals: [{
            id: 'test_goal', description: 'Observe the indicator.', condition_met: 'test_ready >= 2',
            rule_notes: 'SYNTHETIC_NPC_RULE',
        }] }],
        organizations: [{ id: 'test_circle', name: 'Synthetic circle', goals: [{
            id: 'test_goal', description: 'Record the indicator.', condition_met: 'test_ready >= 2',
            rule_notes: 'SYNTHETIC_ORG_RULE',
            on_complete_effects: { set_reputation: [{ faction: 'test_circle', value: 72 }] },
        }] }],
    };
}

function card(prefix, values, children = {}) {
    return {
        querySelector: selector => ({ value: String(values[selector.replace(`.w-${prefix}-`, '')] ?? ''), checked: true }),
        querySelectorAll: selector => children[selector] || [],
    };
}

function goals(actor) {
    return (actor.goals || []).map(goal => card('goal', {
        id: goal.id, desc: goal.description, condition: goal.condition_met,
        type: goal.type, priority: goal.priority,
    }));
}

async function savedPayload(original = declaration(), { form = clone(original), graph = false, onSave } = {}) {
    const lists = {
        '#w-npcs .npc-editor': (form.npcs || []).map(actor => card('npc', actor, { '.npc-goal-row': goals(actor) })),
        '#w-organizations .org-editor': (form.organizations || []).map(actor => card('org', actor, {
            '.org-goals-rows .npc-goal-row': goals(actor),
        })),
        '#w-onetime-events .ote-editor': (form.one_time_events || []).map(event => card('ote', {
            id: event.id, name: event.name, desc: event.description, time: event.trigger_time,
            condition: event.condition, visibility: event.visibility || 'public',
        })),
        '#w-cyclic-events .ce-editor': (form.cyclic_events || []).map(event => card('ce', {
            id: event.id, name: event.name, desc: event.description, first: event.first_trigger,
            'freq-val': event.frequency_value, 'freq-unit': event.frequency_unit,
            condition: event.condition, visibility: event.visibility || 'public',
        })),
        '#w-variables .var-editor': (form.variables || []).map(variable => card('var', variable)),
    };
    const values = {
        'w-script-id': form.script_id, 'w-script-name': form.script_name, 'w-start-time': form.start_time,
        'w-lore-max-recursion': '2', 'w-reasoning-lookback': '2',
    };
    const requests = [];
    const context = vm.createContext({
        _rawScriptOverlay: original, _waitAllTabsLoaded: async () => {},
        _NpcRelGraph: { toRelationships: () => [] }, toPinyin: value => value,
        stIsGraphMode: () => graph,
        _stEditorEvents: [
            ...(form.one_time_events || []).map(event => ({ ...clone(event), _kind: 'one_time' })),
            ...(form.cyclic_events || []).map(event => ({ ...clone(event), _kind: 'cyclic' })),
        ],
        document: {
            getElementById: id => ({ value: values[id] || '', checked: true }),
            querySelectorAll: selector => lists[selector] || [],
        },
        alert: () => {}, editingScriptId: null,
        API: { post: async (url, body) => {
            onSave?.(body);
            requests.push({ url, body: clone(body) });
            return { status: 'ok' };
        } },
    });
    vm.runInContext(graphGetter + builder + saver, context);
    await vm.runInContext('saveWizardScript()', context);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/scripts');
    assert.equal(context.editingScriptId, form.script_id);
    return requests[0].body;
}

if (process.argv.includes('--emit-script')) {
    savedPayload(declaration(), { graph: process.argv.includes('--graph') }).then(payload => {
        process.stdout.write(JSON.stringify(payload));
    }).catch(error => { console.error(error); process.exitCode = 1; });
} else {
    for (const graph of [false, true]) {
        test(`saved ${graph ? 'graph' : 'form'} payload preserves timed event declarations`, async () => {
            const original = declaration();
            const result = (await savedPayload(original, { graph })).content;
            for (const section of ['one_time_events', 'cyclic_events']) {
                for (const field of ['effects', 'narrative_claims', 'rule_notes']) {
                    assert.deepEqual(result[section][0][field], original[section][0][field]);
                }
            }
        });

        test(`saved ${graph ? 'graph' : 'form'} payload preserves nested goal rules and rewards`, async () => {
            const original = declaration();
            const result = (await savedPayload(original, { graph })).content;
            assert.equal(result.npcs[0].goals[0].rule_notes, 'SYNTHETIC_NPC_RULE');
            assert.equal(result.organizations[0].goals[0].rule_notes, 'SYNTHETIC_ORG_RULE');
            assert.deepEqual(result.organizations[0].goals[0].on_complete_effects,
                original.organizations[0].goals[0].on_complete_effects);
        });
    }

    test('edited fields and removed events or goals are not restored from the overlay', async () => {
        const original = declaration();
        const form = clone(original);
        form.one_time_events[0].description = 'Edited synthetic description.';
        form.one_time_events[0].condition = '';
        form.npcs[0].goals[0].condition_met = '';
        form.cyclic_events = [];
        form.organizations[0].goals = [];
        const result = (await savedPayload(original, { form })).content;
        assert.equal(result.one_time_events[0].description, form.one_time_events[0].description);
        assert.equal(result.one_time_events[0].condition, undefined);
        assert.equal(result.npcs[0].goals[0].condition_met, undefined);
        assert.deepEqual(result.cyclic_events, []);
        assert.equal(result.organizations[0].goals, undefined);
    });

    test('goal metadata matches both actor and goal identities without reviving removed actors', async () => {
        const original = declaration();
        original.organizations.push({ id: 'test_other', name: 'Synthetic other circle', goals: [{
            id: 'test_goal', description: 'A separate synthetic goal.', rule_notes: 'SYNTHETIC_OTHER_RULE',
            on_complete_effects: { unlock_locations: ['test_room'] },
        }] });
        const form = clone(original);
        form.organizations.reverse();
        form.npcs = [{ id: 'test_new', name: 'Synthetic new reader', goals: [{
            id: 'test_goal', description: 'A new goal with an existing local ID.',
        }] }];
        form.organizations[1].goals.push({ id: 'test_new_goal', description: 'A newly added synthetic goal.' });
        const result = (await savedPayload(original, { form })).content;
        assert.equal(result.organizations[0].goals[0].rule_notes, 'SYNTHETIC_OTHER_RULE');
        assert.deepEqual(result.organizations[0].goals[0].on_complete_effects, { unlock_locations: ['test_room'] });
        assert.equal(result.organizations[1].goals[0].rule_notes, 'SYNTHETIC_ORG_RULE');
        assert.equal(result.organizations[1].goals[1].rule_notes, undefined);
        assert.equal(result.organizations[1].goals[1].on_complete_effects, undefined);
        assert.equal(result.npcs[0].goals[0].rule_notes, undefined);
        assert.equal(result.npcs.length, 1);
    });

    test('goals without stable actor and goal IDs do not inherit hidden declarations', async () => {
        for (const missing of ['actor', 'goal']) {
            const original = declaration();
            delete (missing === 'actor' ? original.organizations[0] : original.organizations[0].goals[0]).id;
            const result = (await savedPayload(original)).content;
            assert.equal(result.organizations[0].goals[0].rule_notes, undefined);
            assert.equal(result.organizations[0].goals[0].on_complete_effects, undefined);
        }
    });

    test('dictionary-shaped event effects are preserved without conversion', async () => {
        const original = declaration();
        original.cyclic_events[0].effects = { set_var: [{ target: 'script_variables.test_ready', op: 'set', value: 3 }] };
        for (const graph of [false, true]) {
            const result = (await savedPayload(original, { graph })).content;
            assert.deepEqual(result.cyclic_events[0].effects, original.cyclic_events[0].effects);
        }
    });

    test('explicit empty declarations survive JSON-to-form and graph saves', async () => {
        const original = declaration();
        original.one_time_events[0].effects = [];
        original.one_time_events[0].narrative_claims = [];
        original.organizations[0].goals[0].on_complete_effects = {};
        const result = (await savedPayload(original)).content;
        assert.deepEqual(result.one_time_events[0].effects, []);
        assert.deepEqual(result.one_time_events[0].narrative_claims, []);
        assert.deepEqual(result.organizations[0].goals[0].on_complete_effects, {});
        const old = declaration();
        const graphResult = (await savedPayload(old, { form: original, graph: true })).content;
        assert.deepEqual(graphResult.one_time_events[0].effects, []);
        assert.deepEqual(graphResult.one_time_events[0].narrative_claims, []);
    });

    test('restored declarations are deep copies and repeated saves leave the source unchanged', async () => {
        const original = declaration();
        const before = clone(original);
        await savedPayload(original, { onSave: payload => {
            payload.content.one_time_events[0].effects[0].value = 99;
            payload.content.organizations[0].goals[0].on_complete_effects.set_reputation[0].value = 99;
        } });
        const second = (await savedPayload(original)).content;
        assert.equal(second.one_time_events[0].effects[0].value, 2);
        assert.equal(second.organizations[0].goals[0].on_complete_effects.set_reputation[0].value, 72);
        assert.deepEqual(original, before);
    });
}
