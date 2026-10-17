-- 113: skeleton for the tenth Tatarstan batch: leninogorsky (Лениногорский),
-- bugulminsky (Бугульминский), buinsky (Буинский). Same pattern as 101-112:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders verified 2026-10-12
-- from ru.wikipedia:
--   Лениногорский  — Альметьевский (не в сети), Бугульминский (new),
--                     Черемшанский, Самарская обл. 79 506 чел. (2021); адм. центр
--                     город Лениногорск. Touches ONE of ours — Черемшанский.
--   Бугульминский  — Азнакаевский (не в сети), Альметьевский (не в сети),
--                     Бавлинский (не в сети), Лениногорский (new), Ютазинский
--                     (не в сети) + Самарская и Оренбургская обл. 98 145 чел.
--                     (2025); адм. центр город Бугульма. Touches ONE of ours —
--                     Лениногорский (new).
--   Буинский       — Дрожжановский (не в сети), Апастовский, Тетюшский
--                     + Ульяновская обл. и Чувашия. 40 172 чел. (2021); адм.
--                     центр город Буинск. Touches TWO of ours — Апастовский,
--                     Тетюшский.
--
-- The trio is a connected chain (Буинский — Лениногорский — Бугульминский via
-- Апастовский/Тетюшский and Черемшанский respectively). Live edges with
-- multiplicity: Черемшанский x1, Апастовский x1, Тетюшский x1.
-- Leftover for later: Мензелинский, Муслюмовский, Азнакаевский, Бавлинский,
-- Ютазинский, Дрожжановский, Агрызский, Актанышский.
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts).
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109/111/112 — paired or generic names pull wrong villages in
-- discovery):
--   • Лениногорский: пары Верхнее/Нижнее/Среднее Алькеево и пр. — пропущены;
--     взято по одному из парных стемов.
--   • Бугульминский: пары Новая/Старая Александровка, Новое/Старое Исаково и
--     пр. — пропущены; взято одно из пары.
--   • Буинский: пары Козловка, Каменный Брод, Сула и пр. — пропущены; взято
--     одно из пары.
--
-- Group-name length check (VK limit 48): «ЛЕНИНОГОРСКИЙ - ИНФО | Афиша,
-- новости, события» = 46; «БУГУЛЬМИНСКИЙ ...» = 47; «БУИНСКИЙ ...» = 41.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('leninogorsky', 'ЛЕНИНОГОРСКИЙ - ИНФО'),
    ('bugulminsky', 'БУГУЛЬМИНСКИЙ - ИНФО'),
    ('buinsky', 'БУИНСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'leninogorsky',
    '{
        "novost": "Новости Лениногорского района:",
        "reklama": "Объявления Лениногорского района:",
        "kultura": "Культура Лениногорского района:",
        "sport": "Спорт Лениногорского района:",
        "admin": "Власть и общество Лениногорского района:",
        "union": "Молодёжь и образование Лениногорского района:",
        "detsad": "Детские сады Лениногорского района:",
        "sosed": "Происшествия Лениногорского района:",
        "addons": "Лениногорский район — также:"
    }'::json,
    '{"raicentr": "Лениногорск"}'::json,
    4096,
    '["Лениногорск", "Зай-Каратай", "Зелёная Роща", "Ивановка", "Каркали", "Керлигач", "Куакбаш", "Сарабикулово", "Старый Иштеряк", "Тимяшево"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'bugulminsky',
    '{
        "novost": "Новости Бугульминского района:",
        "reklama": "Объявления Бугульминского района:",
        "kultura": "Культура Бугульминского района:",
        "sport": "Спорт Бугульминского района:",
        "admin": "Власть и общество Бугульминского района:",
        "union": "Молодёжь и образование Бугульминского района:",
        "detsad": "Детские сады Бугульминского района:",
        "sosed": "Происшествия Бугульминского района:",
        "addons": "Бугульминский район — также:"
    }'::json,
    '{"raicentr": "Бугульма"}'::json,
    4096,
    '["Бугульма", "Карабаш", "Акбаш", "Берёзовка", "Восточный", "Вязовка", "Ключи", "Кудашево", "Малая Бугульма", "Наратлы"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'buinsky',
    '{
        "novost": "Новости Буинского района:",
        "reklama": "Объявления Буинского района:",
        "kultura": "Культура Буинского района:",
        "sport": "Спорт Буинского района:",
        "admin": "Власть и общество Буинского района:",
        "union": "Молодёжь и образование Буинского района:",
        "detsad": "Детские сады Буинского района:",
        "sosed": "Происшествия Буинского района:",
        "addons": "Буинский район — также:"
    }'::json,
    '{"raicentr": "Буинск"}'::json,
    4096,
    '["Буинск", "Адав-Тулумбаево", "Аксу", "Альшеево", "Бик-Утеево", "Большое Фролово", "Бюрганы", "Верхние Лащи", "Исаково", "Кият"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Альметьевский,
-- Азнакаевский, Бавлинский, Ютазинский, Дрожжановский и области в сети не
-- заведены). Уже живым — текущее значение с прода ПЛЮС новые коды, по
-- алфавиту. Идемпотентно: повторный прогон не дублирует.
UPDATE regions SET neighbors = 'bugulminsky,cheremshansky' WHERE code = 'leninogorsky';
UPDATE regions SET neighbors = 'leninogorsky' WHERE code = 'bugulminsky';
UPDATE regions SET neighbors = 'apastovsky,tetyushsky' WHERE code = 'buinsky';

-- Обратные связи для уже живых (идемпотентно: добавляем новый код, если его
-- ещё нет в списке).
UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'leninogorsky'
    WHEN neighbors LIKE '%leninogorsky%' THEN neighbors
    ELSE neighbors || ',leninogorsky'
  END
WHERE code = 'cheremshansky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'bugulminsky'
    WHEN neighbors LIKE '%bugulminsky%' THEN neighbors
    ELSE neighbors || ',bugulminsky'
  END
WHERE code = 'leninogorsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'buinsky'
    WHEN neighbors LIKE '%buinsky%' THEN neighbors
    ELSE neighbors || ',buinsky'
  END
WHERE code IN ('apastovsky', 'tetyushsky');

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('leninogorsky','bugulminsky','buinsky','cheremshansky',
--                   'apastovsky','tetyushsky')
--   ORDER BY code;
-- Симметричность (пустая выдача = все связи двусторонние):
--   SELECT a.code, b.code FROM regions a JOIN regions b
--     ON b.code = ANY(string_to_array(a.neighbors, ','))
--   WHERE NOT (a.code = ANY(string_to_array(coalesce(b.neighbors,''), ',')));
-- Отсутствие «висящих» соседей (пустая выдача = все коды существуют):
--   SELECT a.code, b.code FROM regions a, unnest(string_to_array(coalesce(a.neighbors,''), ',')) AS b(code)
--   WHERE b.code <> '' AND NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = b.code);
-- Отсутствие самоссылок (эта проверка ловит то, чего принципиально не ловит
--  проверка симметричности — урок 109: список пишется руками):
--   SELECT code FROM regions
--   WHERE code = ANY(string_to_array(coalesce(neighbors,''), ','));
--
-- Rollback:
-- UPDATE regions SET neighbors = 'aksubaevsky,cheremshansky,chistopolsky' WHERE code = 'cheremshansky';
-- UPDATE regions SET neighbors = 'aksubaevsky,alekseevsky,chistopolsky,laishevo,rybnaya_sloboda,spassky' WHERE code = 'apastovsky';
-- UPDATE regions SET neighbors = 'aksubaevsky,alekseevsky,kamsko_ustinsky,laishevo,tetyushsky' WHERE code = 'tetyushsky';
-- DELETE FROM region_configs WHERE region_code IN ('leninogorsky','bugulminsky','buinsky');
-- DELETE FROM regions WHERE code IN ('leninogorsky','bugulminsky','buinsky');
