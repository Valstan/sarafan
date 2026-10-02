// region_ai_batch.js — human-in-the-loop AI categorisation через clipboard.
//
// Flow:
//   1. На загрузке: status + chunk 0.
//   2. Юзер копирует prompt → вставляет в ChatGPT/Claude.ai → копирует ответ.
//   3. Apply: парсим JSON (с regex-fallback на «json [...]» оформление),
//      POST /api/discovery/regions/{code}/ai-batch/apply.
//   4. На success — автоматически грузим следующий чанк.

(function () {
    'use strict';

    const code = window.REGION_CODE;
    if (!code) { console.error('REGION_CODE not set'); return; }

    // DOM
    const statusSummary = document.getElementById('status-summary');
    const statusDetail = document.getElementById('status-detail');
    const progressBar = document.getElementById('progress-bar');
    const chunkPos = document.getElementById('chunk-pos');
    const chunkPrev = document.getElementById('chunk-prev');
    const chunkNext = document.getElementById('chunk-next');
    const chunkReload = document.getElementById('chunk-reload');
    const promptText = document.getElementById('prompt-text');
    const promptCopy = document.getElementById('prompt-copy');
    const promptInfo = document.getElementById('prompt-info');
    const responseText = document.getElementById('response-text');
    const responseError = document.getElementById('response-error');
    const responseApply = document.getElementById('response-apply');
    const applyStatus = document.getElementById('apply-status');

    let chunkIndex = 0;
    let chunksTotal = 0;
    let currentChunkSize = 0;

    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    // ─── Status / progress ────────────────────────────────────────
    async function refreshStatus() {
        try {
            const resp = await fetch(`/api/discovery/regions/${encodeURIComponent(code)}/ai-batch/status`);
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
            const pct = data.total > 0 ? Math.round(100 * data.processed / data.total) : 0;
            progressBar.style.width = pct + '%';
            statusSummary.textContent = `Обработано ${data.processed} из ${data.total} pending-кандидатов`;
            statusDetail.textContent = data.remaining > 0
                ? ` · осталось ${data.remaining}`
                : ' · готово ✓';
            const modeLabels = {auto: 'авто', manual: 'ручной', off: 'выкл'};
            statusDetail.textContent += ` · режим: ${modeLabels[data.mode] || data.mode || '?'}`;
        } catch (e) {
            statusSummary.textContent = `Ошибка статуса: ${escapeHtml(e.message)}`;
        }
    }

    // ─── Chunk load ───────────────────────────────────────────────
    async function loadChunk(idx) {
        chunkIndex = Math.max(0, idx | 0);
        promptText.value = '';
        promptInfo.textContent = '';
        responseText.value = '';
        responseError.textContent = '';
        applyStatus.textContent = '';
        try {
            const resp = await fetch(
                `/api/discovery/regions/${encodeURIComponent(code)}/ai-batch` +
                `?chunk=${chunkIndex}`
            );
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
            chunksTotal = data.chunks_total | 0;
            currentChunkSize = (data.items || []).length;
            if (!currentChunkSize) {
                promptText.value = '';
                promptInfo.innerHTML = chunksTotal === 0
                    ? '<span class="text-success">✓ Нет некатегоризированных pending-кандидатов в этом регионе</span>'
                    : '<span class="text-muted">Этот чанк пуст (выйди за пределы или регион уже обработан)</span>';
                chunkPos.textContent = chunksTotal === 0 ? 'Чанк —' : `Чанк ${chunkIndex + 1} / ${chunksTotal}`;
            } else {
                promptText.value = data.prompt;
                promptInfo.innerHTML = `${currentChunkSize} кандидатов в чанке`;
                chunkPos.textContent = `Чанк ${chunkIndex + 1} / ${chunksTotal}`;
            }
            chunkPrev.disabled = chunkIndex <= 0;
            chunkNext.disabled = chunksTotal === 0 || chunkIndex + 1 >= chunksTotal;
        } catch (e) {
            promptInfo.innerHTML = `<span class="text-danger">Ошибка: ${escapeHtml(e.message)}</span>`;
        }
    }

    // ─── Clipboard ────────────────────────────────────────────────
    promptCopy.addEventListener('click', async () => {
        if (!promptText.value) return;
        try {
            await navigator.clipboard.writeText(promptText.value);
            const orig = promptCopy.innerHTML;
            promptCopy.innerHTML = '<i class="bi bi-check2"></i> Скопировано';
            promptCopy.classList.add('btn-success');
            promptCopy.classList.remove('btn-outline-primary');
            setTimeout(() => {
                promptCopy.innerHTML = orig;
                promptCopy.classList.remove('btn-success');
                promptCopy.classList.add('btn-outline-primary');
            }, 1500);
        } catch (e) {
            alert('Не удалось скопировать: ' + e.message);
        }
    });

    // ─── Robust JSON parsing ──────────────────────────────────────
    function parseLLMResponse(raw) {
        const text = (raw || '').trim();
        if (!text) return {ok: false, error: 'Пусто'};
        // Strip markdown ```json ... ``` обёртку.
        let stripped = text.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/i, '').trim();
        // Прямая попытка.
        try {
            const parsed = JSON.parse(stripped);
            if (Array.isArray(parsed)) return {ok: true, items: parsed};
            if (parsed && Array.isArray(parsed.items)) return {ok: true, items: parsed.items};
        } catch (_e) { /* fallback ниже */ }
        // Найти первый [...] блок.
        const match = stripped.match(/\[[\s\S]*\]/);
        if (match) {
            try {
                const parsed = JSON.parse(match[0]);
                if (Array.isArray(parsed)) return {ok: true, items: parsed};
            } catch (e) {
                return {ok: false, error: `JSON parse: ${e.message}`};
            }
        }
        return {ok: false, error: 'JSON-массив не найден в ответе'};
    }

    // ─── Apply ────────────────────────────────────────────────────
    responseApply.addEventListener('click', async () => {
        responseError.textContent = '';
        applyStatus.textContent = '';
        const parsed = parseLLMResponse(responseText.value);
        if (!parsed.ok) {
            responseError.textContent = `❌ ${parsed.error}. Проверь, что ответ — валидный JSON-массив без markdown-обёртки.`;
            return;
        }
        if (!parsed.items.length) {
            responseError.textContent = '❌ JSON-массив пустой';
            return;
        }
        responseApply.disabled = true;
        applyStatus.innerHTML = '<span class="text-muted"><span class="spinner-border spinner-border-sm me-1"></span>Применяю…</span>';
        try {
            const resp = await fetch(
                `/api/discovery/regions/${encodeURIComponent(code)}/ai-batch/apply`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({items: parsed.items}),
                }
            );
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
            applyStatus.innerHTML = `<span class="text-success">✓ Обновлено: ${data.updated}, релевантных: ${data.summary.relevant}, нерелевантных: ${data.summary.irrelevant}` +
                (data.skipped ? `, пропущено (не pending): ${data.skipped}` : '') +
                (data.missing_ids && data.missing_ids.length ? `, не найдено id: ${data.missing_ids.length}` : '') +
                '</span>';
            await refreshStatus();
            // Авто-загрузка следующего чанка (юзер хочет идти дальше).
            setTimeout(async () => {
                // После apply'а БД изменилась — чанки могут сместиться.
                // Перезагружаем индекс 0, чтобы захватить новый набор pending.
                await loadChunk(0);
            }, 1200);
        } catch (e) {
            applyStatus.innerHTML = `<span class="text-danger">❌ ${escapeHtml(e.message)}</span>`;
        } finally {
            responseApply.disabled = false;
        }
    });

    // ─── Chunk nav ────────────────────────────────────────────────
    chunkPrev.addEventListener('click', () => loadChunk(chunkIndex - 1));
    chunkNext.addEventListener('click', () => loadChunk(chunkIndex + 1));
    chunkReload.addEventListener('click', () => loadChunk(chunkIndex));

    // Kickoff.
    refreshStatus();
    loadChunk(0);
})();
