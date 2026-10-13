const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static/js/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const profileSource = appSource.slice(appSource.indexOf('let _editingProfileId = null;'),
    appSource.indexOf('// --- Presets ---'));
const stages = ['narrative', 'choices', 'state', 'review', 'summary', 'knowledge_graph'];
const overrides = Object.fromEntries(stages.map(stage => [stage, 'synthetic-' + stage]));

function setup(profiles = []) {
    const elements = new Map([...html.matchAll(/<input\b[^>]*\bid="([^"]+)"/g)]
        .map(match => [match[1], { value: '', placeholder: '', scrollIntoView() {} }]));
    elements.set('profile-provider-type', { value: 'openai_compatible' });
    assert.ok(elements.has('profile-sm-review'), 'The settings form must expose review routing');
    const requests = [];
    const statuses = [];
    const context = vm.createContext({
        document: { getElementById: id => elements.get(id), querySelector: () => ({ textContent: '' }) },
        API: {
            get: async () => profiles,
            post: async (url, body) => requests.push({ method: 'POST', url, body: JSON.parse(JSON.stringify(body)) }),
            put: async (url, body) => requests.push({ method: 'PUT', url, body: JSON.parse(JSON.stringify(body)) }),
        },
        showConfigStatus: (message, success) => statuses.push({ message, success }),
        loadProfiles() {}, loadProfilesDropdown() {},
        alert: message => assert.fail(message),
    });
    vm.runInContext(profileSource, context);
    elements.get('profile-name').value = 'Synthetic profile';
    elements.get('profile-model').value = 'synthetic-default';
    elements.get('profile-max-tokens').value = '8192';
    return { context, elements, requests, statuses };
}

function profile(id, stageModels = overrides) {
    return { id, name: 'Synthetic profile', model: 'synthetic-default', max_tokens: 8192,
        has_api_key: true, stage_models: stageModels };
}

test('creating a profile saves review and all existing stages, then resets the form', async () => {
    const view = setup();
    for (const stage of stages) view.elements.get('profile-sm-' + stage).value = '  ' + overrides[stage] + '  ';
    view.elements.get('profile-api-key').value = 'synthetic-credential';
    await view.context.saveNewProfile();
    assert.equal(view.requests.length, 1);
    assert.equal(view.requests[0].method, 'POST');
    assert.equal(view.requests[0].url, '/api/config/profiles');
    assert.deepEqual(view.requests[0].body.stage_models, overrides);
    assert.equal(view.requests[0].body.api_key, 'synthetic-credential');
    assert.equal(view.requests[0].body.max_tokens, 8192);
    for (const stage of stages) assert.equal(view.elements.get('profile-sm-' + stage).value, '');
    assert.equal(view.statuses[0].success, true);
});

for (const apiKey of ['', 'synthetic-replacement-credential']) {
    test(`editing review preserves other stages and ${apiKey ? 'updates an entered' : 'omits an unchanged'} key`, async () => {
        const view = setup([profile(7)]);
        await view.context.editProfile(7);
        for (const stage of stages) assert.equal(view.elements.get('profile-sm-' + stage).value, overrides[stage]);
        assert.equal(view.elements.get('profile-api-key').value, '');
        view.elements.get('profile-sm-review').value = '  synthetic-review-updated  ';
        view.elements.get('profile-api-key').value = apiKey;
        await view.context.saveNewProfile();
        assert.equal(view.requests.length, 1);
        assert.equal(view.requests[0].method, 'PUT');
        assert.equal(view.requests[0].url, '/api/config/profiles/7');
        assert.deepEqual(view.requests[0].body.stage_models, { ...overrides, review: 'synthetic-review-updated' });
        assert.equal(Object.hasOwn(view.requests[0].body, 'api_key'), Boolean(apiKey));
        if (apiKey) assert.equal(view.requests[0].body.api_key, apiKey);
    });
}

test('clearing only review preserves the other overrides', async () => {
    const view = setup([profile(7)]);
    await view.context.editProfile(7);
    view.elements.get('profile-sm-review').value = '   ';
    await view.context.saveNewProfile();
    const expected = { ...overrides };
    delete expected.review;
    assert.deepEqual(view.requests[0].body.stage_models, expected);
});

test('clearing every override sends an empty object on update', async () => {
    const view = setup([profile(7)]);
    await view.context.editProfile(7);
    for (const stage of stages) view.elements.get('profile-sm-' + stage).value = '';
    await view.context.saveNewProfile();
    assert.deepEqual(view.requests[0].body.stage_models, {});
    assert.equal(Object.hasOwn(view.requests[0].body, 'api_key'), false);
});

test('switching to a legacy profile cannot inherit review from the previous form', async () => {
    const view = setup([profile(7), profile(8, { state: 'legacy-state' })]);
    await view.context.editProfile(7);
    await view.context.editProfile(8);
    assert.equal(view.elements.get('profile-sm-review').value, '');
    await view.context.saveNewProfile();
    assert.equal(view.requests[0].url, '/api/config/profiles/8');
    assert.deepEqual(view.requests[0].body.stage_models, { state: 'legacy-state' });
});

test('reset cancels editing and a blank new profile has no overrides', async () => {
    const view = setup([profile(7)]);
    await view.context.editProfile(7);
    view.context._resetProfileForm();
    for (const stage of stages) assert.equal(view.elements.get('profile-sm-' + stage).value, '');
    view.elements.get('profile-name').value = 'Another synthetic profile';
    await view.context.saveNewProfile();
    assert.equal(view.requests[0].method, 'POST');
    assert.equal(view.requests[0].body.stage_models, null);
});
