"""Scheduled sweep for missed (booked, never-started) consultations.

Finds ConsultationBookingRequest rows (single_consultation / ongoing_support
only — casework has no booked slot, see booking_service.requires_booking_slot)
whose requested_start_at is more than GRACE_PERIOD_MINUTES in the past,
joined to a ReferralSession that never started (session_status still
'pending', timer_started_at still null). For each match: marks the
session 'missed', stamps missed_notification_sent_at for dedup, and
notifies the lawyer (in-app + push). No fault attribution — there is no
reliable signal for whether the lawyer or the client failed to show, so
the session is just marked missed generically.

Mirrors the structure of jobs/referral_expiry_jobs.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.booking_service import requires_booking_slot
from services.firebase_notification_service import FirebaseNotificationService
from services.models import ConsultationBookingRequest, DeviceToken, ReferralSession
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)

GRACE_PERIOD_MINUTES = 30


async def sweep_missed_consultations(db) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=GRACE_PERIOD_MINUTES)

    with db.get_session() as lookup_session:
        rows = (
            lookup_session.query(ConsultationBookingRequest, ReferralSession)
            .join(
                ReferralSession,
                ReferralSession.referral_request_id == ConsultationBookingRequest.referral_request_id,
            )
            .filter(
                ConsultationBookingRequest.requested_start_at <= cutoff,
                ReferralSession.session_status == "pending",
                ReferralSession.timer_started_at.is_(None),
                ReferralSession.missed_notification_sent_at.is_(None),
            )
            .all()
        )

        due = [
            (booking.id, session_obj.id, session_obj.referral_request_id, session_obj.lawyer_id)
            for booking, session_obj in rows
            if requires_booking_slot(booking.consultation_category)
        ]

    if not due:
        return

    logger.info(
        "Found %d booked consultations past the %dm missed-consultation grace period.",
        len(due), GRACE_PERIOD_MINUTES,
    )

    for booking_id, session_id, referral_request_id, lawyer_id in due:
        with db.get_session() as session:
            session_obj = (
                session.query(ReferralSession)
                .filter(ReferralSession.id == session_id)
                .with_for_update()
                .first()
            )
            if not session_obj or session_obj.session_status != "pending" or session_obj.missed_notification_sent_at:
                continue

            session_obj.session_status = "missed"
            session_obj.missed_notification_sent_at = datetime.now(timezone.utc)

            referral_id_str = str(referral_request_id)
            session_id_str = str(session_id)

            data = {
                "type": "missed_consultation",
                "referral_id": referral_id_str,
                "session_id": session_id_str,
                "screen": "ConsultationScreen",
                "action": "view_status",
            }
            notification_service = NotificationService(session)
            notification_service.create(
                recipient_id=lawyer_id,
                title="Missed Consultation",
                body="A scheduled consultation was missed and never started.",
                notification_type="missed_consultation",
                data=data,
            )
            session.commit()

            try:
                firebase = FirebaseNotificationService()
                tokens = session.query(DeviceToken).filter(
                    DeviceToken.user_id == lawyer_id,
                    DeviceToken.is_active == True,
                ).all()
                if tokens:
                    await firebase.send_missed_consultation_notification(
                        device_tokens=[token.device_token for token in tokens],
                        session_id=session_id_str,
                        referral_id=referral_id_str,
                    )
            except Exception as exc:
                logger.warning("Failed to deliver missed consultation push: %s", exc)