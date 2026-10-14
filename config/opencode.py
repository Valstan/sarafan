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
"""

from __future__ import annotations

import os
from typing import List

DEFAULT_MODEL_CHAIN = [
    "muse-spark-1.3-contributor-free",
    "longcat-2.5-preview-free",
    "space-bunny-free",
    "mimo-v2.6-flash-free",
]

DEFAULT_BASE_URL = "http://127.0.0.1:4096"


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
