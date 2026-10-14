"""
Shared payload builder for GET /referral/user_status and WebSocket pushes.
"""
import json
import logging
import uuid
from typing import Any, Dict

from services.models import ReferralRequest, User
from services.referral_display_helpers import get_budget_range_display

logger = logging.getLogger(__name__)


async def build_referral_user_status_payload(
    session,
    redis_service,
    user_id: str,
    conversation_id: str,
) -> Dict[str, Any]:
    """Build the same JSON shape as GET /referral/user_status/{conversation_id}."""
    referral = (
        session.query(ReferralRequest)
        .filter(
            ReferralRequest.conversation_id == conversation_id,
            ReferralRequest.user_id == uuid.UUID(user_id),
        )
        .order_by(ReferralRequest.created_at.desc())
        .first()
    )

    if not referral:
        return {"status": "not_found", "message": "No referral request found"}

    notification_key = f"referral_notification:{user_id}"
    notification_data = await redis_service.get_key(notification_key)
    notification = json.loads(notification_data) if notification_data else None

    lawyer_info = None
    if referral.matched_lawyer_id:
        lawyer = session.query(User).filter(User.id == referral.matched_lawyer_id).first()
        if lawyer:
            lawyer_info = {
                "name": f"{lawyer.first_name} {lawyer.last_name}",
                "firm": lawyer.professional_employer,
                "expertise": lawyer.legal_expertise,
            }

    return {
        "referral_id": str(referral.id),
        "status": referral.status,
        "created_at": referral.created_at.isoformat(),
        "lawyer_info": lawyer_info,
        "notification": notification,
        "consultation_summary": {
            "legal_area": referral.legal_area,
            "consultation_category": referral.consultation_category,
            "consultation_type": referral.consultation_type,
            "budget_range": get_budget_range_display(referral.budget_range),
        },
    }
