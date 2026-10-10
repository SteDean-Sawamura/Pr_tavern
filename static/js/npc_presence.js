globalThis.NpcPresence = (() => {
    const requests = new WeakMap();

    function getSchedules(saveId, state) {
        const cached = requests.get(state);
        const gameTime = state.game_time || '';
        if (cached?.saveId === saveId && cached.gameTime === gameTime) return cached.promise;
        const promise = API.get(`/api/game/${encodeURIComponent(saveId)}/npc-schedules`).then(schedules => {
            const entry = requests.get(state);
            if (entry?.promise === promise) entry.schedules = schedules;
            return schedules;
        }).catch(error => {
            if (requests.get(state)?.promise === promise) requests.delete(state);
            throw error;
        });
        requests.set(state, { saveId, gameTime, promise });
        return promise;
    }

    function getCachedSchedules(saveId, state) {
        const cached = requests.get(state);
        return cached?.saveId === saveId && cached.gameTime === (state.game_time || '') ? cached.schedules : null;
    }

    function talkBlockReason(npcId, state, schedules) {
        const npc = state.npcs?.[npcId];
        if (npc == null || (typeof npc === 'object' && npc.known === false)) return '尚未认识';
        if (typeof npc === 'object' && npc.met === false) return '未接触';
        if ((state.companions || []).includes(npcId)) return '';
        if (schedules && !schedules[npcId]) return '不在当前位置';
        const location = (schedules?.[npcId]?.location || (typeof npc === 'object' &&
            (npc.current_location || npc.default_location)) || '').toLowerCase();
        const playerLocation = (state.player?.location || '').toLowerCase();
        if (!schedules && !location) return '位置待确认';
        return location && location !== playerLocation ? '不在当前位置' : '';
    }

    function hasCurrentContact(npcId, state) {
        return state.npc_contacts?.[npcId] === true;
    }

    return { getSchedules, getCachedSchedules, talkBlockReason, hasCurrentContact };
})();
