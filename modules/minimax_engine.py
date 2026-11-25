"""Движок классификации через MiniMax Code (CLI) с пулом аккаунтов и авто-чекином.

**Что это.** У аккаунтов MiniMax Code есть ежедневная награда — очки чекина
(400 в обычный день, 1000 на 4-м и 7-м дне серии, 4000 за неделю; очки живут
30 дней). Очки тратятся на сценарии MiniMax Code — то есть ровно на то, что
делает этот движок: один вызов = одна пачка постов, один вердикт на пост.
Пул аккаунтов позволяет разнести волну по аккаунтам: у каждого своя суточная
награда, а недельное окно расхода — общий потолок.

**Почему через CLI, а не через API.** Публичный API MiniMax требует «API secret
key», а у этих аккаунтов токен-плана нет (`no active token plan subscription`,
код 2062) — очки чекина относятся к сценариям MiniMax Code, а не к API.
Единственный путь, который умеет тратить эти очки, — рантайм Code. Он же даёт
то, что нужно классификатору: ``mcode exec`` без TUI и ``--output-format json``
— ответ приходит JSON-объектом.

⚠️ **Нативная строгая схема (``--output-schema``) у MiniMax не работает:**
замерено 13.11 — ``STRUCTURED_OUTPUT_INVALID / Structured output was not valid
JSON`` на любой схеме (и полной, и плоской). Поэтому формат ответа задаёт
системный промпт (он и в обычном конвейере приводит к JSON), а разбор — тот же
``parse_chunk_response``, что у остальных движков.

⚠️ **Посты агент видит только файлом.** Подача постов строкой в промпте даёт
«тексты постов не предоставлены»: агент-кодер считает содержимое задачи не
данными. Рабочий путь (замерено 13.11): посты файлом (``--file``) + режим
контекста ``standard`` (в ``lightweight`` файлов он не видит) + несколько шагов
агента (на трёх — ``limit_exceeded``). Плата за это — агентный обвяз: ~20 тыс.
токенов на вызов против ~1 тыс. при прямой подаче.

**Авторизация без браузера.** У каждого аккаунта — свой профиль CLI (каталог с
``auth.json``). CLI сам обновляет протухший access-токен по refresh-токену
(``POST account.minimax.io/oauth2/token``), поэтому на боксе нужен только
профиль. Профили, созданные вручную (device-flow в браузере), копируются на
бокс как есть — тем же приёмом, которым авторизация копируется между
областями на ПК владельца.

**Чек-ин.** Очки забираются один раз в сутки на аккаунт:
``GET/POST agent.minimax.io/minimax-cloud/api/v1/signin/{status,claim}``.
Повтор в тот же день возвращает ``claim_result=2`` («уже получено») — очки не
начисляются дважды, поэтому движок дёргает claim перед первым по��ком и не
боится повторов.

**Ротация.** Пул перебирается по правилу Кармана (пионер схемы): сначала
аккаунты, чья ближайшая активация даст больше очков (клетка 4 или 7 серии —
1000), потом — расстояние до бонусной клетки, потом длина серии, потом
давность использования. Аккаунты, у которых очки сегодня уже забраны (или
нечем платить), в этот проход не берутся.

**Учёт.** Каждый вызов пишется в журнал (JSONL) и в общий учёт Кармана — там
живёт счёт активаций по проектам, и без общей статистики один и тот же аккаунт
достанется и Сарафану, и владельцу в одни сутки.

**Аренда у Кармана.** Подбор аккаунта — не наш: по схеме владельца Сарафан
приходит к Карману за следующим аккаунтом (``modules/karman_minimax.py``),
активирует его у себя (забирает очки чекина — это и есть активация) и работает,
пока очки не кончатся; потом приходит за следующим. Подбор, статистику и
оптимальный выбор кандидата считает движок Кармана. Пока ручек у Кармана нет,
движок работает своим пулом и своей ротацией — переключается само.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from modules import karman_minimax, minimax_capacity

logger = logging.getLogger(__name__)

#: Хост платформы MiniMax Code (панель чек-ина и остальное).
AGENT_BASE = "https://agent.minimax.io"
#: Часовой пояс для панели чек-ина: IANA-имя, иначе MiniMax отвечает
#: «invalid timezone_id». Московский — чтобы «сегодня» совпадало с московским
#: днём (у MiniMax сутки календарные, в поясе аккаунта).
SIGNIN_TZ = "Europe/Moscow"
#: То же имя в виде query-параметра: слэш в значении — литерал "%2F".
#: Раньше литерал «Europe%2FMoscow» был продублирован в двух signin-вызовах,
#: а константа SIGNIN_TZ не использовалась нигде (мёртвая копия правила).
SIGNIN_TZ_PARAM = SIGNIN_TZ.replace("/", "%2F")

#: Куда смотреть пул аккаунтов. Каждый аккаунт — каталог с профилем CLI:
#: ``<имя>/config`` (MCODE_CONFIG_DIR) и
#: ``<имя>/data/auth/prod/en/mcode-public/auth.json`` (токен-стор).
ENV_ACCOUNTS_DIR = "MINIMAX_ACCOUNTS_DIR"
DEFAULT_ACCOUNTS_DIR = "/opt/minimax-accounts"
#: Путь к CLI (``mcode``). На боксе — установленный MiniMax Code.
ENV_CLI = "MINIMAX_CLI"
DEFAULT_CLI = "mcode"
#: Таймаут одного вызова CLI.
ENV_TIMEOUT = "MINIMAX_TIMEOUT"
DEFAULT_TIMEOUT = 120
#: Модель по умолчанию. Формат у CLI — ``provider/model``: без префикса провайдера
#: он отвечает «--model must use provider/model». Провайдер у моделей MiniMax —
#: ``minimax`` (его же называет сам ``exec.result``: providerId=minimax).
ENV_MODEL = "MINIMAX_MODEL"
DEFAULT_MODEL = "minimax/MiniMax-M3.1-Flash"
#: Журнал расхода (рядом с прочими runtime-файлами; в git не едет).
ENV_USAGE_LOG = "MINIMAX_USAGE_LOG"
DEFAULT_USAGE_LOG = "logs/minimax_usage.jsonl"
#: Сколько аккаунтов пробуем за один вызов (все, у кого есть очки).
ENV_MAX_ACCOUNTS = "MINIMAX_MAX_ACCOUNTS"
DEFAULT_MAX_ACCOUNTS = 10
#: Режим контекста CLI. ``standard`` — агент видит вложенные файлы; ``lightweight``
#: — не видит, и тогда он просит «пришлите посты», а не вердиктит.
ENV_CLI_MODE = "MINIMAX_CLI_MODE"
DEFAULT_CLI_MODE = "standard"
#: Сколько шагов агента разрешаем. Агенту нужно прочитать файл с постами и
#: ответить; на 3 шагах он не успевает (замер 13.11: ``limit_exceeded``).
ENV_MAX_STEPS = "MINIMAX_MAX_STEPS"
DEFAULT_MAX_STEPS = 6
#: Сколько аккаунтов за один вызов разрешено запросить у Кармана.
ENV_MAX_LEASES = "MINIMAX_MAX_LEASES"
DEFAULT_MAX_LEASES = 5
#: Пауза после сбоя связи с Карманом. Пока она не истекла, мы к нему не ходим:
#: каждый чанк иначе ждал бы сетевого таймаута, а учёт активаций он всё равно
#: не примет.
#:
#: ⚠️ **Пауза — общая и короткая.** Замер 16.11: пауза была 30 минут и лежала
#: в памяти процесса, поэтому четыре форка воркера жили в разных временах.
#: Итог — 514 отказов движка за день и **ровно половина трафика в Дипсике**.
#: Теперь пауза лежит в общем файле (её видят все процессы) и длится 10 секунд:
#: решение владельца 16.11 — «пауза 10 секунд и повторный запрос за аккаунтом».
ENV_KARMAN_DOWN_TTL = "MINIMAX_KARMAN_DOWN_TTL"
DEFAULT_KARMAN_DOWN_TTL = 10
#: Пауза после пустой выдачи Кармана (нет аккаунта с готовым входом). Без неё
#: волна на 40 батчей спросила бы Кармана 40 раз подряд и словила его 429
#: («слишком много запросов») — а отвечает он всё равно тем же.
ENV_KARMAN_EMPTY_TTL = "MINIMAX_KARMAN_EMPTY_TTL"
DEFAULT_KARMAN_EMPTY_TTL = 10
#: Пауза после того, как свежий аккаунт от Кармана отказал сразу (у пула
#: сегодня нет очков). Без неё волна крутила бы ротацию и сжигала пул: живой
#: факт 15.11 — 15 аккаунтов за две минуты.
#:
#: ⚠️ Было 1800 с (30 минут) — это и была причина «половины трафика в Дипсике»:
#: три подряд пустых аккаунта выключали движок на полчаса для всех форков.
#: Теперь 10 секунд (решение владельца 16.11), а длинная пауза заменена
#: **счётчиком подряд идущих пустых аккаунтов** (см. ``MINIMAX_DEAD_STREAK``).
ENV_KARMAN_NO_POINTS_TTL = "MINIMAX_KARMAN_NO_POINTS_TTL"
DEFAULT_KARMAN_NO_POINTS_TTL = 10
#: Сколько подряд идущих **пустых** аккаунтов (баланс ноль, ни одного токена)
#: мы готовы выдержать, прежде чем признать пул мёртвым и отдать волну
#: платной страховке.
#:
#: Решение владельца 16.11: «если 10 подряд пустых аккаунтов — то тогда передача
#: волны Дипсику». Раньше порог был 3, а следствие — пауза на полчаса; теперь
#: следствие другое (страховка), а пауза между попытками — 10 секунд.
ENV_DEAD_STREAK_LIMIT = "MINIMAX_DEAD_STREAK_LIMIT"
DEFAULT_DEAD_STREAK_LIMIT = 10
#: Сколько секунд движок может молчать подряд, прежде чем волна уйдёт в платную
#: страховку (env ``MINIMAX_DEAD_AFTER_SEC``).
#:
#: Второй рубеж рядом со счётчиком пустых аккаунтов: тот ловит «пул выдаёт
#: мёртвые входы», а этот — «движок не отвечает вовсе» (сеть, Карман, CLI).
#: Решение владельца 16.11: Дипсик — страховка на случай сбоев, а не на
#: постоянную половину трафика; десять минут молчания — уже не «случайный сбой».
ENV_DEAD_AFTER_SEC = "MINIMAX_DEAD_AFTER_SEC"
DEFAULT_DEAD_AFTER_SEC = 600
#: Сколько секунд после признания пула мёртвым мы **не берём** ещё аккаунт
#: (env ``MINIMAX_POOL_DEAD_TTL``). Пока пул мёртв, волна идёт платной
#: страховкой целиком; сбрасывается первым же успехом.
ENV_POOL_DEAD_TTL = "MINIMAX_POOL_DEAD_TTL"
DEFAULT_POOL_DEAD_TTL = 600
#: Сколько секунд после рубежа «движок молчит» мы **не берём** аккаунт, а потом
#: всё-таки пробуем снова (env ``MINIMAX_RETRY_AFTER_SEC``).
#:
#: ⚠️ **Без этого срока контур залипал навсегда.** Живой факт 18.11: молчание
#: 35 723 с (10 часов), у Кармана при этом было **111 свободных аккаунтов** —
#: а движок не спросил ни разу, потому что отметку «молчит» чистит только
#: успех, а успех невозможен, если не пробовать. Сутки классификации ушли в
#: платный Дипсик (100 % трафика с 21:32), хотя пул был полон.
#:
#: Теперь рубеж «десять минут молчания» держит страховку лишь ``RETRY_AFTER_SEC``,
#: после чего мы сбрасываем отметку и снова идём за аккаунтом к Карману.
ENV_RETRY_AFTER_SEC = "MINIMAX_RETRY_AFTER_SEC"
DEFAULT_RETRY_AFTER_SEC = 300
#: Сколько мёртвых аккаунтов помнить, чтобы **не просить их у Кармана снова**.
#: Ручка ``exclude=`` в выдаче для этого и сделана: без неё Карман отдаёт тот
#: же id, и мы снова получаем «insufficient balance» (живой факт 16.11:
#: аккаунты 69 → 70 → 71 подряд, все с нулевым балансом).
ENV_DEAD_IDS_LIMIT = "MINIMAX_DEAD_IDS_LIMIT"
DEFAULT_DEAD_IDS_LIMIT = 50
#: Постов в одном вызове MiniMax (env ``MINIMAX_CHUNK_SIZE``). Границы 1..40.
#:
#: Отдель от ``CLASSIFIER_HEADLESS_CHUNK`` не из упрямства: у Дипсика вызов —
#: это HTTP-запрос (стал «40» ещё 26.10, замер: 165 вызовов вместо 194), а у
#: MiniMax вызов — это **запуск агента-CLI**: отдельный процесс, который
#: заново читает правилник и тратит ~16 000 токенов на вызов. На проде чанк
#: был опущен до 5 **общим** параметром — и Дипсик, и MiniMax работали по
#: пять постов, то есть вчетверо больше вызовов, чем нужно.
#:
#: Для MiniMax дефолт 20: агент-CLI на 40 постов рвёт JSON (длинный ответ),
#: а деление пачки пополам — это лишний оплаченный вызов.
#:
#: ⚠️ Имя env-ключа и дефолт живут в ``config/classifier.py``
#: (``get_minimax_chunk_size``) — единственный источник истины. Дубли здесь
#: были мёртвой копией правила, и их легко было бы разъехать.
#:
#: Ограничить выдачу Кармана нашими почтами (``?emails=``). По умолчанию
#: **выключено**: аккаунтами пула занимается Карман, он сам решает, какой дать,
#: и отдаёт токен — ограничивать его выбор нашими локальными профилями нельзя.
#: Флаг нужен только для режима «работаем на своих профилях» (без токенов).
ENV_EMAILS_FILTER = "MINIMAX_EMAILS_FILTER"
DEFAULT_EMAILS_FILTER = "0"
#: Рабочая область MiniMax на боксе — **одна** (заказ владельца 14.11): в ней
#: вершатся посты, и мы меняем только аккаунт внутри, когда бонус исчерпан, —
#: чтобы не терять контекст и не плодить области. Токен нового аккаунта
#: перезаписывает токен-стор этой области; путь области постоянен, значит и
#: ключ записи токена (он выводится из пути) остаётся верным.
ENV_WORK_AREA = "MINIMAX_WORK_AREA"
DEFAULT_WORK_AREA = "work"
#: Потолок состояния CLI в рабочей области (env ``MINIMAX_STATE_MAX_MB``).
#:
#: **Живой факт 22.11 — это и был причиной двухдневного отказа MiniMax.**
#: CLI (MiniMax Code) копит в своей области ВСЁ, что было за время работы:
#: сессии агента, sqlite-состояние, логи наблюдаемости, каталог запусков и
#: фоновых задач. К 22.11 область весила **897 МБ** (31 112 файлов), и вызов
#: CLI поднимал RSS до **554 МБ** — ядро снимало процесс (запись в ``dmesg``:
#: ``OOM killed process 683639 (minimax-code) … anon-rss:554124kB``), вызов
#: возвращал ``exec_failed``, и **каждый** вызов уходил в платный Дипсик
#: (замер: 945 отказов и 945 вызовов Дипсика 20.11, 953 и 954 — 21.11).
#:
#: Что важно: **бокс не был перегружен.** Загрузка воркера за сутки — 27 %,
#: CPU во время волны — 0,3 %, свободно 601–684 МБ. Не хватало не мощности,
#: а разделения. И состояние растёт само: ~26 КБ на вызов — через сутки
#: состояние снова упирается в бокс.
#:
#: Потолок держит область в узде: 64 МБ состояния дают RSS около 315 МБ
#: (замер: пустая область — 297 МБ, 897 МБ — 554 МБ, то есть ~0,29 МБ RSS на
#: МБ состояния), а это уже помещается в свободную память бокса. Сброс —
#: дешевле и надёжнее, чем борьба за мегабайты.
ENV_STATE_MAX_MB = "MINIMAX_STATE_MAX_MB"
DEFAULT_STATE_MAX_MB = 64
#: Потолок кучи V8 для CLI (env ``MINIMAX_NODE_MAX_OLD_SPACE_MB``).
#:
#: Без потолка Node выбирает лимит кучи по памяти машины, и на боксе в 1536 МБ
#: это около 768 МБ: процесс раздувается до ~900 МБ RSS и **его снимает ядро**
#: — а жертвой ядро выбирает крупнейший процесс, то есть на боксе может
#: снять не CLI, а воркер. Потолок превращает это в быстрый отказ самого CLI
#: (и наш откат), а не в убийство чужого процесса.
ENV_NODE_MAX_OLD_SPACE_MB = "MINIMAX_NODE_MAX_OLD_SPACE_MB"
DEFAULT_NODE_MAX_OLD_SPACE_MB = 384
#: Признаки того, что CLI сожрал память (для классификации отказа).
_MEMORY_MARKERS = (
    "heap out of memory",
    "allocation failed",
    "reached heap limit",
    "out of memory",
    "oom",
)
#: Отмечать ли активацию в журнале Кармана (``POST /api/minimax/activations``).
#:
#: По контракту 14.11 (ответ Кармана, id 142) — **не отмечаем**: «в карман
#: больше заходить не нужно: ни активацию записывать, ни что-то помечать —
#: всё это теперь делаем мы» (гашение аккаунта, очки и статистику он пишет сам
#: в момент выдачи). Ручка у него осталась, но звать её не надо. Флаг нужен
#: только на случай, если разделение труда поменяется обратно.
ENV_REPORT_ACTIVATION = "MINIMAX_REPORT_ACTIVATION"
DEFAULT_REPORT_ACTIVATION = "0"
#: Файл с метаданными профиля рядом с ``auth.json``: почта аккаунта и его id
#: в Кармане. По почве аккаунт, выданный Карманом, узнаётся у нас.
PROFILE_META_NAME = "profile.json"
#: Задача, которую CLI получает вместе с файлом с постами.
#:
#: ⚠️ **Задача — часть контракта вызова, и она разная для разных потоков.**
#: Живой факт 18.11: с одним и тем же заданием «вынеси вердикты» переписывание
#: постов получало от MiniMax ответ **вердиктами классификатора**
#: (``{"verdicts": [{"lip": "ПОСТ 1", "theme": …, "action": …}]}``) вместо
#: пересказа: 14 отказов в сутки, каждый закрывался платным Дипсиком. Причина
#: в том, что агенту говорили «вынеси вердикты» даже там, где просили переписать
#: текст: задание и системный промпт противоречили друг другу, и модель
#: выбирала схему, к которой была приучена.
TASK_TEXT = (
    "Вынеси вердикты по постам из прикреплённого файла. "
    "Ответ — только JSON по схеме из системного промпта, без пояснений."
)
#: Переписывание и сборник: та же форма (посты файлом), но задание — про тексты,
#: а не про вердикты.
TASK_TEXT_REWRITE = (
    "Обработай тексты постов из прикреплённого файла. "
    "Ответ — строго в формате из системного промпта, без пояснений."
)
#: Конвейер сайтов: один пост на файл, ответ — JSON по схеме системного промпта.
TASK_TEXT_CONVEYOR = (
    "Обработай пост из прикреплённого файла. "
    "Ответ — только JSON по схеме из системного промпта, без пояснений."
)
#: Подбор сообществ: на входе — карточка сообщества (не посты), на выходе —
#: JSON с одной категорией. Своё задание обязательно: с классификаторским
#: модель отвечает **вердиктами по постам** — ровно тот случай, который 18.11
#: стоил 14 отказов в сутки у переписывания (ADR-0017, P227).
TASK_TEXT_DISCOVERY = (
    "Определи тематику VK-сообщества по карточке из прикреплённого файла. "
    "Ответ — только JSON-объект с полями category, confidence, is_info_page, "
    "reasoning. Без пояснений и без markdown."
)

#: Задача по потоку: метка из журнала вызовов → текст задания для CLI.
#: Классификация сохраняет прежнее задание (проверено на живых волнах), остальные
#: потоки получают своё — иначе модель отвечает «по привычке», а не по задаче.
TASK_BY_LABEL = {
    "headless": TASK_TEXT,
    "post_rewrite": TASK_TEXT_REWRITE,
    "daily_digest": TASK_TEXT_REWRITE,
    "conveyor": TASK_TEXT_CONVEYOR,
    "discovery": TASK_TEXT_DISCOVERY,
}


def task_text_for(label: str = "", explicit: str = "") -> str:
    """Текст задания для CLI: явный > по метке потока > дефолт (классификация).

    Метка потока уже ходит с каждым вызовом (``label=`` в журнале расхода), так
    что правило «задание соответствует потоку» живёт в одном месте — здесь, а
    не по одному вызову в каждом потоке.
    """
    text = (explicit or "").strip()
    if text:
        return text
    return TASK_BY_LABEL.get((label or "").strip().lower(), TASK_TEXT)


#: OAuth-эндпоинт MiniMax Code: обновление access-токена по refresh-токену.
OAUTH_TOKEN_URL = "https://account.minimax.io/oauth2/token"
OAUTH_CLIENT_ID = "mcode-public"
OAUTH_SCOPE = "agent.default"
OAUTH_AUDIENCE = "agent-backend"

#: Признаки «очки кончились» в ответах MiniMax Code.
_QUOTA_MARKERS = (
    "insufficient balance",
    "quota",
    "usage limit",
    "credits exhausted",
    "free usage limit",
)

#: Признаки «вход протух» — аккаунт больше не годится, нужен другой.
#: Для аренды это тот же класс события, что и кончившиеся очки: выходим из
#: аккаунта и идём к Карману за следующим (заказ владельца 14.11).
_AUTH_MARKERS = (
    "sign in to minimax",
    "sign in",
    "please log in",
    "not logged in",
    "session has expired",
    "session expired",
    "unauthorized",
    "401",
    "invalid token",
    "token expired",
    "token has expired",
    "expired_token",
    "invalid_grant",
    "authentication error",
    # Рантайм отверг саму запись токен-стора (например, refreshToken пуст):
    # аккаунт нерабочий — надо взять следующий, а не биться в ту же запись.
    "stored oauth credential record is invalid",
    "credential record is invalid",
    "credentialrecordcorrupt",
)

#: Сколько живёт кэш панели чекина (сек). Панель внутри суток стабильна, а
#: HTTP-запрос на каждый аккаунт на каждый чанк — это сеть вхолостую.
PANEL_TTL = 600
_panel_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


@dataclass
class MinimaxAccount:
    """Один аккаунт MiniMax: профиль CLI + состояние чек-ина."""

    name: str
    root: Path
    email: str = ""
    panel: Optional[Dict[str, Any]] = None
    claimed_today: bool = False
    #: id аккаунта в Кармане (из ``profile.json``) — по нему отмечаем активацию.
    karman_id: Optional[int] = None
    #: Очки, забранные за сегодня (нужны для отчёта в Карман).
    claimed_points: int = 0
    #: Номер клетки серии (1..7) — тоже для отчёта в Карман.
    activation_index: Optional[int] = None

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def auth_path(self) -> Path:
        return self.data_dir / "auth" / "prod" / "en" / "mcode-public" / "auth.json"

    def token(self) -> Optional[str]:
        """Живой access-токен из токен-стора профиля.

        Токен протухает, а обновляет его **CLI** — но только когда CLI сам
        запускается. Движок читает токен напрямую (панель чекина), и протухший
        токен молча выкинул бы аккаунт из пула: «401» на панели выглядел бы
        как «у аккаунта нет очков», и контур тихо работал бы вхолостую.
        Поэтому обновление — здесь, по refresh-токену того же аккаунта.
        """
        records = self._records()
        if not records:
            return None
        now_ms = int(time.time() * 1000)
        live = [r for r in records.values() if (r.get("expiresAtMs") or 0) > now_ms]
        if not live and self.refresh():
            records = self._records()
            live = [r for r in records.values() if (r.get("expiresAtMs") or 0) > now_ms]
        best = max(live or records.values(), key=lambda r: r.get("expiresAtMs") or 0)
        token = best.get("accessToken")
        return str(token) if token else None

    def _records(self) -> Dict[str, Any]:
        """Записи токен-стора (их несколько: десктоп и CLI)."""
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get("records") or {}

    def refresh(self) -> bool:
        """Обновить access-токен по refresh-токену и записать обратно в стор.

        Контракт взят из рантайма MiniMax Code (замерено 13.11): обмен на
        ``account.minimax.io/oauth2/token``, форма ``grant_type=refresh_token``,
        ``client_id``/``scope``/``audience`` — как у публичного клиента.
        """
        records = self._records()
        if not records:
            return False
        key = next(iter(records))
        record = records[key]
        refresh_token = record.get("refreshToken")
        if not refresh_token:
            return False
        if refresh_token == REFRESH_PLACEHOLDER:
            # Настоящего refresh нет (Карман его не присылает) — обмен бессмыслен:
            # вендер ответит invalid_grant, а после истечения access мы просто
            # берём у Кармана следующий аккаунт.
            return False
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": record.get("clientId") or OAUTH_CLIENT_ID,
                "scope": OAUTH_SCOPE,
                "audience": OAUTH_AUDIENCE,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "minimax: не обновить токен аккаунта %s (%s: %s)",
                self.name,
                type(exc).__name__,
                str(exc)[:120],
            )
            return False
        access = payload.get("access_token")
        if not access:
            return False
        expires_in = int(payload.get("expires_in") or 3600)
        record["accessToken"] = access
        if payload.get("refresh_token"):
            record["refreshToken"] = payload["refresh_token"]
        record["expiresAtMs"] = int(time.time() * 1000) + expires_in * 1000 - 300 * 1000
        try:
            store = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        store.setdefault("records", {})[key] = record
        tmp = self.auth_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.auth_path)
        except OSError as exc:
            logger.warning("minimax: токен обновлён, но не записан (%s)", exc)
            return False
        logger.info("minimax: access-токен аккаунта %s обновлён", self.name)
        return True

    def summary(self) -> Dict[str, Any]:
        panel = self.panel or {}
        today = _today_cell(panel)
        return {
            "name": self.name,
            "email": self.email,
            "points_today": today.get("points") if today else None,
            "day_no": today.get("day_no") if today else None,
            "is_today": bool(today and today.get("is_today")),
            "status": today.get("status") if today else None,
        }


def _today_cell(panel: Dict[str, Any]) -> Dict[str, Any]:
    """Клетка сегодняшнего дня из панели (пусто, если панели нет)."""
    days = ((panel or {}).get("data") or {}).get("days") or []
    for day in days:
        if day.get("is_today"):
            return day
    return {}


def _streak(panel: Dict[str, Any]) -> int:
    """Длина серии дней подряд по панели (считаем назад от сегодняшней клетки).

    Номер клетки на панели — это и есть серия (день 4 = три дня подряд минус
    сегодня), но считать её по панели надёжнее, чем по номеру: панель —
    источник истины, наш счётчик — производная от неё.
    """
    days = ((panel or {}).get("data") or {}).get("days") or []
    today = _today_cell(panel)
    if not today:
        return 0
    claimed_today = today.get("status") in (2, 3)
    streak = 1 if claimed_today else 0
    for day in days:
        if day.get("day_no", 0) >= today.get("day_no", 0):
            continue
        if day.get("status") == 3:  # забрано — серия непрерывна
            streak += 1
        else:
            break
    return streak


def _points_of_next_activation(panel: Dict[str, Any]) -> int:
    """Очки ближайшей активации по панели (1000 на клетках 4 и 7, иначе 400)."""
    days = ((panel or {}).get("data") or {}).get("days") or []
    today = _today_cell(panel)
    if not today:
        return 400
    today_no = today.get("day_no") or 1
    upcoming = [d for d in days if (d.get("day_no") or 0) > today_no and d.get("status") != 3]
    if not upcoming:
        return 400
    return int(min(upcoming, key=lambda d: d.get("day_no") or 0).get("points") or 400)


def _distance_to_bonus(panel: Dict[str, Any]) -> int:
    """Сколько дней до ближайшей бонусной клетки (1000 очков)."""
    days = ((panel or {}).get("data") or {}).get("days") or []
    today = _today_cell(panel)
    today_no = today.get("day_no") or 1
    for day in sorted(days, key=lambda d: d.get("day_no") or 0):
        no = day.get("day_no") or 0
        if no > today_no and int(day.get("points") or 0) >= 1000:
            return no - today_no
    return 99


def account_sort_key(account: MinimaxAccount) -> Tuple[int, int, int, int]:
    """Ключ ротации: правило Кармана (по деньгам, не по давноте).

    1. очки ближайшей активации — больше лучше;
    2. расстояние до бонусной клетки — меньше лучше;
    3. длина серии — больше лучше;
    4. давность — по имени (стабильный порядок при равенстве).
    """
    panel = account.panel or {}
    return (
        -_points_of_next_activation(panel),
        _distance_to_bonus(panel),
        -_streak(panel),
        account.name,
    )


def _http_json(
    url: str, token: str, *, method: str = "GET", timeout: float = 20.0
) -> Tuple[int, Dict[str, Any]]:
    """HTTP к панели MiniMax. Сеть/протокол → ``(0, {...})``, как у DeepSeek."""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def signin_panel(account: MinimaxAccount, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """Панель чек-ина аккаунта (7 клеток серии) или ``None``.

    Панель кэшируется на :data:`PANEL_TTL`: внутри суток она не меняется, а
    запрос на каждый аккаунт на каждый чанк — это вхолостую потраченная сеть.
    """
    if not force:
        cached = _panel_cache.get(account.name)
        if cached and (time.monotonic() - cached[0]) < PANEL_TTL:
            account.panel = cached[1]
            return cached[1]
    token = account.token()
    if not token:
        return None
    url = "%s/minimax-cloud/api/v1/signin/status?timezone_id=%s" % (
        AGENT_BASE,
        SIGNIN_TZ_PARAM,
    )
    status, payload = _http_json(url, token)
    if status != 200:
        logger.warning("minimax: панель аккаунта %s недоступна (%s)", account.name, status)
        return None
    account.panel = payload
    _panel_cache[account.name] = (time.monotonic(), payload)
    return payload


def claim_points(account: MinimaxAccount) -> Dict[str, Any]:
    """Забрать очки за сегодня.

    ``claim_result``: 1 — начислили, 2 — «уже получено» (идемпотентно за сутки).
    """
    token = account.token()
    if not token:
        return {"claim_result": 0, "points": 0, "error": "no_token"}
    url = "%s/minimax-cloud/api/v1/signin/claim?timezone_id=%s" % (
        AGENT_BASE,
        SIGNIN_TZ_PARAM,
    )
    status, payload = _http_json(url, token, method="POST")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if status != 200:
        return {"claim_result": 0, "points": 0, "error": payload.get("error") or status}
    return {
        "claim_result": data.get("claim_result"),
        "day_no": data.get("day_no"),
        "points": data.get("points") or 0,
        "expire_at_ms": data.get("expire_at_ms"),
    }


def _ensure_points(account: MinimaxAccount) -> bool:
    """Очки на сегодня получены? Если нет — забрать (один раз в сутки)."""
    panel = account.panel or signin_panel(account)
    if not panel:
        return False
    today = _today_cell(panel)
    if not today:
        return False
    account.activation_index = today.get("day_no")
    account.claimed_points = int(today.get("points") or 0)
    if today.get("status") == 3:
        account.claimed_today = True
        return True
    if today.get("status") != 2:
        return False
    result = claim_points(account)
    account.claimed_today = result.get("claim_result") in (1, 2)
    if result.get("claim_result") == 1:
        logger.info(
            "minimax: аккаунт %s — очки забраны (день %s, %s очков)",
            account.name,
            result.get("day_no"),
            result.get("points"),
        )
    return bool(account.claimed_today)


def _report_activation(account_id: Any, account: MinimaxAccount, *, email: str = "") -> bool:
    """Отметить активацию в журнале Кармана.

    Отмечаем **все** активации Сарафана, а не только арендованные: иначе
    владелец увидит у себя аккаунт «свободным», который уже израсходован на
    Сарафане, и предложит его себе же (требование владельца: один аккаунт не
    достается одновременно Сарафану и владельцу).

    ``activation_index`` — клетка, которую мы увидели на панели MiniMax: у
    Кармана это истина, а расхождение с их предсказанием помечается флагом
    ``mismatch`` (их правило клетки от чужого показания не меняется).

    **Идентификатор — почта** (Карман принимает email вместо внутреннего id с
    14.11): нам не нужно знать их номера, и учёт не рвётся в день, когда
    аккаунт уже отмечен и ручка его не выдаёт.
    """
    if not karman_minimax.enabled():
        return False
    if account_id is None and not (email or account.email):
        logger.warning(
            "minimax: у профиля %s нет ни id, ни почты Кармана — активация не отмечена",
            account.name,
        )
        return False
    try:
        record = karman_minimax.report_activation(
            account_id,
            email=email or account.email or None,
            points=account.claimed_points or None,
            activation_index=account.activation_index,
        )
    except RuntimeError as exc:
        logger.warning("minimax: активация не записана (%s)", exc)
        return False
    if record.get("mismatch"):
        logger.warning(
            "minimax: у Кармана расхождение по очкам аккаунта (ожидали %s, у нас %s)",
            record.get("points_expected"),
            record.get("points"),
        )
    return True


#: Ключ записи токена в сторе MiniMax Code. Рантайм ищет запись по ключу,
#: который выводится из **абсолютного пути** каталога данных профиля, — поэтому
#: для профиля, собранного на лету, ключ надо посчитать заранее.
TOKEN_KEY_PREFIX = "com.minimax.mcode.oauth.prod.en"
TOKEN_CLIENT_ID = "mcode-public"
#: Заглушка ``refreshToken``, когда Карман refresh не присылает. Рантайм MiniMax
#: Code требует это поле непустым (иначе запись считается битой и CLI отказывается
#: работать вовсе), а реального refresh у нас может не быть. Продлить вход
#: заглушкой нельзя — после истечения access мы берём следующий аккаунт.
REFRESH_PLACEHOLDER = "mmoat_placeholder_refresh_not_provided"
#: Запасной срок жизни access-токена, если Карман не прислал ``expires_at_ms``.
#: Замерено 14.11 на реальном входе: access живёт ≈ 9 ч. Ставим меньше, чтобы
#: рантайм не считал токен вечным.
DEFAULT_TOKEN_TTL_MS = 8 * 60 * 60 * 1000


def accounts_root(accounts_dir: Optional[str] = None) -> Path:
    """Каталог пула аккаунтов (тот же, что читает :func:`discover_accounts`)."""
    return Path(accounts_dir or os.getenv(ENV_ACCOUNTS_DIR) or DEFAULT_ACCOUNTS_DIR)


def token_store_key(auth_home: Path) -> str:
    """Ключ, под которым рантайм ищет запись токена для этого пути."""
    digest = hashlib.sha256(("%s\0%s" % (str(auth_home), TOKEN_CLIENT_ID)).encode("utf-8")).digest()
    suffix = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return "%s\0%s" % (TOKEN_KEY_PREFIX, suffix)


def _read_store_record(store_path: Path, key: str) -> Dict[str, Any]:
    """Прежняя запись токен-стора по ключу (пусто — если записи нет)."""
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return {}
    record = records.get(key)
    return record if isinstance(record, dict) else {}


#: Файл состояния сессии CLI — рядом с токен-стором, в той же папке.
CLI_SESSION_STATE_NAME = "auth-state.json"


def cli_session_state_path(data_dir: Path) -> Path:
    """Путь файла состояния сессии CLI (рантайм MiniMax Code)."""
    return data_dir / "auth" / "prod" / "en" / TOKEN_CLIENT_ID / CLI_SESSION_STATE_NAME


def cli_session_generation(data_dir: Path, fallback: int = 1) -> int:
    """Номер поколения сессии CLI — не ниже того, что CLI уже видел.

    CLI считает поколения и отбрасывает запись, которая выглядит старше
    текущей (защита от отката входа). Поэтому, когда мы пишем токен, поколение
    берём максимум из прежней записи и из файла состояния.
    """
    generation = int(fallback or 1)
    try:
        data = json.loads(cli_session_state_path(data_dir).read_text(encoding="utf-8"))
        generation = max(generation, int(data.get("generation") or 0))
    except (OSError, ValueError, TypeError):
        pass
    return max(1, generation)


def write_cli_session_state(data_dir: Path, expires_at_ms: int, generation: int = 1) -> bool:
    """Записать состояние сессии CLI как «вход есть» (живая проверка 22.11).

    **Зачем это нужно.** Рантайм CLI держит сессию в этом файле, и `status`
    решает всё: при `anonymous` CLI на любой запрос отвечает «Sign in to
    MiniMax to use Agent features» — **даже когда access-токен живой**
    (проверено 22.11: панель вендора по тому же токену отвечает, сеть в
    порядке, а CLI просит войти). Обратно: при `authenticated` CLI берёт
    access из токен-стора и работает.

    Карман отдаёт access-токен, а не сессию CLI, поэтому состояние пишем мы —
    тем же форматом, что пишет сам CLI после успешного входа.

    ⚠️ Срок честный: `expiresAtMs` = срок access-токена. Раньше расхождение
    с записью токена заставляло CLI считать сессию протухшей.
    """
    path = cli_session_state_path(data_dir)
    payload = {
        "schemaVersion": 2,
        "status": "authenticated",
        "storeKind": "file",
        "clientId": TOKEN_CLIENT_ID,
        "scopes": ["agent.default"],
        "audience": "agent-backend",
        "buildEnv": "prod",
        "region": "en",
        "generation": max(1, int(generation or 1)),
        "expiresAtMs": int(expires_at_ms),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("minimax: состояние сессии CLI не записано (%s)", exc)
        return False
    return True


def write_access_token(
    account: MinimaxAccount,
    access_token: str,
    expires_at_ms: int,
    refresh_token: str = "",
) -> bool:
    """Положить access-токен в токен-стор рабочей области и поднять сессию CLI.

    Запись токена и состояние сессии пишутся **вместе**: токен без состояния
    «authenticated» рантайм не примет (живая проверка 22.11 — «Sign in to
    MiniMax» при живом токене).
    """
    store_path = account.auth_path
    auth_home = account.data_dir / "auth"
    key = token_store_key(auth_home.resolve())
    previous = _read_store_record(store_path, key)
    generation = max(
        int(previous.get("generation") or 1),
        cli_session_generation(account.data_dir, 1),
    )
    record = {
        "schemaVersion": 1,
        "accessToken": access_token,
        "tokenType": "Bearer",
        "clientId": TOKEN_CLIENT_ID,
        "scopes": ["agent.default"],
        "audience": "agent-backend",
        "expiresAtMs": int(expires_at_ms),
        "generation": generation,
        "loginEpoch": str(uuid.uuid4()),
        "refreshToken": refresh_token or previous.get("refreshToken") or REFRESH_PLACEHOLDER,
    }
    store = {"schemaVersion": 1, "records": {key: record}}
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("minimax: токен в рабочую область не записан (%s)", exc)
        return False
    write_cli_session_state(account.data_dir, int(expires_at_ms), generation)
    return True


def renew_account_token(account: MinimaxAccount) -> bool:
    """Продлить вход **того же** аккаунта (Карман отдаёт свежий access).

    **Зачем.** Отказ «Sign in to MiniMax» — это не «аккаунт мёртв»: по тому же
    access-токену панель вендора отвечает (живая проверка 22.11), сломана
    СЕССИЯ CLI. Новый аккаунт тут не помогает — он умирает так же, и пул
    выедается (замер 22.11: 118 аккаунтов за сутки, 0 успешных вызовов, трафик
    в платный Дипсик). Продление же возвращает свежий access того же
    аккаунта: refresh-цепочка у Кармана своя, новый вход владельцу не нужен.
    """
    account_id = account.karman_id
    if account_id is None:
        return False
    try:
        renewed = karman_minimax.renew_account(account_id)
    except RuntimeError as exc:
        logger.warning("minimax: Карман не продлил аккаунт %s (%s)", account_id, exc)
        return False
    access = str(renewed.get("access_token") or "")
    if not access:
        logger.warning("minimax: Карман продлил аккаунт %s без токена", account_id)
        return False
    expires_at_ms = int(renewed.get("expires_at_ms") or (time.time() * 1000 + DEFAULT_TOKEN_TTL_MS))
    if not write_access_token(account, access, expires_at_ms):
        return False
    logger.info(
        "minimax: вход аккаунта %s продлён (до %s)",
        account_id,
        datetime.fromtimestamp(expires_at_ms / 1000).isoformat(),
    )
    return True


def _log_journal_project(area_name: str) -> None:
    """Написать в лог фактический журнальный проект, под которым берём аккаунты.

    Значение приходит из ``MINIMAX_PROJECT`` (по умолчанию — имя комнаты).
    Переопределение переменной — не ошибка само по себе, но если оно разошлось
    с именем комнаты, продление входа будет отклоняться с 409, и узнать об
    этом можно только здесь.
    """
    headers = karman_minimax.consumer_headers()
    project = str(headers.get("X-MiniMax-Project") or "?")
    expected = (karman_minimax.ROOM_PROJECT or "").strip()
    if expected and project != expected:
        logger.warning(
            "minimax: журнальный проект %s не совпадает с проектом комнаты %s — "
            "продление входа может отклоняться (409)",
            project,
            expected,
        )
    logger.info(
        "minimax: аренда у Кармана под журнальным проектом %s (область %s)",
        project,
        area_name,
    )


def materialize_leased_profile(
    account_id: Any, offer: Any, accounts_dir: Optional[str] = None
) -> Optional[MinimaxAccount]:
    """Положить токен выданного аккаунта в рабочую область.

    Рабочая область **одна** (заказ владельца 14.11): в ней мы вершим посты и
    меняем только аккаунт, когда бонус исчерпан, — контекст и настройки CLI
    остаются теми же. Поэтому токен **перезаписывает** токен-стор области, а
    не создаёт новый профиль: путь области постоянен, значит и ключ записи
    токена (он выводится из пути) остаётся верным.

    Копировать профили вручную и сопоставлять «почта → профиль» больше не
    нужно: токен приходит вместе с аккаунтом.

    Обязателен только ``access_token`` — это и есть «вход в аккаунт».
    ``refresh_token`` — страховка: он продлевает **тот же** вход, когда access
    истёк (замерено 14.11: жизнь access-токена ≈ 9 ч).

    ⚠️ Но поле ``refreshToken`` в записи токен-стора рантайм MiniMax Code
    требует **обязательно** (проверено по коду CLI 14.11: `CREDENTIAL_RECORD_
    CORRUPT`, «The stored OAuth credential record is invalid», если строки нет
    или она пустая). Поэтому, когда Карман refresh не присылает (он перестал
    его давать 14.11), мы пишем заглушку: access-токен при этом работает
    как обычно, а попытка продлить его (после истечения access) честно
    провалится — тогда мы просто берём следующий аккаунт.
    """
    access_token = str(getattr(offer, "access_token", "") or "")
    refresh_token = str(getattr(offer, "refresh_token", "") or "")
    if not access_token:
        return None
    area_name = (os.getenv(ENV_WORK_AREA) or DEFAULT_WORK_AREA).strip() or DEFAULT_WORK_AREA
    root = accounts_root(accounts_dir) / area_name
    expires_at_ms = getattr(offer, "expires_at_ms", None)
    # Поля ``generation`` и ``loginEpoch`` — обязательные для рантайма MiniMax
    # Code: без них CLI отвергает запись («stored OAuth credential record is
    # invalid», замерено 14.11 на боксе). ``loginEpoch`` — метка входа: новый
    # токен = новый вход, поэтому epoch всегда свежий; ``generation`` наследуем
    # из прежней записи и из состояния сессии CLI.
    #
    # Запись токена и состояние сессии CLI пишутся **вместе** (``write_access_token``):
    # токен без состояния «authenticated» рантайм не примет — на живой проверке
    # 22.11 CLI отвечал «Sign in to MiniMax» при живом токене.
    account = MinimaxAccount(
        name=root.name,
        root=root,
        email=str((getattr(offer, "account", None) or {}).get("email") or ""),
        karman_id=account_id,
    )
    if not write_access_token(
        account,
        access_token,
        int(expires_at_ms or (time.time() * 1000 + DEFAULT_TOKEN_TTL_MS)),
        refresh_token,
    ):
        return None
    # Кто сейчас в области — для проб и для владельца.
    try:
        previous_meta = _read_profile_meta(root)
        meta = {
            "karman_account_id": account_id,
            "email": str((getattr(offer, "account", None) or {}).get("email") or ""),
            "updated_at_ms": int(time.time() * 1000),
            # День получения токена: отчёт об активации привязан к выдаче,
            # а не к календарю (вчерашний аккаунт Карман сегодня не выдавал —
            # его ручка отвечает 404).
            "leased_day": karman_minimax.moscow_today(),
        }
        # Общее состояние пула (счётчик сухих отказов и общая пауза) переживает
        # смену аккаунта: иначе каждый новый аккаунт обнулял бы счётчик — и
        # пул снова сжигался бы (живой факт 15.11: 15 аккаунтов за 2 минуты).
        # Список ключей — один (``SHARED_META_KEYS``): свой список здесь
        # разошёлся бы с тем, что чистит движок, молча.
        for key in SHARED_META_KEYS:
            if key in previous_meta:
                meta[key] = previous_meta[key]
        (root / "profile.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    if not refresh_token:
        logger.info(
            "minimax: Карман отдал только access-токен — вход рабочий, "
            "но продлить его автоматически нечем"
        )
    logger.info("minimax: рабочая область %s переведена на новый аккаунт", root.name)
    # Журнальный проект, под которым мы берём аккаунт, — это **владелец**
    # аккаунта для Кармана: ручка ``renew`` продлевает аккаунт только той
    # комнате, чей проект совпал с проектом выдачи, и отказ приходит молча
    # (409). Поэтому фактическое значение заголовка пишем в лог — иначе
    # «продление не сработало» нечем диагностировать, кроме чтения env.
    _log_journal_project(root.name)
    # Данные аккаунта — из выдачи Кармана: они нужны журналу активаций
    # (клетка серии, почта, id) и не должны теряться при переезде токена.
    offered = getattr(offer, "account", None) or {}
    cell = offered.get("activation_index")
    try:
        return MinimaxAccount(
            name=root.name,
            root=root,
            email=str(offered.get("email") or ""),
            karman_id=account_id,
            claimed_points=int(offered.get("points_next") or 0),
            activation_index=int(cell) if cell is not None else None,
        )
    except (TypeError, ValueError):
        return MinimaxAccount(name=root.name, root=root, karman_id=account_id)


def _read_profile_meta(root: Path) -> Dict[str, Any]:
    """Метаданные профиля (``profile.json``): почта аккаунта и его id в Кармане.

    Файл необязательный: профиль без него работает, но аккаунт не узнаётся по
    выдаче Кармана и активация не отмечается в его журнале.
    """
    try:
        data = json.loads((root / PROFILE_META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_accounts(accounts_dir: Optional[str] = None) -> List[MinimaxAccount]:
    """Найти аккаунты в каталоге пула (каталог = профиль CLI)."""
    root = Path(accounts_dir or os.getenv(ENV_ACCOUNTS_DIR) or DEFAULT_ACCOUNTS_DIR)
    if not root.is_dir():
        return []
    out: List[MinimaxAccount] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        account = MinimaxAccount(name=entry.name, root=entry)
        if not account.auth_path.exists():
            continue
        meta = _read_profile_meta(entry)
        account.email = str(meta.get("email") or "").strip()
        karman_id = meta.get("karman_account_id")
        account.karman_id = int(karman_id) if karman_id is not None else None
        out.append(account)
    return out


def _match_lease_account(
    accounts: List[MinimaxAccount], lease: Dict[str, Any]
) -> Optional[MinimaxAccount]:
    """Профиль для аккаунта, выданного Карманом.

    Сначала по id (он у нас точный), потом по почте. Почва — идентификатор
    входа у Кармана, и он же лежит в метаданных профиля.
    """
    wanted_id = lease.get("id")
    wanted_email = str(lease.get("email") or "").strip().lower()
    if wanted_id is not None:
        for account in accounts:
            if account.karman_id is not None and account.karman_id == int(wanted_id):
                return account
    if wanted_email:
        for account in accounts:
            if account.email.strip().lower() == wanted_email:
                return account
    return None


def _is_quota_error(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in _QUOTA_MARKERS)


def _is_auth_error(detail: str) -> bool:
    """Протух ли вход в аккаунт (нужен другой аккаунт, а не ремонт этого)."""
    low = (detail or "").lower()
    if not low:
        return False
    return any(marker in low for marker in _AUTH_MARKERS)


def _classify_failure(detail: str, default: str = "engine_failed") -> str:
    """Разобрать отказ CLI: кончились очки, протух вход или что-то ещё."""
    if _is_quota_error(detail):
        return "quota_exhausted"
    if _is_auth_error(detail):
        return "auth_expired"
    if _looks_like_memory_failure(detail):
        return "engine_oom"
    return default


def _state_max_mb() -> float:
    """Потолок состояния CLI в мегабайтах."""
    try:
        return max(1.0, float(os.getenv(ENV_STATE_MAX_MB) or DEFAULT_STATE_MAX_MB))
    except ValueError:
        return float(DEFAULT_STATE_MAX_MB)


def _node_max_old_space_mb() -> int:
    """Потолок кучи V8 для CLI в мегабайтах."""
    try:
        return max(64, int(os.getenv(ENV_NODE_MAX_OLD_SPACE_MB) or DEFAULT_NODE_MAX_OLD_SPACE_MB))
    except ValueError:
        return int(DEFAULT_NODE_MAX_OLD_SPACE_MB)


def _state_size_mb(data_dir: Path) -> float:
    """Размер состояния CLI в области (МБ).

    Обход целиком, а не «по верхнему уровню»: состояние лежит не в одном
    файле, а в дереве (сессии, sqlite-состояние, логи, каталоги запусков) —
    22.11 это 31 112 файлов на 897 МБ. Обход дорогой ровно тогда, когда состояние
    уже большое (то есть когда мы всё равно его сбрасываем), а после сброса он
    дешёвый: десятки мегабайт.
    """
    total = 0
    try:
        for root, _dirs, files in os.walk(data_dir):
            for name in files:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    except OSError:
        return 0.0
    return total / (1024.0 * 1024.0)


def _reset_cli_state(data_dir: Path) -> None:
    """Сбросить накопленное состояние CLI, **сохранив вход**.

    Токен-стор лежит в ``<область>/data/auth/prod/en/mcode-public/auth.json``,
    а его ключ выводится из **пути** (``token_store_key``) — поэтому путь
    области менять нельзя, чистим содержимое. ``config.yaml`` тоже не трогаем
    (он root-owned, а движку он не нужен: модель и режим передаются флагами).

    Если что-то снести не удалось (файл под root — в старом состоянии такие
    были, 18.11) — **это не молчит**: остаток пишем в лог, иначе сторож
    будет тихо сбрасывать не всё и через сутки упрётся в тот же OOM.
    """
    keep = {"auth", "config.yaml"}
    try:
        entries = list(data_dir.iterdir())
    except OSError:
        return
    left = []
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            if entry.exists():
                left.append(entry.name)
        except OSError:
            left.append(entry.name)
    if left:
        logger.warning(
            "minimax: состояние CLI сброшено частично — не удалось убрать: %s",
            ", ".join(sorted(left)[:10]),
        )


def _ensure_cli_state_bounded(account: MinimaxAccount) -> None:
    """Не дать состоянию CLI вырасти до объёма, который убивает бокс.

    Замер 22.11: состояние 897 МБ → RSS CLI 554 МБ → OOM-килл ядра → **каждый**
    вызов в платный Дипсик (945 и 953 отказа за два дня, ноль успешных).
    Потолок 64 МБ держит RSS в пределах ~315 МБ — он помещается в свободную
    память бокса (601–684 МБ).
    """
    data_dir = account.data_dir
    size = _state_size_mb(data_dir)
    limit = _state_max_mb()
    if size <= limit:
        return
    logger.warning(
        "minimax: состояние CLI %.0f МБ (потолок %.0f МБ) — сбрасываю: "
        "иначе RSS убьёт бокс и весь трафик уйдёт в платный Дипсик",
        size,
        limit,
    )
    _reset_cli_state(data_dir)


def _looks_like_memory_failure(detail: str) -> bool:
    """Съел ли CLI память (куча V8 или ядро)."""
    low = (detail or "").lower()
    if not low:
        return False
    return any(marker in low for marker in _MEMORY_MARKERS)


def _killed_by_kernel(returncode: int) -> bool:
    """Сняло ли **ядро** (SIGKILL), а не сам движок.

    Ядро снимает процесс сигналом 9 → код возврата ``-9`` (или 137, если
    кодировка shell). Это не тот же класс, что отказ V8 по куче
    (``engine_oom``): в одном случае бокс не дал памяти, в другом — движок
    упёрся в свой потолок. Различать надо: лечатся они по-разному, и в журнале
    видно, сколько стоило боксу, а сколько — движку.
    """
    return returncode in (-9, 137)


def _killed_by_memory(returncode: int, stderr: str) -> bool:
    """Различаем «убит по памяти» и прочие отказы.

    Ядро снимает процесс сигналом 9 → код возврата ``-9`` (или 137, если
    кодировкаshell). Это **не** вина аккаунта: другой аккаунт на том же боксе
    даст тот же отказ, поэтому ротация тут бессмысленна — правильное действие
    сбросить накопленное состояние и повторить вызов.
    """
    if _killed_by_kernel(returncode):
        return True
    return _looks_like_memory_failure(stderr)


def run_classification(
    account: MinimaxAccount,
    *,
    system: str,
    user: str,
    timeout: Optional[float] = None,
    task: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    """Один вызов MiniMax Code: ``(ok, текст ответа, детали прогона)``.

    Ответ приходит JSON-объектом в ``exec.result``; в ``content`` отдаём
    ``result`` — это и есть JSON вердиктов, который ждёт ``parse_chunk_response``.

    ``task`` — задание агенту (что делать с файлом). Пустое = прежнее задание
    классификатора; для переписывания и конвейера задание своё (см.
    ``task_text_for``) — иначе модель отвечает вердиктами вместо пересказа.
    """
    # CLI MiniMax отказывается работать с пустым файлом системного промта
    # («--system-prompt-file ... is empty»). Это **наша** ошибка (вызывающий
    # не дал промт), а не отказ аккаунта — но выглядит как поломка движка и
    # стоит вызова. 24.11 так discovery откатывался на платный Дипсик весь
    # ночной срез. Проверяем до всего остального: пустой промт — это наш баг,
    # и прятать его за «cli_missing» незачем.
    system_text = (system or "").strip()
    if not system_text:
        logger.error("minimax: пустой системный промт — вызов не делаем")
        return False, "", {"reason": "empty_system", "detail": "системный промт пуст"}

    cli = os.getenv(ENV_CLI) or DEFAULT_CLI
    # which() возвращает путь С расширением (mcode.cmd на Windows) — без него
    # subprocess файл не находит: он не ищет по PATHEXT.
    cli_path = shutil.which(cli)
    if not cli_path:
        return False, "", {"reason": "cli_missing", "detail": cli}
    limit = float(timeout or os.getenv(ENV_TIMEOUT) or DEFAULT_TIMEOUT)
    model = (os.getenv(ENV_MODEL) or DEFAULT_MODEL).strip()
    cli_mode = (os.getenv(ENV_CLI_MODE) or DEFAULT_CLI_MODE).strip()
    try:
        max_steps = max(1, int(os.getenv(ENV_MAX_STEPS) or DEFAULT_MAX_STEPS))
    except ValueError:
        max_steps = DEFAULT_MAX_STEPS

    tmpdir = Path(tempfile.mkdtemp(prefix="minimax-"))
    lease = None
    try:
        system_file = tmpdir / "system.md"
        system_file.write_text(system_text, encoding="utf-8")
        # Посты — файлом-вложением, а не строкой в промпте. Замерено 13.11:
        # агент-кодер содержимое задачи не считает данными и просит «пришлите
        # посты», пока содержимое не придёт вложением. В lightweight-режиме
        # файлов он вообще не видит («нет доступа к локальным файлам»), поэтому
        # режим контекста тут — standard.
        posts_file = tmpdir / "posts.md"
        posts_file.write_text(user or "", encoding="utf-8")
        answer_file = tmpdir / "answer.json"

        env = dict(os.environ)
        # Прокси в окружении ломает вход в аккаунт (G536) — CLI читает
        # HTTP_PROXY/HTTPS_PROXY/ALL_PROXY сам.
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env.pop(key, None)
        env["MCODE_CONFIG_DIR"] = str(account.config_dir)
        env["MINIMAX_DATA_DIR"] = str(account.data_dir)
        env["MAVIS_DATA_DIR"] = str(account.data_dir)
        # IPv4: на боксе нет маршрута IPv6, а DNS отдаёт AAAA первыми — без
        # этого Node ждёт соединения до таймаута (замерено 13.11).
        #
        # Потолок кучи V8 — страховка бокса: без него Node выбирает лимит по
        # памяти машины (~768 МБ на боксе в 1536 МБ), процесс раздувается до
        # ~900 МБ, и **ядро снимает крупнейший процесс** — то есть не CLI, а
        # воркер (замер 22.11: OOM-килл CLI при RSS 554 МБ).
        env["NODE_OPTIONS"] = (
            env.get("NODE_OPTIONS", "")
            + " --dns-result-order=ipv4first"
            + " --max-old-space-size=%d" % _node_max_old_space_mb()
        ).strip()

        cmd = [
            cli_path,
            "exec",
            "--permission",
            "off",
            "--output-format",
            "json",
            "--output-last-message",
            str(answer_file),
            "--system-prompt-file",
            str(system_file),
            "--max-steps",
            str(max_steps),
            "--mode",
            cli_mode,
            "--timeout",
            "%ds" % int(limit),
        ]
        if model:
            cmd += ["--model", model]
        cmd += ["--file", str(posts_file)]
        cmd.append((task or "").strip() or TASK_TEXT)

        # Перед вызовом — сторож состояния CLI: накопленное состояние тянет RSS
        # процесса вверх, и ядро снимает CLI (а вместе с ним — воркер) или V8
        # упирается в кучу. Замер 22.11: 897 МБ состояния → RSS 554 МБ → OOM.
        _ensure_cli_state_bounded(account)

        # ── Ёмкость бокса: место под ещё один CLI есть? ──
        #
        # Один вызов MiniMax — это процесс ~315 МБ RSS (замер 25.11), а бокс —
        # 1536 МБ без свопа, из них база ~1000 МБ. Запас под CLI — ~536 МБ, то
        # есть **один** вызов влезает, а два (630 МБ) — нет: ядро снимает
        # крупнейший процесс, и это CLI.
        #
        # Что из этого выросло 25.11: ночной срез discovery поднимал 4 CLI
        # разом → **125 ядерных убийств за ночь**, каждый отказ уходил в
        # **платный Дипсик** (122 платных вызова, ≈1 в минуту). Утренняя волна
        # 07:00–07:40 — то же без среза: MiniMax с 0 % успеха, очередь выросла
        # 15 → 72.
        #
        # Слот и проверка памяти — **общие для движка**, а не только для
        # discovery (``DISCOVERY_RECHECK_MAX_CONCURRENT`` покрывал лишь ночной
        # срез): CLI поднимает любая волна, сборник и переписывание.
        lease = minimax_capacity.lease(ttl_seconds=int(limit) + 120)
        if not lease.ok:
            logger.warning(
                "minimax: вызов не запускаю — %s (%s)",
                lease.reason,
                lease.detail,
            )
            return (
                False,
                "",
                dict(
                    lease.details,
                    reason=lease.reason,
                    detail=lease.detail,
                ),
            )

        started = time.monotonic()
        proc = None
        stdout = ""
        stderr = ""
        mem_killed = False
        for attempt in range(2):
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit + 60,
            )
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            if not _killed_by_memory(proc.returncode, stderr):
                break
            # Ядро сняло процесс — это про бокс; V8, отказавший по своей куче
            # (``engine_oom``), — про движок. Отличаем: лечатся по-разному.
            mem_killed = _killed_by_kernel(proc.returncode)
            if attempt:
                break
            # Повтор имеет смысл, только если бокс тянет ещё один вызов:
            # на голодном боксе второй CLI — это ещё одно ядерное убийство и
            # ещё 167 с занятости слота воркера (замер 25.11: убийство в
            # 07:32:53 → повтор → убийство → платный Дипсик).
            retry_ok, retry_mem = minimax_capacity.memory_decision()
            if not retry_ok:
                logger.warning(
                    "minimax: CLI снят по памяти (код %s) — повтор не делаю: "
                    "на боксе %s МБ свободно, вызову нужно %s МБ",
                    proc.returncode,
                    retry_mem.get("mem_available_mb"),
                    retry_mem.get("mem_required_mb"),
                )
                break
            # Убит по памяти — виновато накопленное состояние, а не аккаунт:
            # другой аккаунт на том же боксе дал бы тот же отказ. Сбрасываем
            # состояние и повторяем один раз.
            logger.warning(
                "minimax: CLI снят по памяти (код %s) — состояние сброшено, повторяю вызов",
                proc.returncode,
            )
            _reset_cli_state(account.data_dir)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        details: Dict[str, Any] = {
            "returncode": proc.returncode,
            "elapsed_ms": elapsed_ms,
            # Сколько памяти было на боксе, когда вызов начался: без этого
            # каждый разбор «почему ядро сняло CLI» требовал нового замера.
            "mem_available_mb": lease.details.get("mem_available_mb"),
        }

        payload: Dict[str, Any] = {}
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and candidate.get("type") == "exec.result":
                payload = candidate
                break
        if payload:
            details["status"] = payload.get("status")
            details["model"] = (payload.get("model") or {}).get("modelId")
            details["usage"] = payload.get("usage")
            details["duration_ms"] = payload.get("durationMs")
            if payload.get("status") == "succeeded":
                # Финальный ответ агента лежит в поле ``output`` (замерено
                # 13.11); файл ``--output-last-message`` может остаться пустым.
                answer = str(payload.get("output") or "")
                if not answer and answer_file.exists():
                    answer = answer_file.read_text(encoding="utf-8", errors="replace")
                if not answer and payload.get("result"):
                    answer = str(payload.get("result"))
                if not answer:
                    return False, "", dict(details, reason="empty_response")
                return True, answer, details
            error = payload.get("error") or {}
            message = str(error.get("message") or "")
            reason = _classify_failure(message)
            details["error"] = message
            return False, "", dict(details, reason=reason, detail=message)

        details["stderr"] = stderr[-400:]
        # Начало stderr — **тоже** в деталях: у краша V8 именно там («heap out of
        # memory», «Allocation failed»), а хвост содержит только C++-стек V8, и
        # по нему диагноз не ставится (замер 22.11: причину двухдневного
        # отказа нашли лишь после того, как начало перестало отрезаться).
        details["stderr_head"] = stderr[:400]
        details["stdout"] = stdout[-400:]
        if mem_killed:
            # Ядро сняло процесс (код -9 / 137) — это не «движок не ответил»,
            # а «бокс не дал памяти». Отдельная причина нужна, чтобы видеть
            # класс отказа в журнале: 25.11 таких отказов был 125 за ночь, и
            # каждый уходил в платный Дипсик.
            details["killed_by_kernel"] = True
            return (
                False,
                "",
                dict(
                    details,
                    reason="oom_killed",
                    detail="ядро сняло CLI по памяти (код %s)" % proc.returncode,
                ),
            )
        reason = _classify_failure(stderr, default="exec_failed")
        return False, "", dict(details, reason=reason, detail=stderr[:400] or stderr[-400:])
    except subprocess.TimeoutExpired:
        return False, "", {"reason": "timeout", "detail": "%ds" % int(limit)}
    except Exception as exc:  # noqa: BLE001
        return False, "", {"reason": "runtime", "detail": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Слот вызова отпускаем **всегда**: иначе бокс ждёт следующий вызов,
        # хотя CLI уже отработал (слот протух бы только по TTL).
        if lease is not None:
            lease.release()


def _usage_log_path() -> Path:
    return Path(os.getenv(ENV_USAGE_LOG) or DEFAULT_USAGE_LOG)


def record_usage(
    account: MinimaxAccount,
    *,
    ok: bool,
    reason: str = "",
    detail: str = "",
    points: int = 0,
    posts: int = 0,
    usage: Optional[Dict[str, Any]] = None,
    duration_ms: int = 0,
    label: str = "",
    attempts: int = 0,
) -> None:
    """Записать расход аккаунта в журнал (общий учёт — в Кармане, см. ADR-0017).

    ⚠️ **Журнал — это диагностика, а не летопись.** Прежде сюда не писались
    ``detail`` отказа и длительность вызова, и из-за этого разбор «почему
    половина трафика в Дипсике» пришлось вести по обрывкам лога воркера шесть
    заходов подряд (замер 16.11). Теперь в строке есть всё, что нужно для
    разбора: причина, текст ошибки, сколько попыток и сколько миллисекунд.
    """
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "account": account.name,
        "ok": ok,
        "reason": reason,
        "detail": detail[:300],
        "points": points,
        "posts": posts,
        "duration_ms": int(duration_ms or 0),
        "attempts": int(attempts or 0),
        "label": label,
        "usage": usage or {},
        "project": "Сарафан",
    }
    path = _usage_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("minimax: журнал расхода недоступен (%s)", exc)

    # Тот же вызов — в **общий** журнал нейросетей: панель «Движок
    # классификации» считает по нему все потоки разом (классификация,
    # переписывание, конвейер), а не только то, что пишет каждый движок в свой
    # файл. Свой журнал MiniMax остаётся — в нём есть аккаунт и очки.
    try:
        from modules import neural_usage

        neural_usage.record(
            engine="minimax",
            label=label,
            model=str(os.getenv(ENV_MODEL) or DEFAULT_MODEL),
            ok=ok,
            reason=reason,
            detail=detail,
            posts=posts,
            duration_ms=int(duration_ms or 0),
            account=account.name,
            points=int(points or 0),
        )
    except Exception:  # pragma: no cover — учёт не важнее ответа
        logger.debug("minimax: строка общего журнала не записана", exc_info=True)


def _chat_local(accounts: List[MinimaxAccount], **kwargs: Any) -> Dict[str, Any]:
    """Вызов модели через пул аккаунтов MiniMax Code.

    Контракт тот же, что у остальных движков (``modules/deepseek_client``):
    ``{"ok": True, "content": ..., "model": ..., "usage": ...}`` либо
    ``{"ok": False, "reason": ..., "detail": ...}``.
    """
    system = str(kwargs.get("system") or "")
    user = str(kwargs.get("user") or "")
    if not user.strip():
        return {"ok": False, "reason": "empty_prompt"}

    if not accounts:
        return {"ok": False, "reason": "no_accounts"}

    try:
        max_accounts = max(1, int(os.getenv(ENV_MAX_ACCOUNTS) or DEFAULT_MAX_ACCOUNTS))
    except ValueError:
        max_accounts = DEFAULT_MAX_ACCOUNTS

    # Панели — до выбора: порядок ротации считается по очкам ближайшей
    # активации, а не по имени каталога (правило Кармана).
    for account in accounts:
        signin_panel(account)
    accounts.sort(key=account_sort_key)

    tried: List[str] = []
    last_reason = "accounts_exhausted"
    last_detail = ""

    for account in accounts[:max_accounts]:
        if not _ensure_points(account):
            # Очки сегодня не забрать (уже кончились или ячейка недоступна) —
            # этот аккаунт в проход не годится.
            tried.append(account.name)
            continue
        tried.append(account.name)
        _report_activation(account.karman_id, account)
        ok, content, details = run_classification(
            account,
            system=system,
            user=user,
            timeout=kwargs.get("timeout"),
            task=task_text_for(str(kwargs.get("label") or ""), str(kwargs.get("task") or "")),
        )
        if ok:
            record_usage(
                account,
                ok=True,
                points=int(details.get("points") or 0),
                posts=int(kwargs.get("posts") or 0),
                usage=details.get("usage"),
                label=str(kwargs.get("label") or ""),
            )
            return {
                "ok": True,
                "content": content,
                "model": details.get("model") or os.getenv(ENV_MODEL) or DEFAULT_MODEL,
                "usage": details.get("usage") or {},
                "account": account.name,
            }
        last_reason = str(details.get("reason") or "engine_failed")
        last_detail = str(details.get("detail") or "")
        record_usage(
            account,
            ok=False,
            reason=last_reason,
            points=int(details.get("points") or 0),
            posts=int(kwargs.get("posts") or 0),
            usage=details.get("usage"),
            label=str(kwargs.get("label") or ""),
        )
        if last_reason != "quota_exhausted":
            # Не квота (сеть, CLI, протокол) — следующий аккаунт не поможет.
            break
    else:
        # Все аккаунты кончились по очкам — это НЕ поломка движка: пул разберут
        # завтра, а волна сегодня идёт по обычным фильтрам (fail-open).
        # Причина про квоту не маскируется: «accounts_exhausted» — только если
        # отказа по очкам не было вовсе.
        if last_reason == "accounts_exhausted":
            last_detail = "нет аккаунта с очками на сегодня"

    return {
        "ok": False,
        "reason": last_reason,
        "detail": last_detail,
        "tried_accounts": tried,
    }


def _work_area_account(accounts: List[MinimaxAccount]) -> Optional[MinimaxAccount]:
    """Аккаунт, который сейчас в рабочей области.

    Это наш текущий вход: пока в нём есть очки, мы работаем в нём и не просим
    у Кармана новый (модель владельца 14.11: в рабочей области меняем аккаунт,
    когда бонус исчерпан).

    Аккаунт, у которого сегодня кончились очки, пропускается: иначе каждый
    батч волны тратил бы вызов впустую и спрашивал бы Кармана заново.
    """
    if not accounts:
        return None
    area = (os.getenv(ENV_WORK_AREA) or DEFAULT_WORK_AREA).strip() or DEFAULT_WORK_AREA
    today = karman_minimax.moscow_today()
    for account in accounts:
        if account.name != area:
            continue
        meta = _read_profile_meta(account.root)
        if str(meta.get("exhausted_day") or "") == today:
            return None
        return account
    return accounts[0]


def _remember_exhausted(account: MinimaxAccount) -> None:
    """Пометить: очки этого аккаунта сегодня кончились."""
    meta = _read_profile_meta(account.root)
    meta["exhausted_day"] = karman_minimax.moscow_today()
    try:
        (account.root / PROFILE_META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


#: Ключи общего состояния «пул без очков» — в профиле рабочей области. Файл
#: читают и пишут все процессы воркера, поэтому пауза общая, а не «в этом
#: процессе»: иначе пять параллельных чанков волны сожгли бы по три аккаунта
#: каждый (живой факт 15.11: 15 аккаунтов за две минуты, пауза не сработала).
#:
#: ⚠️ **Все четыре ключа — общие.** Раньше пауза «Карман не отвечает» жила в
#: памяти процесса (``_karman_down_until``), и четыре форка воркера решали
#: независимо, когда спросить Кармана снова. Теперь решение одно на всех.
META_DRY_FAILURES = "dry_failures"
META_PAUSE_UNTIL = "karman_pause_until_ms"
META_KARMAN_DOWN_UNTIL = "karman_down_until_ms"
#: С какого момента движок молчит подряд (мс). Обнуляется на первом успехе.
META_DOWN_SINCE = "down_since_ms"
#: Пул признан мёртвым до этого момента (мс). Пока он не прошёл, волна уходит
#: платной страховке **без попытки** взять ещё один аккаунт: каждый такой
#: вызов — это ещё один сгоревший аккаунт.
META_POOL_DEAD_UNTIL = "pool_dead_until_ms"
#: id аккаунтов, которые умерли с нулевым балансом. Их не просим у Кармана
#: снова — ручка ``exclude=`` для этого и сделана.
META_DEAD_IDS = "dead_account_ids"

#: **Все** ключи общего состояния — одним списком. Он нужен в двух местах: при
#: смене аккаунта (``materialize_leased_profile`` переписывает файл профиля и
#: переносит общее состояние в новый) и здесь. Второй список разошёлся бы с
#: первым молча — а общее состояние в этом файле и есть то, что удерживает
#: паузу для всех процессов воркера.
SHARED_META_KEYS = (
    META_DRY_FAILURES,
    META_PAUSE_UNTIL,
    META_KARMAN_DOWN_UNTIL,
    META_DOWN_SINCE,
    META_POOL_DEAD_UNTIL,
    META_DEAD_IDS,
)


def _work_area_root(accounts_dir: Optional[str] = None) -> Path:
    """Корень рабочей области — там лежит общее состояние пула."""
    area = (os.getenv(ENV_WORK_AREA) or DEFAULT_WORK_AREA).strip() or DEFAULT_WORK_AREA
    return accounts_root(accounts_dir) / area


def _shared_meta(accounts_dir: Optional[str] = None) -> Dict[str, Any]:
    """Общее состояние пула из профиля рабочей области."""
    return _read_profile_meta(_work_area_root(accounts_dir))


def _write_shared_meta(values: Dict[str, Any], accounts_dir: Optional[str] = None) -> None:
    """Записать общее состояние пула (счётчик сухих отказов, паузу)."""
    root = _work_area_root(accounts_dir)
    meta = _read_profile_meta(root)
    meta.update(values)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / PROFILE_META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _karman_pause_active(meta: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    """Пауза после серии аккаунтов без очков — общая для всех воркеров."""
    until = int(meta.get(META_PAUSE_UNTIL) or 0)
    return until > int(now_ms if now_ms is not None else time.time() * 1000)


def _karman_down_active(accounts_dir: Optional[str] = None, now_ms: Optional[int] = None) -> bool:
    """Пауза после сетевого сбоя с Карманом — **общая** для всех воркеров.

    Раньше она жила в памяти процесса (``_karman_down_until``): четыре форка
    воркера решали независимо, кто и когда снова спросит Кармана, и часть
    процессов продолжала долбить ручку, пока другие уже ушли в Дипсик.
    """
    meta = _shared_meta(accounts_dir)
    until = int(meta.get(META_KARMAN_DOWN_UNTIL) or 0)
    return until > int(now_ms if now_ms is not None else time.time() * 1000)


def _note_dry_failure(accounts_dir: Optional[str] = None) -> int:
    """Посчитать аккаунт, умерший без единого токена (пул без очков).

    Счётчик общий: он лежит в профиле рабочей области, поэтому его видят все
    процессы воркера и пауза после него не обходится каждому процессу отдельно.
    """
    count = int(_shared_meta(accounts_dir).get(META_DRY_FAILURES) or 0) + 1
    _write_shared_meta({META_DRY_FAILURES: count}, accounts_dir)
    return count


def _note_dead_account(account_id: Any, accounts_dir: Optional[str] = None) -> None:
    """Запомнить id аккаунта с нулевым балансом — чтобы не просить его снова.

    Карман отдаёт «любой свободный»; без ручки ``exclude=`` он выдавал тот же
    id снова и снова (живой факт 16.11: 69 → 70 → 71 подряд, у каждого баланс
    ноль). Мёртвые id копятся в общем файле и уходят в запрос выдачи.
    """
    if account_id is None:
        return
    try:
        ident = int(account_id)
    except (TypeError, ValueError):
        return
    meta = _shared_meta(accounts_dir)
    ids: List[int] = []
    for value in meta.get(META_DEAD_IDS) or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if ident not in ids:
        ids.append(ident)
    try:
        limit = max(1, int(os.getenv(ENV_DEAD_IDS_LIMIT) or DEFAULT_DEAD_IDS_LIMIT))
    except ValueError:
        limit = DEFAULT_DEAD_IDS_LIMIT
    _write_shared_meta({META_DEAD_IDS: ids[-limit:]}, accounts_dir)


def _dead_account_ids(accounts_dir: Optional[str] = None) -> List[int]:
    """id аккаунтов, которые уже умирали с нулевым балансом."""
    ids: List[int] = []
    for value in _shared_meta(accounts_dir).get(META_DEAD_IDS) or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def _dead_after_sec() -> float:
    """Сколько секунд движок может молчать подряд до передачи волны в Дипсик."""
    try:
        return max(1.0, float(os.getenv(ENV_DEAD_AFTER_SEC) or DEFAULT_DEAD_AFTER_SEC))
    except ValueError:
        return DEFAULT_DEAD_AFTER_SEC


def _pool_dead_ttl() -> float:
    """Сколько секунд пул считается мёртвым после того, как его признали мёртвым.

    Нужно, чтобы следующие чанки **не жгли ещё по аккаунту**: пока пул мёртв,
    волна уходит платной страховке целиком. Сбрасывается первым же успехом.
    """
    try:
        return max(5.0, float(os.getenv(ENV_POOL_DEAD_TTL) or DEFAULT_POOL_DEAD_TTL))
    except ValueError:
        return DEFAULT_POOL_DEAD_TTL


def _retry_after_sec() -> float:
    """Сколько секунд держать страховку после рубежа «движок молчит».

    Раньше срока не было вовсе, и отметка молчания жила до первого успеха —
    то есть до конца суток (живой факт 18.11: 35 723 с молчания при 111
    свободных аккаунтах у Кармана). Теперь после этого срока мы сбрасываем
    отметку и снова пробуем взять аккаунт — контур оживает сам, как только у
    Кармана появляется свободный аккаунт.
    """
    try:
        return max(5.0, float(os.getenv(ENV_RETRY_AFTER_SEC) or DEFAULT_RETRY_AFTER_SEC))
    except ValueError:
        return DEFAULT_RETRY_AFTER_SEC


def _pool_dead_active(accounts_dir: Optional[str] = None, now_ms: Optional[int] = None) -> bool:
    """Пул уже признан мёртвым — волна идёт в платную страховку."""
    until = int(_shared_meta(accounts_dir).get(META_POOL_DEAD_UNTIL) or 0)
    return until > int(now_ms if now_ms is not None else time.time() * 1000)


def _note_down_since(accounts_dir: Optional[str] = None) -> int:
    """Отметить: движок молчит с этого момента (если ещё не отмечено).

    Отметка **общая**: молчание движка — свойство пула, а не процесса.
    """
    meta = _shared_meta(accounts_dir)
    since = int(meta.get(META_DOWN_SINCE) or 0)
    if since:
        return since
    now_ms = int(time.time() * 1000)
    _write_shared_meta({META_DOWN_SINCE: now_ms}, accounts_dir)
    return now_ms


def _down_seconds(accounts_dir: Optional[str] = None, now_ms: Optional[int] = None) -> float:
    """Сколько секунд движок молчит подряд (0 — молчания не было)."""
    since = int(_shared_meta(accounts_dir).get(META_DOWN_SINCE) or 0)
    if not since:
        return 0.0
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    return max(0.0, (now - since) / 1000.0)


def _no_points_ttl() -> float:
    """Пауза после того, как свежий аккаунт отказал сразу (пул без очков)."""
    try:
        return max(1.0, float(os.getenv(ENV_KARMAN_NO_POINTS_TTL) or DEFAULT_KARMAN_NO_POINTS_TTL))
    except ValueError:
        return DEFAULT_KARMAN_NO_POINTS_TTL


def _dead_streak_limit() -> int:
    """Сколько пустых аккаунтов подряд мы выдержим до передачи волны в Дипсик.

    Решение владельца 16.11: десять. Прежде был порог 3, а следствием была
    получасовка пауза, из-за которой половина трафика уходила в платный движок.
    """
    try:
        return max(1, int(os.getenv(ENV_DEAD_STREAK_LIMIT) or DEFAULT_DEAD_STREAK_LIMIT))
    except ValueError:
        return DEFAULT_DEAD_STREAK_LIMIT


def _activation_reported_today(account: MinimaxAccount) -> bool:
    """Отмечали ли мы этот аккаунт у Кармана сегодня."""
    meta = _read_profile_meta(account.root)
    return str(meta.get("activation_day") or "") == karman_minimax.moscow_today()


def _activation_attempted_today(account: MinimaxAccount) -> bool:
    """Пробовали ли мы сегодня отметить активацию этого аккаунта.

    Отметка не удалась (404/409) — значит, повторять её каждый батч бессмысленно:
    это ровно «долбить ручку» десятками запросов в минуту.
    """
    meta = _read_profile_meta(account.root)
    return str(meta.get("activation_attempt_day") or "") == karman_minimax.moscow_today()


def _remember_activation_day(account: MinimaxAccount) -> None:
    """Запомнить: этот аккаунт отмечен у Кармана сегодня."""
    meta = _read_profile_meta(account.root)
    meta["activation_day"] = karman_minimax.moscow_today()
    try:
        (account.root / PROFILE_META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _remember_activation_attempt(account: MinimaxAccount) -> None:
    """Запомнить: отметку активации сегодня уже пробовали (успех или отказ)."""
    meta = _read_profile_meta(account.root)
    meta["activation_attempt_day"] = karman_minimax.moscow_today()
    try:
        (account.root / PROFILE_META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _leased_today(account: MinimaxAccount) -> bool:
    """Получили ли мы этот аккаунт у Кармана **сегодня**.

    Отчёт об активации привязан к факту получения токена (мандат мозга D-220:
    «сколько очков фактически активировалось на только что полученном токене»).
    Аккаунт, взятый вчера и продолжающий работать сегодня, Карман сегодня не
    выдавал — его ручка отвечает 404 («Активация за сегодня не найдена — мы его
    не выдавали»). Отмечать такой аккаунт бессмысленно: отметка привязана к
    выдаче, а не к календарю.
    """
    meta = _read_profile_meta(account.root)
    return str(meta.get("leased_day") or "") == karman_minimax.moscow_today()


def _report_activation_once(account: MinimaxAccount) -> None:
    """Отметить активацию у Кармана — раз в сутки на полученный токен.

    По контракту 14.11 этого **не делаем**: учёт (гашение, очки, статистику)
    Карман ведёт сам в момент выдачи, а ``POST /api/minimax/activations`` звать
    не надо. Функция осталась на случай возврата к прежней схеме — включается
    флагом ``MINIMAX_REPORT_ACTIVATION=1`` (мандат мозга D-220, 15.11: отчёт о
    фактически активированных очках).

    Отчёт уходит **только для токена, полученного сегодня**, и **не повторяется**,
    если ручка отказала: иначе каждый батч бьёт в ручку Кармана (живой факт
    16.11: 404 «мы его не выдавали» каждые ~20 с).
    """
    if not _activation_reporting_enabled():
        return
    if account.karman_id is None:
        return
    if _activation_reported_today(account) or _activation_attempted_today(account):
        return
    if not _leased_today(account):
        return
    try:
        karman_minimax.report_activation(
            account.karman_id,
            email=account.email or None,
            points=account.claimed_points or None,
            activation_index=account.activation_index,
        )
        _remember_activation_day(account)
    except RuntimeError as exc:
        _remember_activation_attempt(account)
        logger.warning("minimax: не отметили активацию аккаунта %s (%s)", account.name, exc)


def _activation_reporting_enabled() -> bool:
    """Шлём ли мы активацию в журнал Кармана (по умолчанию — нет)."""
    raw = (os.getenv(ENV_REPORT_ACTIVATION) or DEFAULT_REPORT_ACTIVATION).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _chat_leased(accounts: List[MinimaxAccount], **kwargs: Any) -> Dict[str, Any]:
    """Работа по аккаунтам, которые выдаёт Карман.

    **Модель владельца (14.11):** мы не занимаемся пулом. Наша работа с
    токенами ровно одна — **спросить у Кармана любой свободный аккаунт и
    получить его**. Какой именно он даст — мы не знаем и знать не должны: учётом,
    ротацией и выбором занимается он. Дальше: заходим под выданным токеном,
    активируем очки и работаем, пока MiniMax не откажет; тогда — снова за
    аккаунтом.

    **Единственное, что мы сообщаем Карману, — факт активации:** сколько очков
    активировалось в этот раз и какая клетка бонуса сработала. Это нужно ему
    для статистики и для точного предсказания, кому он сможет предлагать
    аккаунты дальше.

    **Один аккаунт — на много батчей:** дневная клетка аккаунта — 400–1000
    очков, а это сотни постов, поэтому новый аккаунт берём не на каждый батч,
    а когда текущий перестал работать.

    **Пауза короткая и общая.** Решение владельца 16.11: «пауза 10 секунд и
    повторный запрос за аккаунтом». Прежде пауза после пустого аккаунта была
    получасовой и лежала в памяти процесса — четыре форка воркера жили в
    разных временах, и **ровно половина трафика уходила в Дипсик** (замер:
    514 отказов движка за день, 372 вызова Дипсика на классификацию).

    **Длинная реакция — одна:** десять пустых аккаунтов подряд, и волна уходит
    платной страховке (решение владельца 16.11). До этого — пробуем следующий.
    """
    system = str(kwargs.get("system") or "")
    user = str(kwargs.get("user") or "")

    # Пул уже признан мёртвым — **не берём ещё один аккаунт**: волна идёт
    # платной страховкой целиком, пока не случится успех.
    if _pool_dead_active():
        return {
            "ok": False,
            "reason": "pool_dead",
            "detail": "пул признан мёртвым (волна уходит платной страховке)",
            "tried_accounts": [],
        }

    # Движок молчит дольше порога — это уже не «случайный сбой», а отказ
    # контура: волна уходит платной страховке (решение владельца 16.11 —
    # Дипсик держит «штучные сбои», а не половину трафика).
    down_for = _down_seconds()
    if down_for >= _dead_after_sec():
        # …но **не навсегда**. Раньше отметка молчания жила до первого успеха,
        # а успех невозможен, если не пробовать: контур залипал в pool_dead на
        # сутки (живой факт 18.11 — 35 723 с молчания при 111 свободных
        # аккаунтах у Кармана, 100 % трафика в платный Дипсик).
        #
        # Теперь страховка держится ``RETRY_AFTER_SEC``, после чего мы
        # сбрасываем отметку и снова идём за аккаунтом к Карману.
        if down_for < _dead_after_sec() + _retry_after_sec():
            return {
                "ok": False,
                "reason": "pool_dead",
                "detail": "движок MiniMax молчит %.0f с подряд (порог %.0f с)"
                % (
                    down_for,
                    _dead_after_sec(),
                ),
                "tried_accounts": [],
            }
        _write_shared_meta({META_DOWN_SINCE: 0})
        logger.warning(
            "minimax: контур молчал %.0f с — пробуем снова взять аккаунт у Кармана",
            down_for,
        )

    # Пауза после сетевого сбоя Кармана — **общая** (файл рабочей области), а не
    # в памяти процесса: иначе часть форков продолжала долбить ручку, пока
    # другие уже ушли в Дипсик.
    if _karman_down_active():
        _note_down_since()
        return {
            "ok": False,
            "reason": "karman_unavailable",
            "detail": "Карман не отвечает (общая пауза после сбоя)",
            "tried_accounts": [],
        }

    # Общая пауза после пустого аккаунта: тоже 10 секунд — чтобы не долбить
    # ручку и не ждать полчаса.
    if _karman_pause_active(_shared_meta()):
        _note_down_since()
        return {
            "ok": False,
            "reason": "karman_unavailable",
            "detail": "пауза после пустого аккаунта (общая на всех воркеров)",
            "tried_accounts": [],
        }

    try:
        max_leases = max(1, int(os.getenv(ENV_MAX_LEASES) or DEFAULT_MAX_LEASES))
    except ValueError:
        max_leases = DEFAULT_MAX_LEASES

    tried: List[str] = []
    # Фильтр «только эти аккаунты» — по умолчанию выключен: аккаунтами пула
    # занимается Карман, он сам выбирает, какой отдать, и отдаёт токен.
    known_emails: List[str] = []
    if (os.getenv(ENV_EMAILS_FILTER) or DEFAULT_EMAILS_FILTER).strip().lower() in (
        "1",
        "on",
        "yes",
        "true",
    ):
        known_emails = [a.email for a in accounts if a.email]
    last_reason = "accounts_exhausted"
    last_detail = ""

    # Текущий аккаунт: тот, что уже в рабочей области. Он наш — его мы у
    # Кармана заново не просим.
    current = _work_area_account(accounts)

    def _call_model(account: MinimaxAccount) -> Tuple[bool, str, Dict[str, Any]]:
        """Один вызов модели на аккаунте + запись в журнал расхода."""
        ok, content, details = run_classification(
            account,
            system=system,
            user=user,
            timeout=kwargs.get("timeout"),
            task=task_text_for(str(kwargs.get("label") or ""), str(kwargs.get("task") or "")),
        )
        record_usage(
            account,
            ok=ok,
            reason="" if ok else str(details.get("reason") or "engine_failed"),
            detail="" if ok else str(details.get("detail") or ""),
            points=int(details.get("points") or 0),
            posts=int(kwargs.get("posts") or 0),
            usage=details.get("usage"),
            duration_ms=int(details.get("elapsed_ms") or 0),
            label=str(kwargs.get("label") or ""),
            attempts=len(tried),
        )
        return ok, content, details

    def _ok_result(
        account: MinimaxAccount, content: str, details: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Успешный ответ: пул жив, общие паузы снимаем."""
        # Пул жив: серия пустых аккаунтов обнуляется, общая пауза не нужна,
        # «пул мёртв» снимается. Список мёртвых id остаётся — мёртвый аккаунт
        # мёртв.
        _write_shared_meta(
            {
                META_DRY_FAILURES: 0,
                META_PAUSE_UNTIL: 0,
                META_KARMAN_DOWN_UNTIL: 0,
                META_DOWN_SINCE: 0,
                META_POOL_DEAD_UNTIL: 0,
            }
        )
        return {
            "ok": True,
            "content": content,
            "model": details.get("model") or os.getenv(ENV_MODEL) or DEFAULT_MODEL,
            "usage": details.get("usage") or {},
            "account": account.name,
            "leased": True,
            "karman_account_id": account.karman_id,
            "attempts": len(tried),
            "duration_ms": int(details.get("elapsed_ms") or 0),
        }

    for _attempt in range(max_leases + 1):
        if current is not None:
            # ── 1. Работаем на текущем аккаунте ──
            if not _ensure_points(current):
                # «Панель не прочиталась» — это НЕ «очков нет».
                #
                # Живая проверка 23.11: при протухшем access-токене вендор
                # отвечает на панель 401, а движок делал вывод «очков нет» и
                # брал следующий аккаунт — пул выедался. Сначала продлеваем
                # вход ТОГО ЖЕ аккаунта (ручка renew у Кармана); и только
                # если панель всё равно не читается — считаем, что очков нет.
                name = current.name
                panel_unknown = current.panel is None
                if panel_unknown and renew_account_token(current):
                    # Панель после продления читаем заново: прежняя кэш-
                    # строка могла остаться от протухшего токена.
                    current.panel = None
                    if not _ensure_points(current):
                        current = None
                else:
                    current = None
                if current is None:
                    last_reason = "accounts_exhausted"
                    last_detail = (
                        "панель аккаунта %s не читается, вход не продлился" % name
                        if panel_unknown
                        else "у аккаунта %s сегодня нет очков" % name
                    )
            # Вход продлился и панель прочиталась — работаем на нём же.
            if current is not None:
                # Активация в журнале Кармана — раз в сутки на аккаунт: иначе
                # его статистика видела бы нас только в тот день, когда мы его
                # взяли, а не в те дни, когда мы в нём работали.
                _report_activation_once(current)
                tried.append(current.name)
                ok, content, details = _call_model(current)
                if ok:
                    return _ok_result(current, content, details)
                last_reason = str(details.get("reason") or "engine_failed")
                last_detail = str(details.get("detail") or "")
                if last_reason not in ("quota_exhausted", "auth_expired"):
                    return {
                        "ok": False,
                        "reason": last_reason,
                        "detail": last_detail,
                        "tried_accounts": tried,
                    }
                # «Sign in to MiniMax» — это НЕ «аккаунт мёртв».
                #
                # Живая проверка 22.11: по тому же access-токену панель вендора
                # отвечает (7 клеток), сеть в порядке — сломана СЕССИЯ CLI.
                # Новый аккаунт тут не помогает: он умирает так же, и пул
                # выедается (замер 22.11: 118 аккаунтов за сутки, 0 успешных
                # вызовов, трафик ушёл в платный Дипсик). Помогает ПРОДЛЕНИЕ
                # ТОГО ЖЕ аккаунта — ручка renew у Кармана (refresh-цепочка его,
                # новый вход владельцу не нужен).
                if last_reason == "auth_expired" and renew_account_token(current):
                    tried.append(current.name)
                    ok, content, details = _call_model(current)
                    if ok:
                        return _ok_result(current, content, details)
                    last_reason = str(details.get("reason") or "engine_failed")
                    last_detail = str(details.get("detail") or "")
                    if last_reason not in ("quota_exhausted", "auth_expired"):
                        return {
                            "ok": False,
                            "reason": last_reason,
                            "detail": last_detail,
                            "tried_accounts": tried,
                        }
                # Очки кончились — или протух вход: за следующим аккаунтом.
                if last_reason == "quota_exhausted":
                    # Очки кончились сегодня — до завтра этот аккаунт не трогаем
                    # (и не тратим на него вызовы).
                    _remember_exhausted(current)

                # Аккаунт умер, **не потратив ни токена**: это не «наш аккаунт
                # кончился» (тогда бы токены потратились), а «баланс ноль».
                #
                # ⚠️ Такие аккаунты **не пустые**: панель показывает по ним
                # «сегодня 400 очков, забрано» (status 3), а модель отвечает
                # «insufficient balance (1008)». То есть очки на панели есть,
                # а баланс уже ноль — панель про это не говорит (замер 16.11).
                #
                # Дальше по решению владельца 16.11: **следующий аккаунт**
                # (повторный запрос), пауза между попытками — 10 секунд, и
                # только **десять пустых подряд** означают «пул мёртв» → волна
                # уходит платной страховке.
                usage = details.get("usage") or {}
                dry = last_reason == "quota_exhausted" and not int(usage.get("totalTokens") or 0)
                if dry:
                    _note_dead_account(current.karman_id)
                    dry_failures = _note_dry_failure()
                    limit = _dead_streak_limit()
                    if dry_failures >= limit:
                        _write_shared_meta(
                            {
                                "dry_failures": 0,
                                META_POOL_DEAD_UNTIL: int(
                                    time.time() * 1000 + _pool_dead_ttl() * 1000
                                ),
                            }
                        )
                        last_detail = "%s; %d аккаунтов подряд с нулевым балансом — пул мёртв" % (
                            last_detail,
                            dry_failures,
                        )
                        logger.warning(
                            "minimax: %d аккаунтов подряд с нулевым балансом — пул мёртв,"
                            " волна уходит в платный движок-страховку",
                            dry_failures,
                        )
                        return {
                            "ok": False,
                            "reason": "pool_dead",
                            "detail": last_detail,
                            "tried_accounts": tried,
                        }
                    # Пауза перед следующей попыткой — общая и короткая (10 с):
                    # не долбить ручку и не ждать полчаса.
                    until_ms = int(time.time() * 1000 + _no_points_ttl() * 1000)
                    _write_shared_meta({META_PAUSE_UNTIL: until_ms})
                logger.warning(
                    "minimax: аккаунт %s больше не работает (%s: %s) — берём следующий",
                    current.name,
                    last_reason,
                    last_detail[:120],
                )
                current = None

        # ── 2. Следующий аккаунт — у Кармана ──
        # Просим «любой свободный, не активированный сегодня»: конкретные
        # аккаунты мы не заказываем и о них не знаем (их учёт — его зона).
        # Но **мёртвые** (баланс ноль) — исключаем: без этого Карман отдавал
        # тот же id снова (живой факт 16.11: 69 → 70 → 71 подряд).
        try:
            offer = karman_minimax.take_account(
                exclude=_dead_account_ids(),
                emails=known_emails,
            )
        except RuntimeError as exc:
            logger.warning("minimax: Карман не ответил (%s)", exc)
            try:
                down_ttl = max(
                    1.0, float(os.getenv(ENV_KARMAN_DOWN_TTL) or DEFAULT_KARMAN_DOWN_TTL)
                )
            except ValueError:
                down_ttl = DEFAULT_KARMAN_DOWN_TTL
            # Пауза **общая** (файл рабочей области): её видят все форки.
            _write_shared_meta({META_KARMAN_DOWN_UNTIL: int(time.time() * 1000 + down_ttl * 1000)})
            return {
                "ok": False,
                "reason": "karman_unavailable",
                "detail": str(exc)[:300],
                "tried_accounts": tried,
            }
        if offer.empty:
            # Честная пустота: сегодня брать нечего. Это НЕ поломка движка.
            # Причина от Кармана (например ``no_tokens_ready``: аккаунты есть,
            # но ни у одного нет готового входа) — в детали, иначе диагностика
            # врала бы, что свободных аккаунтов нет.
            last_reason = "accounts_exhausted"
            last_detail = "у Кармана нет свободных аккаунтов (свободно %s, скоро %s)%s" % (
                offer.free,
                offer.soon,
                ("; причина: " + str(offer.reason)) if offer.reason else "",
            )
            # Пауза: следующие батчи не должны долбить Кармана впустую.
            try:
                empty_ttl = max(
                    1.0, float(os.getenv(ENV_KARMAN_EMPTY_TTL) or DEFAULT_KARMAN_EMPTY_TTL)
                )
            except ValueError:
                empty_ttl = DEFAULT_KARMAN_EMPTY_TTL
            # Пауза **общая** и короткая (10 с): следующий батч попробует снова.
            _write_shared_meta({META_KARMAN_DOWN_UNTIL: int(time.time() * 1000 + empty_ttl * 1000)})
            break
        if offer.why:
            logger.info(
                "minimax: Карман выбрал аккаунт %s — %s%s",
                (offer.account or {}).get("id"),
                offer.why,
                # Пометка Кармана «рекомендую этот аккаунт» — часть контракта
                # (ADR-0017 §9): показываем рядом с объяснением, чтобы было
                # видно, когда движок берёт рекомендованный, а когда — просто
                # лучший по очкам.
                " (Карман рекомендует)" if offer.recommended else "",
            )
        # Карман ответил — снимаем общую паузу после сбоя связи.
        _write_shared_meta({META_KARMAN_DOWN_UNTIL: 0})
        account_id = (offer.account or {}).get("id")
        account = None
        if account_id is not None and offer.has_tokens:
            # Карман отдал готовый вход — профиль собираем на лету.
            account = materialize_leased_profile(account_id, offer)
        if account is None:
            account = _match_lease_account(accounts, offer.account or {})
        if account is None:
            # Аккаунт от Кармана, а нашего профиля под него нет: вход в этот
            # аккаунт ещё не сделан (device-flow — только браузер владельца).
            # Следующего попросим просто ещё раз — конкретные аккаунты мы не
            # заказываем.
            last_reason = "account_not_authorized"
            last_detail = "аккаунт %s не авторизован на этой машине (нужен вход владельца)" % (
                karman_minimax.account_identity(offer.account),
            )
            logger.warning("minimax: %s — просим следующего", last_detail)
            if not offer.has_tokens:
                logger.warning(
                    "minimax: Карман не отдал токен — работать пока не на чем "
                    "(ждём выдачу access_token в ответе)"
                )
            continue
        if not _ensure_points(account):
            last_reason = "accounts_exhausted"
            last_detail = "у аккаунта %s сегодня нет очков" % account.name
            continue
        tried.append(account.name)
        if account_id is None:
            account_id = account.karman_id
        # Отчёт о факте активации: сколько очков активировалось в этот раз и
        # какая клетка бонуса сработала. Это нужно Карману для его статистики и
        # для точного предсказания, кому он сможет предлагать аккаунты дальше
        # (запрос 15.11). Никакого управления пулом: конкретные аккаунты мы не
        # заказываем и о них не помним.
        if _activation_reporting_enabled():
            try:
                karman_minimax.report_activation(
                    account_id,
                    email=str((offer.account or {}).get("email") or "") or None,
                    points=account.claimed_points or None,
                    activation_index=account.activation_index,
                )
                _remember_activation_day(account)
            except RuntimeError as exc:
                # 409: аккаунт уже записан за другим проектом или занят окном.
                last_reason = "account_conflict"
                last_detail = str(exc)[:200]
                logger.warning("minimax: аккаунт %s занят (%s) — берём другой", account.name, exc)
                continue
        current = account

    return {
        "ok": False,
        "reason": last_reason,
        "detail": last_detail,
        "tried_accounts": tried,
    }


def chat(**kwargs: Any) -> Dict[str, Any]:
    """Вызов модели через пул аккаунтов MiniMax Code.

    Контракт тот же, что у остальных движков (``modules/deepseek_client``):
    ``{"ok": True, "content": ..., "model": ..., "usage": ...}`` либо
    ``{"ok": False, "reason": ..., "detail": ...}``.

    **Кто выбирает аккаунт.** Если ручки учёта у Кармана настроены, аккаунт
    выбирает Карман: он же ведёт статистику и знает, что уже израсходовано.
    Мы активируем выданный аккаунт и работаем на нём. Если ручек нет — работаем
    своим пулом и своей ротацией (как до аренды).
    """
    user = str(kwargs.get("user") or "")
    if not user.strip():
        return {"ok": False, "reason": "empty_prompt"}

    accounts = discover_accounts()
    lease = karman_minimax.enabled()
    if not accounts and not lease:
        return {"ok": False, "reason": "no_accounts"}

    if lease:
        # При аренде локальные профили не обязательны: если Карман отдал токен,
        # профиль собирается на лету.
        return _chat_leased(accounts, **kwargs)
    return _chat_local(accounts, **kwargs)
