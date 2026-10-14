# jobs/lawyer_weekly_summary_jobs.py
"""
LawNeeds Lawyer Weekly Summary Job

Sends a weekly in-app notification to lawyers summarizing their activity
over the past 7 days: consultations completed, earnings, and new reviews.

In-app only (no push), per the notification spec. Runs weekly via
APScheduler, following the pattern in jobs/subscription_jobs.py.

Lawyers with zero activity in the window are skipped - an empty summary
adds no value and risks feeling like noise.
"""

import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)

REVIEWABLE_TYPES = ["Lawyer", "Law Firm", "A2J Partners"]


async def send_lawyer_weekly_summaries(db):
    """
    Send weekly activity summary notifications to lawyers.

    Runs weekly (Monday 09:30). Aggregates the last 7 days of:
    - Completed consultations
    - Payouts transferred (earnings)
    - New reviews received
    """
    logger.info("Starting job: send_lawyer_weekly_summaries")

    try:
        from services.models import (
            User,
            ReferralSession,
            ConsultationPayment,
            ConsultationPaymentStatus,
            LawyerReview,
        )
        from services.notification_service import NotificationService

        window_start = datetime.now(timezone.utc) - timedelta(days=7)
        notified_count = 0

        with db.get_session() as session:
            lawyers = session.query(User).filter(
                User.professional_type.in_(REVIEWABLE_TYPES)
            ).all()

            for lawyer in lawyers:
                try:
                    consultations_completed = session.query(ReferralSession).filter(
                        ReferralSession.lawyer_id == lawyer.id,
                        ReferralSession.session_status == 'completed',
                        ReferralSession.timer_ended_at >= window_start,
                    ).count()

                    payouts = session.query(ConsultationPayment).filter(
                        ConsultationPayment.lawyer_id == lawyer.id,
                        ConsultationPayment.payment_status == ConsultationPaymentStatus.TRANSFERRED.value,
                        ConsultationPayment.transferred_at >= window_start,
                    ).all()
                    earnings_pence = sum(
                        (p.lawyer_payout_amount_pence or 0) for p in payouts
                    )

                    new_reviews = session.query(LawyerReview).filter(
                        LawyerReview.lawyer_id == lawyer.id,
                        LawyerReview.created_at >= window_start,
                    ).count()

                    if consultations_completed == 0 and earnings_pence == 0 and new_reviews == 0:
                        continue

                    earnings_gbp = f"{(Decimal(earnings_pence) / Decimal('100')).quantize(Decimal('0.01'))}"

                    parts = []
                    if consultations_completed:
                        parts.append(f"{consultations_completed} consultation{'s' if consultations_completed != 1 else ''} completed")
                    if earnings_pence:
                        parts.append(f"GBP {earnings_gbp} earned")
                    if new_reviews:
                        parts.append(f"{new_reviews} new review{'s' if new_reviews != 1 else ''}")

                    body = "This week: " + ", ".join(parts)

                    NotificationService(session).create(
                        recipient_id=lawyer.id,
                        title="Your Weekly Summary",
                        body=body,
                        notification_type="weekly_summary",
                        data={
                            "consultations_completed": consultations_completed,
                            "earnings_gbp": earnings_gbp,
                            "new_reviews": new_reviews,
                            "screen": "LawyerDashboard",
                        },
                    )
                    notified_count += 1

                except Exception as lawyer_error:
                    logger.warning(f"Failed to build weekly summary for lawyer {lawyer.id}: {lawyer_error}")
                    continue

            session.commit()
            logger.info(f"send_lawyer_weekly_summaries: Sent {notified_count} weekly summaries")

    except Exception as e:
        logger.error(f"send_lawyer_weekly_summaries failed: {e}")