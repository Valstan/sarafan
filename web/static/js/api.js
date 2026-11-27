// API Client for SETKA
const apiClient = {
    baseUrl: '/api',
    
    // Generic request handler
    async request(endpoint, options = {}) {
        const url = `${this.baseUrl}${endpoint}`;
        const config = {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        };
        
        try {
            const response = await fetch(url, config);
            
            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: response.statusText }));
                const err = new Error(error.detail || `HTTP ${response.status}`);
                // 503 — «сторонний сервис прилёг, повторите», а не «мы отказали».
                // Статус ехал только в текст, поэтому вызывающий не мог отличить
                // временную аварию от настоящего отказа и красил всё в danger.
                err.status = response.status;
                err.transient = response.status === 503;
                throw err;
            }
            
            return await response.json();
        } catch (error) {
            console.error('API Request failed:', error);
            throw error;
        }
    },
    
    // Health endpoints
    async getHealth() {
        return this.request('/health/');
    },
    
    async getFullHealth() {
        return this.request('/health/full');
    },

    // Ad cabinet endpoints
    async getAdRequests(params = {}) {
        const q = new URLSearchParams();
        if (params.status) q.set('status', params.status);
        if (params.origin) q.set('origin', params.origin);
        if (params.route) q.set('route', params.route);
        if (params.community_vk_id) q.set('community_vk_id', params.community_vk_id);
        if (params.date_from) q.set('date_from', params.date_from);
        if (params.date_to) q.set('date_to', params.date_to);
        const qs = q.toString();
        return this.request(`/ad-cabinet/requests${qs ? '?' + qs : ''}`);
    },

    async getAdThread(id, count = 30) {
        return this.request(`/ad-cabinet/requests/${id}/thread?count=${count}`);
    },

    // Заявки с публичного лендинга (валентин.вмалмыже.рф, заказ 2026-11-27).
    // Живут в своей таблице landing_orders — у ad_requests нет ни группы, ни
    // поста ВК, а заявка с сайта приходит именно от живого человека.
    async getSiteRequests(params = {}) {
        const q = new URLSearchParams();
        if (params.status) q.set('status', params.status);
        const qs = q.toString();
        return this.request(`/ad-crm/landing-orders${qs ? '?' + qs : ''}`);
    },

    async setSiteRequestStatus(id, payload) {
        return this.request(`/ad-crm/landing-orders/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(payload)
        });
    },

    async prepareAdReply(id, templateId) {
        return this.request(`/ad-cabinet/requests/${id}/prepare`, {
            method: 'POST',
            body: JSON.stringify({ template_id: templateId })
        });
    },

    async sendAdReply(id, payload = {}) {
        return this.request(`/ad-cabinet/requests/${id}/send`, {
            method: 'POST',
            body: JSON.stringify({
                message: payload.message ?? null,
                images: payload.images ?? null
            })
        });
    },

    async setAdStatus(id, status) {
        return this.request(`/ad-cabinet/requests/${id}/status`, {
            method: 'POST',
            body: JSON.stringify({ status })
        });
    },

    // Единый роутер входящих ЛС (Этап 1): маршрут и наш статус обработки.
    async setAdRoute(id, route) {
        return this.request(`/ad-cabinet/requests/${id}/route`, {
            method: 'POST',
            body: JSON.stringify({ route })
        });
    },

    async setAdHandling(id, handlingStatus) {
        return this.request(`/ad-cabinet/requests/${id}/handling`, {
            method: 'POST',
            body: JSON.stringify({ handling_status: handlingStatus })
        });
    },

    async bulkAdAction(ids, action, status = null) {
        return this.request('/ad-cabinet/requests/bulk-action', {
            method: 'POST',
            body: JSON.stringify({ ids, action, status })
        });
    },

    // С5 — сквозное оформление заявки одной кнопкой.
    async acceptAdRequest(id, payload) {
        return this.request(`/ad-cabinet/requests/${id}/accept`, {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async publishAdRequestNow(id) {
        return this.request(`/ad-cabinet/requests/${id}/publish`, {
            method: 'POST'
        });
    },

    // Удалить пост-предложку из VK И из кабинета (в отличие от «Пропустить»,
    // который оставляет пост в VK).
    async deleteAdRequestPost(id) {
        return this.request(`/ad-cabinet/requests/${id}/delete-post`, {
            method: 'POST'
        });
    },

    async getAdTemplates() {
        return this.request('/templates/?include_inactive=0');
    },

    // Ad cabinet — библиотека офферных картинок
    async getOfferImages() {
        return this.request('/ad-cabinet/offer-images');
    },

    async uploadOfferImage(file) {
        const fd = new FormData();
        fd.append('file', file);
        // Без Content-Type вручную — браузер сам проставит multipart boundary.
        const resp = await fetch(`${this.baseUrl}/ad-cabinet/offer-images`, {
            method: 'POST',
            body: fd
        });
        if (!resp.ok) {
            const e = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(e.detail || `HTTP ${resp.status}`);
        }
        return resp.json();
    },

    async deleteOfferImage(name) {
        return this.request(`/ad-cabinet/offer-images/${encodeURIComponent(name)}`, {
            method: 'DELETE'
        });
    },

    // Ad cabinet — планировщик отложенных постов
    async getScheduledPosts(params = {}) {
        const q = new URLSearchParams();
        if (params.community_vk_id) q.set('community_vk_id', params.community_vk_id);
        if (params.status) q.set('status', params.status);
        if (params.date_from) q.set('date_from', params.date_from);
        if (params.date_to) q.set('date_to', params.date_to);
        const qs = q.toString();
        return this.request(`/ad-cabinet/scheduled${qs ? '?' + qs : ''}`);
    },

    async createScheduledPosts(payload) {
        return this.request('/ad-cabinet/scheduled', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async cancelScheduledPost(id) {
        return this.request(`/ad-cabinet/scheduled/${id}/cancel`, {
            method: 'POST'
        });
    },

    // Ad cabinet — планировщик предложки (Этап 0): оригинал с подписью + репосты
    async getSuggestedPlanOptions(communityVkId) {
        return this.request(`/ad-cabinet/suggested-plan/options?community_vk_id=${encodeURIComponent(communityVkId)}`);
    },

    async createSuggestedPlan(payload) {
        return this.request('/ad-cabinet/suggested-plan', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async getSuggestedPlan(params = {}) {
        const q = new URLSearchParams();
        if (params.community_vk_id) q.set('community_vk_id', params.community_vk_id);
        if (params.days) q.set('days', params.days);
        const qs = q.toString();
        return this.request(`/ad-cabinet/suggested-plan${qs ? '?' + qs : ''}`);
    },

    // Ad cabinet — CRM (блок C): клиенты / оплаты / публикации
    async getCrmFunnel() {
        return this.request('/ad-crm/funnel');
    },

    // Готовый рекламный текст (автоприветствие с живыми цифрами) — для буфера обмена
    async getCrmGreetingText() {
        return this.request('/ad-crm/greeting-text');
    },

    async getCrmClients(params = {}) {
        const q = new URLSearchParams();
        if (params.stage) q.set('stage', params.stage);
        if (params.region_id) q.set('region_id', params.region_id);
        if (params.q) q.set('q', params.q);
        if (params.debtors_only) q.set('debtors_only', 'true');
        const qs = q.toString();
        return this.request(`/ad-crm/clients${qs ? '?' + qs : ''}`);
    },

    async getCrmClient(id) {
        return this.request(`/ad-crm/clients/${id}`);
    },

    // И5: входящие заявки конкретного клиента (overlay-модалка «всё в карточке»).
    async getClientRequests(id) {
        return this.request(`/ad-crm/clients/${id}/requests`);
    },

    async createCrmClient(payload) {
        return this.request('/ad-crm/clients', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async updateCrmClient(id, payload) {
        return this.request(`/ad-crm/clients/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(payload)
        });
    },

    async deleteCrmClient(id) {
        return this.request(`/ad-crm/clients/${id}`, { method: 'DELETE' });
    },

    async upsertCrmFromRequest(requestId) {
        return this.request(`/ad-crm/clients/upsert-from-request/${requestId}`, {
            method: 'POST'
        });
    },

    async createCrmPayment(payload) {
        return this.request('/ad-crm/payments', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    // ---- рассылка оффера (Этап 4) ----
    async outreachCampaigns() {
        return this.request('/ad-outreach/campaigns');
    },

    async outreachCampaign(id) {
        return this.request(`/ad-outreach/campaigns/${id}`);
    },

    async outreachCreate(payload) {
        return this.request('/ad-outreach/campaigns', { method: 'POST', body: JSON.stringify(payload) });
    },

    async outreachAction(id, action, payload) {
        return this.request(`/ad-outreach/campaigns/${id}/${action}`, {
            method: 'POST', body: JSON.stringify(payload || {})
        });
    },

    async outreachRecipients(id, status) {
        const q = status ? '?status=' + encodeURIComponent(status) : '';
        return this.request(`/ad-outreach/campaigns/${id}/recipients${q}`);
    },

    async outreachManual(id) {
        return this.request(`/ad-outreach/campaigns/${id}/manual`);
    },

    async outreachDone(recipientId) {
        return this.request(`/ad-outreach/recipients/${recipientId}/done`, { method: 'POST', body: '{}' });
    },

    async outreachBlacklist(vkUserId, reason) {
        return this.request('/ad-outreach/blacklist', {
            method: 'POST', body: JSON.stringify({ vk_user_id: vkUserId, reason })
        });
    },

    async confirmOrderPaid(orderRef, bank) {
        return this.request(`/ad-crm/orders/${encodeURIComponent(orderRef)}/confirm-paid`, {
            method: 'POST',
            body: JSON.stringify({ bank: bank || null })
        });
    },

    async confirmClientPayments(clientId, bank) {
        return this.request(`/ad-crm/clients/${clientId}/payments/confirm-all`, {
            method: 'POST',
            body: JSON.stringify({ bank: bank || null })
        });
    },

    async updateCrmPayment(id, payload) {
        return this.request(`/ad-crm/payments/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(payload)
        });
    },

    async deleteCrmPayment(id) {
        return this.request(`/ad-crm/payments/${id}`, { method: 'DELETE' });
    },

    async getCrmBanks() {
        return this.request('/ad-crm/banks');
    },

    async getCrmTimeseries(days = 30) {
        return this.request(`/ad-crm/stats/timeseries?days=${days}`);
    },

    // CRM — метрики публикаций (С3)
    async refreshCrmClientStats(clientId) {
        return this.request(`/ad-crm/clients/${clientId}/refresh-stats`, { method: 'POST' });
    },

    async getCrmClientStatsReport(clientId) {
        return this.request(`/ad-crm/clients/${clientId}/stats-report`);
    },

    // CRM — позиции заказа (PR-4)
    async getCrmOrderItems(clientId) {
        return this.request(`/ad-crm/clients/${clientId}/order-items`);
    },

    async createCrmOrderItem(payload) {
        return this.request('/ad-crm/order-items', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async createCrmOrderItemFromRequest(requestId) {
        return this.request(`/ad-crm/order-items/from-request/${requestId}`, {
            method: 'POST'
        });
    },

    async updateCrmOrderItem(id, payload) {
        return this.request(`/ad-crm/order-items/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(payload)
        });
    },

    async deleteCrmOrderItem(id) {
        return this.request(`/ad-crm/order-items/${id}`, { method: 'DELETE' });
    },

    // CRM — чат с клиентом (PR-5)
    async getCrmClientThread(clientId, count = 30) {
        return this.request(`/ad-crm/clients/${clientId}/thread?count=${count}`);
    },

    async sendCrmClientReply(clientId, message) {
        return this.request(`/ad-crm/clients/${clientId}/reply`, {
            method: 'POST',
            body: JSON.stringify({ message })
        });
    },

    async createCrmPublication(payload) {
        return this.request('/ad-crm/publications', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async deleteCrmPublication(id) {
        return this.request(`/ad-crm/publications/${id}`, { method: 'DELETE' });
    },

    async updateCrmPublication(id, payload) {
        return this.request(`/ad-crm/publications/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(payload)
        });
    },

    async resolveCommunity(url) {
        return this.request('/ad-crm/community-info', {
            method: 'POST',
            body: JSON.stringify({ url })
        });
    },

    async resolveProfile(url) {
        return this.request('/ad-crm/resolve-profile', {
            method: 'POST',
            body: JSON.stringify({ url })
        });
    },

    async scanClientPosts(clientId, payload) {
        return this.request(`/ad-crm/clients/${clientId}/scan-posts`, {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async getCrmClientReport(clientId, filters = {}) {
        return this.request(`/ad-crm/clients/${clientId}/report`, {
            method: 'POST',
            body: JSON.stringify(filters)
        });
    },

    // CRM — таймлайн взаимодействий (PR-2)
    async getCrmTimeline(clientId) {
        return this.request(`/ad-crm/clients/${clientId}/timeline`);
    },

    async createCrmInteraction(payload) {
        return this.request('/ad-crm/interactions', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    },

    async updateCrmInteraction(id, payload) {
        return this.request(`/ad-crm/interactions/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(payload)
        });
    },

    async deleteCrmInteraction(id) {
        return this.request(`/ad-crm/interactions/${id}`, { method: 'DELETE' });
    },

    // Regions endpoints
    async getRegions() {
        return this.request('/regions/');
    },
    
    async getRegion(code) {
        return this.request(`/regions/${code}`);
    },
    
    async createRegion(data) {
        return this.request('/regions/', {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },
    
    async updateRegion(id, data) {
        return this.request(`/regions/${id}`, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    },
    
    async toggleRegionStatus(regionId) {
        return this.request(`/regions/${regionId}/toggle-status`, {
            method: 'PATCH'
        });
    },
    
    async deleteRegion(id) {
        return this.request(`/regions/${id}`, {
            method: 'DELETE'
        });
    },
    
    // Communities endpoints
    async getCommunities(params = {}) {
        let communities = [];

        if (params.region_id) {
            const regionId = params.region_id;
            delete params.region_id;
            communities = await this.request(`/communities/region/${regionId}`);
        } else {
            const regions = await this.getRegions();
            const allCommunities = [];
            for (const region of regions) {
                try {
                    const regionCommunities = await this.request(`/communities/region/${region.id}`);
                    allCommunities.push(...regionCommunities);
                } catch (err) {
                    console.warn(`Failed to load communities for region ${region.id}:`, err);
                }
            }
            communities = allCommunities;
        }

        // Apply filters
        let filtered = communities;
        if (params.category) {
            filtered = filtered.filter(c => c.category === params.category);
        }
        if (params.is_active !== undefined) {
            filtered = filtered.filter(c => c.is_active === params.is_active);
        }
        if (params.health_status) {
            filtered = filtered.filter(c => (c.health_status || 'active') === params.health_status);
        }

        // Apply pagination
        const skip = parseInt(params.skip) || 0;
        const limit = parseInt(params.limit) || 100;
        return filtered.slice(skip, skip + limit);
    },
    
    async getCommunitiesByRegion(regionId) {
        return this.request(`/communities/region/${regionId}`);
    },
    
    async getCommunity(id) {
        return this.request(`/communities/${id}`);
    },
    
    async createCommunity(data) {
        return this.request('/communities/', {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },
    
    async updateCommunity(id, data) {
        return this.request(`/communities/${id}`, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    },
    
    async deleteCommunity(id) {
        return this.request(`/communities/${id}`, {
            method: 'DELETE'
        });
    },

    async applySuggestedCategory(id) {
        return this.request(`/communities/${id}/apply-suggested-category`, {
            method: 'POST'
        });
    },
    
    // Posts endpoints
    async getPosts(params = {}) {
        const queryString = new URLSearchParams(params).toString();
        const endpoint = queryString ? `/posts/?${queryString}` : '/posts/';
        return this.request(endpoint);
    },
    
    async getPost(id) {
        return this.request(`/posts/${id}`);
    },
    
    // VK API monitoring endpoints
    async getVKStats() {
        return this.request('/vk/stats');
    },
    
    async validateVKTokens() {
        return this.request('/vk/validate-tokens');
    },
    
    async getVKCarouselStatus() {
        return this.request('/vk/carousel-status');
    },
    
    // Notifications endpoints
    async getNotifications() {
        return this.request('/notifications/');
    },
    
    async getSuggestedNotifications() {
        return this.request('/notifications/suggested');
    },
    
    async getMessagesNotifications() {
        return this.request('/notifications/messages');
    },
    
    async checkNotificationsNow() {
        return this.request('/notifications/check-now', {
            method: 'POST'
        });
    },
    
    async clearNotifications() {
        return this.request('/notifications/', {
            method: 'DELETE'
        });
    },
    
    // Stats helper
    async getStats() {
        try {
            const [regions, posts, communities] = await Promise.all([
                this.getRegions(),
                this.getPosts({ limit: 1 }),
                this.getCommunities({ limit: 1 })
            ]);
            
            return {
                regionsCount: regions.length,
                postsCount: posts.length,
                communitiesCount: communities.length
            };
        } catch (error) {
            console.error('Error getting stats:', error);
            return {
                regionsCount: 0,
                postsCount: 0,
                communitiesCount: 0
            };
        }
    },

    // Subscriber growth (comparison chart) — главные ИНФО-группы регионов
    async getGrowthRegions(days = 90) {
        return this.request(`/subscriber-growth/regions?days=${days}`);
    },

    async getGrowthSeries(ids, days = 90, oblastSum = [], oblastUniq = []) {
        const csv = (a) => (Array.isArray(a) ? a.join(',') : String(a || ''));
        const params = new URLSearchParams({ days: String(days) });
        if (csv(ids)) params.set('ids', csv(ids));
        if (csv(oblastSum)) params.set('oblast_sum', csv(oblastSum));
        if (csv(oblastUniq)) params.set('oblast_uniq', csv(oblastUniq));
        return this.request(`/subscriber-growth/series?${params.toString()}`);
    }
};

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = apiClient;
}

