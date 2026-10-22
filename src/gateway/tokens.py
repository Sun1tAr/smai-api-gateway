import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

ALGORITHM = "ES256"
ISSUER = "smai-gateway"


class JwtKeyError(ValueError):
    """Ключ подписи непригоден; сервис не стартует. Содержимое файла не раскрывается."""


def load_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        data = path.read_bytes()
    except OSError:
        raise JwtKeyError("JWT_PRIVATE_KEY_FILE cannot be read") from None
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except TypeError:
        raise JwtKeyError(
            "JWT_PRIVATE_KEY_FILE must not be password protected"
        ) from None
    except (ValueError, UnsupportedAlgorithm):
        raise JwtKeyError("JWT_PRIVATE_KEY_FILE is not a PEM private key") from None
    # Алгоритм задан явно (ES256): ключ другого типа или кривой подпись не подменит.
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise JwtKeyError("JWT_PRIVATE_KEY_FILE must be an EC P-256 key")
    return key


# RFC 7638: хэш канонического JWK. Заголовок kid позволит перейти на новый ключ,
# не ломая токены, подписанные старым.
def key_thumbprint(public_key: ec.EllipticCurvePublicKey) -> str:
    jwk = json.loads(ECAlgorithm.to_jwk(public_key))
    canonical = json.dumps(
        {name: jwk[name] for name in ("crv", "kty", "x", "y")},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class TokenIssuer:
    """Подписывает JWT приватным ключом gateway; проверка токена здесь не нужна."""

    def __init__(
        self,
        key: ec.EllipticCurvePrivateKey,
        ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = key
        self._ttl = ttl_seconds
        self._clock = clock
        self.public_key = key.public_key()
        self.kid = key_thumbprint(self.public_key)

    @classmethod
    def from_file(cls, path: Path, ttl_seconds: int) -> "TokenIssuer":
        return cls(load_private_key(path), ttl_seconds)

    def issue(self, user_id: int) -> str:
        issued_at = int(self._clock())
        claims = {
            "iss": ISSUER,
            "sub": str(user_id),
            "jti": str(uuid.uuid4()),
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + self._ttl,
        }
        return jwt.encode(
            claims, self._key, algorithm=ALGORITHM, headers={"kid": self.kid}
        )
