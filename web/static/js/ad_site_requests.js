// Вкладка «Заявки с сайта» в рекламном кабинете /ad.
//
// Заказ владельца 2026-11-27: витрина сети дублируется на публичный домен
// (валентин.вмалмыже.рф), где клиент жмёт кнопку и оставляет заявку. Заявки
// приходят сюда — в свою таблицу landing_orders, а не в ad_requests: у них нет
// ни группы, ни поста ВК, они пришли от живого человека с сайта.
//
// Владелец ждёт их тут и получает уведомление в Telegram и MAX.

const SITE_STATUSES = [
    { value: 'new', label: 'новая' },
    { value: 'contacted', label: 'в работе' },
    { value: 'done', label: 'сделана' },
    { value: 'rejected', label: 'отказ' }
];

let siteRequestsLoaded = false;

function siteEscape(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function siteFormatDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', year: '2-digit',
        hour: '2-digit', minute: '2-digit'
    });
}

function siteRegionsText(order) {
    const items = order.regions || [];
    if (!items.length) return '—';
    const names = items.slice(0, 8).map(r => r.name).join(', ');
    return names + (items.length > 8 ? ` и ещё ${items.length - 8}` : '');
}

function siteOrderCard(order) {
    const badge = {
        new: 'text-bg-danger',
        contacted: 'text-bg-warning',
        done: 'text-bg-success',
        rejected: 'text-bg-secondary'
    }[order.status] || 'text-bg-secondary';
    const label = (SITE_STATUSES.find(s => s.value === order.status) || {}).label || order.status;

    return `
    <tr>
        <td class="align-top">
            <b>${siteEscape(order.name)}</b><br>
            <span class="text-muted small">${siteEscape(order.contact)}</span>
            ${order.comment ? `<br><span class="small">${siteEscape(order.comment)}</span>` : ''}
            ${order.source_host ? `<br><span class="text-muted small">откуда: ${siteEscape(order.source_host)}</span>` : ''}
        </td>
        <td class="align-top">
            ${order.regions_count || 0} шт.<br>
            <span class="text-muted small">${siteEscape(siteRegionsText(order))}</span>
            ${order.reach ? `<br><span class="small">охват ${order.reach}</span>` : ''}
            ${order.price_estimate ? `<br><span class="small">ориентир ${order.price_estimate} ₽</span>` : ''}
        </td>
        <td class="align-top">
            <span class="small">${siteFormatDate(order.created_at)}</span>
        </td>
        <td class="align-top">
            <span class="badge ${badge}">${siteEscape(label)}</span><br>
            <select class="form-select form-select-sm mt-1" style="min-width: 120px;"
                    onchange="siteSetStatus(${order.id}, this.value)">
                ${SITE_STATUSES.map(s =>
                    `<option value="${s.value}" ${s.value === order.status ? 'selected' : ''}>${s.label}</option>`
                ).join('')}
            </select>
        </td>
    </tr>`;
}

async function loadSiteRequests() {
    const box = document.getElementById('site-requests-body');
    if (!box) return;
    try {
        const data = await apiClient.getSiteRequests();
        const orders = data.orders || [];
        const pending = orders.filter(o => o.status === 'new').length;

        const badge = document.getElementById('site-requests-badge');
        if (badge) {
            if (pending > 0) {
                badge.textContent = pending;
                badge.classList.remove('d-none');
            } else {
                badge.classList.add('d-none');
            }
        }

        if (!orders.length) {
            box.innerHTML = '<p class="text-muted mb-0">Заявок с сайта пока нет.</p>';
            return;
        }

        box.innerHTML = `
        <div class="table-responsive">
            <table class="table table-sm align-middle">
                <thead>
                    <tr>
                        <th style="width: 34%">Кто оставил</th>
                        <th style="width: 30%">Что выбрал</th>
                        <th style="width: 16%">Когда</th>
                        <th style="width: 20%">Статус</th>
                    </tr>
                </thead>
                <tbody>${orders.map(siteOrderCard).join('')}</tbody>
            </table>
        </div>`;
    } catch (e) {
        box.innerHTML = `<p class="text-danger mb-0">Не удалось загрузить заявки: ${siteEscape(e.message)}</p>`;
    }
}

async function siteSetStatus(id, status) {
    try {
        await apiClient.setSiteRequestStatus(id, { status: status });
        await loadSiteRequests();
    } catch (e) {
        alert('Не удалось сохранить статус: ' + e.message);
        await loadSiteRequests();
    }
}

function initSiteRequests() {
    if (siteRequestsLoaded) return;
    siteRequestsLoaded = true;
    loadSiteRequests();
}

document.addEventListener('DOMContentLoaded', () => {
    // Бейдж «новые заявки» виден сразу, не дожидаясь открытия вкладки.
    loadSiteRequests();
});
