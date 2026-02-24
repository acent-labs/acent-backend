"""Supabase JWT 인증 모듈."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

LOGGER = logging.getLogger(__name__)

# Supabase JWT 알고리즘
SUPABASE_JWT_ALGORITHM = "HS256"


@dataclass
class AuthUser:
    """인증된 사용자 정보."""

    id: str  # Supabase user ID (UUID)
    email: Optional[str] = None
    name: Optional[str] = None
    picture: Optional[str] = None  # Google profile picture URL

    @classmethod
    def from_jwt_payload(cls, payload: dict) -> "AuthUser":
        """JWT payload에서 사용자 정보 추출."""
        # Supabase JWT payload 구조:
        # {
        #   "sub": "user-uuid",
        #   "email": "user@example.com",
        #   "user_metadata": {"full_name": "...", "avatar_url": "..."}
        # }
        user_metadata = payload.get("user_metadata", {})
        return cls(
            id=payload.get("sub", ""),
            email=payload.get("email"),
            name=user_metadata.get("full_name") or user_metadata.get("name"),
            picture=user_metadata.get("avatar_url") or user_metadata.get("picture"),
        )


class JWTAuthError(Exception):
    """JWT 인증 에러."""

    pass


def verify_supabase_token(token: str) -> AuthUser:
    """Supabase JWT 토큰 검증.

    Args:
        token: Bearer 토큰 (JWT)

    Returns:
        인증된 사용자 정보

    Raises:
        JWTAuthError: 토큰 검증 실패 시
    """
    settings = get_settings()

    if not settings.supabase_jwt_secret:
        LOGGER.warning("SUPABASE_JWT_SECRET not configured, authentication disabled")
        raise JWTAuthError("Authentication not configured")

    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[SUPABASE_JWT_ALGORITHM],
            options={"verify_aud": False},  # Supabase doesn't use aud claim
        )
        return AuthUser.from_jwt_payload(payload)
    except jwt.ExpiredSignatureError:
        raise JWTAuthError("Token has expired")
    except jwt.InvalidTokenError as e:
        LOGGER.debug(f"Invalid JWT token: {e}")
        raise JWTAuthError("Invalid token")


# FastAPI dependency
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[AuthUser]:
    """현재 인증된 사용자 반환 (선택적).

    인증 헤더가 없으면 None 반환.
    """
    if not credentials:
        return None

    try:
        return verify_supabase_token(credentials.credentials)
    except JWTAuthError:
        return None


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> AuthUser:
    """인증 필수 - 인증되지 않으면 401 에러.

    모든 인증이 필요한 엔드포인트에서 사용.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return verify_supabase_token(credentials.credentials)
    except JWTAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )
