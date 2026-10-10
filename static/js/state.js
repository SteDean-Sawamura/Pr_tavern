const _state = {
  gameState: null,
  pendingResult: null,
  selectedEntity: null,   // {type:'npc'|'location', id:'xxx'}
  retryNpcId: null,
  turnHistory: [],
  scriptList: [],
  currentScriptDetail: null,
  selectedPresetId: null,
  lastChoice: null,
  openingText: '',
  createStarted: false,
};

const _listeners = new Map();

export function get(key) { return _state[key]; }

export function set(key, value) {
  const old = _state[key];
  _state[key] = value;
  if (old !== value) {
    const fns = _listeners.get(key);
    if (fns) fns.forEach(fn => fn(value, old));
  }
}

export function on(key, fn) {
  if (!_listeners.has(key)) _listeners.set(key, new Set());
  _listeners.get(key).add(fn);
}

export function off(key, fn) {
  const fns = _listeners.get(key);
  if (fns) fns.delete(fn);
}

// batch update without triggering per-key
export function merge(updates) {
  for (const [k, v] of Object.entries(updates)) {
    set(k, v);
  }
}
