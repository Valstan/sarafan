-- 118: skeleton for the fourteenth Tatarstan batch: bavlinsky (Бавлинский),
-- drozhzhanovsky (Дрожжановский), tukaysky (Тукаевский). Same pattern as 101-117:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- This batch CLOSES Tatarstan: 43 муниципальных района (43 + 2 городских округа
-- по tatarstan.ru и ru.wikipedia), в сети было 40 → станет 43/43.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders verified 2026-11-24
-- from ru.wikipedia (district articles + «Административно-территориальное
-- деление Татарстана»), not from memory:
--   Бавлинский     — W: Бугульминский; N: Ютазинский; E: Башкортостан
--                    (Ермекеевский, Туймазинский, г. Октябрьский); W/S:
--                    Оренбургская область (Северный, Абдулинский).
--                    Адм. центр — город Бавлы; 34 479 чел. (2020);
--                    1222,85 км². Touches TWO of ours — Бугульминский,
--                    Ютазинский (Ютазинский вошёл в сеть 20.11, в порции 13-й
--                    его ещё не было — отсюда и новое ребро).
--   Дрожжановский  — Буинский район Татарстана; на севере Чувашия
--                    (Шемуршинский район), на юге Ульяновская область
--                    (Сурский, Майнский, Цильнинский). Адм. центр — село
--                    Старое Дрожжаное; 1029,5 км². Touches ONE of ours —
--                    Буинский. Самая слабая связность из трёх, но это
--                    последний кусок области, а отказ от него оставляет дыру
--                    на юго-западе.
--   Тукаевский     — N: Менделеевский, Агрызский; W: Елабужский, Нижнекамский;
--                    S: Заинский, Сармановский; E: Мензелинский.
--                    Адм. центр — город Набережные Челны (1729,49 км²,
--                    42 511 чел. в самом районе). Touches SEVEN of ours —
--                    Менделеевский, Агрызский, Елабужский, Нижнекамский,
--                    Заинский, Сармановский, Мензелинский: максимум связности
--                    среди всех трёх кандидатов.
--
-- ⚠️ Тукаевский раньше пропускался сознательно (см. P231): «Административный
-- центр — Набережные Челны, сам город не входит в состав района, является
-- городом республиканского подчинения». Решение владельца 24.11: включаем —
-- «оставшиеся районы» значит все. Поэтому хештег центра — #Тукаевский
-- (а не #НабережныеЧелны: город в состав района не входит и его новости —
-- не наш охват), а город группы в ВК — Набережные Челны (ближайший город,
-- гео-метка).
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts, not okrugs). raicentr goes into hashtags (#<raicentr>), so no spaces:
-- «с. Старое Дрожжаное» → «Старое Дрожжаное».
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109-117 — paired or generic names pull wrong villages in discovery):
--   • Бавлинский: «Кзыл-Яр» (общий стем с «Старый Кзыл-Яр» Агрызского),
--     «Алексеевка» (омоним Алексеевского района), «Бакалы» (Башкортостан),
--     «Муртаза» (омоним Альметьевского района), «Поповка»/«Дмитриевка»/
--     «Дубовка»/«Измайлово» (generic).
--   • Дрожжановский: «Матаки» (омоним с «Базарные Матаки» Альметьевского
--     района), «Городище» (generic), «Новое Ильмово»/«Ильмово» (generic),
--     «Алексеевка» (омоним Алексеевского района).
--   • Тукаевский: «Калмаш» и «Калмия» (общие стемы со Спасским районом),
--     «Мусабай-Завод» и «Новый Мусабай» (тот же стем, Спасский район),
--     «Тлянче-Тамак» (стем «Тамак» с Сармановским/Муслюмовским/Ютазинским),
--     «Бетьки» (омоним Рыбно-Слободского — ловля школьных объявлений Бетьков
--     принесла в пул Тукаевского чужой район, см. журнал освежения 14.09),
--     «Новотроицкое» (омоним Шабалинского района Кировской области),
--     «Мелекес» (омоним Альметьевского района), «Калинино»/«Калиновка»
--     (канон-грабля: стем «калинин» ловит фамилию «Калинина»),
--     «п. Новый» (generic), «Комсомолец» (generic), «Круглое поле» (generic),
--     «Сосновый Бор» (generic), «Совхоз Татарстан» (не название населённого
--     пункта), «Таш-Кичу» (Агрызский район).
--
-- Group-name length check (VK limit 48): «БАВЛИНСКИЙ - ИНФО | Афиша, новости,
-- события» = 43; «ДРОЖЖАНОВСКИЙ ...» = 46; «ТУКАЕВСКИЙ ...» = 43.
--
-- Short addresses follow the canonical template `<город латиницей>_info<код региона>`:
-- Татарстан = 16 → bavly_info16, drozhzhanoe_info16. Тукаевский — по названию
-- района (tukaysky_info16): его город-центр Набережные Челны в состав
-- района не входит, адрес «chelny_info16» вводил бы в заблуждение.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('bavlinsky', 'БАВЛИНСКИЙ - ИНФО'),
    ('drozhzhanovsky', 'ДРОЖЖАНОВСКИЙ - ИНФО'),
    ('tukaysky', 'ТУКАЕВСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'bavlinsky',
    '{
        "novost": "Новости Бавлинского района:",
        "reklama": "Объявления Бавлинского района:",
        "kultura": "Культура Бавлинского района:",
        "sport": "Спорт Бавлинского района:",
        "admin": "Власть и общество Бавлинского района:",
        "union": "Молодёжь и образование Бавлинского района:",
        "detsad": "Детские сады Бавлинского района:",
        "sosed": "Происшествия Бавлинского района:",
        "addons": "Бавлинский район — также:"
    }'::json,
    '{"raicentr": "Бавлы"}'::json,
    4096,
    '["Бавлы", "Исергапово", "Крым-Сарай", "Новозареченск", "Покровский Урустамак", "Потапово-Тумбарла", "Татарский Кандыз", "Татарская Тумбарла", "Удмуртские Ташлы", "Миннигулово"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'drozhzhanovsky',
    '{
        "novost": "Новости Дрожжановского района:",
        "reklama": "Объявления Дрожжановского района:",
        "kultura": "Культура Дрожжановского района:",
        "sport": "Спорт Дрожжановского района:",
        "admin": "Власть и общество Дрожжановского района:",
        "union": "Молодёжь и образование Дрожжановского района:",
        "detsad": "Детские сады Дрожжановского района:",
        "sosed": "Происшествия Дрожжановского района:",
        "addons": "Дрожжановский район — также:"
    }'::json,
    '{"raicentr": "Старое Дрожжаное"}'::json,
    4096,
    '["Старое Дрожжаное", "Новое Дрожжаное", "Хорновар-Шигали", "Нижний Каракитан", "Нижнее Чекурское", "Новые Ишли", "Убей", "Большая Цильна", "Малая Цильна", "Татарский Саплык"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'tukaysky',
    '{
        "novost": "Новости Тукаевского района:",
        "reklama": "Объявления Тукаевского района:",
        "kultura": "Культура Тукаевского района:",
        "sport": "Спорт Тукаевского района:",
        "admin": "Власть и общество Тукаевского района:",
        "union": "Молодёжь и образование Тукаевского района:",
        "detsad": "Детские сады Тукаевского района:",
        "sosed": "Происшествия Тукаевского района:",
        "addons": "Тукаевский район — также:"
    }'::json,
    '{"raicentr": "Тукаевский"}'::json,
    4096,
    '["Старое Абдулово", "Биклянь", "Биюрган", "Бурды", "Иштеряково", "Кузкеево", "Малая Шильна", "Нижний Суык-Су", "Старый Дрюш", "Семекеево"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующих в `regions` (все три района
-- в сети). Уже живым — текущее значение с прода ПЛЮС новые коды, по
-- алфавиту. Идемпотентно: повторный прогон не дублирует.
UPDATE regions SET neighbors = 'bugulminsky,yutazinsky' WHERE code = 'bavlinsky';
UPDATE regions SET neighbors = 'buinsky' WHERE code = 'drozhzhanovsky';
UPDATE regions SET neighbors = 'agryzsky,elabuga,mendeleevsk,menzelinsky,nizhnekamsk,sarmanovsky,zainsky' WHERE code = 'tukaysky';

-- Обратные связи для уже живых (идемпотентно: добавляем новый код, если его
-- ещё нет в списке).
UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'bavlinsky'
    WHEN neighbors LIKE '%bavlinsky%' THEN neighbors
    ELSE neighbors || ',bavlinsky'
  END
WHERE code = 'bugulminsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'bavlinsky'
    WHEN neighbors LIKE '%bavlinsky%' THEN neighbors
    ELSE neighbors || ',bavlinsky'
  END
WHERE code = 'yutazinsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'drozhzhanovsky'
    WHEN neighbors LIKE '%drozhzhanovsky%' THEN neighbors
    ELSE neighbors || ',drozhzhanovsky'
  END
WHERE code = 'buinsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'tukaysky'
    WHEN neighbors LIKE '%tukaysky%' THEN neighbors
    ELSE neighbors || ',tukaysky'
  END
WHERE code IN ('agryzsky', 'elabuga', 'mendeleevsk', 'menzelinsky', 'nizhnekamsk', 'sarmanovsky', 'zainsky');

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('bavlinsky','drozhzhanovsky','tukaysky','bugulminsky','yutazinsky',
--                   'buinsky','agryzsky','elabuga','mendeleevsk','menzelinsky',
--                   'nizhnekamsk','sarmanovsky','zainsky')
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
-- Rollback (текущие значения соседей на прода на 2026-11-24):
-- UPDATE regions SET neighbors = 'leninogorsky,almetyevsky,aznakaevsky,yutazinsky' WHERE code = 'bugulminsky';
-- UPDATE regions SET neighbors = 'aznakaevsky,bugulminsky' WHERE code = 'yutazinsky';
-- UPDATE regions SET neighbors = 'apastovsky,tetyushsky' WHERE code = 'buinsky';
-- UPDATE regions SET neighbors = 'mendeleevsk,menzelinsky' WHERE code = 'agryzsky';
-- UPDATE regions SET neighbors = 'mamadysh,mendeleevsk,nizhnekamsk' WHERE code = 'elabuga';
-- UPDATE regions SET neighbors = 'elabuga,agryzsky' WHERE code = 'mendeleevsk';
-- UPDATE regions SET neighbors = 'muslyumovsky,sarmanovsky,agryzsky,aktanyshsky' WHERE code = 'menzelinsky';
-- UPDATE regions SET neighbors = 'chistopolsky,elabuga,mamadysh,almetyevsky,zainsky' WHERE code = 'nizhnekamsk';
-- UPDATE regions SET neighbors = 'almetyevsky,aznakaevsky,menzelinsky,muslyumovsky,zainsky' WHERE code = 'sarmanovsky';
-- UPDATE regions SET neighbors = 'almetyevsky,nizhnekamsk,sarmanovsky' WHERE code = 'zainsky';
-- DELETE FROM region_configs WHERE region_code IN ('bavlinsky','drozhzhanovsky','tukaysky');
-- DELETE FROM regions WHERE code IN ('bavlinsky','drozhzhanovsky','tukaysky');
