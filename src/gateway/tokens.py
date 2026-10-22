import base64
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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


_SUBJECT = re.compile(r"[1-9][0-9]{0,9}")
# Колонка users.id в MS-3 — int32: больший sub там не существует.
_MAX_USER_ID = 2**31 - 1
_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "jti", "sub"]


class InvalidToken(Exception):
    """Токен не прошёл проверку. `reason` идёт только в метрику и лог."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedToken:
    user_id: int
    jti: str
    exp: int


class TokenVerifier:
    """Проверяет JWT, выданный TokenIssuer, по его публичному ключу."""

    def __init__(self, public_key: ec.EllipticCurvePublicKey, kid: str) -> None:
        self._key = public_key
        self._kid = kid

    @classmethod
    def for_issuer(cls, issuer: TokenIssuer) -> "TokenVerifier":
        return cls(issuer.public_key, issuer.kid)

    def verify(self, token: str) -> VerifiedToken:
        # Алгоритм задан списком из одного значения: alg=none и HS256 с публичным
        # ключом в роли секрета отклоняются самой библиотекой.
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidToken("malformed") from None
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("algorithm")
        if header.get("kid") != self._kid:
            raise InvalidToken("kid")
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                issuer=ISSUER,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError:
            raise InvalidToken("expired") from None
        except jwt.ImmatureSignatureError:
            raise InvalidToken("not_yet_valid") from None
        except jwt.InvalidSignatureError:
            raise InvalidToken("signature") from None
        except jwt.PyJWTError:
            raise InvalidToken("claims") from None
        subject, jti, exp = claims["sub"], claims["jti"], claims["exp"]
        if (
            not isinstance(subject, str)
            or not _SUBJECT.fullmatch(subject)
            or int(subject) > _MAX_USER_ID
        ):
            raise InvalidToken("claims")
        # jti — ключ в Redis: только каноничный UUID, чужие формы не принимаем.
        try:
            canonical = isinstance(jti, str) and str(uuid.UUID(jti)) == jti
        except ValueError:
            canonical = False
        if not canonical or isinstance(exp, bool) or not isinstance(exp, int):
            raise InvalidToken("claims")
        return VerifiedToken(user_id=int(subject), jti=jti, exp=exp)
