"""
NotificationService — thin helper for persisting in-app notification records.

Usage (inside any endpoint that already has a DB session open):

    from services.notification_service import NotificationService

    with self.db.get_session() as session:
        NotificationService(session).create(
            recipient_id=uuid.UUID(lawyer_id),
            title="New Client Request!",
            body="Alice has sent you a referral request.",
            notification_type="new_referral",
            data={"referral_id": referral_id, "screen": "LawyerInbox"},
        )
"""

from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from services.models import Notification

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, session):
        self.session = session

    def create(
        self,
        recipient_id: uuid.UUID,
        title: str,
        body: str,
        notification_type: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Notification:
        """
        Persist a notification row for the given recipient.
        The caller is responsible for committing the session.
        """
        notification = Notification(
            recipient_id=recipient_id,
            title=title,
            body=body,
            notification_type=notification_type,
            data=data,
            is_read=False,
        )
        self.session.add(notification)
        logger.debug(
            "Queued notification for recipient=%s type=%s",
            recipient_id,
            notification_type,
        )
        return notification

    def get_unread_count(self, recipient_id: uuid.UUID) -> int:
        """Return the number of unread notifications for a recipient."""
        return (
            self.session.query(Notification)
            .filter(
                Notification.recipient_id == recipient_id,
                Notification.is_read == False,
            )
            .count()
        )

    def create_or_replace_message(
        self,
        recipient_id: uuid.UUID,
        referral_id: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Notification:
        """
        Persist a 'new_message' notification, replacing any previous unread
        'new_message' notification for the same (recipient, referral).

        This prevents the inbox from being flooded with one notification per
        message, so users only see the latest message notification per
        conversation in /notifications/list. Important notifications of other
        types (e.g. new_referral, one_minute_warning) are never affected.
        """
        previous = (
            self.session.query(Notification)
            .filter(
                Notification.recipient_id == recipient_id,
                Notification.notification_type == "new_message",
                Notification.is_read == False,
            )
            .all()
        )
        for old in previous:
            if (old.data or {}).get("referral_id") == referral_id:
                self.session.delete(old)

        return self.create(
            recipient_id=recipient_id,
            title=title,
            body=body,
            notification_type="new_message",
            data=data,
        )
