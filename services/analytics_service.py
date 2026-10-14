# services/analytics_service.py
"""
LawNeeds Analytics Service

Provides analytics data for:
- Admin dashboard (subscription metrics, revenue, usage patterns)
- User analytics (personal usage trends)

Designed to be minimal and extensible for future enhancements.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from decimal import Decimal

from sqlalchemy import func, and_, or_, case, extract
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class AnalyticsService:
    """
    Analytics service for subscription and usage data.
    
    Admin methods are prefixed with 'admin_' for clarity.
    User methods are prefixed with 'user_' for clarity.
    """
    
    def __init__(self, db_session: Session):
        self.session = db_session
    
    # ============================================
    # ADMIN ANALYTICS (Minimal/Extensible)
    # ============================================
    
    def admin_get_subscription_overview(self) -> Dict[str, Any]:
        """
        Get high-level subscription metrics for admin dashboard.
        
        Returns:
            - Total users by tier
            - Active subscriptions count
            - Subscription status breakdown
        """
        from services.models import UserSubscription, User
        from services.subscription_constants import SubscriptionTier, SubscriptionStatus
        
        try:
            # Total users
            total_users = self.session.query(func.count(User.id)).scalar() or 0
            
            # Users by tier
            tier_counts = self.session.query(
                UserSubscription.tier,
                func.count(UserSubscription.id)
            ).group_by(UserSubscription.tier).all()
            
            users_by_tier = {
                SubscriptionTier.FREEMIUM.value: 0,
                SubscriptionTier.STANDARD.value: 0,
                SubscriptionTier.PREMIUM.value: 0,
            }
            for tier, count in tier_counts:
                if tier in users_by_tier:
                    users_by_tier[tier] = count
            
            # Users without subscription record (default to freemium)
            users_with_sub = sum(users_by_tier.values())
            users_by_tier[SubscriptionTier.FREEMIUM.value] += (total_users - users_with_sub)
            
            # Subscriptions by status
            status_counts = self.session.query(
                UserSubscription.status,
                func.count(UserSubscription.id)
            ).group_by(UserSubscription.status).all()
            
            subscriptions_by_status = {status: count for status, count in status_counts}
            
            # Active paid subscriptions
            active_paid = self.session.query(func.count(UserSubscription.id)).filter(
                and_(
                    UserSubscription.tier != SubscriptionTier.FREEMIUM.value,
                    UserSubscription.status == SubscriptionStatus.ACTIVE.value
                )
            ).scalar() or 0
            
            return {
                "total_users": total_users,
                "users_by_tier": users_by_tier,
                "active_paid_subscriptions": active_paid,
                "subscriptions_by_status": subscriptions_by_status,
                "generated_at": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting subscription overview: {e}")
            raise
    
    def admin_get_usage_metrics(self, days: int = 30) -> Dict[str, Any]:
        """
        Get usage metrics for the last N days.
        
        Args:
            days: Number of days to look back (default 30)
        
        Returns:
            - Total actions by type
            - Minutes consumed
            - Top-up statistics
        """
        from services.models import UsageLog, TopUpTransaction
        from services.subscription_constants import UsageActionType, PaymentStatus
        
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            # Actions by type
            action_counts = self.session.query(
                UsageLog.action_type,
                func.count(UsageLog.id)
            ).filter(
                UsageLog.created_at >= cutoff_date
            ).group_by(UsageLog.action_type).all()
            
            actions_by_type = {action: count for action, count in action_counts}
            
            # Total minutes consumed
            total_minutes = self.session.query(
                func.sum(UsageLog.minutes_deducted)
            ).filter(
                and_(
                    UsageLog.created_at >= cutoff_date,
                    UsageLog.status == 'success'
                )
            ).scalar() or 0
            
            # Top-up statistics
            topups = self.session.query(
                func.count(TopUpTransaction.id),
                func.sum(TopUpTransaction.minutes_purchased),
                func.sum(TopUpTransaction.amount_gbp)
            ).filter(
                and_(
                    TopUpTransaction.created_at >= cutoff_date,
                    TopUpTransaction.payment_status == PaymentStatus.SUCCEEDED.value
                )
            ).first()
            
            topup_count = topups[0] or 0
            topup_minutes = topups[1] or 0
            topup_revenue = float(topups[2]) if topups[2] else 0.0
            
            return {
                "period_days": days,
                "period_start": cutoff_date.isoformat(),
                "actions_by_type": actions_by_type,
                "total_minutes_consumed": total_minutes,
                "topups": {
                    "count": topup_count,
                    "total_minutes": topup_minutes,
                    "total_revenue_gbp": topup_revenue
                },
                "generated_at": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting usage metrics: {e}")
            raise
    
    def admin_get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get recent subscription events for admin monitoring.
        
        Args:
            limit: Maximum number of events to return
        
        Returns:
            List of recent subscription events
        """
        from services.models import SubscriptionEvent, User
        
        try:
            events = self.session.query(
                SubscriptionEvent,
                User.email
            ).join(
                User, SubscriptionEvent.user_id == User.id
            ).order_by(
                SubscriptionEvent.created_at.desc()
            ).limit(limit).all()
            
            return [{
                "id": str(event.id),
                "user_id": str(event.user_id),
                "user_email": email,
                "event_type": event.event_type,
                "old_plan": event.old_plan_id,
                "new_plan": event.new_plan_id,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            } for event, email in events]
            
        except Exception as e:
            logger.error(f"Error getting recent events: {e}")
            raise
    
    # ============================================
    # USER ANALYTICS
    # ============================================
    
    def user_get_usage_trends(
        self, 
        user_id: uuid.UUID, 
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Get usage trends for a specific user.
        
        Args:
            user_id: User's UUID
            days: Number of days to analyze (default 30)
        
        Returns:
            - Daily usage breakdown
            - Usage by action type
            - Usage comparison with previous period
        """
        from services.models import UsageLog
        
        try:
            now = datetime.now(timezone.utc)
            period_start = now - timedelta(days=days)
            prev_period_start = period_start - timedelta(days=days)
            
            # Current period usage by day
            daily_usage = self.session.query(
                func.date(UsageLog.created_at).label('date'),
                func.count(UsageLog.id).label('action_count'),
                func.sum(UsageLog.minutes_deducted).label('minutes_used')
            ).filter(
                and_(
                    UsageLog.user_id == user_id,
                    UsageLog.created_at >= period_start,
                    UsageLog.status == 'success'
                )
            ).group_by(
                func.date(UsageLog.created_at)
            ).order_by(
                func.date(UsageLog.created_at)
            ).all()
            
            daily_breakdown = [{
                "date": str(row.date),
                "actions": row.action_count,
                "minutes": row.minutes_used or 0
            } for row in daily_usage]
            
            # Current period by action type
            current_by_type = self.session.query(
                UsageLog.action_type,
                func.count(UsageLog.id),
                func.sum(UsageLog.minutes_deducted)
            ).filter(
                and_(
                    UsageLog.user_id == user_id,
                    UsageLog.created_at >= period_start,
                    UsageLog.status == 'success'
                )
            ).group_by(UsageLog.action_type).all()
            
            usage_by_type = {
                row[0]: {"count": row[1], "minutes": row[2] or 0}
                for row in current_by_type
            }
            
            # Previous period totals for comparison
            prev_totals = self.session.query(
                func.count(UsageLog.id),
                func.sum(UsageLog.minutes_deducted)
            ).filter(
                and_(
                    UsageLog.user_id == user_id,
                    UsageLog.created_at >= prev_period_start,
                    UsageLog.created_at < period_start,
                    UsageLog.status == 'success'
                )
            ).first()
            
            current_totals = self.session.query(
                func.count(UsageLog.id),
                func.sum(UsageLog.minutes_deducted)
            ).filter(
                and_(
                    UsageLog.user_id == user_id,
                    UsageLog.created_at >= period_start,
                    UsageLog.status == 'success'
                )
            ).first()
            
            prev_actions = prev_totals[0] or 0
            prev_minutes = prev_totals[1] or 0
            curr_actions = current_totals[0] or 0
            curr_minutes = current_totals[1] or 0
            
            # Calculate change percentages
            action_change = None
            minutes_change = None
            
            if prev_actions > 0:
                action_change = round(((curr_actions - prev_actions) / prev_actions) * 100, 1)
            if prev_minutes > 0:
                minutes_change = round(((curr_minutes - prev_minutes) / prev_minutes) * 100, 1)
            
            return {
                "period_days": days,
                "period_start": period_start.isoformat(),
                "period_end": now.isoformat(),
                "daily_breakdown": daily_breakdown,
                "usage_by_type": usage_by_type,
                "totals": {
                    "actions": curr_actions,
                    "minutes": curr_minutes
                },
                "comparison": {
                    "previous_period": {
                        "actions": prev_actions,
                        "minutes": prev_minutes
                    },
                    "change_percent": {
                        "actions": action_change,
                        "minutes": minutes_change
                    }
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting user usage trends: {e}")
            raise
    
    def user_get_spending_summary(
        self, 
        user_id: uuid.UUID
    ) -> Dict[str, Any]:
        """
        Get spending summary for a user (top-ups, subscriptions).
        
        Args:
            user_id: User's UUID
        
        Returns:
            - Total spent on top-ups
            - Top-up history summary
        """
        from services.models import TopUpTransaction, UserSubscription
        from services.subscription_constants import PaymentStatus
        
        try:
            # Top-up summary
            topups = self.session.query(
                func.count(TopUpTransaction.id),
                func.sum(TopUpTransaction.minutes_purchased),
                func.sum(TopUpTransaction.amount_gbp)
            ).filter(
                and_(
                    TopUpTransaction.user_id == user_id,
                    TopUpTransaction.payment_status == PaymentStatus.SUCCEEDED.value
                )
            ).first()
            
            # Last 3 months top-ups
            three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
            recent_topups = self.session.query(
                func.count(TopUpTransaction.id),
                func.sum(TopUpTransaction.amount_gbp)
            ).filter(
                and_(
                    TopUpTransaction.user_id == user_id,
                    TopUpTransaction.payment_status == PaymentStatus.SUCCEEDED.value,
                    TopUpTransaction.created_at >= three_months_ago
                )
            ).first()
            
            return {
                "topups": {
                    "total_count": topups[0] or 0,
                    "total_minutes": topups[1] or 0,
                    "total_spent_gbp": float(topups[2]) if topups[2] else 0.0,
                    "last_90_days": {
                        "count": recent_topups[0] or 0,
                        "spent_gbp": float(recent_topups[1]) if recent_topups[1] else 0.0
                    }
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting user spending summary: {e}")
            raise
