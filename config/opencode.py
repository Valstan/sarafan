"""Конфиг нейро-движка харнесса OpenCode (вариант B пилота классификатора).

Движок — headless-сервер ``opencode serve`` на том же боксе (systemd-юнит
``setka-opencode.service``, слушает 127.0.0.1). Классификатор общается с ним
по HTTP тем же контрактом, что и с DeepSeek (``modules/opencode_engine.py``),
поэтому переключение — одна env-переменная, без правок вызывающего кода.

Env vars:
  CLASSIFIER_ENGINE=deepseek     # deepseek | opencode; дефолт deepseek
                                 # (прежнее поведение, пока пилот не включён)
  OPENCODE_BASE_URL              # адрес serve; дефолт http://127.0.0.1:4096
  OPENCODE_SERVER_USERNAME       # basic-auth serve (опц.; дефолт opencode)
  OPENCODE_SERVER_PASSWORD       # basic-auth serve; пусто = без пароля
  OPENCODE_TIMEOUT=120           # потолок одного вызова модели, сек
  OPENCODE_MODEL_CHAIN           # CSV цепочка фолбэков: первая живая модель
                                 # отвечает. Дефолт — замер 2026-10-14 с VPS:
                                 # muse geo-заблокирована (RegionError с IP
                                 # бокса), longcat/space-bunny/mimo отвечают.
                                 # Когда бокс получит нужный geo (прокси/донор),
                                 # muse автоматически встаёт первой — порядок
                                 # цепочки уже под этим написан.
  OPENCODE_SPILL_GLOB            # маска temp-мусора opencode; дефолт .bcdf*.so
  OPENCODE_SPILL_MAX_AGE         # возраст, после которого свипер удаляет
                                 # найденный мусор, сек; дефолт 1800 (P197)
"""

from __future__ import annotations

import os
from typing import List

DEFAULT_MODEL_CHAIN = [
    "longcat-2.5-preview-free",
    "space-bunny-free",
    "mimo-v2.6-flash-free",
]

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


def get_timeout() -> float:
    try:
        return max(10.0, float(os.getenv("OPENCODE_TIMEOUT", "120")))
    except ValueError:
        return 120.0


def get_model_chain() -> List[str]:
    """Цепочка моделей-фолбэков (CSV в env, иначе дефолт по замеру)."""
    raw = (os.getenv("OPENCODE_MODEL_CHAIN") or "").strip()
    if not raw:
        return list(DEFAULT_MODEL_CHAIN)
    chain = [m.strip() for m in raw.replace(";", ",").split(",") if m.strip()]
    return chain or list(DEFAULT_MODEL_CHAIN)


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


def get_spill_max_age_sec() -> float:
    """Возраст, после которого temp-мусор opencode удаляется, сек (P197)."""
    try:
        return max(60.0, float(os.getenv("OPENCODE_SPILL_MAX_AGE", str(SPILL_MAX_AGE_SEC))))
    except ValueError:
        return float(SPILL_MAX_AGE_SEC)
