"""Гейт идеи пула #250: адрес статики меняется на каждом деплое сам.

nginx отдаёт ``/static`` с ``Cache-Control: public, immutable`` — это обещание,
что по адресу навсегда те же байты и те же заголовки. Обещание честно, только
если адрес действительно меняется при каждом изменении файла. Ручные метки
``?v=20260831`` этим свойством не обладали: часть из них протухла (файл менялся,
метка — нет), и правка застревала в кэше посетителей на 30 дней.

Поэтому все ``/static/``-ссылки шаблонов несут ``?v={{ STATIC_VERSION }}``,
а версия считается сама: env ``SETKA_STATIC_VERSION`` → sha HEAD → ``"dev"``.
Этот файл держит три свойства:

1. ``get_static_version()`` реально считается, а не лежит константой;
2. ни одна ссылка в шаблонах не осталась без ``{{ STATIC_VERSION }}``
   (включая ручные ``?v=`` с датами — их быть не должно вообще);
3. ``main`` отдаёт глобал ``STATIC_VERSION`` в Jinja и версионирует иконки
   PWA-манифеста — иначе перерисованная иконка застревает под тем же
   ``immutable``.
"""

from __future__ import annotations

import re
from pathlib import Path

from web.static_files import STATIC_VERSION, get_static_version

REPO_ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = REPO_ROOT / "web" / "templates"

# Ссылка на статику внутри кавычки: /static/путь с необязательным query.
STATIC_REF = re.compile(r"""/static/[A-Za-z0-9_.\-/]+(?:\?[A-Za-z0-9_{}\-. ]+)?(?=["'])""")
# Ручная метка с датой — признак «человек должен был вспомнить».
HAND_VERSION = re.compile(r"\?v=\d{8}")


def _static_refs(text: str) -> list[str]:
    return STATIC_REF.findall(text)


def test_version_is_computed_not_hardcoded(monkeypatch) -> None:
    monkeypatch.setenv("SETKA_STATIC_VERSION", "test-sha-1")
    assert get_static_version() == "test-sha-1"

    monkeypatch.delenv("SETKA_STATIC_VERSION", raising=False)
    auto = get_static_version()
    assert auto, "версия статики пуста — кэш-бастер мёртв"
    assert auto != "1.5.0", "упали на застывший _version — адрес не сменится на деплое"


def test_no_template_static_ref_without_autoversion() -> None:
    offenders: list[str] = []
    for html in sorted(TPL_DIR.glob("*.html")):
        for ref in _static_refs(html.read_text(encoding="utf-8")):
            if "{{ STATIC_VERSION }}" not in ref:
                offenders.append(f"{html.name}: {ref}")
    assert not offenders, (
        "ссылки на статику без {{ STATIC_VERSION }} — правка этих файлов "
        "застрянет под nginx-immutable:\n" + "\n".join(offenders)
    )


def test_no_handwritten_date_versions_left() -> None:
    offenders: list[str] = []
    for html in sorted(TPL_DIR.glob("*.html")):
        for ref in _static_refs(html.read_text(encoding="utf-8")):
            if HAND_VERSION.search(ref):
                offenders.append(f"{html.name}: {ref}")
    assert not offenders, (
        "остались ручные ?v= с датами — их надо было заменить автоматической "
        "версией:\n" + "\n".join(offenders)
    )


def test_jinja_global_and_manifest_icons_are_versioned() -> None:
    import main

    assert main.templates.env.globals.get("STATIC_VERSION") == STATIC_VERSION
    assert STATIC_VERSION, "STATIC_VERSION пуст на старте"

    srcs = [icon["src"] for icon in main._RADAR_MANIFEST_ICONS]
    assert srcs, "манифест остался без иконок"
    for src in srcs:
        assert src.startswith("/static/radar/"), f"иконка вне /static/radar/: {src}"
        assert STATIC_VERSION in src, (
            f"иконка манифеста без версии деплоя: {src} — перерисовка застрянет " "под immutable"
        )
