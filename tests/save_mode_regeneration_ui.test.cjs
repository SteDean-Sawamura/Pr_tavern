const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const pipelineMode = fs.readFileSync(path.join(__dirname, '../static/js/pipeline_mode.js'), 'utf8');

test('loaded save mode selects the regeneration endpoint after switching saves', async () => {
    const requests = [];
    const streamed = [];
    const saves = { unified: 'agentic_unified', classic: 'workflow' };
    const element = () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} }, remove() {} });
    const elements = new Map([
        ...['game-play-screen', 'game-start-screen', 'status-panel', 'status-panel-toggle',
            'freeform-input', 'narrative-content', 'main-area', 'stream-toggle', 'pipeline-mode-btn',
            'status-attributes', 'status-location', 'status-npcs', 'status-persistent',
            'btn-regen-choices'].map(id => [id, element()]),
    ]);
    elements.get('game-play-screen').style.display = 'none';
    const context = vm.createContext({
        window: {},
        document: {
            getElementById: id => elements.get(id) || null,
            querySelectorAll: () => [],
        },
        localStorage: { getItem: () => null },
        API: {
            get: async url => {
                const saveId = url.split('/')[3];
                return { state: { _pipeline_mode: saves[saveId] }, node_id: `${saveId}-node`, history: { entries: [], total: 0 } };
            },
            post: async url => { requests.push(url); return { choices: [] }; },
        },
        submitActionStream: async (action, url) => { streamed.push({ action, url }); },
        _showGameOverlay() {}, _syncTurnOverflow() {}, renderLogitBiasList() {}, updateHeader() {}, updateSwipeControls() {},
        updateTurnIndicator() {}, _renderLocationMap() {}, _updateRunningStats() {},
        updateSummaryFreezeIndicator() {}, _showContextualTips() {}, _loadNpcSchedules() {},
        renderChoices() {}, showStateChanges() {}, loadSidebarSaves() {}, switchPanel() {},
        console, alert: message => { throw new Error(message); },
    });
    vm.runInContext(
        app.slice(app.indexOf('// Global state'), app.indexOf('// --- Panel switching ---')) +
        'let _npcChatId = null; let _npcChatRequest = null;\n' +
        app.slice(app.indexOf('function cancelActiveGameStream() {'), app.indexOf('async function continueGame(saveId) {')) +
        app.slice(app.indexOf('async function continueGame(saveId) {'), app.indexOf('async function deleteSave(')) +
        app.slice(app.indexOf('function showGameScreen(data,'), app.indexOf('function _appendHistoryDetails(')) +
        app.slice(app.indexOf('function renderPcEventReports(state)'), app.indexOf('//  CLUE BOARD')) +
        app.slice(app.indexOf('let useStreamMode = false;'), app.indexOf('async function testActiveProfile()')) +
        app.slice(app.indexOf('async function regenerateStage(stage) {'), app.indexOf('async function continueNarrative() {')) +
        pipelineMode,
        context,
    );

    assert.equal(await context.continueGame('unified'), true);
    assert.equal(vm.runInContext('_shouldUseStream()', context), true);
    await context.regenerateStage('choices');
    assert.equal(streamed[0].url, '/api/game/unified/regenerate/stream?stage=choices');
    assert.equal(requests.length, 0);

    assert.equal(await context.continueGame('classic'), true);
    assert.equal(vm.runInContext('_shouldUseStream()', context), false);
    await context.regenerateStage('choices');
    assert.deepEqual(requests, ['/api/game/classic/regenerate?stage=choices']);
    assert.equal(streamed.length, 1);
});
