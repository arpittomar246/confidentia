"""Bearer-token verification and role-based access control.

Three things here are security-relevant and each is a deliberate choice.

**The API never mints tokens in a deployed environment.** In ``oidc`` mode it
verifies signatures against an external provider's JWKS and holds no signing key,
so compromising the API does not let an attacker issue itself an auditor token.
``local`` mode -- where the verifier is also the issuer -- is refused outside
``CONFIDENTIA_ENV=local`` by ``AuthSettings``, not here, so the failure happens at
startup rather than at the first request.

**The permitted algorithm list is closed and asymmetric-only in OIDC mode.**
Accepting HS256 alongside RS256 is the algorithm-confusion attack: the JWKS
document is public, so an attacker takes the RSA public key, uses its bytes as an
HMAC secret, and signs a token the verifier accepts. ``jwt.decode`` is therefore
always called with an explicit ``algorithms`` list drawn from settings, and
settings refuse to put a symmetric algorithm in it.

**Issuer and audience are always checked.** A signature proves who signed a token,
not who it was for. Without an audience check, a token minted by the same provider
for a different service is accepted here.

Roles form a linear disclosure ladder -- viewer < operator < auditor -- because
this API is read-only, so the only thing a role can decide is how much a caller may
see. When trigger endpoints exist, ``operator`` gains a capability that is not on
that ladder and this becomes a set membership test instead. It is written as
``_RANK`` rather than as ``>=`` on the enum so that change is one edit.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from confidentia.api.errors import ApiError
from confidentia.common.logging import get_logger
from confidentia.config.settings import AuthMode, Settings, get_settings

logger = get_logger(__name__)

LOCAL_ISSUER = "confidentia-local"
LOCAL_AUDIENCE = "confidentia-api"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    AUDITOR = "auditor"


# Higher rank sees everything a lower rank sees.
_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.OPERATOR: 2, Role.AUDITOR: 3}


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. Carries no token and no key material."""

    subject: str
    roles: frozenset[Role]
    issuer: str
    expires_at: int

    @property
    def rank(self) -> int:
        return max((_RANK[r] for r in self.roles), default=0)

    def may(self, required: Role) -> bool:
        return self.rank >= _RANK[required]


class AuthError(ApiError):
    status_code = 401
    code = "unauthenticated"


class ForbiddenError(ApiError):
    status_code = 403
    code = "forbidden"


# auto_error=False so a missing header produces this module's error shape rather
# than FastAPI's, which would bypass the audit log below.
_bearer = HTTPBearer(auto_error=False, description="OIDC access token")


def _claim_path(claims: dict[str, Any], path: str) -> Any:
    """Resolve a dotted claim path. Keycloak nests roles under realm_access.roles."""
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _parse_roles(claims: dict[str, Any], claim_path: str) -> frozenset[Role]:
    """Read the role claim, discarding values this platform does not define.

    An unknown role is dropped rather than rejected: identity providers routinely
    attach roles for other applications to the same token, and refusing the whole
    token because of one would make the API unusable in any shared tenant. A token
    whose roles are *all* unknown ends up with an empty set and is refused by the
    role check, which is the correct outcome.
    """
    raw = _claim_path(claims, claim_path)
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list | tuple):
        return frozenset()
    known = {r.value for r in Role}
    return frozenset(Role(v) for v in raw if isinstance(v, str) and v in known)


@lru_cache(maxsize=4)
def _jwk_client(url: str, lifespan: int) -> jwt.PyJWKClient:
    """Cached JWKS client.

    Cached because a provider's signing keys rotate on the order of days while the
    API serves many requests a second, and an uncached client fetches the document
    on every single verification -- turning the identity provider into a hard
    dependency of every request and a trivial denial-of-service amplifier.
    """
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=lifespan)


class TokenVerifier:
    """Verifies a bearer token and returns the principal it names.

    Constructed per application rather than per request so the JWKS cache is shared.
    ``jwk_client`` is injectable purely so tests can serve a JWKS document without
    a network listener.
    """

    def __init__(self, settings: Settings, *, jwk_client: jwt.PyJWKClient | None = None) -> None:
        self._settings = settings
        self._injected_client = jwk_client

    @property
    def mode(self) -> AuthMode:
        return self._settings.auth.mode

    def _client(self) -> jwt.PyJWKClient:
        if self._injected_client is not None:
            return self._injected_client
        auth = self._settings.auth
        return _jwk_client(auth.oidc_jwks_url, auth.jwks_cache_seconds)

    def verify(self, token: str) -> Principal:
        auth = self._settings.auth
        if auth.mode is AuthMode.LOCAL:
            key: Any = auth.jwt_secret.get_secret_value()
            algorithms = [auth.jwt_algorithm]
            issuer, audience = LOCAL_ISSUER, LOCAL_AUDIENCE
            roles_claim = "roles"
        elif auth.mode is AuthMode.OIDC:
            try:
                key = self._client().get_signing_key_from_jwt(token).key
            except jwt.PyJWKClientError as exc:
                # A key that is not in the JWKS is an untrusted token, not an
                # outage. Distinguished from the InvalidTokenError below only so
                # the log says which.
                raise AuthError(f"no signing key matches this token: {exc}") from exc
            algorithms = list(auth.oidc_algorithms)
            issuer, audience = auth.oidc_issuer, auth.oidc_audience
            roles_claim = auth.oidc_roles_claim
        else:  # pragma: no cover - StrEnum is exhaustive; fail closed regardless
            raise AuthError(f"unsupported auth mode {auth.mode!r}")

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=algorithms,
                issuer=issuer,
                audience=audience,
                leeway=auth.leeway_seconds,
                options={
                    # Named explicitly. PyJWT's defaults have changed across
                    # versions, and "verify_exp defaults to True" is not something
                    # an authentication path should be inferring from a changelog.
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "require": ["exp", "iss", "aud", "sub"],
                },
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError(str(exc)) from exc

        return Principal(
            subject=str(claims["sub"]),
            roles=_parse_roles(claims, roles_claim),
            issuer=str(claims["iss"]),
            expires_at=int(claims["exp"]),
        )


def issue_local_token(
    subject: str,
    roles: list[Role] | list[str],
    *,
    settings: Settings | None = None,
    ttl_minutes: int | None = None,
) -> str:
    """Mint a development token. Only meaningful in ``local`` auth mode.

    Refuses in any other mode rather than producing a token nothing will accept.
    """
    settings = settings or get_settings()
    auth = settings.auth
    if auth.mode is not AuthMode.LOCAL:
        raise ValueError(
            f"cannot mint tokens in {auth.mode.value!r} mode: tokens are issued by "
            "the identity provider, and this process holds no signing key."
        )
    now = int(time.time())
    payload = {
        "sub": subject,
        "iss": LOCAL_ISSUER,
        "aud": LOCAL_AUDIENCE,
        "iat": now,
        "exp": now + (ttl_minutes or auth.access_token_minutes) * 60,
        "roles": [str(r) for r in roles],
    }
    return jwt.encode(payload, auth.jwt_secret.get_secret_value(), algorithm=auth.jwt_algorithm)


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    verifier: TokenVerifier = request.app.state.verifier
    if credentials is None or not credentials.credentials:
        raise AuthError("missing bearer token")
    principal = verifier.verify(credentials.credentials)
    # Subject and roles only. A token, even an expired one, is never logged.
    logger.debug("authenticated", subject=principal.subject, roles=sorted(principal.roles))
    return principal


def require_role(required: Role) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory guarding a route at ``required`` or above."""

    async def _guard(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.may(required):
            logger.info(
                "authorisation_denied",
                subject=principal.subject,
                roles=sorted(principal.roles),
                required=str(required),
            )
            raise ForbiddenError(
                f"role {required.value!r} or higher is required; "
                f"this token carries {sorted(r.value for r in principal.roles) or 'no known role'}"
            )
        return principal

    # Tagged so that tooling can report which role guards a route by reading the
    # real dependency tree. Without it the only way to answer that is a
    # hand-maintained table, which is a table that goes stale silently.
    _guard.required_role = required  # type: ignore[attr-defined]
    return _guard


ViewerDep = Depends(require_role(Role.VIEWER))
OperatorDep = Depends(require_role(Role.OPERATOR))
AuditorDep = Depends(require_role(Role.AUDITOR))
