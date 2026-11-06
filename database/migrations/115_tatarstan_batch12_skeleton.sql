-- 115: skeleton for the twelfth Tatarstan batch: sarmanovsky (Сармановский),
-- muslyumovsky (Муслюмовский), menzelinsky (Мензелинский). Same pattern as 101-114:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders verified 2026-11-06
-- from ru.wikipedia:
--   Сармановский  — N: Тукаевский (не в сети), NE: Мензелинский (new), E:
--                   Муслюмовский (new), SE: Азнакаевский, SW: Альметьевский, W:
--                   Заинский. 34 230 чел. (2020); адм. центр село Сарманово.
--                   Touches THREE of ours — Азнакаевский, Альметьевский, Заинский.
--   Муслюмовский  — NE: Актанышский (не в сети), N: Мензелинский (new), W:
--                   Сармановский (new), SW: Азнакаевский, SE: Башкортостан.
--                   19 326 чел. (2020); адм. центр село Муслюмово. Touches ONE of
--                   ours — Азнакаевский.
--   Мензелинский  — S: Муслюмовский (new), W: Тукаевский (не в сети), SW:
--                   Сармановский (new), E: Актанышский (не в сети). 27 686 чел.
--                   (2020); адм. центр город Мензелинск. Touches NONE of ours —
--                   it enters the network through the two new districts of this
--                   same portion.
--
-- The trio is a closed triangle (Сармановский — Муслюмовский — Мензелинский):
-- every member borders both others, so the neighbour exchange starts inside the
-- group immediately, and the group hangs off the live patch through four live
-- edges (Азнакаевский x2, Альметьевский x1, Заинский x1) — the maximum among
-- connected three-district options left on the table.
--
-- Leftover for later (five unlocked): Агрызский (touches Менделеевский + Мензелинский
-- по акватории Нижнекамского водохранилища), Актанышский (touches Мензелинский +
-- Муслюмовский), Бавлинский (touches Бугульминский + Ютазинский), Дрожжановский
-- (touches Буинский), Ютазинский (touches Азнакаевский + Бугульминский)
-- (Тукаевский — сознательно пропущен).
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts).
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109-113 — paired or generic names pull wrong villages in discovery):
--   • Сармановский: «Альметьево» пропущено — одноимённое поселение в
--     Альметьевском районе; пары с общим стемом («Кашир», «Муртыш Тамак»,
--     «Чершилы», «Имян», «Мензелябаш», «Тамак») пропущены; «Усаево» есть и в
--     Мензелинском районе — не берём.
--   • Муслюмовский: «Октябрь» пропущено — имя месяца, generic; «Михайловка»
--     generic; пары с общим стемом («Чекмак», «Варяш», «Табын», «Карамалы»,
--     «Саитово») пропущены; «Мелля-Тамак» — общий стем «Тамак» с Сармановским.
--   • Мензелинский: «Николаевка» generic; «Усаево» — см. выше; пары с общим
--     стемом («Мазино», «Юшады», «Такермен», «Иркеняш», «Кичу») пропущены.
--
-- Group-name length check (VK limit 48): «САРМАНОВСКИЙ ...» = 45;
-- «МУСЛЮМОВСКИЙ ...» = 46; «МЕНЗЕЛИНСКИЙ ...» = 46.
--
-- Short addresses follow the canonical template `<город латиницей>_info<код региона>`:
-- Татарстан = 16 → sarmanovo_info16, muslyumovo_info16, menzelinsk_info16.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('sarmanovsky', 'САРМАНОВСКИЙ - ИНФО'),
    ('muslyumovsky', 'МУСЛЮМОВСКИЙ - ИНФО'),
    ('menzelinsky', 'МЕНЗЕЛИНСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'sarmanovsky',
    '{
        "novost": "Новости Сармановского района:",
        "reklama": "Объявления Сармановского района:",
        "kultura": "Культура Сармановского района:",
        "sport": "Спорт Сармановского района:",
        "admin": "Власть и общество Сармановского района:",
        "union": "Молодёжь и образование Сармановского района:",
        "detsad": "Детские сады Сармановского района:",
        "sosed": "Происшествия Сармановского района:",
        "addons": "Сармановский район — также:"
    }'::json,
    '{"raicentr": "Сарманово"}'::json,
    4096,
    '["Сарманово", "Джалиль", "Азалаково", "Александровка", "Рангазар", "Большое Нуркеево", "Петровский Завод", "Ляки", "Иляксаз", "Янурусово"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'muslyumovsky',
    '{
        "novost": "Новости Муслюмовского района:",
        "reklama": "Объявления Муслюмовского района:",
        "kultura": "Культура Муслюмовского района:",
        "sport": "Спорт Муслюмовского района:",
        "admin": "Власть и общество Муслюмовского района:",
        "union": "Молодёжь и образование Муслюмовского района:",
        "detsad": "Детские сады Муслюмовского района:",
        "sosed": "Происшествия Муслюмовского района:",
        "addons": "Муслюмовский район — также:"
    }'::json,
    '{"raicentr": "Муслюмово"}'::json,
    4096,
    '["Муслюмово", "Амикеево", "Баланны", "Баюково", "Большой Чекмак", "Уразметьево", "Варяш-Баш", "Кряш-Шуран", "Исансупово", "Семяково"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'menzelinsky',
    '{
        "novost": "Новости Мензелинского района:",
        "reklama": "Объявления Мензелинского района:",
        "kultura": "Культура Мензелинского района:",
        "sport": "Спорт Мензелинского района:",
        "admin": "Власть и общество Мензелинского района:",
        "union": "Молодёжь и образование Мензелинского района:",
        "detsad": "Детские сады Мензелинского района:",
        "sosed": "Происшествия Мензелинского района:",
        "addons": "Мензелинский район — также:"
    }'::json,
    '{"raicentr": "Мензелинск"}'::json,
    4096,
    '["Мензелинск", "Атрякле", "Бикбулово", "Кузембетьево", "Коноваловка", "Урусово", "Подгорный Байлар", "Кадряково", "Калмурзино", "Юртово"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Тукаевский и Актанышский
-- в сети не заведены). Уже живым — текущее значение с прода ПЛЮС новые коды.
-- Идемпотентно: повторный прогон не дублирует.
UPDATE regions SET neighbors = 'almetyevsky,aznakaevsky,menzelinsky,muslyumovsky,zainsky' WHERE code = 'sarmanovsky';
UPDATE regions SET neighbors = 'aznakaevsky,menzelinsky,sarmanovsky' WHERE code = 'muslyumovsky';
UPDATE regions SET neighbors = 'muslyumovsky,sarmanovsky' WHERE code = 'menzelinsky';

-- Обратные связи для уже живых (идемпотентно: добавляем новый код, если его
-- ещё нет в списке).
UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'sarmanovsky'
    WHEN neighbors LIKE '%sarmanovsky%' THEN neighbors
    ELSE neighbors || ',sarmanovsky'
  END
WHERE code IN ('almetyevsky', 'zainsky');

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'muslyumovsky,sarmanovsky'
    WHEN neighbors LIKE '%sarmanovsky%' AND neighbors LIKE '%muslyumovsky%' THEN neighbors
    WHEN neighbors LIKE '%sarmanovsky%' THEN neighbors || ',muslyumovsky'
    WHEN neighbors LIKE '%muslyumovsky%' THEN neighbors || ',sarmanovsky'
    ELSE neighbors || ',muslyumovsky,sarmanovsky'
  END
WHERE code = 'aznakaevsky';

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('sarmanovsky','muslyumovsky','menzelinsky','almetyevsky',
--                   'aznakaevsky','zainsky')
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
-- Rollback (текущие значения соседей на прода на 2026-11-06):
-- UPDATE regions SET neighbors = 'aznakaevsky,bugulminsky,cheremshansky,leninogorsky,nizhnekamsk,novosheshminsky,zainsky' WHERE code = 'almetyevsky';
-- UPDATE regions SET neighbors = 'almetyevsky,bugulminsky' WHERE code = 'aznakaevsky';
-- UPDATE regions SET neighbors = 'almetyevsky,nizhnekamsk' WHERE code = 'zainsky';
-- DELETE FROM region_configs WHERE region_code IN ('sarmanovsky','muslyumovsky','menzelinsky');
-- DELETE FROM regions WHERE code IN ('sarmanovsky','muslyumovsky','menzelinsky');
