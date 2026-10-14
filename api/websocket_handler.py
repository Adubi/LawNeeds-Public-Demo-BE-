from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from typing import Dict
import asyncio
import json
import logging
import uuid
from datetime import datetime
from services.redis_service import RedisService
from services.models import ReferralSession, ReferralRequest, LawyerInbox, InboxMessage, User
from services.referral_user_status_payload import build_referral_user_status_payload
from services.referral_user_status_hub import referral_user_status_hub
from services.connection_manager import ConnectionManager, inbox_manager
from services.contact_details import CONTACT_DETAILS_ERROR, contains_contact_details
from services.persona_service import require_identity_verification_for_feature
from sqlalchemy.orm import Session
from sqlalchemy import and_

logger = logging.getLogger(__name__)


class WebSocketAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.manager = inbox_manager
        self.redis_service = RedisService()
    
    def get_router(self):
        router = APIRouter()
        
        @router.websocket("/ws/referral/{session_id}")
        async def websocket_endpoint(
            websocket: WebSocket,
            session_id: str,
            token: str
        ):
            """WebSocket endpoint for real-time referral communication"""
            user_id = None
            
            try:
                # Verify token
                token_data = self.auth.verify_token(token)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    await websocket.close(code=4001, reason="Invalid token")
                    return
                
                # Verify user is part of this session
                with self.db.get_session() as db_session:
                    user = db_session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not user:
                        await websocket.close(code=4004, reason="User not found")
                        return
                    try:
                        require_identity_verification_for_feature(db_session, user)
                    except HTTPException:
                        await websocket.close(code=4003, reason="Identity verification required")
                        return

                    session_obj = db_session.query(ReferralSession).filter(
                        ReferralSession.id == uuid.UUID(session_id)
                    ).first()
                    
                    if not session_obj:
                        await websocket.close(code=4004, reason="Session not found")
                        return
                    
                    # Check if user is either the client or the lawyer
                    if str(session_obj.user_id) != user_id and str(session_obj.lawyer_id) != user_id:
                        await websocket.close(code=4003, reason="Access denied")
                        return
                
                # Connect the user
                await self.manager.connect(websocket, user_id, session_id, "session")
                
                # Send initial connection message
                welcome_message = {
                    "type": "connection",
                    "message": "Connected to referral session",
                    "session_id": session_id,
                    "user_id": user_id
                }
                await websocket.send_text(json.dumps(welcome_message))
                
                # Handle incoming messages
                while True:
                    data = await websocket.receive_text()
                    await self._handle_message(data, session_id, user_id)
                    
            except WebSocketDisconnect:
                if user_id:
                    self.manager.disconnect(user_id, session_id, "session")
                    # Notify others of disconnection
                    await self.manager.broadcast_to_session(
                        json.dumps({
                            "type": "user_disconnected",
                            "user_id": user_id,
                            "timestamp": datetime.now().isoformat()
                        }),
                        session_id,
                        exclude_user=user_id
                    )
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if user_id:
                    self.manager.disconnect(user_id, session_id, "session")

        @router.websocket("/ws/inbox/{referral_id}")
        async def inbox_websocket_endpoint(
            websocket: WebSocket,
            referral_id: str,
            token: str
        ):
            """WebSocket endpoint for real-time inbox messaging"""
            user_id = None
            pubsub_task = None
            
            try:
                # Verify token
                token_data = self.auth.verify_token(token)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    await websocket.close(code=4001, reason="Invalid token")
                    return
                
                # Verify user has access to this referral conversation
                with self.db.get_session() as db_session:
                    user = db_session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not user:
                        await websocket.close(code=4004, reason="User not found")
                        return
                    try:
                        require_identity_verification_for_feature(db_session, user)
                    except HTTPException:
                        await websocket.close(code=4003, reason="Identity verification required")
                        return

                    referral = db_session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id)
                    ).first()
                    
                    if not referral:
                        await websocket.close(code=4004, reason="Referral not found")
                        return
                    
                    # Check if user is either the client or the assigned lawyer
                    is_client = str(referral.user_id) == user_id
                    is_lawyer = referral.matched_lawyer_id and str(referral.matched_lawyer_id) == user_id
                    
                    if not (is_client or is_lawyer):
                        await websocket.close(code=4003, reason="Access denied")
                        return
                    
                    # For clients, check if lawyer has accepted the case
                    if is_client:
                        inbox_entry = db_session.query(LawyerInbox).filter(
                            LawyerInbox.referral_request_id == referral.id
                        ).first()
                        
                        if not inbox_entry or inbox_entry.status != 'accepted':
                            await websocket.close(code=4003, reason="Conversation not available until lawyer accepts")
                            return
                
                # Connect the user
                await self.manager.connect(websocket, user_id, referral_id, "inbox")
                
                # Send initial connection message
                welcome_message = {
                    "type": "inbox_connection",
                    "message": "Connected to inbox conversation",
                    "referral_id": referral_id,
                    "user_id": user_id
                }
                await websocket.send_text(json.dumps(welcome_message))

                pubsub_task = asyncio.create_task(
                    self._forward_inbox_pubsub(websocket, referral_id, user_id)
                )
                
                # Handle incoming messages
                while True:
                    data = await websocket.receive_text()
                    await self._handle_inbox_message(data, referral_id, user_id)
                    
            except WebSocketDisconnect:
                if user_id:
                    self.manager.disconnect(user_id, referral_id, "inbox")
                    # Notify others of disconnection
                    await self.manager.broadcast_to_inbox(
                        json.dumps({
                            "type": "user_disconnected",
                            "user_id": user_id,
                            "timestamp": datetime.now().isoformat()
                        }),
                        referral_id,
                        exclude_user=user_id
                    )
            except Exception as e:
                logger.error(f"Inbox WebSocket error: {e}")
                if user_id:
                    self.manager.disconnect(user_id, referral_id, "inbox")
            finally:
                if pubsub_task:
                    pubsub_task.cancel()
                    try:
                        await pubsub_task
                    except asyncio.CancelledError:
                        pass

        @router.websocket("/ws/referral/user_status/{conversation_id}")
        async def referral_user_status_websocket(
            websocket: WebSocket,
            conversation_id: str,
            token: str,
        ):
            """
            Real-time updates for GET /referral/user_status/{conversation_id}.
            Connect with ?token=<JWT>. First message mirrors HTTP response; further updates are pushed when status changes.
            """
            user_id = None
            try:
                token_data = self.auth.verify_token(token)
                user_id = token_data.get("user_id")
                if not user_id:
                    await websocket.close(code=4001, reason="Invalid token")
                    return

                with self.db.get_session() as db_session:
                    user = db_session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not user:
                        await websocket.close(code=4004, reason="User not found")
                        return
                    try:
                        require_identity_verification_for_feature(db_session, user)
                    except HTTPException:
                        await websocket.close(code=4003, reason="Identity verification required")
                        return

                    initial = await build_referral_user_status_payload(
                        db_session, self.redis_service, user_id, conversation_id
                    )

                await websocket.accept()

                referral_user_status_hub.register(user_id, conversation_id, websocket)
                await websocket.send_text(
                    json.dumps({"type": "referral_user_status", **initial})
                )

                while True:
                    try:
                        raw = await websocket.receive_text()
                        data = json.loads(raw)
                        if data.get("type") == "sync_request":
                            with self.db.get_session() as db_session:
                                state = await build_referral_user_status_payload(
                                    db_session, self.redis_service, user_id, conversation_id
                                )
                            await websocket.send_text(
                                json.dumps({"type": "referral_user_status", **state})
                            )
                    except json.JSONDecodeError:
                        pass

            except WebSocketDisconnect:
                if user_id:
                    referral_user_status_hub.unregister(user_id, conversation_id)
            except Exception as e:
                logger.error(f"referral_user_status WebSocket error: {e}")
                if user_id:
                    referral_user_status_hub.unregister(user_id, conversation_id)
                try:
                    await websocket.close(code=1011, reason="referral_user_status websocket error")
                except Exception:
                    pass

        return router

    async def _forward_inbox_pubsub(self, websocket: WebSocket, referral_id: str, user_id: str):
        """Forward Redis pub/sub inbox events to one connected WebSocket."""
        pubsub = None
        channel = self.redis_service.inbox_events_channel(referral_id)
        try:
            pubsub = self.redis_service.redis.pubsub()
            await pubsub.subscribe(channel)
            logger.info("User %s subscribed to Redis channel %s", user_id, channel)

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue

                try:
                    envelope = json.loads(message.get("data") or "{}")
                    if envelope.get("exclude_user_id") == user_id:
                        continue

                    event = envelope.get("event")
                    if event:
                        await websocket.send_text(json.dumps(event))
                except json.JSONDecodeError:
                    logger.warning("Invalid inbox pub/sub payload on %s", channel)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Inbox pub/sub listener failed for %s: %s", channel, e)
        finally:
            if pubsub:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.close()
                except Exception as e:
                    logger.warning("Failed to close inbox pub/sub for %s: %s", channel, e)

    async def _publish_or_broadcast_inbox(
        self,
        payload: Dict,
        referral_id: str,
        exclude_user: str | None = None,
    ):
        published = await self.redis_service.publish_inbox_event(
            referral_id,
            payload,
            exclude_user_id=exclude_user,
        )
        if not published:
            await self.manager.broadcast_to_inbox(
                json.dumps(payload),
                referral_id,
                exclude_user=exclude_user,
            )
    
    async def _handle_inbox_message(self, message: str, referral_id: str, sender_id: str):
        """Handle incoming inbox messages"""
        try:
            data = json.loads(message)
            message_type = data.get("type")
            
            if message_type == "message":
                # Handle text/file message
                if contains_contact_details(data.get("content")):
                    await self.manager.send_personal_message(
                        json.dumps({
                            "type": "message_rejected",
                            "code": "contact_details_not_allowed",
                            "message": CONTACT_DETAILS_ERROR,
                        }),
                        sender_id,
                    )
                    return

                with self.db.get_session() as db_session:
                    # Determine sender type
                    referral = db_session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id)
                    ).first()
                    
                    sender_type = "user" if str(referral.user_id) == sender_id else "lawyer"
                    
                    # Create inbox message
                    inbox_message = InboxMessage(
                        referral_request_id=uuid.UUID(referral_id),
                        sender_id=uuid.UUID(sender_id),
                        sender_type=sender_type,
                        message_content=data.get("content"),
                        message_type=data.get("message_type", "text")
                    )
                    
                    db_session.add(inbox_message)
                    db_session.commit()
                    
                    # Get sender name for broadcast
                    sender = db_session.query(User).filter(User.id == uuid.UUID(sender_id)).first()
                    sender_name = f"{sender.first_name} {sender.last_name}" if sender else "Unknown"
                
                # Create real-time message
                realtime_message = {
                    "type": "new_message",
                    "message_id": str(inbox_message.id),
                    "sender_id": sender_id,
                    "sender_type": sender_type,
                    "sender_name": sender_name,
                    "content": data.get("content"),
                    "message_type": data.get("message_type", "text"),
                    "timestamp": datetime.now().isoformat()
                }
                
                # Store in Redis for history
                await self.redis_service.store_chat_message(
                    conversation_id=f"inbox:{referral_id}",
                    user_id=sender_id,
                    message=realtime_message
                )
                
                # Publish to all participants
                await self._publish_or_broadcast_inbox(realtime_message, referral_id)
            
            elif message_type == "typing":
                # Handle typing indicator
                typing_message = {
                    "type": "typing",
                    "user_id": sender_id,
                    "is_typing": data.get("is_typing", False),
                    "timestamp": datetime.now().isoformat()
                }
                
                # Publish to others
                await self._publish_or_broadcast_inbox(
                    typing_message,
                    referral_id,
                    exclude_user=sender_id,
                )
            
            elif message_type == "mark_read":
                # Mark messages as read
                message_ids = data.get("message_ids", [])
                if message_ids:
                    with self.db.get_session() as db_session:
                        db_session.query(InboxMessage).filter(
                            and_(
                                InboxMessage.id.in_([uuid.UUID(mid) for mid in message_ids]),
                                InboxMessage.referral_request_id == uuid.UUID(referral_id)
                            )
                        ).update({
                            'is_read': True,
                            'read_at': datetime.now()
                        })
                        db_session.commit()
                
                # Notify other participants
                read_message = {
                    "type": "messages_read",
                    "user_id": sender_id,
                    "message_ids": message_ids,
                    "timestamp": datetime.now().isoformat()
                }
                
                await self._publish_or_broadcast_inbox(
                    read_message,
                    referral_id,
                    exclude_user=sender_id,
                )
            
        except json.JSONDecodeError:
            logger.error(
                "Invalid JSON websocket message",
                extra={"message_length": len(message or "")},
            )
        except Exception as e:
            logger.error(f"Error handling inbox message: {e}")
    
    async def _send_session_info(self, session_id: str, user_id: str):
        """Send initial session information to connected user"""
        with self.db.get_session() as db_session:
            session_obj = db_session.query(ReferralSession).filter(
                ReferralSession.id == uuid.UUID(session_id)
            ).first()
            
            # Get user and lawyer info
            user = db_session.query(User).filter(User.id == session_obj.user_id).first()
            lawyer = db_session.query(User).filter(User.id == session_obj.lawyer_id).first()
            
            # Prepare session info
            session_info = {
                "type": "session_info",
                "session": {
                    "id": str(session_obj.id),
                    "status": session_obj.session_status,
                    "start_time": session_obj.start_time.isoformat() if session_obj.start_time else None
                },
                "participants": {
                    "user": {
                        "id": str(user.id),
                        "name": f"{user.first_name} {user.last_name}",
                        "is_you": str(user.id) == user_id
                    },
                    "lawyer": {
                        "id": str(lawyer.id),
                        "name": f"{lawyer.first_name} {lawyer.last_name}",
                        "firm": lawyer.professional_employer,
                        "is_you": str(lawyer.id) == user_id
                    }
                },
                "timestamp": datetime.now().isoformat()
            }
            
            await self.manager.send_personal_message(
                json.dumps(session_info),
                user_id
            )
    
    async def _handle_message(self, message: str, session_id: str, sender_id: str):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            message_type = data.get("type")
            
            if message_type == "chat_message":
                # Handle chat message
                if contains_contact_details(data.get("content")):
                    await self.manager.send_personal_message(
                        json.dumps({
                            "type": "message_rejected",
                            "code": "contact_details_not_allowed",
                            "message": CONTACT_DETAILS_ERROR,
                        }),
                        sender_id,
                    )
                    return

                chat_message = {
                    "type": "chat_message",
                    "sender_id": sender_id,
                    "content": data.get("content"),
                    "timestamp": datetime.now().isoformat()
                }
                
                # Store message in Redis for history
                await self._store_session_message(session_id, chat_message)
                
                # Broadcast to all participants
                await self.manager.broadcast_to_session(
                    json.dumps(chat_message),
                    session_id
                )
            
            elif message_type == "typing":
                # Handle typing indicator
                typing_message = {
                    "type": "typing",
                    "user_id": sender_id,
                    "is_typing": data.get("is_typing", False),
                    "timestamp": datetime.now().isoformat()
                }
                
                # Broadcast to others
                await self.manager.broadcast_to_session(
                    json.dumps(typing_message),
                    session_id,
                    exclude_user=sender_id
                )
            
            elif message_type == "session_end":
                # Handle session end request
                with self.db.get_session() as db_session:
                    session_obj = db_session.query(ReferralSession).filter(
                        ReferralSession.id == uuid.UUID(session_id)
                    ).first()
                    
                    if session_obj:
                        session_obj.session_status = 'completed'
                        session_obj.end_time = datetime.now()
                        db_session.commit()
                
                # Notify all participants
                end_message = {
                    "type": "session_ended",
                    "ended_by": sender_id,
                    "timestamp": datetime.now().isoformat()
                }
                
                await self.manager.broadcast_to_session(
                    json.dumps(end_message),
                    session_id
                )
            
        except json.JSONDecodeError:
            logger.error(
                "Invalid JSON websocket message",
                extra={"message_length": len(message or "")},
            )
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    async def _store_session_message(self, session_id: str, message: Dict):
        """Store session message in Redis for history"""
        key = f"referral_session_messages:{session_id}"
        await self.redis_service.redis.rpush(key, json.dumps(message))
        # Set expiration to 7 days
        await self.redis_service.redis.expire(key, 60 * 60 * 24 * 7) 
