"""
Multitenant Authentication Middleware

Validates requests from multiple frontend platforms (Freshdesk, Zendesk, Web, etc.)
using X-Tenant-ID, X-Platform, X-API-Key headers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

import httpx
from fastapi import HTTPException, Request, status

from app.models.metadata import MetadataFilter


@dataclass
class TenantContext:
    """Authenticated tenant context attached to each request."""
    tenant_id: str
    platform: str
    domain: Optional[str] = None
    verified: bool = False
    mandatory_filters: List[MetadataFilter] = field(default_factory=list)

    def get_mandatory_filters(self) -> List[MetadataFilter]:
        """Return mandatory filters that must be applied to all searches."""
        return self.mandatory_filters.copy()


# Supported platforms
SUPPORTED_PLATFORMS = {"freshdesk", "zendesk", "intercom", "web", "api"}


async def verify_freshdesk_api_key(domain: str, api_key: str) -> bool:
    """
    Verify Freshdesk API key by calling a lightweight endpoint.
    Returns True if the API key is valid for the given domain.
    """
    if not domain or not api_key:
        return False
    
    # Normalize domain
    if not domain.endswith(".freshdesk.com"):
        domain = f"{domain}.freshdesk.com"
    
    url = f"https://{domain}/api/v2/agents/me"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                auth=(api_key, "X"),  # Freshdesk uses API key as username
            )
            
            # 429 Too Many Requests is considered valid auth but rate limited
            if response.status_code == 429:
                return True
                
            return response.status_code == 200
    except httpx.RequestError:
        return False


async def verify_zendesk_api_key(domain: str, api_key: str) -> bool:
    """
    Verify Zendesk API key.
    """
    if not domain or not api_key:
        return False
    
    # Normalize domain
    if not domain.endswith(".zendesk.com"):
        domain = f"{domain}.zendesk.com"
    
    url = f"https://{domain}/api/v2/users/me.json"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return response.status_code == 200
    except httpx.RequestError:
        return False


async def verify_platform_api_key(platform: str, domain: str, api_key: str) -> bool:
    """
    Verify API key based on platform type.
    """
    if platform == "freshdesk":
        return await verify_freshdesk_api_key(domain, api_key)
    elif platform == "zendesk":
        return await verify_zendesk_api_key(domain, api_key)
    elif platform in ("web", "api"):
        # Web/API platforms use a different auth mechanism (e.g., JWT)
        # For now, just check if API key is provided
        return bool(api_key)
    else:
        return False


def extract_tenant_from_domain(domain: str, platform: str) -> str:
    """
    Extract tenant ID from domain.
    e.g., 'wedosoft.freshdesk.com' -> 'wedosoft'
    """
    if not domain:
        return ""
    
    # Remove protocol if present
    domain = domain.replace("https://", "").replace("http://", "")
    
    # Extract subdomain
    parts = domain.split(".")
    if len(parts) >= 1:
        return parts[0].lower()
    
    return domain.lower()


async def get_tenant_context(request: Request) -> TenantContext:
    """
    FastAPI dependency that extracts and validates tenant context from headers.
    
    Required headers:
    - X-Tenant-ID: Tenant identifier (e.g., 'wedosoft')
    - X-Platform: Platform type (e.g., 'freshdesk', 'zendesk', 'web')
    - X-API-Key: Platform-specific API key for verification
    
    Optional headers:
    - X-Domain: Full domain for API key verification
    """
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    platform = request.headers.get("X-Platform", "").strip().lower()
    api_key = request.headers.get("X-API-Key", "").strip()
    domain = request.headers.get("X-Domain", "").strip()
    
    # Validate required headers
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-ID header is required",
        )
    
    if not platform:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Platform header is required",
        )
    
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform: {platform}. Supported: {', '.join(SUPPORTED_PLATFORMS)}",
        )
    
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header is required",
        )
    
    # Use domain if provided, otherwise construct from tenant_id
    effective_domain = domain or f"{tenant_id}.{platform}.com"
    
    # Verify API key with platform
    verified = await verify_platform_api_key(platform, effective_domain, api_key)
    
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key for the specified platform and tenant",
        )
    
    # Build mandatory filters for data isolation
    mandatory_filters = [
        MetadataFilter(key="tenant_id", value=tenant_id, operator="EQUALS"),
        MetadataFilter(key="platform", value=platform, operator="EQUALS"),
    ]
    
    return TenantContext(
        tenant_id=tenant_id,
        platform=platform,
        domain=effective_domain,
        verified=True,
        mandatory_filters=mandatory_filters,
    )


async def get_optional_tenant_context(request: Request) -> Optional[TenantContext]:
    """
    Optional version of get_tenant_context.
    Returns None if tenant headers are not present, otherwise validates them.
    Use this for endpoints that support both authenticated and unauthenticated access.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    platform = request.headers.get("X-Platform", "").strip()
    
    if not tenant_id or not platform:
        return None
    
    # If headers are present, validate them fully
    return await get_tenant_context(request)


def require_tenant(*allowed_platforms: str) -> Callable:
    """
    Decorator factory to restrict endpoints to specific platforms.
    
    Usage:
        @router.post("/freshdesk-only")
        async def freshdesk_endpoint(
            tenant: TenantContext = Depends(require_tenant("freshdesk"))
        ):
            ...
    """
    async def dependency(request: Request) -> TenantContext:
        context = await get_tenant_context(request)
        
        if allowed_platforms and context.platform not in allowed_platforms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint is only available for platforms: {', '.join(allowed_platforms)}",
            )
        
        return context
    
    return dependency
