"""
We Miss You Re-engagement Jobs

Sends the 14-day "We Miss You" re-engagement email to users who haven't
used LawNeeds (as tracked by User.last_seen_at, updated on each verified
request) in 14+ days.

Split by account type (same User table, distinguished by
professional_type):
- send_we_miss_you_reminders: client users (professional_type IS NULL),
  gets the client-worded copy via EmailService.send_we_miss_you_email.
- send_we_miss_you_professional_reminders: professional accounts
  (professional_type IS NOT NULL - lawyer, law firm, HR/legal, A2J
  partner), gets the professional-worded copy via
  EmailService.send_we_miss_you_professional_email.

Both share the same User.last_seen_at / User.we_miss_you_14d_sent_at
columns - no separate schema needed, since professional_type already
distinguishes the two audiences on the same table. Only the query
filter and the EmailService method called differ.

Only the 14-day stage is implemented. The 30/60/90-day stages described
in the spec are NOT built yet -- copy for those stages has not been
provided and must not be invented (see services/email_templates/we_miss_you.py,
we_miss_you_professional.py and their EmailService methods for the same note).

Dedup: tracked via the nullable we_miss_you_14d_sent_at timestamp column
on User, following the same pattern as reminder_24h_sent_at /
reminder_72h_sent_at on ConsultationBookingRequest. Unlike a booking
(which is static once created), a user's activity can restart at any
time, so eligibility is NOT simply "sent_at is None" -- it's "sent_at is
None OR sent_at is older than the user's current last_seen_at". That
comparison lets the reminder re-fire naturally if the user returns and
then goes inactive again, without needing to explicitly reset the
column on login.

Runs daily (not hourly, since this is a day-granularity threshold).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_

logger = logging.getLogger(__name__)


async def send_we_miss_you_reminders(db):
    """
    Send the 14-day "We Miss You" re-engagement email to eligible CLIENT
    users (professional_type IS NULL).

    Runs daily. Only handles the 14-day stage.
    """
    logger.info("Starting job: send_we_miss_you_reminders")

    try:
        from services.models import User
        from services.email import EmailService

        with db.get_session() as session:
            now = datetime.now(timezone.utc)

            # ============================================
            # 14-day reminder (client users only)
            # ============================================
            cutoff_14d = now - timedelta(days=14)

            eligible_users = session.query(User).filter(
                and_(
                    User.professional_type.is_(None),
                    User.last_seen_at.isnot(None),
                    User.last_seen_at <= cutoff_14d,
                    or_(
                        User.we_miss_you_14d_sent_at.is_(None),
                        User.we_miss_you_14d_sent_at < User.last_seen_at,
                    ),
                )
            ).all()

            sent_count = 0
            for user in eligible_users:
                try:
                    if not user.email:
                        continue

                    success = EmailService.send_we_miss_you_email(
                        to_email=user.email,
                        first_name=user.first_name,
                    )
                    if success:
                        user.we_miss_you_14d_sent_at = now
                        sent_count += 1
                    else:
                        logger.error(
                            f"Failed to send 14-day We Miss You email to user {user.id}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error sending 14-day We Miss You email to user {user.id}: {str(e)}"
                    )
                    continue

            logger.info(
                f"send_we_miss_you_reminders: sent {sent_count} 14-day reminder(s)"
            )

        logger.info("✅ Completed job: send_we_miss_you_reminders")

    except Exception as e:
        logger.error(f"❌ Error in send_we_miss_you_reminders: {str(e)}")


async def send_we_miss_you_professional_reminders(db):
    """
    Send the 14-day "We Miss You" re-engagement email to eligible
    PROFESSIONAL accounts (professional_type IS NOT NULL).

    Mirrors send_we_miss_you_reminders() above, filtered to the opposite
    audience and calling the professional-worded email instead. Shares
    the same last_seen_at / we_miss_you_14d_sent_at columns.

    Runs daily. Only handles the 14-day stage.
    """
    logger.info("Starting job: send_we_miss_you_professional_reminders")

    try:
        from services.models import User
        from services.email import EmailService

        with db.get_session() as session:
            now = datetime.now(timezone.utc)

            # ============================================
            # 14-day reminder (professional accounts only)
            # ============================================
            cutoff_14d = now - timedelta(days=14)

            eligible_users = session.query(User).filter(
                and_(
                    User.professional_type.isnot(None),
                    User.last_seen_at.isnot(None),
                    User.last_seen_at <= cutoff_14d,
                    or_(
                        User.we_miss_you_14d_sent_at.is_(None),
                        User.we_miss_you_14d_sent_at < User.last_seen_at,
                    ),
                )
            ).all()

            sent_count = 0
            for user in eligible_users:
                try:
                    if not user.email:
                        continue

                    success = EmailService.send_we_miss_you_professional_email(
                        to_email=user.email,
                        first_name=user.first_name,
                    )
                    if success:
                        user.we_miss_you_14d_sent_at = now
                        sent_count += 1
                    else:
                        logger.error(
                            f"Failed to send 14-day We Miss You (professional) email to user {user.id}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error sending 14-day We Miss You (professional) email to user {user.id}: {str(e)}"
                    )
                    continue

            logger.info(
                f"send_we_miss_you_professional_reminders: sent {sent_count} 14-day reminder(s)"
            )

        logger.info("✅ Completed job: send_we_miss_you_professional_reminders")

    except Exception as e:
        logger.error(f"❌ Error in send_we_miss_you_professional_reminders: {str(e)}")


async def send_inactive_user_push_reminders(db):
    """
    Send the 14-day inactive_user push notification to eligible users.

    Mirrors the eligibility query in send_we_miss_you_reminders() above,
    but targets DeviceToken instead of email, and uses the independent
    inactive_user_push_sent_at dedup column on User (kept separate from
    we_miss_you_14d_sent_at so push and email can succeed, fail, or
    re-fire on their own schedules -- e.g. a user without a device token
    still gets the email, and a user who unsubscribed from email but
    still has the app still gets the push).

    Runs daily, alongside send_we_miss_you_reminders.
    """
    logger.info("Starting job: send_inactive_user_push_reminders")

    try:
        from services.models import User, DeviceToken
        from services.firebase_notification_service import FirebaseNotificationService

        firebase_service = FirebaseNotificationService()

        with db.get_session() as session:
            now = datetime.now(timezone.utc)

            cutoff_14d = now - timedelta(days=14)

            eligible_users = session.query(User).filter(
                and_(
                    User.last_seen_at.isnot(None),
                    User.last_seen_at <= cutoff_14d,
                    or_(
                        User.inactive_user_push_sent_at.is_(None),
                        User.inactive_user_push_sent_at < User.last_seen_at,
                    ),
                )
            ).all()

            sent_count = 0
            for user in eligible_users:
                try:
                    device_tokens = session.query(DeviceToken).filter(
                        and_(
                            DeviceToken.user_id == user.id,
                            DeviceToken.is_active.is_(True),
                        )
                    ).all()

                    if not device_tokens:
                        continue

                    tokens = [dt.device_token for dt in device_tokens]

                    result = await firebase_service.send_welcome_back_notification(
                        device_tokens=tokens,
                        user_name=user.first_name,
                    )

                    if result.get('success_count', 0) > 0:
                        user.inactive_user_push_sent_at = now
                        sent_count += 1
                    else:
                        logger.error(
                            f"Failed to send inactive_user push to user {user.id} "
                            f"(0/{result.get('total', len(tokens))} device(s) succeeded)"
                        )
                except Exception as e:
                    logger.error(
                        f"Error sending inactive_user push to user {user.id}: {str(e)}"
                    )
                    continue

            logger.info(
                f"send_inactive_user_push_reminders: sent {sent_count} 14-day push reminder(s)"
            )

        logger.info("✅ Completed job: send_inactive_user_push_reminders")

    except Exception as e:
        logger.error(f"❌ Error in send_inactive_user_push_reminders: {str(e)}")
        raise


async def send_welcome_back_in_app_notifications(db):
    """
    Create the in-app "welcome back" notification row for eligible users.

    Mirrors the eligibility query in send_we_miss_you_reminders() and
    send_inactive_user_push_reminders() above, but writes to the
    notifications table instead of sending email/push, and uses its own
    independent welcome_back_in_app_sent_at dedup column so the in-app
    notification succeeds, fails, or re-fires on its own schedule --
    unrelated to whether the email or push channel succeeded.

    Runs daily, alongside the other two.
    """
    logger.info("Starting job: send_welcome_back_in_app_notifications")

    try:
        from services.models import User
        from services.notification_service import NotificationService

        with db.get_session() as session:
            now = datetime.now(timezone.utc)

            cutoff_14d = now - timedelta(days=14)

            eligible_users = session.query(User).filter(
                and_(
                    User.last_seen_at.isnot(None),
                    User.last_seen_at <= cutoff_14d,
                    or_(
                        User.welcome_back_in_app_sent_at.is_(None),
                        User.welcome_back_in_app_sent_at < User.last_seen_at,
                    ),
                )
            ).all()

            sent_count = 0
            for user in eligible_users:
                try:
                    NotificationService(session).create(
                        recipient_id=user.id,
                        title="We Miss You! \U0001F44B",
                        body="It's been a while \u2014 come see what's new.",
                        notification_type="welcome_back",
                        data={"screen": "Home"},
                    )
                    user.welcome_back_in_app_sent_at = now
                    sent_count += 1
                except Exception as e:
                    logger.error(
                        f"Error creating welcome_back in-app notification for user {user.id}: {str(e)}"
                    )
                    continue

            logger.info(
                f"send_welcome_back_in_app_notifications: created {sent_count} 14-day in-app notification(s)"
            )

        logger.info("✅ Completed job: send_welcome_back_in_app_notifications")

    except Exception as e:
        logger.error(f"❌ Error in send_welcome_back_in_app_notifications: {str(e)}")
        raise