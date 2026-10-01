-- 108: login case-insensitive uniqueness (P024)
--
-- radar_users.login сейчас имеет регистрозависимый UNIQUE (миграция 037).
-- email уже имеет uq_radar_users_email_lower (миграция 052).
-- Добавляем аналогичный индекс для login: предотвращает «VALSTAN» / «valstan»
-- как разные аккаунты — корень эскалации 2026-08-02.
--
-- Предусловие: коллизий по lower(login) в таблице нет (аккаунтов 4 на 08.09).
-- Миграция идемпотентна: CREATE UNIQUE INDEX IF NOT EXISTS.

-- 1) Проверка предусловия — если коллизии есть, миграция упадёт с понятной ошибкой.
--    Это защита от накатки на грязных данных.
DO $$
DECLARE
    dup_count int;
BEGIN
    SELECT count(*) INTO dup_count
    FROM (
        SELECT lower(login) AS l, count(*)
        FROM radar_users
        WHERE login IS NOT NULL
        GROUP BY lower(login)
        HAVING count(*) > 1
    ) t;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'P024: найдены коллизии по lower(login) — % пар(ы), миграция прервана', dup_count;
    END IF;
END $$;

-- 2) Уникальный индекс по lower(login), null-safe (login nullable с миграции 052).
CREATE UNIQUE INDEX IF NOT EXISTS uq_radar_users_login_lower
    ON radar_users (lower(login)) WHERE login IS NOT NULL;

-- 3) Документируем замысел в комментарии индекса.
COMMENT ON INDEX uq_radar_users_login_lower IS
    'P024: case-insensitive uniqueness for login (prevents VALSTAN/valstan collision). '
    'Mirrors uq_radar_users_email_lower from migration 052.';