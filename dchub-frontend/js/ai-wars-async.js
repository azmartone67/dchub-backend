/**
 * AI Wars Async Battle Submission — DC Hub
 * ==========================================
 * Drop-in JS for ai-wars.html. Handles:
 *   1. Submit challenge → POST /api/v1/ai-wars/submit-challenge (returns 202 immediately)
 *   2. Poll /api/v1/ai-wars/battle-status/<queue_id> every 3s
 *   3. Show progress UI (queued → running → completed/failed)
 *   4. Display results when battle completes
 *
 * Usage in ai-wars.html:
 *   <script src="/js/ai-wars-async.js"></script>
 *   Then call: submitChallenge(question, email?)
 *   Or wire to form: <button onclick="submitFromForm()">Submit Challenge</button>
 *
 * Requires: a container with id="battle-status-container" for status UI
 */

(function () {
    'use strict';

    const API_BASE = 'https://dchub.cloud';
    const POLL_INTERVAL_MS = 3000;   // Poll every 3 seconds
    const MAX_POLLS = 60;            // Give up after 3 minutes (60 * 3s)

    // ─── Status UI ───

    function getOrCreateStatusContainer() {
        let el = document.getElementById('battle-status-container');
        if (!el) {
            el = document.createElement('div');
            el.id = 'battle-status-container';
            el.style.cssText = 'margin:1.5rem 0;display:none;';
            // Try to insert after the challenge form, or at top of main content
            const form = document.getElementById('challenge-form') || document.querySelector('.challenge-section');
            if (form) {
                form.parentNode.insertBefore(el, form.nextSibling);
            } else {
                const main = document.querySelector('main') || document.body;
                main.prepend(el);
            }
        }
        return el;
    }

    function showStatus(html, state) {
        const container = getOrCreateStatusContainer();
        container.style.display = 'block';

        const colors = {
            queued:    { bg: 'rgba(59,130,246,0.08)', border: '#3b82f6', icon: '⏳' },
            running:   { bg: 'rgba(245,158,11,0.08)', border: '#f59e0b', icon: '⚔️' },
            completed: { bg: 'rgba(16,185,129,0.08)', border: '#10b981', icon: '🏆' },
            failed:    { bg: 'rgba(239,68,68,0.08)',  border: '#ef4444', icon: '❌' },
        };
        const c = colors[state] || colors.queued;

        container.innerHTML = `
            <div style="background:${c.bg};border:1px solid ${c.border};border-radius:12px;padding:1.25rem 1.5rem;font-family:'Instrument Sans',system-ui,sans-serif;">
                ${html}
            </div>
        `;
    }

    function showQueued(queueId) {
        showStatus(`
            <div style="display:flex;align-items:center;gap:0.75rem;">
                <div class="battle-spinner" style="width:20px;height:20px;border:2px solid rgba(59,130,246,0.3);border-top:2px solid #3b82f6;border-radius:50%;animation:spin 1s linear infinite;"></div>
                <div>
                    <div style="font-weight:600;color:#e2e8f0;font-size:0.95rem;">Battle Queued</div>
                    <div style="color:#94a3b8;font-size:0.8rem;margin-top:2px;">Preparing battle arena… All platforms will compete shortly.</div>
                </div>
            </div>
            <style>@keyframes spin{to{transform:rotate(360deg)}}</style>
        `, 'queued');
    }

    function showRunning(queueId) {
        showStatus(`
            <div style="display:flex;align-items:center;gap:0.75rem;">
                <div style="font-size:1.5rem;">⚔️</div>
                <div>
                    <div style="font-weight:600;color:#e2e8f0;font-size:0.95rem;">Battle in Progress</div>
                    <div style="color:#94a3b8;font-size:0.8rem;margin-top:2px;">AI platforms are analyzing your question using DC Hub data…</div>
                </div>
            </div>
            <div style="margin-top:0.75rem;height:4px;background:rgba(245,158,11,0.15);border-radius:2px;overflow:hidden;">
                <div style="width:60%;height:100%;background:#f59e0b;border-radius:2px;animation:pulse 1.5s ease-in-out infinite;"></div>
            </div>
            <style>@keyframes pulse{0%,100%{opacity:0.6}50%{opacity:1}}</style>
        `, 'running');
    }

    function showCompleted(data) {
        const battle = data.battle || {};
        const results = battle.results || [];
        const winner = battle.winner || 'Unknown';

        let resultsHtml = results.map((r, i) => {
            const isWinner = r.platform === winner;
            const badges = [];
            if (isWinner) badges.push('<span style="background:#10b981;color:#000;padding:1px 6px;border-radius:4px;font-size:0.7rem;font-weight:600;">WINNER</span>');
            if (r.had_real_response) badges.push('<span style="background:#3b82f6;color:#fff;padding:1px 6px;border-radius:4px;font-size:0.7rem;">LIVE</span>');
            if (r.used_mcp) badges.push('<span style="background:#8b5cf6;color:#fff;padding:1px 6px;border-radius:4px;font-size:0.7rem;">MCP</span>');

            return `
                <div style="display:flex;justify-content:space-between;align-items:center;padding:0.5rem 0;${i < results.length - 1 ? 'border-bottom:1px solid rgba(255,255,255,0.06);' : ''}">
                    <div style="display:flex;align-items:center;gap:0.5rem;">
                        <span style="color:${isWinner ? '#10b981' : '#94a3b8'};font-weight:${isWinner ? '700' : '400'};font-size:0.9rem;">${r.platform}</span>
                        <span style="display:flex;gap:3px;">${badges.join('')}</span>
                    </div>
                    <span style="font-variant-numeric:tabular-nums;color:${isWinner ? '#10b981' : '#e2e8f0'};font-weight:600;font-size:0.9rem;">${r.overall}</span>
                </div>
            `;
        }).join('');

        showStatus(`
            <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem;">
                <div style="font-size:1.5rem;">🏆</div>
                <div>
                    <div style="font-weight:600;color:#10b981;font-size:0.95rem;">Battle Complete!</div>
                    <div style="color:#94a3b8;font-size:0.8rem;margin-top:2px;">Winner: <strong style="color:#e2e8f0;">${winner}</strong> ${battle.battle_id ? '— <a href="/ai-wars#' + battle.battle_id + '" style="color:#3b82f6;">View Details →</a>' : ''}</div>
                </div>
            </div>
            <div style="margin-top:0.5rem;">${resultsHtml}</div>
        `, 'completed');
    }

    function showFailed(error) {
        showStatus(`
            <div style="display:flex;align-items:center;gap:0.75rem;">
                <div style="font-size:1.5rem;">❌</div>
                <div>
                    <div style="font-weight:600;color:#ef4444;font-size:0.95rem;">Battle Failed</div>
                    <div style="color:#94a3b8;font-size:0.8rem;margin-top:2px;">${error || 'Something went wrong. Try again.'}</div>
                </div>
            </div>
        `, 'failed');
    }

    // ─── Polling ───

    async function pollBattleStatus(queueId, pollCount) {
        if (pollCount >= MAX_POLLS) {
            showFailed('Battle timed out — it may still be running. Check back on the AI Wars page.');
            return;
        }

        try {
            const resp = await fetch(`${API_BASE}/api/v1/ai-wars/battle-status/${queueId}`);
            const data = await resp.json();

            if (!data.success) {
                showFailed(data.error || 'Could not check battle status');
                return;
            }

            switch (data.status) {
                case 'queued':
                    showQueued(queueId);
                    setTimeout(() => pollBattleStatus(queueId, pollCount + 1), POLL_INTERVAL_MS);
                    break;
                case 'running':
                    showRunning(queueId);
                    setTimeout(() => pollBattleStatus(queueId, pollCount + 1), POLL_INTERVAL_MS);
                    break;
                case 'completed':
                    showCompleted(data);
                    break;
                case 'failed':
                    showFailed(data.error || 'Battle execution failed');
                    break;
                default:
                    setTimeout(() => pollBattleStatus(queueId, pollCount + 1), POLL_INTERVAL_MS);
            }
        } catch (err) {
            console.error('Poll error:', err);
            // Network error — retry
            if (pollCount < MAX_POLLS) {
                setTimeout(() => pollBattleStatus(queueId, pollCount + 1), POLL_INTERVAL_MS * 2);
            } else {
                showFailed('Network error while checking battle status');
            }
        }
    }

    // ─── Submit ───

    async function submitChallenge(question, email, category) {
        if (!question || question.trim().length < 10) {
            showFailed('Question must be at least 10 characters');
            return;
        }

        showQueued(null);

        try {
            const resp = await fetch(`${API_BASE}/api/v1/ai-wars/submit-challenge`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question: question.trim(),
                    email: email || '',
                    category: category || 'stump-the-ai',
                }),
            });

            const data = await resp.json();

            if (!data.success) {
                showFailed(data.error || 'Submission failed');
                return;
            }

            const queueId = data.queue_id;
            if (!queueId) {
                // Legacy sync response — show result directly
                if (data.battle) {
                    showCompleted(data);
                } else {
                    showFailed('No queue ID returned');
                }
                return;
            }

            // Start polling
            showQueued(queueId);
            setTimeout(() => pollBattleStatus(queueId, 0), POLL_INTERVAL_MS);

        } catch (err) {
            console.error('Submit error:', err);
            showFailed('Could not reach DC Hub. Please try again.');
        }
    }

    // ─── Form helper ───

    function submitFromForm() {
        const questionEl = document.getElementById('challenge-question') || document.querySelector('[name="question"]');
        const emailEl = document.getElementById('challenge-email') || document.querySelector('[name="email"]');
        const categoryEl = document.getElementById('challenge-category') || document.querySelector('[name="category"]');

        if (!questionEl || !questionEl.value.trim()) {
            alert('Please enter a question');
            return;
        }

        submitChallenge(
            questionEl.value,
            emailEl ? emailEl.value : '',
            categoryEl ? categoryEl.value : 'stump-the-ai'
        );
    }

    // ─── Expose globally ───
    window.submitChallenge = submitChallenge;
    window.submitFromForm = submitFromForm;

})();
