/* 酒馆 - 游戏界面辅助函数 */

function wireAgentPlanCard(planCard, saveId, streamId, planId) {
    planCard.planId = planId;
    const actions = planCard.querySelector('.plan-actions');
    const options = planCard.querySelectorAll('.pq-option');
    const questions = planCard.querySelectorAll('.plan-question').length;
    const confirmButton = planCard.querySelector('.plan-confirm-btn');
    const selectionComplete = () => planCard.querySelectorAll('.pq-option.selected').length === questions;
    confirmButton.disabled = questions > 0;
    const error = document.createElement('span');
    error.className = 'plan-error';
    error.setAttribute('role', 'alert');
    error.hidden = true;
    actions.appendChild(error);
    let pending = false;
    let submitted = false;

    const submitReply = async (message, label) => {
        if (pending || submitted || planCard.expired) return;
        pending = true;
        planCard.pendingReply = message;
        const buttons = planCard.querySelectorAll('button');
        buttons.forEach(button => { button.disabled = true; });
        error.hidden = true;
        try {
            await API.post(`/api/game/${saveId}/inject`, { message, stream_id: streamId, plan_id: planId });
            if (planCard.expired) return;
            submitted = true;
            actions.innerHTML = '<span class="plan-result">' + escapeHtml(label) +
                (planCard.replyReceived ? ' · 已被 Agent 接收' : ' · 已排队，等待 Agent 接收') + '</span>';
        } catch (err) {
            if (!planCard.expired) {
                if (planCard.replyReceived) {
                    submitted = true;
                    actions.innerHTML = '<span class="plan-result">' + escapeHtml(label) + ' · 已被 Agent 接收</span>';
                } else if (err.status === 409) {
                    closePendingAgentPlan(planCard, err.message);
                } else {
                    planCard.pendingReply = null;
                    error.textContent = '发送失败：' + err.message + '，请重试';
                    error.hidden = false;
                }
            }
        } finally {
            pending = false;
            if (!submitted && !planCard.expired) {
                buttons.forEach(button => { button.disabled = false; });
                confirmButton.disabled = questions > 0 && !selectionComplete();
            }
        }
    };

    options.forEach(button => {
        button.addEventListener('click', () => {
            if (pending || submitted || planCard.expired) return;
            const group = button.closest('.plan-question');
            group.querySelectorAll('.pq-option').forEach(option => {
                option.classList.remove('selected');
            });
            button.classList.add('selected');
            confirmButton.disabled = !selectionComplete();
        });
    });
    confirmButton.addEventListener('click', () => {
        if (questions) {
            if (!selectionComplete()) return;
            const message = Array.from(planCard.querySelectorAll('.plan-question'), (group, index) =>
                `${index + 1}. ${group.querySelector('.pq-text').textContent.trim()}：${group.querySelector('.pq-option.selected').textContent.trim()}`
            ).join('\n');
            submitReply(message, '已提交选择');
        } else {
            submitReply('确认，按计划执行', '已确认');
        }
    });
    const editInput = planCard.querySelector('.plan-edit-input');
    const sendEditedReply = () => {
        const message = editInput.value.trim();
        if (message) submitReply(message, '已发送');
    };
    planCard.querySelector('.plan-edit-btn').addEventListener('click', sendEditedReply);
    editInput.addEventListener('keydown', event => {
        if (event.key !== 'Enter' || event.isComposing) return;
        event.preventDefault();
        sendEditedReply();
    });
}

function closePendingAgentPlan(planCard, message = '计划已失效，请重新行动') {
    if (!planCard || planCard.expired) return;
    planCard.expired = true;
    delete planCard.pendingReply;
    planCard.querySelectorAll('button').forEach(button => { button.disabled = true; });
    const input = planCard.querySelector('.plan-edit-input');
    if (input) input.disabled = true;
    if (planCard.querySelector('.plan-confirm-btn')) {
        planCard.querySelector('.plan-actions').textContent = planCard.replyReceived ? '已被 Agent 接收' : message;
    }
}

async function sendAgentInterjection(saveId, input, button, streamId, onQueued, deliveryState) {
    const draft = input.value;
    const message = draft.trim();
    if (!message || button.disabled) return;
    button.disabled = true;
    try {
        await API.post(`/api/game/${saveId}/inject`, { message, stream_id: streamId });
        if (input.value === draft) input.value = '';
        if (onQueued) onQueued(message);
    } catch (error) {
        if (deliveryState?.received) {
            if (input.value === draft) input.value = '';
            return;
        }
        const toast = document.createElement('div');
        toast.className = 'toast-error';
        toast.textContent = '插话发送失败: ' + error.message;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    } finally {
        button.disabled = false;
    }
}

// Stream support for game actions (SSE)
async function submitActionStream(action, overrideUrl, retryContext) {
    if (!currentSaveId || isStreaming) return;
    const input = document.getElementById('freeform-input');
    const restoreMapInputFocus = action.type === 'freeform' &&
        document.getElementById('main-map-view')?.hidden === false && document.activeElement === input;
    const startedAt = globalThis.performance?.now?.() ?? Date.now();
    const actionContext = retryContext || {
        baseNodeId: currentNodeId,
        actionId: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
    };
    const saveId = currentSaveId;
    const streamToken = {};
    let controller = new AbortController();
    streamToken.cancel = () => {
        streamToken.cancelled = true;
        controller.abort();
    };
    const streamId = retryContext?.streamId || globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    const pendingActionKey = `tavern-pending-action:${saveId}`;
    if (!overrideUrl) {
        try {
            sessionStorage.setItem(pendingActionKey, JSON.stringify({
                action, baseNodeId: actionContext.baseNodeId,
                actionId: actionContext.actionId, streamId,
            }));
        } catch (_) { /* storage is optional */ }
    }
    window._activeGameAction = streamToken;
    const isActive = () => currentSaveId === saveId && window._activeGameAction === streamToken;
    let queryStream = action.intent_hint === 'explicit_query';
    isStreaming = true;
    _clearNotifStack();  // 新一轮开始时清除上一轮的通知
    _showGameOverlay(true);

    const prevStateSnap = currentState ? Object.assign({}, currentState) : null;
    const choicesArea = document.getElementById('choices-area');
    const prevChoicesHtml = choicesArea.innerHTML;
    const prevChoices = [...choicesArea.children];
    const restoreChoices = () => {
        if (prevChoices.length) {
            choicesArea.innerHTML = '';
            prevChoices.forEach(choice => choicesArea.appendChild(choice));
        } else {
            choicesArea.innerHTML = prevChoicesHtml;
        }
    };
    document.querySelectorAll('.choice-btn').forEach(b => b.disabled = true);
    input.disabled = true;
    const isRegeneration = action.type === 'regen' && !!overrideUrl;
    const isPartialRegeneration = isRegeneration && (action.stage === 'choices' || action.stage === 'state');
    if (!isPartialRegeneration) choicesArea.innerHTML = '';
    const content = document.getElementById('narrative-content');
    content.querySelectorAll(':scope > .stream-interrupted').forEach(recovery => recovery.remove());
    const existingBlocks = content.querySelectorAll('.turn-block');
    const originalBlock = existingBlocks[existingBlocks.length - 1] || null;
    const block = document.createElement('div');
    block.className = 'turn-block latest';

    // Show player action
    const actionText = action.text || (originalBlock && originalBlock.querySelector('.turn-action')?.textContent);
    if (actionText && action.type !== 'system' && !isPartialRegeneration) {
        const actionDiv = document.createElement('div');
        actionDiv.className = 'turn-action';
        actionDiv.textContent = actionText;
        block.appendChild(actionDiv);
    }

    // Collapsible thinking area (hidden until content arrives)
    const thinkWrap = document.createElement('div');
    thinkWrap.className = 'think-block';
    thinkWrap.style.display = 'none';
    thinkWrap.innerHTML = '<div class="think-header" onclick="this.parentElement.classList.toggle(\'open\')">&#128161; AI 思考过程 <span class="toggle-arrow">&#9662;</span></div><div class="think-body"></div>';
    block.appendChild(thinkWrap);

    const textDiv = document.createElement('div');
    textDiv.className = 'narrative-text';
    block.appendChild(textDiv);
    let narrativeReviewStatus = null;
    const clearNarrativeReviewStatus = () => {
        narrativeReviewStatus?.remove();
        narrativeReviewStatus = null;
    };
    const showNarrativeStatus = (message, draft = false) => {
        if (!narrativeReviewStatus) {
            narrativeReviewStatus = document.createElement('div');
            narrativeReviewStatus.setAttribute('role', 'status');
            block.insertBefore(narrativeReviewStatus, textDiv);
        }
        narrativeReviewStatus.className = draft ? 'agent-status-msg narrative-draft-status' : 'agent-status-msg';
        narrativeReviewStatus.textContent = message;
    };
    const updateNarrativeStreamStatus = (reviewed = false) => {
        if (isAgenticStream && !isPartialRegeneration) {
            showNarrativeStatus(reviewed
                ? '叙事初审通过 · 回合未结算，内容仍可能修订'
                : '推演草稿 · 正在核验，内容可能修订', true);
        } else {
            clearNarrativeReviewStatus();
        }
    };

    // Collapsible agent tool-call area (hidden until agentic events arrive)
    const agentToolsWrap = document.createElement('div');
    agentToolsWrap.className = 'agent-tools-block';
    agentToolsWrap.style.display = 'none';
    agentToolsWrap.innerHTML = '<div class="agent-tools-header" onclick="this.parentElement.classList.toggle(\'open\')">\u{1F527} Agent 工具调用 <span class="toggle-arrow">▾</span></div><div class="agent-tools-body"></div>';
    block.insertBefore(agentToolsWrap, textDiv);

    // Agent controls: stop button + inject input (shown during streaming)
    const agentControls = document.createElement('div');
    agentControls.className = 'agent-controls';
    agentControls.style.display = 'none';
    agentControls.innerHTML =
        '<button class="stop-btn" title="停止生成">■ 停止</button>' +
        '<input class="inject-input" placeholder="插话…" />' +
        '<button class="inject-btn">发送</button>';
    block.appendChild(agentControls);

    // Wire up stop button
    const stopBtn = agentControls.querySelector('.stop-btn');
    stopBtn.addEventListener('click', async () => {
        if (!isActive() || stopBtn.disabled) return;
        stopBtn.disabled = true;
        try {
            const token = localStorage.getItem('tavern_api_token');
            const hdrs = { 'Content-Type': 'application/json' };
            if (token) hdrs['Authorization'] = 'Bearer ' + token;
            const response = await fetch('/api/game/' + saveId + '/abort', {
                method: 'POST', headers: hdrs, body: JSON.stringify({ stream_id: streamId }),
            });
            if (!isActive()) return;
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            stopBtn.innerHTML = '正在停止...';
        } catch (error) {
            if (!isActive()) return;
            stopBtn.disabled = false;
            const toast = document.createElement('div');
            toast.className = 'toast-error';
            toast.textContent = '停止失败: ' + error.message;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 4000);
        }
    });

    // Wire up inject button
    const injectInput = agentControls.querySelector('.inject-input');
    const injectBtn = agentControls.querySelector('.inject-btn');
    let pendingInterjection = null;
    const doInject = () => {
        if (!isActive() || !canControlStream || injectBtn.disabled || !injectInput.value.trim()) return;
        const request = { message: injectInput.value.trim(), received: false, dropped: false };
        pendingInterjection = request;
        return sendAgentInterjection(saveId, injectInput, injectBtn, streamId, message => {
            if (!isActive() || !canControlStream || request.received || request.dropped) return;
            const planCard = agentToolsWrap.querySelector('.agent-plan-card');
            if (planCard && !planCard.pendingReply) closePendingAgentPlan(planCard, '已插话，原计划失效');
            agentToolsWrap.style.display = '';
            const status = document.createElement('div');
            status.className = 'agent-status-msg';
            status.textContent = '插话已排队，等待 Agent 接收: ' + message.slice(0, 100);
            agentToolsWrap.querySelector('.agent-tools-body').appendChild(status);
            if (canControlStream && typeof setMainMapAgentStatus === 'function') {
                setMainMapAgentStatus(saveId, '插话已排队，等待 Agent 接收', agentControls);
            }
        }, request).finally(() => {
            if (pendingInterjection === request) pendingInterjection = null;
        });
    };
    injectBtn.addEventListener('click', doInject);
    injectInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doInject(); });

    if (isRegeneration && originalBlock && !isPartialRegeneration) originalBlock.replaceWith(block);
    else content.appendChild(block);
    if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, queryStream ? '正在回答询问…' : '正在生成回合…');
    if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, { type: 'begin', regeneration: isRegeneration, is_query: queryStream });

    // Remove "latest" from previous blocks
    if (!isPartialRegeneration) {
        content.querySelectorAll('.turn-block.latest').forEach(b => {
            if (b !== block) b.classList.remove('latest');
        });
        if (typeof _syncTurnOverflow === 'function') _syncTurnOverflow();
    }

    let timeoutId = null;
    let waitingForPlayer = false;
    let _scrollRAF = null;
    const _scrollToBottom = () => {
        if (_scrollRAF) return;
        _scrollRAF = requestAnimationFrame(() => {
            const area = document.getElementById('narrative-area');
            area.scrollTop = area.scrollHeight;
            _scrollRAF = null;
        });
    };
    let receivedFinal = false;
    const confirmStreamUnload = event => {
        if (receivedFinal || !isActive() || !overrideUrl) return;
        event.preventDefault();
        event.returnValue = '';
    };
    window.addEventListener('beforeunload', confirmStreamUnload);
    let wasAborted = false;
    let streamError = '';
    let sawServerError = false;
    let sawUnnumberedEvent = false;
    let discardPartial = false;
    let preservePendingAction = false;
    let canControlStream = false;
    let isAgenticStream = false;
    let sawAgentPlan = false;
    let pendingUndo = null;
    const clientTimings = {};
    let displayedAgentTrace = null;
    let firstPaintPending = false;
    const elapsed = () => Math.round((globalThis.performance?.now?.() ?? Date.now()) - startedAt);
    const showAgentTrace = () => {
        if (displayedAgentTrace && typeof renderAgentTrace === 'function') {
            renderAgentTrace({ ...displayedAgentTrace, client_timings_ms: clientTimings });
        }
    };
    const recordFirstText = (fromEvent) => {
        if (firstPaintPending || !textDiv.textContent.trim()) return;
        firstPaintPending = true;
        if (fromEvent) clientTimings.first_text_event_ms = elapsed();
        requestAnimationFrame(() => requestAnimationFrame(() => {
            if (currentSaveId !== saveId || block.parentElement !== content ||
                content.querySelector('.turn-block.latest') !== block ||
                (window._activeGameAction && window._activeGameAction !== streamToken)) return;
            clientTimings.first_text_paint_ms = elapsed();
            showAgentTrace();
        }));
    };
    try {
        const sseHeaders = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('tavern_api_token');
        if (token) sseHeaders['Authorization'] = `Bearer ${token}`;

        const armTimeout = () => {
            if (timeoutId !== null) clearTimeout(timeoutId);
            timeoutId = setTimeout(() => controller.abort(), waitingForPlayer ? 60000 : 300000);
        };
        const streamUrl = overrideUrl
            ? `${overrideUrl}${overrideUrl.includes('?') ? '&' : '?'}stream_id=${encodeURIComponent(streamId)}`
            : `/api/game/${saveId}/action/stream`;
        let lastEventId = 0;
        for (let reconnects = 0; !receivedFinal; reconnects++) {
            if (reconnects) {
                if (isRegeneration || sawUnnumberedEvent || reconnects > 3 || wasAborted || streamToken.cancelled || sawServerError) {
                    throw new Error(streamError || 'Connection lost');
                }
                controller = new AbortController();
                await new Promise(resolve => setTimeout(resolve, reconnects * 500));
            }
            armTimeout();
            try {
        const response = await fetch(streamUrl, {
            method: 'POST',
            headers: sseHeaders,
            body: overrideUrl ? undefined : JSON.stringify({
                ...action, stream_id: streamId,
                expected_node_id: actionContext.baseNodeId, action_id: actionContext.actionId,
            }),
            signal: controller.signal,
        });

        if (!response.ok) {
            let errMsg = `HTTP ${response.status}`;
            try { const errBody = await response.json(); errMsg = errBody.detail || errMsg; } catch (_) {}
            const error = new Error(errMsg);
            error.status = response.status;
            throw error;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let eventId = 0;

        while (!receivedFinal) {
            const { done, value } = await reader.read();
            if (done) break;
            if (!isActive()) continue;
            if (timeoutId !== null && value?.byteLength) armTimeout();

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.startsWith('id: ')) {
                    eventId = Number(line.slice(4));
                    continue;
                }
                if (line.startsWith('data: ')) {
                    if (!eventId) sawUnnumberedEvent = true;
                    if (eventId && eventId <= lastEventId) continue;
                    if (eventId) lastEventId = eventId;
                    const data = line.slice(6);
                    if (data === '[DONE]') continue;

                    try {
                        const parsed = JSON.parse(data);
                        if (parsed.type === 'preflight' && parsed.stage === 'query') queryStream = true;
                        if (parsed.type !== 'final' && parsed.type !== 'error' && !(wasAborted && parsed.type === 'agent_controls_closed') && typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, queryStream && ['text', 'narrative_revised', 'agent_done', 'agent_controls_closed'].includes(parsed.type) ? { ...parsed, is_query: true } : parsed);
                        if (parsed.type === 'stream_ready') {
                            canControlStream = parsed.controls === true;
                            isAgenticStream = canControlStream || parsed.stop_available === true;
                            injectInput.disabled = !canControlStream;
                            injectBtn.disabled = !canControlStream;
                            if (isAgenticStream) {
                                agentControls.style.display = '';
                                if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, canControlStream
                                    ? 'Agent 正在推演，可发送插话或请求停止' : '正在准备推演，可请求停止', agentControls);
                            }
                        } else if (parsed.type === 'agent_controls_ready') {
                            canControlStream = true;
                            isAgenticStream = true;
                            injectInput.disabled = false;
                            injectBtn.disabled = false;
                            agentControls.style.display = '';
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, '正在准备推演，可发送插话或请求停止', agentControls);
                        } else if (parsed.type === 'preflight') {
                            if (parsed.stage === 'narrative_review' && !narrativeReviewStatus) {
                                showNarrativeStatus('正在生成并核验叙事正文…');
                            }
                            const stages = {
                                background: '正在同步上一轮的后台任务',
                                intent: '正在准备行动',
                                query: '正在检索资料并回答询问',
                                route_retrieval: '正在检索剧本并规划行动',
                            };
                            if (isAgenticStream && Object.hasOwn(stages, parsed.stage) && typeof setMainMapAgentStatus === 'function') {
                                setMainMapAgentStatus(saveId, `${stages[parsed.stage]}，${canControlStream ? '可插话或请求停止' : '可请求停止'}`, agentControls);
                            }
                        } else if (parsed.type === 'agent_controls_closed') {
                            canControlStream = false;
                            waitingForPlayer = false;
                            agentControls.style.display = 'none';
                            closePendingAgentPlan(agentToolsWrap.querySelector('.agent-plan-card'));
                            armTimeout();
                            if (!wasAborted && typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, queryStream ? '询问完成，正在整理回答…' : '推演完成，正在结算回合…');
                        } else if (parsed.type === 'agent_inject_dropped') {
                            if (!parsed.plan_id && pendingInterjection?.message === parsed.message) pendingInterjection.dropped = true;
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = (parsed.plan_id ? '计划回复未生效' : '插话未生效') + '（Agent 已结束）: ' + (parsed.message || '').slice(0, 100);
                            body.appendChild(msg);
                            const planCard = agentToolsWrap.querySelector('.agent-plan-card');
                            if (parsed.plan_id && parsed.plan_id === planCard?.planId && planCard?.pendingReply === parsed.message) {
                                closePendingAgentPlan(planCard, '回复未生效（Agent 已结束）');
                            }
                            const toast = document.createElement('div');
                            toast.className = 'toast-error';
                            toast.textContent = msg.textContent;
                            document.body.appendChild(toast);
                            setTimeout(() => toast.remove(), 4000);
                        } else if (parsed.type === 'agent_stop_missed') {
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = '停止请求未赶上 Agent 终结，未中断本轮';
                            agentToolsWrap.querySelector('.agent-tools-body').appendChild(msg);
                            const toast = document.createElement('div');
                            toast.className = 'toast-error';
                            toast.textContent = msg.textContent;
                            document.body.appendChild(toast);
                            setTimeout(() => toast.remove(), 4000);
                        } else if (parsed.type === 'thinking') {
                            if (!isAgenticStream) {
                                thinkWrap.style.display = '';
                                thinkWrap.querySelector('.think-body').insertAdjacentHTML('beforeend', escapeHtml(parsed.content).replace(/\n/g, '<br>'));
                            }
                        } else if (parsed.type === 'tool_started') {
                            agentToolsWrap.style.display = '';
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            body.querySelector('.agent-tool-running')?.remove();
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg agent-tool-running';
                            msg.textContent = '正在调用 ' + (parsed.tool_name || '工具') + '…';
                            body.appendChild(msg);
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, msg.textContent, agentControls);
                        } else if (parsed.type === 'tool_call') {
                            isAgenticStream = true;
                            // Agentic: show tool-call card with type-specific icon
                            agentToolsWrap.style.display = '';
                            if (canControlStream) {
                                agentControls.style.display = '';
                                if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, 'Agent 正在推演，可发送插话或请求停止', agentControls);
                            }
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            body.querySelector('.agent-tool-running')?.remove();
                            const card = document.createElement('div');
                            card.className = 'agent-tool-card';
                            const toolName = parsed.tool_name || '';
                            const _toolIcon = /search|retriev|query|lookup|find|lorebook|vector/i.test(toolName) ? '\u{1F50D}'
                                : /state|status|attrib|inventory|check/i.test(toolName) ? '\u{1F4CA}'
                                : /choice|option|branch|plan|decide/i.test(toolName) ? '\u{1F3AF}'
                                : '\u{1F527}';
                            card.innerHTML =
                                '<span class="tool-icon">' + _toolIcon + '</span>' +
                                '<span class="tool-label">[' + escapeHtml(parsed.label || '') + ' R' + (parsed.round || '') + ']</span> ' +
                                '<span class="tool-name">' + escapeHtml(toolName) + '</span>' +
                                '<div class="tool-result">执行详情仅供推演内部使用</div>';
                            body.appendChild(card);
                            // Update header counter
                            const cnt = body.querySelectorAll('.agent-tool-card').length;
                            agentToolsWrap.querySelector('.agent-tools-header').innerHTML =
                                '\u{1F527} Agent 工具调用 (' + cnt + ') <span class="toggle-arrow">▾</span>';
                            _scrollToBottom();
                        } else if (parsed.type === 'agent_done') {
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = (parsed.label || 'Agent') + ' 阶段完成，共' + (parsed.round || '?') + '轮';
                            body.appendChild(msg);
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, '正在核验叙事并结算回合…');
                        } else if (parsed.type === 'aborted') {
                            wasAborted = true;
                            canControlStream = false;
                            closePendingAgentPlan(agentToolsWrap.querySelector('.agent-plan-card'));
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = '已中断 (' + (parsed.label || '') + ')';
                            body.appendChild(msg);
                            agentControls.style.display = 'none';
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, '推演已停止');
                        } else if (parsed.type === 'agent_plan') {
                            sawAgentPlan = true;
                            // 替换旧计划（不堆叠）
                            agentToolsWrap.style.display = '';
                            agentToolsWrap.classList.add('open');
                            agentControls.style.display = '';
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            body.querySelector('.agent-tool-running')?.remove();
                            const oldPlan = body.querySelector('.agent-plan-card');
                            if (oldPlan) {
                                closePendingAgentPlan(oldPlan);
                                oldPlan.remove();
                            }
                            const planCard = document.createElement('div');
                            planCard.className = 'agent-plan-card';
                            let planHtml =
                                '<div class="plan-label">Agent 计划</div>' +
                                '<div class="plan-text">' + escapeHtml(parsed.plan || '') + '</div>';
                            // 交互式选项
                            const questions = parsed.questions || [];
                            if (questions.length > 0) {
                                planHtml += '<div class="plan-questions">';
                                questions.forEach((q, qi) => {
                                    planHtml += '<div class="plan-question"><div class="pq-text">' + escapeHtml(q.question || '') + '</div>';
                                    (q.options || []).forEach((opt, oi) => {
                                        planHtml += '<button class="pq-option" data-qi="' + qi + '" data-oi="' + oi + '">' + escapeHtml(opt) + '</button>';
                                    });
                                    planHtml += '</div>';
                                });
                                planHtml += '</div>';
                            }
                            if (parsed.plan_id && questions.length) {
                                planHtml += '<div class="plan-actions">' +
                                    '<button class="btn-primary btn-small plan-confirm-btn">提交选择</button>' +
                                    '<input type="text" class="plan-edit-input" placeholder="或直接输入你的想法..." />' +
                                    '<button class="btn-secondary btn-small plan-edit-btn">发送</button>' +
                                    '</div>';
                            } else {
                                planHtml += '<div class="plan-result">无需确认，Agent 继续执行</div>';
                            }
                            planCard.innerHTML = planHtml;
                            body.appendChild(planCard);
                            if (parsed.plan_id && questions.length) wireAgentPlanCard(planCard, saveId, streamId, parsed.plan_id);
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, parsed.plan_id ? 'Agent 等待你选择' : 'Agent 按计划推演中', parsed.plan_id ? planCard : agentControls);
                            _scrollToBottom();
                        } else if (parsed.type === 'user_inject') {
                            if (!parsed.plan_id && pendingInterjection?.message === parsed.message) pendingInterjection.received = true;
                            waitingForPlayer = false;
                            armTimeout();
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, 'Agent 继续推演…', agentControls);
                            const planCard = agentToolsWrap.querySelector('.agent-plan-card');
                            if (parsed.plan_id && parsed.plan_id === planCard?.planId) {
                                planCard.replyReceived = true;
                                if (planCard.pendingReply) {
                                    planCard.querySelectorAll('button').forEach(button => { button.disabled = true; });
                                    const input = planCard.querySelector('.plan-edit-input');
                                    if (input) input.disabled = true;
                                    planCard.querySelector('.plan-actions').textContent = '已被 Agent 接收';
                                } else {
                                    closePendingAgentPlan(planCard, '已被 Agent 接收');
                                }
                            } else if (planCard && !planCard.pendingReply) {
                                closePendingAgentPlan(planCard, '已插话，原计划失效');
                            }
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            body.querySelector('.agent-waiting')?.remove();
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = (parsed.plan_id ? 'Agent 已接收计划回复: ' : 'Agent 已接收插话: ') + (parsed.message || '').slice(0, 100);
                            body.appendChild(msg);
                        } else if (parsed.type === 'agent_max_rounds') {
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = (parsed.label || 'Agent') + ' 达到最大轮数 (' + (parsed.max_rounds || '') + ')';
                            body.appendChild(msg);
                        } else if (parsed.type === 'plan_auto_confirmed') {
                            waitingForPlayer = false;
                            armTimeout();
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, 'Agent 继续推演…', agentControls);
                            agentToolsWrap.querySelector('.agent-tools-body')?.querySelector('.agent-waiting')?.remove();
                            closePendingAgentPlan(agentToolsWrap.querySelector('.agent-plan-card'), '已自动确认');
                        } else if (parsed.type === 'agent_waiting') {
                            waitingForPlayer = true;
                            armTimeout();
                            agentControls.style.display = '';
                            const planCard = agentToolsWrap.querySelector('.agent-plan-card');
                            const pendingPlan = planCard?.querySelector('.plan-confirm-btn') ? planCard : null;
                            const waitingStatus = (parsed.reason || 'Agent 等待中') + '；刷新后可续接当前回合';
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, waitingStatus, pendingPlan || agentControls);
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg agent-waiting';
                            msg.textContent = '⏳ ' + waitingStatus;
                            body.appendChild(msg);
                        } else if (parsed.type === 'model_waiting') {
                            waitingForPlayer = false;
                            armTimeout();
                            agentToolsWrap.querySelector('.agent-tools-body')?.querySelector('.agent-waiting')?.remove();
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(
                                saveId, canControlStream ? '模型处理中，可插话或请求停止' : '模型处理中，可请求停止', agentControls);
                        } else if (parsed.type === 'model_output_started') {
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(
                                saveId, canControlStream ? '模型已开始生成，可插话或请求停止' : '模型已开始生成，可请求停止', agentControls);
                        } else if (parsed.type === 'tool_preparing') {
                            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(
                                saveId, '模型正在准备工具调用', agentControls);
                        } else if (parsed.type === 'text') {
                            updateNarrativeStreamStatus(parsed.reviewed === true);
                            const _clean = (parsed.content || '').replace(/<reflect>[\s\S]*?<\/reflect>/gi, '');
                            const span = document.createElement('span');
                            span.className = 'stream-fade-in';
                            span.innerHTML = escapeHtml(_clean).replace(/\n/g, '<br>');
                            textDiv.appendChild(span);
                            recordFirstText(true);
                            _scrollToBottom();
                        } else if (parsed.type === 'narrative_revised') {
                            updateNarrativeStreamStatus(parsed.reviewed === true);
                            const _clean = (parsed.content || '').replace(/<reflect>[\s\S]*?<\/reflect>/gi, '');
                            textDiv.innerHTML = escapeHtml(_clean).replace(/\n/g, '<br>');
                            recordFirstText(true);
                            _scrollToBottom();
                        } else if (parsed.type === 'error') {
                            clearNarrativeReviewStatus();
                            sawServerError = true;
                            streamError = parsed.message || parsed.error || '';
                            discardPartial = !!parsed.discard_partial;
                        } else if (parsed.type === 'final') {
                            clearNarrativeReviewStatus();
                            receivedFinal = true;
                            const mapFinal = { ...parsed, regeneration: isRegeneration };
                            if (isPartialRegeneration) mapFinal.stage = action.stage;
                            agentControls.style.display = 'none';
                            agentToolsWrap.classList.remove('open');
                            if (parsed.agent_trace) thinkWrap.remove();
                            if (isPartialRegeneration) {
                                const _s = (label, fn) => { try { fn(); } catch(e) { console.error(`[stream:${label}]`, e); } };
                                if (action.stage === 'choices') _s('renderChoices', () => renderChoices(parsed.choices || []));
                                if (action.stage === 'state') _s('showStateChanges', () => showStateChanges(parsed.state_changes));
                                _s('updateSwipeControls', () => updateSwipeControls(parsed.swipe_index, parsed.total_swipes));
                                if (parsed.state) {
                                    currentState = parsed.state;
                                    _s('updateStatusPanel', () => updateStatusPanel(currentState));
                                    _s('updateHeader', () => updateHeader());
                                }
                                if (parsed.node_id) currentNodeId = parsed.node_id;
                                block.remove();
                                _s('loadSidebarSaves', () => loadSidebarSaves());
                                if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, mapFinal);
                                continue;
                            }
                            if (parsed.is_query) {
                                block.remove();
                                _showQueryReply(action, parsed.narrative);
                                restoreChoices();
                                if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, mapFinal);
                                continue;
                            }
                            if (parsed.is_undo) {
                                block.remove();
                                pendingUndo = parsed;
                                if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, mapFinal);
                                continue;
                            }
                            if (parsed.is_rewrite && !isRegeneration && originalBlock) {
                                const previousAction = originalBlock.querySelector('.turn-action');
                                const previewAction = block.querySelector('.turn-action');
                                if (previewAction) {
                                    if (previousAction) previewAction.textContent = previousAction.textContent;
                                    else previewAction.remove();
                                }
                                block.remove();
                                originalBlock.replaceWith(block);
                            }
                            const isReplacement = isRegeneration || parsed.is_rewrite;
                            const _s = (label, fn) => { try { fn(); } catch(e) { console.error(`[stream:${label}]`, e); } };
                            _s('historyDetails', () => _appendHistoryDetails(block, parsed));
                            _s('renderChoices', () => renderChoices(parsed.choices || []));
                            _s('renderDice', () => renderDice(parsed.dice_rolls));
                            _s('showStateChanges', () => showStateChanges(parsed.state_changes));
                            _s('showTriggeredEvents', () => showTriggeredEvents(parsed.triggered_events));
                            _s('showExpiredStates', () => showExpiredStates(parsed.expired_states));
                            _s('showCheckResult', () => showCheckResult(parsed.check_result));
                            _s('showTriggeredConsequences', () => showTriggeredConsequences(parsed.triggered_consequences));
                            _s('showAchievedMilestones', () => showAchievedMilestones(parsed.achieved_milestones));
                            _s('showMilestoneProgress', () => showMilestoneProgress(parsed.milestone_progress));
                            _s('showThresholdEvents', () => showThresholdEvents(parsed.threshold_events));
                            _s('showLoreHints', () => showDataBankPanel(parsed.activated_lore, parsed.databank_hits));
                            _s('showWarnings', () => showWarnings(parsed.warnings));
                            _s('showNpcInterjections', () => showNpcInterjections(parsed.npc_interjections));
                            _s('showNpcAttitudeNotifications', () => showNpcAttitudeNotifications(parsed.npc_attitude_notifications));
                            _s('showEmotionLabel', () => showEmotionLabel(parsed.emotion));
                            _s('showTriggerNotifications', () => showTriggerNotifications(parsed.trigger_notifications));
                            _s('showStoryTreeUpdates', () => showStoryTreeUpdates(parsed.story_tree_updates));
                            _s('renderSceneImage', () => {
                                if (parsed.scene_image_path) renderSceneImage({url: parsed.scene_image_path});
                                else if (parsed.scene_image) renderSceneImage(parsed.scene_image);
                            });
                            currentState = parsed.state;
                            if (parsed.node_id) currentNodeId = parsed.node_id;
                            // Enhance narrative text after streaming completes
                            _s('enhanceNarrative', () => {
                                if (typeof parsed.narrative === 'string') {
                                    textDiv.innerHTML = formatNarrativeHtml(parsed.narrative);
                                } else if (typeof enhanceNarrative === 'function') {
                                    // Strip stream-fade-in spans to get clean text for regex processing
                                    let cleanHtml = textDiv.innerHTML
                                        .replace(/<span class="stream-fade-in">/g, '')
                                        .replace(/<\/span>/g, '');
                                    textDiv.innerHTML = enhanceNarrative(cleanHtml);
                                }
                            });
                            recordFirstText(false);
                            _s('showTurnRecap', () => showTurnRecap(parsed, prevStateSnap, block));
                            _s('updateStatusPanel', () => updateStatusPanel(currentState));
                            _s('showChoiceRipples', () => showChoiceRipples(currentState));
                            _s('showMemoryEchoes', () => showMemoryEchoes(currentState));
                            _s('updateHeader', () => updateHeader());
                            _s('updateTurnIndicator', () => updateTurnIndicator());
                            _s('loadSidebarSaves', () => loadSidebarSaves());
                            if (isReplacement) _s('updateSwipeControls', () => updateSwipeControls(parsed.swipe_index, parsed.total_swipes));
                            // #10 成本追踪: 显示 token 用量
                            if (parsed.token_usage && parsed.token_usage.total > 0) {
                                _s('showTokenUsage', () => {
                                    const el = document.getElementById('token-usage');
                                    if (el) {
                                        const u = parsed.token_usage;
                                        el.textContent = u.total + ' tokens';
                                        el.title = 'Prompt: ' + u.prompt + ' / Completion: ' + u.completion;
                                        el.style.display = '';
                                    }
                                });
                            }
                            if (parsed.game_over) _s('showGameOver', () => showGameOver(parsed.game_over, parsed.game_statistics));
                            // E1: Agent Trace
                            if (parsed.agent_trace) {
                                displayedAgentTrace = parsed.agent_trace;
                                _s('renderAgentTrace', showAgentTrace);
                            }
                            // E3: Refresh performance dashboard
                            _s('updatePerfDashboard', () => updatePerformanceDashboard());
                            // D2: 显示 Agent 主动建议
                            if (parsed.proactive_hint) {
                                _s('showProactiveHint', () => {
                                    const hint = document.createElement('div');
                                    hint.className = 'proactive-hint';
                                    hint.textContent = '💡 ' + parsed.proactive_hint;
                                    block.appendChild(hint);
                                });
                            }
                            if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, mapFinal);
                        }
                    } catch (e) { console.warn('[stream-event]', e); }
                }
            }
        }
        if (receivedFinal) reader.cancel?.().catch(() => {});
            } catch (error) {
                if (error.status || isRegeneration || sawUnnumberedEvent || wasAborted || streamToken.cancelled || !isActive() || reconnects >= 3) throw error;
            } finally {
                clearTimeout(timeoutId);
                timeoutId = null;
            }
            if (wasAborted || !isActive()) break;
        }
        if (!isActive()) return;
        if (wasAborted) {
            if (isRegeneration && originalBlock && !isPartialRegeneration) block.replaceWith(originalBlock);
            else block.remove();
            if (!choicesArea.children.length) restoreChoices();
            if (action.type === 'freeform' && !isRegeneration && !input.value?.trim()) input.value = action.text || '';
            return;
        }
        if (!receivedFinal) throw new Error(streamError || '生成未完成，请重试');
        retryContext?.interruptedBlocks?.forEach(oldBlock => oldBlock.remove());
        if (retryContext?.interruptedBlocks?.length) updateTurnIndicator();
        if (pendingUndo) await _showUndoneTurn(pendingUndo, saveId, isActive);
    } catch (e) {
        if (!isActive()) return;
        if (e.status === 409) {
            try { sessionStorage.removeItem(pendingActionKey); } catch (_) { /* storage is optional */ }
        }
        if (streamToken.cancelled && !receivedFinal) {
            if (isRegeneration && originalBlock && !isPartialRegeneration) block.replaceWith(originalBlock);
            else block.remove();
            if (!choicesArea.children.length) restoreChoices();
            return;
        }
        if (receivedFinal && !pendingUndo) return;
        const msg = e.name === 'AbortError' ? '请求超时，请重试' : e.message;
        const hasPartialText = textDiv && textDiv.textContent.trim().length > 0;
        const retryable = (discardPartial ? sawServerError && !isRegeneration && actionContext.actionId :
            (hasPartialText || sawAgentPlan ||
            ((!e.status || e.status >= 500) && !isRegeneration && actionContext.actionId && (sawServerError || !sawUnnumberedEvent)))) &&
            e.status !== 409 && !(isRegeneration && originalBlock);
        preservePendingAction = retryable;
        let retryInterruptedAction = null;
        if (isRegeneration && originalBlock && !receivedFinal) {
            if (isPartialRegeneration) block.remove();
            else block.replaceWith(originalBlock);
        } else if (discardPartial) {
            block.remove();
            retryContext?.interruptedBlocks?.forEach(oldBlock => oldBlock.remove());
            if (action.type === 'freeform' && !isRegeneration) {
                const input = document.getElementById('freeform-input');
                if (!input.value?.trim()) input.value = action.text || '';
            }
        } else if (!retryable) {
            if (block && block.parentElement) block.remove();
            if (action.type === 'freeform' && !isRegeneration) {
                const input = document.getElementById('freeform-input');
                if (!input.value?.trim()) input.value = action.text || '';
            }
        }
        if (retryable) {
            const retryBar = document.createElement('div');
            retryBar.className = 'stream-interrupted';
            retryBar.innerHTML = `<span>（${sawServerError ? '推演失败' : '连接中断'}：${escapeHtml(msg)}）</span> `;
            const retryBtn = document.createElement('button');
            retryBtn.className = 'retry-btn';
            retryBtn.textContent = isRegeneration ? '重试' : sawServerError ? '重新推演' : '续接';
            const retryAnchor = discardPartial ? retryBar : block;
            let retryStarted = false;
            retryInterruptedAction = () => {
                if (retryStarted || currentSaveId !== saveId || currentNodeId !== actionContext.baseNodeId ||
                    isStreaming || retryAnchor.parentElement !== content) return;
                retryStarted = true;
                if (action.type === 'freeform' && input.value === action.text) input.value = '';
                const request = submitActionStream(action, overrideUrl, {
                    ...actionContext, streamId, interruptedBlocks: [...(retryContext?.interruptedBlocks || []), retryAnchor],
                });
                retryBar.remove();
                return request;
            };
            retryBtn.addEventListener('click', retryInterruptedAction);
            retryBar.appendChild(retryBtn);
            (discardPartial ? content : block).appendChild(retryBar);
        }
        if (typeof recordMainMapStreamEvent === 'function') recordMainMapStreamEvent(saveId, streamToken, {
            type: pendingUndo ? 'undo_refresh_error' : 'error',
            retryable,
            retry: retryInterruptedAction,
            server_error: sawServerError,
            discard_partial: discardPartial, regeneration: isRegeneration,
        });
        // 短暂提示错误
        const toast = document.createElement('div');
        toast.className = 'toast-error';
        toast.textContent = pendingUndo
            ? `撤销已生效，但画面刷新失败，请重新加载存档: ${msg}`
            : e.status === 409 ? msg : `生成失败: ${msg}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
        // Restore previous choices so the player isn't stuck
        if (!choicesArea.children.length && (prevChoices.length || prevChoicesHtml)) {
            restoreChoices();
        }
        // 刷新后端状态确保前端同步
        try {
            const freshState = await API.get(`/api/game/${saveId}/state`);
            if (!isActive()) return;
            currentState = freshState;
            updateStatusPanel(currentState);
        } catch (_) { /* 刷新失败不阻塞 */ }
    } finally {
        if (!overrideUrl && (receivedFinal || wasAborted || (sawServerError && !preservePendingAction))) {
            try { sessionStorage.removeItem(pendingActionKey); } catch (_) { /* storage is optional */ }
        }
        window.removeEventListener('beforeunload', confirmStreamUnload);
        clearNarrativeReviewStatus();
        if (timeoutId) clearTimeout(timeoutId);
        if (isActive()) {
            closePendingAgentPlan(agentToolsWrap.querySelector('.agent-plan-card'));
            if (typeof setMainMapAgentStatus === 'function') setMainMapAgentStatus(saveId, '');
            window._activeGameAction = null;
            isStreaming = false;
            _showGameOverlay(false);
            input.disabled = false;
            document.querySelectorAll('.choice-btn').forEach(b => b.disabled = false);
            if (restoreMapInputFocus && document.getElementById('main-map-view')?.hidden === false &&
                document.activeElement === document.body) input.focus({ preventScroll: true });
        }
    }
}

// Format game time for display
function formatGameTime(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        const month = d.getMonth() + 1;
        const day = d.getDate();
        const hour = d.getHours().toString().padStart(2, '0');
        const min = d.getMinutes().toString().padStart(2, '0');
        const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
        return `${month}月${day}日 周${weekdays[d.getDay()]} ${hour}:${min}`;
    } catch {
        return isoStr;
    }
}

function toggleAdventureLog() {
    const content = document.getElementById('adventure-log-content');
    const arrow = document.getElementById('log-toggle-arrow');
    if (content.classList.contains('open')) {
        content.classList.remove('open');
        arrow.classList.remove('open');
    } else {
        content.classList.add('open');
        arrow.classList.add('open');
        loadAdventureLog();
    }
}

// Keyboard shortcuts: 1-4 to select choices
document.addEventListener('keydown', (e) => {
    if (!/^[1-4]$/.test(e.key) || e.defaultPrevented || e.repeat || e.isComposing ||
        e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || isStreaming || !currentSaveId) return;
    if (e.target.isContentEditable || e.target.closest('input, textarea, select, [role="textbox"]')) return;
    const game = document.getElementById('game-play-screen');
    if (!document.getElementById('panel-game').classList.contains('active') ||
        !game.getClientRects().length || game.closest('[inert], [hidden]')) return;
    if (e.target !== document.body && e.target !== document.documentElement && !game.contains(e.target)) return;
    if (document.querySelector('.mobile-open')) return;
    const overlays = document.querySelectorAll('.modal, .pov-modal-overlay, .game-overlay, dialog[open], [aria-modal="true"]');
    if (Array.from(overlays).some(overlay => overlay.getClientRects().length &&
        getComputedStyle(overlay).visibility !== 'hidden')) return;
    const button = document.querySelectorAll('#choices-area .choice-btn')[Number(e.key) - 1];
    const mapSuggestions = document.getElementById('main-map-suggested-actions');
    if (mapSuggestions && !mapSuggestions.open && mapSuggestions.contains(button)) return;
    if (!button || button.disabled || !button.getClientRects().length ||
        button.closest('[inert], [hidden]') || getComputedStyle(button).visibility !== 'visible') return;
    e.preventDefault();
    button.click();
});

/* ──── Scene Image Functions ──── */

function renderSceneImage(imageData) {
    const container = document.getElementById('scene-image-container');
    const genBar = document.getElementById('scene-image-gen-bar');
    if (!container) return;
    if (!imageData || (!imageData.base64 && !imageData.url)) {
        container.style.display = 'none';
        container.classList.remove('overlay-mode');
        const gp = document.getElementById('game-play-screen');
        if (gp) gp.classList.remove('img-overlay-active');
        // Show generate button if image provider is likely configured
        if (genBar && localStorage.getItem('tavern_img_provider_enabled')) {
            genBar.style.display = '';
        }
        return;
    }
    const img = document.getElementById('scene-image');
    const src = imageData.base64
        ? `data:image/png;base64,${imageData.base64}`
        : imageData.url;
    img.src = src;
    container.style.display = '';
    if (genBar) genBar.style.display = 'none';
    applyImageDisplayMode();
}

function toggleImageMode() {
    const current = localStorage.getItem('tavern_img_mode') || 'split';
    const next = current === 'split' ? 'overlay' : 'split';
    localStorage.setItem('tavern_img_mode', next);
    applyImageDisplayMode();
}

function applyImageDisplayMode() {
    const mode = localStorage.getItem('tavern_img_mode') || 'split';
    const container = document.getElementById('scene-image-container');
    const gamePlay = document.getElementById('game-play-screen');
    if (!container) return;

    if (mode === 'overlay') {
        container.classList.add('overlay-mode');
        if (gamePlay) gamePlay.classList.add('img-overlay-active');
    } else {
        container.classList.remove('overlay-mode');
        if (gamePlay) gamePlay.classList.remove('img-overlay-active');
    }
}

/* ──── Image Settings Panel ──── */

function onImgProviderChange() {
    const val = document.getElementById('img-provider-select').value;
    document.getElementById('img-openai-config').style.display = val === 'openai' ? '' : 'none';
    document.getElementById('img-gemini-config').style.display = val === 'gemini' ? '' : 'none';
    document.getElementById('img-comfyui-config').style.display = val === 'comfyui' ? '' : 'none';
}

/* ──── Image Profile Management ──── */

let _editingImgProfileId = null;

function _showImgStatus(msg, ok) {
    const el = document.getElementById('img-config-status');
    el.innerHTML = `<span style="color:var(--${ok ? 'success,#4caf50' : 'danger'})">${msg}</span>`;
    setTimeout(() => { el.innerHTML = ''; }, 3000);
}

async function loadImageProfiles() {
    try {
        const profiles = await API.get('/api/config/image/profiles');
        const container = document.getElementById('img-profiles-list');
        container.innerHTML = '';
        let hasActive = false;
        (profiles || []).forEach(p => {
            const badge = p.is_active ? '<span class="profile-badge">当前</span>' : '';
            if (p.is_active) hasActive = true;
            const providerLabels = { openai: 'OpenAI', gemini: 'Gemini', comfyui: 'ComfyUI' };
            const providerLabel = providerLabels[p.provider] || p.provider;
            const modeLabel = p.display_mode === 'overlay' ? '全屏背景' : '分区';
            let detail = `${providerLabel} | ${modeLabel}`;
            if (p.provider === 'openai' && p.openai) detail += ` | ${p.openai.model || 'gpt-image-1'}`;
            if (p.provider === 'gemini' && p.gemini) detail += ` | ${p.gemini.model || ''}`;
            container.innerHTML += `
                <div class="profile-card${p.is_active ? ' is-active' : ''}">
                    <div class="profile-info">
                        <div class="profile-name">${escapeHtml(p.name)}${badge}</div>
                        <div class="profile-detail">${escapeHtml(detail)}</div>
                    </div>
                    <div class="profile-actions">
                        ${!p.is_active ? `<button class="btn-primary" onclick="activateImageProfile('${p.id}')">启用</button>` : ''}
                        <button class="btn-secondary" onclick="editImageProfile('${p.id}')">编辑</button>
                        <button class="btn-secondary" onclick="deleteImageProfile('${p.id}')">删除</button>
                    </div>
                </div>`;
        });
        // Sync localStorage
        if (hasActive) {
            localStorage.setItem('tavern_img_provider_enabled', '1');
        } else {
            localStorage.removeItem('tavern_img_provider_enabled');
        }
        // Sync display mode from active profile
        const active = (profiles || []).find(p => p.is_active);
        if (active) localStorage.setItem('tavern_img_mode', active.display_mode || 'split');
    } catch (e) { console.warn('loadImageProfiles:', e); }
}

async function saveImageProfile() {
    const name = document.getElementById('img-profile-name').value.trim();
    const provider = document.getElementById('img-provider-select').value;
    const display_mode = document.getElementById('img-display-mode').value;
    if (!name) { alert('请填写配置名称'); return; }

    const body = { name, provider, display_mode };
    if (provider === 'openai') {
        body.openai = {
            api_key: document.getElementById('img-openai-key').value,
            base_url: document.getElementById('img-openai-base-url').value,
            model: document.getElementById('img-openai-model').value,
            size: document.getElementById('img-openai-size').value,
            quality: document.getElementById('img-openai-quality').value,
        };
    } else if (provider === 'gemini') {
        body.gemini = {
            api_key: document.getElementById('img-gemini-key').value,
            base_url: document.getElementById('img-gemini-base-url').value,
            model: document.getElementById('img-gemini-model').value,
        };
    } else if (provider === 'comfyui') {
        body.comfyui = {
            base_url: document.getElementById('img-comfyui-url').value,
            width: parseInt(document.getElementById('img-comfyui-width').value) || 1024,
            height: parseInt(document.getElementById('img-comfyui-height').value) || 576,
            steps: parseInt(document.getElementById('img-comfyui-steps').value) || 20,
            checkpoint: document.getElementById('img-comfyui-checkpoint').value,
        };
    }

    try {
        if (_editingImgProfileId) {
            await API.put(`/api/config/image/profiles/${_editingImgProfileId}`, body);
            _showImgStatus('配置已更新', true);
        } else {
            await API.post('/api/config/image/profiles', body);
            _showImgStatus('配置已保存', true);
        }
        resetImgProfileForm();
        loadImageProfiles();
    } catch (e) {
        _showImgStatus('保存失败: ' + e.message, false);
    }
}

async function activateImageProfile(id) {
    try {
        await API.post(`/api/config/image/profiles/${id}/activate`);
        _showImgStatus('已切换', true);
        loadImageProfiles();
    } catch (e) {
        _showImgStatus('切换失败: ' + e.message, false);
    }
}

async function deleteImageProfile(id) {
    if (!confirm('确定删除此图像配置？')) return;
    try {
        await API.del(`/api/config/image/profiles/${id}`);
        loadImageProfiles();
    } catch (e) {
        _showImgStatus('删除失败: ' + e.message, false);
    }
}

async function editImageProfile(id) {
    try {
        const profiles = await API.get('/api/config/image/profiles');
        const p = (profiles || []).find(x => x.id === id);
        if (!p) return;

        _editingImgProfileId = id;
        document.getElementById('img-profile-name').value = p.name || '';
        document.getElementById('img-provider-select').value = p.provider || 'openai';
        document.getElementById('img-display-mode').value = p.display_mode || 'split';

        // Clear sensitive fields first
        document.getElementById('img-openai-key').value = '';
        document.getElementById('img-openai-key').placeholder = p.openai?.has_api_key ? '留空保持不变' : 'sk-...';
        document.getElementById('img-gemini-key').value = '';
        document.getElementById('img-gemini-key').placeholder = p.gemini?.has_api_key ? '留空保持不变' : 'pk-...';

        if (p.openai) {
            document.getElementById('img-openai-base-url').value = p.openai.base_url || '';
            document.getElementById('img-openai-model').value = p.openai.model || 'gpt-image-1';
            document.getElementById('img-openai-size').value = p.openai.size || '1536x1024';
            document.getElementById('img-openai-quality').value = p.openai.quality || 'low';
        }
        if (p.gemini) {
            document.getElementById('img-gemini-base-url').value = p.gemini.base_url || '';
            document.getElementById('img-gemini-model').value = p.gemini.model || 'Gemini 3-Pro-Image-Preview';
        }
        if (p.comfyui) {
            document.getElementById('img-comfyui-url').value = p.comfyui.base_url || 'http://127.0.0.1:8188';
            document.getElementById('img-comfyui-width').value = p.comfyui.width || 1024;
            document.getElementById('img-comfyui-height').value = p.comfyui.height || 576;
            document.getElementById('img-comfyui-steps').value = p.comfyui.steps || 20;
            document.getElementById('img-comfyui-checkpoint').value = p.comfyui.checkpoint || '';
        }
        onImgProviderChange();
        document.getElementById('img-save-btn').textContent = '更新配置';
        document.getElementById('img-form-title').textContent = `编辑配置: ${p.name}`;
        document.getElementById('img-profile-name').scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
        _showImgStatus('加载失败: ' + e.message, false);
    }
}

function resetImgProfileForm() {
    _editingImgProfileId = null;
    document.getElementById('img-profile-name').value = '';
    document.getElementById('img-provider-select').value = 'openai';
    document.getElementById('img-openai-key').value = '';
    document.getElementById('img-openai-key').placeholder = 'sk-...';
    document.getElementById('img-openai-base-url').value = '';
    document.getElementById('img-openai-model').value = 'gpt-image-1';
    document.getElementById('img-openai-size').value = '1536x1024';
    document.getElementById('img-openai-quality').value = 'low';
    document.getElementById('img-gemini-key').value = '';
    document.getElementById('img-gemini-key').placeholder = 'pk-...';
    document.getElementById('img-gemini-base-url').value = '';
    document.getElementById('img-gemini-model').value = 'Gemini 3-Pro-Image-Preview';
    document.getElementById('img-comfyui-url').value = 'http://127.0.0.1:8188';
    document.getElementById('img-comfyui-width').value = '1024';
    document.getElementById('img-comfyui-height').value = '576';
    document.getElementById('img-comfyui-steps').value = '20';
    document.getElementById('img-comfyui-checkpoint').value = '';
    document.getElementById('img-display-mode').value = 'split';
    document.getElementById('img-save-btn').textContent = '保存配置';
    document.getElementById('img-form-title').textContent = '添加新配置';
    onImgProviderChange();
}

// Load image profiles when settings panel loads
document.addEventListener('DOMContentLoaded', () => { loadImageProfiles(); });

/* ──── Generate Scene Image for Current Turn ──── */

async function generateSceneImageForCurrent() {
    if (!currentSaveId || !currentNodeId) return;
    const btn = document.getElementById('btn-gen-scene-img');
    if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
    try {
        const headers = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('tavern_api_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const resp = await fetch(`/api/game/${currentSaveId}/generate-scene-image/${currentNodeId}`, {
            method: 'POST', headers
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        if (data.scene_image_path) {
            renderSceneImage({ url: data.scene_image_path });
        }
    } catch (e) {
        const toast = document.createElement('div');
        toast.className = 'toast-error';
        toast.textContent = `生成场景图失败: ${e.message}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '生成背景图'; }
    }
}

/* ──── Feedback Regeneration ──── */

async function feedbackRegen() {
    if (!currentSaveId) return;
    const feedback = prompt('请描述你对这轮叙事的修改意见：');
    if (!feedback) return;
    const btn = document.getElementById('btn-feedback-regen');
    if (btn) { btn.disabled = true; }
    try {
        const data = await API.post(`/api/game/${currentSaveId}/feedback-regen`, { feedback });
        if (data.ok && data.narrative) {
            // 替换当前叙事文本
            const blocks = document.querySelectorAll('.turn-block.latest .narrative-text, .turn-block:last-child .narrative-text');
            const target = blocks.length ? blocks[blocks.length - 1] : null;
            if (target) {
                target.innerHTML = typeof enhanceNarrative === 'function'
                    ? enhanceNarrative(escapeHtml(data.narrative).replace(/\n/g, '<br>'))
                    : escapeHtml(data.narrative).replace(/\n/g, '<br>');
            }
        } else {
            alert(data.error || '重写失败');
        }
    } catch (e) {
        alert('反馈重写失败: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; }
    }
}

// ================================================
// E1: Agent Trace rendering
// ================================================

function renderAgentTrace(trace) {
    const container = document.getElementById('agent-trace-content');
    if (!container) return;
    if (!trace) {
        container.textContent = '当前存档暂无 Agent 推演记录。完成一次 Agent 行动后可在此查看。';
        return;
    }
    const totalTokens = (trace.total_tokens.prompt || 0) + (trace.total_tokens.completion || 0);
    let html = `<div class="trace-header">T${trace.turn} | ${trace.mode} | ${trace.tools_count} tools | ${totalTokens} tokens | ${trace.duration_ms}ms</div>`;
    if (Number.isFinite(trace.prepare_ms) || Number.isFinite(trace.plan_event_ms) || Number.isFinite(trace.first_text_ms)) {
        const timings = [];
        if (Number.isFinite(trace.plan_event_ms)) timings.push(`计划事件发出 ${trace.plan_event_ms}ms（服务端）`);
        if (Number.isFinite(trace.prepare_ms)) timings.push(`回合准备 ${trace.prepare_ms}ms`);
        if (Number.isFinite(trace.first_draft_ms)) timings.push(`Agent 启动到首稿 ${trace.first_draft_ms}ms`);
        if (Number.isFinite(trace.first_text_ms)) {
            timings.push(`首段总耗时 ${trace.first_text_ms}ms`);
            if (Number.isFinite(trace.first_draft_to_text_ms)) {
                timings.push(`首稿到可见正文 ${trace.first_draft_to_text_ms}ms（含审查、修订或结算等待）`);
            }
            if (Number.isFinite(trace.first_text_active_ms)) {
                timings.push(`扣除计划确认后 ${trace.first_text_active_ms}ms`);
            }
            if (Number.isFinite(trace.prepare_ms)) {
                timings.push(`准备结束到首段 ${Math.max(0, trace.first_text_ms - trace.prepare_ms)}ms（规划可能与准备交叠）`);
            }
        }
        html += `<div class="trace-plan">${timings.join(' | ')}</div>`;
    }
    if (Number.isFinite(trace.route_plan_overlap_ms)) {
        html += `<div class="trace-plan">首轮规划请求与路由/检索交叠 ${trace.route_plan_overlap_ms}ms（并行观测值，非实测提速）</div>`;
    }
    if (Number.isFinite(trace.plan_wait_ms) && trace.plan_wait_ms > 0) {
        html += `<div class="trace-plan">玩家计划确认累计 ${trace.plan_wait_ms}ms（计入总耗时）</div>`;
    }
    const clientTimings = trace.client_timings_ms || {};
    if (Number.isFinite(clientTimings.first_text_event_ms) || Number.isFinite(clientTimings.first_text_paint_ms)) {
        const timings = [];
        if (Number.isFinite(clientTimings.first_text_event_ms)) timings.push(`前端首段事件 ${clientTimings.first_text_event_ms}ms`);
        if (Number.isFinite(clientTimings.first_text_paint_ms)) timings.push(`首段入屏约 ${clientTimings.first_text_paint_ms}ms`);
        if (Number.isFinite(clientTimings.first_text_event_ms) && Number.isFinite(clientTimings.first_text_paint_ms)) {
            timings.push(`事件至入屏约 ${Math.max(0, clientTimings.first_text_paint_ms - clientTimings.first_text_event_ms)}ms`);
        }
        html += `<div class="trace-plan">${timings.join(' | ')}（仅本次页面）</div>`;
    }
    if (Number.isFinite(trace.first_text_model_calls)) {
        html += `<div class="trace-plan">首段前 Agent 模型调用 ${trace.first_text_model_calls} 次</div>`;
    }
    const stages = trace.preflight_timings_ms || {};
    const stageTimings = [
        ['background', '后台'], ['intent', '意图'], ['context', '上下文'],
        ['route', '路由'], ['retrieval', '检索'],
    ].filter(([key]) => Number.isFinite(stages[key]))
        .map(([key, label]) => `${label} ${stages[key]}ms`);
    if (stageTimings.length) {
        html += `<div class="trace-plan">准备拆解 ${stageTimings.join(' / ')}（路由与检索并行）</div>`;
    }
    const retrieval = trace.retrieval_timings_ms || {};
    const retrievalTimings = [
        ['index_wait', '索引等待'], ['vector_query', '历史向量'], ['data_bank_query', '知识库'],
    ].filter(([key]) => Number.isFinite(retrieval[key]))
        .map(([key, label]) => `${label} ${retrieval[key]}ms`);
    if (retrievalTimings.length) {
        html += `<div class="trace-plan">检索拆解 ${retrievalTimings.join(' / ')}</div>`;
    }
    if (Number.isFinite(trace.primary_model_calls)) {
        const preflight = trace.preflight_model_calls || {};
        html += `<div class="trace-plan">主链路模型调用 ${trace.primary_model_calls} 次（意图 ${preflight.intent || 0} / 路由 ${preflight.route || 0} / Agent ${trace.model_calls || 0} / 审查 ${trace.semantic_review_model_calls ?? trace.semantic_review_calls ?? 0}）</div>`;
    }
    if (trace.agent_model_timings_ms?.length) {
        const modelTimings = trace.agent_model_timings_ms.map((ms, index) => {
            const phase = trace.agent_model_phases?.[index];
            const chars = trace.agent_request_chars?.[index];
            const tokens = trace.agent_model_token_usage?.[index];
            const firstOutput = trace.agent_first_output_ms?.[index];
            return `${phase ? `${escapeHtml(String(phase))} ` : ''}${escapeHtml(String(ms))}ms` +
                (Number.isFinite(firstOutput) ? ` · 首输出 ${firstOutput}ms` : '') +
                (Number.isFinite(chars) ? ` · ${chars}字符` : '') +
                (Number.isFinite(tokens?.prompt) && Number.isFinite(tokens?.completion)
                    ? ` · 输入 ${tokens.prompt} / 输出 ${tokens.completion} token`
                    : trace.agent_model_token_usage ? ' · token 用量未知' : '');
        });
        html += `<div class="trace-plan">Agent 轮次耗时 ${modelTimings.join(' / ')}</div>`;
    }
    const requestParts = (trace.agent_request_breakdowns || []).flatMap((breakdown, index) => {
        if (!Number.isFinite(breakdown?.system_chars) || !Number.isFinite(breakdown?.tools_chars) ||
            !Array.isArray(breakdown.messages) || !breakdown.messages.every(message => Number.isFinite(message?.chars))) {
            return [];
        }
        const messageChars = breakdown.messages.reduce((total, message) => total + message.chars, 0);
        const toolResultChars = breakdown.messages.reduce((total, message) =>
            total + (message.role === 'tool' ? message.chars : 0), 0);
        const reasoningChars = breakdown.messages.reduce((total, message) =>
            total + (Number.isFinite(message.reasoning_chars) ? message.reasoning_chars : 0), 0);
        return [`#${index + 1} 系统 ${breakdown.system_chars} / 工具定义 ${breakdown.tools_chars} / 消息 ${messageChars}` +
            `（工具回复 ${toolResultChars} / 推理 ${reasoningChars}）`];
    });
    if (requestParts.length) {
        html += `<div class="trace-plan">请求构成 ${requestParts.join('；')}</div>`;
    }
    if (trace.semantic_review_timings_ms?.length) {
        html += `<div class="trace-plan">叙事审查耗时 ${trace.semantic_review_timings_ms.map(ms => `${ms}ms`).join(' / ')}（与 Agent 可并行）</div>`;
    }
    if (trace.plan_interactions?.length) {
        for (const interaction of trace.plan_interactions) {
            html += `<div class="trace-plan">计划 #${escapeHtml(String(interaction.step))}：${escapeHtml(interaction.plan || '')}</div>`;
            for (const question of interaction.questions || []) {
                const text = typeof question === 'string' ? question :
                    `${question.question || ''}${question.options?.length ? `：${question.options.join(' / ')}` : ''}`;
                html += `<div class="trace-plan">待选择：${escapeHtml(text)}</div>`;
            }
            if (interaction.reply != null) {
                html += `<div class="trace-plan">玩家选择：${escapeHtml(interaction.reply)}</div>`;
            }
        }
    } else if (trace.plan) {
        html += `<div class="trace-plan">${escapeHtml(trace.plan)}</div>`;
    }
    for (const injection of trace.user_injections || []) {
        if (injection.plan_reply) continue;
        html += `<div class="trace-plan">玩家插话（${escapeHtml(String(injection.label))} #${escapeHtml(String(injection.round))}）：${escapeHtml(injection.message)}</div>`;
    }
    const previewCount = Number.isFinite(trace.lore_preview_count) ? trace.lore_preview_count : trace.lore_preview_ids?.length;
    if (Number.isFinite(trace.lore_activated_count) || Number.isFinite(previewCount)) {
        const triggered = Number.isFinite(trace.lore_activated_count) ? `触发 ${trace.lore_activated_count} 条` : '';
        const previewed = Number.isFinite(previewCount) ? `上下文预览 ${previewCount} 条` : '';
        html += `<div class="trace-plan">自动知识：${[triggered, previewed].filter(Boolean).join(' / ')}</div>`;
    }
    if (Number.isFinite(trace.rag_history_hit_count)) {
        html += `<div class="trace-plan">自动检索：历史 ${trace.rag_history_hit_count} 条 / 知识库 ${trace.databank_hit_count || 0} 条 / 剧本知识 ${trace.rag_lore_hit_count || 0} 条</div>`;
    }
    if (trace.lore_candidate_count || trace.lore_candidate_ids?.length) {
        html += `<div class="trace-plan">候选资料（待核实）：${escapeHtml(String(trace.lore_candidate_count ?? trace.lore_candidate_ids.length))} 条</div>`;
    }
    if (Number.isFinite(trace.lore_candidate_reads)) {
        html += `<div class="trace-plan">候选正文已读取：${trace.lore_candidate_reads} 条（按 ID 成功读取，非全部候选必读）</div>`;
    }
    for (const retrieval of trace.lore_retrievals || []) {
        html += `<div class="trace-plan">剧本检索 #${escapeHtml(String(retrieval.step))}：匹配 ${retrieval.hit_count ?? (retrieval.entry_ids || []).length} 条</div>`;
    }
    for (const step of trace.rounds) {
        html += `<div class="trace-step">
            <span class="trace-tool">${escapeHtml(step.tool)}${step.superseded ? '（已撤销）' : ''}${Number.isFinite(step.duration_ms) ? ` · ${step.duration_ms}ms` : ''}</span>
        </div>`;
    }
    container.innerHTML = html;
}

// ================================================
// E3: Performance dashboard rendering
// ================================================

let performanceDashboardRequestId = 0;

async function updatePerformanceDashboard() {
    if (!currentSaveId) return;
    const saveId = currentSaveId;
    const requestId = ++performanceDashboardRequestId;
    try {
        const [perfRes, usageRes] = await Promise.all([
            fetch(`/api/game/${saveId}/performance`),
            fetch(`/api/game/${saveId}/usage-stats`),
        ]);
        const stats = await perfRes.json();
        const usage = await usageRes.json();
        if (currentSaveId !== saveId || requestId !== performanceDashboardRequestId) return;
        const el = document.getElementById('perf-dashboard');
        if (!el) return;
        // Tool usage breakdown
        let toolHtml = '';
        if (stats.tool_usage) {
            const entries = Object.entries(stats.tool_usage).sort((a, b) => b[1] - a[1]);
            toolHtml = entries.map(([name, count]) =>
                `<div class="perf-stat"><span>${name}</span><span>${count}</span></div>`
            ).join('');
        }
        const phaseTimings = [
            ['avg_prepare_ms', '回合准备'],
            ['avg_first_text_active_ms', '首段正文（扣除计划等待）'],
            ['avg_agent_model_ms', 'Agent 模型调用合计'],
            ['avg_semantic_review_ms', '叙事审查合计'],
            ['avg_preflight_retrieval_ms', '准备阶段检索'],
        ].filter(([key]) => Number.isFinite(stats[key]));
        const timingHtml = phaseTimings.length ?
            '<div class="perf-section-label">阶段耗时均值（仅统计有记录回合，阶段可能重叠）</div>'
            + phaseTimings.map(([key, label]) =>
                `<div class="perf-stat"><span>${label}</span><span>${stats[key]}ms</span></div>`
            ).join('') : '';
        // Usage stats (by model & by task)
        let usageHtml = '';
        if (usage && usage.total && usage.total.requests > 0) {
            const t = usage.total;
            usageHtml += `<div class="perf-section-label">Usage (当前加载会话)</div>`;
            usageHtml += `<div class="perf-stat"><span>LLM calls</span><span>${t.requests}</span></div>`;
            usageHtml += `<div class="perf-stat"><span>Input tokens</span><span>${t.input_tokens.toLocaleString()}</span></div>`;
            usageHtml += `<div class="perf-stat"><span>Output tokens</span><span>${t.output_tokens.toLocaleString()}</span></div>`;
            if (t.cache_tokens > 0) {
                usageHtml += `<div class="perf-stat"><span>Cache tokens</span><span>${t.cache_tokens.toLocaleString()}</span></div>`;
                usageHtml += `<div class="perf-stat"><span>Cache hit</span><span>${usage.cache_hit_rate}%</span></div>`;
            }
            usageHtml += `<div class="perf-stat"><span>Cost (${usage.currency || 'USD'})</span><span>$${t.cost.toFixed(4)}</span></div>`;
            if (usage.by_model && Object.keys(usage.by_model).length > 0) {
                usageHtml += `<div class="perf-section-label">By model</div>`;
                for (const [model, m] of Object.entries(usage.by_model)) {
                    usageHtml += `<div class="perf-stat"><span>${model}</span><span>${m.requests}x / $${m.cost.toFixed(4)}</span></div>`;
                }
            }
            if (usage.by_task && Object.keys(usage.by_task).length > 0) {
                usageHtml += `<div class="perf-section-label">By task</div>`;
                for (const [task, tk] of Object.entries(usage.by_task)) {
                    usageHtml += `<div class="perf-stat"><span>${task}</span><span>${tk.requests}x / ${(tk.input_tokens + tk.output_tokens).toLocaleString()} tok</span></div>`;
                }
            }
        }
        el.innerHTML = `
            <div class="perf-stat"><span>Turns</span><span>${stats.total_turns || 0}</span></div>
            <div class="perf-stat"><span>Tokens</span><span>${stats.total_tokens || 0}</span></div>
            <div class="perf-stat"><span>平均 Agent 阶段耗时（含选择等待）</span><span>${stats.avg_latency_ms || 0}ms</span></div>
            <div class="perf-stat"><span>平均 Agent 阶段耗时（扣除选择等待）</span><span>${stats.avg_active_latency_ms || 0}ms</span></div>
            ${timingHtml}
            <div class="perf-stat"><span>Avg narrative</span><span>${stats.avg_narrative_length || 0}</span></div>
            ${toolHtml ? '<div class="perf-section-label">Tool usage</div>' + toolHtml : ''}
            ${usageHtml}
        `;
    } catch (_) {
        if (currentSaveId === saveId && requestId === performanceDashboardRequestId) {
            const panel = document.getElementById('perf-dashboard');
            if (panel) panel.textContent = '当前存档统计暂不可用。';
        }
    }
}
