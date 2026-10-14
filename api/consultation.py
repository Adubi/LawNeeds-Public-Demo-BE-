from fastapi import APIRouter, Body, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import asyncio
import json
import logging
import os
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from services.consultation_payment_service import ConsultationPaymentService
from services.consultation_extension_service import ConsultationExtensionService
from services.models import ConsultationSurvey, ReferralSession, ReferralRequest, User, DeviceToken, InboxMessage, LawyerReview, Call
from services.stripe_service import StripeService
from services.firebase_notification_service import FirebaseNotificationService
from services.notification_service import NotificationService
from services.persona_service import require_identity_verification
from services.review_service import ReviewService


logger = logging.getLogger(__name__)


class StripeOnboardingRequest(BaseModel):
    return_url: Optional[str] = None
    refresh_url: Optional[str] = None


class PriceProposalRequest(BaseModel):
    amount_gbp: Optional[Decimal] = Field(
        None,
        description="Required for ongoing/casework. Platform-fixed sessions do not require propose-price; "
        "payment auto-initializes on lawyer accept (legacy callers may still POST for idempotency).",
    )
    pricing_type: Optional[str] = Field(
        None,
        description="Defaults to required type for this referral; single sessions must be in_house.",
    )


class ConfirmConsultationPaymentRequest(BaseModel):
    payment_intent_id: Optional[str] = Field(None, description="Stripe PaymentIntent ID")
    payment_method_id: Optional[str] = Field(None, description="Optional Stripe payment method to confirm server-side")

class ConsultationTimeExtensionRequest(BaseModel):
    extend_minutes: int = Field(
        ...,
        description=(
            "Number of minutes to extend by. Required value; allowed values are: "
            "5, 10, 20, 30, or 60."
        ),
        examples=[5, 10, 20, 30, 60],
    )


class ExtensionDecisionRequest(BaseModel):
    decision: str = Field(..., description="accept, deny, or propose")
    proposed_minutes: Optional[int] = None


class ExtensionPaymentRequest(BaseModel):
    payment_intent_id: Optional[str] = None
    payment_method_id: Optional[str] = None


class ConsultationSurveyRequest(BaseModel):
    overall_rating: Optional[int] = Field(None, ge=1, le=5)
    lawyer_rating: Optional[int] = Field(None, ge=1, le=5)
    platform_rating: Optional[int] = Field(None, ge=1, le=5)
    was_helpful: Optional[bool] = None
    would_recommend: Optional[bool] = None
    comment: Optional[str] = Field(None, max_length=2000)
    lawyer_comment: Optional[str] = Field(None, max_length=2000)
    issue_tags: Optional[List[str]] = Field(None, max_length=10)
    skipped: bool = False


class ConsultationEndAnswersRequest(BaseModel):
    questions_addressed: str = Field(..., description="yes_fully, partially, or no")
    next_steps_understood: Optional[str] = Field(None, description="yes, not_sure, or no_next_steps_required")
    final_clarification: Optional[str] = Field(None, description="happy_to_end or another_question")


class ConsultationSurveyPayload(BaseModel):
    id: str
    session_id: str
    referral_id: str
    user_id: str
    lawyer_id: str
    reviewer_role: str
    overall_rating: int
    lawyer_rating: Optional[int]
    platform_rating: Optional[int]
    was_helpful: Optional[bool]
    would_recommend: Optional[bool]
    comment: Optional[str]
    issue_tags: List[str]
    skipped: bool = False
    created_at: str
    updated_at: Optional[str]


class ConsultationSurveyStatusResponse(BaseModel):
    session_id: str
    session_status: str
    can_submit: bool
    submitted: bool
    skipped: bool = False
    survey: Optional[ConsultationSurveyPayload] = None


class ConsultationTimerManager:
    """Manage live timer websocket connections per consultation session."""

    def __init__(self):
        self.connections: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, session_id: str, user_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        session_connections = self.connections.setdefault(session_id, {})
        session_connections[user_id] = websocket

    def disconnect(self, session_id: str, user_id: str) -> None:
        session_connections = self.connections.get(session_id)
        if not session_connections:
            return

        session_connections.pop(user_id, None)
        if not session_connections:
            self.connections.pop(session_id, None)

    async def broadcast(self, session_id: str, payload: Dict[str, Any]) -> None:
        message = json.dumps(payload)
        for websocket in list(self.connections.get(session_id, {}).values()):
            try:
                await websocket.send_text(message)
            except Exception as exc:
                logger.warning(f"Failed to broadcast timer update for session {session_id}: {exc}")


class ConsultationAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.timer_manager = ConsultationTimerManager()
        self.review_service = ReviewService(db)

    def get_router(self):
        router = APIRouter()

        @router.get("/consultation/sessions/{session_id}/survey", response_model=ConsultationSurveyStatusResponse)
        async def get_consultation_survey_status(
            request: Request,
            session_id: str,
        ):
            """Return whether the current participant can submit a post-consultation survey."""
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)
                    survey = self._get_existing_survey(session, session_uuid, user_id)

                    return self._build_survey_status(session_obj, survey)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error fetching consultation survey status: {exc}")
                raise HTTPException(status_code=500, detail="Failed to fetch consultation survey status")

        @router.post("/consultation/sessions/{session_id}/survey", response_model=ConsultationSurveyStatusResponse)
        async def submit_consultation_survey(
            request: Request,
            session_id: str,
            data: ConsultationSurveyRequest,
        ):
            """Submit one private post-consultation survey for the current participant."""
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)

                    if not self._session_is_complete(session_obj):
                        raise HTTPException(status_code=400, detail="Consultation survey is available after the consultation is completed")

                    existing = self._get_existing_survey(session, session_uuid, user_id)
                    if existing:
                        raise HTTPException(status_code=409, detail="Consultation survey has already been submitted")

                    skipped = bool(data.skipped)
                    issue_tags = self._clean_survey_issue_tags(data.issue_tags)
                    if skipped:
                        overall_rating = 0
                        lawyer_rating = None
                        platform_rating = None
                        was_helpful = None
                        would_recommend = None
                        comment = None
                        if "skipped" not in issue_tags:
                            issue_tags.append("skipped")
                    else:
                        if data.overall_rating is None:
                            raise HTTPException(status_code=400, detail="overall_rating is required unless skipped is true")
                        overall_rating = data.overall_rating
                        lawyer_rating = data.lawyer_rating
                        platform_rating = data.platform_rating
                        was_helpful = data.was_helpful
                        would_recommend = data.would_recommend
                        comment = (data.comment or "").strip() or None

                    now = datetime.now(timezone.utc)
                    survey = ConsultationSurvey(
                        session_id=session_uuid,
                        referral_request_id=session_obj.referral_request_id,
                        user_id=uuid.UUID(user_id),
                        lawyer_id=session_obj.lawyer_id,
                        reviewer_role=self._survey_reviewer_role(session_obj, user_id),
                        overall_rating=overall_rating,
                        lawyer_rating=lawyer_rating,
                        platform_rating=platform_rating,
                        was_helpful=was_helpful,
                        would_recommend=would_recommend,
                        comment=comment,
                        issue_tags=issue_tags,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(survey)
                    if not skipped:
                        self._sync_consultation_lawyer_review(
                            session=session,
                            session_obj=session_obj,
                            reviewer_id=user_id,
                            lawyer_rating=lawyer_rating,
                            lawyer_comment=data.lawyer_comment,
                        )
                    session.commit()
                    session.refresh(survey)

                    return self._build_survey_status(session_obj, survey)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error submitting consultation survey: {exc}")
                raise HTTPException(status_code=500, detail="Failed to submit consultation survey")

        @router.post("/consultation/lawyer/stripe/onboard")
        async def onboard_lawyer_to_stripe(
            request: Request,
            data: Optional[StripeOnboardingRequest] = None,
        ):
            try:
                lawyer_id = self._get_request_user_id(request)
                stripe_service = StripeService()
                onboarding_data = data or StripeOnboardingRequest()

                # Extract the raw JWT so it can be embedded in the return URL.
                # Stripe performs a browser redirect to return_url, meaning no
                # Authorization header will be present on that request.
                raw_token = self._extract_raw_token(request)

                with self.db.get_session() as session:
                    lawyer = self._require_lawyer(session, lawyer_id)

                    if not lawyer.stripe_connect_account_id:
                        success, message, account_id = stripe_service.create_connect_account(
                            lawyer_email=lawyer.email,
                            metadata={"lawyer_id": str(lawyer.id)},
                        )
                        if not success or not account_id:
                            raise HTTPException(status_code=502, detail=message)
                        lawyer.stripe_connect_account_id = account_id

                    status_success, status_message, status_data = stripe_service.get_account_status(
                        lawyer.stripe_connect_account_id
                    )

                    if (
                        not status_success
                        and stripe_service.is_connect_account_mode_mismatch(status_message)
                    ):
                        logger.warning(
                            "Resetting Stripe Connect account for mode mismatch: lawyer_id=%s account_id=%s",
                            lawyer.id,
                            lawyer.stripe_connect_account_id,
                        )
                        lawyer.stripe_connect_account_id = None
                        lawyer.stripe_connect_onboarded = False
                        success, message, account_id = stripe_service.create_connect_account(
                            lawyer_email=lawyer.email,
                            metadata={"lawyer_id": str(lawyer.id)},
                        )
                        if not success or not account_id:
                            raise HTTPException(status_code=502, detail=message)
                        lawyer.stripe_connect_account_id = account_id
                        status_success, status_message, status_data = stripe_service.get_account_status(
                            lawyer.stripe_connect_account_id
                        )

                    if not status_success or not status_data:
                        raise HTTPException(status_code=502, detail=status_message)

                    lawyer.stripe_connect_onboarded = status_data["onboarded"]

                    base_url = str(request.base_url).rstrip("/")

                    # Build the return URL. If a custom one is supplied we leave it
                    # as-is; otherwise we use the dedicated browser-friendly return
                    # endpoint and embed the token so auth works after the redirect.
                    if onboarding_data.return_url:
                        return_url = onboarding_data.return_url
                    elif os.getenv("STRIPE_CONNECT_RETURN_URL"):
                        return_url = os.getenv("STRIPE_CONNECT_RETURN_URL")
                    else:
                        qs = urlencode({"token": raw_token}) if raw_token else ""
                        return_url = f"{base_url}/consultation/lawyer/stripe/return" + (f"?{qs}" if qs else "")

                    refresh_url = onboarding_data.refresh_url or os.getenv("STRIPE_CONNECT_REFRESH_URL") or f"{base_url}/consultation/lawyer/stripe/onboard"

                    link_success, link_message, onboarding_url = stripe_service.create_onboarding_link(
                        account_id=lawyer.stripe_connect_account_id,
                        return_url=return_url,
                        refresh_url=refresh_url,
                    )
                    if not link_success or not onboarding_url:
                        raise HTTPException(status_code=502, detail=link_message)

                    session.commit()

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "account_id": lawyer.stripe_connect_account_id,
                            "onboarded": lawyer.stripe_connect_onboarded,
                            "charges_enabled": status_data["charges_enabled"],
                            "payouts_enabled": status_data["payouts_enabled"],
                            "onboarding_url": onboarding_url,
                        },
                    )

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error onboarding lawyer to Stripe: {exc}")
                raise HTTPException(status_code=500, detail="Error creating Stripe onboarding link")

        @router.get("/consultation/lawyer/stripe/return", response_class=HTMLResponse)
        async def stripe_onboarding_return(request: Request, token: Optional[str] = None):
            """
            Browser-facing landing page that Stripe redirects to after Connect onboarding.
            Accepts the JWT via query param (embedded when the onboarding link was created),
            syncs the account status, and renders a confirmation page.
            """
            status_data = None
            error_message = None

            if token:
                try:
                    token_data = self.auth.verify_token(token)
                    lawyer_id = token_data.get("user_id")
                    if not lawyer_id:
                        raise ValueError("Token missing user_id")

                    stripe_service = StripeService()
                    with self.db.get_session() as session:
                        lawyer = self._require_lawyer(session, lawyer_id)
                        if lawyer.stripe_connect_account_id:
                            success, message, status_data = stripe_service.get_account_status(
                                lawyer.stripe_connect_account_id
                            )
                            if success and status_data:
                                lawyer.stripe_connect_onboarded = status_data["onboarded"]
                                session.commit()
                            else:
                                error_message = message
                        else:
                            error_message = "No Stripe account found for this lawyer."
                except Exception as exc:
                    logger.error(f"Error syncing Stripe status on return: {exc}")
                    error_message = "Could not retrieve your Stripe account status."
            else:
                error_message = "Authentication token missing. Please log in and retry."

            if status_data and status_data.get("onboarded"):
                heading = "Onboarding Complete"
                body_lines = [
                    "Your Stripe account has been successfully set up.",
                    "You can now receive consultation payments.",
                ]
                icon = "&#10003;"
                color = "#22c55e"
            elif status_data:
                heading = "Almost There"
                body_lines = [
                    "Stripe received your submission, but your account is not fully verified yet.",
                    "This can take a few minutes. Please check back shortly.",
                ]
                icon = "&#9888;"
                color = "#f59e0b"
            else:
                heading = "Something Went Wrong"
                body_lines = [error_message or "An unexpected error occurred."]
                icon = "&#10005;"
                color = "#ef4444"

            body_html = "".join(f"<p>{line}</p>" for line in body_lines)

            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Stripe Onboarding – LawNeeds</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f8fafc;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,.08);
      padding: 48px 40px;
      max-width: 480px;
      width: 90%;
      text-align: center;
    }}
    .icon {{
      font-size: 56px;
      color: {color};
      margin-bottom: 20px;
    }}
    h1 {{ font-size: 1.6rem; color: #1e293b; margin-bottom: 12px; }}
    p {{ color: #64748b; font-size: 0.95rem; line-height: 1.6; margin-top: 8px; }}
    .btn {{
      display: inline-block;
      margin-top: 28px;
      padding: 12px 28px;
      background: {color};
      color: #fff;
      border-radius: 8px;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.9rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{heading}</h1>
    {body_html}
    <a class="btn" href="javascript:window.close()">Close this window</a>
  </div>
</body>
</html>"""
            return HTMLResponse(content=html)

        @router.get("/consultation/lawyer/stripe/status")
        async def get_lawyer_stripe_status(request: Request):
            try:
                lawyer_id = self._get_request_user_id(request)
                stripe_service = StripeService()

                with self.db.get_session() as session:
                    lawyer = self._require_lawyer(session, lawyer_id)
                    if not lawyer.stripe_connect_account_id:
                        return {
                            "onboarded": False,
                            "charges_enabled": False,
                            "payouts_enabled": False,
                            "account_id": None,
                        }

                    success, message, status_data = stripe_service.get_account_status(
                        lawyer.stripe_connect_account_id
                    )
                    if not success or not status_data:
                        raise HTTPException(status_code=502, detail=message)

                    lawyer.stripe_connect_onboarded = status_data["onboarded"]
                    session.commit()

                    return status_data

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error retrieving lawyer Stripe status: {exc}")
                raise HTTPException(status_code=500, detail="Error retrieving Stripe Connect status")

        @router.post("/consultation/lawyer/stripe/dashboard-link")
        async def create_lawyer_stripe_dashboard_link(request: Request):
            try:
                lawyer_id = self._get_request_user_id(request)
                stripe_service = StripeService()

                with self.db.get_session() as session:
                    lawyer = self._require_lawyer(session, lawyer_id)
                    if not lawyer.stripe_connect_account_id:
                        raise HTTPException(status_code=400, detail="Stripe account is not connected")

                    success, message, status_data = stripe_service.get_account_status(
                        lawyer.stripe_connect_account_id
                    )
                    if not success or not status_data:
                        raise HTTPException(status_code=502, detail=message)

                    lawyer.stripe_connect_onboarded = status_data["onboarded"]
                    session.commit()

                    if not status_data["onboarded"]:
                        raise HTTPException(status_code=400, detail="Stripe onboarding is not complete")

                    link_success, link_message, dashboard_url = stripe_service.create_express_dashboard_login_link(
                        lawyer.stripe_connect_account_id
                    )
                    if not link_success or not dashboard_url:
                        raise HTTPException(status_code=502, detail=link_message)

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "url": dashboard_url,
                        },
                    )

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error creating lawyer Stripe dashboard link: {exc}")
                raise HTTPException(status_code=500, detail="Error creating Stripe dashboard link")

        @router.get("/consultation/sessions/{session_id}/pricing-rules")
        async def get_consultation_pricing_rules(session_id: str, request: Request):
            """Lawyer: whether this session is platform-fixed or requires a proposed fee + pricing type."""
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_lawyer(session_obj, user_id)

                    service = ConsultationPaymentService(session)
                    rules = service.get_pricing_rules(session_uuid)
                    return JSONResponse(status_code=200, content=rules)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error loading consultation pricing rules: {exc}")
                raise HTTPException(status_code=500, detail="Error loading pricing rules")

        @router.post("/consultation/sessions/{session_id}/propose-price")
        async def propose_price(
            session_id: str,
            request: Request,
            data: PriceProposalRequest,
        ):
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_lawyer(session_obj, user_id)

                    service = ConsultationPaymentService(session)
                    success, message, result = service.propose_price_for_session(
                        session_uuid,
                        proposed_amount_gbp=data.amount_gbp,
                        pricing_type=data.pricing_type,
                    )
                    if not success:
                        raise HTTPException(status_code=400, detail=message)

                    # Notify the user that a price has been proposed
                    try:
                        lawyer = session.query(User).filter(
                            User.id == session_obj.lawyer_id
                        ).first()
                        lawyer_name = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"

                        user_tokens = session.query(DeviceToken).filter(
                            DeviceToken.user_id == session_obj.user_id,
                            DeviceToken.is_active == True,
                        ).all()
                        token_list = [t.device_token for t in user_tokens]

                        amount_pence = result.get("total_amount_pence") if result else None
                        amount_display = (
                            f"£{amount_pence / 100:.2f}" if amount_pence else "a fee"
                        )
                        referral_id = str(session_obj.referral_request_id)

                        if token_list:
                            firebase = FirebaseNotificationService()
                            await firebase.send_price_proposed_notification(
                                device_tokens=token_list,
                                lawyer_name=lawyer_name,
                                session_id=session_id,
                                referral_id=referral_id,
                                amount_display=amount_display,
                            )

                        # Persist in-app notification for the user
                        NotificationService(session).create(
                            recipient_id=session_obj.user_id,
                            title="Your Lawyer Has Sent a Quote",
                            body=f"{lawyer_name} has proposed a fee of {amount_display} for your consultation. Review and confirm to proceed.",
                            notification_type="price_proposed",
                            data={"session_id": session_id, "referral_id": referral_id, "screen": "ConsultationPayment"},
                        )
                    except Exception as fcm_err:
                        logger.warning(f"FCM push failed after price proposal: {fcm_err}")

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": message,
                            "payment": result,
                        },
                    )

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error proposing consultation price: {exc}")
                raise HTTPException(status_code=500, detail="Error creating consultation payment")

        @router.get("/consultation/sessions/{session_id}/payment")
        async def get_consultation_payment(session_id: str, request: Request):
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)

                    service = ConsultationPaymentService(session)
                    payment_summary = service.get_payment_summary_with_lazy_platform_fixed_init(session_uuid)
                    if not payment_summary:
                        raise HTTPException(status_code=404, detail="No payment has been initiated for this session")

                    if payment_summary.get("stripe_payment_intent_id") and str(session_obj.user_id) == user_id:
                        stripe_service = StripeService()
                        intent_success, _, intent_data = stripe_service.get_payment_intent(
                            payment_summary["stripe_payment_intent_id"]
                        )
                        if intent_success and intent_data:
                            payment_summary["client_secret"] = intent_data.get("client_secret")
                            payment_summary["stripe_status"] = intent_data.get("status")

                    return payment_summary

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error retrieving consultation payment: {exc}")
                raise HTTPException(status_code=500, detail="Error retrieving consultation payment")

        @router.post("/consultation/sessions/{session_id}/confirm-payment")
        async def confirm_consultation_payment(
            session_id: str,
            request: Request,
            data: ConfirmConsultationPaymentRequest,
        ):
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_user(session_obj, user_id)

                    service = ConsultationPaymentService(session)
                    success, message, result = service.confirm_payment(
                        session_uuid,
                        payment_intent_id=data.payment_intent_id,
                        payment_method_id=data.payment_method_id,
                    )
                    if not success:
                        raise HTTPException(status_code=400, detail=message)

                    # Notify the lawyer that payment has been made
                    try:
                        client = session.query(User).filter(
                            User.id == session_obj.user_id
                        ).first()
                        client_name = f"{client.first_name} {client.last_name}" if client else "Your client"

                        lawyer_tokens = session.query(DeviceToken).filter(
                            DeviceToken.user_id == session_obj.lawyer_id,
                            DeviceToken.is_active == True,
                        ).all()
                        token_list = [t.device_token for t in lawyer_tokens]

                        referral_id = str(session_obj.referral_request_id)

                        if token_list:
                            firebase = FirebaseNotificationService()
                            await firebase.send_payment_confirmed_notification(
                                device_tokens=token_list,
                                user_name=client_name,
                                session_id=session_id,
                                referral_id=referral_id,
                            )

                        # Persist in-app notification for the lawyer
                        NotificationService(session).create(
                            recipient_id=session_obj.lawyer_id,
                            title="Payment Received!",
                            body=f"{client_name} has paid for the consultation. You're all set to begin.",
                            notification_type="payment_confirmed",
                            data={"session_id": session_id, "referral_id": referral_id, "screen": "ConsultationSession"},
                        )
                    except Exception as fcm_err:
                        logger.warning(f"FCM push failed after payment confirm: {fcm_err}")

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": message,
                            "payment": result,
                        },
                    )

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error confirming consultation payment: {exc}")
                raise HTTPException(status_code=500, detail="Error confirming consultation payment")

        @router.post("/consultation/sessions/{session_id}/timer/start")
        async def start_consultation_timer(session_id: str, request: Request):
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)

                    service = ConsultationPaymentService(session)
                    payment_summary = service.get_payment_summary(session_uuid)
                    if not payment_summary or payment_summary["payment_status"] not in ("paid", "transferred"):
                        raise HTTPException(status_code=400, detail="Consultation must be paid before the timer can start")

                    if session_obj.session_status == 'completed':
                        raise HTTPException(status_code=400, detail="Consultation session is already completed")

                    days_total = self._get_session_days_total(session, session_obj)
                    is_ongoing = days_total is not None

                    if is_ongoing:
                        # Initialise days_total on first start
                        if not session_obj.days_total:
                            session_obj.days_total = days_total

                        # Day already in progress — return current state (idempotent)
                        if (session_obj.current_day_timer_started_at
                                and not session_obj.current_day_timer_ended_at):
                            state = self._build_timer_state(session_obj, payment_summary, session)
                            return JSONResponse(status_code=200, content=state)

                        # All days done — can't start another
                        if session_obj.days_completed >= days_total:
                            raise HTTPException(
                                status_code=400,
                                detail=f"All {days_total} daily sessions have been completed",
                            )

                        self._assert_two_way_consultation_messages(session, session_obj)

                        now = datetime.now(timezone.utc)
                        # First day: set the overall session start
                        if not session_obj.timer_started_at:
                            session_obj.timer_started_at = now
                            session_obj.start_time = now
                            session_obj.session_status = 'active'

                        # Reset per-day fields for this new day
                        session_obj.current_day_timer_started_at = now
                        session_obj.current_day_timer_ended_at = None
                        session_obj.end_time = now + timedelta(seconds=30 * 60)
                        session.commit()

                    else:
                        # ── Single-session (original) behaviour ──────────────────
                        if session_obj.timer_started_at and not session_obj.timer_ended_at:
                            state = self._build_timer_state(session_obj, payment_summary, session)
                            return JSONResponse(status_code=200, content=state)

                        self._assert_two_way_consultation_messages(session, session_obj)

                        now = datetime.now(timezone.utc)
                        session_obj.timer_started_at = now
                        session_obj.start_time = now
                        session_obj.session_status = 'active'
                        session.commit()

                        total_duration = self._get_session_duration_seconds(session, session_obj)
                        if total_duration is not None:
                            session_obj.end_time = now + timedelta(seconds=total_duration)
                            session.commit()

                    # Capture IDs for background tasks (can't pass ORM objects across sessions)
                    task_user_id = session_obj.user_id
                    task_lawyer_id = session_obj.lawyer_id
                    task_referral_id = str(session_obj.referral_request_id)

                    payload = {
                        "type": "timer_started",
                        **self._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.timer_manager.broadcast(session_id, payload)

                    # Schedule 1-minute warning and auto-end.
                    # For ongoing sessions the duration is always 30 min per day.
                    # These tasks fire regardless of whether any client is still connected.
                    task_duration = (30 * 60) if is_ongoing else self._get_session_duration_seconds(session, session_obj)
                    if task_duration is not None:
                        if task_duration > 60:
                            asyncio.create_task(
                                self._send_one_minute_warning_task(
                                    delay_seconds=task_duration - 60,
                                    session_id=session_id,
                                    referral_id=task_referral_id,
                                    user_id=task_user_id,
                                    lawyer_id=task_lawyer_id,
                                )
                            )
                        asyncio.create_task(
                            self._auto_end_session_task(
                                delay_seconds=task_duration,
                                session_id=session_id,
                                referral_id=task_referral_id,
                                user_id=task_user_id,
                                lawyer_id=task_lawyer_id,
                            )
                        )

                    return JSONResponse(status_code=200, content=payload)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error starting consultation timer: {exc}")
                raise HTTPException(status_code=500, detail="Error starting consultation timer")

        @router.get("/consultation/sessions/{session_id}/extensions/options")
        async def get_extension_options(session_id: str, request: Request):
            try:
                user_id = self._get_request_user_id(request)
                with self.db.get_session() as session:
                    session_obj = self._require_session(session, uuid.UUID(session_id))
                    self._assert_session_participant(session_obj, user_id)
                    return ConsultationExtensionService(session).get_options(session_obj.id)
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.get("/consultation/sessions/{session_id}/extensions/active")
        async def get_active_extension(session_id: str, request: Request):
            try:
                user_id = self._get_request_user_id(request)
                with self.db.get_session() as session:
                    session_obj = self._require_session(session, uuid.UUID(session_id))
                    self._assert_session_participant(session_obj, user_id)
                    extension = ConsultationExtensionService(session).get_active(session_obj.id)
                    return {"extension": extension}
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/sessions/{session_id}/extensions/pause")
        async def pause_for_extension(session_id: str, request: Request):
            try:
                user_id = uuid.UUID(self._get_request_user_id(request))
                with self.db.get_session() as session:
                    service = ConsultationExtensionService(session)
                    extension_obj = service.pause(uuid.UUID(session_id), user_id)
                    result = service.serialize(extension_obj)
                    session_obj = self._require_session(session, uuid.UUID(session_id))
                    if getattr(extension_obj, "_transitioned", True):
                        await self._notify_extension(
                            session,
                            recipient_id=session_obj.lawyer_id,
                            title="Extension Started",
                            body="Your client paused the consultation to choose an extension.",
                            notification_type="extension_started",
                            extension=result,
                            referral_id=str(session_obj.referral_request_id),
                        )
                    payload = {
                        "type": "timer_paused",
                        "extension": result,
                        **self._build_timer_state(session_obj, None, session),
                    }
                    await self.timer_manager.broadcast(session_id, payload)
                    return payload
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/extensions/{extension_id}/submit")
        async def submit_extension(extension_id: str, request: Request, data: ConsultationTimeExtensionRequest):
            try:
                user_id = uuid.UUID(self._get_request_user_id(request))
                with self.db.get_session() as session:
                    service = ConsultationExtensionService(session)
                    extension_obj = service.submit(uuid.UUID(extension_id), user_id, data.extend_minutes)
                    result = service.serialize(extension_obj)
                    session_obj = self._require_session(session, extension_obj.session_id)
                    total = result["price"]["total_amount_gbp"]
                    if getattr(extension_obj, "_transitioned", True):
                        await self._notify_extension(
                            session,
                            recipient_id=session_obj.lawyer_id,
                            title="Extension Request",
                            body=f"Your client requested {data.extend_minutes} extra minutes (£{total} including VAT).",
                            notification_type="extension_requested",
                            extension=result,
                            referral_id=str(session_obj.referral_request_id),
                        )
                    payload = {"type": "extension_requested", "extension": result}
                    await self.timer_manager.broadcast(str(session_obj.id), payload)
                    return payload
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/extensions/{extension_id}/decision")
        async def decide_extension(extension_id: str, request: Request, data: ExtensionDecisionRequest):
            try:
                lawyer_id = uuid.UUID(self._get_request_user_id(request))
                with self.db.get_session() as session:
                    service = ConsultationExtensionService(session)
                    extension_obj = service.decide(
                        uuid.UUID(extension_id), lawyer_id, data.decision, data.proposed_minutes
                    )
                    result = service.serialize(extension_obj)
                    session_obj = self._require_session(session, extension_obj.session_id)
                    status = extension_obj.status
                    if status == "denied":
                        title, body, event_type = "Extension Denied", "Your lawyer denied the extension request.", "extension_denied"
                    elif status == "proposed":
                        title = "New Extension Time Proposed"
                        body = f"Your lawyer proposed {extension_obj.agreed_minutes} extra minutes."
                        event_type = "extension_proposed"
                    else:
                        title, body, event_type = "Extension Approved", "Your extension was approved. Complete payment within 10 minutes.", "extension_approved"
                    if getattr(extension_obj, "_transitioned", True):
                        await self._notify_extension(
                            session,
                            recipient_id=session_obj.user_id,
                            title=title,
                            body=body,
                            notification_type=event_type,
                            extension=result,
                            referral_id=str(session_obj.referral_request_id),
                        )
                    payload = {"type": event_type, "extension": result}
                    if status == "denied":
                        payload.update(self._build_timer_state(session_obj, None, session))
                    await self.timer_manager.broadcast(str(session_obj.id), payload)
                    return payload
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/extensions/{extension_id}/cancel")
        async def cancel_extension(extension_id: str, request: Request):
            try:
                user_id = uuid.UUID(self._get_request_user_id(request))
                with self.db.get_session() as session:
                    service = ConsultationExtensionService(session)
                    extension_obj = service.cancel(uuid.UUID(extension_id), user_id)
                    session_obj = self._require_session(session, extension_obj.session_id)
                    payload = {
                        "type": "extension_cancelled",
                        "extension": service.serialize(extension_obj),
                        **self._build_timer_state(session_obj, None, session),
                    }
                    await self.timer_manager.broadcast(str(session_obj.id), payload)
                    return payload
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/extensions/{extension_id}/payment")
        async def create_extension_payment(extension_id: str, request: Request):
            try:
                user_id = uuid.UUID(self._get_request_user_id(request))
                with self.db.get_session() as session:
                    return ConsultationExtensionService(session).create_payment(uuid.UUID(extension_id), user_id)
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/extensions/{extension_id}/confirm-payment")
        async def confirm_extension_payment(extension_id: str, request: Request, data: ExtensionPaymentRequest):
            try:
                user_id = uuid.UUID(self._get_request_user_id(request))
                with self.db.get_session() as session:
                    service = ConsultationExtensionService(session)
                    result = service.confirm_payment(
                        uuid.UUID(extension_id), user_id, data.payment_intent_id, data.payment_method_id
                    )
                    extension_obj = service._require_request(uuid.UUID(extension_id))
                    session_obj = self._require_session(session, extension_obj.session_id)
                    extension = service.serialize(extension_obj)
                    if not result.get("already_processed"):
                        await self._notify_extension(
                            session,
                            recipient_id=session_obj.lawyer_id,
                            title="Extension Payment Received",
                            body=f"The {extension_obj.agreed_minutes}-minute extension is paid and the timer has resumed.",
                            notification_type="extension_payment_confirmed",
                            extension=extension,
                            referral_id=str(session_obj.referral_request_id),
                        )
                    payload = {
                        "type": "timer_extended",
                        "extension": extension,
                        **self._build_timer_state(session_obj, None, session),
                    }
                    await self.timer_manager.broadcast(str(session_obj.id), payload)
                    if not result.get("already_processed"):
                        self._schedule_extension_warning(session_obj, extension_obj)
                    return {"success": True, "payment": result, **payload}
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @router.post("/consultation/sessions/{session_id}/timer/extend", deprecated=True)
        async def extend_consultation_timer(
            session_id: str,
            request: Request,
            data: ConsultationTimeExtensionRequest = Body(
                ...,
                description=(
                    "Extend an active consultation timer. Required field `extend_minutes` "
                    "must be one of: 5, 10, 20, 30, 60."
                ),
            ),
        ):
            """Compatibility endpoint: create an approval request; never extends before payment."""
            try:
                user_id = uuid.UUID(self._get_request_user_id(request))
                session_uuid = uuid.UUID(session_id)
                with self.db.get_session() as session:
                    service = ConsultationExtensionService(session)
                    extension_obj = service.pause(session_uuid, user_id)
                    extension_obj = service.submit(extension_obj.id, user_id, data.extend_minutes)
                    result = service.serialize(extension_obj)
                    session_obj = self._require_session(session, session_uuid)
                    await self._notify_extension(
                        session,
                        recipient_id=session_obj.lawyer_id,
                        title="Extension Request",
                        body=f"Your client requested {data.extend_minutes} extra minutes.",
                        notification_type="extension_requested",
                        extension=result,
                        referral_id=str(session_obj.referral_request_id),
                    )
                    payload = {"type": "extension_requested", "extension": result}
                    await self.timer_manager.broadcast(session_id, payload)
                    return JSONResponse(status_code=202, content=payload)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error extending consultation timer: {exc}")
                raise HTTPException(status_code=500, detail="Error extending consultation timer")

        @router.post("/consultation/sessions/{session_id}/end-request")
        async def request_consultation_end(session_id: str, request: Request):
            """Lawyer starts the pre-end confirmation flow and pauses the active timer."""
            try:
                lawyer_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_lawyer(session_obj, lawyer_id)

                    if not session_obj.timer_started_at:
                        raise HTTPException(status_code=400, detail="Consultation timer has not started")
                    if session_obj.timer_ended_at or session_obj.session_status == "completed":
                        raise HTTPException(status_code=400, detail="Consultation session is already completed")

                    if self._timer_is_paused(session_obj) and session_obj.end_request_status != "pending_user_answers":
                        raise HTTPException(status_code=400, detail="Consultation timer is already paused")

                    now = datetime.now(timezone.utc)
                    if session_obj.end_request_status == "pending_user_answers":
                        transitioned = False
                    else:
                        remaining = self._remaining_seconds_for_session(session, session_obj)
                        session_obj.timer_paused_at = now
                        session_obj.paused_remaining_seconds = max(remaining or 0, 0)
                        session_obj.timer_paused_reason = "ending_questions"

                        active_call = session.query(Call).filter(
                            Call.referral_session_id == session_uuid,
                            Call.status.in_(("accepted", "connected")),
                        ).first()
                        if active_call:
                            active_call.status = "ended"
                            active_call.ended_at = now
                            session_obj.current_communication_mode = "chat"

                        session_obj.end_requested_at = now
                        session_obj.end_requested_by = uuid.UUID(lawyer_id)
                        session_obj.end_request_status = "pending_user_answers"
                        session_obj.end_conclusion_answers = None
                        session_obj.end_conclusion_completed_at = None
                        session.commit()
                        transitioned = True

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                    payload = {
                        "type": "consultation_end_requested",
                        **self._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.timer_manager.broadcast(session_id, payload)

                    if transitioned:
                        await self._notify_consultation_end_requested(
                            session=session,
                            session_obj=session_obj,
                            session_id=session_id,
                        )

                    return JSONResponse(status_code=200, content=payload)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error requesting consultation end: {exc}")
                raise HTTPException(status_code=500, detail="Error requesting consultation end")



        @router.post("/consultation/sessions/{session_id}/end-request/answers")
        async def submit_consultation_end_answers(
            session_id: str,
            request: Request,
            data: ConsultationEndAnswersRequest,
        ):
            """User answers the lawyer-requested concluding questions."""
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_user(session_obj, user_id)

                    if session_obj.end_request_status not in ("pending_user_answers", "awaiting_final_confirmation"):
                        raise HTTPException(status_code=400, detail="No end-consultation request is pending")
                    if session_obj.timer_ended_at or session_obj.session_status == "completed":
                        raise HTTPException(status_code=400, detail="Consultation session is already completed")

                    answers = self._validate_consultation_end_answers(data)
                    now = datetime.now(timezone.utc)
                    session_obj.end_conclusion_answers = answers
                    session_obj.end_conclusion_completed_at = now

                    if answers["questions_addressed"] == "no":
                        session_obj.end_request_status = "returned_to_chat"
                        self._resume_paused_timer(session_obj)
                        session.commit()
                        payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                        payload = {
                            "type": "consultation_end_cancelled",
                            "action": "return_to_chat",
                            **self._build_timer_state(session_obj, payment_summary, session),
                        }
                        await self.timer_manager.broadcast(session_id, payload)
                        return JSONResponse(status_code=200, content=payload)

                    if answers.get("final_clarification") == "another_question":
                        has_time_remaining = self._has_consultation_time_remaining(session, session_obj)
                        if has_time_remaining:
                            session_obj.end_request_status = "returned_to_chat"
                            self._resume_paused_timer(session_obj)
                            session.commit()
                            payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                            payload = {
                                "type": "consultation_end_cancelled",
                                "action": "return_to_chat",
                                **self._build_timer_state(session_obj, payment_summary, session),
                            }
                            await self.timer_manager.broadcast(session_id, payload)
                            return JSONResponse(status_code=200, content=payload)

                        session_obj.end_request_status = "follow_up_required"
                        session_obj.timer_paused_at = None
                        session_obj.paused_remaining_seconds = None
                        session_obj.end_time = datetime.now(timezone.utc)
                        session.commit()
                        payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                        payload = {
                            "type": "consultation_end_follow_up_required",
                            "action": "book_follow_up",
                            **self._build_timer_state(session_obj, payment_summary, session),
                        }
                        await self.timer_manager.broadcast(session_id, payload)
                        return JSONResponse(status_code=200, content=payload)

                    session_obj.end_request_status = "awaiting_final_confirmation"
                    session.commit()
                    payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                    payload = {
                        "type": "consultation_end_confirmation_required",
                        "action": "confirm_end",
                        **self._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.timer_manager.broadcast(session_id, payload)
                    return JSONResponse(status_code=200, content=payload)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error submitting consultation end answers: {exc}")
                raise HTTPException(status_code=500, detail="Error submitting consultation end answers")

        @router.post("/consultation/sessions/{session_id}/end-request/confirm")
        async def confirm_consultation_end(session_id: str, request: Request):
            """User confirms they are ready to end after the concluding questions."""
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_user(session_obj, user_id)

                    if session_obj.end_request_status != "awaiting_final_confirmation":
                        raise HTTPException(status_code=400, detail="End-consultation confirmation is not pending")

                return await end_consultation_timer(session_id, request)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error confirming consultation end: {exc}")
                raise HTTPException(status_code=500, detail="Error confirming consultation end")

        @router.post("/consultation/sessions/{session_id}/timer/end")
        async def end_consultation_timer(session_id: str, request: Request):
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)

                    if not session_obj.timer_started_at:
                        raise HTTPException(status_code=400, detail="Consultation timer has not started")

                    # Ending a consultation invalidates any unpaid extension so
                    # a late Stripe confirmation can never revive a completed timer.
                    extension_service = ConsultationExtensionService(session)
                    active_extension = extension_service._active_request(session_uuid, lock=True)
                    if active_extension:
                        extension_service.cancel(active_extension.id, active_extension.user_id)

                    service = ConsultationPaymentService(session)
                    days_total = session_obj.days_total
                    is_ongoing = days_total is not None

                    # ── Ongoing multi-day session ────────────────────────────────
                    if is_ongoing:
                        # If today's timer is already ended, return idempotent response
                        if session_obj.current_day_timer_ended_at:
                            payment_summary = service.get_payment_summary(session_uuid)
                            is_final = session_obj.days_completed >= days_total
                            payload = {
                                "type": "timer_ended" if is_final else "day_timer_ended",
                                **self._build_timer_state(session_obj, payment_summary, session),
                            }
                            return JSONResponse(status_code=200, content=payload)

                        if not session_obj.current_day_timer_started_at:
                            raise HTTPException(status_code=400, detail="Today's timer has not started")

                        now = datetime.now(timezone.utc)
                        was_paused = self._timer_is_paused(session_obj)
                        paused_remaining = max(int(session_obj.paused_remaining_seconds or 0), 0)
                        scheduled_end_before = self._ensure_aware(session_obj.end_time)
                        day_started = self._ensure_aware(session_obj.current_day_timer_started_at)
                        if was_paused and scheduled_end_before and day_started:
                            scheduled_total = max(int((scheduled_end_before - day_started).total_seconds()), 0)
                            day_duration = max(scheduled_total - paused_remaining, 0)
                        else:
                            day_duration = max(int((now - day_started).total_seconds()), 0)

                        # Mark today's slot as ended and increment counter
                        session_obj.current_day_timer_ended_at = now
                        session_obj.days_completed = (session_obj.days_completed or 0) + 1
                        new_days_completed = session_obj.days_completed

                        is_final_day = new_days_completed >= days_total

                        if is_final_day:
                            # Overall session is now complete
                            overall_started = self._ensure_aware(session_obj.timer_started_at)
                            total_elapsed = max(int((now - overall_started).total_seconds()), 0) if overall_started else day_duration
                            session_obj.timer_ended_at = now
                            session_obj.end_time = now
                            session_obj.actual_duration_seconds = total_elapsed
                            session_obj.session_status = 'completed'
                        else:
                            session_obj.end_time = now

                        session_obj.timer_paused_at = None
                        session_obj.paused_remaining_seconds = None
                        if session_obj.end_request_status:
                            session_obj.end_request_status = "completed"
                        session.commit()

                        payment_summary = service.get_payment_summary(session_uuid)
                        payout_success, payout_message = False, "Payout held until all days complete"
                        if is_final_day:
                            payout_success, payout_message, payout_sum = await service.release_payout(session_uuid)
                            try:
                                ConsultationExtensionService(session).release_payouts(session_uuid)
                            except Exception as extension_payout_err:
                                logger.warning(
                                    "Failed to release consultation extension payouts for session %s: %s",
                                    session_uuid,
                                    extension_payout_err,
                                )
                            payment_summary = payout_sum or payment_summary

                        event_type = "timer_ended" if is_final_day else "day_timer_ended"
                        payload = {
                            "type": event_type,
                            "payout_released": payout_success,
                            "payout_message": payout_message,
                            **self._build_timer_state(session_obj, payment_summary, session),
                        }
                        await self.timer_manager.broadcast(session_id, payload)

                        await self._send_day_end_notifications(
                            session=session,
                            session_obj=session_obj,
                            session_id=session_id,
                            referral_id=str(session_obj.referral_request_id),
                            days_completed=new_days_completed,
                            days_total=days_total,
                            is_final_day=is_final_day,
                        )
                        return JSONResponse(status_code=200, content=payload)

                    # ── Single-session (original) behaviour ──────────────────────
                    if session_obj.timer_ended_at:
                        payment_summary = service.get_payment_summary(session_uuid)
                        payload = {
                            "type": "timer_ended",
                            **self._build_timer_state(session_obj, payment_summary, session),
                        }
                        return JSONResponse(status_code=200, content=payload)

                    now = datetime.now(timezone.utc)
                    was_paused = self._timer_is_paused(session_obj)
                    paused_remaining = max(int(session_obj.paused_remaining_seconds or 0), 0)
                    scheduled_end_before = self._ensure_aware(session_obj.end_time)
                    started_at = self._ensure_aware(session_obj.timer_started_at)
                    if was_paused and scheduled_end_before and started_at:
                        scheduled_total = max(int((scheduled_end_before - started_at).total_seconds()), 0)
                        duration_seconds = max(scheduled_total - paused_remaining, 0)
                    else:
                        duration_seconds = max(int((now - started_at).total_seconds()), 0)
                    session_obj.timer_ended_at = now
                    session_obj.end_time = now
                    session_obj.actual_duration_seconds = duration_seconds
                    session_obj.session_status = 'completed'
                    session_obj.timer_paused_at = None
                    session_obj.paused_remaining_seconds = None
                    if session_obj.end_request_status:
                        session_obj.end_request_status = "completed"
                    session.commit()

                    payout_success, payout_message, payout_summary = await service.release_payout(session_uuid)
                    try:
                        ConsultationExtensionService(session).release_payouts(session_uuid)
                    except Exception as extension_payout_err:
                        logger.warning(
                            "Failed to release consultation extension payouts for session %s: %s",
                            session_uuid,
                            extension_payout_err,
                        )
                    payment_summary = payout_summary or service.get_payment_summary(session_uuid)

                    payload = {
                        "type": "timer_ended",
                        "payout_released": payout_success,
                        "payout_message": payout_message,
                        **self._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.timer_manager.broadcast(session_id, payload)

                    referral_id_str = str(session_obj.referral_request_id)
                    client = session.query(User).filter(User.id == session_obj.user_id).first()
                    lawyer = session.query(User).filter(User.id == session_obj.lawyer_id).first()
                    client_name = f"{client.first_name} {client.last_name}" if client else "Your client"
                    lawyer_name = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"

                    # Persist in-app notification rows FIRST. These must always
                    # land regardless of FCM availability so both parties see
                    # the session-ended notification in /notifications/list when
                    # they reopen the app from a killed/background state.
                    try:
                        notif_svc = NotificationService(session)
                        notif_svc.create(
                            recipient_id=session_obj.user_id,
                            title="Consultation Session Ended",
                            body=f"Your session with {lawyer_name} has now ended. We hope it was helpful!",
                            notification_type="session_ended",
                            data={"session_id": session_id, "referral_id": referral_id_str, "screen": "ConsultationSummary"},
                        )
                        notif_svc.create(
                            recipient_id=session_obj.lawyer_id,
                            title="Consultation Session Ended",
                            body=f"Your session with {client_name} has now ended. Thank you for your service.",
                            notification_type="session_ended",
                            data={"session_id": session_id, "referral_id": referral_id_str, "screen": "ConsultationSummary"},
                        )
                        session.commit()
                    except Exception as persist_err:
                        logger.warning(f"Failed to persist session-ended notifications: {persist_err}")

                    # FCM push — isolated per-recipient so a bad/expired token
                    # for one party never blocks delivery to the other.
                    try:
                        firebase = FirebaseNotificationService()
                    except Exception as fb_err:
                        logger.warning(f"Firebase init failed at session end: {fb_err}")
                        firebase = None

                    if firebase is not None:
                        try:
                            user_tokens = session.query(DeviceToken).filter(
                                DeviceToken.user_id == session_obj.user_id,
                                DeviceToken.is_active == True,
                            ).all()
                            if user_tokens:
                                await firebase.send_session_ended_notification(
                                    device_tokens=[t.device_token for t in user_tokens],
                                    other_party_name=lawyer_name,
                                    session_id=session_id,
                                    referral_id=referral_id_str,
                                    recipient_type='user',
                                )
                        except Exception as user_fcm_err:
                            logger.warning(f"User session-ended FCM failed: {user_fcm_err}")

                        try:
                            lawyer_tokens = session.query(DeviceToken).filter(
                                DeviceToken.user_id == session_obj.lawyer_id,
                                DeviceToken.is_active == True,
                            ).all()
                            if lawyer_tokens:
                                await firebase.send_session_ended_notification(
                                    device_tokens=[t.device_token for t in lawyer_tokens],
                                    other_party_name=client_name,
                                    session_id=session_id,
                                    referral_id=referral_id_str,
                                    recipient_type='lawyer',
                                )
                        except Exception as lawyer_fcm_err:
                            logger.warning(f"Lawyer session-ended FCM failed: {lawyer_fcm_err}")

                    return JSONResponse(status_code=200, content=payload)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error ending consultation timer: {exc}")
                raise HTTPException(status_code=500, detail="Error ending consultation timer")

        @router.websocket("/consultation/sessions/{session_id}/timer")
        async def consultation_timer_websocket(
            websocket: WebSocket,
            session_id: str,
            token: str,
        ):
            user_id = None
            try:
                token_data = self.auth.verify_token(token)
                user_id = token_data.get("user_id")
                if not user_id:
                    await websocket.close(code=4001, reason="Invalid token")
                    return

                session_uuid = uuid.UUID(session_id)
                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)
                    service = ConsultationPaymentService(session)
                    payment_summary = service.get_payment_summary(session_uuid)
                    initial_state = {
                        "type": "timer_state",
                        **self._build_timer_state(session_obj, payment_summary, session),
                    }

                await self.timer_manager.connect(session_id, user_id, websocket)
                await websocket.send_text(json.dumps(initial_state))

                while True:
                    raw_message = await websocket.receive_text()
                    try:
                        message = json.loads(raw_message)
                    except json.JSONDecodeError:
                        message = {"type": "sync_request"}

                    if message.get("type") == "sync_request":
                        with self.db.get_session() as session:
                            session_obj = self._require_session(session, session_uuid)
                            service = ConsultationPaymentService(session)
                            payment_summary = service.get_payment_summary(session_uuid)
                            state = {
                                "type": "timer_state",
                                **self._build_timer_state(session_obj, payment_summary, session),
                            }
                        await websocket.send_text(json.dumps(state))

            except WebSocketDisconnect:
                if user_id:
                    self.timer_manager.disconnect(session_id, user_id)
            except Exception as exc:
                logger.error(f"Consultation timer websocket error: {exc}")
                if user_id:
                    self.timer_manager.disconnect(session_id, user_id)
                try:
                    await websocket.close(code=1011, reason="Consultation timer websocket error")
                except Exception:
                    pass

        @router.post("/consultation/stripe/webhook")
        async def consultation_stripe_webhook(request: Request):
            stripe_service = StripeService()
            payload = await request.body()
            signature = request.headers.get("Stripe-Signature")

            verified, message, event = stripe_service.construct_webhook_event(payload, signature)
            if not verified or not event:
                raise HTTPException(status_code=401, detail=message)

            event_type = event.get("type")
            event_object = event.get("data", {}).get("object", {})

            with self.db.get_session() as session:
                service = ConsultationPaymentService(session)
                processed = True
                processing_message = "Event ignored"
                result = None

                if event_type == "payment_intent.succeeded":
                    extension_service = ConsultationExtensionService(session)
                    extension_result = extension_service.mark_paid_by_intent(event_object.get("id"))
                    if extension_result is not None:
                        processed = True
                        processing_message = "Extension payment marked paid and timer resumed"
                        result = extension_result
                        if not extension_result.get("already_processed"):
                            extension_obj = extension_service._require_request(
                                uuid.UUID(extension_result["extension_request_id"])
                            )
                            session_obj = self._require_session(session, extension_obj.session_id)
                            extension = extension_service.serialize(extension_obj)
                            await self._notify_extension(
                                session,
                                recipient_id=session_obj.lawyer_id,
                                title="Extension Payment Received",
                                body=f"The {extension_obj.agreed_minutes}-minute extension is paid and the timer has resumed.",
                                notification_type="extension_payment_confirmed",
                                extension=extension,
                                referral_id=str(session_obj.referral_request_id),
                            )
                            await self.timer_manager.broadcast(
                                str(session_obj.id),
                                {
                                    "type": "timer_extended",
                                    "extension": extension,
                                    **self._build_timer_state(session_obj, None, session),
                                },
                            )
                            self._schedule_extension_warning(session_obj, extension_obj)
                    else:
                        processed, processing_message, result = service.mark_payment_paid_by_intent(
                            event_object.get("id")
                        )
                    # Notify the lawyer via FCM when Stripe confirms payment
                    if extension_result is None and processed and result:
                        try:
                            session_id_str = result.get("session_id")
                            if session_id_str:
                                session_obj = session.query(ReferralSession).filter(
                                    ReferralSession.id == uuid.UUID(session_id_str)
                                ).first()
                                if session_obj:
                                    client = session.query(User).filter(
                                        User.id == session_obj.user_id
                                    ).first()
                                    client_name = (
                                        f"{client.first_name} {client.last_name}"
                                        if client else "Your client"
                                    )
                                    lawyer_tokens = session.query(DeviceToken).filter(
                                        DeviceToken.user_id == session_obj.lawyer_id,
                                        DeviceToken.is_active == True,
                                    ).all()
                                    token_list = [t.device_token for t in lawyer_tokens]
                                    if token_list:
                                        firebase = FirebaseNotificationService()
                                        await firebase.send_payment_confirmed_notification(
                                            device_tokens=token_list,
                                            user_name=client_name,
                                            session_id=session_id_str,
                                            referral_id=str(session_obj.referral_request_id),
                                        )

                                    # Persist in-app notification for the lawyer (webhook path)
                                    with self.db.get_session() as notif_session:
                                        NotificationService(notif_session).create(
                                            recipient_id=session_obj.lawyer_id,
                                            title="Payment Received!",
                                            body=f"{client_name} has paid for the consultation. You're all set to begin.",
                                            notification_type="payment_confirmed",
                                            data={"session_id": session_id_str, "referral_id": str(session_obj.referral_request_id), "screen": "ConsultationSession"},
                                        )
                        except Exception as fcm_err:
                            logger.warning(f"FCM push failed in Stripe webhook: {fcm_err}")
                elif event_type == "account.updated":
                    processed, processing_message, result = service.sync_connect_account(
                        event_object.get("id")
                    )
                elif event_type == "transfer.created":
                    processing_message = "Transfer webhook received"

            return JSONResponse(
                status_code=200,
                content={
                    "received": True,
                    "event_type": event_type,
                    "processed": processed,
                    "message": processing_message,
                    "result": result,
                },
            )

        @router.post("/consultation/stripe/connect-webhook")
        async def consultation_stripe_connect_webhook(request: Request):
            """Handle events emitted for connected lawyer accounts."""
            stripe_service = StripeService()
            payload = await request.body()
            signature = request.headers.get("Stripe-Signature")
            verified, message, event = stripe_service.construct_webhook_event(
                payload, signature, connect=True
            )
            if not verified or not event:
                raise HTTPException(status_code=401, detail=message)

            event_type = event.get("type")
            event_object = event.get("data", {}).get("object", {})
            processed = False
            result = None
            if event_type == "account.updated":
                with self.db.get_session() as session:
                    processed, message, result = ConsultationPaymentService(session).sync_connect_account(
                        event_object.get("id")
                    )
            return {
                "received": True,
                "event_type": event_type,
                "processed": processed,
                "message": message if processed else "Event ignored",
                "result": result,
            }

        @router.get("/consultation/sessions/{session_id}/payment/summary")
        async def get_consultation_payment_summary(session_id: str, request: Request):
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_participant(session_obj, user_id)

                    service = ConsultationPaymentService(session)
                    payment_summary = service.get_payment_summary_with_lazy_platform_fixed_init(session_uuid)
                    if not payment_summary:
                        raise HTTPException(status_code=404, detail="No payment summary found for this session")

                    return payment_summary

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error retrieving consultation payment summary: {exc}")
                raise HTTPException(status_code=500, detail="Error retrieving consultation payment summary")

        return router

    async def _send_one_minute_warning_task(
        self,
        delay_seconds: int,
        session_id: str,
        referral_id: str,
        user_id,
        lawyer_id,
    ) -> None:
        """Background task: send warning 1 minute before the *current* end time.

        If the session gets extended, this task recalculates and waits again so the
        warning is aligned to the latest scheduled end.
        """
        try:
            await asyncio.sleep(delay_seconds)

            while True:
                with self.db.get_session() as session:
                    session_obj = session.query(ReferralSession).filter(
                        ReferralSession.id == uuid.UUID(session_id)
                    ).first()

                    # Don't send if already ended
                    if not session_obj or session_obj.timer_ended_at or session_obj.session_status == 'completed':
                        return

                    now = datetime.now(timezone.utc)
                    if self._timer_is_paused(session_obj):
                        await asyncio.sleep(1)
                        continue
                    end_at = self._ensure_aware(session_obj.end_time)
                    if end_at is None:
                        total_duration = self._get_session_duration_seconds(session, session_obj)
                        started_at = self._ensure_aware(session_obj.timer_started_at)
                        if total_duration is not None and started_at is not None:
                            end_at = started_at + timedelta(seconds=total_duration)
                            session_obj.end_time = end_at
                            session.commit()

                    if end_at is None:
                        return

                    seconds_until_end = int((end_at - now).total_seconds())
                    if seconds_until_end > 60:
                        await asyncio.sleep(seconds_until_end - 60)
                        continue
                    if seconds_until_end <= 0:
                        return

                    # Persist in-app notification rows FIRST so both participants
                    # see the 1-minute warning in /notifications/list even if the
                    # FCM push subsequently fails (network error, expired token,
                    # Firebase not initialised, etc.).
                    try:
                        notif_svc = NotificationService(session)
                        warning_data = {
                            "session_id": session_id,
                            "referral_id": referral_id,
                            "screen": "ConsultationSession",
                        }
                        for recipient in (session_obj.user_id, session_obj.lawyer_id):
                            notif_svc.create(
                                recipient_id=recipient,
                                title="1 Minute Remaining",
                                body="Your consultation session will end in 1 minute.",
                                notification_type="one_minute_warning",
                                data=warning_data,
                            )
                        session.commit()
                    except Exception as persist_err:
                        logger.warning(
                            f"Failed to persist 1-minute warning notification for session {session_id}: {persist_err}"
                        )

                    # FCM push (best-effort, isolated from persistence).
                    # Each recipient's send is wrapped in its own try/except so
                    # a single bad token can't prevent the other participant from
                    # being notified.
                    try:
                        firebase = FirebaseNotificationService()
                    except Exception as fb_err:
                        logger.warning(f"Firebase init failed for 1-minute warning: {fb_err}")
                        firebase = None

                    if firebase is not None:
                        try:
                            user_tokens = session.query(DeviceToken).filter(
                                DeviceToken.user_id == user_id,
                                DeviceToken.is_active == True,
                            ).all()
                            if user_tokens:
                                await firebase.send_one_minute_warning_notification(
                                    device_tokens=[t.device_token for t in user_tokens],
                                    session_id=session_id,
                                    referral_id=referral_id,
                                )
                        except Exception as user_fcm_err:
                            logger.warning(
                                f"User 1-minute FCM failed for session {session_id}: {user_fcm_err}"
                            )

                        try:
                            lawyer_tokens = session.query(DeviceToken).filter(
                                DeviceToken.user_id == lawyer_id,
                                DeviceToken.is_active == True,
                            ).all()
                            if lawyer_tokens:
                                await firebase.send_one_minute_warning_notification(
                                    device_tokens=[t.device_token for t in lawyer_tokens],
                                    session_id=session_id,
                                    referral_id=referral_id,
                                )
                        except Exception as lawyer_fcm_err:
                            logger.warning(
                                f"Lawyer 1-minute FCM failed for session {session_id}: {lawyer_fcm_err}"
                            )

                    # Foreground case: websocket event for in-app dialog.
                    warning_payload = {
                        "type": "one_minute_warning",
                        "session_id": session_id,
                        "referral_id": referral_id,
                        "can_extend": True,
                        "allowed_extension_minutes": (
                            [5, 10, 20, 30]
                            if session_obj.days_total is not None
                            else [5, 10, 20, 30, 60]
                        ),
                    }
                    await self.timer_manager.broadcast(session_id, warning_payload)
                    logger.info(f"Sent 1-minute warning for session {session_id}")
                    return
        except Exception as exc:
            logger.warning(f"1-minute warning task failed for session {session_id}: {exc}")

    async def _auto_end_session_task(
        self,
        delay_seconds: int,
        session_id: str,
        referral_id: str,
        user_id,
        lawyer_id,
    ) -> None:
        """Background task: auto-end the current timer slot when its time expires.

        For single-session types this ends the whole session.
        For multi-day ongoing types this ends today's 30-minute slot; the full
        session only ends when the final day's slot is auto-ended.

        Fires even when both apps are killed, ensuring FCM push notifications
        are delivered and the DB is updated correctly.
        """
        try:
            await asyncio.sleep(delay_seconds)

            while True:
                with self.db.get_session() as session:
                    session_obj = session.query(ReferralSession).filter(
                        ReferralSession.id == uuid.UUID(session_id)
                    ).first()

                    if not session_obj:
                        return

                    days_total = session_obj.days_total
                    is_ongoing = days_total is not None

                    if is_ongoing:
                        # Skip if this day's slot was already ended manually
                        if session_obj.current_day_timer_ended_at or session_obj.session_status == 'completed':
                            logger.info(f"Auto-end skipped for session {session_id} day {session_obj.days_completed}: already ended")
                            return
                    else:
                        # Skip if whole session was already ended manually
                        if session_obj.timer_ended_at or session_obj.session_status == 'completed':
                            logger.info(f"Auto-end skipped for session {session_id}: already completed")
                            return

                    now = datetime.now(timezone.utc)
                    if self._timer_is_paused(session_obj):
                        await asyncio.sleep(1)
                        continue
                    end_at = self._ensure_aware(session_obj.end_time)
                    if end_at is not None and end_at > now:
                        await asyncio.sleep(max(int((end_at - now).total_seconds()), 1))
                        continue
                    # Spec 6.12: backend is authoritative — force-end any call
                    # still connected when the real timer hits zero, rather
                    # than relying on the client to notice and hang up.
                    active_call = session.query(Call).filter(
                        Call.referral_session_id == uuid.UUID(session_id),
                        Call.status.in_(("accepted", "connected")),
                    ).first()
                    if active_call:
                        active_call.status = "ended"
                        active_call.ended_at = now
                        session_obj.current_communication_mode = "chat"
                        session.flush()
                        
                        

                    # ── Ongoing: end today's slot ────────────────────────────────
                    if is_ongoing:
                        day_started = self._ensure_aware(session_obj.current_day_timer_started_at)
                        session_obj.current_day_timer_ended_at = now
                        session_obj.days_completed = (session_obj.days_completed or 0) + 1
                        new_days_completed = session_obj.days_completed
                        is_final_day = new_days_completed >= days_total

                        if is_final_day:
                            overall_started = self._ensure_aware(session_obj.timer_started_at)
                            total_elapsed = max(int((now - overall_started).total_seconds()), 0) if overall_started else delay_seconds
                            session_obj.timer_ended_at = now
                            session_obj.end_time = now
                            session_obj.actual_duration_seconds = total_elapsed
                            session_obj.session_status = 'completed'
                        else:
                            session_obj.end_time = now

                        session.commit()

                        service = ConsultationPaymentService(session)
                        payout_success, payout_message = False, "Payout held until all days complete"
                        payment_summary = service.get_payment_summary(uuid.UUID(session_id))
                        if is_final_day:
                            payout_success, payout_message, payout_sum = await service.release_payout(uuid.UUID(session_id))
                            try:
                                ConsultationExtensionService(session).release_payouts(uuid.UUID(session_id))
                            except Exception as extension_payout_err:
                                logger.warning(
                                    "Failed to release consultation extension payouts for session %s: %s",
                                    session_id,
                                    extension_payout_err,
                                )
                            payment_summary = payout_sum or payment_summary

                        event_type = "timer_ended" if is_final_day else "day_timer_ended"
                        payload = {
                            "type": event_type,
                            "payout_released": payout_success,
                            "payout_message": payout_message,
                            "auto_ended": True,
                            **self._build_timer_state(session_obj, payment_summary, session),
                        }
                        await self.timer_manager.broadcast(session_id, payload)

                        await self._send_day_end_notifications(
                            session=session,
                            session_obj=session_obj,
                            session_id=session_id,
                            referral_id=referral_id,
                            days_completed=new_days_completed,
                            days_total=days_total,
                            is_final_day=is_final_day,
                        )
                        logger.info(
                            f"Auto-ended day {new_days_completed}/{days_total} for session {session_id}, "
                            f"final={is_final_day}"
                        )
                        return

                    # ── Single-session: end the whole thing ───────────────────────
                    started_at = self._ensure_aware(session_obj.timer_started_at)
                    duration_seconds = (
                        max(int((now - started_at).total_seconds()), 0)
                        if started_at else delay_seconds
                    )
                    session_obj.timer_ended_at = now
                    session_obj.end_time = now
                    session_obj.actual_duration_seconds = duration_seconds
                    session_obj.session_status = 'completed'
                    session.commit()

                    service = ConsultationPaymentService(session)
                    payout_success, payout_message, payout_summary = await service.release_payout(uuid.UUID(session_id))
                    try:
                        ConsultationExtensionService(session).release_payouts(uuid.UUID(session_id))
                    except Exception as extension_payout_err:
                        logger.warning(
                            "Failed to release consultation extension payouts for session %s: %s",
                            session_id,
                            extension_payout_err,
                        )
                    payment_summary = payout_summary or service.get_payment_summary(uuid.UUID(session_id))

                    payload = {
                        "type": "timer_ended",
                        "payout_released": payout_success,
                        "payout_message": payout_message,
                        "auto_ended": True,
                        **self._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.timer_manager.broadcast(session_id, payload)

                    client = session.query(User).filter(User.id == user_id).first()
                    lawyer = session.query(User).filter(User.id == lawyer_id).first()
                    client_name = f"{client.first_name} {client.last_name}" if client else "Your client"
                    lawyer_name = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"

                    # Persist in-app notification rows FIRST so both parties see
                    # the session-ended notification when they reopen the app,
                    # even if FCM delivery fails or both apps are killed.
                    try:
                        notif_svc = NotificationService(session)
                        notif_svc.create(
                            recipient_id=session_obj.user_id,
                            title="Consultation Session Ended",
                            body=f"Your session with {lawyer_name} has now ended. We hope it was helpful!",
                            notification_type="session_ended",
                            data={"session_id": session_id, "referral_id": referral_id, "screen": "ConsultationSummary"},
                        )
                        notif_svc.create(
                            recipient_id=session_obj.lawyer_id,
                            title="Consultation Session Ended",
                            body=f"Your session with {client_name} has now ended. Thank you for your service.",
                            notification_type="session_ended",
                            data={"session_id": session_id, "referral_id": referral_id, "screen": "ConsultationSummary"},
                        )
                        session.commit()
                    except Exception as persist_err:
                        logger.warning(
                            f"Failed to persist session-ended notifications for session {session_id}: {persist_err}"
                        )

                    # FCM push — isolated per-recipient.
                    user_token_count = 0
                    lawyer_token_count = 0
                    try:
                        firebase = FirebaseNotificationService()
                    except Exception as fb_err:
                        logger.warning(f"Firebase init failed in auto-end task: {fb_err}")
                        firebase = None

                    if firebase is not None:
                        try:
                            user_tokens = session.query(DeviceToken).filter(
                                DeviceToken.user_id == user_id,
                                DeviceToken.is_active == True,
                            ).all()
                            user_token_count = len(user_tokens)
                            if user_tokens:
                                await firebase.send_session_ended_notification(
                                    device_tokens=[t.device_token for t in user_tokens],
                                    other_party_name=lawyer_name,
                                    session_id=session_id,
                                    referral_id=referral_id,
                                    recipient_type='user',
                                )
                        except Exception as user_fcm_err:
                            logger.warning(
                                f"User session-ended FCM failed for session {session_id}: {user_fcm_err}"
                            )

                        try:
                            lawyer_tokens = session.query(DeviceToken).filter(
                                DeviceToken.user_id == lawyer_id,
                                DeviceToken.is_active == True,
                            ).all()
                            lawyer_token_count = len(lawyer_tokens)
                            if lawyer_tokens:
                                await firebase.send_session_ended_notification(
                                    device_tokens=[t.device_token for t in lawyer_tokens],
                                    other_party_name=client_name,
                                    session_id=session_id,
                                    referral_id=referral_id,
                                    recipient_type='lawyer',
                                )
                        except Exception as lawyer_fcm_err:
                            logger.warning(
                                f"Lawyer session-ended FCM failed for session {session_id}: {lawyer_fcm_err}"
                            )

                    logger.info(
                        f"Auto-ended session {session_id}: payout_released={payout_success}, "
                        f"user_tokens={user_token_count}, lawyer_tokens={lawyer_token_count}"
                    )
                    return

        except Exception as exc:
            logger.warning(f"Auto-end task failed for session {session_id}: {exc}")

    def _get_request_user_id(self, request: Request) -> str:
        token_data = self.auth.verify_request_token(request)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id

    @staticmethod
    def _session_is_complete(session_obj: ReferralSession) -> bool:
        return session_obj.session_status == "completed" or session_obj.timer_ended_at is not None

    def _get_existing_survey(self, session, session_id: uuid.UUID, user_id: str) -> Optional[ConsultationSurvey]:
        return session.query(ConsultationSurvey).filter(
            ConsultationSurvey.session_id == session_id,
            ConsultationSurvey.user_id == uuid.UUID(user_id),
        ).first()

    @staticmethod
    def _survey_reviewer_role(session_obj: ReferralSession, user_id: str) -> str:
        return "client" if str(session_obj.user_id) == user_id else "lawyer"

    @staticmethod
    def _clean_survey_issue_tags(issue_tags: Optional[List[str]]) -> List[str]:
        cleaned: List[str] = []
        seen = set()
        for tag in issue_tags or []:
            normalized = str(tag or "").strip()
            if not normalized or normalized in seen:
                continue
            cleaned.append(normalized[:80])
            seen.add(normalized)
        return cleaned

    def _sync_consultation_lawyer_review(
        self,
        *,
        session,
        session_obj: ReferralSession,
        reviewer_id: str,
        lawyer_rating: Optional[int],
        lawyer_comment: Optional[str],
    ) -> None:
        if not lawyer_rating:
            return
        if self._survey_reviewer_role(session_obj, reviewer_id) != "client":
            return

        reviewer_uuid = uuid.UUID(reviewer_id)
        lawyer_uuid = session_obj.lawyer_id
        reviewer = session.query(User).filter(User.id == reviewer_uuid).first()
        reviewer_name = self._reviewer_display_name(reviewer)
        review_text = (lawyer_comment or "").strip() or None

        existing_review = session.query(LawyerReview).filter(
            LawyerReview.reviewer_id == reviewer_uuid,
            LawyerReview.lawyer_id == lawyer_uuid,
        ).first()

        if existing_review:
            existing_review.rating = lawyer_rating
            existing_review.review_text = review_text
            existing_review.reviewer_name = reviewer_name
        else:
            session.add(LawyerReview(
                reviewer_id=reviewer_uuid,
                lawyer_id=lawyer_uuid,
                rating=lawyer_rating,
                review_text=review_text,
                reviewer_name=reviewer_name,
            ))

        session.flush()
        self.review_service._update_lawyer_stats(session, str(lawyer_uuid))

    @staticmethod
    def _reviewer_display_name(user: Optional[User]) -> str:
        if not user:
            return "Anonymous"
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return (
            full_name
            or getattr(user, "user_name", None)
            or getattr(user, "firm_name", None)
            or getattr(user, "organisation_name", None)
            or "Anonymous"
        )

    def _serialize_survey(self, survey: ConsultationSurvey) -> Dict[str, Any]:
        issue_tags = survey.issue_tags or []
        skipped = survey.overall_rating == 0 or "skipped" in issue_tags
        return {
            "id": str(survey.id),
            "session_id": str(survey.session_id),
            "referral_id": str(survey.referral_request_id),
            "user_id": str(survey.user_id),
            "lawyer_id": str(survey.lawyer_id),
            "reviewer_role": survey.reviewer_role,
            "overall_rating": survey.overall_rating,
            "lawyer_rating": survey.lawyer_rating,
            "platform_rating": survey.platform_rating,
            "was_helpful": survey.was_helpful,
            "would_recommend": survey.would_recommend,
            "comment": survey.comment,
            "issue_tags": issue_tags,
            "skipped": skipped,
            "created_at": survey.created_at.isoformat(),
            "updated_at": survey.updated_at.isoformat() if survey.updated_at else None,
        }

    def _build_survey_status(
        self,
        session_obj: ReferralSession,
        survey: Optional[ConsultationSurvey] = None,
    ) -> Dict[str, Any]:
        submitted = survey is not None
        skipped = bool(
            survey
            and (
                survey.overall_rating == 0
                or "skipped" in (survey.issue_tags or [])
            )
        )
        return {
            "session_id": str(session_obj.id),
            "session_status": session_obj.session_status,
            "can_submit": self._session_is_complete(session_obj) and not submitted,
            "submitted": submitted,
            "skipped": skipped,
            "survey": self._serialize_survey(survey) if survey else None,
        }

    def _extract_raw_token(self, request: Request) -> Optional[str]:
        """Return the raw JWT string from the Authorization header, or None."""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header.split(" ", 1)[1]
        return None

    @staticmethod
    def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
        """Return dt with UTC timezone attached if it is naive (e.g. read from a
        TIMESTAMP (no tz) column before the schema was migrated to TIMESTAMPTZ)."""
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _timer_is_paused(session_obj: ReferralSession) -> bool:
        return isinstance(getattr(session_obj, "timer_paused_at", None), datetime)

    def _remaining_seconds_for_session(self, db_session, session_obj: ReferralSession) -> Optional[int]:
        if self._timer_is_paused(session_obj):
            remaining = getattr(session_obj, "paused_remaining_seconds", None)
            return max(int(remaining or 0), 0)

        now = datetime.now(timezone.utc)
        scheduled_end = self._ensure_aware(session_obj.end_time)
        if scheduled_end is not None:
            return max(int((scheduled_end - now).total_seconds()), 0)

        duration = (
            30 * 60
            if session_obj.days_total is not None
            else self._get_session_duration_seconds(db_session, session_obj)
        )
        started_at = self._ensure_aware(
            session_obj.current_day_timer_started_at
            if session_obj.days_total is not None
            else session_obj.timer_started_at
        )
        if duration is None or started_at is None:
            return None

        elapsed = max(int((now - started_at).total_seconds()), 0)
        return max(duration - elapsed, 0)

    def _has_consultation_time_remaining(self, db_session, session_obj: ReferralSession) -> bool:
        remaining = self._remaining_seconds_for_session(db_session, session_obj)
        return remaining is None or remaining > 0

    def _resume_paused_timer(self, session_obj: ReferralSession) -> None:
        if not self._timer_is_paused(session_obj):
            return

        remaining = max(int(session_obj.paused_remaining_seconds or 0), 0)
        now = datetime.now(timezone.utc)
        if session_obj.end_time is not None:
            session_obj.end_time = now + timedelta(seconds=remaining)
        session_obj.timer_paused_at = None
        session_obj.paused_remaining_seconds = None

    @staticmethod
    def _validate_consultation_end_answers(data: ConsultationEndAnswersRequest) -> Dict[str, Any]:
        questions_addressed = str(data.questions_addressed or "").strip()
        next_steps_understood = str(data.next_steps_understood or "").strip() or None
        final_clarification = str(data.final_clarification or "").strip() or None

        if questions_addressed not in {"yes_fully", "partially", "no"}:
            raise ValueError("questions_addressed must be yes_fully, partially, or no")

        if questions_addressed == "no":
            next_steps_understood = None
            final_clarification = None
        else:
            if next_steps_understood not in {"yes", "not_sure", "no_next_steps_required"}:
                raise ValueError("next_steps_understood must be yes, not_sure, or no_next_steps_required")
            if final_clarification not in {"happy_to_end", "another_question"}:
                raise ValueError("final_clarification must be happy_to_end or another_question")

        return {
            "questions_addressed": questions_addressed,
            "next_steps_understood": next_steps_understood,
            "final_clarification": final_clarification,
        }

    def _build_consultation_end_state(self, session_obj: ReferralSession) -> Optional[Dict[str, Any]]:
        status = getattr(session_obj, "end_request_status", None)
        if not status:
            return None

        return {
            "status": status,
            "requested_at": (
                self._ensure_aware(session_obj.end_requested_at).isoformat()
                if session_obj.end_requested_at else None
            ),
            "requested_by": str(session_obj.end_requested_by) if session_obj.end_requested_by else None,
            "answers": session_obj.end_conclusion_answers,
            "answered_at": (
                self._ensure_aware(session_obj.end_conclusion_completed_at).isoformat()
                if session_obj.end_conclusion_completed_at else None
            ),
        }

    async def _notify_consultation_end_requested(
        self,
        *,
        session,
        session_obj: ReferralSession,
        session_id: str,
    ) -> None:
        referral_id = str(session_obj.referral_request_id)
        try:
            NotificationService(session).create(
                recipient_id=session_obj.user_id,
                title="Consultation End Requested",
                body="Your professional is ready to end this consultation. Please answer a few questions first.",
                notification_type="consultation_end_requested",
                data={
                    "session_id": session_id,
                    "referral_id": referral_id,
                    "screen": "ConsultationSession",
                },
            )
            session.commit()
        except Exception as persist_err:
            logger.warning(f"Failed to persist consultation-end request notification: {persist_err}")

        try:
            user_tokens = session.query(DeviceToken).filter(
                DeviceToken.user_id == session_obj.user_id,
                DeviceToken.is_active == True,
            ).all()
            if user_tokens:
                firebase = FirebaseNotificationService()
                await firebase.send_notification_to_multiple(
                    device_tokens=[t.device_token for t in user_tokens],
                    title="Consultation End Requested",
                    body="Please answer a few questions before the consultation ends.",
                    data={
                        "type": "consultation_end_requested",
                        "session_id": session_id,
                        "referral_id": referral_id,
                        "screen": "ConsultationSession",
                    },
                    badge=1,
                    sound="default",
                    channel_id="high_importance_channel",
                )
        except Exception as fcm_err:
            logger.warning(f"Consultation-end request FCM failed: {fcm_err}")

    def _require_lawyer(self, session, user_id: str) -> User:
        lawyer = session.query(User).filter(
            User.id == uuid.UUID(user_id),
            User.professional_type.in_(['Lawyer', 'Law Firm']),
        ).first()
        if not lawyer:
            raise HTTPException(status_code=403, detail="Access denied. Lawyers and law firms only.")
        require_identity_verification(session, lawyer)
        return lawyer

    def _require_session(self, session, session_id: uuid.UUID) -> ReferralSession:
        session_obj = session.query(ReferralSession).filter(ReferralSession.id == session_id).first()
        if not session_obj:
            raise ValueError("Consultation session not found")
        return session_obj

    def _assert_session_participant(self, session_obj: ReferralSession, user_id: str) -> None:
        if str(session_obj.user_id) != user_id and str(session_obj.lawyer_id) != user_id:
            raise ValueError("You do not have access to this consultation session")

    def _assert_session_lawyer(self, session_obj: ReferralSession, user_id: str) -> None:
        if str(session_obj.lawyer_id) != user_id:
            raise ValueError("Only the assigned lawyer can perform this action")

    def _assert_session_user(self, session_obj: ReferralSession, user_id: str) -> None:
        if str(session_obj.user_id) != user_id:
            raise ValueError("Only the client can confirm this consultation payment")

    def _assert_two_way_consultation_messages(self, session, session_obj: ReferralSession) -> None:
        generated_message_types = ("chat_history", "service_request_manuscript")
        rows = session.query(InboxMessage.sender_type).filter(
            InboxMessage.referral_request_id == session_obj.referral_request_id,
            InboxMessage.sender_type.in_(("user", "lawyer")),
            ~InboxMessage.message_type.in_(generated_message_types),
        ).all()
        senders = {row[0] for row in rows}
        if not {"user", "lawyer"}.issubset(senders):
            raise HTTPException(
                status_code=400,
                detail="Both the client and lawyer must send a message before the consultation timer can start",
            )

    # Per-slot duration for single-session types only (seconds).
    # Ongoing types are NOT listed here — their slot duration is always 30 min
    # and is handled via _get_session_days_total / start endpoint logic.
    _CONSULTATION_DURATION_SECONDS: Dict[str, int] = {
        "single_5_min":  5  * 60,
        "single_10_min": 10 * 60,
        "single_20_min": 20 * 60,
        "single_30_min": 30 * 60,
        "single_1_hour": 60 * 60,
    }

    # How many daily 30-minute sessions each ongoing type requires.
    _ONGOING_DAYS: Dict[str, int] = {
        "ongoing_3_day": 3,
        "ongoing_5_day": 5,
    }

    def _get_session_duration_seconds(self, db_session, session_obj: ReferralSession) -> Optional[int]:
        """Return the per-slot duration in seconds for single-session types, or None for ongoing/open-ended."""
        from services.models import ReferralRequest as _RR
        referral = db_session.query(_RR).filter(_RR.id == session_obj.referral_request_id).first()
        if not referral:
            return None
        return self._CONSULTATION_DURATION_SECONDS.get(referral.consultation_type)

    def _get_session_days_total(self, db_session, session_obj: ReferralSession) -> Optional[int]:
        """Return the total number of daily sessions for ongoing types, or None for single/casework."""
        from services.models import ReferralRequest as _RR
        referral = db_session.query(_RR).filter(_RR.id == session_obj.referral_request_id).first()
        if not referral:
            return None
        return self._ONGOING_DAYS.get(referral.consultation_type)

    def _build_timer_state(
        self,
        session_obj: ReferralSession,
        payment_summary: Optional[Dict[str, Any]] = None,
        db_session=None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        is_paused = self._timer_is_paused(session_obj)
        paused_remaining = getattr(session_obj, "paused_remaining_seconds", 0)
        if not isinstance(paused_remaining, int):
            paused_remaining = 0
        days_total: Optional[int] = session_obj.days_total
        days_completed: int = session_obj.days_completed or 0
        is_ongoing = days_total is not None

        # For ongoing: elapsed/remaining are relative to the current day's slot.
        # For single: relative to the overall session.
        if is_ongoing:
            day_started = self._ensure_aware(session_obj.current_day_timer_started_at)
            day_ended = self._ensure_aware(session_obj.current_day_timer_ended_at)
            slot_duration = 30 * 60
            if day_started and day_ended:
                elapsed_seconds = max(int((day_ended - day_started).total_seconds()), 0)
            elif day_started:
                elapsed_seconds = max(int((now - day_started).total_seconds()), 0)
            else:
                elapsed_seconds = 0
            scheduled_end = self._ensure_aware(session_obj.end_time)
            remaining_seconds: Optional[int] = None
            if is_paused:
                remaining_seconds = max(paused_remaining, 0)
            elif day_ended is None and scheduled_end is not None:
                remaining_seconds = max(int((scheduled_end - now).total_seconds()), 0)
            elif day_ended is not None:
                remaining_seconds = 0
            total_duration_seconds: Optional[int] = slot_duration
        else:
            started_at = self._ensure_aware(session_obj.timer_started_at)
            ended_at = self._ensure_aware(session_obj.timer_ended_at)
            if started_at and ended_at:
                elapsed_seconds = max(int((ended_at - started_at).total_seconds()), 0)
            elif started_at:
                elapsed_seconds = max(int((now - started_at).total_seconds()), 0)
            else:
                elapsed_seconds = 0

            total_duration_seconds = None
            if db_session is not None:
                total_duration_seconds = self._get_session_duration_seconds(db_session, session_obj)

            scheduled_end = self._ensure_aware(session_obj.end_time)
            if scheduled_end is not None and started_at is not None and ended_at is None:
                total_duration_seconds = max(int((scheduled_end - started_at).total_seconds()), 0)

            remaining_seconds = None
            if is_paused:
                remaining_seconds = max(paused_remaining, 0)
            elif ended_at is None and scheduled_end is not None:
                remaining_seconds = max(int((scheduled_end - now).total_seconds()), 0)
            elif total_duration_seconds is not None:
                remaining_seconds = max(0, total_duration_seconds - elapsed_seconds)

        # session_end_date: when the final timer slot ends (or is expected to end)
        if is_ongoing:
            ended_at = self._ensure_aware(session_obj.timer_ended_at)
            scheduled_end = self._ensure_aware(session_obj.end_time)
        else:
            ended_at = self._ensure_aware(session_obj.timer_ended_at)
            started_at = self._ensure_aware(session_obj.timer_started_at)

        session_end_date: Optional[str] = None
        if ended_at is not None:
            session_end_date = ended_at.isoformat()
        elif scheduled_end is not None:
            session_end_date = scheduled_end.isoformat()
        elif not is_ongoing and started_at is not None and total_duration_seconds is not None:
            session_end_date = (started_at + timedelta(seconds=total_duration_seconds)).isoformat()

        state: Dict[str, Any] = {
            "session_id": str(session_obj.id),
            "session_status": session_obj.session_status,
            "timer_started_at": session_obj.timer_started_at.isoformat() if session_obj.timer_started_at else None,
            "timer_ended_at": session_obj.timer_ended_at.isoformat() if session_obj.timer_ended_at else None,
            "session_end_date": session_end_date,
            "actual_duration_seconds": session_obj.actual_duration_seconds,
            "elapsed_seconds": elapsed_seconds,
            "total_duration_seconds": total_duration_seconds,
            "remaining_seconds": remaining_seconds,
            "payment_status": payment_summary.get("payment_status") if payment_summary else None,
            "server_time": now.isoformat(),
            "timer_paused": is_paused,
            "timer_paused_at": (
                self._ensure_aware(session_obj.timer_paused_at).isoformat()
                if is_paused else None
            ),
        }

        end_state = self._build_consultation_end_state(session_obj)
        if end_state is not None:
            state["end_consultation"] = end_state

        # Multi-day fields (only populated for ongoing sessions)
        if is_ongoing:
            current_day = days_completed + (0 if session_obj.current_day_timer_ended_at else 1)
            current_day = min(current_day, days_total)
            state.update({
                "is_ongoing": True,
                "days_total": days_total,
                "days_completed": days_completed,
                "current_day": current_day,
                "is_final_day": current_day >= days_total,
                "current_day_timer_started_at": (
                    session_obj.current_day_timer_started_at.isoformat()
                    if session_obj.current_day_timer_started_at else None
                ),
                "current_day_timer_ended_at": (
                    session_obj.current_day_timer_ended_at.isoformat()
                    if session_obj.current_day_timer_ended_at else None
                ),
            })
        else:
            state["is_ongoing"] = False

        return state

    async def _notify_extension(
        self,
        session,
        *,
        recipient_id,
        title: str,
        body: str,
        notification_type: str,
        extension: Dict[str, Any],
        referral_id: str,
    ) -> None:
        """Persist first, then best-effort FCM delivery for extension events."""
        data = {
            "type": notification_type,
            "session_id": extension["session_id"],
            "referral_id": referral_id,
            "extension_request_id": extension["id"],
            "extension_status": extension["status"],
            "screen": "ConsultationSession",
            "action": "view_extension",
        }
        NotificationService(session).create(
            recipient_id=recipient_id,
            title=title,
            body=body,
            notification_type=notification_type,
            data=data,
        )
        session.commit()
        try:
            tokens = session.query(DeviceToken).filter(
                DeviceToken.user_id == recipient_id,
                DeviceToken.is_active == True,
            ).all()
            if tokens:
                await FirebaseNotificationService().send_notification_to_multiple(
                    device_tokens=[token.device_token for token in tokens],
                    title=title,
                    body=body,
                    data=data,
                    badge=1,
                    sound="default",
                    channel_id="high_importance_channel",
                )
        except Exception as exc:
            logger.warning("Extension FCM delivery failed: %s", exc)

    def _schedule_extension_warning(self, session_obj, extension_obj) -> None:
        """Schedule the next one-minute warning after a successfully paid extension."""
        end_time = self._ensure_aware(session_obj.end_time)
        if not end_time:
            return
        remaining = max(int((end_time - datetime.now(timezone.utc)).total_seconds()), 0)
        if remaining <= 60:
            return
        asyncio.create_task(
            self._send_one_minute_warning_task(
                delay_seconds=remaining - 60,
                session_id=str(session_obj.id),
                referral_id=str(session_obj.referral_request_id),
                user_id=session_obj.user_id,
                lawyer_id=session_obj.lawyer_id,
            )
        )

    async def _send_day_end_notifications(
        self,
        session,
        session_obj: ReferralSession,
        session_id: str,
        referral_id: str,
        days_completed: int,
        days_total: int,
        is_final_day: bool,
    ) -> None:
        """Send FCM push + in-app notifications when a daily slot ends.

        On intermediate days: 'Day X of Y completed' push.
        On the final day: the standard 'session ended' push + in-app records.
        """
        try:
            firebase = FirebaseNotificationService()
            client = session.query(User).filter(User.id == session_obj.user_id).first()
            lawyer = session.query(User).filter(User.id == session_obj.lawyer_id).first()
            client_name = f"{client.first_name} {client.last_name}" if client else "Your client"
            lawyer_name = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"

            user_tokens = [
                t.device_token for t in session.query(DeviceToken).filter(
                    DeviceToken.user_id == session_obj.user_id,
                    DeviceToken.is_active == True,
                ).all()
            ]
            lawyer_tokens = [
                t.device_token for t in session.query(DeviceToken).filter(
                    DeviceToken.user_id == session_obj.lawyer_id,
                    DeviceToken.is_active == True,
                ).all()
            ]

            if is_final_day:
                if user_tokens:
                    await firebase.send_session_ended_notification(
                        device_tokens=user_tokens,
                        other_party_name=lawyer_name,
                        session_id=session_id,
                        referral_id=referral_id,
                        recipient_type='user',
                    )
                if lawyer_tokens:
                    await firebase.send_session_ended_notification(
                        device_tokens=lawyer_tokens,
                        other_party_name=client_name,
                        session_id=session_id,
                        referral_id=referral_id,
                        recipient_type='lawyer',
                    )
                notif_svc = NotificationService(session)
                notif_svc.create(
                    recipient_id=session_obj.user_id,
                    title="Consultation Complete",
                    body=f"All {days_total} sessions with {lawyer_name} have been completed. We hope it was helpful!",
                    notification_type="session_ended",
                    data={"session_id": session_id, "referral_id": referral_id, "screen": "ConsultationSummary"},
                )
                notif_svc.create(
                    recipient_id=session_obj.lawyer_id,
                    title="Consultation Complete",
                    body=f"All {days_total} sessions with {client_name} have been completed. Thank you for your service.",
                    notification_type="session_ended",
                    data={"session_id": session_id, "referral_id": referral_id, "screen": "ConsultationSummary"},
                )
            else:
                remaining_days = days_total - days_completed
                if user_tokens:
                    await firebase.send_day_completed_notification(
                        device_tokens=user_tokens,
                        other_party_name=lawyer_name,
                        day_number=days_completed,
                        days_total=days_total,
                        remaining_days=remaining_days,
                        session_id=session_id,
                        referral_id=referral_id,
                    )
                if lawyer_tokens:
                    await firebase.send_day_completed_notification(
                        device_tokens=lawyer_tokens,
                        other_party_name=client_name,
                        day_number=days_completed,
                        days_total=days_total,
                        remaining_days=remaining_days,
                        session_id=session_id,
                        referral_id=referral_id,
                    )

                # Persist in-app notification rows for both participants
                notif_svc = NotificationService(session)
                day_data = {
                    "session_id": session_id,
                    "referral_id": referral_id,
                    "day_number": str(days_completed),
                    "days_total": str(days_total),
                    "remaining_days": str(remaining_days),
                    "screen": "ConsultationSummary",
                }
                day_suffix = "day" if remaining_days == 1 else "days"
                notif_svc.create(
                    recipient_id=session_obj.user_id,
                    title=f"Day {days_completed} of {days_total} Completed",
                    body=(
                        f"Your Day {days_completed} session with {lawyer_name} has ended. "
                        f"{remaining_days} {day_suffix} remaining."
                    ),
                    notification_type="day_completed",
                    data=day_data,
                )
                notif_svc.create(
                    recipient_id=session_obj.lawyer_id,
                    title=f"Day {days_completed} of {days_total} Completed",
                    body=(
                        f"Your Day {days_completed} session with {client_name} has ended. "
                        f"{remaining_days} {day_suffix} remaining."
                    ),
                    notification_type="day_completed",
                    data=day_data,
                )
                session.commit()
        except Exception as err:
            logger.warning(f"Day-end notifications failed for session {session_id}: {err}")
