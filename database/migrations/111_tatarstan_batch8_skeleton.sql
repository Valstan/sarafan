-- 111: skeleton for the eighth Tatarstan batch: novosheshminsky (Новошешминский),
-- aksubaevsky (Аксубаевский), alekseevsky (Алексеевский). Same pattern as 101-110:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders read from
-- ru.wikipedia on 2026-10-02, not from memory:
--   Новошешминский — Черемшанский, Аксубаевский, Чистопольский, Нижнекамский,
--                  Альметьевский. 13 282 чел. (2021); адм. центр село
--                  Новошешминск. Touches TWO of ours — Чистопольский +
--                  Аксубаевский (new).
--   Аксубаевский   — Чистопольский, Новошешминский, Черемшанский, Нурлатский,
--                  Алексеевский. 27 102 чел. (2021); адм. центр пгт Аксубаево.
--                  Touches TWO of ours — Чистопольский + Алексеевский (new).
--   Алексеевский   — Чистопольский, Аксубаевский, Нурлатский, Алькеевский,
--                  Спасский; по акватории Куйбышевского вдхр. — Лаишевский и
--                  Рыбно-Слободский. 24 924 чел. (2021); адм. центр
--                  пгт Алексеевское. Touches FOUR of ours — Чистопольский,
--                  Спасский, Лаишево (вода), Рыбно-Слободский (вода).
--
-- The trio is a connected chain (Новошешминский — Аксубаевский — Алексеевский),
-- same shape as batch №7 (Апастовский — Тетюшский — Спасский). Live edges with
-- multiplicity: Чистопольский x3, Спасский x1 (+ вода x2) — the most of any
-- connected three-district option still on the table.
--
-- ⚠️ DELIBERATE DEVIATION from the 109 header memo, verified 2026-10-02. That
-- memo named "south-east trio (Новошешминский + Ильшевский + Мензелинский)".
-- «Ильшевский» exists in no district list (typo for Алькеевский), and neither
-- Алькеевский nor Мензелинский shares a border with Новошешминский (both
-- geography sections list five neighbours each, mutually absent) — that trio
-- has ZERO internal edges and is not a cluster at all. The memo's "five edges"
-- do not reproduce from verified borders either. Evidence over memo.
-- Leftover for later: Алькеевский (touches Спасский + Алексеевский once live),
-- Нурлатский, Черемшанский, Мензелинский.
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts).
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109 — paired or generic names pull wrong villages in discovery):
--   • Новошешминский: «Слобода Волчья», «Слобода Архангельская»,
--     «Слобода Екатерининская», «Слобода Петропавловская» («Слобода» — тип
--     поселения, таких пяток только здесь; оставлена одна — Черемуховая);
--     «Русская/Чувашская Чебоксарка» — взята только Чувашская;
--     «Совхоз "Красный Октябрь"», «Гарь», «Урганча» — generic/короткие.
--   • Аксубаевский: пары «Новое/Старое Узеево», «Новая/Старая Киреметь»,
--     «Новое/Старое Ибрайкино», «Старые/Нижние Савруши» — взято по одному;
--     «Новое Аксубаево» — дубль имени райцентра, вместо него Старое Ибрайкино;
--     «Тимошкино», «Киязлы», «Адам» — только парами или россыпью, пропущены.
--   • Алексеевский: пары «Большие/Средние Тиганы», «Мокрые/Сухие Курнали»,
--     «Верхняя Татарская/Чувашская Майна», «Подлесная/Степная Шентала» —
--     пропущены целиком; «Лебяжье» — стем-пара к взятому «Лебедино»;
--     «Большие Полянки» — generic.
--
-- Group-name length check (VK limit 48): «НОВОШЕШМИНСКИЙ - ИНФО | Афиша,
-- новости, события» = 46; «АКСУБАЕВСКИЙ ...» = 43; «АЛЕКСЕЕВСКИЙ ...» = 44.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('novosheshminsky', 'НОВОШЕШМИНСКИЙ - ИНФО'),
    ('aksubaevsky', 'АКСУБАЕВСКИЙ - ИНФО'),
    ('alekseevsky', 'АЛЕКСЕЕВСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'novosheshminsky',
    '{
        "novost": "Новости Новошешминского района:",
        "reklama": "Объявления Новошешминского района:",
        "kultura": "Культура Новошешминского района:",
        "sport": "Спорт Новошешминского района:",
        "admin": "Власть и общество Новошешминского района:",
        "union": "Молодёжь и образование Новошешминского района:",
        "detsad": "Детские сады Новошешминского района:",
        "sosed": "Происшествия Новошешминского района:",
        "addons": "Новошешминский район — также:"
    }'::json,
    '{"raicentr": "Новошешминск"}'::json,
    4096,
    '["Новошешминск", "Азеево", "Акбуре", "Ерыклы", "Ленино", "Тубылгы Тау", "Татарское Утяшкино", "Чувашская Чебоксарка", "Шахмайкино", "Простые Челны"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'aksubaevsky',
    '{
        "novost": "Новости Аксубаевского района:",
        "reklama": "Объявления Аксубаевского района:",
        "kultura": "Культура Аксубаевского района:",
        "sport": "Спорт Аксубаевского района:",
        "admin": "Власть и общество Аксубаевского района:",
        "union": "Молодёжь и образование Аксубаевского района:",
        "detsad": "Детские сады Аксубаевского района:",
        "sosed": "Происшествия Аксубаевского района:",
        "addons": "Аксубаевский район — также:"
    }'::json,
    '{"raicentr": "Аксубаево"}'::json,
    4096,
    '["Аксубаево", "Новое Узеево", "Емелькино", "Караса", "Кривоозерки", "Мюд", "Новая Киреметь", "Старые Савруши", "Старое Ибрайкино", "Сунчелеево"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'alekseevsky',
    '{
        "novost": "Новости Алексеевского района:",
        "reklama": "Объявления Алексеевского района:",
        "kultura": "Культура Алексеевского района:",
        "sport": "Спорт Алексеевского района:",
        "admin": "Власть и общество Алексеевского района:",
        "union": "Молодёжь и образование Алексеевского района:",
        "detsad": "Детские сады Алексеевского района:",
        "sosed": "Происшествия Алексеевского района:",
        "addons": "Алексеевский район — также:"
    }'::json,
    '{"raicentr": "Алексеевское"}'::json,
    4096,
    '["Алексеевское", "Билярск", "Войкино", "Ерыкла", "Лебедино", "Левашево", "Родники", "Ромодан", "Сахаровка", "Саконы"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Черемшанский,
-- Нурлатский, Алькеевский, Нижнекамский, Альметьевский в сети не заведены).
-- Уже живым — текущее значение с прода (снято 2026-10-02) ПЛЮС новые коды,
-- по алфавиту. Водные границы — как в 109 (прецедент: Спасский): Алексеевский
-- соседствует с Лаишево и Рыбной Слободой по акватории вдхр.
UPDATE regions SET neighbors = 'aksubaevsky,chistopolsky' WHERE code = 'novosheshminsky';
UPDATE regions SET neighbors = 'alekseevsky,chistopolsky,novosheshminsky' WHERE code = 'aksubaevsky';
UPDATE regions SET neighbors = 'aksubaevsky,chistopolsky,laishevo,rybnaya_sloboda,spassky' WHERE code = 'alekseevsky';

-- Обратные связи для уже живых (сняты с прода 2026-10-02, см. проверку выше).
UPDATE regions SET neighbors = 'aksubaevsky,alekseevsky,kamsko_ustinsky,kaybitsky,nizhnekamsk,novosheshminsky,rybnaya_sloboda' WHERE code = 'chistopolsky';
UPDATE regions SET neighbors = 'alekseevsky,kamsko_ustinsky,laishevo,tetyushsky' WHERE code = 'spassky';
UPDATE regions SET neighbors = 'alekseevsky,kamsko_ustinsky,pestretsy,rybnaya_sloboda,spassky,verhniy_uslon' WHERE code = 'laishevo';
UPDATE regions SET neighbors = 'alekseevsky,chistopolsky,laishevo,mamadysh,pestretsy,saby,tyulyachi' WHERE code = 'rybnaya_sloboda';

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('novosheshminsky','aksubaevsky','alekseevsky','chistopolsky',
--                   'spassky','laishevo','rybnaya_sloboda')
--   ORDER BY code;
-- Симметричность (пустая выдача = все связи двусторонние):
--   SELECT a.code, b.code FROM regions a JOIN regions b
--     ON b.code = ANY(string_to_array(a.neighbors, ','))
--   WHERE NOT (a.code = ANY(string_to_array(coalesce(b.neighbors,''), ',')));
-- Отсутствие «висящих» соседей (пустая выдача = все коды существуют):
--   SELECT a.code, b.code FROM regions a, unnest(string_to_array(coalesce(a.neighbors,''), ',')) AS b(code)
--   WHERE b.code <> '' AND NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = b.code);
-- Отсутствие самоссылок (эта проверка ловит то, чего принципиально не ловит
-- проверка симметричности — урок 109: список пишется руками):
--   SELECT code FROM regions
--   WHERE code = ANY(string_to_array(coalesce(neighbors,''), ','));
--
-- Rollback:
-- UPDATE regions SET neighbors = 'rybnaya_sloboda,nizhnekamsk,kaybitsky,kamsko_ustinsky' WHERE code = 'chistopolsky';
-- UPDATE regions SET neighbors = 'kamsko_ustinsky,laishevo,tetyushsky' WHERE code = 'spassky';
-- UPDATE regions SET neighbors = 'kamsko_ustinsky,pestretsy,rybnaya_sloboda,spassky,verhniy_uslon' WHERE code = 'laishevo';
-- UPDATE regions SET neighbors = 'chistopolsky,laishevo,mamadysh,pestretsy,saby,tyulyachi' WHERE code = 'rybnaya_sloboda';
-- DELETE FROM region_configs WHERE region_code IN ('novosheshminsky','aksubaevsky','alekseevsky');
-- DELETE FROM regions WHERE code IN ('novosheshminsky','aksubaevsky','alekseevsky');
