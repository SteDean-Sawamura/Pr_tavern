const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/game_ui.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup(post, questionCount = 0) {
    const requests = [];
    const actions = { innerHTML: '', appendChild(element) { this.error = element; } };
    const input = {
        value: '', listeners: {},
        addEventListener(event, listener) { this.listeners[event] = listener; },
        keydown(event) { this.listeners.keydown(event); },
    };
    const createButton = (textContent, group = null) => {
        const classes = new Set();
        const listeners = {};
        return {
            textContent, disabled: false,
            classList: {
                add: name => classes.add(name),
                remove: name => classes.delete(name),
                contains: name => classes.has(name),
            },
            closest: () => group,
            addEventListener: (event, listener) => { listeners[event] = listener; },
            click() { if (!this.disabled) listeners.click(); },
        };
    };
    const groups = Array.from({ length: questionCount }, (_, index) => ({
        question: `问题${index + 1}`,
        querySelector(selector) {
            if (selector === '.pq-text') return { textContent: this.question };
            if (selector === '.pq-option.selected') return this.options.find(button => button.classList.contains('selected'));
            return null;
        },
        querySelectorAll(selector) { return selector === '.pq-option' ? this.options : []; },
    }));
    for (const group of groups) group.options = [createButton('Left', group), createButton('Right', group)];
    const options = groups.flatMap(group => group.options);
    const confirm = createButton('Confirm', actions);
    const edit = createButton('Send');
    const card = {
        querySelector(selector) {
            return {
                '.plan-actions': actions, '.plan-confirm-btn': confirm,
                '.plan-edit-btn': edit, '.plan-edit-input': input,
            }[selector];
        },
        querySelectorAll(selector) {
            if (selector === '.pq-option') return options;
            if (selector === '.plan-question') return groups;
            if (selector === '.pq-option.selected') return options.filter(button => button.classList.contains('selected'));
            if (selector === 'button') return [...options, confirm, edit];
            return [];
        },
    };
    const toasts = [];
    const context = vm.createContext({
        document: {
            addEventListener() {},
            createElement: () => ({ hidden: false, setAttribute() {}, remove() {} }),
            body: { appendChild: toast => toasts.push(toast) },
        },
        setTimeout() {},
        API: { post: (url, body) => {
            requests.push({ url, body });
            return post(url, body);
        } },
        escapeHtml: value => value.replace(/&/g, '&amp;').replace(/</g, '&lt;'),
    });
    vm.runInContext(source, context);
    context.wireAgentPlanCard(card, 'first', 'round-1', 'plan-1');
    return { context, card, actions, input, groups, options, confirm, edit, requests, toasts };
}

test('question choices remain editable until explicit submission and retry', async () => {
    let attempt = 0;
    const view = setup(async () => {
        if (++attempt === 1) throw new Error('HTTP 401');
        return { ok: true };
    }, 2);
    view.groups[0].options[0].click();
    assert.equal(view.requests.length, 0);
    view.groups[1].options[1].click();
    assert.equal(view.requests.length, 0);
    assert.equal(view.confirm.disabled, false);
    view.groups[0].options[1].click();
    assert.equal(view.groups[0].options[1].classList.contains('selected'), true);
    view.confirm.click();
    await flush();
    assert.equal(view.requests.length, 1);
    assert.equal(view.requests[0].url, '/api/game/first/inject');
    assert.deepEqual(JSON.parse(JSON.stringify(view.requests[0].body)), { message: '1. 问题1：Right\n2. 问题2：Right', stream_id: 'round-1', plan_id: 'plan-1' });
    assert.match(view.actions.error.textContent, /HTTP 401/);
    assert.equal(view.actions.error.hidden, false);
    assert.equal(view.actions.innerHTML, '');
    assert.ok(view.options.every(button => !button.disabled));
    assert.equal(view.groups[0].options[1].classList.contains('selected'), true);

    view.groups[0].options[0].click();
    view.confirm.click();
    await flush();
    assert.equal(view.requests.length, 2);
    assert.equal(view.requests[1].body.message, '1. 问题1：Left\n2. 问题2：Right');
    assert.match(view.actions.innerHTML, /已提交选择/);
    assert.ok(view.options.every(button => button.disabled));
});

test('incomplete questions cannot be submitted but freeform remains available', async () => {
    const view = setup(async () => ({ ok: true }), 2);
    assert.equal(view.confirm.disabled, true);
    view.groups[0].options[0].click();
    view.confirm.click();
    assert.equal(view.requests.length, 0);
    view.input.value = '先观察局势';
    view.edit.click();
    await flush();
    assert.equal(view.requests[0].body.message, '先观察局势');
});

test('plan freeform reply submits on Enter without submitting during composition', async () => {
    const view = setup(async () => ({ ok: true }), 1);
    view.input.value = '正在输入';
    view.input.keydown({ key: 'Enter', isComposing: true, preventDefault() {} });
    assert.equal(view.requests.length, 0);

    let prevented = false;
    view.input.value = '  先核对档案  ';
    view.input.keydown({ key: 'Enter', isComposing: false, preventDefault() { prevented = true; } });
    await flush();
    assert.equal(prevented, true);
    assert.equal(view.requests.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(view.requests[0].body)), {
        message: '先核对档案', stream_id: 'round-1', plan_id: 'plan-1',
    });
    view.edit.click();
    assert.equal(view.requests.length, 1);
});

test('an ended stream expires unanswered plan controls', () => {
    const view = setup(async () => ({ ok: true }), 2);
    view.groups[0].options[0].click();
    view.groups[1].options[1].click();
    view.confirm.closest = () => view.actions;
    view.context.closePendingAgentPlan(view.card);
    assert.ok(view.options.every(button => button.disabled));
    assert.equal(view.edit.disabled, true);
    assert.equal(view.input.disabled, true);
    assert.match(view.actions.textContent, /计划已失效/);
    view.confirm.click();
    assert.equal(view.requests.length, 0);
});

test('a late successful reply cannot revive a plan after the stream closes', async () => {
    let finish;
    const view = setup(() => new Promise(resolve => { finish = resolve; }));
    view.confirm.click();
    view.context.closePendingAgentPlan(view.card);
    finish({ ok: true });
    await flush();
    assert.match(view.actions.textContent, /计划已失效/);
    assert.equal(view.card.pendingReply, undefined);
    assert.equal(view.confirm.disabled, true);
    assert.equal(view.edit.disabled, true);
    assert.equal(view.input.disabled, true);
    view.confirm.click();
    assert.equal(view.requests.length, 1);
});

test('a late failed reply cannot re-enable an expired plan', async () => {
    let fail;
    const view = setup(() => new Promise((_, reject) => { fail = reject; }), 1);
    view.groups[0].options[0].click();
    view.confirm.click();
    view.context.closePendingAgentPlan(view.card);
    fail(new Error('Network unavailable'));
    await flush();
    assert.match(view.actions.textContent, /计划已失效/);
    assert.equal(view.actions.error.hidden, true);
    assert.ok(view.options.every(button => button.disabled));
    assert.equal(view.input.disabled, true);
    view.confirm.click();
    assert.equal(view.requests.length, 1);
});

test('auto-confirming a plan disables unanswered controls immediately', () => {
    const view = setup(async () => ({ ok: true }));
    view.context.closePendingAgentPlan(view.card, '已自动确认');
    assert.equal(view.actions.textContent, '已自动确认');
    assert.equal(view.confirm.disabled, true);
    assert.equal(view.edit.disabled, true);
    assert.equal(view.input.disabled, true);
    view.edit.click();
    assert.equal(view.requests.length, 0);
});

test('a rejected stale plan closes controls rather than offering a retry', async () => {
    const view = setup(async () => { throw Object.assign(new Error('计划已失效'), { status: 409 }); }, 1);
    view.groups[0].options[0].click();
    view.confirm.click();
    await flush();
    assert.match(view.actions.textContent, /计划已失效/);
    assert.equal(view.input.disabled, true);
    assert.ok(view.options.every(button => button.disabled));
    view.confirm.click();
    assert.equal(view.requests.length, 1);
});

test('failed confirmation remains retryable without optimistic success', async () => {
    const view = setup(async () => { throw new Error('Network unavailable'); });
    view.confirm.click();
    await flush();
    assert.equal(view.actions.innerHTML, '');
    assert.match(view.actions.error.textContent, /Network unavailable/);
    assert.equal(view.confirm.disabled, false);
    view.confirm.click();
    await flush();
    assert.equal(view.requests.length, 2);
});

test('freeform message stays editable after a failed request', async () => {
    const view = setup(async () => { throw new Error('HTTP 503'); });
    view.input.value = '  Wait here  ';
    view.edit.click();
    await flush();
    assert.equal(view.requests[0].body.message, 'Wait here');
    assert.equal(view.input.value, '  Wait here  ');
    assert.equal(view.edit.disabled, false);
    assert.match(view.actions.error.textContent, /HTTP 503/);
});

test('pending confirmation blocks duplicate requests', async () => {
    let finish;
    const view = setup(() => new Promise(resolve => { finish = resolve; }));
    view.confirm.click();
    view.confirm.click();
    assert.equal(view.requests.length, 1);
    assert.equal(view.confirm.disabled, true);
    finish({ ok: true });
    await flush();
    assert.match(view.actions.innerHTML, /已排队，等待 Agent 接收/);
    view.confirm.click();
    assert.equal(view.requests.length, 1);
});

test('side interjection retains its text and reports a failed request', async () => {
    const view = setup(async () => { throw new Error('HTTP 401'); });
    const input = { value: '  Hello  ' };
    const button = { disabled: false };
    await view.context.sendAgentInterjection('first', input, button, 'round-1');
    assert.equal(input.value, '  Hello  ');
    assert.equal(button.disabled, false);
    assert.equal(view.requests[0].body.message, 'Hello');
    assert.equal(view.requests[0].body.stream_id, 'round-1');
    assert.match(view.toasts[0].textContent, /HTTP 401/);
});

test('side interjection clears the input only after success', async () => {
    let finish;
    const view = setup(() => new Promise(resolve => { finish = resolve; }));
    const input = { value: '  Hello  ' };
    const button = { disabled: false };
    const queued = [];
    const sending = view.context.sendAgentInterjection('first', input, button, 'round-1', message => queued.push(message));
    assert.equal(button.disabled, true);
    assert.equal(input.value, '  Hello  ');
    finish({ ok: true });
    await sending;
    assert.equal(button.disabled, false);
    assert.equal(input.value, '');
    assert.deepEqual(queued, ['Hello']);
    assert.equal(view.toasts.length, 0);
});

test('side interjection preserves a new draft typed while the previous message is sending', async () => {
    let finish;
    const view = setup(() => new Promise(resolve => { finish = resolve; }));
    const input = { value: 'First message' };
    const button = { disabled: false };
    const sending = view.context.sendAgentInterjection('first', input, button, 'round-1');
    input.value = 'Second message';
    finish({ ok: true });
    await sending;
    assert.equal(input.value, 'Second message');
    assert.equal(button.disabled, false);
    assert.equal(view.requests[0].body.message, 'First message');
});
