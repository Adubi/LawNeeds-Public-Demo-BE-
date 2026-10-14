"""Scheduled check for lawyers with a low referral response rate.

Computes each lawyer's response rate over a rolling 90-day window as
(accepted/rejected entries with responded_at) / (accepted + rejected +
expired entries). Pending requests do not lower the rate until the 48-hour
response window expires, and client-cancelled referrals are excluded.

Policy (see notification build-out doc for reasoning):
  - Window: rolling 90 days
  - Threshold: fires when rate drops below 50%
  - Minimum sample size: 5 total inbox entries in the window (lawyers
    below this are skipped entirely -- too few data points to be a
    fair signal)
  - Firing frequency: fires once per crossing. User.response_rate_warning_sent_at
    is set on fire, and cleared back to null once the lawyer's rate
    recovers back above the threshold -- so a later drop is treated as
    a fresh crossing and can fire again, rather than repeating weekly
    while they remain below threshold.

Runs daily. Mirrors the structure of jobs/referral_expiry_jobs.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.firebase_notification_service import FirebaseNotificationService
from services.models import DeviceToken, LawyerInbox, User
from services.notification_service import NotificationService
from sqlalchemy import and_, case, func

logger = logging.getLogger(__name__)

WINDOW_DAYS = 90
THRESHOLD = 0.5
MIN_SAMPLE_SIZE = 5


async def check_lawyer_response_rates(db) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    with db.get_session() as lookup_session:
        rows = (
            lookup_session.query(
                LawyerInbox.lawyer_id,
                func.count(LawyerInbox.id).label("total"),
                func.sum(
                    case(
                        (
                            and_(
                                LawyerInbox.status.in_(["accepted", "rejected"]),
                                LawyerInbox.responded_at.isnot(None),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("responded_count"),
            )
            .filter(
                LawyerInbox.created_at >= cutoff,
                LawyerInbox.status.in_(["accepted", "rejected", "expired"]),
            )
            .group_by(LawyerInbox.lawyer_id)
            .having(func.count(LawyerInbox.id) >= MIN_SAMPLE_SIZE)
            .all()
        )

    if not rows:
        return

    for lawyer_id, total, responded_count in rows:
        rate = responded_count / total

        with db.get_session() as session:
            user = (
                session.query(User)
                .filter(User.id == lawyer_id)
                .with_for_update()
                .first()
            )
            if not user:
                continue

            if rate >= THRESHOLD:
                if user.response_rate_warning_sent_at is not None:
                    user.response_rate_warning_sent_at = None
                    session.commit()
                continue

            if user.response_rate_warning_sent_at is not None:
                continue

            user.response_rate_warning_sent_at = datetime.now(timezone.utc)

            data = {
                "type": "response_rate_warning",
                "response_rate": f"{rate:.0%}",
                "screen": "Profile",
                "action": "view_status",
            }
            notification_service = NotificationService(session)
            notification_service.create(
                recipient_id=lawyer_id,
                title="Response Rate Below Target",
                body="Your referral response rate has dropped below 50% over the last 90 days.",
                notification_type="response_rate_warning",
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
                    await firebase.send_response_rate_warning_notification(
                        device_tokens=[token.device_token for token in tokens],
                        response_rate=f"{rate:.0%}",
                    )
            except Exception as exc:
                logger.warning("Failed to deliver response rate warning push: %s", exc)
