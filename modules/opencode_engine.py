"""Движок классификации через харнесс OpenCode (v2, единый serve; 17.10).

Зачем. DeepSeek-контур мёртв с 22.09 (402, P177), и сеть публикует без
нейро-фильтра. Владелец поставил задачу: вердикты выносит харнесс OpenCode на
том же VPS. Здесь — мост между классификатором (контракт
``modules/deepseek_client.chat``: ``{ok, content, model, usage}`` либо
``{ok: False, reason, detail}``) и headless-сервером ``opencode serve`` v2.

Один сервер на два дела (решение владельца 17.10). На боксе живёт ровно один
``serve`` — systemd-юнит ``opencode-web`` (v2.0.32, 127.0.0.1:49374, всегда
включён): он же веб-кабинет владельца для кодинга, он же движок фильтрации.
Движок НЕ поднимает собственный serve: ``OPENCODE_NO_SPAWN=1`` в проде, иначе
свой экземпляр встал бы на тот же порт и подрался с systemd за него. Прежняя
oneshot-схема (``setka-opencode.service``, v1.19) выведена из эксплуатации.

Контракт v2 (замер 17.10 — ``/openapi.json`` сервера плюс пробы):

1. ``POST /api/session`` — сессия сразу с моделью
   (``{"model": {"id", "providerID"}}``) и запретами инструментов (текст
   постов — недоверенный вход; классификатору инструменты не нужны);
2. ``POST /api/session/{id}/prompt`` — сообщение в очередь (асинхронно);
3. ``POST /api/experimental/session/{id}/wait`` — дождаться тишины сессии;
4. ``GET /api/session/{id}/message`` — собрать ответ: текст в
   ``content[type=text]``, счётчики в ``tokens``, отказ — ``idle.outcome``
   != ``succeeded`` с ``error`` (``provider.no-route`` и пр.); сессия
   удаляется за собой (иначе пул и данные сервера растут молча).

Архитектура намеренно тонкая:

- **Точка переключения одна** — ``modules/classifier/headless.classify_chunk``
  зовёт этот ``chat`` при ``CLASSIFIER_ENGINE=opencode``; DeepSeek остаётся
  дефолтом, поэтому откат — одна env-переменная, без деплоя кода.
- **Цепочка моделей с фолбэком** (``OPENCODE_MODEL_CHAIN``). Бесплатный тариф
  Zen ротирует модели; первая модель, ответившая текстом, закрывает вызов.
  Отказ одной модели не роняет батч: следующая берёт на себя.
- **Никогда не бросает** — тот же контракт отказов, что у DeepSeek-клиента:
  ``network`` | ``http_<код>`` | ``model_error`` | ``empty_response`` |
  ``engine_all_failed``. Классификатор fail-open: нет вердикта — пост идёт по
  обычным фильтрам.

Почему HTTP-сервер, а не CLI `opencode run`: на этом боксе `run` стабильно
висит после `init` (проверено 14.10, ~170 c, ноль байт вывода), а `serve`
поднимается и отвечает за секунду.
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
    no_spawn,
)

logger = logging.getLogger(__name__)

# Запреты инструментов для сессий классификатора: текст постов — недоверенный
# вход, а вердикты — чистый текст, инструменты движку не нужны вовсе (замер
# 17.10: без запрета модель по просьбе из поста реально запускает shell).
# ``execute`` — вторая дверь: с запрещённым shell модель пробует JS-песочницу
# (проба 17.10: внутри неё командной двери нет — «Unknown tool», маркер не
# создан); закрыта тем же списком.
# ``read`` — особый случай: запрет на ``*`` ломает сам вызов free-tier (пробы
# 17–18.10: модель отдаёт provider.auth «free tier can only be used from
# within OpenCode»), а узкие запреты работают и вызов не ломают. Поэтому две
# чувствительные двери закрыты точечно: пароль веб-сервера и ssh-ключи (проба
# 18.10: чтение anon.env → permission.rejected; /etc/hostname как контроль —
# читается; /etc/setka/* держит политика конфига).
CLASSIFY_SESSION_PERMISSIONS: List[Dict[str, str]] = [
    {"action": action, "resource": "*", "effect": "deny"}
    for action in (
        "shell",
        "edit",
        "webfetch",
        "websearch",
        "subagent",
        "browser",
        "grep",
        "glob",
        "todowrite",
        "skill",
        "question",
        "execute",
    )
] + [
    {"action": "read", "resource": "*anon.env*", "effect": "deny"},
    {"action": "read", "resource": "*/.ssh/*", "effect": "deny"},
]

# Фора живому юниту на рестарт в режиме запрета спавна (OPENCODE_NO_SPAWN):
# короткий цикл перезапуска systemd (RestartSec=5) укладывается с запасом,
# а затянувшийся простой не превращает волну в ожидание (fail-open).
NO_SPAWN_WAIT_SEC = 30.0

# Причины, после которых следующая модель цепочки ничем не поможет: это
# отказ контура (нет адреса serve), а не свойство конкретной модели.
_FATAL_REASONS = ("no_base_url",)

# Признаки гейта free-тира по IP (замер 20.10: «free models aren't available
# from your current network… we've detected abusive traffic from this IP»).
# Гейт per-IP, поэтому лечится сменой выхода ВПН, а не сменой модели: все
# модели каталога закрыты одним и тем же адресом.
_NETWORK_GATE_MARKERS = (
    "aren't available from your current network",
    "are not available from your current network",
    "abusive traffic from this ip",
    "abusive traffic",
)

# Файл-метка паузы между поворотами выхода: воркер многопроцессный, поэтому
# метка на диске, а не в памяти процесса.
_EGRESS_ROTATE_STAMP = "setka_egress_rotate.stamp"


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
    """start/stop oneshot-юнита харнесса (legacy-путь ручных прогонов).

    Popen из prefork-воркера Celery нестабилен (ребёнок умирает молча —
    боевой замер 14.10: 72/72 чанков Connection refused при живом
    ручном serve). В единой схеме (17.10) этот путь продом не используется:
    сервер держит systemd-юнит ``opencode-web``, а движок прикрепляется к
    нему (``OPENCODE_NO_SPAWN``). Оставлен для отладочных прогонов на других
    боксах, где oneshot-юнит ещё живёт. Возвращает False, если команда не
    прошла.
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
    """Serve, с которым работает прогон: чужой всегда лучше своего.

    Единый v2-сервер (``opencode-web``) держит systemd постоянно; движок к
    нему только прикрепляется. Если сервера нет, а ``OPENCODE_NO_SPAWN=1``
    (прод) — это отказ контура, а не повод поднять второй serve на тот же
    порт: даём юниту фору на рестарт (``NO_SPAWN_WAIT_SEC``) и сдаёмся тихо
    (fail-open волны). Без запрета (ручные прогоны, отладка) — прежние пути:
    systemd-oneshot (``OPENCODE_SYSTEMD_RUN=1``) или ребёнок Popen, который
    тушится в finally. Кто поднял — тот и тушит: осиротевший процесс не
    остаётся.

    Yield: True — serve на этом порту ответил (можно звать модели).
    """
    if server_alive():
        yield True
        return

    host, port = "127.0.0.1", _port_of(get_base_url())

    if no_spawn():
        alive = _wait_port(host, port, timeout=NO_SPAWN_WAIT_SEC)
        if not alive:
            logger.warning(
                "opencode-engine: serve %s:%s не ответил, спавн запрещён "
                "(OPENCODE_NO_SPAWN) — чанки волны идут без нейро-фильтра",
                host,
                port,
            )
        yield alive
        return

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
    # Приватный TMPDIR на запуск (P197): opencode v1 распаковывает в temp
    # нативный модуль `.bcdf*.so` (~5.5 МБ) и не убирает его за собой — в общем
    # /tmp это копится по файлу на запуск (инцидент 15.10: 552 шт. ≈ 2.9 ГБ,
    # диск 100 %). Свой каталог уносит мусор этого запуска вместе с собой
    # (rmtree в finally); если opencode пишет мимо TMPDIR — подчистит свипер
    # sweep_opencode_spill.
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


def _auth_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    password = get_server_password()
    if password:
        token = base64.b64encode(f"{get_server_username()}:{password}".encode("utf-8")).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {token}"
    return headers


def _post_json(path: str, body: Optional[Dict[str, Any]], timeout: float) -> Tuple[int, Any]:
    """POST на serve (``body=None`` — POST без тела, endpoint ``wait``).

    Сеть/протокол → ``(0, {"error": ...})``, как у DeepSeek.
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(
        f"{get_base_url()}{path}",
        data=data,
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


def _get_json(path: str, timeout: float) -> Tuple[int, Any]:
    """GET с serve. Сеть/протокол → ``(0, {"error": ...})``, как у DeepSeek."""
    req = urllib.request.Request(f"{get_base_url()}{path}", method="GET", headers=_auth_headers())
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


def _close_session(session_id: str) -> None:
    """Убрать сессию за собой: без этого пул и данные сервера растут молча.

    Каждый чанк волны — новая сессия; брошенные сессии у сервера v2 оседают
    в его каталоге данных (тот же класс утечки, что /tmp-мусор v1 в P197).
    Уборка best-effort: её сбой исход вызова не меняет.
    """
    try:
        req = urllib.request.Request(
            f"{get_base_url()}/api/session/{session_id}",
            method="DELETE",
            headers=_auth_headers(),
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:  # noqa: BLE001 — уборка не влияет на исход вызова
        logger.debug("opencode-engine: сессия %s не удалена за собой", session_id, exc_info=True)


def _error_text(err: Any) -> Optional[str]:
    """Отказ v2 (``{type, message, status?}``) → строка для лога и причины."""
    if not isinstance(err, dict):
        return None
    message = str(err.get("message") or err.get("type") or "").strip()
    code = err.get("status") or err.get("statusCode")
    if message and code:
        return f"http_{code}: {message}"
    return message or None


def _reason_from_error(error: str) -> str:
    """Строка отказа → причина контракта.

    Код из тела (``http_429`` и пр.) сохраняем как есть; гейт free-тира по IP
    узнаём по тексту (``network_gate``) — у него нет HTTP-кода, и от смены
    модели он не проходит; отказ без кода (``provider.no-route``) —
    ``model_error``.
    """
    low = (error or "").lower()
    if any(marker in low for marker in _NETWORK_GATE_MARKERS):
        return "network_gate"
    head = error.split(":", 1)[0]
    if head.startswith("http_") and head[5:].isdigit():
        return head
    return "model_error"


def parse_messages(
    messages: List[Dict[str, Any]]
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Собрать из ленты сессии ``(text, usage, error)``.

    Лента v2 (замер 17.10) приходит новейшим вперёд (``order=desc``); у ответа
    текст лежит в ``content[]`` (``{"type": "text"}``; части ``reasoning``
    пропускаем), счётчики — в ``tokens``; отказ виден как ``idle.outcome``
    != ``succeeded`` с ``error`` (``provider.no-route``, ``provider.auth``
    и пр.). Сбой важнее текста: при затопленном ответе вердикты не считать.
    """
    text_chunks: List[str] = []
    usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    assistant_seen = False
    idle_seen = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        if kind == "assistant" and not assistant_seen:
            # Новейший ответ — первый в ленте (запрос с order=desc).
            assistant_seen = True
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_chunks.append(str(part.get("text") or ""))
            tokens = msg.get("tokens")
            if isinstance(tokens, dict):
                usage = tokens
        elif kind == "idle" and not idle_seen:
            idle_seen = True
            outcome = str(msg.get("outcome") or "")
            if outcome and outcome != "succeeded":
                error = _error_text(msg.get("error")) or outcome
    return "".join(text_chunks).strip(), usage, error


def _rotate_egress(detail: str = "") -> bool:
    """Сменить выход ВПН после гейта free-тира по IP. ``True`` — повернули.

    Пауза (``NEURAL_EGRESS_ROTATE_COOLDOWN``) обязательна: без неё волна из
    десятков регионов крутила бы серверы подряд и сожгла все адреса подписки за
    один час. Метка лежит в temp, потому что воркер многопроцессный.
    """
    from config.opencode import get_egress_rotate_cmd, get_egress_rotate_cooldown

    cmd = get_egress_rotate_cmd()
    if not cmd:
        return False

    stamp = os.path.join(tempfile.gettempdir(), _EGRESS_ROTATE_STAMP)
    now = time.time()
    try:
        last = os.path.getmtime(stamp)
    except OSError:
        last = 0.0
    cooldown = get_egress_rotate_cooldown()
    if last and (now - last) < cooldown:
        logger.warning(
            "opencode-engine: гейт free-тира по IP, но поворот выхода на паузе" " (%.0f с из %.0f)",
            now - last,
            cooldown,
        )
        return False

    try:
        proc = subprocess.run(  # noqa: S603 — команда из env бокса, не из ввода
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or "").strip().splitlines()
        tail = out[-1] if out else (proc.stderr or "").strip()[:160]
        logger.warning(
            "opencode-engine: выход ВПН повёрнут после гейта (%s) rc=%s %s",
            detail[:120],
            proc.returncode,
            tail,
        )
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("opencode-engine: поворот выхода не удался: %s", e)
        return False

    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(str(int(now)))
    except OSError:
        pass
    return ok


def _call_model(model_id: str, prompt: str, label: str) -> Dict[str, Any]:
    """Один вызов одной модели через serve v2. Никогда не бросает."""
    if not get_base_url():
        return {"ok": False, "reason": "no_base_url"}

    timeout = get_timeout()

    # Сессия сразу с моделью и запретами инструментов: у v2 модель — свойство
    # сессии, а не сообщения (у prompt поля model нет; строка/объект в теле
    # промпта молча игнорируются — пробы 17.10).
    status, session = _post_json(
        "/api/session",
        {
            "title": f"classify:{label}",
            "model": {"id": model_id, "providerID": "opencode"},
            "permissions": CLASSIFY_SESSION_PERMISSIONS,
        },
        min(timeout, 30.0),
    )
    if status == 0:
        return {"ok": False, "reason": "network", "detail": session}
    if status >= 400:
        return {"ok": False, "reason": f"http_{status}", "detail": session}
    session_id = str(((session or {}).get("data") or {}).get("id") or "")
    if not session_id:
        return {"ok": False, "reason": "network", "detail": session}

    status, prompt_resp = _post_json(
        f"/api/session/{session_id}/prompt", {"text": prompt}, min(timeout, 30.0)
    )
    if status == 0:
        _close_session(session_id)
        return {"ok": False, "reason": "network", "detail": prompt_resp}
    if status >= 400:
        _close_session(session_id)
        return {"ok": False, "reason": f"http_{status}", "detail": prompt_resp}

    # wait блокируется до тишины сессии; потолок ожидания — общий таймаут
    # вызова модели (OPENCODE_TIMEOUT). Сбой на нём — отказ этого захода:
    # следующая модель попробует свежей сессией.
    status, wait_resp = _post_json(f"/api/experimental/session/{session_id}/wait", None, timeout)
    if status == 0 or status >= 400:
        _close_session(session_id)
        reason = "network" if status == 0 else f"http_{status}"
        return {"ok": False, "reason": reason, "detail": wait_resp}

    status, messages = _get_json(
        f"/api/session/{session_id}/message?limit=20&order=desc", min(timeout, 30.0)
    )
    _close_session(session_id)
    if status == 0:
        return {"ok": False, "reason": "network", "detail": messages}
    if status >= 400:
        return {"ok": False, "reason": f"http_{status}", "detail": messages}

    raw_messages = (messages or {}).get("data") if isinstance(messages, dict) else None
    text, usage, error = parse_messages(raw_messages if isinstance(raw_messages, list) else [])
    if error:
        return {"ok": False, "reason": _reason_from_error(error), "detail": error, "usage": usage}
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

    Формат serve (замер 14.10, у v2 тот же): ``{"input": N, "output": M,
    "reasoning": K, "cache": {...}, "total": N+M}``. Классификатор суммирует
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
    api_key: Optional[str] = None,  # noqa: ARG001 — ключ живёт в serve
    model: Optional[str] = None,
    timeout: Optional[float] = None,  # noqa: ARG001 — таймаут внутри _post_json/_get_json
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

    # Постулаты + посты одним сообщением: у serve v2 ролевых частей нет,
    # а префикс-кэша бесплатного тарифа не гарантирует — цена склейки нулевая.
    prompt = f"{system.strip()}\n\n{user.strip()}" if (system or "").strip() else user.strip()

    reasons: List[str] = []
    gate_detail = ""
    # Две попытки: первая — как есть; если вся цепочка легла на гейт free-тира
    # по IP — меняем выход ВПН (если бокс это разрешил) и повторяем цепочку.
    for attempt in (1, 2):
        reasons = []
        gate_detail = ""
        for model_id in chain:
            result = _call_model(model_id, prompt, label)
            if result.get("ok"):
                return result
            reason = str(result.get("reason") or "?")
            detail = str(result.get("detail") or "")[:120]
            reasons.append(f"{model_id}:{reason}")
            if reason == "network_gate" and not gate_detail:
                gate_detail = detail
            logger.warning("opencode-engine: модель %s отказала (%s %s)", model_id, reason, detail)
            if reason in _FATAL_REASONS:
                # Отказ контура (нет адреса serve) — следующая модель не поможет.
                break
        if attempt == 1 and gate_detail and _rotate_egress(gate_detail):
            logger.warning("opencode-engine: повторяю цепочку после смены выхода ВПН")
            continue
        break
    return {
        "ok": False,
        "reason": "engine_all_failed",
        "detail": "; ".join(reasons),
    }
