import logging
import warnings
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import get_api_router
from app.core.config import get_settings
from app.middleware.legacy_observability import LegacyRouteObservabilityMiddleware
from app.middleware.request_id import RequestIdLogFilter, RequestIdMiddleware
from app.services.scheduler_service import get_scheduler_service


logger = logging.getLogger(__name__)
settings = get_settings()


# =============================================================================
# Sentry PII Scrubbing (ported from nexus-ai)
# =============================================================================

def _scrub_pii(event: dict, hint: dict) -> dict:
    """Sentry before_send hook: scrub PII and conversation data."""
    # Request body scrubbing
    if "request" in event and "data" in event["request"]:
        data = event["request"]["data"]
        if isinstance(data, dict):
            for field in [
                "conversation", "conversations", "subject",
                "approved_response", "feedback_text", "body_text",
            ]:
                if field in data:
                    data[field] = "[SCRUBBED]"
        elif isinstance(data, str):
            event["request"]["data"] = "[SCRUBBED_BODY]"

    # Request header scrubbing
    if "request" in event and "headers" in event["request"]:
        headers = event["request"]["headers"]
        if isinstance(headers, dict):
            for key in ["Authorization", "X-Api-Key", "X-Freshdesk-API-Key", "Cookie"]:
                for k in [key, key.lower()]:
                    if k in headers:
                        headers[k] = "[REDACTED]"

    # Exception local variable scrubbing
    if "exception" in event and "values" in event["exception"]:
        for exc in event["exception"]["values"]:
            if "stacktrace" in exc and "frames" in exc["stacktrace"]:
                for frame in exc["stacktrace"]["frames"]:
                    if "vars" in frame:
                        for var_name in list(frame["vars"].keys()):
                            if any(
                                s in var_name.lower()
                                for s in [
                                    "conversation", "subject", "prompt",
                                    "text", "body", "api_key",
                                ]
                            ):
                                frame["vars"][var_name] = "[SCRUBBED]"

    return event


# Initialize Sentry if configured
if settings.sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            before_send=_scrub_pii,
            send_default_pii=False,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
            ],
        )
        logger.info("Sentry initialized (env=%s)", settings.sentry_environment)
    except ImportError:
        logger.warning("sentry-sdk not installed; Sentry disabled")

# Configure logging
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(request_id)s - %(message)s",
)
logging.getLogger("app").setLevel(settings.log_level)
for handler in logging.getLogger().handlers:
    handler.addFilter(RequestIdLogFilter())

# Silence noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
logging.getLogger("openai").setLevel(logging.WARNING)


# Silence noisy Pydantic warnings (keep logs readable during perf debugging)
try:
    # Pydantic v2 warning class
    from pydantic.warnings import UnsupportedFieldAttributeWarning  # type: ignore

    warnings.filterwarnings("ignore", category=UnsupportedFieldAttributeWarning)
except Exception:
    # Fallback by message match (safe even if class name changes)
    warnings.filterwarnings(
        "ignore",
        message=r".*UnsupportedFieldAttributeWarning.*",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 시작/종료 시 스케줄러 관리"""
    # Startup
    scheduler = get_scheduler_service()
    scheduler.start()
    logger.info("Scheduler started")
    
    yield
    
    # Shutdown
    scheduler.shutdown()
    logger.info("Scheduler stopped")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# CORS 설정 수정: FDK 로컬 개발 환경 및 ngrok 지원
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:10001",  # FDK Local
        "http://localhost:8000",   # Backend Local
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:5173",   # Vite 프론트엔드(온보딩)
    ],
    allow_origin_regex="https?://.*",  # 모든 Origin 허용 (개발 편의성)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(LegacyRouteObservabilityMiddleware)
app.add_middleware(RequestIdMiddleware)
app.include_router(get_api_router())


@app.get("/")
def root() -> dict:
    return {"message": "Agent Platform backend", "api_prefix": settings.api_prefix}
