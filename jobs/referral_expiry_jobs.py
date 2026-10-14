"""Scheduled expiry processing for pending referral requests.

Finds LawyerInbox entries stuck at status == "pending" whose created_at
is older than RESPONSE_WINDOW_HOURS, and for each:
  - transitions ReferralRequest.status -> ReferralStatus.EXPIRED.value
  - transitions LawyerInbox.status -> "expired"
  - notifies both the referral's user and the lawyer (in-app + push)

Mirrors the structure of jobs/consultation_extension_jobs.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.firebase_notification_service import FirebaseNotificationService
from services.models import DeviceToken, LawyerInbox, ReferralRequest, ReferralStatus
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)

RESPONSE_WINDOW_HOURS = 48


async def expire_pending_referrals(db) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RESPONSE_WINDOW_HOURS)

    with db.get_session() as lookup_session:
        due_ids = [
            row.id for row in lookup_session.query(LawyerInbox).filter(
                LawyerInbox.status == "pending",
                LawyerInbox.created_at <= cutoff,
            ).all()
        ]

    if not due_ids:
        return

    logger.info(
        "Found %d pending referrals past the %dh response window.",
        len(due_ids), RESPONSE_WINDOW_HOURS,
    )

    for inbox_id in due_ids:
        with db.get_session() as session:
            inbox_entry = (
                session.query(LawyerInbox)
                .filter(LawyerInbox.id == inbox_id)
                .with_for_update()
                .first()
            )
            if not inbox_entry or inbox_entry.status != "pending":
                continue

            referral = (
                session.query(ReferralRequest)
                .filter(ReferralRequest.id == inbox_entry.referral_request_id)
                .with_for_update()
                .first()
            )
            if not referral:
                logger.warning(
                    "LawyerInbox %s references missing ReferralRequest %s, skipping.",
                    inbox_entry.id, inbox_entry.referral_request_id,
                )
                continue

            referral.status = ReferralStatus.EXPIRED.value
            inbox_entry.status = "expired"

            referral_id_str = str(referral.id)
            lawyer_id = inbox_entry.lawyer_id
            user_id = referral.user_id

            data = {
                "type": "referral_expired",
                "referral_id": referral_id_str,
                "screen": "Referrals",
                "action": "view_status",
            }
            notification_service = NotificationService(session)
            for recipient_id in (user_id, lawyer_id):
                notification_service.create(
                    recipient_id=recipient_id,
                    title="Referral Expired",
                    body="This referral expired without a response and is no longer active.",
                    notification_type="referral_expired",
                    data=data,
                )
            session.commit()

            try:
                firebase = FirebaseNotificationService()
                for recipient_id in (user_id, lawyer_id):
                    tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == recipient_id,
                        DeviceToken.is_active == True,
                    ).all()
                    if tokens:
                        await firebase.send_referral_expired_notification(
                            device_tokens=[token.device_token for token in tokens],
                            referral_id=referral_id_str,
                        )
            except Exception as exc:
                logger.warning("Failed to deliver referral expired push: %s", exc)