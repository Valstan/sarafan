#!/usr/bin/env python3
"""Проба выхода: снимается ли квота free-тира холодным стартом движка (P210).

Зачем этот скрипт. Замер 24.10: пять поворотов ВПН (DE, PL, NL, US, PL) **без**
перезапуска движка квоту ``Free usage limit reached`` не сняли. При этом на
машине владельца тот же анонимный клиент работает, и там помогают смена страны
ВПН и полная перезагрузка приложения. Отсюда единственная непроверенная
гипотеза: дело может быть не в отдельном адресе, а в паре «холодный старт
клиента + свежий выход».

Что делает один шаг (по умолчанию до 6 шагов, потом стоп):

1. ``systemctl stop opencode-web`` — гасим движок целиком;
2. ``/etc/neural/egress.d/10-nextgenvpn.sh`` — следующий сервер подписки
   **по порядку**; историю ведёт сам скрипт плеча
   (``/var/lib/neural/vpn/current.json``: индекс, имя, IP, страна, время);
3. ``systemctl start opencode-web`` — свежий клиент;
4. **один** короткий вызов модели через ``modules.opencode_engine.chat``.

Окружение проба собирает сама: запуск идёт из sudo-шелла, а юнит ``setka``
получает env из systemd, поэтому без чтения ``/etc/setka/setka.env`` шаг мерил
не квоту, а собственную конфигурацию. Именно так и вышло 25.10: в шелле не было
``CLASSIFIER_ENGINE=opencode``, ``chat`` вернул ``engine_disabled``, и проба
отчиталась «движок выключен» вместо ответа вендора. Теперь ключи движка
подтягиваются из env-файла приложения (узкий allowlist, значения не печатаются),
а при его отсутствии движок включается по умолчанию — мерить нечего, если он
заведомо выключен.

Никакого таймера и автоповтора здесь нет и не появится: это диагностика
развилки P210 («другой выход» / «подписка Go»), а не обход лимита вендора.
Каждый шаг тратит один вызов модели, поэтому потолок шагов обязателен, а
подряд идущие пробы разделены паузой.

Почему движок гасится целиком: гипотеза владельца — лимит держится не только
адресом, но и состоянием клиента. Поэтому внутренний автоповорот движка
(``NEURAL_EGRESS_ROTATE_CMD``) на время пробы отключён: выход меняет только
этот скрипт, иначе шаг перестаёт быть наблюдаемым (клиент молча повернёт
внутри своего вызова).

Что читать в результате: ``exit`` (адрес и страна каждого шага) и ``reason``
(``ok`` / ``quota_exhausted`` / ``network_gate`` / ...). Если на шести разных
адресах шесть раз ``quota_exhausted`` — гипотеза «адрес» на нашем провайдере не
подтверждается, и дальше остаётся подписка, а не перебор.

Примеры::

    python scripts/neural_quota_probe.py --dry-run
    python scripts/neural_quota_probe.py --max-hops 6
    python scripts/neural_quota_probe.py --direct --max-hops 1
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENGINE_UNIT = "opencode-web"
EGRESS_SCRIPT = "/etc/neural/egress.d/10-nextgenvpn.sh"
ANON_ENV = "/etc/opencode-web/anon.env"
APP_ENV = "/etc/setka/setka.env"
# Ключи приложения, которые нужны пробе. Allowlist узкий и по имени, а не
# «весь файл»: env-файл приложения содержит секреты, и тащить их в окружение
# диагностического скрипта незачем.
APP_ENV_NAMES = ("CLASSIFIER_ENGINE",)
APP_ENV_PREFIXES = ("OPENCODE_", "NEURAL_", "ENGINE_BREAKER_")
DEFAULT_BASE_URL = "http://127.0.0.1:49374"
SOCKS = "127.0.0.1:10808"
PROBE_PROMPT = "Ответь одним словом: работает?"
PROBE_TIMEOUT = 60.0
PORT_WAIT = 45.0
LOG_PATH = "/var/lib/neural/quota-probe.jsonl"
LOCK_PATH = "/run/neural-quota-probe.lock"

# Отказы вендора, на которых имеет смысл пробовать другой выход. Остальные
# причины (`engine_unavailable`, `empty_response`, `network`) — локальные:
# смена адреса их не лечит, и шаг надо прекращать, а не жечь оставшиеся.
VENDOR_REASONS = {"quota_exhausted", "http_429", "network_gate"}


# --- чистая часть (тестируется без бокса) -----------------------------------


def parse_env_file(text: str) -> Dict[str, str]:
    """KEY=VALUE из env-файла юнита; комментарии и мусор игнорируются."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip().strip('"').strip("'")
    return out


def pick_password(env_file_values: Dict[str, str]) -> str:
    """Пароль serve: в anon.env он лежит как OPENCODE_PASSWORD (P205)."""
    return (
        env_file_values.get("OPENCODE_SERVER_PASSWORD")
        or env_file_values.get("OPENCODE_PASSWORD")
        or ""
    )


def wanted_env_key(key: str) -> bool:
    """Нужен ли ключ приложения пробе (узкий allowlist, см. APP_ENV_*)."""
    return key in APP_ENV_NAMES or key.startswith(APP_ENV_PREFIXES)


def load_app_env(path: Optional[str] = None) -> Dict[str, str]:
    """Подтянуть ключи движка из env-файла приложения, не перетирая заданные.

    Возвращает то, что нашлось в файле (для отчёта), но **не печатает значения**:
    среди ключей есть пароль serve. Пустое значение не подставляется — иначе
    пустая строка в файле перебила бы дефолт.
    """
    try:
        text = Path(path or APP_ENV).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    found: Dict[str, str] = {}
    for key, value in parse_env_file(text).items():
        if not wanted_env_key(key):
            continue
        found[key] = value
        if value and not (os.getenv(key) or "").strip():
            os.environ[key] = value
    return found


def decide(result: Dict[str, Any]) -> str:
    """Что делать после шага: success | rotate | stop."""
    if result.get("ok"):
        return "success"
    reason = str(result.get("reason") or "?")
    return "rotate" if reason in VENDOR_REASONS else "stop"


def normalize_reason(result: Dict[str, Any]) -> str:
    """Причина отказа в терминах пробы, а не в терминах движка.

    Движок склеивает отказы моделей цепочки в ``engine_all_failed``, а класс
    отказа кладёт в ``detail`` строкой ``<модель>:<причина>`` и в флаг
    ``quota``. Пробе нужен именно класс: 25.10 она остановилась на первом шаге,
    увидев агрегат при настоящей причине ``quota_exhausted``, и гипотеза
    «холодный старт + свежий выход» осталась непроверенной.
    """
    if result.get("ok"):
        return "ok"
    reason = str(result.get("reason") or "?")
    if reason != "engine_all_failed":
        return reason
    if result.get("quota"):
        return "quota_exhausted"
    detail = str(result.get("detail") or "")
    for candidate in ("quota_exhausted", "http_429", "network_gate"):
        if candidate in detail:
            return candidate
    return reason


# --- шаги на боксе ----------------------------------------------------------


def sh(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001 — отчёт, не падение
        return 127, "", str(exc)


def prepare_env() -> None:
    """Собрать окружение пробы: env приложения + адрес и пароль serve."""
    load_app_env()
    if not (os.getenv("OPENCODE_BASE_URL") or "").strip():
        os.environ["OPENCODE_BASE_URL"] = DEFAULT_BASE_URL
    if not (os.getenv("OPENCODE_SERVER_PASSWORD") or "").strip():
        try:
            text = Path(ANON_ENV).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        password = pick_password(parse_env_file(text))
        if password:
            os.environ["OPENCODE_SERVER_PASSWORD"] = password
    # Проба мерит движок opencode. Если env не сказал иного — включаем его:
    # иначе ``chat`` вернёт ``engine_disabled`` и шаг не измерит ничего.
    if not (os.getenv("CLASSIFIER_ENGINE") or "").strip():
        os.environ["CLASSIFIER_ENGINE"] = "opencode"
    # Выход на время пробы меняет только этот скрипт (см. докстринг).
    os.environ["NEURAL_EGRESS_ROTATE_CMD"] = ""


def engine_ready() -> bool:
    """Включён ли движок opencode с точки зрения классификатора."""
    from config.opencode import engine_enabled

    return bool(engine_enabled())


def engine(action: str) -> Tuple[bool, str]:
    rc, out, err = sh(["systemctl", action, ENGINE_UNIT], timeout=60)
    return rc == 0, (err or out)[:200]


def reset_failed() -> Tuple[bool, str]:
    """Снять с юнита состояние ``failed`` после штатной остановки.

    Причина заведена в самом юните (``SuccessExitStatus=130``), но проба может
    стартовать на боксе, где строка ещё не выкачена: тогда после нашего
    ``stop`` юнит остаётся ``failed``, и это читается как авария движка.
    ``reset-failed`` возвращает честное ``inactive``.
    """
    rc, out, err = sh(["systemctl", "reset-failed", ENGINE_UNIT], timeout=30)
    return rc == 0, (err or out)[:200]


def rotate_egress() -> Tuple[bool, str]:
    cmd = [EGRESS_SCRIPT] if os.geteuid() == 0 else ["sudo", "-n", EGRESS_SCRIPT]
    rc, out, err = sh(cmd, timeout=240)
    return rc == 0, (out or err)[:200]


def exit_address(direct: bool) -> Optional[str]:
    """Выход плеча: «IP|страна». None — плечо не ответило."""
    base = ["curl", "-s", "--max-time", "12"]
    if not direct:
        base += ["--socks5-hostname", SOCKS]
    rc, ip, _ = sh(base + ["https://api.ipify.org"], timeout=20)
    ip = (ip or "").strip()
    if rc != 0 or not ip:
        return None
    rc, country, _ = sh(base + ["https://ipinfo.io/country"], timeout=15)
    country = (country or "").strip()[:2]
    return f"{ip}|{country}" if country else ip


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def wait_port(port: int, opened: bool, timeout: float = PORT_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port) is opened:
            return True
        time.sleep(0.5)
    return False


def probe_once() -> Dict[str, Any]:
    """Один вызов модели тем же клиентом, что у классификатора."""
    from modules.opencode_engine import chat  # локально: импорт тянет config

    result = chat(user=PROBE_PROMPT, label="quota-probe", timeout=PROBE_TIMEOUT)
    return {
        "ok": bool(result.get("ok")),
        "reason": normalize_reason(result),
        "detail": str(result.get("detail") or "")[:160],
        "reply": str(result.get("content") or "")[:120],
    }


@dataclass
class Steps:
    """Шаги цикла: подменяются в тестах, на боксе — реальные функции."""

    engine_stop: Optional[Callable[[], Tuple[bool, str]]] = None
    engine_start: Optional[Callable[[], Tuple[bool, str]]] = None
    engine_reset: Optional[Callable[[], Tuple[bool, str]]] = None
    rotate: Optional[Callable[[], Tuple[bool, str]]] = None
    exit_addr: Optional[Callable[[], Optional[str]]] = None
    probe: Optional[Callable[[], Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        if self.engine_stop is None:
            self.engine_stop = lambda: engine("stop")
        if self.engine_start is None:
            self.engine_start = lambda: engine("start")
        if self.engine_reset is None:
            self.engine_reset = reset_failed
        if self.rotate is None:
            self.rotate = rotate_egress
        if self.exit_addr is None:
            self.exit_addr = lambda: exit_address(False)
        if self.probe is None:
            self.probe = probe_once


def run_cycle(
    steps: Steps,
    *,
    max_hops: int = 6,
    pause: float = 20.0,
    direct: bool = False,
    leave_running: bool = False,
    port: int = 49374,
    write_log: Optional[Callable[[Dict[str, Any]], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    wait: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    """Ограниченный цикл: холодный старт + свежий выход на каждом шаге."""
    wait_fn = wait or (lambda p, opened, timeout=PORT_WAIT: wait_port(p, opened, timeout))
    hops: List[Dict[str, Any]] = []
    success = False
    stop_reason = ""

    def record(entry: Dict[str, Any]) -> None:
        hops.append(entry)
        if write_log is not None:
            write_log(entry)

    for hop in range(1, max_hops + 1):
        if hop > 1:
            sleep(pause)

        if not direct:
            rotated, rotate_detail = steps.rotate()
            if not rotated:
                # Плечо не дало выхода — это мёртвый ВПН, а не отказ вендора:
                # следующий шаг не приблизит ответ, но и не навредит сильнее.
                record({"hop": hop, "step": "rotate", "ok": False, "detail": rotate_detail})
                stop_reason = "egress_rotate_failed"
                break
        address = None if direct else steps.exit_addr()

        stopped, stop_detail = steps.engine_stop()
        if not stopped:
            record({"hop": hop, "step": "stop", "ok": False, "detail": stop_detail})
            stop_reason = "engine_stop_failed"
            break
        wait_fn(port, False)

        started, start_detail = steps.engine_start()
        if not started:
            record({"hop": hop, "step": "start", "ok": False, "detail": start_detail})
            stop_reason = "engine_start_failed"
            break
        if not wait_fn(port, True):
            record({"hop": hop, "step": "start", "ok": False, "detail": "порт не открылся"})
            stop_reason = "engine_port_closed"
            break

        result = steps.probe()
        verdict = decide(result)
        record(
            {
                "hop": hop,
                "step": "probe",
                "exit": address,
                "verdict": verdict,
                **result,
            }
        )
        if verdict == "success":
            success = True
            break
        if verdict == "stop":
            stop_reason = str(result.get("reason") or "probe_failed")
            break

    if not success and not stop_reason:
        stop_reason = "hops_exhausted"
    if not leave_running:
        steps.engine_stop()
        steps.engine_reset()

    return {
        "success": success,
        "stop_reason": stop_reason,
        "hops": hops,
        "engine_left_running": bool(leave_running),
    }


# --- обвязка ---------------------------------------------------------------


def append_jsonl(path: str) -> Callable[[Dict[str, Any]], None]:
    def write(entry: Dict[str, Any]) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": int(time.time()), **entry}, ensure_ascii=False) + "\n")
        except OSError as exc:  # журнал не должен ронять пробу
            print(f"журнал недоступен: {exc}", file=sys.stderr)

    return write


def acquire_lock() -> bool:
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True  # /run недоступен — не повод отказывать в пробе
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock() -> None:
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-hops", type=int, default=6, help="потолок шагов (по умолчанию 6)")
    parser.add_argument("--pause", type=float, default=20.0, help="пауза между шагами, с")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="не трогать ВПН: проба с прямого выхода бокса (один шаг)",
    )
    parser.add_argument(
        "--leave-running",
        action="store_true",
        help="не гасить движок в конце (по умолчанию гасим — как просил владелец)",
    )
    parser.add_argument("--dry-run", action="store_true", help="показать план, ничего не делать")
    parser.add_argument("--log", default=LOG_PATH, help="журнал шагов (jsonl)")
    args = parser.parse_args(argv)

    max_hops = 1 if args.direct else max(1, args.max_hops)
    plan = {
        "engine_unit": ENGINE_UNIT,
        "egress_script": EGRESS_SCRIPT,
        "direct": bool(args.direct),
        "max_hops": max_hops,
        "pause": args.pause,
        "leave_running": bool(args.leave_running),
        "log": args.log,
    }
    if args.dry_run:
        print(json.dumps({"plan": plan}, ensure_ascii=False, indent=2))
        return 0

    if os.geteuid() != 0:
        print("нужен root (systemctl + скрипт плеча) или запуск через sudo", file=sys.stderr)
        return 2
    if not acquire_lock():
        print(f"уже идёт другая проба (замок {LOCK_PATH})", file=sys.stderr)
        return 3

    prepare_env()
    if not engine_ready():
        # Мерить нечего: шаг вернул бы engine_disabled, потратив поворот ВПН.
        print(
            "движок opencode выключен (CLASSIFIER_ENGINE != opencode) — "
            "проба измерила бы свою конфигурацию, а не квоту",
            file=sys.stderr,
        )
        release_lock()
        return 2
    steps = Steps()
    if args.direct:
        steps.exit_addr = lambda: exit_address(True)

    started_at = time.time()
    try:
        result = run_cycle(
            steps,
            max_hops=max_hops,
            pause=max(0.0, args.pause),
            direct=bool(args.direct),
            leave_running=bool(args.leave_running),
            write_log=append_jsonl(args.log),
        )
    finally:
        release_lock()

    result.update({"started_at": int(started_at), "elapsed_s": round(time.time() - started_at, 1)})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
