"""Consultation timer-extension approval, pricing, payment, and payout logic."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from services.consultation_payment_service import ConsultationPaymentService
from services.models import (
    ConsultationExtensionPayment,
    ConsultationExtensionRequest,
    ConsultationExtensionStatus,
    ConsultationPaymentStatus,
    ReferralRequest,
    ReferralSession,
    User,
)
from services.stripe_service import StripeService

logger = logging.getLogger(__name__)


class ConsultationExtensionService:
    VAT_RATE = Decimal("20.00")
    COMMISSION_RATE = Decimal("5.00")
    DRAFT_TTL = timedelta(minutes=2)
    APPROVAL_TTL = timedelta(minutes=10)
    PAYMENT_TTL = timedelta(minutes=10)
    PRICE_BY_MINUTES = {5: 2_000, 10: 5_000, 20: 7_000, 30: 10_000, 60: 15_000}
    ACTIVE_STATUSES = {
        ConsultationExtensionStatus.DRAFT.value,
        ConsultationExtensionStatus.PENDING_LAWYER.value,
        ConsultationExtensionStatus.ACCEPTED.value,
        ConsultationExtensionStatus.PROPOSED.value,
        ConsultationExtensionStatus.PAYMENT_PENDING.value,
        ConsultationExtensionStatus.PAID.value,
    }

    def __init__(self, session, stripe_service: Optional[StripeService] = None):
        self.session = session
        self.stripe_service = stripe_service or StripeService()

    @staticmethod
    def _aware(value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @staticmethod
    def _money(pence: int) -> str:
        return f"{Decimal(pence) / Decimal('100'):.2f}"

    @classmethod
    def price_breakdown(cls, minutes: int) -> Dict[str, Any]:
        if minutes not in cls.PRICE_BY_MINUTES:
            raise ValueError("Invalid extension duration")
        net = cls.PRICE_BY_MINUTES[minutes]
        vat = int((Decimal(net) * cls.VAT_RATE / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        commission = int((Decimal(net) * cls.COMMISSION_RATE / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        gross = net + vat
        payout = net - commission
        return {
            "minutes": minutes,
            "item_amount_pence": net,
            "item_amount_gbp": cls._money(net),
            "net_amount_pence": net,
            "net_amount_gbp": cls._money(net),
            "vat_rate": float(cls.VAT_RATE),
            "vat_amount_pence": vat,
            "vat_amount_gbp": cls._money(vat),
            "gross_amount_pence": gross,
            "gross_amount_gbp": cls._money(gross),
            "total_amount_pence": gross,
            "total_amount_gbp": cls._money(gross),
            "commission_rate": float(cls.COMMISSION_RATE),
            "commission_amount_pence": commission,
            "lawyer_payout_amount_pence": payout,
        }

    def _session_and_referral(
        self, session_id: uuid.UUID, *, lock: bool = False
    ) -> tuple[ReferralSession, ReferralRequest]:
        query = self.session.query(ReferralSession).filter(ReferralSession.id == session_id)
        if lock:
            query = query.with_for_update()
        session_obj = query.first()
        if not session_obj:
            raise ValueError("Consultation session not found")
        referral = self.session.query(ReferralRequest).filter(
            ReferralRequest.id == session_obj.referral_request_id
        ).first()
        if not referral:
            raise ValueError("Referral request not found")
        return session_obj, referral

    def _allowed_minutes(self, referral: ReferralRequest) -> list[int]:
        consultation_type = (referral.consultation_type or "").strip().lower()
        category = (referral.consultation_category or "").strip().lower()
        if consultation_type == "casework" or category == "casework":
            return []
        if consultation_type.startswith("ongoing_") or category == "ongoing_support":
            return [5, 10, 20, 30]
        if consultation_type.startswith("single_") or category == "single_consultation":
            return [5, 10, 20, 30, 60]
        return []

    def _day_number(self, session_obj: ReferralSession) -> Optional[int]:
        if session_obj.days_total is None:
            return None
        return min((session_obj.days_completed or 0) + 1, session_obj.days_total)

    def _used_minutes_today(self, session_obj: ReferralSession) -> int:
        if session_obj.days_total is None:
            return 0
        day_number = self._day_number(session_obj)
        rows = self.session.query(ConsultationExtensionRequest).filter(
            ConsultationExtensionRequest.session_id == session_obj.id,
            ConsultationExtensionRequest.day_number == day_number,
            ConsultationExtensionRequest.status == ConsultationExtensionStatus.APPLIED.value,
        ).all()
        return sum(row.agreed_minutes or 0 for row in rows)

    def get_options(self, session_id: uuid.UUID) -> Dict[str, Any]:
        session_obj, referral = self._session_and_referral(session_id)
        allowed = self._allowed_minutes(referral)
        used = self._used_minutes_today(session_obj)
        if session_obj.days_total is not None:
            allowed = [minutes for minutes in allowed if used + minutes <= 30]
        return {
            "session_id": str(session_obj.id),
            "eligible": bool(allowed),
            "consultation_type": referral.consultation_type,
            "consultation_category": referral.consultation_category,
            "options": [self.price_breakdown(minutes) for minutes in allowed],
            "daily_extension_limit_minutes": 30 if session_obj.days_total is not None else None,
            "daily_extension_minutes_used": used if session_obj.days_total is not None else None,
            "draft_timeout_seconds": int(self.DRAFT_TTL.total_seconds()),
            "lawyer_response_timeout_seconds": int(self.APPROVAL_TTL.total_seconds()),
            "payment_timeout_seconds": int(self.PAYMENT_TTL.total_seconds()),
        }

    def _active_request(self, session_id: uuid.UUID, *, lock: bool = False) -> Optional[ConsultationExtensionRequest]:
        query = self.session.query(ConsultationExtensionRequest).filter(
            ConsultationExtensionRequest.session_id == session_id,
            ConsultationExtensionRequest.status.in_(self.ACTIVE_STATUSES),
        )
        active = query.first()
        if active is not None and lock:
            active = self.session.query(ConsultationExtensionRequest).filter(
                ConsultationExtensionRequest.id == active.id
            ).with_for_update().first()
        return active

    def _resume_original_timer(self, session_obj: ReferralSession, request_obj: ConsultationExtensionRequest) -> None:
        now = datetime.now(timezone.utc)
        remaining = max(request_obj.paused_remaining_seconds or 0, 0)
        session_obj.end_time = now + timedelta(seconds=remaining)
        session_obj.timer_paused_at = None
        session_obj.paused_remaining_seconds = None

    def expire_if_due(self, request_obj: ConsultationExtensionRequest) -> bool:
        if request_obj.status not in self.ACTIVE_STATUSES:
            return False
        expires_at = self._aware(request_obj.expires_at)
        if expires_at and expires_at > datetime.now(timezone.utc):
            return False
        payment = self._payment_for_request(request_obj.id)
        if payment and payment.payment_status == ConsultationPaymentStatus.PENDING.value:
            if payment.stripe_payment_intent_id:
                cancelled, _ = self.stripe_service.cancel_payment_intent(payment.stripe_payment_intent_id)
                if not cancelled:
                    return False
            payment.payment_status = ConsultationPaymentStatus.CANCELLED.value
        session_obj = self.session.query(ReferralSession).filter(
            ReferralSession.id == request_obj.session_id
        ).with_for_update().first()
        if session_obj:
            self._resume_original_timer(session_obj, request_obj)
        request_obj.status = ConsultationExtensionStatus.EXPIRED.value
        self.session.commit()
        return True

    def expire_active_for_session(self, session_id: uuid.UUID) -> Optional[ConsultationExtensionRequest]:
        active = self._active_request(session_id, lock=True)
        if active:
            self.expire_if_due(active)
        return self._active_request(session_id)

    def pause(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ConsultationExtensionRequest:
        # Lock the parent row so two devices cannot both create the one active
        # extension allowed by the partial unique index.
        session_obj, referral = self._session_and_referral(session_id, lock=True)
        if session_obj.user_id != user_id:
            raise ValueError("Only the client can request an extension")
        if not self._allowed_minutes(referral):
            raise ValueError("Extensions are not available for this consultation type")
        if not session_obj.timer_started_at or session_obj.timer_ended_at or session_obj.session_status == "completed":
            raise ValueError("The consultation timer is not active")

        existing = self._active_request(session_id, lock=True)
        if existing and not self.expire_if_due(existing):
            existing._transitioned = False
            return existing

        now = datetime.now(timezone.utc)
        end_time = self._aware(session_obj.end_time)
        remaining = max(int((end_time - now).total_seconds()), 0) if end_time else 0
        if remaining > 75:
            raise ValueError("Extensions become available when 1 minute remains")
        request_obj = ConsultationExtensionRequest(
            session_id=session_obj.id,
            user_id=session_obj.user_id,
            lawyer_id=session_obj.lawyer_id,
            day_number=self._day_number(session_obj),
            status=ConsultationExtensionStatus.DRAFT.value,
            paused_remaining_seconds=remaining,
            expires_at=now + self.DRAFT_TTL,
        )
        session_obj.timer_paused_at = now
        session_obj.paused_remaining_seconds = remaining
        self.session.add(request_obj)
        self.session.commit()
        self.session.refresh(request_obj)
        request_obj._transitioned = True
        return request_obj

    def submit(self, request_id: uuid.UUID, user_id: uuid.UUID, minutes: int) -> ConsultationExtensionRequest:
        request_obj = self._require_request(request_id, lock=True)
        if request_obj.user_id != user_id:
            raise ValueError("Only the client can submit this extension request")
        if self.expire_if_due(request_obj):
            raise ValueError("Extension selection timed out")
        if request_obj.status != ConsultationExtensionStatus.DRAFT.value:
            request_obj._transitioned = False
            return request_obj
        session_obj, referral = self._session_and_referral(request_obj.session_id)
        allowed = self._allowed_minutes(referral)
        if minutes not in allowed:
            raise ValueError("Invalid extension duration")
        if session_obj.days_total is not None and self._used_minutes_today(session_obj) + minutes > 30:
            raise ValueError("Ongoing support extensions are capped at 30 minutes per day")
        now = datetime.now(timezone.utc)
        request_obj.requested_minutes = minutes
        request_obj.status = ConsultationExtensionStatus.PENDING_LAWYER.value
        request_obj.submitted_at = now
        request_obj.expires_at = now + self.APPROVAL_TTL
        self.session.commit()
        request_obj._transitioned = True
        return request_obj

    def decide(
        self,
        request_id: uuid.UUID,
        lawyer_id: uuid.UUID,
        decision: str,
        proposed_minutes: Optional[int] = None,
    ) -> ConsultationExtensionRequest:
        request_obj = self._require_request(request_id, lock=True)
        if request_obj.lawyer_id != lawyer_id:
            raise ValueError("Only the assigned lawyer can decide this extension request")
        if self.expire_if_due(request_obj):
            raise ValueError("Extension request timed out")
        if request_obj.status != ConsultationExtensionStatus.PENDING_LAWYER.value:
            request_obj._transitioned = False
            return request_obj
        now = datetime.now(timezone.utc)
        decision = decision.strip().lower()
        if decision == "deny":
            session_obj, _ = self._session_and_referral(request_obj.session_id)
            request_obj.status = ConsultationExtensionStatus.DENIED.value
            request_obj.decided_at = now
            self._resume_original_timer(session_obj, request_obj)
        elif decision == "accept":
            request_obj.agreed_minutes = request_obj.requested_minutes
            request_obj.status = ConsultationExtensionStatus.ACCEPTED.value
            request_obj.decided_at = now
            request_obj.expires_at = now + self.PAYMENT_TTL
        elif decision == "propose":
            session_obj, referral = self._session_and_referral(request_obj.session_id)
            if proposed_minutes not in self._allowed_minutes(referral):
                raise ValueError("Invalid proposed extension duration")
            if session_obj.days_total is not None and self._used_minutes_today(session_obj) + proposed_minutes > 30:
                raise ValueError("Ongoing support extensions are capped at 30 minutes per day")
            request_obj.proposed_minutes = proposed_minutes
            request_obj.agreed_minutes = proposed_minutes
            request_obj.status = ConsultationExtensionStatus.PROPOSED.value
            request_obj.decided_at = now
            request_obj.expires_at = now + self.PAYMENT_TTL
        else:
            raise ValueError("Decision must be accept, deny, or propose")
        self.session.commit()
        request_obj._transitioned = True
        return request_obj

    def cancel(self, request_id: uuid.UUID, user_id: uuid.UUID) -> ConsultationExtensionRequest:
        request_obj = self._require_request(request_id, lock=True)
        if request_obj.user_id != user_id:
            raise ValueError("Only the client can cancel this extension request")
        if request_obj.status not in self.ACTIVE_STATUSES - {ConsultationExtensionStatus.PAYMENT_PENDING.value, ConsultationExtensionStatus.PAID.value}:
            request_obj._transitioned = False
            return request_obj
        session_obj, _ = self._session_and_referral(request_obj.session_id)
        payment = self._payment_for_request(request_obj.id)
        if payment and payment.payment_status == ConsultationPaymentStatus.PENDING.value:
            if payment.stripe_payment_intent_id:
                cancelled, message = self.stripe_service.cancel_payment_intent(payment.stripe_payment_intent_id)
                if not cancelled:
                    raise ValueError(message)
            payment.payment_status = ConsultationPaymentStatus.CANCELLED.value
        request_obj.status = ConsultationExtensionStatus.CANCELLED.value
        self._resume_original_timer(session_obj, request_obj)
        self.session.commit()
        request_obj._transitioned = True
        return request_obj

    def create_payment(self, request_id: uuid.UUID, user_id: uuid.UUID) -> Dict[str, Any]:
        request_obj = self._require_request(request_id, lock=True)
        if request_obj.user_id != user_id:
            raise ValueError("Only the client can pay for an extension")
        if self.expire_if_due(request_obj):
            raise ValueError("Extension payment window timed out")
        if request_obj.status not in {
            ConsultationExtensionStatus.ACCEPTED.value,
            ConsultationExtensionStatus.PROPOSED.value,
            ConsultationExtensionStatus.PAYMENT_PENDING.value,
        }:
            raise ValueError("The lawyer must approve or propose an extension before payment")
        existing = self._payment_for_request(request_obj.id)
        if existing and existing.stripe_payment_intent_id:
            ok, _, intent = self.stripe_service.get_payment_intent(existing.stripe_payment_intent_id)
            summary = self._payment_summary(request_obj, existing)
            if ok and intent:
                summary["client_secret"] = intent.get("client_secret")
            return summary

        session_obj, _ = self._session_and_referral(request_obj.session_id)
        user = self.session.query(User).filter(User.id == request_obj.user_id).first()
        lawyer = self.session.query(User).filter(User.id == request_obj.lawyer_id).first()
        if not user or not lawyer:
            raise ValueError("Consultation participants were not found")
        if not lawyer.stripe_connect_account_id:
            raise ValueError("Lawyer must complete Stripe onboarding before accepting extensions")
        customer_service = ConsultationPaymentService(self.session, stripe_service=self.stripe_service)
        ok, message, customer_id = customer_service._get_or_create_customer_id(user)
        if not ok or not customer_id:
            raise ValueError(message)

        price = self.price_breakdown(request_obj.agreed_minutes or 0)
        ok, message, intent = self.stripe_service.create_consultation_payment_intent(
            amount_pence=price["gross_amount_pence"],
            customer_id=customer_id,
            lawyer_connect_id=lawyer.stripe_connect_account_id,
            application_fee_pence=price["commission_amount_pence"],
            metadata={
                "payment_kind": "consultation_extension",
                "extension_request_id": str(request_obj.id),
                "session_id": str(session_obj.id),
                "referral_request_id": str(session_obj.referral_request_id),
                "lawyer_id": str(lawyer.id),
                "user_id": str(user.id),
                "minutes": str(request_obj.agreed_minutes),
                "net_amount_pence": str(price["net_amount_pence"]),
                "vat_rate": str(self.VAT_RATE),
                "vat_amount_pence": str(price["vat_amount_pence"]),
                "gross_amount_pence": str(price["gross_amount_pence"]),
                "transfer_group": f"consultation_extension_{request_obj.id}",
            },
            description=f"LawNeeds {request_obj.agreed_minutes}-minute consultation extension",
            idempotency_key=f"consultation-extension-{request_obj.id}",
        )
        if not ok or not intent:
            raise ValueError(message)

        payment = ConsultationExtensionPayment(
            extension_request_id=request_obj.id,
            session_id=request_obj.session_id,
            stripe_payment_intent_id=intent["id"],
            stripe_customer_id=customer_id,
            net_amount_pence=price["net_amount_pence"],
            vat_rate=self.VAT_RATE,
            vat_amount_pence=price["vat_amount_pence"],
            gross_amount_pence=price["gross_amount_pence"],
            commission_rate=self.COMMISSION_RATE,
            commission_amount_pence=price["commission_amount_pence"],
            lawyer_payout_amount_pence=price["lawyer_payout_amount_pence"],
            payment_status=ConsultationPaymentStatus.PENDING.value,
        )
        request_obj.status = ConsultationExtensionStatus.PAYMENT_PENDING.value
        self.session.add(payment)
        self.session.commit()
        summary = self._payment_summary(request_obj, payment)
        summary["client_secret"] = intent.get("client_secret")
        return summary

    def confirm_payment(
        self,
        request_id: uuid.UUID,
        user_id: uuid.UUID,
        payment_intent_id: Optional[str] = None,
        payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request_obj = self._require_request(request_id, lock=True)
        if request_obj.user_id != user_id:
            raise ValueError("Only the client can confirm this payment")
        payment = self._payment_for_request(request_obj.id)
        if not payment:
            raise ValueError("Extension payment has not been initialized")
        if request_obj.status == ConsultationExtensionStatus.APPLIED.value:
            summary = self._payment_summary(request_obj, payment)
            summary["already_processed"] = True
            return summary
        intent_id = payment_intent_id or payment.stripe_payment_intent_id
        if payment_method_id:
            ok, message = self.stripe_service.confirm_payment_intent(intent_id, payment_method_id)
            if not ok and "authentication required" not in message.lower():
                raise ValueError(message)
        ok, message, intent = self.stripe_service.get_payment_intent(intent_id)
        if not ok or not intent:
            raise ValueError(message)
        if intent.get("status") != "succeeded":
            raise ValueError(f"Payment not completed yet. Current status: {intent.get('status')}")
        summary = self._mark_paid_and_apply(request_obj, payment, intent)
        summary["already_processed"] = False
        return summary

    def mark_paid_by_intent(self, payment_intent_id: str) -> Optional[Dict[str, Any]]:
        payment = self.session.query(ConsultationExtensionPayment).filter(
            ConsultationExtensionPayment.stripe_payment_intent_id == payment_intent_id
        ).with_for_update().first()
        if not payment:
            return None
        request_obj = self._require_request(payment.extension_request_id, lock=True)
        if request_obj.status == ConsultationExtensionStatus.APPLIED.value:
            result = self._payment_summary(request_obj, payment)
            result["already_processed"] = True
            return result
        ok, message, intent = self.stripe_service.get_payment_intent(payment_intent_id)
        if not ok or not intent or intent.get("status") != "succeeded":
            raise ValueError(message or "Extension PaymentIntent has not succeeded")
        result = self._mark_paid_and_apply(request_obj, payment, intent)
        result["already_processed"] = False
        return result

    def _mark_paid_and_apply(self, request_obj, payment, intent) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        session_obj, _ = self._session_and_referral(request_obj.session_id)
        payment.payment_status = ConsultationPaymentStatus.PAID.value
        payment.paid_at = now
        payment.stripe_charge_id = intent.get("latest_charge")
        request_obj.status = ConsultationExtensionStatus.APPLIED.value
        request_obj.paid_at = now
        request_obj.applied_at = now
        request_obj.expires_at = now
        new_remaining = max(request_obj.paused_remaining_seconds, 0) + (request_obj.agreed_minutes or 0) * 60
        session_obj.end_time = now + timedelta(seconds=new_remaining)
        session_obj.timer_paused_at = None
        session_obj.paused_remaining_seconds = None
        self._send_extension_confirmation_email(request_obj, session_obj, payment)
        self.session.commit()
        return self._payment_summary(request_obj, payment)

    def _send_extension_confirmation_email(self, request_obj, session_obj, payment) -> None:
        """
        Send the consultation extension payment confirmation email
        (email automation #8), to both the client (receipted) and the
        professional (automation #5, no receipt - see
        send_consultation_extension_confirmation_professional_email).
        Wrapped in try/except so an email failure never blocks the
        extension being applied - same defensive pattern as automation #7's
        _send_payment_confirmation_email in ConsultationPaymentService.
        """
        from services.email import EmailService
        try:
            user = self.session.query(User).filter(User.id == request_obj.user_id).first()
            if not user or not user.email:
                logger.warning(
                    "Skipping consultation extension email: no client email for payment %s",
                    payment.id,
                )
                return
            lawyer = self.session.query(User).filter(User.id == request_obj.lawyer_id).first()
            lawyer_name = " ".join(
                part for part in [lawyer.first_name, lawyer.last_name] if part
            ).strip() if lawyer else None
            end_time = self._aware(session_obj.end_time)
            new_consultation_end_time = end_time.strftime("%H:%M on %d %B %Y") if end_time else None
            EmailService.send_consultation_extension_confirmation_email(
                to_email=user.email,
                first_name=user.first_name,
                lawyer_name=lawyer_name,
                additional_minutes=request_obj.agreed_minutes,
                new_consultation_end_time=new_consultation_end_time,
                receipt_number=str(payment.id),
                transaction_id=payment.stripe_payment_intent_id,
                payment_date=payment.paid_at.strftime("%d %B %Y") if payment.paid_at else None,
                subtotal=self._money(payment.net_amount_pence),
                vat_amount=self._money(payment.vat_amount_pence),
                total_paid=self._money(payment.gross_amount_pence),
            )

            if lawyer and lawyer.email:
                EmailService.send_consultation_extension_confirmation_professional_email(
                    to_email=lawyer.email,
                    first_name=lawyer.first_name,
                    client_first_name=user.first_name,
                    consultation_reference=str(session_obj.id),
                    additional_minutes=request_obj.agreed_minutes,
                    new_consultation_end_time=new_consultation_end_time,
                )
            else:
                logger.warning(
                    "Skipping consultation extension confirmation (professional) email: no lawyer email for payment %s",
                    payment.id,
                )
        except Exception as exc:
            logger.warning(
                "Failed to send consultation extension confirmation email for payment %s: %s",
                payment.id, exc,
            )
    
    
    def release_payouts(self, session_id: uuid.UUID) -> list[Dict[str, Any]]:
        session_obj = self.session.query(ReferralSession).filter(
            ReferralSession.id == session_id
        ).first()
        if not session_obj:
            return []
        lawyer = self.session.query(User).filter(User.id == session_obj.lawyer_id).first()
        connect_account_id = getattr(lawyer, "stripe_connect_account_id", None) if lawyer else None
        if not connect_account_id:
            return []
        payments = self.session.query(ConsultationExtensionPayment).filter(
            ConsultationExtensionPayment.session_id == session_id,
            ConsultationExtensionPayment.payment_status == ConsultationPaymentStatus.PAID.value,
        ).all()
        released = []
        for payment in payments:
            ok, _, transfer = self.stripe_service.transfer_to_lawyer(
                amount_pence=payment.lawyer_payout_amount_pence,
                connect_account_id=connect_account_id,
                payment_intent_id=payment.stripe_payment_intent_id,
            )
            if ok and transfer:
                payment.stripe_transfer_id = transfer["id"]
                payment.payment_status = ConsultationPaymentStatus.TRANSFERRED.value
                payment.transferred_at = datetime.now(timezone.utc)
                released.append(self._payment_summary(self._require_request(payment.extension_request_id), payment))
        self.session.commit()
        return released

    def get_request(self, request_id: uuid.UUID) -> Dict[str, Any]:
        request_obj = self._require_request(request_id, lock=True)
        self.expire_if_due(request_obj)
        return self.serialize(request_obj)

    def get_active(self, session_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        # The scheduled job owns expiry so timeout notifications are never
        # skipped by a read-only poll racing just after expires_at.
        active = self._active_request(session_id)
        return self.serialize(active) if active else None

    def serialize(self, request_obj: ConsultationExtensionRequest) -> Dict[str, Any]:
        minutes = request_obj.agreed_minutes or request_obj.requested_minutes
        payment = self._payment_for_request(request_obj.id)
        data = {
            "id": str(request_obj.id),
            "session_id": str(request_obj.session_id),
            "status": request_obj.status,
            "day_number": request_obj.day_number,
            "requested_minutes": request_obj.requested_minutes,
            "proposed_minutes": request_obj.proposed_minutes,
            "agreed_minutes": request_obj.agreed_minutes,
            "paused_remaining_seconds": request_obj.paused_remaining_seconds,
            "expires_at": self._aware(request_obj.expires_at).isoformat() if request_obj.expires_at else None,
            "created_at": self._aware(request_obj.created_at).isoformat() if request_obj.created_at else None,
        }
        if minutes:
            data["price"] = self.price_breakdown(minutes)
        if payment:
            data["payment"] = self._payment_summary(request_obj, payment)
        return data

    def _payment_summary(self, request_obj, payment) -> Dict[str, Any]:
        return {
            "extension_request_id": str(request_obj.id),
            "session_id": str(request_obj.session_id),
            "stripe_payment_intent_id": payment.stripe_payment_intent_id,
            "payment_status": payment.payment_status,
            "minutes": request_obj.agreed_minutes,
            "item_amount_pence": payment.net_amount_pence,
            "item_amount_gbp": self._money(payment.net_amount_pence),
            "net_amount_pence": payment.net_amount_pence,
            "net_amount_gbp": self._money(payment.net_amount_pence),
            "vat_rate": float(payment.vat_rate),
            "vat_amount_pence": payment.vat_amount_pence,
            "vat_amount_gbp": self._money(payment.vat_amount_pence),
            "gross_amount_pence": payment.gross_amount_pence,
            "gross_amount_gbp": self._money(payment.gross_amount_pence),
            "total_amount_pence": payment.gross_amount_pence,
            "total_amount_gbp": self._money(payment.gross_amount_pence),
            "commission_rate": float(payment.commission_rate),
            "commission_amount_pence": payment.commission_amount_pence,
            "lawyer_payout_amount_pence": payment.lawyer_payout_amount_pence,
        }

    def _payment_for_request(self, request_id: uuid.UUID) -> Optional[ConsultationExtensionPayment]:
        return self.session.query(ConsultationExtensionPayment).filter(
            ConsultationExtensionPayment.extension_request_id == request_id
        ).first()

    def _require_request(self, request_id: uuid.UUID, *, lock: bool = False) -> ConsultationExtensionRequest:
        query = self.session.query(ConsultationExtensionRequest).filter(
            ConsultationExtensionRequest.id == request_id
        )
        if lock:
            query = query.with_for_update()
        request_obj = query.first()
        if not request_obj:
            raise ValueError("Extension request not found")
        return request_obj
