"""
Notification API endpoints for managing push notifications
Handles device token registration, notification preferences, and test notifications
"""

from fastapi import APIRouter, HTTPException, Request, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import logging
import uuid
from datetime import datetime, timezone
from sqlalchemy import desc
from services.models import DeviceToken, User, Notification
from services.firebase_notification_service import FirebaseNotificationService
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)

PROFILE_SURVEY_NOTIFICATION_TYPE = "profile_survey_incomplete"
PROFESSIONAL_TYPES = {
    "Lawyer",
    "Law Firm",
    "HR/Legal (SME)",
    "A2J Partners",
}


class DeviceTokenRequest(BaseModel):
    device_token: str
    device_type: str  # 'ios' or 'android'
    device_name: Optional[str] = None
    app_version: Optional[str] = None


class TestNotificationRequest(BaseModel):
    title: str
    body: str
    data: Optional[dict] = None


class SendNotificationRequest(BaseModel):
    to: str  # FCM device token
    notification: dict  # {"title": "...", "body": "..."}
    data: Optional[dict] = None  # Custom data payload


class MarkReadRequest(BaseModel):
    notification_ids: Optional[List[str]] = None  # None / omit = mark ALL unread as read


class NotificationAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.firebase_service = FirebaseNotificationService()

    @staticmethod
    def _message_notification_key(notification: Notification) -> Optional[str]:
        if notification.notification_type != "new_message":
            return None
        data = notification.data or {}
        for key in ("referral_id", "conversation_id", "session_id"):
            value = data.get(key)
            if value:
                return f"{key}:{value}"
        return None

    @classmethod
    def _dedupe_message_notifications(cls, rows: List[Notification]) -> List[Notification]:
        """
        Keep only the latest message notification per conversation/referral.

        ``rows`` must already be sorted newest-first. Non-message notification
        types are never deduped because they represent distinct events.
        """
        seen_message_keys = set()
        deduped: List[Notification] = []
        for row in rows:
            key = cls._message_notification_key(row)
            if key:
                if key in seen_message_keys:
                    continue
                seen_message_keys.add(key)
            deduped.append(row)
        return deduped

    @staticmethod
    def _sync_profile_survey_notification(session, recipient_id: uuid.UUID) -> None:
        """Ensure an incomplete client survey has one in-app notification.

        A read notification is not recreated, so tapping it still dismisses it
        from the inbox. Completing the survey retires any unread reminder.
        Professional profiles use the separate ``profile_incomplete`` flow.
        """
        user = session.query(User).filter(User.id == recipient_id).first()
        if not isinstance(user, User) or user.professional_type in PROFESSIONAL_TYPES:
            return

        existing = (
            session.query(Notification)
            .filter(
                Notification.recipient_id == recipient_id,
                Notification.notification_type == PROFILE_SURVEY_NOTIFICATION_TYPE,
            )
            .order_by(desc(Notification.created_at))
            .first()
        )
        survey_incomplete = not bool((user.immigration_status or "").strip())

        if survey_incomplete and existing is None:
            NotificationService(session).create(
                recipient_id=recipient_id,
                title="Complete Your Profile",
                body="Answer two quick questions so LawNeeds can personalise your experience.",
                notification_type=PROFILE_SURVEY_NOTIFICATION_TYPE,
                data={
                    "user_id": str(recipient_id),
                    "screen": "ProfileSurvey",
                    "action": "complete_profile_survey",
                },
            )
            session.flush()
        elif not survey_incomplete and existing is not None and not existing.is_read:
            existing.is_read = True
            existing.read_at = datetime.now(timezone.utc)
    
    def get_router(self):
        router = APIRouter()
        
        @router.post("/notifications/register_device")
        async def register_device_token(
            request: Request,
            data: DeviceTokenRequest
        ):
            """
            Register or update a device token for push notifications.
            This should be called when the app launches and gets the FCM token.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Validate device type
                if data.device_type not in ['ios', 'android']:
                    raise HTTPException(
                        status_code=400,
                        detail="device_type must be 'ios' or 'android'"
                    )
                
                with self.db.get_session() as session:
                    # Check if this device token already exists
                    existing_token = session.query(DeviceToken).filter(
                        DeviceToken.device_token == data.device_token
                    ).first()
                    
                    if existing_token:
                        # Update existing token (user might have logged in on a different account)
                        existing_token.user_id = uuid.UUID(user_id)
                        existing_token.device_type = data.device_type
                        existing_token.device_name = data.device_name
                        if data.app_version:
                            existing_token.app_version = data.app_version
                        existing_token.is_active = True
                        existing_token.updated_at = datetime.now(timezone.utc)
                        existing_token.last_used_at = datetime.now(timezone.utc)
                        logger.info(f"Updated device token for user {user_id}")
                    else:
                        # Create new device token
                        new_token = DeviceToken(
                            user_id=uuid.UUID(user_id),
                            device_token=data.device_token,
                            device_type=data.device_type,
                            device_name=data.device_name,
                            app_version=data.app_version,
                            is_active=True
                        )
                        session.add(new_token)
                        logger.info(f"Registered new device token for user {user_id}")
                    
                    session.commit()
                
                return {
                    "success": True,
                    "message": "Device token registered successfully"
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error registering device token: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/notifications/unregister_device")
        async def unregister_device_token(
            request: Request,
            device_token: str = Body(..., embed=True)
        ):
            """
            Unregister a device token (e.g., when user logs out).
            This marks the token as inactive rather than deleting it.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Find and deactivate the device token
                    token_record = session.query(DeviceToken).filter(
                        DeviceToken.device_token == device_token,
                        DeviceToken.user_id == uuid.UUID(user_id)
                    ).first()
                    
                    if token_record:
                        token_record.is_active = False
                        token_record.updated_at = datetime.now(timezone.utc)
                        session.commit()
                        logger.info(f"Unregistered device token for user {user_id}")
                        return {
                            "success": True,
                            "message": "Device token unregistered successfully"
                        }
                    else:
                        return {
                            "success": False,
                            "message": "Device token not found"
                        }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error unregistering device token: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/notifications/devices")
        async def get_user_devices(request: Request):
            """
            Get all registered devices for the current user.
            Useful for managing device notifications in settings.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    devices = session.query(DeviceToken).filter(
                        DeviceToken.user_id == uuid.UUID(user_id)
                    ).all()
                    
                    device_list = []
                    for device in devices:
                        device_list.append({
                            "id": str(device.id),
                            "device_type": device.device_type,
                            "device_name": device.device_name,
                            "is_active": device.is_active,
                            "created_at": device.created_at.isoformat(),
                            "last_used_at": device.last_used_at.isoformat() if device.last_used_at else None
                        })
                    
                    return {
                        "devices": device_list,
                        "total_count": len(device_list)
                    }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting user devices: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/notifications/test")
        async def send_test_notification(
            request: Request,
            data: TestNotificationRequest
        ):
            """
            Send a test notification to the current user's devices.
            Useful for testing notification functionality.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Get all active device tokens for this user
                    devices = session.query(DeviceToken).filter(
                        DeviceToken.user_id == uuid.UUID(user_id),
                        DeviceToken.is_active == True
                    ).all()
                    
                    if not devices:
                        return {
                            "success": False,
                            "message": "No active devices found for this user"
                        }
                    
                    device_tokens = [device.device_token for device in devices]
                    
                    # Send notification
                    result = await self.firebase_service.send_notification_to_multiple(
                        device_tokens=device_tokens,
                        title=data.title,
                        body=data.body,
                        data=data.data or {'type': 'test'}
                    )
                    
                    return {
                        "success": True,
                        "message": f"Test notification sent to {len(device_tokens)} device(s)",
                        "details": result
                    }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending test notification: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.delete("/notifications/device/{device_id}")
        async def delete_device(
            request: Request,
            device_id: str
        ):
            """
            Delete a specific device token permanently.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    # Find and delete the device token
                    device = session.query(DeviceToken).filter(
                        DeviceToken.id == uuid.UUID(device_id),
                        DeviceToken.user_id == uuid.UUID(user_id)
                    ).first()
                    
                    if device:
                        session.delete(device)
                        session.commit()
                        logger.info(f"Deleted device {device_id} for user {user_id}")
                        return {
                            "success": True,
                            "message": "Device deleted successfully"
                        }
                    else:
                        raise HTTPException(
                            status_code=404,
                            detail="Device not found or does not belong to this user"
                        )
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error deleting device: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/notifications/send")
        async def send_notification(
            request: Request,
            data: SendNotificationRequest
        ):
            """
            Send a push notification to a specific device token.
            This endpoint allows sending custom notifications with the format:
            {
                "to": "FCM_TOKEN",
                "notification": {
                    "title": "Request Accepted",
                    "body": "John has accepted your request."
                },
                "data": {
                    "actionType": "ACCEPTED",
                    "requestId": "REQ12345",
                    "userId": "USER_001",
                    "name": "Alice",
                    "profilePicture": "https://test.png"
                }
            }
            """
            try:
                # Verify user token for authentication
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Validate notification structure
                if not isinstance(data.notification, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="notification must be a dictionary with 'title' and 'body'"
                    )
                
                title = data.notification.get("title")
                body = data.notification.get("body")
                
                if not title or not body:
                    raise HTTPException(
                        status_code=400,
                        detail="notification must contain 'title' and 'body' fields"
                    )
                
                # Send notification to the specified device token
                result = await self.firebase_service.send_notification(
                    device_token=data.to,
                    title=title,
                    body=body,
                    data=data.data or {}
                )
                
                if result:
                    logger.info(f"Notification sent successfully to token {data.to[:20]}... by user {user_id}")
                    return {
                        "success": True,
                        "message": "Notification sent successfully"
                    }
                else:
                    logger.warning(f"Failed to send notification to token {data.to[:20]}...")
                    return {
                        "success": False,
                        "message": "Failed to send notification. Firebase may not be initialized or token is invalid."
                    }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/notifications/list")
        async def list_notifications(
            request: Request,
            limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0),
            unread_only: bool = Query(False, description="Return only unread notifications"),
        ):
            """
            Return all persisted notifications for the authenticated user or lawyer,
            ordered most-recent first.  Always includes an unread_count badge field.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                with self.db.get_session() as session:
                    recipient_uuid = uuid.UUID(user_id)
                    self._sync_profile_survey_notification(session, recipient_uuid)

                    base_q = session.query(Notification).filter(
                        Notification.recipient_id == recipient_uuid
                    )

                    unread_rows = (
                        base_q.filter(Notification.is_read == False)
                        .order_by(desc(Notification.created_at))
                        .all()
                    )
                    unread_count = len(self._dedupe_message_notifications(unread_rows))

                    if unread_only:
                        list_q = base_q.filter(Notification.is_read == False)
                    else:
                        list_q = base_q

                    all_rows = (
                        list_q.order_by(desc(Notification.created_at))
                        .all()
                    )
                    deduped_rows = self._dedupe_message_notifications(all_rows)
                    total = len(deduped_rows)
                    rows = deduped_rows[offset: offset + limit]

                    items = [
                        {
                            "id": str(n.id),
                            "title": n.title,
                            "body": n.body,
                            "notification_type": n.notification_type,
                            "data": n.data,
                            "is_read": n.is_read,
                            "read_at": n.read_at.isoformat() if n.read_at else None,
                            "created_at": n.created_at.isoformat(),
                        }
                        for n in rows
                    ]

                return {
                    "notifications": items,
                    "total_count": total,
                    "unread_count": unread_count,
                    "limit": limit,
                    "offset": offset,
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error listing notifications: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/notifications/mark_read")
        async def mark_notifications_read(
            request: Request,
            data: MarkReadRequest,
        ):
            """
            Mark notifications as read for the current user/lawyer.
            - Provide notification_ids to mark specific notifications.
            - Omit or pass null for notification_ids to mark ALL unread as read.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                now = datetime.now(timezone.utc)
                recipient_uuid = uuid.UUID(user_id)

                with self.db.get_session() as session:
                    base_q = session.query(Notification).filter(
                        Notification.recipient_id == recipient_uuid,
                        Notification.is_read == False,
                    )

                    if data.notification_ids:
                        try:
                            id_uuids = [uuid.UUID(nid) for nid in data.notification_ids]
                        except ValueError:
                            raise HTTPException(
                                status_code=400,
                                detail="One or more notification IDs are not valid UUIDs",
                            )
                        notifications = base_q.filter(
                            Notification.id.in_(id_uuids)
                        ).all()
                    else:
                        notifications = base_q.all()

                    updated_count = 0
                    for n in notifications:
                        n.is_read = True
                        n.read_at = now
                        updated_count += 1

                return {
                    "success": True,
                    "updated_count": updated_count,
                    "message": f"{updated_count} notification(s) marked as read",
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error marking notifications as read: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        return router
