"""Конфиг нейро-движка харнесса OpenCode (единый v2-сервер, 17.10).

Движок — всегда живущий ``opencode serve`` v2 на том же боксе (systemd-юнит
``opencode-web``, 127.0.0.1:49374): он же веб-кабинет владельца для кодинга,
он же и фильтрация постов (один сервер на два дела, решение 17.10). Движок к
нему только прикрепляется (``OPENCODE_NO_SPAWN=1``), свой serve не поднимает.
Классификатор общается с ним по HTTP тем же контрактом, что и с DeepSeek
(``modules/opencode_engine.py``), поэтому переключение — одна env-переменная,
без правок вызывающего кода.

Env vars:
  CLASSIFIER_ENGINE=deepseek     # deepseek | opencode; дефолт deepseek
                                 # (прежнее поведение, пока пилот не включён)
  OPENCODE_BASE_URL              # адрес serve; прод — http://127.0.0.1:49374
                                 # (единый opencode-web); дефолт — 4096
  OPENCODE_NO_SPAWN=1            # запретить движку поднимать свой serve:
                                 # единственный сервер на боксе держит
                                 # systemd, движок к нему прикрепляется
  OPENCODE_SERVER_USERNAME       # basic-auth serve (опц.; дефолт opencode)
  OPENCODE_SERVER_PASSWORD       # basic-auth serve; пусто = без пароля
  OPENCODE_TIMEOUT=120           # потолок одного вызова модели, сек
  OPENCODE_PUBLISH_TIMEOUT=40    # потолок вызова на пути публикации (волна),
                                 # сек; остаток бюджета делится между моделями
                                 # цепочки (ADR-0013)
  ENGINE_BREAKER_ENABLED=1       # предохранитель движка: 0 — выключить
  ENGINE_BREAKER_FAILS=3         # сколько отказов по лимиту/сети подряд
  ENGINE_BREAKER_COOLDOWN=300    # на сколько секунд он закрывает вызовы, сек
  ENGINE_BREAKER_QUOTA_COOLDOWN  # отдельный, длинный предохранитель для класса
                                 # «квота аккаунта кончилась», сек; дефолт 1800
  OPENCODE_MODEL_VARIANTS        # карта «модель=уровень рассуждений», напр.
                                 # «deepseek-v4.1-flash-free=high,
                                 # glm-5.3-flash-free=high»; пусто = дефолт
                                 # (deepseek и glm — high, см. ниже)
  OPENCODE_MODEL_CHAIN           # CSV цепочка фолбэков: первая живая модель
                                 # отвечает. Дефолт — замер 20.10 со своего
                                 # бокса: deepseek-v4.1-flash-free первый
                                 # (решение владельца), дальше glm-5.3 /
                                 # mimo-v2.6 / flint-40b. Заведомо мёртвой
                                 # головы в цепочке быть не должно — она
                                 # съедает таймаут на каждом чанке.
  OPENCODE_SPILL_GLOB            # маска temp-мусора opencode; дефолт .bcdf*.so
  OPENCODE_SPILL_MAX_AGE         # возраст, после которого свипер удаляет
                                 # найденный мусор, сек; дефолт 1800 (P197)
  NEURAL_EGRESS_ROTATE_CMD       # команда смены выхода ВПН при гейте free-тира
                                 # по IP (напр. «sudo -n /etc/neural/egress.d/
                                 # 10-nextgenvpn.sh»); пусто = автоповорот выкл.
  NEURAL_EGRESS_ROTATE_COOLDOWN  # пауза между поворотами, сек; дефолт 600
"""

from __future__ import annotations

import os
from typing import Dict, List

# Замер 20.10 (движок v2.0.32, свой бокс): список моделей платформы сменился —
# `space-bunny-free` из каталога ушёл (голова цепочки съедала бы таймаут),
# `deepseek-v4.1-flash-free` отвечает за ~2,7 с и стоит первым по решению
# владельца. Остальные — проверенные 20.10 фолбэки того же каталога.
#
# 24.10: `longcat-2.5-preview-free` из цепочки убран — в каталоге `/api/model`
# его нет (там 9 моделей), и мёртвая голова съедала таймаут на каждом чанке.
# Вместо него — `flint-40b-free` из того же живого каталога.
DEFAULT_MODEL_CHAIN = [
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "mimo-v2.6-flash-free",
    "flint-40b-free",
]

# Уровень рассуждений по модели (решение владельца 24.10). У DeepSeek и GLM —
# `high`, а НЕ `max`: на максимуме обе галлюцинируют, а high — предыдущий к
# максимуму уровень и лучший баланс. Остальным моделям свой максимум владелец
# ставит поштучно через `OPENCODE_MODEL_VARIANTS`, поэтому в дефолте их нет:
# «максимальный у всех» — неверное правило, у разных моделей оптимум разный.
DEFAULT_MODEL_VARIANTS = {
    "deepseek-v4.1-flash-free": "high",
    "glm-5.3-flash-free": "high",
}

DEFAULT_BASE_URL = "http://127.0.0.1:4096"

# Потолок RSS ребёнка serve, МБ (OOM-аудит 14.10: ядро убивало opencode при
# RSS 507–619 МБ — сессии и SSE-буферы копятся на длинных батчах). Ребёнок
# мониторится в процессе: превысил — убит, следующий чанк поднимет свежого
# (fail-open сохраняется, бокс не топит). RLIMIT_AS не подходит: bun
# резервирует ~74 ГБ виртуальной памяти на старте, лимит рубит exec.
SERVE_RSS_KILL_MB = 450

# Страховочное убийство ребёнка: длинный батч (200 постов = 72 чанка) может
# идти часами; если ребёнок жив, но разрастается — убиваем по таймеру,
# следующий чанк поднимет свежего с чистыми сессиями.
SERVE_MAX_LIFETIME_SEC = 15 * 60

# Гигиена temp-мусора (P197, инцидент 15.10). opencode при каждом запуске
# распаковывает в temp нативный модуль `.bcdf*-00000000.so` (~5.5 МБ) и не
# убирает его за собой; в цикле отказов волн это забило /tmp прода (552 файла
# ≈ 2.9 ГБ, диск 100 %, asyncpg.DiskFullError на submit). Ступеней две: свои
# Popen-запуски получают приватный TMPDIR (мусор уходит вместе с каталогом),
# остальные пути (systemd-oneshot, ручные прогоны) подчищает свипер. Моложе
# порога не трогаем: живой serve (потолок жизни — SERVE_MAX_LIFETIME_SEC)
# может ещё держать свой распакованный модуль.
SPILL_GLOB = ".bcdf*.so"
SPILL_MAX_AGE_SEC = 30 * 60


def engine_enabled() -> bool:
    """True → классификатор зовёт движок opencode вместо DeepSeek.

    Дефолт ``deepseek``: переключатель обратимый, прод остаётся на прежнем
    движке, пока пилот не прогнан руками (fail-open волны сохраняется).
    """
    return (os.getenv("CLASSIFIER_ENGINE") or "").strip().lower() == "opencode"


def get_base_url() -> str:
    return (os.getenv("OPENCODE_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def get_server_username() -> str:
    return (os.getenv("OPENCODE_SERVER_USERNAME") or "opencode").strip()


def get_server_password() -> str:
    return (os.getenv("OPENCODE_SERVER_PASSWORD") or "").strip()


def no_spawn() -> bool:
    """True → движку запрещено поднимать свой serve (единый сервер на боксе).

    Решение владельца 17.10: один всегда живой ``opencode-web`` (v2) работает
    и на кодинг владельца, и на фильтрацию постов; свой serve движка встал бы
    на тот же порт и подрался с systemd за него. Выключено (дефолт) — прежние
    пути спавна для ручных прогонов и отладки.
    """
    return (os.getenv("OPENCODE_NO_SPAWN") or "").strip() == "1"


def get_timeout() -> float:
    try:
        return max(10.0, float(os.getenv("OPENCODE_TIMEOUT", "120")))
    except ValueError:
        return 120.0


def get_publish_timeout() -> float:
    """Потолок одного вызова движка на пути публикации, сек.

    ``OPENCODE_TIMEOUT`` (120 с) — потолок ручных прогонов и фоновых батчей, где
    ждать долго терпимо. Волна публикации ждёт иначе: она стоит в единственном
    слоте воркера, и каждый её вызов — это задержка публикаций всех остальных
    районов. Поэтому у пути публикации свой, короткий потолок: не ответил за
    ``OPENCODE_PUBLISH_TIMEOUT`` — публикуем исходный текст (fail-open уже
    встроен в пересказ и в нейро-фильтр).

    Почему дефолт 40 с, а не 25. Замер на боксе 21.10
    (``scripts/post_rewrite_sample.py --region mi --limit 3``): три пересказа —
    64 с на всех, то есть 20–30 с на удачный ответ. При потолке 25 с один из
    трёх не успевал и падал в ``engine_all_failed`` — то есть мы платили те же
    25 с и не получали пересказа. 40 с — типичный ответ с запасом на медленный,
    и по-прежнему втрое меньше прежних 120 с × 6 (цепочка × попытки).
    """
    try:
        return max(5.0, float(os.getenv("OPENCODE_PUBLISH_TIMEOUT", "40")))
    except ValueError:
        return 40.0


def get_model_timeout() -> float:
    """Потолок **одной** модели в цепочке фолбэков, сек.

    Замер 21.10 (лог воркера, один день). Отказов по коду почти нет — 9 × 429, —
    а волн без пересказа 69 (15 %): ``opencode-engine: модель
    deepseek-v4.1-flash-free отказала (network TimeoutError)``. Причина не в
    лимитах, а в том, что первая модель цепочки получала **весь** бюджет вызова
    (``OPENCODE_PUBLISH_TIMEOUT``, 40 с), зависала на нём и оставляла остальные
    две без времени: ``budget_exhausted`` → ``engine_all_failed`` → fail-open,
    в ленту уходит исходный текст.

    С потолком одной модели зависшая модель стоит ``OPENCODE_MODEL_TIMEOUT``
    секунд, и цепочка честно пробует следующую. Волна при этом ещё и короче:
    зависание стоило 40 с, теперь 20.

    Дефолт 20 с — середина между обычным удачным ответом free-тира (15–20 с по
    замеру 21.10) и остатком бюджета публикации: три модели по 20 с в 40 с не
    влезают, но двух зависаний подряд хватает, чтобы сработал fail-open не
    раньше, чем кончится время.
    """
    try:
        return max(5.0, float(os.getenv("OPENCODE_MODEL_TIMEOUT", "20")))
    except ValueError:
        return 20.0


def get_model_chain() -> List[str]:
    """Цепочка моделей-фолбэков (CSV в env, иначе дефолт по замеру)."""
    raw = (os.getenv("OPENCODE_MODEL_CHAIN") or "").strip()
    if not raw:
        return list(DEFAULT_MODEL_CHAIN)
    chain = [m.strip() for m in raw.replace(";", ",").split(",") if m.strip()]
    return chain or list(DEFAULT_MODEL_CHAIN)


def get_model_variants() -> Dict[str, str]:
    """Карта «id модели → уровень рассуждений» (``variant`` в ссылке модели).

    Env ``OPENCODE_MODEL_VARIANTS="id=high,id2=max"``; значение ``-``/``off``
    снимает вариант с модели (платформа возьмёт свой дефолт). Пусто — дефолт
    файла: deepseek и glm на ``high``.
    """
    raw = (os.getenv("OPENCODE_MODEL_VARIANTS") or "").strip()
    if not raw:
        return dict(DEFAULT_MODEL_VARIANTS)
    out: Dict[str, str] = {}
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        model_id, variant = item.split("=", 1)
        model_id, variant = model_id.strip(), variant.strip().lower()
        if not model_id or variant in ("", "-", "off", "none"):
            continue
        out[model_id] = variant
    return out


def get_model_variant(model_id: str) -> str:
    """Уровень рассуждений для модели; пусто — вариант не передаём."""
    return get_model_variants().get((model_id or "").strip(), "")


def get_serve_rss_kill_mb() -> int:
    """Потолок RSS ребёнка serve в МБ (OOM-аудит 14.10)."""
    try:
        return max(64, int(os.getenv("OPENCODE_SERVE_RSS_KILL_MB", str(SERVE_RSS_KILL_MB))))
    except ValueError:
        return SERVE_RSS_KILL_MB


def get_serve_max_lifetime() -> float:
    """Страховочное убийство ребёнка, сек (0 = выключено)."""
    try:
        return max(
            0.0, float(os.getenv("OPENCODE_SERVE_MAX_LIFETIME", str(SERVE_MAX_LIFETIME_SEC)))
        )
    except ValueError:
        return SERVE_MAX_LIFETIME_SEC


def get_spill_glob() -> str:
    """Маска temp-мусора opencode (P197)."""
    return (os.getenv("OPENCODE_SPILL_GLOB") or "").strip() or SPILL_GLOB


def get_egress_rotate_cmd() -> List[str]:
    """Команда смены выхода ВПН; пусто — автоповорот выключен.

    Free-тир OpenCode закрывается по IP: «free models aren't available from your
    current network… abusive traffic from this IP». Гейт per-IP, поэтому смена
    выхода его снимает (замер 20.10: смена сервера — 1,7–2,6 с, движок живёт).
    Здесь — команда поворота, а не путь к скрипту: политика «кто и как меняет
    выход» остаётся настройкой бокса, а движок только просит.
    """
    raw = (os.getenv("NEURAL_EGRESS_ROTATE_CMD") or "").strip()
    if not raw:
        return []
    import shlex

    return shlex.split(raw)


def get_egress_rotate_cooldown() -> float:
    """Пауза между автоповоротами выхода, сек (дефолт 10 минут)."""
    try:
        return max(0.0, float(os.getenv("NEURAL_EGRESS_ROTATE_COOLDOWN", "600")))
    except ValueError:
        return 600.0


def get_identity_reset_cmd() -> List[str]:
    """Команда смены идентичности анонимного аккаунта; пусто — выключено.

    Зачем отдельный механизм, если есть поворот выхода. Замер 24.10: пять
    поворотов ВПН подряд (DE, PL, NL, US, PL — разные адреса) не сняли
    ``Free usage limit reached``. Квота free-тира считается по аккаунту, а
    анонимный аккаунт опознаётся по локальному состоянию движка, а не по IP:
    смена выхода не создаёт новый аккаунт. Создаёт его сброс состояния —
    и только он (решение владельца 24.10, с явным пониманием, что это обход
    лимита вендора).

    Процедура целиком живёт на боксе (``infra/neural/neural-identity-reset.sh``):
    выгрузить движок со всеми процессами → сменить сервер и страну ВПН →
    сбросить идентичность → поднять → проверить free-тир ОДНИМ коротким
    сообщением. Движок только просит, политика — настройка бокса.
    """
    raw = (os.getenv("NEURAL_IDENTITY_RESET_CMD") or "").strip()
    if not raw:
        return []
    import shlex

    return shlex.split(raw)


def get_identity_reset_cooldown() -> float:
    """Пауза между сбросами идентичности, сек (дефолт 6 часов).

    Дороже поворота выхода: движок на это время выгружается целиком, а он же
    веб-кабинет владельца. Шесть часов — чтобы неудачный сброс не превратился
    в цикл «подняли-выгрузили» и чтобы владелец, работающий в кабинете, не
    терял сессию каждые десять минут.
    """
    try:
        return max(0.0, float(os.getenv("NEURAL_IDENTITY_RESET_COOLDOWN", "21600")))
    except ValueError:
        return 21600.0


def get_spill_max_age_sec() -> float:
    """Возраст, после которого temp-мусор opencode удаляется, сек (P197)."""
    try:
        return max(60.0, float(os.getenv("OPENCODE_SPILL_MAX_AGE", str(SPILL_MAX_AGE_SEC))))
    except ValueError:
        return float(SPILL_MAX_AGE_SEC)
