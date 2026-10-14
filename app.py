import logging
import os
import time
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from api.app import AppAPI
from api.chat import ChatAPI
from api.knowledgebases import KnowledgeBasesAPI
from services.db import DB
from services.auth import Auth
import traceback
from api.admin_auth import AuthAPI
from api.password_reset import PasswordResetAPI
from contextlib import asynccontextmanager
import asyncio
import uuid
from api.speech import SpeechAPI
from io import BytesIO
from fastapi.middleware.cors import CORSMiddleware
from api.feedback import FeedbackAPI
from api.email_verification import EmailVerificationAPI
from api.chat_feedback import ChatFeedbackAPI
from api.referral import ReferralAPI
from api.consultation import ConsultationAPI
from api.websocket_handler import WebSocketAPI
from api.inbox import InboxAPI
from api.identity_verification import IdentityVerificationAPI
from api.admin import AdminAPI
from api.product_analytics import ProductAnalyticsAPI
from services.models import User
from services.persona_service import require_identity_verification_for_feature
from services.logging_config import configure_logging
from dotenv import load_dotenv

# Import scheduler
try:
    from jobs import start_scheduler, stop_scheduler
    SCHEDULER_AVAILABLE = True
except ImportError as e:
    SCHEDULER_AVAILABLE = False
    logging.warning(f"Scheduler not available: {e}")



############################################
# Variables                                #
############################################

# Load environment variables from .env before services initialize
load_dotenv()

# Configure structured JSON logging for DigitalOcean/OpenSearch forwarding
configure_logging()

# App name.
app_name = "lawneeds"

# Volume path.
volume_path = "/Users/ibrahimadubi/Downloads/lawneeds/LawNeeds/volume/" + app_name + "/volume/"
# volume_path = "/lawneeds/volume/" + app_name + "/volume/"

logger = logging.getLogger(__name__)  # Create a logger for the current module

############################################
# App services (initialized before lifespan)
############################################

try:
    db = DB(app_name)  # Pass app_name instead of volume_path
    auth = Auth(app_name=app_name, db=db)
    
except Exception as e:
    logger.error("Error initializing database or services: %s", e)
    raise RuntimeError("Database initialization failed")

# Create the database engine
try:
    create_db_engine = db.create_db_engine()
except Exception as e:
    logger.error("Error creating database engine: %s", e)
    raise RuntimeError("Database engine creation failed")

############################################
# Lifespan (startup/shutdown)              #
############################################

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Handles startup and shutdown events.
    """
    # STARTUP
    logger.info("Application starting up...")
    
    # Start the subscription scheduler if available and enabled
    scheduler_enabled = os.getenv('SCHEDULER_ENABLED', 'true').lower() == 'true'
    
    if SCHEDULER_AVAILABLE and scheduler_enabled:
        try:
            start_scheduler(db, consultation_api)
            logger.info("✅ Subscription scheduler started")
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")
    else:
        if not SCHEDULER_AVAILABLE:
            logger.warning("Scheduler module not available")
        else:
            logger.info("Scheduler disabled via SCHEDULER_ENABLED env var")
    
    yield  # Application runs here
    
    # SHUTDOWN
    logger.info("Application shutting down...")
    
    if SCHEDULER_AVAILABLE and scheduler_enabled:
        try:
            stop_scheduler()
            logger.info("✅ Subscription scheduler stopped")
        except Exception as e:
            logger.error(f"Failed to stop scheduler: {e}")

############################################
# Api setup                                #
############################################


app = FastAPI(lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
    expose_headers=["X-Request-ID"],
)

REQUEST_ID_HEADER = "X-Request-ID"

PUBLIC_EXACT_PATHS = {
    "/",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
    "/test",
    "/token",
    "/verify-token",
    "/auth_user",
    "/create_user",
    "/create_pro_account",
    "/password/forgot",
    "/password/verify-otp",
    "/password/reset",
    "/verify-registration-otp",
    "/resend-registration-otp",
    "/email-verification/verify-registration-otp",
    "/email-verification/resend-registration-otp",
    "/consultation/lawyer/stripe/return",
    "/consultation/stripe/webhook",
    "/consultation/stripe/connect-webhook",
    "/identity-verification/webhooks/persona",
    "/subscription/webhook",
}
PUBLIC_PREFIXES = (
    "/oauth/",
)

ADMIN_EXACT_PATHS = {
    "/process_s3_pdfs",
    "/scrape_url",
    "/scraped_urls",
    "/check_bucket_permissions",
    "/feedback/list",
    "/hallucination_stats",
    "/recent_validations",
}
ADMIN_PREFIXES = (
    "/admin/",
    "/backend/users/",
    "/processing_status/",
    "/subscription/admin/",
)

IDENTITY_PROTECTED_FEATURE_PATHS = (
    "/chat_stream",
    "/conversation_history",
    "/conversations",
    "/inbox",
    "/referral",
    "/search",
    "/consultation/sessions",
)
IDENTITY_PROTECTED_SUBSCRIPTION_PATHS = (
    "/subscription/plans",
    "/subscription/upgrade-options",
    "/subscription/upgrade",
    "/subscription/top-up",
    "/subscription/daily-pass",
    "/subscription/subscribe",
    "/subscription/downgrade",
    "/subscription/reactivate",
    "/subscription/sync",
    "/subscription/sync-consumables",
)


def default_authentication_enabled() -> bool:
    return os.getenv("ENFORCE_DEFAULT_AUTH", "true").lower() != "false"


def path_matches_prefix(path: str, prefixes) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes)


def public_path(path: str) -> bool:
    return path in PUBLIC_EXACT_PATHS or path_matches_prefix(path, PUBLIC_PREFIXES)


def admin_token_path(path: str) -> bool:
    return path in ADMIN_EXACT_PATHS or path_matches_prefix(path, ADMIN_PREFIXES)


def identity_feature_path_protected(path: str) -> bool:
    if path.startswith("/referral/payment_redirect/"):
        # Public redirect page opened directly from an email link
        # (automation #11) - no user session exists at that point, so
        # it must not require identity verification like the rest of
        # /referral/* does.
        return False
    return path in IDENTITY_PROTECTED_SUBSCRIPTION_PATHS or any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in IDENTITY_PROTECTED_FEATURE_PATHS
    )


def get_request_user_id(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    try:
        token_data = auth.verify_request_token(request)
        return token_data.get("user_id")
    except HTTPException:
        return None


@app.middleware("http")
async def enforce_default_authentication(request: Request, call_next):
    if request.method == "OPTIONS" or not default_authentication_enabled():
        return await call_next(request)

    path = request.url.path
    if public_path(path):
        return await call_next(request)

    if admin_token_path(path):
        try:
            AdminAPI._require_admin_token(request.headers.get("X-Admin-Token"))
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        return await call_next(request)

    try:
        token_data = auth.verify_request_token(request)
        request.state.user_id = token_data.get("user_id")
        request.state.token_data = token_data
    except HTTPException as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex}"
    request.state.request_id = request_id
    start_time = time.perf_counter()
    user_id = get_request_user_id(request)

    log_context = {
        "request_id": request_id,
        "user_id": user_id,
        "endpoint": request.url.path,
        "method": request.method,
        "client_host": request.client.host if request.client else None,
    }

    try:
        response = await call_next(request)
    except Exception as error:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.exception(
            "request_failed",
            extra={
                **log_context,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        raise

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    response.headers[REQUEST_ID_HEADER] = request_id

    log_method = logger.info
    message = "request_completed"
    if response.status_code >= 500:
        log_method = logger.error
        message = "request_failed"
    elif response.status_code >= 400:
        log_method = logger.warning
        message = "request_rejected"

    log_method(
        message,
        extra={
            **log_context,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        },
    )

    return response


@app.middleware("http")
async def enforce_identity_verification_for_features(request: Request, call_next):
    if request.method == "OPTIONS" or not identity_feature_path_protected(request.url.path):
        return await call_next(request)

    try:
        token_data = auth.verify_request_token(request)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        with db.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            require_identity_verification_for_feature(session, user)
    except HTTPException as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    return await call_next(request)


@app.get("/")
async def root():
    try:
        logger.info("Accessed root endpoint")
        return {"message": "Hello World"}
    except Exception as e:
        logger.error("Error in root endpoint: %s", e)
        raise HTTPException(status_code=500, detail="Internal Server Error")

############################################
# Endpoints                                #
############################################

app_api = AppAPI(app_name=app_name, db=db, auth=auth)
app.include_router(app_api.get_router())

knowledgeBases_api = KnowledgeBasesAPI(app_name=app_name, db=db, auth=auth)
app.include_router(knowledgeBases_api.get_router())

chat_api = ChatAPI(app_name=app_name, db=db, auth=auth)
app.include_router(chat_api.get_router())

auth_api = AuthAPI()
app.include_router(auth_api.get_router())

speech_api = SpeechAPI(app_name=app_name, db=db, auth=auth)
app.include_router(speech_api.get_router())

# Add password reset API
password_reset_api = PasswordResetAPI(app_name=app_name, db=db, auth=auth)
app.include_router(password_reset_api.get_router())

# Add feedback API
feedback_api = FeedbackAPI(app_name=app_name, db=db, auth=auth)
app.include_router(feedback_api.get_router())

# Add email verification API
email_verification_api = EmailVerificationAPI(app_name=app_name, db=db, auth=auth)
app.include_router(email_verification_api.get_router())

# Add chat feedback API
chat_feedback_api = ChatFeedbackAPI(app_name=app_name, db=db, auth=auth)
app.include_router(chat_feedback_api.get_router())

# Add referral API
referral_api = ReferralAPI(app_name=app_name, db=db, auth=auth)
app.include_router(referral_api.get_router())

# Add consultation marketplace API
consultation_api = ConsultationAPI(app_name=app_name, db=db, auth=auth)
app.include_router(consultation_api.get_router(), tags=["Consultation"])

# Add Calls API
from api.calls import CallsAPI
calls_api = CallsAPI(app_name=app_name, db=db, auth=auth, consultation_api=consultation_api)
app.include_router(calls_api.get_router(), tags=["Calls"])

# Add inbox API
inbox_api = InboxAPI(app_name=app_name, db=db, auth=auth)
app.include_router(inbox_api.get_router())

# Add Persona identity verification API
identity_verification_api = IdentityVerificationAPI(app_name=app_name, db=db, auth=auth)
app.include_router(identity_verification_api.get_router())

# Add authenticated product analytics ingestion
product_analytics_api = ProductAnalyticsAPI(app_name=app_name, db=db, auth=auth)
app.include_router(product_analytics_api.get_router())

# Add internal admin API
admin_api = AdminAPI(app_name=app_name, db=db)
app.include_router(admin_api.get_router())

# Add file download API for secure file access
from api.file_download import FileDownloadAPI
file_download_api = FileDownloadAPI(app_name=app_name, db=db, auth=auth)
app.include_router(file_download_api.get_router())

# Add WebSocket API for real-time communication
websocket_api = WebSocketAPI(app_name=app_name, db=db, auth=auth)
app.include_router(websocket_api.get_router())

# Add OAuth API
from api.oauth import OAuthAPI
oauth_api = OAuthAPI(app_name=app_name, db=db, auth=auth)
app.include_router(oauth_api.get_router())

# Add Reviews API
from api.reviews import ReviewsAPI
reviews_api = ReviewsAPI(app_name=app_name, db=db, auth=auth)
app.include_router(reviews_api.get_router(), tags=["Reviews"])

# Add Notifications API
from api.notifications import NotificationAPI
notifications_api = NotificationAPI(app_name=app_name, db=db, auth=auth)
app.include_router(notifications_api.get_router(), tags=["Notifications"])

# Add Subscription API
from api.subscription import SubscriptionAPI
subscription_api = SubscriptionAPI(app_name=app_name, db=db, auth=auth)
app.include_router(subscription_api.get_router(), tags=["Subscription"])

# 5. Add this if you want to run the app directly with Python
if __name__ == "__main__":
    logger.info("Starting application...")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
