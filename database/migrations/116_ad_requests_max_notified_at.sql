-- 116: ad_requests.max_notified_at — метка «карточка ушла в MAX».
--
-- Why this exists: уведомление о новой рекламной заявке уходит в мессенджер
-- MAX (бот «САРАФАН»), и без метки отправки скан заявок слал бы одну и ту же
-- карточку снова и снова — каждый проход, то есть каждые полчаса.
--
-- Почему колонка, а не ключ в Redis: заявка живёт в Postgres (так же, как
-- status/can_message/greeting_sent_at), и её «уже отправили» — это свойство
-- самой заявки. Redis-ключ потерялся бы при перезапуске/вытеснении, и тогда
-- владелец получил бы повторные картоки — ровно тот шум, который мы убираем.
--
-- NULL = в MAX ещё не отправляли. Значение = когда отправили (UTC, как
-- остальные timestamps в этой таблице).
--
-- Rollback:
-- ALTER TABLE ad_requests DROP COLUMN IF EXISTS max_notified_at;

BEGIN;

ALTER TABLE ad_requests
    ADD COLUMN IF NOT EXISTS max_notified_at TIMESTAMP NULL;

CREATE INDEX IF NOT EXISTS ix_ad_requests_max_notified_pending
    ON ad_requests (id)
    WHERE max_notified_at IS NULL AND status = 'new';

COMMIT;

-- Проверка: колонка есть, строк с меткой — ноль (до первой отправки).
--    SELECT column_name, data_type FROM information_schema.columns
--      WHERE table_name = 'ad_requests' AND column_name = 'max_notified_at';
--    SELECT count(*) FROM ad_requests WHERE max_notified_at IS NOT NULL;
