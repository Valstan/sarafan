-- 112: skeleton for the ninth Tatarstan batch: alkeevsky (Алькеевский),
-- nurlatsky (Нурлатский), cheremshansky (Черемшанский). Same pattern as 101-111:
-- INACTIVE regions with vk_group_id NULL — the "- ИНФО" groups are created after
-- this migration. Additive and reversible.
--
-- Why these three (connectivity, per the canonical rule — pick the cluster that
-- closes a gap, not the one with the biggest pools). Borders verified 2026-10-04,
-- not from memory:
--   Алькеевский    — Спасский, Алексеевский, Нурлатский (+ Самарская и
--                    Ульяновская обл.). 18 481 чел. (2021, ВПН-2020); адм. центр
--                    село Базарные Матаки. Touches THREE of ours — Спасский,
--                    Алексеевский + Нурлатский (new).
--   Нурлатский     — Алькеевский, Алексеевский, Аксубаевский, Черемшанский
--                    (+ Самарская обл., Ульяновская обл.). 53 200 чел. (2021);
--                    адм. центр город Нурлат. Touches THREE of ours —
--                    Алексеевский, Аксубаевский + Черемшанский (new).
--   Черемшанский   — Нурлатский, Аксубаевский, Новошешминский, Альметьевский,
--                    Лениногорский (+ Самарская обл.). 18 371 чел. (2021); адм.
--                    центр село Черемшан. Touches THREE of ours —
--                    Аксубаевский, Новошешминский + Нурлатский (new).
--
-- The trio is a connected chain (Алькеевский — Нурлатский — Черемшанский),
-- same shape as batches №7 and №8. Live edges with multiplicity:
-- Аксубаевский x2, Алексеевский x2, Новошешминский x1, Спасский x1.
-- Leftover for later: Мензелинский (touches nobody live yet) and the far
-- east/south-east districts.
--
-- Branding: full 9-theme Kirov template, «района» (Tatarstan has municipal
-- districts).
--
-- Localities: administrative centres of городские/сельские поселения per
-- ru.wikipedia, ten per district. Deliberately left out (homonym stems, same
-- rule as 109/111 — paired or generic names pull wrong villages in discovery):
--   • Алькеевский: пары Верхнее/Нижнее/Среднее Алькеево, Верхнее/Нижнее Качеево,
--     Новые/Старые Ургагары, Новое/Старое Алпарово, Новое/Старое Камкино,
--     Верхнее/Нижнее Колчурино, Новые/Старые Челны, Русские/Татарские Шибаши,
--     Чувашское/Татарское Бурнаево, Старые/Новые/Нижние Салманы — пропущены;
--     «Кошки» — пропущено (домашние кошки в выдаче); взято по одному из парных
--     стемов, как «Чувашская Чебоксарка» в 111: Нижнее Алькеево (исторический
--     центр), Салманы.
--   • Нурлатский: пары Новая/Старая (Татарская/Русская) Амзя, Новое/Старое
--     Иглайкино, Новое/Старое Альметьево, Нижние/Средние/Старые Челны,
--     Биляр/Степное/Чёрное/Светлое/Кривое Озеро, Русский/Чувашский Тимерлек,
--     Средняя/Малая Камышла — пропущены; «Заречный» — generic, пропущен.
--   • Черемшанский: пары Верхняя/Нижняя Каменка, Верхняя/Нижняя Кармалка,
--     Новое/Старое Ильмово, Старые/Новые Кутуши, Старый/Подлесный Утямыш,
--     Старое Кадеево/Новокадеевское, Беркет/Чёрный Ключ — пропущены; взято одно
--     из пары, как в 111: Мордовское Афонькино (адм. центр, vs Чувашское).
--
-- Group-name length check (VK limit 48): «АЛЬКЕЕВСКИЙ - ИНФО | Афиша,
-- новости, события» = 44; «НУРЛАТСКИЙ ...» = 43; «ЧЕРЕМШАНСКИЙ ...» = 45.

BEGIN;

INSERT INTO regions (code, name, vk_group_id, kind, parent_region_id, is_active)
SELECT v.code, v.name, NULL, 'raion', (SELECT id FROM regions WHERE code = 'tatarstan_obl'), FALSE
FROM (VALUES
    ('alkeevsky', 'АЛЬКЕЕВСКИЙ - ИНФО'),
    ('nurlatsky', 'НУРЛАТСКИЙ - ИНФО'),
    ('cheremshansky', 'ЧЕРЕМШАНСКИЙ - ИНФО')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = v.code);

INSERT INTO region_configs (region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
SELECT * FROM (VALUES
(
    'alkeevsky',
    '{
        "novost": "Новости Алькеевского района:",
        "reklama": "Объявления Алькеевского района:",
        "kultura": "Культура Алькеевского района:",
        "sport": "Спорт Алькеевского района:",
        "admin": "Власть и общество Алькеевского района:",
        "union": "Молодёжь и образование Алькеевского района:",
        "detsad": "Детские сады Алькеевского района:",
        "sosed": "Происшествия Алькеевского района:",
        "addons": "Алькеевский район — также:"
    }'::json,
    '{"raicentr": "Базарные Матаки"}'::json,
    4096,
    '["Базарные Матаки", "Аппаково", "Борискино", "Каргополь", "Нижнее Алькеево", "Салманы", "Старая Хурада", "Тяжбердино", "Чувашский Брод", "Юхмачи"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'nurlatsky',
    '{
        "novost": "Новости Нурлатского района:",
        "reklama": "Объявления Нурлатского района:",
        "kultura": "Культура Нурлатского района:",
        "sport": "Спорт Нурлатского района:",
        "admin": "Власть и общество Нурлатского района:",
        "union": "Молодёжь и образование Нурлатского района:",
        "detsad": "Детские сады Нурлатского района:",
        "sosed": "Происшествия Нурлатского района:",
        "addons": "Нурлатский район — также:"
    }'::json,
    '{"raicentr": "Нурлат"}'::json,
    4096,
    '["Нурлат", "Андреевка", "Бурметьево", "Гайтанкино", "Егоркино", "Кульбаево-Мараса", "Мамыково", "Тюрнясево", "Фомкино", "Чулпаново"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
),
(
    'cheremshansky',
    '{
        "novost": "Новости Черемшанского района:",
        "reklama": "Объявления Черемшанского района:",
        "kultura": "Культура Черемшанского района:",
        "sport": "Спорт Черемшанского района:",
        "admin": "Власть и общество Черемшанского района:",
        "union": "Молодёжь и образование Черемшанского района:",
        "detsad": "Детские сады Черемшанского района:",
        "sosed": "Происшествия Черемшанского района:",
        "addons": "Черемшанский район — также:"
    }'::json,
    '{"raicentr": "Черемшан"}'::json,
    4096,
    '["Черемшан", "Ивашкино", "Карамышево", "Кутема", "Лашманка", "Мордовское Афонькино", "Старый Утямыш", "Туйметкино", "Ульяновка", "Утыз Имян"]'::json,
    (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
)
) AS v(region_code, zagolovki, heshteg_local, text_post_maxsize_simbols, localities, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM region_configs rc WHERE rc.region_code = v.region_code);

-- Соседи. Новым — только коды, существующие в `regions` (Альметьевский,
-- Лениногорский и области в сети не заведены). Уже живым — текущее значение
-- с прода (снято 2026-10-04, см. проверку выше) ПЛЮС новые коды, по алфавиту.
UPDATE regions SET neighbors = 'alekseevsky,nurlatsky,spassky' WHERE code = 'alkeevsky';
UPDATE regions SET neighbors = 'aksubaevsky,alekseevsky,alkeevsky,cheremshansky' WHERE code = 'nurlatsky';
UPDATE regions SET neighbors = 'aksubaevsky,novosheshminsky,nurlatsky' WHERE code = 'cheremshansky';

-- Обратные связи для уже живых (сняты с прода 2026-10-04, см. проверку выше).
UPDATE regions SET neighbors = 'alekseevsky,cheremshansky,chistopolsky,novosheshminsky,nurlatsky' WHERE code = 'aksubaevsky';
UPDATE regions SET neighbors = 'aksubaevsky,alkeevsky,chistopolsky,laishevo,nurlatsky,rybnaya_sloboda,spassky' WHERE code = 'alekseevsky';
UPDATE regions SET neighbors = 'aksubaevsky,cheremshansky,chistopolsky' WHERE code = 'novosheshminsky';
UPDATE regions SET neighbors = 'alekseevsky,alkeevsky,kamsko_ustinsky,laishevo,tetyushsky' WHERE code = 'spassky';

COMMIT;

-- Проверка после применения:
--   SELECT code, name, is_active, vk_group_id, neighbors FROM regions
--   WHERE code IN ('alkeevsky','nurlatsky','cheremshansky','aksubaevsky',
--                   'alekseevsky','novosheshminsky','spassky')
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
-- UPDATE regions SET neighbors = 'alekseevsky,chistopolsky,novosheshminsky' WHERE code = 'aksubaevsky';
-- UPDATE regions SET neighbors = 'aksubaevsky,chistopolsky,laishevo,rybnaya_sloboda,spassky' WHERE code = 'alekseevsky';
-- UPDATE regions SET neighbors = 'aksubaevsky,chistopolsky' WHERE code = 'novosheshminsky';
-- UPDATE regions SET neighbors = 'alekseevsky,kamsko_ustinsky,laishevo,tetyushsky' WHERE code = 'spassky';
-- DELETE FROM region_configs WHERE region_code IN ('alkeevsky','nurlatsky','cheremshansky');
-- DELETE FROM regions WHERE code IN ('alkeevsky','nurlatsky','cheremshansky');
