#!/usr/bin/env python3
"""Проверка продления входа MiniMax: продлился ли вход **того же** аккаунта.

**Зачем этот скрипт.** 22.11 отказ «Sign in to MiniMax» съел 118 аккаунтов за
сутки: движок читал сломанную сессию CLI как «аккаунт мёртв» и брал следующий.
Починка (PR #1207, #1209) добавила **продление того же** аккаунта через ручку
Кармана ``POST /api/minimax/renew`` и разделила два разных отказа: «панель не
прочиталась» (протух наш вход) и «очков нет» (ресурс исчерпан). Но проверять
это на проде было нечем: ответ лежит в **ротированном логе воркера** и в
**токен-сторе рабочей области**, и собирать его приходилось цепочкой грепов по
SSH. Класс отказа без симптома — состояние в двух местах сразу.

**Что делает.** Читает (ничего не пишет и не меняет):

* профиль рабочей области (``profile.json``) — какой аккаунт сейчас в работе;
* токен-стор и состояние сессии CLI (``auth.json`` / ``auth-state.json``) —
  **срок access-токена** и состояние сессии (``authenticated`` / ``anonymous``);
* журнал расхода (``logs/minimax_usage.jsonl``) — были ли вызовы **после**
  истечения токена;
* лог воркера (``logs/celery-worker.log`` и его ротации) — **продления**
  («вход аккаунта N продлён»), **отказы продления** («Карман не продлил
  аккаунт») и **новые выдачи** («Карман выбрал аккаунт»).

**Что не делает.** Ни одного запроса к Карману и ни одной записи: access-токен
не печатается (читается только срок), учёт в Кармане не трогается. Скрипт
**не требует env-файла приложения** — только права на чтение рабочей области и
логов, поэтому его можно запускать из обычного SSH-шелла.

**Как читать вердикт.**

* ``продление сработало`` — вход продлён, тот же аккаунт, новых выдач нет;
* ``Карман отклонил продление`` — ручка ``renew`` ответила отказом (обычно
  409: аккаунт выдан под другим журнальным проектом), движок взял другой;
* ``вход не продлили`` — вызовы после истечения были, а следов продления нет;
* ``не проверено`` — после истечения токена вызовов ещё не было: продление
  ещё не могло сработать, ждать следующего окна трафика.

Запуск на проде::

    cd ~/SETKA && ./venv/bin/python scripts/minimax_renew_check.py

    --json    вывод в JSON (для автоматики и панелей)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent

#: Клиент рантайма MiniMax Code — он же имя папки в токен-сторе.
CLIENT_ID = "mcode-public"
#: Файл состояния сессии CLI рядом с токен-стором.
SESSION_STATE_NAME = "auth-state.json"
#: Метаданные профиля (почта и id аккаунта в Кармане).
PROFILE_NAME = "profile.json"
#: Журнал расхода движка.
USAGE_LOG = "logs/minimax_usage.jsonl"
#: Логи воркера: текущий и ротации (``.1``, ``.2.gz`` …).
WORKER_LOG = "logs/celery-worker.log"
#: Каталог пула аккаунтов по умолчанию (как в движке).
DEFAULT_ACCOUNTS_DIR = "/opt/minimax-accounts"
#: Рабочая область по умолчанию.
DEFAULT_WORK_AREA = "work"
#: Время жизни access-токена, если срок не прочитан (движок считает 8 ч).
FALLBACK_TTL_SECONDS = 8 * 3600

#: Метки в логе воркера. Строки движка русские, поэтому и метки русские —
#: метка должна совпадать с тем, что реально пишет движок, иначе проверка
#: молча покажет «следов нет».
MARK_RENEWED = "вход аккаунта"
MARK_RENEWED_TAIL = "продлён"
MARK_RENEW_FAILED = "Карман не продлил аккаунт"
MARK_NEW_ACCOUNT = "Карман выбрал аккаунт"
MARK_ROTATED = "больше не работает"
#: Панель не прочиталась — на протухшем access-токене вендор отвечает 401.
#: Это **след** того, что токен истёк, даже если продление не вызывалось.
MARK_PANEL_UNAVAILABLE = "недоступна"

#: Начало строки лога Celery: ``[2026-11-23 10:12:00,006: …]``.
LOG_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


# --- чистая часть (тестируется без бокса) -----------------------------------


def parse_log_timestamp(line: str) -> Optional[float]:
    """Время строки лога воркера в epoch-секундах (локальное время бокса).

    ``None``, если строка не из лога Celery или время не разобралось.
    """
    match = LOG_TS_RE.match(line or "")
    if not match:
        return None
    try:
        moment = datetime.strptime(
            "%s.%s" % (match.group(1), match.group(2)), "%Y-%m-%d %H:%M:%S.%f"
        )
    except ValueError:
        return None
    return moment.timestamp()


def read_json(path: Path) -> Dict[str, Any]:
    """JSON-файл как словарь (пустой словарь, если файла нет или он битый)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def token_state(work_root: Path) -> Dict[str, Any]:
    """Срок access-токена и состояние сессии CLI рабочей области.

    **Токен не читается.** Из токен-стора берётся только ``expiresAtMs`` —
    печатать секрет нельзя, а для проверки продления срока достаточно.
    """
    auth_dir = Path(work_root) / "data" / "auth" / "prod" / "en" / CLIENT_ID
    store = read_json(auth_dir / "auth.json")
    session = read_json(auth_dir / SESSION_STATE_NAME)

    expires_at_ms = 0
    generation = 0
    for record in (store.get("records") or {}).values():
        if not isinstance(record, dict):
            continue
        try:
            expires_at_ms = max(expires_at_ms, int(record.get("expiresAtMs") or 0))
        except (TypeError, ValueError):
            pass
        try:
            generation = max(generation, int(record.get("generation") or 0))
        except (TypeError, ValueError):
            pass
    if not generation:
        # Поколение лежит в записи токен-стора; верхний уровень — запасной путь.
        try:
            generation = int(store.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0

    session_expires_ms = 0
    try:
        session_expires_ms = int(session.get("expiresAtMs") or 0)
    except (TypeError, ValueError):
        session_expires_ms = 0

    return {
        "work_root": str(work_root),
        "token_expires_at_ms": expires_at_ms,
        "session_expires_at_ms": session_expires_ms,
        "session_status": str(session.get("status") or ""),
        "generation": generation,
        "store_present": bool(store),
        "session_present": bool(session),
    }


def profile_state(work_root: Path) -> Dict[str, Any]:
    """Метаданные профиля: какой аккаунт в работе и что с пулом."""
    meta = read_json(Path(work_root) / PROFILE_NAME)
    dead = meta.get("dead_account_ids")
    return {
        "karman_id": meta.get("karman_account_id"),
        "email": str(meta.get("email") or ""),
        "leased_day": str(meta.get("leased_day") or ""),
        "activation_day": str(meta.get("activation_day") or ""),
        "dead_account_ids": sorted(int(x) for x in dead) if isinstance(dead, list) else [],
        "updated_at_ms": int(meta.get("updated_at_ms") or 0),
    }


def read_usage(path: Path, since: float) -> Dict[str, Any]:
    """Журнал расхода за окно: вызовы, успех, отказы по причинам.

    ``since`` — epoch-секунды. Возвращается и ``last_call_at`` — последний
    вызов в окне: по нему видно, что трафик идёт.
    """
    calls = 0
    ok = 0
    fail_by_reason: Counter = Counter()
    last_call_at = 0.0
    try:
        handle = Path(path).open("r", encoding="utf-8-sig", errors="replace")
    except OSError:
        return {
            "exists": False,
            "calls": 0,
            "ok": 0,
            "fail_by_reason": {},
            "last_call_at": 0.0,
            "calls_after": 0,
        }
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            try:
                stamp = datetime.fromisoformat(str(row.get("ts") or "")).timestamp()
            except ValueError:
                continue
            if stamp < since:
                continue
            calls += 1
            last_call_at = max(last_call_at, stamp)
            if row.get("ok"):
                ok += 1
            else:
                fail_by_reason[str(row.get("reason") or "?")] += 1
    return {
        "exists": True,
        "calls": calls,
        "ok": ok,
        "fail_by_reason": dict(fail_by_reason.most_common()),
        "last_call_at": last_call_at,
    }


def usage_calls_after(path: Path, expires_at: float) -> int:
    """Сколько вызовов движка было **после** срока access-токена.

    Именно после истечения продление и должно сработать: до срока токен ещё
    жив, и никаких следов продления в логе быть не может.
    """
    if expires_at <= 0:
        return 0
    calls = 0
    try:
        handle = Path(path).open("r", encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                stamp = datetime.fromisoformat(str(row.get("ts") or "")).timestamp()
            except (ValueError, AttributeError):
                continue
            if stamp > expires_at:
                calls += 1
    return calls


def scan_worker_log(paths: Iterable[Path], since: float) -> Dict[str, Any]:
    """Следы в логе воркера: продления, отказы продления, новые выдачи.

    Считаются только строки **после** ``since`` (epoch-секунды) — иначе
    счётчик отражает всю историю журнала, а не окно проверки.
    """
    renewed: List[float] = []
    renew_failed: List[float] = []
    new_accounts: List[float] = []
    rotated: List[float] = []
    panel_unavailable: List[float] = []
    samples: List[str] = []

    for path in paths:
        try:
            handle = Path(path).open("r", encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                stamp = parse_log_timestamp(line)
                if stamp is None or stamp < since:
                    continue
                if MARK_RENEWED in line and MARK_RENEWED_TAIL in line:
                    renewed.append(stamp)
                elif MARK_RENEW_FAILED in line:
                    renew_failed.append(stamp)
                elif MARK_NEW_ACCOUNT in line:
                    new_accounts.append(stamp)
                elif MARK_ROTATED in line:
                    rotated.append(stamp)
                elif MARK_PANEL_UNAVAILABLE in line:
                    panel_unavailable.append(stamp)
                if (
                    MARK_RENEWED in line
                    or MARK_RENEW_FAILED in line
                    or MARK_NEW_ACCOUNT in line
                    or MARK_PANEL_UNAVAILABLE in line
                ):
                    samples.append(line.strip()[:220])
                    # Держим **последние** образцы: первые 40 строк окна давно
                    # неинтересны — смотреть надо на свежие.
                    if len(samples) > 40:
                        samples.pop(0)

    return {
        "renewed": len(renewed),
        "renew_failed": len(renew_failed),
        "new_accounts": len(new_accounts),
        "rotated": len(rotated),
        "panel_unavailable": len(panel_unavailable),
        "last_renewed_at": max(renewed) if renewed else 0.0,
        "last_new_account_at": max(new_accounts) if new_accounts else 0.0,
        "last_panel_unavailable_at": max(panel_unavailable) if panel_unavailable else 0.0,
        "samples": samples,
    }


def worker_log_paths(app_root: Path) -> List[Path]:
    """Текущий лог воркера и его ротации — от свежих к старым."""
    main = Path(app_root) / WORKER_LOG
    paths: List[Path] = []
    if main.exists():
        paths.append(main)
    for candidate in sorted(main.parent.glob(main.name + ".*")):
        # Сжатые ротации не читаем: распаковка — отдельная работа, а следы
        # продления всё равно попадают в свежий лог.
        if candidate.is_file() and not candidate.name.endswith(".gz"):
            paths.append(candidate)
    return paths


def verdict(
    *,
    calls_after_expiry: int,
    renewed: int,
    renew_failed: int,
    new_accounts: int,
) -> Dict[str, str]:
    """Вердикт проверки: что с продлением входа.

    Порядок важнее формулировок: **нет вызовов после истечения** — значит
    проверить было нечего (вердикт «не проверено»), и никакие следы
    продления в этом случае не считаются ни успехом, ни провалом.
    """
    if calls_after_expiry <= 0:
        return {
            "code": "not_checked",
            "text": "не проверено: после истечения токена вызовов ещё не было",
        }
    if renewed > 0 and new_accounts == 0:
        return {
            "code": "renewed",
            "text": "продление сработало: вход продлён, новый аккаунт не брали",
        }
    if renewed > 0:
        return {
            "code": "renewed_with_rotation",
            "text": "продление сработало, но новый аккаунт всё равно брали",
        }
    if renew_failed > 0:
        return {
            "code": "renew_rejected",
            "text": "Карман отклонил продление (обычно 409) — движок взял другой аккаунт",
        }
    if new_accounts > 0:
        return {
            "code": "rotated",
            "text": "вход не продлили: пошёл новый аккаунт, следов продления нет",
        }
    return {
        "code": "no_trace",
        "text": "вызовы после истечения были, следов продления в логе нет — разбираться",
    }


def collect(
    *,
    accounts_dir: Path,
    work_area: str,
    app_root: Path,
    window_hours: float,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Собрать картину: аккаунт, токен, трафик, следы в логе, вердикт."""
    now_ts = float(now if now is not None else datetime.now().timestamp())
    since = now_ts - window_hours * 3600
    work_root = Path(accounts_dir) / work_area

    token = token_state(work_root)
    profile = profile_state(work_root)

    expires_at = (token["token_expires_at_ms"] or 0) / 1000.0
    if not expires_at:
        expires_at = now_ts + FALLBACK_TTL_SECONDS
        token["expires_known"] = False
    else:
        token["expires_known"] = True
    token["expires_at"] = expires_at
    token["expires_in_seconds"] = int(expires_at - now_ts)

    usage_log = Path(app_root) / USAGE_LOG
    usage = read_usage(usage_log, since)
    calls_after = usage_calls_after(usage_log, expires_at)

    log_scan = scan_worker_log(worker_log_paths(app_root), since)
    # Новые выдачи считаем и после истечения токена: именно они показывают,
    # что движок ушёл на другой аккаунт вместо продления.
    after_log = scan_worker_log(worker_log_paths(app_root), expires_at)
    # Каждое из этих событий (продление, отказ продления, уход на другой
    # аккаунт) случилось **в ответ на вызов** — значит, вызов после истечения
    # точно был, даже если журнал расхода недоступен.
    proof_of_calls = after_log["renewed"] + after_log["renew_failed"] + after_log["rotated"]
    calls_after = max(calls_after, proof_of_calls)

    result = verdict(
        calls_after_expiry=calls_after,
        renewed=after_log["renewed"],
        renew_failed=after_log["renew_failed"],
        new_accounts=after_log["new_accounts"],
    )

    return {
        "now": now_ts,
        "window_hours": window_hours,
        "accounts_dir": str(accounts_dir),
        "work_area": work_area,
        "work_root": str(work_root),
        "work_root_exists": work_root.is_dir(),
        "account": profile,
        "token": token,
        "usage": usage,
        "log": log_scan,
        "log_after_expiry": after_log,
        "calls_after_expiry": calls_after,
        "verdict": result,
    }


# --- вывод -------------------------------------------------------------------


def _configure_output() -> None:
    """Кодировка вывода: перенаправленный stdout ждёт UTF-8, консоль — свою.

    На Windows stdout, ушедший в конвейер, кодируется по локали (cp866/cp1251),
    а читают его обычно как UTF-8 — русский текст превращается в кракозябры.
    Консоль при этом остаётся в своей кодировке: там всё видно и так.
    """
    stream = sys.stdout
    try:
        is_tty = bool(stream.isatty())
    except (AttributeError, ValueError):
        is_tty = False
    if is_tty or not hasattr(stream, "reconfigure"):
        return
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _fmt_moment(stamp: float) -> str:
    """Время в локальной зоне бокса (МСК) — так же, как в логе воркера."""
    if not stamp:
        return "—"
    return datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M:%S")


def _fmt_left(seconds: int) -> str:
    if seconds <= 0:
        return "истёк"
    minutes = seconds // 60
    return "%d ч %02d мин" % (minutes // 60, minutes % 60)


def _print(data: Dict[str, Any]) -> None:
    account = data["account"]
    token = data["token"]
    usage = data["usage"]
    log = data["log"]
    after = data["log_after_expiry"]

    print("=== АККАУНТ В РАБОТЕ ===")
    print("  каталог пула: %s (рабочая область: %s)" % (data["accounts_dir"], data["work_area"]))
    if not data["work_root_exists"]:
        print("  ⚠️ рабочая область не найдена: %s" % data["work_root"])
        print("     (путь на боксе: --accounts-dir, по умолчанию ~/minimax-accounts)")
    print("  аккаунт: id=%s %s" % (account["karman_id"], account["email"]))
    print(
        "  аренда: сутки %s, активация %s"
        % (account["leased_day"] or "—", account["activation_day"] or "—")
    )
    if account["updated_at_ms"]:
        print(
            "  рабочая область переведена на этот аккаунт: %s МСК"
            % _fmt_moment(account["updated_at_ms"] / 1000)
        )
    print("  мёртвых аккаунтов в exclude: %d" % len(account["dead_account_ids"]))

    print("=== ВХОД (access-токен) ===")
    if token["expires_known"]:
        print(
            "  срок: %s МСК (осталось %s)"
            % (_fmt_moment(token["expires_at"]), _fmt_left(token["expires_in_seconds"]))
        )
    else:
        print("  срок в токен-сторе не прочитан — срок неизвестен")
    print("  состояние сессии CLI: %s" % (token["session_status"] or "—"))
    print("  поколение сессии: %s" % (token["generation"] or "—"))

    print("=== РАСХОД (окно %d ч) ===" % int(data["window_hours"]))
    if not usage["exists"]:
        print("  журнала нет: %s" % USAGE_LOG)
    else:
        print("  вызовов: %d, успешных: %d" % (usage["calls"], usage["ok"]))
        print("  отказы по причинам: %s" % (usage["fail_by_reason"] or "—"))
        print("  последний вызов: %s" % _fmt_moment(usage["last_call_at"]))
    print("  вызовов после истечения токена: %d" % data["calls_after_expiry"])

    print("=== СЛЕДЫ В ЛОГЕ ВОРКЕРА (окно %d ч) ===" % int(data["window_hours"]))
    print("  продлений входа: %d" % log["renewed"])
    print("  отказов продления: %d" % log["renew_failed"])
    print("  новых выдач аккаунтов: %d" % log["new_accounts"])
    print("  панель не прочиталась (след протухшего токена): %d" % log["panel_unavailable"])
    print(
        "  после истечения токена: продлений %d, отказов %d, новых выдач %d, панель-401 %d"
        % (
            after["renewed"],
            after["renew_failed"],
            after["new_accounts"],
            after["panel_unavailable"],
        )
    )
    for line in log["samples"][-10:]:
        print("  · %s" % line)

    print("=== ВЕРДИКТ ===")
    print("  %s" % data["verdict"]["text"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_output()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--accounts-dir",
        default=os.getenv("MINIMAX_ACCOUNTS_DIR") or "",
        help="каталог пула аккаунтов (по умолчанию — из MINIMAX_ACCOUNTS_DIR)",
    )
    parser.add_argument(
        "--work-area",
        default=os.getenv("MINIMAX_WORK_AREA") or DEFAULT_WORK_AREA,
        help="рабочая область внутри каталога пула (по умолчанию work)",
    )
    parser.add_argument(
        "--app-root",
        default=str(ROOT),
        help="корень приложения: там журналы расхода и логи воркера",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="окно разбора журналов в часах (по умолчанию 24)",
    )
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args(argv)

    accounts_dir = args.accounts_dir
    if not accounts_dir:
        candidate = Path.home() / "minimax-accounts"
        accounts_dir = str(candidate) if candidate.exists() else DEFAULT_ACCOUNTS_DIR

    data = collect(
        accounts_dir=Path(accounts_dir),
        work_area=args.work_area,
        app_root=Path(args.app_root),
        window_hours=args.window_hours,
    )
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
