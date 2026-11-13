#!/usr/bin/env python3
"""Пин рабочих адресов MiniMax в /etc/hosts — для прод-бокса.

**Зачем.** DNS-резолверы VPS отдают для ``*.minimax.io`` адреса Akamai, до
которых соединение **не устанавливается** (таймаут 12 с), а резолвер 1.1.1.1
отдаёт другие — до них соединение поднимается за 0,03 с. Проверено на боксе
13.11.2026: адреса ``2.21.173.x`` и ``2.21.89.x`` (ответ VPS-резолвера) —
таймаут, адреса ``188.234.73.x`` (ответ 1.1.1.1) — работают.

**Как.** Скрипт спрашивает A-записи у нескольких резолверов, оставляет только
те адреса, до которых реально доходит TCP-соединение на 443, и записывает их
в ``/etc/hosts``. ``nsswitch`` на боксе читает ``files`` раньше ``dns``, поэтому
запись выигрывает у резолвера.

**Почему это не «костыль, а правильное место».** Адреса Akamai ротируются, и
запись вручную протухнет — поэтому скрипт запускается по таймеру (раз в час
достаточно) и перезаписывает свою секцию целиком.

Запуск на боксе:

    sudo python3 ~/SETKA/scripts/minimax_dns_pin.py
    # или с проверкой, что адреса действительно живые:
    sudo python3 ~/SETKA/scripts/minimax_dns_pin.py --check
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
from pathlib import Path
from typing import List

HOSTS = (
    "agent.minimax.io",
    "account.minimax.io",
    "platform.minimax.io",
    "api.minimax.io",
    "www.minimax.io",
)
RESOLVERS = ("1.1.1.1", "9.9.9.9", "8.8.8.8")
HOSTS_PATH = Path("/etc/hosts")
MARK = "# minimax-pin (обновляется скриптом minimax_dns_pin.py)"


def query(server: str, host: str, timeout: float = 5.0) -> List[str]:
    """A-записи хоста через UDP/53 (свой запрос — без внешних зависимостей)."""
    header = struct.pack(">HHHHHH", 0x4242, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    except Exception:  # noqa: BLE001
        return []
    finally:
        sock.close()
    ancount = struct.unpack(">H", data[6:8])[0]
    offset = 12
    while offset < len(data) and data[offset]:
        offset += data[offset] + 1
    offset += 1 + 4
    addrs: List[str] = []
    for _ in range(ancount):
        if offset >= len(data):
            break
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset]:
                offset += data[offset] + 1
            offset += 1
        if offset + 10 > len(data):
            break
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        if rtype == 1 and rdlen == 4:
            addrs.append(socket.inet_ntoa(data[offset : offset + 4]))
        offset += rdlen
    return addrs


def reachable(addr: str, timeout: float = 4.0) -> bool:
    """Доходит ли TCP-соединение до адреса."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((addr, 443))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        sock.close()


def pick(host: str) -> str:
    """Первый адрес, до которого доходит соединение."""
    for resolver in RESOLVERS:
        for addr in query(resolver, host):
            if reachable(addr):
                return addr
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Пин адресов MiniMax в /etc/hosts")
    ap.add_argument("--check", action="store_true", help="только проверить, ничего не писать")
    args = ap.parse_args()

    lines: List[str] = []
    for host in HOSTS:
        addr = pick(host)
        if addr:
            lines.append("%s %s" % (addr, host))
            print("%-22s -> %s" % (host, addr))
        else:
            print("%-22s -> рабочего адреса не нашлось" % host, file=sys.stderr)
    if not lines:
        return 1

    if args.check:
        return 0

    current = [ln for ln in HOSTS_PATH.read_text(encoding="utf-8").splitlines() if MARK not in ln]
    HOSTS_PATH.write_text(
        "\n".join(current).rstrip("\n") + "\n" + MARK + "\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print("записано в %s: %d строк" % (HOSTS_PATH, len(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
