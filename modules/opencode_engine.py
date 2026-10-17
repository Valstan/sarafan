"""Движок классификации через харнесс OpenCode (пилот, решение владельца 14.10).

Зачем. DeepSeek-контур мёртв с 22.09 (402, P177), и сеть публикует без
нейро-фильтра. Владелец поставил задачу: вердикты выносит харнесс OpenCode на
том же VPS. Здесь — мост между классификатором (контракт
``modules/deepseek_client.chat``: ``{ok, content, model, usage}`` либо
``{ok: False, reason, detail}``) и headless-сервером ``opencode serve``
(systemd-юнит ``setka-opencode.service``, 127.0.0.1).

Архитектура намеренно тонкая:

- **Точка переключения одна** — ``modules/classifier/headless.classify_chunk``
  зовёт этот ``chat`` при ``CLASSIFIER_ENGINE=opencode``; DeepSeek остаётся
  дефолтом, поэтому откат — одна env-переменная, без деплоя кода.
- **Цепочка моделей с фолбэком** (``OPENCODE_MODEL_CHAIN``). Бесплатный тариф
  Zen ротирует модели (владелец 14.10): сегодня отвечают
  longcat/space-bunny/mimo, muse отдаёт ``RegionError`` с IP бокса. Порядок
  цепочки уже написан под будущее: когда бокс получит нужный geo, muse встаёт
  первой сама. Первая модель, ответившая текстом, закрывает вызов.
- **Никогда не бросает** — тот же контракт отказов, что у DeepSeek-клиента:
  ``network`` | ``http_<код>`` | ``empty_response`` | ``engine_all_failed``.
  Классификатор fail-open: нет вердикта — пост идёт по обычным фильтрам.

Почему HTTP-сервер, а не CLI `opencode run`: на этом боксе `run` стабильно
висит после `init` (проверено 14.10, ~170 c, ноль байт вывода), а `serve`
поднимается и отвечает за секунду.

Почему oneshot, а не всегда живой сервис (решение владельца 14.10): бокс —
1.5 ГБ с постоянно живущими uvicorn/celery/postgres; always-on serve держит
~264 МБ и в учебном прогоне 14.10 трижды ложился OOM'ом. Теперь `serve`
поднимается на время прогона и тушится сразу (``harness_server``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from config.opencode import (
    engine_enabled,
    get_base_url,
    get_model_chain,
    get_serve_max_lifetime,
    get_serve_rss_kill_mb,
    get_server_password,
    get_server_username,
    get_spill_glob,
    get_spill_max_age_sec,
    get_timeout,
)

logger = logging.getLogger(__name__)


def _port_of(base_url: str) -> int:
    try:
        return int(base_url.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return 4096


def server_alive(host: str = "127.0.0.1", port: Optional[int] = None) -> bool:
    """Поднят ли уже serve на порту — тогда свой не поднимаем."""
    port = port if port is not None else _port_of(get_base_url())
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _child_rss_mb(pid: int) -> Optional[float]:
    """RSS ребёнка в МБ из /proc; None — процесса уже нет."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024.0
    except OSError:
        return None
    return None


def _systemctl_run(action: str, timeout: float = 30.0) -> bool:
    """start/stop oneshot-юнита харнесса (автоматический путь из воркера).

    Popen из prefork-воркера Celery нестабилен (ребёнок умирает молча —
    боевой замер 14.10: 72/72 чанков Connection refused при живом
    ручном serve). systemd-oneshot надёжнее: serve под сервисом с
    EnvironmentFile, движок только зовёт systemctl (sudo -n, NOPASSWD
    только на этот юнит). Возвращает False, если команда не прошла.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", action, "setka-opencode-run"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 — таймаут/сеть системы
        logger.warning("opencode-engine: systemctl %s не удался: %s", action, e)
        return False
    if result.returncode != 0:
        logger.warning(
            "opencode-engine: systemctl %s rc=%s stderr=%s",
            action,
            result.returncode,
            (result.stderr or "")[:200],
        )
        return False
    return True


@contextmanager
def harness_server() -> Iterator[bool]:
    """Serve ровно на время прогона (oneshot, решение владельца 14.10).

    Бокс — 1.5 ГБ с всегда живущими uvicorn/celery/postgres; постоянно
    запущенный serve держит ~264 МБ и давно ел OOM'ом остальное (учебный
    прогон 14.10: три рестарта serve). Поэтому: порт уже занят живым
    serve — работаем с ним (ручной/отладочный режим); порт свободен —
    поднимаем serve ребёнком на время блока и тушим в finally. Кто
    поднял — тот и тушит: осиротевший процесс не остаётся.

    Yield: True — serve на этом порту ответил (можно звать модели).
    """
    if server_alive():
        yield True
        return

    host, port = "127.0.0.1", _port_of(get_base_url())

    # Автоматический путь (воркер/таска): systemd-oneshot вместо Popen.
    # Popen из prefork-воркера Celery нестабилен (боевой замер 14.10).
    # OPENCODE_SYSTEMD_RUN=1 — включается в env приложения; иначе —
    # прежний Popen-путь (ручные прогоны, отладка).
    if os.getenv("OPENCODE_SYSTEMD_RUN") == "1":
        if not _systemctl_run("start"):
            yield False
            return
        try:
            yield _wait_port(host, port)
        finally:
            _systemctl_run("stop")
        return

    binary = os.environ.get(
        "OPENCODE_BIN",
        str(Path.home() / ".opencode" / "bin" / "opencode"),
    )
    # Прод-диагностика (14.10): в prefork-воркере Celery ребёнок serve молча
    # умирал, DEVNULL скрывал причину. OPENCODE_SERVE_LOG задан — пишем
    # stderr туда; иначе тишина (прод — шум не нужен).
    serve_log = os.environ.get("OPENCODE_SERVE_LOG", "").strip()
    stderr_target: Any = open(serve_log, "ab") if serve_log else subprocess.DEVNULL  # noqa: SIM115
    # Приватный TMPDIR на запуск (P197): opencode распаковывает в temp нативный
    # модуль `.bcdf*.so` (~5.5 МБ) и не убирает его за собой — в общем /tmp это
    # копится по файлу на запуск (инцидент 15.10: 552 шт. ≈ 2.9 ГБ, диск 100 %).
    # Свой каталог уносит мусор этого запуска вместе с собой (rmtree в finally);
    # если opencode пишет мимо TMPDIR — подчистит свипер sweep_opencode_spill.
    child_tmp = tempfile.mkdtemp(prefix="setka-opencode-")
    child_env = {k: v for k, v in os.environ.items() if v}
    child_env["TMPDIR"] = child_tmp
    try:
        child = subprocess.Popen(
            [binary, "serve", "--port", str(port), "--hostname", host],
            env=child_env,
            stdout=stderr_target,
            stderr=stderr_target,
            start_new_session=True,
        )
    except BaseException:
        shutil.rmtree(child_tmp, ignore_errors=True)
        raise
    # Страховочное убийство: длинный батч идёт часами; если ребёнок жив, но
    # разрастается — убиваем по таймеру, следующий чанк поднимет свежего.
    max_lifetime = get_serve_max_lifetime()
    killer: Optional[threading.Timer] = None
    if max_lifetime > 0:
        killer = threading.Timer(max_lifetime, child.kill)
        killer.daemon = True
        killer.start()
    # RSS-монитор (OOM-аудит 14.10): ядро убивало opencode при RSS 507–619 МБ.
    # RLIMIT_AS не подходит (bun резервирует ~74 ГБ VM на старте) — вместо
    # лимита в момент exec следим за RSS в процессе: превысил потолок —
    # убит, следующий чанк поднимет свежего.
    rss_kill_mb = get_serve_rss_kill_mb()
    stop_monitor = threading.Event()

    def _watch_rss() -> None:
        while not stop_monitor.wait(10):
            if child.poll() is not None:
                return
            rss = _child_rss_mb(child.pid)
            if rss is not None and rss > rss_kill_mb:
                logger.warning(
                    "opencode-engine: ребёнок serve RSS %.0f МБ > порога %.0f — убиваем",
                    rss,
                    rss_kill_mb,
                )
                child.kill()
                return

    monitor = threading.Thread(target=_watch_rss, daemon=True)
    monitor.start()
    try:
        yield _wait_port(host, port)
    finally:
        stop_monitor.set()
        if killer is not None:
            killer.cancel()
        if stderr_target is not subprocess.DEVNULL:
            stderr_target.close()
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        shutil.rmtree(child_tmp, ignore_errors=True)


def sweep_opencode_spill(
    directory: Optional[str] = None,
    *,
    pattern: Optional[str] = None,
    max_age_sec: Optional[float] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Убрать temp-мусор opencode старше возраста (страховка P197).

    opencode распаковывает в temp нативный модуль (``.bcdf*.so``, ~5.5 МБ) при
    каждом запуске и не удаляет его; в цикле отказов волн это заполнило ``/tmp``
    прода (15.10: 552 файла ≈ 2.9 ГБ, диск 100 %, ``asyncpg.DiskFullError``).
    Приватный ``TMPDIR`` (Popen-путь в ``harness_server``) убирает мусор своих
    запусков сразу; здесь — вторая ступень для путей, где наш код каталог не
    выбирает (systemd-oneshot-юнит, ручные прогоны).

    Удаляем только старше ``max_age_sec``: живой serve (потолок жизни —
    ``SERVE_MAX_LIFETIME_SEC``) может ещё держать распакованный модуль. Заодно
    подчищаются каталоги прерванных запусков ``setka-opencode-*``. Никогда не
    бросает: свипер, падающий на каждом тике, — это молчащий свипер.
    """
    base = Path(directory) if directory else Path(tempfile.gettempdir())
    mask = pattern or get_spill_glob()
    age = max_age_sec if max_age_sec is not None else get_spill_max_age_sec()
    moment = time.time() if now is None else now
    stats: Dict[str, Any] = {
        "dir": str(base),
        "deleted": 0,
        "freed_bytes": 0,
        "removed_dirs": 0,
        "failed": 0,
    }
    try:
        entries = sorted(set(base.glob(mask)) | set(base.glob("setka-opencode-*")))
    except OSError as e:  # каталога нет / нет прав на чтение
        stats["error"] = f"{type(e).__name__}: {e}"
        return stats

    for path in entries:
        try:
            if not path.exists() or moment - path.stat().st_mtime <= age:
                continue
            if path.is_dir():
                shutil.rmtree(path)
                stats["removed_dirs"] += 1
            elif path.is_file():
                size = path.stat().st_size
                path.unlink()
                stats["deleted"] += 1
                stats["freed_bytes"] += size
        except OSError as e:
            stats["failed"] += 1
            logger.warning("opencode-engine: temp-мусор %s не убран: %s", path, e)

    if stats["deleted"] or stats["removed_dirs"] or stats["failed"]:
        logger.info(
            "opencode-engine: temp-мусор opencode в %s: файлов %d (%.1f МБ),"
            " каталогов %d, ошибок %d",
            base,
            stats["deleted"],
            stats["freed_bytes"] / 1024 / 1024,
            stats["removed_dirs"],
            stats["failed"],
        )
    return stats


# Причины, при которых имеет смысл пробовать следующую модель цепочки.
# 403 может быть как geo (muse), так и перегрев free-tier — оба лечатся
# следующей моделью. 429 — квота конкретной модели. 5xx/сеть — сбой транспорта.
_RETRYABLE_REASONS = (
    "network",
    "http_403",
    "http_429",
    "http_500",
    "http_502",
    "http_503",
    "empty_response",
)


def _auth_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    password = get_server_password()
    if password:
        token = base64.b64encode(f"{get_server_username()}:{password}".encode("utf-8")).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {token}"
    return headers


def _post_json(path: str, body: Dict[str, Any], timeout: float) -> Tuple[int, Any]:
    """POST на serve. Сеть/протокол → ``(0, {"error": ...})``, как у DeepSeek."""
    req = urllib.request.Request(
        f"{get_base_url()}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=_auth_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return int(resp.status), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover — тело ошибки не обязано читаться
            pass
        return int(e.code), {"error": detail}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"[:300]}


def _post_json_lines(path: str, body: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]]]:
    """POST с потоком JSON-lines (ответ ``/message``).

    Поток читается по строкам; мусорные строки пропускаются. Сетевой сбой
    до первого байта → ``(0, [...])``: различение с «пустым потоком» важно,
    первое — транспорт (следующая модель), второе — модель промолчала.
    """
    req = urllib.request.Request(
        f"{get_base_url()}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=_auth_headers(),
    )
    events: List[Dict[str, Any]] = []
    try:
        with urllib.request.urlopen(req, timeout=get_timeout()) as resp:
            status = int(resp.status)
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if isinstance(evt, dict):
                    events.append(evt)
            return status, events
    except urllib.error.HTTPError as e:
        return int(e.code), [{"error": e.read().decode("utf-8", "replace")[:300]}]
    except Exception as e:
        return 0, [{"error": f"{type(e).__name__}: {e}"[:300]}]


def parse_stream(
    events: List[Dict[str, Any]]
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Собрать из потока ``(text, usage, error)``.

    Формат потока (замер 14.10): json-lines; у события текст лежит в
    ``parts[]`` (``{"type": "text", "text": ...}``), счётчики — в
    ``info.tokens``, отказ модели — в ``info.error.data.message``.
    """
    text_chunks: List[str] = []
    usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    for evt in events:
        parts = evt.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_chunks.append(str(part.get("text") or ""))
        info = evt.get("info")
        if isinstance(info, dict):
            tokens = info.get("tokens")
            if isinstance(tokens, dict):
                usage = tokens
            err = info.get("error")
            if isinstance(err, dict):
                data = err.get("data") or {}
                message = str(data.get("message") or err.get("name") or "unknown")
                code = data.get("statusCode")
                error = f"http_{code}: {message}" if code else message
    return "".join(text_chunks).strip(), usage, error


def _call_model(model_id: str, prompt: str, label: str) -> Dict[str, Any]:
    """Один вызов одной модели через serve. Никогда не бросает."""
    if not get_base_url():
        return {"ok": False, "reason": "no_base_url"}

    status, session = _post_json("/session", {"title": f"classify:{label}"}, get_timeout())
    if status == 0 or not isinstance(session, dict) or not session.get("id"):
        return {"ok": False, "reason": "network", "detail": session}
    session_id = str(session["id"])

    status, events = _post_json_lines(
        f"/session/{session_id}/message",
        {
            "model": {"providerID": "opencode", "modelID": model_id},
            "parts": [{"type": "text", "text": prompt}],
        },
    )
    if status == 0:
        return {"ok": False, "reason": "network", "detail": events[0] if events else None}

    text, usage, error = parse_stream(events)
    if error:
        # Нормализуем в http_<код>, когда поток его нёс: фолбэк по цепочке
        # решает именно по коду (403 geo/quota, 429, 5xx — следующая модель).
        reason = "model_error"
        if error.startswith("http_") and error.split(":", 1)[0][5:].isdigit():
            reason = error.split(":", 1)[0]
        return {"ok": False, "reason": reason, "detail": error, "usage": usage}
    if not text:
        return {"ok": False, "reason": "empty_response", "usage": usage}

    return {
        "ok": True,
        "content": _normalize_content(text),
        "model": f"opencode/{model_id}",
        "usage": _normalize_usage(usage),
    }


def _normalize_content(text: str) -> str:
    """Привести текст модели к контракту классификатора.

    Свободные модели Zen (замер 14.10, longcat) охотнее отдают топ-level
    массив вердиктов и оборачивают его в ```json — ``_extract_json`` головного
    классификатора ловит только ``{...}`` с ключом ``verdicts``. Нормализация
    живёт здесь, а не в классификаторе: чинить терпимость общего парсера под
    привычку одного движка — версия расходится молча (G334-класс).
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, list):
        return json.dumps({"verdicts": parsed}, ensure_ascii=False)
    return text


def _normalize_usage(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Счётчики serve → контракт DeepSeek-клиента (``total_tokens`` и пр.).

    Формат serve (замер 14.10): ``{"input": N, "output": M, "reasoning": K,
    "cache": {...}, "total": N+M}``. Классификатор суммирует
    ``usage.total_tokens`` по чанкам; без нормализации прогон копил бы нули и
    отчёт токенов врал бы (тот же класс, что молчащий ноль в P175).
    """
    if not isinstance(usage, dict):
        return None
    try:
        prompt = int(usage.get("input") or 0)
        completion = int(usage.get("output") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "total_tokens": prompt + completion,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }


def chat(
    *,
    user: str,
    system: Optional[str] = None,
    temperature: float = 0.2,  # noqa: ARG001 — свободные модели Zen температуру не принимают
    max_tokens: Optional[int] = None,  # noqa: ARG001 — лимиты на стороне serve
    json_object: bool = False,  # noqa: ARG001 — strict-JSON включается phrasing'ом промпта
    api_key: Optional[str] = None,  # noqa: ARG001 — ключ живёт в serve (auth.json)
    model: Optional[str] = None,
    timeout: Optional[float] = None,  # noqa: ARG001 — таймаут внутри _post_json_lines
    label: str = "opencode",
) -> Dict[str, Any]:
    """Вызов классификатора через харнесс OpenCode.

    Сигнатура и контракт ответа зеркалят ``modules/deepseek_client.chat`` —
    вызывающий код (headless) не различает движки. ``model``, если задан,
    встаёт во главу цепочки фолбэков; далее — ``OPENCODE_MODEL_CHAIN``.
    """
    if not engine_enabled():
        return {"ok": False, "reason": "engine_disabled"}
    if not (user or "").strip():
        return {"ok": False, "reason": "empty_prompt"}

    chain = get_model_chain()
    if model:
        chain = [model] + [m for m in chain if m != model]

    # Постулаты + посты одним сообщением: у serve нет ролевых parts, а
    # префикс-кэша бесплатного тарифа не гарантирует — цена склейки нулевая.
    prompt = f"{system.strip()}\n\n{user.strip()}" if (system or "").strip() else user.strip()

    reasons: List[str] = []
    for model_id in chain:
        result = _call_model(model_id, prompt, label)
        if result.get("ok"):
            return result
        reason = str(result.get("reason") or "?")
        detail = str(result.get("detail") or "")[:120]
        reasons.append(f"{model_id}:{reason}")
        logger.warning("opencode-engine: модель %s отказала (%s %s)", model_id, reason, detail)
        if reason not in _RETRYABLE_REASONS:
            # Отказ, который следующая модель не починит (модель промолчала
            # не по своей вине — всё ещё стоит попробовать: пустой ответ
            # бывает и от перегруза).
            break
    return {
        "ok": False,
        "reason": "engine_all_failed",
        "detail": "; ".join(reasons),
    }
