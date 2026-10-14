"""
Calls API — Phase 1 of LawNeeds in-app voice/video calling.

Implements the request/start/accept/decline/cancel/end lifecycle from
Section 6.3 of the calling scope doc, with server-side entitlement and
participant validation per Section 6.4.

NOT yet implemented in this file (tracked separately):
  - Real Agora token generation (Section 6.1) — stubbed, needs App ID/Certificate.
  - Push notifications (Section 6.5) — stubbed with TODOs, needs confirmed
    NotificationService/FirebaseNotificationService method signatures.
  - Auto-expire-to-`missed` after REQUEST_TIMEOUT_SECONDS (Section 6.3) — needs
    a background job (jobs/ folder), not an HTTP endpoint, so it's not here.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.models import Call, ReferralSession, ReferralRequest, User, DeviceToken
from services.firebase_notification_service import FirebaseNotificationService
from services.agora_service import generate_rtc_token
from services.consultation_payment_service import ConsultationPaymentService 

from config.call_entitlements import get_call_entitlement

logger = logging.getLogger(__name__)

# Section 6.3 — hardcoded constant per Phase 1 resolved open questions.
REQUEST_TIMEOUT_SECONDS = 75

# Statuses that mean "there's already a live call/request on this session"
# — used to block a second call being started (Section 6.4 rule 5).
_ACTIVE_CALL_STATUSES = ("requested", "ringing", "accepted", "connected")


class CallRequestRequest(BaseModel):
    referral_session_id: str
    call_type: str = Field(..., description="'voice' or 'video'")


class CallStartRequest(BaseModel):
    referral_session_id: str
    call_type: str = Field(..., description="'voice' or 'video'")


class CallResponse(BaseModel):
    id: str
    referral_session_id: str
    user_id: str
    lawyer_id: str
    call_type: str
    requested_by: str
    status: str
    agora_channel_name: str
    requested_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    failure_reason: Optional[str] = None


class CallsAPI:
    def __init__(self, *, app_name, db, auth, consultation_api):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.consultation_api = consultation_api

    def get_router(self):
        router = APIRouter()
        
      
        
        @router.get("/calls/entitlement/{referral_session_id}")
        async def get_entitlement(request: Request, referral_session_id: str):
            """
            Voice/video entitlement for a referral session, ahead of any call
            being requested. Used by the Inbox to decide which call icons to
            show (hide, never disable) before the user taps anything.

            Per spec 7.3: a Guided Support package's calendar duration (3/5
            days) and its timed live consultation allocation (e.g. 30 min/day)
            are separate. Once the current day's timed allocation is used up
            (or hasn't started yet), Voice/Video must disappear even though
            the package itself is still active for text messaging.
            """
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(referral_session_id)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    if str(session_obj.user_id) != user_id and str(session_obj.lawyer_id) != user_id:
                        raise ValueError("Not a participant on this consultation")

                    referral_request = session.query(ReferralRequest).filter(
                        ReferralRequest.id == session_obj.referral_request_id
                    ).first()
                    if not referral_request:
                        raise ValueError("Underlying referral request not found for this consultation")

                    package_entitlement = get_call_entitlement(referral_request.consultation_type)

                    if not (package_entitlement.get("voice") or package_entitlement.get("video")):
                        return package_entitlement

                    if not self._live_allocation_available(session_obj):
                        return {"voice": False, "video": False}

                    return package_entitlement

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error fetching call entitlement: {exc}")
                raise HTTPException(status_code=500, detail="Failed to fetch call entitlement")
        
        @router.post("/calls/request", response_model=CallResponse)
        async def request_call(request: Request, data: CallRequestRequest):
            """User requests a voice/video call. Never rings the lawyer directly."""
            try:
                user_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(data.referral_session_id)
                call_type = self._require_valid_call_type(data.call_type)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_user(session_obj, user_id)
                    self._assert_session_active(session_obj)
                    self._assert_entitled(session, session_obj, call_type)
                    self._assert_no_active_call(session, session_uuid)
                    self._ensure_day_started(session_obj)

                    call = Call(
                        id=uuid.uuid4(),
                        referral_session_id=session_uuid,
                        user_id=session_obj.user_id,
                        lawyer_id=session_obj.lawyer_id,
                        call_type=call_type,
                        requested_by="user",
                        status="requested",
                        agora_channel_name=str(uuid.uuid4()),
                        requested_at=datetime.now(timezone.utc),
                    )
                    session.add(call)
                    session.flush()
                    
                    if self.consultation_api._timer_is_paused(session_obj):
                        raise ValueError("Consultation timer is already paused")

                    now = datetime.now(timezone.utc)
                    remaining = self.consultation_api._remaining_seconds_for_session(session, session_obj)
                    session_obj.timer_paused_at = now
                    session_obj.paused_remaining_seconds = max(remaining or 0, 0)
                    session_obj.timer_paused_reason = "pending_request"
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                    payload = {
                        "type": "call_requested",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(session_uuid), payload)
                    

                    lawyer_tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == session_obj.lawyer_id,
                        DeviceToken.is_active == True,
                    ).all()
                    token_list = [t.device_token for t in lawyer_tokens]

                    if token_list:
                        user = session.query(User).filter(User.id == session_obj.user_id).first()
                        user_name = f"{user.first_name} {user.last_name}" if user else "A user"
                        firebase = FirebaseNotificationService()
                        await firebase.send_call_requested_notification(
                            device_tokens=token_list,
                            user_name=user_name,
                            call_type=call_type,
                            call_id=str(call.id),
                            session_id=str(session_obj.id),
                            referral_id=str(session_obj.referral_request_id),
                        )

                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error requesting call: {exc}")
                raise HTTPException(status_code=500, detail="Failed to request call")
       
       
       
        
        @router.post("/calls/start", response_model=CallResponse)
        async def start_call(request: Request, data: CallStartRequest):
            """Lawyer starts a call directly — no request/accept step on their side."""
            try:
                lawyer_id = self._get_request_user_id(request)
                session_uuid = uuid.UUID(data.referral_session_id)
                call_type = self._require_valid_call_type(data.call_type)

                with self.db.get_session() as session:
                    session_obj = self._require_session(session, session_uuid)
                    self._assert_session_lawyer(session_obj, lawyer_id)
                    self._assert_session_active(session_obj)
                    self._assert_entitled(session, session_obj, call_type)
                    self._assert_no_active_call(session, session_uuid)
                    self._ensure_day_started(session_obj)

                    call = Call(
                        id=uuid.uuid4(),
                        referral_session_id=session_uuid,
                        user_id=session_obj.user_id,
                        lawyer_id=session_obj.lawyer_id,
                        call_type=call_type,
                        requested_by="lawyer",
                        status="ringing",
                        agora_channel_name=str(uuid.uuid4()),
                    )
                    session.add(call)
                    session.flush()
                    
                    if self.consultation_api._timer_is_paused(session_obj):
                        raise ValueError("Consultation timer is already paused")

                    now = datetime.now(timezone.utc)
                    remaining = self.consultation_api._remaining_seconds_for_session(session, session_obj)
                    session_obj.timer_paused_at = now
                    session_obj.paused_remaining_seconds = max(remaining or 0, 0)
                    session_obj.timer_paused_reason = "pending_request"
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(session_uuid)
                    payload = {
                        "type": "call_requested",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(session_uuid), payload)
                    

                    user_tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == session_obj.user_id,
                        DeviceToken.is_active == True,
                    ).all()
                    token_list = [t.device_token for t in user_tokens]

                    if token_list:
                        lawyer = session.query(User).filter(User.id == session_obj.lawyer_id).first()
                        lawyer_name = f"{lawyer.first_name} {lawyer.last_name}" if lawyer else "Your lawyer"
                        firebase = FirebaseNotificationService()
                        await firebase.send_incoming_call_notification(
                            device_tokens=token_list,
                            caller_name=lawyer_name,
                            call_type=call_type,
                            call_id=str(call.id),
                            session_id=str(session_obj.id),
                            referral_id=str(session_obj.referral_request_id),
                        )

                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error starting call: {exc}")
                raise HTTPException(status_code=500, detail="Failed to start call")




        @router.post("/calls/{call_id}/accept", response_model=CallResponse)
        async def accept_call(request: Request, call_id: str):
            """Receiving party accepts. Issues Agora tokens (stubbed for now)."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    call = self._require_call(session, call_uuid)
                    session_obj = self._require_session(session, call.referral_session_id)
                    self._assert_call_participant(call, user_id)
                    self._assert_is_receiver(call, user_id)

                    if call.status not in ("requested", "ringing"):
                        raise ValueError(f"Call cannot be accepted from status '{call.status}'")

                    call.status = "accepted"
                    
                    old_call = session.query(Call).filter(
                        Call.referral_session_id == call.referral_session_id,
                        Call.id != call.id,
                        Call.status.in_(("accepted", "connected")),
                    ).first()
                    if old_call:
                        old_call.status = "ended"
                        old_call.ended_at = datetime.now(timezone.utc)
                    
                    
                    call.accepted_at = datetime.now(timezone.utc)
                    session.flush()
                    
                    
                    
                    self.consultation_api._resume_paused_timer(session_obj)
                    session_obj.current_communication_mode = call.call_type
                    session_obj.timer_paused_reason = None
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(call.referral_session_id)
                    payload = {
                        "type": "call_accepted",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(call.referral_session_id), payload)
                    

                    agora_token = generate_rtc_token(call.agora_channel_name)

                    # Notify the caller that the call was accepted.
                    requester_id = call.user_id if call.requested_by == "user" else call.lawyer_id
                    requester_tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == requester_id,
                        DeviceToken.is_active == True,
                    ).all()
                    token_list = [t.device_token for t in requester_tokens]

                    if token_list:
                        firebase = FirebaseNotificationService()
                        await firebase.send_call_accepted_notification(
                            device_tokens=token_list,
                            call_type=call.call_type,
                            call_id=str(call.id),
                            session_id=str(call.referral_session_id),
                        )

                    return self._to_response(call, agora_token=agora_token)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error accepting call {call_id}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to accept call")

        
        
        
        @router.post("/calls/{call_id}/decline", response_model=CallResponse)
        async def decline_call(request: Request, call_id: str):
            """Receiving party declines."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    call = self._require_call(session, call_uuid)
                    self._assert_call_participant(call, user_id)
                    self._assert_is_receiver(call, user_id)

                    if call.status not in ("requested", "ringing"):
                        raise ValueError(f"Call cannot be declined from status '{call.status}'")

                    call.status = "declined"
                    session.flush()
                    
                    
                    session_obj = self._require_session(session, call.referral_session_id)
                    self.consultation_api._resume_paused_timer(session_obj)
                    prior_call = session.query(Call).filter(
                        Call.referral_session_id == call.referral_session_id,
                        Call.id != call.id,
                        Call.status.in_(("accepted", "connected")),
                    ).first()
                    session_obj.current_communication_mode = prior_call.call_type if prior_call else "chat"
                    session_obj.timer_paused_reason = None
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(call.referral_session_id)
                    payload = {
                        "type": "call_declined",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(call.referral_session_id), payload)
                    

                    # Notify the requester that the call was declined.
                    requester_id = call.user_id if call.requested_by == "user" else call.lawyer_id
                    requester_tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == requester_id,
                        DeviceToken.is_active == True,
                    ).all()
                    token_list = [t.device_token for t in requester_tokens]

                    if token_list:
                        firebase = FirebaseNotificationService()
                        await firebase.send_call_declined_notification(
                            device_tokens=token_list,
                            call_type=call.call_type,
                            call_id=str(call.id),
                            session_id=str(call.referral_session_id),
                        )

                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error declining call {call_id}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to decline call")
        
        
        
        
        
        @router.post("/calls/{call_id}/cancel", response_model=CallResponse)
        async def cancel_call(request: Request, call_id: str):
            """Requester cancels before the other party responds."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    call = self._require_call(session, call_uuid)
                    session_obj = self._require_session(session, call.referral_session_id)
                    self._assert_call_participant(call, user_id)
                    self._assert_is_requester(call, user_id)

                    if call.status not in ("requested", "ringing"):
                        raise ValueError(f"Call cannot be cancelled from status '{call.status}'")

                    call.status = "cancelled"
                    session.flush()

                    session_obj = self._require_session(session, call.referral_session_id)
                    self.consultation_api._resume_paused_timer(session_obj)

                    prior_call = session.query(Call).filter(
                        Call.referral_session_id == call.referral_session_id,
                        Call.id != call.id,
                        Call.status.in_(("accepted", "connected")),
                    ).first()
                    session_obj.current_communication_mode = prior_call.call_type if prior_call else "chat"
                    session_obj.timer_paused_reason = None
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(call.referral_session_id)
                    payload = {
                        "type": "call_cancelled",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(call.referral_session_id), payload)
                    
                    

                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error cancelling call {call_id}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to cancel call")
            
                 
            
        @router.post("/calls/{call_id}/switch", response_model=CallResponse)
        async def switch_call(request: Request, call_id: str):
            """Request to switch an active Voice call to Video, or vice versa."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    old_call = self._require_call(session, call_uuid)
                    self._assert_call_participant(old_call, user_id)

                    if old_call.status not in ("accepted", "connected"):
                        raise ValueError(f"Cannot switch from call status '{old_call.status}'")

                    session_obj = self._require_session(session, old_call.referral_session_id)
                    target_call_type = "video" if old_call.call_type == "voice" else "voice"
                    self._assert_entitled(session, session_obj, target_call_type)

                    other_active = session.query(Call).filter(
                        Call.referral_session_id == old_call.referral_session_id,
                        Call.id != old_call.id,
                        Call.status.in_(_ACTIVE_CALL_STATUSES),
                    ).first()
                    if other_active:
                        raise ValueError("A switch or call is already pending for this consultation")

                    is_user = str(old_call.user_id) == user_id
                    new_call = Call(
                        id=uuid.uuid4(),
                        referral_session_id=old_call.referral_session_id,
                        user_id=old_call.user_id,
                        lawyer_id=old_call.lawyer_id,
                        call_type=target_call_type,
                        requested_by="user" if is_user else "lawyer",
                        status="requested" if is_user else "ringing",
                        agora_channel_name=str(uuid.uuid4()),
                        requested_at=datetime.now(timezone.utc),
                    )
                    session.add(new_call)
                    session.flush()

                    if self.consultation_api._timer_is_paused(session_obj):
                        raise ValueError("Consultation timer is already paused")

                    now = datetime.now(timezone.utc)
                    remaining = self.consultation_api._remaining_seconds_for_session(session, session_obj)
                    session_obj.timer_paused_at = now
                    session_obj.paused_remaining_seconds = max(remaining or 0, 0)
                    session_obj.timer_paused_reason = "pending_request"
                    session.flush()

                    other_party_id = old_call.lawyer_id if is_user else old_call.user_id
                    other_tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == other_party_id,
                        DeviceToken.is_active == True,
                    ).all()
                    token_list = [t.device_token for t in other_tokens]

                    if token_list:
                        requester_id = old_call.user_id if is_user else old_call.lawyer_id
                        requester = session.query(User).filter(User.id == requester_id).first()
                        requester_name = (
                            f"{requester.first_name} {requester.last_name}" if requester
                            else ("A user" if is_user else "Your lawyer")
                        )
                        firebase = FirebaseNotificationService()
                        await firebase.send_call_requested_notification(
                            device_tokens=token_list,
                            user_name=requester_name,
                            call_type=target_call_type,
                            call_id=str(new_call.id),
                            session_id=str(session_obj.id),
                            referral_id=str(session_obj.referral_request_id),
                        )

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(old_call.referral_session_id)
                    payload = {
                        "type": "call_switch_requested",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(old_call.referral_session_id), payload)

                    return self._to_response(new_call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error switching call {call_id}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to switch call")
               



        @router.post("/calls/{call_id}/disconnect", response_model=CallResponse)
        async def disconnect_call(request: Request, call_id: str):
            """Client reports a technical disconnect (Agora onConnectionStateChanged/
            onUserOffline). Pauses the timer with a grace period rather than ending
            the call outright — see expire_stale_disconnects for the grace sweep."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    call = self._require_call(session, call_uuid)
                    self._assert_call_participant(call, user_id)

                    if call.status not in ("accepted", "connected"):
                        raise ValueError(f"Call cannot be marked disconnected from status '{call.status}'")

                    if call.disconnected_at is not None:
                        # Already flagged — idempotent, don't pause twice.
                        return self._to_response(call)

                    session_obj = self._require_session(session, call.referral_session_id)

                    if self.consultation_api._timer_is_paused(session_obj):
                        raise ValueError("Consultation timer is already paused")

                    now = datetime.now(timezone.utc)
                    remaining = self.consultation_api._remaining_seconds_for_session(session, session_obj)
                    session_obj.timer_paused_at = now
                    session_obj.paused_remaining_seconds = max(remaining or 0, 0)
                    session_obj.timer_paused_reason = "reconnecting"
                    call.disconnected_at = now
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(call.referral_session_id)
                    payload = {
                        "type": "call_disconnected",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(call.referral_session_id), payload)

                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error marking call {call_id} disconnected: {exc}")
                raise HTTPException(status_code=500, detail="Failed to mark call disconnected")


        @router.post("/calls/{call_id}/reconnect", response_model=CallResponse)
        async def reconnect_call(request: Request, call_id: str):
            """Client reports a successful reconnect within the grace period."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    call = self._require_call(session, call_uuid)
                    self._assert_call_participant(call, user_id)

                    if call.status not in ("accepted", "connected"):
                        raise ValueError(f"Call cannot be reconnected from status '{call.status}'")

                    if call.disconnected_at is None:
                        # Nothing to resume — idempotent no-op.
                        return self._to_response(call)

                    session_obj = self._require_session(session, call.referral_session_id)

                    self.consultation_api._resume_paused_timer(session_obj)
                    session_obj.timer_paused_reason = None
                    call.disconnected_at = None
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(call.referral_session_id)
                    payload = {
                        "type": "call_reconnected",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(call.referral_session_id), payload)

                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error reconnecting call {call_id}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to reconnect call")





        @router.post("/calls/{call_id}/end", response_model=CallResponse)
        async def end_call(request: Request, call_id: str):
            """Either party ends an active call."""
            try:
                user_id = self._get_request_user_id(request)
                call_uuid = uuid.UUID(call_id)

                with self.db.get_session() as session:
                    call = self._require_call(session, call_uuid)
                    self._assert_call_participant(call, user_id)

                    if call.status not in ("accepted", "connected"):
                        raise ValueError(f"Call cannot be ended from status '{call.status}'")

                    call.status = "ended"
                    call.ended_at = datetime.now(timezone.utc)
                    session.flush()
                    
                    
                    session_obj = self._require_session(session, call.referral_session_id)
                    session_obj.current_communication_mode = "chat"
                    session.flush()

                    payment_summary = ConsultationPaymentService(session).get_payment_summary(call.referral_session_id)
                    payload = {
                        "type": "call_ended",
                        **self.consultation_api._build_timer_state(session_obj, payment_summary, session),
                    }
                    await self.consultation_api.timer_manager.broadcast(str(call.referral_session_id), payload)
                    
                    return self._to_response(call)

            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.error(f"Error ending call {call_id}: {exc}")
                raise HTTPException(status_code=500, detail="Failed to end call")

        return router

    
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_request_user_id(self, request: Request) -> str:
        token_data = self.auth.verify_request_token(request)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id

    @staticmethod
    def _require_valid_call_type(call_type: str) -> str:
        if call_type not in ("voice", "video"):
            raise ValueError("call_type must be 'voice' or 'video'")
        return call_type

    @staticmethod
    def _require_session(session, session_id: uuid.UUID) -> ReferralSession:
        session_obj = session.query(ReferralSession).filter(
            ReferralSession.id == session_id
        ).first()
        if not session_obj:
            raise HTTPException(status_code=404, detail="Consultation session not found")
        return session_obj

    @staticmethod
    def _require_call(session, call_id: uuid.UUID) -> Call:
        call = session.query(Call).filter(Call.id == call_id).first()
        if not call:
            raise HTTPException(status_code=404, detail="Call not found")
        return call

    @staticmethod
    def _assert_session_user(session_obj: ReferralSession, user_id: str) -> None:
        if str(session_obj.user_id) != user_id:
            raise ValueError("Only the client can request a call on this consultation")

    @staticmethod
    def _assert_session_lawyer(session_obj: ReferralSession, user_id: str) -> None:
        if str(session_obj.lawyer_id) != user_id:
            raise ValueError("Only the assigned lawyer can start a call on this consultation")

    @staticmethod
    def _assert_session_active(session_obj: ReferralSession) -> None:
        # Matches consultation.py's actual convention: every gate there blocks
        # only on 'completed', never requires 'active' specifically. A call
        # can happen while a session is 'pending' (before the timer starts —
        # the call itself may be how the session gets underway) or 'active'.
        # Also block the other terminal states from the model's status comment.
        if session_obj.session_status in ("completed", "cancelled", "missed"):
            raise ValueError(f"Consultation is not active (status: {session_obj.session_status})")


    def _assert_entitled(self, session, session_obj: ReferralSession, call_type: str) -> None:
        referral_request = session.query(ReferralRequest).filter(
            ReferralRequest.id == session_obj.referral_request_id
        ).first()
        if not referral_request:
            raise ValueError("Underlying referral request not found for this consultation")

        entitlement = get_call_entitlement(referral_request.consultation_type)
        if not entitlement.get(call_type, False):
            raise ValueError(
                f"This consultation package is not entitled to {call_type} calls"
            )

        if not self._live_allocation_available(session_obj):
            raise ValueError(
                "Today's live consultation allocation has already been used up"
            )

    @staticmethod
    def _ensure_day_started(session_obj: ReferralSession) -> None:
        """Mirrors consultation.py's /timer/start day-init logic — a call
        requested before today's slot has begun should start it, same as
        opening chat does. No-op for single-session consultations."""
        if session_obj.days_total is None:
            return
        if session_obj.current_day_timer_started_at and not session_obj.current_day_timer_ended_at:
            return  # today's slot already in progress

        now = datetime.now(timezone.utc)
        if not session_obj.timer_started_at:
            session_obj.timer_started_at = now
            session_obj.start_time = now
            session_obj.session_status = 'active'

        session_obj.current_day_timer_started_at = now
        session_obj.current_day_timer_ended_at = None
        session_obj.end_time = now + timedelta(seconds=30 * 60)
    
    
    
    @staticmethod
    def _assert_no_active_call(session, session_id: uuid.UUID) -> None:
        existing = session.query(Call).filter(
            Call.referral_session_id == session_id,
            Call.status.in_(_ACTIVE_CALL_STATUSES),
        ).first()
        if existing:
            raise ValueError("A call is already active or pending for this consultation")

    @staticmethod
    def _assert_call_participant(call: Call, user_id: str) -> None:
        if str(call.user_id) != user_id and str(call.lawyer_id) != user_id:
            raise ValueError("You do not have access to this call")

    @staticmethod
    def _assert_is_receiver(call: Call, user_id: str) -> None:
        # The receiver is whichever participant did NOT initiate the call.
        initiator_id = call.user_id if call.requested_by == "user" else call.lawyer_id
        if str(initiator_id) == user_id:
            raise ValueError("The requesting party cannot accept/decline their own call")

    @staticmethod
    def _assert_is_requester(call: Call, user_id: str) -> None:
        initiator_id = call.user_id if call.requested_by == "user" else call.lawyer_id
        if str(initiator_id) != user_id:
            raise ValueError("Only the requesting party can cancel this call")
    @staticmethod
    def _to_response(call: Call, agora_token: Optional[str] = None) -> CallResponse:
        return CallResponse(
            id=str(call.id),
            referral_session_id=str(call.referral_session_id),
            user_id=str(call.user_id),
            lawyer_id=str(call.lawyer_id),
            call_type=call.call_type,
            requested_by=call.requested_by,
            status=call.status,
            agora_channel_name=call.agora_channel_name,
            requested_at=call.requested_at,
            accepted_at=call.accepted_at,
            started_at=call.started_at,
            ended_at=call.ended_at,
            failure_reason=call.failure_reason,
            agora_token=agora_token,
        )
        
      
    @staticmethod
    def _live_allocation_available(session_obj: ReferralSession) -> bool:
        """Whether the timed live-consultation allocation is currently open
        for Voice/Video — separate from whether the package itself is still
        active for messaging. See spec 7.3."""
        if session_obj.session_status == "completed":
            return False

        if session_obj.days_total is not None:
            # Multi-day ongoing: only blocked once today's slot is used up.
            # Not-yet-started is fine — a call is allowed to start the day,
            # same as chat does via /timer/start.
            if session_obj.current_day_timer_ended_at:
                return False
            return True

        # Single-session.
        if session_obj.timer_ended_at:
            return False
        if not session_obj.timer_started_at:
            return False
        return True