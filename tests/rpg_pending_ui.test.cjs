const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const panels = fs.readFileSync(path.join(__dirname, '../static/js/modules/panels.js'), 'utf8');
const turn = fs.readFileSync(path.join(__dirname, '../static/js/modules/game-turn.js'), 'utf8');

function setup() {
  const elements = new Map();
  const rendered = [];
  const confirms = [];
  const statuses = [];
  const data = { turnHistory: [] };
  const element = id => {
    if (!elements.has(id)) elements.set(id, { style: {}, classList: { add() {}, remove() {} }, disabled: false, value: '' });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { body: { classList: { add() {} } }, getElementById: element },
    state: { get: key => data[key], set: (key, value) => { data[key] = value; } },
    api: { confirm: async body => { confirms.push(body); return { confirmed: true }; }, getState: async () => ({ turn: 2, locations: [], pending_result: null }) },
    LOC_NAMES: {}, updateHeader() {}, renderAll() {}, updateStatus: message => statuses.push(message), clearDetail() {},
    esc: text => text.replaceAll('<', '&lt;').replaceAll('>', '&gt;'), switchTab() {},
    window: {
      renderInferenceBubbles: outputs => rendered.push(['npc', outputs]),
      showTurnResults: preview => rendered.push(['scene', preview]),
      clearDetail: () => rendered.push(['clear']),
      showDetail: html => rendered.push(['opening', html]),
    },
  });
  vm.runInContext(panels.slice(panels.indexOf('function enterGameScreen('), panels.indexOf('// ===== Window bindings')), context);
  return { context, element, rendered, confirms, statuses, data };
}

test('an RPG opening is shown without creating a pending turn', () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 0, locations: [], opening_text: '开场 <南山>', pending_result: null });
  assert.equal(view.data.pendingResult, null);
  assert.equal(view.element('action-input').disabled, false);
  assert.equal(view.element('action-input').value, '');
  assert.equal(view.element('btn-save').style.display, '');
  assert.ok(view.rendered.some(([type, html]) => type === 'opening' && html.includes('开场 &lt;南山&gt;')));
});

test('loading a pending RPG save restores preview and confirm controls', () => {
  const view = setup();
  const pending = { player_action: '调查南山', npc_outputs: { actor: {} }, scene: { scene_text: '街头传来消息' } };
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: pending });
  assert.equal(view.data.pendingResult, pending);
  assert.equal(view.element('action-input').value, pending.player_action);
  assert.equal(view.element('action-input').disabled, true);
  assert.equal(view.element('btn-submit').disabled, true);
  assert.equal(view.element('btn-confirm').style.display, '');
  assert.equal(view.element('btn-save').style.display, '');
  assert.deepEqual(view.rendered.map(([type]) => type), ['npc', 'scene']);

  view.context.enterGameScreen({ turn: 1, locations: [], pending_result: null });
  assert.equal(view.data.pendingResult, null);
  assert.equal(view.element('btn-confirm').style.display, 'none');
  assert.equal(view.element('btn-submit').disabled, false);
  assert.equal(view.element('action-input').disabled, false);
  assert.equal(view.rendered.at(-1)[0], 'clear');
});

test('RPG refuses a second turn while a preview is pending', async () => {
  const statuses = [];
  const context = vm.createContext({
    state: { get: () => ({ pending: true }) },
    updateStatus: message => statuses.push(message),
    document: { getElementById: () => { throw new Error('should not submit'); } },
  });
  vm.runInContext(turn.slice(turn.indexOf('async function submitAction()'), turn.indexOf('function showTurnResults(')), context);
  await context.submitAction();
  assert.deepEqual(statuses, ['请先确认当前推演']);
});

function setupSubmit() {
  const view = setup();
  view.context.setPhase = () => {};
  view.context.sleep = async () => {};
  view.context.switchTab = () => {};
  view.context.executeUICommand = () => {};
  view.context.showDirectorReply = () => {};
  view.context.renderInferenceBubbles = () => {};
  view.context.showTurnResults = () => {};
  vm.runInContext(turn.slice(turn.indexOf('async function submitAction()'), turn.indexOf('function showTurnResults(')), view.context);
  view.context.enterGameScreen({ turn: 1, locations: [], pending_result: null });
  view.element('action-input').value = '旧行动';
  return view;
}

test('late RPG turn cannot replace a loaded save preview', async () => {
  const view = setupSubmit();
  let resolveTurn;
  view.context.api.submitTurn = () => new Promise(resolve => { resolveTurn = resolve; });
  const submitting = view.context.submitAction();
  const pending = { player_action: '新行动', scene: { scene_text: '新场景' } };
  const nextGame = { turn: 4, locations: [], pending_result: pending };
  view.context.enterGameScreen(nextGame);
  resolveTurn({ pending: true, scene: { scene_text: '旧场景' } });
  await submitting;

  assert.equal(view.data.gameState, nextGame);
  assert.equal(view.data.pendingResult, pending);
  assert.equal(view.element('action-input').value, '新行动');
  assert.equal(view.element('action-input').disabled, true);
  assert.equal(view.element('btn-submit').disabled, true);
});

test('late RPG turn state refresh cannot replace a loaded save', async () => {
  const view = setupSubmit();
  let resolveState;
  view.context.api.submitTurn = async () => ({ pending: true, scene: { scene_text: '旧场景' } });
  view.context.api.getState = () => new Promise(resolve => { resolveState = resolve; });
  const submitting = view.context.submitAction();
  await new Promise(resolve => setImmediate(resolve));
  const nextGame = { turn: 4, locations: [], pending_result: null };
  view.context.enterGameScreen(nextGame);
  resolveState({ turn: 2, locations: [], pending_result: null });
  await submitting;

  assert.equal(view.data.gameState, nextGame);
  assert.equal(view.data.pendingResult, null);
  assert.equal(view.element('action-input').disabled, false);
});

test('late RPG turn failure does not disturb a loaded save', async () => {
  const view = setupSubmit();
  let rejectTurn;
  view.context.api.submitTurn = () => new Promise((resolve, reject) => { rejectTurn = reject; });
  const submitting = view.context.submitAction();
  const pending = { player_action: '新行动', scene: { scene_text: '新场景' } };
  view.context.enterGameScreen({ turn: 4, locations: [], pending_result: pending });
  const statusCount = view.statuses.length;
  rejectTurn(new Error('network'));
  await submitting;

  assert.equal(view.data.pendingResult, pending);
  assert.equal(view.element('action-input').disabled, true);
  assert.equal(view.element('btn-submit').disabled, true);
  assert.equal(view.statuses.length, statusCount);
});

test('confirming a restored RPG preview re-enables the action input', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '前往街头', scene: { scene_text: '街头传来消息' }, outline: {},
  } });
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  await view.context.confirmTurn();

  assert.equal(view.data.pendingResult, null);
  assert.equal(view.data.turnHistory.length, 1);
  assert.equal(view.data.turnHistory[0].player_action, '前往街头');
  assert.equal(view.element('btn-confirm').style.display, 'none');
  assert.equal(view.element('btn-submit').disabled, false);
  assert.equal(view.element('action-input').disabled, false);
  assert.equal(view.element('action-input').value, '');
});

test('choosing an RPG branch sends a choice_id to the confirmation endpoint', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '前往街头', scene: {
      scene_text: '街头传来消息', choices: [{ id: 'choice-a', text: '接触线人' }],
    }, outline: {},
  } });
  view.data.lastChoice = { id: 'choice-a', text: '接触线人' };
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  await view.context.confirmTurnWithChoice('choice-a');

  assert.equal(view.confirms.length, 1);
  assert.equal(view.confirms[0].choice_id, 'choice-a');
  assert.equal(view.data.turnHistory[0].choice.id, 'choice-a');
  assert.equal(view.element('action-input').value, '');
  assert.equal(view.data.lastChoice, null);
});

test('a confirmed RPG branch remains confirmed when state refresh fails', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '前往街头', scene: {
      choices: [{ id: 'choice-a', text: '接触线人' }],
    }, outline: {},
  } });
  view.context.api.confirm = async body => {
    view.confirms.push(body);
    return { confirmed: true, state_after: { turn: 3, location_id: 'street', player_attrs: { money: 8 } } };
  };
  view.context.api.getState = async () => { throw new Error('network'); };
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  await view.context.makeChoice('choice-a');

  assert.equal(view.confirms.length, 1);
  assert.equal(view.data.pendingResult, null);
  assert.equal(view.data.turnHistory.length, 1);
  assert.equal(view.data.turnHistory[0].choice.text, '接触线人');
  assert.equal(view.data.gameState.turn, 3);
  assert.equal(view.data.gameState.location, 'street');
  assert.equal(view.element('action-input').value, '');
  assert.equal(view.element('action-input').disabled, false);
  assert.equal(view.data.lastChoice, null);
  assert.match(view.statuses.at(-1), /已确认.*状态刷新失败/);
  await view.context.confirmTurnWithChoice('choice-a');
  assert.equal(view.confirms.length, 1);
});

test('a confirmed RPG turn with no fallback state waits for a reload after refresh fails', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '前往街头', scene: {}, outline: {},
  } });
  view.context.api.getState = async () => { throw new Error('network'); };
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  await view.context.confirmTurn();

  assert.equal(view.data.pendingResult, null);
  assert.equal(view.data.turnHistory.length, 1);
  assert.equal(view.element('action-input').disabled, true);
  assert.equal(view.element('btn-submit').disabled, true);
  assert.equal(view.element('btn-confirm').style.display, 'none');
  assert.match(view.statuses.at(-1), /已确认.*状态刷新失败/);
});

test('late RPG confirmation cannot clear a newly loaded pending turn', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '旧行动', scene: {}, outline: {},
  } });
  let resolveConfirm;
  view.context.api.confirm = () => new Promise(resolve => { resolveConfirm = resolve; });
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  const confirming = view.context.confirmTurn();
  const nextPending = { turn_num: 4, player_action: '新行动', scene: {}, outline: {} };
  view.context.enterGameScreen({ turn: 4, locations: [], pending_result: nextPending });
  resolveConfirm({ confirmed: true });
  await confirming;

  assert.equal(view.data.pendingResult, nextPending);
  assert.equal(view.data.turnHistory.length, 0);
  assert.equal(view.element('action-input').value, '新行动');
  assert.equal(view.element('btn-confirm').style.display, '');
  assert.equal(view.statuses.at(-1), '确认推演中...');
});

test('late RPG state refresh cannot replace a newly loaded game', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '旧行动', scene: {}, outline: {},
  } });
  let resolveState;
  view.context.api.getState = () => new Promise(resolve => { resolveState = resolve; });
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  const confirming = view.context.confirmTurn();
  await new Promise(resolve => setImmediate(resolve));
  const nextGame = { turn: 4, locations: [], pending_result: null };
  view.context.enterGameScreen(nextGame);
  resolveState({ turn: 2, locations: [], pending_result: null });
  await confirming;

  assert.equal(view.data.gameState, nextGame);
  assert.equal(view.data.turnHistory.length, 1);
  assert.equal(view.element('action-input').value, '');
  assert.equal(view.statuses.at(-1), '确认推演中...');
});

test('RPG only sends one confirmation while a choice or button is pending', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '前往街头', scene: { choices: [{ id: 'choice-a', text: '接触线人' }] }, outline: {},
  } });
  let resolveConfirm;
  const pendingConfirm = new Promise(resolve => { resolveConfirm = resolve; });
  view.context.api.confirm = async body => { view.confirms.push(body); return pendingConfirm; };
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);

  const first = view.context.confirmTurn();
  view.context.makeChoice('choice-a');
  const duplicate = view.context.confirmTurn();
  const requestCount = view.confirms.length;
  resolveConfirm({ confirmed: true });
  await Promise.all([first, duplicate]);
  assert.equal(requestCount, 1);
  assert.equal(view.data.lastChoice, null);
  assert.equal(view.element('btn-confirm').disabled, false);
  assert.equal(view.data.turnHistory.length, 1);
});

test('failed RPG choice confirmation remains retryable', async () => {
  const view = setup();
  view.context.enterGameScreen({ turn: 2, locations: [], pending_result: {
    turn_num: 2, player_action: '前往街头', scene: { choices: [{ id: 'choice-a', text: '接触线人' }] }, outline: {},
  } });
  vm.runInContext(turn.slice(turn.indexOf('let confirmInFlight'), turn.indexOf('async function regenerateScene()')), view.context);
  view.context.api.confirm = async () => { throw new Error('network'); };

  await view.context.confirmTurnWithChoice('choice-a');
  assert.ok(view.data.pendingResult);
  assert.equal(view.element('btn-confirm').disabled, false);
  assert.equal(view.data.turnHistory.length, 0);

  view.context.api.confirm = async () => ({ confirmed: true });
  await view.context.confirmTurnWithChoice('choice-a');
  assert.equal(view.data.pendingResult, null);
  assert.equal(view.data.turnHistory.length, 1);
});
