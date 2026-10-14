"""Scheduled sweep for stale (unanswered) call requests/rings.

Finds Call rows still in 'requested' or 'ringing' status whose created_at
is more than REQUEST_TIMEOUT_SECONDS in the past (Section 6.3 of the
calling scope doc — hardcoded constant, not admin-configurable in Phase 1).
For each match: marks the call 'missed' and notifies whichever party
was supposed to answer (the receiver, not the requester).

Phase 2: also resumes the consultation timer (paused when the call/switch
was requested) and reverts current_communication_mode to whatever mode
was active before the request — or "chat" if there was none — then
broadcasts the updated timer state over the consultation's websocket.

Mirrors the structure of jobs/missed_consultation_jobs.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.consultation_payment_service import ConsultationPaymentService
from services.firebase_notification_service import FirebaseNotificationService
from services.models import Call, DeviceToken, ReferralSession

logger = logging.getLogger(__name__)

# Section 6.3 — hardcoded constant per Phase 1 resolved open questions.
REQUEST_TIMEOUT_SECONDS = 75


async def expire_stale_calls(db, consultation_api) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=REQUEST_TIMEOUT_SECONDS)

    with db.get_session() as lookup_session:
        due_calls = (
            lookup_session.query(Call)
            .filter(
                Call.status.in_(("requested", "ringing")),
                Call.created_at <= cutoff,
            )
            .all()
        )
        due_ids = [call.id for call in due_calls]

    if not due_ids:
        return

    logger.info(
        "Found %d call(s) past the %ds request timeout.",
        len(due_ids), REQUEST_TIMEOUT_SECONDS,
    )

    for call_id in due_ids:
        with db.get_session() as session:
            call = (
                session.query(Call)
                .filter(Call.id == call_id)
                .with_for_update()
                .first()
            )
            if not call or call.status not in ("requested", "ringing"):
                # Already answered/cancelled/handled since the lookup query ran.
                continue

            call.status = "missed"
            session.flush()

            # --- Phase 2: resume the consultation timer that was paused
            # when this call (or switch request) was made, and revert to
            # whatever mode was active before it, if any. ---
            session_obj = session.query(ReferralSession).filter(
                ReferralSession.id == call.referral_session_id
            ).first()
            if session_obj:
                prior_call = session.query(Call).filter(
                    Call.referral_session_id == call.referral_session_id,
                    Call.id != call.id,
                    Call.status.in_(("accepted", "connected")),
                ).first()

                consultation_api._resume_paused_timer(session_obj)
                session_obj.current_communication_mode = prior_call.call_type if prior_call else "chat"
                session_obj.timer_paused_reason = None
                session.flush()

                payment_summary = ConsultationPaymentService(session).get_payment_summary(
                    call.referral_session_id
                )
                payload = {
                    "type": "call_missed",
                    **consultation_api._build_timer_state(session_obj, payment_summary, session),
                }
                await consultation_api.timer_manager.broadcast(
                    str(call.referral_session_id), payload
                )

            # The receiver is whichever participant did NOT initiate the call.
            receiver_id = call.lawyer_id if call.requested_by == "user" else call.user_id

            receiver_tokens = session.query(DeviceToken).filter(
                DeviceToken.user_id == receiver_id,
                DeviceToken.is_active == True,
            ).all()
            token_list = [t.device_token for t in receiver_tokens]

            if token_list:
                try:
                    firebase = FirebaseNotificationService()
                    await firebase.send_missed_call_notification(
                        device_tokens=token_list,
                        call_type=call.call_type,
                        call_id=str(call.id),
                        session_id=str(call.referral_session_id),
                    )
                except Exception as exc:
                    logger.error("Failed to send missed-call notification for call %s: %s", call_id, exc)

            logger.info("Marked call %s as missed (timed out).", call_id)