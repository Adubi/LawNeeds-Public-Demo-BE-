"""Hourly reminders from minimal metadata, with seven-day retention."""
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from services.models import RegistrationReminderStatus, User
from services.registration_security import cleanup, lock_email, utc
from services.email import EmailService

logger = logging.getLogger(__name__)

async def send_registration_completion_reminders(db):
    with db.get_session() as session:
        cleanup(session)
    with db.get_session() as session:
        emails = [row[0] for row in session.query(RegistrationReminderStatus.email).all()]
    for email in emails:
        try:
            with db.get_session() as session:
                lock_email(session, email)
                status = session.query(RegistrationReminderStatus).filter_by(email=email).with_for_update().first()
                if not status or not status.started_at:
                    continue
                if session.query(User.id).filter(func.lower(User.email) == email).first():
                    continue
                now = datetime.now(timezone.utc)
                started = utc(status.started_at)
                field = None
                if status.reminder_24h_sent_at is None and started <= now - timedelta(hours=24):
                    field = "reminder_24h_sent_at"
                elif (status.reminder_24h_sent_at is not None and status.reminder_72h_sent_at is None
                      and started <= now - timedelta(hours=72)):
                    field = "reminder_72h_sent_at"
                if not field:
                    continue
                sender = (EmailService.send_registration_completion_reminder_professional_email
                          if status.professional_type else EmailService.send_registration_completion_reminder_email)
                if sender(to_email=email, first_name=status.first_name):
                    setattr(status, field, now)
                else:
                    logger.warning("Registration reminder send failed")
        except Exception:
            logger.error("Registration reminder processing failed")
