-- 119: landing_orders — заявки, оставленные с публичного лендинга /regions/links.
--
-- Заказ владельца 2026-11-27: витрина сети дублируется на публичный домен
-- (валентин.вмалмыже.рф), и клиент должен нажать кнопку и СОЗДАТЬ ЗАЯВКУ,
-- а не скопировать текст в буфер (как было на лендинге до этого).
--
-- Почему своя таблица, а не запись в ad_requests:
--   * ad_requests — это реклама, ПОЙМАННАЯ в предложке сообщества или во
--     входящем ЛС: у неё есть community_vk_id (NOT NULL), vk_post_id, peer_id.
--     У заявки с сайта нет ни того, ни другого — писать фиктивные id ради
--     общей таблицы значило бы завалить её мусором и ломать чужую логику
--     (сканер, дедуп, роутер уведомлений).
--   * origin у ad_requests — это 'suggested' | 'inbound_dm'; третий источник
--     («лендинг») тянул бы за собой правки в сканере, в MAX-карточке и в
--     фильтрах кабинета.
-- Поэтому здесь своя таблица со своей формой и своим списком в /ad.
--
-- Время — UTC, как остальные timestamps проекта.
--
-- Rollback:
-- DROP TABLE IF EXISTS landing_orders;

BEGIN;

CREATE TABLE IF NOT EXISTS landing_orders (
    id BIGSERIAL PRIMARY KEY,
    -- Время — явный UTC: на машине timezone=Host, и голое now() отдало бы
    -- московское время (конвенция — tests/test_migrations_utc_convention.py).
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    -- Что клиент оставил о себе. Имя и контакт обязательны: без контакта
    -- заявку нельзя взять в работу (позвонить/написать некуда).
    name TEXT NOT NULL,
    contact TEXT NOT NULL,
    -- Выбранные на лендинге сообщества: [{code, name, members, url}].
    -- Снимок на момент отправки: список районов меняется, а заявка должна
    -- оставаться верной той картине, которую клиент видел.
    regions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    regions_count INTEGER NOT NULL DEFAULT 0,
    -- Суммарный охват выбранных сообществ (подписчики) на момент отправки.
    reach INTEGER,
    -- Расчётная цена по таблице config/ad_landing.py (ориентир, не счёт).
    price_estimate INTEGER,
    comment TEXT,
    -- Токен одной отправки: страница генерирует его один раз, все повторы
    -- формы (двойной клик, F5 после отправки) несут тот же токен. Уникальный
    -- индекс не даёт родиться второй заявке на один клик.
    request_token TEXT,
    -- Откуда пришла заявка: хост и страница (для разбора «откуда трафик»).
    source_host TEXT,
    source_page TEXT,
    -- new | contacted | done | rejected — жизненный цикл заявки.
    status TEXT NOT NULL DEFAULT 'new',
    owner_note TEXT,
    -- Метка «уведомление ушло» (TG/MAX), чтобы повторно не слать.
    notified_at TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS ix_landing_orders_created_at
    ON landing_orders (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_landing_orders_status
    ON landing_orders (status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_landing_orders_request_token
    ON landing_orders (request_token)
    WHERE request_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_landing_orders_notified_pending
    ON landing_orders (id)
    WHERE notified_at IS NULL AND status = 'new';

COMMIT;

-- Проверка: таблица есть, заявок ноль (до первой отправки).
--    SELECT column_name, data_type FROM information_schema.columns
--      WHERE table_name = 'landing_orders' ORDER BY ordinal_position;
--    SELECT count(*) FROM landing_orders;
