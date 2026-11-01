"""Инварианты nginx-конфигов: загружаемые файлы не под `immutable`, копии не расходятся.

Что здесь зафиксировано (01.11, D-096):

1. **Загружаемые файлы не под `immutable`.** `/static/broadcast/*` и
   `/static/ad_offers/*` отдаются с ИМЕНЕМ ФАЙЛА, а не с версией: загрузка идёт
   под исходным именем (`safe_image_name` → базовое имя, `upload_image` пишет
   `dest.write_bytes` поверх). Тот же URL спокойно отдаёт новое содержимое, а
   `immutable` на нём — обещание «по адресу навсегда те же байты», и оно ложно:
   браузер держал бы старую картинку до 30 дней. Обход — отдельный `location`
   c `must-revalidate`.

2. **Прод-конфиги и git не расходятся.** До правки `config/nginx_setka_http_default.conf`
   в git лежала ДО-#251 версия: `location /` проксировал всё в приложение, то
   есть сканеры (`.env`, `.git/HEAD`, `phpinfo`) получали 401 и подтверждали, что
   здесь кто-то живёт. Скопировавший такую копию на бокс вернул бы сканерам
   доступ. Прод-версия закрывает чужой Host кодом 444 — и теперь она в git.

3. **Скрипт применения ставит живой конфиг, а не шаблон.** `apply_nginx_config.sh`
   копировал на бокс `setka.conf.editable` — с `listen 80`, плейсхолдером
   `YOUR_DOMAIN` и без TLS. То есть скрипт из документации сносил бы с
   техдомена HTTPS.

4. **`setka_host` определён в репозитории.** Формат лога живёт в
   `config/00_setka_log_format.conf`; на боксе он был, в git — нет, и сборка
   бокса из репозитория была бы невозможна (`nginx -t` → «unknown log format»).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

#: Конфиги, которые отдают `/static` напрямую с alias'ом (не проксируют в
#: приложение) — именно там заголовок cache-control задаёт nginx.
STATIC_SERVING_CONFIGS = (
    "nginx_setka.conf",
    "nginx_setka_http_redirect_domain.conf",
    "setka.conf.editable",
)

#: Каталоги с ЗАГРУЖАЕМЫМИ файлами: имя не несёт версию, содержимое меняется
#: вне деплоя (загрузил новую картинку — она поехала в VK по тому же URL).
UPLOADED_PREFIXES = ("/static/broadcast/", "/static/ad_offers/")


def _read(name: str) -> str:
    return (CONFIG_DIR / name).read_text(encoding="utf-8")


def _location_block(text: str, prefix: str) -> str:
    """Текст location-блока по префиксу (до следующей location или конца сервера)."""
    start = text.find(f"location {prefix}")
    if start == -1:
        return ""
    rest = text[start:]
    nxt = rest.find("location ", 1)
    return rest if nxt == -1 else rest[:nxt]


@pytest.mark.parametrize("config_name", STATIC_SERVING_CONFIGS)
def test_uploaded_files_are_not_served_immutable(config_name: str) -> None:
    """У загружаемых каталогов должно быть исключение из `immutable`."""
    text = _read(config_name)
    for prefix in UPLOADED_PREFIXES:
        block = _location_block(text, prefix)
        assert block, f"{config_name}: нет отдельного location для {prefix}"
        assert "immutable" not in block, (
            f"{config_name}: {prefix} под immutable — имя файла не несёт версию, "
            "содержимое меняется вне деплоя, браузер будет держать старое до 30 дней"
        )
        assert "expires -1" in block, (
            f"{config_name}: {prefix} без `expires -1` — это `Cache-Control: no-cache`, "
            "ровно то, что отдаёт второй слой (RevalidatingStaticFiles). Без него "
            "либо короткое окно, в котором новая картинка не доезжает, либо вечное"
        )


@pytest.mark.parametrize("config_name", STATIC_SERVING_CONFIGS)
def test_uploaded_exception_keeps_the_same_alias_root(config_name: str) -> None:
    """Исключение обязано смотреть в тот же каталог, что и общий `/static`.

    Ошибка тут — тихая: location с неверным alias отдаёт 404 на файле, который
    загрузили и ждут в VK.
    """
    text = _read(config_name)
    for prefix in UPLOADED_PREFIXES:
        block = _location_block(text, prefix)
        assert (
            "alias /home/valstan/SETKA/web/static" in block
        ), f"{config_name}: {prefix} смотрит не в web/static — файлы отдадутся 404"


def test_default_server_config_is_the_hardened_version() -> None:
    """`setka_http_default.conf` в git обязан совпадать с прод-версией (#251).

    До правки в git лежала ДО-#251 версия, где `location /` проксировал всё в
    приложение: сканеры получали 401 и подтверждали, что здесь кто-то живёт.
    Копирование такой версии на бокс вернуло бы сканерам доступ.
    """
    text = _read("nginx_setka_http_default.conf")
    assert "return 444;" in text, (
        "nginx_setka_http_default.conf: чужой Host должен обрываться кодом 444, "
        "а не проксироваться в приложение (прод-версия от 18.09, #251)"
    )
    assert (
        "ssl_reject_handshake on;" in text
    ), "nginx_setka_http_default.conf: чужой SNI должен отклоняться рукопожатием"
    assert "proxy_pass" not in text, (
        "nginx_setka_http_default.conf: default-сервер не должен проксировать "
        "ничего в приложение — это дыра для сканеров"
    )


def test_log_format_used_by_configs_is_defined_in_repo() -> None:
    """`setka_host` определён в репозитории, а не только на боксе.

    На боксе файл `00_setka_log_format.conf` был, в git — нет. Сборка бокса из
    репозитория была бы невозможна: `nginx -t` падает на «unknown log format».
    """
    fmt = _read("00_setka_log_format.conf")
    assert "log_format setka_host" in fmt, "в репо нет определения формата setka_host"

    users = [
        name
        for name in STATIC_SERVING_CONFIGS + ("nginx_setka_http_default.conf",)
        if "setka_host" in _read(name)
    ]
    assert users, "никто не ссылается на setka_host — формат лога осиротел"


def test_apply_script_installs_live_config_not_template() -> None:
    """Скрипт применения ставит `nginx_setka.conf`, а не шаблон."""
    script = (REPO_ROOT / "scripts" / "apply_nginx_config.sh").read_text(encoding="utf-8")
    source_line = re.search(r'SOURCE_FILE="([^"]+)"', script)
    assert source_line, "apply_nginx_config.sh: не найден SOURCE_FILE"
    assert source_line.group(1).endswith("nginx_setka.conf"), (
        f"apply_nginx_config.sh ставит {source_line.group(1)} — это должен быть "
        "живой конфиг nginx_setka.conf, а не шаблон setka.conf.editable "
        "(шаблон с YOUR_DOMAIN и listen 80 снёс бы с техдомена HTTPS)"
    )
    assert "YOUR_DOMAIN" in script, (
        "apply_nginx_config.sh: нужна страховка — отказ, если в исходнике "
        "остался плейсхолдер YOUR_DOMAIN"
    )


def test_template_is_labelled_as_template() -> None:
    """`setka.conf.editable` — шаблон для чужого развёртывания, и это сказано."""
    text = _read("setka.conf.editable")
    assert "YOUR_DOMAIN" in text, "шаблон обязан оставаться шаблоном с плейсхолдером"
    assert "шаблон" in text.lower(), "шаблон должен быть помечен как шаблон"
