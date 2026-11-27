// Публичный лендинг /regions/links — список сообществ сети + вкладка рекламы.
// Данные приходят из GET /api/regions/vk-links (публичный эндпоинт). Формат
// строки собирает сервер (modules/region_links.py) и кладёт в item.line —
// клиент только меняет ПОРЯДОК строк и склеивает их, но не пересобирает формат.

let rlData = null;                  // ответ API целиком
let rlItems = [];                   // плоский список сообществ
// Дефолт — «по подписчикам» (заказ владельца 2026-08-27). Класс active стоит на
// той же кнопке в region_links.html: JS разметку не читает, синхронность ручная.
let rlSort = 'members';             // alpha | members | neighbors
let rlAnchor = null;                // код опорного района для сортировки по соседству
const rlPicked = new Set();         // коды отмеченных галочками сообществ

// ── Вкладки ────────────────────────────────────────────────────────────────

function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('show'));
            btn.classList.add('active');
            document.getElementById('pane-' + btn.dataset.tab).classList.add('show');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    });
}

// ── Загрузка ───────────────────────────────────────────────────────────────

async function loadRegionLinks() {
    const status = document.getElementById('rl-status');
    const container = document.getElementById('rl-blocks');
    status.className = 'loading';
    status.textContent = 'Загружаем сеть…';
    container.innerHTML = '';
    try {
        const resp = await fetch('/api/regions/vk-links');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        rlData = await resp.json();
    } catch (e) {
        status.className = 'error-box';
        status.textContent = 'Не удалось загрузить список: ' + e.message;
        return;
    }
    status.textContent = '';
    status.className = '';
    rlItems = rlData.blocks.flatMap(b => b.items.map(i => ({ ...i, block: b.title })));
    renderStats(rlData);
    fillAnchorOptions();
    render();
}

function fmt(n) {
    return (n === null || n === undefined) ? '' : n.toLocaleString('ru-RU');
}

function renderStats(data) {
    document.getElementById('stat-groups').textContent = fmt(data.total);
    document.getElementById('stat-members').textContent =
        data.total_members ? fmt(data.total_members) : '—';
    renderGrowth(data.growth);
}

// ── Прирост подписчиков ────────────────────────────────────────────────────
// Считает сервер (modules/network_growth.py): и числа, и подписи окон
// приезжают готовыми — правило «сколько истории накоплено» не должно жить в
// двух местах. Клиент только раскладывает их по плиткам и столбикам.

function signed(n) {
    if (n === null || n === undefined) return '—';
    return (n < 0 ? '−' : '+') + fmt(Math.abs(n));
}

function deltaClass(delta) {
    if (delta === null || delta === undefined) return ' is-empty';
    if (delta < 0) return ' is-negative';
    return delta === 0 ? ' is-flat' : '';
}

function renderGrowth(growth) {
    // Плитки пересобираются целиком: «⟳ Обновить» зовёт renderStats повторно,
    // и без очистки они бы копились.
    document.querySelectorAll('#hero-stats .stat--growth').forEach(el => el.remove());
    const monthsBox = document.getElementById('growth-months');
    const staleBox = document.getElementById('stat-stale');
    staleBox.hidden = true;
    staleBox.textContent = '';
    if (!growth) {
        // Меньше двух дней снимков: показывать «+0» значило бы выдать
        // отсутствие данных за отсутствие роста.
        monthsBox.hidden = true;
        return;
    }

    const host = document.getElementById('hero-stats');
    const anchor = host.querySelector('.stat--always');
    (growth.windows || []).forEach(w => {
        const tile = document.createElement('div');
        tile.className = 'stat stat--growth' + deltaClass(w.delta);
        tile.innerHTML =
            `<b>${signed(w.delta)}</b><span>${escapeHtml(w.title)}</span>` +
            (w.note ? `<em class="stat-note">${escapeHtml(w.note)}</em>` : '');
        host.insertBefore(tile, anchor);
    });

    if (growth.stale_days > 1) {
        staleBox.textContent =
            `⚠️ Числа на ${growth.latest_date_human}: ночной сбор не обновлялся ` +
            `${growth.stale_days} ${plural(growth.stale_days, 'день', 'дня', 'дней')}.`;
        staleBox.hidden = false;
    }

    renderGrowthMonths(growth);
}

function renderGrowthMonths(growth) {
    const box = document.getElementById('growth-months');
    const grid = document.getElementById('gm-grid');
    const months = (growth.months || []).filter(m => m.delta !== null && m.delta !== undefined);
    if (!months.length) {
        box.hidden = true;
        return;
    }
    // Столбик — доля от самого большого месяца по модулю: полоса сравнивает
    // месяцы между собой, а не с абсолютной шкалой.
    const maxAbs = Math.max(1, ...months.map(m => Math.abs(m.delta)));
    grid.innerHTML = (growth.months || []).map(m => {
        const has = m.delta !== null && m.delta !== undefined;
        const width = has ? Math.max(2, Math.round(Math.abs(m.delta) / maxAbs * 100)) : 0;
        return `
        <div class="gm-month${m.current ? ' is-current' : ''}${deltaClass(m.delta)}">
            <div class="gm-name">${escapeHtml(m.title)}</div>
            <div class="gm-value">${has ? signed(m.delta) : '—'}</div>
            <div class="gm-bar"><i style="width:${width}%"></i></div>
            <div class="gm-note">${escapeHtml(has ? (m.note || 'подписчиков за месяц') : 'снимков ещё не было')}</div>
        </div>`;
    }).join('');
    document.getElementById('gm-hint').textContent =
        `наблюдаем с ${growth.first_date_human}, счёт обновляется каждую ночь`;
    box.hidden = false;
}

// Выпадашка «мой район» — только районы (области опорной точкой не бывают).
function fillAnchorOptions() {
    const sel = document.getElementById('rl-anchor');
    const raions = rlItems.filter(i => i.kind !== 'oblast');
    sel.innerHTML = raions
        .map(i => `<option value="${i.code}">${escapeHtml(i.name.replace(/ ИНФО$/, ''))}</option>`)
        .join('');
    if (!rlAnchor && raions.length) rlAnchor = raions[0].code;
    if (rlAnchor) sel.value = rlAnchor;
}

// ── Сортировка по соседству: кольца BFS по графу соседей ──────────────────
// Граф приходит с сервера по ВСЕМ регионам, включая ещё не запущенные: они
// транзитные узлы, без них цепочка соседства рвётся (Луза → Мураши → …).

function neighborRings(anchorCode) {
    const graph = (rlData && rlData.neighbors) || {};
    const depth = { [anchorCode]: 0 };
    let frontier = [anchorCode];
    while (frontier.length) {
        const next = [];
        for (const code of frontier) {
            for (const nb of (graph[code] || [])) {
                if (depth[nb] === undefined) {
                    depth[nb] = depth[code] + 1;
                    next.push(nb);
                }
            }
        }
        frontier = next;
    }
    return depth;
}

const RING_TITLES = [
    'Ваш район',
    'Соседние районы — граничат с вами',
    'Через один район',
    'Через два района',
];

// Областные группы соседей не имеют (они «над» районами) — им своя строка,
// иначе они молча падают в «остальные», хотя это самый широкий охват.
const OBLAST_RING = 'Областные сообщества — охват всей области разом';

function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
    return many;
}

function ringTitle(item, depth) {
    if (item.kind === 'oblast') return OBLAST_RING;
    const d = depth[item.code];
    if (d === undefined) return 'Остальные сообщества сети';
    return RING_TITLES[d] || `Дальше по сети — ${d} ${plural(d, 'район', 'района', 'районов')} от вас`;
}

// Порядок групп: кольца по возрастанию, затем несвязанные, областные — в конце.
function ringOrder(item, depth) {
    if (item.kind === 'oblast') return 10000;
    const d = depth[item.code];
    return d === undefined ? 9000 : d;
}

// ── Рендер ─────────────────────────────────────────────────────────────────

function render() {
    const container = document.getElementById('rl-blocks');
    if (!rlItems.length) {
        container.innerHTML = '<div class="error-box">Список пока пуст.</div>';
        return;
    }
    // Две колонки — только там, где блок = область. «По соседству» рендерит
    // один плоский список, который читается сверху вниз как «ближе → дальше»:
    // разложи его по колонкам — и порядок, ради которого режим существует,
    // сломается.
    container.classList.toggle('cols', rlSort !== 'neighbors');
    if (rlSort === 'neighbors') {
        renderByNeighbors(container);
    } else {
        renderByBlocks(container);
    }
    bindRowHandlers(container);
    updateSelbar();
}

function blockMembers(block) {
    return block.items.reduce((sum, i) => sum + (i.members || 0), 0);
}

// ЕДИНСТВЕННОЕ место, где вычисляется порядок для режимов alpha/members.
// Раньше тот же компаратор был выписан второй раз в currentFullText, и любое
// расхождение всплывало не на экране, а уже в тексте, вставленном в VK.
function orderedBlocks() {
    const blocks = rlData.blocks.map(b => ({ ...b, items: b.items.slice() }));
    if (rlSort !== 'members') return blocks;
    blocks.forEach(b => b.items.sort((x, y) => (y.members || 0) - (x.members || 0)));
    // Блоки — тоже по убыванию охвата, чтобы Кировская область оказалась в
    // левой колонке не «по счастливому алфавиту», а по тому же правилу, что и
    // строки внутри. «Другие» (районы без области) остаются в конце.
    blocks.sort((a, b) => {
        if (a.code === '_other') return 1;
        if (b.code === '_other') return -1;
        return blockMembers(b) - blockMembers(a);
    });
    return blocks;
}

// Алфавит и подписчики: сохраняем разбивку по областям.
function renderByBlocks(container) {
    const blocks = orderedBlocks();
    container.innerHTML = blocks.map((block, idx) => `
        <div class="oblast-card">
            <div class="oblast-head">
                <h3>${escapeHtml(block.title)}</h3>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <label class="head-check">
                        <input type="checkbox" class="block-check" data-block="${idx}">
                        выбрать все
                    </label>
                    <span class="count">${block.items.length} ${plural(block.items.length, 'сообщество', 'сообщества', 'сообществ')}</span>
                    <button class="btn btn-ghost rl-copy-block" data-block="${idx}">⎘ Копировать блок</button>
                </div>
            </div>
            <ul class="community-list">
                ${block.items.map(item => rowHtml(item)).join('')}
            </ul>
        </div>
    `).join('');
    // Кнопки «копировать блок» берут ТЕКУЩИЙ порядок, а не серверный block.text.
    container.querySelectorAll('.rl-copy-block').forEach(btn => {
        btn.addEventListener('click', () => {
            const block = blocks[parseInt(btn.dataset.block, 10)];
            const text = [block.title + ':'].concat(block.items.map(i => i.line)).join('\n');
            copyToClipboard(text, btn, '⎘ Копировать блок');
        });
    });
    container.querySelectorAll('.block-check').forEach(chk => {
        chk.addEventListener('change', () => {
            const block = blocks[parseInt(chk.dataset.block, 10)];
            block.items.forEach(i => chk.checked ? rlPicked.add(i.code) : rlPicked.delete(i.code));
            render();
        });
    });
}

function sortedByNeighbors(depth) {
    return rlItems.slice().sort((a, b) => {
        const fa = ringOrder(a, depth), fb = ringOrder(b, depth);
        if (fa !== fb) return fa - fb;
        return sortKey(a.name).localeCompare(sortKey(b.name), 'ru');
    });
}

// Соседство: плоский список кольцами от выбранного района.
function renderByNeighbors(container) {
    const depth = neighborRings(rlAnchor);
    const groups = new Map();
    sortedByNeighbors(depth).forEach(item => {
        const key = ringTitle(item, depth);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
    });

    container.innerHTML = `
        <div class="oblast-card">
            <div class="oblast-head">
                <h3>От ближних к дальним</h3>
                <span class="count">${rlItems.length} ${plural(rlItems.length, 'сообщество', 'сообщества', 'сообществ')} · опора: ${escapeHtml(
                    (rlItems.find(i => i.code === rlAnchor) || {}).name || '—'
                )}</span>
            </div>
            ${[...groups.entries()].map(([title, items]) => `
                <div class="ring-head">${escapeHtml(title)} · ${items.length}</div>
                <ul class="community-list">
                    ${items.map(item => rowHtml(item, true)).join('')}
                </ul>
            `).join('')}
        </div>
    `;
}

// Строка списка: кликабельно САМО ИМЯ сообщества (заказ владельца 2026-08-27) —
// адрес в строке больше не пишем, он занимал полстроки и ронял длинные названия
// на второй ряд. В ТЕКСТЕ для копирования (item.line, собран сервером) ссылка
// остаётся отдельным токеном: VK делает кликабельным только голый URL.
function rowHtml(item, showOblast) {
    const picked = rlPicked.has(item.code);
    return `
        <li class="${item.kind === 'oblast' ? 'oblast-row' : ''} ${picked ? 'picked' : ''}">
            <input type="checkbox" class="c-check" data-code="${escapeHtml(item.code)}" ${picked ? 'checked' : ''}
                   title="Выбрать для заказа рекламы">
            <a class="c-name" href="${escapeHtml(item.url)}" target="_blank" rel="noopener"
               title="Открыть ${escapeHtml(item.name)} во ВКонтакте">${escapeHtml(item.name)}</a>
            ${item.members !== null ? `<span class="c-dash">—</span><span class="c-members">${fmt(item.members)}</span>` : ''}
            ${showOblast && item.oblast ? `<span class="c-oblast">${escapeHtml(item.oblast)}</span>` : ''}
        </li>
    `;
}

function bindRowHandlers(container) {
    container.querySelectorAll('.c-check').forEach(chk => {
        chk.addEventListener('change', () => {
            const code = chk.dataset.code;
            if (chk.checked) rlPicked.add(code); else rlPicked.delete(code);
            chk.closest('li').classList.toggle('picked', chk.checked);
            updateSelbar();
        });
    });
}

function sortKey(s) { return s.toLowerCase().replace(/ё/g, 'е'); }

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : text;
    return div.innerHTML;
}

// ── Панель выбора ──────────────────────────────────────────────────────────

function pickedItems() {
    return rlItems.filter(i => rlPicked.has(i.code));
}

// Таблица цен приходит с сервера (config/ad_landing.py): правило скидок живёт
// в одном месте и покрыто тестами, клиент только показывает готовое число.
let priceTable = null;

function loadPriceTable() {
    if (priceTable) return priceTable;
    try {
        priceTable = JSON.parse(document.getElementById('price-table').textContent);
    } catch (e) {
        priceTable = [];
    }
    return priceTable;
}

function quote(n) {
    const table = loadPriceTable();
    if (!table.length || n < 1) return null;
    // Сверх таблицы цена уже упёрлась в потолок — берём последнюю строку.
    return table[Math.min(n, table.length) - 1];
}

// Подсказка «доберите до рубежа»: на 5-м и 10-м сообществе цена сбрасывается
// до пакетной, поэтому 9 штук дороже 10. Молчать об этом нечестно — ищем
// ближайший шаг вперёд, где итог становится дешевле или бесплатно шире.
function ladderNudge(n) {
    const table = loadPriceTable();
    const now = quote(n);
    if (!now || !table.length) return null;

    // Уже на потолке «всей сети» — оставшиеся сообщества ничего не стоят.
    if (now.capped && rlItems.length > n) {
        return { kind: 'free', rest: rlItems.length - n };
    }
    const limit = Math.min(table.length, rlItems.length || table.length);
    for (let k = n + 1; k <= Math.min(n + 6, limit); k++) {
        const next = quote(k);
        if (!next) break;
        if (next.price < now.price) {
            return { kind: 'cheaper', add: k - n, diff: now.price - next.price, price: next.price };
        }
        if (next.capped) {
            // Следующий шаг упирается в потолок: дальше добор ничего не стоит.
            return { kind: 'reaches-cap', add: k - n, extra: next.price - now.price };
        }
    }
    return null;
}

function updateSelbar() {
    const bar = document.getElementById('rl-selbar');
    const items = pickedItems();
    if (!items.length) {
        bar.hidden = true;
        updateOrderSummary();
        return;
    }
    bar.hidden = false;
    const reach = items.reduce((s, i) => s + (i.members || 0), 0);
    document.getElementById('sel-n').textContent = fmt(items.length);
    document.getElementById('sel-reach').textContent = fmt(reach);
    const q = quote(items.length);
    const box = document.getElementById('sel-price');
    if (!q) {
        box.innerHTML = '';
        return;
    }
    const nudge = ladderNudge(items.length);
    box.innerHTML =
        `≈ ${fmt(q.price)} ₽` +
        (q.saved
            ? `<span class="saved">скидка ${fmt(q.saved)} ₽ · −${q.percent}%</span>`
            : '') +
        `<span class="hint">(${escapeHtml(q.anchor || '')}; точную цену подтвердим при заказе)</span>` +
        nudgeHtml(nudge);
    updateOrderSummary();
}

function nudgeHtml(nudge) {
    if (!nudge) return '';
    if (nudge.kind === 'free') {
        return `<span class="nudge">🔥 это уже цена всей сети — добавьте оставшиеся ${nudge.rest} ${plural(nudge.rest, 'сообщество', 'сообщества', 'сообществ')} бесплатно</span>`;
    }
    if (nudge.kind === 'reaches-cap') {
        const add = `${nudge.add} ${plural(nudge.add, 'сообщество', 'сообщества', 'сообществ')}`;
        return `<span class="nudge">🔥 ещё ${add}${nudge.extra ? ` (+${fmt(nudge.extra)} ₽)` : ''} — и это цена всей сети, дальше добор бесплатный</span>`;
    }
    return `<span class="nudge">🔥 добавьте ещё ${nudge.add} ${plural(nudge.add, 'сообщество', 'сообщества', 'сообществ')} — и станет дешевле на ${fmt(nudge.diff)} ₽</span>`;
}

function orderText() {
    const items = pickedItems();
    const reach = items.reduce((s, i) => s + (i.members || 0), 0);
    const q = quote(items.length);
    const lines = [
        `Здравствуйте! Хочу разместить рекламу в сети САРАФАН — выбрал ${items.length} ` +
            `${plural(items.length, 'сообщество', 'сообщества', 'сообществ')}:`,
        '',
    ];
    lines.push(...items.map(i => i.line));
    lines.push('');
    lines.push(`Суммарный охват: ${fmt(reach)} подписчиков.`);
    if (q) {
        lines.push(
            `Ориентировочно по вашим расценкам: ${fmt(q.price)} ₽` +
            (q.anchor ? ` (${q.anchor})` : '') +
            (q.saved ? `, скидка ${fmt(q.saved)} ₽ к поштучной цене` : '') +
            '.'
        );
    }
    return lines.join('\n');
}

// ── Копирование ────────────────────────────────────────────────────────────
// navigator.clipboard живёт только в secure context (https / localhost);
// иначе — fallback через скрытую textarea + execCommand.

async function copyToClipboard(text, btn, restoreLabel) {
    let ok = false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            ok = true;
        }
    } catch (e) {
        ok = false;
    }
    if (!ok) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {
            ok = document.execCommand('copy');
        } catch (e) {
            ok = false;
        }
        document.body.removeChild(ta);
    }
    flashButton(btn, ok, restoreLabel);
}

function flashButton(btn, ok, restoreLabel) {
    if (!btn) return;
    const original = restoreLabel || btn.innerHTML;
    btn.innerHTML = ok ? '✓ Скопировано' : '✗ Не вышло';
    setTimeout(() => { btn.innerHTML = original; }, 1600);
}

// Полный текст в текущем порядке — «копировать весь список» уважает сортировку.
function currentFullText() {
    if (rlSort !== 'neighbors') {
        return orderedBlocks()
            .map(b => [b.title + ':'].concat(b.items.map(i => i.line)).join('\n'))
            .join('\n\n');
    }
    const depth = neighborRings(rlAnchor);
    const out = [];
    let current = null;
    sortedByNeighbors(depth).forEach(item => {
        const title = ringTitle(item, depth);
        if (title !== current) {
            if (current !== null) out.push('');
            out.push(title + ':');
            current = title;
        }
        out.push(item.line);
    });
    return out.join('\n');
}

// ── Форма заявки (заказ владельца 2026-11-27) ─────────────────────────────
//
// Клиент жмёт «Отправить заявку» — и заявка падает в базу владельца (в /ad
// видна как «Заявки с сайта», в Telegram и MAX приходит уведомление). До
// этого кнопка «Заказать» только клала текст в буфер, и заявка доезжала
// вручную через мессенджер.
//
// Токен отправки живёт на странице: двойной клик или F5 после отправки несут
// тот же токен, и сервер не создаёт вторую заявку.

let rlOrderToken = null;

function newOrderToken() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

function updateOrderSummary() {
    const box = document.getElementById('order-summary');
    if (!box) return;
    const items = pickedItems();
    if (!items.length) {
        box.textContent = 'Можно отправить и без выбора сообществ — подберём охват под задачу.';
        return;
    }
    const reach = items.reduce((s, i) => s + (i.members || 0), 0);
    const q = quote(items.length);
    box.innerHTML =
        'В заявке: <b>' + items.length + '</b> ' +
        plural(items.length, 'сообщество', 'сообщества', 'сообществ') +
        ' · охват <b>' + fmt(reach) + '</b>' +
        (q ? ' · ориентир <b>' + fmt(q.price) + ' ₽</b>' : '');
}

function initOrderForm() {
    const form = document.getElementById('order-form-el');
    if (!form) return;
    rlOrderToken = newOrderToken();
    const status = document.getElementById('order-status');
    const submit = document.getElementById('order-submit');

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const nameEl = document.getElementById('order-name');
        const contactEl = document.getElementById('order-contact');
        const commentEl = document.getElementById('order-comment');
        const name = nameEl.value.trim();
        const contact = contactEl.value.trim();
        if (!name || !contact) {
            status.className = 'order-status err';
            status.textContent = 'Заполните имя и контакт — без них мы не сможем вам ответить.';
            return;
        }
        submit.disabled = true;
        status.className = 'order-status';
        status.textContent = 'Отправляем…';
        try {
            const items = pickedItems();
            const resp = await fetch('/api/landing/orders', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    contact: contact,
                    comment: commentEl.value.trim() || null,
                    regions: items.map(i => ({ code: i.code })),
                    website: document.getElementById('order-website').value,
                    request_token: rlOrderToken,
                    page: window.location.href
                })
            });
            if (!resp.ok) {
                let detail = 'HTTP ' + resp.status;
                try {
                    const j = await resp.json();
                    if (j && j.detail) detail = j.detail;
                } catch (parseErr) { /* тело не JSON — оставляем HTTP-код */ }
                throw new Error(detail);
            }
            const data = await resp.json();
            status.className = 'order-status ok';
            status.textContent = '✅ Заявка принята, номер ' + (data.ref || '—') +
                '. Мы ответим в тот же день.';
            nameEl.value = '';
            contactEl.value = '';
            commentEl.value = '';
            // Следующая отправка — новая заявка, а не повтор текущей.
            rlOrderToken = newOrderToken();
            rlPicked.clear();
            render();
        } catch (err) {
            status.className = 'order-status err';
            status.textContent = 'Не удалось отправить: ' + err.message +
                '. Попробуйте ещё раз или напишите нам в мессенджер.';
        } finally {
            submit.disabled = false;
        }
    });

    updateOrderSummary();
}

// ── Инициализация ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initOrderForm();
    loadRegionLinks();

    document.getElementById('rl-refresh').addEventListener('click', loadRegionLinks);
    document.getElementById('rl-copy-all').addEventListener('click', (e) => {
        if (!rlData) return;
        copyToClipboard(currentFullText(), e.currentTarget, '⎘ Копировать весь список');
    });

    document.querySelectorAll('#rl-sort .seg-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#rl-sort .seg-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            rlSort = btn.dataset.sort;
            document.getElementById('rl-anchor-wrap').hidden = rlSort !== 'neighbors';
            if (rlData) render();
        });
    });
    document.getElementById('rl-anchor').addEventListener('change', (e) => {
        rlAnchor = e.target.value;
        render();
    });

    document.getElementById('sel-clear').addEventListener('click', () => {
        rlPicked.clear();
        render();
    });
    document.getElementById('sel-copy').addEventListener('click', (e) => {
        copyToClipboard(orderText(), e.currentTarget, '⎘ Скопировать заказ');
    });

    // Кнопка «Заказать» на тарифе ведёт к форме заявки: тариф подставляется в
    // комментарий, клиенту остаётся вписать имя и контакт и нажать «Отправить».
    // Заявка сразу попадает к владельцу (заказ 2026-11-27) — раньше кнопка
    // только копировала текст, и заявка доезжала вручную.
    document.querySelectorAll('.pkg-order').forEach(btn => {
        btn.addEventListener('click', () => {
            const comment = document.getElementById('order-comment');
            if (comment && !comment.value.trim()) {
                comment.value = 'Интересует тариф: ' + btn.dataset.pkg;
            }
            const form = document.getElementById('order-form');
            if (form) {
                form.scrollIntoView({ behavior: 'smooth', block: 'center' });
                const name = document.getElementById('order-name');
                if (name) setTimeout(() => name.focus(), 400);
            }
        });
    });
});
