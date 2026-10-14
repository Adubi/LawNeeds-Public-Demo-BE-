# services/consultation_payment_service.py
"""
Consultation marketplace payment service.

Handles lawyer pricing validation, commission calculation, Stripe payment
initiation, payment confirmation, payout release, and cancellation/refunds.
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple

from sqlalchemy.orm import Session

from services.models import (
    ConsultationCategory,
    ConsultationPayment,
    ConsultationPaymentPricingType,
    ConsultationPaymentStatus,
    ConsultationPricing,
    ReferralRequest,
    ReferralSession,
    User,
    DeviceToken,
)
from services.stripe_service import StripeService
from services.subscription_service import SubscriptionService
from services.booking_service import BookingService
from services.notification_service import NotificationService
from services.firebase_notification_service import FirebaseNotificationService
from services.referral_consultation_pricing_display import (
    CASEWORK_LABEL_BY_SERVICE,
    CASEWORK_PRICE_BY_SERVICE,
)


logger = logging.getLogger(__name__)


class ConsultationPaymentService:
    """Service for marketplace consultation pricing, payment, and payout flows."""

    VAT_RATE = Decimal("20.00")

    COMMISSION_RATES = {
        "none": Decimal("0.00"),
        ConsultationPaymentPricingType.IN_HOUSE.value: Decimal("3.00"),
        ConsultationPaymentPricingType.CUSTOM.value: Decimal("5.00"),
        ConsultationPaymentPricingType.CASEWORK.value: Decimal("10.00"),
    }

    SINGLE_MIN_PENCE = 2_000
    SINGLE_MAX_PENCE = 15_000
    # Ongoing list prices + £200 lawyer adjustment cap (pricing.md — Ongoing Support Plans)
    ONGOING_MIN_PENCE = 30_000  # lowest plan floor (£300)
    ONGOING_MAX_PENCE = 70_000  # 5-day £500 + £200 cap = £700
    ONGOING_3_DAY_MIN_PENCE = 30_000
    ONGOING_3_DAY_MAX_PENCE = 50_000  # £300 + £200 cap
    ONGOING_5_DAY_MIN_PENCE = 50_000
    ONGOING_5_DAY_MAX_PENCE = 70_000  # £500 + £200 cap
    CASEWORK_MIN_PENCE = 70_000

    def __init__(self, db_session: Session, stripe_service: Optional[StripeService] = None):
        self.session = db_session
        self.stripe_service = stripe_service or StripeService()

    def _pricing_classification(
        self, referral: ReferralRequest
    ) -> Tuple[Optional[str], Optional[str], str]:
        """
        Returns (mode, error_message, required_pricing_type).

        mode is 'platform_fixed' (single_*) or 'lawyer_proposes' (ongoing / casework).
        """
        ct = (referral.consultation_type or "").strip().lower()
        cat = (referral.consultation_category or "").strip().lower()

        if not ct:
            return None, "Referral is missing consultation_type.", ""

        if ct.startswith("single_"):
            return "platform_fixed", None, ConsultationPaymentPricingType.IN_HOUSE.value
        if ct.startswith("ongoing_"):
            return "lawyer_proposes", None, ConsultationPaymentPricingType.IN_HOUSE.value
        if ct == "casework":
            return "lawyer_proposes", None, ConsultationPaymentPricingType.CASEWORK.value
        if cat == ConsultationCategory.CASEWORK.value:
            return "lawyer_proposes", None, ConsultationPaymentPricingType.CASEWORK.value
        if cat == ConsultationCategory.ONGOING_SUPPORT.value:
            return "lawyer_proposes", None, ConsultationPaymentPricingType.IN_HOUSE.value
        if cat == ConsultationCategory.NOT_SURE.value:
            return (
                None,
                "Referral is incomplete: finish choosing a consultation option before payment.",
                "",
            )
        return None, f"Unsupported consultation_type {ct!r} for marketplace payment.", ""

    def calculate_commission(
        self,
        amount_pence: int,
        pricing_type: str,
        referral: Optional[ReferralRequest] = None,
    ) -> Tuple[int, int, Decimal]:
        """Calculate commission and payout amounts for a consultation payment."""
        if referral and (referral.consultation_type or "").strip().lower().startswith("single_"):
            return 0, amount_pence, self.COMMISSION_RATES["none"]

        normalized_pricing_type = self._normalize_pricing_type(pricing_type)
        rate = self.COMMISSION_RATES[normalized_pricing_type]
        commission_amount = int(
            (Decimal(amount_pence) * rate / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        payout_amount = amount_pence - commission_amount
        return commission_amount, payout_amount, rate

    def get_pricing_rules(self, session_id: uuid.UUID) -> Dict[str, Any]:
        """
        Return how pricing works for this session's referral (lawyer UI).

        - platform_fixed: single_* — amount is the platform list price; type is always in_house.
        - lawyer_proposes: ongoing_* or casework — lawyer must send amount + correct pricing_type.
        """
        session_obj = self._get_session(session_id)
        referral = self._get_referral(session_obj.referral_request_id)
        mode, err, required_pt = self._pricing_classification(referral)
        if err or not mode:
            raise ValueError(err or "Cannot determine pricing rules for this referral.")

        platform_fixed = mode == "platform_fixed"
        fixed_pence: Optional[int] = None
        if platform_fixed:
            pr = self._get_platform_price_range(referral)
            if pr:
                lo, hi = pr
                if hi is not None and lo == hi:
                    fixed_pence = lo

        hints: Dict[str, Any] = {}
        if not platform_fixed:
            ct = (referral.consultation_type or "").strip().lower()
            if ct.startswith("ongoing_") or (referral.consultation_category or "").strip().lower() == (
                ConsultationCategory.ONGOING_SUPPORT.value
            ):
                lo, hi = self._ongoing_allowed_fee_range_pence(referral)
                hints["amount_min_gbp"] = float(lo / 100)
                hints["amount_max_gbp"] = float(hi / 100)
                in_house_pence = self._ongoing_in_house_price_pence(referral)
                if in_house_pence is not None:
                    hints["in_house_amount_gbp"] = float(in_house_pence / 100)
            if required_pt == ConsultationPaymentPricingType.CASEWORK.value:
                hints["amount_minimum_gbp"] = float(self.CASEWORK_MIN_PENCE / 100)

        existing_payment = self._get_payment_for_session(session_id)
        proposed_price_status: Dict[str, Any] = {"price_proposed": False}
        if existing_payment:
            proposed_amount_pence = self._payment_net_amount_pence(existing_payment)
            proposed_price_status = {
                "price_proposed": True,
                "payment_id": str(existing_payment.id),
                "payment_status": existing_payment.payment_status,
                "proposed_amount_pence": proposed_amount_pence,
                "proposed_amount_gbp": self._pence_to_gbp_string(proposed_amount_pence),
                "pricing_type": existing_payment.pricing_type,
                "created_at": existing_payment.created_at.isoformat() if existing_payment.created_at else None,
            }

        proposal = {
            "exists": existing_payment is not None,
            "payment_id": str(existing_payment.id) if existing_payment else None,
            "payment_status": existing_payment.payment_status if existing_payment else None,
            "pricing_type": existing_payment.pricing_type if existing_payment else None,
            "amount": (
                self._money_amount(self._payment_net_amount_pence(existing_payment))
                if existing_payment else None
            ),
            "created_at": (
                existing_payment.created_at.isoformat()
                if existing_payment and existing_payment.created_at else None
            ),
        }

        return {
            "session_id": str(session_obj.id),
            "pricing_mode": mode,
            "consultation": {
                "category": referral.consultation_category,
                "type": referral.consultation_type,
            },
            "default_pricing_type": required_pt,
            "pricing_options": self._build_pricing_options(
                referral=referral,
                pricing_mode=mode,
                required_pricing_type=required_pt,
                platform_fixed_pence=fixed_pence,
            ),
            "proposal": proposal,
            "consultation_category": referral.consultation_category,
            "consultation_type": referral.consultation_type,
            "ongoing_budget_gbp": getattr(referral, "ongoing_budget_gbp", None),
            "casework_service_type": referral.casework_service_type,
            "casework_service_label": (
                CASEWORK_LABEL_BY_SERVICE.get(referral.casework_service_type)
                if referral.casework_service_type else None
            ),
            "casework_price_range": (
                CASEWORK_PRICE_BY_SERVICE.get(referral.casework_service_type)
                if referral.casework_service_type else None
            ),
            "casework_budget_gbp": referral.casework_budget_gbp,
            "required_pricing_type": required_pt,
            "lawyer_must_propose_amount": not platform_fixed,
            "platform_price_pence": fixed_pence,
            "platform_price_gbp": self._pence_to_gbp_string(fixed_pence) if fixed_pence is not None else None,
            "allowed_pricing_types": (
                [
                    ConsultationPaymentPricingType.IN_HOUSE.value,
                    ConsultationPaymentPricingType.CUSTOM.value,
                ]
                if (
                    not platform_fixed
                    and (
                        (referral.consultation_type or "").strip().lower().startswith("ongoing_")
                        or (referral.consultation_category or "").strip().lower()
                        == ConsultationCategory.ONGOING_SUPPORT.value
                    )
                )
                else [required_pt] if not platform_fixed else [ConsultationPaymentPricingType.IN_HOUSE.value]
            ),
            "amount_hints_gbp": hints,
            "proposed_price_status": proposed_price_status,
            "message": (
                "This session uses the fixed platform price. Payment is created automatically when the lawyer "
                "accepts or when the client opens payment."
                if platform_fixed
                else (
                    "A price has already been proposed for this session."
                    if existing_payment
                    else "Propose your fee within the allowed range using the required pricing type."
                )
            ),
        }

    def _money_amount(self, amount_pence: Optional[int]) -> Optional[Dict[str, Any]]:
        if amount_pence is None:
            return None
        return {
            "pence": amount_pence,
            "gbp": self._pence_to_gbp_string(amount_pence),
        }

    def _calculate_vat_breakdown(self, net_amount_pence: int) -> Dict[str, Any]:
        vat_amount_pence = int(
            (Decimal(net_amount_pence) * self.VAT_RATE / Decimal("100")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        gross_amount_pence = net_amount_pence + vat_amount_pence
        return {
            "net_amount_pence": net_amount_pence,
            "vat_rate": self.VAT_RATE,
            "vat_amount_pence": vat_amount_pence,
            "gross_amount_pence": gross_amount_pence,
        }

    def _pricing_option(
        self,
        pricing_type: str,
        amount_rule: str,
        *,
        fixed_pence: Optional[int] = None,
        minimum_pence: Optional[int] = None,
        maximum_pence: Optional[int] = None,
    ) -> Dict[str, Any]:
        return {
            "pricing_type": pricing_type,
            "amount_rule": amount_rule,
            "fixed_amount": self._money_amount(fixed_pence),
            "minimum_amount": self._money_amount(minimum_pence),
            "maximum_amount": self._money_amount(maximum_pence),
        }

    def _build_pricing_options(
        self,
        referral: ReferralRequest,
        pricing_mode: str,
        required_pricing_type: str,
        platform_fixed_pence: Optional[int],
    ) -> list[Dict[str, Any]]:
        if pricing_mode == "platform_fixed":
            return [
                self._pricing_option(
                    ConsultationPaymentPricingType.IN_HOUSE.value,
                    "fixed",
                    fixed_pence=platform_fixed_pence,
                )
            ]

        if required_pricing_type == ConsultationPaymentPricingType.CASEWORK.value:
            return [
                self._pricing_option(
                    ConsultationPaymentPricingType.CASEWORK.value,
                    "range",
                    minimum_pence=self.CASEWORK_MIN_PENCE,
                )
            ]

        options = []
        in_house_range = self._ongoing_in_house_allowed_range_pence(referral)
        if in_house_range:
            minimum_pence, maximum_pence = in_house_range
            if minimum_pence == maximum_pence:
                options.append(
                    self._pricing_option(
                        ConsultationPaymentPricingType.IN_HOUSE.value,
                        "fixed",
                        fixed_pence=minimum_pence,
                    )
                )
            else:
                options.append(
                    self._pricing_option(
                        ConsultationPaymentPricingType.IN_HOUSE.value,
                        "range",
                        minimum_pence=minimum_pence,
                        maximum_pence=maximum_pence,
                    )
                )

        minimum_pence, maximum_pence = self._ongoing_allowed_fee_range_pence(referral)
        options.append(
            self._pricing_option(
                ConsultationPaymentPricingType.CUSTOM.value,
                "range",
                minimum_pence=minimum_pence,
                maximum_pence=maximum_pence,
            )
        )
        return options

    def _enrich_summary_with_intent_client_secret(self, summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not summary:
            return None
        pid = summary.get("stripe_payment_intent_id")
        if not pid:
            return summary
        intent_success, _, intent_data = self.stripe_service.get_payment_intent(pid)
        if not intent_success or not intent_data:
            return summary
        out = dict(summary)
        out["client_secret"] = intent_data.get("client_secret")
        return out

    def try_initiate_platform_fixed_payment(
        self,
        session_id: uuid.UUID,
        proposed_amount_gbp: Optional[Decimal] = None,
        pricing_type: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        For platform_fixed referrals only: ensure a Stripe payment exists (idempotent).

        Used when the lawyer accepts (auto-init) or when a participant loads payment (lazy init).
        Returns (True, "skipped_not_platform_fixed", None) if this referral is not platform-fixed.
        """
        session_obj = self._get_session(session_id)
        referral = self._get_referral(session_obj.referral_request_id)
        mode, class_err, _ = self._pricing_classification(referral)
        if class_err or not mode:
            return False, class_err or "Cannot determine pricing for this referral.", None
        if mode != "platform_fixed":
            return True, "skipped_not_platform_fixed", None

        existing_payment = self._get_payment_for_session(session_obj.id)
        if existing_payment:
            if existing_payment.payment_status in (
                ConsultationPaymentStatus.PAID.value,
                ConsultationPaymentStatus.TRANSFERRED.value,
            ):
                return (
                    False,
                    "Payment has already been completed for this session",
                    self.get_payment_summary(session_id),
                )
            if existing_payment.payment_status == ConsultationPaymentStatus.PENDING.value:
                summary = self.get_payment_summary(session_id)
                return True, "Payment already initialized", self._enrich_summary_with_intent_client_secret(
                    summary
                )

        return self.initiate_payment(session_id, proposed_amount_gbp, pricing_type)

    def propose_price_for_session(
        self,
        session_id: uuid.UUID,
        proposed_amount_gbp: Optional[Decimal],
        pricing_type: Optional[str],
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Lawyer-facing propose-price: only ongoing/casework uses fee proposal.
        Platform-fixed sessions use auto-init; this path stays idempotent for legacy clients.
        """
        session_obj = self._get_session(session_id)
        referral = self._get_referral(session_obj.referral_request_id)
        mode, class_err, _ = self._pricing_classification(referral)
        if class_err or not mode:
            return False, class_err or "Cannot determine pricing for this referral.", None
        if mode == "platform_fixed":
            return self.try_initiate_platform_fixed_payment(session_id, proposed_amount_gbp, pricing_type)
        return self.initiate_payment(session_id, proposed_amount_gbp, pricing_type)

    def initiate_payment(
        self,
        session_id: uuid.UUID,
        proposed_amount_gbp: Optional[Decimal],
        pricing_type: Optional[str],
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Create or refresh a consultation payment intent for a session."""
        session_obj = self._get_session(session_id)
        referral = self._get_referral(session_obj.referral_request_id)
        lawyer = self._get_user(session_obj.lawyer_id)
        user = self._get_user(session_obj.user_id)

        mode, class_err, required_pt = self._pricing_classification(referral)
        if class_err or not mode:
            return False, class_err or "Cannot determine pricing for this referral.", None

        platform_fixed = mode == "platform_fixed"

        if platform_fixed:
            normalized_pricing_type = ConsultationPaymentPricingType.IN_HOUSE.value
            if pricing_type is not None and str(pricing_type).strip():
                try:
                    pt_norm = self._normalize_pricing_type(pricing_type)
                except ValueError:
                    return False, "Invalid pricing_type.", None
                if pt_norm != normalized_pricing_type:
                    return (
                        False,
                        "Single-session referrals use the fixed platform price only (in_house). "
                        "Custom or casework pricing is not available.",
                        None,
                    )

            pr = self._get_platform_price_range(referral)
            if not pr:
                return False, "Platform pricing is not configured for this consultation type.", None
            min_p, max_p = pr
            if max_p is not None and min_p != max_p:
                return False, "Platform price for this consultation type must be a single fixed amount.", None
            amount_pence = min_p
            if proposed_amount_gbp is not None:
                if self._gbp_to_pence(proposed_amount_gbp) != amount_pence:
                    return (
                        False,
                        f"Amount must match the platform price ({self._pence_to_gbp_string(amount_pence)} GBP) "
                        "for this consultation.",
                        None,
                    )
        else:
            if proposed_amount_gbp is None:
                in_house_pence = (
                    self._ongoing_in_house_price_pence(referral)
                    if required_pt == ConsultationPaymentPricingType.IN_HOUSE.value
                    else None
                )
                if in_house_pence is not None:
                    amount_pence = in_house_pence
                    normalized_pricing_type = ConsultationPaymentPricingType.IN_HOUSE.value
                else:
                    return (
                        False,
                        "This consultation requires you to propose a fee (ongoing support or casework).",
                        None,
                    )
            else:
                if pricing_type is None or not str(pricing_type).strip():
                    normalized_pricing_type = required_pt
                else:
                    try:
                        normalized_pricing_type = self._normalize_pricing_type(pricing_type)
                    except ValueError:
                        return False, "Invalid pricing_type.", None
                allowed_pricing_types = [required_pt]
                if required_pt == ConsultationPaymentPricingType.IN_HOUSE.value:
                    allowed_pricing_types.append(ConsultationPaymentPricingType.CUSTOM.value)
                if normalized_pricing_type not in allowed_pricing_types:
                    return (
                        False,
                        f"This referral requires pricing_type '{required_pt}'.",
                        None,
                    )
                amount_pence = self._gbp_to_pence(proposed_amount_gbp)

        if not lawyer.stripe_connect_account_id:
            return False, "Lawyer must complete Stripe onboarding before proposing a price", None

        status_success, status_message, status_data = self.stripe_service.get_account_status(
            lawyer.stripe_connect_account_id
        )
        if not status_success or not status_data:
            return False, status_message, None
        if not status_data["onboarded"]:
            return False, "Lawyer must complete Stripe onboarding before proposing a price", None
        lawyer.stripe_connect_onboarded = status_data["onboarded"]

        is_valid, validation_message = self._validate_price(referral, normalized_pricing_type, amount_pence)
        if not is_valid:
            return False, validation_message, None

        existing_payment = self._get_payment_for_session(session_obj.id)
        if existing_payment and existing_payment.payment_status in (
            ConsultationPaymentStatus.PAID.value,
            ConsultationPaymentStatus.TRANSFERRED.value,
        ):
            return False, "Payment has already been completed for this session", None

        success, customer_message, customer_id = self._get_or_create_customer_id(user)
        if not success or not customer_id:
            return False, customer_message, None

        commission_amount_pence, lawyer_payout_amount_pence, commission_rate = self.calculate_commission(
            amount_pence, normalized_pricing_type, referral
        )
        vat_breakdown = self._calculate_vat_breakdown(amount_pence)

        intent_success, intent_message, payment_intent = self.stripe_service.create_consultation_payment_intent(
            amount_pence=vat_breakdown["gross_amount_pence"],
            customer_id=customer_id,
            lawyer_connect_id=lawyer.stripe_connect_account_id,
            application_fee_pence=commission_amount_pence,
            metadata={
                "session_id": str(session_obj.id),
                "referral_request_id": str(referral.id),
                "lawyer_id": str(lawyer.id),
                "user_id": str(user.id),
                "pricing_type": normalized_pricing_type,
                "net_amount_pence": str(vat_breakdown["net_amount_pence"]),
                "vat_rate": str(vat_breakdown["vat_rate"]),
                "vat_amount_pence": str(vat_breakdown["vat_amount_pence"]),
                "gross_amount_pence": str(vat_breakdown["gross_amount_pence"]),
                "transfer_group": f"consultation_session_{session_obj.id}",
            },
            description=f"LawNeeds consultation for referral {referral.id}",
        )
        if not intent_success or not payment_intent:
            return False, intent_message, None

        if existing_payment is None:
            payment = ConsultationPayment(
                session_id=session_obj.id,
                referral_request_id=referral.id,
                lawyer_id=lawyer.id,
                user_id=user.id,
                stripe_customer_id=customer_id,
                net_amount_pence=vat_breakdown["net_amount_pence"],
                vat_rate=vat_breakdown["vat_rate"],
                vat_amount_pence=vat_breakdown["vat_amount_pence"],
                gross_amount_pence=vat_breakdown["gross_amount_pence"],
                total_amount_pence=vat_breakdown["gross_amount_pence"],
                pricing_type=normalized_pricing_type,
                commission_rate=commission_rate,
                commission_amount_pence=commission_amount_pence,
                lawyer_payout_amount_pence=lawyer_payout_amount_pence,
                payment_status=ConsultationPaymentStatus.PENDING.value,
            )
            self.session.add(payment)
            self.session.flush()
        else:
            if existing_payment.payment_status == ConsultationPaymentStatus.PENDING.value:
                return False, "A payment is already pending for this session", None

            payment = existing_payment
            payment.stripe_customer_id = customer_id
            payment.net_amount_pence = vat_breakdown["net_amount_pence"]
            payment.vat_rate = vat_breakdown["vat_rate"]
            payment.vat_amount_pence = vat_breakdown["vat_amount_pence"]
            payment.gross_amount_pence = vat_breakdown["gross_amount_pence"]
            payment.total_amount_pence = vat_breakdown["gross_amount_pence"]
            payment.pricing_type = normalized_pricing_type
            payment.commission_rate = commission_rate
            payment.commission_amount_pence = commission_amount_pence
            payment.lawyer_payout_amount_pence = lawyer_payout_amount_pence
            payment.payment_status = ConsultationPaymentStatus.PENDING.value
            payment.paid_at = None
            payment.transferred_at = None
            payment.refunded_at = None

        payment.stripe_payment_intent_id = payment_intent["id"]
        payment.stripe_transfer_id = None
        payment.stripe_charge_id = None
        session_obj.payment_id = payment.id
        session_obj.session_status = 'pending'
        self.session.commit()

        summary = self.get_payment_summary(session_obj.id)
        if summary:
            summary["client_secret"] = payment_intent.get("client_secret")
            summary["stripe_payment_intent_id"] = payment_intent["id"]

        return True, intent_message, summary

    def confirm_payment(
        self,
        session_id: uuid.UUID,
        payment_intent_id: Optional[str] = None,
        payment_method_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Confirm a pending consultation payment after the client completes checkout."""
        session_obj = self._get_session(session_id)
        payment = self._require_payment_for_session(session_obj.id)

        if payment.payment_status in (
            ConsultationPaymentStatus.PAID.value,
            ConsultationPaymentStatus.TRANSFERRED.value,
        ):
            return True, "Payment already confirmed", self.get_payment_summary(session_obj.id)

        target_intent_id = payment_intent_id or payment.stripe_payment_intent_id
        if not target_intent_id:
            return False, "No Stripe payment intent found for this session", None

        if payment_method_id:
            confirm_success, confirm_message = self.stripe_service.confirm_payment_intent(
                target_intent_id,
                payment_method_id=payment_method_id,
            )
            if not confirm_success and "authentication required" not in confirm_message.lower():
                return False, confirm_message, None

        intent_success, intent_message, payment_intent = self.stripe_service.get_payment_intent(target_intent_id)
        if not intent_success or not payment_intent:
            return False, intent_message, None

        if payment_intent["status"] != "succeeded":
            return False, f"Payment not completed yet. Current status: {payment_intent['status']}", None

        self._mark_payment_paid(payment, payment_intent)
        self.session.commit()
        return True, "Payment confirmed", self.get_payment_summary(session_obj.id)

    async def release_payout(self, session_id: uuid.UUID) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Transfer lawyer payout after the consultation session ends."""
        session_obj = self._get_session(session_id)
        payment = self._require_payment_for_session(session_obj.id)
        lawyer = self._get_user(session_obj.lawyer_id)

        if payment.payment_status == ConsultationPaymentStatus.TRANSFERRED.value:
            return True, "Payout already released", self.get_payment_summary(session_obj.id)

        if payment.payment_status != ConsultationPaymentStatus.PAID.value:
            return False, "Payout can only be released after payment is confirmed", None

        if not payment.stripe_payment_intent_id:
            await self._notify_payout_failed(session_obj, "Cannot release payout without a Stripe payment intent")
            return False, "Cannot release payout without a Stripe payment intent", None

        if not lawyer.stripe_connect_account_id:
            await self._notify_payout_failed(session_obj, "Lawyer does not have a Stripe Connect account configured")
            return False, "Lawyer does not have a Stripe Connect account configured", None

        transfer_success, transfer_message, transfer_data = self.stripe_service.transfer_to_lawyer(
            amount_pence=payment.lawyer_payout_amount_pence,
            connect_account_id=lawyer.stripe_connect_account_id,
            payment_intent_id=payment.stripe_payment_intent_id,
        )
        if not transfer_success or not transfer_data:
            await self._notify_payout_failed(session_obj, transfer_message)
            return False, transfer_message, None

        payment.stripe_transfer_id = transfer_data["id"]
        payment.payment_status = ConsultationPaymentStatus.TRANSFERRED.value
        payment.transferred_at = datetime.now(timezone.utc)
        self.session.commit()

        await self._notify_payout_sent(session_obj, payment)

        return True, "Payout released", self.get_payment_summary(session_obj.id)

    async def _notify_payout_failed(self, session_obj: ReferralSession, reason: str) -> None:
        """
        Notify the lawyer (in-app + push, best-effort) that their consultation
        payout failed to send. Never raises - a notification failure must not
        block or alter the payout failure response already being returned.
        """
        try:
            notif_svc = NotificationService(self.session)
            notif_svc.create(
                recipient_id=session_obj.lawyer_id,
                title="Payout Failed",
                body=f"We could not send your payout: {reason}",
                notification_type="payout_failed",
                data={
                    "session_id": str(session_obj.id),
                    "referral_id": str(session_obj.referral_request_id),
                    "reason": reason,
                    "screen": "ConsultationSummary",
                },
            )
            self.session.commit()
        except Exception as notify_error:
            logger.warning(f"Failed to persist payout_failed notification for session {session_obj.id}: {notify_error}")

        try:
            firebase = FirebaseNotificationService()
            lawyer_devices = self.session.query(DeviceToken).filter(
                DeviceToken.user_id == session_obj.lawyer_id,
                DeviceToken.is_active == True,
            ).all()
            if lawyer_devices:
                await firebase.send_payout_failed_notification(
                    device_tokens=[d.device_token for d in lawyer_devices],
                    reason=reason,
                    session_id=str(session_obj.id),
                    referral_id=str(session_obj.referral_request_id),
                )
        except Exception as push_error:
            logger.warning(f"Failed to send payout_failed push for session {session_obj.id}: {push_error}")

    async def _notify_payout_sent(self, session_obj: ReferralSession, payment: ConsultationPayment) -> None:
        """
        Notify the lawyer (in-app + push, best-effort) that their consultation
        payout has been sent. Never raises.
        """
        amount_gbp = self._pence_to_gbp_string(payment.lawyer_payout_amount_pence)
        try:
            notif_svc = NotificationService(self.session)
            notif_svc.create(
                recipient_id=session_obj.lawyer_id,
                title="Payout Sent",
                body=f"Your payout of GBP {amount_gbp} has been sent",
                notification_type="payout_sent",
                data={
                    "session_id": str(session_obj.id),
                    "referral_id": str(session_obj.referral_request_id),
                    "amount_gbp": amount_gbp,
                    "screen": "ConsultationSummary",
                },
            )
            self.session.commit()
        except Exception as notify_error:
            logger.warning(f"Failed to persist payout_sent notification for session {session_obj.id}: {notify_error}")

        try:
            firebase = FirebaseNotificationService()
            lawyer_devices = self.session.query(DeviceToken).filter(
                DeviceToken.user_id == session_obj.lawyer_id,
                DeviceToken.is_active == True,
            ).all()
            if lawyer_devices:
                await firebase.send_payout_sent_notification(
                    device_tokens=[d.device_token for d in lawyer_devices],
                    amount_gbp=amount_gbp,
                    session_id=str(session_obj.id),
                    referral_id=str(session_obj.referral_request_id),
                )
        except Exception as push_error:
            logger.warning(f"Failed to send payout_sent push for session {session_obj.id}: {push_error}")

    def handle_cancellation(self, session_id: uuid.UUID) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Cancel a consultation payment and refund it if it has already been captured."""
        session_obj = self._get_session(session_id)
        payment = self._get_payment_for_session(session_obj.id)

        if payment is None:
            return True, "No consultation payment exists for this session", None

        if payment.payment_status == ConsultationPaymentStatus.TRANSFERRED.value:
            return False, "Cannot cancel a consultation after payout has been released", None

        if payment.payment_status == ConsultationPaymentStatus.PAID.value and payment.stripe_payment_intent_id:
            refund_success, refund_message, _ = self.stripe_service.refund_payment_intent(
                payment.stripe_payment_intent_id
            )
            if not refund_success:
                return False, refund_message, None

        payment.payment_status = ConsultationPaymentStatus.REFUNDED.value
        payment.refunded_at = datetime.now(timezone.utc)
        session_obj.payment_id = payment.id
        self.session.commit()

        return True, "Consultation payment cancelled", self.get_payment_summary(session_obj.id)

    def mark_payment_paid_by_intent(self, payment_intent_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Used by Stripe webhooks to mark a consultation payment as paid."""
        payment = self.session.query(ConsultationPayment).filter(
            ConsultationPayment.stripe_payment_intent_id == payment_intent_id
        ).first()
        if not payment:
            return False, "Consultation payment not found", None

        if payment.payment_status in (
            ConsultationPaymentStatus.PAID.value,
            ConsultationPaymentStatus.TRANSFERRED.value,
        ):
            return True, "Payment already marked as paid", self.get_payment_summary(payment.session_id)

        intent_success, intent_message, payment_intent = self.stripe_service.get_payment_intent(payment_intent_id)
        if not intent_success or not payment_intent:
            return False, intent_message, None

        if payment_intent["status"] != "succeeded":
            return False, f"Payment intent is not successful: {payment_intent['status']}", None

        self._mark_payment_paid(payment, payment_intent)
        self.session.commit()
        return True, "Payment marked as paid", self.get_payment_summary(payment.session_id)

    def sync_connect_account(self, account_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Refresh stored Stripe Connect onboarding state for a lawyer."""
        user = self.session.query(User).filter(User.stripe_connect_account_id == account_id).first()
        if not user:
            return False, "No lawyer found for Stripe Connect account", None

        status_success, status_message, status_data = self.stripe_service.get_account_status(account_id)
        if not status_success or not status_data:
            return False, status_message, None

        user.stripe_connect_onboarded = status_data["onboarded"]
        self.session.commit()
        return True, "Stripe Connect status synced", status_data

    def get_payment_summary_with_lazy_platform_fixed_init(self, session_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Return payment summary, creating a platform-fixed Stripe intent on first access if needed."""
        summary = self.get_payment_summary(session_id)
        if summary:
            return summary
        ok, _, _ = self.try_initiate_platform_fixed_payment(session_id)
        if ok:
            return self.get_payment_summary(session_id)
        return None

    def _payment_net_amount_pence(self, payment: ConsultationPayment) -> int:
        return getattr(payment, "net_amount_pence", None) or payment.total_amount_pence

    def _payment_vat_rate(self, payment: ConsultationPayment) -> Decimal:
        return getattr(payment, "vat_rate", None) or Decimal("0.00")

    def _payment_vat_amount_pence(self, payment: ConsultationPayment) -> int:
        return getattr(payment, "vat_amount_pence", None) or 0

    def _payment_gross_amount_pence(self, payment: ConsultationPayment) -> int:
        return getattr(payment, "gross_amount_pence", None) or payment.total_amount_pence

    def get_payment_summary(self, session_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Return a normalized payment summary for consultation APIs."""
        session_obj = self._get_session(session_id)
        payment = self._get_payment_for_session(session_obj.id)
        if not payment:
            return None
        net_amount_pence = self._payment_net_amount_pence(payment)
        vat_rate = self._payment_vat_rate(payment)
        vat_amount_pence = self._payment_vat_amount_pence(payment)
        gross_amount_pence = self._payment_gross_amount_pence(payment)

        return {
            "payment_id": str(payment.id),
            "session_id": str(session_obj.id),
            "referral_request_id": str(payment.referral_request_id) if payment.referral_request_id else None,
            "stripe_payment_intent_id": payment.stripe_payment_intent_id,
            "stripe_transfer_id": payment.stripe_transfer_id,
            "pricing_type": payment.pricing_type,
            "payment_status": payment.payment_status,
            "item_amount_pence": net_amount_pence,
            "item_amount_gbp": self._pence_to_gbp_string(net_amount_pence),
            "net_amount_pence": net_amount_pence,
            "net_amount_gbp": self._pence_to_gbp_string(net_amount_pence),
            "vat_rate": float(vat_rate),
            "vat_amount_pence": vat_amount_pence,
            "vat_amount_gbp": self._pence_to_gbp_string(vat_amount_pence),
            "gross_amount_pence": gross_amount_pence,
            "gross_amount_gbp": self._pence_to_gbp_string(gross_amount_pence),
            "total_amount_pence": gross_amount_pence,
            "total_amount_gbp": self._pence_to_gbp_string(gross_amount_pence),
            "commission_rate": float(payment.commission_rate),
            "commission_amount_pence": payment.commission_amount_pence,
            "commission_amount_gbp": self._pence_to_gbp_string(payment.commission_amount_pence),
            "lawyer_payout_amount_pence": payment.lawyer_payout_amount_pence,
            "lawyer_payout_amount_gbp": self._pence_to_gbp_string(payment.lawyer_payout_amount_pence),
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            "transferred_at": payment.transferred_at.isoformat() if payment.transferred_at else None,
            "refunded_at": payment.refunded_at.isoformat() if payment.refunded_at else None,
        }

    def _validate_price(
        self,
        referral: ReferralRequest,
        pricing_type: str,
        amount_pence: int
    ) -> Tuple[bool, str]:
        if pricing_type not in self.COMMISSION_RATES:
            return False, "Invalid pricing type. Use in_house, custom, or casework."

        ct = (referral.consultation_type or "").strip().lower()
        cat = (referral.consultation_category or "").strip().lower()

        if ct.startswith("single_"):
            if pricing_type != ConsultationPaymentPricingType.IN_HOUSE.value:
                return False, "Single-session consultations use fixed platform pricing (in_house only)."
        elif ct.startswith("ongoing_"):
            if pricing_type not in (
                ConsultationPaymentPricingType.IN_HOUSE.value,
                ConsultationPaymentPricingType.CUSTOM.value,
            ):
                return False, "Ongoing support requires in_house or custom pricing."
        elif ct == "casework" or cat == ConsultationCategory.CASEWORK.value:
            if pricing_type != ConsultationPaymentPricingType.CASEWORK.value:
                return False, "Casework requires casework pricing (lawyer-proposed fee)."

        if pricing_type == ConsultationPaymentPricingType.CASEWORK.value:
            if amount_pence < self.CASEWORK_MIN_PENCE:
                return False, "Casework pricing must be at least £700."
            return True, "Casework pricing valid"

        if pricing_type == ConsultationPaymentPricingType.IN_HOUSE.value and ct.startswith("ongoing_"):
            in_house_range = self._ongoing_in_house_allowed_range_pence(referral)
            if in_house_range is None:
                return False, "In-house ongoing support pricing is not configured for this plan."
            return self._validate_against_range(
                amount_pence,
                in_house_range,
                "Amount must match the in-house ongoing support price.",
            )

        if pricing_type == ConsultationPaymentPricingType.IN_HOUSE.value:
            configured_range = self._get_platform_price_range(referral)
            if configured_range:
                return self._validate_against_range(
                    amount_pence,
                    configured_range,
                    "Amount must match the platform-set consultation pricing.",
                )

        if referral.consultation_category == ConsultationCategory.SINGLE_CONSULTATION.value:
            return self._validate_against_range(
                amount_pence,
                (self.SINGLE_MIN_PENCE, self.SINGLE_MAX_PENCE),
                "Single consultation pricing must be between £20 and £150.",
            )

        if pricing_type == ConsultationPaymentPricingType.CUSTOM.value and (
            ct.startswith("ongoing_")
            or cat == ConsultationCategory.ONGOING_SUPPORT.value
        ):
            lo, hi = self._ongoing_allowed_fee_range_pence(referral)
            return self._validate_against_range(
                amount_pence,
                (lo, hi),
                f"Ongoing support fee must be between £{lo // 100} and £{hi // 100} for this plan "
                "(list price plus up to £200 adjustment cap).",
            )

        if referral.consultation_category == ConsultationCategory.CASEWORK.value:
            if amount_pence < self.CASEWORK_MIN_PENCE:
                return False, "Casework pricing must be at least £700."
            return True, "Casework pricing valid"

        return amount_pence > 0, "Consultation pricing valid" if amount_pence > 0 else "Price must be greater than zero."

    def _ongoing_in_house_price_pence(self, referral: ReferralRequest) -> Optional[int]:
        exact_budget = self._ongoing_exact_budget_pence(referral)
        if exact_budget is not None:
            return exact_budget

        ct = (referral.consultation_type or "").strip().lower()
        if ct == "ongoing_3_day":
            return self.ONGOING_3_DAY_MIN_PENCE
        if ct == "ongoing_5_day":
            return self.ONGOING_5_DAY_MIN_PENCE
        return None

    def _ongoing_exact_budget_pence(self, referral: ReferralRequest) -> Optional[int]:
        budget_gbp = getattr(referral, "ongoing_budget_gbp", None)
        if budget_gbp is None:
            budget_range = getattr(referral, "budget_range", None)
            if budget_range and str(budget_range).strip().isdigit():
                budget_gbp = int(str(budget_range).strip())

        if budget_gbp is None:
            return None

        try:
            amount_pence = int(budget_gbp) * 100
        except (TypeError, ValueError):
            return None

        lo, hi = self._ongoing_allowed_fee_range_pence(referral)
        if lo <= amount_pence <= hi:
            return amount_pence
        return None

    def _ongoing_in_house_allowed_range_pence(self, referral: ReferralRequest) -> Optional[Tuple[int, int]]:
        exact_budget = self._ongoing_exact_budget_pence(referral)
        if exact_budget is not None:
            return exact_budget, exact_budget

        ct = (referral.consultation_type or "").strip().lower()
        budget_range = (getattr(referral, "budget_range", None) or "").strip()
        if ct == "ongoing_3_day" and budget_range in {"300_to_499", "300_to_500"}:
            return self.ONGOING_3_DAY_MIN_PENCE, self.ONGOING_3_DAY_MAX_PENCE
        if ct == "ongoing_5_day" and budget_range in {"500_to_699", "500_to_700"}:
            return self.ONGOING_5_DAY_MIN_PENCE, self.ONGOING_5_DAY_MAX_PENCE

        floor = self._ongoing_in_house_price_pence(referral)
        if floor is None:
            return None
        return floor, floor

    def _validate_against_range(
        self,
        amount_pence: int,
        allowed_range: Tuple[int, Optional[int]],
        failure_message: str
    ) -> Tuple[bool, str]:
        min_pence, max_pence = allowed_range
        if amount_pence < min_pence:
            return False, failure_message
        if max_pence is not None and amount_pence > max_pence:
            return False, failure_message
        return True, "Price valid"

    def _ongoing_allowed_fee_range_pence(self, referral: ReferralRequest) -> Tuple[int, int]:
        """
        Allowed total fee for ongoing support (custom pricing).

        pricing.md: 3-day plan £300, 5-day plan £500, lawyers may adjust within £200 cap.
        """
        ct = (referral.consultation_type or "").strip().lower()
        if ct == "ongoing_3_day":
            return self.ONGOING_3_DAY_MIN_PENCE, self.ONGOING_3_DAY_MAX_PENCE
        if ct == "ongoing_5_day":
            return self.ONGOING_5_DAY_MIN_PENCE, self.ONGOING_5_DAY_MAX_PENCE
        if ct.startswith("ongoing_"):
            return self.ONGOING_MIN_PENCE, self.ONGOING_MAX_PENCE
        cat = (referral.consultation_category or "").strip().lower()
        if cat == ConsultationCategory.ONGOING_SUPPORT.value:
            return self.ONGOING_MIN_PENCE, self.ONGOING_MAX_PENCE
        return self.ONGOING_MIN_PENCE, self.ONGOING_MAX_PENCE

    def _get_platform_price_range(self, referral: ReferralRequest) -> Optional[Tuple[int, Optional[int]]]:
        pricing = self.session.query(ConsultationPricing).filter(
            ConsultationPricing.consultation_type == referral.consultation_type
        ).first()
        if not pricing or not pricing.price_gbp:
            return None

        normalized = pricing.price_gbp.replace(",", "").strip()
        numbers = [int(match) for match in re.findall(r"\d+", normalized)]
        if not numbers:
            return None

        if normalized.endswith("+"):
            return numbers[0] * 100, None
        if "-" in normalized or "/" in normalized:
            return min(numbers) * 100, max(numbers) * 100
        return numbers[0] * 100, numbers[0] * 100

    def _get_or_create_customer_id(self, user: User) -> Tuple[bool, str, Optional[str]]:
        if not user.email:
            return False, "User email is required to create a Stripe customer", None

        subscription_service = SubscriptionService(self.session)
        subscription = subscription_service.get_or_create_subscription(user.id)
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None

        success, message, customer_id = self.stripe_service.get_or_create_customer(
            email=user.email,
            stripe_customer_id=subscription.stripe_customer_id,
            name=full_name,
            user_id=str(user.id),
        )
        if success and customer_id and subscription.stripe_customer_id != customer_id:
            subscription.stripe_customer_id = customer_id

        return success, message, customer_id

    def _mark_payment_paid(self, payment: ConsultationPayment, payment_intent: Dict[str, Any]) -> None:
        payment.payment_status = ConsultationPaymentStatus.PAID.value
        payment.paid_at = datetime.now(timezone.utc)
        payment.stripe_payment_intent_id = payment_intent["id"]
        payment.stripe_charge_id = payment_intent.get("latest_charge")
        if payment.referral_request_id:
            try:
                BookingService(self.session).mark_paid_for_referral(payment.referral_request_id)
            except Exception as exc:
                logger.warning(
                    "Failed to mark booking paid for referral %s: %s",
                    payment.referral_request_id,
                    exc,
                )
        self._send_payment_confirmation_email(payment) 
        
        

    def _send_payment_confirmation_email(self, payment: ConsultationPayment) -> None:
        """
        Send the consultation payment confirmation email (email automation #7),
        to both the client (receipted) and the professional (automation #4,
        no receipt - see send_consultation_payment_confirmation_professional_email).

        SCOPE: single_consultation and ongoing_support only. Casework
        payments have no booked slot at all (booking_service.
        requires_booking_slot() is False for casework), so there is no
        lawyer/date/time to show - this deliberately skips casework.
        A casework-specific confirmation email is a separate, not-yet-built
        automation.

        Wrapped in try/except at the call site's level of concern: an
        email/lookup failure here must never block a payment from being
        marked paid, same reasoning as the BookingService call above.
        """
        from services.email import EmailService
        from services.models import ConsultationBookingRequest
        from services.booking_service import consultation_duration_minutes

        if payment.pricing_type == ConsultationPaymentPricingType.CASEWORK.value:
            return

        try:
            session_obj = self.session.query(ReferralSession).filter(
                ReferralSession.id == payment.session_id
            ).first()
            if not session_obj:
                logger.warning(
                    "Skipping consultation payment email: session %s not found for payment %s",
                    payment.session_id, payment.id,
                )
                return

            client = self.session.query(User).filter(User.id == payment.user_id).first()
            if not client or not client.email:
                logger.warning(
                    "Skipping consultation payment email: no client email for payment %s",
                    payment.id,
                )
                return

            lawyer = self.session.query(User).filter(User.id == payment.lawyer_id).first()
            lawyer_name = " ".join(
                part for part in [lawyer.first_name, lawyer.last_name] if part
            ).strip() if lawyer else None

            booking = None
            if payment.referral_request_id:
                booking = self.session.query(ConsultationBookingRequest).filter(
                    ConsultationBookingRequest.referral_request_id == payment.referral_request_id
                ).first()

            consultation_date = consultation_time = consultation_length = None
            if booking and booking.requested_start_at:
                start = booking.requested_start_at
                consultation_date = start.strftime("%d %B %Y")
                consultation_time = start.strftime("%H:%M")
                duration_minutes = consultation_duration_minutes(booking.consultation_type)
                consultation_length = f"{duration_minutes} minutes"

            net_amount_pence = self._payment_net_amount_pence(payment)
            vat_amount_pence = self._payment_vat_amount_pence(payment)
            gross_amount_pence = self._payment_gross_amount_pence(payment)

            EmailService.send_consultation_payment_confirmation_email(
                to_email=client.email,
                first_name=client.first_name,
                lawyer_name=lawyer_name,
                consultation_reference=str(session_obj.id),
                consultation_date=consultation_date,
                consultation_time=consultation_time,
                consultation_length=consultation_length,
                receipt_number=str(payment.id),
                transaction_id=payment.stripe_payment_intent_id,
                payment_date=payment.paid_at.strftime("%d %B %Y") if payment.paid_at else None,
                subtotal=self._pence_to_gbp_string(net_amount_pence),
                vat_amount=self._pence_to_gbp_string(vat_amount_pence),
                total_paid=self._pence_to_gbp_string(gross_amount_pence),
            )

            if lawyer and lawyer.email:
                client_name = " ".join(
                    part for part in [client.first_name, client.last_name] if part
                ).strip() or None

                EmailService.send_consultation_payment_confirmation_professional_email(
                    to_email=lawyer.email,
                    first_name=lawyer.first_name,
                    client_name=client_name,
                    consultation_reference=str(session_obj.id),
                    consultation_date=consultation_date,
                    consultation_time=consultation_time,
                    consultation_length=consultation_length,
                )
            else:
                logger.warning(
                    "Skipping consultation payment confirmation (professional) email: no lawyer email for payment %s",
                    payment.id,
                )
        except Exception as exc:
            logger.warning(
                "Failed to send consultation payment confirmation email for payment %s: %s",
                payment.id, exc,
            )
    
    
    def _get_session(self, session_id: uuid.UUID) -> ReferralSession:
        session_obj = self.session.query(ReferralSession).filter(ReferralSession.id == session_id).first()
        if not session_obj:
            raise ValueError("Consultation session not found")
        return session_obj

    def _get_referral(self, referral_id: uuid.UUID) -> ReferralRequest:
        referral = self.session.query(ReferralRequest).filter(ReferralRequest.id == referral_id).first()
        if not referral:
            raise ValueError("Referral request not found")
        return referral

    def _get_user(self, user_id: uuid.UUID) -> User:
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")
        return user

    def _get_payment_for_session(self, session_id: uuid.UUID) -> Optional[ConsultationPayment]:
        return self.session.query(ConsultationPayment).filter(
            ConsultationPayment.session_id == session_id
        ).first()

    def _require_payment_for_session(self, session_id: uuid.UUID) -> ConsultationPayment:
        payment = self._get_payment_for_session(session_id)
        if not payment:
            raise ValueError("No consultation payment exists for this session")
        return payment

    def _normalize_pricing_type(self, pricing_type: str) -> str:
        if not pricing_type:
            raise ValueError("pricing_type is required")
        return pricing_type.strip().lower()

    def _gbp_to_pence(self, amount_gbp: Decimal) -> int:
        decimal_amount = Decimal(str(amount_gbp))
        return int((decimal_amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _pence_to_gbp_string(self, amount_pence: int) -> str:
        return f"{(Decimal(amount_pence) / Decimal('100')).quantize(Decimal('0.01'))}"
