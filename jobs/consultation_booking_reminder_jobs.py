"""
Consultation Booking Reminder Jobs

Sends "Complete Your Pending Action" reminder emails for casework
consultation bookings that were started but never completed:

- 24h after the booking request was created, if still pending
- A follow-up at 72h, if still pending and the 24h reminder was already sent
- Never sent (at either stage) once the booking leaves 'pending' status
  (accepted, rejected, cancelled, expired, completed all stop reminders)

Runs hourly. Each stage is tracked via a nullable timestamp column
(reminder_24h_sent_at / reminder_72h_sent_at) on
ConsultationBookingRequest, following the same pattern as the existing
accepted_at / cancelled_at columns on that table.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_

logger = logging.getLogger(__name__)


async def send_consultation_booking_reminders(db):
    """
    Send pending-action reminder emails for abandoned casework consultation
    bookings.

    Runs hourly. Handles both the 24h reminder and the 72h follow-up in a
    single pass.
    """
    logger.info("Starting job: send_consultation_booking_reminders")

    try:
        from sqlalchemy.orm import aliased
        from services.models import ConsultationBookingRequest, User, DeviceToken
        from services.email import EmailService
        from services.firebase_notification_service import FirebaseNotificationService

        firebase_service = FirebaseNotificationService()
        Professional = aliased(User)

        def _professional_display_name(professional):
            full_name = f"{professional.first_name or ''} {professional.last_name or ''}".strip()
            return full_name or professional.firm_name or "your legal professional"

        with db.get_session() as session:
            now = datetime.now(timezone.utc)

            # ============================================
            # 24h reminder
            # ============================================
            reminder_24h_cutoff = now - timedelta(hours=24)

            pending_24h = session.query(
                ConsultationBookingRequest, User, Professional
            ).join(
                User, ConsultationBookingRequest.user_id == User.id
            ).join(
                Professional, ConsultationBookingRequest.professional_id == Professional.id
            ).filter(
                and_(
                    ConsultationBookingRequest.status == 'pending',
                    ConsultationBookingRequest.consultation_category == 'casework',
                    ConsultationBookingRequest.created_at <= reminder_24h_cutoff,
                    ConsultationBookingRequest.reminder_24h_sent_at.is_(None),
                )
            ).all()

            sent_24h_count = 0
            for booking, user, professional in pending_24h:
                try:
                    if not user or not user.email:
                        continue

                    success = EmailService.send_pending_action_email(
                        to_email=user.email,
                        first_name=user.first_name,
                        pending_action="Consultation booking",
                    )
                    if success:
                        booking.reminder_24h_sent_at = now
                        sent_24h_count += 1
                    else:
                        logger.error(
                            f"Failed to send 24h reminder for booking {booking.id} "
                            f"(user {user.id})"
                        )

                    # Push notification, alongside the email above. Best
                    # effort -- a push failure here does not affect the
                    # email send/dedup logic, and vice versa.
                    try:
                        device_tokens = session.query(DeviceToken).filter(
                            and_(
                                DeviceToken.user_id == user.id,
                                DeviceToken.is_active.is_(True),
                            )
                        ).all()
                        if device_tokens:
                            push_result = await firebase_service.send_pending_action_notification(
                                device_tokens=[dt.device_token for dt in device_tokens],
                                professional_name=_professional_display_name(professional),
                                booking_request_id=str(booking.id),
                                referral_id=str(booking.referral_request_id or ''),
                            )
                            if push_result.get('success_count', 0) == 0:
                                logger.error(
                                    f"Failed to send 24h push reminder for booking {booking.id} "
                                    f"(user {user.id})"
                                )
                    except Exception as push_error:
                        logger.error(
                            f"Error sending 24h push reminder for booking {booking.id}: {str(push_error)}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error sending 24h reminder for booking {booking.id}: {str(e)}"
                    )
                    continue

            logger.info(
                f"send_consultation_booking_reminders: sent {sent_24h_count} "
                f"24h reminder(s)"
            )

            # ============================================
            # 72h follow-up
            # ============================================
            reminder_72h_cutoff = now - timedelta(hours=72)

            pending_72h = session.query(
                ConsultationBookingRequest, User, Professional
            ).join(
                User, ConsultationBookingRequest.user_id == User.id
            ).join(
                Professional, ConsultationBookingRequest.professional_id == Professional.id
            ).filter(
                and_(
                    ConsultationBookingRequest.status == 'pending',
                    ConsultationBookingRequest.consultation_category == 'casework',
                    ConsultationBookingRequest.created_at <= reminder_72h_cutoff,
                    ConsultationBookingRequest.reminder_24h_sent_at.isnot(None),
                    ConsultationBookingRequest.reminder_72h_sent_at.is_(None),
                )
            ).all()

            sent_72h_count = 0
            for booking, user, professional in pending_72h:
                try:
                    if not user or not user.email:
                        continue

                    success = EmailService.send_pending_action_email(
                        to_email=user.email,
                        first_name=user.first_name,
                        pending_action="Consultation booking",
                    )
                    if success:
                        booking.reminder_72h_sent_at = now
                        sent_72h_count += 1
                    else:
                        logger.error(
                            f"Failed to send 72h reminder for booking {booking.id} "
                            f"(user {user.id})"
                        )

                    # Push notification, alongside the email above. Best
                    # effort -- a push failure here does not affect the
                    # email send/dedup logic, and vice versa.
                    try:
                        device_tokens = session.query(DeviceToken).filter(
                            and_(
                                DeviceToken.user_id == user.id,
                                DeviceToken.is_active.is_(True),
                            )
                        ).all()
                        if device_tokens:
                            push_result = await firebase_service.send_pending_action_notification(
                                device_tokens=[dt.device_token for dt in device_tokens],
                                professional_name=_professional_display_name(professional),
                                booking_request_id=str(booking.id),
                                referral_id=str(booking.referral_request_id or ''),
                            )
                            if push_result.get('success_count', 0) == 0:
                                logger.error(
                                    f"Failed to send 72h push reminder for booking {booking.id} "
                                    f"(user {user.id})"
                                )
                    except Exception as push_error:
                        logger.error(
                            f"Error sending 72h push reminder for booking {booking.id}: {str(push_error)}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error sending 72h reminder for booking {booking.id}: {str(e)}"
                    )
                    continue

            logger.info(
                f"send_consultation_booking_reminders: sent {sent_72h_count} "
                f"72h follow-up reminder(s)"
            )

        logger.info("✅ Completed job: send_consultation_booking_reminders")

    except Exception as e:
        logger.error(f"❌ Error in send_consultation_booking_reminders: {str(e)}")
        raise