"""Admin auth (spec §4): a single shared bearer token until Phase 4.

Attach `Depends(require_admin)` to every /admin/* router. The token is read
from settings at request time so tests can swap it without re-importing.
"""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User

_bearer = HTTPBearer(auto_error=False)


async def verify_admin_user(db: AsyncSession) -> None:
    """Startup check: ADMIN_USER_ID, when set, must name an existing user
    with role 'admin'. Raises RuntimeError otherwise, so a typo or a
    deleted row surfaces at boot instead of as an FK error on the first
    approve."""
    admin_id = settings.admin_user_id
    if admin_id is None:
        return
    user = await db.get(User, admin_id)
    if user is None:
        raise RuntimeError(f"ADMIN_USER_ID {admin_id} is not a users.id")
    if user.role != "admin":
        raise RuntimeError(f"ADMIN_USER_ID {admin_id} has role {user.role!r}, expected 'admin'")


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
