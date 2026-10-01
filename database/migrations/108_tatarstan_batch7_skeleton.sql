-- 108: skeleton for the seventh Tatarstan batch: apastovsky (Апастовский),
-- tetyushsky (Тетюшский), spassky (Спасский). Same pattern as 101–107:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders read from
-- ru.wikipedia on 2026-10-01, not from memory:
--   Апастовский  — Буинский, Тетюшский, Камско-Устьинский, Верхнеуслонский,
--                  Кайбицкий, + Яльчинский (Чувашия). 19 093 чел. (2021);
--                  адм. центр пгт Апастово. **Touches THREE of ours** —
--                  Камское Устье, Верхний Услон, Кайбицы.
--   Тетюшский    — Ульяновская обл., Буинский, Апастовский, Камско-Устьинский.
--                  21 584 чел.; адм. центр г. Тетюши. Touches Камское Устье.
--   Спасский     — Алькеевский, Алексеевский, Ульяновская обл.; по акватории
--                  Волги — Тетюшский и Камско-Устьинский, по акватории Камы —
--                  Лаишевский. 18 599 чел.; адм. центр г. Болгар.
--
-- Total six edges into the live patch, the most of any three-district option
-- still on the table, and the chain closes the south-west gap that batch №6
-- (Камское Устье / Кайбицы / Чистополь) left open between Лаишево and
-- Чистополь. The alternative south-east trio (Новошешминский + Ильшевский +
-- Мензелинский) scores five edges and is left intact for a later portion.
--
-- ⚠️ Side effect worth naming: kaybitsky.neighbors ALREADY listed `apastovsky`
-- from migration 107 (which wrote codes for districts that did not exist yet,
-- against its own stated rule). This migration makes that dangling reference
-- valid — no fixup needed, and the reason the trio was worth checking first.
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts).
--
-- Localities: administrative centres of сельские/городские поселения per
-- ru.wikipedia, ten per district. Deliberately left out:
--   • Апастовский: «Среднее Балтаево», «Старый Юмралы», «Малые Болгояры» —
--     stems «Среднее/Старые/Малые» pull homonyms in discovery;
--   • Тетюшский: «Большие Атряси», «Большие Тарханы», «Большая Турма»
--     (same «Большие» stem; one of them left in on purpose, the town is known);
--   • Спасский: «Полянки», «Средний Юрткуль», «Чэчэкле», «Ямбухтино»,
--     «Антоновка» — generic or too short for a locality search.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('apastovsky', 'АПАСТОВСКИЙ - ИНФО'),
    ('tetyushsky', 'ТЕТЮШСКИЙ - ИНФО'),
    ('spassky', 'СПАССКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'apastovsky',
    '{
        "novost": "Новости Апастовского района:",
        "reklama": "Объявления Апастовского района:",
        "kultura": "Культура Апастовского района:",
        "sport": "Спорт Апастовского района:",
        "admin": "Власть и общество Апастовского района:",
        "union": "Молодёжь и образование Апастовского района:",
        "detsad": "Детские сады Апастовского района:",
        "sosed": "Происшествия Апастовского района:",
        "addons": "Апастовский район — также:"
    }'::json,
    '{"raicentr": "Апастово"}'::json,
    4096,
    '["Апастово", "Каратун", "Черемшан", "Сатламышево", "Куштово", "Бишево", "Деушево", "Эбалаково", "Чуру-Барышево", "Кзыл-Тау"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'tetyushsky',
    '{
        "novost": "Новости Тетюшского района:",
        "reklama": "Объявления Тетюшского района:",
        "kultura": "Культура Тетюшского района:",
        "sport": "Спорт Тетюшского района:",
        "admin": "Власть и общество Тетюшского района:",
        "union": "Молодёжь и образование Тетюшского района:",
        "detsad": "Детские сады Тетюшского района:",
        "sosed": "Происшествия Тетюшского района:",
        "addons": "Тетюшский район — также:"
    }'::json,
    '{"raicentr": "Тетюши"}'::json,
    4096,
    '["Тетюши", "Сюндюково", "Льяшево", "Кляшево", "Монастырское", "Кильдюшево", "Федоровка", "Алабердино", "Байрашево", "Большое Шемякино"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'spassky',
    '{
        "novost": "Новости Спасского района:",
        "reklama": "Объявления Спасского района:",
        "kultura": "Культура Спасского района:",
        "sport": "Спорт Спасского района:",
        "admin": "Власть и общество Спасского района:",
        "union": "Молодёжь и образование Спасского района:",
        "detsad": "Детские сады Спасского района:",
        "sosed": "Происшествия Спасского района:",
        "addons": "Спасский район — также:"
    }'::json,
    '{"raicentr": "Болгар"}'::json,
    4096,
    '["Болгар", "Аграмаковка", "Бураково", "Измери", "Иске-Рязап", "Красная Слобода", "Кузнечиха", "Никольское", "Три Озера", "Куралово"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Буинский, Алькеевский,
-- Алексеевский в сети не заведены). Уже живым — текущее значение с прода
-- (снято 2026-10-01) ПЛЮС новые коды, по алфавиту.
UPDATE regions SET neighbors = 'apastovsky,tetyushsky,kamsko_ustinsky,verhniy_uslon,kaybitsky' WHERE code = 'apastovsky';
UPDATE regions SET neighbors = 'apastovsky,kamsko_ustinsky,spassky' WHERE code = 'tetyushsky';
UPDATE regions SET neighbors = 'kamsko_ustinsky,laishevo,tetyushsky' WHERE code = 'spassky';

-- Обратные связи для уже живых:
UPDATE regions SET neighbors = 'apastovsky,chistopolsky,kamsko_ustinsky,kaybitsky,laishevo,spassky,verhniy_uslon' WHERE code = 'kamsko_ustinsky';
UPDATE regions SET neighbors = 'apastovsky,kamsko_ustinsky,kaybitsky,laishevo,zelenodolsk' WHERE code = 'verhniy_uslon';
UPDATE regions SET neighbors = 'kamsko_ustinsky,pestretsy,rybnaya_sloboda,spassky,verhniy_uslon' WHERE code = 'laishevo';
-- kaybitsky: `apastovsky` уже прописан миграцией 107 — менять нечего.

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('apastovsky','tetyushsky','spassky','kamsko_ustinsky',
--                   'verhniy_uslon','laishevo','kaybitsky')
--   ORDER BY code;
-- Симметричность (пустая выдача = все связи двусторонние):
--   SELECT a.code, b.code FROM regions a JOIN regions b
--     ON b.code = ANY(string_to_array(a.neighbors, ','))
--   WHERE NOT (a.code = ANY(string_to_array(coalesce(b.neighbors,''), ',')));
-- Отсутствие «висящих» соседей (пустая выдача = все коды существуют):
--   SELECT a.code, b.code FROM regions a, unnest(string_to_array(coalesce(a.neighbors,''), ',')) AS b(code)
--   WHERE b.code <> '' AND NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = b.code);
--
-- Rollback (возврат к состоянию после 107, снятому с прода 2026-09-29):
-- UPDATE regions SET neighbors = 'laishevo,verhniy_uslon,kaybitsky,chistopolsky' WHERE code = 'kamsko_ustinsky';
-- UPDATE regions SET neighbors = 'laishevo,zelenodolsk,kamsko_ustinsky,kaybitsky' WHERE code = 'verhniy_uslon';
-- UPDATE regions SET neighbors = 'pestretsy,rybnaya_sloboda,kamsko_ustinsky,verhniy_uslon' WHERE code = 'laishevo';
-- DELETE FROM region_configs WHERE region_code IN ('apastovsky','tetyushsky','spassky');
-- DELETE FROM regions WHERE code IN ('apastovsky','tetyushsky','spassky');
