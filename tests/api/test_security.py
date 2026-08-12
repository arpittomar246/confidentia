"""Token verification and the role ladder.

The tests that matter here are the refusals. A verifier that accepts good tokens is
easy; the defects that get shipped are the ones where it also accepts a token that
was expired, minted for another service, or signed with the wrong algorithm.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from confidentia.api.security import (
    LOCAL_AUDIENCE,
    LOCAL_ISSUER,
    AuthError,
    Principal,
    Role,
    TokenVerifier,
    _parse_roles,
    issue_local_token,
)
from confidentia.config.settings import AuthMode, AuthSettings, Settings

pytestmark = pytest.mark.unit


def _settings(**auth: object) -> Settings:
    settings = Settings()
    settings.auth = AuthSettings(**auth)  # type: ignore[arg-type]
    return settings


# --------------------------------------------------------------- role ladder


def _principal(*roles: Role) -> Principal:
    return Principal(subject="s", roles=frozenset(roles), issuer="i", expires_at=0)


def test_role_ladder_is_transitive_upwards() -> None:
    assert _principal(Role.AUDITOR).may(Role.VIEWER)
    assert _principal(Role.OPERATOR).may(Role.VIEWER)
    assert not _principal(Role.VIEWER).may(Role.OPERATOR)
    assert not _principal(Role.OPERATOR).may(Role.AUDITOR)


def test_no_roles_satisfies_nothing() -> None:
    """A token with an empty or entirely unrecognised role claim is not a viewer."""
    assert not _principal().may(Role.VIEWER)


# --------------------------------------------------------------- role claims


def test_roles_read_from_a_dotted_claim_path() -> None:
    claims = {"realm_access": {"roles": ["operator", "offline_access"]}}
    assert _parse_roles(claims, "realm_access.roles") == frozenset({Role.OPERATOR})


def test_space_delimited_role_claim() -> None:
    assert _parse_roles({"roles": "viewer auditor"}, "roles") == frozenset(
        {Role.VIEWER, Role.AUDITOR}
    )


def test_unknown_roles_are_dropped_not_fatal() -> None:
    """Providers attach roles for other applications to the same token."""
    assert _parse_roles({"roles": ["viewer", "salesforce_admin"]}, "roles") == frozenset(
        {Role.VIEWER}
    )


def test_missing_claim_yields_no_roles() -> None:
    assert _parse_roles({"sub": "x"}, "roles") == frozenset()


# --------------------------------------------------------------- local mode


def test_local_token_round_trip() -> None:
    settings = _settings()
    token = issue_local_token("arpit", [Role.OPERATOR], settings=settings)
    principal = TokenVerifier(settings).verify(token)
    assert principal.subject == "arpit"
    assert principal.roles == frozenset({Role.OPERATOR})
    assert principal.issuer == LOCAL_ISSUER


def test_expired_token_is_refused() -> None:
    settings = _settings(leeway_seconds=0)
    now = int(time.time())
    token = jwt.encode(
        {"sub": "x", "iss": LOCAL_ISSUER, "aud": LOCAL_AUDIENCE, "exp": now - 60, "roles": []},
        settings.auth.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(AuthError, match="expired"):
        TokenVerifier(settings).verify(token)


def test_token_for_another_audience_is_refused() -> None:
    """A valid signature from the right issuer is not enough."""
    settings = _settings()
    token = jwt.encode(
        {
            "sub": "x",
            "iss": LOCAL_ISSUER,
            "aud": "some-other-service",
            "exp": int(time.time()) + 60,
            "roles": ["auditor"],
        },
        settings.auth.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        TokenVerifier(settings).verify(token)


def test_token_signed_with_the_wrong_secret_is_refused() -> None:
    settings = _settings()
    token = jwt.encode(
        {
            "sub": "x",
            "iss": LOCAL_ISSUER,
            "aud": LOCAL_AUDIENCE,
            "exp": int(time.time()) + 60,
        },
        "a-different-secret-that-is-also-long-enough",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        TokenVerifier(settings).verify(token)


def test_token_without_a_subject_is_refused() -> None:
    """``sub`` is in the required claim list; without it there is no principal."""
    settings = _settings()
    token = jwt.encode(
        {"iss": LOCAL_ISSUER, "aud": LOCAL_AUDIENCE, "exp": int(time.time()) + 60},
        settings.auth.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        TokenVerifier(settings).verify(token)


# ---------------------------------------------------------------- OIDC mode


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _oidc_settings() -> Settings:
    return _settings(
        mode=AuthMode.OIDC,
        oidc_issuer="https://idp.example.test/",
        oidc_audience="confidentia",
        oidc_jwks_url="https://idp.example.test/jwks",
    )


def _forge_hs256(claims: dict[str, object], *, secret: bytes) -> str:
    """Build an HS256 token without PyJWT, the way an attacker would."""
    import base64
    import hashlib
    import hmac
    import json

    def segment(data: dict[str, object]) -> bytes:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = segment({"alg": "HS256", "typ": "JWT"}) + b"." + segment(claims)
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _verifier_for(key: object) -> TokenVerifier:
    """A verifier whose JWKS lookup always returns ``key``.

    Injected rather than served over HTTP: the property under test is which key and
    which algorithms ``jwt.decode`` is called with, not whether PyJWT can fetch a
    document.
    """
    client = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=key))
    return TokenVerifier(_oidc_settings(), jwk_client=client)  # type: ignore[arg-type]


def test_oidc_token_is_accepted_and_roles_extracted(rsa_key: rsa.RSAPrivateKey) -> None:
    token = jwt.encode(
        {
            "sub": "svc-dashboard",
            "iss": "https://idp.example.test/",
            "aud": "confidentia",
            "exp": int(time.time()) + 300,
            "roles": ["viewer"],
        },
        rsa_key,
        algorithm="RS256",
    )
    principal = _verifier_for(rsa_key.public_key()).verify(token)
    assert principal.subject == "svc-dashboard"
    assert principal.roles == frozenset({Role.VIEWER})


def test_algorithm_confusion_is_refused(rsa_key: rsa.RSAPrivateKey) -> None:
    """The attack this whole design is arranged around.

    The JWKS document is public. An attacker takes the RSA **public** key, uses its
    PEM bytes as an HMAC secret, and signs an auditor token with HS256. A verifier
    that accepts both RS256 and HS256 validates it, because the key it looks up is
    the same public key. The permitted-algorithm list is asymmetric-only, so this
    must fail.
    """
    from cryptography.hazmat.primitives import serialization

    public_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # Forged by hand. `jwt.encode` refuses to use a PEM as an HMAC secret, which is
    # a guard on the *signing* side -- an attacker is not using PyJWT to sign, so a
    # test that relies on it would be testing the wrong library's defences.
    forged = _forge_hs256(
        {
            "sub": "attacker",
            "iss": "https://idp.example.test/",
            "aud": "confidentia",
            "exp": int(time.time()) + 300,
            "roles": ["auditor"],
        },
        secret=public_pem,
    )
    with pytest.raises(AuthError):
        _verifier_for(rsa_key.public_key()).verify(forged)


def test_oidc_token_from_another_issuer_is_refused(rsa_key: rsa.RSAPrivateKey) -> None:
    token = jwt.encode(
        {
            "sub": "x",
            "iss": "https://evil.example.test/",
            "aud": "confidentia",
            "exp": int(time.time()) + 300,
        },
        rsa_key,
        algorithm="RS256",
    )
    with pytest.raises(AuthError):
        _verifier_for(rsa_key.public_key()).verify(token)


def test_minting_is_refused_in_oidc_mode() -> None:
    """The API holds no signing key when an identity provider is in charge."""
    with pytest.raises(ValueError, match="holds no signing key"):
        issue_local_token("x", [Role.VIEWER], settings=_oidc_settings())


# ------------------------------------------------------------ configuration


def test_oidc_without_issuer_or_audience_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="AUTH_OIDC_ISSUER"):
        AuthSettings(mode=AuthMode.OIDC, oidc_jwks_url="https://x/jwks")


def test_local_mode_is_refused_outside_the_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the deployed API both issues and verifies its own tokens."""
    monkeypatch.setenv("CONFIDENTIA_ENV", "cloud")
    with pytest.raises(ValueError, match="AUTH_MODE=local is refused"):
        AuthSettings(mode=AuthMode.LOCAL)


def test_default_dev_secret_is_long_enough_for_hs256() -> None:
    """Below 32 bytes PyJWT warns on every token, which trains people to ignore it."""
    assert len(AuthSettings().jwt_secret.get_secret_value().encode()) >= 32
