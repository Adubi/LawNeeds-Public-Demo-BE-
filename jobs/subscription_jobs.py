# jobs/subscription_jobs.py
"""
LawNeeds Subscription Scheduled Jobs

This module implements all scheduled tasks for the subscription system:
- Daily pass reset
- Monthly usage reset
- Subscription expiration handling
- Trial cleanup
- Usage alert notifications

All jobs are async-compatible and designed to run with APScheduler.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ============================================
# DAILY JOBS
# ============================================

async def reset_expired_daily_passes(db):
    """
    Reset expired daily passes for freemium users.
    
    Runs at midnight (00:05) daily.
    Clears daily_pass_active flag for passes that have expired.
    """
    logger.info("Starting job: reset_expired_daily_passes")
    
    try:
        from services.models import FreemiumLimits
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            
            # Find all active daily passes that have expired
            expired_passes = session.query(FreemiumLimits).filter(
                and_(
                    FreemiumLimits.daily_pass_active == True,
                    FreemiumLimits.daily_pass_expires_at < now
                )
            ).all()
            
            count = len(expired_passes)
            
            for limits in expired_passes:
                limits.daily_pass_active = False
                limits.daily_pass_expires_at = None
                logger.debug(f"Reset daily pass for user {limits.user_id}")
            
            session.commit()
            logger.info(f"✅ reset_expired_daily_passes: Reset {count} expired daily passes")
            
    except Exception as e:
        logger.error(f"❌ reset_expired_daily_passes failed: {e}")


async def reset_daily_ad_watch_counters(db):
    """
    Reset daily ad watch counters for freemium users.
    
    Runs at midnight (00:10) daily.
    Resets ads_watched_today counter to 0.
    """
    logger.info("Starting job: reset_daily_ad_watch_counters")
    
    try:
        from services.models import FreemiumLimits
        from sqlalchemy import update
        
        with db.get_session() as session:
            # Bulk update all users with ads watched today > 0
            result = session.execute(
                update(FreemiumLimits)
                .where(FreemiumLimits.ads_watched_today > 0)
                .values(ads_watched_today=0)
            )
            
            count = result.rowcount
            session.commit()
            
            logger.info(f"✅ reset_daily_ad_watch_counters: Reset ad counters for {count} users")
            
    except Exception as e:
        logger.error(f"❌ reset_daily_ad_watch_counters failed: {e}")


async def cleanup_expired_trials(db):
    """
    Mark expired trial periods as expired.
    
    Runs at midnight (00:15) daily.
    Sets trial_expired=True for trials that have passed their expiration date.
    """
    logger.info("Starting job: cleanup_expired_trials")
    
    try:
        from services.models import FreemiumLimits
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            
            # Find all active trials that have expired
            expired_trials = session.query(FreemiumLimits).filter(
                and_(
                    FreemiumLimits.trial_expired == False,
                    FreemiumLimits.trial_expires_at.isnot(None),
                    FreemiumLimits.trial_expires_at < now
                )
            ).all()
            
            count = len(expired_trials)
            
            for limits in expired_trials:
                limits.trial_expired = True
                logger.debug(f"Marked trial expired for user {limits.user_id}")
            
            session.commit()
            logger.info(f"✅ cleanup_expired_trials: Marked {count} trials as expired")
            
    except Exception as e:
        logger.error(f"❌ cleanup_expired_trials failed: {e}")


async def send_usage_alert_notifications(db):
    """
    Send push notifications to users who have reached 85% usage threshold.
    
    Runs at 6:00 AM daily.
    Finds users at or above 85% minutes usage and sends notification.
    Only notifies users once per billing period.
    """
    logger.info("Starting job: send_usage_alert_notifications")
    
    try:
        from services.models import (
            MinutesAccount, UserSubscription, DeviceToken, UsageLog
        )
        from services.subscription_constants import (
            USAGE_ALERT_THRESHOLD_PERCENT, SubscriptionTier
        )
        from services.firebase_notification_service import FirebaseNotificationService
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            
            # Find users who:
            # 1. Are on a paid plan (have total_minutes > 0)
            # 2. Have used >= 85% of their total_minutes
            # 3. Haven't been notified this period
            
            # Get accounts at threshold
            accounts_at_threshold = session.query(MinutesAccount).filter(
                and_(
                    MinutesAccount.total_minutes > 0,
                    # Calculate usage percentage: used / total >= 0.85
                    MinutesAccount.used_minutes >= MinutesAccount.total_minutes * (USAGE_ALERT_THRESHOLD_PERCENT / 100)
                )
            ).all()
            
            if not accounts_at_threshold:
                logger.info("✅ send_usage_alert_notifications: No users at threshold")
                return
            
            notified_count = 0
            firebase_service = None
            
            for account in accounts_at_threshold:
                try:
                    # Check if we've already sent an alert this period
                    existing_alert = session.query(UsageLog).filter(
                        and_(
                            UsageLog.user_id == account.user_id,
                            UsageLog.action_type == 'usage_alert_sent',
                            UsageLog.created_at >= account.period_start
                        )
                    ).first()
                    
                    if existing_alert:
                        continue  # Already notified this period
                    
                    # Get user's device tokens
                    device_tokens = session.query(DeviceToken).filter(
                        and_(
                            DeviceToken.user_id == account.user_id,
                            DeviceToken.is_active == True
                        )
                    ).all()
                    
                    if not device_tokens:
                        continue  # No active devices
                    
                    # Calculate remaining minutes
                    available = account.total_minutes - account.used_minutes + account.rollover_minutes + account.top_up_minutes
                    usage_percent = (account.used_minutes / account.total_minutes * 100) if account.total_minutes > 0 else 0
                    
                    # Prepare notification
                    title = "Minutes Running Low ⚠️"
                    body = f"You've used {usage_percent:.0f}% of your minutes. {available} minutes remaining. Consider topping up!"
                    
                    data = {
                        'type': 'usage_alert',
                        'screen': 'SubscriptionStatus',
                        'usage_percent': str(int(usage_percent)),
                        'available_minutes': str(available)
                    }
                    
                    # Send notification
                    if firebase_service is None:
                        firebase_service = FirebaseNotificationService()
                    
                    tokens = [dt.device_token for dt in device_tokens]
                    await firebase_service.send_notification_to_multiple(
                        device_tokens=tokens,
                        title=title,
                        body=body,
                        data=data
                    )
                    
                    # Log that we sent the alert
                    alert_log = UsageLog(
                        user_id=account.user_id,
                        action_type='usage_alert_sent',
                        action_description=f'Usage alert sent at {usage_percent:.0f}%',
                        minutes_deducted=0,
                        minutes_before=available,
                        minutes_after=available,
                        status='success'
                    )
                    session.add(alert_log)
                    
                    notified_count += 1
                    logger.debug(f"Sent usage alert to user {account.user_id}")
                    
                except Exception as user_error:
                    logger.warning(f"Failed to send alert to user {account.user_id}: {user_error}")
                    continue
            
            session.commit()
            logger.info(f"✅ send_usage_alert_notifications: Sent {notified_count} notifications")
            
    except Exception as e:
        logger.error(f"❌ send_usage_alert_notifications failed: {e}")


# ============================================
# HOURLY JOBS
# ============================================

async def process_expiring_subscriptions(db):
    """
    Process subscriptions that are expiring or have expired.
    
    Runs every hour at :30.
    
    Handles:
    1. Subscriptions past their period_end with auto_renew=False -> downgrade to freemium
    2. Subscriptions with pending_downgrade_to -> apply downgrade
    3. Update status for expired subscriptions
    """
    logger.info("Starting job: process_expiring_subscriptions")
    
    try:
        from services.models import (
            UserSubscription, SubscriptionPlan, MinutesAccount,
            SubscriptionEvent, DeviceToken
        )
        from services.subscription_constants import (
            SubscriptionTier, SubscriptionStatus
        )
        from services.notification_service import NotificationService
        from services.firebase_notification_service import FirebaseNotificationService
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            firebase_service = None
            
            # ============================================
            # 1. Handle expired subscriptions (no auto-renew)
            # ============================================
            
            # Catch ALL subscriptions whose billing period has ended regardless of
            # auto_renew setting.  A subscription with auto_renew=True that wasn't
            # renewed by RevenueCat before period_end should still be expired; the
            # sync endpoint will re-activate it if RevenueCat confirms renewal later.
            expired_subs = session.query(UserSubscription).filter(
                and_(
                    UserSubscription.plan_id != 'freemium',
                    UserSubscription.current_period_end.isnot(None),
                    UserSubscription.current_period_end < now,
                    UserSubscription.status.in_([
                        SubscriptionStatus.ACTIVE.value,
                        SubscriptionStatus.CANCELLED.value
                    ])
                )
            ).all()
            
            expired_count = 0
            for sub in expired_subs:
                old_plan = sub.plan_id
                
                # Downgrade to freemium
                sub.plan_id = 'freemium'
                sub.tier = SubscriptionTier.FREEMIUM.value
                sub.billing_period = None
                sub.status = SubscriptionStatus.EXPIRED.value
                
                # Reset minutes account
                account = session.query(MinutesAccount).filter(
                    MinutesAccount.user_id == sub.user_id
                ).first()
                if account:
                    account.total_minutes = 0
                    account.used_minutes = 0
                    account.rollover_minutes = 0
                    # Keep top_up_minutes as they never expire
                
                # Log event
                event = SubscriptionEvent(
                    user_id=sub.user_id,
                    subscription_id=sub.id,
                    event_type='expired',
                    old_plan_id=old_plan,
                    new_plan_id='freemium',
                    event_metadata={'reason': 'subscription_period_ended', 'job': 'process_expiring_subscriptions'}
                )
                session.add(event)
                
                # Notify the user their subscription has expired
                try:
                    device_tokens = session.query(DeviceToken).filter(
                        and_(
                            DeviceToken.user_id == sub.user_id,
                            DeviceToken.is_active == True
                        )
                    ).all()
                    
                    if device_tokens:
                        NotificationService(session).create(
                            recipient_id=sub.user_id,
                            title="Your Subscription Has Expired",
                            body=f"Your {old_plan} plan has expired and your account has been moved to the free plan.",
                            notification_type="subscription_expired",
                            data={"subscription_id": str(sub.id), "plan_name": old_plan},
                        )
                        
                        if firebase_service is None:
                            firebase_service = FirebaseNotificationService()
                        
                        tokens = [dt.device_token for dt in device_tokens]
                        await firebase_service.send_subscription_expired_notification(
                            device_tokens=tokens,
                            plan_name=old_plan,
                            subscription_id=str(sub.id),
                        )
                except Exception as notify_error:
                    logger.warning(f"Failed to send expired notification to user {sub.user_id}: {notify_error}")
                
                expired_count += 1
                logger.debug(f"Expired subscription for user {sub.user_id}: {old_plan} -> freemium")
            
            # ============================================
            # 2. Handle pending downgrades
            # ============================================
            
            pending_downgrades = session.query(UserSubscription).filter(
                and_(
                    UserSubscription.pending_downgrade_to.isnot(None),
                    UserSubscription.current_period_end.isnot(None),
                    UserSubscription.current_period_end < now
                )
            ).all()
            
            downgrade_count = 0
            for sub in pending_downgrades:
                new_plan_id = sub.pending_downgrade_to
                old_plan_id = sub.plan_id
                
                # Get new plan
                new_plan = session.query(SubscriptionPlan).filter(
                    SubscriptionPlan.id == new_plan_id
                ).first()
                
                if new_plan:
                    sub.plan_id = new_plan_id
                    sub.tier = new_plan.tier
                    sub.billing_period = new_plan.billing_period
                    sub.pending_downgrade_to = None
                    sub.status = SubscriptionStatus.ACTIVE.value
                    
                    # Update minutes for new plan
                    account = session.query(MinutesAccount).filter(
                        MinutesAccount.user_id == sub.user_id
                    ).first()
                    if account:
                        account.total_minutes = new_plan.minutes_included
                        account.used_minutes = 0
                        # Rollover if applicable
                        if new_plan.rollover_enabled:
                            # Keep existing rollover + unused top-ups
                            pass
                        else:
                            account.rollover_minutes = 0
                    
                    # Log event
                    event = SubscriptionEvent(
                        user_id=sub.user_id,
                        subscription_id=sub.id,
                        event_type='downgrade_applied',
                        old_plan_id=old_plan_id,
                        new_plan_id=new_plan_id,
                        event_metadata={'job': 'process_expiring_subscriptions'}
                    )
                    session.add(event)
                    
                    downgrade_count += 1
                    logger.debug(f"Applied downgrade for user {sub.user_id}: {old_plan_id} -> {new_plan_id}")
                else:
                    # Plan not found, clear the pending downgrade
                    sub.pending_downgrade_to = None
                    logger.warning(f"Pending downgrade plan not found: {new_plan_id}")
            
            # ============================================
            # 3. Update past_due subscriptions that have fully expired
            # ============================================
            
            past_due_expired = session.query(UserSubscription).filter(
                and_(
                    UserSubscription.status == SubscriptionStatus.PAST_DUE.value,
                    UserSubscription.current_period_end.isnot(None),
                    UserSubscription.current_period_end < (now - timedelta(days=7))  # 7 day grace period
                )
            ).all()
            
            past_due_count = 0
            for sub in past_due_expired:
                old_plan = sub.plan_id
                
                sub.plan_id = 'freemium'
                sub.tier = SubscriptionTier.FREEMIUM.value
                sub.billing_period = None
                sub.status = SubscriptionStatus.EXPIRED.value
                
                # Reset minutes
                account = session.query(MinutesAccount).filter(
                    MinutesAccount.user_id == sub.user_id
                ).first()
                if account:
                    account.total_minutes = 0
                    account.used_minutes = 0
                    account.rollover_minutes = 0
                
                event = SubscriptionEvent(
                    user_id=sub.user_id,
                    subscription_id=sub.id,
                    event_type='expired_past_due',
                    old_plan_id=old_plan,
                    new_plan_id='freemium',
                    event_metadata={'reason': 'past_due_grace_period_ended', 'job': 'process_expiring_subscriptions'}
                )
                session.add(event)
                
                past_due_count += 1
            
            session.commit()
            
            logger.info(
                f"✅ process_expiring_subscriptions: "
                f"Expired: {expired_count}, Downgrades: {downgrade_count}, Past Due: {past_due_count}"
            )
            
    except Exception as e:
        logger.error(f"❌ process_expiring_subscriptions failed: {e}")


# ============================================
# MONTHLY JOBS
# ============================================

async def reset_monthly_usage_caps(db):
    """
    Reset monthly usage counters for all paid subscribers.
    
    Runs on the 1st of each month at 00:01.
    
    Note: This is a fallback for users whose subscription didn't renew
    through the normal flow. The primary reset happens during renewal.
    
    For freemium users, this resets trial counters (they stay at limit).
    """
    logger.info("Starting job: reset_monthly_usage_caps")
    
    try:
        from services.models import UsageTracking, FreemiumLimits
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            current_month = now.month
            current_year = now.year
            
            # Check if usage records exist for current month
            # If they do, this job may have already run or renewals created them
            existing_count = session.query(UsageTracking).filter(
                and_(
                    UsageTracking.month == current_month,
                    UsageTracking.year == current_year
                )
            ).count()
            
            if existing_count > 0:
                logger.info(f"✅ reset_monthly_usage_caps: {existing_count} records already exist for {current_year}-{current_month:02d}")
                # The renewal process creates fresh usage records
                # This job serves as a safety net but shouldn't duplicate work
                return
            
            # Reset trial counters for freemium users who haven't expired
            # (So they get fresh trial allowances each month if trial still active)
            # Note: Based on spec, freemium trial is first month only, so this 
            # mainly serves users who signed up mid-month
            
            logger.info(f"✅ reset_monthly_usage_caps: Job completed for {current_year}-{current_month:02d}")
            
    except Exception as e:
        logger.error(f"❌ reset_monthly_usage_caps failed: {e}")


# ============================================
# ON-DEMAND JOBS (callable from API or manually)
# ============================================

async def send_subscription_expires_today_notifications(db):
    """
    Notify users whose subscription is expiring or renewing today.

    Recommend running this once daily (e.g. 09:00, alongside the other
    daily subscription jobs) -- confirm the actual registration time
    with whoever owns the scheduler config.

    Finds ACTIVE, non-freemium subscriptions whose current_period_end
    falls within today's UTC calendar day. Mirrors the query shape used
    in send_subscription_renewal_reminders, but checks "today" instead
    of an N-days-ahead window.
    """
    logger.info("Starting job: send_subscription_expires_today_notifications")

    try:
        from services.models import UserSubscription, DeviceToken
        from services.subscription_constants import SubscriptionStatus
        from services.notification_service import NotificationService
        from services.firebase_notification_service import FirebaseNotificationService

        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

            expiring_today = session.query(UserSubscription).filter(
                and_(
                    UserSubscription.plan_id != 'freemium',
                    UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                    UserSubscription.current_period_end.isnot(None),
                    UserSubscription.current_period_end >= day_start,
                    UserSubscription.current_period_end < day_end
                )
            ).all()

            if not expiring_today:
                logger.info("✅ send_subscription_expires_today_notifications: No subscriptions expiring today")
                return

            firebase_service = None
            notified_count = 0

            for sub in expiring_today:
                try:
                    device_tokens = session.query(DeviceToken).filter(
                        and_(
                            DeviceToken.user_id == sub.user_id,
                            DeviceToken.is_active == True
                        )
                    ).all()

                    if not device_tokens:
                        continue

                    NotificationService(session).create(
                        recipient_id=sub.user_id,
                        title="Your Subscription Expires Today",
                        body=f"Your {sub.plan_id} plan expires today. Renew now to avoid interruption.",
                        notification_type="subscription_expires_today",
                        data={"subscription_id": str(sub.id), "plan_name": sub.plan_id},
                    )

                    if firebase_service is None:
                        firebase_service = FirebaseNotificationService()

                    tokens = [dt.device_token for dt in device_tokens]
                    await firebase_service.send_subscription_expires_today_notification(
                        device_tokens=tokens,
                        plan_name=sub.plan_id,
                        subscription_id=str(sub.id),
                    )

                    notified_count += 1

                except Exception as user_error:
                    logger.warning(f"Failed to send expires-today notification to user {sub.user_id}: {user_error}")
                    continue

            session.commit()
            logger.info(f"✅ send_subscription_expires_today_notifications: Sent {notified_count} notifications")

    except Exception as e:
        logger.error(f"❌ send_subscription_expires_today_notifications failed: {e}")


async def send_subscription_renewal_reminders(db, days_before: int = 3):
    """
    Send renewal reminder notifications to users whose subscription 
    is expiring soon.
    
    Args:
        db: Database instance
        days_before: Number of days before expiration to send reminder
    
    This job can be scheduled or called on-demand.
    """
    logger.info(f"Starting job: send_subscription_renewal_reminders (days_before={days_before})")
    
    try:
        from services.models import UserSubscription, DeviceToken, User, SubscriptionPlan
        from services.subscription_constants import SubscriptionStatus
        from services.firebase_notification_service import FirebaseNotificationService
        from services.email import EmailService
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            reminder_window_start = now + timedelta(days=days_before - 1)
            reminder_window_end = now + timedelta(days=days_before)
            
            # Find subscriptions expiring in the reminder window
            expiring_subs = session.query(UserSubscription).filter(
                and_(
                    UserSubscription.plan_id != 'freemium',
                    UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                    UserSubscription.current_period_end.isnot(None),
                    UserSubscription.current_period_end >= reminder_window_start,
                    UserSubscription.current_period_end < reminder_window_end
                )
            ).all()
            
            if not expiring_subs:
                logger.info("✅ send_subscription_renewal_reminders: No expiring subscriptions")
                return
            
            firebase_service = FirebaseNotificationService()
            notified_count = 0
            
            for sub in expiring_subs:
                try:
                    device_tokens = session.query(DeviceToken).filter(
                        and_(
                            DeviceToken.user_id == sub.user_id,
                            DeviceToken.is_active == True
                        )
                    ).all()
                    
                    if not device_tokens:
                        continue
                    
                    if sub.auto_renew:
                        title = "Subscription Renewing Soon 🔄"
                        body = f"Your {sub.plan_id} subscription will renew in {days_before} days."
                    else:
                        title = "Subscription Expiring Soon ⏰"
                        body = f"Your {sub.plan_id} subscription expires in {days_before} days. Renew to keep your benefits!"
                    
                    data = {
                        'type': 'subscription_reminder',
                        'screen': 'SubscriptionStatus',
                        'plan_id': sub.plan_id,
                        'days_remaining': str(days_before)
                    }
                    
                    tokens = [dt.device_token for dt in device_tokens]
                    await firebase_service.send_notification_to_multiple(
                        device_tokens=tokens,
                        title=title,
                        body=body,
                        data=data
                    )
                    
                    notified_count += 1

                    # Email reminder: spec only wants this at the 3-day mark,
                    # not the 7-day push-only stage. Only sent to auto_renew
                    # users - the email template's copy ("will renew
                    # automatically using your existing payment method") is
                    # only accurate for that case. Users who have turned
                    # auto-renew off still get the push notification above,
                    # just not this email, since no "expiring" copy variant
                    # has been provided for that case.
                    if days_before == 3 and sub.auto_renew:
                        try:
                            user = session.query(User).filter(
                                User.id == sub.user_id
                            ).first()

                            if user and user.email:
                                plan = session.query(SubscriptionPlan).filter(
                                    SubscriptionPlan.id == sub.plan_id
                                ).first()
                                plan_name = plan.name if plan else sub.plan_id
                                plan_price = str(plan.price_gbp) if plan else "-"
                                renewal_date_display = (
                                    sub.current_period_end.strftime('%d %B %Y')
                                    if sub.current_period_end else "-"
                                )

                                EmailService.send_subscription_renewal_reminder_email(
                                    to_email=user.email,
                                    first_name=user.first_name,
                                    subscription_name=plan_name,
                                    renewal_date=renewal_date_display,
                                    renewal_amount=plan_price,
                                )
                        except Exception as email_error:
                            logger.error(
                                f"Failed to send renewal reminder email to user {sub.user_id}: {email_error}"
                            )

                except Exception as user_error:
                    logger.warning(f"Failed to send renewal reminder to user {sub.user_id}: {user_error}")
                    continue
            
            logger.info(f"✅ send_subscription_renewal_reminders: Sent {notified_count} reminders")
            
    except Exception as e:
        logger.error(f"❌ send_subscription_renewal_reminders failed: {e}")


async def expire_annual_consultation_credits(db):
    """
    Expire annual consultation credits that have passed their valid_until date.
    
    This can be run daily or on-demand.
    """
    logger.info("Starting job: expire_annual_consultation_credits")
    
    try:
        from services.models import AnnualConsultationCredit
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            
            # Find credits that are past their valid_until and not yet marked expired
            expired_credits = session.query(AnnualConsultationCredit).filter(
                and_(
                    AnnualConsultationCredit.expired == False,
                    AnnualConsultationCredit.valid_until < now
                )
            ).all()
            
            count = len(expired_credits)
            
            for credit in expired_credits:
                credit.expired = True
                logger.debug(f"Expired consultation credit for user {credit.user_id}")
            
            session.commit()
            logger.info(f"✅ expire_annual_consultation_credits: Expired {count} credits")
            
    except Exception as e:
        logger.error(f"❌ expire_annual_consultation_credits failed: {e}")


async def send_annual_credit_reminders(db):
    """
    Send reminders to annual subscribers who have unused consultation credits.
    
    This runs monthly to remind users to use their annual benefits before expiry.
    Sends reminders to users with:
    - Active annual subscription
    - Unused consultation credits (minutes_remaining > 0)
    - Credit not expired
    """
    logger.info("Starting job: send_annual_credit_reminders")
    
    try:
        from services.models import AnnualConsultationCredit, UserSubscription, DeviceToken
        from services.subscription_constants import BillingPeriod
        
        with db.get_session() as session:
            now = datetime.now(timezone.utc)
            
            # Find users with unused annual credits
            unused_credits = session.query(
                AnnualConsultationCredit,
                UserSubscription
            ).join(
                UserSubscription, 
                AnnualConsultationCredit.user_id == UserSubscription.user_id
            ).filter(
                and_(
                    AnnualConsultationCredit.expired == False,
                    AnnualConsultationCredit.minutes_remaining > 0,
                    AnnualConsultationCredit.valid_until > now,
                    UserSubscription.billing_period == BillingPeriod.ANNUAL.value
                )
            ).all()
            
            if not unused_credits:
                logger.info("✅ send_annual_credit_reminders: No users with unused credits")
                return
            
            notified_count = 0
            
            for credit, subscription in unused_credits:
                try:
                    # Get user's device tokens
                    device_tokens = session.query(DeviceToken.token).filter(
                        and_(
                            DeviceToken.user_id == credit.user_id,
                            DeviceToken.is_active == True
                        )
                    ).all()
                    
                    tokens = [t[0] for t in device_tokens]
                    
                    if not tokens:
                        continue
                    
                    # Calculate days until expiry
                    days_until_expiry = (credit.valid_until - now).days
                    
                    # Determine urgency of reminder
                    if days_until_expiry <= 30:
                        title = "⏰ Your Free Consultation Expires Soon!"
                        body = f"You have {credit.minutes_remaining} unused consultation minutes. Use them before they expire in {days_until_expiry} days!"
                    else:
                        title = "🎁 Don't Forget Your Free Consultation!"
                        body = f"As an annual subscriber, you have {credit.minutes_remaining} free consultation minutes available. Book a consultation with a lawyer today!"
                    
                    # Send notification
                    from services.firebase_notification_service import FirebaseNotificationService
                    firebase_service = FirebaseNotificationService()
                    
                    await firebase_service.send_notification_to_multiple(
                        device_tokens=tokens,
                        title=title,
                        body=body,
                        data={
                            'type': 'annual_credit_reminder',
                            'screen': 'AnnualCredit',
                            'action': 'view_credit',
                            'minutes_remaining': str(credit.minutes_remaining),
                            'days_until_expiry': str(days_until_expiry)
                        }
                    )
                    
                    notified_count += 1
                    logger.debug(f"Sent annual credit reminder to user {credit.user_id}")
                    
                except Exception as user_error:
                    logger.warning(f"Failed to send annual credit reminder to user {credit.user_id}: {user_error}")
                    continue
            
            logger.info(f"✅ send_annual_credit_reminders: Sent {notified_count} reminders")
            
    except Exception as e:
        logger.error(f"❌ send_annual_credit_reminders failed: {e}")