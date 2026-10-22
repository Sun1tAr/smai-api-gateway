import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gateway.tokens import (
    ALGORITHM,
    ISSUER,
    InvalidToken,
    TokenIssuer,
    TokenVerifier,
    VerifiedToken,
    load_private_key,
)


@pytest.fixture
def key(jwt_key_file) -> ec.EllipticCurvePrivateKey:
    return load_private_key(jwt_key_file)


@pytest.fixture
def issuer(key) -> TokenIssuer:
    return TokenIssuer(key, 3600)


@pytest.fixture
def verifier(issuer: TokenIssuer) -> TokenVerifier:
    return TokenVerifier.for_issuer(issuer)


def claims(**override: object) -> dict:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "sub": "42",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    base.update(override)
    return {name: value for name, value in base.items() if value is not None}


def sign(key, payload: dict, kid: str | None, alg: str = ALGORITHM) -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


def reason(verifier: TokenVerifier, token: str) -> str:
    with pytest.raises(InvalidToken) as error:
        verifier.verify(token)
    return error.value.reason


def test_issued_token_is_accepted(issuer: TokenIssuer, verifier: TokenVerifier) -> None:
    token = issuer.issue(42)
    result = verifier.verify(token)
    claims_ = jwt.decode(token, options={"verify_signature": False})
    assert result == VerifiedToken(42, claims_["jti"], claims_["exp"])


def test_token_signed_by_another_key_is_rejected(issuer, verifier) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    assert reason(verifier, sign(other, claims(), issuer.kid)) == "signature"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_token_with_a_tampered_payload_is_rejected(issuer, verifier) -> None:
    head, _, tail = issuer.issue(42).split(".")
    forged = b64(json.dumps(claims(sub="1")).encode())
    assert reason(verifier, f"{head}.{forged}.{tail}") == "signature"


def test_alg_none_is_rejected(issuer, verifier) -> None:
    token = jwt.encode(claims(), None, algorithm="none", headers={"kid": issuer.kid})
    assert reason(verifier, token) == "algorithm"


def test_hs256_signed_with_the_public_key_is_rejected(issuer, verifier) -> None:
    # Классическая подмена: публичный ключ в роли HMAC-секрета. PyJWT такой токен
    # не подпишет, поэтому подпись считается вручную.
    public_pem = issuer.public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": issuer.kid}).encode())
    payload = b64(json.dumps(claims()).encode())
    mac = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256)
    assert reason(verifier, f"{header}.{payload}.{b64(mac.digest())}") == "algorithm"


def test_other_ec_algorithm_is_rejected(issuer, verifier) -> None:
    other = ec.generate_private_key(ec.SECP384R1())
    token = sign(other, claims(), issuer.kid, alg="ES384")
    assert reason(verifier, token) == "algorithm"


def test_expired_token_is_rejected(issuer, verifier, key) -> None:
    past = int(time.time()) - 10
    token = sign(key, claims(iat=past - 100, nbf=past - 100, exp=past), issuer.kid)
    assert reason(verifier, token) == "expired"


def test_token_expiring_this_second_is_rejected_without_leeway(
    issuer, verifier, key
) -> None:
    now = int(time.time())
    token = sign(key, claims(exp=now), issuer.kid)
    assert reason(verifier, token) == "expired"


def test_token_valid_only_in_the_future_is_rejected(issuer, verifier, key) -> None:
    future = int(time.time()) + 600
    token = sign(key, claims(nbf=future, exp=future + 60), issuer.kid)
    assert reason(verifier, token) == "not_yet_valid"


@pytest.mark.parametrize("missing", ["exp", "iat", "nbf", "jti", "sub"])
def test_token_without_a_required_claim_is_rejected(
    issuer, verifier, key, missing: str
) -> None:
    token = sign(key, claims(**{missing: None}), issuer.kid)
    assert reason(verifier, token) == "claims"


def test_token_without_issuer_or_with_another_one_is_rejected(
    issuer, verifier, key
) -> None:
    assert reason(verifier, sign(key, claims(iss=None), issuer.kid)) == "claims"
    assert reason(verifier, sign(key, claims(iss="evil"), issuer.kid)) == "claims"


def test_unknown_or_missing_kid_is_rejected(issuer, verifier, key) -> None:
    assert reason(verifier, sign(key, claims(), "other")) == "kid"
    assert reason(verifier, sign(key, claims(), None)) == "kid"


@pytest.mark.parametrize(
    "subject", ["0", "-1", "abc", "01", "1.5", "", " 1", "1 ", "1e3", 42, None, True]
)
def test_bad_subject_is_rejected(issuer, verifier, key, subject: object) -> None:
    payload = claims()
    payload["sub"] = subject
    assert reason(verifier, sign(key, payload, issuer.kid)) == "claims"


@pytest.mark.parametrize(
    "jti",
    [
        "not-a-uuid",
        "",
        str(uuid.uuid4()).upper(),
        uuid.uuid4().hex,
        "{" + str(uuid.uuid4()) + "}",
        "urn:uuid:" + str(uuid.uuid4()),
        42,
        None,
    ],
)
def test_jti_must_be_a_canonical_uuid(issuer, verifier, key, jti: object) -> None:
    payload = claims()
    payload["jti"] = jti
    assert reason(verifier, sign(key, payload, issuer.kid)) == "claims"


@pytest.mark.parametrize("exp_kind", ["float", "bool"])
def test_exp_must_be_an_integer(issuer, verifier, key, exp_kind: str) -> None:
    exp = time.time() + 3600.5 if exp_kind == "float" else True
    payload = claims()
    payload["exp"] = exp
    assert reason(verifier, sign(key, payload, issuer.kid)) in ("claims", "expired")


@pytest.mark.parametrize(
    "token",
    ["", "x", "a.b", "a.b.c", "....", "Bearer x.y.z", "é.é.é", "\x00.\x00.\x00"],
)
def test_garbage_is_malformed(verifier, token: str) -> None:
    assert reason(verifier, token) == "malformed"


def test_verifier_never_raises_anything_but_invalid_token(
    issuer, verifier, key
) -> None:
    token = issuer.issue(7)
    head, payload, signature = token.split(".")
    for broken in (
        f"{head}.{payload}.",
        f"{head}.{payload}.{signature[:-4]}",
        f"{head}..{signature}",
        f"{payload}.{head}.{signature}",
    ):
        with pytest.raises(InvalidToken):
            verifier.verify(broken)
