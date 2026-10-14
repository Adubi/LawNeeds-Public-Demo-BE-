"""Scheduled sweep for calls that disconnected and never reconnected in time.

Mirrors jobs/call_expiry_jobs.py's structure. Finds Call rows still
'accepted'/'connected' with disconnected_at older than
RECONNECT_GRACE_SECONDS, ends the call, resumes the consultation timer,
and returns the consultation to ACTIVE/CHAT.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.consultation_payment_service import ConsultationPaymentService
from services.models import Call, ReferralSession

logger = logging.getLogger(__name__)

# Spec 6.10 leaves this unspecified ("doc doesn't specify a number") —
# 30s chosen as a reasonable default; adjust here if that changes.
RECONNECT_GRACE_SECONDS = 30


async def expire_stale_disconnects(db, consultation_api) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECONNECT_GRACE_SECONDS)

    with db.get_session() as lookup_session:
        due_calls = (
            lookup_session.query(Call)
            .filter(
                Call.status.in_(("accepted", "connected")),
                Call.disconnected_at.isnot(None),
                Call.disconnected_at <= cutoff,
            )
            .all()
        )
        due_ids = [call.id for call in due_calls]

    if not due_ids:
        return

    logger.info(
        "Found %d call(s) past the %ds reconnect grace period.",
        len(due_ids), RECONNECT_GRACE_SECONDS,
    )

    for call_id in due_ids:
        with db.get_session() as session:
            call = (
                session.query(Call)
                .filter(Call.id == call_id)
                .with_for_update()
                .first()
            )
            if not call or call.status not in ("accepted", "connected") or call.disconnected_at is None:
                # Reconnected, ended, or handled since the lookup query ran.
                continue

            now = datetime.now(timezone.utc)
            call.status = "ended"
            call.ended_at = now
            call.failure_reason = "reconnect_grace_period_expired"
            session.flush()

            session_obj = session.query(ReferralSession).filter(
                ReferralSession.id == call.referral_session_id
            ).first()
            if session_obj:
                consultation_api._resume_paused_timer(session_obj)
                session_obj.current_communication_mode = "chat"
                session_obj.timer_paused_reason = None
                session.flush()

                payment_summary = ConsultationPaymentService(session).get_payment_summary(
                    call.referral_session_id
                )
                payload = {
                    "type": "call_reconnect_failed",
                    **consultation_api._build_timer_state(session_obj, payment_summary, session),
                }
                await consultation_api.timer_manager.broadcast(
                    str(call.referral_session_id), payload
                )

            logger.info("Ended call %s after reconnect grace period expired.", call_id)