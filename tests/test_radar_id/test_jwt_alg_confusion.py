"""Refute-фаза аудита ЕСА #057: токен нельзя подделать сменой алгоритма.

Классическая уязвимость JWT — «algorithm confusion»: если сервер принимает
алгоритм из заголовка токена, не ограничивая его, атакующий может подписать
токен HS256, использовав ПУБЛИЧНЫЙ ключ как HMAC-секрет (сервер ведь отдаёт
его в ``/.well-known/jwks.json``). Плюс вариант ``alg=none`` — токен без
подписи вообще.

Проверка нужна, потому что ``service.userinfo`` и ``service.client_id_from_id_token_hint``
вызывают ``jwt.decode(token, get_public_jwks())`` — без явного списка
алгоритмов. В PyJWT такой вызов был бы дырой; в Authlib поведение другое, и
именно это здесь проверяется, а не assumed.

Токены подделываются здесь ЛОКАЛЬНО, на сгенерированном тестовом ключе —
никакого прод-касания.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import pytest
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from modules.radar_id import keys as keys_mod
from modules.radar_id import service


@pytest.fixture()
def rsa_key_env(tmp_path):
    """RS256 PEM во временном файле + env + сброс lru_cache."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "radar_id_rs256.pem"
    path.write_bytes(pem)
    keys_mod._load_private_jwk.cache_clear()
    with patch.dict(os.environ, {"RADAR_ID_PRIVATE_KEY_FILE": str(path)}):
        yield str(path)


def _public_pem() -> bytes:
    """Публичная часть ключа — ровно то, что отдаётся в jwks.json."""
    key = keys_mod.get_signing_key()
    pub = key.as_dict(is_private=False)
    return JsonWebKey.import_key(pub, {"kty": "RSA", "use": "sig", "alg": "RS256"}).as_pem()


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _forged_hs256(payload: dict, public_pem: bytes) -> str:
    """Токен, подписанный HS256 секретом = публичный ключ (алгоритм-путаница)."""
    import hashlib
    import hmac

    header = {"alg": "HS256", "typ": "JWT", "kid": keys_mod.get_kid()}
    head = _b64url(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    # HMAC-секрет — ПУБЛИЧНЫЙ ключ в PEM-байтах.
    sig = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def _forged_none(payload: dict) -> str:
    """Токен с alg=none — без подписи вовсе."""
    head = _b64url(json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    return f"{head}.{body}."


def _payload(**over) -> dict:
    now = int(time.time())
    base = {
        "iss": service.get_issuer(),
        "aud": service.get_issuer(),
        "sub": "u-1",
        "scope": "openid email profile",
        "iat": now,
        "exp": now + 300,
    }
    base.update(over)
    return base


def test_hs256_forgery_with_public_key_is_rejected(rsa_key_env):
    """Публичный ключ из jwks.json нельзя использовать как HMAC-секрет."""
    forged = _forged_hs256(_payload(), _public_pem())
    with pytest.raises(Exception):
        jwt.decode(forged, keys_mod.get_public_jwks())


def test_alg_none_token_is_rejected(rsa_key_env):
    """Токен без подписи не принимается."""
    forged = _forged_none(_payload())
    with pytest.raises(Exception):
        jwt.decode(forged, keys_mod.get_public_jwks())


def test_forged_token_does_not_pass_userinfo(rsa_key_env):
    """Подделка не проходит и через userinfo — точку, где токен живёт."""
    forged = _forged_hs256(_payload(), _public_pem())
    with pytest.raises(service.OidcError):
        import asyncio

        asyncio.run(service.userinfo(None, forged))


def test_forged_token_does_not_yield_client_id_on_logout(rsa_key_env):
    """Подделка не подставляет чужой client_id в logout."""
    forged = _forged_hs256(_payload(aud="чужой-клиент"), _public_pem())
    assert service.client_id_from_id_token_hint(forged) is None
