"""Admin auth (spec §4): a single shared bearer token until Phase 4.

Attach `Depends(require_admin)` to every /admin/* router. The token is read
from settings at request time so tests can swap it without re-importing.
"""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_bearer = HTTPBearer(auto_error=False)


def require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="admin bearer token required",
        headers={"WWW-Authenticate": "Bearer"},
    )
    expected = settings.admin_token
    if not expected:
        # Fail closed: an unconfigured token must never mean "open".
        raise unauthorized
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise unauthorized
