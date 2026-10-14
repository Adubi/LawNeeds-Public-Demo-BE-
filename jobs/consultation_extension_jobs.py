"""Scheduled expiry processing for paused consultation extension workflows."""

import logging
from datetime import datetime, timezone

from services.consultation_extension_service import ConsultationExtensionService
from services.firebase_notification_service import FirebaseNotificationService
from services.models import ConsultationExtensionRequest, DeviceToken
from services.notification_service import NotificationService


logger = logging.getLogger(__name__)


async def expire_consultation_extensions(db) -> None:
    with db.get_session() as lookup_session:
        due_ids = [
            row.id for row in lookup_session.query(ConsultationExtensionRequest).filter(
                ConsultationExtensionRequest.status.in_(ConsultationExtensionService.ACTIVE_STATUSES),
                ConsultationExtensionRequest.expires_at <= datetime.now(timezone.utc),
            ).all()
        ]

    for request_id in due_ids:
        with db.get_session() as session:
            service = ConsultationExtensionService(session)
            request_obj = service._require_request(request_id, lock=True)
            if not service.expire_if_due(request_obj):
                continue

            data = {
                "type": "extension_timed_out",
                "session_id": str(request_obj.session_id),
                "extension_request_id": str(request_obj.id),
                "screen": "ConsultationSession",
                "action": "view_extension",
            }
            notification_service = NotificationService(session)
            for recipient_id in (request_obj.user_id, request_obj.lawyer_id):
                notification_service.create(
                    recipient_id=recipient_id,
                    title="Extension Request Timed Out",
                    body="The extension request timed out and the original timer has resumed.",
                    notification_type="extension_timed_out",
                    data=data,
                )
            session.commit()

            try:
                firebase = FirebaseNotificationService()
                for recipient_id in (request_obj.user_id, request_obj.lawyer_id):
                    tokens = session.query(DeviceToken).filter(
                        DeviceToken.user_id == recipient_id,
                        DeviceToken.is_active == True,
                    ).all()
                    if tokens:
                        await firebase.send_notification_to_multiple(
                            device_tokens=[token.device_token for token in tokens],
                            title="Extension Request Timed Out",
                            body="The extension request timed out and the original timer has resumed.",
                            data=data,
                            badge=1,
                            sound="default",
                            channel_id="high_importance_channel",
                        )
            except Exception as exc:
                logger.warning("Failed to deliver extension timeout push: %s", exc)
