# services/stripe_service.py
"""
Stripe Payment Service for LawNeeds
Handles all Stripe-related operations for subscriptions and payments.
"""

import os
import logging
import json
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal
from config.environment import (
    get_payments_env,
    get_stripe_webhook_secret,
    load_environment,
)

logger = logging.getLogger(__name__)

# Only import stripe if available
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    logger.warning("Stripe library not installed. Payment processing disabled.")


class StripeService:
    """
    Service class for Stripe payment operations.
    
    Handles:
    - Customer management
    - Subscription creation/updates
    - One-time payments (top-ups, daily passes)
    - Payment method management
    """
    
    def __init__(self):
        load_environment()
        self.api_key = os.getenv('STRIPE_API_KEY')
        self.payments_env = get_payments_env()
        self.webhook_secret = get_stripe_webhook_secret(connect=False)
        self.connect_webhook_secret = get_stripe_webhook_secret(connect=True)
        
        if STRIPE_AVAILABLE and self.api_key:
            stripe.api_key = self.api_key
            self.enabled = True
        else:
            self.enabled = False
            if not self.api_key:
                logger.warning("STRIPE_API_KEY not set. Stripe payments disabled.")
    
    def is_enabled(self) -> bool:
        """Check if Stripe is properly configured"""
        return self.enabled and STRIPE_AVAILABLE
    
    # ============================================
    # CUSTOMER MANAGEMENT
    # ============================================
    
    def create_customer(
        self,
        email: str,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Create a Stripe customer.
        
        Returns: (success, message, customer_id)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock customer created", "mock_cus_" + email.replace("@", "_")
        
        try:
            customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata=metadata or {}
            )
            logger.info(f"Created Stripe customer: {customer.id}")
            return True, "Customer created", customer.id
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating customer: {e}")
            return False, str(e), None
    
    def get_or_create_customer(
        self,
        email: str,
        stripe_customer_id: Optional[str] = None,
        name: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Get existing customer or create new one.
        
        Returns: (success, message, customer_id)
        """
        if not self.is_enabled():
            return True, "Stripe disabled", "mock_cus_" + (user_id or email.replace("@", "_"))
        
        # If we have a customer ID, verify it exists
        if stripe_customer_id:
            try:
                customer = stripe.Customer.retrieve(stripe_customer_id)
                if customer and not customer.get('deleted'):
                    return True, "Customer found", stripe_customer_id
            except stripe.error.InvalidRequestError:
                pass  # Customer doesn't exist, create new one
        
        # Create new customer
        return self.create_customer(
            email=email,
            name=name,
            metadata={'user_id': user_id} if user_id else None
        )

    # ============================================
    # STRIPE CONNECT MARKETPLACE
    # ============================================

    def create_connect_account(
        self,
        lawyer_email: str,
        country: str = "GB",
        metadata: Optional[Dict[str, str]] = None
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Create a Stripe Connect Express account for a lawyer.

        Returns: (success, message, account_id)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock connect account created", "mock_acct_" + lawyer_email.replace("@", "_")

        try:
            account = stripe.Account.create(
                type="express",
                country=country,
                email=lawyer_email,
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                metadata=metadata or {},
            )
            logger.info(f"Created Stripe Connect account: {account.id}")
            return True, "Connect account created", account.id
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating Connect account: {e}")
            return False, str(e), None

    def create_onboarding_link(
        self,
        account_id: str,
        return_url: str,
        refresh_url: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Create a Stripe Connect onboarding link.

        Returns: (success, message, onboarding_url)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock onboarding link created", f"{return_url}?mock_account_id={account_id}"

        try:
            account_link = stripe.AccountLink.create(
                account=account_id,
                return_url=return_url,
                refresh_url=refresh_url,
                type="account_onboarding",
            )
            return True, "Onboarding link created", account_link.url
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating onboarding link: {e}")
            return False, str(e), None

    def create_express_dashboard_login_link(
        self,
        account_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Create a short-lived Stripe Express Dashboard login link.

        Returns: (success, message, login_url)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock dashboard link created", "https://connect.stripe.com/express_login"

        try:
            login_link = stripe.Account.create_login_link(account_id)
            return True, "Dashboard login link created", login_link.url
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating Express Dashboard login link: {e}")
            return False, str(e), None

    def get_account_status(
        self,
        account_id: str
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Retrieve the current status for a Stripe Connect account.

        Returns: (success, message, account_status)
        """
        if not self.is_enabled():
            return True, "Stripe disabled", {
                "account_id": account_id,
                "details_submitted": True,
                "charges_enabled": True,
                "payouts_enabled": True,
                "onboarded": True,
            }

        try:
            account = stripe.Account.retrieve(account_id)
            status = {
                "account_id": account.id,
                "details_submitted": bool(account.details_submitted),
                "charges_enabled": bool(account.charges_enabled),
                "payouts_enabled": bool(account.payouts_enabled),
            }
            status["onboarded"] = all(
                [status["details_submitted"], status["charges_enabled"], status["payouts_enabled"]]
            )
            return True, "Account status retrieved", status
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error retrieving Connect account status: {e}")
            return False, str(e), None

    @staticmethod
    def is_connect_account_mode_mismatch(message: Optional[str]) -> bool:
        normalized = (message or "").lower()
        return (
            "test account created with a testmode key" in normalized
            or "can only be used with testmode keys" in normalized
            or "live mode account" in normalized and "test mode key" in normalized
        )
    
    # ============================================
    # SUBSCRIPTION MANAGEMENT
    # ============================================
    
    def create_subscription(
        self,
        customer_id: str,
        price_id: str,
        payment_method_id: Optional[str] = None,
        trial_days: int = 0,
        coupon_id: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Create a Stripe subscription.
        
        Returns: (success, message, subscription_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock subscription created", {
                'id': 'mock_sub_' + customer_id,
                'status': 'active',
                'current_period_end': None
            }
        
        try:
            subscription_data = {
                'customer': customer_id,
                'items': [{'price': price_id}],
                'metadata': metadata or {},
                'payment_behavior': 'default_incomplete',
                'expand': ['latest_invoice.payment_intent']
            }
            
            if payment_method_id:
                subscription_data['default_payment_method'] = payment_method_id
            
            if trial_days > 0:
                subscription_data['trial_period_days'] = trial_days
            
            if coupon_id:
                subscription_data['coupon'] = coupon_id
            
            subscription = stripe.Subscription.create(**subscription_data)
            
            logger.info(f"Created Stripe subscription: {subscription.id}")
            
            return True, "Subscription created", {
                'id': subscription.id,
                'status': subscription.status,
                'current_period_end': subscription.current_period_end,
                'client_secret': subscription.latest_invoice.payment_intent.client_secret 
                    if subscription.latest_invoice and subscription.latest_invoice.payment_intent else None
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating subscription: {e}")
            return False, str(e), None
    
    def update_subscription(
        self,
        subscription_id: str,
        new_price_id: Optional[str] = None,
        cancel_at_period_end: Optional[bool] = None,
        metadata: Optional[Dict[str, str]] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Update a Stripe subscription.
        
        Returns: (success, message, subscription_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled", {'id': subscription_id, 'status': 'active'}
        
        try:
            update_data = {}
            
            if new_price_id:
                # Get current subscription to find item ID
                sub = stripe.Subscription.retrieve(subscription_id)
                if sub.items.data:
                    update_data['items'] = [{
                        'id': sub.items.data[0].id,
                        'price': new_price_id
                    }]
            
            if cancel_at_period_end is not None:
                update_data['cancel_at_period_end'] = cancel_at_period_end
            
            if metadata:
                update_data['metadata'] = metadata
            
            if update_data:
                subscription = stripe.Subscription.modify(subscription_id, **update_data)
                return True, "Subscription updated", {
                    'id': subscription.id,
                    'status': subscription.status
                }
            
            return True, "No changes made", None
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error updating subscription: {e}")
            return False, str(e), None
    
    def cancel_subscription(
        self,
        subscription_id: str,
        immediately: bool = False
    ) -> Tuple[bool, str]:
        """
        Cancel a Stripe subscription.
        
        Returns: (success, message)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - subscription cancelled (mock)"
        
        try:
            if immediately:
                stripe.Subscription.delete(subscription_id)
                return True, "Subscription cancelled immediately"
            else:
                stripe.Subscription.modify(
                    subscription_id,
                    cancel_at_period_end=True
                )
                return True, "Subscription will cancel at period end"
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error cancelling subscription: {e}")
            return False, str(e)
    
    def reactivate_subscription(
        self,
        subscription_id: str
    ) -> Tuple[bool, str]:
        """
        Reactivate a subscription scheduled for cancellation.
        
        Returns: (success, message)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - subscription reactivated (mock)"
        
        try:
            stripe.Subscription.modify(
                subscription_id,
                cancel_at_period_end=False
            )
            return True, "Subscription reactivated"
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error reactivating subscription: {e}")
            return False, str(e)
    
    # ============================================
    # ONE-TIME PAYMENTS
    # ============================================
    
    def create_payment_intent(
        self,
        amount_gbp: Decimal,
        customer_id: str,
        payment_method_id: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Create a payment intent for one-time charges (top-ups, daily pass).
        
        Returns: (success, message, payment_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock payment created", {
                'id': 'mock_pi_' + customer_id,
                'status': 'succeeded',
                'client_secret': 'mock_secret'
            }
        
        try:
            amount_pence = int(amount_gbp * 100)  # Convert to pence
            
            intent_data = {
                'amount': amount_pence,
                'currency': 'gbp',
                'customer': customer_id,
                'metadata': metadata or {},
                'description': description,
            }
            
            if payment_method_id:
                intent_data['payment_method'] = payment_method_id
                intent_data['confirm'] = True
                intent_data['off_session'] = True
            
            payment_intent = stripe.PaymentIntent.create(**intent_data)
            
            logger.info(f"Created payment intent: {payment_intent.id}")
            
            return True, "Payment intent created", {
                'id': payment_intent.id,
                'status': payment_intent.status,
                'client_secret': payment_intent.client_secret
            }
        except stripe.error.CardError as e:
            logger.error(f"Card error: {e}")
            return False, f"Card error: {e.user_message}", None
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating payment: {e}")
            return False, str(e), None
    
    def confirm_payment_intent(
        self,
        payment_intent_id: str,
        payment_method_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Confirm a payment intent.
        
        Returns: (success, message)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - payment confirmed (mock)"
        
        try:
            intent = stripe.PaymentIntent.confirm(
                payment_intent_id,
                payment_method=payment_method_id
            )
            
            if intent.status == 'succeeded':
                return True, "Payment successful"
            elif intent.status == 'requires_action':
                return False, "Additional authentication required"
            else:
                return False, f"Payment status: {intent.status}"
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error confirming payment: {e}")
            return False, str(e)

    def create_consultation_payment_intent(
        self,
        amount_pence: int,
        customer_id: str,
        lawyer_connect_id: Optional[str],
        application_fee_pence: int,
        metadata: Optional[Dict[str, str]] = None,
        payment_method_id: Optional[str] = None,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Create a PaymentIntent for a consultation marketplace payment.

        The payment is collected on the platform account and the lawyer payout is
        released after the session ends. `lawyer_connect_id` is kept for
        traceability and future payout routing.

        Returns: (success, message, payment_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock consultation payment intent created", {
                "id": "mock_consultation_pi_" + customer_id,
                "status": "requires_payment_method",
                "client_secret": "mock_consultation_secret",
                "transfer_group": (metadata or {}).get("transfer_group"),
            }

        try:
            intent_metadata = metadata.copy() if metadata else {}
            if lawyer_connect_id:
                intent_metadata["lawyer_connect_id"] = lawyer_connect_id
            intent_metadata["application_fee_pence"] = str(application_fee_pence)

            intent_data = {
                "amount": amount_pence,
                "currency": "gbp",
                "customer": customer_id,
                "metadata": intent_metadata,
                "description": description,
                "transfer_group": intent_metadata.get("transfer_group"),
                "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
            }

            if payment_method_id:
                intent_data["payment_method"] = payment_method_id
                intent_data["confirm"] = True

            if idempotency_key:
                intent_data["idempotency_key"] = idempotency_key
            payment_intent = stripe.PaymentIntent.create(**intent_data)

            logger.info(f"Created consultation payment intent: {payment_intent.id}")

            return True, "Consultation payment intent created", {
                "id": payment_intent.id,
                "status": payment_intent.status,
                "client_secret": payment_intent.client_secret,
                "transfer_group": payment_intent.transfer_group,
            }
        except stripe.error.CardError as e:
            logger.error(f"Card error creating consultation payment: {e}")
            return False, f"Card error: {e.user_message}", None
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating consultation payment intent: {e}")
            return False, str(e), None

    def get_payment_intent(
        self,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Retrieve a payment intent by ID.

        Returns: (success, message, payment_intent_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled", {
                "id": payment_intent_id,
                "status": "succeeded",
                "client_secret": "mock_consultation_secret",
                "latest_charge": "mock_charge_id",
                "transfer_group": "mock_transfer_group",
                "metadata": {},
            }

        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            return True, "Payment intent retrieved", {
                "id": payment_intent.id,
                "status": payment_intent.status,
                "client_secret": payment_intent.client_secret,
                "customer": payment_intent.customer,
                "latest_charge": payment_intent.latest_charge,
                "amount": payment_intent.amount,
                "amount_received": payment_intent.amount_received,
                "transfer_group": payment_intent.transfer_group,
                "metadata": dict(payment_intent.metadata or {}),
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error retrieving payment intent: {e}")
            return False, str(e), None

    def cancel_payment_intent(self, payment_intent_id: str) -> Tuple[bool, str]:
        """Cancel an unpaid PaymentIntent when its application payment window expires."""
        if not self.is_enabled():
            return True, "Stripe disabled - payment intent cancelled"
        try:
            intent = stripe.PaymentIntent.cancel(payment_intent_id)
            return True, f"Payment intent {intent.id} cancelled"
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error cancelling payment intent: {e}")
            return False, str(e)

    def transfer_to_lawyer(
        self,
        amount_pence: int,
        connect_account_id: str,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Transfer consultation funds to the lawyer after session completion.

        Returns: (success, message, transfer_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock transfer created", {
                "id": "mock_tr_" + payment_intent_id,
                "destination": connect_account_id,
                "amount": amount_pence,
            }

        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            charge_id = payment_intent.latest_charge

            if not charge_id:
                return False, "Payment intent does not have a captured charge yet", None

            transfer = stripe.Transfer.create(
                amount=amount_pence,
                currency="gbp",
                destination=connect_account_id,
                source_transaction=charge_id,
                transfer_group=payment_intent.transfer_group,
            )

            logger.info(f"Created transfer {transfer.id} for payment intent {payment_intent_id}")

            return True, "Transfer created", {
                "id": transfer.id,
                "destination": transfer.destination,
                "amount": transfer.amount,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating transfer: {e}")
            return False, str(e), None

    def refund_payment_intent(
        self,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Refund a consultation payment intent.

        Returns: (success, message, refund_data)
        """
        if not self.is_enabled():
            return True, "Stripe disabled - mock refund created", {
                "id": "mock_re_" + payment_intent_id,
                "status": "succeeded",
            }

        try:
            refund = stripe.Refund.create(payment_intent=payment_intent_id)
            logger.info(f"Refund created for payment intent {payment_intent_id}: {refund.id}")
            return True, "Refund created", {
                "id": refund.id,
                "status": refund.status,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating refund: {e}")
            return False, str(e), None

    def construct_webhook_event(
        self,
        payload: bytes,
        signature: Optional[str],
        *,
        connect: bool = False,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Verify and construct a Stripe webhook event.

        Returns: (success, message, event_payload)
        """
        try:
            webhook_secret = self.connect_webhook_secret if connect else self.webhook_secret
            if self.is_enabled():
                if not webhook_secret:
                    webhook_type = "Connect" if connect else "platform"
                    return (
                        False,
                        f"Stripe {webhook_type} webhook secret not configured for {self.payments_env}",
                        None,
                    )
                if not signature:
                    return False, "Missing Stripe signature", None

                event = stripe.Webhook.construct_event(
                    payload=payload,
                    sig_header=signature,
                    secret=webhook_secret,
                )
                return True, "Webhook verified", event

            parsed_event = json.loads(payload.decode("utf-8"))
            return True, "Webhook parsed without signature verification", parsed_event
        except ValueError as e:
            logger.error(f"Invalid Stripe webhook payload: {e}")
            return False, "Invalid payload", None
        except stripe.error.SignatureVerificationError as e:
            logger.error(
                "Stripe webhook signature verification failed: %s "
                "(payments_env=%s, scope=%s, webhook_secret_configured=%s, signature_header_present=%s, payload_bytes=%s)",
                e,
                self.payments_env,
                "connect" if connect else "platform",
                bool(webhook_secret),
                bool(signature),
                len(payload or b""),
            )
            return False, "Invalid Stripe signature", None
        except stripe.error.StripeError as e:
            logger.error(f"Stripe webhook error: {e}")
            return False, str(e), None
    
    # ============================================
    # PRICE MANAGEMENT
    # ============================================
    
    def get_price_id_for_plan(self, plan_id: str) -> Optional[str]:
        """
        Get the Stripe price ID for a subscription plan.
        
        Price IDs should be stored in environment variables like:
        STRIPE_PRICE_STANDARD_WEEKLY=price_xxxxx
        """
        env_key = f"STRIPE_PRICE_{plan_id.upper()}"
        price_id = os.getenv(env_key)
        
        if not price_id:
            logger.warning(f"No Stripe price ID found for plan: {plan_id}")
        
        return price_id
    
    # ============================================
    # COUPON/DISCOUNT MANAGEMENT
    # ============================================
    
    def get_first_month_coupon_id(self) -> Optional[str]:
        """
        Get the coupon ID for first month 50% discount.
        """
        return os.getenv('STRIPE_COUPON_FIRST_MONTH_50')
    
    def create_first_month_coupon(self) -> Tuple[bool, str, Optional[str]]:
        """
        Create a 50% off first month coupon (run once during setup).
        
        Returns: (success, message, coupon_id)
        """
        if not self.is_enabled():
            return True, "Stripe disabled", "mock_coupon_50off"
        
        try:
            coupon = stripe.Coupon.create(
                percent_off=50,
                duration='once',
                name='First Month 50% Off',
                metadata={'type': 'first_month_discount'}
            )
            logger.info(f"Created coupon: {coupon.id}")
            return True, "Coupon created", coupon.id
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error creating coupon: {e}")
            return False, str(e), None
