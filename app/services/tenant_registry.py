from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Dict

from fastapi import HTTPException, status

from app.models.tenant import TenantConfig

CONFIG_ENV = "AGENT_PLATFORM_TENANT_CONFIG"
CONFIG_PATH_ENV = "AGENT_PLATFORM_TENANT_CONFIG_PATH"


class TenantRegistry:
    def __init__(self) -> None:
        self._tenants: Dict[str, TenantConfig] = {}
        self._load()

    def _load(self) -> None:
        raw_config = os.getenv(CONFIG_ENV)
        config_source = "env"
        if not raw_config:
            path = os.getenv(CONFIG_PATH_ENV)
            if path:
                full_path = os.path.abspath(os.path.expanduser(path))
                config_source = full_path
                try:
                    with open(full_path, "r", encoding="utf-8") as fp:
                        raw_config = fp.read()
                except OSError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"테넌트 설정 파일을 읽는 중 오류가 발생했습니다: {exc}",
                    ) from exc
        if not raw_config:
            self._tenants = {}
            return
        try:
            payload = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"테넌트 설정 JSON 파싱 실패 ({config_source})",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="테넌트 설정은 객체(JSON) 형태여야 합니다.",
            )
        tenants: Dict[str, TenantConfig] = {}
        for tenant_id, tenant_config in payload.items():
            if not isinstance(tenant_config, dict):
                continue
            tenant_config["id"] = tenant_id
            tenants[tenant_id] = TenantConfig(**tenant_config)
        self._tenants = tenants

    def get(self, tenant_id: str) -> TenantConfig:
        try:
            return self._tenants[tenant_id]
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="테넌트를 찾을 수 없습니다.") from exc

    def list(self) -> Dict[str, TenantConfig]:
        return self._tenants


@lru_cache
def get_tenant_registry() -> TenantRegistry:
    return TenantRegistry()
