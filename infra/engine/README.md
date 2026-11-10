# infra/engine — локальный движок классификатора на самом боксе

**Что это.** Движок (opencode serve, free-тир моделей) и демон моста живут на
боксе: классификация идёт независимо от того, включён ли компьютер владельца.

| Файл | Что делает |
|---|---|
| `opencode-engine.sh` | обёртка: поднимает `opencode serve --service` и следит за портом, поднимая заново, если serve упал |
| `opencode-engine.service` | systemd-юнит обёртки (переживает перезагрузку, потолок памяти 512 М — бокс на 1,5 ГБ) |
| `local-engine.cron` | строка кроны: демон раз в 5 минут делает один круг (`--once`) |

**Почему обёртка, а не сам serve в юните.** OpenCode v2 в режиме `--service`
уходит в фон и пишет пароль сервера в `~/.config/opencode/service.json` —
оттуда его берёт клиент движка. Но systemd такой процесс не сторожит: юнит
завершится, serve останется сиротой. Обёртка совмещает «поднять в фоне» и
«сторожит порт».

**Почему free-тир без ключа.** Провайдер `opencode` (модели `opencode/*-free`)
отдаётся с публичным ключом, поэтому секрет на сервер не переносится: на
боксе нужен только сам opencode. Замер 10.11: `deepseek-v4.1-flash-free`
ответила с IP бокса без гейта по IP (ADR-0014: тот гейт тогда закрывал весь
каталог — значит, либо сменился адрес, либо правило смягчилось; проверять
надо было, а не предполагать).

**Установка на боксе:**

```bash
# 1. opencode (пакет @opencode/cli, версию держать как на ПК)
sudo -n npm install -g @opencode/cli@2.1.4

# 2. юнит движка
sudo -n install -m 0644 -o root -g root infra/engine/opencode-engine.service \
    /etc/systemd/system/opencode-engine.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now opencode-engine.service

# 3. крона демона (в кронтаб пользователя valstan)
crontab -l > /tmp/crontab.backup
cat /tmp/crontab.backup infra/engine/local-engine.cron | crontab -

# 4. проверка
systemctl status opencode-engine.service
ss -ltn | grep 4097
# API с авторизацией: пароль читается из файла в рантайме и в команде его нет
printf 'machine 127.0.0.1 login opencode password %s\n' \
  "$(jq -r .password ~/.config/opencode/service.json)" > ~/.opencode-netrc
chmod 600 ~/.opencode-netrc
curl -s --netrc-file ~/.opencode-netrc http://127.0.0.1:4097/api/model | head -c 200
```

**Проверка вручную (без кроны):**

```bash
cd /home/valstan/SETKA && ./venv/bin/python scripts/local_engine_daemon.py \
    --local --engine opencode --model-url http://127.0.0.1:4097 \
    --once --regions-per-chunk 2 --model-timeout 100
```

**Если free-тир отвалится** (квота аккаунта, гейт по IP): демон просто не
запишет вердикты, и посты заберёт облачный headless (DeepSeek). Это
fail-open: волны публикуются, но за DeepSeek платится. Признак — в
`logs/local_engine_daemon.jsonl` строки `ok: false`.
