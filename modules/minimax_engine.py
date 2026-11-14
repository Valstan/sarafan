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

from modules import karman_minimax

logger = logging.getLogger(__name__)

#: Хост платформы MiniMax Code (панель чек-ина и остальное).
AGENT_BASE = "https://agent.minimax.io"
#: Часовой пояс для панели чек-ина: IANA-имя, иначе MiniMax отвечает
#: «invalid timezone_id». Московский — чтобы «сегодня» совпадало с московским
#: днём (у MiniMax сутки календарные, в поясе аккаунта).
SIGNIN_TZ = "Europe/Moscow"

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
#: Сколько аккаунтов за один вызов разрешено запросить у Кармана. Больше трёх
#: подряд «пустых» ответов — уже не «очки кончились», а неполадки на стороне
#: Кармана, и волна должна уйти на платный движок-страховку.
ENV_MAX_LEASES = "MINIMAX_MAX_LEASES"
DEFAULT_MAX_LEASES = 5
#: Пауза после сбоя связи с Карманом. Пока она не истекла, мы к нему не ходим:
#: каждый чанк иначе ждал бы сетевого таймаута, а учёт активаций он всё равно
#: не примет. Это НЕ предохранитель движка: волна уходит в платный DeepSeek.
ENV_KARMAN_DOWN_TTL = "MINIMAX_KARMAN_DOWN_TTL"
DEFAULT_KARMAN_DOWN_TTL = 60
#: Пауза после пустой выдачи Кармана (нет аккаунта с готовым входом). Без неё
#: волна на 40 батчей спросила бы Кармана 40 раз подряд и словила его 429
#: («слишком много запросов») — а отвечает он всё равно тем же.
ENV_KARMAN_EMPTY_TTL = "MINIMAX_KARMAN_EMPTY_TTL"
DEFAULT_KARMAN_EMPTY_TTL = 60
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
_karman_down_until = 0.0
#: Файл с метаданными профиля рядом с ``auth.json``: почта аккаунта и его id
#: в Кармане. По почве аккаунт, выданный Карманом, узнаётся у нас.
PROFILE_META_NAME = "profile.json"
#: Задача, которую CLI получает вместе с файлом с постами.
TASK_TEXT = (
    "Вынеси вердикты по постам из прикреплённого файла. "
    "Ответ — только JSON по схеме из системного промпта, без пояснений."
)

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
    "unauthorized",
    "401",
    "invalid token",
    "token expired",
    "invalid_grant",
    "authentication error",
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
        "Europe%2FMoscow",
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
        "Europe%2FMoscow",
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
    истёк (замерено 14.11: жизнь access-токена ≈ 9 ч). Без него работа не
    останавливается — просто по истечении токена придётся взять вход заново.
    """
    access_token = str(getattr(offer, "access_token", "") or "")
    refresh_token = str(getattr(offer, "refresh_token", "") or "")
    if not access_token:
        return None
    area_name = (os.getenv(ENV_WORK_AREA) or DEFAULT_WORK_AREA).strip() or DEFAULT_WORK_AREA
    root = accounts_root(accounts_dir) / area_name
    auth_home = root / "data" / "auth"
    store_path = auth_home / "prod" / "en" / TOKEN_CLIENT_ID / "auth.json"
    expires_at_ms = getattr(offer, "expires_at_ms", None)
    # Поля ``generation`` и ``loginEpoch`` — обязательные для рантайма MiniMax
    # Code: без них CLI отвергает запись («stored OAuth credential record is
    # invalid», замерено 14.11 на боксе). ``loginEpoch`` — метка входа: новый
    # токен = новый вход, поэтому epoch всегда свежий; ``generation`` наследуем
    # из прежней записи.
    previous = _read_store_record(store_path, token_store_key(auth_home.resolve()))
    record = {
        "schemaVersion": 1,
        "accessToken": access_token,
        "tokenType": "Bearer",
        "clientId": TOKEN_CLIENT_ID,
        "scopes": ["agent.default"],
        "audience": "agent-backend",
        "expiresAtMs": int(expires_at_ms or (time.time() * 1000 + DEFAULT_TOKEN_TTL_MS)),
        "generation": int(previous.get("generation") or 1),
        "loginEpoch": str(uuid.uuid4()),
    }
    if refresh_token:
        record["refreshToken"] = refresh_token
    store = {
        "schemaVersion": 1,
        "records": {token_store_key(auth_home.resolve()): record},
    }
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        (root / "config").mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("minimax: токен в рабочую область не записан (%s)", exc)
        return None
    # Кто сейчас в области — для проб и для владельца.
    try:
        meta = {
            "karman_account_id": account_id,
            "email": str((getattr(offer, "account", None) or {}).get("email") or ""),
            "updated_at_ms": int(time.time() * 1000),
        }
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
    return default


def run_classification(
    account: MinimaxAccount,
    *,
    system: str,
    user: str,
    timeout: Optional[float] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Один вызов MiniMax Code: ``(ok, текст ответа, детали прогона)``.

    Ответ приходит JSON-объектом в ``exec.result``; в ``content`` отдаём
    ``result`` — это и есть JSON вердиктов, который ждёт ``parse_chunk_response``.
    """
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
    try:
        system_file = tmpdir / "system.md"
        system_file.write_text(system or "", encoding="utf-8")
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
        env["NODE_OPTIONS"] = (
            env.get("NODE_OPTIONS", "") + " --dns-result-order=ipv4first"
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
        cmd.append(TASK_TEXT)

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
        details: Dict[str, Any] = {"returncode": proc.returncode}

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
        details["stdout"] = stdout[-400:]
        reason = _classify_failure(stderr, default="exec_failed")
        return False, "", dict(details, reason=reason, detail=stderr[-400:])
    except subprocess.TimeoutExpired:
        return False, "", {"reason": "timeout", "detail": "%ds" % int(limit)}
    except Exception as exc:  # noqa: BLE001
        return False, "", {"reason": "runtime", "detail": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _usage_log_path() -> Path:
    return Path(os.getenv(ENV_USAGE_LOG) or DEFAULT_USAGE_LOG)


def record_usage(
    account: MinimaxAccount,
    *,
    ok: bool,
    reason: str = "",
    points: int = 0,
    posts: int = 0,
    usage: Optional[Dict[str, Any]] = None,
) -> None:
    """Записать расход аккаунта в журнал (общий учёт — в Кармане, см. ADR-0017)."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "account": account.name,
        "ok": ok,
        "reason": reason,
        "points": points,
        "posts": posts,
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
        )
        if ok:
            record_usage(
                account,
                ok=True,
                points=int(details.get("points") or 0),
                posts=int(kwargs.get("posts") or 0),
                usage=details.get("usage"),
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


def _activation_reported_today(account: MinimaxAccount) -> bool:
    """Отмечали ли мы этот аккаунт у Кармана сегодня."""
    meta = _read_profile_meta(account.root)
    return str(meta.get("activation_day") or "") == karman_minimax.moscow_today()


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


def _report_activation_once(account: MinimaxAccount) -> None:
    """Отметить активацию у Кармана — раз в сутки на аккаунт.

    Иначе статистика Кармана видела бы нас только в тот день, когда мы взяли
    аккаунт, а не в те дни, когда мы в нём работали. Отказ не роняет волну:
    тогда аккаунт просто не переиспользуется (см. ``_activation_reported_today``).
    """
    if account.karman_id is None or _activation_reported_today(account):
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
        logger.warning("minimax: не отметили активацию аккаунта %s (%s)", account.name, exc)


def _chat_leased(accounts: List[MinimaxAccount], **kwargs: Any) -> Dict[str, Any]:
    """Работа по аккаунтам, которые выдаёт Карман.

    Кто выбирает аккаунт — Карман: он же ведёт журнал и знает, что уже
    израсходовано. Мы получаем аккаунт, активируем его у себя (забираем очки
    чекина — это и есть активация), отмечаем активацию в его журнале и работаем,
    пока очки не кончатся; потом — снова за аккаунтом.

    **Один аккаунт — на много чанков** (модель владельца 14.11: «рабочая
    область — одна, в ней мы меняем аккаунт, когда бонус исчерпан»). Дневная
    клетка аккаунта — 400–1000 очков, а это сотни постов; брать у Кармана
    новый аккаунт на каждый чанк значило бы съедать пул (и квоту активаций) в
    считаные минуты. Поэтому: текущий аккаунт рабочей области мы используем,
    пока он работает, а за следующим идём только когда очки кончились
    (``quota_exhausted``) или вход протух (``auth_expired``).
    """
    global _karman_down_until

    system = str(kwargs.get("system") or "")
    user = str(kwargs.get("user") or "")

    if time.monotonic() < _karman_down_until:
        # Карман лежит: не ходим к нему до конца паузы (каждый чанк иначе ждал
        # бы сетевого таймаута). Волна уходит в платный движок-страховку.
        return {
            "ok": False,
            "reason": "karman_unavailable",
            "detail": "Карман не отвечает (пауза после сбоя)",
            "tried_accounts": [],
        }

    try:
        max_leases = max(1, int(os.getenv(ENV_MAX_LEASES) or DEFAULT_MAX_LEASES))
    except ValueError:
        max_leases = DEFAULT_MAX_LEASES

    tried: List[str] = []
    excluded: List[Any] = []
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

    for _attempt in range(max_leases + 1):
        if current is not None:
            # ── 1. Работаем на текущем аккаунте ──
            if not _ensure_points(current):
                last_reason = "accounts_exhausted"
                last_detail = "у аккаунта %s сегодня нет очков" % current.name
                current = None
            else:
                # Активация в журнале Кармана — раз в сутки на аккаунт: иначе
                # его статистика видела бы нас только в тот день, когда мы его
                # взяли, а не в те дни, когда мы в нём работали.
                _report_activation_once(current)
                tried.append(current.name)
                ok, content, details = run_classification(
                    current,
                    system=system,
                    user=user,
                    timeout=kwargs.get("timeout"),
                )
                if ok:
                    record_usage(
                        current,
                        ok=True,
                        points=int(details.get("points") or 0),
                        posts=int(kwargs.get("posts") or 0),
                        usage=details.get("usage"),
                    )
                    return {
                        "ok": True,
                        "content": content,
                        "model": details.get("model") or os.getenv(ENV_MODEL) or DEFAULT_MODEL,
                        "usage": details.get("usage") or {},
                        "account": current.name,
                        "leased": True,
                        "karman_account_id": current.karman_id,
                    }
                last_reason = str(details.get("reason") or "engine_failed")
                last_detail = str(details.get("detail") or "")
                record_usage(
                    current,
                    ok=False,
                    reason=last_reason,
                    points=int(details.get("points") or 0),
                    posts=int(kwargs.get("posts") or 0),
                    usage=details.get("usage"),
                )
                if last_reason not in ("quota_exhausted", "auth_expired"):
                    return {
                        "ok": False,
                        "reason": last_reason,
                        "detail": last_detail,
                        "tried_accounts": tried,
                    }
                # Очки кончились — или протух вход: за следующим аккаунтом.
                # Истёкший исключаем явно: Карман о нём ничего не знает и
                # может выдать его снова.
                if current.karman_id is not None:
                    excluded.append(current.karman_id)
                if last_reason == "quota_exhausted":
                    # Очки кончились сегодня — до завтра этот аккаунт не трогаем
                    # (и не тратим на него вызовы).
                    _remember_exhausted(current)
                logger.warning(
                    "minimax: аккаунт %s больше не работает (%s: %s) — берём следующий",
                    current.name,
                    last_reason,
                    last_detail[:120],
                )
                current = None

        # ── 2. Следующий аккаунт — у Кармана ──
        try:
            offer = karman_minimax.take_account(exclude=excluded, emails=known_emails)
        except RuntimeError as exc:
            logger.warning("minimax: Карман не ответил (%s)", exc)
            try:
                down_ttl = max(
                    5.0, float(os.getenv(ENV_KARMAN_DOWN_TTL) or DEFAULT_KARMAN_DOWN_TTL)
                )
            except ValueError:
                down_ttl = DEFAULT_KARMAN_DOWN_TTL
            _karman_down_until = time.monotonic() + down_ttl
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
                    5.0, float(os.getenv(ENV_KARMAN_EMPTY_TTL) or DEFAULT_KARMAN_EMPTY_TTL)
                )
            except ValueError:
                empty_ttl = DEFAULT_KARMAN_EMPTY_TTL
            _karman_down_until = time.monotonic() + empty_ttl
            break
        if offer.why:
            logger.info(
                "minimax: Карман выбрал аккаунт %s — %s", (offer.account or {}).get("id"), offer.why
            )
        _karman_down_until = 0.0
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
            # Просим Кармана пропустить его (exclude) и взять следующего.
            excluded.append((offer.account or {}).get("id"))
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
        # Активация в журнале Кармана: с этого момента аккаунт считается
        # израсходованным на Сарафане и владельцу в те же сутки не предлагается.
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
