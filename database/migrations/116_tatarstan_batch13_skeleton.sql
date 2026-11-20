-- 116: skeleton for the thirteenth Tatarstan batch: agryzsky (Агрызский),
-- aktanyshsky (Актанышский), yutazinsky (Ютазинский). Same pattern as 101-115:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders verified 2026-11-20
-- from ru.wikipedia, not from memory:
--   Агрызский    — W/N/E: Удмуртия (Алнашский, Можгинский, Малопургинский,
--                  Киясовский, Сарапульский, Каракулинский); S, across the
--                  Нижнекамское водохранилище: Мензелинский, Тукаевский,
--                  Менделеевский. 36 012 чел. (2021); адм. центр город Агрыз.
--                  Touches TWO of ours — Менделеевский, Мензелинский
--                  (Тукаевский — сознательно пропущен, как и раньше).
--   Актанышский  — Мензелинский, Муслюмовский + Башкортостан (Илишевский,
--                  Краснокамский, Бакалинский, Агидель) + Удмуртия
--                  (Каракулинский). 29 384 чел. (2020); адм. центр село
--                  Актаныш, 97,5 % татар, село на 100 % сельское.
--                  Touches TWO of ours — Мензелинский, Муслюмовский.
--   Ютазинский   — Азнакаевский, Бугульминский, Бавлинский + Башкортостан
--                  (Туймазинский, Шаранский, Октябрьский). 20 248 чел.
--                  (2020); адм. центр пгт Уруссу. Touches TWO of ours —
--                  Азнакаевский, Бугульминский.
--
-- Итого 6 живых рёбер (Агрызский ×2, Актанышский ×2, Ютазинский ×2) — максимум
-- среди связных троек из пяти неразобранных (Агрызский, Актанышский, Бавлинский,
-- Дрожжановский, Ютазинский; Тукаевский — сознательно пропущен). Замкнутого
-- треугольника среди них нет: единственное внутреннее ребро — Бавлинский ↔
-- Ютазинский, а связность с живым кустом у этой пары на ребро меньше
-- (Бавлинский — только Бугульминский, Дрожжановский — только Буинский).
--
-- Три района закрывают две дыры сети: северо-восток (Агрызский + Актанышский,
-- оба висят на Мензелинском/Менделеевском) и юго-восток (Ютазинский, оба края —
-- на Азнакаевском и Бугульминском).
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts, not okrugs). raicentr goes into hashtags (#<raicentr>), so no spaces:
-- «пгт Уруссу» → «Уруссу».
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109-115 — paired or generic names pull wrong villages in discovery):
--   • Агрызский: «Красный Бор» (generic), «Новое Аккузино»/«Старый Кзыл-Яр»
--     (generic stems), «Уразаево» — есть и в Актанышском районе, «Зуево»
--     (generic), «Сосново» (generic).
--   • Актанышский: «Михайловка» (generic, тот же канон), «Старое Айманово»
--     (generic prefix), «Каенлык» — есть и в Нижнекамском районе, «Чураево»
--     (общий стем с «Чуракаево»), «Шабизбаш» (общий стем с «Шабызово»),
--     «Актанышбаш» (общий стем с «Актаныш»).
--   • Ютазинский: «Старый Каразерик»/«Новый Каразерик» (generic prefix),
--     «Байряки-Тамак» (общий стем «Тамак» с Сармановским/Муслюмовским),
--     «Дым-Тамак» (тот же стем), «Салкын-Чишма» (общий стем «Чишма» с
--     Актанышским и Агрызским), «Алма-Ата» (встречается в чужих районах).
--
-- Group-name length check (VK limit 48): «АГРЫЗСКИЙ ...» = 41;
-- «АКТАНЫШСКИЙ ...» = 43; «ЮТАЗИНСКИЙ ...» = 42.
--
-- Short addresses follow the canonical template `<город латиницей>_info<код региона>`:
-- Татарстан = 16 → agryz_info16, aktanysh_info16, urusu_info16.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('agryzsky', 'АГРЫЗСКИЙ - ИНФО'),
    ('aktanyshsky', 'АКТАНЫШСКИЙ - ИНФО'),
    ('yutazinsky', 'ЮТАЗИНСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'agryzsky',
    '{
        "novost": "Новости Агрызского района:",
        "reklama": "Объявления Агрызского района:",
        "kultura": "Культура Агрызского района:",
        "sport": "Спорт Агрызского района:",
        "admin": "Власть и общество Агрызского района:",
        "union": "Молодёжь и образование Агрызского района:",
        "detsad": "Детские сады Агрызского района:",
        "sosed": "Происшествия Агрызского района:",
        "addons": "Агрызский район — также:"
    }'::json,
    '{"raicentr": "Агрыз"}'::json,
    4096,
    '["Агрыз", "Иж-Бобья", "Крынды", "Табарле", "Татарская Чильча", "Кудашево", "Назяр", "Мукшур", "Ямурзино", "Исенбаево"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'aktanyshsky',
    '{
        "novost": "Новости Актанышского района:",
        "reklama": "Объявления Актанышского района:",
        "kultura": "Культура Актанышского района:",
        "sport": "Спорт Актанышского района:",
        "admin": "Власть и общество Актанышского района:",
        "union": "Молодёжь и образование Актанышского района:",
        "detsad": "Детские сады Актанышского района:",
        "sosed": "Происшествия Актанышского района:",
        "addons": "Актанышский район — также:"
    }'::json,
    '{"raicentr": "Актаныш"}'::json,
    4096,
    '["Актаныш", "Поисево", "Шабызово", "Чуракаево", "Азякуль", "Татарские Ямалы", "Такталачук", "Миннярово", "Качкиново", "Шайчурино"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'yutazinsky',
    '{
        "novost": "Новости Ютазинского района:",
        "reklama": "Объявления Ютазинского района:",
        "kultura": "Культура Ютазинского района:",
        "sport": "Спорт Ютазинского района:",
        "admin": "Власть и общество Ютазинского района:",
        "union": "Молодёжь и образование Ютазинского района:",
        "detsad": "Детские сады Ютазинского района:",
        "sosed": "Происшествия Ютазинского района:",
        "addons": "Ютазинский район — также:"
    }'::json,
    '{"raicentr": "Уруссу"}'::json,
    4096,
    '["Уруссу", "Ютаза", "Абсалямово", "Акбаш", "Байлярово", "Каракашлы", "Каркале", "Исметьево", "Хуррият", "Яссы-Тугай"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующих и активных в `regions`
-- (Тукаевский в сети не заведён). Уже живым — текущее значение с прода ПЛЮС
-- новые коды, по алфавиту. Идемпотентно: повторный прогон не дублирует.
UPDATE regions SET neighbors = 'mendeleevsk,menzelinsky' WHERE code = 'agryzsky';
UPDATE regions SET neighbors = 'menzelinsky,muslyumovsky' WHERE code = 'aktanyshsky';
UPDATE regions SET neighbors = 'aznakaevsky,bugulminsky' WHERE code = 'yutazinsky';

-- Обратные связи для уже живых (идемпотентно: добавляем новый код, если его
-- ещё нет в списке).
UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'agryzsky'
    WHEN neighbors LIKE '%agryzsky%' THEN neighbors
    ELSE neighbors || ',agryzsky'
  END
WHERE code = 'mendeleevsk';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'agryzsky,aktanyshsky'
    WHEN neighbors LIKE '%agryzsky%' AND neighbors LIKE '%aktanyshsky%' THEN neighbors
    WHEN neighbors LIKE '%agryzsky%' THEN neighbors || ',aktanyshsky'
    WHEN neighbors LIKE '%aktanyshsky%' THEN neighbors || ',agryzsky'
    ELSE neighbors || ',agryzsky,aktanyshsky'
  END
WHERE code = 'menzelinsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'aktanyshsky'
    WHEN neighbors LIKE '%aktanyshsky%' THEN neighbors
    ELSE neighbors || ',aktanyshsky'
  END
WHERE code = 'muslyumovsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'yutazinsky'
    WHEN neighbors LIKE '%yutazinsky%' THEN neighbors
    ELSE neighbors || ',yutazinsky'
  END
WHERE code = 'aznakaevsky';

UPDATE regions SET neighbors =
  CASE
    WHEN neighbors IS NULL OR neighbors = '' THEN 'yutazinsky'
    WHEN neighbors LIKE '%yutazinsky%' THEN neighbors
    ELSE neighbors || ',yutazinsky'
  END
WHERE code = 'bugulminsky';

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('agryzsky','aktanyshsky','yutazinsky','mendeleevsk','menzelinsky',
--                   'muslyumovsky','aznakaevsky','bugulminsky')
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
-- Rollback (текущие значения соседей на прода на 2026-11-20):
-- UPDATE regions SET neighbors = 'elabuga' WHERE code = 'mendeleevsk';
-- UPDATE regions SET neighbors = 'muslyumovsky,sarmanovsky' WHERE code = 'menzelinsky';
-- UPDATE regions SET neighbors = 'aznakaevsky,menzelinsky,sarmanovsky' WHERE code = 'muslyumovsky';
-- UPDATE regions SET neighbors = 'almetyevsky,bugulminsky,muslyumovsky,sarmanovsky' WHERE code = 'aznakaevsky';
-- UPDATE regions SET neighbors = 'leninogorsky,almetyevsky,aznakaevsky' WHERE code = 'bugulminsky';
-- DELETE FROM region_configs WHERE region_code IN ('agryzsky','aktanyshsky','yutazinsky');
-- DELETE FROM regions WHERE code IN ('agryzsky','aktanyshsky','yutazinsky');
