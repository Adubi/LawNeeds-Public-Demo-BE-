from fastapi import HTTPException, APIRouter, Request, Body, Query, Form, File, UploadFile
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import logging
import uuid
from datetime import datetime, timedelta, timezone
from services.models import (
    ReferralRequest, ReferralSession, LawyerAvailability, ConsultationPricing,
    ConsultationType, ConsultationCategory, BudgetRange, ReferralStatus, UrgencyLevel, User,
    LawyerRecommendation, LawyerInbox, InboxMessage, DeviceToken, ConsultationBookingStatus,
    ConsultationBookingRequest
)
from services.booking_service import (
    BookingService,
    consultation_duration_minutes,
    parse_datetime,
    requires_booking_slot,
)
from services.notification_service import NotificationService
from services.redis_service import RedisService
from services.referral_user_status_payload import build_referral_user_status_payload
from services.referral_user_status_hub import referral_user_status_hub
from services.referral_consultation_pricing_display import (
    consultation_category_options,
    single_consultation_duration_options,
    single_consultation_confirmation_fragment,
    casework_service_options,
    casework_budget_options,
    casework_price_label,
    validate_casework_budget,
    CASEWORK_MIN_BUDGET,
    CASEWORK_PRICE_BY_SERVICE,
    CASEWORK_LABEL_BY_SERVICE,
    CASEWORK_SERVICE_VALUES,
)
from services.referral_confirmation_builder import build_referral_confirmation_parts
from services.s3_service import S3Service
from services.upload_multipart import (
    infer_referral_file_type,
    collect_upload_files_from_form,
    merge_referral_upload_into_context,
    build_referral_attachment_metadata,
    attachment_confirmation_lines,
    try_pdf_document_title,
    content_sha256_hex,
    names_from_referral_context,
    check_duplicate_filename,
)
from services.firebase_notification_service import FirebaseNotificationService
from services.consultation_payment_service import ConsultationPaymentService
from services.pdf_service import PDFService
from services.persona_service import (
    identity_verification_approved,
    persona_enforcement_required,
    require_identity_verification,
)
import services.featured_lawyers as featured_lawyers_config
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func
import json
import re
import base64

# Import subscription service for usage validation
try:
    from services.subscription_service import SubscriptionService
    from services.subscription_constants import UsageActionType, BlockedReason
    SUBSCRIPTION_FEATURES_ENABLED = True
except ImportError as e:
    logger.warning(f"Subscription features not available for referral: {e}")
    SUBSCRIPTION_FEATURES_ENABLED = False

# Set up logging
logger = logging.getLogger(__name__)

REFERRAL_CONTEXT_TTL_SECONDS = 30 * 24 * 60 * 60

# Memory storage for referral context (fallback when Redis is not available)
MEMORY_STORAGE = {}

# Pydantic models for request/response
class ReferralFlowRequest(BaseModel):
    conversation_id: str
    step: str = Field(..., description="Current step: consultation_type, budget, duration, casework_service, casework_budget, urgency, notes, upload, confirm, select_lawyer, restart")
    response: Optional[str] = Field(None, description="User's response to current step")
    back: Optional[bool] = Field(False, description="If true, navigate one step back")

class LawyerProfile(BaseModel):
    lawyer_id: str
    name: str
    firm: Optional[str] = None
    expertise: List[str]
    years_experience: Optional[int] = None
    is_best_suited: bool = False
    match_score: int
    match_reasons: List[str]
    about: Optional[str] = None
    profile_picture: Optional[str] = None
    hourly_rate: Optional[float] = None
    firm_hourly_rate: Optional[float] = None
    sra_bsb: Optional[str] = None
    sra_bsb_iaa: Optional[str] = None

class ReferralFlowResponse(BaseModel):
    next_step: Optional[str] = Field(None, description="Next step in the flow")
    question: Optional[str] = Field(None, description="Question to ask user")
    description: Optional[str] = Field(
        None,
        description=(
            "Structured details block shown under the question. On the "
            "confirmation step this contains the consultation type, budget, "
            "urgency, notes and attachments, and is what clients should apply "
            "the blue border around."
        ),
    )
    verification: Optional[str] = Field(
        None,
        description=(
            "Verification prompt shown below the description (e.g. "
            "'Is this correct?'). Returned separately so clients do not have "
            "to split the question string."
        ),
    )
    options: Optional[List[Dict[str, Any]]] = Field(None, description="Options for dropdown")
    completed: bool = Field(False, description="Whether flow is complete")
    summary: Optional[Dict[str, Any]] = Field(None, description="Summary when complete")
    referral_id: Optional[str] = Field(None, description="Referral ID when created")
    recommended_lawyers: Optional[List[LawyerProfile]] = Field(None, description="List of recommended lawyers")
    consultation_category: Optional[str] = Field(None, description="Selected consultation category for client resume")
    consultation_type: Optional[str] = Field(None, description="Selected consultation type for client resume")

class LawyerSelectionRequest(BaseModel):
    referral_id: str
    selected_lawyer_id: str
    booking_start_at: Optional[str] = None
    booking_timezone: Optional[str] = "Europe/London"

class LawyerRecommendationsRefreshRequest(BaseModel):
    referral_id: str
    current_lawyer_ids: Optional[List[str]] = Field(
        None,
        description="Lawyer IDs currently displayed to the user; used to force at least one replacement when possible."
    )

class LawyerInboxRequest(BaseModel):
    referral_id: str
    action: str = Field(..., description="accept or reject")
    response_message: Optional[str] = Field(None, description="Optional message from lawyer")

class LawyerInboxResponse(BaseModel):
    success: bool
    message: str
    session_id: Optional[str] = Field(None, description="Session ID if accepted")

class FileUploadRequest(BaseModel):
    conversation_id: str
    file_type: str = Field(..., description="Type of file: voice_note, pdf, docx, or image")
    file_name: str
    content_type: str
    file_size: int

class ReferralAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.redis_service = RedisService()
        self.s3_service = S3Service()
        self.firebase_service = FirebaseNotificationService()
        self.pdf_service = PDFService()

        # Budget to consultation category mapping
        self.budget_mapping = {
            BudgetRange.BELOW_300.value: ConsultationCategory.SINGLE_CONSULTATION.value,
            BudgetRange.BETWEEN_300_499.value: ConsultationCategory.ONGOING_SUPPORT.value,
            BudgetRange.BETWEEN_500_699.value: ConsultationCategory.ONGOING_SUPPORT.value,
            BudgetRange.ABOVE_700.value: ConsultationCategory.CASEWORK.value
        }

    async def _push_referral_user_status_ws(self, user_id: str, conversation_id: str) -> None:
        """Push latest referral status to the user's WebSocket for this conversation (if connected)."""
        try:
            with self.db.get_session() as session:
                payload = await build_referral_user_status_payload(
                    session, self.redis_service, user_id, conversation_id
                )
            await referral_user_status_hub.send_update(user_id, conversation_id, payload)
        except Exception as exc:
            logger.error("_push_referral_user_status_ws failed: %s", exc)

    def _normalize_budget_range(self, budget_range: str) -> str:
        """
        Convert specific budget amounts to appropriate range categories for storage.
        This ensures compatibility with the existing range-based enum system.
        """
        if not budget_range:
            return budget_range
            
        # Canonical and variant spellings — normalise variants to canonical on the way in
        _canonical = {
            "below_300":  "below_300",
            "300_to_499": "300_to_499",
            "300_to_500": "300_to_499",  # app.py variant → canonical
            "500_to_699": "500_to_699",
            "500_to_700": "500_to_699",  # app.py variant → canonical
            "above_700":  "above_700",
        }
        if budget_range in _canonical:
            return _canonical[budget_range]
            
        # Convert specific amounts to ranges
        try:
            amount = int(budget_range)
            if amount < 300:
                return "below_300"
            elif 300 <= amount <= 499:
                return "300_to_499"
            elif 500 <= amount <= 700:
                return "500_to_699"
            else:
                return "above_700"
        except ValueError:
            # If not a number, return as-is
            return budget_range

    def _ongoing_budget_gbp(self, consultation_type: Optional[str], budget_range: Optional[str]) -> Optional[int]:
        if not consultation_type or not budget_range:
            return None

        valid_budgets = {
            "ongoing_3_day": {300, 350, 400, 450, 500},
            "ongoing_5_day": {500, 550, 600, 650, 700},
        }.get(consultation_type)
        if not valid_budgets:
            return None

        try:
            amount = int(str(budget_range).strip())
        except (TypeError, ValueError):
            return None

        return amount if amount in valid_budgets else None

    def get_router(self):
        router = APIRouter()
        
        @router.post("/referral/start_flow", response_model=ReferralFlowResponse)
        async def start_referral_flow(
            request: Request,
            data: ReferralFlowRequest
        ):
            """
            Start the conversational referral flow based on the UX document.
            Progressive disclosure: one question at a time.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Get or create referral context
                context_key = f"referral_context:{data.conversation_id}"
                context = await self._get_referral_context(context_key)
                
                if not context:
                    # Create initial context - this should have been set by chat detection
                    context = {
                        'user_id': user_id,
                        'conversation_id': data.conversation_id,
                        'legal_area': 'immigration',  # Default, should be inferred from chat
                        'urgency': 'normal',
                        'timestamp': datetime.now().isoformat()
                    }
                    await self._store_referral_context(context_key, context)
                
                # Back-navigation handling (navigate one step back)
                if getattr(data, 'back', False):
                    # Ensure context exists and belongs to this user
                    if not context:
                        raise HTTPException(status_code=404, detail="No active referral flow for this conversation")
                    if context.get('user_id') and context['user_id'] != user_id:
                        raise HTTPException(status_code=403, detail="Not allowed to modify this referral flow")

                    prev_step = self._get_previous_step(current_step=data.step, context=context) or "consultation_type"
                    self._cleanup_context_from_step(step=data.step, context=context)
                    await self._store_referral_context(context_key, context)
                    return await self._render_step_prompt(prev_step, context_key, context)

                # Handle flow steps based on UX document
                if data.step == "consultation_type":
                    return await self._handle_consultation_type(context_key, context, data.response)
                
                elif data.step == "budget":
                    return await self._handle_budget_selection(context_key, context, data.response)
                
                elif data.step == "ongoing_duration":
                    return await self._handle_ongoing_duration_selection(context_key, context, data.response)
                
                elif data.step == "specific_budget":
                    return await self._handle_specific_budget_selection(context_key, context, data.response)
                
                elif data.step == "duration":
                    return await self._handle_duration_selection(context_key, context, data.response)

                elif data.step == "casework_service":
                    return await self._handle_casework_service_selection(context_key, context, data.response)

                elif data.step == "casework_budget":
                    return await self._handle_casework_budget_selection(context_key, context, data.response)

                elif data.step == "urgency":
                    return await self._handle_urgency_selection(context_key, context, data.response)
                
                elif data.step == "notes":
                    return await self._handle_additional_notes(context_key, context, data.response)
                
                elif data.step == "upload":
                    return await self._handle_file_upload_option(context_key, context, data.response)
                
                elif data.step == "confirm":
                    return await self._handle_confirmation(context_key, context, data.response)
                
                elif data.step == "select_lawyer":
                    return await self._handle_lawyer_selection(context_key, context, data.response)
                
                elif data.step == "restart":
                    return await self._handle_restart_flow(context_key, context)
                
                else:
                    # Start of flow - ask consultation type
                    return ReferralFlowResponse(
                        next_step="consultation_type",
                        question="Do you think you'll need just a Single Consultation, Ongoing Support, Casework, or Not Sure Yet?",
                        options=consultation_category_options(),
                        completed=False
                    )
                
            except Exception as e:
                logger.error(f"Error in referral flow: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/referral/select_lawyer")
        async def select_lawyer(
            request: Request,
            data: LawyerSelectionRequest
        ):
            """
            Handle user's lawyer selection and send request to lawyer's inbox.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    requesting_user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not requesting_user:
                        raise HTTPException(status_code=404, detail="User not found")
                    require_identity_verification(session, requesting_user)

                    # Verify the referral belongs to this user
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(data.referral_id),
                        ReferralRequest.user_id == uuid.UUID(user_id),
                        ReferralRequest.status == ReferralStatus.CONFIRMED.value
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found or not accessible")
                    
                    # Verify the selected lawyer was in the recommendations
                    recommendation = session.query(LawyerRecommendation).filter(
                        LawyerRecommendation.referral_request_id == uuid.UUID(data.referral_id),
                        LawyerRecommendation.lawyer_id == uuid.UUID(data.selected_lawyer_id)
                    ).first()
                    
                    if not recommendation:
                        raise HTTPException(status_code=400, detail="Selected lawyer was not in recommendations")

                    selected_lawyer_uuid = uuid.UUID(data.selected_lawyer_id)
                    requested_start_at = None
                    requested_end_at = None
                    if requires_booking_slot(referral.consultation_category):
                        if not data.booking_start_at:
                            raise HTTPException(
                                status_code=400,
                                detail="booking_start_at is required for consultation requests.",
                            )
                        try:
                            requested_start_at = parse_datetime(data.booking_start_at, field_name="booking_start_at")
                        except ValueError as exc:
                            raise HTTPException(status_code=400, detail=str(exc))
                        requested_end_at = requested_start_at + timedelta(
                            minutes=consultation_duration_minutes(referral.consultation_type)
                        )
                        if requested_start_at <= datetime.now(timezone.utc):
                            raise HTTPException(status_code=400, detail="booking_start_at must be in the future")
                        booking_service = BookingService(session)
                        if not booking_service.validate_slot(
                            selected_lawyer_uuid,
                            requested_start_at=requested_start_at,
                            requested_end_at=requested_end_at,
                            consultation_type=referral.consultation_type,
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail="Selected booking slot is no longer available.",
                            )
                    
                    # Update referral status and assign lawyer
                    referral.matched_lawyer_id = selected_lawyer_uuid
                    referral.status = ReferralStatus.MATCHED.value

                    if requires_booking_slot(referral.consultation_category):
                        BookingService(session).create_booking_request(
                            user_id=uuid.UUID(user_id),
                            professional_id=selected_lawyer_uuid,
                            referral_request_id=referral.id,
                            consultation_category=referral.consultation_category,
                            consultation_type=referral.consultation_type,
                            requested_start_at=requested_start_at,
                            requested_end_at=requested_end_at,
                            timezone_name=data.booking_timezone or "Europe/London",
                        )
                    
                    # Add to lawyer's inbox
                    inbox_item = LawyerInbox(
                        lawyer_id=selected_lawyer_uuid,
                        referral_request_id=uuid.UUID(data.referral_id),
                        status='pending',
                        priority_score=70 if referral.urgency_level == 'urgent' else 50
                    )
                    session.add(inbox_item)
                    
                    # Send chat history to lawyer inbox
                    await self._send_chat_history_to_inbox(referral, uuid.UUID(data.selected_lawyer_id), session)
                    
                    session.commit()

                    # Get lawyer info for response
                    lawyer = session.query(User).filter(User.id == uuid.UUID(data.selected_lawyer_id)).first()

                    # Get user info for notification
                    user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()

                    try:
                        from services.email import EmailService
                        EmailService.send_user_referral_request_submitted_email(
                            to_email=user.email if user else None,
                            first_name=user.first_name if user else None,
                            lawyer_name=f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "your lawyer",
                            urgency_level=referral.urgency_level,
                        )
                    except Exception as e:
                        logger.error(f"Failed to send user-referral-request-submitted email for referral {referral.id}: {e}")

                    await self._push_referral_user_status_ws(user_id, referral.conversation_id)

                    # Send push notification to lawyer
                    lawyer_devices = session.query(DeviceToken).filter(
                        DeviceToken.user_id == uuid.UUID(data.selected_lawyer_id),
                        DeviceToken.is_active == True
                    ).all()

                    if lawyer_devices:
                        device_tokens = [device.device_token for device in lawyer_devices]
                        await self.firebase_service.send_new_referral_notification(
                            device_tokens=device_tokens,
                            client_name=f"{user.first_name} {user.last_name}",
                            referral_id=data.referral_id,
                            user_id=user_id,
                            legal_area=referral.legal_area
                        )
                        logger.info(f"Sent new referral notification to lawyer {data.selected_lawyer_id}")

                    # Persist in-app notification for the lawyer
                    client_name_str = f"{user.first_name} {user.last_name}" if user else "A client"
                    legal_area_str = referral.legal_area or "legal matter"
                    NotificationService(session).create(
                        recipient_id=uuid.UUID(data.selected_lawyer_id),
                        title="New Client Request!",
                        body=f"{client_name_str} has sent you a referral request for {legal_area_str}.",
                        notification_type="new_referral",
                        data={"referral_id": data.referral_id, "user_id": user_id, "screen": "LawyerInbox"},
                    )
                    
                    return {
                        "success": True,
                        "message": f"Your request has been sent to {lawyer.first_name} {lawyer.last_name}. You'll be notified when they respond.",
                        "lawyer_name": f"{lawyer.first_name} {lawyer.last_name}",
                        "referral_id": data.referral_id
                    }
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error selecting lawyer: {e}")
                raise HTTPException(status_code=500, detail=str(e))

       
       
        @router.get("/referral/payment_redirect/{session_id}")
        async def payment_redirect(session_id: str, request: Request):
            """
            Public redirect page for automation #11's email button.

            Gmail (and most email clients) strip/disable custom URL scheme
            links (e.g. com.steve.lawneeds://...) directly in email bodies,
            so the email button points here instead - a normal https:// page,
            which then attempts to open the app via the custom scheme, and
            falls back to the Play Store / App Store if that fails (e.g. app
            not installed).
            """
            android_fallback = "https://play.google.com/store/apps/details?id=com.steve.LawNeeds"
            ios_fallback = "https://apps.apple.com/us/app/lawneeds/id6557033638"

            html_content = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="utf-8" />
                    <title>Opening LawNeeds...</title>
                </head>
                <body>
                    <p>Opening the LawNeeds app...</p>
                    <script>
                        var isIOS = /iPhone|iPad|iPod/i.test(navigator.userAgent);
                        var deepLink = "com.steve.lawneeds://requestFlow/payment/{session_id}";
                        var fallbackUrl = isIOS ? "{ios_fallback}" : "{android_fallback}";

                        window.location = deepLink;

                        setTimeout(function () {{
                            window.location = fallbackUrl;
                        }}, 1500);
                    </script>
                </body>
                </html>
            """
            return HTMLResponse(content=html_content)

        @router.post("/referral/refresh_lawyers", response_model=ReferralFlowResponse)
        async def refresh_lawyer_recommendations(
            request: Request,
            data: LawyerRecommendationsRefreshRequest
        ):
            """
            Refresh the user's lawyer recommendations for a confirmed referral.

            The refresh keeps the recommendation count at three where possible,
            but forces at least one currently unseen lawyer into the refreshed
            list when another suitable candidate exists.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                referral_uuid = uuid.UUID(data.referral_id)
                current_lawyer_ids = self._normalize_lawyer_id_list(data.current_lawyer_ids)

                with self.db.get_session() as session:
                    requesting_user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not requesting_user:
                        raise HTTPException(status_code=404, detail="User not found")
                    require_identity_verification(session, requesting_user)

                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == referral_uuid,
                        ReferralRequest.user_id == uuid.UUID(user_id),
                        ReferralRequest.status == ReferralStatus.CONFIRMED.value
                    ).first()

                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found or not accessible")

                    if not current_lawyer_ids:
                        current_lawyer_ids = {
                            row[0]
                            for row in session.query(LawyerRecommendation.lawyer_id)
                            .filter(LawyerRecommendation.referral_request_id == referral_uuid)
                            .all()
                        }

                    context = self._context_from_referral(referral)

                recommended_lawyers = await self._generate_lawyer_recommendations(
                    referral_uuid,
                    context,
                    refresh=True,
                    current_lawyer_ids=current_lawyer_ids,
                )

                return ReferralFlowResponse(
                    next_step="select_lawyer",
                    question="Here are refreshed lawyer recommendations for your case. Please select the lawyer you'd like to work with:",
                    recommended_lawyers=recommended_lawyers,
                    completed=False,
                    referral_id=str(referral_uuid)
                )

            except HTTPException:
                raise
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid referral_id or lawyer ID")
            except Exception as e:
                logger.error(f"Error refreshing lawyer recommendations: {e}")
                raise HTTPException(status_code=500, detail=str(e))





        @router.get("/referral/active_flow/{conversation_id}", response_model=ReferralFlowResponse)
        async def get_active_referral_flow(
            request: Request,
            conversation_id: str
        ):
            """
            Resume the user's active referral wizard for a chat conversation.

            Chat history stores the transcript, but the referral wizard state is
            stored separately by conversation_id. This endpoint returns the
            current actionable wizard prompt/options so clients can restore the
            referral UI when a previous chat is reopened.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                user_uuid = uuid.UUID(user_id)

                with self.db.get_session() as session:
                    referral = (
                        session.query(ReferralRequest)
                        .filter(
                            ReferralRequest.conversation_id == conversation_id,
                            ReferralRequest.user_id == user_uuid,
                        )
                        .order_by(ReferralRequest.created_at.desc())
                        .first()
                    )

                    if referral:
                        if (
                            referral.status == ReferralStatus.CONFIRMED.value
                            and referral.matched_lawyer_id is None
                        ):
                            recommendations = self._get_saved_lawyer_recommendations(session, referral.id)
                            context = self._context_from_referral(referral)

                            if not recommendations:
                                recommendations = await self._generate_lawyer_recommendations(
                                    referral.id,
                                    context,
                                )

                            return ReferralFlowResponse(
                                next_step="select_lawyer",
                                question=(
                                    "Great! Here are the qualified lawyers we recommend for your case. "
                                    "Please select the lawyer you'd like to work with:"
                                ),
                                recommended_lawyers=recommendations,
                                completed=False,
                                referral_id=str(referral.id),
                                consultation_category=referral.consultation_category,
                                consultation_type=referral.consultation_type,
                            )

                        return ReferralFlowResponse(completed=True)

                context_key = f"referral_context:{conversation_id}"
                context = await self._get_referral_context(context_key)

                if not context:
                    return ReferralFlowResponse(completed=True)

                if context.get("user_id") and context["user_id"] != user_id:
                    raise HTTPException(status_code=403, detail="Not allowed to access this referral flow")

                step = self._infer_referral_resume_step(context)
                response = await self._render_step_prompt(step, context_key, context)
                response.consultation_category = context.get("consultation_category")
                response.consultation_type = context.get("consultation_type")
                return response

            except HTTPException:
                raise
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user or conversation identifier")
            except Exception as e:
                logger.error(f"Error resuming active referral flow: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.get("/referral/lawyer_inbox")
        async def get_lawyer_inbox(
            request: Request,
            status: Optional[str] = Query("pending", description="Filter by status"),
            limit: int = Query(10, description="Number of items to return")
        ):
            """
            Get referral requests in lawyer's inbox.
            Lawyers can see pending referral requests sent to them.
            """
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                lawyer_id = token_data.get("user_id")
                
                if not lawyer_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Verify user is a lawyer
                    lawyer = session.query(User).filter(
                        User.id == uuid.UUID(lawyer_id),
                        User.professional_type == 'Lawyer'
                    ).first()
                    
                    if not lawyer:
                        raise HTTPException(status_code=403, detail="Access denied. Lawyers only.")
                    require_identity_verification(session, lawyer)
                    
                    # Get referral requests for this lawyer
                    query = session.query(ReferralRequest, User).join(
                        User, ReferralRequest.user_id == User.id
                    ).filter(
                        ReferralRequest.matched_lawyer_id == uuid.UUID(lawyer_id)
                    )
                    
                    if status != "all":
                        query = query.filter(ReferralRequest.status == status)
                    
                    referral_requests = query.limit(limit).all()
                    
                    inbox_items = []
                    for referral, user in referral_requests:
                        inbox_items.append({
                            "referral_id": str(referral.id),
                            "user_info": {
                                "name": f"{user.first_name} {user.last_name}",
                                "email'[]": user.email
                            },
                            "legal_area": referral.legal_area,
                            "consultation_category": referral.consultation_category,
                            "consultation_type": referral.consultation_type,
                            "budget_range": referral.budget_range,
                            "urgency_level": referral.urgency_level,
                            "issue_summary": referral.issue_summary,
                            "confirmation_summary": referral.confirmation_summary,
                            "status": referral.status,
                            "created_at": referral.created_at.isoformat(),
                            "location": referral.user_location
                        })
                    
                    return {
                        "inbox_items": inbox_items,
                        "total_count": len(inbox_items),
                        "status_filter": status
                    }
                    
            except Exception as e:
                logger.error(f"Error getting lawyer inbox: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/referral/lawyer_response", response_model=LawyerInboxResponse)
        async def lawyer_response(
            request: Request,
            data: LawyerInboxRequest
        ):
            """
            Handle lawyer's response to a referral request (accept/reject).
            If accepted, creates a session. If rejected, updates status and notifies user.
            """
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                lawyer_id = token_data.get("user_id")
                
                if not lawyer_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                if data.action not in ["accept", "reject"]:
                    raise HTTPException(status_code=400, detail="Action must be 'accept' or 'reject'")
                
                # Add logging for debugging
                logger.info(f"Lawyer {lawyer_id} attempting to {data.action} referral {data.referral_id}")
                
                with self.db.get_session() as session:
                    # Verify user is a lawyer
                    lawyer = session.query(User).filter(
                        User.id == uuid.UUID(lawyer_id),
                        User.professional_type == 'Lawyer'
                    ).first()
                    
                    if not lawyer:
                        raise HTTPException(status_code=403, detail="Access denied. Lawyers only.")
                    require_identity_verification(session, lawyer)
                    
                    # Get the referral request
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(data.referral_id),
                        ReferralRequest.matched_lawyer_id == uuid.UUID(lawyer_id),
                        ReferralRequest.status == ReferralStatus.MATCHED.value
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral request not found or not available")
                    
                    # CRITICAL FIX: Also verify the LawyerInbox entry exists and is pending
                    inbox_entry = session.query(LawyerInbox).filter(
                        and_(
                            LawyerInbox.referral_request_id == uuid.UUID(data.referral_id),
                            LawyerInbox.lawyer_id == uuid.UUID(lawyer_id),
                            LawyerInbox.status.in_(['pending', 'read'])
                        )
                    ).first()
                    
                    if not inbox_entry:
                        raise HTTPException(status_code=404, detail="Request not found in your inbox or already processed")
                    
                    if data.action == "accept":
                        # Accept the referral - update both statuses
                        referral.status = ReferralStatus.ACTIVE.value
                        inbox_entry.status = 'accepted'
                        inbox_entry.responded_at = datetime.now(timezone.utc)
                        inbox_entry.response_message = data.response_message
                        
                        logger.info(f"Lawyer {lawyer_id} accepted referral {data.referral_id}. Updated statuses: ReferralRequest={referral.status}, LawyerInbox={inbox_entry.status}")
                        
                        # Create referral session
                        session_obj = ReferralSession(
                            referral_request_id=referral.id,
                            lawyer_id=uuid.UUID(lawyer_id),
                            user_id=referral.user_id,
                            session_status='pending'
                        )
                        session.add(session_obj)
                        BookingService(session).update_booking_for_referral(
                            referral.id,
                            ConsultationBookingStatus.ACCEPTED.value,
                        )
                        session.commit()
                        session.refresh(session_obj)

                        booking_start_at = None
                        if requires_booking_slot(referral.consultation_category):
                            booking_request = session.query(ConsultationBookingRequest).filter(
                                ConsultationBookingRequest.referral_request_id == referral.id
                            ).first()
                            if booking_request:
                                booking_start_at = booking_request.requested_start_at
                        
                        logger.info(f"Created session {session_obj.id} for referral {data.referral_id}")

                        try:
                            pay_service = ConsultationPaymentService(session)
                            pay_ok, pay_msg, _ = pay_service.try_initiate_platform_fixed_payment(session_obj.id)
                            if not pay_ok:
                                logger.warning(
                                    "Platform-fixed payment auto-init skipped or failed for session %s: %s",
                                    session_obj.id,
                                    pay_msg,
                                )
                        except Exception as pay_exc:
                            logger.warning(
                                "Platform-fixed payment auto-init error for session %s: %s",
                                session_obj.id,
                                pay_exc,
                            )

                        try:
                            from services.email import EmailService
                            client_user = session.query(User).filter(User.id == referral.user_id).first()
                            # TODO(confirm): exact ConsultationLength display format still
                            # pending your check of consultation_duration_minutes()'s return
                            # shape - "60 minutes" is a placeholder, not a final decision.
                            consultation_minutes = consultation_duration_minutes(referral.consultation_type)
                            consultation_length_display = f"{consultation_minutes} minutes"
                            EmailService.send_lawyer_accepted_your_request_email(
                                to_email=client_user.email if client_user else None,
                                first_name=client_user.first_name if client_user else None,
                                lawyer_name=f"{lawyer.first_name} {lawyer.last_name}",
                                consultation_length=consultation_length_display,
                                session_id=str(session_obj.id),
                            )
                        except Exception as email_exc:
                            logger.error(
                                "Failed to send lawyer-accepted-your-request email for referral %s: %s",
                                referral.id,
                                email_exc,
                            )

                        # Store acceptance message in Redis for real-time notification
                        notification_data = {
                            'type': 'referral_accepted',
                            'referral_id': str(referral.id),
                            'session_id': str(session_obj.id),
                            'lawyer_name': f"{lawyer.first_name} {lawyer.last_name}",
                            'lawyer_message': data.response_message,
                            'timestamp': datetime.now().isoformat()
                        }
                        await self.redis_service.set_key(
                            f"referral_notification:{referral.user_id}",
                            json.dumps(notification_data),
                            expire=3600
                        )
                        await self._push_referral_user_status_ws(
                            str(referral.user_id), referral.conversation_id
                        )
                        
                        # Send push notification to user
                        user_devices = session.query(DeviceToken).filter(
                            DeviceToken.user_id == referral.user_id,
                            DeviceToken.is_active == True
                        ).all()
                        
                        if user_devices:
                            device_tokens = [device.device_token for device in user_devices]
                            await self.firebase_service.send_referral_accepted_notification(
                                device_tokens=device_tokens,
                                lawyer_name=f"{lawyer.first_name} {lawyer.last_name}",
                                referral_id=str(referral.id),
                                session_id=str(session_obj.id),
                                lawyer_id=str(lawyer.id)
                            )
                            logger.info(f"Sent referral accepted notification to user {referral.user_id}")

                        # Persist in-app notification for the user
                        lawyer_name_str = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"
                        NotificationService(session).create(
                            recipient_id=referral.user_id,
                            title="Lawyer Accepted Your Request!",
                            body=f"{lawyer_name_str} has accepted your case and is ready to assist you.",
                            notification_type="referral_accepted",
                            data={"referral_id": str(referral.id), "session_id": str(session_obj.id), "screen": "InboxConversation"},
                        )

                        if booking_start_at:
                            if user_devices:
                                await self.firebase_service.send_consultation_booked_notification(
                                    device_tokens=device_tokens,
                                    lawyer_name=f"{lawyer.first_name} {lawyer.last_name}",
                                    referral_id=str(referral.id),
                                    session_id=str(session_obj.id),
                                    requested_start_at_iso=booking_start_at.isoformat(),
                                )
                                logger.info(f"Sent consultation booked notification to user {referral.user_id}")

                            NotificationService(session).create(
                                recipient_id=referral.user_id,
                                title="Consultation Booked",
                                body=f"{lawyer_name_str} confirmed your consultation.",
                                notification_type="consultation_booked",
                                data={
                                    "referral_id": str(referral.id),
                                    "session_id": str(session_obj.id),
                                    "requested_start_at": booking_start_at.isoformat(),
                                    "screen": "ConsultationScreen",
                                },
                            )
                        
                        return LawyerInboxResponse(
                            success=True,
                            message="Referral accepted successfully. Session created and awaiting payment.",
                            session_id=str(session_obj.id)
                        )
                    
                    else:  # reject
                        # Reject the referral - update both statuses
                        referral.status = ReferralStatus.CANCELLED.value
                        inbox_entry.status = 'rejected'
                        inbox_entry.responded_at = datetime.now(timezone.utc)
                        inbox_entry.response_message = data.response_message
                        BookingService(session).update_booking_for_referral(
                            referral.id,
                            ConsultationBookingStatus.REJECTED.value,
                        )
                        session.commit()
                        
                        logger.info(f"Lawyer {lawyer_id} rejected referral {data.referral_id}. Updated statuses: ReferralRequest={referral.status}, LawyerInbox={inbox_entry.status}")
                        
                        # Store rejection message in Redis for real-time notification
                        notification_data = {
                            'type': 'referral_rejected',
                            'referral_id': str(referral.id),
                            'lawyer_name': f"{lawyer.first_name} {lawyer.last_name}",
                            'rejection_reason': data.response_message,
                            'timestamp': datetime.now().isoformat()
                        }
                        await self.redis_service.set_key(
                            f"referral_notification:{referral.user_id}",
                            json.dumps(notification_data),
                            expire=3600
                        )
                        await self._push_referral_user_status_ws(
                            str(referral.user_id), referral.conversation_id
                        )
                        
                        # Send push notification to user
                        user_devices = session.query(DeviceToken).filter(
                            DeviceToken.user_id == referral.user_id,
                            DeviceToken.is_active == True
                        ).all()
                        
                        if user_devices:
                            device_tokens = [device.device_token for device in user_devices]
                            await self.firebase_service.send_referral_rejected_notification(
                                device_tokens=device_tokens,
                                lawyer_name=f"{lawyer.first_name} {lawyer.last_name}",
                                referral_id=str(referral.id),
                                reason=data.response_message
                            )
                            logger.info(f"Sent referral rejected notification to user {referral.user_id}")

                        # Persist in-app notification for the user
                        lawyer_name_str = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"
                        NotificationService(session).create(
                            recipient_id=referral.user_id,
                            title="Referral Update",
                            body=f"{lawyer_name_str} is unable to take your case at this time.",
                            notification_type="referral_rejected",
                            data={"referral_id": str(referral.id), "screen": "Referrals"},
                        )
                        
                        # TODO: Trigger re-matching with other lawyers
                        
                        return LawyerInboxResponse(
                            success=True,
                            message="Referral rejected. User will be notified."
                        )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error handling lawyer response: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.get("/referral/user_status/{conversation_id}")
        async def get_referral_status(
            request: Request,
            conversation_id: str
        ):
            """
            Get the current status of a user's referral request.
            Users can check if their referral has been accepted/rejected.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    return await build_referral_user_status_payload(
                        session, self.redis_service, user_id, conversation_id
                    )
                    
            except Exception as e:
                logger.error(f"Error getting referral status: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/referral/match_lawyers")
        async def match_lawyers_to_referral(
            request: Request,
            referral_id: str = Body(...),
            max_lawyers: int = Body(3, description="Maximum number of lawyers to match")
        ):
            """
            Match a confirmed referral request with available lawyers.
            Sends the referral to lawyers' inboxes for acceptance/rejection.
            """
            try:
                # Verify admin/system token (this might be called by system processes)
                token_data = self.auth.verify_request_token(request)
                
                with self.db.get_session() as session:
                    # Get the referral request
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id),
                        ReferralRequest.status == ReferralStatus.CONFIRMED.value
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found or not confirmed")
                    
                    # Find suitable lawyers based on criteria
                    query = session.query(LawyerAvailability, User).join(
                        User, LawyerAvailability.lawyer_id == User.id
                    ).filter(
                        LawyerAvailability.is_available == True,
                        User.professional_type == 'Lawyer'
                    )
                    
                    # Filter by legal expertise (area) - ONLY the four areas LawNeeds offers
                    if referral.legal_area:
                        # Convert area to match legal_expertise format - ONLY the four areas LawNeeds offers
                        area_mapping = {
                            'immigration': 'Immigration Law',
                            'family': 'Family Law',
                            'employment': 'Employment Law',
                            'road_traffic': 'Road Traffic Law',
                            'road_traffic_offences': 'Road Traffic Law'
                        }
                        expertise_area = area_mapping.get(referral.legal_area, referral.legal_area)
                        query = query.filter(
                            User.legal_expertise.contains([expertise_area])
                        )
                    
                    # Filter by consultation type capability
                    if referral.consultation_type:
                        query = query.filter(
                            LawyerAvailability.consultation_types.contains([referral.consultation_type])
                        )
                    
                    # Get available lawyers
                    available_lawyers = query.limit(max_lawyers).all()
                    if persona_enforcement_required():
                        available_lawyers = [
                            candidate
                            for candidate in available_lawyers
                            if identity_verification_approved(session, candidate[1].id)
                        ]
                    
                    if not available_lawyers:
                        return {
                            "matched": False,
                            "message": "No suitable lawyers available at this time",
                            "lawyers_notified": 0
                        }
                    
                    # For now, assign to the first lawyer (in production, implement better matching)
                    lawyer_availability, lawyer = available_lawyers[0]
                    
                    # Update referral with matched lawyer
                    referral.matched_lawyer_id = lawyer.id
                    referral.status = ReferralStatus.MATCHED.value

                    # Add to lawyer's inbox — auto-match previously updated the
                    # referral status but never created a LawyerInbox row, so the
                    # lawyer had nothing to accept/reject in their inbox.
                    inbox_item = LawyerInbox(
                        lawyer_id=lawyer.id,
                        referral_request_id=referral.id,
                        status='pending',
                        priority_score=70 if referral.urgency_level == 'urgent' else 50
                    )
                    session.add(inbox_item)

                    session.commit()
                    await self._push_referral_user_status_ws(
                        str(referral.user_id), referral.conversation_id
                    )
                    
                    # Store notification for lawyer
                    lawyer_notification = {
                        'type': 'new_referral',
                        'referral_id': str(referral.id),
                        'user_info': {
                            'legal_area': referral.legal_area,
                            'consultation_type': referral.consultation_type,
                            'urgency': referral.urgency_level
                        },
                        'timestamp': datetime.now().isoformat()
                    }
                    await self.redis_service.set_key(
                        f"lawyer_notification:{lawyer.id}",
                        json.dumps(lawyer_notification),
                        expire=3600
                    )

                    # Get user info for notification content
                    user = session.query(User).filter(User.id == referral.user_id).first()
                    client_name_str = f"{user.first_name} {user.last_name}" if user else "A client"

                    # Send push notification to lawyer
                    lawyer_devices = session.query(DeviceToken).filter(
                        DeviceToken.user_id == lawyer.id,
                        DeviceToken.is_active == True
                    ).all()

                    if lawyer_devices:
                        device_tokens = [device.device_token for device in lawyer_devices]
                        await self.firebase_service.send_new_referral_notification(
                            device_tokens=device_tokens,
                            client_name=client_name_str,
                            referral_id=str(referral.id),
                            user_id=str(referral.user_id),
                            legal_area=referral.legal_area
                        )
                        logger.info(f"Sent new referral notification to lawyer {lawyer.id}")

                    # Persist in-app notification for the lawyer
                    legal_area_str = referral.legal_area or "legal matter"
                    NotificationService(session).create(
                        recipient_id=lawyer.id,
                        title="New Client Request!",
                        body=f"{client_name_str} has sent you a referral request for {legal_area_str}.",
                        notification_type="new_referral",
                        data={"referral_id": str(referral.id), "user_id": str(referral.user_id), "screen": "LawyerInbox"},
                    )
                    # Email the lawyer that a new referral is waiting in their
                    # inbox (automation #6). Same trigger point as the push and
                    # in-app notifications above - fires once, for the single
                    # matched lawyer, after the inbox row and referral status
                    # are committed.
                    from services.email import EmailService
                    EmailService.send_user_referral_request_received_professional_email(
                        to_email=lawyer.email,
                        first_name=lawyer.first_name,
                        legal_area=referral.legal_area,
                        budget_range=referral.budget_range,
                        casework_budget_gbp=referral.casework_budget_gbp,
                        urgency_level=referral.urgency_level,
                    )
                    
                    return {
                        "matched": True,
                        "message": "Referral sent to lawyer's inbox",
                        "lawyer_id": str(lawyer.id),
                        "lawyer_name": f"{lawyer.first_name} {lawyer.last_name}",
                        "lawyers_notified": 1
                    }
                    
            except Exception as e:
                logger.error(f"Error matching lawyers: {e}")
                raise HTTPException(status_code=500, detail=str(e))
       
       
       
       
       
       
       
       
        @router.post("/referral/upload_file")
        async def upload_file(request: Request):
            """
            Upload one or more voice notes, PDFs, DOCX files, or images for a referral.

            Multipart: conversation_id (required). Repeat fields files, attachments, file, or attachment.
            Legacy single upload: one file + file_type + file_name (file_type required when only one part).
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                form = await request.form()
                conversation_id = form.get("conversation_id")
                if not conversation_id or not str(conversation_id).strip():
                    raise HTTPException(status_code=400, detail="conversation_id is required")
                conversation_id = str(conversation_id).strip()

                legacy_file_type = form.get("file_type")
                legacy_file_name = form.get("file_name")

                unique_files = collect_upload_files_from_form(form)
                if not unique_files:
                    raise HTTPException(status_code=400, detail="No file uploaded")

                context_key = f"referral_context:{conversation_id}"
                context = await self._get_referral_context(context_key)
                if not context:
                    raise HTTPException(status_code=404, detail="Referral context not found")

                referral_id = context.get("referral_id")
                if not referral_id:
                    referral_id = str(uuid.uuid4())
                    context["referral_id"] = referral_id
                    await self._store_referral_context(context_key, context)

                prepared = []
                for idx, uf in enumerate(unique_files):
                    try:
                        file_bytes = await uf.read()
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=f"Failed to read file: {e}")

                    display_name = uf.filename or (
                        str(legacy_file_name)
                        if len(unique_files) == 1 and legacy_file_name
                        else f"upload_{idx}"
                    )

                    if len(unique_files) == 1 and legacy_file_type:
                        effective_type = str(legacy_file_type)
                        if not self.s3_service.validate_file_type(uf.content_type, effective_type):
                            raise HTTPException(
                                status_code=400,
                                detail=f"Invalid file type for {effective_type}",
                            )
                    else:
                        effective_type = infer_referral_file_type(uf.content_type, uf.filename)
                        if not self.s3_service.validate_file_type(uf.content_type, effective_type):
                            raise HTTPException(
                                status_code=400,
                                detail="Unsupported file type (expected pdf, docx, image, or audio)",
                            )

                    max_size = self.s3_service.get_max_file_size(effective_type)
                    if len(file_bytes) > max_size:
                        raise HTTPException(
                            status_code=400,
                            detail=f"File too large. Maximum size: {max_size // (1024*1024)}MB",
                        )

                    page_count = 0
                    is_image = effective_type == "image"
                    if not is_image and effective_type in ("pdf", "docx"):
                        try:
                            if effective_type == "pdf":
                                import fitz
                                pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
                                page_count = pdf_doc.page_count
                                pdf_doc.close()
                            else:
                                page_count = max(1, len(file_bytes) // 50000)
                        except Exception as e:
                            logger.warning(f"Could not determine page count: {e}")
                            page_count = 1

                    prepared.append((file_bytes, display_name, effective_type, uf.content_type, page_count, is_image))

                existing_names = names_from_referral_context(context)
                batch_names: Dict[str, str] = {}
                for _, display_name, _, _, _, _ in prepared:
                    check_duplicate_filename(display_name, existing_names, batch_names)
                    batch_names[display_name.lower().strip()] = display_name

                if SUBSCRIPTION_FEATURES_ENABLED:
                    try:
                        with self.db.get_session() as session:
                            subscription_service = SubscriptionService(session)
                            user_uuid = uuid.UUID(user_id)
                            for file_bytes, _, effective_type, _, page_count, is_image in prepared:
                                can_upload, blocked_reason, message = subscription_service.can_perform_action(
                                    user_id=user_uuid,
                                    action_type=UsageActionType.UPLOAD,
                                    file_size_bytes=len(file_bytes),
                                    page_count=page_count,
                                    is_image=is_image,
                                )
                                if not can_upload:
                                    suggestions = self._get_upload_suggestions(blocked_reason)
                                    error_code = blocked_reason.value if blocked_reason else "upload_blocked"
                                    if blocked_reason == BlockedReason.PAGE_LIMIT_EXCEEDED:
                                        enhanced_message = f"This document exceeds our page limit. {message}"
                                    elif blocked_reason == BlockedReason.FILE_SIZE_EXCEEDED:
                                        enhanced_message = f"This file exceeds our size limit. {message}"
                                    else:
                                        enhanced_message = message
                                    status_code = (
                                        402 if blocked_reason == BlockedReason.INSUFFICIENT_MINUTES else 400
                                    )
                                    return JSONResponse(
                                        status_code=status_code,
                                        content={
                                            "error": error_code,
                                            "message": enhanced_message,
                                            "blocked": True,
                                            "suggestions": suggestions,
                                            "file_type": effective_type,
                                            "file_size": len(file_bytes),
                                            "page_count": page_count if not is_image else None,
                                        },
                                    )
                    except Exception as e:
                        logger.warning(f"Subscription check for upload failed, allowing: {e}")

                upload_payloads = []
                for file_bytes, display_name, effective_type, content_type, _, _ in prepared:
                    digest = content_sha256_hex(file_bytes).lower()
                    upload_result = self.s3_service.upload_file(
                        file_content=file_bytes,
                        file_name=display_name,
                        content_type=content_type,
                        referral_id=referral_id,
                        file_type=effective_type,
                    )
                    if not upload_result:
                        if not self.s3_service.s3_client:
                            raise HTTPException(
                                status_code=503,
                                detail="File upload service not configured. Please contact administrator.",
                            )
                        raise HTTPException(status_code=500, detail="File upload failed")
                    upload_result["content_sha256"] = digest
                    if effective_type == "pdf":
                        pdf_title = try_pdf_document_title(file_bytes)
                        if pdf_title:
                            upload_result["document_title"] = pdf_title
                    merge_referral_upload_into_context(
                        context, effective_type, upload_result, display_name
                    )
                    upload_payloads.append(
                        {
                            "file_type": effective_type,
                            "file_url": upload_result.get("url"),
                            "file_metadata": upload_result,
                        }
                    )

                await self._store_referral_context(context_key, context)

                if SUBSCRIPTION_FEATURES_ENABLED:
                    for (file_bytes, display_name, _, _, page_count, _), _up in zip(prepared, upload_payloads):
                        try:
                            with self.db.get_session() as session:
                                subscription_service = SubscriptionService(session)
                                user_uuid = uuid.UUID(user_id)
                                subscription_service.deduct_usage(
                                    user_id=user_uuid,
                                    action_type=UsageActionType.UPLOAD,
                                    file_size_bytes=len(file_bytes),
                                    page_count=page_count,
                                    file_name=display_name,
                                    referral_id=uuid.UUID(referral_id) if referral_id else None,
                                    conversation_id=conversation_id,
                                )
                        except Exception as e:
                            logger.warning(f"Failed to deduct upload usage: {e}")

                return {
                    "success": True,
                    "message": f"{len(upload_payloads)} file(s) uploaded successfully",
                    "files": upload_payloads,
                    "file_url": upload_payloads[0]["file_url"] if upload_payloads else None,
                    "file_metadata": upload_payloads[0]["file_metadata"] if upload_payloads else None,
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error uploading file: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/referral/record_voice")
        async def record_voice_note(
            request: Request,
            conversation_id: str = Body(...),
            audio_data: str = Body(..., description="Base64 encoded audio data"),
            language: Optional[str] = Body(None, description="Language for transcription")
        ):
            """
            Record and transcribe a live voice note for referral
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Get referral context
                context_key = f"referral_context:{conversation_id}"
                context = await self._get_referral_context(context_key)
                
                if not context:
                    raise HTTPException(status_code=404, detail="Referral context not found")
                
                # Get or create referral ID
                referral_id = context.get('referral_id')
                if not referral_id:
                    referral_id = str(uuid.uuid4())
                    context['referral_id'] = referral_id
                    await self._store_referral_context(context_key, context)
                
                # Decode base64 audio data
                try:
                    # Remove the data URL prefix if present
                    base64_str = audio_data
                    if ',' in base64_str:
                        base64_str = base64_str.split(',')[1]
                    audio_bytes = base64.b64decode(base64_str)
                    logger.info(f"Audio decoded successfully, size: {len(audio_bytes)} bytes")
                except Exception as e:
                    logger.error(f"Error decoding base64 audio: {str(e)}")
                    raise HTTPException(status_code=400, detail="Invalid audio data format")

                
                # VALIDATE VOICE NOTE USAGE BEFORE PROCESSING
                if SUBSCRIPTION_FEATURES_ENABLED:
                    try:
                        with self.db.get_session() as session:
                            subscription_service = SubscriptionService(session)
                            user_uuid = uuid.UUID(user_id)
                            
                            # Estimate duration: assume 16kHz, 16-bit mono audio = 32000 bytes/second
                            estimated_duration_seconds = len(audio_bytes) / 32000
                            
                            # Validate voice note recording
                            can_record, blocked_reason, message = subscription_service.can_perform_action(
                                user_id=user_uuid,
                                action_type=UsageActionType.VOICE_NOTE,
                                duration_seconds=int(estimated_duration_seconds)
                            )
                            
                            if not can_record:
                                # Get suggestions based on blocked reason
                                suggestions = self._get_voice_note_suggestions(blocked_reason)
                                
                                status_code = 402 if blocked_reason == BlockedReason.INSUFFICIENT_MINUTES else 403
                                return JSONResponse(
                                    status_code=status_code,
                                    content={
                                        "error": blocked_reason.value if blocked_reason else "voice_note_blocked",
                                        "message": message,
                                        "blocked": True,
                                        "suggestions": suggestions
                                    }
                                )
                    except Exception as e:
                        logger.warning(f"Subscription check for voice note failed, allowing: {e}")
                        # Continue if subscription check fails - don't block users
                
                # Transcribe the audio using OpenAI
                from services.openai_service import OpenAIService
                openai_service = OpenAIService()
                
                transcription_result = await openai_service.transcribe_audio(
                    audio_bytes=audio_bytes,
                    language=language
                )
                
                if not transcription_result.get('success'):
                    raise HTTPException(status_code=500, detail="Audio transcription failed")
                
                transcribed_text = transcription_result.get('text', '')
                
                # Upload audio to S3 for storage
                file_name = f"voice_note_{uuid.uuid4()}.wav"
                upload_result = self.s3_service.upload_file(
                    file_content=audio_bytes,
                    file_name=file_name,
                    content_type="audio/wav",
                    referral_id=referral_id,
                    file_type="voice_note"
                )
                if upload_result:
                    upload_result["content_sha256"] = content_sha256_hex(audio_bytes).lower()
                
                # Store transcription and voice note info in context
                context['voice_note_url'] = upload_result['url'] if upload_result else None
                context['voice_note_transcription'] = transcribed_text
                context['voice_note_metadata'] = {
                    'original_filename': file_name,
                    'size': len(audio_bytes),
                    'transcription': transcribed_text,
                    'language': language,
                    'upload_result': upload_result
                }
                if upload_result and upload_result.get("url"):
                    context.setdefault("uploaded_files", [])
                    vurl = upload_result["url"]
                    if not any((e.get("url") == vurl) for e in context["uploaded_files"]):
                        context["uploaded_files"].append({
                            "kind": "voice_note",
                            "url": vurl,
                            "original_filename": file_name,
                            "metadata": context["voice_note_metadata"],
                        })
                
                await self._store_referral_context(context_key, context)
                
                # DEDUCT VOICE NOTE USAGE AFTER SUCCESSFUL UPLOAD
                actual_duration = transcription_result.get('duration', estimated_duration_seconds if 'estimated_duration_seconds' in dir() else 0)
                if SUBSCRIPTION_FEATURES_ENABLED:
                    try:
                        with self.db.get_session() as session:
                            subscription_service = SubscriptionService(session)
                            user_uuid = uuid.UUID(user_id)
                            
                            # Deduct voice note usage
                            subscription_service.deduct_usage(
                                user_id=user_uuid,
                                action_type=UsageActionType.VOICE_NOTE,
                                duration_seconds=int(actual_duration),
                                file_name=file_name,
                                referral_id=uuid.UUID(referral_id) if referral_id else None,
                                conversation_id=conversation_id
                            )
                    except Exception as e:
                        logger.warning(f"Failed to deduct voice note usage: {e}")
                
                return {
                    "success": True,
                    "message": "Voice note recorded and transcribed successfully",
                    "transcription": transcribed_text,
                    "file_url": upload_result['url'] if upload_result else None,
                    "audio_duration": actual_duration
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error recording voice note: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        return router

    async def _get_referral_context(self, context_key: str) -> Optional[Dict]:
        """Get referral context from Redis or memory"""
        try:
            context_str = await self.redis_service.get_key(context_key)
            if context_str:
                return json.loads(context_str)
        except Exception as e:
            logger.warning(f"Redis not available: {e}")
        
        # Fallback to memory storage
        return MEMORY_STORAGE.get(context_key)

    async def _store_referral_context(self, context_key: str, context: Dict):
        """Store referral context in Redis or memory"""
        try:
            await self.redis_service.set_key(context_key, json.dumps(context), expire=REFERRAL_CONTEXT_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"Redis not available, using memory: {e}")
            MEMORY_STORAGE[context_key] = context

    def _get_voice_note_suggestions(self, blocked_reason) -> List[Dict]:
        """Get upgrade/purchase suggestions for blocked voice notes"""
        suggestions = []
        
        if SUBSCRIPTION_FEATURES_ENABLED:
            if blocked_reason == BlockedReason.MONTHLY_CAP_EXCEEDED:
                suggestions.append({
                    "type": "upgrade",
                    "message": "Upgrade to Premium for unlimited voice notes",
                    "plan": "premium-monthly"
                })
                suggestions.append({
                    "type": "wait",
                    "message": "Monthly limit resets at the start of next month"
                })
            elif blocked_reason == BlockedReason.TRIAL_EXPIRED:
                suggestions.append({
                    "type": "upgrade",
                    "message": "Subscribe to continue using voice notes",
                    "plan": "standard-monthly"
                })
            elif blocked_reason == BlockedReason.INSUFFICIENT_MINUTES:
                suggestions.append({
                    "type": "topup",
                    "message": "Top up your minutes to continue",
                    "min_minutes": 5,
                    "price_per_minute": 1.00
                })
                suggestions.append({
                    "type": "upgrade",
                    "message": "Upgrade for more minutes",
                    "plan": "premium-monthly"
                })
        
        return suggestions
    
    def _get_upload_suggestions(self, blocked_reason) -> List[Dict]:
        """Get upgrade/purchase suggestions for blocked uploads"""
        suggestions = []
        
        if SUBSCRIPTION_FEATURES_ENABLED:
            if blocked_reason == BlockedReason.FILE_SIZE_EXCEEDED:
                suggestions.append({
                    "type": "upgrade",
                    "message": "Upgrade to Premium for larger file sizes (50MB)",
                    "plan": "premium-monthly"
                })
            elif blocked_reason == BlockedReason.PAGE_LIMIT_EXCEEDED:
                suggestions.append({
                    "type": "upgrade",
                    "message": "Upgrade to Premium for documents up to 25 pages",
                    "plan": "premium-monthly"
                })
                suggestions.append({
                    "type": "split",
                    "message": "Split your document into smaller parts"
                })
            elif blocked_reason == BlockedReason.MONTHLY_CAP_EXCEEDED:
                suggestions.append({
                    "type": "upgrade",
                    "message": "Upgrade for more monthly uploads",
                    "plan": "premium-monthly"
                })
                suggestions.append({
                    "type": "wait",
                    "message": "Monthly limit resets at the start of next month"
                })
            elif blocked_reason == BlockedReason.TRIAL_EXPIRED:
                suggestions.append({
                    "type": "upgrade",
                    "message": "Subscribe to continue uploading files",
                    "plan": "standard-monthly"
                })
            elif blocked_reason == BlockedReason.INSUFFICIENT_MINUTES:
                suggestions.append({
                    "type": "topup",
                    "message": "Top up your minutes to continue",
                    "min_minutes": 5,
                    "price_per_minute": 1.00
                })
                suggestions.append({
                    "type": "upgrade",
                    "message": "Upgrade for more minutes",
                    "plan": "premium-monthly"
                })
        
        return suggestions

    def _get_previous_step(self, *, current_step: str, context: Dict) -> Optional[str]:
        """Determine the previous step across all flow branches."""
        consultation_type = context.get('consultation_type')
        consultation_category = context.get('consultation_category')
        budget_range = context.get('budget_range')

        if current_step == 'consultation_type':
            return None
        if current_step == 'ongoing_duration':
            return 'consultation_type'
        if current_step == 'specific_budget':
            return 'ongoing_duration'
        if current_step == 'duration':
            if budget_range == 'below_300':
                return 'budget'
            return 'consultation_type'
        if current_step == 'casework_service':
            return 'consultation_type'
        if current_step == 'casework_budget':
            return 'casework_service'
        if current_step == 'urgency':
            if consultation_type and consultation_type.startswith('single_'):
                return 'duration'
            if consultation_type and consultation_type.startswith('ongoing_'):
                return 'specific_budget'
            if consultation_type == 'casework' or consultation_category == 'casework':
                # New casework flow: budget step is service-aware. Fall back to
                # the legacy "budget" step only if the new fields aren't set
                # (e.g. an older context resumed mid-flow).
                if context.get('casework_service_type'):
                    return 'casework_budget'
                return 'budget'
            if budget_range == 'below_300':
                return 'duration'
            return 'consultation_type'
        if current_step == 'notes':
            return 'urgency'
        if current_step == 'upload':
            return 'notes'
        if current_step == 'confirm':
            return 'upload'
        if current_step == 'select_lawyer':
            return 'confirm'
        return 'consultation_type'

    def _cleanup_context_from_step(self, *, step: str, context: Dict):
        """Remove keys set at the given step and downstream steps, to allow changing earlier answers."""
        keys_to_remove = set()

        if step in ('select_lawyer', 'confirm', 'upload', 'notes', 'urgency', 'duration', 'specific_budget', 'ongoing_duration', 'budget', 'casework_service', 'casework_budget'):
            keys_to_remove.update([
                'confirmation_summary',
                'upload_type',
                'voice_note_url', 'voice_note_metadata', 'voice_note_transcription',
                'pdf_attachment_url', 'pdf_metadata',
                'docx_attachment_url', 'docx_metadata',
                'image_attachment_url', 'image_metadata'
            ])

        if step in ('notes', 'upload', 'confirm', 'select_lawyer'):
            keys_to_remove.update(['additional_notes'])

        if step in ('urgency', 'notes', 'upload', 'confirm', 'select_lawyer'):
            keys_to_remove.update(['urgency_level'])

        if step in ('duration', 'urgency', 'notes', 'upload', 'confirm', 'select_lawyer'):
            if (context.get('consultation_type') or '').startswith('single_'):
                keys_to_remove.update(['consultation_type'])

        if step in ('ongoing_duration', 'specific_budget', 'urgency', 'notes', 'upload', 'confirm', 'select_lawyer'):
            if (context.get('consultation_type') or '').startswith('ongoing_'):
                keys_to_remove.update(['consultation_type', 'ongoing_budget_gbp'])

        # Stepping back to the casework_service picker should clear the
        # service-and-budget triple so the user re-picks cleanly.
        if step in ('casework_service', 'casework_budget'):
            keys_to_remove.update(['casework_budget_gbp', 'budget_range'])
        if step == 'casework_service':
            keys_to_remove.update(['casework_service_type'])

        if step in ('budget', 'duration', 'urgency', 'notes', 'upload', 'confirm', 'select_lawyer'):
            keys_to_remove.update(['budget_range'])

        for k in keys_to_remove:
            if k in context:
                context.pop(k, None)

    async def _render_step_prompt(self, step: str, context_key: str, context: Dict) -> 'ReferralFlowResponse':
        """Return the prompt/options for the requested step without mutating forward state."""
        if step == 'consultation_type':
            return ReferralFlowResponse(
                next_step='consultation_type',
                question="Do you think you'll need just a Single Consultation, Ongoing Support, Casework, or Not Sure Yet?",
                options=consultation_category_options(),
                completed=False
            )

        if step == 'ongoing_duration':
            return ReferralFlowResponse(
                next_step='ongoing_duration',
                question='What type of ongoing support do you need?',
                options=[
                    {"value": "ongoing_3_day", "label": "3-Day Support (£300–£500)"},
                    {"value": "ongoing_5_day", "label": "5-Day Support (£500–£700)"}
                ],
                completed=False
            )

        if step == 'specific_budget':
            ct = context.get('consultation_type')
            if ct == 'ongoing_3_day':
                return ReferralFlowResponse(
                    next_step='specific_budget',
                    question="What's your specific budget for 3-day support?",
                    options=[{"value": v, "label": f"£{v}"} for v in ["300", "350", "400", "450", "500"]],
                    completed=False
                )
            return ReferralFlowResponse(
                next_step='specific_budget',
                question="What's your specific budget for 5-day support?",
                options=[{"value": v, "label": f"£{v}"} for v in ["500", "550", "600", "650", "700"]],
                completed=False
            )

        if step == 'duration':
            return ReferralFlowResponse(
                next_step='duration',
                question='Choose a single-session consultation (fixed platform prices):',
                options=single_consultation_duration_options(),
                completed=False
            )

        if step == 'casework_service':
            return ReferralFlowResponse(
                next_step='casework_service',
                question='Which casework service do you need?',
                options=casework_service_options(),
                completed=False
            )

        if step == 'casework_budget':
            service = context.get('casework_service_type') or ''
            if service in CASEWORK_MIN_BUDGET:
                min_gbp = CASEWORK_MIN_BUDGET[service]
                price_range = CASEWORK_PRICE_BY_SERVICE[service]
                service_label = CASEWORK_LABEL_BY_SERVICE[service]
                question = (
                    f"What budget are you working with for {service_label}? "
                    f"(Estimated range: {price_range}. Minimum: £{min_gbp:,}.)"
                )
                opts = casework_budget_options(service)
            else:
                question = "Please pick a casework service first:"
                opts = casework_service_options()
                return ReferralFlowResponse(
                    next_step='casework_service',
                    question=question,
                    options=opts,
                    completed=False
                )
            return ReferralFlowResponse(
                next_step='casework_budget',
                question=question,
                options=opts,
                completed=False
            )

        if step == 'budget':
            return ReferralFlowResponse(
                next_step='budget',
                question='What budget are you working with?',
                options=[
                    {"value": "below_300", "label": "Below £300"},
                    {"value": "300_to_499", "label": "£300–£499"},
                    {"value": "500_to_699", "label": "£500–£700 (5-day ongoing)"},
                    {"value": "above_700", "label": "£700+"}
                ],
                completed=False
            )

        if step == 'urgency':
            return await self._handle_urgency_step(context_key, context)

        if step == 'notes':
            return ReferralFlowResponse(
                next_step='notes',
                question='Please provide any additional notes or specific details about your legal matter (optional):',
                options=None,
                completed=False
            )

        if step == 'upload':
            return ReferralFlowResponse(
                next_step='upload',
                question='Would you like to attach any supporting documents or voice notes? (optional)',
                options=[
                    {"value": "voice_note", "label": "Record a voice note"},
                    {"value": "pdf", "label": "Upload a PDF document"},
                    {"value": "docx", "label": "Upload a Word document (.docx)"},
                    {"value": "image", "label": "Upload an image"},
                    {"value": "skip", "label": "Skip - no attachments"}
                ],
                completed=False
            )

        if step == 'confirm':
            stored_confirmation = self._stored_confirmation_response(context)
            if stored_confirmation:
                return stored_confirmation
            return await self._prepare_confirmation(context_key, context)

        if step == 'select_lawyer':
            return ReferralFlowResponse(
                next_step='select_lawyer',
                question='Please select a lawyer from the recommendations:',
                completed=False
            )

        # Fallback to starting step
        return await self._render_step_prompt('consultation_type', context_key, context)

    async def _handle_consultation_type(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle consultation type selection per UX flow"""
        context['consultation_category'] = response
        await self._store_referral_context(context_key, context)
        
        if response == "single_consultation" or response == "not_sure":
            # Skip budget, go directly to duration selection
            return ReferralFlowResponse(
                next_step="duration",
                question="Choose a single-session consultation (fixed platform prices):",
                options=single_consultation_duration_options(),
                completed=False
            )
        
        elif response == "ongoing_support":
            # For ongoing support, ask for consultation duration first
            return ReferralFlowResponse(
                next_step="ongoing_duration",
                question="What type of ongoing support do you need?",
                options=[
                    {"value": "ongoing_3_day", "label": "3-Day Support (£300–£500)"},
                    {"value": "ongoing_5_day", "label": "5-Day Support (£500–£700)"}
                ],
                completed=False
            )
        elif response == "casework":
            # Casework now uses a Service & Price dropdown that mirrors the
            # Single Session "Select Time & Rate" picker. The budget step is
            # service-aware (per-service floor) and is handled separately.
            return ReferralFlowResponse(
                next_step="casework_service",
                question="Which casework service do you need? Each service has a fixed fee range — pick the one that matches your case:",
                options=casework_service_options(),
                completed=False
            )

    async def _handle_budget_selection(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle budget selection per UX flow logic"""
        context['budget_range'] = response
        await self._store_referral_context(context_key, context)
        
        if response == "below_300":
            # Redirect to single consultation as per UX flow
            context['consultation_category'] = "single_consultation"
            await self._store_referral_context(context_key, context)
            
            return ReferralFlowResponse(
                next_step="duration",
                question="For budgets below £300, we recommend single consultations. Choose a session (fixed platform prices):",
                options=single_consultation_duration_options(),
                completed=False
            )
        
        elif response in ["300_to_499", "500_to_699"]:
            # Ongoing support - set specific type based on budget
            if response == "300_to_499":
                context['consultation_type'] = "ongoing_3_day"
            else:  # 500_to_699
                context['consultation_type'] = "ongoing_5_day"
            await self._store_referral_context(context_key, context)
            return await self._handle_urgency_step(context_key, context)
        
        elif response == "above_700":
            # Casework option
            context['consultation_type'] = "casework"
            await self._store_referral_context(context_key, context)
            return await self._handle_urgency_step(context_key, context)

    async def _handle_ongoing_duration_selection(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle ongoing support duration selection (3-day or 5-day)"""
        context['consultation_type'] = response
        await self._store_referral_context(context_key, context)
        
        # Show specific budget options based on selected duration
        if response == "ongoing_3_day":
            return ReferralFlowResponse(
                next_step="specific_budget",
                question="What's your specific budget for 3-day support?",
                options=[
                    {"value": "300", "label": "£300"},
                    {"value": "350", "label": "£350"},
                    {"value": "400", "label": "£400"},
                    {"value": "450", "label": "£450"},
                    {"value": "500", "label": "£500"}
                ],
                completed=False
            )
        elif response == "ongoing_5_day":
            return ReferralFlowResponse(
                next_step="specific_budget",
                question="What's your specific budget for 5-day support?",
                options=[
                    {"value": "500", "label": "£500"},
                    {"value": "550", "label": "£550"},
                    {"value": "600", "label": "£600"},
                    {"value": "650", "label": "£650"},
                    {"value": "700", "label": "£700"}
                ],
                completed=False
            )
        else:
            # Fallback to standard urgency step
            return await self._handle_urgency_step(context_key, context)

    async def _handle_specific_budget_selection(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle specific budget amount selection for ongoing support"""
        context['budget_range'] = response
        ongoing_budget = self._ongoing_budget_gbp(context.get('consultation_type'), response)
        if ongoing_budget is not None:
            context['ongoing_budget_gbp'] = ongoing_budget
        await self._store_referral_context(context_key, context)
        return await self._handle_urgency_step(context_key, context)

    async def _handle_duration_selection(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle duration selection for single consultations"""
        context['consultation_type'] = response
        await self._store_referral_context(context_key, context)
        return await self._handle_urgency_step(context_key, context)

    async def _handle_casework_service_selection(
        self, context_key: str, context: Dict, response: str
    ) -> ReferralFlowResponse:
        """User picked a casework service from the Service & Price dropdown.

        After this we ask for the budget using a service-specific picker so the
        floor (e.g. >= £1,000 for Student Visa) is enforced by construction.
        """
        service = (response or "").strip()
        if service not in CASEWORK_MIN_BUDGET:
            return ReferralFlowResponse(
                next_step="casework_service",
                question="Please select a valid casework service:",
                options=casework_service_options(),
                completed=False,
            )

        context['casework_service_type'] = service
        # The DB row needs a non-null consultation_type; casework path always
        # stores the literal "casework" enum value.
        context['consultation_type'] = ConsultationType.CASEWORK.value
        context['consultation_category'] = ConsultationCategory.CASEWORK.value
        await self._store_referral_context(context_key, context)

        min_gbp = CASEWORK_MIN_BUDGET[service]
        price_range = CASEWORK_PRICE_BY_SERVICE[service]
        service_label = CASEWORK_LABEL_BY_SERVICE[service]

        return ReferralFlowResponse(
            next_step="casework_budget",
            question=(
                f"What budget are you working with for {service_label}? "
                f"(Estimated range: {price_range}. Minimum allowed: £{min_gbp:,}.)"
            ),
            options=casework_budget_options(service),
            completed=False,
        )

    async def _handle_casework_budget_selection(
        self, context_key: str, context: Dict, response: str
    ) -> ReferralFlowResponse:
        """Validate the chosen budget against the service-specific floor.

        Implements the requirement: a user can't select a budget below the
        published range for the service they chose (e.g. <£1,000 for Student
        Visa). On rejection the picker is re-rendered with an explanatory
        message; on success we move on to the urgency step.
        """
        service = context.get('casework_service_type')
        if not service or service not in CASEWORK_MIN_BUDGET:
            return ReferralFlowResponse(
                next_step="casework_service",
                question="Please select a casework service first:",
                options=casework_service_options(),
                completed=False,
            )

        raw = (response or "").strip()
        # Accept both raw integers ("1500") and "£1,500"-style strings.
        cleaned = raw.replace("£", "").replace(",", "").strip()
        try:
            amount = int(cleaned)
        except (ValueError, TypeError):
            min_gbp = CASEWORK_MIN_BUDGET[service]
            return ReferralFlowResponse(
                next_step="casework_budget",
                question=(
                    f"Please pick a budget from the list "
                    f"(minimum £{min_gbp:,} for this service):"
                ),
                options=casework_budget_options(service),
                completed=False,
            )

        err = validate_casework_budget(service, amount)
        if err:
            min_gbp = CASEWORK_MIN_BUDGET[service]
            return ReferralFlowResponse(
                next_step="casework_budget",
                question=(
                    f"{err} Please choose a budget at or above £{min_gbp:,}:"
                ),
                options=casework_budget_options(service),
                completed=False,
            )

        context['casework_budget_gbp'] = amount
        # Persist as a specific amount so downstream displays show "£1,500"
        # rather than the generic "above_700" bucket. _normalize_budget_range
        # collapses this back to the storage enum on DB insert.
        context['budget_range'] = str(amount)
        await self._store_referral_context(context_key, context)
        return await self._handle_urgency_step(context_key, context)

    async def _handle_confirmation(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle user confirmation.

        The ``confirm`` step accepts three kinds of responses:

        * ``uploaded`` / ``recorded`` / ``skip_upload`` — sent by the client
          right after an attachment flow completes (file uploaded, voice note
          recorded, or upload abandoned). These are **upload-complete signals,
          not a final confirmation**. The server responds with the summary
          card (``question`` / ``description`` / ``verification``) so the user
          can verify their selections before the referral is actually
          created. This ensures the consultation type, budget, urgency and
          notes a user entered are always surfaced for review regardless of
          whether they chose to upload a file.
        * ``yes`` — the user confirmed the summary card. Only at this point
          do we persist the referral and generate lawyer recommendations.
          The consultation-type safety net (which guards against an
          incomplete wizard — e.g. a voice-note upload that short-circuited
          the flow) runs here because it is only relevant before the DB row
          is inserted.
        * anything else (including ``no``) — user wants to modify, restart
          the flow.
        """
        response_lower = (response or "").strip().lower()

        # (1) Upload-complete signals -> show the summary first, do NOT finalize.
        if response_lower in {"uploaded", "recorded", "skip_upload"}:
            return await self._prepare_confirmation(context_key, context)

        # (2) Explicit user confirmation -> finalize the referral.
        if response_lower == "yes":
            # Safety net: the DB requires a ``consultation_type``. If the
            # user reached confirmation without one (e.g. a voice-note upload
            # that short-circuited the wizard) route them back to the missing
            # step instead of crashing on insert.
            consultation_category = context.get('consultation_category')
            consultation_type = context.get('consultation_type')
            if not consultation_type:
                if consultation_category in ("single_consultation", "not_sure"):
                    return ReferralFlowResponse(
                        next_step="duration",
                        question="Before we finish, please choose a single-session consultation (fixed platform prices):",
                        options=single_consultation_duration_options(),
                        completed=False,
                    )
                if consultation_category == "ongoing_support":
                    return ReferralFlowResponse(
                        next_step="ongoing_duration",
                        question="Before we finish, what type of ongoing support do you need?",
                        options=[
                            {"value": "ongoing_3_day", "label": "3-Day Support (£300–£500)"},
                            {"value": "ongoing_5_day", "label": "5-Day Support (£500–£700)"},
                        ],
                        completed=False,
                    )
                if consultation_category == "casework":
                    # Casework requires a Service & Price selection. Send the
                    # user back to that picker if they somehow skipped it.
                    if not context.get('casework_service_type'):
                        return ReferralFlowResponse(
                            next_step="casework_service",
                            question="Before we finish, please pick the casework service that matches your case:",
                            options=casework_service_options(),
                            completed=False,
                        )
                    context['consultation_type'] = ConsultationType.CASEWORK.value
                    await self._store_referral_context(context_key, context)
                else:
                    return ReferralFlowResponse(
                        next_step="consultation_type",
                        question="Let's pick a consultation type. Do you need a Single Consultation, Ongoing Support, Casework, or Not Sure Yet?",
                        options=consultation_category_options(),
                        completed=False,
                    )

            referral_id = await self._create_referral_request(context)

            # Generate lawyer recommendations
            recommended_lawyers = await self._generate_lawyer_recommendations(referral_id, context)

            return ReferralFlowResponse(
                next_step="select_lawyer",
                question="Great! Here are 3 qualified lawyers we recommend for your case. Please select the lawyer you'd like to work with:",
                recommended_lawyers=recommended_lawyers,
                completed=False,
                referral_id=str(referral_id)
            )

        # (3) User wants to modify (``no`` or anything else) -> restart.
        return ReferralFlowResponse(
            next_step="consultation_type",
            question="No problem! Let's start over. Do you think you'll need just a Single Consultation, Ongoing Support, Casework, or Not Sure Yet?",
            options=consultation_category_options(),
            completed=False
        )

    async def _prepare_confirmation(self, context_key: str, context: Dict) -> ReferralFlowResponse:
        """Prepare confirmation message based on UX document templates.

        Returns structured ``question`` / ``description`` / ``verification``
        fields so clients can render each block without having to split a
        single concatenated string.
        """
        # Get issue summary from legal matter description or chat history
        issue_summary = context.get('legal_matter_description', '')

        if not issue_summary:
            conversation_id = context.get('conversation_id')
            try:
                chat_history = await self.redis_service.get_chat_history(conversation_id)
                issue_summary = self._generate_issue_summary(chat_history)
            except Exception as e:
                logger.warning(f"Could not get chat history: {e}")
                issue_summary = "Legal assistance needed"

        # Persist the raw issue summary (pre-truncation) on context for the
        # DB row; the builder will truncate for display purposes.
        context['issue_summary'] = (
            issue_summary[:200] + "..." if len(issue_summary) > 200 else issue_summary
        )

        parts = build_referral_confirmation_parts(
            context,
            attachment_confirmation_lines_fn=attachment_confirmation_lines,
            single_consultation_fragment_fn=single_consultation_confirmation_fragment,
            casework_price_label_fn=casework_price_label,
        )

        # Keep the legacy single-string summary available for consumers that
        # still rely on it (e.g. lawyer inbox ``confirmation_summary`` column).
        context['confirmation_summary'] = parts['full_summary']
        context['confirmation_parts'] = {
            'question': parts['question'],
            'description': parts['description'],
            'verification': parts['verification'],
        }
        context['confirmation_details'] = {
            'issue_summary': parts['issue_summary'],
            'consultation_category': context.get('consultation_category'),
            'consultation_type': context.get('consultation_type'),
            'budget_range': context.get('budget_range'),
            'ongoing_budget_gbp': context.get('ongoing_budget_gbp'),
            'casework_service_type': context.get('casework_service_type'),
            'casework_budget_gbp': context.get('casework_budget_gbp'),
            'urgency_level': context.get('urgency_level'),
            'additional_notes': context.get('additional_notes'),
        }
        await self._store_referral_context(context_key, context)

        return ReferralFlowResponse(
            next_step="confirm",
            question=parts['question'],
            description=parts['description'],
            verification=parts['verification'],
            options=[
                {"value": "yes", "label": "Yes, that's correct"},
                {"value": "no", "label": "No, I need to change something"}
            ],
            completed=False
        )

    def _stored_confirmation_response(self, context: Dict[str, Any]) -> Optional[ReferralFlowResponse]:
        """Return the saved confirmation card for resume, preserving selections.

        When a user leaves the app at the summary stage, reopening a previous
        chat should show the same summary card they already reviewed. Rebuilding
        it from a partially restored context can produce blank/default
        selections, so prefer the stored structured parts. Older contexts only
        have ``confirmation_summary``; for those, return the legacy text as the
        question rather than overwriting it with a rebuilt empty summary.
        """
        parts = context.get('confirmation_parts')
        if isinstance(parts, dict):
            question = parts.get('question')
            description = parts.get('description')
            verification = parts.get('verification')
            if question or description or verification:
                return ReferralFlowResponse(
                    next_step="confirm",
                    question=question,
                    description=description,
                    verification=verification,
                    options=[
                        {"value": "yes", "label": "Yes, that's correct"},
                        {"value": "no", "label": "No, I need to change something"},
                    ],
                    completed=False,
                    consultation_category=context.get('consultation_category'),
                    consultation_type=context.get('consultation_type'),
                )

        legacy_summary = (context.get('confirmation_summary') or '').strip()
        if legacy_summary:
            return ReferralFlowResponse(
                next_step="confirm",
                question=legacy_summary,
                options=[
                    {"value": "yes", "label": "Yes, that's correct"},
                    {"value": "no", "label": "No, I need to change something"},
                ],
                completed=False,
                consultation_category=context.get('consultation_category'),
                consultation_type=context.get('consultation_type'),
            )

        return None

    def _generate_issue_summary(self, chat_history: List[Dict]) -> str:
        """Generate a brief issue summary from chat history"""
        if not chat_history:
            return "Legal assistance needed"
        
        # Get user messages (exclude bot responses)
        user_messages = [msg for msg in chat_history if msg.get('role') == 'user']
        
        if not user_messages:
            return "Legal assistance needed"
        
        # Take the most substantial user message (longest one)
        substantial_message = max(user_messages, key=lambda x: len(x.get('content', '')))
        content = substantial_message.get('content', '')
        
        # Truncate if too long
        if len(content) > 200:
            return content[:200] + "..."
        
        return content

    async def _create_referral_request(self, context: Dict) -> uuid.UUID:
        """Create referral request in database"""
        conversation_id = context['conversation_id']
        chat_history = context.get('chat_history') or []
        if not chat_history:
            try:
                chat_history = await self.redis_service.get_chat_history(conversation_id)
            except Exception as exc:
                logger.warning(
                    "Could not load chat history while creating referral for conversation %s: %s",
                    conversation_id,
                    exc,
                )
                chat_history = []

        with self.db.get_session() as session:
            # Normalize budget range for storage
            budget_range = context.get('budget_range')
            normalized_budget_range = self._normalize_budget_range(budget_range) if budget_range else None
            consultation_type = context.get('consultation_type')
            ongoing_budget_gbp = (
                context.get('ongoing_budget_gbp')
                or self._ongoing_budget_gbp(consultation_type, budget_range)
            )

            # Both columns are NOT NULL in the DB. Apply safe fallbacks so an unusual
            # client flow (e.g. confirming after a voice-note upload without picking a
            # duration) cannot trigger a NotNullViolation on insert.
            consultation_category = context.get('consultation_category') or 'single_consultation'
            if not consultation_type:
                if consultation_category == 'casework':
                    consultation_type = 'casework'
                elif consultation_category == 'ongoing_support':
                    consultation_type = 'ongoing_3_day'
                else:
                    consultation_type = 'single_10_min'
                logger.warning(
                    "Referral missing consultation_type for category=%s; defaulting to %s",
                    consultation_category,
                    consultation_type,
                )

            referral = ReferralRequest(
                user_id=uuid.UUID(context['user_id']),
                conversation_id=conversation_id,
                legal_area=context.get('legal_area', 'immigration'),
                issue_summary=context.get('issue_summary', 'Legal assistance needed'),
                consultation_category=consultation_category,
                consultation_type=consultation_type,
                budget_range=normalized_budget_range,
                ongoing_budget_gbp=ongoing_budget_gbp if consultation_type.startswith('ongoing_') else None,
                casework_service_type=context.get('casework_service_type'),
                casework_budget_gbp=context.get('casework_budget_gbp'),
                urgency_level=context.get('urgency_level', UrgencyLevel.NORMAL.value),
                confirmation_summary=context.get('confirmation_summary'),
                user_confirmed=True,
                status=ReferralStatus.CONFIRMED.value,
                additional_info=context.get('additional_notes', ''),
                voice_note_url=context.get('voice_note_url'),
                pdf_attachment_url=context.get('pdf_attachment_url'),
                docx_attachment_url=context.get('docx_attachment_url'),
                image_attachment_url=context.get('image_attachment_url'),
                attachment_metadata=build_referral_attachment_metadata(context),
                chat_history=chat_history,
            )
            
            session.add(referral)
            session.commit()
            session.refresh(referral)
            rid = referral.id
            uid = str(referral.user_id)
            cid = referral.conversation_id

        await self._push_referral_user_status_ws(uid, cid)
        return rid

    async def _handle_lawyer_selection(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle lawyer selection from user"""
        # This step is completed via the /select_lawyer endpoint
        # Return completion message
        return ReferralFlowResponse(
            completed=True,
            summary={
                "message": "Perfect! Your request has been sent to the selected lawyer. You'll receive a notification when they respond.",
                "next_steps": "The lawyer will review your case and either accept or provide feedback. You'll be notified of their response."
            }
        )

    async def _handle_restart_flow(self, context_key: str, context: Dict) -> ReferralFlowResponse:
        """Handle restart of referral flow"""
        # Reset the consultation preferences but keep legal matter description
        legal_matter = context.get('legal_matter_description', '')
        legal_area = context.get('legal_area', 'general')
        urgency = context.get('urgency', 'normal')
        
        # Clear consultation preferences
        context = {
            'user_id': context['user_id'],
            'conversation_id': context['conversation_id'],
            'legal_area': legal_area,
            'urgency': urgency,
            'legal_matter_description': legal_matter,
            'timestamp': datetime.now().isoformat()
        }
        
        await self._store_referral_context(context_key, context)
        
        # Start over with consultation type
        return ReferralFlowResponse(
            next_step="consultation_type",
            question="No problem! Let's start over with your consultation preferences. Do you think you'll need just a Single Consultation, Ongoing Support, Casework, or Not Sure Yet?",
            options=consultation_category_options(),
            completed=False
        )

    # Canonical mapping from referral `legal_area` values to lawyer `legal_expertise` labels.
    _LEGAL_AREA_MAPPING: Dict[str, str] = {
        'immigration': 'Immigration Law',
        'family': 'Family Law',
        'employment': 'Employment Law',
        'road_traffic': 'Road Traffic Law',
        'road_traffic_offences': 'Road Traffic Law',
    }

    # Budget range -> (min_gbp, max_gbp). Used for hourly-rate fit checks.
    _BUDGET_RANGE_BOUNDS: Dict[str, tuple] = {
        'below_300': (0, 300),
        '300_to_499': (300, 499),
        '500_to_699': (500, 699),
        'above_700': (700, 10_000),
    }

    def _resolve_expertise_area(self, legal_area: Optional[str]) -> str:
        key = (legal_area or 'immigration')
        return self._LEGAL_AREA_MAPPING.get(key, key.replace('_', ' ').title() + ' Law')

    def _normalize_lawyer_id_list(self, lawyer_ids: Optional[List[str]]) -> set:
        normalized = set()
        for lawyer_id in lawyer_ids or []:
            if lawyer_id:
                normalized.add(uuid.UUID(str(lawyer_id)))
        return normalized

    def _context_from_referral(self, referral: ReferralRequest) -> Dict[str, Any]:
        return {
            'user_id': str(referral.user_id),
            'conversation_id': referral.conversation_id,
            'legal_area': referral.legal_area or 'immigration',
            'consultation_category': referral.consultation_category,
            'consultation_type': referral.consultation_type,
            'budget_range': referral.budget_range,
            'ongoing_budget_gbp': referral.ongoing_budget_gbp,
            'casework_service_type': referral.casework_service_type,
            'casework_budget_gbp': referral.casework_budget_gbp,
            'urgency_level': referral.urgency_level,
            'user_location': referral.user_location,
            'issue_summary': referral.issue_summary,
            'additional_notes': referral.additional_info,
        }

    def _get_saved_lawyer_recommendations(
        self,
        session: Session,
        referral_id: uuid.UUID,
    ) -> List[LawyerProfile]:
        rows = (
            session.query(LawyerRecommendation, User)
            .join(User, User.id == LawyerRecommendation.lawyer_id)
            .filter(LawyerRecommendation.referral_request_id == referral_id)
            .order_by(LawyerRecommendation.recommendation_rank.asc())
            .all()
        )

        recommendations: List[LawyerProfile] = []
        for recommendation, lawyer in rows:
            recommendations.append(
                LawyerProfile(
                    lawyer_id=str(lawyer.id),
                    name=f"{lawyer.first_name} {lawyer.last_name}",
                    firm=lawyer.professional_employer,
                    expertise=lawyer.legal_expertise or [],
                    is_best_suited=bool(recommendation.is_best_suited),
                    match_score=recommendation.match_score,
                    match_reasons=recommendation.match_reasons or [],
                    about=lawyer.about,
                    profile_picture=lawyer.profile_picture_path,
                    hourly_rate=float(lawyer.hourly_rate) if lawyer.hourly_rate else None,
                    firm_hourly_rate=float(lawyer.firm_hourly_rate) if lawyer.firm_hourly_rate else None,
                    sra_bsb=lawyer.sra_bsb,
                    sra_bsb_iaa=lawyer.sra_bsb_iaa,
                )
            )

        return recommendations

    def _infer_referral_resume_step(self, context: Dict[str, Any]) -> str:
        """Infer the next actionable wizard step from persisted referral context."""
        consultation_category = context.get('consultation_category')
        consultation_type = context.get('consultation_type')

        if context.get('confirmation_summary'):
            return 'confirm'

        if not consultation_category:
            return 'consultation_type'

        if consultation_category in ('single_consultation', 'not_sure'):
            if not consultation_type:
                return 'duration'

        if consultation_category == 'ongoing_support':
            if not consultation_type or not str(consultation_type).startswith('ongoing_'):
                return 'ongoing_duration'
            if not context.get('budget_range'):
                return 'specific_budget'

        if consultation_category == 'casework' or consultation_type == ConsultationType.CASEWORK.value:
            if not context.get('casework_service_type'):
                return 'casework_service'
            if context.get('casework_budget_gbp') is None:
                return 'casework_budget'

        if not context.get('urgency_level'):
            return 'urgency'

        if 'additional_notes' not in context:
            return 'notes'

        return 'upload'

    def _choose_lawyer_recommendation_entries(
        self,
        ranked_lawyers: List[Dict[str, Any]],
        *,
        refresh: bool = False,
        current_lawyer_ids: Optional[set] = None,
        max_recommendations: int = 3,
    ) -> List[Dict[str, Any]]:
        if not refresh:
            return ranked_lawyers[:max_recommendations]

        current_lawyer_ids = current_lawyer_ids or set()
        selected_ids = set()

        alternatives = [
            lawyer_data for lawyer_data in ranked_lawyers
            if lawyer_data['lawyer'].id not in current_lawyer_ids
        ]
        if alternatives:
            selected_ids.add(alternatives[0]['lawyer'].id)

        for lawyer_data in ranked_lawyers:
            if len(selected_ids) >= max_recommendations:
                break
            selected_ids.add(lawyer_data['lawyer'].id)

        return [
            lawyer_data for lawyer_data in ranked_lawyers
            if lawyer_data['lawyer'].id in selected_ids
        ][:max_recommendations]

    async def _generate_lawyer_recommendations(
        self,
        referral_id: uuid.UUID,
        context: Dict,
        *,
        refresh: bool = False,
        current_lawyer_ids: Optional[set] = None,
    ) -> List[LawyerProfile]:
        """Generate up to 3 lawyer recommendations using multi-factor rule-based scoring.

        Candidate pool:
          1. Featured (branch-out): lawyers explicitly listed in
             ``services/featured_lawyers.py`` when the feature flag is enabled.
             These are always placed first, in the order defined in that file.
          2. Preferred: lawyers whose ``legal_expertise`` contains the mapped
             expertise area, scored by ``_compute_lawyer_match``.
          3. Fallback: any lawyer (only used when no expertise match exists).

        Remaining recommendation slots (up to 3 total) after featured lawyers
        are filled by the highest-scoring non-featured candidates.
        Each candidate is left-joined with its ``LawyerAvailability`` row and
        the top results are persisted as ``LawyerRecommendation`` rows.
        """
        MAX_RECOMMENDATIONS = 3
        legal_area = context.get('legal_area', 'immigration')
        expertise_area = self._resolve_expertise_area(legal_area)

        with self.db.get_session() as session:
            # ── Step 1: Featured lawyers (branch-out system) ──────────────────
            featured_entries = []
            featured_ids: set = set()

            if featured_lawyers_config.is_enabled():
                featured_rows = featured_lawyers_config.fetch_featured_lawyers(
                    session, User, LawyerAvailability
                )
                for lawyer, availability in featured_rows:
                    if persona_enforcement_required() and not identity_verification_approved(session, lawyer.id):
                        continue
                    score, reasons = self._compute_lawyer_match(lawyer, availability, context)
                    featured_entries.append({'lawyer': lawyer, 'score': score, 'reasons': reasons})
                    featured_ids.add(lawyer.id)

            # ── Step 2: Score-based candidates (excluding featured lawyers) ───
            remaining_slots = max(0, MAX_RECOMMENDATIONS - min(len(featured_entries), MAX_RECOMMENDATIONS))
            scored = []

            if refresh or remaining_slots > 0:
                base_query = (
                    session.query(User, LawyerAvailability)
                    .outerjoin(LawyerAvailability, LawyerAvailability.lawyer_id == User.id)
                    .filter(
                        User.professional_type == 'Lawyer',
                        User.is_search_visible.is_(True),
                    )
                )

                if featured_ids:
                    base_query = base_query.filter(User.id.notin_(featured_ids))

                candidate_limit = 50 if refresh else 25
                fallback_limit = 30 if refresh else 10

                candidates = base_query.filter(
                    User.legal_expertise.contains([expertise_area])
                ).limit(candidate_limit).all()
                if persona_enforcement_required():
                    candidates = [
                        candidate
                        for candidate in candidates
                        if identity_verification_approved(session, candidate[0].id)
                    ]

                if not candidates:
                    candidates = base_query.limit(fallback_limit).all()
                    if persona_enforcement_required():
                        candidates = [
                            candidate
                            for candidate in candidates
                            if identity_verification_approved(session, candidate[0].id)
                        ]

                for lawyer, availability in candidates:
                    score, reasons = self._compute_lawyer_match(lawyer, availability, context)
                    scored.append({'lawyer': lawyer, 'score': score, 'reasons': reasons})

                scored.sort(key=lambda x: (-x['score'], str(x['lawyer'].id)))

            # ── Step 3: Merge — featured first, then scored fill remaining slots
            ranked_lawyers = []
            seen_lawyer_ids = set()
            for lawyer_data in featured_entries + scored:
                lawyer_id = lawyer_data['lawyer'].id
                if lawyer_id in seen_lawyer_ids:
                    continue
                ranked_lawyers.append(lawyer_data)
                seen_lawyer_ids.add(lawyer_id)

            top_lawyers = self._choose_lawyer_recommendation_entries(
                ranked_lawyers,
                refresh=refresh,
                current_lawyer_ids=current_lawyer_ids,
                max_recommendations=MAX_RECOMMENDATIONS,
            )

            # ── Step 4: Persist and build response ────────────────────────────
            if refresh:
                session.query(LawyerRecommendation).filter(
                    LawyerRecommendation.referral_request_id == referral_id
                ).delete(synchronize_session=False)

            recommended_lawyers: List[LawyerProfile] = []
            for i, lawyer_data in enumerate(top_lawyers):
                lawyer = lawyer_data['lawyer']
                is_best_suited = (i == 0)

                recommendation = LawyerRecommendation(
                    referral_request_id=referral_id,
                    lawyer_id=lawyer.id,
                    is_best_suited=is_best_suited,
                    match_score=lawyer_data['score'],
                    match_reasons=lawyer_data['reasons'],
                    recommendation_rank=i + 1
                )
                session.add(recommendation)

                lawyer_profile = LawyerProfile(
                    lawyer_id=str(lawyer.id),
                    name=f"{lawyer.first_name} {lawyer.last_name}",
                    firm=lawyer.professional_employer,
                    expertise=lawyer.legal_expertise or [],
                    is_best_suited=is_best_suited,
                    match_score=lawyer_data['score'],
                    match_reasons=lawyer_data['reasons'],
                    about=lawyer.about,
                    profile_picture=lawyer.profile_picture_path,
                    hourly_rate=float(lawyer.hourly_rate) if lawyer.hourly_rate else None,
                    firm_hourly_rate=float(lawyer.firm_hourly_rate) if lawyer.firm_hourly_rate else None,
                    sra_bsb=lawyer.sra_bsb,
                    sra_bsb_iaa=lawyer.sra_bsb_iaa
                )
                recommended_lawyers.append(lawyer_profile)

            session.commit()
            return recommended_lawyers

    def _compute_lawyer_match(
        self,
        lawyer: User,
        availability: Optional[LawyerAvailability],
        context: Dict,
    ) -> tuple:
        """Compute a weighted multi-factor match score (0-100) and human-readable reasons.

        Signals considered:
          - Legal expertise match (strongest)
          - Lawyer availability profile preferences (legal area, category, type,
            urgency, budget range)
          - Current capacity vs. daily cap
          - Profile completeness (firm, about, picture)
          - Location proximity (city match when available)
          - Stripe onboarding (can actually get paid)
          - Hourly-rate fit vs. user's budget range

        Returns a tuple ``(score, reasons)`` where ``reasons`` is a concise list of the
        strongest positive match reasons suitable for surfacing to the user.
        """
        legal_area = context.get('legal_area', 'immigration')
        expertise_area = self._resolve_expertise_area(legal_area)
        consultation_category = context.get('consultation_category')
        consultation_type = context.get('consultation_type')
        urgency_level = (context.get('urgency_level') or '').lower() or None
        user_location = (context.get('user_location') or '').strip().lower() or None

        budget_range = context.get('budget_range')
        normalized_budget = self._normalize_budget_range(budget_range) if budget_range else None

        score = 50  # neutral baseline
        reasons: List[str] = []

        expertise = lawyer.legal_expertise or []

        # 1. Legal expertise match - the single strongest signal.
        if expertise_area in expertise:
            score += 25
            reasons.append(f"Specializes in {expertise_area}")
        elif expertise:
            # Related-area match: lawyer has SOME expertise but not the exact one.
            score += 5

        # 2. Versatility across related areas.
        if len(expertise) > 1:
            other_areas = [a for a in expertise if a != expertise_area][:2]
            if other_areas:
                score += 4
                reasons.append(f"Also experienced in {', '.join(other_areas)}")

        # 3. Availability profile preferences (only if we have the joined row).
        if availability is not None:
            if availability.is_available:
                score += 4
            else:
                score -= 15  # soft penalty; we still allow surfacing if nothing else matches

            # Preferred legal areas
            if availability.preferred_legal_areas and expertise_area in availability.preferred_legal_areas:
                score += 4

            # Consultation category / type match
            if consultation_category and availability.consultation_categories and \
               consultation_category in availability.consultation_categories:
                score += 6
                reasons.append(f"Offers {consultation_category.replace('_', ' ')} consultations")

            if consultation_type and availability.consultation_types and \
               consultation_type in availability.consultation_types:
                score += 5

            # Urgency match
            if urgency_level and availability.preferred_urgency_levels and \
               urgency_level in availability.preferred_urgency_levels:
                score += 5
                if urgency_level in ('urgent', 'inferred_urgent'):
                    reasons.append("Handles urgent matters")

            # Budget range match
            if normalized_budget and availability.preferred_budget_ranges and \
               normalized_budget in availability.preferred_budget_ranges:
                score += 5

            # Daily capacity
            max_daily = availability.max_daily_consultations or 0
            current_daily = availability.current_daily_consultations or 0
            if max_daily > 0:
                if current_daily < max_daily:
                    score += 3
                else:
                    score -= 10  # at-capacity soft penalty

        # 4. Profile completeness (indicates a trustworthy, onboarded lawyer).
        if lawyer.professional_employer:
            score += 4
            reasons.append(f"Works at {lawyer.professional_employer}")

        if lawyer.about:
            score += 2

        if lawyer.profile_picture_path:
            score += 1

        # 5. Stripe onboarding - without this the lawyer can't actually be paid.
        if getattr(lawyer, 'stripe_connect_onboarded', False):
            score += 3

        # 6. Location proximity (simple city match fallback - no geo math available).
        if user_location and lawyer.town_city and lawyer.town_city.strip().lower() == user_location:
            score += 5
            reasons.append(f"Based in {lawyer.town_city}")

        # 7. Hourly-rate fit vs. budget range.
        bounds = self._BUDGET_RANGE_BOUNDS.get(normalized_budget) if normalized_budget else None
        try:
            rate = float(lawyer.hourly_rate) if lawyer.hourly_rate else None
        except (TypeError, ValueError):
            rate = None
        if bounds and rate is not None:
            lo, hi = bounds
            if lo <= rate <= hi:
                score += 4
            elif rate > hi * 1.5:
                score -= 3  # clearly over budget

        if not reasons:
            reasons.append("Qualified legal professional")

        # Cap reasons to top 4 to keep the card concise.
        reasons = reasons[:4]
        return min(100, max(0, score)), reasons

    def _calculate_lawyer_match_score(self, lawyer: User, context: Dict) -> int:
        """Backward-compatible wrapper around `_compute_lawyer_match`.

        Looks up the lawyer's availability row so callers that only have a `User` can still
        get a multi-factor score.
        """
        availability: Optional[LawyerAvailability] = None
        try:
            with self.db.get_session() as session:
                availability = session.query(LawyerAvailability).filter(
                    LawyerAvailability.lawyer_id == lawyer.id
                ).first()
        except Exception:  # pragma: no cover - defensive; score still works without it
            availability = None
        score, _ = self._compute_lawyer_match(lawyer, availability, context)
        return score

    def _generate_match_reasons(self, lawyer: User, context: Dict) -> List[str]:
        """Backward-compatible wrapper that returns only the reasons list."""
        availability: Optional[LawyerAvailability] = None
        try:
            with self.db.get_session() as session:
                availability = session.query(LawyerAvailability).filter(
                    LawyerAvailability.lawyer_id == lawyer.id
                ).first()
        except Exception:  # pragma: no cover
            availability = None
        _, reasons = self._compute_lawyer_match(lawyer, availability, context)
        return reasons

    async def _send_chat_history_to_inbox(self, referral: ReferralRequest, lawyer_id: uuid.UUID, session: Session):
        """Send the chat history to the lawyer's inbox as a PDF document"""
        try:
            # Prefer the live Redis transcript, then fall back to the copy
            # persisted when the referral was created.
            conversation_id = referral.conversation_id
            chat_history = await self.redis_service.get_chat_history(conversation_id)
            if not chat_history:
                chat_history = referral.chat_history or []

            if not chat_history:
                logger.warning(
                    "No chat history found in Redis or ReferralRequest for conversation %s; "
                    "manuscript PDF was not generated",
                    conversation_id,
                )
                return
            
            # Get user information for PDF metadata
            user = session.query(User).filter(User.id == referral.user_id).first()
            client_name = f"{user.first_name} {user.last_name}" if user else "Unknown Client"
            
            # Prepare metadata for PDF
            pdf_metadata = {
                'client_name': client_name,
                'referral_id': str(referral.id),
                'legal_area': referral.legal_area or "Not specified",
                'consultation_type': referral.consultation_type if referral.consultation_type else "Not specified",
                'urgency_level': referral.urgency_level if referral.urgency_level else "Not specified",
                'date_started': referral.created_at.strftime('%B %d, %Y at %H:%M UTC') if referral.created_at else ""
            }
            
            # Generate PDF from chat history
            try:
                pdf_bytes = self.pdf_service.generate_chat_history_pdf(
                    chat_history=chat_history,
                    referral_metadata=pdf_metadata
                )
                
                # Upload PDF to S3. Use a clean display name for the attachment;
                # uniqueness in S3 is handled internally by upload_file (which
                # generates a uuid-based object key under the referral folder).
                display_file_name = "Chat History.pdf"
                s3_metadata = self.s3_service.upload_file(
                    file_content=pdf_bytes,
                    file_name=display_file_name,
                    content_type="application/pdf",
                    referral_id=str(referral.id),
                    file_type="chat_history"
                )
                
                if s3_metadata:
                    # Create inbox message with PDF attachment
                    message_content = "📄 Chat History with LawNeeds AI\n\nPlease review the attached PDF document containing the full conversation transcript between the client and our AI assistant."
                    
                    initial_message = InboxMessage(
                        referral_request_id=referral.id,
                        sender_id=referral.user_id,
                        sender_type='user',
                        message_content=message_content,
                        message_type='chat_history',
                        file_url=s3_metadata['url'],
                        file_name=s3_metadata['original_filename'],
                        file_type='application/pdf',
                        file_size=s3_metadata['file_size'],
                        reply_to_message_id=None
                    )
                    
                    session.add(initial_message)
                    
                    # Also store in Redis for real-time updates
                    await self.redis_service.store_chat_message(
                        conversation_id=f"inbox:{referral.id}",
                        user_id=str(referral.user_id),
                        message={
                            'message_id': str(initial_message.id) if hasattr(initial_message, 'id') else str(uuid.uuid4()),
                            'sender_type': 'user',
                            'content': message_content,
                            'message_type': 'chat_history',
                            'file_url': s3_metadata['url'],
                            'file_name': s3_metadata['original_filename'],
                            'file_type': 'application/pdf',
                            'timestamp': datetime.now(timezone.utc).isoformat()
                        }
                    )
                    
                    logger.info(f"Chat history PDF sent to lawyer {lawyer_id} for referral {referral.id}")
                else:
                    # Fallback to text format if S3 upload fails
                    logger.warning(f"S3 upload failed, using text fallback for referral {referral.id}")
                    await self._send_chat_history_as_text(referral, chat_history, session)
                    
            except Exception as pdf_error:
                logger.error(f"Error generating/uploading PDF: {pdf_error}")
                # Fallback to text format if PDF generation fails
                logger.info(f"Using text fallback for referral {referral.id}")
                await self._send_chat_history_as_text(referral, chat_history, session)
            
        except Exception as e:
            logger.error(f"Error sending chat history to inbox: {e}")
            # Don't raise exception as this shouldn't block the referral process
    
    async def _send_chat_history_as_text(self, referral: ReferralRequest, chat_history: List[Dict], session: Session):
        """Fallback method to send chat history as text if PDF generation fails"""
        try:
            # Format chat history into a readable format
            formatted_chat = self._format_chat_history_for_inbox(chat_history)
            
            # Create an initial inbox message with the chat history
            initial_message = InboxMessage(
                referral_request_id=referral.id,
                sender_id=referral.user_id,
                sender_type='user',
                message_content=formatted_chat,
                message_type='chat_history',
                file_url=None,
                file_name=None,
                file_type=None,
                file_size=None,
                reply_to_message_id=None
            )
            
            session.add(initial_message)
            
            # Also store in Redis for real-time updates
            await self.redis_service.store_chat_message(
                conversation_id=f"inbox:{referral.id}",
                user_id=str(referral.user_id),
                message={
                    'message_id': str(initial_message.id) if hasattr(initial_message, 'id') else str(uuid.uuid4()),
                    'sender_type': 'user',
                    'content': formatted_chat,
                    'message_type': 'chat_history',
                    'file_url': None,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }
            )
            
            logger.info(f"Chat history text sent as fallback for referral {referral.id}")
            
        except Exception as e:
            logger.error(f"Error in text fallback: {e}")

    def _format_chat_history_for_inbox(self, chat_history: List[Dict]) -> str:
        """Format chat history into a readable message for the lawyer"""
        if not chat_history:
            return "No chat history available."
        
        formatted_messages = []
        formatted_messages.append("=== CHAT HISTORY WITH LAWNEEDS AI ===\n")
        formatted_messages.append("Below is the conversation the user had with our AI assistant before requesting a lawyer referral:\n")
        
        for message in chat_history:
            role = message.get('role', 'unknown')
            content = message.get('content', '')
            timestamp = message.get('timestamp', '')
            
            if role == 'user':
                formatted_messages.append(f"👤 USER: {content}")
            elif role == 'bot' or role == 'assistant':
                formatted_messages.append(f"🤖 AI ASSISTANT: {content}")
            
            if timestamp:
                try:
                    # Parse and format timestamp
                    from datetime import datetime
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    formatted_time = dt.strftime('%Y-%m-%d %H:%M:%S UTC')
                    formatted_messages.append(f"   ⏰ {formatted_time}")
                except:
                    pass
            
            formatted_messages.append("")  # Empty line for readability
        
        formatted_messages.append("=== END OF CHAT HISTORY ===")
        formatted_messages.append("\nThis chat history provides context about the user's legal situation and needs.")
        
        return "\n".join(formatted_messages)

    async def _handle_urgency_step(self, context_key: str, context: Dict) -> ReferralFlowResponse:
        """Present urgency selection options"""
        return ReferralFlowResponse(
            next_step="urgency",
            question="How urgent is your legal matter?",
            options=[
                {"value": "urgent", "label": "Urgent - Need help within 24 hours"},
                {"value": "within_week", "label": "Within a week"},
                {"value": "within_month", "label": "Within a month"},
                {"value": "not_urgent", "label": "Not urgent - can wait"}
            ],
            completed=False
        )

    async def _handle_urgency_selection(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle urgency selection and move to notes"""
        context['urgency_level'] = response
        await self._store_referral_context(context_key, context)
        
        return ReferralFlowResponse(
            next_step="notes",
            question="Please provide any additional notes or specific details about your legal matter (optional):",
            options=None,  # Free text input
            completed=False
        )

    async def _handle_additional_notes(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle additional notes and move to upload option"""
        # Store notes (can be empty)
        context['additional_notes'] = response.strip() if response else ""
        await self._store_referral_context(context_key, context)
        
        return ReferralFlowResponse(
            next_step="upload",
            question="Would you like to attach any supporting documents or voice notes? (optional)",
            options=[
                {"value": "voice_note", "label": "Record a voice note"},
                {"value": "pdf", "label": "Upload a PDF document"},
                {"value": "docx", "label": "Upload a Word document (.docx)"},
                {"value": "image", "label": "Upload an image"},
                {"value": "skip", "label": "Skip - no attachments"}
            ],
            completed=False
        )

    async def _handle_file_upload_option(self, context_key: str, context: Dict, response: str) -> ReferralFlowResponse:
        """Handle file upload option selection"""
        if response == "skip":
            # No file upload, proceed to confirmation
            return await self._prepare_confirmation(context_key, context)
        
        # Store upload choice and show upload instructions
        context['upload_type'] = response
        await self._store_referral_context(context_key, context)

        if response == "voice_note":
            message = "Please record your voice note by clicking the microphone button below. You can speak for up to 5 minutes and your message will be automatically transcribed. Maximum size: 50MB."
        elif response == "pdf":
            message = "Please select your PDF document to upload. Maximum size: 25MB."
        elif response == "docx":
            message = "Please select your Word document (.docx) to upload. Maximum size: 25MB."
        elif response == "image":
            message = "Please select an image to upload (JPEG, PNG, GIF, BMP, WebP, TIFF, SVG). Maximum size: 10MB."
        else:
            message = "Please upload your file. Supported formats: PDF, DOCX, images, or voice notes."
        
        return ReferralFlowResponse(
            next_step="confirm",  # After file upload, go to confirmation
            question=f"{message}\n\nOnce completed, we'll proceed to review your request.",
            options=None,
            completed=False
        )
