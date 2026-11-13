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
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
#: Задача, которую CLI получает вместе с файлом с постами.
TASK_TEXT = (
    "Вынеси вердикты по постам из прикреплённого файла. "
    "Ответ — только JSON по схеме из системного промпта, без пояснений."
)

#: Признаки «очки кончились» в ответах MiniMax Code.
_QUOTA_MARKERS = (
    "insufficient balance",
    "quota",
    "usage limit",
    "credits exhausted",
    "free usage limit",
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

        В сторе несколько записей (десктоп и CLI); берём ту, что не протухла.
        Токен сам обновляется — CLI делает это при первом обращении к профилю.
        """
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        records = list((data.get("records") or {}).values())
        if not records:
            return None
        now_ms = int(time.time() * 1000)
        live = [r for r in records if (r.get("expiresAtMs") or 0) > now_ms]
        best = max(live or records, key=lambda r: r.get("expiresAtMs") or 0)
        token = best.get("accessToken")
        return str(token) if token else None

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
        out.append(account)
    return out


def _is_quota_error(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in _QUOTA_MARKERS)


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
            reason = "quota_exhausted" if _is_quota_error(message) else "engine_failed"
            details["error"] = message
            return False, "", dict(details, reason=reason, detail=message)

        details["stderr"] = stderr[-400:]
        details["stdout"] = stdout[-400:]
        reason = "quota_exhausted" if _is_quota_error(stderr) else "exec_failed"
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


def chat(**kwargs: Any) -> Dict[str, Any]:
    """Вызов модели через пул аккаунтов MiniMax Code.

    Контракт тот же, что у остальных движков (``modules/deepseek_client``):
    ``{"ok": True, "content": ..., "model": ..., "usage": ...}`` либо
    ``{"ok": False, "reason": ..., "detail": ...}``.
    """
    system = str(kwargs.get("system") or "")
    user = str(kwargs.get("user") or "")
    if not user.strip():
        return {"ok": False, "reason": "empty_prompt"}

    accounts = discover_accounts()
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
        last_reason = "accounts_exhausted"
        last_detail = "нет аккаунта с очками на сегодня"

    return {
        "ok": False,
        "reason": last_reason,
        "detail": last_detail,
        "tried_accounts": tried,
    }
