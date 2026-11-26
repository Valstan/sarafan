# P251 — Локальный стенд не собирается из репозитория: env-файл вне git, а `install-stand.ps1`, описанный в README, отсутствует

**Приоритет:** 🟡 техдолг (стенд живёт, но воспроизводимости нет)
**Открыто:** 2026-11-26 · `⏱ 2026-11-26 · snooze 0 · fresh`

## Симптом

Замер 26.11 (после переезда боевого контура на локальную машину):

- `infra/windows/README.md` перечисляет файл `install-stand.ps1` —
  «развёртывание стенда с нуля», — **но такого файла в каталоге нет**
  (`backup_db.ps1`, `classifier_parity.py`, `common.ps1`, `hidden_run.vbs`,
  `local_env.py`, `prod_counters.sh`, `prod_dump.sh`, `README.md`,
  `register-scheduled-tasks.ps1`, `restore_db.ps1`, `setup_minimax_area.py`,
  `start-*.ps1`). То есть README описывает несуществующий шаг.
- env-файл стенда — `config/local-stand.env` — **под gitignore**
  (`.gitignore`: `config/*.env`), и в `git ls-files config/` его нет: там
  только `setka.env.example`. 79 переменных боевого env, перенесённых на
  локалку, живут **только на этой машине** и только в этом файле.
- Часть перенесённого живёт **и вообще в одном экземпляре**: ключ подписи
  `radar_id` и фото рекламодателей забраны с бокса (по handoff 26.11).

## Чем это грозит

Стенд — теперь боевой контур, а не стенд. При этом поднять его заново из
репозитория **нельзя**: нет ни скрипта установки, ни шаблона env, ни списка
обязательных переопределений. Любая переустановка Windows, смена машины или
переезд на другого хоста превращаются в ручную работу по памяти — с риском
перепутать машинные и боевые значения (`DATABASE_URL` на 5433, `REDIS_URL`,
`MINIMAX_ACCOUNTS_DIR`, `MINIMAX_CLI`, `RADAR_ID_PRIVATE_KEY_FILE`,
`AD_UPLOAD_DIR`, `PROMO_NETWORK_LIST_URL`, `NODE_OPTIONS`).

## Что делать

1. **Дописать `infra/windows/install-stand.ps1`** под то, что README уже
   обещает: поднять PostgreSQL/Redis, скопировать базу, зарегистрировать
   задачи планировщика, разложить `venv`, принять env-файл.
2. **Завести в репозиторий шаблон без секретов** — `config/local-stand.env.example`
   — со всеми переопределениями машинных переменных и комментарием, что
   переопределено и почему. Секреты в шаблон **не** попадают: значения
   боевого env подтягиваются процедурой переноса, а ключи — комнатой
   (`bootstrap_secrets`).
3. **Список обязательного** — короткий раздел в `infra/windows/README.md`:
   что должно быть в env-файле стенда, чтобы он считался боевым.

## Связанное

- `infra/windows/README.md`, `infra/windows/local_env.py` (загрузка env-файла
  самим процессом — на Windows нет `EnvironmentFile=`).
- `docs/MIGRATION_TO_WINDOWS.md` — план переезда.
- [P249](P249-fizicheskie-shagi-posle-pereezda-udalit-vps-dns-sosedi.md) —
  физические шаги после переезда.
