from fastapi import HTTPException, APIRouter, Request, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import logging
import uuid
from datetime import datetime
from services.models import ChatResponseFeedback, FeedbackType, ThumbsDownReason
from services.redis_service import RedisService
from sqlalchemy.orm import Session

# Set up logging
logger = logging.getLogger(__name__)

# Helper functions
def get_feedback_type_display(feedback_type: str) -> str:
    """Convert feedback type enum key to display value"""
    feedback_type_mapping = {
        "thumbs_up": "Thumbs Up",
        "thumbs_down": "Thumbs Down"
    }
    return feedback_type_mapping.get(feedback_type, feedback_type)

def get_thumbs_down_reason_display(thumbs_down_reason: str) -> str:
    """Convert thumbs down reason enum key to display value"""
    thumbs_down_reason_mapping = {
        "incorrect_info": "Incorrect Information",
        "not_helpful": "Not Helpful",
        "too_generic": "Too Generic",
        "confusing": "Confusing",
        "other": "Other"
    }
    return thumbs_down_reason_mapping.get(thumbs_down_reason, thumbs_down_reason)

# Pydantic models for request/response
class SubmitFeedbackRequest(BaseModel):
    conversation_id: str = Field(..., description="ID of the conversation")
    message_index: int = Field(..., description="Index of the message in conversation (0-based)")
    feedback_type: str = Field(..., description="Either 'thumbs_up' or 'thumbs_down'")
    thumbs_down_reason: Optional[str] = Field(None, description="Reason for thumbs down")
    response_content: str = Field(..., description="The actual response content that was rated")
    user_question: str = Field(..., description="The user question that led to this response")

class FeedbackResponse(BaseModel):
    id: str
    message: str
    created_at: str

class ConversationFeedbackResponse(BaseModel):
    conversation_id: str
    feedback_count: int
    feedback_items: List[Dict[str, Any]]

class ChatFeedbackAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.redis_service = RedisService()
        
        # Track deprecation warnings
        self.deprecation_warnings_logged = set()

    def _log_deprecation_warning(self, endpoint_name: str, user_id: str = None):
        """Log deprecation warning only once per endpoint to avoid spam"""
        key = f"{endpoint_name}:{user_id}" if user_id else endpoint_name
        if key not in self.deprecation_warnings_logged:
            logger.warning(f"DEPRECATED ENDPOINT USED: {endpoint_name} by user {user_id}")
            self.deprecation_warnings_logged.add(key)

    def get_router(self):
        router = APIRouter()

        @router.post("/chat_feedback/submit", response_model=FeedbackResponse)
        async def submit_chat_feedback(
            request: Request,
            feedback_data: SubmitFeedbackRequest
        ):
            """
            Submit feedback for a chatbot response.
            
            This endpoint accepts:
            - conversation_id: ID of the conversation
            - message_index: Position of the response in the conversation
            - feedback_type: Either 'thumbs_up' or 'thumbs_down'
            - thumbs_down_reason: Required if feedback_type is 'thumbs_down'
            - response_content: The actual response that was rated
            - user_question: The user question that led to this response
            """
            try:
                # Verify user token and get user_id
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                logger.info(f"Received feedback submission from user {user_id} for conversation {feedback_data.conversation_id}")

                # Validate feedback type
                if feedback_data.feedback_type not in [FeedbackType.THUMBS_UP.value, FeedbackType.THUMBS_DOWN.value]:
                    raise HTTPException(
                        status_code=400, 
                        detail="feedback_type must be either 'thumbs_up' or 'thumbs_down'"
                    )

                # Validate thumbs down reason if feedback is negative
                if feedback_data.feedback_type == FeedbackType.THUMBS_DOWN.value:
                    if not feedback_data.thumbs_down_reason:
                        raise HTTPException(
                            status_code=400,
                            detail="thumbs_down_reason is required when feedback_type is 'thumbs_down'"
                        )
                    
                    # Validate reason is one of the allowed values
                    valid_reasons = [reason.value for reason in ThumbsDownReason]
                    if feedback_data.thumbs_down_reason not in valid_reasons:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid thumbs_down_reason. Must be one of: {', '.join(valid_reasons)}"
                        )

                # Create feedback record in PostgreSQL
                with self.db.get_session() as session:
                    # Verify user exists before creating feedback
                    from services.models import User
                    user_exists = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                    if not user_exists:
                        logger.error(f"User {user_id} not found in database")
                        raise HTTPException(status_code=404, detail="User not found")
                    
                    # Check if feedback already exists for this message
                    existing_feedback = session.query(ChatResponseFeedback).filter(
                        ChatResponseFeedback.user_id == uuid.UUID(user_id),
                        ChatResponseFeedback.conversation_id == feedback_data.conversation_id,
                        ChatResponseFeedback.message_index == feedback_data.message_index
                    ).first()

                    if existing_feedback:
                        # Update existing feedback
                        existing_feedback.feedback_type = feedback_data.feedback_type
                        existing_feedback.thumbs_down_reason = feedback_data.thumbs_down_reason
                        existing_feedback.updated_at = datetime.utcnow()
                        feedback_id = str(existing_feedback.id)
                        logger.info(f"Updated existing feedback with ID: {feedback_id}")
                    else:
                        # Create new feedback
                        feedback = ChatResponseFeedback(
                            user_id=uuid.UUID(user_id),
                            conversation_id=feedback_data.conversation_id,
                            message_index=feedback_data.message_index,
                            response_content=feedback_data.response_content,
                            user_question=feedback_data.user_question,
                            feedback_type=feedback_data.feedback_type,
                            thumbs_down_reason=feedback_data.thumbs_down_reason
                        )
                        
                        session.add(feedback)
                        session.flush()  # Get the ID
                        feedback_id = str(feedback.id)
                        logger.info(f"Created new feedback with ID: {feedback_id}")

                    session.commit()

                # Also store in Redis for quick access
                redis_feedback_data = {
                    'conversation_id': feedback_data.conversation_id,
                    'message_index': feedback_data.message_index,
                    'feedback_type': feedback_data.feedback_type,
                    'thumbs_down_reason': feedback_data.thumbs_down_reason,
                    'feedback_id': feedback_id
                }

                await self.redis_service.store_feedback(
                    conversation_id=feedback_data.conversation_id,
                    user_id=user_id,
                    feedback_data=redis_feedback_data
                )

                return FeedbackResponse(
                    id=feedback_id,
                    message="Feedback submitted successfully",
                    created_at=datetime.utcnow().isoformat()
                )

            except HTTPException:
                # Re-raise HTTP exceptions
                raise
            except Exception as e:
                logger.error(f"Error in submit_chat_feedback: {str(e)}")
                raise HTTPException(status_code=500, detail="Internal server error")

        # DEPRECATED ENDPOINT
        @router.get(
            "/chat_feedback/conversation/{conversation_id}",
            deprecated=True,
            summary="[DEPRECATED] Get conversation feedback from Redis",
            description="""
            ⚠️ **DEPRECATED**: This endpoint is deprecated and will be removed in v2.0.0.
            
            **Migration Path:**
            - Use `GET /conversations/{conversation_id}/feedback` instead
            - The new endpoint provides better integration with persistent conversations
            
            **Removal Date:**  2026
            """,
            tags=["Chat Feedback (Deprecated)"]
        )
        async def get_conversation_feedback_deprecated(
            request: Request,
            conversation_id: str
        ):
            """
            ⚠️ DEPRECATED - Use GET /conversations/{conversation_id}/feedback instead
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                # Log deprecation usage
                self._log_deprecation_warning("chat_feedback_conversation_redis", user_id)

                # First try to get from Redis (faster)
                redis_feedback = await self.redis_service.get_conversation_feedback(conversation_id)
                
                # Also get from PostgreSQL for persistence
                db_feedback_list = []
                with self.db.get_session() as session:
                    from services.models import ChatResponseFeedback
                    db_feedback = session.query(ChatResponseFeedback).filter(
                        ChatResponseFeedback.conversation_id == conversation_id,
                        ChatResponseFeedback.user_id == uuid.UUID(user_id)
                    ).order_by(ChatResponseFeedback.message_index).all()

                    for feedback in db_feedback:
                        db_feedback_list.append({
                            'feedback_id': str(feedback.id),
                            'message_index': feedback.message_index,
                            'feedback_type': feedback.feedback_type,
                            'thumbs_down_reason': feedback.thumbs_down_reason,
                            'created_at': feedback.created_at.isoformat(),
                            'updated_at': feedback.updated_at.isoformat()
                        })

                # Combine and deduplicate (prefer DB data for accuracy)
                all_feedback = {}
                
                # Add Redis data first
                if redis_feedback:
                    for feedback in redis_feedback:
                        all_feedback[feedback.get('message_index')] = feedback

                # Override with DB data (more authoritative)
                for feedback in db_feedback_list:
                    all_feedback[feedback['message_index']] = feedback

                response_data = {
                    'conversation_id': conversation_id,
                    'feedback': list(all_feedback.values()),
                    'total_feedback_count': len(all_feedback),
                    '_deprecation_warning': {
                        'message': 'This endpoint is deprecated and will be removed in v2.0.0',
                        'removal_date': '2025-09-05',
                        'replacement_endpoint': f'/conversations/{conversation_id}/feedback'                  
                    }
                }

                # Create response with deprecation headers
                response = JSONResponse(content=response_data)
                response.headers["X-Deprecated"] = "true"
                response.headers["X-Deprecation-Date"] = "2025-09-05"
                response.headers["X-Replacement-Endpoint"] = f"/conversations/{conversation_id}/feedback"
                response.headers["Sunset"] = "Wed, 01 Mar 2024 00:00:00 GMT"
                
                return response

            except Exception as e:
                logger.error(f"Error in deprecated conversation feedback endpoint: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # NEW ENDPOINT (replacement)
        @router.get(
            "/conversations/{conversation_id}/feedback",
            summary="Get conversation feedback with persistence",
            description="Get all feedback for a specific conversation with persistent storage",
            tags=["Conversations"]
        )
        async def get_conversation_feedback_persistent(
            conversation_id: str,
            request: Request
        ):
            """Get feedback for a conversation with persistent storage"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                feedback_list = []
                with self.db.get_session() as session:
                    from services.models import ChatResponseFeedback
                    db_feedback = session.query(ChatResponseFeedback).filter(
                        ChatResponseFeedback.conversation_id == conversation_id,
                        ChatResponseFeedback.user_id == uuid.UUID(user_id)
                    ).order_by(ChatResponseFeedback.message_index).all()

                    for feedback in db_feedback:
                        feedback_list.append({
                            'feedback_id': str(feedback.id),
                            'message_index': feedback.message_index,
                            'feedback_type': feedback.feedback_type,
                            'thumbs_down_reason': feedback.thumbs_down_reason,
                            'created_at': feedback.created_at.isoformat(),
                            'updated_at': feedback.updated_at.isoformat()
                        })

                return JSONResponse(content={
                    'conversation_id': conversation_id,
                    'feedback': feedback_list,
                    'total_feedback_count': len(feedback_list)
                })

            except Exception as e:
                logger.error(f"Error getting conversation feedback: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.get("/chat_feedback/reasons")
        async def get_thumbs_down_reasons():
            """
            Get available thumbs down reasons with both values and display labels.
            """
            try:
                reasons = [
                    {
                        "value": reason.value, 
                        "label": get_thumbs_down_reason_display(reason.value)
                    } 
                    for reason in ThumbsDownReason
                ]
                return {"reasons": reasons}
            except Exception as e:
                logger.error(f"Error getting thumbs down reasons: {str(e)}")
                raise HTTPException(status_code=500, detail="Internal server error")

        @router.delete("/chat_feedback/{feedback_id}")
        async def delete_feedback(
            request: Request,
            feedback_id: str
        ):
            """
            Delete a specific feedback entry.
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                with self.db.get_session() as session:
                    feedback = session.query(ChatResponseFeedback).filter(
                        ChatResponseFeedback.id == uuid.UUID(feedback_id),
                        ChatResponseFeedback.user_id == uuid.UUID(user_id)
                    ).first()

                    if not feedback:
                        raise HTTPException(status_code=404, detail="Feedback not found")

                    # Store info for Redis cleanup
                    conversation_id = feedback.conversation_id
                    message_index = feedback.message_index

                    # Delete from database
                    session.delete(feedback)
                    session.commit()

                    # Delete from Redis
                    await self.redis_service.delete_feedback(conversation_id, message_index)

                    return {"message": "Feedback deleted successfully"}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error deleting feedback: {str(e)}")
                raise HTTPException(status_code=500, detail="Internal server error")

        return router 