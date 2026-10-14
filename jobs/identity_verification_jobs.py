"""
jobs/identity_verification_jobs.py

Identity Verification Reminder (automation 3) + Not Commenced / Abandoned
in-app + push notifications.

REMINDER JOB (existing):
Spec: sent twice monthly -- the 1st Monday and the last Monday of each
month -- only to users who require identity verification and haven't
been approved yet. Also fires "verification_not_commenced" in-app/push
for the subset of those recipients who have no IdentityVerification
record, or whose latest record is still "not_started".

ABANDONED CHECK (new):
Runs DAILY (no 1st/last-Monday gate). Flags any IdentityVerification row
stuck at status == "in_progress" whose last_event_at is 7+ days old, and
fires "verification_abandoned" in-app + push exactly once per inquiry
(guarded by checking for a prior notification tagged with that
inquiry_id, since there's no dedicated "abandoned_notified_at" column).

Both jobs are registered in jobs/__init__.py the same way as the other
subscription jobs: added to the shared APScheduler AsyncIOScheduler with
args=[db].
"""

import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from services.models import IdentityVerification, User, DeviceToken, Notification
from services.email import EmailService
from services.persona_service import identity_verification_required, latest_identity_verification
from services.notification_service import NotificationService
from services.firebase_notification_service import FirebaseNotificationService

logger = logging.getLogger(__name__)

ABANDONED_THRESHOLD_DAYS = 7


def _mondays_in_month(year: int, month: int):
    """Return all Monday dates in the given year/month, in order."""
    _, days_in_month = calendar.monthrange(year, month)
    return [
        date(year, month, day)
        for day in range(1, days_in_month + 1)
        if date(year, month, day).weekday() == 0  # Monday == 0
    ]


def _is_first_or_last_monday(today: date) -> bool:
    mondays = _mondays_in_month(today.year, today.month)
    if not mondays:
        return False
    return today == mondays[0] or today == mondays[-1]


def _get_unverified_eligible_users(session):
    """
    Users who require identity verification and do NOT have an approved
    IdentityVerification record. Mirrors the eligibility check used in
    persona_service.identity_verification_required / _approved, but as a
    bulk query instead of per-user lookups.
    """
    approved_user_ids = {
        row[0]
        for row in session.query(IdentityVerification.user_id)
        .filter(IdentityVerification.status == "approved")
        .all()
    }

    candidates = session.query(User).all()
    return [
        user
        for user in candidates
        if identity_verification_required(user) and user.id not in approved_user_ids
    ]


async def send_identity_verification_reminders(db, force: bool = False) -> dict:
    """
    Scheduled job entrypoint (called by APScheduler with args=[db], same
    as every other job in this package). Set force=True to bypass the
    "is today the 1st/last Monday" gate -- useful for manual/admin testing.
    """
    today = datetime.now(timezone.utc).date()

    if not force and not _is_first_or_last_monday(today):
        logger.info(
            "Skipping identity verification reminder run: %s is not the "
            "1st or last Monday of the month.",
            today,
        )
        return {"ran": False, "reason": "not_scheduled_day", "sent": 0, "failed": 0}

    sent, failed = 0, 0
    not_commenced_notified = 0
    firebase_service = None

    with db.get_session() as session:
        users = _get_unverified_eligible_users(session)
        logger.info("Sending identity verification reminders to %d users.", len(users))

        for user in users:
            # Existing email reminder -- unchanged, goes to everyone in
            # the broader unverified/eligible set regardless of status.
            # Professional accounts (professional_type is set, e.g.
            # "Lawyer" / "Law Firm") get the professional-copy reminder;
            # regular users get the standard one.
            email = getattr(user, "email", None)
            if email:
                if getattr(user, "professional_type", None):
                    ok = EmailService.send_identity_verification_reminder_professional_email(
                        email, getattr(user, "first_name", None)
                    )
                else:
                    ok = EmailService.send_identity_verification_reminder_email(
                        email, getattr(user, "first_name", None)
                    )
                if ok:
                    sent += 1
                else:
                    failed += 1
                    logger.warning(
                        "Failed to send identity verification reminder to user_id=%s",
                        user.id,
                    )

            # In-app + push "verification_not_commenced" notification,
            # only for the subset whose latest verification is not_started
            # or who have no IdentityVerification record at all.
            verification = latest_identity_verification(session, user.id)
            not_commenced = verification is None or verification.status == "not_started"
            if not not_commenced:
                continue

            NotificationService(session).create(
                recipient_id=user.id,
                title="Verify Your Identity",
                body="You haven't started identity verification yet. It only takes a few minutes.",
                notification_type="verification_not_commenced",
                data={"screen": "identity-verification"},
            )

            device_tokens = (
                session.query(DeviceToken)
                .filter(
                    DeviceToken.user_id == user.id,
                    DeviceToken.is_active == True,
                )
                .all()
            )
            if device_tokens:
                if firebase_service is None:
                    firebase_service = FirebaseNotificationService()
                tokens = [dt.device_token for dt in device_tokens]
                await firebase_service.send_identity_verification_not_commenced_notification(
                    device_tokens=tokens,
                    user_id=str(user.id),
                )
            not_commenced_notified += 1

        session.commit()

    logger.info(
        "Identity verification reminders: sent=%d failed=%d not_commenced_notified=%d",
        sent, failed, not_commenced_notified,
    )
    return {
        "ran": True,
        "sent": sent,
        "failed": failed,
        "not_commenced_notified": not_commenced_notified,
    }



def _already_notified_abandoned(session, user_id, inquiry_id) -> bool:
    """
    Guard against re-firing verification_abandoned every day once a given
    inquiry has already been flagged. There's no dedicated
    "abandoned_notified_at" column, so this checks whether a prior
    verification_abandoned Notification exists for this exact inquiry_id.
    """
    existing = (
        session.query(Notification)
        .filter(
            Notification.recipient_id == user_id,
            Notification.notification_type == "verification_abandoned",
        )
        .all()
    )
    return any((n.data or {}).get("inquiry_id") == inquiry_id for n in existing)


async def send_identity_verification_abandoned_check(db) -> dict:
    """
    Scheduled job entrypoint, runs DAILY (called by APScheduler with
    args=[db]). No day-of-month gate, unlike send_identity_verification_reminders.

    Flags IdentityVerification rows stuck at status == "in_progress" with
    last_event_at older than ABANDONED_THRESHOLD_DAYS, and fires
    "verification_abandoned" in-app + push exactly once per inquiry.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=ABANDONED_THRESHOLD_DAYS)
    notified = 0
    firebase_service = None

    with db.get_session() as session:
        stale_verifications = (
            session.query(IdentityVerification)
            .filter(
                IdentityVerification.status == "in_progress",
                IdentityVerification.last_event_at < cutoff,
            )
            .all()
        )
        logger.info(
            "Found %d stale in_progress verifications (threshold=%dd).",
            len(stale_verifications), ABANDONED_THRESHOLD_DAYS,
        )

        for verification in stale_verifications:
            if _already_notified_abandoned(session, verification.user_id, verification.inquiry_id):
                continue

            NotificationService(session).create(
                recipient_id=verification.user_id,
                title="Finish Verifying Your Identity",
                body="Your identity verification is still incomplete. Pick up where you left off.",
                notification_type="verification_abandoned",
                data={
                    "screen": "identity-verification",
                    "inquiry_id": verification.inquiry_id,
                },
            )

            device_tokens = (
                session.query(DeviceToken)
                .filter(
                    DeviceToken.user_id == verification.user_id,
                    DeviceToken.is_active == True,
                )
                .all()
            )
            if device_tokens:
                if firebase_service is None:
                    firebase_service = FirebaseNotificationService()
                tokens = [dt.device_token for dt in device_tokens]
                await firebase_service.send_identity_verification_abandoned_notification(
                    device_tokens=tokens,
                    user_id=str(verification.user_id),
                )
            notified += 1

        session.commit()

    logger.info("Identity verification abandoned check: notified=%d", notified)
    return {"ran": True, "notified": notified}