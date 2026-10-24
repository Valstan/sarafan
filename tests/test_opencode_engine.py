"""Тесты движка OpenCode-классификации (v2, единый serve; 17.10).

Мокаем транспорт ``urllib`` — сеть не нужна. Проверяем то, что дороже всего
ломается молча: сбор ответа из ленты сессии v2
(create -> prompt -> wait -> message -> delete), фолбэк по цепочке при отказе
модели, границу «все модели молчат» (контракт ``engine_all_failed`` — чтобы
классификатор получил тихий отказ, а не исключение) и запрет инстанса-двойника
(``OPENCODE_NO_SPAWN``): на боксе живёт один serve на два дела, и движок не
имеет права поднимать второй.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from modules import opencode_engine
from modules.opencode_engine import chat, parse_messages


class _FakeResponse:
    """Файл-подобный ответ urlopen: read() плюс контекстный менеджер."""

    def __init__(self, status: int, payload: Any = None):
        self._status = status
        self._payload = payload

    @property
    def status(self) -> int:
        return self._status

    def read(self) -> bytes:
        if self._payload is None:
            return b""
        if isinstance(self._payload, (dict, list)):
            return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")
        return str(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


# План маршрутов: (метод, суффикс пути, ответ либо исключение). Ответ берётся
# один раз — повторный незапланированный запрос роняет тест громко.
_Plan = List[Tuple[str, str, Any]]


class _Transport:
    """urlopen-маршрутизатор: помнит запросы (метод, URL, тело), отдаёт план."""

    def __init__(self, plan: _Plan):
        self.remaining = list(plan)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, req, timeout: Optional[float] = None):  # noqa: ARG002
        path = req.full_url.split("?", 1)[0]
        body = None
        if getattr(req, "data", None):
            try:
                body = json.loads(req.data.decode("utf-8"))
            except Exception:  # pragma: no cover — тела наших запросов всегда JSON
                body = req.data
        self.calls.append({"method": req.get_method(), "url": req.full_url, "body": body})
        for i, (method, suffix, resp) in enumerate(self.remaining):
            if method == req.get_method() and path.endswith(suffix):
                self.remaining.pop(i)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"незапланированный запрос: {req.get_method()} {req.full_url}")


def _session_route(sid: str = "ses_t1") -> Tuple[str, str, _FakeResponse]:
    return ("POST", "/api/session", _FakeResponse(200, {"data": {"id": sid}}))


def _prompt_route() -> Tuple[str, str, _FakeResponse]:
    return ("POST", "/prompt", _FakeResponse(200, {"data": {"id": "msg_t1"}}))


def _wait_route() -> Tuple[str, str, _FakeResponse]:
    return ("POST", "/wait", _FakeResponse(204, None))


def _message_route(*messages: Dict[str, Any]) -> Tuple[str, str, _FakeResponse]:
    return ("GET", "/message", _FakeResponse(200, {"data": list(messages)}))


def _delete_route(sid: str) -> Tuple[str, str, _FakeResponse]:
    return ("DELETE", f"/{sid}", _FakeResponse(204, None))


def _assistant_msg(
    text: str, tokens: Optional[Dict[str, Any]] = None, finish: str = "stop"
) -> Dict[str, Any]:
    return {
        "type": "assistant",
        "agent": "build",
        "model": {"id": "longcat-2.5-preview-free", "providerID": "opencode"},
        "content": [
            {"type": "reasoning", "text": "размышление"},
            {"type": "text", "text": text},
        ],
        "tokens": tokens if tokens is not None else {"input": 100, "output": 20, "reasoning": 5},
        "finish": finish,
    }


def _idle_msg(outcome: str = "succeeded", error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"type": "idle", "outcome": outcome}
    if error is not None:
        msg["error"] = error
    return msg


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_ENGINE", "opencode")
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4999")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "")
    # Цепочка фиксируется в тесте: дефолт меняется по замеру каталога моделей,
    # и падение из-за смены головы цепочки — шум, а не регрессия.
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "longcat-2.5-preview-free")


@pytest.fixture
def spawn_allowed(monkeypatch):
    """Спавн-пути живут только вне запрета OPENCODE_NO_SPAWN (прод)."""
    monkeypatch.delenv("OPENCODE_NO_SPAWN", raising=False)


def _use_transport(monkeypatch, plan: _Plan) -> _Transport:
    transport = _Transport(plan)
    monkeypatch.setattr(opencode_engine.urllib.request, "urlopen", transport)
    return transport


def test_chat_answers_v2(monkeypatch, engine_on):
    """Полный цикл v2: сессия с моделью -> prompt -> wait -> message -> delete."""
    transport = _use_transport(
        monkeypatch,
        [
            _session_route("ses_a"),
            _prompt_route(),
            _wait_route(),
            _message_route(_idle_msg(), _assistant_msg('{"verdicts": []}'), {"type": "user"}),
            _delete_route("ses_a"),
        ],
    )
    result = chat(user="посты", system="постулаты", label="test")
    assert result["ok"] is True
    assert result["content"] == '{"verdicts": []}'
    assert result["model"] == "opencode/longcat-2.5-preview-free"
    assert result["usage"] == {"total_tokens": 120, "prompt_tokens": 100, "completion_tokens": 20}

    create = transport.calls[0]
    assert create["method"] == "POST" and create["url"].endswith("/api/session")
    assert create["body"]["model"] == {
        "id": "longcat-2.5-preview-free",
        "providerID": "opencode",
    }
    assert {"action": "shell", "resource": "*", "effect": "deny"} in create["body"]["permissions"]
    prompt = transport.calls[1]
    assert prompt["body"]["text"] == "постулаты\n\nпосты"
    assert [c["method"] for c in transport.calls] == ["POST", "POST", "POST", "GET", "DELETE"]
    assert transport.remaining == []


def test_classify_session_permissions_deny_tools_but_not_read_all():
    """Инструменты запрещены; read на ``*`` — нет: он ломает free-tier (проба).

    При этом узкие read-запреты есть — они проверены живьём: точечное чтение
    anon.env отклоняется, контрольное чтение живо, вызов не ломается.
    """
    rules = opencode_engine.CLASSIFY_SESSION_PERMISSIONS
    actions = {rule["action"] for rule in rules}
    assert {"shell", "edit", "webfetch", "subagent", "execute"} <= actions
    assert not any(r["action"] == "read" and r["resource"] == "*" for r in rules)
    assert any(r == {"action": "read", "resource": "*anon.env*", "effect": "deny"} for r in rules)
    assert any(r == {"action": "read", "resource": "*/.ssh/*", "effect": "deny"} for r in rules)


def test_parse_messages_collects_text_usage_and_error():
    text, usage, error = parse_messages(
        [
            _idle_msg(),
            _assistant_msg("вердикты", tokens={"input": 10, "output": 5}),
            {"type": "user", "text": "посты"},
        ]
    )
    assert text == "вердикты"
    # ``finish`` живёт внутри usage намеренно (см. докстринг parse_messages):
    # кортеж из трёх элементов — публичный контракт.
    assert usage == {"input": 10, "output": 5, "finish": "stop"}
    assert error is None

    text, usage, error = parse_messages(
        [
            _idle_msg(),
            _assistant_msg("оборвано", finish="length"),
        ]
    )
    assert text == "оборвано"
    assert (usage or {}).get("finish") == "length", "причину остановки модель обязана донести"

    text, _, error = parse_messages(
        [
            _idle_msg("failed", {"type": "provider.no-route", "message": "Model unavailable"}),
            {"type": "user", "text": "посты"},
        ]
    )
    assert text == ""
    assert error == "Model unavailable"


def test_reason_from_error_maps_http_codes_and_model_errors():
    from modules.opencode_engine import _reason_from_error

    assert _reason_from_error("http_429: too many requests") == "http_429"
    assert _reason_from_error("Model unavailable: opencode/x") == "model_error"


def test_chat_falls_back_when_model_unavailable(monkeypatch, engine_on):
    """provider.no-route у первой модели — отвечает вторая, вызов зелёный."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "muse-dead-free,longcat-2.5-preview-free")
    transport = _use_transport(
        monkeypatch,
        [
            _session_route("ses_1"),
            _prompt_route(),
            _wait_route(),
            _message_route(
                _idle_msg(
                    "failed",
                    {
                        "type": "provider.no-route",
                        "message": "Model unavailable: opencode/muse-dead-free",
                    },
                ),
                {"type": "user"},
            ),
            _delete_route("ses_1"),
            _session_route("ses_2"),
            _prompt_route(),
            _wait_route(),
            _message_route(_idle_msg(), _assistant_msg("OK"), {"type": "user"}),
            _delete_route("ses_2"),
        ],
    )
    result = chat(user="посты", label="test")
    assert result["ok"] is True
    assert result["content"] == "OK"
    assert result["model"] == "opencode/longcat-2.5-preview-free"
    assert transport.remaining == []


def test_chat_falls_back_when_session_create_rejected(monkeypatch, engine_on):
    """400 на создании сессии (модели нет в пуле) — пробуем следующую."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "ghost-free,longcat-2.5-preview-free")
    transport = _use_transport(
        monkeypatch,
        [
            ("POST", "/api/session", _FakeResponse(400, {"error": "model not found"})),
            _session_route("ses_ok"),
            _prompt_route(),
            _wait_route(),
            _message_route(_idle_msg(), _assistant_msg("OK"), {"type": "user"}),
            _delete_route("ses_ok"),
        ],
    )
    result = chat(user="посты", label="test")
    assert result["ok"] is True
    assert result["model"] == "opencode/longcat-2.5-preview-free"
    assert transport.remaining == []


def test_chat_treats_silent_session_as_empty_response(monkeypatch, engine_on):
    """Модель промолчала (ни текста, ни ошибки) — шанс следующей модели."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "silent-free,longcat-2.5-preview-free")
    transport = _use_transport(
        monkeypatch,
        [
            _session_route("ses_silent"),
            _prompt_route(),
            _wait_route(),
            _message_route(_idle_msg(), {"type": "user"}),
            _delete_route("ses_silent"),
            _session_route("ses_ok"),
            _prompt_route(),
            _wait_route(),
            _message_route(_idle_msg(), _assistant_msg("OK"), {"type": "user"}),
            _delete_route("ses_ok"),
        ],
    )
    result = chat(user="посты", label="test")
    assert result["ok"] is True
    assert result["model"] == "opencode/longcat-2.5-preview-free"
    assert transport.remaining == []


def test_chat_all_models_fail_gives_engine_all_failed(monkeypatch, engine_on):
    """Транспорт мёртв (serve не поднят) — тихий отказ, не исключение."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "longcat-2.5-preview-free,space-bunny-free")
    _use_transport(
        monkeypatch,
        [
            ("POST", "/api/session", OSError("Connection refused")),
            ("POST", "/api/session", OSError("Connection refused")),
        ],
    )
    result = chat(user="посты", label="test")
    assert result["ok"] is False
    assert result["reason"] == "engine_all_failed"
    assert result["detail"].count("network") == 2


def test_chat_skips_retry_when_no_base_url(monkeypatch, engine_on):
    """no_base_url не лечится следующей моделью — выходим сразу."""
    monkeypatch.setenv("OPENCODE_BASE_URL", "   ")
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "a-free,b-free,c-free")
    transport = _use_transport(monkeypatch, [])
    result = chat(user="посты", label="test")
    assert result["ok"] is False
    assert result["reason"] == "engine_all_failed"
    assert result["detail"].count("no_base_url") == 1
    assert transport.calls == []


def test_chat_engine_disabled(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    result = chat(user="посты", label="test")
    assert result == {"ok": False, "reason": "engine_disabled"}


def test_chat_empty_prompt(monkeypatch, engine_on):
    result = chat(user="   ", label="test")
    assert result == {"ok": False, "reason": "empty_prompt"}


def test_chat_timeout_budget_covers_whole_chain(monkeypatch, engine_on):
    """``timeout`` — бюджет всего вызова, а не одной модели.

    Живой повод 21.10: цепочка трёх моделей с двумя попытками умножает потолок
    на шесть, поэтому «25 с на пересказ» превращались в 120 с простоя слота.
    """
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "m1,m2,m3")

    class _Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def monotonic(self) -> float:
            self.t += 0.5
            return self.t

    monkeypatch.setattr(opencode_engine, "time", _Clock())

    seen: List[Optional[float]] = []

    def fake_call(model_id, prompt, label, timeout=None):
        seen.append(timeout)
        return {"ok": False, "reason": "http_429", "detail": "limit"}

    monkeypatch.setattr(opencode_engine, "_call_model", fake_call)

    result = chat(user="пост", timeout=2.0)

    assert result["ok"] is False
    assert result["reason"] == "engine_all_failed"
    assert "budget_exhausted" in result["detail"]
    assert seen, "первая модель должна была получить остаток бюджета"
    assert all(t is not None and t <= 2.0 for t in seen)
    assert len(seen) < 3, "бюджет кончился — вторую модель звать уже нечем"


def test_stalled_model_does_not_eat_the_whole_budget(monkeypatch, engine_on):
    """Зависшая модель платит своим потолком, а не бюджетом всей цепочки.

    Живой повод 21.10: 69 волн за день (15 %) ушли в fail-open с
    ``engine_all_failed``, а в логе — ``модель deepseek-v4.1-flash-free отказала
    (network TimeoutError)``. Первая модель получала весь бюджет вызова,
    зависала на нём и оставляла остальные две без времени. Здесь — что цепочка
    доходит до конца, хотя каждая модель висит.
    """
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "m1,m2,m3")
    monkeypatch.setenv("OPENCODE_MODEL_TIMEOUT", "20")

    class _Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def monotonic(self) -> float:
            return self.t

    clock = _Clock()
    monkeypatch.setattr(opencode_engine, "time", clock)
    monkeypatch.setattr(opencode_engine, "_rotate_egress", lambda detail: False)

    seen: List[Tuple[str, Optional[float]]] = []

    def fake_call(model_id, prompt, label, timeout=None):
        seen.append((model_id, timeout))
        clock.t += timeout or 0.0  # модель «висит» весь отпущенный ей бюджет
        return {"ok": False, "reason": "network", "detail": "TimeoutError"}

    monkeypatch.setattr(opencode_engine, "_call_model", fake_call)

    result = chat(user="пост", timeout=60.0)

    assert result["reason"] == "engine_all_failed"
    assert [m for m, _ in seen] == ["m1", "m2", "m3"], "каждая модель должна получить шанс"
    assert all(t is not None and t <= 20.0 for _, t in seen)


def test_publish_budget_gives_second_model_a_chance(monkeypatch, engine_on):
    """Бюджет публикации 40 с: раньше первая же зависшая модель съедала всё,
    теперь до второй дело доходит."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "m1,m2,m3")
    monkeypatch.setenv("OPENCODE_MODEL_TIMEOUT", "20")

    class _Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def monotonic(self) -> float:
            return self.t

    clock = _Clock()
    monkeypatch.setattr(opencode_engine, "time", clock)
    monkeypatch.setattr(opencode_engine, "_rotate_egress", lambda detail: False)

    seen: List[Tuple[str, Optional[float]]] = []

    def fake_call(model_id, prompt, label, timeout=None):
        seen.append((model_id, timeout))
        clock.t += timeout or 0.0
        return {"ok": False, "reason": "network", "detail": "TimeoutError"}

    monkeypatch.setattr(opencode_engine, "_call_model", fake_call)

    chat(user="пост", timeout=40.0)

    assert len(seen) >= 2, "вторая модель должна получить свою половину бюджета"


def test_chat_without_timeout_keeps_engine_default(monkeypatch, engine_on):
    seen: List[Optional[float]] = []

    def fake_call(model_id, prompt, label, timeout=None):
        seen.append(timeout)
        return {"ok": False, "reason": "http_429", "detail": "limit"}

    monkeypatch.setattr(opencode_engine, "_call_model", fake_call)

    chat(user="пост")

    assert seen == [None], "без явного бюджета поведение прежнее: общий таймаут движка"


def test_headless_router_follows_engine_flag(monkeypatch, engine_on):
    """Флаг CLASSIFIER_ENGINE реально переводят вызов на opencode и обратно."""
    from modules import opencode_engine
    from modules.classifier import headless

    monkeypatch.setattr(
        opencode_engine,
        "chat",
        lambda **kw: {"ok": True, "content": "x", "model": "opencode/y", "usage": None},
    )
    sentinel = {"ok": False, "reason": "deepseek-path"}
    monkeypatch.setattr(headless, "_deepseek_chat", lambda **kw: sentinel)

    assert headless.chat(user="u") == {
        "ok": True,
        "content": "x",
        "model": "opencode/y",
        "usage": None,
    }

    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    assert headless.chat(user="u") is sentinel


def test_normalize_content_unwraps_markdown_and_array():
    """longcat отдаёт ```json + топ-level массив — движок приводит к контракту."""
    from modules.opencode_engine import _normalize_content

    assert json.loads(_normalize_content('```json\n[{"a": 1}]\n```')) == {"verdicts": [{"a": 1}]}
    # Объект с нужным ключом проходит как есть.
    raw = '{"verdicts": [{"a": 1}]}'
    assert _normalize_content(raw) == raw
    # Не-JSON не трогаем: пусть классификатор получит llm_unparseable.
    assert _normalize_content(" boltox ") == " boltox "


def test_normalize_usage_maps_serve_counters():
    from modules.opencode_engine import _normalize_usage

    usage = _normalize_usage({"input": 4254, "output": 181, "reasoning": 251, "total": 4435})
    assert usage == {"total_tokens": 4435, "prompt_tokens": 4254, "completion_tokens": 181}
    assert _normalize_usage(None) is None


class _FakeChild:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):  # noqa: ARG002
        return 0

    def kill(self):
        self.killed = True


def test_port_of_parses_base_url(monkeypatch):
    from modules.opencode_engine import _port_of

    assert _port_of("http://127.0.0.1:4096") == 4096
    assert _port_of("http://127.0.0.1:4099/base") == 4099
    assert _port_of("мусор") == 4096


def test_no_spawn_flag_reads_env(monkeypatch):
    from config import opencode as oc

    monkeypatch.delenv("OPENCODE_NO_SPAWN", raising=False)
    assert oc.no_spawn() is False
    monkeypatch.setenv("OPENCODE_NO_SPAWN", "1")
    assert oc.no_spawn() is True
    monkeypatch.setenv("OPENCODE_NO_SPAWN", "0")
    assert oc.no_spawn() is False


def test_harness_server_reuses_live_serve(monkeypatch):
    """Порт занят живым serve — своего ребёнка не плодим."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: True)

    def boom(*a, **kw):  # pragma: no cover — Popen не должен вызываться
        raise AssertionError("Popen вызван при живом serve")

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", boom)
    with opencode_engine.harness_server() as alive:
        assert alive is True


def test_harness_server_no_spawn_waits_for_restart(monkeypatch):
    """Прод-режим: своего serve не поднимаем — даём юниту фору на рестарт."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setenv("OPENCODE_NO_SPAWN", "1")
    waits: List[float] = []

    def fake_wait(host, port, timeout=45.0):  # noqa: ARG001
        waits.append(timeout)
        return True

    monkeypatch.setattr(opencode_engine, "_wait_port", fake_wait)

    def boom(*a, **kw):  # pragma: no cover — Popen не должен вызываться
        raise AssertionError("Popen вызван в режиме OPENCODE_NO_SPAWN")

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", boom)
    with opencode_engine.harness_server() as alive:
        assert alive is True
    assert waits == [opencode_engine.NO_SPAWN_WAIT_SEC]


def test_harness_server_no_spawn_dead_serve_fails_open(monkeypatch):
    """Сервис лежит и спавн запрещён — чанки идут без нейро-фильтра, не падаем."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setenv("OPENCODE_NO_SPAWN", "1")
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: False)

    def boom(*a, **kw):  # pragma: no cover — Popen не должен вызываться
        raise AssertionError("Popen вызван в режиме OPENCODE_NO_SPAWN")

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", boom)
    with opencode_engine.harness_server() as alive:
        assert alive is False


def test_harness_server_spawns_and_terminates(monkeypatch, spawn_allowed):
    """Порт свободен — serve поднят ребёнком и убит в finally."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")

    seen = {}
    child = _FakeChild()

    def fake_popen(cmd, env=None, **kwargs):  # noqa: ARG001
        seen["cmd"] = cmd
        seen["start_new_session"] = kwargs.get("start_new_session")
        return child

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", fake_popen)
    with opencode_engine.harness_server() as alive:
        assert alive is True
    assert "serve" in seen["cmd"]
    assert seen["start_new_session"] is True
    assert child.terminated is True


def test_harness_server_terminates_on_exception(monkeypatch, spawn_allowed):
    """Падение прогона внутри блока не оставляет serve-сироты."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")
    child = _FakeChild()
    monkeypatch.setattr(opencode_engine.subprocess, "Popen", lambda *a, **kw: child)

    with pytest.raises(RuntimeError):
        with opencode_engine.harness_server():
            raise RuntimeError("прогон упал")
    assert child.terminated is True


def test_default_chain_excludes_geo_dead_models():
    """Мёртвая голова цепочки съедает таймаут на каждом чанке — её там нет."""
    from config.opencode import DEFAULT_MODEL_CHAIN

    assert "muse-spark-1.3-contributor-free" not in DEFAULT_MODEL_CHAIN
    assert "space-bunny-free" not in DEFAULT_MODEL_CHAIN
    assert DEFAULT_MODEL_CHAIN[0] == "deepseek-v4.1-flash-free"


def test_default_chain_priority_is_deepseek_then_glm():
    """Решение владельца 24.10: DeepSeek первый, GLM второй, прочие — в крайнем случае."""
    from config.opencode import DEFAULT_MODEL_CHAIN

    assert DEFAULT_MODEL_CHAIN[:2] == ["deepseek-v4.1-flash-free", "glm-5.3-flash-free"]
    # longcat ушёл из каталога платформы (замер 24.10, `/api/model`): мёртвая
    # голова стоила таймаута на каждом чанке.
    assert "longcat-2.5-preview-free" not in DEFAULT_MODEL_CHAIN


def test_model_variants_default_high_for_deepseek_and_glm(monkeypatch):
    """У DeepSeek и GLM — high, а не max: на максимуме обе галлюцинируют."""
    from config import opencode as oc

    monkeypatch.delenv("OPENCODE_MODEL_VARIANTS", raising=False)
    assert oc.get_model_variant("deepseek-v4.1-flash-free") == "high"
    assert oc.get_model_variant("glm-5.3-flash-free") == "high"
    # Модели вне карты получают дефолт платформы, а не выдуманный уровень.
    assert oc.get_model_variant("mimo-v2.6-flash-free") == ""


def test_model_variants_env_override_and_off(monkeypatch):
    from config import opencode as oc

    monkeypatch.setenv(
        "OPENCODE_MODEL_VARIANTS",
        "deepseek-v4.1-flash-free=max, flint-40b-free=xhigh, mimo-v2.6-flash-free=off",
    )
    assert oc.get_model_variant("deepseek-v4.1-flash-free") == "max"
    assert oc.get_model_variant("flint-40b-free") == "xhigh"
    assert oc.get_model_variant("mimo-v2.6-flash-free") == ""
    assert oc.get_model_variant("glm-5.3-flash-free") == ""


def test_serve_limits_from_env(monkeypatch):
    from config import opencode as oc

    assert oc.get_serve_rss_kill_mb() == 450
    assert oc.get_serve_max_lifetime() == 15 * 60
    monkeypatch.setenv("OPENCODE_SERVE_RSS_KILL_MB", "300")
    monkeypatch.setenv("OPENCODE_SERVE_MAX_LIFETIME", "600")
    assert oc.get_serve_rss_kill_mb() == 300
    assert oc.get_serve_max_lifetime() == 600.0


class _FakeTimer:
    started = False
    cancelled = False

    def __init__(self, delay, fn):  # noqa: ARG002
        type(self).started = True

    def start(self):
        pass

    def cancel(self):
        type(self).cancelled = True


def test_harness_server_starts_rss_monitor(monkeypatch, spawn_allowed):
    """Ребёнок без preexec_fn (RLIMIT_AS ломает старт bun); RSS-монитор
    запускается и останавливается при выходе."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")

    seen = {}
    child = _FakeChild()

    def fake_popen(cmd, env=None, **kwargs):  # noqa: ARG001
        seen["preexec_fn"] = kwargs.get("preexec_fn")
        seen["start_new_session"] = kwargs.get("start_new_session")
        return child

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(opencode_engine.threading, "Timer", _FakeTimer)

    with opencode_engine.harness_server():
        pass
    assert seen["preexec_fn"] is None
    assert seen["start_new_session"] is True
    assert _FakeTimer.started is True
    assert _FakeTimer.cancelled is True
    assert child.terminated is True


def test_harness_server_systemd_oneshot(monkeypatch, spawn_allowed):
    """Legacy-путь ручных прогонов: systemctl start/stop вместо Popen."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_SYSTEMD_RUN", "1")

    calls = []

    def fake_systemctl(action, timeout=30.0):  # noqa: ARG002
        calls.append(action)
        return True

    monkeypatch.setattr(opencode_engine, "_systemctl_run", fake_systemctl)

    def boom(*a, **kw):  # pragma: no cover — Popen не должен вызываться
        raise AssertionError("Popen вызван в systemd-режиме")

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", boom)
    with opencode_engine.harness_server() as alive:
        assert alive is True
    assert calls == ["start", "stop"]


def test_harness_server_systemd_start_failure(monkeypatch, spawn_allowed):
    """systemctl start не прошёл — yield False, stop не зовём."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setenv("OPENCODE_SYSTEMD_RUN", "1")
    monkeypatch.setattr(
        opencode_engine, "_systemctl_run", lambda action, timeout=30.0: action == "stop"
    )

    with opencode_engine.harness_server() as alive:
        assert alive is False


def test_child_rss_mb_reads_proc():
    import os

    from modules.opencode_engine import _child_rss_mb

    if not os.path.exists("/proc/self/status"):  # pragma: no cover — Windows
        pytest.skip("/proc доступен только на Linux")
    rss = _child_rss_mb(os.getpid())
    assert rss is not None and rss > 0
    assert _child_rss_mb(999999) is None


def test_classify_posts_wraps_harness_for_opencode_engine(monkeypatch):
    """Батч под opencode идёт внутри harness_server — воркеру не надо ничего знать."""
    from modules import opencode_engine
    from modules.classifier import headless

    posts = [{"lip": "-1_1", "region_code": "mi", "text": "тест"}]

    class _Ctx:
        entered = False

        def __enter__(self):
            type(self).entered = True
            return True

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(opencode_engine, "harness_server", lambda: _Ctx())
    verdict_json = (
        '{"verdicts": [{"lip": "-1_1", "theme": "новости", '
        '"action": "publish", "confidence": 90}]}'
    )
    monkeypatch.setattr(
        opencode_engine,
        "chat",
        lambda **kw: {
            "ok": True,
            "content": verdict_json,
            "model": "opencode/x",
            "usage": {"total_tokens": 10, "prompt_tokens": 6, "completion_tokens": 4},
        },
    )
    monkeypatch.setattr(headless, "engine_enabled", lambda: True)

    run = headless.classify_posts(posts, postulates="p")
    assert _Ctx.entered is True
    assert len(run["verdicts"]) == 1
    assert run["failures"] == []
    assert run["tokens"] == 10


def test_sweep_opencode_spill_removes_only_old_matching(tmp_path):
    """P197: старый `.bcdf*.so` убран, свежий и посторонние файлы — нет."""
    import os
    import time

    from modules.opencode_engine import sweep_opencode_spill

    old = tmp_path / ".bcdf1234abcd-00000000.so"
    old.write_bytes(b"x" * 1024)
    fresh = tmp_path / ".bcdf5678efgh-00000000.so"
    fresh.write_bytes(b"y" * 1024)
    other = tmp_path / "keep.txt"
    other.write_bytes(b"z")
    now = time.time()
    os.utime(old, (now - 3600, now - 3600))
    os.utime(other, (now - 3600, now - 3600))

    stats = sweep_opencode_spill(str(tmp_path), max_age_sec=1800, now=now)

    assert not old.exists()
    assert fresh.exists()
    assert other.exists()
    assert stats["deleted"] == 1
    assert stats["freed_bytes"] == 1024
    assert stats["failed"] == 0


def test_sweep_opencode_spill_removes_stale_run_dir(tmp_path):
    """Каталог запуска, прерванного до finally, не живёт вечно."""
    import os
    import time

    from modules.opencode_engine import sweep_opencode_spill

    stale = tmp_path / "setka-opencode-stale"
    stale.mkdir()
    (stale / ".bcdf0000-00000000.so").write_bytes(b"x" * 10)
    live = tmp_path / "setka-opencode-live"
    live.mkdir()
    now = time.time()
    os.utime(stale, (now - 3600, now - 3600))

    stats = sweep_opencode_spill(str(tmp_path), max_age_sec=1800, now=now)

    assert not stale.exists()
    assert live.exists()
    assert stats["removed_dirs"] == 1


def test_sweep_opencode_spill_missing_dir_is_silent(tmp_path):
    """Свипер не бросает: падающий на каждом тике свипер — молчащий свипер."""
    from modules.opencode_engine import sweep_opencode_spill

    stats = sweep_opencode_spill(str(tmp_path / "нет-каталога"))

    assert stats["deleted"] == 0
    assert stats["failed"] == 0


def test_harness_server_popen_gets_private_tmpdir(monkeypatch, spawn_allowed):
    """P197: ребёнок получает приватный TMPDIR, каталог исчезает после блока."""
    import os
    from pathlib import Path

    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")

    seen = {}
    child = _FakeChild()

    def fake_popen(cmd, env=None, **kwargs):  # noqa: ARG001
        seen["env"] = env
        return child

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", fake_popen)
    with opencode_engine.harness_server():
        tmpdir = seen["env"]["TMPDIR"]
        assert os.path.isdir(tmpdir)
        # Мусор запуска оседает внутри приватного каталога...
        Path(tmpdir, ".bcdf0000-00000000.so").write_bytes(b"x")
    # ...и уходит вместе с ним.
    assert not Path(tmpdir).exists()


def test_beat_schedule_has_opencode_tmp_sweep():
    """Страховка P197 зарегистрирована в beat — иначе она молчит."""
    from tasks.celery_app import app

    entry = app.conf.beat_schedule.get("sweep-opencode-tmp")
    assert entry is not None
    assert entry["task"] == "tasks.celery_app.sweep_opencode_tmp"
    assert entry["options"]["catchup"] is False


GATE_ERROR = (
    "OpenCode's free models aren't available from your current network because"
    " we've detected abusive traffic from this IP address."
)


def test_reason_from_error_detects_network_gate():
    """Гейт free-тира по IP идёт без HTTP-кода — узнаём по тексту."""
    from modules.opencode_engine import _reason_from_error

    assert _reason_from_error(GATE_ERROR) == "network_gate"
    assert _reason_from_error("http_429: rate limit") == "http_429"
    assert _reason_from_error("Model unavailable") == "model_error"


def test_chat_rotates_egress_and_retries_after_gate(monkeypatch):
    """Вся цепочка легла на гейт по IP → меняем выход и повторяем один раз."""
    from modules import opencode_engine as engine

    monkeypatch.setenv("CLASSIFIER_ENGINE", "opencode")
    chain_len = len(engine.get_model_chain())
    calls = {"models": 0, "rotate": 0}

    def fake_call(model_id, prompt, label):
        calls["models"] += 1
        # Первая цепочка целиком — гейт, вторая — успех.
        if calls["models"] <= chain_len:
            return {"ok": False, "reason": "network_gate", "detail": GATE_ERROR}
        return {"ok": True, "content": "ok", "model": f"opencode/{model_id}", "usage": None}

    monkeypatch.setattr(engine, "_call_model", fake_call)
    monkeypatch.setattr(
        engine,
        "_rotate_egress",
        lambda detail="": calls.__setitem__("rotate", calls["rotate"] + 1) or True,
    )

    result = engine.chat(user="u")

    assert result["ok"] is True
    assert calls["rotate"] == 1
    assert calls["models"] == chain_len + 1


def test_chat_rotates_egress_and_retries_after_quota(monkeypatch):
    """Лимит 429 на всей цепочке — тоже per-IP: меняем выход и повторяем.

    Замер 20.10 19:40: все три модели ``http_429 Rate limit exceeded``,
    поворот ВПН → тот же запрос прошёл за 4,9 с.
    """
    from modules import opencode_engine as engine

    monkeypatch.setenv("CLASSIFIER_ENGINE", "opencode")
    chain_len = len(engine.get_model_chain())
    calls = {"models": 0, "rotate": 0}

    def fake_call(model_id, prompt, label):
        calls["models"] += 1
        if calls["models"] <= chain_len:
            return {
                "ok": False,
                "reason": "http_429",
                "detail": "http_429: Rate limit exceeded. Please try again later.",
            }
        return {"ok": True, "content": "ok", "model": f"opencode/{model_id}", "usage": None}

    monkeypatch.setattr(engine, "_call_model", fake_call)
    monkeypatch.setattr(
        engine,
        "_rotate_egress",
        lambda detail="": calls.__setitem__("rotate", calls["rotate"] + 1) or True,
    )

    result = engine.chat(user="u")

    assert result["ok"] is True
    assert calls["rotate"] == 1
    assert calls["models"] == chain_len + 1


def test_chat_does_not_rotate_without_gate(monkeypatch):
    """Обычный отказ модели не трогает ВПН: выход меняют только по гейту."""
    from modules import opencode_engine as engine

    monkeypatch.setenv("CLASSIFIER_ENGINE", "opencode")
    rotated = []
    monkeypatch.setattr(
        engine,
        "_call_model",
        lambda *a, **kw: {"ok": False, "reason": "model_error", "detail": "no route"},
    )
    monkeypatch.setattr(engine, "_rotate_egress", lambda detail="": rotated.append(detail) or True)

    result = engine.chat(user="u")

    assert result["ok"] is False
    assert result["reason"] == "engine_all_failed"
    assert rotated == []


def test_rotate_egress_respects_cooldown(monkeypatch, tmp_path):
    """Поворот не чаще паузы: иначе волна сожжёт все адреса подписки за час."""
    from modules import opencode_engine as engine

    class _Proc:
        returncode = 0
        stdout = "egress: AT -> DE\n"
        stderr = ""

    runs = []
    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_CMD", "rotate-egress --next")
    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_COOLDOWN", "600")
    monkeypatch.setattr(engine.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **kw: runs.append(a) or _Proc())

    assert engine._rotate_egress("gate") is True
    # Вторая попытка сразу — на паузе, команда не вызывается.
    assert engine._rotate_egress("gate") is False
    assert len(runs) == 1


def test_rotate_egress_disabled_without_command(monkeypatch):
    from modules import opencode_engine as engine

    monkeypatch.delenv("NEURAL_EGRESS_ROTATE_CMD", raising=False)
    assert engine._rotate_egress("gate") is False


# --- 24.10: уровень рассуждений, класс «квота аккаунта», экономия запросов ---

QUOTA_ERROR = (
    "http_429: Free usage limit reached. Subscribe to OpenCode Go to keep going:"
    " https://opencode.ai/go"
)


def test_call_model_passes_reasoning_variant(monkeypatch, engine_on):
    """``variant`` едет в ссылке модели: до 24.10 он не передавался вовсе."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "deepseek-v4.1-flash-free")
    transport = _use_transport(
        monkeypatch,
        [
            _session_route("ses_v"),
            _prompt_route(),
            _wait_route(),
            _message_route(_idle_msg(), _assistant_msg('{"verdicts": []}')),
            _delete_route("ses_v"),
        ],
    )

    result = chat(user="посты", label="variant")

    assert result["ok"] is True
    assert transport.calls[0]["body"]["model"] == {
        "id": "deepseek-v4.1-flash-free",
        "providerID": "opencode",
        "variant": "high",
    }


def test_reason_from_error_detects_account_quota():
    """Квота аккаунта и per-IP лимит приходят одним 429 — различаем по тексту."""
    from modules.opencode_engine import _reason_from_error

    assert _reason_from_error(QUOTA_ERROR) == "quota_exhausted"
    # per-IP лимит остаётся собой: у него другая реакция (смена выхода).
    assert _reason_from_error("http_429: Rate limit exceeded") == "http_429"


def test_chat_stops_chain_on_quota(monkeypatch, engine_on):
    """Квота аккаунта общая для всех моделей: цепочку не перебираем."""
    from modules import opencode_engine as engine

    monkeypatch.setenv(
        "OPENCODE_MODEL_CHAIN",
        "deepseek-v4.1-flash-free,glm-5.3-flash-free,mimo-v2.6-flash-free",
    )
    calls = {"models": 0, "rotate": 0, "mark": 0}

    def fake_call(model_id, prompt, label):
        calls["models"] += 1
        return {"ok": False, "reason": "quota_exhausted", "detail": QUOTA_ERROR}

    monkeypatch.setattr(engine, "_call_model", fake_call)
    monkeypatch.setattr(
        engine,
        "_rotate_egress",
        lambda detail="": calls.__setitem__("rotate", calls["rotate"] + 1) or True,
    )
    monkeypatch.setattr(engine, "_mark_quota", lambda: calls.__setitem__("mark", calls["mark"] + 1))

    result = engine.chat(user="u")

    assert result["ok"] is False
    assert result["quota"] is True
    # Одна модель на попытку вместо трёх: квота не про модель, и каждый лишний
    # перебор — сожжённый запрос в единственном слоте воркера.
    assert calls["models"] == 2, "цепочка перебиралась при мёртвой квоте"
    assert calls["rotate"] == 1, "поворот выхода для этого класса владелец оставил"
    assert calls["mark"] == 2, "метку квоты ставит каждая неудачная попытка"


def test_quota_mark_keeps_first_age_and_clears(monkeypatch, tmp_path):
    """Возраст метки = сколько квота мертва; повторный отказ его не обновляет."""
    from modules import opencode_engine as engine

    monkeypatch.setattr(engine.tempfile, "gettempdir", lambda: str(tmp_path))
    engine._mark_quota()
    path = tmp_path / engine._QUOTA_STAMP
    assert path.exists()
    first = path.read_text(encoding="utf-8")

    engine._mark_quota()
    assert path.read_text(encoding="utf-8") == first, "возраст метки перезаписан"

    engine._clear_quota_mark()
    assert not path.exists()
