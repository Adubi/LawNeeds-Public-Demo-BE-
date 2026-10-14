from fastapi import HTTPException, APIRouter, Request, Body, Query, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
import logging
import uuid
from datetime import datetime, timezone
from services.models import (
    ReferralRequest, ReferralSession, LawyerInbox, InboxMessage, User, ReferralStatus, DeviceToken,
    LawyerRecommendation, ConsultationBookingRequest
)
from services.notification_service import NotificationService
from services.redis_service import RedisService
from services.s3_service import S3Service
from services.connection_manager import inbox_manager
from services.contact_details import assert_no_contact_details
from services.firebase_notification_service import FirebaseNotificationService
from services.referral_display_helpers import (
    get_budget_range_display,
    get_urgency_level_display,
    get_consultation_duration_display,
    get_consultation_type_display,
)
from services.referral_consultation_pricing_display import (
    CASEWORK_LABEL_BY_SERVICE,
    CASEWORK_PRICE_BY_SERVICE,
)
from services.upload_multipart import (
    collect_upload_files_from_form,
    inbox_mime_to_category,
    public_attachment_display_name,
    try_pdf_document_title,
    infer_referral_file_type,
    content_sha256_hex,
    check_duplicate_filename,
    names_from_attachment_metadata_dict,
)
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func, desc
import json
import mimetypes

# Set up logging
logger = logging.getLogger(__name__)

# Pydantic models for request/response
class LawyerProfile(BaseModel):
    """Lawyer profile information for case details"""
    lawyer_id: str
    name: str
    firm: Optional[str] = None
    expertise: List[str]
    about: Optional[str] = None
    profile_picture: Optional[str] = None
    tagline: Optional[str] = None
    professional_employer: Optional[str] = None
    hourly_rate: Optional[float] = None
    firm_hourly_rate: Optional[float] = None
    push_token: Optional[str] = None

class InboxCaseTile(BaseModel):
    """Tile view for user inbox showing case overview"""
    referral_id: str
    conversation_id: str
    consultation_type: str
    consultation_duration: Optional[str] = None
    legal_area: str
    date_started: str
    urgency_level: str
    status: str
    lawyer_name: Optional[str] = None
    lawyer_profile: Optional[LawyerProfile] = None
    lawyer_response_status: Optional[str] = None
    unread_messages: int = 0
    session_id: Optional[str] = None
    session_status: Optional[str] = None
    casework_service_type: Optional[str] = None
    casework_service_label: Optional[str] = None
    casework_price_range: Optional[str] = None
    casework_budget_gbp: Optional[int] = None

class ClientProfile(BaseModel):
    """Client profile information for lawyer view"""
    client_name: str
    date_of_birth: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    about: Optional[str] = None
    tagline: Optional[str] = None
    immigration_status: Optional[str] = None
    profile_picture_path: Optional[str] = None
    user_location: Optional[str] = None  # From referral request
    push_token: Optional[str] = None

class CaseDetails(BaseModel):
    """Detailed case information for user inbox"""
    referral_id: str
    conversation_id: str
    client_profile: Optional[ClientProfile] = None  # Enhanced client profile for lawyer view
    lawyer_profile: Optional[LawyerProfile] = None  # For user view
    selected_service: str
    consultation_type: str
    consultation_duration: Optional[str] = None
    urgency_level: str
    budget_range: Optional[str] = None
    casework_service_type: Optional[str] = None
    casework_service_label: Optional[str] = None
    casework_price_range: Optional[str] = None
    casework_budget_gbp: Optional[int] = None
    booking: Optional[Dict[str, Any]] = None
    user_location: Optional[str] = None
    issue_summary: Optional[str] = None
    additional_notes: Optional[str] = None
    confirmation_summary: Optional[str] = None  # Add confirmation summary field
    voice_note_url: Optional[str] = None
    pdf_attachment_url: Optional[str] = None
    docx_attachment_url: Optional[str] = None
    image_attachment_url: Optional[str] = None
    attachments: List[Dict[str, Any]] = []
    voice_note: Optional[Dict[str, Any]] = None
    can_access_messages: bool = False
    session_id: Optional[str] = None
    session_status: Optional[str] = None
    consultation_state: str = "none"
    can_start_consultation: bool = False
    timer_started_at: Optional[str] = None
    timer_ended_at: Optional[str] = None
    status: str
    created_at: str

class InboxMessageResponse(BaseModel):
    """Individual message in conversation thread"""
    message_id: str
    sender_id: str
    sender_type: str
    sender_name: str
    message_content: Optional[str] = None
    message_type: str
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    file_size: Optional[int] = None
    file_attachments: Optional[List[Dict[str, Any]]] = None
    is_read: bool
    created_at: str
    reply_to_message_id: Optional[str] = None

class SendMessageRequest(BaseModel):
    """Request to send a message in conversation"""
    message_content: Optional[str] = None
    message_type: str = "text"
    reply_to_message_id: Optional[str] = None

class CancelCaseRequest(BaseModel):
    """Request to cancel a case"""
    cancellation_reason: Optional[str] = Field(None, description="Optional reason for cancellation")

class DeleteInboxItemsRequest(BaseModel):
    """Request to delete multiple inbox items"""
    referral_ids: List[str] = Field(..., description="List of referral IDs to delete")

class DeleteInboxResponse(BaseModel):
    """Response for inbox deletion operations"""
    success: bool
    message: str
    deleted_count: int
    deleted_ids: List[str]
    failed_ids: List[str] = []
    errors: Optional[Dict[str, str]] = None

class LawyerInboxItem(BaseModel):
    """Lawyer inbox item for pending requests"""
    inbox_id: str
    referral_id: str
    user_name: str
    user_profile: Optional[ClientProfile] = None
    legal_area: str
    consultation_type: str
    urgency_level: str
    issue_summary: str
    budget_range: Optional[str] = None
    casework_service_type: Optional[str] = None
    casework_service_label: Optional[str] = None
    casework_price_range: Optional[str] = None
    casework_budget_gbp: Optional[int] = None
    priority_score: int
    created_at: str
    attachments_count: int = 0

class LawyerCaseItem(BaseModel):
    """Active case for lawyer dashboard"""
    referral_id: str
    session_id: str
    user_name: str
    user_profile: Optional[ClientProfile] = None
    legal_area: str
    consultation_type: str
    consultation_duration: Optional[str] = None
    session_status: str
    unread_messages: int = 0
    last_message_at: Optional[str] = None
    started_at: str
    casework_service_type: Optional[str] = None
    casework_service_label: Optional[str] = None
    casework_price_range: Optional[str] = None
    casework_budget_gbp: Optional[int] = None

class InboxAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.redis_service = RedisService()
        self.s3_service = S3Service()
        self.firebase_service = FirebaseNotificationService()

    async def _publish_or_broadcast_inbox(
        self,
        payload: Dict[str, Any],
        referral_id: str,
        exclude_user: Optional[str] = None,
    ) -> None:
        """Publish inbox event via Redis pub/sub, falling back to local sockets."""
        published = await self.redis_service.publish_inbox_event(
            referral_id,
            payload,
            exclude_user_id=exclude_user,
        )
        if not published:
            await inbox_manager.broadcast_to_inbox(
                json.dumps(payload),
                referral_id,
                exclude_user=exclude_user,
            )
    
    def _get_user_device_tokens(self, session, user_id: str) -> list:
        """Return all active FCM device tokens for a user."""
        try:
            devices = session.query(DeviceToken).filter(
                DeviceToken.user_id == uuid.UUID(user_id),
                DeviceToken.is_active == True
            ).all()
            return [d.device_token for d in devices]
        except Exception:
            return []

    def _get_user_push_token(self, session, user_id: str) -> Optional[str]:
        """
        Get the primary (most recently used) active push token for a user.
        Returns None if no active tokens are found.
        """
        try:
            device = session.query(DeviceToken).filter(
                DeviceToken.user_id == uuid.UUID(user_id),
                DeviceToken.is_active == True
            ).order_by(DeviceToken.last_used_at.desc()).first()
            
            return device.device_token if device else None
        except Exception as e:
            logger.warning(f"Error fetching push token for user {user_id}: {e}")
            return None

    def _is_casework_referral(self, referral: ReferralRequest) -> bool:
        return (
            (referral.consultation_category or "").strip().lower() == "casework"
            or (referral.consultation_type or "").strip().lower() == "casework"
        )

    def _consultation_duration_for_response(self, referral: ReferralRequest) -> Optional[str]:
        if self._is_casework_referral(referral):
            return None
        return get_consultation_type_display(referral.consultation_type) if referral.consultation_type else "Not specified"

    def _casework_payload(self, referral: ReferralRequest) -> Dict[str, Any]:
        service_type = referral.casework_service_type
        return {
            "casework_service_type": service_type,
            "casework_service_label": CASEWORK_LABEL_BY_SERVICE.get(service_type) if service_type else None,
            "casework_price_range": CASEWORK_PRICE_BY_SERVICE.get(service_type) if service_type else None,
            "casework_budget_gbp": referral.casework_budget_gbp,
        }

    def _booking_payload(self, session: Session, referral: ReferralRequest) -> Optional[Dict[str, Any]]:
        booking = session.query(ConsultationBookingRequest).filter(
            ConsultationBookingRequest.referral_request_id == referral.id
        ).first()
        if not booking:
            return None
        return {
            "id": str(booking.id),
            "status": booking.status,
            "start_at": booking.requested_start_at.isoformat(),
            "end_at": booking.requested_end_at.isoformat(),
            "timezone": booking.timezone,
            "consultation_type": booking.consultation_type,
            "consultation_category": booking.consultation_category,
        }

    def _inbox_message_file_attachments_payload(self, msg: InboxMessage) -> Optional[List[Dict[str, Any]]]:
        if msg.file_attachments:
            # Refresh presigned URLs for stored attachments
            refreshed_attachments = []
            for att in msg.file_attachments:
                if not isinstance(att, dict):
                    continue
                refreshed_att = dict(att)
                # Regenerate presigned URL using s3_key or by extracting from stored URL
                s3_key = att.get("s3_key")
                stored_url = att.get("url")
                if s3_key or stored_url:
                    fresh_url = self.s3_service.refresh_presigned_url(
                        url=stored_url, s3_key=s3_key, expiration=3600
                    )
                    if fresh_url:
                        refreshed_att["url"] = fresh_url
                refreshed_attachments.append(refreshed_att)
            return refreshed_attachments if refreshed_attachments else None
        if msg.file_url:
            meta = {
                "original_filename": msg.file_name,
                "file_name": msg.file_name,
            }
            # Refresh presigned URL for legacy single-file attachment
            fresh_url = self.s3_service.refresh_presigned_url(url=msg.file_url, expiration=3600)
            return [
                {
                    "url": fresh_url or msg.file_url,
                    "file_name": msg.file_name,
                    "display_name": public_attachment_display_name(
                        kind=inbox_mime_to_category(msg.file_type, msg.file_name),
                        metadata=meta,
                        original_filename=msg.file_name,
                        placeholder=msg.file_name or "Attachment",
                    ),
                    "file_type": msg.file_type,
                    "file_size": msg.file_size,
                    "category": inbox_mime_to_category(msg.file_type, msg.file_name),
                }
            ]
        return None

    def _case_details_attachments_from_referral(
        self, referral: ReferralRequest
    ) -> tuple:  # (attachments, voice_note)
        """Build attachments list and optional voice_note highlight for CaseDetails."""
        attachments: List[Dict[str, Any]] = []
        voice_note = None
        files_meta = (
            referral.attachment_metadata.get("files")
            if referral.attachment_metadata
            else None
        )
        if files_meta:
            for f in files_meta:
                url = f.get("url") or (f.get("metadata") or {}).get("url")
                if not url:
                    continue
                kind = f.get("kind", "file")
                meta = f.get("metadata")
                # Get s3_key from metadata for URL refresh
                s3_key = (meta or {}).get("s3_key") if isinstance(meta, dict) else None
                # Refresh presigned URL
                fresh_url = self.s3_service.refresh_presigned_url(
                    url=url, s3_key=s3_key, expiration=3600
                )
                display_name = public_attachment_display_name(
                    kind=kind,
                    metadata=meta if isinstance(meta, dict) else None,
                    original_filename=f.get("original_filename"),
                    placeholder=None,
                )
                entry = {
                    "type": kind,
                    "url": fresh_url or url,
                    "name": display_name,
                    "document_title": (meta or {}).get("document_title") if isinstance(meta, dict) else None,
                    "metadata": meta,
                }
                attachments.append(entry)
                if kind == "voice_note" and not voice_note:
                    voice_note = entry
        else:
            am = referral.attachment_metadata or {}
            if referral.pdf_attachment_url:
                pm = am.get("pdf")
                s3_key = (pm or {}).get("s3_key") if isinstance(pm, dict) else None
                fresh_url = self.s3_service.refresh_presigned_url(
                    url=referral.pdf_attachment_url, s3_key=s3_key, expiration=3600
                )
                attachments.append({
                    "type": "pdf",
                    "url": fresh_url or referral.pdf_attachment_url,
                    "name": public_attachment_display_name(
                        kind="pdf",
                        metadata=pm if isinstance(pm, dict) else None,
                        placeholder="PDF",
                    ),
                    "document_title": pm.get("document_title") if isinstance(pm, dict) else None,
                    "metadata": pm if isinstance(pm, dict) else None,
                })

            if referral.docx_attachment_url:
                dm = am.get("docx")
                s3_key = (dm or {}).get("s3_key") if isinstance(dm, dict) else None
                fresh_url = self.s3_service.refresh_presigned_url(
                    url=referral.docx_attachment_url, s3_key=s3_key, expiration=3600
                )
                attachments.append({
                    "type": "docx",
                    "url": fresh_url or referral.docx_attachment_url,
                    "name": public_attachment_display_name(
                        kind="docx",
                        metadata=dm if isinstance(dm, dict) else None,
                        placeholder="Document",
                    ),
                    "metadata": dm if isinstance(dm, dict) else None,
                })

            if referral.image_attachment_url:
                im = am.get("image")
                s3_key = (im or {}).get("s3_key") if isinstance(im, dict) else None
                fresh_url = self.s3_service.refresh_presigned_url(
                    url=referral.image_attachment_url, s3_key=s3_key, expiration=3600
                )
                attachments.append({
                    "type": "image",
                    "url": fresh_url or referral.image_attachment_url,
                    "name": public_attachment_display_name(
                        kind="image",
                        metadata=im if isinstance(im, dict) else None,
                        placeholder="Image",
                    ),
                    "metadata": im if isinstance(im, dict) else None,
                })

            if referral.voice_note_url:
                vm = am.get("voice_note")
                s3_key = (vm or {}).get("s3_key") if isinstance(vm, dict) else None
                fresh_url = self.s3_service.refresh_presigned_url(
                    url=referral.voice_note_url, s3_key=s3_key, expiration=3600
                )
                vn_name = public_attachment_display_name(
                    kind="voice_note",
                    metadata=vm if isinstance(vm, dict) else None,
                    placeholder="Voice note",
                )
                voice_note = {
                    "type": "voice_note",
                    "url": fresh_url or referral.voice_note_url,
                    "name": vn_name,
                    "metadata": vm if isinstance(vm, dict) else None,
                }
                attachments.append(voice_note)
        return attachments, voice_note

    def _existing_inbox_attachment_names(
        self, db_session: Session, referral_request_id: uuid.UUID
    ) -> Dict[str, str]:
        """Normalized filename -> display label from prior inbox message attachments."""
        out: Dict[str, str] = {}
        rows = (
            db_session.query(InboxMessage)
            .filter(InboxMessage.referral_request_id == referral_request_id)
            .all()
        )
        for msg in rows:
            blob = msg.file_attachments
            if not isinstance(blob, list):
                continue
            for att in blob:
                if not isinstance(att, dict):
                    continue
                label = (
                    att.get("display_name")
                    or att.get("file_name")
                    or ""
                )
                if not label:
                    continue
                out[str(label).lower().strip()] = str(label)
        return out

    async def _upload_inbox_attachments(
        self,
        upload_files: List[UploadFile],
        referral_id: str,
        *,
        existing_name_labels: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        existing = dict(existing_name_labels or {})
        batch: Dict[str, str] = {}
        for f in upload_files:
            if not f.filename:
                continue
            file_content = await f.read()
            file_size = len(file_content)
            check_duplicate_filename(f.filename, existing, batch)
            batch[f.filename.lower().strip()] = f.filename
            digest = content_sha256_hex(file_content).lower()
            file_type = f.content_type or mimetypes.guess_type(f.filename)[0]
            # boto3 rejects None as ContentType; fall back to a safe default so
            # recordings and other files with unrecognised MIME types still upload.
            safe_content_type = file_type or "application/octet-stream"
            kind = infer_referral_file_type(file_type, f.filename)
            max_allowed = self.s3_service.get_max_file_size(kind)
            if file_size > max_allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"File too large. Maximum size is {max_allowed // (1024*1024)}MB for this file type.",
                )
            file_url_result = self.s3_service.upload_file(
                file_content=file_content,
                file_name=f.filename,
                content_type=safe_content_type,
                referral_id=referral_id,
                file_type="inbox_attachment",
            )
            if not file_url_result:
                raise HTTPException(status_code=500, detail="Failed to upload file to S3")
            file_url_result["content_sha256"] = digest
            meta_for_label: Dict[str, Any] = {
                "original_filename": f.filename,
                **file_url_result,
            }
            if (file_type or "").lower() == "application/pdf":
                pdf_title = try_pdf_document_title(file_content)
                if pdf_title:
                    meta_for_label["document_title"] = pdf_title
                    file_url_result["document_title"] = pdf_title
            display_name = public_attachment_display_name(
                kind=inbox_mime_to_category(file_type, f.filename),
                metadata=meta_for_label,
                original_filename=f.filename,
                placeholder=f.filename,
            )
            attachments.append(
                {
                    "url": file_url_result["url"],
                    "file_name": f.filename,
                    "display_name": display_name,
                    "document_title": meta_for_label.get("document_title"),
                    "file_type": file_type,
                    "file_size": file_size,
                    "category": inbox_mime_to_category(file_type, f.filename),
                    "s3_key": file_url_result.get("s3_key"),
                    "content_sha256": digest,
                }
            )
        return attachments

    def _assert_consultation_paid(
        self,
        db_session: Session,
        referral_id_str: str,
        *,
        lawyer_id_str: Optional[str] = None,
    ) -> None:
        """Raise HTTP 403 if the consultation session has not been paid.

        Called by all four send-message endpoints so payment is always enforced
        before a message is persisted. When *lawyer_id_str* is provided the
        session lookup is scoped to that lawyer; otherwise any session row for
        the referral is used (user-side check).
        """
        try:
            from services.consultation_payment_service import ConsultationPaymentService
            filters = [
                ReferralSession.referral_request_id == uuid.UUID(referral_id_str),
                ReferralSession.session_status.in_(["pending", "active", "completed"]),
            ]
            if lawyer_id_str:
                filters.append(ReferralSession.lawyer_id == uuid.UUID(lawyer_id_str))

            session_obj = db_session.query(ReferralSession).filter(
                and_(*filters)
            ).order_by(desc(ReferralSession.created_at)).first()

            if not session_obj:
                # No session yet means no price has been proposed — block.
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "payment_required",
                        "message": "Cannot send messages until the consultation fee has been paid.",
                    },
                )

            pay_service = ConsultationPaymentService(db_session)
            summary = pay_service.get_payment_summary(session_obj.id)
            payment_status = (summary or {}).get("payment_status")

            if payment_status not in ("paid", "transferred"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "payment_required",
                        "message": "Cannot send messages until the consultation fee has been paid.",
                        "payment_status": payment_status or "pending",
                    },
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Payment gate check failed, blocking message: %s", exc)
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "payment_required",
                    "message": "Cannot verify payment status. Please try again.",
                },
            )

    def _get_latest_consultation_session(
        self, db_session: Session, referral: ReferralRequest
    ) -> Optional[ReferralSession]:
        """Most recent session for the matched lawyer, regardless of status."""
        if not referral.matched_lawyer_id:
            return None
        return db_session.query(ReferralSession).filter(
            and_(
                ReferralSession.referral_request_id == referral.id,
                ReferralSession.lawyer_id == referral.matched_lawyer_id,
            )
        ).order_by(desc(ReferralSession.created_at)).first()

    def _describe_consultation_state(
        self,
        db_session: Session,
        latest_session: Optional[ReferralSession],
    ) -> Dict[str, Any]:
        """
        Summarize the consultation lifecycle for a referral so the FE can
        unambiguously drive the "Start Consultation" CTA.

        Returns a dict with:
          - consultation_state: one of
              'none'              - no session row yet (lawyer hasn't accepted)
              'awaiting_payment'  - session pending, payment not yet paid/transferred
              'ready_to_start'    - session pending, payment paid, timer not started
              'in_progress'       - timer currently running
              'completed'         - timer ended
              'cancelled'         - session cancelled
          - can_start_consultation: True only in 'ready_to_start'
          - timer_started_at / timer_ended_at: ISO strings or None
        """
        result = {
            "consultation_state": "none",
            "can_start_consultation": False,
            "timer_started_at": None,
            "timer_ended_at": None,
        }

        if not latest_session:
            return result

        timer_started_at = latest_session.timer_started_at
        timer_ended_at = latest_session.timer_ended_at
        result["timer_started_at"] = (
            timer_started_at.isoformat() if timer_started_at else None
        )
        result["timer_ended_at"] = (
            timer_ended_at.isoformat() if timer_ended_at else None
        )

        status = latest_session.session_status
        if status == "completed" or timer_ended_at is not None:
            result["consultation_state"] = "completed"
            return result
        if status == "cancelled":
            result["consultation_state"] = "cancelled"
            return result
        if status == "active" or (timer_started_at and not timer_ended_at):
            result["consultation_state"] = "in_progress"
            return result

        # session_status == 'pending' (or any other non-terminal value): gate on payment
        payment_status = None
        try:
            from services.consultation_payment_service import ConsultationPaymentService
            pay_service = ConsultationPaymentService(db_session)
            payment_summary = pay_service.get_payment_summary(latest_session.id)
            if payment_summary:
                payment_status = payment_summary.get("payment_status")
        except Exception as exc:  # defensive: never break the details endpoint
            logger.warning(
                f"Failed to read payment summary for session {latest_session.id}: {exc}"
            )

        if payment_status in ("paid", "transferred"):
            result["consultation_state"] = "ready_to_start"
            result["can_start_consultation"] = True
        else:
            result["consultation_state"] = "awaiting_payment"
        return result

    def get_router(self):
        router = APIRouter()

        @router.get("/inbox/user/cases", response_model=List[InboxCaseTile])
        async def get_user_cases(request: Request):
            """Get all user's referral cases for inbox tiles view"""
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Get referral requests for this user that have completed lawyer selection
                    # Only show referrals where the user successfully completed the select_lawyer step
                    completed_statuses = [
                        ReferralStatus.MATCHED.value,
                        ReferralStatus.ACTIVE.value, 
                        ReferralStatus.COMPLETED.value,
                        ReferralStatus.CANCELLED.value,
                        ReferralStatus.EXPIRED.value
                    ]
                    
                    query = session.query(ReferralRequest).filter(
                        ReferralRequest.user_id == uuid.UUID(user_id),
                        ReferralRequest.status.in_(completed_statuses)
                    ).order_by(desc(ReferralRequest.created_at))
                    
                    referrals = query.all()
                    cases = []
                    
                    for referral in referrals:
                        # Get lawyer info if matched
                        lawyer_name = None
                        lawyer_profile = None
                        lawyer_response_status = None
                        
                        if referral.matched_lawyer_id:
                            lawyer = session.query(User).filter(
                                User.id == referral.matched_lawyer_id
                            ).first()
                            if lawyer:
                                lawyer_name = f"{lawyer.first_name} {lawyer.last_name}"
                                lawyer_profile = LawyerProfile(
                                    lawyer_id=str(lawyer.id),
                                    name=f"{lawyer.first_name} {lawyer.last_name}",
                                    firm=lawyer.professional_employer,
                                    expertise=lawyer.legal_expertise or [],
                                    about=lawyer.about,
                                    profile_picture=lawyer.profile_picture_path,
                                    tagline=lawyer.tagline,
                                    professional_employer=lawyer.professional_employer,
                                    push_token=self._get_user_push_token(session, str(lawyer.id))
                                )
                        
                        # Get lawyer inbox status
                        inbox_entry = session.query(LawyerInbox).filter(
                            LawyerInbox.referral_request_id == referral.id
                        ).first()
                        
                        if inbox_entry:
                            lawyer_response_status = inbox_entry.status
                        
                        latest_session = self._get_latest_consultation_session(session, referral)
                        if latest_session:
                            date_started = (
                                latest_session.start_time or latest_session.created_at
                            ).isoformat()
                        else:
                            date_started = referral.created_at.isoformat()
                        
                        # Count unread messages
                        unread_count = 0
                        if lawyer_response_status == 'accepted':
                            unread_count = session.query(InboxMessage).filter(
                                and_(
                                    InboxMessage.referral_request_id == referral.id,
                                    InboxMessage.sender_type == 'lawyer',
                                    InboxMessage.is_read == False
                                )
                            ).count()
                        
                        cases.append(InboxCaseTile(
                            referral_id=str(referral.id),
                            conversation_id=referral.conversation_id,
                            consultation_type=get_consultation_duration_display(referral.consultation_category),
                            consultation_duration=self._consultation_duration_for_response(referral),
                            legal_area=referral.legal_area,
                            date_started=date_started,
                            urgency_level=get_urgency_level_display(referral.urgency_level),
                            status=referral.status,
                            lawyer_name=lawyer_name,
                            lawyer_profile=lawyer_profile,
                            lawyer_response_status=lawyer_response_status,
                            unread_messages=unread_count,
                            session_id=str(latest_session.id) if latest_session else None,
                            session_status=latest_session.session_status if latest_session else None,
                            **self._casework_payload(referral),
                        ))
                    
                    return cases
                    
            except Exception as e:
                logger.error(f"Error getting user cases: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving cases")

        @router.get("/inbox/user/case/{referral_id}", response_model=CaseDetails)
        async def get_case_details(referral_id: str, request: Request):
            """Get detailed case information for user"""
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                try:
                    referral_uuid = uuid.UUID(referral_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid case ID")
                
                with self.db.get_session() as session:
                    # Get referral request
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == referral_uuid,
                            ReferralRequest.user_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")
                    
                    # Get lawyer profile if matched
                    lawyer_profile = None
                    can_access_messages = False
                    
                    if referral.matched_lawyer_id:
                        lawyer = session.query(User).filter(
                            User.id == referral.matched_lawyer_id
                        ).first()
                        
                        if lawyer:
                            lawyer_profile = LawyerProfile(
                                lawyer_id=str(lawyer.id),
                                name=f"{lawyer.first_name} {lawyer.last_name}",
                                firm=lawyer.professional_employer,
                                expertise=lawyer.legal_expertise or [],
                                about=lawyer.about,
                                profile_picture=lawyer.profile_picture_path,
                                tagline=lawyer.tagline,
                                professional_employer=lawyer.professional_employer,
                                hourly_rate=float(lawyer.hourly_rate) if lawyer.hourly_rate else None,
                                firm_hourly_rate=float(lawyer.firm_hourly_rate) if lawyer.firm_hourly_rate else None,
                                push_token=self._get_user_push_token(session, str(lawyer.id))
                            )
                        
                        
                        
                        
                        
                        
                        # Check if lawyer has accepted the case
                        inbox_entry = session.query(LawyerInbox).filter(
                            LawyerInbox.referral_request_id == referral.id
                        ).first()
                        
                        if inbox_entry and inbox_entry.status == 'accepted':
                            can_access_messages = True
                    
                    attachments, voice_note = self._case_details_attachments_from_referral(referral)
                    
                    # Refresh individual URL fields for backward compatibility
                    am = referral.attachment_metadata or {}
                    refreshed_voice_note_url = self.s3_service.refresh_presigned_url(
                        url=referral.voice_note_url,
                        s3_key=(am.get("voice_note") or {}).get("s3_key") if isinstance(am.get("voice_note"), dict) else None,
                        expiration=3600
                    ) if referral.voice_note_url else None
                    refreshed_pdf_url = self.s3_service.refresh_presigned_url(
                        url=referral.pdf_attachment_url,
                        s3_key=(am.get("pdf") or {}).get("s3_key") if isinstance(am.get("pdf"), dict) else None,
                        expiration=3600
                    ) if referral.pdf_attachment_url else None
                    refreshed_docx_url = self.s3_service.refresh_presigned_url(
                        url=referral.docx_attachment_url,
                        s3_key=(am.get("docx") or {}).get("s3_key") if isinstance(am.get("docx"), dict) else None,
                        expiration=3600
                    ) if referral.docx_attachment_url else None
                    refreshed_image_url = self.s3_service.refresh_presigned_url(
                        url=referral.image_attachment_url,
                        s3_key=(am.get("image") or {}).get("s3_key") if isinstance(am.get("image"), dict) else None,
                        expiration=3600
                    ) if referral.image_attachment_url else None
                    
                    latest_session = self._get_latest_consultation_session(session, referral)
                    state_info = self._describe_consultation_state(session, latest_session)
                    return CaseDetails(
                        referral_id=str(referral.id),
                        conversation_id=referral.conversation_id,
                        lawyer_profile=lawyer_profile,
                        selected_service=referral.legal_area,
                        consultation_type=get_consultation_duration_display(referral.consultation_category),
                        consultation_duration=self._consultation_duration_for_response(referral),
                        urgency_level=get_urgency_level_display(referral.urgency_level),
                        budget_range=get_budget_range_display(referral.budget_range),
                        **self._casework_payload(referral),
                        booking=self._booking_payload(session, referral),
                        user_location=referral.user_location,
                        issue_summary=referral.issue_summary,
                        additional_notes=(referral.additional_info.strip() if referral.additional_info and referral.additional_info.strip() else None),
                        voice_note_url=refreshed_voice_note_url,
                        pdf_attachment_url=refreshed_pdf_url,
                        docx_attachment_url=refreshed_docx_url,
                        image_attachment_url=refreshed_image_url,
                        attachments=attachments,
                        voice_note=voice_note,
                        can_access_messages=can_access_messages,
                        session_id=str(latest_session.id) if latest_session else None,
                        session_status=latest_session.session_status if latest_session else None,
                        consultation_state=state_info["consultation_state"],
                        can_start_consultation=state_info["can_start_consultation"],
                        timer_started_at=state_info["timer_started_at"],
                        timer_ended_at=state_info["timer_ended_at"],
                        status=referral.status,
                        created_at=referral.created_at.isoformat()
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting case details: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving case details")

        @router.get("/inbox/user/case/{referral_id}/messages", response_model=List[InboxMessageResponse])
        async def get_case_messages(referral_id: str, request: Request):
            """Get conversation thread for a case (only if lawyer accepted)"""
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Verify user owns this referral
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.user_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")
                    
                    # Check if lawyer has accepted the case
                    inbox_entry = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == referral.id
                    ).first()
                    
                    if not inbox_entry or inbox_entry.status != 'accepted':
                        raise HTTPException(status_code=403, detail="Messages not accessible until lawyer accepts the case")
                    
                    # Get messages
                    messages_query = session.query(InboxMessage).filter(
                        InboxMessage.referral_request_id == uuid.UUID(referral_id)
                    ).order_by(InboxMessage.created_at)
                    
                    messages = messages_query.all()
                    
                    # Mark user's unread messages as read
                    session.query(InboxMessage).filter(
                        and_(
                            InboxMessage.referral_request_id == uuid.UUID(referral_id),
                            InboxMessage.sender_type == 'lawyer',
                            InboxMessage.is_read == False
                        )
                    ).update({
                        'is_read': True,
                        'read_at': datetime.now(timezone.utc)
                    })
                    session.commit()
                    
                    # Format messages for response
                    formatted_messages = []
                    for msg in messages:
                        # Get sender name
                        sender = session.query(User).filter(User.id == msg.sender_id).first()
                        sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Unknown"
                        
                        # Refresh file_url for legacy single-file attachments
                        refreshed_file_url = self.s3_service.refresh_presigned_url(
                            url=msg.file_url, expiration=3600
                        ) if msg.file_url else None
                        
                        formatted_messages.append(InboxMessageResponse(
                            message_id=str(msg.id),
                            sender_id=str(msg.sender_id),
                            sender_type=msg.sender_type,
                            sender_name=sender_name,
                            message_content=msg.message_content,
                            message_type=msg.message_type,
                            file_url=refreshed_file_url,
                            file_name=msg.file_name,
                            file_type=msg.file_type,
                            file_size=msg.file_size,
                            file_attachments=self._inbox_message_file_attachments_payload(msg),
                            is_read=msg.is_read,
                            created_at=msg.created_at.isoformat(),
                            reply_to_message_id=str(msg.reply_to_message_id) if msg.reply_to_message_id else None
                        ))
                    
                    return formatted_messages
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting case messages: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving messages")

        @router.post("/inbox/user/case/{referral_id}/message")
        async def send_user_message(referral_id: str, request: Request):
            """Send a message from user to lawyer. Multipart fields: message_content, message_type, reply_to_message_id.
            Attach multiple files using repeated form fields: files, attachments, file, or attachment."""
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                form = await request.form()
                raw_content = form.get("message_content")
                message_content = str(raw_content) if raw_content is not None else None
                message_type = str(form.get("message_type") or "text")
                reply_to_message_id = form.get("reply_to_message_id")
                upload_files = collect_upload_files_from_form(form)
                assert_no_contact_details(message_content)
                for upload_file in upload_files:
                    assert_no_contact_details(upload_file.filename)
                
                with self.db.get_session() as session:
                    # Verify user owns this referral and lawyer has accepted
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.user_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")
                    
                    inbox_entry = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == referral.id
                    ).first()
                    
                    if not inbox_entry or inbox_entry.status != 'accepted':
                        raise HTTPException(status_code=403, detail="Cannot send messages until lawyer accepts the case")

                    # Block if consultation fee has not been paid
                    self._assert_consultation_paid(session, referral_id)

                    file_url = None
                    file_name = None
                    file_type = None
                    file_size = None
                    file_attachments_blob = None

                    if upload_files:
                        try:
                            existing_names = self._existing_inbox_attachment_names(session, referral.id)
                            existing_names.update(
                                names_from_attachment_metadata_dict(
                                    referral.attachment_metadata or {}
                                )
                            )
                            attachments_meta = await self._upload_inbox_attachments(
                                upload_files,
                                referral_id,
                                existing_name_labels=existing_names,
                            )
                        except HTTPException:
                            raise
                        except Exception as e:
                            logger.error(f"Error uploading file: {e}")
                            raise HTTPException(status_code=500, detail="Error uploading file")
                        if attachments_meta:
                            first = attachments_meta[0]
                            file_url = first["url"]
                            file_name = first["file_name"]
                            file_type = first["file_type"]
                            file_size = first["file_size"]
                            file_attachments_blob = attachments_meta
                            if message_type == "text":
                                message_type = "file"

                    # Create message
                    message = InboxMessage(
                        referral_request_id=uuid.UUID(referral_id),
                        sender_id=uuid.UUID(user_id),
                        sender_type='user',
                        message_content=message_content,
                        message_type=message_type,
                        file_url=file_url,
                        file_name=file_name,
                        file_type=file_type,
                        file_size=file_size,
                        file_attachments=file_attachments_blob,
                        reply_to_message_id=uuid.UUID(reply_to_message_id) if reply_to_message_id else None
                    )

                    session.add(message)
                    latest_session = self._get_latest_consultation_session(session, referral)
                    if latest_session:
                        latest_session.messages_exchanged = (latest_session.messages_exchanged or 0) + 1
                    session.commit()
                    
                    ws_payload = {
                        'type': 'new_message',
                        'message_id': str(message.id),
                        'sender_id': user_id,
                        'sender_type': 'user',
                        'content': message_content,
                        'message_type': message_type,
                        'file_url': file_url,
                        'file_attachments': file_attachments_blob,
                        'timestamp': message.created_at.isoformat()
                    }

                    # Store in Redis for real-time updates
                    await self.redis_service.store_chat_message(
                        conversation_id=f"inbox:{referral_id}",
                        user_id=user_id,
                        message=ws_payload
                    )

                    # Push to any connected WebSocket clients
                    await self._publish_or_broadcast_inbox(
                        ws_payload,
                        referral_id,
                        exclude_user=user_id,
                    )

                    # FCM push notification to the lawyer
                    try:
                        sender = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                        sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Your client"
                        if referral.matched_lawyer_id:
                            lawyer_tokens = self._get_user_device_tokens(session, str(referral.matched_lawyer_id))
                            if lawyer_tokens:
                                preview = message_content or ("📎 File attachment" if file_url else "")
                                latest_session = self._get_latest_consultation_session(session, referral)
                                await self.firebase_service.send_new_message_notification(
                                    device_tokens=lawyer_tokens,
                                    sender_name=sender_name,
                                    message_preview=preview,
                                    referral_id=referral_id,
                                    sender_id=user_id,
                                    sender_type='user',
                                    session_id=str(latest_session.id) if latest_session else None,
                                )
                            # Persist in-app notification for the lawyer (dedup: keep only latest per conversation)
                            preview = message_content or ("📎 File attachment" if file_url else "New message")
                            NotificationService(session).create_or_replace_message(
                                recipient_id=referral.matched_lawyer_id,
                                referral_id=referral_id,
                                title=f"New message from {sender_name}",
                                body=preview[:200],
                                data={"referral_id": referral_id, "sender_id": user_id, "sender_type": "user", "screen": "InboxConversation"},
                            )
                    except Exception as fcm_err:
                        logger.warning(f"FCM push failed for user message: {fcm_err}")

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Message sent successfully",
                            "message_id": str(message.id)
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending user message: {e}")
                raise HTTPException(status_code=500, detail="Error sending message")

        @router.post("/inbox/user/case/{referral_id}/text-message")
        async def send_user_text_message(
            referral_id: str,
            request: Request,
            message_data: SendMessageRequest
        ):
            """Send a simple text message from user to lawyer (JSON endpoint)"""
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                assert_no_contact_details(message_data.message_content)
                
                with self.db.get_session() as session:
                    # Verify user owns this referral and lawyer has accepted
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.user_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")
                    
                    inbox_entry = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == referral.id
                    ).first()
                    
                    if not inbox_entry or inbox_entry.status != 'accepted':
                        raise HTTPException(status_code=403, detail="Cannot send messages until lawyer accepts the case")

                    # Block if consultation fee has not been paid
                    self._assert_consultation_paid(session, referral_id)

                    # Create message
                    message = InboxMessage(
                        referral_request_id=uuid.UUID(referral_id),
                        sender_id=uuid.UUID(user_id),
                        sender_type='user',
                        message_content=message_data.message_content,
                        message_type=message_data.message_type,
                        reply_to_message_id=uuid.UUID(message_data.reply_to_message_id) if message_data.reply_to_message_id else None
                    )

                    session.add(message)
                    latest_session = self._get_latest_consultation_session(session, referral)
                    if latest_session:
                        latest_session.messages_exchanged = (latest_session.messages_exchanged or 0) + 1
                    session.commit()
                    
                    ws_payload = {
                        'type': 'new_message',
                        'message_id': str(message.id),
                        'sender_id': user_id,
                        'sender_type': 'user',
                        'content': message_data.message_content,
                        'message_type': message_data.message_type,
                        'timestamp': message.created_at.isoformat()
                    }

                    # Store in Redis for real-time updates
                    await self.redis_service.store_chat_message(
                        conversation_id=f"inbox:{referral_id}",
                        user_id=user_id,
                        message=ws_payload
                    )

                    # Push to any connected WebSocket clients
                    await self._publish_or_broadcast_inbox(
                        ws_payload,
                        referral_id,
                        exclude_user=user_id,
                    )

                    # FCM push notification to the lawyer
                    try:
                        sender = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                        sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Your client"
                        if referral.matched_lawyer_id:
                            lawyer_tokens = self._get_user_device_tokens(session, str(referral.matched_lawyer_id))
                            if lawyer_tokens:
                                latest_session = self._get_latest_consultation_session(session, referral)
                                await self.firebase_service.send_new_message_notification(
                                    device_tokens=lawyer_tokens,
                                    sender_name=sender_name,
                                    message_preview=message_data.message_content or "",
                                    referral_id=referral_id,
                                    sender_id=user_id,
                                    sender_type='user',
                                    session_id=str(latest_session.id) if latest_session else None,
                                )
                            # Persist in-app notification for the lawyer (dedup: keep only latest per conversation)
                            NotificationService(session).create_or_replace_message(
                                recipient_id=referral.matched_lawyer_id,
                                referral_id=referral_id,
                                title=f"New message from {sender_name}",
                                body=(message_data.message_content or "New message")[:200],
                                data={"referral_id": referral_id, "sender_id": user_id, "sender_type": "user", "screen": "InboxConversation"},
                            )
                    except Exception as fcm_err:
                        logger.warning(f"FCM push failed for user text message: {fcm_err}")

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Message sent successfully",
                            "message_id": str(message.id)
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending user text message: {e}")
                raise HTTPException(status_code=500, detail="Error sending message")

        @router.get("/inbox/lawyer/requests", response_model=List[LawyerInboxItem])
        async def get_lawyer_inbox_requests(request: Request):
            """Get pending referral requests for lawyer"""
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Verify user is a lawyer
                    lawyer = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not lawyer or lawyer.professional_type != 'Lawyer':
                        raise HTTPException(status_code=403, detail="Access denied: Not a lawyer")
                    
                    # Get pending inbox items
                    inbox_query = session.query(LawyerInbox).filter(
                        and_(
                            LawyerInbox.lawyer_id == uuid.UUID(user_id),
                            LawyerInbox.status.in_(['pending', 'read'])
                        )
                    ).order_by(desc(LawyerInbox.priority_score), desc(LawyerInbox.created_at))
                    
                    inbox_items = inbox_query.all()
                    requests = []
                    
                    for item in inbox_items:
                        # Get referral details
                        referral = session.query(ReferralRequest).filter(
                            ReferralRequest.id == item.referral_request_id
                        ).first()
                        
                        if referral:
                            # Get user details
                            user = session.query(User).filter(User.id == referral.user_id).first()
                            user_name = f"{user.first_name} {user.last_name}" if user else "Unknown User"
                            
                            # Build comprehensive user profile for lawyer view
                            user_profile = None
                            if user:
                                # Prepare user name with fallback
                                if user.first_name and user.last_name:
                                    client_name = f"{user.first_name} {user.last_name}"
                                elif user.first_name:
                                    client_name = user.first_name
                                else:
                                    client_name = "Unknown Client"
                                
                                # Build full address if available
                                address_parts = []
                                if user.address:
                                    address_parts.append(user.address)
                                if user.town_city:
                                    address_parts.append(user.town_city)
                                if user.postcode:
                                    address_parts.append(user.postcode)
                                full_address = ", ".join(address_parts) if address_parts else None
                                
                                user_profile = ClientProfile(
                                    client_name=client_name,
                                    date_of_birth=user.date_of_birth.isoformat() if user.date_of_birth else None,
                                    address=full_address,
                                    town_city=user.town_city,
                                    country=user.country,
                                    postcode=user.postcode,
                                    about=user.about,
                                    tagline=user.tagline,
                                    immigration_status=user.immigration_status,
                                    profile_picture_path=user.profile_picture_path,
                                    user_location=referral.user_location,  # From referral request
                                    push_token=self._get_user_push_token(session, str(user.id))
                                )
                            
                            att_list, _ = self._case_details_attachments_from_referral(referral)
                            attachments_count = len(att_list)
                            
                            requests.append(LawyerInboxItem(
                                inbox_id=str(item.id),
                                referral_id=str(referral.id),
                                user_name=user_name,
                                user_profile=user_profile,
                                legal_area=referral.legal_area,
                                consultation_type=get_consultation_duration_display(referral.consultation_category),
                                urgency_level=get_urgency_level_display(referral.urgency_level),
                                issue_summary=referral.issue_summary,
                                budget_range=get_budget_range_display(referral.budget_range),
                                **self._casework_payload(referral),
                                priority_score=item.priority_score,
                                created_at=item.created_at.isoformat(),
                                attachments_count=attachments_count
                            ))
                    
                    return requests
                    
            except Exception as e:
                logger.error(f"Error getting lawyer requests: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving requests")

        @router.get("/inbox/lawyer/case/{referral_id}", response_model=CaseDetails)
        async def get_lawyer_case_details(referral_id: str, request: Request):
            """Get detailed case information for lawyer"""
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Verify user is a lawyer and has access to this case
                    lawyer = session.query(User).filter(
                        User.id == uuid.UUID(user_id),
                        User.professional_type == 'Lawyer'
                    ).first()
                    
                    if not lawyer:
                        raise HTTPException(status_code=403, detail="Access denied. Lawyers only.")
                    
                    # Get referral request that is assigned to this lawyer
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.matched_lawyer_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found or not assigned to you")
                    
                    # Get client info
                    client = session.query(User).filter(
                        User.id == referral.user_id
                    ).first()
                    
                    # Build comprehensive client profile
                    client_profile = None
                    if client:
                        # Prepare client name with fallback
                        if client.first_name and client.last_name:
                            client_name = f"{client.first_name} {client.last_name}"
                        elif client.first_name:
                            client_name = client.first_name
                        else:
                            client_name = "Unknown Client"
                        
                        # Build full address if available
                        address_parts = []
                        if client.address:
                            address_parts.append(client.address)
                        if client.town_city:
                            address_parts.append(client.town_city)
                        if client.postcode:
                            address_parts.append(client.postcode)
                        full_address = ", ".join(address_parts) if address_parts else None
                        
                        client_profile = ClientProfile(
                            client_name=client_name,
                            date_of_birth=client.date_of_birth.isoformat() if client.date_of_birth else None,
                            address=full_address,
                            town_city=client.town_city,
                            country=client.country,
                            postcode=client.postcode,
                            about=client.about,
                            tagline=client.tagline,
                            immigration_status=client.immigration_status,
                            profile_picture_path=client.profile_picture_path,
                            user_location=referral.user_location,  # From referral request
                            push_token=self._get_user_push_token(session, str(client.id))
                        )
                    
                    # Debug logging
                    logger.info(f"Lawyer case details for referral {referral_id}: client_profile='{client_profile.client_name if client_profile else 'None'}', issue_summary='{referral.issue_summary or 'None'}'")
                    
                    
                    # Check if lawyer has accepted the case
                    inbox_entry = session.query(LawyerInbox).filter(
                        and_(
                            LawyerInbox.referral_request_id == referral.id,
                            LawyerInbox.lawyer_id == uuid.UUID(user_id)
                        )
                    ).first()

                    # First-fetch transition: lawyer opening the case for the first time
                    # moves it from 'pending' to 'read' and notifies the client that
                    # their case is now under review. Guarded so it only fires once.
                    if inbox_entry and inbox_entry.status == 'pending':
                        inbox_entry.status = 'read'
                        NotificationService(session).create(
                            recipient_id=referral.user_id,
                            title="Your Case Is Under Review",
                            body=f"{lawyer.first_name} {lawyer.last_name} is reviewing your case.",
                            notification_type="referral_under_review",
                            data={"referral_id": str(referral.id), "screen": "Referrals"},
                        )
                        session.commit()
                        session.refresh(inbox_entry)

                    can_access_messages = inbox_entry and inbox_entry.status == 'accepted'
                    



                    
                    latest_session = self._get_latest_consultation_session(session, referral)
                    state_info = self._describe_consultation_state(session, latest_session)
                    
                    attachments, voice_note = self._case_details_attachments_from_referral(referral)
                    
                    # Refresh individual URL fields for backward compatibility
                    am = referral.attachment_metadata or {}
                    refreshed_voice_note_url = self.s3_service.refresh_presigned_url(
                        url=referral.voice_note_url,
                        s3_key=(am.get("voice_note") or {}).get("s3_key") if isinstance(am.get("voice_note"), dict) else None,
                        expiration=3600
                    ) if referral.voice_note_url else None
                    refreshed_pdf_url = self.s3_service.refresh_presigned_url(
                        url=referral.pdf_attachment_url,
                        s3_key=(am.get("pdf") or {}).get("s3_key") if isinstance(am.get("pdf"), dict) else None,
                        expiration=3600
                    ) if referral.pdf_attachment_url else None
                    refreshed_docx_url = self.s3_service.refresh_presigned_url(
                        url=referral.docx_attachment_url,
                        s3_key=(am.get("docx") or {}).get("s3_key") if isinstance(am.get("docx"), dict) else None,
                        expiration=3600
                    ) if referral.docx_attachment_url else None
                    refreshed_image_url = self.s3_service.refresh_presigned_url(
                        url=referral.image_attachment_url,
                        s3_key=(am.get("image") or {}).get("s3_key") if isinstance(am.get("image"), dict) else None,
                        expiration=3600
                    ) if referral.image_attachment_url else None
                    
                    return CaseDetails(
                        referral_id=str(referral.id),
                        conversation_id=referral.conversation_id,
                        client_profile=client_profile,  # Include comprehensive client profile
                        selected_service=referral.legal_area or "Not specified",
                        consultation_type=get_consultation_duration_display(referral.consultation_category) if referral.consultation_category else "Not specified",
                        consultation_duration=self._consultation_duration_for_response(referral),
                        urgency_level=get_urgency_level_display(referral.urgency_level) if referral.urgency_level else "Not specified",
                        budget_range=get_budget_range_display(referral.budget_range),
                        **self._casework_payload(referral),
                        booking=self._booking_payload(session, referral),
                        user_location=referral.user_location or "Not specified",
                        issue_summary=referral.issue_summary or "No issue summary provided",
                        confirmation_summary=referral.confirmation_summary,  # Add confirmation summary to response
                        status=referral.status,
                        created_at=referral.created_at.isoformat(),
                        can_access_messages=can_access_messages,
                        session_id=str(latest_session.id) if latest_session else None,
                        session_status=latest_session.session_status if latest_session else None,
                        consultation_state=state_info["consultation_state"],
                        can_start_consultation=state_info["can_start_consultation"],
                        timer_started_at=state_info["timer_started_at"],
                        timer_ended_at=state_info["timer_ended_at"],
                        voice_note_url=refreshed_voice_note_url,
                        pdf_attachment_url=refreshed_pdf_url,
                        docx_attachment_url=refreshed_docx_url,
                        image_attachment_url=refreshed_image_url,
                        attachments=attachments,
                        voice_note=voice_note,
                        additional_notes=(referral.additional_info.strip() if referral.additional_info and referral.additional_info.strip() else None)
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting lawyer case details: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving case details")

        @router.get("/inbox/lawyer/case/{referral_id}/messages", response_model=List[InboxMessageResponse])
        async def get_lawyer_case_messages(referral_id: str, request: Request):
            """Get conversation thread for a case from lawyer perspective"""
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Verify user is a lawyer and has access to this case
                    lawyer = session.query(User).filter(
                        User.id == uuid.UUID(user_id),
                        User.professional_type == 'Lawyer'
                    ).first()
                    
                    if not lawyer:
                        raise HTTPException(status_code=403, detail="Access denied. Lawyers only.")
                    
                    # Get referral request that is assigned to this lawyer
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.matched_lawyer_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(
                            status_code=404, 
                            detail="No lawyer has been assigned to this case yet. Please wait for a lawyer to be matched to your request."
                        )
                    inbox_entry = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == referral.id
                    ).first()
                    if inbox_entry.status == 'pending':
                        raise HTTPException(
                            status_code=423, 
                            detail="Your case has been sent to a lawyer, but they haven't responded yet. Messages will be available once the lawyer accepts your case."
                        )
                    elif inbox_entry.status == 'read':
                        raise HTTPException(
                            status_code=423, 
                            detail="The lawyer has reviewed your case but hasn't made a decision yet. Messages will be available once they accept your case."
                        )
                    elif inbox_entry.status == 'rejected':
                        raise HTTPException(
                            status_code=410, 
                            detail="Unfortunately, the lawyer has declined your case."
                        )
                    elif inbox_entry.status != 'accepted':
                        # Fallback for any other unexpected status
                        raise HTTPException(
                            status_code=403, 
                            detail="Messages are not accessible until the lawyer accepts your case."
                        )
                    
                    # Get messages
                    messages_query = session.query(InboxMessage).filter(
                        InboxMessage.referral_request_id == uuid.UUID(referral_id)
                    ).order_by(InboxMessage.created_at)
                    
                    messages = messages_query.all()
                    
                    # Mark lawyer's unread messages as read
                    session.query(InboxMessage).filter(
                        and_(
                            InboxMessage.referral_request_id == uuid.UUID(referral_id),
                            InboxMessage.sender_type == 'user',
                            InboxMessage.is_read == False
                        )
                    ).update({"is_read": True, "read_at": datetime.now(timezone.utc)})
                    session.commit()
                    
                    # Convert to response format
                    message_responses = []
                    for msg in messages:
                        # Get sender name
                        sender = session.query(User).filter(User.id == msg.sender_id).first()
                        sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Unknown"
                        
                        # Refresh file_url for legacy single-file attachments
                        refreshed_file_url = self.s3_service.refresh_presigned_url(
                            url=msg.file_url, expiration=3600
                        ) if msg.file_url else None
                        
                        message_responses.append(InboxMessageResponse(
                            message_id=str(msg.id),
                            sender_id=str(msg.sender_id),
                            sender_name=sender_name,
                            sender_type=msg.sender_type,
                            message_content=msg.message_content,
                            message_type=msg.message_type,
                            file_url=refreshed_file_url,
                            file_name=msg.file_name,
                            file_type=msg.file_type,
                            file_size=msg.file_size,
                            file_attachments=self._inbox_message_file_attachments_payload(msg),
                            is_read=msg.is_read,
                            created_at=msg.created_at.isoformat(),
                            reply_to_message_id=str(msg.reply_to_message_id) if msg.reply_to_message_id else None
                        ))
                    
                    return message_responses
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting lawyer case messages: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving messages")

        @router.get("/inbox/lawyer/cases", response_model=List[LawyerCaseItem])
        async def get_lawyer_active_cases(request: Request):
            """Get active cases for lawyer"""
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    sessions_query = session.query(ReferralSession).filter(
                        and_(
                            ReferralSession.lawyer_id == uuid.UUID(user_id),
                            ReferralSession.session_status.in_(['pending', 'active', 'completed'])
                        )
                    ).order_by(desc(ReferralSession.created_at))
                    
                    sessions = sessions_query.all()
                    cases = []
                    
                    for session_obj in sessions:
                        # Get referral details
                        referral = session.query(ReferralRequest).filter(
                            ReferralRequest.id == session_obj.referral_request_id
                        ).first()
                        
                        if referral:
                            # Get user details
                            user = session.query(User).filter(User.id == referral.user_id).first()
                            user_name = f"{user.first_name} {user.last_name}" if user else "Unknown User"
                            
                            # Build comprehensive user profile for lawyer view
                            user_profile = None
                            if user:
                                # Prepare user name with fallback
                                if user.first_name and user.last_name:
                                    client_name = f"{user.first_name} {user.last_name}"
                                elif user.first_name:
                                    client_name = user.first_name
                                else:
                                    client_name = "Unknown Client"
                                
                                # Build full address if available
                                address_parts = []
                                if user.address:
                                    address_parts.append(user.address)
                                if user.town_city:
                                    address_parts.append(user.town_city)
                                if user.postcode:
                                    address_parts.append(user.postcode)
                                full_address = ", ".join(address_parts) if address_parts else None
                                
                                user_profile = ClientProfile(
                                    client_name=client_name,
                                    date_of_birth=user.date_of_birth.isoformat() if user.date_of_birth else None,
                                    address=full_address,
                                    town_city=user.town_city,
                                    country=user.country,
                                    postcode=user.postcode,
                                    about=user.about,
                                    tagline=user.tagline,
                                    immigration_status=user.immigration_status,
                                    profile_picture_path=user.profile_picture_path,
                                    user_location=referral.user_location,  # From referral request
                                    push_token=self._get_user_push_token(session, str(user.id))
                                )
                            
                            # Count unread messages from user
                            unread_count = session.query(InboxMessage).filter(
                                and_(
                                    InboxMessage.referral_request_id == referral.id,
                                    InboxMessage.sender_type == 'user',
                                    InboxMessage.is_read == False
                                )
                            ).count()
                            
                            # Get last message timestamp
                            last_message = session.query(InboxMessage).filter(
                                InboxMessage.referral_request_id == referral.id
                            ).order_by(desc(InboxMessage.created_at)).first()
                            
                            last_message_at = last_message.created_at.isoformat() if last_message else None
                            
                            cases.append(LawyerCaseItem(
                                referral_id=str(referral.id),
                                session_id=str(session_obj.id),
                                user_name=user_name,
                                user_profile=user_profile,
                                legal_area=referral.legal_area,
                                consultation_type=get_consultation_duration_display(referral.consultation_category),
                                consultation_duration=self._consultation_duration_for_response(referral),
                                session_status=session_obj.session_status,
                                unread_messages=unread_count,
                                last_message_at=last_message_at,
                                started_at=(session_obj.start_time or session_obj.created_at).isoformat(),
                                **self._casework_payload(referral),
                            ))
                    
                    return cases
                    
            except Exception as e:
                logger.error(f"Error getting lawyer cases: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving cases")

        @router.post("/inbox/lawyer/case/{referral_id}/message")
        async def send_lawyer_message(referral_id: str, request: Request):
            """Send a message from lawyer to user. Multiple files: repeat files, attachments, file, or attachment."""
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                form = await request.form()
                raw_content = form.get("message_content")
                message_content = str(raw_content) if raw_content is not None else None
                message_type = str(form.get("message_type") or "text")
                reply_to_message_id = form.get("reply_to_message_id")
                upload_files = collect_upload_files_from_form(form)
                assert_no_contact_details(message_content)
                for upload_file in upload_files:
                    assert_no_contact_details(upload_file.filename)
                
                with self.db.get_session() as session:
                    # Verify lawyer has an active session for this referral
                    session_obj = session.query(ReferralSession).filter(
                        and_(
                            ReferralSession.referral_request_id == uuid.UUID(referral_id),
                            ReferralSession.lawyer_id == uuid.UUID(user_id),
                            ReferralSession.session_status.in_(['pending', 'active'])
                        )
                    ).first()
                    
                    if not session_obj:
                        raise HTTPException(status_code=403, detail="No active session found for this case")

                    # Block if consultation fee has not been paid
                    self._assert_consultation_paid(session, referral_id, lawyer_id_str=user_id)

                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id)
                    ).first()
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")

                    file_url = None
                    file_name = None
                    file_type = None
                    file_size = None
                    file_attachments_blob = None

                    if upload_files:
                        try:
                            existing_names = self._existing_inbox_attachment_names(session, referral.id)
                            existing_names.update(
                                names_from_attachment_metadata_dict(
                                    referral.attachment_metadata or {}
                                )
                            )
                            attachments_meta = await self._upload_inbox_attachments(
                                upload_files,
                                referral_id,
                                existing_name_labels=existing_names,
                            )
                        except HTTPException:
                            raise
                        except Exception as e:
                            logger.error(f"Error uploading file: {e}")
                            raise HTTPException(status_code=500, detail="Error uploading file")
                        if attachments_meta:
                            first = attachments_meta[0]
                            file_url = first["url"]
                            file_name = first["file_name"]
                            file_type = first["file_type"]
                            file_size = first["file_size"]
                            file_attachments_blob = attachments_meta
                            if message_type == "text":
                                message_type = "file"
                    
                    # Create message
                    message = InboxMessage(
                        referral_request_id=uuid.UUID(referral_id),
                        sender_id=uuid.UUID(user_id),
                        sender_type='lawyer',
                        message_content=message_content,
                        message_type=message_type,
                        file_url=file_url,
                        file_name=file_name,
                        file_type=file_type,
                        file_size=file_size,
                        file_attachments=file_attachments_blob,
                        reply_to_message_id=uuid.UUID(reply_to_message_id) if reply_to_message_id else None
                    )
                    
                    session.add(message)
                    
                    # Update session message count
                    session_obj.messages_exchanged += 1
                    
                    session.commit()
                    
                    ws_payload = {
                        'type': 'new_message',
                        'message_id': str(message.id),
                        'sender_id': user_id,
                        'sender_type': 'lawyer',
                        'content': message_content,
                        'message_type': message_type,
                        'file_url': file_url,
                        'file_attachments': file_attachments_blob,
                        'timestamp': message.created_at.isoformat()
                    }

                    # Store in Redis for real-time updates
                    await self.redis_service.store_chat_message(
                        conversation_id=f"inbox:{referral_id}",
                        user_id=user_id,
                        message=ws_payload
                    )

                    # Push to any connected WebSocket clients
                    await self._publish_or_broadcast_inbox(
                        ws_payload,
                        referral_id,
                        exclude_user=user_id,
                    )

                    # FCM push notification to the user (client)
                    try:
                        sender = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                        sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Your lawyer"
                        referral_obj = session.query(ReferralRequest).filter(
                            ReferralRequest.id == uuid.UUID(referral_id)
                        ).first()
                        if referral_obj:
                            client_tokens = self._get_user_device_tokens(session, str(referral_obj.user_id))
                            if client_tokens:
                                preview = message_content or ("📎 File attachment" if file_url else "")
                                latest_session = self._get_latest_consultation_session(session, referral_obj)
                                await self.firebase_service.send_new_message_notification(
                                    device_tokens=client_tokens,
                                    sender_name=sender_name,
                                    message_preview=preview,
                                    referral_id=referral_id,
                                    sender_id=user_id,
                                    sender_type='lawyer',
                                    session_id=str(latest_session.id) if latest_session else None,
                                )
                            # Persist in-app notification for the user (dedup: keep only latest per conversation)
                            preview = message_content or ("📎 File attachment" if file_url else "New message")
                            NotificationService(session).create_or_replace_message(
                                recipient_id=referral_obj.user_id,
                                referral_id=referral_id,
                                title=f"New message from {sender_name}",
                                body=preview[:200],
                                data={"referral_id": referral_id, "sender_id": user_id, "sender_type": "lawyer", "screen": "InboxConversation"},
                            )
                    except Exception as fcm_err:
                        logger.warning(f"FCM push failed for lawyer message: {fcm_err}")

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Message sent successfully",
                            "message_id": str(message.id)
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending lawyer message: {e}")
                raise HTTPException(status_code=500, detail="Error sending message")

        @router.post("/inbox/lawyer/case/{referral_id}/text-message")
        async def send_lawyer_text_message(
            referral_id: str,
            request: Request,
            message_data: SendMessageRequest
        ):
            """Send a simple text message from lawyer to user (JSON endpoint)"""
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                assert_no_contact_details(message_data.message_content)
                
                with self.db.get_session() as session:
                    # Verify lawyer has an active session for this referral
                    session_obj = session.query(ReferralSession).filter(
                        and_(
                            ReferralSession.referral_request_id == uuid.UUID(referral_id),
                            ReferralSession.lawyer_id == uuid.UUID(user_id),
                            ReferralSession.session_status.in_(['pending', 'active'])
                        )
                    ).first()
                    
                    if not session_obj:
                        raise HTTPException(status_code=403, detail="No active session found for this case")

                    # Block if consultation fee has not been paid
                    self._assert_consultation_paid(session, referral_id, lawyer_id_str=user_id)

                    # Create message
                    message = InboxMessage(
                        referral_request_id=uuid.UUID(referral_id),
                        sender_id=uuid.UUID(user_id),
                        sender_type='lawyer',
                        message_content=message_data.message_content,
                        message_type=message_data.message_type,
                        reply_to_message_id=uuid.UUID(message_data.reply_to_message_id) if message_data.reply_to_message_id else None
                    )
                    
                    session.add(message)
                    
                    # Update session message count
                    session_obj.messages_exchanged += 1
                    
                    session.commit()

                    ws_payload = {
                        'type': 'new_message',
                        'message_id': str(message.id),
                        'sender_id': user_id,
                        'sender_type': 'lawyer',
                        'content': message_data.message_content,
                        'message_type': message_data.message_type,
                        'timestamp': message.created_at.isoformat()
                    }
                    
                    # Store in Redis for real-time updates
                    await self.redis_service.store_chat_message(
                        conversation_id=f"inbox:{referral_id}",
                        user_id=user_id,
                        message=ws_payload
                    )

                    # Push to any connected WebSocket clients
                    await self._publish_or_broadcast_inbox(
                        ws_payload,
                        referral_id,
                        exclude_user=user_id,
                    )

                    # FCM push notification to the user (client)
                    try:
                        sender = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                        sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Your lawyer"
                        referral_obj = session.query(ReferralRequest).filter(
                            ReferralRequest.id == uuid.UUID(referral_id)
                        ).first()
                        if referral_obj:
                            client_tokens = self._get_user_device_tokens(session, str(referral_obj.user_id))
                            if client_tokens:
                                await self.firebase_service.send_new_message_notification(
                                    device_tokens=client_tokens,
                                    sender_name=sender_name,
                                    message_preview=message_data.message_content or "",
                                    referral_id=referral_id,
                                    sender_id=user_id,
                                    sender_type='lawyer',
                                    session_id=str(session_obj.id) if session_obj else None,
                                )
                            # Persist in-app notification for the user (dedup: keep only latest per conversation)
                            NotificationService(session).create_or_replace_message(
                                recipient_id=referral_obj.user_id,
                                referral_id=referral_id,
                                title=f"New message from {sender_name}",
                                body=(message_data.message_content or "New message")[:200],
                                data={"referral_id": referral_id, "sender_id": user_id, "sender_type": "lawyer", "screen": "InboxConversation"},
                            )
                    except Exception as fcm_err:
                        logger.warning(f"FCM push failed for lawyer text message: {fcm_err}")
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Message sent successfully",
                            "message_id": str(message.id)
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending lawyer text message: {e}")
                raise HTTPException(status_code=500, detail="Error sending message")

        @router.post("/inbox/user/case/{referral_id}/cancel")
        async def cancel_user_case(
            referral_id: str,
            request: Request,
            cancel_data: CancelCaseRequest
        ):
            """Cancel a user's case request"""
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Get the referral request and verify ownership
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.user_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")
                    
                    # Check if case can be cancelled
                    if referral.status in [ReferralStatus.CANCELLED.value, ReferralStatus.COMPLETED.value]:
                        raise HTTPException(
                            status_code=400, 
                            detail=f"Cannot cancel case with status: {referral.status}"
                        )
                    
                    # Update referral status to cancelled
                    old_status = referral.status
                    referral.status = ReferralStatus.CANCELLED.value
                    referral.updated_at = datetime.now(timezone.utc)
                    
                    # Update lawyer inbox entry if it exists
                    inbox_entry = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == referral.id
                    ).first()
                    
                    if inbox_entry and inbox_entry.status in ('pending', 'read'):
                        inbox_entry.status = 'cancelled'
                        inbox_entry.responded_at = datetime.now(timezone.utc)
                        inbox_entry.response_message = f"User cancelled case. Reason: {cancel_data.cancellation_reason or 'No reason provided'}"
                    
                    # Update any active session
                    active_session = session.query(ReferralSession).filter(
                        and_(
                            ReferralSession.referral_request_id == referral.id,
                            ReferralSession.session_status.in_(['pending', 'active'])
                        )
                    ).first()
                    
                    if active_session:
                        active_session.session_status = 'cancelled'
                        active_session.end_time = datetime.now(timezone.utc)
                        active_session.notes = f"Session cancelled by user. Reason: {cancel_data.cancellation_reason or 'No reason provided'}"
                    
                    session.commit()
                    
                    # Log the cancellation
                    logger.info(f"User {user_id} cancelled case {referral_id}. Previous status: {old_status}")
                    
                    # Send notification to lawyer if they were matched
                    if referral.matched_lawyer_id:
                        # Store Redis notification for backwards compatibility
                        notification_data = {
                            'type': 'case_cancelled_by_user',
                            'referral_id': str(referral.id),
                            'user_name': f"Client", # Keep user privacy
                            'cancellation_reason': cancel_data.cancellation_reason,
                            'timestamp': datetime.now().isoformat()
                        }
                        await self.redis_service.set_key(
                            f"lawyer_notification:{referral.matched_lawyer_id}",
                            json.dumps(notification_data),
                            expire=3600
                        )
                        
                        # Send FCM push notification to the lawyer
                        try:
                            # Get client name for notification
                            client_user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                            client_name = f"{client_user.first_name} {client_user.last_name}" if client_user else "A client"
                            
                            lawyer_devices = session.query(DeviceToken).filter(
                                DeviceToken.user_id == referral.matched_lawyer_id,
                                DeviceToken.is_active == True
                            ).all()
                            
                            if lawyer_devices:
                                device_tokens = [device.device_token for device in lawyer_devices]
                                await self.firebase_service.send_case_cancelled_notification(
                                    device_tokens=device_tokens,
                                    client_name=client_name,
                                    referral_id=str(referral.id),
                                    reason=cancel_data.cancellation_reason,
                                    is_deletion=False
                                )
                                logger.info(f"Sent case cancellation notification to lawyer {referral.matched_lawyer_id}")

                            # Persist in-app notification for the lawyer
                            NotificationService(session).create(
                                recipient_id=referral.matched_lawyer_id,
                                title="Case Request Withdrawn",
                                body=f"{client_name} has withdrawn their case request.",
                                notification_type="case_cancelled",
                                data={"referral_id": str(referral.id), "screen": "LawyerInbox"},
                            )
                        except Exception as notif_err:
                            logger.warning(f"Failed to send cancellation notification: {notif_err}")
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Case cancelled successfully",
                            "referral_id": referral_id,
                            "previous_status": old_status,
                            "new_status": ReferralStatus.CANCELLED.value
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error cancelling user case: {e}")
                raise HTTPException(status_code=500, detail="Error cancelling case")

        # =====================================
        # DELETE INBOX ITEMS ENDPOINTS
        # =====================================

        @router.delete("/inbox/user/case/{referral_id}", response_model=DeleteInboxResponse)
        async def delete_user_inbox_item(referral_id: str, request: Request):
            """
            Delete a cancelled or pending (rejected by lawyer) referral request from user's inbox.
            
            Only allows deletion of cases with status:
            - cancelled: Cases that were cancelled by user or system
            - matched: Cases where the lawyer hasn't responded yet (pending in inbox)
            
            This is a soft delete - the records are removed from the user's inbox view
            but related data is cleaned up.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Get the referral request and verify ownership
                    referral = session.query(ReferralRequest).filter(
                        and_(
                            ReferralRequest.id == uuid.UUID(referral_id),
                            ReferralRequest.user_id == uuid.UUID(user_id)
                        )
                    ).first()
                    
                    if not referral:
                        raise HTTPException(status_code=404, detail="Case not found")
                    
                    # Only allow deletion of cancelled or pending (matched) cases
                    deletable_statuses = [
                        ReferralStatus.CANCELLED.value,
                        ReferralStatus.MATCHED.value,  # Pending - waiting for lawyer response
                        ReferralStatus.EXPIRED.value
                    ]
                    
                    # Also check if the lawyer rejected the case (status in LawyerInbox is 'rejected')
                    inbox_entry = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == referral.id
                    ).first()
                    
                    is_rejected_by_lawyer = inbox_entry and inbox_entry.status == 'rejected'
                    
                    if referral.status not in deletable_statuses and not is_rejected_by_lawyer:
                        raise HTTPException(
                            status_code=400, 
                            detail=f"Cannot delete case with status: {referral.status}. Only cancelled, pending, or rejected cases can be deleted."
                        )
                    
                    # Send notification to lawyer BEFORE deleting records (need matched_lawyer_id)
                    if referral.matched_lawyer_id:
                        try:
                            # Get client name for notification
                            client_user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                            client_name = f"{client_user.first_name} {client_user.last_name}" if client_user else "A client"
                            
                            lawyer_devices = session.query(DeviceToken).filter(
                                DeviceToken.user_id == referral.matched_lawyer_id,
                                DeviceToken.is_active == True
                            ).all()
                            
                            if lawyer_devices:
                                device_tokens = [device.device_token for device in lawyer_devices]
                                await self.firebase_service.send_case_cancelled_notification(
                                    device_tokens=device_tokens,
                                    client_name=client_name,
                                    referral_id=referral_id,
                                    reason=None,
                                    is_deletion=True
                                )
                                logger.info(f"Sent case deletion notification to lawyer {referral.matched_lawyer_id}")

                            # Persist in-app notification for the lawyer
                            NotificationService(session).create(
                                recipient_id=referral.matched_lawyer_id,
                                title="Case Request Removed",
                                body=f"{client_name} has removed their case request from the system.",
                                notification_type="case_deleted",
                                data={"referral_id": referral_id, "screen": "LawyerInbox"},
                            )
                        except Exception as notif_err:
                            logger.warning(f"Failed to send deletion notification: {notif_err}")
                    
                    # Perform cleanup - delete related records
                    deleted_messages = 0
                    deleted_recommendations = 0
                    deleted_inbox_entries = 0
                    
                    # Delete inbox messages
                    deleted_messages = session.query(InboxMessage).filter(
                        InboxMessage.referral_request_id == uuid.UUID(referral_id)
                    ).delete(synchronize_session=False)
                    
                    # Delete lawyer recommendations
                    deleted_recommendations = session.query(LawyerRecommendation).filter(
                        LawyerRecommendation.referral_request_id == uuid.UUID(referral_id)
                    ).delete(synchronize_session=False)
                    
                    # Delete lawyer inbox entries
                    deleted_inbox_entries = session.query(LawyerInbox).filter(
                        LawyerInbox.referral_request_id == uuid.UUID(referral_id)
                    ).delete(synchronize_session=False)
                    
                    # Delete any referral sessions
                    deleted_sessions = session.query(ReferralSession).filter(
                        ReferralSession.referral_request_id == uuid.UUID(referral_id)
                    ).delete(synchronize_session=False)
                    
                    # Finally, delete the referral request itself
                    session.delete(referral)
                    session.commit()
                    
                    logger.info(
                        f"User {user_id} deleted case {referral_id}. "
                        f"Cleaned up: {deleted_messages} messages, {deleted_recommendations} recommendations, "
                        f"{deleted_inbox_entries} inbox entries, {deleted_sessions} sessions"
                    )
                    
                    # Clean up Redis data
                    try:
                        await self.redis_service.delete_key(f"inbox:{referral_id}")
                    except Exception as e:
                        logger.warning(f"Could not clean Redis data for referral {referral_id}: {e}")
                    
                    return DeleteInboxResponse(
                        success=True,
                        message="Case deleted successfully from your inbox",
                        deleted_count=1,
                        deleted_ids=[referral_id]
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error deleting user inbox item: {e}")
                raise HTTPException(status_code=500, detail="Error deleting case from inbox")

        @router.post("/inbox/user/cases/delete", response_model=DeleteInboxResponse)
        async def batch_delete_user_inbox_items(
            request: Request,
            delete_request: DeleteInboxItemsRequest
        ):
            """
            Batch delete cancelled or pending referral requests from user's inbox.
            
            Only deletes cases with status:
            - cancelled: Cases that were cancelled
            - matched: Cases where the lawyer hasn't responded yet (pending)
            - expired: Cases that have expired
            - Cases where the lawyer rejected the request
            
            Cases with other statuses (active, completed) will be skipped and reported in failed_ids.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                if not delete_request.referral_ids:
                    raise HTTPException(status_code=400, detail="No referral IDs provided")
                
                deleted_ids = []
                failed_ids = []
                errors = {}
                
                with self.db.get_session() as session:
                    for referral_id in delete_request.referral_ids:
                        try:
                            # Get the referral request and verify ownership
                            referral = session.query(ReferralRequest).filter(
                                and_(
                                    ReferralRequest.id == uuid.UUID(referral_id),
                                    ReferralRequest.user_id == uuid.UUID(user_id)
                                )
                            ).first()
                            
                            if not referral:
                                failed_ids.append(referral_id)
                                errors[referral_id] = "Case not found or not owned by user"
                                continue
                            
                            # Check deletable status
                            deletable_statuses = [
                                ReferralStatus.CANCELLED.value,
                                ReferralStatus.MATCHED.value,
                                ReferralStatus.EXPIRED.value
                            ]
                            
                            # Check if rejected by lawyer
                            inbox_entry = session.query(LawyerInbox).filter(
                                LawyerInbox.referral_request_id == referral.id
                            ).first()
                            
                            is_rejected_by_lawyer = inbox_entry and inbox_entry.status == 'rejected'
                            
                            if referral.status not in deletable_statuses and not is_rejected_by_lawyer:
                                failed_ids.append(referral_id)
                                errors[referral_id] = f"Cannot delete case with status: {referral.status}"
                                continue
                            
                            # Send notification to lawyer BEFORE deleting records
                            if referral.matched_lawyer_id:
                                try:
                                    # Get client name for notification
                                    client_user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                                    client_name = f"{client_user.first_name} {client_user.last_name}" if client_user else "A client"
                                    
                                    lawyer_devices = session.query(DeviceToken).filter(
                                        DeviceToken.user_id == referral.matched_lawyer_id,
                                        DeviceToken.is_active == True
                                    ).all()
                                    
                                    if lawyer_devices:
                                        device_tokens = [device.device_token for device in lawyer_devices]
                                        await self.firebase_service.send_case_cancelled_notification(
                                            device_tokens=device_tokens,
                                            client_name=client_name,
                                            referral_id=referral_id,
                                            reason=None,
                                            is_deletion=True
                                        )
                                        logger.info(f"Sent case deletion notification to lawyer {referral.matched_lawyer_id}")

                                    # Persist in-app notification for the lawyer
                                    NotificationService(session).create(
                                        recipient_id=referral.matched_lawyer_id,
                                        title="Case Request Removed",
                                        body=f"{client_name} has removed their case request from the system.",
                                        notification_type="case_deleted",
                                        data={"referral_id": referral_id, "screen": "LawyerInbox"},
                                    )
                                except Exception as notif_err:
                                    logger.warning(f"Failed to send deletion notification for {referral_id}: {notif_err}")
                            
                            # Perform cleanup
                            session.query(InboxMessage).filter(
                                InboxMessage.referral_request_id == uuid.UUID(referral_id)
                            ).delete(synchronize_session=False)
                            
                            session.query(LawyerRecommendation).filter(
                                LawyerRecommendation.referral_request_id == uuid.UUID(referral_id)
                            ).delete(synchronize_session=False)
                            
                            session.query(LawyerInbox).filter(
                                LawyerInbox.referral_request_id == uuid.UUID(referral_id)
                            ).delete(synchronize_session=False)
                            
                            session.query(ReferralSession).filter(
                                ReferralSession.referral_request_id == uuid.UUID(referral_id)
                            ).delete(synchronize_session=False)
                            
                            session.delete(referral)
                            deleted_ids.append(referral_id)
                            
                            # Clean up Redis
                            try:
                                await self.redis_service.delete_key(f"inbox:{referral_id}")
                            except Exception:
                                pass  # Non-critical
                            
                        except ValueError as e:
                            failed_ids.append(referral_id)
                            errors[referral_id] = f"Invalid referral ID format: {str(e)}"
                        except Exception as e:
                            # Catch all other exceptions (SQLAlchemy errors, foreign key violations, etc.)
                            failed_ids.append(referral_id)
                            errors[referral_id] = f"Database error: {str(e)}"
                            logger.error(f"Error deleting referral {referral_id}: {e}")
                    
                    # Only commit if there are successful deletions
                    if deleted_ids:
                        session.commit()
                    else:
                        session.rollback()
                
                logger.info(f"User {user_id} batch deleted {len(deleted_ids)} cases. Failed: {len(failed_ids)}")
                
                return DeleteInboxResponse(
                    success=len(deleted_ids) > 0,
                    message=f"Deleted {len(deleted_ids)} case(s) from inbox" + 
                            (f", {len(failed_ids)} failed" if failed_ids else ""),
                    deleted_count=len(deleted_ids),
                    deleted_ids=deleted_ids,
                    failed_ids=failed_ids,
                    errors=errors if errors else None
                )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error batch deleting user inbox items: {e}")
                raise HTTPException(status_code=500, detail="Error deleting cases from inbox")

        @router.delete("/inbox/lawyer/request/{referral_id}", response_model=DeleteInboxResponse)
        async def delete_lawyer_inbox_item(referral_id: str, request: Request):
            """
            Delete a cancelled or rejected referral request from lawyer's inbox.
            
            Only allows deletion of inbox items with status:
            - rejected: Cases the lawyer rejected
            - cancelled: Cases cancelled by the user
            
            Active or pending cases cannot be deleted.
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
                    
                    # Get the inbox entry for this lawyer and referral
                    inbox_entry = session.query(LawyerInbox).filter(
                        and_(
                            LawyerInbox.referral_request_id == uuid.UUID(referral_id),
                            LawyerInbox.lawyer_id == uuid.UUID(lawyer_id)
                        )
                    ).first()
                    
                    if not inbox_entry:
                        raise HTTPException(status_code=404, detail="Request not found in your inbox")
                    
                    # Only allow deletion of rejected or cancelled inbox items
                    deletable_statuses = ['rejected', 'cancelled']
                    
                    # Also check referral status for cancelled cases
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id)
                    ).first()
                    
                    is_referral_cancelled = referral and referral.status == ReferralStatus.CANCELLED.value
                    
                    if inbox_entry.status not in deletable_statuses and not is_referral_cancelled:
                        raise HTTPException(
                            status_code=400, 
                            detail=f"Cannot delete inbox item with status: {inbox_entry.status}. Only rejected or cancelled cases can be deleted."
                        )
                    
                    # Delete only the lawyer inbox entry (not the whole referral - that belongs to the user)
                    session.delete(inbox_entry)
                    session.commit()
                    
                    logger.info(f"Lawyer {lawyer_id} deleted inbox item for referral {referral_id}")
                    
                    return DeleteInboxResponse(
                        success=True,
                        message="Request removed from your inbox",
                        deleted_count=1,
                        deleted_ids=[referral_id]
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error deleting lawyer inbox item: {e}")
                raise HTTPException(status_code=500, detail="Error removing request from inbox")

        @router.post("/inbox/lawyer/requests/delete", response_model=DeleteInboxResponse)
        async def batch_delete_lawyer_inbox_items(
            request: Request,
            delete_request: DeleteInboxItemsRequest
        ):
            """
            Batch delete cancelled or rejected referral requests from lawyer's inbox.
            
            Only deletes inbox items with status:
            - rejected: Cases the lawyer rejected
            - cancelled: Cases cancelled by the user
            
            Active or pending cases will be skipped and reported in failed_ids.
            """
            try:
                # Verify lawyer token
                token_data = self.auth.verify_request_token(request)
                lawyer_id = token_data.get("user_id")
                
                if not lawyer_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                if not delete_request.referral_ids:
                    raise HTTPException(status_code=400, detail="No referral IDs provided")
                
                with self.db.get_session() as session:
                    # Verify user is a lawyer
                    lawyer = session.query(User).filter(
                        User.id == uuid.UUID(lawyer_id),
                        User.professional_type == 'Lawyer'
                    ).first()
                    
                    if not lawyer:
                        raise HTTPException(status_code=403, detail="Access denied. Lawyers only.")
                    
                    deleted_ids = []
                    failed_ids = []
                    errors = {}
                    
                    for referral_id in delete_request.referral_ids:
                        try:
                            # Get the inbox entry
                            inbox_entry = session.query(LawyerInbox).filter(
                                and_(
                                    LawyerInbox.referral_request_id == uuid.UUID(referral_id),
                                    LawyerInbox.lawyer_id == uuid.UUID(lawyer_id)
                                )
                            ).first()
                            
                            if not inbox_entry:
                                failed_ids.append(referral_id)
                                errors[referral_id] = "Request not found in your inbox"
                                continue
                            
                            # Check deletable status
                            deletable_statuses = ['rejected', 'cancelled']
                            
                            # Check referral status
                            referral = session.query(ReferralRequest).filter(
                                ReferralRequest.id == uuid.UUID(referral_id)
                            ).first()
                            
                            is_referral_cancelled = referral and referral.status == ReferralStatus.CANCELLED.value
                            
                            if inbox_entry.status not in deletable_statuses and not is_referral_cancelled:
                                failed_ids.append(referral_id)
                                errors[referral_id] = f"Cannot delete inbox item with status: {inbox_entry.status}"
                                continue
                            
                            # Delete the inbox entry
                            session.delete(inbox_entry)
                            deleted_ids.append(referral_id)
                            
                        except ValueError as e:
                            failed_ids.append(referral_id)
                            errors[referral_id] = f"Invalid referral ID format: {str(e)}"
                        except Exception as e:
                            # Catch all other exceptions (SQLAlchemy errors, foreign key violations, etc.)
                            failed_ids.append(referral_id)
                            errors[referral_id] = f"Database error: {str(e)}"
                            logger.error(f"Error deleting lawyer inbox item for referral {referral_id}: {e}")
                    
                    # Only commit if there are successful deletions
                    if deleted_ids:
                        session.commit()
                    else:
                        session.rollback()
                
                logger.info(f"Lawyer {lawyer_id} batch deleted {len(deleted_ids)} inbox items. Failed: {len(failed_ids)}")
                
                return DeleteInboxResponse(
                    success=len(deleted_ids) > 0,
                    message=f"Removed {len(deleted_ids)} request(s) from inbox" + 
                            (f", {len(failed_ids)} failed" if failed_ids else ""),
                    deleted_count=len(deleted_ids),
                    deleted_ids=deleted_ids,
                    failed_ids=failed_ids,
                    errors=errors if errors else None
                )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error batch deleting lawyer inbox items: {e}")
                raise HTTPException(status_code=500, detail="Error removing requests from inbox")

        return router 
