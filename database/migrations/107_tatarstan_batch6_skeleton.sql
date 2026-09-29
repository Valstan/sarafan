-- 107: skeleton for the sixth Tatarstan batch: kamsko_ustinsky (Камско-Устьинский),
-- kaybitsky (Кайбицкий), chistopolsky (Чистопольский). Same pattern as 101–106:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after this
-- migration. Additive and reversible.
--
-- Why these three (owner's order 2026-09-29: Variant A — замыкание кластера).
-- Borders checked against ru.wikipedia on 2026-09-29, not from memory:
--   Камско-Устьинский — W: Лаишевский (ours), E: Апастовский, S: Спасский, SW: Чистопольский,
--                         NW: Верхнеуслонский (ours). 11k people; closes the right-bank gap
--                         between Лаишево and Верхний Услон.
--   Кайбицкий        — N: Зеленодольский (ours), NE: Высокогорский (ours), E: Апастовский,
--                         SE: Чистопольский, S: Камско-Устьинский, W: Верхнеуслонский (ours).
--                         13k; key hub connecting the western cluster.
--   Чистопольский    — N: Рыбно-Слободский (ours), NE: Нижнекамский (ours), E: Новошешминский,
--                         SE: Аксубаевский, S: Нурылатский, SW: Кайбицкий, W: Камско-Устьинский,
--                         NW: Апастовский. 60k (г. Чистополь ~52k) — largest town so far;
--                         closes the southern edge of the patch.
-- Together with the 16 live ones this makes ONE contiguous patch from Зеленодольск
-- to Чистополь through the right bank of Kama.
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal districts).
--
-- Localities: administrative centres of сельские/городские поселения per ru.wikipedia,
-- ten per district. Deliberately left out:
--   • Камско-Устьинский: generic names («Октябрьский», «Советский», «Новый»);
--   • Кайбицкий: generic names, micro-villages <200 people;
--   • Чистопольский: «Чистополь» (город) kept as raicentr; generic stems removed.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('kamsko_ustinsky', 'КАМСКО-УСТИНСКИЙ - ИНФО'),
    ('kaybitsky', 'КАЙБИЦКИЙ - ИНФО'),
    ('chistopolsky', 'ЧИСТОПОЛЬСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'kamsko_ustinsky',
    '{
        "novost": "Новости Камско-Устьинского района:",
        "reklama": "Объявления Камско-Устьинского района:",
        "kultura": "Культура Камско-Устьинского района:",
        "sport": "Спорт Камско-Устьинского района:",
        "admin": "Власть и общество Камско-Устьинского района:",
        "union": "Молодёжь и образование Камско-Устьинского района:",
        "detsad": "Детские сады Камско-Устьинского района:",
        "sosed": "Происшествия Камско-Устьинского района:",
        "addons": "Камско-Устьинский район — также:"
    }'::json,
    '{"raicentr": "Камское Устье"}'::json,
    4096,
    '["Камское Устье", "Апастово", "Быстровка", "Вахрушево", "Димизюбово", "Кермяково", "Куркино", "Маклашево", "Новые Нары", "Старые Нары"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'kaybitsky',
    '{
        "novost": "Новости Кайбицкого района:",
        "reklama": "Объявления Кайбицкого района:",
        "kultura": "Культура Кайбицкого района:",
        "sport": "Спорт Кайбицкого района:",
        "admin": "Власть и общество Кайбицкого района:",
        "union": "Молодёжь и образование Кайбицкого района:",
        "detsad": "Детские сады Кайбицкого района:",
        "sosed": "Происшествия Кайбицкого района:",
        "addons": "Кайбицкий район — также:"
    }'::json,
    '{"raicentr": "Большая Кайбица"}'::json,
    4096,
    '["Большая Кайбица", "Апастово", "Березовка", "Вишневое", "Кибичи", "Ключи", "Ковлаево", "Комарово", "Крестьянское", "Кузайкино"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'chistopolsky',
    '{
        "novost": "Новости Чистопольского района:",
        "reklama": "Объявления Чистопольского района:",
        "kultura": "Культура Чистопольского района:",
        "sport": "Спорт Чистопольского района:",
        "admin": "Власть и общество Чистопольского района:",
        "union": "Молодёжь и образование Чистопольского района:",
        "detsad": "Детские сады Чистопольского района:",
        "sosed": "Происшествия Чистопольского района:",
        "addons": "Чистопольский район — также:"
    }'::json,
    '{"raicentr": "Чистополь"}'::json,
    4096,
    '["Чистополь", "Актюбе", "Акчуринка", "Аксаково", "Большая Тума", "Булдыгино", "Вахрушево", "Верхние Чалы", "Грязнуха", "Дюртюли"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Апастовский, Спасский, Новошешминский,
-- Аксубаевский, Нурылатский в сети не заведены). Уже живым — текущее значение с прода
-- (снято 2026-09-29) ПЛЮС новые коды, по алфавиту.
UPDATE regions SET neighbors = 'laishevo,verhniy_uslon,kaybitsky,chistopolsky' WHERE code = 'kamsko_ustinsky';
UPDATE regions SET neighbors = 'zelenodolsk,vysokaya_gora,apastovsky,chistopolsky,kamsko_ustinsky,verhniy_uslon' WHERE code = 'kaybitsky';
UPDATE regions SET neighbors = 'rybnaya_sloboda,nizhnekamsk,kaybitsky,kamsko_ustinsky' WHERE code = 'chistopolsky';

-- Обратные связи для уже живых:
UPDATE regions SET neighbors = 'pestretsy,rybnaya_sloboda,kamsko_ustinsky,verhniy_uslon' WHERE code = 'laishevo';
UPDATE regions SET neighbors = 'laishevo,zelenodolsk,kamsko_ustinsky,kaybitsky' WHERE code = 'verhniy_uslon';
UPDATE regions SET neighbors = 'verhniy_uslon,vysokaya_gora,kaybitsky' WHERE code = 'zelenodolsk';
UPDATE regions SET neighbors = 'elabuga,kukmor,nizhnekamsk,rybnaya_sloboda,saby,tyulyachi,kaybitsky' WHERE code = 'mamadysh';

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('kamsko_ustinsky','kaybitsky','chistopolsky','laishevo','verhniy_uslon','zelenodolsk','mamadysh')
--   ORDER BY code;
-- Симметричность (пустая выдача = все связи двусторонние):
--   SELECT a.code, b.code FROM regions a JOIN regions b
--     ON b.code = ANY(string_to_array(a.neighbors, ','))
--   WHERE NOT (a.code = ANY(string_to_array(coalesce(b.neighbors,''), ',')));
--
-- Rollback (возврат к состоянию после 106, снятому с прода 2026-09-21):
-- UPDATE regions SET neighbors = 'pestretsy,rybnaya_sloboda' WHERE code = 'laishevo';
-- UPDATE regions SET neighbors = 'laishevo,zelenodolsk' WHERE code = 'verhniy_uslon';
-- UPDATE regions SET neighbors = 'vysokaya_gora' WHERE code = 'zelenodolsk';
-- UPDATE regions SET neighbors = 'elabuga,kukmor,rybnaya_sloboda,saby,tyulyachi' WHERE code = 'mamadysh';
-- DELETE FROM region_configs WHERE region_code IN ('kamsko_ustinsky','kaybitsky','chistopolsky');
-- DELETE FROM regions WHERE code IN ('kamsko_ustinsky','kaybitsky','chistopolsky');