-- 110: neighbor symmetry repair for Tatarstan batches №6 and №7.
--
-- Why this exists: `regions.neighbors` drives the neighbor-news cascade
-- (`share_neighbor_news`) — a district posts what its neighbors posted. That
-- makes symmetry a data invariant, not a preference: a one-sided link means
-- the cascade silently runs in one direction and nobody notices, because the
-- missing half produces no error, no log line and no empty post.
--
-- The invariant was only ever written as a comment in the batch migrations, so
-- nothing enforced it. Portion №7 (109) hit it three times in one sitting:
--   • `kamsko_ustinsky` listed itself;
--   • `apastovsky` listed itself;
--   • `kaybitsky` forgot to list `tetyushsky` while `tetyushsky` listed
--     `kamsko_ustinsky` — one-sided, exactly the cascade bug.
-- Two of the three (the self-links) are structurally invisible to a symmetry
-- check: a code is symmetric with itself by construction. That is why the
-- checks below are three separate queries, not one.
--
-- Migration 107 (batch №6, 2026-09-29) left four one-sided links of its own,
-- found by the same query. They are fixed here rather than left as background
-- noise, because they are the same defect as the one this migration was
-- written for:
--   chistopolsky → nizhnekamsk        (nizhnekamsk did not list it back)
--   chistopolsky → rybnaya_sloboda    (rybnaya_sloboda did not list it back)
--   kaybitsky → vysokaya_gora         (vysokaya_gora did not list it back)
--   mamadysh → kaybitsky              (kaybitsky did not list it back)
--
-- All UPDATEs are absolute assignments of the full list, so this migration is
-- idempotent: re-running it converges rather than appending twice. Values below
-- were read from prod on 2026-10-01, not reconstructed from memory.
--
-- Rollback (returns the four districts to the batch-№6 values, undoing the
-- reverse links this migration added; the 109 self-link fixes are rolled back
-- by re-applying 109's own rollback block):
-- UPDATE regions SET neighbors = 'elabuga,mamadysh' WHERE code = 'nizhnekamsk';
-- UPDATE regions SET neighbors = 'laishevo,mamadysh,pestretsy,saby,tyulyachi' WHERE code = 'rybnaya_sloboda';
-- UPDATE regions SET neighbors = 'arsk,atnya,pestretsy,zelenodolsk' WHERE code = 'vysokaya_gora';
-- UPDATE regions SET neighbors = 'zelenodolsk,vysokaya_gora,apastovsky,chistopolsky,kamsko_ustinsky,verhniy_uslon' WHERE code = 'kaybitsky';

BEGIN;

-- Обратные ссылки батча №6 (добавляем недостающие половины):
UPDATE regions SET neighbors = 'chistopolsky,elabuga,mamadysh' WHERE code = 'nizhnekamsk';
UPDATE regions SET neighbors = 'chistopolsky,laishevo,mamadysh,pestretsy,saby,tyulyachi' WHERE code = 'rybnaya_sloboda';
UPDATE regions SET neighbors = 'arsk,atnya,kaybitsky,pestretsy,zelenodolsk' WHERE code = 'vysokaya_gora';
UPDATE regions SET neighbors = 'apastovsky,chistopolsky,kamsko_ustinsky,mamadysh,verhniy_uslon,vysokaya_gora,zelenodolsk' WHERE code = 'kaybitsky';

COMMIT;

-- Три независимые проверки. Все три должны вернуть пустую выдачу.
-- 1) Симметрия (односторонних связей нет):
--    SELECT a.code, b.code FROM regions a JOIN regions b
--      ON b.code = ANY(string_to_array(a.neighbors, ','))
--    WHERE NOT (a.code = ANY(string_to_array(coalesce(b.neighbors,''), ',')));
--
-- 2) Самоссылки (район не сосед сам с собой; ловит то, чего не видит п. 1):
--    SELECT code FROM regions
--    WHERE code = ANY(string_to_array(coalesce(neighbors,''), ','));
--
-- 3) Висящие коды (каждый сосед существует в regions):
--    SELECT a.code, b.code FROM regions a,
--      unnest(string_to_array(coalesce(a.neighbors,''), ',')) AS b(code)
--    WHERE b.code <> '' AND NOT EXISTS (SELECT 1 FROM regions r WHERE r.code = b.code);
