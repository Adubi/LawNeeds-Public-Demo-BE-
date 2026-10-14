# jobs/__init__.py
"""
LawNeeds Subscription Jobs Module

This module provides scheduled background tasks for the subscription system:
- Daily pass reset at midnight
- Monthly usage cap reset on 1st of each month
- Subscription expiration processing
- Trial expiration cleanup
- Usage alert notifications
- Identity verification reminders (1st & last Monday of each month)
- Identity verification abandoned check (daily)
- Consultation booking pending-action reminders (24h + 72h)
- We Miss You re-engagement reminder (14-day stage only)

The scheduler uses APScheduler with AsyncIOScheduler for async compatibility
with FastAPI.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from jobs.call_reconnect_jobs import expire_stale_disconnects

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: AsyncIOScheduler = None


def get_scheduler() -> AsyncIOScheduler:
    """Get or create the scheduler instance"""
    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler()
    return scheduler


def start_scheduler(db, consultation_api):
    """
    Start the scheduler with all subscription jobs.
    
    Args:
        db: Database instance for job database access
    """
    from jobs.subscription_jobs import (
        reset_expired_daily_passes,
        reset_monthly_usage_caps,
        process_expiring_subscriptions,
        cleanup_expired_trials,
        send_usage_alert_notifications,
        reset_daily_ad_watch_counters,
        expire_annual_consultation_credits,
        send_annual_credit_reminders,
        send_subscription_renewal_reminders,
        send_subscription_expires_today_notifications,
    )
    from jobs.consultation_extension_jobs import expire_consultation_extensions
    from jobs.referral_expiry_jobs import expire_pending_referrals
    from jobs.missed_consultation_jobs import sweep_missed_consultations
    from jobs.call_expiry_jobs import expire_stale_calls
    from jobs.response_rate_jobs import check_lawyer_response_rates
    from jobs.identity_verification_jobs import (
        send_identity_verification_reminders,
        send_identity_verification_abandoned_check,
    )
    from jobs.consultation_booking_reminder_jobs import send_consultation_booking_reminders
    from jobs.registration_completion_reminder_jobs import send_registration_completion_reminders 
    from jobs.we_miss_you_jobs import send_we_miss_you_reminders, send_we_miss_you_professional_reminders, send_inactive_user_push_reminders, send_welcome_back_in_app_notifications    
    from jobs.lawyer_weekly_summary_jobs import send_lawyer_weekly_summaries
    from jobs.app_update_jobs import send_app_update_notifications
    
    global scheduler
    scheduler = get_scheduler()
    
    # Skip if already running
    if scheduler.running:
        logger.info("Scheduler already running, skipping startup")
        return scheduler
    
    # ============================================
    # DAILY JOBS
    # ============================================
    
    # Reset expired daily passes at midnight (00:05 to avoid exact midnight conflicts)
    scheduler.add_job(
        reset_expired_daily_passes,
        trigger=CronTrigger(hour=0, minute=5),
        args=[db],
        id='reset_daily_passes',
        name='Reset Expired Daily Passes',
        replace_existing=True,
        misfire_grace_time=3600,  # 1 hour grace time
    )
    logger.info("Scheduled: reset_expired_daily_passes at 00:05 daily")
    
    # Reset daily ad watch counters at midnight
    scheduler.add_job(
        reset_daily_ad_watch_counters,
        trigger=CronTrigger(hour=0, minute=10),
        args=[db],
        id='reset_ad_counters',
        name='Reset Daily Ad Watch Counters',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: reset_daily_ad_watch_counters at 00:10 daily")
    
    # Cleanup expired trials at midnight
    scheduler.add_job(
        cleanup_expired_trials,
        trigger=CronTrigger(hour=0, minute=15),
        args=[db],
        id='cleanup_trials',
        name='Cleanup Expired Trials',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: cleanup_expired_trials at 00:15 daily")
    
    # Send usage alert notifications at 6:00 AM
    scheduler.add_job(
        send_usage_alert_notifications,
        trigger=CronTrigger(hour=6, minute=0),
        args=[db],
        id='usage_alerts',
        name='Send Usage Alert Notifications',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_usage_alert_notifications at 06:00 daily")
    
    # Check lawyer response rates and warn if below threshold (see
    # jobs/response_rate_jobs.py for the rolling-window/threshold policy)
    scheduler.add_job(
        check_lawyer_response_rates,
        trigger=CronTrigger(hour=7, minute=0),
        args=[db],
        id='check_lawyer_response_rates',
        name='Check Lawyer Response Rates',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: check_lawyer_response_rates at 07:00 daily")
    
    
    
    # ============================================
    # HOURLY JOBS
    # ============================================
    
    # Process expiring subscriptions every hour
    scheduler.add_job(
        process_expiring_subscriptions,
        trigger=CronTrigger(minute=30),  # Every hour at :30
        args=[db],
        id='process_expiring',
        name='Process Expiring Subscriptions',
        replace_existing=True,
        misfire_grace_time=1800,  # 30 min grace time
    )
    logger.info("Scheduled: process_expiring_subscriptions every hour at :30")
    
    
    
    # Send registration-completion reminders for signups that started
    # /create_user or /create_pro_account but never finished OTP
    # verification (24h + 72h follow-up; both stages handled in one daily
    # morning run, gating logic lives in
    # jobs/registration_completion_reminder_jobs.py)
    scheduler.add_job(
        send_registration_completion_reminders,
        trigger=CronTrigger(hour=9, minute=0),  # Daily at 09:00
        args=[db],
        id='registration_completion_reminders',
        name='Registration Completion Reminders',
        replace_existing=True,
        misfire_grace_time=1800,  # 30 min grace time
    )
    logger.info("Scheduled: send_registration_completion_reminders daily at 09:00")
    

    # Send pending-action reminders for abandoned casework consultation
    # bookings (24h + 72h follow-up; both stages handled in one daily
    # morning run, gating logic lives in jobs/consultation_booking_reminder_jobs.py)
    scheduler.add_job(
        send_consultation_booking_reminders,
        trigger=CronTrigger(hour=9, minute=0),  # Daily at 09:00
        args=[db],
        id='consultation_booking_reminders',
        name='Consultation Booking Pending Action Reminders',
        replace_existing=True,
        misfire_grace_time=1800,  # 30 min grace time
    )
    logger.info("Scheduled: send_consultation_booking_reminders daily at 09:00")

    scheduler.add_job(
        expire_consultation_extensions,
        trigger=IntervalTrigger(seconds=30),
        args=[db],
        id="expire_consultation_extensions",
        name="Expire Consultation Extension Requests",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    logger.info("Scheduled: expire_consultation_extensions every 30 seconds")

    scheduler.add_job(
        expire_pending_referrals,
        trigger=IntervalTrigger(minutes=5),
        args=[db],
        id="expire_pending_referrals",
        name="Expire Pending Referrals",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    
    logger.info("Scheduled: expire_pending_referrals every 5 minutes")
    
    
    
    scheduler.add_job(
        sweep_missed_consultations,
        trigger=IntervalTrigger(minutes=5),
        args=[db],
        id="sweep_missed_consultations",
        name="Sweep Missed Consultations",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info("Scheduled: sweep_missed_consultations every 5 minutes")

    scheduler.add_job(
        expire_stale_calls,
        trigger=IntervalTrigger(seconds=15),
        args=[db, consultation_api],
        id="expire_stale_calls",
        name="Expire Stale Calls",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    logger.info("Scheduled: expire_stale_calls every 15 seconds")
    
    scheduler.add_job(
        expire_stale_disconnects,
        trigger=IntervalTrigger(seconds=15),
        args=[db, consultation_api],
        id="expire_stale_disconnects",
        name="Expire Stale Disconnects",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    logger.info("Scheduled: expire_stale_disconnects every 15 seconds")

    
    # ============================================
    # MONTHLY JOBS
    # ============================================
    
    # Reset monthly usage caps on 1st of each month at 00:01
    scheduler.add_job(
        reset_monthly_usage_caps,
        trigger=CronTrigger(day=1, hour=0, minute=1),
        args=[db],
        id='reset_monthly_caps',
        name='Reset Monthly Usage Caps',
        replace_existing=True,
        misfire_grace_time=86400,  # 24 hour grace time (important for monthly job)
    )
    logger.info("Scheduled: reset_monthly_usage_caps on 1st of month at 00:01")
    
    # Expire annual consultation credits on 1st of each month at 00:30
    scheduler.add_job(
        expire_annual_consultation_credits,
        trigger=CronTrigger(day=1, hour=0, minute=30),
        args=[db],
        id='expire_annual_credits',
        name='Expire Annual Consultation Credits',
        replace_existing=True,
        misfire_grace_time=86400,
    )
    logger.info("Scheduled: expire_annual_consultation_credits on 1st of month at 00:30")
    
    # Send annual credit reminders on 15th of each month at 10:00 AM
    scheduler.add_job(
        send_annual_credit_reminders,
        trigger=CronTrigger(day=15, hour=10, minute=0),
        args=[db],
        id='annual_credit_reminders',
        name='Send Annual Credit Reminders',
        replace_existing=True,
        misfire_grace_time=86400,
    )
    logger.info("Scheduled: send_annual_credit_reminders on 15th of month at 10:00")

    # Send subscription renewal reminders daily at 09:00 AM
    # 7-day warning
    scheduler.add_job(
        send_subscription_renewal_reminders,
        trigger=CronTrigger(hour=9, minute=0),
        args=[db],
        kwargs={'days_before': 7},
        id='renewal_reminder_7_days',
        name='Subscription Renewal Reminder (7 days)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_subscription_renewal_reminders (7 days) at 09:00 daily")

    # 3-day warning
    scheduler.add_job(
        send_subscription_renewal_reminders,
        trigger=CronTrigger(hour=9, minute=5),
        args=[db],
        kwargs={'days_before': 3},
        id='renewal_reminder_3_days',
        name='Subscription Renewal Reminder (3 days)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_subscription_renewal_reminders (3 days) at 09:05 daily")

    # Notify users whose subscription expires or renews today
    scheduler.add_job(
        send_subscription_expires_today_notifications,
        trigger=CronTrigger(hour=9, minute=18),
        args=[db],
        id='subscription_expires_today',
        name='Subscription Expires Today Notification',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_subscription_expires_today_notifications at 09:18 daily")

    # Send identity verification reminder -- checked daily, only actually
    # sends on the 1st and last Monday of the month (gating logic lives in
    # jobs/identity_verification_jobs.py since APScheduler cron can't
    # express "last Monday of month" directly). Also fires the
    # verification_not_commenced in-app/push notification on those same
    # run days.
    scheduler.add_job(
        send_identity_verification_reminders,
        trigger=CronTrigger(hour=9, minute=10),
        args=[db],
        id='identity_verification_reminder',
        name='Identity Verification Reminder (1st & last Monday)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_identity_verification_reminders at 09:10 daily (fires 1st & last Monday)")

    # Check for abandoned identity verifications (status stuck at
    # in_progress with no activity for 7+ days) -- runs every day, no
    # 1st/last-Monday gate, so abandonment is caught within a day of
    # crossing the threshold. See jobs/identity_verification_jobs.py.
    scheduler.add_job(
        send_identity_verification_abandoned_check,
        trigger=CronTrigger(hour=9, minute=20),
        args=[db],
        id='identity_verification_abandoned_check',
        name='Identity Verification Abandoned Check',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_identity_verification_abandoned_check at 09:20 daily")

    # Send We Miss You re-engagement email (14-day stage only -- see
    # jobs/we_miss_you_jobs.py for why 30/60/90-day stages aren't built
    # yet). Runs once daily; eligibility is based on User.last_seen_at.
    scheduler.add_job(
        send_we_miss_you_reminders,
        trigger=CronTrigger(hour=9, minute=15),
        args=[db],
        id='we_miss_you_14d_reminder',
        name='We Miss You Reminder (14 days)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_we_miss_you_reminders at 09:15 daily")
    
    
    
    
    # Send We Miss You re-engagement email for PROFESSIONAL accounts
    # (14-day stage only, professional_type IS NOT NULL). Mirrors the
    # client job above -- see jobs/we_miss_you_jobs.py.
    scheduler.add_job(
        send_we_miss_you_professional_reminders,
        trigger=CronTrigger(hour=9, minute=15),
        args=[db],
        id='we_miss_you_professional_14d_reminder',
        name='We Miss You Professional Reminder (14 days)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_we_miss_you_professional_reminders at 09:15 daily")
    

    # Send inactive_user push notification (14-day stage, push equivalent
    # of the We Miss You email above). Uses the independent
    # inactive_user_push_sent_at dedup column -- see
    # jobs/we_miss_you_jobs.py -- so it succeeds/fails/re-fires on its own
    # schedule regardless of the email job's outcome.
    scheduler.add_job(
        send_inactive_user_push_reminders,
        trigger=CronTrigger(hour=9, minute=16),
        args=[db],
        id='inactive_user_push_14d_reminder',
        name='Inactive User Push Reminder (14 days)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_inactive_user_push_reminders at 09:16 daily")
    
    
    
    
    # Send welcome_back in-app notification (14-day stage, in-app equivalent
    # of the email/push above). Uses the independent
    # welcome_back_in_app_sent_at dedup column -- see
    # jobs/we_miss_you_jobs.py -- so it succeeds/fails/re-fires on its own
    # schedule regardless of the email or push job's outcome.
    scheduler.add_job(
        send_welcome_back_in_app_notifications,
        trigger=CronTrigger(hour=9, minute=17),
        args=[db],
        id='welcome_back_in_app_14d_reminder',
        name='Welcome Back In-App Reminder (14 days)',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_welcome_back_in_app_notifications at 09:17 daily")
    
    

    # Send app_update_available notification (push + in-app) for devices
    # running an app version older than AppSetting's
    # latest_ios_version / latest_android_version. See
    # jobs/app_update_jobs.py for the 48h re-notify cooldown policy.
    scheduler.add_job(
        send_app_update_notifications,
        trigger=CronTrigger(hour=9, minute=17),
        args=[db],
        id='app_update_available_reminder',
        name='App Update Available Reminder',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_app_update_notifications at 09:17 daily")
    
    # Send lawyer weekly activity summary (in-app only, no push - see
    # jobs/lawyer_weekly_summary_jobs.py). Runs weekly, Monday 09:30,
    # aggregating the last 7 days of completed consultations, payouts,
    # and new reviews. Lawyers with zero activity are skipped.
    scheduler.add_job(
        send_lawyer_weekly_summaries,
        trigger=CronTrigger(day_of_week='mon', hour=9, minute=30),
        args=[db],
        id='lawyer_weekly_summary',
        name='Lawyer Weekly Summary',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled: send_lawyer_weekly_summaries every Monday at 09:30")

    # Start the scheduler
    scheduler.start()
    logger.info("✅ Subscription job scheduler started successfully")
    
    return scheduler


def stop_scheduler():
    """Stop the scheduler gracefully"""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped")
    scheduler = None
