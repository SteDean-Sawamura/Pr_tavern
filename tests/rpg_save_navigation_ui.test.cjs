const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/modules/start-screen.js'), 'utf8');
const loadSaveSource = source.slice(source.indexOf('let loadSaveSequence'), source.indexOf('async function deleteSave('));

function setup() {
  const loads = [];
  const entered = [];
  const alerts = [];
  const data = {};
  let activeSave;
  let selectedProfile = '';
  const context = vm.createContext({
    api: {
      loadSave: (id, aiProfileId) => new Promise((resolve, reject) => loads.push({ id, aiProfileId, resolve: value => {
        activeSave = id;
        resolve(value);
      }, reject })),
      getState: async () => ({ saveId: activeSave }),
      startGame: async () => { throw new Error('load must not start a new game'); },
    },
    state: { set: (key, value) => { data[key] = value; } },
    document: { getElementById: () => ({ value: selectedProfile }) },
    _enterGameScreen: value => entered.push(value),
    alert: message => alerts.push(message),
  });
  vm.runInContext(loadSaveSource, context);
  return { context, loads, entered, alerts, data, selectProfile: value => { selectedProfile = value; } };
}

test('RPG sends the selected AI profile with a save load', async () => {
  const view = setup();
  view.selectProfile('7');
  const loading = view.context.loadSave(1);
  await Promise.resolve();
  assert.equal(view.loads[0].aiProfileId, 7);
  view.loads[0].resolve({ ok: true });
  await loading;
  assert.equal(view.entered[0].saveId, 1);
});

test('RPG serializes save loads and displays the latest selection', async () => {
  const view = setup();
  const first = view.context.loadSave(1);
  await Promise.resolve();
  const second = view.context.loadSave(2);
  assert.deepEqual(view.loads.map(load => load.id), [1]);

  view.loads[0].resolve({ ok: true, turn_history: [{ turn_num: 1 }] });
  await first;
  assert.deepEqual(view.loads.map(load => load.id), [1, 2]);
  assert.equal(view.entered.length, 0);

  view.loads[1].resolve({ ok: true, turn_history: [{ turn_num: 2 }] });
  await second;
  assert.equal(view.entered.length, 1);
  assert.equal(view.entered[0].saveId, 2);
  assert.equal(view.data.turnHistory[0].turn_num, 2);
});

test('an obsolete RPG load failure does not alert over a newer selection', async () => {
  const view = setup();
  const first = view.context.loadSave(1);
  await Promise.resolve();
  const second = view.context.loadSave(2);
  view.loads[0].reject(new Error('network'));
  await first;
  view.loads[1].resolve({ ok: true });
  await second;

  assert.deepEqual(view.alerts, []);
  assert.equal(view.entered[0].saveId, 2);
  assert.equal(view.data.turnHistory.length, 0);
});
