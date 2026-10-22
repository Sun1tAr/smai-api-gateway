import base64
import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from gateway.main import create_app
from gateway.tokens import ISSUER, JwtKeyError, TokenIssuer, load_private_key
from tests.conftest import write_key


def pem(key, password: bytes | None = None) -> bytes:
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )


@pytest.fixture
def issuer(jwt_key_file) -> TokenIssuer:
    clock = iter([1_700_000_000.9] * 100)
    return TokenIssuer(load_private_key(jwt_key_file), 3600, clock=lambda: next(clock))


def test_token_claims_header_and_signature(issuer: TokenIssuer) -> None:
    token = issuer.issue(42)
    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["typ"], header["kid"]) == ("ES256", "JWT", issuer.kid)
    claims = jwt.decode(
        token,
        issuer.public_key,
        algorithms=["ES256"],
        issuer=ISSUER,
        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    assert claims["sub"] == "42"
    assert (claims["iat"], claims["nbf"], claims["exp"]) == (
        1_700_000_000,
        1_700_000_000,
        1_700_003_600,
    )
    assert set(claims) == {"iss", "sub", "jti", "iat", "nbf", "exp"}


def test_every_token_has_a_unique_jti(issuer: TokenIssuer) -> None:
    tokens = [
        jwt.decode(issuer.issue(1), options={"verify_signature": False})["jti"]
        for _ in range(20)
    ]
    assert len(set(tokens)) == 20


def test_token_is_valid_now_and_expires_after_ttl(jwt_key_file) -> None:
    key = load_private_key(jwt_key_file)
    fresh = TokenIssuer(key, 60)
    decoded = jwt.decode(
        fresh.issue(7), fresh.public_key, algorithms=["ES256"], issuer=ISSUER
    )
    assert decoded["exp"] - decoded["iat"] == 60
    stale = TokenIssuer(key, 60, clock=lambda: time.time() - 3600)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(
            stale.issue(7), stale.public_key, algorithms=["ES256"], issuer=ISSUER
        )


def test_token_is_rejected_with_another_key_or_algorithm(
    issuer: TokenIssuer, tmp_path
) -> None:
    other = load_private_key(write_key(tmp_path / "other.pem")).public_key()
    token = issuer.issue(1)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other, algorithms=["ES256"], options={"verify_exp": False})
    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(token, issuer.public_key, algorithms=["HS256"])


def test_kid_is_the_rfc7638_thumbprint_of_the_public_key(issuer: TokenIssuer) -> None:
    numbers = issuer.public_key.public_numbers()

    def b64(value: int) -> str:
        raw = value.to_bytes(32, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    canonical = json.dumps(
        {"crv": "P-256", "kty": "EC", "x": b64(numbers.x), "y": b64(numbers.y)},
        separators=(",", ":"),
        sort_keys=True,
    )
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(canonical.encode()).digest()
    ).rstrip(b"=")
    assert issuer.kid == expected.decode()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (pem(rsa.generate_private_key(65537, 2048)), "EC P-256"),
        (pem(ec.generate_private_key(ec.SECP384R1())), "EC P-256"),
        (pem(ec.generate_private_key(ec.SECP256K1())), "EC P-256"),
        (pem(ed25519.Ed25519PrivateKey.generate()), "EC P-256"),
        (pem(ec.generate_private_key(ec.SECP256R1()), b"hunter2"), "password"),
        (b"", "PEM"),
        (
            b"-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n",
            "PEM",
        ),
        (b"plain text secret-material", "PEM"),
    ],
)
def test_unsuitable_keys_are_rejected_without_leaking_content(
    tmp_path, content: bytes, message: str
) -> None:
    path = tmp_path / "key.pem"
    path.write_bytes(content)
    with pytest.raises(JwtKeyError, match=message) as error:
        load_private_key(path)
    assert "secret-material" not in str(error.value) and "BEGIN" not in str(error.value)


def test_missing_key_file_is_a_clear_error(tmp_path) -> None:
    with pytest.raises(JwtKeyError, match="cannot be read"):
        load_private_key(tmp_path / "absent.pem")


def test_gateway_does_not_start_with_an_unsuitable_key(settings, tmp_path) -> None:
    path = tmp_path / "rsa.pem"
    path.write_bytes(pem(rsa.generate_private_key(65537, 2048)))
    with pytest.raises(JwtKeyError):
        create_app(settings.model_copy(update={"jwt_private_key_file": path}))
