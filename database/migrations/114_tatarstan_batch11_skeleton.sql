-- 114: skeleton for the eleventh Tatarstan batch: aznakaevsky (Азнакаевский),
-- almetyevsky (Альметьевский), zainsky (Заинский). Same pattern as 101-113:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders verified 2026-10-16
-- from ru.wikipedia:
--   Азнакаевский  — Муслюмовский (не в сети), Сармановский (не в сети),
--                    Альметьевский (new), Бугульминский, Ютазинский (не в
--                    сети) + Бакалинский/Шаранский (Башкортостан). 58 966 чел.
--                    (2021); адм. центр город Азнакаево. Touches ONE of ours —
--                    Бугульминский.
--   Альметьевский — Сармановский (не в сети), Заинский (new), Нижнекамский,
--                    Новошешминский, Черемшанский, Лениногорский, Бугульминский,
--                    Азнакаевский (new). 213 235 чел. (2021); адм. центр город
--                    Альметьевск. Touches FIVE of ours — Нижнекамский,
--                    Новошешминский, Черемшанский, Лениногорский, Бугульминский.
--   Заинский      — Тукаевский (не в сети), Сармановский (не в сети),
--                    Альметьевский (new), Нижнекамский. 53 731 чел. (2021);
--                    адм. центр город Заинск. Touches ONE of ours —
--                    Нижнекамский.
--
-- The trio is a connected chain (Заинский — Альметьевский — Азнакаевский) with
-- Альметьевский as the hub. Live edges with multiplicity: Бугульминский x2,
-- Нижнекамский x2, Новошешминский x1, Черемшанский x1, Лениногорский x1 —
-- seven live edges total.
-- Leftover for later (eight unlocked): Агрызский, Актанышский, Бавлинский,
-- Дрожжановский, Мензелинский, Муслюмовский, Сармановский, Ютазинский
-- (Тукаевский — сознательно пропущен).
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts).
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109-113 — paired or generic names pull wrong villages in discovery):
--   • Азнакаевский: «Победа» (Вахитовское сп) пропущена — generic; «Урсаево»
--     исключено — одноимённый адм. центр есть и в Заинском, в списках не
--     оставлено ни одного.
--   • Альметьевский: «Молодёжный» и «Васильевка» пропущены — generic;
--     пара Кичуй/Кичучатово (общий стем) пропущена.
--   • Заинский: «Поповка» пропущена — generic; стем «Багряж» (Средний и
--     Верхний Багряж тут, Багряж и Багряж-Никольское в Альметьевском) —
--     пропущен; «Урсаево» исключено (см. выше).
--
-- Group-name length check (VK limit 48): «АЗНАКАЕВСКИЙ - ИНФО | Афиша,
-- новости, события» = 45; «АЛЬМЕТЬЕВСКИЙ ...» = 46; «ЗАИНСКИЙ ...» = 41.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('aznakaevsky', 'АЗНАКАЕВСКИЙ - ИНФО'),
    ('almetyevsky', 'АЛЬМЕТЬЕВСКИЙ - ИНФО'),
    ('zainsky', 'ЗАИНСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'aznakaevsky',
    '{
        "novost": "Новости Азнакаевского района:",
        "reklama": "Объявления Азнакаевского района:",
        "kultura": "Культура Азнакаевского района:",
        "sport": "Спорт Азнакаевского района:",
        "admin": "Власть и общество Азнакаевского района:",
        "union": "Молодёжь и образование Азнакаевского района:",
        "detsad": "Детские сады Азнакаевского района:",
        "sosed": "Происшествия Азнакаевского района:",
        "addons": "Азнакаевский район — также:"
    }'::json,
    '{"raicentr": "Азнакаево"}'::json,
    4096,
    '["Азнакаево", "Актюбинский", "Агерзе", "Какре-Елга", "Мальбагуш", "Сарлы", "Тумутук", "Урманаево", "Чалпы", "Чубар-Абдуллово"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'almetyevsky',
    '{
        "novost": "Новости Альметьевского района:",
        "reklama": "Объявления Альметьевского района:",
        "kultura": "Культура Альметьевского района:",
        "sport": "Спорт Альметьевского района:",
        "admin": "Власть и общество Альметьевского района:",
        "union": "Молодёжь и образование Альметьевского района:",
        "detsad": "Детские сады Альметьевского района:",
        "sosed": "Происшествия Альметьевского района:",
        "addons": "Альметьевский район — также:"
    }'::json,
    '{"raicentr": "Альметьевск"}'::json,
    4096,
    '["Альметьевск", "Нижняя Мактама", "Абдрахманово", "Калейкино", "Кульшарипово", "Миннибаево", "Новое Каширово", "Новое Надырово", "Русский Акташ", "Сулеево"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'zainsky',
    '{
        "novost": "Новости Заинского района:",
        "reklama": "Объявления Заинского района:",
        "kultura": "Культура Заинского района:",
        "sport": "Спорт Заинского района:",
        "admin": "Власть и общество Заинского района:",
        "union": "Молодёжь и образование Заинского района:",
        "detsad": "Детские сады Заинского района:",
        "sosed": "Происшествия Заинского района:",
        "addons": "Заинский район — также:"
    }'::json,
    '{"raicentr": "Заинск"}'::json,
    4096,
    '["Заинск", "Аксарино", "Бегишево", "Бухарай", "Верхние Шипки", "Дурт-Мунча", "Нижнее Бишево", "Поручиково", "Савалеево", "Чубуклы"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Муслюмовский,
-- Сармановский, Тукаевский, Ютазинский и Башкортостан в сети не заведены).
-- Уже живым — текущее значение с прода ПЛЮС недостающие новые коды.
-- Идемпотентно: повторный прогон не дублирует.
UPDATE regions SET neighbors = 'almetyevsky,bugulminsky' WHERE code = 'aznakaevsky';
UPDATE regions SET neighbors = 'aznakaevsky,bugulminsky,cheremshansky,leninogorsky,nizhnekamsk,novosheshminsky,zainsky' WHERE code = 'almetyevsky';
UPDATE regions SET neighbors = 'almetyevsky,nizhnekamsk' WHERE code = 'zainsky';

-- Обратные связи для уже живых (идемпотентно: добавляем новый код, если его
-- ещё нет в списке).
UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'almetyevsky'
    WHEN neighbors LIKE '%almetyevsky%' THEN neighbors
    ELSE neighbors || ',almetyevsky'
  END
WHERE code IN ('cheremshansky', 'leninogorsky', 'novosheshminsky');

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'almetyevsky'
    WHEN neighbors LIKE '%almetyevsky%' THEN neighbors
    ELSE neighbors || ',almetyevsky'
  END
WHERE code = 'bugulminsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'aznakaevsky'
    WHEN neighbors LIKE '%aznakaevsky%' THEN neighbors
    ELSE neighbors || ',aznakaevsky'
  END
WHERE code = 'bugulminsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'almetyevsky'
    WHEN neighbors LIKE '%almetyevsky%' THEN neighbors
    ELSE neighbors || ',almetyevsky'
  END
WHERE code = 'nizhnekamsk';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'zainsky'
    WHEN neighbors LIKE '%zainsky%' THEN neighbors
    ELSE neighbors || ',zainsky'
  END
WHERE code = 'nizhnekamsk';

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('aznakaevsky','almetyevsky','zainsky','bugulminsky',
--                   'cheremshansky','leninogorsky','nizhnekamsk',
--                   'novosheshminsky')
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
-- Rollback (текущие значения прода на 2026-10-16):
-- UPDATE regions SET neighbors = 'chistopolsky,elabuga,mamadysh' WHERE code = 'nizhnekamsk';
-- UPDATE regions SET neighbors = 'aksubaevsky,cheremshansky,chistopolsky' WHERE code = 'novosheshminsky';
-- UPDATE regions SET neighbors = 'aksubaevsky,novosheshminsky,nurlatsky,leninogorsky' WHERE code = 'cheremshansky';
-- UPDATE regions SET neighbors = 'bugulminsky,cheremshansky' WHERE code = 'leninogorsky';
-- UPDATE regions SET neighbors = 'leninogorsky' WHERE code = 'bugulminsky';
-- DELETE FROM region_configs WHERE region_code IN ('aznakaevsky','almetyevsky','zainsky');
-- DELETE FROM regions WHERE code IN ('aznakaevsky','almetyevsky','zainsky');
