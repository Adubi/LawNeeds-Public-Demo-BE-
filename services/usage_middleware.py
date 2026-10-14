# services/usage_middleware.py
"""
Usage Tracking Middleware
Validates and tracks usage for minute-consuming actions.

This module provides decorators and helper functions to:
- Check if user can perform an action before executing it
- Deduct minutes after action completes
- Track usage for analytics
"""

import logging
import uuid
from typing import Callable, Any, Tuple, Optional
from functools import wraps

from services.subscription_service import SubscriptionService
from services.subscription_constants import UsageActionType, BlockedReason

logger = logging.getLogger(__name__)


class UsageTracker:
    """
    Helper class to validate and track usage within a session.
    
    Usage:
        with db.get_session() as session:
            tracker = UsageTracker(session, user_id)
            
            # Check if action is allowed
            can_perform, reason, message = tracker.can_perform_action(
                UsageActionType.VOICE_NOTE,
                duration_seconds=120
            )
            
            if not can_perform:
                # Block action
                return {"error": message}
            
            # Perform the action...
            
            # Deduct usage
            tracker.deduct_usage(
                UsageActionType.VOICE_NOTE,
                duration_seconds=120,
                file_name="voice_note.wav"
            )
    """
    
    def __init__(self, session, user_id: uuid.UUID):
        self.session = session
        self.user_id = user_id
        self.service = SubscriptionService(session)
    
    def can_perform_action(
        self, 
        action_type: UsageActionType, 
        **kwargs
    ) -> Tuple[bool, Optional[BlockedReason], str]:
        """
        Check if user can perform an action.
        
        Args:
            action_type: Type of action (VOICE_NOTE, UPLOAD, DOCGEN, QUERY)
            **kwargs: Additional parameters (duration_seconds, file_size_bytes, page_count, is_image)
        
        Returns:
            Tuple of (can_perform, blocked_reason, message)
        """
        return self.service.can_perform_action(self.user_id, action_type, **kwargs)
    
    def deduct_usage(
        self, 
        action_type: UsageActionType, 
        **kwargs
    ) -> Tuple[bool, str, int]:
        """
        Deduct usage after action completes.
        
        Args:
            action_type: Type of action
            **kwargs: Additional parameters (duration_seconds, file_size_bytes, page_count, etc.)
        
        Returns:
            Tuple of (success, message, minutes_deducted)
        """
        return self.service.deduct_usage(self.user_id, action_type, **kwargs)
    
    def get_usage_summary(self) -> dict:
        """Get user's current usage summary"""
        return self.service.get_usage_summary(self.user_id)


# ============================================
# VALIDATION HELPERS
# ============================================

def validate_voice_note(
    session, 
    user_id: uuid.UUID, 
    duration_seconds: int
) -> Tuple[bool, str]:
    """
    Validate voice note recording.
    
    Args:
        session: Database session
        user_id: User ID
        duration_seconds: Voice note duration
    
    Returns:
        Tuple of (is_allowed, error_message)
    """
    tracker = UsageTracker(session, user_id)
    
    can_perform, reason, message = tracker.can_perform_action(
        UsageActionType.VOICE_NOTE,
        duration_seconds=duration_seconds
    )
    
    if not can_perform:
        logger.warning(f"Voice note blocked for user {user_id}: {reason} - {message}")
        return False, message
    
    return True, "OK"


def validate_upload(
    session,
    user_id: uuid.UUID,
    file_size_bytes: int,
    page_count: int = 0,
    is_image: bool = False
) -> Tuple[bool, str]:
    """
    Validate file upload.
    
    Args:
        session: Database session
        user_id: User ID
        file_size_bytes: File size in bytes
        page_count: Number of pages (for documents)
        is_image: Whether file is an image
    
    Returns:
        Tuple of (is_allowed, error_message)
    """
    tracker = UsageTracker(session, user_id)
    
    can_perform, reason, message = tracker.can_perform_action(
        UsageActionType.UPLOAD,
        file_size_bytes=file_size_bytes,
        page_count=page_count,
        is_image=is_image
    )
    
    if not can_perform:
        logger.warning(f"Upload blocked for user {user_id}: {reason} - {message}")
        return False, message
    
    return True, "OK"


def validate_docgen(
    session,
    user_id: uuid.UUID,
    page_count: int
) -> Tuple[bool, str]:
    """
    Validate document generation.
    
    Args:
        session: Database session
        user_id: User ID
        page_count: Number of pages to generate
    
    Returns:
        Tuple of (is_allowed, error_message)
    """
    tracker = UsageTracker(session, user_id)
    
    can_perform, reason, message = tracker.can_perform_action(
        UsageActionType.DOCGEN,
        page_count=page_count
    )
    
    if not can_perform:
        logger.warning(f"Doc generation blocked for user {user_id}: {reason} - {message}")
        return False, message
    
    return True, "OK"


def validate_query(
    session,
    user_id: uuid.UUID
) -> Tuple[bool, str]:
    """
    Validate AI query.
    
    Args:
        session: Database session
        user_id: User ID
    
    Returns:
        Tuple of (is_allowed, error_message)
    """
    tracker = UsageTracker(session, user_id)
    
    can_perform, reason, message = tracker.can_perform_action(
        UsageActionType.QUERY
    )
    
    if not can_perform:
        logger.warning(f"Query blocked for user {user_id}: {reason} - {message}")
        return False, message
    
    return True, "OK"


# ============================================
# DEDUCTION HELPERS
# ============================================

def deduct_voice_note_usage(
    session,
    user_id: uuid.UUID,
    duration_seconds: int,
    file_name: Optional[str] = None,
    referral_id: Optional[uuid.UUID] = None,
    conversation_id: Optional[str] = None
) -> Tuple[bool, int]:
    """
    Deduct usage for voice note.
    
    Returns:
        Tuple of (success, minutes_deducted)
    """
    tracker = UsageTracker(session, user_id)
    
    success, message, minutes = tracker.deduct_usage(
        UsageActionType.VOICE_NOTE,
        duration_seconds=duration_seconds,
        file_name=file_name,
        referral_id=referral_id,
        conversation_id=conversation_id,
        description=f"Voice note recorded ({duration_seconds}s)"
    )
    
    if success:
        logger.info(f"Deducted {minutes} minutes for voice note (user {user_id})")
    else:
        logger.error(f"Failed to deduct voice note usage for user {user_id}: {message}")
    
    return success, minutes


def deduct_upload_usage(
    session,
    user_id: uuid.UUID,
    file_size_bytes: int,
    file_name: Optional[str] = None,
    page_count: int = 0,
    referral_id: Optional[uuid.UUID] = None,
    conversation_id: Optional[str] = None
) -> Tuple[bool, int]:
    """
    Deduct usage for file upload.
    
    Returns:
        Tuple of (success, minutes_deducted)
    """
    tracker = UsageTracker(session, user_id)
    
    success, message, minutes = tracker.deduct_usage(
        UsageActionType.UPLOAD,
        file_size_bytes=file_size_bytes,
        file_name=file_name,
        page_count=page_count,
        referral_id=referral_id,
        conversation_id=conversation_id,
        description=f"File uploaded: {file_name}"
    )
    
    if success:
        logger.info(f"Deducted {minutes} minutes for upload (user {user_id})")
    else:
        logger.error(f"Failed to deduct upload usage for user {user_id}: {message}")
    
    return success, minutes


def deduct_docgen_usage(
    session,
    user_id: uuid.UUID,
    page_count: int,
    file_name: Optional[str] = None,
    referral_id: Optional[uuid.UUID] = None,
    conversation_id: Optional[str] = None
) -> Tuple[bool, int]:
    """
    Deduct usage for document generation.
    
    Returns:
        Tuple of (success, minutes_deducted)
    """
    tracker = UsageTracker(session, user_id)
    
    success, message, minutes = tracker.deduct_usage(
        UsageActionType.DOCGEN,
        page_count=page_count,
        file_name=file_name,
        referral_id=referral_id,
        conversation_id=conversation_id,
        description=f"AI document generated ({page_count} pages)"
    )
    
    if success:
        logger.info(f"Deducted {minutes} minutes for doc generation (user {user_id})")
    else:
        logger.error(f"Failed to deduct docgen usage for user {user_id}: {message}")
    
    return success, minutes


def deduct_query_usage(
    session,
    user_id: uuid.UUID,
    conversation_id: Optional[str] = None
) -> Tuple[bool, int]:
    """
    Deduct usage for AI query (freemium only).
    
    Returns:
        Tuple of (success, minutes_deducted)
    """
    tracker = UsageTracker(session, user_id)
    
    success, message, minutes = tracker.deduct_usage(
        UsageActionType.QUERY,
        conversation_id=conversation_id,
        description="AI query executed"
    )
    
    if success:
        logger.info(f"Recorded query usage for user {user_id}")
    else:
        logger.error(f"Failed to record query usage for user {user_id}: {message}")
    
    return success, minutes


# ============================================
# USAGE ALERTS
# ============================================

def check_usage_alerts(session, user_id: uuid.UUID) -> Optional[dict]:
    """
    Check if user should receive usage alerts.
    
    Returns:
        Alert dict if alert should be shown, None otherwise
    """
    from services.subscription_constants import USAGE_ALERT_THRESHOLD_PERCENT, TOP_UP_SUGGESTION_COUNT
    
    tracker = UsageTracker(session, user_id)
    summary = tracker.get_usage_summary()
    
    alerts = []
    
    # Check 85% usage threshold
    usage_percent = summary['minutes']['usage_percent']
    if usage_percent >= USAGE_ALERT_THRESHOLD_PERCENT:
        alerts.append({
            'type': 'high_usage',
            'severity': 'warning',
            'message': f"You've used {usage_percent:.0f}% of your minutes. Consider upgrading.",
            'action': 'upgrade'
        })
    
    # Check top-up suggestions
    minutes_account = tracker.service._create_minutes_account(user_id)
    if minutes_account.top_ups_this_month >= TOP_UP_SUGGESTION_COUNT:
        alerts.append({
            'type': 'frequent_topups',
            'severity': 'info',
            'message': f"You've topped up {minutes_account.top_ups_this_month} times this month. A higher plan may suit you better.",
            'action': 'suggest_upgrade'
        })
    
    return {'alerts': alerts} if alerts else None


# ============================================
# EXAMPLE INTEGRATION
# ============================================

"""
Example of how to integrate into existing endpoints:

# In api/referral.py or api/chat.py:

from services.usage_middleware import (
    validate_voice_note, deduct_voice_note_usage,
    validate_upload, deduct_upload_usage,
    validate_query, deduct_query_usage,
    check_usage_alerts
)

@router.post("/referral/record_voice_note")
async def record_voice_note(request: Request, ...):
    token_data = auth.verify_request_token(request)
    user_id = token_data.get("user_id")
    
    with db.get_session() as session:
        # VALIDATE BEFORE PROCESSING
        is_allowed, error_message = validate_voice_note(
            session, 
            uuid.UUID(user_id), 
            duration_seconds=voice_duration
        )
        
        if not is_allowed:
            # Show upgrade prompt
            return JSONResponse(
                status_code=403,
                content={
                    "error": error_message,
                    "blocked": True,
                    "action": "upgrade_required"
                }
            )
        
        # Process voice note...
        # ... (existing logic) ...
        
        # DEDUCT USAGE AFTER SUCCESS
        deduct_voice_note_usage(
            session,
            uuid.UUID(user_id),
            duration_seconds=voice_duration,
            file_name="voice_note.wav",
            referral_id=referral_id
        )
        
        # Check for alerts
        alerts = check_usage_alerts(session, uuid.UUID(user_id))
        
        return {
            "success": True,
            "voice_note_url": url,
            "alerts": alerts  # Include in response
        }
"""

