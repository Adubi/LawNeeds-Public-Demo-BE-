# services/subscription_service.py
"""
LawNeeds Subscription Service
Handles subscription management, minutes tracking, and usage control.

Based on: LAWNEEDS MINUTES-BASED SYSTEM specification
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from services.models import (
    User, SubscriptionPlan, UserSubscription, MinutesAccount,
    UsageTracking, FreemiumLimits, UsageLog, TopUpTransaction,
    AnnualConsultationCredit, SubscriptionEvent, DeviceToken
)
from services.subscription_constants import (
    SubscriptionTier, BillingPeriod, SubscriptionStatus,
    UsageActionType, BlockedReason, PaymentStatus,
    SUBSCRIPTION_PLANS, FREEMIUM_QUERIES_PER_WINDOW, FREEMIUM_WINDOW_HOURS,
    FREEMIUM_DAILY_PASS_PRICE_GBP, FREEMIUM_AD_BONUS_QUERIES,
    FREEMIUM_TRIAL_VOICE_NOTES, FREEMIUM_TRIAL_UPLOADS, FREEMIUM_TRIAL_DOCGEN,
    FREEMIUM_TRIAL_DURATION_DAYS, TOP_UP_MIN_MINUTES, TOP_UP_MAX_MINUTES,
    USAGE_ALERT_THRESHOLD_PERCENT, TOP_UP_SUGGESTION_COUNT,
    FIRST_MONTH_DISCOUNT_PERCENT, UPLOAD_FLAT_MINUTES,
    calculate_voice_note_minutes, get_docgen_minutes, calculate_topup_price,
    get_block_message, is_unlimited,
    REVENUECAT_SUBSCRIPTION_PRODUCTS,
    REVENUECAT_TOP_UP_PRODUCTS, REVENUECAT_DAILY_PASS_PRODUCT_ID,
    REVENUECAT_DAILY_PASS_PRODUCT_IDS
)

logger = logging.getLogger(__name__)


class SubscriptionService:
    """
    Service class for managing subscriptions and usage.
    
    Provides methods for:
    - Creating and managing subscriptions
    - Tracking and deducting minutes
    - Enforcing usage limits
    - Managing freemium restrictions
    - Processing top-ups
    """
    
    def __init__(self, db_session: Session):
        self.session = db_session

    @staticmethod
    def serialize_utc_datetime(value: Optional[datetime]) -> Optional[str]:
        """Return an ISO-8601 UTC timestamp with an explicit timezone marker."""
        if value is None:
            return None

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)

        return value.isoformat().replace("+00:00", "Z")

    def _is_lawyer_account(self, user_id: uuid.UUID) -> bool:
        """Return True for lawyer accounts, which are exempt from user free-tier caps."""
        user = self.session.query(User).filter(User.id == user_id).first()
        return bool(user and user.professional_type == "Lawyer")
    
    # ============================================
    # SUBSCRIPTION MANAGEMENT
    # ============================================
    
    def get_or_create_subscription(self, user_id: uuid.UUID) -> UserSubscription:
        """
        Get existing subscription or create freemium subscription for user.
        Every user should have exactly one subscription record.
        """
        # First verify user exists
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"User {user_id} does not exist")
        
        subscription = self.session.query(UserSubscription).filter(
            UserSubscription.user_id == user_id
        ).first()
        
        if not subscription:
            subscription = self._create_freemium_subscription(user_id)
        
        return subscription
    
    def _create_freemium_subscription(self, user_id: uuid.UUID) -> UserSubscription:
        """Create a new freemium subscription for a user"""
        now = datetime.now(timezone.utc)
        
        subscription = UserSubscription(
            user_id=user_id,
            plan_id='freemium',
            tier=SubscriptionTier.FREEMIUM.value,
            billing_period=None,
            started_at=now,
            current_period_start=now,
            current_period_end=None,  # Freemium never expires
            status=SubscriptionStatus.ACTIVE.value,
            is_first_month=True,
        )
        
        self.session.add(subscription)
        
        # Also create minutes account and freemium limits
        self._create_minutes_account(user_id)
        self._create_freemium_limits(user_id)
        
        # Log event
        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='created',
            new_plan_id='freemium'
        )
        
        self.session.commit()
        return subscription
    
    def _create_minutes_account(self, user_id: uuid.UUID) -> MinutesAccount:
        """Create minutes account for user"""
        account = self.session.query(MinutesAccount).filter(
            MinutesAccount.user_id == user_id
        ).first()
        
        if not account:
            now = datetime.now(timezone.utc)
            account = MinutesAccount(
                user_id=user_id,
                total_minutes=0,
                used_minutes=0,
                rollover_minutes=0,
                top_up_minutes=0,
                period_start=now,
                period_end=None,
            )
            self.session.add(account)
        
        return account
    
    def _create_freemium_limits(self, user_id: uuid.UUID) -> FreemiumLimits:
        """Create freemium limits for user"""
        limits = self.session.query(FreemiumLimits).filter(
            FreemiumLimits.user_id == user_id
        ).first()
        
        if not limits:
            now = datetime.now(timezone.utc)
            trial_expires = now + timedelta(days=FREEMIUM_TRIAL_DURATION_DAYS)
            
            now_naive = now.replace(tzinfo=None)
            limits = FreemiumLimits(
                user_id=user_id,
                query_count_current_window=0,
                window_start=now_naive,
                window_end=now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS),
                trial_started_at=now_naive,
                trial_expires_at=trial_expires.replace(tzinfo=None),
                trial_expired=False,
            )
            self.session.add(limits)
        
        return limits
    
    def upgrade_subscription(
        self, 
        user_id: uuid.UUID, 
        new_plan_id: str,
        stripe_subscription_id: Optional[str] = None,
        stripe_customer_id: Optional[str] = None,
        revenuecat_app_user_id: Optional[str] = None,
        revenuecat_product_id: Optional[str] = None,
        revenuecat_entitlement_id: Optional[str] = None,
        revenuecat_original_transaction_id: Optional[str] = None,
        current_period_end: Optional[datetime] = None,
        revenuecat_event_id: Optional[str] = None,
        event_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Optional[UserSubscription]]:
        """
        Upgrade user to a new subscription plan.
        
        Returns:
            Tuple of (success, message, subscription)
        """
        # Validate plan exists
        plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == new_plan_id,
            SubscriptionPlan.is_active == True
        ).first()
        
        if not plan:
            return False, f"Plan '{new_plan_id}' not found or inactive", None
        
        # Get or create subscription
        subscription = self.get_or_create_subscription(user_id)
        old_plan_id = subscription.plan_id
        
        now = datetime.now(timezone.utc)
        
        # Calculate period end based on billing period (override if provided)
        period_end = None
        if plan.billing_period == BillingPeriod.WEEKLY.value:
            period_end = now + timedelta(weeks=1)
        elif plan.billing_period == BillingPeriod.MONTHLY.value:
            period_end = now + timedelta(days=30)
        elif plan.billing_period == BillingPeriod.ANNUAL.value:
            period_end = now + timedelta(days=365)
        if current_period_end:
            if current_period_end.tzinfo is None:
                current_period_end = current_period_end.replace(tzinfo=timezone.utc)
            period_end = current_period_end
        
        # Update subscription
        subscription.plan_id = new_plan_id
        subscription.tier = plan.tier
        subscription.billing_period = plan.billing_period
        subscription.current_period_start = now
        subscription.current_period_end = period_end
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.stripe_subscription_id = stripe_subscription_id
        subscription.stripe_customer_id = stripe_customer_id
        subscription.revenuecat_app_user_id = revenuecat_app_user_id or subscription.revenuecat_app_user_id
        subscription.revenuecat_product_id = revenuecat_product_id or subscription.revenuecat_product_id
        subscription.revenuecat_entitlement_id = revenuecat_entitlement_id or subscription.revenuecat_entitlement_id
        subscription.revenuecat_original_transaction_id = (
            revenuecat_original_transaction_id or subscription.revenuecat_original_transaction_id
        )
        
        # Apply first month discount if applicable
        if subscription.is_first_month and plan.billing_period == BillingPeriod.MONTHLY.value:
            subscription.first_month_discount_used = True
        
        subscription.is_first_month = False
        
        # Update minutes account
        self._update_minutes_for_plan(user_id, plan)
        
        # Reset monthly usage counters on upgrade (fresh start with new plan)
        if old_plan_id != new_plan_id:
            self._reset_monthly_usage_on_upgrade(user_id)
        
        # Create annual consultation credit if applicable
        if plan.free_consultation_minutes:
            self._create_consultation_credit(
                user_id=user_id,
                subscription_id=subscription.id,
                minutes=plan.free_consultation_minutes,
                valid_until=period_end
            )
        
        # Log event
        metadata = event_metadata.copy() if event_metadata else {}
        if stripe_subscription_id and "stripe_subscription_id" not in metadata:
            metadata["stripe_subscription_id"] = stripe_subscription_id
        if revenuecat_product_id and "revenuecat_product_id" not in metadata:
            metadata["revenuecat_product_id"] = revenuecat_product_id
        if revenuecat_entitlement_id and "revenuecat_entitlement_id" not in metadata:
            metadata["revenuecat_entitlement_id"] = revenuecat_entitlement_id

        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='upgraded',
            old_plan_id=old_plan_id,
            new_plan_id=new_plan_id,
            revenuecat_event_id=revenuecat_event_id,
            event_metadata=metadata if metadata else None
        )
        
        self.session.commit()
        
        # Refresh to ensure subsequent queries get fresh data
        self.session.refresh(subscription)
        
        return True, f"Successfully upgraded to {plan.name}", subscription
    
    def _update_minutes_for_plan(self, user_id: uuid.UUID, plan: SubscriptionPlan):
        """Update minutes account when plan changes"""
        account = self._create_minutes_account(user_id)
        
        # For upgrades during same period, add the difference
        # For renewals/new subscriptions, reset with potential rollover
        account.total_minutes = plan.minutes_included
        account.used_minutes = 0  # Reset for new period
        account.period_start = datetime.now(timezone.utc)
        
        # Calculate period end
        if plan.billing_period == BillingPeriod.WEEKLY.value:
            account.period_end = account.period_start + timedelta(weeks=1)
        elif plan.billing_period == BillingPeriod.MONTHLY.value:
            account.period_end = account.period_start + timedelta(days=30)
        elif plan.billing_period == BillingPeriod.ANNUAL.value:
            account.period_end = account.period_start + timedelta(days=365)
    
    def _create_consultation_credit(
        self, 
        user_id: uuid.UUID, 
        subscription_id: uuid.UUID,
        minutes: int,
        valid_until: datetime
    ) -> AnnualConsultationCredit:
        """Create free consultation credit for annual subscribers"""
        credit = AnnualConsultationCredit(
            user_id=user_id,
            subscription_id=subscription_id,
            consultation_minutes=minutes,
            minutes_remaining=minutes,
            valid_from=datetime.now(timezone.utc),
            valid_until=valid_until,
        )
        self.session.add(credit)
        return credit
    
    def cancel_subscription(self, user_id: uuid.UUID, immediate: bool = False) -> Tuple[bool, str]:
        """
        Cancel user subscription.
        If immediate=False, subscription remains active until period end.
        """
        subscription = self.get_or_create_subscription(user_id)
        
        if subscription.plan_id == 'freemium':
            return False, "Cannot cancel freemium plan"
        
        now = datetime.now(timezone.utc)
        old_plan_id = subscription.plan_id
        
        subscription.cancelled_at = now
        subscription.auto_renew = False
        
        if immediate:
            subscription.status = SubscriptionStatus.CANCELLED.value
            subscription.current_period_end = now
            # Downgrade to freemium
            subscription.plan_id = 'freemium'
            subscription.tier = SubscriptionTier.FREEMIUM.value
            subscription.billing_period = None
            # Reset freemium limits for returning user
            self._reset_freemium_limits_for_returning_user(user_id)
        
        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='cancelled',
            old_plan_id=old_plan_id,
            new_plan_id='freemium' if immediate else old_plan_id,
            event_metadata={'immediate': immediate}
        )
        
        self.session.commit()
        
        # Refresh to ensure subsequent queries get fresh data
        self.session.refresh(subscription)

        # Send cancellation confirmation email
        try:
            user = self.session.query(User).filter(User.id == user_id).first()
            plan = self._get_plan(old_plan_id)
            plan_display_name = plan.name if plan else old_plan_id
            end_date_display = subscription.current_period_end.strftime('%d %B %Y') if subscription.current_period_end else '-'
            if user and user.email:
                from services.email import EmailService
                EmailService.send_subscription_cancellation_email(
                    to_email=user.email,
                    first_name=user.first_name,
                    subscription_name=plan_display_name,
                    subscription_end_date=end_date_display,
                )
        except Exception as e:
            logger.error(f"Failed to send subscription cancellation email for user {user_id}: {e}")
        
        message = "Subscription cancelled immediately" if immediate else "Subscription will end at period end"
        return True, message
    
    def renew_subscription(
        self,
        user_id: uuid.UUID,
        current_period_end: Optional[datetime] = None,
        revenuecat_event_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Renew subscription at period end.
        Handles rollover for monthly plans.

        Returns:
            Tuple of (success, message, notification_data). notification_data
            is None unless a push notification needs to be sent by the caller
            (this method is sync and cannot await the async Firebase send
            itself -- see the RevenueCat webhook route, which is async and
            performs the actual send using this returned data).
        """
        subscription = self.get_or_create_subscription(user_id)
        
        if subscription.plan_id == 'freemium':
            return False, "Freemium plan does not renew", None
        
        if not subscription.auto_renew:
            # Downgrade to freemium
            subscription.plan_id = 'freemium'
            subscription.tier = SubscriptionTier.FREEMIUM.value
            subscription.billing_period = None
            subscription.status = SubscriptionStatus.EXPIRED.value
            # Reset freemium limits for returning user
            self._reset_freemium_limits_for_returning_user(user_id)
            self.session.commit()
            return False, "Subscription expired - auto-renew disabled", None
        
        plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == subscription.plan_id
        ).first()
        
        if not plan:
            return False, "Plan not found", None
        
        # Handle rollover for monthly plans
        account = self._create_minutes_account(user_id)
        rollover_minutes = 0
        
        if plan.rollover_enabled and plan.billing_period == BillingPeriod.MONTHLY.value:
            # Rollover ALL unused minutes including existing rollover and top-ups
            # available_minutes = total - used + rollover + top_up
            total_available = (
                account.total_minutes 
                - account.used_minutes 
                + account.rollover_minutes  # Include existing rollover
                + account.top_up_minutes     # Include unused top-ups
            )
            rollover_minutes = max(0, total_available)
            logger.info(f"Rolling over {rollover_minutes} minutes for user {user_id} "
                       f"(plan: {account.total_minutes - account.used_minutes}, "
                       f"prev_rollover: {account.rollover_minutes}, "
                       f"top_up: {account.top_up_minutes})")
        
        # Update for new period
        now = datetime.now(timezone.utc)
        subscription.current_period_start = now
        
        if current_period_end:
            if current_period_end.tzinfo is None:
                current_period_end = current_period_end.replace(tzinfo=timezone.utc)
            subscription.current_period_end = current_period_end
        elif plan.billing_period == BillingPeriod.WEEKLY.value:
            subscription.current_period_end = now + timedelta(weeks=1)
        elif plan.billing_period == BillingPeriod.MONTHLY.value:
            subscription.current_period_end = now + timedelta(days=30)
        elif plan.billing_period == BillingPeriod.ANNUAL.value:
            subscription.current_period_end = now + timedelta(days=365)
        
        # Reset minutes with rollover
        account.total_minutes = plan.minutes_included
        account.used_minutes = 0
        account.rollover_minutes = rollover_minutes
        account.top_up_minutes = 0  # Top-ups are now included in rollover_minutes
        account.period_start = now
        account.period_end = subscription.current_period_end
        account.top_ups_this_month = 0  # Reset top-up counter
        
        # Reset monthly usage tracking
        self._reset_monthly_usage(user_id)
        
        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='renewed',
            old_plan_id=subscription.plan_id,
            new_plan_id=subscription.plan_id,
            revenuecat_event_id=revenuecat_event_id,
            event_metadata={'rollover_minutes': rollover_minutes}
        )

        # Queue the in-app notification now (sync-safe -- NotificationService.create()
        # just adds to the session, no await needed). The push send itself is handed
        # back to the caller via notification_data, since Firebase sends are async
        # and this method is not.
        notification_data = None
        try:
            from services.notification_service import NotificationService

            device_tokens = self.get_user_device_tokens(user_id)
            if device_tokens:
                NotificationService(self.session).create(
                    recipient_id=user_id,
                    title="Subscription Renewed",
                    body=f"Your {subscription.plan_id} plan has been renewed successfully.",
                    notification_type="subscription_renewed",
                    data={"subscription_id": str(subscription.id), "plan_name": subscription.plan_id},
                )
                notification_data = {
                    'notification_type': 'subscription_renewed',
                    'device_tokens': device_tokens,
                    'plan_name': subscription.plan_id,
                    'subscription_id': str(subscription.id),
                }
        except Exception as notify_error:
            logger.warning(f"Failed to queue renewal notification for user {user_id}: {notify_error}")
        
        self.session.commit()
        
        return True, f"Subscription renewed with {rollover_minutes} rollover minutes", notification_data
    
    # ============================================
    # USAGE TRACKING & LIMITS
    # ============================================
    
    def get_usage_summary(self, user_id: uuid.UUID) -> Dict[str, Any]:
        """Get comprehensive usage summary for a user"""
        # Expire all cached objects to ensure fresh data is read from database
        # This is important when called after sync operations
        self.session.expire_all()
        
        subscription = self.get_or_create_subscription(user_id)
        account = self._create_minutes_account(user_id)
        
        # Get current month usage
        now = datetime.now(timezone.utc)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        # Get plan details
        plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == subscription.plan_id
        ).first()
        
        # For freemium users, minutes don't apply (they use trial allowances)
        # Show zeros to avoid confusion, but keep data in DB for potential re-subscription
        is_freemium = subscription.tier == SubscriptionTier.FREEMIUM.value
        
        if is_freemium:
            available_minutes = 0
            usage_percent = 0
            minutes_data = {
                'total': 0,
                'used': 0,
                'available': 0,
                'rollover': 0,
                'top_up': 0,  # Keep in DB but don't show - will be restored on re-subscription
                'top_ups_this_month': 0,
                'usage_percent': 0,
                'alert_threshold_reached': False,
            }
        else:
            available_minutes = account.total_minutes - account.used_minutes + account.rollover_minutes + account.top_up_minutes
            usage_percent = (account.used_minutes / account.total_minutes * 100) if account.total_minutes > 0 else 0
            minutes_data = {
                'total': account.total_minutes,
                'used': account.used_minutes,
                'available': max(0, available_minutes),
                'rollover': account.rollover_minutes,
                'top_up': account.top_up_minutes,
                'top_ups_this_month': account.top_ups_this_month,
                'usage_percent': round(usage_percent, 1),
                'alert_threshold_reached': usage_percent >= USAGE_ALERT_THRESHOLD_PERCENT,
            }
        
        # Real-time expiry check: if the billing period has already passed but the
        # DB row hasn't been updated yet (scheduler lag or auto_renew=True bypass),
        # surface the correct effective status to the caller immediately.
        effective_status = subscription.status
        if subscription.current_period_end and subscription.tier != SubscriptionTier.FREEMIUM.value:
            period_end = subscription.current_period_end
            if period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=timezone.utc)
            if period_end < now and effective_status == SubscriptionStatus.ACTIVE.value:
                effective_status = SubscriptionStatus.EXPIRED.value
                logger.info(
                    "[get_usage_summary] User %s: period_end=%s is in the past — "
                    "reporting status as expired (DB still shows active; scheduler will reconcile)",
                    user_id, period_end.isoformat(),
                )

        summary = {
            'subscription': {
                'plan_id': subscription.plan_id,
                'plan_name': plan.name if plan else 'Unknown',
                'tier': subscription.tier,
                'billing_period': subscription.billing_period,
                'status': effective_status,
                'current_period_end': self.serialize_utc_datetime(subscription.current_period_end),
                'auto_renew': subscription.auto_renew,
                'is_first_month': subscription.is_first_month,
            },
            'minutes': minutes_data,
            'monthly_usage': {
                'voice_notes': usage.voice_note_count,
                'uploads': usage.upload_count,
                'docgen': usage.docgen_count,
                'queries': usage.query_count,
            },
            'limits': {
                'voice_notes_limit': plan.voice_notes_limit if plan else None,
                'uploads_limit': plan.uploads_limit if plan else None,
                'docgen_limit': plan.docgen_limit if plan else None,
                'max_image_size_mb': plan.max_image_size_mb if plan else 25,
                'max_doc_pages': plan.max_doc_pages if plan else 8,
            },
            'features': {
                'unlimited_ai_queries': plan.unlimited_ai_queries if plan else False,
                'rollover_enabled': plan.rollover_enabled if plan else False,
                'priority_support': plan.priority_support if plan else False,
            }
        }
        
        # Add freemium-specific info
        if subscription.tier == SubscriptionTier.FREEMIUM.value:
            limits = self._get_freemium_limits(user_id)
            
            # Calculate queries_remaining and locked status
            queries_remaining = limits.queries_remaining if limits else 0
            queries_limit = (
                FREEMIUM_QUERIES_PER_WINDOW + (limits.ad_bonus_queries if limits else 0)
            )
            daily_pass_active = limits.daily_pass_active if limits else False
            
            # Check if daily pass is actually still valid
            if daily_pass_active and limits and limits.daily_pass_expires_at:
                expires_at = limits.daily_pass_expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if now > expires_at:
                    daily_pass_active = False

            if daily_pass_active:
                express_plan = self._get_express_plan()
                if express_plan:
                    summary['limits'] = {
                        'voice_notes_limit': express_plan.voice_notes_limit,
                        'uploads_limit': express_plan.uploads_limit,
                        'docgen_limit': express_plan.docgen_limit,
                        'max_image_size_mb': express_plan.max_image_size_mb,
                        'max_doc_pages': express_plan.max_doc_pages,
                    }
                    summary['features'] = {
                        'unlimited_ai_queries': express_plan.unlimited_ai_queries,
                        'rollover_enabled': express_plan.rollover_enabled,
                        'priority_support': express_plan.priority_support,
                    }
            
            # Determine if queries are locked (no remaining and no active daily pass)
            queries_locked = queries_remaining == 0 and not daily_pass_active
            
            # Calculate unlock_at timestamp (when queries will be available again)
            unlock_at = None
            if queries_locked and limits and limits.window_end:
                window_end = limits.window_end
                if window_end.tzinfo is None:
                    window_end = window_end.replace(tzinfo=timezone.utc)
                unlock_at = window_end.isoformat()
            
            # Basic/freemium caps are enforced from monthly UsageTracking (see can_perform_action).
            # Mirror those counts into trial_* keys so clients that still read them see the same
            # numbers as monthly_usage.{voice_notes,uploads,docgen}.
            summary['freemium'] = {
                'queries_remaining': queries_remaining,
                'queries_limit': queries_limit,
                'base_query_limit': FREEMIUM_QUERIES_PER_WINDOW,
                'queries_locked': queries_locked,
                'unlock_at': unlock_at,
                'window_ends': limits.window_end.isoformat() if limits and limits.window_end else None,
                'daily_pass_active': daily_pass_active,
                'daily_pass_expires': limits.daily_pass_expires_at.isoformat() if limits and limits.daily_pass_expires_at else None,
                'trial_expired': limits.trial_expired if limits else True,
                'trial_voice_notes_used': usage.voice_note_count,
                'trial_uploads_used': usage.upload_count,
                'trial_docgen_used': usage.docgen_count,
            }

        return summary
    
    def _get_or_create_monthly_usage(self, user_id: uuid.UUID, month: int, year: int) -> UsageTracking:
        """Get or create monthly usage tracking record, ensuring no NULL counters."""
        usage = self.session.query(UsageTracking).filter(
            and_(
                UsageTracking.user_id == user_id,
                UsageTracking.month == month,
                UsageTracking.year == year
            )
        ).first()

        if not usage:
            usage = UsageTracking(
                user_id=user_id,
                month=month,
                year=year,
            )
            self.session.add(usage)

        _INT_FIELDS = (
            "voice_note_count", "upload_count", "docgen_count", "query_count",
            "voice_note_minutes_used", "upload_minutes_used", "docgen_minutes_used",
        )
        for field in _INT_FIELDS:
            if getattr(usage, field, None) is None:
                setattr(usage, field, 0)

        return usage

    def _get_express_plan(self) -> Optional[SubscriptionPlan]:
        return self.session.query(SubscriptionPlan).filter(
            or_(
                SubscriptionPlan.id == "express-daily",
                SubscriptionPlan.id == "express_daily",
            )
        ).first()
    
    def _reset_monthly_usage(self, user_id: uuid.UUID):
        """Reset monthly counters for new billing period"""
        # Note: We keep historical records, just create new one for current month
        now = datetime.now(timezone.utc)
        self._get_or_create_monthly_usage(user_id, now.month, now.year)
    
    def _reset_monthly_usage_on_upgrade(self, user_id: uuid.UUID):
        """Reset monthly usage counters when user upgrades plans.
        
        This gives users a fresh start with their new plan limits,
        so previous freemium trial usage doesn't count against paid limits.
        """
        now = datetime.now(timezone.utc)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        # Reset all counters to 0
        usage.voice_note_count = 0
        usage.upload_count = 0
        usage.docgen_count = 0
        usage.query_count = 0
        usage.minutes_used = 0
        
        logger.info(f"Reset monthly usage counters for user {user_id} on plan upgrade")
    
    def _reset_freemium_limits_for_returning_user(self, user_id: uuid.UUID):
        """Restore freemium state when a paid subscriber returns to freemium.
        
        Trial usage (voice, upload, docgen) is PRESERVED - not reset.
        - If user used 2/2 before upgrading → they get 0 remaining when downgrading
        - If user used 1/2 before upgrading → they get 1 remaining when downgrading
        FreemiumLimits.trial_*_used are never reset on upgrade, so they retain pre-upgrade values.
        """
        limits = self._get_freemium_limits(user_id)
        if not limits:
            limits = self._create_freemium_limits(user_id)
        
        now = datetime.now(timezone.utc)
        
        # DO NOT reset trial_voice_notes_used, trial_uploads_used, trial_docgen_used
        # They retain the values from when the user was on freemium before upgrading.
        # If user exhausted 2/2, they stay at 2 → 0 remaining. If they used 1/2, they stay at 1 → 1 remaining.
        
        # DO NOT reset trial_expired, trial_started_at, trial_expires_at
        # Preserve the original trial period state.
        
        # Reset query window (fresh 6-hour window for AI queries)
        now_naive = now.replace(tzinfo=None)
        limits.query_count_current_window = 0
        limits.ad_bonus_queries = 0
        limits.window_start = now_naive
        limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
        
        # Reset daily pass
        limits.daily_pass_active = False
        limits.daily_pass_expires_at = None
        
        # Sync usage_tracking to match trial values for correct display (e.g. 2/2 or 1/2)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        usage.voice_note_count = limits.trial_voice_notes_used
        usage.upload_count = limits.trial_uploads_used
        usage.docgen_count = limits.trial_docgen_used
        usage.query_count = 0
        usage.minutes_used = 0
        
        logger.info(
            f"Restored freemium state for returning user {user_id}: "
            f"voice={limits.trial_voice_notes_used}/2, uploads={limits.trial_uploads_used}/2, "
            f"docgen={limits.trial_docgen_used}/2"
        )
    
    def _get_freemium_limits(self, user_id: uuid.UUID) -> Optional[FreemiumLimits]:
        """Get freemium limits for user"""
        return self.session.query(FreemiumLimits).filter(
            FreemiumLimits.user_id == user_id
        ).first()
    
    # ============================================
    # ACTION VALIDATION & DEDUCTION
    # ============================================
    
    def can_perform_action(
        self, 
        user_id: uuid.UUID, 
        action_type: UsageActionType,
        **kwargs
    ) -> Tuple[bool, Optional[BlockedReason], str]:
        """
        Check if user can perform an action.
        
        Args:
            user_id: User ID
            action_type: Type of action (voice_note, upload, docgen, query)
            **kwargs: Additional params (duration_seconds, file_size_bytes, page_count)
        
        Returns:
            Tuple of (can_perform, blocked_reason, message)
        """
        if self._is_lawyer_account(user_id):
            return True, None, "OK (Lawyer account)"

        subscription = self.get_or_create_subscription(user_id)
        
        # Check subscription is active
        # Note: Freemium users are always allowed (they use trial allowances)
        if subscription.tier != SubscriptionTier.FREEMIUM.value:
            if subscription.status not in [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIALING.value]:
                return False, BlockedReason.SUBSCRIPTION_INACTIVE, get_block_message(BlockedReason.SUBSCRIPTION_INACTIVE)
        
        plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == subscription.plan_id
        ).first()
        
        if not plan:
            return False, BlockedReason.SUBSCRIPTION_INACTIVE, "Plan not found"

        if (
            subscription.tier == SubscriptionTier.FREEMIUM.value
            and action_type in (UsageActionType.VOICE_NOTE, UsageActionType.UPLOAD)
            and self._daily_pass_is_active(user_id)
        ):
            express_plan = self._get_express_plan()
            if express_plan:
                plan = express_plan
        
        # Route to specific validation
        if action_type == UsageActionType.VOICE_NOTE:
            return self._can_record_voice_note(user_id, subscription, plan, **kwargs)
        elif action_type == UsageActionType.UPLOAD:
            return self._can_upload(user_id, subscription, plan, **kwargs)
        elif action_type == UsageActionType.DOCGEN:
            return self._can_generate_doc(user_id, subscription, plan, **kwargs)
        elif action_type == UsageActionType.QUERY:
            return self._can_query(user_id, subscription, plan)
        else:
            return False, BlockedReason.UPGRADE_REQUIRED, "Unknown action type"

    def _daily_pass_is_active(self, user_id: uuid.UUID) -> bool:
        limits = self._get_freemium_limits(user_id)
        if not limits or not limits.daily_pass_active or not limits.daily_pass_expires_at:
            return False

        expires_at = limits.daily_pass_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > datetime.now(timezone.utc)
    
    def _can_record_voice_note(
        self, 
        user_id: uuid.UUID, 
        subscription: UserSubscription,
        plan: SubscriptionPlan,
        duration_seconds: int = 0,
        **kwargs
    ) -> Tuple[bool, Optional[BlockedReason], str]:
        """Check if user can record a voice note"""
        now = datetime.now(timezone.utc)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        # Check monthly cap (unless unlimited)
        if plan.voice_notes_limit is not None:
            if usage.voice_note_count >= plan.voice_notes_limit:
                return False, BlockedReason.MONTHLY_CAP_EXCEEDED, get_block_message(
                    BlockedReason.MONTHLY_CAP_EXCEEDED,
                    current_usage=usage.voice_note_count,
                    limit=plan.voice_notes_limit
                )
        
        # Express allowances are item-based and do not require a minutes balance.
        if subscription.tier not in (
            SubscriptionTier.FREEMIUM.value,
            SubscriptionTier.EXPRESS.value,
        ):
            account = self._create_minutes_account(user_id)
            minutes_needed = calculate_voice_note_minutes(duration_seconds)
            
            if account.available_minutes < minutes_needed:
                return False, BlockedReason.INSUFFICIENT_MINUTES, get_block_message(
                    BlockedReason.INSUFFICIENT_MINUTES
                )
        
        return True, None, "OK"
    
    def _can_upload(
        self, 
        user_id: uuid.UUID, 
        subscription: UserSubscription,
        plan: SubscriptionPlan,
        file_size_bytes: int = 0,
        page_count: int = 0,
        is_image: bool = False,
        **kwargs
    ) -> Tuple[bool, Optional[BlockedReason], str]:
        """Check if user can upload a file"""
        now = datetime.now(timezone.utc)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        # Check monthly cap
        if plan.uploads_limit is not None:
            if usage.upload_count >= plan.uploads_limit:
                return False, BlockedReason.MONTHLY_CAP_EXCEEDED, get_block_message(
                    BlockedReason.MONTHLY_CAP_EXCEEDED,
                    current_usage=usage.upload_count,
                    limit=plan.uploads_limit
                )
        
        # Check file size (max_image_size_mb applies to all uploads)
        # Documents are primarily validated by page count (below), but we still enforce a size limit
        max_size_mb = plan.max_image_size_mb  # Same limit applies to all file types
        max_size_bytes = max_size_mb * 1024 * 1024
        if file_size_bytes > max_size_bytes:
            return False, BlockedReason.FILE_SIZE_EXCEEDED, get_block_message(BlockedReason.FILE_SIZE_EXCEEDED)
        
        # Check page count for documents
        if not is_image and page_count > 0:
            if page_count > plan.max_doc_pages:
                return False, BlockedReason.PAGE_LIMIT_EXCEEDED, (
                    "This document exceeds our page or size limit. "
                    "Please split it and upload separately. "
                    f"{'Each upload reduces usage.' if subscription.tier in (SubscriptionTier.FREEMIUM.value, SubscriptionTier.EXPRESS.value) else 'Each upload deducts 1 minute.'}"
                )
        
        # Express allowances are item-based and do not require a minutes balance.
        if subscription.tier not in (
            SubscriptionTier.FREEMIUM.value,
            SubscriptionTier.EXPRESS.value,
        ):
            account = self._create_minutes_account(user_id)
            if account.available_minutes < UPLOAD_FLAT_MINUTES:
                return False, BlockedReason.INSUFFICIENT_MINUTES, get_block_message(BlockedReason.INSUFFICIENT_MINUTES)
        
        return True, None, "OK"
    
    def _can_generate_doc(
        self, 
        user_id: uuid.UUID, 
        subscription: UserSubscription,
        plan: SubscriptionPlan,
        page_count: int = 1,
        **kwargs
    ) -> Tuple[bool, Optional[BlockedReason], str]:
        """Check if user can generate a document"""
        now = datetime.now(timezone.utc)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        # Check monthly cap
        if plan.docgen_limit is not None:
            if usage.docgen_count >= plan.docgen_limit:
                return False, BlockedReason.MONTHLY_CAP_EXCEEDED, get_block_message(
                    BlockedReason.MONTHLY_CAP_EXCEEDED,
                    current_usage=usage.docgen_count,
                    limit=plan.docgen_limit
                )
        
        # Check page limit for freemium
        if plan.max_docgen_pages is not None and page_count > plan.max_docgen_pages:
            return False, BlockedReason.PAGE_LIMIT_EXCEEDED, f"Freemium users limited to {plan.max_docgen_pages} pages per document."
        
        # For paid plans, check minutes
        if subscription.tier != SubscriptionTier.FREEMIUM.value:
            account = self._create_minutes_account(user_id)
            minutes_needed = get_docgen_minutes(page_count)
            
            if account.available_minutes < minutes_needed:
                return False, BlockedReason.INSUFFICIENT_MINUTES, get_block_message(BlockedReason.INSUFFICIENT_MINUTES)
        
        return True, None, "OK"
    
    def _can_query(
        self, 
        user_id: uuid.UUID, 
        subscription: UserSubscription,
        plan: SubscriptionPlan
    ) -> Tuple[bool, Optional[BlockedReason], str]:
        """Check if user can make an AI query"""
        # Paid plans have unlimited queries
        if plan.unlimited_ai_queries:
            return True, None, "OK"
        
        # Freemium: check limits
        limits = self._get_freemium_limits(user_id)
        if not limits:
            limits = self._create_freemium_limits(user_id)
        
        now = datetime.now(timezone.utc)
        
        # Check daily pass
        if limits.daily_pass_active and limits.daily_pass_expires_at:
            expires_at = limits.daily_pass_expires_at
            # Ensure timezone-aware for comparison
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > now:
                return True, None, "OK (Daily Pass)"
        
        # Reset window if expired
        if limits.window_end:
            window_end = limits.window_end
            # Ensure timezone-aware for comparison
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            if now > window_end:
                now_naive = now.replace(tzinfo=None)
                limits.query_count_current_window = 0
                limits.window_start = now_naive
                limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
                limits.ad_bonus_queries = 0  # Reset ad bonus each window
        
        # Check if within limit (base + ad bonus)
        total_allowed = FREEMIUM_QUERIES_PER_WINDOW + limits.ad_bonus_queries
        if limits.query_count_current_window >= total_allowed:
            return False, BlockedReason.QUERY_LIMIT_EXCEEDED, (
                f"You've used all {total_allowed} queries for this period. "
                f"Wait until {limits.window_end.strftime('%H:%M')} or purchase a daily pass (£5)."
            )
        
        return True, None, "OK"
    
    def deduct_usage(
        self, 
        user_id: uuid.UUID, 
        action_type: UsageActionType,
        **kwargs
    ) -> Tuple[bool, str, int]:
        """
        Deduct usage after action is performed.
        
        Args:
            user_id: User ID
            action_type: Type of action
            **kwargs: Additional params
        
        Returns:
            Tuple of (success, message, minutes_deducted)
        """
        if self._is_lawyer_account(user_id):
            return True, "Lawyer account - usage not deducted", 0

        subscription = self.get_or_create_subscription(user_id)
        account = self._create_minutes_account(user_id)
        now = datetime.now(timezone.utc)
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        minutes_before = account.available_minutes
        minutes_deducted = 0
        
        try:
            if action_type == UsageActionType.VOICE_NOTE:
                duration_seconds = kwargs.get('duration_seconds', 0)
                minutes_deducted = calculate_voice_note_minutes(duration_seconds)
                usage.voice_note_count += 1
                usage.voice_note_minutes_used += minutes_deducted
                
                # Update freemium trial if applicable
                if subscription.tier == SubscriptionTier.FREEMIUM.value:
                    limits = self._get_freemium_limits(user_id)
                    if limits:
                        limits.trial_voice_notes_used += 1
            
            elif action_type == UsageActionType.UPLOAD:
                minutes_deducted = UPLOAD_FLAT_MINUTES
                usage.upload_count += 1
                usage.upload_minutes_used += minutes_deducted
                
                if subscription.tier == SubscriptionTier.FREEMIUM.value:
                    limits = self._get_freemium_limits(user_id)
                    if limits:
                        limits.trial_uploads_used += 1
            
            elif action_type == UsageActionType.DOCGEN:
                page_count = kwargs.get('page_count', 1)
                minutes_deducted = get_docgen_minutes(page_count)
                usage.docgen_count += 1
                usage.docgen_minutes_used += minutes_deducted
                
                if subscription.tier == SubscriptionTier.FREEMIUM.value:
                    limits = self._get_freemium_limits(user_id)
                    if limits:
                        limits.trial_docgen_used += 1
            
            elif action_type == UsageActionType.QUERY:
                usage.query_count += 1
                
                if subscription.tier == SubscriptionTier.FREEMIUM.value:
                    limits = self._get_freemium_limits(user_id)
                    if limits:
                        limits.query_count_current_window += 1

            if subscription.tier == SubscriptionTier.EXPRESS.value:
                minutes_deducted = 0
            
            # Deduct minutes from account (for paid plans)
            if subscription.tier not in (
                SubscriptionTier.FREEMIUM.value,
                SubscriptionTier.EXPRESS.value,
            ) and minutes_deducted > 0:
                # Deduct from top-up first, then plan minutes
                if account.top_up_minutes >= minutes_deducted:
                    account.top_up_minutes -= minutes_deducted
                else:
                    remaining = minutes_deducted - account.top_up_minutes
                    account.top_up_minutes = 0
                    account.used_minutes += remaining
            
            minutes_after = account.available_minutes
            
            # Log the usage
            self._log_usage(
                user_id=user_id,
                action_type=action_type.value,
                minutes_deducted=minutes_deducted,
                minutes_before=minutes_before,
                minutes_after=minutes_after,
                status='success',
                **kwargs
            )
            
            self.session.commit()
            
            return True, f"Deducted {minutes_deducted} minutes", minutes_deducted
            
        except Exception as e:
            self.session.rollback()
            logger.error(f"Error deducting usage for user {user_id}: {e}")
            
            self._log_usage(
                user_id=user_id,
                action_type=action_type.value,
                minutes_deducted=0,
                minutes_before=minutes_before,
                minutes_after=minutes_before,
                status='failed',
                error_message=str(e),
                **kwargs
            )
            
            return False, f"Error: {str(e)}", 0
    
    def _log_usage(
        self,
        user_id: uuid.UUID,
        action_type: str,
        minutes_deducted: int,
        minutes_before: int,
        minutes_after: int,
        status: str = 'success',
        blocked_reason: Optional[str] = None,
        error_message: Optional[str] = None,
        **kwargs
    ):
        """Log usage action to usage_logs table"""
        log = UsageLog(
            user_id=user_id,
            action_type=action_type,
            action_description=kwargs.get('description'),
            minutes_deducted=minutes_deducted,
            minutes_before=minutes_before,
            minutes_after=minutes_after,
            referral_id=kwargs.get('referral_id'),
            conversation_id=kwargs.get('conversation_id'),
            file_size_bytes=kwargs.get('file_size_bytes'),
            file_name=kwargs.get('file_name'),
            duration_seconds=kwargs.get('duration_seconds'),
            page_count=kwargs.get('page_count'),
            status=status,
            blocked_reason=blocked_reason,
            error_message=error_message,
        )
        self.session.add(log)
    
    def _log_subscription_event(
        self,
        user_id: uuid.UUID,
        subscription_id: Optional[uuid.UUID],
        event_type: str,
        old_plan_id: Optional[str] = None,
        new_plan_id: Optional[str] = None,
        stripe_event_id: Optional[str] = None,
        revenuecat_event_id: Optional[str] = None,
        event_metadata: Optional[Dict] = None
    ):
        """Log subscription event"""
        event = SubscriptionEvent(
            user_id=user_id,
            subscription_id=subscription_id,
            event_type=event_type,
            old_plan_id=old_plan_id,
            new_plan_id=new_plan_id,
            stripe_event_id=stripe_event_id,
            revenuecat_event_id=revenuecat_event_id,
            event_metadata=event_metadata,
        )
        self.session.add(event)
    
    # ============================================
    # TOP-UPS & PURCHASES
    # ============================================
    
    def purchase_top_up(
        self, 
        user_id: uuid.UUID, 
        minutes: int,
        stripe_payment_intent_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[TopUpTransaction]]:
        """
        Process a top-up purchase.
        
        Args:
            user_id: User ID
            minutes: Minutes to purchase (5-30)
            stripe_payment_intent_id: Stripe payment intent ID
        
        Returns:
            Tuple of (success, message, transaction)
        """
        # Validate minutes
        if minutes < TOP_UP_MIN_MINUTES or minutes > TOP_UP_MAX_MINUTES:
            return False, f"Top-up must be between {TOP_UP_MIN_MINUTES} and {TOP_UP_MAX_MINUTES} minutes", None
        
        amount = calculate_topup_price(minutes)
        
        # Create transaction
        transaction = TopUpTransaction(
            user_id=user_id,
            minutes_purchased=minutes,
            amount_gbp=amount,
            stripe_payment_intent_id=stripe_payment_intent_id,
            payment_status=PaymentStatus.PENDING.value,
        )
        self.session.add(transaction)
        self.session.commit()
        
        return True, f"Top-up of {minutes} minutes for £{amount} created", transaction
    
    def complete_top_up(self, transaction_id: uuid.UUID) -> Tuple[bool, str, bool]:
        """Apply a completed top-up to user's account"""
        transaction = self.session.query(TopUpTransaction).filter(
            TopUpTransaction.id == transaction_id
        ).first()
        
        if not transaction:
            return False, "Transaction not found"
        
        if transaction.applied_to_account:
            return False, "Top-up already applied"
        
        # Update account
        account = self._create_minutes_account(transaction.user_id)
        account.top_up_minutes += transaction.minutes_purchased
        account.top_ups_this_month += 1
        account.last_top_up_at = datetime.now(timezone.utc)
        
        # Mark transaction as applied
        transaction.payment_status = PaymentStatus.SUCCEEDED.value
        transaction.applied_to_account = True
        transaction.applied_at = datetime.now(timezone.utc)
        
        self.session.commit()
        
        # Check if should suggest upgrade and send notification
        message = f"Added {transaction.minutes_purchased} minutes to your account"
        should_suggest = account.top_ups_this_month >= TOP_UP_SUGGESTION_COUNT
        
        if should_suggest:
            message += ". You've topped up twice this month. A higher plan may suit you better."
        
        return True, message, should_suggest  # Return flag for notification
    
    def purchase_daily_pass(
        self, 
        user_id: uuid.UUID,
        stripe_payment_intent_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Purchase daily unlimited query pass for freemium user"""
        subscription = self.get_or_create_subscription(user_id)
        
        if subscription.tier != SubscriptionTier.FREEMIUM.value:
            return False, "Daily pass only available for freemium users"
        
        limits = self._get_freemium_limits(user_id)
        if not limits:
            limits = self._create_freemium_limits(user_id)
        
        now = datetime.now(timezone.utc)
        now_naive = now.replace(tzinfo=None)
        midnight = now_naive.replace(hour=23, minute=59, second=59)
        
        limits.daily_pass_active = True
        limits.daily_pass_expires_at = midnight
        limits.daily_pass_purchased_at = now_naive

        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        usage.voice_note_count = 0
        usage.upload_count = 0
        
        self.session.commit()
        
        return True, f"Daily pass activated until {midnight.strftime('%H:%M')}"
    
    def sync_consumable_from_client(
        self,
        user_id: uuid.UUID,
        transaction_id: str,
        product_id: str,
        purchase_date: Optional[str] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Sync a consumable purchase (Daily Pass or Top-up) from the client.
        
        Called immediately after a successful RevenueCat purchase to update
        the backend state without waiting for the webhook.
        
        Args:
            user_id: User ID
            transaction_id: RevenueCat transaction ID (used for deduplication)
            product_id: RevenueCat product ID (e.g., 'daily-pass', 'topup-5')
            purchase_date: ISO timestamp of purchase (optional)
        
        Returns:
            Tuple of (success, message, details_dict)
        """
        details: Dict[str, Any] = {
            'transaction_id': transaction_id,
            'product_id': product_id
        }
        
        # Handle Daily Pass (any platform-specific variant)
        if product_id in REVENUECAT_DAILY_PASS_PRODUCT_IDS:
            # Check if daily pass is already active (avoid duplicate activation)
            limits = self._get_freemium_limits(user_id)
            if limits and limits.daily_pass_active:
                now = datetime.now(timezone.utc)
                if limits.daily_pass_expires_at:
                    expires_at = limits.daily_pass_expires_at
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    if now < expires_at:
                        details['already_active'] = True
                        details['expires_at'] = expires_at.isoformat()
                        return True, "Daily pass already active", details
            
            # Activate daily pass
            success, message = self.purchase_daily_pass(user_id=user_id)
            
            if success:
                # Refresh limits to get updated expiration
                limits = self._get_freemium_limits(user_id)
                details['daily_pass_active'] = True
                details['expires_at'] = limits.daily_pass_expires_at.isoformat() if limits and limits.daily_pass_expires_at else None
                
                # Log the sync event
                subscription = self.get_or_create_subscription(user_id)
                self._log_subscription_event(
                    user_id=user_id,
                    subscription_id=subscription.id,
                    event_type='consumable_sync_daily_pass',
                    event_metadata={
                        'transaction_id': transaction_id,
                        'product_id': product_id,
                        'source': 'client_sync'
                    }
                )
                self.session.commit()
            
            return success, message, details
        
        # Handle Top-up
        if product_id in REVENUECAT_TOP_UP_PRODUCTS:
            # Check for duplicate transaction
            existing = self.session.query(TopUpTransaction).filter(
                TopUpTransaction.revenuecat_purchase_id == transaction_id
            ).first()
            
            if existing:
                details['already_processed'] = True
                details['minutes'] = existing.minutes_purchased
                details['applied'] = existing.applied_to_account
                return True, "Top-up already processed", details
            
            # Get minutes from product ID
            minutes = REVENUECAT_TOP_UP_PRODUCTS[product_id]
            amount = calculate_topup_price(minutes)
            
            # Create and apply the top-up transaction
            transaction = TopUpTransaction(
                user_id=user_id,
                minutes_purchased=minutes,
                amount_gbp=amount,
                revenuecat_purchase_id=transaction_id,
                revenuecat_product_id=product_id,
                payment_status=PaymentStatus.PENDING.value,
            )
            self.session.add(transaction)
            self.session.commit()
            
            # Complete the top-up (apply minutes to account)
            success, message, should_notify = self.complete_top_up(transaction.id)
            
            if success:
                # Get updated account balance
                account = self._create_minutes_account(user_id)
                details['minutes_added'] = minutes
                details['total_top_up_minutes'] = account.top_up_minutes
                details['top_ups_this_month'] = account.top_ups_this_month
                details['should_suggest_upgrade'] = should_notify
                
                # Log the sync event
                subscription = self.get_or_create_subscription(user_id)
                self._log_subscription_event(
                    user_id=user_id,
                    subscription_id=subscription.id,
                    event_type='consumable_sync_top_up',
                    event_metadata={
                        'transaction_id': transaction_id,
                        'product_id': product_id,
                        'minutes': minutes,
                        'amount_gbp': float(amount),
                        'source': 'client_sync'
                    }
                )
                self.session.commit()

                self._send_wallet_topup_confirmation_email(
                    user_id=user_id,
                    transaction_id=transaction_id,
                    minutes_added=minutes,
                    total_paid=amount,
                )
            
            return success, message, details
        
        # Unknown product
        details['error'] = 'unknown_product'
        return False, f"Unknown consumable product: {product_id}", details
    
    def grant_ad_bonus(self, user_id: uuid.UUID) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Grant bonus queries for watching an ad.

        Adds ``FREEMIUM_AD_BONUS_QUERIES`` (6) queries to the user's current window
        without resetting the window clock or the used-query counter. The bonus is
        cumulative: each ad watched in the same window stacks (1 ad → +6, 2 ads → +12,
        etc.). A user who has already used 10/10 queries will have
        ``queries_remaining == 6`` after the first ad and ``queries_remaining == 12``
        after the second.

        If the current window has naturally expired at the time this is called, the
        window is rolled forward first (same behavior as ``check_query_limit_for_freemium``)
        so the newly granted bonus isn't silently wiped on the next limit check.

        Returns:
            Tuple of (success, message, status_dict)
            status_dict contains: bonus_queries, queries_remaining, queries_locked,
            unlock_at, window_ends, total_queries_allowed
        """
        limits = self._get_freemium_limits(user_id)
        if not limits:
            return False, "User is not a freemium user", {}

        now = datetime.now(timezone.utc)
        now_naive = now.replace(tzinfo=None)

        # Initialize window if one has never been created for this user.
        if not limits.window_end:
            limits.window_start = now_naive
            limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
            limits.query_count_current_window = 0
        else:
            # If the window has naturally expired, roll it forward before granting
            # the bonus (otherwise the next limit check would reset ad_bonus to 0).
            window_end = limits.window_end
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            if now > window_end:
                limits.window_start = now_naive
                limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
                limits.query_count_current_window = 0

        # Grant the bonus without touching window_start / window_end /
        # query_count_current_window in the common (unexpired) case, so every ad
        # watched adds FREEMIUM_AD_BONUS_QUERIES more queries cumulatively.
        limits.ad_bonus_queries += FREEMIUM_AD_BONUS_QUERIES
        limits.last_ad_watched_at = now_naive
        limits.ads_watched_today += 1

        self.session.commit()
        
        # Calculate updated status to return
        total_allowed = FREEMIUM_QUERIES_PER_WINDOW + limits.ad_bonus_queries
        queries_remaining = max(0, total_allowed - limits.query_count_current_window)
        queries_locked = queries_remaining == 0
        
        # Calculate unlock_at (only if locked)
        unlock_at = None
        if queries_locked and limits.window_end:
            window_end = limits.window_end
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            unlock_at = window_end.isoformat()
        
        status = {
            'bonus_queries': FREEMIUM_AD_BONUS_QUERIES,
            'queries_remaining': queries_remaining,
            'queries_locked': queries_locked,
            'unlock_at': unlock_at,
            'window_ends': limits.window_end.isoformat() if limits.window_end else None,
            'total_queries_allowed': total_allowed,
        }
        
        return True, f"Unlocked {FREEMIUM_AD_BONUS_QUERIES} bonus queries", status
    
    def check_query_limit_for_freemium(self, user_id: uuid.UUID) -> Dict[str, Any]:
        """
        Check if freemium user can make a query.
        Returns comprehensive status with suggestions.
        
        Returns:
            Dict with keys: allowed, reason, message, queries_used, queries_remaining,
            queries_limit, window_resets_at, suggestions
        """
        if self._is_lawyer_account(user_id):
            return {
                'allowed': True,
                'reason': 'lawyer_account',
                'message': 'OK (Lawyer account)',
                'tier': 'lawyer',
            }

        subscription = self.get_or_create_subscription(user_id)
        plan = self._get_plan(subscription.plan_id)
        
        # Paid plans have unlimited queries
        if plan and plan.unlimited_ai_queries:
            return {
                'allowed': True,
                'reason': 'unlimited_queries',
                'message': 'OK',
                'tier': subscription.tier
            }
        
        # Freemium: get limits
        limits = self._get_freemium_limits(user_id)
        if not limits:
            limits = self._create_freemium_limits(user_id)
        
        now = datetime.now(timezone.utc)
        
        # Initialize window if needed
        if not limits.window_start:
            now_naive = now.replace(tzinfo=None)
            limits.window_start = now_naive
            limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
            limits.query_count_current_window = 0
            self.session.commit()
        
        # Check daily pass first
        if limits.daily_pass_active and limits.daily_pass_expires_at:
            expires_at = limits.daily_pass_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            
            # Reset daily pass if expired
            if expires_at <= now:
                limits.daily_pass_active = False
                limits.daily_pass_expires_at = None
                self.session.commit()
            else:
                return {
                    'allowed': True,
                    'reason': 'daily_pass',
                    'message': 'OK (Daily Pass Active)',
                    'daily_pass_expires_at': expires_at.isoformat(),
                    'tier': subscription.tier
                }
        
        # Check and reset window if expired
        if limits.window_end:
            window_end = limits.window_end
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            
            if now > window_end:
                now_naive = now.replace(tzinfo=None)
                limits.query_count_current_window = 0
                limits.ad_bonus_queries = 0  # Reset ad bonus on new window
                limits.window_start = now_naive
                limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
                self.session.commit()
        
        # Calculate total allowed (base + ad bonus)
        total_allowed = FREEMIUM_QUERIES_PER_WINDOW + limits.ad_bonus_queries
        queries_used = limits.query_count_current_window
        queries_remaining = max(0, total_allowed - queries_used)
        
        # Check if within limit
        if queries_used >= total_allowed:
            window_end = limits.window_end
            if window_end and window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            
            # Calculate remaining time until reset
            remaining_seconds = 0
            remaining_time_formatted = None
            if window_end:
                time_diff = window_end - now
                remaining_seconds = max(0, int(time_diff.total_seconds()))
                
                # Format remaining time
                hours = remaining_seconds // 3600
                minutes = (remaining_seconds % 3600) // 60
                if hours > 0:
                    remaining_time_formatted = f"{hours}h {minutes}m"
                else:
                    remaining_time_formatted = f"{minutes}m"
            
            return {
                'allowed': False,
                'reason': 'query_limit_exceeded',
                'message': f"You've used all {total_allowed} queries for this 6-hour period.",
                'queries_used': queries_used,
                'queries_limit': total_allowed,
                'window_resets_at': window_end.isoformat() if window_end else None,
                'remaining_time_seconds': remaining_seconds,
                'remaining_time_formatted': remaining_time_formatted,
                'locked': True,  # Frontend should block input
                'tier': subscription.tier,
                'suggestions': [
                    {
                        'type': 'daily_pass',
                        'price_gbp': float(FREEMIUM_DAILY_PASS_PRICE_GBP),
                        'benefit': 'Unlimited queries for the rest of today'
                    },
                    {
                        'type': 'watch_ad',
                        'benefit': f'Get {FREEMIUM_AD_BONUS_QUERIES} bonus queries'
                    },
                    {
                        'type': 'upgrade',
                        'benefit': 'Unlimited queries with subscription'
                    },
                    {
                        'type': 'wait',
                        'window_resets_at': window_end.isoformat() if window_end else None,
                        'remaining_time_seconds': remaining_seconds,
                        'remaining_time_formatted': remaining_time_formatted
                    }
                ]
            }
        
        return {
            'allowed': True,
            'reason': 'within_limit',
            'message': 'OK',
            'queries_used': queries_used,
            'queries_remaining': queries_remaining,
            'queries_limit': total_allowed,
            'window_resets_at': limits.window_end.isoformat() if limits.window_end else None,
            'ad_bonus_available': limits.ad_bonus_queries,
            'tier': subscription.tier
        }
    
    def get_freemium_status(self, user_id: uuid.UUID) -> Dict[str, Any]:
        """
        Get comprehensive freemium user status.
        
        Returns:
            Dict with tier, queries info, daily_pass info, trial_allowances
        """
        subscription = self.get_or_create_subscription(user_id)
        
        if subscription.tier != SubscriptionTier.FREEMIUM.value:
            return {
                'tier': subscription.tier,
                'message': 'User is not on freemium tier',
                'is_freemium': False
            }
        
        limits = self._get_freemium_limits(user_id)
        if not limits:
            limits = self._create_freemium_limits(user_id)
        
        now = datetime.now(timezone.utc)
        plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == subscription.plan_id
        ).first()
        usage = self._get_or_create_monthly_usage(user_id, now.month, now.year)
        
        # Initialize window if needed
        if not limits.window_start:
            now_naive = now.replace(tzinfo=None)
            limits.window_start = now_naive
            limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
            self.session.commit()
        
        # Check window expiry and reset if needed
        window_end = limits.window_end
        if window_end:
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            if now > window_end:
                now_naive = now.replace(tzinfo=None)
                limits.query_count_current_window = 0
                limits.ad_bonus_queries = 0
                limits.window_start = now_naive
                limits.window_end = now_naive + timedelta(hours=FREEMIUM_WINDOW_HOURS)
                window_end = limits.window_end
                self.session.commit()
        
        # Check daily pass status
        daily_pass_active = False
        daily_pass_expires_at = None
        if limits.daily_pass_active and limits.daily_pass_expires_at:
            expires_at = limits.daily_pass_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > now:
                daily_pass_active = True
                daily_pass_expires_at = expires_at
            else:
                # Reset expired daily pass
                limits.daily_pass_active = False
                limits.daily_pass_expires_at = None
                self.session.commit()
        
        total_allowed = FREEMIUM_QUERIES_PER_WINDOW + limits.ad_bonus_queries
        queries_remaining = max(0, total_allowed - limits.query_count_current_window)
        
        # Get trial status
        trial_expired = limits.trial_expired
        trial_expires_at = None
        if limits.trial_expires_at:
            trial_expires_at = limits.trial_expires_at
            if trial_expires_at.tzinfo is None:
                trial_expires_at = trial_expires_at.replace(tzinfo=timezone.utc)
            if now > trial_expires_at:
                trial_expired = True
        
        return {
            'tier': 'freemium',
            'is_freemium': True,
            'queries': {
                'used_this_window': limits.query_count_current_window,
                'limit': total_allowed,
                'base_limit': FREEMIUM_QUERIES_PER_WINDOW,
                'ad_bonus_available': limits.ad_bonus_queries,
                'remaining': queries_remaining,
                'window_resets_at': window_end.isoformat() if window_end else None,
                'window_start': limits.window_start.isoformat() if limits.window_start else None
            },
            'daily_pass': {
                'active': daily_pass_active,
                'expires_at': daily_pass_expires_at.isoformat() if daily_pass_expires_at else None,
                'price_gbp': float(FREEMIUM_DAILY_PASS_PRICE_GBP)
            },
            'trial_allowances': {
                'voice_notes': {
                    'used': usage.voice_note_count,
                    'limit': plan.voice_notes_limit if plan and plan.voice_notes_limit is not None else FREEMIUM_TRIAL_VOICE_NOTES,
                    'remaining': max(
                        0,
                        (plan.voice_notes_limit if plan and plan.voice_notes_limit is not None else FREEMIUM_TRIAL_VOICE_NOTES)
                        - usage.voice_note_count,
                    ),
                },
                'uploads': {
                    'used': usage.upload_count,
                    'limit': plan.uploads_limit if plan and plan.uploads_limit is not None else FREEMIUM_TRIAL_UPLOADS,
                    'remaining': max(
                        0,
                        (plan.uploads_limit if plan and plan.uploads_limit is not None else FREEMIUM_TRIAL_UPLOADS)
                        - usage.upload_count,
                    ),
                },
                'docgen': {
                    'used': usage.docgen_count,
                    'limit': plan.docgen_limit if plan and plan.docgen_limit is not None else FREEMIUM_TRIAL_DOCGEN,
                    'remaining': max(
                        0,
                        (plan.docgen_limit if plan and plan.docgen_limit is not None else FREEMIUM_TRIAL_DOCGEN)
                        - usage.docgen_count,
                    ),
                },
                'expires_at': trial_expires_at.isoformat() if trial_expires_at else None,
                'expired': trial_expired
            }
        }
    
    def _get_plan(self, plan_id: str) -> Optional[SubscriptionPlan]:
        """Get subscription plan by ID"""
        return self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == plan_id
        ).first()
    
    # ============================================
    # ANALYTICS HELPERS
    # ============================================
    
    def get_usage_history(
        self, 
        user_id: uuid.UUID, 
        limit: int = 50
    ) -> list:
        """Get usage history for a user"""
        logs = self.session.query(UsageLog).filter(
            UsageLog.user_id == user_id
        ).order_by(UsageLog.created_at.desc()).limit(limit).all()
        
        return [{
            'id': str(log.id),
            'action_type': log.action_type,
            'minutes_deducted': log.minutes_deducted,
            'status': log.status,
            'created_at': log.created_at.isoformat(),
            'file_name': log.file_name,
            'duration_seconds': log.duration_seconds,
            'page_count': log.page_count,
        } for log in logs]
    
    def get_subscription_events(
        self, 
        user_id: uuid.UUID, 
        limit: int = 20
    ) -> list:
        """Get subscription events for a user"""
        events = self.session.query(SubscriptionEvent).filter(
            SubscriptionEvent.user_id == user_id
        ).order_by(SubscriptionEvent.created_at.desc()).limit(limit).all()
        
        return [{
            'id': str(event.id),
            'event_type': event.event_type,
            'old_plan': event.old_plan_id,
            'new_plan': event.new_plan_id,
            'created_at': event.created_at.isoformat(),
        } for event in events]
    
    # ============================================
    # DOWNGRADE & REACTIVATE METHODS
    # ============================================
    
    def downgrade_subscription(
        self,
        user_id: uuid.UUID,
        new_plan_id: str,
        stripe_subscription_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[UserSubscription]]:
        """
        Downgrade subscription to a lower tier.
        Downgrade takes effect at end of current billing period.
        
        Returns: (success, message, subscription)
        """
        subscription = self.get_or_create_subscription(user_id)
        
        if subscription.plan_id == 'freemium':
            return False, "Already on freemium plan", None
        
        # Get current and new plan info
        current_plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == subscription.plan_id
        ).first()
        
        new_plan = self.session.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == new_plan_id,
            SubscriptionPlan.is_active == True
        ).first()
        
        if not new_plan:
            return False, f"Plan '{new_plan_id}' not found or inactive", None
        
        if new_plan_id == 'freemium':
            # Downgrading to freemium is same as cancellation
            return self.cancel_subscription(user_id, immediate=False) + (subscription,)
        
        # Verify it's actually a downgrade
        tier_order = {'freemium': 0, 'standard': 1, 'premium': 2}
        current_tier_order = tier_order.get(current_plan.tier if current_plan else 'freemium', 0)
        new_tier_order = tier_order.get(new_plan.tier, 0)
        
        if new_tier_order >= current_tier_order:
            return False, "This is not a downgrade. Use upgrade endpoint instead.", None
        
        old_plan_id = subscription.plan_id
        
        # Schedule downgrade at period end
        # We store the pending downgrade in metadata
        subscription.pending_downgrade_to = new_plan_id
        
        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='downgrade_scheduled',
            old_plan_id=old_plan_id,
            new_plan_id=new_plan_id,
            event_metadata={
                'effective_at': self.serialize_utc_datetime(subscription.current_period_end)
            }
        )
        
        self.session.commit()
        
        # Refresh to ensure subsequent queries get fresh data
        self.session.refresh(subscription)
        
        period_end = subscription.current_period_end.strftime('%Y-%m-%d') if subscription.current_period_end else 'period end'
        return True, f"Downgrade to {new_plan.name} scheduled for {period_end}", subscription
    
    def reactivate_subscription(
        self,
        user_id: uuid.UUID
    ) -> Tuple[bool, str, Optional[UserSubscription]]:
        """
        Reactivate a cancelled subscription.
        Only works if subscription hasn't expired yet.
        
        Returns: (success, message, subscription)
        """
        subscription = self.get_or_create_subscription(user_id)
        
        if subscription.plan_id == 'freemium':
            return False, "No subscription to reactivate. Please subscribe to a plan.", None
        
        if subscription.status == SubscriptionStatus.ACTIVE.value and subscription.auto_renew:
            return False, "Subscription is already active", None
        
        now = datetime.now(timezone.utc)
        
        # Check if subscription period has already ended
        if subscription.current_period_end:
            period_end = subscription.current_period_end
            # Ensure timezone-aware for comparison
            if period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=timezone.utc)
            if period_end < now:
                return False, "Subscription has already expired. Please create a new subscription.", None
        
        # Reactivate: set auto_renew back to True and clear cancellation
        old_status = subscription.status
        subscription.cancelled_at = None
        subscription.auto_renew = True
        subscription.status = SubscriptionStatus.ACTIVE.value
        
        # Clear any pending downgrade
        if hasattr(subscription, 'pending_downgrade_to'):
            subscription.pending_downgrade_to = None
        
        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='reactivated',
            old_plan_id=subscription.plan_id,
            new_plan_id=subscription.plan_id,
            event_metadata={'previous_status': old_status}
        )
        
        self.session.commit()
        
        # Refresh to ensure subsequent queries get fresh data
        self.session.refresh(subscription)
        
        return True, "Subscription reactivated successfully", subscription
    
    def subscribe(
        self,
        user_id: uuid.UUID,
        plan_id: str,
        stripe_subscription_id: Optional[str] = None,
        stripe_customer_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[UserSubscription]]:
        """
        Subscribe a user to a paid plan.
        This is a wrapper around upgrade_subscription with better naming for new subscribers.
        
        Returns: (success, message, subscription)
        """
        if plan_id == 'freemium':
            return False, "Cannot subscribe to freemium. All users start with freemium automatically.", None
        
        # Use upgrade logic (works from freemium to any paid plan)
        return self.upgrade_subscription(
            user_id=user_id,
            new_plan_id=plan_id,
            stripe_subscription_id=stripe_subscription_id
        )

    # ============================================
    # REVENUECAT WEBHOOK HANDLERS
    # ============================================

    def _revenuecat_event_already_processed(self, event_id: Optional[str]) -> bool:
        if not event_id:
            return False
        existing = self.session.query(SubscriptionEvent).filter(
            SubscriptionEvent.revenuecat_event_id == event_id
        ).first()
        return existing is not None

    def _resolve_revenuecat_entitlement(
        self,
        entitlement_ids: list
    ) -> Optional[str]:
        if not entitlement_ids:
            return None
        if "premium_access" in entitlement_ids:
            return "premium_access"
        if "standard_access" in entitlement_ids:
            return "standard_access"
        return entitlement_ids[0]

    def _update_revenuecat_fields(
        self,
        subscription: UserSubscription,
        app_user_id: Optional[str],
        product_id: Optional[str],
        entitlement_id: Optional[str],
        original_transaction_id: Optional[str]
    ):
        if app_user_id:
            subscription.revenuecat_app_user_id = app_user_id
        if product_id:
            subscription.revenuecat_product_id = product_id
        if entitlement_id:
            subscription.revenuecat_entitlement_id = entitlement_id
        if original_transaction_id:
            subscription.revenuecat_original_transaction_id = original_transaction_id

    def handle_revenuecat_event(self, event: Dict[str, Any]) -> Tuple[bool, str, bool, Optional[Dict[str, Any]]]:
        """
        Handle RevenueCat webhook events.

        Routes subscription vs consumable events and applies updates to the DB.

        Returns:
            Tuple of (success, message, should_notify, subscription_notification).
            should_notify is the pre-existing top-up-suggestion flag (unrelated to
            subscription_renewed/expired). subscription_notification is None unless
            a subscription-level push (currently: subscription_renewed) needs to be
            sent by the async webhook route -- this method and everything it calls
            is sync, so the actual Firebase send happens one layer up.
        """
        event_id = event.get("event_id")
        if self._revenuecat_event_already_processed(event_id):
            return True, "Duplicate RevenueCat event ignored", False, None

        # Try user_id first (our actual UUID), fall back to app_user_id
        # app_user_id may be RevenueCat's anonymous format: $RCAnonymousID:xxx
        raw_user_id = event.get("user_id")
        app_user_id = event.get("app_user_id")  # Keep for logging/storage
        
        if not raw_user_id and not app_user_id:
            return False, "Missing user_id or app_user_id", False, None

        user_id = None
        
        # First, try to parse user_id directly as UUID
        if raw_user_id:
            try:
                user_id = uuid.UUID(raw_user_id)
            except ValueError:
                pass
        
        # If no valid user_id, try app_user_id
        if not user_id and app_user_id:
            # Check if it's a direct UUID
            try:
                user_id = uuid.UUID(app_user_id)
            except ValueError:
                # Try stripping common prefixes like "rc_"
                if not app_user_id.startswith("$RC"):
                    try:
                        cleaned_id = app_user_id.replace("rc_", "").replace("RC_", "")
                        user_id = uuid.UUID(cleaned_id)
                    except ValueError:
                        pass
        
        # If still no user_id, lookup by RevenueCat app_user_id in our database
        if not user_id and app_user_id:
            subscription = self.session.query(UserSubscription).filter(
                UserSubscription.revenuecat_app_user_id == app_user_id
            ).first()
            if subscription:
                user_id = subscription.user_id
                logger.info(f"Resolved user_id {user_id} from revenuecat_app_user_id: {app_user_id}")
        
        if not user_id:
            return False, f"Could not resolve user from app_user_id: {app_user_id}. User must sync first.", False, None

        product_id = event.get("product_id")
        if product_id in REVENUECAT_TOP_UP_PRODUCTS or product_id == REVENUECAT_DAILY_PASS_PRODUCT_ID:
            success, message, should_notify = self._handle_revenuecat_consumable(
                user_id=user_id,
                product_id=product_id,
                event=event
            )
            return success, message, should_notify, None

        if product_id and product_id not in REVENUECAT_SUBSCRIPTION_PRODUCTS:
            return False, f"Unknown RevenueCat product_id: {product_id}", False, None

        success, message, subscription_notification = self._handle_revenuecat_subscription_event(
            user_id=user_id,
            product_id=product_id,
            event=event
        )
        return success, message, False, subscription_notification

    def _handle_revenuecat_consumable(
        self,
        user_id: uuid.UUID,
        product_id: Optional[str],
        event: Dict[str, Any]
    ) -> Tuple[bool, str, bool]:
        event_id = event.get("event_id")
        purchase_id = event.get("transaction_id") or event.get("original_transaction_id") or event_id

        # Check if this is any variant of daily pass (platform-specific or generic)
        if product_id in REVENUECAT_DAILY_PASS_PRODUCT_IDS:
            success, message = self.purchase_daily_pass(user_id=user_id)
            subscription = self.get_or_create_subscription(user_id)
            self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.id,
                event_type='revenuecat_daily_pass',
                revenuecat_event_id=event_id,
                event_metadata={
                    'product_id': product_id,
                    'purchase_id': purchase_id,
                    'success': success,
                    'message': message
                }
            )
            self.session.commit()
            return success, message, False

        if product_id not in REVENUECAT_TOP_UP_PRODUCTS:
            return False, f"Unknown consumable product_id: {product_id}", False

        if purchase_id:
            existing = self.session.query(TopUpTransaction).filter(
                TopUpTransaction.revenuecat_purchase_id == purchase_id
            ).first()
            if existing:
                return True, "Top-up already processed", False

        minutes = REVENUECAT_TOP_UP_PRODUCTS[product_id]
        amount = calculate_topup_price(minutes)

        transaction = TopUpTransaction(
            user_id=user_id,
            minutes_purchased=minutes,
            amount_gbp=amount,
            revenuecat_purchase_id=purchase_id,
            revenuecat_product_id=product_id,
            payment_status=PaymentStatus.PENDING.value,
        )
        self.session.add(transaction)
        self.session.commit()

        success, message, should_notify = self.complete_top_up(transaction.id)

        subscription = self.get_or_create_subscription(user_id)
        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='revenuecat_top_up',
            revenuecat_event_id=event_id,
            event_metadata={
                'product_id': product_id,
                'purchase_id': purchase_id,
                'minutes': minutes,
                'amount_gbp': float(amount),
                'success': success,
                'message': message,
                'should_notify': should_notify
            }
        )
        self.session.commit()

        if success:
            self._send_wallet_topup_confirmation_email(
                user_id=user_id,
                transaction_id=purchase_id,
                minutes_added=minutes,
                total_paid=amount,
            )

        return success, message, should_notify

    def _handle_revenuecat_subscription_event(
        self,
        user_id: uuid.UUID,
        product_id: Optional[str],
        event: Dict[str, Any]
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Returns:
            Tuple of (success, message, subscription_notification). subscription_notification
            is None in every branch except RENEWAL (passed through from renew_subscription()).
            The EXPIRATION branch deliberately does NOT populate this -- subscription_expired
            notifications are sent only from jobs/subscription_jobs.py's hourly job, to avoid
            a duplicate send given this branch and that job can both observe the same expiry.
        """
        event_type = event.get("type")
        event_id = event.get("event_id")
        app_user_id = event.get("app_user_id")
        entitlement_ids = event.get("entitlement_ids") or []
        expiration_at = event.get("expiration_at")
        original_transaction_id = event.get("original_transaction_id")

        subscription = self.get_or_create_subscription(user_id)
        plan = self._get_plan(product_id) if product_id else None
        entitlement_id = self._resolve_revenuecat_entitlement(entitlement_ids)

        if plan:
            self._update_revenuecat_fields(
                subscription=subscription,
                app_user_id=app_user_id,
                product_id=product_id,
                entitlement_id=entitlement_id,
                original_transaction_id=original_transaction_id
            )

        if event_type in {"INITIAL_PURCHASE", "PRODUCT_CHANGE", "UNCANCELLATION"}:
            if not plan:
                return False, f"Plan not found for product_id: {product_id}", None
            success, message, _ = self.upgrade_subscription(
                user_id=user_id,
                new_plan_id=product_id,
                current_period_end=expiration_at,
                revenuecat_app_user_id=app_user_id,
                revenuecat_product_id=product_id,
                revenuecat_entitlement_id=entitlement_id,
                revenuecat_original_transaction_id=original_transaction_id,
                revenuecat_event_id=event_id,
                event_metadata={'revenuecat_event_type': event_type}
            )
            return success, message, None

        if event_type == "RENEWAL":
            if not plan:
                return False, f"Plan not found for product_id: {product_id}", None
            if subscription.plan_id != product_id:
                success, message, _ = self.upgrade_subscription(
                    user_id=user_id,
                    new_plan_id=product_id,
                    current_period_end=expiration_at,
                    revenuecat_app_user_id=app_user_id,
                    revenuecat_product_id=product_id,
                    revenuecat_entitlement_id=entitlement_id,
                    revenuecat_original_transaction_id=original_transaction_id,
                    revenuecat_event_id=event_id,
                    event_metadata={'revenuecat_event_type': event_type}
                )
                return success, message, None
            subscription.auto_renew = True
            subscription.status = SubscriptionStatus.ACTIVE.value
            self.session.commit()
            return self.renew_subscription(
                user_id=user_id,
                current_period_end=expiration_at,
                revenuecat_event_id=event_id
            )

        if event_type == "CANCELLATION":
            subscription.auto_renew = False
            subscription.cancelled_at = datetime.now(timezone.utc)
            subscription.status = SubscriptionStatus.ACTIVE.value
            if expiration_at:
                if expiration_at.tzinfo is None:
                    expiration_at = expiration_at.replace(tzinfo=timezone.utc)
                subscription.current_period_end = expiration_at
            self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.id,
                event_type='revenuecat_cancelled',
                old_plan_id=subscription.plan_id,
                new_plan_id=subscription.plan_id,
                revenuecat_event_id=event_id,
                event_metadata={'revenuecat_event_type': event_type}
            )
            self.session.commit()
            # Send cancellation confirmation email
            try:
                user = self.session.query(User).filter(User.id == user_id).first()
                plan_for_email = self._get_plan(subscription.plan_id)
                plan_display_name = plan_for_email.name if plan_for_email else subscription.plan_id
                end_date_display = subscription.current_period_end.strftime('%d %B %Y') if subscription.current_period_end else '-'
                if user and user.email:
                    from services.email import EmailService
                    EmailService.send_subscription_cancellation_email(
                        to_email=user.email,
                        first_name=user.first_name,
                        subscription_name=plan_display_name,
                        subscription_end_date=end_date_display,
                    )
            except Exception as e:
                logger.error(f"Failed to send subscription cancellation email for user {user_id}: {e}")

            return True, "Subscription cancellation scheduled"

        if event_type == "BILLING_ISSUE":
            subscription.status = SubscriptionStatus.PAST_DUE.value
            if expiration_at:
                if expiration_at.tzinfo is None:
                    expiration_at = expiration_at.replace(tzinfo=timezone.utc)
                subscription.current_period_end = expiration_at
            self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.id,
                event_type='revenuecat_billing_issue',
                old_plan_id=subscription.plan_id,
                new_plan_id=subscription.plan_id,
                revenuecat_event_id=event_id,
                event_metadata={'revenuecat_event_type': event_type}
            )
            self.session.commit()
            return True, "Billing issue recorded", None

        if event_type == "EXPIRATION":
            old_plan_id = subscription.plan_id
            subscription.plan_id = 'freemium'
            subscription.tier = SubscriptionTier.FREEMIUM.value
            subscription.billing_period = None
            subscription.status = SubscriptionStatus.CANCELLED.value
            subscription.auto_renew = False
            subscription.cancelled_at = datetime.now(timezone.utc)
            # Reset freemium limits for returning user
            self._reset_freemium_limits_for_returning_user(user_id)
            subscription.revenuecat_product_id = None
            subscription.revenuecat_entitlement_id = None
            if expiration_at:
                if expiration_at.tzinfo is None:
                    expiration_at = expiration_at.replace(tzinfo=timezone.utc)
                subscription.current_period_end = expiration_at
            self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.id,
                event_type='revenuecat_expired',
                old_plan_id=old_plan_id,
                new_plan_id='freemium',
                revenuecat_event_id=event_id,
                event_metadata={'revenuecat_event_type': event_type}
            )
            self.session.commit()
            # Deliberately no subscription_notification here -- see docstring above.
            return True, "Subscription expired", None

        self._log_subscription_event(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='revenuecat_unhandled',
            old_plan_id=subscription.plan_id,
            new_plan_id=subscription.plan_id,
            revenuecat_event_id=event_id,
            event_metadata={'revenuecat_event_type': event_type}
        )
        self.session.commit()
        return True, f"Unhandled RevenueCat event type: {event_type}", None

    # ============================================
    # STRIPE WEBHOOK HANDLERS
    # ============================================
    
    def handle_stripe_subscription_updated(
        self,
        stripe_subscription_id: str,
        status: str,
        current_period_end: Optional[datetime] = None,
        cancel_at_period_end: bool = False
    ) -> Tuple[bool, str]:
        """
        Handle Stripe subscription.updated webhook event.
        
        Returns: (success, message)
        """
        subscription = self.session.query(UserSubscription).filter(
            UserSubscription.stripe_subscription_id == stripe_subscription_id
        ).first()
        
        if not subscription:
            logger.warning(f"Stripe subscription not found: {stripe_subscription_id}")
            return False, "Subscription not found"
        
        # Map Stripe status to our status
        # Note: If cancel_at_period_end is True, user still has active access until period ends
        status_mapping = {
            'active': SubscriptionStatus.ACTIVE.value,
            'past_due': SubscriptionStatus.PAST_DUE.value,
            'canceled': SubscriptionStatus.CANCELLED.value,
            'unpaid': SubscriptionStatus.EXPIRED.value,
            'incomplete': SubscriptionStatus.ACTIVE.value,
            'incomplete_expired': SubscriptionStatus.EXPIRED.value,
            'trialing': SubscriptionStatus.ACTIVE.value,
        }
        
        old_status = subscription.status
        
        # If cancel_at_period_end is true, keep status ACTIVE so user retains benefits
        # They will be downgraded when subscription.deleted event fires at period end
        if cancel_at_period_end and status == 'active':
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.cancelled_at = datetime.now(timezone.utc)
        else:
            subscription.status = status_mapping.get(status, SubscriptionStatus.ACTIVE.value)
        
        if current_period_end:
            if current_period_end.tzinfo is None:
                current_period_end = current_period_end.replace(tzinfo=timezone.utc)
            subscription.current_period_end = current_period_end
        
        subscription.auto_renew = not cancel_at_period_end
        
        self._log_subscription_event(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            event_type='stripe_updated',
            old_plan_id=subscription.plan_id,
            new_plan_id=subscription.plan_id,
            event_metadata={
                'stripe_status': status,
                'old_status': old_status,
                'new_status': subscription.status
            }
        )
        
        self.session.commit()
        
        return True, f"Subscription updated to status: {subscription.status}"
    
    def handle_stripe_subscription_deleted(
        self,
        stripe_subscription_id: str
    ) -> Tuple[bool, str]:
        """
        Handle Stripe subscription.deleted webhook event.
        
        Returns: (success, message)
        """
        subscription = self.session.query(UserSubscription).filter(
            UserSubscription.stripe_subscription_id == stripe_subscription_id
        ).first()
        
        if not subscription:
            logger.warning(f"Stripe subscription not found: {stripe_subscription_id}")
            return False, "Subscription not found"
        
        old_plan_id = subscription.plan_id
        
        # Downgrade to freemium
        subscription.plan_id = 'freemium'
        subscription.tier = SubscriptionTier.FREEMIUM.value
        subscription.billing_period = None
        subscription.status = SubscriptionStatus.CANCELLED.value
        subscription.cancelled_at = datetime.now(timezone.utc)
        subscription.auto_renew = False
        subscription.stripe_subscription_id = None
        
        # Reset freemium limits for returning user
        self._reset_freemium_limits_for_returning_user(subscription.user_id)
        
        self._log_subscription_event(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            event_type='stripe_deleted',
            old_plan_id=old_plan_id,
            new_plan_id='freemium',
            event_metadata={'stripe_subscription_id': stripe_subscription_id}
        )
        
        self.session.commit()
        
        return True, "Subscription cancelled and downgraded to freemium"
    
    def handle_stripe_invoice_paid(
        self,
        stripe_subscription_id: str,
        stripe_invoice_id: str,
        amount_paid: int,
        period_end: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """
        Handle Stripe invoice.payment_succeeded webhook event.
        This triggers subscription renewal.
        
        Returns: (success, message)
        """
        subscription = self.session.query(UserSubscription).filter(
            UserSubscription.stripe_subscription_id == stripe_subscription_id
        ).first()
        
        if not subscription:
            logger.warning(f"Stripe subscription not found: {stripe_subscription_id}")
            return False, "Subscription not found"
        
        # Update subscription status
        subscription.status = SubscriptionStatus.ACTIVE.value
        
        if period_end:
            subscription.current_period_end = period_end
        
        # After first month, clear the first_month flag
        if subscription.is_first_month:
            subscription.is_first_month = False
        
        self._log_subscription_event(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            event_type='invoice_paid',
            old_plan_id=subscription.plan_id,
            new_plan_id=subscription.plan_id,
            event_metadata={
                'stripe_invoice_id': stripe_invoice_id,
                'amount_paid_cents': amount_paid
            }
        )
        
        # Trigger renewal logic (reset minutes, handle rollover)
        # Note: renew_subscription now returns a 3-tuple (success, message, notification_data).
        # This caller is dead code (no route invokes handle_stripe_invoice_paid -- confirmed
        # separately), so the extra return value is simply unused here, same as before.
        self.renew_subscription(subscription.user_id)
        
        self.session.commit()
        
        return True, "Invoice paid, subscription renewed"
    
    def handle_stripe_invoice_failed(
        self,
        stripe_subscription_id: str,
        stripe_invoice_id: str,
        attempt_count: int
    ) -> Tuple[bool, str]:
        """
        Handle Stripe invoice.payment_failed webhook event.
        
        Returns: (success, message)
        """
        subscription = self.session.query(UserSubscription).filter(
            UserSubscription.stripe_subscription_id == stripe_subscription_id
        ).first()
        
        if not subscription:
            logger.warning(f"Stripe subscription not found: {stripe_subscription_id}")
            return False, "Subscription not found"
        
        subscription.status = SubscriptionStatus.PAST_DUE.value
        
        self._log_subscription_event(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            event_type='invoice_failed',
            old_plan_id=subscription.plan_id,
            new_plan_id=subscription.plan_id,
            event_metadata={
                'stripe_invoice_id': stripe_invoice_id,
                'attempt_count': attempt_count
            }
        )
        
        self.session.commit()
        
        # TODO: Send notification to user about payment failure
        
        return True, f"Payment failed (attempt {attempt_count}), status set to past_due"
    
    def get_user_device_tokens(self, user_id: uuid.UUID) -> list:
        """Get active device tokens for a user"""
        devices = self.session.query(DeviceToken).filter(
            DeviceToken.user_id == user_id,
            DeviceToken.is_active == True
        ).all()
        return [device.device_token for device in devices]
    
    def get_suggested_plan(self, user_id: uuid.UUID) -> str:
        """Get suggested plan for upgrade based on current tier"""
        subscription = self.get_or_create_subscription(user_id)
        if subscription.tier == SubscriptionTier.FREEMIUM.value:
            return "Standard Monthly"
        elif subscription.tier == SubscriptionTier.STANDARD.value:
            return "Premium Monthly"
        return "Standard Monthly"
    
    # ============================================
    # REVENUECAT SYNC (APP → BACKEND)
    # ============================================

    def sync_from_revenuecat(
        self,
        user_id: uuid.UUID,
        app_user_id: str,
        plan: Optional[str],
        entitlement_id: Optional[str],
        package_id: Optional[str],
        is_active: bool,
        expires_at: Optional[str],
        will_renew: bool,
        last_event: Optional[str] = None,
        price_paid: Optional[float] = None,
        currency: Optional[str] = None,
        transaction_id: Optional[str] = None,
    ) -> Tuple[bool, str, Optional['UserSubscription']]:
        """
        Sync subscription state from RevenueCat SDK on app login.
        
        This acts as a reconciliation mechanism - backend ALWAYS trusts
        RevenueCat entitlement over its own state.
        
        Args:
            user_id: LawNeeds user ID
            app_user_id: RevenueCat anonymous user ID
            plan: Current plan ('standard', 'premium', or None for freemium)
            entitlement_id: RevenueCat entitlement ID
            package_id: RevenueCat package ID (e.g., 'standard:standard-monthly')
            is_active: Whether subscription is currently active
            expires_at: ISO timestamp of subscription expiration
            will_renew: Whether subscription will auto-renew
            last_event: Last RevenueCat event type
        
        Returns:
            Tuple of (success, message, updated_subscription)
        """
        try:
            subscription = self.get_or_create_subscription(user_id)
            now = datetime.now(timezone.utc)
            
            # Parse expiration date
            expiration_dt = None
            if expires_at:
                try:
                    iso_value = expires_at.replace("Z", "+00:00")
                    expiration_dt = datetime.fromisoformat(iso_value)
                    if expiration_dt.tzinfo is None:
                        expiration_dt = expiration_dt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid expires_at format: {expires_at} - {e}")
            
            # Determine target plan from package_id or entitlement
            target_plan_id = self._resolve_plan_from_sync(plan, entitlement_id, package_id)
            
            # Handle case where is_active but we can't determine the exact plan
            if is_active and not target_plan_id:
                # User has active subscription in RevenueCat but we can't determine the plan
                # Keep existing subscription state, but log warning
                logger.warning(
                    f"User {user_id} has active RevenueCat subscription but package_id not provided. "
                    f"Cannot determine exact plan (weekly/monthly/annual). "
                    f"Keeping existing subscription: {subscription.plan_id}. "
                    f"FE should pass package_id from CustomerInfo.activeSubscriptions."
                )
                # If user already has a paid subscription, keep it
                if subscription.plan_id != 'freemium':
                    self.session.commit()
                    return True, "Sync incomplete: package_id required to update plan. Current plan retained.", subscription
            
            # Always update RevenueCat fields
            subscription.revenuecat_app_user_id = app_user_id
            if entitlement_id:
                subscription.revenuecat_entitlement_id = entitlement_id
            
            # Extract product_id from package_id (format: "tier:product_id")
            if package_id and ":" in package_id:
                product_id = package_id.split(":", 1)[1]
                subscription.revenuecat_product_id = product_id
            
            old_plan_id = subscription.plan_id
            old_status = subscription.status
            changes_made = []
            newly_purchased_plan = None
            
            if is_active and target_plan_id and target_plan_id != 'freemium':
                # User has an active paid subscription in RevenueCat
                
                # Check if plan needs update
                if subscription.plan_id != target_plan_id:
                    plan_record = self._get_plan(target_plan_id)
                    if plan_record:
                        subscription.plan_id = target_plan_id
                        subscription.tier = plan_record.tier
                        subscription.billing_period = plan_record.billing_period
                        changes_made.append(f"plan: {old_plan_id} → {target_plan_id}")
                        
                        # Update minutes for new plan
                        self._update_minutes_for_plan(user_id, plan_record)
                        
                        # Reset monthly usage on upgrade so freemium usage doesn't carry over
                        # This gives users a fresh start (0/12 instead of 2/12)
                        self._reset_monthly_usage_on_upgrade(user_id)

                        # This is a genuine plan change while active - candidate for
                        # the purchase confirmation email, if purchase data is present.
                        newly_purchased_plan = plan_record
                                
                # Update status
                if subscription.status != SubscriptionStatus.ACTIVE.value:
                    subscription.status = SubscriptionStatus.ACTIVE.value
                    changes_made.append(f"status: {old_status} → active")
                
                # Update expiration
                if expiration_dt:
                    subscription.current_period_end = expiration_dt
                
                # Update auto-renew and cancellation timestamp
                subscription.auto_renew = will_renew
                if will_renew:
                    subscription.cancelled_at = None  # Clear cancellation if user reactivates
                elif not subscription.cancelled_at:
                    subscription.cancelled_at = now  # Set cancellation time if not already set
                
            else:
                # No active subscription in RevenueCat - check if should downgrade
                
                if subscription.plan_id != 'freemium':
                    # Update current_period_end from expires_at if provided
                    # This ensures we use RevenueCat's expiration time, not stale DB value
                    if expiration_dt:
                        subscription.current_period_end = expiration_dt
                    
                    # Check if current period has actually ended
                    period_ended = False
                    if subscription.current_period_end:
                        period_end = subscription.current_period_end
                        if period_end.tzinfo is None:
                            period_end = period_end.replace(tzinfo=timezone.utc)
                        period_ended = now > period_end
                    
                    if period_ended:
                        # Period has actually ended - downgrade to freemium
                        subscription.plan_id = 'freemium'
                        subscription.tier = SubscriptionTier.FREEMIUM.value
                        subscription.billing_period = None
                        subscription.status = SubscriptionStatus.EXPIRED.value
                        subscription.auto_renew = False
                        # Reset freemium limits for returning user
                        self._reset_freemium_limits_for_returning_user(user_id)
                        changes_made.append(f"plan: {old_plan_id} → freemium (period expired)")
                    elif not is_active:
                        # Subscription cancelled but period not ended yet
                        # User keeps access until period ends - just mark as cancelled
                        subscription.auto_renew = False
                        subscription.cancelled_at = subscription.cancelled_at or now
                        # Keep status as ACTIVE so user retains benefits until period end
                        if subscription.status == SubscriptionStatus.ACTIVE.value:
                            changes_made.append("marked as cancelled (benefits retained until period end)")
                        else:
                            subscription.status = SubscriptionStatus.ACTIVE.value
                            changes_made.append(f"status: {old_status} → active (benefits retained until period end)")
            
            # Log sync event
            self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.id,
                event_type='app_sync',
                old_plan_id=old_plan_id,
                new_plan_id=subscription.plan_id,
                event_metadata={
                    'app_user_id': app_user_id,
                    'entitlement_id': entitlement_id,
                    'package_id': package_id,
                    'is_active': is_active,
                    'will_renew': will_renew,
                    'last_event': last_event,
                    'changes': changes_made if changes_made else ['no_changes']
                }
            )
            
            self.session.commit()
            
            self.session.refresh(subscription)

            # Send purchase confirmation email - only when this sync represents
            # a genuine new/changed paid plan AND carries purchase data (i.e. this
            # is the immediate post-purchase sync, not a generic login sync).
            if newly_purchased_plan and transaction_id:
                self._send_purchase_confirmation_email(
                    user_id=user_id,
                    subscription=subscription,
                    plan_record=newly_purchased_plan,
                    price_paid=price_paid,
                    currency=currency,
                    transaction_id=transaction_id,
                )
            
            if changes_made:
                message = f"Subscription synced: {', '.join(changes_made)}"
                logger.info(f"User {user_id} subscription synced: {changes_made}")
            else:
                message = "Subscription already in sync"
            
            return True, message, subscription
            
        except Exception as e:
            self.session.rollback()
            logger.error(f"Error syncing subscription for user {user_id}: {e}")
            return False, f"Sync error: {str(e)}", None

    def _send_purchase_confirmation_email(
        self,
        user_id: uuid.UUID,
        subscription: 'UserSubscription',
        plan_record: 'SubscriptionPlan',
        price_paid: Optional[float],
        currency: Optional[str],
        transaction_id: str,
    ) -> None:
        """
        Fire the subscription purchase confirmation email + PDF receipt.
        Failures here are logged but never raised - a failed email must not
        break the sync response the app is waiting on.
        """
        try:
            from services.email import EmailService

            user = self.session.query(User).filter(User.id == user_id).first()
            if not user or not user.email:
                logger.warning(f"Cannot send purchase confirmation email - no email on file for user {user_id}")
                return

            now = datetime.now(timezone.utc)
            renewal_date = None
            if subscription.current_period_end:
                period_end = subscription.current_period_end
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=timezone.utc)
                renewal_date = period_end.strftime("%d %B %Y")

            EmailService.send_subscription_purchase_confirmation_email(
                to_email=user.email,
                first_name=getattr(user, "first_name", None),
                subscription_name=plan_record.name,
                billing_cycle=plan_record.billing_period,
                minutes_included=plan_record.minutes_included,
                renewal_date=renewal_date,
                transaction_id=transaction_id,
                payment_date=now.strftime("%d %B %Y"),
                total_paid=price_paid,
                currency=currency,
            )
        except Exception as e:
            logger.error(f"Failed to send purchase confirmation email for user {user_id}: {e}")

    def _send_wallet_topup_confirmation_email(
        self,
        user_id: uuid.UUID,
        transaction_id: str,
        minutes_added: int,
        total_paid,
    ) -> None:
        """
        Fire the wallet top-up confirmation email + PDF receipt.
        Failures here are logged but never raised - a failed email must not
        break the sync/webhook response the caller is waiting on.
        """
        try:
            from services.email import EmailService

            user = self.session.query(User).filter(User.id == user_id).first()
            if not user or not user.email:
                logger.warning(f"Cannot send wallet top-up confirmation email - no email on file for user {user_id}")
                return

            account = self._create_minutes_account(user_id)
            updated_balance = account.top_up_minutes

            now = datetime.now(timezone.utc)

            EmailService.send_wallet_topup_confirmation_email(
                to_email=user.email,
                first_name=getattr(user, "first_name", None),
                minutes_added=minutes_added,
                updated_balance=updated_balance,
                transaction_id=transaction_id,
                payment_date=now.strftime("%d %B %Y"),
                total_paid=total_paid,
                currency="GBP",
            )
        except Exception as e:
            logger.error(f"Failed to send wallet top-up confirmation email for user {user_id}: {e}")
    
    
    
    def _resolve_plan_from_sync(
        self,
        plan: Optional[str],
        entitlement_id: Optional[str],
        package_id: Optional[str]
    ) -> Optional[str]:
        """
        Resolve the plan ID from sync data.
        
        Priority:
        1. package_id (most specific - includes billing period)
        2. plan name (if it's a full plan ID like 'standard-weekly')
        3. entitlement_id - WARNING: cannot determine billing period, requires package_id
        
        IMPORTANT: package_id MUST be passed to correctly identify weekly vs monthly plans.
        entitlement_id alone only gives us the tier (standard/premium), not the billing period.
        """
        # Try package_id first (format: "tier:product_id" e.g., "standard:standard-weekly")
        # This is the ONLY reliable way to get the exact plan with billing period
        if package_id:
            if ":" in package_id:
                product_id = package_id.split(":", 1)[1]
                if product_id in SUBSCRIPTION_PLANS:
                    logger.info(f"Resolved plan from package_id: {package_id} -> {product_id}")
                    return product_id
            # Try package_id as-is (might be the product_id directly)
            if package_id in SUBSCRIPTION_PLANS:
                logger.info(f"Resolved plan from package_id (direct): {package_id}")
                return package_id
        
        # Try plan name directly (if FE passes full plan ID like 'standard-weekly')
        if plan:
            plan_lower = plan.lower()
            # Direct match to plan ID
            if plan_lower in SUBSCRIPTION_PLANS:
                logger.info(f"Resolved plan from plan name: {plan_lower}")
                return plan_lower
            # Try common variations (e.g., 'standard_weekly' -> 'standard-weekly')
            plan_normalized = plan_lower.replace("_", "-")
            if plan_normalized in SUBSCRIPTION_PLANS:
                logger.info(f"Resolved plan from normalized plan name: {plan_normalized}")
                return plan_normalized
        
        # entitlement_id alone cannot determine billing period (weekly vs monthly vs annual)
        # Log warning and return None - FE must pass package_id
        if entitlement_id:
            logger.warning(
                f"Cannot resolve exact plan from entitlement_id '{entitlement_id}' alone. "
                f"package_id is required to determine billing period (weekly/monthly/annual). "
                f"FE should pass package_id from RevenueCat CustomerInfo."
            )
            # DO NOT default to monthly - this causes bugs with weekly plans
            return None
        
        # Try plan as tier name (last resort - still can't determine billing period)
        if plan:
            plan_lower = plan.lower()
            # Check if it's just a tier name like 'standard' or 'premium'
            tier_names = ['standard', 'premium']
            if plan_lower in tier_names:
                logger.warning(
                    f"Plan '{plan}' is just a tier name without billing period. "
                    f"Cannot determine if weekly/monthly/annual. FE should pass package_id."
                )
                return None
            
        return None

    def send_annual_subscriber_benefits_reminder(self, user_id: uuid.UUID) -> Tuple[bool, Optional[Dict]]:
        """
        Send reminder notification to annual subscribers about their free consultation minutes.
        Returns True if notification was sent, False otherwise.
        """
        try:
            from services.firebase_notification_service import FirebaseNotificationService
            
            subscription = self.get_or_create_subscription(user_id)
            plan = self._get_plan(subscription.plan_id)
            
            # Only send to annual subscribers
            if not plan or plan.billing_period != BillingPeriod.ANNUAL.value:
                return False, None
            
            # Check if user has consultation credits
            credit = self.session.query(AnnualConsultationCredit).filter(
                AnnualConsultationCredit.user_id == user_id,
                AnnualConsultationCredit.minutes_remaining > 0
            ).first()
            
            if not credit:
                return False, None
            
            # Get user's device tokens
            device_tokens = self.get_user_device_tokens(user_id)
            
            if not device_tokens:
                logger.debug(f"No active device tokens for user {user_id}")
                return False, None
            
            firebase_service = FirebaseNotificationService()
            
            # Determine benefit message based on plan
            if subscription.tier == SubscriptionTier.STANDARD.value:
                title = "Your Annual Benefit 🎁"
                body = f"You have {credit.minutes_remaining} minutes of free consultation remaining. Use them for your legal needs!"
            elif subscription.tier == SubscriptionTier.PREMIUM.value:
                title = "Your Annual Benefit 🎁"
                body = f"You have {credit.minutes_remaining} minutes of free consultation remaining. Use them for your legal needs!"
            else:
                return False, None
            
            data = {
                'type': 'annual_benefits_reminder',
                'screen': 'Referrals',
                'action': 'create_referral',
                'minutes_remaining': str(credit.minutes_remaining)
            }
            
            # Return data for async sending (caller should handle async)
            # Store notification data to be sent by API layer
            logger.info(f"Prepared annual benefits reminder for user {user_id}")
            return True, {
                'device_tokens': device_tokens,
                'title': title,
                'body': body,
                'data': data
            }
        except Exception as e:
            logger.error(f"Error preparing annual benefits reminder: {e}")
            return False, None
